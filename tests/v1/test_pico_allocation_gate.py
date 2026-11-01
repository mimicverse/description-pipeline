"""Real cross-process allocation exclusion; no native or business qualification."""

import asyncio
from pathlib import Path
import sys
import tempfile
import unittest

from description_pipeline.simulation.resources import StorageGate


class PicoAllocationGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_separate_process_owns_the_same_root_lock_and_cancelled_waiters_release_only_their_handle(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            child = await asyncio.create_subprocess_exec(
                sys.executable,
                "-I",
                "-B",
                "-c",
                "import fcntl,sys; f=open(sys.argv[1],'a+'); fcntl.flock(f,fcntl.LOCK_EX); "
                "print('locked',flush=True); sys.stdin.readline(); f.close()",
                str(root / "allocation.lock"),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            entered = asyncio.Event()
            gate = StorageGate(root)

            async def allocate():
                async with gate:
                    entered.set()
                    (root / "allocated.bin").write_bytes(b"Actual allocation")

            task = None
            try:
                self.assertEqual(b"locked\n", await asyncio.wait_for(child.stdout.readline(), 5))
                task = asyncio.create_task(allocate())
                await asyncio.sleep(0.05)
                self.assertFalse(entered.is_set())
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertFalse(gate.locked())
                self.assertFalse((root / "allocated.bin").exists())
                task = asyncio.create_task(allocate())
                await asyncio.sleep(0.05)
                self.assertFalse(entered.is_set())
                _stdout, stderr = await asyncio.wait_for(child.communicate(b"release\n"), 5)
                self.assertEqual(0, child.returncode)
                self.assertEqual(b"", stderr)
                await asyncio.wait_for(task, 5)
                self.assertTrue(entered.is_set())
                self.assertEqual(b"Actual allocation", (root / "allocated.bin").read_bytes())
            finally:
                if child.returncode is None:
                    child.kill()
                    await child.wait()
                if task is not None and not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
