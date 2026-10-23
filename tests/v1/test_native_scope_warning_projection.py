"""Native scope warning projection must reach every release-facing report."""

from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from description_pipeline.geometry.native_u import (
    NATIVE_SCOPE_IDENTITY_WARNING_CODES,
    NATIVE_SCOPE_IDENTITY_RULE,
    NATIVE_SCOPE_WARNING_DETAIL_FIELDS,
    NATIVE_TESSELLATION_EDGE_PROJECTION_TOLERANCE_M,
    NATIVE_TOLERANT_EDGE_AFFECTED_FIELDS,
    NATIVE_TOLERANT_EDGE_BOUNDARY_RULE,
    NATIVE_TOLERANT_EDGE_EVALUATE_METRIC,
    NATIVE_TOLERANT_EDGE_GAP_SOURCE,
    NATIVE_TOLERANT_EDGE_GAP_WARNING,
    NATIVE_TOLERANT_EDGE_PROJECTION_METRIC,
    NATIVE_TOLERANT_EDGE_WARNING_REQUIRED_FIELDS,
    NATIVE_TOLERANT_EDGE_WARNING_DETAIL_FIELDS,
    NATIVE_TOLERANT_EDGE_WARNING_QUALIFICATION,
    NATIVE_TOLERANT_EDGE_WARNING_REASON,
    native_scope_warning_details_match,
    native_tolerant_edge_warning_details_match,
)
from description_pipeline.io import PipelineError, canonical_json_value, write_json
from description_pipeline.orchestration import portal as portal_module
from description_pipeline.orchestration.portal import (
    PortalError,
    _quality_warnings,
)
from description_pipeline.orchestration.report_view import _recorded_warnings
from description_pipeline.repository.urdf_pr import _body
from description_pipeline.repository import urdf_pr as urdf_pr_module
from description_pipeline.structural_report import build_structural_report
from description_pipeline.verification.solidworks_urdf import (
    native_gate_warning_diagnostics,
)

BODY_OPTIONS = {
    "slug": "example/m3.0",
    "quality_sha256": "c" * 64,
}


def raw_warnings() -> list[dict]:
    """The exact grouped warning shape emitted by the native parser."""

    warnings = []
    for code in sorted(NATIVE_SCOPE_IDENTITY_WARNING_CODES):
        row = {
                "code": code,
                "component": "part-1",
                "entity_kind": "face",
                "body_type": 0,
                "body_index": 1,
                "owner_scope_key": "a" * 64,
                "persistent": False,
                "rule_version": NATIVE_SCOPE_IDENTITY_RULE,
                "object_indices": [0, 2],
                "field": "identity.warnings",
                "native_reference_sha256": "b" * 64,
                "source_document_sha256": "c" * 64,
                "referenced_configuration": "Default",
                "occurrence_scope": "part-1",
                "body_name": "Body1",
                "mapping_source_part_document_sha256": "c" * 64,
                "mapping_source_body_reference_sha256": "d" * 64,
                "mapped_source_indices": [0, 2],
                "mapped_source_persistent_reference_sha256": [
                    "e" * 64,
                    "f" * 64,
                ],
                "topology_signature_sha256s": ["1" * 64, "2" * 64],
                "object": "part-1:face:0/1",
            }
        row["detail"] = {
            key: row[key]
            for key in NATIVE_SCOPE_WARNING_DETAIL_FIELDS
        }
        warnings.append(row)
    return warnings


def diagnostics() -> list[dict]:
    return native_gate_warning_diagnostics({"warnings": raw_warnings()})


def tolerant_warning() -> dict:
    return {
        "code": NATIVE_TOLERANT_EDGE_GAP_WARNING,
        "blocking": False,
        "component": "part-1",
        "body_type": 1,
        "body_index": 0,
        "edge_reference_sha256": "a" * 64,
        "fin_id": 329,
        "fin_endpoint": "end",
        "parameter_boundary": "u_max",
        "point_roundtrip_rule": NATIVE_TOLERANT_EDGE_BOUNDARY_RULE,
        "evaluate_rule_version": "native-curve-endpoint-float32-operation-v4",
        "evaluate_metric": NATIVE_TOLERANT_EDGE_EVALUATE_METRIC,
        "evaluate_point_error_m": 1e-12,
        "point_tolerance_m": 7.45e-9,
        "projection_metric": NATIVE_TOLERANT_EDGE_PROJECTION_METRIC,
        "projection_to_curve_l2_error_m": 4.98e-8,
        "eligible_gap_m": 5e-7,
        "gap_used": True,
        "composed_projection_tolerance_m": 5e-7,
        "gap_source_api": NATIVE_TOLERANT_EDGE_GAP_SOURCE,
        "gap_cap_m": NATIVE_TESSELLATION_EDGE_PROJECTION_TOLERANCE_M,
        "edge_tolerance_out_variant_type": 0x4005,
        "field": "end_evidence.tolerant_edge_evidence",
        "affected_fields": list(NATIVE_TOLERANT_EDGE_AFFECTED_FIELDS),
        "reason": NATIVE_TOLERANT_EDGE_WARNING_REASON,
        "qualification": NATIVE_TOLERANT_EDGE_WARNING_QUALIFICATION,
    }


def quality_report(
    rows: list[dict] | None = None,
    raw: list[dict] | None = None,
) -> dict:
    return {
        "checks": [
            {
                "id": "geometry.native_tessellation",
                "state": "passed",
                "passed": True,
                "details": {
                    "warnings": raw_warnings() if raw is None else raw,
                },
            }
        ],
        "diagnostics": diagnostics() if rows is None else rows,
        "qualifications": [],
    }


class NativeScopeWarningProjectionTests(unittest.TestCase):
    def test_tolerant_edge_warning_projection_is_exact_and_complete(self) -> None:
        raw = tolerant_warning()
        projected = native_gate_warning_diagnostics({"warnings": [raw]})

        self.assertEqual(len(projected), 1)
        self.assertEqual(projected[0]["code"], NATIVE_TOLERANT_EDGE_GAP_WARNING)
        self.assertEqual(
            set(projected[0]["detail"]),
            set(NATIVE_TOLERANT_EDGE_WARNING_DETAIL_FIELDS),
        )
        self.assertTrue(
            native_tolerant_edge_warning_details_match(projected, [raw])
        )
        self.assertFalse(
            native_tolerant_edge_warning_details_match([], [raw])
        )
        self.assertFalse(
            native_tolerant_edge_warning_details_match(
                [projected[0], projected[0]],
                [raw, raw],
            )
        )

        for field in sorted(NATIVE_TOLERANT_EDGE_WARNING_REQUIRED_FIELDS):
            with self.subTest(missing=field):
                changed = copy.deepcopy(raw)
                changed.pop(field)
                with self.assertRaises(PipelineError):
                    native_gate_warning_diagnostics(
                        {"warnings": [changed]}
                    )

        for field, value in (
            ("evaluate_metric", NATIVE_TOLERANT_EDGE_PROJECTION_METRIC),
            ("projection_metric", NATIVE_TOLERANT_EDGE_EVALUATE_METRIC),
            ("edge_reference_sha256", "f" * 64),
            ("gap_used", False),
            ("point_roundtrip_rule", "forged"),
            ("field", "start_evidence.tolerant_edge_evidence"),
            ("affected_fields", []),
            (
                "affected_fields",
                ["point_tolerance_m", "point_tolerance_m"],
            ),
            ("affected_fields", ["point_tolerance_m"]),
            ("reason", ""),
            ("qualification", "forged"),
        ):
            with self.subTest(field=field):
                changed = native_gate_warning_diagnostics(
                    {"warnings": [raw]}
                )
                changed[0]["detail"][field] = value
                self.assertFalse(
                    native_tolerant_edge_warning_details_match(
                        changed,
                        [raw],
                    )
                )

        quality = quality_report(projected, [raw])
        rows = [
            {
                "id": "geometry.native_tessellation",
                "state": "passed",
                "raw_details": {"warnings": [raw]},
            }
        ]
        report_view_rows = _recorded_warnings(
            {"id": "verify", "name": "Verify"},
            [],
            rows,
            projected,
        )
        self.assertEqual(
            report_view_rows[0]["code"],
            NATIVE_TOLERANT_EDGE_GAP_WARNING,
        )
        self.assertEqual(
            report_view_rows[0]["detail"],
            projected[0]["detail"],
        )
        self.assertEqual(
            set(report_view_rows[0]["detail"]),
            set(NATIVE_TOLERANT_EDGE_WARNING_DETAIL_FIELDS),
        )
        ordered_rows = [
            {
                "id": "source.other",
                "state": "passed",
                "raw_details": {
                    "warnings": [
                        {
                            "code": "other.warning",
                            "blocking": False,
                        }
                    ]
                },
            },
            *rows,
        ]
        ordered = _recorded_warnings(
            {"id": "verify", "name": "Verify"},
            [],
            ordered_rows,
            projected,
        )
        self.assertEqual(
            [row["code"] for row in ordered],
            ["other.warning", NATIVE_TOLERANT_EDGE_GAP_WARNING],
        )
        portal_warning = _quality_warnings(quality)[0]
        self.assertEqual(
            portal_warning["code"],
            NATIVE_TOLERANT_EDGE_GAP_WARNING,
        )
        self.assertEqual(portal_warning["detail"], projected[0]["detail"])
        self.assertEqual(
            urdf_pr_module._warning_diagnostics(quality)[0]["code"],
            NATIVE_TOLERANT_EDGE_GAP_WARNING,
        )
        pr_rows = urdf_pr_module._general_warning_rows(quality)
        pr_tolerant, = [
            row
            for row in pr_rows
            if row["code"] == NATIVE_TOLERANT_EDGE_GAP_WARNING
        ]
        self.assertEqual(pr_tolerant["detail"], projected[0]["detail"])

        forged = native_gate_warning_diagnostics({"warnings": [raw]})
        forged[0]["detail"]["evaluate_metric"] = (
            NATIVE_TOLERANT_EDGE_PROJECTION_METRIC
        )
        with self.assertRaises(PipelineError):
            _recorded_warnings(
                {"id": "verify", "name": "Verify"},
                [],
                rows,
                forged,
            )
        with self.assertRaises((PipelineError, PortalError)):
            _quality_warnings(quality_report(forged, [raw]))
        with self.assertRaises(PipelineError):
            urdf_pr_module._warning_diagnostics(quality_report(forged, [raw]))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_json(
                root / "reports/quality.json",
                quality_report(forged, [raw]),
            )
            structural = build_structural_report({}, evidence_root=root)
        self.assertIn(
            "report.native_tolerant_edge_warning_invalid",
            {row["code"] for row in structural["findings"]},
        )

        without_diagnostics = quality_report(projected, [raw])
        without_diagnostics.pop("diagnostics")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_json(
                root / "reports/quality.json",
                without_diagnostics,
            )
            structural = build_structural_report({}, evidence_root=root)
        self.assertIn(
            "report.native_tolerant_edge_warning_invalid",
            {row["code"] for row in structural["findings"]},
        )

    def test_warning_consumers_share_canonical_serialization_and_body_hash(self) -> None:
        self.assertIs(portal_module.canonical_json_value, canonical_json_value)
        self.assertIs(urdf_pr_module.canonical_json_value, canonical_json_value)

        quality = quality_report(diagnostics())
        portal_rows = [
            {key: value for key, value in row.items() if key != "source"}
            for row in _quality_warnings(quality)
        ]
        self.assertEqual(portal_rows, urdf_pr_module._warning_diagnostics(quality))

        body = _body("a" * 64, "b" * 40, quality, "engineering", **BODY_OPTIONS)
        self.assertEqual(
            hashlib.sha256(body.encode("utf-8")).hexdigest(),
            "754f87f4c84e34578501d8c3cfc2b2a039b07fe5c7f4c83da1c8d4538e3ae2ca",
        )
        self.assertEqual(
            canonical_json_value(portal_rows),
            json.dumps(
                portal_rows,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ),
        )

    def test_report_view_compares_tolerant_gate_only_on_native_row(self) -> None:
        raw = tolerant_warning()
        projected = native_gate_warning_diagnostics({"warnings": [raw]})
        rows = [
            {
                "id": "source.earlier",
                "state": "passed",
                "raw_details": {
                    "warnings": [
                        {
                            "code": "source.earlier.warning",
                            "blocking": False,
                        }
                    ]
                },
            },
            {
                "id": "geometry.native_tessellation",
                "state": "passed",
                "raw_details": {"warnings": [raw]},
            },
        ]

        warnings = _recorded_warnings(
            {"id": "verify", "name": "Verify"},
            [],
            rows,
            projected,
        )

        self.assertEqual(
            [row["code"] for row in warnings],
            ["source.earlier.warning", NATIVE_TOLERANT_EDGE_GAP_WARNING],
        )

    def test_quality_gate_promotes_all_four_complete_warnings(self) -> None:
        rows = diagnostics()

        self.assertEqual(
            {row["code"] for row in rows},
            NATIVE_SCOPE_IDENTITY_WARNING_CODES,
        )
        self.assertTrue(
            all(
                row["blocking"] is False
                and row["severity"] == "warning"
                and row["object"] == "part-1:face:0/1"
                and row["detail"]["object_indices"] == [0, 2]
                and row["detail"]["field"] == "identity.warnings"
                for row in rows
            )
        )

    def test_quality_gate_rejects_partial_or_unlocated_warning_sets(self) -> None:
        partial = {"warnings": raw_warnings()[:1]}
        with self.assertRaisesRegex(PipelineError, "incomplete"):
            native_gate_warning_diagnostics(partial)

        unlocated = copy.deepcopy(raw_warnings())
        unlocated[0]["detail"].pop("owner_scope_key")
        with self.assertRaisesRegex(PipelineError, "incomplete"):
            native_gate_warning_diagnostics({"warnings": unlocated})

        for missing in (
            "native_reference_sha256",
            "source_document_sha256",
            "mapping_source_part_document_sha256",
            "mapping_source_body_reference_sha256",
            "mapped_source_indices",
            "mapped_source_persistent_reference_sha256",
            "topology_signature_sha256s",
        ):
            malformed = copy.deepcopy(raw_warnings())
            malformed[0]["detail"].pop(missing)
            with self.subTest(missing=missing), self.assertRaises(PipelineError):
                native_gate_warning_diagnostics({"warnings": malformed})

        for field, value in (
            ("object_indices", []),
            ("mapped_source_indices", []),
            ("mapped_source_persistent_reference_sha256", []),
            ("topology_signature_sha256s", []),
            ("topology_signature_sha256s", ["1" * 64]),
            ("mapped_source_indices", [0]),
        ):
            malformed = copy.deepcopy(raw_warnings())
            malformed[0]["detail"][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(
                PipelineError
            ):
                native_gate_warning_diagnostics({"warnings": malformed})

        blocking = copy.deepcopy(raw_warnings())
        blocking[0]["blocking"] = True
        with self.assertRaises(PipelineError):
            native_gate_warning_diagnostics({"warnings": blocking})

        duplicate = [*raw_warnings(), copy.deepcopy(raw_warnings()[0])]
        with self.assertRaises(PipelineError):
            native_gate_warning_diagnostics({"warnings": duplicate})

        complete = diagnostics()
        for key, value in (
            ("severity", "error"),
            ("message", "forged"),
        ):
            forged = copy.deepcopy(complete)
            forged[0][key] = value
            self.assertFalse(
                native_scope_warning_details_match(
                    forged,
                    raw_warnings(),
                )
            )

        for field in (
            "referenced_configuration",
            "occurrence_scope",
            "body_name",
        ):
            nulled = copy.deepcopy(raw_warnings())
            nulled[0]["detail"][field] = None
            with self.subTest(face_null=field), self.assertRaises(
                PipelineError
            ):
                native_gate_warning_diagnostics({"warnings": nulled})

    def test_each_owner_group_requires_all_four_identical_warning_codes(
        self,
    ) -> None:
        first = raw_warnings()
        second = copy.deepcopy(first)
        for row in second:
            row["owner_scope_key"] = "9" * 64
            row["object_indices"] = [4, 5]
            row["mapped_source_indices"] = [4, 5]
            row["mapped_source_persistent_reference_sha256"] = [
                "7" * 64,
                "8" * 64,
            ]
            row["topology_signature_sha256s"] = [
                "3" * 64,
                "4" * 64,
            ]
            row["detail"] = {
                key: row[key]
                for key in NATIVE_SCOPE_WARNING_DETAIL_FIELDS
            }
            row["object"] = "part-1:face:0/1"
        raw = [*first, *second]
        projected = native_gate_warning_diagnostics({"warnings": raw})
        self.assertEqual(len(projected), 8)
        self.assertTrue(
            native_scope_warning_details_match(projected, raw)
        )
        self.assertFalse(
            native_scope_warning_details_match(projected[:4], raw)
        )
        partial_quality = quality_report(projected[:4], raw)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_json(
                root / "reports/quality.json",
                partial_quality,
            )
            structural = build_structural_report({}, evidence_root=root)
        self.assertIn(
            "report.native_identity_warning_invalid",
            {row["code"] for row in structural["findings"]},
        )
        with self.assertRaises(PortalError):
            _quality_warnings(partial_quality)

    def test_unique_persistent_path_emits_no_scope_warning(self) -> None:
        self.assertEqual(
            native_gate_warning_diagnostics({"warnings": []}),
            [],
        )

        raw = raw_warnings()
        quality = quality_report([], raw)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_json(root / "reports/quality.json", quality)
            structural = build_structural_report({}, evidence_root=root)
        self.assertIn(
            "report.native_identity_warning_invalid",
            {row["code"] for row in structural["findings"]},
        )
        with self.assertRaises(PortalError):
            _quality_warnings(quality)
        with self.assertRaises(PipelineError):
            _body("a" * 64, "b" * 40, quality, "engineering", **BODY_OPTIONS)

    def test_a_passed_gate_must_explicitly_carry_its_warning_list(
        self,
    ) -> None:
        for details in ({}, {"warnings": "not-a-list"}):
            quality = {
                "checks": [
                    {
                        "id": "geometry.native_tessellation",
                        "state": "passed",
                        "passed": True,
                        "details": details,
                    }
                ],
                "diagnostics": [],
                "qualifications": [],
            }
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                write_json(root / "reports/quality.json", quality)
                structural = build_structural_report(
                    {},
                    evidence_root=root,
                )
            self.assertIn(
                "report.native_identity_warning_invalid",
                {row["code"] for row in structural["findings"]},
            )
            with self.assertRaises(PortalError):
                _quality_warnings(quality)
            with self.assertRaises(PipelineError):
                _body("a" * 64, "b" * 40, quality, "engineering", **BODY_OPTIONS)
            with self.assertRaises(PipelineError):
                _recorded_warnings(
                    {"id": "verify", "name": "Verify"},
                    [],
                    [
                        {
                            "id": "geometry.native_tessellation",
                            "state": "passed",
                            "raw_details": details,
                        }
                    ],
                    [],
                )
            with self.assertRaises(PipelineError):
                _recorded_warnings(
                    {"id": "verify", "name": "Verify"},
                    [],
                    [
                        {
                            "id": "geometry.native_tessellation",
                            "passed": True,
                            "raw_details": {},
                        }
                    ],
                    [],
                )

    def test_structural_portal_report_view_and_pr_keep_object_location(
        self,
    ) -> None:
        rows = diagnostics()
        quality = quality_report(rows)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_json(root / "reports/quality.json", quality)
            structural = build_structural_report({}, evidence_root=root)

        findings = {
            row["code"]: row
            for row in structural["findings"]
            if row["code"] in NATIVE_SCOPE_IDENTITY_WARNING_CODES
        }
        self.assertEqual(
            set(findings),
            NATIVE_SCOPE_IDENTITY_WARNING_CODES,
        )
        self.assertTrue(
            all(
                row["severity"] == "warning"
                and row["object"] == "part-1:face:0/1"
                and row["component"] == "part-1"
                and row["entity_kind"] == "face"
                and row["body_type"] == 0
                and row["body_index"] == 1
                and row["object_indices"] == [0, 2]
                and row["field"] == "identity.warnings"
                for row in findings.values()
            )
        )

        portal = _quality_warnings(quality)
        self.assertEqual(
            {row["code"] for row in portal},
            NATIVE_SCOPE_IDENTITY_WARNING_CODES,
        )
        self.assertTrue(
            all(
                row["object"] == "part-1:face:0/1"
                and row["detail"]["object_indices"] == [0, 2]
                for row in portal
            )
        )

        view = _recorded_warnings(
            {"id": "verify", "name": "Verify"},
            [],
            [
                {
                    "id": "geometry.native_tessellation",
                    "state": "passed",
                    "raw_details": {"warnings": raw_warnings()},
                }
            ],
            rows,
        )
        self.assertEqual(
            {row["code"] for row in view},
            NATIVE_SCOPE_IDENTITY_WARNING_CODES,
        )
        self.assertTrue(
            all(
                row["object"] == "part-1:face:0/1"
                and row["object_indices"] == [0, 2]
                and row["field"] == "identity.warnings"
                for row in view
            )
        )

        body = _body("a" * 64, "b" * 40, quality, "engineering", **BODY_OPTIONS)
        self.assertIn(
            f"warning codes: total={len(NATIVE_SCOPE_IDENTITY_WARNING_CODES)}; "
            f"shown={len(NATIVE_SCOPE_IDENTITY_WARNING_CODES)}; omitted=0",
            body,
        )
        self.assertTrue(all(f"`{code}` groups=1 rows=1" in body for code in NATIVE_SCOPE_IDENTITY_WARNING_CODES))
        self.assertNotIn("- general warning ", body)
        self.assertNotIn("object_indices=", body)
        self.assertNotIn("native scope identity", body)

    def test_downstream_projection_refuses_forged_objects(self) -> None:
        forged = diagnostics()
        forged[0]["object"] = "part-1:face:0/9"

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_json(
                root / "reports/quality.json",
                quality_report(forged),
            )
            structural = build_structural_report({}, evidence_root=root)
        self.assertIn(
            "report.native_identity_warning_invalid",
            {row["code"] for row in structural["findings"]},
        )

        quality = quality_report(forged)
        with self.assertRaises(PortalError):
            _quality_warnings(quality)
        with self.assertRaisesRegex(PipelineError, "incomplete or forged"):
            _body(
                "a" * 64,
                "b" * 40,
                quality,
                "engineering",
                **BODY_OPTIONS,
            )

        encoded = json.dumps(forged, sort_keys=True)
        self.assertIn("part-1:face:0/9", encoded)

        missing_face_identity = diagnostics()
        raw_missing_face_identity = raw_warnings()
        for row in (*missing_face_identity, *raw_missing_face_identity):
            detail = row["detail"]
            detail["referenced_configuration"] = None
            detail["occurrence_scope"] = None
            detail["body_name"] = None
        missing_quality = quality_report(
            missing_face_identity,
            raw_missing_face_identity,
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_json(
                root / "reports/quality.json",
                missing_quality,
            )
            structural = build_structural_report({}, evidence_root=root)
        self.assertIn(
            "report.native_identity_warning_invalid",
            {row["code"] for row in structural["findings"]},
        )
        with self.assertRaises(PortalError):
            _quality_warnings(missing_quality)
        with self.assertRaises(PipelineError):
            _body(
                "a" * 64,
                "b" * 40,
                missing_quality,
                "engineering",
                **BODY_OPTIONS,
            )

    def test_report_view_rejects_orphan_groups_and_partial_fields(self) -> None:
        first = raw_warnings()
        second = copy.deepcopy(first)
        for row in second:
            row["owner_scope_key"] = "9" * 64
            row["object_indices"] = [4, 5]
            row["mapped_source_indices"] = [4, 5]
            row["mapped_source_persistent_reference_sha256"] = [
                "7" * 64,
                "8" * 64,
            ]
            row["topology_signature_sha256s"] = [
                "3" * 64,
                "4" * 64,
            ]
            row["detail"] = {
                key: row[key]
                for key in NATIVE_SCOPE_WARNING_DETAIL_FIELDS
            }
        raw = [*first, *second]
        complete = native_gate_warning_diagnostics({"warnings": raw})
        with self.assertRaisesRegex(PipelineError, "differ"):
            _recorded_warnings(
                {"id": "verify", "name": "Verify"},
                [],
                [
                    {
                        "id": "geometry.native_tessellation",
                        "state": "passed",
                        "raw_details": {"warnings": raw},
                    }
                ],
                complete[:4],
            )

        missing = copy.deepcopy(raw)
        missing[0]["detail"].pop("topology_signature_sha256s")
        forged = copy.deepcopy(complete)
        forged[0]["detail"].pop("topology_signature_sha256s")
        with self.assertRaisesRegex(PipelineError, "differ"):
            _recorded_warnings(
                {"id": "verify", "name": "Verify"},
                [],
                [
                    {
                        "id": "geometry.native_tessellation",
                        "state": "passed",
                        "raw_details": {"warnings": missing},
                    }
                ],
                forged,
            )

        with self.assertRaisesRegex(PipelineError, "differ"):
            _recorded_warnings(
                {"id": "verify", "name": "Verify"},
                [],
                [
                    {
                        "id": "geometry.native_tessellation",
                        "state": "passed",
                        "raw_details": {"warnings": first},
                    }
                ],
                complete[:1],
            )


if __name__ == "__main__":
    unittest.main()
