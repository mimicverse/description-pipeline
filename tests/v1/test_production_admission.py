"""Production admission ingestion over real artifact, config and Git inputs."""

from __future__ import annotations

import copy
import json
import shutil
import subprocess
import tempfile
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

try:
    import pgserver
    import psycopg2
except ImportError:
    pgserver = None
    psycopg2 = None

from description_pipeline.artifacts import ArtifactStore
from description_pipeline import __version__
from description_pipeline.delivery import PIPELINE_ID
from description_pipeline.interface_records import config_snapshot_digest
from description_pipeline.io import PipelineError, canonical, digest
from description_pipeline.pipeline.admission import (
    ADMISSION_SPEC_SCHEMA,
    AdmissionSpec,
    build_freeze_command,
    build_run_admission,
)
from description_pipeline.pipeline.application import (
    AdmissionOutcome,
    StageApplication,
)
from description_pipeline.pipeline.contracts import (
    DispatchCommand,
    DispatchConf,
    ProtocolError,
    StageCommand,
)
from description_pipeline.pipeline import discover as discover_module
from description_pipeline.pipeline.freeze import (
    RULESET_CATALOG_SCHEMA,
    RulesetCatalog,
    RulesetRoot,
    ruleset_record_roots,
)
from description_pipeline.pipeline.ingestion import (
    ProductionAdmissionFactory,
    ProductionAdmissionRequest,
    ReceivedUploadReceipt,
    load_production_admission,
)
from description_pipeline.pipeline.publish_contract import REPOSITORY_SNAPSHOT_FILE
from description_pipeline.pipeline.stages.generate import GenerateHandler
from description_pipeline.pipeline.stages.publish import PublishHandler
from description_pipeline.repository.publish import materialize_repository_snapshot
from description_pipeline.schemas import (
    AIRFLOW_DISPATCH_CONF_SCHEMA,
    CONFIG_SNAPSHOT_SCHEMA,
    PLATFORM_CONFIG_SCHEMA,
    TOOL_SCHEMA,
)
from description_pipeline.platform.facts import FactStore
from description_pipeline.sources.solidworks.handoff import describe_handoff
from description_pipeline.sources.solidworks.discovery import (
    DiscoverySettings,
    _resolve_record,
)


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


PLATFORM_SCHEMA = Path(__file__).resolve().parents[2] / "deploy" / "platform" / "schema.sql"
PG_AVAILABLE = pgserver is not None and psycopg2 is not None


def _admission_outcome(artifacts: ArtifactStore, spec: AdmissionSpec) -> AdmissionOutcome:
    admission = build_run_admission(spec)
    freeze = build_freeze_command(spec)
    conf = DispatchConf(
        schema_version=AIRFLOW_DISPATCH_CONF_SCHEMA,
        run_id=spec.run_id,
        freeze_command_id=freeze.command_id,
    )
    conf_artifact = artifacts.seal_dispatch_conf(
        conf,
        producer_run_id=spec.run_id,
        producer_tool_sha256=spec.dispatch_executor_sha256,
    )
    dispatch = DispatchCommand.create(
        command_id="dispatch:" + digest({"run_id": spec.run_id}),
        run_id=spec.run_id,
        freeze_command_id=freeze.command_id,
        idempotency_key=f"{spec.run_id}/dispatch",
        dag_id="solidworks_to_urdf",
        dag_run_id=spec.request_id,
        conf_artifact=conf_artifact,
        expected_executor_sha256=spec.dispatch_executor_sha256,
    )
    return AdmissionOutcome(
        admission=admission,
        freeze_command=freeze,
        dispatch_command=dispatch,
    )


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
        package_files = {
            "description_pipeline/runtime.py": digest({"release-file": "runtime"}),
        }
        self.release_identity = {
            "schema_version": "solidworks-to-urdf.release/v1",
            "pipeline_id": PIPELINE_ID,
            "version": __version__,
            "source_commit": "a" * 40,
            "source_tree": "b" * 40,
            "source_sha256": digest(package_files),
            "package_files": package_files,
        }
        self.portable_tool = {
            "schema_version": TOOL_SCHEMA,
            "pipeline_id": PIPELINE_ID,
            "version": __version__,
            "source_sha256": digest({"portable": "installed"}),
            "release": copy.deepcopy(self.release_identity),
            "runtime": {
                "role": "portable",
                "system": "Linux",
                "python": "3.12.10",
            },
        }
        self.native_tool = {
            "schema_version": TOOL_SCHEMA,
            "pipeline_id": PIPELINE_ID,
            "version": __version__,
            "source_sha256": digest({"native": "observed"}),
            "release": copy.deepcopy(self.release_identity),
            "runtime": {
                "role": "native",
                "system": "Windows",
                "python": "3.12.10",
            },
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

    @unittest.skipUnless(
        PG_AVAILABLE,
        "pgserver/psycopg2 are not installed",
    )
    def test_real_pg_replays_written_outcome_and_resolves_concurrent_first_admission(
        self,
    ) -> None:
        server_root = tempfile.TemporaryDirectory(
            prefix="production-admission-postgres-"
        )
        self.addCleanup(server_root.cleanup)
        server = pgserver.get_server(
            Path(server_root.name) / "pgdata",
            cleanup_mode="delete",
        )
        self.addCleanup(server.cleanup)
        schema = "production_admission_" + uuid.uuid4().hex
        schema_sql = PLATFORM_SCHEMA.read_text(encoding="utf-8").replace(
            ':"platform_schema"',
            f'"{schema}"',
        )
        with (
            psycopg2.connect(server.get_uri()) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(f'CREATE SCHEMA "{schema}"')
            cursor.execute(schema_sql)
            cursor.execute(
                f'UPDATE "{schema}".admission SET state = %s WHERE id = 1',
                ("open",),
            )

        def connect():
            return psycopg2.connect(
                server.get_uri(),
                options=f"-c search_path={schema},public",
            )

        def query(statement, parameters=()):
            with connect() as connection, connection.cursor() as cursor:
                cursor.execute(statement, parameters)
                return cursor.fetchall()

        facts = FactStore(connect, self.artifacts)
        application = StageApplication(
            services=SimpleNamespace(
                facts=facts,
                artifacts=self.artifacts,
            ),
            facts=facts,
            artifacts=self.artifacts,
            handlers={},
            builders={},
        )
        provider_calls = {"native": 0, "portable": 0}
        provider_lock = threading.Lock()

        def native_provider():
            with provider_lock:
                provider_calls["native"] += 1
            return self.native_tool

        def portable_provider():
            with provider_lock:
                provider_calls["portable"] += 1
            return self.portable_tool

        def admit_request(
            request,
            *,
            work_root,
            clock,
        ):
            return load_production_admission(
                application,
                request,
                artifacts=self.artifacts,
                platform_snapshot=self.snapshot_path,
                native_tool_provider=native_provider,
                work_root=work_root,
                clock=clock,
                portable_tool_provider=portable_provider,
            )

        concurrent_request = replace(
            self.request,
            run_id="run-concurrent-lost-response",
            request_id="request-concurrent-lost-response",
        )
        concurrent_work_a = self.root / "concurrent-work-a"
        concurrent_work_b = self.root / "concurrent-work-b"
        concurrent_work_a.mkdir()
        concurrent_work_b.mkdir()
        prepared_a = ProductionAdmissionFactory(
            artifacts=self.artifacts,
            platform_snapshot=self.snapshot_path,
            native_tool_provider=native_provider,
            work_root=concurrent_work_a,
            clock=lambda: datetime(2026, 10, 25, 10, tzinfo=UTC),
            portable_tool_provider=portable_provider,
        ).prepare(concurrent_request)
        prepared_b = ProductionAdmissionFactory(
            artifacts=self.artifacts,
            platform_snapshot=self.snapshot_path,
            native_tool_provider=native_provider,
            work_root=concurrent_work_b,
            clock=lambda: datetime(2026, 10, 25, 11, tzinfo=UTC),
            portable_tool_provider=portable_provider,
        ).prepare(concurrent_request)
        self.assertNotEqual(prepared_a.spec, prepared_b.spec)
        barrier = threading.Barrier(2)

        def concurrent_call(prepared):
            barrier.wait(timeout=5)
            return prepared.register(application)

        with ThreadPoolExecutor(max_workers=2) as executor:
            first_future = executor.submit(concurrent_call, prepared_a)
            second_future = executor.submit(concurrent_call, prepared_b)
            concurrent_a = first_future.result(timeout=30)
            concurrent_b = second_future.result(timeout=30)
        self.assertEqual(concurrent_a, concurrent_b)
        self.assertEqual(
            query(
                "SELECT count(*) FROM run_admission WHERE run_id = %s",
                (concurrent_request.run_id,),
            )[0][0],
            1,
        )
        self.assertEqual(
            query(
                "SELECT count(*) FROM command WHERE run_id = %s",
                (concurrent_request.run_id,),
            )[0][0],
            2,
        )
        self.assertEqual(
            query(
                """
                SELECT count(*)
                FROM effect_intent intent
                JOIN command command ON command.command_id = intent.command_id
                WHERE command.run_id = %s
                """,
                (concurrent_request.run_id,),
            )[0][0],
            1,
        )

        first_work = self.root / "lost-response-work"
        first_work.mkdir()
        first = admit_request(
            self.request,
            work_root=first_work,
            clock=lambda: datetime(2026, 10, 25, 12, tzinfo=UTC),
        )
        shutil.rmtree(self.upload)
        shutil.rmtree(self.records)
        shutil.rmtree(self.checkout)
        self.snapshot_path.unlink()
        shutil.rmtree(first_work)
        second_work = self.root / "lost-response-retry-work"
        second_work.mkdir()
        before_retry = dict(provider_calls)
        artifact_rows = query("SELECT count(*) FROM artifact")[0][0]

        second = admit_request(
            self.request,
            work_root=second_work,
            clock=lambda: datetime(2026, 10, 25, 13, tzinfo=UTC),
        )
        self.assertEqual(second, first)
        self.assertEqual(provider_calls, before_retry)
        self.assertEqual(
            query("SELECT count(*) FROM artifact")[0][0],
            artifact_rows,
        )
        self.assertEqual(
            query(
                "SELECT count(*) FROM command WHERE run_id = %s",
                (self.request.run_id,),
            )[0][0],
            2,
        )
        self.assertEqual(
            query(
                """
                SELECT count(*)
                FROM effect_intent intent
                JOIN command command ON command.command_id = intent.command_id
                WHERE command.run_id = %s
                """,
                (self.request.run_id,),
            )[0][0],
            1,
        )

        changed_requests = [
            replace(
                self.request,
                request_id="another-request-id",
            ),
            replace(
                self.request,
                principal="cli:tenant:ou_other",
            ),
            replace(
                self.request,
                parent_run_id="another-parent",
            ),
            replace(
                self.request,
                from_stage="discover",
            ),
            replace(
                self.request,
                main_assembly="other.SLDASM",
                receipt=replace(
                    self.receipt,
                    main_assembly="other.SLDASM",
                ),
            ),
            replace(
                self.request,
                target_hardware="m3.1",
                receipt=replace(
                    self.receipt,
                    target_hardware="m3.1",
                ),
            ),
            replace(
                self.request,
                purpose="inspection_preview",
                receipt=replace(
                    self.receipt,
                    purpose="inspection_preview",
                ),
            ),
            replace(
                self.request,
                change_summary="Changed administrative intent",
                receipt=replace(
                    self.receipt,
                    change_summary="Changed administrative intent",
                ),
            ),
            replace(
                self.request,
                receipt=replace(
                    self.receipt,
                    files=self.receipt.files + 1,
                ),
            ),
            replace(
                self.request,
                receipt=replace(
                    self.receipt,
                    bytes=self.receipt.bytes + 1,
                ),
            ),
            replace(
                self.request,
                receipt=replace(
                    self.receipt,
                    handoff_sha256="f" * 64,
                ),
            ),
        ]
        for changed in changed_requests:
            with self.subTest(request=changed):
                with self.assertRaises(ProtocolError) as raised:
                    admit_request(
                        changed,
                        work_root=second_work,
                        clock=lambda: datetime(
                            2026,
                            10,
                            25,
                            14,
                            tzinfo=UTC,
                        ),
                    )
                self.assertEqual(
                    raised.exception.code,
                    "admission_ingestion.intent_conflict",
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
        parsed_catalog = RulesetCatalog.from_record(catalog)
        self.assertEqual(
            spec.ruleset.version,
            "records-" + digest([root.to_record() for root in parsed_catalog.roots]),
        )
        self.assertEqual(
            ruleset_record_roots(ruleset_root, parsed_catalog),
            (ruleset_root / "root-000",),
        )
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
        self.assertEqual(
            spec.dispatch_executor_sha256,
            spec.executors.freeze,
        )
        self.assertEqual(self.native_reads, 1)
        for artifact in (
            spec.originals.ref,
            spec.ruleset.artifact.ref,
            spec.configuration.ref,
            spec.native_tool_requirement.ref,
            spec.publish_intent.repository.artifact.ref,
        ):
            self.assertEqual(
                artifact.producer_tool_sha256,
                self.portable_tool["source_sha256"],
            )
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

    def test_sealed_ruleset_roots_drive_discover_and_preserve_ambiguity(self) -> None:
        work = self.root / "work"
        work.mkdir()
        prepared = self._factory(work_root=work).prepare(self.request)
        ruleset_root = self.artifacts.materialize(
            prepared.spec.ruleset.artifact.ref,
            self.root / "sealed-rules-for-discover",
        )
        catalog = discover_module._ruleset_version(
            ruleset_root,
            prepared.spec.ruleset.version,
        )
        record_roots = ruleset_record_roots(ruleset_root, catalog)
        findings = []
        resolved = _resolve_record(
            DiscoverySettings(record_roots=record_roots),
            "joints/arm.json#limits",
            findings,
            "robot",
        )
        self.assertEqual(resolved["value"], {"lower": -1, "upper": 1})
        self.assertEqual(findings, [])

        ambiguous_root = self.root / "ambiguous-records"
        for root_name in ("first", "second"):
            target = ambiguous_root / root_name / "joints"
            target.mkdir(parents=True)
            (target / "arm.json").write_text(json.dumps({"limits": {"root": root_name}}), encoding="utf-8")
        ambiguous_config = self._config_snapshot()
        ambiguous_config["resolved"]["specifications"]["record_roots"] = [
            str(ambiguous_root / "first"),
            str(ambiguous_root / "second"),
        ]
        ambiguous_config["resolved"]["derived"]["discovery"]["record_roots"] = list(
            ambiguous_config["resolved"]["specifications"]["record_roots"]
        )
        ambiguous_snapshot = self.root / "ambiguous-config.json"
        ambiguous_snapshot.write_bytes(canonical(ambiguous_config))
        ambiguous_request = replace(
            self.request,
            run_id="run-ambiguous",
            request_id="request-ambiguous",
        )
        ambiguous_factory = ProductionAdmissionFactory(
            artifacts=self.artifacts,
            platform_snapshot=ambiguous_snapshot,
            native_tool_provider=lambda: self.native_tool,
            work_root=work,
            clock=lambda: datetime(2026, 10, 25, 10, tzinfo=UTC),
            portable_tool_provider=lambda: self.portable_tool,
        )
        ambiguous = ambiguous_factory.prepare(ambiguous_request)
        materialized = self.artifacts.materialize(
            ambiguous.spec.ruleset.artifact.ref,
            work / "ambiguous-sealed",
        )
        ambiguous_catalog = discover_module._ruleset_version(
            materialized,
            ambiguous.spec.ruleset.version,
        )
        self.assertEqual(
            [root.path for root in ambiguous_catalog.roots],
            ["root-000", "root-001"],
        )
        self.assertNotIn(
            str(ambiguous_root),
            json.dumps(ambiguous_catalog.to_record()),
        )
        ambiguous_findings = []
        self.assertIsNone(
            _resolve_record(
                DiscoverySettings(
                    record_roots=ruleset_record_roots(
                        materialized,
                        ambiguous_catalog,
                    )
                ),
                "joints/arm.json#limits",
                ambiguous_findings,
                "robot",
            )
        )
        self.assertEqual(
            [finding["code"] for finding in ambiguous_findings],
            ["discovery.record_ambiguous"],
        )

        shutil.rmtree(self.records)
        shutil.rmtree(self.checkout)
        self.snapshot_path.unlink()
        new_work = self.root / "discover-new-work"
        new_work.mkdir()
        restaged = self.artifacts.materialize(
            prepared.spec.ruleset.artifact.ref,
            new_work / "ruleset",
        )
        rewritten = discover_module._ruleset_version(
            restaged,
            prepared.spec.ruleset.version,
        )
        rewritten_roots = ruleset_record_roots(restaged, rewritten)
        self.assertEqual(
            _resolve_record(
                DiscoverySettings(record_roots=rewritten_roots),
                "joints/arm.json#limits",
                [],
                "robot",
            )["value"],
            {"lower": -1, "upper": 1},
        )

    def test_ruleset_roots_reject_overlap_unlisted_bytes_and_links(self) -> None:
        leaf = RulesetRoot(
            path="root-000",
            members={"record.json": "a" * 64},
        )
        nested = RulesetRoot(
            path="root-000/nested",
            members={"other.json": "b" * 64},
        )
        with self.assertRaises(ValueError):
            RulesetCatalog(
                version="records-" + digest(
                    [leaf.to_record(), nested.to_record()]
                ),
                roots=(leaf, nested),
                records={
                    "root-000/record.json": "a" * 64,
                    "root-000/nested/other.json": "b" * 64,
                },
            )

        work = self.root / "unlisted-work"
        work.mkdir()
        prepared = self._factory(work_root=work).prepare(self.request)
        materialized = self.artifacts.materialize(
            prepared.spec.ruleset.artifact.ref,
            work / "ruleset",
        )
        catalog = RulesetCatalog.from_record(
            json.loads((materialized / "catalog.json").read_text(encoding="utf-8"))
        )
        (materialized / "root-000" / "unlisted.json").write_text(
            "{}\n",
            encoding="utf-8",
        )
        with self.assertRaises(PipelineError):
            ruleset_record_roots(materialized, catalog)

        external = self.root / "external-record.json"
        external.write_text("{}\n", encoding="utf-8")
        (self.records / "linked.json").symlink_to(external)
        linked_work = self.root / "linked-work"
        linked_work.mkdir()
        with self.assertRaises(ProtocolError) as raised:
            self._factory(work_root=linked_work).prepare(self.request)
        self.assertEqual(
            raised.exception.code,
            "admission_ingestion.ruleset_invalid",
        )

    def test_stage_application_receives_one_atomic_admit_and_no_stage_runs(self) -> None:
        work = self.root / "work"
        work.mkdir()
        calls = []
        store = self.artifacts

        class Application:
            def load_admission_outcome(self, run_id):
                return None

            def admit(self, spec):
                calls.append(spec)
                return _admission_outcome(store, spec)

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
            mock.patch.object(
                GenerateHandler,
                "execute",
                side_effect=AssertionError("Generate must not run during admission"),
            ),
            mock.patch(
                "description_pipeline.pipeline.verify.VerifyHandler.execute",
                side_effect=AssertionError("Verify must not run during admission"),
            ),
            mock.patch.object(
                PublishHandler,
                "execute",
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
        self.assertEqual(result.admission.run_id, self.request.run_id)
        self.assertEqual(result.freeze_command.stage, "freeze")

    def test_mismatched_application_outcome_is_rejected_after_one_atomic_call(self) -> None:
        work = self.root / "work"
        work.mkdir()
        calls = 0
        artifacts = self.artifacts

        class Application:
            def load_admission_outcome(self, run_id):
                return None

            def admit(self, spec):
                nonlocal calls
                calls += 1
                outcome = _admission_outcome(artifacts, spec)
                original = outcome.freeze_command
                return replace(
                    outcome,
                    freeze_command=StageCommand.create(
                        command_id="freeze:different",
                        run_id=original.run_id,
                        stage="freeze",
                        idempotency_key=original.idempotency_key,
                        request={**dict(original.request), "principal": "other"},
                        input_artifacts=original.input_artifacts,
                        config_snapshot_sha256=original.config_snapshot_sha256,
                        contract_sha256=original.contract_sha256,
                        policy_sha256=original.policy_sha256,
                        expected_executor_sha256=original.expected_executor_sha256,
                        predecessor_decision_id=original.predecessor_decision_id,
                    ),
                )

        with self.assertRaisesRegex(ProtocolError, "outcome_mismatch"):
            load_production_admission(
                Application(),
                self.request,
                artifacts=self.artifacts,
                platform_snapshot=self.snapshot_path,
                native_tool_provider=lambda: self.native_tool,
                work_root=work,
                clock=lambda: datetime(2026, 10, 25, 10, tzinfo=UTC),
                portable_tool_provider=lambda: self.portable_tool,
            )
        self.assertEqual(calls, 1)

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
            "release": {
                **self.release_identity,
                "source_commit": "f" * 40,
            },
        }
        with self.assertRaisesRegex(ProtocolError, "release_mismatch"):
            self._factory(work_root=work).prepare(self.request)

    def test_formal_dual_end_release_identity_is_required_and_exact(self) -> None:
        work = self.root / "release-work"
        work.mkdir()
        portable = copy.deepcopy(self.portable_tool)
        native = copy.deepcopy(self.native_tool)
        self.portable_tool = {**portable, "release": None}
        self.native_tool = {**native, "release": None}
        with self.assertRaises(ProtocolError) as missing:
            self._factory(work_root=work).prepare(self.request)
        self.assertEqual(
            missing.exception.code,
            "admission_ingestion.release_missing",
        )

        cases = [
            (
                "version",
                lambda tool: None,
                lambda tool: tool.update(
                    version="9.9.9",
                    release={**tool["release"], "version": "9.9.9"},
                ),
            ),
            (
                "source_commit",
                lambda tool: tool["release"].update(
                    source_commit="c" * 40
                ),
                lambda tool: None,
            ),
            (
                "source_tree",
                lambda tool: None,
                lambda tool: tool["release"].update(
                    source_tree="d" * 40
                ),
            ),
        ]
        for field, mutate_portable, mutate_native in cases:
            with self.subTest(field=field):
                portable = copy.deepcopy(self.portable_tool)
                native = copy.deepcopy(self.native_tool)
                portable["release"] = copy.deepcopy(self.release_identity)
                portable["version"] = __version__
                native["release"] = copy.deepcopy(self.release_identity)
                native["version"] = __version__
                mutate_portable(portable)
                mutate_native(native)
                self.portable_tool = portable
                self.native_tool = native
                with self.assertRaises(ProtocolError) as mismatch:
                    self._factory(work_root=work).prepare(self.request)
                self.assertEqual(
                    mismatch.exception.code,
                    "admission_ingestion.release_mismatch",
                )
                self.assertIn(
                    field,
                    mismatch.exception.detail["fields"],
                )

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
