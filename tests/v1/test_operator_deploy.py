"""Operator deployment renderer/lifecycle tests (stdlib only; external services are mocked)."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import shlex
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from description_pipeline import schemas
from description_pipeline import interface_records as ir
from description_pipeline.stages import CONTRACT, CONTRACT_SHA256

from .contract_wheel import write_contract_wheel

ROOT = Path(__file__).resolve().parents[2]
OPERATOR = ROOT / "deploy" / "operator"


_RENDER_MODULE: object | None = None


def _render_module():
    global _RENDER_MODULE
    if _RENDER_MODULE is None:
        spec = importlib.util.spec_from_file_location("render_operator_under_test", OPERATOR / "render_operator.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _RENDER_MODULE = module
    return _RENDER_MODULE


def decode_env_value(raw: str) -> str:
    """The canonical env decode (render_operator.encode_env_value's exact inverse)."""

    return _render_module().decode_env_value(raw)


def encode_env_value(value: str) -> str:
    """The canonical env encoding, so fixtures write exactly what the writers would."""

    return _render_module().encode_env_value(value)
RENDER = OPERATOR / "render_operator.py"
CONTROL = OPERATOR / "operatorctl.sh"
HEALTH = OPERATOR / "health.sh"
HEALTH_GATE = OPERATOR / "health_gate.sh"

# Copied byte-for-byte from the W5 v8r5 R2 Windows response (including CRLF). The Windows
# lifecycle's Die helper emits this passed:false object and exits 1; ssh preserves rc 1.
CAPTURED_WINDOWS_CANDIDATE_CONFIG_MISMATCH = (
    '{"message":"the staged candidate configuration differs from the staged digest: '
    'C:\\\\description-v1-review\\\\endpoint-state\\\\lifecycle\\\\gen150-f2c5463\\\\generation\\\\endpoint.json",'
    '"code":"candidate_config_mismatch","verb":"promote","passed":false}\r\n'
)


def base_env(state: Path, **overrides: str) -> dict[str, str]:
    env = {
        "OPERATOR_HOST": "rehearsal.local",
        "OPERATOR_BIND": "127.0.0.1",
        "OPERATOR_HTTPS_PORT": "18443",
        "OPERATOR_STATE": str(state),
        "OPERATOR_UPSTREAM": "127.0.0.1:18788",
        "AIRFLOW_VENV": str(state.parent / "venv"),
        "AIRFLOW_HOME": str(state.parent / "home"),
        "AIRFLOW_DB_URL": "postgresql+psycopg2://solidworks@/airflow_meta?host=/tmp/socket&port=5433",
        "NGINX_BIN": "/usr/sbin/nginx",
        "SOLIDWORKS_SSH_HOST": "windows-m3",
        "SOLIDWORKS_HANDOFF_ROOT": str(state.parent / "handoffs"),
    }
    env.update(overrides)
    return env


def run(cmd: list[str], env: dict[str, str], cwd: Path = ROOT) -> subprocess.CompletedProcess:
    merged = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(Path.home()),
        "PYTHONPATH": str(ROOT / "src"),
    }
    merged.update(env)
    return subprocess.run(cmd, capture_output=True, text=True, env=merged, cwd=str(cwd))


def render(state: Path, **overrides: str) -> subprocess.CompletedProcess:
    env = base_env(state, **overrides)
    env_file = state.parent / "operator.env"
    env_file.parent.mkdir(parents=True, exist_ok=True)
    env_file.write_text(
        "".join(f"{key}={encode_env_value(value)}\n" for key, value in env.items()), encoding="utf-8"
    )
    return run([sys.executable, str(RENDER), "--env-file", str(env_file)], env)


def platform_fixture(
    tmp: Path,
    *,
    install_id: str = "gen-1",
    host: str = "rehearsal.local",
    https_port: int = 18443,
    ssh_host: str = "windows-m3",
) -> tuple[Path, Path, dict[str, str]]:
    """Config-driven fixture: platform.json + snapshot + derived env + active record.

    The snapshot and the per-generation environment are produced by the real modules; the
    active record is a minimal fixture (its install_id is the only field load_active_env
    consumes - the full record binding is exercised by the install tests and
    tests/v1/test_install_record.py).
    """

    state = tmp / "state"
    secrets = tmp / "secrets"
    secrets.mkdir(exist_ok=True)
    generated = subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-sha256",
            "-nodes",
            "-days",
            "1",
            "-subj",
            f"/CN={host}",
            "-addext",
            f"subjectAltName=DNS:{host}",
            "-keyout",
            str(secrets / "tls.key"),
            "-out",
            str(secrets / "tls.crt"),
        ],
        capture_output=True,
        text=True,
    )
    if generated.returncode != 0:
        raise AssertionError(generated.stderr)
    for name in ("tls.crt", "tls.key"):
        (secrets / name).chmod(0o600)
    feishu = secrets / "feishu.json"
    feishu.write_text('{"app_id": "cli_test", "app_secret": "s"}\n', encoding="utf-8")
    feishu.chmod(0o600)
    token = secrets / "endpoint.token"
    token.write_text("test-token\n", encoding="utf-8")
    token.chmod(0o600)
    wheel = tmp / "mimicverse_description-test-py3-none-any.whl"
    write_contract_wheel(wheel)
    (tmp / "description-1.4.0-windows-cp312-x86_64.zip").write_bytes(b"windows-archive")
    (tmp / "description-1.4.0-linux-cp312-x86_64.zip").write_bytes(b"linux-archive")
    (tmp / "description-1.4.0-deployment.zip").write_bytes(b"deployment-archive")
    release_document = {
        "schema_version": "solidworks-to-urdf.release/v1",
        "pipeline_id": "solidworks-to-urdf",
        "version": "1.4.0",
        "source_commit": "c" * 40,
        "source_sha256": "a" * 64,
        "artifacts": {
            wheel.name: hashlib.sha256(wheel.read_bytes()).hexdigest(),
            "description-1.4.0-windows-cp312-x86_64.zip": hashlib.sha256(b"windows-archive").hexdigest(),
            "description-1.4.0-linux-cp312-x86_64.zip": hashlib.sha256(b"linux-archive").hexdigest(),
            "description-1.4.0-deployment.zip": hashlib.sha256(b"deployment-archive").hexdigest(),
        },
    }
    release_path = tmp / "release.json"
    release_path.write_text(json.dumps(release_document), encoding="utf-8")
    config = {
        "schema_version": schemas.PLATFORM_CONFIG_SCHEMA,
        "operator": {
            "origin": f"https://{host}:{https_port}",
            "certificate_file": str(secrets / "tls.crt"),
            "private_key_file": str(secrets / "tls.key"),
        },
        "storage": {
            "state_root": str(state),
            "handoff_root": str(tmp / "handoffs"),
            "artifact_root": str(tmp / "artifacts"),
            "workspace_root": str(tmp / "workspaces"),
            "release_root": str(tmp / "releases"),
        },
        "database": {"dsn_file": str(secrets / "postgres.dsn")},
        "windows": {
            "ssh_host": ssh_host,
            "endpoint_port": 9765,
            "token_file": str(token),
            "instance": {
                "task_name": "description-e2e-endpoint",
                "package_root": "C:/description-e2e/packages",
                "output_root": "C:/description-e2e/results",
                "state_root": "C:/description-e2e/state",
            },
        },
        "auth": {
            "feishu_secret_file": str(feishu),
            "tenant_keys": ["tenant-key"],
            "admin_open_ids": [],
        },
        "targets": {"m3.0": {"repository_slug": "owner/model-repository", "base": "main"}},
        "specifications": {"record_roots": [str(tmp / "specifications")]},
    }
    config_path = tmp / "platform.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    rendered = run(
        [
            sys.executable,
            str(OPERATOR / "platform_config.py"),
            "render",
            "--config",
            str(config_path),
            "--install-id",
            install_id,
            "--wheel",
            str(wheel),
        ],
        {},
    )
    if rendered.returncode != 0:
        raise AssertionError(rendered.stderr)
    snapshot = next(line.split("=", 1)[1] for line in rendered.stdout.splitlines() if line.startswith("snapshot="))
    emitted = run(
        [
            sys.executable,
            str(OPERATOR / "platform_env.py"),
            "emit",
            "--snapshot",
            snapshot,
            "--install-id",
            install_id,
            "--tree",
            str(tmp),
            "--release-file",
            str(release_path),
        ],
        {},
    )
    if emitted.returncode != 0:
        raise AssertionError(emitted.stderr)
    env_path = next(line.split("=", 1)[1] for line in emitted.stdout.splitlines() if line.startswith("env="))
    env: dict[str, str] = {}
    for line in Path(env_path).read_text(encoding="utf-8").splitlines():
        key, _, value = line.partition("=")
        if key:
            env[key] = value
    # One managed proxy binary for the whole fixture: the lifecycle resolves NGINX_BIN from
    # resolved.env, so the render pins the same stubbed executable the control verbs see.
    (tmp / "bin").mkdir(exist_ok=True)
    fixture_nginx = tmp / "bin" / "nginx"
    write_stub(fixture_nginx, "#!/bin/bash\nexit 0\n")
    env["NGINX_BIN"] = str(fixture_nginx)
    repinned = run(
        [
            sys.executable,
            str(OPERATOR / "render_operator.py"),
            "--env-file",
            env_path,
            "--nginx-bin",
            str(fixture_nginx),
        ],
        {},
    )
    if repinned.returncode != 0:
        raise AssertionError(repinned.stderr)
    venv_bin = Path(env["AIRFLOW_VENV"]) / "bin"
    venv_bin.mkdir(parents=True, exist_ok=True)
    write_stub(venv_bin / "python", '#!/bin/bash\nexec python3 "$@"\n')
    identity_path = tmp / "endpoint-identity.json"
    identity_path.write_text(
        json.dumps(
            {
                "install_id": "ep-1",
                "config_digest": "d" * 64,
                "release": {
                    "version": release_document["version"],
                    "source_commit": release_document["source_commit"],
                    "source_sha256": release_document["source_sha256"],
                    "role": "native",
                },
            }
        ),
        encoding="utf-8",
    )
    dag_path = tmp / "solidworks_to_urdf.py"
    dag_path.write_text("# dag fixture\n", encoding="utf-8")
    built = run(
        [
            sys.executable,
            str(OPERATOR / "install_record.py"),
            "build",
            "--state-root",
            str(state),
            "--install-id",
            install_id,
            "--snapshot",
            snapshot,
            "--release-file",
            str(release_path),
            "--endpoint-identity",
            str(identity_path),
            "--services",
            "description-postgres,description-airflow-scheduler,description-operator-proxy",
            "--dag",
            str(dag_path),
            "--wheel",
            str(wheel),
        ],
        {},
    )
    if built.returncode != 0:
        raise AssertionError(built.stderr)
    return config_path, Path(env_path), env


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tree_state(root: Path) -> dict:
    """Every path under ``root`` with its mtime, size and content digest (read-only snapshot)."""

    state = {}
    for path in sorted(root.rglob("*")):
        metadata = path.lstat()
        if path.is_file():
            state[str(path.relative_to(root))] = (
                metadata.st_mtime_ns,
                metadata.st_size,
                hashlib.sha256(path.read_bytes()).hexdigest(),
            )
        else:
            state[str(path.relative_to(root))] = ("dir", metadata.st_mtime_ns)
    return state


def write_stub(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


class RenderTests(unittest.TestCase):
    def test_linux_discovery_settings_are_rendered_and_changes_are_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            settings = root / "discovery.json"
            expected = {"record_roots": [str(root / "records")], "frozen_names_file": str(root / "names.json")}
            settings.write_text(json.dumps(expected))
            settings.chmod(0o600)
            rendered = render(state, DISCOVERY_SETTINGS_FILE=str(settings))
            self.assertEqual(rendered.returncode, 0, rendered.stderr)
            config = json.loads((state / "portal.json").read_text())
            self.assertEqual(config["pipeline"]["discovery"], expected)
            before = (state / "portal.json").read_bytes()
            settings.write_text(json.dumps({"record_roots": [str(root / "new-records")]}))
            drift = run([sys.executable, str(RENDER), "--env-file", str(root / "operator.env"), "--dry-run"], {})
            self.assertNotEqual(drift.returncode, 0)
            self.assertIn("portal.json", drift.stdout + drift.stderr)
            self.assertEqual((state / "portal.json").read_bytes(), before)
            settings.write_text(json.dumps({"record_roots": ["relative"]}))
            self.assertNotEqual(render(state, DISCOVERY_SETTINGS_FILE=str(settings)).returncode, 0)

    def test_linux_routes_are_rendered_and_mapping_changes_are_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            mapping = root / "repositories.json"
            mapping.write_text(json.dumps({"example/robot": str(root / "models/robot")}))
            mapping.chmod(0o600)
            result = render(state, MODEL_REPOSITORIES_FILE=str(mapping))
            self.assertEqual(result.returncode, 0, result.stderr)
            config = json.loads((state / "portal.json").read_text())
            self.assertEqual(config["pipeline"]["repositories"], {"example/robot": str(root / "models/robot")})
            old = (state / "portal.json").read_bytes()
            mapping.write_text(json.dumps({"example/robot": str(root / "models/new-checkout")}))
            drift = run([sys.executable, str(RENDER), "--env-file", str(root / "operator.env"), "--dry-run"], {})
            self.assertNotEqual(drift.returncode, 0)
            self.assertIn("portal.json", drift.stdout + drift.stderr)
            self.assertEqual((state / "portal.json").read_bytes(), old)

    def test_linux_store_and_checkout_cannot_overlap_intake_or_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            for path in (root / "handoffs", root / "venv/checkpoints", state, Path("/srv")):
                with self.subTest(store=str(path)):
                    self.assertNotEqual(render(state, PIPELINE_STORE_ROOT=str(path)).returncode, 0)
            mapping = root / "repositories.json"
            mapping.write_text(json.dumps({"example/robot": str(state / "runs/robot")}))
            mapping.chmod(0o600)
            rejected = render(state, MODEL_REPOSITORIES_FILE=str(mapping))
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn("overlap", rejected.stderr)
            mapping.write_text(json.dumps({"../robot": str(root / "models/robot")}))
            self.assertNotEqual(render(state, MODEL_REPOSITORIES_FILE=str(mapping)).returncode, 0)

    def test_render_private_idempotent_and_shell_safe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            first = render(state)
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(stat.S_IMODE(state.stat().st_mode), 0o700)
            for secret in (state / "secrets/tls.key", state / "nginx/nginx.conf"):
                self.assertEqual(stat.S_IMODE(secret.stat().st_mode), 0o600, secret)
            before = {name: digest(state / "secrets" / name) for name in ("tls.crt", "tls.key")}
            # resolved.env must be sourceable even though it carries a DSN with '&' and a command with spaces.
            sourced = run(["bash", "-c", f'source "{state}/resolved.env"; echo "$OPERATOR_URL"'], {})
            self.assertEqual(sourced.returncode, 0, sourced.stderr)
            self.assertEqual(sourced.stdout.strip(), "https://rehearsal.local:18443/")
            second = render(state)
            self.assertEqual(second.returncode, 0, second.stderr)
            after = {name: digest(state / "secrets" / name) for name in before}
            self.assertEqual(before, after)
            san = run(
                ["openssl", "x509", "-in", str(state / "secrets/tls.crt"), "-noout", "-ext", "subjectAltName"], {}
            )
            self.assertEqual(san.returncode, 0, san.stderr)
            self.assertIn("DNS:rehearsal.local", san.stdout)

    def test_port_overrides_render_into_every_artifact(self) -> None:
        """DEP-01: the renderer is the one deploy source; overrides reach every rendered file."""

        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            result = render(state, AIRFLOW_API_PORT="19991", TUNNEL_LOOPBACK_PORT="19965")
            self.assertEqual(result.returncode, 0, result.stderr)
            nginx = (state / "nginx/nginx.conf").read_text(encoding="utf-8")
            self.assertIn("proxy_pass http://127.0.0.1:19991;", nginx)
            self.assertNotIn("8791", nginx)
            portal = (state / "portal.json").read_text(encoding="utf-8")
            self.assertIn('"url": "http://127.0.0.1:19965"', portal)
            resolved = (state / "resolved.env").read_text(encoding="utf-8")
            self.assertIn("AIRFLOW_API_PORT=19991", resolved)
            self.assertIn("TUNNEL_LOOPBACK_PORT=19965", resolved)

    def test_rejects_invalid_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            relative = render(state, OPERATOR_STATE="relative/state")
            self.assertNotEqual(relative.returncode, 0)
            self.assertIn("absolute", relative.stderr)
            bad_port = render(state, OPERATOR_HTTPS_PORT="0")
            self.assertNotEqual(bad_port.returncode, 0)
            collision = render(state, OPERATOR_UPSTREAM="127.0.0.1:18443")
            self.assertNotEqual(collision.returncode, 0)
            self.assertIn("collide", collision.stderr)
            provided = render(state, OPERATOR_TLS_CERT=str(state / "missing.crt"))
            self.assertNotEqual(provided.returncode, 0)
            self.assertIn("together", provided.stderr)
            bad_host = render(state, OPERATOR_HOST="bad host")
            self.assertNotEqual(bad_host.returncode, 0)
            without_tunnel = base_env(state)
            without_tunnel.pop("SOLIDWORKS_SSH_HOST")
            env_file = state.parent / "no-tunnel.env"
            env_file.write_text("".join(f"{key}={value}\n" for key, value in without_tunnel.items()), encoding="utf-8")
            missing_tunnel = run([sys.executable, str(RENDER), "--env-file", str(env_file)], {})
            self.assertNotEqual(missing_tunnel.returncode, 0)
            self.assertIn("SOLIDWORKS_SSH_HOST", missing_tunnel.stderr)
            for broad in ("/", "/home", str(Path.home()), str(state)):
                rejected = render(state, SOLIDWORKS_HANDOFF_ROOT=broad)
                self.assertNotEqual(rejected.returncode, 0, broad)
            for unsupported in (
                "OPERATOR_BASIC_USER",
                "OPERATOR_BASIC_PASSWORD",
                "OPERATOR_HTPASSWD_FILE",
                "FEISHU_APP_ID",
                "FEISHU_AUTHORIZE_BASE",
                "FEISHU_TOKEN_URL",
                "FEISHU_USERINFO_URL",
                "FEISHU_STATE_TTL_SECONDS",
            ):
                rejected = render(state, **{unsupported: "x"})
                self.assertNotEqual(rejected.returncode, 0, unsupported)
                self.assertIn(unsupported, rejected.stderr)

    def test_single_https_listener_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            self.assertEqual(render(state).returncode, 0)
            config = (state / "nginx/nginx.conf").read_text(encoding="utf-8")
            self.assertNotIn("default_server", config)
            self.assertNotIn("listen 127.0.0.1:80", config)
            self.assertEqual(config.count("listen "), 1)
            self.assertIn("listen 127.0.0.1:18443 ssl;", config)
            self.assertNotIn("auth_basic", config)
            # Feishu SSO callback is the only Airflow route exposed; the operator page stays on
            # the portal upstream.
            self.assertIn("location ^~ /auth/feishu/ {", config)
            self.assertIn("proxy_pass http://127.0.0.1:8791;", config)
            self.assertIn("proxy_pass http://127.0.0.1:18788;", config)

    def test_access_logs_exclude_oauth_queries_and_headers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            self.assertEqual(render(state).returncode, 0)
            config = (state / "nginx/nginx.conf").read_text(encoding="utf-8")
            # Redirects must preserve OAuth parameters; only logging must exclude them.
            logging = "\n".join(re.findall(r"\b(?:log_format|access_log)\s+[^;]*;", config))
            self.assertIn("$request_method $uri $server_protocol", logging)
            access_logs = re.findall(r"\baccess_log\s+[^;]*;", config)
            self.assertTrue(access_logs)
            for directive in access_logs:
                self.assertTrue(directive.endswith(" operator_uri_only;") or directive == "access_log off;")
            for sensitive in (
                "$request_uri",
                "$args",
                "$query_string",
                "$http_referer",
                "$http_cookie",
                "$http_authorization",
            ):
                self.assertNotIn(sensitive, logging)
            self.assertNotIn('"$request"', logging)
            airflow = (ROOT / "deploy/airflow/airflow.cfg.template").read_text(encoding="utf-8")
            self.assertIn("namespace_levels = http.access=WARNING", airflow)

    def test_check_paths_reports_missing_runtime_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            env = base_env(state)
            env_file = Path(tmp) / "operator.env"
            env_file.write_text("".join(f"{k}={v}\n" for k, v in env.items()), encoding="utf-8")
            missing = run(
                [sys.executable, str(RENDER), "--env-file", str(env_file), "--check-paths"],
                {"NGINX_BIN": str(Path(tmp) / "nginx")},
            )
            self.assertNotEqual(missing.returncode, 0)
            self.assertIn("NGINX_BIN", missing.stderr)

    def test_portal_config_and_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            self.assertEqual(render(state).returncode, 0)
            resolved = (state / "resolved.env").read_text(encoding="utf-8")
            self.assertIn("-m description_pipeline.orchestration.portal --config", resolved)
            portal = (state / "portal.json").read_text(encoding="utf-8")
            self.assertIn('"url": "http://127.0.0.1:8791"', portal)
            self.assertIn('"url": "http://127.0.0.1:18765"', portal)
            self.assertIn('"port": 18788', portal)
            # The portal refuses unknown keys, so the bootstrap config stays exactly four
            # sections; upload_root names the single Linux intake used by the folder picker.
            import json

            parsed = json.loads(portal)
            self.assertEqual(sorted(parsed), ["airflow", "endpoint", "pipeline", "portal"])
            self.assertEqual(
                parsed["pipeline"],
                {"store_root": str(state / "runs"), "repositories": {}, "targets": {}, "discovery": {}},
            )
            self.assertEqual(sorted(parsed["airflow"]), ["url"])
            self.assertEqual(sorted(parsed["endpoint"]), ["token_file", "url"])
            self.assertEqual(sorted(parsed["portal"]), ["host", "port", "upload_root"])
            # Deterministic: a changed environment re-renders the current config, identical
            # environments produce identical bytes, and unknown keys are not passed through.
            first = (state / "portal.json").read_bytes()
            self.assertEqual(render(state).returncode, 0)
            self.assertEqual(first, (state / "portal.json").read_bytes())
            moved = render(state, OPERATOR_UPSTREAM="127.0.0.1:18799")
            self.assertEqual(moved.returncode, 0, moved.stderr)
            self.assertIn('"port": 18799', (state / "portal.json").read_text(encoding="utf-8"))
            ignored = render(state, PORTAL_CONFIG="/tmp/legacy.json", PORTAL_COMMAND="legacy --flag")
            self.assertEqual(ignored.returncode, 0, ignored.stderr)
            resolved_text = (state / "resolved.env").read_text(encoding="utf-8")
            self.assertNotIn("PORTAL_CONFIG=/tmp/legacy.json", resolved_text)
            self.assertIn("description_pipeline.orchestration.portal --config", resolved_text)

    def test_private_key_never_written_into_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            result = render(state)
            self.assertEqual(result.returncode, 0, result.stderr)
            key_body = (state / "secrets/tls.key").read_text(encoding="utf-8").splitlines()
            fingerprint = key_body[len(key_body) // 2].strip()
            self.assertGreater(len(fingerprint), 20)
            for path in OPERATOR.rglob("*"):
                if path.is_file() and path.suffix != ".pyc":
                    self.assertNotIn(fingerprint, path.read_text(errors="ignore"), path)

    def test_dry_run_detects_drift_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            self.assertEqual(render(state).returncode, 0)
            env_file = state.parent / "operator.env"
            installed = (state / "portal.json").read_bytes()
            clean = run([sys.executable, str(RENDER), "--env-file", str(env_file), "--dry-run"], {})
            self.assertEqual(clean.returncode, 0, clean.stdout + clean.stderr)
            self.assertIn("DRIFT none", clean.stdout)
            drifted_env = base_env(state, OPERATOR_UPSTREAM="127.0.0.1:18799")
            drift_file = state.parent / "drifted.env"
            drift_file.write_text("".join(f"{key}={value}\n" for key, value in drifted_env.items()), encoding="utf-8")
            drifted = run([sys.executable, str(RENDER), "--env-file", str(drift_file), "--dry-run"], {})
            self.assertEqual(drifted.returncode, 1)
            self.assertIn("DRIFT", drifted.stdout)
            self.assertEqual(installed, (state / "portal.json").read_bytes())

    def test_feishu_env_render_mode_and_redirect(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            secret = Path(tmp) / "feishu_app.json"
            secret.write_text('{"app_id": "cli_x", "app_secret": "s"}\n', encoding="utf-8")
            secret.chmod(0o600)
            result = render(
                state,
                FEISHU_APP_SECRET_FILE=str(secret),
                FEISHU_TENANT_KEYS="tenant_a,tenant_b",
                FEISHU_ADMIN_OPEN_IDS="ou_admin",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            feishu = state / "feishu.env"
            self.assertEqual(stat.S_IMODE(feishu.stat().st_mode), 0o600)
            text = feishu.read_text(encoding="utf-8")
            self.assertIn(f"FEISHU_APP_SECRET_FILE={secret}", text)
            self.assertIn("FEISHU_TENANT_KEYS=tenant_a,tenant_b", text)
            self.assertIn("FEISHU_REDIRECT_URI=https://rehearsal.local:18443/auth/feishu/callback", text)
            self.assertIn("FEISHU_ADMIN_OPEN_IDS=ou_admin", text)
            resolved = (state / "resolved.env").read_text(encoding="utf-8")
            self.assertIn(f"FEISHU_ENV_FILE={state}/feishu.env", resolved)
            # The optional admin list disappears when unset; credentials are optional at install
            # time (the auth manager then fails explicitly instead of falling back).
            self.assertEqual(
                render(state, FEISHU_APP_SECRET_FILE=str(secret), FEISHU_TENANT_KEYS="tenant_a").returncode, 0
            )
            self.assertNotIn("FEISHU_ADMIN_OPEN_IDS", (state / "feishu.env").read_text(encoding="utf-8"))
            self.assertEqual(render(state).returncode, 0)
            text = (state / "feishu.env").read_text(encoding="utf-8")
            self.assertIn("FEISHU_REDIRECT_URI=", text)
            self.assertNotIn("FEISHU_APP_SECRET_FILE", text)
            self.assertNotIn("FEISHU_TENANT_KEYS", text)

    def test_rejects_bad_feishu_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            missing = render(state, FEISHU_APP_SECRET_FILE=str(Path(tmp) / "nope.json"), FEISHU_TENANT_KEYS="tenant_a")
            self.assertNotEqual(missing.returncode, 0)
            self.assertIn("FEISHU_APP_SECRET_FILE", missing.stderr)
            relative = render(state, FEISHU_APP_SECRET_FILE="feishu.json", FEISHU_TENANT_KEYS="tenant_a")
            self.assertNotEqual(relative.returncode, 0)
            self.assertIn("absolute", relative.stderr)
            loose = Path(tmp) / "loose.json"
            loose.write_text("{}", encoding="utf-8")
            loose.chmod(0o644)
            world_readable = render(state, FEISHU_APP_SECRET_FILE=str(loose), FEISHU_TENANT_KEYS="tenant_a")
            self.assertNotEqual(world_readable.returncode, 0)
            self.assertIn("0600", world_readable.stderr)
            half = render(state, FEISHU_APP_SECRET_FILE=str(loose))
            self.assertNotEqual(half.returncode, 0)
            self.assertIn("together", half.stderr)
            keys_only = render(state, FEISHU_TENANT_KEYS="tenant_a")
            self.assertNotEqual(keys_only.returncode, 0)
            self.assertIn("together", keys_only.stderr)

    def test_dry_run_covers_feishu_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            secret = Path(tmp) / "feishu_app.json"
            secret.write_text('{"app_id": "cli_x", "app_secret": "s"}\n', encoding="utf-8")
            secret.chmod(0o600)
            self.assertEqual(
                render(state, FEISHU_APP_SECRET_FILE=str(secret), FEISHU_TENANT_KEYS="tenant_a").returncode, 0
            )
            env_file = state.parent / "operator.env"
            clean = run([sys.executable, str(RENDER), "--env-file", str(env_file), "--dry-run"], {})
            self.assertEqual(clean.returncode, 0, clean.stdout + clean.stderr)
            (state / "feishu.env").unlink()
            missing = run([sys.executable, str(RENDER), "--env-file", str(env_file), "--dry-run"], {})
            self.assertEqual(missing.returncode, 1)
            self.assertIn("feishu.env", missing.stdout)


class LifecycleTests(unittest.TestCase):

    def _control_tree(self, tmp: Path) -> Path:
        """A release-local operator mirror: real siblings, a stub health probe.

        The lifecycle calls its own sibling health_gate.sh whose probe is the sibling
        health.sh; the mirror keeps every real file while the probe becomes a stub, so the
        suite exercises the real gate wiring without live HTTP services.
        """
        release = tmp / "release"
        op = release / "deploy" / "operator"
        op.mkdir(parents=True)
        (release / "release.json").write_text("{}\n", encoding="utf-8")
        for name in ("airflow", "platform"):
            os.symlink(OPERATOR.parent / name, release / "deploy" / name)
        for item in OPERATOR.iterdir():
            if item.name in {"operatorctl.sh", "health_gate.sh", "health.sh"}:
                continue
            os.symlink(item, op / item.name)
        (op / "operatorctl.sh").write_bytes(CONTROL.read_bytes())
        (op / "health_gate.sh").write_bytes(HEALTH_GATE.read_bytes())
        write_stub(
            op / "health.sh",
            '#!/usr/bin/env bash\necho "health-gate probe" >> "$ORDER_LOG"\nexit "${HEALTH_GATE_RC:-0}"\n',
        )
        return op / "operatorctl.sh"
    def _render_units(self, tmp: Path) -> tuple[Path, Path, Path, Path]:
        config_path, env_path, env = platform_fixture(tmp)
        state = Path(env["OPERATOR_STATE"])
        config_home = tmp / "config"
        target = config_home / "systemd" / "user"
        target.mkdir(parents=True)
        rendered = run(
            [
                sys.executable,
                str(RENDER),
                "--env-file",
                str(env_path),
                "--nginx-bin",
                env["NGINX_BIN"],
                "--units-dir",
                str(target),
            ],
            {"XDG_CONFIG_HOME": str(config_home)},
        )
        self.assertEqual(rendered.returncode, 0, rendered.stderr)
        self._render_airflow_units(state, config_home, derived_env=True)
        return config_path, state, config_home, target

    def _render_airflow_units(self, state: Path, config_home: Path, *, derived_env: bool) -> None:
        airflow_env = {"XDG_CONFIG_HOME": str(config_home)}
        for line in (state / "resolved.env").read_text(encoding="utf-8").splitlines():
            key, _, value = line.partition("=")
            if key:
                airflow_env[key] = value
        if derived_env:
            airflow_env["OPERATOR_DERIVED_ENV"] = str(state / "derived" / "gen-1" / "operator.env")
        airflow = run(["bash", str(OPERATOR.parent / "airflow" / "services.sh"), "render"], airflow_env)
        self.assertEqual(airflow.returncode, 0, airflow.stderr)

    def _admission_fixture(self, tmp: Path, *, register_after: int = 0, unpause_rc: int = 0, pause_rc: int = 0):
        config_path, state, config_home, _ = self._render_units(tmp)
        derived_env: dict[str, str] = {}
        for line in (state / "derived" / "gen-1" / "operator.env").read_text(encoding="utf-8").splitlines():
            key, _, value = line.partition("=")
            if key:
                derived_env[key] = decode_env_value(value.strip())
        bindir = tmp / "bin"
        bindir.mkdir(exist_ok=True)
        pgbin = state / "airflow-pg" / "root" / "usr" / "lib" / "postgresql" / "14" / "bin"
        pgbin.mkdir(parents=True, exist_ok=True)
        write_stub(pgbin / "pg_isready", '#!/bin/bash\necho "pg_isready" >> "$ORDER_LOG"\nexit 0\n')
        write_stub(
            pgbin / "pg_dump",
            '#!/bin/bash\necho "pg_dump" >> "$ORDER_LOG"\nout=""\nprev=""\n'
            'for argument in "$@"; do [ "$prev" = "-f" ] && out="$argument"; prev="$argument"; done\n'
            '[ -n "$out" ] && : > "$out"\nexit 0\n',
        )
        write_stub(bindir / "scp", '#!/bin/bash\necho "scp $*" >> "$ORDER_LOG"\nexit 0\n')
        write_stub(
            bindir / "ssh",
            '#!/bin/bash\necho "ssh $*" >> "$ORDER_LOG"\n'
            'case "$*" in\n'
            "  *\"-Verb stage\"*) echo '{\"passed\":true,\"verb\":\"stage\"}' ;;\n"
            "  *\"-Verb promote\"*)\n"
            '    if [ "${FIXTURE_ACK_LOSS:-0}" = "1" ]; then exit 255; fi\n'
            '    if [ -n "${FIXTURE_PROMOTE_OUTPUT:-}" ]; then\n'
            '      cat "$FIXTURE_PROMOTE_OUTPUT"\n'
            '      exit "${FIXTURE_PROMOTE_RC:-0}"\n'
            '    fi\n'
            '    if [ "${FIXTURE_FAIL_PROMOTE:-0}" = "1" ]; then\n'
            "      echo '{\"passed\":false,\"verb\":\"promote\",\"code\":\"probe\",\"message\":\"refused\"}'; exit 0\n"
            '    fi\n'
            "    echo '{\"passed\":true,\"verb\":\"promote\",\"generation\":\"gen-2\","
            "\"previous\":\"C:/description-e2e/state/instance-promotions/previous-0001\"}' ;;\n"
            "  *\"-Verb status\"*)\n"
            '    if [ "${FIXTURE_STATUS:-switched}" = "before" ]; then\n'
            "      echo '{\"passed\":true,\"verb\":\"status\",\"live_task\":"
            "{\"execute\":\"C:/original-install with spaces/runtime/Scripts/description.exe\","
            "\"arguments\":\"serve --config \\\"C:/original-install with spaces/endpoint.json\\\"\","
            "\"config\":\"C:/original-install with spaces/endpoint.json\",\"state\":\"Running\"},"
            "\"pending\":{\"intent\":true,\"verb\":\"promote\","
            "\"generation\":\"C:/description-e2e/state/lifecycle/gen-2/generation\","
            "\"previous\":\"C:/description-e2e/state/instance-promotions/previous-0001\","
            "\"source_launcher\":\"C:/original-install with spaces/runtime/Scripts/description.exe\","
            "\"source_config\":\"C:/original-install with spaces/endpoint.json\"},"
            "\"last_promotion\":null}'\n"
            '    elif [ "${FIXTURE_STATUS:-switched}" = "foreign" ]; then\n'
            "      echo '{\"passed\":true,\"verb\":\"status\",\"live_task\":"
            "{\"execute\":\"C:/foreign/not-description.exe\","
            "\"arguments\":\"foreign-service --option x\","
            "\"config\":\"C:/foreign/foreign.json\",\"state\":\"Running\"},"
            "\"pending\":null,\"last_promotion\":null}'\n"
            "    else\n"
            "      echo '{\"passed\":true,\"verb\":\"status\",\"live_task\":"
            "{\"execute\":\"C:/description-e2e/state/lifecycle/gen-2/generation/runtime/Scripts/description.exe\","
            "\"arguments\":\"serve --config C:/description-e2e/state/lifecycle/gen-2/generation/endpoint.json\","
            "\"config\":\"C:/description-e2e/state/lifecycle/gen-2/generation/endpoint.json\","
            "\"state\":\"Running\"},"
            "\"pending\":{\"intent\":true,\"verb\":\"promote\","
            "\"generation\":\"C:/description-e2e/state/lifecycle/gen-2/generation\","
            "\"previous\":\"C:/description-e2e/state/instance-promotions/previous-0001\"},"
            "\"last_promotion\":null}'\n"
            "    fi ;;\n"
            "  *\"-Verb rollback\"*) echo '{\"passed\":true,\"verb\":\"rollback\","
            "\"restored_from\":\"C:/description-e2e/state/instance-promotions/previous-0001\"}' ;;\n"
            "  *\"-Verb health\"*) cat \"$FIXTURE_HEALTH_FILE\" ;;\n"
            "esac\n"
            "exit 0\n",
        )
        write_stub(
            bindir / "systemctl",
            '#!/bin/bash\necho "systemctl $*" >> "$ORDER_LOG"\n'
            'case "$*" in\n'
            '  "--user enable --now description-portal.service description-operator-proxy.service")\n'
            '    [ "${START_INGRESS_RC:-0}" = "0" ] || exit "${START_INGRESS_RC}" ;;\n'
            "esac\n",
        )
        write_stub(bindir / "sleep", "#!/bin/bash\nexit 0\n")
        venv = Path(derived_env["AIRFLOW_VENV"]) / "bin"
        venv.mkdir(parents=True, exist_ok=True)
        write_stub(
            venv / "airflow",
            '#!/bin/bash\necho "airflow $*" >> "$ORDER_LOG"\n'
            'count=$(cat "$REGISTER_COUNT" 2>/dev/null || echo 0)\n'
            'case "$*" in\n'
            '  "dags unpause solidworks_to_urdf")\n'
            '    [ "$UNPAUSE_RC" = "0" ] || exit "$UNPAUSE_RC"\n'
            '    if [ "$count" -ge "$REGISTER_AFTER" ]; then echo false > "$ADMISSION"; fi ;;\n'
            '  "dags pause solidworks_to_urdf")\n'
            '    [ "$PAUSE_RC" = "0" ] || exit "$PAUSE_RC"\n'
            '    echo true > "$ADMISSION" ;;\n'
            "  *) exit 1 ;;\n"
            "esac\n",
        )
        write_stub(
            venv / "python",
            "#!/bin/bash\n"
            'case "$*" in\n'
            "  *install_record.py*)\n"
            '    exec python3 "$@" ;;\n'
            '  "-m description_pipeline.orchestration.lifecycle_state"*)\n'
            "    python3 -c 'import json,os; from pathlib import Path; "
            'Path(os.environ["LIFECYCLE_ENV_LOG"]).write_text(json.dumps('
            '{"dsn": os.environ.get("PLATFORM_OPERATOR_DSN"), '
            '"expected_dsn": os.environ["EXPECTED_PLATFORM_DSN"]}))\'\n'
            '    [ "${PLATFORM_OPERATOR_DSN:-}" = "$EXPECTED_PLATFORM_DSN" ] || exit 77\n'
            '    case "$*" in\n'
            '      *--require-admission-closed*)\n'
            '        [ "$(cat "$ADMISSION" 2>/dev/null)" = "true" ] || exit 1 ;;\n'
            '    esac\n'
            '    if [ -n "$PROJECTION_FILE" ] && [ -f "$PROJECTION_FILE" ]; then cat "$PROJECTION_FILE"; fi\n'
            '    exit "${PROJECTION_RC:-0}" ;;\n'
            "esac\n"
            'script="$(cat)"\n'
            'case "$script" in\n'
            "  *platform-close-admission*)\n"
            '    echo true > "$ADMISSION"\n'
            '    echo "platform close_admission" >> "$ORDER_LOG"\n'
            '    echo "admission=closed revision=2" ;;\n'
            "  *platform-open-admission*)\n"
            '    echo false > "$ADMISSION"\n'
            '    echo "platform open_admission" >> "$ORDER_LOG"\n'
            '    echo "admission=open revision=3" ;;\n'
            "  *platform-reconcile*)\n"
            '    [ -n "${RECONCILE_RUN_ID:-}" ] && [ -n "${RECONCILE_OUTCOME:-}" ] || exit 61\n'
            '    echo "platform reconcile ${RECONCILE_RUN_ID}:${RECONCILE_STAGE}:${RECONCILE_OUTCOME}"'
            ' >> "$ORDER_LOG"\n'
            '    echo "reconciled=${RECONCILE_RUN_ID}:${RECONCILE_STAGE} outcome=${RECONCILE_OUTCOME}" ;;\n'
            "  *)\n"
            '    echo "airflow admission check" >> "$ORDER_LOG"\n'
            '    count=$(cat "$REGISTER_COUNT" 2>/dev/null || echo 0)\n'
            '    if [ "$count" -lt "$REGISTER_AFTER" ]; then\n'
            '      echo $((count + 1)) > "$REGISTER_COUNT"; exit 1\n'
            "    fi\n"
            '    [ "$(cat "$ADMISSION")" = "false" ] ;;\n'
            "esac\n",
        )
        admission, order = tmp / "admission", tmp / "order.log"
        admission.write_text("true")
        projection = tmp / "projection.json"
        projection.write_text(
            json.dumps({"in_flight": [], "unresolved": [], "unknown": [], "settled": True}), encoding="utf-8"
        )
        health = {
            "schema_version": ir.HEALTH_SCHEMA,
            "pipeline_id": ir.PIPELINE_ID,
            "native_platform": True,
            "ready": True,
            "release": {"version": "1.4.0", "source_commit": "c" * 40, "source_sha256": "a" * 64, "role": "native"},
            "contract": {"schema_version": CONTRACT["schema_version"], "digest": CONTRACT_SHA256},
            "resource_policy": {"schema_version": ir.RESOURCE_POLICY_SCHEMA, "digest": ir.resource_policy_v1_digest()},
            "install": {"config_digest": "d" * 64, "install_id": "ep-1"},
            "endpoint": {"port": 9765},
            "cad": {"external_session_detected": False, "owned_sessions": 0, "lease_held": False},
            "jobs": {"active": 0, "queued": 0, "unresolved": 0},
            "capacity": {"free_bytes": 1, "reserved_bytes": 0},
            "readiness": {"reason_code": "", "native": {"ok": True, "problems": []}},
        }
        health_file = tmp / "windows-health.json"
        health_file.write_text(json.dumps({"passed": True, "verb": "health", "health": health}), encoding="utf-8")
        env = {
            "PATH": f"{bindir}:{os.environ.get('PATH', '/usr/bin:/bin')}",
            "XDG_CONFIG_HOME": str(config_home),
            "ORDER_LOG": str(order),
            "ADMISSION": str(admission),
            "REGISTER_COUNT": str(tmp / "register-count"),
            "REGISTER_AFTER": str(register_after),
            "UNPAUSE_RC": str(unpause_rc),
            "PAUSE_RC": str(pause_rc),
            "PROJECTION_FILE": str(projection),
            "LIFECYCLE_ENV_LOG": str(tmp / "query-environment.json"),
            "EXPECTED_PLATFORM_DSN": derived_env["PLATFORM_OPERATOR_DSN"],
            "FIXTURE_HEALTH_FILE": str(health_file),
        }
        control = self._control_tree(tmp)
        return config_path, env, admission, order, control

    def test_start_opens_the_admission_only_after_the_ingress_is_ready(self) -> None:
        for register_after in (0, 1):
            with self.subTest(register_after=register_after), tempfile.TemporaryDirectory() as tmp:
                config_path, env, admission, order, control = self._admission_fixture(
                    Path(tmp), register_after=register_after
                )
                result = run(["bash", str(control), "start", "--config", str(config_path)], env)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(admission.read_text().strip(), "false")
                lines = order.read_text().splitlines()
                # Cold start: the managed database comes up first, then the actual close.
                self.assertLess(
                    lines.index("systemctl --user start description-postgres.service"),
                    lines.index("platform close_admission"),
                )
                self.assertLess(lines.index("pg_isready"), lines.index("platform close_admission"))
                ingress = lines.index(
                    "systemctl --user enable --now description-portal.service description-operator-proxy.service"
                )
                self.assertLess(max(i for i, line in enumerate(lines) if line == "airflow admission check"), ingress)
                ready = lines.index(
                    "systemctl --user is-active --quiet description-portal.service description-operator-proxy.service"
                )
                self.assertLess(ingress, ready)
                probe = lines.index("health-gate probe")
                self.assertLess(ready, probe, "the Linux health gate runs after the ingress is ready")
                self.assertLess(probe, lines.index("platform open_admission"), "the open waits for a passed gate")
                self.assertEqual(lines.count("airflow dags unpause solidworks_to_urdf"), register_after + 1)
        with tempfile.TemporaryDirectory() as tmp:
            config_path, env, admission, order, control = self._admission_fixture(Path(tmp))
            admission.write_text("false")  # a leftover open gate from an interrupted window
            env["START_INGRESS_RC"] = "7"
            result = run(["bash", str(control), "start", "--config", str(config_path)], env)
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("platform admission stays closed", result.stderr)
            # The DAG scheduling unpause already happened (a recorded fact); the PLATFORM gate
            # is closed through the controlled start precondition and never reopens.
            order_text = order.read_text()
            self.assertIn("platform close_admission", order_text)
            lines = order_text.splitlines()
            self.assertLess(
                lines.index("platform close_admission"),
                lines.index("airflow dags unpause solidworks_to_urdf"),
                "the start window closes the leftover gate before scheduling changes",
            )
            self.assertNotIn("is-active", order_text)
            self.assertNotIn("health-gate probe", order_text)
            self.assertNotIn("platform open_admission", order_text)

    def test_promote_and_rollback_refuse_before_any_side_effect(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path, env, _, order, control = self._admission_fixture(Path(tmp))
            missing_id = run(["bash", str(control), "promote", "--config", str(config_path)], env)
            self.assertEqual(missing_id.returncode, 1)
            self.assertIn("requires --install-id", missing_id.stderr)
            no_receipt = run(
                ["bash", str(control), "promote", "--config", str(config_path), "--install-id", "gen-9"], env
            )
            self.assertEqual(no_receipt.returncode, 1)
            self.assertIn("run stage first", no_receipt.stderr)
            rollback = run(["bash", str(control), "rollback", "--config", str(config_path)], env)
            self.assertEqual(rollback.returncode, 1)
            self.assertIn("no promotion receipt", rollback.stderr)
            lines = order.read_text(encoding="utf-8").splitlines() if order.exists() else []
            self.assertNotIn("platform close_admission", lines, "a refused switch must not touch the admission")
            self.assertFalse([line for line in lines if line.startswith("airflow ") and "dags" in line])

    def test_reconcile_binds_a_registered_operation_and_refuses_unbound_claims(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path, env, _, order, control = self._admission_fixture(Path(tmp))
            refused = run(["bash", str(control), "reconcile", "--config", str(config_path)], env)
            self.assertEqual(refused.returncode, 1)
            self.assertIn("reconcile requires", refused.stderr)
            bad = run(
                [
                    "bash", str(control), "reconcile", "--config", str(config_path),
                    "--run-id", "run-1", "--stage", "capture", "--outcome", "unresolved",
                    "--evidence", "receipt.json",
                ],
                env,
            )
            self.assertEqual(bad.returncode, 1)
            self.assertIn("unresolved is not a result", bad.stderr)
            reconciled = run(
                [
                    "bash", str(control), "reconcile", "--config", str(config_path),
                    "--run-id", "run-1", "--stage", "capture", "--outcome", "completed",
                    "--evidence", "receipt.json",
                ],
                env,
            )
            self.assertEqual(reconciled.returncode, 0, reconciled.stdout + reconciled.stderr)
            self.assertIn("reconciled=run-1:capture outcome=completed", reconciled.stdout)
            self.assertIn(
                "platform reconcile run-1:capture:completed",
                order.read_text(encoding="utf-8").splitlines(),
            )

    def test_unpause_failure_does_not_start_ingress(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path, env, admission, order, control = self._admission_fixture(Path(tmp), unpause_rc=7)
            result = run(["bash", str(control), "start", "--config", str(config_path)], env)
            self.assertEqual(result.returncode, 7, result.stdout + result.stderr)
            self.assertEqual(admission.read_text().strip(), "true")
            self.assertNotIn("description-portal.service", order.read_text())
            self.assertNotIn("description-operator-proxy.service", order.read_text())

    def test_start_keep_admission_closed_starts_the_services_without_opening(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path, env, admission, order, control = self._admission_fixture(Path(tmp))
            result = run(
                ["bash", str(control), "start", "--config", str(config_path), "--keep-admission-closed"], env
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("services are running; the platform admission stays closed", result.stdout)
            self.assertEqual(admission.read_text().strip(), "true")
            lines = order.read_text().splitlines()
            self.assertIn("platform close_admission", lines)
            self.assertNotIn("platform open_admission", lines)
            self.assertNotIn("airflow dags unpause solidworks_to_urdf", lines)
            self.assertIn("systemctl --user enable --now description-airflow-api-server", lines)
            self.assertIn(
                "systemctl --user enable --now description-portal.service description-operator-proxy.service",
                lines,
            )
        # Default behavior is unchanged: without the switch the verified start opens the gate.
        with tempfile.TemporaryDirectory() as tmp:
            config_path, env, admission, order, control = self._admission_fixture(Path(tmp))
            default = run(["bash", str(control), "start", "--config", str(config_path)], env)
            self.assertEqual(default.returncode, 0, default.stdout + default.stderr)
            self.assertEqual(admission.read_text().strip(), "false")
            default_calls = order.read_text()
            self.assertIn("platform open_admission", default_calls)
            self.assertIn("airflow dags unpause solidworks_to_urdf", default_calls)

    def test_start_keep_admission_closed_failure_stays_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path, env, admission, order, control = self._admission_fixture(Path(tmp))
            admission.write_text("false")  # a leftover open gate from an interrupted window
            env["START_INGRESS_RC"] = "7"
            result = run(
                ["bash", str(control), "start", "--config", str(config_path), "--keep-admission-closed"], env
            )
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("platform admission stays closed", result.stderr)
            self.assertEqual(admission.read_text().strip(), "true")
            lines = order.read_text().splitlines()
            self.assertIn("platform close_admission", lines)
            self.assertNotIn("platform open_admission", lines)
            self.assertNotIn("airflow dags unpause solidworks_to_urdf", lines)

    def test_start_health_gate_failure_keeps_the_admission_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path, env, _admission, order, control = self._admission_fixture(Path(tmp))
            # The explicit internal path: the mirror carries the real gate with explicit small
            # bounds, the probe always fails.  The lifecycle itself stays on the fixed policy.
            mirror_gate = (
                HEALTH_GATE.read_text(encoding="utf-8")
                .replace('DEADLINE_S="${2:-180}"', 'DEADLINE_S="${2:-0.5}"')
                .replace('PER_PROBE_S="${3:-60}"', 'PER_PROBE_S="${3:-0.5}"')
                .replace('INTERVAL_S="${4:-5}"', 'INTERVAL_S="${4:-0.1}"')
            )
            write_stub(control.parent / "health_gate.sh", mirror_gate)
            write_stub(
                control.parent / "health.sh",
                '#!/usr/bin/env bash\necho "health-gate probe" >> "$ORDER_LOG"\nexit 5\n',
            )
            result = run(["bash", str(control), "start", "--config", str(config_path)], env)
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("Linux health gate did not pass", result.stderr)
            lines = order.read_text().splitlines()
            self.assertIn("health-gate probe", lines)
            self.assertNotIn("platform open_admission", lines)
            state_dir = Path(env["ORDER_LOG"]).parent / "state"
            summaries = sorted((state_dir / "health-gate").glob("start-*/run-*/summary.txt"))
            self.assertEqual(len(summaries), 1)
            self.assertIn("linux_health_gate=FAIL", summaries[0].read_text(encoding="utf-8"))

    def test_start_keep_admission_closed_does_not_consult_the_health_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path, env, admission, order, control = self._admission_fixture(Path(tmp))
            env["HEALTH_GATE_RC"] = "5"  # would fail the open if it were (wrongly) consulted
            result = run(
                ["bash", str(control), "start", "--config", str(config_path), "--keep-admission-closed"], env
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertNotIn("health-gate probe", order.read_text(encoding="utf-8"))
            self.assertEqual(admission.read_text().strip(), "true")

    def test_promote_verify_gates_the_linux_health_before_its_open(self) -> None:
        control = CONTROL.read_text(encoding="utf-8")
        self.assertEqual(
            control.count('bash "$HERE/health_gate.sh"'),
            2,
            "the one gate serves both the plain start and promote's verify step",
        )
        verify_anchor = control.index('--out "$EVIDENCE_DIR/final-identity.json"')
        gate_in_promote = control.index('bash "$HERE/health_gate.sh" "$EVIDENCE_DIR/linux-health-')
        open_in_promote = control.index('platform_open_admission > "$EVIDENCE_DIR/open-admission.out"')
        self.assertLess(verify_anchor, gate_in_promote, "the promote gate runs inside the verify step")
        self.assertLess(gate_in_promote, open_in_promote, "the promote opens only after the gate")
        start_gate = control.index('bash "$HERE/health_gate.sh" "$OPERATOR_STATE/health-gate/start-')
        self.assertLess(start_gate, control.rindex("platform_open_admission"), "the start opens only after the gate")

    def test_keep_admission_closed_flag_is_refused_by_other_verbs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path, env, admission, order, control = self._admission_fixture(Path(tmp))
            untouched = admission.read_text(encoding="utf-8")
            before = order.read_text(encoding="utf-8") if order.exists() else None
            for action in (
                "validate",
                "install",
                "drain",
                "stop",
                "status",
                "health",
                "reconcile",
                "stage",
                "promote",
                "rollback",
            ):
                with self.subTest(action=action):
                    result = run(
                        ["bash", str(control), action, "--config", str(config_path), "--keep-admission-closed"],
                        env,
                    )
                    self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                    self.assertIn("--keep-admission-closed is only accepted by start", result.stderr)
                    self.assertEqual(admission.read_text(encoding="utf-8"), untouched)
                    after = order.read_text(encoding="utf-8") if order.exists() else None
                    self.assertEqual(before, after, f"{action} performed a side effect before the refusal")
        # The usage probe runs outside the temporary fixture: the real control script.
        usage = run(["bash", str(CONTROL)], {})
        self.assertEqual(usage.returncode, 2)
        self.assertIn("--keep-admission-closed", usage.stderr)

    def _project(self, env: dict, data: dict, rc: int) -> None:
        Path(env["PROJECTION_FILE"]).write_text(json.dumps(data), encoding="utf-8")
        env["PROJECTION_RC"] = str(rc)

    def test_stop_requires_closed_admission_and_settled_store(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path, env, admission, order, control = self._admission_fixture(Path(tmp))
            admission.write_text("false")
            result = run(["bash", str(control), "stop", "--config", str(config_path)], env)
            self.assertEqual(result.returncode, 1)
            self.assertIn("run drain first", result.stderr)
            self.assertFalse(order.exists())
        with tempfile.TemporaryDirectory() as tmp:
            config_path, env, admission, order, control = self._admission_fixture(Path(tmp))
            self._project(
                env,
                {
                    "in_flight": [{"run_id": "run-active", "state": "running"}],
                    "unresolved": [],
                    "unknown": [],
                    "settled": False,
                },
                1,
            )
            result = run(["bash", str(control), "stop", "--config", str(config_path)], env)
            self.assertEqual(result.returncode, 1)
            self.assertIn("stop refused", result.stderr)
            self.assertFalse(order.exists())
        with tempfile.TemporaryDirectory() as tmp:
            config_path, env, admission, order, control = self._admission_fixture(Path(tmp))
            self._project(
                env,
                {
                    "in_flight": [],
                    "unresolved": [{"run_id": "run-unresolved", "state": "unresolved"}],
                    "unknown": [],
                    "settled": False,
                },
                1,
            )
            result = run(["bash", str(control), "stop", "--config", str(config_path)], env)
            self.assertEqual(result.returncode, 1)
            self.assertIn("stop refused", result.stderr)
            self.assertFalse(order.exists())
        with tempfile.TemporaryDirectory() as tmp:
            config_path, env, admission, order, control = self._admission_fixture(Path(tmp))
            result = run(["bash", str(control), "stop", "--config", str(config_path)], env)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            lines = order.read_text().splitlines()
            self.assertEqual(
                lines[0],
                "systemctl --user disable --now description-operator-proxy.service description-portal.service",
            )
            pause = lines.index("airflow dags pause solidworks_to_urdf")
            self.assertLess(pause, lines.index("systemctl --user disable --now description-airflow-scheduler"))
            self.assertEqual(admission.read_text().strip(), "true")

    def test_drain_closes_admission_and_requires_resolved_outcomes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path, env, admission, order, control = self._admission_fixture(Path(tmp))
            admission.write_text("false")
            result = run(["bash", str(control), "drain", "--config", str(config_path)], env)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(admission.read_text().strip(), "true")
            self.assertIn("airflow dags pause solidworks_to_urdf", order.read_text())
            self.assertIn('"settled": true', result.stdout)
        with tempfile.TemporaryDirectory() as tmp:
            config_path, env, admission, order, control = self._admission_fixture(Path(tmp), pause_rc=9)
            result = run(["bash", str(control), "drain", "--config", str(config_path)], env)
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertIn("could not pause the DAG scheduling", result.stderr)
        with tempfile.TemporaryDirectory() as tmp:
            config_path, env, admission, order, control = self._admission_fixture(Path(tmp))
            self._project(
                env,
                {
                    "in_flight": [],
                    "unresolved": [{"run_id": "run-unresolved", "state": "unresolved"}],
                    "unknown": [],
                    "settled": False,
                },
                1,
            )
            result = run(["bash", str(control), "drain", "--config", str(config_path)], env)
            self.assertEqual(result.returncode, 1)
            self.assertIn("reconcile must close them", result.stderr)
            self.assertIn('"unresolved"', result.stdout)
        with tempfile.TemporaryDirectory() as tmp:
            config_path, env, admission, order, control = self._admission_fixture(Path(tmp))
            self._project(
                env,
                {
                    "in_flight": [{"run_id": "run-active", "state": "running"}],
                    "unresolved": [],
                    "unknown": [],
                    "settled": False,
                },
                0,
            )
            result = run(["bash", str(control), "drain", "--config", str(config_path)], env)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn('"run_id": "run-active"', result.stdout)

    def test_gates_query_with_the_installed_platform_dsn(self) -> None:
        for action in ("drain", "stop"):
            with self.subTest(action=action), tempfile.TemporaryDirectory() as tmp:
                config_path, env, _, _, control = self._admission_fixture(Path(tmp))
                result = run(["bash", str(control), action, "--config", str(config_path)], env)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                observed = json.loads(Path(env["LIFECYCLE_ENV_LOG"]).read_text(encoding="utf-8"))
                self.assertEqual(observed["dsn"], observed["expected_dsn"])
                self.assertIn("operator_ro@", observed["dsn"])

    def test_operational_entry_rejects_canonical_validator_divergence(self) -> None:
        def break_previous(record: dict) -> None:
            record["previous"] = {"release": {}, "config": {"path": "/x", "digest": "a" * 64}, "record": "/y"}

        for label, mutate in (
            ("previous.release", break_previous),
            ("impossible timestamp", lambda record: record.__setitem__("timestamp", "2026-99-99T00:00:00Z")),
        ):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as tmp:
                config_path, state, config_home, target = self._render_units(Path(tmp))
                record_path = state / "install-record.json"
                record = json.loads(record_path.read_text(encoding="utf-8"))
                mutate(record)
                record_path.write_text(json.dumps(record), encoding="utf-8")
                before = {path: digest(path) for path in target.glob("*.service")}
                result = run(
                    ["bash", str(CONTROL), "status", "--config", str(config_path)],
                    {"XDG_CONFIG_HOME": str(config_home)},
                )
                self.assertEqual(result.returncode, 1)
                self.assertIn("install record invalid", result.stderr)
                self.assertEqual(before, {path: digest(path) for path in target.glob("*.service")})

    def test_units_render_and_static_health_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, state, config_home, target = self._render_units(Path(tmp))
            (target / "unrelated.service").write_text("[Unit]\nDescription=keep\n", encoding="utf-8")
            token_file = state / "secrets" / "endpoint.token"
            token_file.parent.mkdir(parents=True, exist_ok=True)
            token_file.write_text("test-token\n", encoding="utf-8")
            token_file.chmod(0o600)
            names = sorted(path.name for path in target.glob("*.service"))
            self.assertEqual(
                names,
                [
                    "description-airflow-api-server.service",
                    "description-airflow-dag-processor.service",
                    "description-airflow-scheduler.service",
                    "description-operator-proxy.service",
                    "description-portal.service",
                    "description-postgres.service",
                    "description-solidworks-tunnel.service",
                    "unrelated.service",
                ],
            )
            self.assertEqual((target / "unrelated.service").read_text(encoding="utf-8"), "[Unit]\nDescription=keep\n")
            portal = (target / "description-portal.service").read_text(encoding="utf-8")
            self.assertIn("description_pipeline.orchestration.portal", portal)
            proxy = (target / "description-operator-proxy.service").read_text(encoding="utf-8")
            self.assertIn(" -t -c ", proxy)
            health = run(
                ["bash", str(HEALTH), "--static"], {"OPERATOR_STATE": str(state), "XDG_CONFIG_HOME": str(config_home)}
            )
            self.assertEqual(health.returncode, 0, health.stdout + health.stderr)
            self.assertIn("STATIC OK", health.stdout)

    def test_operatorctl_surface_is_config_only(self) -> None:
        missing_target = run(["bash", str(CONTROL), "install"], {})
        self.assertEqual(missing_target.returncode, 2)
        self.assertIn("usage:", missing_target.stderr)
        with tempfile.TemporaryDirectory() as tmp:
            config_path, _, _ = platform_fixture(Path(tmp))
            removed = run(["bash", str(CONTROL), "rehearse", "--config", str(config_path)], {})
            self.assertEqual(removed.returncode, 2)
            env_entry = run(["bash", str(CONTROL), "health", "--env-file", str(Path(tmp) / "operator.env")], {})
            self.assertEqual(env_entry.returncode, 2)
            self.assertIn("usage:", env_entry.stderr)
            no_config = run(["bash", str(CONTROL), "start"], {})
            self.assertEqual(no_config.returncode, 2)
            other = Path(tmp) / "other.json"
            other.write_text(
                config_path.read_text(encoding="utf-8").replace(str(Path(tmp) / "state"), str(Path(tmp) / "other")),
                encoding="utf-8",
            )
            not_installed = run(["bash", str(CONTROL), "status", "--config", str(other)], {})
            self.assertEqual(not_installed.returncode, 1)
            self.assertIn("not installed", not_installed.stderr)

    def test_status_is_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path, state, config_home, target = self._render_units(Path(tmp))
            tracked = (
                state / "resolved.env",
                state / "portal.json",
                state / "nginx/nginx.conf",
                target / "description-portal.service",
            )
            before = {path: digest(path) for path in tracked}
            result = run(
                ["bash", str(CONTROL), "status", "--config", str(config_path)], {"XDG_CONFIG_HOME": str(config_home)}
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertNotIn("DRIFT", result.stdout)
            self.assertEqual(before, {path: digest(path) for path in before})

    def test_status_reports_a_proxy_unit_that_executes_another_binary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path, state, config_home, target = self._render_units(Path(tmp))
            bound = ""
            for line in (state / "resolved.env").read_text(encoding="utf-8").splitlines():
                key, _, raw = line.partition("=")
                if key == "NGINX_BIN" and raw:
                    bound = decode_env_value(raw)
            self.assertTrue(bound, "the fixture must bind an NGINX_BIN")
            other = Path(tmp) / "other-nginx"
            write_stub(other, "#!/bin/bash\nexit 0\n")
            proxy = target / "description-operator-proxy.service"
            proxy.write_text(proxy.read_text(encoding="utf-8").replace(bound, str(other)), encoding="utf-8")
            result = run(
                ["bash", str(CONTROL), "status", "--config", str(config_path)],
                {"XDG_CONFIG_HOME": str(config_home)},
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn(
                "DRIFT the installed proxy unit does not execute the bound nginx binary", result.stdout
            )
            self.assertTrue(Path(bound).exists(), "the bound binary must not be touched")

    def test_operational_entry_requires_the_complete_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path, state, config_home, target = self._render_units(Path(tmp))
            (state / "install-record.json").write_text(json.dumps({"install_id": "gen-1"}), encoding="utf-8")
            before = {path: digest(path) for path in target.glob("*.service")}
            result = run(
                ["bash", str(CONTROL), "status", "--config", str(config_path)], {"XDG_CONFIG_HOME": str(config_home)}
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("install record", result.stderr)
            self.assertEqual(before, {path: digest(path) for path in target.glob("*.service")})

    def test_operational_entry_rejects_config_change_and_snapshot_tamper(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path, _, config_home, _ = self._render_units(Path(tmp))
            document = json.loads(config_path.read_text(encoding="utf-8"))
            document["targets"]["m3.0"]["base"] = "changed-base"
            config_path.write_text(json.dumps(document), encoding="utf-8")
            result = run(
                ["bash", str(CONTROL), "status", "--config", str(config_path)], {"XDG_CONFIG_HOME": str(config_home)}
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("does not match the active snapshot", result.stderr)
        with tempfile.TemporaryDirectory() as tmp:
            config_path, state, config_home, _ = self._render_units(Path(tmp))
            record = json.loads((state / "install-record.json").read_text(encoding="utf-8"))
            snapshot_path = Path(record["snapshot"]["path"])
            snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
            snapshot["resolved"]["targets"]["m3.0"]["base"] = "tampered-base"
            snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")
            result = run(
                ["bash", str(CONTROL), "status", "--config", str(config_path)], {"XDG_CONFIG_HOME": str(config_home)}
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("snapshot digest mismatch", result.stderr)

    def test_toolchain_pins_are_current(self) -> None:
        toolchain = (OPERATOR / "scripts" / "install_toolchain.sh").read_text(encoding="utf-8")
        self.assertIn("UV_VERSION=0.12.23", toolchain)
        self.assertIn("PYTHON_VERSION=3.12.14", toolchain)
        for knob in ("${UV_VERSION:-", "${PYTHON_VERSION:-"):
            self.assertNotIn(knob, toolchain)

    def test_health_probes_endpoint_auth_and_connection(self) -> None:
        health = HEALTH.read_text(encoding="utf-8")
        self.assertIn("TUNNEL_LOOPBACK_PORT", health)
        self.assertIn("/health", health)
        self.assertIn("Authorization", health)
        self.assertIn("solidworks_windows", health)
        # The two identity probes consume the canonical owners only: IF-09 for the endpoint,
        # IF-11 for the installed connection; no targets/repositories lexemes remain.
        self.assertIn("interface_records.validate_health", health)
        self.assertIn("require_endpoint_matches_install_record", health)
        self.assertIn("validate_connection_config_identity", health)
        self.assertIn("check_config_identity_file", health)
        self.assertIn("verify_config_identity_digest", health)
        self.assertIn("require_active_config_identity", health)
        self.assertNotIn("targets", health)
        self.assertNotIn("repositories", health)
        self.assertIn("/auth/feishu/health", health)
        self.assertIn("FEISHU_ENV_FILE", health)
        self.assertIn("FEISHU_APP_SECRET_FILE", health)
        self.assertIn('if status == 200 and body.get("configured") is True', health)
        self.assertIn("connection.extra_dejson", health)
        self.assertIn("2??|3??", health)
        self.assertIn("Airflow api-server serves its login entry", health)
        self.assertIn("non-JSON response", health)

    def test_unconfigured_feishu_is_not_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            env_file = Path(tmp) / "operator.env"
            env = base_env(state, POSTGRES_ROOT=str(Path(tmp) / "pg"), SOLIDWORKS_SSH_HOST="windows-m3")
            env_file.write_text("".join(f"{key}={value}\n" for key, value in env.items()), encoding="utf-8")
            config_home = Path(tmp) / "config"
            target = config_home / "systemd" / "user"
            target.mkdir(parents=True)
            rendered = run(
                [sys.executable, str(RENDER), "--env-file", str(env_file), "--units-dir", str(target)],
                {"XDG_CONFIG_HOME": str(config_home)},
            )
            self.assertEqual(rendered.returncode, 0, rendered.stderr)
            airflow = run(
                ["bash", str(OPERATOR.parent / "airflow" / "services.sh"), "render"],
                {
                    "XDG_CONFIG_HOME": str(config_home),
                    "AIRFLOW_VENV": env["AIRFLOW_VENV"],
                    "AIRFLOW_HOME": env["AIRFLOW_HOME"],
                    "POSTGRES_ROOT": env["POSTGRES_ROOT"],
                    "POSTGRES_MAJOR": "14",
                    "AIRFLOW_API_PORT": "8791",
                    "POSTGRES_PORT": "5433",
                    "TUNNEL_LOOPBACK_PORT": "18765",
                    "SOLIDWORKS_ENDPOINT_PORT": "8765",
                    "SOLIDWORKS_SSH_HOST": "windows-m3",
                    "FEISHU_ENV_FILE": str(state / "feishu.env"),
                },
            )
            self.assertEqual(airflow.returncode, 0, airflow.stderr)
            health = run(
                ["bash", str(HEALTH), "--static"], {"OPERATOR_STATE": str(state), "XDG_CONFIG_HOME": str(config_home)}
            )
            self.assertNotEqual(health.returncode, 0)
            self.assertIn("preparation state only", health.stdout)
            self.assertIn("STATIC FAILURES", health.stdout)

    def test_operatorctl_guards_required_modules_and_optional_python(self) -> None:
        control = CONTROL.read_text(encoding="utf-8")
        self.assertIn('AIRFLOW_PYTHON="${AIRFLOW_PYTHON:-}"', control)
        self.assertNotIn('AIRFLOW_PYTHON="$AIRFLOW_PYTHON"', control)
        self.assertIn('PIPELINE_WHEEL="${PIPELINE_WHEEL:?', control)
        self.assertIn("description_pipeline.orchestration.feishu_auth", control)
        self.assertIn("description_pipeline.orchestration.portal", control)

    def test_drift_check_reports_missing_and_changed_units(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path, _, config_home, target = self._render_units(Path(tmp))
            control_env = {"XDG_CONFIG_HOME": str(config_home)}
            portal = target / "description-portal.service"
            portal.write_text(portal.read_text(encoding="utf-8") + "# drifted\n", encoding="utf-8")
            changed = run(["bash", str(CONTROL), "status", "--config", str(config_path)], control_env)
            self.assertEqual(changed.returncode, 0, changed.stdout + changed.stderr)
            self.assertIn("DRIFT unit differs: description-portal.service", changed.stdout)
            (target / "description-airflow-scheduler.service").unlink()
            missing = run(["bash", str(CONTROL), "status", "--config", str(config_path)], control_env)
            self.assertIn("DRIFT unit missing: description-airflow-scheduler.service", missing.stdout)
            self.assertIn("DRIFT unit differs: description-portal.service", missing.stdout)

    def test_drift_check_reports_render_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path, state, config_home, _ = self._render_units(Path(tmp))
            env_file = state / "derived" / "gen-1" / "operator.env"
            env_file.write_text(
                env_file.read_text(encoding="utf-8")
                + f"OPERATOR_TLS_CERT={Path(tmp) / 'missing.crt'}\n"
                + f"OPERATOR_TLS_KEY={Path(tmp) / 'missing.key'}\n",
                encoding="utf-8",
            )
            failed = run(
                ["bash", str(CONTROL), "status", "--config", str(config_path)],
                {"XDG_CONFIG_HOME": str(config_home)},
            )
            self.assertIn("DRIFT unit missing from render: description-portal.service", failed.stdout)
            self.assertIn("DRIFT unit missing from render: description-operator-proxy.service", failed.stdout)
            self.assertIn("provided TLS certificate or key is missing", failed.stderr)

    def test_portal_unit_injects_the_derived_environment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, state, _, target = self._render_units(Path(tmp))
            env_path = state / "derived" / "gen-1" / "operator.env"
            unit = (target / "description-portal.service").read_text(encoding="utf-8")
            self.assertIn(f"EnvironmentFile={env_path}", unit)
            # The portal consumes the platform store through the derived DSN (A's U1 slice);
            # the canonical env encoding single-quotes the value (its "&"/"?" are not
            # shell-safe), which a `source` and systemd's EnvironmentFile both decode.
            self.assertIn(
                "PLATFORM_SERVICE_DSN='postgresql://platform_service@/airflow_meta",
                env_path.read_text(encoding="utf-8"),
            )


    def test_airflow_units_load_the_same_derived_environment_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, state, config_home, target = self._render_units(Path(tmp))
            derived = state / "derived" / "gen-1" / "operator.env"
            controlled = None
            for row in derived.read_text(encoding="utf-8").splitlines():
                key, _, value = row.partition("=")
                if key == "PLATFORM_CONFIG_DIGEST":
                    controlled = decode_env_value(value.strip())
            self.assertIsNotNone(controlled, "the derived environment carries the controlled digest")
            self.assertRegex(controlled, r"^[0-9a-f]{64}$")
            for unit in (
                "description-airflow-dag-processor",
                "description-airflow-scheduler",
                "description-airflow-api-server",
            ):
                content = (target / f"{unit}.service").read_text(encoding="utf-8")
                self.assertIn(f"EnvironmentFile={derived}", content)
            portal = (target / "description-portal.service").read_text(encoding="utf-8")
            self.assertIn(f"EnvironmentFile={derived}", portal)
            unit = target / "description-airflow-dag-processor.service"
            # Falsifiable negative control: the same renderer without the derived environment
            # reproduces the incident - the task process inherits no digest at all, the exact
            # condition freeze refuses with "PLATFORM_CONFIG_DIGEST is unavailable".
            self._render_airflow_units(state, config_home, derived_env=False)
            self.assertEqual(self._digest_reaching_task(unit), "")
            # With the derived environment wired in, the same task process obtains it.
            self._render_airflow_units(state, config_home, derived_env=True)
            self.assertEqual(self._digest_reaching_task(unit), controlled)

    def test_airflow_render_binds_this_calls_env_file_not_an_ambient_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, state, _, _ = self._render_units(Path(tmp))
            derived = state / "derived" / "gen-1" / "operator.env"
            decoy = Path(tmp) / "ambient-decoy.env"
            decoy.write_text("OPERATOR_STATE=/ambient\n", encoding="utf-8")
            airflow_home = Path(tmp) / "airflow-home"
            stub = airflow_home / "services.sh"
            stub.parent.mkdir()
            stub.write_text('#!/usr/bin/env bash\nprintf \'%s\\n\' "$OPERATOR_DERIVED_ENV"\n', encoding="utf-8")

            def call(env_file: str) -> subprocess.CompletedProcess:
                return subprocess.run(
                    [
                        "bash",
                        "-c",
                        "set -euo pipefail\n"
                        'die() { echo "operatorctl: $*" >&2; exit 1; }\n'
                        "eval \"$(sed -n '/^airflow_render()/,/^}$/p' \"$CONTROL_PATH\")\"\n"
                        "airflow_render\n",
                    ],
                    capture_output=True,
                    text=True,
                    env={
                        "PATH": os.environ.get("PATH", ""),
                        "CONTROL_PATH": str(CONTROL),
                        "AIRFLOW_HERE": str(airflow_home),
                        "ENV_FILE": env_file,
                        "OPERATOR_DERIVED_ENV": str(decoy),
                        "AIRFLOW_VENV": "/nonexistent/venv",
                        "AIRFLOW_HOME": "/nonexistent/home",
                        "POSTGRES_ROOT": "/nonexistent/pg",
                        "POSTGRES_MAJOR": "14",
                        "SOLIDWORKS_SSH_HOST": "windows-m3",
                        "AIRFLOW_API_PORT": "18439",
                        "POSTGRES_PORT": "5433",
                        "TUNNEL_LOOPBACK_PORT": "18788",
                        "SOLIDWORKS_ENDPOINT_PORT": "8743",
                        "FEISHU_ENV_FILE": "/nonexistent/feishu.env",
                    },
                )

            bound = call(str(derived))
            self.assertEqual(bound.returncode, 0, bound.stderr)
            self.assertEqual(
                bound.stdout.strip(),
                str(derived),
                "the formal caller binds the current ENV_FILE, never the ambient duplicate",
            )
            # An empty ENV_FILE refuses loudly instead of rendering unwired units.
            refused = call("")
            self.assertNotEqual(refused.returncode, 0)
            self.assertIn("ENV_FILE is not set", refused.stderr)

    @staticmethod
    def _digest_reaching_task(unit: Path) -> str:
        """One fresh task process through the unit's own environment; nothing is exported here.

        systemd builds a task's environment from a minimal inherited environment plus the
        unit's ``Environment=`` lines and ``EnvironmentFile=`` files; this assembles exactly
        that and proves which controlled values actually reach the process.
        """
        environment = {"PATH": os.environ.get("PATH", ""), "HOME": str(Path.home())}
        for line in unit.read_text(encoding="utf-8").splitlines():
            if line.startswith("EnvironmentFile="):
                path = Path(line.split("=", 1)[1].lstrip("-"))
                if path.is_file():
                    for row in path.read_text(encoding="utf-8").splitlines():
                        key, _, value = row.partition("=")
                        if key and not key.startswith("#"):
                            environment[key] = decode_env_value(value.strip())
            elif line.startswith("Environment="):
                key, _, value = line.split("=", 1)[1].partition("=")
                if key:
                    environment[key] = value
        probe = subprocess.run(
            [sys.executable, "-c", "import os;print(os.environ.get('PLATFORM_CONFIG_DIGEST', ''))"],
            capture_output=True,
            text=True,
            env=environment,
        )
        if probe.returncode != 0:
            raise AssertionError(probe.stderr)
        return probe.stdout.strip()

    def test_api_server_unit_loads_the_optional_feishu_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, state, _, target = self._render_units(Path(tmp))
            derived = state / "derived" / "gen-1" / "operator.env"
            unit = (target / "description-airflow-api-server.service").read_text(encoding="utf-8")
            self.assertIn(f"EnvironmentFile={derived}", unit)
            self.assertIn(f"EnvironmentFile=-{state}/feishu.env", unit)
            self.assertNotIn("@", unit)
            scheduler = (target / "description-airflow-scheduler.service").read_text(encoding="utf-8")
            # The runtime units load exactly the one controlled derived environment; the
            # optional Feishu sign-in state stays an api-server-only concern.
            self.assertEqual(scheduler.count("EnvironmentFile="), 1)
            self.assertIn(f"EnvironmentFile={derived}", scheduler)
            self.assertNotIn("feishu.env", scheduler)
            control = CONTROL.read_text(encoding="utf-8")
            self.assertIn('FEISHU_ENV_FILE="${FEISHU_ENV_FILE:-$AIRFLOW_HOME/feishu.env}"', control)

    def test_scripts_are_syntactically_valid(self) -> None:
        for script in (
            CONTROL,
            HEALTH,
            HEALTH_GATE,
            OPERATOR / "scripts" / "install_toolchain.sh",
            OPERATOR / "scripts" / "install_proxy.sh",
            OPERATOR / "scripts" / "ensure_proxy_binary.sh",
            OPERATOR / "scripts" / "check_proxy_binary.sh",
        ):
            result = run(["bash", "-n", str(script)], {})
            self.assertEqual(result.returncode, 0, f"{script}: {result.stderr}")
        compile(RENDER.read_text(encoding="utf-8"), str(RENDER), "exec")


class InstallOrderingTests(unittest.TestCase):
    """Install must start the managed postgres unit and wait for readiness before migration."""

    def _prepare(
        self,
        tmp: Path,
        *,
        fresh: bool,
        ready_after: int,
        db_exists: bool = False,
        psql_query_rc: int = 0,
        createdb_rc: int = 0,
        start_rc: int = 0,
    ) -> tuple[Path, Path, dict[str, str], Path]:
        release = tmp / "release" / "deploy"
        (release / "operator" / "scripts").mkdir(parents=True)
        (release / "airflow" / "scripts").mkdir(parents=True)
        (release / "airflow" / "dags").mkdir()
        (release / "operator" / "operatorctl.sh").write_bytes(CONTROL.read_bytes())
        (release / "operator" / "health.sh").write_bytes(HEALTH.read_bytes())
        (release / "operator" / "health_gate.sh").write_bytes(HEALTH_GATE.read_bytes())
        (release / "operator" / "render_operator.py").write_bytes(RENDER.read_bytes())
        for module in ("platform_config.py", "platform_env.py", "install_record.py", "promotion.py", "backup.py"):
            (release / "operator" / module).write_bytes((OPERATOR / module).read_bytes())
        (release / "airflow" / "dags" / "solidworks_to_urdf.py").write_text("# dag fixture\n", encoding="utf-8")
        for template in ("nginx.conf.template", "portal.json.template"):
            (release / "operator" / template).write_bytes((OPERATOR / template).read_bytes())
        (release / "operator" / "systemd").mkdir()
        for unit in (OPERATOR / "systemd").glob("*.service"):
            (release / "operator" / "systemd" / unit.name).write_bytes(unit.read_bytes())
        (release / "platform").mkdir()
        for artifact in ("apply.sh", "bootstrap.sql", "schema.sql", "views.sql", "roles.sql"):
            (release / "platform" / artifact).write_bytes((OPERATOR.parent / "platform" / artifact).read_bytes())
        write_stub(
            release / "operator" / "scripts" / "install_toolchain.sh",
            '#!/usr/bin/env bash\necho "AIRFLOW_PYTHON=/usr/bin/python3"\n',
        )
        write_stub(
            release / "operator" / "scripts" / "install_proxy.sh",
            "#!/usr/bin/env bash\n"
            'BIN="$OPERATOR_STATE/toolchain/nginx/root/usr/sbin/nginx"\n'
            'mkdir -p "$(dirname "$BIN")"\n'
            'printf "#!/usr/bin/env bash\\nexit 0\\n" > "$BIN"\n'
            'chmod +x "$BIN"\n'
            'echo "$BIN"\n',
        )
        (release / "operator" / "scripts" / "ensure_proxy_binary.sh").write_bytes(
            (OPERATOR / "scripts" / "ensure_proxy_binary.sh").read_bytes()
        )
        (release / "operator" / "scripts" / "check_proxy_binary.sh").write_bytes(
            (OPERATOR / "scripts" / "check_proxy_binary.sh").read_bytes()
        )
        write_stub(
            release / "airflow" / "services.sh",
            '#!/usr/bin/env bash\necho "services_$1" >> "$ORDER_LOG"\n'
            'case "$1" in\n  stop|stop-executors) exit "${FIXTURE_FAIL_STOP:-0}" ;;\n'
            '  start) exit "${FIXTURE_FAIL_START:-0}" ;;\nesac\n'
            "exit 0\n",
        )
        write_stub(
            release / "airflow" / "install.sh",
            '#!/usr/bin/env bash\necho "airflow_install" >> "$ORDER_LOG"\n'
            'if [ "${1:-}" = "--stage" ]; then\n'
            '  mkdir -p "$AIRFLOW_VENV/bin"\n'
            '  cp "$FIXTURE_STUB_PYTHON" "$AIRFLOW_VENV/bin/python"\n'
            '  chmod +x "$AIRFLOW_VENV/bin/python"\n'
            "  printf '#!/usr/bin/env bash\\nexit 0\\n' > \"$AIRFLOW_VENV/bin/airflow\"\n"
            '  chmod +x "$AIRFLOW_VENV/bin/airflow"\n'
            "fi\nexit 0\n",
        )
        write_stub(
            release / "airflow" / "scripts" / "install_postgres.sh",
            '#!/usr/bin/env bash\necho "install_postgres" >> "$ORDER_LOG"\n'
            'mkdir -p "$POSTGRES_ROOT/data"\n: > "$POSTGRES_ROOT/data/PG_VERSION"\nexit 0\n',
        )
        (release / "airflow" / "scripts" / "add_connection.py").write_text("# stub\n", encoding="utf-8")

        (tmp / "releases" / "airflow-venv-gen-1" / "bin").mkdir(parents=True)
        write_stub(
            tmp / "releases" / "airflow-venv-gen-1" / "bin" / "python",
            "#!/usr/bin/env bash\n"
            "case \"$*\" in\n"
            "  *install_record.py*)\n"
            '    exec python3 "$@" ;;\n'
            '  "-m description_pipeline.orchestration.lifecycle_state"*)\n'
            "    python3 -c 'import json,os; from pathlib import Path; "
            'Path(os.environ["LIFECYCLE_ENV_LOG"]).write_text(json.dumps('
            '{"dsn": os.environ.get("PLATFORM_OPERATOR_DSN"), '
            '"expected_dsn": os.environ["EXPECTED_PLATFORM_DSN"]}))\'\n'
            '    [ "${PLATFORM_OPERATOR_DSN:-}" = "$EXPECTED_PLATFORM_DSN" ] || exit 77\n'
            '    case "$*" in\n'
            "      *--require-admission-closed*)\n"
            '        [ "$(cat "$ADMISSION" 2>/dev/null)" = "true" ] || exit 1 ;;\n'
            "    esac\n"
            '    if [ -n "$PROJECTION_FILE" ] && [ -f "$PROJECTION_FILE" ]; then cat "$PROJECTION_FILE"; fi\n'
            '    exit "${PROJECTION_RC:-0}" ;;\n'
            "esac\n"
            'script="$(cat)"\n'
            'case "$script" in\n'
            "  *platform-close-admission*)\n"
            '    echo true > "$ADMISSION"\n'
            '    echo "platform close_admission" >> "$ORDER_LOG"\n'
            '    echo "admission=closed revision=2" ;;\n'
            "  *platform-open-admission*)\n"
            '    echo false > "$ADMISSION"\n'
            '    echo "platform open_admission" >> "$ORDER_LOG"\n'
            '    echo "admission=open revision=3" ;;\n'
            "  *platform-reconcile*)\n"
            '    [ -n "${RECONCILE_RUN_ID:-}" ] && [ -n "${RECONCILE_OUTCOME:-}" ] || exit 61\n'
            '    echo "platform reconcile ${RECONCILE_RUN_ID}:${RECONCILE_STAGE}:${RECONCILE_OUTCOME}"'
            ' >> "$ORDER_LOG"\n'
            '    echo "reconciled=${RECONCILE_RUN_ID}:${RECONCILE_STAGE} outcome=${RECONCILE_OUTCOME}" ;;\n'
            "esac\n"
            "exit 0\n",
        )
        write_stub(
            tmp / "releases" / "airflow-venv-gen-1" / "bin" / "airflow",
            '#!/usr/bin/env bash\necho "airflow $*" >> "$ORDER_LOG"\n'
            'case "$*" in\n'
            '  "db migrate")\n'
            '    if [ "$(cat "$SYSTEMCTL_STATE_DIR/description-postgres" 2>/dev/null)" != "active" ]; then\n'
            '      echo "psycopg2.OperationalError: connection to server on socket '
            '$POSTGRES_ROOT/socket/.s.PGSQL.$POSTGRES_PORT failed: No such file or directory" >&2\n'
            '      exit 1\n'
            '    fi ;;\n'
            'esac\n'
            'exit 0\n',
        )
        (tmp / "admission").write_text("true")
        (tmp / "projection.json").write_text(
            json.dumps({"in_flight": [], "unresolved": [], "unknown": [], "settled": True}), encoding="utf-8"
        )
        (tmp / "token").write_text("test-token\n", encoding="utf-8")
        (tmp / "token").chmod(0o600)
        secrets = tmp / "secrets"
        secrets.mkdir()
        for name in ("tls.crt", "tls.key", "feishu.json", "postgres.dsn"):
            path = secrets / name
            path.write_text("{}" if name == "feishu.json" else "x", encoding="utf-8")
            path.chmod(0o600)
        if not fresh:
            (tmp / "state" / "airflow-pg" / "data").mkdir(parents=True)
            (tmp / "state" / "airflow-pg" / "data" / "PG_VERSION").write_text("14\n", encoding="utf-8")
        tree = tmp / "release"
        wheel = tree / "mimicverse_description-1.4.0-py3-none-any.whl"
        write_contract_wheel(wheel)
        config = {
            "schema_version": schemas.PLATFORM_CONFIG_SCHEMA,
            "operator": {
                "origin": "https://rehearsal.local:18443",
                "certificate_file": str(tmp / "secrets" / "tls.crt"),
                "private_key_file": str(tmp / "secrets" / "tls.key"),
            },
            "storage": {
                "state_root": str(tmp / "state"),
                "handoff_root": str(tmp / "handoffs"),
                "artifact_root": str(tmp / "artifacts"),
                "workspace_root": str(tmp / "workspaces"),
                "release_root": str(tmp / "releases"),
            },
            "database": {"dsn_file": str(tmp / "secrets" / "postgres.dsn")},
            "windows": {
                "ssh_host": "windows-m3",
                "endpoint_port": 9765,
                "token_file": str(tmp / "token"),
                "instance": {
                    "task_name": "description-e2e-endpoint",
                    "package_root": "C:/description-e2e/packages",
                    "output_root": "C:/description-e2e/results",
                    "state_root": "C:/description-e2e/state",
                },
            },
            "auth": {
                "feishu_secret_file": str(tmp / "secrets" / "feishu.json"),
                "tenant_keys": ["tenant-key"],
                "admin_open_ids": [],
            },
            "targets": {"m3.0": {"repository_slug": "owner/model-repository", "base": "main"}},
            "specifications": {"record_roots": [str(tmp / "specifications")]},
        }
        (tree / "platform.json").write_text(json.dumps(config), encoding="utf-8")
        windows_archive = tree / "description-1.4.0-windows-cp312-x86_64.zip"
        windows_archive.write_bytes(b"windows-archive")
        (tree / "description-1.4.0-linux-cp312-x86_64.zip").write_bytes(b"linux-archive")
        (tree / "description-1.4.0-deployment.zip").write_bytes(b"deployment-archive")
        (tree / "release.json").write_text(
            json.dumps(
                {
                    "schema_version": "solidworks-to-urdf.release/v1",
                    "pipeline_id": "solidworks-to-urdf",
                    "version": "1.4.0",
                    "source_commit": "c" * 40,
                    "source_sha256": "a" * 64,
                    "artifacts": {
                        wheel.name: hashlib.sha256(wheel.read_bytes()).hexdigest(),
                        windows_archive.name: hashlib.sha256(windows_archive.read_bytes()).hexdigest(),
                        "description-1.4.0-linux-cp312-x86_64.zip": hashlib.sha256(b"linux-archive").hexdigest(),
                        "description-1.4.0-deployment.zip": hashlib.sha256(b"deployment-archive").hexdigest(),
                    },
                }
            ),
            encoding="utf-8",
        )
        (tree / "endpoint-identity.json").write_text(
            json.dumps(
                {
                    "install_id": "ep-7",
                    "config_digest": "d" * 64,
                    "release": {
                        "version": "1.4.0",
                        "source_commit": "c" * 40,
                        "source_sha256": "a" * 64,
                        "role": "native",
                    },
                }
            ),
            encoding="utf-8",
        )

        bindir = tmp / "bin"
        bindir.mkdir()
        write_stub(
            bindir / "systemctl",
            '#!/usr/bin/env bash\n'
            'echo "systemctl $*" >> "$ORDER_LOG"\n'
            '# Stateful unit store: a stopped unit is really stopped (is-active and the database\n'
            '# probes read this state), so a full stop genuinely removes database availability.\n'
            'mkdir -p "$SYSTEMCTL_STATE_DIR"\n'
            'if [ "${1:-} ${2:-}" = "--user daemon-reload" ]; then exit 0; fi\n'
            'op=""; quiet=0; units=()\n'
            'for argument in "$@"; do\n'
            '  case "$argument" in\n'
            '    --quiet) quiet=1 ;;\n'
            '    start|stop|enable|disable|is-active) op="$argument" ;;\n'
            '    -*) ;;\n'
            '    *) units+=("${argument%.service}") ;;\n'
            '  esac\n'
            'done\n'
            'case "$op" in\n'
            '  start|enable)\n'
            '    for unit in "${units[@]}"; do\n'
            '      if [ "$unit" = "description-postgres" ] && [ "${START_RC:-0}" != "0" ]; then exit "$START_RC"; fi\n'
            '      if [ -n "${FIXTURE_START_REFUSES:-}" ] && [ "$unit" = "$FIXTURE_START_REFUSES" ]; then exit 1; fi\n'
            '      printf active > "$SYSTEMCTL_STATE_DIR/$unit"\n'
            '    done ;;\n'
            '  stop|disable)\n'
            '    for unit in "${units[@]}"; do\n'
            '      if [ -n "${FIXTURE_STOP_REFUSES:-}" ] && [ "$unit" = "$FIXTURE_STOP_REFUSES" ]; then exit 1; fi\n'
            '      printf inactive > "$SYSTEMCTL_STATE_DIR/$unit"\n'
            '    done ;;\n'
            '  is-active)\n'
            '    status=0\n'
            '    for unit in "${units[@]}"; do\n'
            '      state="$(cat "$SYSTEMCTL_STATE_DIR/$unit" 2>/dev/null || echo inactive)"\n'
            '      [ "$quiet" = "1" ] || echo "$state"\n'
            '      [ "$state" = "active" ] || status=3\n'
            '    done\n'
            '    exit "$status" ;;\n'
            '  *) echo "unexpected systemctl call: $*" >&2; exit 1 ;;\n'
            "esac\n",
        )
        pgbin = tmp / "state" / "airflow-pg" / "root" / "usr" / "lib" / "postgresql" / "14" / "bin"
        pgbin.mkdir(parents=True)
        client_libraries = (
            '[ "${LD_LIBRARY_PATH%%:*}" = "$PG_CLIENT_LIB" ] || '
            '{ echo "missing private PostgreSQL client libraries" >&2; exit 90; }\n'
        )
        write_stub(
            pgbin / "pg_isready",
            "#!/usr/bin/env bash\n" + client_libraries +
            '# The database is only ready while its unit state says so: a stopped server is\n'
            '# unreachable, exactly like a missing socket.\n'
            'if [ "$(cat "$SYSTEMCTL_STATE_DIR/description-postgres" 2>/dev/null)" != "active" ]; then\n'
            '  echo "no response" >&2; exit 2\n'
            "fi\n" +
            'n=$(cat "$PG_READY_COUNT" 2>/dev/null || echo 0)\n'
            "n=$((n + 1))\n"
            'echo "$n" > "$PG_READY_COUNT"\n'
            'echo "pg_isready $n" >> "$ORDER_LOG"\n'
            '[ "${PG_READY_AFTER:-1}" = "0" ] && exit 1\n'
            '[ "$n" -ge "${PG_READY_AFTER:-1}" ]\n',
        )
        write_stub(
            pgbin / "psql",
            "#!/usr/bin/env bash\n"
            + client_libraries
            + 'case "$*" in\n'
            '  *-f*platform/bootstrap.sql*)\n'
            '    echo "psql_platform_bootstrap" >> "$ORDER_LOG"; exit 0 ;;\n'
            '  *-f*platform/grants-airflow.sql*)\n'
            '    echo "psql_airflow_grants" >> "$ORDER_LOG"; exit 0 ;;\n'
            '  *-f*platform/schema.sql*|*-f*platform/views.sql*|*-f*platform/roles.sql*)\n'
            '    echo "psql_platform_apply" >> "$ORDER_LOG"; exit 0 ;;\n'
            "esac\n"
            'echo "psql_db_query" >> "$ORDER_LOG"\n'
            'rc="${PSQL_QUERY_RC:-0}"\n'
            '[ "$rc" != "0" ] && exit "$rc"\n'
            'echo "${DB_EXISTS:-}"\n'
            "exit 0\n",
        )
        write_stub(
            pgbin / "createdb",
            "#!/usr/bin/env bash\n" + client_libraries + 'echo "createdb" >> "$ORDER_LOG"\nexit "${CREATEDB_RC:-0}"\n',
        )
        write_stub(
            pgbin / "pg_dump",
            "#!/usr/bin/env bash\n" + client_libraries + 'echo "pg_dump" >> "$ORDER_LOG"\n'
            'out=""\nprev=""\nfor argument in "$@"; do [ "$prev" = "-f" ] && out="$argument"; prev="$argument"; done\n'
            "[ -n \"$out\" ] && printf 'PGDUMP-DOUBLE\\n' > \"$out\"\nexit 0\n",
        )
        write_stub(bindir / "sleep", '#!/usr/bin/env bash\necho "sleep" >> "$ORDER_LOG"\nexit 0\n')
        write_stub(bindir / "scp", '#!/usr/bin/env bash\necho "scp $*" >> "$ORDER_LOG"\nexit 0\n')
        write_stub(
            bindir / "ssh",
            '#!/usr/bin/env bash\necho "ssh $*" >> "$ORDER_LOG"\n'
            'case "$*" in\n'
            "  *\"-Verb stage\"*) echo '{\"passed\":true,\"verb\":\"stage\"}' ;;\n"
            "  *\"-Verb promote\"*)\n"
            '    if [ "${FIXTURE_ACK_LOSS:-0}" = "1" ]; then exit 255; fi\n'
            '    if [ -n "${FIXTURE_PROMOTE_OUTPUT:-}" ]; then\n'
            '      cat "$FIXTURE_PROMOTE_OUTPUT"\n'
            '      exit "${FIXTURE_PROMOTE_RC:-0}"\n'
            '    fi\n'
            '    if [ "${FIXTURE_FAIL_PROMOTE:-0}" = "1" ]; then\n'
            "      echo '{\"passed\":false,\"verb\":\"promote\",\"code\":\"probe\",\"message\":\"refused\"}'; exit 0\n"
            '    fi\n'
            "    echo '{\"passed\":true,\"verb\":\"promote\",\"generation\":\"gen-2\","
            "\"previous\":\"C:/description-e2e/state/instance-promotions/previous-0001\"}' ;;\n"
            "  *\"-Verb rollback\"*) echo '{\"passed\":true,\"verb\":\"rollback\","
            "\"restored_from\":\"C:/description-e2e/state/instance-promotions/previous-0001\"}' ;;\n"
            "  *\"-Verb health\"*) cat \"$FIXTURE_HEALTH_FILE\" ;;\n"
            "  *\"-Verb status\"*)\n"
            '    if [ "${FIXTURE_STATUS:-switched}" = "before" ]; then\n'
            "      echo '{\"passed\":true,\"verb\":\"status\",\"live_task\":"
            "{\"execute\":\"C:/original-install with spaces/runtime/Scripts/description.exe\","
            "\"arguments\":\"serve --config \\\"C:/original-install with spaces/endpoint.json\\\"\","
            "\"config\":\"C:/original-install with spaces/endpoint.json\",\"state\":\"Running\"},"
            "\"pending\":{\"intent\":true,\"verb\":\"promote\","
            "\"generation\":\"C:/description-e2e/state/lifecycle/gen-2/generation\","
            "\"previous\":\"C:/description-e2e/state/instance-promotions/previous-0001\","
            "\"source_launcher\":\"C:/original-install with spaces/runtime/Scripts/description.exe\","
            "\"source_config\":\"C:/original-install with spaces/endpoint.json\"},"
            "\"last_promotion\":null}'\n"
            '    elif [ "${FIXTURE_STATUS:-switched}" = "foreign" ]; then\n'
            "      echo '{\"passed\":true,\"verb\":\"status\",\"live_task\":"
            "{\"execute\":\"C:/foreign/not-description.exe\","
            "\"arguments\":\"foreign-service --option x\","
            "\"config\":\"C:/foreign/foreign.json\",\"state\":\"Running\"},"
            "\"pending\":null,\"last_promotion\":null}'\n"
            "    else\n"
            "      echo '{\"passed\":true,\"verb\":\"status\",\"live_task\":"
            "{\"execute\":\"C:/description-e2e/state/lifecycle/gen-2/generation/runtime/Scripts/description.exe\","
            "\"arguments\":\"serve --config C:/description-e2e/state/lifecycle/gen-2/generation/endpoint.json\","
            "\"config\":\"C:/description-e2e/state/lifecycle/gen-2/generation/endpoint.json\","
            "\"state\":\"Running\"},"
            "\"pending\":{\"intent\":true,\"verb\":\"promote\","
            "\"generation\":\"C:/description-e2e/state/lifecycle/gen-2/generation\","
            "\"previous\":\"C:/description-e2e/state/instance-promotions/previous-0001\"},"
            "\"last_promotion\":null}'\n"
            "    fi ;;\n"
            "esac\n"
            "exit 0\n",
        )

        order = tmp / "order.log"
        control_env = {
            "PATH": f"{bindir}:{os.environ.get('PATH', '/usr/bin:/bin')}",
            "XDG_CONFIG_HOME": str(tmp / "config"),
            "ORDER_LOG": str(order),
            "SYSTEMCTL_STATE_DIR": str(tmp / "unit-state"),
            "PG_READY_COUNT": str(tmp / "ready-count"),
            "PG_READY_AFTER": str(ready_after),
            "DB_EXISTS": "1" if db_exists else "",
            "PSQL_QUERY_RC": str(psql_query_rc),
            "CREATEDB_RC": str(createdb_rc),
            "START_RC": str(start_rc),
            "PG_CLIENT_LIB": str(tmp / "state" / "airflow-pg" / "root" / "usr" / "lib" / "x86_64-linux-gnu"),
            "LD_LIBRARY_PATH": "/unrelated/vendor",
            "ADMISSION": str(tmp / "admission"),
            "REGISTER_COUNT": str(tmp / "register-count"),
            "REGISTER_AFTER": "0",
            "UNPAUSE_RC": "0",
            "PAUSE_RC": "0",
            "FIXTURE_STUB_PYTHON": str(tmp / "releases" / "airflow-venv-gen-1" / "bin" / "python"),
            "PROJECTION_FILE": str(tmp / "projection.json"),
            "LIFECYCLE_ENV_LOG": str(tmp / "query-environment.json"),
        }
        return release, control_env, order

    def _install(self, release: Path, control_env: dict[str, str]):
        tree = release.parent
        return run(
            [
                "bash",
                str(release / "operator" / "operatorctl.sh"),
                "install",
                "--config",
                str(tree / "platform.json"),
                "--install-id",
                "gen-1",
                "--endpoint-identity",
                str(tree / "endpoint-identity.json"),
            ],
            control_env,
        )

    def test_fresh_install_creates_then_uses_the_managed_unit_before_migration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            release, control_env, order = self._prepare(Path(tmp), fresh=True, ready_after=2)
            result = self._install(release, control_env)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            lines = order.read_text(encoding="utf-8").splitlines()
            self.assertLess(
                lines.index("install_postgres"), lines.index("systemctl --user start description-postgres.service")
            )
            self.assertLess(
                lines.index("systemctl --user start description-postgres.service"), lines.index("pg_isready 1")
            )
            self.assertLess(lines.index("pg_isready 2"), lines.index("airflow_install"))
            self.assertLess(lines.index("pg_isready 2"), lines.index("psql_db_query"))
            self.assertLess(lines.index("psql_db_query"), lines.index("createdb"))
            self.assertLess(lines.index("createdb"), lines.index("psql_platform_bootstrap"))
            self.assertLess(lines.index("psql_platform_bootstrap"), lines.index("airflow_install"))
            self.assertLess(lines.index("createdb"), lines.index("airflow_install"))
            self.assertLess(lines.index("airflow_install"), lines.index("psql_airflow_grants"))
            self.assertLess(lines.index("psql_airflow_grants"), lines.index("psql_platform_apply"))
            self.assertLess(lines.index("airflow_install"), lines.index("psql_platform_apply"))
            self.assertEqual(
                [line for line in lines if line.startswith("pg_isready ")], ["pg_isready 1", "pg_isready 2"]
            )
            systemctl_lines = [line for line in lines if line.startswith("systemctl ")]
            self.assertIn("systemctl --user start description-postgres.service", systemctl_lines)
            self.assertFalse(
                [
                    line
                    for line in systemctl_lines
                    if "enable" in line or ("description-postgres" not in line and "daemon-reload" not in line)
                ],
                systemctl_lines,
            )
            record = json.loads((Path(tmp) / "state" / "install-record.json").read_text(encoding="utf-8"))
            self.assertEqual(record["install_id"], "gen-1")
            self.assertEqual(
                record["services"],
                [
                    "description-postgres",
                    "description-solidworks-tunnel",
                    "description-airflow-dag-processor",
                    "description-airflow-scheduler",
                    "description-airflow-api-server",
                    "description-portal",
                    "description-operator-proxy",
                ],
            )

    def test_stopped_reinstall_starts_the_managed_unit_before_migration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            release, control_env, order = self._prepare(Path(tmp), fresh=False, ready_after=1, db_exists=True)
            result = self._install(release, control_env)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            lines = order.read_text(encoding="utf-8").splitlines()
            self.assertNotIn("install_postgres", lines)
            self.assertNotIn("createdb", lines)
            self.assertLess(
                lines.index("systemctl --user start description-postgres.service"), lines.index("pg_isready 1")
            )
            self.assertLess(lines.index("psql_db_query"), lines.index("airflow_install"))

    def test_status_and_health_diagnose_a_missing_binary_without_reinstalling_it(self) -> None:
        """The read verbs only report drift: no installer run, no repair, no write at all."""

        with tempfile.TemporaryDirectory() as tmp:
            outer = Path(tmp)
            work = outer / "work"
            work.mkdir()
            scratch = outer / "scratch"
            (scratch / "tmp").mkdir(parents=True)
            release, control_env, _ = self._prepare(work, fresh=False, ready_after=1, db_exists=True)
            # Probe instrumentation stays outside the snapshotted deployment tree.
            order = scratch / "order.log"
            control_env["ORDER_LOG"] = str(order)
            control_env["TMPDIR"] = str(scratch / "tmp")
            control_env["SYSTEMCTL_STATE_DIR"] = str(scratch / "unit-state")
            control_env["NGINX_BIN"] = str(work / "absent-system-nginx")
            installed = self._install(release, control_env)
            self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
            values = {}
            for line in (work / "state" / "resolved.env").read_text(encoding="utf-8").splitlines():
                key, _, raw = line.partition("=")
                if key and raw:
                    values[key] = decode_env_value(raw)
            binary = Path(values["NGINX_BIN"])
            self.assertTrue(binary.is_relative_to(work), binary)
            self.assertTrue(binary.is_file(), binary)
            # Tripwires in the preparation chain: any call would land in the order log.
            for name, marker in (
                ("install_proxy.sh", "proxy-installer-invoked"),
                ("ensure_proxy_binary.sh", "proxy-prepare-invoked"),
            ):
                script = release / "operator" / "scripts" / name
                script.write_text(
                    script.read_text(encoding="utf-8").replace("\n", f'\necho {marker} >> "$ORDER_LOG"\n', 1),
                    encoding="utf-8",
                )
            binary.rename(work / "saved-nginx")
            operatorctl = ["bash", str(release / "operator" / "operatorctl.sh")]
            config = str(release.parent / "platform.json")
            for verb, expect_failure in (("status", False), ("health", True)):
                with self.subTest(verb=verb):
                    order.write_text("", encoding="utf-8")
                    before = tree_state(work)
                    result = run([*operatorctl, verb, "--config", config], control_env)
                    if expect_failure:
                        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                    else:
                        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertIn("managed nginx binary missing or not executable", result.stdout)
                    if verb == "health":
                        # The real health battery (not a stub) fails on the bound, missing binary.
                        self.assertIn(
                            f"FAIL     managed nginx binary missing or not executable: {binary}",
                            result.stdout,
                        )
                    self.assertEqual(before, tree_state(work), f"{verb} wrote into the deployment tree")
                    self.assertEqual(
                        list((scratch / "tmp").iterdir()), [], f"{verb} left temporary files behind"
                    )
                    calls = order.read_text(encoding="utf-8")
                    self.assertNotIn("proxy-installer-invoked", calls, f"{verb} ran the installer")
                    self.assertNotIn("proxy-prepare-invoked", calls, f"{verb} ran the preparation")
                    self.assertFalse(binary.exists(), f"{verb} recreated the removed binary")

    def test_stage_builds_both_candidate_generations_without_switching(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            release, control_env, order = self._prepare(Path(tmp), fresh=False, ready_after=1, db_exists=True)
            result = self._install(release, control_env)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            tree = release.parent
            staged = run(
                [
                    "bash",
                    str(release / "operator" / "operatorctl.sh"),
                    "stage",
                    "--config",
                    str(tree / "platform.json"),
                    "--install-id",
                    "gen-2",
                ],
                control_env,
            )
            self.assertEqual(staged.returncode, 0, staged.stdout + staged.stderr)
            state = Path(tmp) / "state"
            active = json.loads((state / "install-record.json").read_text(encoding="utf-8"))
            self.assertEqual(active["install_id"], "gen-1", "stage must not switch the active generation")
            self.assertTrue((state / "snapshots" / "gen-2.json").is_file())
            self.assertTrue((state / "derived" / "gen-2" / "operator.env").is_file())
            receipt = json.loads((state / "promotions" / "gen-2" / "receipt.json").read_text(encoding="utf-8"))
            self.assertEqual(
                [row["step"] for row in receipt["steps"]],
                ["linux-generation", "windows-generation"],
                "stage performs no schema DDL; the guarded windows own it",
            )
            self.assertEqual({row["status"] for row in receipt["steps"]}, {"completed"})
            # The bound source is the immutable generation file with its real digest.
            self.assertEqual(receipt["from"], str(state / "install-records" / "gen-1.json"))
            self.assertEqual(len(receipt["from_sha256"]), 64)
            self.assertEqual(len(receipt["to_sha256"]), 64)
            binding = json.loads(
                (state / "promotions" / "gen-2" / "windows-binding.json").read_text(encoding="utf-8")
            )
            self.assertEqual(binding["task_name"], "description-e2e-endpoint")
            self.assertEqual(binding["state_root"], "C:/description-e2e/state")
            self.assertEqual(binding["dir"], "C:/description-e2e/state/lifecycle/gen-2")
            self.assertEqual(binding["archive"], "description-1.4.0-windows-cp312-x86_64.zip")
            self.assertEqual(binding["artifact_sha256"], hashlib.sha256(b"windows-archive").hexdigest())
            lines = order.read_text(encoding="utf-8").splitlines()
            mkdir_line = next(
                index for index, line in enumerate(lines) if line.startswith("ssh ") and "New-Item" in line
            )
            scp_line = next(index for index, line in enumerate(lines) if line.startswith("scp "))
            self.assertLess(mkdir_line, scp_line, "the private remote directory exists before the transfer")
            # The full release carries three archives; exactly the native one travels.
            self.assertIn("description-1.4.0-windows-cp312-x86_64.zip", lines[scp_line])
            self.assertNotIn("linux-cp312", lines[scp_line])
            self.assertNotIn("deployment.zip", lines[scp_line])
            self.assertNotIn("platform close_admission", lines)

    @staticmethod
    def _derived_value(state: Path, generation: str, key: str) -> str:
        for line in (state / "derived" / generation / "operator.env").read_text(encoding="utf-8").splitlines():
            if line.startswith(key + "="):
                return decode_env_value(line.split("=", 1)[1].strip())
        raise AssertionError(f"the derived environment carries no {key}")

    def _clone_generation_stubs(self, state: Path) -> None:
        source = Path(self._derived_value(state, "gen-1", "AIRFLOW_VENV")) / "bin"
        target = Path(self._derived_value(state, "gen-2", "AIRFLOW_VENV")) / "bin"
        target.mkdir(parents=True, exist_ok=True)
        for name in ("python", "airflow"):
            (target / name).write_bytes((source / name).read_bytes())
            (target / name).chmod(0o755)

    def _maintenance(self, release: Path, env: dict, verb: str, generation: str = ""):
        tree = release.parent
        command = ["bash", str(release / "operator" / "operatorctl.sh"), verb, "--config", str(tree / "platform.json")]
        if generation:
            command += ["--install-id", generation]
        return run(command, env)

    def _health_double(self, release: Path, env: dict) -> None:
        health = {
            "schema_version": ir.HEALTH_SCHEMA,
            "pipeline_id": ir.PIPELINE_ID,
            "native_platform": True,
            "ready": True,
            "release": {"version": "1.4.0", "source_commit": "c" * 40, "source_sha256": "a" * 64, "role": "native"},
            "contract": {"schema_version": CONTRACT["schema_version"], "digest": CONTRACT_SHA256},
            "resource_policy": {"schema_version": ir.RESOURCE_POLICY_SCHEMA, "digest": ir.resource_policy_v1_digest()},
            "install": {"config_digest": "d" * 64, "install_id": "ep-7"},
            "endpoint": {"port": 9765},
            "cad": {"external_session_detected": False, "owned_sessions": 0, "lease_held": False},
            "jobs": {"active": 0, "queued": 0, "unresolved": 0},
            "capacity": {"free_bytes": 1, "reserved_bytes": 0},
            "readiness": {"reason_code": "", "native": {"ok": True, "problems": []}},
        }
        health_file = release.parent / "windows-health.json"
        health_file.write_text(json.dumps({"passed": True, "verb": "health", "health": health}), encoding="utf-8")
        env["FIXTURE_HEALTH_FILE"] = str(health_file)

    def _linux_health_double(self, release: Path) -> None:
        """The gate runs for real; its Linux probe is a bound double (no live HTTP services)."""
        write_stub(release / "operator" / "health.sh", "#!/usr/bin/env bash\nexit 0\n")

    def test_maintenance_reconcile_verifies_the_candidate_before_any_ddl(self) -> None:
        """A tampered staged snapshot refuses with zero live-DDL calls."""

        with tempfile.TemporaryDirectory() as tmp:
            release, control_env, order = self._prepare(Path(tmp), fresh=False, ready_after=1, db_exists=True)
            installed = self._install(release, control_env)
            self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
            state = Path(tmp) / "state"
            control_env["EXPECTED_PLATFORM_DSN"] = self._derived_value(state, "gen-1", "PLATFORM_OPERATOR_DSN")
            self._health_double(release, control_env)
            (Path(tmp) / "handoffs").mkdir(exist_ok=True)
            (Path(tmp) / "specifications").mkdir(exist_ok=True)
            staged = self._maintenance(release, control_env, "stage", "gen-2")
            self.assertEqual(staged.returncode, 0, staged.stdout + staged.stderr)
            receipt = json.loads((state / "promotions" / "gen-2" / "receipt.json").read_text(encoding="utf-8"))
            snapshot = Path(receipt["to"])
            snapshot.write_bytes(snapshot.read_bytes() + b"\n")
            (Path(tmp) / "admission").write_text("true", encoding="utf-8")  # the fixture's closed gate
            before = order.read_text(encoding="utf-8").count("psql_platform_apply")
            refused = run(
                [
                    "bash", str(release / "operator" / "operatorctl.sh"), "reconcile",
                    "--config", str(release.parent / "platform.json"), "--install-id", "gen-2",
                    "--run-id", "portal-1", "--stage", "freeze", "--outcome", "failed",
                    "--evidence", "receipt.json",
                ],
                control_env,
            )
            self.assertNotEqual(refused.returncode, 0, refused.stdout + refused.stderr)
            self.assertIn("snapshot", refused.stderr)
            self.assertEqual(
                order.read_text(encoding="utf-8").count("psql_platform_apply"),
                before,
                "no live DDL may run before the staged identity verifies",
            )

    def test_maintenance_reconcile_refuses_an_undefined_candidate_value(self) -> None:
        """A candidate env that cannot define a value never falls back to the running generation."""

        with tempfile.TemporaryDirectory() as tmp:
            release, control_env, order = self._prepare(Path(tmp), fresh=False, ready_after=1, db_exists=True)
            installed = self._install(release, control_env)
            self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
            state = Path(tmp) / "state"
            control_env["EXPECTED_PLATFORM_DSN"] = self._derived_value(state, "gen-1", "PLATFORM_OPERATOR_DSN")
            self._health_double(release, control_env)
            (Path(tmp) / "handoffs").mkdir(exist_ok=True)
            (Path(tmp) / "specifications").mkdir(exist_ok=True)
            staged = self._maintenance(release, control_env, "stage", "gen-2")
            self.assertEqual(staged.returncode, 0, staged.stdout + staged.stderr)
            (Path(tmp) / "admission").write_text("true", encoding="utf-8")  # the fixture's closed gate
            candidate_env = state / "derived" / "gen-2" / "operator.env"
            original = candidate_env.read_text(encoding="utf-8")
            dsn_line = next(line for line in original.splitlines() if line.startswith("PLATFORM_SERVICE_DSN="))
            foreign = "postgresql://platform_service@/airflow_meta?host=/elsewhere&port=1"
            cases = {
                # A missing key, an empty candidate and a defeated (unquoted "&") assignment are
                # all three "the file does not define it": never the inherited running value,
                # always a fail-closed refusal at the read, before any DDL or recovery step.
                "missing_key": (
                    "PLATFORM_SERVICE_DSN",
                    "\n".join(
                        line for line in original.splitlines() if not line.startswith("PLATFORM_SERVICE_DSN=")
                    )
                    + "\n",
                ),
                "empty_file": ("AIRFLOW_VENV", ""),
                "defeated_unquoted_assignment": (
                    "PLATFORM_SERVICE_DSN",
                    original.replace(dsn_line, f"PLATFORM_SERVICE_DSN={foreign}"),
                ),
            }
            receipt_path = state / "promotions" / "gen-2" / "receipt.json"
            for label, (key, document) in cases.items():
                with self.subTest(label):
                    candidate_env.write_text(document, encoding="utf-8")
                    receipt_before = receipt_path.read_bytes()
                    before = order.read_text(encoding="utf-8").count("psql_platform_apply")
                    refused = run(
                        [
                            "bash", str(release / "operator" / "operatorctl.sh"), "reconcile",
                            "--config", str(release.parent / "platform.json"), "--install-id", "gen-2",
                            "--run-id", "portal-1", "--stage", "freeze", "--outcome", "failed",
                            "--evidence", "receipt.json",
                        ],
                        control_env,
                    )
                    self.assertNotEqual(refused.returncode, 0, refused.stdout + refused.stderr)
                    self.assertIn("derived environment", refused.stderr)
                    self.assertIn(key, refused.stderr)
                    self.assertEqual(
                        order.read_text(encoding="utf-8").count("psql_platform_apply"),
                        before,
                        f"{label}: no live DDL may run for an undefined candidate value",
                    )
                    self.assertEqual(
                        receipt_path.read_bytes(), receipt_before, f"{label}: the receipt is untouched"
                    )
                    self.assertFalse(
                        (state / "promotions" / "gen-2" / "reconcile-schema-migration.out").exists(),
                        f"{label}: the guarded migration never ran",
                    )
            candidate_env.write_text(original, encoding="utf-8")

    def test_maintenance_reconcile_reads_a_legacy_running_environment(self) -> None:
        """A pre-contract running generation (bare, unsourceable DSN lines) must not block the
        route: the running side of the authority check comes from its canonical rendering."""

        with tempfile.TemporaryDirectory() as tmp:
            release, control_env, _ = self._prepare(Path(tmp), fresh=False, ready_after=1, db_exists=True)
            installed = self._install(release, control_env)
            self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
            state = Path(tmp) / "state"
            control_env["EXPECTED_PLATFORM_DSN"] = self._derived_value(state, "gen-1", "PLATFORM_OPERATOR_DSN")
            self._health_double(release, control_env)
            (Path(tmp) / "handoffs").mkdir(exist_ok=True)
            (Path(tmp) / "specifications").mkdir(exist_ok=True)
            staged = self._maintenance(release, control_env, "stage", "gen-2")
            self.assertEqual(staged.returncode, 0, staged.stdout + staged.stderr)
            # Degrade the running generation's derived env to the pre-contract writer's shape:
            # bare DSN values whose "&" no shell can source (the running deployment's canonical
            # rendering, resolved.env, is untouched and stays the route's running authority).
            running_env = state / "derived" / "gen-1" / "operator.env"
            legacy_lines = []
            for line in running_env.read_text(encoding="utf-8").splitlines():
                key = line.split("=", 1)[0]
                if "=" in line and key.endswith(("_DSN", "_DB_URL")):
                    legacy_lines.append(f"{key}={decode_env_value(line.split('=', 1)[1].strip())}")
                else:
                    legacy_lines.append(line)
            running_env.write_text("\n".join(legacy_lines) + "\n", encoding="utf-8")
            (Path(tmp) / "admission").write_text("true", encoding="utf-8")  # the fixture's closed gate
            receipt = json.loads((state / "promotions" / "gen-2" / "receipt.json").read_text(encoding="utf-8"))
            snapshot = Path(receipt["to"])
            snapshot.write_bytes(snapshot.read_bytes() + b"\n")
            refused = run(
                [
                    "bash", str(release / "operator" / "operatorctl.sh"), "reconcile",
                    "--config", str(release.parent / "platform.json"), "--install-id", "gen-2",
                    "--run-id", "portal-1", "--stage", "freeze", "--outcome", "failed",
                    "--evidence", "receipt.json",
                ],
                control_env,
            )
            self.assertNotEqual(refused.returncode, 0, refused.stdout + refused.stderr)
            # The route passed the environment reads (the legacy running side included) and
            # reached the staged-identity guard - the same failure point as with a canonical
            # running environment.
            self.assertIn("snapshot", refused.stderr)
            self.assertNotIn("does not declare", refused.stderr)

    def test_maintenance_reconcile_refuses_a_corrupt_running_environment(self) -> None:
        """The running side is decoded as data: missing, empty, malformed and foreign
        declarations all refuse explicitly, with nothing inherited and no live DDL."""

        with tempfile.TemporaryDirectory() as tmp:
            release, control_env, order = self._prepare(Path(tmp), fresh=False, ready_after=1, db_exists=True)
            installed = self._install(release, control_env)
            self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
            state = Path(tmp) / "state"
            control_env["EXPECTED_PLATFORM_DSN"] = self._derived_value(state, "gen-1", "PLATFORM_OPERATOR_DSN")
            self._health_double(release, control_env)
            (Path(tmp) / "handoffs").mkdir(exist_ok=True)
            (Path(tmp) / "specifications").mkdir(exist_ok=True)
            staged = self._maintenance(release, control_env, "stage", "gen-2")
            self.assertEqual(staged.returncode, 0, staged.stdout + staged.stderr)
            (Path(tmp) / "admission").write_text("true", encoding="utf-8")  # the fixture's closed gate
            running_env = state / "derived" / "gen-1" / "operator.env"
            original = running_env.read_text(encoding="utf-8")
            dsn_line = next(line for line in original.splitlines() if line.startswith("PLATFORM_SERVICE_DSN="))
            foreign = decode_env_value(dsn_line.split("=", 1)[1].strip()).replace("airflow_meta", "another_meta")
            cases = {
                "missing_key": (
                    "does not declare PLATFORM_SERVICE_DSN",
                    "\n".join(
                        line for line in original.splitlines() if not line.startswith("PLATFORM_SERVICE_DSN=")
                    )
                    + "\n",
                ),
                "empty_file": ("does not declare PLATFORM_SERVICE_DSN", ""),
                "malformed": (
                    "not one shell word",
                    original.replace(dsn_line, "PLATFORM_SERVICE_DSN=one two"),
                ),
                "foreign_authority": (
                    "another database authority",
                    original.replace(dsn_line, f"PLATFORM_SERVICE_DSN={shlex.quote(foreign)}"),
                ),
                "duplicate_conflict": (
                    "conflicting duplicate declaration",
                    original + "PLATFORM_SERVICE_DSN=" + shlex.quote(foreign) + "\n",
                ),
            }
            receipt_path = state / "promotions" / "gen-2" / "receipt.json"
            for label, (message, document) in cases.items():
                with self.subTest(label):
                    running_env.write_text(document, encoding="utf-8")
                    receipt_before = receipt_path.read_bytes()
                    before = order.read_text(encoding="utf-8").count("psql_platform_apply")
                    refused = run(
                        [
                            "bash", str(release / "operator" / "operatorctl.sh"), "reconcile",
                            "--config", str(release.parent / "platform.json"), "--install-id", "gen-2",
                            "--run-id", "portal-1", "--stage", "freeze", "--outcome", "failed",
                            "--evidence", "receipt.json",
                        ],
                        control_env,
                    )
                    self.assertNotEqual(refused.returncode, 0, refused.stdout + refused.stderr)
                    self.assertIn(message, refused.stderr)
                    self.assertEqual(
                        order.read_text(encoding="utf-8").count("psql_platform_apply"),
                        before,
                        f"{label}: no live DDL may run for a corrupt running declaration",
                    )
                    self.assertEqual(
                        receipt_path.read_bytes(), receipt_before, f"{label}: the receipt is untouched"
                    )
                    self.assertFalse(
                        (state / "promotions" / "gen-2" / "reconcile-schema-migration.out").exists(),
                        f"{label}: the guarded migration never ran",
                    )
            running_env.write_text(original, encoding="utf-8")

    def test_maintenance_reconcile_reads_identical_duplicate_declarations(self) -> None:
        """Identical duplicates decode to the same bytes the canonical readers see and are
        accepted; only conflicting duplicates refuse (never first-wins)."""

        with tempfile.TemporaryDirectory() as tmp:
            release, control_env, _ = self._prepare(Path(tmp), fresh=False, ready_after=1, db_exists=True)
            installed = self._install(release, control_env)
            self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
            state = Path(tmp) / "state"
            control_env["EXPECTED_PLATFORM_DSN"] = self._derived_value(state, "gen-1", "PLATFORM_OPERATOR_DSN")
            self._health_double(release, control_env)
            (Path(tmp) / "handoffs").mkdir(exist_ok=True)
            (Path(tmp) / "specifications").mkdir(exist_ok=True)
            staged = self._maintenance(release, control_env, "stage", "gen-2")
            self.assertEqual(staged.returncode, 0, staged.stdout + staged.stderr)
            running_env = state / "derived" / "gen-1" / "operator.env"
            original = running_env.read_text(encoding="utf-8")
            dsn_line = next(line for line in original.splitlines() if line.startswith("PLATFORM_SERVICE_DSN="))
            running_env.write_text(original + dsn_line + "\n", encoding="utf-8")
            (Path(tmp) / "admission").write_text("true", encoding="utf-8")  # the fixture's closed gate
            receipt = json.loads((state / "promotions" / "gen-2" / "receipt.json").read_text(encoding="utf-8"))
            snapshot = Path(receipt["to"])
            snapshot.write_bytes(snapshot.read_bytes() + b"\n")
            refused = run(
                [
                    "bash", str(release / "operator" / "operatorctl.sh"), "reconcile",
                    "--config", str(release.parent / "platform.json"), "--install-id", "gen-2",
                    "--run-id", "portal-1", "--stage", "freeze", "--outcome", "failed",
                    "--evidence", "receipt.json",
                ],
                control_env,
            )
            running_env.write_text(original, encoding="utf-8")
            # The route passed the environment reads (identical duplicates accepted) and reached
            # the staged-identity guard, exactly like the canonical single-declaration case.
            self.assertNotEqual(refused.returncode, 0, refused.stdout + refused.stderr)
            self.assertIn("snapshot", refused.stderr)
            self.assertNotIn("duplicate", refused.stderr)
            self.assertNotIn("does not declare", refused.stderr)

    def test_stage_promote_rollback_drill_with_bound_doubles(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            release, control_env, _ = self._prepare(Path(tmp), fresh=False, ready_after=1, db_exists=True)
            installed = self._install(release, control_env)
            self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
            state = Path(tmp) / "state"
            control_env["EXPECTED_PLATFORM_DSN"] = self._derived_value(state, "gen-1", "PLATFORM_OPERATOR_DSN")
            self._health_double(release, control_env)
            (Path(tmp) / "handoffs").mkdir(exist_ok=True)
            (Path(tmp) / "handoffs" / "frozen-original.dat").write_bytes(b"frozen")
            (Path(tmp) / "specifications").mkdir(exist_ok=True)
            (Path(tmp) / "specifications" / "spec.json").write_bytes(b"spec")
            staged = self._maintenance(release, control_env, "stage", "gen-2")
            self.assertEqual(staged.returncode, 0, staged.stdout + staged.stderr)
            self._clone_generation_stubs(state)
            self._linux_health_double(release)
            promoted = self._maintenance(release, control_env, "promote", "gen-2")
            self.assertEqual(promoted.returncode, 0, promoted.stdout + promoted.stderr)
            active = json.loads((state / "install-record.json").read_text(encoding="utf-8"))
            self.assertEqual(active["install_id"], "gen-2")
            self.assertEqual([row["step"] for row in active["journal"]], ["promotion-receipt"])
            self.assertTrue(active["previous"]["record"].endswith("install-records/gen-1.json"))
            admission = Path(tmp) / "admission"
            self.assertEqual(admission.read_text().strip(), "false")
            receipt = json.loads((state / "promotions" / "gen-2" / "receipt.json").read_text(encoding="utf-8"))
            self.assertEqual(
                [row["step"] for row in receipt["steps"]],
                [
                    "linux-generation", "windows-generation",
                    "close-admission", "drain", "backup",
                    "stop-services", "schema-migration", "windows-switch", "endpoint-identity",
                    "switch-pointer", "start-services", "verify", "open-admission",
                ],
            )
            self.assertEqual({row["status"] for row in receipt["steps"]}, {"completed"})
            self.assertEqual(
                (state / "promotions" / "gen-2" / "windows-promote.out.ssh-rc").read_text(encoding="utf-8"),
                "0\n",
            )
            # The candidate's verified migrations run after both ends stopped and before
            # either pointer moves (deployment.md).
            steps = [row["step"] for row in receipt["steps"] if row["verb"] == "promote"]
            self.assertLess(steps.index("stop-services"), steps.index("schema-migration"))
            self.assertLess(steps.index("schema-migration"), steps.index("windows-switch"))
            backup_manifest = json.loads(
                (state / "promotions" / "gen-2" / "evidence" / "backup-manifest.json").read_text(encoding="utf-8")
            )
            self.assertIn("platform.pgc", backup_manifest["files"])
            self.assertIn("state/resolved.env", backup_manifest["files"])
            self.assertFalse((state / "promotions" / "gen-2" / "backup").exists())
            rolled = self._maintenance(release, control_env, "rollback")
            self.assertEqual(rolled.returncode, 0, rolled.stdout + rolled.stderr)
            restored = json.loads((state / "install-record.json").read_text(encoding="utf-8"))
            self.assertEqual(restored["install_id"], "gen-1")
            self.assertEqual(admission.read_text().strip(), "true", "rollback keeps the admission closed")
            final_receipt = json.loads((state / "promotions" / "gen-2" / "receipt.json").read_text(encoding="utf-8"))
            self.assertEqual(
                [row["step"] for row in final_receipt["steps"] if row["verb"] == "rollback"],
                [
                    "rollback-close", "rollback-drain", "rollback-stop", "rollback-windows",
                    "rollback-linux", "rollback-verify",
                ],
            )
            # Real restore drill on the sealed archive: open it, then rebuild a broken record
            # from the backup bytes and re-verify the running generation.
            tree = release.parent
            sealed = state / "promotions" / "gen-2" / "backup.tar.enc"
            self.assertTrue(sealed.is_file())
            manifest = json.loads(
                (state / "promotions" / "gen-2" / "evidence" / "backup-manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["encrypted"]["sha256"], hashlib.sha256(sealed.read_bytes()).hexdigest())
            key = state / "secrets" / "backup.key"
            self.assertEqual(stat.S_IMODE(key.stat().st_mode), 0o600)
            self.assertFalse((state / "promotions" / "gen-2" / "backup").exists(), "staging must be cleaned")
            manifest_path = state / "promotions" / "gen-2" / "evidence" / "backup-manifest.json"
            scratch = Path(tmp) / "restore"
            opened = run(
                [
                    "python3", str(release / "operator" / "backup.py"), "open",
                    "--archive", str(sealed), "--key", str(key), "--manifest", str(manifest_path),
                    "--dest", str(scratch),
                ],
                {},
            )
            self.assertEqual(opened.returncode, 0, opened.stderr)
            closure = json.loads((scratch / "closure.json").read_text(encoding="utf-8"))
            self.assertIn("handoff_root", closure["captured_roots"])
            self.assertIn("record_root_0", closure["captured_roots"])
            self.assertTrue((scratch / "handoff" / "frozen-original.dat").is_file())
            self.assertTrue((scratch / "specifications" / "root-0" / "spec.json").is_file())
            self.assertTrue(
                {"certificate", "private_key", "database_dsn", "feishu_secret", "endpoint_token"}
                <= set(closure["captured_secrets"])
            )
            self.assertEqual(
                set(closure["not_captured"]),
                {"workspace_root", "store_root", "release_root"},
                "every uncaptured reference is declared",
            )
            tampered = Path(tmp) / "tampered.enc"
            forged = bytearray(sealed.read_bytes())
            forged[-1] ^= 1
            tampered.write_bytes(bytes(forged))
            refused = run(
                [
                    "python3", str(release / "operator" / "backup.py"), "open",
                    "--archive", str(tampered), "--key", str(key), "--manifest", str(manifest_path),
                    "--dest", str(Path(tmp) / "tampered-out"),
                ],
                {},
            )
            self.assertNotEqual(refused.returncode, 0)
            self.assertIn("does not match the manifest", refused.stderr)
            marker = Path(tmp) / "outside-private.dat"
            marker.write_bytes(b"outside")
            forged_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            forged_manifest["files"] = {"../outside-private.dat": hashlib.sha256(marker.read_bytes()).hexdigest()}
            forged_path = Path(tmp) / "forged-manifest.json"
            forged_path.write_text(json.dumps(forged_manifest), encoding="utf-8")
            escape = run(
                [
                    "python3", str(release / "operator" / "backup.py"), "open",
                    "--archive", str(sealed), "--key", str(key), "--manifest", str(forged_path),
                    "--dest", str(Path(tmp) / "escape-out"),
                ],
                {},
            )
            self.assertNotEqual(escape.returncode, 0)
            self.assertIn("escapes its root", escape.stderr)
            base_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

            def refused_manifest(label: str, text: str, needle: str) -> None:
                forged = Path(tmp) / f"manifest-{label}.json"
                forged.write_text(text, encoding="utf-8")
                result = run(
                    [
                        "python3", str(release / "operator" / "backup.py"), "open",
                        "--archive", str(sealed), "--key", str(key), "--manifest", str(forged),
                        "--dest", str(Path(tmp) / f"out-{label}"),
                    ],
                    {},
                )
                self.assertNotEqual(result.returncode, 0, label)
                self.assertIn(needle, result.stderr, label)

            with_unknown = dict(base_manifest)
            with_unknown["extra"] = 1
            refused_manifest(
                "unknown-field", json.dumps(with_unknown), "unknown or missing fields"
            )
            with_cipher = json.loads(json.dumps(base_manifest))
            with_cipher["encrypted"]["cipher"] = "unsupported-invented-cipher"
            refused_manifest("cipher", json.dumps(with_cipher), "unsupported backup cipher")
            duplicate = (
                '{"schema": "solidworks-to-urdf.promote-backup/v1", '
                '"files": {"retained.dat": "' + "0" * 64 + '", "retained.dat": "' + "1" * 64 + '"}, '
                '"encrypted": {"path": "x", "sha256": "' + "2" * 64 + '", "cipher": "aes-256-cbc", "key": "k"}}'
            )
            refused_manifest("duplicate", duplicate, "duplicate manifest key")
            (state / "install-record.json").write_bytes(
                (scratch / "state" / "install-record.json").read_bytes()
            )
            verified = run(
                [
                    "python3", str(release / "operator" / "install_record.py"), "verify-active",
                    "--config", str(tree / "platform.json"), "--state-root", str(state),
                ],
                {},
            )
            self.assertEqual(verified.returncode, 0, verified.stdout + verified.stderr)

    def test_cold_stage_promote_uses_database_and_restarts_only_after_switch(self) -> None:
        """The sealed recovery state can stage and promote without restoring old executors.

        PostgreSQL stays authoritative and active; the Airflow, tunnel, portal and proxy units
        are initially inactive. Stage reads the running record and database only. Promote's own
        start-services step brings the candidate up after both pointers and identity verify.
        """

        with tempfile.TemporaryDirectory() as tmp:
            release, control_env, order = self._prepare(Path(tmp), fresh=False, ready_after=1, db_exists=True)
            self.assertEqual(self._install(release, control_env).returncode, 0)
            state = Path(tmp) / "state"
            control_env["EXPECTED_PLATFORM_DSN"] = self._derived_value(state, "gen-1", "PLATFORM_OPERATOR_DSN")
            units = state.parent / "unit-state"
            for unit in (
                "description-solidworks-tunnel",
                "description-airflow-dag-processor",
                "description-airflow-scheduler",
                "description-airflow-api-server",
                "description-portal",
                "description-operator-proxy",
            ):
                (units / unit).write_text("inactive", encoding="utf-8")
            self.assertEqual((units / "description-postgres").read_text(encoding="utf-8"), "active")

            order.write_text("", encoding="utf-8")
            staged = self._maintenance(release, control_env, "stage", "gen-2")
            self.assertEqual(staged.returncode, 0, staged.stdout + staged.stderr)
            self.assertNotIn("services_start", order.read_text(encoding="utf-8"))
            self.assertEqual(
                {path.name: path.read_text(encoding="utf-8") for path in units.glob("description-*")},
                {
                    "description-postgres": "active",
                    "description-solidworks-tunnel": "inactive",
                    "description-airflow-dag-processor": "inactive",
                    "description-airflow-scheduler": "inactive",
                    "description-airflow-api-server": "inactive",
                    "description-portal": "inactive",
                    "description-operator-proxy": "inactive",
                },
            )

            self._clone_generation_stubs(state)
            self._health_double(release, control_env)
            self._linux_health_double(release)
            promoted = self._maintenance(release, control_env, "promote", "gen-2")
            self.assertEqual(promoted.returncode, 0, promoted.stdout + promoted.stderr)
            self.assertEqual(
                json.loads((state / "install-record.json").read_text(encoding="utf-8"))["install_id"],
                "gen-2",
            )
            receipt = json.loads((state / "promotions" / "gen-2" / "receipt.json").read_text(encoding="utf-8"))
            steps = {row["step"]: row["status"] for row in receipt["steps"]}
            self.assertEqual(steps["stop-services"], "completed")
            self.assertEqual(steps["start-services"], "completed")
            self.assertEqual(steps["open-admission"], "completed")

    def test_promote_failure_keeps_the_admission_closed_and_records_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            release, control_env, _ = self._prepare(Path(tmp), fresh=False, ready_after=1, db_exists=True)
            installed = self._install(release, control_env)
            self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
            state = Path(tmp) / "state"
            control_env["EXPECTED_PLATFORM_DSN"] = self._derived_value(state, "gen-1", "PLATFORM_OPERATOR_DSN")
            self._health_double(release, control_env)
            staged = self._maintenance(release, control_env, "stage", "gen-2")
            self.assertEqual(staged.returncode, 0, staged.stdout + staged.stderr)
            self._clone_generation_stubs(state)
            failed = self._maintenance(release, dict(control_env, FIXTURE_FAIL_PROMOTE="1"), "promote", "gen-2")
            self.assertEqual(failed.returncode, 1)
            self.assertIn("Windows switch failed", failed.stderr)
            active = json.loads((state / "install-record.json").read_text(encoding="utf-8"))
            self.assertEqual(active["install_id"], "gen-1", "a refused switch must not move the pointer")
            self.assertEqual((Path(tmp) / "admission").read_text().strip(), "true")
            receipt = json.loads((state / "promotions" / "gen-2" / "receipt.json").read_text(encoding="utf-8"))
            steps = {row["step"]: row["status"] for row in receipt["steps"]}
            self.assertEqual(steps["windows-switch"], "failed")
            self.assertNotIn("open-admission", steps)

    def test_windows_structured_refusal_with_ssh_rc_one_is_recorded_failed(self) -> None:
        """The real PowerShell refusal and its rc 1 are one definite pre-switch refusal.

        The Windows Die helper emits the captured JSON and exits 1, which SSH preserves.
        Parsing only after a zero-exit test previously discarded that evidence and recorded
        the switch as unknown. The active pointer, admission and token must remain untouched.
        """

        with tempfile.TemporaryDirectory() as tmp:
            release, control_env, order = self._prepare(Path(tmp), fresh=False, ready_after=1, db_exists=True)
            self.assertEqual(self._install(release, control_env).returncode, 0)
            state = Path(tmp) / "state"
            control_env["EXPECTED_PLATFORM_DSN"] = self._derived_value(state, "gen-1", "PLATFORM_OPERATOR_DSN")
            self._health_double(release, control_env)
            self.assertEqual(self._maintenance(release, control_env, "stage", "gen-2").returncode, 0)
            self._clone_generation_stubs(state)
            refusal = Path(tmp) / "captured-windows-refusal.out"
            refusal.write_bytes(CAPTURED_WINDOWS_CANDIDATE_CONFIG_MISMATCH.encode("utf-8"))
            self.assertEqual(len(CAPTURED_WINDOWS_CANDIDATE_CONFIG_MISMATCH.encode("utf-8")), 245)
            record_before = (state / "install-record.json").read_bytes()
            binding_before = (state / "promotions" / "gen-2" / "windows-binding.json").read_bytes()
            token_before = (Path(tmp) / "token").read_bytes()
            order.write_text("", encoding="utf-8")

            refused = self._maintenance(
                release,
                dict(
                    control_env,
                    FIXTURE_PROMOTE_OUTPUT=str(refusal),
                    FIXTURE_PROMOTE_RC="1",
                ),
                "promote",
                "gen-2",
            )

            self.assertEqual(refused.returncode, 1, refused.stdout + refused.stderr)
            self.assertIn("definite refusal", refused.stderr)
            self.assertIn("Windows switch failed", refused.stderr)
            self.assertNotIn("unacknowledged", refused.stderr)
            self.assertEqual((state / "install-record.json").read_bytes(), record_before)
            self.assertEqual((state / "promotions" / "gen-2" / "windows-binding.json").read_bytes(), binding_before)
            self.assertEqual((Path(tmp) / "token").read_bytes(), token_before)
            self.assertEqual((Path(tmp) / "admission").read_text().strip(), "true")
            self.assertEqual(
                (state / "promotions" / "gen-2" / "windows-promote.out").read_bytes(),
                refusal.read_bytes(),
            )
            self.assertEqual(
                (state / "promotions" / "gen-2" / "windows-promote.out.ssh-rc").read_text(encoding="utf-8"),
                "1\n",
            )
            receipt = json.loads((state / "promotions" / "gen-2" / "receipt.json").read_text(encoding="utf-8"))
            steps = {row["step"]: row["status"] for row in receipt["steps"]}
            self.assertEqual(steps["windows-switch"], "failed")
            self.assertNotIn("open-admission", steps)
            self.assertNotIn("platform open_admission", order.read_text(encoding="utf-8"))

    def test_windows_lost_or_contradictory_results_stay_unknown(self) -> None:
        cases = (
            ("lost-transport", 255, CAPTURED_WINDOWS_CANDIDATE_CONFIG_MISMATCH),
            ("malformed", 1, "not-json\r\n"),
            ("empty", 1, ""),
            ("wrong-verb", 1, '{"passed":false,"code":"refused","verb":"stage"}\r\n'),
            ("success-on-nonzero", 1, '{"passed":true,"verb":"promote"}\r\n'),
        )
        for label, remote_rc, response in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as tmp:
                release, control_env, order = self._prepare(
                    Path(tmp), fresh=False, ready_after=1, db_exists=True
                )
                self.assertEqual(self._install(release, control_env).returncode, 0)
                state = Path(tmp) / "state"
                control_env["EXPECTED_PLATFORM_DSN"] = self._derived_value(
                    state, "gen-1", "PLATFORM_OPERATOR_DSN"
                )
                self._health_double(release, control_env)
                self.assertEqual(self._maintenance(release, control_env, "stage", "gen-2").returncode, 0)
                self._clone_generation_stubs(state)
                output = Path(tmp) / "windows-response.out"
                output.write_text(response, encoding="utf-8")
                record_before = (state / "install-record.json").read_bytes()
                token_before = (Path(tmp) / "token").read_bytes()
                order.write_text("", encoding="utf-8")

                result = self._maintenance(
                    release,
                    dict(
                        control_env,
                        FIXTURE_PROMOTE_OUTPUT=str(output),
                        FIXTURE_PROMOTE_RC=str(remote_rc),
                    ),
                    "promote",
                    "gen-2",
                )

                self.assertEqual(result.returncode, 1, f"{label}: {result.stdout}{result.stderr}")
                self.assertIn("unacknowledged", result.stderr, label)
                self.assertEqual((state / "install-record.json").read_bytes(), record_before, label)
                self.assertEqual((Path(tmp) / "token").read_bytes(), token_before, label)
                self.assertEqual((Path(tmp) / "admission").read_text().strip(), "true", label)
                receipt = json.loads(
                    (state / "promotions" / "gen-2" / "receipt.json").read_text(encoding="utf-8")
                )
                steps = {row["step"]: row["status"] for row in receipt["steps"]}
                self.assertEqual(steps["windows-switch"], "unknown", label)
                self.assertNotIn("open-admission", steps, label)
                self.assertNotIn("platform open_admission", order.read_text(encoding="utf-8"), label)
                self.assertEqual(
                    (state / "promotions" / "gen-2" / "windows-promote.out.ssh-rc").read_text(encoding="utf-8"),
                    f"{remote_rc}\n",
                    label,
                )

    def test_unwritable_existing_lifecycle_output_is_not_read_as_fresh_evidence(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("root can truncate a 0444 file; the permission negative control needs an unprivileged user")
        with tempfile.TemporaryDirectory() as tmp:
            release, control_env, order = self._prepare(Path(tmp), fresh=False, ready_after=1, db_exists=True)
            self.assertEqual(self._install(release, control_env).returncode, 0)
            state = Path(tmp) / "state"
            control_env["EXPECTED_PLATFORM_DSN"] = self._derived_value(state, "gen-1", "PLATFORM_OPERATOR_DSN")
            self._health_double(release, control_env)
            self.assertEqual(self._maintenance(release, control_env, "stage", "gen-2").returncode, 0)
            self._clone_generation_stubs(state)
            output = state / "promotions" / "gen-2" / "windows-promote.out"
            output.write_bytes(CAPTURED_WINDOWS_CANDIDATE_CONFIG_MISMATCH.encode("utf-8"))
            output.chmod(0o444)
            record_before = (state / "install-record.json").read_bytes()
            token_before = (Path(tmp) / "token").read_bytes()
            order.write_text("", encoding="utf-8")

            result = self._maintenance(release, control_env, "promote", "gen-2")

            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertIn("unacknowledged", result.stderr)
            self.assertEqual(output.read_bytes(), CAPTURED_WINDOWS_CANDIDATE_CONFIG_MISMATCH.encode("utf-8"))
            self.assertEqual((state / "install-record.json").read_bytes(), record_before)
            self.assertEqual((Path(tmp) / "token").read_bytes(), token_before)
            receipt = json.loads((state / "promotions" / "gen-2" / "receipt.json").read_text(encoding="utf-8"))
            steps = {row["step"]: row["status"] for row in receipt["steps"]}
            self.assertEqual(steps["windows-switch"], "unknown")
            self.assertNotIn("platform open_admission", order.read_text(encoding="utf-8"))

    def test_lifecycle_exit_status_evidence_write_failure_stays_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            release, control_env, order = self._prepare(Path(tmp), fresh=False, ready_after=1, db_exists=True)
            self.assertEqual(self._install(release, control_env).returncode, 0)
            state = Path(tmp) / "state"
            control_env["EXPECTED_PLATFORM_DSN"] = self._derived_value(state, "gen-1", "PLATFORM_OPERATOR_DSN")
            self._health_double(release, control_env)
            self.assertEqual(self._maintenance(release, control_env, "stage", "gen-2").returncode, 0)
            self._clone_generation_stubs(state)
            output = state / "promotions" / "gen-2" / "windows-promote.out"
            output.write_bytes(CAPTURED_WINDOWS_CANDIDATE_CONFIG_MISMATCH.encode("utf-8"))
            (state / "promotions" / "gen-2" / "windows-promote.out.ssh-rc").mkdir()
            record_before = (state / "install-record.json").read_bytes()
            token_before = (Path(tmp) / "token").read_bytes()
            order.write_text("", encoding="utf-8")

            result = self._maintenance(release, control_env, "promote", "gen-2")

            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertIn("unacknowledged", result.stderr)
            self.assertEqual(output.read_bytes(), b"", "the stale refusal must be truncated before the rc write fails")
            self.assertEqual((state / "install-record.json").read_bytes(), record_before)
            self.assertEqual((Path(tmp) / "token").read_bytes(), token_before)
            receipt = json.loads((state / "promotions" / "gen-2" / "receipt.json").read_text(encoding="utf-8"))
            steps = {row["step"]: row["status"] for row in receipt["steps"]}
            self.assertEqual(steps["windows-switch"], "unknown")
            self.assertNotIn("platform open_admission", order.read_text(encoding="utf-8"))

    def test_promote_stop_failure_stays_closed_without_switching(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            release, control_env, _ = self._prepare(Path(tmp), fresh=False, ready_after=1, db_exists=True)
            self.assertEqual(self._install(release, control_env).returncode, 0)
            state = Path(tmp) / "state"
            control_env["EXPECTED_PLATFORM_DSN"] = self._derived_value(state, "gen-1", "PLATFORM_OPERATOR_DSN")
            self._health_double(release, control_env)
            self.assertEqual(self._maintenance(release, control_env, "stage", "gen-2").returncode, 0)
            self._clone_generation_stubs(state)
            failed = self._maintenance(release, dict(control_env, FIXTURE_FAIL_STOP="1"), "promote", "gen-2")
            self.assertEqual(failed.returncode, 1)
            self.assertIn("did not stop", failed.stderr)
            active = json.loads((state / "install-record.json").read_text(encoding="utf-8"))
            self.assertEqual(active["install_id"], "gen-1")
            self.assertEqual((Path(tmp) / "admission").read_text().strip(), "true")
            receipt = json.loads((state / "promotions" / "gen-2" / "receipt.json").read_text(encoding="utf-8"))
            steps = {row["step"]: row["status"] for row in receipt["steps"]}
            self.assertEqual(steps["stop-services"], "failed")
            self.assertNotIn("windows-switch", steps)

    def _use_real_airflow_services(self, release: Path) -> None:
        """Run the real services.sh in the release tree: the stop it performs decides
        whether the shared database survives into the guarded migration (the harness
        default is a recording stub). Only the stop phases delegate to the real script -
        the install/start phases keep the recording stub because the real render/start
        need the deployed environment (the changed boundary is the stop behaviour)."""

        real = release / "airflow" / "services.sh.real"
        real.write_bytes((ROOT / "deploy" / "airflow" / "services.sh").read_bytes())
        (release / "airflow" / "services.sh").write_text(
            "#!/usr/bin/env bash\n"
            'if [ "${1:-}" = "stop-executors" ] || [ "${1:-}" = "stop" ]; then\n'
            '  exec bash "$(dirname "$0")/services.sh.real" "$@"\n'
            "fi\n"
            'echo "services_$1" >> "$ORDER_LOG"\n'
            "exit 0\n",
            encoding="utf-8",
        )

    def test_promote_stop_keeps_the_shared_database_for_the_guarded_migrations(self) -> None:
        """Stateful doubles make the database dependency real (a full stop removes the
        socket and fails the next migration - see the companion test), so this regression
        fails by construction unless promote stops only the execution services and proves
        the shared database ready before its migrations."""

        with tempfile.TemporaryDirectory() as tmp:
            release, control_env, order = self._prepare(Path(tmp), fresh=False, ready_after=1, db_exists=True)
            self.assertEqual(self._install(release, control_env).returncode, 0)
            state = Path(tmp) / "state"
            control_env["EXPECTED_PLATFORM_DSN"] = self._derived_value(state, "gen-1", "PLATFORM_OPERATOR_DSN")
            self._health_double(release, control_env)
            self.assertEqual(self._maintenance(release, control_env, "stage", "gen-2").returncode, 0)
            self._clone_generation_stubs(state)
            # Only the promotion stop path must run the real services.sh; the install/start
            # phase stays on the harness stub (the real render needs the deployed env).
            self._use_real_airflow_services(release)
            self._linux_health_double(release)
            order.write_text("", encoding="utf-8")
            promoted = self._maintenance(release, control_env, "promote", "gen-2")
            self.assertEqual(promoted.returncode, 0, promoted.stdout + promoted.stderr)
            receipt_dir = state / "promotions" / "gen-2"
            rows = json.loads((receipt_dir / "receipt.json").read_text(encoding="utf-8"))["steps"]
            steps = {row["step"]: row["status"] for row in rows}
            self.assertEqual(steps["stop-services"], "completed")
            self.assertEqual(steps["schema-migration"], "completed")
            lines = order.read_text(encoding="utf-8").splitlines()
            self.assertFalse(
                [line for line in lines if "disable --now description-postgres" in line],
                "the promotion must never stop the shared database",
            )
            stopped = [line for line in lines if line.startswith("systemctl --user disable --now")]
            for unit in (
                "description-airflow-dag-processor",
                "description-airflow-scheduler",
                "description-airflow-api-server",
            ):
                self.assertTrue(any(unit in line for line in stopped), (unit, lines))
            migrate_at = next(index for index, line in enumerate(lines) if line == "airflow db migrate")
            self.assertTrue(
                all(
                    index < migrate_at
                    for index, line in enumerate(lines)
                    if line.startswith("systemctl --user disable --now")
                ),
                "every stop precedes the guarded migration",
            )
            self.assertEqual(
                (Path(control_env["SYSTEMCTL_STATE_DIR"]) / "description-postgres").read_text(),
                "active",
                "the shared database must stay up through the whole promotion",
            )
            evidence = (receipt_dir / "evidence" / "stop-services.out").read_text(encoding="utf-8")
            self.assertIn("services.sh: description-airflow-scheduler stopped (active_state=inactive)", evidence)
            self.assertIn("operatorctl: description-portal.service stopped (active_state=inactive)", evidence)
            self.assertIn("shared database ready for the guarded migrations", evidence)

    def test_full_stop_removes_database_availability_by_construction(self) -> None:
        """The companion of the regression above, on the same stateful doubles: the full
        stop (the operatorctl stop verb's semantics) really takes the database down, and
        the very next migration fails with the incident's socket error - the exact ordering
        the promotion must never use again."""

        with tempfile.TemporaryDirectory() as tmp:
            release, control_env, _ = self._prepare(Path(tmp), fresh=False, ready_after=1, db_exists=True)
            self.assertEqual(self._install(release, control_env).returncode, 0)
            state = Path(tmp) / "state"
            values = {}
            for line in (state / "resolved.env").read_text(encoding="utf-8").splitlines():
                key, _, raw = line.partition("=")
                if key and raw:
                    values[key] = decode_env_value(raw)
            stop_env = dict(control_env)
            stop_env.update(
                {
                    key: values[key]
                    for key in (
                        "AIRFLOW_VENV",
                        "AIRFLOW_HOME",
                        "POSTGRES_ROOT",
                        "POSTGRES_MAJOR",
                        "SOLIDWORKS_SSH_HOST",
                        "AIRFLOW_API_PORT",
                        "POSTGRES_PORT",
                        "TUNNEL_LOOPBACK_PORT",
                        "SOLIDWORKS_ENDPOINT_PORT",
                    )
                    if key in values
                }
            )
            pg_root = Path(values["POSTGRES_ROOT"])
            pg_bin = pg_root / "root/usr/lib/postgresql" / values.get("POSTGRES_MAJOR", "14") / "bin"
            probe_env = dict(stop_env)
            probe_env["LD_LIBRARY_PATH"] = f"{pg_root}/root/usr/lib/x86_64-linux-gnu:/unrelated/vendor"
            probe = [
                str(pg_bin / "pg_isready"),
                "-h",
                str(pg_root / "socket"),
                "-p",
                values["POSTGRES_PORT"],
            ]
            ready = run(probe, probe_env)
            self.assertEqual(ready.returncode, 0, ready.stdout + ready.stderr)
            self._use_real_airflow_services(release)
            stopped = run(["bash", str(release / "airflow" / "services.sh"), "stop"], stop_env)
            self.assertEqual(stopped.returncode, 0, stopped.stdout + stopped.stderr)
            self.assertEqual(
                (Path(control_env["SYSTEMCTL_STATE_DIR"]) / "description-postgres").read_text(),
                "inactive",
                "the full stop must actually take the database down",
            )
            gone = run(probe, probe_env)
            self.assertNotEqual(gone.returncode, 0, "the stopped database can no longer be ready")
            migrate = run(
                [str(Path(self._derived_value(state, "gen-1", "AIRFLOW_VENV")) / "bin" / "airflow"), "db migrate"],
                stop_env,
            )
            self.assertEqual(migrate.returncode, 1)
            self.assertIn("psycopg2.OperationalError", migrate.stderr)
            self.assertIn(".s.PGSQL.", migrate.stderr)

    def test_promote_seals_readonly_roots_without_changing_them(self) -> None:
        """The backup step copies the running roots with their modes (0555 directories, 0444
        files - the W5 v8 R2 stop): the copy is sealed, the single staging owner removes the
        read-only staging, and the source roots keep their bytes and modes untouched."""

        with tempfile.TemporaryDirectory() as tmp:
            release, control_env, _ = self._prepare(Path(tmp), fresh=False, ready_after=1, db_exists=True)
            self.assertEqual(self._install(release, control_env).returncode, 0)
            state = Path(tmp) / "state"
            control_env["EXPECTED_PLATFORM_DSN"] = self._derived_value(state, "gen-1", "PLATFORM_OPERATOR_DSN")
            self._health_double(release, control_env)
            roots = [Path(tmp) / name for name in ("handoffs", "specifications", "artifacts")]
            for root in roots:
                (root / "sealed-entry").mkdir(parents=True)
                frozen = root / "sealed-entry" / "frozen.dat"
                frozen.write_bytes(f"frozen-{root.name}".encode())
                frozen.chmod(0o444)
                (root / "sealed-entry").chmod(0o555)
            digests_before = self._digest_tree(*roots)
            modes_before = {
                str(path): stat.S_IMODE(path.lstat().st_mode) for root in roots for path in sorted(root.rglob("*"))
            }
            self.assertEqual(self._maintenance(release, control_env, "stage", "gen-2").returncode, 0)
            self._clone_generation_stubs(state)
            self._linux_health_double(release)
            promoted = self._maintenance(release, control_env, "promote", "gen-2")
            self.assertEqual(promoted.returncode, 0, promoted.stdout + promoted.stderr)
            self.assertEqual(self._digest_tree(*roots), digests_before, "source bytes must not change")
            self.assertEqual(
                {
                    str(path): stat.S_IMODE(path.lstat().st_mode)
                    for root in roots
                    for path in sorted(root.rglob("*"))
                },
                modes_before,
                "source modes must not change; nothing outside the private staging is ever chmod-ed",
            )
            receipt_dir = state / "promotions" / "gen-2"
            self.assertFalse((receipt_dir / "backup").exists(), "the plaintext staging must be gone")
            sealed = receipt_dir / "backup.tar.enc"
            manifest = json.loads((receipt_dir / "evidence" / "backup-manifest.json").read_text(encoding="utf-8"))
            archive_bytes = sealed.read_bytes()
            self.assertEqual(hashlib.sha256(archive_bytes).hexdigest(), manifest["encrypted"]["sha256"])
            self.assertIn("handoff/sealed-entry/frozen.dat", manifest["files"])
            self.assertIn("specifications/root-0/sealed-entry/frozen.dat", manifest["files"])
            self.assertIn("artifacts/sealed-entry/frozen.dat", manifest["files"])
            again = run(
                [
                    "python3", str(release / "operator" / "backup.py"),
                    "clean-staging", "--receipt-dir", str(receipt_dir),
                ],
                {},
            )
            self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
            self.assertEqual(again.stdout.strip(), "staging=absent")
            self.assertEqual(sealed.read_bytes(), archive_bytes, "the sealed archive stays byte-identical")
            self.assertEqual(
                (receipt_dir / "evidence" / "backup-staging-clean.out").read_text(encoding="utf-8").strip(),
                "staging=absent",
            )
            rows = json.loads((receipt_dir / "receipt.json").read_text(encoding="utf-8"))["steps"]
            backup_rows = [row for row in rows if row["step"] == "backup"]
            self.assertEqual([row["status"] for row in backup_rows], ["completed"])
            self.assertTrue(
                any(str(path).endswith("backup-staging-removed.out") for path in backup_rows[0]["evidence"])
            )
            self.assertEqual(
                (receipt_dir / "evidence" / "backup-staging-removed.out").read_text(encoding="utf-8").strip(),
                "staging=removed",
            )

    def test_promote_retry_cleans_a_readonly_partial_staging(self) -> None:
        """A failed attempt's leftover staging - copied read-only modes, a stale marker and the
        cp -a target already in place - is removed before the rebuild: the retry succeeds with
        fresh non-nested copies and no stale content in the sealed closure."""

        with tempfile.TemporaryDirectory() as tmp:
            release, control_env, _ = self._prepare(Path(tmp), fresh=False, ready_after=1, db_exists=True)
            self.assertEqual(self._install(release, control_env).returncode, 0)
            state = Path(tmp) / "state"
            control_env["EXPECTED_PLATFORM_DSN"] = self._derived_value(state, "gen-1", "PLATFORM_OPERATOR_DSN")
            self._health_double(release, control_env)
            handoffs = Path(tmp) / "handoffs"
            handoffs.mkdir(exist_ok=True)
            (handoffs / "kept.dat").write_bytes(b"kept")
            self.assertEqual(self._maintenance(release, control_env, "stage", "gen-2").returncode, 0)
            self._clone_generation_stubs(state)
            self._linux_health_double(release)
            receipt_dir = state / "promotions" / "gen-2"
            leftover = receipt_dir / "backup"
            (leftover / "handoff" / "sealed").mkdir(parents=True)
            (leftover / "handoff" / "sealed" / "frozen.dat").write_bytes(b"leftover")
            (leftover / "handoff" / "sealed" / "frozen.dat").chmod(0o444)
            (leftover / "stale-leftover.dat").write_bytes(b"stale")
            (leftover / "stale-leftover.dat").chmod(0o444)
            (leftover / "state" / "install-records").mkdir(parents=True)
            (leftover / "state" / "install-records" / "nested-decoy.json").write_bytes(b"decoy")
            (leftover / "state" / "install-records" / "nested-decoy.json").chmod(0o444)
            (leftover / "state" / "install-records").chmod(0o555)
            (leftover / "handoff" / "sealed").chmod(0o555)
            promoted = self._maintenance(release, control_env, "promote", "gen-2")
            self.assertEqual(promoted.returncode, 0, promoted.stdout + promoted.stderr)
            self.assertFalse((receipt_dir / "backup").exists(), "no leftover staging may survive the retry")
            manifest = json.loads((receipt_dir / "evidence" / "backup-manifest.json").read_text(encoding="utf-8"))
            files = manifest["files"]
            self.assertIn("state/install-records/gen-1.json", files)
            self.assertIn("handoff/kept.dat", files)
            self.assertNotIn("handoff/sealed/frozen.dat", files)
            self.assertFalse([name for name in files if "install-records/install-records" in name])
            self.assertFalse([name for name in files if "stale" in name or "decoy" in name])
            self.assertEqual(
                (receipt_dir / "evidence" / "backup-staging-clean.out").read_text(encoding="utf-8").strip(),
                "staging=removed",
            )
            archive = receipt_dir / "backup.tar.enc"
            self.assertEqual(hashlib.sha256(archive.read_bytes()).hexdigest(), manifest["encrypted"]["sha256"])
            rows = json.loads((receipt_dir / "receipt.json").read_text(encoding="utf-8"))["steps"]
            self.assertEqual({row["status"] for row in rows}, {"completed"})

    def test_promote_refuses_a_symlinked_staging_and_records_backup_failed(self) -> None:
        """Forced cleanup failure: a symlinked staging root is refused, the failure is recorded
        under the backup step with its evidence, the run stops before stop-services, and both
        the admission and the active generation stay untouched."""

        with tempfile.TemporaryDirectory() as tmp:
            release, control_env, order = self._prepare(Path(tmp), fresh=False, ready_after=1, db_exists=True)
            self.assertEqual(self._install(release, control_env).returncode, 0)
            state = Path(tmp) / "state"
            control_env["EXPECTED_PLATFORM_DSN"] = self._derived_value(state, "gen-1", "PLATFORM_OPERATOR_DSN")
            self._health_double(release, control_env)
            self.assertEqual(self._maintenance(release, control_env, "stage", "gen-2").returncode, 0)
            self._clone_generation_stubs(state)
            receipt_dir = state / "promotions" / "gen-2"
            outside = Path(tmp) / "outside-target"
            outside.mkdir()
            (outside / "keep.dat").write_bytes(b"keep")
            (receipt_dir / "backup").symlink_to(outside, target_is_directory=True)
            order.write_text("", encoding="utf-8")
            refused = self._maintenance(release, control_env, "promote", "gen-2")
            self.assertEqual(refused.returncode, 1)
            self.assertIn("could not be cleaned", refused.stderr)
            rows = json.loads((receipt_dir / "receipt.json").read_text(encoding="utf-8"))["steps"]
            self.assertEqual(
                [(row["step"], row["status"]) for row in rows],
                [
                    ("linux-generation", "completed"),
                    ("windows-generation", "completed"),
                    ("close-admission", "completed"),
                    ("drain", "completed"),
                    ("backup", "failed"),
                ],
            )
            failed_row = rows[-1]
            self.assertEqual(failed_row["verb"], "promote")
            evidence = Path(failed_row["evidence"][0])
            self.assertEqual(evidence.name, "backup-staging-clean.out")
            self.assertTrue(evidence.is_file())
            self.assertIn("symbolic link; refused", evidence.read_text(encoding="utf-8"))
            self.assertEqual((Path(tmp) / "admission").read_text().strip(), "true")
            self.assertEqual(
                json.loads((state / "install-record.json").read_text(encoding="utf-8"))["install_id"], "gen-1"
            )
            lines = order.read_text(encoding="utf-8").splitlines()
            self.assertNotIn("services_stop-executors", lines)
            self.assertFalse([line for line in lines if line.startswith("scp ") or "psql_platform_apply" in line])
            self.assertFalse((receipt_dir / "backup.tar.enc").exists(), "no sealed artifact may exist")
            self.assertTrue((receipt_dir / "backup").is_symlink(), "the refusal never touches the link")
            self.assertEqual((outside / "keep.dat").read_bytes(), b"keep")

    def test_promote_start_failure_stays_closed_and_rolls_back(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            release, control_env, _ = self._prepare(Path(tmp), fresh=False, ready_after=1, db_exists=True)
            self.assertEqual(self._install(release, control_env).returncode, 0)
            state = Path(tmp) / "state"
            control_env["EXPECTED_PLATFORM_DSN"] = self._derived_value(state, "gen-1", "PLATFORM_OPERATOR_DSN")
            self._health_double(release, control_env)
            self.assertEqual(self._maintenance(release, control_env, "stage", "gen-2").returncode, 0)
            self._clone_generation_stubs(state)
            failed = self._maintenance(release, dict(control_env, FIXTURE_FAIL_START="1"), "promote", "gen-2")
            self.assertEqual(failed.returncode, 1)
            self.assertIn("did not start", failed.stderr)
            active = json.loads((state / "install-record.json").read_text(encoding="utf-8"))
            self.assertEqual(active["install_id"], "gen-2")
            self.assertEqual((Path(tmp) / "admission").read_text().strip(), "true")
            receipt = json.loads((state / "promotions" / "gen-2" / "receipt.json").read_text(encoding="utf-8"))
            steps = {row["step"]: row["status"] for row in receipt["steps"]}
            self.assertEqual(steps["start-services"], "failed")
            self.assertNotIn("open-admission", steps)
            rolled = self._maintenance(release, control_env, "rollback")
            self.assertEqual(rolled.returncode, 0, rolled.stdout + rolled.stderr)
            restored = json.loads((state / "install-record.json").read_text(encoding="utf-8"))
            self.assertEqual(restored["install_id"], "gen-1")
            self.assertEqual((Path(tmp) / "admission").read_text().strip(), "true")

    def test_ack_loss_records_unknown_and_reconciles_from_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            release, control_env, _ = self._prepare(Path(tmp), fresh=False, ready_after=1, db_exists=True)
            self.assertEqual(self._install(release, control_env).returncode, 0)
            state = Path(tmp) / "state"
            control_env["EXPECTED_PLATFORM_DSN"] = self._derived_value(state, "gen-1", "PLATFORM_OPERATOR_DSN")
            self._health_double(release, control_env)
            self.assertEqual(self._maintenance(release, control_env, "stage", "gen-2").returncode, 0)
            self._clone_generation_stubs(state)
            lost = self._maintenance(release, dict(control_env, FIXTURE_ACK_LOSS="1"), "promote", "gen-2")
            self.assertEqual(lost.returncode, 1)
            self.assertIn("unacknowledged", lost.stderr)
            active = json.loads((state / "install-record.json").read_text(encoding="utf-8"))
            self.assertEqual(active["install_id"], "gen-1")
            self.assertEqual((Path(tmp) / "admission").read_text().strip(), "true")
            receipt = json.loads((state / "promotions" / "gen-2" / "receipt.json").read_text(encoding="utf-8"))
            steps = {row["step"]: row["status"] for row in receipt["steps"]}
            self.assertEqual(steps["windows-switch"], "unknown")
            reconciled = self._maintenance(release, control_env, "rollback", "gen-2")
            self.assertEqual(reconciled.returncode, 0, reconciled.stdout + reconciled.stderr)
            self.assertEqual(
                json.loads((state / "install-record.json").read_text(encoding="utf-8"))["install_id"], "gen-1"
            )
            self.assertEqual((Path(tmp) / "admission").read_text().strip(), "true")
            final = json.loads((state / "promotions" / "gen-2" / "receipt.json").read_text(encoding="utf-8"))
            rolled = {row["step"]: row["status"] for row in final["steps"] if row["verb"] == "rollback"}
            self.assertEqual(rolled["rollback-windows"], "completed")
            self.assertEqual(rolled["rollback-linux"], "completed")
            # The accepted remote switch is actually restored: exactly one bound rollback call.
            lines = Path(tmp, "order.log").read_text(encoding="utf-8").splitlines()
            rollback_calls = [line for line in lines if line.startswith("ssh ") and "-Verb rollback" in line]
            self.assertEqual(len(rollback_calls), 1, lines[-8:])
            self.assertIn("previous-0001", rollback_calls[0])
            self.assertEqual(
                len([line for line in lines if line.startswith("ssh ") and "-Verb status" in line]), 1
            )

    def test_foreign_live_launcher_stays_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            release, control_env, _ = self._prepare(Path(tmp), fresh=False, ready_after=1, db_exists=True)
            self.assertEqual(self._install(release, control_env).returncode, 0)
            state = Path(tmp) / "state"
            control_env["EXPECTED_PLATFORM_DSN"] = self._derived_value(state, "gen-1", "PLATFORM_OPERATOR_DSN")
            self._health_double(release, control_env)
            self.assertEqual(self._maintenance(release, control_env, "stage", "gen-2").returncode, 0)
            self._clone_generation_stubs(state)
            lost = self._maintenance(release, dict(control_env, FIXTURE_ACK_LOSS="1"), "promote", "gen-2")
            self.assertEqual(lost.returncode, 1)
            # The endpoint runs a foreign launcher: neither the target nor the bound source.
            foreign = run(
                [
                    "bash", str(release / "operator" / "operatorctl.sh"), "rollback",
                    "--config", str(release.parent / "platform.json"), "--install-id", "gen-2",
                ],
                dict(control_env, FIXTURE_STATUS="foreign"),
            )
            self.assertEqual(foreign.returncode, 1)
            self.assertIn("not provably recoverable", foreign.stderr)
            receipt = json.loads((state / "promotions" / "gen-2" / "receipt.json").read_text(encoding="utf-8"))
            steps = {row["step"]: row["status"] for row in receipt["steps"] if row["verb"] == "rollback"}
            self.assertEqual(steps.get("rollback-windows"), "unknown")
            lines = Path(tmp, "order.log").read_text(encoding="utf-8").splitlines()
            self.assertFalse([line for line in lines if line.startswith("ssh ") and "-Verb rollback" in line])

    def test_config_upgrade_flows_through_stage_and_promote(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            release, control_env, _ = self._prepare(Path(tmp), fresh=False, ready_after=1, db_exists=True)
            self.assertEqual(self._install(release, control_env).returncode, 0)
            state = Path(tmp) / "state"
            tree = release.parent
            # A legitimate configuration upgrade: the candidate describes new storage roots; the
            # running generation keeps its own snapshot and the maintenance window flows.
            document = json.loads((tree / "platform.json").read_text(encoding="utf-8"))
            document["storage"]["handoff_root"] = str(Path(tmp) / "handoffs-next")
            document["storage"]["artifact_root"] = str(Path(tmp) / "artifacts-next")
            (tree / "platform.json").write_text(json.dumps(document), encoding="utf-8")
            control_env["EXPECTED_PLATFORM_DSN"] = self._derived_value(state, "gen-1", "PLATFORM_OPERATOR_DSN")
            self._health_double(release, control_env)
            staged = self._maintenance(release, control_env, "stage", "gen-2")
            self.assertEqual(staged.returncode, 0, staged.stdout + staged.stderr)
            candidate = json.loads((state / "snapshots" / "gen-2.json").read_text(encoding="utf-8"))
            self.assertEqual(
                candidate["resolved"]["storage"]["handoff_root"], str(Path(tmp) / "handoffs-next")
            )
            self.assertEqual(
                json.loads((state / "install-record.json").read_text(encoding="utf-8"))["install_id"], "gen-1"
            )
            self._clone_generation_stubs(state)
            self._linux_health_double(release)
            promoted = self._maintenance(release, control_env, "promote", "gen-2")
            self.assertEqual(promoted.returncode, 0, promoted.stdout + promoted.stderr)
            self.assertEqual(
                json.loads((state / "install-record.json").read_text(encoding="utf-8"))["install_id"], "gen-2"
            )
            verified = run(
                [
                    "python3", str(release / "operator" / "install_record.py"), "verify-active",
                    "--config", str(tree / "platform.json"), "--state-root", str(state),
                ],
                {},
            )
            self.assertEqual(verified.returncode, 0, verified.stdout + verified.stderr)

    def test_ack_loss_with_live_before_state_records_never_switched(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            release, control_env, _ = self._prepare(Path(tmp), fresh=False, ready_after=1, db_exists=True)
            self.assertEqual(self._install(release, control_env).returncode, 0)
            state = Path(tmp) / "state"
            control_env["EXPECTED_PLATFORM_DSN"] = self._derived_value(state, "gen-1", "PLATFORM_OPERATOR_DSN")
            self._health_double(release, control_env)
            self.assertEqual(self._maintenance(release, control_env, "stage", "gen-2").returncode, 0)
            self._clone_generation_stubs(state)
            lost = self._maintenance(release, dict(control_env, FIXTURE_ACK_LOSS="1"), "promote", "gen-2")
            self.assertEqual(lost.returncode, 1)
            reconciled = self._maintenance(
                release, dict(control_env, FIXTURE_STATUS="before"), "rollback", "gen-2"
            )
            self.assertEqual(reconciled.returncode, 0, reconciled.stdout + reconciled.stderr)
            self.assertEqual(
                json.loads((state / "install-record.json").read_text(encoding="utf-8"))["install_id"], "gen-1"
            )
            final = json.loads((state / "promotions" / "gen-2" / "receipt.json").read_text(encoding="utf-8"))
            rolled = {row["step"]: row["status"] for row in final["steps"] if row["verb"] == "rollback"}
            self.assertEqual(rolled["rollback-windows"], "completed")
            lines = Path(tmp, "order.log").read_text(encoding="utf-8").splitlines()
            self.assertFalse(
                [line for line in lines if line.startswith("ssh ") and "-Verb rollback" in line],
                "a provably unswitched endpoint must not receive a rollback",
            )

    def test_not_ready_postgres_fails_before_migration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            release, control_env, order = self._prepare(Path(tmp), fresh=True, ready_after=0)
            result = self._install(release, control_env)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("PostgreSQL did not become ready", result.stderr)
            lines = order.read_text(encoding="utf-8").splitlines() if order.exists() else []
            self.assertNotIn("airflow_install", lines)
            self.assertNotIn("psql_db_query", lines)
            self.assertNotIn("createdb", lines)
            attempts = [line for line in lines if line.startswith("pg_isready ")]
            self.assertEqual(len(attempts), 20, lines)

    def test_database_service_failure_exits_before_migration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            release, control_env, order = self._prepare(Path(tmp), fresh=False, ready_after=1, start_rc=1)
            result = self._install(release, control_env)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("description-postgres.service failed to start", result.stderr)
            lines = order.read_text(encoding="utf-8").splitlines()
            self.assertNotIn("pg_isready 1", lines)
            self.assertNotIn("psql_db_query", lines)
            self.assertNotIn("createdb", lines)
            self.assertNotIn("airflow_install", lines)

    def test_database_query_failure_exits_before_migration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            release, control_env, order = self._prepare(Path(tmp), fresh=False, ready_after=1, psql_query_rc=1)
            result = self._install(release, control_env)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("could not query pg_database", result.stderr)
            lines = order.read_text(encoding="utf-8").splitlines()
            self.assertNotIn("createdb", lines)
            self.assertNotIn("airflow_install", lines)

    def test_database_creation_failure_exits_before_migration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            release, control_env, order = self._prepare(
                Path(tmp), fresh=True, ready_after=1, db_exists=False, createdb_rc=1
            )
            result = self._install(release, control_env)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("could not create the airflow_meta database", result.stderr)
            lines = order.read_text(encoding="utf-8").splitlines()
            self.assertIn("createdb", lines)
            self.assertNotIn("airflow_install", lines)

    def _digest_tree(self, *roots: Path) -> dict[str, str]:
        snapshot = {}
        for root in roots:
            if root.exists():
                for path in sorted(root.rglob("*")):
                    if path.is_file():
                        snapshot[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
        return snapshot

    def test_preflight_conflicts_write_nothing(self) -> None:
        for case in ("endpoint_triple", "rogue_wheel"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                release, control_env, order = self._prepare(Path(tmp), fresh=True, ready_after=1)
                tree = release.parent
                identity = tree / "endpoint-identity.json"
                args = [
                    "bash",
                    str(release / "operator" / "operatorctl.sh"),
                    "install",
                    "--config",
                    str(tree / "platform.json"),
                    "--install-id",
                    "gen-1",
                    "--endpoint-identity",
                    str(identity),
                ]
                if case == "endpoint_triple":
                    document = json.loads(identity.read_text(encoding="utf-8"))
                    document["release"]["source_commit"] = "e" * 40
                    identity.write_text(json.dumps(document), encoding="utf-8")
                else:
                    rogue = Path(tmp) / "unrelated" / "mimicverse_description-1.4.0-py3-none-any.whl"
                    rogue.parent.mkdir()
                    rogue.write_bytes(b"UNINDEXED-ROGUE-WHEEL")
                    args.extend(["--wheel", str(rogue)])
                state = Path(tmp) / "state"
                config_home = Path(control_env["XDG_CONFIG_HOME"])
                before = self._digest_tree(state, config_home)
                result = run(args, control_env)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(
                    self._digest_tree(state, config_home),
                    before,
                    "a conflicted preflight must not write state or unit files",
                )
                self.assertFalse((state / "install-record.json").exists())
                self.assertFalse(order.exists(), "no installation leg may run")


class BackupStagingCleanupUnitTests(unittest.TestCase):
    """backup.py clean-staging (unit-level): the single private-staging cleanup owner.

    The scripted end-to-end promote paths that consume it live in InstallOrderingTests."""

    CONTROL = OPERATOR / "backup.py"

    @staticmethod
    def _snapshot(root: Path) -> dict[str, object]:
        snapshot: dict[str, object] = {}
        for current, dirnames, filenames in os.walk(root, followlinks=False):
            for name in sorted(dirnames + filenames):
                path = Path(current) / name
                relative = path.relative_to(root).as_posix()
                mode = stat.S_IMODE(path.lstat().st_mode)
                if path.is_symlink():
                    snapshot[relative] = (mode, "link", os.readlink(path))
                elif path.is_dir():
                    snapshot[relative] = (mode, "dir")
                else:
                    snapshot[relative] = (mode, "file", hashlib.sha256(path.read_bytes()).hexdigest())
        return snapshot

    def _clean(self, receipt: Path) -> subprocess.CompletedProcess:
        return run([sys.executable, str(self.CONTROL), "clean-staging", "--receipt-dir", str(receipt)], {})

    def _fixture(self, tmp: Path) -> tuple[Path, Path]:
        """A promotion receipt directory with a read-only private staging (0555/0444 copies,
        a zero-mode nested directory and a symlink entry) and an outside tree the symlink
        points at."""

        receipt = tmp / "receipt"
        (receipt / "evidence").mkdir(parents=True)
        (receipt / "receipt.json").write_text('{"fixture": true}\n', encoding="utf-8")
        (receipt / "backup.tar.enc").write_bytes(b"sealed-archive-bytes")
        (receipt / "evidence" / "backup-manifest.json").write_bytes(b"manifest-bytes")
        outside = tmp / "outside"
        (outside / "keep").mkdir(parents=True)
        (outside / "keep" / "keep.dat").write_bytes(b"keep-me")
        (outside / "keep" / "keep.dat").chmod(0o444)
        (outside / "keep").chmod(0o555)
        staging = receipt / "backup"
        (staging / "sealed" / "deeper").mkdir(parents=True)
        (staging / "sealed" / "frozen.dat").write_bytes(b"frozen")
        (staging / "sealed" / "deeper" / "nested.dat").write_bytes(b"nested")
        (staging / "stale.dat").write_bytes(b"stale")
        (staging / "escape-link").symlink_to(outside, target_is_directory=True)
        (staging / "sealed" / "frozen.dat").chmod(0o444)
        (staging / "sealed" / "deeper").chmod(0o000)
        (staging / "sealed").chmod(0o555)
        (staging / "stale.dat").chmod(0o444)
        return receipt, outside

    def test_removes_the_readonly_staging_and_preserves_everything_else(self) -> None:
        """The read-only staging is removed; the receipt's siblings, the evidence and the
        symlink's target stay byte- and mode-identical, and a second run is a clean no-op."""

        with tempfile.TemporaryDirectory() as tmp:
            receipt, outside = self._fixture(Path(tmp))
            tracked = {
                "receipt": receipt / "receipt.json",
                "archive": receipt / "backup.tar.enc",
                "manifest": receipt / "evidence" / "backup-manifest.json",
            }
            digests_before = {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in tracked.items()}
            modes_before = {name: stat.S_IMODE(path.lstat().st_mode) for name, path in tracked.items()}
            outside_before = self._snapshot(outside)
            cleaned = self._clean(receipt)
            self.assertEqual(cleaned.returncode, 0, cleaned.stdout + cleaned.stderr)
            self.assertEqual(cleaned.stdout.strip(), "staging=removed")
            self.assertFalse((receipt / "backup").exists())
            self.assertEqual(
                {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in tracked.items()},
                digests_before,
            )
            self.assertEqual({name: stat.S_IMODE(path.lstat().st_mode) for name, path in tracked.items()}, modes_before)
            self.assertEqual(self._snapshot(outside), outside_before, "the symlink target must be untouched")
            second = self._clean(receipt)
            self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
            self.assertEqual(second.stdout.strip(), "staging=absent")

    def test_refuses_symlinked_mistyped_and_unmarked_targets(self) -> None:
        """Unsafe roots are refused instead of resolved: a symlinked staging, a non-directory
        staging, a directory without the receipt marker and a relative receipt path."""

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            outside = tmp / "outside"
            outside.mkdir()
            (outside / "keep.dat").write_bytes(b"keep")
            receipt = tmp / "receipt"
            receipt.mkdir()
            (receipt / "receipt.json").write_text("{}\n", encoding="utf-8")
            (receipt / "backup").symlink_to(outside, target_is_directory=True)
            refused = self._clean(receipt)
            self.assertEqual(refused.returncode, 1)
            self.assertIn("symbolic link; refused", refused.stderr)
            self.assertTrue((receipt / "backup").is_symlink())
            self.assertEqual((outside / "keep.dat").read_bytes(), b"keep")
            (receipt / "backup").unlink()
            (receipt / "backup").write_bytes(b"not-a-staging")
            refused = self._clean(receipt)
            self.assertEqual(refused.returncode, 1)
            self.assertIn("not a directory; refused", refused.stderr)
            self.assertEqual((receipt / "backup").read_bytes(), b"not-a-staging")
            unmarked = tmp / "unmarked"
            (unmarked / "backup").mkdir(parents=True)
            (unmarked / "backup" / "content.dat").write_bytes(b"content")
            refused = self._clean(unmarked)
            self.assertEqual(refused.returncode, 1)
            self.assertIn("not a promotion receipt directory", refused.stderr)
            self.assertTrue((unmarked / "backup" / "content.dat").is_file())
            refused = self._clean(Path("relative-receipt"))
            self.assertEqual(refused.returncode, 1)
            self.assertIn("absolute receipt directory", refused.stderr)

    def test_refuses_symlinked_ancestors_and_traversal_receipts(self) -> None:
        """The PR #61 boundary probes: <tmp>/alias/receipt (symlinked ancestor), the
        <receipt>/child/.. traversal form and a self-loop receipt are all refused with the
        whole target intact, while the canonical spelling still cleans."""

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            receipt = tmp / "real" / "receipt"
            (receipt / "backup" / "sealed" / "deeper").mkdir(parents=True)
            (receipt / "receipt.json").write_text("{}\n", encoding="utf-8")
            (receipt / "backup" / "sealed" / "deeper" / "frozen.dat").write_bytes(b"frozen")
            (receipt / "backup" / "sealed" / "deeper" / "frozen.dat").chmod(0o444)
            (receipt / "backup" / "sealed" / "deeper").chmod(0o555)
            (receipt / "backup" / "sealed").chmod(0o555)
            (receipt / "child").mkdir()
            target_before = self._snapshot(receipt)
            alias = tmp / "alias"
            alias.symlink_to(tmp / "real", target_is_directory=True)
            refused = self._clean(alias / "receipt")
            self.assertEqual(refused.returncode, 1)
            self.assertIn("canonical", refused.stderr)
            self.assertEqual(self._snapshot(receipt), target_before, "the aliased target must be untouched")
            refused = self._clean(receipt / "child" / "..")
            self.assertEqual(refused.returncode, 1)
            self.assertIn("canonical", refused.stderr)
            self.assertEqual(self._snapshot(receipt), target_before, "the traversal form must remove nothing")
            loop = tmp / "loop"
            loop.symlink_to("loop")
            refused = self._clean(loop)
            self.assertEqual(refused.returncode, 1)
            self.assertIn("symbolic link component", refused.stderr)
            self.assertTrue(loop.is_symlink())
            cleaned = self._clean(receipt)
            self.assertEqual(cleaned.returncode, 0, cleaned.stdout + cleaned.stderr)
            self.assertEqual(cleaned.stdout.strip(), "staging=removed")


class InstallPostgresBehaviorTests(unittest.TestCase):
    """install_postgres.sh provisions only: no server lifecycle and no database creation."""

    def _fixture(self, tmp: Path, *, preexisting: bool) -> tuple[Path, dict[str, str], Path]:
        pgroot = tmp / "pg"
        bindir = pgroot / "root" / "usr" / "lib" / "postgresql" / "14" / "bin"
        bindir.mkdir(parents=True)
        write_stub(
            bindir / "postgres",
            '#!/usr/bin/env bash\n[ "${1:-}" = "--version" ] && echo "postgres (PostgreSQL) 14.0"\nexit 0\n',
        )
        write_stub(
            bindir / "initdb",
            "#!/usr/bin/env bash\n"
            'data=""\n'
            'while [ $# -gt 0 ]; do case "$1" in -D) data="$2"; shift 2 ;; *) shift ;; esac; done\n'
            'mkdir -p "$data"\n: > "$data/PG_VERSION"\n: > "$data/postgresql.conf"\n: > "$data/pg_hba.conf"\n'
            'echo "initdb" >> "$ORDER_LOG"\n',
        )
        for command in ("pg_ctl", "createdb", "psql"):
            write_stub(bindir / command, f'#!/usr/bin/env bash\necho "{command}" >> "$ORDER_LOG"\nexit 0\n')
        if preexisting:
            data = pgroot / "data"
            data.mkdir(parents=True)
            (data / "PG_VERSION").write_text("14\n", encoding="utf-8")
            (data / "postgresql.conf").write_text("", encoding="utf-8")
            (data / "pg_hba.conf").write_text("", encoding="utf-8")
        pathbin = tmp / "pathbin"
        pathbin.mkdir()
        write_stub(pathbin / "apt-get", "#!/usr/bin/env bash\n: > dummy.deb\nexit 0\n")
        write_stub(pathbin / "dpkg-deb", "#!/usr/bin/env bash\nexit 0\n")
        write_stub(pathbin / "ldd", "#!/usr/bin/env bash\nexit 0\n")
        order = tmp / "order.log"
        env = {
            "PATH": f"{pathbin}:{os.environ.get('PATH', '/usr/bin:/bin')}",
            "POSTGRES_ROOT": str(pgroot),
            "POSTGRES_MAJOR": "14",
            "POSTGRES_PORT": "5433",
            "ORDER_LOG": str(order),
        }
        return order, env, pgroot

    def _run(self, env: dict[str, str]):
        script = ROOT / "deploy" / "airflow" / "scripts" / "install_postgres.sh"
        return run(["bash", str(script)], env)

    def _assert_provisioned_only(self, order: Path) -> list[str]:
        lines = order.read_text(encoding="utf-8").splitlines() if order.is_file() else []
        self.assertFalse([line for line in lines if line.startswith(("pg_ctl", "createdb", "psql"))], lines)
        return lines

    def test_fresh_provision_writes_cluster_and_hardening_without_a_server(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            order, env, pgroot = self._fixture(Path(tmp), preexisting=False)
            result = self._run(env)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            lines = self._assert_provisioned_only(order)
            self.assertIn("initdb", lines)
            self.assertTrue((pgroot / "data" / "PG_VERSION").is_file())
            conf = (pgroot / "data" / "postgresql.conf").read_text(encoding="utf-8")
            self.assertIn("# >>> description-postgres", conf)
            self.assertIn("reject", (pgroot / "data" / "pg_hba.conf").read_text(encoding="utf-8"))
            self.assertIn("AIRFLOW_DB_URL=postgresql+psycopg2://airflow_migrate@/airflow_meta", result.stdout)
            self.assertFalse((pgroot / "data" / ".running").exists())

    def test_existing_cluster_is_rehardened_without_server_or_database_calls(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            order, env, pgroot = self._fixture(Path(tmp), preexisting=True)
            result = self._run(env)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            lines = self._assert_provisioned_only(order)
            self.assertNotIn("initdb", lines)
            conf = (pgroot / "data" / "postgresql.conf").read_text(encoding="utf-8")
            self.assertIn("# >>> description-postgres", conf)
            self.assertFalse((pgroot / "data" / ".running").exists())


class NativePoolTests(unittest.TestCase):
    """`windows_native` (capacity 1) exists after install and is a health item.

    Contract: contracts.md §六阶段契约 (native operations use a capacity-1 pool;
    waiting for native results releases Airflow worker slots) and frozen
    interface IF-02.
    """

    SCRIPT = ROOT / "deploy" / "airflow" / "scripts" / "ensure_pool.py"
    INSTALL = ROOT / "deploy" / "airflow" / "install.sh"

    class _NameSlot:
        """Descriptor so ``pool_class.pool == name`` evaluates to a named tuple."""

        def __get__(self, instance, owner):
            return NativePoolTests._NameSlot()

        def __eq__(self, other):
            return ("name", other)

    class _FakePool:
        pool = None

        def __init__(self, *, pool, slots, description, include_deferred=False):
            self.pool = pool
            self.slots = slots
            self.description = description
            self.include_deferred = include_deferred

    class _FakeQuery:
        def __init__(self, store, persist):
            self._store = store
            self._persist = persist
            self._condition = None

        def filter(self, condition):
            self._condition = condition
            return self

        def one_or_none(self):
            if not self._persist:
                return None
            _kind, name = self._condition
            return self._store.get(name)

    class _FakeSession:
        def __init__(self, store, persist=True):
            self._store = store
            self._persist = persist

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def query(self, pool_class):
            return NativePoolTests._FakeQuery(self._store, self._persist)

        def add(self, pool):
            if self._persist:
                self._store[pool.pool] = pool

        def commit(self):
            return None

    _FakePool.pool = _NameSlot()

    def _load(self):
        spec = importlib.util.spec_from_file_location("ensure_pool_under_test", self.SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def _session(self, store, persist=True):
        return NativePoolTests._FakeSession(store, persist=persist)

    def test_defaults_follow_the_stage_contract(self) -> None:
        module = self._load()
        self.assertEqual(module.DEFAULT_NAME, "windows_native")
        self.assertEqual(module.DEFAULT_SLOTS, 1)

    def test_missing_pool_is_created_with_capacity_one(self) -> None:
        module = self._load()
        store: dict = {}
        result = module.ensure_pool(
            "windows_native",
            1,
            "desc",
            session_factory=lambda: self._session(store),
            pool_class=self._FakePool,
        )
        self.assertEqual(result, {"name": "windows_native", "slots": 1, "created": True})
        self.assertEqual(store["windows_native"].slots, 1)
        self.assertEqual(store["windows_native"].description, "desc")
        self.assertFalse(store["windows_native"].include_deferred)

    def test_existing_pool_keeps_other_fields_and_enforces_capacity(self) -> None:
        module = self._load()
        pool = self._FakePool(pool="windows_native", slots=4, description="keep-me", include_deferred=True)
        store = {"windows_native": pool}
        result = module.ensure_pool(
            "windows_native",
            1,
            "ignored",
            session_factory=lambda: self._session(store),
            pool_class=self._FakePool,
        )
        self.assertFalse(result["created"])
        self.assertEqual(pool.slots, 1)
        self.assertEqual(pool.description, "keep-me")
        self.assertTrue(pool.include_deferred)

    def test_existing_pool_at_capacity_is_a_noop(self) -> None:
        module = self._load()
        pool = self._FakePool(pool="windows_native", slots=1, description="keep-me", include_deferred=True)
        store = {"windows_native": pool}
        result = module.ensure_pool(
            "windows_native",
            1,
            "ignored",
            session_factory=lambda: self._session(store),
            pool_class=self._FakePool,
        )
        self.assertFalse(result["created"])
        self.assertEqual((pool.slots, pool.description, pool.include_deferred), (1, "keep-me", True))

    def test_failed_verification_exits_nonzero(self) -> None:
        module = self._load()
        with self.assertRaises(SystemExit):
            module.ensure_pool(
                "windows_native",
                1,
                "desc",
                session_factory=lambda: self._session({}, persist=False),
                pool_class=self._FakePool,
            )

    def test_install_runs_the_ensure_step_after_migration(self) -> None:
        text = self.INSTALL.read_text(encoding="utf-8")
        self.assertIn("scripts/ensure_pool.py", text)
        self.assertLess(text.index("db migrate"), text.index("scripts/ensure_pool.py"))

    def test_health_battery_requires_the_pool(self) -> None:
        text = HEALTH.read_text(encoding="utf-8")
        self.assertIn("windows_native", text)
        self.assertIn("native pool windows_native is missing or its capacity is not 1", text)

    def test_script_help_runs_without_airflow(self) -> None:
        result = run([sys.executable, str(self.SCRIPT), "--help"], {})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class ScheduledSmokePreparationTests(unittest.TestCase):
    """R02: the scheduled smoke prepares through the production pool entry and
    verifies the capacity-1 pool's reschedule slot-release semantics.

    The real scheduler run stays opt-in (RUN_SCHEDULED_AIRFLOW=1); these guards
    keep the preparation and the sensor mode from regressing silently.
    """

    SMOKE = ROOT / "deploy" / "airflow" / "scripts" / "scheduled_smoke.py"

    def test_pool_preparation_uses_the_production_entry(self) -> None:
        text = self.SMOKE.read_text(encoding="utf-8")
        self.assertIn("ensure_pool.py", text)
        self.assertLess(text.index('"db", "migrate"'), text.index("ensure_pool.py"))
        # No bypass: the smoke must not fall back to the default pool for the native stage.
        self.assertNotIn("default_pool", text)

    def test_smoke_pins_reschedule_and_has_no_poke_override(self) -> None:
        text = self.SMOKE.read_text(encoding="utf-8")
        self.assertIn('SENSOR_MODE_REQUIRED = "reschedule"', text)
        self.assertIn("SOLIDWORKS_SENSOR_MODE=SENSOR_MODE_REQUIRED", text)
        self.assertNotIn('SOLIDWORKS_SENSOR_MODE="poke"', text)

    def test_smoke_uses_only_the_current_six_task_ids(self) -> None:
        text = self.SMOKE.read_text(encoding="utf-8")
        self.assertNotIn("wait_for_job", text)
        self.assertNotIn("confirm_job", text)

    def test_smoke_requires_raw_task_reschedule_evidence(self) -> None:
        text = self.SMOKE.read_text(encoding="utf-8")
        self.assertIn("TaskReschedule", text)
        self.assertIn('"reschedule_evidence"', text)
        self.assertIn("up_for_reschedule", text)
        self.assertIn("not proven", text)


if __name__ == "__main__":
    unittest.main()
