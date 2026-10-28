"""The pre-acquisition capture plan: sealed production, consumption and refusals."""

from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from description_pipeline.io import PipelineError, digest, write_json
from description_pipeline.schemas import (
    CAPTURE_EVIDENCE_REQUESTS,
    NATIVE_DISCOVERY_SCHEMA,
)
from description_pipeline.sources.solidworks import discovery
from description_pipeline.sources.solidworks.capture_package import (
    consume_sealed_definition,
)
from description_pipeline.sources.solidworks.capture_transaction import (
    inspect_prepared_package,
)
from tests.sources.selection_support import sealed_selection

MAIN = "cad/robot.SLDASM"

steps = SimpleNamespace(
    _consume_sealed_definition=consume_sealed_definition,
)


def build_package(root: Path) -> Path:
    """One prepared package: sealed discovery, sealed source identity, sealed plan."""

    discovery = {
        "schema_version": NATIVE_DISCOVERY_SCHEMA,
        "run_id": "r-1",
        "derivation": {"main_assembly": MAIN, "configuration": None},
        "identity": {"hardware_id": "m3.0", "revision": "r1"},
        "native_files": {
            MAIN: "a" * 64,
            "cad/base.SLDPRT": "b" * 64,
            "cad/arm.SLDPRT": "c" * 64,
        },
        "raw": {
            "saved_configuration": "Default",
            "components": [
                {
                    "instance_id": "base-1",
                    "name2": "base-1",
                    "document": "cad/base.SLDPRT",
                    "suppression_state": 2,
                },
                {
                    "instance_id": "arm-1",
                    "name2": "arm-1",
                    "document": "cad/arm.SLDPRT",
                    "suppression_state": 2,
                },
            ],
        },
    }
    files = {str(key): str(value) for key, value in discovery["native_files"].items()}
    discovery["raw"]["files"] = files
    discovery["raw"]["source_selection"] = sealed_selection(files, MAIN)
    (root / "discovery").mkdir(parents=True)
    discovery_path = root / "discovery" / "native-discovery.json"
    write_json(discovery_path, discovery)
    write_json(root / "source.json", {"schema_version": "solidworks-to-urdf.source/v1", "robot_name": "robot"})
    plan = {
        "schema_version": "solidworks-to-urdf.capture-plan/v1",
        "run_id": "r-1",
        "handoff_sha256": digest(files),
        "discovery_sha256": hashlib.sha256(discovery_path.read_bytes()).hexdigest(),
        "main_assembly": MAIN,
        "configuration": "Default",
        "identity": {"hardware_id": "m3.0", "revision": "r1"},
        "selection": {"files": files},
        "objects": [
            {"name": "base-1", "document": "cad/base.SLDPRT", "state": 2},
            {"name": "arm-1", "document": "cad/arm.SLDPRT", "state": 2},
        ],
        "groups": [
            {"name": "base_link", "components": ["base-1"]},
            {"name": "arm_link", "components": ["arm-1"]},
        ],
        "axis_requests": [
            {
                "name": "shoulder_pitch_joint",
                "axis_reference": {
                    "kind": "reference_axis",
                    "component": "",
                    "feature_name": "Axis1",
                },
            }
        ],
        "datums": ["CS_base_link"],
        "evidence_requests": list(CAPTURE_EVIDENCE_REQUESTS),
    }
    write_json(root / "capture-plan.json", plan)
    return root


class CapturePlanTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="capture-plan-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "prepared"
        self.root.mkdir()
        build_package(self.root)

    def _rewrite_plan(self, mutate) -> None:
        plan = json.loads((self.root / "capture-plan.json").read_text(encoding="utf-8"))
        mutate(plan)
        write_json(self.root / "capture-plan.json", plan)

    def test_an_intact_plan_is_consumed_with_the_sealed_source(self) -> None:
        source, plan = steps._consume_sealed_definition(self.root)
        self.assertEqual(source["robot_name"], "robot")
        self.assertEqual(plan["run_id"], "r-1")

    def test_a_sealed_embedded_virtual_selector_omits_the_session_document(self) -> None:
        owner_sha = "a" * 64
        selector = discovery._capture_plan_selector(
            {
                "instance_id": "waist-1/virtual-1",
                "name2": "waist-1/virtual-1",
                "document": "c:/users/operator/appdata/local/temp/swx1/vc~~/waist/virtual.sldprt",
                "configuration": "默认",
                "source_provenance": {
                    "kind": "native_embedded_virtual_component",
                    "state": "embedded_virtual_component",
                    "is_virtual": True,
                    "occurrence": "waist-1/virtual-1",
                    "owner": "waist.SLDASM",
                    "owner_sha256": owner_sha,
                    "session_document_path": (
                        "C:\\Users\\operator\\AppData\\Local\\Temp\\swx1\\VC~~\\waist\\virtual.SLDPRT"
                    ),
                    "session_temp_root": "C:\\Users\\operator\\AppData\\Local\\Temp\\swx1",
                    "native_document_path": (
                        "C:\\Users\\operator\\AppData\\Local\\Temp\\swx1\\VC~~\\waist\\virtual.SLDPRT"
                    ),
                    "materialization_path": (
                        "C:\\Users\\operator\\AppData\\Local\\Temp\\swx1\\VC~~\\waist\\virtual.SLDPRT"
                    ),
                    "native_api_pid": 1,
                    "used_api": "IComponent2.GetPathName/IComponent2.GetImportedPath/IComponent2.IsVirtual",
                    "author_imported_path": None,
                    "author_imported_error": None,
                    "document": None,
                },
                "suppression_state": 2,
                "state": 2,
            },
            {"waist.SLDASM": owner_sha},
        )
        self.assertEqual(selector, {"name": "waist-1/virtual-1", "state": 2})

    def test_a_fake_or_incomplete_virtual_selector_is_refused(self) -> None:
        cases = (
            (
                "portable staged inventory",
                {
                "kind": "native_embedded_virtual_component",
                "state": "embedded_virtual_component",
                "is_virtual": False,
                "occurrence": "waist-1/virtual-1",
                "owner": "waist.SLDASM",
                "owner_sha256": "a" * 64,
                "session_document_path": "c:/temp/VC~~/waist/virtual.sldprt",
                "session_temp_root": "c:/temp",
                "native_api_pid": 1,
                "used_api": "IComponent2.GetPathName",
                "author_imported_path": None,
                "author_imported_error": None,
                "document": None,
                },
            ),
            (
                "portable staged inventory",
                {
                "kind": "native_embedded_virtual_component",
                "state": "embedded_virtual_component",
                "is_virtual": True,
                "occurrence": "waist-1/virtual-1",
                "owner": "waist.SLDASM",
                "owner_sha256": "a" * 64,
                "session_document_path": "c:/temp/VC~~/waist/virtual.sldprt",
                "session_temp_root": "c:/temp",
                "native_api_pid": 1,
                "used_api": "IComponent2.GetPathName",
                "author_imported_path": None,
                "author_imported_error": None,
                "document": None,
                },
            ),
        )
        for expected, provenance in cases:
            with (
                self.subTest(kind=provenance["kind"], is_virtual=provenance["is_virtual"]),
                self.assertRaisesRegex(PipelineError, expected),
            ):
                discovery._capture_plan_selector(
                    {
                        "instance_id": "waist-1/virtual-1",
                        "name2": "waist-1/virtual-1",
                        "document": "c:/temp/VC~~/waist/virtual.sldprt",
                        "configuration": "默认",
                        "source_provenance": provenance,
                        "suppression_state": 2,
                        "state": 2,
                    },
                    {"waist.SLDASM": "a" * 64},
                )

    def test_a_portable_delivered_selector_keeps_its_document(self) -> None:
        selector = discovery._capture_plan_selector(
            {
                "instance_id": "part-1",
                "name2": "part-1",
                "document": "cad\\part.SLDPRT",
                "suppression_state": 2,
                "state": 2,
            },
            {},
        )
        self.assertEqual(selector, {"name": "part-1", "document": "cad/part.SLDPRT", "state": 2})

    def test_a_non_session_selector_with_an_absolute_document_is_refused(self) -> None:
        with self.assertRaisesRegex(PipelineError, "portable staged inventory"):
            discovery._capture_plan_selector(
                {
                    "instance_id": "part-1",
                    "name2": "part-1",
                    "document": "c:/users/operator/part.SLDPRT",
                    "suppression_state": 2,
                    "state": 2,
                },
                {},
            )

    def test_a_producer_refuses_conflicting_native_instance_identities(self) -> None:
        with self.assertRaisesRegex(PipelineError, "conflicting instance identities"):
            discovery._capture_plan_selector(
                {
                    "instance_id": "part-1",
                    "name2": "part-2",
                    "document": "cad/part.SLDPRT",
                    "suppression_state": 2,
                },
                {},
            )

    def test_a_duplicate_native_selector_is_refused(self) -> None:
        self._rewrite_plan(lambda plan: plan["objects"].append(dict(plan["objects"][0])))
        with self.assertRaisesRegex(PipelineError, "repeats a native object selector"):
            steps._consume_sealed_definition(self.root)

    def test_a_missing_plan_is_refused(self) -> None:
        (self.root / "capture-plan.json").unlink()
        with self.assertRaisesRegex(PipelineError, "no sealed capture plan"):
            steps._consume_sealed_definition(self.root)

    def test_a_plan_not_bound_to_the_sealed_discovery_is_refused(self) -> None:
        self._rewrite_plan(lambda plan: plan.update(discovery_sha256="f" * 64))
        with self.assertRaisesRegex(PipelineError, "does not bind the sealed discovery"):
            steps._consume_sealed_definition(self.root)

    def test_a_plan_with_another_selection_is_refused(self) -> None:
        self._rewrite_plan(lambda plan: plan["selection"]["files"].pop("cad/arm.SLDPRT"))
        with self.assertRaisesRegex(PipelineError, "selection differs"):
            steps._consume_sealed_definition(self.root)

    def test_a_plan_naming_another_project_entry_is_refused(self) -> None:
        self._rewrite_plan(lambda plan: plan.update(main_assembly="cad/other.SLDASM"))
        with self.assertRaisesRegex(PipelineError, "another project entry"):
            steps._consume_sealed_definition(self.root)

    def test_a_plan_with_another_configuration_is_refused(self) -> None:
        self._rewrite_plan(lambda plan: plan.update(configuration="Other"))
        with self.assertRaisesRegex(PipelineError, "saved configuration"):
            steps._consume_sealed_definition(self.root)

    def test_a_plan_without_usable_object_selectors_is_refused(self) -> None:
        self._rewrite_plan(lambda plan: plan.update(objects=[]))
        with self.assertRaisesRegex(PipelineError, "no native object selectors"):
            steps._consume_sealed_definition(self.root)

    def test_a_plan_with_an_unknown_selector_field_is_refused(self) -> None:
        self._rewrite_plan(lambda plan: plan["objects"][1].update(instance="x"))
        with self.assertRaisesRegex(PipelineError, "unknown native selector field"):
            steps._consume_sealed_definition(self.root)

    def test_a_boolean_selector_state_is_refused(self) -> None:
        self._rewrite_plan(lambda plan: plan["objects"][0].update(state=True))
        with self.assertRaisesRegex(PipelineError, "native suppression state"):
            steps._consume_sealed_definition(self.root)

    def test_a_source_selection_not_bound_to_the_raw_inventory_is_refused(self) -> None:
        discovery_document = json.loads(
            (self.root / "discovery" / "native-discovery.json").read_text(encoding="utf-8")
        )
        discovery_document["raw"]["files"].pop("cad/arm.SLDPRT")
        write_json(self.root / "discovery" / "native-discovery.json", discovery_document)
        discovery_sha = hashlib.sha256(
            (self.root / "discovery" / "native-discovery.json").read_bytes()
        ).hexdigest()
        self._rewrite_plan(lambda plan: plan.update(discovery_sha256=discovery_sha))
        with self.assertRaisesRegex(PipelineError, "complete frozen inventory"):
            steps._consume_sealed_definition(self.root)

    def test_a_plan_with_an_unknown_top_level_field_is_refused(self) -> None:
        self._rewrite_plan(lambda plan: plan.update(invented=True))
        with self.assertRaisesRegex(PipelineError, "unknown field"):
            steps._consume_sealed_definition(self.root)

    def test_a_plan_naming_an_unregistered_instance_is_refused(self) -> None:
        self._rewrite_plan(lambda plan: plan["objects"][1].update(name="ghost-1"))
        with self.assertRaisesRegex(PipelineError, "outside the sealed record"):
            steps._consume_sealed_definition(self.root)

    def test_conflicting_native_instance_identities_are_refused(self) -> None:
        discovery_path = self.root / "discovery" / "native-discovery.json"
        discovery = json.loads(discovery_path.read_text(encoding="utf-8"))
        component = discovery["raw"]["components"][1]
        component["instance_id"] = component["name2"]
        component["name2"] = "tampered-1"
        write_json(discovery_path, discovery)
        discovery_sha = hashlib.sha256(discovery_path.read_bytes()).hexdigest()
        self._rewrite_plan(lambda plan: plan.update(discovery_sha256=discovery_sha))
        with self.assertRaisesRegex(PipelineError, "conflicting instance identities"):
            steps._consume_sealed_definition(self.root)

    def test_duplicate_native_component_rows_are_refused_before_mapping(self) -> None:
        discovery_path = self.root / "discovery" / "native-discovery.json"
        discovery = json.loads(discovery_path.read_text(encoding="utf-8"))
        discovery["raw"]["components"].append(copy.deepcopy(discovery["raw"]["components"][1]))
        write_json(discovery_path, discovery)
        discovery_sha = hashlib.sha256(discovery_path.read_bytes()).hexdigest()
        self._rewrite_plan(lambda plan: plan.update(discovery_sha256=discovery_sha))
        with self.assertRaisesRegex(PipelineError, "repeats a native object selector"):
            steps._consume_sealed_definition(self.root)

    def test_a_plan_omitting_a_non_virtual_document_is_refused(self) -> None:
        self._rewrite_plan(lambda plan: plan["objects"][1].pop("document"))
        with self.assertRaisesRegex(PipelineError, "without complete session proof"):
            steps._consume_sealed_definition(self.root)

    def test_a_producer_refuses_a_non_virtual_missing_document(self) -> None:
        for document in (None, "", 7):
            with (
                self.subTest(document=document),
                self.assertRaisesRegex(PipelineError, "no document selector"),
            ):
                discovery._capture_plan_selector(
                    {
                        "instance_id": "part-1",
                        "name2": "part-1",
                        "document": document,
                        "suppression_state": 2,
                    },
                    {},
                )

    def test_an_embedded_virtual_selector_requires_the_complete_recomputed_selection(self) -> None:
        discovery_document = json.loads(
            (self.root / "discovery" / "native-discovery.json").read_text(encoding="utf-8")
        )
        component = next(
            item for item in discovery_document["raw"]["components"] if item["instance_id"] == "arm-1"
        )
        session_path = "C:\\Users\\operator\\AppData\\Local\\Temp\\swx1\\VC~~\\arm\\arm.SLDPRT"
        component.update(
            document=session_path.replace("\\", "/").lower(),
            configuration="Default",
            suppression_state=2,
            source_provenance={
                "kind": "native_embedded_virtual_component",
                "state": "embedded_virtual_component",
                "is_virtual": True,
                "occurrence": "arm-1",
                "owner": MAIN,
                "owner_sha256": "a" * 64,
                "session_document_path": session_path,
                "session_temp_root": "C:\\Users\\operator\\AppData\\Local\\Temp\\swx1",
                "native_document_path": session_path,
                "materialization_path": session_path,
                "native_api_pid": 1,
                "used_api": "IComponent2.GetPathName/IComponent2.GetImportedPath/IComponent2.IsVirtual",
                "author_imported_path": None,
                "author_imported_error": None,
                "document": None,
            },
        )
        entry = {
            "instance": "arm-1",
            "name": "arm-1",
            "kind": "native_embedded_virtual_component",
            "state": "embedded_virtual_component",
            "is_virtual": True,
            "owner": MAIN,
            "owner_sha256": "a" * 64,
            "configuration": "Default",
            "binding": "owner_embedded_geometry",
            "author_imported_path": None,
            "author_imported_error": None,
            "owner_anchor": None,
            "suppression_state": 2,
            "session_document_path": session_path,
            "session_temp_root": "C:\\Users\\operator\\AppData\\Local\\Temp\\swx1",
            "native_api_pid": 1,
            "used_api": "IComponent2.GetPathName/IComponent2.GetImportedPath/IComponent2.IsVirtual",
        }
        files = dict(discovery_document["raw"]["files"])
        manifest = sealed_selection(files, MAIN, virtual_components=[entry])
        discovery_document["raw"]["source_selection"] = manifest
        write_json(self.root / "discovery" / "native-discovery.json", discovery_document)
        discovery_sha = hashlib.sha256(
            (self.root / "discovery" / "native-discovery.json").read_bytes()
        ).hexdigest()
        self._rewrite_plan(
            lambda plan: (
                plan.update(discovery_sha256=discovery_sha),
                plan["objects"][1].pop("document"),
            )
        )
        steps._consume_sealed_definition(self.root)

        wrong_state = {**entry, "suppression_state": 3}
        discovery_document["raw"]["source_selection"] = sealed_selection(
            files,
            MAIN,
            virtual_components=[wrong_state],
        )
        write_json(self.root / "discovery" / "native-discovery.json", discovery_document)
        discovery_sha = hashlib.sha256(
            (self.root / "discovery" / "native-discovery.json").read_bytes()
        ).hexdigest()
        self._rewrite_plan(lambda plan: plan.update(discovery_sha256=discovery_sha))
        with self.assertRaisesRegex(PipelineError, "does not bind the embedded-virtual occurrence"):
            steps._consume_sealed_definition(self.root)

        unbound = dict(entry)
        unbound.pop("binding")
        with self.assertRaisesRegex(PipelineError, "owner-bound"):
            sealed_selection(files, MAIN, virtual_components=[unbound])

        discovery_document["raw"]["source_selection"] = manifest
        for field in (
            "schema_version",
            "scope",
            "reader_sha256",
            "upload_sha256",
            "main_assembly",
            "files",
            "handoff_sha256",
            "closure",
            "excluded_files",
            "selection_sha256",
        ):
            with self.subTest(field=field):
                incomplete = json.loads(json.dumps(discovery_document))
                incomplete["raw"]["source_selection"].pop(field)
                write_json(self.root / "discovery" / "native-discovery.json", incomplete)
                changed_sha = hashlib.sha256(
                    (self.root / "discovery" / "native-discovery.json").read_bytes()
                ).hexdigest()
                self._rewrite_plan(
                    lambda plan, sha=changed_sha: plan.update(discovery_sha256=sha)
                )
                with self.assertRaises(PipelineError):
                    steps._consume_sealed_definition(self.root)

        discovery_document["raw"]["source_selection"] = {
            "closure": {"virtual_components": [entry]}
        }
        write_json(self.root / "discovery" / "native-discovery.json", discovery_document)
        discovery_sha = hashlib.sha256(
            (self.root / "discovery" / "native-discovery.json").read_bytes()
        ).hexdigest()
        self._rewrite_plan(lambda plan: plan.update(discovery_sha256=discovery_sha))
        with self.assertRaises(PipelineError):
            steps._consume_sealed_definition(self.root)

    def test_a_plan_omitting_a_fake_virtual_document_is_refused(self) -> None:
        discovery = json.loads(
            (self.root / "discovery" / "native-discovery.json").read_text(encoding="utf-8")
        )
        component = next(item for item in discovery["raw"]["components"] if item["instance_id"] == "arm-1")
        component["document"] = None
        component["source_provenance"] = {
            "kind": "native_embedded_virtual_component",
            "state": "embedded_virtual_component",
            "is_virtual": True,
            "occurrence": "arm-1",
            "owner": MAIN,
            "owner_sha256": "a" * 64,
            "session_document_path": "c:/temp/swx0/VC~~/arm/arm.sldprt",
            "session_temp_root": "c:/temp/swx0",
            "native_api_pid": 0,
            "used_api": "IComponent2.GetPathName/IComponent2.ImportedPath/IComponent2.IsVirtual",
            "author_imported_path": None,
            "author_imported_error": None,
            "document": None,
        }
        write_json(self.root / "discovery" / "native-discovery.json", discovery)
        discovery_sha = hashlib.sha256((self.root / "discovery" / "native-discovery.json").read_bytes()).hexdigest()
        self._rewrite_plan(
            lambda plan: (
                plan.update(discovery_sha256=discovery_sha),
                plan["objects"][1].pop("document"),
            )
        )
        with self.assertRaisesRegex(PipelineError, "without complete session proof"):
            steps._consume_sealed_definition(self.root)

    def test_plan_entry_binds_an_api_proven_cached_import(self) -> None:
        session_path = "C:\\Users\\operator\\AppData\\Local\\Temp\\swx7\\IC~~\\arm.step.SLDPRT"
        session_root = "C:\\Users\\operator\\AppData\\Local\\Temp\\swx7"
        selection_root = "C:\\selection\\m3-flat"
        selection_path = selection_root + "\\swx8\\IC~~\\arm.step.SLDPRT"
        source_root = "C:\\discovery\\m3-flat"
        discovery_document = json.loads(
            (self.root / "discovery" / "native-discovery.json").read_text(encoding="utf-8")
        )
        component = next(
            item for item in discovery_document["raw"]["components"] if item["instance_id"] == "arm-1"
        )
        component.update(
            document=session_path,
            configuration="Default",
            suppression_state=2,
            source_provenance={
                "kind": "native_import_materialization",
                "state": "cached_materialization",
                "is_virtual": False,
                "occurrence": "arm-1",
                "owner": MAIN,
                "owner_sha256": "a" * 64,
                "materialization_path": session_path,
                "session_document_path": session_path,
                "native_document_path": session_path,
                "session_temp_root": session_root,
                "native_api_pid": 7,
                "author_imported_in_source_root": True,
                "author_imported_relative": "cad/arm.SLDPRT",
                "author_imported_path": source_root + "\\cad\\arm.SLDPRT",
                "source_root": source_root,
                "defining_anchor": None,
                "used_api": "IComponent2.GetPathName/IComponent2.GetImportedPath/IComponent2.IsVirtual",
                "error": None,
                "author_imported_error": None,
                "configuration": "Default",
            },
        )
        identity = {
            "kind": "native_import_materialization",
            "state": "cached_materialization",
            "owner": MAIN,
            "owner_sha256": "a" * 64,
            "occurrence": "arm-1",
            "defining_anchor": None,
            "configuration": "Default",
            "is_virtual": False,
            "used_api": "IComponent2.GetPathName/IComponent2.GetImportedPath/IComponent2.IsVirtual",
        }
        session_source_sha256 = digest(identity)
        entry = {
            "document": selection_path,
            "owner": MAIN,
            "owner_sha256": "a" * 64,
            "occurrence": "arm-1",
            "bound_source": "cad/arm.SLDPRT",
            "defining_materialization": None,
            "instance": "arm-1",
            "name": "arm-1",
            "author_imported_path": selection_root + "\\cad\\arm.SLDPRT",
            "author_metadata_in_stage": True,
            "kind": "native_import_materialization",
            "is_virtual": False,
            "materialization": selection_path,
            "session_temp_root": selection_root + "\\swx8",
            "native_api_pid": 8,
            "configuration": "Default",
            "suppression_state": 2,
            "state": "cached_materialization",
            "binding": "author_metadata_only",
            "owner_anchor": None,
            "defining_anchor": None,
            "used_api": identity["used_api"],
            "session_source_sha256": session_source_sha256,
        }
        files = dict(discovery_document["raw"]["files"])
        from description_pipeline.sources.solidworks.selection import seal_closure_selection
        from tests.sources.selection_support import closure_evidence

        evidence = closure_evidence(
            files,
            MAIN,
            edges={
                MAIN: ["cad/base.SLDPRT", "cad/arm.SLDPRT"],
                "cad/base.SLDPRT": [],
                "cad/arm.SLDPRT": [],
            },
            imports=[entry],
            stage_root=selection_root,
        )
        next(
            edge
            for edge in evidence["documents"][MAIN]["resolved_edges"]
            if edge["resolved"].replace("\\", "/") == "cad/arm.SLDPRT"
        ).update(
            kind="native_imported_document",
            instance="arm-1",
            resolved="cad/arm.SLDPRT",
            configuration="Default",
            suppression_state=2,
        )
        manifest = seal_closure_selection(
            files,
            MAIN,
            evidence,
            reader_sha256=evidence["reader_sha256"],
        )
        discovery_document["raw"]["source_selection"] = manifest
        write_json(self.root / "discovery" / "native-discovery.json", discovery_document)
        discovery_sha = hashlib.sha256(
            (self.root / "discovery" / "native-discovery.json").read_bytes()
        ).hexdigest()
        self._rewrite_plan(
            lambda plan: (
                plan.update(
                    discovery_sha256=discovery_sha,
                    objects=[
                        plan["objects"][0],
                        {
                            "name": "arm-1",
                            "state": 2,
                            "session_source_sha256": session_source_sha256,
                        },
                    ],
                )
            )
        )
        from description_pipeline.sources.solidworks.discovery import _capture_plan_selector

        self.assertEqual(
            _capture_plan_selector(component, files),
            {
                "name": "arm-1",
                "state": 2,
                "session_source_sha256": session_source_sha256,
            },
        )
        _source, consumed = steps._consume_sealed_definition(self.root)
        self.assertEqual(
            consumed["objects"][1]["session_source_sha256"],
            session_source_sha256,
        )
        self._rewrite_plan(
            lambda plan: plan["objects"][1].update(
                session_source_sha256="0" * 64
            )
        )
        with self.assertRaisesRegex(PipelineError, "cached selector differs"):
            steps._consume_sealed_definition(self.root)

    def test_plan_entry_keeps_a_saved_resident_document_with_import_origin(self) -> None:
        discovery_document = json.loads(
            (self.root / "discovery" / "native-discovery.json").read_text(encoding="utf-8")
        )
        component = next(
            item for item in discovery_document["raw"]["components"] if item["instance_id"] == "arm-1"
        )
        component.update(
            document="cad/arm.SLDPRT",
            configuration="Default",
            suppression_state=2,
            source_provenance={
                "state": "resolved",
                "kind": "native_imported_document",
                "source_root": "C:\\author",
                "native_document_path": "C:\\author\\arm.SLDPRT",
                "used_api": "IComponent2.GetPathName/IComponent2.GetImportedPath/IComponent2.IsVirtual",
                "imported_path": "C:\\author\\arm.step",
                "document": "cad/arm.SLDPRT",
                "sha256": "c" * 64,
                "is_virtual": False,
            },
        )
        write_json(self.root / "discovery" / "native-discovery.json", discovery_document)
        discovery_sha = hashlib.sha256(
            (self.root / "discovery" / "native-discovery.json").read_bytes()
        ).hexdigest()
        self._rewrite_plan(lambda plan: plan.update(discovery_sha256=discovery_sha))

        _source, plan = steps._consume_sealed_definition(self.root)

        selector = next(item for item in plan["objects"] if item["name"] == "arm-1")
        self.assertEqual(selector["document"], "cad/arm.SLDPRT")

    def test_a_plan_naming_another_run_is_refused(self) -> None:
        self._rewrite_plan(lambda plan: plan.update(run_id="r-2"))
        with self.assertRaisesRegex(PipelineError, "another run"):
            steps._consume_sealed_definition(self.root)

    def test_a_plan_with_foreign_identity_is_refused(self) -> None:
        self._rewrite_plan(
            lambda plan: plan["identity"].update(hardware_id="foreign")
        )
        with self.assertRaisesRegex(PipelineError, "plan identity differs"):
            steps._consume_sealed_definition(self.root)

    def test_a_plan_requesting_unknown_evidence_is_refused(self) -> None:
        self._rewrite_plan(lambda plan: plan["evidence_requests"].append("evidence/invented.json"))
        with self.assertRaisesRegex(PipelineError, "unknown evidence items"):
            steps._consume_sealed_definition(self.root)

    def test_a_missing_source_identity_is_refused(self) -> None:
        (self.root / "source.json").unlink()
        report = inspect_prepared_package(self.root)
        self.assertIs(report["passed"], False)
        self.assertEqual(report["warnings"], [])
        self.assertEqual(report["errors"][0]["code"], "input.source_identity")
        with self.assertRaisesRegex(PipelineError, "no sealed source identity"):
            steps._consume_sealed_definition(self.root)

    def test_a_plan_with_an_unknown_axis_request_field_is_refused(self) -> None:
        self._rewrite_plan(lambda plan: plan["axis_requests"][0].update(invented=1))
        with self.assertRaisesRegex(PipelineError, "unusable native axis requests"):
            steps._consume_sealed_definition(self.root)

    def test_the_retired_carrier_keys_are_refused_by_the_freeze_contract(self) -> None:
        from description_pipeline.sources.solidworks.errors import ConfigError
        from description_pipeline.sources.solidworks.freeze import validate_source_config

        base = {
            "provider": "solidworks",
            "assembly": "/tmp/a/robot.SLDASM",
            "configuration": "Default",
            "groups": [{"name": "base_link", "components": ["base-1"]}],
            "axis_requests": [],
        }
        validate_source_config(base)
        for retired in ("bodies", "joints", "frames"):
            with self.subTest(retired=retired), self.assertRaisesRegex(ConfigError, "unknown keys"):
                validate_source_config({**base, retired: []})


if __name__ == "__main__":
    unittest.main()
