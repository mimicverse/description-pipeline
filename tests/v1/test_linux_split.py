"""The Linux half: admitted capture, staged checkpoints and the fail-closed identity gate."""

from __future__ import annotations

import json
import hashlib
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from packaging.utils import canonicalize_name

from description_pipeline.io import PipelineError, digest, file_digest, write_json
from description_pipeline.orchestration import linux_runner
from description_pipeline.orchestration.linux_store import LinuxStore
from description_pipeline.orchestration.stage_transfer import seal_capture
from description_pipeline.runtime import RUNTIME_VERSIONS, required_packages
from description_pipeline.stages import STAGE_IDS

from .test_stage_transfer import MAIN_ASSEMBLY, capture_root, native_tool

RUN = "7c9b44b3-0b9c-5ffe-9f4f-4bbdafb4a865"
from .linux_split_support import HANDOFF, SELECTED, UPLOAD, capture_job
def portable_record(native: dict, *, source_sha256: str | None = None, mujoco: str = "3.13.0") -> dict:
    record = json.loads(json.dumps(native))
    packages = {canonicalize_name(name): RUNTIME_VERSIONS[name] for name in required_packages("portable")}
    packages["mujoco"] = mujoco
    record["runtime"] = {
        **record.get("runtime", {}),
        "role": "portable",
        "system": "Linux",
        "python": "3.12.14",
        "packages": packages,
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
        source = capture_root(self.tmp, run_id=self.run_id, handoff=HANDOFF)
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
        from tests.v1.request_support import install_request_authority

        result = self.store.import_capture(
            self.run_id,
            self.archive,
            expected_handoff_sha256=HANDOFF,
            expected_main_assembly=MAIN_ASSEMBLY,
            expected_native_tool=self.tool,
        )
        install_request_authority(self, self.store, self.run_id)
        return result

    def test_capture_is_admitted_once_and_seeds_raw_native_events(self) -> None:
        self.assertIsNone(self.store.meta(self.run_id))
        meta = self.admit()
        self.assertEqual(meta["state"], "capture_admitted")
        self.assertTrue((self.store.capture_dir(self.run_id) / "input" / "cad" / MAIN_ASSEMBLY).is_file())
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

        def fake_generate(seed_dir, output, **kwargs):
            calls.update(kwargs)
            calls["seed_dir"], calls["output"] = Path(seed_dir), Path(output)
            kwargs["on_event"]({"stage": "generate", "state": "running", "at": "2026-10-10T00:30:00+00:00"})
            return {
                "state": "generated",
                "passed": False,
                "subject_sha256": "a" * 64,
                "events": [{"stage": "generate", "state": "completed", "at": "2026-10-10T00:00:00+00:00"}],
            }

        with (
            mock.patch.object(linux_runner.solidworks, "generate", side_effect=fake_generate),
            mock.patch.object(linux_runner, "tool_record", return_value=portable_record(self.tool)),
        ):
            receipt = linux_runner.run_portable_stage(self.store, self.run_id, "generate")
        self.assertEqual(receipt["state"], "generated")
        self.assertEqual(Path(calls["seed_dir"]), self.store.capture_dir(self.run_id))
        self.assertEqual(calls["output"], self.store.stage_dir(self.run_id, "generate"))
        self.assertEqual(calls["run_id"], self.run_id)
        self.assertIsNone(calls["resume"])
        self.assertEqual(calls["handoff_sha256"], HANDOFF)
        self.assertTrue(self.store.receipt_path(self.run_id, "generate").is_file())
        self.assertEqual((self.store.meta(self.run_id) or {}).get("state"), "generated")
        self.assertTrue(
            any(
                event.get("stage") == "generate" and event.get("state") == "running"
                for event in self.store.events(self.run_id)
            )
        )

    def _registered_generate_checkpoint(self) -> str:
        """A real generated-shaped checkpoint recorded through the store (reuse fixture)."""

        from description_pipeline.delivery import subject_inventory, write_delivery_manifest
        from description_pipeline.interface_records import CHECK_POLICY_SCHEMA, check_policy_digest
        from description_pipeline.runtime import tool_record
        from description_pipeline.stages import CONTRACT, CONTRACT_SHA256

        output = self.store.stage_dir(self.run_id, "generate")
        shutil.copytree(self.store.capture_dir(self.run_id), output)
        for name in ("model", "urdf", "meshes"):
            (output / name).mkdir(exist_ok=True)
        write_json(output / "tool.json", tool_record())
        write_delivery_manifest(
            output,
            contract={"schema_version": CONTRACT["schema_version"], "digest": CONTRACT_SHA256},
            policy={"schema_version": CHECK_POLICY_SCHEMA, "digest": check_policy_digest()},
        )
        subject = digest(subject_inventory(output))
        self.store.record_stage(
            self.run_id,
            "generate",
            {
                "schema_version": "solidworks-to-urdf.bundle/v1",
                "run_id": self.run_id,
                "stage": "generate",
                "state": "generated",
                "passed": False,
                "subject_sha256": subject,
                "handoff_sha256": HANDOFF,
                "events": [],
            },
        )
        return subject

    def test_a_registered_checkpoint_is_returned_without_rerunning_the_stage(self) -> None:
        self.admit()
        subject = self._registered_generate_checkpoint()
        with (
            mock.patch.object(linux_runner, "tool_record", return_value=portable_record(self.tool)),
            mock.patch.object(
                linux_runner.solidworks, "generate", side_effect=AssertionError("registered checkpoints are immutable")
            ),
        ):
            receipt = linux_runner.run_portable_stage(self.store, self.run_id, "generate")
        self.assertEqual(receipt["state"], "generated")
        self.assertEqual(receipt["subject_sha256"], subject)

    def test_a_forged_receipt_claiming_confirmation_is_refused(self) -> None:
        self.admit()
        self.store.record_stage(
            self.run_id,
            "verify",
            {
                "schema_version": "solidworks-to-urdf.bundle/v1",
                "run_id": "9b2f4c55-0c3a-4f2b-9d1e-7a6b5c4d3e2f",
                "stage": "publish",
                "state": "verified",
                "passed": True,
                "subject_sha256": "bad",
            },
        )
        with (
            mock.patch.object(linux_runner, "tool_record", return_value=portable_record(self.tool)),
            mock.patch.object(linux_runner.solidworks, "verify", side_effect=AssertionError("must not rerun")),
            self.assertRaisesRegex(PipelineError, "another run or stage"),
        ):
            linux_runner.run_portable_stage(self.store, self.run_id, "verify", expected_subject="a" * 64)

    def test_a_confirmed_receipt_without_sealed_artifacts_is_refused(self) -> None:
        self.admit()
        self.store.record_stage(
            self.run_id,
            "generate",
            {
                "schema_version": "solidworks-to-urdf.bundle/v1",
                "run_id": self.run_id,
                "stage": "generate",
                "state": "generated",
                "passed": False,
                "subject_sha256": "a" * 64,
                "handoff_sha256": HANDOFF,
            },
        )
        with (
            mock.patch.object(linux_runner.solidworks, "generate", side_effect=AssertionError("must not rerun")),
            self.assertRaisesRegex(PipelineError, "no sealed delivery"),
        ):
            linux_runner.run_portable_stage(self.store, self.run_id, "generate")

    def test_a_tampered_checkpoint_is_refused_instead_of_replayed(self) -> None:
        self.admit()
        self._registered_generate_checkpoint()
        (self.store.stage_dir(self.run_id, "generate") / "model" / "tampered.bin").write_bytes(b"x")
        with (
            mock.patch.object(linux_runner, "tool_record", return_value=portable_record(self.tool)),
            mock.patch.object(linux_runner.solidworks, "generate", side_effect=AssertionError("must not rerun")),
            self.assertRaisesRegex(PipelineError, "manifest"),
        ):
            linux_runner.run_portable_stage(self.store, self.run_id, "generate")

    def test_reuse_still_passes_the_runtime_identity_gate(self) -> None:
        self.admit()
        self._registered_generate_checkpoint()
        drifted = portable_record(self.tool, source_sha256="0" * 64)
        with (
            mock.patch.object(linux_runner, "tool_record", return_value=drifted),
            mock.patch.object(linux_runner.solidworks, "generate", side_effect=AssertionError("must not rerun")),
            self.assertRaisesRegex(PipelineError, "differs from the native capture"),
        ):
            linux_runner.run_portable_stage(self.store, self.run_id, "generate")

    def test_verify_reuse_requires_the_bound_quality_report(self) -> None:
        self.admit()
        subject = self._registered_generate_checkpoint()
        verify = self.store.stage_dir(self.run_id, "verify")
        shutil.copytree(self.store.stage_dir(self.run_id, "generate"), verify)
        write_json(verify / "reports/quality.json", {"passed": False, "subject_sha256": subject})
        self.store.record_stage(
            self.run_id,
            "verify",
            {
                "schema_version": "solidworks-to-urdf.bundle/v1",
                "run_id": self.run_id,
                "stage": "verify",
                "state": "verified",
                "passed": True,
                "subject_sha256": subject,
                "handoff_sha256": HANDOFF,
                "events": [],
            },
        )
        with (
            mock.patch.object(linux_runner, "tool_record", return_value=portable_record(self.tool)),
            mock.patch.object(linux_runner.solidworks, "verify", side_effect=AssertionError("must not rerun")),
            self.assertRaises(PipelineError),
        ):
            linux_runner.run_portable_stage(self.store, self.run_id, "verify", expected_subject=subject)

    def test_preview_and_artifacts_are_bound_to_the_verified_bytes(self) -> None:
        from description_pipeline.delivery import subject_digest
        from description_pipeline.orchestration import linux_store as store_module

        self.admit()
        verify = self.store.stage_dir(self.run_id, "verify")
        shutil.copytree(self.store.capture_dir(self.run_id), verify)
        for name in ("model", "meshes", "urdf"):
            (verify / name).mkdir(exist_ok=True)
        (verify / "reports/tool.json").write_text("{}\n", encoding="utf-8")
        (verify / "tool.json").write_text("{}\n", encoding="utf-8")
        (verify / "README.md").write_text("fixture\n", encoding="utf-8")
        (verify / "urdf/robot.urdf").write_text("<robot/>\n", encoding="utf-8")
        subject = subject_digest(verify)
        report = {"passed": True, "subject_sha256": subject}
        calls = {"bundle": 0}

        def counting_check(bundle):
            calls["bundle"] += 1
            return report

        with (
            mock.patch.object(store_module, "check_bundle", side_effect=counting_check),
            mock.patch.object(store_module, "rebuilt_definition", return_value={}),
            mock.patch.object(store_module, "require_qualified_report", return_value=report),
        ):
            preview = self.store.preview(self.run_id)
            self.assertEqual(preview["subject_sha256"], subject)
            self.assertIs(self.store.preview(self.run_id), preview)
            digest = preview["files"]["urdf/robot.urdf"]
            stream, size = self.store.open_artifact(self.run_id, "urdf/robot.urdf", sha256=digest)
            with stream:
                self.assertEqual(stream.read(), b"<robot/>\n")
                self.assertEqual(stream.read(), b"")
            self.assertEqual(size, len(b"<robot/>\n"))
            second, _second_size = self.store.open_artifact(self.run_id, "urdf/robot.urdf", sha256=digest)
            with second:
                self.assertEqual(second.read(), b"<robot/>\n")
            with self.assertRaises(PipelineError):
                self.store.open_artifact(self.run_id, "urdf/robot.urdf", sha256="c" * 64)
            # The preview is verified once for immutable checkpoint serving.
            self.assertEqual(calls["bundle"], 1)
            # An in-place member edit keeps the verified reports untouched; the digest-bound
            # stream still refuses the changed bytes at EOF.
            (verify / "urdf/robot.urdf").write_text("<roboX/>\n", encoding="utf-8")
            tampered, _tampered_size = self.store.open_artifact(self.run_id, "urdf/robot.urdf", sha256=digest)
            with tampered:
                self.assertEqual(tampered.read(), b"<roboX/>\n")
                with self.assertRaises(PipelineError):
                    tampered.read()
        # A report member change forces re-verification of the checkpoint.
        (verify / "reports/quality.json").write_text('{"passed": true}\n', encoding="utf-8")
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
                mock.patch.object(linux_runner.solidworks, "generate", side_effect=AssertionError("must not run")),
                self.assertRaises(PipelineError),
            ):
                linux_runner.run_portable_stage(self.store, self.run_id, "generate")
            with (
                mock.patch.object(linux_runner, "tool_record", return_value=drifted),
                mock.patch.object(linux_runner.solidworks, "verify", side_effect=AssertionError("must not run")),
                self.assertRaises(PipelineError),
            ):
                linux_runner.run_portable_stage(self.store, self.run_id, "verify", expected_subject="a" * 64)
        with (
            mock.patch.object(linux_runner.solidworks, "generate", side_effect=AssertionError("must not run")),
            self.assertRaises(PipelineError),
        ):
            linux_runner.run_portable_stage(self.store, "0e4f3f74-0e7e-4c31-a1ad-6e5a4a1f0000", "generate")

    def test_capture_is_revalidated_before_generate(self) -> None:
        self.admit()
        target = self.store.capture_dir(self.run_id) / "input" / "cad" / MAIN_ASSEMBLY
        target.write_bytes(target.read_bytes() + b"tampered")
        with (
            mock.patch.object(linux_runner, "tool_record", return_value=portable_record(self.tool)),
            mock.patch.object(linux_runner.solidworks, "generate", side_effect=AssertionError("must not run")),
            self.assertRaises(PipelineError),
        ):
            linux_runner.run_portable_stage(self.store, self.run_id, "generate")
        receipt = json.loads(self.store.receipt_path(self.run_id, "generate").read_text())
        self.assertEqual(receipt["state"], "failed")
        self.assertEqual((self.store.meta(self.run_id) or {}).get("state"), "failed")

    def test_driver_failure_leaves_a_failed_receipt(self) -> None:
        self.admit()

        def explode(*args, **kwargs):
            kwargs["on_event"]({"stage": "generate", "state": "running", "at": "2026-10-10T04:00:00+00:00"})
            raise RuntimeError("driver exploded")

        with (
            mock.patch.object(linux_runner, "tool_record", return_value=portable_record(self.tool)),
            mock.patch.object(linux_runner.solidworks, "generate", side_effect=explode),
            self.assertRaises(RuntimeError),
        ):
            linux_runner.run_portable_stage(self.store, self.run_id, "generate")
        receipt = json.loads(self.store.receipt_path(self.run_id, "generate").read_text())
        self.assertEqual(receipt["state"], "failed")
        self.assertIn("driver exploded", receipt["error"])
        self.assertEqual((self.store.meta(self.run_id) or {}).get("state"), "failed")
        job = self.store.merged_job(self.run_id)
        self.assertEqual(job["status"], "failed")
        self.assertIn("driver exploded", job.get("error") or "")
        self.assertTrue(
            any(event.get("state") == "running" for event in self.store.events(self.run_id)),
            self.store.events(self.run_id),
        )

    def test_fetch_short_circuits_a_committed_admission(self) -> None:
        self.admit()
        endpoint = mock.Mock()
        endpoint.stream_capture_archive.side_effect = AssertionError("must not re-download")
        job = capture_job(self.run_id, self.archive, self.tool)
        binding = linux_runner.import_native_capture(self.store, endpoint, self.run_id, job)
        self.assertEqual(binding["state"], "capture_admitted")
        self.assertEqual(binding["capture_dir"], str(self.store.capture_dir(self.run_id)))
        self.assertEqual(self.store.meta(self.run_id)["selection"], job["selection"]["manifest"])
        endpoint.stream_capture_archive.assert_not_called()

    def test_verify_and_publish_seed_from_the_previous_checkpoints(self) -> None:
        self.admit()
        stages: list[tuple] = []

        def fake_generate(seed_dir, output, **kwargs):
            stages.append(("generate", Path(seed_dir), Path(output), None))
            Path(output).mkdir(parents=True, exist_ok=True)
            return {
                "state": "generated",
                "passed": False,
                "subject_sha256": "a" * 64,
                "events": [
                    {"stage": "generate", "state": "completed", "at": "2026-10-10T01:00:00+00:00"}
                ],
            }

        def fake_verify(seed_dir, output, **kwargs):
            stages.append(("verify", Path(seed_dir), Path(output), None))
            Path(output).mkdir(parents=True, exist_ok=True)
            return {
                "state": "verified",
                "passed": True,
                "subject_sha256": "a" * 64,
                "events": [{"stage": "verify", "state": "completed", "at": "2026-10-10T02:00:00+00:00"}],
            }

        def fake_publish(seed_dir, output, **kwargs):
            stages.append(("publish", Path(seed_dir), Path(output), kwargs.get("repository")))
            Path(output).mkdir(parents=True, exist_ok=True)
            return {
                "state": "published",
                "passed": True,
                "subject_sha256": "a" * 64,
                "events": [{"stage": "publish", "state": "completed", "at": "2026-10-10T03:00:00+00:00"}],
            }

        with (
            mock.patch.object(linux_runner.solidworks, "generate", side_effect=fake_generate),
            mock.patch.object(linux_runner.solidworks, "verify", side_effect=fake_verify),
            mock.patch.object(linux_runner.solidworks, "publish", side_effect=fake_publish),
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
