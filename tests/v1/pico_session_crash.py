"""Run a real isolated supervisor until the parent test kills its process."""

import asyncio
import json
from pathlib import Path
import sys

import psycopg2

from description_pipeline.artifacts import ArtifactStore
from description_pipeline.pipeline.contracts import ArtifactRef
from description_pipeline.simulation.service import SimulationService
from description_pipeline.simulation.store import SimulationStore


async def main():
    config = json.load(sys.stdin)
    artifacts = ArtifactStore(Path(config["artifacts"]))
    store = SimulationStore(lambda: psycopg2.connect(config["dsn"]))
    service = SimulationService(store, artifacts, Path(config["root"]), Path(config["python"]))
    await service.start()

    def read_member(name, member):
        return artifacts.read_metadata_member(
            ArtifactRef.from_record(member["artifact"]), name, max_bytes=64 * 1024 * 1024
        )[0]

    request = {**config["request"], "record": False, "runtime": service.runtime_identity}
    row = await service.create(
        "app:tenant:operator", "crashed-supervisor", config["run_id"], request, config["members"], read_member
    )
    sid = row["session_id"]
    async with asyncio.timeout(15):
        while service.running[sid].model is None:
            await asyncio.sleep(0.02)
    body = {"type": "estop", "command_id": "before-crash-estop"}
    store.authorize_command(sid, body["command_id"], "app:tenant:operator", body)
    await service.send(sid, {"type": "estop"}, command_id=body["command_id"])
    print(json.dumps({"session_id": sid, "identity": service.running[sid].identity}), flush=True)
    await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
