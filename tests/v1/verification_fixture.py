"""Hermetic native evidence fixture for Verify and end-to-end tests.

The native-shaped backend exercises the production path; it is not physical CAD
qualification or a claim about a licensed SolidWorks installation.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from description_pipeline import solidworks
from description_pipeline.delivery import read_delivery_manifest
from description_pipeline.interface_records import PURPOSE_ENGINEERING
from description_pipeline.io import digest
from description_pipeline.pipeline.contracts import ArtifactRef
from description_pipeline.request_evidence import build_request_evidence, request_evidence_bytes
from description_pipeline.runtime import tool_record
from description_pipeline.sources.solidworks.discovery import prepare_native_package
from description_pipeline.sources.solidworks.revision import package_inventory

from tests.sources import support
from tests.sources.test_solidworks_discovery import FakeBackend
from tests.v1 import workflow_fixture as steps
from tests.v1.native_fixture_support import AxisFixtureBackend, capture_record
from tests.v1.test_stage_transfer import native_tool

RUN_ID = "3f2c1f7a-9d4e-4d0a-9f11-2b6c5a7e8d90"
MAIN_ASSEMBLY = "cad/robot.SLDASM"
BASE_BRANCH = "feature/nd_fixture"
OVERRIDES = {
    "OverrideMass": False,
    "OverrideCenterOfMass": False,
    "OverrideMomentsOfInertia": False,
}


def _native_tool() -> dict:
    return {**native_tool(), "release": None}


def _portable_tool() -> dict:
    native = _native_tool()
    return {
        **tool_record("portable"),
        "source_sha256": native["source_sha256"],
        "release": None,
    }


def _git(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def _mass_reading(mass: float, com: tuple[float, float, float], part_document: Path) -> dict:
    """One self-contained part reading accepted by the real physics verifier."""

    return {
        "mass": mass,
        "com": list(com),
        "inertia": [[0.001, 0.0, 0.0], [0.0, 0.001, 0.0], [0.0, 0.0, 0.001]],
        "reference": {
            "used_api": "IMassProperty2.GetMomentOfInertia(0)",
            "product_convention": "solidworks_standard",
            "scope": "part_document",
            "axes": "part_document_axes",
            "reference_point": "center_of_mass",
            "use_system_units": True,
            "configuration": "Default",
            "part_document": str(part_document),
            "volume_m3": mass / 1000.0,
            "density_kg_m3": 1000.0,
            "overrides": dict(OVERRIDES),
            "material_assignment": {
                "part": {"name": "Alloy Steel", "database": "SolidWorks Materials"},
                "bodies": [{"material": {}, "effective_source": "part"}],
            },
        },
    }


def _fixture_appearance_owner(configuration: str, values=None) -> dict:
    return {
        "used_api": "fixture-native-appearance-owner",
        "material_call": {
            "configuration_semantics": "active_configuration",
            "active_configuration": configuration,
        },
        "presence_call": {
            "arguments": [],
            "configuration_semantics": "native_owner_context",
        },
        "material_property_values": values,
        "has_material_property_values": values is not None,
        "texture": {
            "configuration": configuration,
            "state": "absent",
            "material_name": None,
        },
        "read_errors": {},
    }


def _fixture_appearance_raw(
    component: str,
    source_scope: dict,
    face_inventory: list[dict],
) -> dict:
    configuration = str(source_scope["referenced_configuration"])
    face_values = (
        [0.7, 0.7, 0.7, 1.0, 1.0, 0.5, 0.3125, 0.0, 0.0],
        [0.15, 0.35, 0.75, 1.0, 1.0, 0.5, 0.3125, 0.0, 0.0],
    )
    bodies = {}
    for face in face_inventory:
        body_key = (face["body_type"], face["body_index"])
        body = bodies.setdefault(
            body_key,
            {
                "component": component,
                "enumeration_api": "IComponent2.GetBodies2",
                "body_type": body_key[0],
                "body_index": body_key[1],
                "body_name": "fixture-solid",
                "appearance": _fixture_appearance_owner(configuration),
                "faces": [],
            },
        )
        body["faces"].append(
            {
                "face_index": face["face_index"],
                "appearance": {
                    **_fixture_appearance_owner(
                        configuration,
                        face_values[face["face_index"] % 2],
                    ),
                    "native_identity": {
                        "persistent_reference": [
                            1,
                            2,
                            face["face_index"] + 3,
                        ],
                        "conversion": {
                            "component": component,
                            "input_context": {
                                "owner_scope": "assembly_occurrence",
                                "object_kind": "assembly_occurrence",
                                "source_api": (
                                    "IComponent2.GetBodies2/"
                                    "IBody2.GetFaces"
                                ),
                                "traversal_api": (
                                    "IConfiguration.GetRootComponent3/"
                                    "IComponent2.GetChildren"
                                ),
                            },
                            "receiver": {
                                "owner_scope": "component_document",
                                "owner_document_path": source_scope[
                                    "source_document_path"
                                ],
                                "interface": "IModelDocExtension",
                                "object_source": (
                                    "captured_component_source_document"
                                ),
                            },
                            "api": (
                                "IModelDocExtension."
                                "GetCorrespondingEntity2"
                            ),
                            "output_interface_kind": "IFace2",
                            "output_domain": {
                                "proof_api": (
                                    "IModelDocExtension."
                                    "GetObjectByPersistReference3"
                                ),
                                "status": 0,
                                "interface_kind": "IFace2",
                                "expected_owner_document_path": (
                                    source_scope["source_document_path"]
                                ),
                            },
                            "state": "observed",
                            "read_errors": {},
                        },
                        "read_errors": {},
                        "feature": {
                            "name": "fixture-solid",
                            "type_name": "Fixture",
                            "persistent_reference": [4, 5, 6],
                            "conversion": {
                                "component": component,
                                "input_context": {
                                    "owner_scope": "assembly_occurrence",
                                    "object_kind": "assembly_occurrence",
                                    "source_api": (
                                        "IComponent2.GetBodies2/"
                                        "IBody2.GetFaces"
                                    ),
                                    "traversal_api": (
                                        "IConfiguration."
                                        "GetRootComponent3/"
                                        "IComponent2.GetChildren"
                                    ),
                                },
                                "receiver": {
                                    "owner_scope": "component_document",
                                    "owner_document_path": source_scope[
                                        "source_document_path"
                                    ],
                                    "interface": "IModelDocExtension",
                                    "object_source": (
                                        "captured_component_source_document"
                                    ),
                                },
                                "api": (
                                    "IFace2.GetFeature+"
                                    "IModelDocExtension."
                                    "GetPersistReference3"
                                ),
                                "output_interface_kind": "IFeature",
                                "output_domain": {
                                    "proof_api": (
                                        "IModelDocExtension."
                                        "GetObjectByPersistReference3"
                                    ),
                                    "status": 0,
                                    "interface_kind": "IFeature",
                                    "expected_owner_document_path": (
                                        source_scope[
                                            "source_document_path"
                                        ]
                                    ),
                                },
                                "state": "observed",
                                "read_errors": {},
                            },
                            "read_errors": {},
                        },
                    },
                },
            }
        )
    return {
        "component": component,
        "source_document_path": source_scope["source_document_path"],
        "referenced_configuration": configuration,
        "native_active_configuration": configuration,
        "native_active_configuration_error": None,
        "native_referenced_display_state": "Default_Display_State",
        "native_referenced_display_state_error": None,
        "document": _fixture_appearance_owner(configuration),
        "occurrence": _fixture_appearance_owner(configuration),
        "bodies": [bodies[key] for key in sorted(bodies)],
        "state_render_materials": [
            {
                "owner_document_path": "fixture-top.SLDASM",
                "owner_scope": "top_assembly",
                "api": "IModelDocExtension.GetRenderMaterials2",
                "display_state_option": 3,
                "configuration": "Top Configuration",
                "display_state": "Top Display State",
                "display_states": [
                    "Top Display State",
                    "Alternate Top Display State",
                ],
                "display_states_state": "observed",
                "native_render_material_count_api": (
                    "IModelDocExtension.GetRenderMaterialsCount2"
                ),
                "native_render_material_count": 0,
                "native_render_material_count_error": None,
                "call_state": "empty",
                "materials": [],
                "mapping_errors": [],
                "native_reference_index": [],
                "read_errors": {},
            },
            {
                "owner_document_path": "fixture-parent.SLDASM",
                "owner_scope": "parent_assembly",
                "api": "IModelDocExtension.GetRenderMaterials2",
                "display_state_option": 3,
                "configuration": "Parent Configuration",
                "display_state": "Parent Display State",
                "display_states": None,
                "display_states_state": "not_applicable",
                "native_render_material_count_api": (
                    "IModelDocExtension.GetRenderMaterialsCount2"
                ),
                "native_render_material_count": 0,
                "native_render_material_count_error": None,
                "call_state": "empty",
                "materials": [],
                "mapping_errors": [],
                "native_reference_index": [],
                "read_errors": {},
            },
            {
                "owner_document_path": source_scope["source_document_path"],
                "owner_scope": "component_document",
                "api": "IModelDocExtension.GetRenderMaterials2",
                "display_state_option": 3,
                "configuration": configuration,
                "display_state": "Default_Display_State",
                "display_states": None,
                "display_states_state": "not_applicable",
                "native_render_material_count_api": (
                    "IModelDocExtension.GetRenderMaterialsCount2"
                ),
                "native_render_material_count": 0,
                "native_render_material_count_error": None,
                "call_state": "empty",
                "materials": [],
                "mapping_errors": [],
                "native_reference_index": [],
                "read_errors": {},
            },
        ],
    }


class QualifiedFixtureBackend(AxisFixtureBackend):
    """Prepared one-session fixture with hermetic native environment metadata.

    The backend emits the CAD evidence class required by native transfer, but
    it has no native appearance facts. Tests that run past transfer must isolate
    appearance qualification and must not claim a native appearance positive.
    """

    name = "solidworks"
    evidence_class = "cad"

    def environment(self) -> dict:
        return {
            **super().environment(),
            "revision": "34.0.0",
            "build": "hermetic-test",
            "license": 1,
            "visible": True,
        }

    def capture_prepared_geometry(self, source_scopes, on_activity=None):
        rows = super().capture_prepared_geometry(
            source_scopes,
            on_activity=on_activity,
        )
        for component, row in rows.items():
            row["appearance_raw"] = _fixture_appearance_raw(
                component,
                source_scopes[component],
                row["face_inventory"],
            )
        return rows


class HermeticNativeFixture:
    def __init__(self) -> None:
        self._assertions = unittest.TestCase()
        self._cleanups = []

    def addCleanup(self, function, /, *arguments, **keywords) -> None:
        self._cleanups.append((function, arguments, keywords))

    def doCleanups(self) -> None:
        while self._cleanups:
            function, arguments, keywords = self._cleanups.pop()
            function(*arguments, **keywords)

    def assertEqual(self, first, second, message=None) -> None:
        self._assertions.assertEqual(first, second, message)

    def assertIs(self, first, second, message=None) -> None:
        self._assertions.assertIs(first, second, message)

    def assertTrue(self, expression, message=None) -> None:
        self._assertions.assertTrue(expression, message)

    def assertIn(self, member, container, message=None) -> None:
        self._assertions.assertIn(member, container, message)

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="hermetic-six-stage-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.handoff = self.root / "handoff"
        for name in (MAIN_ASSEMBLY, "cad/base.SLDPRT", "cad/arm.SLDPRT"):
            path = self.handoff / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(f"placeholder {name}\n".encode())
        self.records = self.root / "records"
        limits = self.records / "joints/arm.json"
        limits.parent.mkdir(parents=True)
        limits.write_text(
            json.dumps({"limits": {"lower": -1.5, "upper": 1.5}, "drive": {"effort": 6.0, "velocity": 2.0}}),
            encoding="utf-8",
        )
        (self.records / "budget.json").write_text(
            json.dumps({"robot": {"expected_mass_kg": [0.28, 0.32], "expected_extent_m": [0.35, 0.5]}}),
            encoding="utf-8",
        )
        self.events: list[dict] = []

    def _record(self, event: dict) -> None:
        self.events.append({"at": datetime.now(UTC).isoformat(), **event})

    def _prepare_and_capture(self) -> tuple[Path, dict]:
        handoff_digest = digest(package_inventory(self.handoff))
        frozen = steps.freeze_inputs(
            self.handoff,
            handoff_digest,
            package_inventory(self.handoff),
            main_assembly=MAIN_ASSEMBLY,
            on_event=self._record,
        )
        prepared, _target, discovery, prepared_files = steps.discover_structure(
            self.handoff,
            self.root / "prepared",
            RUN_ID,
            expected_digest=handoff_digest,
            expected_files=package_inventory(self.handoff),
            main_assembly=MAIN_ASSEMBLY,
            configuration={"record_roots": [str(self.records)], "purpose": PURPOSE_ENGINEERING},
            targets={
                "nd_fixture": {
                    "repository_slug": "mimicverse/description",
                    "base": BASE_BRANCH,
                }
            },
            preparer=lambda *args, **kwargs: prepare_native_package(
                *args,
                backend=FakeBackend(capture_record()),
                **kwargs,
            ),
            on_event=self._record,
        )
        components = [
            {
                "name": "base-1",
                "path": str(prepared / "cad/base.SLDPRT"),
                "transform": support.placement(),
                "mass": _mass_reading(0.192, (0.03, 0.02, 0.01), prepared / "cad/base.SLDPRT"),
            },
            {
                "name": "arm-1",
                "path": str(prepared / "cad/arm.SLDPRT"),
                "transform": support.placement((0.0, 0.0, 0.1)),
                "mass": _mass_reading(0.105654866776462, (0.0, 0.0, 0.05), prepared / "cad/arm.SLDPRT"),
            },
        ]
        backend = QualifiedFixtureBackend(
            prepared / MAIN_ASSEMBLY,
            components,
            dependencies=[prepared / "cad/base.SLDPRT", prepared / "cad/arm.SLDPRT"],
            coordinate_systems={
                "CS_base_link": support.placement(),
                "CS_arm_link": support.placement((0.0, 0.0, 0.1)),
            },
        )
        native_identity = _native_tool()
        producer_tool_sha256 = digest(native_identity)
        upstream_artifacts = tuple(
            ArtifactRef(
                artifact_id=f"fixture-{stage}-artifact",
                kind=f"stage_output:{stage}",
                locator=f"cas://artifact/fixture-{stage}-artifact",
                manifest_sha256=digest({"manifest": payload}),
                set_sha256=digest({stage: payload}),
                producer_run_id=RUN_ID,
                producer_stage=stage,
                producer_tool_sha256=producer_tool_sha256,
            ).to_record()
            for stage, payload in (
                ("freeze", frozen),
                (
                    "discover",
                    {
                        "discovery_sha256": discovery.discovery_sha256,
                        "prepared_sha256": discovery.prepared_sha256,
                        "revision": discovery.revision,
                    },
                ),
            )
        )
        execution = {
            "claim_id": "fixture-capture-claim",
            "command_id": "fixture-capture-command",
            "effect_key": "native",
            "executor_sha256": digest({"executor": "capture"}),
            "fence": 1,
            "native_tool_sha256": digest(native_identity),
            "owner_sha256": digest({"owner": "hermetic-capture"}),
            "run_id": RUN_ID,
        }
        with patch("description_pipeline.runtime.tool_record", return_value=native_identity):
            from description_pipeline.pipeline.capture import (
                _capture_native_transaction,
            )

            capture = _capture_native_transaction(
                prepared,
                self.root / "capture",
                backend=backend,
                run_id=RUN_ID,
                handoff_sha256=handoff_digest,
                upload_sha256=handoff_digest,
                main_assembly=MAIN_ASSEMBLY,
                expected_inputs=prepared_files,
                on_event=self._record,
                native_tool_factory=lambda: dict(native_identity),
                upstream_artifacts=upstream_artifacts,
                execution=execution,
            )
        self.assertEqual(capture["state"], "native_complete", capture.get("error"))
        request = build_request_evidence(
            purpose=PURPOSE_ENGINEERING,
            request_id="hermetic-six-stage",
            target_hardware="nd_fixture",
            change_summary="Hermetic six-stage delivery",
            main_assembly=MAIN_ASSEMBLY,
            handoff_digest=handoff_digest,
            platform_config_digest="c" * 64,
        )
        request_path = self.root / "capture/input/request.json"
        request_path.parent.mkdir(parents=True, exist_ok=True)
        request_path.write_bytes(request_evidence_bytes(request))
        return self.root / "capture", {"freeze": frozen, "capture": capture}

    def _repository(self) -> Path:
        remote = self.root / "remote.git"
        seed = self.root / "seed"
        _git("init", "--bare", "-b", BASE_BRANCH, str(remote))
        seed.mkdir()
        _git("init", "-b", BASE_BRANCH, cwd=seed)
        for key, value in (
            ("user.email", "hermetic@example.com"),
            ("user.name", "Hermetic Test"),
            ("core.autocrlf", "false"),
        ):
            _git("config", key, value, cwd=seed)
        (seed / "README.md").write_text("base\n", encoding="utf-8")
        _git("add", "README.md", cwd=seed)
        _git("commit", "-m", "base", cwd=seed)
        _git("remote", "add", "origin", str(remote), cwd=seed)
        _git("push", "-u", "origin", BASE_BRANCH, cwd=seed)
        repository = self.root / "repository"
        _git("clone", "-c", "core.autocrlf=false", str(remote), str(repository))
        _git("config", "user.email", "hermetic@example.com", cwd=repository)
        _git("config", "user.name", "Hermetic Test", cwd=repository)
        return repository

    def _generate_and_verify(self) -> tuple[dict, dict, dict]:
        original_handoff = _tree_bytes(self.handoff)
        capture, native = self._prepare_and_capture()
        self.assertEqual(native["freeze"]["handoff_sha256"], native["capture"]["handoff_sha256"])
        self.assertEqual(native["freeze"]["handoff_sha256"], digest(package_inventory(self.handoff)))

        portable_identity = _portable_tool()
        with patch.object(steps, "tool_record", return_value=portable_identity):
            generated = solidworks.generate(
                capture,
                self.root / "generate",
                run_id=RUN_ID,
                handoff_sha256=native["capture"]["handoff_sha256"],
                prior_events=self.events,
                on_event=self._record,
            )
        self.assertEqual(generated["state"], "generated", generated.get("error"))
        generated_subject = generated["subject_sha256"]
        self.assertEqual(read_delivery_manifest(self.root / "generate")["subject"]["digest"], generated_subject)

        with patch.object(steps, "tool_record", return_value=portable_identity):
            verified = solidworks.verify(
                self.root / "generate",
                self.root / "verify",
                run_id=RUN_ID,
                expected_subject=generated_subject,
                handoff_sha256=native["capture"]["handoff_sha256"],
                prior_events=self.events,
                on_event=self._record,
            )
        self.assertEqual(verified["state"], "verified", verified.get("error"))
        self.assertIs(verified["passed"], True)
        self.assertEqual(verified["subject_sha256"], generated_subject)
        self.assertEqual(read_delivery_manifest(self.root / "verify")["subject"]["digest"], generated_subject)
        quality = json.loads((self.root / "verify/reports/quality.json").read_text(encoding="utf-8"))
        metadata_diagnostics = [
            diagnostic
            for diagnostic in quality["diagnostics"]
            if diagnostic.get("code") == "metadata.non_native"
        ]
        self.assertEqual(len(metadata_diagnostics), 1)
        self.assertTrue(
            all(
                row.get("fields")
                and row.get("file")
                and row.get("sha256")
                and row.get("reference")
                for row in metadata_diagnostics[0]["detail"]["objects"]
            )
        )
        urdf = (self.root / "verify/urdf/robot.urdf").read_text(encoding="utf-8")
        self.assertIn("WARNING metadata.non_native", urdf)
        return original_handoff, native, verified

    def test_published_subject_supports_self_contained_cli_check(self) -> None:
        from description_pipeline.verification.solidworks_urdf import check_bundle

        _original_handoff, _native, verified = self._generate_and_verify()
        published = self.root / "self-contained"
        for name in ("input", "evidence", "model", "urdf", "meshes"):
            shutil.copytree(self.root / "verify" / name, published / name)
        shutil.copy2(self.root / "verify/tool.json", published / "tool.json")
        shutil.copy2(self.root / "verify/manifest.json", published / "manifest.json")
        (published / "reports").mkdir()
        shutil.copy2(
            self.root / "verify/reports/quality.json",
            published / "reports/quality.json",
        )

        report = check_bundle(published)
        self.assertIs(report["passed"], True, report.get("checks"))
        self.assertEqual(report["subject_sha256"], verified["subject_sha256"])

        shutil.copy2(
            self.root / "verify/reports/native-stages.json",
            published / "reports/native-stages.json",
        )
        partial_receipts = check_bundle(published)
        self.assertIs(partial_receipts["passed"], False)
        (published / "reports/native-stages.json").unlink()

        with_required_receipts = check_bundle(
            published,
            require_native_receipts=True,
        )
        self.assertIs(with_required_receipts["passed"], False)

    def test_verify_rereads_the_subject_and_native_receipts_before_success(self) -> None:
        from description_pipeline.verification import solidworks_urdf as quality

        _original_handoff, native, _verified = self._generate_and_verify()
        seed = self.root / "generate"
        expected_subject = read_delivery_manifest(seed)["subject"]["digest"]
        original_check = quality.check_bundle

        def check_then_append_to_tool(*args, **kwargs):
            report = original_check(*args, **kwargs)
            tool = Path(args[0]) / "tool.json"
            tool.write_bytes(tool.read_bytes() + b"\n")
            return report

        with patch.object(quality, "check_bundle", side_effect=check_then_append_to_tool):
            receipt = solidworks.verify(
                seed,
                self.root / "verify-race",
                run_id=RUN_ID,
                expected_subject=expected_subject,
                handoff_sha256=native["capture"]["handoff_sha256"],
                prior_events=self.events,
            )
        self.assertEqual(receipt["state"], "failed", receipt)
        self.assertIn("changed after the final report was replayed", receipt["error"])

    def _capture_with_environment_mutation(self, mutate) -> tuple[Path, dict]:
        from description_pipeline.sources.solidworks import freeze as freeze_module

        write_json = freeze_module.write_json

        def injected_write(path, payload):
            if Path(path).name == "environment.json":
                mutate(payload)
            write_json(path, payload)

        with patch.object(freeze_module, "write_json", side_effect=injected_write):
            return self._prepare_and_capture()

    def _assert_capture_mutation_rejected(self, mutate, expected_error: str) -> None:
        capture, native = self._capture_with_environment_mutation(mutate)
        generated = solidworks.generate(
            capture,
            self.root / "generate",
            run_id=RUN_ID,
            handoff_sha256=native["capture"]["handoff_sha256"],
            prior_events=self.events,
            on_event=self._record,
        )
        self.assertEqual(generated["state"], "failed")
        self.assertIn(expected_error, generated["error"])

    def test_sealed_environment_active_configuration_must_match_frozen_selection(self) -> None:
        def mutate(environment):
            boundary = environment["session_boundary"]
            boundary["observed_configuration"] = "Other"
            boundary["capture_state"]["active_configuration"] = "Other"
            boundary["capture_state"]["configurations"] = ["Other"]

        self._assert_capture_mutation_rejected(
            mutate,
            "capture_state.active_configuration differs from the frozen source configuration",
        )

    def test_sealed_environment_paths_must_match_the_dependency_closure(self) -> None:
        def mutate(environment):
            boundary = environment["session_boundary"]
            other = str(Path(boundary["opened"]["path"]).with_name("other.SLDASM"))
            boundary["opened"]["path"] = other
            boundary["capture_state"]["path"] = other

        self._assert_capture_mutation_rejected(
            mutate,
            "opened and captured paths do not match the dependency closure",
        )

    def test_sealed_environment_open_errors_must_be_zero(self) -> None:
        def mutate(environment):
            environment["session_boundary"]["opened"]["errors"] = 1

        self._assert_capture_mutation_rejected(
            mutate,
            "OpenDoc6 reported a failed or invalid open",
        )

if __name__ == "__main__":
    unittest.main()
