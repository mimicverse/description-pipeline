"""Opt-in real Airflow 3 proof for a production-admitted Freeze task.

The test creates immutable admission facts through
``ProductionAdmissionFactory`` and ``StageApplication``.  The real DAG
processor, scheduler, worker and API server then execute the registered Freeze
command from a clean wheel extraction.  PostgreSQL is the sole run authority.

Required environment:

* ``DESCRIPTION_SUPERVISED_AIRFLOW_DSN``: Airflow metadata DSN
* ``DESCRIPTION_SUPERVISED_PLATFORM_DSN``: platform-service DSN
* ``DESCRIPTION_SUPERVISED_AIRFLOW_VENV``: Airflow 3 virtual environment
* ``DESCRIPTION_SUPERVISED_WHEEL``: wheel built from this candidate
* ``DESCRIPTION_SUPERVISED_EVIDENCE_DIR``: persistent evidence parent

The caller owns PostgreSQL bootstrap and migrations.  Every service this test
starts is isolated, owned, and terminated here.  The scope is the Freeze
boundary; no native CAD, downstream stage, deployment, or release claim is
made.
"""

from __future__ import annotations

import contextlib
import copy
import hashlib
import json
import os
import platform
import shutil
import signal
import socket
import subprocess
import tempfile
import time
import unittest
import uuid
import zipfile
from base64 import urlsafe_b64encode
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

from description_pipeline.schemas import TOOL_SCHEMA

ROOT = Path(__file__).resolve().parents[2]
DAG_ID = "solidworks_to_urdf"
DAG_FILE = ROOT / "deploy" / "airflow" / "dags" / "solidworks_to_urdf.py"
SUPERVISED = {
    "airflow_dsn": os.environ.get("DESCRIPTION_SUPERVISED_AIRFLOW_DSN", "").strip(),
    "platform_dsn": os.environ.get("DESCRIPTION_SUPERVISED_PLATFORM_DSN", "").strip(),
    "venv": os.environ.get("DESCRIPTION_SUPERVISED_AIRFLOW_VENV", "").strip(),
    "wheel": os.environ.get("DESCRIPTION_SUPERVISED_WHEEL", "").strip(),
    "evidence": os.environ.get("DESCRIPTION_SUPERVISED_EVIDENCE_DIR", "").strip(),
}


def _free_port(exclude: set[int] | None = None) -> int:
    excluded = exclude or set()
    for _ in range(50):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        if port not in excluded:
            return port
    raise AssertionError("no free loopback port")


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git(cwd: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *arguments],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()


def _wheel_package_files(wheel_root: Path) -> dict[str, str]:
    package = wheel_root / "description_pipeline"
    return {
        path.relative_to(package).as_posix(): _sha256_file(path)
        for path in sorted(package.rglob("*"))
        if path.is_file()
        and "__pycache__" not in path.parts
        and path.name != "tool-release.json"
    }


def _initialize_repository(root: Path) -> Path:
    source = root / "repository-source"
    source.mkdir()
    _git(source, "init", "-b", "main")
    _git(source, "config", "user.name", "Supervised Fixture")
    _git(source, "config", "user.email", "supervised@example.test")
    (source / "README.md").write_text("supervised repository\n", encoding="utf-8")
    _git(source, "add", "README.md")
    _git(source, "commit", "-m", "seed")

    remote = root / "repository.git"
    _git(root, "init", "--bare", str(remote))
    _git(remote, "symbolic-ref", "HEAD", "refs/heads/main")
    _git(source, "remote", "add", "origin", str(remote))
    _git(source, "push", "-u", "origin", "main")

    checkout = root / "workspaces" / "model-repository"
    _git(root, "clone", str(remote), str(checkout))
    _git(checkout, "config", "user.name", "Supervised Fixture")
    _git(checkout, "config", "user.email", "supervised@example.test")
    return checkout


@unittest.skipUnless(
    all(SUPERVISED.values()),
    "DESCRIPTION_SUPERVISED_* isolation inputs are not configured",
)
class SupervisedFreezeIdentityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(tempfile.mkdtemp(prefix="supervised-freeze-"))
        cls.processes: list[subprocess.Popen] = []
        cls.logs: list[object] = []
        cls.service_log_dirs: list[Path] = []
        cls.stop_history: list[dict] = []
        cls._cleaned = False
        cls.airflow_home = cls.root / "airflow-home"
        cls.airflow_home.mkdir()
        cls.venv = Path(SUPERVISED["venv"])
        cls.wheel = Path(SUPERVISED["wheel"])
        cls.original_environ = dict(os.environ)
        cls.addClassCleanup(cls._cleanup)

        evidence_parent = Path(SUPERVISED["evidence"])
        if not evidence_parent.is_absolute():
            raise AssertionError("DESCRIPTION_SUPERVISED_EVIDENCE_DIR must be absolute")
        evidence_parent.mkdir(parents=True, exist_ok=True)
        cls.evidence_dir = (
            evidence_parent
            / f"supervised-{time.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
        )
        cls.evidence_dir.mkdir()
        if (
            cls.evidence_dir == cls.root
            or cls.root in cls.evidence_dir.parents
            or cls.evidence_dir in cls.root.parents
        ):
            raise AssertionError("evidence and temporary root must be disjoint")

        ports = {
            "api": _free_port(),
        }
        ports["worker_log"] = _free_port(set(ports.values()))
        ports["trigger_log"] = _free_port(set(ports.values()))
        cls.ports = ports

        from description_pipeline import __version__
        from description_pipeline.artifacts import ArtifactStore
        from description_pipeline.delivery import PIPELINE_ID
        from description_pipeline.interface_records import (
            check_policy_digest,
            resource_policy_v1_digest,
        )
        from description_pipeline.io import digest
        from description_pipeline.pipeline.application import StageApplication
        from description_pipeline.pipeline.contracts import ProtocolError
        from description_pipeline.pipeline.freeze import FreezeHandler
        from description_pipeline.pipeline.ingestion import (
            ProductionAdmissionFactory,
            ProductionAdmissionRequest,
            ProductionFreezeSuccessorRequest,
            ReceivedUploadReceipt,
        )
        from description_pipeline.pipeline.services import CanonicalStageServices, ClaimPolicy
        from description_pipeline.platform.facts import FactStore
        from description_pipeline.stages import CONTRACT_SHA256
        from tests.v1.config_identity_support import activate
        from tests.v1.test_install_record import platform_document

        cls.digest = staticmethod(digest)
        cls.ProtocolError = ProtocolError
        cls.ProductionAdmissionFactory = ProductionAdmissionFactory
        cls.ProductionAdmissionRequest = ProductionAdmissionRequest
        cls.ProductionFreezeSuccessorRequest = ProductionFreezeSuccessorRequest
        cls.ReceivedUploadReceipt = ReceivedUploadReceipt

        cls.wheel_root = cls.root / "wheel"
        with zipfile.ZipFile(cls.wheel) as archive:
            archive.extractall(cls.wheel_root)
        cls.package_init = cls.wheel_root / "description_pipeline" / "__init__.py"
        if not cls.package_init.is_file():
            raise AssertionError("the clean wheel carries no description_pipeline package")
        cls.package_sha256 = _sha256_file(cls.package_init)

        package_files = _wheel_package_files(cls.wheel_root)
        cls.portable_source_sha256 = digest(package_files)
        release = {
            "schema_version": "solidworks-to-urdf.release/v1",
            "pipeline_id": PIPELINE_ID,
            "version": __version__,
            "source_commit": "a" * 40,
            "source_tree": "b" * 40,
            "source_sha256": cls.portable_source_sha256,
            "package_files": package_files,
            "contract": {
                "schema_version": "solidworks-to-urdf.contract/v1",
                "stage": CONTRACT_SHA256,
                "check_policy": check_policy_digest(),
                "resource_policy": resource_policy_v1_digest(),
            },
            "support": {
                "python": "3.12",
                "targets": ["linux", "windows"],
            },
        }

        def tool_record(role: str, system: str) -> dict:
            return {
                "schema_version": TOOL_SCHEMA,
                "pipeline_id": PIPELINE_ID,
                "version": __version__,
                "source_sha256": cls.portable_source_sha256,
                "release": copy.deepcopy(release),
                "runtime": {
                    "role": role,
                    "system": system,
                    "python": platform.python_version(),
                    "machine": platform.machine(),
                    "packages": {},
                },
            }

        cls.portable_tool = tool_record("portable", "Linux")
        cls.native_tool = tool_record("native", "Windows")

        _initialize_repository(cls.root)
        cls._git_target = mock.patch(
            "description_pipeline.pipeline.ingestion.validate_git_remote",
            side_effect=lambda repository, _slug: _git(
                repository,
                "remote",
                "get-url",
                "origin",
            ),
        )
        cls._git_target.start()
        cls.addClassCleanup(cls._git_target.stop)
        record = cls.root / "specifications" / "joints.json"
        record.parent.mkdir(parents=True, exist_ok=True)
        record.write_text(
            '{"limits":{"lower":-1,"upper":1}}\n',
            encoding="utf-8",
        )
        cls.snapshot, cls.install_record = activate(
            cls.root,
            "supervised",
            platform_document(cls.root),
        )
        cls.snapshot_digest = _sha256_file(cls.snapshot)

        cls.upload_root = cls.root / "handoffs" / "supervised"
        cls.intake = cls.upload_root / "CAD"
        cls.intake.mkdir(parents=True)
        (cls.intake / "M3.0.3.SLDASM").write_bytes(b"assembly-bytes")
        (cls.intake / "bracket.SLDPRT").write_bytes(b"part-bytes")

        cls.artifacts = ArtifactStore(cls.root / "artifacts")

        def connect():
            import psycopg2

            return psycopg2.connect(SUPERVISED["platform_dsn"])

        cls.connect = staticmethod(connect)
        cls.facts = FactStore(connect, cls.artifacts)
        cls.facts.open_admission(
            reason="supervised production Freeze proof",
            actor="supervised-test",
        )
        cls.actor = "cli_supervised:tenant:operator"
        cls.stage_work = cls.root / "stage-work"
        cls.stage_work.mkdir()
        services = CanonicalStageServices(
            facts=cls.facts,
            artifacts=cls.artifacts,
            work_root=cls.stage_work,
            claim_policy=ClaimPolicy(
                owner_sha256=digest({"process": uuid.uuid4().hex}),
                executor_sha256=cls.portable_source_sha256,
                lease=timedelta(minutes=10),
                claim_id_factory=lambda: "supervised-claim-" + uuid.uuid4().hex,
            ),
            clock=lambda: datetime(2026, 10, 25, 10, tzinfo=UTC),
        )
        cls.application = StageApplication(
            services=services,
            facts=cls.facts,
            artifacts=cls.artifacts,
            handlers={"freeze": FreezeHandler()},
            builders={},
            clock=lambda: datetime(2026, 10, 25, 10, tzinfo=UTC),
        )
        cls.runs: dict[str, dict[str, object]] = {}
        cls.actual_identities: dict[str, dict] = {}
        cls._admit_run("fresh")
        cls._admit_run("parent")

        decoy = cls.root / "decoy-snapshot.json"
        decoy.write_text("{}\n", encoding="utf-8")
        database_connection = {
            "conn_type": "http",
            "host": "",
            "port": None,
            "extra": {
                "config_identity": {
                    "schema_version": "solidworks-to-urdf.platform-config-snapshot/v1",
                    "path": str(decoy),
                    "sha256": "0" * 64,
                }
            },
        }
        fernet = urlsafe_b64encode(
            hashlib.sha256(b"supervised-freeze-identity").digest()
        ).decode()
        token = cls.root / "secrets" / "endpoint.token"
        cls.env = dict(
            os.environ,
            AIRFLOW_HOME=str(cls.airflow_home),
            AIRFLOW__DATABASE__SQL_ALCHEMY_CONN=SUPERVISED["airflow_dsn"],
            AIRFLOW__CORE__DAGS_FOLDER=str(cls.root / "dags"),
            AIRFLOW__CORE__LOAD_EXAMPLES="false",
            AIRFLOW__CORE__EXECUTOR="LocalExecutor",
            AIRFLOW__CORE__EXECUTION_API_SERVER_URL=(
                f"http://127.0.0.1:{cls.ports['api']}/execution"
            ),
            AIRFLOW__LOGGING__WORKER_LOG_SERVER_PORT=str(cls.ports["worker_log"]),
            AIRFLOW__LOGGING__TRIGGER_LOG_SERVER_PORT=str(cls.ports["trigger_log"]),
            AIRFLOW__API_AUTH__JWT_SECRET="supervised-identity-" + "0" * 32,
            AIRFLOW__CORE__FERNET_KEY=fernet,
            AIRFLOW_CONN_SOLIDWORKS_WINDOWS=json.dumps(database_connection),
            PLATFORM_ARTIFACT_ROOT=str(cls.artifacts.root),
            PLATFORM_SERVICE_DSN=SUPERVISED["platform_dsn"],
            PLATFORM_CONFIG_SNAPSHOT=str(cls.snapshot),
            PIPELINE_WORK_ROOT=str(cls.root / "airflow-work"),
            PIPELINE_CLAIM_LEASE_SECONDS="600",
            SOLIDWORKS_ENDPOINT_URL="http://127.0.0.1:9",
            ENDPOINT_TOKEN_FILE=str(token),
            PYTHONPATH=str(cls.wheel_root),
        )
        (cls.root / "airflow-work").mkdir()
        os.environ.update(cls.env)
        (cls.root / "dags").mkdir()
        cls.dag_file = cls.root / "dags" / DAG_FILE.name
        cls.dag_file.write_bytes(DAG_FILE.read_bytes())
        cls.dag_sha256 = _sha256_file(cls.dag_file)
        if cls.dag_sha256 != _sha256_file(DAG_FILE):
            raise AssertionError("the supervised DAG copy is not byte-identical")

        cls._write_connection(database_connection)
        cls._start_services("initial", cls.dag_file.parent)
        cls._wait_for_dag()
        cls.serialized = cls._serialized_identity(cls.dag_file)
        cls._write_setup_summary()

    @classmethod
    def _factory(cls, *, work_root: Path, snapshot: Path | None = None):
        work_root.mkdir(parents=True, exist_ok=True)
        return cls.ProductionAdmissionFactory(
            artifacts=cls.artifacts,
            platform_snapshot=snapshot or cls.snapshot,
            native_tool_provider=lambda: copy.deepcopy(cls.native_tool),
            work_root=work_root,
            clock=lambda: datetime(2026, 10, 25, 10, tzinfo=UTC),
            portable_tool_provider=lambda: copy.deepcopy(cls.portable_tool),
        )

    @classmethod
    def _request(
        cls,
        label: str,
    ):
        dag_run_id = f"portal-supervised-{label}-{uuid.uuid4().hex[:10]}"
        identity = {
            "files": {
                path.relative_to(cls.intake).as_posix(): _sha256_file(path)
                for path in sorted(cls.intake.rglob("*"))
                if path.is_file()
            }
        }
        receipt = cls.ReceivedUploadReceipt(
            folder="CAD",
            files=len(identity["files"]),
            bytes=sum(
                path.stat().st_size
                for path in cls.intake.rglob("*")
                if path.is_file()
            ),
            handoff_sha256=cls.digest(identity["files"]),
            main_assembly="M3.0.3.SLDASM",
            target_hardware="m3.0",
            change_summary=f"Supervised production admission {label}",
            purpose="engineering",
        )
        request = cls.ProductionAdmissionRequest(
            run_id=str(uuid.uuid4()),
            request_id=dag_run_id,
            upload_dir=cls.upload_root,
            receipt=receipt,
            main_assembly=receipt.main_assembly,
            target_hardware=receipt.target_hardware,
            purpose=receipt.purpose,
            change_summary=receipt.change_summary,
            principal=cls.actor,
        )
        return request, dag_run_id

    @classmethod
    def _admit_run(cls, label: str) -> None:
        request, dag_run_id = cls._request(label)
        work = cls.root / "admission-work" / f"{label}-{uuid.uuid4().hex}"
        outcome = cls._factory(work_root=work).admit(cls.application, request)
        if outcome.freeze_command.request["native_target_identity"] != (
            cls.install_record["ends"]["windows"]["target_identity"]
        ):
            raise AssertionError("the Freeze command differs from the registered native target")
        conf = cls.artifacts.read_dispatch_conf(outcome.dispatch_command.conf_artifact)
        if (
            outcome.dispatch_command.dag_id != DAG_ID
            or outcome.dispatch_command.dag_run_id != dag_run_id
            or conf.run_id != request.run_id
            or conf.freeze_command_id != outcome.freeze_command.command_id
        ):
            raise AssertionError("the admission dispatch identity is not internally bound")
        cls.runs[label] = {
            "dag_run_id": dag_run_id,
            "run_id": request.run_id,
            "freeze_command_id": outcome.freeze_command.command_id,
            "dispatch_dag_id": outcome.dispatch_command.dag_id,
            "dispatch_dag_run_id": outcome.dispatch_command.dag_run_id,
            "dispatch_conf_run_id": conf.run_id,
            "dispatch_conf_freeze_command_id": conf.freeze_command_id,
            "task_id": "freeze",
            "map_index": -1,
        }

    @classmethod
    def _admit_successor(cls, label: str) -> None:
        parent = str(cls.runs["parent"]["run_id"])
        dag_run_id = f"portal-supervised-{label}-{uuid.uuid4().hex[:10]}"
        request = cls.ProductionFreezeSuccessorRequest(
            run_id=str(uuid.uuid4()),
            request_id=dag_run_id,
            parent_run_id=parent,
            main_assembly="M3.0.3.SLDASM",
            target_hardware="m3.0",
            purpose="engineering",
            change_summary="Supervised linked proof from committed Freeze bytes",
            principal=cls.actor,
        )
        work = cls.root / "admission-work" / f"{label}-{uuid.uuid4().hex}"
        outcome = cls._factory(work_root=work).admit_from_frozen_freeze(
            cls.application,
            request,
        )
        conf = cls.artifacts.read_dispatch_conf(outcome.dispatch_command.conf_artifact)
        if (
            outcome.dispatch_command.dag_id != DAG_ID
            or outcome.dispatch_command.dag_run_id != dag_run_id
            or conf.run_id != request.run_id
            or conf.freeze_command_id != outcome.freeze_command.command_id
        ):
            raise AssertionError("the successor dispatch identity is not internally bound")
        cls.runs[label] = {
            "dag_run_id": dag_run_id,
            "run_id": request.run_id,
            "freeze_command_id": outcome.freeze_command.command_id,
            "dispatch_dag_id": outcome.dispatch_command.dag_id,
            "dispatch_dag_run_id": outcome.dispatch_command.dag_run_id,
            "dispatch_conf_run_id": conf.run_id,
            "dispatch_conf_freeze_command_id": conf.freeze_command_id,
            "task_id": "freeze",
            "map_index": -1,
        }

    @classmethod
    def _write_setup_summary(cls) -> None:
        summary = {
            "scope": "production-admitted Freeze task through real Airflow 3",
            "airflow_version": cls._run("version", timeout=60).stdout.strip(),
            "dag_file": str(cls.dag_file),
            "dag_sha256": cls.dag_sha256,
            "package_file": str(cls.package_init),
            "package_sha256": cls.package_sha256,
            "portable_executor_sha256": cls.portable_source_sha256,
            "wheel": str(cls.wheel),
            "wheel_sha256": _sha256_file(cls.wheel),
            "snapshot_digest": cls.snapshot_digest,
            "serialized_dag": cls.serialized,
            "ports": cls.ports,
            "runs": cls.runs,
            "runtime_authority": {
                "facts": "FactStore",
                "artifacts": str(cls.artifacts.root),
                "legacy_platform_store_constructed": False,
                "legacy_airflow_connection_config_used": False,
            },
        }
        (cls.evidence_dir / "setup.json").write_text(
            json.dumps(summary, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    @classmethod
    def _latest_serialized(cls) -> dict:
        from airflow.models.dag_version import DagVersion
        from airflow.models.serialized_dag import SerializedDagModel
        from airflow.settings import Session

        with Session() as session:
            version = (
                session.query(DagVersion)
                .filter(DagVersion.dag_id == DAG_ID)
                .order_by(DagVersion.version_number.desc())
                .first()
            )
            if version is None:
                raise AssertionError("the supervised Airflow has no DagVersion")
            rows = (
                session.query(SerializedDagModel)
                .filter(
                    SerializedDagModel.dag_id == DAG_ID,
                    SerializedDagModel.dag_version_id == version.id,
                )
                .all()
            )
            if len(rows) != 1:
                raise AssertionError("the DAG has no unique SerializedDag row")
            row = rows[0]
            document = row.data["dag"]
            return {
                "bundle_name": version.bundle_name,
                "bundle_version": version.bundle_version,
                "dag_hash": row.dag_hash,
                "dag_version_id": str(version.id),
                "dag_version_number": version.version_number,
                "fileloc": document.get("fileloc"),
                "processor_dags_folder": document.get("_processor_dags_folder"),
                "row_dag_version_id": str(row.dag_version_id),
            }

    @classmethod
    def _serialized_identity(cls, expected_dag_file: Path) -> dict:
        identity = cls._latest_serialized()
        if identity["bundle_name"] != "dags-folder" or identity["bundle_version"] is not None:
            raise AssertionError(f"the local bundle identity is wrong: {identity}")
        if identity["row_dag_version_id"] != identity["dag_version_id"]:
            raise AssertionError(f"SerializedDag binds another DagVersion: {identity}")
        if identity["fileloc"] != str(expected_dag_file):
            raise AssertionError(f"the serialized DAG paths differ: {identity}")
        if identity["processor_dags_folder"] != str(expected_dag_file.parent):
            raise AssertionError(f"the serialized processor path differs: {identity}")
        return identity

    @classmethod
    def _write_connection(cls, connection: dict) -> None:
        from airflow.models.connection import Connection
        from airflow.settings import Session

        with Session() as session:
            row = (
                session.query(Connection)
                .filter(Connection.conn_id == "solidworks_windows")
                .one_or_none()
            )
            if row is None:
                row = Connection(conn_id="solidworks_windows")
                session.add(row)
            row.conn_type = connection["conn_type"]
            row.host = connection["host"]
            row.port = None
            row.login = None
            row.password = None
            row.schema = None
            row.extra = json.dumps(connection["extra"])
            session.commit()

    @classmethod
    def _run(cls, *arguments: str, timeout: float = 120) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(cls.venv / "bin" / "airflow"), *arguments],
            capture_output=True,
            text=True,
            env=cls.env,
            timeout=timeout,
        )

    @classmethod
    def _start_services(cls, label: str, dags_folder: Path) -> None:
        used = set(cls.ports.values())
        api_port = _free_port(used)
        worker_log_port = _free_port(used | {api_port})
        trigger_log_port = _free_port(used | {api_port, worker_log_port})
        cls.ports = {
            "api": api_port,
            "worker_log": worker_log_port,
            "trigger_log": trigger_log_port,
        }
        cls.env["AIRFLOW__CORE__DAGS_FOLDER"] = str(dags_folder)
        cls.env["AIRFLOW__CORE__EXECUTION_API_SERVER_URL"] = (
            f"http://127.0.0.1:{api_port}/execution"
        )
        cls.env["AIRFLOW__LOGGING__WORKER_LOG_SERVER_PORT"] = str(worker_log_port)
        cls.env["AIRFLOW__LOGGING__TRIGGER_LOG_SERVER_PORT"] = str(trigger_log_port)
        cls.env["PYTHONPATH"] = str(cls.wheel_root)
        components = (
            ("api-server", ["--host", "127.0.0.1", "--port", str(api_port)]),
            ("dag-processor", []),
            ("scheduler", []),
        )
        log_dir = cls.root / f"logs-{label}"
        log_dir.mkdir()
        cls.service_log_dirs.append(log_dir)
        for name, arguments in components:
            handle = open(log_dir / f"{name}.log", "w", encoding="utf-8")  # noqa: SIM115
            cls.logs.append(handle)
            process = subprocess.Popen(
                [str(cls.venv / "bin" / "airflow"), name, *arguments],
                stdout=handle,
                stderr=subprocess.STDOUT,
                env=cls.env,
                start_new_session=True,
            )
            cls.processes.append(process)
        time.sleep(12)
        for process, (name, _arguments) in zip(cls.processes, components, strict=True):
            if process.poll() is not None:
                tail = (log_dir / f"{name}.log").read_text(encoding="utf-8")[-4000:]
                raise AssertionError(f"{name} exited during startup:\n{tail}")

    @classmethod
    def _stop_services(cls, label: str = "cleanup") -> list[dict]:
        def group_alive(process) -> bool:
            process.poll()
            try:
                os.killpg(process.pid, 0)
            except ProcessLookupError:
                return False
            return True

        def wait_group(process, timeout: float) -> bool:
            deadline = time.monotonic() + timeout
            while group_alive(process):
                if time.monotonic() >= deadline:
                    return False
                time.sleep(0.05)
            return True

        stats = []
        retained = []
        for process in cls.processes:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
        for process in cls.processes:
            stopped = wait_group(process, 15)
            if not stopped:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                stopped = wait_group(process, 5)
            row = {
                "pid": process.pid,
                "returncode": process.returncode,
                "process_group_alive": not stopped,
            }
            if not stopped:
                retained.append(process)
                snapshot = subprocess.run(
                    ["ps", "-eo", "pid=,ppid=,pgid=,stat=,args="],
                    capture_output=True, text=True, check=False,
                )
                row["remaining_members"] = [
                    line.strip() for line in snapshot.stdout.splitlines()
                    if len(line.split(None, 4)) >= 4
                    and line.split(None, 4)[2] == str(process.pid)
                ]
                row["snapshot_returncode"] = snapshot.returncode
            stats.append(row)
        cls.processes = retained
        if not retained:
            for handle in cls.logs:
                handle.close()
            cls.logs = []
        cls.stop_history.append({"label": label, "processes": stats})
        return stats

    @classmethod
    def _wait_for_dag(cls) -> None:
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            result = cls._run("dags", "list", "-o", "plain", timeout=60)
            if result.returncode == 0 and DAG_ID in result.stdout:
                return
            time.sleep(2)
        raise AssertionError("the supervised DAG was not serialized within 120 seconds")

    @classmethod
    def _wait_for_dag_at(cls, dags_folder: Path, timeout: float = 120) -> dict:
        expected = dags_folder / DAG_FILE.name
        deadline = time.monotonic() + timeout
        observed = {}
        while time.monotonic() < deadline:
            try:
                observed = cls._latest_serialized()
            except AssertionError:
                time.sleep(2)
                continue
            if observed.get("fileloc") == str(expected):
                return observed
            time.sleep(2)
        raise AssertionError(
            f"the DAG processor did not bind {expected} within {timeout:.0f}s: {observed}"
        )

    @classmethod
    def _runtime_scope(cls) -> dict:
        from airflow.models.dagrun import DagRun
        from airflow.models.taskinstance import TaskInstance
        from airflow.settings import Session

        with Session() as session:
            dag_runs = (
                session.query(DagRun)
                .filter(DagRun.dag_id == DAG_ID)
                .order_by(DagRun.run_id)
                .all()
            )
            task_instances = (
                session.query(TaskInstance)
                .filter(TaskInstance.dag_id == DAG_ID)
                .order_by(
                    TaskInstance.run_id,
                    TaskInstance.task_id,
                    TaskInstance.map_index,
                )
                .all()
            )
        platform_rows = {}
        queries = {
            "commands": (
                "SELECT command_id, run_id, kind, stage FROM command "
                "ORDER BY run_id, stage NULLS LAST, kind"
            ),
            "stage_decisions": (
                "SELECT run_id, stage, command_id, outcome, reason_code "
                "FROM stage_decision ORDER BY run_id, stage"
            ),
            "execution_claims": (
                "SELECT command_id, dag_id, dag_run_id, task_id, map_index, "
                "fence, closure_code FROM execution_claim "
                "ORDER BY dag_run_id, task_id, fence"
            ),
            "effect_intents": (
                "SELECT effect_id, command_id, effect_kind, effect_key "
                "FROM effect_intent ORDER BY command_id, effect_key"
            ),
            "effect_facts": (
                "SELECT effect_id, seq, fact_type, source, state_class "
                "FROM effect_fact ORDER BY effect_id, seq"
            ),
        }
        with cls.connect() as connection, connection.cursor() as cursor:
            for name, query in queries.items():
                cursor.execute(query)
                columns = [item.name for item in cursor.description]
                platform_rows[name] = [
                    {
                        column: str(value) if value is not None else None
                        for column, value in zip(columns, row, strict=True)
                    }
                    for row in cursor.fetchall()
                ]
        return {
            "dag_runs": [
                {
                    "run_id": run.run_id,
                    "state": (
                        run.state.value
                        if hasattr(run.state, "value")
                        else str(run.state or "")
                    ),
                }
                for run in dag_runs
            ],
            "task_instances": [
                {
                    "run_id": task.run_id,
                    "task_id": task.task_id,
                    "map_index": task.map_index,
                    "state": (
                        task.state.value
                        if hasattr(task.state, "value")
                        else str(task.state or "")
                    ),
                    "try_number": task.try_number,
                }
                for task in task_instances
            ],
            "platform": platform_rows,
        }

    @classmethod
    def _cleanup(cls) -> None:
        if cls._cleaned:
            return
        cls._cleaned = True
        process_stats = cls._stop_services("final")
        cleanup = {
            "processes": process_stats,
            "process_stop_history": cls.stop_history,
            "temp_root": str(cls.root),
            "temp_removed": False,
        }
        evidence_dir = getattr(cls, "evidence_dir", None)
        if evidence_dir is not None and hasattr(cls, "connect"):
            try:
                runtime_scope = cls._runtime_scope()
                (evidence_dir / "runtime-scope.json").write_text(
                    json.dumps(runtime_scope, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
                cleanup["runtime_scope"] = "runtime-scope.json"
            except Exception as error:  # noqa: BLE001 - evidence capture must survive
                cleanup["runtime_scope_error"] = (
                    f"{type(error).__name__}: {error}"
                )
        original = getattr(cls, "original_environ", None)
        if original is not None:
            os.environ.clear()
            os.environ.update(original)
        if evidence_dir is None:
            return
        try:
            logs = evidence_dir / "logs"
            logs.mkdir(exist_ok=True)
            if (cls.airflow_home / "logs").is_dir():
                shutil.copytree(
                    cls.airflow_home / "logs",
                    logs / "airflow",
                    symlinks=True,
                    dirs_exist_ok=True,
                )
            for source in cls.service_log_dirs:
                shutil.copytree(
                    source,
                    logs / "services" / source.name,
                    symlinks=True,
                    dirs_exist_ok=True,
                )
            for name in ("supervised-report.json", "setup.json"):
                source = cls.root / name
                if source.is_file():
                    shutil.copy2(source, evidence_dir / name)
            manifest = []
            for path in sorted(evidence_dir.rglob("*")):
                if path.is_file() and path.name != "MANIFEST.sha256":
                    manifest.append(
                        f"{_sha256_file(path)}  {path.relative_to(evidence_dir).as_posix()}"
                    )
            (evidence_dir / "MANIFEST.sha256").write_text(
                "\n".join(manifest) + "\n",
                encoding="utf-8",
            )
            cleanup["evidence_dir"] = str(evidence_dir)
            cleanup["evidence_files"] = len(manifest)
            shutil.rmtree(cls.root)
            cleanup["temp_removed"] = True
        except Exception as error:  # noqa: BLE001 - evidence capture must survive
            cleanup["error"] = f"{type(error).__name__}: {error}"
        with contextlib.suppress(OSError):
            (evidence_dir / "cleanup.json").write_text(
                json.dumps(cleanup, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )

    def _trigger(self, label: str, conf: dict) -> dict:
        return self._trigger_run(
            label,
            str(self.runs[label]["dag_run_id"]),
            conf,
        )

    def _trigger_run(self, label: str, dag_run_id: str, conf: dict) -> dict:
        paused = self._run("dags", "pause", DAG_ID)
        self.assertEqual(paused.returncode, 0, paused.stderr)
        triggered = self._run(
            "dags",
            "trigger",
            DAG_ID,
            "-r",
            dag_run_id,
            "-c",
            json.dumps(conf),
            "-o",
            "plain",
        )
        self.assertEqual(triggered.returncode, 0, triggered.stdout + triggered.stderr)
        unpaused = self._run("dags", "unpause", DAG_ID)
        self.assertEqual(unpaused.returncode, 0, unpaused.stderr)
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            identity = self._task_identity(dag_run_id)
            if identity.get("state") in {"success", "failed", "upstream_failed"}:
                with contextlib.suppress(AssertionError):
                    self._record_actual_identity(label, identity)
                self._run("dags", "pause", DAG_ID)
                return identity
            time.sleep(1)
        self.fail(f"freeze of {dag_run_id} did not reach a terminal state")

    @staticmethod
    def _task_identity(dag_run_id: str) -> dict:
        from airflow.models.dagrun import DagRun
        from airflow.models.taskinstance import TaskInstance
        from airflow.settings import Session

        with Session() as session:
            task = (
                session.query(TaskInstance)
                .filter(
                    TaskInstance.dag_id == DAG_ID,
                    TaskInstance.run_id == dag_run_id,
                    TaskInstance.task_id == "freeze",
                    TaskInstance.map_index == -1,
                )
                .one_or_none()
            )
            if task is None:
                return {}
            run = (
                session.query(DagRun)
                .filter(DagRun.dag_id == DAG_ID, DagRun.run_id == dag_run_id)
                .one()
            )
            return {
                "state": task.state.value if hasattr(task.state, "value") else str(task.state or ""),
                "task_dag_version_id": str(task.dag_version_id) if task.dag_version_id else "",
                "dag_run_dag_version_id": (
                    str(run.created_dag_version_id)
                    if run.created_dag_version_id
                    else ""
                ),
            }

    def _record_actual_identity(self, label: str, identity: dict) -> None:
        task_version = identity["task_dag_version_id"]
        run_version = identity["dag_run_dag_version_id"]
        self.assertTrue(task_version, f"{label}: TaskInstance carries no dag_version_id")
        self.assertEqual(
            run_version,
            task_version,
            f"{label}: DagRun and TaskInstance versions differ",
        )
        registered = self._version_identity(task_version)
        self.assertEqual(task_version, self.serialized["dag_version_id"])
        self.assertEqual(registered["serialized"]["dag_version_id"], task_version)
        identity["registered_version"] = registered
        self.actual_identities[label] = identity
        run_dir = self.evidence_dir / "runs"
        run_dir.mkdir(exist_ok=True)
        (run_dir / f"{label}.json").write_text(
            json.dumps(identity, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    @classmethod
    def _version_identity(cls, version_id: str) -> dict:
        from airflow.models.dag_version import DagVersion
        from airflow.models.serialized_dag import SerializedDagModel
        from airflow.settings import Session

        with Session() as session:
            version = session.get(DagVersion, uuid.UUID(version_id))
            if version is None:
                raise AssertionError(f"dag_version_id {version_id} is not registered")
            rows = (
                session.query(SerializedDagModel)
                .filter(
                    SerializedDagModel.dag_id == DAG_ID,
                    SerializedDagModel.dag_version_id == version.id,
                )
                .all()
            )
            if len(rows) != 1:
                raise AssertionError(
                    f"dag_version_id {version_id} has no unique SerializedDag"
                )
            row = rows[0]
            document = row.data["dag"]
            return {
                "dag_id": version.dag_id,
                "bundle_name": version.bundle_name,
                "bundle_version": version.bundle_version,
                "dag_version_number": version.version_number,
                "serialized": {
                    "dag_version_id": str(row.dag_version_id),
                    "dag_hash": row.dag_hash,
                    "fileloc": document.get("fileloc"),
                    "processor_dags_folder": document.get("_processor_dags_folder"),
                },
            }

    def _log(self, dag_run_id: str) -> str:
        path = (
            self.airflow_home
            / "logs"
            / f"dag_id={DAG_ID}"
            / f"run_id={dag_run_id}"
            / "task_id=freeze"
            / "attempt=1.log"
        )
        self.assertTrue(path.is_file(), f"the supervised task log is missing: {path}")
        return path.read_text(encoding="utf-8")

    def _freeze_result(self, run_id: str):
        command = self.facts.load_stage_command(run_id, "freeze")
        decision = self.facts.decision_for(command)
        self.assertIsNotNone(decision)
        self.assertEqual(decision.outcome, "completed")
        result = self.facts.load_result(decision)
        self.assertEqual(result.actual_executor_sha256, self.portable_source_sha256)
        return result

    def _assert_claim_binding(self, label: str) -> None:
        expected = self.runs[label]
        with self.connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT dag_id, dag_run_id, task_id, map_index, fence
                FROM execution_claim
                WHERE command_id = %s
                ORDER BY fence
                """,
                (expected["freeze_command_id"],),
            )
            rows = cursor.fetchall()
        self.assertEqual(len(rows), 1)
        dag_id, dag_run_id, task_id, map_index, _fence = rows[0]
        self.assertEqual(dag_id, expected["dispatch_dag_id"])
        self.assertEqual(dag_run_id, expected["dispatch_dag_run_id"])
        self.assertEqual(dag_run_id, expected["dag_run_id"])
        self.assertEqual(task_id, expected["task_id"])
        self.assertEqual(map_index, expected["map_index"])
        self.assertEqual(
            expected["dispatch_conf_run_id"],
            expected["run_id"],
        )
        self.assertEqual(
            expected["dispatch_conf_freeze_command_id"],
            expected["freeze_command_id"],
        )

    def _trigger_missing_run_id(self, label: str) -> tuple[str, dict]:
        dag_run_id = f"path-switch-{label}-{uuid.uuid4().hex[:10]}"
        identity = self._trigger_run(label, dag_run_id, {"run_id": ""})
        return dag_run_id, identity

    def test_mismatched_config_snapshot_is_refused_without_fact_writes(self) -> None:
        request, dag_run_id = self._request("invalid-config")
        bad_snapshot = self.root / "state" / "snapshots" / f"bad-{dag_run_id}.json"
        bad_snapshot.write_text("{}\n", encoding="utf-8")
        work = self.root / "admission-work" / f"invalid-config-{uuid.uuid4().hex}"
        with self.assertRaises(self.ProtocolError) as raised:
            self._factory(work_root=work, snapshot=bad_snapshot).prepare(request)
        self.assertEqual(raised.exception.code, "admission_ingestion.config_invalid")
        with self.connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM run_admission WHERE run_id = %s",
                (request.run_id,),
            )
            self.assertEqual(cursor.fetchone()[0], 0)
        report = {
            "run_id": request.run_id,
            "dag_run_id": dag_run_id,
            "error_code": raised.exception.code,
            "fact_rows": 0,
            "legacy_platform_store_constructed": False,
            "legacy_airflow_connection_config_used": False,
        }
        (self.evidence_dir / "config-rejection.json").write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def test_runtime_dag_path_switch_updates_serialized_and_actual_source(self) -> None:
        dags_a = self.root / "dags-a"
        dags_b = self.root / "dags-b"
        dags_a.mkdir()
        dags_b.mkdir()
        (dags_a / DAG_FILE.name).write_bytes(DAG_FILE.read_bytes())
        (dags_b / DAG_FILE.name).write_bytes(DAG_FILE.read_bytes())
        phases = (
            ("a", dags_a),
            ("b", dags_b),
            ("a-return", dags_a),
            ("b-forward", dags_b),
        )
        report = []
        report_path = self.evidence_dir / "path-switch-report.json"
        try:
            for label, dags_folder in phases:
                before_stop = self._stop_services(f"before-{label}")
                report.append(
                    {
                        "phase": label,
                        "event": "stop-before-start",
                        "processes": before_stop,
                    }
                )
                self.assertFalse(
                    any(row["process_group_alive"] for row in before_stop),
                    f"a previous process group remains alive before {label}",
                )
                self._start_services(label, dags_folder)
                serialized = self._wait_for_dag_at(dags_folder)
                dag_run_id, identity = self._trigger_missing_run_id(label)
                log = self._log(dag_run_id)
                self.assertIn("dag_run.conf.run_id is required", log)
                self.assertEqual(identity["state"], "failed")
                self.assertEqual(
                    identity["task_dag_version_id"],
                    identity["dag_run_dag_version_id"],
                )
                registered = self._version_identity(identity["task_dag_version_id"])
                expected_dag_file = dags_folder / DAG_FILE.name
                self.assertEqual(registered["serialized"]["fileloc"], str(expected_dag_file))
                self.assertEqual(
                    registered["serialized"]["processor_dags_folder"],
                    str(dags_folder),
                )
                report.append(
                    {
                        "phase": label,
                        "dags_folder": str(dags_folder),
                        "dag_file": str(expected_dag_file),
                        "dag_sha256": _sha256_file(expected_dag_file),
                        "serialized": serialized,
                        "task_version": identity,
                        "registered_version": registered,
                        "log_sha256": hashlib.sha256(log.encode()).hexdigest(),
                    }
                )
                report_path.write_text(
                    json.dumps(report, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
        finally:
            final_stop = self._stop_services("path-switch-final")
            report.append({"phase": "final", "event": "stop", "processes": final_stop})
            report_path.write_text(
                json.dumps(report, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )

    def test_fresh_parent_linked_and_early_failure(self) -> None:
        fresh_run = str(self.runs["fresh"]["run_id"])
        fresh = self._trigger("fresh", {"run_id": fresh_run})
        self.assertEqual(fresh["state"], "success")
        fresh_result = self._freeze_result(fresh_run)
        self.assertEqual(fresh_result.run_id, fresh_run)
        self._assert_claim_binding("fresh")

        early_dag_run = f"supervised-early-{uuid.uuid4().hex[:10]}"
        early = self._trigger_run("early", early_dag_run, {"run_id": ""})
        self.assertEqual(early["state"], "failed")
        self.assertIn(
            "dag_run.conf.run_id is required",
            self._log(early_dag_run),
        )

        parent_run = str(self.runs["parent"]["run_id"])
        parent = self._trigger("parent", {"run_id": parent_run})
        self.assertEqual(parent["state"], "success")
        parent_result = self._freeze_result(parent_run)
        self.assertEqual(parent_result.run_id, parent_run)
        self._assert_claim_binding("parent")

        self._admit_successor("linked")
        linked_run = str(self.runs["linked"]["run_id"])
        linked = self._trigger("linked", {"run_id": linked_run})
        self.assertEqual(linked["state"], "success")
        linked_result = self._freeze_result(linked_run)
        admission = self.facts.load_admission(linked_run)
        self.assertIsNotNone(admission)
        self.assertEqual(admission.parent_run_id, parent_run)
        self.assertEqual(admission.from_stage, "freeze")
        self.assertEqual(linked_result.run_id, linked_run)
        self._assert_claim_binding("linked")

        report = {
            "airflow_version": self._run("version").stdout.strip(),
            "dag_file": str(self.dag_file),
            "dag_sha256": self.dag_sha256,
            "package_file": str(self.package_init),
            "package_sha256": self.package_sha256,
            "portable_executor_sha256": self.portable_source_sha256,
            "wheel": str(self.wheel),
            "wheel_sha256": _sha256_file(self.wheel),
            "snapshot_digest": self.snapshot_digest,
            "malicious_airflow_connection_present": True,
            "runtime_authority": "ProductionAdmissionFactory + FactStore + StageApplication",
            "scope": "Freeze only; no native CAD or downstream stage",
            "serialized_dag": self.serialized,
            "actual_task_versions": self.actual_identities,
            "runs": self.runs,
            "states": {
                "fresh": fresh["state"],
                "parent": parent["state"],
                "linked": linked["state"],
                "early": early["state"],
            },
            "results": {
                "fresh": fresh_result.to_record(),
                "parent": parent_result.to_record(),
                "linked": linked_result.to_record(),
            },
        }
        report_path = self.evidence_dir / "supervised-report.json"
        report_path.write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(f"supervised report: {report_path}")


if __name__ == "__main__":
    unittest.main()
