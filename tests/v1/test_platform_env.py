"""platform.json snapshot -> internal operator environment derivation (W2 install bridge)."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OPERATOR = ROOT / "deploy" / "operator"


def _load_from_path(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


platform_config = _load_from_path("deploy_platform_config_env", OPERATOR / "platform_config.py")
platform_env = _load_from_path("deploy_platform_env", OPERATOR / "platform_env.py")
install_record = _load_from_path("deploy_install_record_env", OPERATOR / "install_record.py")


def write_release(root: Path, *wheels: Path) -> dict:
    document = {
        "schema_version": install_record.RELEASE_SCHEMA,
        "pipeline_id": "solidworks-to-urdf",
        "version": "1.4.0",
        "source_commit": "c" * 40,
        "source_sha256": "a" * 64,
        "artifacts": {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in wheels},
    }
    (root / "release.json").write_text(json.dumps(document), encoding="utf-8")
    return install_record.release_identity(document)


def platform_document(root: Path) -> dict:
    return {
        "schema_version": platform_config.PLATFORM_SCHEMA,
        "operator": {
            "origin": "https://10.0.0.235:9443",
            "certificate_file": str(root / "secrets" / "tls.crt"),
            "private_key_file": str(root / "secrets" / "tls.key"),
        },
        "storage": {
            "state_root": str(root / "state"),
            "handoff_root": str(root / "handoffs"),
            "artifact_root": str(root / "artifacts"),
            "workspace_root": str(root / "workspaces"),
            "release_root": str(root / "releases"),
        },
        "database": {"dsn_file": str(root / "secrets" / "postgres.dsn")},
        "windows": {
            "ssh_host": "windows-m3",
            "endpoint_port": 9765,
            "token_file": str(root / "secrets" / "endpoint.token"),
            "instance": {
                "task_name": "description-e2e-endpoint",
                "package_root": "C:/description-e2e/packages",
                "output_root": "C:/description-e2e/results",
                "state_root": "C:/description-e2e/state",
            },
        },
        "auth": {
            "feishu_secret_file": str(root / "secrets" / "feishu.json"),
            "tenant_keys": ["tenant-a", "tenant-b"],
            "admin_open_ids": ["ou_admin"],
        },
        "targets": {"m3.0": {"repository_slug": "owner/model-repository", "base": "main"}},
        "specifications": {"record_roots": [str(root / "specifications")]},
    }


class PlatformEnvTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="platform-env-")
        self.root = Path(self._temporary.name)
        resolved = platform_config.validate_platform(platform_document(self.root))
        snapshot = platform_config.build_snapshot(
            resolved,
            platform_json_sha256="e" * 64,
            install_id="gen-1",
            generated_at="2026-10-13T00:00:00Z",
        )
        self.snapshot_path = platform_config.write_snapshot(snapshot, self.root / "state" / "snapshots")
        self.snapshot = snapshot
        self.tree = self.root / "deployment"
        self.tree.mkdir()
        self.wheel = self.tree / "mimicverse_description-1.4.0-py3-none-any.whl"
        self.wheel.write_bytes(b"wheel")
        self.release = write_release(self.root, self.wheel)

    def derive(self, snapshot, *, install_id, tree_root=None, wheel=None):
        return platform_env.derive(
            snapshot,
            install_id=install_id,
            tree_root=tree_root or self.tree,
            release=self.release,
            wheel=wheel,
        )

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def test_derives_the_internal_environment_and_derived_files(self):
        result = self.derive(self.snapshot, install_id="gen-1", tree_root=self.tree)
        env = result["env"]
        self.assertEqual(env["OPERATOR_HOST"], "10.0.0.235")
        self.assertEqual(env["OPERATOR_HTTPS_PORT"], "9443")
        self.assertEqual(env["OPERATOR_STATE"], str(self.root / "state"))
        self.assertEqual(env["AIRFLOW_VENV"], str(self.root / "releases" / "airflow-venv-gen-1"))
        self.assertEqual(env["AIRFLOW_HOME"], str(self.root / "state" / "airflow-home"))
        self.assertEqual(env["POSTGRES_ROOT"], str(self.root / "state" / "airflow-pg"))
        self.assertEqual(env["PIPELINE_WHEEL"], str(self.wheel))
        self.assertEqual(env["PIPELINE_STORE_ROOT"], str(self.root / "state" / "runs"))
        self.assertEqual(env["FEISHU_TENANT_KEYS"], "tenant-a,tenant-b")
        self.assertEqual(env["FEISHU_ADMIN_OPEN_IDS"], "ou_admin")
        self.assertEqual(env["SOLIDWORKS_ENDPOINT_PORT"], "9765")
        self.assertIn("airflow-pg/socket", env["AIRFLOW_DB_URL"])

        derived_dir = Path(env["OPERATOR_STATE"]) / "derived" / "gen-1"
        self.assertEqual(result["derived_dir"], derived_dir)
        self.assertFalse((Path(env["OPERATOR_STATE"]) / "derived" / "operator.env").exists())
        repositories = json.loads((derived_dir / "repositories.json").read_text(encoding="utf-8"))
        self.assertEqual(repositories, self.snapshot["resolved"]["derived"]["repositories"])
        targets = json.loads((derived_dir / "targets.json").read_text(encoding="utf-8"))
        self.assertEqual(targets, self.snapshot["resolved"]["targets"])
        discovery = json.loads((derived_dir / "discovery.json").read_text(encoding="utf-8"))
        self.assertEqual(discovery, self.snapshot["resolved"]["derived"]["discovery"])
        env_file = Path(result["env_path"])
        self.assertEqual(stat.S_IMODE(env_file.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(env_file.parent.stat().st_mode), 0o700)
        parsed = dict(line.split("=", 1) for line in env_file.read_text(encoding="utf-8").splitlines() if line)
        self.assertEqual(parsed, env)

    def test_wheel_selection_is_bound_to_the_release(self):
        second = self.tree / "another.whl"
        second.write_bytes(b"other")
        with self.assertRaises(SystemExit) as raised:
            self.derive(self.snapshot, install_id="gen-1")
        self.assertIn("exactly one", str(raised.exception))
        self.release = write_release(self.root, self.wheel, second)
        result = self.derive(self.snapshot, install_id="gen-1", wheel=second)
        self.assertEqual(result["wheel"], second)
        rogue = self.tree / "rogue.whl"
        rogue.write_bytes(b"rogue")
        with self.assertRaises(SystemExit) as raised:
            self.derive(self.snapshot, install_id="gen-1", wheel=rogue)
        self.assertIn("not a release artifact", str(raised.exception))
        rogue_same_name = self.root / "unrelated" / self.wheel.name
        rogue_same_name.parent.mkdir()
        rogue_same_name.write_bytes(b"UNINDEXED-ROGUE-WHEEL")
        with self.assertRaises(SystemExit) as raised:
            self.derive(self.snapshot, install_id="gen-1", wheel=rogue_same_name)
        self.assertIn("does not match release.json", str(raised.exception))
        with self.assertRaises(SystemExit):
            self.derive(self.snapshot, install_id="gen-1", wheel=self.tree / "missing.whl")

    def test_render_operator_consumes_the_derived_env(self):
        result = self.derive(self.snapshot, install_id="gen-1", tree_root=self.tree)
        state = self.root / "state"
        secrets = state / "secrets"
        secrets.mkdir(parents=True, exist_ok=True)
        (secrets / "tls.crt").write_bytes(b"cert")
        (secrets / "tls.key").write_bytes(b"key")
        (secrets / "feishu.json").write_bytes(b"{}")
        (secrets / "endpoint.token").write_bytes(b"token")
        (secrets / "postgres.dsn").write_bytes(b"dsn")
        os.chmod(state, 0o700)
        for name in ("tls.crt", "tls.key", "feishu.json", "endpoint.token", "postgres.dsn"):
            os.chmod(secrets / name, 0o600)
        completed = subprocess.run(
            [
                sys.executable,
                str(OPERATOR / "render_operator.py"),
                "--env-file",
                str(result["env_path"]),
                "--dry-run",
            ],
            capture_output=True,
            text=True,
            cwd=str(ROOT),
        )
        # A first dry-run reports missing installed files as DRIFT; it must parse the derived env
        # and never fail on configuration shape (exit 1 = drift, exit 0 = none).
        self.assertIn(completed.returncode, (0, 1), completed.stderr)
        self.assertNotIn("Traceback", completed.stderr)

    def test_emit_cli(self):
        completed = subprocess.run(
            [
                sys.executable,
                str(OPERATOR / "platform_env.py"),
                "emit",
                "--snapshot",
                str(self.snapshot_path),
                "--install-id",
                "gen-1",
                "--tree",
                str(self.tree),
                "--release-file",
                str(self.root / "release.json"),
            ],
            capture_output=True,
            text=True,
            cwd=str(ROOT),
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        summary = dict(line.split("=", 1) for line in completed.stdout.strip().splitlines())
        self.assertTrue(Path(summary["env"]).is_file())
        self.assertEqual(Path(summary["wheel"]), self.wheel)

    def test_generations_are_independent_and_immutable(self):
        first = self.derive(self.snapshot, install_id="gen-1", tree_root=self.tree)
        first_env_bytes = Path(first["env_path"]).read_bytes()
        first_targets_path = Path(first["env"]["MODEL_TARGETS_FILE"])
        first_targets_bytes = first_targets_path.read_bytes()

        second_snapshot = json.loads(json.dumps(self.snapshot))
        second_snapshot["install_id"] = "gen-2"
        second_snapshot["resolved"]["targets"]["m3.0"]["base"] = "feature/review-next"
        platform_config.validate_snapshot_document(second_snapshot)
        second = self.derive(second_snapshot, install_id="gen-2", tree_root=self.tree)
        self.assertNotEqual(first["env_path"], second["env_path"])
        self.assertEqual(Path(first["env_path"]).read_bytes(), first_env_bytes)
        self.assertEqual(first_targets_path.read_bytes(), first_targets_bytes)
        self.assertEqual(json.loads(first_targets_path.read_text())["m3.0"]["base"], "main")
        self.assertEqual(
            json.loads(Path(second["env"]["MODEL_TARGETS_FILE"]).read_text())["m3.0"]["base"],
            "feature/review-next",
        )

        conflicting = json.loads(json.dumps(self.snapshot))
        conflicting["resolved"]["targets"]["m3.0"]["base"] = "other"
        with self.assertRaises(SystemExit):
            self.derive(conflicting, install_id="gen-1", tree_root=self.tree)
        self.assertEqual(first_targets_path.read_bytes(), first_targets_bytes)
        self.assertEqual(Path(first["env_path"]).read_bytes(), first_env_bytes)

    def test_malformed_inputs_fail_closed_without_writing(self):
        cases = []
        broken = json.loads(json.dumps(self.snapshot))
        broken["resolved"]["storage"]["state_root"] = "relative/state"
        cases.append(("relative state root", broken, "gen-1"))
        broken = json.loads(json.dumps(self.snapshot))
        broken["resolved"]["windows"]["endpoint_port"] = "9765"
        cases.append(("string endpoint port", broken, "gen-1"))
        cases.append(("generation mismatch", self.snapshot, "gen-2"))
        foreign = json.loads(json.dumps(self.snapshot))
        foreign["schema_version"] = "review.foreign-snapshot/v99"
        cases.append(("foreign schema", foreign, "gen-1"))
        cases.append(("bad install id", self.snapshot, "bad/id"))
        for label, snapshot, install_id in cases:
            with self.subTest(label):
                with self.assertRaises(SystemExit):
                    self.derive(snapshot, install_id=install_id, tree_root=self.tree)
                self.assertFalse((self.root / "state" / "derived").exists(), "no derived files may be written")


if __name__ == "__main__":
    unittest.main()
