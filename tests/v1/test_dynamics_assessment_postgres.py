"""Actual PostgreSQL idempotency, execution fencing and cancellation races."""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from description_pipeline.dynamics_assessment.contracts import (
    RESULT_SCHEMA,
    AssessmentConflict,
    AssessmentRequest,
)
from description_pipeline.dynamics_assessment.store import AssessmentStore
from description_pipeline.io import canonical, digest
from tests.v1.test_dynamics_assessment_contracts import request_case

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def postgres_server():
    pgserver = pytest.importorskip("pgserver")
    pytest.importorskip("psycopg2")
    with tempfile.TemporaryDirectory(prefix="assessment-postgres-") as temporary:
        server = pgserver.get_server(Path(temporary) / "pgdata", cleanup_mode="delete")
        try:
            yield server
        finally:
            server.cleanup()


@pytest.fixture
def database(tmp_path, postgres_server):
    import psycopg2

    schema = "assessment_test_" + uuid.uuid4().hex
    sql = (ROOT / "deploy/platform/schema.sql").read_text()
    if ".assessment_request" not in sql:
        addition = os.environ.get("ASSESSMENT_TEST_SCHEMA_APPEND")
        if not addition:
            pytest.skip("Assessment public DDL is awaiting root integration")
        sql += "\n" + Path(addition).read_text()
    sql = sql.replace(':"platform_schema"', '"' + schema + '"')
    with psycopg2.connect(postgres_server.get_uri()) as connection, connection.cursor() as cursor:
        cursor.execute('CREATE SCHEMA "' + schema + '"')
        cursor.execute(sql)
        cursor.execute('UPDATE "' + schema + "\".admission SET state='open' WHERE id=1")
        cursor.execute(
            'INSERT INTO "' + schema + '".run_admission '
            "(run_id,request_id,request,request_digest) VALUES(%s,%s,%s::jsonb,%s)",
            ("source-run", "source-dag-run", "{}", digest({})),
        )

    def connect():
        return psycopg2.connect(postgres_server.get_uri(), options="-c search_path=" + schema + ",public")

    artifacts, request = request_case(tmp_path)
    store = AssessmentStore(connect, artifacts)
    yield store, request, tmp_path
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
    return attempt, owner


def test_registration_is_idempotent_and_conflicting_payload_is_refused(database):
    store, request, root = database
    request_id = register(store, request, root)
    assert register(store, request, root) == request_id
    assert store.request(request_id).to_record() == request.to_record()
    changed = request.to_record()
    changed["source"]["subject_sha256"] = digest({"fixture": "changed-source"})
    with pytest.raises(AssessmentConflict, match="another immutable request"):
        register(store, AssessmentRequest(changed), root)


def test_concurrent_claim_and_duplicate_task_have_one_owner(database):
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


def test_queued_cancel_prevents_any_worker_claim(database):
    store, request, root = database
    request_id = register(store, request, root)
    attempt = store.new_attempt(request_id)
    store.begin_dispatch(attempt["attempt_id"])
    store.dispatched(attempt["attempt_id"])
    cancelled = store.cancel(attempt["attempt_id"])
    assert cancelled["state"] == "cancelled" and cancelled["resources_settled"]
    with pytest.raises(AssessmentConflict):
        store.claim(attempt["attempt_id"], "a" * 64, controller={"fixture": 1})


def test_cancel_before_commit_cannot_label_completed_artifact_as_cancelled(database):
    store, request, root = database
    attempt, owner = owned_attempt(store, request, root)
    completed = result(store, request, root, attempt, "completed")
    store.cancel(attempt["attempt_id"])
    with pytest.raises(AssessmentConflict):
        store.finish(attempt["attempt_id"], owner, state="completed", artifact=completed, detail={})
    assert store.result_artifact(attempt["attempt_id"]) is None
    cancelled = result(store, request, root, attempt, "cancelled")
    final = store.finish(attempt["attempt_id"], owner, state="cancelled", artifact=cancelled, detail={})
    assert final["state"] == "cancelled" and final["resources_settled"]
    assert store.result_artifact(attempt["attempt_id"]) == cancelled


def test_stale_owner_cannot_commit_or_spawn_again_after_unknown(database):
    store, request, root = database
    attempt, owner = owned_attempt(store, request, root)
    with store.transaction() as cursor:
        cursor.execute(
            "UPDATE assessment_attempt SET lease_until=now()-interval '1 second' WHERE attempt_id=%s",
            (attempt["attempt_id"],),
        )
    with pytest.raises(AssessmentConflict):
        store.heartbeat(attempt["attempt_id"], owner, phase="running", process=None)
    store.unknown(attempt["attempt_id"], {"reason": "owner_lost"}, owner=owner)
    with pytest.raises(AssessmentConflict):
        store.claim(attempt["attempt_id"], "b" * 64, controller={"fixture": 2})
    with pytest.raises(AssessmentConflict):
        store.new_attempt(attempt["request_id"], retry=True)


def test_retry_preserves_previous_failed_attempt_and_uses_new_identity(database):
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
