"""PostgreSQL coverage for same-command Airflow retry and admitted-run visibility."""
from __future__ import annotations

import io
import json
import tempfile
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError

try:
    import pgserver
    import psycopg2
except ImportError:
    pgserver = psycopg2 = None

from description_pipeline.io import digest
from description_pipeline.orchestration.airflow_client import native_run_id
from description_pipeline.orchestration.portal import (
    AirflowApiError, AirflowAuthError, AirflowDispatchAdapter, DEFAULT_SESSION_COOKIE, PortalApp, PortalConfig,
)
from description_pipeline.pipeline.application import StageApplication
from description_pipeline.pipeline.effects import EffectIntent, EffectState
from description_pipeline.pipeline.services import ClaimPolicy
from description_pipeline.platform.facts import FactConflict, FactStore, FactStoreError, airflow_retry_claim_id
from description_pipeline.platform.queries import read_run_projection
from tests.v1.test_pipeline_admission import AdmissionHarness

ROOT = Path(__file__).resolve().parents[2]


class _Airflow:
    """Transport double preserving Airflow's unique ID and HTTP error semantics."""

    def __init__(self):
        self.runs = {}
        self.sends = 0
        self.drop = True
        self.race = False
        self.unobservable = False
        self.mismatch = False
        self.role = "USER"
        self.principal = "app:tenant:owner"
        self.lock = threading.Lock()

    def profile(self, token):
        if getattr(self, "revoked", False):
            raise AirflowAuthError("The session was revoked")
        app, tenant, user = self.principal.split(":")
        return {"principal": self.principal, "app_id": app, "tenant_key": tenant,
                "open_id": user, "name": "Operator", "role": self.role}

    def trigger_dag_run(self, token, dag_id, dag_run_id, conf):
        with self.lock:
            self.sends += 1
            if self.drop:
                self.drop = False
                raise AirflowApiError("The first response was lost before creation")
            if self.race:
                self.runs[dag_run_id] = dict(conf)
                self.race = False
            if dag_run_id in self.runs:
                raise AirflowApiError("Airflow returned HTTP 409") from HTTPError(
                    "http://loopback/run", 409, "Conflict", {}, None,
                )
            self.runs[dag_run_id] = dict(conf)
            return {"dag_id": dag_id, "dag_run_id": dag_run_id}

    def dag_run(self, token, dag_id, dag_run_id):
        if self.unobservable:
            raise AirflowApiError("Airflow is unavailable")
        if dag_run_id not in self.runs:
            raise AirflowApiError("Airflow returned HTTP 404") from HTTPError(
                "http://loopback/run", 404, "Missing", {}, None,
            )
        conf = {"different": True} if self.mismatch else self.runs[dag_run_id]
        return {"dag_id": dag_id, "dag_run_id": dag_run_id, "conf": conf, "state": "queued"}

    def task_instances(self, *args):
        if getattr(self, "tasks_missing", False):
            raise AirflowApiError("Airflow returned task HTTP 404") from HTTPError(
                "http://loopback/tasks", 404, "Missing", {}, None,
            )
        return []


@unittest.skipUnless(pgserver is not None and psycopg2 is not None, "PostgreSQL test dependencies unavailable")
class AirflowDispatchRetryPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pg_root = tempfile.TemporaryDirectory(prefix="dispatch-retry-postgres-")
        cls.server = pgserver.get_server(Path(cls.pg_root.name) / "pgdata", cleanup_mode="delete")

    @classmethod
    def tearDownClass(cls):
        cls.server.cleanup()
        cls.pg_root.cleanup()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="dispatch-retry-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.schema = "dispatch_retry_" + uuid.uuid4().hex
        sql = (ROOT / "deploy/platform/schema.sql").read_text().replace(':"platform_schema"', '"'+self.schema+'"')
        with psycopg2.connect(self.server.get_uri()) as connection, connection.cursor() as cursor:
            cursor.execute('CREATE SCHEMA "'+self.schema+'"')
            cursor.execute(sql)
            cursor.execute('UPDATE "'+self.schema+'".admission SET state=\'open\' WHERE id=1')
        self.harness = AdmissionHarness(self.root)
        self.facts = FactStore(self.connect, self.harness.store)
        self.now = datetime(2026, 10, 29, tzinfo=UTC)
        self.request_id = "portal-retry-test"
        self.spec = replace(self.harness.spec, run_id=native_run_id(self.request_id),
                            request_id=self.request_id, principal="app:tenant:owner")
        self.policy = ClaimPolicy(owner_sha256=digest({"owner": "test"}),
                                  executor_sha256=self.spec.dispatch_executor_sha256,
                                  lease=timedelta(seconds=5))
        self.application = StageApplication(
            services=SimpleNamespace(facts=self.facts, artifacts=self.harness.store,
                                     claim_policy=self.policy, work_root=self.root / "work"),
            facts=self.facts, artifacts=self.harness.store, handlers={}, builders={}, clock=lambda: self.now,
        )
        self.outcome = self.application.admit(self.spec)
        self.command = self.outcome.dispatch_command
        self.airflow = _Airflow()
        self.adapter = AirflowDispatchAdapter(self.airflow, "token")

    def connect(self):
        return psycopg2.connect(self.server.get_uri(), options="-c search_path="+self.schema+",public")

    def projection(self, run_id):
        if getattr(self, "fail_reads", False):
            raise AssertionError("Revoked sessions must not read canonical facts")
        with self.connect() as connection:
            return read_run_projection(connection, run_id, artifacts=self.harness.store)

    def unknown(self):
        result = self.application.dispatch(self.command.command_id, self.adapter)
        self.assertEqual(result.state, EffectState.UNKNOWN)
        self.assertEqual(self.airflow.sends, 1)
        return result

    def elapsed(self):
        self.now += timedelta(seconds=6)

    def acquire(self, fact_id):
        return self.facts.acquire_claim(
            self.command.command_id, claim_id=airflow_retry_claim_id(fact_id),
            owner_sha256=self.policy.owner_sha256, executor_sha256=self.command.expected_executor_sha256,
            now=self.now, expires_at=self.now+timedelta(seconds=5), airflow_absent_fact_id=fact_id,
        )

    def authorize(self, claim, fact_id):
        return self.facts.authorize_effect_dispatch(
            effect_id=self.facts.effect_settlement_for_key(self.command.command_id, self.command.effect_key).effect_id,
            claim=claim, source="airflow-dispatch", payload_digest=self.command.request_digest,
            observed_at=self.now, fact_id="fact-"+uuid.uuid4().hex,
            expected_command_id=self.command.command_id, airflow_absent_fact_id=fact_id,
        )

    def test_expired_unknown_retry_sends_the_original_command_and_conf(self):
        original = self.unknown()
        self.elapsed()
        result = self.application.dispatch(self.command.command_id, self.adapter, retry_absent=True)
        self.assertEqual(result.state, EffectState.SATISFIED)
        self.assertEqual(result.command_id, original.command_id)
        self.assertEqual(result.effect_id, original.effect_id)
        self.assertEqual(self.airflow.runs, {
            self.request_id: self.harness.store.read_dispatch_conf(self.command.conf_artifact).to_record()
        })
        self.assertEqual(self.airflow.sends, 2)

    def test_live_claim_cannot_be_stolen(self):
        self.unknown()
        with self.assertRaises(FactConflict):
            self.application.dispatch(self.command.command_id, self.adapter, retry_absent=True)
        self.assertEqual(self.airflow.sends, 1)

    def test_unobservable_and_mismatched_conf_never_resend(self):
        self.unknown()
        self.elapsed()
        self.airflow.unobservable = True
        result = self.application.dispatch(self.command.command_id, self.adapter, retry_absent=True)
        self.assertEqual(result.state, EffectState.UNKNOWN)
        self.airflow.unobservable = False
        self.airflow.runs[self.request_id] = {"different": True}
        result = self.application.dispatch(self.command.command_id, self.adapter, retry_absent=True)
        self.assertEqual(result.state, EffectState.UNKNOWN)
        self.assertEqual(self.airflow.sends, 1)

    def test_late_original_creation_and_409_settle_one_dag(self):
        self.unknown()
        self.elapsed()
        self.airflow.race = True
        result = self.application.dispatch(self.command.command_id, self.adapter, retry_absent=True)
        self.assertEqual(result.state, EffectState.SATISFIED)
        self.assertEqual(len(self.airflow.runs), 1)
        self.assertEqual(self.airflow.sends, 2)

    def test_known_and_conflict_cannot_authorize_resend(self):
        self.unknown()
        self.elapsed()
        conf = self.harness.store.read_dispatch_conf(self.command.conf_artifact)
        self.airflow.runs[self.request_id] = conf.to_record()
        result = self.application.dispatch(self.command.command_id, self.adapter, retry_absent=True)
        self.assertEqual(result.state, EffectState.SATISFIED)
        self.assertEqual(self.airflow.sends, 1)
        self.facts.append_effect_fact(effect_id=result.effect_id, claim=None, fact_type="conflict",
                                      source="test", state_class=None, payload_artifact=None,
                                      payload_digest=digest({"conflict": True}), observed_at=self.now,
                                      fact_id="fact-"+uuid.uuid4().hex,
                                      expected_command_id=self.command.command_id)
        self.assertEqual(self.application.dispatch(self.command.command_id, self.adapter, retry_absent=True).state,
                         EffectState.CONFLICT)
        self.assertEqual(self.airflow.sends, 1)

    def test_one_absent_fact_allows_only_one_concurrent_claim(self):
        result = self.unknown()
        self.elapsed()
        def acquire_one(_):
            try:
                return self.acquire(result.observation_fact_id)
            except FactConflict:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            claims = list(pool.map(acquire_one, range(2)))
        self.assertEqual(sum(claim is not None for claim in claims), 1)
        self.authorize(next(claim for claim in claims if claim is not None), result.observation_fact_id)
        with self.assertRaises(FactConflict):
            self.authorize(next(claim for claim in claims if claim is not None), result.observation_fact_id)

    def test_new_fact_invalidates_old_claim_evidence_and_cannot_rebind_claim(self):
        result = self.unknown()
        self.elapsed()
        claim = self.acquire(result.observation_fact_id)
        newer = self.application.dispatch(self.command.command_id, self.adapter)
        with self.assertRaises(FactConflict):
            self.authorize(claim, result.observation_fact_id)
        with self.assertRaises(FactConflict):
            self.authorize(claim, newer.observation_fact_id)
        self.assertEqual(self.airflow.sends, 1)

    def test_stage_command_cannot_use_airflow_retry_exception(self):
        result = self.unknown()
        self.elapsed()
        freeze = self.outcome.entry_command
        with self.assertRaises(FactStoreError):
            self.facts.acquire_claim(freeze.command_id, claim_id=airflow_retry_claim_id(result.observation_fact_id),
                                     owner_sha256=self.policy.owner_sha256,
                                     executor_sha256=freeze.expected_executor_sha256,
                                     now=self.now, expires_at=self.now+timedelta(seconds=5),
                                     airflow_absent_fact_id=result.observation_fact_id)

    def test_non_airflow_effect_kind_cannot_use_the_retry_exception(self):
        result = self.unknown()
        freeze = self.outcome.entry_command
        claim = self.facts.acquire_claim(
            freeze.command_id, claim_id="stage-claim", owner_sha256=self.policy.owner_sha256,
            executor_sha256=freeze.expected_executor_sha256,
            now=self.now, expires_at=self.now+timedelta(seconds=5),
        )
        for kind in ("native_operation", "git_ref_update", "pr_upsert"):
            with self.subTest(kind=kind):
                target = {"fixture": kind}
                expected = {"fixture": kind}
                if kind == "git_ref_update":
                    target = {"repository": "example/test", "branch_ref": "work/test"}
                    expected = {"schema_version": "solidworks-to-urdf.git-ref-update/v1",
                                **target, "base_ref": "main", "expected_base_oid": "1"*40,
                                "expected_head_oid": None, "candidate_commit": "2"*40,
                                "candidate_tree": "3"*40, "subject_sha256": "a"*64}
                elif kind == "pr_upsert":
                    target = {"repository": "example/test", "base_ref": "main", "head_ref": "work/test"}
                    expected = {"schema_version": "solidworks-to-urdf.pr-upsert/v1",
                                **target, "candidate_commit": "2"*40, "subject_sha256": "a"*64,
                                "title_digest": "b"*64, "body_digest": "c"*64, "expected_pr": None}
                intent = EffectIntent(
                    effect_id="effect-"+kind, command_id=freeze.command_id,
                    effect_key=kind, effect_kind=kind, target=target,
                    expected_state=expected, expected_state_digest=digest(expected),
                    outbound_artifact_id=self.command.conf_artifact.artifact_id,
                    outbound_digest=self.command.conf_artifact.set_sha256, created_at=self.now,
                )
                self.facts.register_effect_intent(intent, claim=claim, outbound_artifact=self.command.conf_artifact)
                arguments = {
                    "effect_id": intent.effect_id, "claim": claim, "source": "fixture",
                    "payload_digest": digest({"fixture": kind}), "observed_at": self.now,
                    "fact_id": "fact-"+uuid.uuid4().hex,
                }
                self.facts.authorize_effect_dispatch(**arguments)
                arguments["fact_id"] = "fact-"+uuid.uuid4().hex
                with self.assertRaises(FactConflict):
                    self.facts.authorize_effect_dispatch(**arguments)
                with self.assertRaises(FactStoreError):
                    self.facts.authorize_effect_dispatch(**arguments,
                                                        airflow_absent_fact_id=result.observation_fact_id)
        self.assertEqual(self.airflow.sends, 1)

    def portal(self):
        app = PortalApp(PortalConfig(airflow=self.airflow, production_application=self.application,
                                    artifact_store=self.harness.store, run_control_store=self.facts,
                                    run_projection_factory=self.projection))
        session = app.sessions.create("Operator", "token", principal=self.airflow.principal)
        def request(method, path, body=None, *, csrf=True):
            data = b"" if body is None else json.dumps(body).encode()
            environ = {"REQUEST_METHOD": method, "PATH_INFO": path, "QUERY_STRING": "",
                       "CONTENT_LENGTH": str(len(data)), "CONTENT_TYPE": "application/json",
                       "wsgi.input": io.BytesIO(data),
                       "HTTP_COOKIE": DEFAULT_SESSION_COOKIE+"="+session.session_id}
            if csrf:
                environ["HTTP_X_CSRF_TOKEN"] = session.csrf_token
            status = []
            payload = b"".join(app(environ, lambda value, headers: status.append(int(value.split()[0]))))
            return status[0], json.loads(payload)
        return request

    def test_admitted_missing_dag_is_listed_and_has_retryable_detail(self):
        self.unknown()
        request = self.portal()
        status, result = request("GET", "/api/runs")
        self.assertEqual(status, 200, result)
        self.assertEqual([row["dag_run_id"] for row in result["runs"]], [self.request_id])
        status, result = request("GET", "/api/runs/"+self.request_id)
        self.assertEqual(status, 200, result)
        self.assertTrue(result["retry"]["eligible"])
        self.elapsed()
        status, result = request("POST", "/api/runs/"+self.request_id+"/retry", {})
        self.assertEqual(status, 200, result)
        self.assertEqual(result["trigger"], "satisfied")

    def test_missing_dag_retry_requires_csrf_and_owner_or_admin(self):
        self.unknown()
        self.elapsed()
        request = self.portal()
        self.assertEqual(request("POST", "/api/runs/"+self.request_id+"/retry", {}, csrf=False)[0], 403)
        self.airflow.principal = "app:tenant:other"
        request = self.portal()
        self.assertEqual(request("POST", "/api/runs/"+self.request_id+"/retry", {})[0], 403)
        self.airflow.role = "ADMIN"
        self.assertEqual(request("POST", "/api/runs/"+self.request_id+"/retry", {})[0], 200)

    def test_backend_outage_is_not_reported_as_a_missing_dag(self):
        request = self.portal()
        self.airflow.unobservable = True
        self.assertEqual(request("GET", "/api/runs")[0], 503)
        self.assertEqual(request("GET", "/api/runs/"+self.request_id)[0], 502)

    def test_revoked_session_is_checked_before_canonical_facts(self):
        request = self.portal()
        self.fail_reads = True
        self.airflow.revoked = True
        self.assertEqual(request("GET", "/api/runs/"+self.request_id)[0], 401)
        self.assertEqual(request("POST", "/api/runs/"+self.request_id+"/retry", {})[0], 401)

    def test_existing_dag_with_missing_tasks_is_an_error_not_pending_detail(self):
        self.unknown()
        self.elapsed()
        self.application.dispatch(self.command.command_id, self.adapter, retry_absent=True)
        self.airflow.tasks_missing = True
        self.assertEqual(self.portal()("GET", "/api/runs/"+self.request_id)[0], 502)
