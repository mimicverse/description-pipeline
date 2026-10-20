"""CLI-level state-first replay tests for native capture maintenance."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path

from description_pipeline.io import digest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "deploy" / "operator" / "reconcile-native-capture.py"
RUN_ID = "cfa3e725-abc5-5b60-94ad-6ba838110408"
OWNER_ID = "owner-operation"
EVIDENCE = "/evidence/native-job.json#sha256=" + "a" * 64


def load_script():
    spec = importlib.util.spec_from_file_location("reconcile_native_capture_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeStore:
    def __init__(self, result):
        self.result = result
        self.calls: list[dict] = []

    def completed_native_capture_recovery(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class ReconcileNativeCaptureCliTests(unittest.TestCase):
    def argv(self, root: Path) -> list[str]:
        return [
            "--run-id",
            RUN_ID,
            "--job",
            str(root / "missing-native-job.json"),
            "--derived",
            str(root / "missing-derived"),
            "--archive",
            str(root / "missing-capture-request.zip"),
            "--operation-id",
            OWNER_ID,
            "--task-id",
            "discover",
            "--map-index",
            "-1",
            "--evidence",
            EVIDENCE,
        ]

    def expected_digest(self) -> str:
        return digest(
            {
                "outcome": "failed",
                "evidence": [EVIDENCE],
                "binding": {
                    "operation_id": OWNER_ID,
                    "task_id": "discover",
                    "map_index": -1,
                },
                "reported_stage": "capture",
            }
        )

    def test_a_recorded_result_returns_without_any_input_file(self) -> None:
        module = load_script()
        recorded = {
            "operation": {
                "operation_id": OWNER_ID,
                "operation_key": f"operatorctl-reconcile:{RUN_ID}:discover:failed",
            },
            "stages": {
                "discover": {"checkpoint_uri": f"store://sealed/{RUN_ID}/discover"},
                "capture": {"state": "failed"},
            },
        }
        fake = FakeStore(recorded)
        stdout = io.StringIO()
        with (
            tempfile.TemporaryDirectory(prefix="native-capture-cli-") as directory,
            contextlib.redirect_stdout(stdout),
        ):
            result = module.main(self.argv(Path(directory)), store_factory=lambda: fake)

        self.assertEqual(result, 0)
        payload = json.loads(stdout.getvalue().removeprefix("native capture recovery already complete: "))
        self.assertEqual(payload["run_id"], RUN_ID)
        self.assertEqual(payload["operation_id"], OWNER_ID)
        self.assertEqual(payload["capture_state"], "failed")
        self.assertEqual(
            fake.calls,
            [
                {
                    "run_id": RUN_ID,
                    "operation_key": f"operatorctl-reconcile:{RUN_ID}:discover:failed",
                    "params_digest": self.expected_digest(),
                    "owner_operation_id": OWNER_ID,
                }
            ],
        )

    def test_an_unknown_operation_fails_closed_when_the_original_input_is_absent(self) -> None:
        module = load_script()
        fake = FakeStore(None)
        stderr = io.StringIO()
        with (
            tempfile.TemporaryDirectory(prefix="native-capture-cli-") as directory,
            contextlib.redirect_stderr(stderr),
        ):
            result = module.main(self.argv(Path(directory)), store_factory=lambda: fake)

        self.assertEqual(result, 1)
        self.assertIn("maintenance receipt is missing", stderr.getvalue())
        self.assertEqual(len(fake.calls), 1)

    def test_a_store_conflict_never_falls_through_to_input_rebuild(self) -> None:
        module = load_script()
        fake = FakeStore(module.PlatformStoreError("the recorded reconcile identity conflicts"))
        stderr = io.StringIO()
        with (
            tempfile.TemporaryDirectory(prefix="native-capture-cli-") as directory,
            contextlib.redirect_stderr(stderr),
        ):
            result = module.main(self.argv(Path(directory)), store_factory=lambda: fake)

        self.assertEqual(result, 1)
        self.assertIn("identity conflicts", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
