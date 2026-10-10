"""The Linux half: admitted capture, staged checkpoints and the fail-closed identity gate."""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from description_pipeline.io import PipelineError
from description_pipeline.orchestration import linux_runner
from description_pipeline.orchestration.linux_store import LinuxStore
from description_pipeline.orchestration.stage_transfer import seal_capture
from description_pipeline.stages import STAGE_IDS

from .test_stage_transfer import HANDOFF, MAIN_ASSEMBLY, capture_root, native_tool

RUN = "7c9b44b3-0b9c-5ffe-9f4f-4bbdafb4a865"


def portable_record(native: dict, *, source_sha256: str | None = None, mujoco: str = "3.13.0") -> dict:
    record = json.loads(json.dumps(native))
    record["runtime"] = {
        **record.get("runtime", {}),
        "role": "portable",
        "system": "Linux",
        "packages": {"mujoco": mujoco},
    }
    if source_sha256 is not None:
        record["source_sha256"] = source_sha256
    return record


class LinuxSplitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="linux-split-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.store = LinuxStore(self.tmp / "store")
        self.tool = native_tool()
        self.run_id = RUN
        source = capture_root(self.tmp, run_id=self.run_id)
        self.archive = self.tmp / "native-evidence.zip"
        seal_capture(
            source,
            self.archive,
            run_id=self.run_id,
            handoff_sha256=HANDOFF,
            main_assembly=MAIN_ASSEMBLY,
            native_tool=self.tool,
        )

    def admit(self) -> dict:
        return self.store.import_capture(
            self.run_id,
            self.archive,
            expected_handoff_sha256=HANDOFF,
            expected_main_assembly=MAIN_ASSEMBLY,
            expected_native_tool=self.tool,
        )

    def test_capture_is_admitted_once_and_seeds_raw_native_events(self) -> None:
        self.assertIsNone(self.store.meta(self.run_id))
        meta = self.admit()
        self.assertEqual(meta["state"], "capture_admitted")
        self.assertTrue((self.store.capture_dir(self.run_id) / "input/robot.yaml").is_file())
        native_receipt = json.loads((self.store.capture_dir(self.run_id) / "reports/native-stages.json").read_text())
        self.assertEqual(self.store.events(self.run_id), native_receipt.get("events") or [])
        self.assertIsNone(self.store.delivery_dir(self.run_id))
        view = self.store.view(self.run_id)
        self.assertEqual([stage["id"] for stage in view["stages"]], list(STAGE_IDS))
        seeded = self.store.events(self.run_id)
        at = "2026-10-10T03:00:00+00:00"
        first = {"stage": "generate", "state": "running", "at": at, "check": {"id": "one"}}
        second = {"stage": "generate", "state": "running", "at": at, "check": {"id": "two"}}
        self.store.append_events(self.run_id, [first, second, first])
        self.assertEqual(self.store.events(self.run_id), [*seeded, first, second])
        again = self.admit()
        self.assertEqual(again["state"], "capture_admitted")
        self.assertEqual(self.store.events(self.run_id), [*seeded, first, second])

    def test_crash_window_between_install_and_meta_is_recovered(self) -> None:
        self.admit()
        meta_path = self.store.meta_path(self.run_id)
        provisional = json.loads(meta_path.read_text())
        provisional["state"] = "importing"
        provisional.pop("transfer", None)
        meta_path.write_text(json.dumps(provisional))
        recovered = self.admit()
        self.assertEqual(recovered["state"], "capture_admitted")
        meta_path.unlink()
        with self.assertRaises(PipelineError):
            self.admit()

    def test_admission_fails_closed_on_identity_mismatch(self) -> None:
        with self.assertRaises(PipelineError):
            self.store.import_capture(
                self.run_id,
                self.archive,
                expected_handoff_sha256="c" * 64,
                expected_main_assembly=MAIN_ASSEMBLY,
                expected_native_tool=self.tool,
            )
        with self.assertRaises(PipelineError):
            self.store.import_capture(
                self.run_id,
                self.archive,
                expected_handoff_sha256=HANDOFF,
                expected_main_assembly=MAIN_ASSEMBLY,
                expected_native_tool={**self.tool, "source_sha256": "0" * 64},
            )

    def test_generate_runs_bounded_and_records_the_checkpoint(self) -> None:
        self.admit()
        calls: dict = {}

        def fake_run(package, output, **kwargs):
            calls.update(kwargs)
            calls["package"], calls["output"] = Path(package), Path(output)
            kwargs["on_event"]({"stage": "generate", "state": "running", "at": "2026-10-10T00:30:00+00:00"})
            return {
                "state": "generated",
                "passed": False,
                "subject_sha256": "a" * 64,
                "events": [{"stage": "generate", "state": "completed", "at": "2026-10-10T00:00:00+00:00"}],
            }

        with (
            mock.patch.object(linux_runner.solidworks, "run", side_effect=fake_run),
            mock.patch.object(linux_runner, "tool_record", return_value=portable_record(self.tool)),
        ):
            receipt = linux_runner.run_portable_stage(self.store, self.run_id, "generate")
        self.assertEqual(receipt["state"], "generated")
        self.assertEqual(calls["resume_from"], "generate")
        self.assertEqual(calls["stop_after"], "generate")
        self.assertIsNone(calls["repository"])
        self.assertEqual(Path(calls["seed_dir"]), self.store.capture_dir(self.run_id))
        self.assertEqual(calls["output"], self.store.stage_dir(self.run_id, "generate"))
        self.assertEqual(calls["package"], self.store.capture_dir(self.run_id))
        self.assertEqual(calls["handoff_sha256"], HANDOFF)
        self.assertTrue(self.store.receipt_path(self.run_id, "generate").is_file())
        self.assertEqual((self.store.meta(self.run_id) or {}).get("state"), "generated")
        self.assertTrue(
            any(
                event.get("stage") == "generate" and event.get("state") == "running"
                for event in self.store.events(self.run_id)
            )
        )

    def test_preview_and_artifacts_are_bound_to_the_verified_bytes(self) -> None:
        from description_pipeline.delivery import subject_digest
        from description_pipeline.orchestration import linux_store as store_module

        self.admit()
        verify = self.store.stage_dir(self.run_id, "verify")
        shutil.copytree(self.store.capture_dir(self.run_id), verify)
        for name in ("model", "meshes", "urdf"):
            (verify / name).mkdir(exist_ok=True)
        (verify / "reports/tool.json").write_text("{}\n", encoding="utf-8")
        (verify / "README.md").write_text("fixture\n", encoding="utf-8")
        (verify / "urdf/robot.urdf").write_text("<robot/>\n", encoding="utf-8")
        subject = subject_digest(verify)
        report = {"passed": True, "subject_sha256": subject}
        with (
            mock.patch.object(store_module, "check_bundle", return_value=report),
            mock.patch.object(store_module, "require_qualified_report", return_value=report),
        ):
            preview = self.store.preview(self.run_id)
            self.assertEqual(preview["subject_sha256"], subject)
            digest = preview["files"]["urdf/robot.urdf"]
            stream, size = self.store.open_artifact(self.run_id, "urdf/robot.urdf", sha256=digest)
            with stream:
                self.assertEqual(stream.read(), b"<robot/>\n")
            self.assertEqual(size, len(b"<robot/>\n"))
            with self.assertRaises(PipelineError):
                self.store.open_artifact(self.run_id, "urdf/robot.urdf", sha256="c" * 64)
        with (
            mock.patch.object(store_module, "check_bundle", side_effect=PipelineError("report bytes changed")),
            self.assertRaises(PipelineError),
        ):
            self.store.preview(self.run_id)

    def test_stage_guards_and_identity_gate_fail_closed(self) -> None:
        self.admit()
        with mock.patch.object(linux_runner, "tool_record", return_value=portable_record(self.tool)):
            with self.assertRaises(PipelineError):
                linux_runner.run_portable_stage(self.store, self.run_id, "generate", expected_subject="a" * 64)
            with self.assertRaises(PipelineError):
                linux_runner.run_portable_stage(self.store, self.run_id, "verify")
            with self.assertRaises(PipelineError):
                linux_runner.run_portable_stage(self.store, self.run_id, "publish", expected_subject="a" * 64)
            drifted = portable_record(self.tool, source_sha256="0" * 64)
            with (
                mock.patch.object(linux_runner, "tool_record", return_value=drifted),
                mock.patch.object(linux_runner.solidworks, "run", side_effect=AssertionError("must not run")),
                self.assertRaises(PipelineError),
            ):
                linux_runner.run_portable_stage(self.store, self.run_id, "generate")
        with (
            mock.patch.object(linux_runner.solidworks, "run", side_effect=AssertionError("must not run")),
            self.assertRaises(PipelineError),
        ):
            linux_runner.run_portable_stage(self.store, "0e4f3f74-0e7e-4c31-a1ad-6e5a4a1f0000", "generate")

    def test_verify_and_publish_seed_from_the_previous_checkpoints(self) -> None:
        self.admit()
        stages: list[tuple] = []

        def fake_run(package, output, **kwargs):
            stages.append((kwargs["resume_from"], Path(kwargs["seed_dir"]), Path(output), kwargs.get("repository")))
            return {
                "state": {"generate": "generated", "verify": "verified", "publish": "published"}[kwargs["resume_from"]],
                "passed": kwargs["resume_from"] != "generate",
                "subject_sha256": "a" * 64,
                "events": [
                    {
                        "stage": kwargs["resume_from"],
                        "state": "completed",
                        "at": f"2026-10-10T0{len(stages)}:00:00+00:00",
                    }
                ],
            }

        with (
            mock.patch.object(linux_runner.solidworks, "run", side_effect=fake_run),
            mock.patch.object(linux_runner, "tool_record", return_value=portable_record(self.tool)),
        ):
            linux_runner.run_portable_stage(self.store, self.run_id, "generate")
            linux_runner.run_portable_stage(self.store, self.run_id, "verify", expected_subject="a" * 64)
            linux_runner.run_portable_stage(
                self.store, self.run_id, "publish", expected_subject="a" * 64, repository=self.tmp / "repo"
            )
        self.assertEqual(stages[1][1], self.store.stage_dir(self.run_id, "generate"))
        self.assertEqual(stages[2][1], self.store.stage_dir(self.run_id, "verify"))
        self.assertEqual(stages[2][2], self.store.output_dir(self.run_id))
        self.assertEqual(stages[2][3], self.tmp / "repo")
        self.assertEqual((self.store.meta(self.run_id) or {}).get("state"), "published")
        self.assertGreaterEqual(len(self.store.events(self.run_id)), 3)
