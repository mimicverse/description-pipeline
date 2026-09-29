"""swbridged: loopback HTTP service (127.0.0.1) plus job scheduling."""

from __future__ import annotations

import argparse
import json
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional, cast
from urllib.parse import parse_qs, urlparse

from . import __version__
from .backends import Backend, FakeBackend
from .com_executor import ComExecutor
from .config import load_config
from .errors import BridgeError, EnvironmentError_
from .exporter import run_export
from .jobs import JobManager
from .policy import path_is_allowed

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 18765


def default_state_dir() -> str:
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return os.path.join(base, "swbridge")
    return os.path.join(os.path.expanduser("~"), ".swbridge")


def allowed_roots_from_env() -> list:
    raw = os.environ.get("SWBRIDGE_ALLOWED_ROOTS", "")
    return [item.strip() for item in re.split(r"[;]", raw) if item.strip()]


class AuditLog:
    """Append-only JSONL audit log; failures never break a request."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.Lock()

    def record(self, payload: dict) -> None:
        try:
            line = json.dumps(payload, ensure_ascii=False, sort_keys=True)
            with self._lock, open(self.path, "a", encoding="utf-8", newline="\n") as handle:
                handle.write(line + "\n")
        except OSError:
            pass


class BridgeServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler, backend: Backend, state_dir: str, allowed_roots=None):
        super().__init__(address, handler)
        self.backend = backend
        self.cad = ComExecutor(backend) if backend.name == "solidworks" else None
        self.state_dir = state_dir
        self.allowed_roots = list(allowed_roots) if allowed_roots is not None else allowed_roots_from_env()
        self.audit = AuditLog(os.path.join(state_dir, "audit.jsonl"))
        self.jobs = JobManager(state_dir)

    def cad_call(self, name, *args, **kwargs):
        def fn():
            return getattr(self.backend, name)(*args, **kwargs)

        return self.cad.run(fn) if self.cad else fn()

    def server_close(self):
        super().server_close()
        self.jobs.close()
        if self.cad:
            self.cad.close()


class BridgeHandler(BaseHTTPRequestHandler):
    server_version = "swbridged/" + __version__
    protocol_version = "HTTP/1.1"

    # Keep stdout clean; the service logs a single startup line.
    def log_message(self, fmt, *args):  # noqa: D102
        pass

    @property
    def _bridge(self) -> "BridgeServer":
        """self.server 在 typeshed 里是 BaseServer；这里给出本服务的类型。"""

        return cast(BridgeServer, self.server)

    # ------------------------------------------------------------- helpers
    @staticmethod
    def _first(values: Optional[list]) -> Optional[str]:
        return values[0] if values else None

    def _start_request(self) -> None:
        self._request_started = time.time()

    def _ensure_allowed(self, path: str, label: str) -> None:
        if not path_is_allowed(path, self._bridge.allowed_roots):
            raise BridgeError(
                "path_not_allowed",
                f"{label} is outside the configured allowlist",
                {"path": path, "allowed_roots": self._bridge.allowed_roots},
                exit_code=1,
                http_status=403,
            )

    def _send(self, status: int, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        started = getattr(self, "_request_started", None)
        job = payload.get("job") if isinstance(payload, dict) else None
        error = payload.get("error") if isinstance(payload, dict) else None
        self._bridge.audit.record(
            {
                "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "method": getattr(self, "command", None),
                "path": getattr(self, "path", None),
                "status": status,
                "duration_ms": None if started is None else round((time.time() - started) * 1000.0, 3),
                "client": self.client_address[0] if self.client_address else None,
                "job_id": job.get("id") if isinstance(job, dict) else None,
                "error_code": error.get("code") if isinstance(error, dict) else None,
            }
        )
        # Complete the audit attempt before acknowledging the request. Otherwise
        # a fast client can observe a successful response before any log exists.
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_error(self, exc: BridgeError) -> None:
        self._send(exc.http_status, {"ok": False, "error": exc.to_dict()})

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError as exc:
            raise BridgeError(
                "usage_error", f"request body is not valid JSON: {exc}", exit_code=1, http_status=400
            ) from exc
        if not isinstance(payload, dict):
            raise BridgeError("usage_error", "request body must be a JSON object", exit_code=1, http_status=400)
        return payload

    # --------------------------------------------------------------- routes
    def do_GET(self):  # noqa: N802
        self._start_request()
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        try:
            if parsed.path == "/v1/health":
                return self._send(200, {"ok": True, "health": self._bridge.cad_call("health")})
            if parsed.path == "/v1/documents":
                return self._send(200, {"ok": True, "documents": self._bridge.cad_call("list_documents")})
            if parsed.path == "/v1/jobs":
                return self._send(200, {"ok": True, "jobs": self._bridge.jobs.list_jobs()})
            if parsed.path == "/v1/selftest":
                result = self._bridge.cad_call(
                    "selftest",
                    test_cs=self._first(query.get("test_cs")),
                    export_mesh=self._first(query.get("export_mesh")),
                )
                points = result.get("points", {}) if isinstance(result, dict) else {}
                ok = (
                    bool(points)
                    and all(p.get("ok") is True for p in points.values())
                    and result.get("ok", True) is True
                )
                return self._send(200, {"ok": ok, "selftest": result})
            parts = parsed.path.strip("/").split("/")
            if len(parts) == 3 and parts[0] == "v1" and parts[1] == "jobs":
                job = self._bridge.jobs.get(parts[2])
                return self._send(200, {"ok": True, "job": job.to_dict()})
            if len(parts) == 4 and parts[0] == "v1" and parts[1] == "jobs" and parts[3] == "log":
                return self._send(200, {"ok": True, "log": self._bridge.jobs.log_text(parts[2])})
            raise BridgeError("not_found", f"unknown route: {parsed.path}", exit_code=1, http_status=404)
        except BridgeError as exc:
            return self._send_error(exc)
        except Exception as exc:  # pragma: no cover - defensive
            return self._send(500, {"ok": False, "error": {"code": "internal_error", "message": str(exc)}})

    def do_POST(self):  # noqa: N802
        self._start_request()
        parsed = urlparse(self.path)
        try:
            body = self._body()
            if parsed.path == "/v1/documents/open":
                path = body.get("path")
                if not isinstance(path, str) or not path:
                    raise BridgeError("usage_error", "path is required", exit_code=1, http_status=400)
                self._ensure_allowed(path, "path")
                return self._send(200, {"ok": True, "result": self._bridge.cad_call("open_document", path)})
            if parsed.path == "/v1/documents/close":
                name = body.get("name")
                if not isinstance(name, str) or not name:
                    raise BridgeError("usage_error", "name is required", exit_code=1, http_status=400)
                return self._send(
                    200,
                    {
                        "ok": True,
                        "result": self._bridge.cad_call("close_document", name, bool(body.get("confirm", False))),
                    },
                )
            if parsed.path == "/v1/jobs":
                return self._create_job(body)
            parts = parsed.path.strip("/").split("/")
            if len(parts) == 4 and parts[0] == "v1" and parts[1] == "jobs" and parts[3] == "cancel":
                return self._send(200, {"ok": True, "job": self._bridge.jobs.cancel(parts[2])})
            raise BridgeError("not_found", f"unknown route: {parsed.path}", exit_code=1, http_status=404)
        except BridgeError as exc:
            return self._send_error(exc)
        except Exception as exc:  # pragma: no cover - defensive
            return self._send(500, {"ok": False, "error": {"code": "internal_error", "message": str(exc)}})

    def _create_job(self, body: dict):
        kind = body.get("kind", "export-urdf")
        if kind != "export-urdf":
            raise BridgeError("usage_error", f"unsupported job kind: {kind!r}", exit_code=1, http_status=400)
        doc = body.get("doc")
        out = body.get("out")
        config_path = body.get("config")
        for value, label in ((doc, "doc"), (out, "out"), (config_path, "config")):
            if not isinstance(value, str) or not value:
                raise BridgeError("usage_error", f"{label} is required", exit_code=1, http_status=400)
            self._ensure_allowed(value, label)
        assert isinstance(doc, str) and isinstance(out, str) and isinstance(config_path, str)
        cfg = load_config(config_path)
        job_id = body.get("job_id")
        evidence_class = body.get("evidence_class")
        if evidence_class is None:
            evidence_class = "synthetic" if self._bridge.backend.name != "solidworks" else "real_cad"
        elif evidence_class not in ("real_cad", "synthetic"):
            raise BridgeError(
                "usage_error", "evidence_class must be 'real_cad' or 'synthetic'", exit_code=1, http_status=400
            )

        def runner(job):
            with open(job.log_path, "a", encoding="utf-8", newline="\n") as handle:

                def log(message: str) -> None:
                    handle.write(f"{message}\n")
                    handle.flush()

                log(f"job {job.id}: export-urdf")

                def export():
                    return run_export(
                        self._bridge.backend,
                        cfg,
                        out,
                        doc,
                        log=log,
                        job=job,
                        evidence_class=evidence_class,
                        progress=lambda payload: self._bridge.jobs.update_progress(job.id, payload),
                    )

                # The whole export owns the COM thread. A concurrent HTTP
                # open/close/selftest cannot replace documents mid-export.
                result = self._bridge.cad.run(export, timeout=3600) if self._bridge.cad else export()
                log(f"job {job.id}: export finished")
                return result

        params = {"doc": doc, "out": out, "config": config_path}
        params["evidence_class"] = evidence_class
        if job_id:
            params["job_id"] = job_id
        job = self._bridge.jobs.submit("export-urdf", params, runner)
        return self._send(200, {"ok": True, "job": job.to_dict()})


def create_server(
    backend: Backend, state_dir: str, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, allowed_roots=None
) -> BridgeServer:
    return BridgeServer((host, port), BridgeHandler, backend, state_dir, allowed_roots=allowed_roots)


def serve_main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(prog="swbridge serve", description="run the SolidWorks bridge service")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--state-dir", default=None)
    parser.add_argument(
        "--backend", choices=("solidworks", "fake"), default=os.environ.get("SWBRIDGE_BACKEND", "solidworks")
    )
    parser.add_argument(
        "--allow-root",
        action="append",
        default=None,
        help="restrict document/output paths to this directory (repeatable); default comes from SWBRIDGE_ALLOWED_ROOTS",
    )
    args = parser.parse_args(argv)

    state_dir = args.state_dir or default_state_dir()
    os.makedirs(state_dir, exist_ok=True)
    if args.backend == "fake":
        backend: Backend = FakeBackend()
    else:
        from .native_swapi import SolidWorksBackend

        from typing import cast

        # The compatibility API is structural; the new native backend owns its raw dataclasses.
        backend = cast(Backend, SolidWorksBackend())
    try:
        httpd = create_server(backend, state_dir, args.host, args.port, allowed_roots=args.allow_root)
    except OSError as exc:
        error = EnvironmentError_("bridge_unavailable", f"cannot bind {args.host}:{args.port}: {exc}")
        print(json.dumps({"ok": False, "error": error.to_dict()}, ensure_ascii=False), flush=True)
        return error.exit_code
    print(
        json.dumps(
            {
                "ok": True,
                "service": "swbridged",
                "host": args.host,
                "port": args.port,
                "backend": args.backend,
                "state_dir": state_dir,
                "version": __version__,
                "allowed_roots": httpd.allowed_roots,
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0
