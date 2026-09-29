"""Local job API for the Windows collection worker.

The worker binds to loopback only.  Cross-machine access is expected to arrive
through a tunnel (SSH or equivalent); no CAD credential ever leaves the host.
Every request is executed on the single COM thread through the job runner, so
two callers can never own the same SolidWorks session at once.
"""

from __future__ import annotations

import json
import os
import tarfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from typing import Any
from collections.abc import Callable

from . import doctor as doctor_module
from .errors import BridgeError
from .executor import ComExecutor
from .jobs import Job, JobRunner, JobStore

#: Binding to loopback keeps other machines out, but every page in the user's own browser can still
#: reach 127.0.0.1.  A rebinding name arrives in `Host`; a cross-site POST carries `Origin`.
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _authority_host(value: str) -> str | None:
    """The hostname a `Host`/`Origin` value names, or ``None`` when it cannot be parsed."""

    parsed = urlsplit(value if "://" in value else "//" + value)
    if parsed.hostname is None:
        return None
    try:
        _port = parsed.port  # a malformed port is not an authority this worker serves
    except ValueError:
        return None
    return parsed.hostname.lower()


def _untrusted_client(origin: str | None, host: str | None, allowed: set[str] = LOOPBACK_HOSTS) -> str | None:
    """The header that proves the caller is not the local client, or ``None``."""

    for header, value in (("Origin", origin), ("Host", host)):
        if value:
            name = _authority_host(value)
            if name is None or name not in allowed:
                return f"{header}: {value}"
    return None


def _freeze_entry(
    config: dict[str, Any], destination: Path, worker_version: str = "unknown", *, backend=None
) -> dict[str, Any]:
    """Late import so the worker starts even when only the job API is needed."""

    from .freeze import freeze

    return freeze(config, destination, worker_version=worker_version, backend=backend)


WORKER_SCHEMA = "description-pipeline.solidworks-worker/v1"


def worker_version() -> str:
    """The pipeline version, or a clearly-marked development fallback."""

    try:
        from description_pipeline import __version__
    except Exception:  # noqa: BLE001 - the worker must still diagnose itself
        return "unknown"
    return str(__version__)


def default_jobs_root() -> Path:
    base = os.environ.get("DESCRIPTION_WORKER_JOBS")
    if base:
        return Path(base)
    local = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return Path(local) / "description-pipeline" / "jobs"


class Worker:
    """Owns the job store, the COM thread and the freeze entry point."""

    def __init__(
        self,
        *,
        jobs_root: Path | None = None,
        backend_factory: Callable[[], Any] | None = None,
        freeze_fn: Callable[[dict[str, Any], Path], dict[str, Any]] | None = None,
        watchdog_seconds: float = 900.0,
    ) -> None:
        self.jobs_root = Path(jobs_root) if jobs_root else default_jobs_root()
        self.store = JobStore(self.jobs_root)
        self._backend_factory = backend_factory
        self._freeze_fn = freeze_fn
        self._backend: Any = None
        self._executor: ComExecutor | None = None
        self._executor_lock = threading.RLock()
        self._admission_lock = threading.RLock()
        self._maintenance = False
        self.version = worker_version()
        # Recovered jobs are adopted by the runner before its thread starts: the
        # previous process's queue lives in the records, not in memory, so without
        # this the job would stay "queued" forever while the worker reported idle.
        recovered = self.store.recover(worker_version=self.version)
        self.runner = JobRunner(
            self.store,
            self._run_job,
            worker_version=self.version,
            watchdog_seconds=watchdog_seconds,
            initial_job_ids=[job.job_id for job in recovered],
        )
        self.started_at = time.time()

    # -- backend / executor ---------------------------------------------

    def backend(self) -> Any:
        with self._executor_lock:
            if self._backend is None:
                factory = self._backend_factory
                if factory is None:
                    from .native import SolidWorksBackend

                    factory = SolidWorksBackend
                self._backend = factory()
            return self._backend

    def executor(self) -> ComExecutor:
        with self._executor_lock:
            if self._executor is None or not self._executor.alive:
                self._executor = ComExecutor(self.backend())
            return self._executor

    def close(self) -> None:
        self.runner.close()
        if self._executor is not None:
            self._executor.close()

    # -- job execution ---------------------------------------------------

    def _run_job(self, job: Job, staging: Path) -> dict[str, Any]:
        kind = str(job.request.get("kind") or "freeze")
        if kind == "freeze":
            return self._run_freeze(job, staging)
        if kind == "doctor":
            return self._run_doctor(job)
        raise BridgeError("unknown_job_kind", f"unsupported job kind: {kind}", {"kind": kind})

    def _run_freeze(self, job: Job, staging: Path) -> dict[str, Any]:
        config = job.request.get("config")
        if not isinstance(config, dict):
            raise BridgeError("invalid_request", "freeze requests need a 'config' object")
        freeze_fn: Callable[[dict[str, Any], Path], dict[str, Any]] = self._freeze_fn or (
            lambda config, destination: _freeze_entry(config, destination, self.version, backend=self.backend())
        )
        destination = staging / "snapshot"
        raw_timeout = job.request.get("cad_timeout_seconds")
        timeout = float(raw_timeout) if isinstance(raw_timeout, (int, float)) else 900.0

        def capture():
            if self.store.load(job.job_id).cancelled:
                raise BridgeError("cancelled", "Capture was cancelled before CAD startup")
            return freeze_fn(config, destination)

        manifest = self.executor().run(capture, timeout=timeout, operation_id=job.job_id)
        return {"kind": "freeze", "snapshot": str(destination), "manifest": manifest}

    def _run_doctor(self, job: Job) -> dict[str, Any]:
        assembly = job.request.get("assembly")
        configuration = job.request.get("configuration")
        backend = self.backend()
        raw_timeout = job.request.get("cad_timeout_seconds")
        timeout = float(raw_timeout) if isinstance(raw_timeout, (int, float)) else 300.0

        return self.executor().run(
            lambda: doctor_module.diagnose_owned(
                backend, str(assembly) if assembly else None, str(configuration) if configuration else None
            ),
            timeout=timeout,
            operation_id=job.job_id,
        )

    # -- introspection ---------------------------------------------------

    def health(self) -> dict[str, Any]:
        return {
            "schema_version": WORKER_SCHEMA,
            "status": "ok",
            "worker_version": self.version,
            "pid": os.getpid(),
            "uptime_seconds": round(time.time() - self.started_at, 3),
            "jobs_root": str(self.jobs_root),
            "runner": self.runner.snapshot(),
            # straight from the records, so a caller that only sees /health can
            # tell "idle" from "has work this process never queued"
            "jobs": self.store.active_jobs(),
            "cad_operation_active": self._executor is not None and self._executor.busy,
            "cad_recovery_required": self._executor is not None and self._executor.recovery_required,
            "maintenance": self._maintenance,
        }

    def doctor(self, assembly: str | None, configuration: str | None) -> dict[str, Any]:
        with self._admission_lock:
            self._accepting()
            backend = self.backend()
            return self.executor().run(
                lambda: doctor_module.diagnose_owned(backend, assembly, configuration), timeout=300.0
            )

    def _accepting(self) -> None:
        if self._maintenance:
            raise BridgeError("worker_maintenance", "worker is paused for a version switch")

    def submit(self, request: dict[str, Any]) -> Job:
        with self._admission_lock:
            self._accepting()
            return self.runner.submit(request)

    def cancel(self, job_id: str) -> Job:
        job = self.runner.cancel(job_id)
        if job.state == "running" and self._executor is not None:
            self._executor.cancel(job_id)
        return job

    def maintenance(self, enabled: bool) -> dict[str, Any]:
        with self._admission_lock:
            state = self.runner.snapshot()
            active = self.store.active_jobs()
            cad_active = self._executor is not None and self._executor.busy
            recovery = self._executor is not None and self._executor.recovery_required
            if enabled and (
                state["current"]
                or state["queued"]
                or active["queued"]
                or active["running"]
                or (cad_active and not recovery)
            ):
                raise BridgeError(
                    "worker_busy",
                    "wait for active and queued jobs before switching versions",
                    {"runner": state, "jobs": active, "cad_operation_active": cad_active},
                )
            self._maintenance = enabled
            return self.health()


class _Handler(BaseHTTPRequestHandler):
    server_version = "description-worker/" + worker_version()
    worker: Worker  # set by the factory below

    # -- helpers ---------------------------------------------------------

    def log_message(self, fmt: str, *args: Any) -> None:  # keep the console quiet
        return

    def _send(self, status: int, payload: object, content_type: str = "application/json") -> None:
        if content_type == "application/json":
            body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        else:
            body = str(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_tar(self, filename: str, directory: Path) -> None:
        """Stream a snapshot directory as a tar so the caller can fetch it whole."""

        self.send_response(200)
        self.send_header("Content-Type", "application/x-tar")
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        with tarfile.open(fileobj=self.wfile, mode="w|") as archive:
            archive.add(directory, arcname=directory.name)

    def _error(self, exc: BridgeError) -> None:
        self._send(exc.http_status or 500, {"error": exc.to_dict()})

    def _foreign_client(self) -> bool:
        """Refuse the headers a browser adds and a local client never sends.

        A page on any website can POST to a loopback service without reading the answer, so
        "loopback only" is not the same as "only the caller here".  The CLI, the tunneled client and
        `worker.ps1` all send nothing but the loopback address they connected to.
        """

        # A worker started with `--host <address>` answers that address too, but nothing else.
        address = getattr(self.server, "server_address", "")
        bound = str(address[0]).lower() if isinstance(address, tuple) and address else ""
        allowed = LOOPBACK_HOSTS | ({bound} if bound else set())
        suspicious = _untrusted_client(self.headers.get("Origin"), self.headers.get("Host"), allowed)
        if suspicious:
            self._send(
                403,
                {
                    "error": {
                        "code": "untrusted_client",
                        "message": f"{suspicious}; this worker only answers the address it was started on",
                    }
                },
            )
            return True
        return False

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BridgeError("invalid_request", f"body is not JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise BridgeError("invalid_request", "body must be a JSON object")
        return payload

    # -- routes ----------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - http.server naming
        if self._foreign_client():
            return
        path, _, query = self.path.partition("?")
        try:
            if path == "/health":
                return self._send(200, self.worker.health())
            if path == "/doctor":
                params = {key: values[-1] for key, values in parse_qs(query).items()}
                result = self.worker.doctor(params.get("assembly"), params.get("configuration"))
                return self._send(200, result)
            if path == "/jobs":
                return self._send(200, {"jobs": self.worker.store.list_jobs()})
            parts = [segment for segment in path.split("/") if segment]
            if len(parts) == 2 and parts[0] == "jobs":
                return self._send(200, self.worker.store.load(parts[1]).to_dict())
            if len(parts) == 3 and parts[0] == "jobs" and parts[2] == "log":
                return self._send(200, self.worker.store.log_text(parts[1]), "text/plain; charset=utf-8")
            if len(parts) == 3 and parts[0] == "jobs" and parts[2] == "result":
                job = self.worker.store.load(parts[1])
                if job.state != "succeeded" or job.result is None:
                    return self._send(409, {"state": job.state, "error": job.error})
                return self._send(200, job.result)
            if len(parts) == 3 and parts[0] == "jobs" and parts[2] in ("manifest", "files", "package"):
                job = self.worker.store.load(parts[1])
                if job.state != "succeeded" or not isinstance(job.result, dict):
                    return self._send(409, {"state": job.state, "error": job.error})
                payload = job.result if isinstance(job.result, dict) else {}
                raw_manifest = payload.get("manifest")
                manifest: dict[str, Any] = raw_manifest if isinstance(raw_manifest, dict) else {}
                if parts[2] == "manifest":
                    return self._send(200, manifest)
                if parts[2] == "files":
                    return self._send(
                        200,
                        {
                            "job_id": job.job_id,
                            "snapshot": payload.get("snapshot"),
                            "files": manifest.get("files") or {},
                        },
                    )
                snapshot = payload.get("snapshot")
                if not snapshot or not Path(str(snapshot)).is_dir():
                    return self._send(409, {"state": job.state, "error": {"code": "snapshot_missing"}})
                return self._send_tar(f"{job.job_id}.tar", Path(str(snapshot)))
            return self._send(404, {"error": {"code": "not_found", "message": path}})
        except BridgeError as exc:
            return self._error(exc)

    def do_POST(self) -> None:  # noqa: N802 - http.server naming
        if self._foreign_client():
            return
        path, _, _query = self.path.partition("?")
        try:
            if path == "/jobs":
                body = self._body()
                job = self.worker.submit(body)
                return self._send(202, job.to_dict())
            if path in {"/maintenance", "/resume"}:
                return self._send(200, self.worker.maintenance(path == "/maintenance"))
            parts = [segment for segment in path.split("/") if segment]
            if len(parts) == 3 and parts[0] == "jobs" and parts[2] == "cancel":
                job = self.worker.cancel(parts[1])
                return self._send(200, job.to_dict())
            return self._send(404, {"error": {"code": "not_found", "message": path}})
        except BridgeError as exc:
            return self._error(exc)


def serve(worker: Worker, host: str = "127.0.0.1", port: int = 8765) -> tuple[ThreadingHTTPServer, threading.Thread]:
    """Start the loopback API; returns the server and its thread."""

    handler = type("BoundHandler", (_Handler,), {"worker": worker})
    server = ThreadingHTTPServer((host, port), handler)
    thread = threading.Thread(target=server.serve_forever, name="solidworks-worker-http", daemon=True)
    thread.start()
    return server, thread


if __name__ == "__main__":  # ``python -m description_pipeline.sources.solidworks.worker``
    from .cli import main

    raise SystemExit(main())
