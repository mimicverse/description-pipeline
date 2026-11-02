"""Real owned thread and reader lifetimes under asyncio and AnyIO cancellation."""

import asyncio
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from anyio import CancelScope

from description_pipeline.simulation.tasks import run_thread


class PicoTaskTests(unittest.IsolatedAsyncioTestCase):
    async def test_level_cancel_waits_for_the_real_read_then_closes_without_busy_waiting(self):
        entered, release, closed = threading.Event(), threading.Event(), threading.Event()
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "source.bin"
            path.write_bytes(b"Original read bytes")

            def archive():
                try:
                    with path.open("rb") as source:
                        entered.set()
                        if not release.wait(5):
                            raise TimeoutError("Reader test release was not delivered")
                        yield source.read(64 * 1024)
                finally:
                    closed.set()

            stream = archive()
            pulses = []

            async def cancel_and_release(scope):
                self.assertTrue(await asyncio.to_thread(entered.wait, 3))
                scope.cancel()
                for _ in range(5):
                    await asyncio.sleep(0.01)
                    pulses.append(True)
                    self.assertFalse(closed.is_set())
                release.set()

            canceller = None
            try:
                with patch("description_pipeline.simulation.tasks.asyncio.shield", wraps=asyncio.shield) as shield:
                    with CancelScope() as scope:
                        canceller = asyncio.create_task(cancel_and_release(scope))
                        try:
                            await run_thread(next, stream, None)
                        finally:
                            await run_thread(stream.close)
                    self.assertTrue(scope.cancelled_caught)
                    self.assertLess(shield.call_count, 20)
                await canceller
                self.assertEqual(5, len(pulses))
                self.assertTrue(closed.is_set())
                self.assertFalse(stream.gi_running)
            finally:
                release.set()
                if canceller is not None:
                    await canceller
                stream.close()

    async def test_actual_result_and_failure_survive_and_cancel_preserves_its_original_failure_cause(self):
        value, failure = object(), OSError("Original thread failure")
        self.assertIs(value, await run_thread(lambda: value))

        def fail():
            raise failure

        with self.assertRaises(OSError) as uncancelled:
            await run_thread(fail)
        self.assertIs(failure, uncancelled.exception)
        entered, release = threading.Event(), threading.Event()
        caught = []

        def delayed_failure():
            entered.set()
            if not release.wait(5):
                raise TimeoutError("Failure test release was not delivered")
            raise failure

        async def cancel_and_release(scope):
            self.assertTrue(await asyncio.to_thread(entered.wait, 3))
            scope.cancel()
            await asyncio.sleep(0.02)
            release.set()

        canceller = None
        try:
            with CancelScope() as scope:
                canceller = asyncio.create_task(cancel_and_release(scope))
                try:
                    await run_thread(delayed_failure)
                except asyncio.CancelledError as error:
                    caught.append(error)
                    raise
            await canceller
            self.assertTrue(scope.cancelled_caught)
            self.assertEqual(1, len(caught))
            self.assertIs(failure, caught[0].__cause__)
        finally:
            release.set()
            if canceller is not None:
                await canceller
