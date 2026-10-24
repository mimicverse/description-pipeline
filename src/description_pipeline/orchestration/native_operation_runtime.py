"""Persistent single-resource Windows native operations."""

from __future__ import annotations

import copy
import json
import os
import queue
import re
import shutil
import sqlite3
import threading
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from ..io import acquire_process_lock, canonical, digest, file_digest
from ..sources.solidworks.discover_stage import (
    NATIVE_OBSERVATION_SCHEMA,
    NativeDiscoverRefusal,
    NativeDiscoverRequest,
)
from ..sources.solidworks.handoff import MAX_HANDOFF_BYTES
from .discover_protocol import run_windows_discover, seal_observation_bundle

OPERATION_SCHEMA = "description-pipeline.native-operation-request/v1"
OPERATION_VIEW_SCHEMA = "description-pipeline.native-operation-view/v1"
DISCOVER_KIND = "discover"
CAPTURE_KIND = "capture"
_KINDS = frozenset({DISCOVER_KIND, CAPTURE_KIND})
_STATES = frozenset(
    {
        "accepted",
        "awaiting_input",
        "running",
        "satisfied",
        "failed",
        "cancelled",
        "unknown",
    }
)
_TERMINAL_STATES = frozenset({"satisfied", "failed", "cancelled", "unknown"})
_SHA_CHARS = frozenset("0123456789abcdef")
_OPERATION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_WINDOWS_RESERVED_NAMES = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{index}" for index in range(1, 10)),
        *(f"LPT{index}" for index in range(1, 10)),
    }
)


class NativeOperationError(RuntimeError):
    """A client-correctable native operation protocol error."""

    def __init__(
        self,
        message: str,
        *,
        status: int = 400,
        code: str = "native_operation_invalid",
        detail: Mapping[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.detail = dict(detail or {})


class NativeOperationConflict(NativeOperationError):
    def __init__(self, operation_id: str) -> None:
        super().__init__(
            f"Native operation {operation_id!r} is already bound to another payload",
            status=409,
            code="native_operation_conflict",
        )


class NativeOperationFailure(Exception):
    """A definite domain failure with a resource-settlement statement."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        resource_terminated: bool,
        detail: Mapping[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.resource_terminated = resource_terminated
        self.detail = dict(detail or {})


class NativeOperationUnresolved(Exception):
    """An external operation whose result or resource release is unknown."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        resource_terminated: bool = False,
        detail: Mapping[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.resource_terminated = resource_terminated
        self.detail = dict(detail or {})


def _operation_id(value: object) -> str:
    if (
        not isinstance(value, str)
        or _OPERATION_ID.fullmatch(value) is None
        or value.endswith(".")
        or value.split(".", 1)[0].upper() in _WINDOWS_RESERVED_NAMES
    ):
        raise NativeOperationError(
            "operation_id must be a stable ASCII path-safe identifier"
        )
    return value


def _sha256(value: object, where: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in _SHA_CHARS for character in value)
    ):
        raise NativeOperationError(f"{where} must be a lowercase SHA-256 digest")
    return value


def _optional_sha256(value: object, where: str) -> str | None:
    return None if value is None else _sha256(value, where)


def _timestamp(value: object, where: str, *, optional: bool = False) -> str | None:
    if optional and value is None:
        return None
    if not isinstance(value, str):
        raise NativeOperationError(f"{where} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise NativeOperationError(f"{where} must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None:
        raise NativeOperationError(f"{where} must carry a timezone")
    return value


def _text(value: object, where: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or "\x00" in value
        or any(ord(character) < 32 for character in value)
    ):
        raise NativeOperationError(f"{where} must be non-empty printable text")
    return value


def _artifact_name(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 255
        or value in {".", ".."}
        or "\x00" in value
        or any(ord(character) < 32 for character in value)
        or "/" in value
        or "\\" in value
        or not value.casefold().endswith(".zip")
    ):
        raise NativeOperationError("artifact.name must be one plain ZIP file name")
    return value


def _single_line(value: object, *, limit: int = 4096) -> str:
    text = str(value or "")
    return "".join(
        " " if ord(character) < 32 or ord(character) == 127 else character
        for character in text
    )[:limit] or "native operation terminal"


def build_operation_record(
    *,
    operation_id: str,
    kind: str,
    request: Mapping[str, object],
    input_archive_sha256: str,
    input_archive_bytes: int,
    native_tool_requirement_sha256: str,
    output_contract: str,
) -> dict[str, object]:
    """Build the canonical wire record, including its derived payload digest."""

    requested = _operation_id(operation_id)
    if kind not in _KINDS:
        raise NativeOperationError("Native operation kind must be discover or capture")
    if not isinstance(request, Mapping):
        raise NativeOperationError("Native operation request must be an object")
    request_record = copy.deepcopy(dict(request))
    request_sha256 = digest(request_record)
    archive_sha256 = _sha256(input_archive_sha256, "input.archive_sha256")
    if type(input_archive_bytes) is not int or input_archive_bytes <= 0:
        raise NativeOperationError("input.archive_bytes must be positive")
    tool_sha256 = _sha256(
        native_tool_requirement_sha256,
        "native_tool.requirement_sha256",
    )
    output = _text(output_contract, "output_contract")
    payload_record = {
        "input_archive_bytes": input_archive_bytes,
        "input_archive_sha256": archive_sha256,
        "kind": kind,
        "native_tool_requirement_sha256": tool_sha256,
        "operation_id": requested,
        "output_contract": output,
        "request": request_record,
        "request_sha256": request_sha256,
        "schema_version": OPERATION_SCHEMA,
    }
    return {
        "schema_version": OPERATION_SCHEMA,
        "operation_id": requested,
        "kind": kind,
        "request": request_record,
        "request_sha256": request_sha256,
        "input": {
            "archive_sha256": archive_sha256,
            "archive_bytes": input_archive_bytes,
        },
        "native_tool": {"requirement_sha256": tool_sha256},
        "output_contract": output,
        "payload_sha256": digest(payload_record),
    }


@dataclass(frozen=True, slots=True)
class NativeArtifactView:
    name: str
    bytes: int
    sha256: str
    media_type: str

    def __post_init__(self) -> None:
        try:
            _artifact_name(self.name)
        except NativeOperationError as error:
            raise NativeOperationError(str(error)) from error
        if type(self.bytes) is not int or self.bytes <= 0:
            raise NativeOperationError("artifact.bytes must be positive")
        _sha256(self.sha256, "artifact.sha256")
        _text(self.media_type, "artifact.media_type")

    def to_record(self) -> dict[str, object]:
        return {
            "name": self.name,
            "bytes": self.bytes,
            "sha256": self.sha256,
            "media_type": self.media_type,
        }

    @classmethod
    def from_record(cls, value: object) -> NativeArtifactView:
        if not isinstance(value, Mapping) or set(value) != {
            "name",
            "bytes",
            "sha256",
            "media_type",
        }:
            raise NativeOperationError("artifact fields are invalid")
        return cls(
            name=value["name"],
            bytes=value["bytes"],
            sha256=value["sha256"],
            media_type=value["media_type"],
        )


@dataclass(frozen=True, slots=True)
class NativeFailureView:
    code: str
    message: str

    def __post_init__(self) -> None:
        _text(self.code, "failure.code")
        _text(self.message, "failure.message")

    def to_record(self) -> dict[str, object]:
        return {"code": self.code, "message": self.message}

    @classmethod
    def from_record(cls, value: object) -> NativeFailureView:
        if not isinstance(value, Mapping) or set(value) != {"code", "message"}:
            raise NativeOperationError("failure fields are invalid")
        return cls(code=value["code"], message=value["message"])


@dataclass(frozen=True, slots=True)
class NativeOperationView:
    schema_version: str
    operation_id: str
    kind: str
    payload_sha256: str
    state: str
    request_sha256: str
    input_archive_sha256: str
    native_tool_requirement_sha256: str
    actual_native_tool_sha256: str | None
    owner_sha256: str | None
    executor_sha256: str | None
    resource_terminated: bool
    artifact: NativeArtifactView | None
    failure: NativeFailureView | None
    created_at: str
    started_at: str | None
    finished_at: str | None

    def __post_init__(self) -> None:
        if self.schema_version != OPERATION_VIEW_SCHEMA:
            raise NativeOperationError("Native operation view schema is unsupported")
        _operation_id(self.operation_id)
        if self.kind not in _KINDS:
            raise NativeOperationError("Native operation view kind is invalid")
        _sha256(self.payload_sha256, "payload_sha256")
        if self.state not in _STATES:
            raise NativeOperationError("Native operation view state is invalid")
        _sha256(self.request_sha256, "request_sha256")
        _sha256(self.input_archive_sha256, "input_archive_sha256")
        _sha256(
            self.native_tool_requirement_sha256,
            "native_tool_requirement_sha256",
        )
        _optional_sha256(self.actual_native_tool_sha256, "actual_native_tool_sha256")
        _optional_sha256(self.owner_sha256, "owner_sha256")
        _optional_sha256(self.executor_sha256, "executor_sha256")
        if type(self.resource_terminated) is not bool:
            raise NativeOperationError("resource_terminated must be boolean")
        if self.artifact is not None and not isinstance(self.artifact, NativeArtifactView):
            raise NativeOperationError("artifact must be an artifact view or null")
        if self.failure is not None and not isinstance(self.failure, NativeFailureView):
            raise NativeOperationError("failure must be a failure view or null")
        _timestamp(self.created_at, "created_at")
        _timestamp(self.started_at, "started_at", optional=True)
        _timestamp(self.finished_at, "finished_at", optional=True)
        if self.state == "satisfied":
            if self.artifact is None or not self.resource_terminated:
                raise NativeOperationError(
                    "A satisfied operation requires an artifact and terminated resource"
                )
            if self.failure is not None:
                raise NativeOperationError("A satisfied operation cannot carry a failure")
        if self.state not in {"failed", "cancelled", "unknown"} and self.failure is not None:
            raise NativeOperationError("Only a terminal failure state can carry failure details")
        if self.state in _TERMINAL_STATES and self.finished_at is None:
            raise NativeOperationError("A terminal operation requires finished_at")

    def to_record(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "operation_id": self.operation_id,
            "kind": self.kind,
            "payload_sha256": self.payload_sha256,
            "state": self.state,
            "request_sha256": self.request_sha256,
            "input_archive_sha256": self.input_archive_sha256,
            "native_tool_requirement_sha256": self.native_tool_requirement_sha256,
            "actual_native_tool_sha256": self.actual_native_tool_sha256,
            "owner_sha256": self.owner_sha256,
            "executor_sha256": self.executor_sha256,
            "resource_terminated": self.resource_terminated,
            "artifact": None if self.artifact is None else self.artifact.to_record(),
            "failure": None if self.failure is None else self.failure.to_record(),
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }

    @classmethod
    def from_record(cls, record: Mapping[str, object]) -> NativeOperationView:
        if not isinstance(record, Mapping):
            raise NativeOperationError("Native operation view must be an object")
        expected = {
            "schema_version",
            "operation_id",
            "kind",
            "payload_sha256",
            "state",
            "request_sha256",
            "input_archive_sha256",
            "native_tool_requirement_sha256",
            "actual_native_tool_sha256",
            "owner_sha256",
            "executor_sha256",
            "resource_terminated",
            "artifact",
            "failure",
            "created_at",
            "started_at",
            "finished_at",
        }
        if set(record) != expected:
            raise NativeOperationError(
                "Native operation view fields are incomplete or unknown",
                detail={
                    "missing": sorted(expected - set(record)),
                    "unknown": sorted(set(record) - expected),
                },
            )
        artifact = record["artifact"]
        failure = record["failure"]
        return cls(
            schema_version=record["schema_version"],
            operation_id=record["operation_id"],
            kind=record["kind"],
            payload_sha256=record["payload_sha256"],
            state=record["state"],
            request_sha256=record["request_sha256"],
            input_archive_sha256=record["input_archive_sha256"],
            native_tool_requirement_sha256=record["native_tool_requirement_sha256"],
            actual_native_tool_sha256=record["actual_native_tool_sha256"],
            owner_sha256=record["owner_sha256"],
            executor_sha256=record["executor_sha256"],
            resource_terminated=record["resource_terminated"],
            artifact=None if artifact is None else NativeArtifactView.from_record(artifact),
            failure=None if failure is None else NativeFailureView.from_record(failure),
            created_at=record["created_at"],
            started_at=record["started_at"],
            finished_at=record["finished_at"],
        )


@dataclass(frozen=True, slots=True)
class NativeOperationCreate:
    schema_version: str
    operation_id: str
    kind: str
    request: Mapping[str, object]
    request_sha256: str
    input_archive_sha256: str
    input_archive_bytes: int
    native_tool_requirement_sha256: str
    output_contract: str
    payload_sha256: str

    def __post_init__(self) -> None:
        if self.schema_version != OPERATION_SCHEMA:
            raise NativeOperationError("Native operation request schema is unsupported")
        _operation_id(self.operation_id)
        if self.kind not in _KINDS:
            raise NativeOperationError("Native operation kind must be discover or capture")
        if not isinstance(self.request, Mapping):
            raise NativeOperationError("Native operation request must be an object")
        request = copy.deepcopy(dict(self.request))
        try:
            canonical(request)
        except (TypeError, ValueError) as error:
            raise NativeOperationError("Native operation request is not canonical JSON") from error
        object.__setattr__(self, "request", request)
        if digest(request) != _sha256(self.request_sha256, "request_sha256"):
            raise NativeOperationError("request_sha256 differs from the canonical request")
        _sha256(self.input_archive_sha256, "input.archive_sha256")
        if type(self.input_archive_bytes) is not int or self.input_archive_bytes <= 0:
            raise NativeOperationError("input.archive_bytes must be positive")
        _sha256(
            self.native_tool_requirement_sha256,
            "native_tool.requirement_sha256",
        )
        _text(self.output_contract, "output_contract")
        expected_payload = digest(self.payload_record())
        if expected_payload != _sha256(self.payload_sha256, "payload_sha256"):
            raise NativeOperationError("payload_sha256 differs from the immutable operation intent")
        if self.kind == DISCOVER_KIND:
            try:
                discover = NativeDiscoverRequest.from_record(request)
            except NativeDiscoverRefusal as error:
                raise NativeOperationError(
                    error.message,
                    code="native_discover_request_invalid",
                    detail=error.detail,
                ) from error
            if discover.operation_id != self.operation_id:
                raise NativeOperationError(
                    "Discover request operation_id differs from the operation envelope"
                )
            if self.output_contract != NATIVE_OBSERVATION_SCHEMA:
                raise NativeOperationError(
                    "Discover output_contract must name the native observation schema"
                )
            if self.input_archive_bytes > MAX_HANDOFF_BYTES:
                raise NativeOperationError(
                    "Discover input archive exceeds the source-set transfer limit",
                    status=413,
                )

    def payload_record(self) -> dict[str, object]:
        return {
            "input_archive_bytes": self.input_archive_bytes,
            "input_archive_sha256": self.input_archive_sha256,
            "kind": self.kind,
            "native_tool_requirement_sha256": self.native_tool_requirement_sha256,
            "operation_id": self.operation_id,
            "output_contract": self.output_contract,
            "request": dict(self.request),
            "request_sha256": self.request_sha256,
            "schema_version": self.schema_version,
        }

    def to_record(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "operation_id": self.operation_id,
            "kind": self.kind,
            "request": dict(self.request),
            "request_sha256": self.request_sha256,
            "input": {
                "archive_sha256": self.input_archive_sha256,
                "archive_bytes": self.input_archive_bytes,
            },
            "native_tool": {"requirement_sha256": self.native_tool_requirement_sha256},
            "output_contract": self.output_contract,
            "payload_sha256": self.payload_sha256,
        }

    @classmethod
    def from_record(cls, record: Mapping[str, object]) -> NativeOperationCreate:
        if not isinstance(record, Mapping):
            raise NativeOperationError("Native operation request must be an object")
        expected = {
            "schema_version",
            "operation_id",
            "kind",
            "request",
            "request_sha256",
            "input",
            "native_tool",
            "output_contract",
            "payload_sha256",
        }
        if set(record) != expected:
            raise NativeOperationError(
                "Native operation request fields are incomplete or unknown",
                detail={
                    "missing": sorted(expected - set(record)),
                    "unknown": sorted(set(record) - expected),
                },
            )
        input_value = record["input"]
        tool = record["native_tool"]
        if not isinstance(input_value, Mapping) or set(input_value) != {
            "archive_sha256",
            "archive_bytes",
        }:
            raise NativeOperationError("input fields are invalid")
        if not isinstance(tool, Mapping) or set(tool) != {"requirement_sha256"}:
            raise NativeOperationError("native_tool fields are invalid")
        return cls(
            schema_version=record["schema_version"],
            operation_id=record["operation_id"],
            kind=record["kind"],
            request=record["request"],
            request_sha256=record["request_sha256"],
            input_archive_sha256=input_value["archive_sha256"],
            input_archive_bytes=input_value["archive_bytes"],
            native_tool_requirement_sha256=tool["requirement_sha256"],
            output_contract=record["output_contract"],
            payload_sha256=record["payload_sha256"],
        )


@dataclass(frozen=True, slots=True)
class NativeOperationResult:
    """A sealed file returned by one native executor."""

    artifact_path: Path
    artifact_name: str
    output_contract: str
    resource_terminated: bool

    def __post_init__(self) -> None:
        if not isinstance(self.artifact_path, Path):
            raise NativeOperationUnresolved(
                "native_result_invalid",
                "artifact_path must be a Path",
            )
        if not self.artifact_path.is_file() or self.artifact_path.is_symlink():
            raise NativeOperationUnresolved(
                "native_result_invalid",
                "artifact_path is not a regular file",
            )
        _artifact_name(self.artifact_name)
        _text(self.output_contract, "output_contract")
        if type(self.resource_terminated) is not bool:
            raise NativeOperationUnresolved(
                "native_result_invalid",
                "resource_terminated must be boolean",
            )


class NativeOperationExecutor(Protocol):
    def __call__(
        self,
        request: Mapping[str, object],
        source_archive: Path,
        workspace: Path,
        native_tool: Mapping[str, object],
    ) -> NativeOperationResult: ...


@dataclass(frozen=True, slots=True)
class DiscoverOperationExecutor:
    """The production Discover operation over the existing native core."""

    selection_reader_factory: Callable | None = None
    backend_factory: Callable | None = None
    cad_ownership_file: Path | None = None
    on_activity: Callable | None = None

    def __call__(
        self,
        request: Mapping[str, object],
        source_archive: Path,
        workspace: Path,
        native_tool: Mapping[str, object],
    ) -> NativeOperationResult:
        native_request = NativeDiscoverRequest.from_record(request)
        try:
            observation = run_windows_discover(
                source_archive,
                native_request,
                workspace / "discover",
                native_tool=native_tool,
                selection_reader_factory=self.selection_reader_factory,
                backend_factory=self.backend_factory,
                cad_ownership_file=self.cad_ownership_file,
                on_activity=self.on_activity,
            )
        except NativeDiscoverRefusal as error:
            raise NativeOperationFailure(
                error.code,
                error.message,
                resource_terminated=True,
                detail=error.detail,
            ) from error
        except Exception as error:
            raise NativeOperationUnresolved(
                "discover.native_unknown",
                f"{type(error).__name__}: {error}",
            ) from error
        try:
            artifact = seal_observation_bundle(
                observation,
                workspace / "native-observation.zip",
            )
        except Exception as error:
            raise NativeOperationFailure(
                "discover.observation_seal_failed",
                f"{type(error).__name__}: {error}",
                resource_terminated=True,
            ) from error
        return NativeOperationResult(
            artifact_path=artifact,
            artifact_name="native-observation.zip",
            output_contract=NATIVE_OBSERVATION_SCHEMA,
            resource_terminated=True,
        )


class NativeOperationRuntime:
    """One authenticated Windows operation server over append-only SQLite facts."""

    def __init__(
        self,
        *,
        state_root: Path,
        native_tool_factory: Callable[[], Mapping[str, object]],
        executors: Mapping[str, NativeOperationExecutor],
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.state_root = Path(state_root).resolve()
        self.state_root.mkdir(parents=True, exist_ok=True)
        self._ownership = acquire_process_lock(self.state_root / ".native-operation.lock")
        self.runtime_root = self.state_root / "native-operations"
        self.db_path = self.runtime_root / "operations.sqlite3"
        self.input_root = self.runtime_root / "inputs"
        self.artifact_root = self.runtime_root / "artifacts"
        self.workspace_root = self.runtime_root / "work"
        for root in (self.input_root, self.artifact_root, self.workspace_root):
            root.mkdir(parents=True, exist_ok=True)
        if not callable(native_tool_factory):
            raise TypeError("native_tool_factory must be callable")
        if any(kind not in _KINDS for kind in executors):
            raise ValueError("executor registry contains an unknown native operation kind")
        self.native_tool_factory = native_tool_factory
        self.executors = dict(executors)
        self.clock = clock or (lambda: datetime.now(UTC))
        self._lock = threading.RLock()
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._resource_waiters: set[str] = set()
        self._stopping = False
        self._closed = False
        try:
            self._init_db()
            self.install_id = self._install_id()
            self._recover()
            self._thread = threading.Thread(
                target=self._work,
                name="native-operation-worker",
                daemon=True,
            )
            self._thread.start()
        except BaseException:
            os.close(self._ownership)
            raise

    @property
    def active(self) -> bool:
        return self._thread.is_alive() and not self._stopping

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def _init_db(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS native_operation (
                    operation_id TEXT PRIMARY KEY,
                    payload_sha256 TEXT NOT NULL UNIQUE,
                    kind TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    request_sha256 TEXT NOT NULL,
                    input_archive_sha256 TEXT NOT NULL,
                    input_archive_bytes INTEGER NOT NULL,
                    native_tool_requirement_sha256 TEXT NOT NULL,
                    output_contract TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS native_operation_event (
                    operation_id TEXT NOT NULL REFERENCES native_operation(operation_id),
                    seq INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY (operation_id, seq)
                );
                CREATE TABLE IF NOT EXISTS native_resource_lease (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    operation_id TEXT NOT NULL REFERENCES native_operation(operation_id),
                    claim_id TEXT NOT NULL,
                    fence INTEGER NOT NULL,
                    acquired_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS native_install (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    install_id TEXT NOT NULL
                );
                """
            )

    def _install_id(self) -> str:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT install_id FROM native_install WHERE singleton=1"
            ).fetchone()
            if row is not None:
                return str(row[0])
            value = "win-" + uuid.uuid4().hex
            connection.execute(
                "INSERT INTO native_install(singleton, install_id) VALUES(1, ?)",
                (value,),
            )
            return value

    def _now(self) -> str:
        value = self.clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise NativeOperationUnresolved(
                "clock.invalid",
                "Clock must return a timezone-aware datetime",
            )
        return value.astimezone(UTC).isoformat()

    @staticmethod
    def _decode_events(rows) -> list[dict]:
        return [
            {
                "seq": int(row[0]),
                "type": str(row[1]),
                "at": str(row[2]),
                "payload": json.loads(row[3]),
            }
            for row in rows
        ]

    def _events(self, operation_id: str) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT seq, event_type, observed_at, payload_json "
                "FROM native_operation_event WHERE operation_id=? ORDER BY seq",
                (operation_id,),
            ).fetchall()
        return self._decode_events(rows)

    def _events_in_connection(
        self,
        connection: sqlite3.Connection,
        operation_id: str,
    ) -> list[dict]:
        return self._decode_events(
            connection.execute(
                "SELECT seq, event_type, observed_at, payload_json "
                "FROM native_operation_event WHERE operation_id=? ORDER BY seq",
                (operation_id,),
            ).fetchall()
        )

    def _append_in_connection(
        self,
        connection: sqlite3.Connection,
        operation_id: str,
        event_type: str,
        payload: Mapping[str, object],
        *,
        observed_at: str | None = None,
    ) -> dict:
        row = connection.execute(
            "SELECT COALESCE(MAX(seq), 0) FROM native_operation_event WHERE operation_id=?",
            (operation_id,),
        ).fetchone()
        seq = int(row[0]) + 1
        at = observed_at or self._now()
        body = copy.deepcopy(dict(payload))
        connection.execute(
            "INSERT INTO native_operation_event("
            "operation_id, seq, event_type, observed_at, payload_json"
            ") VALUES(?, ?, ?, ?, ?)",
            (
                operation_id,
                seq,
                event_type,
                at,
                canonical(body).decode("utf-8"),
            ),
        )
        return {"seq": seq, "type": event_type, "at": at, "payload": body}

    def _append(
        self,
        operation_id: str,
        event_type: str,
        payload: Mapping[str, object],
    ) -> dict:
        with self._lock, self._connect() as connection:
            return self._append_in_connection(
                connection,
                operation_id,
                event_type,
                payload,
            )

    def create(self, record: Mapping[str, object]) -> dict:
        intent = NativeOperationCreate.from_record(record)
        with self._lock, self._connect() as connection:
            if self._stopping:
                raise NativeOperationError(
                    "The native operation runtime is closing",
                    status=503,
                    code="native_operation_closing",
                )
            row = connection.execute(
                "SELECT payload_sha256 FROM native_operation WHERE operation_id=?",
                (intent.operation_id,),
            ).fetchone()
            if row is not None:
                if str(row[0]) != intent.payload_sha256:
                    raise NativeOperationConflict(intent.operation_id)
            else:
                created_at = self._now()
                connection.execute(
                    """
                    INSERT INTO native_operation(
                        operation_id, payload_sha256, kind, request_json,
                        request_sha256, input_archive_sha256,
                        input_archive_bytes, native_tool_requirement_sha256,
                        output_contract, created_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        intent.operation_id,
                        intent.payload_sha256,
                        intent.kind,
                        canonical(dict(intent.request)).decode("utf-8"),
                        intent.request_sha256,
                        intent.input_archive_sha256,
                        intent.input_archive_bytes,
                        intent.native_tool_requirement_sha256,
                        intent.output_contract,
                        created_at,
                    ),
                )
                self._append_in_connection(
                    connection,
                    intent.operation_id,
                    "accepted",
                    {"payload_sha256": intent.payload_sha256},
                    observed_at=created_at,
                )
        return self.view(intent.operation_id)

    def _intent(self, operation_id: str) -> tuple[sqlite3.Row, NativeOperationCreate]:
        canonical_id = _operation_id(operation_id)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM native_operation WHERE operation_id=?",
                (canonical_id,),
            ).fetchone()
        if row is None:
            raise NativeOperationError(
                "Native operation was not found",
                status=404,
                code="native_operation_not_found",
            )
        record = {
            "schema_version": OPERATION_SCHEMA,
            "operation_id": row["operation_id"],
            "kind": row["kind"],
            "request": json.loads(row["request_json"]),
            "request_sha256": row["request_sha256"],
            "input": {
                "archive_sha256": row["input_archive_sha256"],
                "archive_bytes": row["input_archive_bytes"],
            },
            "native_tool": {
                "requirement_sha256": row["native_tool_requirement_sha256"]
            },
            "output_contract": row["output_contract"],
            "payload_sha256": row["payload_sha256"],
        }
        return row, NativeOperationCreate.from_record(record)

    def _input_path(self, operation_id: str, archive_sha256: str) -> Path:
        return self.input_root / operation_id / f"{archive_sha256}.zip"

    def _admitted_input_matches(
        self,
        operation_id: str,
        intent: NativeOperationCreate,
    ) -> bool:
        path = self._input_path(operation_id, intent.input_archive_sha256)
        return (
            path.is_file()
            and not path.is_symlink()
            and path.stat().st_size == intent.input_archive_bytes
            and file_digest(path) == intent.input_archive_sha256
        )

    def admit_input(self, operation_id: str, archive_path: str | Path) -> dict:
        canonical_id = _operation_id(operation_id)
        source = Path(archive_path)
        with self._lock:
            _row, intent = self._intent(canonical_id)
            if not source.is_file() or source.is_symlink():
                raise NativeOperationError(
                    "Input archive is missing",
                    status=404,
                    code="input_archive_missing",
                )
            if (
                source.stat().st_size != intent.input_archive_bytes
                or file_digest(source) != intent.input_archive_sha256
            ):
                raise NativeOperationError(
                    "Input archive differs from its admitted identity",
                    status=409,
                    code="input_archive_mismatch",
                )
            events = self._events(canonical_id)
            types = {event["type"] for event in events}
            if "input_admitted" in types:
                if not self._admitted_input_matches(canonical_id, intent):
                    raise NativeOperationError(
                        "The admitted input archive is missing or changed",
                        status=409,
                        code="input_archive_storage_changed",
                    )
                return self.view(canonical_id)
            if types & _TERMINAL_STATES:
                raise NativeOperationError(
                    "Native operation is already terminal",
                    status=409,
                    code="native_operation_terminal",
                )
            if self._stopping:
                raise NativeOperationError(
                    "The native operation runtime is closing",
                    status=503,
                    code="native_operation_closing",
                )
            destination = self._input_path(
                canonical_id,
                intent.input_archive_sha256,
            )
            destination.parent.mkdir(parents=True, exist_ok=True)
            if not (
                destination.is_file()
                and not destination.is_symlink()
                and destination.stat().st_size == intent.input_archive_bytes
                and file_digest(destination) == intent.input_archive_sha256
            ):
                temporary = destination.with_name(
                    "." + destination.name + "." + uuid.uuid4().hex
                )
                try:
                    with source.open("rb") as input_stream, temporary.open(
                        "xb"
                    ) as output_stream:
                        shutil.copyfileobj(
                            input_stream,
                            output_stream,
                            length=1024 * 1024,
                        )
                        output_stream.flush()
                        os.fsync(output_stream.fileno())
                    if (
                        temporary.stat().st_size != intent.input_archive_bytes
                        or file_digest(temporary) != intent.input_archive_sha256
                    ):
                        raise NativeOperationError(
                            "Input changed while it was admitted",
                            status=409,
                            code="input_archive_mismatch",
                        )
                    os.replace(temporary, destination)
                finally:
                    temporary.unlink(missing_ok=True)
            with self._connect() as connection:
                current = connection.execute(
                    "SELECT 1 FROM native_operation_event "
                    "WHERE operation_id=? AND event_type='input_admitted'",
                    (canonical_id,),
                ).fetchone()
                if current is None:
                    self._append_in_connection(
                        connection,
                        canonical_id,
                        "input_admitted",
                        {"archive_sha256": intent.input_archive_sha256},
                    )
            self._queue.put(canonical_id)
        return self.view(canonical_id)

    def _recover(self) -> None:
        with self._lock, self._connect() as connection:
            operations = connection.execute(
                "SELECT operation_id FROM native_operation ORDER BY created_at, operation_id"
            ).fetchall()
            lease = connection.execute(
                "SELECT operation_id FROM native_resource_lease WHERE singleton=1"
            ).fetchone()
        lease_owner = None if lease is None else str(lease[0])
        for (operation_id,) in operations:
            operation_id = str(operation_id)
            events = self._events(operation_id)
            types = {event["type"] for event in events}
            if types & _TERMINAL_STATES:
                continue
            if "execution_started" in types:
                self._settle(
                    operation_id,
                    "unknown",
                    {
                        "code": "native_operation_restart_unknown",
                        "message": "Process restarted after native execution started",
                    },
                )
                continue
            if lease_owner == operation_id:
                with self._lock, self._connect() as connection:
                    connection.execute(
                        "DELETE FROM native_resource_lease "
                        "WHERE singleton=1 AND operation_id=?",
                        (operation_id,),
                    )
                    self._append_in_connection(
                        connection,
                        operation_id,
                        "claim_aborted",
                        {
                            "reason": "process_restarted_before_execution_started"
                        },
                    )
            if "input_admitted" in types:
                self._queue.put(operation_id)

    def _claim_and_start(
        self,
        operation_id: str,
        *,
        claim_id: str,
        fence: int,
        owner_sha256: str,
    ) -> bool:
        with self._lock, self._connect() as connection:
            current = connection.execute(
                "SELECT operation_id FROM native_resource_lease WHERE singleton=1"
            ).fetchone()
            if current is not None:
                return False
            acquired_at = self._now()
            connection.execute(
                "INSERT INTO native_resource_lease("
                "singleton, operation_id, claim_id, fence, acquired_at"
                ") VALUES(1, ?, ?, ?, ?)",
                (operation_id, claim_id, fence, acquired_at),
            )
            claim = {
                "claim_id": claim_id,
                "fence": fence,
                "owner_sha256": owner_sha256,
            }
            self._append_in_connection(
                connection,
                operation_id,
                "claimed",
                claim,
                observed_at=acquired_at,
            )
            self._append_in_connection(
                connection,
                operation_id,
                "resource_acquired",
                {"claim_id": claim_id, "fence": fence},
                observed_at=acquired_at,
            )
            self._append_in_connection(
                connection,
                operation_id,
                "execution_started",
                {"claim_id": claim_id, "fence": fence},
                observed_at=acquired_at,
            )
            return True

    def _settle(
        self,
        operation_id: str,
        state: str,
        payload: Mapping[str, object],
        *,
        release_resource: bool = False,
        release_reason: str | None = None,
    ) -> bool:
        if state not in _TERMINAL_STATES:
            raise ValueError("settlement state must be terminal")
        waiter_ids: list[str] = []
        with self._lock, self._connect() as connection:
            events = self._events_in_connection(connection, operation_id)
            types = {event["type"] for event in events}
            final_state = state
            final_payload = copy.deepcopy(dict(payload))
            if (
                state in {"satisfied", "failed"}
                and "cancel_requested" in types
                and "unknown" not in types
            ):
                final_state = "unknown"
                final_payload = {
                    "code": "native_cancel_after_send",
                    "message": "Cancellation arrived after native execution started",
                }
            if release_resource:
                lease = connection.execute(
                    "SELECT operation_id FROM native_resource_lease WHERE singleton=1"
                ).fetchone()
                if lease is not None and str(lease[0]) == operation_id:
                    connection.execute(
                        "DELETE FROM native_resource_lease WHERE singleton=1"
                    )
                    self._append_in_connection(
                        connection,
                        operation_id,
                        "resource_released",
                        {"reason": release_reason or "native_terminal"},
                    )
                    waiter_ids = sorted(self._resource_waiters)
                    self._resource_waiters.clear()
            if types & _TERMINAL_STATES:
                appended = False
            else:
                self._append_in_connection(
                    connection,
                    operation_id,
                    final_state,
                    final_payload,
                )
                appended = True
        for waiting_id in waiter_ids:
            self._queue.put(waiting_id)
        return appended

    def _install_artifact(
        self,
        operation_id: str,
        result: NativeOperationResult,
    ) -> NativeArtifactView:
        source_sha256 = file_digest(result.artifact_path)
        named_id = digest(
            {
                "artifact_sha256": source_sha256,
                "operation_id": operation_id,
            }
        )
        directory = self.artifact_root / operation_id
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"{named_id}.zip"
        if not target.exists():
            temporary = directory / f".{named_id}.{uuid.uuid4().hex}"
            try:
                with result.artifact_path.open("rb") as source, temporary.open(
                    "xb"
                ) as destination:
                    shutil.copyfileobj(source, destination, length=1024 * 1024)
                    destination.flush()
                    os.fsync(destination.fileno())
                if file_digest(temporary) != source_sha256:
                    raise NativeOperationUnresolved(
                        "native_result_changed",
                        "Native artifact changed while it was sealed",
                    )
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
        actual_sha256 = file_digest(target)
        if actual_sha256 != source_sha256:
            raise NativeOperationUnresolved(
                "native_result_changed",
                "Stored native artifact differs from the executor result",
            )
        artifact = NativeArtifactView(
            name=result.artifact_name,
            bytes=target.stat().st_size,
            sha256=actual_sha256,
            media_type="application/zip",
        )
        self._append(
            operation_id,
            "artifact_sealed",
            {
                **artifact.to_record(),
                "path": str(target),
                "output_contract": result.output_contract,
            },
        )
        return artifact

    def _execute(self, operation_id: str) -> None:
        _row, intent = self._intent(operation_id)
        events = self._events(operation_id)
        types = {event["type"] for event in events}
        if types & _TERMINAL_STATES:
            return
        if self._stopping:
            return
        if "cancel_requested" in types:
            self._settle(
                operation_id,
                "cancelled",
                {
                    "code": "native_operation_cancelled",
                    "message": "Cancellation completed before native execution",
                },
            )
            return
        if "input_admitted" not in types:
            return
        if not self._admitted_input_matches(operation_id, intent):
            self._settle(
                operation_id,
                "failed",
                {
                    "code": "input_archive_storage_changed",
                    "message": "The admitted input archive is missing or changed",
                    "resource_terminated": True,
                },
            )
            return
        executor = self.executors.get(intent.kind)
        if executor is None:
            self._settle(
                operation_id,
                "failed",
                {
                    "code": "native_operation_executor_unavailable",
                    "message": f"No executor is configured for {intent.kind!r}",
                    "resource_terminated": True,
                },
            )
            return
        try:
            native_tool = copy.deepcopy(dict(self.native_tool_factory()))
            actual_tool_sha256 = digest(native_tool)
        except Exception as error:
            self._settle(
                operation_id,
                "failed",
                {
                    "code": "native_tool_unavailable",
                    "message": f"{type(error).__name__}: {error}",
                    "resource_terminated": True,
                },
            )
            return
        self._append(
            operation_id,
            "tool_observed",
            {
                "actual_native_tool": native_tool,
                "actual_native_tool_sha256": actual_tool_sha256,
            },
        )
        if actual_tool_sha256 != intent.native_tool_requirement_sha256:
            self._settle(
                operation_id,
                "failed",
                {
                    "code": "native_tool_mismatch",
                    "message": "Installed native tool differs from the operation requirement",
                    "resource_terminated": True,
                },
            )
            return
        claim_id = "claim-" + uuid.uuid4().hex
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM native_operation_event "
                "WHERE operation_id=? AND event_type='claimed'",
                (operation_id,),
            ).fetchone()
        fence = int(row[0]) + 1
        owner_sha256 = digest(
            {"claim_id": claim_id, "install_id": self.install_id}
        )
        workspace = self.workspace_root / operation_id / claim_id
        workspace.mkdir(parents=True, exist_ok=False)
        if not self._claim_and_start(
            operation_id,
            claim_id=claim_id,
            fence=fence,
            owner_sha256=owner_sha256,
        ):
            shutil.rmtree(workspace, ignore_errors=True)
            with self._lock:
                self._resource_waiters.add(operation_id)
            return
        try:
            try:
                result = executor(
                    dict(intent.request),
                    self._input_path(
                        operation_id,
                        intent.input_archive_sha256,
                    ),
                    workspace,
                    native_tool,
                )
            except NativeOperationFailure as failure:
                self._settle(
                    operation_id,
                    "failed",
                    {
                        "code": failure.code,
                        "message": str(failure),
                        "detail": failure.detail,
                    },
                    release_resource=failure.resource_terminated,
                    release_reason="native_definite_failure",
                )
                return
            except NativeOperationUnresolved as error:
                self._settle(
                    operation_id,
                    "unknown",
                    {
                        "code": error.code,
                        "message": str(error),
                        "detail": error.detail,
                    },
                    release_resource=error.resource_terminated,
                    release_reason="native_unresolved_cleanup",
                )
                return
            except Exception as error:
                self._settle(
                    operation_id,
                    "unknown",
                    {
                        "code": "native_operation_exception",
                        "message": f"{type(error).__name__}: {error}",
                    },
                )
                return
            if result.output_contract != intent.output_contract:
                settlement_payload = {
                    "code": "native_output_contract_mismatch",
                    "message": "Native executor returned a different output contract",
                    "actual": result.output_contract,
                }
                if result.resource_terminated:
                    self._settle(
                        operation_id,
                        "unknown",
                        settlement_payload,
                        release_resource=True,
                        release_reason="native_output_contract_mismatch",
                    )
                else:
                    self._settle(operation_id, "unknown", settlement_payload)
                return
            try:
                artifact = self._install_artifact(operation_id, result)
            except Exception as error:
                settlement_payload = {
                    "code": "native_artifact_seal_failed",
                    "message": f"{type(error).__name__}: {error}",
                }
                if result.resource_terminated:
                    self._settle(
                        operation_id,
                        "unknown",
                        settlement_payload,
                        release_resource=True,
                        release_reason="native_artifact_seal_failed",
                    )
                else:
                    self._settle(operation_id, "unknown", settlement_payload)
                return
            if result.resource_terminated:
                self._settle(
                    operation_id,
                    "satisfied",
                    {"artifact_sha256": artifact.sha256},
                    release_resource=True,
                    release_reason="native_terminal",
                )
            else:
                self._settle(
                    operation_id,
                    "unknown",
                    {
                        "code": "native_resource_not_terminated",
                        "message": "Native executor did not prove resource termination",
                        "artifact_sha256": artifact.sha256,
                    },
                )
        finally:
            shutil.rmtree(workspace, ignore_errors=True)

    def _work(self) -> None:
        while True:
            operation_id = self._queue.get()
            try:
                if operation_id is None:
                    return
                try:
                    self._execute(operation_id)
                except Exception as error:
                    events = self._events(operation_id)
                    if not {event["type"] for event in events} & _TERMINAL_STATES:
                        self._settle(
                            operation_id,
                            "unknown",
                            {
                                "code": "native_worker_exception",
                                "message": f"{type(error).__name__}: {error}",
                            },
                        )
            finally:
                self._queue.task_done()

    def cancel(self, operation_id: str) -> dict:
        canonical_id = _operation_id(operation_id)
        _self_intent = self._intent(canonical_id)
        del _self_intent
        with self._lock, self._connect() as connection:
            events = self._events_in_connection(connection, canonical_id)
            types = {event["type"] for event in events}
            if types & _TERMINAL_STATES:
                pass
            else:
                if "cancel_requested" not in types:
                    self._append_in_connection(
                        connection,
                        canonical_id,
                        "cancel_requested",
                        {},
                    )
                if "execution_started" in types:
                    if "unknown" not in types:
                        self._append_in_connection(
                            connection,
                            canonical_id,
                            "unknown",
                            {
                                "code": "native_cancel_after_send",
                                "message": (
                                    "Cancellation arrived after native execution "
                                    "started; the result remains unknown"
                                ),
                            },
                        )
                elif "cancelled" not in types:
                    self._append_in_connection(
                        connection,
                        canonical_id,
                        "cancelled",
                        {
                            "code": "native_operation_cancelled",
                            "message": "Cancellation completed before native execution",
                        },
                    )
        return self.view(canonical_id)

    def reconcile_resource_termination(
        self,
        operation_id: str,
        *,
        evidence_sha256: str,
        reason: str,
    ) -> dict:
        """Release one unknown lease only after a trusted external termination fact."""

        canonical_id = _operation_id(operation_id)
        evidence = _sha256(evidence_sha256, "evidence_sha256")
        reason_text = _text(reason, "reason")
        waiter_ids: list[str] = []
        with self._lock, self._connect() as connection:
            events = self._events_in_connection(connection, canonical_id)
            types = {event["type"] for event in events}
            if "unknown" not in types:
                raise NativeOperationError(
                    "Only an unknown operation can be reconciled",
                    status=409,
                    code="native_resource_reconcile_invalid",
                )
            lease = connection.execute(
                "SELECT operation_id FROM native_resource_lease WHERE singleton=1"
            ).fetchone()
            if lease is None or str(lease[0]) != canonical_id:
                if "resource_released" in types:
                    return self.view(canonical_id)
                raise NativeOperationError(
                    "The unknown operation does not own the resource lease",
                    status=409,
                    code="native_resource_reconcile_invalid",
                )
            self._append_in_connection(
                connection,
                canonical_id,
                "resource_termination_observed",
                {"evidence_sha256": evidence, "reason": reason_text},
            )
            connection.execute("DELETE FROM native_resource_lease WHERE singleton=1")
            self._append_in_connection(
                connection,
                canonical_id,
                "resource_released",
                {"reason": reason_text},
            )
            waiter_ids = sorted(self._resource_waiters)
            self._resource_waiters.clear()
        for waiting_id in waiter_ids:
            self._queue.put(waiting_id)
        return self.view(canonical_id)

    def _project(self, operation_id: str) -> NativeOperationView:
        row, intent = self._intent(operation_id)
        events = self._events(operation_id)
        types = {event["type"] for event in events}
        state = "accepted"
        failure = None
        artifact = None
        resource_terminated = False
        actual_native_tool_sha256 = None
        owner_sha256 = None
        executor_sha256 = None
        started_at = None
        finished_at = None
        if "input_admitted" in types:
            state = "awaiting_input"
        if "claimed" in types or "execution_started" in types:
            state = "running"
        for event in events:
            payload = event["payload"]
            if event["type"] == "tool_observed":
                actual_native_tool_sha256 = payload.get(
                    "actual_native_tool_sha256"
                )
                executor_sha256 = actual_native_tool_sha256
            elif event["type"] == "claimed":
                owner_sha256 = payload.get("owner_sha256")
            elif event["type"] == "execution_started":
                started_at = event["at"]
            elif event["type"] == "artifact_sealed":
                artifact = NativeArtifactView(
                    name=payload["name"],
                    bytes=payload["bytes"],
                    sha256=payload["sha256"],
                    media_type=payload["media_type"],
                )
            elif event["type"] == "resource_released":
                resource_terminated = True
            elif event["type"] in _TERMINAL_STATES:
                state = event["type"]
                finished_at = event["at"]
                resource_terminated = (
                    resource_terminated
                    or payload.get("resource_terminated") is True
                )
                if event["type"] in {"failed", "cancelled", "unknown"}:
                    failure = NativeFailureView(
                        code=_single_line(
                            payload.get("code")
                            or "native_operation_terminal",
                            limit=128,
                        ),
                        message=_single_line(
                            payload.get("message")
                            or payload.get("reason")
                            or payload.get("code")
                            or event["type"]
                        ),
                    )
        return NativeOperationView(
            schema_version=OPERATION_VIEW_SCHEMA,
            operation_id=operation_id,
            kind=intent.kind,
            payload_sha256=intent.payload_sha256,
            state=state,
            request_sha256=intent.request_sha256,
            input_archive_sha256=intent.input_archive_sha256,
            native_tool_requirement_sha256=intent.native_tool_requirement_sha256,
            actual_native_tool_sha256=actual_native_tool_sha256,
            owner_sha256=owner_sha256,
            executor_sha256=executor_sha256,
            resource_terminated=resource_terminated,
            artifact=artifact,
            failure=failure,
            created_at=str(row["created_at"]),
            started_at=started_at,
            finished_at=finished_at,
        )

    def view(self, operation_id: str) -> dict:
        return self._project(_operation_id(operation_id)).to_record()

    def artifact_path(self, operation_id: str) -> tuple[Path, dict]:
        canonical_id = _operation_id(operation_id)
        view = self._project(canonical_id)
        if view.state != "satisfied" or view.artifact is None:
            raise NativeOperationError(
                "The native operation has no satisfied artifact",
                status=409,
                code="native_artifact_unavailable",
            )
        sealed = next(
            event
            for event in self._events(canonical_id)
            if event["type"] == "artifact_sealed"
        )
        path = Path(sealed["payload"]["path"])
        if (
            not path.is_file()
            or path.is_symlink()
            or file_digest(path) != sealed["payload"]["sha256"]
        ):
            raise NativeOperationError(
                "The sealed artifact is missing or changed",
                status=409,
                code="native_artifact_changed",
            )
        return path, view.artifact.to_record()

    def stats(self) -> dict[str, int]:
        views = [
            self._project(str(row[0])) for row in self._operation_ids()
        ]
        return {
            "active": sum(view.state == "running" for view in views),
            "queued": sum(
                view.state in {"accepted", "awaiting_input"} for view in views
            ),
            "unresolved": sum(view.state == "unknown" for view in views),
        }

    def _operation_ids(self) -> list[tuple[str]]:
        with self._connect() as connection:
            return [
                (str(row[0]),)
                for row in connection.execute(
                    "SELECT operation_id FROM native_operation "
                    "ORDER BY created_at, operation_id"
                )
            ]

    def close(self) -> None:
        if self._closed:
            return
        if not self._stopping:
            self._stopping = True
            self._queue.put(None)
        self._queue.join()
        self._thread.join(timeout=30)
        if self._thread.is_alive():
            raise NativeOperationUnresolved(
                "native_worker_still_running",
                "The native worker did not terminate; endpoint ownership remains held",
            )
        os.close(self._ownership)
        self._closed = True


__all__ = [
    "CAPTURE_KIND",
    "DISCOVER_KIND",
    "OPERATION_SCHEMA",
    "OPERATION_VIEW_SCHEMA",
    "DiscoverOperationExecutor",
    "NativeArtifactView",
    "NativeFailureView",
    "NativeOperationConflict",
    "NativeOperationCreate",
    "NativeOperationError",
    "NativeOperationExecutor",
    "NativeOperationFailure",
    "NativeOperationResult",
    "NativeOperationRuntime",
    "NativeOperationUnresolved",
    "NativeOperationView",
    "build_operation_record",
]
