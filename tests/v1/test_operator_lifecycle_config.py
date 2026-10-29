"""First-install operator CLI surface tests."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONTROL = ROOT / "deploy" / "operator" / "operatorctl.sh"
ENVIRONMENT = ROOT / "deploy" / "operator" / "environment.sh"
PUBLIC_VERBS = {
    "validate",
    "install",
    "start",
    "drain",
    "stop",
    "status",
    "health",
    "reconcile",
}
WINDOWS_VERBS = {"start", "drain", "stop", "status", "health"}


def run(arguments: list[str], *, control_python: str | None = None) -> subprocess.CompletedProcess[str]:
    environment = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(Path.home()),
        "PYTHONDONTWRITEBYTECODE": "1",
        "CONTROL_PYTHON": control_python or sys.executable,
    }
    return subprocess.run(
        ["bash", str(CONTROL), *arguments],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


class OperatorctlSurfaceTests(unittest.TestCase):
    def test_usage_names_only_the_first_install_lifecycle(self) -> None:
        result = run([])
        self.assertEqual(result.returncode, 2)
        usage = result.stderr
        for verb in PUBLIC_VERBS:
            self.assertIn(verb, usage)
        for removed in ("stage", "promote", "rollback"):
            self.assertNotIn(f"|{removed}|", usage)
        self.assertNotIn("--source-config", usage)
        self.assertNotIn("--endpoint-identity", usage)

    def test_removed_public_verbs_are_usage_errors(self) -> None:
        for verb in ("stage", "promote", "rollback"):
            with self.subTest(verb=verb):
                result = run([verb, "--config", "/nonexistent/platform.json"])
                self.assertEqual(result.returncode, 2)
                self.assertIn("usage:", result.stderr)

    def test_source_generation_and_cross_generation_inputs_are_absent(self) -> None:
        source = CONTROL.read_text(encoding="utf-8")
        for removed in (
            "--source-config",
            "SOURCE_CONFIG_SOURCE_FILE",
            "delegate_active_public_action",
            "run_generation_control",
            "promotion.py",
            "release_recovery.py",
            "connection_snapshot.py",
            "PIPELINE_STORE_ROOT",
        ):
            self.assertNotIn(removed, source)
        self.assertNotIn("load_env(", (ROOT / "deploy/operator/render_operator.py").read_text(encoding="utf-8"))

    def test_linux_consumes_only_the_installed_endpoint_verbs(self) -> None:
        source = CONTROL.read_text(encoding="utf-8")
        block = source[source.index("windows_lifecycle()") : source.index("windows_transport()")]
        for verb in WINDOWS_VERBS:
            self.assertIn(verb, block)
        for removed in ("stage", "promote", "rollback", "resume", "recovery-"):
            self.assertNotIn(f'"{removed}"', block)
        self.assertNotIn('"install"', block)
        self.assertNotIn("StageDir", source)
        self.assertNotIn("-Previous", source)
        self.assertNotIn("windows-lifecycle/v1", source)
        self.assertNotIn("scp ", source)
        self.assertIn('remote_binding="$WINDOWS_STATE_ROOT/windows-binding.json"', source)
        self.assertIn('remote_script="$WINDOWS_STATE_ROOT/instance-lifecycle.ps1"', source)

    def test_install_is_the_only_asset_bearing_verb(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "platform.json"
            config.write_text("{}", encoding="utf-8")
            for arguments in (
                ["start", "--config", str(config), "--install-id", "gen-1"],
                ["status", "--config", str(config), "--wheel", "wheel.whl"],
                ["health", "--config", str(config), "--release-file", "release.json"],
            ):
                with self.subTest(arguments=arguments):
                    result = run(arguments)
                    self.assertEqual(result.returncode, 2)
                    self.assertIn("usage:", result.stderr)

    def test_lifecycle_switches_are_action_specific(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "platform.json"
            config.write_text("{}", encoding="utf-8")
            self.assertEqual(
                run(["status", "--config", str(config), "--keep-database"]).returncode,
                2,
            )
            self.assertEqual(
                run(["drain", "--config", str(config), "--keep-admission-closed"]).returncode,
                2,
            )

    def test_control_python_guard_precedes_config_work(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            marker = Path(temporary) / "marker"
            wrapper = Path(temporary) / "python"
            wrapper.write_text(
                "#!/bin/bash\n"
                'case "$*" in\n'
                '  *"-I -B -c"*) exit 1 ;;\n'
                f"  *) printf reached > {str(marker)!r}; exit 1 ;;\n"
                "esac\n"
                "exit 1\n",
                encoding="utf-8",
            )
            wrapper.chmod(0o755)
            result = run(["validate", "--config", "/nonexistent/platform.json"], control_python=str(wrapper))
            self.assertEqual(result.returncode, 1)
            self.assertFalse(marker.exists())

    def test_environment_library_prevents_imports_from_mutating_the_release(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            operator = Path(temporary) / "operator"
            operator.mkdir()
            (operator / "environment.sh").write_bytes(ENVIRONMENT.read_bytes())
            (operator / "render_operator.py").write_text(
                "from pathlib import Path\n"
                "\n"
                "def read_env(path):\n"
                '    key, value = Path(path).read_text(encoding="utf-8").strip().split("=", 1)\n'
                "    return {key: value}\n",
                encoding="utf-8",
            )
            env_file = Path(temporary) / "operator.env"
            env_file.write_text("PROBE=present\n", encoding="utf-8")
            environment = {
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "HOME": str(Path.home()),
            }
            result = subprocess.run(
                [
                    "bash",
                    "-c",
                    'set -euo pipefail; HERE="$1"; CONTROL_PYTHON="$2"; '
                    'source "$HERE/environment.sh"; load_env_file "$3" PROBE; '
                    'test "$PROBE" = present; test ! -e "$HERE/__pycache__"',
                    "bash",
                    str(operator),
                    sys.executable,
                    str(env_file),
                ],
                cwd=ROOT,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_reconcile_keeps_only_current_business_effect_inputs(self) -> None:
        source = CONTROL.read_text(encoding="utf-8")
        self.assertIn("--run-id R --stage STAGE --effect-key KEY", source)
        self.assertIn('"$HERE/reconcile-effect.py"', source)


if __name__ == "__main__":
    unittest.main()
