"""Lifecycle projection consumption: bucket-only classification, admission facts, gate codes."""

from __future__ import annotations

import contextlib
import io
import json
import os
import unittest
from unittest.mock import patch

from description_pipeline.orchestration import lifecycle_state


def collected(rows, admission_state="closed"):
    return {
        "rows": rows,
        "admission": {
            "state": admission_state,
            "reason": "test",
            "actor": "test",
            "changed_at": None,
            "revision": 1,
        },
    }


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

    def test_request_rows_never_enter_the_activity_gates(self):
        rows = [
            {"object_kind": "request", "object_id": "req-1", "state": "accepted", "bucket": "terminal"},
            {"object_kind": "request", "object_id": "req-2", "state": None, "bucket": "unknown"},
        ]
        data = lifecycle_state.classify(rows)
        self.assertTrue(data["settled"], "request rows are acceptance facts, not activity")
        self.assertEqual(data["in_flight"], [])
        self.assertEqual(data["unresolved"], [])
        self.assertEqual(data["unknown"], [])
        self.assertEqual([row["object_id"] for row in data["requests"]], ["req-1", "req-2"])

    def test_unreadable_projection_refuses_the_gate(self):
        def broken():
            raise RuntimeError("connection refused")

        stream = io.StringIO()
        with contextlib.redirect_stderr(stream):
            code = lifecycle_state.main(["report", "--require-settled"], collect=broken)
        self.assertEqual(code, 1, "an unreadable authority must refuse")
        self.assertIn("projection unreadable", stream.getvalue())

    def test_missing_read_dsn_refuses(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PLATFORM_OPERATOR_DSN", None)
            with self.assertRaises(RuntimeError):
                lifecycle_state.collect_projection()

    def test_report_exit_codes(self):
        def run(rows, *flags, admission_state="closed"):
            stream = io.StringIO()
            with contextlib.redirect_stdout(stream):
                code = lifecycle_state.main(
                    ["report", *flags], collect=lambda: collected(rows, admission_state)
                )
            return code, json.loads(stream.getvalue())

        in_flight = {"object_kind": "run", "run_id": "a", "state": "running", "bucket": "in_flight"}
        unresolved = {"object_kind": "operation", "run_id": "a", "state": "unresolved", "bucket": "unresolved"}
        terminal = {"object_kind": "run", "run_id": "a", "state": "succeeded", "bucket": "terminal"}

        code, data = run([in_flight], "--require-no-unresolved")
        self.assertEqual(code, 0, "in-flight work is allowed while draining")
        self.assertEqual(data["in_flight"][0]["run_id"], "a")
        code, _ = run([in_flight], "--require-settled")
        self.assertEqual(code, 1)
        code, _ = run([unresolved], "--require-no-unresolved")
        self.assertEqual(code, 1)
        code, _ = run([unresolved], "--require-settled")
        self.assertEqual(code, 1)
        code, _ = run([terminal], "--require-settled", "--require-no-unresolved")
        self.assertEqual(code, 0)
        code, _ = run([], "--require-admission-closed")
        self.assertEqual(code, 0)
        code, data = run([], "--require-admission-closed", admission_state="open")
        self.assertEqual(code, 1)
        self.assertFalse(data["drain_requested"])

    def test_snapshot_distinguishes_acknowledged_and_drained(self):
        in_flight = {"object_kind": "stage", "run_id": "a", "state": "running", "bucket": "in_flight"}
        draining = lifecycle_state.snapshot(lambda: collected([in_flight], "closed"))
        self.assertTrue(draining["drain_requested"], "a successful close acknowledges draining")
        self.assertFalse(draining["drained"])
        drained = lifecycle_state.snapshot(lambda: collected([], "closed"))
        self.assertTrue(drained["drain_requested"])
        self.assertTrue(drained["drained"])
        open_run = lifecycle_state.snapshot(lambda: collected([], "open"))
        self.assertFalse(open_run["drain_requested"])
        self.assertFalse(open_run["drained"])


if __name__ == "__main__":
    unittest.main()
