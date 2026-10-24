"""Strict client and Discover adapter for Windows native operations."""

from __future__ import annotations

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
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field as dataclass_field
from pathlib import Path
from typing import Any
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest

from ..io import PipelineError, digest, file_digest, write_json
from ..native_transfer import CAPTURE_TRANSFER_SCHEMA
from ..sources.solidworks.discover_stage import (
    NATIVE_OBSERVATION_SCHEMA,
    NativeDiscoverRefusal,
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
from .stage_transfer import CAPTURE_ARCHIVE

MAX_OPERATION_JSON_BYTES = 8 * 1024**2
MAX_OPERATION_ARCHIVE_BYTES = 16 * 1024**3
_LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})
_TERMINAL_STATES = frozenset({"satisfied", "failed", "cancelled", "unknown"})
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
        if view.state != "satisfied" or artifact is None or not view.resource_terminated:
            raise NativeOperationClientError(
                "native operation has no satisfied artifact",
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
    "NativeOperationEndpoint",
    "NativeOperationNotFound",
    "NativeOperationProtocolError",
    "NativeOperationRemoteUnknown",
    "NativeOperationToolProvider",
    "NativeOperationWaitTimeout",
    "NativeRemoteDiscoverAdapter",
    "NativeToolIdentity",
    "RemoteNativeCaptureResult",
    "capture_handler_from_native_operation",
    "discover_adapter_from_native_operation",
    "native_capture_operation_id",
    "observe_native_operation",
]
