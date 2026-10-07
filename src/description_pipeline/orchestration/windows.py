"""Authenticated, persistent, serial Windows execution endpoint for Airflow.

The API freezes native engineering folders and routes their discovered hardware
identity through platform configuration. It cannot execute shell commands or
accept arbitrary output/repository paths. Job identity is
stable across network retries; an interrupted native run fails explicitly and
is never silently replayed after a process restart.
"""

from __future__ import annotations

import contextlib
import hmac
import hashlib
import json
import os
import queue
import re
import sys
import threading
import tempfile
import uuid
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

from ..delivery import PIPELINE_ID, subject_inventory
from ..io import (
    PipelineError,
    acquire_process_lock,
    artifact_path_parts,
    confined,
    digest,
    file_digest,
    inventory,
    read_data,
    write_json,
)
from ..sources.solidworks.revision import package_inventory, read_revision
from ..repository.urdf_pr import _origin_slug, _slug_hardware
from .handoffs import HANDOFF_SCHEMA, MAX_HANDOFF_BYTES, freeze_handoff, import_archive

_SHA = re.compile(r"^[0-9a-f]{64}$")
_HARDWARE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")
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
            "discovery",
        },
        "Unknown endpoint configuration key",
    )
    for key in ("package_root", "output_root", "state_root"):
        config[key] = _root(config[key])
    _require(config["package_root"].is_dir(), "Package root does not exist")
    settings = config.get("discovery", {})
    _require(
        isinstance(settings, dict) and set(settings) <= {"record_roots", "frozen_names_file"},
        "Unknown native discovery setting",
    )
    records = settings.get("record_roots", [])
    _require(isinstance(records, list), "Native record_roots must be a list")
    settings["record_roots"] = [_root(value) for value in records]
    _require(all(path.is_dir() for path in settings["record_roots"]), "Native record root does not exist")
    if "frozen_names_file" in settings:
        settings["frozen_names_file"] = _root(settings["frozen_names_file"])
        _require(settings["frozen_names_file"].is_file(), "Missing frozen-name registry")
    config["discovery"] = settings
    targets = config["targets"]
    _require(isinstance(targets, dict) and bool(targets), "Configure at least one model repository target")
    roots = [config[key] for key in ("package_root", "output_root", "state_root")]
    for hardware, target in targets.items():
        _require(
            isinstance(hardware, str)
            and _HARDWARE.fullmatch(hardware) is not None
            and isinstance(target, dict)
            and set(target) == {"repository", "base"},
            "Each hardware identity needs a repository and base",
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
    _require(
        config["host"] in {"127.0.0.1", "::1", "localhost"},
        "The Windows endpoint must bind to loopback and use the authenticated SSH tunnel",
    )
    return config


def _owner_lock(path):
    """Process-held ownership survives neither a crash nor an OS restart."""
    try:
        return acquire_process_lock(path)
    except OSError as error:
        raise PipelineError("Another endpoint owns the state root or its ownership lock is unavailable") from error


class Jobs:
    """One persistent queue, one CAD runner, one endpoint process owner."""

    def __init__(self, config, *, runner=None, native_preparer=None):
        from ..solidworks import run

        self.config = config
        self.runner = runner or run
        self.native_preparer = native_preparer
        self.directory = config["state_root"] / "jobs"
        self.directory.mkdir(parents=True, exist_ok=True)
        inventory(self.directory)
        self.lock_path = config["state_root"] / ".endpoint.lock"
        self.handle = _owner_lock(self.lock_path)
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
                        error="Endpoint restarted during native execution; inspect diagnostics and use a new run_id",
                    )
                    self._save(job)
                elif job["status"] == "queued":
                    self.queue.put(identifier)
            self.thread = threading.Thread(target=self._work, name="description-cad-queue", daemon=True)
            self.thread.start()
        except BaseException:
            os.close(self.handle)
            raise

    def _save(self, job):
        write_json(self.directory / (job["run_id"] + ".json"), job)

    def _handoff(self, package, identity):
        return {
            "schema_version": HANDOFF_SCHEMA,
            "pipeline_id": PIPELINE_ID,
            "package": package.relative_to(self.config["package_root"]).as_posix(),
            "handoff_sha256": identity["handoff_sha256"],
        }

    def resolve_handoff(self, request):
        _require(isinstance(request, dict) and set(request) == {"handoff_path"}, "Expected a mechanical handoff folder")
        path = request["handoff_path"]
        _require(
            isinstance(path, str) and bool(path.strip()) and not any(ord(c) < 32 for c in path),
            "Mechanical handoff folder is required",
        )
        source = Path(path)
        if not source.is_absolute():
            source = confined(self.config["package_root"], path.rstrip("/") + "/.handoff-folder", exists=False).parent
        package, identity = freeze_handoff(source, self.config["package_root"] / "imports")
        return self._handoff(package, identity)

    def import_handoff(self, archive):
        package, identity = import_archive(archive, self.config["package_root"] / "imports")
        return self._handoff(package, identity)

    def validate(self, request):
        _require(
            isinstance(request, dict) and set(request) == {"run_id", "package", "handoff_sha256"},
            "Expected run_id, package and handoff_sha256",
        )
        _job_id(request["run_id"])
        _require(
            isinstance(request["package"], str) and bool(artifact_path_parts(request["package"])),
            "Invalid native package path",
        )
        _require(
            isinstance(request["handoff_sha256"], str) and _SHA.fullmatch(request["handoff_sha256"]) is not None,
            "Invalid native handoff digest",
        )
        package = confined(self.config["package_root"], request["package"] + "/.handoff-folder", exists=False).parent
        from .handoffs import describe_handoff

        identity = describe_handoff(package)
        _require(
            identity["handoff_sha256"] == request["handoff_sha256"],
            "Native engineering files changed after selection; start a new run",
        )
        return package

    def preview(self, identifier):
        """Expose only artifact bytes bound to a successful independently verified run."""
        job = self.snapshot(identifier)
        _require(job["status"] in {"passed", "failed"}, "URDF preview is available after verification")
        output = self.config["output_root"] / identifier
        files = subject_inventory(output)
        result = job.get("result") or {}
        quality = result.get("quality") or {}
        _require(
            quality.get("passed") is True and quality.get("subject_sha256") == result.get("subject_sha256"),
            "Unverified models cannot be previewed",
        )
        _require(
            any(
                check.get("id") == "source.native_discovery" and check.get("passed") is True
                for check in quality.get("checks", [])
            ),
            "Native-derived models require independent discovery verification before preview",
        )
        _require(digest(files) == result.get("subject_sha256"), "Delivered model differs from its verified subject")
        assets = {name: checksum for name, checksum in files.items() if name.startswith(("urdf/", "meshes/"))}
        _require("urdf/robot.urdf" in assets, "Verified delivery has no URDF preview")
        return {
            "pipeline_id": PIPELINE_ID,
            "run_id": identifier,
            "subject_sha256": result["subject_sha256"],
            "urdf": "urdf/robot.urdf",
            "files": assets,
        }

    def artifact(self, identifier, name):
        preview = self.preview(identifier)
        _require(name in preview["files"], "Only verified URDF and mesh assets are available")
        path = confined(self.config["output_root"] / identifier, name)
        stream = path.open("rb")
        try:
            _require(
                hashlib.file_digest(stream, "sha256").hexdigest() == preview["files"][name],
                "Preview artifact differs from its verified bytes",
            )
            stream.seek(0)
            return stream, path.stat().st_size
        except BaseException:
            stream.close()
            raise

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

    def _prepare_native(self, identifier, frozen):
        """Discover semantics on the owned queue, then select a platform target."""
        job = self.jobs[identifier]
        output = self.config["state_root"] / "prepared" / identifier

        def event(item):
            self._event(identifier, item)

        event({"stage": "discover", "state": "running"})
        preparer = self.native_preparer
        settings = None
        if preparer is None:
            from ..sources.solidworks.discovery import DiscoverySettings, prepare_native_package

            preparer = prepare_native_package
            configured = self.config.get("discovery", {})
            names = read_data(configured["frozen_names_file"]) if configured.get("frozen_names_file") else {}
            _require(
                isinstance(names, dict)
                and all(isinstance(key, str) and isinstance(value, str) for key, value in names.items()),
                "Frozen-name registry must map stable native identities to interface names",
            )
            settings = DiscoverySettings(record_roots=tuple(configured.get("record_roots", [])), frozen_names=names)
        prepared = preparer(frozen, output, identifier, settings=settings, on_event=event)
        with self.mutex:
            job["discovery"] = {
                "passed": prepared.passed,
                "findings": list(prepared.findings),
                "hardware_id": prepared.hardware_id,
                "revision": prepared.revision,
                "discovery_sha256": prepared.discovery_sha256,
            }
            self._save(job)
        _require(prepared.passed is True, "Native discovery has blocking findings; correct the engineering source")
        _require(
            prepared.handoff_sha256 == job["request"]["handoff_sha256"],
            "Native discovery is bound to a different handoff",
        )
        package = Path(prepared.package).resolve()
        _require(package.is_relative_to(output.resolve()), "Native preparation returned an unmanaged package")
        discovery = Path(prepared.discovery_path).resolve()
        _require(
            discovery.is_relative_to(package) and file_digest(discovery) == prepared.discovery_sha256,
            "Prepared inputs lack the bound raw native discovery record",
        )
        _require(package_inventory(frozen) == job["package_files"], "Frozen engineering changed during discovery")
        for name, checksum in job["package_files"].items():
            _require(file_digest(confined(package, name)) == checksum, "Native preparation changed engineering files")
        revision = read_revision(package)
        _require(
            revision["hardware_id"] == prepared.hardware_id and revision["revision"] == prepared.revision,
            "Generated revision differs from native discovery",
        )
        target = self.config["targets"].get(prepared.hardware_id)
        _require(target is not None, f"Hardware {prepared.hardware_id!r} has no configured model repository")
        with self.mutex:
            job.update(
                hardware_id=prepared.hardware_id,
                revision=prepared.revision,
                repository_slug=_origin_slug(target["repository"]),
                repository_base=target["base"],
            )
            self._save(job)
        event({"stage": "discover", "state": "completed"})
        return package, target

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
                    package_inventory(package) == job["package_files"], "Native inputs changed while the job was queued"
                )
                package, target = self._prepare_native(identifier, package)
                _require(
                    isinstance(job.get("repository_slug"), str)
                    and bool(job["repository_slug"])
                    and job.get("repository_base") == target["base"],
                    "Persisted job lacks matching repository metadata; review it and use a new run_id",
                )
                _require(
                    _origin_slug(target["repository"]) == job["repository_slug"],
                    "Configured repository origin changed while the job was queued",
                )
                result = self.runner(
                    package,
                    self.config["output_root"] / identifier,
                    repository=target["repository"],
                    base=target["base"],
                    run_id=identifier,
                    on_event=lambda event, identifier=identifier: self._event(identifier, event),
                )
                with self.mutex:
                    job["result"] = result
                    submission = result.get("submission", {})
                    quality = result.get("quality", {})
                    subject = result.get("subject_sha256")
                    passed = (
                        result.get("passed") is True
                        and quality.get("passed") is True
                        and isinstance(subject, str)
                        and _SHA.fullmatch(subject) is not None
                        and quality.get("subject_sha256") == subject
                        and submission.get("passed") is True
                        and submission.get("subject_sha256") == subject
                        and submission.get("repository_slug") == job["repository_slug"]
                        and submission.get("base") == target["base"]
                        and submission.get("branch")
                        == "work/solidworks/" + _slug_hardware(read_revision(package)["hardware_id"])
                        and submission.get("state") in {"published", "updated", "noop"}
                        and re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", str(submission.get("commit", "")))
                        is not None
                        and re.fullmatch(
                            r"https://github\.com/" + re.escape(job["repository_slug"]) + r"/pull/[1-9][0-9]*",
                            str(submission.get("url", "")),
                        )
                        is not None
                        and _origin_slug(target["repository"]) == job["repository_slug"]
                        and any(
                            check.get("id") == "source.native_discovery" and check.get("passed") is True
                            for check in quality.get("checks", [])
                        )
                    )
                    job.update(
                        status="passed" if passed else "failed",
                        error=None if passed else result.get("error") or "Incomplete or mismatched publication receipt",
                    )
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
                    suffix = self.path[len("/v1/jobs/") :]
                    if "/artifacts/" in suffix:
                        identifier, name = suffix.split("/artifacts/", 1)
                        stream, size = jobs.artifact(identifier, unquote(name))
                        with stream:
                            self.send_response(200)
                            self.send_header(
                                "Content-Type",
                                "application/xml" if name.endswith(".urdf") else "application/octet-stream",
                            )
                            self.send_header("Content-Length", str(size))
                            self.send_header("Cache-Control", "private, no-store")
                            self.send_header("X-Content-Type-Options", "nosniff")
                            self.send_header("Connection", "close")
                            self.end_headers()
                            import shutil

                            shutil.copyfileobj(stream, self.wfile, length=1024 * 1024)
                            self.close_connection = True
                    elif suffix.endswith("/preview"):
                        self._reply(200, jobs.preview(suffix[: -len("/preview")]))
                    else:
                        self._reply(200, jobs.snapshot(suffix))
                else:
                    self._reply(404, {"error": "Unknown API route"})
            except (PipelineError, OSError, ValueError, TypeError) as error:
                self._reply(getattr(error, "status", 400), {"error": str(error)})

        def do_POST(self):
            if not self._authorized():
                return
            if self.path not in {"/v1/jobs", "/v1/handoffs/resolve", "/v1/handoffs/import"}:
                self._reply(404, {"error": "Unknown API route"})
                return
            try:
                if self.path == "/v1/handoffs/import":
                    _require(
                        self.headers.get("Content-Type", "").split(";")[0] == "application/zip",
                        "Content-Type must be application/zip",
                    )
                    size = int(self.headers.get("Content-Length", "0"))
                    _require(
                        0 < size <= MAX_HANDOFF_BYTES and not self.headers.get("Transfer-Encoding"),
                        "Invalid handoff size",
                    )
                    self.connection.settimeout(60)
                    with tempfile.TemporaryDirectory(prefix=".transport-", dir=jobs.config["state_root"]) as temporary:
                        archive = Path(temporary) / "handoff.zip"
                        with archive.open("xb") as output:
                            remaining = size
                            while remaining:
                                chunk = self.rfile.read(min(remaining, 1024 * 1024))
                                _require(bool(chunk), "Incomplete handoff transport; no job was started")
                                output.write(chunk)
                                remaining -= len(chunk)
                        self._reply(200, jobs.import_handoff(archive))
                    return
                _require(
                    self.headers.get("Content-Type", "").split(";")[0] == "application/json",
                    "Content-Type must be application/json",
                )
                size = int(self.headers.get("Content-Length", "0"))
                _require(0 < size <= 16384 and not self.headers.get("Transfer-Encoding"), "Invalid request size")
                self.connection.settimeout(10)
                request = json.loads(self.rfile.read(size))
                if self.path == "/v1/handoffs/resolve":
                    self._reply(200, jobs.resolve_handoff(request))
                else:
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
        print(
            json.dumps({"pipeline_id": PIPELINE_ID, "endpoint": f"{config['host']}:{config['port']}", "ready": True}),
            flush=True,
        )
        try:
            server.serve_forever(poll_interval=0.5)
        except KeyboardInterrupt:
            return
