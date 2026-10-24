"""Per-stage boundary regressions: the native seal, the generated checkpoint, verification."""

from __future__ import annotations

import json
import hashlib
import shutil
import tempfile
import unittest
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from description_pipeline import solidworks, steps
from description_pipeline.delivery import subject_inventory
from description_pipeline.io import PipelineError, digest, file_digest
from description_pipeline.orchestration import stage_transfer as transfer
from description_pipeline.stages import CONTRACT

from .test_stage_entries import REPLAYED_INSPECTION, make_seed
from .test_stage_transfer import capture_root as build_capture_root
from .test_stage_transfer import native_tool as build_native_tool

MAIN = "robot.SLDASM"
HANDOFF = digest({MAIN: hashlib.sha256(b"CAD-BYTES").hexdigest()})


def stage_events(stage_id: str) -> list[dict]:
    """Contract-valid boundary-check events for one stage, then completion."""

    definition = next(stage for stage in CONTRACT["stages"] if stage["id"] == stage_id)
    events = []
    for boundary, key in (("input", "input_qc"), ("output", "output_qc")):
        for item in definition[key]:
            events.append(
                {
                    "at": datetime.now(UTC).isoformat(),
                    "stage": stage_id,
                    "state": "running",
                    "check": {"id": item["id"], "boundary": boundary, "state": "passed", "details": {}},
                }
            )
    events.append({"at": datetime.now(UTC).isoformat(), "stage": stage_id, "state": "completed"})
    return events


def forbid(calls, name):
    def blocked(*args, **kwargs):
        calls[name] += 1
        raise AssertionError(f"{name} must not run inside another stage's entry")

    return blocked


class StageBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.package = self.root / "handoff"
        self.package.mkdir()
        (self.package / "assembly.SLDASM").write_bytes(b"control")
        self.output = self.root / "delivery"

    def _native_events(self, run_id: str) -> list[dict]:
        event_fixture = build_capture_root(
            self.root / f"event-fixture-{run_id}",
            run_id=run_id,
            handoff=HANDOFF,
        )
        return json.loads(
            (event_fixture / "reports/native-stages.json").read_text(encoding="utf-8")
        )["events"]

    @staticmethod
    def _fake_capture(calls, *, run_id):
        def fake_capture(
            package,
            staging,
            *,
            backend=None,
            on_event=None,
            on_activity=None,
            expected_inputs=None,
            handoff_sha256=None,
        ):
            calls["capture"] += 1
            fixture = build_capture_root(
                Path(staging).parent / "fixture",
                run_id=run_id,
                handoff=handoff_sha256,
            )
            shutil.copytree(fixture, Path(staging), dirs_exist_ok=True)
            # The shared fixture models a legacy captured tree; the native boundary
            # now seals raw evidence only and renders no report.
            for name in ("reports/structural-team.md", "reports/structural-team.json"):
                (Path(staging) / name).unlink(missing_ok=True)
            if on_event is not None:
                fixture_events = json.loads(
                    (Path(staging) / "reports/native-stages.json").read_text(encoding="utf-8")
                )["events"]
                for event in (
                    item for item in fixture_events if item.get("stage") == "capture"
                ):
                    on_event(event)
            if on_activity is not None:
                on_activity({"phase": "capture", "action": "seal"})
            return {"hardware_id": "robot"}, {"cad_revision": {"revision": "r1"}, "package_files": {}}

        return fake_capture

    def test_capture_forwards_activity_and_stops_at_the_seal(self) -> None:
        calls = {"capture": 0, "generate": 0, "verify": 0, "publish": 0}
        records = []
        run_id = "8d0c55c4-1cad-5aff-af5f-5cc0bfc5a977"
        native_events = self._native_events(run_id)

        with (
            patch.object(solidworks, "_native_tool_record", return_value=build_native_tool()),
            patch.object(
                steps,
                "capture_evidence",
                side_effect=self._fake_capture(
                    calls,
                    run_id=run_id,
                ),
            ),
            patch.object(steps, "generate_model", side_effect=forbid(calls, "generate")),
            patch.object(steps, "verify_delivery", side_effect=forbid(calls, "verify")),
            patch.object(steps, "publish_model", side_effect=forbid(calls, "publish")),
        ):
            result = solidworks.capture(
                self.package,
                self.output,
                backend=object(),
                run_id=run_id,
                handoff_sha256=HANDOFF,
                main_assembly=MAIN,
                prior_events=[
                    event for event in native_events if event.get("stage") in {"freeze", "discover"}
                ],
                on_activity=records.append,
            )

        self.assertEqual(records, [{"phase": "capture", "action": "seal"}])
        self.assertIs(result["native_complete"], True)
        self.assertEqual(calls, {"capture": 1, "generate": 0, "verify": 0, "publish": 0})

    def test_capture_seals_transfer_and_stops(self) -> None:
        calls = {"capture": 0, "generate": 0, "verify": 0, "publish": 0}
        run_id = "7c9b44b3-0b9c-5ffe-9f4f-4bbdafb4a865"
        native_events = self._native_events(run_id)

        with (
            patch.object(solidworks, "_native_tool_record", return_value=build_native_tool()),
            patch.object(
                steps,
                "capture_evidence",
                side_effect=self._fake_capture(
                    calls,
                    run_id=run_id,
                ),
            ),
            patch.object(steps, "generate_model", side_effect=forbid(calls, "generate")),
            patch.object(steps, "verify_delivery", side_effect=forbid(calls, "verify")),
            patch.object(steps, "publish_model", side_effect=forbid(calls, "publish")),
        ):
            result = solidworks.capture(
                self.package,
                self.output,
                backend=object(),
                run_id=run_id,
                handoff_sha256=HANDOFF,
                main_assembly=MAIN,
                prior_events=[
                    event for event in native_events if event.get("stage") in {"freeze", "discover"}
                ],
            )
        self.assertIs(result["native_complete"], True)
        self.assertIs(result["passed"], False)
        self.assertEqual(result["state"], "native_complete", result.get("error"))
        self.assertEqual(result["execution_scope"], ["freeze", "discover", "capture"])
        archive = self.output / transfer.CAPTURE_ARCHIVE
        manifest = self.output / transfer.CAPTURE_MANIFEST
        self.assertTrue(archive.is_file())
        self.assertTrue(manifest.is_file())
        info = result["capture_archive"]
        self.assertEqual(set(info), {"name", "sha256", "size", "manifest_sha256"})
        self.assertEqual(info["name"], transfer.CAPTURE_ARCHIVE)
        self.assertEqual(info["sha256"], file_digest(archive))
        self.assertEqual(info["size"], archive.stat().st_size)
        self.assertEqual(info["manifest_sha256"], file_digest(manifest))
        self.assertEqual(calls, {"capture": 1, "generate": 0, "verify": 0, "publish": 0})
        self.assertTrue((self.output / "reports/native-tool.json").is_file())
        self.assertTrue((self.output / "reports/stages.json").is_file())
        native_stages = self.output / "reports" / "native-stages.json"
        self.assertTrue(native_stages.is_file())
        native_view = json.loads(native_stages.read_text(encoding="utf-8"))
        rendered = json.loads((self.output / "reports/stages.json").read_text(encoding="utf-8"))
        self.assertEqual({key: value for key, value in native_view.items() if key != "events"}, rendered)
        self.assertEqual(native_view["events"], result["events"])
        self.assertTrue(native_view["events"])
        self.assertTrue(all(isinstance(event.get("at"), str) and event["at"] for event in native_view["events"]))
        self.assertEqual(result["state"], "native_complete")
        self.assertIs(result["passed"], False)
        self.assertEqual(result["capture_archive"], info)

        # Real-sealer roundtrip: seal -> admit, and only the immutable native receipt travels.
        with zipfile.ZipFile(archive) as opened:
            names = opened.namelist()
        self.assertIn("reports/native-stages.json", names)
        self.assertNotIn("reports/stages.json", names)
        self.assertNotIn("reports/run.json", names)
        # The native capture seals raw evidence only: no rendered report travels
        # with it, and the Linux side re-creates the report from its receipt.
        self.assertNotIn("reports/structural-team.md", names)
        self.assertNotIn("reports/structural-team.json", names)
        destination = self.root / "admitted"
        transfer.admit_capture(
            archive,
            destination,
            expected_run_id=result["run_id"],
            expected_handoff_sha256=HANDOFF,
            expected_upload_sha256=HANDOFF,
            expected_main_assembly=MAIN,
            expected_native_tool=build_native_tool(),
        )
        self.assertEqual(
            (destination / "reports" / "native-stages.json").read_bytes(),
            native_stages.read_bytes(),
        )
        # A native capture-only run carries no rendered report on either side.
        for name in ("structural-team.md", "structural-team.json"):
            self.assertFalse((self.output / "reports" / name).exists())
            self.assertFalse((destination / "reports" / name).exists())

    def test_generate_installs_unverified_checkpoint(self) -> None:
        seed = make_seed(self.root)
        calls = {"generate": 0, "verify": 0, "publish": 0}

        def fake_generate(staging, definition, input_report, on_event=None):
            calls["generate"] += 1
            return "a" * 64

        with (
            patch.object(steps, "generate_model", side_effect=fake_generate),
            patch.object(steps, "verify_delivery", side_effect=forbid(calls, "verify")),
            patch.object(steps, "publish_model", side_effect=forbid(calls, "publish")),
            patch.object(steps, "inspect_prepared_input", return_value=REPLAYED_INSPECTION),
        ):
            result = solidworks.generate(
                seed,
                self.output,
                resume={"parent_run": "8b6adf2e-5e19-4d87-a177-e26c2f0f4a1c", "from_stage": "generate"},
            )
        self.assertEqual(result["state"], "generated")
        self.assertIs(result["passed"], False)
        self.assertEqual(result["subject_sha256"], "a" * 64)
        self.assertEqual(result["execution_scope"], ["generate"])
        self.assertEqual(calls, {"generate": 1, "verify": 0, "publish": 0})
        self.assertTrue((self.output / "input" / "robot.yaml").is_file())
        self.assertTrue((self.output / "reports/stages.json").is_file())
        self.assertFalse((self.output / "reports" / "quality.json").exists())

    def test_a_non_empty_output_is_refused_and_left_untouched(self) -> None:
        user = self.root / "user-space"
        (user / "reports").mkdir(parents=True)
        (user / "input").mkdir()
        notes = user / "reports/user-notes.md"
        notes.write_text("keep me\n", encoding="utf-8")
        private = user / "input/private-cad-note.txt"
        private.write_text("keep me too\n", encoding="utf-8")
        readme = user / "README.md"
        readme.write_text("unrelated\n", encoding="utf-8")

        with self.assertRaisesRegex(PipelineError, "absent or empty"):
            solidworks.capture(self.package, user, backend=object(), run_id="b6a1f2c3-4d5e-6f70-8192-a3b4c5d6e7f8")
        self.assertEqual("keep me\n", notes.read_text(encoding="utf-8"))
        self.assertEqual("keep me too\n", private.read_text(encoding="utf-8"))
        self.assertEqual("unrelated\n", readme.read_text(encoding="utf-8"))

    def test_a_sealed_checkpoint_is_never_overwritten(self) -> None:
        seed = make_seed(self.root)
        calls = {"generate": 0}

        def fake_generate(staging, definition, input_report, on_event=None):
            calls["generate"] += 1
            return "a" * 64

        with (
            patch.object(steps, "generate_model", side_effect=fake_generate),
            patch.object(steps, "inspect_prepared_input", return_value=REPLAYED_INSPECTION),
        ):
            first = solidworks.generate(seed, self.output)
            before = {
                path.relative_to(self.output).as_posix(): path.read_bytes()
                for path in sorted(self.output.rglob("*"))
                if path.is_file()
            }
            with self.assertRaisesRegex(PipelineError, "absent or empty"):
                solidworks.generate(seed, self.output)
        self.assertEqual(first["state"], "generated")
        self.assertEqual(calls["generate"], 1)
        after = {
            path.relative_to(self.output).as_posix(): path.read_bytes()
            for path in sorted(self.output.rglob("*"))
            if path.is_file()
        }
        self.assertEqual(after, before)

    def test_verify_reruns_the_checker_on_the_seeded_subject(self) -> None:
        seed = make_seed(self.root)
        calls = {"generate": 0, "verify": 0}

        def fake_generate(staging, definition, input_report, on_event=None):
            calls["generate"] += 1
            staging = Path(staging)
            (staging / "model").mkdir(exist_ok=True)
            (staging / "model" / "robot.json").write_text("{}\n", encoding="utf-8")
            (staging / "urdf").mkdir(exist_ok=True)
            (staging / "urdf" / "robot.urdf").write_text("<robot/>\n", encoding="utf-8")
            (staging / "meshes").mkdir(exist_ok=True)
            (staging / "meshes" / "part.stl").write_bytes(b"stl")
            (staging / "tool.json").write_text("{}\n", encoding="utf-8")
            (staging / "manifest.json").write_text("{}\n", encoding="utf-8")
            return digest(subject_inventory(staging))

        def fake_verify(staging, generated_subject, on_event=None):
            calls["verify"] += 1
            return {"passed": True, "subject_sha256": generated_subject}

        generated = self.root / "generated"
        with (
            patch.object(steps, "generate_model", side_effect=fake_generate),
            patch.object(steps, "verify_delivery", side_effect=fake_verify),
            patch.object(steps, "inspect_prepared_input", return_value=REPLAYED_INSPECTION),
        ):
            checkpoint = solidworks.generate(seed, generated)
            result = solidworks.verify(generated, self.output, expected_subject=checkpoint["subject_sha256"])
        self.assertEqual(checkpoint["state"], "generated")
        self.assertEqual(result["state"], "verified")
        self.assertIs(result["passed"], True)
        self.assertEqual(result["quality"]["passed"], True)
        self.assertEqual(result["subject_sha256"], checkpoint["subject_sha256"])
        self.assertEqual(calls, {"generate": 1, "verify": 1})

    def test_generate_carries_the_transfer_provenance(self) -> None:
        seed = make_seed(self.root)
        (seed / "reports" / "native-tool.json").write_text('{"role": "native"}\n', encoding="utf-8")
        (seed / "reports" / "native-stages.json").write_text('{"stages": []}\n', encoding="utf-8")
        (seed / "transfer-manifest.json").write_text('{"schema_version": "stub"}\n', encoding="utf-8")
        calls = {"generate": 0, "verify": 0}

        def fake_generate(staging, definition, input_report, on_event=None):
            calls["generate"] += 1
            return "a" * 64

        def fake_verify(staging, generated_subject, on_event=None):
            calls["verify"] += 1
            return {"passed": True, "subject_sha256": generated_subject}

        with (
            patch.object(steps, "generate_model", side_effect=fake_generate),
            patch.object(steps, "verify_delivery", side_effect=fake_verify),
            patch.object(steps, "inspect_prepared_input", return_value=REPLAYED_INSPECTION),
        ):
            result = solidworks.generate(seed, self.output, handoff_sha256=HANDOFF)
        self.assertEqual(result["state"], "generated")
        self.assertEqual(calls, {"generate": 1, "verify": 0})
        self.assertEqual(result["handoff_sha256"], HANDOFF)
        self.assertEqual(
            (self.output / "transfer-manifest.json").read_text(encoding="utf-8"),
            '{"schema_version": "stub"}\n',
        )
        self.assertEqual(
            (self.output / "reports" / "native-tool.json").read_text(encoding="utf-8"),
            '{"role": "native"}\n',
        )
        self.assertEqual(
            (self.output / "reports" / "native-stages.json").read_text(encoding="utf-8"),
            '{"stages": []}\n',
        )


if __name__ == "__main__":
    unittest.main()
