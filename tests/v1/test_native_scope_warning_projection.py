"""Derived native IDs retain their provenance through every report surface."""

from __future__ import annotations

import copy
import json
import unittest

from description_pipeline.geometry.native_scope import (
    NATIVE_GATE_WARNING_CODES,
    NATIVE_SCOPE_IDENTITY_RULE,
    NATIVE_SCOPE_LOCAL_IDENTITY_DERIVED,
    NATIVE_SCOPE_WARNING_DETAIL_FIELDS,
    native_gate_warning_details_match,
    native_scope_identity_sha256,
)
from description_pipeline.io import PipelineError
from description_pipeline.orchestration.portal import PortalError, _quality_warnings
from description_pipeline.orchestration.report_view import _recorded_warnings
from description_pipeline.verification.solidworks_urdf import native_gate_warning_diagnostics


IDENTITY_FIELDS = {
    "body": "body_reference_sha256",
    "face": "face_reference_sha256",
}


def raw_warnings(kind: str = "face", body_index: int = 1) -> list[dict]:
    detail = {
        "component": "part-1",
        "entity_kind": kind,
        "body_type": 0,
        "body_index": body_index,
        "owner_scope_key": native_scope_identity_sha256({
            "source_scope_sha256": "c" * 64,
            "body_type": 0,
            "body_index": body_index,
        }),
        "persistent": False,
        "rule_version": NATIVE_SCOPE_IDENTITY_RULE,
        "object_indices": [body_index] if kind == "body" else [0, 1],
        "field": IDENTITY_FIELDS[kind],
        "source_scope_sha256": "c" * 64,
    }
    return [{
        "code": NATIVE_SCOPE_LOCAL_IDENTITY_DERIVED,
        "object": f"part-1:{kind}:0/{body_index}",
        "detail": detail,
    }]


def quality_report(raw: list[dict], rows: list[dict] | None = None) -> dict:
    report = {
        "checks": [{
            "id": "geometry.native_tessellation",
            "state": "passed",
            "passed": True,
            "details": {"warnings": raw},
        }],
        "diagnostics": (
            native_gate_warning_diagnostics({"warnings": raw})
            if rows is None else rows
        ),
        "qualifications": [],
    }

    return json.loads(json.dumps(report))


def recorded_warnings(quality: dict) -> list[dict]:
    check = quality["checks"][0]
    return _recorded_warnings(
        {"id": "verify", "name": "Verify"},
        [],
        [{
            "id": check["id"],
            "state": check["state"],
            "raw_details": check["details"],
        }],
        quality.get("diagnostics"),
    )


class NativeScopeWarningProjectionTests(unittest.TestCase):
    def test_each_native_entity_keeps_its_source_and_field_in_every_report(self):
        for kind in IDENTITY_FIELDS:
            with self.subTest(kind=kind):
                raw = raw_warnings(kind)
                quality = quality_report(raw)
                expected = raw[0]["detail"]
                for surface, projected in (
                    ("portal", _quality_warnings(quality)),
                    ("view", recorded_warnings(quality)),
                ):
                    with self.subTest(surface=surface):
                        self.assertEqual(len(projected), 1)
                        row = projected[0]
                        detail = row.get("detail", row)
                        self.assertEqual(
                            {key: detail[key] for key in NATIVE_SCOPE_WARNING_DETAIL_FIELDS},
                            expected,
                        )
                        self.assertEqual(row["object"], raw[0]["object"])
                        self.assertEqual(row["code"], NATIVE_SCOPE_LOCAL_IDENTITY_DERIVED)
                        self.assertNotIn(None, expected.values())

    def test_missing_or_invalid_location_is_rejected_at_the_native_gate(self):
        original = raw_warnings()[0]
        legacy = {key: value for key, value in original.items() if key != "detail"}
        legacy.update(original["detail"])
        with self.assertRaises(PipelineError):
            native_gate_warning_diagnostics({"warnings": [legacy]})
        for key in NATIVE_SCOPE_WARNING_DETAIL_FIELDS:
            with self.subTest(missing=key):
                row = copy.deepcopy(original)
                row["detail"].pop(key)
                with self.assertRaises(PipelineError):
                    native_gate_warning_diagnostics({"warnings": [row]})
        for key, value in (
            ("body_type", True),
            ("body_index", -1),
            ("entity_kind", "unknown"),
            ("owner_scope_key", "invalid"),
            ("owner_scope_key", "a" * 64),
            ("persistent", True),
            ("rule_version", "unsupported"),
            ("object_indices", []),
            ("object_indices", [0, 0]),
            ("object_indices", [[0]]),
            ("field", "unrelated_field"),
            ("source_scope_sha256", "invalid"),
            ("source_scope_sha256", "d" * 64),
        ):
            with self.subTest(field=key, value=value):
                row = copy.deepcopy(original)
                row["detail"][key] = value
                with self.assertRaises(PipelineError):
                    native_gate_warning_diagnostics({"warnings": [row]})

    def assert_rejected_by_reports(self, quality):
        with self.assertRaises(PortalError):
            _quality_warnings(quality)
        with self.assertRaises(PipelineError):
            recorded_warnings(quality)

    def test_reports_reject_forged_objects_and_changed_source_binding(self):
        raw = raw_warnings("face")
        quality = quality_report(raw)
        forged = copy.deepcopy(quality)
        forged["diagnostics"][0]["object"] = "part-1:face:0/9"
        self.assert_rejected_by_reports(forged)
        changed = copy.deepcopy(quality)
        changed["diagnostics"][0]["detail"]["source_scope_sha256"] = "9" * 64
        self.assert_rejected_by_reports(changed)

    def test_malformed_raw_warning_preserves_reported_failure(self):
        for detail in (None, "invalid", []):
            with self.subTest(detail=detail):
                quality = quality_report(raw_warnings())
                quality["checks"][0]["details"]["warnings"][0]["detail"] = detail
                self.assert_rejected_by_reports(quality)

    def test_reports_reject_omitted_or_forged_non_scope_native_warnings(self):
        for code in sorted(NATIVE_GATE_WARNING_CODES - {NATIVE_SCOPE_LOCAL_IDENTITY_DERIVED}):
            with self.subTest(code=code):
                raw = [*raw_warnings(), {
                    "code": code,
                    "object": "part-1:body:0/1",
                    "message": "A recorded native field was derived",
                    "detail": {"component": "part-1", "field": "derived_field"},
                }]
                quality = quality_report(raw)
                self.assertEqual(len(recorded_warnings(quality)), 2)
                omitted = copy.deepcopy(quality)
                omitted["diagnostics"].pop()
                self.assert_rejected_by_reports(omitted)
                forged = copy.deepcopy(quality)
                forged["diagnostics"][-1]["detail"]["field"] = "another_field"
                self.assert_rejected_by_reports(forged)

    def test_reports_reject_an_omitted_warning_group(self):
        raw = [*raw_warnings("face", 1), *raw_warnings("face", 2)]
        quality = quality_report(raw)
        self.assertTrue(native_gate_warning_details_match(quality["diagnostics"], raw))
        quality["diagnostics"] = quality["diagnostics"][:1]
        self.assertFalse(native_gate_warning_details_match(quality["diagnostics"], raw))
        self.assert_rejected_by_reports(quality)

    def test_reports_reject_both_warning_projections_removed_from_a_passed_gate(self):
        self.assert_rejected_by_reports(quality_report([], []))
        with self.assertRaises(PipelineError):
            native_gate_warning_diagnostics({"warnings": []})

    def test_reports_reject_a_passed_native_gate_without_diagnostics(self):
        for raw in ([], raw_warnings()):
            with self.subTest(raw=raw):
                quality = quality_report(raw, [])
                quality.pop("diagnostics")
                self.assert_rejected_by_reports(quality)

    def test_a_failed_native_gate_does_not_invent_scope_identity_warnings(self):
        quality = quality_report([], [])
        quality["checks"][0].update(state="failed", passed=False)
        self.assertEqual(_quality_warnings(quality), [])
        self.assertEqual(recorded_warnings(quality), [])

    def test_a_passed_native_gate_requires_its_explicit_warning_list(self):
        for details in ({}, {"warnings": "not-a-list"}):
            with self.subTest(details=details):
                quality = quality_report([], [])
                quality["checks"][0]["details"] = details
                self.assert_rejected_by_reports(quality)


if __name__ == "__main__":
    unittest.main()
