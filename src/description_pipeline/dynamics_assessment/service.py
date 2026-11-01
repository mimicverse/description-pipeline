"""Authenticated admission, exact auxiliary dispatch and immutable result reads."""

from __future__ import annotations

import tempfile
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ..io import canonical, digest, safe_process_output
from ..pipeline.contracts import ArtifactRef
from .contracts import (
    DAG_ID,
    REQUEST_SCHEMA,
    AssessmentConflict,
    AssessmentDisabled,
    AssessmentRequest,
    RuntimePin,
    SourceBinding,
    SourceNotReady,
    dispatch_conf,
    dispatch_was_accepted,
    parse_json,
    relative,
    require,
    sha,
    text,
)
from .store import AssessmentStore


class DispatchTransport(Protocol):
    """The root binds its current authenticated Airflow client to this surface."""

    def trigger(self, dag_id: str, dag_run_id: str, conf: dict) -> dict: ...

    def observe(self, dag_id: str, dag_run_id: str) -> dict | None:
        """Return the exact DagRun, or authenticated authoritative absence."""
        ...


@dataclass(frozen=True)
class AssessmentAccess:
    """Per-request authenticated context, supplied by the existing portal."""

    principal: str
    is_admin: bool
    source: Callable[[str, bool], SourceBinding]

    def __post_init__(self):
        text(self.principal, "Authenticated assessment principal")
        require(type(self.is_admin) is bool and callable(self.source), "Assessment access context is invalid")


class AssessmentService:
    """No source generation, physics, required-stage decision or PR publication."""

    def __init__(
        self,
        store: AssessmentStore,
        *,
        enabled: bool = False,
        runtime: RuntimePin | None = None,
        executor_sha256: str | None = None,
        work_root: Path | None = None,
        capacity: int = 2,
        input_limit: int = 64 * 1024**2,
    ):
        require(type(enabled) is bool, "Assessment enable flag must be explicit")
        self.store = store
        self.enabled = enabled
        self.runtime = runtime
        self.executor_sha256 = executor_sha256
        self.work_root = work_root
        require(type(capacity) is int and 1 <= capacity <= 16, "Assessment capacity is invalid")
        require(type(input_limit) is int and 0 < input_limit <= 64 * 1024**2, "Assessment input limit is invalid")
        self.capacity = capacity
        self.input_limit = input_limit
        self._upload_gate = None
        self._upload_gate_lock = threading.Lock()
        if enabled:
            require(isinstance(runtime, RuntimePin), "Enabled assessment requires a fixed runtime")
            sha(executor_sha256, "Assessment executor")
            require(
                isinstance(work_root, Path)
                and work_root.is_absolute()
                and work_root.is_dir()
                and not work_root.is_symlink(),
                "Assessment needs an existing private work root",
            )

    def _enabled(self) -> None:
        if not self.enabled:
            raise AssessmentDisabled("Optional dynamics assessment is disabled")

    @staticmethod
    def _source(access: AssessmentAccess, dag_run_id: str, *, control: bool) -> SourceBinding:
        require(isinstance(access, AssessmentAccess), "Authenticated assessment access is required")
        source = access.source(dag_run_id, control)
        require(
            isinstance(source, SourceBinding) and source.record["dag_run_id"] == dag_run_id,
            "Authorized source does not match the selected source DagRun",
        )
        return source

    def _authorized(self, access: AssessmentAccess, request_id: str, *, control: bool) -> AssessmentRequest:
        request = self.store.request(request_id)
        source = self._source(access, request.source.record["dag_run_id"], control=control)
        if request.record["principal"] != access.principal and not access.is_admin:
            raise PermissionError("Assessment belongs to another operator")
        require(
            source.sha256 == request.source.sha256, "Assessment source changed or names another certificate closure"
        )
        return request

    def upload_bundle(self, access: AssessmentAccess, dag_run_id: str, read_archive, *, should_cancel=lambda: False):
        """Authorize and reserve capacity before the caller consumes upload bytes."""
        from .bundle import seal_upload

        self._enabled()
        source = self._source(access, dag_run_id, control=True)
        with self._upload_gate_lock:
            if self._upload_gate is None:
                from ..orchestration.uploads import UploadGate

                self._upload_gate = UploadGate(self.capacity)
        with self._upload_gate.slot(access.principal):
            if should_cancel():
                raise AssessmentConflict("Execution input upload was cancelled")
            archive = read_archive(self.input_limit)
            return seal_upload(self, access, source, archive, should_cancel=should_cancel)

    def create(
        self,
        access: AssessmentAccess,
        dag_run_id: str,
        key: str,
        bundle: ArtifactRef | None,
        transport: DispatchTransport,
    ) -> dict:
        self._enabled()
        text(key, "Assessment idempotency key")
        require(len(key) <= 128, "Assessment idempotency key is too long")
        source = self._source(access, dag_run_id, control=True)
        require(bundle is None or isinstance(bundle, ArtifactRef), "Execution bundle must be an ArtifactRef")
        request = AssessmentRequest(
            {
                "schema": REQUEST_SCHEMA,
                "principal": access.principal,
                "source": source.to_record(),
                "bundle": bundle.to_record() if bundle else None,
                "runtime": self.runtime.to_record(),
                "executor_sha256": self.executor_sha256,
            }
        )
        source.verify(self.store.artifacts)
        if bundle is not None:
            manifest = self.store.artifacts.verify(bundle)
            if any(member["path"] == "admission.json" for member in manifest["members"]):
                data, _member, _manifest = self.store.artifacts.read_metadata_member(
                    bundle, "admission.json", max_bytes=65536
                )
                admission = parse_json(data)
                require(
                    admission.get("principal") == access.principal and admission.get("source_sha256") == source.sha256,
                    "Execution bundle was admitted for another operator or source",
                )
            self.store.platform.register_artifact(bundle)
        request_id = digest({"principal": access.principal, "key": key})
        with tempfile.TemporaryDirectory(prefix="assessment-request-", dir=self.work_root) as temporary:
            root = Path(temporary)
            (root / "request.json").write_bytes(canonical(request.to_record()) + b"\n")
            artifact = self.store.artifacts.seal_tree(
                root,
                kind="dynamics_request",
                producer_run_id=request_id,
                producer_stage=None,
                producer_tool_sha256=self.executor_sha256,
            )
            request_id = self.store.register(key, request, artifact)
        attempt = self.store.new_attempt(request_id)
        self.dispatch(attempt["attempt_id"], transport)
        return self.get(access, request_id)

    def dispatch(self, attempt_id: str, transport: DispatchTransport) -> None:
        """Dispatch once. Any lost/ambiguous response stays UNKNOWN until observed."""
        self._enabled()
        attempt = self.store.attempt(attempt_id)
        request = self.store.request(attempt["request_id"])
        require(
            request.runtime.sha256 == self.runtime.sha256 and request.record["executor_sha256"] == self.executor_sha256,
            "Assessment request names another runtime or executor",
        )
        if not self.store.begin_dispatch(attempt_id):
            return
        conf = dispatch_conf(attempt["request_id"], attempt_id, request)
        try:
            payload = transport.trigger(DAG_ID, attempt["dag_run_id"], conf)
            self._match_dispatch(payload, attempt, conf)
        except Exception as error:
            self.store.unknown(attempt_id, {"phase": "dispatch", "message": safe_process_output(str(error))["text"]})
            return
        self.store.dispatched(attempt_id)

    @staticmethod
    def _match_dispatch(payload: object, attempt: dict, conf: dict) -> None:
        require(
            isinstance(payload, dict)
            and payload.get("dag_id") == DAG_ID
            and payload.get("dag_run_id") == attempt["dag_run_id"]
            and payload.get("conf") == conf,
            "Airflow observation differs from the registered assessment dispatch",
        )

    def reconcile_dispatch(self, access: AssessmentAccess, attempt_id: str, transport: DispatchTransport) -> dict:
        attempt = self.store.attempt(attempt_id)
        request = self._authorized(access, attempt["request_id"], control=True)
        if attempt["owner_token"] is not None or attempt["state"] not in {
            "registered",
            "dispatching",
            "unknown",
            "queued",
        }:
            return self.get(access, attempt["request_id"])
        conf = dispatch_conf(attempt["request_id"], attempt_id, request)
        try:
            payload = transport.observe(DAG_ID, attempt["dag_run_id"])
            if payload is None:
                accepted = dispatch_was_accepted(attempt)
                if accepted or attempt["state"] == "queued":
                    if not accepted:
                        self.store.dispatched(attempt_id)
                    raise AssessmentConflict("A previously accepted assessment DagRun is now absent")
                if not self.store.dispatch_absent(attempt_id):
                    raise AssessmentConflict("Assessment ownership or acceptance changed during absence observation")
            else:
                self._match_dispatch(payload, attempt, conf)
                self.store.dispatched(attempt_id)
        except Exception as error:
            self.store.unknown(
                attempt_id, {"phase": "dispatch_reconciliation", "message": safe_process_output(str(error))["text"]}
            )
        return self.get(access, attempt["request_id"])

    def dispatch_registered(self, access: AssessmentAccess, attempt_id: str, transport: DispatchTransport) -> dict:
        """Explicitly send the original request after an authoritative absence."""
        self._enabled()
        attempt = self.store.attempt(attempt_id)
        self._authorized(access, attempt["request_id"], control=True)
        if (
            attempt["state"] != "registered"
            or dispatch_was_accepted(attempt)
            or attempt["detail"].get("dispatch_observation") != "absent"
        ):
            raise AssessmentConflict("Original dispatch requires a never-accepted attempt and confirmed absence")
        self.dispatch(attempt_id, transport)
        return self.get(access, attempt["request_id"])

    def retry(self, access: AssessmentAccess, request_id: str, transport: DispatchTransport) -> dict:
        self._enabled()
        request = self._authorized(access, request_id, control=True)
        require(
            request.runtime.sha256 == self.runtime.sha256 and request.record["executor_sha256"] == self.executor_sha256,
            "Assessment retry requires the original fixed runtime and executor",
        )
        attempt = self.store.new_attempt(request_id, retry=True)
        self.dispatch(attempt["attempt_id"], transport)
        return self.get(access, request_id)

    def cancel(self, access: AssessmentAccess, attempt_id: str) -> dict:
        attempt = self.store.attempt(attempt_id)
        self._authorized(access, attempt["request_id"], control=True)
        self.store.cancel(attempt_id)
        return self.get(access, attempt["request_id"])

    def reconcile(
        self, access: AssessmentAccess, attempt_id: str, transport: DispatchTransport, runtime_reconciler=None
    ) -> dict:
        attempt = self.store.attempt(attempt_id)
        self._authorized(access, attempt["request_id"], control=True)
        if attempt["owner_token"] is None:
            return self.reconcile_dispatch(access, attempt_id, transport)
        if runtime_reconciler is None:
            raise AssessmentConflict("Runtime reconciliation requires the owning assessment resource host")
        runtime_reconciler(attempt_id)
        return self.get(access, attempt["request_id"])

    def get(self, access: AssessmentAccess, request_id: str) -> dict:
        request = self._authorized(access, request_id, control=False)
        attempt = self.store.latest(request_id)
        public_fields = (
            "attempt_id",
            "attempt_number",
            "dag_run_id",
            "state",
            "cancel_requested",
            "resources_settled",
            "detail",
        )
        result = {
            "request_id": request_id,
            "source_dag_run_id": request.source.record["dag_run_id"],
            "source_sha256": request.source.sha256,
            "source_certificates": request.source.record["certificates"],
            "runtime_sha256": request.runtime.sha256,
            "attempt": {key: attempt[key] for key in public_fields} if attempt else None,
            "publish_dependency": False,
        }
        if attempt:
            result["attempt"]["dispatch_accepted"] = dispatch_was_accepted(attempt)
            artifact = self.store.result_artifact(attempt["attempt_id"])
            result["result_artifact"] = artifact.to_record() if artifact else None
        return result

    def list_for_source(self, access: AssessmentAccess, dag_run_id: str) -> dict:
        if not self.enabled:
            return {"enabled": False, "assessments": [], "publish_dependency": False}
        try:
            source = self._source(access, dag_run_id, control=False)
        except SourceNotReady as error:
            return {
                "enabled": True,
                "source_ready": False,
                "input_findings": [str(error)],
                "assessments": [],
                "publish_dependency": False,
            }
        with self.store.transaction() as cursor:
            cursor.execute(
                "SELECT request_id FROM assessment_request WHERE source_run_id=%s "
                "AND (principal=%s OR %s) ORDER BY created_at DESC LIMIT 100",
                (source.record["run_id"], access.principal, access.is_admin),
            )
            identities = [row[0] for row in cursor.fetchall()]
        return {
            "enabled": True,
            "source_ready": True,
            "source": source.to_record(),
            "source_sha256": source.sha256,
            "input_limit_bytes": self.input_limit,
            "assessments": [self.get(access, identity) for identity in identities],
            "publish_dependency": False,
        }

    def read_result(
        self,
        access: AssessmentAccess,
        attempt_id: str,
        path: str = "assessment_result.json",
        *,
        max_bytes: int = 16 * 1024 * 1024,
    ) -> tuple[bytes, dict]:
        attempt = self.store.attempt(attempt_id)
        self._authorized(access, attempt["request_id"], control=False)
        artifact = self.store.result_artifact(attempt_id)
        if artifact is None:
            raise AssessmentConflict("No settled assessment result artifact is registered")
        relative(path, "Assessment result member")
        data, member, _manifest = self.store.artifacts.read_metadata_member(artifact, path, max_bytes=max_bytes)
        if path == "assessment_result.json":
            result = parse_json(data)
            require(
                result.get("attempt_id") == attempt_id and result.get("state") == attempt["state"],
                "Registered assessment result state differs from its attempt",
            )
        return data, member
