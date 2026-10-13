"""Operator deployment renderer/lifecycle tests (stdlib only; external services are mocked)."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from description_pipeline import schemas

from .contract_wheel import write_contract_wheel

ROOT = Path(__file__).resolve().parents[2]
OPERATOR = ROOT / "deploy" / "operator"
RENDER = OPERATOR / "render_operator.py"
CONTROL = OPERATOR / "operatorctl.sh"
HEALTH = OPERATOR / "health.sh"


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
    env_file.write_text("".join(f"{key}={value}\n" for key, value in env.items()), encoding="utf-8")
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
    release_document = {
        "schema_version": "solidworks-to-urdf.release/v1",
        "pipeline_id": "solidworks-to-urdf",
        "version": "1.4.0",
        "source_commit": "c" * 40,
        "source_sha256": "a" * 64,
        "artifacts": {wheel.name: hashlib.sha256(wheel.read_bytes()).hexdigest()},
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
        ],
        {},
    )
    if built.returncode != 0:
        raise AssertionError(built.stderr)
    return config_path, Path(env_path), env


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


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
    def _render_units(self, tmp: Path) -> tuple[Path, Path, Path, Path]:
        config_path, env_path, env = platform_fixture(tmp)
        state = Path(env["OPERATOR_STATE"])
        config_home = tmp / "config"
        target = config_home / "systemd" / "user"
        target.mkdir(parents=True)
        rendered = run(
            [sys.executable, str(RENDER), "--env-file", str(env_path), "--units-dir", str(target)],
            {"XDG_CONFIG_HOME": str(config_home)},
        )
        self.assertEqual(rendered.returncode, 0, rendered.stderr)
        airflow_env = {"XDG_CONFIG_HOME": str(config_home)}
        for line in (state / "resolved.env").read_text(encoding="utf-8").splitlines():
            key, _, value = line.partition("=")
            if key:
                airflow_env[key] = value
        airflow = run(["bash", str(OPERATOR.parent / "airflow" / "services.sh"), "render"], airflow_env)
        self.assertEqual(airflow.returncode, 0, airflow.stderr)
        return config_path, state, config_home, target

    def _admission_fixture(self, tmp: Path, *, register_after: int = 0, unpause_rc: int = 0, pause_rc: int = 0):
        config_path, state, config_home, _ = self._render_units(tmp)
        derived_env: dict[str, str] = {}
        for line in (state / "derived" / "gen-1" / "operator.env").read_text(encoding="utf-8").splitlines():
            key, _, value = line.partition("=")
            if key:
                derived_env[key] = value
        bindir = tmp / "bin"
        bindir.mkdir()
        pgbin = state / "airflow-pg" / "root" / "usr" / "lib" / "postgresql" / "14" / "bin"
        pgbin.mkdir(parents=True, exist_ok=True)
        write_stub(pgbin / "pg_isready", '#!/bin/bash\necho "pg_isready" >> "$ORDER_LOG"\nexit 0\n')
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
        }
        return config_path, env, admission, order

    def test_start_opens_the_admission_only_after_the_ingress_is_ready(self) -> None:
        for register_after in (0, 1):
            with self.subTest(register_after=register_after), tempfile.TemporaryDirectory() as tmp:
                config_path, env, admission, order = self._admission_fixture(Path(tmp), register_after=register_after)
                result = run(["bash", str(CONTROL), "start", "--config", str(config_path)], env)
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
                self.assertLess(ready, lines.index("platform open_admission"))
                self.assertEqual(lines.count("airflow dags unpause solidworks_to_urdf"), register_after + 1)
        with tempfile.TemporaryDirectory() as tmp:
            config_path, env, admission, order = self._admission_fixture(Path(tmp))
            admission.write_text("false")  # a leftover open gate from an interrupted window
            env["START_INGRESS_RC"] = "7"
            result = run(["bash", str(CONTROL), "start", "--config", str(config_path)], env)
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
            self.assertNotIn("platform open_admission", order_text)

    def test_unpause_failure_does_not_start_ingress(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path, env, admission, order = self._admission_fixture(Path(tmp), unpause_rc=7)
            result = run(["bash", str(CONTROL), "start", "--config", str(config_path)], env)
            self.assertEqual(result.returncode, 7, result.stdout + result.stderr)
            self.assertEqual(admission.read_text().strip(), "true")
            self.assertNotIn("description-portal.service", order.read_text())
            self.assertNotIn("description-operator-proxy.service", order.read_text())

    def _project(self, env: dict, data: dict, rc: int) -> None:
        Path(env["PROJECTION_FILE"]).write_text(json.dumps(data), encoding="utf-8")
        env["PROJECTION_RC"] = str(rc)

    def test_stop_requires_closed_admission_and_settled_store(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path, env, admission, order = self._admission_fixture(Path(tmp))
            admission.write_text("false")
            result = run(["bash", str(CONTROL), "stop", "--config", str(config_path)], env)
            self.assertEqual(result.returncode, 1)
            self.assertIn("run drain first", result.stderr)
            self.assertFalse(order.exists())
        with tempfile.TemporaryDirectory() as tmp:
            config_path, env, admission, order = self._admission_fixture(Path(tmp))
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
            result = run(["bash", str(CONTROL), "stop", "--config", str(config_path)], env)
            self.assertEqual(result.returncode, 1)
            self.assertIn("stop refused", result.stderr)
            self.assertFalse(order.exists())
        with tempfile.TemporaryDirectory() as tmp:
            config_path, env, admission, order = self._admission_fixture(Path(tmp))
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
            result = run(["bash", str(CONTROL), "stop", "--config", str(config_path)], env)
            self.assertEqual(result.returncode, 1)
            self.assertIn("stop refused", result.stderr)
            self.assertFalse(order.exists())
        with tempfile.TemporaryDirectory() as tmp:
            config_path, env, admission, order = self._admission_fixture(Path(tmp))
            result = run(["bash", str(CONTROL), "stop", "--config", str(config_path)], env)
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
            config_path, env, admission, order = self._admission_fixture(Path(tmp))
            admission.write_text("false")
            result = run(["bash", str(CONTROL), "drain", "--config", str(config_path)], env)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(admission.read_text().strip(), "true")
            self.assertIn("airflow dags pause solidworks_to_urdf", order.read_text())
            self.assertIn('"settled": true', result.stdout)
        with tempfile.TemporaryDirectory() as tmp:
            config_path, env, admission, order = self._admission_fixture(Path(tmp), pause_rc=9)
            result = run(["bash", str(CONTROL), "drain", "--config", str(config_path)], env)
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertIn("could not pause the DAG scheduling", result.stderr)
        with tempfile.TemporaryDirectory() as tmp:
            config_path, env, admission, order = self._admission_fixture(Path(tmp))
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
            result = run(["bash", str(CONTROL), "drain", "--config", str(config_path)], env)
            self.assertEqual(result.returncode, 1)
            self.assertIn("reconcile must close them", result.stderr)
            self.assertIn('"unresolved"', result.stdout)
        with tempfile.TemporaryDirectory() as tmp:
            config_path, env, admission, order = self._admission_fixture(Path(tmp))
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
            result = run(["bash", str(CONTROL), "drain", "--config", str(config_path)], env)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn('"run_id": "run-active"', result.stdout)

    def test_gates_query_with_the_installed_platform_dsn(self) -> None:
        for action in ("drain", "stop"):
            with self.subTest(action=action), tempfile.TemporaryDirectory() as tmp:
                config_path, env, _, _ = self._admission_fixture(Path(tmp))
                result = run(["bash", str(CONTROL), action, "--config", str(config_path)], env)
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
        self.assertIn(":${AIRFLOW_API_PORT}/", health)
        self.assertIn("Authorization", health)
        self.assertIn("handoff_roots", health)
        self.assertIn("solidworks_windows", health)
        self.assertIn("/auth/feishu/health", health)
        self.assertIn("FEISHU_ENV_FILE", health)
        self.assertIn("FEISHU_APP_SECRET_FILE", health)
        self.assertIn('if status == 200 and body.get("configured") is True', health)
        self.assertIn("connection.password == expected_token", health)
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

    def test_api_server_unit_loads_the_optional_feishu_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, state, _, target = self._render_units(Path(tmp))
            unit = (target / "description-airflow-api-server.service").read_text(encoding="utf-8")
            self.assertIn(f"EnvironmentFile=-{state}/feishu.env", unit)
            self.assertNotIn("@", unit)
            scheduler = (target / "description-airflow-scheduler.service").read_text(encoding="utf-8")
            self.assertNotIn("EnvironmentFile", scheduler)
            control = CONTROL.read_text(encoding="utf-8")
            self.assertIn('FEISHU_ENV_FILE="${FEISHU_ENV_FILE:-$AIRFLOW_HOME/feishu.env}"', control)

    def test_scripts_are_syntactically_valid(self) -> None:
        for script in (
            CONTROL,
            HEALTH,
            OPERATOR / "scripts" / "install_toolchain.sh",
            OPERATOR / "scripts" / "install_proxy.sh",
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
        (release / "operator" / "render_operator.py").write_bytes(RENDER.read_bytes())
        for module in ("platform_config.py", "platform_env.py", "install_record.py"):
            (release / "operator" / module).write_bytes((OPERATOR / module).read_bytes())
        (release / "airflow" / "dags" / "solidworks_to_urdf.py").write_text("# dag fixture\n", encoding="utf-8")
        for template in ("nginx.conf.template", "portal.json.template"):
            (release / "operator" / template).write_bytes((OPERATOR / template).read_bytes())
        (release / "operator" / "systemd").mkdir()
        for unit in (OPERATOR / "systemd").glob("*.service"):
            (release / "operator" / "systemd" / unit.name).write_bytes(unit.read_bytes())
        (release / "platform").mkdir()
        for artifact in ("apply.sh", "schema.sql", "views.sql", "roles.sql"):
            (release / "platform" / artifact).write_bytes((OPERATOR.parent / "platform" / artifact).read_bytes())
        write_stub(
            release / "operator" / "scripts" / "install_toolchain.sh",
            '#!/usr/bin/env bash\necho "AIRFLOW_PYTHON=/usr/bin/python3"\n',
        )
        write_stub(release / "operator" / "scripts" / "install_proxy.sh", "#!/usr/bin/env bash\nexit 0\n")
        write_stub(
            release / "airflow" / "services.sh", '#!/usr/bin/env bash\necho "services_render" >> "$ORDER_LOG"\nexit 0\n'
        )
        write_stub(
            release / "airflow" / "install.sh", '#!/usr/bin/env bash\necho "airflow_install" >> "$ORDER_LOG"\nexit 0\n'
        )
        write_stub(
            release / "airflow" / "scripts" / "install_postgres.sh",
            '#!/usr/bin/env bash\necho "install_postgres" >> "$ORDER_LOG"\n'
            'mkdir -p "$POSTGRES_ROOT/data"\n: > "$POSTGRES_ROOT/data/PG_VERSION"\nexit 0\n',
        )
        (release / "airflow" / "scripts" / "add_connection.py").write_text("# stub\n", encoding="utf-8")

        (tmp / "releases" / "airflow-venv-gen-1" / "bin").mkdir(parents=True)
        write_stub(tmp / "releases" / "airflow-venv-gen-1" / "bin" / "python", "#!/usr/bin/env bash\nexit 0\n")
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
        (tree / "release.json").write_text(
            json.dumps(
                {
                    "schema_version": "solidworks-to-urdf.release/v1",
                    "pipeline_id": "solidworks-to-urdf",
                    "version": "1.4.0",
                    "source_commit": "c" * 40,
                    "source_sha256": "a" * 64,
                    "artifacts": {wheel.name: hashlib.sha256(wheel.read_bytes()).hexdigest()},
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
            '#!/usr/bin/env bash\necho "systemctl $*" >> "$ORDER_LOG"\n'
            'case "$*" in\n'
            '  "--user daemon-reload") exit 0 ;;\n'
            '  "--user start description-postgres.service") exit "${START_RC:-0}" ;;\n'
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
            "#!/usr/bin/env bash\n" + client_libraries + 'n=$(cat "$PG_READY_COUNT" 2>/dev/null || echo 0)\n'
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
            '  *-f*platform/schema.sql*|*-f*platform/views.sql*|*-f*platform/roles.sql*)\n'
            '    echo "psql_platform_apply" >> "$ORDER_LOG"; exit 0 ;;\n'
            '  *-c*platform_migrate*)\n'
            '    echo "psql_airflow_reader_grant" >> "$ORDER_LOG"; exit 0 ;;\n'
            '  *-v*ON_ERROR_STOP=1*q*)\n'
            '    echo "psql_airflow_grants" >> "$ORDER_LOG"; exit 0 ;;\n'
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
        write_stub(bindir / "sleep", '#!/usr/bin/env bash\necho "sleep" >> "$ORDER_LOG"\nexit 0\n')

        order = tmp / "order.log"
        control_env = {
            "PATH": f"{bindir}:{os.environ.get('PATH', '/usr/bin:/bin')}",
            "XDG_CONFIG_HOME": str(tmp / "config"),
            "ORDER_LOG": str(order),
            "PG_READY_COUNT": str(tmp / "ready-count"),
            "PG_READY_AFTER": str(ready_after),
            "DB_EXISTS": "1" if db_exists else "",
            "PSQL_QUERY_RC": str(psql_query_rc),
            "CREATEDB_RC": str(createdb_rc),
            "START_RC": str(start_rc),
            "PG_CLIENT_LIB": str(tmp / "state" / "airflow-pg" / "root" / "usr" / "lib" / "x86_64-linux-gnu"),
            "LD_LIBRARY_PATH": "/unrelated/vendor",
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
            self.assertLess(lines.index("airflow_install"), lines.index("psql_airflow_reader_grant"))
            self.assertLess(lines.index("psql_airflow_reader_grant"), lines.index("psql_platform_apply"))
            self.assertLess(lines.index("airflow_install"), lines.index("psql_platform_apply"))
            self.assertLess(lines.index("psql_platform_apply"), lines.index("psql_airflow_grants"))
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

    def test_not_ready_postgres_fails_before_migration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            release, control_env, order = self._prepare(Path(tmp), fresh=True, ready_after=0)
            result = self._install(release, control_env)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("PostgreSQL did not become ready", result.stderr)
            lines = order.read_text(encoding="utf-8").splitlines()
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
