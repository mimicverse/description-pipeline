"""Strict client and Discover adapter for Windows native operations."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import http.client
import json
import os
import re
import shutil
import tempfile
import threading
import time
import uuid
import zipfile
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field as dataclass_field
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest

from ..io import PipelineError, digest, file_digest, parse_data, read_data, write_json
from ..native_transfer import (
    CAPTURE_MANIFEST,
    CAPTURE_TRANSFER_SCHEMA,
    MAX_MANIFEST_BYTES,
)
from ..schemas import (
    NATIVE_CAPTURE_EFFECT_SCHEMA,
    NATIVE_OPERATION_OBSERVATION_SCHEMA,
)
from ..sources.solidworks.discover_stage import (
    NATIVE_OBSERVATION_SCHEMA,
    NativeDiscoverRefusal,
    NativeDiscoverRequest,
    NativeObservation,
    admit_native_observation,
)
from .discover_protocol import (
    OBSERVATION_ARCHIVE,
    SOURCE_ARCHIVE,
    admit_observation_bundle,
    seal_source_archive,
)
from .native_operation_runtime import (
    CAPTURE_KIND,
    CAPTURE_OPERATION_SCHEMA,
    DISCOVER_KIND,
    CaptureOperationRequest,
    NativeOperationCreate,
    NativeOperationError,
    NativeOperationView,
    build_operation_record,
)
from .native_operation_server import NATIVE_TOOL_IDENTITY_SCHEMA
from .stage_transfer import CAPTURE_ARCHIVE, admit_capture

if TYPE_CHECKING:
    from ..pipeline.contracts import StageCommand
    from ..pipeline.effects import EffectIntent
    from ..pipeline.reconciliation import EffectReconciliationEvidence

MAX_OPERATION_JSON_BYTES = 8 * 1024**2
MAX_OPERATION_ARCHIVE_BYTES = 16 * 1024**3
_LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})
_TERMINAL_STATES = frozenset({"satisfied", "failed", "cancelled", "unknown"})
_KINDS = frozenset({DISCOVER_KIND, CAPTURE_KIND})
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


class _NoRedirect(urlrequest.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        del req, fp, code, msg, headers, newurl
        return None


def _internal_opener(request: urlrequest.Request, *, timeout: float):
    opener = urlrequest.build_opener(
        urlrequest.ProxyHandler({}),
        _NoRedirect(),
    )
    return opener.open(request, timeout=timeout)


class NativeOperationClientError(RuntimeError):
    """An authenticated native operation endpoint refused a request."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        code: str | None = None,
        detail: Mapping[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.detail = dict(detail or {})


class NativeOperationClientConflict(NativeOperationClientError):
    """The same operation identity is bound to different immutable content."""


class NativeOperationNotFound(NativeOperationClientError):
    """The native operation identity is not known to the endpoint."""


class NativeOperationProtocolError(NativeOperationClientError):
    """The endpoint returned an invalid or inconsistent wire record."""


class NativeOperationRemoteUnknown(NativeOperationClientError):
    """The remote side-effect result requires reconciliation."""


class NativeOperationWaitTimeout(NativeOperationClientError):
    """The operation has not reached a terminal fact before the client deadline."""


@dataclass(frozen=True, slots=True)
class NativeOperationEndpoint:
    base_url: str
    token: str = dataclass_field(repr=False)
    timeout: float = 30.0

    def __post_init__(self) -> None:
        if not isinstance(self.token, str) or len(self.token) < 32:
            raise ValueError("token must contain at least 32 characters")
        if not self.token.isascii() or any(character.isspace() for character in self.token):
            raise ValueError("token must contain non-whitespace ASCII characters")
        parsed = urlparse.urlsplit(self.base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ValueError("base_url must be one HTTP(S) origin without credentials or a path")
        if parsed.scheme == "http" and parsed.hostname not in _LOOPBACK:
            raise ValueError("plaintext HTTP is allowed only for loopback native operation endpoints")
        if type(self.timeout) not in {int, float} or self.timeout <= 0:
            raise ValueError("timeout must be positive")
        object.__setattr__(self, "base_url", f"{parsed.scheme}://{parsed.netloc}")


@dataclass(frozen=True, slots=True)
class ArtifactDownload:
    operation_id: str
    path: Path
    name: str
    bytes: int
    sha256: str
    media_type: str


@dataclass(frozen=True, slots=True)
class NativeOperationStatusExpectation:
    """The immutable remote-operation identity that a status probe must match."""

    operation_id: str
    kind: str
    request_sha256: str
    native_tool_requirement_sha256: str
    input_archive_sha256: str
    payload_sha256: str

    def __post_init__(self) -> None:
        NativeOperationClient._operation_id(self.operation_id)
        if self.kind not in _KINDS:
            raise ValueError("kind is not a native operation kind")
        for name in (
            "request_sha256",
            "native_tool_requirement_sha256",
            "input_archive_sha256",
            "payload_sha256",
        ):
            value = getattr(self, name)
            if (
                not isinstance(value, str)
                or len(value) != 64
                or any(character not in "0123456789abcdef" for character in value)
            ):
                raise ValueError(f"{name} must be a lowercase SHA-256 digest")

    @classmethod
    def from_create(cls, intent: NativeOperationCreate) -> NativeOperationStatusExpectation:
        if not isinstance(intent, NativeOperationCreate):
            raise TypeError("intent must be a NativeOperationCreate")
        return cls(
            operation_id=intent.operation_id,
            kind=intent.kind,
            request_sha256=intent.request_sha256,
            native_tool_requirement_sha256=intent.native_tool_requirement_sha256,
            input_archive_sha256=intent.input_archive_sha256,
            payload_sha256=intent.payload_sha256,
        )

    def require(self, view: NativeOperationView) -> NativeOperationView:
        """Reject an endpoint view that names another immutable operation."""

        if not isinstance(view, NativeOperationView):
            raise NativeOperationProtocolError(
                "native operation status is not a canonical view"
            )
        if (
            view.operation_id != self.operation_id
            or view.kind != self.kind
            or view.request_sha256 != self.request_sha256
            or view.native_tool_requirement_sha256
            != self.native_tool_requirement_sha256
            or view.input_archive_sha256 != self.input_archive_sha256
            or view.payload_sha256 != self.payload_sha256
        ):
            raise NativeOperationClientConflict(
                "native operation status differs from the frozen request",
                status=409,
                code="native_operation_conflict",
            )
        if (
            view.actual_native_tool_sha256 is not None
            and view.actual_native_tool_sha256
            != self.native_tool_requirement_sha256
        ):
            raise NativeOperationClientConflict(
                "native operation status reports another native tool",
                status=409,
                code="native_tool_mismatch",
            )
        return view


@dataclass(frozen=True, slots=True)
class NativeToolIdentity:
    schema_version: str
    tool: Mapping[str, object]
    tool_sha256: str

    def __post_init__(self) -> None:
        if self.schema_version != NATIVE_TOOL_IDENTITY_SCHEMA:
            raise NativeOperationProtocolError(
                "native tool identity schema is unsupported"
            )
        if not isinstance(self.tool, Mapping) or not self.tool:
            raise NativeOperationProtocolError("native tool identity is empty")
        tool = copy.deepcopy(dict(self.tool))
        if digest(tool) != self.tool_sha256:
            raise NativeOperationProtocolError(
                "native tool identity digest differs from its record"
            )
        object.__setattr__(self, "tool", tool)

    @classmethod
    def from_record(cls, record: Mapping[str, object]) -> NativeToolIdentity:
        if not isinstance(record, Mapping) or set(record) != {
            "schema_version",
            "tool",
            "tool_sha256",
        }:
            raise NativeOperationProtocolError(
                "native tool identity fields are invalid"
            )
        return cls(
            schema_version=record["schema_version"],
            tool=record["tool"],
            tool_sha256=record["tool_sha256"],
        )


class NativeOperationToolProvider:
    """Read the full Windows native tool record once and reuse the observation."""

    def __init__(self, client: NativeOperationClient) -> None:
        if not isinstance(client, NativeOperationClient):
            raise TypeError("client must be a NativeOperationClient")
        self.client = client
        self._identity: NativeToolIdentity | None = None
        self._lock = threading.Lock()

    def __call__(self) -> dict[str, object]:
        with self._lock:
            if self._identity is None:
                self._identity = self.client.get_native_tool()
            return copy.deepcopy(dict(self._identity.tool))

    @property
    def identity(self) -> NativeToolIdentity:
        self()
        with self._lock:
            assert self._identity is not None
            return self._identity


@dataclass(frozen=True, slots=True)
class RemoteNativeCaptureResult:
    """CaptureHandler-compatible result returned by the Linux client adapter."""

    outcome: str
    output: Path | None
    archive: Path | None
    receipt: Mapping[str, object]
    resources_settled: bool
    reason_code: str | None = None


class NativeOperationClient:
    """Small strict JSON/stream client with an injectable HTTP opener."""

    def __init__(
        self,
        endpoint: NativeOperationEndpoint,
        *,
        opener: Callable[..., Any] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not isinstance(endpoint, NativeOperationEndpoint):
            raise TypeError("endpoint must be a NativeOperationEndpoint")
        self.endpoint = endpoint
        self._opener = opener or _internal_opener
        self._sleep = sleeper
        self._clock = clock

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.endpoint.token}",
            "Cache-Control": "no-store",
        }

    def _open(self, request: urlrequest.Request):
        try:
            return self._opener(request, timeout=self.endpoint.timeout)
        except urlerror.HTTPError as error:
            self._raise_http_error(error)
        except urlerror.URLError as error:
            raise NativeOperationClientError(
                f"native operation endpoint is unreachable: {error.reason}"
            ) from error
        except (TimeoutError, OSError) as error:
            raise NativeOperationClientError(
                f"native operation endpoint transport is unavailable: {type(error).__name__}"
            ) from error

    def _raise_http_error(self, error: urlerror.HTTPError) -> None:
        code = None
        detail: dict[str, object] = {}
        message = f"native operation endpoint returned HTTP {error.code}"
        with suppress(Exception):
            raw = error.read(MAX_OPERATION_JSON_BYTES)
            body = json.loads(raw.decode("utf-8"))
            if isinstance(body, dict):
                code = body.get("error_code") if isinstance(body.get("error_code"), str) else None
                if isinstance(body.get("detail"), dict):
                    detail = dict(body["detail"])
                if isinstance(body.get("error"), str) and body["error"]:
                    message = body["error"]
        error_type = (
            NativeOperationClientConflict
            if error.code == 409
            else NativeOperationNotFound
            if error.code == 404
            else NativeOperationClientError
        )
        raise error_type(
            message,
            status=error.code,
            code=code,
            detail=detail,
        )

    def _response_json(self, response) -> dict:
        content_type = response.headers.get_content_type()
        if content_type != "application/json":
            raise NativeOperationProtocolError(
                "native operation endpoint returned a non-JSON response"
            )
        try:
            raw = response.read(MAX_OPERATION_JSON_BYTES + 1)
        except (OSError, http.client.HTTPException) as error:
            raise NativeOperationProtocolError(
                "native operation response transport failed"
            ) from error
        if len(raw) > MAX_OPERATION_JSON_BYTES:
            raise NativeOperationProtocolError("native operation response exceeds its JSON limit")
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise NativeOperationProtocolError(
                "native operation endpoint returned invalid UTF-8 JSON"
            ) from error
        if not isinstance(value, dict):
            raise NativeOperationProtocolError(
                "native operation endpoint response is not a JSON object"
            )
        return value

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        payload: Mapping[str, object] | None = None,
        source: Any = None,
        source_size: int | None = None,
        content_type: str = "application/json",
    ) -> dict:
        if payload is not None and source is not None:
            raise ValueError("a native operation request cannot contain JSON and a file body")
        headers = {
            **self._headers,
            "Accept": "application/json",
            "Content-Type": content_type,
        }
        body: bytes | Any = None
        size = 0
        if payload is not None:
            try:
                body = json.dumps(
                    payload,
                    ensure_ascii=False,
                    allow_nan=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            except (TypeError, ValueError) as error:
                raise NativeOperationProtocolError(
                    "native operation request is not canonical JSON"
                ) from error
            size = len(body)
        elif source is not None:
            if source_size is None or source_size <= 0:
                raise ValueError("source_size is required for a file body")
            body = source
            size = source_size
        if body is not None:
            headers["Content-Length"] = str(size)
        request = urlrequest.Request(
            self.endpoint.base_url + path,
            data=body,
            method=method,
            headers=headers,
        )
        with self._open(request) as response:
            return self._response_json(response)

    @staticmethod
    def _operation_id(operation_id: str) -> str:
        if (
            not isinstance(operation_id, str)
            or _OPERATION_ID.fullmatch(operation_id) is None
            or operation_id.endswith(".")
            or operation_id.split(".", 1)[0].upper() in _WINDOWS_RESERVED_NAMES
        ):
            raise ValueError("operation_id is not path-safe")
        return operation_id

    @staticmethod
    def _view(value: Mapping[str, object]) -> NativeOperationView:
        try:
            return NativeOperationView.from_record(value)
        except NativeOperationError as error:
            raise NativeOperationProtocolError(
                f"native operation endpoint returned an invalid view: {error}"
            ) from error

    def get_native_tool(self) -> NativeToolIdentity:
        response = self._request_json("GET", "/native-tool")
        return NativeToolIdentity.from_record(response)

    def create_operation(self, envelope: Mapping[str, object]) -> NativeOperationView:
        try:
            intent = NativeOperationCreate.from_record(envelope)
        except NativeOperationError as error:
            raise NativeOperationProtocolError(str(error)) from error
        response = self._request_json(
            "POST",
            "/operations",
            payload=intent.to_record(),
        )
        view = self._view(response)
        if (
            view.operation_id != intent.operation_id
            or view.kind != intent.kind
            or view.payload_sha256 != intent.payload_sha256
            or view.request_sha256 != intent.request_sha256
            or view.input_archive_sha256 != intent.input_archive_sha256
            or view.native_tool_requirement_sha256
            != intent.native_tool_requirement_sha256
        ):
            raise NativeOperationProtocolError(
                "native operation endpoint returned a different operation identity"
            )
        return view

    def upload_input(self, operation_id: str, archive: str | Path) -> NativeOperationView:
        canonical_id = self._operation_id(operation_id)
        source = Path(archive)
        if not source.is_file() or source.is_symlink():
            raise ValueError("input archive must be one regular file")
        size = source.stat().st_size
        if not 0 < size <= MAX_OPERATION_ARCHIVE_BYTES:
            raise ValueError("input archive has an invalid size")
        source_sha256 = file_digest(source)
        with source.open("rb") as stream:
            response = self._request_json(
                "POST",
                f"/operations/{canonical_id}/input",
                source=stream,
                source_size=size,
                content_type="application/zip",
            )
        if (
            source.stat().st_size != size
            or file_digest(source) != source_sha256
        ):
            raise NativeOperationProtocolError(
                "input archive changed while it was uploaded"
            )
        view = self._view(response)
        if (
            view.operation_id != canonical_id
            or view.input_archive_sha256 != source_sha256
        ):
            raise NativeOperationProtocolError(
                "native operation endpoint admitted a different input archive"
            )
        return view

    def get_operation(self, operation_id: str) -> NativeOperationView:
        operation_id = self._operation_id(operation_id)
        response = self._request_json(
            "GET",
            f"/operations/{operation_id}",
        )
        view = self._view(response)
        if view.operation_id != operation_id:
            raise NativeOperationProtocolError(
                "native operation endpoint answered for another operation"
            )
        return view

    def submit_operation(
        self,
        envelope: Mapping[str, object],
        archive: str | Path,
    ) -> NativeOperationView:
        """Submit once when needed, then return the current durable state without polling."""

        try:
            intent = NativeOperationCreate.from_record(envelope)
        except NativeOperationError as error:
            raise NativeOperationProtocolError(str(error)) from error
        expected = NativeOperationStatusExpectation.from_create(intent)
        try:
            view = self.get_operation(intent.operation_id)
        except NativeOperationNotFound:
            view = None
        if view is None or view.state == "accepted":
            source = Path(archive)
            if not source.is_file() or source.is_symlink():
                raise ValueError("input archive must be one regular file")
            if (
                source.stat().st_size != intent.input_archive_bytes
                or file_digest(source) != intent.input_archive_sha256
            ):
                raise NativeOperationClientConflict(
                    "local input archive differs from the sealed operation intent",
                    status=409,
                    code="input_archive_mismatch",
                )
        if view is None:
            view = self.create_operation(envelope)
            expected.require(view)
        else:
            expected.require(view)
        if view.state == "accepted":
            view = self.upload_input(intent.operation_id, archive)
            expected.require(view)
        return view

    async def probe_operation_status_async(
        self,
        expected: NativeOperationStatusExpectation,
    ) -> NativeOperationView | None:
        """Perform one read-only status GET without blocking the event loop."""

        if not isinstance(expected, NativeOperationStatusExpectation):
            raise TypeError("expected must be a NativeOperationStatusExpectation")
        try:
            view = await asyncio.to_thread(
                self.get_operation,
                expected.operation_id,
            )
        except NativeOperationNotFound:
            return None
        return expected.require(view)

    def cancel_operation(self, operation_id: str) -> NativeOperationView:
        operation_id = self._operation_id(operation_id)
        response = self._request_json(
            "POST",
            f"/operations/{operation_id}/cancel",
        )
        view = self._view(response)
        if view.operation_id != operation_id:
            raise NativeOperationProtocolError(
                "native operation endpoint cancelled another operation"
            )
        return view

    def wait_for_terminal(
        self,
        operation_id: str,
        *,
        timeout: float = 600.0,
        poll_interval: float = 0.2,
    ) -> NativeOperationView:
        if timeout <= 0 or poll_interval <= 0:
            raise ValueError("timeout and poll_interval must be positive")
        deadline = self._clock() + timeout
        while True:
            view = self.get_operation(operation_id)
            if view.state in _TERMINAL_STATES:
                return view
            remaining = deadline - self._clock()
            if remaining <= 0:
                raise NativeOperationWaitTimeout(
                    "native operation did not reach a terminal state before the client deadline",
                    code="native_operation_wait_timeout",
                    detail={"last_state": view.state},
                )
            self._sleep(min(poll_interval, remaining))

    def download_artifact(
        self,
        operation_id: str,
        destination: str | Path,
        *,
        limit: int = MAX_OPERATION_ARCHIVE_BYTES,
    ) -> ArtifactDownload:
        view = self.get_operation(operation_id)
        artifact = view.artifact
        if artifact is None or not view.resource_terminated:
            raise NativeOperationClientError(
                "native operation has no complete sealed artifact",
                status=409,
                code="native_artifact_unavailable",
            )
        if artifact.media_type != "application/zip":
            raise NativeOperationProtocolError("native artifact has an unsupported media type")
        if type(limit) is not int or limit <= 0 or artifact.bytes > limit:
            raise NativeOperationProtocolError("native artifact exceeds the configured download limit")
        target = Path(destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        request = urlrequest.Request(
            self.endpoint.base_url + f"/operations/{operation_id}/artifact",
            method="GET",
            headers={**self._headers, "Accept": "application/zip"},
        )
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{target.name}.",
            dir=target.parent,
        )
        temporary = Path(temporary_name)
        hasher = hashlib.sha256()
        size = 0
        try:
            with os.fdopen(descriptor, "wb") as stream, self._open(request) as response:
                if response.headers.get_content_type() != "application/zip":
                    raise NativeOperationProtocolError(
                        "native artifact response has a different media type"
                    )
                if response.headers.get("X-Artifact-SHA256") != artifact.sha256:
                    raise NativeOperationProtocolError(
                        "native artifact response carries a different digest"
                    )
                try:
                    declared = int(response.headers.get("Content-Length", ""))
                except ValueError as error:
                    raise NativeOperationProtocolError(
                        "native artifact response has no valid Content-Length"
                    ) from error
                if declared != artifact.bytes:
                    raise NativeOperationProtocolError(
                        "native artifact response size differs from its view"
                    )
                while block := response.read(1024 * 1024):
                    size += len(block)
                    if size > artifact.bytes:
                        raise NativeOperationProtocolError(
                            "native artifact response exceeds its declared size"
                        )
                    hasher.update(block)
                    stream.write(block)
                stream.flush()
                os.fsync(stream.fileno())
            if size != artifact.bytes or hasher.hexdigest() != artifact.sha256:
                raise NativeOperationProtocolError(
                    "native artifact response differs from its size or digest"
                )
            os.replace(temporary, target)
        except (OSError, http.client.HTTPException) as error:
            raise NativeOperationClientError(
                f"native artifact transport is unavailable: {type(error).__name__}"
            ) from error
        finally:
            temporary.unlink(missing_ok=True)
        return ArtifactDownload(
            operation_id=operation_id,
            path=target,
            name=artifact.name,
            bytes=artifact.bytes,
            sha256=artifact.sha256,
            media_type=artifact.media_type,
        )


def native_capture_operation_id(
    *,
    run_id: str,
    command_id: str,
    effect_key: str,
) -> str:
    """The exact path-safe identity for one Capture native effect."""

    for field, value in (
        ("run_id", run_id),
        ("command_id", command_id),
        ("effect_key", effect_key),
    ):
        if not isinstance(value, str) or not value:
            raise ValueError(f"{field} is required")
    return "capture-" + digest(
        {
            "run_id": run_id,
            "command_id": command_id,
            "effect_key": effect_key,
        }
    )


def observe_native_operation(
    client: NativeOperationClient,
    operation_id: str,
) -> NativeOperationView | None:
    """Read one exact operation identity without creating or sending anything."""

    try:
        return client.get_operation(operation_id)
    except NativeOperationNotFound:
        return None


def native_operation_input_was_admitted(view: NativeOperationView) -> bool:
    """Whether submission no longer needs to upload the immutable input."""

    if not isinstance(view, NativeOperationView):
        raise TypeError("view must be a NativeOperationView")
    return view.state != "accepted"


def _capture_archive_manifest(archive: Path) -> dict[str, object]:
    try:
        with zipfile.ZipFile(archive) as source:
            info = source.getinfo(CAPTURE_MANIFEST)
            if info.file_size > MAX_MANIFEST_BYTES:
                raise NativeOperationProtocolError(
                    "native artifact transfer manifest exceeds its size limit"
                )
            record = parse_data(source.read(info), Path(CAPTURE_MANIFEST))
    except (KeyError, zipfile.BadZipFile, OSError) as error:
        raise NativeOperationProtocolError(
            "native artifact is not a readable Capture transfer"
        ) from error
    if not isinstance(record, dict):
        raise NativeOperationProtocolError(
            "native artifact transfer manifest is not an object"
        )
    return record


@dataclass(frozen=True, slots=True)
class SealedNativeOperationRequest:
    """The canonical request identity reconstructed from a sealed outbound."""

    operation_id: str
    kind: str
    request: CaptureOperationRequest | NativeDiscoverRequest
    request_sha256: str
    payload_sha256: str | None
    native_tool_requirement_sha256: str
    input_archive: Path | None = None
    input_archive_sha256: str | None = None


def read_sealed_native_operation_request(
    command: StageCommand,
    intent: EffectIntent,
    outbound_root: Path,
) -> SealedNativeOperationRequest:
    """Read one canonical native request from immutable effect evidence."""

    if command.stage == "capture":
        from ..pipeline.capture import (
            CaptureRequest,
            _outbound_tree_sha256,
            validate_capture_effect_outbound,
        )

        outbound = validate_capture_effect_outbound(outbound_root, command)
        request = CaptureRequest.from_command(command)
        objects = outbound["objects"]
        expected_upstream = [
            artifact.to_record()
            for artifact in sorted(
                (
                    item
                    for item in command.input_artifacts
                    if item.producer_stage in {"freeze", "discover"}
                ),
                key=lambda item: (str(item.producer_stage), item.artifact_id),
            )
        ]
        expected_state = {
            "command_id": command.command_id,
            "effect_key": request.effect_key,
            "handoff_sha256": request.handoff_sha256,
            "main_assembly": request.main_assembly,
            "native_tool_record_sha256": request.native_tool_record_sha256,
            "native_tool_source_sha256": request.native_tool_source_sha256,
            "object_set_sha256": digest(objects),
            "outbound_sha256": _outbound_tree_sha256(outbound),
            "prepared_sha256": request.prepared_sha256,
            "request_digest": command.request_digest,
            "schema_version": NATIVE_CAPTURE_EFFECT_SCHEMA,
            "upload_sha256": request.upload_sha256,
        }
        if (
            outbound["upstream_artifacts"] != expected_upstream
            or dict(intent.expected_state) != expected_state
        ):
            raise NativeOperationProtocolError(
                "Capture reconciliation outbound differs from the frozen command"
            )
        request_record = {
            "schema_version": CAPTURE_OPERATION_SCHEMA,
            "command_id": command.command_id,
            "run_id": command.run_id,
            "request_digest": command.request_digest,
            "effect_key": request.effect_key,
            "handoff_sha256": outbound["handoff_sha256"],
            "upload_sha256": outbound["upload_sha256"],
            "prepared_sha256": request.prepared_sha256,
            "main_assembly": outbound["main_assembly"],
            "package_files": outbound["prepared_files"],
            "upstream_artifacts": outbound["upstream_artifacts"],
            "execution": outbound["execution"],
            "native_tool_source_sha256": request.native_tool_source_sha256,
            "request_evidence": outbound["request_evidence"],
        }
        try:
            native_request = CaptureOperationRequest.from_record(request_record)
        except Exception as error:
            raise NativeOperationProtocolError(
                "Capture reconciliation outbound is not a canonical native request"
            ) from error
        return SealedNativeOperationRequest(
            operation_id=native_capture_operation_id(
                run_id=command.run_id,
                command_id=command.command_id,
                effect_key=request.effect_key,
            ),
            kind=CAPTURE_KIND,
            request=native_request,
            request_sha256=digest(native_request.to_record()),
            payload_sha256=None,
            native_tool_requirement_sha256=request.native_tool_record_sha256,
        )

    if command.stage == "discover":
        request = NativeDiscoverRequest.from_record(
            read_data(outbound_root / "request.json")
        )
        source = read_data(outbound_root / "source.json")
        archive = outbound_root / SOURCE_ARCHIVE
        if (
            source
            != {
                "source_files": dict(request.source_files),
                "source_inventory_sha256": request.source_inventory_sha256,
            }
            or request.command_id != command.command_id
            or request.run_id != command.run_id
            or request.request_digest != command.request_digest
            or request.expected_native_tool_sha256
            != command.request.get("expected_native_tool_sha256")
            or request.expected_native_tool_record_sha256
            != command.request.get("expected_native_tool_record_sha256")
            or request.native_tool_requirement_artifact_id
            != command.request.get("native_tool_requirement_artifact_id")
            or not archive.is_file()
            or archive.is_symlink()
        ):
            raise NativeOperationProtocolError(
                "Discover reconciliation outbound differs from the frozen command"
            )
        source_archive_sha256 = file_digest(archive)
        if (
            intent.expected_state.get("source_archive_sha256")
            != source_archive_sha256
            or intent.expected_state.get("request_digest")
            != command.request_digest
            or intent.expected_state.get("native_tool_record_sha256")
            != request.expected_native_tool_record_sha256
            or intent.expected_state.get(
                "native_tool_requirement_artifact_id"
            )
            != request.native_tool_requirement_artifact_id
        ):
            raise NativeOperationProtocolError(
                "Discover reconciliation outbound differs from its effect intent"
            )
        operation = build_operation_record(
            operation_id=request.operation_id,
            kind=DISCOVER_KIND,
            request=request.to_record(),
            input_archive_sha256=source_archive_sha256,
            input_archive_bytes=archive.stat().st_size,
            native_tool_requirement_sha256=(
                request.expected_native_tool_record_sha256
            ),
            output_contract=NATIVE_OBSERVATION_SCHEMA,
        )
        return SealedNativeOperationRequest(
            operation_id=request.operation_id,
            kind=DISCOVER_KIND,
            request=request,
            request_sha256=digest(request.to_record()),
            payload_sha256=operation["payload_sha256"],
            native_tool_requirement_sha256=(
                request.expected_native_tool_record_sha256
            ),
            input_archive=archive,
            input_archive_sha256=source_archive_sha256,
        )

    raise NativeOperationProtocolError(
        "native reconciliation supports only Discover and Capture"
    )


@dataclass(frozen=True, slots=True)
class NativeOperationEffectObserver:
    """Read one exact native operation as domain reconciliation evidence."""

    client: NativeOperationClient

    def __post_init__(self) -> None:
        if not isinstance(self.client, NativeOperationClient):
            raise TypeError("client must be a NativeOperationClient")

    def observe(
        self,
        *,
        command: StageCommand,
        intent: EffectIntent,
        outbound_root: Path,
        workspace_root: Path,
    ) -> EffectReconciliationEvidence:
        if not isinstance(outbound_root, Path) or not outbound_root.is_dir():
            raise NativeOperationProtocolError(
                "native reconciliation outbound root is missing"
            )
        if not isinstance(workspace_root, Path) or not workspace_root.is_dir():
            raise NativeOperationProtocolError(
                "native reconciliation workspace is missing"
            )
        if (
            intent.effect_kind != "native_operation"
            or intent.command_id != command.command_id
            or intent.target.get("operation_id", intent.target.get("command_id"))
            != command.command_id
        ):
            raise NativeOperationProtocolError(
                "native reconciliation intent belongs to another command"
            )
        if command.stage == "capture":
            return self._observe_capture(
                command=command,
                intent=intent,
                outbound_root=outbound_root,
                workspace_root=workspace_root,
            )
        if command.stage == "discover":
            return self._observe_discover(
                command=command,
                intent=intent,
                outbound_root=outbound_root,
                workspace_root=workspace_root,
            )
        raise NativeOperationProtocolError(
            "native reconciliation supports only Discover and Capture"
        )

    def _operation(
        self,
        operation_id: str,
        *,
        workspace_root: Path,
    ) -> NativeOperationView | EffectReconciliationEvidence:
        try:
            return self.client.get_operation(operation_id)
        except NativeOperationNotFound:
            return self._unknown_evidence(
                workspace_root,
                operation_id=operation_id,
                state_class="absent",
                reason="operation_not_found",
                metadata={"http_status": 404},
            )
        except NativeOperationClientError as error:
            if error.status is not None and error.status < 500:
                raise
            return self._unknown_evidence(
                workspace_root,
                operation_id=operation_id,
                state_class="unobservable",
                reason="operation_unreachable",
                metadata={
                    "error_type": type(error).__name__,
                    "http_status": error.status,
                },
            )

    @staticmethod
    def _unknown_evidence(
        workspace_root: Path,
        *,
        operation_id: str,
        state_class: str,
        reason: str,
        metadata: Mapping[str, object],
        view: NativeOperationView | None = None,
    ) -> EffectReconciliationEvidence:
        from ..pipeline.reconciliation import EffectReconciliationEvidence

        root = workspace_root / f"native-{state_class}-{operation_id}"
        root.mkdir(parents=True, exist_ok=False)
        write_json(
            root / "operation.json",
            {
                "schema_version": NATIVE_OPERATION_OBSERVATION_SCHEMA,
                "operation_id": operation_id,
                "state_class": state_class,
                "reason": reason,
                "operation": None if view is None else view.to_record(),
            },
        )
        return EffectReconciliationEvidence(
            fact_type="observation",
            state_class=state_class,
            payload_root=root,
            metadata={
                "operation_id": operation_id,
                "reason": reason,
                **dict(metadata),
            },
        )

    @staticmethod
    def _terminal_absence(
        workspace_root: Path,
        view: NativeOperationView,
    ) -> EffectReconciliationEvidence:
        from ..pipeline.reconciliation import EffectReconciliationEvidence

        root = workspace_root / f"native-absent-{view.operation_id}"
        root.mkdir(parents=True, exist_ok=False)
        write_json(
            root / "operation.json",
            {
                "schema_version": NATIVE_OPERATION_OBSERVATION_SCHEMA,
                "operation_id": view.operation_id,
                "state_class": "absent",
                "reason": "terminated_without_artifact",
                "operation": view.to_record(),
            },
        )
        return EffectReconciliationEvidence(
            fact_type="reconciled",
            state_class="absent",
            payload_root=root,
            metadata={
                "operation_id": view.operation_id,
                "state": view.state,
                "resource_terminated": True,
                "artifact": None,
            },
        )

    @staticmethod
    def _view_identity(
        view: NativeOperationView,
        *,
        operation_id: str,
        kind: str,
        request_sha256: str,
        native_tool_requirement_sha256: str,
        payload_sha256: str | None = None,
    ) -> None:
        if (
            view.operation_id != operation_id
            or view.kind != kind
            or view.request_sha256 != request_sha256
            or view.native_tool_requirement_sha256
            != native_tool_requirement_sha256
            or (
                payload_sha256 is not None
                and view.payload_sha256 != payload_sha256
            )
        ):
            raise NativeOperationClientConflict(
                "native operation differs from the sealed outbound request",
                status=409,
                code="native_operation_conflict",
            )
        if (
            view.actual_native_tool_sha256 is not None
            and view.actual_native_tool_sha256
            != native_tool_requirement_sha256
        ):
            raise NativeOperationClientConflict(
                "Windows executed with a different full native tool record",
                status=409,
                code="native_tool_mismatch",
            )

    def _non_satisfied(
        self,
        view: NativeOperationView,
        *,
        workspace_root: Path,
    ) -> EffectReconciliationEvidence:
        if (
            view.state in {"failed", "cancelled", "unknown"}
            and view.resource_terminated
            and view.artifact is None
            and view.actual_native_tool_sha256
            == view.native_tool_requirement_sha256
        ):
            return self._terminal_absence(workspace_root, view)
        return self._unknown_evidence(
            workspace_root,
            operation_id=view.operation_id,
            state_class="unobservable",
            reason=(
                "operation_not_terminal"
                if view.state not in {"failed", "cancelled", "unknown"}
                else "resource_termination_unproven"
            ),
            metadata={
                "state": view.state,
                "resource_terminated": view.resource_terminated,
                "artifact_present": view.artifact is not None,
            },
            view=view,
        )

    def _observe_capture(
        self,
        *,
        command: StageCommand,
        intent: EffectIntent,
        outbound_root: Path,
        workspace_root: Path,
    ) -> EffectReconciliationEvidence:
        from ..pipeline.capture import CaptureRequest
        from ..pipeline.reconciliation import EffectReconciliationEvidence
        from .native_operation_runtime import CaptureOperationRequest

        identity = read_sealed_native_operation_request(
            command,
            intent,
            outbound_root,
        )
        if (
            identity.kind != CAPTURE_KIND
            or not isinstance(identity.request, CaptureOperationRequest)
        ):
            raise NativeOperationProtocolError(
                "Capture reconciliation resolved another request kind"
            )
        request = CaptureRequest.from_command(command)
        native_request = identity.request
        execution = dict(native_request.execution)
        upstream = [
            dict(record) for record in native_request.upstream_artifacts
        ]
        prepared_files = dict(native_request.package_files)
        operation_id = identity.operation_id
        observed = self._operation(
            operation_id,
            workspace_root=workspace_root,
        )
        if isinstance(observed, EffectReconciliationEvidence):
            return observed
        self._view_identity(
            observed,
            operation_id=operation_id,
            kind=CAPTURE_KIND,
            request_sha256=identity.request_sha256,
            native_tool_requirement_sha256=request.native_tool_record_sha256,
        )
        if observed.state != "satisfied" and not (
            observed.state == "unknown"
            and observed.artifact is not None
            and observed.resource_terminated
        ):
            return self._non_satisfied(
                observed,
                workspace_root=workspace_root,
            )
        download_root = workspace_root / f"native-download-{operation_id}"
        download_root.mkdir(parents=True, exist_ok=False)
        try:
            downloaded = self.client.download_artifact(
                operation_id,
                download_root / CAPTURE_ARCHIVE,
            )
        except NativeOperationClientError as error:
            if error.status is not None and error.status < 500:
                raise
            return self._unknown_evidence(
                workspace_root,
                operation_id=operation_id,
                state_class="unobservable",
                reason="artifact_unreachable",
                metadata={
                    "error_type": type(error).__name__,
                    "http_status": error.status,
                },
                view=observed,
            )
        if (
            downloaded.name != CAPTURE_ARCHIVE
            or self.client.get_operation(operation_id) != observed
        ):
            raise NativeOperationClientConflict(
                "Capture operation changed while its artifact was observed",
                status=409,
                code="native_operation_conflict",
            )
        manifest = _capture_archive_manifest(downloaded.path)
        native_tool = manifest.get("native_tool")
        if not isinstance(native_tool, dict):
            raise NativeOperationProtocolError(
                "Capture transfer carries no native tool record"
            )
        native_tool_sha256 = digest(native_tool)
        if (
            native_tool.get("source_sha256")
            != request.native_tool_source_sha256
            or native_tool_sha256 != request.native_tool_record_sha256
            or observed.actual_native_tool_sha256 != native_tool_sha256
        ):
            raise NativeOperationClientConflict(
                "Capture transfer native tool differs from the frozen requirement",
                status=409,
                code="native_tool_mismatch",
            )
        admitted_root = workspace_root / f"admitted-capture-{operation_id}"
        admit_capture(
            downloaded.path,
            admitted_root,
            expected_run_id=command.run_id,
            expected_handoff_sha256=request.handoff_sha256,
            expected_upload_sha256=request.upload_sha256,
            expected_main_assembly=request.main_assembly,
            expected_native_tool=native_tool,
            expected_prepared_dir=None,
            expected_prepared_files=prepared_files,
            expected_upstream_artifacts=upstream,
            expected_execution=execution,
        )
        payload = workspace_root / f"capture-evidence-{operation_id}"
        payload.mkdir(parents=True, exist_ok=False)
        shutil.copyfile(downloaded.path, payload / CAPTURE_ARCHIVE)
        write_json(payload / "execution.json", execution)
        write_json(payload / "native-tool.json", native_tool)
        return EffectReconciliationEvidence(
            fact_type="reconciled",
            state_class="known",
            payload_root=payload,
            metadata={
                "operation_id": operation_id,
                "operation_state": observed.state,
                "resource_terminated": observed.resource_terminated,
                "native_tool_requirement_sha256": native_tool_sha256,
                "actual_native_tool_sha256": observed.actual_native_tool_sha256,
                "artifact_sha256": downloaded.sha256,
                "request_sha256": observed.request_sha256,
                "input_archive_sha256": observed.input_archive_sha256,
            },
        )

    def _observe_discover(
        self,
        *,
        command: StageCommand,
        intent: EffectIntent,
        outbound_root: Path,
        workspace_root: Path,
    ) -> EffectReconciliationEvidence:
        from ..pipeline.reconciliation import EffectReconciliationEvidence

        identity = read_sealed_native_operation_request(
            command,
            intent,
            outbound_root,
        )
        if (
            identity.kind != DISCOVER_KIND
            or not isinstance(identity.request, NativeDiscoverRequest)
            or identity.input_archive is None
            or identity.input_archive_sha256 is None
        ):
            raise NativeOperationProtocolError(
                "Discover reconciliation resolved another request kind"
            )
        request = identity.request
        source_archive_sha256 = identity.input_archive_sha256
        operation_id = identity.operation_id
        observed = self._operation(
            operation_id,
            workspace_root=workspace_root,
        )
        if isinstance(observed, EffectReconciliationEvidence):
            return observed
        self._view_identity(
            observed,
            operation_id=operation_id,
            kind=DISCOVER_KIND,
            request_sha256=identity.request_sha256,
            native_tool_requirement_sha256=(
                identity.native_tool_requirement_sha256
            ),
            payload_sha256=identity.payload_sha256,
        )
        if observed.input_archive_sha256 != source_archive_sha256:
            raise NativeOperationClientConflict(
                "Discover operation input archive differs from the sealed outbound",
                status=409,
                code="native_operation_conflict",
            )
        if observed.state != "satisfied" and not (
            observed.state == "unknown"
            and observed.artifact is not None
            and observed.resource_terminated
        ):
            return self._non_satisfied(
                observed,
                workspace_root=workspace_root,
            )
        download_root = workspace_root / f"native-download-{operation_id}"
        download_root.mkdir(parents=True, exist_ok=False)
        try:
            downloaded = self.client.download_artifact(
                operation_id,
                download_root / OBSERVATION_ARCHIVE,
            )
        except NativeOperationClientError as error:
            if error.status is not None and error.status < 500:
                raise
            return self._unknown_evidence(
                workspace_root,
                operation_id=operation_id,
                state_class="unobservable",
                reason="artifact_unreachable",
                metadata={
                    "error_type": type(error).__name__,
                    "http_status": error.status,
                },
                view=observed,
            )
        if (
            downloaded.name != OBSERVATION_ARCHIVE
            or self.client.get_operation(operation_id) != observed
        ):
            raise NativeOperationClientConflict(
                "Discover operation changed while its artifact was observed",
                status=409,
                code="native_operation_conflict",
            )
        evidence_root = workspace_root / f"discover-evidence-{operation_id}"
        observation = admit_observation_bundle(
            downloaded.path,
            evidence_root,
            request=request,
        )
        native_tool = dict(observation.native_tool)
        native_tool_sha256 = digest(native_tool)
        if (
            native_tool.get("source_sha256")
            != request.expected_native_tool_sha256
            or native_tool_sha256
            != request.expected_native_tool_record_sha256
            or observed.actual_native_tool_sha256 != native_tool_sha256
            or observation.manifest.get("source_archive_sha256")
            != source_archive_sha256
        ):
            raise NativeOperationClientConflict(
                "Discover observation differs from the frozen tool or input closure",
                status=409,
                code="native_operation_conflict",
            )
        return EffectReconciliationEvidence(
            fact_type="reconciled",
            state_class="known",
            payload_root=observation.root,
            metadata={
                "operation_id": operation_id,
                "operation_state": observed.state,
                "resource_terminated": observed.resource_terminated,
                "native_tool_requirement_sha256": native_tool_sha256,
                "actual_native_tool_sha256": observed.actual_native_tool_sha256,
                "source_archive_sha256": source_archive_sha256,
                "artifact_sha256": downloaded.sha256,
                "request_sha256": observed.request_sha256,
            },
        )


@dataclass(frozen=True, slots=True)
class NativeOperationCapturePort:
    """CaptureHandler-native-operation adapter over one Windows operation."""

    client: NativeOperationClient
    timeout: float = 600.0
    poll_interval: float = 0.2

    def __post_init__(self) -> None:
        if not isinstance(self.client, NativeOperationClient):
            raise TypeError("client must be a NativeOperationClient")
        if self.timeout <= 0 or self.poll_interval <= 0:
            raise ValueError("timeout and poll_interval must be positive")

    @staticmethod
    def operation_id(
        *,
        run_id: str,
        command_id: str,
        effect_key: str,
    ) -> str:
        return native_capture_operation_id(
            run_id=run_id,
            command_id=command_id,
            effect_key=effect_key,
        )

    def observe(
        self,
        *,
        run_id: str,
        command_id: str,
        effect_key: str,
    ) -> NativeOperationView | None:
        return observe_native_operation(
            self.client,
            self.operation_id(
                run_id=run_id,
                command_id=command_id,
                effect_key=effect_key,
            ),
        )

    @staticmethod
    def _request_record(
        inputs,
        current_native_tool: Mapping[str, object],
        *,
        run_id: str,
        request_digest: str,
        handoff_sha256: str,
        upload_sha256: str,
        main_assembly: str,
        upstream_artifacts: tuple[Mapping[str, object], ...],
        execution: Mapping[str, object],
    ) -> tuple[CaptureOperationRequest, dict[str, object]]:
        current_tool = copy.deepcopy(dict(current_native_tool))
        full_tool_sha256 = digest(current_tool)
        source_sha256 = current_tool.get("source_sha256")
        if not isinstance(source_sha256, str):
            raise NativeOperationProtocolError(
                "The current native tool carries no source identity"
            )
        if execution.get("native_tool_sha256") != full_tool_sha256:
            raise NativeOperationProtocolError(
                "Capture execution does not bind the current full native tool"
            )
        record = {
            "schema_version": CAPTURE_OPERATION_SCHEMA,
            "command_id": execution["command_id"],
            "run_id": run_id,
            "request_digest": request_digest,
            "effect_key": execution["effect_key"],
            "handoff_sha256": handoff_sha256,
            "upload_sha256": upload_sha256,
            "prepared_sha256": digest(dict(inputs.prepared_files)),
            "main_assembly": main_assembly,
            "package_files": dict(inputs.prepared_files),
            "upstream_artifacts": [
                dict(record) for record in upstream_artifacts
            ],
            "execution": copy.deepcopy(dict(execution)),
            "native_tool_source_sha256": source_sha256,
            "request_evidence": copy.deepcopy(
                dict(inputs.request_evidence)
            ),
        }
        request = CaptureOperationRequest.from_record(record)
        return request, current_tool

    def __call__(
        self,
        inputs,
        output: Path,
        *,
        backend,
        run_id: str,
        request_digest: str,
        handoff_sha256: str,
        upload_sha256: str,
        main_assembly: str,
        upstream_artifacts: tuple[Mapping[str, object], ...],
        execution: Mapping[str, object],
        native_tool: Mapping[str, object],
    ) -> RemoteNativeCaptureResult:
        if backend is not None:
            raise NativeOperationProtocolError(
                "The remote Capture adapter does not accept a Linux CAD backend"
            )
        request, current_tool = self._request_record(
            inputs,
            native_tool,
            run_id=run_id,
            request_digest=request_digest,
            handoff_sha256=handoff_sha256,
            upload_sha256=upload_sha256,
            main_assembly=main_assembly,
            upstream_artifacts=upstream_artifacts,
            execution=execution,
        )
        if dict(native_tool) != current_tool:
            raise NativeOperationProtocolError(
                "Capture handler supplied a tool identity other than the current native tool"
            )
        operation_id = self.operation_id(
            run_id=run_id,
            command_id=request.command_id,
            effect_key=request.effect_key,
        )
        view = observe_native_operation(self.client, operation_id)
        request_sha256 = digest(request.to_record())
        full_tool_sha256 = digest(current_tool)
        if view is not None and (
            view.kind != CAPTURE_KIND
            or view.request_sha256 != request_sha256
            or view.native_tool_requirement_sha256 != full_tool_sha256
        ):
            raise NativeOperationClientConflict(
                "Capture operation identity is bound to a different request",
                status=409,
                code="native_operation_conflict",
            )
        archive = output.parent / f"{operation_id}-prepared.zip"
        if view is None or view.state == "accepted":
            archive.parent.mkdir(parents=True, exist_ok=True)
            expected_archive = None if view is None else view.input_archive_sha256
            if not (
                expected_archive is not None
                and archive.is_file()
                and not archive.is_symlink()
                and file_digest(archive) == expected_archive
            ):
                archive.unlink(missing_ok=True)
                archive = seal_source_archive(
                    inputs.package,
                    inputs.prepared_files,
                    archive,
                )
            if (
                expected_archive is not None
                and file_digest(archive) != expected_archive
            ):
                raise NativeOperationClientConflict(
                    "local Capture archive differs from the existing operation",
                    status=409,
                    code="native_operation_conflict",
                )
            envelope = build_operation_record(
                operation_id=operation_id,
                kind=CAPTURE_KIND,
                request=request.to_record(),
                input_archive_sha256=file_digest(archive),
                input_archive_bytes=archive.stat().st_size,
                native_tool_requirement_sha256=full_tool_sha256,
                output_contract=CAPTURE_TRANSFER_SCHEMA,
            )
            view = self.client.create_operation(envelope)
        if view.state == "accepted":
            view = self.client.upload_input(operation_id, archive)
        if view.state not in _TERMINAL_STATES:
            view = self.client.wait_for_terminal(
                operation_id,
                timeout=self.timeout,
                poll_interval=self.poll_interval,
            )
        if (
            view.actual_native_tool_sha256 is not None
            and view.actual_native_tool_sha256 != full_tool_sha256
        ):
            raise NativeOperationProtocolError(
                "Windows executed Capture with a different full native tool record"
            )
        receipt = view.to_record()
        if view.state == "satisfied":
            output.mkdir(parents=True, exist_ok=True)
            downloaded = self.client.download_artifact(
                operation_id,
                output / CAPTURE_ARCHIVE,
            )
            return RemoteNativeCaptureResult(
                outcome="completed",
                output=output,
                archive=downloaded.path,
                receipt=receipt,
                resources_settled=True,
            )
        if view.state in {"failed", "cancelled"}:
            failure_output = output.with_name(output.name + "-remote-failure")
            failure_output.mkdir(parents=True, exist_ok=False)
            write_json(failure_output / "native-result.json", receipt)
            return RemoteNativeCaptureResult(
                outcome=view.state,
                output=failure_output,
                archive=None,
                receipt=receipt,
                resources_settled=view.resource_terminated,
                reason_code=(
                    view.failure.code
                    if view.failure is not None
                    else f"capture.{view.state}"
                ),
            )
        failure = view.failure
        return RemoteNativeCaptureResult(
            outcome="unknown",
            output=None,
            archive=None,
            receipt=receipt,
            resources_settled=view.resource_terminated,
            reason_code=(
                failure.code
                if failure is not None
                else "capture.native_unknown"
            ),
        )


def capture_handler_from_native_operation(
    client: NativeOperationClient,
    *,
    artifact_store=None,
    timeout: float = 600.0,
    poll_interval: float = 0.2,
    tool_provider: NativeOperationToolProvider | None = None,
):
    """Build CaptureHandler with the Windows port and no local CAD fallback."""

    from ..pipeline.capture import CaptureHandler

    if tool_provider is None:
        tool_provider = NativeOperationToolProvider(client)
    if not isinstance(tool_provider, NativeOperationToolProvider):
        raise TypeError("tool_provider must be a NativeOperationToolProvider")
    return CaptureHandler(
        artifact_store=artifact_store,
        native_operation=NativeOperationCapturePort(
            client,
            timeout=timeout,
            poll_interval=poll_interval,
        ),
        native_tool_factory=tool_provider,
    )


@dataclass(frozen=True, slots=True)
class NativeDiscoverExecution:
    """Compatibility result for ports that wrap one sealed observation."""

    effect_id: str
    observation: NativeObservation


@dataclass(frozen=True, slots=True)
class NativeOperationDiscoverPort:
    """Discover port that owns one remote operation and never starts Capture."""

    client: NativeOperationClient
    native_tool: Mapping[str, object] | None
    timeout: float = 600.0
    poll_interval: float = 0.2
    tool_provider: Callable[[], Mapping[str, object]] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.client, NativeOperationClient):
            raise TypeError("client must be a NativeOperationClient")
        if self.native_tool is None:
            if not callable(self.tool_provider):
                raise ValueError(
                    "native_tool or a callable tool_provider is required"
                )
        else:
            tool = dict(self.native_tool)
            if not tool:
                raise ValueError("native_tool must not be empty")
            object.__setattr__(self, "native_tool", tool)
        if self.timeout <= 0 or self.poll_interval <= 0:
            raise ValueError("timeout and poll_interval must be positive")

    def _tool(self) -> dict[str, object]:
        if self.tool_provider is None:
            assert self.native_tool is not None
            return copy.deepcopy(dict(self.native_tool))
        tool = copy.deepcopy(dict(self.tool_provider()))
        if not tool:
            raise NativeOperationProtocolError(
                "The native tool provider returned an empty record"
            )
        return tool

    def _validate_existing(self, view: NativeOperationView, request) -> None:
        request_record = request.to_record()
        if (
            view.kind != DISCOVER_KIND
            or view.request_sha256 != digest(request_record)
            or view.native_tool_requirement_sha256 != digest(self._tool())
        ):
            raise NativeOperationClientConflict(
                "native operation identity is bound to a different Discover request",
                status=409,
                code="native_operation_conflict",
            )

    @staticmethod
    def operation_id(request) -> str:
        return request.operation_id

    def observe(self, request) -> NativeOperationView | None:
        return observe_native_operation(
            self.client,
            self.operation_id(request),
        )

    def invoke(
        self,
        source_archive: Path,
        request,
        workspace: Path,
    ) -> NativeObservation:
        archive = Path(source_archive)
        workspace = Path(workspace)
        workspace.mkdir(parents=True, exist_ok=True)
        view = self.observe(request)
        if view is not None:
            self._validate_existing(view, request)
        if view is None or view.state == "accepted":
            if not archive.is_file() or archive.is_symlink():
                raise NativeOperationProtocolError(
                    "Discover requires the complete sealed SourceSet archive"
                )
            if view is not None and file_digest(archive) != view.input_archive_sha256:
                raise NativeOperationClientConflict(
                    "local SourceSet archive differs from the existing operation",
                    status=409,
                    code="native_operation_conflict",
                )
            envelope = build_operation_record(
                operation_id=request.operation_id,
                kind=DISCOVER_KIND,
                request=request.to_record(),
                input_archive_sha256=file_digest(archive),
                input_archive_bytes=archive.stat().st_size,
                native_tool_requirement_sha256=digest(self._tool()),
                output_contract=NATIVE_OBSERVATION_SCHEMA,
            )
            view = self.client.create_operation(envelope)
            self._validate_existing(view, request)
        if view.state == "accepted":
            view = self.client.upload_input(request.operation_id, archive)
        if view.state not in _TERMINAL_STATES:
            view = self.client.wait_for_terminal(
                request.operation_id,
                timeout=self.timeout,
                poll_interval=self.poll_interval,
            )
        if view.state == "unknown":
            raise NativeOperationRemoteUnknown(
                view.failure.message if view.failure else "native Discover result is unknown",
                code=view.failure.code if view.failure else "native_operation_unknown",
            )
        if view.state in {"failed", "cancelled"}:
            failure = view.failure
            raise NativeDiscoverRefusal(
                failure.code if failure else f"discover.{view.state}",
                failure.message if failure else f"native Discover ended {view.state}",
            )
        if view.state != "satisfied":
            raise NativeOperationProtocolError(
                f"native Discover ended in unexpected state {view.state!r}"
            )
        downloaded = self.client.download_artifact(
            request.operation_id,
            workspace / f"{request.operation_id}-{OBSERVATION_ARCHIVE}",
        )
        observation_root = workspace / f"native-observation-{request.operation_id}"
        if observation_root.exists():
            try:
                observation = admit_native_observation(
                    observation_root,
                    request=request,
                )
            except PipelineError:
                shutil.rmtree(observation_root)
                observation = admit_observation_bundle(
                    downloaded.path,
                    observation_root,
                    request=request,
                )
        else:
            observation = admit_observation_bundle(
                downloaded.path,
                observation_root,
                request=request,
            )
        return observation

    def discover(
        self,
        request,
        source_root: Path,
        workspace: Path,
    ) -> NativeDiscoverExecution:
        archive = seal_source_archive(
            Path(source_root),
            request.source_files,
            Path(workspace)
            / f"{request.operation_id}-input-{uuid.uuid4().hex}-{SOURCE_ARCHIVE}",
        )
        observation = self.invoke(
            archive,
            request,
            Path(workspace) / "operation",
        )
        return NativeDiscoverExecution(
            effect_id=f"native-discover:{request.operation_id}",
            observation=observation,
        )


@dataclass(frozen=True, slots=True)
class NativeRemoteDiscoverAdapter:
    """DiscoverDomainAdapter over the Windows operation client."""

    port: NativeOperationDiscoverPort
    artifacts: object

    def invoke(
        self,
        source_archive: Path,
        request,
        workspace: Path,
    ) -> NativeObservation:
        return self.port.invoke(source_archive, request, workspace)

    def materialize(
        self,
        artifact,
        request,
        destination: Path,
    ) -> NativeObservation:
        root = self.artifacts.materialize(artifact, destination)
        return admit_native_observation(
            root,
            request=request,
            artifact_ref=artifact,
            resources_settled=True,
        )


def discover_adapter_from_native_operation(
    client: NativeOperationClient,
    tool_provider: NativeOperationToolProvider,
    artifacts,
    *,
    timeout: float = 600.0,
    poll_interval: float = 0.2,
) -> NativeRemoteDiscoverAdapter:
    """Build Discover with one observed full Windows tool record."""

    if not isinstance(tool_provider, NativeOperationToolProvider):
        raise TypeError("tool_provider must be a NativeOperationToolProvider")
    if not callable(getattr(artifacts, "materialize", None)):
        raise TypeError("artifacts must provide materialize()")
    return NativeRemoteDiscoverAdapter(
        port=NativeOperationDiscoverPort(
            client,
            tool_provider(),
            timeout=timeout,
            poll_interval=poll_interval,
        ),
        artifacts=artifacts,
    )


__all__ = [
    "MAX_OPERATION_ARCHIVE_BYTES",
    "MAX_OPERATION_JSON_BYTES",
    "ArtifactDownload",
    "NativeDiscoverExecution",
    "NativeOperationCapturePort",
    "NativeOperationClient",
    "NativeOperationClientConflict",
    "NativeOperationClientError",
    "NativeOperationDiscoverPort",
    "NativeOperationEffectObserver",
    "NativeOperationEndpoint",
    "NativeOperationNotFound",
    "NativeOperationProtocolError",
    "NativeOperationRemoteUnknown",
    "NativeOperationStatusExpectation",
    "NativeOperationToolProvider",
    "NativeOperationWaitTimeout",
    "NativeRemoteDiscoverAdapter",
    "NativeToolIdentity",
    "RemoteNativeCaptureResult",
    "SealedNativeOperationRequest",
    "capture_handler_from_native_operation",
    "discover_adapter_from_native_operation",
    "native_capture_operation_id",
    "native_operation_input_was_admitted",
    "observe_native_operation",
    "read_sealed_native_operation_request",
]
