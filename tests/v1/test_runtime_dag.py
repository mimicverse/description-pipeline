"""Runtime DAG identity checks shared by health and promotion verification."""

from __future__ import annotations

import hashlib
import importlib.util
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
OPERATOR = ROOT / "deploy" / "operator"
SPEC = importlib.util.spec_from_file_location("deploy_runtime_dag", OPERATOR / "runtime_dag.py")
runtime_dag = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(runtime_dag)


class RuntimeDagTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="runtime-dag-")
        self.root = Path(self.temp.name)
        self.dag = self.root / runtime_dag.DAG_ID / "solidworks_to_urdf.py"
        self.dag.parent.mkdir(parents=True)
        self.dag.write_text("# current dag\n", encoding="utf-8")
        self.digest = hashlib.sha256(self.dag.read_bytes()).hexdigest()
        self.airflow_home = self.root / "airflow-home"
        self.airflow_home.mkdir()
        self.cfg = self.airflow_home / "airflow.cfg"
        self.write_config(self.dag.parent)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write_config(self, dags_folder: Path) -> None:
        self.cfg.write_text(
            "# managed-by: description-airflow\n"
            "[core]\n"
            f"dags_folder = {dags_folder}\n",
            encoding="utf-8",
        )
        self.cfg.chmod(0o600)

    def record(self) -> dict:
        return {
            "install_id": "gen-1",
            "release": {"source_commit": "c" * 40},
            "dag": {
                "id": runtime_dag.DAG_ID,
                "path": str(self.dag),
                "digest": self.digest,
            },
        }

    def test_active_dag_and_config_bind_the_same_effective_file(self) -> None:
        path, digest = runtime_dag.active_dag(self.record())
        folder, _ = runtime_dag.configured_dags_folder(self.airflow_home)
        self.assertEqual(path, self.dag.resolve())
        self.assertEqual(digest, self.digest)
        self.assertEqual(folder, self.dag.parent.resolve())

    def test_dag_digest_and_config_folder_mismatches_fail(self) -> None:
        record = self.record()
        record["dag"]["digest"] = "0" * 64
        with self.assertRaisesRegex(RuntimeError, "digest differs"):
            runtime_dag.active_dag(record)
        self.write_config(self.root / "historical")
        (self.root / "historical").mkdir()
        with (
            mock.patch.object(runtime_dag, "read_installed_generation", return_value=(self.record(), {})),
            mock.patch.object(runtime_dag, "effective_dags_folder", return_value=self.root / "historical"),
            self.assertRaisesRegex(RuntimeError, "does not point at"),
        ):
            runtime_dag.check(self.root / "state", self.airflow_home, files_only=True)

    def test_local_bundle_name_and_null_version_are_explicit(self) -> None:
        runtime_dag.require_local_bundle("dags-folder", None)
        with self.assertRaisesRegex(RuntimeError, "bundle name"):
            runtime_dag.require_local_bundle("other", None)
        with self.assertRaisesRegex(RuntimeError, "null version"):
            runtime_dag.require_local_bundle("dags-folder", "v1")

    def serialized_fixture(self):
        version = SimpleNamespace(
            dag_id=runtime_dag.DAG_ID,
            bundle_name="dags-folder",
            bundle_version=None,
            id="version-uuid",
            version_number=3,
        )
        row = SimpleNamespace(
            dag_id=runtime_dag.DAG_ID,
            dag_version_id="version-uuid",
            dag_hash="a" * 32,
            data={
                "dag": {
                    "dag_id": runtime_dag.DAG_ID,
                    "fileloc": str(self.dag.resolve()),
                    "_processor_dags_folder": str(self.dag.parent.resolve()),
                }
            },
        )
        return version, row

    def test_serialized_identity_binds_one_version_one_row_and_one_folder(self) -> None:
        version, row = self.serialized_fixture()
        result = runtime_dag.serialized_identity(
            runtime_dag.DAG_ID,
            version,
            [row],
            dag_path=self.dag.resolve(),
            dags_folder=self.dag.parent.resolve(),
        )
        self.assertEqual(result["dag_version_id"], "version-uuid")
        self.assertEqual(result["dag_hash"], "a" * 32)

    def test_serialized_identity_refuses_a_foreign_version_or_bundle(self) -> None:
        version, row = self.serialized_fixture()
        version.dag_id = "other_dag"
        with self.assertRaisesRegex(RuntimeError, "version belongs to another DAG"):
            runtime_dag.serialized_identity(
                runtime_dag.DAG_ID,
                version,
                [row],
                dag_path=self.dag.resolve(),
                dags_folder=self.dag.parent.resolve(),
            )
        version, row = self.serialized_fixture()
        version.bundle_name = "git"
        with self.assertRaisesRegex(RuntimeError, "bundle name"):
            runtime_dag.serialized_identity(
                runtime_dag.DAG_ID,
                version,
                [row],
                dag_path=self.dag.resolve(),
                dags_folder=self.dag.parent.resolve(),
            )
        version, row = self.serialized_fixture()
        version.bundle_version = "v1"
        with self.assertRaisesRegex(RuntimeError, "null version"):
            runtime_dag.serialized_identity(
                runtime_dag.DAG_ID,
                version,
                [row],
                dag_path=self.dag.resolve(),
                dags_folder=self.dag.parent.resolve(),
            )

    def test_serialized_identity_refuses_a_wrong_row_version_or_dag(self) -> None:
        version, row = self.serialized_fixture()
        row.dag_version_id = "older-version"
        with self.assertRaisesRegex(RuntimeError, "version differs from the current DagVersion"):
            runtime_dag.serialized_identity(
                runtime_dag.DAG_ID,
                version,
                [row],
                dag_path=self.dag.resolve(),
                dags_folder=self.dag.parent.resolve(),
            )
        version, row = self.serialized_fixture()
        row.dag_id = "other_dag"
        with self.assertRaisesRegex(RuntimeError, "row belongs to another DAG"):
            runtime_dag.serialized_identity(
                runtime_dag.DAG_ID,
                version,
                [row],
                dag_path=self.dag.resolve(),
                dags_folder=self.dag.parent.resolve(),
            )
        version, row = self.serialized_fixture()
        with self.assertRaisesRegex(RuntimeError, "exactly one serialized DAG"):
            runtime_dag.serialized_identity(
                runtime_dag.DAG_ID,
                version,
                [row, row],
                dag_path=self.dag.resolve(),
                dags_folder=self.dag.parent.resolve(),
            )

    def test_serialized_identity_refuses_a_historical_file_or_processor_folder(self) -> None:
        historical = self.root / "historical" / "solidworks_to_urdf.py"
        historical.parent.mkdir()
        historical.write_text("# historical\n", encoding="utf-8")
        version, row = self.serialized_fixture()
        row.data["dag"]["fileloc"] = str(historical)
        with self.assertRaisesRegex(RuntimeError, "fileloc differs"):
            runtime_dag.serialized_identity(
                runtime_dag.DAG_ID,
                version,
                [row],
                dag_path=self.dag.resolve(),
                dags_folder=self.dag.parent.resolve(),
            )
        version, row = self.serialized_fixture()
        row.data["dag"]["_processor_dags_folder"] = str(historical.parent)
        with self.assertRaisesRegex(RuntimeError, "processor folder differs"):
            runtime_dag.serialized_identity(
                runtime_dag.DAG_ID,
                version,
                [row],
                dag_path=self.dag.resolve(),
                dags_folder=self.dag.parent.resolve(),
            )

    def test_serialized_identity_refuses_malformed_or_hashless_rows(self) -> None:
        version, row = self.serialized_fixture()
        row.data["dag"]["dag_id"] = "other_dag"
        with self.assertRaisesRegex(RuntimeError, "serialized DAG id differs"):
            runtime_dag.serialized_identity(
                runtime_dag.DAG_ID,
                version,
                [row],
                dag_path=self.dag.resolve(),
                dags_folder=self.dag.parent.resolve(),
            )
        version, row = self.serialized_fixture()
        row.dag_hash = ""
        with self.assertRaisesRegex(RuntimeError, "hash is missing"):
            runtime_dag.serialized_identity(
                runtime_dag.DAG_ID,
                version,
                [row],
                dag_path=self.dag.resolve(),
                dags_folder=self.dag.parent.resolve(),
            )
        version, row = self.serialized_fixture()
        row.data = []
        with self.assertRaisesRegex(RuntimeError, "serialized DAG is malformed"):
            runtime_dag.serialized_identity(
                runtime_dag.DAG_ID,
                version,
                [row],
                dag_path=self.dag.resolve(),
                dags_folder=self.dag.parent.resolve(),
            )

    def test_files_only_check_does_not_require_serialized_state(self) -> None:
        with (
            mock.patch.object(runtime_dag, "read_installed_generation", return_value=(self.record(), {})),
            mock.patch.object(
                runtime_dag,
                "effective_dags_folder",
                return_value=self.dag.parent.resolve(),
            ),
        ):
            result = runtime_dag.check(self.root / "state", self.airflow_home, files_only=True)
        self.assertEqual(result["dag_path"], str(self.dag.resolve()))
        self.assertNotIn("serialized", result)

    def test_full_check_returns_registered_version_identity(self) -> None:
        serialized = {
            "bundle_name": "dags-folder",
            "bundle_version": None,
            "dag_hash": "a" * 32,
            "dag_version_id": "version-1",
            "dag_version_number": 3,
            "fileloc": str(self.dag.resolve()),
            "processor_dags_folder": str(self.dag.parent.resolve()),
        }
        with (
            mock.patch.object(runtime_dag, "read_installed_generation", return_value=(self.record(), {})),
            mock.patch.object(runtime_dag, "serialized_dag", return_value=serialized),
            mock.patch.object(
                runtime_dag,
                "effective_dags_folder",
                return_value=self.dag.parent.resolve(),
            ),
        ):
            result = runtime_dag.check(self.root / "state", self.airflow_home)
        self.assertEqual(result["serialized"], serialized)

    def test_airflow_config_environment_overrides_are_rejected(self) -> None:
        for key in runtime_dag.FORBIDDEN_CONFIG_ENV:
            with (
                self.subTest(key=key),
                mock.patch.dict(runtime_dag.os.environ, {key: "/tmp/other"}, clear=False),
                self.assertRaisesRegex(RuntimeError, "must not override"),
            ):
                runtime_dag.effective_dags_folder(self.airflow_home)


if __name__ == "__main__":
    unittest.main()
