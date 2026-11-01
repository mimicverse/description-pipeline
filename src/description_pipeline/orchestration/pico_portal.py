"""Same-origin ASGI transport over the existing portal and native sessions."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
import json
import hmac
import xml.etree.ElementTree as ET
import time
import math
import secrets
import logging

from a2wsgi import WSGIMiddleware
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse as StarletteJSONResponse, FileResponse
from starlette.background import BackgroundTask
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from ..orchestration.airflow_client import native_run_id
from ..simulation.service import SimulationService
from ..simulation.store import SimulationConflict
from .portal import PortalApp, PortalError

log = logging.getLogger(__name__)


class JSONResponse(StarletteJSONResponse):
    def __init__(self, content, status_code=200):
        super().__init__(content, status_code=status_code, headers={"Cache-Control": "no-store"})


class PicoPortal:
    """Share one authentication store; keep blocking portal I/O off the loop."""

    def __init__(self, portal: PortalApp, service: SimulationService):
        self.portal, self.service = portal, service

    async def actor(self, headers, *, csrf=False):
        environment = {"HTTP_COOKIE": headers.get("cookie", ""), "HTTP_X_CSRF_TOKEN": headers.get("x-csrf-token", "")}
        session = await asyncio.to_thread(self.portal._session, environment, csrf=csrf)
        principal, _ = await asyncio.to_thread(self.portal._viewer_identity, session)
        return session, principal

    async def authorized(self, session_id, headers, *, csrf=False):
        session, principal = await self.actor(headers, csrf=csrf)
        row = await asyncio.to_thread(self.service.store.get, session_id)
        if row["actor"] != principal:
            raise PermissionError("Simulation belongs to another operator")
        await asyncio.to_thread(self.portal._authorize_run, session, row["request"]["source"]["portal_run_id"])
        return session, principal, row

    async def create(self, request: Request):
        try:
            session, principal = await self.actor(request.headers, csrf=True)
            payload = await _json_request(request, 128 * 1024)
            if set(payload) - {"profile", "settings", "record"}:
                raise ValueError("Simulation request contains unknown fields")
            dag = request.path_params["run_id"]
            run = await asyncio.to_thread(self.portal._require_run, session, dag)
            if not await asyncio.to_thread(self.portal._can_manage, session, run):
                raise PermissionError("Only the source operator or an administrator may launch a simulation")
            preview = await asyncio.to_thread(self.portal._preview_payload, session, dag)
            descriptor = preview.get("mjcf")
            if descriptor is None:
                raise ValueError("This run has no completed, registered MJCF")
            settings = payload.get("settings")
            if not isinstance(settings, dict) or not {"position_scale", "joint_speed_radps"} <= settings.keys():
                raise ValueError("Explicit position and joint-rate budgets are required")
            if set(settings) - {"position_scale", "joint_speed_radps", "head_speed_radps", "controller_local_to_tcp"}:
                raise ValueError("Unknown Teleop setting")
            if not isinstance(payload.get("profile"), dict) or type(payload.get("record", False)) is not bool:
                raise ValueError("Simulation profile or recording flag is invalid")
            _finite_tree(payload)
            for key in ("position_scale", "joint_speed_radps", "head_speed_radps"):
                value = settings.get(key, 4 if key == "head_speed_radps" else None)
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value <= 20:
                    raise ValueError("Position and joint-rate budgets must be positive and at most 20")
            profile = payload["profile"]
            if not isinstance(profile.get("arms"), dict) or set(profile["arms"]) != {"left", "right"}:
                raise ValueError("The profile must explicitly bind left and right arms")
            for arm in profile["arms"].values():
                if not isinstance(arm, dict) or not isinstance(arm.get("joints"), list) or not arm["joints"]:
                    raise ValueError("Each arm requires an explicit joint list")
            if not isinstance(profile.get("head_joints"), list):
                raise ValueError("The profile requires an explicit head joint list")
            source = {
                key: descriptor[key]
                for key in ("files", "entry", "engine", "subject_sha256", "warnings", "verification_scope")
            }
            source["portal_run_id"] = dag
            body = {
                "source": source,
                "profile": payload["profile"],
                "settings": settings,
                "record": payload.get("record", False),
                "runtime": self.service.runtime_identity,
            }
            projection = await asyncio.to_thread(self.portal._read_fact_projection, native_run_id(dag))
            urdf = await asyncio.to_thread(
                self.portal._registered_member_bytes, "urdf/robot.urdf", projection["members"]["urdf/robot.urdf"]
            )
            joints = {item.get("name"): item.get("type") for item in ET.fromstring(urdf).findall("joint")}
            for arm in payload["profile"].get("arms", {}).values():
                if any(joints.get(name) != "revolute" for name in arm.get("joints", [])):
                    raise ValueError("PICO upper-body v1 requires bounded revolute arm joints")
            if any(joints.get(name) != "revolute" for name in profile["head_joints"]):
                raise ValueError("PICO upper-body v1 requires bounded revolute head joints")
            row = await self.service.create(
                principal,
                request.headers.get("idempotency-key", ""),
                native_run_id(dag),
                body,
                projection["members"],
                self.portal._registered_member_bytes,
            )
            return JSONResponse(row, status_code=202)
        except Exception as error:
            return self.error(error)

    async def recording(self, request: Request):
        try:
            _, _, row = await self.authorized(request.path_params["session_id"], request.headers)
            if row["recording_artifact"] is None:
                raise SimulationConflict("No sealed recording is available")
            from ..pipeline.contracts import ArtifactRef

            ref = ArtifactRef.from_record(row["recording_artifact"])
            path, temporary = await asyncio.to_thread(self.service.recording_archive, ref)
            return FileResponse(
                path,
                media_type="application/zip",
                filename="teleop-recording.zip",
                headers={"Cache-Control": "no-store"},
                background=BackgroundTask(temporary.cleanup),
            )
        except Exception as error:
            return self.error(error)

    async def verify_recording(self, request: Request):
        try:
            if await _json_request(request, 1024, allow_empty=True):
                raise ValueError("Recording verification identity comes from the registered session")
            session_id = request.path_params["session_id"]
            _, _, row = await self.authorized(session_id, request.headers, csrf=True)
            projection = await asyncio.to_thread(self.portal._read_fact_projection, row["source_run_id"])
            result = await self.service.verify_recording(
                session_id,
                projection["members"],
                self.portal._registered_member_bytes,
            )
            return JSONResponse(result)
        except Exception as error:
            return self.error(error)

    async def previous_profile(self, request: Request):
        try:
            session, principal = await self.actor(request.headers)
            preview = await asyncio.to_thread(self.portal._preview_payload, session, request.path_params["run_id"])
            source = preview.get("mjcf")
            if source is None:
                raise ValueError("This run has no completed MJCF")
            profile = await asyncio.to_thread(self.service.store.previous_profile, principal, source["subject_sha256"])
            return JSONResponse({"saved": profile, "subject_sha256": source["subject_sha256"]})
        except Exception as error:
            return self.error(error)

    async def get(self, request: Request):
        try:
            _, _, row = await self.authorized(request.path_params["session_id"], request.headers)
            result = self.service.public(row)
            result["source"] = row["request"]["source"]
            result["runtime"] = row["request"]["runtime"]
            result["recording_artifact"] = row["recording_artifact"]
            return JSONResponse(result)
        except Exception as error:
            return self.error(error)

    async def command(self, request: Request):
        try:
            session_id = request.path_params["session_id"]
            _, principal, _ = await self.authorized(session_id, request.headers, csrf=True)
            body = await _json_request(request, 1024)
            if set(body) != {"command_id", "type"} or body["type"] not in {"estop", "stop", "pair"}:
                raise ValueError("Simulation command is invalid")
            if not isinstance(body["command_id"], str) or not 1 <= len(body["command_id"]) <= 128:
                raise ValueError("Command identity is invalid")
            previous = await asyncio.to_thread(
                self.service.store.authorize_command, session_id, body["command_id"], principal, body
            )
            if previous is not None:
                return JSONResponse(previous)
            if body["type"] == "pair":
                result = {"pairing_code": await self.service.pair(session_id), "expires_in_s": 60}
            elif body["type"] == "estop":
                await self.service.send(session_id, {"type": "estop"})
                result = {"accepted": True}
            else:
                await self.service.stop(session_id)
                result = {"stopped": True}
            await asyncio.to_thread(self.service.store.complete_command, session_id, body["command_id"], result)
            return JSONResponse(result)
        except Exception as error:
            return self.error(error)

    async def viewer(self, websocket: WebSocket):
        session_id = websocket.path_params["session_id"]
        try:
            origin = websocket.headers.get("origin")
            expected = ("https" if websocket.url.scheme == "wss" else "http") + "://" + websocket.headers["host"]
            if origin != expected:
                raise PermissionError("WebSocket origin is not allowed")
            session, _, _ = await self.authorized(session_id, websocket.headers)
            current = self.service.running.get(session_id)
            if current is None:
                raise SimulationConflict("Native simulation worker is unavailable")
            await websocket.accept()
            first = await asyncio.wait_for(websocket.receive_json(), 5)
            if (
                set(first) != {"type", "csrf_token"}
                or first["type"] != "authenticate"
                or not isinstance(first["csrf_token"], str)
                or not hmac.compare_digest(first["csrf_token"], session.csrf_token)
            ):
                raise PermissionError("WebSocket request validation failed")
            binding = secrets.token_hex(24)
            if current.viewer_binding is not None:
                raise SimulationConflict("A controlling viewer is already connected")
            current.viewer_binding = binding
            current.viewer_until = time.monotonic() + 2

            async def receive():
                checked = time.monotonic()
                while True:
                    message = await asyncio.wait_for(websocket.receive_json(), 2)
                    if message != {"type": "heartbeat"}:
                        raise ValueError("Unknown viewer message")
                    if time.monotonic() - checked > 15:
                        await self.authorized(session_id, websocket.headers)
                        checked = time.monotonic()
                    current.viewer_until = time.monotonic() + 2

            async def publish():
                model_sent = False
                sequence = -1
                while current.process.returncode is None:
                    async with current.condition:
                        await current.condition.wait_for(
                            lambda seen=sequence: current.sequence != seen or current.process.returncode is not None
                        )
                        sequence = current.sequence
                    if not model_sent and current.model is not None:
                        await websocket.send_json(current.model)
                        model_sent = True
                    if current.state is not None:
                        await websocket.send_json({"type": "state", "state": current.state})
                    await asyncio.sleep(1 / 30)

            tasks = [asyncio.create_task(receive()), asyncio.create_task(publish())]
            try:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                if current.viewer_binding == binding:
                    current.viewer_binding = None
                    current.viewer_until = 0
                with suppress(RuntimeError):
                    await websocket.close(code=1000)
        except (TimeoutError, WebSocketDisconnect):
            pass
        except Exception:
            with suppress(RuntimeError):
                await websocket.close(code=1008)

    async def input(self, websocket: WebSocket):
        session_id = websocket.path_params["session_id"]
        binding = None
        current = None
        try:
            if websocket.url.scheme != "wss" and websocket.url.hostname not in {"localhost", "127.0.0.1", "::1"}:
                raise PermissionError("Remote PICO input requires WSS")
            await websocket.accept()
            hello = await asyncio.wait_for(websocket.receive_json(), 5)
            if set(hello) != {"pairing_code"}:
                raise ValueError("PICO pairing request is invalid")
            binding = await self.service.consume_pair(session_id, hello["pairing_code"])
            current = self.service.running[session_id]
            anchor = time.monotonic()
            await websocket.send_json({"type": "paired", "binding": binding, "anchor": anchor})
            while True:
                message = await asyncio.wait_for(websocket.receive_json(), 1)
                now = time.monotonic()
                if (
                    set(message) != {"binding", "anchor", "frame"}
                    or message["binding"] != binding
                    or current.source_session != binding
                    or message["anchor"] != anchor
                    or not 0 <= now - anchor <= 0.25
                ):
                    raise ValueError("PICO input is expired or belongs to another binding")
                current.input_until = now + 0.25
                await self.service.send(session_id, {"type": "frame", "frame": message["frame"]})
                anchor = time.monotonic()
                await websocket.send_json({"type": "input_ack", "anchor": anchor})
        except (TimeoutError, WebSocketDisconnect):
            pass
        except Exception:
            with suppress(RuntimeError):
                await websocket.close(code=1008)
        finally:
            if current is not None and current.source_session == binding:
                current.input_until = 0
                current.source_session = None
            with suppress(RuntimeError):
                await websocket.close(code=1000)

    @staticmethod
    def error(error):
        if isinstance(error, PortalError):
            return JSONResponse({"error": error.message}, status_code=int(error.status))
        if isinstance(error, PermissionError):
            return JSONResponse({"error": str(error)}, status_code=403)
        if isinstance(error, KeyError):
            return JSONResponse({"error": "Simulation does not exist"}, status_code=404)
        if isinstance(error, SimulationConflict):
            return JSONResponse({"error": str(error)}, status_code=409)
        if isinstance(error, (ValueError, TypeError)):
            return JSONResponse({"error": str(error)}, status_code=400)
        log.exception("Simulation request failed", exc_info=error)
        return JSONResponse({"error": "Simulation service is unavailable"}, status_code=503)


def create_app(portal: PortalApp, service: SimulationService):
    routes = PicoPortal(portal, service)

    @asynccontextmanager
    async def lifespan(_app):
        try:
            await service.start()
            yield
        finally:
            await service.close()

    return Starlette(
        lifespan=lifespan,
        routes=[
            Route("/api/runs/{run_id}/simulations", routes.create, methods=["POST"]),
            Route("/api/runs/{run_id}/simulation-profile", routes.previous_profile, methods=["GET"]),
            Route("/api/simulations/{session_id}", routes.get, methods=["GET"]),
            Route("/api/simulations/{session_id}/commands", routes.command, methods=["POST"]),
            Route("/api/simulations/{session_id}/recording", routes.recording, methods=["GET"]),
            Route("/api/simulations/{session_id}/recording/verify", routes.verify_recording, methods=["POST"]),
            WebSocketRoute("/api/simulations/{session_id}/stream", routes.viewer),
            WebSocketRoute("/api/simulations/{session_id}/input", routes.input),
            Mount("/", WSGIMiddleware(portal, workers=10)),
        ],
    )


async def _json_request(request: Request, limit: int, *, allow_empty=False):
    parts = []
    size = 0
    async for part in request.stream():
        size += len(part)
        if size > limit:
            raise ValueError("Request exceeds its byte budget")
        parts.append(part)
    value = json.loads(b"".join(parts)) if size or not allow_empty else {}
    if not isinstance(value, dict):
        raise ValueError("Request must be a JSON object")
    return value


def _finite_tree(value):
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("Simulation numbers must be finite")
    if isinstance(value, dict):
        for item in value.values():
            _finite_tree(item)
    elif isinstance(value, list):
        for item in value:
            _finite_tree(item)
