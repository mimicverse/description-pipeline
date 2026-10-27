"""The recovery entry must prefer its own release source over caller shadows."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
RECOVERY = ROOT / "deploy" / "operator" / "recovery.py"
RELEASE_SOURCE = ROOT / "src"
sys.path.insert(0, str(RECOVERY.parent))
import recovery  # noqa: E402


class RecoveryImportPriorityTests(unittest.TestCase):
    def _run(self, program: str) -> dict:
        completed = subprocess.run(
            [sys.executable, "-I", "-B", "-c", program],
            cwd=ROOT,
            env={
                **os.environ,
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONPATH": "",
            },
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertEqual(completed.stderr, "")
        return json.loads(completed.stdout)

    def test_shadow_control_and_release_source_entry(self) -> None:
        with tempfile.TemporaryDirectory(prefix="recovery-import-priority-") as temporary:
            shadow = Path(temporary) / "shadow"
            package = shadow / "description_pipeline"
            package.mkdir(parents=True)
            (package / "__init__.py").write_text("", encoding="utf-8")

            control = self._run(
                f"""
import json
import sys

sys.path.insert(0, {str(shadow)!r})
sys.path.append({str(RELEASE_SOURCE)!r})
try:
    import description_pipeline.pipeline
except ModuleNotFoundError as error:
    print(json.dumps({{"error": error.name}}))
else:
    raise SystemExit("shadow package unexpectedly resolved")
"""
            )
            self.assertEqual(control, {"error": "description_pipeline.pipeline"})

            result = self._run(
                f"""
import json
import runpy
import sys
from pathlib import Path

sys.path.insert(0, {str(RECOVERY.parent)!r})
sys.path.insert(0, {str(shadow)!r})
sys.path.append({str(RELEASE_SOURCE)!r})
runpy.run_path({str(RECOVERY)!r}, run_name="recovery_import_priority_probe")

import description_pipeline

source = Path(description_pipeline.__file__).resolve()
release = Path({str(RELEASE_SOURCE)!r}).resolve()
if not source.is_relative_to(release):
    raise SystemExit(f"recovery imported outside its release source: {{source}}")
print(json.dumps({{"source": str(source)}}))
"""
            )
            self.assertTrue(Path(result["source"]).is_relative_to(RELEASE_SOURCE))

    def test_recovery_has_no_package_history_authority(self) -> None:
        source = RECOVERY.read_text(encoding="utf-8")
        for removed in (
            "journal_selection",
            "promotion.py",
            "release_recovery.py",
            "connection_snapshot.py",
            "import backup",
            "--source-config",
            "read_recovery_delivery_manifest",
            '"deploy", "airflow", "install.sh"',
            '["bash", str(install), "--stage"]',
            "database-acl",
            "facts-boundary",
            "store_root",
            "PIPELINE_STORE_ROOT",
            "state/runs",
            "-Verb recovery-",
        ):
            self.assertNotIn(removed, source)
        self.assertIn("read_delivery_manifest", source)
        self.assertIn("windows-binding.json", source)
        self.assertIn("instance-lifecycle.ps1", source)

    def test_current_encrypted_backup_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            (source / "nested").mkdir(parents=True)
            (source / "database.dump").write_bytes(b"database")
            (source / "nested/data.json").write_text('{"ok":true}\n', encoding="utf-8")
            key = root / "recovery.key"
            archive = root / "recovery.tar.enc"
            digest = recovery.seal_recovery_archive(source, key, archive)
            self.assertEqual(digest, recovery.sha256_file(archive))
            destination = root / "restored"
            recovery.extract_recovery_archive(archive, key, destination)
            self.assertEqual((destination / "database.dump").read_bytes(), b"database")
            self.assertEqual(
                (destination / "nested/data.json").read_text(encoding="utf-8"),
                '{"ok":true}\n',
            )

    def test_private_staging_cleanup_removes_only_the_owned_tree(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            staging = root / "backup"
            staging.mkdir()
            (staging / "member").write_text("data", encoding="utf-8")
            retained = root / "recovery-manifest.json"
            retained.write_text("{}", encoding="utf-8")
            self.assertTrue(recovery.remove_private_staging(root))
            self.assertFalse(staging.exists())
            self.assertTrue(retained.exists())

    def test_windows_target_plan_uses_current_status_identity_only(self) -> None:
        state = {
            "ssh_host": "windows.example",
            "state_root": "C:/description/state",
            "output_root": "C:/description/results",
            "package_root": "C:/description/packages",
            "status": {
                "identity": {
                    "install_id": "win-" + "1" * 32,
                    "config_digest": "d" * 64,
                    "release": {
                        "version": "1.0.0",
                        "source_commit": "a" * 40,
                        "source_sha256": "b" * 64,
                        "role": "native",
                    },
                }
            },
        }
        target = recovery._windows_target_from_state(state)
        self.assertEqual(
            set(target),
            {
                "ssh_host",
                "state_root",
                "output_root",
                "package_root",
                "install_id",
                "config_digest",
                "release",
            },
        )
        self.assertNotIn("task_xml_sha256", target)
        self.assertNotIn("previous", target)

    def test_windows_scripts_use_only_the_deployment_member_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "deployment.zip"
            destination = root / "scripts" / "instance-lifecycle.ps1"
            with zipfile.ZipFile(archive, "w") as bundle:
                bundle.writestr("deploy/windows/instance-lifecycle.ps1", b"current")
                bundle.writestr("windows/instance-lifecycle.ps1", b"legacy")
                bundle.writestr("instance-lifecycle.ps1", b"root")
            recovery._windows_script_from_deployment(
                archive,
                destination,
                name="instance-lifecycle.ps1",
            )
            self.assertEqual(destination.read_bytes(), b"current")

            legacy = root / "legacy.zip"
            with zipfile.ZipFile(legacy, "w") as bundle:
                bundle.writestr("windows/instance-lifecycle.ps1", b"legacy")
            with self.assertRaises(recovery.RecoveryError) as raised:
                recovery._windows_script_from_deployment(
                    legacy,
                    root / "scripts" / "legacy.ps1",
                    name="instance-lifecycle.ps1",
                )
            self.assertEqual(
                raised.exception.code,
                "windows_recovery_bundle_invalid",
            )

    def test_windows_verify_only_observes_the_current_disabled_status(self) -> None:
        identity = {
            "install_id": "win-" + "1" * 32,
            "config_digest": "d" * 64,
            "release": {
                "version": "1.0.0",
                "source_commit": "a" * 40,
                "source_sha256": "b" * 64,
                "role": "native",
            },
        }
        task = {
            "task_name": "URDF Endpoint",
            "task_path": "\\URDF\\",
            "state": "Disabled",
            "semantic_sha256": "c" * 64,
            "security_descriptor_sha256": "e" * 64,
        }
        state = {"task": task, "identity": identity}
        live = {
            "passed": True,
            "verb": "status",
            "task": dict(task),
            "identity": identity,
            "listener": None,
            "health": None,
            "status": {
                "task_exists": True,
                "task_state": "Disabled",
                "error": None,
                "consistent": True,
                "solidworks_processes": 0,
            },
        }
        plan = {
            "windows": {
                "ssh_host": "windows.example",
                "state_root": "C:/description/state",
            }
        }
        with tempfile.TemporaryDirectory() as temporary:
            status_dir = Path(temporary) / "status"
            status_dir.mkdir()
            with (
                mock.patch.object(
                    recovery,
                    "_validate_windows_recovery_state",
                    return_value={"status": state},
                ),
                mock.patch.object(recovery, "_verify_windows_state_members"),
                mock.patch.object(
                    recovery,
                    "_windows_public_verb",
                    return_value=live,
                ) as observed,
            ):
                result = recovery.restore_windows_recovery(
                    manifest={"windows": {}},
                    plan=plan,
                    extracted=Path(temporary) / "unused",
                    status_dir=status_dir,
                    verify_only=True,
                    commands=[],
                )
            self.assertTrue(result["verified_only"])
            self.assertEqual(result["status"], live)
            observed.assert_called_once_with(
                ssh_host="windows.example",
                state_root="C:/description/state",
                verb="status",
                timeout=180,
                commands=[],
            )

            live["task"]["state"] = "Running"
            with (
                mock.patch.object(
                    recovery,
                    "_validate_windows_recovery_state",
                    return_value={"status": state},
                ),
                mock.patch.object(recovery, "_verify_windows_state_members"),
                mock.patch.object(
                    recovery,
                    "_windows_public_verb",
                    return_value=live,
                ),
                self.assertRaises(recovery.RecoveryError) as raised,
            ):
                recovery.restore_windows_recovery(
                    manifest={"windows": {}},
                    plan=plan,
                    extracted=Path(temporary) / "unused",
                    status_dir=Path(temporary) / "status-running",
                    verify_only=True,
                    commands=[],
                )
            self.assertEqual(raised.exception.code, "windows_recovery_state_invalid")

    def test_windows_restore_uploads_the_validated_install_receipt(self) -> None:
        identity = {
            "install_id": "win-" + "1" * 32,
            "config_digest": "d" * 64,
            "release": {
                "version": "1.0.0",
                "source_commit": "a" * 40,
                "source_sha256": "b" * 64,
                "role": "native",
            },
        }
        task = {
            "task_name": "URDF Endpoint",
            "task_path": "\\URDF\\",
            "state": "Disabled",
            "semantic_sha256": "c" * 64,
            "security_descriptor_sha256": "e" * 64,
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            extracted = root / "extracted"
            windows = extracted / "windows"
            windows.mkdir(parents=True)
            artifacts = extracted / "release" / "artifacts"
            artifacts.mkdir(parents=True)
            status_dir = root / "status"
            status_dir.mkdir()

            binding = {
                "endpoint_config": "C:/description/config/endpoint.json",
                "release_file": "C:/description/release/release.json",
                "task_name": task["task_name"],
                "task_path": task["task_path"],
                "service_account": "DESCRIPTION\\operator",
                "python_executable": "C:/Python312/python.exe",
            }
            (windows / "windows-binding.json").write_text(
                json.dumps(binding),
                encoding="utf-8",
            )
            (windows / "endpoint.json").write_text(
                json.dumps({"token_file": "C:/description/config/endpoint.token"}),
                encoding="utf-8",
            )
            (windows / "token").write_text("token\n", encoding="utf-8")
            (windows / "install-id").write_text(
                identity["install_id"] + "\n",
                encoding="utf-8",
            )
            (windows / "install-receipt.json").write_text(
                '{"passed":true}\n',
                encoding="utf-8",
            )
            (windows / "release.json").write_text("{}\n", encoding="utf-8")
            (windows / "output").mkdir()

            deployment_bytes = b"deployment"
            deployment_name = "description-1.0.0-test-deployment.zip"
            deployment = artifacts / deployment_name
            deployment.write_bytes(deployment_bytes)
            windows_name = "description-1.0.0-windows-cp312-x86_64.zip"
            windows_archive = artifacts / windows_name
            windows_archive.write_bytes(b"windows-runtime")
            lifecycle = b"lifecycle\n"
            bootstrap = b"bootstrap\n"
            state = {
                "ssh_host": "windows.example",
                "state_root": "C:/description/state",
                "output_root": "C:/description/output",
                "package_root": "C:/description/packages",
                "binding": {"member": "windows/windows-binding.json"},
                "endpoint_config": {"member": "windows/endpoint.json"},
                "token": {"member": "windows/token"},
                "install_id": {"member": "windows/install-id"},
                "install_receipt": {"member": "windows/install-receipt.json"},
                "release": {"member": "windows/release.json"},
                "lifecycle": {
                    "sha256": hashlib.sha256(lifecycle).hexdigest(),
                },
                "bootstrap": {
                    "sha256": hashlib.sha256(bootstrap).hexdigest(),
                },
                "native_operations": {"exists": False},
                "output": {"member": "windows/output"},
                "archive_name": windows_name,
                "archive_sha256": hashlib.sha256(b"windows-runtime").hexdigest(),
                "status": {"task": task, "identity": identity},
            }
            manifest = {
                "windows": {},
                "release": {
                    "artifacts": [
                        {
                            "name": deployment_name,
                            "member": f"release/artifacts/{deployment_name}",
                            "sha256": hashlib.sha256(deployment_bytes).hexdigest(),
                        }
                    ]
                },
            }
            live = {
                "passed": True,
                "verb": "status",
                "task": dict(task),
                "identity": identity,
                "listener": None,
                "health": None,
                "status": {
                    "task_exists": True,
                    "task_state": "Disabled",
                    "error": None,
                    "consistent": True,
                    "solidworks_processes": 0,
                },
            }
            transfers = []

            def fake_scp(**arguments):
                transfers.append(
                    (Path(arguments["source"]), arguments["destination"])
                )

            def fake_script(
                archive: Path,
                destination: Path,
                *,
                name: str,
            ) -> None:
                self.assertEqual(archive, deployment)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(
                    lifecycle if name == "instance-lifecycle.ps1" else bootstrap
                )

            def public_verb(*, verb, **arguments):
                del arguments
                if verb == "install":
                    return {"passed": True, "verb": "install", "identity": identity}
                return live

            with (
                mock.patch.object(
                    recovery,
                    "_validate_windows_recovery_state",
                    return_value=state,
                ),
                mock.patch.object(recovery, "_verify_windows_state_members"),
                mock.patch.object(
                    recovery,
                    "_windows_script_from_deployment",
                    side_effect=fake_script,
                ),
                mock.patch.object(
                    recovery,
                    "_windows_restore_target_empty",
                ),
                mock.patch.object(recovery, "_windows_create_parents"),
                mock.patch.object(
                    recovery,
                    "_windows_scp",
                    side_effect=fake_scp,
                ),
                mock.patch.object(
                    recovery,
                    "_windows_public_verb",
                    side_effect=public_verb,
                ),
            ):
                result = recovery.restore_windows_recovery(
                    manifest=manifest,
                    plan={"windows": {"ssh_host": "windows.example", "state_root": state["state_root"]}},
                    extracted=extracted,
                    status_dir=status_dir,
                    verify_only=False,
                    commands=[],
                )
            self.assertEqual(result["status"], live)
            self.assertTrue(
                any(
                    destination == "windows.example:C:/description/state/install-receipt.json"
                    for _source, destination in transfers
                )
            )


if __name__ == "__main__":
    unittest.main()
