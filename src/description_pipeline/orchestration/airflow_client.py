"""Client for the bearer-authenticated Windows SolidWorks execution endpoint.

The endpoint contract is fixed and carries no shell or local paths:

* ``POST /v1/handoffs/resolve`` with ``{handoff_path}`` resolves one operator-supplied folder to
  the managed package and its complete-package digest; an absolute POSIX path is archived locally
  and sent to ``POST /v1/handoffs/import`` (``application/zip``) only when it is link-free and
  inside the platform ``handoff_roots`` allowlist from the Airflow connection;
* ``POST /v1/jobs`` with ``{run_id, package, handoff_sha256}`` starts one run (idempotent per
  run_id; a different payload for the same run_id is HTTP 409); an optional ``main_assembly``
  names the delivered assembly explicitly. Hardware, revision and repository routing resolve
  inside the serialized Windows job after CAD discovery;
* ``GET /v1/jobs/<uuid>`` returns status/events/result/error and, for native runs, the routing the
  job resolved after discovery;
* ``GET /v1/jobs/<uuid>/preview`` and ``GET /v1/jobs/<uuid>/artifacts/<name>`` expose the verified
  delivery for the operator portal;
* ``GET /health`` exposes only ``pipeline_id`` and readiness.

Plaintext HTTP is allowed only for loopback hosts; anything remote requires TLS.
"""

from __future__ import annotations

import json
import hashlib
import math
import os
import re
import tempfile
import time
import unicodedata
import uuid
from dataclasses import dataclass
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest

from ..delivery import PIPELINE_ID
from ..io import PipelineError, artifact_path_parts
from ..sources.solidworks.handoff import HANDOFF_SCHEMA
from ..stages import STAGE_IDS

JOB_SCHEMA = "solidworks-to-urdf.job/v1"
NATIVE_RUN_NAMESPACE = "solidworks_to_urdf"
EVENT_KEYS = ("stage", "state", "at")
RUN_STATES = {"queued", "running", "passed", "failed"}
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_COMMIT = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_PULL_URL = re.compile(r"https://github\.com/[^/\s]+/[^/\s]+/pull/[1-9]\d*\Z")
_REVIEW_BRANCH = re.compile(r"work/solidworks/[a-z0-9_.-]+\Z")
_HARDWARE_ID = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,63}\Z")
SUBMISSION_STATES = {"published", "updated", "noop"}
_LOOPBACK = {"127.0.0.1", "localhost", "::1"}
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class EndpointError(RuntimeError):
    """Transport or protocol failure while talking to the execution endpoint."""


class EndpointAuthError(EndpointError):
    """The endpoint rejected the bearer token."""


class EndpointConflict(EndpointError):
    """The run_id is already bound to a different request payload."""


class EndpointProtocolError(EndpointError):
    """The endpoint returned an unexpected shape or value."""


class EndpointNotFound(EndpointError):
    """The endpoint has no record of the requested run, job or route."""


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


def native_run_id(dag_run_id: str) -> str:
    """The canonical native job UUID for one Airflow DAG run id (stable across retries)."""
    if not isinstance(dag_run_id, str) or not dag_run_id.strip():
        raise EndpointProtocolError("dag_run_id must be a non-empty string")
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{NATIVE_RUN_NAMESPACE}:{dag_run_id}"))


def validate_resume(value: object, *, run_id: str | None = None) -> dict:
    """One linked-run binding: the parent native UUID and the canonical restart stage."""
    if not isinstance(value, dict) or set(value) != {"parent_run", "from_stage"}:
        raise EndpointProtocolError("resume must carry exactly parent_run and from_stage")
    parent = validate_run_id(value.get("parent_run"))
    stage = value.get("from_stage")
    if stage not in STAGE_IDS:
        raise EndpointProtocolError(f"from_stage is not a canonical engineering stage: {stage!r}")
    if run_id is not None and parent == run_id:
        raise EndpointProtocolError("a linked run cannot reuse itself")
    return {"parent_run": parent, "from_stage": stage}


def _validate_relative_path(value: str, field: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise EndpointProtocolError(f"{field} must be a POSIX relative path")
    try:
        artifact_path_parts(value)
    except PipelineError as error:
        raise EndpointProtocolError(f"{field} must be a portable relative path") from error
    path = PurePosixPath(value)
    if path.is_absolute() or any(segment in {"", ".", ".."} for segment in value.split("/")):
        raise EndpointProtocolError(f"{field} must stay inside the delivery: {value!r}")
    return path.as_posix()


#: One explicit delivered-assembly selection shares the member-path bound.
MAX_MAIN_ASSEMBLY = 1024


def validate_main_assembly(value: object) -> str:
    """One explicit delivered assembly: canonical POSIX path inside the handoff root.

    The value is the operator's authoritative entry selection (the top engineering
    folder is excluded). It names a saved SolidWorks assembly and must match a
    frozen file exactly; the endpoint re-verifies that before any CAD runs.
    """

    if not isinstance(value, str) or not value or len(value) > MAX_MAIN_ASSEMBLY:
        raise EndpointProtocolError("main_assembly must be a non-empty relative path")
    if value != value.strip():
        raise EndpointProtocolError("main_assembly must not carry leading or trailing whitespace")
    if unicodedata.normalize("NFC", value) != value:
        raise EndpointProtocolError("main_assembly must be NFC-normalized")
    _validate_relative_path(value, "main_assembly")
    if not value.casefold().endswith(".sldasm"):
        raise EndpointProtocolError("main_assembly must name a saved SolidWorks assembly (.SLDASM)")
    return value


def validate_package(value: str) -> str:
    return _validate_relative_path(value, "package")


def validate_artifact_name(value: str) -> str:
    return _validate_relative_path(value, "artifact name")


def _validate_digest(value: str, field: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise EndpointProtocolError(f"{field} must be a lowercase SHA-256 digest")
    return value


def validate_handoff_sha(value: str) -> str:
    return _validate_digest(value, "handoff_sha256")


def validate_sha256(value: str) -> str:
    return _validate_digest(value, "sha256")


def validate_handoff_path(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EndpointProtocolError("handoff_path must be a non-empty path")
    path = value.strip()
    if _CONTROL.search(path):
        raise EndpointProtocolError("handoff_path must not contain control characters")
    return path


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
    handoff_roots: tuple[Path, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "base_url", _validate_base_url(self.base_url))
        if not self.token:
            raise EndpointProtocolError("a bearer token is required")
        if not math.isfinite(self.timeout) or self.timeout <= 0:
            raise EndpointProtocolError("Endpoint timeout must be finite and positive")
        object.__setattr__(self, "handoff_roots", _handoff_roots(self.handoff_roots))


def _handoff_roots(values) -> tuple[Path, ...]:
    """Validate the platform allowlist with the shared handoff boundary rules."""
    if values is None or values == () or values == "":
        return ()
    try:
        from ..sources.solidworks.handoff import validate_handoff_roots

        return validate_handoff_roots(values)
    except (ImportError, PipelineError, TypeError, ValueError, OSError) as error:
        raise EndpointProtocolError(str(error)) from error


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
    return EndpointConfig(
        base_url=host,
        token=connection.password or "",
        timeout=float(extra.get("timeout", timeout)),
        handoff_roots=extra.get("handoff_roots") or (),
    )


@dataclass(frozen=True)
class HandoffResolution:
    """One operator path resolved to the mechanical handoff the endpoint will run.

    The resolution carries only the managed package and its handoff digest; hardware, revision and
    repository routing are resolved inside the serialized Windows job after CAD discovery.
    """

    package: str
    handoff_sha256: str

    @classmethod
    def from_payload(cls, payload: dict) -> HandoffResolution:
        if not isinstance(payload, dict):
            raise EndpointProtocolError("handoff resolution is not a JSON object")
        expected = {"schema_version", "pipeline_id", "package", "handoff_sha256"}
        if set(payload) != expected:
            raise EndpointProtocolError(f"handoff resolution must carry exactly {sorted(expected)}")
        if payload.get("schema_version") != HANDOFF_SCHEMA:
            raise EndpointProtocolError(f"unexpected handoff schema: {payload.get('schema_version')!r}")
        if payload.get("pipeline_id") != PIPELINE_ID:
            raise EndpointProtocolError(f"unexpected pipeline_id: {payload.get('pipeline_id')!r}")
        return cls(
            package=validate_package(payload.get("package")),
            handoff_sha256=validate_handoff_sha(payload.get("handoff_sha256")),
        )


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

    def _exchange(self, request: urlrequest.Request) -> dict:
        try:
            with self._opener(request, timeout=self.config.timeout) as response:
                data = json.loads(response.read().decode("utf-8") or "{}")
        except urlerror.HTTPError as error:
            if error.code in {401, 403}:
                raise EndpointAuthError(f"endpoint rejected the bearer token ({error.code})") from error
            if error.code == 404:
                raise EndpointNotFound("endpoint has no record of the requested resource") from error
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

    def _request(self, method: str, path: str, payload: dict | None = None) -> dict:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        return self._exchange(
            urlrequest.Request(
                self.config.base_url + path,
                data=body,
                method=method,
                headers={
                    "Authorization": f"Bearer {self.config.token}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
            )
        )

    def _request_stream(self, method: str, path: str, source: Any, *, size: int) -> dict:
        """Stream one file body with a fixed Content-Length (no chunked upload)."""
        return self._exchange(
            urlrequest.Request(
                self.config.base_url + path,
                data=source,
                method=method,
                headers={
                    "Authorization": f"Bearer {self.config.token}",
                    "Content-Type": "application/zip",
                    "Accept": "application/json",
                    "Content-Length": str(size),
                },
            )
        )

    def _authorized_handoff_dir(self, path: str) -> Path:
        """Authorize one absolute Linux folder against the platform handoff_roots allowlist."""
        if not self.config.handoff_roots:
            raise EndpointProtocolError(
                "absolute Linux handoff folders require handoff_roots in the Airflow endpoint connection"
            )
        try:
            from ..sources.solidworks.handoff import authorize_handoff

            return authorize_handoff(Path(path), self.config.handoff_roots)
        except (ImportError, PipelineError, TypeError, ValueError, OSError) as error:
            raise EndpointProtocolError(str(error)) from error

    def resolve_handoff(self, handoff_path: str) -> HandoffResolution:
        """Resolve one operator path; an authorized absolute POSIX folder is ZIP-imported first."""
        path = validate_handoff_path(handoff_path)
        if os.name == "posix" and PurePosixPath(path).is_absolute():
            return self._import_handoff(self._authorized_handoff_dir(path))
        return HandoffResolution.from_payload(self._request("POST", "/v1/handoffs/resolve", {"handoff_path": path}))

    def _import_handoff(self, source: Path) -> HandoffResolution:
        from ..sources.solidworks.handoff import prepare_archive

        if not source.is_dir():
            raise EndpointProtocolError(f"handoff_path is not a directory: {source}")
        with tempfile.TemporaryDirectory(prefix="handoff-import-") as tmp:
            archive = Path(tmp) / "handoff.zip"
            identity = prepare_archive(source, archive)
            size = archive.stat().st_size
            if size <= 0:
                raise EndpointProtocolError("handoff archive is empty")
            with archive.open("rb") as handle:
                payload = self._request_stream("POST", "/v1/handoffs/import", handle, size=size)
        resolved = HandoffResolution.from_payload(payload)
        expected = str((identity or {}).get("handoff_sha256") or "")
        if resolved.handoff_sha256 != expected:
            raise EndpointProtocolError("endpoint stored a different handoff digest than the uploaded archive")
        return resolved

    def health(self) -> dict:
        payload = self._request("GET", "/health")
        if payload.get("pipeline_id") != PIPELINE_ID:
            raise EndpointProtocolError(f"unexpected pipeline_id: {payload.get('pipeline_id')!r}")
        if "ready" not in payload and "readiness" not in payload:
            raise EndpointProtocolError("health response must include readiness")
        return payload

    def start_job(
        self,
        *,
        run_id: str,
        resolution: HandoffResolution,
        resume: dict | None = None,
        main_assembly: str | None = None,
    ) -> dict:
        """Start one native run with exactly the resolved package and its handoff digest.

        ``resume`` links the new job to a terminal parent (``parent_run`` native UUID and the
        canonical ``from_stage``); the endpoint revalidates the parent, its checkpoints and
        the retained upload before accepting it.  ``main_assembly`` names the delivered
        assembly explicitly; the endpoint verifies it against the frozen handoff.
        """
        if not isinstance(resolution, HandoffResolution):
            raise EndpointProtocolError("start_job requires a resolved handoff")
        payload = {
            "run_id": validate_run_id(run_id),
            "package": resolution.package,
            "handoff_sha256": resolution.handoff_sha256,
        }
        if resume is not None:
            payload["resume"] = validate_resume(resume, run_id=payload["run_id"])
        if main_assembly is not None:
            payload["main_assembly"] = validate_main_assembly(main_assembly)
        response = self._request("POST", "/v1/jobs", payload)
        if response.get("run_id") != payload["run_id"]:
            raise EndpointProtocolError("endpoint returned a different run_id")
        if response.get("status") not in RUN_STATES:
            raise EndpointProtocolError(f"unexpected job status: {response.get('status')!r}")
        if response.get("request") != payload:
            raise EndpointProtocolError("Endpoint accepted a different mechanical handoff")
        return response

    def rerun_plan(self, run_id: str) -> dict:
        """Authoritative per-stage restart plan of one terminal native job.

        The endpoint probes its own retained checkpoints, dependency snapshot and
        publication target; the portal must not recompute eligibility locally.
        """
        canonical = validate_run_id(run_id)
        payload = self._request("GET", f"/v1/jobs/{canonical}/reruns")
        if payload.get("run_id") != canonical:
            raise EndpointProtocolError("rerun plan returned a different run_id")
        if payload.get("status") not in RUN_STATES:
            raise EndpointProtocolError(f"unexpected rerun plan status: {payload.get('status')!r}")
        rows = payload.get("stage_reruns")
        if not isinstance(rows, list) or [row.get("stage") for row in rows if isinstance(row, dict)] != list(STAGE_IDS):
            raise EndpointProtocolError("rerun plan must cover every engineering stage exactly once")
        for row in rows:
            if not isinstance(row, dict) or any(
                key not in row
                for key in (
                    "stage",
                    "name_zh",
                    "eligible",
                    "reason",
                    "reason_zh",
                    "recomputes",
                    "retains",
                    "prerequisites",
                    "target_changed",
                )
            ):
                raise EndpointProtocolError("rerun plan row is incomplete")
            if not isinstance(row["eligible"], bool) or not isinstance(row["recomputes"], list):
                raise EndpointProtocolError("rerun plan row carries invalid eligibility or recompute stages")
        return payload

    def get_job(self, run_id: str) -> dict:
        job = self._request("GET", f"/v1/jobs/{validate_run_id(run_id)}")
        if job.get("run_id") != run_id:
            raise EndpointProtocolError("endpoint returned a different run_id")
        if job.get("schema_version") != JOB_SCHEMA:
            raise EndpointProtocolError(f"unexpected job schema: {job.get('schema_version')!r}")
        if job.get("pipeline_id") != PIPELINE_ID:
            raise EndpointProtocolError(f"unexpected pipeline_id: {job.get('pipeline_id')!r}")
        for field in ("repository_slug", "repository_base"):
            value = job.get(field)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise EndpointProtocolError(f"job {field} must be non-empty text when present")
        if job.get("status") not in RUN_STATES:
            raise EndpointProtocolError(f"unexpected job status: {job.get('status')!r}")
        if not isinstance(job.get("events"), list):
            raise EndpointProtocolError("job response must include an events list")
        for event in job["events"]:
            if not isinstance(event, dict) or any(key not in event for key in EVENT_KEYS):
                raise EndpointProtocolError("Events must carry stage, state and timestamp")
        return job

    def get_preview(self, run_id: str) -> dict:
        """Preview metadata of one passed, bound delivery (operator portal entry point)."""
        canonical = validate_run_id(run_id)
        preview = self._request("GET", f"/v1/jobs/{canonical}/preview")
        if preview.get("pipeline_id") != PIPELINE_ID:
            raise EndpointProtocolError(f"unexpected pipeline_id: {preview.get('pipeline_id')!r}")
        if preview.get("run_id") != canonical:
            raise EndpointProtocolError("preview returned a different run_id")
        subject = preview.get("subject_sha256")
        if not isinstance(subject, str) or _SHA256.fullmatch(subject) is None:
            raise EndpointProtocolError("preview must carry the subject_sha256 digest")
        urdf = validate_artifact_name(preview.get("urdf"))
        files = preview.get("files")
        if not isinstance(files, dict) or not files:
            raise EndpointProtocolError("preview must list the delivered files")
        for name, digest in files.items():
            validate_artifact_name(name)
            if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
                raise EndpointProtocolError("preview file digests must be lowercase SHA-256")
        if urdf not in files:
            raise EndpointProtocolError("preview urdf must be one of the delivered files")
        return preview

    def open_artifact(self, run_id: str, name: str) -> Any:
        """Open one verified delivery artifact for streaming; the caller closes the response."""
        canonical = validate_run_id(run_id)
        artifact = validate_artifact_name(name)
        encoded = urlparse.quote(artifact, safe="/")
        request = urlrequest.Request(
            f"{self.config.base_url}/v1/jobs/{canonical}/artifacts/{encoded}",
            method="GET",
            headers={"Authorization": f"Bearer {self.config.token}", "Accept": "*/*"},
        )
        try:
            return self._opener(request, timeout=self.config.timeout)
        except urlerror.HTTPError as error:
            if error.code in {401, 403}:
                raise EndpointAuthError(f"endpoint rejected the bearer token ({error.code})") from error
            if error.code == 404:
                raise EndpointNotFound(f"artifact {artifact!r} is not part of run {canonical}") from error
            raise EndpointError(f"endpoint returned HTTP {error.code}") from error
        except urlerror.URLError as error:
            raise EndpointError(f"endpoint unreachable: {error.reason}") from error

    def read_artifact(self, run_id: str, name: str, *, sha256: str, limit: int = 64 * 1024 * 1024) -> bytes:
        """Read one artifact into memory and require its exact digest before returning it."""
        expected = validate_sha256(sha256)
        if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
            raise EndpointProtocolError("artifact size limit must be a positive integer")
        with self.open_artifact(run_id, name) as response:
            declared = response.headers.get("Content-Length")
            if declared is not None:
                try:
                    size = int(declared)
                except (TypeError, ValueError) as error:
                    raise EndpointProtocolError("artifact Content-Length is not an integer") from error
                if size < 0 or size > limit:
                    raise EndpointProtocolError(f"artifact exceeds the {limit}-byte limit")
            data = bytearray()
            while True:
                chunk = response.read(65536)
                if not chunk:
                    break
                data.extend(chunk)
                if len(data) > limit:
                    raise EndpointProtocolError(f"artifact exceeds the {limit}-byte limit")
        digest = hashlib.sha256(bytes(data)).hexdigest()
        if digest != expected:
            raise EndpointProtocolError(f"artifact {name!r} does not match the preview digest")
        return bytes(data)

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


def verified_result(result: dict | None) -> dict:
    """Fail closed unless the run carries an independently verified model result.

    Qualification covers the delivered model only: the pipeline identity, the run subject and a
    passing, subject-bound quality document. Whole-job and publication success are separate
    gates (:func:`check_result`); a PR service failure must not hide an otherwise verified
    delivery, and a failed or unverified model must never be shown.
    """
    if not isinstance(result, dict):
        raise ResultNotPublishable("job has no model result payload")
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
    return result


def check_result(result: dict | None, *, expected_slug: str, expected_base: str) -> dict:
    """Fail closed unless the passed job carries bound quality and publication evidence.

    The publication gate stays strict: it requires the whole job's ``passed`` flag in addition
    to the model qualification from :func:`verified_result` and a subject-bound, passing
    submission for the configured repository.
    """
    result = verified_result(result)
    if result.get("passed") is not True:
        raise ResultNotPublishable("result.passed is not true")
    subject = result["subject_sha256"]
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


def resolved_routing(job: dict) -> dict:
    """The routing a native job resolved inside the serialized Windows execution.

    A native handoff cannot name hardware, revision or destination before CAD discovery, so the
    passed job snapshot must carry the routing it resolved instead of the operator request. The
    repository target is keyed directly by the native hardware identity, so no alias is carried.
    """
    if not isinstance(job, dict):
        raise EndpointProtocolError("job is not a JSON object")
    routing = {}
    for field in ("hardware_id", "revision", "repository_slug", "repository_base"):
        value = job.get(field)
        if not isinstance(value, str) or not value.strip() or _CONTROL.search(value):
            raise EndpointProtocolError(f"native job has not resolved {field}")
        routing[field] = value.strip()
    if _HARDWARE_ID.fullmatch(routing["hardware_id"]) is None:
        raise EndpointProtocolError("native job resolved a non-ASCII hardware_id")
    if "/" not in routing["repository_slug"]:
        raise EndpointProtocolError("native job resolved a repository_slug without an owner")
    return routing


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


# Internal service calls use direct connections, independent of ambient proxy settings.
_OPENER = urlrequest.build_opener(urlrequest.ProxyHandler({}), _SameHostRedirect())


def _opener(request: urlrequest.Request, timeout: float):
    return _OPENER.open(request, timeout=timeout)
