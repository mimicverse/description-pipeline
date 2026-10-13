"""Lifecycle projection classification and gate exit codes (fail-closed view-owned buckets)."""

from __future__ import annotations

import contextlib
import io
import json
import unittest
from types import SimpleNamespace

from description_pipeline.orchestration import lifecycle_state


class LifecycleStateTests(unittest.TestCase):
    def test_classify_consumes_only_the_view_owned_bucket(self):
        settled = lifecycle_state.classify([{"run_id": "r", "state": "running", "bucket": "terminal"}])
        self.assertTrue(settled["settled"])
        in_flight = lifecycle_state.classify([{"run_id": "r", "state": "publishing", "bucket": "in_flight"}])
        self.assertFalse(in_flight["settled"])
        self.assertEqual([row["run_id"] for row in in_flight["in_flight"]], ["r"])
        unresolved = lifecycle_state.classify([{"run_id": "r", "state": "unresolved", "bucket": "unresolved"}])
        self.assertFalse(unresolved["settled"])
        self.assertEqual([row["run_id"] for row in unresolved["unresolved"]], ["r"])
        for row in (
            {"run_id": "r", "state": "unexpected", "bucket": "unknown"},
            {"run_id": "r", "state": "running", "bucket": "unrecognized"},
            {"run_id": "r", "state": "success"},
            {"run_id": "r", "state": None, "bucket": None},
            {"run_id": "r"},
        ):
            with self.subTest(row=row):
                data = lifecycle_state.classify([row])
                self.assertFalse(data["settled"], "a row without a recognized bucket must never settle")
                self.assertEqual([entry["run_id"] for entry in data["unknown"]], ["r"])

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

    def test_bucket_rows_are_consumed_verbatim_and_unknown_buckets_fail_closed(self):
        rows = [
            {"run_id": "a", "state": "running", "bucket": "terminal"},
            {"run_id": "b", "state": "publishing", "bucket": "in_flight"},
            {"run_id": "c", "state": "unresolved", "bucket": "unresolved"},
            {"run_id": "d", "state": None, "bucket": "unknown"},
            {"run_id": "e", "state": "running", "bucket": "unrecognized"},
        ]
        data = lifecycle_state.classify(rows)
        self.assertEqual([row["run_id"] for row in data["in_flight"]], ["b"])
        self.assertEqual([row["run_id"] for row in data["unresolved"]], ["c"])
        self.assertEqual([row["run_id"] for row in data["unknown"]], ["d", "e"])
        self.assertFalse(data["settled"])
        self.assertTrue(
            lifecycle_state.classify([{"run_id": "a", "state": "running", "bucket": "terminal"}])["settled"]
        )

    def test_projection_seam_is_the_default_collector(self):
        self.assertTrue(callable(lifecycle_state.collect_projection))
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            code = lifecycle_state.main(["report"], collect=lambda: [{"run_id": "a", "state": "success"}])
        self.assertEqual(code, 0)

    def test_report_exit_codes(self):
        def run(rows, *flags):
            stream = io.StringIO()
            with contextlib.redirect_stdout(stream):
                code = lifecycle_state.main(["report", *flags], collect=lambda: rows)
            return code, json.loads(stream.getvalue())

        code, data = run([{"run_id": "a", "state": "running", "bucket": "in_flight"}], "--require-no-unresolved")
        self.assertEqual(code, 0)
        self.assertEqual(data["in_flight"][0]["run_id"], "a")
        code, _ = run([{"run_id": "a", "state": "running", "bucket": "in_flight"}], "--require-settled")
        self.assertEqual(code, 1)
        code, _ = run([{"run_id": "a", "state": "unresolved", "bucket": "unresolved"}], "--require-no-unresolved")
        self.assertEqual(code, 1)
        code, _ = run([{"run_id": "a", "state": "unresolved", "bucket": "unresolved"}], "--require-settled")
        self.assertEqual(code, 1)
        code, _ = run(
            [{"run_id": "a", "state": "completed", "bucket": "terminal"}],
            "--require-settled",
            "--require-no-unresolved",
        )
        self.assertEqual(code, 0)
        code, _ = run([{"run_id": "a", "state": "success"}], "--require-settled")
        self.assertEqual(code, 1, "scheduler rows without the view's bucket are unknown and must block")


if __name__ == "__main__":
    unittest.main()
