"""Real PostgreSQL composition tests for the unified stage runtime.

The test is skipped unless the separately installed ``pgserver`` and
``psycopg2`` packages are on ``PYTHONPATH``. When present it starts an isolated
throwaway cluster and never contacts an existing platform or Airflow service.
"""

from __future__ import annotations

import json
import hashlib
import tempfile
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib import error as urlerror
from urllib import request as urlrequest
from unittest import mock

try:
    import pgserver
    import psycopg2
except ImportError:
    pgserver = None
    psycopg2 = None

from description_pipeline.io import canonical, digest, read_data, write_json
from description_pipeline.pipeline import (
    DispatchCommand,
    DispatchConf,
    DispatchObservation,
    EffectState,
    ExecutionClaim,
    StageCommand,
    StageExecution,
    StageFailure,
    StageResult,
    TechnicalDispatchService,
    VerifiedArtifact,
    VerifiedDiscoverResult,
    VerifiedPublishIntent,
    VerifiedPredecessor,
    VerifiedRepositorySnapshot,
    VerifiedVerifyResult,
    build_capture_command,
    build_discover_command,
    build_freeze_command,
    build_generate_command,
    build_publish_command,
    build_run_admission,
    build_verify_command,
    execute_stage,
)
from description_pipeline.pipeline.contracts import ProtocolError
from description_pipeline.pipeline.publish_contract import (
    REPOSITORY_SNAPSHOT_FILE,
    REPOSITORY_SNAPSHOT_KIND,
    REPOSITORY_SNAPSHOT_METADATA,
    REPOSITORY_SNAPSHOT_SCHEMA,
)
from description_pipeline.pipeline.services import CanonicalStageServices, ClaimPolicy
from description_pipeline.pipeline.verify_contract import (
    CERTIFICATE_FILENAME,
    EXECUTION_RECEIPT_FILENAME,
)
from description_pipeline.platform.facts import FactConflict, FactStore, FactStoreError
from description_pipeline.repository.publish import CommitSpec
from description_pipeline.schemas import (
    AIRFLOW_DISPATCH_CONF_SCHEMA,
    DISCOVER_RESULT_SCHEMA,
    STAGE_RESULT_SCHEMA,
    VERIFICATION_CERTIFICATE_SCHEMA,
    VERIFY_REQUEST_SCHEMA,
)
from tests.v1.test_pipeline_admission import AdmissionHarness

PG_AVAILABLE = pgserver is not None and psycopg2 is not None
PLATFORM_SCHEMA = Path(__file__).resolve().parents[2] / "deploy" / "platform" / "schema.sql"


class _EffectHandler:
    def __init__(self) -> None:
        self.external_calls = 0
        self.validations = 0

    def execute(self, command, claim, workspace, *, effects):
        outbound = workspace.root / "outbound"
        outbound.mkdir()
        (outbound / "request.json").write_text('{"operation": "native"}\n', encoding="utf-8")
        intent = effects.prepare_effect(
            effect_key="native",
            effect_kind="native_operation",
            target={"endpoint": "local-test", "operation": command.command_id},
            expected_state={"operation": command.command_id, "state": "complete"},
            outbound_root=outbound,
        )
        effects.authorize_dispatch(intent.effect_id, source="runtime-test")
        self.external_calls += 1

        response = workspace.root / "response"
        response.mkdir()
        (response / "response.json").write_text('{"state": "complete"}\n', encoding="utf-8")
        effects.record_response(intent.effect_id, source="runtime-test", response_root=response)

        observation = workspace.root / "observation"
        observation.mkdir()
        (observation / "observation.json").write_text('{"state": "complete"}\n', encoding="utf-8")
        effects.record_observation(
            intent.effect_id,
            source="runtime-test",
            state_class="known",
            observation_root=observation,
        )

        output = workspace.root / "output"
        output.mkdir()
        (output / "model.txt").write_text("verified output\n", encoding="utf-8")
        return StageExecution(
            outcome="completed",
            candidate_root=str(output),
            checks_sha256=digest({"checks": "passed"}),
            resources_settled=True,
        )

    def validate_candidate(self, command, claim, candidate, *, effects):
        self.validations += 1
        return candidate


class _FailureHandler:
    def execute(self, command, claim, workspace, *, effects):
        raise StageFailure(
            "domain_input_invalid",
            "The domain input is definitely invalid",
            unavailable_reason="diagnostic sealing deliberately unavailable",
        )

    def validate_candidate(self, command, claim, candidate, *, effects):
        raise AssertionError("A failing execution must not validate a candidate")


class _UnsealedFailureHandler:
    def execute(self, command, claim, workspace, *, effects):
        diagnostics = workspace.root / "missing-diagnostics"
        diagnostics.mkdir()
        (diagnostics / "diagnostic.txt").write_text("raw native failure\n", encoding="utf-8")
        (diagnostics / "manifest.json").write_text("{}\n", encoding="utf-8")
        return StageExecution(
            outcome="failed",
            candidate_root=str(diagnostics),
            checks_sha256=digest({"checks": "failed"}),
            resources_settled=False,
            failure=StageFailure(
                "domain_failed",
                "The original domain failure",
                detail={"field": "assembly", "value": "missing"},
                retained_locations=(str(diagnostics),),
            ),
        )

    def validate_candidate(self, command, claim, candidate, *, effects):
        raise AssertionError("An unsealed failure has no candidate")


class _UnexpectedFailureHandler:
    def execute(self, command, claim, workspace, *, effects):
        (workspace.root / "native-attempt.txt").write_text("in-flight native state\n", encoding="utf-8")
        (workspace.root / "failure.json").write_text("original domain failure\n", encoding="utf-8")
        raise RuntimeError("unexpected native state")

    def validate_candidate(self, command, claim, candidate, *, effects):
        raise AssertionError("An unhandled failure has no candidate")


class _AirflowStub:
    """One local, controlled Airflow HTTP stub with exact fixed identities."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.present: set[tuple[str, str]] = set()
        self.trigger_count = 0
        self.observe_count = 0
        self.lost_response = False
        self.expected_conf: dict[str, object] | None = None
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, _format, *_args):
                return

            def _identity(self):
                parts = self.path.strip("/").split("/")
                if len(parts) != 6 or parts[:3] != ["api", "v1", "dags"] or parts[4] != "dagRuns":
                    self.send_error(404)
                    return None
                return parts[3], parts[5]

            def _authorized(self):
                if self.headers.get("Authorization") != "Bearer controlled-token":
                    self.send_error(401)
                    return False
                return True

            def do_POST(self):
                identity = self._identity()
                if identity is None or not self._authorized():
                    return
                length = int(self.headers.get("Content-Length") or "0")
                body = json.loads(self.rfile.read(length) or b"{}")
                if body != stub.expected_conf:
                    self.send_error(409)
                    return
                with stub.lock:
                    stub.trigger_count += 1
                    stub.present.add(identity)
                    lost = stub.lost_response
                if lost:
                    self.close_connection = True
                    return
                payload = json.dumps({"dag_id": identity[0], "dag_run_id": identity[1]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self):
                identity = self._identity()
                if identity is None or not self._authorized():
                    return
                with stub.lock:
                    stub.observe_count += 1
                    present = identity in stub.present
                if not present:
                    self.send_error(404)
                    return
                payload = json.dumps(
                    {"dag_id": identity[0], "dag_run_id": identity[1], "state": "queued"}
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.assert_stopped()

    def assert_stopped(self) -> None:
        if self.thread.is_alive():
            raise AssertionError("Airflow HTTP stub did not stop")

    @property
    def base_url(self) -> str:
        host, port = self.server.server_address
        return f"http://{host}:{port}"


class _HttpDispatchAdapter:
    def __init__(self, *, base_url: str, credential_file: Path) -> None:
        self.base_url = base_url.rstrip("/")
        self.credential_file = credential_file

    def _request(self, method: str, command: DispatchCommand, conf: DispatchConf):
        body = None if method == "GET" else json.dumps(conf.to_record(), sort_keys=True).encode()
        request = urlrequest.Request(
            f"{self.base_url}/api/v1/dags/{command.dag_id}/dagRuns/{command.dag_run_id}",
            data=body,
            method=method,
            headers={
                "Authorization": "Bearer " + self.credential_file.read_text(encoding="utf-8").strip(),
                "Content-Type": "application/json",
            },
        )
        with urlrequest.urlopen(request, timeout=3) as response:
            return json.loads(response.read())

    def trigger(self, command: DispatchCommand, conf: DispatchConf):
        return self._request("POST", command, conf)

    def observe(self, command: DispatchCommand, conf: DispatchConf) -> DispatchObservation:
        try:
            payload = self._request("GET", command, conf)
        except urlerror.HTTPError as error:
            if error.code == 404:
                return DispatchObservation("absent", {"error": "not_found"})
            raise
        except Exception as error:
            return DispatchObservation(
                "unobservable",
                {"error_type": type(error).__name__, "error_message": str(error)[:200]},
            )
        return DispatchObservation("known", payload)


@unittest.skipUnless(PG_AVAILABLE, "pgserver/psycopg2 are not installed")
class RuntimeCandidatePostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server_root = tempfile.TemporaryDirectory(prefix="runtime-candidate-postgres-")
        cls.server = pgserver.get_server(Path(cls.server_root.name) / "pgdata", cleanup_mode="delete")
        cls.airflow_stub = _AirflowStub()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.airflow_stub.close()
        cls.server.cleanup()
        cls.server_root.cleanup()

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="runtime-candidate-cas-")
        self.addCleanup(self.directory.cleanup)
        self.schema = "runtime_" + uuid.uuid4().hex
        schema_sql = PLATFORM_SCHEMA.read_text(encoding="utf-8").replace(
            ':"platform_schema"',
            f'"{self.schema}"',
        )
        with psycopg2.connect(self.server.get_uri()) as connection, connection.cursor() as cursor:
            cursor.execute(f'CREATE SCHEMA "{self.schema}"')
            cursor.execute(schema_sql)
            cursor.execute(f'UPDATE "{self.schema}".admission SET state = %s WHERE id = 1', ("open",))

        self.admission_harness = AdmissionHarness(Path(self.directory.name) / "admission")
        self.now = self.admission_harness.spec.created_at
        self.executor = self.admission_harness.spec.executors.freeze
        self.owner = digest({"process": str(uuid.uuid4())})
        self.artifacts = self.admission_harness.store
        self.facts = FactStore(self._connect, self.artifacts)
        self.admission = build_run_admission(self.admission_harness.spec)
        self.command = build_freeze_command(self.admission_harness.spec)
        self.dispatch_executor = digest({"tool": "portal-dispatch"})
        self.dispatch_conf = DispatchConf(
            schema_version=AIRFLOW_DISPATCH_CONF_SCHEMA,
            run_id=self.admission.run_id,
            freeze_command_id=self.command.command_id,
        )
        self.conf_artifact = self.artifacts.seal_dispatch_conf(
            self.dispatch_conf,
            producer_run_id=self.admission.run_id,
            producer_tool_sha256=self.dispatch_executor,
        )
        self.dispatch_command = DispatchCommand.create(
            command_id="command-dispatch",
            run_id=self.admission.run_id,
            freeze_command_id=self.command.command_id,
            idempotency_key="run-1/dispatch",
            dag_id="solidworks_to_urdf",
            dag_run_id="portal-run-1",
            conf_artifact=self.conf_artifact,
            expected_executor_sha256=self.dispatch_executor,
        )
        self.freeze_command, self.dispatch_command, self.dispatch_effect = self.facts.admit_run(
            self.admission,
            self.command,
            self.dispatch_command,
            self.conf_artifact,
        )
        self.services = CanonicalStageServices(
            facts=self.facts,
            artifacts=self.artifacts,
            work_root=Path(self.directory.name) / "work",
            claim_policy=ClaimPolicy(
                owner_sha256=self.owner,
                executor_sha256=self.executor,
                lease=timedelta(minutes=5),
                claim_id_factory=lambda: "claim-" + str(uuid.uuid4()),
            ),
            clock=lambda: self.now,
        )
        with self.airflow_stub.lock:
            self.airflow_stub.present.clear()
            self.airflow_stub.trigger_count = 0
            self.airflow_stub.observe_count = 0
            self.airflow_stub.lost_response = False
            self.airflow_stub.expected_conf = dict(self.dispatch_conf.to_record())
        credential = Path(self.directory.name) / "airflow-token"
        credential.write_text("controlled-token\n", encoding="utf-8")
        self.dispatch_service = TechnicalDispatchService(
            facts=self.facts,
            artifacts=self.artifacts,
            work_root=Path(self.directory.name) / "dispatch-work",
            claim_policy=ClaimPolicy(
                owner_sha256=digest({"process": "dispatch-" + str(uuid.uuid4())}),
                executor_sha256=self.dispatch_executor,
                lease=timedelta(minutes=5),
                claim_id_factory=lambda: "dispatch-claim-" + str(uuid.uuid4()),
            ),
            clock=lambda: self.now,
            adapter=_HttpDispatchAdapter(
                base_url=self.airflow_stub.base_url,
                credential_file=credential,
            ),
        )

    def _connect(self):
        connection = psycopg2.connect(
            self.server.get_uri(),
            options=f"-c search_path={self.schema},public",
        )
        return connection

    def _execute(self, handler):
        return execute_stage(
            command_id=self.command.command_id,
            handler=handler,
            services=self.services,
        )

    def _query(self, statement, parameters=()):
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(statement, parameters)
            return cursor.fetchall()

    def _new_admission(self, suffix: str):
        spec = replace(
            self.admission_harness.spec,
            run_id=f"run-{suffix}",
            request_id=f"request-{suffix}",
            parent_run_id=None,
            from_stage="freeze",
        )
        admission = build_run_admission(spec)
        command = build_freeze_command(spec)
        return admission, command

    def _dispatch_for(self, admission, command, *, conf_freeze_id=None):
        conf = DispatchConf(
            schema_version=AIRFLOW_DISPATCH_CONF_SCHEMA,
            run_id=admission.run_id,
            freeze_command_id=conf_freeze_id or command.command_id,
        )
        artifact = self.artifacts.seal_dispatch_conf(
            conf,
            producer_run_id=admission.run_id,
            producer_tool_sha256=self.dispatch_executor,
        )
        dispatch = DispatchCommand.create(
            command_id=f"command-dispatch-{admission.run_id}",
            run_id=admission.run_id,
            freeze_command_id=command.command_id,
            idempotency_key=f"{admission.run_id}/dispatch",
            dag_id="solidworks_to_urdf",
            dag_run_id=f"portal-{admission.run_id}",
            conf_artifact=artifact,
            expected_executor_sha256=self.dispatch_executor,
        )
        return dispatch, artifact

    def _predecessor_for(self, command: StageCommand) -> VerifiedPredecessor:
        stored = self.facts.load_command(command.command_id)
        decision = self.facts.decision_for(stored)
        self.assertIsNotNone(decision)
        result = self.facts.load_result(decision)
        return VerifiedPredecessor(
            stage=stored.stage,
            command=stored,
            result=result,
            decision=decision,
        )

    def _persist_predecessor_decision(self, predecessor: VerifiedPredecessor) -> None:
        self.facts.register_artifact(predecessor.result.output_artifact)
        self.facts.register_artifact(predecessor.decision.result_record_artifact)
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO stage_decision (
                    decision_id, run_id, stage, command_id, outcome, result_record_artifact_id,
                    result_digest, actual_executor_sha256, original_producer_decision_id,
                    reason_code, failure_envelope, failure_digest, decided_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NULL, NULL, NULL, NULL, %s)
                """,
                (
                    predecessor.decision.decision_id,
                    predecessor.decision.run_id,
                    predecessor.decision.stage,
                    predecessor.decision.command_id,
                    predecessor.decision.outcome,
                    predecessor.decision.result_record_artifact.artifact_id,
                    predecessor.decision.result_digest,
                    predecessor.decision.actual_executor_sha256,
                    predecessor.decision.decided_at,
                ),
            )

    def _discover_payload(self, command: StageCommand) -> dict[str, object]:
        files = {"cad/robot.SLDASM": self.admission_harness.originals.inventory["cad/robot.SLDASM"]}
        return {
            "schema_version": DISCOVER_RESULT_SCHEMA,
            "command_id": command.command_id,
            "run_id": self.admission_harness.spec.run_id,
            "request_digest": command.request_digest,
            "selection": {
                "files": files,
                "handoff_sha256": digest(files),
                "upload_sha256": self.admission_harness.originals.inventory_sha256,
                "main_assembly": self.admission_harness.spec.main_assembly,
            },
            "prepared_sha256": digest({"prepared": 1}),
            "native_observation_sha256": digest({"observation": 1}),
            "source_set_artifact": self.admission_harness.originals_ref.to_record(),
            "ruleset_artifact": self.admission_harness.ruleset_ref.to_record(),
            "configuration_artifact": self.admission_harness.configuration_ref.to_record(),
            "native_tool": self.admission_harness.native_tool,
            "passed": True,
        }

    def test_admission_binds_run_freeze_dispatch_conf_and_effect_atomically(self) -> None:
        replay = self.facts.admit_run(
            self.admission,
            self.command,
            self.dispatch_command,
            self.conf_artifact,
        )
        self.assertEqual(replay[0], self.command)
        self.assertEqual(replay[1], self.dispatch_command)
        rows = self._query(
            "SELECT "
            "(SELECT count(*) FROM run_admission WHERE run_id = %s),"
            "(SELECT count(*) FROM command WHERE run_id = %s AND kind = 'stage'),"
            "(SELECT count(*) FROM command WHERE run_id = %s AND kind = 'dispatch'),"
            "(SELECT count(*) FROM effect_intent WHERE command_id = %s),"
            "(SELECT count(*) FROM stage_decision WHERE command_id = %s)",
            (
                self.admission.run_id,
                self.admission.run_id,
                self.admission.run_id,
                self.dispatch_command.command_id,
                self.dispatch_command.command_id,
            ),
        )
        self.assertEqual(rows[0], (1, 1, 1, 1, 0))

    def test_forged_freeze_command_is_refused_by_typed_admission_before_writes(self) -> None:
        admission, command = self._new_admission("forged")
        dispatch, artifact = self._dispatch_for(admission, command)
        forged_request = {**dict(command.request), "principal": "forged-principal"}
        forged = AdmissionHarness.manual(command, request=forged_request)
        with self.assertRaisesRegex(FactStoreError, "typed semantic validation"):
            self.facts.admit_run(
                admission,
                forged,
                dispatch,
                artifact,
            )
        rows = self._query(
            "SELECT "
            "(SELECT count(*) FROM run_admission WHERE run_id = %s),"
            "(SELECT count(*) FROM command WHERE run_id = %s),"
            "(SELECT count(*) FROM effect_intent WHERE command_id = %s)",
            (admission.run_id, admission.run_id, dispatch.command_id),
        )
        self.assertEqual(rows[0], (0, 0, 0))

    def test_wrong_dispatch_conf_binding_is_refused_before_any_admission_write(self) -> None:
        admission, command = self._new_admission("wrong-conf")
        dispatch, artifact = self._dispatch_for(
            admission,
            command,
            conf_freeze_id="command-from-another-run",
        )
        with self.assertRaisesRegex(FactStoreError, "conf names another"):
            self.facts.admit_run(admission, command, dispatch, artifact)
        rows = self._query(
            "SELECT count(*) FROM run_admission WHERE run_id = %s",
            (admission.run_id,),
        )
        self.assertEqual(rows[0][0], 0)

    def test_wrong_original_artifact_is_refused_before_any_admission_write(self) -> None:
        admission, command = self._new_admission("wrong-original")
        alternate = self.admission_harness.alternate("freeze_originals", "alternate-originals")
        forged_inputs = (alternate, *command.input_artifacts[1:])
        forged = AdmissionHarness.manual(command, input_artifacts=forged_inputs)
        dispatch, artifact = self._dispatch_for(admission, forged)
        with self.assertRaisesRegex(FactStoreError, "typed semantic validation"):
            self.facts.admit_run(admission, forged, dispatch, artifact)
        rows = self._query(
            "SELECT "
            "(SELECT count(*) FROM run_admission WHERE run_id = %s),"
            "(SELECT count(*) FROM command WHERE run_id = %s),"
            "(SELECT count(*) FROM effect_intent WHERE command_id = %s)",
            (admission.run_id, admission.run_id, dispatch.command_id),
        )
        self.assertEqual(rows[0], (0, 0, 0))

    def test_discover_is_rebuilt_from_the_registered_freeze_fact_before_insert(self) -> None:
        self._execute(_EffectHandler())
        freeze_command = self.facts.load_command(self.command.command_id)
        freeze_decision = self.facts.decision_for(freeze_command)
        self.assertIsNotNone(freeze_decision)
        freeze_result = self.facts.load_result(freeze_decision)
        predecessor = VerifiedPredecessor(
            stage="freeze",
            command=freeze_command,
            result=freeze_result,
            decision=freeze_decision,
        )
        discover = build_discover_command(self.admission_harness.spec, predecessor)
        self.assertEqual(
            self.facts.create_command(
                discover,
                admission=self.admission,
                predecessor=predecessor,
            ),
            discover,
        )
        self.assertEqual(self.facts.load_command(discover.command_id), discover)

        forged = AdmissionHarness.manual(
            discover,
            request={**dict(discover.request), "purpose": "inspection_preview"},
        )
        with self.assertRaisesRegex(FactStoreError, "typed semantic validation"):
            self.facts.create_command(
                forged,
                admission=self.admission,
                predecessor=predecessor,
            )
        rows = self._query(
            "SELECT count(*) FROM command WHERE run_id = %s AND stage = 'discover'",
            (self.admission.run_id,),
        )
        self.assertEqual(rows[0][0], 1)

    def test_store_persists_the_six_supported_projection_stages_in_order(self) -> None:
        self._execute(_EffectHandler())
        freeze = self._predecessor_for(self.command)

        discover_command = build_discover_command(self.admission_harness.spec, freeze)
        self.facts.create_command(
            discover_command,
            admission=self.admission,
            predecessor=freeze,
        )
        discover = self.admission_harness.predecessor(
            "discover",
            discover_command,
            self._discover_payload(discover_command),
        )
        self._persist_predecessor_decision(discover)
        self.artifacts.verify(discover.result.output_artifact)
        discover_result = VerifiedDiscoverResult.from_record(
            discover,
            read_data(
                self.artifacts.objects
                / discover.result.output_artifact.artifact_id
                / "result.json"
            ),
        )

        capture_command = build_capture_command(
            self.admission_harness.spec,
            freeze,
            discover,
            discover_result,
        )
        self.assertNotEqual(
            capture_command.expected_executor_sha256,
            capture_command.request["native_tool_source_sha256"],
        )
        self.facts.create_command(
            capture_command,
            admission=self.admission,
            predecessor=discover,
            supporting_predecessors=(freeze,),
            discover_result=discover_result,
        )
        capture = self.admission_harness.predecessor(
            "capture",
            capture_command,
            {"stage": "capture"},
        )
        self._persist_predecessor_decision(capture)

        generate_command = build_generate_command(self.admission_harness.spec, capture)
        self.facts.create_command(
            generate_command,
            admission=self.admission,
            predecessor=capture,
        )
        generate = self.admission_harness.predecessor(
            "generate",
            generate_command,
            {"stage": "generate"},
        )
        self._persist_predecessor_decision(generate)

        verify_command = build_verify_command(
            self.admission_harness.spec,
            generate,
            capture,
        )
        self.facts.create_command(
            verify_command,
            admission=self.admission,
            predecessor=generate,
            supporting_predecessors=(capture,),
        )
        verify_request = {
            "capture_artifact": capture.result.output_artifact.to_record(),
            "config_snapshot_sha256": self.admission_harness.configuration_ref.set_sha256,
            "contract_sha256": self.admission_harness.spec.contract_sha256,
            "expected_verifier_sha256": self.admission_harness.spec.executors.verify,
            "generate_artifact": generate.result.output_artifact.to_record(),
            "output_contract": "solidworks-to-urdf.verification-artifact/v1",
            "policy_sha256": self.admission_harness.spec.policy_sha256,
            "purpose": self.admission_harness.spec.purpose,
            "schema_version": VERIFY_REQUEST_SCHEMA,
        }
        certificate = {
            "actual_verifier": {"executor": "verify"},
            "actual_verifier_sha256": self.admission_harness.spec.executors.verify,
            "capture_artifact": capture.result.output_artifact.to_record(),
            "config_snapshot_sha256": self.admission_harness.configuration_ref.set_sha256,
            "contract_sha256": self.admission_harness.spec.contract_sha256,
            "decision": "PASS",
            "generate_artifact": generate.result.output_artifact.to_record(),
            "passed": True,
            "policy_sha256": self.admission_harness.spec.policy_sha256,
            "purpose": self.admission_harness.spec.purpose,
            "request": verify_request,
            "request_sha256": digest(verify_request),
            "schema_version": VERIFICATION_CERTIFICATE_SCHEMA,
            "subject_sha256": generate.result.output_artifact.set_sha256,
        }
        certificate["verification_key"] = digest(certificate)
        certificate_bytes = canonical(certificate)
        verify = self.admission_harness._predecessor(
            "verify",
            verify_command,
            {},
            files={
                CERTIFICATE_FILENAME: certificate_bytes,
                EXECUTION_RECEIPT_FILENAME: b"{}\n",
            },
        )
        self._persist_predecessor_decision(verify)
        verify_result = VerifiedVerifyResult.from_artifact_bytes(
            verify,
            VerifiedArtifact.from_store_record(
                verify.result.output_artifact,
                self.artifacts.verify(verify.result.output_artifact),
            ),
            certificate_bytes,
        )

        snapshot_root = Path(self.directory.name) / "publish-snapshot"
        bundle_bytes = b"repository snapshot"
        snapshot_metadata = {
            "schema_version": REPOSITORY_SNAPSHOT_SCHEMA,
            "repository": "example/robot",
            "origin_url": "https://example.test/example/robot.git",
            "base_ref": "main",
            "base_oid": "1" * 40,
            "branch_ref": "work/solidworks/robot/engineering",
            "head_oid": None,
            "bundle_sha256": digest({"unused": "replaced below"}),
        }
        snapshot_metadata["bundle_sha256"] = hashlib.sha256(bundle_bytes).hexdigest()
        write_json(snapshot_root / REPOSITORY_SNAPSHOT_METADATA, snapshot_metadata)
        (snapshot_root / REPOSITORY_SNAPSHOT_FILE).write_bytes(bundle_bytes)
        snapshot_ref = self.artifacts.seal_tree(
            snapshot_root,
            kind=REPOSITORY_SNAPSHOT_KIND,
            producer_run_id=None,
            producer_stage=None,
            producer_tool_sha256=digest({"tool": "snapshot"}),
        )
        publish_intent = VerifiedPublishIntent(
            repository=VerifiedRepositorySnapshot.from_metadata_bytes(
                VerifiedArtifact.from_store_record(
                    snapshot_ref,
                    self.artifacts.verify(snapshot_ref),
                ),
                (snapshot_root / REPOSITORY_SNAPSHOT_METADATA).read_bytes(),
            ),
            commit=CommitSpec(
                author_name="Publish Bot",
                author_email="publish@example.test",
                authored_at="2026-10-25T00:00:00+00:00",
                committer_name="Publish Bot",
                committer_email="publish@example.test",
                committed_at="2026-10-25T00:00:00+00:00",
                subject="feat(robot): publish URDF",
                body="verified publication",
            ),
        )
        publish_command = build_publish_command(
            self.admission_harness.spec,
            verify_result,
            publish_intent,
        )
        self.facts.create_command(
            publish_command,
            admission=self.admission,
            predecessor=verify,
            verify_result=verify_result,
            publish_intent=publish_intent,
        )
        forged_publish = AdmissionHarness.manual(
            publish_command,
            request={**dict(publish_command.request), "branch_ref": "work/forged"},
        )
        with self.assertRaisesRegex(FactStoreError, "typed semantic validation"):
            self.facts.create_command(
                forged_publish,
                admission=self.admission,
                predecessor=verify,
                verify_result=verify_result,
                publish_intent=publish_intent,
            )
        rows = self._query(
            "SELECT stage FROM command WHERE run_id = %s AND kind = 'stage' ORDER BY stage",
            (self.admission.run_id,),
        )
        self.assertEqual(
            [row[0] for row in rows],
            ["capture", "discover", "freeze", "generate", "publish", "verify"],
        )
        self.assertEqual(
            self.facts.load_command(verify_command.command_id),
            verify_command,
        )
        self.assertEqual(
            self.facts.load_command(publish_command.command_id),
            publish_command,
        )

    def test_technical_dispatch_uses_its_own_claim_and_creates_no_stage_decision(self) -> None:
        result = self.dispatch_service.dispatch(self.dispatch_command.command_id)
        self.assertEqual(result.state, EffectState.SATISFIED)
        self.assertEqual(self.airflow_stub.trigger_count, 1)
        self.assertGreaterEqual(self.airflow_stub.observe_count, 1)
        replay = self.dispatch_service.dispatch(self.dispatch_command.command_id)
        self.assertEqual(replay.state, EffectState.SATISFIED)
        self.assertEqual(self.airflow_stub.trigger_count, 1)
        rows = self._query(
            "SELECT "
            "(SELECT count(*) FROM stage_decision WHERE command_id = %s),"
            "(SELECT count(*) FROM execution_claim WHERE command_id = %s),"
            "(SELECT count(*) FROM execution_claim WHERE command_id = %s)",
            (
                self.dispatch_command.command_id,
                self.dispatch_command.command_id,
                self.command.command_id,
            ),
        )
        self.assertEqual(rows[0], (0, 1, 0))
        claim_rows = self._query(
            "SELECT owner_sha256, executor_sha256, closed_at IS NOT NULL "
            "FROM execution_claim WHERE command_id = %s",
            (self.dispatch_command.command_id,),
        )
        self.assertNotEqual(claim_rows[0][0], claim_rows[0][1])
        self.assertTrue(claim_rows[0][2])

    def test_transport_identity_is_explicit_without_requiring_a_different_digest(self) -> None:
        admission, freeze_command = self._new_admission("same-transport")
        conf = DispatchConf(
            schema_version="description-pipeline.airflow-dispatch-conf/v1",
            run_id=admission.run_id,
            freeze_command_id=freeze_command.command_id,
        )
        artifact = self.artifacts.seal_dispatch_conf(
            conf,
            producer_run_id=admission.run_id,
            producer_tool_sha256=self.executor,
        )
        dispatch = DispatchCommand.create(
            command_id=f"dispatch-{admission.run_id}",
            run_id=admission.run_id,
            freeze_command_id=freeze_command.command_id,
            idempotency_key=f"{admission.run_id}/dispatch",
            dag_id="solidworks_to_urdf",
            dag_run_id=f"portal-{admission.run_id}",
            conf_artifact=artifact,
            expected_executor_sha256=self.executor,
        )
        stored_freeze, stored_dispatch, _intent = self.facts.admit_run(
            admission,
            freeze_command,
            dispatch,
            artifact,
        )
        self.assertEqual(stored_freeze.expected_executor_sha256, stored_dispatch.expected_executor_sha256)
        self.assertNotEqual(stored_freeze.command_id, stored_dispatch.command_id)

    def test_lost_dispatch_response_observes_the_same_dag_run_without_resending(self) -> None:
        with self.airflow_stub.lock:
            self.airflow_stub.lost_response = True
        first = self.dispatch_service.dispatch(self.dispatch_command.command_id)
        self.assertEqual(first.state, EffectState.SATISFIED)
        self.assertEqual(self.airflow_stub.trigger_count, 1)
        with self.airflow_stub.lock:
            self.airflow_stub.lost_response = False
        second = self.dispatch_service.dispatch(self.dispatch_command.command_id)
        self.assertEqual(second.state, EffectState.SATISFIED)
        self.assertEqual(self.airflow_stub.trigger_count, 1)

    def test_dispatch_unknown_does_not_trigger_on_replay(self) -> None:
        with self.airflow_stub.lock:
            self.airflow_stub.lost_response = True
        original_observe = self.dispatch_service.adapter.observe
        self.dispatch_service.adapter.observe = lambda *_args, **_kwargs: DispatchObservation(
            "unobservable",
            {"error": "stub unavailable"},
        )
        first = self.dispatch_service.dispatch(self.dispatch_command.command_id)
        self.assertEqual(first.state, EffectState.UNKNOWN)
        self.assertEqual(self.airflow_stub.trigger_count, 1)
        self.dispatch_service.adapter.observe = original_observe
        with self.airflow_stub.lock:
            self.airflow_stub.present.clear()
            self.airflow_stub.lost_response = False
        second = self.dispatch_service.dispatch(self.dispatch_command.command_id)
        self.assertEqual(second.state, EffectState.UNKNOWN)
        self.assertEqual(self.airflow_stub.trigger_count, 1)

    def test_real_artifact_effect_claim_and_decision_chain(self) -> None:
        handler = _EffectHandler()
        result = self._execute(handler)

        self.assertIsInstance(result, StageResult)
        self.assertEqual(handler.external_calls, 1)
        self.assertEqual(handler.validations, 1)
        decision = self.facts.decision_for(self.command)
        self.assertIsNotNone(decision)
        self.assertNotEqual(
            result.output_artifact.artifact_id,
            decision.result_record_artifact.artifact_id,
        )
        self.assertEqual(result.output_artifact.producer_tool_sha256, self.executor)
        self.assertEqual(decision.actual_executor_sha256, self.executor)
        self.assertEqual(self.artifacts.read_result(decision.result_record_artifact), result)
        self.artifacts.verify(result.output_artifact)
        effect_ids = self.facts.effect_ids_for_command(self.command.command_id)
        self.assertEqual(len(effect_ids), 1)
        self.assertEqual(self.facts.effect_state(effect_ids[0]), EffectState.SATISFIED)
        settlement = self.facts.effect_settlement(effect_ids[0])
        self.assertEqual(settlement.state, EffectState.SATISFIED)
        self.assertIsNotNone(settlement.payload_artifact)
        self.artifacts.verify(settlement.payload_artifact)
        rows = self._query(
            "SELECT c.owner_sha256, c.executor_sha256, p.output_artifact_id, d.result_record_artifact_id "
            "FROM execution_claim c "
            "JOIN stage_candidate p ON p.command_id = c.command_id "
            "JOIN stage_decision d ON d.command_id = c.command_id "
            "WHERE c.command_id = %s",
            (self.command.command_id,),
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], self.owner)
        self.assertEqual(rows[0][1], self.executor)
        self.assertNotEqual(rows[0][2], rows[0][3])
        command_root = self.services.work_root / self.command.command_id
        self.assertEqual(list(command_root.iterdir()), [])

    def test_unknown_effect_refuses_dispatch_before_external_callback(self) -> None:
        claim = self.services.acquire_claim(self.command)
        workspace = self.services.materialize(self.command, claim, candidate=None)
        effects = self.services.effect_port(self.command, claim, workspace)
        self.addCleanup(self.services.discard_workspace, workspace)
        outbound = workspace.root / "outbound"
        outbound.mkdir()
        (outbound / "request.json").write_text("{}\n", encoding="utf-8")
        intent = effects.prepare_effect(
            effect_key="native",
            effect_kind="native_operation",
            target={"endpoint": "local-test"},
            expected_state={"state": "complete"},
            outbound_root=outbound,
        )
        replay = effects.prepare_effect(
            effect_key="native",
            effect_kind="native_operation",
            target={"endpoint": "local-test"},
            expected_state={"state": "complete"},
            outbound_root=outbound,
        )
        self.assertEqual(replay.effect_id, intent.effect_id)
        changed = workspace.root / "changed-outbound"
        changed.mkdir()
        (changed / "request.json").write_text('{"changed": true}\n', encoding="utf-8")
        with self.assertRaises(FactConflict):
            effects.prepare_effect(
                effect_key="native",
                effect_kind="native_operation",
                target={"endpoint": "local-test"},
                expected_state={"state": "different"},
                outbound_root=changed,
            )
        effects.authorize_dispatch(intent.effect_id, source="runtime-test")
        effects.record_timeout(intent.effect_id, source="runtime-test")
        stale = ExecutionClaim(
            claim_id=claim.claim_id,
            command_id=claim.command_id,
            fence=claim.fence + 1,
            owner_sha256=claim.owner_sha256,
            executor_sha256=claim.executor_sha256,
            lane_key=claim.lane_key,
            expires_at=claim.expires_at,
        )
        with self.assertRaises(FactConflict):
            self.facts.authorize_effect_dispatch(
                effect_id=intent.effect_id,
                claim=stale,
                source="stale",
                payload_digest=digest({"stale": 1}),
                observed_at=self.now,
                fact_id="fact-stale",
            )
        self.assertIsNone(effects.effect_settlement_for_key("absent"))
        query_count = 1
        settlement = effects.effect_settlement_for_key("native")
        self.assertEqual(settlement.state, EffectState.UNKNOWN)
        callback_count = 0
        with self.assertRaises(FactConflict):
            effects.authorize_dispatch(intent.effect_id, source="runtime-test")
            callback_count += 1
        self.assertEqual(callback_count, 0)
        self.assertEqual(query_count, 1)
        self.assertEqual(effects.effect_state(intent.effect_id), EffectState.UNKNOWN)
        with self.assertRaises(ProtocolError):
            self.services.commit_failure(
                self.command,
                claim,
                StageFailure("native_unknown", "The native result remains unknown"),
                workspace_root=workspace.root,
            )
        decision_count = self._query(
            "SELECT count(*) FROM stage_decision WHERE command_id = %s",
            (self.command.command_id,),
        )[0][0]
        self.assertEqual(decision_count, 0)

        expired_outbound = workspace.root / "expired-outbound"
        expired_outbound.mkdir()
        (expired_outbound / "request.json").write_text("{}\n", encoding="utf-8")
        expired_effect = effects.prepare_effect(
            effect_key="expired",
            effect_kind="native_operation",
            target={"endpoint": "local-test"},
            expected_state={"state": "complete"},
            outbound_root=expired_outbound,
        )
        with self.assertRaises(FactConflict):
            self.facts.authorize_effect_dispatch(
                effect_id=expired_effect.effect_id,
                claim=claim,
                source="expired",
                payload_digest=digest({"expired": 1}),
                observed_at=self.now + timedelta(minutes=6),
                fact_id="fact-expired",
            )
        self.assertEqual(effects.effect_state(expired_effect.effect_id), EffectState.PREPARED)

        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "UPDATE execution_claim SET closed_at = %s, closure_code = 'operator_closed' WHERE claim_id = %s",
                (self.now, claim.claim_id),
            )
        with self.assertRaises(FactConflict):
            self.facts.acquire_claim(
                self.command.command_id,
                claim_id="claim-after-close",
                owner_sha256=digest({"process": "replacement"}),
                executor_sha256=self.executor,
                now=self.now,
                expires_at=self.now + timedelta(minutes=5),
            )

        outside = Path(self.directory.name) / "outside"
        outside.mkdir()
        (outside / "payload.json").write_text("{}\n", encoding="utf-8")
        with self.assertRaises(ProtocolError):
            effects.prepare_effect(
                effect_key="outside",
                effect_kind="native_operation",
                target={"endpoint": "local-test"},
                expected_state={"state": "complete"},
                outbound_root=outside,
            )

    def test_satisfied_effect_returns_the_same_sealed_observation_without_resend(self) -> None:
        claim = self.services.acquire_claim(self.command)
        workspace = self.services.materialize(self.command, claim, candidate=None)
        self.addCleanup(self.services.discard_workspace, workspace)
        effects = self.services.effect_port(self.command, claim, workspace)
        outbound = workspace.root / "outbound"
        outbound.mkdir()
        (outbound / "request.json").write_text("{}\n", encoding="utf-8")
        intent = effects.prepare_effect(
            effect_key="discover",
            effect_kind="native_operation",
            target={"endpoint": "local-test", "operation": "discover"},
            expected_state={"schema": "native-discover/v1", "members": ["observation.json"]},
            outbound_root=outbound,
        )
        effects.authorize_dispatch(intent.effect_id, source="runtime-test")
        call_count = 1
        observation = workspace.root / "observation"
        observation.mkdir()
        (observation / "observation.json").write_text('{"objects": []}\n', encoding="utf-8")
        observation_fact = effects.record_observation(
            intent.effect_id,
            source="runtime-test",
            state_class="known",
            observation_root=observation,
        )

        restarted_effects = self.services.effect_port(self.command, claim, workspace)
        settlement = restarted_effects.effect_settlement_for_key("discover")
        self.assertEqual(settlement.state, EffectState.SATISFIED)
        self.assertIsNotNone(settlement.payload_artifact)
        self.assertEqual(settlement.payload_artifact.artifact_id, observation_fact.payload_artifact_id)
        self.artifacts.verify(settlement.payload_artifact)
        repeated = restarted_effects.effect_settlement_for_key("discover")
        self.assertEqual(repeated, settlement)
        with self.assertRaises(FactConflict):
            restarted_effects.authorize_dispatch(intent.effect_id, source="must-not-send")
            call_count += 1
        self.assertEqual(call_count, 1)

    def test_concurrent_claims_have_one_owner_and_same_tool_is_not_owner_identity(self) -> None:
        barrier = threading.Barrier(2)
        same_owner = digest({"process": "same-entry"})

        def acquire():
            barrier.wait(timeout=5)
            try:
                return self.facts.acquire_claim(
                    self.command.command_id,
                    claim_id="claim-" + str(uuid.uuid4()),
                    owner_sha256=same_owner,
                    executor_sha256=self.executor,
                    now=self.now,
                    expires_at=self.now + timedelta(minutes=5),
                )
            except FactConflict as error:
                return error

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(lambda _item: acquire(), range(2)))
        winners = [item for item in outcomes if not isinstance(item, FactConflict)]
        conflicts = [item for item in outcomes if isinstance(item, FactConflict)]
        self.assertEqual(len(winners), 1)
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(winners[0].owner_sha256, same_owner)
        self.assertNotEqual(winners[0].owner_sha256, winners[0].executor_sha256)
        with self.assertRaises(FactConflict):
            self.facts.acquire_claim(
                self.command.command_id,
                claim_id="claim-" + str(uuid.uuid4()),
                owner_sha256=digest({"process": "other-entry"}),
                executor_sha256=self.executor,
                now=self.now,
                expires_at=self.now + timedelta(minutes=5),
            )
        count = self._query(
            "SELECT count(*) FROM execution_claim WHERE command_id = %s AND closed_at IS NULL",
            (self.command.command_id,),
        )[0][0]
        self.assertEqual(count, 1)

    def test_decision_requires_the_complete_effect_ledger(self) -> None:
        claim = self.services.acquire_claim(self.command)
        workspace = self.services.materialize(self.command, claim, candidate=None)
        self.addCleanup(self.services.discard_workspace, workspace)
        effects = self.services.effect_port(self.command, claim, workspace)
        outbound = workspace.root / "outbound"
        outbound.mkdir()
        (outbound / "request.json").write_text("{}\n", encoding="utf-8")
        effects.prepare_effect(
            effect_key="native",
            effect_kind="native_operation",
            target={"endpoint": "local-test"},
            expected_state={"state": "complete"},
            outbound_root=outbound,
        )

        output_root = workspace.root / "manual-output"
        output_root.mkdir()
        (output_root / "model.txt").write_text("{}\n", encoding="utf-8")
        output = self.artifacts.seal_tree(
            output_root,
            kind="stage_output:freeze",
            producer_run_id=self.command.run_id,
            producer_stage=self.command.stage,
            producer_tool_sha256=self.executor,
        )
        result = StageResult(
            schema_version=STAGE_RESULT_SCHEMA,
            command_id=self.command.command_id,
            run_id=self.command.run_id,
            stage=self.command.stage,
            request_digest=self.command.request_digest,
            outcome="completed",
            output_artifact=output,
            actual_executor_sha256=self.executor,
            checks_sha256=digest({"checks": 1}),
            resources_settled=True,
            required_effect_ids=(),
        )
        record = self.artifacts.seal_stage_result(result)
        candidate = self.facts.register_candidate(
            candidate_id="candidate-" + result.digest,
            claim=claim,
            output_artifact=output,
            result_record_artifact=record,
            result=result,
            created_at=self.now,
        )
        with self.assertRaises(FactConflict):
            self.facts.commit_decision(
                decision_id="decision-forged",
                command=self.command,
                claim=claim,
                candidate=candidate,
                decided_at=self.now,
            )
        count = self._query(
            "SELECT count(*) FROM stage_decision WHERE command_id = %s",
            (self.command.command_id,),
        )[0][0]
        self.assertEqual(count, 0)

    def test_minimal_failure_envelope_does_not_require_diagnostic_seal(self) -> None:
        result = self._execute(_FailureHandler())
        self.assertNotIsInstance(result, StageResult)
        decision = self.facts.decision_for(self.command)
        self.assertIsNotNone(decision)
        self.assertIsNone(decision.result_record_artifact)
        self.assertIsNotNone(decision.failure_envelope)
        self.assertEqual(decision.failure_envelope.reason_code, "domain_input_invalid")
        self.assertEqual(decision.failure_envelope.unavailable_reason, "diagnostic sealing deliberately unavailable")
        rows = self._query(
            "SELECT result_record_artifact_id, failure_envelope, failure_digest "
            "FROM stage_decision WHERE command_id = %s",
            (self.command.command_id,),
        )
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0][0])
        self.assertIsNotNone(rows[0][1])
        self.assertIsNotNone(rows[0][2])
        self.assertTrue(result.retained_locations)
        self.assertTrue(Path(result.retained_locations[0]).is_dir())

    def test_unsealable_failed_execution_falls_back_to_failure_envelope(self) -> None:
        result = self._execute(_UnsealedFailureHandler())
        self.assertNotIsInstance(result, StageResult)
        self.assertEqual(result.reason_code, "domain_failed")
        self.assertEqual(result.error_message, "The original domain failure")
        self.assertEqual(dict(result.error_detail), {"field": "assembly", "value": "missing"})
        self.assertTrue(result.unavailable_reason.startswith("diagnostic_or_result_seal_failed:"))
        retained_workspace = Path(result.retained_locations[0])
        retained_diagnostics = Path(result.retained_locations[1])
        self.assertTrue((retained_workspace / "missing-diagnostics" / "diagnostic.txt").is_file())
        self.assertTrue((retained_diagnostics / "diagnostic.txt").is_file())
        decision = self.facts.decision_for(self.command)
        self.assertIsNone(decision.result_record_artifact)
        self.assertEqual(decision.failure_envelope, result)

    def test_unhandled_exception_retains_workspace_and_raw_failure_marker(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "unexpected native state"):
            self._execute(_UnexpectedFailureHandler())
        command_root = self.services.work_root / self.command.command_id
        workspaces = [item for item in command_root.iterdir() if item.is_dir()]
        self.assertEqual(len(workspaces), 1)
        workspace = workspaces[0]
        self.assertTrue((workspace / "native-attempt.txt").is_file())
        self.assertEqual((workspace / "failure.json").read_text(encoding="utf-8"), "original domain failure\n")
        marker = workspace / "engine-unhandled-failure.json"
        self.assertTrue(marker.is_file())
        self.assertEqual(
            __import__("json").loads(marker.read_text(encoding="utf-8"))["error_type"],
            "RuntimeError",
        )

    def test_lost_decision_commit_response_retains_sealed_workspace(self) -> None:
        commit = self.services.commit_decision
        calls = 0

        def commit_then_lose_response(*args, **kwargs):
            nonlocal calls
            calls += 1
            decision = commit(*args, **kwargs)
            self.assertIsNotNone(decision)
            raise TimeoutError("decision commit response lost")

        with (
            mock.patch.object(self.services, "commit_decision", side_effect=commit_then_lose_response),
            self.assertRaisesRegex(TimeoutError, "response lost"),
        ):
            self._execute(_EffectHandler())
        self.assertEqual(calls, 1)
        self.assertIsNotNone(self.facts.decision_for(self.command))
        command_root = self.services.work_root / self.command.command_id
        workspaces = [item for item in command_root.iterdir() if item.is_dir()]
        self.assertEqual(len(workspaces), 1)
        self.assertIn(
            "decision commit response lost",
            (workspaces[0] / "engine-unhandled-failure.json").read_text(encoding="utf-8"),
        )

    def test_expected_executor_drift_stops_before_handler_execution(self) -> None:
        wrong_tool = digest({"tool": "other-release"})
        wrong_services = CanonicalStageServices(
            facts=self.facts,
            artifacts=self.artifacts,
            work_root=Path(self.directory.name) / "wrong-work",
            claim_policy=ClaimPolicy(
                owner_sha256=digest({"process": "wrong-entry"}),
                executor_sha256=wrong_tool,
                lease=timedelta(minutes=5),
            ),
            clock=lambda: self.now,
        )
        handler = _EffectHandler()
        with self.assertRaises(ProtocolError):
            execute_stage(
                command_id=self.command.command_id,
                handler=handler,
                services=wrong_services,
            )
        self.assertEqual(handler.external_calls, 0)


if __name__ == "__main__":
    unittest.main()
