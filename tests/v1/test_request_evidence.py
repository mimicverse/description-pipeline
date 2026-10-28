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
    read_request_evidence,
    request_evidence_bytes,
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
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_bytes(request_evidence_bytes(evidence))
        written = hashlib.sha256(self.path.read_bytes()).hexdigest()
        self.assertEqual(self.path.read_bytes(), canonical(evidence))
        self.assertEqual(written, hashlib.sha256(self.path.read_bytes()).hexdigest())
        carried, digest_read = read_request_evidence(self.path)
        self.assertEqual(carried, evidence)
        self.assertEqual(digest_read, written)

    def test_missing_file_is_refused(self) -> None:
        with self.assertRaisesRegex(PipelineError, "carries no input/request.json"):
            read_request_evidence(self.path)

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
