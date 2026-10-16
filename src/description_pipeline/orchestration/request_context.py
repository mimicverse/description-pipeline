"""Trusted per-request context for the Airflow api-server.

The packaged Airflow plugin installs one pure-ASGI root middleware that binds the actual request
scope for the duration of a single HTTP request and always resets it in ``finally``. The Feishu
auth manager reads the resolved endpoint and path parameters during authorization. A bounded
request body selects a parent run; ownership comes exclusively from that run's stored actor.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

#: The one shared binding object; every consumer imports this module, so two import paths can
#: never end up writing into different context variables.
_DESCRIPTION_PIPELINE_REQUEST: ContextVar[Any] = ContextVar("description_pipeline_airflow_request", default=None)

#: Trusted scope key holding the evaluated candidate body of the one owner-granted clear route.
CLEAR_BODY_SCOPE_KEY = "description_pipeline_clear_body"
CREATE_BODY_SCOPE_KEY = "description_pipeline_create_body"
#: Only this one POST route is a candidate; the path merely selects what to buffer, while
#: authorization still runs on the resolved endpoint and router-populated path parameters.
_CLEAR_BODY_LIMIT = 4096
_CLEAR_BODY_FIELDS = frozenset({"dry_run", "only_failed", "only_new", "run_on_latest_version"})
#: The create body carries exactly the submission frame the DAG understands: the
#: frozen intake path, the frozen request identity the DAG intake requires, the
#: optional linked-attempt linkage, the selected main assembly and the publish
#: target with its multi-line change summary.  The recorded submitter identity is
#: never a create-body field: ownership comes from the run's stored actor and
#: the Airflow-authenticated actor, never from a client-provided identity claim.
_CREATE_FIELDS = frozenset(
    {
        "handoff_path",
        "parent_dag_run_id",
        "resume_from",
        "target_hardware",
        "change_summary",
        "main_assembly",
        # The portal-issued frozen request identity of one submission
        # (``portal-<dag_run_id>``); the DAG intake refuses a run without it.
        "request_id",
    }
)
_CREATE_SINGLE_LINE_FIELDS = ("handoff_path", "target_hardware", "main_assembly", "request_id")
_CREATE_SINGLE_LINE_LIMIT = 1024
_CREATE_SUMMARY_LIMIT = 2048


def _safe_single_line(value: object) -> bool:
    """One bounded, control-character-free line of submission frame text."""

    return (
        isinstance(value, str)
        and 0 < len(value) <= _CREATE_SINGLE_LINE_LIMIT
        and all(ord(char) >= 32 for char in value)
    )


def _safe_change_summary(value: object) -> bool:
    """The frozen identity contract: LF stays inside a summary, every other control character refuses.

    Line endings are folded to LF first, exactly like the portal and the DAG
    intake fold them, so a CRLF submission keeps its multi-line shape.
    """

    if not isinstance(value, str):
        return False
    folded = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not folded or len(folded) > _CREATE_SUMMARY_LIMIT:
        return False
    return all(ord(char) >= 32 or char == "\n" for char in folded)


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
        elif _selects_create_candidate(scope):
            receive, candidate, oversize = await _buffer_candidate(receive, evaluate_create_candidate)
            if oversize:
                await _send_oversize(send)
                return
            scope[CREATE_BODY_SCOPE_KEY] = candidate
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


def _selects_create_candidate(scope: dict) -> bool:
    path = scope.get("path")
    if scope.get("method") != "POST" or not isinstance(path, str):
        return False
    root = scope.get("root_path")
    if isinstance(root, str) and root and path.startswith(root):
        path = path[len(root) :]
    if path.startswith("/api/v2"):
        path = path[len("/api/v2") :]
    return path.startswith("/dags/") and path.endswith("/dagRuns")


def evaluate_create_candidate(raw: bytes, *, oversize: bool) -> dict:
    """Parse a requested lineage selector, never a client-provided owner identity."""
    from ..stages import STAGE_IDS

    if oversize:
        return {"safe": False}
    try:
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=_strict_object)
    except (UnicodeDecodeError, ValueError):
        return {"safe": False}
    if not isinstance(data, dict):
        return {"safe": False}
    conf = data.get("conf")
    if conf is None:
        conf = {}
    if not isinstance(conf, dict) or set(conf) - _CREATE_FIELDS:
        return {"safe": False}
    for name in _CREATE_SINGLE_LINE_FIELDS:
        if name in conf and not _safe_single_line(conf[name]):
            return {"safe": False}
    if "change_summary" in conf and not _safe_change_summary(conf["change_summary"]):
        return {"safe": False}
    if "parent_dag_run_id" not in conf and "resume_from" not in conf:
        return {"safe": True, "parent_dag_run_id": None}
    parent, stage = conf.get("parent_dag_run_id"), conf.get("resume_from")
    if (
        not isinstance(parent, str)
        or not parent
        or len(parent) > 250
        or any(ord(char) < 32 for char in parent)
        or parent == "~"
        or not isinstance(stage, str)
        or stage not in STAGE_IDS
    ):
        return {"safe": False}
    return {"safe": True, "parent_dag_run_id": parent}


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
    return await _buffer_candidate(receive, evaluate_clear_candidate)


async def _buffer_candidate(receive: Any, evaluate: Any) -> tuple[Any, dict, bool]:
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
    raw = b"".join(message.get("body", b"") for message in messages if message.get("type") == "http.request")
    candidate = evaluate(raw, oversize=oversize)
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
    body = b'{"detail": "run request body exceeds the fixed candidate limit"}'
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
