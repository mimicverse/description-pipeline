"""Raw geometry sealing and Linux-side STL projection regressions."""

from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from description_pipeline.geometry.native_scope import native_source_scope_is_valid
from description_pipeline.io import PipelineError, write_json
from description_pipeline.schemas import (
    CAPTURE_EVIDENCE_REQUESTS,
    NATIVE_CAPTURE_OBSERVATION_SCHEMA,
)
from description_pipeline.sources.solidworks.errors import CadError
from description_pipeline.sources.solidworks.capture_geometry import (
    materialize_native_geometry,
    seal_native_geometry,
)
from description_pipeline.sources.solidworks.capture_projection import (
    build_native_capture_observation,
    load_native_capture_observation,
    project_native_capture_observation,
    validate_native_capture_plan,
)
from tests.sources import support


class CaptureGeometryTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="capture-geometry-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "prepared"
        assembly = support.make_cad_tree(self.root)
        parts = [self.root / "base.SLDPRT", self.root / "arm.SLDPRT"]
        self.backend = support.FixtureCadBackend(
            assembly,
            [
                {
                    "name": "base-1",
                    "path": str(parts[0]),
                    "transform": support.placement(),
                    "mass": support.mass_payload(
                        1.0,
                        (0.01, 0.02, 0.0),
                    ),
                },
                {
                    "name": "arm-1",
                    "path": str(parts[1]),
                    "transform": support.placement((0.0, 0.0, 0.2)),
                    "mass": support.mass_payload(
                        0.5,
                        (0.0, 0.0, 0.05),
                    ),
                },
            ],
            dependencies=parts,
        )
        document = str(assembly)
        self.backend.prepare_prepared_capture(
            str(self.root),
            document,
            "Default",
        )
        self.raw_facts = self.backend.collect_prepared_scene(
            document,
            [],
            groups=[
                {"name": "base_link", "components": ["base-1"]},
                {"name": "arm_link", "components": ["arm-1"]},
            ],
        )
        self.raw_geometry = self.backend.capture_prepared_geometry(
            self.raw_facts["source_scopes"]
        )
        for component, facts in self.raw_geometry.items():
            scope = self.raw_facts["source_scopes"][component]
            facts["appearance_raw"] = {
                "component": component,
                "source_document_path": scope["source_document_path"],
                "referenced_configuration": scope[
                    "referenced_configuration"
                ],
                "fixture": True,
            }
        self.raw_axis = self.backend.capture_axis_reference(
            {
                "kind": "cylinder",
                "component": "arm-1",
                "body_type": "solid",
                "face_index": 0,
            }
        )
        self.backend.release()

    def test_raw_sealing_has_no_stl_and_linux_materialization_matches_legacy(
        self,
    ) -> None:
        evidence_root = Path(self.root) / "sealed/evidence"
        geometry_raw = seal_native_geometry(
            self.raw_geometry,
            self.raw_facts["source_scopes"],
            evidence_root,
        )
        self.assertFalse((evidence_root / "geometry").exists())
        self.assertTrue((evidence_root / "raw/triangles").is_dir())
        for row in geometry_raw.values():
            self.assertNotIn("triangle_values", row)
            self.assertNotIn("source_scope", row)
            self.assertNotIn(
                "source_document_path",
                row["appearance_raw"],
            )
            self.assertNotIn(
                "referenced_configuration",
                row["appearance_raw"],
            )
            raw_path = evidence_root / row["stream_path"]
            self.assertTrue(raw_path.is_file())

        actual = materialize_native_geometry(
            geometry_raw,
            self.raw_facts["source_scopes"],
            evidence_root=evidence_root,
            destination_root=Path(self.root) / "linux",
        )
        self.assertEqual(set(actual), set(geometry_raw))
        for component, row in actual.items():
            self.assertEqual(row["component"], component)
            self.assertGreater(row["triangles"], 0)
            self.assertTrue(
                (Path(self.root) / "linux" / row["path"]).is_file()
            )

    def test_linux_materialization_refuses_a_tampered_stream(self) -> None:
        evidence_root = Path(self.root) / "sealed/evidence"
        geometry_raw = seal_native_geometry(
            self.raw_geometry,
            self.raw_facts["source_scopes"],
            evidence_root,
        )
        component = sorted(geometry_raw)[0]
        raw_path = evidence_root / geometry_raw[component]["stream_path"]
        raw_path.write_bytes(raw_path.read_bytes() + b"tamper")

        with self.assertRaisesRegex(
            CadError,
            "triangle stream digest differs",
        ):
            materialize_native_geometry(
                geometry_raw,
                self.raw_facts["source_scopes"],
                evidence_root=evidence_root,
                destination_root=Path(self.root) / "linux",
            )

    def _named_geometry_fixture(self, root, component):
        assembly = support.make_cad_tree(root)
        parts = [root / "base.SLDPRT", root / "arm.SLDPRT"]
        backend = support.FixtureCadBackend(assembly, [
            {
                "name": "base-1", "path": str(parts[0]), "transform": support.placement(),
                "mass": support.mass_payload(1.0, (0.01, 0.02, 0.0)),
            },
            {
                "name": component, "path": str(parts[1]), "transform": support.placement((0.0, 0.0, 0.2)),
                "mass": support.mass_payload(0.5, (0.0, 0.0, 0.05)),
            },
        ], dependencies=parts)
        try:
            backend.prepare_prepared_capture(str(root), str(assembly), "Default")
            facts = backend.collect_prepared_scene(str(assembly), [], groups=[
                {"name": "base_link", "components": ["base-1"]},
                {"name": "arm_link", "components": [component]},
            ])
            geometry = backend.capture_prepared_geometry(facts["source_scopes"])
            for name, raw in geometry.items():
                scope = facts["source_scopes"][name]
                raw["appearance_raw"] = {
                    "component": name, "source_document_path": scope["source_document_path"],
                    "referenced_configuration": scope["referenced_configuration"], "fixture": True,
                }
            return facts, geometry
        finally:
            backend.release()

    def test_stl_staging_is_independent_of_native_occurrence_names(self) -> None:
        reference_bytes = None
        for index, component in enumerate(("Arm-1", "Assembly-1/Part-1", "P" * 248 + "-1")):
            with self.subTest(component=component):
                root = self.root / f"name-case-{index}"
                facts, raw = self._named_geometry_fixture(root / "prepared", component)
                scopes = facts["source_scopes"]
                self.assertTrue(all(native_source_scope_is_valid(scope, name) for name, scope in scopes.items()))
                original_facts, original_raw = copy.deepcopy(facts), copy.deepcopy(raw)
                cad_inputs = {path: path.read_bytes() for path in (root / "prepared").iterdir()}
                evidence = root / "evidence"
                sealed = seal_native_geometry(raw, scopes, evidence)
                sealed_bytes = {path: path.read_bytes() for path in evidence.rglob("*") if path.is_file()}
                destination = root / "linux"
                geometry_directory = destination / "geometry"
                geometry_directory.mkdir(parents=True)
                unrelated = geometry_directory / "unrelated.keep"
                unrelated.write_bytes(b"retain unrelated output")
                result = materialize_native_geometry(
                    sealed, scopes, evidence_root=evidence, destination_root=destination,
                )
                self.assertEqual(set(result), set(scopes))
                for name, row in result.items():
                    self.assertEqual(row["component"], name)
                    self.assertEqual(row["raw_sha"], sealed[name]["stream_sha256"])
                    self.assertEqual(row["path"], f"geometry/{row['sha']}.stl")
                    payload = (destination / row["path"]).read_bytes()
                    if reference_bytes is None:
                        reference_bytes = payload
                    self.assertEqual(payload, reference_bytes)
                self.assertEqual(set(geometry_directory.iterdir()), {
                    unrelated, *(destination / row["path"] for row in result.values()),
                })
                self.assertEqual(unrelated.read_bytes(), b"retain unrelated output")
                self.assertEqual({path: path.read_bytes() for path in cad_inputs}, cad_inputs)
                self.assertEqual({path: path.read_bytes() for path in sealed_bytes}, sealed_bytes)
                self.assertEqual(facts, original_facts)
                self.assertEqual(raw, original_raw)

    def test_stl_staging_cleans_owned_files_after_materialization_and_publish_failures(self) -> None:
        evidence = self.root / "failure-evidence"
        scopes = self.raw_facts["source_scopes"]
        sealed = seal_native_geometry(self.raw_geometry, scopes, evidence)
        before = {path: path.read_bytes() for path in evidence.rglob("*") if path.is_file()}

        def fail_partial(_raw_path, target, _spans, *, triangle_count):
            self.assertGreater(triangle_count, 0)
            self.assertFalse(target.exists())
            with target.open("xb") as stream:
                stream.write(b"partial STL")
            raise OSError("materialization failed after a partial write")

        module = "description_pipeline.sources.solidworks.capture_geometry"
        for failure in ("materialize", "publish"):
            with self.subTest(failure=failure):
                destination = self.root / f"failed-{failure}"
                geometry_directory = destination / "geometry"
                geometry_directory.mkdir(parents=True)
                unrelated = geometry_directory / "unrelated.keep"
                unrelated.write_bytes(b"retain unrelated output")
                fault = (
                    mock.patch(module + ".materialize_native_stl", side_effect=fail_partial)
                    if failure == "materialize" else mock.patch(
                        module + ".os.replace", side_effect=OSError("STL publication failed")
                    )
                )
                with fault, self.assertRaises(OSError):
                    materialize_native_geometry(
                        sealed, scopes, evidence_root=evidence, destination_root=destination,
                    )
                self.assertEqual(list(geometry_directory.iterdir()), [unrelated])
                self.assertEqual(unrelated.read_bytes(), b"retain unrelated output")
                self.assertEqual({path: path.read_bytes() for path in before}, before)

    def test_raw_observation_reader_and_projection_use_one_record(self) -> None:
        evidence_root = Path(self.root) / "sealed/evidence"
        geometry_raw = seal_native_geometry(
            self.raw_geometry,
            self.raw_facts["source_scopes"],
            evidence_root,
        )
        observation = build_native_capture_observation(
            facts=self.raw_facts,
            geometry_raw=geometry_raw,
            axis_references_raw=[{"joint": "hinge", **self.raw_axis}],
            document_state={
                "before": {
                    "path": self.raw_facts["scene_raw"]["document"],
                    "read_only": True,
                    "active_configuration": self.raw_facts[
                        "scene_raw"
                    ]["configuration"],
                },
                "after": {
                    "path": self.raw_facts["scene_raw"]["document"],
                    "read_only": True,
                    "active_configuration": self.raw_facts[
                        "scene_raw"
                    ]["configuration"],
                },
            },
            input_stability={
                "unchanged": True,
                "files_checked": 3,
            },
        )
        self.assertEqual(
            observation["schema_version"],
            NATIVE_CAPTURE_OBSERVATION_SCHEMA,
        )
        write_json(
            evidence_root / "raw/native_capture_facts.json.gz",
            observation,
        )

        loaded = load_native_capture_observation(evidence_root)
        projected = project_native_capture_observation(
            loaded,
            {
                "configuration": "Default",
                "groups": [
                    {
                        "name": "base_link",
                        "components": ["base-1"],
                    },
                    {
                        "name": "arm_link",
                        "components": ["arm-1"],
                    },
                ],
            },
        )
        scene = loaded["scene_raw"]
        occurrences = {
            row["name"]: row
            for row in scene["occurrences"]
        }
        plan = {
            "schema_version": "solidworks-to-urdf.capture-plan/v1",
            "run_id": "fixture-run",
            "handoff_sha256": "a" * 64,
            "discovery_sha256": "b" * 64,
            "main_assembly": Path(scene["document"]).name,
            "configuration": scene["configuration"],
            "identity": {"hardware_id": "fixture", "revision": "r1"},
            "selection": {"files": {"robot.SLDASM": "a" * 64}},
            "objects": [
                {
                    "name": name,
                    "document": Path(row["path"]).name,
                    "state": row["suppression_state"],
                }
                for name, row in sorted(occurrences.items())
            ],
            "groups": [
                {"name": "base_link", "components": ["base-1"]},
                {"name": "arm_link", "components": ["arm-1"]},
            ],
            "axis_requests": [
                {
                    "name": "hinge",
                    "axis_reference": {
                        **loaded["axis_references_raw"][0]["selector"]
                    },
                }
            ],
            "datums": sorted(scene["coordinate_systems"]),
            "evidence_requests": list(CAPTURE_EVIDENCE_REQUESTS),
        }
        validate_native_capture_plan(loaded, plan)
        with self.assertRaisesRegex(
            PipelineError,
            "axis inventory differs",
        ):
            validate_native_capture_plan(
                loaded,
                {**plan, "axis_requests": []},
            )

        container_observation = copy.deepcopy(loaded)
        container_observation["scene_raw"]["occurrences"].append(
            {
                "name": "sub-1",
                "path": str(Path(scene["document"]).with_name("sub.SLDASM")),
                "transform_raw": self.raw_facts["scene_raw"]["occurrences"][0][
                    "transform_raw"
                ],
                "suppression_state": 2,
                "is_fixed": False,
                "document_type": "assembly",
            }
        )
        container_plan = copy.deepcopy(plan)
        container_plan["objects"].append(
            {
                "name": "sub-1",
                "document": "sub.SLDASM",
                "state": 2,
            }
        )
        validate_native_capture_plan(container_observation, container_plan)

        def nest(value):
            if isinstance(value, str):
                return "carrier-1/base-1" if value == "base-1" else value
            if isinstance(value, list):
                return [nest(item) for item in value]
            if isinstance(value, dict):
                return {nest(key): nest(item) for key, item in value.items()}
            return value

        provider_observation = nest(copy.deepcopy(loaded))
        provider_plan = nest(copy.deepcopy(plan))
        provider_observation["scene_raw"]["occurrences"].append(
            {**container_observation["scene_raw"]["occurrences"][-1], "name": "carrier-1"}
        )
        provider_plan["objects"].append(
            {"name": "carrier-1", "document": "sub.SLDASM", "state": 2}
        )
        provider_observation["component_mass_raw"]["instances"].append(
            {"name": "carrier-1", "effective": {"role": "effective_instance"}}
        )
        provider_group = provider_observation["link_group_mass_raw"]["links"]["base_link"]
        provider_group["selection"] = ["carrier-1"]
        validate_native_capture_plan(provider_observation, provider_plan)
        for selection in (
            [],
            ["carrier-1/base-1"],
            ["carrier-1", "carrier-1/base-1"],
            ["carrier-1/base-1", "carrier-1"],
            ["carrier-1", "carrier-1"],
            ["carrier"],
            ["arm-1"],
            [["carrier-1"]],
        ):
            with self.subTest(selection=selection), self.assertRaises(PipelineError):
                invalid_provider = copy.deepcopy(provider_observation)
                invalid_provider["link_group_mass_raw"]["links"]["base_link"][
                    "selection"
                ] = selection
                validate_native_capture_plan(invalid_provider, provider_plan)
        unbacked_provider = copy.deepcopy(provider_observation)
        unbacked_provider["component_mass_raw"]["instances"].pop()
        with self.assertRaises(PipelineError):
            validate_native_capture_plan(unbacked_provider, provider_plan)
        malformed_instances = copy.deepcopy(provider_observation)
        malformed_instances["component_mass_raw"]["instances"] = None
        with self.assertRaises(PipelineError):
            validate_native_capture_plan(malformed_instances, provider_plan)
        cross_group_plan = copy.deepcopy(provider_plan)
        cross_group_plan["groups"][0]["components"].append("arm-1")
        with self.assertRaises(PipelineError):
            validate_native_capture_plan(provider_observation, cross_group_plan)

        reference_observation = copy.deepcopy(loaded)
        reference_selector = {
            "kind": "reference_axis",
            "component": "",
            "feature_name": "Axis1",
        }
        reference_observation["axis_references_raw"][0].update(
            kind="reference_axis",
            selector=reference_selector,
        )
        reference_plan = copy.deepcopy(plan)
        reference_plan["axis_requests"][0][
            "axis_reference"
        ] = reference_selector
        validate_native_capture_plan(reference_observation, reference_plan)

        swapped_groups = copy.deepcopy(plan)
        swapped_groups["groups"][0]["components"], swapped_groups[
            "groups"
        ][1]["components"] = (
            swapped_groups["groups"][1]["components"],
            swapped_groups["groups"][0]["components"],
        )
        invalid = (
            (
                "schema",
                loaded,
                {**copy.deepcopy(plan), "schema_version": "forged"},
            ),
            (
                "main",
                loaded,
                {**copy.deepcopy(plan), "main_assembly": "foreign.SLDASM"},
            ),
            (
                "configuration",
                loaded,
                {**copy.deepcopy(plan), "configuration": "Other"},
            ),
            (
                "axis",
                loaded,
                copy.deepcopy(plan)
                | {
                    "axis_requests": [
                        {
                            "name": "hinge",
                            "axis_reference": {
                                **plan["axis_requests"][0]["axis_reference"],
                                "component": "base-1",
                            },
                        }
                    ]
                },
            ),
            (
                "group",
                loaded,
                copy.deepcopy(plan)
                | {
                    "groups": [
                        {"name": "foreign", "components": ["base-1"]},
                        {"name": "arm_link", "components": ["arm-1"]},
                    ]
                },
            ),
            (
                "group-membership",
                loaded,
                swapped_groups,
            ),
            (
                "document",
                loaded,
                copy.deepcopy(plan)
                | {
                    "objects": [
                        {**plan["objects"][0], "document": "foreign.SLDPRT"},
                        plan["objects"][1],
                    ]
                },
            ),
            (
                "unknown-plan-field",
                loaded,
                copy.deepcopy(plan) | {"second_authority": True},
            ),
            (
                "datum-omission",
                copy.deepcopy(loaded)
                | {
                    "scene_raw": {
                        **scene,
                        "coordinate_systems": {"CS_base_link": {}},
                    }
                },
                plan,
            ),
            (
                "datum-duplicate",
                copy.deepcopy(loaded)
                | {
                    "scene_raw": {
                        **scene,
                        "coordinate_systems": {"CS_base_link": {}},
                    }
                },
                copy.deepcopy(plan)
                | {"datums": ["CS_base_link", "CS_base_link"]},
            ),
            (
                "scene-document-state",
                copy.deepcopy(loaded)
                | {
                    "scene_raw": {
                        **scene,
                        "document_state": {},
                    }
                },
                plan,
            ),
            (
                "scene-source-sha256",
                copy.deepcopy(loaded)
                | {
                    "scene_raw": {
                        **scene,
                        "source_sha256": "f" * 64,
                    }
                },
                plan,
            ),
        )
        for label, candidate_observation, malformed in invalid:
            with self.subTest(label=label), self.assertRaises(PipelineError):
                validate_native_capture_plan(
                    candidate_observation,
                    malformed,
                )
        incomplete = copy.deepcopy(loaded)
        incomplete["part_mass_raw"] = {}
        with self.assertRaises(PipelineError):
            validate_native_capture_plan(incomplete, plan)

        self.assertEqual(loaded, observation)
        self.assertEqual(
            {component.name for component in projected["scene"].components},
            {"base-1", "arm-1"},
        )
        self.assertEqual(projected["axis_references"][0]["joint"], "hinge")
        component = sorted(geometry_raw)[0]
        stream = evidence_root / geometry_raw[component]["stream_path"]
        stream.write_bytes(stream.read_bytes() + b"tamper")
        with self.assertRaisesRegex(
            PipelineError,
            "triangle stream digest differs",
        ):
            load_native_capture_observation(evidence_root)

        for field in ("document_state", "source_sha256"):
            with self.subTest(field=field):
                stream.write_bytes(stream.read_bytes()[:-6])
                malformed = copy.deepcopy(observation)
                malformed["scene_raw"][field] = {}
                write_json(
                    evidence_root / "raw/native_capture_facts.json.gz",
                    malformed,
                )
                with self.assertRaisesRegex(
                    PipelineError,
                    "document identity is incomplete",
                ):
                    load_native_capture_observation(evidence_root)

if __name__ == "__main__":
    unittest.main()
