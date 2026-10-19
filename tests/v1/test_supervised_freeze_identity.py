"""Opt-in real Airflow 3 supervised freeze identity proof.

The test starts its own DAG processor, scheduler and api-server against an
already-migrated, isolated Airflow metadata database.  The task process imports
``description_pipeline`` from a clean wheel extraction, while the DAG file is a
byte copy of this checkout.  Every case is triggered through the real
scheduler and LocalExecutor (never ``airflow tasks test``).

Required environment:

* ``DESCRIPTION_SUPERVISED_AIRFLOW_DSN``: Airflow metadata DSN
* ``DESCRIPTION_SUPERVISED_PLATFORM_DSN``: platform service DSN
* ``DESCRIPTION_SUPERVISED_AIRFLOW_VENV``: Airflow 3 virtual environment
* ``DESCRIPTION_SUPERVISED_WHEEL``: wheel built from this candidate
* ``DESCRIPTION_SUPERVISED_EVIDENCE_DIR``: persistent parent directory for
  task logs, service logs, the report and cleanup status

The caller owns PostgreSQL bootstrap, migrations and the two schemas.  Every
process this test starts is its own process group and is terminated by the
test; no installed service or shared virtual environment is modified.

Scope: the freeze boundary only.  This is a supervised-task identity proof,
not a native CAD or full end-to-end acceptance.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
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
from pathlib import Path

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
        cls.evidence_dir = evidence_parent / f"supervised-{time.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
        cls.evidence_dir.mkdir()
        if cls.evidence_dir == cls.root or cls.root in cls.evidence_dir.parents or cls.evidence_dir in cls.root.parents:
            raise AssertionError("the evidence directory must be disjoint from the temporary run root")
        first = _free_port()
        second = _free_port({first})
        third = _free_port({first, second})
        cls.ports = {
            "api": first,
            "worker_log": second,
            "trigger_log": third,
        }

        from description_pipeline.delivery import BUNDLE_SCHEMA, PIPELINE_ID
        from description_pipeline.io import digest
        from description_pipeline.orchestration.airflow_client import native_run_id
        from description_pipeline.platform.store import PlatformStore
        from description_pipeline.sources.solidworks.revision import package_inventory
        from description_pipeline.sources.solidworks.selection import validate_file_index
        from tests.v1.config_identity_support import prepare_root, write_snapshot
        from tests.v1.test_install_record import platform_document

        cls.wheel_root = cls.root / "wheel"
        with zipfile.ZipFile(cls.wheel) as archive:
            archive.extractall(cls.wheel_root)
        package_init = cls.wheel_root / "description_pipeline" / "__init__.py"
        if not package_init.is_file():
            raise AssertionError("the clean wheel carries no description_pipeline package")

        cls.state_root = cls.root / "state"
        prepare_root(cls.root)
        cls.snapshot = write_snapshot(cls.root, "supervised", platform_document(cls.root))
        cls.snapshot_digest = _sha256_file(cls.snapshot)
        cls.intake = cls.root / "handoffs" / "M3.0.3"
        (cls.intake / "parts").mkdir(parents=True, exist_ok=True)
        (cls.intake / "3.0 总装.SLDASM").write_bytes(b"assembly-bytes")
        (cls.intake / "parts" / "bracket.SLDPRT").write_bytes(b"part-bytes")
        files = validate_file_index(package_inventory(cls.intake))
        handoff = digest(files)
        baseline = {
            "handoff_sha256": handoff,
            "files": len(files),
            "bytes": sum(entry.stat().st_size for entry in cls.intake.rglob("*") if entry.is_file()),
            "folder": cls.intake.name,
            "platform_config_digest": cls.snapshot_digest,
        }
        cls.store_root = cls.root / "state" / "runs"
        cls.store = PlatformStore.from_environment(
            {
                "PLATFORM_SERVICE_DSN": SUPERVISED["platform_dsn"],
                "PIPELINE_STORE_ROOT": str(cls.store_root),
            }
        )
        cls.store.open_admission(reason="supervised identity proof", actor="supervised-test")
        cls.actor = "cli_supervised:tenant:operator"
        cls.runs: dict[str, dict[str, str]] = {}
        cls.actual_identities: dict[str, dict] = {}

        def admit(label: str, *, parent_run_id: str | None = None, from_stage: str | None = None) -> str:
            suffix = uuid.uuid4().hex[:10]
            dag_run_id = f"portal-supervised-{label}-{suffix}"
            run_id = native_run_id(dag_run_id)
            cls.store.admit_run(
                request_id=dag_run_id,
                run_id=run_id,
                principal=cls.actor,
                target_hardware="m3.0",
                main_assembly="3.0 总装.SLDASM",
                configuration={"purpose": "engineering"},
                baseline=baseline,
                from_stage=from_stage,
                parent_run_id=parent_run_id,
                dispatch={
                    "stage": "freeze",
                    "operation_key": f"portal-{dag_run_id}/freeze:0",
                    "params_digest": "a" * 64,
                    "dag_id": DAG_ID,
                    "dag_run_id": dag_run_id,
                },
            )
            cls.runs[label] = {"dag_run_id": dag_run_id, "run_id": run_id}
            return run_id

        fresh_run = admit("fresh")
        early_run = admit("early")
        parent_run = admit("parent")
        cls.store.confirm_stage(
            run_id=parent_run,
            stage="freeze",
            state="failed",
            receipt={
                "schema_version": BUNDLE_SCHEMA,
                "pipeline_id": PIPELINE_ID,
                "run_id": parent_run,
                "stage": "freeze",
                "state": "failed",
                "passed": False,
                "error": "controlled parent freeze failure",
            },
        )
        admit("linked", parent_run_id=parent_run, from_stage="freeze")
        cls.fresh_run = fresh_run
        cls.early_run = early_run
        cls.parent_run = parent_run
        cls.linked_run = cls.runs["linked"]["run_id"]

        cls.decoy = cls.root / "decoy-snapshot.json"
        cls.decoy.write_text("{}\n", encoding="utf-8")
        database_connection = {
            "conn_type": "http",
            "host": "",
            "port": None,
            "extra": {
                "config_identity": {
                    "schema_version": "solidworks-to-urdf.platform-config-snapshot/v1",
                    "path": str(cls.snapshot),
                    "sha256": cls.snapshot_digest,
                }
            },
        }
        malicious_connection = {
            "conn_type": "http",
            "host": "",
            "port": None,
            "extra": {
                "config_identity": {
                    "schema_version": "solidworks-to-urdf.platform-config-snapshot/v1",
                    "path": str(cls.decoy),
                    "sha256": "0" * 64,
                }
            },
        }
        fernet = urlsafe_b64encode(hashlib.sha256(b"supervised-freeze-identity").digest()).decode()
        cls.env = dict(
            os.environ,
            AIRFLOW_HOME=str(cls.airflow_home),
            AIRFLOW__DATABASE__SQL_ALCHEMY_CONN=SUPERVISED["airflow_dsn"],
            AIRFLOW__CORE__DAGS_FOLDER=str(cls.root / "dags"),
            AIRFLOW__CORE__LOAD_EXAMPLES="false",
            AIRFLOW__CORE__EXECUTOR="LocalExecutor",
            AIRFLOW__CORE__EXECUTION_API_SERVER_URL=f"http://127.0.0.1:{cls.ports['api']}/execution",
            AIRFLOW__LOGGING__WORKER_LOG_SERVER_PORT=str(cls.ports["worker_log"]),
            AIRFLOW__LOGGING__TRIGGER_LOG_SERVER_PORT=str(cls.ports["trigger_log"]),
            AIRFLOW__API_AUTH__JWT_SECRET="supervised-identity-" + "0" * 32,
            AIRFLOW__CORE__FERNET_KEY=fernet,
            AIRFLOW_CONN_SOLIDWORKS_WINDOWS=json.dumps(malicious_connection),
            OPERATOR_STATE=str(cls.state_root),
            PLATFORM_SERVICE_DSN=SUPERVISED["platform_dsn"],
            PIPELINE_STORE_ROOT=str(cls.store_root),
            SOLIDWORKS_SENSOR_MODE="reschedule",
            PYTHONPATH=str(cls.wheel_root),
        )
        os.environ.update(cls.env)
        (cls.root / "dags").mkdir()
        cls.dag_file = cls.root / "dags" / DAG_FILE.name
        cls.dag_file.write_bytes(DAG_FILE.read_bytes())
        cls.dag_sha256 = _sha256_file(cls.dag_file)
        if cls.dag_sha256 != _sha256_file(DAG_FILE):
            raise AssertionError("the supervised DAG copy is not byte-identical to the candidate")
        cls.package_init = package_init
        cls.package_sha256 = _sha256_file(package_init)
        cls._write_connection(database_connection)
        cls._start_services("initial", cls.dag_file.parent)
        cls._wait_for_dag()
        cls.serialized = cls._serialized_identity(cls.dag_file)
        cls._write_setup_summary()

    @classmethod
    def _write_setup_summary(cls) -> None:
        summary = {
            "scope": "freeze boundary only; not a native CAD or full end-to-end run",
            "airflow_version": cls._run("version", timeout=60).stdout.strip(),
            "dag_file": str(cls.dag_file),
            "dag_sha256": cls.dag_sha256,
            "package_file": str(cls.package_init),
            "package_sha256": cls.package_sha256,
            "wheel": str(cls.wheel),
            "wheel_sha256": _sha256_file(cls.wheel),
            "snapshot_digest": cls.snapshot_digest,
            "serialized_dag": cls.serialized,
            "ports": cls.ports,
            "malicious_connection_environment": {
                "present": True,
                "path": str(cls.decoy),
                "sha256": "0" * 64,
            },
            "airflow_dsn_configured": bool(SUPERVISED["airflow_dsn"]),
            "platform_dsn_configured": bool(SUPERVISED["platform_dsn"]),
            "runs": cls.runs,
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
                raise AssertionError("the supervised Airflow has no DagVersion for the DAG")
            rows = (
                session.query(SerializedDagModel)
                .filter(
                    SerializedDagModel.dag_id == DAG_ID,
                    SerializedDagModel.dag_version_id == version.id,
                )
                .all()
            )
            if len(rows) != 1:
                raise AssertionError("the supervised Airflow did not bind exactly one SerializedDag row")
            row = rows[0]
            document = row.data["dag"]
            identity = {
                "bundle_name": version.bundle_name,
                "bundle_version": version.bundle_version,
                "dag_hash": row.dag_hash,
                "dag_version_id": str(version.id),
                "dag_version_number": version.version_number,
                "fileloc": document.get("fileloc"),
                "processor_dags_folder": document.get("_processor_dags_folder"),
                "row_dag_version_id": str(row.dag_version_id),
            }
        return identity

    @classmethod
    def _serialized_identity(cls, expected_dag_file: Path) -> dict:
        identity = cls._latest_serialized()
        if identity["bundle_name"] != "dags-folder" or identity["bundle_version"] is not None:
            raise AssertionError(f"the supervised local bundle identity is wrong: {identity}")
        if identity["row_dag_version_id"] != identity["dag_version_id"]:
            raise AssertionError(f"the SerializedDag row binds another DagVersion: {identity}")
        if identity["fileloc"] != str(expected_dag_file):
            raise AssertionError(f"the serialized DAG fileloc differs: {identity}")
        if identity["processor_dags_folder"] != str(expected_dag_file.parent):
            raise AssertionError(f"the serialized processor folder differs: {identity}")
        return identity

    @classmethod
    def _write_connection(cls, connection: dict) -> None:
        from airflow.models.connection import Connection
        from airflow.settings import Session

        with Session() as session:
            row = session.query(Connection).filter(Connection.conn_id == "solidworks_windows").one_or_none()
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
        used = set(cls.ports.values()) if cls.ports else set()
        api_port = _free_port(used)
        worker_log_port = _free_port(used | {api_port})
        trigger_log_port = _free_port(used | {api_port, worker_log_port})
        cls.ports = {
            "api": api_port,
            "worker_log": worker_log_port,
            "trigger_log": trigger_log_port,
        }
        cls.env["AIRFLOW__CORE__DAGS_FOLDER"] = str(dags_folder)
        cls.env["AIRFLOW__CORE__EXECUTION_API_SERVER_URL"] = f"http://127.0.0.1:{api_port}/execution"
        cls.env["AIRFLOW__LOGGING__WORKER_LOG_SERVER_PORT"] = str(worker_log_port)
        cls.env["AIRFLOW__LOGGING__TRIGGER_LOG_SERVER_PORT"] = str(trigger_log_port)
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
        for process, (name, _) in zip(cls.processes, components, strict=True):
            if process.poll() is not None:
                tail = (log_dir / f"{name}.log").read_text(encoding="utf-8")[-2000:]
                raise AssertionError(f"{name} exited during startup:\n{tail}")

    @classmethod
    def _stop_services(cls, label: str = "cleanup") -> list[dict]:
        stats = []
        for process in cls.processes:
            if process.poll() is None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGTERM)
        for process in cls.processes:
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
            group_alive = False
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, 0)
                group_alive = True
            stats.append(
                {
                    "pid": process.pid,
                    "returncode": process.returncode,
                    "process_group_alive": group_alive,
                }
            )
        for handle in cls.logs:
            handle.close()
        cls.processes = []
        cls.logs = []
        cls.stop_history.append({"label": label, "processes": stats})
        return stats

    @classmethod
    def _wait_for_dag(cls) -> None:
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            result = cls._run("dags", "list", "-o", "plain", timeout=60)
            if DAG_ID in result.stdout:
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
            f"the DAG processor did not bind {expected} within {timeout:.0f}s; observed {observed}"
        )

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
        original = getattr(cls, "original_environ", None)
        if original is not None:
            os.environ.clear()
            os.environ.update(original)

        if evidence_dir is not None:
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
                        manifest.append(f"{_sha256_file(path)}  {path.relative_to(evidence_dir).as_posix()}")
                (evidence_dir / "MANIFEST.sha256").write_text("\n".join(manifest) + "\n", encoding="utf-8")
                cleanup["evidence_dir"] = str(evidence_dir)
                cleanup["evidence_files"] = len(manifest)
                shutil.rmtree(cls.root)
                cleanup["temp_removed"] = True
            except Exception as error:  # noqa: BLE001 - cleanup evidence must survive
                cleanup["error"] = f"{type(error).__name__}: {error}"
        if evidence_dir is not None:
            with contextlib.suppress(OSError):
                (evidence_dir / "cleanup.json").write_text(
                    json.dumps(cleanup, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )

    def _trigger(self, label: str, conf: dict) -> dict:
        dag_run_id = self.runs[label]["dag_run_id"]
        paused = self._run("dags", "pause", DAG_ID)
        self.assertEqual(paused.returncode, 0, paused.stderr)
        triggered = self._run("dags", "trigger", DAG_ID, "-r", dag_run_id, "-c", json.dumps(conf), "-o", "plain")
        self.assertEqual(triggered.returncode, 0, triggered.stdout + triggered.stderr)
        from airflow.models.dagrun import DagRun
        from airflow.settings import Session
        from description_pipeline.orchestration.feishu_oauth import build_actor_name

        with Session() as session:
            row = session.query(DagRun).filter(DagRun.dag_id == DAG_ID, DagRun.run_id == dag_run_id).one()
            row.triggering_user_name = build_actor_name(self.actor, "Supervised Operator")
            session.commit()
        unpaused = self._run("dags", "unpause", DAG_ID)
        self.assertEqual(unpaused.returncode, 0, unpaused.stderr)
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            identity = self._task_identity(dag_run_id)
            if identity["state"] in {"success", "failed", "upstream_failed"}:
                self._record_actual_identity(label, identity)
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
                .one()
            )
            run = session.query(DagRun).filter(DagRun.dag_id == DAG_ID, DagRun.run_id == dag_run_id).one()
            return {
                "state": task.state.value if hasattr(task.state, "value") else str(task.state or ""),
                "task_dag_version_id": str(task.dag_version_id) if task.dag_version_id else "",
                "dag_run_dag_version_id": (
                    str(run.created_dag_version_id) if run.created_dag_version_id else ""
                ),
            }

    def _record_actual_identity(self, label: str, identity: dict) -> None:
        task_version = identity["task_dag_version_id"]
        run_version = identity["dag_run_dag_version_id"]
        self.assertTrue(task_version, f"{label}: the TaskInstance carries no dag_version_id")
        self.assertEqual(run_version, task_version, f"{label}: DagRun and TaskInstance versions differ")
        registered = self._version_identity(task_version)
        self.assertEqual(task_version, self.serialized["dag_version_id"])
        self.assertEqual(registered["serialized"]["dag_version_id"], task_version)
        self.assertEqual(registered["serialized"]["fileloc"], str(self.dag_file))
        self.assertEqual(registered["serialized"]["processor_dags_folder"], str(self.dag_file.parent))
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
                raise AssertionError(f"the actual dag_version_id {version_id} is not registered")
            rows = (
                session.query(SerializedDagModel)
                .filter(
                    SerializedDagModel.dag_id == DAG_ID,
                    SerializedDagModel.dag_version_id == version.id,
                )
                .all()
            )
            if len(rows) != 1:
                raise AssertionError(f"the actual dag_version_id {version_id} has no unique SerializedDag")
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

    def _assert_source_identity(self, log: str, expected_dag_file: Path | None = None) -> None:
        expected = expected_dag_file or self.dag_file
        self.assertIn("task source identity task=freeze", log)
        self.assertIn(f"dag_file={expected}", log)
        self.assertIn(f"dag_sha256={self.dag_sha256}", log)
        self.assertIn(f"package_file={self.package_init}", log)
        self.assertIn(f"package_sha256={self.package_sha256}", log)
        self.assertNotIn("Direct database access via the ORM is not allowed", log)

    def _trigger_early_failure(self, label: str) -> tuple[str, dict]:
        dag_run_id = f"path-switch-{label}-{uuid.uuid4().hex[:10]}"
        paused = self._run("dags", "pause", DAG_ID)
        self.assertEqual(paused.returncode, 0, paused.stderr)
        conf = {
            "target_hardware": "m3.0",
            "change_summary": f"path switch {label}",
            "purpose": "engineering",
            "handoff_path": str(self.intake),
        }
        triggered = self._run("dags", "trigger", DAG_ID, "-r", dag_run_id, "-c", json.dumps(conf), "-o", "plain")
        self.assertEqual(triggered.returncode, 0, triggered.stdout + triggered.stderr)
        unpaused = self._run("dags", "unpause", DAG_ID)
        self.assertEqual(unpaused.returncode, 0, unpaused.stderr)
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            identity = self._task_identity(dag_run_id)
            if identity["state"] in {"success", "failed", "upstream_failed"}:
                return dag_run_id, identity
            time.sleep(1)
        self.fail(f"freeze of {dag_run_id} did not reach a terminal state")

    def test_runtime_dag_path_switch_updates_serialized_and_actual_source(self) -> None:
        dags_a = self.root / "dags-a"
        dags_b = self.root / "dags-b"
        dags_a.mkdir()
        dags_b.mkdir()
        (dags_a / DAG_FILE.name).write_bytes(DAG_FILE.read_bytes())
        (dags_b / DAG_FILE.name).write_bytes(DAG_FILE.read_bytes())
        phases = (("a", dags_a), ("b", dags_b), ("a-return", dags_a), ("b-forward", dags_b))
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
                report_path.write_text(
                    json.dumps(report, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
                self.assertFalse(
                    any(row["process_group_alive"] for row in before_stop),
                    f"a process group from the previous phase is still alive before {label}",
                )
                self._start_services(label, dags_folder)
                try:
                    serialized = self._wait_for_dag_at(dags_folder)
                except AssertionError as error:
                    report.append(
                        {
                            "phase": label,
                            "expected_dags_folder": str(dags_folder),
                            "observed_serialized": self._latest_serialized(),
                            "error": str(error),
                        }
                    )
                    report_path.write_text(
                        json.dumps(report, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8",
                    )
                    raise
                dag_run_id, identity = self._trigger_early_failure(label)
                log = self._log(dag_run_id)
                expected_dag_file = dags_folder / DAG_FILE.name
                self._assert_source_identity(log, expected_dag_file)
                self.assertEqual(identity["state"], "failed")
                self.assertEqual(identity["task_dag_version_id"], identity["dag_run_dag_version_id"])
                task_version = identity["task_dag_version_id"]
                registered = self._version_identity(task_version)
                phase = {
                    "phase": label,
                    "dags_folder": str(dags_folder),
                    "dag_file": str(expected_dag_file),
                    "dag_sha256": _sha256_file(expected_dag_file),
                    "serialized": serialized,
                    "task_version": identity,
                    "registered_version": registered,
                    "log": {
                        "path": str(
                            self.airflow_home
                            / "logs"
                            / f"dag_id={DAG_ID}"
                            / f"run_id={dag_run_id}"
                            / "task_id=freeze"
                            / "attempt=1.log"
                        ),
                        "sha256": hashlib.sha256(log.encode()).hexdigest(),
                    },
                }
                report.append(phase)
                report_path.write_text(
                    json.dumps(report, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
                self.assertEqual(registered["serialized"]["fileloc"], str(expected_dag_file))
                self.assertEqual(registered["serialized"]["processor_dags_folder"], str(dags_folder))
        finally:
            final_stop = self._stop_services("path-switch-final")
            report.append({"phase": "final", "event": "stop", "processes": final_stop})
            report_path.write_text(
                json.dumps(report, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            self.assertFalse(
                any(row["process_group_alive"] for row in final_stop),
                "a final path-switch process group is still alive",
            )

    def test_fresh_linked_and_early_failure_are_orm_free(self) -> None:
        fresh = self._trigger(
            "fresh",
            {
                "request_id": self.runs["fresh"]["dag_run_id"],
                "target_hardware": "m3.0",
                "change_summary": "supervised fresh proof",
                "purpose": "engineering",
                "handoff_path": str(self.intake),
            },
        )
        self.assertEqual(fresh["state"], "success")
        fresh_log = self._log(self.runs["fresh"]["dag_run_id"])
        self._assert_source_identity(fresh_log)
        self.assertIn(f"freeze fresh admitted config_digest={self.snapshot_digest}", fresh_log)

        early = self._trigger(
            "early",
            {
                "target_hardware": "m3.0",
                "change_summary": "supervised early failure",
                "purpose": "engineering",
                "handoff_path": str(self.intake),
            },
        )
        self.assertEqual(early["state"], "failed")
        early_log = self._log(self.runs["early"]["dag_run_id"])
        self._assert_source_identity(early_log)
        self.assertIn("request_id is required", early_log)

        linked = self._trigger(
            "linked",
            {
                "parent_dag_run_id": self.runs["parent"]["dag_run_id"],
                "resume_from": "freeze",
                "request_id": self.runs["linked"]["dag_run_id"],
                "target_hardware": "m3.0",
                "change_summary": "supervised linked proof",
                "purpose": "engineering",
                "handoff_path": str(self.intake),
            },
        )
        self.assertEqual(linked["state"], "success")
        linked_log = self._log(self.runs["linked"]["dag_run_id"])
        self._assert_source_identity(linked_log)
        self.assertIn(f"freeze linked admitted config_digest={self.snapshot_digest}", linked_log)

        self.assertEqual(self.store.stage_records(self.fresh_run)["freeze"]["state"], "completed")
        self.assertEqual(self.store.stage_records(self.early_run)["freeze"]["state"], "failed")
        self.assertEqual(self.store.stage_records(self.linked_run)["freeze"]["state"], "completed")
        self.assertEqual(self.store.run_record(self.early_run)["state"], "failed")
        self.assertEqual(self.store.run_record(self.linked_run)["parent_run_id"], self.parent_run)
        self.assertNotEqual(self.decoy.read_text(encoding="utf-8"), self.snapshot.read_text(encoding="utf-8"))
        report = {
            "airflow_version": self._run("version").stdout.strip(),
            "dag_file": str(self.dag_file),
            "dag_sha256": self.dag_sha256,
            "package_file": str(self.package_init),
            "package_sha256": self.package_sha256,
            "wheel": str(self.wheel),
            "wheel_sha256": _sha256_file(self.wheel),
            "snapshot_digest": self.snapshot_digest,
            "malicious_connection_environment": True,
            "scope": "freeze boundary only; not a native CAD or full end-to-end run",
            "serialized_dag": self.serialized,
            "actual_task_versions": self.actual_identities,
            "runs": self.runs,
            "states": {
                "fresh": fresh["state"],
                "early": early["state"],
                "linked": linked["state"],
            },
            "logs": {
                name: {
                    "path": str(
                        self.airflow_home
                        / "logs"
                        / f"dag_id={DAG_ID}"
                        / f"run_id={self.runs[name]['dag_run_id']}"
                        / "task_id=freeze"
                        / "attempt=1.log"
                    ),
                    "sha256": hashlib.sha256(self._log(self.runs[name]["dag_run_id"]).encode()).hexdigest(),
                }
                for name in ("fresh", "early", "linked")
            },
        }
        report_path = self.evidence_dir / "supervised-report.json"
        report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"supervised report: {report_path}")


if __name__ == "__main__":
    unittest.main()
