"""operatorctl unified lifecycle surface over platform.json (config only).

All frozen lifecycle verbs (``validate/install/start/drain/stop/status/health/reconcile/stage/
promote/rollback``) take ``--config`` as the single entry; every one of them passes the same
static ``platform_config.py`` gate first.  The operational verbs load the active install
record and its derived per-generation environment; verbs whose bridge has not landed yet exit 3
without pretending.  ``--env-file`` does not exist as an entry point.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from description_pipeline import schemas

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
UNBRIDGED_VERBS = tuple(
    verb for verb in ALL_VERBS if verb not in {"validate", "install", "start", "drain", "stop", "status", "health"}
)
BRIDGED_OPERATIONAL = ("start", "drain", "stop", "status", "health")


def run(args: list[str]) -> subprocess.CompletedProcess:
    merged = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(Path.home())}
    return subprocess.run(["bash", str(CONTROL), *args], capture_output=True, text=True, env=merged, cwd=str(ROOT))


def platform_document(root: Path) -> dict:
    return {
        "schema_version": schemas.PLATFORM_CONFIG_SCHEMA,
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

    def test_install_over_config_requires_its_bound_inputs(self):
        missing_identity = run(["install", "--config", str(self.config)])
        self.assertEqual(missing_identity.returncode, 1)
        self.assertIn("--endpoint-identity", missing_identity.stderr)
        identity = self.root / "endpoint-identity.json"
        identity.write_text("{}", encoding="utf-8")
        missing_release = run(["install", "--config", str(self.config), "--endpoint-identity", str(identity)])
        self.assertEqual(missing_release.returncode, 1)
        self.assertIn("release manifest", missing_release.stderr)

    def test_every_other_verb_fails_closed_after_the_config_gate(self):
        for verb in UNBRIDGED_VERBS:
            with self.subTest(verb):
                result = run([verb, "--config", str(self.config)])
                self.assertEqual(result.returncode, 3)
                self.assertIn(verb, result.stderr)
                self.assertIn("not implemented in this increment", result.stderr)

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
        self.assertEqual(run(["validate", "--config", str(self.config), "--env-file", str(self.config)]).returncode, 2)
        self.assertEqual(run(["drain", "--env-file", str(self.config)]).returncode, 2)
        self.assertEqual(run(["install", "--env-file", str(self.config)]).returncode, 2)
        self.assertEqual(run(["status", "--config", str(self.config), "--install-id", "x"]).returncode, 2)
        missing = run(["install", "--config", str(self.root / "missing.json")])
        self.assertEqual(missing.returncode, 1)
        self.assertIn("config file not found", missing.stderr)

    def test_bridged_verbs_require_an_installed_generation(self):
        for verb in BRIDGED_OPERATIONAL:
            with self.subTest(verb):
                result = run([verb, "--config", str(self.config)])
                self.assertEqual(result.returncode, 1)
                self.assertIn("not installed", result.stderr)


if __name__ == "__main__":
    unittest.main()
