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
        warnings.append(
            {
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
        )
    return warnings


def diagnostics() -> list[dict]:
    return native_gate_warning_diagnostics({"warnings": raw_warnings()})


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
        unlocated[0].pop("owner_scope_key")
        with self.assertRaisesRegex(PipelineError, "object location"):
            native_gate_warning_diagnostics({"warnings": unlocated})

    def test_unique_persistent_path_emits_no_scope_warning(self) -> None:
        self.assertEqual(
            native_gate_warning_diagnostics({"warnings": []}),
            [],
        )

    def test_structural_portal_report_view_and_pr_keep_object_location(
        self,
    ) -> None:
        rows = diagnostics()
        quality = {
            "checks": [],
            "diagnostics": rows,
            "qualifications": [],
        }

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
            [{"raw_details": {"warnings": raw_warnings()}}],
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
                {
                    "checks": [],
                    "diagnostics": forged,
                    "qualifications": [],
                },
            )
            structural = build_structural_report({}, evidence_root=root)
        self.assertIn(
            "report.native_identity_warning_invalid",
            {row["code"] for row in structural["findings"]},
        )

        quality = {"diagnostics": forged}
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


if __name__ == "__main__":
    unittest.main()
