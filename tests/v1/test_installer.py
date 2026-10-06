"""Deployment-installer guards: atomic config, service units, path validation, pinned lock.

Runs on the standard-library interpreter (no Airflow import) so the checks work in the shared
Linux test environment as well as inside the deployment venv.
"""

from __future__ import annotations

import ast
import os
import re
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "deploy" / "airflow"
DSN = "postgresql+psycopg2://solidworks@/airflow_meta?host=/srv/socket&port=5433"


def run(cmd: list[str], env: dict[str, str], cwd: Path = ROOT) -> subprocess.CompletedProcess:
    merged = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(Path.home())}
    merged.update(env)
    return subprocess.run(cmd, capture_output=True, text=True, env=merged, cwd=str(cwd))


class RenderConfigTest(unittest.TestCase):
    def _render(self, home: Path, venv: Path, dsn: str = DSN) -> subprocess.CompletedProcess:
        return run(
            [
                sys.executable,
                str(DEPLOY / "render_config.py"),
                "--home",
                str(home),
                "--venv",
                str(venv),
                "--template",
                str(DEPLOY / "airflow.cfg.template"),
                "--dags-folder",
                str(DEPLOY / "dags"),
            ],
            {"AIRFLOW_DB_URL": dsn},
        )

    def test_renders_private_config_and_preserves_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home, venv = Path(tmp) / "home", Path(tmp) / "venv"
            venv.mkdir()
            first = self._render(home, venv)
            self.assertEqual(first.returncode, 0, first.stderr)
            cfg = home / "airflow.cfg"
            mode = stat.S_IMODE(cfg.stat().st_mode)
            self.assertEqual(mode, 0o600)
            text = cfg.read_text(encoding="utf-8")
            self.assertIn("# managed-by: description-airflow", text)
            self.assertIn(f"sql_alchemy_conn = {DSN}", text)
            self.assertIn(f"dags_folder = {DEPLOY / 'dags'}", text)
            self.assertIn("simple_auth_manager_users = operator:admin", text)
            first_keys = re.findall(r"^(?:fernet_key|jwt_secret) = (\S+)$", text, re.M)
            self.assertEqual(len(first_keys), 2)
            # Rerun with a different socket: secrets must survive byte-for-byte.
            moved = DSN.replace("/srv/socket", "/srv/socket2")
            second = self._render(home, venv, moved)
            self.assertEqual(second.returncode, 0, second.stderr)
            again = cfg.read_text(encoding="utf-8")
            self.assertEqual(re.findall(r"^(?:fernet_key|jwt_secret) = (\S+)$", again, re.M), first_keys)
            self.assertIn(f"sql_alchemy_conn = {moved}", again)
            self.assertEqual(sorted(p.name for p in home.iterdir()), ["airflow.cfg"])
            self.assertEqual(stat.S_IMODE(cfg.stat().st_mode), 0o600)

    def test_rejects_unmanaged_existing_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home, venv = Path(tmp) / "home", Path(tmp) / "venv"
            home.mkdir()
            venv.mkdir()
            (home / "airflow.cfg").write_text("[core]\nfernet_key = keep\n", encoding="utf-8")
            result = self._render(home, venv)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("not managed", result.stderr)
            self.assertEqual((home / "airflow.cfg").read_text(encoding="utf-8"), "[core]\nfernet_key = keep\n")

    def test_rejects_relative_shared_and_identical_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            venv = Path(tmp) / "venv"
            venv.mkdir()
            relative = self._render(Path("relative"), venv)
            self.assertNotEqual(relative.returncode, 0)
            self.assertIn("must be an absolute path", relative.stderr)
            shared = self._render(Path("/"), venv)
            self.assertNotEqual(shared.returncode, 0)
            self.assertIn("refuses shared/root paths", shared.stderr)
            same = self._render(venv, venv)
            self.assertNotEqual(same.returncode, 0)
            self.assertIn("distinct", same.stderr)

    def test_rejects_non_postgres_dsn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = self._render(Path(tmp) / "home", Path(tmp) / "venv", "sqlite:////tmp/x.db")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("PostgreSQL", result.stderr)


class ServicesRenderTest(unittest.TestCase):
    def test_render_writes_only_description_units(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_home = Path(tmp) / "config"
            target = config_home / "systemd" / "user"
            target.mkdir(parents=True)
            (target / "unrelated.service").write_text("[Unit]\nDescription=keep me\n", encoding="utf-8")
            result = run(
                ["bash", str(DEPLOY / "services.sh"), "render"],
                {
                    "XDG_CONFIG_HOME": str(config_home),
                    "AIRFLOW_VENV": str(Path(tmp) / "venv"),
                    "AIRFLOW_HOME": str(Path(tmp) / "home"),
                    "POSTGRES_ROOT": str(Path(tmp) / "pg"),
                },
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            names = sorted(path.name for path in target.glob("*.service"))
            self.assertEqual(
                names,
                [
                    "description-airflow-api-server.service",
                    "description-airflow-dag-processor.service",
                    "description-airflow-scheduler.service",
                    "description-postgres.service",
                    "unrelated.service",
                ],
            )
            postgres = (target / "description-postgres.service").read_text(encoding="utf-8")
            self.assertIn(f"-D {Path(tmp) / 'pg'}/data", postgres)
            self.assertIn("listen_addresses=", postgres)
            self.assertNotIn("127.0.0.1", postgres)
            self.assertIn("UMask=0077", postgres)
            scheduler = (target / "description-airflow-scheduler.service").read_text(encoding="utf-8")
            self.assertIn(f"Environment=AIRFLOW_HOME={Path(tmp) / 'home'}", scheduler)
            self.assertIn("UMask=0077", scheduler)
            self.assertNotIn("@", scheduler)
            api_server = (target / "description-airflow-api-server.service").read_text(encoding="utf-8")
            self.assertIn("api-server --host 127.0.0.1 --port 8791", api_server)
            untouched = (target / "unrelated.service").read_text(encoding="utf-8")
            self.assertEqual(untouched, "[Unit]\nDescription=keep me\n")

    def test_render_without_postgres_root_skips_postgres_unit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_home = Path(tmp) / "config"
            result = run(
                ["bash", str(DEPLOY / "services.sh"), "render"],
                {
                    "XDG_CONFIG_HOME": str(config_home),
                    "AIRFLOW_VENV": str(Path(tmp) / "venv"),
                    "AIRFLOW_HOME": str(Path(tmp) / "home"),
                },
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            names = sorted(path.name for path in (config_home / "systemd" / "user").glob("*.service"))
            self.assertEqual(len(names), 3)
            self.assertNotIn("description-postgres.service", names)


class InstallGuardsTest(unittest.TestCase):
    def _install(self, tmp: str, **env: str) -> subprocess.CompletedProcess:
        base = {
            "AIRFLOW_VENV": f"{tmp}/venv",
            "AIRFLOW_HOME": f"{tmp}/home",
            "PIPELINE_WHEEL": f"{tmp}/wheels/mimicverse_description-0.3.25-py3-none-any.whl",
            "AIRFLOW_DB_URL": DSN,
        }
        base.update(env)
        return run(["bash", str(DEPLOY / "install.sh")], base)

    def test_rejects_relative_forbidden_and_missing_wheel(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            relative = self._install(tmp, AIRFLOW_VENV="relative/venv")
            self.assertNotEqual(relative.returncode, 0)
            self.assertIn("absolute path", relative.stderr)
            forbidden = self._install(tmp, AIRFLOW_HOME="/opt")
            self.assertNotEqual(forbidden.returncode, 0)
            self.assertIn("shared/root", forbidden.stderr)
            same = self._install(tmp, AIRFLOW_HOME=f"{tmp}/venv")
            self.assertNotEqual(same.returncode, 0)
            self.assertIn("distinct", same.stderr)
            missing = self._install(tmp)
            self.assertNotEqual(missing.returncode, 0)
            self.assertIn("does not exist", missing.stderr)

    def test_rejects_foreign_wheel_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            wheels = Path(tmp) / "wheels"
            wheels.mkdir()
            foreign = wheels / "description_pipeline-0.3.25-py3-none-any.whl"
            foreign.write_bytes(b"x")
            result = self._install(tmp, PIPELINE_WHEEL=str(foreign))
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("mimicverse_description", result.stderr)
            self.assertFalse(Path(f"{tmp}/home").exists())

    def test_rejects_non_python312_interpreter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            wheels = Path(tmp) / "wheels"
            wheels.mkdir()
            (wheels / "mimicverse_description-0.3.25-py3-none-any.whl").write_bytes(b"x")
            result = self._install(tmp, AIRFLOW_PYTHON="/bin/false")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Python 3.12", result.stderr)


class LockAndScriptsTest(unittest.TestCase):
    def test_lock_is_fully_hash_pinned(self) -> None:
        entries = [
            line
            for line in (DEPLOY / "requirements.lock").read_text(encoding="utf-8").splitlines()
            if line and not line.startswith("#")
        ]
        self.assertGreaterEqual(len(entries), 100)
        for line in entries:
            self.assertRegex(line, r"^[A-Za-z0-9][A-Za-z0-9._-]*==[^\s]+ --hash=sha256:[0-9a-f]{64}  # \S+\.whl$")
        joined = "\n".join(entries)
        for required in (
            "apache-airflow==3.3.2",
            "apache-airflow-task-sdk==1.3.2",
            "asyncpg==0.31.0",
            "psycopg2-binary==2.9.13",
            "mujoco==3.13.0",
            "numpy==2.5.3",
        ):
            self.assertIn(required, joined)
        self.assertNotIn("mimicverse-description==", joined)
        installer = (DEPLOY / "install.sh").read_text(encoding="utf-8")
        self.assertIn("--require-hashes --only-binary=:all:", installer)
        self.assertIn("-m pip check", installer)
        self.assertNotIn("pip install --upgrade", installer)
        self.assertNotIn("sed -e", installer)

    def test_postgres_installer_is_socket_only(self) -> None:
        script = (DEPLOY / "scripts" / "install_postgres.sh").read_text(encoding="utf-8")
        self.assertIn("--auth-host=reject", script)
        self.assertIn("listen_addresses = ''", script)
        self.assertIn('chmod 700 "$WORK/data" "$SOCKET"', script)
        self.assertNotIn("--auth=trust", script)
        self.assertNotIn("-h 127.0.0.1", script)

    def test_add_connection_defaults_match_dag(self) -> None:
        source = DEPLOY / "scripts" / "add_connection.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        defaults: dict[str, object] = {}
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "add_argument"
                and node.args
                and isinstance(node.args[0], ast.Constant)
            ):
                for keyword in node.keywords:
                    if keyword.arg == "default":
                        defaults[node.args[0].value] = ast.literal_eval(keyword.value)
        self.assertEqual(defaults.get("--conn-id"), "solidworks_windows")
        dag = (DEPLOY / "dags" / "solidworks_to_urdf.py").read_text(encoding="utf-8")
        self.assertIn('"conn_id": Param("solidworks_windows"', dag)


if __name__ == "__main__":
    unittest.main()
