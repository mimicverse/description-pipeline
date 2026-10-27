from __future__ import annotations

import os
import json
import tempfile
import unittest
from pathlib import Path

from description_pipeline import interface_records as ir
from description_pipeline.delivery import (
    SUBJECT_DIRECTORIES,
    SUBJECT_FILES,
    read_delivery_manifest,
    read_source_record,
    subject_digest,
    subject_inventory,
    write_delivery_manifest,
)
from description_pipeline.io import PipelineError, digest
from description_pipeline.schemas import CHECK_POLICY_SCHEMA, SOURCE_RECORD_SCHEMA
from description_pipeline.stages import CONTRACT, CONTRACT_SHA256

HEX = "a" * 64
OTHER_HEX = "b" * 64


def source_record() -> dict:
    identity = {
        "kind": "source-revision",
        "handoff_digest": HEX,
        "main_assembly": "3.0 总装1008.SLDASM",
        "configuration": "Default",
        "hardware_id": "m3.0",
        "records": [{"reference": "components/waist/r2.json#drive", "file_sha256": OTHER_HEX}],
    }
    return {
        "schema_version": SOURCE_RECORD_SCHEMA,
        "source_revision_id": digest(identity),
        "identity": identity,
        "selection": {
            "main_assembly": identity["main_assembly"],
            "configuration": "Default",
            "hardware_id": "m3.0",
            "hardware_source": "cad",
        },
        "metadata": {
            "revision": "A.3",
            "owner": "结构组",
            "control": {"system": "pdm"},
            "change_summary": "首版",
            "change_summary_source": "cad",
        },
    }


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "delivery"
        for name in SUBJECT_DIRECTORIES:
            (self.root / name).mkdir(parents=True)
            (self.root / name / "subject.txt").write_text(name)
        for name in SUBJECT_FILES:
            (self.root / name).write_text(name)
        (self.root / "reports").mkdir()
        (self.root / "reports/verification-execution.json").write_text('{"passed": true}')
        (self.root / "input/source.json").write_text("{}")
        self.contract = {"schema_version": CONTRACT["schema_version"], "digest": CONTRACT_SHA256}
        self.policy = {"schema_version": CHECK_POLICY_SCHEMA, "digest": ir.check_policy_digest()}
        write_delivery_manifest(self.root, contract=self.contract, policy=self.policy)

    def test_subject_binds_assets_and_excludes_manifest_and_reports(self):
        files = subject_inventory(self.root)
        self.assertIn("tool.json", files)
        self.assertNotIn("manifest.json", files)
        self.assertNotIn("reports/verification-execution.json", files)
        before = subject_digest(self.root)
        (self.root / "reports/verification-execution.json").write_text('{"passed": false}')
        self.assertEqual(subject_digest(self.root), before)
        (self.root / "meshes/subject.txt").write_text("changed")
        self.assertNotEqual(subject_digest(self.root), before)

    def test_new_file_in_subject_directory_changes_identity(self):
        before = subject_digest(self.root)
        (self.root / "evidence/extra.json").write_text("{}")
        self.assertNotEqual(subject_digest(self.root), before)

    def test_missing_tool_record_fails(self):
        (self.root / "tool.json").unlink()
        with self.assertRaises(PipelineError):
            subject_digest(self.root)

    def test_manifest_roundtrip_rebinding_and_tamper_rejection(self):
        manifest = read_delivery_manifest(self.root)
        self.assertEqual(manifest["subject"]["digest"], subject_digest(self.root))
        (self.root / "model/subject.txt").write_text("changed")
        with self.assertRaisesRegex(PipelineError, "differ(s)? from manifest.json"):
            read_delivery_manifest(self.root)
        (self.root / "model/subject.txt").write_text("model")
        (self.root / "manifest.json").write_text(
            (self.root / "manifest.json").read_text().replace(manifest["subject"]["digest"], OTHER_HEX, 1)
        )
        with self.assertRaises(ir.InterfaceRecordError):
            read_delivery_manifest(self.root)

    def test_manifest_reader_rebinds_every_subject_record(self):
        manifest_path = self.root / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["subject"]["files"][0]["bytes"] += 1
        manifest_path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(PipelineError, "files differ"):
            read_delivery_manifest(self.root)
        write_delivery_manifest(self.root, contract=self.contract, policy=self.policy)
        (self.root / "evidence/extra.json").write_text("{}")
        with self.assertRaisesRegex(PipelineError, "files differ"):
            read_delivery_manifest(self.root)
        (self.root / "evidence/extra.json").unlink()
        target = self.root / "meshes/subject.txt"
        target.write_text("changed")
        with self.assertRaisesRegex(PipelineError, "files differ"):
            read_delivery_manifest(self.root)
        target.write_text("meshes")
        read_delivery_manifest(self.root)

    def test_source_record_reader_validates_content_identity(self):
        record = source_record()
        (self.root / "input/source.json").write_text(json.dumps(record, ensure_ascii=False))
        self.assertEqual(read_source_record(self.root)["source_revision_id"], record["source_revision_id"])
        record["selection"]["configuration"] = "Other"
        (self.root / "input/source.json").write_text(json.dumps(record, ensure_ascii=False))
        with self.assertRaises(ir.InterfaceRecordError):
            read_source_record(self.root)

    @unittest.skipIf(os.name == "nt", "symlink creation needs Windows privileges")
    def test_symlink_in_delivery_fails(self):
        target = self.root / "meshes/subject.txt"
        (self.root / "input/linked.txt").symlink_to(target)
        with self.assertRaises(PipelineError):
            subject_digest(self.root)

    @unittest.skipIf(os.name == "nt", "symlink creation needs Windows privileges")
    def test_symlinked_report_directory_fails(self):
        reports = self.root / "reports"
        reports.rename(self.root / "real-reports")
        reports.symlink_to(self.root / "real-reports", target_is_directory=True)
        with self.assertRaises(PipelineError):
            subject_digest(self.root)


if __name__ == "__main__":
    unittest.main()
