"""Independent checks over sealed native capture facts."""

from __future__ import annotations

import copy
import hashlib
import tempfile
import unittest
from pathlib import Path

import numpy as np

from description_pipeline.geometry.native_scope import (
    native_scope_identity_sha256,
)
from description_pipeline.geometry.native_tessellation import (
    native_triangle_relative_path,
    write_native_triangle_stream,
)
from description_pipeline.geometry.native_transform import transform_from_solidworks
from description_pipeline.io import PipelineError, write_json
from description_pipeline.schemas import (
    EFFECTIVE_INSTANCES_SCHEMA,
    NATIVE_CAPTURE_OBSERVATION_SCHEMA,
)
from description_pipeline.sources.snapshot import verify_snapshot, write_manifest
from description_pipeline.sources.solidworks import capture_projection
from description_pipeline.sources.solidworks.errors import BridgeError, CadError
from description_pipeline.verification import capture_evidence
from description_pipeline.verification.native_capture_facts import verify_native_capture_facts
from description_pipeline.verification.solidworks_physics import (
    _require_link_repeatability,
    verify_urdf_mass_equality,
)


class NativeCaptureFactsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="native-capture-facts-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "evidence/raw").mkdir(parents=True)
        (self.root / "urdf").mkdir()
        (self.root / "urdf/robot.urdf").write_text(
            '<robot name="fixture"><link name="base"><inertial><mass value="2.0"/></inertial></link></robot>',
            encoding="utf-8",
        )
        self.identity = [
            1.0,
            0.0,
            0.0,
            0.0,
            1.0,
            0.0,
            0.0,
            0.0,
            1.0,
            0.1,
            0.2,
            0.3,
            1.0,
            0.0,
            0.0,
            0.0,
        ]
        self.transform = list(transform_from_solidworks(self.identity))
        self.datum_transform = [
            sum(
                self.transform[4 * row + index] * self.transform[4 * index + column]
                for index in range(4)
            )
            for row in range(4)
            for column in range(4)
        ]
        self.about = [[1.0, 0.0, 0.0], [0.0, 2.0, 0.0], [0.0, 0.0, 3.0]]
        self.origin = [1.08, -0.04, 0.0, -0.04, 2.02, 0.0, 0.0, 0.0, 3.10]
        self.overrides = {
            "OverrideMass": False,
            "OverrideCenterOfMass": False,
            "OverrideMomentsOfInertia": False,
        }
        self.read = {
            "mass_kg": 2.0,
            "com_m": [0.1, 0.2, 0.0],
            "inertia_values_raw": [1.0, 0.0, 0.0, 0.0, 2.0, 0.0, 0.0, 0.0, 3.0],
            "inertia_values_about_origin_api": self.origin,
            "overrides": self.overrides,
            "read_errors": {},
        }
        raw_mass = {
            "native_raw": {
                "mass_kg": 2.0,
                "center_of_mass_m": [0.1, 0.2, 0.0],
                "inertia_values": [1.0, 0.0, 0.0, 0.0, 2.0, 0.0, 0.0, 0.0, 3.0],
                "overrides": self.overrides,
            },
            "reference": {"scope": "part_document"},
            "read_errors": {},
        }
        source_scope = {
            "rule_version": "native-open-edge-scope-binding-v1",
            "component": "base",
            "source_document_path": "C:/source/base.SLDPRT",
            "source_copy_path": "source/base.SLDPRT",
            "source_document_sha256": "a" * 64,
            "source_copy_sha256": "a" * 64,
            "source_document_identity_sha256": "a" * 64,
            "source_document_identity_kind": "file_sha256",
            "referenced_configuration": "Default",
            "occurrence_scope": "base",
            "virtual_owner": None,
            "virtual_owner_sha256": None,
            "virtual_session_document_path": None,
            "virtual_session_temp_root": None,
            "virtual_native_api_pid": None,
        }
        raw_facts = {
            "schema_version": NATIVE_CAPTURE_OBSERVATION_SCHEMA,
            "scene_raw": {
                "document": "/source/root.SLDASM",
                "configuration": "Default",
                "occurrences": [
                    {
                        "name": "base",
                        "path": "/source/base.SLDPRT",
                        "transform_raw": self.identity,
                        "is_fixed": True,
                        "document_type": "part",
                    }
                ],
                "components": [
                    {
                        "name": "base",
                        "path": "/source/base.SLDPRT",
                        "transform_raw": self.identity,
                        "is_fixed": True,
                        "document_type": "part",
                    }
                ],
                "coordinate_systems": {
                    "base::Datum": {
                        "key": "base::Datum",
                        "name": "Datum",
                        "owner": "base",
                        "configuration": "Default",
                        "document": "/source/base.SLDPRT",
                        "array_data": self.identity,
                        "read_errors": {},
                        "occurrence_transform_raw": self.identity,
                    }
                },
            },
            "part_mass_raw": {"base": raw_mass},
            "assembly_mass_raw": raw_mass,
            "component_mass_raw": {
                "assembly": "/source/root.SLDASM",
                "configuration": "Default",
                "instances": [
                    {
                        "name": "base",
                        "parent": None,
                        "depth": 0,
                        "document": "/source/base.SLDPRT",
                        "document_type": "part",
                        "context_mass_kg": 2.0,
                        "context_volume_m3": 1.0,
                        "overrides": self.overrides,
                        "read_errors": {},
                    }
                ],
                "errors": [],
                "reference": {},
            },
            "link_group_mass_raw": {
                "assembly": "/source/root.SLDASM",
                "configuration": "Default",
                "used_api": "fixture",
                "selection_rule": "fixture",
                "links": {
                    "base": {"selection": ["base"], "reads": [self.read, self.read]}
                },
            },
            "source_scopes": {"base": source_scope},
            "geometry_raw": {
                "base": {
                    "stream_path": "",
                    "stream_sha256": "",
                    "triangle_count": 1,
                    "transform_raw": self.identity,
                    "body_inventory": [
                        {
                            "body_type": 0,
                            "body_index": 0,
                            "body_name": "base",
                            "face_count": 1,
                        }
                    ],
                    "face_inventory": [
                        {
                            "body_type": 0,
                            "body_index": 0,
                            "face_index": 0,
                            "triangle_start": 0,
                            "triangle_count": 1,
                        }
                    ],
                    "body_counts": {"solid": 1, "sheet": 0},
                    "used_api": "fixture",
                    "source_scope_sha256": native_scope_identity_sha256(
                        source_scope
                    ),
                    "appearance_raw": None,
                }
            },
            "axis_references_raw": [],
            "document_state": {
                "before": {
                    "configuration": "Default",
                    "read_only": True,
                    "save_flag": False,
                },
                "after": {
                    "configuration": "Default",
                    "read_only": True,
                    "save_flag": False,
                },
                "used_api": "fixture",
            },
            "input_stability": {
                "originals_unchanged": {"files_checked": 1},
            },
            "notes": {},
        }
        projected_mass = {
            "mass": 2.0,
            "com": [0.1, 0.2, 0.0],
            "inertia": self.about,
            "reference": {"scope": "part_document"},
        }
        projected_context = {
            "status": "recorded",
            "assembly_mass_kg": 2.0,
            "instances": [
                {
                    "name": "base",
                    "parent": None,
                    "depth": 0,
                    "document": "/source/base.SLDPRT",
                    "document_type": "part",
                    "context_mass_kg": 2.0,
                    "context_volume_m3": 1.0,
                    "overrides": self.overrides,
                    "effective": None,
                }
            ],
            "errors": [],
            "reference": {},
        }
        projected_link_read = {
            "mass_kg": 2.0,
            "com_m": [0.1, 0.2, 0.0],
            "inertia_tensor_about_com": self.about,
            "inertia_values_about_origin_api": self.origin,
            "overrides": self.overrides,
        }
        self.raw_facts = raw_facts
        self.capture = {
            "scene": {
                "document": "/source/root.SLDASM",
                "components": [
                    {
                        "name": "base",
                        "path": "/source/base.SLDPRT",
                        "transform": self.transform,
                        "is_fixed": True,
                        "document_type": "part",
                    }
                ],
                "coordinate_systems": {"base::Datum": self.datum_transform},
                "notes": {"effective_instances": {}},
            },
            "components": [{"name": "base", "mass_properties": projected_mass}],
            "datums": [],
            "mass_closure": {
                "status": "recorded",
                "mode": "full",
                "top_level": {
                    "mass": 2.0,
                    "com": [0.1, 0.2, 0.0],
                    "inertia": self.about,
                    "reference": {"scope": "assembly_document"},
                },
                "component_context": projected_context,
            },
            "effective_instances": {
                "schema_version": EFFECTIVE_INSTANCES_SCHEMA,
                "assembly": "/source/root.SLDASM",
                "configuration": "Default",
                "readings": {},
                "warnings": [],
            },
            "link_group_mass_properties": {
                "assembly": "/source/root.SLDASM",
                "configuration": "Default",
                "used_api": "fixture",
                "selection_rule": "fixture",
                "links": {
                    "base": {
                        "selection": ["base"],
                        "reads": [projected_link_read, dict(projected_link_read)],
                        "reproducibility": {
                            "mass_delta_kg": 0.0,
                            "com_delta_norm_m": 0.0,
                            "tensor_delta_max_abs": 0.0,
                        },
                    }
                },
            },
        }
        self.write_capture()

    def write_capture(
        self,
        *,
        detected: str = "solidworks_standard",
        applied: bool = False,
    ):
        del detected, applied
        triangle_values = [
            0.0,
            0.0,
            0.0,
            1.0,
            0.0,
            0.0,
            0.0,
            1.0,
            0.0,
        ]
        temporary_stream = self.root / "evidence/raw/fixture.triangles"
        count = write_native_triangle_stream(
            temporary_stream,
            triangle_values,
        )
        stream_sha256 = hashlib.sha256(
            temporary_stream.read_bytes()
        ).hexdigest()
        stream_path = native_triangle_relative_path(stream_sha256)
        target = self.root / "evidence" / stream_path
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary_stream.replace(target)
        geometry = self.raw_facts["geometry_raw"]["base"]
        geometry.update(
            stream_path=stream_path,
            stream_sha256=stream_sha256,
            triangle_count=count,
        )
        write_json(
            self.root / "evidence/raw/native_capture_facts.json",
            self.raw_facts,
        )
        write_manifest(
            self.root / "evidence",
            kind="solidworks",
            identity={
                "assembly": "C:/source/root.SLDASM",
                "configuration": "Default",
            },
            evidence_class="fixture",
        )

    def write_urdf(self, body: str) -> None:
        (self.root / "urdf/robot.urdf").write_text(
            f'<robot name="fixture">{body}</robot>',
            encoding="utf-8",
        )

    def test_raw_values_reconstruct_and_close_mass(self):
        result = verify_native_capture_facts(self.root)
        self.assertTrue(result["native_capture_facts"])
        self.assertEqual(result["inertia_family"], "solidworks_standard")
        self.assertEqual(verify_urdf_mass_equality(self.root)["urdf_mass_kg"], 2.0)

    def test_urdf_mass_reconciles_to_the_raw_whole_value_exactly(self) -> None:
        details = verify_urdf_mass_equality(self.root)
        self.assertEqual(details["whole_cad_mass_kg"], 2.0)
        self.assertEqual(details["urdf_mass_kg"], 2.0)
        self.assertEqual(details["delta_kg"], 0.0)
        self.assertEqual(details["atol_kg"], 1e-12)
        self.assertEqual(details["rtol"], 0.0)

        self.write_urdf(
            '<link name="base"><inertial><mass value="2.000001"/>'
            "</inertial></link>"
        )
        with self.assertRaisesRegex(PipelineError, "differs"):
            verify_urdf_mass_equality(self.root)

    def test_urdf_mass_parser_rejects_malformed_inertial_inventory(self) -> None:
        cases = {
            "no-inertial": '<link name="base"/>',
            "multiple-inertial": (
                '<link name="base"><inertial><mass value="2.0"/></inertial>'
                '<inertial><mass value="2.0"/></inertial></link>'
            ),
            "missing-mass": '<link name="base"><inertial/></link>',
            "duplicate-mass": (
                '<link name="base"><inertial><mass value="2.0"/>'
                '<mass value="2.0"/></inertial></link>'
            ),
            "nonnumeric-mass": (
                '<link name="base"><inertial><mass value="invalid"/>'
                "</inertial></link>"
            ),
        }
        for label, body in cases.items():
            with self.subTest(label=label):
                self.write_urdf(body)
                with self.assertRaises(PipelineError):
                    verify_urdf_mass_equality(self.root)

    def test_urdf_mass_counts_only_actual_links(self) -> None:
        self.write_urdf(
            '<link name="base"><inertial><mass value="2.0"/></inertial></link>'
            '<link name="sensor_frame"/>'
            "<extension><inertial><mass value=\"100.0\"/></inertial></extension>"
        )
        details = verify_urdf_mass_equality(self.root)
        self.assertEqual(details["urdf_mass_kg"], 2.0)
        self.assertEqual(details["inertials"], 1)

    def test_material_provenance_warning_reconstructs_from_raw_facts(self) -> None:
        self.assertEqual(
            capture_evidence.material_provenance(self.root)["warnings"],
            [],
        )
        assignment = {
            "schema_version": "swbridge.material-assignment/v1",
            "configuration": "Default",
            "unverified_reason": "cad_material_provenance_missing",
            "message": "explicit physical material is missing",
            "missing_body_indices": [0],
        }
        self.raw_facts["part_mass_raw"]["base"]["reference"][
            "material_assignment"
        ] = copy.deepcopy(assignment)
        self.write_capture()

        record = capture_evidence.material_provenance(self.root)
        self.assertEqual(
            [warning["code"] for warning in record["warnings"]],
            ["cad_material_provenance_missing"],
        )
        warning = record["warnings"][0]
        self.assertIs(warning["blocking"], False)
        self.assertEqual(warning["component"], "base")
        self.assertEqual(warning["material_assignment"], assignment)
        self.assertEqual(warning["measured"]["mass_kg"], 2.0)
        self.assertEqual(warning["measured"]["center_of_mass_m"], [0.1, 0.2, 0.0])
        self.assertTrue(
            verify_native_capture_facts(self.root)["native_capture_facts"]
        )

    def test_link_group_repeatability_records_and_rejects_raw_drift(self) -> None:
        baseline = copy.deepcopy(self.raw_facts)
        record = capture_evidence.link_group_mass_properties(self.root)
        row = record["links"]["base"]
        tensors = [
            np.asarray(read["inertia_tensor_about_com"], dtype=float)
            for read in row["reads"]
        ]
        _require_link_repeatability(
            "base",
            row["reads"],
            tensors,
            row["reproducibility"],
        )

        mutations = (
            (
                "mass",
                lambda read: read.__setitem__("mass_kg", 2.001),
                lambda read: read.__setitem__("mass_kg", 2.001),
            ),
            (
                "com",
                lambda read: read["com_m"].__setitem__(0, 0.101),
                lambda read: read["com_m"].__setitem__(0, 0.101),
            ),
            (
                "tensor",
                lambda read: read["inertia_values_raw"].__setitem__(0, 1.00001),
                lambda read: read["inertia_tensor_about_com"][0].__setitem__(
                    0,
                    1.00001,
                ),
            ),
        )
        for label, mutate_raw, mutate_projected in mutations:
            with self.subTest(label=label):
                facts = copy.deepcopy(baseline)
                reads = facts["link_group_mass_raw"]["links"]["base"]["reads"]
                reads[1] = copy.deepcopy(reads[0])
                mutate_raw(reads[1])
                self.raw_facts = facts
                self.write_capture()

                with self.assertRaisesRegex(
                    BridgeError,
                    "neither product convention fits",
                ):
                    capture_evidence.link_group_mass_properties(self.root)

                projected = copy.deepcopy(row)
                projected["reads"][1] = copy.deepcopy(projected["reads"][0])
                mutate_projected(projected["reads"][1])
                tensors = [
                    np.asarray(
                        read["inertia_tensor_about_com"],
                        dtype=float,
                    )
                    for read in projected["reads"]
                ]
                with self.assertRaisesRegex(PipelineError, "do not repeat"):
                    _require_link_repeatability(
                        "base",
                        projected["reads"],
                        tensors,
                        projected["reproducibility"],
                    )

    def test_positive_products_are_normalized_from_raw_facts(self):
        raw_facts = copy.deepcopy(self.raw_facts)
        positive_about = [1.0, 0.04, 0.0, 0.04, 2.0, 0.0, 0.0, 0.0, 3.0]
        positive_origin = [1.08, 0.08, 0.0, 0.08, 2.02, 0.0, 0.0, 0.0, 3.10]
        for raw in (
            raw_facts["part_mass_raw"]["base"],
            raw_facts["assembly_mass_raw"],
        ):
            raw["native_raw"]["inertia_values"] = positive_about
        for read in raw_facts["link_group_mass_raw"]["links"]["base"]["reads"]:
            read["inertia_values_raw"] = positive_about
            read["inertia_values_about_origin_api"] = positive_origin
        self.raw_facts = raw_facts
        self.write_capture()
        result = verify_native_capture_facts(self.root)
        self.assertTrue(result["inertia_transform_applied"])

    def test_effective_instance_projection_uses_raw_component_context(self) -> None:
        raw = self.raw_facts["component_mass_raw"]["instances"][0]
        overrides = {
            "OverrideMass": True,
            "OverrideCenterOfMass": False,
            "OverrideMomentsOfInertia": False,
        }
        raw["effective"] = {
            "native_raw": {
                "mass_kg": 1.0,
                "center_of_mass_m": [0.01, 0.0, 0.0],
                "inertia_values": [
                    0.1,
                    0.0,
                    0.0,
                    0.0,
                    0.2,
                    0.0,
                    0.0,
                    0.0,
                    0.3,
                ],
                "overrides": overrides,
            },
            "reference": {
                "scope": "assembly_component_group",
                "part_document": "/source/base.SLDPRT",
                "assembly_document": "/source/root.SLDASM",
                "configuration": "Default",
                "overrides": overrides,
            },
            "read_errors": {},
        }
        self.write_capture()
        result = verify_native_capture_facts(self.root)
        self.assertTrue(result["native_capture_facts"])
        effective = capture_evidence.effective_instances(self.root)
        self.assertEqual(set(effective["readings"]), {"base"})
        self.assertEqual(
            [row["code"] for row in effective["warnings"]],
            ["cad_instance_override_effective"],
        )

    def test_explicit_mass_and_link_read_errors_fail_closed(self) -> None:
        self.raw_facts["part_mass_raw"]["base"]["read_errors"] = {
            "mass_kg": {
                "code": "cad_mass_read_failed",
                "message": "unreadable",
            }
        }
        self.write_capture()
        with self.assertRaisesRegex(PipelineError, "raw mass_kg read failed"):
            verify_native_capture_facts(self.root)

        self.raw_facts["part_mass_raw"]["base"]["read_errors"] = {}
        self.raw_facts["link_group_mass_raw"]["links"]["base"]["reads"][0][
            "read_errors"
        ] = {
            "mass_kg": {
                "code": "cad_mass_read_failed",
                "message": "unreadable",
            }
        }
        self.write_capture()
        with self.assertRaisesRegex(
            PipelineError,
            "link-group base read 0 mass_kg failed: cad_mass_read_failed",
        ):
            capture_evidence.link_group_mass_properties(self.root)

    def test_axis_unread_state_cannot_be_masked_by_valid_values(self) -> None:
        self.raw_facts["axis_references_raw"] = [
            {
                "joint": "hinge",
                "kind": "reference_axis",
                "component": "base",
                "selector": {"kind": "reference_axis", "component": "base"},
                "coordinate_frame": "component_local",
                "read_errors": {},
                "used_api": "fixture",
                "suppressed_raw": False,
                "feature_error_code_raw": False,
                "endpoints_raw": [0.0, 0.0, 0.0, 0.0, 0.0, 1.0],
            }
        ]
        self.write_capture()
        with self.assertRaisesRegex(PipelineError, "feature error"):
            capture_evidence.axes(self.root)

        row = self.raw_facts["axis_references_raw"][0]
        row.update(
            kind="cylinder",
            feature_name="Cyl1",
            body_type_native="solid",
            face_candidates=[
                {
                    "body_index": 0,
                    "face_index": 0,
                    "feature_name": "Cyl1",
                    "is_cylinder": True,
                    "cylinder_params_raw": [
                        0.0,
                        0.0,
                        0.0,
                        0.0,
                        0.0,
                        1.0,
                        0.01,
                    ],
                    "read_errors": {
                        "is_cylinder": {
                            "code": "cad_axis_reference_unreadable"
                        }
                    },
                }
            ],
        )
        self.write_capture()
        with self.assertRaisesRegex(PipelineError, "candidate type is unreadable"):
            capture_evidence.axes(self.root)

        candidate = row["face_candidates"][0]
        candidate["read_errors"] = {}
        for field, value in (
            ("body_index", False),
            ("body_index", -1),
            ("body_index", "0"),
            ("face_index", False),
            ("face_index", -1),
            ("face_index", "0"),
        ):
            with self.subTest(field=field, value=value):
                malformed = copy.deepcopy(row)
                malformed["face_candidates"][0][field] = value
                with self.assertRaisesRegex(CadError, "identity"):
                    capture_projection._project_cylinder_axis(
                        copy.deepcopy(malformed)
                    )
                with self.assertRaisesRegex(PipelineError, "identity"):
                    capture_evidence._axis_cylinder(
                        copy.deepcopy(malformed)
                    )

    def test_link_group_origin_error_is_preserved_without_a_vote(self) -> None:
        links = self.raw_facts["link_group_mass_raw"]["links"]
        reads = links["base"]["reads"]
        reads[0] = copy.deepcopy(reads[0])
        reads[1] = copy.deepcopy(reads[1])
        origin_error = {
            "code": "RuntimeError",
            "message": "origin getter unavailable",
        }
        reads[0]["inertia_values_about_origin_api"] = copy.deepcopy(
            origin_error
        )
        reads[0]["read_errors"][
            "inertia_values_about_origin_api"
        ] = copy.deepcopy(origin_error)
        links["base_secondary"] = {
            "selection": ["base"],
            "reads": [
                copy.deepcopy(reads[1]),
                copy.deepcopy(reads[1]),
            ],
        }
        self.write_capture()

        projected = capture_evidence.link_group_mass_properties(self.root)
        self.assertEqual(
            projected["links"]["base"]["reads"][0][
                "inertia_values_about_origin_api"
            ],
            origin_error,
        )
        self.assertIsInstance(
            projected["links"]["base"]["reads"][1][
                "inertia_values_about_origin_api"
            ],
            list,
        )
        self.assertTrue(
            verify_native_capture_facts(self.root)["native_capture_facts"]
        )
        producer = capture_projection._project_link_group_mass(
            copy.deepcopy(self.raw_facts["link_group_mass_raw"])
        )
        self.assertEqual(
            producer["links"]["base"]["reads"][0][
                "inertia_values_about_origin_api"
            ],
            origin_error,
        )

        facts = copy.deepcopy(self.raw_facts)
        facts["link_group_mass_raw"]["links"]["base"]["reads"][0][
            "inertia_values_about_origin_api"
        ] = {}
        self.raw_facts = facts
        self.write_capture()
        with self.assertRaisesRegex(BridgeError, "origin.*malformed"):
            capture_evidence.link_group_mass_properties(self.root)

        facts = copy.deepcopy(self.raw_facts)
        read = facts["link_group_mass_raw"]["links"]["base"]["reads"][0]
        read["inertia_values_about_origin_api"] = copy.deepcopy(
            origin_error
        )
        read["read_errors"]["inertia_values_about_origin_api"][
            "message"
        ] = "a different native result"
        with self.assertRaisesRegex(
            CadError,
            "differs from its read-error entry",
        ):
            capture_projection._project_link_group_mass(
                copy.deepcopy(facts["link_group_mass_raw"])
            )
        self.raw_facts = facts
        self.write_capture()
        with self.assertRaisesRegex(
            BridgeError,
            "origin error differs",
        ):
            capture_evidence.link_group_mass_properties(self.root)

    def test_datum_error_map_must_be_an_exact_object(self) -> None:
        baseline = copy.deepcopy(self.raw_facts)
        missing = object()
        for label, value in (
            ("missing", missing),
            ("list", []),
            ("false", False),
            ("zero", 0),
            ("empty-string", ""),
        ):
            with self.subTest(label=label):
                facts = copy.deepcopy(baseline)
                datum = facts["scene_raw"]["coordinate_systems"]["base::Datum"]
                if value is missing:
                    datum.pop("read_errors")
                else:
                    datum["read_errors"] = value

                with self.assertRaisesRegex(CadError, "error map"):
                    capture_projection._project_scene(
                        copy.deepcopy(facts["scene_raw"])
                    )

                self.raw_facts = facts
                self.write_capture()
                with self.assertRaisesRegex(PipelineError, "error map"):
                    capture_evidence.scene(self.root)
                with self.assertRaisesRegex(PipelineError, "error map"):
                    verify_native_capture_facts(self.root)

        class ErrorMap(dict):
            pass

        facts = copy.deepcopy(baseline)
        facts["scene_raw"]["coordinate_systems"]["base::Datum"][
            "read_errors"
        ] = ErrorMap()
        with self.assertRaisesRegex(CadError, "error map"):
            capture_projection._project_scene(
                copy.deepcopy(facts["scene_raw"])
            )

    def test_datum_owner_must_not_be_coerced_to_global_scope(self) -> None:
        baseline = copy.deepcopy(self.raw_facts)
        for label, owner in (
            ("zero", 0),
            ("false", False),
            ("list", []),
            ("object", {}),
        ):
            with self.subTest(label=label):
                facts = copy.deepcopy(baseline)
                facts["scene_raw"]["coordinate_systems"]["base::Datum"][
                    "owner"
                ] = owner

                with self.assertRaisesRegex(CadError, "string owner"):
                    capture_projection._project_scene(
                        copy.deepcopy(facts["scene_raw"])
                    )

                self.raw_facts = facts
                self.write_capture()
                with self.assertRaisesRegex(PipelineError, "string owner"):
                    capture_evidence.scene(self.root)
                with self.assertRaisesRegex(PipelineError, "string owner"):
                    verify_native_capture_facts(self.root)

    def test_valid_fixed_state_rejects_a_non_null_fixed_error(self) -> None:
        baseline = copy.deepcopy(self.raw_facts)
        error = {
            "code": "cad_component_fixed_invalid",
            "message": "unreadable",
        }
        for field in ("occurrences", "components"):
            with self.subTest(field=field):
                facts = copy.deepcopy(baseline)
                facts["scene_raw"][field][0]["is_fixed_error"] = copy.deepcopy(
                    error
                )

                with self.assertRaisesRegex(CadError, "fixed"):
                    capture_projection._project_scene(
                        copy.deepcopy(facts["scene_raw"])
                    )

                self.raw_facts = facts
                self.write_capture()
                with self.assertRaisesRegex(PipelineError, "fixed"):
                    capture_evidence.scene(self.root)
                with self.assertRaisesRegex(PipelineError, "fixed"):
                    verify_native_capture_facts(self.root)

    def test_missing_sealed_native_facts_member_is_rejected(self):
        (self.root / "evidence/raw/native_capture_facts.json").unlink()
        with self.assertRaises(PipelineError):
            verify_native_capture_facts(self.root)

    def test_raw_member_tamper_breaks_the_source_manifest(self):
        path = self.root / "evidence/raw/native_capture_facts.json"
        record = self.raw_facts
        record["assembly_mass_raw"]["native_raw"]["mass_kg"] = 2.01
        write_json(path, record)
        with self.assertRaisesRegex(
            PipelineError,
            "Source snapshot is incomplete or changed",
        ):
            verify_snapshot(self.root / "evidence")


if __name__ == "__main__":
    unittest.main()
