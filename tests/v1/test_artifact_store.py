"""Immutable artifact sealing, verification and StageResult readback."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from description_pipeline.artifacts import ArtifactStore, ArtifactStoreError
from description_pipeline.io import digest
from description_pipeline.pipeline.contracts import DispatchConf, StageResult
from description_pipeline.schemas import AIRFLOW_DISPATCH_CONF_SCHEMA, STAGE_RESULT_SCHEMA


class ArtifactStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = ArtifactStore(self.root / "cas")
        self.tool = digest({"tool": "generate"})

    def source(self, name: str) -> Path:
        root = self.root / name
        (root / "nested").mkdir(parents=True)
        (root / "nested" / "a.txt").write_text("alpha\n", encoding="utf-8")
        (root / "b.bin").write_bytes(b"\x00\x01\x02")
        return root

    def seal(self, source: Path):
        return self.store.seal_tree(
            source,
            kind="generated",
            producer_run_id="run-1",
            producer_stage="generate",
            producer_tool_sha256=self.tool,
        )

    def test_same_tree_identity_is_independent_of_its_work_root(self) -> None:
        first = self.seal(self.source("first"))
        second = self.seal(self.source("second"))
        self.assertEqual(first, second)
        self.store.verify(first)
        destination = self.root / "materialized"
        self.store.materialize(first, destination)
        self.assertEqual((destination / "nested" / "a.txt").read_text(encoding="utf-8"), "alpha\n")
        self.assertEqual((destination / "b.bin").read_bytes(), b"\x00\x01\x02")

    def test_tampered_member_is_rejected(self) -> None:
        artifact = self.seal(self.source("source"))
        path = self.store.objects / artifact.artifact_id / "b.bin"
        path.write_bytes(b"changed")
        with self.assertRaises(ArtifactStoreError):
            self.store.verify(artifact)

    def test_symlink_member_is_rejected(self) -> None:
        source = self.source("source")
        try:
            os.symlink(source / "b.bin", source / "nested" / "link")
        except OSError as error:
            self.skipTest(f"The host cannot create a symbolic link: {error}")
        with self.assertRaises(ArtifactStoreError):
            self.seal(source)

    def test_symlink_source_root_is_rejected(self) -> None:
        source = self.source("source")
        linked = self.root / "linked"
        try:
            os.symlink(source, linked, target_is_directory=True)
        except OSError as error:
            self.skipTest(f"The host cannot create a symbolic link: {error}")
        with self.assertRaisesRegex(ArtifactStoreError, "source root"):
            self.store.seal_tree(
                linked,
                kind="generated",
                producer_run_id="run-1",
                producer_stage="generate",
                producer_tool_sha256=self.tool,
            )

    def test_stage_result_record_roundtrip_is_byte_verified(self) -> None:
        output = self.seal(self.source("output"))
        result = StageResult(
            schema_version=STAGE_RESULT_SCHEMA,
            command_id="command-1",
            run_id="run-1",
            stage="generate",
            request_digest=digest({"request": 1}),
            outcome="completed",
            output_artifact=output,
            actual_executor_sha256=self.tool,
            checks_sha256=digest({"checks": 1}),
            resources_settled=True,
            required_effect_ids=(),
        )
        sealed = self.store.seal_stage_result(result)
        self.assertEqual(self.store.read_result(sealed), result)
        result_path = self.store.objects / sealed.artifact_id / "result.json"
        result_path.write_text("{}", encoding="utf-8")
        with self.assertRaises(ArtifactStoreError):
            self.store.read_result(sealed)

    def test_dispatch_conf_is_a_separate_reference_only_artifact(self) -> None:
        conf = DispatchConf(
            schema_version=AIRFLOW_DISPATCH_CONF_SCHEMA,
            run_id="run-1",
            freeze_command_id="command-freeze",
        )
        artifact = self.store.seal_dispatch_conf(
            conf,
            producer_run_id="run-1",
            producer_tool_sha256=self.tool,
        )
        self.assertEqual(artifact.kind, "airflow_dispatch_conf")
        self.assertIsNone(artifact.producer_stage)
        self.assertEqual(self.store.read_dispatch_conf(artifact), conf)
        records = json.loads((self.store.objects / artifact.artifact_id / "conf.json").read_text(encoding="utf-8"))
        self.assertEqual(set(records), {"schema_version", "run_id", "freeze_command_id"})

    def test_import_object_preserves_manifest_bytes_and_historical_producer(self) -> None:
        artifact = self.seal(self.source("historical-source"))
        destination = ArtifactStore(self.root / "private-diagnostic-cas")
        source_manifest = (self.store.objects / artifact.artifact_id / "manifest.json").read_bytes()
        source_members = {
            path.relative_to(self.store.objects / artifact.artifact_id).as_posix(): path.read_bytes()
            for path in (self.store.objects / artifact.artifact_id).rglob("*")
            if path.is_file() and path.name != "manifest.json"
        }
        imported = destination.import_object(self.store, artifact)
        self.assertEqual(imported, artifact)
        self.assertEqual(imported.producer_run_id, "run-1")
        self.assertEqual(imported.producer_tool_sha256, self.tool)
        self.assertEqual((destination.objects / artifact.artifact_id / "manifest.json").read_bytes(), source_manifest)
        self.assertEqual(
            {
                path.relative_to(destination.objects / artifact.artifact_id).as_posix(): path.read_bytes()
                for path in (destination.objects / artifact.artifact_id).rglob("*")
                if path.is_file() and path.name != "manifest.json"
            },
            source_members,
        )
        self.store.verify(artifact)


if __name__ == "__main__":
    unittest.main()
