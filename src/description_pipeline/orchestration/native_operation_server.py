"""HTTP surface for isolated Windows native operations."""

from __future__ import annotations

import hmac
import json
import os
import shutil
import sys
import tempfile
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

from ..delivery import PIPELINE_ID
from ..interface_records import (
    InterfaceRecordError,
    resource_policy_v1_digest,
    validate_health,
)
from ..io import PipelineError, canonical, digest, parse_data
from ..schemas import (
    HEALTH_SCHEMA,
    NATIVE_TOOL_IDENTITY_SCHEMA,
    RESOURCE_POLICY_SCHEMA,
)
from ..stages import CONTRACT, CONTRACT_SHA256
from .native_operation_runtime import NativeOperationError, NativeOperationRuntime

MAX_JSON_BYTES = 8 * 1024**2
MAX_ARCHIVE_BYTES = 16 * 1024**3


def _health(runtime: NativeOperationRuntime) -> dict:
    stats = runtime.stats()
    try:
        native_tool = dict(runtime.native_tool_factory())
        canonical(native_tool)
    except Exception as error:
        raise NativeOperationError(
            "The native tool identity is unavailable",
            status=503,
            code="native_health_identity_unavailable",
            detail={"error_type": type(error).__name__},
        ) from error
    embedded_release = native_tool.get("release")
    if not isinstance(embedded_release, dict):
        embedded_release = {}
    release = {
        "version": str(native_tool.get("version") or ""),
        "source_commit": str(embedded_release.get("source_commit") or ""),
        "source_sha256": str(native_tool.get("source_sha256") or ""),
        "role": "native",
    }
    native_platform = sys.platform == "win32"
    problems: list[str] = []
    if not native_platform:
        problems.append("unsupported_platform")
    if native_tool.get("pipeline_id") != PIPELINE_ID:
        problems.append("native_tool_pipeline_mismatch")
    if not runtime.active:
        problems.append("native_worker_inactive")
    if runtime.endpoint_config_digest is None:
        problems.append("endpoint_config_digest_unavailable")
    if runtime.endpoint_port is None:
        problems.append("endpoint_port_unavailable")
    if not all(
        (
            release["version"],
            release["source_commit"],
            release["source_sha256"],
        )
    ):
        problems.append("native_release_identity_unavailable")

    cad = {
        "external_session_detected": False,
        "owned_sessions": 0,
        "lease_held": False,
    }
    if native_platform:
        try:
            from ..sources.solidworks import isolation

            cad = isolation.cad_session_facts(
                isolation.registered_executable(),
                ownership_file=runtime.cad_ownership_file,
            )
        except Exception:
            problems.append("cad_state_unavailable")
        else:
            problems.extend(str(item) for item in cad.get("problems", ()))
        try:
            from ..runtime import native_readiness

            native_readiness()
        except Exception:
            problems.append("native_readiness_failed")

    usage = shutil.disk_usage(runtime.capacity_root)
    native_ok = native_platform and not any(
        item != "unsupported_platform" for item in problems
    )
    ready = (
        native_ok
        and not cad["external_session_detected"]
        and stats["unresolved"] == 0
    )
    if ready:
        reason_code = None
    elif problems:
        reason_code = problems[0]
    elif cad["external_session_detected"]:
        reason_code = "external_cad_session"
    else:
        reason_code = "unresolved_operations"
    payload = {
        "schema_version": HEALTH_SCHEMA,
        "pipeline_id": PIPELINE_ID,
        "native_platform": native_platform,
        "ready": ready,
        "release": release,
        "contract": {
            "schema_version": CONTRACT["schema_version"],
            "digest": CONTRACT_SHA256,
        },
        "resource_policy": {
            "schema_version": RESOURCE_POLICY_SCHEMA,
            "digest": resource_policy_v1_digest(),
        },
        "install_id": runtime.install_id,
        "config_digest": runtime.endpoint_config_digest,
        "target_identity": runtime.target_identity.to_record(),
        "endpoint": {"port": runtime.endpoint_port},
        "cad": {
            "external_session_detected": bool(
                cad.get("external_session_detected", False)
            ),
            "owned_sessions": int(cad.get("owned_sessions", 0)),
            "lease_held": bool(cad.get("lease_held", False)),
        },
        "jobs": {
            "active": stats["active"],
            "queued": stats["queued"],
            "unresolved": stats["unresolved"],
        },
        "capacity": {
            "free_bytes": int(usage.free),
            "reserved_bytes": 0,
        },
        "readiness": {
            "reason_code": reason_code,
            "native": {
                "ok": native_ok,
                "problems": problems,
            },
        },
    }
    try:
        validate_health(payload)
    except InterfaceRecordError as error:
        raise NativeOperationError(
            "The native endpoint cannot report its complete health identity",
            status=503,
            code="native_health_identity_invalid",
            detail={"reason": str(error)},
        ) from error
    return payload


def _native_tool_identity(runtime: NativeOperationRuntime) -> dict:
    tool = dict(runtime.native_tool_factory())
    canonical(tool)
    return {
        "schema_version": NATIVE_TOOL_IDENTITY_SCHEMA,
        "tool": tool,
        "tool_sha256": digest(tool),
    }


def native_operation_handler(runtime: NativeOperationRuntime, token: str):
    if not isinstance(runtime, NativeOperationRuntime):
        raise TypeError("runtime must be a NativeOperationRuntime")
    if not isinstance(token, str) or len(token) < 32:
        raise ValueError("token must contain at least 32 characters")

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format, *args):
            return

        def _reply(self, status: int, data: dict) -> None:
            payload = (json.dumps(data, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header(
                "X-Native-Target-ID",
                runtime.target_identity.target_id,
            )
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(payload)
            self.close_connection = True

        def _authorized(self, *, require_target: bool = True) -> bool:
            received = self.headers.get("Authorization", "")
            if not hmac.compare_digest(received.encode("utf-8"), ("Bearer " + token).encode("ascii")):
                self._reply(401, {"error": "Unauthorized"})
                return False
            if require_target:
                target_id = self.headers.get("X-Native-Target-ID", "")
                if not hmac.compare_digest(
                    target_id.encode("utf-8"),
                    runtime.target_identity.target_id.encode("ascii"),
                ):
                    self._reply(
                        409,
                        {
                            "error": "Native target mismatch",
                            "error_code": "native_target_mismatch",
                        },
                    )
                    return False
            return True

        def _failure(self, error: Exception) -> None:
            if isinstance(error, NativeOperationError):
                self._reply(
                    error.status,
                    {
                        "error": str(error),
                        "error_code": error.code,
                        "detail": error.detail,
                    },
                )
                return
            self._reply(500, {"error": type(error).__name__, "error_code": "native_operation_server_error"})

        def _json_body(self) -> dict:
            content_type = self.headers.get("Content-Type", "").split(";")[0].strip()
            if content_type != "application/json":
                raise NativeOperationError("Content-Type must be application/json")
            if self.headers.get("Transfer-Encoding"):
                raise NativeOperationError("Transfer-Encoding is not accepted")
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError as error:
                raise NativeOperationError("Content-Length must be an integer") from error
            if length <= 0 or length > MAX_JSON_BYTES:
                raise NativeOperationError("Invalid JSON request size", status=413)
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise NativeOperationError("Incomplete JSON request")
            try:
                value = parse_data(raw, Path("operation-request.json"))
            except PipelineError as error:
                raise NativeOperationError("Request body is not valid JSON") from error
            if not isinstance(value, dict):
                raise NativeOperationError("Request body must be a JSON object")
            return value

        def _archive_body(self) -> Path:
            content_type = self.headers.get("Content-Type", "").split(";")[0].strip()
            if content_type != "application/zip":
                raise NativeOperationError("Content-Type must be application/zip")
            if self.headers.get("Transfer-Encoding"):
                raise NativeOperationError("Transfer-Encoding is not accepted")
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError as error:
                raise NativeOperationError("Content-Length must be an integer") from error
            if length <= 0 or length > MAX_ARCHIVE_BYTES:
                raise NativeOperationError("Invalid archive size", status=413)
            self.connection.settimeout(60)
            descriptor, name = tempfile.mkstemp(prefix=".operation-input-", dir=runtime.runtime_root)
            path = Path(name)
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    remaining = length
                    while remaining:
                        chunk = self.rfile.read(min(remaining, 1024 * 1024))
                        if not chunk:
                            raise NativeOperationError("Input archive transport was interrupted")
                        stream.write(chunk)
                        remaining -= len(chunk)
                    stream.flush()
                    os.fsync(stream.fileno())
                return path
            except BaseException:
                path.unlink(missing_ok=True)
                raise

        def _route_path(self) -> str:
            parsed = urlsplit(self.path)
            if parsed.query or parsed.fragment or parsed.scheme or parsed.netloc:
                raise NativeOperationError("Native operation routes do not accept a URL query or fragment")
            return parsed.path

        def do_GET(self) -> None:
            try:
                path = self._route_path()
                if path == "/health":
                    if not self._authorized(require_target=False):
                        return
                    self._reply(200, _health(runtime))
                    return
                if not self._authorized():
                    return
                if path == "/native-tool":
                    self._reply(200, _native_tool_identity(runtime))
                    return
                if path == "/target":
                    self._reply(
                        200,
                        {
                            **runtime.target_identity.to_record(),
                            "install_id": runtime.install_id,
                            "pipeline_id": runtime.target_identity.pipeline_id,
                        },
                    )
                    return
                if path.startswith("/operations/"):
                    suffix = path[len("/operations/") :]
                    if suffix.endswith("/artifact"):
                        operation_id = unquote(suffix[: -len("/artifact")])
                        path, metadata = runtime.artifact_path(
                            operation_id,
                            expected_target_id=self.headers.get(
                                "X-Native-Target-ID"
                            ),
                        )
                        with path.open("rb") as stream:
                            self.send_response(200)
                            self.send_header("Content-Type", "application/zip")
                            self.send_header("Content-Length", str(path.stat().st_size))
                            self.send_header(
                                "Content-Disposition",
                                "attachment; filename*=UTF-8''" + quote(metadata["name"], safe=""),
                            )
                            self.send_header("X-Artifact-SHA256", metadata["sha256"])
                            self.send_header(
                                "X-Native-Target-ID",
                                runtime.target_identity.target_id,
                            )
                            self.send_header("Cache-Control", "private, no-store")
                            self.send_header("Connection", "close")
                            self.end_headers()
                            shutil.copyfileobj(stream, self.wfile, length=1024 * 1024)
                        self.close_connection = True
                        return
                    self._reply(
                        200,
                        runtime.view(
                            unquote(suffix),
                            expected_target_id=self.headers.get(
                                "X-Native-Target-ID"
                            ),
                        ),
                    )
                    return
                self._reply(404, {"error": "Unknown API route"})
            except (NativeOperationError, OSError, ValueError, TypeError) as error:
                self._failure(error)

        def do_POST(self) -> None:
            if not self._authorized():
                return
            try:
                path = self._route_path()
                if path == "/operations":
                    self._reply(202, runtime.create(self._json_body()))
                    return
                prefix = "/operations/"
                if path.startswith(prefix):
                    suffix = path[len(prefix) :]
                    if suffix.endswith("/input"):
                        operation_id = unquote(suffix[: -len("/input")])
                        archive = self._archive_body()
                        try:
                            result = runtime.admit_input(
                                operation_id,
                                archive,
                                expected_target_id=self.headers.get(
                                    "X-Native-Target-ID"
                                ),
                            )
                        finally:
                            archive.unlink(missing_ok=True)
                        self._reply(202, result)
                        return
                    if suffix.endswith("/cancel"):
                        if self.headers.get("Content-Length", "0") != "0":
                            raise NativeOperationError("Cancellation requests do not carry a body")
                        operation_id = unquote(suffix[: -len("/cancel")])
                        self._reply(
                            202,
                            runtime.cancel(
                                operation_id,
                                expected_target_id=self.headers.get(
                                    "X-Native-Target-ID"
                                ),
                            ),
                        )
                        return
                self._reply(404, {"error": "Unknown API route"})
            except (NativeOperationError, OSError, ValueError, TypeError) as error:
                self._failure(error)

    return Handler


__all__ = [
    "HEALTH_SCHEMA",
    "MAX_ARCHIVE_BYTES",
    "MAX_JSON_BYTES",
    "NATIVE_TOOL_IDENTITY_SCHEMA",
    "native_operation_handler",
]
