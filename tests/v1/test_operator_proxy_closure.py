"""The managed-nginx closure: the installer's actual binary feeds resolved.env, the units and
health - one path, one source - and a missing result refuses instead of rendering a path that
was never installed.

Runs on the standard-library interpreter; the toolchain install leg is exercised through the
real scripts with disposable fixture state (a pre-seeded toolchain binary stands in for the
downloaded package, and a failing ``apt-get`` stub covers the refusal path).
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OPERATOR = ROOT / "deploy" / "operator"
SCRIPTS = OPERATOR / "scripts"
ENSURE = SCRIPTS / "ensure_proxy_binary.sh"
RENDER = OPERATOR / "render_operator.py"
HEALTH = OPERATOR / "health.sh"


def run(cmd: list, env: dict[str, str], cwd: Path = ROOT) -> subprocess.CompletedProcess:
    merged = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(Path.home()),
        "PYTHONPATH": str(ROOT / "src"),
    }
    merged.update(env)
    return subprocess.run(
        [str(item) for item in cmd], capture_output=True, text=True, env=merged, cwd=str(cwd)
    )


def write_executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def base_env(state: Path, **overrides: str) -> dict[str, str]:
    env = {
        "OPERATOR_HOST": "closure.local",
        "OPERATOR_BIND": "127.0.0.1",
        "OPERATOR_HTTPS_PORT": "18443",
        "OPERATOR_STATE": str(state),
        "OPERATOR_UPSTREAM": "127.0.0.1:18788",
        "AIRFLOW_VENV": str(state.parent / "venv"),
        "AIRFLOW_HOME": str(state.parent / "home"),
        "AIRFLOW_DB_URL": "postgresql+psycopg2://solidworks@/airflow_meta?host=/tmp/socket&port=5433",
        "SOLIDWORKS_SSH_HOST": "windows-m3",
        "SOLIDWORKS_HANDOFF_ROOT": str(state.parent / "handoffs"),
    }
    env.update(overrides)
    return env


class EnsureProxyBinaryTests(unittest.TestCase):
    def test_existing_executable_binary_is_used_as_is(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            binary = write_executable(Path(tmp) / "bin" / "nginx")
            result = run(["bash", ENSURE], {"NGINX_BIN": str(binary)})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), str(binary))

    def test_fresh_state_lands_the_toolchain_binary_and_reports_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            state.mkdir()
            binary = write_executable(state / "toolchain" / "nginx" / "root" / "usr" / "sbin" / "nginx")
            result = run(
                ["bash", ENSURE],
                {"OPERATOR_STATE": str(state), "NGINX_BIN": str(Path(tmp) / "absent-nginx")},
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), str(binary))
            self.assertTrue(os.access(binary, os.X_OK))

    def test_install_failure_refuses_with_a_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            state.mkdir()
            stub_bin = Path(tmp) / "bin"
            stub_bin.mkdir()
            apt_get = stub_bin / "apt-get"
            apt_get.write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")
            apt_get.chmod(0o755)
            result = run(
                ["bash", ENSURE],
                {
                    "OPERATOR_STATE": str(state),
                    "NGINX_BIN": str(Path(tmp) / "absent-nginx"),
                    "PATH": f"{stub_bin}:{os.environ.get('PATH', '/usr/bin:/bin')}",
                },
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("ensure_proxy_binary", result.stderr)


class RenderClosureTests(unittest.TestCase):
    def _env_file(self, tmp: Path, **overrides: str) -> Path:
        state = Path(tmp) / "state"
        env = base_env(state, **overrides)
        env_file = Path(tmp) / "operator.env"
        env_file.write_text(
            "".join(f"{key}={value}\n" for key, value in env.items()), encoding="utf-8"
        )
        return env_file

    def test_render_writes_the_decided_binary_into_env_and_units(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            binary = write_executable(Path(tmp) / "toolchain" / "nginx")
            wheel = Path(tmp) / "package.whl"
            wheel.write_bytes(b"wheel")
            env_file = self._env_file(
                Path(tmp), PIPELINE_WHEEL=str(wheel), NGINX_BIN=str(Path(tmp) / "absent-nginx")
            )
            units = Path(tmp) / "units"
            result = run(
                [
                    sys.executable,
                    RENDER,
                    "--env-file",
                    env_file,
                    "--nginx-bin",
                    str(binary),
                    "--check-paths",
                    "--units-dir",
                    units,
                ],
                {},
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            resolved = (Path(tmp) / "state" / "resolved.env").read_text(encoding="utf-8")
            self.assertIn(f"NGINX_BIN={binary}\n", resolved)
            proxy = (units / "description-operator-proxy.service").read_text(encoding="utf-8")
            self.assertIn(str(binary), proxy)
            health = HEALTH.read_text(encoding="utf-8")
            self.assertIn("managed nginx binary missing or not executable", health)

    def test_missing_binary_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            wheel = Path(tmp) / "package.whl"
            wheel.write_bytes(b"wheel")
            env_file = self._env_file(
                Path(tmp), PIPELINE_WHEEL=str(wheel), NGINX_BIN=str(Path(tmp) / "absent-nginx")
            )
            result = run(
                [
                    sys.executable,
                    RENDER,
                    "--env-file",
                    env_file,
                    "--nginx-bin",
                    str(Path(tmp) / "absent-nginx"),
                    "--check-paths",
                ],
                {},
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("NGINX_BIN", result.stderr)


if __name__ == "__main__":
    unittest.main()
