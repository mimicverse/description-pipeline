"""Crash fixture: keep a real startup spawn response pending for its parent."""

import asyncio
from pathlib import Path
import sys
from unittest.mock import patch

from description_pipeline.simulation.service import SimulationService


async def main():
    spawn = asyncio.create_subprocess_exec

    async def pending_response(*args, **options):
        process = await spawn(
            sys.executable,
            "-I",
            "-c",
            "import time;time.sleep(60)",
            args[-1],
            **options,
        )
        print(process.pid, flush=True)
        await asyncio.Event().wait()
        return process

    service = SimulationService(None, None, Path(sys.argv[1]), Path(sys.argv[2]))
    with patch("description_pipeline.simulation.service.asyncio.create_subprocess_exec", pending_response):
        await service.start()


if __name__ == "__main__":
    asyncio.run(main())
