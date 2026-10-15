"""Atomic sealing: validated trees, verbatim bytes, subject idempotency and refusals."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from description_pipeline.delivery import read_delivery_manifest, write_delivery_manifest
from description_pipeline.interface_records import CHECK_POLICY_SCHEMA, check_policy_digest
from description_pipeline.io import PipelineError
from description_pipeline.platform.storage import seal_delivery, sealed_resolver
from description_pipeline.stages import CONTRACT, CONTRACT_SHA256
from tests.v1.test_platform_store import build_sealed_delivery


class SealDeliveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="seal-delivery-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.source = self.root / "delivery-source"
        self.source.mkdir()
        self.subject, _artifacts, _handoff = build_sealed_delivery(self.source)

    def _rebuild_source(self, name: str) -> Path:
        directory = self.root / name
        directory.mkdir()
        build_sealed_delivery(directory)
        (directory / "urdf" / "robot.urdf").write_text("<robot name='changed'/>\n", encoding="utf-8")
        write_delivery_manifest(
            directory,
            contract={"schema_version": CONTRACT["schema_version"], "digest": CONTRACT_SHA256},
            policy={"schema_version": CHECK_POLICY_SCHEMA, "digest": check_policy_digest()},
        )
        return directory

    def test_seals_atomically_and_resolves_to_the_same_subject(self) -> None:
        uri = seal_delivery(self.root, "run-1", "generate", self.source)
        self.assertEqual(uri, "store://sealed/run-1/generate")
        target = sealed_resolver(self.root)(uri)
        self.assertEqual(read_delivery_manifest(target)["subject"]["digest"], self.subject)
        self.assertEqual(
            (target / "manifest.json").read_bytes(), (self.source / "manifest.json").read_bytes()
        )
        self.assertEqual([entry.name for entry in (self.root / "sealed" / "run-1").iterdir()], ["generate"])

    def test_resealing_the_same_subject_is_idempotent(self) -> None:
        first = seal_delivery(self.root, "run-1", "generate", self.source)
        target = sealed_resolver(self.root)(first)
        before = (target / "manifest.json").read_bytes()
        again = seal_delivery(self.root, "run-1", "generate", self.source)
        self.assertEqual(first, again)
        self.assertEqual((target / "manifest.json").read_bytes(), before)

    def test_a_different_subject_for_the_same_stage_is_refused(self) -> None:
        seal_delivery(self.root, "run-1", "generate", self.source)
        other = self._rebuild_source("other-source")
        self.assertNotEqual(
            read_delivery_manifest(other)["subject"]["digest"], self.subject
        )
        with self.assertRaisesRegex(PipelineError, "different subject"):
            seal_delivery(self.root, "run-1", "generate", other)

    def test_invalid_locators_and_sources_are_refused(self) -> None:
        with self.assertRaises(PipelineError):
            seal_delivery(self.root, "run-1", "not-a-stage", self.source)
        with self.assertRaises(PipelineError):
            seal_delivery(self.root, "../escape", "generate", self.source)
        empty = self.root / "empty-source"
        empty.mkdir()
        with self.assertRaises(PipelineError):
            seal_delivery(self.root, "run-1", "generate", empty)

    def test_symbolic_links_in_the_source_are_refused(self) -> None:
        (self.source / "evil").symlink_to(self.source / "urdf" / "robot.urdf")
        with self.assertRaisesRegex(PipelineError, "symbolic links"):
            seal_delivery(self.root, "run-1", "generate", self.source)
