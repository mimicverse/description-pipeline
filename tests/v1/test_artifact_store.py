"""Immutable artifact sealing, verification and StageResult readback."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import description_pipeline.artifacts.store as store_module
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

    def test_concurrent_identical_seals_publish_one_verified_artifact(self) -> None:
        sources = (self.source("first"), self.source("second"))
        barrier = threading.Barrier(2)
        original_replace = os.replace
        results = []
        errors = []

        def racing_replace(source, destination):
            source_path = Path(source)
            destination_path = Path(destination)
            if (
                source_path.parent == self.store.objects
                and destination_path.parent == self.store.objects
            ):
                barrier.wait(timeout=5)
            return original_replace(source, destination)

        def seal(source):
            try:
                results.append(self.seal(source))
            except BaseException as error:  # noqa: BLE001 - retained for the test thread
                errors.append(error)

        with patch.object(store_module.os, "replace", side_effect=racing_replace):
            threads = [threading.Thread(target=seal, args=(source,)) for source in sources]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0], results[1])
        self.store.verify(results[0])

    def test_tampered_member_is_rejected(self) -> None:
        artifact = self.seal(self.source("source"))
        path = self.store.objects / artifact.artifact_id / "b.bin"
        path.write_bytes(b"changed")
        with self.assertRaises(ArtifactStoreError):
            self.store.verify(artifact)

    def test_metadata_member_read_skips_unrelated_payload_bytes(self) -> None:
        source = self.source("metadata-source")
        (source / "request.json").write_bytes(b"{}")
        artifact = self.seal(source)
        with patch.object(
            self.store,
            "_verify_root",
            side_effect=AssertionError("full verification is not permitted"),
        ):
            content, member, manifest = self.store.read_metadata_member(
                artifact,
                "request.json",
                max_bytes=1024,
            )
        self.assertEqual(content, b"{}")
        self.assertEqual(member["path"], "request.json")
        self.assertIn("b.bin", {item["path"] for item in manifest["members"]})

        (self.store.objects / artifact.artifact_id / "b.bin").write_bytes(
            b"changed-large-payload"
        )
        self.assertEqual(
            self.store.read_metadata_member(
                artifact,
                "request.json",
                max_bytes=1024,
            )[0],
            b"{}",
        )
        with self.assertRaises(ArtifactStoreError):
            self.store.verify(artifact)

    def test_metadata_member_read_allows_an_empty_file(self) -> None:
        source = self.source("empty-metadata-source")
        (source / "empty.json").write_bytes(b"")
        artifact = self.seal(source)
        content, member, _manifest = self.store.read_metadata_member(
            artifact,
            "empty.json",
            max_bytes=0,
        )
        self.assertEqual(content, b"")
        self.assertEqual(member["bytes"], 0)

    def test_metadata_member_read_rejects_unsafe_or_tampered_member(self) -> None:
        source = self.source("unsafe-metadata-source")
        (source / "request.json").write_bytes(b"{}")
        artifact = self.seal(source)
        for member in ("", ".", "../request.json", "/request.json", "manifest.json"):
            with self.subTest(member=member), self.assertRaises(ArtifactStoreError):
                self.store.read_metadata_member(
                    artifact,
                    member,
                    max_bytes=1024,
                )
        request = self.store.objects / artifact.artifact_id / "request.json"
        request.write_bytes(b'{"tampered":true}')
        with self.assertRaises(ArtifactStoreError):
            self.store.read_metadata_member(
                artifact,
                "request.json",
                max_bytes=1024,
            )

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
        with self.assertRaisesRegex(ArtifactStoreError, "traverse a symbolic link"):
            self.store.seal_tree(
                linked,
                kind="generated",
                producer_run_id="run-1",
                producer_stage="generate",
                producer_tool_sha256=self.tool,
            )

    def test_linked_parent_is_rejected_before_resolve(self) -> None:
        real = self.root / "real"
        real.mkdir()
        linked = self.root / "linked-parent"
        try:
            os.symlink(real, linked, target_is_directory=True)
        except OSError as error:
            self.skipTest(f"The host cannot create a symbolic link: {error}")
        with self.assertRaisesRegex(ArtifactStoreError, "traverse"):
            ArtifactStore(linked / "cas")
        with self.assertRaisesRegex(ArtifactStoreError, "traverse"):
            ArtifactStore(linked / ".." / "cas")

        source = real / "source"
        (source / "nested").mkdir(parents=True)
        (source / "nested" / "a.txt").write_text("alpha\n", encoding="utf-8")
        with self.assertRaisesRegex(ArtifactStoreError, "traverse"):
            self.seal(linked / "source")

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
