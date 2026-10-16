"""The portable flow: registered seals, fail-closed gates and stage seeding.

Wave-2 port of the legacy Linux-runner suite onto ``platform/flow`` +
``platform/capture`` + ``PlatformStore`` (the single current workflow).  The
case names and assertion intents are preserved; only the call surface moved.
The artifact-read case (preview/open_artifact byte streaming) has no subject
on the platform read face yet and is tracked as a reported gap, not silently
dropped.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from description_pipeline.io import PipelineError, file_digest, write_json
from description_pipeline.orchestration.stage_transfer import seal_capture
from description_pipeline.platform import capture as platform_capture
from description_pipeline.platform import flow
from description_pipeline.platform import storage as platform_storage
from description_pipeline.platform.store import PlatformStore

from .linux_split_support import (
    HANDOFF,
    MAIN_ASSEMBLY,
    FakeEndpoint,
    SplitAdmissionMixin,
    capture_job,
    portable_record,
)
from .test_platform_store import FakeWorld
from .test_stage_transfer import capture_root, native_tool

RUN = "7c9b44b3-0b9c-5ffe-9f4f-4bbdafb4a865"


class LinuxSplitTests(SplitAdmissionMixin, unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="linux-split-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.store_root = self.tmp / "store"
        self.world = FakeWorld()
        self.store = PlatformStore(self.world.connect, sealed=platform_storage.sealed_resolver(self.store_root))
        self.store.admit_run(
            request_id="q-split",
            run_id=RUN,
            principal="portal",
            target_hardware="nd",
            main_assembly=MAIN_ASSEMBLY,
            dispatch={"stage": "capture", "operation_key": f"{RUN}/capture:0", "params_digest": "a" * 64},
        )
        self.tool = native_tool()
        source = capture_root(self.tmp, run_id=RUN, handoff=HANDOFF)
        self.archive = self.tmp / "native-evidence.zip"
        seal_capture(
            source,
            self.archive,
            run_id=RUN,
            handoff_sha256=HANDOFF,
            main_assembly=MAIN_ASSEMBLY,
            native_tool=self.tool,
        )

    # ------------------------------------------------------------------ fixture
    def _portable(self, **overrides):
        return portable_record(self.tool, **overrides)

    def _record_generate(self, run_id: str = RUN) -> str:
        """A real generated-shaped registered checkpoint (reuse fixture)."""
        paths = self._paths(run_id)
        output = self.tmp / f"fixture-generate-{run_id}"
        shutil.copytree(paths.capture_dir, output)
        self._subject_layout(output)
        write_json(output / "model" / "robot.json", {"model": "fixture"})
        (output / "urdf" / "robot.urdf").write_text("<robot/>\n", encoding="utf-8")
        (output / "meshes" / "base.stl").write_bytes(b"STL-BYTES")
        completed = self._register_stage(run_id, "generate", output, state="generated")
        return completed["subject_sha256"]

    def _work_json(self) -> list[dict]:
        payloads = []
        for path in sorted(self._paths(RUN).run_dir.rglob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except ValueError:
                continue
            if isinstance(payload, dict):
                payloads.append(payload)
        return payloads

    # ------------------------------------------------------------------ cases
    def test_capture_is_admitted_once_and_seeds_raw_native_events(self) -> None:
        self.admit(RUN)
        paths = self._paths(RUN)
        self.assertTrue((paths.capture_dir / "input" / "cad" / MAIN_ASSEMBLY).is_file())
        seeded = [event for event in self.world.events if event[0] == RUN]
        self.assertTrue(seeded, "the sealed native events must mirror into the platform stream")
        job = capture_job(RUN, self.archive, self.tool)
        endpoint = FakeEndpoint(self.archive.read_bytes())
        again = platform_capture.import_native_capture(self.store, paths, RUN, job, endpoint)
        self.assertEqual(again["state"], "capture_admitted")
        self.assertEqual(endpoint.calls, 0, "a committed admission is never re-downloaded")
        self.assertEqual([event for event in self.world.events if event[0] == RUN], seeded)
        self.assertEqual(self.store.stage_records(RUN)["capture"]["state"], "completed")

    def test_crash_window_between_install_and_meta_is_recovered(self) -> None:
        self.admit(RUN)
        paths = self._paths(RUN)
        carried = paths.capture_dir / "input" / "request.json"
        carried.unlink()
        job = capture_job(RUN, self.archive, self.tool)
        endpoint = FakeEndpoint(self.archive.read_bytes())
        recovered = platform_capture.import_native_capture(self.store, paths, RUN, job, endpoint)
        self.assertEqual(recovered["state"], "capture_admitted")
        self.assertEqual(endpoint.calls, 0)
        self.assertTrue(carried.is_file(), "the frozen request is recovered from its sealed source")
        # Without the sealed source and without the carried copy the recovery refuses:
        # deleting the registered original breaks the sealed freeze delivery itself, so
        # the registration read refuses before any weaker fallback could engage.
        carried.unlink()
        freeze_uri = self.store.stage_records(RUN)["freeze"]["checkpoint_uri"]
        (self.store.sealed_path(freeze_uri) / "input" / "request.json").unlink()
        with self.assertRaisesRegex(PipelineError, "manifest"):
            platform_capture.import_native_capture(self.store, paths, RUN, job, endpoint)
        self.assertEqual(endpoint.calls, 0, "the refusal never falls back to a re-download")

    def test_admission_fails_closed_on_identity_mismatch(self) -> None:
        self._register_freeze(RUN)
        paths = self._paths(RUN)
        mutations = (
            lambda job: job["result"]["capture_archive"].update(sha256="d" * 64),
            lambda job: job["result"].update(native_tool={**self.tool, "source_sha256": "0" * 64}),
            lambda job: job["request"].update(upload_sha256="0" * 64),
            lambda job: job.update(handoff_sha256="c" * 64),
        )
        for index, mutate in enumerate(mutations):
            with self.subTest(mutation=index):
                job = capture_job(RUN, self.archive, self.tool)
                job["result"]["capture_archive"]["sha256"] = file_digest(self.archive)
                mutate(job)
                endpoint = FakeEndpoint(self.archive.read_bytes())
                with self.assertRaises(PipelineError):
                    platform_capture.import_native_capture(self.store, paths, RUN, job, endpoint)

    def test_generate_runs_bounded_and_records_the_checkpoint(self) -> None:
        self.admit(RUN)
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

        events: list[dict] = []
        with (
            mock.patch.object(flow.solidworks, "generate", side_effect=fake_generate),
            mock.patch.object(flow, "tool_record", return_value=self._portable()),
        ):
            receipt = flow.run_stage(self.store, self.store_root, RUN, "generate", on_event=events.append)
        self.assertEqual(receipt["state"], "generated")
        capture_uri = self.store.stage_records(RUN)["capture"]["checkpoint_uri"]
        self.assertEqual(calls["seed_dir"], self.store.sealed_path(capture_uri))
        self.assertEqual(calls["output"], self._paths(RUN).stage_dir("generate"))
        self.assertEqual(calls["run_id"], RUN)
        self.assertIsNone(calls["resume"])
        self.assertEqual(calls["handoff_sha256"], HANDOFF)
        self.assertTrue(any(event.get("state") == "running" for event in events))

    def test_a_registered_checkpoint_is_returned_without_rerunning_the_stage(self) -> None:
        self.admit(RUN)
        subject = self._record_generate()
        with (
            mock.patch.object(flow, "tool_record", return_value=self._portable()),
            mock.patch.object(
                flow.solidworks, "generate", side_effect=AssertionError("registered checkpoints are immutable")
            ),
        ):
            receipt = flow.run_stage(self.store, self.store_root, RUN, "generate")
        self.assertEqual(receipt["state"], "generated")
        self.assertEqual(receipt["subject_sha256"], subject)

    def test_a_forged_receipt_claiming_confirmation_is_refused(self) -> None:
        self.admit(RUN)
        with self.assertRaises(PipelineError):
            self.store.confirm_stage(
                run_id=RUN,
                stage="verify",
                state="completed",
                receipt={
                    "schema_version": "solidworks-to-urdf.bundle/v1",
                    "run_id": "9b2f4c55-0c3a-4f2b-9d1e-7a6b5c4d3e2f",
                    "stage": "publish",
                    "state": "verified",
                    "passed": True,
                    "subject_sha256": "bad",
                },
            )

    def test_a_confirmed_receipt_without_sealed_artifacts_is_refused(self) -> None:
        self.admit(RUN)
        with self.assertRaises(PipelineError):
            self.store.confirm_stage(
                run_id=RUN,
                stage="generate",
                state="completed",
                receipt={
                    "schema_version": "solidworks-to-urdf.bundle/v1",
                    "run_id": RUN,
                    "stage": "generate",
                    "state": "generated",
                    "passed": False,
                    "subject_sha256": "a" * 64,
                    "handoff_sha256": HANDOFF,
                },
            )

    def test_a_tampered_checkpoint_is_refused_instead_of_replayed(self) -> None:
        self.admit(RUN)
        self._record_generate()
        uri = self.store.stage_records(RUN)["generate"]["checkpoint_uri"]
        tampered = self.store.sealed_path(uri) / "model"
        tampered.mkdir(exist_ok=True)
        (tampered / "tampered.bin").write_bytes(b"x")
        with (
            mock.patch.object(flow, "tool_record", return_value=self._portable()),
            mock.patch.object(
                flow.solidworks, "generate", side_effect=AssertionError("registered checkpoints are immutable")
            ),
            self.assertRaisesRegex(PipelineError, "manifest"),
        ):
            flow.run_stage(self.store, self.store_root, RUN, "generate")

    def test_reuse_still_passes_the_runtime_identity_gate(self) -> None:
        self.admit(RUN)
        self._record_generate()
        drifted = self._portable(source_sha256="0" * 64)
        with (
            mock.patch.object(flow, "tool_record", return_value=drifted),
            mock.patch.object(flow.solidworks, "generate", side_effect=AssertionError("must not rerun")),
            self.assertRaisesRegex(PipelineError, "differs from the native capture"),
        ):
            flow.run_stage(self.store, self.store_root, RUN, "generate")

    def test_verify_reuse_requires_the_bound_quality_report(self) -> None:
        self.admit(RUN)
        self._record_generate()
        with self.assertRaises(PipelineError):
            self.store.confirm_stage(
                run_id=RUN,
                stage="verify",
                state="completed",
                receipt={
                    "schema_version": "solidworks-to-urdf.bundle/v1",
                    "run_id": RUN,
                    "stage": "verify",
                    "state": "verified",
                    "passed": True,
                    "subject_sha256": "a" * 64,
                    "handoff_sha256": HANDOFF,
                    "events": [],
                },
            )

    def test_stage_guards_and_identity_gate_fail_closed(self) -> None:
        self.admit(RUN)
        with mock.patch.object(flow, "tool_record", return_value=self._portable()):
            with self.assertRaises(PipelineError):
                flow.run_stage(self.store, self.store_root, RUN, "generate", expected_subject="a" * 64)
            with self.assertRaisesRegex(PipelineError, "recorded subject"):
                flow.run_stage(self.store, self.store_root, RUN, "verify")
            with self.assertRaisesRegex(PipelineError, "model repository"):
                flow.run_stage(self.store, self.store_root, RUN, "publish", expected_subject="a" * 64)
            drifted = self._portable(source_sha256="0" * 64)
            with (
                mock.patch.object(flow, "tool_record", return_value=drifted),
                mock.patch.object(flow.solidworks, "generate", side_effect=AssertionError("must not run")),
                self.assertRaises(PipelineError),
            ):
                flow.run_stage(self.store, self.store_root, RUN, "generate")
            with (
                mock.patch.object(flow, "tool_record", return_value=drifted),
                mock.patch.object(flow.solidworks, "verify", side_effect=AssertionError("must not run")),
                self.assertRaises(PipelineError),
            ):
                flow.run_stage(self.store, self.store_root, RUN, "verify", expected_subject="a" * 64)
        with (
            mock.patch.object(flow.solidworks, "generate", side_effect=AssertionError("must not run")),
            self.assertRaisesRegex(PipelineError, "not admitted"),
        ):
            flow.run_stage(self.store, self.store_root, "0e4f3f74-0e7e-4c31-a1ad-6e5a4a1f0000", "generate")

    def test_capture_is_revalidated_before_generate(self) -> None:
        self.admit(RUN)
        target = self._paths(RUN).capture_dir / "input" / "cad" / MAIN_ASSEMBLY
        target.write_bytes(target.read_bytes() + b"tampered")
        with (
            mock.patch.object(flow, "tool_record", return_value=self._portable()),
            mock.patch.object(flow.solidworks, "generate", side_effect=AssertionError("must not run")),
            self.assertRaises(PipelineError),
        ):
            flow.run_stage(self.store, self.store_root, RUN, "generate")
        self.assertTrue(self._work_json(), "the failure leaves its products in the work tree")
        self.assertNotIn((RUN, "generate"), self.world.stages)

    def test_driver_failure_leaves_a_failed_receipt(self) -> None:
        self.admit(RUN)

        def explode(*args, **kwargs):
            kwargs["on_event"]({"stage": "generate", "state": "running", "at": "2026-10-10T04:00:00+00:00"})
            raise RuntimeError("driver exploded")

        events: list[dict] = []
        with (
            mock.patch.object(flow, "tool_record", return_value=self._portable()),
            mock.patch.object(flow.solidworks, "generate", side_effect=explode),
            self.assertRaises(RuntimeError),
        ):
            flow.run_stage(self.store, self.store_root, RUN, "generate", on_event=events.append)
        self.assertTrue(any(event.get("state") == "running" for event in events))
        payloads = self._work_json()
        self.assertTrue(payloads, "the failure products are retained in the work tree")
        self.assertTrue(
            any("driver exploded" in json.dumps(payload) for payload in payloads),
            "the failed receipt records the driver error",
        )
        self.assertNotIn((RUN, "generate"), self.world.stages)

    def test_fetch_short_circuits_a_committed_admission(self) -> None:
        self.admit(RUN)
        endpoint = mock.Mock()
        endpoint.stream_capture_archive.side_effect = AssertionError("must not re-download")
        job = capture_job(RUN, self.archive, self.tool)
        binding = platform_capture.import_native_capture(self.store, self._paths(RUN), RUN, job, endpoint)
        self.assertEqual(binding["state"], "capture_admitted")
        self.assertEqual(binding["selection"], job["selection"]["manifest"])
        endpoint.stream_capture_archive.assert_not_called()

    def test_verify_and_publish_seed_from_the_previous_checkpoints(self) -> None:
        self.admit(RUN)
        subject = self._record_generate()
        stages: list[tuple] = []

        def fake_verify(seed_dir, output, **kwargs):
            stages.append(("verify", Path(seed_dir), Path(output)))
            Path(output).mkdir(parents=True, exist_ok=True)
            return {
                "state": "verified",
                "passed": True,
                "subject_sha256": subject,
                "events": [{"stage": "verify", "state": "completed", "at": "2026-10-10T02:00:00+00:00"}],
            }

        with (
            mock.patch.object(flow, "tool_record", return_value=self._portable()),
            mock.patch.object(flow.solidworks, "verify", side_effect=fake_verify),
        ):
            receipt = flow.run_stage(self.store, self.store_root, RUN, "verify", expected_subject=subject)
        generate_uri = self.store.stage_records(RUN)["generate"]["checkpoint_uri"]
        self.assertEqual(stages[0][1], self.store.sealed_path(generate_uri))
        self.assertEqual(stages[0][2], self._paths(RUN).stage_dir("verify"))
        self.assertEqual(receipt["state"], "verified")

        # The registered verify checkpoint this case seeds from is fixture-provided:
        # the case owns the flow's seeding and caller forwarding, while the
        # confirmation grammar lives in the store suite (tests.v1.test_platform_store)
        # and the caller-side sealing/registration in the DAG suite
        # (tests.v1.test_airflow_dag).
        verify_uri = f"store://sealed/{RUN}/verify"
        self.store.sealed_path(verify_uri).mkdir(parents=True, exist_ok=True)
        self.world.stages[(RUN, "verify")] = ("completed", {"state": "verified"}, verify_uri)
        published = {
            "state": "published",
            "passed": True,
            "subject_sha256": subject,
            "submission": {"passed": True, "state": "published", "url": "https://example.invalid/pr/1"},
            "events": [{"stage": "publish", "state": "completed", "at": "2026-10-10T03:00:00+00:00"}],
        }
        events: list[dict] = []
        repository = self.tmp / "repo"

        def fake_publish(seed_dir, output, **kwargs):
            stages.append(("publish", Path(seed_dir), Path(output), kwargs.get("repository"), kwargs))
            Path(output).mkdir(parents=True, exist_ok=True)
            kwargs["on_event"]({"stage": "publish", "state": "running", "at": "2026-10-10T03:00:00+00:00"})
            return published

        with (
            mock.patch.object(flow, "tool_record", return_value=self._portable()),
            mock.patch.object(flow.solidworks, "publish", side_effect=fake_publish),
        ):
            publish_receipt = flow.run_stage(
                self.store,
                self.store_root,
                RUN,
                "publish",
                expected_subject=subject,
                repository=repository,
                base="main",
                on_event=events.append,
            )
        publish_call = stages[1][4]
        self.assertEqual(stages[1][1], self.store.sealed_path(verify_uri))
        self.assertEqual(stages[1][2], self._paths(RUN).output_dir)
        self.assertEqual(stages[1][3], repository)
        self.assertEqual(publish_call["expected_subject"], subject)
        self.assertEqual(publish_call["base"], "main")
        self.assertEqual(publish_call["run_id"], RUN)
        self.assertEqual(publish_call["handoff_sha256"], HANDOFF)
        self.assertEqual(publish_receipt["state"], "published")
        self.assertTrue(any(event.get("state") == "running" for event in events))
        # The flow never registers; the caller seals and confirms the receipt.
        self.assertNotIn((RUN, "publish"), self.world.stages)


if __name__ == "__main__":
    unittest.main()
