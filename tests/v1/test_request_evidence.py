"""The frozen request evidence: schema, canonical bytes, binding and refusals."""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from description_pipeline.io import PipelineError, canonical
from description_pipeline.request_evidence import (
    REQUEST_FIELDS,
    REQUEST_SCHEMA,
    build_request_evidence,
    carry_request_evidence,
    freeze_request_evidence,
    read_request_evidence,
    request_evidence_bytes,
    verify_request_evidence,
    write_request_evidence,
)

DIGEST = "a" * 64


def record(**overrides) -> dict:
    payload = {
        "purpose": "engineering",
        "request_id": "portal-20261016T000000-abcdef01",
        "target_hardware": "m3.0",
        "change_summary": "one line",
        "main_assembly": "3.0 总装.SLDASM",
        "handoff_digest": DIGEST,
        "platform_config_digest": "b" * 64,
    }
    payload.update(overrides)
    return build_request_evidence(**payload)


class RequestEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="request-evidence-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.path = self.root / "input" / "request.json"

    def test_builds_exactly_the_eight_fields_in_canonical_bytes(self) -> None:
        evidence = record()
        self.assertEqual(set(evidence), set(REQUEST_FIELDS))
        self.assertEqual(evidence["schema_version"], REQUEST_SCHEMA)
        self.assertEqual(request_evidence_bytes(evidence), canonical(evidence))

    def test_purpose_is_required_explicit_and_only_the_two_values(self) -> None:
        base = record()
        self.assertEqual(base["purpose"], "engineering")
        self.assertEqual(record(purpose="inspection_preview")["purpose"], "inspection_preview")
        without = {key: value for key, value in base.items() if key != "purpose"}
        with self.assertRaisesRegex(PipelineError, "request_record_invalid"):
            request_evidence_bytes(without)
        with self.assertRaisesRegex(PipelineError, "request_record_invalid"):
            request_evidence_bytes({**base, "purpose": "preview"})
        with self.assertRaisesRegex(PipelineError, "request_record_invalid"):
            request_evidence_bytes({**base, "purpose": None})

    def test_change_summary_is_lf_normalized_and_other_controls_are_refused(self) -> None:
        self.assertEqual(record(change_summary="line1\r\nline2\rline3")["change_summary"], "line1\nline2\nline3")
        with self.assertRaisesRegex(PipelineError, "request_record_invalid"):
            record(change_summary="bad\x07summary")

    def test_missing_or_misshaped_fields_are_refused(self) -> None:
        with self.assertRaisesRegex(PipelineError, "request_record_invalid"):
            record(request_id="")
        with self.assertRaisesRegex(PipelineError, "request_record_invalid"):
            record(change_summary="   ")
        with self.assertRaisesRegex(PipelineError, "request_record_invalid"):
            record(handoff_digest="not-a-digest")
        with self.assertRaisesRegex(PipelineError, "request_record_invalid"):
            record(platform_config_digest="F" * 64)
        with self.assertRaisesRegex(PipelineError, "request_record_invalid"):
            request_evidence_bytes({**record(), "schema_version": "solidworks-to-urdf.request/v1"})
        extra = {**record(), "secret": "nope"}
        with self.assertRaisesRegex(PipelineError, "request_record_invalid"):
            request_evidence_bytes(extra)

    def test_write_read_round_trip_is_byte_stable_and_binds_its_digest(self) -> None:
        evidence = record(change_summary="multi\nline")
        written = write_request_evidence(self.path, evidence)
        self.assertEqual(self.path.read_bytes(), canonical(evidence))
        self.assertEqual(written, hashlib.sha256(self.path.read_bytes()).hexdigest())
        carried, digest_read = read_request_evidence(self.path)
        self.assertEqual(carried, evidence)
        self.assertEqual(digest_read, written)
        self.assertEqual(verify_request_evidence(self.path, expected_sha256=written), evidence)

    def test_missing_file_and_mismatched_binding_are_refusals(self) -> None:
        with self.assertRaisesRegex(PipelineError, "carries no input/request.json"):
            read_request_evidence(self.path)
        written = write_request_evidence(self.path, record())
        with self.assertRaisesRegex(PipelineError, "do not match the receipt binding"):
            verify_request_evidence(self.path, expected_sha256="0" * 64)
        self.path.write_bytes(self.path.read_bytes() + b"\n")
        with self.assertRaisesRegex(PipelineError, "do not match the receipt binding"):
            verify_request_evidence(self.path, expected_sha256=written)

    def test_null_and_misshaped_required_fields_are_refusals(self) -> None:
        base = record()
        for field in ("request_id", "main_assembly", "handoff_digest", "platform_config_digest"):
            with self.subTest(field=field), self.assertRaisesRegex(PipelineError, "request_record_invalid"):
                request_evidence_bytes({**base, field: None})
        with self.assertRaisesRegex(PipelineError, "request_record_invalid"):
            request_evidence_bytes({**base, "request_id": 123})
        with self.assertRaisesRegex(PipelineError, "request_record_invalid"):
            request_evidence_bytes({**base, "platform_config_digest": "not-a-sha"})
        with self.assertRaisesRegex(PipelineError, "request_record_invalid"):
            request_evidence_bytes({**base, "handoff_digest": "c" * 63})

    def test_duplicate_keys_in_the_carried_file_are_refused(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_bytes(
            b'{"schema_version":"solidworks-to-urdf.request/v1","request_id":"a","request_id":"b",'
            b'"target_hardware":"m3.0","change_summary":"x","main_assembly":"a.SLDASM",'
            b'"handoff_digest":"' + b"a" * 64 + b'","platform_config_digest":"' + b"b" * 64 + b'"}'
        )
        with self.assertRaisesRegex(PipelineError, "canonical strict JSON"):
            read_request_evidence(self.path)

    def test_freeze_writes_before_any_tree_and_carry_is_byte_verbatim(self) -> None:
        run_dir = self.root / "run-1"
        written = freeze_request_evidence(
            run_dir,
            purpose="engineering",
            request_id="portal-20261016T000000-abcdef01",
            target_hardware="m3.0",
            change_summary="freeze first",
            main_assembly="3.0 总装.SLDASM",
            handoff_digest=DIGEST,
            platform_config_digest="b" * 64,
        )
        self.assertEqual(written, run_dir / "input" / "request.json")
        frozen_bytes = written.read_bytes()
        tree = self.root / "run-1" / "capture"
        carried = carry_request_evidence(run_dir, tree)
        self.assertEqual(carried.read_bytes(), frozen_bytes)
        self.assertEqual(read_request_evidence(carried)[0], read_request_evidence(written)[0])
        self.assertEqual(read_request_evidence(carried)[0]["request_id"], "portal-20261016T000000-abcdef01")

    def test_carry_refuses_when_the_frozen_evidence_is_absent(self) -> None:
        with self.assertRaisesRegex(PipelineError, "carries no input/request.json"):
            carry_request_evidence(self.root / "run-empty", self.root / "run-empty" / "capture")
