"""Native scope warning projection must reach every release-facing report."""

from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from xml.etree import ElementTree as ET

from description_pipeline.backends.urdf import generate_urdf
from description_pipeline.geometry.native_u import (
    NATIVE_SCOPE_IDENTITY_WARNING_CODES,
    NATIVE_SCOPE_IDENTITY_RULE,
    NATIVE_SCOPE_WARNING_DETAIL_FIELDS,
    NATIVE_OPEN_EDGE_BOUNDARY_IDENTITY_RULE,
    NATIVE_OPEN_EDGE_SCOPE_RULE,
    NATIVE_TESSELLATION_EDGE_PROJECTION_TOLERANCE_M,
    NATIVE_TOLERANT_EDGE_AFFECTED_FIELDS,
    NATIVE_TOLERANT_EDGE_BOUNDARY_RULE,
    NATIVE_TOLERANT_EDGE_EVALUATE_METRIC,
    NATIVE_TOLERANT_EDGE_GAP_SOURCE,
    NATIVE_TOLERANT_EDGE_GAP_WARNING,
    NATIVE_TOLERANT_EDGE_PROJECTION_METRIC,
    NATIVE_TOLERANT_EDGE_WARNING_REQUIRED_FIELDS,
    NATIVE_TOLERANT_EDGE_WARNING_DETAIL_FIELDS,
    NATIVE_TOLERANT_EDGE_WARNING_MESSAGE,
    NATIVE_TOLERANT_EDGE_WARNING_QUALIFICATION,
    NATIVE_TOLERANT_EDGE_WARNING_REASON,
    native_scope_warning_details_match,
    native_curve_topology_endpoint_set_assignment,
    native_tolerant_edge_value_warning,
    native_tolerant_edge_warning_details_match,
)
from description_pipeline.io import PipelineError, canonical_json_value, write_json
from description_pipeline.model import EngineeringModel
from description_pipeline.orchestration import portal as portal_module
from description_pipeline.orchestration.portal import (
    PortalError,
    _quality_warnings,
)
from description_pipeline.orchestration.report_view import _recorded_warnings
from description_pipeline.structural_report import build_structural_report
from description_pipeline.verification.solidworks_urdf import (
    native_gate_warning_diagnostics,
)

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


def tolerant_source_scope() -> dict:
    source_sha256 = hashlib.sha256(b"native-source").hexdigest()
    copy_sha256 = hashlib.sha256(b"frozen-copy").hexdigest()
    return {
        "rule_version": NATIVE_OPEN_EDGE_SCOPE_RULE,
        "component": "part-1",
        "source_document_path": "C:/staged/source/Part.SLDPRT",
        "source_copy_path": "source/Part.SLDPRT",
        "source_document_sha256": source_sha256,
        "source_copy_sha256": copy_sha256,
        "source_document_identity_sha256": source_sha256,
        "source_document_identity_kind": "file_sha256",
        "referenced_configuration": "Default",
        "occurrence_scope": "part-1",
        "virtual_owner": None,
        "virtual_owner_sha256": None,
        "virtual_session_document_path": None,
        "virtual_session_temp_root": None,
        "virtual_native_api_pid": None,
    }


def tolerant_warning() -> dict:
    topology = native_curve_topology_endpoint_set_assignment(
        (0.0, 0.0, 0.0),
        (1e-7, 0.0, 0.0),
        (0.0, 0.0, 0.0),
        (1e-7, 0.0, 0.0),
    )
    return {
        "code": NATIVE_TOLERANT_EDGE_GAP_WARNING,
        "blocking": False,
        "message": NATIVE_TOLERANT_EDGE_WARNING_MESSAGE,
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
        "topology_endpoint_set": topology,
        "source_scope": tolerant_source_scope(),
        "open_edge_boundary_identity_sha256": hashlib.sha256(
            NATIVE_OPEN_EDGE_BOUNDARY_IDENTITY_RULE.encode("ascii")
        ).hexdigest(),
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

    def test_tolerant_warning_is_a_complete_urdf_value_warning(self) -> None:
        raw = tolerant_warning()
        value_warning, = native_tolerant_edge_value_warning([raw])

        self.assertEqual(value_warning["code"], NATIVE_TOLERANT_EDGE_GAP_WARNING)
        self.assertEqual(
            value_warning["message"],
            NATIVE_TOLERANT_EDGE_WARNING_MESSAGE,
        )
        self.assertEqual(len(value_warning["objects"]), 1)
        subject = value_warning["objects"][0]
        self.assertEqual(subject["object_type"], "native_tessellation_fin_endpoint")
        self.assertEqual(subject["component"], "part-1")
        self.assertEqual(
            subject["topology_endpoint_set"],
            raw["topology_endpoint_set"],
        )
        self.assertEqual(subject["source_scope"], raw["source_scope"])
        self.assertEqual(
            subject["open_edge_boundary_identity_sha256"],
            raw["open_edge_boundary_identity_sha256"],
        )

        changed = copy.deepcopy(raw)
        changed["source_scope"]["virtual_owner"] = "Foreign.SLDASM"
        with self.assertRaises(ValueError):
            native_tolerant_edge_value_warning([changed])

        source = Path(__file__).resolve().parents[1] / "fixtures/v1/mesh-source"
        scene = json.loads(
            (source / "scene.json").read_text(encoding="utf-8")
        )
        scene["name"] = "native_warning"
        scene["provenance"]["value_warnings"] = [value_warning]
        with tempfile.TemporaryDirectory() as directory:
            output = generate_urdf(
                EngineeringModel.from_dict(scene),
                source,
                Path(directory),
            )
            parser = ET.XMLParser(target=ET.TreeBuilder(insert_comments=True))
            document = ET.fromstring(output.read_bytes(), parser=parser)
        comments = [
            child.text or ""
            for child in document
            if child.tag is ET.Comment
        ]
        self.assertEqual(len(comments), 1)
        self.assertIn(
            f"WARNING {NATIVE_TOLERANT_EDGE_GAP_WARNING}:",
            comments[0],
        )
        self.assertIn(
            subject["open_edge_boundary_identity_sha256"],
            comments[0],
        )
        self.assertIn('"source_copy_path": "source/Part.SLDPRT"', comments[0])

    def test_tolerant_edge_warning_identity_keeps_part_occurrences_distinct(
        self,
    ) -> None:
        first = tolerant_warning()
        second = copy.deepcopy(first)
        second["component"] = "part-2"
        second["body_index"] = 1
        second["source_scope"]["component"] = "part-2"
        second["source_scope"]["occurrence_scope"] = "part-2"

        projected = native_gate_warning_diagnostics(
            {"warnings": [first, second]}
        )

        self.assertTrue(
            native_tolerant_edge_warning_details_match(
                projected,
                [first, second],
            )
        )
        self.assertFalse(
            native_tolerant_edge_warning_details_match(
                [projected[0], copy.deepcopy(projected[0])],
                [first, copy.deepcopy(first)],
            )
        )

    def test_warning_consumers_share_canonical_serialization(self) -> None:
        self.assertIs(portal_module.canonical_json_value, canonical_json_value)

        quality = quality_report(diagnostics())
        portal_rows = [
            {key: value for key, value in row.items() if key != "source"}
            for row in _quality_warnings(quality)
        ]
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
