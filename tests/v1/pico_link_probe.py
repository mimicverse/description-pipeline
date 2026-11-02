"""Exercise the real link's ZMQ and WebSocket transports in its private environment."""

import asyncio
import json
from pathlib import Path
import runpy
import sys

from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed
import zmq
import zmq.asyncio

LINK_SCRIPT = (
    Path(sys.argv[1]) if len(sys.argv) == 2
    else Path(__file__).resolve().parents[2] / "scripts/pico_pipeline_link.py"
)


async def probe(gap):
    link = runpy.run_path(str(LINK_SCRIPT))
    for endpoint in ("ws://remote.example", "wss://user:password@example.test", "wss://example.test/?secret=x"):
        try:
            link["validate_endpoint"](endpoint)
        except ValueError:
            pass
        else:
            raise AssertionError("Unsafe endpoint was accepted")
    context = zmq.asyncio.Context()
    publisher = context.socket(zmq.XPUB)
    publisher.setsockopt(zmq.LINGER, 0)
    publisher.bind("tcp://127.0.0.1:*")
    gateway = publisher.getsockopt_string(zmq.LAST_ENDPOINT)
    paired, received = asyncio.Event(), []

    async def handle(websocket):
        assert json.loads(await websocket.recv()) == {"pairing_code": "fixture-only"}
        await websocket.send(json.dumps({"type": "paired", "binding": "fixture", "anchor": 1}))
        paired.set()
        try:
            while True:
                message = json.loads(await websocket.recv())
                received.append(message["frame"]["left_grip"])
                await websocket.send(json.dumps({"type": "input_ack", "anchor": len(received) + 1}))
                if len(received) == 3:
                    return
        except ConnectionClosed:
            pass

    try:
        async with serve(handle, "127.0.0.1", 0) as server:
            origin = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
            task = asyncio.create_task(link["run"](origin, "0" * 32, "fixture-only", gateway))
            await asyncio.wait_for(paired.wait(), 2)
            assert await asyncio.wait_for(publisher.recv(), 2) == b"\x01pico_input/v2 "
            pose = (0, 0, 0, 0, 0, 0, 1)
            for sequence, grip in enumerate((0, 1, 0)):
                frame = {
                    "sequence": sequence + (1 if gap and sequence > 0 else 0),
                    "device_timestamp_ns": sequence + 1,
                    "gateway_monotonic_ns": sequence + 1,
                    "left_controller_pose_xyzw": pose,
                    "right_controller_pose_xyzw": pose,
                    "headset_pose_xyzw": pose,
                    "schema_version": 2,
                    "left_grip": grip,
                    "source_session_id": "fixture-source",
                }
                await publisher.send(b"pico_input/v2 " + json.dumps(frame).encode())
                await asyncio.sleep(0.03)
            try:
                await asyncio.wait_for(task, 2)
            except link["LinkError"] as error:
                assert ("raw transition was lost" if gap else "Pipeline input disconnected") in str(error), (
                    str(error), received,
                )
            except (ConnectionClosed, TimeoutError):
                assert not gap
            assert received == ([0] if gap else [0, 1, 0]), received
    finally:
        publisher.close()
        context.term()


async def error_probe(kind, expected):
    link = runpy.run_path(str(LINK_SCRIPT))
    context = zmq.asyncio.Context()
    publisher = context.socket(zmq.XPUB)
    publisher.setsockopt(zmq.LINGER, 0)
    publisher.bind("tcp://127.0.0.1:*")
    gateway = publisher.getsockopt_string(zmq.LAST_ENDPOINT)
    ready, closed = asyncio.Event(), asyncio.Event()

    async def handle(websocket):
        try:
            assert json.loads(await websocket.recv()) == {"pairing_code": "fixture-only"}
            ready.set()
            if kind == "rejected":
                await websocket.close(code=1008, reason="fixture-only")
                return
            await websocket.send(json.dumps(
                [] if kind == "bad-pair" else {"type": "paired", "binding": "fixture", "anchor": 1}
            ))
            if kind in {"ack-timeout", "bad-ack"}:
                await websocket.recv()
                if kind == "bad-ack":
                    await websocket.send(json.dumps({"type": "input_ack", "anchor": True}))
            await websocket.wait_closed()
        finally:
            closed.set()

    async def publish():
        await ready.wait()
        assert await asyncio.wait_for(publisher.recv(), 2) == b"\x01pico_input/v2 "
        sequence = 0
        while True:
            await publisher.send(b"pico_input/v2 " + json.dumps({
                "source_session_id": "fixture-source", "sequence": sequence,
            }).encode())
            sequence += 1
            await asyncio.sleep(0.02)

    producer = None
    try:
        async with serve(handle, "127.0.0.1", 0) as server:
            if kind in {"ack-timeout", "bad-ack"}:
                producer = asyncio.create_task(publish())
            origin = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
            try:
                await asyncio.wait_for(link["run"](origin, "0" * 32, "fixture-only", gateway), 3)
            except link["LinkError"] as error:
                assert expected in str(error), (kind, str(error))
                assert "fixture-only" not in str(error)
            else:
                raise AssertionError("Transport failure was not reported")
            await asyncio.wait_for(closed.wait(), 2)
    finally:
        if producer is not None:
            producer.cancel()
            await asyncio.gather(producer, return_exceptions=True)
        publisher.close()
        context.term()


asyncio.run(probe(False))
asyncio.run(probe(True))
for kind, expected in (
    ("rejected", "Pairing was rejected or expired"),
    ("no-input", "PICO gateway input stopped"),
    ("bad-pair", "Pipeline pairing failed"),
    ("ack-timeout", "acknowledgement timed out"),
    ("bad-ack", "acknowledgement is malformed"),
):
    asyncio.run(error_probe(kind, expected))
print("PICO link: transitions, source gaps, pairing, input loss, malformed replies and ACK timeout passed")
