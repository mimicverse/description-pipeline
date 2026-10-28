"""First-install record ownership and identity tests."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from description_pipeline.delivery import PIPELINE_ID
from description_pipeline.pipeline.contracts import NativeTargetIdentity

from .contract_wheel import write_contract_wheel
from .test_interface_records import config_snapshot, health as interface_health
from .test_interface_records import install_record as interface_install_record

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


def windows_health(record: dict, *, ready: bool = True) -> dict:
    windows = record["ends"]["windows"]
    document = interface_health(ready)
    document.update(
        release=windows["release"],
        install_id=windows["install_id"],
        config_digest=windows["config_digest"],
        target_identity=windows["target_identity"],
        endpoint={"port": config_snapshot()["resolved"]["windows"]["endpoint_port"]},
    )
    return document


def running_windows_identity(record: dict) -> dict:
    return {
        **record["ends"]["windows"],
        "port": config_snapshot()["resolved"]["windows"]["endpoint_port"],
    }


class InstallRecordTests(unittest.TestCase):
    def test_linux_wheelhouse_requires_release_bytes_and_confined_members(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle = root / "description-1.0.0-linux-cp312-x86_64.zip"
            release_file = root / "release.json"
            destination = root / "wheels"
            destination.mkdir()

            def bind(members):
                with zipfile.ZipFile(bundle, "w") as archive:
                    for name, payload in members.items():
                        archive.writestr(name, payload)
                release_file.write_text(json.dumps({
                    "schema_version": install_record.RELEASE_SCHEMA,
                    "version": "1.0.0", "source_commit": COMMIT, "source_tree": "b" * 40,
                    "source_sha256": SOURCE_SHA,
                    "artifacts": {bundle.name: digest(bundle.read_bytes())},
                }))

            bind({"wheels/dependency-1.0-py3-none-any.whl": b"wheel", "README.md": b"instructions"})
            original = bundle.read_bytes()
            bundle.write_bytes(original + b"tampered")
            with self.assertRaisesRegex(SystemExit, "differs from release.json"):
                install_record.extract_linux_wheelhouse(release_file, destination)
            self.assertEqual(list(destination.iterdir()), [])
            bundle.write_bytes(original)
            install_record.extract_linux_wheelhouse(release_file, destination)
            self.assertEqual({p.name: p.read_bytes() for p in destination.iterdir()},
                             {"dependency-1.0-py3-none-any.whl": b"wheel"})
            with self.assertRaisesRegex(SystemExit, "empty ordinary directory"):
                install_record.extract_linux_wheelhouse(release_file, destination)
            for path in destination.iterdir():
                path.unlink()
            bind({"wheels/../../escape.whl": b"wheel"})
            with self.assertRaisesRegex(SystemExit, "escapes"):
                install_record.extract_linux_wheelhouse(release_file, destination)
            self.assertFalse((root / "escape.whl").exists())

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

    def test_deployment_tree_is_the_exact_release_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            deployment, release_document, fixture = self._fixture(root)
            self.assertEqual(
                install_record.verify_deployment_tree(deployment, release_document),
                1,
            )
            fixture["dag"].write_bytes(b"# changed\n")
            with self.assertRaisesRegex(SystemExit, "deployment members differ"):
                install_record.verify_deployment_tree(deployment, release_document)
            fixture["dag"].write_bytes(b"# dag\n")
            extra = deployment / "deploy" / "operator" / "unbound.py"
            extra.write_text("UNBOUND = True\n", encoding="utf-8")
            with self.assertRaisesRegex(SystemExit, "deployment tree members differ"):
                install_record.verify_deployment_tree(deployment, release_document)
            extra.unlink()
            linked_source = root / "linked-source.py"
            linked_source.write_bytes(fixture["dag"].read_bytes())
            fixture["dag"].unlink()
            fixture["dag"].symlink_to(linked_source)
            with self.assertRaisesRegex(SystemExit, "crosses a link"):
                install_record.verify_deployment_tree(deployment, release_document)

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

    def test_windows_result_reader_preserves_last_line_and_unknown_outcome(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = root / "windows-result.out"
            document = {
                "schema_version": install_record.WINDOWS_RESULT_SCHEMA,
                "passed": True,
                "verb": "status",
            }
            result.write_text(
                "transport diagnostic\n" + json.dumps(document) + "\n",
                encoding="utf-8",
            )
            self.assertEqual(install_record.load_windows_result(result), document)
            result.write_text("not json\n", encoding="utf-8")
            self.assertIsNone(install_record.load_windows_result(result))
            self.assertEqual(
                install_record.verify_windows_result(
                    {"verb": "status"},
                    remote_rc=2,
                    verb="status",
                    record_path=None,
                ),
                3,
            )

    def test_windows_result_validator_accepts_current_producer_shapes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            record = interface_install_record()
            record_path = root / "install-record.json"
            record_path.write_text(json.dumps(record), encoding="utf-8")
            identity = running_windows_identity(record)

            start = {
                "schema_version": install_record.WINDOWS_RESULT_SCHEMA,
                "passed": True,
                "verb": "start",
                "task": {"state": "Running"},
                "identity": identity,
                "health": windows_health(record),
                "listener": {"listener_pid": 42},
            }
            result_path = root / "windows-start.json"
            result_path.write_text(json.dumps(start), encoding="utf-8")
            command = [
                sys.executable, str(OPERATOR / "install_record.py"),
                "verify-windows-result", "--result", str(result_path),
                "--remote-rc", "0", "--verb", "start",
                "--install-record", str(record_path),
            ]
            environment = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
            result = subprocess.run(command, env=environment, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            drifted = copy.deepcopy(start)
            drifted["health"]["endpoint"]["port"] += 1
            result_path.write_text(json.dumps(drifted), encoding="utf-8")
            result = subprocess.run(command, env=environment, capture_output=True, text=True)
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertEqual(
                install_record.verify_windows_result(
                    start,
                    remote_rc=0,
                    verb="start",
                    record_path=record_path,
                ),
                0,
            )

            drain = copy.deepcopy(start)
            drain["verb"] = "drain"
            drain["health"] = windows_health(record, ready=False)
            drain["safety"] = {
                "jobs": {"active": 0, "queued": 0, "unresolved": 0},
                "cad": {
                    "external_session_detected": False,
                    "owned_sessions": 0,
                    "lease_held": False,
                    "solidworks_processes": 0,
                },
                "listener_pid": 42,
                "task_state": "Running",
            }
            self.assertEqual(
                install_record.verify_windows_result(
                    drain,
                    remote_rc=0,
                    verb="drain",
                    record_path=record_path,
                ),
                0,
            )

            stopped = {
                "listener_count": 0,
                "writer_count": 0,
                "solidworks_process_count": 0,
            }
            stop_from_running = copy.deepcopy(start)
            stop_from_running["verb"] = "stop"
            stop_from_running["task"] = {"state": "Disabled"}
            stop_from_running["health"] = None
            stop_from_running["listener"] = None
            stop_from_running["safety"] = {
                "jobs": {"active": 0, "queued": 0, "unresolved": 0},
                "cad": {
                    "external_session_detected": False,
                    "owned_sessions": 0,
                    "lease_held": False,
                    "solidworks_processes": 0,
                },
                "listener_pid": 42,
                "task_state": "Running",
            }
            stop_from_running["stopped"] = stopped
            self.assertEqual(
                install_record.verify_windows_result(
                    stop_from_running,
                    remote_rc=0,
                    verb="stop",
                    record_path=record_path,
                ),
                0,
            )

            stop_already_stopped = copy.deepcopy(stop_from_running)
            stop_already_stopped["identity"] = {
                key: value for key, value in identity.items() if key != "target_identity"
            }
            stop_already_stopped["safety"] = {
                "jobs": None,
                "cad": {
                    "external_session_detected": False,
                    "owned_sessions": 0,
                    "lease_held": False,
                    "solidworks_processes": 0,
                },
                "listener_pid": None,
                "task_state": "Disabled",
                "source": "already_stopped",
            }
            self.assertEqual(
                install_record.verify_windows_result(
                    stop_already_stopped,
                    remote_rc=0,
                    verb="stop",
                    record_path=record_path,
                ),
                0,
            )

    def test_windows_result_validator_refuses_identity_and_outcome_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            record = interface_install_record()
            record_path = root / "install-record.json"
            record_path.write_text(json.dumps(record), encoding="utf-8")
            identity = running_windows_identity(record)
            document = {
                "schema_version": install_record.WINDOWS_RESULT_SCHEMA,
                "passed": True,
                "verb": "start",
                "task": {"state": "Running"},
                "identity": identity,
                "health": windows_health(record),
                "listener": {"listener_pid": 42},
            }
            mismatch = copy.deepcopy(document)
            mismatch["identity"]["install_id"] = "win-" + "8" * 32
            self.assertEqual(
                install_record.verify_windows_result(
                    mismatch,
                    remote_rc=0,
                    verb="start",
                    record_path=record_path,
                ),
                1,
            )

            for name, mutate in {
                "contract digest": lambda value: value["health"]["contract"].__setitem__(
                    "digest", "0" * 64
                ),
                "policy digest": lambda value: value["health"]["resource_policy"].__setitem__(
                    "digest", "0" * 64
                ),
                "endpoint port": lambda value: value["health"]["endpoint"].__setitem__("port", 9999),
            }.items():
                with self.subTest(drift=name):
                    drifted = copy.deepcopy(document)
                    mutate(drifted)
                    self.assertEqual(
                        install_record.verify_windows_result(
                            drifted,
                            remote_rc=0,
                            verb="start",
                            record_path=record_path,
                        ),
                        1,
                    )

            refused = {
                "schema_version": install_record.WINDOWS_RESULT_SCHEMA,
                "passed": False,
                "verb": "start",
                "code": "start_failed",
            }
            self.assertEqual(
                install_record.verify_windows_result(
                    refused,
                    remote_rc=1,
                    verb="start",
                    record_path=record_path,
                ),
                1,
            )
            self.assertEqual(
                install_record.verify_windows_result(
                    refused,
                    remote_rc=2,
                    verb="start",
                    record_path=record_path,
                ),
                3,
            )

    def test_windows_result_validator_refuses_incomplete_lifecycle_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            record = interface_install_record()
            record_path = root / "install-record.json"
            record_path.write_text(json.dumps(record), encoding="utf-8")
            identity = running_windows_identity(record)
            start = {
                "schema_version": install_record.WINDOWS_RESULT_SCHEMA,
                "passed": True,
                "verb": "start",
                "task": {"state": "Running"},
                "identity": identity,
                "health": windows_health(record),
                "listener": {"listener_pid": 42},
            }
            drain = copy.deepcopy(start)
            drain["verb"] = "drain"
            drain["health"] = windows_health(record, ready=False)
            drain["safety"] = {
                "jobs": {"active": 0, "queued": 0, "unresolved": 0},
                "cad": {
                    "external_session_detected": False,
                    "owned_sessions": 0,
                    "lease_held": False,
                    "solidworks_processes": 0,
                },
                "listener_pid": 42,
                "task_state": "Running",
            }
            stopped = {
                "listener_count": 0,
                "writer_count": 0,
                "solidworks_process_count": 0,
            }
            stop_from_running = {
                "schema_version": install_record.WINDOWS_RESULT_SCHEMA,
                "passed": True,
                "verb": "stop",
                "task": {"state": "Disabled"},
                "identity": identity,
                "listener": None,
                "health": None,
                "safety": copy.deepcopy(drain["safety"]),
                "stopped": stopped,
            }
            stop_already_stopped = copy.deepcopy(stop_from_running)
            stop_already_stopped["identity"] = {
                key: value for key, value in identity.items() if key != "target_identity"
            }
            stop_already_stopped["safety"].update(
                jobs=None,
                listener_pid=None,
                task_state="Disabled",
                source="already_stopped",
            )

            cases = (
                ("start-task", start, "start", lambda value: value["task"].update(state="Ready")),
                ("start-listener", start, "start", lambda value: value.update(listener={})),
                ("drain-task", drain, "drain", lambda value: value["task"].update(state="Ready")),
                ("drain-listener", drain, "drain", lambda value: value.update(listener=None)),
                ("drain-safety", drain, "drain", lambda value: value.pop("safety")),
                (
                    "drain-jobs",
                    drain,
                    "drain",
                    lambda value: value["safety"]["jobs"].update(unresolved=1),
                ),
                (
                    "drain-solidworks",
                    drain,
                    "drain",
                    lambda value: value["safety"]["cad"].update(solidworks_processes=1),
                ),
                (
                    "stop-running-jobs",
                    stop_from_running,
                    "stop",
                    lambda value: value["safety"].update(jobs=None),
                ),
                (
                    "stop-running-listener",
                    stop_from_running,
                    "stop",
                    lambda value: value["safety"].update(listener_pid=None),
                ),
                (
                    "stop-final-task",
                    stop_from_running,
                    "stop",
                    lambda value: value["task"].update(state="Running"),
                ),
                (
                    "stop-final-task-ready",
                    stop_from_running,
                    "stop",
                    lambda value: value["task"].update(state="Ready"),
                ),
                (
                    "stop-residue",
                    stop_from_running,
                    "stop",
                    lambda value: value["stopped"].update(writer_count=1),
                ),
                (
                    "already-stopped-jobs",
                    stop_already_stopped,
                    "stop",
                    lambda value: value["safety"].update(
                        jobs={"active": 0, "queued": 0, "unresolved": 0}
                    ),
                ),
            )
            for name, document, verb, mutate in cases:
                with self.subTest(case=name):
                    candidate = copy.deepcopy(document)
                    mutate(candidate)
                    self.assertEqual(
                        install_record.verify_windows_result(
                            candidate,
                            remote_rc=0,
                            verb=verb,
                            record_path=record_path,
                        ),
                        1,
                    )


if __name__ == "__main__":
    unittest.main()
