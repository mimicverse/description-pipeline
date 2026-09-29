"""The COM executor owns one worker thread and never kills SolidWorks."""

import threading
import unittest

from tools.solidworks_export.backends import FakeBackend
from tools.solidworks_export.com_executor import ComExecutor
from tools.solidworks_export.errors import BridgeError


class ComExecutorTests(unittest.TestCase):
    def setUp(self):
        self.executor = ComExecutor(FakeBackend())
        self.addCleanup(self.executor.close)

    def test_work_runs_on_the_executor_thread(self):
        seen = {}

        def work():
            seen["thread"] = threading.current_thread().name
            return 42

        self.assertEqual(self.executor.run(work), 42)
        self.assertEqual(seen["thread"], "swbridge-com-sta")

    def test_exceptions_propagate_to_the_caller(self):
        def work():
            raise BridgeError("cad_export_failed", "boom", exit_code=2)

        with self.assertRaises(BridgeError) as caught:
            self.executor.run(work)
        self.assertEqual(caught.exception.code, "cad_export_failed")

    def test_timeout_reports_a_modal_dialog_hint(self):
        release = threading.Event()

        def work():
            release.wait(5)

        self.addCleanup(release.set)
        with self.assertRaises(BridgeError) as caught:
            self.executor.run(work, timeout=0.2)
        self.assertEqual(caught.exception.code, "modal_dialog_blocked")
        self.assertEqual(caught.exception.http_status, 504)
        self.assertEqual(caught.exception.exit_code, 4)

    def test_close_stops_the_worker(self):
        self.executor.close()
        self.assertFalse(self.executor._thread.is_alive())

    def test_call_from_the_executor_thread_runs_inline(self):
        """A nested call must not deadlock waiting on its own queue."""
        result = {}

        def outer():
            result["inner"] = self.executor.run(lambda: "inline")

        self.executor.run(outer)
        self.assertEqual(result["inner"], "inline")


if __name__ == "__main__":
    unittest.main()
