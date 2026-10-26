"""Appearance raw remains evidence until every delivered face can be represented."""

from __future__ import annotations

import copy
import unittest

from description_pipeline.sources.solidworks.appearance import (
    UNRECORDED_APPEARANCE,
    AppearanceError,
    audit_appearance,
    appearance_groups,
    bind_capture_appearance,
    require_deliverable_appearance,
    resolve_deliverable_appearance,
)
from description_pipeline.geometry.native_tessellation import native_stl_subset

from . import support

COMPONENT = "arm-1"
DOCUMENT = r"C:\capture\arm.SLDPRT"
CONFIGURATION = "Painted"


def _layer(
    *,
    owner_configuration: str,
    values=None,
    has_values: bool = False,
    texture_state: str = "absent",
    texture_name=None,
    read_errors=None,
):
    return {
        "used_api": f"appearance/{owner_configuration}",
        "material_call": {
            "configuration_semantics": "requested_configuration",
            "configuration_option": 3,
            "configuration_names": [owner_configuration],
        },
        "presence_call": {
            "arguments": [],
            "configuration_semantics": "native_owner_context",
        },
        "material_property_values": values,
        "has_material_property_values": has_values,
        "texture": {
            "configuration": owner_configuration,
            "state": texture_state,
            "material_name": texture_name,
        },
        "read_errors": dict(read_errors or {}),
    }


def _body_layer(
    *,
    active_configuration: str,
    values=None,
    has_values: bool = False,
    texture_state: str = "absent",
    texture_name=None,
    read_errors=None,
):
    layer = _layer(
        owner_configuration=active_configuration,
        values=values,
        has_values=has_values,
        texture_state=texture_state,
        texture_name=texture_name,
        read_errors=read_errors,
    )
    layer["material_call"] = {
        "configuration_semantics": "active_configuration",
        "active_configuration": active_configuration,
    }
    return layer


def _raw(
    *,
    document=None,
    occurrence=None,
    body=None,
    active_configuration: str = CONFIGURATION,
    active_configuration_error=None,
):
    return {
        "component": COMPONENT,
        "source_document_path": DOCUMENT,
        "referenced_configuration": CONFIGURATION,
        "native_active_configuration": active_configuration,
        "native_active_configuration_error": active_configuration_error,
        "document": document
        or _layer(owner_configuration=CONFIGURATION),
        "occurrence": occurrence
        or _layer(owner_configuration=CONFIGURATION),
        "bodies": [
            {
                "component": COMPONENT,
                "enumeration_api": "IComponent2.GetBodies2",
                "body_type": 0,
                "body_index": 0,
                "body_name": "arm",
                "appearance": body
                or _body_layer(active_configuration=active_configuration),
            }
        ],
    }


def _resolved_raw(values):
    face_rows = []
    for index, face_values in enumerate(values):
        face_rows.append(
            {
                "face_index": index,
                "appearance": _layer(
                    owner_configuration=CONFIGURATION,
                    values=face_values,
                    has_values=True,
                ),
            }
        )
    return {
        **_raw(),
        "native_referenced_display_state": "Display State 1",
        "native_referenced_display_state_error": None,
        "state_render_materials": [
            {
                "owner_document_path": DOCUMENT,
                "owner_scope": "component_document",
                "api": "IModelDocExtension.GetRenderMaterials2",
                "display_state_option": 3,
                "display_state": "Display State 1",
                "call_state": "empty",
                "materials": [],
                "read_errors": {},
            }
        ],
        "bodies": [
            {
                "component": COMPONENT,
                "enumeration_api": "IComponent2.GetBodies2",
                "body_type": 0,
                "body_index": 0,
                "body_name": "arm",
                "appearance": _body_layer(
                    active_configuration=CONFIGURATION
                ),
                "faces": face_rows,
            }
        ],
    }


class AppearanceAuditTests(unittest.TestCase):
    def _audit(self, raw):
        return audit_appearance(
            raw,
            component=COMPONENT,
            expected_document_path=DOCUMENT,
            expected_configuration=CONFIGURATION,
        )

    def test_unrecorded_or_explicitly_unobserved_owners_are_not_deliverable(self):
        raw = _raw(
            occurrence=_layer(
                owner_configuration=CONFIGURATION,
                values=[-1.0] * 9,
                has_values=False,
            )
        )
        audit = self._audit(raw)
        self.assertEqual(audit.state, "unobserved")
        self.assertEqual(audit.issues, ())
        self.assertEqual(audit.claims, ())
        for candidate in (UNRECORDED_APPEARANCE, raw):
            with self.subTest(candidate=candidate):
                with self.assertRaises(AppearanceError) as caught:
                    require_deliverable_appearance(
                        candidate,
                        component=COMPONENT,
                        expected_document_path=DOCUMENT,
                        expected_configuration=CONFIGURATION,
                    )
                self.assertIn(
                    "cad_appearance_effective_scope_unproven",
                    {
                        issue["code"]
                        for issue in caught.exception.details["appearance"]["issues"]
                    },
                )

    def test_read_failure_is_not_treated_as_absence(self):
        raw = _raw(
            document=_layer(
                owner_configuration=CONFIGURATION,
                texture_state="unreadable",
                read_errors={"texture": {"code": "native_read_failed"}},
            )
        )
        with self.assertRaises(AppearanceError) as caught:
            require_deliverable_appearance(
                raw,
                component=COMPONENT,
                expected_document_path=DOCUMENT,
                expected_configuration=CONFIGURATION,
            )
        issue_paths = {
            issue["path"] for issue in caught.exception.details["appearance"]["issues"]
        }
        self.assertIn(
            "appearance_raw.document.read_errors.texture",
            issue_paths,
        )
        self.assertIn("appearance_raw.document.texture", issue_paths)

    def test_explicit_missing_record_is_not_treated_as_unrecorded(self):
        self.assertEqual(
            self._audit(UNRECORDED_APPEARANCE).state,
            "unobserved",
        )
        audit = self._audit(None)
        self.assertEqual(audit.state, "invalid")
        self.assertEqual(
            audit.issues[0]["code"],
            "cad_appearance_record_missing",
        )

    def test_texture_pointer_cannot_be_silently_omitted_from_monochrome_urdf(self):
        raw = _raw(
            document=_layer(
                owner_configuration=CONFIGURATION,
                texture_state="present",
                texture_name=r"C:\textures\paint.jpg",
            )
        )
        audit = self._audit(raw)
        self.assertEqual(audit.state, "unrepresentable")
        self.assertIn(
            "cad_appearance_texture_unrepresentable",
            {issue["code"] for issue in audit.issues},
        )

    def test_conflicting_owner_vectors_and_incomplete_scope_are_both_reported(self):
        raw = _raw(
            document=_layer(
                owner_configuration=CONFIGURATION,
                values=[0.1] * 9,
                has_values=True,
            ),
            body=_body_layer(
                active_configuration=CONFIGURATION,
                values=[0.7] * 9,
                has_values=True,
            ),
        )
        audit = self._audit(raw)
        self.assertEqual(audit.state, "unrepresentable")
        self.assertEqual(len(audit.claims), 2)
        self.assertIn(
            "cad_appearance_owner_conflict",
            {issue["code"] for issue in audit.issues},
        )
        self.assertIn(
            "cad_appearance_effective_scope_unproven",
            {issue["code"] for issue in audit.issues},
        )

    def test_equal_owner_vectors_do_not_prove_the_complete_effective_appearance(self):
        values = [0.2, 0.3, 0.4, 0.1, 0.2, 0.3, 0.4, 0.0, 0.0]
        audit = self._audit(
            _raw(
                document=_layer(
                    owner_configuration=CONFIGURATION,
                    values=values,
                    has_values=True,
                ),
                body=_body_layer(
                    active_configuration=CONFIGURATION,
                    values=values,
                    has_values=True,
                ),
            )
        )
        codes = {issue["code"] for issue in audit.issues}
        self.assertNotIn("cad_appearance_owner_conflict", codes)
        self.assertIn("cad_appearance_effective_scope_unproven", codes)

    def test_wrong_owner_document_or_configuration_is_rejected(self):
        raw = copy.deepcopy(_raw())
        raw["component"] = "other-1"
        raw["source_document_path"] = r"C:\capture\other.SLDPRT"
        raw["referenced_configuration"] = "Default"
        audit = self._audit(raw)
        codes = {issue["code"] for issue in audit.issues}
        self.assertIn("cad_appearance_object_mismatch", codes)
        self.assertIn("cad_appearance_owner_mismatch", codes)
        self.assertIn("cad_appearance_configuration_mismatch", codes)

    def test_body_appearance_inventory_must_match_the_captured_bodies(self):
        audit = audit_appearance(
            _raw(),
            component=COMPONENT,
            expected_document_path=DOCUMENT,
            expected_configuration=CONFIGURATION,
            expected_bodies=[
                {"body_type": 0, "body_index": 0},
                {"body_type": 0, "body_index": 1},
            ],
        )
        self.assertIn(
            "cad_appearance_body_inventory_mismatch",
            {issue["code"] for issue in audit.issues},
        )

    def test_consumers_bind_appearance_and_scope_from_native_facts(self):
        appearance = _raw()
        raw_scene = {"components": [{"name": COMPONENT}]}
        geometry = [{"component": COMPONENT, "path": "geometry/arm.stl"}]
        scope = {
            "source_document_path": DOCUMENT,
            "referenced_configuration": CONFIGURATION,
        }
        bound_scene, bound_geometry = bind_capture_appearance(
            raw_scene,
            geometry,
            {
                "scene_raw": {
                    "occurrences": [
                        {
                            "name": COMPONENT,
                            "appearance_raw": appearance,
                        }
                    ]
                },
                "source_scopes": {COMPONENT: scope},
            },
        )
        self.assertEqual(
            bound_scene["components"][0]["appearance_raw"],
            appearance,
        )
        self.assertEqual(bound_geometry[0]["source_scope"], scope)
        self.assertNotIn("appearance_raw", raw_scene["components"][0])
        self.assertNotIn("source_scope", geometry[0])


class AppearanceResolutionTests(unittest.TestCase):
    def test_face_owners_partition_one_occurrence_into_multiple_materials(self):
        red = [1.0, 0.0, 0.0, 1.0, 1.0, 0.5, 0.3125, 0.25, 0.0]
        blue = [0.0, 0.0, 1.0, 1.0, 1.0, 0.5, 0.3125, 0.0, 0.0]
        spans = [
            {
                "body_type": 0,
                "body_index": 0,
                "face_index": index,
                "triangle_start": index * 2,
                "triangle_count": 2,
                "face_reference_sha256": str(index + 1) * 64,
            }
            for index in range(2)
        ]
        resolution = resolve_deliverable_appearance(
            _resolved_raw([red, blue]),
            component=COMPONENT,
            expected_document_path=DOCUMENT,
            expected_configuration=CONFIGURATION,
            face_spans=spans,
        )
        groups = appearance_groups(resolution.faces)
        self.assertEqual(len(groups), 2)
        self.assertEqual(
            {tuple(group["rgba"]) for group in groups},
            {
                (1.0, 0.0, 0.0, 0.75),
                (0.0, 0.0, 1.0, 1.0),
            },
        )
        self.assertTrue(
            all(
                group["triangle_ranges"]
                == [{"triangle_start": index * 2, "triangle_count": 2}]
                for index, group in enumerate(
                    sorted(groups, key=lambda row: row["face_references"][0])
                )
            )
        )
        source = support.binary_stl_bytes(
            [((0, 0, 0), (1, 0, 0), (0, 1, 0))] * 4
        )
        subsets = [
            native_stl_subset(source, group["triangle_ranges"])
            for group in groups
        ]
        self.assertEqual(
            sum(count for _payload, count in subsets),
            4,
        )
        self.assertTrue(all(count == 2 for _payload, count in subsets))

    def test_documented_face_override_wins_over_part_default(self):
        red = [1.0, 0.0, 0.0, 1.0, 1.0, 0.5, 0.3125, 0.0, 0.0]
        blue = [0.0, 0.0, 1.0, 1.0, 1.0, 0.5, 0.3125, 0.0, 0.0]
        raw = _resolved_raw([red])
        raw["document"] = _layer(
            owner_configuration=CONFIGURATION,
            values=blue,
            has_values=True,
        )
        resolved = resolve_deliverable_appearance(
            raw,
            component=COMPONENT,
            expected_document_path=DOCUMENT,
            expected_configuration=CONFIGURATION,
            face_spans=[
                {
                    "body_type": 0,
                    "body_index": 0,
                    "face_index": 0,
                    "triangle_start": 0,
                    "triangle_count": 1,
                    "face_reference_sha256": "a" * 64,
                }
            ],
        )
        self.assertEqual(resolved.faces[0].rgba, (1.0, 0.0, 0.0, 1.0))
        self.assertEqual(resolved.faces[0].source["scopes"], ["face"])

    def test_source_render_material_owner_shape_stops_before_unproven_color_decode(self):
        raw = _resolved_raw(
            [[1.0, 0.0, 0.0, 1.0, 1.0, 0.5, 0.3125, 0.0, 0.0]]
        )
        raw["state_render_materials"] = [
            {
                "owner_document_path": DOCUMENT,
                "owner_scope": "component_document",
                "api": "IModelDocExtension.GetRenderMaterials2",
                "display_state_option": 3,
                "configuration": CONFIGURATION,
                "display_state": "Display State 1",
                "call_state": "observed",
                "materials": [
                    {
                        "material_index": 0,
                        "linked_display_states": ["Display State 1"],
                        "entities": ["native-face"],
                        "entities_state": "observed",
                        "entity_records": [
                            {
                                "entity_index": 0,
                                "native_object_type": "IFace2",
                                "persistent_reference": [1, 2, 3],
                                "read_errors": {},
                                "matched_identity": {
                                    "occurrences": [],
                                    "bodies": [],
                                    "faces": [
                                        {
                                            "component": COMPONENT,
                                            "body_type": 0,
                                            "body_index": 0,
                                            "face_index": 0,
                                        }
                                    ],
                                    "features": [],
                                },
                            }
                        ],
                        "primary_color": 0x00AABBCC,
                        "transparency": 0.25,
                        "color_form": 1,
                        "texture_filename": "",
                        "surface_finish_file": "",
                        "read_errors": {},
                    }
                ],
                "read_errors": {},
            }
        ]
        with self.assertRaises(AppearanceError) as caught:
            resolve_deliverable_appearance(
                raw,
                component=COMPONENT,
                expected_document_path=DOCUMENT,
                expected_configuration=CONFIGURATION,
                face_spans=[
                    {
                        "body_type": 0,
                        "body_index": 0,
                        "face_index": 0,
                        "triangle_start": 0,
                        "triangle_count": 1,
                        "face_reference_sha256": "a" * 64,
                    }
                ],
            )
        self.assertEqual(
            caught.exception.appearance["issues"][0]["code"],
            "cad_appearance_render_color_encoding_unproven",
        )


if __name__ == "__main__":
    unittest.main()
