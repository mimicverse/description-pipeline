"""Appearance raw remains evidence until every delivered face can be represented."""

from __future__ import annotations

import copy
import unittest

from description_pipeline.sources.solidworks.appearance import (
    UNRECORDED_APPEARANCE,
    AppearanceError,
    audit_appearance,
    bind_capture_appearance,
    require_fallback_eligible,
)

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


class AppearanceAuditTests(unittest.TestCase):
    def _audit(self, raw):
        return audit_appearance(
            raw,
            component=COMPONENT,
            expected_document_path=DOCUMENT,
            expected_configuration=CONFIGURATION,
        )

    def test_explicitly_unobserved_owners_leave_the_warned_fallback_eligible(self):
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
        self.assertEqual(
            require_fallback_eligible(
                _raw(),
                component=COMPONENT,
                expected_document_path=DOCUMENT,
                expected_configuration=CONFIGURATION,
            ).state,
            "unobserved",
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
            require_fallback_eligible(
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


if __name__ == "__main__":
    unittest.main()
