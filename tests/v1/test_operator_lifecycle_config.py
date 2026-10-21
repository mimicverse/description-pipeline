"""operatorctl unified lifecycle surface over platform.json (config only).

All frozen lifecycle verbs (``validate/install/start/drain/stop/status/health/reconcile/stage/
promote/rollback``) take ``--config`` as the single entry; every one of them passes the same
static ``platform_config.py`` gate first.  The operational verbs load the active install
record and its derived per-generation environment; each of them refuses on its real
preconditions with zero side effects instead of pretending.  ``--env-file`` does not exist as
an entry point.
"""

from __future__ import annotations

import fcntl
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
OPERATIONAL_VERBS = tuple(verb for verb in ALL_VERBS if verb not in {"validate", "install"})


def run(
    args: list[str],
    *,
    env: dict[str, str] | None = None,
    pass_fds: tuple[int, ...] = (),
) -> subprocess.CompletedProcess:
    merged = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(Path.home())}
    merged.update(env or {})
    return subprocess.run(
        ["bash", str(CONTROL), *args],
        capture_output=True,
        text=True,
        env=merged,
        cwd=str(ROOT),
        pass_fds=pass_fds,
    )


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


class OperatorctlSurfaceTests(unittest.TestCase):
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

    def test_every_operational_verb_requires_an_installed_generation(self):
        # Every verb — the maintenance switch included — fails closed on its real first
        # precondition (the active generation) before any side effect, with no transitional
        # "not implemented" state left anywhere.
        for verb in OPERATIONAL_VERBS:
            with self.subTest(verb):
                result = run([verb, "--config", str(self.config)])
                self.assertEqual(result.returncode, 1)
                self.assertIn("not installed", result.stderr)

    def test_keep_database_is_a_stop_only_maintenance_switch(self):
        accepted = run(
            [
                "stop",
                "--config",
                str(self.config),
                "--keep-database",
            ]
        )
        self.assertEqual(accepted.returncode, 1)
        self.assertIn("not installed", accepted.stderr)
        for verb in ("start", "drain", "promote"):
            with self.subTest(verb):
                refused = run(
                    [
                        verb,
                        "--config",
                        str(self.config),
                        "--keep-database",
                    ]
                )
                self.assertEqual(refused.returncode, 2)
                self.assertIn("only accepted by stop", refused.stderr)

    def test_keep_database_stop_branch_retains_postgres(self):
        source = CONTROL.read_text(encoding="utf-8")
        branch = source.split('if [ -n "$KEEP_DATABASE" ]; then', 1)[1].split(
            "else",
            1,
        )[0]
        self.assertIn("airflow_lifecycle stop-executors", branch)
        self.assertNotIn("airflow_lifecycle stop\n", branch)
        self.assertIn("description-postgres.service", branch)
        self.assertIn("PostgreSQL remains active", branch)

    def test_start_resumes_windows_after_linux_and_before_admission(self):
        source = CONTROL.read_text(encoding="utf-8")
        start_branch = source.rsplit("  start)\n", 1)[1].split("  drain)\n", 1)[0]
        linux_services = start_branch.index("systemctl --user enable --now")
        windows_resume = start_branch.index('instance_lifecycle_resume "$ACTIVE_ID" "$STATE_ROOT/install-record.json"')
        dag_unpause = start_branch.index("airflow_unpause_dag")
        admission_open = start_branch.index("platform_open_admission")
        self.assertLess(linux_services, windows_resume)
        self.assertLess(windows_resume, dag_unpause)
        self.assertLess(dag_unpause, admission_open)

    def test_private_windows_resume_uses_authenticated_identity_and_health(self):
        source = CONTROL.read_text(encoding="utf-8")
        resume = source.split("instance_lifecycle_resume() {", 1)[1].split("\n# The Windows end of the unified", 1)[0]
        self.assertIn('"release_identity_sha256": hashlib.sha256', resume)
        self.assertIn('"install_id": end["install_id"]', resume)
        self.assertIn('"config_digest": end["config_digest"]', resume)
        self.assertIn('"script_sha256": hashlib.sha256', resume)
        self.assertIn("-o StrictHostKeyChecking=yes", resume)
        self.assertIn("-Verb resume -Binding", resume)
        self.assertNotIn("endpoint.token", resume)
        self.assertNotIn("password", resume.lower())

    def test_start_refuses_while_the_shared_maintenance_lock_is_held(self):
        state_root = self.root / "state"
        state_root.mkdir()
        (state_root / "resolved.env").write_text("", encoding="utf-8")
        lock_dir = state_root / "locks"
        lock_dir.mkdir()
        lock_path = lock_dir / "maintenance.lock"
        lock_path.write_text(
            json.dumps(
                {
                    "schema_version": "solidworks-to-urdf.maintenance-lock/v1",
                    "owner_id": "a" * 64,
                }
            ),
            encoding="utf-8",
        )
        descriptor = os.open(lock_path, os.O_RDWR)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = run(["start", "--config", str(self.config)])
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)
        self.assertEqual(result.returncode, 1)
        self.assertIn(
            "another supported lifecycle operation owns the maintenance lock",
            result.stderr,
        )

    def test_inherited_lock_requires_the_actual_held_file_descriptor(self):
        state_root = self.root / "state"
        state_root.mkdir()
        (state_root / "resolved.env").write_text("", encoding="utf-8")
        lock_dir = state_root / "locks"
        lock_dir.mkdir()
        lock_path = lock_dir / "maintenance.lock"
        owner = "a" * 64
        lock_path.write_text(
            json.dumps(
                {
                    "schema_version": "solidworks-to-urdf.maintenance-lock/v1",
                    "owner_id": owner,
                    "acquired_at": "2026-10-21T00:00:00Z",
                    "pid": os.getpid(),
                }
            ),
            encoding="utf-8",
        )
        held = os.open(lock_path, os.O_RDWR)
        unlocked = os.open(lock_path, os.O_RDWR)
        try:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            missing_fd = run(
                ["start", "--config", str(self.config)],
                env={
                    "OPERATORCTL_MAINTENANCE_LOCK_OWNER": owner,
                    "OPERATORCTL_MAINTENANCE_LOCK_PATH": str(lock_path),
                },
            )
            self.assertIn("must carry the caller's file descriptor", missing_fd.stderr)

            unlocked_result = run(
                ["start", "--config", str(self.config)],
                env={
                    "OPERATORCTL_MAINTENANCE_LOCK_OWNER": owner,
                    "OPERATORCTL_MAINTENANCE_LOCK_FD": str(unlocked),
                    "OPERATORCTL_MAINTENANCE_LOCK_PATH": str(lock_path),
                },
                pass_fds=(unlocked,),
            )
            self.assertIn(
                "does not itself hold the write lock",
                unlocked_result.stderr,
            )

            inherited = run(
                ["start", "--config", str(self.config)],
                env={
                    "OPERATORCTL_MAINTENANCE_LOCK_OWNER": owner,
                    "OPERATORCTL_MAINTENANCE_LOCK_FD": str(held),
                    "OPERATORCTL_MAINTENANCE_LOCK_PATH": str(lock_path),
                },
                pass_fds=(held,),
            )
            self.assertIn("maintenance_lock=inherited:", inherited.stdout)
            self.assertNotIn("inherited maintenance lock", inherited.stderr)

            replaced = self.root / "maintenance.lock.replaced"
            os.replace(lock_path, replaced)
            lock_path.write_text(
                json.dumps(
                    {
                        "schema_version": "solidworks-to-urdf.maintenance-lock/v1",
                        "owner_id": owner,
                        "acquired_at": "2026-10-21T00:00:01Z",
                        "pid": os.getpid(),
                    }
                ),
                encoding="utf-8",
            )
            replaced_result = run(
                ["start", "--config", str(self.config)],
                env={
                    "OPERATORCTL_MAINTENANCE_LOCK_OWNER": owner,
                    "OPERATORCTL_MAINTENANCE_LOCK_FD": str(held),
                    "OPERATORCTL_MAINTENANCE_LOCK_PATH": str(lock_path),
                },
                pass_fds=(held,),
            )
            self.assertIn(
                "descriptor is not the held state-root lock",
                replaced_result.stderr,
            )
        finally:
            os.close(unlocked)
            os.close(held)

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


if __name__ == "__main__":
    unittest.main()
