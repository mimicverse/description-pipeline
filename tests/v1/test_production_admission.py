"""Production admission ingestion over real artifact, config and Git inputs."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from description_pipeline.artifacts import ArtifactStore
from description_pipeline.delivery import PIPELINE_ID
from description_pipeline.interface_records import config_snapshot_digest
from description_pipeline.io import canonical, digest
from description_pipeline.pipeline.admission import (
    ADMISSION_SPEC_SCHEMA,
    AdmissionSpec,
    build_run_admission,
)
from description_pipeline.pipeline.contracts import ProtocolError
from description_pipeline.pipeline.freeze import RULESET_CATALOG_SCHEMA
from description_pipeline.pipeline.ingestion import (
    ProductionAdmissionFactory,
    ProductionAdmissionRequest,
    ReceivedUploadReceipt,
    load_production_admission,
)
from description_pipeline.pipeline.publish_contract import REPOSITORY_SNAPSHOT_FILE
from description_pipeline.repository.publish import materialize_repository_snapshot
from description_pipeline.schemas import (
    CONFIG_SNAPSHOT_SCHEMA,
    PLATFORM_CONFIG_SCHEMA,
    TOOL_SCHEMA,
)
from description_pipeline.sources.solidworks.handoff import describe_handoff


def _git(path: Path, *args: str, check: bool = True) -> str:
    completed = subprocess.run(
        ["git", "-C", str(path), *args],
        check=False,
        capture_output=True,
        text=True,
    )
    if check and completed.returncode != 0:
        raise AssertionError(completed.stderr)
    return completed.stdout.strip()


class ProductionAdmissionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="production-admission-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.state = self.root / "state"
        self.handoff = self.root / "handoff"
        self.artifacts_root = self.root / "artifacts"
        self.workspace = self.root / "workspace"
        self.release = self.root / "release"
        self.records = self.root / "records"
        for path in (
            self.state,
            self.handoff,
            self.artifacts_root,
            self.workspace,
            self.release,
            self.records,
        ):
            path.mkdir()
        self.token = self.root / "token"
        self.token.write_text("t" * 64, encoding="utf-8")
        self.secret = self.root / "secret"
        self.secret.write_text("s" * 64, encoding="utf-8")
        self.cert = self.root / "certificate.pem"
        self.cert.write_text("certificate", encoding="utf-8")
        self.key = self.root / "private-key.pem"
        self.key.write_text("key", encoding="utf-8")
        self.dsn = self.root / "database-dsn"
        self.dsn.write_text("postgresql://fixture", encoding="utf-8")
        self.record_file = self.records / "joints/arm.json"
        self.record_file.parent.mkdir()
        self.record_file.write_text(
            '{"limits":{"lower":-1,"upper":1}}\n',
            encoding="utf-8",
        )
        self.checkout = self._repository()
        self.snapshot_path = self.root / "config-snapshot.json"
        self.snapshot_bytes = (
            json.dumps(self._config_snapshot(), indent=2).encode("utf-8") + b"\n"
        )
        self.snapshot_path.write_bytes(self.snapshot_bytes)
        self.upload = self.handoff / "run-produced"
        source = self.upload / "CAD"
        (source / "parts").mkdir(parents=True)
        (source / "robot.SLDASM").write_bytes(b"assembly bytes")
        (source / "parts/arm.SLDPRT").write_bytes(b"part bytes")
        identity = describe_handoff(source)
        self.receipt = ReceivedUploadReceipt(
            folder="CAD",
            files=len(identity["files"]),
            bytes=sum((source / name).stat().st_size for name in identity["files"]),
            handoff_sha256=identity["handoff_sha256"],
            main_assembly="robot.SLDASM",
            target_hardware="m3.0",
            change_summary="Release the verified arm\r\nwith retained evidence",
            purpose="engineering",
        )
        self.request = ProductionAdmissionRequest(
            run_id="run-produced",
            request_id="request-produced",
            upload_dir=self.upload,
            receipt=self.receipt,
            main_assembly=self.receipt.main_assembly,
            target_hardware=self.receipt.target_hardware,
            purpose=self.receipt.purpose,
            change_summary=self.receipt.change_summary,
            principal="cli:tenant:ou_worker",
            from_stage="freeze",
        )
        self.portable_tool = {
            "schema_version": TOOL_SCHEMA,
            "pipeline_id": PIPELINE_ID,
            "source_sha256": digest({"portable": "installed"}),
            "release": None,
            "runtime": {"role": "portable"},
        }
        self.native_tool = {
            "schema_version": TOOL_SCHEMA,
            "pipeline_id": PIPELINE_ID,
            "source_sha256": digest({"native": "observed"}),
            "release": {"source_sha256": digest({"native": "observed"})},
            "runtime": {"role": "native", "system": "Windows"},
        }
        self.native_reads = 0
        self.artifacts = ArtifactStore(self.artifacts_root)

    def _repository(self) -> Path:
        source = self.root / "repository-source"
        source.mkdir()
        _git(source, "init", "-b", "main")
        _git(source, "config", "user.name", "Seed")
        _git(source, "config", "user.email", "seed@example.test")
        (source / "README.md").write_text("fixture\n", encoding="utf-8")
        _git(source, "add", "README.md")
        _git(source, "commit", "-m", "fixture")
        remote = self.root / "repository.git"
        subprocess.run(
            ["git", "init", "--bare", str(remote)],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(remote), "symbolic-ref", "HEAD", "refs/heads/main"],
            check=True,
            capture_output=True,
        )
        _git(source, "remote", "add", "origin", str(remote))
        _git(source, "push", "-u", "origin", "main")
        checkout = self.workspace / "robot"
        subprocess.run(
            ["git", "clone", str(remote), str(checkout)],
            check=True,
            capture_output=True,
        )
        _git(checkout, "config", "user.name", "Release Service")
        _git(checkout, "config", "user.email", "release@example.test")
        return checkout

    def _config_snapshot(self) -> dict:
        resolved = {
            "schema_version": PLATFORM_CONFIG_SCHEMA,
            "operator": {
                "origin": "https://operator.example.test",
                "certificate_file": str(self.cert),
                "private_key_file": str(self.key),
            },
            "storage": {
                "state_root": str(self.state),
                "handoff_root": str(self.handoff),
                "artifact_root": str(self.artifacts_root),
                "workspace_root": str(self.workspace),
                "release_root": str(self.release),
            },
            "database": {"dsn_file": str(self.dsn)},
            "windows": {
                "ssh_host": "windows-fixture",
                "endpoint_port": 8765,
                "token_file": str(self.token),
                "tunnel_loopback_port": 18765,
                "instance": {
                    "task_name": "description-native",
                    "package_root": "C:/payloads/packages",
                    "output_root": "C:/payloads/outputs",
                    "state_root": "C:/payloads/state",
                },
            },
            "auth": {
                "feishu_secret_file": str(self.secret),
                "tenant_keys": ["tenant"],
                "admin_open_ids": [],
            },
            "targets": {
                "m3.0": {
                    "repository_slug": "mimicverse/robot",
                    "base": "main",
                }
            },
            "specifications": {"record_roots": [str(self.records)]},
            "retention": {"unreferenced_evidence_days": None},
            "derived": {
                "store_root": str(self.state / "runs"),
                "repositories": {
                    "mimicverse/robot": str(self.workspace / "robot"),
                },
                "discovery": {"record_roots": [str(self.records)]},
            },
        }
        return {
            "schema_version": CONFIG_SNAPSHOT_SCHEMA,
            "install_id": "install-fixture",
            "generated_at": "2026-10-25T00:00:00Z",
            "resolved": resolved,
            "sources": {"platform_json_sha256": "a" * 64},
        }

    def _factory(self, *, work_root: Path | None = None) -> ProductionAdmissionFactory:
        def native_provider():
            self.native_reads += 1
            return self.native_tool

        return ProductionAdmissionFactory(
            artifacts=self.artifacts,
            platform_snapshot=self.snapshot_path,
            native_tool_provider=native_provider,
            work_root=work_root or (self.root / "work"),
            clock=lambda: datetime(
                2026,
                10,
                25,
                10,
                tzinfo=UTC,
            ),
            portable_tool_provider=lambda: self.portable_tool,
        )

    def test_real_inputs_are_sealed_and_admission_keeps_no_checkout_path(self) -> None:
        work = self.root / "work"
        work.mkdir()
        prepared = self._factory(work_root=work).prepare(self.request)
        spec = prepared.spec
        self.assertEqual(spec.schema_version, ADMISSION_SPEC_SCHEMA)
        self.assertEqual(spec.originals.inventory, describe_handoff(self.upload / "CAD")["files"])
        ruleset_root = self.artifacts.materialize(
            spec.ruleset.artifact.ref,
            work / "sealed-ruleset",
        )
        catalog = json.loads((ruleset_root / "catalog.json").read_text(encoding="utf-8"))
        self.assertEqual(spec.ruleset.version, "records-" + digest(catalog["records"]))
        self.assertEqual(spec.platform_config_sha256, self._config_digest())
        native_tool_root = self.artifacts.materialize(
            spec.native_tool_requirement.ref,
            work / "sealed-native-tool",
        )
        self.assertEqual(
            json.loads((native_tool_root / "tool.json").read_text(encoding="utf-8")),
            self.native_tool,
        )
        self.assertEqual(spec.native_tool_source_sha256, self.native_tool["source_sha256"])
        self.assertEqual(spec.native_tool_record_sha256, digest(self.native_tool))
        self.assertEqual(self.native_reads, 1)
        self.assertEqual(
            spec.publish_intent.repository.origin_url,
            str(self.root / "repository.git"),
        )
        self.assertEqual(spec.publish_intent.repository.base_ref, "main")
        self.assertEqual(
            spec.publish_intent.commit.author_email,
            "release@example.test",
        )
        serialized = spec.to_record()
        self.assertNotIn(str(self.checkout), json.dumps(serialized))
        restored = AdmissionSpec.from_record(serialized)
        self.assertEqual(restored, spec)

    def test_artifact_closure_survives_origin_deletion_and_new_work_root(self) -> None:
        work = self.root / "work"
        work.mkdir()
        prepared = self._factory(work_root=work).prepare(self.request)
        shutil.rmtree(self.upload)
        shutil.rmtree(self.records)
        shutil.rmtree(self.checkout)
        self.snapshot_path.unlink()
        new_work = self.root / "new-work"
        new_work.mkdir()
        spec = prepared.spec
        originals = self.artifacts.materialize(spec.originals.ref, new_work / "originals")
        ruleset = self.artifacts.materialize(spec.ruleset.artifact.ref, new_work / "ruleset")
        configuration = self.artifacts.materialize(
            spec.configuration.ref,
            new_work / "configuration",
        )
        repository = self.artifacts.materialize(
            spec.publish_intent.repository.artifact.ref,
            new_work / "repository",
        )
        self.assertEqual(
            json.loads((ruleset / "catalog.json").read_text(encoding="utf-8"))["schema_version"],
            RULESET_CATALOG_SCHEMA,
        )
        self.assertEqual(
            (configuration / "config-snapshot.json").read_bytes(),
            self.snapshot_bytes,
        )
        self.assertTrue((repository / REPOSITORY_SNAPSHOT_FILE).is_file())
        clone = new_work / "repository-clone"
        snapshot = spec.publish_intent.repository
        materialize_repository_snapshot(
            repository,
            clone,
            expected_repository=snapshot.repository,
            expected_origin_url=snapshot.origin_url,
            expected_base_ref=snapshot.base_ref,
            expected_base_oid=snapshot.expected_base_oid,
            expected_branch_ref=snapshot.branch_ref,
            expected_head_oid=snapshot.expected_head_oid,
        )
        self.assertTrue((clone / "README.md").is_file())
        self.assertTrue(originals.is_dir())

    def test_stage_application_receives_one_atomic_admit_and_no_stage_runs(self) -> None:
        work = self.root / "work"
        work.mkdir()
        calls = []

        class Application:
            def admit(self, spec):
                calls.append(spec)
                return build_run_admission(spec)

        with (
            mock.patch(
                "description_pipeline.pipeline.freeze.FreezeHandler.execute",
                side_effect=AssertionError("Freeze must not run during admission"),
            ),
            mock.patch(
                "description_pipeline.pipeline.discover.DiscoverHandler.execute",
                side_effect=AssertionError("Discover must not run during admission"),
            ),
            mock.patch(
                "description_pipeline.pipeline.capture.CaptureHandler.execute",
                side_effect=AssertionError("Capture must not run during admission"),
            ),
            mock.patch(
                "description_pipeline.pipeline.stages.generate.GenerateHandler.execute",
                side_effect=AssertionError("Generate must not run during admission"),
            ),
            mock.patch(
                "description_pipeline.pipeline.verify.VerifyHandler.execute",
                side_effect=AssertionError("Verify must not run during admission"),
            ),
            mock.patch(
                "description_pipeline.pipeline.stages.publish.PublishHandler.execute",
                side_effect=AssertionError("Publish must not run during admission"),
            ),
        ):
            result = load_production_admission(
                Application(),
                self.request,
                artifacts=self.artifacts,
                platform_snapshot=self.snapshot_path,
                native_tool_provider=lambda: self.native_tool,
                work_root=work,
                clock=lambda: datetime(
                    2026,
                    10,
                    25,
                    10,
                    tzinfo=UTC,
                ),
                portable_tool_provider=lambda: self.portable_tool,
            )
        self.assertEqual(len(calls), 1)
        self.assertEqual(result.run_id, self.request.run_id)

    def test_empty_rules_unknown_target_and_native_identity_mismatch_are_refused(self) -> None:
        work = self.root / "work"
        work.mkdir()
        empty = self.root / "empty-records"
        empty.mkdir()
        snapshot = self._config_snapshot()
        snapshot["resolved"]["specifications"]["record_roots"] = [str(empty)]
        snapshot["resolved"]["derived"]["discovery"]["record_roots"] = [str(empty)]
        self.snapshot_path.write_bytes(canonical(snapshot))
        with self.assertRaisesRegex(ProtocolError, "ruleset_empty"):
            self._factory(work_root=work).prepare(self.request)

        other_artifacts = self.root / "other-artifacts"
        other_artifacts.mkdir()
        bad_config = self._config_snapshot()
        bad_config["resolved"]["storage"]["artifact_root"] = str(other_artifacts)
        self.snapshot_path.write_bytes(canonical(bad_config))
        with self.assertRaisesRegex(ProtocolError, "config_mismatch"):
            self._factory(work_root=work).prepare(self.request)

        self.snapshot_path.write_bytes(canonical(self._config_snapshot()))
        bad_target = replace(
            self.request,
            target_hardware="unknown",
            receipt=ReceivedUploadReceipt(
                **{**self.receipt.to_record(), "target_hardware": "unknown"}
            ),
        )
        with self.assertRaises(ProtocolError):
            self._factory(work_root=work).prepare(bad_target)

        self.native_tool = {
            **self.native_tool,
            "release": {"source_sha256": "f" * 64},
        }
        with self.assertRaisesRegex(ProtocolError, "native_tool_invalid"):
            self._factory(work_root=work).prepare(self.request)

    def test_missing_publication_actor_configuration_is_refused(self) -> None:
        _git(self.checkout, "config", "--unset", "user.email")
        work = self.root / "work"
        work.mkdir()
        with self.assertRaisesRegex(ProtocolError, "publication_actor_missing"):
            self._factory(work_root=work).prepare(self.request)

    def _config_digest(self) -> str:
        return config_snapshot_digest(self._config_snapshot())


if __name__ == "__main__":
    unittest.main()
