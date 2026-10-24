"""Control-fixture split roundtrip: real freeze → sealed transfer → real generate/verify.

This exercises the producer/consumer boundary end to end with the fixture CAD backend and
the real generation and verification algorithms (including the MuJoCo consumer gate on the
portable role).  The control fixture is explicitly NOT native CAD qualification.
"""

from __future__ import annotations

import copy
import hashlib
import json
import shutil
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from description_pipeline import solidworks
from description_pipeline.io import digest
from description_pipeline.interface_records import PURPOSE_ENGINEERING
from description_pipeline.orchestration import stage_transfer as transfer
from description_pipeline.request_evidence import build_request_evidence, request_evidence_bytes
from description_pipeline.sources.solidworks import capture_transaction
from description_pipeline.sources.solidworks.discovery import prepare_native_package
from description_pipeline.sources.solidworks.revision import package_inventory
from description_pipeline.sources.solidworks.selection import seal_closure_selection

from tests.sources import support
from tests.sources.test_solidworks_discovery import FakeBackend, record as native_record
from tests.v1 import workflow_fixture as steps
from .test_stage_transfer import native_tool


def capture_record() -> dict:
    """The shared discovery fixture sealed with this assembly's occurrence identities.

    The capture proves occurrence coverage by exact ``(owner document, instance)``
    identity between the admitted closure and the re-opened session.  The shared
    fixture spells closure instances as canonical document names, so re-seal it
    with the Name2-style instances the fixture session reports.
    """

    record = copy.deepcopy(native_record())
    selection = record["source_selection"]
    instances = {"cad/base.SLDPRT": "base-1", "cad/arm.SLDPRT": "arm-1"}
    for edge in selection["closure"]["documents"]["cad/robot.SLDASM"]["resolved_edges"]:
        edge["instance"] = instances[edge["resolved"].replace("\\", "/")]
    files = {**selection["excluded_files"], **selection["files"]}
    record["source_selection"] = seal_closure_selection(
        files,
        selection["main_assembly"],
        selection["closure"],
        reader_sha256=selection["reader_sha256"],
    )
    return record


def fixture_mass_payload(
    mass: float,
    com: tuple[float, float, float],
) -> dict:
    """Mass fixture with the configuration consumed by source-scope checks."""

    payload = support.mass_payload(mass, com)
    payload["reference"] = {
        **payload["reference"],
        "configuration": "Default",
    }
    return payload


class AxisFixtureBackend(support.FixtureCadBackend):
    """Fixture backend that can resolve the authored joint axis selector."""

    evidence_class = "cad"

    def native_ready(self) -> dict:
        return {
            "role": "native",
            "scope": "provided synthetic CAD backend for the capture regression suite",
            "solidworks_executable": r"C:\Program Files\SOLIDWORKS Corp\SOLIDWORKS\SLDWORKS.exe",
        }

    def environment(self) -> dict:
        environment = super().environment()
        copy_session = environment["sessions"]["copy"]
        if not hasattr(self, "_capture_source_pid"):
            self._capture_source_pid = max(1, self.session_pid - 1)
        environment["sessions"] = {
            "source": {
                "pid": self._capture_source_pid,
                "state": "closed",
            },
            "copy": {
                "pid": copy_session["pid"],
                "state": copy_session["state"],
            },
        }
        return environment

    def retire_session(self, role: str = "copy") -> dict:
        self.assert_role(role)
        before = {
            "pid": self.session_pid,
            "state": "active",
        }
        self.retired_session_pids.append(self.session_pid)
        self.open_documents.clear()
        self.session_pid += 1
        return {
            "role": role,
            "retired": True,
            "before": before,
            "after": {
                "pid": before["pid"],
                "state": "closed",
            },
        }

    @staticmethod
    def assert_role(role: str) -> None:
        if role != "copy":
            raise AssertionError(f"unexpected retired role: {role}")

    def collect_scene(self, doc_path, coordinate_systems, on_activity=None, require_material=True):
        self._capture_document = Path(doc_path).resolve()
        scene = super().collect_scene(
            doc_path,
            coordinate_systems,
            on_activity=on_activity,
            require_material=require_material,
        )
        for row in scene.components:
            row.path = str(self._source_path(row.name))
        return scene

    def _source_path(self, component):
        capture_root = getattr(self, "_capture_document", self.document).parent
        entry = next(
            (
                row
                for row in self.components
                if str(row.get("name")) == str(component)
            ),
            None,
        )
        path_name = (
            Path(str(entry.get("path"))).name
            if entry is not None and entry.get("path")
            else f"{str(component).split('-', 1)[0]}.SLDPRT"
        )
        source_path = capture_root / path_name
        if not source_path.is_file():
            source_path = next(
                (
                    capture_root / dependency.name
                    for dependency in self._dependencies
                    if dependency.name.casefold() == path_name.casefold()
                ),
                source_path,
            )
        if not source_path.is_file():
            raise RuntimeError(
                f"control fixture source scope has no source file: {component}"
            )
        return source_path

    def source_scopes(self, components):
        """Supply the control fixture's exact source scope records."""

        scopes = {}
        for component in components:
            source_path = self._source_path(component)
            sha256 = hashlib.sha256(source_path.read_bytes()).hexdigest()
            scopes[str(component)] = {
                "rule_version": "native-open-edge-scope-binding-v1",
                "component": str(component),
                "source_document_path": str(source_path),
                "source_copy_path": None,
                "source_document_sha256": sha256,
                "source_copy_sha256": sha256,
                "source_document_identity_sha256": sha256,
                "source_document_identity_kind": "file_sha256",
                "referenced_configuration": str(
                    self.configurations[0]
                ),
                "occurrence_scope": str(component),
                "virtual_owner": None,
                "virtual_owner_sha256": None,
                "virtual_session_document_path": None,
                "virtual_session_temp_root": None,
                "virtual_native_api_pid": None,
            }
        return scopes

    def assembly_mass_properties(self, path):
        from description_pipeline.sources.solidworks.errors import CadError

        raise CadError("cad_mass_property_unavailable", "Control fixture has no native whole-assembly reading")

    def assembly_component_mass_properties(self, path):
        from description_pipeline.sources.solidworks.errors import CadError

        raise CadError("cad_component_context_unavailable", "Control fixture has no native instance context")

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

    def export_component_meshes(self, destinations, on_activity=None):
        """Mirror the native exporter response: body counts plus tessellation accounting.

        The real native reader reports, per component, the native body counts and
        the display-tessellation accounting (total faces and any facetless sheet
        faces) beside the exported mesh identity.  The control fixture reports
        the canonical cube stand-in: one solid body whose six faces all
        tessellate, with no omitted faces.
        """

        entries = super().export_component_meshes(destinations, on_activity=on_activity)
        for info in entries.values():
            info["bodies"] = {"solid": 1, "sheet": 0}
        return entries


class _LegacySplitFixture:
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
        self.discovery_backend = FakeBackend(capture_record())
        self.capture_output = self.root / "capture-delivery"
        self.delivery_output = self.root / "portable-delivery"
        self.prepared = self.root / "prepared"
        self.handoff = digest(package_inventory(self.handoff_dir))
        self.run_id = "3f2c1f7a-9d4e-4d0a-9f11-2b6c5a7e8d90"

    def _prepare(self, events: list[dict]) -> tuple[Path, dict, dict]:
        """Freeze and discover the control handoff; the shared setup for capture and resume."""

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
            configuration={"record_roots": [str(self.records)], "purpose": PURPOSE_ENGINEERING},
            targets={"nd_fixture": {"repository_slug": "mimicverse/description", "base": "feature/control"}},
            preparer=lambda *args, **kwargs: prepare_native_package(*args, backend=self.discovery_backend, **kwargs),
            on_event=record,
        )
        self.assertTrue(prepared.hardware_id)
        self.assertEqual(target["repository_slug"], "mimicverse/description")
        self.assertTrue(any(event.get("stage") == "discover" for event in events))
        return prepared_package, target, prepared_files

    def _capture(self, events: list[dict], prepared_package: Path, prepared_files: dict) -> dict:
        """Run the real capture phase on the native role and return its stage receipt."""

        backend = AxisFixtureBackend(
            prepared_package / "cad/robot.SLDASM",
            [
                {
                    "name": "base-1",
                    "transform": support.placement((0.0, 0.0, 0.0)),
                    # Off-axis COM: the product signal proves the convention at capture time.
                    "mass": fixture_mass_payload(0.192, (0.01, 0.02, 0.0)),
                },
                {
                    "name": "arm-1",
                    "transform": support.placement((0.0, 0.0, 0.1)),
                    "mass": fixture_mass_payload(
                        0.105654866776462,
                        (0.0, 0.0, 0.05),
                    ),
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
        # fact (the role-aware tool record refuses the portable host).  A Windows-rooted
        # producer root, when the scenario asks for one, enters the handoff BEFORE the
        # seal so the sealed receipt carries it coherently.
        windows_root = getattr(self, "_windows_root", None)
        original_inspect = capture_transaction.inspect_prepared_package

        def inspect_with_root(*args, **kwargs):
            report = original_inspect(*args, **kwargs)
            if windows_root is not None:
                report["input_receipt"] = {
                    **(report.get("input_receipt") or {}),
                    "resolved_package_root": windows_root,
                }
            return report

        with (
            patch.object(solidworks, "_native_tool_record", return_value=native_tool()),
            patch(
                "description_pipeline.runtime.tool_record",
                return_value=native_tool(),
            ),
            patch.object(
                capture_transaction,
                "inspect_prepared_package",
                side_effect=inspect_with_root,
            ),
        ):
            capture = solidworks.capture(
                prepared_package,
                self.capture_output,
                backend=backend,
                run_id=self.run_id,
                handoff_sha256=self.handoff,
                main_assembly="cad/robot.SLDASM",
                expected_inputs=prepared_files,
                prior_events=events,
            )
        self.assertEqual(
            capture["state"],
            "native_complete",
            capture.get("error"),
        )
        self.assertIs(capture["passed"], False)
        archive = self.capture_output / transfer.CAPTURE_ARCHIVE
        manifest_path = self.capture_output / transfer.CAPTURE_MANIFEST
        self.assertTrue(archive.is_file() and manifest_path.is_file())
        source_scopes = json.loads(
            (
                self.capture_output
                / "evidence/raw/source_scopes.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(set(source_scopes), {"base-1", "arm-1"})
        for component, scope in source_scopes.items():
            self.assertEqual(scope["component"], component)
            self.assertEqual(
                scope["source_copy_path"],
                f"source/{component.split('-', 1)[0]}.SLDPRT",
            )
            self.assertEqual(
                scope["source_document_identity_kind"],
                "file_sha256",
            )
            self.assertEqual(
                scope["source_document_sha256"],
                scope["source_document_identity_sha256"],
            )
        native_stages = json.loads((self.capture_output / "reports/native-stages.json").read_text(encoding="utf-8"))
        self.assertEqual(native_stages["execution_scope"], ["freeze", "discover", "capture"])
        self.assertTrue(native_stages["events"])
        # The DAG materializes the frozen request into the capture tree; the
        # generate reader refuses a delivery that does not carry it.
        request = build_request_evidence(
            purpose=PURPOSE_ENGINEERING,
            request_id="fixture-frozen-request",
            target_hardware="nd_fixture",
            change_summary="split roundtrip fixture request",
            main_assembly="cad/robot.SLDASM",
            handoff_digest=self.handoff,
            platform_config_digest="c" * 64,
        )
        (self.capture_output / "input").mkdir(parents=True, exist_ok=True)
        (self.capture_output / "input" / "request.json").write_bytes(request_evidence_bytes(request))
        return native_stages

    def _windows_resume_seed(
        self, events: list[dict], prepared_package: Path, prepared_files: dict
    ) -> tuple[Path, Path]:
        """A generate seed whose frozen receipt carries the producing host's absolute root.

        The seed is a real capture delivery; everything matches the produced report byte for
        byte except ``resolved_package_root``, which keeps the absolute Windows path a
        genuine native capture records, while the archived Linux inputs stay identical.
        """

        self._windows_root = rf"C:\description-v1-review\acceptance-review\state\prepared\{self.run_id}"
        try:
            self._capture(events, prepared_package, prepared_files)
        finally:
            del self._windows_root
        seed = self.root / "resume-seed"
        shutil.copytree(self.capture_output, seed)
        sealed_receipt = seed / "input/handoff.json"
        # The Windows producer root is part of the sealed receipt itself - nothing is
        # edited after the seal.
        self.assertIn(
            "resolved_package_root",
            json.loads(sealed_receipt.read_text(encoding="utf-8"))["input_receipt"],
        )
        return seed, sealed_receipt

    def test_windows_rooted_receipt_resumes_from_the_local_archived_input(self) -> None:
        events: list[dict] = []
        prepared_package, _target, prepared_files = self._prepare(events)
        seed, sealed_receipt = self._windows_resume_seed(events, prepared_package, prepared_files)
        frozen = sealed_receipt.read_bytes()

        output = self.root / "portable-generate"
        result = solidworks.generate(
            seed,
            output,
            run_id=self.run_id,
            handoff_sha256=self.handoff,
            prior_events=events,
        )
        self.assertEqual(result.get("state"), "generated", result.get("error"))
        self.assertTrue(result.get("subject_sha256"))
        # The frozen native report is evidence and stays byte-for-byte unchanged ...
        self.assertEqual(sealed_receipt.read_bytes(), frozen)
        # ... and the generated checkpoint carries that archived report, not a local replay:
        # accidentally handing generate_model the replayed report would fail here.
        self.assertEqual((output / "input/handoff.json").read_bytes(), frozen)

    def test_altered_captured_input_still_refuses_the_resume(self) -> None:
        events: list[dict] = []
        prepared_package, _target, prepared_files = self._prepare(events)
        seed, _sealed_receipt = self._windows_resume_seed(events, prepared_package, prepared_files)
        altered = seed / "input" / "cad" / "cad" / "base.SLDPRT"
        altered.write_bytes(altered.read_bytes() + b"tamper")

        output = self.root / "portable-generate-tampered"
        result = solidworks.generate(
            seed,
            output,
            run_id=self.run_id,
            handoff_sha256=self.handoff,
            prior_events=events,
        )
        # A refused continuation returns the failed receipt and installs nothing.
        self.assertEqual(result.get("state"), "failed")
        self.assertIs(result.get("passed"), False)
        self.assertIn("Retained input bytes do not match the recorded inspection receipt", str(result.get("error")))
        self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
