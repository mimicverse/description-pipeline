"""Native receipt integrity checks."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from description_pipeline.io import PipelineError, canonical, digest, write_json
from description_pipeline.verification.solidworks_urdf import _native_receipts_evidence
from tests.v1.test_stage_transfer import (
    HANDOFF,
    MAIN_ASSEMBLY,
    RUN_ID,
    capture_root,
    native_tool,
    seal,
)


class NativeReceiptTests(unittest.TestCase):
    def receipts(self) -> tuple[Path, dict[str, bytes], dict]:
        temporary = tempfile.TemporaryDirectory(prefix="native-receipts-")
        self.addCleanup(temporary.cleanup)
        workspace = Path(temporary.name)
        root = capture_root(
            workspace,
            run_id=RUN_ID,
            handoff=HANDOFF,
            main=MAIN_ASSEMBLY,
        )
        tool = native_tool()
        transfer = seal(
            root,
            workspace / "native-evidence.zip",
            run_id=RUN_ID,
            handoff_sha256=HANDOFF,
            main_assembly=MAIN_ASSEMBLY,
            native_tool=tool,
        )
        write_json(root / "tool.json", tool)
        return (
            root,
            {
                "native_tool_bytes": (root / "reports/native-tool.json").read_bytes(),
                "native_stages_bytes": (root / "reports/native-stages.json").read_bytes(),
                "transfer_manifest_bytes": canonical(transfer),
            },
            transfer,
        )

    def test_receipt_triplet_is_exactly_cross_bound(self) -> None:
        root, receipts, _transfer = self.receipts()
        evidence = _native_receipts_evidence(root, receipts)
        self.assertEqual(evidence["native_tool_sha256"], digest(evidence["native_tool"]))

    def test_tampered_native_tool_or_stage_bytes_are_rejected(self) -> None:
        for key in ("native_tool_bytes", "native_stages_bytes"):
            with self.subTest(key=key):
                root, receipts, _transfer = self.receipts()
                payload = json.loads(receipts[key])
                payload["tampered"] = True
                receipts[key] = canonical(payload)
                with self.assertRaises(PipelineError):
                    _native_receipts_evidence(root, receipts)

    def test_transfer_identity_must_match_the_tool_bytes(self) -> None:
        root, receipts, transfer = self.receipts()
        transfer["native_tool_sha256"] = "f" * 64
        receipts["transfer_manifest_bytes"] = canonical(transfer)
        with self.assertRaisesRegex(PipelineError, "native tool digest"):
            _native_receipts_evidence(root, receipts)

    def test_transfer_schema_inventory_total_and_stage_events_are_semantic(self) -> None:
        mutations = (
            ("schema", lambda transfer: transfer.update(schema_version="unknown"), "unknown schema"),
            ("file_count", lambda transfer: transfer.update(file_count=transfer["file_count"] + 1), "file count"),
            (
                "file_count_type",
                lambda transfer: transfer.update(file_count=float(transfer["file_count"])),
                "file count",
            ),
            (
                "total_bytes",
                lambda transfer: transfer.update(total_bytes=transfer["total_bytes"] + 1),
                "size differs",
            ),
            (
                "total_bytes_type",
                lambda transfer: transfer.update(total_bytes=float(transfer["total_bytes"])),
                "total bytes",
            ),
            ("unknown_field", lambda transfer: transfer.update(extra=True), "incomplete or unknown"),
            ("run_id", lambda transfer: transfer.update(run_id=RUN_ID + "-other"), "UUID"),
            (
                "execution_run",
                lambda transfer: transfer["execution"].update(
                    run_id=RUN_ID + "-other"
                ),
                "execution run differs",
            ),
        )
        for label, mutate, pattern in mutations:
            with self.subTest(case=label):
                root, receipts, transfer = self.receipts()
                mutate(transfer)
                receipts["transfer_manifest_bytes"] = canonical(transfer)
                with self.assertRaisesRegex(PipelineError, pattern):
                    _native_receipts_evidence(root, receipts)

        root, receipts, transfer = self.receipts()
        previous_stages_bytes = len(receipts["native_stages_bytes"])
        stages = json.loads(receipts["native_stages_bytes"])
        stages.pop("events")
        stages_blob = canonical(stages)
        transfer["total_bytes"] += len(stages_blob) - previous_stages_bytes
        transfer["files"]["reports/native-stages.json"] = hashlib.sha256(stages_blob).hexdigest()
        receipts["native_stages_bytes"] = stages_blob
        receipts["transfer_manifest_bytes"] = canonical(transfer)
        (root / "reports/native-stages.json").write_bytes(stages_blob)
        with self.assertRaisesRegex(PipelineError, "raw protocol events"):
            _native_receipts_evidence(root, receipts)

    def test_required_receipt_members_cannot_be_dropped_around_changed_blobs(self) -> None:
        root, receipts, transfer = self.receipts()
        original_lengths = {
            name: len(receipts[name])
            for name in ("native_tool_bytes", "native_stages_bytes")
        }
        transfer["files"].pop("reports/native-tool.json")
        transfer["files"].pop("reports/native-stages.json")
        transfer["file_count"] -= 2
        transfer["total_bytes"] -= sum(original_lengths.values())
        receipts["native_tool_bytes"] = b" " + receipts["native_tool_bytes"]
        receipts["transfer_manifest_bytes"] = canonical(transfer)

        with self.assertRaisesRegex(PipelineError, "omits required capture reports"):
            _native_receipts_evidence(root, receipts)
        write_json(root / "transfer-manifest.json", transfer)
        with self.assertRaisesRegex(PipelineError, "omits required capture reports"):
            from description_pipeline.native_transfer import verify_transfer

            verify_transfer(
                root,
                expected_upstream_artifacts=transfer["upstream_artifacts"],
                expected_execution=transfer["execution"],
            )


if __name__ == "__main__":
    unittest.main()
