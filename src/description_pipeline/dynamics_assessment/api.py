"""Small WSGI response adapter; root portal retains sessions, CSRF and routing."""

from __future__ import annotations

import logging
import base64
import re
import zipfile
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import PurePosixPath
from urllib.parse import parse_qs

from ..io import canonical, safe_process_output
from ..pipeline.contracts import ArtifactRef
from .contracts import (
    AssessmentConflict,
    AssessmentDisabled,
    AssessmentError,
    SourceNotReady,
    object_record,
    parse_json,
    require,
)
from .service import AssessmentAccess, AssessmentService

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ApiResponse:
    status: int
    body: bytes
    headers: tuple[tuple[str, str], ...]

    @classmethod
    def json(cls, payload, status=200):
        return cls(
            status,
            canonical(payload) + b"\n",
            (("Content-Type", "application/json; charset=utf-8"), ("Cache-Control", "no-store")),
        )

    def wsgi(self, start_response):
        start_response(
            f"{self.status} {HTTPStatus(self.status).phrase}",
            [*self.headers, ("Content-Length", str(len(self.body))), ("X-Content-Type-Options", "nosniff")],
        )
        return [self.body]


class AssessmentApi:
    """Context factory must perform existing root session/CSRF/source authorization.

    ``context(environ, mutating)`` returns an AssessmentAccess and a transport
    bound to that same authenticated Airflow session. No token is persisted.
    """

    def __init__(self, service: AssessmentService, context, *, runtime_reconciler=None):
        self.service = service
        self.context = context
        self.runtime_reconciler = runtime_reconciler

    @staticmethod
    def _body(environ, *, empty=False, max_bytes=128 * 1024):
        value = environ.get("CONTENT_LENGTH", "0")
        require(isinstance(value, str) and value.isdecimal(), "Assessment content length is invalid")
        length = int(value)
        require(length <= max_bytes, "Assessment request body is too large")
        if length == 0 and empty:
            return {}
        data = environ["wsgi.input"].read(length)
        require(len(data) == length, "Assessment request body is incomplete")
        return parse_json(data)

    def handle(self, environ, *, action: str, identity: str) -> ApiResponse:
        mutating = action in {"bundle", "create", "retry", "cancel", "reconcile", "dispatch"}
        try:
            method = "POST" if mutating else "GET"
            if environ.get("REQUEST_METHOD") != method:
                return ApiResponse.json({"error": "Assessment method is not allowed"}, 405)
            access, transport = self.context(environ, mutating)
            require(isinstance(access, AssessmentAccess), "Authenticated assessment context is missing")
            if action == "list":
                return ApiResponse.json(self.service.list_for_source(access, identity))
            if action == "source":
                self.service._enabled()
                source = self.service._source(access, identity, control=False)
                return ApiResponse(
                    200,
                    canonical(source.to_record()) + b"\n",
                    (
                        ("Content-Type", "application/json; charset=utf-8"),
                        ("Cache-Control", "no-store"),
                        ("Content-Disposition", 'attachment; filename="assessment-source.json"'),
                    ),
                )
            if action == "get":
                return ApiResponse.json(self.service.get(access, identity))
            if action == "bundle":

                def read_archive(limit):
                    body = object_record(
                        self._body(environ, max_bytes=4 * ((limit + 2) // 3) + 256),
                        {"archive_base64"},
                        "Execution archive upload",
                    )
                    require(isinstance(body["archive_base64"], str), "Execution archive encoding is invalid")
                    return base64.b64decode(body["archive_base64"], validate=True)

                cancelled = environ.get("description.assessment_upload_cancelled", lambda: False)
                require(callable(cancelled), "Execution upload cancellation observer is invalid")
                return ApiResponse.json(
                    self.service.upload_bundle(access, identity, read_archive, should_cancel=cancelled), 201
                )
            if action == "create":
                payload = object_record(self._body(environ), {"bundle"}, "Assessment submission")
                bundle = ArtifactRef.from_record(payload["bundle"]) if payload["bundle"] is not None else None
                result = self.service.create(
                    access, identity, environ.get("HTTP_IDEMPOTENCY_KEY", ""), bundle, transport
                )
                return ApiResponse.json(result, 202)
            if action in {"retry", "cancel", "reconcile", "dispatch"}:
                require(
                    not self._body(environ, empty=True), "Assessment control has no client-supplied identity fields"
                )
                if action == "retry":
                    result = self.service.retry(access, identity, transport)
                elif action == "cancel":
                    result = self.service.cancel(access, identity)
                elif action == "dispatch":
                    result = self.service.dispatch_registered(access, identity, transport)
                else:
                    result = self.service.reconcile(access, identity, transport, self.runtime_reconciler)
                return ApiResponse.json(result, 202)
            if action == "result":
                query = parse_qs(environ.get("QUERY_STRING", ""), keep_blank_values=True)
                require(
                    not set(query) - {"path"} and len(query.get("path", [""])) == 1,
                    "Assessment result query is invalid",
                )
                path = query.get("path", ["assessment_result.json"])[0]
                data, _member = self.service.read_result(access, identity, path)
                filename = re.sub(r"[^A-Za-z0-9_.-]", "_", PurePosixPath(path).name)
                content_type = (
                    "application/json; charset=utf-8" if path.endswith(".json") else "application/octet-stream"
                )
                return ApiResponse(
                    200,
                    data,
                    (
                        ("Content-Type", content_type),
                        ("Cache-Control", "no-store"),
                        ("Content-Disposition", f'attachment; filename="{filename}"'),
                    ),
                )
            return ApiResponse.json({"error": "Assessment route does not exist"}, 404)
        except PermissionError as error:
            return ApiResponse.json({"error": str(error)}, 403)
        except KeyError:
            return ApiResponse.json({"error": "Assessment does not exist or is not visible"}, 404)
        except (AssessmentConflict, AssessmentDisabled, SourceNotReady) as error:
            return ApiResponse.json({"error": str(error)}, 409)
        except (AssessmentError, ValueError, TypeError, zipfile.BadZipFile) as error:
            return ApiResponse.json({"error": str(error)}, 400)
        except Exception as error:
            status = getattr(error, "status", None)
            message = getattr(error, "message", None)
            if isinstance(status, int) and 400 <= status <= 599 and isinstance(message, str):
                # Preserve the existing portal's authentication/source error protocol.
                return ApiResponse.json({"error": message}, status)
            log.error("Assessment request failed: %s", safe_process_output(str(error))["text"])
            return ApiResponse.json({"error": "Assessment service is unavailable"}, 503)
