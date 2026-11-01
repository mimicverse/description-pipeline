"""Actual PostgreSQL idempotency, execution fencing and cancellation races."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

from description_pipeline.dynamics_assessment.contracts import RESULT_SCHEMA, AssessmentConflict, AssessmentRequest
from description_pipeline.dynamics_assessment.executor import AssessmentExecutor
from description_pipeline.dynamics_assessment.runtime import (
    ProcessLimits,
    TeleopProcess,
    process_identity,
    process_state,
    resource_state,
    terminate_owned,
)
from description_pipeline.dynamics_assessment.service import AssessmentService
from description_pipeline.dynamics_assessment.store import AssessmentStore
from description_pipeline.io import canonical, digest
from tests.v1.test_dynamics_assessment_contracts import AssessmentCase, request_case
from tests.v1.test_dynamics_assessment_runtime import fork_after_proc_listing, owned_group, reap_group

ROOT = Path(__file__).resolve().parents[2]


@contextmanager
def postgres_server():
    try:
        import pgserver
        import psycopg2  # noqa: F401 - prerequisite check before starting private PostgreSQL
    except ImportError as error:
        raise unittest.SkipTest("pgserver/psycopg2 are not installed") from error
    with tempfile.TemporaryDirectory(prefix="assessment-postgres-") as temporary:
        server = pgserver.get_server(Path(temporary) / "pgdata", cleanup_mode="delete")
        try:
            yield server
        finally:
            server.cleanup()


@contextmanager
def database_case(tmp_path, postgres_server):
    import psycopg2

    schema = "assessment_test_" + uuid.uuid4().hex
    sql = (ROOT / "deploy/platform/schema.sql").read_text()
    if ".assessment_request" not in sql:
        addition = os.environ.get("ASSESSMENT_TEST_SCHEMA_APPEND")
        if not addition:
            raise unittest.SkipTest("Assessment public DDL is awaiting root integration")
        sql += "\n" + Path(addition).read_text()
    sql = sql.replace(':"platform_schema"', '"' + schema + '"')
    with psycopg2.connect(postgres_server.get_uri()) as connection, connection.cursor() as cursor:
        cursor.execute('CREATE SCHEMA "' + schema + '"')
        cursor.execute(sql)
        cursor.execute('UPDATE "' + schema + "\".admission SET state='open' WHERE id=1")
        cursor.execute(
            'INSERT INTO "'
            + schema
            + '".run_admission (run_id,request_id,request,request_digest) VALUES(%s,%s,%s::jsonb,%s)',
            ("source-run", "source-dag-run", "{}", digest({})),
        )

    def connect():
        return psycopg2.connect(postgres_server.get_uri(), options="-c search_path=" + schema + ",public")

    artifacts, request = request_case(tmp_path)
    store = AssessmentStore(connect, artifacts)
    try:
        yield (store, request, tmp_path)
    finally:
        with psycopg2.connect(postgres_server.get_uri()) as connection, connection.cursor() as cursor:
            cursor.execute('DROP SCHEMA "' + schema + '" CASCADE')


def register(store, request, root, key="request-key"):
    folder = root / ("request-" + request.sha256)
    folder.mkdir(exist_ok=True)
    (folder / "request.json").write_bytes(canonical(request.to_record()) + b"\n")
    request_id = digest({"principal": request.record["principal"], "key": key})
    ref = store.artifacts.seal_tree(
        folder,
        kind="dynamics_request",
        producer_run_id=request_id,
        producer_stage=None,
        producer_tool_sha256=request.record["executor_sha256"],
    )
    return store.register(key, request, ref)


def result(store, request, root, attempt, state):
    folder = root / ("result-" + state)
    folder.mkdir(exist_ok=True)
    record = {
        "schema": RESULT_SCHEMA,
        "request_id": attempt["request_id"],
        "attempt_id": attempt["attempt_id"],
        "state": state,
        "resources_settled": True,
        "request_sha256": request.sha256,
        "runtime_sha256": request.runtime.sha256,
        "source_sha256": request.source.sha256,
    }
    (folder / "assessment_result.json").write_text(json.dumps(record))
    return store.artifacts.seal_tree(
        folder,
        kind="dynamics_result",
        producer_run_id=attempt["attempt_id"],
        producer_stage=None,
        producer_tool_sha256=request.record["executor_sha256"],
    )


def owned_attempt(store, request, root):
    request_id = register(store, request, root)
    attempt = store.new_attempt(request_id)
    assert store.begin_dispatch(attempt["attempt_id"])
    owner = digest({"owner": 1})
    store.claim(attempt["attempt_id"], owner, controller={"fixture": "controller"})
    return (attempt, owner)


class PostgresCase(AssessmentCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.server = cls.enterClassContext(postgres_server())

    def setUp(self):
        super().setUp()
        self.database = self.enterContext(database_case(self.root, self.server))


class PostgresDatabaseTests(PostgresCase):
    def _dead_controller(self):
        nonce = digest({"controller": self.id()})
        process = subprocess.Popen(
            [sys.executable, "-I", "-c", "import sys; sys.stdin.readline()", nonce],
            stdin=subprocess.PIPE,
            start_new_session=True,
        )
        try:
            identity = process_identity(process.pid, nonce)
            process.stdin.close()
            process.wait(timeout=5)
            self.assertEqual(process_state(identity), "dead")
            return identity
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)

    def _resource_executor(self):
        store, request, root = self.database
        work = root / "resource-work"
        work.mkdir()
        service = AssessmentService(
            store,
            enabled=True,
            runtime=request.runtime,
            executor_sha256=request.record["executor_sha256"],
            work_root=work,
        )
        return AssessmentExecutor(
            service, TeleopProcess(Path(sys.executable), request.runtime, ProcessLimits(10, 1024**3, 10, 1024**2))
        )

    def test_real_orphan_scan_fork_race_cannot_commit_or_release_process(self):
        store, request, root = self.database
        request_id = register(store, request, root)
        attempt = store.new_attempt(request_id)
        store.begin_dispatch(attempt["attempt_id"])
        owner = digest({"owner": "original-orphan"})
        controller = self._dead_controller()
        executor = self._resource_executor()
        with owned_group(root, late=True) as (_leader, identity, trigger, record):
            store.claim(attempt["attempt_id"], owner, controller=controller)
            store.heartbeat(attempt["attempt_id"], owner, phase="running", process=identity)
            store.unknown(attempt["attempt_id"], {"phase": "controller_dead"}, owner=owner)
            before = store.attempt(attempt["attempt_id"])
            with fork_after_proc_listing(trigger):
                response = executor.reconcile(attempt["attempt_id"])
            self.assertEqual(response["state"], "unknown")
            after = store.attempt(attempt["attempt_id"])
            for key in ("owner_token", "owner_process", "process", "lease_until", "detail"):
                self.assertEqual(after[key], before[key])
            self.assertFalse(after["resources_settled"])
            self.assertIsNone(store.result_artifact(attempt["attempt_id"]))
            self.assertNotEqual(resource_state(identity), "dead")
            self.assertIn(process_state(record["identity"]), {"alive", "dead"})

    def test_reconciliation_claim_keeps_identity_visible_to_scan_fork_seal_and_commit_gates(self):
        store, request, root = self.database
        request_id = register(store, request, root)
        attempt = store.new_attempt(request_id)
        store.begin_dispatch(attempt["attempt_id"])
        owner, next_owner = digest({"owner": "original"}), digest({"owner": "sealer"})
        controller = self._dead_controller()
        executor = self._resource_executor()
        with owned_group(root, late=True) as (_leader, identity, trigger, record):
            store.claim(attempt["attempt_id"], owner, controller=controller)
            store.heartbeat(attempt["attempt_id"], owner, phase="running", process=identity)
            store.claim_reconciliation(
                attempt["attempt_id"], owner, next_owner, controller=process_identity(os.getpid(), next_owner)
            )
            self.assertEqual(store.attempt(attempt["attempt_id"])["process"], identity)
            with (
                fork_after_proc_listing(trigger),
                self.assertRaisesRegex(AssessmentConflict, "process group is unresolved"),
            ):
                executor._seal(attempt, request, next_owner, root, "failed", {}, {}, None)
            artifact = result(store, request, root, attempt, "failed")
            with self.assertRaises(AssessmentConflict):
                store.finish(attempt["attempt_id"], next_owner, state="failed", artifact=artifact, detail={})
            store.unknown(attempt["attempt_id"], {"phase": "reconciliation_group_unresolved"}, owner=next_owner)
            after = store.attempt(attempt["attempt_id"])
            self.assertEqual(after["state"], "unknown")
            self.assertEqual(after["process"], identity)
            self.assertFalse(after["resources_settled"])
            self.assertEqual(process_state(record["identity"]), "alive")

    def test_commit_cannot_clear_live_group_identity_as_resource_proof(self):
        store, request, root = self.database
        attempt, owner = owned_attempt(store, request, root)
        with owned_group(root) as (leader, identity, child):
            store.heartbeat(attempt["attempt_id"], owner, phase="running", process=identity)
            artifact = result(store, request, root, attempt, "failed")
            with self.assertRaises(AssessmentConflict):
                store.finish(attempt["attempt_id"], owner, state="failed", artifact=artifact, detail={})
            current = store.attempt(attempt["attempt_id"])
            self.assertEqual(current["process"], identity)
            self.assertFalse(current["resources_settled"])
            self.assertTrue(terminate_owned(identity, grace_seconds=0.1, reap=lambda: reap_group(leader, child)))
            store.heartbeat(attempt["attempt_id"], owner, phase="sealing", process=None)
            settled = store.finish(attempt["attempt_id"], owner, state="failed", artifact=artifact, detail={})
            self.assertTrue(settled["resources_settled"])

    def test_unknown_group_preserves_owner_process_through_seal_cancel_and_orphan_reconcile(self):
        store, request, root = self.database
        request_id = register(store, request, root)
        attempt = store.new_attempt(request_id)
        store.begin_dispatch(attempt["attempt_id"])
        work = root / "resource-work"
        work.mkdir()
        service = AssessmentService(
            store,
            enabled=True,
            runtime=request.runtime,
            executor_sha256=request.record["executor_sha256"],
            work_root=work,
        )
        executor = AssessmentExecutor(
            service, TeleopProcess(Path(sys.executable), request.runtime, ProcessLimits(10, 1024**3, 10, 1024**2))
        )
        with owned_group(root, unbound=True) as (leader, identity, _child):
            owner = digest({"fixture": "resource-owner"})
            store.claim(attempt["attempt_id"], owner, controller=identity)
            store.heartbeat(attempt["attempt_id"], owner, phase="running", process=identity)
            with self.assertRaisesRegex(AssessmentConflict, "process group is unresolved"):
                executor._seal(attempt, request, owner, root, "failed", {}, {}, None)
            self.assertEqual(store.attempt(attempt["attempt_id"])["process"], identity)
            leader.kill()
            leader.wait(timeout=5)
            store.unknown(attempt["attempt_id"], {"phase": "original_orphan_observation"}, owner=owner)
            before = store.attempt(attempt["attempt_id"])
            self.assertEqual(executor.reconcile(attempt["attempt_id"])["state"], "unknown")
            self.assertFalse(executor.cancel_owned(attempt["attempt_id"]))
            after = store.attempt(attempt["attempt_id"])
            for key in ("owner_token", "owner_process", "process", "lease_until", "detail"):
                self.assertEqual(after[key], before[key])
            self.assertFalse(after["resources_settled"])
            self.assertTrue(after["cancel_requested"])

    def test_registration_is_idempotent_and_conflicting_payload_is_refused(self):
        database = self.database
        store, request, root = database
        request_id = register(store, request, root)
        assert register(store, request, root) == request_id
        assert store.request(request_id).to_record() == request.to_record()
        changed = request.to_record()
        changed["source"]["subject_sha256"] = digest({"fixture": "changed-source"})
        with self.assertRaisesRegex(AssessmentConflict, "another immutable request"):
            register(store, AssessmentRequest(changed), root)

    def test_concurrent_claim_and_duplicate_task_have_one_owner(self):
        database = self.database
        store, request, root = database
        request_id = register(store, request, root)
        attempt = store.new_attempt(request_id)
        assert not store.attempt(attempt["attempt_id"])["resources_settled"]
        assert store.begin_dispatch(attempt["attempt_id"])

        def claim(number):
            try:
                store.claim(attempt["attempt_id"], digest({"owner": number}), controller={"fixture": number})
                return True
            except AssessmentConflict:
                return False

        with ThreadPoolExecutor(max_workers=2) as pool:
            assert sorted(pool.map(claim, [1, 2])) == [False, True]
        assert store.new_attempt(request_id)["attempt_id"] == attempt["attempt_id"]
        self.assertTrue(store.attempt(attempt["attempt_id"])["detail"]["dispatch_accepted"])

    def test_queued_cancel_prevents_any_worker_claim(self):
        database = self.database
        store, request, root = database
        request_id = register(store, request, root)
        attempt = store.new_attempt(request_id)
        store.begin_dispatch(attempt["attempt_id"])
        store.dispatched(attempt["attempt_id"])
        cancelled = store.cancel(attempt["attempt_id"])
        assert cancelled["state"] == "cancelled" and cancelled["resources_settled"]
        self.assertTrue(cancelled["detail"]["dispatch_accepted"])
        with self.assertRaises(AssessmentConflict):
            store.claim(attempt["attempt_id"], "a" * 64, controller={"fixture": 1})

    def test_cancel_before_commit_cannot_label_completed_artifact_as_cancelled(self):
        database = self.database
        store, request, root = database
        attempt, owner = owned_attempt(store, request, root)
        completed = result(store, request, root, attempt, "completed")
        store.cancel(attempt["attempt_id"])
        with self.assertRaises(AssessmentConflict):
            store.finish(attempt["attempt_id"], owner, state="completed", artifact=completed, detail={})
        assert store.result_artifact(attempt["attempt_id"]) is None
        cancelled = result(store, request, root, attempt, "cancelled")
        final = store.finish(attempt["attempt_id"], owner, state="cancelled", artifact=cancelled, detail={})
        assert final["state"] == "cancelled" and final["resources_settled"]
        assert store.result_artifact(attempt["attempt_id"]) == cancelled
        self.assertTrue(final["detail"]["dispatch_accepted"])

    def test_stale_owner_cannot_commit_or_spawn_again_after_unknown(self):
        database = self.database
        store, request, root = database
        attempt, owner = owned_attempt(store, request, root)
        with store.transaction() as cursor:
            cursor.execute(
                "UPDATE assessment_attempt SET lease_until=now()-interval '1 second' WHERE attempt_id=%s",
                (attempt["attempt_id"],),
            )
        with self.assertRaises(AssessmentConflict):
            store.heartbeat(attempt["attempt_id"], owner, phase="running", process=None)
        store.unknown(attempt["attempt_id"], {"reason": "owner_lost"}, owner=owner)
        with self.assertRaises(AssessmentConflict):
            store.claim(attempt["attempt_id"], "b" * 64, controller={"fixture": 2})
        with self.assertRaises(AssessmentConflict):
            store.new_attempt(attempt["request_id"], retry=True)

    def test_retry_preserves_previous_failed_attempt_and_uses_new_identity(self):
        database = self.database
        store, request, root = database
        attempt, owner = owned_attempt(store, request, root)
        failure = result(store, request, root, attempt, "failed")
        store.finish(attempt["attempt_id"], owner, state="failed", artifact=failure, detail={"reason": "fixture"})
        successor = store.new_attempt(attempt["request_id"], retry=True)
        assert successor["attempt_id"] != attempt["attempt_id"]
        assert successor["dag_run_id"] != attempt["dag_run_id"]
        assert successor["attempt_number"] == 2
        assert store.result_artifact(attempt["attempt_id"]) == failure
        assert store.attempt(attempt["attempt_id"])["state"] == "failed"
        self.assertTrue(store.attempt(attempt["attempt_id"])["detail"]["dispatch_accepted"])
        self.assertNotIn("dispatch_accepted", successor["detail"])
