"""operatorctl unified verb skeleton over platform.json (W2 lifecycle increment 1).

The skeleton recognizes the frozen lifecycle verbs
(``validate/install/start/drain/stop/status/health/reconcile/stage/promote/rollback``), gates
every one of them on the same static ``platform_config.py`` check, and refuses to pretend a
bridge that does not exist: verbs without their ``--config`` implementation exit 3 and point at
the transitional ``--env-file`` entry.  The transitional entry itself must stay behaviorally
untouched until the bridge replaces it.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OPERATOR = ROOT / "deploy" / "operator"
CONTROL = OPERATOR / "operatorctl.sh"

ALL_VERBS = (
    "validate",
    "install",
    "start",
    "drain",
    "stop",
    "status",
    "health",
    "reconcile",
    "stage",
    "promote",
    "rollback",
)
UNBRIDGED_VERBS = tuple(verb for verb in ALL_VERBS if verb != "validate")


def run(args: list[str]) -> subprocess.CompletedProcess:
    merged = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(Path.home())}
    return subprocess.run(["bash", str(CONTROL), *args], capture_output=True, text=True, env=merged, cwd=str(ROOT))


def platform_document(root: Path) -> dict:
    return {
        "schema_version": "solidworks-to-urdf.platform/v1",
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
            "tenant_keys": ["tenant-key"],
            "admin_open_ids": [],
        },
        "targets": {"m3.0": {"repository_slug": "owner/model-repository", "base": "main"}},
        "specifications": {"record_roots": [str(root / "specifications")]},
    }


class LifecycleSkeletonTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="operatorctl-config-")
        self.root = Path(self._temporary.name)
        self.config = self.root / "platform.json"
        self.config.write_text(json.dumps(platform_document(self.root)), encoding="utf-8")

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def test_validate_verb_accepts_a_valid_configuration(self):
        result = run(["validate", "--config", str(self.config)])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("configuration valid", result.stdout)

    def test_every_other_verb_fails_closed_after_the_config_gate(self):
        for verb in UNBRIDGED_VERBS:
            with self.subTest(verb):
                result = run([verb, "--config", str(self.config)])
                self.assertEqual(result.returncode, 3)
                self.assertIn(verb, result.stderr)
                self.assertIn("not implemented in this increment", result.stderr)
                self.assertIn("--env-file", result.stderr)

    def test_invalid_config_fails_before_any_verb_behavior(self):
        bad = self.root / "bad.json"
        document = platform_document(self.root)
        document["storage"]["state_root"] = "relative/state"
        bad.write_text(json.dumps(document), encoding="utf-8")
        for verb in ("validate", "install", "promote"):
            with self.subTest(verb):
                result = run([verb, "--config", str(bad)])
                self.assertEqual(result.returncode, 1)
                self.assertIn("platform_config:", result.stderr)

    def test_usage_errors(self):
        self.assertEqual(run([]).returncode, 2)
        self.assertEqual(run(["frobnicate", "--config", str(self.config)]).returncode, 2)
        self.assertEqual(run(["validate"]).returncode, 2)
        self.assertEqual(
            run(["validate", "--config", str(self.config), "--env-file", str(self.config)]).returncode, 2
        )
        self.assertEqual(run(["drain", "--env-file", str(self.config)]).returncode, 2)
        missing = run(["install", "--config", str(self.root / "missing.json")])
        self.assertEqual(missing.returncode, 1)
        self.assertIn("config file not found", missing.stderr)

    def test_transitional_env_entry_is_untouched(self):
        result = run(["install", "--env-file", str(self.root / "missing.env")])
        self.assertEqual(result.returncode, 1)
        self.assertIn("env file not found", result.stderr)
        stub = self.root / "operator.env"
        stub.write_text("OPERATOR_STATE=/nonexistent\n", encoding="utf-8")
        result = run(["status", "--env-file", str(stub)])
        self.assertEqual(result.returncode, 1)
        self.assertIn("not installed", result.stderr)


if __name__ == "__main__":
    unittest.main()
