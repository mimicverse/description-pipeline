"""Freeze through a Windows worker instead of calling COM locally.

The build host has no SolidWorks.  With ``source.worker_url`` set, ``freeze``
submits a job to the worker over a loopback tunnel, follows it, downloads the
immutable package and verifies it before the snapshot is accepted.

A job that outlives the client's patience is left running: the error carries the
job id and last stage, and calling freeze again with ``job_id`` attaches to the
same job instead of starting a second capture.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import shutil
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any
from collections.abc import Callable

from .errors import BridgeError

TERMINAL_STATES = ("succeeded", "failed", "cancelled")
DEFAULT_POLL_SECONDS = 2.0
DEFAULT_JOB_TIMEOUT = 3600.0


def _shared_verify(root: Path) -> dict[str, Any]:
    from ..snapshot import verify_snapshot

    return verify_snapshot(root)


class WorkerClient:
    """Minimal HTTP client for the worker job API (loopback by default)."""

    def __init__(
        self,
        base_url: str,
        *,
        allow_remote: bool = False,
        http_timeout: float = 60.0,
        poll_seconds: float = DEFAULT_POLL_SECONDS,
    ) -> None:
        parsed = urllib.parse.urlparse(base_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise BridgeError("invalid_worker_url", f"worker_url must be an http(s) URL: {base_url!r}")
        host = (parsed.hostname or "").lower()
        if host not in ("127.0.0.1", "localhost", "::1") and not allow_remote:
            raise BridgeError(
                "worker_url_not_loopback",
                "worker_url must point at a loopback tunnel; set allow_remote_worker to override",
                {"worker_url": base_url},
            )
        self.base_url = base_url.rstrip("/")
        self.http_timeout = http_timeout
        self.poll_seconds = poll_seconds
        handlers = [urllib.request.ProxyHandler({})] if host in ("127.0.0.1", "localhost", "::1") else []
        self._opener = urllib.request.build_opener(*handlers)

    # -- transport -------------------------------------------------------

    def _request(self, path: str, *, method: str = "GET", payload: object = None, raw: bool = False) -> Any:
        url = f"{self.base_url}{path}"
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with self._opener.open(request, timeout=self.http_timeout) as response:
                body = response.read()
                if raw:
                    return body, response.headers.get("Content-Type", "")
                if not body:
                    return {}
                return json.loads(body.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:2000]
            try:
                parsed = json.loads(detail)
                error = parsed.get("error") or {}
            except json.JSONDecodeError:
                error = {"message": detail}
            raise BridgeError(
                "worker_http_error",
                f"worker returned HTTP {exc.code} for {path}",
                {"status": exc.code, "error": error, "path": path},
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise BridgeError(
                "worker_unreachable",
                f"cannot reach the worker at {self.base_url}: {exc}",
                {"worker_url": self.base_url, "path": path},
            ) from exc

    # -- API -------------------------------------------------------------

    def health(self) -> dict[str, Any]:
        return dict(self._request("/health"))

    def doctor(self, assembly: str | None = None, configuration: str | None = None) -> dict[str, Any]:
        query = []
        if assembly:
            query.append("assembly=" + urllib.parse.quote(assembly))
        if configuration:
            query.append("configuration=" + urllib.parse.quote(configuration))
        suffix = "?" + "&".join(query) if query else ""
        return dict(self._request("/doctor" + suffix))

    def submit(self, request: dict[str, Any]) -> dict[str, Any]:
        return dict(self._request("/jobs", method="POST", payload=request))

    def job(self, job_id: str) -> dict[str, Any]:
        return dict(self._request(f"/jobs/{job_id}"))

    def cancel(self, job_id: str) -> dict[str, Any]:
        return dict(self._request(f"/jobs/{job_id}/cancel", method="POST", payload={}))

    def manifest(self, job_id: str) -> dict[str, Any]:
        return dict(self._request(f"/jobs/{job_id}/manifest"))

    def download_package(self, job_id: str, *, attempts: int = 3) -> bytes:
        """Download the job's package, retrying briefly on 5xx responses.

        A worker that has just committed a snapshot may still be flushing the
        last file, so a single 5xx is not treated as a permanent failure.
        """

        last: BridgeError | None = None
        for attempt in range(1, attempts + 1):
            try:
                body, _content_type = self._request(f"/jobs/{job_id}/package", raw=True)
                return bytes(body)
            except BridgeError as exc:
                detail = exc.detail if isinstance(exc.detail, dict) else {}
                status = detail.get("status")
                if exc.code != "worker_http_error" or not isinstance(status, int) or status < 500:
                    raise
                last = exc
                time.sleep(min(2.0, 0.2 * attempt))
        assert last is not None
        raise last

    def wait(
        self,
        job_id: str,
        *,
        timeout: float = DEFAULT_JOB_TIMEOUT,
        on_progress: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """Follow a job until it finishes; a timeout keeps the job running."""

        deadline = time.monotonic() + timeout
        last: dict[str, Any] = {}
        while True:
            last = self.job(job_id)
            if on_progress is not None:
                on_progress(last)
            state = str(last.get("state"))
            if state in TERMINAL_STATES:
                break
            if time.monotonic() >= deadline:
                raise BridgeError(
                    "worker_job_timeout",
                    "the worker job is still running; it was not cancelled and can be resumed",
                    {
                        "job_id": job_id,
                        "state": state,
                        "stage": last.get("stage"),
                        "progress": last.get("progress"),
                        "resume": {"worker_url": self.base_url, "job_id": job_id},
                    },
                )
            time.sleep(self.poll_seconds)
        if last["state"] != "succeeded":
            raise BridgeError(
                "worker_job_failed",
                f"worker job ended in state {last['state']}",
                {"job_id": job_id, "error": last.get("error"), "progress": last.get("progress")},
            )
        return last

    def fetch_snapshot(
        self,
        job_id: str,
        destination: Path,
        *,
        expected: dict[str, Any] | None = None,
        evidence_class: str | None = None,
        timeout: float = 600.0,
    ) -> dict[str, Any]:
        """Download, verify and install a snapshot; nothing is trusted blindly.

        Everything is checked before a single byte reaches ``destination``: the
        package has to extract safely, re-hash to its own manifest, declare the
        source that was requested and carry the evidence class this capture
        claims.  A failure keeps the package and the safely-extracted tree beside
        the destination instead of deleting them, so a mismatch can be inspected
        rather than re-run.
        """

        destination = Path(destination)
        existing_empty = _prepare_destination(destination)
        destination = destination.resolve()
        payload = self.download_package(job_id)
        staging = destination.with_name(destination.name + ".partial")
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir(parents=True)
        installed = False
        stage = "extract"
        try:
            _extract_tar(payload, staging, timeout=timeout)
            stage = "verify"
            root = _snapshot_root(staging)
            manifest = _shared_verify(root)
            if manifest.get("kind") != "solidworks":
                raise BridgeError(
                    "snapshot_kind_mismatch",
                    "downloaded snapshot is not a SolidWorks capture",
                    {"kind": manifest.get("kind"), "job_id": job_id},
                )
            _check_snapshot_source(manifest, expected, evidence_class, job_id)
            stage = "install"
            if existing_empty:
                destination.rmdir()
            os.replace(root, destination)
            installed = True
            shutil.rmtree(staging, ignore_errors=True)
            return manifest
        except BaseException as error:
            retained = None if installed else _retain_failure(staging, destination, payload, error, stage, job_id)
            if retained and isinstance(error, BridgeError):
                # the caller has to be able to say where the evidence is, and the
                # original error is re-raised unchanged
                detail = dict(error.detail) if isinstance(error.detail, dict) else {"detail": error.detail}
                detail["diagnostic_path"] = retained
                error.detail = detail
                error.diagnostic_path = retained  # type: ignore[attr-defined]
            raise


def _same_assembly(left: str, right: str) -> bool:
    """Windows path comparison: separators and case do not name a different file."""

    def normalise(value: str) -> str:
        return value.replace("\\", "/").rstrip("/").lower()

    return normalise(left) == normalise(right)


def _same_configuration(left: str, right: str) -> bool:
    """Configuration names are compared exactly, like the native freeze does.

    ``Default`` and ``default`` are two different configurations in SolidWorks, so
    normalising case here would let a snapshot of the wrong model state through.
    """

    return left == right


def _check_snapshot_source(
    manifest: dict[str, Any],
    expected: dict[str, Any] | None,
    evidence_class: str | None,
    job_id: str,
) -> None:
    """Bind a downloaded snapshot to the capture that was actually requested."""

    if evidence_class is not None and manifest.get("evidence_class") != evidence_class:
        raise BridgeError(
            "snapshot_evidence_class_mismatch",
            "the worker's snapshot is not the kind of evidence this source asked for",
            {"job_id": job_id, "expected": evidence_class, "actual": manifest.get("evidence_class")},
        )
    if not expected:
        return
    identity = manifest.get("identity")
    if not isinstance(identity, dict) or not identity:
        raise BridgeError(
            "snapshot_source_mismatch",
            "the snapshot declares no source identity to check the request against",
            {"job_id": job_id},
        )
    problems = []
    for field, compare in (("assembly", _same_assembly), ("configuration", _same_configuration)):
        wanted = expected.get(field)
        if wanted is None:
            continue
        actual = identity.get(field)
        if not isinstance(actual, str) or not compare(str(wanted), actual):
            problems.append({"field": field, "requested": wanted, "snapshot": actual})
    if problems:
        raise BridgeError(
            "snapshot_source_mismatch",
            "the worker's snapshot is not the source that was requested",
            {"job_id": job_id, "fields": problems},
        )


def _retain_failure(
    staging: Path,
    destination: Path,
    payload: bytes,
    error: BaseException,
    stage: str,
    job_id: str,
) -> str | None:
    """Keep what the worker sent instead of deleting the evidence.

    The extracted tree was never verified, so it stays out of ``destination``; it
    is parked next to it together with the raw package, which is the only local
    record of what the worker actually produced.
    """

    if not staging.exists():
        return None
    record = {
        "schema_version": "description-pipeline.solidworks-remote-failure/v1",
        "stage": stage,
        "code": getattr(error, "code", type(error).__name__),
        "message": str(error),
        "detail": getattr(error, "detail", None),
        "job_id": job_id,
        "destination": str(destination),
        "package_bytes": len(payload),
        "package_sha256": hashlib.sha256(payload).hexdigest(),
    }
    for index in range(1, 1001):
        candidate = destination.with_name(f"{destination.name}.failed-{index:03d}")
        try:
            candidate.mkdir(parents=True)
        except FileExistsError:
            continue
        except OSError:
            return None
        moved = staging
        try:
            moved = candidate / "partial"
            os.replace(staging, moved)
        except OSError:
            moved = staging
        with contextlib.suppress(OSError):
            (candidate / "package.tar").write_bytes(payload)
        with contextlib.suppress(OSError):
            (candidate / "failure.json").write_text(
                json.dumps({**record, "partial": str(moved)}, ensure_ascii=False, indent=2, default=str) + "\n",
                encoding="utf-8",
                newline="\n",
            )
        return str(candidate)
    return None


def _prepare_destination(destination: Path) -> bool:
    """Same contract as the local freeze: missing or empty, nothing else."""

    if destination.is_symlink():
        raise BridgeError(
            "destination_is_symlink",
            "refusing to write a snapshot through a symlink",
            {"destination": str(destination)},
            exit_code=1,
        )
    if not destination.exists():
        return False
    if not destination.is_dir():
        raise BridgeError(
            "destination_not_a_directory",
            "the snapshot destination exists and is not a directory",
            {"destination": str(destination)},
            exit_code=1,
        )
    if any(destination.iterdir()):
        raise BridgeError(
            "destination_not_empty",
            "the snapshot destination must be empty",
            {"destination": str(destination)},
            exit_code=1,
        )
    return True


def _snapshot_root(staging: Path) -> Path:
    """Find the snapshot inside an extracted package by its manifest."""

    if (staging / "manifest.json").is_file():
        return staging
    candidates = [entry for entry in staging.iterdir() if entry.is_dir() and (entry / "manifest.json").is_file()]
    if len(candidates) == 1:
        return candidates[0]
    raise BridgeError(
        "snapshot_package_layout",
        "downloaded package does not contain exactly one snapshot manifest",
        {"staging": str(staging), "candidates": [entry.name for entry in candidates]},
    )


def _tar_member_problem(member: tarfile.TarInfo) -> str | None:
    """Return why a tar member is unsafe, or ``None`` when it is acceptable."""

    name = member.name
    normalised = name.replace("\\", "/")
    if not normalised or normalised in (".", "./"):
        return "empty member name"
    if normalised.startswith(("/", "//")):
        return "absolute path"
    if len(normalised) >= 2 and normalised[1] == ":":
        return "windows drive-letter path"
    if normalised.startswith(("//?/", "//./")):
        return "windows device path"
    if ".." in normalised.split("/"):
        return "path escapes the package"
    from ...io import PipelineError, artifact_path_parts

    try:
        artifact_path_parts(normalised.rstrip("/"))
    except PipelineError as error:
        return str(error)
    if member.issym() or member.islnk():
        return "link"
    if member.ischr() or member.isblk() or member.isfifo():
        return "device or fifo"
    if not (member.isfile() or member.isdir()):
        return f"unsupported member type {member.type!r}"
    return None


def _extract_tar(payload: bytes, target: Path, *, timeout: float) -> None:
    """Extract a tar safely: plain files and directories only, no duplicates."""

    target = Path(target).resolve()
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r") as archive:
        members = archive.getmembers()
        seen: set[str] = set()
        for member in members:
            problem = _tar_member_problem(member)
            if problem is not None:
                raise BridgeError(
                    "snapshot_package_unsafe",
                    f"downloaded package contains an unsafe member: {problem}",
                    {"member": member.name, "problem": problem},
                )
            key = str(Path(member.name.replace("\\", "/").rstrip("/"))).casefold()
            if key in seen:
                raise BridgeError(
                    "snapshot_package_unsafe",
                    "downloaded package contains a duplicate member",
                    {"member": member.name},
                )
            seen.add(key)
        deadline = time.monotonic() + timeout
        for member in members:
            if time.monotonic() > deadline:
                raise BridgeError("snapshot_download_timeout", "extracting the package took too long")
            archive.extract(member, target, set_attrs=False, filter="data")


def freeze_via_worker(
    config: dict[str, Any],
    destination: Path,
    *,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Submit (or resume) a freeze job and install the verified snapshot."""

    worker_url = str(config.get("worker_url") or "")
    if not worker_url:
        raise BridgeError("missing_worker_url", "freeze_via_worker needs source.worker_url")
    client = WorkerClient(
        worker_url,
        allow_remote=bool(config.get("allow_remote_worker", False)),
        http_timeout=float(config.get("worker_http_timeout_seconds") or 60.0),
        poll_seconds=float(config.get("worker_poll_seconds") or DEFAULT_POLL_SECONDS),
    )
    remote_config_expected = {
        key: value
        for key, value in config.items()
        if key
        not in (
            "worker_url",
            "allow_remote_worker",
            "worker_http_timeout_seconds",
            "worker_poll_seconds",
            "worker_job_timeout_seconds",
            "job_id",
        )
    }
    job_id = config.get("job_id")
    resolved_job_id: str
    if job_id:
        # Recovery path: attach to a job that was already submitted - but only
        # when it really captured *this* source config.  Otherwise a stale job
        # would be re-labelled with a new lock.
        job = client.job(str(job_id))
        request = job.get("request")
        if not isinstance(request, dict):
            raise BridgeError(
                "worker_job_unverifiable",
                "the worker did not return the request for this job; cannot verify the source config",
                {"job_id": job_id},
            )
        if request.get("kind") != "freeze":
            raise BridgeError(
                "worker_job_kind_mismatch",
                "the resumed job is not a freeze job",
                {"job_id": job_id, "kind": request.get("kind")},
            )
        remote_config = request.get("config")
        if not isinstance(remote_config, dict) or remote_config != remote_config_expected:
            raise BridgeError(
                "worker_job_config_mismatch",
                "the resumed job was submitted with a different source config",
                {
                    "job_id": job_id,
                    "remote": remote_config,
                    "requested": remote_config_expected,
                },
            )
        if job.get("state") == "failed":
            raise BridgeError(
                "worker_job_failed",
                "the resumed job had already failed",
                {"job_id": job_id, "error": job.get("error")},
            )
        resolved_job_id = str(job_id)
    else:
        timeout_seconds = config.get("worker_job_timeout_seconds")
        # Every submission gets its own request id.  Without it the worker's
        # idempotency check (same request digest -> same job) would hand back the
        # *previous* snapshot for the same config, even after the CAD on that path
        # has changed.  Resuming by job_id is the explicit way to reuse a job.
        submission: dict[str, Any] = {
            "kind": "freeze",
            "config": remote_config_expected,
            "request_id": uuid.uuid4().hex,
        }
        if timeout_seconds:
            submission["cad_timeout_seconds"] = float(timeout_seconds)
        job = client.submit(submission)
        returned = job.get("request")
        if returned != submission:
            raise BridgeError(
                "worker_job_identity_mismatch",
                "the worker answered with a different job than the one submitted",
                {"job_id": job.get("job_id"), "request_id": submission["request_id"]},
            )
        resolved_job_id = str(job["job_id"])
    client.wait(
        resolved_job_id,
        timeout=float(config.get("worker_job_timeout_seconds") or DEFAULT_JOB_TIMEOUT),
        on_progress=on_progress,
    )
    return client.fetch_snapshot(
        resolved_job_id,
        Path(destination),
        expected=remote_config_expected,
        # a worker capture is native CAD evidence unless the source deliberately
        # declares a weaker class (the same key the local freeze honours)
        evidence_class=str(remote_config_expected.get("evidence_class") or "cad"),
    )
