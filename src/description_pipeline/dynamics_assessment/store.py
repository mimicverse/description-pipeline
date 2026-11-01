"""Short PostgreSQL transactions for optional requests and owned attempts."""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import timedelta

from ..artifacts import ArtifactStore
from ..io import digest
from ..pipeline.contracts import ArtifactRef
from ..platform.facts import FactStore
from .contracts import (
    ACTIVE_STATES,
    RESULT_SCHEMA,
    TERMINAL_STATES,
    AssessmentConflict,
    AssessmentRequest,
    auxiliary_dag_run_id,
    parse_json,
    require,
    sha,
    text,
)


def _artifact_sql(alias: str) -> str:
    fields = (
        "artifact_id",
        "kind",
        "locator",
        "manifest_sha256",
        "set_sha256",
        "producer_run_id",
        "producer_stage",
        "producer_tool_sha256",
    )
    return "json_build_object(" + ",".join(f"'{key}',{alias}.{key}" for key in fields) + ")"


def _row(cursor) -> dict | None:
    value = cursor.fetchone()
    return dict(zip((field[0] for field in cursor.description), value, strict=True)) if value else None


def _detail_update(detail: dict) -> str:
    require(
        isinstance(detail, dict) and "dispatch_accepted" not in detail,
        "Assessment detail cannot replace dispatch acceptance",
    )
    return json.dumps(detail, allow_nan=False)


class AssessmentStore:
    """Reuse the platform artifact registry; store no duplicate artifact bytes."""

    def __init__(self, connect, artifacts: ArtifactStore):
        self.connect = connect
        self.artifacts = artifacts
        self.platform = FactStore(connect, artifacts)

    @contextmanager
    def transaction(self):
        connection = self.connect()
        try:
            with connection, connection.cursor() as cursor:
                yield cursor
        finally:
            connection.close()

    def register(self, key: str, request: AssessmentRequest, artifact: ArtifactRef) -> str:
        text(key, "Assessment idempotency key")
        require(len(key) <= 128, "Assessment idempotency key is too long")
        require(
            artifact.kind == "dynamics_request"
            and artifact.producer_stage is None
            and artifact.producer_tool_sha256 == request.record["executor_sha256"],
            "Assessment request artifact has the wrong producer",
        )
        data, _member, _manifest = self.artifacts.read_metadata_member(artifact, "request.json", max_bytes=1024 * 1024)
        require(
            AssessmentRequest(parse_json(data)).sha256 == request.sha256,
            "Registered request bytes differ from their identity",
        )
        self.artifacts.verify(artifact)
        self.platform.register_artifact(artifact)
        principal = request.record["principal"]
        request_id = digest({"principal": principal, "key": key})
        require(artifact.producer_run_id == request_id, "Request artifact belongs to another assessment")
        with self.transaction() as cursor:
            cursor.execute(
                "SELECT request_id,request_sha256,request_artifact_id FROM assessment_request "
                "WHERE principal=%s AND idempotency_key=%s",
                (principal, key),
            )
            existing = cursor.fetchone()
            if existing is not None:
                if existing != (request_id, request.sha256, artifact.artifact_id):
                    raise AssessmentConflict("Assessment idempotency key names another immutable request")
                return request_id
            cursor.execute("SELECT state FROM admission WHERE id=1 FOR SHARE")
            require(cursor.fetchone() == ("open",), "Platform admission is closed")
            cursor.execute(
                "INSERT INTO assessment_request(request_id,principal,idempotency_key,source_run_id,"
                "request_sha256,request_artifact_id) VALUES(%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT(principal,idempotency_key) DO NOTHING",
                (request_id, principal, key, request.source.record["run_id"], request.sha256, artifact.artifact_id),
            )
            cursor.execute(
                "SELECT request_id,request_sha256,request_artifact_id FROM assessment_request "
                "WHERE principal=%s AND idempotency_key=%s",
                (principal, key),
            )
            row = cursor.fetchone()
            if row != (request_id, request.sha256, artifact.artifact_id):
                raise AssessmentConflict("Assessment idempotency key names another immutable request")
        return request_id

    def request(self, request_id: str) -> AssessmentRequest:
        sha(request_id, "Assessment request id")
        with self.transaction() as cursor:
            cursor.execute(
                "SELECT r.principal,r.source_run_id,r.request_sha256,"
                + _artifact_sql("a")
                + " AS artifact FROM assessment_request r JOIN artifact a "
                "ON a.artifact_id=r.request_artifact_id WHERE r.request_id=%s",
                (request_id,),
            )
            row = _row(cursor)
        if row is None:
            raise KeyError(request_id)
        artifact = ArtifactRef.from_record(row["artifact"])
        data, _member, _manifest = self.artifacts.read_metadata_member(artifact, "request.json", max_bytes=1024 * 1024)
        request = AssessmentRequest(parse_json(data))
        require(
            request.sha256 == row["request_sha256"]
            and request.record["principal"] == row["principal"]
            and request.source.record["run_id"] == row["source_run_id"]
            and artifact.kind == "dynamics_request"
            and artifact.producer_stage is None
            and artifact.producer_tool_sha256 == request.record["executor_sha256"],
            "Assessment registration differs from its sealed request",
        )
        return request

    def new_attempt(self, request_id: str, *, retry: bool = False) -> dict:
        self.request(request_id)
        with self.transaction() as cursor:
            cursor.execute("SELECT request_id FROM assessment_request WHERE request_id=%s FOR UPDATE", (request_id,))
            require(cursor.fetchone() is not None, "Assessment request does not exist")
            cursor.execute(
                "SELECT * FROM assessment_attempt WHERE request_id=%s ORDER BY attempt_number DESC LIMIT 1",
                (request_id,),
            )
            previous = _row(cursor)
            if previous is not None:
                if not retry:
                    return previous
                if previous["state"] not in {"failed", "cancelled", "timeout"} or not previous["resources_settled"]:
                    raise AssessmentConflict(
                        "Assessment retry requires a settled failed, cancelled or timed-out attempt"
                    )
            elif retry:
                raise AssessmentConflict("The first assessment attempt is not a retry")
            number = previous["attempt_number"] + 1 if previous else 1
            attempt_id = digest({"request_id": request_id, "attempt_number": number})
            cursor.execute(
                "INSERT INTO assessment_attempt(attempt_id,request_id,attempt_number,dag_run_id,state) "
                "VALUES(%s,%s,%s,%s,'registered') RETURNING *",
                (attempt_id, request_id, number, auxiliary_dag_run_id(attempt_id)),
            )
            return _row(cursor)

    def attempt(self, attempt_id: str) -> dict:
        sha(attempt_id, "Assessment attempt id")
        with self.transaction() as cursor:
            cursor.execute(
                "SELECT *,lease_until<=now() AS lease_expired FROM assessment_attempt WHERE attempt_id=%s",
                (attempt_id,),
            )
            result = _row(cursor)
        if result is None:
            raise KeyError(attempt_id)
        require(result["state"] in ACTIVE_STATES | TERMINAL_STATES, "Assessment attempt state is invalid")
        if result["state"] in {"running", "verifying", "sealing"} and result["lease_expired"]:
            # A read projects uncertainty; it does not settle resources or grant takeover.
            result["detail"] = {**result["detail"], "reason": "owner_lease_expired", "previous_phase": result["state"]}
            result["state"] = "unknown"
        return result

    def latest(self, request_id: str) -> dict | None:
        with self.transaction() as cursor:
            cursor.execute(
                "SELECT * FROM assessment_attempt WHERE request_id=%s ORDER BY attempt_number DESC LIMIT 1",
                (request_id,),
            )
            latest = _row(cursor)
        return self.attempt(latest["attempt_id"]) if latest is not None else None

    def begin_dispatch(self, attempt_id: str) -> bool:
        with self.transaction() as cursor:
            cursor.execute("SELECT state FROM admission WHERE id=1 FOR SHARE")
            require(cursor.fetchone() == ("open",), "Platform admission is closed")
            cursor.execute(
                "UPDATE assessment_attempt SET state='dispatching',"
                "detail=detail-'dispatch_observation',updated_at=now() "
                "WHERE attempt_id=%s AND state='registered' AND NOT cancel_requested "
                "AND NOT (detail ? 'dispatch_accepted') RETURNING attempt_id",
                (attempt_id,),
            )
            return cursor.fetchone() is not None

    def dispatched(self, attempt_id: str) -> None:
        with self.transaction() as cursor:
            cursor.execute(
                "UPDATE assessment_attempt SET "
                "detail=jsonb_set(detail-'dispatch_observation','{dispatch_accepted}','true'::jsonb),"
                "state=CASE WHEN state IN ('registered','dispatching','unknown') AND owner_token IS NULL "
                "AND NOT cancel_requested THEN 'queued' ELSE state END,updated_at=now() WHERE attempt_id=%s",
                (attempt_id,),
            )

    def unknown(self, attempt_id: str, detail: dict, *, owner: str | None = None) -> None:
        with self.transaction() as cursor:
            cursor.execute(
                "UPDATE assessment_attempt SET state='unknown',detail=detail || %s::jsonb,updated_at=now() "
                "WHERE attempt_id=%s "
                "AND state NOT IN ('completed','unsupported','failed','cancelled','timeout') "
                "AND owner_token IS NOT DISTINCT FROM %s",
                (_detail_update(detail), attempt_id, owner),
            )

    def dispatch_absent(self, attempt_id: str) -> bool:
        """An authenticated exact-DAG absence observation permits the same dispatch."""
        with self.transaction() as cursor:
            cursor.execute(
                "UPDATE assessment_attempt SET state='registered',detail=detail || %s::jsonb,updated_at=now() "
                "WHERE attempt_id=%s AND state IN ('registered','dispatching','unknown') "
                "AND owner_token IS NULL AND process IS NULL AND NOT (detail ? 'dispatch_accepted') "
                "RETURNING attempt_id",
                (json.dumps({"dispatch_observation": "absent"}), attempt_id),
            )
            return cursor.fetchone() is not None

    def claim(self, attempt_id: str, owner: str, *, controller: dict, lease_seconds: float = 30) -> dict:
        sha(owner, "Assessment owner token")
        require(1 <= lease_seconds <= 300, "Assessment owner lease is invalid")
        with self.transaction() as cursor:
            cursor.execute(
                "UPDATE assessment_attempt SET state='running',owner_token=%s,owner_process=%s::jsonb,"
                "detail=jsonb_set(detail-'dispatch_observation','{dispatch_accepted}','true'::jsonb),"
                "lease_until=now()+%s,updated_at=now() WHERE attempt_id=%s "
                "AND state IN ('dispatching','queued') AND owner_token IS NULL "
                "AND NOT cancel_requested RETURNING *",
                (owner, json.dumps(controller, allow_nan=False), timedelta(seconds=lease_seconds), attempt_id),
            )
            result = _row(cursor)
        if result is None:
            raise AssessmentConflict("Assessment attempt is cancelled, unresolved, terminal or already owned")
        return result

    def heartbeat(self, attempt_id: str, owner: str, *, phase: str, process: dict | None) -> dict:
        require(phase in {"running", "verifying", "sealing"}, "Assessment execution phase is invalid")
        with self.transaction() as cursor:
            cursor.execute(
                "UPDATE assessment_attempt SET state=%s,process=%s::jsonb,"
                "lease_until=now()+interval '30 seconds',"
                "updated_at=now() WHERE attempt_id=%s AND owner_token=%s "
                "AND state IN ('running','verifying','sealing') AND lease_until>now() RETURNING *",
                (phase, json.dumps(process) if process is not None else None, attempt_id, owner),
            )
            result = _row(cursor)
        if result is None:
            raise AssessmentConflict("Assessment execution ownership is stale or unresolved")
        return result

    def cancel(self, attempt_id: str) -> dict:
        with self.transaction() as cursor:
            cursor.execute("SELECT * FROM assessment_attempt WHERE attempt_id=%s FOR UPDATE", (attempt_id,))
            current = _row(cursor)
            if current is None:
                raise KeyError(attempt_id)
            if current["state"] in TERMINAL_STATES:
                return current
            cursor.execute(
                "UPDATE assessment_attempt SET cancel_requested=true,"
                "state=CASE WHEN owner_token IS NULL THEN 'cancelled' ELSE state END,"
                "resources_settled=CASE WHEN owner_token IS NULL THEN true ELSE resources_settled END,"
                "updated_at=now() WHERE attempt_id=%s RETURNING *",
                (attempt_id,),
            )
            row = _row(cursor)
        if row is None:
            raise KeyError(attempt_id)
        return row

    def finish(self, attempt_id: str, owner: str, *, state: str, artifact: ArtifactRef, detail: dict) -> dict:
        require(state in TERMINAL_STATES, "Assessment final state is invalid")
        attempt = self.attempt(attempt_id)
        request = self.request(attempt["request_id"])
        require(
            artifact.kind == "dynamics_result"
            and artifact.producer_stage is None
            and artifact.producer_run_id == attempt_id
            and artifact.producer_tool_sha256 == request.record["executor_sha256"],
            "Assessment result artifact has the wrong producer",
        )
        self.artifacts.verify(artifact)
        data, _member, _manifest = self.artifacts.read_metadata_member(
            artifact,
            "assessment_result.json",
            max_bytes=1024 * 1024,
        )
        summary = parse_json(data)
        require(
            summary.get("schema") == RESULT_SCHEMA
            and summary.get("request_id") == attempt["request_id"]
            and summary.get("attempt_id") == attempt_id
            and summary.get("state") == state
            and summary.get("resources_settled") is True
            and summary.get("request_sha256") == request.sha256
            and summary.get("runtime_sha256") == request.runtime.sha256
            and summary.get("source_sha256") == request.source.sha256,
            "Assessment result differs from its immutable request or terminal state",
        )
        self.platform.register_artifact(artifact)
        with self.transaction() as cursor:
            cursor.execute(
                "UPDATE assessment_attempt SET state=%s,"
                "result_artifact_id=%s,detail=detail || %s::jsonb,resources_settled=true,process=NULL,lease_until=NULL,"
                "updated_at=now() WHERE attempt_id=%s AND owner_token=%s "
                "AND state IN ('running','verifying','sealing') AND lease_until>now() "
                "AND process IS NULL "
                "AND (NOT cancel_requested OR %s='cancelled') RETURNING *",
                (state, artifact.artifact_id, _detail_update(detail), attempt_id, owner, state),
            )
            result = _row(cursor)
        if result is None:
            raise AssessmentConflict("Only the current assessment owner may commit a settled result")
        return result

    def result_artifact(self, attempt_id: str) -> ArtifactRef | None:
        with self.transaction() as cursor:
            cursor.execute(
                "SELECT " + _artifact_sql("a") + " AS artifact FROM assessment_attempt t "
                "JOIN artifact a ON a.artifact_id=t.result_artifact_id WHERE t.attempt_id=%s",
                (attempt_id,),
            )
            row = cursor.fetchone()
        return ArtifactRef.from_record(row[0]) if row else None

    def claim_reconciliation(self, attempt_id: str, previous_owner: str, owner: str, *, controller: dict) -> None:
        """Fence an OS-reconciled orphan for sealing only, never re-execution."""
        sha(previous_owner, "Previous assessment owner")
        sha(owner, "Assessment reconciliation owner")
        with self.transaction() as cursor:
            cursor.execute(
                "UPDATE assessment_attempt SET state='sealing',owner_token=%s,owner_process=%s::jsonb,"
                "lease_until=now()+interval '30 seconds',updated_at=now() "
                "WHERE attempt_id=%s AND owner_token=%s AND NOT resources_settled "
                "AND state IN ('running','verifying','sealing','unknown') RETURNING attempt_id",
                (owner, json.dumps(controller, allow_nan=False), attempt_id, previous_owner),
            )
            if cursor.fetchone() is None:
                raise AssessmentConflict("Assessment orphan identity changed during reconciliation")
