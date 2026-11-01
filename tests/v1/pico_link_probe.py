"""Exercise the real link's ZMQ and WebSocket transports in its private environment."""

import asyncio
import json
from pathlib import Path
import runpy

from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed
import zmq
import zmq.asyncio


async def probe(gap):
    link = runpy.run_path(str(Path(__file__).resolve().parents[2] / "scripts/pico_pipeline_link.py"))
    for endpoint in ("ws://remote.example", "wss://user:password@example.test", "wss://example.test/?secret=x"):
        try:
            link["validate_endpoint"](endpoint)
        except ValueError:
            pass
        else:
            raise AssertionError("Unsafe endpoint was accepted")
    context = zmq.asyncio.Context()
    publisher = context.socket(zmq.PUB)
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
            await asyncio.sleep(0.15)
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
            except ValueError:
                assert gap
            except (ConnectionClosed, TimeoutError):
                assert not gap
            assert received == ([0] if gap else [0, 1, 0]), received
    finally:
        publisher.close()
        context.term()


asyncio.run(probe(False))
asyncio.run(probe(True))
print("PICO link: transitions preserved; source gaps and unsafe endpoints rejected")
