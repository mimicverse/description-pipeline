"""First-install record ownership and identity tests."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from description_pipeline.delivery import PIPELINE_ID
from description_pipeline.pipeline.contracts import NativeTargetIdentity

from .contract_wheel import write_contract_wheel

ROOT = Path(__file__).resolve().parents[2]
OPERATOR = ROOT / "deploy" / "operator"


def load_deploy_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


platform_config = load_deploy_module("deploy_platform_config_ir", OPERATOR / "platform_config.py")
install_record = load_deploy_module("deploy_install_record", OPERATOR / "install_record.py")

TIMESTAMP = "2026-10-27T00:00:00Z"
COMMIT = "a" * 40
SOURCE_SHA = "c" * 64
WHEEL_NAME = "mimicverse_description-1.0.0-py3-none-any.whl"
SERVICES = ["description-postgres", "description-airflow-scheduler", "description-operator-proxy"]


def digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def native_target(install_id: str = "win-" + "7" * 32) -> dict:
    return NativeTargetIdentity.create(
        install_id=install_id,
        pipeline_id=PIPELINE_ID,
    ).to_record()


def disabled_status(release: dict, install_id: str) -> dict:
    return {
        "schema_version": "solidworks-to-urdf.windows-lifecycle-result/v1",
        "passed": True,
        "verb": "status",
        "task": {
            "task_name": "description-e2e-endpoint",
            "task_path": "\\URDF\\",
            "state": "Disabled",
        },
        "identity": {
            "install_id": install_id,
            "config_digest": "d" * 64,
            "release": {**release, "role": "native"},
        },
        "listener": None,
        "health": None,
        "status": {
            "consistent": True,
            "task_exists": True,
            "task_state": "Disabled",
            "error": None,
            "solidworks_processes": 0,
        },
    }


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
            "tenant_keys": ["tenant-key"],
            "admin_open_ids": [],
        },
        "targets": {"m3.0": {"repository_slug": "owner/model-repository", "base": "main"}},
        "specifications": {"record_roots": [str(root / "specifications")]},
    }


def contract_owner(root: Path):
    wheel = root / "distribution" / WHEEL_NAME
    if not wheel.is_file():
        wheel.parent.mkdir(parents=True, exist_ok=True)
        write_contract_wheel(wheel)
    return platform_config.load_contract_owner(platform_config.extract_contract_closure(wheel))


def make_snapshot(root: Path) -> Path:
    resolved = platform_config.validate_platform(platform_document(root))
    snapshot = platform_config.build_snapshot(
        resolved,
        platform_json_sha256="e" * 64,
        install_id="gen-1",
        generated_at=TIMESTAMP,
    )
    return platform_config.write_snapshot(
        snapshot,
        root / "state" / "snapshots",
        contract_owner(root),
    )


def make_dag(root: Path) -> Path:
    path = root / install_record.DAG_MEMBER
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# dag fixture\n", encoding="utf-8")
    return path


def make_release(root: Path) -> Path:
    distribution = root / "distribution"
    distribution.mkdir(parents=True, exist_ok=True)
    wheel = distribution / WHEEL_NAME
    write_contract_wheel(wheel)
    dag = make_dag(root)
    document = {
        "schema_version": install_record.RELEASE_SCHEMA,
        "pipeline_id": PIPELINE_ID,
        "version": "1.0.0",
        "source_commit": COMMIT,
        "source_tree": "b" * 40,
        "source_sha256": SOURCE_SHA,
        "artifacts": {WHEEL_NAME: digest(wheel.read_bytes())},
        "deployment_files": {install_record.DAG_MEMBER: digest(dag.read_bytes())},
    }
    path = distribution / "release.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def make_endpoint_identity(root: Path) -> Path:
    target = native_target()
    document = {
        "install_id": target["install_id"],
        "config_digest": "d" * 64,
        "release": {
            "version": "1.0.0",
            "source_commit": COMMIT,
            "source_sha256": SOURCE_SHA,
            "role": "native",
        },
        "target_identity": target,
    }
    path = root / "endpoint-identity.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def build_record(
    root: Path,
    *,
    install_id: str = "gen-1",
    snapshot_path: Path | None = None,
) -> dict:
    release_document = json.loads((root / "distribution" / "release.json").read_text(encoding="utf-8"))
    release = install_record.release_identity(release_document)
    endpoint = install_record.endpoint_identity(
        json.loads((root / "endpoint-identity.json").read_text(encoding="utf-8"))
    )
    return install_record.build_install_record(
        install_id=install_id,
        timestamp=TIMESTAMP,
        snapshot_path=snapshot_path or root / "state" / "snapshots" / "gen-1.json",
        release=release,
        endpoint=endpoint,
        services=list(SERVICES),
        dag_file=root / install_record.DAG_MEMBER,
        deployment_root=root,
        release_dag_sha256=install_record.release_dag_digest(release_document),
    )


def record_document(root: Path, *, install_id: str = "gen-1") -> dict:
    return build_record(root, install_id=install_id)


class InstallRecordTests(unittest.TestCase):
    def _fixture(self, root: Path) -> tuple[Path, dict, dict]:
        deployment = root / "release"
        (deployment / "deploy" / "operator").mkdir(parents=True)
        (deployment / "deploy" / "airflow" / "dags").mkdir(parents=True)
        dag = deployment / install_record.DAG_MEMBER
        dag.write_bytes(b"# dag\n")
        wheel = deployment / "mimicverse_description-1.0.0-py3-none-any.whl"
        wheel.write_bytes(b"wheel")
        windows = deployment / "description-1.0.0-windows-cp312-x86_64.zip"
        windows.write_bytes(b"windows")
        release_document = {
            "schema_version": install_record.RELEASE_SCHEMA,
            "version": "1.0.0",
            "source_commit": "a" * 40,
            "source_tree": "b" * 40,
            "source_sha256": "c" * 64,
            "artifacts": {
                wheel.name: digest(wheel.read_bytes()),
                windows.name: digest(windows.read_bytes()),
            },
            "deployment_files": {
                install_record.DAG_MEMBER: digest(dag.read_bytes()),
            },
        }
        release_file = deployment / "release.json"
        release_file.write_text(json.dumps(release_document), encoding="utf-8")
        snapshot = root / "state" / "snapshots" / "gen-1.json"
        snapshot.parent.mkdir(parents=True)
        snapshot_document = {
            "schema_version": install_record.SNAPSHOT_SCHEMA,
            "install_id": "gen-1",
            "resolved": {
                "storage": {
                    "state_root": str(root / "state"),
                    "handoff_root": str(root / "handoffs"),
                    "artifact_root": str(root / "artifacts"),
                    "workspace_root": str(root / "workspaces"),
                    "release_root": str(root / "runtime"),
                }
            },
        }
        snapshot.write_text(json.dumps(snapshot_document), encoding="utf-8")
        target = NativeTargetIdentity.create(
            install_id="win-" + "1" * 32,
            pipeline_id=PIPELINE_ID,
        ).to_record()
        endpoint = {
            "install_id": target["install_id"],
            "config_digest": "d" * 64,
            "release": {
                "version": "1.0.0",
                "source_commit": "a" * 40,
                "source_sha256": "c" * 64,
                "role": "native",
            },
            "target_identity": target,
        }
        return deployment, release_document, {
            "snapshot": snapshot,
            "endpoint": endpoint,
            "dag": dag,
        }

    def test_builds_only_the_first_install_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            deployment, release_document, fixture = self._fixture(root)
            record = install_record.build_install_record(
                install_id="gen-1",
                timestamp="2026-10-27T00:00:00Z",
                snapshot_path=fixture["snapshot"],
                release=install_record.release_identity(release_document),
                endpoint=fixture["endpoint"],
                services=["description-postgres", "description-operator-proxy"],
                dag_file=fixture["dag"],
                deployment_root=deployment,
                release_dag_sha256=digest(fixture["dag"].read_bytes()),
            )
            self.assertNotIn("previous", record)
            self.assertNotIn("journal", record)
            self.assertEqual(record["ends"]["windows"], fixture["endpoint"])
            self.assertEqual(
                record["snapshot"]["digest"],
                digest(fixture["snapshot"].read_bytes()),
            )

    def test_endpoint_must_share_the_current_release(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, release_document, fixture = self._fixture(root)
            fixture["endpoint"]["release"]["source_commit"] = "e" * 40
            with self.assertRaises(SystemExit):
                install_record.build_install_record(
                    install_id="gen-1",
                    timestamp="2026-10-27T00:00:00Z",
                    snapshot_path=fixture["snapshot"],
                    release=install_record.release_identity(release_document),
                    endpoint=fixture["endpoint"],
                    services=["description-postgres"],
                    dag_file=fixture["dag"],
                    deployment_root=Path(temporary) / "release",
                    release_dag_sha256=digest(fixture["dag"].read_bytes()),
                )

    def test_installed_status_identity_derives_the_canonical_target(self) -> None:
        install_id = "win-" + "9" * 32
        release = {
            "version": "1.0.0",
            "source_commit": COMMIT,
            "source_sha256": SOURCE_SHA,
            "artifacts": [],
        }
        status = disabled_status(release, install_id)
        endpoint = install_record.endpoint_identity_from_status(status, release)
        self.assertEqual(endpoint["install_id"], install_id)
        self.assertEqual(endpoint["config_digest"], "d" * 64)
        self.assertEqual(
            endpoint["target_identity"],
            NativeTargetIdentity.create(
                install_id=install_id,
                pipeline_id=PIPELINE_ID,
            ).to_record(),
        )

    def test_installed_status_identity_requires_a_stopped_consistent_endpoint(self) -> None:
        install_id = "win-" + "9" * 32
        release = {
            "version": "1.0.0",
            "source_commit": COMMIT,
            "source_sha256": SOURCE_SHA,
            "artifacts": [],
        }
        valid = disabled_status(release, install_id)
        mutations = {
            "running task": lambda value: value["task"].update(state="Running"),
            "task absent": lambda value: value["status"].update(task_exists=False),
            "task state differs": lambda value: value["status"].update(task_state="Running"),
            "status error": lambda value: value["status"].update(error={"code": "failed"}),
            "status inconsistent": lambda value: value["status"].update(consistent=False),
            "solidworks running": lambda value: value["status"].update(solidworks_processes=1),
            "listener present": lambda value: value.update(listener={"listener_pid": 42}),
            "health present": lambda value: value.update(health={"ready": False}),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                status = copy.deepcopy(valid)
                mutate(status)
                with self.assertRaises(SystemExit):
                    install_record.endpoint_identity_from_status(status, release)

    def test_release_wheel_is_staged_under_the_runtime_artifact_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            deployment, release_document, _ = self._fixture(root)
            release = install_record.release_identity(release_document)
            runtime = root / "runtime"
            stable = install_record.stage_release_wheel(
                release,
                release_root=runtime,
                wheel=None,
                tree_root=deployment,
            )
            self.assertEqual(stable, runtime / "artifacts" / WHEEL_NAME)
            self.assertEqual(stable.read_bytes(), (deployment / WHEEL_NAME).read_bytes())
            self.assertEqual(
                install_record.stage_release_wheel(
                    release,
                    release_root=runtime,
                    wheel=stable,
                ),
                stable,
            )

    def test_release_artifact_check_is_complete(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            deployment, release_document, _ = self._fixture(root)
            release = install_record.release_identity(release_document)
            self.assertEqual(
                install_record.verify_artifacts(deployment, release["artifacts"]),
                2,
            )
            (deployment / "description-1.0.0-windows-cp312-x86_64.zip").unlink()
            with self.assertRaises(SystemExit):
                install_record.verify_artifacts(deployment, release["artifacts"])

    def test_record_is_create_once_and_never_repoints(self) -> None:
        class Owner:
            @staticmethod
            def canonical(value):
                return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()

        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            record = {"install_id": "gen-1"}
            generation, active = install_record.write_install_record(record, state, Owner)
            self.assertTrue(generation.is_file())
            self.assertTrue(active.is_file())
            with self.assertRaises(SystemExit):
                install_record.write_install_record({"install_id": "gen-2"}, state, Owner)

    def test_verify_active_binds_config_snapshot_and_release(self) -> None:
        class Records:
            @staticmethod
            def validate_install_record(value):
                return value

            @staticmethod
            def validate_config_snapshot(value):
                return value

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            snapshot = state / "snapshots" / "gen-1.json"
            snapshot.parent.mkdir(parents=True)
            snapshot.write_text(
                json.dumps(
                    {
                        "install_id": "gen-1",
                        "sources": {"platform_json_sha256": "a" * 64},
                        "resolved": {"storage": {"state_root": str(state)}},
                    }
                ),
                encoding="utf-8",
            )
            config = root / "platform.json"
            config.write_bytes(b"config")
            digest_value = digest(snapshot.read_bytes())
            record = {
                "install_id": "gen-1",
                "snapshot": {
                    "path": str(snapshot),
                    "digest": digest_value,
                },
                "release": {
                    "version": "1.0.0",
                    "source_commit": "b" * 40,
                    "source_sha256": "c" * 64,
                },
            }
            (state / "install-record.json").write_text(json.dumps(record), encoding="utf-8")
            snapshot_document = json.loads(snapshot.read_text(encoding="utf-8"))
            snapshot_document["sources"]["platform_json_sha256"] = digest(config.read_bytes())
            snapshot.write_text(json.dumps(snapshot_document), encoding="utf-8")
            record["snapshot"]["digest"] = digest(snapshot.read_bytes())
            (state / "install-record.json").write_text(json.dumps(record), encoding="utf-8")
            with mock.patch.object(install_record, "_canonical_records", return_value=Records):
                verified = install_record.verify_active_generation(config, state)
            self.assertEqual(verified["install_id"], "gen-1")


if __name__ == "__main__":
    unittest.main()
