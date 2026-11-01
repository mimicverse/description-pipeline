"""Real database, native wheel and HTTP/WebSocket session integration."""

from __future__ import annotations

import asyncio
from datetime import datetime, UTC
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import unittest
import uuid
import xml.etree.ElementTree as ET

import httpx
import mujoco
import pgserver
import psycopg2
import uvicorn
from websockets.asyncio.client import connect

from description_pipeline.artifacts import ArtifactStore
from description_pipeline.io import digest
from description_pipeline.orchestration.pico_portal import create_app
from description_pipeline.orchestration.airflow_client import native_run_id
from description_pipeline.orchestration.feishu_oauth import build_actor_name
from description_pipeline.orchestration.portal import PortalApp, PortalConfig
from description_pipeline.pipeline.contracts import RunAdmission
from description_pipeline.simulation.service import SimulationService
from description_pipeline.simulation.store import SimulationConflict, SimulationStore
from description_pipeline.simulation.verify_recording import verify

ROOT = Path(__file__).resolve().parents[2]
WORKER = os.environ.get("TELEOP_WORKER_PYTHON")
LINK = os.environ.get("PICO_LINK_PYTHON", WORKER)


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


@unittest.skipUnless(WORKER, "Set TELEOP_WORKER_PYTHON to a noneditable fixed wheel environment")
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
        self.members = dict.fromkeys(files)
        self.read_member = lambda name, _ref: (source / name).read_bytes()

    async def asyncTearDown(self):
        await self.service.close()

    async def wait_ready(self, sid):
        async with asyncio.timeout(20):
            while (row := await asyncio.to_thread(self.store.get, sid))["state"] == "starting":
                await asyncio.sleep(0.05)
        self.assertEqual("ready", row["state"], row)

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
        await self.service.start()
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
        self.assertEqual("stopped", self.store.get(sid)["state"])
        self.assertIsNotNone(self.store.get(sid)["recording_artifact"])
        recording = self.service.root / sid / "recording"
        verified = await asyncio.to_thread(verify, recording, self.root / "model")
        self.assertGreater(verified["ticks"], 0)
        path = recording / "ticks.jsonl"
        lines = path.read_text().splitlines()
        tampered = json.loads(lines[0])
        tampered["result"]["report"]["arms"]["left"]["applied_pose"]["position_m"][0] += 0.1
        lines[0] = json.dumps(tampered)
        path.write_text("\n".join(lines) + "\n")
        with self.assertRaisesRegex(ValueError, "TCP differs"):
            await asyncio.to_thread(verify, recording, self.root / "model")

    async def test_bad_source_and_maintenance_drain_release_capacity(self):
        await self.service.start()
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
        self.assertEqual("stopped", self.store.get(sid)["state"])
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
        await self.service.start()

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
        portal._registered_member_bytes = self.read_member
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
                async with connect(stream, origin=origin, additional_headers={"Cookie": cookie}) as viewer:
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
                        await source.send(
                            json.dumps({"binding": paired["binding"], "anchor": paired["anchor"], "frame": frame})
                        )
                        self.assertEqual("input_ack", json.loads(await source.recv())["type"])
                    self.assertIsNone(self.service.running[sid].source_session)
                headers = {"X-CSRF-Token": session.csrf_token}
                response = await client.post(
                    f"/api/simulations/{sid}/commands", headers=headers, json={"type": "stop", "command_id": "stop"}
                )
                self.assertEqual(200, response.status_code, response.text)
                response = await client.get(f"/api/simulations/{sid}/recording")
                self.assertEqual(200, response.status_code, response.text[:100] if response.status_code != 200 else "")
                self.assertTrue(response.content.startswith(b"PK"))
        finally:
            server.should_exit = True
            await task
