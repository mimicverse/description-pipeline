"""Real PostgreSQL and loopback restart proof for claim-free effect reconciliation."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid
from unittest.mock import AsyncMock, patch
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:
    import pgserver
    import psycopg2
except ImportError:
    pgserver = None
    psycopg2 = None

from description_pipeline.artifacts import ArtifactStore
from description_pipeline.io import digest, write_json
from description_pipeline.orchestration.airflow_effect_trigger import EffectPollTrigger
from description_pipeline.pipeline.application import EffectWakeupHint
from description_pipeline.pipeline.contracts import ArtifactRef, ExecutionClaim, ProtocolError
from description_pipeline.pipeline.diagnostic import DiagnosticAdmissionSpec, build_diagnostic_command
from description_pipeline.pipeline.effects import EffectState
from description_pipeline.pipeline.reconciliation import (
    EffectReconciliationEvidence,
    EffectReconciliationFailure,
    EffectReconciliationService,
    reconciliation_metadata_document,
)
from description_pipeline.pipeline.engine import execute_stage
from description_pipeline.pipeline.services import CanonicalStageServices, ClaimPolicy
from description_pipeline.platform.facts import FactConflict, FactStore, FactStoreError
from description_pipeline.schemas import DIAGNOSTIC_ADMISSION_SCHEMA

PG_AVAILABLE = pgserver is not None and psycopg2 is not None
PLATFORM_SCHEMA = Path(__file__).resolve().parents[2] / "deploy" / "platform" / "schema.sql"
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


class _LoopbackLostResponseServer:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.present: dict[str, str] = {}
        self.trigger_count = 0
        self.observe_count = 0
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, _format, *_args):
                return

            def do_POST(self):
                if self.path != "/send":
                    self.send_error(404)
                    return
                length = int(self.headers.get("Content-Length") or "0")
                body = json.loads(self.rfile.read(length))
                with owner.lock:
                    owner.trigger_count += 1
                    owner.present[body["effect_id"]] = body["command_id"]
                self.close_connection = True

            def do_GET(self):
                prefix = "/observe/"
                if not self.path.startswith(prefix):
                    self.send_error(404)
                    return
                effect_id = self.path[len(prefix) :]
                with owner.lock:
                    owner.observe_count += 1
                    present = effect_id in owner.present
                if not present:
                    self.send_error(404)
                    return
                payload = json.dumps({"effect_id": effect_id, "state": "created"}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base_url(self) -> str:
        host, port = self.server.server_address
        return f"http://{host}:{port}"

    def reset(self) -> None:
        with self.lock:
            self.present.clear()
            self.trigger_count = 0
            self.observe_count = 0

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        if self.thread.is_alive():
            raise AssertionError("Loopback reconciliation server did not stop")


class _AbsentAdapter:
    def __init__(self, fact_type: str) -> None:
        self.fact_type = fact_type

    def observe(self, *, command, intent, outbound_root, workspace_root):
        if not outbound_root.is_dir():
            raise AssertionError("The original outbound artifact was not materialized")
        payload = workspace_root / f"capture-evidence-{self.fact_type}"
        payload.mkdir()
        (payload / "execution.json").write_text('{"absent":true}\n', encoding="utf-8")
        (payload / "native-tool.json").write_text('{"tool":"fixture"}\n', encoding="utf-8")
        (payload / "native-evidence.zip").write_bytes(b"PK\\x03\\x04fixture")
        return EffectReconciliationEvidence(
            fact_type=self.fact_type,
            state_class="absent",
            payload_root=payload,
            metadata={"effect_id": intent.effect_id, "present": False},
        )


class _KnownAdapter:
    def observe(self, *, command, intent, outbound_root, workspace_root):
        del command, outbound_root
        payload = workspace_root / "known-reconciliation"
        payload.mkdir()
        (payload / "observation.json").write_text('{"present":true}\n', encoding="utf-8")
        return EffectReconciliationEvidence(
            fact_type="reconciled",
            state_class="known",
            payload_root=payload,
            metadata={"effect_id": intent.effect_id, "present": True},
        )


class _FailingEvidenceAdapter:
    def observe(self, *, command, intent, outbound_root, workspace_root):
        del command, intent, outbound_root
        payload = workspace_root / "publish-evidence"
        payload.mkdir()
        (payload / "observation.json").write_text('{"raw":"response"}\n', encoding="utf-8")
        (payload / "publication-plan.json").write_text('{"plan":"original"}\n', encoding="utf-8")
        raise RuntimeError("adapter failed after downloading evidence")


class _ExistingArtifactAdapter:
    def __init__(self, artifact) -> None:
        self.artifact = artifact

    def observe(self, *, command, intent, outbound_root, workspace_root):
        del command, intent, outbound_root, workspace_root
        return EffectReconciliationEvidence(
            fact_type="reconciled",
            state_class="absent",
            payload_artifact=self.artifact,
            metadata={"source": "existing-verified-artifact"},
        )


class _ForbiddenHandler:
    def execute(self, *_args, **_kwargs):
        raise AssertionError("A cached decision must be validated before handler execution")

    def validate_candidate(self, *_args, **_kwargs):
        raise AssertionError("A cached decision must never reach candidate validation")


@unittest.skipUnless(PG_AVAILABLE, "pgserver/psycopg2 are not installed")
class EffectReconciliationPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server_root = tempfile.TemporaryDirectory(prefix="effect-reconcile-postgres-")
        cls.server = pgserver.get_server(Path(cls.server_root.name) / "pgdata", cleanup_mode="delete")
        cls.http = _LoopbackLostResponseServer()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.http.close()
        cls.server.cleanup()
        cls.server_root.cleanup()

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="effect-reconcile-runtime-")
        self.addCleanup(self.directory.cleanup)
        self.schema = "effect_reconcile_" + uuid.uuid4().hex
        schema_sql = PLATFORM_SCHEMA.read_text(encoding="utf-8").replace(
            ':"platform_schema"',
            f'"{self.schema}"',
        )
        with psycopg2.connect(self.server.get_uri()) as connection, connection.cursor() as cursor:
            cursor.execute(f'CREATE SCHEMA "{self.schema}"')
            cursor.execute(schema_sql)
            cursor.execute(f'UPDATE "{self.schema}".admission SET state = %s WHERE id = 1', ("open",))
        self.artifacts = ArtifactStore(Path(self.directory.name) / "cas")
        self.facts = FactStore(self._connect, self.artifacts)
        self.work_root = Path(self.directory.name) / "work"
        self.now = datetime(2026, 10, 25, 3, 0, tzinfo=UTC)
        self.executor = digest({"tool": "effect-handler"})
        self.observer_tool = digest({"tool": "read-only-reconciler"})
        self.http.reset()

    def _connect(self):
        return psycopg2.connect(
            self.server.get_uri(),
            options=f"-c search_path={self.schema},public",
        )

    def _query(self, statement, parameters=()):
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(statement, parameters)
            return cursor.fetchall()

    def _artifact_ref(self, artifact_id: str) -> ArtifactRef:
        row = self._query(
            "SELECT artifact_id, kind, locator, manifest_sha256, set_sha256, "
            "producer_run_id, producer_stage, producer_tool_sha256 "
            "FROM artifact WHERE artifact_id = %s",
            (artifact_id,),
        )[0]
        return ArtifactRef(*row)

    def _admit_fixture(self, suffix: str):
        input_root = Path(self.directory.name) / f"input-{suffix}"
        input_root.mkdir()
        (input_root / "payload.txt").write_text(f"{suffix}\n", encoding="utf-8")
        input_artifact = self.artifacts.seal_tree(
            input_root,
            kind="fixture_input",
            producer_run_id="historical-run",
            producer_stage="capture",
            producer_tool_sha256=digest({"tool": "historical"}),
        )
        spec = DiagnosticAdmissionSpec(
            schema_version=DIAGNOSTIC_ADMISSION_SCHEMA,
            run_id=f"effect-run-{suffix}",
            request_id=f"effect-request-{suffix}",
            target_stage="capture",
            request={"schema_version": "effect-fixture/v1"},
            input_artifacts=(input_artifact,),
            expected_executor_sha256=self.executor,
            config_snapshot_sha256=digest({"config": 1}),
            contract_sha256=digest({"contract": 1}),
            policy_sha256=digest({"policy": 1}),
            created_at=self.now,
        )
        command = build_diagnostic_command(spec)
        self.facts.admit_diagnostic(spec, command)
        return command

    def _child_environment(self) -> dict[str, str]:
        environment = os.environ.copy()
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        environment["PYTHONPATH"] = os.pathsep.join(
            (str(REPOSITORY_ROOT / "src"), str(REPOSITORY_ROOT), environment.get("PYTHONPATH", ""))
        )
        return environment

    def _run_child(self, *arguments: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-B", "-m", "tests.v1.effect_reconcile_child", *arguments],
            cwd=REPOSITORY_ROOT,
            env=self._child_environment(),
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )

    def _authorize_unknown_effect(self, suffix: str):
        command = self._admit_fixture(suffix)
        owner = digest({"process": uuid.uuid4().hex})
        services = CanonicalStageServices(
            facts=self.facts,
            artifacts=self.artifacts,
            work_root=self.work_root / f"{suffix}-source",
            claim_policy=ClaimPolicy(
                owner_sha256=owner,
                executor_sha256=self.executor,
                lease=timedelta(minutes=5),
            ),
            clock=lambda: self.now,
        )
        claim = services.acquire_claim(command)
        workspace = services.materialize(command, claim, candidate=None)
        self.addCleanup(services.discard_workspace, workspace)
        effects = services.effect_port(command, claim, workspace)
        outbound = workspace.root / "outbound"
        outbound.mkdir()
        (outbound / "request.json").write_text('{"effect":"fixture"}\n', encoding="utf-8")
        intent = effects.prepare_effect(
            effect_key="native",
            effect_kind="native_operation",
            target={"endpoint": "local-fixture"},
            expected_state={"state": "created"},
            outbound_root=outbound,
        )
        effects.authorize_dispatch(intent.effect_id, source="old-process")
        return command, services, workspace, effects, intent

    def test_process_restart_reconciles_lost_response_then_resumes_without_resend(self) -> None:
        command = self._admit_fixture("restart")
        old_owner = digest({"process": uuid.uuid4().hex})
        new_owner = digest({"process": uuid.uuid4().hex})
        common = (
            "--database-uri",
            self.server.get_uri(),
            "--schema",
            self.schema,
            "--artifact-root",
            str(self.artifacts.root),
            "--work-root",
            str(self.work_root),
            "--command-id",
            command.command_id,
            "--executor-sha256",
            self.executor,
        )
        sent = self._run_child(
            "--mode",
            "send",
            *common,
            "--owner-sha256",
            old_owner,
            "--now",
            self.now.isoformat(),
            "--base-url",
            self.http.base_url,
        )
        self.assertEqual(sent.returncode, 23, sent.stderr)
        self.assertEqual(self.http.trigger_count, 1)
        self.assertEqual(
            self.facts.effect_states_for_command(command.command_id),
            {self.facts.effect_ids_for_command(command.command_id)[0]: EffectState.UNKNOWN},
        )
        later = self.now + timedelta(minutes=10)
        with self.assertRaisesRegex(FactConflict, "unresolved"):
            self.facts.acquire_claim(
                command.command_id,
                claim_id="premature-claim",
                owner_sha256=new_owner,
                executor_sha256=self.executor,
                now=later,
                expires_at=later + timedelta(minutes=5),
            )
        retry_services = CanonicalStageServices(
            facts=self.facts,
            artifacts=self.artifacts,
            work_root=self.work_root / "unreconciled-retry",
            claim_policy=ClaimPolicy(
                owner_sha256=new_owner,
                executor_sha256=self.executor,
                lease=timedelta(minutes=5),
            ),
            clock=lambda: later,
        )
        with self.assertRaisesRegex(FactConflict, "unresolved"):
            execute_stage(
                command_id=command.command_id,
                handler=_ForbiddenHandler(),
                services=retry_services,
            )
        self.assertEqual(self.http.trigger_count, 1)

        resumed = self._run_child(
            "--mode",
            "resume",
            *common,
            "--owner-sha256",
            new_owner,
            "--now",
            later.isoformat(),
            "--observer-tool-sha256",
            self.observer_tool,
        )
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertEqual(self.http.trigger_count, 1)
        self.assertEqual(self.http.observe_count, 1)
        settlement = self.facts.effect_settlement_for_key(command.command_id, "native")
        self.assertIsNotNone(settlement)
        self.assertEqual(settlement.state, EffectState.SATISFIED)
        self.assertIsNotNone(settlement.outbound_artifact)
        self.assertEqual(
            settlement.outbound_artifact.artifact_id,
            self._query(
                "SELECT outbound_artifact_id FROM effect_intent WHERE effect_id = %s",
                (settlement.effect_id,),
            )[0][0],
        )
        self.assertIsNotNone(settlement.payload_artifact)
        self.artifacts.verify(settlement.payload_artifact)
        self.assertIsNotNone(settlement.decisive_fact)
        self.assertIsNotNone(settlement.decisive_fact.metadata_artifact_id)
        with tempfile.TemporaryDirectory(prefix="published-evidence-") as directory:
            payload = self.artifacts.materialize(
                settlement.payload_artifact,
                Path(directory) / "payload",
            )
            self.assertEqual(
                json.loads((payload / "observation.json").read_text(encoding="utf-8"))["state"],
                "created",
            )
            self.assertIn("base_url", json.loads((payload / "publication-plan.json").read_text(encoding="utf-8")))
            self.assertNotIn("reconciliation.json", {path.name for path in payload.iterdir()})
        claims = self._query(
            "SELECT fence, owner_sha256, closure_code FROM execution_claim "
            "WHERE command_id = %s ORDER BY fence",
            (command.command_id,),
        )
        self.assertEqual(
            claims,
            [
                (1, old_owner, "reconciled"),
                (2, new_owner, "completed"),
            ],
        )
        facts = self._query(
            "SELECT f.fact_type, f.claim_id IS NULL FROM effect_fact f "
            "JOIN effect_intent i ON i.effect_id = f.effect_id "
            "WHERE i.command_id = %s ORDER BY f.seq",
            (command.command_id,),
        )
        self.assertEqual(facts, [("dispatch_authorized", False), ("reconciled", True)])
        self.assertEqual(
            self._query(
                "SELECT outcome FROM stage_decision WHERE command_id = %s",
                (command.command_id,),
            ),
            [("completed",)],
        )
        old_claim = self._query(
            "SELECT claim_id, fence, owner_sha256, executor_sha256, lane_key, expires_at "
            "FROM execution_claim WHERE command_id = %s AND fence = 1",
            (command.command_id,),
        )[0]
        old_claim_record = ExecutionClaim(
            claim_id=old_claim[0],
            command_id=command.command_id,
            fence=old_claim[1],
            owner_sha256=old_claim[2],
            executor_sha256=old_claim[3],
            lane_key=old_claim[4],
            expires_at=old_claim[5],
        )
        old_services = CanonicalStageServices(
            facts=self.facts,
            artifacts=self.artifacts,
            work_root=self.work_root / "old-claim",
            claim_policy=ClaimPolicy(
                owner_sha256=old_claim_record.owner_sha256,
                executor_sha256=self.executor,
                lease=timedelta(minutes=5),
            ),
            clock=lambda: later,
        )
        old_workspace = old_services.materialize(command, old_claim_record, candidate=None)
        self.addCleanup(old_services.discard_workspace, old_workspace)
        old_effects = old_services.effect_port(command, old_claim_record, old_workspace)
        late_observation = old_workspace.root / "late-observation"
        late_observation.mkdir()
        (late_observation / "observation.json").write_text('{"late": true}\n', encoding="utf-8")
        late_reconciliation = old_workspace.root / "late-reconciliation"
        late_reconciliation.mkdir()
        (late_reconciliation / "evidence.json").write_text('{"late": true}\n', encoding="utf-8")
        with self.assertRaisesRegex(FactConflict, "closed execution claim"):
            old_effects.record_observation(
                settlement.effect_id,
                source="late-old-observation",
                state_class="unobservable",
                observation_root=late_observation,
            )
        with self.assertRaisesRegex(FactConflict, "closed execution claim"):
            old_effects.reconcile_effect(
                settlement.effect_id,
                source="late-old-reconciliation",
                state_class="absent",
                evidence_root=late_reconciliation,
            )
        with self.assertRaisesRegex(FactConflict, "closed execution claim"):
            self.facts.append_effect_fact(
                effect_id=settlement.effect_id,
                claim=old_claim_record,
                fact_type="conflict",
                source="late-old-conflict",
                state_class=None,
                payload_artifact=None,
                payload_digest=digest({"late_conflict": command.command_id}),
                observed_at=later + timedelta(minutes=1),
                fact_id="late-conflict-" + command.command_id,
                expected_command_id=command.command_id,
            )
        self.facts.append_effect_fact(
            effect_id=settlement.effect_id,
            claim=old_claim_record,
            fact_type="response",
            source="late-old-process-response",
            state_class=None,
            payload_artifact=None,
            payload_digest=digest({"late_response": command.command_id}),
            observed_at=later + timedelta(minutes=1),
            fact_id="late-response-" + command.command_id,
            expected_command_id=command.command_id,
        )
        self.assertEqual(
            self.facts.effect_settlement_for_key(command.command_id, "native").state,
            EffectState.SATISFIED,
        )
        self.assertEqual(
            self._query(
                "SELECT f.fact_type FROM effect_fact f JOIN effect_intent i ON i.effect_id = f.effect_id "
                "WHERE i.command_id = %s ORDER BY f.seq",
                (command.command_id,),
            ),
            [("dispatch_authorized",), ("reconciled",), ("response",)],
        )
        self.assertEqual(
            self._query(
                "SELECT outcome FROM stage_decision WHERE command_id = %s",
                (command.command_id,),
            ),
            [("completed",)],
        )
        self.facts.append_effect_fact(
            effect_id=settlement.effect_id,
            claim=None,
            fact_type="conflict",
            source="independent-conflict-audit",
            state_class=None,
            payload_artifact=None,
            payload_digest=digest({"independent_conflict": command.command_id}),
            observed_at=later + timedelta(minutes=2),
            fact_id="independent-conflict-" + command.command_id,
            expected_command_id=command.command_id,
        )
        self.assertEqual(
            self.facts.effect_settlement_for_key(command.command_id, "native").state,
            EffectState.CONFLICT,
        )
        restarted_services = CanonicalStageServices(
            facts=self.facts,
            artifacts=self.artifacts,
            work_root=self.work_root / "restarted-decision",
            claim_policy=ClaimPolicy(
                owner_sha256=digest({"process": "restarted-decision"}),
                executor_sha256=self.executor,
                lease=timedelta(minutes=5),
            ),
            clock=lambda: later + timedelta(minutes=3),
        )
        with self.assertRaisesRegex(FactConflict, "non-satisfied effect"):
            execute_stage(
                command_id=command.command_id,
                handler=_ForbiddenHandler(),
                services=restarted_services,
            )
        self.assertEqual(
            self._query(
                "SELECT outcome FROM stage_decision WHERE command_id = %s",
                (command.command_id,),
            ),
            [("completed",)],
        )

    def test_conflict_absorbs_late_fence_evidence_until_verified_reconciliation(self) -> None:
        command, _services, workspace, effects, intent = self._authorize_unknown_effect("conflict-resolution")
        observation = workspace.root / "pre-conflict-observation"
        observation.mkdir()
        (observation / "observation.json").write_text('{"present":true}\n', encoding="utf-8")
        effects.record_observation(
            intent.effect_id,
            source="first-fence",
            state_class="known",
            observation_root=observation,
        )
        self.facts.append_effect_fact(
            effect_id=intent.effect_id,
            claim=None,
            fact_type="conflict",
            source="independent-conflict",
            state_class=None,
            payload_artifact=None,
            payload_digest=digest({"conflict": command.command_id}),
            observed_at=self.now + timedelta(minutes=1),
            fact_id="conflict-" + command.command_id,
            expected_command_id=command.command_id,
        )
        response = workspace.root / "late-response"
        response.mkdir()
        (response / "response.json").write_text('{"late":true}\n', encoding="utf-8")
        effects.record_response(
            intent.effect_id,
            source="late-fence",
            response_root=response,
        )
        late_observation = workspace.root / "late-observation"
        late_observation.mkdir()
        (late_observation / "observation.json").write_text('{"present":true}\n', encoding="utf-8")
        effects.record_observation(
            intent.effect_id,
            source="late-observation",
            state_class="known",
            observation_root=late_observation,
        )
        self.assertEqual(
            self.facts.effect_settlement_for_key(command.command_id, "native").state,
            EffectState.CONFLICT,
        )
        reconciler = EffectReconciliationService(
            facts=self.facts,
            artifacts=self.artifacts,
            work_root=self.work_root / "conflict-resolution",
            observer_tool_sha256=self.observer_tool,
            clock=lambda: self.now + timedelta(minutes=2),
        )
        resolved = reconciler.reconcile(
            command_id=command.command_id,
            effect_key="native",
            adapter=_KnownAdapter(),
        )
        self.assertEqual(resolved.settlement.state, EffectState.SATISFIED)
        self.assertIsNotNone(resolved.fact)
        self.assertEqual(resolved.fact.fact_type, "reconciled")
        self.assertIsNotNone(resolved.fact.metadata_artifact_id)
        self.assertEqual(
            self._query(
                "SELECT fact_type FROM effect_fact WHERE effect_id = %s ORDER BY seq",
                (intent.effect_id,),
            ),
            [
                ("dispatch_authorized",),
                ("observation",),
                ("conflict",),
                ("response",),
                ("observation",),
                ("reconciled",),
            ],
        )

    def test_reconciliation_metadata_cannot_be_reused_across_effect_closures(self) -> None:
        command_a, _services_a, _workspace_a, _effects_a, intent_a = self._authorize_unknown_effect(
            "metadata-a"
        )
        command_b, _services_b, _workspace_b, _effects_b, _intent_b = self._authorize_unknown_effect(
            "metadata-b"
        )
        reconciler = EffectReconciliationService(
            facts=self.facts,
            artifacts=self.artifacts,
            work_root=self.work_root / "metadata-source-b",
            observer_tool_sha256=self.observer_tool,
            clock=lambda: self.now + timedelta(minutes=1),
        )
        resolved_b = reconciler.reconcile(
            command_id=command_b.command_id,
            effect_key="native",
            adapter=_KnownAdapter(),
        )
        self.assertEqual(resolved_b.settlement.state, EffectState.SATISFIED)
        self.assertIsNotNone(resolved_b.settlement.payload_artifact)
        self.assertIsNotNone(resolved_b.settlement.outbound_artifact)
        self.assertIsNotNone(resolved_b.fact)
        self.assertIsNotNone(resolved_b.fact.metadata_artifact_id)
        payload_b = resolved_b.settlement.payload_artifact
        outbound_b = resolved_b.settlement.outbound_artifact
        metadata_b = self._artifact_ref(resolved_b.fact.metadata_artifact_id)
        observed_b = resolved_b.fact.observed_at

        with self.assertRaisesRegex(FactStoreError, "append_reconciliation_fact"):
            self.facts.append_effect_fact(
                effect_id=intent_a.effect_id,
                claim=None,
                fact_type="reconciled",
                source="effect-reconciler:" + self.observer_tool,
                state_class="known",
                payload_artifact=payload_b,
                payload_digest=payload_b.set_sha256,
                observed_at=observed_b,
                fact_id="common-metadata-bypass",
                expected_command_id=command_a.command_id,
                metadata_artifact=metadata_b,
            )
        with self.assertRaisesRegex(ProtocolError, "metadata_mismatch"):
            self.facts.append_reconciliation_fact(
                effect_id=intent_a.effect_id,
                fact_type="reconciled",
                source="effect-reconciler:" + self.observer_tool,
                state_class="known",
                payload_artifact=payload_b,
                metadata_artifact=metadata_b,
                payload_digest=payload_b.set_sha256,
                observed_at=observed_b,
                fact_id="foreign-effect-metadata",
                expected_command_id=command_a.command_id,
            )

        payload_root = Path(self.directory.name) / "metadata-a-payload"
        payload_root.mkdir()
        (payload_root / "payload.json").write_text('{"effect":"a"}\n', encoding="utf-8")
        payload_a = self.artifacts.seal_tree(
            payload_root,
            kind="effect_observation",
            producer_run_id=command_a.run_id,
            producer_stage=command_a.stage,
            producer_tool_sha256=self.observer_tool,
        )
        settlement_a = self.facts.effect_settlement_for_key(command_a.command_id, "native")
        self.assertIsNotNone(settlement_a.outbound_artifact)
        outbound_a = settlement_a.outbound_artifact
        observed_at = self.now + timedelta(minutes=2)

        def seal_metadata(label: str, **overrides) -> ArtifactRef:
            root = Path(self.directory.name) / f"metadata-{label}"
            root.mkdir()
            document = reconciliation_metadata_document(
                command=command_a,
                intent=intent_a,
                fact_type="reconciled",
                state_class="known",
                metadata={"label": label},
                outbound_artifact=outbound_a,
                payload_artifact=payload_a,
                observer_tool_sha256=self.observer_tool,
                observed_at=observed_at,
            )
            document.update(overrides)
            write_json(root / "metadata.json", document)
            return self.artifacts.seal_tree(
                root,
                kind="effect_reconciliation_metadata",
                producer_run_id=command_a.run_id,
                producer_stage=command_a.stage,
                producer_tool_sha256=self.observer_tool,
            )

        cases = {
            "wrong_outbound": {
                "outbound_artifact": outbound_b.to_record(),
            },
            "wrong_payload": {
                "payload_artifact": payload_b.to_record(),
            },
            "wrong_state": {
                "state_class": "absent",
            },
            "wrong_fact_type": {
                "fact_type": "observation",
            },
        }
        for label, overrides in cases.items():
            with self.subTest(case=label):
                metadata = seal_metadata(label, **overrides)
                with self.assertRaisesRegex(ProtocolError, "metadata_mismatch"):
                    self.facts.append_reconciliation_fact(
                        effect_id=intent_a.effect_id,
                        fact_type="reconciled",
                        source="effect-reconciler:" + self.observer_tool,
                        state_class="known",
                        payload_artifact=payload_a,
                        metadata_artifact=metadata,
                        payload_digest=payload_a.set_sha256,
                        observed_at=observed_at,
                        fact_id=f"invalid-{label}",
                        expected_command_id=command_a.command_id,
                    )

        cross_run_payload_metadata = seal_metadata(
            "cross-run-payload",
            payload_artifact=payload_b.to_record(),
        )
        resolved_a = self.facts.append_reconciliation_fact(
            effect_id=intent_a.effect_id,
            fact_type="reconciled",
            source="effect-reconciler:" + self.observer_tool,
            state_class="known",
            payload_artifact=payload_b,
            metadata_artifact=cross_run_payload_metadata,
            payload_digest=payload_b.set_sha256,
            observed_at=observed_at,
            fact_id="cross-run-payload-reconciliation",
            expected_command_id=command_a.command_id,
        )
        self.assertEqual(resolved_a.fact_type, "reconciled")
        self.assertEqual(
            self.facts.effect_settlement_for_key(command_a.command_id, "native").state,
            EffectState.SATISFIED,
        )

    def test_observation_absence_and_expired_lease_do_not_settle_until_reconciled(self) -> None:
        command, _services, _workspace, _effects, _intent = self._authorize_unknown_effect("absence")
        new_owner = digest({"process": uuid.uuid4().hex})
        later = self.now + timedelta(minutes=10)
        reconciliation = EffectReconciliationService(
            facts=self.facts,
            artifacts=self.artifacts,
            work_root=self.work_root / "absence-reconcile",
            observer_tool_sha256=self.observer_tool,
            clock=lambda: later,
        )
        observed = reconciliation.reconcile(
            command_id=command.command_id,
            effect_key="native",
            adapter=_AbsentAdapter("observation"),
        )
        self.assertEqual(observed.settlement.state, EffectState.UNKNOWN)
        self.assertIsNotNone(observed.settlement.payload_artifact)
        with tempfile.TemporaryDirectory(prefix="capture-evidence-") as directory:
            payload = self.artifacts.materialize(
                observed.settlement.payload_artifact,
                Path(directory) / "payload",
            )
            self.assertEqual(
                {path.name for path in payload.iterdir()},
                {"manifest.json", "execution.json", "native-tool.json", "native-evidence.zip"},
            )
            self.assertEqual((payload / "native-evidence.zip").read_bytes(), b"PK\\x03\\x04fixture")
        with self.assertRaisesRegex(FactConflict, "unresolved"):
            self.facts.acquire_claim(
                command.command_id,
                claim_id="after-observation",
                owner_sha256=new_owner,
                executor_sha256=self.executor,
                now=later,
                expires_at=later + timedelta(minutes=5),
            )
        existing_root = Path(self.directory.name) / "existing-capture-evidence"
        existing_root.mkdir()
        (existing_root / "execution.json").write_text('{"existing":true}\n', encoding="utf-8")
        (existing_root / "native-tool.json").write_text('{"tool":"existing"}\n', encoding="utf-8")
        (existing_root / "native-evidence.zip").write_bytes(b"PK\\x03\\x04existing")
        existing = self.artifacts.seal_tree(
            existing_root,
            kind="effect_observation",
            producer_run_id="historical-run",
            producer_stage="capture",
            producer_tool_sha256=digest({"tool": "historical-observer"}),
        )
        reconciled = reconciliation.reconcile(
            command_id=command.command_id,
            effect_key="native",
            adapter=_ExistingArtifactAdapter(existing),
        )
        self.assertEqual(reconciled.settlement.state, EffectState.DEFINITELY_FAILED)
        self.assertEqual(reconciled.settlement.payload_artifact, existing)
        replacement = self.facts.acquire_claim(
            command.command_id,
            claim_id="after-reconciliation",
            owner_sha256=new_owner,
            executor_sha256=self.executor,
            now=later,
            expires_at=later + timedelta(minutes=5),
        )
        self.assertEqual(replacement.fence, 2)
        self.assertEqual(self.http.trigger_count, 0)

    def test_running_trigger_wakes_after_real_absence_reconciliation_without_remote_change(self) -> None:
        command, _services, _workspace, effects, intent = self._authorize_unknown_effect("trigger-absence")
        pending = self.facts.yield_claim_for_effect(
            command=command, claim=effects.claim, effect_id=intent.effect_id,
            yielded_at=self.now + timedelta(seconds=1),
        )
        reconciliation = EffectReconciliationService(
            facts=self.facts, artifacts=self.artifacts,
            work_root=self.work_root / "trigger-reconcile",
            observer_tool_sha256=self.observer_tool,
            clock=lambda: self.now + timedelta(seconds=2),
        )
        remote = AsyncMock(return_value=EffectWakeupHint(
            wait_digest=pending.wait.digest, observed_at=self.now, changed=False,
        ))

        real_sleep = asyncio.sleep

        async def reconcile_during_wait(_delay):
            if self.facts.effect_settlement(intent.effect_id).state is EffectState.UNKNOWN:
                result = reconciliation.reconcile(
                    command_id=command.command_id, effect_key=intent.effect_key,
                    adapter=_AbsentAdapter("reconciled"),
                )
                self.assertEqual(result.settlement.state, EffectState.DEFINITELY_FAILED)
            await real_sleep(0)

        async def run_trigger():
            return await asyncio.wait_for(
                anext(EffectPollTrigger(wait=pending.wait.to_record(), poll_interval=0.1).run()),
                timeout=5,
            )

        with (
            patch.object(ArtifactStore, "from_environment", return_value=self.artifacts),
            patch.object(FactStore, "from_environment", return_value=self.facts),
            patch(
                "description_pipeline.orchestration.airflow_effect_trigger._prepare_native_effect_probe",
                new=AsyncMock(return_value=remote),
            ),
            patch(
                "description_pipeline.orchestration.airflow_effect_trigger.asyncio.sleep",
                new=AsyncMock(side_effect=reconcile_during_wait),
            ) as sleep,
        ):
            event = asyncio.run(run_trigger())
        self.assertEqual(event.payload, {"wait": pending.wait.to_record()})
        remote.assert_awaited_once_with(pending.wait)
        sleep.assert_awaited_once_with(0.1)
        self.assertEqual(self._query("SELECT count(*) FROM execution_claim"), [(1,)])
        self.assertEqual(self._query("SELECT count(*) FROM stage_decision"), [(0,)])
        self.assertEqual(self.http.trigger_count, 0)
        self.assertEqual(self.facts.effect_intent(intent.effect_id), intent)

    def test_adapter_failure_retains_unsealed_domain_evidence(self) -> None:
        command, _services, _workspace, _effects, _intent = self._authorize_unknown_effect("retained")
        reconciliation = EffectReconciliationService(
            facts=self.facts,
            artifacts=self.artifacts,
            work_root=self.work_root / "retained-reconcile",
            observer_tool_sha256=self.observer_tool,
            clock=lambda: self.now,
        )
        with self.assertRaises(EffectReconciliationFailure) as raised:
            reconciliation.reconcile(
                command_id=command.command_id,
                effect_key="native",
                adapter=_FailingEvidenceAdapter(),
            )
        retained = raised.exception.retained_workspace
        self.assertEqual(
            (retained / "publish-evidence" / "observation.json").read_text(encoding="utf-8"),
            '{"raw":"response"}\n',
        )
        self.assertEqual(
            (retained / "publish-evidence" / "publication-plan.json").read_text(encoding="utf-8"),
            '{"plan":"original"}\n',
        )
        self.assertTrue((retained / "reconciliation-failure.json").is_file())
        self.assertEqual(
            self.facts.effect_states_for_command(command.command_id),
            {self.facts.effect_ids_for_command(command.command_id)[0]: EffectState.UNKNOWN},
        )
        self.assertEqual(
            self._query(
                "SELECT f.fact_type FROM effect_fact f JOIN effect_intent i ON i.effect_id = f.effect_id "
                "WHERE i.command_id = %s ORDER BY f.seq",
                (command.command_id,),
            ),
            [("dispatch_authorized",)],
        )


if __name__ == "__main__":
    unittest.main()
