"""Trusted per-request context for the Airflow api-server.

The packaged Airflow plugin installs one pure-ASGI root middleware that binds the actual request
scope for the duration of a single HTTP request and always resets it in ``finally``. The Feishu
auth manager reads the router-populated endpoint and path parameters from that binding during
authorization, so run ownership is decided from server-owned values only: never from headers,
request bodies, route regexes or any client-supplied field.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

#: The one shared binding object; every consumer imports this module, so two import paths can
#: never end up writing into different context variables.
_DESCRIPTION_PIPELINE_REQUEST: ContextVar[Any] = ContextVar(
    "description_pipeline_airflow_request", default=None
)

#: Trusted scope key holding the evaluated candidate body of the one owner-granted clear route.
CLEAR_BODY_SCOPE_KEY = "description_pipeline_clear_body"
#: Only this one POST route is a candidate; the path merely selects what to buffer, while
#: authorization still runs on the resolved endpoint and router-populated path parameters.
_CLEAR_BODY_LIMIT = 4096
_CLEAR_BODY_FIELDS = frozenset({"dry_run", "only_failed", "only_new", "run_on_latest_version"})


def bound_request() -> Any | None:
    """The Request bound by the root middleware, or ``None`` outside a routed request."""
    return _DESCRIPTION_PIPELINE_REQUEST.get()


@contextmanager
def use_request(request: Any) -> Iterator[None]:
    """Bind one request object for the current context; always reset in ``finally``."""
    token = _DESCRIPTION_PIPELINE_REQUEST.set(request)
    try:
        yield
    finally:
        _DESCRIPTION_PIPELINE_REQUEST.reset(token)


class BindRequestMiddleware:
    """Pure ASGI middleware; importable by every Airflow component, active in the api-server."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        from starlette.requests import Request  # noqa: PLC0415 - kept out of module import cost

        if _selects_clear_candidate(scope):
            receive, candidate, oversize = await _buffer_clear_candidate(receive)
            if oversize:
                # Fail promptly with bounded buffering: the application never reads the rest.
                await _send_oversize(send)
                return
            scope[CLEAR_BODY_SCOPE_KEY] = candidate
        with use_request(Request(scope)):
            await self.app(scope, receive, send)


def _selects_clear_candidate(scope: dict) -> bool:
    """True only for the single-run clear route shape; captures nothing else.

    The middleware wraps the root application, so the real request may carry the stable API
    prefix in ``path``, in ``root_path``, or in neither depending on how the server is mounted.
    """
    path = scope.get("path")
    if scope.get("method") != "POST" or not isinstance(path, str):
        return False
    root_path = scope.get("root_path")
    if isinstance(root_path, str) and root_path and path.startswith(root_path):
        path = path[len(root_path) :]
    if path.startswith("/api/v2"):
        path = path[len("/api/v2") :]
    return path.startswith("/dags/") and "/dagRuns/" in path and path.endswith("/clear")


def _strict_object(pairs: list[tuple[str, Any]]) -> dict:
    """JSON object hook that refuses duplicate keys instead of silently keeping the last one."""
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def evaluate_clear_candidate(raw: bytes, *, oversize: bool) -> dict:
    """Evaluate one candidate clear body against the strict immutable-retry shape.

    The owner grant accepts exactly ``dry_run`` (bool), ``only_failed: true``,
    ``only_new: false`` and ``run_on_latest_version: false`` — every other shape, duplicate or
    unknown field, non-boolean value, malformed JSON or oversized body is unsafe.
    """
    if oversize:
        return {"safe": False}
    try:
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=_strict_object)
    except (UnicodeDecodeError, ValueError):
        return {"safe": False}
    if not isinstance(data, dict) or set(data) != _CLEAR_BODY_FIELDS:
        return {"safe": False}
    if not all(isinstance(value, bool) for value in data.values()):
        return {"safe": False}
    if data["only_failed"] is not True or data["only_new"] is not False:
        return {"safe": False}
    if data["run_on_latest_version"] is not False:
        return {"safe": False}
    return {"safe": True, "dry_run": data["dry_run"]}


async def _buffer_clear_candidate(receive: Any) -> tuple[Any, dict, bool]:
    """Buffer and replay one bounded clear body; downstream still sees the exact bytes."""
    messages: list[dict] = []
    total = 0
    oversize = False
    while True:
        message = await receive()
        if message.get("type") != "http.request":
            messages.append(message)
            break
        body = message.get("body", b"")
        if total + len(body) > _CLEAR_BODY_LIMIT:
            # Checked before buffering, so the middleware never holds more than the limit; the
            # oversize request short-circuits below and this chunk is never replayed.
            oversize = True
            break
        messages.append(message)
        total += len(body)
        if not message.get("more_body", False):
            break
    raw = b"".join(
        message.get("body", b"") for message in messages if message.get("type") == "http.request"
    )
    candidate = evaluate_clear_candidate(raw, oversize=oversize)
    index = 0

    async def replay() -> dict:
        nonlocal index
        if index < len(messages):
            message = messages[index]
            index += 1
            return message
        return await receive()

    return replay, candidate, oversize


async def _send_oversize(send: Any) -> None:
    body = b'{"detail": "clear body exceeds the fixed candidate limit"}'
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
