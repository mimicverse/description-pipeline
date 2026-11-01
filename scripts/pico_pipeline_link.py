"""Pair a local PICO gateway with one remote pipeline simulation.

Run in the lightweight PICO link environment, separately from the Python 3.10 SDK.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import math
from pathlib import Path
import re
from urllib.parse import urlsplit
import warnings

PICO_INPUT_TOPIC = b"pico_input/v2 "
LINK_SETUP_SCHEMA = "solidworks-to-urdf.pico-link/v1"


class LinkError(ValueError):
    """An actionable transport failure that contains no pairing credentials."""


def validate_endpoint(url, *, local=False):
    if not isinstance(url, str) or url != url.strip():
        raise LinkError("Endpoint must be an absolute URL without surrounding whitespace")
    try:
        parsed = urlsplit(url)
    except ValueError:
        raise LinkError("Endpoint URL is invalid") from None
    if not parsed.hostname:
        raise LinkError("Endpoint must include a hostname")
    try:
        port = parsed.port
    except ValueError:
        raise LinkError("Endpoint port is invalid") from None
    if port is not None and not 1 <= port <= 65535:
        raise LinkError("Endpoint port is invalid")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise LinkError("Endpoints must not contain credentials, queries or fragments")
    loopback = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    if local and (parsed.scheme != "tcp" or not loopback or parsed.path or port is None):
        raise LinkError("The PICO gateway must use a loopback TCP endpoint with a port")
    if not local and (
        parsed.scheme not in {"ws", "wss"} or (parsed.scheme == "ws" and not loopback) or parsed.path not in {"", "/"}
    ):
        raise LinkError("Remote pipeline input requires a WSS origin")
    return url.rstrip("/")


def read_setup(path):
    try:
        with path.open("rb") as stream:
            raw = stream.read(4097)
        if len(raw) > 4096:
            raise LinkError("Link setup exceeds its size limit; download a new setup file")
        setup = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise LinkError("Cannot read link setup JSON; select the file downloaded from the PICO page") from None
    if (
        not isinstance(setup, dict)
        or set(setup) != {"schema_version", "origin", "session_id"}
        or setup["schema_version"] != LINK_SETUP_SCHEMA
    ):
        raise LinkError("Link setup schema is unsupported; download a new setup file")
    session_id = setup["session_id"]
    if not isinstance(session_id, str) or re.fullmatch(r"[0-9a-f]{32}", session_id) is None:
        raise LinkError("Link setup session identity is invalid")
    return validate_endpoint(setup["origin"]), session_id


def transport_dependencies():
    try:
        from websockets.asyncio.client import connect
        from websockets.exceptions import ConnectionClosed, InvalidHandshake
        import zmq
        import zmq.asyncio
    except ImportError:
        raise LinkError("PICO link dependencies are missing; run the lightweight link installer") from None
    return connect, ConnectionClosed, InvalidHandshake, zmq


async def run(origin, session_id, code, gateway):
    """Forward every original transition; overflow and sequence gaps revoke the link."""
    connect, ConnectionClosed, InvalidHandshake, zmq = transport_dependencies()
    queue = asyncio.Queue(maxsize=128)
    context = zmq.asyncio.Context()
    source = context.socket(zmq.SUB)
    source.setsockopt(zmq.SUBSCRIBE, PICO_INPUT_TOPIC)
    source.setsockopt(zmq.RCVHWM, 256)
    source.setsockopt(zmq.LINGER, 0)
    source.connect(gateway)

    async def receive():
        previous = None
        while True:
            try:
                packet = await asyncio.wait_for(source.recv(), 1)
            except TimeoutError:
                raise LinkError("No fresh PICO gateway input; check the SDK gateway and restart the link") from None
            if not packet.startswith(PICO_INPUT_TOPIC) or len(packet) > 64 * 1024:
                raise LinkError("PICO raw transport envelope is invalid")
            try:
                frame = json.loads(packet[len(PICO_INPUT_TOPIC) :])
                identity = (frame["source_session_id"], frame["sequence"])
            except (ValueError, TypeError, KeyError):
                raise LinkError("PICO gateway input is malformed") from None
            if not isinstance(identity[0], str) or not identity[0] or type(identity[1]) is not int or identity[1] < 0:
                raise LinkError("PICO transport sequence identity is invalid")
            if previous is not None and (identity[0] != previous[0] or identity[1] != previous[1] + 1):
                raise LinkError("PICO source restarted or a raw transition was lost; start a new simulation")
            previous = identity
            try:
                queue.put_nowait((frame, asyncio.get_running_loop().time()))
            except asyncio.QueueFull:
                raise LinkError("PICO input queue overflowed; restart the link with a fresh pairing code") from None

    async def forward():
        async with connect(
            f"{origin}/api/simulations/{session_id}/input", max_size=65536, max_queue=4, open_timeout=5, close_timeout=2
        ) as websocket:
            await websocket.send(json.dumps({"pairing_code": code}))
            try:
                paired = json.loads(await asyncio.wait_for(websocket.recv(), 5))
            except (ConnectionClosed, TimeoutError):
                raise LinkError("Pairing was rejected or expired; generate a fresh code for this session") from None
            except ValueError:
                raise LinkError("Pipeline pairing response is malformed") from None
            if not isinstance(paired, dict) or paired.get("type") != "paired":
                raise LinkError("Pipeline pairing failed; generate a fresh code for this session")
            anchor = paired.get("anchor")
            if (
                not isinstance(paired.get("binding"), str) or not paired["binding"]
                or type(anchor) not in (int, float) or not math.isfinite(anchor)
            ):
                raise LinkError("Pipeline pairing response is malformed")
            while True:
                try:
                    frame, received = await asyncio.wait_for(queue.get(), 0.5)
                except TimeoutError:
                    if websocket.close_code is not None:
                        raise LinkError("Pipeline input disconnected; inspect the session and pair again") from None
                    raise LinkError("PICO gateway input stopped; check the SDK gateway and pair again") from None
                if asyncio.get_running_loop().time() - received > 0.1:
                    raise LinkError("PICO input backlog exceeded its freshness budget; pair again")
                try:
                    payload = json.dumps(
                        {"binding": paired["binding"], "anchor": anchor, "frame": frame}, allow_nan=False,
                    )
                except ValueError:
                    raise LinkError("PICO gateway frame cannot be encoded as strict JSON") from None
                await websocket.send(payload)
                try:
                    reply = json.loads(await asyncio.wait_for(websocket.recv(), 0.25))
                except TimeoutError:
                    raise LinkError("Pipeline input acknowledgement timed out; check the link and pair again") from None
                except ValueError:
                    raise LinkError("Pipeline input acknowledgement is malformed") from None
                if not isinstance(reply, dict) or reply.get("type") != "input_ack":
                    raise LinkError("Pipeline did not acknowledge the input")
                anchor = reply.get("anchor")
                if type(anchor) not in (int, float) or not math.isfinite(anchor):
                    raise LinkError("Pipeline input acknowledgement is malformed")

    tasks = [asyncio.create_task(receive()), asyncio.create_task(forward())]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    except (InvalidHandshake, OSError):
        raise LinkError("Cannot connect to the pipeline; check the setup origin, TLS connection and session") from None
    except ConnectionClosed:
        raise LinkError("Pipeline input disconnected; inspect the session and generate a fresh pairing code") from None
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        source.close()
        context.term()


def main():
    parser = argparse.ArgumentParser(description="Forward local PICO raw frames to a paired pipeline session")
    parser.add_argument("setup", nargs="?", type=Path, help="Link setup JSON downloaded from the PICO page")
    parser.add_argument("--origin", help="Advanced: pipeline WSS origin, without a path")
    parser.add_argument("--session", help="Advanced: simulation session identity from the portal")
    parser.add_argument("--gateway", default="tcp://127.0.0.1:5560")
    args = parser.parse_args()
    if args.setup and (args.origin or args.session):
        parser.error("Use a setup file or the advanced origin/session options")
    if not args.setup and (not args.origin or not args.session):
        parser.error("Select a downloaded setup file: pico-link /path/to/pico-link-session.json")
    try:
        if args.setup:
            origin, session_id = read_setup(args.setup)
        else:
            origin, session_id = validate_endpoint(args.origin), args.session
            if re.fullmatch(r"[0-9a-f]{32}", session_id) is None:
                raise LinkError("Simulation session identity is invalid")
        gateway = validate_endpoint(args.gateway, local=True)
        transport_dependencies()
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            code = getpass.getpass("One-time pairing code: ")
        if not code:
            raise LinkError("Pairing code is empty; generate a fresh code in the PICO page")
        asyncio.run(run(origin, session_id, code, gateway))
    except getpass.GetPassWarning:
        parser.exit(2, "Open an interactive terminal to enter the pairing code privately.\n")
    except (KeyboardInterrupt, EOFError):
        parser.exit(130, "PICO link stopped by the operator. Input disconnected.\n")
    except LinkError as error:
        parser.exit(1, f"PICO link stopped: {error}. Input disconnected.\n")
    except Exception as error:
        parser.exit(1, f"PICO link stopped: unexpected {type(error).__name__}; inspect the session before retrying.\n")


if __name__ == "__main__":
    main()
