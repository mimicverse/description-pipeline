"""swctl: thin CLI client for the swbridged loopback service."""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Dict, Optional

from .errors import BridgeError, EnvironmentError_, exit_code_for
from .jobs import TERMINAL

DEFAULT_ENDPOINT = "http://127.0.0.1:18765"


class Client:
    def __init__(self, endpoint: str) -> None:
        self.endpoint = endpoint.rstrip("/")
        host = urllib.parse.urlparse(endpoint).hostname
        handlers = [urllib.request.ProxyHandler({})] if host in ("127.0.0.1", "localhost", "::1") else []
        self._opener = urllib.request.build_opener(*handlers)

    def request(self, method: str, path: str, body: Optional[dict] = None, timeout: float = 60.0) -> dict:
        url = self.endpoint + path
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with self._opener.open(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                payload = json.loads(exc.read().decode("utf-8"))
            except Exception:
                payload = {}
            finally:
                exc.close()
            error = payload.get("error") or {}
            code = error.get("code", "bridge_error")
            raise BridgeError(
                code,
                error.get("message", str(exc)),
                error.get("detail"),
                exit_code=exit_code_for(code),
                http_status=exc.code,
            ) from exc
        except urllib.error.URLError as exc:
            raise EnvironmentError_(
                "bridge_unavailable",
                f"cannot reach swbridged at {self.endpoint}: {exc}",
                {"hint": "start the service: python -m solidworks_export serve"},
            ) from exc


def _emit(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True), flush=True)


def _wait_for_job(client: Client, job_id: str, interval: float) -> dict:
    while True:
        job = client.request("GET", f"/v1/jobs/{job_id}")["job"]
        if job["status"] in TERMINAL:
            return job
        time.sleep(interval)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="swctl", description="control the SolidWorks bridge")
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("ping", help="service and SolidWorks liveness")
    sub.add_parser("status", help="health plus recent jobs")
    sub.add_parser("list", help="list open documents")

    open_parser = sub.add_parser("open", help="open a document")
    open_parser.add_argument("path")

    close_parser = sub.add_parser("close", help="close a document")
    close_parser.add_argument("name")
    close_parser.add_argument("--confirm", action="store_true")

    export_parser = sub.add_parser("export-urdf", help="export URDF + meshes")
    export_parser.add_argument("--doc", required=True)
    export_parser.add_argument("--out", required=True)
    export_parser.add_argument("--config", required=True)
    export_parser.add_argument("--job-id", default=None)
    export_parser.add_argument("--no-wait", action="store_true", help="return as soon as the job is queued")
    export_parser.add_argument("--poll-interval", type=float, default=2.0)
    export_parser.add_argument("--evidence-class", default="auto", choices=("auto", "real_cad", "synthetic"))

    job_parser = sub.add_parser("job", help="inspect or cancel jobs")
    job_sub = job_parser.add_subparsers(dest="job_command", required=True)
    for name in ("status", "log", "cancel"):
        item = job_sub.add_parser(name)
        item.add_argument("job_id")

    selftest_parser = sub.add_parser("selftest", help="probe SolidWorks contact points")
    selftest_parser.add_argument("--test-cs", default=None)
    selftest_parser.add_argument("--export-mesh", default=None)

    verify_parser = sub.add_parser("verify-package", help="offline integrity check of an export package")
    verify_parser.add_argument("path")

    config_parser = sub.add_parser("validate-config", help="offline check of an export config (no CAD needed)")
    config_parser.add_argument("path")

    return parser


def main(argv: Optional[list] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command in ("verify-package", "validate-config"):
        # Offline commands: they never touch the service, so report their errors
        # in the same shape as the HTTP client does.
        try:
            if args.command == "verify-package":
                from .package_check import verify_package

                report = verify_package(args.path)
                _emit(report)
                return 0 if report["ok"] else 1
            from .config import load_config

            cfg = load_config(args.path)
            _emit(
                {
                    "ok": True,
                    "model": cfg.model,
                    "links": len(cfg.links),
                    "joints": len(cfg.joints),
                    "frames": sum(1 for link in cfg.links if link.is_frame),
                    "mesh_path_prefix": cfg.mesh_path_prefix,
                    "source_length_unit": cfg.source_length_unit,
                }
            )
            return 0
        except BridgeError as exc:
            print(
                json.dumps({"ok": False, "error": exc.to_dict()}, ensure_ascii=False, sort_keys=True), file=sys.stderr
            )
            return exc.exit_code

    client = Client(args.endpoint)
    try:
        if args.command == "ping":
            _emit(client.request("GET", "/v1/health"))
        elif args.command == "status":
            health = client.request("GET", "/v1/health")
            jobs = client.request("GET", "/v1/jobs")
            _emit({"ok": True, "health": health.get("health"), "jobs": jobs.get("jobs", [])[:5]})
        elif args.command == "list":
            _emit(client.request("GET", "/v1/documents"))
        elif args.command == "open":
            _emit(client.request("POST", "/v1/documents/open", {"path": args.path}))
        elif args.command == "close":
            _emit(client.request("POST", "/v1/documents/close", {"name": args.name, "confirm": args.confirm}))
        elif args.command == "export-urdf":
            body: Dict = {"kind": "export-urdf", "doc": args.doc, "out": args.out, "config": args.config}
            if args.job_id:
                body["job_id"] = args.job_id
            if args.evidence_class != "auto":
                body["evidence_class"] = args.evidence_class
            response = client.request("POST", "/v1/jobs", body)
            job = response["job"]
            if args.no_wait:
                _emit({"ok": True, "job": job})
            else:
                final = _wait_for_job(client, job["id"], args.poll_interval)
                _emit({"ok": final["status"] == "succeeded", "job": final})
                if final["status"] != "succeeded":
                    return exit_code_for((final.get("error") or {}).get("code", ""))
        elif args.command == "job":
            if args.job_command == "status":
                _emit(client.request("GET", f"/v1/jobs/{args.job_id}"))
            elif args.job_command == "log":
                _emit(client.request("GET", f"/v1/jobs/{args.job_id}/log"))
            else:
                _emit(client.request("POST", f"/v1/jobs/{args.job_id}/cancel", {}))
        elif args.command == "selftest":
            query = {}
            if args.test_cs:
                query["test_cs"] = args.test_cs
            if args.export_mesh:
                query["export_mesh"] = args.export_mesh
            path = "/v1/selftest"
            if query:
                path += "?" + urllib.parse.urlencode(query)
            result = client.request("GET", path)
            _emit(result)
            if result.get("ok") is not True:
                return 3
        else:  # pragma: no cover - argparse enforces the command set
            parser.error("unknown command")
        return 0
    except BridgeError as exc:
        print(json.dumps({"ok": False, "error": exc.to_dict()}, ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return exc.exit_code
