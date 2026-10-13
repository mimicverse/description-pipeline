"""Lifecycle projection classification and gate exit codes (fail-closed platform vocabulary)."""

from __future__ import annotations

import contextlib
import io
import json
import unittest
from types import SimpleNamespace

from description_pipeline.orchestration import lifecycle_state


class LifecycleStateTests(unittest.TestCase):
    def test_classify_settles_only_recognized_terminal_states(self):
        for state in ("succeeded", "success", "published", "completed", "failed", "cancelled"):
            with self.subTest(state=state):
                self.assertTrue(lifecycle_state.classify([{"run_id": "r", "state": state}])["settled"])
        for state in ("running", "queued", "pending", "publishing", "cancel_requested"):
            with self.subTest(state=state):
                data = lifecycle_state.classify([{"run_id": "r", "state": state}])
                self.assertFalse(data["settled"])
                self.assertEqual([row["run_id"] for row in data["in_flight"]], ["r"])
        data = lifecycle_state.classify([{"run_id": "r", "state": "unresolved"}])
        self.assertFalse(data["settled"])
        self.assertEqual([row["run_id"] for row in data["unresolved"]], ["r"])
        for state in ("unexpected", "unknown", ""):
            with self.subTest(state=state):
                data = lifecycle_state.classify([{"run_id": "r", "state": state}])
                self.assertFalse(data["settled"])
                self.assertEqual([row["run_id"] for row in data["unknown"]], ["r"])

    def test_observe_keeps_null_states_visible_without_filters(self):
        observed_filters = []

        class Query:
            def __init__(self, model):
                self.model = model
                self.members = [SimpleNamespace(run_id="run-1", task_id="verify", state=None)]

            def filter(self, expression):
                observed_filters.append(self.model.__name__)
                return self

            def all(self):
                return self.members

        class Session:
            def query(self, model):
                return Query(model)

        collected = lifecycle_state.observe(
            Session(),
            type("DagRunModel", (), {}),
            type("TaskInstanceModel", (), {}),
        )
        self.assertEqual(observed_filters, [], "the observation must not pre-filter by state")
        data = lifecycle_state.classify(collected)
        self.assertFalse(data["settled"], "a NULL scheduler state must block, not settle")
        self.assertTrue(data["unknown"])

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
