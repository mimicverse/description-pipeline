"""Operator deployment renderer/lifecycle tests (stdlib only; external services are mocked)."""

from __future__ import annotations

import contextlib
import copy
import fcntl
import hashlib
import importlib.util
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile
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
        "CONTROL_PYTHON": sys.executable,
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
    dag_content = b"# dag fixture\n"
    release_document = {
        "schema_version": "solidworks-to-urdf.release/v1",
        "pipeline_id": "solidworks-to-urdf",
        "version": "1.4.0",
        "source_commit": "c" * 40,
        "source_tree": "d" * 40,
        "source_sha256": "a" * 64,
        "artifacts": {
            wheel.name: hashlib.sha256(wheel.read_bytes()).hexdigest(),
            "description-1.4.0-windows-cp312-x86_64.zip": hashlib.sha256(b"windows-archive").hexdigest(),
            "description-1.4.0-linux-cp312-x86_64.zip": hashlib.sha256(b"linux-archive").hexdigest(),
            "description-1.4.0-deployment.zip": hashlib.sha256(b"deployment-archive").hexdigest(),
        },
        "deployment_files": {
            "deploy/airflow/dags/solidworks_to_urdf.py": hashlib.sha256(dag_content).hexdigest(),
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
    fixture_ssh = tmp / "bin" / "ssh"
    write_stub(
        fixture_ssh,
        r'''#!/usr/bin/env bash
root="$(cd "$(dirname "$0")/.." && pwd)"
stdin_script="$(cat; printf x)"
stdin_script="${stdin_script%x}"
python3 - "$root" "$stdin_script" "$@" <<'PY'
import base64
import gzip
import json
import re
import sys
from pathlib import Path

root = Path(sys.argv[1])
stdin_script = sys.argv[2]
arguments = sys.argv[3:]
override = root / "windows-status.json"
if override.is_file():
    sys.stdout.write(override.read_text(encoding="utf-8").strip())
    sys.stdout.write("\n")
    stderr_path = root / "windows-status.stderr"
    if stderr_path.is_file():
        sys.stderr.buffer.write(stderr_path.read_bytes())
    rc_path = root / "windows-status.rc"
    raise SystemExit(int(rc_path.read_text(encoding="ascii")) if rc_path.is_file() else 0)
script = stdin_script
if "-EncodedCommand" in arguments:
    encoded = arguments[arguments.index("-EncodedCommand") + 1]
    launcher = base64.b64decode(encoded).decode("utf-16le")
    if (
        "System.Byte[] 8192" not in launcher
        or ".Read($buffer,0,$buffer.Length)" not in launcher
        or "$chunk.IndexOf([char]10)" not in launcher
        or "ReadLine()" in launcher
        or "FromBase64String('" in launcher
    ):
        raise SystemExit(0)
    if stdin_script.count("\n") != 1 or not stdin_script.endswith("\n"):
        raise SystemExit(3)
    script = gzip.decompress(base64.b64decode(stdin_script)).decode("utf-8")
match = re.search(r"FromBase64String\('([^']+)'\)", script)
if match is None:
    raise SystemExit(2)
expected = json.loads(base64.b64decode(match.group(1)).decode())
contract = (
    "legacy_pip_launcher_v0"
    if expected["release"]["source_commit"] == "55cdf8da178e37239d3ac199cd27fc50cf56540d"
    else "python_module_v1"
)
sample = {
    "task_name": expected["task_name"],
    "task_state": "Running",
    "action_count": 1,
    "last_task_result": 267009,
    "listener_pid": 4242,
    "process_start_utc": "2026-10-21T00:00:00Z",
    "install_id": expected["install_id"],
    "config_digest": expected["config_digest"],
    "release": expected["release"],
    "health_install_id": expected["install_id"],
    "health_config_digest": expected["config_digest"],
    "health_release": expected["release"],
    "launch_contract": contract,
    "action_matches_generation": True,
}
print(json.dumps({"passed": True, "sample": sample}, sort_keys=True))
PY
''',
    )
    with Path(env_path).open("a", encoding="utf-8") as stream:
        stream.write(
            "SOLIDWORKS_SSH_BIN="
            + encode_env_value(str(fixture_ssh))
            + "\n"
        )
    env["SOLIDWORKS_SSH_BIN"] = str(fixture_ssh)
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
                "install_id": "win-" + "1" * 32,
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
    dag_path = tmp / "deploy" / "airflow" / "dags" / "solidworks_to_urdf.py"
    dag_path.parent.mkdir(parents=True, exist_ok=True)
    dag_path.write_bytes(dag_content)
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
            "--deployment-root",
            str(tmp),
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


def status_env(tmp: Path, config_home: Path) -> dict[str, str]:
    """A read-only status environment with every managed unit explicitly active."""

    bindir = tmp / "bin"
    bindir.mkdir(exist_ok=True)
    state_dir = tmp / "status-unit-state"
    state_dir.mkdir(exist_ok=True)
    for unit in (
        "description-postgres",
        "description-solidworks-tunnel",
        "description-airflow-dag-processor",
        "description-airflow-scheduler",
        "description-airflow-api-server",
        "description-portal",
        "description-operator-proxy",
    ):
        (state_dir / unit).write_text("active\n", encoding="utf-8")
    systemctl = bindir / "status-systemctl"
    write_stub(
        systemctl,
        '#!/usr/bin/env bash\n'
        '[ "${1:-}" = "--user" ] && [ "${2:-}" = "is-active" ] || exit 2\n'
        'cat "$STATUS_UNIT_STATE_DIR/${3:-}" 2>/dev/null || echo unknown\n',
    )
    return {
        "XDG_CONFIG_HOME": str(config_home),
        "SYSTEMCTL_BIN": str(systemctl),
        "STATUS_UNIT_STATE_DIR": str(state_dir),
    }


class RenderTests(unittest.TestCase):
    def test_linux_discovery_settings_are_rendered_and_changes_are_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            settings = root / "discovery.json"
            expected = {"record_roots": [str(root / "records")]}
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
        for name in ("airflow", "platform", "windows"):
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
        write_stub(
            bindir / "scp",
            '#!/bin/bash\n'
            'echo "scp $*" >> "$ORDER_LOG"\n'
            'src=""\n'
            'for argument in "$@"; do case "$argument" in *.zip) src="$argument" ;; esac; done\n'
            'if [ -n "${FIXTURE_REMOTE_ARCHIVE:-}" ] && [ -n "$src" ]; then\n'
            '  mkdir -p "$(dirname "$FIXTURE_REMOTE_ARCHIVE")"\n'
            '  cp "$src" "$FIXTURE_REMOTE_ARCHIVE"\n'
            '  chmod 0444 "$FIXTURE_REMOTE_ARCHIVE"\n'
            'fi\n'
            'exit 0\n',
        )
        write_stub(
            bindir / "ssh",
            '#!/bin/bash\n'
            'stdin_payload="$(cat; printf x)"\n'
            'stdin_payload="${stdin_payload%x}"\n'
            'decoded_script="$(printf "%s" "$stdin_payload" | python3 -c \''
            'import base64,gzip,sys\n'
            'raw=sys.stdin.buffer.read()\n'
            'if raw and (raw.count(b"\\n") != 1 or not raw.endswith(b"\\n")): raise SystemExit(3)\n'
            'd=raw.strip()\n'
            'print(gzip.decompress(base64.b64decode(d)).decode("utf-8")'
            '.replace(chr(92), chr(47)) if d else "")'
            '\' 2>/dev/null || true)"\n'
            'echo "ssh $* $(printf "%s" "$decoded_script" | tr "\\n" " ")" >> "$ORDER_LOG"\n'
            'case "$* $decoded_script" in\n'
            '  *"two status probes returned different"*)\n'
            '    python3 - "$FIXTURE_OPERATOR_STATE/install-record.json" <<\'PY\'\n'
            "import json\n"
            "import os\n"
            "import sys\n"
            "record = json.load(open(sys.argv[1], encoding='utf-8'))\n"
            "identity = record['ends']['windows']\n"
            "release = identity['release']\n"
            "sample = {\n"
            "    'task_name': 'description-e2e-endpoint',\n"
            "    'task_state': 'Running',\n"
            "    'action_count': 1,\n"
            "    'listener_pid': 4242,\n"
            "    'process_start_utc': '2026-10-21T00:00:00Z',\n"
            "    'install_id': identity['install_id'],\n"
            "    'config_digest': identity['config_digest'],\n"
            "    'release': release,\n"
            "    'health_install_id': identity['install_id'],\n"
            "    'health_config_digest': identity['config_digest'],\n"
            "    'health_release': release,\n"
            "    'launch_contract': 'python_module_v1',\n"
            "    'action_matches_generation': True,\n"
            "}\n"
            "if os.environ.get('FIXTURE_STATUS') == 'foreign':\n"
            "    sample['install_id'] = 'win-' + 'f' * 32\n"
            "    sample['health_install_id'] = sample['install_id']\n"
            "print(json.dumps({'passed': True, 'sample': sample}, sort_keys=True))\n"
            "PY\n"
            "    ;;\n"
            '  *"Get-FileHash"*)\n'
            '    if [ -n "${FIXTURE_REMOTE_ARCHIVE:-}" ] && [ -f "$FIXTURE_REMOTE_ARCHIVE" ]; then\n'
            '      printf "%s|%s\\n" "$(stat -c %s "$FIXTURE_REMOTE_ARCHIVE")" \\\n'
            '        "$(sha256sum "$FIXTURE_REMOTE_ARCHIVE" | cut -d" " -f1)"\n'
            '    else echo absent; fi ;;\n'
            "  *\"-Verb resume\"*)\n"
            '    if [ "${FIXTURE_RESUME_RC:-0}" = "0" ]; then\n'
            "      echo '{\"passed\":true,\"verb\":\"resume\"}'\n"
            '    else\n'
            "      echo '{\"passed\":false,\"verb\":\"resume\",\"code\":\"settle-timeout\",\"message\":\"refused\"}'\n"
            '      exit "${FIXTURE_RESUME_RC}"\n'
            '    fi ;;\n'
            "  *\"-Verb stage\"*)\n"
            '    if [ -n "${FIXTURE_STAGE_OUTPUT:-}" ]; then\n'
            '      cat "$FIXTURE_STAGE_OUTPUT"; exit "${FIXTURE_STAGE_RC:-0}"\n'
            '    fi\n'
            "    echo '{\"passed\":true,\"verb\":\"stage\"}' ;;\n"
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
            "  *\"-Verb status\"*|*\"two status probes returned different\"*)\n"
            '    if [ "${FIXTURE_STATUS:-switched}" = "before" ]; then\n'
            "      echo '{\"passed\":true,\"verb\":\"status\",\"live_task\":"
            "{\"execute\":\"C:/original-install with spaces/runtime/Scripts/description.exe\","
            "\"arguments\":\"serve --config \\\"C:/original-install with spaces/endpoint.json\\\"\","
            "\"working_directory\":\"C:/description-e2e/state\","
            "\"action_count\":1,\"launch_contract\":\"legacy_pip_launcher_v0\","
            "\"action_matches_generation\":true,"
            "\"config\":\"C:/original-install with spaces/endpoint.json\",\"state\":\"Running\"},"
            "\"pending\":{\"intent\":true,\"verb\":\"promote\","
            "\"generation\":\"C:/description-e2e/state/lifecycle/gen-2/generation\","
            "\"previous\":\"C:/description-e2e/state/instance-promotions/previous-0001\","
            "\"source_launcher\":\"C:/original-install with spaces/runtime/Scripts/description.exe\","
            "\"source_arguments\":\"serve --config \\\"C:/original-install with spaces/endpoint.json\\\"\","
            "\"source_working_directory\":\"C:/description-e2e/state\","
            "\"source_config\":\"C:/original-install with spaces/endpoint.json\"},"
            "\"last_promotion\":null}'\n"
            '    elif [ "${FIXTURE_STATUS:-switched}" = "foreign" ]; then\n'
            "      echo '{\"passed\":true,\"verb\":\"status\",\"live_task\":"
            "{\"execute\":\"C:/foreign/not-description.exe\","
            "\"arguments\":\"foreign-service --option x\","
            "\"working_directory\":\"C:/description-e2e/state\","
            "\"action_count\":1,\"launch_contract\":null,"
            "\"action_matches_generation\":false,"
            "\"config\":\"C:/foreign/foreign.json\",\"state\":\"Running\"},"
            "\"pending\":null,\"last_promotion\":null}'\n"
            "    else\n"
            "      echo '{\"passed\":true,\"verb\":\"status\",\"live_task\":"
            "{\"execute\":\"C:/description-e2e/state/lifecycle/gen-2/generation/runtime/Scripts/python.exe\","
            "\"arguments\":\"-I -B -m description_pipeline.orchestration.windows serve --config "
            "\\\"C:/description-e2e/state/lifecycle/gen-2/generation/endpoint.json\\\"\","
            "\"working_directory\":\"C:/description-e2e/state\","
            "\"action_count\":1,\"launch_contract\":\"python_module_v1\","
            "\"action_matches_generation\":true,"
            "\"config\":\"C:/description-e2e/state/lifecycle/gen-2/generation/endpoint.json\","
            "\"state\":\"Running\"},"
            "\"pending\":{\"intent\":true,\"verb\":\"promote\","
            "\"generation\":\"C:/description-e2e/state/lifecycle/gen-2/generation\","
            "\"previous\":\"C:/description-e2e/state/instance-promotions/previous-0001\","
            "\"source_launcher\":\"C:/original-install with spaces/runtime/Scripts/description.exe\","
            "\"source_arguments\":\"serve --config \\\"C:/original-install with spaces/endpoint.json\\\"\","
            "\"source_working_directory\":\"C:/description-e2e/state\","
            "\"source_config\":\"C:/original-install with spaces/endpoint.json\"},"
            "\"last_promotion\":null}'\n"
            "    fi ;;\n"
            "  *\"-Verb rollback\"*) echo '{\"passed\":true,\"verb\":\"rollback\","
            "\"restored_from\":\"C:/description-e2e/state/instance-promotions/previous-0001\"}' ;;\n"
            "  *\"-Verb health\"*)\n"
            '    case "$* $decoded_script" in\n'
            '      *"/lifecycle/gen-1/"*binding.json*)\n'
            '        cat "${FIXTURE_HEALTH_FILE_PREVIOUS:-$FIXTURE_HEALTH_FILE}" ;;\n'
            '      *"/lifecycle/gen-2/windows-health-promote-verify-binding.json"*)\n'
            '        cat "${FIXTURE_HEALTH_FILE_VERIFY:-${FIXTURE_HEALTH_FILE_CANDIDATE:-$FIXTURE_HEALTH_FILE}}" ;;\n'
            '      *"/lifecycle/gen-2/"*binding.json*)\n'
            '        cat "${FIXTURE_HEALTH_FILE_CANDIDATE:-$FIXTURE_HEALTH_FILE}" ;;\n'
            '      *) cat "$FIXTURE_HEALTH_FILE" ;;\n'
            "    esac ;;\n"
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
            '    count=$((count + 1)); echo "$count" > "$REGISTER_COUNT"\n'
            '    if [ "$count" -gt "$REGISTER_AFTER" ]; then echo false > "$ADMISSION"; fi ;;\n'
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
            '"expected_dsn": os.environ["EXPECTED_PLATFORM_DSN"], '
            '"venv": os.environ.get("AIRFLOW_VENV")}))\'\n'
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
            "  *airflow-dag-capture*)\n"
            '    paused="$(cat "$ADMISSION")"\n'
            "    printf '{\"dag_id\":\"solidworks_to_urdf\",\"paused\":%s}\\n' \"$paused\" > \"$2\"\n"
            '    echo "dag_capture paused=$paused" >> "$ORDER_LOG" ;;\n'
            "  *airflow-dag-state-check*)\n"
            '    expected="$2"\n'
            '    actual="$(cat "$ADMISSION")"\n'
            '    echo "dag_state_check expected=$expected actual=$actual" >> "$ORDER_LOG"\n'
            '    [ "$actual" = "$expected" ] || exit 1 ;;\n'
            "  *platform-close-admission*)\n"
            '    if [ "$PLATFORM_SERVICE_DSN" != "$EXPECTED_PLATFORM_SERVICE_DSN" ]; then '
            'echo "service=$PLATFORM_SERVICE_DSN expected=$EXPECTED_PLATFORM_SERVICE_DSN" >&2; exit 78; fi\n'
            '    [ "$PLATFORM_OPERATOR_DSN" = "$EXPECTED_PLATFORM_DSN" ] || exit 77\n'
            '    [ "$PIPELINE_STORE_ROOT" = "$EXPECTED_PIPELINE_STORE_ROOT" ] || exit 79\n'
            '    [ "$AIRFLOW_VENV" = "$EXPECTED_AIRFLOW_VENV" ] || exit 80\n'
            '    [ "$OPERATOR_STATE" = "$EXPECTED_OPERATOR_STATE" ] || exit 81\n'
            '    [ "$PLATFORM_CONFIG_DIGEST" = "$EXPECTED_PLATFORM_CONFIG_DIGEST" ] || exit 82\n'
            '    echo true > "$ADMISSION"\n'
            '    echo "platform close_admission" >> "$ORDER_LOG"\n'
            '    echo "admission=closed revision=2" ;;\n'
            "  *platform-open-admission*)\n"
            '    if [ "$PLATFORM_SERVICE_DSN" != "$EXPECTED_PLATFORM_SERVICE_DSN" ]; then '
            'echo "service=$PLATFORM_SERVICE_DSN expected=$EXPECTED_PLATFORM_SERVICE_DSN" >&2; exit 78; fi\n'
            '    [ "$PLATFORM_OPERATOR_DSN" = "$EXPECTED_PLATFORM_DSN" ] || exit 77\n'
            '    [ "$PIPELINE_STORE_ROOT" = "$EXPECTED_PIPELINE_STORE_ROOT" ] || exit 79\n'
            '    [ "$AIRFLOW_VENV" = "$EXPECTED_AIRFLOW_VENV" ] || exit 80\n'
            '    [ "$OPERATOR_STATE" = "$EXPECTED_OPERATOR_STATE" ] || exit 81\n'
            '    [ "$PLATFORM_CONFIG_DIGEST" = "$EXPECTED_PLATFORM_CONFIG_DIGEST" ] || exit 82\n'
            '    echo false > "$ADMISSION"\n'
            '    echo "platform open_admission" >> "$ORDER_LOG"\n'
            '    echo "admission=open revision=3" ;;\n'
            "  *platform-reconcile*)\n"
            '    [ -n "${RECONCILE_RUN_ID:-}" ] && [ -n "${RECONCILE_OUTCOME:-}" ] || exit 61\n'
            '    if [ "$PLATFORM_SERVICE_DSN" != "$EXPECTED_PLATFORM_SERVICE_DSN" ]; then '
            'echo "service=$PLATFORM_SERVICE_DSN expected=$EXPECTED_PLATFORM_SERVICE_DSN" >&2; exit 78; fi\n'
            '    [ "$PLATFORM_OPERATOR_DSN" = "$EXPECTED_PLATFORM_DSN" ] || exit 77\n'
            '    [ "$PIPELINE_STORE_ROOT" = "$EXPECTED_PIPELINE_STORE_ROOT" ] || exit 79\n'
            '    [ "$PLATFORM_CONFIG_DIGEST" = "$EXPECTED_PLATFORM_CONFIG_DIGEST" ] || exit 82\n'
            '    echo "reconcile_env stage=${RECONCILE_STAGE} reported=${RECONCILE_REPORTED_STAGE:-} '
            'native=${RECONCILE_NATIVE_RUN_ID:-} op=${RECONCILE_ORPHAN_OPERATION_ID:-} '
            'task=${RECONCILE_ORPHAN_TASK_ID:-} map=${RECONCILE_ORPHAN_MAP_INDEX:-}" >> "$ORDER_LOG"\n'
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
            "install": {"config_digest": "d" * 64, "install_id": "win-" + "1" * 32},
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
            "EXPECTED_PLATFORM_SERVICE_DSN": derived_env["PLATFORM_SERVICE_DSN"],
            "EXPECTED_PIPELINE_STORE_ROOT": derived_env["PIPELINE_STORE_ROOT"],
            "EXPECTED_AIRFLOW_VENV": derived_env["AIRFLOW_VENV"],
            "EXPECTED_OPERATOR_STATE": derived_env["OPERATOR_STATE"],
            "EXPECTED_PLATFORM_CONFIG_DIGEST": derived_env["PLATFORM_CONFIG_DIGEST"],
            "FIXTURE_HEALTH_FILE": str(health_file),
            "FIXTURE_OPERATOR_STATE": str(tmp / "state"),
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
                ready = lines.index(
                    "systemctl --user is-active --quiet description-portal.service description-operator-proxy.service"
                )
                self.assertLess(ingress, ready)
                resume = next(i for i, line in enumerate(lines) if "-Verb resume" in line)
                self.assertLess(ready, resume, "the Windows endpoint resumes after Linux services are active")
                self.assertLess(
                    resume,
                    min(i for i, line in enumerate(lines) if line.startswith("dag_state_check ")),
                    "DAG state checks run only after the Windows endpoint has resumed",
                )
                unpause = lines.index("airflow dags unpause solidworks_to_urdf")
                self.assertLess(resume, unpause, "the Windows endpoint resumes before DAG scheduling is enabled")
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
            # The controlled start precondition closes the platform gate before services are
            # launched; an ingress failure has no scheduling or Windows-resume side effect.
            order_text = order.read_text()
            self.assertIn("platform close_admission", order_text)
            lines = order_text.splitlines()
            self.assertNotIn("airflow dags unpause solidworks_to_urdf", order_text)
            self.assertFalse([line for line in lines if "-Verb resume" in line])
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

    def test_reconcile_forwards_reported_stage_with_the_bound_native_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path, env, _, order, control = self._admission_fixture(Path(tmp))
            evidence = Path(tmp) / "native-receipt.json"
            evidence.write_text("{}\n", encoding="utf-8")
            invalid = run(
                [
                    "bash", str(control), "reconcile", "--config", str(config_path),
                    "--run-id", "run-1", "--stage", "discover", "--outcome", "failed",
                    "--evidence", str(evidence), "--reported-stage", "freeze",
                ],
                env,
            )
            self.assertEqual(invalid.returncode, 1, invalid.stdout + invalid.stderr)
            self.assertIn("orphan recovery requires", invalid.stderr)
            valid = run(
                [
                    "bash", str(control), "reconcile", "--config", str(config_path),
                    "--run-id", "run-1", "--stage", "discover", "--outcome", "failed",
                    "--evidence", str(evidence), "--native-receipt", str(evidence),
                    "--native-run-id", "3324bcb0-085a-5f77-8edc-a4c1be2f1a5a",
                    "--orphan-operation-id", "e88a68da-6bac-49c6-a325-8f2f979daf76",
                    "--orphan-task-id", "discover", "--orphan-map-index", "-1",
                    "--reported-stage", "freeze",
                ],
                env,
            )
            self.assertEqual(valid.returncode, 0, valid.stdout + valid.stderr)
            self.assertIn(
                "reconcile_env stage=discover reported=freeze "
                "native=3324bcb0-085a-5f77-8edc-a4c1be2f1a5a "
                "op=e88a68da-6bac-49c6-a325-8f2f979daf76 task=discover map=-1",
                order.read_text(encoding="utf-8"),
            )
            control_text = control.read_text(encoding="utf-8")
            self.assertIn('reconcile_frame["reported_stage"] = reported_stage', control_text)
            self.assertIn('"params_digest": io.digest(reconcile_frame)', control_text)

    def test_embedded_reconcile_digest_changes_with_reported_stage(self) -> None:
        from description_pipeline import io
        import io as standard_io
        import types
        from unittest import mock

        control_text = CONTROL.read_text(encoding="utf-8")
        marker = control_text.index("# platform-reconcile")
        start = control_text.index("\n", marker) + 1
        end = control_text.index("\nPY\n", start)
        script = control_text[start:end]
        captured: dict = {}

        class FakePlatformStore:
            @classmethod
            def from_environment(cls):
                return cls()

            def confirm_stage(self, **kwargs):
                captured.update(kwargs)

        fake_store = types.ModuleType("description_pipeline.platform.store")
        fake_store.PlatformStore = FakePlatformStore
        common = {
            "RECONCILE_RUN_ID": "run-1",
            "RECONCILE_STAGE": "discover",
            "RECONCILE_OUTCOME": "failed",
            "RECONCILE_EVIDENCE": json.dumps(["receipt.json"]),
            "RECONCILE_NATIVE_RECEIPT": "receipt.json",
            "RECONCILE_NATIVE_RUN_ID": "3324bcb0-085a-5f77-8edc-a4c1be2f1a5a",
            "RECONCILE_ORPHAN_OPERATION_ID": "e88a68da-6bac-49c6-a325-8f2f979daf76",
            "RECONCILE_ORPHAN_TASK_ID": "discover",
            "RECONCILE_ORPHAN_MAP_INDEX": "-1",
        }
        frame = {
            "outcome": "failed",
            "evidence": ["receipt.json"],
            "binding": {
                "operation_id": common["RECONCILE_ORPHAN_OPERATION_ID"],
                "task_id": "discover",
                "map_index": -1,
            },
        }

        def run_embedded(reported_stage: str) -> dict:
            captured.clear()
            environment = {**common, "RECONCILE_REPORTED_STAGE": reported_stage}
            with (
                mock.patch.dict(sys.modules, {"description_pipeline.platform.store": fake_store}),
                mock.patch.dict(os.environ, environment, clear=False),
                contextlib.redirect_stdout(standard_io.StringIO()),
            ):
                exec(compile(script, "<operatorctl-platform-reconcile>", "exec"), {"__name__": "__main__"})
            return dict(captured["reconcile"])

        classic = run_embedded("")
        reported = run_embedded("freeze")
        self.assertNotIn("reported_stage", classic)
        self.assertEqual(reported["reported_stage"], "freeze")
        self.assertEqual(classic["params_digest"], io.digest(frame))
        self.assertEqual(
            reported["params_digest"],
            io.digest({**frame, "reported_stage": "freeze"}),
        )
        self.assertNotEqual(classic["params_digest"], reported["params_digest"])

    def test_windows_reconciliation_requires_exact_python_module_action(self) -> None:
        control_text = CONTROL.read_text(encoding="utf-8")
        marker = (
            '      if ! "$CONTROL_PYTHON" - "$RECEIPT_DIR/windows-binding.json" '
            '"$WINDOWS_STATUS" \\\n'
            '          > "$RECONCILIATION_OUTPUT" 2> "$RECONCILIATION_OUTPUT.stderr" <<\'PY\'\n'
        )
        start = control_text.index(marker) + len(marker)
        end = control_text.index("\nPY\n", start)
        script = control_text[start:end]
        target = "C:/description-e2e/state/lifecycle/gen-2/generation"
        previous = "C:/description-e2e/state/instance-promotions/previous-0001"
        exact_arguments = (
            '-I -B -m description_pipeline.orchestration.windows serve --config "'
            + target
            + '/endpoint.json"'
        )
        binding = {
            "dir": "C:/description-e2e/state/lifecycle/gen-2",
            "state_root": "C:/description-e2e/state",
        }
        status = {
            "live_task": {
                "execute": target + "/runtime/Scripts/python.exe",
                "arguments": exact_arguments,
                "working_directory": binding["state_root"],
                "config": target + "/endpoint.json",
                "state": "Running",
                "action_count": 1,
                "launch_contract": "python_module_v1",
                "action_matches_generation": True,
            },
            "pending": {
                "generation": target,
                "previous": previous,
                "source_launcher": "C:/old/runtime/Scripts/description.exe",
                "source_arguments": 'serve --config "C:/old/endpoint.json"',
                "source_working_directory": binding["state_root"],
                "source_config": "C:/old/endpoint.json",
            },
            "last_promotion": None,
        }

        def reconcile(live_status: dict) -> str:
            with tempfile.TemporaryDirectory() as tmp:
                binding_path = Path(tmp) / "windows-binding.json"
                status_path = Path(tmp) / "windows-status.json"
                binding_path.write_text(json.dumps(binding), encoding="utf-8")
                status_path.write_text(json.dumps(live_status), encoding="utf-8")
                result = subprocess.run(
                    [sys.executable, "-c", script, str(binding_path), str(status_path)],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                return result.stdout.strip()

        self.assertEqual(reconcile(status), previous)
        for separator in ("\\", "mixed"):
            with self.subTest(separator=separator):
                variant = copy.deepcopy(status)
                variant["live_task"].update(
                    execute=(
                        target.replace("/", "\\")
                        if separator == "\\"
                        else target.replace("lifecycle/gen-2", "lifecycle\\gen-2")
                    )
                    + (
                        "\\runtime\\Scripts\\python.exe"
                        if separator == "\\"
                        else "/runtime\\Scripts/python.exe"
                    ),
                    arguments=exact_arguments.replace("/", "\\")
                    if separator == "\\"
                    else exact_arguments.replace("lifecycle/gen-2", "lifecycle\\gen-2"),
                    config=target.replace("/", "\\") + "\\endpoint.json"
                    if separator == "\\"
                    else target + "\\endpoint.json",
                )
                self.assertEqual(reconcile(variant), previous)
        source_status = copy.deepcopy(status)
        source_status["live_task"] = {
            "execute": "C:/old/runtime/Scripts/description.exe",
            "arguments": 'serve --config "C:/old/endpoint.json"',
            "working_directory": binding["state_root"],
            "config": "C:/old/endpoint.json",
            "state": "Running",
            "action_count": 1,
            "launch_contract": "legacy_pip_launcher_v0",
            "action_matches_generation": True,
        }
        self.assertEqual(reconcile(source_status), "before")
        python_source = "C:/description-e2e/state/lifecycle/gen-1/generation"
        python_source_status = copy.deepcopy(status)
        python_source_status["live_task"] = {
            "execute": python_source + "/runtime/Scripts/python.exe",
            "arguments": '-I -m description_pipeline serve --config "'
            + python_source
            + '/endpoint.json"',
            "working_directory": binding["state_root"],
            "config": python_source + "/endpoint.json",
            "state": "Running",
            "action_count": 1,
            "launch_contract": "python_module_v1",
            "action_matches_generation": True,
        }
        python_source_status["pending"].update(
            source_launcher=python_source + "/runtime/Scripts/python.exe",
            source_arguments='-I -m description_pipeline serve --config "'
            + python_source
            + '/endpoint.json"',
            source_working_directory=binding["state_root"],
            source_config=python_source + "/endpoint.json",
        )
        self.assertEqual(reconcile(python_source_status), "before")
        source_near_match = copy.deepcopy(source_status)
        source_near_match["live_task"]["arguments"] += " --foreign"
        self.assertEqual(reconcile(source_near_match), "unknown")
        source_case_mismatch = copy.deepcopy(source_status)
        source_case_mismatch["live_task"]["arguments"] = (
            source_case_mismatch["live_task"]["arguments"].replace("serve", "Serve", 1)
        )
        self.assertEqual(reconcile(source_case_mismatch), "unknown")
        legacy_pending = copy.deepcopy(source_status)
        legacy_pending["live_task"].update(
            launch_contract="legacy_pip_launcher_v0",
            action_matches_generation=True,
        )
        legacy_pending["pending"].pop("source_arguments")
        legacy_pending["pending"].pop("source_working_directory")
        self.assertEqual(reconcile(legacy_pending), "before")
        for label, mutate in (
            (
                "legacy-contract-missing",
                lambda value: value["live_task"].pop("launch_contract"),
            ),
            (
                "legacy-action-mismatch",
                lambda value: value["live_task"].update(
                    action_matches_generation=False
                ),
            ),
            (
                "legacy-source-arguments-unbound",
                lambda value: value["live_task"].update(
                    arguments=value["live_task"]["arguments"] + " --foreign"
                ),
            ),
            (
                "legacy-source-fields-partial",
                lambda value: value["pending"].update(
                    source_working_directory=binding["state_root"]
                ),
            ),
            (
                "legacy-source-config-unpaired",
                lambda value: value["pending"].update(
                    source_config="C:/unbound/endpoint.json"
                ),
            ),
        ):
            with self.subTest(label=label):
                changed = copy.deepcopy(legacy_pending)
                mutate(changed)
                self.assertEqual(reconcile(changed), "unknown")
        python_source_missing_fields = copy.deepcopy(legacy_pending)
        python_arguments = (
            '-I -B -m description_pipeline serve --config "C:/old/endpoint.json"'
        )
        python_source_missing_fields["live_task"].update(
            execute="C:/old/runtime/Scripts/python.exe",
            arguments=python_arguments,
            launch_contract="python_module_v1",
        )
        python_source_missing_fields["pending"][
            "source_launcher"
        ] = "C:/old/runtime/Scripts/python.exe"
        self.assertEqual(reconcile(python_source_missing_fields), "unknown")
        for label, arguments in (
            ("additional-token", exact_arguments + " --foreign"),
            ("bytecode-flag-missing", exact_arguments.replace("-I -B ", "-I ", 1)),
            ("interactive-flag", exact_arguments.replace("-I ", "-i ", 1)),
            ("module-case", exact_arguments.replace("description_pipeline", "Description_Pipeline")),
        ):
            with self.subTest(label=label):
                near_match = copy.deepcopy(status)
                near_match["live_task"]["arguments"] = arguments
                self.assertEqual(reconcile(near_match), "unknown")
        for label, changes in (
            ("action-not-matched", {"action_matches_generation": False}),
            ("contract-null", {"launch_contract": None}),
            ("contract-foreign", {"launch_contract": "foreign_launcher_v1"}),
            ("action-count-boolean", {"action_count": True}),
            ("action-count-float", {"action_count": 1.0}),
            ("task-ready", {"state": "Ready"}),
            ("task-disabled", {"state": "Disabled"}),
            ("task-unknown", {"state": "Unknown"}),
        ):
            with self.subTest(label=label):
                changed = copy.deepcopy(status)
                changed["live_task"].update(changes)
                self.assertEqual(reconcile(changed), "unknown")

    def test_promotion_replay_fresh_requires_both_stage_steps(self) -> None:
        promotion = OPERATOR / "promotion.py"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            source = root / "source.json"
            target = root / "target.json"
            release = root / "release.json"
            source.write_text('{"install_id":"gen-1"}\n', encoding="utf-8")
            target.write_text('{"install_id":"gen-2"}\n', encoding="utf-8")
            release.write_text('{"version":"1.5.0"}\n', encoding="utf-8")

            def init(
                install_id: str,
                plan: str = "linux-generation,windows-generation,close-admission",
            ) -> None:
                target.write_text(
                    json.dumps({"install_id": install_id}) + "\n",
                    encoding="utf-8",
                )
                result = subprocess.run(
                    [
                        sys.executable,
                        str(promotion),
                        "init",
                        "--state-root",
                        str(state),
                        "--install-id",
                        install_id,
                        "--from",
                        str(source),
                        "--to",
                        str(target),
                        "--release-file",
                        str(release),
                        "--plan",
                        plan,
                    ],
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode, 0, result.stderr)

            def step(install_id: str, name: str, verb: str = "promote") -> None:
                result = subprocess.run(
                    [
                        sys.executable,
                        str(promotion),
                        "step",
                        "--state-root",
                        str(state),
                        "--install-id",
                        install_id,
                        "--verb",
                        verb,
                        "--step",
                        name,
                        "--status",
                        "completed",
                        "--evidence",
                        str(source),
                        "--freeze-root",
                        str(state / "promotions" / install_id / "evidence-immutable"),
                    ],
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode, 0, result.stderr)

            def replay(install_id: str) -> subprocess.CompletedProcess:
                return subprocess.run(
                    [
                        sys.executable,
                        str(promotion),
                        "replay",
                        "--state-root",
                        str(state),
                        "--install-id",
                        install_id,
                        "--verb",
                        "promote",
                        "--active-record",
                        str(source),
                    ],
                    capture_output=True,
                    text=True,
                )

            init("linux-only")
            step("linux-only", "linux-generation")
            self.assertEqual(replay("linux-only").returncode, 11)
            init("empty-promote")
            self.assertEqual(replay("empty-promote").returncode, 11)
            init("windows-only")
            step("windows-only", "windows-generation")
            self.assertEqual(replay("windows-only").returncode, 11)
            init("both-stage-steps")
            step("both-stage-steps", "linux-generation")
            step("both-stage-steps", "windows-generation")
            duplicate = subprocess.run(
                [
                    sys.executable,
                    str(promotion),
                    "step",
                    "--state-root",
                    str(state),
                    "--install-id",
                    "both-stage-steps",
                    "--verb",
                    "promote",
                    "--step",
                    "linux-generation",
                    "--status",
                    "completed",
                    "--evidence",
                    str(source),
                    "--freeze-root",
                    str(state / "promotions" / "both-stage-steps" / "evidence-immutable"),
                ],
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(duplicate.returncode, 0)
            self.assertIn("already recorded", duplicate.stderr)
            result = replay("both-stage-steps")
            self.assertEqual(result.returncode, 10)
            self.assertEqual(result.stdout.strip(), "replay=fresh")
            stage_receipt = state / "promotions" / "both-stage-steps" / "receipt.json"
            stage_document = json.loads(stage_receipt.read_text(encoding="utf-8"))
            stage_document["steps"].append(
                {
                    "seq": len(stage_document["steps"]) + 1,
                    "verb": "promote",
                    "step": "close-admission",
                    "status": "failed",
                    "at": "2026-10-22T00:00:00Z",
                    "evidence": [str(source)],
                    "evidence_digests": [
                        {
                            "path": str(source),
                            "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                        }
                    ],
                }
            )
            stage_receipt.write_text(json.dumps(stage_document), encoding="utf-8")
            self.assertEqual(replay("both-stage-steps").returncode, 11)

            init("fresh-rollback")
            for active_record in (source, target):
                rollback_fresh = subprocess.run(
                    [
                        sys.executable,
                        str(promotion),
                        "replay",
                        "--state-root",
                        str(state),
                        "--install-id",
                        "fresh-rollback",
                        "--verb",
                        "rollback",
                        "--active-record",
                        str(active_record),
                    ],
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(rollback_fresh.returncode, 10, rollback_fresh.stderr)
                self.assertEqual(rollback_fresh.stdout.strip(), "replay=fresh")
            unrelated = root / "unrelated.json"
            unrelated.write_text('{"install_id":"gen-9"}\n', encoding="utf-8")
            rollback_fresh = subprocess.run(
                [
                    sys.executable,
                    str(promotion),
                    "replay",
                    "--state-root",
                    str(state),
                    "--install-id",
                    "fresh-rollback",
                    "--verb",
                    "rollback",
                    "--active-record",
                    str(unrelated),
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(rollback_fresh.returncode, 11)

            rollback_steps = (
                "rollback-close",
                "rollback-drain",
                "rollback-stop",
                "rollback-windows",
                "rollback-linux",
                "rollback-config-reference",
                "rollback-verify",
            )
            init("rollback-complete", ",".join(rollback_steps))
            for name in rollback_steps:
                step("rollback-complete", name, "rollback")
            rollback_receipt = (
                state / "promotions" / "rollback-complete" / "receipt.json"
            )
            rollback_replay = subprocess.run(
                [
                    sys.executable,
                    str(promotion),
                    "replay",
                    "--state-root",
                    str(state),
                    "--install-id",
                    "rollback-complete",
                    "--verb",
                    "rollback",
                    "--active-record",
                    str(source),
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(rollback_replay.returncode, 0, rollback_replay.stderr)
            rollback_bytes = rollback_receipt.read_bytes()
            rollback_document = json.loads(rollback_bytes)
            rollback_document["install_id"] = "misbound-generation"
            rollback_receipt.write_text(json.dumps(rollback_document), encoding="utf-8")
            rollback_replay = subprocess.run(
                [
                    sys.executable,
                    str(promotion),
                    "replay",
                    "--state-root",
                    str(state),
                    "--install-id",
                    "rollback-complete",
                    "--verb",
                    "rollback",
                    "--active-record",
                    str(source),
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(rollback_replay.returncode, 11)
            rollback_receipt.write_bytes(rollback_bytes)
            tampered_document = json.loads(rollback_bytes)
            tampered_document["steps"][0]["evidence_digests"][0]["sha256"] = "0" * 64
            rollback_receipt.write_text(json.dumps(tampered_document), encoding="utf-8")
            self.assertEqual(
                subprocess.run(
                    [
                        sys.executable,
                        str(promotion),
                        "replay",
                        "--state-root",
                        str(state),
                        "--install-id",
                        "rollback-complete",
                        "--verb",
                        "rollback",
                        "--active-record",
                        str(source),
                    ],
                    capture_output=True,
                    text=True,
                ).returncode,
                11,
            )
            rollback_receipt.write_bytes(rollback_bytes)
            source.write_text('{"install_id":"gen-1"} \n', encoding="utf-8")
            rollback_replay = subprocess.run(
                [
                    sys.executable,
                    str(promotion),
                    "replay",
                    "--state-root",
                    str(state),
                    "--install-id",
                    "rollback-complete",
                    "--verb",
                    "rollback",
                    "--active-record",
                    str(source),
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(rollback_replay.returncode, 11)

    def test_promotion_provenance_requires_immutable_maps(self) -> None:
        promotion = OPERATOR / "promotion.py"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            source = root / "source.json"
            target = root / "target.json"
            release = root / "release.json"
            evidence = root / "step.out"
            source.write_text('{"install_id":"gen-1"}\n', encoding="utf-8")
            target.write_text('{"install_id":"gen-2"}\n', encoding="utf-8")
            release.write_text('{"version":"1.5.0"}\n', encoding="utf-8")
            evidence.write_text("sealed evidence\n", encoding="utf-8")

            def init(install_id: str) -> None:
                result = subprocess.run(
                    [
                        sys.executable,
                        str(promotion),
                        "init",
                        "--state-root",
                        str(state),
                        "--install-id",
                        install_id,
                        "--from",
                        str(source),
                        "--to",
                        str(target),
                        "--release-file",
                        str(release),
                        "--plan",
                        "linux-generation",
                    ],
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode, 0, result.stderr)

            def step_command(
                install_id: str,
                *,
                freeze_root: Path | None,
            ) -> list[str]:
                command = [
                    sys.executable,
                    str(promotion),
                    "step",
                    "--state-root",
                    str(state),
                    "--install-id",
                    install_id,
                    "--verb",
                    "promote",
                    "--step",
                    "linux-generation",
                    "--status",
                    "completed",
                    "--evidence",
                    str(evidence),
                ]
                if freeze_root is not None:
                    command.extend(
                        [
                            "--freeze-root",
                            str(freeze_root),
                        ]
                    )
                return command

            def step_state(install_id: str) -> subprocess.CompletedProcess:
                return subprocess.run(
                    [
                        sys.executable,
                        str(promotion),
                        "step-state",
                        "--state-root",
                        str(state),
                        "--install-id",
                        install_id,
                        "--verb",
                        "promote",
                        "--step",
                        "linux-generation",
                    ],
                    capture_output=True,
                    text=True,
                )

            init("missing-freeze")
            missing_freeze = subprocess.run(
                step_command("missing-freeze", freeze_root=None),
                capture_output=True,
                text=True,
            )
            self.assertEqual(
                missing_freeze.returncode,
                2,
                missing_freeze.stdout + missing_freeze.stderr,
            )
            self.assertIn("--freeze-root", missing_freeze.stderr)

            init("provenance")
            registered = subprocess.run(
                step_command(
                    "provenance",
                    freeze_root=(
                        state / "promotions" / "provenance" / "evidence-immutable"
                    ),
                ),
                capture_output=True,
                text=True,
            )
            self.assertEqual(registered.returncode, 0, registered.stderr)
            receipt = state / "promotions" / "provenance" / "receipt.json"
            receipt_bytes = receipt.read_bytes()
            document = json.loads(receipt_bytes)
            row = document["steps"][0]
            map_path = Path(
                next(path for path in row["evidence"] if path.endswith("evidence-map.json"))
            )
            snapshot_path = Path(
                next(path for path in row["evidence"] if not path.endswith("evidence-map.json"))
            )
            map_bytes = map_path.read_bytes()
            valid = step_state("provenance")
            self.assertEqual(valid.returncode, 0, valid.stderr)
            self.assertEqual(valid.stdout.strip(), "step=completed")

            def write_document(value: dict) -> None:
                for value_row in value["steps"]:
                    value_row["evidence_digests"] = [
                        {
                            "path": path,
                            "sha256": hashlib.sha256(Path(path).read_bytes()).hexdigest(),
                        }
                        for path in value_row["evidence"]
                    ]
                receipt.write_text(
                    json.dumps(value, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )

            def assert_partial(label: str) -> None:
                result = step_state("provenance")
                self.assertEqual(
                    result.returncode,
                    11,
                    f"{label}: {result.stdout}{result.stderr}",
                )
                self.assertEqual(result.stdout.strip(), "step=partial", label)

            outside = root / "outside-freeze.out"
            outside.write_text("unfrozen evidence\n", encoding="utf-8")
            draft = copy.deepcopy(document)
            draft["steps"][0]["evidence"] = [str(outside)]
            write_document(draft)
            assert_partial("unfrozen row")

            draft = copy.deepcopy(document)
            draft["steps"][0]["evidence"] = [str(snapshot_path)]
            write_document(draft)
            assert_partial("missing map")

            receipt.write_bytes(receipt_bytes)
            map_path.write_text("[]\n", encoding="utf-8")
            draft = copy.deepcopy(document)
            write_document(draft)
            assert_partial("empty map")

            map_path.write_bytes(map_bytes)
            alternate = receipt.parent / "alternate-freeze"
            alternate.mkdir()
            alternate_map = alternate / map_path.name
            alternate_snapshot = alternate / snapshot_path.name
            shutil.copy2(map_path, alternate_map)
            shutil.copy2(snapshot_path, alternate_snapshot)
            draft = copy.deepcopy(document)
            draft["steps"][0]["evidence"] = [
                str(alternate_snapshot),
                str(alternate_map),
            ]
            write_document(draft)
            assert_partial("noncanonical freeze root")

            receipt.write_bytes(receipt_bytes)
            extra = map_path.parent / "unmapped-extra.snapshot"
            extra.write_text("extra\n", encoding="utf-8")
            assert_partial("unmapped snapshot")
            extra.unlink()

            symlink_target = root / "symlink-target.out"
            symlink_target.write_bytes(snapshot_path.read_bytes())
            snapshot_path.unlink()
            snapshot_path.symlink_to(symlink_target)
            receipt.write_bytes(receipt_bytes)
            assert_partial("symlink evidence")
            snapshot_path.unlink()
            snapshot_path.write_bytes(symlink_target.read_bytes())
            final = step_state("provenance")
            self.assertEqual(final.returncode, 0, final.stderr)
            self.assertEqual(final.stdout.strip(), "step=completed")

            init("symlinked-freeze")
            external_freeze = root / "external-freeze"
            external_freeze.mkdir()
            symlinked_root = (
                state / "promotions" / "symlinked-freeze" / "evidence-immutable"
            )
            symlinked_root.symlink_to(external_freeze, target_is_directory=True)
            refused_symlink = subprocess.run(
                step_command("symlinked-freeze", freeze_root=symlinked_root),
                capture_output=True,
                text=True,
            )
            self.assertEqual(
                refused_symlink.returncode,
                1,
                refused_symlink.stdout + refused_symlink.stderr,
            )
            self.assertIn("symlink ancestor", refused_symlink.stderr)
            self.assertEqual(list(external_freeze.iterdir()), [])
            symlinked_receipt = (
                state / "promotions" / "symlinked-freeze" / "receipt.json"
            )
            self.assertEqual(
                json.loads(symlinked_receipt.read_text(encoding="utf-8"))["steps"],
                [],
            )

            init("aliased-freeze")
            alias_target = root / "alias-target"
            alias_target.mkdir()
            alias = state / "promotions" / "aliased-freeze" / "freeze-alias"
            alias.symlink_to(alias_target, target_is_directory=True)
            refused_alias = subprocess.run(
                step_command("aliased-freeze", freeze_root=alias),
                capture_output=True,
                text=True,
            )
            self.assertEqual(
                refused_alias.returncode,
                1,
                refused_alias.stdout + refused_alias.stderr,
            )
            self.assertIn("freeze root is not canonical", refused_alias.stderr)
            self.assertEqual(list(alias_target.iterdir()), [])
            self.assertFalse(
                (state / "promotions" / "aliased-freeze" / "evidence-immutable").exists()
            )

            promotions = state / "promotions"
            moved_promotions = root / "moved-promotions"
            promotions.rename(moved_promotions)
            promotions.symlink_to(moved_promotions, target_is_directory=True)
            ancestor_partial = step_state("provenance")
            self.assertEqual(ancestor_partial.returncode, 11)
            self.assertEqual(ancestor_partial.stdout.strip(), "step=partial")

    def test_promotion_freeze_detaches_original_evidence(self) -> None:
        promotion = OPERATOR / "promotion.py"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            source = root / "source.json"
            target = root / "target.json"
            release = root / "release.json"
            first_evidence = root / "linux.out"
            second_evidence = root / "windows.out"
            source.write_text('{"install_id":"gen-1"}\n', encoding="utf-8")
            target.write_text('{"install_id":"detached"}\n', encoding="utf-8")
            release.write_text('{"version":"1.5.0"}\n', encoding="utf-8")
            first_evidence.write_text("linux evidence\n", encoding="utf-8")
            second_evidence.write_text("windows evidence\n", encoding="utf-8")

            initialized = subprocess.run(
                [
                    sys.executable,
                    str(promotion),
                    "init",
                    "--state-root",
                    str(state),
                    "--install-id",
                    "detached",
                    "--from",
                    str(source),
                    "--to",
                    str(target),
                    "--release-file",
                    str(release),
                    "--plan",
                    "linux-generation,windows-generation",
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            freeze_root = state / "promotions" / "detached" / "evidence-immutable"
            for step, evidence in (
                ("linux-generation", first_evidence),
                ("windows-generation", second_evidence),
            ):
                registered = subprocess.run(
                    [
                        sys.executable,
                        str(promotion),
                        "step",
                        "--state-root",
                        str(state),
                        "--install-id",
                        "detached",
                        "--verb",
                        "promote",
                        "--step",
                        step,
                        "--status",
                        "completed",
                        "--evidence",
                        str(evidence),
                        "--freeze-root",
                        str(freeze_root),
                    ],
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(registered.returncode, 0, registered.stderr)

            def replay() -> subprocess.CompletedProcess:
                return subprocess.run(
                    [
                        sys.executable,
                        str(promotion),
                        "replay",
                        "--state-root",
                        str(state),
                        "--install-id",
                        "detached",
                        "--verb",
                        "promote",
                        "--active-record",
                        str(source),
                    ],
                    capture_output=True,
                    text=True,
                )

            before_replay = replay()
            self.assertEqual(before_replay.returncode, 10, before_replay.stderr)
            self.assertEqual(before_replay.stdout.strip(), "replay=fresh")
            receipt = state / "promotions" / "detached" / "receipt.json"
            receipt_before = receipt.read_bytes()
            document = json.loads(receipt_before)
            snapshots = [
                Path(path)
                for row in document["steps"]
                for path in row["evidence"]
                if not path.endswith("evidence-map.json")
            ]
            frozen_before = {path: path.read_bytes() for path in snapshots}
            for snapshot, original in zip(
                snapshots,
                (first_evidence, second_evidence),
                strict=True,
            ):
                self.assertNotEqual(snapshot.stat().st_ino, original.stat().st_ino)

            first_evidence.write_text("overwritten after freeze\n", encoding="utf-8")
            second_evidence.write_bytes(b"")
            self.assertEqual(receipt.read_bytes(), receipt_before)
            for path, payload in frozen_before.items():
                self.assertEqual(path.read_bytes(), payload)
            after_replay = replay()
            self.assertEqual(after_replay.returncode, 10, after_replay.stderr)
            self.assertEqual(after_replay.stdout.strip(), "replay=fresh")

    def test_promotion_init_is_atomic_under_concurrency_and_refuses_dangling_links(
        self,
    ) -> None:
        promotion = OPERATOR / "promotion.py"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            source = root / "source.json"
            release = root / "release.json"
            source.write_text('{"install_id":"gen-1"}\n', encoding="utf-8")
            release.write_text('{"version":"1.5.0"}\n', encoding="utf-8")

            def init_command(
                install_id: str,
                target: Path,
                state_root: Path = state,
            ) -> list[str]:
                return [
                    sys.executable,
                    str(promotion),
                    "init",
                    "--state-root",
                    str(state_root),
                    "--install-id",
                    install_id,
                    "--from",
                    str(source),
                    "--to",
                    str(target),
                    "--release-file",
                    str(release),
                    "--plan",
                    "linux-generation,windows-generation,close-admission",
                ]

            target_a = root / "target-a.json"
            target_b = root / "target-b.json"
            target_a.write_text('{"install_id":"race-a"}\n', encoding="utf-8")
            target_b.write_text('{"install_id":"race-b"}\n', encoding="utf-8")
            race_processes = [
                subprocess.Popen(
                    init_command("race", target),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                for target in (target_a, target_b)
            ]
            first_out, first_err = race_processes[0].communicate(timeout=15)
            receipt = state / "promotions" / "race" / "receipt.json"
            winner_bytes = receipt.read_bytes()
            second_out, second_err = race_processes[1].communicate(timeout=15)
            self.assertEqual(
                sorted([race_processes[0].returncode, race_processes[1].returncode]),
                [0, 1],
                first_out + first_err + second_out + second_err,
            )
            self.assertEqual(receipt.read_bytes(), winner_bytes)
            winner = json.loads(winner_bytes)
            self.assertEqual(winner["install_id"], "race")
            self.assertIn(winner["to"], {str(target_a), str(target_b)})
            self.assertEqual(
                sorted(
                    path.name
                    for path in receipt.parent.iterdir()
                    if path.name != "receipt.json"
                ),
                [],
            )

            dangling = state / "promotions" / "dangling"
            dangling.mkdir(parents=True)
            missing_target = dangling / "missing-receipt.json"
            receipt_link = dangling / "receipt.json"
            receipt_link.symlink_to(missing_target)
            refused = subprocess.run(
                init_command("dangling", target_a),
                capture_output=True,
                text=True,
            )
            self.assertEqual(refused.returncode, 1, refused.stdout + refused.stderr)
            self.assertIn("preserve the existing promotion receipt", refused.stderr)
            self.assertTrue(receipt_link.is_symlink())
            self.assertEqual(Path(os.readlink(receipt_link)), missing_target)
            self.assertFalse(missing_target.exists())
            self.assertEqual(
                [path.name for path in dangling.iterdir()],
                ["receipt.json"],
            )

            linked_state = root / "linked-state"
            linked_state.mkdir()
            outside = root / "outside-promotions"
            outside.mkdir()
            (linked_state / "promotions").symlink_to(
                outside,
                target_is_directory=True,
            )
            escaped_init = subprocess.run(
                init_command("linked", target_a, linked_state),
                capture_output=True,
                text=True,
            )
            self.assertEqual(
                escaped_init.returncode,
                1,
                escaped_init.stdout + escaped_init.stderr,
            )
            self.assertIn("symlink ancestor", escaped_init.stderr)
            self.assertEqual(list(outside.iterdir()), [])

    def test_resume_failure_keeps_scheduling_and_admission_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path, env, admission, order, control = self._admission_fixture(Path(tmp))
            env["FIXTURE_RESUME_RC"] = "1"
            result = run(["bash", str(control), "start", "--config", str(config_path)], env)
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn(
                "the Windows endpoint did not resume under the active install identity; admission stays closed",
                result.stderr,
            )
            self.assertEqual(admission.read_text().strip(), "true")
            lines = order.read_text().splitlines()
            ingress = lines.index(
                "systemctl --user is-active --quiet description-portal.service description-operator-proxy.service"
            )
            resume = next(i for i, line in enumerate(lines) if "-Verb resume" in line)
            self.assertLess(ingress, resume)
            self.assertNotIn("airflow dags unpause solidworks_to_urdf", lines)
            self.assertNotIn("health-gate probe", lines)
            self.assertNotIn("platform open_admission", lines)

    def test_unpause_failure_keeps_admission_closed_after_resume(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path, env, admission, order, control = self._admission_fixture(Path(tmp), unpause_rc=7)
            result = run(["bash", str(control), "start", "--config", str(config_path)], env)
            self.assertEqual(result.returncode, 7, result.stdout + result.stderr)
            self.assertEqual(admission.read_text().strip(), "true")
            lines = order.read_text().splitlines()
            self.assertTrue([line for line in lines if "-Verb resume" in line])
            self.assertNotIn("health-gate probe", lines)
            self.assertNotIn("platform open_admission", lines)

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
            3,
            "the one gate serves the plain start, promote verify and rollback verify",
        )
        verify_anchor = control.index('--out "$EVIDENCE_DIR/final-identity.json"')
        gate_in_promote = control.index('bash "$HERE/health_gate.sh" "$EVIDENCE_DIR/linux-health-')
        open_in_promote = control.index('platform_open_admission > "$EVIDENCE_DIR/open-admission.out"')
        self.assertLess(verify_anchor, gate_in_promote, "the promote gate runs inside the verify step")
        self.assertLess(gate_in_promote, open_in_promote, "the promote opens only after the gate")
        start_gate = control.index('bash "$HERE/health_gate.sh" "$OPERATOR_STATE/health-gate/start-')
        self.assertLess(start_gate, control.rindex("platform_open_admission"), "the start opens only after the gate")
        rollback_identity = control.index('--out "$EVIDENCE_DIR/rollback-identity.json"')
        rollback_gate = control.index('bash "$HERE/health_gate.sh" \\\n        "$OPERATOR_STATE/health-gate/rollback-')
        rollback_dag_state = control.index(
            'airflow_restore_dag_state "$DAG_STATE_FILE"', rollback_gate
        )
        self.assertLess(rollback_identity, rollback_gate, "rollback health follows the restored identity")
        self.assertLess(rollback_gate, rollback_dag_state, "DAG state is restored only after health")

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

    def test_reconcile_exposes_the_supported_reported_stage_input(self) -> None:
        control = CONTROL.read_text(encoding="utf-8")
        self.assertIn("--reported-stage STAGE", control)
        self.assertIn('RECONCILE_REPORTED_STAGE="$REPORTED_STAGE"', control)
        self.assertIn('reconcile["reported_stage"] = reported_stage', control)

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

    def test_clean_shell_verbs_export_the_verified_generation_environment(self) -> None:
        """Canonical verbs replace clean-shell ambient decoys with the selected generation."""

        cases = (
            ("start", ["--keep-admission-closed"]),
            ("drain", []),
            ("stop", []),
            (
                "reconcile",
                [
                    "--run-id",
                    "run-1",
                    "--stage",
                    "capture",
                    "--outcome",
                    "completed",
                    "--evidence",
                    "receipt.json",
                ],
            ),
        )
        for action, arguments in cases:
            with self.subTest(action=action), tempfile.TemporaryDirectory() as tmp:
                config_path, env, _, _, control = self._admission_fixture(Path(tmp))
                env.update(
                    {
                        "PLATFORM_SERVICE_DSN": "postgresql://ambient@/wrong",
                        "PLATFORM_OPERATOR_DSN": "postgresql://ambient_ro@/wrong",
                        "PIPELINE_STORE_ROOT": "/ambient/store",
                        "PLATFORM_CONFIG_DIGEST": "f" * 64,
                        "AIRFLOW_VENV": "/ambient/venv",
                        "OPERATOR_STATE": "/ambient/state",
                    }
                )
                result = run(["bash", str(control), action, "--config", str(config_path), *arguments], env)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_missing_generation_value_is_refused_before_a_child_side_effect(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path, env, _, order, control = self._admission_fixture(root)
            derived = root / "state" / "derived" / "gen-1" / "operator.env"
            derived.write_text(
                "\n".join(
                    line
                    for line in derived.read_text(encoding="utf-8").splitlines()
                    if not line.startswith("PLATFORM_SERVICE_DSN=")
                )
                + "\n",
                encoding="utf-8",
            )
            result = run(["bash", str(control), "drain", "--config", str(config_path)], env)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("does not define PLATFORM_SERVICE_DSN", result.stderr)
            self.assertFalse(order.exists(), "the loader must refuse before invoking a child process")

    def test_failing_generation_environment_statement_fails_closed_before_a_child_side_effect(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path, env, _, order, control = self._admission_fixture(root)
            derived = root / "state" / "derived" / "gen-1" / "operator.env"
            derived.write_text(
                derived.read_text(encoding="utf-8") + "false\n",
                encoding="utf-8",
            )
            result = run(["bash", str(control), "drain", "--config", str(config_path)], env)
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn("unexpected EOF", result.stderr)
            self.assertFalse(order.exists(), "a failed environment load must never reach a child process")

    def test_generation_loader_preserves_optional_empty_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path, env, _, _, control = self._admission_fixture(root)
            derived = root / "state" / "derived" / "gen-1" / "operator.env"
            derived.write_text(
                derived.read_text(encoding="utf-8")
                + "FEISHU_APP_SECRET_FILE=\nFEISHU_TENANT_KEYS=\n",
                encoding="utf-8",
            )
            result = run(["bash", str(control), "drain", "--config", str(config_path)], env)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

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
                ["bash", str(CONTROL), "status", "--config", str(config_path)],
                status_env(Path(tmp), config_home),
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertNotIn("DRIFT", result.stdout)
            self.assertEqual(before, {path: digest(path) for path in before})

    def test_windows_status_probe_uses_encoded_command_and_rejects_bad_transport(self) -> None:
        control = CONTROL.read_text(encoding="utf-8")
        self.assertIn("export PYTHONDONTWRITEBYTECODE=1", control)
        self.assertIn('CONTROL_PYTHON="${CONTROL_PYTHON:-python3}"', control)
        self.assertIn("sys.implementation.name", control)
        self.assertIn("sys.version_info[:2] == (3, 12)", control)
        self.assertEqual(control.count("python3"), 1)
        transport = (OPERATOR / "windows_transport.py").read_text(encoding="utf-8")
        self.assertIn('"-EncodedCommand"', transport)
        self.assertIn("windows_transport.py", control)
        self.assertIn("windows_lifecycle()", control)
        self.assertIn("WINDOWS_LIFECYCLE_TIMEOUT=480", control)
        self.assertIn('windows_transport "$WINDOWS_LIFECYCLE_TIMEOUT"', control)
        self.assertIn('"$output.transport.json"', control)
        self.assertIn('2> "$output.stderr"', control)
        self.assertIn('[ "$status" != "completed" ]', control)
        self.assertIn("$python -I -B -c $program $configPath", control)
        self.assertIn("Test-Bytecode-TransitionGeneration", control)
        self.assertIn("Test-Transition-LaunchArguments", control)
        self.assertIn("140d39e51be5f0c39c32d7cfea9ca25fe642c273", control)
        self.assertNotIn("gzip.compress(script", control)
        self.assertNotIn('ssh -o BatchMode=yes "$SOLIDWORKS_SSH_HOST"', control)
        self.assertNotIn('ssh -o BatchMode=yes -o StrictHostKeyChecking=yes "$SOLIDWORKS_SSH_HOST"', control)
        self.assertNotIn('"-Command",\n            "-",', control)
        self.assertIn("$task.Actions).Count -ne 1", control)
        self.assertIn("$record.passed -isnot [bool]", control)
        self.assertIn("$record.launch_contract -isnot [string]", control)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path, state, config_home, _ = self._render_units(root)
            status_path = root / "windows-status.json"
            stderr_path = root / "windows-status.stderr"
            rc_path = root / "windows-status.rc"

            def status() -> subprocess.CompletedProcess:
                return run(
                    ["bash", str(CONTROL), "status", "--config", str(config_path)],
                    status_env(root, config_home),
                )

            baseline = status()
            self.assertEqual(baseline.returncode, 0, baseline.stdout + baseline.stderr)
            baseline_sample = json.loads(baseline.stdout.strip().splitlines()[-1])
            self.assertEqual(baseline_sample["task_state"], "Running")
            self.assertEqual(
                baseline_sample["transport"]["encoding"],
                "base64(gzip(utf-8))",
            )
            self.assertGreater(baseline_sample["transport"]["script_bytes"], 0)
            self.assertLess(
                baseline_sample["transport"]["launcher_bytes"],
                8_192,
            )
            self.assertEqual(
                len(baseline_sample["transport"]["payload_sha256"]),
                64,
            )
            status_evidence = baseline_sample["evidence"]
            for path in status_evidence.values():
                self.assertTrue(Path(path).is_file())
            status_metadata = json.loads(
                Path(status_evidence["metadata"]).read_text(encoding="utf-8")
            )
            self.assertEqual(
                status_metadata["schema_version"],
                "solidworks-to-urdf.windows-transport/v1",
            )
            self.assertEqual(status_metadata["returncode"], 0)
            self.assertGreater(status_metadata["stdout"]["bytes"], 0)
            self.assertEqual(len(status_metadata["stdout"]["sha256"]), 64)
            self.assertEqual(
                status_metadata["transport"]["framing"],
                "single-line-lf",
            )
            expected = json.loads((state / "install-record.json").read_text(encoding="utf-8"))["ends"]["windows"]
            raw_sample = {
                "task_name": baseline_sample["task_name"],
                "task_state": "Running",
                "action_count": 1,
                "listener_pid": baseline_sample["listener_pid"],
                "process_start_utc": baseline_sample["process_start_utc"],
                "install_id": baseline_sample["install_id"],
                "config_digest": baseline_sample["config_digest"],
                "release": expected["release"],
                "health_install_id": baseline_sample["install_id"],
                "health_config_digest": baseline_sample["config_digest"],
                "health_release": expected["release"],
                "launch_contract": baseline_sample["launch_contract"],
                "action_matches_generation": True,
            }
            raw_transport = json.dumps(
                {"passed": True, "sample": raw_sample},
                sort_keys=True,
            )

            status_path.write_text(
                "transport start\n" + raw_transport + "\n",
                encoding="utf-8",
            )
            multiline = status()
            self.assertEqual(multiline.returncode, 0, multiline.stdout + multiline.stderr)

            for name, content, expected in (
                ("empty", "", "Windows probe returned no JSON"),
                ("non-json", "not a JSON object\n", "Windows probe returned no JSON"),
                ("truncated", '{"passed":true\n', "Windows probe returned no JSON"),
            ):
                with self.subTest(name=name):
                    evidence_before = set(
                        (state / "health-gate").glob("windows-status-*.transport.json")
                    )
                    status_path.write_text(content, encoding="utf-8")
                    result = status()
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn(expected, result.stderr)
                    evidence_after = set(
                        (state / "health-gate").glob("windows-status-*.transport.json")
                    )
                    self.assertEqual(len(evidence_after - evidence_before), 1)
                    failed_metadata = json.loads(
                        next(iter(evidence_after - evidence_before)).read_text(
                            encoding="utf-8"
                        )
                    )
                    self.assertEqual(failed_metadata["status"], "error")
                    self.assertEqual(failed_metadata["error_type"], "InvalidJson")
                    self.assertEqual(failed_metadata["returncode"], 0)

            status_path.write_text(raw_transport + "\n", encoding="utf-8")
            stderr_path.write_bytes(b"\xd5\n")
            rc_path.write_text("1\n", encoding="ascii")
            invalid_stderr = status()
            self.assertNotEqual(invalid_stderr.returncode, 0)
            self.assertIn("Windows probe failed", invalid_stderr.stderr)
            self.assertNotIn("UnicodeDecodeError", invalid_stderr.stderr)

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
                status_env(Path(tmp), config_home),
            )
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
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
        self.assertIn("-m description_pipeline.orchestration.airflow_client", health)
        self.assertIn('--state-root "$OPERATOR_STATE"', health)
        self.assertNotIn("targets", health)
        self.assertNotIn("repositories", health)
        self.assertIn("/auth/feishu/health", health)
        self.assertIn("FEISHU_ENV_FILE", health)
        self.assertIn("FEISHU_APP_SECRET_FILE", health)
        self.assertIn('if status == 200 and body.get("configured") is True', health)
        self.assertNotIn("extra_dejson", health)
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
            control_env = status_env(Path(tmp), config_home)
            portal = target / "description-portal.service"
            portal.write_text(portal.read_text(encoding="utf-8") + "# drifted\n", encoding="utf-8")
            changed = run(["bash", str(CONTROL), "status", "--config", str(config_path)], control_env)
            self.assertNotEqual(changed.returncode, 0, changed.stdout + changed.stderr)
            self.assertIn("DRIFT unit differs: description-portal.service", changed.stdout)
            (target / "description-airflow-scheduler.service").unlink()
            missing = run(["bash", str(CONTROL), "status", "--config", str(config_path)], control_env)
            self.assertNotEqual(missing.returncode, 0, missing.stdout + missing.stderr)
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
                status_env(Path(tmp), config_home),
            )
            self.assertNotEqual(failed.returncode, 0, failed.stdout + failed.stderr)
            self.assertIn("DRIFT unit missing from render: description-portal.service", failed.stdout)
            self.assertIn("DRIFT unit missing from render: description-operator-proxy.service", failed.stdout)
            self.assertIn("provided TLS certificate or key is missing", failed.stderr)

    def test_status_requires_the_ready_windows_task_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path, state, config_home, _ = self._render_units(root)
            env = status_env(root, config_home)
            record = json.loads(
                (state / "install-record.json").read_text(encoding="utf-8")
            )
            identity = record["ends"]["windows"]
            for task_state in ("Ready", "Disabled", "Failed"):
                sample = {
                    "passed": True,
                    "sample": {
                        "task_name": "description-e2e-endpoint",
                        "task_state": task_state,
                        "action_count": 1,
                        "last_task_result": 267009,
                        "listener_pid": 4242,
                        "process_start_utc": "2026-10-21T00:00:00Z",
                        "install_id": identity["install_id"],
                        "config_digest": identity["config_digest"],
                        "release": identity["release"],
                        "health_install_id": identity["install_id"],
                        "health_config_digest": identity["config_digest"],
                        "health_release": identity["release"],
                        "launch_contract": "python_module_v1",
                        "action_matches_generation": True,
                    },
                }
                (root / "windows-status.json").write_text(
                    json.dumps(sample),
                    encoding="utf-8",
                )
                result = run(
                    [
                        "bash",
                        str(CONTROL),
                        "status",
                        "--config",
                        str(config_path),
                    ],
                    env,
                )
                self.assertNotEqual(
                    result.returncode,
                    0,
                    result.stdout + result.stderr,
                )

            for label, changes in (
                ("wrong-contract", {"launch_contract": "foreign_launcher_v1"}),
                ("contract-case", {"launch_contract": "Python_Module_V1"}),
                ("action-mismatch", {"action_matches_generation": False}),
                ("multi-action", {"action_count": 2}),
                ("boolean-action-count", {"action_count": True}),
                ("float-action-count", {"action_count": 1.0}),
            ):
                with self.subTest(label=label):
                    sample = {
                        "passed": True,
                        "sample": {
                            "task_name": "description-e2e-endpoint",
                            "task_state": "Running",
                            "action_count": 1,
                            "listener_pid": 4242,
                            "process_start_utc": "2026-10-21T00:00:00Z",
                            "install_id": identity["install_id"],
                            "config_digest": identity["config_digest"],
                            "release": identity["release"],
                            "health_install_id": identity["install_id"],
                            "health_config_digest": identity["config_digest"],
                            "health_release": identity["release"],
                            "launch_contract": "python_module_v1",
                            "action_matches_generation": True,
                        },
                    }
                    sample["sample"].update(changes)
                    (root / "windows-status.json").write_text(
                        json.dumps(sample),
                        encoding="utf-8",
                    )
                    result = run(
                        [
                            "bash",
                            str(CONTROL),
                            "status",
                            "--config",
                            str(config_path),
                        ],
                        env,
                    )
                    self.assertNotEqual(result.returncode, 0)

            (root / "windows-status.json").write_text(
                json.dumps(
                    {
                        "passed": False,
                        "error": "scheduled task is missing",
                    }
                ),
                encoding="utf-8",
            )
            missing = run(
                [
                    "bash",
                    str(CONTROL),
                    "status",
                    "--config",
                    str(config_path),
                ],
                env,
            )
            self.assertNotEqual(
                missing.returncode,
                0,
                missing.stdout + missing.stderr,
            )
            control = CONTROL.read_text(encoding="utf-8")
            self.assertNotIn("LastTaskResult -ne 0", control)
            self.assertIn("$task.State -ne 'Running'", control)

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
        for module in (
            "platform_config.py",
            "platform_env.py",
            "install_record.py",
            "promotion.py",
            "backup.py",
            "runtime_dag.py",
        ):
            (release / "operator" / module).write_bytes((OPERATOR / module).read_bytes())
        for item in OPERATOR.iterdir():
            destination = release / "operator" / item.name
            if item.is_file() and not destination.exists():
                shutil.copyfile(item, destination)
        write_stub(
            release / "operator" / "runtime_dag.py",
            "#!/usr/bin/env python3\n"
            "import os\n"
            "print('runtime-dag-check')\n"
            "raise SystemExit(int(os.environ.get('RUNTIME_DAG_RC', '0')))\n",
        )
        for name in ("render_config.py", "airflow.cfg.template"):
            (release / "airflow" / name).write_bytes((OPERATOR.parent / "airflow" / name).read_bytes())
        dag_content = b"# dag fixture\n"
        (release / "airflow" / "dags" / "solidworks_to_urdf.py").write_bytes(dag_content)
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
        (release / "airflow" / "services.sh.real").write_bytes(
            (ROOT / "deploy" / "airflow" / "services.sh").read_bytes()
        )
        (release / "systemd-units.tsv").write_bytes(
            (ROOT / "deploy" / "systemd-units.tsv").read_bytes()
        )
        write_stub(
            release / "airflow" / "services.sh",
            "#!/usr/bin/env bash\n"
            'if [ "${1:-}" = "units" ]; then\n'
            '  exec bash "$(dirname "$0")/services.sh.real" "$@"\n'
            "fi\n"
            'echo "services_$1" >> "$ORDER_LOG"\n'
            'mkdir -p "$SYSTEMCTL_STATE_DIR"\n'
            'execution_units=(\n'
            "  description-solidworks-tunnel\n"
            "  description-airflow-dag-processor\n"
            "  description-airflow-scheduler\n"
            "  description-airflow-api-server\n"
            ")\n"
            'stop_runtime() {\n'
            '  local unit="$1" pidfile="$SYSTEMCTL_STATE_DIR/$1.pid" pid\n'
            '  [ -f "$pidfile" ] || return 0\n'
            '  pid="$(cat "$pidfile")"\n'
            '  kill "$pid" 2>/dev/null || true\n'
            '  rm -f "$pidfile"\n'
            '}\n'
            'spawn_runtime() {\n'
            '  local unit="$1" marker pidfile\n'
            '  [ "${FIXTURE_RUNTIME_PROCESS:-0}" = "1" ] || return 0\n'
            '  case "$unit" in description-airflow-*) ;; *) return 0 ;; esac\n'
            '  stop_runtime "$unit"\n'
            '  mkdir -p "$AIRFLOW_VENV/lib"\n'
            '  marker="$AIRFLOW_VENV/lib/runtime-$unit.map"\n'
            '  printf "runtime map for %s\\n" "$unit" > "$marker"\n'
            '  FIXTURE_RUNTIME_MARKER="$marker" python3 -c '
            '\'import mmap,os,time; f=open(os.environ["FIXTURE_RUNTIME_MARKER"],"r+b"); '
            'm=mmap.mmap(f.fileno(),0,access=mmap.ACCESS_READ); time.sleep(300)\' '
            '</dev/null >/dev/null 2>&1 &\n'
            '  pidfile="$SYSTEMCTL_STATE_DIR/$unit.pid"\n'
            '  echo $! > "$pidfile"\n'
            '}\n'
            'case "$1" in\n'
            "  render)\n"
            '    target="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"\n'
            '    mkdir -p "$target"\n'
            '    echo "services_render venv=$AIRFLOW_VENV derived=$OPERATOR_DERIVED_ENV" >> "$ORDER_LOG"\n'
            '    for unit in description-airflow-dag-processor description-airflow-scheduler '
            'description-airflow-api-server; do\n'
            '      printf "[Service]\\nEnvironmentFile=%s\\nExecStart=%s/bin/airflow fixture\\n" '
            '"$OPERATOR_DERIVED_ENV" "$AIRFLOW_VENV" > "$target/$unit.service"\n'
            '    done\n'
            "    exit 0 ;;\n"
            "  stop|stop-executors)\n"
            '    [ "$1" = "stop" ] && printf inactive > "$SYSTEMCTL_STATE_DIR/description-postgres"\n'
            '    for unit in "${execution_units[@]}"; do\n'
            '      if [ "$1" = "stop-executors" ] && [ "$unit" = "${FIXTURE_LEAVE_READER_ACTIVE:-}" ]; then\n'
            '        printf active > "$SYSTEMCTL_STATE_DIR/$unit"\n'
            '        continue\n'
            '      fi\n'
            '      stop_runtime "$unit"\n'
            '      printf inactive > "$SYSTEMCTL_STATE_DIR/$unit"\n'
            "    done\n"
            '    exit "${FIXTURE_FAIL_STOP:-0}" ;;\n'
            "  start)\n"
            '    for unit in description-postgres "${execution_units[@]}"; do\n'
            '      printf active > "$SYSTEMCTL_STATE_DIR/$unit"\n'
            '      spawn_runtime "$unit"\n'
            "    done\n"
            '    exit "${FIXTURE_FAIL_START:-0}" ;;\n'
            "esac\n"
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
        write_stub(
            release / "airflow" / "scripts" / "add_connection.py",
            "#!/usr/bin/env python3\n"
            "import os\n"
            "with open(os.environ['ORDER_LOG'], 'a', encoding='utf-8') as stream:\n"
            "    stream.write('add_connection\\n')\n"
            "print('connection solidworks_windows bound')\n",
        )
        write_stub(
            release / "operator" / "connection_snapshot.py",
            "#!/usr/bin/env python3\n"
            "import argparse\n"
            "import json\n"
            "import os\n"
            "import stat\n"
            "from pathlib import Path\n"
            "parser = argparse.ArgumentParser()\n"
            "parser.add_argument('action', choices=('capture', 'restore', 'verify'))\n"
            "parser.add_argument('--conn-id', default='solidworks_windows')\n"
            "parser.add_argument('--file', type=Path, required=True)\n"
            "parser.add_argument('--receipt', type=Path, required=True)\n"
            "parser.add_argument('--manifest', type=Path)\n"
            "parser.add_argument('--sealed', type=Path)\n"
            "parser.add_argument('--member')\n"
            "args = parser.parse_args()\n"
            "log = Path(os.environ['ORDER_LOG'])\n"
            "if args.action == 'capture':\n"
            "    args.file.parent.mkdir(parents=True, exist_ok=True)\n"
            "    os.chmod(args.file.parent, 0o700)\n"
            "    descriptor = os.open(args.file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)\n"
            "    with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:\n"
            "        json.dump({'password': 'fixture-secret', 'fields': 'complete'}, stream)\n"
            "    with log.open('a', encoding='utf-8') as stream:\n"
            "        stream.write('connection_capture\\n')\n"
            "    print('connection row captured privately')\n"
            "else:\n"
            "    if args.manifest is None or args.sealed is None:\n"
            "        raise SystemExit('restore requires manifest and sealed archive')\n"
            "    if not args.manifest.is_file() or not args.sealed.is_file():\n"
            "        raise SystemExit('restore binding is missing')\n"
            "    postgres = Path(os.environ['SYSTEMCTL_STATE_DIR']) / 'description-postgres'\n"
            "    if not postgres.is_file() or postgres.read_text() != 'active':\n"
            "        raise SystemExit('restore requires the shared PostgreSQL to be running')\n"
            "    if stat.S_IMODE(args.file.stat().st_mode) != 0o600:\n"
            "        raise SystemExit('capture mode is not 0600')\n"
            "    with log.open('a', encoding='utf-8') as stream:\n"
            "        stream.write('connection_restore\\n')\n"
            "    print('connection row restored exactly')\n",
        )

        (tmp / "releases" / "airflow-venv-gen-1" / "bin").mkdir(parents=True)
        write_stub(
            tmp / "releases" / "airflow-venv-gen-1" / "bin" / "python",
            "#!/usr/bin/env bash\n"
            "case \"$*\" in\n"
            "  *install_record.py*|*add_connection.py*|*connection_snapshot.py*)\n"
            '    exec python3 "$@" ;;\n'
            "  -c*interface_records.release_domain_identity*)\n"
            "    python3 -c 'import json,os; "
            "domain={\"contract\":{\"fixture\":True},\"support\":{\"fixture\":True}}; "
            "print(json.dumps({\"root\":os.environ[\"AIRFLOW_VENV\"],"
            "\"pipeline_id\":\"solidworks-to-urdf\",\"domain\":domain,"
            "\"identity\":{\"pipeline_id\":\"solidworks-to-urdf\","
            "\"version\":\"1.4.0\",\"source_sha256\":\"a\"*64,"
            "\"contract\":domain[\"contract\"],\"support\":domain[\"support\"]}}))' ;;\n"
            '  "-m description_pipeline.orchestration.lifecycle_state"*)\n'
            "    python3 -c 'import json,os; from pathlib import Path; "
            'Path(os.environ["LIFECYCLE_ENV_LOG"]).write_text(json.dumps('
            '{"dsn": os.environ.get("PLATFORM_OPERATOR_DSN"), '
            '"expected_dsn": os.environ["EXPECTED_PLATFORM_DSN"], '
            '"venv": os.environ.get("AIRFLOW_VENV")}))\'\n'
            '    [ "${PLATFORM_OPERATOR_DSN:-}" = "$EXPECTED_PLATFORM_DSN" ] || exit 77\n'
            '    case "$*" in\n'
            "      *--require-admission-closed*)\n"
            '        [ "$(cat "$ADMISSION" 2>/dev/null)" = "true" ] || exit 1 ;;\n'
            "    esac\n"
            '    if [ -n "$PROJECTION_FILE" ] && [ -f "$PROJECTION_FILE" ]; then cat "$PROJECTION_FILE"; fi\n'
            '    exit "${PROJECTION_RC:-0}" ;;\n'
            "  *-c*description_pipeline.orchestration.portal*description_pipeline.orchestration.feishu_auth*)\n"
            '    echo "module_check" >> "$ORDER_LOG"\n'
            '    exit "${MODULE_CHECK_RC:-0}" ;;\n'
            "  *.py|*.py\\ *)\n"
            '    exec python3 "$@" ;;\n'
            "esac\n"
            'script="$(cat)"\n'
            'case "$script" in\n'
            "  *airflow-dag-capture*)\n"
            '    paused="$(cat "$ADMISSION")"\n'
            "    printf '{\"dag_id\":\"solidworks_to_urdf\",\"paused\":%s}\\n' \"$paused\" > \"$2\"\n"
            '    echo "dag_capture paused=$paused" >> "$ORDER_LOG" ;;\n'
            "  *airflow-dag-state-check*)\n"
            '    expected="$2"\n'
            '    actual="$(cat "$ADMISSION")"\n'
            '    echo "dag_state_check expected=$expected actual=$actual" >> "$ORDER_LOG"\n'
            '    [ "$actual" = "$expected" ] || exit 1 ;;\n'
            "  *platform-close-admission*)\n"
            '    [ "$PLATFORM_SERVICE_DSN" = "$EXPECTED_PLATFORM_SERVICE_DSN" ] || exit 78\n'
            '    [ "$PLATFORM_OPERATOR_DSN" = "$EXPECTED_PLATFORM_DSN" ] || exit 77\n'
            '    [ "$PIPELINE_STORE_ROOT" = "$EXPECTED_PIPELINE_STORE_ROOT" ] || exit 79\n'
            '    echo true > "$ADMISSION"\n'
            '    echo "platform close_admission" >> "$ORDER_LOG"\n'
            '    echo "admission=closed revision=2" ;;\n'
            "  *platform-open-admission*)\n"
            '    [ "$PLATFORM_SERVICE_DSN" = "$EXPECTED_PLATFORM_SERVICE_DSN" ] || exit 78\n'
            '    [ "$PLATFORM_OPERATOR_DSN" = "$EXPECTED_PLATFORM_DSN" ] || exit 77\n'
            '    [ "$PIPELINE_STORE_ROOT" = "$EXPECTED_PIPELINE_STORE_ROOT" ] || exit 79\n'
            '    echo false > "$ADMISSION"\n'
            '    echo "platform open_admission" >> "$ORDER_LOG"\n'
            '    echo "admission=open revision=3" ;;\n'
            "  *platform-reconcile*)\n"
            '    [ -n "${RECONCILE_RUN_ID:-}" ] && [ -n "${RECONCILE_OUTCOME:-}" ] || exit 61\n'
            '    [ "$PLATFORM_SERVICE_DSN" = "$EXPECTED_PLATFORM_SERVICE_DSN" ] || exit 78\n'
            '    [ "$PLATFORM_OPERATOR_DSN" = "$EXPECTED_PLATFORM_DSN" ] || exit 77\n'
            '    [ "$PIPELINE_STORE_ROOT" = "$EXPECTED_PIPELINE_STORE_ROOT" ] || exit 79\n'
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
            '  "dags unpause solidworks_to_urdf")\n'
            '    [ "${UNPAUSE_RC:-0}" = "0" ] || exit "$UNPAUSE_RC"\n'
            '    echo false > "$ADMISSION" ;;\n'
            '  "dags pause solidworks_to_urdf")\n'
            '    [ "${PAUSE_RC:-0}" = "0" ] || exit "$PAUSE_RC"\n'
            '    echo true > "$ADMISSION" ;;\n'
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
                    "source_tree": "d" * 40,
                    "source_sha256": "a" * 64,
                    "artifacts": {
                        wheel.name: hashlib.sha256(wheel.read_bytes()).hexdigest(),
                        windows_archive.name: hashlib.sha256(windows_archive.read_bytes()).hexdigest(),
                        "description-1.4.0-linux-cp312-x86_64.zip": hashlib.sha256(b"linux-archive").hexdigest(),
                        "description-1.4.0-deployment.zip": hashlib.sha256(b"deployment-archive").hexdigest(),
                    },
                    "deployment_files": {
                        "deploy/airflow/dags/solidworks_to_urdf.py": hashlib.sha256(
                            dag_content
                        ).hexdigest(),
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
            'if [ "${1:-}" = "--user" ] && [ "${2:-}" = "show" ]; then\n'
            '  for argument in "$@"; do unit="${argument%.service}"; done\n'
            '  cat "$SYSTEMCTL_STATE_DIR/$unit.pid" 2>/dev/null || echo 0\n'
            '  exit 0\n'
            'fi\n'
            'stop_runtime() {\n'
            '  local unit="$1" pidfile="$SYSTEMCTL_STATE_DIR/$1.pid" pid\n'
            '  [ -f "$pidfile" ] || return 0\n'
            '  pid="$(cat "$pidfile")"\n'
            '  kill "$pid" 2>/dev/null || true\n'
            '  rm -f "$pidfile"\n'
            '}\n'
            'spawn_runtime() {\n'
            '  local unit="$1" marker pidfile\n'
            '  [ "${FIXTURE_RUNTIME_PROCESS:-0}" = "1" ] || return 0\n'
            '  case "$unit" in description-airflow-*) ;; *) return 0 ;; esac\n'
            '  stop_runtime "$unit"\n'
            '  mkdir -p "$AIRFLOW_VENV/lib"\n'
            '  marker="$AIRFLOW_VENV/lib/runtime-$unit.map"\n'
            '  printf "runtime map for %s\\n" "$unit" > "$marker"\n'
            '  FIXTURE_RUNTIME_MARKER="$marker" python3 -c '
            '\'import mmap,os,time; f=open(os.environ["FIXTURE_RUNTIME_MARKER"],"r+b"); '
            'm=mmap.mmap(f.fileno(),0,access=mmap.ACCESS_READ); time.sleep(300)\' '
            '</dev/null >/dev/null 2>&1 &\n'
            '  pidfile="$SYSTEMCTL_STATE_DIR/$unit.pid"\n'
            '  echo $! > "$pidfile"\n'
            '}\n'
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
            '      spawn_runtime "$unit"\n'
            '    done ;;\n'
            '  stop|disable)\n'
            '    for unit in "${units[@]}"; do\n'
            '      if [ -n "${FIXTURE_STOP_REFUSES:-}" ] && [ "$unit" = "$FIXTURE_STOP_REFUSES" ]; then exit 1; fi\n'
            '      stop_runtime "$unit"\n'
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
        write_stub(
            bindir / "scp",
            '#!/usr/bin/env bash\n'
            'echo "scp $*" >> "$ORDER_LOG"\n'
            'src=""\n'
            'for argument in "$@"; do case "$argument" in *.zip) src="$argument" ;; esac; done\n'
            'dest="${FIXTURE_REMOTE_ARCHIVE:-}"\n'
            'gen=""\n'
            'case "$*" in\n'
            '  *"/lifecycle/gen-1/"*) gen="gen-1"; dest="${FIXTURE_REMOTE_ARCHIVE_GEN1:-$dest}" ;;\n'
            '  *"/lifecycle/gen-2/"*) gen="gen-2"; dest="${FIXTURE_REMOTE_ARCHIVE_GEN2:-$dest}" ;;\n'
            'esac\n'
            'if [ -n "$gen" ] && [ -z "$dest" ] && [ -n "$src" ]; then\n'
            '  dest="$FIXTURE_REMOTE_ARCHIVE_DIR/$gen/$(basename "$src")"\n'
            'fi\n'
            'if [ -n "$dest" ] && [ -n "$src" ]; then\n'
            '  mkdir -p "$(dirname "$dest")"\n'
            '  cp "$src" "$dest"\n'
            '  chmod 0444 "$dest"\n'
            'fi\n'
            'exit 0\n',
        )
        write_stub(
            bindir / "ssh",
            '#!/usr/bin/env bash\n'
            'stdin_payload="$(cat; printf x)"\n'
            'stdin_payload="${stdin_payload%x}"\n'
            'decoded_script="$(printf "%s" "$stdin_payload" | python3 -c \''
            'import base64,gzip,sys\n'
            'raw=sys.stdin.buffer.read()\n'
            'if raw and (raw.count(b"\\n") != 1 or not raw.endswith(b"\\n")): raise SystemExit(3)\n'
            'd=raw.strip()\n'
            'print(gzip.decompress(base64.b64decode(d)).decode("utf-8")'
            '.replace(chr(92), chr(47)) if d else "")'
            '\' 2>/dev/null || true)"\n'
            'echo "ssh $* $(printf "%s" "$decoded_script" | tr "\\n" " ")" >> "$ORDER_LOG"\n'
            'case "$* $decoded_script" in\n'
            '  *"two status probes returned different"*)\n'
            '    python3 - "$FIXTURE_OPERATOR_STATE/install-record.json" <<\'PY\'\n'
            "import json\n"
            "import os\n"
            "import sys\n"
            "record = json.load(open(sys.argv[1], encoding='utf-8'))\n"
            "identity = record['ends']['windows']\n"
            "release = identity['release']\n"
            "sample = {\n"
            "    'task_name': 'description-e2e-endpoint',\n"
            "    'task_state': 'Running',\n"
            "    'action_count': 1,\n"
            "    'listener_pid': 4242,\n"
            "    'process_start_utc': '2026-10-21T00:00:00Z',\n"
            "    'install_id': identity['install_id'],\n"
            "    'config_digest': identity['config_digest'],\n"
            "    'release': release,\n"
            "    'health_install_id': identity['install_id'],\n"
            "    'health_config_digest': identity['config_digest'],\n"
            "    'health_release': release,\n"
            "    'launch_contract': 'python_module_v1',\n"
            "    'action_matches_generation': True,\n"
            "}\n"
            "if os.environ.get('FIXTURE_STATUS') == 'foreign':\n"
            "    sample['install_id'] = 'win-' + 'f' * 32\n"
            "    sample['health_install_id'] = sample['install_id']\n"
            "print(json.dumps({'passed': True, 'sample': sample}, sort_keys=True))\n"
            "PY\n"
            "    ;;\n"
            '  *"Get-FileHash"*)\n'
            '    file="${FIXTURE_REMOTE_ARCHIVE:-}"\n'
            '    gen=""\n'
            '    case "$* $decoded_script" in\n'
            '      *"/lifecycle/gen-1/"*) gen="gen-1"; file="${FIXTURE_REMOTE_ARCHIVE_GEN1:-$file}" ;;\n'
            '      *"/lifecycle/gen-2/"*) gen="gen-2"; file="${FIXTURE_REMOTE_ARCHIVE_GEN2:-$file}" ;;\n'
            '    esac\n'
            '    if [ -z "$file" ] && [ -n "$gen" ]; then\n'
            '      file="$(find "$FIXTURE_REMOTE_ARCHIVE_DIR/$gen" -maxdepth 1 -type f '
            '-name "*.zip" -print -quit 2>/dev/null)"\n'
            '    fi\n'
            '    if [ -n "$file" ] && [ -f "$file" ]; then\n'
            '      printf "%s|%s\\n" "$(stat -c %s "$file")" \\\n'
            '        "$(sha256sum "$file" | cut -d" " -f1)"\n'
            '    else echo absent; fi ;;\n'
            "  *\"-Verb stage\"*)\n"
            '    if [ -n "${FIXTURE_STAGE_OUTPUT:-}" ]; then\n'
            '      cat "$FIXTURE_STAGE_OUTPUT"; exit "${FIXTURE_STAGE_RC:-0}"\n'
            '    fi\n'
            "    echo '{\"passed\":true,\"verb\":\"stage\"}' ;;\n"
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
            "  *\"-Verb health\"*)\n"
            '    case "$* $decoded_script" in\n'
            '      *"/lifecycle/gen-1/"*binding.json*)\n'
            '        cat "${FIXTURE_HEALTH_FILE_PREVIOUS:-$FIXTURE_HEALTH_FILE}" ;;\n'
            '      *"/lifecycle/gen-2/windows-health-promote-verify-binding.json"*)\n'
            '        cat "${FIXTURE_HEALTH_FILE_VERIFY:-${FIXTURE_HEALTH_FILE_CANDIDATE:-$FIXTURE_HEALTH_FILE}}" ;;\n'
            '      *"/lifecycle/gen-2/"*binding.json*)\n'
            '        cat "${FIXTURE_HEALTH_FILE_CANDIDATE:-$FIXTURE_HEALTH_FILE}" ;;\n'
            '      *) cat "$FIXTURE_HEALTH_FILE" ;;\n'
            "    esac ;;\n"
            "  *\"-Verb status\"*|*\"two status probes returned different\"*)\n"
            '    if [ "${FIXTURE_STATUS:-switched}" = "before" ]; then\n'
            "      echo '{\"passed\":true,\"verb\":\"status\",\"live_task\":"
            "{\"execute\":\"C:/original-install with spaces/runtime/Scripts/description.exe\","
            "\"arguments\":\"serve --config \\\"C:/original-install with spaces/endpoint.json\\\"\","
            "\"working_directory\":\"C:/description-e2e/state\","
            "\"action_count\":1,\"launch_contract\":\"legacy_pip_launcher_v0\","
            "\"action_matches_generation\":true,"
            "\"config\":\"C:/original-install with spaces/endpoint.json\",\"state\":\"Running\"},"
            "\"pending\":{\"intent\":true,\"verb\":\"promote\","
            "\"generation\":\"C:/description-e2e/state/lifecycle/gen-2/generation\","
            "\"previous\":\"C:/description-e2e/state/instance-promotions/previous-0001\","
            "\"source_launcher\":\"C:/original-install with spaces/runtime/Scripts/description.exe\","
            "\"source_arguments\":\"serve --config \\\"C:/original-install with spaces/endpoint.json\\\"\","
            "\"source_working_directory\":\"C:/description-e2e/state\","
            "\"source_config\":\"C:/original-install with spaces/endpoint.json\"},"
            "\"last_promotion\":null}'\n"
            '    elif [ "${FIXTURE_STATUS:-switched}" = "foreign" ]; then\n'
            "      echo '{\"passed\":true,\"verb\":\"status\",\"live_task\":"
            "{\"execute\":\"C:/foreign/not-description.exe\","
            "\"arguments\":\"foreign-service --option x\","
            "\"working_directory\":\"C:/description-e2e/state\","
            "\"action_count\":1,\"launch_contract\":null,"
            "\"action_matches_generation\":false,"
            "\"config\":\"C:/foreign/foreign.json\",\"state\":\"Running\"},"
            "\"pending\":null,\"last_promotion\":null}'\n"
            "    else\n"
            "      echo '{\"passed\":true,\"verb\":\"status\",\"live_task\":"
            "{\"execute\":\"C:/description-e2e/state/lifecycle/gen-2/generation/runtime/Scripts/python.exe\","
            "\"arguments\":\"-I -B -m description_pipeline.orchestration.windows serve --config "
            "\\\"C:/description-e2e/state/lifecycle/gen-2/generation/endpoint.json\\\"\","
            "\"working_directory\":\"C:/description-e2e/state\","
            "\"action_count\":1,\"launch_contract\":\"python_module_v1\","
            "\"action_matches_generation\":true,"
            "\"config\":\"C:/description-e2e/state/lifecycle/gen-2/generation/endpoint.json\","
            "\"state\":\"Running\"},"
            "\"pending\":{\"intent\":true,\"verb\":\"promote\","
            "\"generation\":\"C:/description-e2e/state/lifecycle/gen-2/generation\","
            "\"previous\":\"C:/description-e2e/state/instance-promotions/previous-0001\","
            "\"source_launcher\":\"C:/original-install with spaces/runtime/Scripts/description.exe\","
            "\"source_arguments\":\"serve --config \\\"C:/original-install with spaces/endpoint.json\\\"\","
            "\"source_working_directory\":\"C:/description-e2e/state\","
            "\"source_config\":\"C:/original-install with spaces/endpoint.json\"},"
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
            "FIXTURE_REMOTE_ARCHIVE_DIR": str(tmp / "remote-archives"),
            "FIXTURE_STUB_PYTHON": str(tmp / "releases" / "airflow-venv-gen-1" / "bin" / "python"),
            "FIXTURE_OPERATOR_STATE": str(tmp / "state"),
            "PROJECTION_FILE": str(tmp / "projection.json"),
            "LIFECYCLE_ENV_LOG": str(tmp / "query-environment.json"),
        }
        return release, control_env, order

    def _install(self, release: Path, control_env: dict[str, str]):
        tree = release.parent
        result = run(
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
        state = tree.parent / "state"
        derived = state / "derived" / "gen-1" / "operator.env"
        if derived.is_file():
            values = {}
            for line in derived.read_text(encoding="utf-8").splitlines():
                key, _, raw = line.partition("=")
                if key:
                    values[key] = decode_env_value(raw.strip())
            control_env.update(
                EXPECTED_PLATFORM_DSN=values["PLATFORM_OPERATOR_DSN"],
                EXPECTED_PLATFORM_SERVICE_DSN=values["PLATFORM_SERVICE_DSN"],
                EXPECTED_PIPELINE_STORE_ROOT=values["PIPELINE_STORE_ROOT"],
            )
        return result

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
            for verb, expect_failure in (("status", True), ("health", True)):
                with self.subTest(verb=verb):
                    order.write_text("", encoding="utf-8")
                    before = tree_state(release)
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
                    self.assertEqual(
                        before,
                        tree_state(release),
                        f"{verb} wrote into the deployment tree",
                    )
                    self.assertEqual(
                        list((scratch / "tmp").iterdir()), [], f"{verb} left temporary files behind"
                    )
                    calls = order.read_text(encoding="utf-8")
                    self.assertNotIn("proxy-installer-invoked", calls, f"{verb} ran the installer")
                    self.assertNotIn("proxy-prepare-invoked", calls, f"{verb} ran the preparation")
                    self.assertFalse(binary.exists(), f"{verb} recreated the removed binary")

    def test_early_windows_stage_failure_creates_hashed_receipt_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            release, control_env, _ = self._prepare(
                Path(tmp), fresh=False, ready_after=1, db_exists=True
            )
            self.assertEqual(self._install(release, control_env).returncode, 0)
            state = Path(tmp) / "state"
            control_env["EXPECTED_PLATFORM_DSN"] = self._derived_value(
                state, "gen-1", "PLATFORM_OPERATOR_DSN"
            )
            self._health_double(release, control_env)
            (release.parent / "description-1.4.0-windows-cp312-x86_64.zip").unlink()
            staged = self._maintenance(release, control_env, "stage", "gen-2")
            self.assertNotEqual(staged.returncode, 0)
            receipt = json.loads(
                (state / "promotions" / "gen-2" / "receipt.json").read_text(
                    encoding="utf-8"
                )
            )
            windows_failure = next(
                row
                for row in receipt["steps"]
                if row["verb"] == "promote" and row["step"] == "windows-generation"
            )
            self.assertEqual(windows_failure["status"], "failed")
            self.assertTrue(windows_failure["evidence"])
            for entry in windows_failure["evidence_digests"]:
                path = Path(entry["path"])
                self.assertTrue(path.is_file())
                self.assertEqual(
                    hashlib.sha256(path.read_bytes()).hexdigest(),
                    entry["sha256"],
                )
            promotion_root = state / "promotions" / "gen-2"
            files_before_retry = {
                path.relative_to(state).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in promotion_root.rglob("*")
                if path.is_file()
            }
            retry = self._maintenance(release, control_env, "stage", "gen-2")
            self.assertNotEqual(retry.returncode, 0)
            files_after_retry = {
                path.relative_to(state).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in promotion_root.rglob("*")
                if path.is_file()
            }
            self.assertEqual(files_after_retry, files_before_retry)
            self.assertFalse((promotion_root / "candidate-wheel.selection").exists())
            snapshot_path = Path(receipt["to"])
            snapshot_path.unlink()
            files_after_delete = {
                path.relative_to(state).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in promotion_root.rglob("*")
                if path.is_file()
            }
            retry_without_snapshot = self._maintenance(
                release, control_env, "stage", "gen-2"
            )
            self.assertNotEqual(retry_without_snapshot.returncode, 0)
            self.assertIn("preserves the existing promotion receipt", retry_without_snapshot.stderr)
            self.assertFalse(snapshot_path.exists())
            files_after_missing_snapshot_retry = {
                path.relative_to(state).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in promotion_root.rglob("*")
                if path.is_file()
            }
            self.assertEqual(files_after_missing_snapshot_retry, files_after_delete)

    def test_stage_refuses_the_active_generation_before_writes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            release, control_env, _ = self._prepare(
                Path(tmp), fresh=False, ready_after=1, db_exists=True
            )
            self.assertEqual(self._install(release, control_env).returncode, 0)
            state = Path(tmp) / "state"
            tracked = {
                "active-record": (state / "install-record.json").read_bytes(),
                "snapshot": (state / "snapshots" / "gen-1.json").read_bytes(),
                "derived": (
                    state / "derived" / "gen-1" / "operator.env"
                ).read_bytes(),
                "runtime": (
                    Path(tmp) / "releases" / "airflow-venv-gen-1" / "bin" / "python"
                ).read_bytes(),
            }
            refused = self._maintenance(release, control_env, "stage", "gen-1")
            self.assertNotEqual(refused.returncode, 0)
            self.assertIn("cannot target the active generation", refused.stderr)
            self.assertEqual(
                tracked,
                {
                    "active-record": (state / "install-record.json").read_bytes(),
                    "snapshot": (state / "snapshots" / "gen-1.json").read_bytes(),
                    "derived": (
                        state / "derived" / "gen-1" / "operator.env"
                    ).read_bytes(),
                    "runtime": (
                        Path(tmp) / "releases" / "airflow-venv-gen-1" / "bin" / "python"
                    ).read_bytes(),
                },
            )
            self.assertFalse((state / "promotions" / "gen-1").exists())

    def test_stage_refuses_a_historical_generation_before_writes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            release, control_env, _ = self._prepare(
                Path(tmp), fresh=False, ready_after=1, db_exists=True
            )
            self.assertEqual(self._install(release, control_env).returncode, 0)
            state = Path(tmp) / "state"
            control_env["EXPECTED_PLATFORM_DSN"] = self._derived_value(
                state, "gen-1", "PLATFORM_OPERATOR_DSN"
            )
            self._health_double(release, control_env)
            self.assertEqual(
                self._maintenance(release, control_env, "stage", "gen-2").returncode,
                0,
            )
            self._clone_generation_stubs(state)
            self._linux_health_double(release)
            self.assertEqual(
                self._maintenance(release, control_env, "promote", "gen-2").returncode,
                0,
            )
            tracked = {
                "record": (state / "install-records" / "gen-1.json").read_bytes(),
                "snapshot": (state / "snapshots" / "gen-1.json").read_bytes(),
                "derived": (
                    state / "derived" / "gen-1" / "operator.env"
                ).read_bytes(),
                "runtime": (
                    Path(tmp) / "releases" / "airflow-venv-gen-1" / "bin" / "python"
                ).read_bytes(),
            }
            refused = self._maintenance(release, control_env, "stage", "gen-1")
            self.assertNotEqual(refused.returncode, 0)
            self.assertIn("reused historical generation", refused.stderr)
            self.assertEqual(
                tracked,
                {
                    "record": (state / "install-records" / "gen-1.json").read_bytes(),
                    "snapshot": (state / "snapshots" / "gen-1.json").read_bytes(),
                    "derived": (
                        state / "derived" / "gen-1" / "operator.env"
                    ).read_bytes(),
                    "runtime": (
                        Path(tmp) / "releases" / "airflow-venv-gen-1" / "bin" / "python"
                    ).read_bytes(),
                },
            )
            self.assertFalse((state / "promotions" / "gen-1").exists())

    def test_pre_receipt_stage_failure_is_quarantined_and_burns_the_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            release, control_env, _ = self._prepare(
                Path(tmp),
                fresh=False,
                ready_after=1,
                db_exists=True,
            )
            installed = self._install(release, control_env)
            self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
            state = Path(tmp) / "state"
            promotion = release / "operator" / "promotion.py"
            implementation = release / "operator" / "promotion-impl.py"
            implementation.write_bytes(promotion.read_bytes())
            write_stub(
                promotion,
                "#!/usr/bin/env python3\n"
                "import os\n"
                "import runpy\n"
                "import sys\n"
                "if os.environ.get('FIXTURE_FAIL_PROMOTION_INIT') == '1' "
                "and sys.argv[1:2] == ['init']:\n"
                "    raise SystemExit(71)\n"
                "runpy.run_path(os.path.join(os.path.dirname(__file__), "
                "'promotion-impl.py'), run_name='__main__')\n",
            )

            active_record = state / "install-record.json"
            resolved_env = state / "resolved.env"
            admission = Path(tmp) / "admission"
            tracked = {
                "active": active_record.read_bytes(),
                "resolved": resolved_env.read_bytes(),
                "admission": admission.read_bytes(),
            }
            failed = self._maintenance(
                release,
                dict(control_env, FIXTURE_FAIL_PROMOTION_INIT="1"),
                "stage",
                "gen-2",
            )
            self.assertEqual(failed.returncode, 71, failed.stdout + failed.stderr)
            self.assertFalse((state / "promotions" / "gen-2" / "receipt.json").exists())
            self.assertTrue((state / "snapshots" / "gen-2.json").is_file())
            self.assertTrue((state / "derived" / "gen-2" / "operator.env").is_file())
            self.assertEqual(
                {
                    "active": active_record.read_bytes(),
                    "resolved": resolved_env.read_bytes(),
                    "admission": admission.read_bytes(),
                },
                tracked,
            )

            quarantined = {
                path.relative_to(state).as_posix(): hashlib.sha256(
                    path.read_bytes()
                ).hexdigest()
                for path in (
                    state / "snapshots" / "gen-2.json",
                    *sorted((state / "derived" / "gen-2").rglob("*")),
                )
                if path.is_file()
            }
            same_id = self._maintenance(release, control_env, "stage", "gen-2")
            self.assertEqual(
                same_id.returncode,
                1,
                same_id.stdout + same_id.stderr,
            )
            self.assertIn("reused historical generation", same_id.stderr)
            self.assertEqual(
                {
                    path.relative_to(state).as_posix(): hashlib.sha256(
                        path.read_bytes()
                    ).hexdigest()
                    for path in (
                        state / "snapshots" / "gen-2.json",
                        *sorted((state / "derived" / "gen-2").rglob("*")),
                    )
                    if path.is_file()
                },
                quarantined,
            )
            self.assertFalse((state / "promotions" / "gen-2").exists())
            self.assertEqual(
                {
                    "active": active_record.read_bytes(),
                    "resolved": resolved_env.read_bytes(),
                    "admission": admission.read_bytes(),
                },
                tracked,
            )

            fresh_id = self._maintenance(release, control_env, "stage", "gen-3")
            self.assertEqual(fresh_id.returncode, 0, fresh_id.stdout + fresh_id.stderr)
            self.assertTrue(
                (state / "promotions" / "gen-3" / "receipt.json").is_file()
            )
            self.assertEqual(
                {
                    "active": active_record.read_bytes(),
                    "resolved": resolved_env.read_bytes(),
                    "admission": admission.read_bytes(),
                },
                tracked,
            )

    def test_mutating_verbs_lock_before_reading_the_active_generation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            release, control_env, _ = self._prepare(
                Path(tmp), fresh=False, ready_after=1, db_exists=True
            )
            self.addCleanup(self._stop_runtime_processes, Path(tmp))
            self.assertEqual(self._install(release, control_env).returncode, 0)
            state = Path(tmp) / "state"
            control_env["EXPECTED_PLATFORM_DSN"] = self._derived_value(
                state, "gen-1", "PLATFORM_OPERATOR_DSN"
            )
            self._health_double(release, control_env)
            self.assertEqual(
                self._maintenance(release, control_env, "stage", "gen-2").returncode,
                0,
            )
            self._clone_generation_stubs(state)
            self._linux_health_double(release)
            self.assertEqual(
                self._maintenance(release, control_env, "promote", "gen-2").returncode,
                0,
            )
            self.assertEqual(
                self._maintenance(
                    release,
                    dict(control_env, FIXTURE_RUNTIME_PROCESS="1"),
                    "rollback",
                    "gen-2",
                ).returncode,
                0,
            )
            self._stop_runtime_processes(Path(tmp))

            bindir = Path(control_env["PATH"].split(os.pathsep, 1)[0])
            marker = Path(tmp) / "active-switch.marker"
            write_stub(
                bindir / "flock",
                "#!/usr/bin/env bash\n"
                "set -euo pipefail\n"
                'if [ "${FIXTURE_SWITCH_ACTIVE:-0}" = "1" ] && [ ! -e "$FIXTURE_SWITCH_MARKER" ]; then\n'
                '  cp "$FIXTURE_OPERATOR_STATE/install-records/gen-2.json" '
                '"$FIXTURE_OPERATOR_STATE/install-record.json"\n'
                '  : > "$FIXTURE_SWITCH_MARKER"\n'
                "fi\n"
                'exec /usr/bin/flock "$@"\n',
            )
            race_env = dict(
                control_env,
                FIXTURE_SWITCH_ACTIVE="1",
                FIXTURE_SWITCH_MARKER=str(marker),
                FIXTURE_REMOTE_ARCHIVE=str(Path(tmp) / "remote" / "gen-3.zip"),
            )
            staged = self._maintenance(release, race_env, "stage", "gen-3")
            self.assertEqual(staged.returncode, 0, staged.stdout + staged.stderr)
            receipt = json.loads(
                (state / "promotions" / "gen-3" / "receipt.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                receipt["from"],
                str(state / "install-records" / "gen-2.json"),
            )

            for verb in ("drain", "stop"):
                (state / "install-record.json").write_bytes(
                    (state / "install-records" / "gen-1.json").read_bytes()
                )
                marker.unlink(missing_ok=True)
                Path(control_env["LIFECYCLE_ENV_LOG"]).unlink(missing_ok=True)
                result = run(
                    ["bash", str(release / "operator" / "operatorctl.sh"), verb, "--config",
                     str(release.parent / "platform.json")],
                    race_env,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                observed = json.loads(
                    Path(control_env["LIFECYCLE_ENV_LOG"]).read_text(encoding="utf-8")
                )
                self.assertIn("airflow-venv-gen-2", observed["venv"])

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

    @staticmethod
    def _stop_runtime_processes(root: Path) -> None:
        for pidfile in (root / "unit-state").glob("*.pid"):
            with contextlib.suppress(OSError, ValueError):
                os.kill(int(pidfile.read_text(encoding="utf-8").strip()), 15)

    def _clone_generation_stubs(self, state: Path, target_generation: str = "gen-2") -> None:
        source = Path(self._derived_value(state, "gen-1", "AIRFLOW_VENV")) / "bin"
        target = Path(self._derived_value(state, target_generation, "AIRFLOW_VENV")) / "bin"
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

    def _health_double(
        self,
        release: Path,
        env: dict,
        *,
        verify_free_bytes: int | None = None,
    ) -> None:
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
        if verify_free_bytes is not None:
            verify_health = json.loads(json.dumps(health))
            verify_health["capacity"]["free_bytes"] = verify_free_bytes
            verify_file = release.parent / "windows-health-verify.json"
            verify_file.write_text(
                json.dumps({"passed": True, "verb": "health", "health": verify_health}),
                encoding="utf-8",
            )
            env["FIXTURE_HEALTH_FILE_VERIFY"] = str(verify_file)
        # The installed endpoint is the resident previous generation before any maintenance.
        # Seed its immutable Windows archive in the fake remote generation directory so a
        # rollback health check exercises the same size/digest identity as production.
        remote_previous = Path(env["FIXTURE_REMOTE_ARCHIVE_DIR"]) / "gen-1"
        remote_previous.mkdir(parents=True, exist_ok=True)
        for archive in release.parent.glob("*-windows-*.zip"):
            shutil.copyfile(archive, remote_previous / archive.name)
            (remote_previous / archive.name).chmod(0o444)

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

    def test_completed_candidate_reconcile_skips_file_rewrite_and_ddl(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            release, control_env, order = self._prepare(
                Path(tmp), fresh=False, ready_after=1, db_exists=True
            )
            self.assertEqual(self._install(release, control_env).returncode, 0)
            state = Path(tmp) / "state"
            control_env["EXPECTED_PLATFORM_DSN"] = self._derived_value(
                state, "gen-1", "PLATFORM_OPERATOR_DSN"
            )
            self._health_double(release, control_env)
            (Path(tmp) / "handoffs").mkdir(exist_ok=True)
            (Path(tmp) / "specifications").mkdir(exist_ok=True)
            self.assertEqual(
                self._maintenance(release, control_env, "stage", "gen-2").returncode,
                0,
            )
            evidence = Path(tmp) / "reconcile-input.json"
            evidence.write_text("{}\n", encoding="utf-8")
            receipt_path = state / "promotions" / "gen-2" / "receipt.json"
            output_path = state / "promotions" / "gen-2" / "reconcile-schema-migration.out"
            (Path(tmp) / "admission").write_text("true", encoding="utf-8")
            command = [
                "bash",
                str(release / "operator" / "operatorctl.sh"),
                "reconcile",
                "--config",
                str(release.parent / "platform.json"),
                "--install-id",
                "gen-2",
                "--run-id",
                "portal-repeat",
                "--stage",
                "freeze",
                "--outcome",
                "failed",
                "--evidence",
                str(evidence),
            ]
            first = run(command, control_env)
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
            self.assertIn(
                "reconciled=portal-repeat:freeze outcome=failed",
                first.stdout,
            )
            self.assertTrue(output_path.is_file())
            promotion_root = receipt_path.parent
            tree_before = {
                path.relative_to(promotion_root).as_posix(): hashlib.sha256(
                    path.read_bytes()
                ).hexdigest()
                for path in promotion_root.rglob("*")
                if path.is_file()
            }
            receipt_before = receipt_path.read_bytes()
            output_before = output_path.read_bytes()
            ddl_before = order.read_text(encoding="utf-8").count(
                "psql_platform_apply"
            )
            second = run(command, control_env)
            self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
            tree_after = {
                path.relative_to(promotion_root).as_posix(): hashlib.sha256(
                    path.read_bytes()
                ).hexdigest()
                for path in promotion_root.rglob("*")
                if path.is_file()
            }
            self.assertEqual(tree_after, tree_before)
            self.assertEqual(receipt_path.read_bytes(), receipt_before)
            self.assertEqual(output_path.read_bytes(), output_before)
            self.assertEqual(
                order.read_text(encoding="utf-8").count("psql_platform_apply"),
                ddl_before,
            )
            receipt_document = json.loads(receipt_path.read_text(encoding="utf-8"))
            completed_reconcile = next(
                row
                for row in receipt_document["steps"]
                if row["verb"] == "reconcile" and row["step"] == "schema-migration"
            )
            tampered_evidence = Path(completed_reconcile["evidence"][0])
            tampered_evidence.write_bytes(tampered_evidence.read_bytes() + b"tampered\n")
            tampered = run(
                [
                    "bash",
                    str(release / "operator" / "operatorctl.sh"),
                    "reconcile",
                    "--config",
                    str(release.parent / "platform.json"),
                    "--install-id",
                    "gen-2",
                    "--run-id",
                    "portal-repeat",
                    "--stage",
                    "freeze",
                    "--outcome",
                    "failed",
                    "--evidence",
                    str(evidence),
                ],
                control_env,
            )
            self.assertNotEqual(tampered.returncode, 0)
            self.assertIn("reconcile schema-migration state is unreadable", tampered.stderr)
            self.assertEqual(receipt_path.read_bytes(), receipt_before)
            self.assertEqual(output_path.read_bytes(), output_before)
            self.assertEqual(
                order.read_text(encoding="utf-8").count("psql_platform_apply"),
                ddl_before,
            )

    def test_candidate_reconcile_refuses_before_ddl_when_maintenance_lock_is_held(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            release, control_env, order = self._prepare(
                Path(tmp),
                fresh=False,
                ready_after=1,
                db_exists=True,
            )
            installed = self._install(release, control_env)
            self.assertEqual(
                installed.returncode,
                0,
                installed.stdout + installed.stderr,
            )
            state = Path(tmp) / "state"
            control_env["EXPECTED_PLATFORM_DSN"] = self._derived_value(
                state,
                "gen-1",
                "PLATFORM_OPERATOR_DSN",
            )
            self._health_double(release, control_env)
            (Path(tmp) / "handoffs").mkdir(exist_ok=True)
            (Path(tmp) / "specifications").mkdir(exist_ok=True)
            staged = self._maintenance(release, control_env, "stage", "gen-2")
            self.assertEqual(staged.returncode, 0, staged.stdout + staged.stderr)
            (Path(tmp) / "admission").write_text("true", encoding="utf-8")
            receipt_path = state / "promotions" / "gen-2" / "receipt.json"
            receipt_before = receipt_path.read_bytes()
            ddl_before = order.read_text(encoding="utf-8").count(
                "psql_platform_apply"
            )
            lock_dir = state / "locks"
            lock_dir.mkdir(exist_ok=True)
            lock_path = lock_dir / "maintenance.lock"
            lock_path.write_text("external lifecycle owns this lock\n", encoding="utf-8")
            descriptor = os.open(lock_path, os.O_RDWR)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                refused = run(
                    [
                        "bash",
                        str(release / "operator" / "operatorctl.sh"),
                        "reconcile",
                        "--config",
                        str(release.parent / "platform.json"),
                        "--install-id",
                        "gen-2",
                        "--run-id",
                        "portal-1",
                        "--stage",
                        "freeze",
                        "--outcome",
                        "failed",
                        "--evidence",
                        "receipt.json",
                    ],
                    control_env,
                )
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)
            self.assertNotEqual(
                refused.returncode,
                0,
                refused.stdout + refused.stderr,
            )
            self.assertIn(
                "another supported lifecycle operation owns the maintenance lock",
                refused.stderr,
            )
            self.assertEqual(
                order.read_text(encoding="utf-8").count("psql_platform_apply"),
                ddl_before,
            )
            self.assertEqual(receipt_path.read_bytes(), receipt_before)

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

    def test_stage_archive_is_read_only_and_promote_reuses_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            release, control_env, order = self._prepare(
                Path(tmp), fresh=False, ready_after=1, db_exists=True
            )
            self.addCleanup(self._stop_runtime_processes, Path(tmp))
            installed = self._install(release, control_env)
            self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
            state = Path(tmp) / "state"
            control_env["EXPECTED_PLATFORM_DSN"] = self._derived_value(
                state, "gen-1", "PLATFORM_OPERATOR_DSN"
            )
            self._health_double(release, control_env)
            (Path(tmp) / "handoffs").mkdir(exist_ok=True)
            (Path(tmp) / "specifications").mkdir(exist_ok=True)
            remote_archive = Path(tmp) / "remote" / "description-1.4.0-windows-cp312-x86_64.zip"
            control_env["FIXTURE_REMOTE_ARCHIVE"] = str(remote_archive)
            staged = self._maintenance(release, control_env, "stage", "gen-2")
            self.assertEqual(staged.returncode, 0, staged.stdout + staged.stderr)
            self.assertTrue(remote_archive.is_file())
            self.assertFalse(stat.S_IMODE(remote_archive.stat().st_mode) & 0o222)
            original = remote_archive.read_bytes()
            self._clone_generation_stubs(state)
            self._linux_health_double(release)
            promoted = self._maintenance(release, control_env, "promote", "gen-2")
            detail = promoted.stdout + promoted.stderr
            for name in ("config-reference.out", "start-services.out", "verify.out"):
                path = state / "promotions" / "gen-2" / "evidence" / name
                if path.is_file():
                    detail += f"\n== {name} ==\n{path.read_text(encoding='utf-8')}"
            self.assertEqual(promoted.returncode, 0, detail)
            self.assertEqual(remote_archive.read_bytes(), original)
            self.assertFalse(stat.S_IMODE(remote_archive.stat().st_mode) & 0o222)
            scp_lines = order.read_text(encoding="utf-8").splitlines()
            archive_scp = [
                line
                for line in scp_lines
                if line.startswith("scp ") and "description-1.4.0-windows-cp312-x86_64.zip" in line
            ]
            self.assertEqual(len(archive_scp), 1, "stage should transfer the archive exactly once")

    def test_promote_refuses_a_failed_generation_render_after_the_pointer_switch(self) -> None:
        """A renderer exit is fatal even when it already left a plausible airflow.cfg on disk."""

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            release, control_env, order = self._prepare(root, fresh=False, ready_after=1, db_exists=True)
            installed = self._install(release, control_env)
            self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
            state = root / "state"
            control_env["EXPECTED_PLATFORM_DSN"] = self._derived_value(
                state, "gen-1", "PLATFORM_OPERATOR_DSN"
            )
            self._health_double(release, control_env)
            (root / "handoffs").mkdir(exist_ok=True)
            (root / "specifications").mkdir(exist_ok=True)
            staged = self._maintenance(release, control_env, "stage", "gen-2")
            self.assertEqual(staged.returncode, 0, staged.stdout + staged.stderr)
            self._clone_generation_stubs(state)
            candidate_venv = Path(self._derived_value(state, "gen-2", "AIRFLOW_VENV"))
            write_stub(candidate_venv / "bin" / "python", '#!/usr/bin/env bash\nexec python3 "$@"\n')
            self._linux_health_double(release)
            write_stub(
                release / "airflow" / "render_config.py",
                "#!/usr/bin/env python3\n"
                "import os\n"
                "import sys\n"
                "from pathlib import Path\n"
                "arguments = sys.argv[1:]\n"
                'folder = Path(arguments[arguments.index("--dags-folder") + 1])\n'
                'home = Path(arguments[arguments.index("--home") + 1])\n'
                "home.mkdir(parents=True, exist_ok=True)\n"
                'cfg = home / "airflow.cfg"\n'
                'cfg.write_text("# managed-by: description-airflow\\n[core]\\ndags_folder = " + str(folder) + "\\n",'
                " encoding='utf-8')\n"
                "cfg.chmod(0o600)\n"
                'print("renderer prepared a plausible config then failed", file=sys.stderr)\n'
                "raise SystemExit(9)\n",
            )
            before = order.read_text(encoding="utf-8")
            promoted = self._maintenance(release, control_env, "promote", "gen-2")
            self.assertNotEqual(promoted.returncode, 0, promoted.stdout + promoted.stderr)
            self.assertIn(
                "the Airflow DAG/config reference could not bind the switched generation",
                promoted.stderr,
            )
            evidence = state / "promotions" / "gen-2" / "evidence"
            self.assertIn(
                "renderer prepared a plausible config then failed",
                (evidence / "config-reference.out").read_text(encoding="utf-8"),
            )
            self.assertEqual(
                (root / "admission").read_text(encoding="utf-8").strip(),
                "true",
                "a post-switch renderer failure must keep the admission closed",
            )
            active = json.loads((state / "install-record.json").read_text(encoding="utf-8"))
            self.assertEqual(active["install_id"], "gen-2")
            appended = order.read_text(encoding="utf-8")[len(before):]
            self.assertIn("platform close_admission", appended)
            self.assertNotIn("dags unpause", appended)
            self.assertNotIn("services_start", appended)
            self.assertNotIn("systemctl --user enable --now description-portal.service", appended)
            for unit in (
                "description-airflow-dag-processor",
                "description-airflow-scheduler",
                "description-airflow-api-server",
                "description-portal",
                "description-operator-proxy",
            ):
                path = root / "unit-state" / unit
                self.assertEqual(path.read_text(encoding="utf-8"), "inactive", unit)

    def test_promote_renders_the_candidate_api_port_not_the_ambient_port(self) -> None:
        """The target renderer default and explicit target port both beat the ambient port."""

        def replace_env_value(path: Path, key: str, value: str) -> None:
            lines = path.read_text(encoding="utf-8").splitlines()
            replacement = f"{key}={encode_env_value(value)}"
            found = False
            updated = []
            for line in lines:
                if line.startswith(f"{key}="):
                    updated.append(replacement)
                    found = True
                else:
                    updated.append(line)
            if not found:
                updated.append(replacement)
            path.write_text("\n".join(updated) + "\n", encoding="utf-8")

        for label, explicit_port in (("default", None), ("explicit", "18991")):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                release, control_env, _order = self._prepare(root, fresh=False, ready_after=1, db_exists=True)
                installed = self._install(release, control_env)
                self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
                state = root / "state"
                control_env["EXPECTED_PLATFORM_DSN"] = self._derived_value(
                    state, "gen-1", "PLATFORM_OPERATOR_DSN"
                )
                self._health_double(release, control_env)
                (root / "handoffs").mkdir(exist_ok=True)
                (root / "specifications").mkdir(exist_ok=True)
                staged = self._maintenance(release, control_env, "stage", "gen-2")
                self.assertEqual(staged.returncode, 0, staged.stdout + staged.stderr)
                ambient = "47001"
                replace_env_value(state / "resolved.env", "AIRFLOW_API_PORT", ambient)
                candidate_env = state / "derived" / "gen-2" / "operator.env"
                if explicit_port is not None:
                    replace_env_value(candidate_env, "AIRFLOW_API_PORT", explicit_port)
                    receipt_path = state / "promotions" / "gen-2" / "receipt.json"
                    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
                    candidate_env_digest = hashlib.sha256(
                        candidate_env.read_bytes()
                    ).hexdigest()
                    for row in receipt["steps"]:
                        for entry in row.get("evidence_digests", []):
                            if entry["path"] == str(candidate_env):
                                entry["sha256"] = candidate_env_digest
                    receipt_path.write_text(
                        json.dumps(receipt, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8",
                    )
                self._clone_generation_stubs(state)
                candidate_venv = Path(self._derived_value(state, "gen-2", "AIRFLOW_VENV"))
                candidate_python = candidate_venv / "bin" / "python"
                preserved_python = candidate_venv / "bin" / "python.fixture"
                shutil.copyfile(candidate_python, preserved_python)
                preserved_python.chmod(0o755)
                write_stub(
                    candidate_python,
                    "#!/usr/bin/env bash\n"
                    'case "$*" in\n'
                    '  *render_config.py*|*render_operator.py*)\n'
                    '    exec python3 "$@" ;;\n'
                    "esac\n"
                    'exec "$0.fixture" "$@"\n',
                )
                self._linux_health_double(release)
                promoted = self._maintenance(release, control_env, "promote", "gen-2")
                detail = promoted.stdout + promoted.stderr
                for name in ("config-reference.out", "start-services.out", "verify.out"):
                    path = state / "promotions" / "gen-2" / "evidence" / name
                    if path.is_file():
                        detail += f"\n== {name} ==\n{path.read_text(encoding='utf-8')}"
                self.assertEqual(promoted.returncode, 0, detail)
                expected = explicit_port or str(_render_module().PORT_DEFAULTS["AIRFLOW_API_PORT"])
                cfg = (state / "airflow-home" / "airflow.cfg").read_text(encoding="utf-8")
                self.assertIn(f"http://127.0.0.1:{expected}/execution", cfg)
                self.assertNotIn(f"http://127.0.0.1:{ambient}/execution", cfg)

    def test_stage_refuses_a_release_manifest_outside_the_controller_tree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            release, control_env, _order = self._prepare(root, fresh=False, ready_after=1, db_exists=True)
            installed = self._install(release, control_env)
            self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
            foreign = root / "foreign"
            foreign.mkdir()
            shutil.copyfile(release.parent / "release.json", foreign / "release.json")
            refused = run(
                [
                    "bash",
                    str(release / "operator" / "operatorctl.sh"),
                    "stage",
                    "--config",
                    str(release.parent / "platform.json"),
                    "--install-id",
                    "gen-2",
                    "--release-file",
                    str(foreign / "release.json"),
                ],
                control_env,
            )
            self.assertNotEqual(refused.returncode, 0, refused.stdout + refused.stderr)
            self.assertIn("controller release tree", refused.stderr)
            self.assertFalse((root / "state" / "promotions" / "gen-2").exists())

    def test_the_stage_receipt_binds_the_controller_release_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            release, control_env, _order = self._prepare(root, fresh=False, ready_after=1, db_exists=True)
            installed = self._install(release, control_env)
            self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
            state = root / "state"
            staged = self._maintenance(release, control_env, "stage", "gen-2")
            self.assertEqual(staged.returncode, 0, staged.stdout + staged.stderr)
            receipt = json.loads((state / "promotions" / "gen-2" / "receipt.json").read_text(encoding="utf-8"))
            manifest = release.parent / "release.json"
            self.assertEqual(
                receipt["release_manifest"],
                {"path": str(manifest.resolve()), "sha256": hashlib.sha256(manifest.read_bytes()).hexdigest()},
            )
            control_env["EXPECTED_PLATFORM_DSN"] = self._derived_value(
                state, "gen-1", "PLATFORM_OPERATOR_DSN"
            )
            self._health_double(release, control_env)
            receipt_path = state / "promotions" / "gen-2" / "receipt.json"
            tampered = json.loads(receipt_path.read_text(encoding="utf-8"))
            tampered["release_manifest"]["sha256"] = "0" * 64
            receipt_path.write_text(json.dumps(tampered), encoding="utf-8")
            promoted = self._maintenance(release, control_env, "promote", "gen-2")
            self.assertNotEqual(promoted.returncode, 0, promoted.stdout + promoted.stderr)
            self.assertIn("binds another release manifest", promoted.stderr)
            active = json.loads((state / "install-record.json").read_text(encoding="utf-8"))
            self.assertEqual(active["install_id"], "gen-1")
            self.assertEqual((root / "admission").read_text(encoding="utf-8").strip(), "true")

    def test_promote_refuses_an_old_receipt_without_release_binding_before_writes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            release, control_env, _order = self._prepare(root, fresh=False, ready_after=1, db_exists=True)
            installed = self._install(release, control_env)
            self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
            state = root / "state"
            staged = self._maintenance(release, control_env, "stage", "gen-2")
            self.assertEqual(staged.returncode, 0, staged.stdout + staged.stderr)
            control_env["EXPECTED_PLATFORM_DSN"] = self._derived_value(
                state, "gen-1", "PLATFORM_OPERATOR_DSN"
            )
            self._health_double(release, control_env)
            receipt_path = state / "promotions" / "gen-2" / "receipt.json"
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            receipt.pop("release_manifest")
            receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
            before = receipt_path.read_bytes()
            promoted = self._maintenance(release, control_env, "promote", "gen-2")
            self.assertNotEqual(promoted.returncode, 0, promoted.stdout + promoted.stderr)
            self.assertIn("carries no release manifest binding", promoted.stderr)
            self.assertEqual(receipt_path.read_bytes(), before, "the old receipt is not rewritten")
            active = json.loads((state / "install-record.json").read_text(encoding="utf-8"))
            self.assertEqual(active["install_id"], "gen-1")
            self.assertEqual((root / "admission").read_text(encoding="utf-8").strip(), "true")

    def test_maintenance_actions_refuse_a_cross_tree_manifest_before_writes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            release, control_env, _order = self._prepare(root, fresh=False, ready_after=1, db_exists=True)
            installed = self._install(release, control_env)
            self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
            state = root / "state"
            staged = self._maintenance(release, control_env, "stage", "gen-2")
            self.assertEqual(staged.returncode, 0, staged.stdout + staged.stderr)
            foreign = root / "foreign"
            foreign.mkdir()
            shutil.copyfile(release.parent / "release.json", foreign / "release.json")
            evidence = root / "reconcile-evidence.json"
            evidence.write_text("{}\n", encoding="utf-8")
            commands = {
                "promote": [
                    "bash", str(release / "operator" / "operatorctl.sh"), "promote",
                    "--config", str(release.parent / "platform.json"),
                    "--install-id", "gen-2", "--release-file", str(foreign / "release.json"),
                ],
                "rollback": [
                    "bash", str(release / "operator" / "operatorctl.sh"), "rollback",
                    "--config", str(release.parent / "platform.json"),
                    "--install-id", "gen-2", "--release-file", str(foreign / "release.json"),
                ],
                "reconcile": [
                    "bash", str(release / "operator" / "operatorctl.sh"), "reconcile",
                    "--config", str(release.parent / "platform.json"),
                    "--install-id", "gen-2", "--run-id", "portal-cross-tree",
                    "--stage", "freeze", "--outcome", "failed", "--evidence", str(evidence),
                    "--release-file", str(foreign / "release.json"),
                ],
            }
            for label, command in commands.items():
                with self.subTest(action=label):
                    result = run(command, control_env)
                    self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                    active = json.loads((state / "install-record.json").read_text(encoding="utf-8"))
                    self.assertEqual(active["install_id"], "gen-1")
                    self.assertEqual((root / "admission").read_text(encoding="utf-8").strip(), "true")

    def test_promote_refuses_a_generation_switch_while_a_dag_reader_is_alive(self) -> None:
        """The pointer switch has a no-reader barrier; a surviving reader keeps the old record."""

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            release, control_env, order = self._prepare(root, fresh=False, ready_after=1, db_exists=True)
            installed = self._install(release, control_env)
            self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
            state = root / "state"
            control_env["EXPECTED_PLATFORM_DSN"] = self._derived_value(
                state, "gen-1", "PLATFORM_OPERATOR_DSN"
            )
            self._health_double(release, control_env)
            (root / "handoffs").mkdir(exist_ok=True)
            (root / "specifications").mkdir(exist_ok=True)
            staged = self._maintenance(release, control_env, "stage", "gen-2")
            self.assertEqual(staged.returncode, 0, staged.stdout + staged.stderr)
            self._clone_generation_stubs(state)
            self._linux_health_double(release)
            control_env["FIXTURE_LEAVE_READER_ACTIVE"] = "description-airflow-scheduler"
            before = order.read_text(encoding="utf-8")
            promoted = self._maintenance(release, control_env, "promote", "gen-2")
            self.assertNotEqual(promoted.returncode, 0, promoted.stdout + promoted.stderr)
            self.assertIn(
                "description-airflow-scheduler is active; refusing a non-barriered generation switch",
                promoted.stderr,
            )
            active = json.loads((state / "install-record.json").read_text(encoding="utf-8"))
            self.assertEqual(active["install_id"], "gen-1", "a live reader must block the pointer switch")
            evidence = state / "promotions" / "gen-2" / "evidence"
            self.assertFalse(
                (evidence / "switch-pointer.out").exists(),
                "no record write may start after the no-reader barrier refuses",
            )
            self.assertEqual((root / "admission").read_text(encoding="utf-8").strip(), "true")
            appended = order.read_text(encoding="utf-8")[len(before):]
            self.assertNotIn("services_start", appended)
            self.assertNotIn("dags unpause", appended)
            self.assertEqual(
                (root / "unit-state" / "description-airflow-scheduler").read_text(encoding="utf-8"),
                "active",
            )

    def test_promote_refuses_a_mismatched_read_only_staged_archive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            release, control_env, _ = self._prepare(
                Path(tmp), fresh=False, ready_after=1, db_exists=True
            )
            installed = self._install(release, control_env)
            self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
            state = Path(tmp) / "state"
            control_env["EXPECTED_PLATFORM_DSN"] = self._derived_value(
                state, "gen-1", "PLATFORM_OPERATOR_DSN"
            )
            self._health_double(release, control_env)
            (Path(tmp) / "handoffs").mkdir(exist_ok=True)
            (Path(tmp) / "specifications").mkdir(exist_ok=True)
            remote_archive = Path(tmp) / "remote" / "description-1.4.0-windows-cp312-x86_64.zip"
            control_env["FIXTURE_REMOTE_ARCHIVE"] = str(remote_archive)
            self.assertEqual(
                self._maintenance(release, control_env, "stage", "gen-2").returncode, 0
            )
            remote_archive.chmod(0o600)
            remote_archive.write_bytes(b"wrong-staged-archive")
            remote_archive.chmod(0o444)
            failed = self._maintenance(release, control_env, "promote", "gen-2")
            self.assertNotEqual(failed.returncode, 0, failed.stdout + failed.stderr)
            self.assertIn("does not match the release binding", failed.stderr)
            self.assertEqual(remote_archive.read_bytes(), b"wrong-staged-archive")

    def test_stage_promote_rollback_drill_with_bound_doubles(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            release, control_env, order = self._prepare(Path(tmp), fresh=False, ready_after=1, db_exists=True)
            self.addCleanup(self._stop_runtime_processes, Path(tmp))
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
            detail = promoted.stdout + promoted.stderr
            for name in ("config-reference.out", "start-services.out", "verify.out"):
                path = state / "promotions" / "gen-2" / "evidence" / name
                if path.is_file():
                    detail += f"\n== {name} ==\n{path.read_text(encoding='utf-8')}"
            self.assertEqual(promoted.returncode, 0, detail)
            active = json.loads((state / "install-record.json").read_text(encoding="utf-8"))
            self.assertEqual(active["install_id"], "gen-2")
            restored_venv = self._derived_value(state, "gen-1", "AIRFLOW_VENV")
            candidate_venv = self._derived_value(state, "gen-2", "AIRFLOW_VENV")
            restored_env = state / "derived" / "gen-1" / "operator.env"
            candidate_unit = Path(tmp) / "config" / "systemd" / "user" / "description-airflow-scheduler.service"
            self.assertIn(candidate_venv, candidate_unit.read_text(encoding="utf-8"))
            captured_dag = json.loads(
                (state / "promotions" / "gen-2" / "evidence" / "dag-before-promotion.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(captured_dag, {"dag_id": "solidworks_to_urdf", "paused": True})
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
                    "switch-pointer", "config-reference", "start-services", "verify", "open-admission",
                ],
            )
            self.assertEqual({row["status"] for row in receipt["steps"]}, {"completed"})
            private_capture = (
                state / "promotions" / "gen-2" / "evidence" / "private" / "connection-before-promotion.json"
            )
            self.assertEqual(stat.S_IMODE(private_capture.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(private_capture.parent.stat().st_mode), 0o700)
            manifest_text = (
                state / "promotions" / "gen-2" / "evidence" / "backup-manifest.json"
            ).read_text(encoding="utf-8")
            self.assertIn("private/connection-before-promotion.json", manifest_text)
            self.assertNotIn("fixture-secret", manifest_text)
            order_rows = (Path(control_env["ORDER_LOG"])).read_text(encoding="utf-8").splitlines()
            capture_at = order_rows.index("connection_capture")
            self.assertIn("add_connection", order_rows[capture_at:])
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
            rolled = self._maintenance(
                release,
                dict(
                    control_env,
                    AIRFLOW_VENV=candidate_venv,
                    FIXTURE_RUNTIME_PROCESS="1",
                ),
                "rollback",
            )
            self.assertEqual(rolled.returncode, 0, rolled.stdout + rolled.stderr)
            restored = json.loads((state / "install-record.json").read_text(encoding="utf-8"))
            self.assertEqual(restored["install_id"], "gen-1")
            self.assertEqual(admission.read_text().strip(), "true", "rollback keeps the admission closed")
            unit_dir = Path(tmp) / "config" / "systemd" / "user"
            for unit in (
                "description-airflow-dag-processor.service",
                "description-airflow-scheduler.service",
                "description-airflow-api-server.service",
            ):
                text = (unit_dir / unit).read_text(encoding="utf-8")
                self.assertIn(f"ExecStart={restored_venv}/", text)
                self.assertIn(f"EnvironmentFile={restored_env}", text)
                self.assertNotIn(candidate_venv, text)
            render_lines = [
                row
                for row in order.read_text(encoding="utf-8").splitlines()
                if row.startswith("services_render ")
            ]
            self.assertTrue(render_lines)
            self.assertIn(f"venv={restored_venv}", render_lines[-1])
            self.assertIn(f"derived={restored_env}", render_lines[-1])
            rollback_verify = (
                state / "promotions" / "gen-2" / "evidence" / "rollback-verify.out"
            ).read_text(encoding="utf-8")
            self.assertIn("restored_maps=", rollback_verify)
            self.assertNotIn("restored_maps=0", rollback_verify)
            self.assertIn("candidate_maps=0", rollback_verify)
            self.assertIn("dag_paused_restored=true", rollback_verify)
            live_maps = []
            for pidfile in (Path(tmp) / "unit-state").glob("description-airflow-*.pid"):
                pid = int(pidfile.read_text(encoding="utf-8").strip())
                live_maps.append(Path(f"/proc/{pid}/maps").read_text(encoding="utf-8"))
            self.assertEqual(len(live_maps), 3)
            self.assertTrue(all(restored_venv in document for document in live_maps))
            self.assertTrue(all(candidate_venv not in document for document in live_maps))
            final_receipt = json.loads((state / "promotions" / "gen-2" / "receipt.json").read_text(encoding="utf-8"))
            self.assertEqual(
                [row["step"] for row in final_receipt["steps"] if row["verb"] == "rollback"],
                [
                    "rollback-close", "rollback-drain", "rollback-stop", "rollback-windows",
                    "rollback-linux", "rollback-config-reference", "rollback-verify",
                ],
            )
            self.assertIn(
                "connection_restore",
                (Path(control_env["ORDER_LOG"])).read_text(encoding="utf-8").splitlines(),
            )
            lifecycle_calls = [
                row
                for row in (Path(control_env["ORDER_LOG"])).read_text(encoding="utf-8").splitlines()
                if row in {"services_start", "services_stop", "services_stop-executors"}
            ]
            self.assertEqual(
                lifecycle_calls,
                ["services_stop-executors", "services_start", "services_stop-executors", "services_start"],
                "promotion and rollback stop executors while PostgreSQL remains available for restore",
            )
            self.assertIn(
                "connection row restored exactly",
                (state / "promotions" / "gen-2" / "evidence" / "rollback-config-reference.out").read_text(
                    encoding="utf-8"
                ),
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

    def test_rollback_health_binds_the_restored_resident_release_not_the_candidate(self) -> None:
        """A rollback verifies the previous endpoint with its own archive identity.

        Candidate and resident deliberately carry different Windows archive sizes and digests.
        The candidate generation keeps the candidate archive, while the restored generation
        keeps the resident archive; health must bind the latter from the restored install
        record and reach the real rollback health gate.
        """

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            release, control_env, _order = self._prepare(
                root, fresh=False, ready_after=1, db_exists=True
            )
            self.addCleanup(self._stop_runtime_processes, root)
            installed = self._install(release, control_env)
            self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
            state = root / "state"
            control_env["EXPECTED_PLATFORM_DSN"] = self._derived_value(
                state, "gen-1", "PLATFORM_OPERATOR_DSN"
            )
            self._health_double(release, control_env)
            (root / "handoffs").mkdir(exist_ok=True)
            (root / "specifications").mkdir(exist_ok=True)

            archive_name = "description-1.4.0-windows-cp312-x86_64.zip"
            previous_archive = (release.parent / archive_name).read_bytes()
            candidate_archive = b"candidate-windows-archive-with-a-distinct-size-and-digest"
            self.assertNotEqual(len(previous_archive), len(candidate_archive))
            self.assertNotEqual(
                hashlib.sha256(previous_archive).digest(),
                hashlib.sha256(candidate_archive).digest(),
            )

            candidate_root = root / "candidate-release"
            shutil.copytree(release.parent, candidate_root)
            candidate = candidate_root / "deploy"
            (candidate_root / archive_name).write_bytes(candidate_archive)
            candidate_manifest = json.loads(
                (candidate_root / "release.json").read_text(encoding="utf-8")
            )
            candidate_manifest["source_commit"] = "d" * 40
            candidate_manifest["source_sha256"] = "b" * 64
            candidate_manifest["artifacts"][archive_name] = hashlib.sha256(
                candidate_archive
            ).hexdigest()
            (candidate_root / "release.json").write_text(
                json.dumps(candidate_manifest), encoding="utf-8"
            )

            previous_health = json.loads(
                (release.parent / "windows-health.json").read_text(encoding="utf-8")
            )
            candidate_health = json.loads(json.dumps(previous_health))
            candidate_health["health"]["release"]["source_commit"] = "d" * 40
            candidate_health["health"]["release"]["source_sha256"] = "b" * 64
            candidate_health_path = root / "candidate-windows-health.json"
            candidate_health_path.write_text(json.dumps(candidate_health), encoding="utf-8")
            control_env["FIXTURE_HEALTH_FILE_PREVIOUS"] = str(
                release.parent / "windows-health.json"
            )
            control_env["FIXTURE_HEALTH_FILE_CANDIDATE"] = str(candidate_health_path)
            control_env["FIXTURE_HEALTH_FILE"] = str(candidate_health_path)

            remote_previous_archive = root / "remote-gen-1" / archive_name
            remote_candidate_archive = root / "remote-gen-2" / archive_name
            remote_previous_archive.parent.mkdir(parents=True)
            remote_previous_archive.write_bytes(previous_archive)
            remote_previous_archive.chmod(0o444)
            control_env["FIXTURE_REMOTE_ARCHIVE_GEN1"] = str(remote_previous_archive)
            control_env["FIXTURE_REMOTE_ARCHIVE_GEN2"] = str(remote_candidate_archive)
            resident_binding_path = state / "promotions" / "gen-1" / "windows-binding.json"
            resident_binding_path.parent.mkdir(parents=True, exist_ok=True)
            resident_binding_bytes = b'{"resident":"canonical-binding-sentinel"}\n'
            resident_binding_path.write_bytes(resident_binding_bytes)
            resident_binding_path.chmod(0o600)
            resident_binding_mode = stat.S_IMODE(resident_binding_path.stat().st_mode)

            staged = self._maintenance(candidate, control_env, "stage", "gen-2")
            self.assertEqual(staged.returncode, 0, staged.stdout + staged.stderr)
            self._clone_generation_stubs(state)
            self._linux_health_double(candidate)
            promoted = self._maintenance(candidate, control_env, "promote", "gen-2")
            self.assertEqual(promoted.returncode, 0, promoted.stdout + promoted.stderr)
            promoted_record = json.loads(
                (state / "install-record.json").read_text(encoding="utf-8")
            )
            self.assertEqual(promoted_record["install_id"], "gen-2")

            candidate_venv = self._derived_value(state, "gen-2", "AIRFLOW_VENV")
            rolled = self._maintenance(
                candidate,
                dict(
                    control_env,
                    AIRFLOW_VENV=candidate_venv,
                    FIXTURE_RUNTIME_PROCESS="1",
                ),
                "rollback",
            )
            self.assertEqual(rolled.returncode, 0, rolled.stdout + rolled.stderr)
            restored_record = json.loads(
                (state / "install-record.json").read_text(encoding="utf-8")
            )
            self.assertEqual(restored_record["install_id"], "gen-1")
            self.assertEqual(
                restored_record["release"]["source_commit"],
                "c" * 40,
            )

            previous_health_binding = json.loads(
                (
                    state
                    / "promotions"
                    / "gen-2"
                    / "evidence"
                    / "windows-health-rollback-verify-gen-2-binding.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(previous_health_binding["source_commit"], "c" * 40)
            self.assertEqual(previous_health_binding["source_sha256"], "a" * 64)
            self.assertEqual(
                previous_health_binding["artifact_sha256"],
                hashlib.sha256(previous_archive).hexdigest(),
            )
            self.assertEqual(
                resident_binding_path.read_bytes(),
                resident_binding_bytes,
                "health must not rewrite the restored resident's lifecycle binding",
            )
            self.assertEqual(stat.S_IMODE(resident_binding_path.stat().st_mode), resident_binding_mode)
            for label in ("endpoint-identity", "promote-verify"):
                with self.subTest(label=label):
                    candidate_health_binding = json.loads(
                        (
                            state
                            / "promotions"
                            / "gen-2"
                            / f"windows-health-{label}-binding.json"
                        ).read_text(encoding="utf-8")
                    )
                    self.assertEqual(candidate_health_binding["source_commit"], "d" * 40)
                    self.assertEqual(
                        candidate_health_binding["artifact_sha256"],
                        hashlib.sha256(candidate_archive).hexdigest(),
                    )

            rollback_health = json.loads(
                (
                    state
                    / "promotions"
                    / "gen-2"
                    / "evidence"
                    / "rollback-health.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(rollback_health["release"]["source_commit"], "c" * 40)
            rollback_verify = (
                state / "promotions" / "gen-2" / "evidence" / "rollback-verify.out"
            ).read_text(encoding="utf-8")
            self.assertIn("linux_health_gate=PASS", rollback_verify)
            final_receipt = json.loads(
                (state / "promotions" / "gen-2" / "receipt.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                [(row["step"], row["status"]) for row in final_receipt["steps"] if row["verb"] == "rollback"][-1],
                ("rollback-verify", "completed"),
            )

    def test_rollback_restores_the_pre_maintenance_unpaused_dag_state(self) -> None:
        """Rollback restores the captured DAG state instead of toggling a constant."""

        with tempfile.TemporaryDirectory() as tmp:
            release, control_env, _ = self._prepare(Path(tmp), fresh=False, ready_after=1, db_exists=True)
            self.addCleanup(self._stop_runtime_processes, Path(tmp))
            self.assertEqual(self._install(release, control_env).returncode, 0)
            state = Path(tmp) / "state"
            control_env["EXPECTED_PLATFORM_DSN"] = self._derived_value(
                state, "gen-1", "PLATFORM_OPERATOR_DSN"
            )
            self._health_double(release, control_env)
            self.assertEqual(self._maintenance(release, control_env, "stage", "gen-2").returncode, 0)
            self._clone_generation_stubs(state)
            self._linux_health_double(release)
            admission = Path(tmp) / "admission"
            admission.write_text("false", encoding="utf-8")
            promoted = self._maintenance(release, control_env, "promote", "gen-2")
            self.assertEqual(promoted.returncode, 0, promoted.stdout + promoted.stderr)
            captured = json.loads(
                (state / "promotions" / "gen-2" / "evidence" / "dag-before-promotion.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(captured, {"dag_id": "solidworks_to_urdf", "paused": False})
            candidate_venv = self._derived_value(state, "gen-2", "AIRFLOW_VENV")
            rolled = self._maintenance(
                release,
                dict(
                    control_env,
                    AIRFLOW_VENV=candidate_venv,
                    FIXTURE_RUNTIME_PROCESS="1",
                ),
                "rollback",
            )
            self.assertEqual(rolled.returncode, 0, rolled.stdout + rolled.stderr)
            self.assertEqual(admission.read_text(encoding="utf-8").strip(), "false")
            verify = (
                state / "promotions" / "gen-2" / "evidence" / "rollback-verify.out"
            ).read_text(encoding="utf-8")
            self.assertIn("dag_paused_restored=false", verify)

    def test_rollback_pause_failure_stops_before_drain_projection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            release, control_env, order = self._prepare(
                Path(tmp),
                fresh=False,
                ready_after=1,
                db_exists=True,
            )
            self.addCleanup(self._stop_runtime_processes, Path(tmp))
            self.assertEqual(self._install(release, control_env).returncode, 0)
            state = Path(tmp) / "state"
            control_env["EXPECTED_PLATFORM_DSN"] = self._derived_value(
                state,
                "gen-1",
                "PLATFORM_OPERATOR_DSN",
            )
            self._health_double(release, control_env)
            self.assertEqual(
                self._maintenance(release, control_env, "stage", "gen-2").returncode,
                0,
            )
            self._clone_generation_stubs(state)
            self._linux_health_double(release)
            promoted = self._maintenance(release, control_env, "promote", "gen-2")
            self.assertEqual(promoted.returncode, 0, promoted.stdout + promoted.stderr)
            candidate_venv = self._derived_value(state, "gen-2", "AIRFLOW_VENV")
            lifecycle_log = Path(control_env["LIFECYCLE_ENV_LOG"])
            lifecycle_log.unlink(missing_ok=True)
            order.write_text("", encoding="utf-8")
            rolled = self._maintenance(
                release,
                dict(
                    control_env,
                    AIRFLOW_VENV=candidate_venv,
                    FIXTURE_RUNTIME_PROCESS="1",
                    PAUSE_RC="1",
                ),
                "rollback",
            )
            self.assertEqual(rolled.returncode, 1)
            self.assertIn(
                "rollback could not pause scheduling or drain settled work",
                rolled.stderr,
            )
            self.assertFalse(
                lifecycle_log.exists(),
                "the lifecycle projection ran after the pause failure",
            )
            receipt = json.loads(
                (state / "promotions" / "gen-2" / "receipt.json").read_text(
                    encoding="utf-8"
                )
            )
            rollback_steps = {
                row["step"]: row["status"]
                for row in receipt["steps"]
                if row["verb"] == "rollback"
            }
            self.assertEqual(rollback_steps["rollback-drain"], "failed")
            self.assertNotIn("rollback-stop", rollback_steps)
            self.assertNotIn("rollback-windows", rollback_steps)
            self.assertNotIn("rollback-linux", rollback_steps)
            self.assertNotIn("rollback-verify", rollback_steps)
            lines = order.read_text(encoding="utf-8").splitlines()
            self.assertNotIn("services_stop-executors", lines)
            self.assertFalse(
                [
                    line
                    for line in lines
                    if line.startswith("systemctl --user disable --now")
                ]
            )

    def test_rollback_health_failure_leaves_safe_paused_evidence(self) -> None:
        """A failed restored-system health check never silently re-enables scheduling."""

        with tempfile.TemporaryDirectory() as tmp:
            release, control_env, order = self._prepare(Path(tmp), fresh=False, ready_after=1, db_exists=True)
            self.addCleanup(self._stop_runtime_processes, Path(tmp))
            self.assertEqual(self._install(release, control_env).returncode, 0)
            state = Path(tmp) / "state"
            control_env["EXPECTED_PLATFORM_DSN"] = self._derived_value(
                state, "gen-1", "PLATFORM_OPERATOR_DSN"
            )
            self._health_double(release, control_env)
            self.assertEqual(self._maintenance(release, control_env, "stage", "gen-2").returncode, 0)
            self._clone_generation_stubs(state)
            self._linux_health_double(release)
            promoted = self._maintenance(release, control_env, "promote", "gen-2")
            self.assertEqual(promoted.returncode, 0, promoted.stdout + promoted.stderr)
            write_stub(
                release / "operator" / "health_gate.sh",
                '#!/usr/bin/env bash\necho "rollback health gate failure" >> "$ORDER_LOG"\nexit 7\n',
            )
            candidate_venv = self._derived_value(state, "gen-2", "AIRFLOW_VENV")
            rolled = self._maintenance(
                release,
                dict(
                    control_env,
                    AIRFLOW_VENV=candidate_venv,
                    FIXTURE_RUNTIME_PROCESS="1",
                ),
                "rollback",
            )
            self.assertEqual(rolled.returncode, 1)
            self.assertIn("rollback health gate failure", order.read_text(encoding="utf-8"))
            admission = Path(tmp) / "admission"
            self.assertEqual(admission.read_text(encoding="utf-8").strip(), "true")
            evidence = state / "promotions" / "gen-2" / "evidence"
            verify = (evidence / "rollback-verify.out").read_text(encoding="utf-8")
            self.assertIn("restored_maps=", verify)
            self.assertIn("candidate_maps=0", verify)
            self.assertNotIn("dag_paused_restored=", verify)
            self.assertTrue((evidence / "rollback-health.json").is_file())
            self.assertTrue((evidence / "rollback-identity.json").is_file())
            receipt = json.loads((state / "promotions" / "gen-2" / "receipt.json").read_text(encoding="utf-8"))
            steps = {row["step"]: row["status"] for row in receipt["steps"] if row["verb"] == "rollback"}
            self.assertEqual(steps["rollback-verify"], "failed")

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
            windows_switch = next(
                row
                for row in receipt["steps"]
                if row["verb"] == "promote" and row["step"] == "windows-switch"
            )
            promotion_dir = state / "promotions" / "gen-2"
            evidence_map = Path(
                next(path for path in windows_switch["evidence"] if path.endswith("evidence-map.json"))
            )
            mapped_sources = {
                Path(row["source"]).name
                for group in json.loads(evidence_map.read_text(encoding="utf-8"))
                for row in group
            }
            self.assertEqual(
                mapped_sources,
                {
                    "windows-promote.out",
                    "windows-promote.out.stderr",
                    "windows-promote.out.transport.json",
                    "windows-promote.out.ssh-rc",
                    "windows-promote.path",
                    "windows-promote.path.stderr",
                },
            )
            self.assertTrue(
                all(
                    Path(path).is_relative_to(promotion_dir / "evidence-immutable")
                    for path in windows_switch["evidence"]
                )
            )
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
            windows_switch = next(
                row
                for row in receipt["steps"]
                if row["verb"] == "promote" and row["step"] == "windows-switch"
            )
            promotion_dir = state / "promotions" / "gen-2"
            evidence_map = Path(
                next(path for path in windows_switch["evidence"] if path.endswith("evidence-map.json"))
            )
            mapped_sources = {
                Path(row["source"]).name
                for group in json.loads(evidence_map.read_text(encoding="utf-8"))
                for row in group
            }
            self.assertEqual(
                mapped_sources,
                {
                    "windows-promote.out",
                    "windows-promote.out.stderr",
                    "windows-promote.out.transport.json",
                    "windows-promote.out.ssh-rc",
                    "windows-promote.path",
                    "windows-promote.path.stderr",
                },
            )
            self.assertTrue(
                all(
                    Path(path).is_relative_to(promotion_dir / "evidence-immutable")
                    for path in windows_switch["evidence"]
                )
            )
            steps = {row["step"]: row["status"] for row in receipt["steps"]}
            self.assertEqual(steps["windows-switch"], "failed")
            self.assertNotIn("open-admission", steps)
            self.assertNotIn("platform open_admission", order.read_text(encoding="utf-8"))

    def test_windows_lost_or_contradictory_results_stay_unknown(self) -> None:
        cases = (
            ("structured-unknown", 2, '{"passed":false,"code":"instance_lifecycle_exception","verb":"promote"}\r\n'),
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

    def test_stage_acl_exceptions_are_definite_and_raw_evidence_is_preserved(self) -> None:
        for operation, remote_rc in (("set", 1), ("assert", 1), ("set", 0), ("assert", 2)):
            with self.subTest(operation=operation, remote_rc=remote_rc), tempfile.TemporaryDirectory() as tmp:
                release, env, _ = self._prepare(Path(tmp), fresh=False, ready_after=1, db_exists=True)
                self.assertEqual(self._install(release, env).returncode, 0)
                state = Path(tmp) / "state"
                before = (state / "install-record.json").read_bytes()
                output = Path(tmp) / "stage-exception.out"
                document = {
                    "passed": False, "code": "instance_lifecycle_exception", "verb": "stage",
                    "phase": "stage-acl-" + operation, "role": "endpoint_config", "operation": operation,
                    "hresult": -2147024891, "error_id": "UnauthorizedAccessException",
                }
                raw = ("pip succeeded\n" + json.dumps(document) + "\n").encode()
                output.write_bytes(raw)
                result = self._maintenance(
                    release, dict(env, FIXTURE_STAGE_OUTPUT=str(output), FIXTURE_STAGE_RC=str(remote_rc)),
                    "stage", "gen-2",
                )
                self.assertNotEqual(result.returncode, 0)
                expected = "unknown" if remote_rc == 2 else "failed"
                if remote_rc != 2:
                    self.assertIn("definite refusal", result.stderr)
                    self.assertNotIn("unacknowledged", result.stderr)
                else:
                    self.assertIn("unacknowledged", result.stderr)
                self.assertEqual((state / "install-record.json").read_bytes(), before)
                promotion = state / "promotions" / "gen-2"
                self.assertEqual((promotion / "windows-stage.out").read_bytes(), raw)
                self.assertEqual((promotion / "windows-stage.out.ssh-rc").read_text().strip(), str(remote_rc))
                receipt = json.loads((promotion / "receipt.json").read_text())
                step = next(row for row in receipt["steps"] if row["step"] == "windows-generation")
                self.assertEqual(step["status"], expected)

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
            'if [ "${1:-}" = "units" ] || [ "${1:-}" = "stop-executors" ] || [ "${1:-}" = "stop" ]; then\n'
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
            self.assertTrue(evidence.name.endswith("backup-staging-clean.out"))
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
            self.addCleanup(self._stop_runtime_processes, Path(tmp))
            self.assertEqual(self._install(release, control_env).returncode, 0)
            state = Path(tmp) / "state"
            control_env["EXPECTED_PLATFORM_DSN"] = self._derived_value(state, "gen-1", "PLATFORM_OPERATOR_DSN")
            self._health_double(release, control_env)
            self.assertEqual(self._maintenance(release, control_env, "stage", "gen-2").returncode, 0)
            self._clone_generation_stubs(state)
            self._linux_health_double(release)
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
            rolled = self._maintenance(
                release, dict(control_env, FIXTURE_RUNTIME_PROCESS="1"), "rollback"
            )
            self.assertEqual(rolled.returncode, 0, rolled.stdout + rolled.stderr)
            restored = json.loads((state / "install-record.json").read_text(encoding="utf-8"))
            self.assertEqual(restored["install_id"], "gen-1")
            self.assertEqual((Path(tmp) / "admission").read_text().strip(), "true")

    def test_ack_loss_records_unknown_and_reconciles_from_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            release, control_env, _ = self._prepare(Path(tmp), fresh=False, ready_after=1, db_exists=True)
            self.addCleanup(self._stop_runtime_processes, Path(tmp))
            self.assertEqual(self._install(release, control_env).returncode, 0)
            state = Path(tmp) / "state"
            control_env["EXPECTED_PLATFORM_DSN"] = self._derived_value(state, "gen-1", "PLATFORM_OPERATOR_DSN")
            self._health_double(release, control_env)
            self.assertEqual(self._maintenance(release, control_env, "stage", "gen-2").returncode, 0)
            self._clone_generation_stubs(state)
            self._linux_health_double(release)
            lost = self._maintenance(release, dict(control_env, FIXTURE_ACK_LOSS="1"), "promote", "gen-2")
            self.assertEqual(lost.returncode, 1)
            self.assertIn("unacknowledged", lost.stderr)
            active = json.loads((state / "install-record.json").read_text(encoding="utf-8"))
            self.assertEqual(active["install_id"], "gen-1")
            self.assertEqual((Path(tmp) / "admission").read_text().strip(), "true")
            receipt = json.loads((state / "promotions" / "gen-2" / "receipt.json").read_text(encoding="utf-8"))
            steps = {row["step"]: row["status"] for row in receipt["steps"]}
            self.assertEqual(steps["windows-switch"], "unknown")
            reconciled = self._maintenance(
                release,
                dict(control_env, FIXTURE_RUNTIME_PROCESS="1"),
                "rollback",
                "gen-2",
            )
            self.assertEqual(reconciled.returncode, 0, reconciled.stdout + reconciled.stderr)
            self.assertTrue(
                (state / "promotions" / "gen-2" / "windows-status.out.transport.json").is_file()
            )
            self.assertTrue(
                (state / "promotions" / "gen-2" / "windows-status.out.stderr").is_file()
            )
            transport_metadata = json.loads(
                (state / "promotions" / "gen-2" / "windows-status.out.transport.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(transport_metadata["timeout_seconds"], 480)
            self.assertEqual(transport_metadata["transport"]["framing"], "single-line-lf")
            self.assertEqual(len(transport_metadata["transport"]["script_sha256"]), 64)
            self.assertEqual(len(transport_metadata["transport"]["payload_sha256"]), 64)
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
            self.assertIn("module_check", lines)
            rollback_calls = [line for line in lines if line.startswith("ssh ") and "-Verb rollback" in line]
            self.assertEqual(len(rollback_calls), 1, lines[-8:])
            self.assertIn("previous-0001", rollback_calls[0])
            self.assertEqual(
                len([line for line in lines if line.startswith("ssh ") and "-Verb status" in line]), 1
            )
            rollback_linux = next(
                row
                for row in final["steps"]
                if row["verb"] == "rollback" and row["step"] == "rollback-linux"
            )
            for entry in rollback_linux["evidence_digests"]:
                path = Path(entry["path"])
                self.assertTrue(path.is_file())
                self.assertEqual(
                    hashlib.sha256(path.read_bytes()).hexdigest(),
                    entry["sha256"],
                )
            receipt_before_replay = (
                state / "promotions" / "gen-2" / "receipt.json"
            ).read_bytes()
            self._stop_runtime_processes(Path(tmp))
            replayed = self._maintenance(
                release,
                dict(control_env, FIXTURE_RUNTIME_PROCESS="1"),
                "rollback",
                "gen-2",
            )
            self.assertEqual(replayed.returncode, 0, replayed.stdout + replayed.stderr)
            self.assertIn("replay complete", replayed.stdout)
            self.assertEqual(
                (state / "promotions" / "gen-2" / "receipt.json").read_bytes(),
                receipt_before_replay,
            )

    def test_status_remote_path_failure_cannot_freeze_a_stale_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            release, control_env, order = self._prepare(
                Path(tmp),
                fresh=False,
                ready_after=1,
                db_exists=True,
            )
            self.addCleanup(self._stop_runtime_processes, Path(tmp))
            self.assertEqual(self._install(release, control_env).returncode, 0)
            state = Path(tmp) / "state"
            control_env["EXPECTED_PLATFORM_DSN"] = self._derived_value(
                state,
                "gen-1",
                "PLATFORM_OPERATOR_DSN",
            )
            self._health_double(release, control_env)
            self.assertEqual(
                self._maintenance(release, control_env, "stage", "gen-2").returncode,
                0,
            )
            self._clone_generation_stubs(state)
            self._linux_health_double(release)
            lost = self._maintenance(
                release,
                dict(control_env, FIXTURE_ACK_LOSS="1"),
                "promote",
                "gen-2",
            )
            self.assertEqual(lost.returncode, 1)
            receipt_dir = state / "promotions" / "gen-2"
            stale_output = receipt_dir / "windows-status.out"
            stale_output.write_text(
                '{"passed":true,"verb":"status","code":null}\n',
                encoding="utf-8",
            )
            (receipt_dir / "windows-status.remote.txt").mkdir()
            active_before = (state / "install-record.json").read_bytes()
            admission = Path(tmp) / "admission"
            admission_before = admission.read_bytes()
            order.write_text("", encoding="utf-8")

            failed = self._maintenance(
                release,
                dict(control_env, FIXTURE_RUNTIME_PROCESS="1"),
                "rollback",
                "gen-2",
            )
            self.assertEqual(failed.returncode, 1)
            self.assertIn("endpoint status is unreadable", failed.stderr)
            self.assertEqual(stale_output.read_bytes(), b"")
            self.assertEqual((state / "install-record.json").read_bytes(), active_before)
            self.assertEqual(admission.read_bytes(), admission_before)
            self.assertNotIn(
                "-Verb status",
                order.read_text(encoding="utf-8"),
            )
            receipt = json.loads(
                (receipt_dir / "receipt.json").read_text(encoding="utf-8")
            )
            rollback_windows = next(
                row
                for row in reversed(receipt["steps"])
                if row["verb"] == "rollback" and row["step"] == "rollback-windows"
            )
            self.assertEqual(rollback_windows["status"], "unknown")
            frozen_outputs = [
                Path(entry["path"])
                for entry in rollback_windows["evidence_digests"]
                if entry["path"].endswith(".out")
            ]
            self.assertTrue(frozen_outputs)
            for frozen in frozen_outputs:
                self.assertEqual(frozen.read_bytes(), b"")

    def test_rollback_status_reconciliation_failures_keep_original_evidence(
        self,
    ) -> None:
        for mode in ("exec-failure", "malformed", "empty"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                release, control_env, order = self._prepare(
                    Path(tmp),
                    fresh=False,
                    ready_after=1,
                    db_exists=True,
                )
                self.addCleanup(self._stop_runtime_processes, Path(tmp))
                self.assertEqual(self._install(release, control_env).returncode, 0)
                state = Path(tmp) / "state"
                control_env["EXPECTED_PLATFORM_DSN"] = self._derived_value(
                    state,
                    "gen-1",
                    "PLATFORM_OPERATOR_DSN",
                )
                self._health_double(release, control_env)
                self.assertEqual(
                    self._maintenance(release, control_env, "stage", "gen-2").returncode,
                    0,
                )
                self._clone_generation_stubs(state)
                self._linux_health_double(release)
                lost = self._maintenance(
                    release,
                    dict(control_env, FIXTURE_ACK_LOSS="1"),
                    "promote",
                    "gen-2",
                )
                self.assertEqual(lost.returncode, 1)
                receipt_dir = state / "promotions" / "gen-2"
                (receipt_dir / "windows-status.reconciliation.txt").write_text(
                    "stale reconciliation output\n",
                    encoding="utf-8",
                )
                wrapper = Path(tmp) / "control-python"
                wrapper.write_text(
                    "#!/usr/bin/env bash\n"
                    "set -euo pipefail\n"
                    'if [ "$#" -eq 3 ] && [ "$1" = "-" ] '
                    '&& [[ "$2" == */windows-binding.json ]] '
                    '&& [[ "$3" == */windows-status.out ]]; then\n'
                    '  case "${FIXTURE_RECONCILE_MODE:-}" in\n'
                    "    exec-failure)\n"
                    "      printf 'partial reconciliation\\n'\n"
                    "      printf 'reconciliation executor failed\\n' >&2\n"
                    "      exit 9\n"
                    "      ;;\n"
                    "    malformed)\n"
                    "      printf 'not-json\\n'\n"
                    "      ;;\n"
                    "    empty)\n"
                    "      :\n"
                    "      ;;\n"
                    "  esac\n"
                    "  exit 0\n"
                    "fi\n"
                    'exec "$FIXTURE_REAL_PYTHON" "$@"\n',
                    encoding="utf-8",
                )
                wrapper.chmod(0o755)
                active_before = (state / "install-record.json").read_bytes()
                admission_before = (Path(tmp) / "admission").read_bytes()
                order.write_text("", encoding="utf-8")
                failed = self._maintenance(
                    release,
                    dict(
                        control_env,
                        CONTROL_PYTHON=str(wrapper),
                        FIXTURE_REAL_PYTHON=sys.executable,
                        FIXTURE_RECONCILE_MODE=mode,
                        FIXTURE_RUNTIME_PROCESS="1",
                    ),
                    "rollback",
                    "gen-2",
                )
                self.assertEqual(failed.returncode, 1)
                self.assertIn(
                    "endpoint status could not be reconciled"
                    if mode == "exec-failure"
                    else "endpoint reconciliation result is not one token"
                    if mode == "empty"
                    else "endpoint state is not provably recoverable",
                    failed.stderr,
                )
                self.assertEqual(
                    (state / "install-record.json").read_bytes(),
                    active_before,
                )
                self.assertEqual(
                    (Path(tmp) / "admission").read_bytes(),
                    admission_before,
                )
                receipt = json.loads(
                    (receipt_dir / "receipt.json").read_text(encoding="utf-8")
                )
                rollback_windows = next(
                    row
                    for row in reversed(receipt["steps"])
                    if row["verb"] == "rollback" and row["step"] == "rollback-windows"
                )
                self.assertEqual(rollback_windows["status"], "unknown")
                evidence_map = Path(
                    next(
                        path
                        for path in rollback_windows["evidence"]
                        if path.endswith("evidence-map.json")
                    )
                )
                sources = {
                    Path(entry["source"]).name
                    for group in json.loads(
                        evidence_map.read_text(encoding="utf-8")
                    )
                    for entry in group
                }
                self.assertTrue(
                    {
                        "windows-status.out",
                        "windows-status.path",
                        "windows-status.reconciliation.txt",
                        "windows-status.reconciliation.txt.stderr",
                        "windows-binding.json",
                    }.issubset(sources),
                    sources,
                )
                for entry in rollback_windows["evidence_digests"]:
                    path = Path(entry["path"])
                    self.assertTrue(path.is_file())
                    self.assertEqual(
                        hashlib.sha256(path.read_bytes()).hexdigest(),
                        entry["sha256"],
                    )
                self.assertNotIn(
                    "stale reconciliation output",
                    (receipt_dir / "windows-status.reconciliation.txt").read_text(
                        encoding="utf-8"
                    ),
                )
                self.assertFalse(
                    [
                        line
                        for line in order.read_text(encoding="utf-8").splitlines()
                        if "-Verb rollback" in line
                    ]
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
            self._linux_health_double(release)
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

    def test_completed_promote_and_rollback_replays_are_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            release, control_env, order = self._prepare(
                Path(tmp), fresh=False, ready_after=1, db_exists=True
            )
            self.addCleanup(self._stop_runtime_processes, Path(tmp))
            self.assertEqual(self._install(release, control_env).returncode, 0)
            state = Path(tmp) / "state"
            control_env["EXPECTED_PLATFORM_DSN"] = self._derived_value(
                state, "gen-1", "PLATFORM_OPERATOR_DSN"
            )
            self._health_double(release, control_env, verify_free_bytes=2)
            self.assertEqual(
                self._maintenance(release, control_env, "stage", "gen-2").returncode,
                0,
            )
            stage_receipt = json.loads(
                (state / "promotions" / "gen-2" / "receipt.json").read_text(
                    encoding="utf-8"
                )
            )
            windows_generation = next(
                row
                for row in stage_receipt["steps"]
                if row["verb"] == "promote" and row["step"] == "windows-generation"
            )
            self.assertTrue(
                any(path.endswith(".transport.json") for path in windows_generation["evidence"])
            )
            self._clone_generation_stubs(state)
            self._linux_health_double(release)
            promoted = self._maintenance(release, control_env, "promote", "gen-2")
            self.assertEqual(promoted.returncode, 0, promoted.stdout + promoted.stderr)
            receipt_path = state / "promotions" / "gen-2" / "receipt.json"
            receipt_before = receipt_path.read_bytes()
            receipt_document = json.loads(receipt_before)
            completed_promote_evidence = [
                row["path"]
                for step in receipt_document["steps"]
                if step["verb"] == "promote" and step["status"] == "completed"
                for row in step["evidence_digests"]
            ]
            self.assertEqual(
                len(completed_promote_evidence),
                len(set(completed_promote_evidence)),
            )
            promote_evidence_before_rollback = {
                row["path"]: Path(row["path"]).read_bytes()
                for step in receipt_document["steps"]
                if step["verb"] == "promote" and step["status"] == "completed"
                for row in step["evidence_digests"]
            }
            endpoint_health = {
                Path(row["path"]).name
                for step in receipt_document["steps"]
                if step["verb"] == "promote" and step["step"] == "endpoint-identity"
                for row in step["evidence_digests"]
                if "windows-health" in Path(row["path"]).name
            }
            verify_health = {
                Path(row["path"]).name
                for step in receipt_document["steps"]
                if step["verb"] == "promote" and step["step"] == "verify"
                for row in step["evidence_digests"]
                if "windows-health" in Path(row["path"]).name
            }
            self.assertTrue(endpoint_health)
            self.assertTrue(verify_health)
            self.assertTrue(endpoint_health.isdisjoint(verify_health))
            promoted_active_record = Path(tmp) / "promoted-active-record.json"
            promoted_active_record.write_bytes((state / "install-record.json").read_bytes())
            windows_step = next(
                row
                for row in receipt_document["steps"]
                if row["verb"] == "promote" and row["step"] == "windows-switch"
            )
            self.assertTrue(
                any(path.endswith(".transport.json") for path in windows_step["evidence"])
            )
            self.assertTrue(any(path.endswith(".stderr") for path in windows_step["evidence"]))
            before_promote_replay = order.read_text(encoding="utf-8").splitlines()
            drift = self._maintenance(
                release,
                dict(control_env, FIXTURE_STATUS="foreign"),
                "promote",
                "gen-2",
            )
            self.assertNotEqual(drift.returncode, 0)
            self.assertIn("replay verification failed", drift.stderr)
            self.assertEqual(receipt_path.read_bytes(), receipt_before)
            self._stop_runtime_processes(Path(tmp))
            replayed = self._maintenance(release, control_env, "promote", "gen-2")
            self.assertEqual(replayed.returncode, 0, replayed.stdout + replayed.stderr)
            self.assertIn("replay complete", replayed.stdout)
            self.assertEqual(receipt_path.read_bytes(), receipt_before)
            after_promote_replay = order.read_text(encoding="utf-8").splitlines()
            for marker in ("platform close_admission", "platform open_admission", "pg_dump"):
                self.assertEqual(
                    sum(marker in line for line in after_promote_replay),
                    sum(marker in line for line in before_promote_replay),
                    marker,
                )

            rolled = self._maintenance(
                release,
                dict(control_env, FIXTURE_RUNTIME_PROCESS="1"),
                "rollback",
                "gen-2",
            )
            self.assertEqual(rolled.returncode, 0, rolled.stdout + rolled.stderr)
            for evidence_path, before in promote_evidence_before_rollback.items():
                self.assertEqual(Path(evidence_path).read_bytes(), before, evidence_path)
            promote_replay = run(
                [
                    "python3",
                    str(release / "operator" / "promotion.py"),
                    "replay",
                    "--state-root",
                    str(state),
                    "--install-id",
                    "gen-2",
                    "--verb",
                    "promote",
                    "--active-record",
                    str(promoted_active_record),
                ],
                {},
            )
            self.assertEqual(promote_replay.returncode, 0, promote_replay.stdout + promote_replay.stderr)
            self.assertIn("replay=completed", promote_replay.stdout)
            drifted_promote_replay = run(
                [
                    "python3",
                    str(release / "operator" / "promotion.py"),
                    "replay",
                    "--state-root",
                    str(state),
                    "--install-id",
                    "gen-2",
                    "--verb",
                    "promote",
                    "--active-record",
                    str(state / "install-record.json"),
                ],
                {},
            )
            self.assertEqual(
                drifted_promote_replay.returncode,
                11,
                drifted_promote_replay.stdout + drifted_promote_replay.stderr,
            )
            self.assertIn("replay=drift", drifted_promote_replay.stdout)
            receipt_before = receipt_path.read_bytes()
            before_rollback_replay = order.read_text(encoding="utf-8").splitlines()
            self._stop_runtime_processes(Path(tmp))
            replayed = self._maintenance(
                release,
                dict(control_env, FIXTURE_RUNTIME_PROCESS="1"),
                "rollback",
                "gen-2",
            )
            self.assertEqual(replayed.returncode, 0, replayed.stdout + replayed.stderr)
            self.assertIn("replay complete", replayed.stdout)
            self.assertEqual(receipt_path.read_bytes(), receipt_before)
            after_rollback_replay = order.read_text(encoding="utf-8").splitlines()
            for marker in ("platform close_admission", "platform open_admission", "pg_dump"):
                self.assertEqual(
                    sum(marker in line for line in after_rollback_replay),
                    sum(marker in line for line in before_rollback_replay),
                    marker,
                )
            rollback_step = next(
                row
                for row in reversed(
                    json.loads(receipt_before)["steps"]
                )
                if row["verb"] == "rollback" and row["step"] == "rollback-verify"
            )
            tampered_rollback_evidence = Path(rollback_step["evidence"][0])
            tampered_rollback_evidence.write_bytes(
                tampered_rollback_evidence.read_bytes() + b"tampered\n"
            )
            cross_verb_replay = run(
                [
                    "python3",
                    str(release / "operator" / "promotion.py"),
                    "replay",
                    "--state-root",
                    str(state),
                    "--install-id",
                    "gen-2",
                    "--verb",
                    "promote",
                    "--active-record",
                    str(promoted_active_record),
                ],
                {},
            )
            self.assertEqual(
                cross_verb_replay.returncode,
                11,
                cross_verb_replay.stdout + cross_verb_replay.stderr,
            )
            self.assertIn("replay=partial", cross_verb_replay.stdout)

    def test_second_rollback_to_same_resident_preserves_first_receipt_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            release, control_env, _ = self._prepare(
                Path(tmp), fresh=False, ready_after=1, db_exists=True
            )
            self.addCleanup(self._stop_runtime_processes, Path(tmp))
            self.assertEqual(self._install(release, control_env).returncode, 0)
            state = Path(tmp) / "state"
            control_env["EXPECTED_PLATFORM_DSN"] = self._derived_value(
                state, "gen-1", "PLATFORM_OPERATOR_DSN"
            )
            self._health_double(release, control_env)

            for generation in ("gen-2", "gen-3"):
                remote_archive = Path(tmp) / "remote" / f"{generation}.zip"
                generation_env = dict(control_env, FIXTURE_REMOTE_ARCHIVE=str(remote_archive))
                staged = self._maintenance(release, generation_env, "stage", generation)
                self.assertEqual(staged.returncode, 0, staged.stdout + staged.stderr)
                self._clone_generation_stubs(state, generation)
                self._linux_health_double(release)
                promoted = self._maintenance(release, generation_env, "promote", generation)
                self.assertEqual(promoted.returncode, 0, promoted.stdout + promoted.stderr)
                promoted_record = Path(tmp) / f"{generation}-promoted-record.json"
                promoted_record.write_bytes((state / "install-record.json").read_bytes())
                rolled = self._maintenance(
                    release,
                    dict(generation_env, FIXTURE_RUNTIME_PROCESS="1"),
                    "rollback",
                    generation,
                )
                self.assertEqual(rolled.returncode, 0, rolled.stdout + rolled.stderr)
                self.assertEqual(
                    json.loads((state / "install-record.json").read_text(encoding="utf-8"))[
                        "install_id"
                    ],
                    "gen-1",
                )
                self._stop_runtime_processes(Path(tmp))
                if generation == "gen-2":
                    first_receipt = (
                        state / "promotions" / "gen-2" / "receipt.json"
                    ).read_bytes()
                    first_receipt_document = json.loads(first_receipt)
                    first_evidence = {
                        row["path"]: Path(row["path"]).read_bytes()
                        for step in first_receipt_document["steps"]
                        if step["verb"] in {"promote", "rollback"}
                        and step["status"] == "completed"
                        for row in step["evidence_digests"]
                    }
                    first_rollback_active = Path(tmp) / "gen-2-rollback-record.json"
                    first_rollback_active.write_bytes(
                        (state / "install-record.json").read_bytes()
                    )
                    first_promotion_roots = tuple(
                        sorted(path.name for path in (state / "promotions").iterdir())
                    )
                    first_promotion_files = {
                        path.relative_to(state / "promotions").as_posix(): hashlib.sha256(
                            path.read_bytes()
                        ).hexdigest()
                        for path in (state / "promotions").rglob("*")
                        if path.is_file()
                    }

            self.assertEqual(
                (state / "promotions" / "gen-2" / "receipt.json").read_bytes(),
                first_receipt,
            )
            for evidence_path, before in first_evidence.items():
                self.assertEqual(Path(evidence_path).read_bytes(), before, evidence_path)
            for root_name in first_promotion_roots:
                root = state / "promotions" / root_name
                self.assertTrue(root.is_dir(), root)
                after_root = {
                    path.relative_to(state / "promotions").as_posix(): hashlib.sha256(
                        path.read_bytes()
                    ).hexdigest()
                    for path in root.rglob("*")
                    if path.is_file()
                }
                before_root = {
                    name: digest
                    for name, digest in first_promotion_files.items()
                    if Path(name).parts[0] == root_name
                }
                self.assertEqual(after_root, before_root, root_name)
            promote_replay = run(
                [
                    "python3",
                    str(release / "operator" / "promotion.py"),
                    "replay",
                    "--state-root",
                    str(state),
                    "--install-id",
                    "gen-2",
                    "--verb",
                    "promote",
                    "--active-record",
                    str(Path(tmp) / "gen-2-promoted-record.json"),
                ],
                {},
            )
            self.assertEqual(promote_replay.returncode, 0, promote_replay.stdout + promote_replay.stderr)
            self.assertIn("replay=completed", promote_replay.stdout)
            rollback_replay = run(
                [
                    "python3",
                    str(release / "operator" / "promotion.py"),
                    "replay",
                    "--state-root",
                    str(state),
                    "--install-id",
                    "gen-2",
                    "--verb",
                    "rollback",
                    "--active-record",
                    str(first_rollback_active),
                ],
                {},
            )
            self.assertEqual(rollback_replay.returncode, 0, rollback_replay.stdout + rollback_replay.stderr)
            self.assertIn("replay=completed", rollback_replay.stdout)

    def test_ack_loss_with_live_before_state_records_never_switched(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            release, control_env, _ = self._prepare(Path(tmp), fresh=False, ready_after=1, db_exists=True)
            self.addCleanup(self._stop_runtime_processes, Path(tmp))
            self.assertEqual(self._install(release, control_env).returncode, 0)
            state = Path(tmp) / "state"
            control_env["EXPECTED_PLATFORM_DSN"] = self._derived_value(state, "gen-1", "PLATFORM_OPERATOR_DSN")
            self._health_double(release, control_env)
            self.assertEqual(self._maintenance(release, control_env, "stage", "gen-2").returncode, 0)
            self._clone_generation_stubs(state)
            self._linux_health_double(release)
            lost = self._maintenance(release, dict(control_env, FIXTURE_ACK_LOSS="1"), "promote", "gen-2")
            self.assertEqual(lost.returncode, 1)
            reconciled = self._maintenance(
                release,
                dict(
                    control_env,
                    FIXTURE_STATUS="before",
                    FIXTURE_RUNTIME_PROCESS="1",
                ),
                "rollback",
                "gen-2",
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


class BackupStreamingUnitTests(unittest.TestCase):
    """Seal and open stream tar bytes through OpenSSL without a plaintext temp archive."""

    CONTROL = OPERATOR / "backup.py"

    def test_seal_failure_removes_its_temp_and_leaves_retry_clean(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            (source / "record.json").write_text("{}\n", encoding="utf-8")
            out = root / "sealed" / "backup.tar.enc"
            key = root / "secrets" / "backup.key"
            bindir = root / "bin"
            bindir.mkdir()
            log = root / "openssl.log"
            failing = (
                "#!/usr/bin/env bash\n"
                "set -euo pipefail\n"
                'out=""\n'
                'previous=""\n'
                'for argument in "$@"; do\n'
                '  [ "$previous" = "-out" ] && out="$argument"\n'
                '  previous="$argument"\n'
                "done\n"
                "cat >/dev/null\n"
                "printf 'PARTIAL' > \"$out\"\n"
                "exit 9\n"
            )
            write_stub(bindir / "openssl", failing)
            env = {
                "PATH": f"{bindir}:{os.environ.get('PATH', '/usr/bin:/bin')}",
                "OPENSSL_LOG": str(log),
            }
            command = [
                sys.executable,
                str(self.CONTROL),
                "seal",
                "--source",
                str(source),
                "--key",
                str(key),
                "--out",
                str(out),
            ]
            failed = run(command, env)
            self.assertNotEqual(failed.returncode, 0, failed.stdout + failed.stderr)
            self.assertIn("openssl failed", failed.stderr)
            self.assertFalse(out.exists())
            self.assertEqual(list(out.parent.iterdir()), [])

            out.parent.mkdir(exist_ok=True)
            out.write_bytes(b"preexisting")
            failed_existing = run(command, env)
            self.assertNotEqual(failed_existing.returncode, 0, failed_existing.stdout + failed_existing.stderr)
            self.assertEqual(out.read_bytes(), b"preexisting")
            self.assertEqual(list(out.parent.iterdir()), [out])

            write_stub(
                bindir / "openssl",
                "#!/usr/bin/env bash\n"
                "set -euo pipefail\n"
                'out=""\n'
                'previous=""\n'
                'for argument in "$@"; do\n'
                '  [ "$previous" = "-out" ] && out="$argument"\n'
                '  previous="$argument"\n'
                "done\n"
                "cat >/dev/null\n"
                "printf 'sealed-on-retry\\n' > \"$out\"\n",
            )
            retried = run(command, env)
            self.assertEqual(retried.returncode, 0, retried.stdout + retried.stderr)
            self.assertEqual(out.read_bytes(), b"sealed-on-retry\n")
            self.assertEqual(list(out.parent.iterdir()), [out])

    def test_seal_has_no_plaintext_tar_temp_and_passes_no_input_file_to_openssl(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            (source / "large.bin").write_bytes(b"x" * (2 * 1024 * 1024 + 17))
            (source / "nested").mkdir()
            (source / "nested" / "record.json").write_text("{}", encoding="utf-8")
            out = root / "sealed" / "newgen-backup.tar.enc"
            key = root / "secrets" / "backup.key"
            bindir = root / "bin"
            bindir.mkdir()
            log = root / "openssl.log"
            write_stub(
                bindir / "openssl",
                "#!/usr/bin/env bash\n"
                "set -euo pipefail\n"
                'out=""\n'
                'previous=""\n'
                'for argument in "$@"; do\n'
                '  [ "$previous" = "-out" ] && out="$argument"\n'
                '  previous="$argument"\n'
                "done\n"
                'printf "arg=%s\\n" "$@" >> "$OPENSSL_LOG"\n'
                'printf "before=%s\\n" "$(find "$(dirname "$out")" -maxdepth 1 -type f -printf "%f," 2>/dev/null)"'
                ' >> "$OPENSSL_LOG"\n'
                "cat >/dev/null\n"
                "printf 'double-openssl-output\\n' > \"$out\"\n",
            )
            result = run(
                [
                    sys.executable,
                    str(self.CONTROL),
                    "seal",
                    "--source",
                    str(source),
                    "--key",
                    str(key),
                    "--out",
                    str(out),
                ],
                {
                    "PATH": f"{bindir}:{os.environ.get('PATH', '/usr/bin:/bin')}",
                    "OPENSSL_LOG": str(log),
                },
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(list(out.parent.iterdir()), [out])
            calls = log.read_text(encoding="utf-8")
            self.assertNotIn("arg=-in", calls)
            self.assertIn("arg=-out\n", calls)
            self.assertIn(".partial", calls)

    def test_open_streams_openssl_stdout_and_keeps_member_and_extra_checks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload = root / "payload.json"
            payload.write_text('{"install":"resident"}\n', encoding="utf-8")
            member = "state/install-record.json"
            plaintext_tar = root / "plaintext.tar"
            with tarfile.open(plaintext_tar, "w") as tar:
                tar.add(payload, arcname=member)
            extra_tar = root / "extra.tar"
            with tarfile.open(extra_tar, "w") as tar:
                tar.add(payload, arcname=member)
                tar.add(payload, arcname="state/unexpected.json")
            changed = root / "changed.json"
            changed.write_text('{"install":"changed"}\n', encoding="utf-8")
            changed_tar = root / "changed.tar"
            with tarfile.open(changed_tar, "w") as tar:
                tar.add(changed, arcname=member)

            inputs = root / "input"
            inputs.mkdir()
            archive = inputs / "backup.tar.enc"
            archive.write_bytes(b"sealed-input")
            key = inputs / "backup.key"
            key.write_bytes(b"test-key")
            manifest = inputs / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "schema": "solidworks-to-urdf.promote-backup/v1",
                        "files": {member: hashlib.sha256(payload.read_bytes()).hexdigest()},
                        "encrypted": {
                            "path": str(archive),
                            "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                            "cipher": "aes-256-cbc",
                            "key": str(key),
                        },
                    }
                ),
                encoding="utf-8",
            )
            bindir = root / "bin"
            bindir.mkdir()
            log = root / "openssl.log"
            write_stub(
                bindir / "openssl",
                "#!/usr/bin/env bash\n"
                "set -euo pipefail\n"
                "input=\"\"\n"
                "previous=\"\"\n"
                "for argument in \"$@\"; do\n"
                "  [ \"$previous\" = \"-in\" ] && input=\"$argument\"\n"
                "  previous=\"$argument\"\n"
                "done\n"
                "printf \"arg=%s\\n\" \"$@\" >> \"$OPENSSL_LOG\"\n"
                "printf \"before=%s\\n\" \"$(find \"$FIXTURE_DEST_PARENT\" -maxdepth 1 "
                "-type f -printf \"%f,\" 2>/dev/null)\""
                " >> \"$OPENSSL_LOG\"\n"
                "cat \"$input\" >/dev/null\n"
                "if [ \"${FIXTURE_OPEN_FAIL:-0}\" = \"1\" ]; then printf 'partial'; exit 9; fi\n"
                "cat \"$FIXTURE_PLAINTEXT_TAR\"\n",
            )
            base_env = {
                "PATH": f"{bindir}:{os.environ.get('PATH', '/usr/bin:/bin')}",
                "OPENSSL_LOG": str(log),
            }

            def invoke(dest_parent: Path, tar_path: Path, *, fail: bool = False) -> subprocess.CompletedProcess:
                dest_parent.mkdir(exist_ok=True)
                return run(
                    [
                        sys.executable,
                        str(self.CONTROL),
                        "open",
                        "--archive",
                        str(archive),
                        "--key",
                        str(key),
                        "--manifest",
                        str(manifest),
                        "--dest",
                        str(dest_parent / "restored"),
                    ],
                    {
                        **base_env,
                        "FIXTURE_DEST_PARENT": str(dest_parent),
                        "FIXTURE_PLAINTEXT_TAR": str(tar_path),
                        "FIXTURE_OPEN_FAIL": "1" if fail else "0",
                    },
                )

            failed = invoke(root / "retry", plaintext_tar, fail=True)
            self.assertNotEqual(failed.returncode, 0, failed.stdout + failed.stderr)
            self.assertFalse((root / "retry" / "restored").exists())
            self.assertEqual(list((root / "retry").iterdir()), [])
            retried = invoke(root / "retry", plaintext_tar)
            self.assertEqual(retried.returncode, 0, retried.stdout + retried.stderr)
            self.assertEqual((root / "retry" / "restored" / member).read_bytes(), payload.read_bytes())

            opened = invoke(root / "ok", plaintext_tar)
            self.assertEqual(opened.returncode, 0, opened.stdout + opened.stderr)
            self.assertEqual((root / "ok" / "restored" / member).read_bytes(), payload.read_bytes())
            self.assertEqual(list((root / "ok").iterdir()), [root / "ok" / "restored"])
            calls = log.read_text(encoding="utf-8")
            self.assertNotIn("arg=-out", calls)
            self.assertIn("before=\n", calls)

            extra = invoke(root / "extra", extra_tar)
            self.assertNotEqual(extra.returncode, 0, extra.stdout + extra.stderr)
            self.assertIn("members outside the manifest", extra.stderr)
            self.assertFalse((root / "extra" / "restored").exists())
            self.assertEqual(list((root / "extra").iterdir()), [])
            changed_result = invoke(root / "changed", changed_tar)
            self.assertNotEqual(changed_result.returncode, 0, changed_result.stdout + changed_result.stderr)
            self.assertIn("restored member differs from the manifest", changed_result.stderr)
            self.assertFalse((root / "changed" / "restored").exists())
            self.assertEqual(list((root / "changed").iterdir()), [])


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


if __name__ == "__main__":
    unittest.main()
