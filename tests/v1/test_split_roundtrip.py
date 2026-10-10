"""Control-fixture split roundtrip: real freeze → sealed transfer → real generate/verify.

This exercises the producer/consumer boundary end to end with the fixture CAD backend and
the real generation and verification algorithms (including the MuJoCo consumer gate on the
portable role).  The control fixture is explicitly NOT native CAD qualification.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from description_pipeline import solidworks, steps
from description_pipeline.io import digest
from description_pipeline.orchestration import stage_transfer as transfer
from description_pipeline.orchestration import linux_runner
from description_pipeline.orchestration.linux_store import LinuxStore
from description_pipeline.sources.solidworks.discovery import prepare_native_package
from description_pipeline.sources.solidworks.revision import package_inventory

from tests.sources import support
from tests.sources.test_solidworks_discovery import FakeBackend, record as native_record
from .test_linux_split import portable_record
from .test_stage_transfer import native_tool


class AxisFixtureBackend(support.FixtureCadBackend):
    """Fixture backend that can resolve the authored joint axis selector."""

    def capture_axis_reference(self, reference):
        record = {
            "component": str(reference.get("component") or ""),
            "body_type": str(reference.get("body_type") or "solid"),
            "selector": {key: value for key, value in reference.items() if key != "note"},
            "surface": "cylinder",
            "coordinate_frame": "component_local",
            "direction_semantics": "undirected_axis_line",
            "axis_point_m": [0.0, 0.0, 0.1],
            "axis_direction": [0.0, 0.0, 1.0],
            "radius_m": 0.006,
            "used_api": "fixture",
        }
        if isinstance(reference.get("face_index"), int) and not isinstance(reference.get("face_index"), bool):
            record["face_index"] = reference["face_index"]
        return record


class SplitRoundtripTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.handoff_dir = self.root / "native"
        for name in ("cad/robot.SLDASM", "cad/base.SLDPRT", "cad/arm.SLDPRT"):
            path = self.handoff_dir / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(f"placeholder {name}\n".encode())
        self.records = self.root / "records"
        limits = self.records / "joints" / "arm.json"
        limits.parent.mkdir(parents=True, exist_ok=True)
        limits.write_text(
            json.dumps({"limits": {"lower": -1.5, "upper": 1.5}, "drive": {"effort": 6.0, "velocity": 2.0}}),
            encoding="utf-8",
        )
        (self.records / "budget.json").write_text(
            json.dumps({"robot": {"expected_mass_kg": [0.28, 0.32], "expected_extent_m": [0.35, 0.5]}}),
            encoding="utf-8",
        )
        self.discovery_backend = FakeBackend(native_record())
        self.capture_output = self.root / "capture-delivery"
        self.delivery_output = self.root / "portable-delivery"
        self.prepared = self.root / "prepared"
        self.handoff = digest(package_inventory(self.handoff_dir))
        self.run_id = "3f2c1f7a-9d4e-4d0a-9f11-2b6c5a7e8d90"

    def test_real_freeze_seals_and_the_real_tail_verifies(self) -> None:
        events: list[dict] = []

        def record(event: dict) -> None:
            # The endpoint stamps events as it records them; mirror that contract here.
            events.append({"at": datetime.now(UTC).isoformat(), **event})

        steps.freeze_inputs(
            self.handoff_dir,
            self.handoff,
            package_inventory(self.handoff_dir),
            main_assembly="cad/robot.SLDASM",
            on_event=record,
        )
        prepared_package, target, prepared, prepared_files = steps.discover_structure(
            self.handoff_dir,
            self.prepared,
            self.run_id,
            expected_digest=self.handoff,
            expected_files=package_inventory(self.handoff_dir),
            main_assembly="cad/robot.SLDASM",
            configuration={"record_roots": [str(self.records)]},
            targets={"nd_fixture": {"repository_slug": "mimicverse/description", "base": "feature/control"}},
            preparer=lambda *args, **kwargs: prepare_native_package(*args, backend=self.discovery_backend, **kwargs),
            on_event=record,
        )
        self.assertTrue(prepared.hardware_id)
        self.assertEqual(target["repository_slug"], "mimicverse/description")
        self.assertTrue(any(event.get("stage") == "discover" for event in events))

        backend = AxisFixtureBackend(
            prepared_package / "cad/robot.SLDASM",
            [
                {
                    "name": "base-1",
                    "transform": support.placement((0.0, 0.0, 0.0)),
                    "mass": support.mass_payload(0.192, (0.0, 0.0, 0.0)),
                },
                {
                    "name": "arm-1",
                    "transform": support.placement((0.0, 0.0, 0.1)),
                    "mass": support.mass_payload(0.105654866776462, (0.0, 0.0, 0.05)),
                },
            ],
            dependencies=[
                prepared_package / "cad/base.SLDPRT",
                prepared_package / "cad/arm.SLDPRT",
            ],
            coordinate_systems={
                "CS_base_link": support.placement((0.0, 0.0, 0.0)),
                "CS_arm_link": support.placement((0.0, 0.0, 0.1)),
            },
        )

        # The capture phase runs under the native role; simulate only that environment
        # fact (the role-aware tool record refuses the portable host).
        with patch.object(solidworks, "_native_tool_record", return_value=native_tool()):
            capture = solidworks.run(
                prepared_package,
                self.capture_output,
                stop_after="capture",
                backend=backend,
                run_id=self.run_id,
                handoff_sha256=self.handoff,
                main_assembly="cad/robot.SLDASM",
                expected_inputs=prepared_files,
                prior_events=events,
            )
        self.assertIs(capture["native_complete"], True)
        self.assertIs(capture["passed"], False)
        archive = self.capture_output / transfer.CAPTURE_ARCHIVE
        manifest_path = self.capture_output / transfer.CAPTURE_MANIFEST
        self.assertTrue(archive.is_file() and manifest_path.is_file())
        native_stages = json.loads((self.capture_output / "reports/native-stages.json").read_text(encoding="utf-8"))
        self.assertEqual(native_stages["execution_scope"], ["freeze", "discover", "capture"])
        self.assertTrue(native_stages["events"])

        # The real Linux boundary: store admission seeds the raw native events, then the
        # portable runner drives generate and verify as their own staged checkpoints with
        # the actual generator and verifier (MuJoCo consumer gate included).
        store = LinuxStore(self.root / "linux-store")
        admitted = store.import_capture(
            self.run_id,
            archive,
            expected_handoff_sha256=self.handoff,
            expected_main_assembly="cad/robot.SLDASM",
            expected_native_tool=native_tool(),
        )
        self.assertEqual(admitted.get("state"), "capture_admitted")
        seeded = [
            event for event in store.events(self.run_id) if event.get("stage") in {"freeze", "discover", "capture"}
        ]
        self.assertEqual(seeded, native_stages["events"])

        with patch.object(linux_runner, "tool_record", return_value=portable_record(native_tool())):
            generated = linux_runner.run_portable_stage(store, self.run_id, "generate")
            self.assertEqual(generated.get("state"), "generated", generated.get("error"))
            subject = generated.get("subject_sha256")
            self.assertTrue(subject)
            result = linux_runner.run_portable_stage(store, self.run_id, "verify", expected_subject=subject)
        # The control fixture is explicitly NOT native CAD qualification: the release-only
        # gates below can never pass on it.  Everything else must, and any drift beyond the
        # documented control limit set fails this test.
        control_limits = {
            "source.native",  # control fixture is not native CAD qualification
            "source.dependencies",  # fixture closure has no top_level mapping row
            "physics.mass_closure_equality",  # fixture backend has no assembly_mass_properties
            "physics.independent",  # fixture inertia payload carries no qualified API receipt
            "geometry.base_link",
            "geometry.arm_link",
            "geometry.assets",
            "geometry.expected_extent",
        }
        diagnostic = Path(result["diagnostic_path"])
        quality = json.loads((diagnostic / "reports/quality.json").read_text(encoding="utf-8"))
        failed = {
            check["id"] for check in quality["checks"] if check.get("passed") is False or check.get("state") == "failed"
        }
        self.assertTrue(failed <= control_limits, sorted(failed - control_limits))
        gates = {check.get("id"): check for check in quality["checks"]}
        for required in (
            "bundle.subject",
            "input.valid",
            "source.native_discovery",
            "frames.native",
            "urdf.syntax_names",
            "joints.shoulder_pitch_joint",
            "urdf.topology",
            "consumer.urdf",
            "verification.complete",
        ):
            self.assertTrue(gates[required].get("passed"), gates[required])
        self.assertEqual(quality["subject_sha256"], gates["bundle.subject"]["details"]["sha256"])
        self.assertEqual(quality["subject_sha256"], subject)
        self.assertEqual(result["state"], "failed")
        self.assertIn("URDF verification failed", result["error"])
        self.assertEqual(store.receipt_path(self.run_id, "generate").is_file(), True)
        stored_verify = json.loads(store.receipt_path(self.run_id, "verify").read_text(encoding="utf-8"))
        self.assertEqual(stored_verify["state"], "failed")

        # The real generator ran and the transfer provenance survived the portable tail.
        self.assertTrue((diagnostic / "urdf/robot.urdf").is_file())
        self.assertTrue((diagnostic / "transfer-manifest.json").is_file())
        self.assertTrue((diagnostic / "reports/native-tool.json").is_file())
        carried = json.loads((diagnostic / "reports/native-stages.json").read_text(encoding="utf-8"))
        self.assertEqual(carried["events"], native_stages["events"])


if __name__ == "__main__":
    unittest.main()
