"""Operator deployment renderer/lifecycle tests (stdlib only; no services are started)."""

from __future__ import annotations

import hashlib
import os
import re
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

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
    merged = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(Path.home())}
    merged.update(env)
    return subprocess.run(cmd, capture_output=True, text=True, env=merged, cwd=str(cwd))


def render(state: Path, **overrides: str) -> subprocess.CompletedProcess:
    env = base_env(state, **overrides)
    env_file = state.parent / "operator.env"
    env_file.parent.mkdir(parents=True, exist_ok=True)
    env_file.write_text("".join(f"{key}={value}\n" for key, value in env.items()), encoding="utf-8")
    return run([sys.executable, str(RENDER), "--env-file", str(env_file)], env)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class RenderTests(unittest.TestCase):
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
            san = run(["openssl", "x509", "-in", str(state / "secrets/tls.crt"),
                       "-noout", "-ext", "subjectAltName"], {})
            self.assertEqual(san.returncode, 0, san.stderr)
            self.assertIn("DNS:rehearsal.local", san.stdout)

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
            env_file.write_text("".join(f"{key}={value}\n" for key, value in without_tunnel.items()),
                                encoding="utf-8")
            missing_tunnel = run([sys.executable, str(RENDER), "--env-file", str(env_file)], {})
            self.assertNotEqual(missing_tunnel.returncode, 0)
            self.assertIn("SOLIDWORKS_SSH_HOST", missing_tunnel.stderr)
            for broad in ("/", "/home", str(Path.home()), str(state)):
                rejected = render(state, SOLIDWORKS_HANDOFF_ROOT=broad)
                self.assertNotEqual(rejected.returncode, 0, broad)

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

    def test_check_paths_reports_missing_runtime_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            env = base_env(state)
            env_file = Path(tmp) / "operator.env"
            env_file.write_text("".join(f"{k}={v}\n" for k, v in env.items()), encoding="utf-8")
            missing = run([sys.executable, str(RENDER), "--env-file", str(env_file), "--check-paths"],
                          {"NGINX_BIN": str(Path(tmp) / "nginx")})
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
            # c9's portal refuses unknown keys, so the bootstrap config is exactly three sections.
            import json

            parsed = json.loads(portal)
            self.assertEqual(sorted(parsed), ["airflow", "endpoint", "portal"])
            self.assertEqual(sorted(parsed["airflow"]), ["url"])
            self.assertEqual(sorted(parsed["endpoint"]), ["token_file", "url"])
            self.assertEqual(sorted(parsed["portal"]), ["host", "port"])
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
            drift_file.write_text("".join(f"{key}={value}\n" for key, value in drifted_env.items()),
                                  encoding="utf-8")
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
            result = render(state, FEISHU_APP_SECRET_FILE=str(secret),
                            FEISHU_TENANT_KEYS="tenant_a,tenant_b", FEISHU_ADMIN_OPEN_IDS="ou_admin")
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
            self.assertEqual(render(state, FEISHU_APP_SECRET_FILE=str(secret),
                                    FEISHU_TENANT_KEYS="tenant_a").returncode, 0)
            self.assertNotIn("FEISHU_ADMIN_OPEN_IDS", (state / "feishu.env").read_text(encoding="utf-8"))
            self.assertEqual(render(state).returncode, 0)
            text = (state / "feishu.env").read_text(encoding="utf-8")
            self.assertIn("FEISHU_REDIRECT_URI=", text)
            self.assertNotIn("FEISHU_APP_SECRET_FILE", text)
            self.assertNotIn("FEISHU_TENANT_KEYS", text)

    def test_rejects_bad_feishu_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            missing = render(state, FEISHU_APP_SECRET_FILE=str(Path(tmp) / "nope.json"),
                             FEISHU_TENANT_KEYS="tenant_a")
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
            self.assertEqual(render(state, FEISHU_APP_SECRET_FILE=str(secret),
                                    FEISHU_TENANT_KEYS="tenant_a").returncode, 0)
            env_file = state.parent / "operator.env"
            clean = run([sys.executable, str(RENDER), "--env-file", str(env_file), "--dry-run"], {})
            self.assertEqual(clean.returncode, 0, clean.stdout + clean.stderr)
            (state / "feishu.env").unlink()
            missing = run([sys.executable, str(RENDER), "--env-file", str(env_file), "--dry-run"], {})
            self.assertEqual(missing.returncode, 1)
            self.assertIn("feishu.env", missing.stdout)


class LifecycleTests(unittest.TestCase):
    def _render_units(self, tmp: Path) -> tuple[Path, Path, Path]:
        state = tmp / "state"
        env_file = tmp / "operator.env"
        env = base_env(state, POSTGRES_ROOT=str(tmp / "pg"), SOLIDWORKS_SSH_HOST="windows-m3")
        env_file.write_text("".join(f"{key}={value}\n" for key, value in env.items()), encoding="utf-8")
        config_home = tmp / "config"
        target = config_home / "systemd" / "user"
        target.mkdir(parents=True)
        rendered = run([sys.executable, str(RENDER), "--env-file", str(env_file), "--units-dir", str(target)],
                       {"XDG_CONFIG_HOME": str(config_home)})
        self.assertEqual(rendered.returncode, 0, rendered.stderr)
        airflow = run(["bash", str(OPERATOR.parent / "airflow" / "services.sh"), "render"],
                      {"XDG_CONFIG_HOME": str(config_home), "AIRFLOW_VENV": env["AIRFLOW_VENV"],
                       "AIRFLOW_HOME": env["AIRFLOW_HOME"], "POSTGRES_ROOT": env["POSTGRES_ROOT"],
                       "POSTGRES_MAJOR": "14", "SOLIDWORKS_SSH_HOST": "windows-m3",
                       "FEISHU_ENV_FILE": str(state / "feishu.env")})
        self.assertEqual(airflow.returncode, 0, airflow.stderr)
        return state, config_home, target

    def test_units_render_and_static_health_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state, config_home, target = self._render_units(Path(tmp))
            (target / "unrelated.service").write_text("[Unit]\nDescription=keep\n", encoding="utf-8")
            token_file = state / "secrets" / "endpoint.token"
            token_file.parent.mkdir(parents=True, exist_ok=True)
            token_file.write_text("test-token\n", encoding="utf-8")
            token_file.chmod(0o600)
            names = sorted(path.name for path in target.glob("*.service"))
            self.assertEqual(names, [
                "description-airflow-api-server.service",
                "description-airflow-dag-processor.service",
                "description-airflow-scheduler.service",
                "description-operator-proxy.service",
                "description-portal.service",
                "description-postgres.service",
                "description-solidworks-tunnel.service",
                "unrelated.service",
            ])
            self.assertEqual((target / "unrelated.service").read_text(encoding="utf-8"),
                             "[Unit]\nDescription=keep\n")
            portal = (target / "description-portal.service").read_text(encoding="utf-8")
            self.assertIn("description_pipeline.orchestration.portal", portal)
            proxy = (target / "description-operator-proxy.service").read_text(encoding="utf-8")
            self.assertIn(" -t -c ", proxy)
            health = run(["bash", str(HEALTH), "--static"],
                         {"OPERATOR_STATE": str(state), "XDG_CONFIG_HOME": str(config_home)})
            self.assertEqual(health.returncode, 0, health.stdout + health.stderr)
            self.assertIn("STATIC OK", health.stdout)

    def test_operatorctl_surface_and_env_file(self) -> None:
        missing_target = run(["bash", str(CONTROL), "install"], {})
        self.assertEqual(missing_target.returncode, 2)
        self.assertIn("usage:", missing_target.stderr)
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / "operator.env"
            env_file.write_text("OPERATOR_HOST=rehearsal.local\n", encoding="utf-8")
            removed = run(["bash", str(CONTROL), "rehearse", "--env-file", str(env_file)], {})
            self.assertEqual(removed.returncode, 2)
            absent = run(["bash", str(CONTROL), "install", "--env-file", str(Path(tmp) / "nope.env")], {})
            self.assertEqual(absent.returncode, 1)
            self.assertIn("env file not found", absent.stderr)

    def test_status_is_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state, config_home, target = self._render_units(Path(tmp))
            env_file = Path(tmp) / "operator.env"
            tracked = (state / "resolved.env", state / "portal.json", state / "nginx/nginx.conf",
                       target / "description-portal.service")
            before = {path: digest(path) for path in tracked}
            result = run(["bash", str(CONTROL), "status", "--env-file", str(env_file)],
                         {"XDG_CONFIG_HOME": str(config_home)})
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertNotIn("DRIFT", result.stdout)
            self.assertEqual(before, {path: digest(path) for path in before})

    def test_env_example_and_toolchain_are_current(self) -> None:
        example = (OPERATOR / "operator.env.example").read_text(encoding="utf-8")
        self.assertIn("--env-file", example)
        for stale in ("install-base", "install-proxy", "rehearse", "--refresh", "c9", "root provides"):
            self.assertNotIn(stale, example)
        toolchain = (OPERATOR / "scripts" / "install_toolchain.sh").read_text(encoding="utf-8")
        self.assertIn("UV_VERSION=0.12.23", toolchain)
        self.assertIn("PYTHON_VERSION=3.12.14", toolchain)
        for knob in ("${UV_VERSION:-", "${PYTHON_VERSION:-"):
            self.assertNotIn(knob, toolchain)

    def test_health_probes_endpoint_auth_and_connection(self) -> None:
        health = HEALTH.read_text(encoding="utf-8")
        self.assertIn("http://127.0.0.1:18765/health", health)
        self.assertIn("Authorization", health)
        self.assertIn("handoff_roots", health)
        self.assertIn("solidworks_windows", health)
        self.assertIn("/auth/feishu/health", health)
        self.assertIn("FEISHU_ENV_FILE", health)
        self.assertIn("FEISHU_APP_SECRET_FILE", health)
        self.assertIn('if status == 200 and body.get("configured") is True', health)

    def test_api_server_unit_loads_the_optional_feishu_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state, _, target = self._render_units(Path(tmp))
            unit = (target / "description-airflow-api-server.service").read_text(encoding="utf-8")
            self.assertIn(f"EnvironmentFile=-{state}/feishu.env", unit)
            self.assertNotIn("@", unit)
            scheduler = (target / "description-airflow-scheduler.service").read_text(encoding="utf-8")
            self.assertNotIn("EnvironmentFile", scheduler)
            control = CONTROL.read_text(encoding="utf-8")
            self.assertIn('FEISHU_ENV_FILE="${FEISHU_ENV_FILE:-$AIRFLOW_HOME/feishu.env}"', control)

    def test_shipped_example_path_relationships_validate(self) -> None:
        example = (OPERATOR / "operator.env.example").read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as tmp:
            prefix = Path(tmp) / "home" / "andy"
            rewritten = "\n".join(
                re.sub(r"^([A-Z0-9_]+)=/home/andy(.*)$", rf"\1={prefix}\2", line)
                for line in example.splitlines())
            env_file = Path(tmp) / "example.env"
            env_file.write_text(rewritten + "\n", encoding="utf-8")
            result = run([sys.executable, str(RENDER), "--env-file", str(env_file)], {})
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            resolved = (prefix / "operator/state/resolved.env").read_text(encoding="utf-8")
            self.assertIn(f"SOLIDWORKS_HANDOFF_ROOT={prefix}/cad-handoffs", resolved)
            # The shipped intake must stay outside the managed state/runtime tree.
            self.assertNotIn(f"SOLIDWORKS_HANDOFF_ROOT={prefix}/operator/", resolved)

    def test_scripts_are_syntactically_valid(self) -> None:
        for script in (CONTROL, HEALTH, OPERATOR / "scripts" / "install_toolchain.sh",
                       OPERATOR / "scripts" / "install_proxy.sh"):
            result = run(["bash", "-n", str(script)], {})
            self.assertEqual(result.returncode, 0, f"{script}: {result.stderr}")
        compile(RENDER.read_text(encoding="utf-8"), str(RENDER), "exec")


if __name__ == "__main__":
    unittest.main()
