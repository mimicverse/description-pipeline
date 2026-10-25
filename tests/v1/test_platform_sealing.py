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
from tests.v1.portal_read_support import build_sealed_delivery


class SealDeliveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="seal-delivery-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.source = self.root / "delivery-source"
        self.source.mkdir()
        self.subject, _artifacts, _handoff_full, _handoff_native = build_sealed_delivery(self.source)

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

    def test_a_source_changed_mid_seal_never_publishes(self) -> None:
        from unittest import mock

        from description_pipeline import delivery as delivery_module

        real = delivery_module.read_delivery_manifest
        reads = {"count": 0}

        def flaky(root):
            reads["count"] += 1
            if reads["count"] == 2:
                # The second read validates the copied temporary tree: a source
                # changed mid-seal must fail there, before any publish.
                raise PipelineError("Delivery subject files differ from the manifest")
            return real(root)

        with mock.patch.object(delivery_module, "read_delivery_manifest", side_effect=flaky), self.assertRaises(
            PipelineError
        ):
            seal_delivery(self.root, "run-1", "generate", self.source)
        run_root = self.root / "sealed" / "run-1"
        self.assertFalse((run_root / "generate").exists())
        self.assertEqual(list(run_root.iterdir()), [])

    def test_a_symlinked_sealed_root_refuses_and_writes_nothing_outside(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        (self.root / "sealed").symlink_to(outside)
        with self.assertRaisesRegex(PipelineError, "symbolic link"):
            seal_delivery(self.root, "run-1", "generate", self.source)
        self.assertEqual(list(outside.iterdir()), [])

    def test_a_newly_created_sealed_directory_syncs_its_parent_entry(self) -> None:
        from unittest import mock

        from description_pipeline.platform import storage as storage_module

        calls: list[Path] = []
        real = storage_module._fsync_dir

        def record(path):
            calls.append(Path(path))
            real(path)

        with mock.patch.object(storage_module, "_fsync_dir", side_effect=record):
            seal_delivery(self.root, "run-1", "generate", self.source)
        self.assertIn(Path(self.root).resolve(), calls)
        self.assertIn(Path(self.root).resolve() / "sealed", calls)

    def test_first_creation_of_a_multi_level_root_syncs_every_new_ancestor(self) -> None:
        from unittest import mock

        from description_pipeline.platform import storage as storage_module

        deep = self.root / "new-parent" / "new-child" / "store"
        calls: list[Path] = []
        real = storage_module._fsync_dir

        def record(path):
            calls.append(Path(path))
            real(path)

        with mock.patch.object(storage_module, "_fsync_dir", side_effect=record):
            uri = seal_delivery(deep, "run-1", "generate", self.source)
        resolved = deep.resolve()
        self.assertIn(resolved, calls)
        self.assertIn(resolved.parent.parent, calls)
        self.assertIn(resolved / "sealed", calls)
        self.assertTrue((resolved / "sealed" / "run-1" / "generate" / "manifest.json").is_file())
        self.assertEqual(uri, "store://sealed/run-1/generate")

    def test_stage_confirmation_registers_report_members_from_the_sealed_bytes(self) -> None:
        from description_pipeline.io import file_digest
        from description_pipeline.platform.storage import seal_stage_confirmation

        manifest = read_delivery_manifest(self.source)
        receipt = {"stage": "verify", "artifacts": [dict(record) for record in manifest["subject"]["files"]]}
        uri, completed = seal_stage_confirmation(self.root, "run-1", "verify", self.source, receipt)
        self.assertEqual(uri, "store://sealed/run-1/verify")
        subject_side = [record for record in completed["artifacts"] if not record["path"].startswith("reports/")]
        self.assertEqual(subject_side, receipt["artifacts"])
        registered = next(record for record in completed["artifacts"] if record["path"] == "reports/quality.json")
        sealed_report = self.root / "sealed" / "run-1" / "verify" / "reports" / "quality.json"
        self.assertEqual(registered["bytes"], sealed_report.stat().st_size)
        self.assertEqual(registered["sha256"], file_digest(sealed_report))
        self.assertIn("reports/native-tool.json", {record["path"] for record in completed["artifacts"]})
