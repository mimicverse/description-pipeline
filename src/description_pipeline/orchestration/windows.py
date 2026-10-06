"""Authenticated, persistent, serial Windows execution endpoint for Airflow.

The API selects configured packages and repository targets. It cannot execute
shell commands or accept arbitrary output/repository paths. Job identity is
stable across network retries; an interrupted native run fails explicitly and
is never silently replayed after a process restart.
"""

from __future__ import annotations

import contextlib
import hmac
import json
import os
import queue
import re
import ssl
import sys
import threading
import uuid
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from ..delivery import PIPELINE_ID
from ..io import PipelineError, artifact_path_parts, confined, file_digest, inventory, read_data, write_json
from ..sources.solidworks.revision import package_inventory, read_revision

_ALIAS = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_SHA = re.compile(r"^[0-9a-f]{64}$")
JOB_SCHEMA = "solidworks-to-urdf.job/v1"
CONFIG_SCHEMA = "solidworks-to-urdf.endpoint/v1"


class RequestError(PipelineError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def _require(value, message):
    if not value:
        raise RequestError(message)


def _job_id(value):
    try:
        parsed = str(uuid.UUID(value))
    except (ValueError, AttributeError, TypeError) as error:
        raise RequestError("run_id must be a canonical UUID") from error
    _require(parsed == value, "run_id must be a canonical UUID")
    return parsed


def _root(value):
    path = Path(value)
    _require(
        path.is_absolute() and not path.is_symlink() and not path.is_junction(),
        "Configured roots must be absolute real directories",
    )
    return path.resolve()


def read_config(path):
    config = read_data(Path(path))
    _require(
        isinstance(config, dict) and config.get("schema_version") == CONFIG_SCHEMA, "Unknown endpoint configuration"
    )
    _require(
        set(config)
        <= {
            "schema_version",
            "package_root",
            "output_root",
            "state_root",
            "targets",
            "token_file",
            "host",
            "port",
            "tls_cert",
            "tls_key",
        },
        "Unknown endpoint configuration key",
    )
    for key in ("package_root", "output_root", "state_root"):
        config[key] = _root(config[key])
    _require(config["package_root"].is_dir(), "Package root does not exist")
    targets = config["targets"]
    _require(isinstance(targets, dict) and bool(targets), "Configure at least one model repository target")
    roots = [config[key] for key in ("package_root", "output_root", "state_root")]
    for name, target in targets.items():
        _require(
            _ALIAS.fullmatch(name) is not None and isinstance(target, dict) and set(target) == {"repository", "base"},
            "Each target needs an alias, repository and base",
        )
        target["repository"] = _root(target["repository"])
        _require(target["repository"].is_dir(), "Target repository does not exist")
        _require(
            isinstance(target["base"], str) and target["base"].startswith("feature/"),
            "Model PR base must be feature/<hardware>",
        )
        roots.append(target["repository"])
    for index, first in enumerate(roots):
        for second in roots[index + 1 :]:
            _require(
                not first.is_relative_to(second) and not second.is_relative_to(first),
                "Endpoint package, output, state and repository roots must be separate",
            )
    token_file = _root(config["token_file"])
    _require(token_file.is_file(), "Missing endpoint token file")
    token = token_file.read_text(encoding="utf-8").strip()
    _require(
        len(token) >= 32 and token.isascii() and not any(character.isspace() for character in token),
        "Endpoint token must contain at least 32 non-whitespace ASCII characters",
    )
    config["token"] = token
    config.pop("token_file")
    config.setdefault("host", "127.0.0.1")
    config.setdefault("port", 8765)
    _require(
        isinstance(config["port"], int) and not isinstance(config["port"], bool) and 1 <= config["port"] <= 65535,
        "Invalid endpoint port",
    )
    tls = bool(config.get("tls_cert")) and bool(config.get("tls_key"))
    _require(bool(config.get("tls_cert")) == bool(config.get("tls_key")), "TLS needs both certificate and key")
    _require(
        config["host"] in {"127.0.0.1", "::1", "localhost"} or tls,
        "Remote endpoint binding requires TLS; use an SSH tunnel for loopback HTTP",
    )
    return config


class Jobs:
    """One persistent queue, one CAD runner, one endpoint process owner."""

    def __init__(self, config, *, runner=None):
        from ..solidworks import run

        self.config = config
        self.runner = runner or run
        self.directory = config["state_root"] / "jobs"
        self.directory.mkdir(parents=True, exist_ok=True)
        inventory(self.directory)
        self.lock_path = config["state_root"] / ".endpoint.lock"
        try:
            self.handle = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as error:
            raise PipelineError(
                "Another endpoint owns the state root; inspect the old process before removing its lock"
            ) from error
        os.write(self.handle, str(os.getpid()).encode("ascii"))
        self.mutex = threading.RLock()
        self.queue = queue.Queue()
        self.jobs = {}
        try:
            for path in sorted(self.directory.glob("*.json")):
                identifier = _job_id(path.stem)
                job = read_data(path)
                _require(
                    job.get("schema_version") == JOB_SCHEMA and job.get("run_id") == identifier, "Invalid persisted job"
                )
                self.jobs[identifier] = job
                if job["status"] == "running":
                    job.update(
                        status="failed",
                        error="Endpoint restarted during native execution; inspect retained diagnostics and request a new run_id",
                    )
                    self._save(job)
                elif job["status"] == "queued":
                    self.queue.put(identifier)
            self.thread = threading.Thread(target=self._work, name="description-cad-queue", daemon=True)
            self.thread.start()
        except BaseException:
            os.close(self.handle)
            self.lock_path.unlink(missing_ok=True)
            raise

    def _save(self, job):
        write_json(self.directory / (job["run_id"] + ".json"), job)

    def validate(self, request):
        _require(
            isinstance(request, dict) and set(request) == {"run_id", "package", "revision_sha256", "target"},
            "Expected run_id, package, revision_sha256 and target",
        )
        _job_id(request["run_id"])
        _require(
            isinstance(request["package"], str) and bool(artifact_path_parts(request["package"])),
            "Invalid package path",
        )
        _require(
            isinstance(request["revision_sha256"], str) and _SHA.fullmatch(request["revision_sha256"]) is not None,
            "Invalid CAD revision manifest digest",
        )
        _require(request["target"] in self.config["targets"], "Unknown configured repository target")
        # Resolve a file inside the package so every parent is checked for links.
        manifest = confined(self.config["package_root"], request["package"] + "/cad-revision.json")
        _require(
            file_digest(manifest) == request["revision_sha256"],
            "CAD revision manifest differs from the requested handoff",
        )
        read_revision(manifest.parent)
        return manifest.parent

    def create(self, request):
        _require(isinstance(request, dict), "Expected a JSON object")
        identifier = _job_id(request.get("run_id"))
        with self.mutex:
            if identifier in self.jobs:
                job = self.jobs[identifier]
                if job["request"] != request:
                    raise RequestError("run_id is already bound to a different request", 409)
                return self.snapshot(identifier), False
            package = self.validate(request)
            job = {
                "schema_version": JOB_SCHEMA,
                "pipeline_id": PIPELINE_ID,
                "run_id": identifier,
                "request": dict(request),
                "status": "queued",
                "events": [],
                "result": None,
                "error": None,
                "created_at": datetime.now(UTC).isoformat(),
            }
            job["package_files"] = package_inventory(package)
            self._save(job)
            self.jobs[identifier] = job
            self.queue.put(identifier)
            return self.snapshot(identifier), True

    def snapshot(self, identifier):
        identifier = _job_id(identifier)
        with self.mutex:
            if identifier not in self.jobs:
                raise RequestError("Unknown run_id", 404)
            return json.loads(json.dumps(self.jobs[identifier]))

    def _event(self, identifier, event):
        with self.mutex:
            job = self.jobs[identifier]
            job["events"].append(dict(event))
            self._save(job)

    def _work(self):
        while True:
            identifier = self.queue.get()
            if identifier is None:
                self.queue.task_done()
                return
            job = self.jobs[identifier]
            try:
                with self.mutex:
                    job.update(status="running", started_at=datetime.now(UTC).isoformat())
                    self._save(job)
                package = self.validate(job["request"])
                _require(
                    package_inventory(package) == job["package_files"], "Author inputs changed while the job was queued"
                )
                target = self.config["targets"][job["request"]["target"]]
                result = self.runner(
                    package,
                    self.config["output_root"] / identifier,
                    repository=target["repository"],
                    base=target["base"],
                    run_id=identifier,
                    on_event=lambda event: self._event(identifier, event),
                )
                with self.mutex:
                    submission = result.get("submission", {})
                    passed = (
                        result.get("passed") is True
                        and submission.get("passed") is True
                        and bool(submission.get("url"))
                    )
                    job.update(result=result, status="passed" if passed else "failed", error=result.get("error"))
            except Exception as error:
                with self.mutex:
                    job.update(status="failed", error=f"{type(error).__name__}: {error}")
            finally:
                with self.mutex:
                    job["completed_at"] = datetime.now(UTC).isoformat()
                    self._save(job)
                self.queue.task_done()

    def close(self):
        self.queue.put(None)
        self.thread.join(timeout=30)
        if self.thread.is_alive():
            raise PipelineError("Native job still runs; endpoint ownership lock retained")
        os.close(self.handle)
        self.lock_path.unlink(missing_ok=True)


def handler(jobs, token):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format, *args):
            # HTTP headers and user requests never enter logs, especially tokens.
            return

        def _reply(self, status, data):
            payload = (json.dumps(data, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(payload)
            self.close_connection = True

        def _authorized(self):
            received = self.headers.get("Authorization", "")
            if not hmac.compare_digest(received.encode("utf-8"), ("Bearer " + token).encode("ascii")):
                self._reply(401, {"error": "Unauthorized"})
                return False
            return True

        def do_GET(self):
            if not self._authorized():
                return
            try:
                if self.path == "/health":
                    self._reply(
                        200,
                        {
                            "pipeline_id": PIPELINE_ID,
                            "ready": jobs.thread.is_alive(),
                            "native_platform": sys.platform == "win32",
                        },
                    )
                elif self.path.startswith("/v1/jobs/"):
                    self._reply(200, jobs.snapshot(self.path[len("/v1/jobs/") :]))
                else:
                    self._reply(404, {"error": "Unknown API route"})
            except RequestError as error:
                self._reply(error.status, {"error": str(error)})

        def do_POST(self):
            if not self._authorized():
                return
            if self.path != "/v1/jobs":
                self._reply(404, {"error": "Unknown API route"})
                return
            try:
                _require(
                    self.headers.get("Content-Type", "").split(";")[0] == "application/json",
                    "Content-Type must be application/json",
                )
                size = int(self.headers.get("Content-Length", "0"))
                _require(0 < size <= 16384 and not self.headers.get("Transfer-Encoding"), "Invalid request size")
                self.connection.settimeout(10)
                request = json.loads(self.rfile.read(size))
                result, created = jobs.create(request)
                self._reply(202 if created else 200, result)
            except (PipelineError, ValueError, OSError, TypeError) as error:
                self._reply(getattr(error, "status", 400), {"error": str(error)})

    return Handler


def serve(config_path):
    _require(sys.platform == "win32", "The native execution endpoint requires Windows with licensed SolidWorks")
    config = read_config(config_path)
    with contextlib.ExitStack() as stack:
        jobs = Jobs(config)
        stack.callback(jobs.close)
        server = ThreadingHTTPServer((config["host"], config["port"]), handler(jobs, config["token"]))
        stack.callback(server.server_close)
        if config.get("tls_cert"):
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.load_cert_chain(config["tls_cert"], config["tls_key"])
            server.socket = context.wrap_socket(server.socket, server_side=True)
        print(
            json.dumps({"pipeline_id": PIPELINE_ID, "endpoint": f"{config['host']}:{config['port']}", "ready": True}),
            flush=True,
        )
        try:
            server.serve_forever(poll_interval=0.5)
        except KeyboardInterrupt:
            return
