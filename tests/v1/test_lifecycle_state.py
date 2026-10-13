"""Lifecycle projection classification and gate exit codes (single PostgreSQL authority)."""

from __future__ import annotations

import contextlib
import io
import json
import unittest

from description_pipeline.orchestration import lifecycle_state


class LifecycleStateTests(unittest.TestCase):
    def test_classify_splits_outcomes(self):
        rows = [
            {"run_id": "a", "state": "running"},
            {"run_id": "b", "state": "up_for_reschedule"},
            {"run_id": "c", "state": "unresolved"},
            {"run_id": "d", "state": "success"},
            {"run_id": "e", "state": "unknown"},
            {"run_id": "f", "state": ""},
        ]
        data = lifecycle_state.classify(rows)
        self.assertEqual([row["run_id"] for row in data["in_flight"]], ["a", "b"])
        self.assertEqual([row["run_id"] for row in data["unresolved"]], ["c"])
        self.assertEqual([row["run_id"] for row in data["unknown"]], ["e", "f"])
        self.assertFalse(data["settled"])

    def test_classify_settled_and_active_only(self):
        self.assertTrue(lifecycle_state.classify([{"run_id": "a", "state": "success"}])["settled"])
        active = lifecycle_state.classify([{"run_id": "a", "state": "running"}])
        self.assertFalse(active["settled"])
        self.assertFalse(active["unresolved"] or active["unknown"])

    def test_report_exit_codes(self):
        def run(rows, *flags):
            stream = io.StringIO()
            with contextlib.redirect_stdout(stream):
                code = lifecycle_state.main(["report", *flags], collect=lambda: rows)
            return code, json.loads(stream.getvalue())

        code, data = run([{"run_id": "a", "state": "running"}], "--require-no-unresolved")
        self.assertEqual(code, 0)
        self.assertEqual(data["in_flight"][0]["run_id"], "a")
        code, _ = run([{"run_id": "a", "state": "running"}], "--require-settled")
        self.assertEqual(code, 1)
        code, _ = run([{"run_id": "a", "state": "unresolved"}], "--require-no-unresolved")
        self.assertEqual(code, 1)
        code, _ = run([{"run_id": "a", "state": "unresolved"}], "--require-settled")
        self.assertEqual(code, 1)
        code, _ = run([{"run_id": "a", "state": "published"}], "--require-settled", "--require-no-unresolved")
        self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
