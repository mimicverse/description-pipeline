"""Pair a local PICO gateway with one remote pipeline simulation.

Run in the lightweight PICO link environment, separately from the Python 3.10 SDK.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import re
from urllib.parse import urlsplit

from websockets.asyncio.client import connect
import zmq
import zmq.asyncio

PICO_INPUT_TOPIC = b"pico_input/v2 "


def validate_endpoint(url, *, local=False):
    parsed = urlsplit(url)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Endpoints must not contain credentials, queries or fragments")
    loopback = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    if local and (parsed.scheme != "tcp" or not loopback or parsed.path):
        raise ValueError("The PICO gateway must use a loopback TCP endpoint")
    if not local and (
        parsed.scheme not in {"ws", "wss"} or (parsed.scheme == "ws" and not loopback) or parsed.path not in {"", "/"}
    ):
        raise ValueError("Remote pipeline input requires a WSS origin")
    return url.rstrip("/")


async def run(origin, session_id, code, gateway):
    """Forward every original transition; overflow and sequence gaps revoke the link."""
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
            packet = await asyncio.wait_for(source.recv(), 1)
            if not packet.startswith(PICO_INPUT_TOPIC) or len(packet) > 64 * 1024:
                raise ValueError("PICO raw transport envelope is invalid")
            frame = json.loads(packet[len(PICO_INPUT_TOPIC) :])
            identity = (frame["source_session_id"], frame["sequence"])
            if not isinstance(identity[0], str) or type(identity[1]) is not int:
                raise ValueError("PICO transport sequence identity is invalid")
            if previous is not None and (identity[0] != previous[0] or identity[1] != previous[1] + 1):
                raise ValueError("PICO source restarted or a raw transition was lost; start a new simulation")
            previous = identity
            queue.put_nowait((frame, asyncio.get_running_loop().time()))

    async def forward():
        async with connect(
            f"{origin}/api/simulations/{session_id}/input", max_size=65536, max_queue=4, open_timeout=5, close_timeout=2
        ) as websocket:
            await websocket.send(json.dumps({"pairing_code": code}))
            paired = json.loads(await asyncio.wait_for(websocket.recv(), 5))
            if paired.get("type") != "paired":
                raise ValueError("Pipeline pairing failed")
            anchor = paired["anchor"]
            while True:
                frame, received = await asyncio.wait_for(queue.get(), 0.5)
                if asyncio.get_running_loop().time() - received > 0.1:
                    raise ValueError("PICO input backlog exceeded the local freshness budget")
                await websocket.send(
                    json.dumps(
                        {"binding": paired["binding"], "anchor": anchor, "frame": frame},
                        allow_nan=False,
                    )
                )
                reply = json.loads(await asyncio.wait_for(websocket.recv(), 0.25))
                if reply.get("type") != "input_ack":
                    raise ValueError("Pipeline did not acknowledge the input")
                anchor = reply["anchor"]

    tasks = [asyncio.create_task(receive()), asyncio.create_task(forward())]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        source.close()
        context.term()


def main():
    parser = argparse.ArgumentParser(description="Forward local PICO raw frames to a paired pipeline session")
    parser.add_argument("--origin", required=True, help="Pipeline WSS origin, without a path")
    parser.add_argument("--session", required=True, help="Simulation session identity from the portal")
    parser.add_argument("--gateway", default="tcp://127.0.0.1:5560")
    args = parser.parse_args()
    if re.fullmatch(r"[0-9a-f]{32}", args.session) is None:
        parser.error("Simulation session identity is invalid")
    try:
        origin = validate_endpoint(args.origin)
        gateway = validate_endpoint(args.gateway, local=True)
        code = getpass.getpass("One-time pairing code: ")
        asyncio.run(run(origin, args.session, code, gateway))
    except (Exception, KeyboardInterrupt) as error:
        parser.exit(1, f"PICO link stopped: {type(error).__name__}. Input has been revoked.\n")


if __name__ == "__main__":
    main()
