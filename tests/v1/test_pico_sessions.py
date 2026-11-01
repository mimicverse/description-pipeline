"""Real database, native wheel and HTTP/WebSocket session integration."""

from __future__ import annotations

import asyncio
from datetime import datetime, UTC
import hashlib
import json
import math
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]
WORKER = os.environ.get("TELEOP_WORKER_PYTHON")
LINK = os.environ.get("PICO_LINK_PYTHON", WORKER)
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
    from description_pipeline.pipeline.contracts import RunAdmission, StageCommand, StageResult
    from description_pipeline.simulation.service import SimulationService
    from description_pipeline.simulation.store import SimulationConflict, SimulationStore
    from description_pipeline.simulation.verify_recording import verify
    from description_pipeline.simulation.recording import run_verification
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


@unittest.skipUnless(SESSION_TESTS, "Linux and TELEOP_WORKER_PYTHON with a fixed wheel are required")
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
                "warnings": ["Inspection fixture"],
                "verification_scope": "kinematic fixture",
                "portal_run_id": "source",
            },
            "profile": profile,
            "settings": {"position_scale": 0.1, "joint_speed_radps": 1},
            "record": True,
            "runtime": {},
        }
        source_ref = self.artifacts.seal_tree(
            source, kind="pico_test_source", producer_run_id=None, producer_stage=None, producer_tool_sha256="c" * 64
        )
        self.members = {
            item["path"]: {**item, "artifact": source_ref.to_record()}
            for item in self.artifacts.verify(source_ref)["members"]
        }
        self.read_member = lambda name, member: self.artifacts.read_metadata_member(
            ArtifactRef.from_record(member["artifact"]), name, max_bytes=64 * 1024 * 1024
        )[0]

    async def asyncTearDown(self):
        await self.service.close()

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

    async def start_service(self):
        await self.service.start()
        self.request["runtime"] = self.service.runtime_identity

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
        await self.start_service()
        entered, release = threading.Event(), threading.Event()

        def slow_member(name, member):
            entered.set()
            if not release.wait(5):
                raise TimeoutError("Fixture reader was not released")
            return self.read_member(name, member)

        creating = asyncio.create_task(
            self.service.create(
                "app:tenant:operator", "cancel-create", self.run_id, self.request, self.members, slow_member
            )
        )
        self.assertTrue(await asyncio.to_thread(entered.wait, 2))
        creating.cancel()
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
        checking = asyncio.create_task(self.service.verify_recording(row["session_id"], self.members, slow_member))
        self.assertTrue(await asyncio.to_thread(entered.wait, 2))
        checking.cancel()
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await checking
        self.assertFalse(list(self.service.root.glob("recording-verification-*")))
        self.assertFalse(self.service.verification_gate.locked())

    async def test_cancelled_verifier_reaps_its_real_process(self):
        from unittest.mock import patch

        actual_spawn = asyncio.create_subprocess_exec
        spawned = asyncio.Event()
        child = None

        async def fixture_spawn(*_args, **options):
            nonlocal child
            child = await actual_spawn(sys.executable, "-I", "-c", "import time;time.sleep(30)", **options)
            spawned.set()
            return child

        with patch("description_pipeline.simulation.recording.asyncio.create_subprocess_exec", fixture_spawn):
            task = asyncio.create_task(run_verification({}, self.artifacts.root, self.root / "model"))
            await asyncio.wait_for(spawned.wait(), 2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertIsNotNone(child.returncode)
        with self.assertRaises(ProcessLookupError):
            os.kill(child.pid, 0)

    async def test_real_pico_link_preserves_transitions_and_rejects_gaps(self):
        result = await asyncio.to_thread(
            subprocess.run,
            [LINK, "-I", "-B", str(ROOT / "tests/v1/pico_link_probe.py")],
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
        self.assertFalse(bound["control_replay_verified"])
        self.register_cli_source_fixture()
        cli = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "-m", "description_pipeline.simulation.verify_recording", "--session", sid],
            env={
                **os.environ,
                "PYTHONPATH": str(ROOT / "src"),
                "PLATFORM_ARTIFACT_ROOT": str(self.artifacts.root),
                "PLATFORM_OPERATOR_DSN": psycopg2.extensions.make_dsn(
                    self.pg.get_uri(), options=f"-csearch_path={self.schema}"
                ),
            },
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(0, cli.returncode, cli.stderr)
        self.assertEqual(bound["binding"], json.loads(cli.stdout)["binding"])
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
        recording = self.service.root / sid / "recording"
        verified = await asyncio.to_thread(
            verify, recording, self.root / "model", expected_request_digest=self.store.get(sid)["request_digest"]
        )
        self.assertGreater(verified["ticks"], 0)
        path = recording / "ticks.jsonl"
        lines = path.read_text().splitlines()
        tampered = json.loads(lines[0])
        tampered["result"]["report"]["arms"]["left"]["applied_pose"]["position_m"][0] += 0.1
        lines[0] = json.dumps(tampered)
        path.write_text("\n".join(lines) + "\n")
        with self.assertRaisesRegex(ValueError, "TCP differs"):
            await asyncio.to_thread(
                verify, recording, self.root / "model", expected_request_digest=self.store.get(sid)["request_digest"]
            )
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
        with self.assertRaisesRegex(ValueError, "digest changed"):
            await self.service.create(
                "app:tenant:operator", "bad-source", self.run_id, self.request, self.members, lambda *_args: b"changed"
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
        portal._read_fact_projection = lambda *_args: {"members": self.members}
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
                self.assertEqual(self.request["source"]["warnings"], descriptor["warnings"])
                self.assertEqual(self.request["source"]["verification_scope"], descriptor["verification_scope"])
                self.assertEqual("probe", descriptor["model"]["robot_name"])
                self.assertEqual(9, len(descriptor["model"]["joints"]))
                self.assertIn("head_link3", descriptor["model"]["links"])
                self.assertEqual(
                    {"name": "left_0", "type": "revolute", "parent": "base", "child": "left_link_0",
                     "lower": "-1.2", "upper": "1.2"},
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
                self.assertEqual({"stopped": True, "state": "stopped", "error": None}, response.json())
                self.assert_stopped(sid)
                await asyncio.to_thread(
                    verify,
                    self.service.root / sid / "recording",
                    self.root / "model",
                    expected_request_digest=self.store.get(sid)["request_digest"],
                )
                response = await client.post(
                    f"/api/simulations/{sid}/recording/verify", headers=headers, json={"request_digest": "0" * 64}
                )
                self.assertEqual(400, response.status_code, response.text)
                response = await client.post(f"/api/simulations/{sid}/recording/verify", headers=headers)
                self.assertEqual(200, response.status_code, response.text)
                self.assertEqual(sid, response.json()["binding"]["session_id"])
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

                async def kill_during_stop(session_id, command):
                    if session_id == failed_sid and command["type"] == "stop":
                        process = self.service.running[session_id].process
                        process.kill()
                        await process.wait()
                        self.assertEqual(-9, process.returncode)
                    else:
                        await native_send(session_id, command)

                self.service.send = kill_during_stop
                try:
                    response = await client.post(
                        f"/api/simulations/{failed_sid}/commands", headers=headers,
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
                    f"/api/simulations/{failed_sid}/commands", headers=headers,
                    json={"type": "stop", "command_id": "failed-stop"},
                )
                self.assertEqual(failure, repeated.json())
                outcome = json.loads((self.service.root / failed_sid / "recording/outcome.json").read_bytes())
                self.assertFalse(outcome["complete"])
        finally:
            server.should_exit = True
            await task
