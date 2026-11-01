"""PostgreSQL authority for interactive session lifecycle and commands."""

from __future__ import annotations

from contextlib import contextmanager
import json
import secrets

from ..io import digest
from ..pipeline.contracts import ArtifactRef


class SimulationConflict(ValueError):
    """An immutable session or command identity was reused inconsistently."""


class SimulationStore:
    def __init__(self, connect):
        self.connect = connect

    def acquire_supervisor(self):
        connection = self.connect()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_try_advisory_lock(738204163)")
                if not cursor.fetchone()[0]:
                    raise SimulationConflict("Another simulation supervisor owns this database")
            connection.commit()
            return connection
        except BaseException:
            connection.close()
            raise

    @contextmanager
    def transaction(self):
        connection = self.connect()
        try:
            with connection:
                yield connection.cursor()
        finally:
            connection.close()

    def create(self, actor: str, key: str, source_run: str, request: dict, owner: str, *, capacity: int = 2) -> dict:
        request_digest = digest(request)
        with self.transaction() as cursor:
            # One bounded capacity domain; no process is spawned inside this transaction.
            cursor.execute("SELECT pg_advisory_xact_lock(738204162)")
            cursor.execute(
                "SELECT session_id, request_digest FROM simulation_session WHERE actor=%s AND request_key=%s",
                (actor, key),
            )
            existing = cursor.fetchone()
            if existing:
                if existing[1] != request_digest:
                    raise SimulationConflict("Session idempotency key names another request")
                session_id = existing[0]
            else:
                cursor.execute("SELECT state FROM admission WHERE id=1 FOR SHARE")
                if cursor.fetchone() != ("open",):
                    raise SimulationConflict("Platform admission is closed")
                cursor.execute("SELECT count(*) FROM simulation_session WHERE state NOT IN ('stopped','failed')")
                if cursor.fetchone()[0] >= capacity:
                    raise SimulationConflict("Simulation capacity is exhausted")
                session_id = secrets.token_hex(16)
                cursor.execute(
                    "INSERT INTO simulation_session(session_id,actor,request_key,source_run_id,request_digest,request,"
                    "owner_nonce,state) VALUES(%s,%s,%s,%s,%s,%s::jsonb,%s,'starting')",
                    (session_id, actor, key, source_run, request_digest, json.dumps(request), owner),
                )
        return self.get(session_id)

    def admission_open(self):
        with self.transaction() as cursor:
            cursor.execute("SELECT state FROM admission WHERE id=1")
            return cursor.fetchone() == ("open",)

    def get(self, session_id: str) -> dict:
        with self.transaction() as cursor:
            cursor.execute(
                "SELECT session_id,actor,source_run_id,request_digest,request,owner_nonce,state,revision,"
                "error,process_identity,recording_artifact FROM simulation_session WHERE session_id=%s",
                (session_id,),
            )
            row = cursor.fetchone()
            if row is None:
                raise KeyError(session_id)
            result = dict(
                zip(
                    (
                        "session_id",
                        "actor",
                        "source_run_id",
                        "request_digest",
                        "request",
                        "owner_nonce",
                        "state",
                        "revision",
                        "error",
                        "process_identity",
                        "recording_artifact",
                    ),
                    row,
                    strict=True,
                )
            )
            if digest(result["request"]) != result["request_digest"]:
                raise SimulationConflict("Stored simulation request digest is inconsistent")
            if result["recording_artifact"] is not None:
                ref = ArtifactRef.from_record(result["recording_artifact"])
                cursor.execute(
                    "SELECT kind,locator,manifest_sha256,set_sha256,producer_run_id,producer_stage,"
                    "producer_tool_sha256 "
                    "FROM artifact WHERE artifact_id=%s",
                    (ref.artifact_id,),
                )
                registered = cursor.fetchone()
                if registered != (
                    ref.kind,
                    ref.locator,
                    ref.manifest_sha256,
                    ref.set_sha256,
                    ref.producer_run_id,
                    ref.producer_stage,
                    ref.producer_tool_sha256,
                ):
                    raise SimulationConflict("Session recording differs from its artifact registration")
            return result

    def transition(self, session_id: str, owner: str, state: str, *, error=None, process=None, recording=None) -> dict:
        allowed = {
            "starting": {"starting"},
            "ready": {"starting"},
            "stopping": {"starting", "ready", "stopping"},
            "stopped": {"stopping"},
            "failed": {"starting", "ready", "stopping", "unresolved"},
            "unresolved": {"starting", "ready", "stopping", "unresolved"},
        }
        if state not in allowed:
            raise ValueError("Invalid simulation transition")
        with self.transaction() as cursor:
            cursor.execute(
                "SELECT state FROM simulation_session WHERE session_id=%s AND owner_nonce=%s FOR UPDATE",
                (session_id, owner),
            )
            row = cursor.fetchone()
            if row is None or row[0] not in allowed[state]:
                raise SimulationConflict("Session ownership or lifecycle changed")
            if recording is not None:
                ref = ArtifactRef.from_record(recording)
                cursor.execute(
                    "INSERT INTO artifact(artifact_id,kind,locator,manifest_sha256,set_sha256,producer_run_id,"
                    "producer_stage,producer_tool_sha256) VALUES(%s,%s,%s,%s,%s,%s,%s,%s) "
                    "ON CONFLICT (artifact_id) DO NOTHING",
                    (
                        ref.artifact_id,
                        ref.kind,
                        ref.locator,
                        ref.manifest_sha256,
                        ref.set_sha256,
                        ref.producer_run_id,
                        ref.producer_stage,
                        ref.producer_tool_sha256,
                    ),
                )
            cursor.execute(
                "UPDATE simulation_session SET state=%s,error=%s,process_identity=COALESCE(%s::jsonb,process_identity),"
                "recording_artifact=COALESCE(%s::jsonb,recording_artifact),revision=revision+1,updated_at=now(),"
                "ready_at=CASE WHEN %s='ready' THEN COALESCE(ready_at,now()) ELSE ready_at END "
                "WHERE session_id=%s AND owner_nonce=%s AND state NOT IN ('stopped','failed')",
                (
                    state,
                    error,
                    json.dumps(process) if process else None,
                    json.dumps(recording) if recording else None,
                    state,
                    session_id,
                    owner,
                ),
            )
            if cursor.rowcount != 1:
                raise SimulationConflict("Session ownership or terminal state changed")
        return self.get(session_id)

    def previous_profile(self, actor: str, subject: str) -> dict | None:
        with self.transaction() as cursor:
            cursor.execute(
                "SELECT request_digest,request FROM simulation_session WHERE actor=%s AND ready_at IS NOT NULL "
                "AND request->'source'->>'subject_sha256'=%s ORDER BY created_at DESC LIMIT 1",
                (actor, subject),
            )
            row = cursor.fetchone()
            if row is not None and digest(row[1]) != row[0]:
                raise SimulationConflict("Stored simulation request digest is inconsistent")
            return {key: row[1][key] for key in ("profile", "settings", "record")} if row else None

    def authorize_command(self, session_id: str, command_id: str, actor: str, request: dict) -> dict | None:
        with self.transaction() as cursor:
            cursor.execute("SELECT actor,state FROM simulation_session WHERE session_id=%s FOR UPDATE", (session_id,))
            row = cursor.fetchone()
            if row is None or row[0] != actor:
                raise SimulationConflict("Session is not controlled by this actor")
            cursor.execute(
                "SELECT request_digest,result FROM simulation_command WHERE session_id=%s AND command_id=%s",
                (session_id, command_id),
            )
            existing = cursor.fetchone()
            if existing:
                if existing[0] != digest(request):
                    raise SimulationConflict("Command identity names another request")
                return existing[1] or {"state": "pending"}
            if row[1] not in {"starting", "ready", "stopping"}:
                raise SimulationConflict("Session is terminal or unresolved")
            cursor.execute(
                "INSERT INTO simulation_command(session_id,command_id,request_digest,request) "
                "VALUES(%s,%s,%s,%s::jsonb)",
                (session_id, command_id, digest(request), json.dumps(request)),
            )
        return None

    def complete_command(self, session_id: str, command_id: str, result: dict) -> None:
        with self.transaction() as cursor:
            cursor.execute(
                "UPDATE simulation_command SET result=%s::jsonb WHERE session_id=%s AND command_id=%s "
                "AND result IS NULL",
                (json.dumps(result), session_id, command_id),
            )

    def orphaned(self, owner: str) -> list[dict]:
        with self.transaction() as cursor:
            cursor.execute(
                "SELECT session_id FROM simulation_session WHERE owner_nonce<>%s AND state NOT IN ('stopped','failed')",
                (owner,),
            )
            ids = [row[0] for row in cursor.fetchall()]
        return [self.get(session_id) for session_id in ids]

    def reconcile_orphan(self, session_id: str, previous_owner: str) -> None:
        # No guessed PID termination: an old worker must stop on its private pipe/watchdog.
        with self.transaction() as cursor:
            cursor.execute(
                "UPDATE simulation_session SET state='unresolved',error='Supervisor ownership changed',"
                "revision=revision+1,updated_at=now() WHERE session_id=%s AND owner_nonce=%s "
                "AND state NOT IN ('stopped','failed')",
                (session_id, previous_owner),
            )
