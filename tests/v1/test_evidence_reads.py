"""Canonical delivery evidence accessors fail closed on absent records."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from description_pipeline.io import PipelineError, digest, write_json
from description_pipeline.schemas import (
    NATIVE_DISCOVERY_SCHEMA,
    SOURCE_RECORD_SCHEMA,
)
from description_pipeline.verification import capture_evidence as evidence

HEX = "a" * 64


def _source_record() -> dict:
    identity = {
        "kind": "source-revision",
        "handoff_digest": HEX,
        "main_assembly": "robot.SLDASM",
        "configuration": "Default",
        "hardware_id": "fixture",
        "records": [],
    }
    return {
        "schema_version": SOURCE_RECORD_SCHEMA,
        "source_revision_id": digest(identity),
        "identity": identity,
        "selection": {
            "main_assembly": "robot.SLDASM",
            "configuration": "Default",
            "hardware_id": "fixture",
            "hardware_source": "cad",
        },
        "metadata": {
            "revision": "r1",
            "owner": None,
            "control": None,
            "change_summary": "fixture",
            "change_summary_source": "cad",
        },
    }


class CanonicalEvidenceReadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="evidence-reads-"
        )
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        write_json(self.root / "input/source.json", _source_record())
        write_json(self.root / "input/handoff.json", {"passed": True})
        write_json(
            self.root / "input/discovery/native-discovery.json",
            {"schema_version": NATIVE_DISCOVERY_SCHEMA},
        )
        write_json(
            self.root / "evidence/native-runtime.json",
            {"solidworks": {"revision": "34.0"}},
        )

    def test_accessors_read_only_canonical_records(self) -> None:
        self.assertEqual(
            evidence.source(self.root)["selection"]["hardware_id"],
            "fixture",
        )
        self.assertEqual(evidence.handoff(self.root), {"passed": True})
        self.assertEqual(
            evidence.runtime(self.root)["solidworks"]["revision"],
            "34.0",
        )
        self.assertEqual(
            evidence.discovery(self.root)["schema_version"],
            NATIVE_DISCOVERY_SCHEMA,
        )

    def test_missing_canonical_record_fails_closed(self) -> None:
        (self.root / "input/discovery/native-discovery.json").unlink()
        with self.assertRaises(PipelineError):
            evidence.discovery(self.root)


if __name__ == "__main__":
    unittest.main()
