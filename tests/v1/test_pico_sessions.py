"""Real database, native wheel and HTTP/WebSocket session integration."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, contextmanager, suppress
from copy import deepcopy
import ctypes
from datetime import datetime, UTC
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import socket
import signal
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]
WORKER = os.environ.get("TELEOP_PICO_PYTHON")
LINK = os.environ.get("PICO_LINK_PYTHON")
SESSION_TESTS = bool(WORKER) and sys.platform == "linux"

if SESSION_TESTS:
    import httpx
    import mujoco
    import pgserver
    import psycopg2
    import uvicorn
    from websockets.asyncio.client import connect

    from description_pipeline.artifacts import ArtifactStore
    from description_pipeline.io import digest, canonical
    from description_pipeline.orchestration.pico_portal import create_app
    from description_pipeline.orchestration.airflow_client import native_run_id
    from description_pipeline.orchestration.feishu_oauth import build_actor_name
    from description_pipeline.orchestration.portal import PortalApp, PortalConfig
    from description_pipeline.orchestration.report_view import text_warning_inventory
    from description_pipeline.pipeline.contracts import RunAdmission, StageCommand, StageResult
    from description_pipeline.owned_process import process_identity, resource_state
    from description_pipeline.simulation.service import RunningSession, SimulationService
    from description_pipeline.simulation.store import SimulationConflict, SimulationStore
    from description_pipeline.simulation.recording import prepare_bound
    from description_pipeline.simulation.recording import recording_binding, run_verification, verification_execution
    from description_pipeline.simulation import recording as recording_module
    from description_pipeline.simulation.recording import VerificationUnresolved, session_identity
    from description_pipeline.pipeline.contracts import ArtifactRef
    from description_pipeline.platform.facts import FactStore
    from description_pipeline.schemas import STAGE_RESULT_SCHEMA


def model_source(root):
    """Construct an explicit inspection fixture, independent of production profiles."""
    robot = ET.Element("robot", name="probe")
    ET.SubElement(robot, "link", name="base")
    compiler = ET.SubElement(ET.SubElement(robot, "mujoco"), "compiler")
    compiler.set("discardvisual", "false")
    compiler.set("fusestatic", "false")
    arms = {}
    for side, sign in (("left", 1), ("right", -1), ("head", 0)):
        names, parent = [], "base"
        for index, axis in enumerate(("0 0 1", "0 1 0", "1 0 0")):
            name, child = f"{side}_{index}", (f"head_link{index + 1}" if side == "head" else f"{side}_link_{index}")
            link = ET.SubElement(robot, "link", name=child)
            ET.SubElement(ET.SubElement(ET.SubElement(link, "visual"), "geometry"), "box", size="0.05 0.02 0.02")
            inertial = ET.SubElement(link, "inertial")
            ET.SubElement(inertial, "mass", value="0.1")
            ET.SubElement(inertial, "inertia", ixx="0.001", iyy="0.001", izz="0.001", ixy="0", ixz="0", iyz="0")
            joint = ET.SubElement(robot, "joint", name=name, type="revolute")
            ET.SubElement(joint, "parent", link=parent)
            ET.SubElement(joint, "child", link=child)
            ET.SubElement(joint, "origin", xyz=f"0.08 {0.15 * sign if index == 0 else 0} 0")
            ET.SubElement(joint, "axis", xyz=axis)
            ET.SubElement(joint, "limit", lower="-1.2", upper="1.2", effort="1", velocity="1")
            names.append(name)
            parent = child
        if side != "head":
            arms[side] = {"joints": names, "frame": parent, "tip_offset": [0.03, 0, 0], "home": [0, 0, 0]}
    (root / "urdf").mkdir()
    (root / "mjcf").mkdir()
    ET.ElementTree(robot).write(root / "urdf/robot.urdf")
    model = mujoco.MjModel.from_xml_path(str(root / "urdf/robot.urdf"))
    mujoco.mj_saveLastXML(str(root / "mjcf/robot.xml"), model)
    profile = {
        "schema_version": 1,
        "id": "probe",
        "label": "Probe",
        "robot_name": "probe",
        "description_env": "UNUSED_PROBE",
        "asset_directory": "unused",
        "urdf": "urdf/robot.urdf",
        "mjcf": "mjcf/robot.xml",
        "arms": arms,
        "home_joints": {},
        "input_to_base_rotation": [[1, 0, 0], [0, 1, 0], [0, 0, 1]],
        "head_joints": ["head_0", "head_1", "head_2"],
        "waist_joint": None,
        "leg_joints": [],
        "backend_adapter": "urdf-preview",
        "notes": "Inspection fixture",
        "pose_mode_default": "position",
        "allow_motion_without_collision_geometry": True,
    }
    files = {
        name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in ("urdf/robot.urdf", "mjcf/robot.xml")
    }
    return profile, files


@unittest.skipUnless(SESSION_TESTS, "Linux and TELEOP_PICO_PYTHON with a fixed wheel are required")
class PicoSessionTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.database = tempfile.TemporaryDirectory(prefix="pico-postgres-")
        cls.pg = pgserver.get_server(Path(cls.database.name) / "db", cleanup_mode="delete")

    @classmethod
    def tearDownClass(cls):
        cls.pg.cleanup()
        cls.database.cleanup()

    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="pico-session-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.schema = "pico_" + uuid.uuid4().hex
        sql = (ROOT / "deploy/platform/schema.sql").read_text().replace(':"platform_schema"', self.schema)
        with psycopg2.connect(self.pg.get_uri()) as db, db.cursor() as cursor:
            cursor.execute(f"CREATE SCHEMA {self.schema}")
            cursor.execute(sql)
            cursor.execute(f"UPDATE {self.schema}.admission SET state='open'")
            self.run_id = native_run_id("source")
            admission = RunAdmission.create(
                run_id=self.run_id,
                request_id="source-request",
                request={},
                parent_run_id=None,
                from_stage=None,
                created_at=datetime.now(UTC),
            )
            cursor.execute(
                f"INSERT INTO {self.schema}.run_admission(run_id,request_id,request,request_digest) "
                "VALUES(%s,%s,%s::jsonb,%s)",
                (self.run_id, admission.request_id, "{}", admission.request_digest),
            )
        self.store = SimulationStore(
            lambda: psycopg2.connect(self.pg.get_uri(), options=f"-csearch_path={self.schema}")
        )
        source = self.root / "model"
        source.mkdir()
        profile, files = model_source(source)
        self.artifacts = ArtifactStore(self.root / "cas")
        self.service = SimulationService(self.store, self.artifacts, self.root / "sessions", Path(WORKER))
        self.request = {
            "source": {
                "files": files,
                "entry": "mjcf/robot.xml",
                "engine": {"mujoco": "3.13.0"},
                "subject_sha256": digest(files),
                "verification_scope": "kinematic fixture",
                "portal_run_id": "source",
            },
            "profile": profile,
            "settings": {"position_scale": 0.1, "joint_speed_radps": 1},
            "record": True,
            "runtime": {},
        }
        warning_path = "reports/mjcf-certificate.json"
        warnings = ["Inspection fixture"]
        certificate = {
            "report": {
                "checks": [
                    {
                        "id": "provenance.warnings",
                        "details": {"warning_comments": warnings, "warning_sha256": digest(warnings)},
                    }
                ]
            }
        }
        (source / warning_path).parent.mkdir()
        certificate_bytes = canonical(certificate)
        (source / warning_path).write_bytes(certificate_bytes)
        files[warning_path] = hashlib.sha256(certificate_bytes).hexdigest()
        source_ref = self.artifacts.seal_tree(
            source, kind="pico_test_source", producer_run_id=None, producer_stage=None, producer_tool_sha256="c" * 64
        )
        self.members = {
            item["path"]: {**item, "artifact": source_ref.to_record()}
            for item in self.artifacts.verify(source_ref)["members"]
        }
        self.request["source"]["warning_inventory"] = text_warning_inventory(warnings, self.members[warning_path])
        self.read_member = lambda name, member: self.artifacts.read_metadata_member(
            ArtifactRef.from_record(member["artifact"]), name, max_bytes=64 * 1024 * 1024
        )[0]

    async def asyncTearDown(self):
        await self.service.close()

    def sealed_recording(self, sid):
        ref = ArtifactRef.from_record(self.store.get(sid)["recording_artifact"])
        self.artifacts.verify(ref)
        return self.artifacts.objects / ref.artifact_id

    def diagnostic_recording(self, sid):
        matches = list(self.artifacts.root.glob(f"session-result-{sid}-*/recording"))
        self.assertEqual(1, len(matches))
        return matches[0]

    async def native_rows(self, native, logical):
        manifest = json.loads((native / "session-manifest.json").read_bytes())
        member = manifest["files"][logical]
        self.assertEqual("gzip-jsonl", member["encoding"])
        self.assertEqual(
            {"raw": "raw-input.jsonl.gz", "records": "records.jsonl.gz"}[logical], member["name"]
        )
        self.assertEqual(member["sha256"], hashlib.sha256((native / member["name"]).read_bytes()).hexdigest())
        program = """import json,sys
from pathlib import Path
from teleop.robots.m3_0.session_evidence import iter_raw_input_evidence,iter_record_evidence
decoder = {"raw": iter_raw_input_evidence, "records": iter_record_evidence}[sys.argv[2]]
print(json.dumps(list(decoder(Path(sys.argv[1])))))
"""
        child = await asyncio.create_subprocess_exec(
            WORKER, "-I", "-B", "-c", program, str(native), logical,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(child.communicate(), 30)
        finally:
            if child.returncode is None:
                child.kill()
                await child.wait()
        self.assertEqual(0, child.returncode, stderr.decode())
        return json.loads(stdout)

    def register_cli_source_fixture(self):
        # Canonical fixture facts exercise CLI reads; they do not claim a production pipeline execution.
        command = StageCommand.create(
            command_id="pico-fixture-mjcf",
            run_id=self.run_id,
            stage="mjcf",
            idempotency_key="pico-cli-source",
            request={"run_id": self.run_id},
            input_artifacts=(),
            config_snapshot_sha256="c" * 64,
            contract_sha256="d" * 64,
            policy_sha256="e" * 64,
            expected_executor_sha256="f" * 64,
            predecessor_decision_id=None,
        )
        checks = {
            "command_id": command.command_id,
            "request_digest": command.request_digest,
            "checks": [{"id": "fixture.source", "state": "passed", "details": {"scope": "fixture"}}],
        }
        (self.root / "model/checks.json").write_bytes(canonical(checks) + b"\n")
        output = self.artifacts.seal_tree(
            self.root / "model",
            kind="stage_output:mjcf",
            producer_run_id=self.run_id,
            producer_stage="mjcf",
            producer_tool_sha256=command.expected_executor_sha256,
        )
        result = StageResult(
            schema_version=STAGE_RESULT_SCHEMA,
            command_id=command.command_id,
            run_id=self.run_id,
            stage="mjcf",
            request_digest=command.request_digest,
            outcome="completed",
            output_artifact=output,
            actual_executor_sha256=command.expected_executor_sha256,
            checks_sha256=digest(checks),
            resources_settled=True,
        )
        result_ref = self.artifacts.seal_stage_result(result)
        facts = FactStore(self.store.connect, self.artifacts)
        facts.register_artifact(output)
        facts.register_artifact(result_ref)
        with self.store.transaction() as cursor:
            cursor.execute(
                "INSERT INTO command(command_id,run_id,kind,stage,idempotency_key,request,request_digest,"
                "config_snapshot_sha256,contract_sha256,policy_sha256,expected_executor_sha256) "
                "VALUES(%s,%s,'stage','mjcf',%s,%s::jsonb,%s,%s,%s,%s,%s)",
                (
                    command.command_id,
                    self.run_id,
                    command.idempotency_key,
                    json.dumps(command.to_record()["request"]),
                    command.request_digest,
                    command.config_snapshot_sha256,
                    command.contract_sha256,
                    command.policy_sha256,
                    command.expected_executor_sha256,
                ),
            )
            cursor.execute(
                "INSERT INTO stage_decision(decision_id,run_id,stage,command_id,outcome,"
                "result_record_artifact_id,result_digest,actual_executor_sha256,decided_at) "
                "VALUES('pico-source-decision',%s,'mjcf',%s,'completed',%s,%s,%s,now())",
                (
                    self.run_id,
                    command.command_id,
                    result_ref.artifact_id,
                    result.digest,
                    command.expected_executor_sha256,
                ),
            )

    def canonical_projection(self):
        from description_pipeline.platform.queries import read_run_projection

        connection = self.store.connect()
        try:
            return read_run_projection(connection, self.run_id, artifacts=self.artifacts)
        finally:
            connection.close()

    def cli_config(self, python=WORKER):
        path = self.root / ("portal-cli-" + uuid.uuid4().hex + ".json")
        path.write_bytes(
            canonical(
                {
                    "portal": {
                        "simulation": {
                            "root": str(self.service.root),
                            "python": str(python),
                            "capacity": self.service.capacity,
                        }
                    }
                }
            )
        )
        return str(path)

    async def software_recording_for_cli(self):
        """Real source/CAS/PG fixture with explicit completion-proof doubles, not native qualification."""
        from tests.v1.test_pico_evidence import EvidenceFixture

        await self.start_service()
        row = self.store.create(
            "fixture-operator", "cli-software-recording", self.run_id, self.request, self.service.owner
        )
        session = self.service.root / row["session_id"]
        fixture = EvidenceFixture(session)
        recording_module.materialize_source(session / "source", self.request["source"], self.members, self.read_member)
        (session / "recording/request.json").write_bytes(canonical(self.request) + b"\n")
        (session / "recording/session.json").write_bytes(canonical(session_identity(row)) + b"\n")
        fixture.finalization_fixture()
        outcome = {
            "complete": True,
            "error": None,
            "container_complete": True,
            "session_complete": True,
            "formal_one_hour_qualified": False,
            "streams": {},
            "manifest_sha256": hashlib.sha256((fixture.native / "session-manifest.json").read_bytes()).hexdigest(),
            "finalization_verification": fixture.finalization_result_double(),
        }
        async with self.service._storage_gate:
            record = await asyncio.to_thread(
                self.service._store_recording_result, session, outcome, self.request["runtime"], True
            )
        row = self.store.transition(row["session_id"], self.service.owner, "failed", recording=record)
        self.register_cli_source_fixture()
        return row

    def cli_environment(self):
        environment = dict(os.environ)
        environment.pop("PLATFORM_OPERATOR_DSN", None)
        environment.pop("PLATFORM_SERVICE_DSN", None)
        environment["PLATFORM_ARTIFACT_ROOT"] = str(self.artifacts.root)
        environment["PLATFORM_SERVICE_DSN"] = getattr(
            self,
            "cli_service_dsn",
            psycopg2.extensions.make_dsn(self.pg.get_uri(), options=f"-csearch_path={self.schema}"),
        )
        if hasattr(self, "cli_operator_dsn"):
            environment["PLATFORM_OPERATOR_DSN"] = self.cli_operator_dsn
        return environment

    def install_deployed_cli_roles(self):
        """Replace only this private test schema using actual deployment ownership and grants."""
        from tests.v1.test_platform_schema_postgres import _sql, PlatformSchemaPostgresTests

        airflow_schema = "cli_airflow_" + uuid.uuid4().hex
        with self.store.connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT current_database()")
            database = cursor.fetchone()[0]
            cursor.execute(f'DROP SCHEMA "{self.schema}" CASCADE')
            cursor.execute(
                _sql("bootstrap.sql", platform_schema=self.schema, airflow_schema=airflow_schema, database=database)
            )
        fixture = PlatformSchemaPostgresTests()
        fixture.server, fixture.schema, fixture.airflow_schema, fixture.database = (
            self.pg,
            self.schema,
            airflow_schema,
            database,
        )
        fixture._create_airflow_tables()
        with (
            psycopg2.connect(
                self.pg.get_uri(), user="platform_migrate", options=f"-csearch_path={self.schema},pg_catalog"
            ) as connection,
            connection.cursor() as cursor,
        ):
            for name in ("schema.sql", "views.sql", "roles.sql"):
                cursor.execute(
                    _sql(name, platform_schema=self.schema, airflow_schema=airflow_schema, database=database)
                )
        with self.store.transaction() as cursor:
            cursor.execute("UPDATE admission SET state='open'")
            admission = RunAdmission.create(
                run_id=self.run_id,
                request_id="source-request",
                request={},
                parent_run_id=None,
                from_stage=None,
                created_at=datetime.now(UTC),
            )
            cursor.execute(
                "INSERT INTO run_admission(run_id,request_id,request,request_digest) VALUES(%s,%s,%s::jsonb,%s)",
                (self.run_id, admission.request_id, "{}", admission.request_digest),
            )
        self.cli_service_dsn = psycopg2.extensions.make_dsn(self.pg.get_uri(), user="platform_service")
        self.cli_operator_dsn = psycopg2.extensions.make_dsn(self.pg.get_uri(), user="operator_ro")
        with psycopg2.connect(self.cli_service_dsn) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT current_user")
            self.assertEqual("platform_service", cursor.fetchone()[0])

    async def launch_managed_cli(self, sid, python, *, environment=None):
        return await asyncio.create_subprocess_exec(
            sys.executable,
            "-B",
            "-m",
            "description_pipeline.simulation.verify_recording",
            "--session",
            sid,
            "--config",
            self.cli_config(python),
            env=self.cli_environment() if environment is None else environment,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

    async def test_managed_cli_config_path_records_real_exec_failure_and_releases_its_partial_and_slot(self):
        row = await self.software_recording_for_cli()
        sid = row["session_id"]
        ref = ArtifactRef.from_record(row["recording_artifact"])
        before = self.artifacts.verify(ref)
        child = await self.launch_managed_cli(sid, self.root / "missing-python")
        stdout, stderr = await asyncio.wait_for(child.communicate(), 15)
        self.assertNotEqual(0, child.returncode)
        self.assertIn("absolute fixed PICO", (stdout + stderr).decode())
        attempt = self.store.verification_attempts(sid)[-1]
        self.assertEqual("failed", attempt["result"]["state"])
        self.assertIsNone(attempt["result"]["process_identity"])
        self.assertFalse(self.service._verification_partial(attempt["command_id"], attempt["request"]).exists())
        self.assertEqual(before, self.artifacts.verify(ref))
        slot = self.store.acquire_verification_slot()
        self.assertIsNotNone(slot)
        slot.close()

    async def test_live_managed_cli_survives_real_service_restart_and_sigterm_cleans_its_exact_helper(self):
        await self.exercise_live_managed_cli_service_restart()

    async def exercise_live_managed_cli_service_restart(self):
        row = await self.software_recording_for_cli()
        sid = row["session_id"]
        wrapper = self.root / "blocked-native-entry"
        wrapper.write_text(
            "#!" + sys.executable + "\nimport os,sys\nos.execv(sys.executable, "
            "[sys.executable,'-I','-B','-c','import time;time.sleep(60)',*sys.argv[1:]])\n"
        )
        wrapper.chmod(0o700)
        await self.service.close()
        child = await self.launch_managed_cli(sid, wrapper)
        identity = None
        restarted = SimulationService(self.store, self.artifacts, self.service.root, Path(WORKER))
        try:
            async with asyncio.timeout(15):
                while True:
                    attempts = self.store.verification_attempts(sid)
                    if attempts and attempts[-1]["result"]["state"] == "running":
                        identity = attempts[-1]["result"]["process_identity"]
                        break
                    if child.returncode is not None:
                        stdout, stderr = await child.communicate()
                        self.fail((stdout + stderr).decode())
                    await asyncio.sleep(0.02)
            self.assertEqual("alive", resource_state(identity))
            await restarted.start()
            self.assertEqual("alive", resource_state(identity))
            with self.assertRaisesRegex(SimulationConflict, "capacity"):
                await restarted.verify_recording(sid, self.members, self.read_member)
            await restarted.close()

            self.assertEqual("alive", resource_state(identity))
            child.send_signal(signal.SIGTERM)
            stdout, stderr = await asyncio.wait_for(child.communicate(), 15)
            self.assertEqual(130, child.returncode, (stdout + stderr).decode())
            self.assertEqual(b"", stdout)
            attempt = self.store.verification_attempts(sid)[-1]
            self.assertEqual("cancelled", attempt["result"]["state"])
            self.assertEqual(identity, attempt["result"]["process_identity"])
            self.assertEqual("dead", resource_state(identity))
            self.assertFalse(restarted._verification_partial(attempt["command_id"], attempt["request"]).exists())
            slot = self.store.acquire_verification_slot()
            self.assertIsNotNone(slot)
            slot.close()
        finally:
            if child.returncode is None:
                child.kill()
                await child.wait()
            if identity is not None:
                await self.service._terminate_identity(identity)
            await restarted.close()

    async def test_managed_cli_deployed_roles_write_attempts_and_reject_missing_or_read_only_service_identity(self):
        self.install_deployed_cli_roles()
        row = await self.software_recording_for_cli()
        sid = row["session_id"]
        source = ArtifactRef.from_record(row["recording_artifact"])
        original = self.artifacts.verify(source)
        reader = SimulationStore(lambda: psycopg2.connect(self.cli_service_dsn))
        child = await self.launch_managed_cli(sid, self.root / "missing-role-python")
        stdout, stderr = await asyncio.wait_for(child.communicate(), 15)
        self.assertNotEqual(0, child.returncode)
        self.assertIn("absolute fixed PICO", (stdout + stderr).decode())
        attempts = reader.verification_attempts(sid)
        self.assertEqual(1, len(attempts))
        self.assertEqual("failed", attempts[0]["result"]["state"])
        self.assertIsNone(attempts[0]["result"]["process_identity"])

        for statement in (
            "SELECT * FROM simulation_command LIMIT 0",
            "UPDATE simulation_session SET error=error WHERE false",
        ):
            with psycopg2.connect(self.cli_operator_dsn) as connection:
                with self.assertRaises(psycopg2.errors.InsufficientPrivilege) as denied, connection.cursor() as cursor:
                    cursor.execute(statement)
                self.assertEqual("42501", denied.exception.pgcode)

        missing = self.cli_environment()
        missing.pop("PLATFORM_SERVICE_DSN")
        child = await self.launch_managed_cli(sid, Path(WORKER), environment=missing)
        stdout, stderr = await asyncio.wait_for(child.communicate(), 15)
        self.assertEqual(2, child.returncode)
        self.assertIn("PLATFORM_SERVICE_DSN is required", stderr.decode())
        self.assertEqual(attempts, reader.verification_attempts(sid))

        wrong_role = self.cli_environment()
        wrong_role["PLATFORM_SERVICE_DSN"] = self.cli_operator_dsn
        child = await self.launch_managed_cli(sid, Path(WORKER), environment=wrong_role)
        stdout, stderr = await asyncio.wait_for(child.communicate(), 15)
        self.assertNotEqual(0, child.returncode)
        self.assertIn("permission denied for table simulation_command", stderr.decode())
        self.assertEqual(attempts, reader.verification_attempts(sid))
        self.assertEqual(original, self.artifacts.verify(source))
        self.assertFalse(list(self.service.root.glob("recording-verification-*")))
        slot = reader.acquire_verification_slot()
        self.assertIsNotNone(slot)
        slot.close()

    async def test_managed_cli_deployed_service_role_records_running_and_cancelled_receipts(self):
        self.install_deployed_cli_roles()
        await self.exercise_live_managed_cli_service_restart()

    async def start_service(self):
        await self.service.start()
        self.request["runtime"] = self.service.runtime_identity

    async def exercise_fresh_parent_helper_drift(self, kind):
        import description_pipeline

        destination = Path(os.environ.get("PICO_PARENT_IMPORT_EVIDENCE", self.root)) / kind
        config = {
            "root": str(destination),
            "package": str(Path(description_pipeline.__file__).parent),
            "worker": WORKER,
            "kind": kind,
        }
        child = await asyncio.create_subprocess_exec(
            sys.executable,
            "-B",
            str(ROOT / "tests/v1/pico_parent_import_probe.py"),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(child.communicate(canonical(config)), 40)
        self.assertEqual(0, child.returncode, (stdout + stderr).decode())
        result = json.loads(stdout)
        self.assertFalse(result["marker_executed"])
        self.assertTrue(result["original_release_inventory_preserved"])
        self.assertFalse(result["parent_runtime_imported_after"])
        self.assertFalse(result["parent_recording_imported_after"])
        if kind == "cold_runtime":
            self.assertIn("Installed release files differ", result["error"])
            self.assertEqual(0, result["helpers"])
        else:
            self.assertIn("helper bytes changed", result["error"])
            self.assertEqual(result["original_helper_sha256"], result["expected_helper_sha256"])
            self.assertEqual(1, result["helpers"])
            self.assertTrue(result["helpers_dead"])
            self.assertFalse(result["parent_runtime_imported_before"])
            self.assertFalse(result["parent_recording_imported_before"])

    async def test_active_session_ids_keep_unresolved_capacity_and_exclude_terminal_rows(self):
        first = self.store.create("fixture-operator", "capacity-one", self.run_id, self.request, self.service.owner)
        second = self.store.create("fixture-operator", "capacity-two", self.run_id, self.request, self.service.owner)
        self.assertEqual({first["session_id"], second["session_id"]}, set(self.store.active_session_ids()))
        self.store.transition(first["session_id"], self.service.owner, "unresolved", error="Fixture unknown ownership")
        self.store.transition(second["session_id"], self.service.owner, "failed", error="Fixture confirmed failure")
        self.assertEqual([first["session_id"]], self.store.active_session_ids())

    async def test_live_cli_slot_prevents_reconciliation_and_owner_crash_allows_exact_group_cleanup(self):
        from description_pipeline.simulation.verification import ManagedRecordingVerification

        row = self.store.create("fixture-operator", "live-cli-slot", self.run_id, self.request, self.service.owner)
        recording = self.root / "slot-recording"
        recording.mkdir()
        (recording / "scope.txt").write_text(
            "PG slot and process ownership fixture; no business or native qualification"
        )
        ref = self.artifacts.seal_tree(
            recording,
            kind="teleop_recording",
            producer_run_id=None,
            producer_stage=None,
            producer_tool_sha256=digest(self.request["runtime"]),
        )
        row = self.store.transition(row["session_id"], self.service.owner, "failed", recording=ref.to_record())
        command = "verify-crashed-cli"
        request = {
            "type": "verify_recording",
            "binding": recording_binding(row),
            "partial": "recording-verification-" + command,
            "execution": self.service._consumer_execution,
        }
        partial = self.service._verification_partial(command, request)
        partial.mkdir(parents=True)
        (partial / "source.bin").write_bytes(b"Original partial bytes")
        child = await asyncio.create_subprocess_exec(
            sys.executable,
            "-B",
            str(ROOT / "tests/v1/pico_verification_slot_owner.py"),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        identity = None
        try:
            payload = {
                "dsn": self.pg.get_uri(),
                "schema": self.schema,
                "session": row["session_id"],
                "command": command,
                "request": request,
                "owner": "separate-cli-owner",
                "nonce": uuid.uuid4().hex,
            }
            child.stdin.write(canonical(payload))
            await child.stdin.drain()
            # The owner reads one complete request, then keeps its existing PG session lock.
            child.stdin.write(b"\n")
            await child.stdin.drain()
            identity = json.loads(await asyncio.wait_for(child.stdout.readline(), 10))
            self.assertEqual("alive", resource_state(identity))
            original = self.store.verification_attempts(row["session_id"])
            contender = ManagedRecordingVerification(self.store, self.artifacts, self.service.root, Path(WORKER))
            await contender._reconcile_verifications()
            self.assertEqual(original, self.store.verification_attempts(row["session_id"]))
            self.assertEqual(b"Original partial bytes", (partial / "source.bin").read_bytes())
            with self.assertRaisesRegex(SimulationConflict, "capacity"):
                await contender.verify_recording(row["session_id"], {}, lambda *_arguments: None)
            self.assertEqual("alive", resource_state(identity))
            child.kill()
            await child.wait()
            async with asyncio.timeout(10):
                while self.store.verification_attempts(row["session_id"])[0]["result"]["state"] != "failed":
                    await contender._reconcile_verifications()
                    await asyncio.sleep(0.02)
            actual = self.store.verification_attempts(row["session_id"])[0]
            self.assertEqual(request, actual["request"])
            self.assertEqual(identity, actual["result"]["process_identity"])
            self.assertEqual("dead", resource_state(identity))
            self.assertFalse(partial.exists())
            slot = self.store.acquire_verification_slot()
            self.assertIsNotNone(slot)
            slot.close()
        finally:
            if child.returncode is None:
                child.kill()
                await child.wait()
            if identity is not None:
                await self.service._terminate_identity(identity)

    async def test_repeated_cancellation_waits_for_the_actual_verification_slot_connection(self):
        from unittest.mock import patch
        from description_pipeline.simulation.verification import ManagedRecordingVerification

        entered, release = threading.Event(), threading.Event()
        acquired = {}
        actual = self.store.acquire_verification_slot

        def delayed():
            connection = actual()
            acquired["connection"] = connection
            entered.set()
            if not release.wait(5):
                connection.close()
                raise TimeoutError("Verification slot test release was not delivered")
            return connection

        managed = ManagedRecordingVerification(self.store, self.artifacts, self.service.root, Path(WORKER))
        with patch.object(self.store, "acquire_verification_slot", delayed):
            checking = asyncio.create_task(managed.verify_recording("not-admitted", {}, lambda *_arguments: None))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 5))
                for _ in range(3):
                    checking.cancel()
                    await asyncio.sleep(0.02)
                    self.assertFalse(checking.done())
                    self.assertFalse(acquired["connection"].closed)
                self.assertIsNone(actual())
            finally:
                release.set()
            with self.assertRaises(asyncio.CancelledError):
                await checking
        self.assertTrue(acquired["connection"].closed)
        self.assertEqual([], self.store.verification_attempts())
        replacement = actual()
        self.assertIsNotNone(replacement)
        replacement.close()

    async def test_fresh_parent_cold_runtime_import_rejects_marker_before_execution(self):
        await self.exercise_fresh_parent_helper_drift("cold_runtime")

    async def test_fresh_parent_late_runtime_drift_keeps_original_inventory_and_cold_gate(self):
        await self.exercise_fresh_parent_helper_drift("late_runtime")

    async def test_fresh_parent_late_recording_import_rejects_marker_before_execution(self):
        await self.exercise_fresh_parent_helper_drift("late_recording")

    @contextmanager
    def synthetic_release_consumer(self):
        from unittest.mock import patch

        directory = self.root / "synthetic-release-consumer"
        directory.mkdir()
        inventory = {}
        for name in ("runtime.py", "verify_recording.py", "teleop-runtime.json"):
            content = Path(recording_module.__file__).with_name(name).read_bytes()
            (directory / name).write_bytes(content)
            inventory["simulation/" + name] = hashlib.sha256(content).hexdigest()
        # Only the existing product inventory trust boundary is a fixture.
        # Hashing, child execution, stdin, ownership and marker refusal are real.
        tool = {"release": {"package_files": inventory}, "source_sha256": digest(inventory)}
        with (
            patch.object(recording_module, "__file__", str(directory / "recording.py")),
            patch("description_pipeline.runtime.tool_record", return_value=tool),
        ):
            yield directory

    async def test_cold_consumer_helper_marker_is_rejected_by_original_release_inventory(self):
        marker = self.root / "cold-helper-executed"
        with self.synthetic_release_consumer() as directory:
            runtime = directory / "runtime.py"
            runtime.write_bytes(runtime.read_bytes() + f"\nPath({str(marker)!r}).write_text('executed')\n".encode())
            with self.assertRaisesRegex(ValueError, "installed release inventory"):
                SimulationService(self.store, self.artifacts, self.service.root, Path(WORKER))
        self.assertFalse(marker.exists())

    async def test_startup_gate_refuses_constructor_late_drift_before_cold_top_level_marker(self):
        marker = self.root / "late-runtime-executed"
        with self.synthetic_release_consumer() as directory:
            self.service = SimulationService(self.store, self.artifacts, self.service.root, Path(WORKER))
            basis = deepcopy(self.service._consumer_execution)
            runtime = directory / "runtime.py"
            runtime.write_bytes(runtime.read_bytes() + f"\nPath({str(marker)!r}).write_text('executed')\n".encode())
            with self.assertRaisesRegex(ValueError, "Runtime helper bytes changed"):
                await self.service.start()
            self.assertEqual(basis, self.service._consumer_execution)
            self.assertIsNone(self.service.runtime_identity)
            self.assertIsNone(self.service._lock)
            self.assertIsNone(self.service._probe)
            self.assertEqual(b"", (self.service.root / "supervisor.lock").read_bytes())
        self.assertFalse(marker.exists())

    async def test_recording_gate_reuses_constructor_release_basis_after_helper_drift(self):
        marker = self.root / "late-recording-executed"
        with self.synthetic_release_consumer() as directory:
            self.service = SimulationService(self.store, self.artifacts, self.service.root, Path(WORKER))
            await self.start_service()
            row = await self.service.create(
                "app:tenant:operator", "recording-drift", self.run_id, self.request, self.members, self.read_member
            )
            sid = row["session_id"]
            await self.wait_ready(sid)
            await self.service.stop(sid)
            original = self.store.get(sid)["recording_artifact"]
            helper = directory / "verify_recording.py"
            helper.write_bytes(helper.read_bytes() + f"\nPath({str(marker)!r}).write_text('executed')\n".encode())
            with self.assertRaisesRegex(ValueError, "Recording helper bytes changed"):
                await self.service.verify_recording(sid, self.members, self.read_member)
            attempt = self.store.verification_attempts(sid)[-1]
            self.assertEqual("failed", attempt["result"]["state"])
            self.assertEqual("dead", resource_state(attempt["result"]["process_identity"]))
            self.assertEqual(original, self.store.get(sid)["recording_artifact"])
        self.assertFalse(marker.exists())

    async def test_failed_supervisor_probe_releases_root_ownership(self):
        import fcntl

        self.service.python = Path(sys.executable)
        with self.assertRaisesRegex(ValueError, "Teleop runtime identity failed"):
            await self.service.start()
        with (self.service.root / "supervisor.lock").open("a") as lease:
            fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.assertIsNone(self.service._lock)
        self.assertIsNone(self.service.runtime_identity)

    async def test_probe_definite_exec_failure_clears_intent_before_releasing_ownership(self):
        interpreter = self.root / "nonexecutable-worker"
        interpreter.write_text("Fixture executable permission failure\n")
        interpreter.chmod(0o600)
        self.service.python = interpreter
        with self.assertRaises(PermissionError):
            await self.service.start()
        self.assertEqual(b"", (self.service.root / "supervisor.lock").read_bytes())
        self.assertIsNone(self.service._probe)
        self.assertIsNone(self.service._lock)
        self.assertIsNone(self.service._database_lock)

    async def test_fast_exit_probe_without_full_identity_retains_original_ownership(self):
        from unittest.mock import patch

        self.service.python = Path("/bin/false").resolve()
        spawn = asyncio.create_subprocess_exec
        spawned = []

        async def reaped_spawn(*args, **options):
            process = await spawn(*args, **options)
            spawned.append(process)
            await process.wait()
            return process

        try:
            with (
                patch("description_pipeline.simulation.service.asyncio.create_subprocess_exec", reaped_spawn),
                self.assertRaisesRegex(SimulationConflict, "Startup process group is unresolved"),
            ):
                await self.service.start()
            self.assertIsNotNone(self.service._lock)
            record = json.loads((self.service.root / "supervisor.lock").read_bytes())
            self.assertEqual(spawned[0].pid, record["process_identity"]["pid"])
            self.assertNotIn("start_ticks", record["process_identity"])
            self.assertIsNone(self.service.runtime_identity)
        finally:
            if spawned:
                self.assertIsNotNone(spawned[0].returncode)
                with self.assertRaises(ProcessLookupError):
                    os.killpg(spawned[0].pid, 0)
                # Only the fixture clears its own proven-absent fast-exit sample.
                self.service._probe_identity = None
                self.service._write_probe_identity()
                await self.service.close()

    async def test_probe_vendor_import_waits_for_parent_identity_registration(self):
        from unittest.mock import patch

        self.service.python = Path(sys.executable)
        entered, release = asyncio.Event(), asyncio.Event()
        spawn = asyncio.create_subprocess_exec
        spawned = []

        async def held_response(*args, **options):
            process = await spawn(*args, **options)
            spawned.append((process, process_identity(process.pid, args[-1])))
            entered.set()
            await release.wait()
            return process

        with patch("description_pipeline.simulation.service.asyncio.create_subprocess_exec", held_response):
            starting = asyncio.create_task(self.service.start())
            try:
                await asyncio.wait_for(entered.wait(), 5)
                await asyncio.sleep(0.1)
                self.assertIsNone(spawned[0][0].returncode)
                self.assertEqual("alive", resource_state(spawned[0][1]))
                pending = json.loads((self.service.root / "supervisor.lock").read_bytes())
                self.assertEqual({"nonce"}, set(pending["process_identity"]))
            finally:
                release.set()
            with self.assertRaisesRegex(ValueError, "Teleop runtime identity failed"):
                await starting
        self.assertEqual("dead", resource_state(spawned[0][1]))
        self.assertIsNone(self.service._lock)

    async def test_inline_warning_inventory_is_rejected_before_database_admission(self):
        await self.start_service()
        request = {**self.request, "source": {**self.request["source"], "warnings": ["Inspection fixture"]}}
        with self.assertRaisesRegex(ValueError, "Inline warning inventories are unsupported"):
            await self.service.create(
                "app:tenant:operator",
                "inline-warning-copy",
                self.run_id,
                request,
                self.members,
                self.read_member,
            )
        with self.store.transaction() as cursor:
            cursor.execute("SELECT count(*) FROM simulation_session WHERE request_key='inline-warning-copy'")
            self.assertEqual(0, cursor.fetchone()[0])

    async def test_cancelled_supervisor_acquisition_releases_real_database_lease(self):
        from unittest.mock import patch

        entered, release = threading.Event(), threading.Event()
        acquired = {}
        acquire = self.store.acquire_supervisor

        def delayed_acquire():
            connection = acquire()
            acquired["connection"] = connection
            entered.set()
            if not release.wait(5):
                connection.close()
                raise TimeoutError("Fixture database acquisition was not released")
            return connection

        try:
            with patch.object(self.store, "acquire_supervisor", delayed_acquire):
                starting = asyncio.create_task(self.service.start())
                try:
                    self.assertTrue(await asyncio.to_thread(entered.wait, 10))
                    for _ in range(3):
                        starting.cancel()
                        await asyncio.sleep(0.02)
                        self.assertFalse(starting.done())
                        self.assertFalse(acquired["connection"].closed)
                finally:
                    release.set()
                    await asyncio.gather(starting, return_exceptions=True)
                self.assertTrue(starting.cancelled())
            self.assertTrue(acquired["connection"].closed)
            self.assertIsNone(self.service._lock)
            self.assertIsNone(self.service.runtime_identity)
            replacement = await asyncio.to_thread(acquire)
            replacement.close()
        finally:
            release.set()
            if "connection" in acquired:
                acquired["connection"].close()

    async def test_orphan_reconciliation_preserves_unknown_and_recording_identity(self):
        previous_owner = "previous-inspection-supervisor"
        unresolved = self.store.create(
            "app:tenant:operator",
            "orphan-without-process",
            self.run_id,
            {**self.request, "record": False},
            previous_owner,
        )
        unissued = {"type": "estop", "command_id": "unissued-orphan"}
        self.store.authorize_command(unresolved["session_id"], unissued["command_id"], "app:tenant:operator", unissued)
        previous = self.store.create(
            "app:tenant:operator",
            "orphan-with-recording",
            self.run_id,
            self.request,
            previous_owner,
        )
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-I",
            "-c",
            "import sys;sys.stdin.buffer.read(1)",
            previous_owner,
            stdin=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        try:
            identity = process_identity(process.pid, previous_owner)
            self.store.transition(previous["session_id"], previous_owner, "starting", process=identity)
        finally:
            process.stdin.close()
            await process.wait()
        recording = self.service.root / previous["session_id"] / "recording"
        recording.mkdir(parents=True)
        (recording / "request.json").write_bytes(canonical(previous["request"]))
        (recording / "session.json").write_bytes(canonical(session_identity(previous)))
        (recording / "native").mkdir()
        (recording / "native/records.jsonl").write_bytes(b"")

        await self.start_service()
        with self.subTest(branch="missing-process-identity"):
            self.assertEqual("unresolved", self.store.get(unresolved["session_id"])["state"])
            self.assertEqual("not_sent", self.store.worker_commands(unresolved["session_id"])[0]["result"]["state"])
        recovered = self.store.get(previous["session_id"])
        self.assertEqual("failed", recovered["state"])
        self.assertEqual(previous_owner, recovered["owner_nonce"])
        ref = ArtifactRef.from_record(recovered["recording_artifact"])
        with self.subTest(branch="recording-producer-identity"):
            self.assertEqual(digest(previous["request"]["runtime"]), ref.producer_tool_sha256)
        self.artifacts.verify(ref)
        sealed = self.artifacts.objects / ref.artifact_id
        self.assertEqual(session_identity(previous), json.loads((sealed / "session.json").read_bytes()))
        self.assertFalse(json.loads((sealed / "outcome.json").read_bytes())["complete"])

    @asynccontextmanager
    async def exited_leader_group(self, *, marked, owner=None, register=True, exit_leader=True):
        """Own and reap a real descendant without changing the host reaper."""
        libc = ctypes.CDLL(None, use_errno=True)
        previous = ctypes.c_int()
        self.assertEqual(0, libc.prctl(37, ctypes.byref(previous), 0, 0, 0))
        self.assertEqual(0, libc.prctl(36, 1, 0, 0, 0))
        process = None
        descriptor = None
        reaping = None
        try:
            program = """import os,signal,sys,time
read_fd,write_fd=os.pipe()
child=os.fork()
if child==0:
 os.close(read_fd)
 if sys.argv[2]=='unmarked':
  os.set_inheritable(write_fd,True)
  script='import os,signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);'
  script+='os.write('+str(write_fd)+',b"r");os.close('+str(write_fd)+');time.sleep(60)'
  os.execv(sys.executable,[sys.executable,'-I','-c',script])
 signal.signal(signal.SIGTERM,signal.SIG_IGN)
 os.write(write_fd,b'r');os.close(write_fd)
 while True: time.sleep(1)
os.close(write_fd);os.read(read_fd,1);os.close(read_fd)
print(child,flush=True)
sys.stdin.buffer.read(1)
"""
            nonce = uuid.uuid4().hex
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-I",
                "-B",
                "-c",
                program,
                nonce,
                "marked" if marked else "unmarked",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
            child = int(await asyncio.wait_for(process.stdout.readline(), 3))
            descriptor = os.pidfd_open(child)
            identity = process_identity(process.pid, nonce)
            row = None
            if register:
                row = self.store.create(
                    "app:tenant:operator",
                    nonce,
                    self.run_id,
                    self.request,
                    owner or self.service.owner,
                )
                self.store.transition(row["session_id"], row["owner_nonce"], "starting", process=identity)
                self.store.transition(row["session_id"], row["owner_nonce"], "stopping")
            recording = self.service.root / (row["session_id"] if row else nonce) / "recording"
            recording.mkdir(parents=True)
            (recording / "request.json").write_bytes(canonical(self.request))
            if row is not None:
                (recording / "session.json").write_bytes(canonical(session_identity(row)))
            (recording / "native").mkdir()
            (recording / "native/records.jsonl").write_bytes(b"original partial\n")
            current = RunningSession(process, recording.parent, identity=identity)
            if exit_leader:
                process.stdin.close()
                await self.service._wait_leader(current, 3)
                self.assertEqual(0, process.returncode)
            self.assertEqual("alive" if marked else "unknown", resource_state(identity))

            async def reap_descendant():
                await self.service._wait_leader(current, 60)
                return await asyncio.to_thread(os.waitpid, child, 0)

            reaping = asyncio.create_task(reap_descendant())
            yield row, current, child
        finally:
            if descriptor is not None:
                with suppress(ProcessLookupError):
                    signal.pidfd_send_signal(descriptor, signal.SIGKILL)
                os.close(descriptor)
            if process is not None and process.returncode is None:
                process.kill()
            if reaping is not None:
                await asyncio.wait_for(asyncio.shield(reaping), 5)
            if process is not None:
                await asyncio.wait_for(process.wait(), 5)
            self.assertEqual(0, libc.prctl(36, previous.value, 0, 0, 0))

    async def test_exited_leader_owned_descendant_is_reaped_before_sealing(self):
        await self.start_service()
        async with self.exited_leader_group(marked=True) as (row, current, child):
            sid = row["session_id"]
            self.service.running[sid] = current
            current.tasks = [
                asyncio.create_task(self.service._read(sid)),
                asyncio.create_task(self.service._watch(sid)),
            ]
            await asyncio.wait_for(current.tasks[0], 10)
            settled = self.store.get(sid)
            self.assertEqual("failed", settled["state"])
            self.assertEqual("dead", resource_state(current.identity))
            self.assertFalse(Path(f"/proc/{child}").exists())
            self.assertIsNotNone(settled["recording_artifact"])
            self.assertFalse(json.loads((self.sealed_recording(sid) / "outcome.json").read_bytes())["complete"])
            self.assertNotIn(sid, self.service.running)

    async def test_unknown_group_retains_partial_identity_and_capacity_until_reconciled(self):
        await self.start_service()
        async with self.exited_leader_group(marked=False) as (row, current, child):
            sid = row["session_id"]
            self.service.running[sid] = current
            current.tasks = [
                asyncio.create_task(self.service._read(sid)),
                asyncio.create_task(self.service._watch(sid)),
            ]
            await asyncio.wait_for(current.tasks[0], 10)
            unresolved = self.store.get(sid)
            self.assertEqual("unresolved", unresolved["state"])
            self.assertEqual(current.identity, unresolved["process_identity"])
            self.assertIsNone(unresolved["recording_artifact"])
            self.assertTrue(Path(f"/proc/{child}").exists())
            self.assertEqual(b"original partial\n", (current.root / "recording/native/records.jsonl").read_bytes())
            self.assertFalse((current.root / "recording/outcome.json").exists())
            with self.assertRaisesRegex(SimulationConflict, "capacity"):
                self.store.create(
                    "app:tenant:operator",
                    "capacity-after-unknown",
                    self.run_id,
                    self.request,
                    self.service.owner,
                    capacity=1,
                )
            with self.assertRaisesRegex(SimulationConflict, "unresolved"):
                await self.service.stop(sid)
            self.assertEqual("unresolved", self.store.get(sid)["state"])
        await self.service.stop(sid)
        reconciled = self.store.get(sid)
        self.assertEqual("failed", reconciled["state"])
        self.assertIsNotNone(reconciled["recording_artifact"])
        self.assertFalse(json.loads((self.sealed_recording(sid) / "outcome.json").read_bytes())["complete"])

    async def test_orphan_with_exited_leader_settles_only_after_owned_descendant_reaped(self):
        async with self.exited_leader_group(marked=True, owner="previous-supervisor") as (row, current, child):
            await self.start_service()
            settled = self.store.get(row["session_id"])
            self.assertEqual("failed", settled["state"])
            self.assertEqual("dead", resource_state(current.identity))
            self.assertFalse(Path(f"/proc/{child}").exists())
            self.assertIsNotNone(settled["recording_artifact"])

    async def test_unknown_orphan_preserves_partial_until_later_supervisor_reconciliation(self):
        async with self.exited_leader_group(marked=False, owner="previous-supervisor") as (row, current, child):
            await self.start_service()
            unresolved = self.store.get(row["session_id"])
            self.assertEqual("unresolved", unresolved["state"])
            self.assertEqual(current.identity, unresolved["process_identity"])
            self.assertIsNone(unresolved["recording_artifact"])
            self.assertTrue(Path(f"/proc/{child}").exists())
            self.assertFalse((current.root / "recording/outcome.json").exists())
        await self.service.close()
        await self.start_service()
        settled = self.store.get(row["session_id"])
        self.assertEqual("failed", settled["state"])
        self.assertIsNotNone(settled["recording_artifact"])

    async def test_cancellation_during_real_spawn_captures_identity_and_reaps_worker(self):
        from unittest.mock import patch

        await self.start_service()
        entered, release = asyncio.Event(), asyncio.Event()
        actual_spawn = asyncio.create_subprocess_exec
        spawned = []

        async def delayed_spawn(*args, **options):
            if args[-2] != "teleop.backends.mujoco.upper_body_worker":
                return await actual_spawn(*args, **options)
            process = await actual_spawn(*args, **options)
            spawned.append(process)
            entered.set()
            await release.wait()
            return process

        with patch("description_pipeline.simulation.service.asyncio.create_subprocess_exec", delayed_spawn):
            creating = asyncio.create_task(
                self.service.create(
                    "app:tenant:operator",
                    "cancel-real-spawn",
                    self.run_id,
                    self.request,
                    self.members,
                    self.read_member,
                )
            )
            try:
                await asyncio.wait_for(entered.wait(), 5)
                creating.cancel()
                await asyncio.sleep(0)
            finally:
                release.set()
            with self.assertRaises(asyncio.CancelledError):
                await creating
        with self.store.transaction() as cursor:
            cursor.execute("SELECT session_id FROM simulation_session WHERE request_key='cancel-real-spawn'")
            sid = cursor.fetchone()[0]
        settled = self.store.get(sid)
        self.assertEqual("failed", settled["state"])
        self.assertEqual(spawned[0].pid, settled["process_identity"]["pid"])
        self.assertEqual("dead", resource_state(settled["process_identity"]))
        self.assertIsNotNone(spawned[0].returncode)
        self.assertIsNotNone(settled["recording_artifact"])
        self.assertEqual(1, len(spawned))
        self.assertFalse(json.loads((self.sealed_recording(sid) / "outcome.json").read_bytes())["complete"])
        self.assertFalse(self.service.running)

    async def test_cancelled_probe_spawn_retains_handle_until_group_is_reaped(self):
        from unittest.mock import patch

        entered, release = asyncio.Event(), asyncio.Event()
        actual_spawn = asyncio.create_subprocess_exec
        spawned = []

        async def delayed_probe(*args, **options):
            process = await actual_spawn(
                sys.executable,
                "-I",
                "-c",
                "import time;time.sleep(60)",
                args[-1],
                **options,
            )
            spawned.append(process)
            entered.set()
            await release.wait()
            return process

        with patch("description_pipeline.simulation.service.asyncio.create_subprocess_exec", delayed_probe):
            starting = asyncio.create_task(self.service.start())
            try:
                await asyncio.wait_for(entered.wait(), 5)
                for _ in range(3):
                    starting.cancel()
                    await asyncio.sleep(0.02)
                    self.assertFalse(starting.done())
            finally:
                release.set()
            with self.assertRaises(asyncio.CancelledError):
                await starting
        self.assertIsNotNone(spawned[0].returncode)
        self.assertFalse(Path(f"/proc/{spawned[0].pid}").exists())
        self.assertIsNone(self.service._lock)
        self.assertIsNone(self.service._probe_identity)
        self.assertEqual("", (self.service.root / "supervisor.lock").read_text())

    async def exercise_close_during_worker_spawn(self, *, cancel):
        from unittest.mock import patch

        await self.start_service()
        entered, release = asyncio.Event(), asyncio.Event()
        actual_spawn = asyncio.create_subprocess_exec
        spawned = []

        async def delayed_spawn(*args, **options):
            if args[-2] != "teleop.backends.mujoco.upper_body_worker":
                return await actual_spawn(*args, **options)
            process = await actual_spawn(*args, **options)
            spawned.append((process, process_identity(process.pid, args[-1])))
            entered.set()
            await release.wait()
            return process

        closing = None
        with patch("description_pipeline.simulation.service.asyncio.create_subprocess_exec", delayed_spawn):
            creating = asyncio.create_task(
                self.service.create(
                    "app:tenant:operator",
                    "close-during-real-spawn",
                    self.run_id,
                    self.request,
                    self.members,
                    self.read_member,
                )
            )
            try:
                await asyncio.wait_for(entered.wait(), 5)
                closing = asyncio.create_task(self.service.close())
                await asyncio.sleep(0)
                self.assertFalse(closing.done())
                self.assertIsNotNone(self.service._lock)
                self.assertIsNotNone(self.service._database_lock)
                self.assertIsNotNone(self.service.runtime_identity)
                self.assertEqual("alive", resource_state(spawned[0][1]))
                with self.assertRaisesRegex(SimulationConflict, "not accepting"):
                    await self.service.create(
                        "app:tenant:operator",
                        "late-session",
                        self.run_id,
                        self.request,
                        self.members,
                        self.read_member,
                    )
                if cancel:
                    creating.cancel()
            finally:
                release.set()
            with self.assertRaises(asyncio.CancelledError if cancel else SimulationConflict):
                await creating
            await asyncio.wait_for(closing, 10)
        with self.store.transaction() as cursor:
            cursor.execute("SELECT session_id FROM simulation_session WHERE request_key='close-during-real-spawn'")
            sid = cursor.fetchone()[0]
        self.assertEqual("failed", self.store.get(sid)["state"])
        self.assertEqual(1, len(spawned))
        self.assertEqual(spawned[0][1], self.store.get(sid)["process_identity"])
        self.assertEqual("dead", resource_state(spawned[0][1]))
        self.assertIsNone(self.service._lock)
        self.assertIsNone(self.service._database_lock)
        self.assertIsNone(self.service.runtime_identity)
        self.assertFalse(self.service.running)
        await self.service.close()

    async def test_close_waits_for_real_worker_spawn_before_releasing_ownership(self):
        await self.exercise_close_during_worker_spawn(cancel=False)

    async def test_close_and_cancel_wait_for_real_worker_spawn_before_releasing_ownership(self):
        await self.exercise_close_during_worker_spawn(cancel=True)

    async def test_close_waits_for_real_probe_spawn_before_releasing_startup_lock(self):
        from unittest.mock import patch

        entered, release = asyncio.Event(), asyncio.Event()
        actual_spawn = asyncio.create_subprocess_exec
        spawned = []

        async def delayed_probe(*args, **options):
            process = await actual_spawn(
                sys.executable,
                "-I",
                "-c",
                "import time;time.sleep(60)",
                args[-1],
                **options,
            )
            spawned.append((process, process_identity(process.pid, args[-1])))
            entered.set()
            await release.wait()
            return process

        with patch("description_pipeline.simulation.service.asyncio.create_subprocess_exec", delayed_probe):
            starting = asyncio.create_task(self.service.start())
            try:
                await asyncio.wait_for(entered.wait(), 5)
                closing = asyncio.create_task(self.service.close())
                await asyncio.sleep(0)
                self.assertFalse(closing.done())
                self.assertIsNotNone(self.service._lock)
                self.assertEqual("alive", resource_state(spawned[0][1]))
                for _ in range(3):
                    closing.cancel()
                    await asyncio.sleep(0.02)
                    self.assertFalse(closing.done())
                    self.assertIsNotNone(self.service._lock)
            finally:
                release.set()
            with self.assertRaisesRegex(SimulationConflict, "closed during startup"):
                await starting
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(closing, 10)
        self.assertIsNone(self.service._lock)
        self.assertEqual("dead", resource_state(spawned[0][1]))
        await self.service.close()

    async def test_close_and_cancel_wait_for_database_admission_before_releasing_ownership(self):
        from unittest.mock import patch

        await self.start_service()
        entered, release = threading.Event(), threading.Event()
        admit = self.store.create
        admitted = []

        def delayed_admission(*args, **kwargs):
            row = admit(*args, **kwargs)
            admitted.append(row)
            entered.set()
            if not release.wait(5):
                raise TimeoutError("Fixture admission was not released")
            return row

        with patch.object(self.store, "create", delayed_admission):
            creating = asyncio.create_task(
                self.service.create(
                    "app:tenant:operator",
                    "cancel-pg-admission",
                    self.run_id,
                    self.request,
                    self.members,
                    self.read_member,
                )
            )
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 5))
                creating.cancel()
                closing = asyncio.create_task(self.service.close())
                await asyncio.sleep(0)
                self.assertFalse(closing.done())
                self.assertIsNotNone(self.service._database_lock)
            finally:
                release.set()
            with self.assertRaises(asyncio.CancelledError):
                await creating
            await asyncio.wait_for(closing, 10)
        settled = self.store.get(admitted[0]["session_id"])
        self.assertEqual("failed", settled["state"])
        self.assertIsNone(settled["process_identity"])
        self.assertIsNone(self.service._database_lock)
        self.assertIsNone(self.service._lock)

    async def test_unknown_probe_retains_startup_lock_and_original_group_across_retry(self):
        async with self.exited_leader_group(marked=False, register=False) as (_, current, child):
            record = {"schema_version": "solidworks-to-urdf.simulation-probe/v1", "process_identity": current.identity}
            lock_path = self.service.root / "supervisor.lock"
            lock_path.write_bytes(canonical(record))
            with self.assertRaisesRegex(SimulationConflict, "Startup process group is unresolved"):
                await self.service.start()
            self.assertIsNotNone(self.service._lock)
            self.assertEqual(record, json.loads(lock_path.read_bytes()))
            self.service._probe_identity = {**current.identity, "nonce": "changed-operation"}
            with self.assertRaisesRegex(SimulationConflict, "operation identity cannot change"):
                self.service._write_probe_identity()
            self.assertEqual(record, json.loads(lock_path.read_bytes()))
            self.service._probe_identity = current.identity
            self.assertTrue(Path(f"/proc/{child}").exists())
            replacement = SimulationService(self.store, self.artifacts, self.service.root, Path(WORKER))
            with self.assertRaisesRegex(ValueError, "Another simulation supervisor"):
                await replacement.start()
            self.assertIsNone(replacement._lock)
            with self.assertRaisesRegex(SimulationConflict, "already started"):
                await self.service.start()
        await self.service.close()
        await self.start_service()
        self.assertEqual("", lock_path.read_text())

    async def test_owned_probe_orphan_is_reaped_before_a_new_runtime_probe(self):
        async with self.exited_leader_group(marked=True, register=False) as (_, current, child):
            (self.service.root / "supervisor.lock").write_bytes(
                canonical(
                    {
                        "schema_version": "solidworks-to-urdf.simulation-probe/v1",
                        "process_identity": current.identity,
                    }
                )
            )
            await self.start_service()
            self.assertFalse(Path(f"/proc/{child}").exists())
            self.assertEqual("dead", resource_state(current.identity))
            self.assertEqual("", (self.service.root / "supervisor.lock").read_text())

    async def test_probe_lost_spawn_response_remains_unknown_after_owner_process_crash(self):
        libc = ctypes.CDLL(None, use_errno=True)
        previous = ctypes.c_int()
        self.assertEqual(0, libc.prctl(37, ctypes.byref(previous), 0, 0, 0))
        self.assertEqual(0, libc.prctl(36, 1, 0, 0, 0))
        parent = None
        descriptor = None
        child = None
        try:
            parent = await asyncio.create_subprocess_exec(
                sys.executable,
                "-B",
                str(ROOT / "tests/v1/pico_probe_crash.py"),
                str(self.service.root),
                WORKER,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            child = int(await asyncio.wait_for(parent.stdout.readline(), 5))
            descriptor = os.pidfd_open(child)
            lock_path = self.service.root / "supervisor.lock"
            original = lock_path.read_bytes()
            self.assertEqual({"nonce"}, set(json.loads(original)["process_identity"]))
            parent.kill()
            await asyncio.wait_for(parent.wait(), 5)
            with self.assertRaisesRegex(SimulationConflict, "Startup process group is unresolved"):
                await self.service.start()
            self.assertEqual(original, lock_path.read_bytes())
            self.assertTrue(Path(f"/proc/{child}").exists())
            self.assertIsNone(self.service.runtime_identity)
        finally:
            if parent is not None and parent.returncode is None:
                parent.kill()
                await parent.wait()
            if descriptor is not None:
                with suppress(ProcessLookupError):
                    signal.pidfd_send_signal(descriptor, signal.SIGKILL)
                os.close(descriptor)
                await asyncio.to_thread(os.waitpid, child, 0)
            # Fixture cleanup uses the actual captured child pidfd and reap proof.
            # Production deliberately cannot resolve a lost PID from a nonce alone.
            if self.service._lock is not None:
                self.service._probe_identity = None
                self.service._write_probe_identity()
                await self.service.close()
            self.assertEqual(0, libc.prctl(36, previous.value, 0, 0, 0))

    async def exercise_probe_metadata_write_crash(self, point, *, accepted):
        libc = ctypes.CDLL(None, use_errno=True)
        previous = ctypes.c_int()
        self.assertEqual(0, libc.prctl(37, ctypes.byref(previous), 0, 0, 0))
        self.assertEqual(0, libc.prctl(36, 1, 0, 0, 0))
        marker = self.root / "probe-write-crash.json"
        controller, identity, result = None, None, None
        try:
            controller = await asyncio.create_subprocess_exec(
                sys.executable,
                "-B",
                str(ROOT / "tests/v1/pico_probe_write_crash.py"),
                str(self.service.root),
                WORKER,
                str(marker),
                point,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(controller.communicate(), 15)
            self.assertEqual(73, controller.returncode, (stdout, stderr))
            crashed = json.loads(marker.read_bytes())
            identity = crashed["identity"]
            lock_path = self.service.root / "supervisor.lock"
            original = lock_path.read_bytes()
            self.assertFalse(crashed["append"])
            self.assertEqual(crashed["inode"], lock_path.stat().st_ino)
            before = resource_state(identity) if identity is not None else "not_spawned"
            if accepted:
                self.assertEqual("dead", before)
                await self.start_service()
                self.assertIsNotNone(self.service._database_lock)
                self.assertIsNotNone(self.service.runtime_identity)
                self.assertEqual(b"", lock_path.read_bytes())
            else:
                self.assertGreater(len(original), 0)
                if point == "receipt_partial":
                    with self.assertRaises(json.JSONDecodeError):
                        json.loads(original)
                elif identity is not None:
                    self.assertEqual(identity["nonce"], json.loads(original)["process_identity"]["nonce"])
                with self.assertRaisesRegex(SimulationConflict, "unresolved"):
                    await self.service.start()
                self.assertEqual(original, lock_path.read_bytes())
                self.assertIsNone(self.service._database_lock)
                self.assertIsNone(self.service.runtime_identity)
                self.assertIsNotNone(self.service._lock)
                if identity is not None:
                    self.assertEqual("alive", before)
                    self.assertEqual("alive", resource_state(identity))
                with self.assertRaisesRegex(SimulationConflict, "unresolved"):
                    await self.service.close()
                self.assertEqual(original, lock_path.read_bytes())
            self.assertEqual(crashed["inode"], os.fstat(self.service._lock.fileno()).st_ino)
            result = {
                "point": point,
                "controller_exit_code": controller.returncode,
                "ownership_bytes_after_crash": len(original),
                "inode": crashed["inode"],
                "append": crashed["append"],
                "old_group_before_replacement": before,
                "replacement_accepted": accepted,
                "old_group_after_replacement": resource_state(identity) if identity is not None else "not_spawned",
            }
            evidence = os.environ.get("PICO_PROBE_WRITE_EVIDENCE")
            if evidence:
                destination = Path(evidence)
                destination.mkdir(parents=True, exist_ok=True)
                (destination / f"{point}-ownership.bin").write_bytes(original)
                result["ownership_sha256"] = hashlib.sha256(original).hexdigest()
        finally:
            if controller is not None and controller.returncode is None:
                controller.kill()
                await controller.wait()
            if identity is not None and resource_state(identity) != "dead":
                descriptor = os.pidfd_open(identity["pid"])
                try:
                    self.assertEqual(identity, process_identity(identity["pid"], identity["nonce"]))
                    signal.pidfd_send_signal(descriptor, signal.SIGKILL)
                finally:
                    os.close(descriptor)
                await asyncio.to_thread(os.waitpid, identity["pid"], 0)
            if self.service._lock is not None:
                if identity is not None:
                    # Audit-only recovery uses the fixture's exact captured identity.
                    self.service._probe_identity = identity
                else:
                    # This fixture crashed before the first spawn; no child exists.
                    self.service._lock.seek(0)
                    self.service._lock.truncate(0)
                    self.service._lock.flush()
                    os.fsync(self.service._lock.fileno())
                    self.service._probe_identity = None
                await self.service.close()
            if result is not None:
                result["old_group_after_audit_cleanup"] = resource_state(identity) if identity else "not_spawned"
                if os.environ.get("PICO_PROBE_WRITE_EVIDENCE"):
                    (Path(os.environ["PICO_PROBE_WRITE_EVIDENCE"]) / f"{point}-result.json").write_text(
                        json.dumps(result, indent=2) + "\n"
                    )
            self.assertEqual(0, libc.prctl(36, previous.value, 0, 0, 0))

    async def test_probe_intent_crash_retains_nonempty_unknown_on_same_inode(self):
        await self.exercise_probe_metadata_write_crash("intent_truncate", accepted=False)

    async def test_probe_receipt_truncate_crash_retains_original_operation_on_same_inode(self):
        await self.exercise_probe_metadata_write_crash("receipt_truncate", accepted=False)

    async def test_probe_partial_receipt_write_crash_retains_unknown_fragment(self):
        await self.exercise_probe_metadata_write_crash("receipt_partial", accepted=False)

    async def test_probe_clear_before_truncate_crash_reconciles_proven_dead_group(self):
        await self.exercise_probe_metadata_write_crash("clear_before_truncate", accepted=True)

    async def test_probe_clear_after_truncate_crash_accepts_only_proven_dead_group(self):
        await self.exercise_probe_metadata_write_crash("clear_after_truncate", accepted=True)

    async def test_probe_clear_failure_keeps_lock_and_retries_original_identity(self):
        await self.start_service()
        async with self.exited_leader_group(marked=True, register=False) as (_, current, child):
            self.service._probe_identity = current.identity
            self.service._write_probe_identity()
            lock_path = self.service.root / "supervisor.lock"
            original, inode = lock_path.read_bytes(), lock_path.stat().st_ino

            class FailFirstClear:
                def __init__(self, stream):
                    self.stream, self.failed = stream, False

                def __getattr__(self, name):
                    return getattr(self.stream, name)

                def truncate(self, size=None):
                    if size == 0 and not self.failed:
                        self.failed = True
                        raise OSError("Fixture clear failure before truncate")
                    return self.stream.truncate(size)

            self.service._lock = FailFirstClear(self.service._lock)
            with self.assertRaisesRegex(OSError, "Fixture clear failure"):
                await self.service.close()
            self.assertIsNotNone(self.service._database_lock)
            self.assertIsNotNone(self.service.runtime_identity)
            self.assertEqual(inode, lock_path.stat().st_ino)
            self.assertEqual(original, lock_path.read_bytes())
            self.assertFalse(Path(f"/proc/{child}").exists())
            self.assertEqual("dead", resource_state(current.identity))
            await self.service.close()
            self.assertEqual(b"", lock_path.read_bytes())
            self.assertEqual(inode, lock_path.stat().st_ino)
            self.assertIsNone(self.service._database_lock)
            self.assertIsNone(self.service._lock)

    async def exercise_verifier_group_cancellation(self, *, marked):
        from unittest.mock import patch

        await self.start_service()
        row = await self.service.create(
            "app:tenant:operator",
            "verifier-group",
            self.run_id,
            self.request,
            self.members,
            self.read_member,
        )
        sid = row["session_id"]
        await self.wait_ready(sid)
        await self.service.stop(sid)
        original_recording = self.store.get(sid)["recording_artifact"]
        async with self.exited_leader_group(marked=marked, register=False, exit_leader=False) as (_, current, child):

            async def existing_group(*_args, **_kwargs):
                return current.process

            with (
                patch(
                    "description_pipeline.simulation.recording.secrets.token_hex",
                    return_value=current.identity["nonce"],
                ),
                patch("description_pipeline.simulation.recording.asyncio.create_subprocess_exec", existing_group),
            ):
                checking = asyncio.create_task(self.service.verify_recording(sid, self.members, self.read_member))
                async with asyncio.timeout(5):
                    while (
                        not (attempts := self.store.verification_attempts(sid))
                        or attempts[-1]["result"]["state"] != "running"
                    ):
                        if checking.done():
                            checking.result()
                        await asyncio.sleep(0.02)
                checking.cancel()
                with self.assertRaises(asyncio.CancelledError if marked else VerificationUnresolved):
                    await checking
            attempt = self.store.verification_attempts(sid)[-1]
            self.assertEqual(current.identity, attempt["result"]["process_identity"])
            self.assertEqual("cancelled" if marked else "unknown", attempt["result"]["state"])
            self.assertEqual(original_recording, self.store.get(sid)["recording_artifact"])
            partial = self.service.root / attempt["request"]["partial"]
            if marked:
                self.assertFalse(partial.exists())
                self.assertFalse(Path(f"/proc/{child}").exists())
                return
            original_source = (partial / "source/mjcf/robot.xml").read_bytes()
            self.assertTrue(Path(f"/proc/{child}").exists())
            with self.assertRaisesRegex(SimulationConflict, "capacity"):
                self.store.begin_verification(sid, "second-check", attempt["request"], self.service.owner)
            await self.service.close()
            await self.start_service()
            restarted = self.store.verification_attempts(sid)[-1]
            self.assertEqual(attempt["result"]["process_identity"], restarted["result"]["process_identity"])
            self.assertEqual("unknown", restarted["result"]["state"])
            self.assertEqual(original_source, (partial / "source/mjcf/robot.xml").read_bytes())
        verified = await self.service.verify_recording(sid, self.members, self.read_member)
        self.assertTrue(verified["recording_complete"])
        attempts = self.store.verification_attempts(sid)
        self.assertEqual(["failed", "completed"], [item["result"]["state"] for item in attempts])
        self.assertFalse(partial.exists())
        self.assertEqual(original_recording, self.store.get(sid)["recording_artifact"])

    async def test_owned_verifier_descendant_is_reaped_on_cancellation(self):
        await self.exercise_verifier_group_cancellation(marked=True)

    async def test_unknown_verifier_retains_pg_slot_partial_and_identity_across_restart(self):
        await self.exercise_verifier_group_cancellation(marked=False)

    async def wait_ready(self, sid):
        async with asyncio.timeout(20):
            while (row := await asyncio.to_thread(self.store.get, sid))["state"] == "starting":
                await asyncio.sleep(0.05)
        self.assertEqual("ready", row["state"], row)

    def assert_stopped(self, sid):
        row = self.store.get(sid)
        if row["state"] != "stopped":
            with (self.service.root / sid / "worker-stderr.log").open("rb") as stream:
                details = {"session": row, "worker_stderr": stream.read(8192).decode(errors="replace")}
            self.fail(json.dumps(details, sort_keys=True))
        return row

    async def test_cancelled_materialization_settles_admission_and_cleans_verification(self):
        from unittest.mock import patch

        await self.start_service()
        entered, release = threading.Event(), threading.Event()
        materializing = asyncio.Event()

        async def observe_materialization(*args, **kwargs):
            materializing.set()
            return await recording_module.prepare_source(*args, **kwargs)

        def slow_member(name, member):
            entered.set()
            if not release.wait(5):
                raise TimeoutError("Fixture reader was not released")
            return self.read_member(name, member)

        with patch("description_pipeline.simulation.service.prepare_source", observe_materialization):
            creating = asyncio.create_task(
                self.service.create(
                    "app:tenant:operator", "cancel-create", self.run_id, self.request, self.members, slow_member
                )
            )
            try:
                await asyncio.wait_for(materializing.wait(), 30)
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                creating.cancel()
                await asyncio.sleep(0)
                self.assertFalse(creating.done())
            finally:
                release.set()
            with self.assertRaises(asyncio.CancelledError):
                await creating
        with self.store.transaction() as cursor:
            cursor.execute("SELECT session_id FROM simulation_session WHERE request_key='cancel-create'")
            cancelled_id = cursor.fetchone()[0]
        self.assertEqual("failed", self.store.get(cancelled_id)["state"])
        self.assertFalse(self.service.running)
        row = await self.service.create(
            "app:tenant:operator", "cancel-verify", self.run_id, self.request, self.members, self.read_member
        )
        await self.wait_ready(row["session_id"])
        await self.service.stop(row["session_id"])
        entered.clear()
        release.clear()
        materializing.clear()
        with patch("description_pipeline.simulation.verification.prepare_source", observe_materialization):
            checking = asyncio.create_task(self.service.verify_recording(row["session_id"], self.members, slow_member))
            try:
                await asyncio.wait_for(materializing.wait(), 30)
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                checking.cancel()
                await asyncio.sleep(0)
                self.assertFalse(checking.done())
            finally:
                release.set()
            with self.assertRaises(asyncio.CancelledError):
                await checking
        self.assertFalse(list(self.service.root.glob("recording-verification-*")))
        self.assertFalse(self.service.verification_gate.locked())
        self.assertEqual("cancelled", self.store.verification_attempts(row["session_id"])[-1]["result"]["state"])
        slot = await asyncio.to_thread(self.store.acquire_verification_slot)
        self.assertIsNotNone(slot)
        slot.close()
        await self.service.close()
        self.assertIsNone(self.service._lock)
        self.assertIsNone(self.service._database_lock)

    async def test_cancelled_verifier_reaps_its_real_process(self):
        from unittest.mock import patch

        await self.start_service()
        row = await self.service.create(
            "app:tenant:operator",
            "verifier-cancel",
            self.run_id,
            self.request,
            self.members,
            self.read_member,
        )
        sid = row["session_id"]
        await self.wait_ready(sid)
        await self.service.stop(sid)
        actual_spawn = asyncio.create_subprocess_exec
        spawned = asyncio.Event()
        child = None

        async def fixture_spawn(*args, **options):
            nonlocal child
            child = await actual_spawn(sys.executable, "-I", "-c", "import time;time.sleep(30)", args[-1], **options)
            spawned.set()
            return child

        with patch("description_pipeline.simulation.recording.asyncio.create_subprocess_exec", fixture_spawn):
            task = asyncio.create_task(self.service.verify_recording(sid, self.members, self.read_member))
            await asyncio.wait_for(spawned.wait(), 2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertIsNotNone(child.returncode)
        with self.assertRaises(ProcessLookupError):
            os.kill(child.pid, 0)
        attempt = self.store.verification_attempts(sid)[-1]
        self.assertEqual("cancelled", attempt["result"]["state"])
        self.assertEqual("dead", resource_state(attempt["result"]["process_identity"]))
        self.assertFalse((self.service.root / attempt["request"]["partial"]).exists())

    async def test_close_during_verifier_spawn_waits_for_handle_and_settles_attempt(self):
        from unittest.mock import patch

        await self.start_service()
        row = await self.service.create(
            "app:tenant:operator",
            "close-verifier-spawn",
            self.run_id,
            self.request,
            self.members,
            self.read_member,
        )
        sid = row["session_id"]
        await self.wait_ready(sid)
        await self.service.stop(sid)
        entered, release = asyncio.Event(), asyncio.Event()
        actual_spawn = asyncio.create_subprocess_exec
        spawned = []

        async def delayed_spawn(*args, **options):
            process = await actual_spawn(
                sys.executable,
                "-I",
                "-c",
                "import time;time.sleep(60)",
                args[-1],
                **options,
            )
            spawned.append((process, process_identity(process.pid, args[-1])))
            entered.set()
            await release.wait()
            return process

        with patch("description_pipeline.simulation.recording.asyncio.create_subprocess_exec", delayed_spawn):
            checking = asyncio.create_task(self.service.verify_recording(sid, self.members, self.read_member))
            try:
                await asyncio.wait_for(entered.wait(), 5)
                closing = asyncio.create_task(self.service.close())
                await asyncio.sleep(0)
                self.assertFalse(closing.done())
                self.assertIsNotNone(self.service._database_lock)
                self.assertEqual("alive", resource_state(spawned[0][1]))
            finally:
                release.set()
            with self.assertRaises(asyncio.CancelledError):
                await checking
            await asyncio.wait_for(closing, 10)
        self.assertEqual("dead", resource_state(spawned[0][1]))
        self.assertEqual("cancelled", self.store.verification_attempts(sid)[-1]["result"]["state"])
        self.assertIsNone(self.service._database_lock)
        self.assertIsNone(self.service._lock)

    @unittest.skipUnless(LINK, "PICO_LINK_PYTHON with the explicit link closure is required")
    async def test_real_pico_link_preserves_transitions_and_rejects_gaps(self):
        result = await asyncio.to_thread(
            subprocess.run,
            [
                LINK,
                "-I",
                "-B",
                str(ROOT / "tests/v1/pico_link_probe.py"),
                str(Path(LINK).parent.parent / "libexec/pico_pipeline_link.py"),
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(0, result.returncode, result.stderr)

    async def test_native_lifecycle_identity_recording_and_idempotency(self):
        await self.start_service()
        self.request["runtime"] = self.service.runtime_identity
        row = await self.service.create(
            "app:tenant:operator", "one", self.run_id, self.request, self.members, self.read_member
        )
        sid = row["session_id"]
        await self.wait_ready(sid)
        again = await self.service.create(
            "app:tenant:operator", "one", self.run_id, self.request, self.members, self.read_member
        )
        self.assertEqual(sid, again["session_id"])
        with self.assertRaises(SimulationConflict):
            await self.service.create(
                "app:tenant:operator",
                "one",
                self.run_id,
                {**self.request, "record": False},
                self.members,
                self.read_member,
            )
        current = self.service.running[sid]
        self.assertFalse((current.root / "request.json").exists())
        self.assertEqual("3.13.0", current.model["engine"])
        self.assertEqual(9, len(current.model["joints"]))
        code = await self.service.pair(sid)
        await self.service.consume_pair(sid, code)
        with self.assertRaises(SimulationConflict):
            await self.service.consume_pair(sid, code)
        await self.service.send(sid, {"type": "estop"})
        await asyncio.sleep(0.1)
        self.assertTrue(current.state["report"]["estop_latched"])
        self.assertIsNotNone(current.state["report"]["head"])
        await self.service.stop(sid)
        self.assert_stopped(sid)
        self.assertIsNotNone(self.store.get(sid)["recording_artifact"])
        bound = await self.service.verify_recording(sid, self.members, self.read_member)
        self.assertEqual(self.store.get(sid)["request_digest"], bound["binding"]["request_digest"])
        self.assertTrue(bound["recording_complete"])
        self.assertTrue(bound["control_replay_verified"])
        self.assertTrue(bound["replay"]["source_root_overridden"])
        self.register_cli_source_fixture()
        cli_temporary = self.root / "cli-temporary"
        cli_temporary.mkdir()
        cli = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "description_pipeline.simulation.verify_recording",
            "--session",
            sid,
            "--config",
            self.cli_config(),
            env={**self.cli_environment(), "TMPDIR": str(cli_temporary)},
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(cli.communicate(), 30)
        self.assertEqual(0, cli.returncode, (stdout + stderr).decode())
        cli_result = json.loads(stdout)
        self.assertEqual(bound["binding"], cli_result["binding"])
        cli_attempt = self.store.verification_attempts(sid)[-1]
        self.assertEqual("completed", cli_attempt["result"]["state"])
        self.assertEqual(cli_result, cli_attempt["result"]["value"])
        self.assertEqual(cli_result["verifier_pid"], cli_attempt["result"]["process_identity"]["pid"])
        self.assertEqual("dead", resource_state(cli_attempt["result"]["process_identity"]))
        self.assertEqual([], list(cli_temporary.iterdir()))
        other = await self.service.create(
            "app:tenant:other",
            "other-recording",
            self.run_id,
            self.request,
            self.members,
            self.read_member,
        )
        await self.wait_ready(other["session_id"])
        await self.service.stop(other["session_id"])
        self.assert_stopped(other["session_id"])
        self.assertEqual(self.store.get(sid)["request_digest"], self.store.get(other["session_id"])["request_digest"])
        original_ref = self.store.get(sid)["recording_artifact"]
        with self.store.transaction() as cursor:
            cursor.execute(
                "UPDATE simulation_session SET recording_artifact=%s::jsonb WHERE session_id=%s",
                (json.dumps(self.store.get(other["session_id"])["recording_artifact"]), sid),
            )
        with self.assertRaisesRegex(ValueError, "registered session"):
            await self.service.verify_recording(sid, self.members, self.read_member)
        with self.store.transaction() as cursor:
            cursor.execute(
                "UPDATE simulation_session SET recording_artifact=%s::jsonb WHERE session_id=%s",
                (json.dumps(original_ref), sid),
            )
        self.assertGreater(bound["ticks"], 0)
        payload = prepare_bound(
            recording_binding(self.store.get(sid)),
            self.artifacts,
            self.service.root / sid / "source",
            commands=self.store.worker_commands(sid),
            execution=verification_execution(),
        )
        recording = self.root / "diagnostic-recording"
        shutil.copytree(payload["recording"], recording)
        payload["recording"] = str(recording)
        manifest = json.loads((recording / "native/session-manifest.json").read_bytes())
        path = recording / "native" / manifest["files"]["records"]["name"]
        rows = await self.native_rows(recording / "native", "records")
        self.assertTrue(rows)
        tampered = rows[0]
        tampered["extension"]["pico_session"]["state"]["report"]["arms"]["left"]["applied_pose"]["position_m"][0] += 0.1
        path.write_bytes(gzip.compress(b"".join(canonical(row) + b"\n" for row in rows), mtime=0))
        # This private diagnostic bypasses only the archive hash, to reach independent FK.
        # The real CAS artifact and registered session remain untouched.
        payload["inventory"][path.relative_to(recording).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
        identities = []

        async def register(identity):
            identities.append(identity)

        with self.assertRaisesRegex(ValueError, "TCP differs"):
            await run_verification(payload, Path(WORKER), register_process=register)
        self.assertEqual("dead", resource_state(identities[0]))
        saved = self.store.previous_profile("app:tenant:operator", self.request["source"]["subject_sha256"])
        self.assertEqual(self.request["profile"], saved["profile"])
        with self.store.transaction() as cursor:
            cursor.execute(
                "UPDATE simulation_session SET request=jsonb_set(request,'{record}','false') WHERE session_id=%s",
                (sid,),
            )
        with self.assertRaisesRegex(SimulationConflict, "digest"):
            self.store.previous_profile("app:tenant:operator", self.request["source"]["subject_sha256"])

    async def test_bad_source_and_maintenance_drain_release_capacity(self):
        await self.start_service()

        def changed_member(name, member):
            original = self.read_member(name, member)
            self.assertTrue(original)
            changed = bytes([original[0] ^ 1]) + original[1:]
            self.assertEqual(member["bytes"], len(changed))
            return changed

        with self.assertRaisesRegex(ValueError, "digest changed"):
            await self.service.create(
                "app:tenant:operator", "bad-source", self.run_id, self.request, self.members, changed_member
            )
        self.assertFalse(self.service.running)
        row = await self.service.create(
            "app:tenant:operator", "drain", self.run_id, self.request, self.members, self.read_member
        )
        sid = row["session_id"]
        await self.wait_ready(sid)
        with self.store.transaction() as cursor:
            cursor.execute("UPDATE admission SET state='closed'")
        async with asyncio.timeout(10):
            while sid in self.service.running:
                await asyncio.sleep(0.05)
        self.assert_stopped(sid)
        with self.assertRaisesRegex(SimulationConflict, "admission is closed"):
            await self.service.create(
                "app:tenant:operator", "closed", self.run_id, self.request, self.members, self.read_member
            )
        row = self.store.get(sid)
        self.assertEqual("stopped", row["state"], row)
        self.assertIsNotNone(row["recording_artifact"])
        self.assertNotIn(sid, self.service.running)
        await self.service.stop(sid)

    async def exercise_worker1_operator_receipt(self, *, cancel):
        from unittest.mock import patch

        await self.start_service()
        row = await self.service.create(
            "app:tenant:operator",
            "worker1-control",
            self.run_id,
            self.request,
            self.members,
            self.read_member,
        )
        sid = row["session_id"]
        await self.wait_ready(sid)
        body = {"type": "estop", "command_id": "operator-estop"}
        self.assertIsNone(self.store.authorize_command(sid, body["command_id"], "app:tenant:operator", body))
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.service._response

        async def pause_after_acceptance(session, current, message):
            await original(session, current, message)
            if message["type"] == "accepted" and message["kind"] == "estop":
                entered.set()
                await release.wait()

        with patch.object(self.service, "_response", pause_after_acceptance):
            sending = asyncio.create_task(self.service.send(sid, {"type": "estop"}, command_id=body["command_id"]))
            try:
                await asyncio.wait_for(entered.wait(), 5)
                accepted = self.store.worker_commands(sid)[0]["result"]
                self.assertEqual("accepted", accepted["state"])
                self.assertIs(accepted["response"]["applied"], False)
                self.assertFalse(sending.done())
                envelope = accepted["envelope"]
                if cancel:
                    sending.cancel()
                    await asyncio.sleep(0)
                    self.assertFalse(sending.done())
            finally:
                release.set()
            if cancel:
                with self.assertRaises(asyncio.CancelledError):
                    await sending
            else:
                receipt = await sending
                self.assertEqual("applied", receipt["type"])
                self.assertEqual("estop_latched", receipt["reason"])
        applied = self.store.worker_commands(sid)[0]["result"]
        self.assertEqual("applied", applied["state"])
        self.assertEqual(envelope, applied["envelope"])
        retried = await self.service.send(sid, {"type": "estop"}, command_id=body["command_id"])
        self.assertEqual(applied["response"], retried)
        self.assertEqual(applied, self.store.authorize_command(sid, body["command_id"], "app:tenant:operator", body))
        with self.assertRaisesRegex(SimulationConflict, "another request"):
            self.store.authorize_command(sid, body["command_id"], "app:tenant:operator", {**body, "type": "stop"})
        stop = {"type": "stop", "command_id": "operator-stop"}
        self.store.authorize_command(sid, stop["command_id"], "app:tenant:operator", stop)
        await self.service.stop(sid, command_id=stop["command_id"])
        self.assert_stopped(sid)
        recording = self.sealed_recording(sid)
        raw = [row["raw"] for row in await self.native_rows(recording / "native", "raw")]
        self.assertEqual(1, sum(item.get("id") == envelope["id"] for item in raw))
        self.assertTrue(json.loads((recording / "outcome.json").read_bytes())["complete"])
        self.assertEqual(["applied", "applied"], [item["result"]["state"] for item in self.store.worker_commands(sid)])

    async def test_worker1_acceptance_waits_for_applied_and_retry_preserves_original_envelope(self):
        await self.exercise_worker1_operator_receipt(cancel=False)

    async def test_worker1_cancelled_request_preserves_eventual_applied_evidence(self):
        await self.exercise_worker1_operator_receipt(cancel=True)

    async def test_native_verifier_rejects_changed_release_bytes_runtime_and_receipts(self):
        await self.start_service()
        row = await self.service.create(
            "app:tenant:operator", "verifier-gates", self.run_id, self.request, self.members, self.read_member
        )
        sid = row["session_id"]
        await self.wait_ready(sid)
        body = {"type": "estop", "command_id": "gate-estop"}
        self.store.authorize_command(sid, body["command_id"], "app:tenant:operator", body)
        await self.service.send(sid, {"type": "estop"}, command_id=body["command_id"])
        await self.service.stop(sid)
        payload = prepare_bound(
            recording_binding(self.store.get(sid)),
            self.artifacts,
            self.service.root / sid / "source",
            commands=self.store.worker_commands(sid),
            execution=verification_execution(),
        )
        original = deepcopy(payload["inventory"])
        cases = []
        value = deepcopy(payload)
        value["execution"]["helper_sha256"] = "0" * 64
        cases.append((value, "Recording helper bytes changed"))
        value = deepcopy(payload)
        value["execution"]["runtime_helper_sha256"] = "0" * 64
        cases.append((value, "Runtime helper differs"))
        value = deepcopy(payload)
        value["request"]["runtime"]["source_commit"] = "0" * 40
        cases.append((value, "original fixed runtime"))
        value = deepcopy(payload)
        value["commands"][0]["result"]["envelope"]["received_monotonic_ns"] += 1
        cases.append((value, "immutable registered envelope"))
        value = deepcopy(payload)
        value["commands"][0]["result"]["response"]["state_digest"] = "0" * 64
        cases.append((value, "state digest differs"))
        identities = []

        async def register(identity):
            identities.append(identity)

        for value, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError, message):
                    await run_verification(value, Path(WORKER), register_process=register)
                self.assertEqual("dead", resource_state(identities[-1]))
        verified = await self.service.verify_recording(sid, self.members, self.read_member)
        self.assertTrue(verified["control_replay_verified"])
        artifact = ArtifactRef.from_record(self.store.get(sid)["recording_artifact"])
        after = {item["path"]: item["sha256"] for item in self.artifacts.verify(artifact)["members"]}
        after["manifest.json"] = artifact.manifest_sha256
        self.assertEqual(original, after)

    async def native_verifier_fixture(self, key):
        await self.start_service()
        row = await self.service.create(
            "app:tenant:operator", key, self.run_id, self.request, self.members, self.read_member
        )
        sid = row["session_id"]
        await self.wait_ready(sid)
        await self.service.stop(sid)
        payload = prepare_bound(
            recording_binding(self.store.get(sid)),
            self.artifacts,
            self.service.root / sid / "source",
            commands=self.store.worker_commands(sid),
            execution=verification_execution(),
        )
        return sid, payload

    async def test_native_verification_policy_sets_actual_child_limits(self):
        import resource

        _sid, payload = await self.native_verifier_fixture("native-verifier-budget")
        self.assertEqual(
            {
                "revision": 1,
                "cpu_seconds": 900,
                "recording_wall_seconds": 1200,
                "address_space_bytes": 4 * 1024**3,
                "nofile": 128,
            },
            dict(recording_module.NATIVE_VERIFICATION_POLICY),
        )
        identities = []

        async def register(identity):
            identities.append(identity)
            self.assertEqual((900, 900), resource.prlimit(identity["pid"], resource.RLIMIT_CPU))
            self.assertEqual((4 * 1024**3, 4 * 1024**3), resource.prlimit(identity["pid"], resource.RLIMIT_AS))
            self.assertEqual((128, 128), resource.prlimit(identity["pid"], resource.RLIMIT_NOFILE))

        result = await run_verification(payload, Path(WORKER), register_process=register)
        self.assertTrue(result["control_replay_verified"])
        self.assertEqual(1, len(identities))
        self.assertEqual("dead", resource_state(identities[0]))

    async def test_native_verification_policy_parent_timeout_settles_managed_attempt(self):
        from types import MappingProxyType
        from unittest.mock import patch

        sid, _payload = await self.native_verifier_fixture("native-verifier-wall-budget")
        original_recording = self.store.get(sid)["recording_artifact"]
        policy = dict(recording_module.NATIVE_VERIFICATION_POLICY)
        policy["recording_wall_seconds"] = 0.001
        started = time.monotonic()
        with (
            patch.object(recording_module, "NATIVE_VERIFICATION_POLICY", MappingProxyType(policy)),
            self.assertRaises(TimeoutError),
        ):
            await self.service.verify_recording(sid, self.members, self.read_member)
        self.assertLess(time.monotonic() - started, 5)
        attempt = self.store.verification_attempts(sid)[-1]
        self.assertEqual("failed", attempt["result"]["state"])
        self.assertEqual("TimeoutError", attempt["result"]["error"])
        self.assertIsNotNone(attempt["result"]["process_identity"])
        self.assertEqual("dead", resource_state(attempt["result"]["process_identity"]))
        self.assertFalse((self.service.root / attempt["request"]["partial"]).exists())
        self.assertEqual(original_recording, self.store.get(sid)["recording_artifact"])

    async def test_native_verifier_cancellation_before_stdin_gate_reaps_actual_helper(self):
        _sid, payload = await self.native_verifier_fixture("native-verifier-cancel")
        registered = asyncio.Event()
        identities = []

        async def hold_registration(identity):
            identities.append(identity)
            registered.set()
            await asyncio.Future()

        checking = asyncio.create_task(run_verification(payload, Path(WORKER), register_process=hold_registration))
        await asyncio.wait_for(registered.wait(), 5)
        self.assertEqual("alive", resource_state(identities[0]))
        checking.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await checking
        self.assertEqual("dead", resource_state(identities[0]))

    async def test_native_verifier_spawn_cancellation_reaches_pending_registration(self):
        from unittest.mock import patch

        _sid, payload = await self.native_verifier_fixture("native-verifier-spawn-registration")
        actual_spawn = asyncio.create_subprocess_exec
        spawned, release = asyncio.Event(), asyncio.Event()
        identities = []

        async def delayed_spawn(*args, **options):
            process = await actual_spawn(*args, **options)
            spawned.set()
            await release.wait()
            return process

        async def hold_registration(identity):
            identities.append(identity)
            await asyncio.Future()

        with patch("description_pipeline.simulation.recording.asyncio.create_subprocess_exec", delayed_spawn):
            checking = asyncio.create_task(run_verification(payload, Path(WORKER), register_process=hold_registration))
            try:
                await asyncio.wait_for(spawned.wait(), 5)
                checking.cancel("cancel-before-spawn-return")
                await asyncio.sleep(0)
                self.assertFalse(checking.done())
            finally:
                release.set()
            with self.assertRaises(asyncio.CancelledError) as cancelled:
                await asyncio.wait_for(checking, 10)
        self.assertEqual(("cancel-before-spawn-return",), cancelled.exception.args)
        self.assertEqual(1, len(identities))
        self.assertEqual("dead", resource_state(identities[0]))

    async def test_native_verifier_cancel_waits_for_real_registration_transaction(self):
        from unittest.mock import patch

        sid, _payload = await self.native_verifier_fixture("native-verifier-durable-registration")
        original_recording = self.store.get(sid)["recording_artifact"]
        original_transition = self.store.transition_verification
        entered, release = threading.Event(), threading.Event()
        identities = []

        def blocked_transition(*args, **kwargs):
            if args[3] == "running":
                identities.append(kwargs["process"])
                entered.set()
                if not release.wait(5):
                    raise TimeoutError("Registration fixture was not released")
            return original_transition(*args, **kwargs)

        with patch.object(self.store, "transition_verification", blocked_transition):
            checking = asyncio.create_task(self.service.verify_recording(sid, self.members, self.read_member))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 5))
                checking.cancel("cancel-during-registration")
                await asyncio.sleep(0)
                checking.cancel("repeat-cancel-during-registration")
                await asyncio.sleep(0)
                self.assertFalse(checking.done())
                self.assertEqual("alive", resource_state(identities[0]))
            finally:
                release.set()
            with self.assertRaises(asyncio.CancelledError) as cancelled:
                await asyncio.wait_for(checking, 10)
        self.assertEqual(("cancel-during-registration",), cancelled.exception.args)
        attempt = self.store.verification_attempts(sid)[-1]
        self.assertEqual("cancelled", attempt["result"]["state"])
        self.assertEqual(identities[0], attempt["result"]["process_identity"])
        self.assertEqual("dead", resource_state(identities[0]))
        self.assertFalse((self.service.root / attempt["request"]["partial"]).exists())
        self.assertEqual(original_recording, self.store.get(sid)["recording_artifact"])
        verified = await self.service.verify_recording(sid, self.members, self.read_member)
        self.assertTrue(verified["control_replay_verified"])
        completed = self.store.verification_attempts(sid)[-1]
        self.assertEqual("completed", completed["result"]["state"])
        self.assertEqual("dead", resource_state(completed["result"]["process_identity"]))
        self.assertEqual(original_recording, self.store.get(sid)["recording_artifact"])

    async def test_native_verifier_registration_failure_keeps_error_and_reaps_helper(self):
        _sid, payload = await self.native_verifier_fixture("native-verifier-registration-error")
        failure = RuntimeError("Registration fixture failed")
        identities = []

        async def fail_registration(identity):
            identities.append(identity)
            raise failure

        with self.assertRaises(RuntimeError) as raised:
            await run_verification(payload, Path(WORKER), register_process=fail_registration)
        self.assertIs(failure, raised.exception)
        self.assertEqual(1, len(identities))
        self.assertEqual("dead", resource_state(identities[0]))

    async def test_recording_cli_uses_managed_partial_on_exec_and_native_runtime_failure(self):
        await self.start_service()
        row = await self.service.create(
            "app:tenant:operator", "cli-error-handoff", self.run_id, self.request, self.members, self.read_member
        )
        sid = row["session_id"]
        await self.wait_ready(sid)
        await self.service.stop(sid)
        self.register_cli_source_fixture()
        directory = self.root / "cli-errors"
        directory.mkdir()
        environment = {**self.cli_environment(), "TMPDIR": str(directory)}
        for python, message in ((self.root / "absent-python", "absolute fixed PICO"), (Path(sys.executable), "teleop")):
            with self.subTest(python=str(python)):
                child = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-m",
                    "description_pipeline.simulation.verify_recording",
                    "--session",
                    sid,
                    "--config",
                    self.cli_config(python),
                    env=environment,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await asyncio.wait_for(child.communicate(), 20)
                self.assertNotEqual(0, child.returncode)
                self.assertIn(message, (stdout + stderr).decode())
                self.assertEqual([], list(directory.iterdir()))

    async def test_unsaved_worker1_session_cleans_only_after_clean_stop(self):
        await self.start_service()
        row = await self.service.create(
            "app:tenant:operator",
            "unsaved-clean",
            self.run_id,
            {**self.request, "record": False},
            self.members,
            self.read_member,
        )
        sid = row["session_id"]
        await self.wait_ready(sid)
        self.assertTrue((self.service.root / sid / "recording/native/raw-input.jsonl.gz").is_file())
        await self.service.stop(sid)
        self.assertEqual("stopped", self.store.get(sid)["state"])
        self.assertIsNone(self.store.get(sid)["recording_artifact"])
        self.assertFalse((self.service.root / sid / "recording").exists())

    async def test_unsaved_worker1_parent_eof_keeps_failure_evidence(self):
        await self.start_service()
        row = await self.service.create(
            "app:tenant:operator",
            "unsaved-eof",
            self.run_id,
            {**self.request, "record": False},
            self.members,
            self.read_member,
        )
        sid = row["session_id"]
        await self.wait_ready(sid)
        current = self.service.running[sid]
        current.process.stdin.close()
        async with asyncio.timeout(10):
            while sid in self.service.running:
                await asyncio.sleep(0.05)
        self.assertEqual("failed", self.store.get(sid)["state"])
        self.assertIsNone(self.store.get(sid)["recording_artifact"])
        self.assertEqual("dead", resource_state(current.identity))
        self.assertTrue((current.root / "recording/native/raw-input.jsonl.gz").is_file())
        self.assertFalse((current.root / "recording/outcome.json").exists())
        self.assertFalse(json.loads((self.diagnostic_recording(sid) / "outcome.json").read_bytes())["complete"])

    async def test_unsaved_worker1_cleanup_failure_retains_native_evidence(self):
        await self.start_service()
        row = await self.service.create(
            "app:tenant:operator",
            "unsaved-cleanup-failure",
            self.run_id,
            {**self.request, "record": False},
            self.members,
            self.read_member,
        )
        sid = row["session_id"]
        await self.wait_ready(sid)
        native = self.service.root / sid / "recording/native"
        current = self.service.running[sid]
        seal = self.service._seal_recording

        async def prevent_cleanup(*args, **kwargs):
            self.assertEqual("dead", resource_state(current.identity))
            self.assertTrue((native / "session-manifest.json").is_file())
            native.chmod(0o500)
            return await seal(*args, **kwargs)

        self.service._seal_recording = prevent_cleanup
        try:
            await self.service.stop(sid)
            settled = self.store.get(sid)
            self.assertEqual("stopped", settled["state"])
            self.assertIn("cleanup failed", settled["error"])
            self.assertFalse(self.service.public(settled)["session_complete"])
            self.assertIsNone(settled["recording_artifact"])
            with self.assertRaisesRegex(SimulationConflict, "sealed recording"):
                recording_binding(settled)
            self.assertEqual("dead", resource_state(settled["process_identity"]))
            manifest = json.loads((native / "session-manifest.json").read_bytes())
            self.assertEqual("stopped", manifest["status"])
            self.assertEqual(0, manifest["exit_code"])
            self.assertEqual("normal_stop", manifest["reason"])
            self.assertTrue((native / "raw-input.jsonl.gz").is_file())
            self.assertFalse((native.parent / "outcome.json").exists())
            outcome = json.loads((self.diagnostic_recording(sid) / "outcome.json").read_bytes())
            self.assertFalse(outcome["complete"])
            self.assertFalse(outcome["session_complete"])
            self.assertIn("cleanup failed", outcome["error"])
        finally:
            native.chmod(0o700)

    async def test_unsaved_active_supervisor_crash_reconciles_original_native_evidence(self):
        libc = ctypes.CDLL(None, use_errno=True)
        previous = ctypes.c_int()
        self.assertEqual(0, libc.prctl(37, ctypes.byref(previous), 0, 0, 0))
        self.assertEqual(0, libc.prctl(36, 1, 0, 0, 0))
        parent, descriptor, identity, reaped = None, None, None, False
        try:
            parent = await asyncio.create_subprocess_exec(
                sys.executable,
                "-B",
                str(ROOT / "tests/v1/pico_session_crash.py"),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            config = {
                "dsn": psycopg2.extensions.make_dsn(self.pg.get_uri(), options=f"-csearch_path={self.schema}"),
                "artifacts": str(self.artifacts.root),
                "root": str(self.service.root),
                "python": WORKER,
                "request": self.request,
                "run_id": self.run_id,
                "members": self.members,
            }
            parent.stdin.write(canonical(config))
            await parent.stdin.drain()
            parent.stdin.close()
            raw = await asyncio.wait_for(parent.stdout.readline(), 20)
            if not raw:
                self.fail((await parent.stderr.read()).decode())
            started = json.loads(raw)
            sid, identity = started["session_id"], started["identity"]
            descriptor = os.pidfd_open(identity["pid"])
            self.assertEqual("alive", resource_state(identity))
            original = self.store.get(sid)
            applied = self.store.worker_commands(sid)[0]["result"]
            self.assertEqual("applied", applied["state"])
            for identifier in ("never-dispatched", "registered-pending"):
                body = {"type": "estop", "command_id": identifier}
                self.store.authorize_command(sid, identifier, "app:tenant:operator", body)
            pending_envelope = {
                "protocol": "teleop.upper-body.worker/1",
                "type": "estop",
                "id": "operator-" + hashlib.sha256(b"registered-pending").hexdigest(),
                "source_session_id": "supervisor-" + original["owner_nonce"],
                "sequence": 1000000,
                "received_monotonic_ns": time.monotonic_ns(),
            }
            self.store.bind_worker_command(sid, "registered-pending", original["owner_nonce"], pending_envelope)
            parent.kill()
            await asyncio.wait_for(parent.wait(), 5)
            async with asyncio.timeout(10):
                while True:
                    pid, _status = os.waitpid(identity["pid"], os.WNOHANG)
                    if pid:
                        reaped = True
                        break
                    await asyncio.sleep(0.02)
            self.assertEqual("dead", resource_state(identity))
            native = self.service.root / sid / "recording/native"
            before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in native.iterdir() if p.is_file()}
            self.assertIn("raw-input.jsonl.gz", before)
            await self.start_service()
            settled = self.store.get(sid)
            self.assertEqual("failed", settled["state"])
            self.assertIsNone(settled["recording_artifact"])
            self.assertEqual(identity, settled["process_identity"])
            self.assertEqual(original["request"], settled["request"])
            results = {item["command_id"]: item["result"] for item in self.store.worker_commands(sid)}
            self.assertEqual(applied, results["before-crash-estop"])
            self.assertEqual("not_sent", results["never-dispatched"]["state"])
            self.assertIsNone(results["never-dispatched"]["envelope"])
            self.assertEqual("unknown", results["registered-pending"]["state"])
            self.assertEqual(pending_envelope, results["registered-pending"]["envelope"])
            self.assertEqual(
                before, {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in native.iterdir() if p.is_file()}
            )
            self.assertFalse((native.parent / "outcome.json").exists())
            self.assertFalse(json.loads((self.diagnostic_recording(sid) / "outcome.json").read_bytes())["complete"])
        finally:
            if parent is not None and parent.returncode is None:
                parent.kill()
                await parent.wait()
            if descriptor is not None:
                try:
                    if not reaped:
                        with suppress(ProcessLookupError):
                            signal.pidfd_send_signal(descriptor, signal.SIGKILL)
                        await asyncio.to_thread(os.waitpid, identity["pid"], 0)
                finally:
                    os.close(descriptor)
            self.assertEqual(0, libc.prctl(36, previous.value, 0, 0, 0))

    async def test_http_websocket_authentication_and_exclusive_viewer(self):
        await self.start_service()

        class Identity:
            def profile(self, _token):
                return {
                    "principal": "app:tenant:operator",
                    "name": "Operator",
                    "role": "USER",
                    "app_id": "app",
                    "tenant_key": "tenant",
                    "open_id": "operator",
                }

        portal = PortalApp(PortalConfig(airflow=Identity(), artifact_store=self.artifacts))
        portal._authorize_run = lambda *_args: None  # Only the external run-authorization boundary is stubbed.
        portal._require_run = lambda *_args: {
            "triggering_user_name": build_actor_name("app:tenant:operator", "Operator")
        }
        portal._preview_payload = lambda *_args: {"mjcf": self.request["source"]}
        self.register_cli_source_fixture()
        portal._read_fact_projection = lambda *_args: self.canonical_projection()
        session = portal.sessions.create("Operator", "external-token", principal="app:tenant:operator")
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(create_app(portal, self.service), lifespan="off", log_level="error"))
        task = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            while not server.started:
                await asyncio.sleep(0.01)
            origin = f"http://127.0.0.1:{port}"
            cookie = f"{portal.config.session_cookie}={session.session_id}"
            async with httpx.AsyncClient(base_url=origin, headers={"Cookie": cookie}) as client:
                choices = await client.get("/api/runs/source/simulation-profile")
                self.assertEqual(200, choices.status_code, choices.text)
                descriptor = choices.json()
                self.assertIsNone(descriptor["saved"])
                self.assertEqual(self.request["source"]["subject_sha256"], descriptor["subject_sha256"])
                self.assertEqual(self.request["source"]["warning_inventory"], descriptor["warning_inventory"])
                self.assertEqual(self.request["source"]["verification_scope"], descriptor["verification_scope"])
                self.assertEqual("probe", descriptor["model"]["robot_name"])
                self.assertEqual(9, len(descriptor["model"]["joints"]))
                self.assertIn("head_link3", descriptor["model"]["links"])
                self.assertEqual(
                    {
                        "name": "left_0",
                        "type": "revolute",
                        "parent": "base",
                        "child": "left_link_0",
                        "lower": "-1.2",
                        "upper": "1.2",
                    },
                    descriptor["model"]["joints"][0],
                )
                member_reader = portal._registered_member_bytes
                portal._registered_member_bytes = lambda *_args: b"<robot name='wrong-revision'/>"
                try:
                    mismatch = await client.get("/api/runs/source/simulation-profile")
                    self.assertEqual(400, mismatch.status_code, mismatch.text)
                    self.assertIn("registered source URDF", mismatch.json()["error"])
                finally:
                    portal._registered_member_bytes = member_reader
                headers = {"X-CSRF-Token": session.csrf_token, "Idempotency-Key": "http-create"}
                response = await client.post(
                    "/api/runs/source/simulations",
                    headers=headers,
                    json={key: self.request[key] for key in ("profile", "settings", "record")},
                )
                self.assertEqual(202, response.status_code, response.text)
                sid = response.json()["session_id"]
                await self.wait_ready(sid)
                self.assertEqual(
                    403,
                    (
                        await client.post(
                            f"/api/simulations/{sid}/commands", json={"type": "stop", "command_id": "invalid-csrf"}
                        )
                    ).status_code,
                )
                response = await client.get(f"/api/simulations/{sid}")
                self.assertEqual(200, response.status_code, response.text)
                stream = f"ws://127.0.0.1:{port}/api/simulations/{sid}/stream"
                async with connect(
                    stream, origin=origin, additional_headers={"Cookie": cookie}, max_queue=512
                ) as viewer:
                    await viewer.send(json.dumps({"type": "authenticate", "csrf_token": session.csrf_token}))
                    model = json.loads(await asyncio.wait_for(viewer.recv(), 2))
                    self.assertEqual("model", model["type"])
                    self.assertEqual(9, len(model["joints"]))
                    async with connect(stream, origin=origin, additional_headers={"Cookie": cookie}) as second:
                        await second.send(json.dumps({"type": "authenticate", "csrf_token": session.csrf_token}))
                        from websockets.exceptions import ConnectionClosedError

                        with self.assertRaises(ConnectionClosedError):
                            await second.recv()
                    response = await client.post(
                        f"/api/simulations/{sid}/commands", headers=headers, json={"type": "pair", "command_id": "pair"}
                    )
                    self.assertEqual(200, response.status_code, response.text)
                    input_url = f"ws://127.0.0.1:{port}/api/simulations/{sid}/input"
                    async with connect(input_url) as source:
                        await source.send(json.dumps({"pairing_code": response.json()["pairing_code"]}))
                        paired = json.loads(await source.recv())
                        frame = {
                            "sequence": 0,
                            "device_timestamp_ns": 100,
                            "gateway_monotonic_ns": 100,
                            "left_controller_pose_xyzw": [0, 0, 0, 0, 0, 0, 1],
                            "right_controller_pose_xyzw": [0, 0, 0, 0, 0, 0, 1],
                            "headset_pose_xyzw": [0, 0, 0, 0, 0, 0, 1],
                            "schema_version": 2,
                            "source_session_id": "test-input",
                        }

                        anchor = paired["anchor"]
                        # Follow the real raw protocol through calibration, independent arm clutch and head IK.
                        for sequence in range(1, 81):
                            progress = max(0, sequence - 40) / 40
                            pose = [0.02 * progress, 0, 0, 0, 0, 0, 1]
                            current_frame = {
                                **frame,
                                "sequence": sequence,
                                "device_timestamp_ns": (sequence + 1) * 20_000_000,
                                "gateway_monotonic_ns": (sequence + 1) * 20_000_000,
                                "left_grip": 1.0 if sequence > 40 else 0.0,
                                "right_grip": 1.0 if sequence > 40 else 0.0,
                                "left_controller_pose_xyzw": pose,
                                "right_controller_pose_xyzw": [-pose[0], *pose[1:]],
                                "headset_pose_xyzw": [
                                    0,
                                    0,
                                    0,
                                    0,
                                    math.sin(0.05 * progress),
                                    0,
                                    math.cos(0.05 * progress),
                                ],
                            }
                            if sequence % 10 == 0:
                                await viewer.send(json.dumps({"type": "heartbeat"}))
                            await source.send(
                                json.dumps({"binding": paired["binding"], "anchor": anchor, "frame": current_frame})
                            )
                            reply = json.loads(await source.recv())
                            self.assertEqual("input_ack", reply["type"])
                            anchor = reply["anchor"]
                            await asyncio.sleep(0.02)
                        await asyncio.sleep(0.05)
                        current = self.service.running[sid].state
                        self.assertTrue(current["calibrated"], current["calibration_state"])
                        for prefix in ("left_", "right_", "head_"):
                            columns = [joint["q"] for joint in model["joints"] if joint["name"].startswith(prefix)]
                            self.assertGreater(
                                max(abs(current["qpos"][column]) for column in columns), 1e-5, current["report"]
                            )
                        released = {
                            **current_frame,
                            "sequence": 81,
                            "device_timestamp_ns": 1_640_000_000,
                            "gateway_monotonic_ns": 1_640_000_000,
                            "left_grip": 0.0,
                            "right_grip": 0.0,
                        }
                        await source.send(
                            json.dumps({"binding": paired["binding"], "anchor": anchor, "frame": released})
                        )
                        await source.recv()
                        await asyncio.sleep(0.05)
                        for arm in self.service.running[sid].state["report"]["arms"].values():
                            self.assertFalse(arm["clutched"])

                    self.assertIsNone(self.service.running[sid].source_session)
                headers = {"X-CSRF-Token": session.csrf_token}
                response = await client.post(
                    f"/api/simulations/{sid}/commands", headers=headers, json={"type": "stop", "command_id": "stop"}
                )
                self.assertEqual(200, response.status_code, response.text)
                stopped = response.json()
                self.assertTrue(stopped["stopped"])
                self.assertEqual("stopped", stopped["state"])
                self.assertIsNone(stopped["error"])
                self.assertEqual("applied", stopped["command_state"])
                self.assertTrue(stopped["response"]["applied"])
                self.assert_stopped(sid)
                response = await client.post(
                    f"/api/simulations/{sid}/recording/verify", headers=headers, json={"request_digest": "0" * 64}
                )
                self.assertEqual(400, response.status_code, response.text)
                response = await client.post(f"/api/simulations/{sid}/recording/verify", headers=headers)
                self.assertEqual(200, response.status_code, response.text)
                self.assertEqual(sid, response.json()["binding"]["session_id"])
                verified = response.json()
                self.assertTrue(verified["control_replay_verified"])
                inspected = await client.get(f"/api/simulations/{sid}")
                self.assertEqual("completed", inspected.json()["verification"]["state"])
                self.assertEqual(verified, inspected.json()["verification"]["value"])
                self.assertNotIn("owner_nonce", inspected.json()["verification"])
                response = await client.get(f"/api/simulations/{sid}/recording")
                self.assertEqual(200, response.status_code, response.text[:100] if response.status_code != 200 else "")
                self.assertTrue(response.content.startswith(b"PK"))
                response = await client.post(
                    "/api/runs/source/simulations",
                    headers={**headers, "Idempotency-Key": "http-stop-failure"},
                    json={key: self.request[key] for key in ("profile", "settings", "record")},
                )
                self.assertEqual(202, response.status_code, response.text)
                failed_sid = response.json()["session_id"]
                await self.wait_ready(failed_sid)
                native_send = self.service.send

                async def kill_during_stop(session_id, command, **kwargs):
                    if session_id == failed_sid and command["type"] == "stop":
                        process = self.service.running[session_id].process
                        process.kill()
                        await process.wait()
                        self.assertEqual(-9, process.returncode)
                    else:
                        return await native_send(session_id, command, **kwargs)

                self.service.send = kill_during_stop
                try:
                    response = await client.post(
                        f"/api/simulations/{failed_sid}/commands",
                        headers=headers,
                        json={"type": "stop", "command_id": "failed-stop"},
                    )
                finally:
                    self.service.send = native_send
                self.assertEqual(200, response.status_code, response.text)
                failure = response.json()
                self.assertFalse(failure["stopped"])
                self.assertEqual("failed", failure["state"])
                self.assertTrue(failure["error"])
                self.assertEqual(self.store.get(failed_sid)["error"], failure["error"])
                repeated = await client.post(
                    f"/api/simulations/{failed_sid}/commands",
                    headers=headers,
                    json={"type": "stop", "command_id": "failed-stop"},
                )
                self.assertEqual(failure, repeated.json())
                outcome = json.loads((self.sealed_recording(failed_sid) / "outcome.json").read_bytes())
                self.assertFalse(outcome["complete"])
        finally:
            server.should_exit = True
            await task
