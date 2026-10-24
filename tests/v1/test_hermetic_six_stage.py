"""Hermetic six-stage happy path: frozen evidence through a local Git publication.

The native-shaped backend exercises the production path; it is not physical CAD
qualification or a claim about a licensed SolidWorks installation.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import numpy as np

from description_pipeline import solidworks, steps
from description_pipeline.delivery import read_delivery_manifest
from description_pipeline.interface_records import PURPOSE_ENGINEERING
from description_pipeline.io import digest
from description_pipeline.repository import urdf_pr
from description_pipeline.request_evidence import build_request_evidence, request_evidence_bytes
from description_pipeline.runtime import tool_record
from description_pipeline.sources.solidworks.discovery import prepare_native_package
from description_pipeline.sources.solidworks.native import normalize_document_path
from description_pipeline.sources.solidworks.revision import package_inventory
from description_pipeline.sources.solidworks.scene import combine_mass_properties
from description_pipeline.stages import STAGE_IDS

from tests.sources import support
from tests.sources.test_solidworks_discovery import FakeBackend
from tests.v1.test_split_roundtrip import AxisFixtureBackend, capture_record
from tests.v1.test_stage_transfer import native_tool

RUN_ID = "3f2c1f7a-9d4e-4d0a-9f11-2b6c5a7e8d90"
MAIN_ASSEMBLY = "cad/robot.SLDASM"
REVIEW_BRANCH = "work/solidworks/nd_fixture/engineering"
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


class QualifiedFixtureBackend(AxisFixtureBackend):
    """A hermetic native-shaped backend with complete, internally consistent evidence."""

    name = "solidworks"
    evidence_class = "cad"

    def environment(self) -> dict:
        if not hasattr(self, "_hermetic_source_pid"):
            self._hermetic_source_pid = max(1, self.session_pid - 1)
        return {
            "revision": "34.0.0",
            "input_mode": "saved_documents",
            "sessions": {
                "source": {
                    "pid": self._hermetic_source_pid,
                    "state": "closed",
                },
                "copy": {
                    "pid": self.session_pid,
                    "state": "active",
                },
            },
            "build": "hermetic-test",
            "license": 1,
            "visible": True,
        }

    def retire_session(self, role: str = "copy") -> dict:
        order = getattr(self, "order", None)
        if order is None:
            order = self.order = []
        order.append("retire_session")
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

    def assembly_mass_properties(self, path: str) -> dict:
        entries = []
        for component in self.components:
            reading = component["mass"]
            transform = np.asarray(component["transform"], dtype=float).reshape(4, 4)
            rotation = transform[:3, :3]
            entries.append(
                {
                    "mass": reading["mass"],
                    "com": rotation @ np.asarray(reading["com"], dtype=float) + transform[:3, 3],
                    "inertia": rotation @ np.asarray(reading["inertia"], dtype=float) @ rotation.T,
                }
            )
        combined = combine_mass_properties(entries)
        return {
            "mass": combined["mass"],
            "com": combined["com"],
            "inertia": combined["inertia"],
            "reference": {
                "used_api": "IMassProperty2.GetMomentOfInertia(0)",
                "product_convention": "solidworks_standard",
                "scope": "assembly_document",
                "axes": "assembly_document_axes",
                "reference_point": "center_of_mass",
                "use_system_units": True,
                "configuration": "Default",
                "overrides": dict(OVERRIDES),
            },
        }

    def assembly_component_mass_properties(self, path: str) -> dict:
        return {
            "assembly": path,
            "configuration": "Default",
            "instances": [
                {
                    "name": component["name"],
                    "parent": None,
                    "depth": 0,
                    "document": str(component["path"]),
                    "document_type": "part",
                    "context_mass_kg": component["mass"]["mass"],
                    "context_volume_m3": component["mass"]["reference"]["volume_m3"],
                    "overrides": dict(OVERRIDES),
                    "effective": None,
                    "effective_error": None,
                }
                for component in self.components
            ],
            "errors": [],
            "reference": {"method": "IMassProperty2 (SelectedItems = component instance)"},
        }

    def collect_scene(self, doc_path, coordinate_systems, on_activity=None, require_material=True):
        scene = super().collect_scene(
            doc_path,
            coordinate_systems,
            on_activity=on_activity,
            require_material=require_material,
        )
        self._capture_component_paths = [
            (str(component.name), str(component.path))
            for component in scene.components
        ]
        configuration = "Default"
        state = {
            "save_flag": False,
            "needs_rebuild2_value": 0,
            "read_only": True,
            "configuration": configuration,
            "configuration_needs_rebuild": False,
            "path": str(doc_path),
            "configurations": [
                {
                    "name": configuration,
                    "needs_rebuild": False,
                    "add_rebuild_save_mark": False,
                }
            ],
            "controls": {
                "enable_assembly_rebuild": False,
                "lightweight_component_count": 0,
            },
        }
        source_files = {}
        for path in [doc_path, *(component.path for component in scene.components)]:
            key = normalize_document_path(os.path.abspath(path))
            source_files.setdefault(
                key,
                {
                    "path": str(path),
                    "sha256": hashlib.sha256(Path(path).read_bytes()).hexdigest(),
                },
            )
        topology = [
            {
                "instance": "top",
                "depth": 0,
                "document_path": str(doc_path),
                "normalized_path": normalize_document_path(os.path.abspath(doc_path)),
                "document_type": 2,
                "referenced_configuration": configuration,
                "suppression_state": None,
                "storage_scope": "file",
                "provenance_state": None,
                "owner_document_path": None,
                "owner_sha256": None,
                "native_api_pid": None,
            },
            *[
                {
                    "instance": component.name,
                    "depth": 1,
                    "document_path": component.path,
                    "normalized_path": normalize_document_path(os.path.abspath(component.path)),
                    "document_type": 1,
                    "referenced_configuration": configuration,
                    "suppression_state": 2,
                    "storage_scope": "file",
                    "provenance_state": None,
                    "owner_document_path": None,
                    "owner_sha256": None,
                    "native_api_pid": None,
                }
                for component in scene.components
            ],
        ]
        topology.sort(
            key=lambda row: tuple(
                "" if row.get(name) is None else str(row.get(name))
                for name in (
                    "instance",
                    "normalized_path",
                    "document_type",
                    "referenced_configuration",
                    "depth",
                    "suppression_state",
                    "storage_scope",
                    "provenance_state",
                )
            )
        )
        top_sha256 = source_files[normalize_document_path(os.path.abspath(doc_path))]["sha256"]
        loaded_types = {}
        for row in topology:
            if row["storage_scope"] == "suppressed":
                continue
            path = normalize_document_path(os.path.abspath(row["document_path"]))
            prior = loaded_types.setdefault(path, row["document_type"])
            if prior != row["document_type"]:
                raise AssertionError("fixture loaded document type changed")
        loaded_documents = {
            "file_paths": sorted(loaded_types),
            "session_paths": [],
            "document_types": [
                {"path": path, "document_type": document_type}
                for path, document_type in sorted(loaded_types.items())
            ],
        }
        scene.notes["capture_preparation"] = {
            "session": {"role": "copy", "identity": self.environment()["sessions"]["copy"], "temp_root": None},
            "schema_version": "solidworks-to-urdf.capture-preparation/v2",
            "used_api": "IModelDocExtension.NeedsRebuild2/IConfiguration.NeedsRebuild",
            "scope": "collected_copy_in_memory",
            "document": str(doc_path),
            "configuration": configuration,
            "read_only": True,
            "saved_to_disk": False,
            "rebuild_performed": False,
            "component_count": len(scene.components),
            "assembly_document_count": 1,
            "child_rebuilds": [],
            "top_rebuild": None,
            "rebuild_calls": [
                {
                    "kind": "initial",
                    "initial_pending": [],
                    "rebuild_candidates": [],
                }
            ],
            "needs_rebuild2_before": 0,
            "needs_rebuild2_after": 0,
            "configuration_needs_rebuild_before": False,
            "configuration_needs_rebuild_after": False,
            "source_sha256_before": top_sha256,
            "source_sha256_after": top_sha256,
            "save_flag_before": False,
            "save_flag_after": False,
            "state_before": dict(state),
            "state_after_children": dict(state),
            "state_after": dict(state),
            "assembly_states_before": [
                {
                    "depth": 0,
                    "path": str(doc_path),
                    "instances": ["top"],
                    "state": dict(state),
                }
            ],
            "assembly_states_after": [
                {
                    "depth": 0,
                    "path": str(doc_path),
                    "instances": ["top"],
                    "state": dict(state),
                }
            ],
            "part_states_after": [
                {
                    "path": str(component.path),
                    "instances": [component.name],
                    "state": {
                        "save_flag": False,
                        "needs_rebuild2_value": 0,
                        "read_only": False,
                        "configuration": configuration,
                        "configuration_needs_rebuild": False,
                        "path": str(component.path),
                        "configurations": [
                            {
                                "name": configuration,
                                "needs_rebuild": False,
                                "add_rebuild_save_mark": False,
                            }
                        ],
                    },
                }
                for component in scene.components
            ],
            "source_files_before": source_files,
            "source_files_after": dict(source_files),
            "session_dependency_paths": [],
            "topology_before": topology,
            "topology_after": [dict(row) for row in topology],
            "loaded_documents_before": copy.deepcopy(loaded_documents),
            "loaded_documents_after": copy.deepcopy(loaded_documents),
            "session_recovery": None,
        }
        return scene

    def verify_capture_documents_ready(self) -> dict:
        top_path = str(self._capture_document)
        documents = [
            {
                "path": top_path,
                "document_type": 2,
                "state": {
                    "save_flag": False,
                    "needs_rebuild2_value": 0,
                    "read_only": True,
                    "configuration": "Default",
                    "configuration_needs_rebuild": False,
                    "path": top_path,
                    "configurations": [
                        {
                            "name": "Default",
                            "needs_rebuild": False,
                            "add_rebuild_save_mark": False,
                        }
                    ],
                    "controls": {
                        "enable_assembly_rebuild": False,
                        "lightweight_component_count": 0,
                    },
                },
            },
            *[
                {
                    "path": component_path,
                    "document_type": 1,
                    "state": {
                        "save_flag": False,
                        "needs_rebuild2_value": 0,
                        "read_only": False,
                        "configuration": "Default",
                        "configuration_needs_rebuild": False,
                        "path": component_path,
                        "configurations": [
                            {
                                "name": "Default",
                                "needs_rebuild": False,
                                "add_rebuild_save_mark": False,
                            }
                        ],
                    },
                }
                for _component_name, component_path in self._capture_component_paths
            ],
        ]
        return {
            "document_count": len(documents),
            "session": {"role": "copy", "identity": self.environment()["sessions"]["copy"], "temp_root": None},
            "parts": sum(item["document_type"] == 1 for item in documents),
            "assemblies": sum(item["document_type"] == 2 for item in documents),
            "documents": documents,
            "topology": [
                {
                    "instance": "top",
                    "depth": 0,
                    "document_path": top_path,
                    "normalized_path": normalize_document_path(os.path.abspath(top_path)),
                    "document_type": 2,
                    "referenced_configuration": "Default",
                    "suppression_state": None,
                    "storage_scope": "file",
                    "provenance_state": None,
                    "owner_document_path": None,
                    "owner_sha256": None,
                    "native_api_pid": None,
                },
                *[
                    {
                        "instance": component_name,
                        "depth": 1,
                        "document_path": component_path,
                        "normalized_path": normalize_document_path(os.path.abspath(component_path)),
                        "document_type": 1,
                        "referenced_configuration": "Default",
                        "suppression_state": 2,
                        "storage_scope": "file",
                        "provenance_state": None,
                        "owner_document_path": None,
                        "owner_sha256": None,
                        "native_api_pid": None,
                    }
                    for component_name, component_path in self._capture_component_paths
                ],
            ],
        }

    def export_component_meshes(self, destinations: dict[str, str], on_activity=None) -> dict[str, dict]:
        entries = {}
        for component, destination in destinations.items():
            face_edges, native_edges = support.cube_native_edges(half=0.199999)
            triangles, _attributes = support.cube_triangles(half=0.199999)
            triangles = [(first, third, second) for first, second, third in triangles]
            spans = []
            start = 0
            for face_index in range(6):
                spans.append(
                    {
                        "body_type": 0,
                        "body_index": 0,
                        "body_name": "fixture-solid",
                        "body_reference_sha256": hashlib.sha256(b"fixture-body").hexdigest(),
                        "face_index": face_index,
                        "face_reference_sha256": hashlib.sha256(f"fixture-face-{face_index}".encode()).hexdigest(),
                        "face_owner_id": face_index + 1,
                        "edge_references": face_edges[face_index],
                        "triangle_start": start,
                        "triangle_count": 2,
                        "triangle_records_sha256": "0" * 64,
                    }
                )
                start += 2
            tessellation = support.native_tessellation_fixture(
                spans,
                native_edges,
                triangle_vertices=triangles,
            )
            Path(destination).write_bytes(tessellation.stl_bytes)
            self.exported.append(str(destination))
            entries[component] = {
                "component": component,
                "written": destination,
                "used_api": "IBody2.GetTessellation",
                "triangles": len(triangles),
                "bodies": {"solid": 1, "sheet": 0},
                "tessellation": tessellation,
            }
        return entries


class HermeticSixStageTests(unittest.TestCase):
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
        prepared, _target, _discovery, prepared_files = steps.discover_structure(
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
        with patch("description_pipeline.runtime.tool_record", return_value=native_identity):
            capture = solidworks.capture(
                prepared,
                self.root / "capture",
                backend=backend,
                run_id=RUN_ID,
                handoff_sha256=handoff_digest,
                upload_sha256=handoff_digest,
                main_assembly=MAIN_ASSEMBLY,
                expected_inputs=prepared_files,
                prior_events=self.events,
                on_event=self._record,
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

    def _github(self, repository: Path, *args: str) -> str:
        if args[:2] == ("pr", "list"):
            return "[]"
        if args[:2] == ("pr", "create"):
            return "https://github.com/example/nd_fixture/pull/1"
        if args[:2] == ("pr", "view"):
            head = _git("ls-remote", str(self.root / "remote.git"), f"refs/heads/{REVIEW_BRANCH}").split()[0]
            return json.dumps(
                {
                    "url": "https://github.com/example/nd_fixture/pull/1",
                    "state": "OPEN",
                    "baseRefName": BASE_BRANCH,
                    "headRefName": REVIEW_BRANCH,
                    "headRefOid": head,
                    "isCrossRepository": False,
                }
            )
        raise AssertionError(f"unexpected GitHub command: {args}")

    def _github_failure(self, repository: Path, *args: str) -> str:
        if args[:2] == ("pr", "list"):
            return "[]"
        if args[:2] == ("pr", "create"):
            raise subprocess.CalledProcessError(1, ["gh", *args], stderr="GitHub submission unavailable")
        raise AssertionError(f"unexpected GitHub command: {args}")

    def _assert_failed_publish_evidence(self, failed: dict, subject: str) -> Path:
        output = Path(failed["output"])
        self.assertEqual(read_delivery_manifest(output)["subject"]["digest"], subject)
        for relative in (
            "transfer-manifest.json",
            "reports/native-tool.json",
            "reports/native-stages.json",
            "reports/stages.json",
            "reports/structural-team.json",
            "reports/structural-team.md",
            "reports/quality.json",
        ):
            self.assertTrue((output / relative).is_file(), relative)
        self.assertEqual(failed["stage_report"]["path"], "reports/stages.json")
        self.assertEqual(
            failed["stage_report"]["sha256"],
            hashlib.sha256((output / "reports/stages.json").read_bytes()).hexdigest(),
        )
        return output

    def _portable_publish_failure(self, receipt: dict) -> BaseException:
        from tests.v1.test_failure_boundary_fields import _module

        module = _module()
        with self.assertRaises(module.AirflowFailException) as caught:
            module._portable_summary(receipt, stage="publish")
        return caught.exception

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

    def test_frozen_evidence_passes_real_generate_verify_and_local_git_publish(self) -> None:
        original_handoff, native, verified = self._generate_and_verify()
        generated_subject = verified["subject_sha256"]
        verified_bytes = _tree_bytes(self.root / "verify")

        release = {"schema_version": "solidworks-to-urdf.release/v1", "version": solidworks.__version__}
        tool = {**tool_record("portable"), "release": release}
        with (
            patch.object(steps, "tool_record", return_value=tool),
            patch.object(urdf_pr, "_origin_slug", return_value="example/nd_fixture"),
            patch.object(urdf_pr, "_gh", side_effect=self._github),
        ):
            published = solidworks.publish(
                self.root / "verify",
                self.root / "publish",
                run_id=RUN_ID,
                expected_subject=generated_subject,
                repository=self._repository(),
                base=BASE_BRANCH,
                handoff_sha256=native["capture"]["handoff_sha256"],
                prior_events=self.events,
                on_event=self._record,
            )

        self.assertEqual(published["state"], "published", published.get("error"))
        self.assertIs(published["passed"], True)
        self.assertEqual(published["subject_sha256"], generated_subject)
        self.assertEqual(published["submission"]["subject"], generated_subject)
        self.assertNotIn("error", published)
        self.assertNotIn("error_code", published)
        self.assertNotIn("detail", published)
        self.assertEqual(read_delivery_manifest(self.root / "publish")["subject"]["digest"], generated_subject)
        self.assertEqual(
            _git("ls-remote", str(self.root / "remote.git"), f"refs/heads/{REVIEW_BRANCH}").split()[0],
            published["submission"]["commit"],
        )
        for relative in (
            "transfer-manifest.json",
            "reports/native-tool.json",
            "reports/native-stages.json",
            "reports/stages.json",
            "reports/structural-team.json",
            "reports/structural-team.md",
            "reports/quality.json",
        ):
            self.assertTrue((self.root / "publish" / relative).is_file(), relative)
        committed = set(
            _git("ls-tree", "-r", "--name-only", published["submission"]["commit"], cwd=self.root / "repository")
            .strip()
            .splitlines()
        )
        self.assertIn("reports/quality.json", committed)
        self.assertFalse(
            {
                "transfer-manifest.json",
                "reports/native-tool.json",
                "reports/native-stages.json",
                "reports/stages.json",
                "reports/structural-team.json",
                "reports/structural-team.md",
            }
            & committed
        )
        after_handoff = _tree_bytes(self.handoff)
        self.assertEqual(after_handoff, original_handoff)
        self.assertEqual(_tree_bytes(self.root / "verify"), verified_bytes)
        self.assertEqual({event["stage"] for event in self.events}, set(STAGE_IDS))
        handoff_subjects = {
            event["check"]["details"]["handoff_sha256"]
            for event in self.events
            if isinstance(event.get("check"), dict)
            and isinstance(event["check"].get("details"), dict)
            and event["check"]["details"].get("handoff_sha256")
        }
        self.assertEqual(handoff_subjects, {native["capture"]["handoff_sha256"]})
        stage_subjects = {
            event["check"]["details"]["subject_sha256"]
            for event in self.events
            if isinstance(event.get("check"), dict)
            and isinstance(event["check"].get("details"), dict)
            and event["check"]["details"].get("subject_sha256")
        }
        self.assertEqual(stage_subjects, {generated_subject})

    def test_failed_github_submission_preserves_the_verified_delivery(self) -> None:
        _original_handoff, native, verified = self._generate_and_verify()
        subject = verified["subject_sha256"]
        verified_bytes = _tree_bytes(self.root / "verify")
        release = {"schema_version": "solidworks-to-urdf.release/v1", "version": solidworks.__version__}
        tool = {**tool_record("portable"), "release": release}
        with (
            patch.object(steps, "tool_record", return_value=tool),
            patch.object(urdf_pr, "_origin_slug", return_value="example/nd_fixture"),
            patch.object(urdf_pr, "_gh", side_effect=self._github_failure),
        ):
            failed = solidworks.publish(
                self.root / "verify",
                self.root / "publish",
                run_id=RUN_ID,
                expected_subject=subject,
                repository=self._repository(),
                base=BASE_BRANCH,
                handoff_sha256=native["capture"]["handoff_sha256"],
                prior_events=self.events,
                on_event=self._record,
            )

        self.assertEqual(failed["state"], "unresolved")
        self.assertEqual(failed["error"], "github_failed_after_push")
        self.assertIs(failed["passed"], False)
        self.assertEqual(failed["error"], "github_failed_after_push")
        self.assertEqual(failed["reason"], "github_failed")
        self.assertEqual(failed["error_code"], "github_failed_after_push")
        self.assertEqual(failed["submission"]["error"], "github_failed_after_push")
        self.assertEqual(failed["subject_sha256"], subject)
        self.assertEqual(failed["quality"]["passed"], True)
        self._assert_failed_publish_evidence(failed, subject)
        boundary_failure = self._portable_publish_failure(failed)
        self.assertEqual(getattr(boundary_failure, "code", None), "github_failed_after_push")
        self.assertTrue(
            _git("ls-remote", str(self.root / "remote.git"), f"refs/heads/{REVIEW_BRANCH}").strip(),
            "the local Git push completed before GitHub failed",
        )
        self.assertEqual(_tree_bytes(self.root / "verify"), verified_bytes)

    def test_failed_git_submission_preserves_the_verified_delivery(self) -> None:
        _original_handoff, native, verified = self._generate_and_verify()
        subject = verified["subject_sha256"]
        verified_bytes = _tree_bytes(self.root / "verify")
        repository = self._repository()
        real_git = urdf_pr._git
        stderr = "pre-push submission rejected"

        def git_with_failed_push(target, *args: str, **kwargs):
            if args[:1] == ("push",):
                raise subprocess.CalledProcessError(128, ["git", *args], stderr=stderr)
            return real_git(target, *args, **kwargs)

        release = {"schema_version": "solidworks-to-urdf.release/v1", "version": solidworks.__version__}
        tool = {**tool_record("portable"), "release": release}
        with (
            patch.object(steps, "tool_record", return_value=tool),
            patch.object(urdf_pr, "_origin_slug", return_value="example/nd_fixture"),
            patch.object(urdf_pr, "_gh", side_effect=self._github),
            patch.object(urdf_pr, "_git", side_effect=git_with_failed_push),
        ):
            failed = solidworks.publish(
                self.root / "verify",
                self.root / "publish",
                run_id=RUN_ID,
                expected_subject=subject,
                repository=repository,
                base=BASE_BRANCH,
                handoff_sha256=native["capture"]["handoff_sha256"],
                prior_events=self.events,
                on_event=self._record,
            )

        self.assertEqual(failed["state"], "failed")
        self.assertIs(failed["passed"], False)
        self.assertEqual(failed["error"], "git_failed")
        self.assertEqual(failed["error_code"], "git_failed")
        self.assertEqual(failed["detail"], stderr)
        self.assertEqual(failed["submission"]["error"], "git_failed")
        self.assertEqual(failed["submission"]["detail"], stderr)
        self.assertEqual(failed["subject_sha256"], subject)
        self.assertEqual(failed["quality"]["passed"], True)
        self._assert_failed_publish_evidence(failed, subject)
        boundary_failure = self._portable_publish_failure(failed)
        self.assertEqual(getattr(boundary_failure, "code", None), "git_failed")
        self.assertEqual(getattr(boundary_failure, "detail", None), stderr)
        self.assertEqual(
            _git("ls-remote", str(self.root / "remote.git"), f"refs/heads/{REVIEW_BRANCH}").strip(),
            "",
        )
        self.assertEqual(_tree_bytes(self.root / "verify"), verified_bytes)


if __name__ == "__main__":
    unittest.main()
