"""Operator deployment renderer/lifecycle tests (stdlib only; external services are mocked)."""

from __future__ import annotations

import hashlib
import json
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


def write_stub(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


class RenderTests(unittest.TestCase):
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
            self.assertEqual(parsed["pipeline"], {"store_root": str(state / "runs"), "repositories": {}})
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
    def _render_units(self, tmp: Path) -> tuple[Path, Path, Path]:
        state = tmp / "state"
        env_file = tmp / "operator.env"
        secret = tmp / "feishu_app.json"
        secret.write_text('{"app_id": "cli_test", "app_secret": "s"}\n', encoding="utf-8")
        secret.chmod(0o600)
        env = base_env(
            state,
            POSTGRES_ROOT=str(tmp / "pg"),
            SOLIDWORKS_SSH_HOST="windows-m3",
            FEISHU_APP_SECRET_FILE=str(secret),
            FEISHU_TENANT_KEYS="tenant_a",
        )
        env_file.write_text("".join(f"{key}={value}\n" for key, value in env.items()), encoding="utf-8")
        config_home = tmp / "config"
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
                "SOLIDWORKS_SSH_HOST": "windows-m3",
                "FEISHU_ENV_FILE": str(state / "feishu.env"),
            },
        )
        self.assertEqual(airflow.returncode, 0, airflow.stderr)
        return state, config_home, target

    def _admission_fixture(self, tmp: Path, *, register_after: int = 0, unpause_rc: int = 0, pause_rc: int = 0):
        _, config_home, _ = self._render_units(tmp)
        bindir = tmp / "bin"
        bindir.mkdir()
        write_stub(bindir / "systemctl", '#!/bin/bash\necho "systemctl $*" >> "$ORDER_LOG"\n')
        write_stub(bindir / "sleep", "#!/bin/bash\nexit 0\n")
        venv = tmp / "venv/bin"
        venv.mkdir(parents=True)
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
            '#!/bin/bash\necho "airflow admission check" >> "$ORDER_LOG"\n'
            'count=$(cat "$REGISTER_COUNT" 2>/dev/null || echo 0)\n'
            'if [ "$count" -lt "$REGISTER_AFTER" ]; then\n'
            '  echo $((count + 1)) > "$REGISTER_COUNT"; exit 1\n'
            'fi\n[ "$(cat "$ADMISSION")" = "false" ]\n',
        )
        admission, order = tmp / "admission", tmp / "order.log"
        admission.write_text("true")
        env = {
            "PATH": f"{bindir}:{os.environ.get('PATH', '/usr/bin:/bin')}",
            "XDG_CONFIG_HOME": str(config_home),
            "ORDER_LOG": str(order),
            "ADMISSION": str(admission),
            "REGISTER_COUNT": str(tmp / "register-count"),
            "REGISTER_AFTER": str(register_after),
            "UNPAUSE_RC": str(unpause_rc),
            "PAUSE_RC": str(pause_rc),
        }
        return tmp / "operator.env", env, admission, order

    def test_start_opens_verified_admission_before_ingress(self) -> None:
        for register_after in (0, 1):
            with self.subTest(register_after=register_after), tempfile.TemporaryDirectory() as tmp:
                env_file, env, admission, order = self._admission_fixture(Path(tmp), register_after=register_after)
                result = run(["bash", str(CONTROL), "start", "--env-file", str(env_file)], env)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(admission.read_text().strip(), "false")
                lines = order.read_text().splitlines()
                ingress = lines.index(
                    "systemctl --user enable --now description-portal.service description-operator-proxy.service"
                )
                self.assertLess(max(i for i, line in enumerate(lines) if line == "airflow admission check"), ingress)
                self.assertEqual(lines.count("airflow dags unpause solidworks_to_urdf"), register_after + 1)

    def test_unpause_failure_does_not_start_ingress(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_file, env, admission, order = self._admission_fixture(Path(tmp), unpause_rc=7)
            result = run(["bash", str(CONTROL), "start", "--env-file", str(env_file)], env)
            self.assertEqual(result.returncode, 7, result.stdout + result.stderr)
            self.assertEqual(admission.read_text(), "true")
            self.assertNotIn("description-portal.service", order.read_text())
            self.assertNotIn("description-operator-proxy.service", order.read_text())

    def test_stop_closes_ingress_and_stops_services_even_when_pause_fails(self) -> None:
        for pause_rc in (0, 9):
            with self.subTest(pause_rc=pause_rc), tempfile.TemporaryDirectory() as tmp:
                env_file, env, admission, order = self._admission_fixture(Path(tmp), pause_rc=pause_rc)
                admission.write_text("false")
                result = run(["bash", str(CONTROL), "stop", "--env-file", str(env_file)], env)
                self.assertEqual(result.returncode, pause_rc, result.stdout + result.stderr)
                self.assertEqual(admission.read_text().strip(), "true" if pause_rc == 0 else "false")
                lines = order.read_text().splitlines()
                self.assertEqual(
                    lines[0],
                    "systemctl --user disable --now description-operator-proxy.service description-portal.service",
                )
                pause = lines.index("airflow dags pause solidworks_to_urdf")
                self.assertLess(pause, lines.index("systemctl --user disable --now description-airflow-scheduler"))

    def test_units_render_and_static_health_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state, config_home, target = self._render_units(Path(tmp))
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
            tracked = (
                state / "resolved.env",
                state / "portal.json",
                state / "nginx/nginx.conf",
                target / "description-portal.service",
            )
            before = {path: digest(path) for path in tracked}
            result = run(
                ["bash", str(CONTROL), "status", "--env-file", str(env_file)], {"XDG_CONFIG_HOME": str(config_home)}
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertNotIn("DRIFT", result.stdout)
            self.assertEqual(before, {path: digest(path) for path in before})

    def test_env_example_and_toolchain_are_current(self) -> None:
        example = (OPERATOR / "operator.env.example").read_text(encoding="utf-8")
        self.assertIn("--env-file", example)
        for stale in ("install-base", "install-proxy", "rehearse", "--refresh", "c9", "root provides"):
            self.assertNotIn(stale, example)
        self.assertIn("mimicverse_description-1.0.1-py3-none-any.whl", example)
        self.assertNotIn("mimicverse_description-1.0.0-py3-none-any.whl", example)
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
            state, config_home, target = self._render_units(Path(tmp))
            env_file = Path(tmp) / "operator.env"
            control_env = {"XDG_CONFIG_HOME": str(config_home), "OPERATOR_STATE": str(state)}
            portal = target / "description-portal.service"
            portal.write_text(portal.read_text(encoding="utf-8") + "# drifted\n", encoding="utf-8")
            changed = run(["bash", str(CONTROL), "status", "--env-file", str(env_file)], control_env)
            self.assertEqual(changed.returncode, 0, changed.stdout + changed.stderr)
            self.assertIn("DRIFT unit differs: description-portal.service", changed.stdout)
            (target / "description-airflow-scheduler.service").unlink()
            missing = run(["bash", str(CONTROL), "status", "--env-file", str(env_file)], control_env)
            self.assertIn("DRIFT unit missing: description-airflow-scheduler.service", missing.stdout)
            self.assertIn("DRIFT unit differs: description-portal.service", missing.stdout)

    def test_drift_check_reports_render_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state, config_home, _ = self._render_units(Path(tmp))
            env_file = Path(tmp) / "operator.env"
            env_file.write_text(
                env_file.read_text(encoding="utf-8")
                + f"OPERATOR_TLS_CERT={Path(tmp) / 'missing.crt'}\n"
                + f"OPERATOR_TLS_KEY={Path(tmp) / 'missing.key'}\n",
                encoding="utf-8",
            )
            failed = run(
                ["bash", str(CONTROL), "status", "--env-file", str(env_file)],
                {"XDG_CONFIG_HOME": str(config_home), "OPERATOR_STATE": str(state)},
            )
            self.assertIn("DRIFT unit missing from render: description-portal.service", failed.stdout)
            self.assertIn("DRIFT unit missing from render: description-operator-proxy.service", failed.stdout)
            self.assertIn("provided TLS certificate or key is missing", failed.stderr)

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
        values = dict(line.split("=", 1) for line in example.splitlines() if line and not line.startswith("#"))
        original_prefix = Path(values["OPERATOR_STATE"]).parent
        with tempfile.TemporaryDirectory() as tmp:
            prefix = Path(tmp) / "deployment"
            rewritten = example.replace(str(original_prefix), str(prefix))
            env_file = Path(tmp) / "example.env"
            env_file.write_text(rewritten + "\n", encoding="utf-8")
            result = run([sys.executable, str(RENDER), "--env-file", str(env_file)], {})
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            resolved = (prefix / "state/resolved.env").read_text(encoding="utf-8")
            self.assertIn(f"SOLIDWORKS_HANDOFF_ROOT={prefix}/cad-handoffs", resolved)
            # The shipped intake must stay outside the managed state/runtime tree.
            resolved_values = dict(line.split("=", 1) for line in resolved.splitlines() if line)
            intake = Path(resolved_values["SOLIDWORKS_HANDOFF_ROOT"])
            for key in ("OPERATOR_STATE", "AIRFLOW_VENV", "AIRFLOW_HOME", "POSTGRES_ROOT"):
                runtime = Path(resolved_values[key])
                self.assertFalse(intake.is_relative_to(runtime) or runtime.is_relative_to(intake))

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
        release = tmp / "release"
        (release / "operator" / "scripts").mkdir(parents=True)
        (release / "airflow" / "scripts").mkdir(parents=True)
        (release / "operator" / "operatorctl.sh").write_bytes(CONTROL.read_bytes())
        (release / "operator" / "render_operator.py").write_bytes(RENDER.read_bytes())
        for template in ("nginx.conf.template", "portal.json.template"):
            (release / "operator" / template).write_bytes((OPERATOR / template).read_bytes())
        (release / "operator" / "systemd").mkdir()
        for unit in (OPERATOR / "systemd").glob("*.service"):
            (release / "operator" / "systemd" / unit.name).write_bytes(unit.read_bytes())
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

        (tmp / "venv" / "bin").mkdir(parents=True)
        write_stub(tmp / "venv" / "bin" / "python", "#!/usr/bin/env bash\nexit 0\n")
        (tmp / "token").write_text("test-token\n", encoding="utf-8")
        (tmp / "token").chmod(0o600)
        if not fresh:
            (tmp / "pg" / "data").mkdir(parents=True)
            (tmp / "pg" / "data" / "PG_VERSION").write_text("14\n", encoding="utf-8")

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
        pgbin = tmp / "pg" / "root" / "usr" / "lib" / "postgresql" / "14" / "bin"
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
            "#!/usr/bin/env bash\n" + client_libraries + 'echo "psql_db_query" >> "$ORDER_LOG"\n'
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

        state = tmp / "state"
        env = base_env(
            state,
            POSTGRES_ROOT=str(tmp / "pg"),
            PIPELINE_WHEEL=str(tmp / "wheel.whl"),
            ENDPOINT_TOKEN_FILE=str(tmp / "token"),
            SOLIDWORKS_HANDOFF_ROOT=str(tmp / "handoffs"),
        )
        env_file = tmp / "operator.env"
        env_file.write_text("".join(f"{key}={value}\n" for key, value in env.items()), encoding="utf-8")
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
            "PG_CLIENT_LIB": str(tmp / "pg" / "root" / "usr" / "lib" / "x86_64-linux-gnu"),
            "LD_LIBRARY_PATH": "/unrelated/vendor",
        }
        return release, env_file, control_env, order

    def _install(self, release: Path, env_file: Path, control_env: dict[str, str]):
        return run(
            ["bash", str(release / "operator" / "operatorctl.sh"), "install", "--env-file", str(env_file)], control_env
        )

    def test_fresh_install_creates_then_uses_the_managed_unit_before_migration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            release, env_file, control_env, order = self._prepare(Path(tmp), fresh=True, ready_after=2)
            result = self._install(release, env_file, control_env)
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
            self.assertLess(lines.index("createdb"), lines.index("airflow_install"))
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

    def test_stopped_reinstall_starts_the_managed_unit_before_migration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            release, env_file, control_env, order = self._prepare(Path(tmp), fresh=False, ready_after=1, db_exists=True)
            result = self._install(release, env_file, control_env)
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
            release, env_file, control_env, order = self._prepare(Path(tmp), fresh=True, ready_after=0)
            result = self._install(release, env_file, control_env)
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
            release, env_file, control_env, order = self._prepare(Path(tmp), fresh=False, ready_after=1, start_rc=1)
            result = self._install(release, env_file, control_env)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("description-postgres.service failed to start", result.stderr)
            lines = order.read_text(encoding="utf-8").splitlines()
            self.assertNotIn("pg_isready 1", lines)
            self.assertNotIn("psql_db_query", lines)
            self.assertNotIn("createdb", lines)
            self.assertNotIn("airflow_install", lines)

    def test_database_query_failure_exits_before_migration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            release, env_file, control_env, order = self._prepare(
                Path(tmp), fresh=False, ready_after=1, psql_query_rc=1
            )
            result = self._install(release, env_file, control_env)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("could not query pg_database", result.stderr)
            lines = order.read_text(encoding="utf-8").splitlines()
            self.assertNotIn("createdb", lines)
            self.assertNotIn("airflow_install", lines)

    def test_database_creation_failure_exits_before_migration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            release, env_file, control_env, order = self._prepare(
                Path(tmp), fresh=True, ready_after=1, db_exists=False, createdb_rc=1
            )
            result = self._install(release, env_file, control_env)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("could not create the airflow_meta database", result.stderr)
            lines = order.read_text(encoding="utf-8").splitlines()
            self.assertIn("createdb", lines)
            self.assertNotIn("airflow_install", lines)


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
            self.assertIn("AIRFLOW_DB_URL=postgresql+psycopg2://solidworks@/airflow_meta", result.stdout)
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


if __name__ == "__main__":
    unittest.main()
