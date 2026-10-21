"""Native scope warning projection must reach every release-facing report."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from description_pipeline.geometry.native_u import (
    NATIVE_SCOPE_IDENTITY_WARNING_CODES,
    NATIVE_SCOPE_IDENTITY_RULE,
    NATIVE_SCOPE_WARNING_DETAIL_FIELDS,
    native_scope_warning_details_match,
)
from description_pipeline.io import PipelineError, write_json
from description_pipeline.orchestration.portal import (
    PortalError,
    _quality_warnings,
)
from description_pipeline.orchestration.report_view import _recorded_warnings
from description_pipeline.repository.urdf_pr import _body
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
                _body("a" * 64, "b" * 40, quality, "engineering")
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

        body = _body("a" * 64, "b" * 40, quality, "engineering")
        self.assertTrue(
            all(
                code in body and "object_indices=[0,2]" in body
                for code in NATIVE_SCOPE_IDENTITY_WARNING_CODES
            )
        )
        self.assertIn("persistent=false", body)

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
            )

        encoded = json.dumps(forged, sort_keys=True)
        self.assertIn("part-1:face:0/9", encoded)

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
