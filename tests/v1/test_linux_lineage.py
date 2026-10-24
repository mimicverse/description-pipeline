"""Linked attempts seed the parent's boundary predecessor and their own checkpoints afterwards."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from description_pipeline.io import PipelineError
from description_pipeline.platform import flow
from description_pipeline.stages import STAGE_IDS

PARENT = "9b0c5a63-8f31-43f0-9a19-1f6a22e21a11"
CHILD = "2ed90b89-de34-44a8-92e9-bc64e827aa37"

REUSE_TAG = {"parent_run": PARENT, "source_run": PARENT, "reused": True}


class _RegisteredRows:
    """The flow-facing surface of the platform store over synthetic registered rows."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.runs: dict[str, dict | None] = {}
        self.stages: dict[str, dict[str, dict]] = {}
        self.events: dict[str, list[dict]] = {}
        self.confirmed: dict[tuple[str, str], dict] = {}
        self.gate_calls: list[tuple[str, str]] = []
        self.appended: list[dict] = []

    def seal(self, run_id: str, *stages: str) -> None:
        for stage in stages:
            self.stages.setdefault(run_id, {})[stage] = {
                "state": "completed",
                "checkpoint_uri": f"store://sealed/{run_id}/{stage}",
            }

    def sealed_seed(self, owner: str, stage: str) -> Path:
        return self.root / "sealed" / owner / stage

    # ---- the PlatformStore surface the flow consumes

    def confirmed_stage(self, run_id, stage):
        return self.confirmed.get((run_id, stage))

    def run_record(self, run_id):
        return self.runs.get(run_id)

    def stage_records(self, run_id):
        return dict(self.stages.get(run_id, {}))

    def sealed_path(self, uri):
        return self.root / "sealed" / uri.removeprefix("store://sealed/")

    def run_events(self, run_id, *, after_seq=0, limit=200):
        """The store contract's bounded page: everything after ``after_seq``, capped."""
        rows = list(self.events.get(run_id, []))
        for index, row in enumerate(rows, start=1):
            if isinstance(row, dict):
                row.setdefault("seq", index)
        return [row for row in rows if isinstance(row, dict) and row.get("seq", 0) > after_seq][:limit]

    def append_event(self, run_id, *, stage, kind, payload):
        self.appended.append({"run_id": run_id, "stage": stage, "kind": kind, "payload": payload})


class LinuxLineageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.registry = _RegisteredRows(self.root)

    def run_stage(self, run_id, stage, *, source=None, subject=None, repository=None):
        """Call the product flow with the capture gates and producers as isolated boundaries."""
        seeds: list[Path] = []

        def producer(seed, output, **kwargs):
            seeds.append(Path(seed))
            return {"run_id": run_id, "state": stage, "subject_sha256": "b" * 64}

        def capture(platform, owner):
            self.registry.gate_calls.append(("capture", owner))
            return {
                "handoff_sha256": "a" * 64,
                "upload_sha256": "a" * 64,
                "native_prepared_dir": str(self.root / owner / "native"),
                "native_prepared_files": {"cad/robot.SLDASM": "a" * 64},
            }

        def revalidate(platform, owner, capture_path, frozen_path):
            self.registry.gate_calls.append(("revalidate", owner))

        def identity(platform, owner):
            self.registry.gate_calls.append(("identity", owner))
            return {}

        with (
            mock.patch.object(flow, "registered_capture", side_effect=capture),
            mock.patch.object(flow, "revalidate_capture", side_effect=revalidate),
            mock.patch.object(flow, "portable_identity_gate", side_effect=identity),
            mock.patch.object(flow, "verify_transfer", return_value={"run_id": run_id}),
            mock.patch.object(flow, "_registered_request_sha256", return_value="a" * 64),
            mock.patch.object(flow.solidworks, "generate", side_effect=producer),
            mock.patch.object(flow.solidworks, "verify", side_effect=producer),
        ):
            receipt = flow.run_stage(
                self.registry,
                self.root / "work",
                run_id,
                stage,
                source_run_id=source,
                expected_subject=subject,
            )
        return receipt, seeds

    def gate_owners(self) -> set[str]:
        return {owner for _, owner in self.registry.gate_calls}

    def test_an_ordinary_run_seeds_its_own_registered_checkpoints(self):
        self.registry.runs[CHILD] = {"parent_run_id": None, "from_stage": None}
        self.registry.seal(CHILD, "capture", "generate", "verify")
        _, seeds = self.run_stage(CHILD, "generate", subject=None)
        self.assertEqual(seeds, [self.registry.sealed_seed(CHILD, "capture")])
        _, seeds = self.run_stage(CHILD, "verify", subject="b" * 64)
        self.assertEqual(seeds, [self.registry.sealed_seed(CHILD, "generate")])
        self.assertEqual(self.gate_owners(), {CHILD})

    def test_a_portable_resume_seeds_the_boundary_from_the_parent_then_the_attempt(self):
        self.registry.runs[PARENT] = {"parent_run_id": None, "from_stage": None}
        self.registry.runs[CHILD] = {"parent_run_id": PARENT, "from_stage": "generate"}
        self.registry.seal(PARENT, "capture", "generate", "verify")
        _, seeds = self.run_stage(CHILD, "generate", source=PARENT, subject=None)
        self.assertEqual(seeds, [self.registry.sealed_seed(PARENT, "capture")])
        self.registry.seal(CHILD, "generate")
        _, seeds = self.run_stage(CHILD, "verify", source=PARENT, subject="b" * 64)
        self.assertEqual(seeds, [self.registry.sealed_seed(CHILD, "generate")])
        # The capture/input authority stays bound to the parent for the whole attempt.
        self.assertEqual(self.gate_owners(), {PARENT})

    def test_a_resume_from_verify_seeds_the_parent_generate_then_the_attempts_verify(self):
        self.registry.runs[PARENT] = {"parent_run_id": None, "from_stage": None}
        self.registry.runs[CHILD] = {"parent_run_id": PARENT, "from_stage": "verify"}
        self.registry.seal(PARENT, "capture", "generate", "verify")
        _, seeds = self.run_stage(CHILD, "verify", source=PARENT, subject="b" * 64)
        self.assertEqual(seeds, [self.registry.sealed_seed(PARENT, "generate")])
        self.assertEqual(self.gate_owners(), {PARENT})

    def test_a_native_boundary_linked_attempt_consumes_its_own_capture(self):
        for from_stage in ("freeze", "discover", "capture"):
            with self.subTest(from_stage=from_stage):
                self.registry.gate_calls.clear()
                self.registry.stages.clear()
                self.registry.runs[PARENT] = {"parent_run_id": None, "from_stage": None}
                self.registry.runs[CHILD] = {"parent_run_id": PARENT, "from_stage": from_stage}
                self.registry.seal(CHILD, "capture", "generate", "verify")
                _, seeds = self.run_stage(CHILD, "generate", subject=None)
                self.assertEqual(seeds, [self.registry.sealed_seed(CHILD, "capture")])
                _, seeds = self.run_stage(CHILD, "verify", subject="b" * 64)
                self.assertEqual(seeds, [self.registry.sealed_seed(CHILD, "generate")])
                self.assertEqual(self.gate_owners(), {CHILD})
                self.assertNotIn(PARENT, self.gate_owners())

    def test_the_parent_events_are_reused_once_at_the_boundary(self):
        self.registry.runs[PARENT] = {"parent_run_id": None, "from_stage": None}
        self.registry.runs[CHILD] = {"parent_run_id": PARENT, "from_stage": "generate"}
        self.registry.seal(PARENT, "capture", "generate", "verify")
        self.registry.events[PARENT] = [
            {"stage": stage, "kind": "stage", "payload": {"stage": stage, "marker": "original-" + stage}}
            for stage in STAGE_IDS
        ]
        self.run_stage(CHILD, "generate", source=PARENT, subject=None)
        self.assertEqual([event["stage"] for event in self.registry.appended], ["freeze", "discover", "capture"])
        self.assertTrue(all(event["payload"]["reuse"] == REUSE_TAG for event in self.registry.appended))
        self.registry.seal(CHILD, "generate")
        self.run_stage(CHILD, "verify", source=PARENT, subject="b" * 64)
        # Later stages never copy the parent's events again.
        self.assertEqual([event["stage"] for event in self.registry.appended], ["freeze", "discover", "capture"])

    def test_a_missing_attempt_checkpoint_never_falls_back_to_the_parent(self):
        self.registry.runs[PARENT] = {"parent_run_id": None, "from_stage": None}
        self.registry.runs[CHILD] = {"parent_run_id": PARENT, "from_stage": "generate"}
        self.registry.seal(PARENT, "capture", "generate", "verify")
        with self.assertRaisesRegex(PipelineError, "no registered checkpoint"):
            self.run_stage(CHILD, "verify", source=PARENT, subject="b" * 64)

    def test_an_already_confirmed_stage_revalidates_under_the_bound_capture_owner(self):
        self.registry.runs[PARENT] = {"parent_run_id": None, "from_stage": None}
        self.registry.runs[CHILD] = {"parent_run_id": PARENT, "from_stage": "generate"}
        self.registry.confirmed[(CHILD, "verify")] = {"state": "completed", "subject_sha256": "b" * 64}
        receipt, seeds = self.run_stage(CHILD, "verify", source=PARENT, subject="b" * 64)
        self.assertEqual(receipt["state"], "completed")
        self.assertEqual(seeds, [])
        self.assertEqual(self.gate_owners(), {PARENT})
        self.registry.gate_calls.clear()
        self.registry.runs[CHILD] = {"parent_run_id": PARENT, "from_stage": "capture"}
        self.registry.confirmed[(CHILD, "generate")] = {"state": "completed", "subject_sha256": "b" * 64}
        self.run_stage(CHILD, "generate", subject=None)
        self.assertEqual(self.gate_owners(), {CHILD})

    def test_the_stage_subject_checks_are_unchanged(self):
        self.registry.runs[CHILD] = {"parent_run_id": None, "from_stage": None}
        self.registry.seal(CHILD, "capture", "generate", "verify")
        with self.assertRaisesRegex(PipelineError, "does not take an expected subject"):
            self.run_stage(CHILD, "generate", subject="b" * 64)
        with self.assertRaisesRegex(PipelineError, "requires the recorded subject"):
            self.run_stage(CHILD, "verify", subject=None)
        with self.assertRaisesRegex(PipelineError, "stage must be one of"):
            self.run_stage(CHILD, "publish", subject="b" * 64, repository=self.root / "repo")


if __name__ == "__main__":
    unittest.main()
