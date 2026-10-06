"""Client for the bearer-authenticated Windows SolidWorks execution endpoint.

The endpoint contract is fixed and carries no shell or local paths:

* ``POST /v1/jobs`` with ``{run_id, package, revision_sha256, target}`` (idempotent per run_id;
  a different payload for the same run_id is HTTP 409);
* ``GET /v1/jobs/<uuid>`` returns status/events/result/error;
* ``GET /health`` exposes only ``pipeline_id`` and readiness.

Plaintext HTTP is allowed only for loopback hosts; anything remote requires TLS.
"""

from __future__ import annotations

import json
import math
import re
import time
import uuid
from dataclasses import dataclass
from collections.abc import Callable
from pathlib import PurePosixPath
from typing import Any
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest

from ..delivery import PIPELINE_ID
from ..io import PipelineError, artifact_path_parts

JOB_SCHEMA = "solidworks-to-urdf.job/v1"
EVENT_KEYS = ("stage", "state", "at")
RUN_STATES = {"queued", "running", "passed", "failed"}
TERMINAL_STATES = {"passed", "failed"}
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_COMMIT = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_PULL_URL = re.compile(r"https://github\.com/[^/\s]+/[^/\s]+/pull/[1-9]\d*\Z")
_REVIEW_BRANCH = re.compile(r"work/solidworks/[a-z0-9_.-]+\Z")
SUBMISSION_STATES = {"published", "updated", "noop"}
_LOOPBACK = {"127.0.0.1", "localhost", "::1"}


class EndpointError(RuntimeError):
    """Transport or protocol failure while talking to the execution endpoint."""


class EndpointAuthError(EndpointError):
    """The endpoint rejected the bearer token."""


class EndpointConflict(EndpointError):
    """The run_id is already bound to a different request payload."""


class EndpointProtocolError(EndpointError):
    """The endpoint returned an unexpected shape or value."""


class JobFailed(EndpointError):
    """The remote job reached the failed state."""

    def __init__(self, run_id: str, job: dict[str, Any]) -> None:
        super().__init__(f"job {run_id} failed: {job.get('error') or 'unknown error'}")
        self.run_id = run_id
        self.job = job


class ResultNotPublishable(EndpointError):
    """A passed job whose result is missing quality or PR evidence."""


def validate_run_id(value: str) -> str:
    try:
        canonical = str(uuid.UUID(str(value)))
    except (ValueError, AttributeError, TypeError) as error:
        raise EndpointProtocolError(f"run_id is not a UUID: {value!r}") from error
    if str(value) != canonical:
        raise EndpointProtocolError(f"run_id must be the canonical lowercase UUID: {value!r}")
    return canonical


def validate_package(value: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise EndpointProtocolError("package must be a POSIX relative path")
    try:
        artifact_path_parts(value)
    except PipelineError as error:
        raise EndpointProtocolError("package must be a portable relative path") from error
    path = PurePosixPath(value)
    if path.is_absolute() or any(segment in {"", ".", ".."} for segment in value.split("/")):
        raise EndpointProtocolError(f"package must stay inside the configured root: {value!r}")
    return path.as_posix()


def validate_revision_sha(value: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise EndpointProtocolError("revision_sha256 must be a lowercase SHA-256 digest")
    return value


def _validate_base_url(url: str) -> str:
    try:
        parsed = urlparse.urlsplit(url)
        _ = parsed.port
    except ValueError as error:
        raise EndpointProtocolError("Invalid endpoint address") from error
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise EndpointProtocolError("Endpoint URL must use absolute HTTP or HTTPS")
    if parsed.scheme == "http" and parsed.hostname not in _LOOPBACK:
        raise EndpointProtocolError(
            "plaintext HTTP is only allowed for loopback; use TLS or an SSH tunnel for remote endpoints"
        )
    if parsed.username or parsed.password:
        raise EndpointProtocolError("credentials must not be embedded in the endpoint URL")
    if parsed.query or parsed.fragment:
        raise EndpointProtocolError("Endpoint URL cannot contain a query or fragment")
    return url.rstrip("/")


@dataclass(frozen=True)
class EndpointConfig:
    base_url: str
    token: str
    timeout: float = 30.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "base_url", _validate_base_url(self.base_url))
        if not self.token:
            raise EndpointProtocolError("a bearer token is required")
        if not math.isfinite(self.timeout) or self.timeout <= 0:
            raise EndpointProtocolError("Endpoint timeout must be finite and positive")


def config_from_airflow_connection(conn_id: str, *, timeout: float = 30.0) -> EndpointConfig:
    """Build the endpoint config from an Airflow connection (host/port/password/extra)."""
    from airflow.hooks.base import BaseHook

    connection = BaseHook.get_connection(conn_id)
    host = (connection.host or "").strip()
    if not host:
        raise EndpointProtocolError(f"Airflow connection {conn_id!r} has no host")
    if not host.startswith(("http://", "https://")):
        port = f":{connection.port}" if connection.port else ""
        host = f"http://{host}{port}"
    extra = connection.extra_dejson or {}
    return EndpointConfig(base_url=host, token=connection.password or "", timeout=float(extra.get("timeout", timeout)))


class WindowsEndpoint:
    """Small JSON client; injectable opener keeps it testable without network."""

    def __init__(
        self,
        config: EndpointConfig,
        *,
        opener: Callable[..., Any] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self._opener = opener or _opener
        self._sleep = sleeper
        self._clock = clock

    def _request(self, method: str, path: str, payload: dict | None = None) -> dict:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urlrequest.Request(
            self.config.base_url + path,
            data=body,
            method=method,
            headers={
                "Authorization": f"Bearer {self.config.token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with self._opener(request, timeout=self.config.timeout) as response:
                data = json.loads(response.read().decode("utf-8") or "{}")
        except urlerror.HTTPError as error:
            if error.code in {401, 403}:
                raise EndpointAuthError(f"endpoint rejected the bearer token ({error.code})") from error
            if error.code == 409:
                raise EndpointConflict(f"run_id already bound to a different request ({error.code})") from error
            raise EndpointError(f"endpoint returned HTTP {error.code}") from error
        except urlerror.URLError as error:
            raise EndpointError(f"endpoint unreachable: {error.reason}") from error
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise EndpointProtocolError("Endpoint returned invalid UTF-8 JSON") from error
        if not isinstance(data, dict):
            raise EndpointProtocolError("endpoint response is not a JSON object")
        return data

    def health(self) -> dict:
        payload = self._request("GET", "/health")
        if payload.get("pipeline_id") != PIPELINE_ID:
            raise EndpointProtocolError(f"unexpected pipeline_id: {payload.get('pipeline_id')!r}")
        if "ready" not in payload and "readiness" not in payload:
            raise EndpointProtocolError("health response must include readiness")
        return payload

    def start_job(self, *, run_id: str, package: str, revision_sha256: str, target: str) -> dict:
        payload = {
            "run_id": validate_run_id(run_id),
            "package": validate_package(package),
            "revision_sha256": validate_revision_sha(revision_sha256),
            "target": str(target).strip(),
        }
        if not payload["target"]:
            raise EndpointProtocolError("target must be a configured repository alias")
        response = self._request("POST", "/v1/jobs", payload)
        if response.get("run_id") != payload["run_id"]:
            raise EndpointProtocolError("endpoint returned a different run_id")
        if response.get("status") not in RUN_STATES:
            raise EndpointProtocolError(f"unexpected job status: {response.get('status')!r}")
        if response.get("request") != payload:
            raise EndpointProtocolError("Endpoint accepted a different mechanical handoff")
        return response

    def get_job(self, run_id: str) -> dict:
        job = self._request("GET", f"/v1/jobs/{validate_run_id(run_id)}")
        if job.get("run_id") != run_id:
            raise EndpointProtocolError("endpoint returned a different run_id")
        if job.get("schema_version") != JOB_SCHEMA:
            raise EndpointProtocolError(f"unexpected job schema: {job.get('schema_version')!r}")
        if job.get("pipeline_id") != PIPELINE_ID:
            raise EndpointProtocolError(f"unexpected pipeline_id: {job.get('pipeline_id')!r}")
        if not str(job.get("repository_slug") or "").strip():
            raise EndpointProtocolError("job must persist the configured repository_slug")
        if not str(job.get("repository_base") or "").strip():
            raise EndpointProtocolError("job must persist the configured repository_base")
        if job.get("status") not in RUN_STATES:
            raise EndpointProtocolError(f"unexpected job status: {job.get('status')!r}")
        if not isinstance(job.get("events"), list):
            raise EndpointProtocolError("job response must include an events list")
        for event in job["events"]:
            if not isinstance(event, dict) or any(key not in event for key in EVENT_KEYS):
                raise EndpointProtocolError("Events must carry stage, state and timestamp")
        return job

    def wait(self, run_id: str, *, interval: float = 2.0, timeout: float = 600.0) -> dict:
        """Bounded polling; returns the job dict only for the terminal passed state."""
        deadline = self._clock() + timeout
        while True:
            job = self.get_job(run_id)
            status = job["status"]
            if status == "passed":
                return job
            if status == "failed":
                raise JobFailed(run_id, job)
            if self._clock() >= deadline:
                raise EndpointError(f"job {run_id} did not finish within {timeout:.0f}s")
            self._sleep(interval)


def check_result(result: dict | None, *, expected_slug: str, expected_base: str) -> dict:
    """Fail closed unless the passed job carries bound quality and publication evidence."""
    if not isinstance(result, dict):
        raise ResultNotPublishable("passed job has no result payload")
    if result.get("passed") is not True:
        raise ResultNotPublishable("result.passed is not true")
    if result.get("pipeline_id") != PIPELINE_ID:
        raise ResultNotPublishable(f"result.pipeline_id must be {PIPELINE_ID}")
    subject = result.get("subject_sha256")
    if not isinstance(subject, str) or _SHA256.fullmatch(subject) is None:
        raise ResultNotPublishable("result.subject_sha256 is missing")
    quality = result.get("quality")
    if not isinstance(quality, dict) or quality.get("passed") is not True:
        raise ResultNotPublishable("result.quality is missing or not passed")
    if quality.get("subject_sha256") != subject:
        raise ResultNotPublishable("result.quality.subject_sha256 differs from the run subject")
    submission = result.get("submission")
    if not isinstance(submission, dict) or submission.get("passed") is not True:
        raise ResultNotPublishable("result.submission is missing or not passed")
    if submission.get("subject_sha256") != subject:
        raise ResultNotPublishable("result.submission.subject_sha256 differs from the run subject")
    if submission.get("state") not in SUBMISSION_STATES:
        raise ResultNotPublishable(f"result.submission.state must be one of {sorted(SUBMISSION_STATES)}")
    if not isinstance(submission.get("base"), str) or not submission["base"].strip():
        raise ResultNotPublishable("result.submission.base is missing")
    if not isinstance(submission.get("commit"), str) or _COMMIT.fullmatch(submission["commit"]) is None:
        raise ResultNotPublishable("result.submission.commit must be a hex commit")
    url = str(submission.get("url") or "")
    if _PULL_URL.fullmatch(url) is None:
        raise ResultNotPublishable("result.submission.url must be an exact GitHub pull URL")
    if not expected_slug or submission.get("repository_slug") != expected_slug:
        raise ResultNotPublishable("repository_slug does not match the expected origin slug")
    if not url.startswith(f"https://github.com/{expected_slug}/pull/"):
        raise ResultNotPublishable("pull URL does not match the configured repository slug")
    if submission.get("base") != expected_base:
        raise ResultNotPublishable("submission.base does not match the expected base branch")
    if not isinstance(submission.get("branch"), str) or _REVIEW_BRANCH.fullmatch(submission["branch"]) is None:
        raise ResultNotPublishable("submission.branch must be the deterministic work/solidworks/<hardware> branch")
    return result


class _SameHostRedirect(urlrequest.HTTPRedirectHandler):
    """Never forward the bearer token across hosts."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        try:
            old = urlparse.urlsplit(req.full_url)
            new = urlparse.urlsplit(newurl)
            same_origin = (old.scheme, old.hostname, old.port) == (new.scheme, new.hostname, new.port)
        except ValueError:
            same_origin = False
        if not same_origin:
            raise urlerror.HTTPError(newurl, code, "cross-host redirect refused", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urlrequest.build_opener(_SameHostRedirect())


def _opener(request: urlrequest.Request, timeout: float):
    return _OPENER.open(request, timeout=timeout)
