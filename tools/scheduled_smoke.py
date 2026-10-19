"""Scheduled (not dags-test) split-boundary smoke run against a neutral mocked endpoint.

Starts the Airflow DAG processor, scheduler and api-server with the current Feishu auth manager,
then triggers one DAG run the way the operator page does: through the portal's Airflow client with
a real authorized operator JWT.  Requires an installed environment; writes nothing outside
AIRFLOW_HOME.

The mocked Windows delivery is the sealed control fixture, served through the one supported
contract: the endpoint stages the exact full indexed folder in one upload, resolves and seals the
native closure and the raw native record bundle server-side, accepts the generated text
definition, and only then reports the sealed capture transfer at the ``native_complete``
boundary.  The Linux half derives
the prepared package with the real pure derivation, then executes the real generate stage and the
real verify stage, which must reject the control fixture on its documented limits while
publication never executes.  This is an expected rejection proof of the scheduled transport,
never native CAD qualification, and the report states that class explicitly.

When the venv is wired to the checkout by PYTHONPATH alone its metadata carries no
``airflow.plugins`` entry point, so the isolated home receives a plugin-folder shim for the same
class; the middleware itself is proven by the auth health contract.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest

from description_pipeline.interface_records import (
    InterfaceRecordError,
    cache_release_params_digest,
    cache_release_receipt_digest,
    resource_policy_v1_digest,
    validate_cache_release_receipt,
    validate_cache_release_request,
    validate_health,
)
from description_pipeline.schemas import CACHE_RELEASE_RECEIPT_SCHEMA, HEALTH_SCHEMA, RESOURCE_POLICY_SCHEMA
from description_pipeline.stages import CONTRACT, CONTRACT_SHA256

TOKEN = "scheduled-token"
ROOT = Path(__file__).resolve().parents[1]
#: One smoke enterprise app; the api-server and the minted operator token must agree on it.
FEISHU_APP_ID = "cli_smoke"
FEISHU_TENANT = "smoke-tenant"
FEISHU_OPERATOR = "ou_smoke"
FEISHU_SECRET = "smoke-app-secret"
JWT_SECRET = "scheduled-smoke-" + "0" * 48
DAG_ID = "solidworks_to_urdf"
PIPELINE_ID = "solidworks-to-urdf"
#: One relative operator path inside the connection's intake allowlist.
HANDOFF_PATH = "handoff/m3.0"
#: The control fixture's CAD carries its own hardware identity (``fixture["hardware_id"]``);
#: the connection, the portal configuration and the submission all name that identity.
REPOSITORY_SLUG = "example/m3.0"
REPOSITORY_BASE = "feature/m3.0"
#: The smoke verifies the contract's slot-release semantics: the native wait must
#: reschedule (never hold an Airflow slot while polling); poke mode is refused.
SENSOR_MODE_REQUIRED = "reschedule"


def _read_json(path: Path) -> dict:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _view(job: dict) -> dict:
    """The job document as served: no test-only poke counter or mock-private bookkeeping."""

    return {key: value for key, value in job.items() if key != "pokes" and not key.startswith("_")}


def _native_record(fixture_root: Path) -> dict:
    """The exact raw native record the prepared control fixture was derived from."""

    payload = json.loads((fixture_root / "prepared/discovery/native-discovery.json").read_text(encoding="utf-8"))
    return payload["raw"]


def _mock_health_payload(port: int, free_bytes: int) -> dict:
    """The canonical IF-09 identity needed by release and readiness consumers."""

    payload = {
        "schema_version": HEALTH_SCHEMA,
        "pipeline_id": PIPELINE_ID,
        "native_platform": True,
        "ready": True,
        "release": {
            "version": "1.5.0-smoke",
            "source_commit": "0" * 40,
            "source_sha256": "0" * 64,
            "role": "native",
        },
        "contract": {"schema_version": CONTRACT["schema_version"], "digest": CONTRACT_SHA256},
        "resource_policy": {
            "schema_version": RESOURCE_POLICY_SCHEMA,
            "digest": resource_policy_v1_digest(),
        },
        "install": {"config_digest": "0" * 64, "install_id": "scheduled-smoke-endpoint"},
        "endpoint": {"port": port},
        "cad": {"external_session_detected": False, "owned_sessions": 0, "lease_held": False},
        "jobs": {"active": 0, "queued": 0, "unresolved": 0},
        "capacity": {"free_bytes": free_bytes, "reserved_bytes": 0},
        "readiness": {"reason_code": None, "native": {"ok": True, "problems": []}},
    }
    return validate_health(payload)


def _mock_endpoint(
    fixture: dict, *, run_id: str, upload_files: dict[str, str], record: dict, bundle_dir: Path, port: int = 0
) -> tuple[ThreadingHTTPServer, dict]:
    """The one-workflow endpoint contract: full-folder staging, raw record, definition, capture."""

    from description_pipeline.io import digest  # noqa: PLC0415 - the checkout arrives via --root
    from description_pipeline.orchestration import discovery_transfer  # noqa: PLC0415
    from description_pipeline.sources.solidworks.selection import seal_closure_selection  # noqa: PLC0415
    from tests.sources.selection_support import closure_evidence  # noqa: PLC0415

    main_assembly = fixture["main_assembly"]
    dependencies = sorted(name for name in upload_files if name != main_assembly)
    reader_sha256 = fixture["native_tool"]["source_sha256"]
    stage_root = str(bundle_dir.parent / "native-stage")
    edges = {main_assembly: list(dependencies), **{name: [] for name in dependencies}}
    state = {
        "jobs": {},
        "artifact_paths": [],
        "definition_posts": [],
        "uploads": 0,
        "cache_receipts": {},
        "cache_releases": [],
    }
    archive = Path(fixture["archive"]).read_bytes()

    def sealed_manifest() -> dict:
        """The closure the native endpoint seals server-side after the single full-folder upload."""

        evidence = closure_evidence(
            upload_files, main_assembly, edges=edges, reader_sha256=reader_sha256, stage_root=stage_root
        )
        return seal_closure_selection(upload_files, main_assembly, evidence, reader_sha256=reader_sha256)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args) -> None:
            return

        def _send(self, code: int, payload: dict) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _auth(self) -> bool:
            if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                self._send(401, {"error": "unauthorized"})
                return False
            return True

        def do_GET(self) -> None:
            if not self._auth():
                return
            if self.path == "/health":
                self._send(
                    200,
                    _mock_health_payload(
                        int(self.server.server_address[1]),
                        int(shutil.disk_usage(bundle_dir).free),
                    ),
                )
                return
            if not self.path.startswith("/v1/jobs/"):
                self._send(404, {"error": "unknown"})
                return
            segments = self.path[len("/v1/jobs/") :].split("/")
            job = state["jobs"].get(segments[0])
            if job is None:
                self._send(404, {"error": "unknown"})
                return
            if len(segments) >= 2 and segments[1] == "artifacts":
                name = urlparse.unquote("/".join(segments[2:]))
                data = (job.get("_artifacts") or {}).get(name)
                if data is None:
                    self._send(404, {"error": "only sealed transfers are available"})
                    return
                state["artifact_paths"].append(self.path)
                self.send_response(200)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            if len(segments) != 1:
                self._send(404, {"error": "unknown"})
                return
            job["pokes"] += 1
            self._send(200, _view(job))

        def do_POST(self) -> None:
            if not self._auth():
                return
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length)
            segments = self.path[len("/v1/jobs/") :].split("/") if self.path.startswith("/v1/jobs/") else []
            job = state["jobs"].get(segments[0]) if segments and segments[0] else None
            if self.path == "/v1/jobs":
                payload = json.loads(body or b"{}")
                fresh = {
                    "run_id": run_id,
                    "upload_files": upload_files,
                    "upload_sha256": digest(upload_files),
                    "main_assembly": main_assembly,
                }
                if (
                    set(payload) - {"run_id", "upload_files", "upload_sha256", "main_assembly", "resume"}
                    or {key: payload.get(key) for key in fresh} != fresh
                ):
                    self._send(409, {"error": "selection request differs from the staged intake"})
                    return
                existing = state["jobs"].get(fresh["run_id"])
                if existing is not None:
                    if existing["request"] != payload:
                        self._send(409, {"error": "run_id is bound to a different request"})
                        return
                    self._send(200, _view(existing))
                    return
                job = {
                    "schema_version": "solidworks-to-urdf.job/v1",
                    "pipeline_id": PIPELINE_ID,
                    "run_id": fresh["run_id"],
                    "request": dict(payload),
                    "main_assembly": main_assembly,
                    "native_tool": fixture["native_tool"],
                    "status": "selecting",
                    "events": [{"stage": "job", "state": "selecting", "at": "t0"}],
                    "selection": {
                        "state": "awaiting_files",
                        "reader_sha256": reader_sha256,
                        "upload_sha256": digest(upload_files),
                        "staged_files": {},
                    },
                    "result": None,
                    "error": None,
                    "_artifacts": {},
                    "pokes": 0,
                }
                state["jobs"][fresh["run_id"]] = job
                self._send(202, _view(job))
                return
            if job is None:
                self._send(404, {"error": "unknown run_id"})
                return
            if len(segments) == 3 and segments[1:] == ["cache", "release"]:
                payload = json.loads(body or b"{}")
                try:
                    validate_cache_release_request(payload)
                except InterfaceRecordError:
                    self._send(409, {"error": "cache release request is invalid"})
                    return
                operation_id = payload["operation_id"]
                existing = state["cache_receipts"].get(operation_id)
                if existing is not None:
                    self._send(200, existing)
                    return
                import_block = payload["import"]
                imported = import_block["confirmation"]["imported"]
                receipt = {
                    "schema_version": CACHE_RELEASE_RECEIPT_SCHEMA,
                    "status": "released",
                    "operation_id": operation_id,
                    "params_sha256": cache_release_params_digest(import_block),
                    "import": import_block,
                    "released": {
                        "files": len(imported),
                        "bytes": sum(int(entry["bytes"]) for entry in imported),
                    },
                    "retained": [],
                    "released_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                    "identity": {**payload["binding"]["release"], "role": "native"},
                    "receipt_sha256": "",
                }
                receipt["receipt_sha256"] = cache_release_receipt_digest(receipt)
                validate_cache_release_receipt(receipt)
                state["cache_receipts"][operation_id] = receipt
                state["cache_releases"].append(receipt)
                self._send(200, receipt)
                return
            if len(segments) == 2 and segments[1] == "files":
                block = job["selection"]
                try:
                    with zipfile.ZipFile(io.BytesIO(body)) as source:
                        members = {info.filename: source.read(info) for info in source.infolist()}
                except (OSError, ValueError, zipfile.BadZipFile):
                    self._send(400, {"error": "staging archive is not a readable ZIP"})
                    return
                if set(members) != set(upload_files) or any(
                    hashlib.sha256(members[name]).hexdigest() != checksum for name, checksum in upload_files.items()
                ):
                    self._send(409, {"error": "staging archive differs from the upload index"})
                    return
                if block["state"] != "awaiting_files":
                    # A logical retry of the exact full-index archive: no native rework.
                    self._send(200, _view(job))
                    return
                state["uploads"] += 1
                manifest = sealed_manifest()
                sealed = discovery_transfer.seal_raw_record_bundle(
                    {**record, "source_selection": manifest},
                    bundle_dir / "raw-record.zip",
                    run_id=run_id,
                    selection=manifest,
                    native_tool=fixture["native_tool"],
                )
                job["_artifacts"][sealed["artifact"]["name"]] = (bundle_dir / "raw-record.zip").read_bytes()
                job["native_record"] = {
                    "name": sealed["artifact"]["name"],
                    "sha256": sealed["artifact"]["sha256"],
                    "size": sealed["artifact"]["bytes"],
                    "manifest_sha256": digest(sealed["manifest"]),
                    "manifest": sealed["manifest"],
                }
                block.update(state="sealed", staged_files=dict(upload_files), manifest=manifest)
                # The sealed server shape records the frozen handoff on the job.
                job.update(
                    handoff_sha256=manifest["handoff_sha256"],
                    package_files=dict(upload_files),
                )
                job["status"] = "awaiting_definition"
                job["events"] = [
                    *fixture["native_stages"]["events"],
                    {"stage": "job", "state": "awaiting_definition", "at": "t0"},
                ]
                self._send(200, _view(job))
                return
            if len(segments) == 2 and segments[1] == "definition":
                prepared = self.headers.get("X-Prepared-SHA256") or ""
                if not body or len(prepared) != 64 or any(item not in "0123456789abcdef" for item in prepared):
                    self._send(400, {"error": "definition binding is invalid"})
                    return
                state["definition_posts"].append({"prepared_sha256": prepared, "size": len(body)})
                job["_artifacts"][fixture["receipt"]["name"]] = archive
                job["status"] = "native_complete"
                job["events"] = [
                    *fixture["native_stages"]["events"],
                    {"stage": "job", "state": "native_complete", "at": "t0"},
                ]
                job["result"] = {
                    "native_complete": True,
                    "native_tool": fixture["native_tool"],
                    "capture_archive": dict(fixture["receipt"]),
                }
                job["error"] = None
                self._send(200, _view(job))
                return
            if len(segments) == 2 and segments[1] == "abort":
                payload = json.loads(body or b"{}")
                state.setdefault("aborts", []).append(payload)
                job["status"] = "failed"
                job["error"] = str(payload.get("error") or "derivation refused")
                job["events"] = [*job.get("events", []), {"stage": "job", "state": "failed", "at": "t0"}]
                self._send(200, _view(job))
                return
            self._send(404, {"error": "not found"})

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, state


def _run(venv: Path, env: dict, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([str(venv / "bin/airflow"), *args], env=env, capture_output=True, text=True, timeout=120)


def _free_port() -> int:
    """A free loopback port, so an isolated smoke never collides with a running deployment."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _distinct_free_port(*excluded: int) -> int:
    for _ in range(50):
        candidate = _free_port()
        if candidate not in excluded:
            return candidate
    raise RuntimeError("could not find a distinct free loopback port")


def _auth_health(port: int) -> dict:
    """The current auth manager's public health contract, as the deployment probe reads it."""
    url = f"http://127.0.0.1:{port}/auth/feishu/health"
    try:
        with urlrequest.urlopen(url, timeout=10) as response:
            payload = json.loads(response.read() or b"{}")
            return {
                "status": response.status,
                "configured": payload.get("configured"),
                "request_context": payload.get("request_context"),
            }
    except urlerror.HTTPError as error:
        with contextlib.suppress(ValueError):
            payload = json.loads(error.read() or b"{}")
            return {
                "status": error.code,
                "configured": payload.get("configured"),
                "request_context": payload.get("request_context"),
            }
        return {"status": error.code}
    except (urlerror.URLError, TimeoutError, OSError) as error:
        return {"status": None, "error": str(error)}


def _close_mock(server) -> None:
    """Close one started mock server without masking the original failure."""

    if server is None:
        return
    with contextlib.suppress(Exception):
        server.shutdown()
    with contextlib.suppress(Exception):
        server.server_close()


def _deploy_defaults(root: Path) -> dict:
    """The deployment port defaults live once, in the operator renderer (DEP-01)."""

    spec = importlib.util.spec_from_file_location(
        "deploy_render_operator", root / "deploy/operator/render_operator.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.PORT_DEFAULTS


def _operator_https_port(root: Path) -> int:
    override = os.environ.get("OPERATOR_HTTPS_PORT", "")
    if override:
        if not override.isdigit():
            raise SystemExit("OPERATOR_HTTPS_PORT must be a TCP port")
        return int(override)
    return int(_deploy_defaults(root)["OPERATOR_HTTPS_PORT"])


def _feishu_env(home: Path, https_port: int) -> dict[str, str]:
    """One throwaway 0600 enterprise app, so the smoke exercises the real Feishu manager."""
    secret = home / "feishu.json"
    secret.write_text(json.dumps({"app_id": FEISHU_APP_ID, "app_secret": FEISHU_SECRET}), encoding="utf-8")
    os.chmod(secret, 0o600)
    return {
        "FEISHU_APP_SECRET_FILE": str(secret),
        "FEISHU_TENANT_KEYS": FEISHU_TENANT,
        "FEISHU_REDIRECT_URI": f"https://127.0.0.1:{https_port}/auth/feishu/callback",
    }


def _config_snapshot_environment(
    args,
    *,
    target: str,
    state_root: Path,
    handoff_root: Path,
    workspace_root: Path,
    record_roots: list[str],
    endpoint_port: int,
    tunnel_port: int,
) -> tuple[dict[str, str], dict]:
    """The controlled platform-configuration identity that governs THIS smoke run.

    The IF-16 snapshot is built by the deploy-side owner's own mechanism
    (``validate_platform`` then ``build_snapshot``) from the exact inputs this run
    consumes - its state/handoff/workspace roots, its record roots, its targets and
    its real endpoint port - re-validated by the package's IF-16 validator,
    serialized with the one canonical serializer and digested over exactly those
    bytes (the same derivation the operator environment and the install record
    bind).  The package-side snapshot digest must agree, otherwise the smoke
    refuses instead of fabricating a second identity.  The resolved tree is
    returned so every consumer below reads the snapshot's declared values.
    """
    from description_pipeline import interface_records  # noqa: PLC0415
    from description_pipeline.io import canonical  # noqa: PLC0415

    spec = importlib.util.spec_from_file_location(
        "deploy_platform_config", args.root / "deploy/operator/platform_config.py"
    )
    platform_config = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(platform_config)
    fixture_root = args.airflow_home / "smoke-config"
    document = {
        "schema_version": platform_config.PLATFORM_SCHEMA,
        "operator": {
            "origin": f"https://127.0.0.1:{_operator_https_port(args.root)}",
            "certificate_file": str(fixture_root / "secrets/tls.crt"),
            "private_key_file": str(fixture_root / "secrets/tls.key"),
        },
        "storage": {
            "state_root": str(state_root),
            "handoff_root": str(handoff_root),
            "artifact_root": str(args.airflow_home / "smoke-artifacts"),
            "workspace_root": str(workspace_root),
            "release_root": str(args.airflow_home / "smoke-releases"),
        },
        "database": {"dsn_file": str(fixture_root / "secrets/postgres.dsn")},
        "windows": {
            "ssh_host": "smoke-windows",
            "endpoint_port": endpoint_port,
            "tunnel_loopback_port": tunnel_port,
            "token_file": str(fixture_root / "secrets/endpoint.token"),
            "instance": {
                "task_name": "description-smoke-endpoint",
                "package_root": "C:/description-smoke/packages",
                "output_root": "C:/description-smoke/results",
                "state_root": "C:/description-smoke/state",
            },
        },
        "auth": {
            "feishu_secret_file": str(args.airflow_home / "feishu.json"),
            "tenant_keys": [FEISHU_TENANT],
            "admin_open_ids": [FEISHU_OPERATOR],
        },
        "targets": {target: {"repository_slug": REPOSITORY_SLUG, "base": REPOSITORY_BASE}},
        "specifications": {"record_roots": list(record_roots)},
    }
    resolved = platform_config.validate_platform(document)
    platform_json_bytes = canonical(document)
    install_id = f"smoke-{uuid.uuid4().hex[:12]}"
    snapshot = platform_config.build_snapshot(
        resolved,
        platform_json_sha256=hashlib.sha256(platform_json_bytes).hexdigest(),
        install_id=install_id,
        generated_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    interface_records.validate_config_snapshot(snapshot)
    snapshot_bytes = canonical(snapshot)
    digest_value = hashlib.sha256(snapshot_bytes).hexdigest()
    if digest_value != interface_records.config_snapshot_digest(snapshot):
        raise RuntimeError("the snapshot digest owners disagree; refusing a second identity")
    fixture_root.mkdir(parents=True, exist_ok=True)
    (fixture_root / "platform.json").write_bytes(platform_json_bytes)
    snapshot_path = platform_config.atomic_write_once(
        Path(state_root) / "snapshots" / f"{install_id}.json", snapshot_bytes
    )
    return {"PLATFORM_CONFIG_DIGEST": digest_value, "OPERATOR_STATE": str(state_root)}, resolved, snapshot_path


def _write_smoke_install_record(state_root: Path, snapshot_path: Path, resolved: dict) -> None:
    """Create the active snapshot binding the real Airflow consumers resolve."""

    from description_pipeline import interface_records  # noqa: PLC0415
    from description_pipeline.stages import STAGE_IDS  # noqa: PLC0415

    snapshot_digest = hashlib.sha256(snapshot_path.read_bytes()).hexdigest()
    install_id = json.loads(snapshot_path.read_text(encoding="utf-8"))["install_id"]
    version = "1.5.0-smoke"
    source_commit = "0" * 40
    source_sha256 = "0" * 64
    release = {
        "version": version,
        "source_commit": source_commit,
        "source_sha256": source_sha256,
        "artifacts": [{"name": "scheduled-smoke", "sha256": "1" * 64}],
    }
    dag = ROOT / "deploy/airflow/dags/solidworks_to_urdf.py"
    record = {
        "schema_version": interface_records.INSTALL_RECORD_SCHEMA,
        "install_id": install_id,
        "timestamp": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "snapshot": {"path": str(snapshot_path), "digest": snapshot_digest, "install_id": install_id},
        "release": release,
        "services": [
            "description-postgres",
            "description-airflow-dag-processor",
            "description-airflow-scheduler",
            "description-airflow-api-server",
            "description-portal",
            "description-operator-proxy",
        ],
        "paths": resolved["storage"],
        "dag": {
            "id": DAG_ID,
            "path": str(dag),
            "digest": hashlib.sha256(dag.read_bytes()).hexdigest(),
            "tasks": len(STAGE_IDS),
        },
        "ends": {
            "linux": {
                "install_id": install_id,
                "config_digest": snapshot_digest,
                "release": {
                    **{key: release[key] for key in ("version", "source_commit", "source_sha256")},
                    "role": "portable",
                },
            },
            "windows": {
                "install_id": "win-scheduled-smoke",
                "config_digest": "2" * 64,
                "release": {
                    **{key: release[key] for key in ("version", "source_commit", "source_sha256")},
                    "role": "native",
                },
            },
        },
        "previous": None,
        "journal": [],
    }
    interface_records.validate_install_record(record)
    target = state_root / "install-record.json"
    target.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    target.chmod(0o600)


def _plugin_preflight(venv: Path, env: dict[str, str], home: Path) -> str | None:
    """Ensure the plugin loads from the installed entry point or from this home's plugins folder."""
    code = (
        "import importlib.metadata as m;"
        "print(any(e.group == 'airflow.plugins' and e.name == 'description_pipeline' for e in m.entry_points()))"
    )
    try:
        probe = subprocess.run(
            [str(venv / "bin/python"), "-c", code], env=env, capture_output=True, text=True, timeout=60
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return f"cannot probe the Airflow environment: {error}"
    if probe.returncode == 0 and probe.stdout.strip() == "True":
        return None
    # PYTHONPATH-only wiring has no distribution metadata; the isolated home loads the same class
    # from its plugins folder instead.  The middleware itself is proven by the auth health probe.
    try:
        plugins = home / "plugins"
        plugins.mkdir(parents=True, exist_ok=True)
        (plugins / "description_pipeline_plugin.py").write_text(
            '"""Isolated smoke home: re-export the checkout\'s Airflow plugin class."""\n'
            "\n"
            "from description_pipeline.orchestration.airflow_plugin import DescriptionPipelinePlugin  # noqa: F401\n",
            encoding="utf-8",
        )
    except OSError as error:
        return f"the Airflow environment provides no plugin and the isolated home cannot add one: {error}"
    return None


def _operator_jwt(venv: Path, env: dict[str, str]) -> str:
    """The token /auth/feishu/callback would mint for one approved-tenant operator."""
    code = (
        "from description_pipeline.orchestration import feishu_auth as a;"
        f"u=a.FeishuUser(app_id='{FEISHU_APP_ID}',open_id='{FEISHU_OPERATOR}',name='smoke',"
        f"avatar_url='',tenant_key='{FEISHU_TENANT}');"
        "print(a.FeishuAuthManager().generate_jwt(u))"
    )
    minted = subprocess.run(
        [str(venv / "bin/python"), "-c", code], env=env, capture_output=True, text=True, timeout=120
    )
    if minted.returncode != 0:
        raise RuntimeError(f"cannot mint the smoke operator token: {minted.stderr[-500:]}")
    return minted.stdout.strip().splitlines()[-1]


def _task_reschedule_evidence(
    venv: Path, env: dict[str, str], dag_id: str, run_id: str, task_id: str
) -> tuple[list[dict], str | None]:
    """Raw TaskReschedule rows for one task of one run, read from the isolated metadata DB."""

    code = (
        "import json\n"
        "from airflow.models.taskreschedule import TaskReschedule\n"
        "from airflow.models.taskinstance import TaskInstance\n"
        "from airflow.settings import Session\n"
        f"dag, run, task = {dag_id!r}, {run_id!r}, {task_id!r}\n"
        "def row(record):\n"
        "    return {\n"
        "        name: (None if getattr(record, name, None) is None else str(getattr(record, name)))\n"
        "        for name in ('id', 'ti_id', 'start_date', 'end_date', 'reschedule_date')\n"
        "    }\n"
        "with Session() as session:\n"
        "    rows = [\n"
        "        row(record)\n"
        "        for record in session.query(TaskReschedule).join(\n"
        "            TaskInstance, TaskReschedule.ti_id == TaskInstance.id\n"
        "        ).filter(\n"
        "            TaskInstance.dag_id == dag,\n"
        "            TaskInstance.run_id == run,\n"
        "            TaskInstance.task_id == task,\n"
        "        ).order_by(TaskReschedule.id).all()\n"
        "    ]\n"
        "print(json.dumps({'rows': rows}))\n"
    )
    try:
        probe = subprocess.run(
            [str(venv / "bin/python"), "-c", code], env=env, capture_output=True, text=True, timeout=120
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return [], f"cannot query TaskReschedule: {error}"
    if probe.returncode != 0:
        return [], probe.stderr[-500:] or "TaskReschedule probe failed"
    try:
        payload = json.loads(probe.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError) as error:
        return [], f"unreadable TaskReschedule probe output: {error}"
    rows = payload.get("rows")
    return (rows if isinstance(rows, list) else []), None


def _start_portal_server(
    root: Path,
    airflow_base: str,
    upload_root: Path,
    repository: Path,
    targets: dict,
    dag_id: str,
    endpoint_holder: dict,
) -> tuple[object, str]:
    """Start the real portal in-process (admission through the operator HTTP surface).

    The endpoint is bound lazily: the portal is up before the mocked Windows endpoint exists
    (admission happens while the DAG is still paused, so no task can call it yet).  The
    platform store, and with it the sealed store root, derives from the same deploy environment
    the smoke published (``PLATFORM_SERVICE_DSN`` + ``PIPELINE_STORE_ROOT``) - the portal
    carries no second store-root channel.
    """

    from wsgiref.simple_server import make_server  # noqa: PLC0415

    from description_pipeline.orchestration.portal import AirflowApi, PortalApp, PortalConfig  # noqa: PLC0415

    config = PortalConfig(
        airflow=AirflowApi(airflow_base),
        endpoint=lambda: endpoint_holder["endpoint"],
        static_dir=root / "src/description_pipeline/orchestration/static",
        upload_root=upload_root,
        pipeline_repositories={REPOSITORY_SLUG: repository},
        pipeline_targets=targets,
        dag_id=dag_id,
    )
    server = make_server("127.0.0.1", 0, PortalApp(config))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def _open_platform_admission() -> dict:
    """The operator ``start`` step through the same store API the deployment uses.

    Installation seeds the single admission row CLOSED; every submission path refuses until
    the gate is opened.  The smoke opens it exactly as ``operatorctl start`` does, so the
    portal admission below exercises the real open-gate path instead of a bypass.
    """

    from description_pipeline.platform.store import PlatformStore, PlatformStoreError  # noqa: PLC0415

    try:
        view = PlatformStore.from_environment(os.environ).open_admission(
            reason="start", actor="scheduled_smoke"
        )
    except PlatformStoreError as error:
        return {"state": "", "error": str(error)}
    return {"state": view.state, "revision": view.revision, "actor": view.actor}


def _http_json(
    method: str,
    url: str,
    *,
    token: str = "",
    cookies: str = "",
    csrf: str = "",
    data: bytes | None = None,
    content_type: str = "",
) -> tuple[int, dict, list[str]]:
    headers: dict[str, str] = {}
    cookie = "; ".join(item for item in (f"_token={token}" if token else "", cookies) if item)
    if cookie:
        headers["Cookie"] = cookie
    if csrf:
        headers["X-CSRF-Token"] = csrf
    if content_type:
        headers["Content-Type"] = content_type
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            body = response.read()
            payload = json.loads(body) if body else {}
            return response.status, payload, response.headers.get_all("Set-Cookie") or []
    except urllib.error.HTTPError as error:
        body = error.read()
        try:
            payload = json.loads(body) if body else {}
        except ValueError:
            payload = {"error": body[:200].decode("utf-8", "replace")}
        return error.code, payload, []


def _portal_admit(
    portal_base: str, token: str, intake_dir: Path, folder_name: str, target: str, summary: str, purpose: str
) -> tuple[str, str, dict]:
    """Adopt an operator session (Airflow SSO cookie) and submit the folder through the portal.

    ``folder_name`` is the leaf of the handoff root: the portal reconstitutes the upload as
    ``upload_root/<dag_run_id>/<folder_name>`` and the DAG reads that path as its handoff, so
    the leaf keeps the fixture-relative file layout intact.
    """

    from description_pipeline.sources.solidworks.revision import package_inventory  # noqa: PLC0415
    from tests.v1.test_portal import _upload_body  # noqa: PLC0415

    status, session, set_cookies = _http_json("GET", f"{portal_base}/api/session", token=token)
    if status != 200:
        raise RuntimeError(f"portal session failed: {status} {session}")
    cookie = next(
        (item.split(";", 1)[0] for item in set_cookies if item.startswith("solidworks_portal_session=")), ""
    )
    files = [(name, (intake_dir / name).read_bytes()) for name in sorted(package_inventory(intake_dir))]
    body = _upload_body(
        folder=folder_name,
        files=files,
        fields=[("target_hardware", target), ("change_summary", summary), ("purpose", purpose)],
    )
    status, payload, _ = _http_json(
        "POST",
        f"{portal_base}/api/runs",
        token=token,
        cookies=cookie,
        csrf=str(session.get("csrf_token") or ""),
        data=body,
        content_type="multipart/form-data; boundary=portal-boundary",
    )
    if status != 201 or not payload.get("dag_run_id"):
        raise RuntimeError(f"portal admission failed: {status} {payload}")
    return str(payload["dag_run_id"]), cookie, session


def _portal_negative_checks(
    portal_base: str, token: str, cookie: str, csrf: str, top_name: str, run_id: str, target: str
) -> list[dict]:
    """The HTTP-driven negative matrix (illegal fields and an illegal rerun stage)."""

    from tests.v1.test_portal import _upload_body  # noqa: PLC0415

    def multipart(fields: list[tuple[str, str]]) -> bytes:
        return _upload_body(folder=top_name, files=[], fields=fields)

    results = []
    checks = (
        ("missing_target", multipart([("change_summary", "x"), ("purpose", "engineering")])),
        (
            "unconfigured_target",
            multipart([("target_hardware", "not-configured"), ("change_summary", "x"), ("purpose", "engineering")]),
        ),
        ("missing_summary", multipart([("target_hardware", target), ("purpose", "engineering")])),
        ("missing_purpose", multipart([("target_hardware", target), ("change_summary", "x")])),
        ("unknown_purpose", multipart([("target_hardware", target), ("change_summary", "x"), ("purpose", "ghost")])),
    )
    for name, body in checks:
        status, payload, _ = _http_json(
            "POST",
            f"{portal_base}/api/runs",
            token=token,
            cookies=cookie,
            csrf=csrf,
            data=body,
            content_type="multipart/form-data; boundary=portal-boundary",
        )
        results.append({"check": name, "status": status, "expected": 400, "ok": status == 400, "payload": payload})
    status, payload, _ = _http_json(
        "POST",
        f"{portal_base}/api/runs/{run_id}/attempts",
        token=token,
        cookies=cookie,
        csrf=csrf,
        data=json.dumps({"stage": "bogus"}).encode("utf-8"),
        content_type="application/json",
    )
    results.append(
        {"check": "illegal_stage", "status": status, "expected": 400, "ok": status == 400, "payload": payload}
    )
    return results


def _quality_rejection_comparisons(checks: list[dict], preview_failing: set[str]) -> dict:
    """The documented control rejection: required gates executed and passed, failures
    confined to the control limits, and both formal views agreeing on the failing set.

    ``REQUIRED_CONTROL_GATES`` (nine checks) must each carry a definite executed
    ``state == "passed"`` with ``passed is True`` - absent, not_run or failed all
    refuse.  Failures are allowed only inside ``CONTROL_LIMITS`` (eight classes,
    disjoint from the required set); anything else refuses.  The quality document
    and the diagnostic summary must list the same failing identities.
    """

    from tests.v1.split_capture_fixture import (  # noqa: PLC0415
        CONTROL_LIMITS,
        REQUIRED_CONTROL_GATES,
    )

    by_id = {str(item.get("id")): item for item in checks}
    executed = bool(checks) and all(
        item.get("state") in {"passed", "failed"} and isinstance(item.get("passed"), bool)
        for item in checks
    )
    consistent = executed and all(
        (item.get("passed") is True) == (item.get("state") == "passed") for item in checks
    )
    failing = {
        str(item.get("id")) for item in checks if item.get("passed") is False or item.get("state") == "failed"
    }
    required_ok = all(
        (by_id.get(gate) or {}).get("state") == "passed" and (by_id.get(gate) or {}).get("passed") is True
        for gate in REQUIRED_CONTROL_GATES
    )
    return {
        "executed": executed,
        "consistent": consistent,
        "failing": failing,
        "unexpected_gates": sorted(failing - set(CONTROL_LIMITS)),
        "missing_required_gates": [] if required_ok else sorted(REQUIRED_CONTROL_GATES),
        "views_agree": failing == preview_failing,
        # The controlled rejection is the documented failure set: required gates pass and
        # at least one allowed control-limit gate fails.  An all-passing document can never
        # substantiate a rejection claim.
        "allowed_failure": bool(failing) and failing <= set(CONTROL_LIMITS),
    }


def _settlement_comparisons(rows: dict, projection: dict) -> list[dict]:
    """The strict settlement comparisons: platform rows versus the observed projection.

    Every comparison is unconditional: the run and stage rows must match the portal
    projection exactly, the run's dispatch and native operations must be in definite
    terminal states (completed side effects are never left queued, unresolved or
    running), and the run events must exist and carry the identity of the state
    changes they describe - never an arbitrary fixed count.
    """

    terminal_run_states = {"succeeded", "failed", "cancelled"}
    terminal_operation_states = {"succeeded", "failed", "cancelled"}
    comparisons = [
        {"name": "admission_run_row", "passed": len(rows["run_state"]) == 1, "observed": rows["run_state"]},
        {"name": "admission_request_row", "passed": len(rows["request"]) == 1, "observed": rows["request"]},
    ]
    operations = [
        {
            "operation_key": row[0],
            "stage": row[1],
            "kind": row[2],
            "state": row[3],
            "task_id": row[4],
            "map_index": row[5],
        }
        for row in rows["operations"]
    ]
    dispatch = [item for item in operations if item["kind"] == "dispatch"]
    comparisons.append(
        {
            "name": "dispatch_operation_terminal",
            "passed": bool(dispatch) and all(item["state"] in terminal_operation_states for item in dispatch),
            "observed": operations,
        }
    )
    comparisons.append(
        {
            "name": "dispatch_task_binding",
            "passed": bool(dispatch)
            and all(
                item["task_id"] == "freeze"
                and type(item["map_index"]) is int
                and item["map_index"] == -1
                for item in dispatch
            ),
            "observed": dispatch,
        }
    )
    lifecycle = [dict(item) for item in rows.get("lifecycle", [])]
    comparisons.append(
        {
            "name": "lifecycle_projection_terminal",
            "passed": bool(lifecycle) and all(item.get("bucket") == "terminal" for item in lifecycle),
            "observed": lifecycle,
        }
    )
    native = [item for item in operations if item["kind"] == "native"]
    stages = dict(rows["stages"])
    capture_settled = stages.get("capture") in {"completed", "failed", "cancelled"}
    comparisons.append(
        {
            "name": "native_operation_terminal",
            "passed": all(item["state"] in terminal_operation_states for item in native)
            and (bool(native) or not capture_settled),
            "observed": {"operations": operations, "capture": stages.get("capture")},
        }
    )
    projected_state = projection.get("state") if isinstance(projection, dict) else None
    terminal = [state for state in rows["run_state"] if state in terminal_run_states]
    mapped = {"success": "succeeded", "failed": "failed"}.get(str(projected_state))
    comparisons.append(
        {
            "name": "run_state_matches_projection",
            "passed": len(terminal) == 1 and terminal[0] == mapped,
            "observed": [terminal or "no terminal platform run row", projected_state],
        }
    )
    tasks = {str(item.get("task_id")): item.get("state") for item in (projection.get("tasks") or [])}
    mapping = {"success": "completed", "failed": "failed", "upstream_failed": "blocked", "skipped": "blocked"}
    expected_stages = {stage: mapping.get(str(state), str(state)) for stage, state in tasks.items()}
    comparisons.append(
        {
            "name": "stage_rows_match_projection",
            "passed": bool(expected_stages) and stages == expected_stages,
            "observed": {"sql": stages, "projected": expected_stages},
        }
    )
    events = [dict(item) for item in rows["events"]]
    legal_states = {"pending", "running", "completed", "failed", "cancelled", "unresolved", "blocked"}
    identity_ok = True
    latest_by_stage: dict[str, tuple[int, str]] = {}
    for event in events:
        if event.get("kind") != "stage":
            continue
        stage = str(event.get("stage"))
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        state = payload.get("state")
        if stage not in stages or state not in legal_states:
            # An event must describe a real stage and carry a real state; a
            # nonexistent stage or a missing state is an identity failure.
            identity_ok = False
            continue
        # History is legitimate: the latest event per stage must match the
        # current row; earlier transitions (running before failed) are kept.
        seq = event.get("seq") if isinstance(event.get("seq"), int) else 0
        if stage not in latest_by_stage or seq >= latest_by_stage[stage][0]:
            latest_by_stage[stage] = (seq, str(state))
    missing_transitions = sorted(set(stages) - set(latest_by_stage))
    mismatched = sorted(stage for stage, (_seq, state) in latest_by_stage.items() if stages.get(stage) != state)
    comparisons.append(
        {
            "name": "run_event_identity",
            # Every registered stage must carry its own transition event bound to the row it
            # produced (the latest event wins, so a recorded running->failed history stays
            # valid); an unrelated event can never stand in for a stage transition, and no
            # fixed event count is assumed.
            "passed": bool(stages) and identity_ok and not missing_transitions and not mismatched,
            "observed": {
                "events": events,
                "stages": stages,
                "missing_transitions": missing_transitions,
                "mismatched": mismatched,
            },
        }
    )
    return comparisons


def _platform_sql_evidence(platform_schema: str, run_id: str, projection: dict, *, request_id: str) -> dict:
    """The platform rows of this run plus the strict settlement comparisons.

    ``run_id`` is the store's canonical native UUID (the DAG and the admission both
    write that identity); ``request_id`` is the portal-issued request identity of the
    same submission (``portal-<dag_run_id>``).
    """

    import psycopg2  # noqa: PLC0415

    if not re.fullmatch(r"[a-z_][a-z0-9_]*", platform_schema or ""):
        return {"ok": False, "problems": ["platform schema name is not queryable"], "rows": {}, "comparisons": []}
    dsn = os.environ.get("PLATFORM_SERVICE_DSN", "")
    if not dsn:
        return {"ok": False, "problems": ["PLATFORM_SERVICE_DSN is not set"], "rows": {}, "comparisons": []}
    rows: dict = {}
    with psycopg2.connect(dsn) as connection, connection.cursor() as cursor:
        cursor.execute(f"SELECT state FROM {platform_schema}.run WHERE run_id=%s", (run_id,))
        rows["run_state"] = [row[0] for row in cursor.fetchall()]
        cursor.execute(
            f"SELECT request_id, target_hardware, principal FROM {platform_schema}.request WHERE request_id=%s",
            (request_id,),
        )
        rows["request"] = cursor.fetchall()
        cursor.execute(
            f"SELECT operation_key, stage, kind, state, task_id, map_index"
            f" FROM {platform_schema}.operation WHERE run_id=%s "
            "ORDER BY operation_id",
            (run_id,),
        )
        rows["operations"] = cursor.fetchall()
        cursor.execute(
            f"SELECT object_kind, object_id, state, bucket"
            f" FROM {platform_schema}.lifecycle_projection WHERE run_id=%s"
            " ORDER BY object_kind, object_id",
            (run_id,),
        )
        rows["lifecycle"] = [
            {"object_kind": row[0], "object_id": row[1], "state": row[2], "bucket": row[3]}
            for row in cursor.fetchall()
        ]
        cursor.execute(f"SELECT stage, state FROM {platform_schema}.stage WHERE run_id=%s ORDER BY stage", (run_id,))
        rows["stages"] = cursor.fetchall()
        cursor.execute(
            f"SELECT seq, kind, stage, payload FROM {platform_schema}.run_event WHERE run_id=%s ORDER BY seq",
            (run_id,),
        )
        rows["events"] = [
            {"seq": row[0], "kind": row[1], "stage": row[2], "payload": row[3]} for row in cursor.fetchall()
        ]
    comparisons = _settlement_comparisons(rows, projection)
    problems = [
        f"platform settlement assertion failed: {item['name']} {item['observed']}"
        for item in comparisons
        if not item["passed"]
    ]
    return {
        "ok": not problems,
        "schema": platform_schema,
        "rows": rows,
        "comparisons": comparisons,
        "problems": problems,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--venv", type=Path, required=True)
    parser.add_argument("--airflow-home", type=Path, required=True)
    parser.add_argument("--api-port", type=int, default=0, help="loopback api-server port (0 picks a free one)")
    parser.add_argument("--timeout", type=float, default=420)
    parser.add_argument(
        "--portal-admission",
        action="store_true",
        help="submit through a locally started real portal (HTTP multipart) instead of the client trigger",
    )
    parser.add_argument(
        "--purpose",
        required=True,
        choices=["engineering", "inspection_preview"],
        help="the declared purpose of this smoke run (never defaulted)",
    )
    parser.add_argument("--platform-schema", default="", help="platform schema for the SQL evidence queries")
    parser.add_argument("--airflow-schema", default="", help="the Airflow schema this run's metadata lives in")
    parser.add_argument("--airflow-db-conn", default="", help="Airflow metadata DSN (PostgreSQL) for this session")
    parser.add_argument("--report", type=Path, default=None, help="also write the report JSON here")
    args = parser.parse_args()
    # The in-process portal is a control-plane reader of the same Airflow metadata
    # database as the scheduler.  Bind that identity before any Airflow module is
    # imported: task workers never take this ORM path, but portal admission does.
    os.environ["AIRFLOW_HOME"] = str(args.airflow_home)
    if args.airflow_db_conn:
        os.environ["AIRFLOW__DATABASE__SQL_ALCHEMY_CONN"] = args.airflow_db_conn
    ambient_sensor_mode = os.environ.get("SOLIDWORKS_SENSOR_MODE")
    if ambient_sensor_mode not in (None, SENSOR_MODE_REQUIRED):
        print(
            json.dumps(
                {
                    "ok": False,
                    "stage": "sensor_mode",
                    "error": f"ambient SOLIDWORKS_SENSOR_MODE={ambient_sensor_mode!r} is not supported; "
                    f"the smoke verifies {SENSOR_MODE_REQUIRED} slot-release semantics",
                }
            )
        )
        return 1
    sys.path.insert(0, str(args.root / "src"))
    sys.path.insert(0, str(args.root))
    from description_pipeline.orchestration.airflow_client import native_run_id  # noqa: PLC0415
    from description_pipeline.orchestration.portal import AirflowApi  # noqa: PLC0415
    from description_pipeline.sources.solidworks.revision import package_inventory  # noqa: PLC0415
    from tests.v1.split_capture_fixture import (  # noqa: PLC0415
        QUALIFICATION,
        build_fixture_capture,
    )

    portal_mode = bool(args.portal_admission)
    run_id = f"smoke-{uuid.uuid4().hex[:12]}"
    native_id = native_run_id(run_id)
    # The smoke owns its scratch space inside its own AIRFLOW_HOME: a previous attempt (or an
    # earlier smoke on a persistent home) must not collide with this run's fixtures, staged
    # transfer archives or intake folder.
    shutil.rmtree(args.airflow_home / "smoke", ignore_errors=True)
    if portal_mode:
        # The real portal admits the run and its dag_run_id binds the native identity; the
        # placeholder built below exists only to prepare the uploaded folder before admission.
        run_id = ""
        native_id = ""
    placeholder_native = native_run_id(f"smoke-{uuid.uuid4().hex[:12]}")
    fixture_root = args.airflow_home / "smoke" / f"fixture-{placeholder_native}"
    try:
        fixture = build_fixture_capture(fixture_root, run_id=placeholder_native)
    except Exception as error:  # noqa: BLE001 - the smoke reports the exact stage
        print(json.dumps({"ok": False, "stage": "control_fixture", "error": f"{type(error).__name__}: {error}"}))
        return 1
    # The control fixture's CAD carries its own hardware identity; the submission must name
    # exactly that identity (and the snapshot must route it), or the derivation refuses the
    # run as an identity conflict.
    target = str(fixture["hardware_id"])

    origin_port = _operator_https_port(args.root)
    api_port = args.api_port or _distinct_free_port(origin_port)
    intake_root = args.airflow_home / "smoke" / "intake"
    intake = intake_root / HANDOFF_PATH
    shutil.copytree(fixture_root / "native", intake)
    upload_files = package_inventory(intake)
    record = _native_record(fixture_root)
    server = None
    portal_server = None
    state: dict = {}
    tunnel_port = _distinct_free_port(origin_port, api_port) if portal_mode else 0
    if not portal_mode:
        server, state = _mock_endpoint(
            fixture,
            run_id=native_id,
            upload_files=upload_files,
            record=record,
            bundle_dir=args.airflow_home / "smoke" / "definition",
        )
        tunnel_port = server.server_address[1]
    endpoint_port = _distinct_free_port(origin_port, api_port, tunnel_port)
    # The controlled platform-configuration identity of this smoke world: one canonical
    # IF-16 snapshot built by the deploy-side mechanism from THIS run's exact inputs
    # (paths, ports, records, targets), published to the child Airflow services through
    # the environment.  Every consumer below reads the snapshot's resolved values - the
    # snapshot governs the deployed fixture instead of declaring a parallel one, and
    # the runtime freeze still refuses a missing or mismatched digest.
    try:
        snapshot_env, resolved, snapshot_path = _config_snapshot_environment(
            args,
            target=target,
            state_root=args.airflow_home / "smoke-state",
            handoff_root=intake_root,
            workspace_root=args.airflow_home / "smoke-workspaces",
            record_roots=[str(fixture_root / "records")],
            endpoint_port=endpoint_port,
            tunnel_port=tunnel_port,
        )
    except Exception as error:  # noqa: BLE001 - the smoke reports the exact stage
        print(json.dumps({"ok": False, "stage": "config_snapshot", "error": f"{type(error).__name__}: {error}"}))
        _close_mock(server)
        return 1
    os.environ.update(snapshot_env)
    # The snapshot's secret references must be the real ones this run uses: the endpoint
    # token file is materialized (0600), and the platform
    # database reference is the actual PLATFORM_SERVICE_DSN this smoke was given -
    # materialized as the referenced file and re-published from it.  Nothing secret is
    # printed or copied into evidence.
    secrets_dir = args.airflow_home / "smoke-config" / "secrets"
    secrets_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(secrets_dir, 0o700)
    token_file = Path(resolved["windows"]["token_file"])
    token_file.write_text(TOKEN, encoding="utf-8")
    os.chmod(token_file, 0o600)
    supplied_dsn = os.environ.get("PLATFORM_SERVICE_DSN", "")
    if not supplied_dsn:
        print(
            json.dumps(
                {
                    "ok": False,
                    "stage": "platform_dsn",
                    "error": (
                        "PLATFORM_SERVICE_DSN is required: the platform database reference "
                        "must be the real connection this run uses"
                    ),
                }
            )
        )
        _close_mock(server)
        return 1
    dsn_file = Path(resolved["database"]["dsn_file"])
    dsn_file.write_text(supplied_dsn, encoding="utf-8")
    os.chmod(dsn_file, 0o600)
    os.environ["PLATFORM_SERVICE_DSN"] = dsn_file.read_text(encoding="utf-8")
    # The one canonical smoke store root: the snapshot's derived store root, created once
    # here (the operator convention: a dedicated directory under the isolated home, mode
    # 0700) and published to every consumer of this smoke: the in-process admission and
    # the lazily bound portal read it from the environment, the scheduler reads the same
    # path from the snapshot reference below, and the child processes inherit it through
    # ``env``.  ``PlatformStore.from_environment`` keeps requiring it - there is no
    # implicit fallback and no second root.
    store_root = Path(resolved["derived"]["store_root"])
    repository_path = Path(resolved["derived"]["repositories"][REPOSITORY_SLUG])
    if Path(resolved["storage"]["handoff_root"]) != intake_root:
        print(json.dumps({"ok": False, "stage": "config_snapshot", "error": "the snapshot handoff root diverged"}))
        _close_mock(server)
        return 1
    store_root.mkdir(parents=True, exist_ok=True)
    store_root.chmod(0o700)
    os.environ["PIPELINE_STORE_ROOT"] = str(store_root)
    _write_smoke_install_record(Path(resolved["storage"]["state_root"]), snapshot_path, resolved)
    from description_pipeline import interface_records

    reference = {
        "schema_version": interface_records.CONFIG_SNAPSHOT_SCHEMA,
        "path": str(snapshot_path),
        "sha256": hashlib.sha256(snapshot_path.read_bytes()).hexdigest(),
    }
    common_env = dict(
        os.environ,
        AIRFLOW_HOME=str(args.airflow_home),
        PYTHONPATH=os.pathsep.join(filter(None, [str(args.root / "src"), os.environ.get("PYTHONPATH")])),
        OPERATOR_STATE=resolved["storage"]["state_root"],
        PLATFORM_CONFIG_DIGEST=reference["sha256"],
    )
    if args.airflow_db_conn:
        common_env["AIRFLOW__DATABASE__SQL_ALCHEMY_CONN"] = args.airflow_db_conn
    writer = subprocess.run(
        [
            str(args.venv / "bin/python"),
            str(args.root / "deploy/airflow/scripts/add_connection.py"),
            "--state-root",
            resolved["storage"]["state_root"],
            "--snapshot",
            str(snapshot_path),
        ],
        capture_output=True,
        text=True,
        env=common_env,
        cwd=args.root,
    )
    if writer.returncode != 0:
        print(json.dumps({"ok": False, "stage": "config_connection", "stderr": writer.stderr[-500:]}))
        _close_mock(server)
        return 1
    reader = subprocess.run(
        [
            str(args.venv / "bin/python"),
            "-m",
            "description_pipeline.orchestration.airflow_client",
            "--conn-id",
            "solidworks_windows",
            "--state-root",
            resolved["storage"]["state_root"],
        ],
        capture_output=True,
        text=True,
        env=common_env,
        cwd=args.root,
    )
    if reader.returncode != 0 or f"config_digest={reference['sha256']}" not in reader.stdout:
        print(
            json.dumps(
                {
                    "ok": False,
                    "stage": "config_connection_readback",
                    "stderr": reader.stderr[-500:],
                    "stdout": reader.stdout[-500:],
                }
            )
        )
        _close_mock(server)
        return 1
    env = dict(
        common_env,
        PIPELINE_STORE_ROOT=str(store_root),
        AIRFLOW__CORE__AUTH_MANAGER="description_pipeline.orchestration.feishu_auth.FeishuAuthManager",
        AIRFLOW__CORE__LOAD_EXAMPLES="false",
        AIRFLOW__API_AUTH__JWT_SECRET=JWT_SECRET,
        **_feishu_env(args.airflow_home, _operator_https_port(args.root)),
        SOLIDWORKS_SENSOR_MODE=SENSOR_MODE_REQUIRED,
        SOLIDWORKS_POLL_INTERVAL="1",
        SOLIDWORKS_TIMEOUT="120",
        # An isolated home has no airflow.cfg pointing at the repository DAG folder.
        AIRFLOW__CORE__DAGS_FOLDER=str(args.root / "deploy/airflow/dags"),
        AIRFLOW__CORE__EXECUTION_API_SERVER_URL=f"http://127.0.0.1:{api_port}/execution",
    )
    env.pop("AIRFLOW_CONN_SOLIDWORKS_WINDOWS", None)
    problem = _plugin_preflight(args.venv, env, args.airflow_home)
    if problem is not None:
        print(json.dumps({"ok": False, "stage": "plugin_missing", "error": problem}))
        _close_mock(server)
        return 1
    migrate = _run(args.venv, env, "db", "migrate")
    if migrate.returncode != 0:
        print(json.dumps({"ok": False, "stage": "db_migrate", "stderr": migrate.stderr[-500:]}))
        _close_mock(server)
        return 1
    # The production pool entry (install.sh runs the same script): a freshly migrated home only
    # carries the stock default pool, and the DAG's native stage waits on windows_native; without
    # this the isolated scheduler smoke stalls at scheduling instead of exercising the run.
    pool = subprocess.run(
        [str(args.venv / "bin/python"), str(ROOT / "deploy/airflow/scripts/ensure_pool.py")],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if pool.returncode != 0:
        print(json.dumps({"ok": False, "stage": "ensure_pool", "stderr": pool.stderr[-500:]}))
        _close_mock(server)
        return 1
    log_dir = args.airflow_home / "logs" / "smoke"
    log_dir.mkdir(parents=True, exist_ok=True)
    logs = {}
    procs = []
    stack = contextlib.ExitStack()
    components = (
        ("dag-processor", []),
        ("scheduler", []),
        ("api-server", ["--host", "127.0.0.1", "--port", str(api_port)]),
    )
    for component, extra in components:
        # Handles live in an ExitStack closed below in ``finally``; the child process needs the fd first.
        handle = stack.enter_context(
            open(log_dir / f"{component}.log", "w", encoding="utf-8")  # noqa: SIM115 - closed via ExitStack
        )
        logs[component] = handle
        procs.append(
            subprocess.Popen(
                [str(args.venv / "bin/airflow"), component, *extra],
                env=env,
                stdout=handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        )
    try:
        time.sleep(15)
        for (component, _), proc in zip(components, procs, strict=True):
            if proc.poll() is not None:
                tail = (log_dir / f"{component}.log").read_text(encoding="utf-8")[-600:]
                print(json.dumps({"ok": False, "stage": f"{component}_exit", "log": tail}))
                return 1
        auth_health = _auth_health(api_port)
        if auth_health.get("status") is None:
            print(json.dumps({"ok": False, "stage": "auth_health", "auth_health": auth_health}))
            return 1
        if auth_health.get("status") == 200 and auth_health.get("request_context") is not True:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "stage": "auth_middleware_missing",
                        "auth_health": auth_health,
                        "error": "the request-context middleware is not bound; the isolated home must load the "
                        "description-pipeline Airflow plugin",
                    }
                )
            )
            return 1
        airflow = AirflowApi(f"http://127.0.0.1:{api_port}")
        token = _operator_jwt(args.venv, env)
        portal_base = ""
        portal_cookie = ""
        portal_csrf = ""
        projection: dict = {}
        negatives: list[dict] = []
        rejection: dict = {}
        sql_evidence: dict | None = None
        identity_stable: bool | None = None
        trigger_kind = "portal-airflow-client"
        endpoint_holder: dict = {}
        admission: dict = {}
        portal_server = None
        if portal_mode:
            # The operator "start" half of the workflow: the gate is opened through the store
            # API before any submission can be admitted.
            admission = _open_platform_admission()
            if admission.get("state") != "open":
                print(json.dumps({"ok": False, "stage": "platform_admission", "admission": admission}))
                return 1
            # Admission first, while the DAG is paused: the portal's dag_run_id binds the native
            # identity, so the fixture and mocked endpoint are built for it afterwards and only
            # then is scheduling released.
            portal_server, portal_base = _start_portal_server(
                args.root,
                f"http://127.0.0.1:{api_port}",
                intake_root,
                repository_path,
                resolved["targets"],
                DAG_ID,
                endpoint_holder,
            )
            try:
                run_id, portal_cookie, portal_session = _portal_admit(
                    portal_base,
                    token,
                    intake,
                    Path(HANDOFF_PATH).name,
                    target,
                    "scheduled control-fixture smoke",
                    args.purpose,
                )
                portal_csrf = str(portal_session.get("csrf_token") or "")
            except Exception as error:  # noqa: BLE001 - the smoke reports the exact stage
                print(json.dumps({"ok": False, "stage": "portal_admission", "error": str(error)}))
                return 1
            native_id = native_run_id(run_id)
            rebuilt_root = args.airflow_home / "smoke" / f"fixture-{native_id}"
            try:
                rebuilt = build_fixture_capture(rebuilt_root, run_id=native_id)
            except Exception as error:  # noqa: BLE001
                print(
                    json.dumps(
                        {
                            "ok": False,
                            "stage": "control_fixture_rebuild",
                            "error": f"{type(error).__name__}: {error}",
                        }
                    )
                )
                return 1
            # The uploaded folder is the frozen handoff: its relative inventory must equal the
            # rebuilt fixture's native tree and seal the same handoff digest.  The derived
            # capture archive is bound to the run identity and legitimately differs per build.
            identity_stable = rebuilt.get("handoff_sha256") == fixture.get("handoff_sha256") and (
                package_inventory(rebuilt_root / "native") == package_inventory(intake)
            )
            fixture = rebuilt
            fixture_root = rebuilt_root
            upload_files = package_inventory(intake)
            record = _native_record(fixture_root)
            server, state = _mock_endpoint(
                fixture,
                run_id=native_id,
                upload_files=upload_files,
                record=record,
                bundle_dir=args.airflow_home / "smoke" / "definition",
                port=tunnel_port,
            )
            from description_pipeline.orchestration.airflow_client import (  # noqa: PLC0415
                EndpointConfig,
                WindowsEndpoint,
            )

            endpoint_holder["endpoint"] = WindowsEndpoint(
                EndpointConfig(base_url=f"http://127.0.0.1:{tunnel_port}", token=TOKEN)
            )
            resume = _run(args.venv, env, "dags", "unpause", DAG_ID)
            if resume.returncode != 0:
                print(json.dumps({"ok": False, "stage": "unpause", "stderr": resume.stderr[-500:]}))
                return 1
            trigger_kind = "portal-http-admission"
        else:
            unpause = _run(args.venv, env, "dags", "unpause", DAG_ID)
            if unpause.returncode != 0:
                print(json.dumps({"ok": False, "stage": "unpause", "stderr": unpause.stderr[-500:]}))
                return 1
            # Trigger exactly as the operator page does: the portal's client, a real operator JWT
            # and the pinned strict request model.  A CLI trigger could not catch a body regression.
            try:
                airflow.trigger_dag_run(
                    token,
                    DAG_ID,
                    run_id,
                    {
                        "handoff_path": str(intake),
                        "target_hardware": target,
                        "change_summary": "scheduled control-fixture smoke",
                        "purpose": args.purpose,
                    },
                )
            except Exception as error:  # noqa: BLE001 - the smoke reports the exact stage
                print(json.dumps({"ok": False, "stage": "portal_trigger", "error": str(error)}))
                return 1
        deadline = time.time() + args.timeout
        triggering_user_name = None
        dag_state = None
        reschedule_observed: list[dict] = []
        while time.time() < deadline:
            try:
                run = airflow.dag_run(token, DAG_ID, run_id)
            except Exception:  # noqa: BLE001 - the run may not be visible for a moment
                time.sleep(5)
                continue
            triggering_user_name = run.get("triggering_user_name")
            dag_state = run.get("state")
            if dag_state in {"success", "failed"}:
                break
            try:
                live = airflow.task_instances(token, DAG_ID, run_id)
            except Exception:  # noqa: BLE001 - the run may not be visible for a moment
                live = []
            if any(
                str(item.get("task_id")) == "discover" and str(item.get("state")) == "up_for_reschedule"
                for item in live
            ):
                reschedule_observed.append(
                    {
                        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        "task_id": "discover",
                        "state": "up_for_reschedule",
                    }
                )
            # One-second reschedule windows: a looser poll misses the observable state even
            # though the TaskReschedule rows prove the slot release.
            time.sleep(1)
        else:
            tails = {c: (log_dir / f"{c}.log").read_text(encoding="utf-8")[-400:] for c in logs}
            print(
                json.dumps({"ok": False, "stage": "timeout", "run_id": run_id, "dag_state": dag_state, "logs": tails})
            )
            return 1

        # The scheduler serialized every task: the same operator API that renders the run reports
        # each task instance, its state and its attempt count.
        instances = airflow.task_instances(token, DAG_ID, run_id)
        task_states = {
            str(item.get("task_id")): {"state": item.get("state"), "try_number": item.get("try_number")}
            for item in instances
        }
        problems: list[str] = []
        if portal_mode:
            status, projection, _ = _http_json(
                "GET", f"{portal_base}/api/runs/{run_id}", token=token, cookies=portal_cookie
            )
            if status != 200:
                problems.append(f"the portal projection could not be read: {status}")
            negatives = _portal_negative_checks(
                portal_base, token, portal_cookie, portal_csrf, HANDOFF_PATH, run_id, target
            )
            for item in negatives:
                if not item["ok"]:
                    problems.append(
                        f"portal negative check {item['check']} answered {item['status']}, "
                        f"expected {item['expected']}"
                    )
            # The expected rejection must be provable from the formal report surface: the
            # portal's diagnostic preview is digest-bound to the retained verify receipt and
            # carries the exact failing checks, so a verifier crash can never masquerade as
            # the documented control rejection.  Bytes are consumed only through this route;
            # the store is never searched directly.
            status, preview_payload, _ = _http_json(
                "GET", f"{portal_base}/api/runs/{run_id}/preview", token=token, cookies=portal_cookie
            )
            # The full quality document comes through the artifact route, which binds the
            # bytes to the digest its own preview declared (no client-supplied digest);
            # every check must carry definite execution evidence, so "absent from the
            # failing summary" can never pass for "executed and passed".
            document_status, quality_doc, _ = _http_json(
                "GET",
                f"{portal_base}/api/runs/{run_id}/artifacts/reports/quality.json",
                token=token,
                cookies=portal_cookie,
            )
            checks = [
                item for item in (quality_doc.get("checks") or []) if isinstance(item, dict)
            ] if isinstance(quality_doc, dict) else []
            preview_failing = {
                str(item.get("id"))
                for item in (preview_payload.get("failing_checks") or [])
                if isinstance(item, dict)
            }
            quality = _quality_rejection_comparisons(checks, preview_failing)
            subject = str(preview_payload.get("subject_sha256") or "")
            rejection = {
                "served": status == 200,
                "reason": preview_payload.get("reason"),
                "verified": preview_payload.get("verified"),
                "error": preview_payload.get("error"),
                "failed_gates": sorted(quality["failing"]),
                "unexpected_gates": quality["unexpected_gates"],
                "missing_required_gates": quality["missing_required_gates"],
                "subject_sha256": subject or None,
                "urdf": preview_payload.get("urdf"),
                "quality": {
                    "document_served": document_status == 200,
                    "checks": len(checks),
                    "execution_evidence": quality["executed"],
                    "consistent": quality["consistent"],
                    "views_agree": quality["views_agree"],
                    "allowed_failure": quality["allowed_failure"],
                    "document_subject_sha256": quality_doc.get("subject_sha256")
                    if isinstance(quality_doc, dict)
                    else None,
                },
                "subject_binding": "route-bound preview and quality document; platform receipts at wiring",
            }
            if status != 200:
                problems.append(f"the diagnostic preview could not be served: {status}")
            else:
                if (
                    preview_payload.get("reason") != "verification_failed"
                    or preview_payload.get("verified") is not False
                ):
                    problems.append(f"the rejection is not the bound verification failure: {rejection}")
                if "URDF verification failed" not in str(preview_payload.get("error") or ""):
                    problems.append(f"the rejection error is not the documented verification failure: {rejection}")
                if document_status != 200:
                    problems.append(f"the quality document could not be served: {document_status}")
                elif not quality["executed"] or not quality["consistent"]:
                    problems.append(f"quality checks carry no definite execution evidence: {rejection['quality']}")
                elif not quality["views_agree"]:
                    problems.append(
                        f"the quality document and the diagnostic summary disagree on failures: {rejection}"
                    )
                if not quality["allowed_failure"]:
                    problems.append(
                        "the expected rejection requires a non-empty allowed failure set: "
                        + str(rejection["quality"])
                    )
                if quality["unexpected_gates"]:
                    problems.append(
                        f"quality failed outside the documented control limits: {quality['unexpected_gates']}"
                    )
                if quality["missing_required_gates"]:
                    problems.append(
                        f"required gates are missing or not passed: {quality['missing_required_gates']}"
                    )
                document_subject = (
                    str(quality_doc.get("subject_sha256") or "") if isinstance(quality_doc, dict) else ""
                )
                if (
                    len(subject) != 64
                    or any(char not in "0123456789abcdef" for char in subject)
                    or document_subject != subject
                ):
                    problems.append(
                        "the preview and the quality document do not agree on one bound subject digest"
                    )
            sql_evidence = _platform_sql_evidence(
                args.platform_schema,
                native_id,
                projection,
                request_id=f"portal-{run_id}",
            )
            problems.extend(sql_evidence.get("problems", []))
            if identity_stable is False:
                problems.append(
                    "the rebuilt fixture differs from the uploaded placeholder fixture; "
                    "the run identity binding is unstable"
                )
        if dag_state != "failed":
            problems.append(f"DAG run state is {dag_state!r}, expected the documented rejection (failed)")
        for task_id, expected_state in (
            ("freeze", "success"),
            ("discover", "success"),
            ("capture", "success"),
            ("generate", "success"),
            ("verify", "failed"),
        ):
            actual = task_states.get(task_id, {}).get("state")
            if actual != expected_state:
                problems.append(f"task {task_id} is {actual!r}, expected {expected_state!r}")
        for task_id in ("publish",):
            # Blocked without execution: the scheduler marked the task upstream_failed/skipped and
            # it never entered a running attempt, so a publish that started and failed early can
            # never be mistaken for a prevention proof.
            actual = task_states.get(task_id) or {}
            if actual.get("state") not in {"upstream_failed", "skipped"} or actual.get("try_number"):
                problems.append(
                    f"task {task_id} is {actual.get('state')!r} with {actual.get('try_number')!r} attempts; "
                    "publication must be blocked without ever executing"
                )
        # Slot-release proof: the run's own TaskReschedule rows for the native wait task plus an
        # observed up_for_reschedule state on that task.  Counts, other tasks and the configured
        # mode string are not evidence.
        reschedule_query = (
            f"TaskReschedule(dag_id={DAG_ID!r}, run_id={run_id!r}, task_id='discover') "
            "via the isolated home's Airflow ORM"
        )
        reschedule_rows, reschedule_error = _task_reschedule_evidence(args.venv, env, DAG_ID, run_id, "discover")
        if reschedule_error is not None:
            problems.append(f"the TaskReschedule evidence could not be read: {reschedule_error}")
        if not reschedule_rows:
            problems.append("no TaskReschedule rows exist for discover; the native wait's slot release is not proven")
        if not reschedule_observed:
            problems.append("discover was never observed in up_for_reschedule; its slot release is not proven")

        jobs = list(state["jobs"].values())
        if len(jobs) != 1 or jobs[0].get("status") != "native_complete":
            problems.append("the mocked endpoint did not serve exactly one native_complete job")
        if state["uploads"] != 1:
            problems.append("the full-folder staging upload did not happen exactly once")
        if not state["definition_posts"]:
            problems.append("the generated text definition was never submitted")
        if len(state["artifact_paths"]) < 2:
            problems.append("the raw record and the sealed capture transfer were not both streamed")
        report = {
            "ok": not problems,
            "qualification": QUALIFICATION,
            "run_id": run_id,
            "native_run_id": native_id,
            "api_port": api_port,
            "auth_health": auth_health,
            "trigger": trigger_kind,
            "native_endpoint": "mock (transport-only)",
            "scope": {
                "platform_schema": args.platform_schema or None,
                "airflow_schema": args.airflow_schema or None,
                "portal_admission": portal_mode,
                # Exported by the operator environment from the install snapshot digest;
                # the run freeze consumes it (conf first, environment second).
                "platform_config_digest": os.environ.get("PLATFORM_CONFIG_DIGEST") or None,
            },
            "triggering_user_name": triggering_user_name,
            "dag_state": dag_state,
            "reschedule_evidence": {
                "query": reschedule_query,
                "rows": reschedule_rows,
                "observed": reschedule_observed,
                "error": reschedule_error,
            },
            "task_states": task_states,
            "selection_uploads": state["uploads"],
            "definition_posts": state["definition_posts"],
            "artifact_paths": state["artifact_paths"],
            "endpoint_aborts": state.get("aborts") or None,
            "handoff_sha256": fixture["handoff_sha256"],
            "target_hardware": target,
            "fixture_identity_stable": identity_stable,
            "platform_sql": sql_evidence,
            "platform_admission": admission,
            "verify_rejection": rejection,
            "portal_projection": {
                "dag_run_id": run_id,
                "state": projection.get("state") if isinstance(projection, dict) else None,
                "tasks": projection.get("tasks") if isinstance(projection, dict) else None,
                "handoff_path": projection.get("handoff_path") if isinstance(projection, dict) else None,
            },
            "negative_http": negatives,
            "problems": problems,
        }
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(
                json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
        print(json.dumps(report))
        return 0 if not problems else 1
    finally:
        stack.close()
        for proc in procs:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        for proc in procs:
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=20)
        for httpd in (server, portal_server):
            if httpd is not None:
                httpd.shutdown()
                httpd.server_close()


if __name__ == "__main__":
    raise SystemExit(main())
