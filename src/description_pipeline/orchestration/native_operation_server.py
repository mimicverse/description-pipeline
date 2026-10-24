"""HTTP surface for isolated Windows native operations."""

from __future__ import annotations

import hmac
import json
import os
import shutil
import tempfile
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

from ..delivery import PIPELINE_ID
from ..io import PipelineError, canonical, digest, parse_data
from .native_operation_runtime import NativeOperationError, NativeOperationRuntime

HEALTH_SCHEMA = "description-pipeline.native-operation-health/v1"
MAX_JSON_BYTES = 8 * 1024**2
MAX_ARCHIVE_BYTES = 16 * 1024**3


def _health(runtime: NativeOperationRuntime) -> dict:
    stats = runtime.stats()
    native_tool = None
    native_tool_error = None
    try:
        native_tool = dict(runtime.native_tool_factory())
        canonical(native_tool)
    except Exception as error:
        native_tool_error = f"{type(error).__name__}: {error}"
    native_tool_sha256 = None if native_tool is None else digest(native_tool)
    identity_matches = (
        native_tool is not None
        and native_tool.get("pipeline_id") == PIPELINE_ID
    )
    return {
        "schema_version": HEALTH_SCHEMA,
        "pipeline_id": PIPELINE_ID,
        "ready": (
            runtime.active
            and stats["unresolved"] == 0
            and identity_matches
        ),
        "install_id": runtime.install_id,
        "native_tool_sha256": native_tool_sha256,
        "native_tool_error": native_tool_error,
        "operations": stats,
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
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(payload)
            self.close_connection = True

        def _authorized(self) -> bool:
            received = self.headers.get("Authorization", "")
            if not hmac.compare_digest(received.encode("utf-8"), ("Bearer " + token).encode("ascii")):
                self._reply(401, {"error": "Unauthorized"})
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
            if not self._authorized():
                return
            try:
                path = self._route_path()
                if path == "/health":
                    self._reply(200, _health(runtime))
                    return
                if path.startswith("/operations/"):
                    suffix = path[len("/operations/") :]
                    if suffix.endswith("/artifact"):
                        operation_id = unquote(suffix[: -len("/artifact")])
                        path, metadata = runtime.artifact_path(operation_id)
                        with path.open("rb") as stream:
                            self.send_response(200)
                            self.send_header("Content-Type", "application/zip")
                            self.send_header("Content-Length", str(path.stat().st_size))
                            self.send_header(
                                "Content-Disposition",
                                "attachment; filename*=UTF-8''" + quote(metadata["name"], safe=""),
                            )
                            self.send_header("X-Artifact-SHA256", metadata["sha256"])
                            self.send_header("Cache-Control", "private, no-store")
                            self.send_header("Connection", "close")
                            self.end_headers()
                            shutil.copyfileobj(stream, self.wfile, length=1024 * 1024)
                        self.close_connection = True
                        return
                    self._reply(200, runtime.view(unquote(suffix)))
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
                            result = runtime.admit_input(operation_id, archive)
                        finally:
                            archive.unlink(missing_ok=True)
                        self._reply(202, result)
                        return
                    if suffix.endswith("/cancel"):
                        if self.headers.get("Content-Length", "0") != "0":
                            raise NativeOperationError("Cancellation requests do not carry a body")
                        operation_id = unquote(suffix[: -len("/cancel")])
                        self._reply(202, runtime.cancel(operation_id))
                        return
                self._reply(404, {"error": "Unknown API route"})
            except (NativeOperationError, OSError, ValueError, TypeError) as error:
                self._failure(error)

    return Handler


__all__ = ["HEALTH_SCHEMA", "MAX_ARCHIVE_BYTES", "MAX_JSON_BYTES", "native_operation_handler"]
