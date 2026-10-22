"""A deployed archive is the recovery source; runtime roots are destinations."""

import hashlib
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "deploy/operator"))
import recovery  # noqa: E402


class ArchiveNativeRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.archive = self.root / "archive"
        self.runtime = self.root / "runtime"
        self.state = self.root / "state"
        self.script = self.archive / "deploy/operator/recovery.py"
        self.script.parent.mkdir(parents=True)
        self.script.write_bytes(Path(recovery.__file__).read_bytes())
        self.runtime.mkdir()
        self.state.mkdir()
        self.raw = {
            "schema_version": "solidworks-to-urdf.release/v1",
            "version": "1.5.0",
            "source_commit": "a" * 40,
            "source_tree": "b" * 40,
            "source_sha256": "c" * 64,
            "deployment_files": {"deploy/operator/recovery.py": self.sha(self.script)},
        }
        self.wheel = self.archive / "mimicverse_description-1.5.0-py3-none-any.whl"
        with zipfile.ZipFile(self.wheel, "w") as archive:
            archive.writestr("description_pipeline/tool-release.json", json.dumps(self.raw))
        self.raw["artifacts"] = {self.wheel.name: self.sha(self.wheel)}
        self.manifest = self.archive / "release.json"
        self.save_manifest()
        self.config = self.root / "platform.json"
        self.config.write_text("{}")
        self.snapshot_path = self.state / "snapshot.json"
        self.snapshot_path.write_text("{}")
        self.resolved = {
            "storage": {"state_root": str(self.state), "release_root": str(self.runtime)},
            "derived": {"store_root": str(self.state / "store")},
        }
        self.snapshot = {
            "schema_version": recovery.platform_config.SNAPSHOT_SCHEMA,
            "install_id": "fixture-r1",
            "sources": {"platform_json_sha256": self.sha(self.config)},
            "resolved": self.resolved,
        }
        self.record = {
            "install_id": "fixture-r1",
            "snapshot": {"path": str(self.snapshot_path), "digest": self.sha(self.snapshot_path)},
            "release": recovery.install_record.release_identity(self.raw),
        }
        self.env_file = self.state / "derived/fixture-r1/operator.env"
        self.env_file.parent.mkdir(parents=True)
        self.env_file.write_text("")
        self.venv = self.runtime / "airflow-venv-fixture-r1"
        self.venv.mkdir()
        self.env = {
            "POSTGRES_MAJOR": "14",
            "POSTGRES_ROOT": str(self.state / "airflow-pg"),
            "AIRFLOW_HOME": str(self.state / "airflow-home"),
            "AIRFLOW_VENV": str(self.venv),
            "PIPELINE_WHEEL": str(self.wheel),
            "PIPELINE_STORE_ROOT": str(self.state / "store"),
            **dict.fromkeys(
                ("AIRFLOW_DB_URL", "AIRFLOW_RUNTIME_DB_URL", "PLATFORM_SERVICE_DSN", "PLATFORM_OPERATOR_DSN"), "fixture"
            ),
        }

    @staticmethod
    def sha(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def save_manifest(self):
        self.manifest.write_text(json.dumps(self.raw))

    def load(self):
        with (
            mock.patch.object(recovery, "__file__", str(self.script)),
            mock.patch.object(
                recovery.platform_config,
                "load_document",
                side_effect=[(self.config.read_bytes(), {}), (self.snapshot_path.read_bytes(), self.snapshot)],
            ),
            mock.patch.object(recovery.platform_config, "validate_platform", return_value=self.resolved),
            mock.patch.object(recovery.install_record, "verify_active_generation", return_value=self.record),
            mock.patch.object(recovery.render_operator, "load_env", return_value=self.env),
            mock.patch.object(recovery, "workspace_reconstructible_plan", return_value={}),
            mock.patch.object(
                recovery.subprocess, "check_output", side_effect=AssertionError("Git must not be queried")
            ),
        ):
            return recovery.load_backup_context(self.config)

    def test_non_git_archive_loads_without_runtime_manifest(self):
        context = self.load()
        self.assertEqual(context.archive_root, self.archive)
        self.assertEqual(context.source_code, {"commit": "a" * 40, "tree": "b" * 40, "worktree": "release-archive"})
        self.assertFalse((self.runtime / "release.json").exists())

    def test_archive_copy_uses_assets_but_records_runtime_destination(self):
        context = self.load()
        result = recovery.copy_release_tree(context, self.root / "staging")
        self.assertEqual(result["path"], str(self.runtime))
        self.assertEqual((self.root / "staging/release/release.json").read_bytes(), self.manifest.read_bytes())
        self.assertFalse((self.runtime / "release.json").exists())

    def test_wrong_active_release_is_rejected(self):
        self.record["release"]["source_commit"] = "d" * 40
        with self.assertRaises(recovery.RecoveryError):
            self.load()

    def test_invalid_or_conflicting_source_tree_is_rejected(self):
        for value in (None, "bad", "A" * 40, "d" * 40):
            with self.subTest(value=value):
                self.raw["source_tree"] = value
                self.save_manifest()
                with self.assertRaises((recovery.RecoveryError, SystemExit)):
                    self.load()

    def test_changed_asset_or_deployment_file_is_rejected(self):
        self.wheel.write_bytes(self.wheel.read_bytes() + b"changed")
        with self.assertRaises((recovery.RecoveryError, SystemExit)):
            self.load()

    def test_changed_deployment_member_is_rejected(self):
        self.script.write_bytes(self.script.read_bytes() + b"\n# changed\n")
        with self.assertRaises(recovery.RecoveryError):
            self.load()

    def test_changed_active_artifact_identity_is_rejected(self):
        self.record["release"]["artifacts"][0]["sha256"] = "0" * 64
        with self.assertRaises(recovery.RecoveryError):
            self.load()

    def test_missing_archive_manifest_does_not_fall_back_to_runtime(self):
        (self.runtime / "release.json").write_bytes(self.manifest.read_bytes())
        self.manifest.unlink()
        with self.assertRaises(recovery.RecoveryError):
            self.load()

    def test_symlinked_archive_asset_is_rejected(self):
        target = self.root / "wheel-copy"
        self.wheel.rename(target)
        self.wheel.symlink_to(target)
        with self.assertRaises(recovery.RecoveryError):
            self.load()
