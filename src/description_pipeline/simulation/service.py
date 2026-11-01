"""Supervise private native workers without occupying an Airflow task."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass, field
import hashlib
import fcntl
import json
import logging
import math
import os
from pathlib import Path
import secrets
import shutil
import time
import tempfile
import zipfile

from ..io import digest, canonical
from ..owned_process import module_command, process_identity, resource_state, terminate_owned
from .store import SimulationConflict, SimulationStore
from .recording import VerificationUnresolved, prepare_source, recording_binding, run_verification, session_identity, verification_execution
from .runtime import load_runtime_contract
from .runtime import validate_runtime_identity
from .warnings import warning_inventory

log = logging.getLogger(__name__)
WORKER_PROTOCOL = "teleop.upper-body.worker/1"


@dataclass
class RunningSession:
    process: asyncio.subprocess.Process
    root: Path
    identity: dict | None = None
    model: dict | None = None
    state: dict | None = None
    condition: asyncio.Condition = field(default_factory=asyncio.Condition)
    sequence: int = 0
    viewer_until: float = 0
    viewer_binding: str | None = None
    input_until: float = 0
    pair_digest: str | None = None
    pair_until: float = 0
    input_connected: bool | None = None
    source_session: str | None = None
    stdin_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    stop_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    termination_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    settlement_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    tasks: list = field(default_factory=list)
    commands: dict = field(default_factory=dict)
    completed: OrderedDict = field(default_factory=OrderedDict)
    deliveries: set = field(default_factory=set)
    command_sequence: int = 0
    stopped: bool = False
    stop_applied: bool = False
    ending: bool = False
    reader_eof: bool = False
    failure: str | None = None


class SimulationService:
    def __init__(self, store: SimulationStore, artifacts, root: Path, python: Path, *, capacity: int = 2):
        self.store, self.artifacts = store, artifacts
        self.root, self.python = Path(root), Path(python)
        self.capacity = capacity
        self.owner = secrets.token_hex(24)
        self.runtime_identity = None
        self.running: dict[str, RunningSession] = {}
        self.gate = asyncio.Lock()
        self.verification_gate = asyncio.Semaphore(1)
        self._lock = None
        self._database_lock = None
        self._probe = None
        self._probe_identity = None
        self._verification_task = None
        self._lifecycle_lock = asyncio.Lock()
        self._lifecycle_state = "closed"

    async def start(self) -> None:
        async with self._lifecycle_lock:
            if self._lock is not None or self._database_lock is not None:
                raise SimulationConflict("Simulation supervisor is already started")
            self._lifecycle_state = "starting"
            try:
                await self._start()
            except BaseException:
                self._lifecycle_state = "closing"
                async with self.gate:
                    await self._close_owned()
                raise
            if self._lifecycle_state == "starting":
                self._lifecycle_state = "open"

    async def _start(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.root.is_symlink() or not self.python.is_absolute() or not self.python.is_file():
            raise ValueError("Simulation root or worker interpreter is invalid")
        if shutil.which("prlimit") is None:
            raise ValueError("Native worker resource control requires prlimit")
        descriptor = os.open(self.root / "supervisor.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        self._lock = os.fdopen(descriptor, "r+")
        try:
            fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._lock.close()
            self._lock = None
            raise ValueError("Another simulation supervisor owns this root") from None
        self._probe_identity = self._read_probe_identity()
        if self._probe_identity is not None and not await self._close_probe():
            raise SimulationConflict("Previous startup process group is unresolved")
        contract = load_runtime_contract(Path(__file__).with_name("teleop-runtime.json"), consumer="pico")
        nonce = secrets.token_hex(24)
        self._probe_identity = {"nonce": nonce}
        self._write_probe_identity()
        spawning = asyncio.create_task(
            asyncio.create_subprocess_exec(
                str(self.python),
                "-I",
                "-B",
                str(Path(__file__).with_name("runtime.py")),
                nonce,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
                start_new_session=True,
            )
        )
        interrupted = False
        try:
            try:
                probe = await asyncio.shield(spawning)
            except asyncio.CancelledError:
                probe = await spawning
                interrupted = True
        except Exception:
            self._probe_identity = None
            self._write_probe_identity()
            raise
        self._probe = probe
        self._probe_identity = {"pid": probe.pid, "nonce": nonce}
        self._write_probe_identity()
        self._probe_identity = process_identity(probe.pid, nonce)
        self._write_probe_identity()
        if interrupted:
            raise asyncio.CancelledError
        if self._lifecycle_state != "starting":
            raise SimulationConflict("Simulation supervisor closed during startup")
        stdout, stderr = await asyncio.wait_for(
            probe.communicate(canonical(list(contract["dependency_versions"])) + b"\n"),
            30,
        )
        clean = await asyncio.to_thread(resource_state, self._probe_identity) == "dead"
        if not await self._close_probe():
            raise SimulationConflict("Startup process group is unresolved")
        if not clean:
            raise SimulationConflict("Startup process group did not exit cleanly")
        if probe.returncode != 0:
            raise ValueError(f"Teleop runtime identity failed: {stderr.decode(errors='replace')[-1000:]}")
        if self._lifecycle_state != "starting":
            raise SimulationConflict("Simulation supervisor closed during startup")
        self.runtime_identity = validate_runtime_identity(json.loads(stdout), contract)
        acquiring = asyncio.create_task(asyncio.to_thread(self.store.acquire_supervisor))
        try:
            self._database_lock = await asyncio.shield(acquiring)
        except asyncio.CancelledError:
            # The database call continues in its thread; cleanup must own its returned lease.
            (result,) = await asyncio.gather(acquiring, return_exceptions=True)
            if not isinstance(result, BaseException):
                self._database_lock = result
            raise
        if self._lifecycle_state != "starting":
            raise SimulationConflict("Simulation supervisor closed during startup")
        await self._reconcile_verifications()
        for row in await asyncio.to_thread(self.store.orphaned, self.owner):
            identity = row["process_identity"]
            quiescent = identity is not None and await self._terminate_identity(identity)
            if not quiescent:
                await asyncio.to_thread(self.store.reconcile_orphan, row["session_id"], row["owner_nonce"])
            else:
                recording = await self._seal_recording(
                    self.root / row["session_id"],
                    False,
                    "Supervisor interrupted",
                    runtime=row["request"]["runtime"],
                    save=row["request"]["record"],
                )
                await asyncio.to_thread(
                    self.store.transition,
                    row["session_id"],
                    row["owner_nonce"],
                    "failed",
                    error="Supervisor interrupted; previous process group is absent",
                    recording=recording,
                )

    def _read_probe_identity(self):
        self._lock.seek(0)
        previous = self._lock.read(32769)
        if not previous:
            return None
        try:
            record = json.loads(previous)
        except ValueError:
            raise SimulationConflict("Startup ownership metadata is unresolved") from None
        if (
            len(previous) > 32768
            or not isinstance(record, dict)
            or set(record) != {"schema_version", "process_identity"}
            or record["schema_version"] != "solidworks-to-urdf.simulation-probe/v1"
            or not isinstance(record["process_identity"], dict)
        ):
            raise SimulationConflict("Startup ownership metadata is unresolved")
        return record["process_identity"]

    def _write_probe_identity(self):
        payload = ""
        if self._probe_identity is not None:
            previous = self._read_probe_identity()
            if previous is not None and any(self._probe_identity.get(key) != value for key, value in previous.items()):
                raise SimulationConflict("Startup operation identity cannot change during receipt update")
            payload = json.dumps(
                {
                    "schema_version": "solidworks-to-urdf.simulation-probe/v1",
                    "process_identity": self._probe_identity,
                }
            )
        # Keep the same locked inode and a nonempty intent until its replacement
        # is durable. A crash may leave an unresolved fragment, never an empty gap.
        self._lock.seek(0)
        if self._lock.write(payload) != len(payload):
            raise OSError("Startup ownership metadata write is incomplete")
        self._lock.flush()
        os.fsync(self._lock.fileno())
        self._lock.truncate(len(payload))
        self._lock.flush()
        os.fsync(self._lock.fileno())

    async def _close_probe(self):
        if self._lock is None:
            return True
        if self._probe_identity is None:
            self._probe_identity = self._read_probe_identity()
        if self._probe_identity is not None:
            if not await self._terminate_identity(self._probe_identity):
                return False
            self._probe_identity = None
            self._probe = None
            self._write_probe_identity()
        self._lock.seek(0)
        return not self._lock.read(1)

    async def create(self, actor: str, key: str, run_id: str, request: dict, members: dict, read_member) -> dict:
        if self._lifecycle_state != "open":
            raise SimulationConflict("Simulation supervisor is not accepting sessions")
        if not key or len(key) > 128:
            raise ValueError("A bounded idempotency key is required")
        if self.runtime_identity is None or request.get("runtime") != self.runtime_identity:
            raise ValueError("Simulation request runtime differs from the active fixed worker")
        warning_inventory(request["source"])
        async with self.gate:
            if self._lifecycle_state != "open":
                raise SimulationConflict("Simulation supervisor is not accepting sessions")
            admitting = asyncio.create_task(
                asyncio.to_thread(
                    self.store.create,
                    actor,
                    key,
                    run_id,
                    request,
                    self.owner,
                    capacity=self.capacity,
                )
            )
            interrupted = False
            try:
                row = await asyncio.shield(admitting)
            except asyncio.CancelledError:
                row = await admitting
                interrupted = True
            session_id = row["session_id"]
            if interrupted:
                if (
                    row["owner_nonce"] == self.owner
                    and row["state"] == "starting"
                    and row["process_identity"] is None
                    and session_id not in self.running
                ):
                    await asyncio.to_thread(
                        self.store.transition,
                        session_id,
                        self.owner,
                        "failed",
                        error="Session admission was cancelled",
                    )
                raise asyncio.CancelledError
            if session_id in self.running or row["state"] != "starting" or row["owner_nonce"] != self.owner:
                return self.public(row)
            session_root = self.root / session_id
            source = session_root / "source"
            try:
                await prepare_source(source, request["source"], members, read_member)
                if self._lifecycle_state != "open":
                    raise SimulationConflict("Simulation supervisor is closing")
                config = {
                    "protocol": WORKER_PROTOCOL,
                    "session_id": session_id,
                    "mode": "kinematic_ik",
                    "root": str(source),
                    "inventory": request["source"]["files"],
                    "entry": request["source"]["entry"],
                    "profile": request["profile"],
                    **request["settings"],
                }
                output = session_root / "recording"
                output.mkdir(mode=0o700)
                (output / "request.json").write_bytes(canonical(request) + b"\n")
                (output / "session.json").write_bytes(canonical(session_identity(row)) + b"\n")
                config["record_dir"] = str(output / "native")
                environment = {key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL") if key in os.environ}
                environment.update({"PYTHONUNBUFFERED": "1", "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1"})
                error_log = (session_root / "worker-stderr.log").open("xb")
                nonce = secrets.token_hex(24)
                interrupted = False
                try:
                    spawning = asyncio.create_task(
                        asyncio.create_subprocess_exec(
                            shutil.which("prlimit"),
                            "--as=4294967296",
                            "--nofile=256",
                            "--fsize=536870912",
                            "--",
                            *module_command(self.python, "teleop.backends.mujoco.upper_body_worker", nonce),
                            stdin=asyncio.subprocess.PIPE,
                            stdout=asyncio.subprocess.PIPE,
                            stderr=error_log,
                            cwd=session_root,
                            env=environment,
                            start_new_session=True,
                            limit=64 * 1024 * 1024,
                        )
                    )
                    try:
                        process = await asyncio.shield(spawning)
                    except asyncio.CancelledError:
                        # The kernel spawn continues: obtain its handle before cleanup.
                        process = await spawning
                        interrupted = True
                finally:
                    error_log.close()
                current = RunningSession(process, session_root, identity={"pid": process.pid, "nonce": nonce})
                self.running[session_id] = current
                current.identity = process_identity(process.pid, nonce)
                if interrupted:
                    raise asyncio.CancelledError
                if self._lifecycle_state != "open":
                    raise SimulationConflict("Simulation supervisor is closing")
                await asyncio.to_thread(
                    self.store.transition, session_id, self.owner, "starting", process=current.identity
                )
                await self._write(current, config)
                current.tasks = [
                    asyncio.create_task(self._read(session_id)),
                    asyncio.create_task(self._watch(session_id)),
                ]
                return self.public(row)
            except BaseException as error:
                current = self.running.get(session_id)
                if current is not None:
                    current.failure = str(error) or type(error).__name__
                    await self._terminate(current)
                    if current.tasks:
                        if not current.tasks[0].done():
                            current.tasks[0].cancel()
                        await asyncio.gather(current.tasks[0], return_exceptions=True)
                    await self._settle(session_id, current)
                else:
                    latest = await asyncio.to_thread(self.store.get, session_id)
                    if latest["state"] not in {"stopped", "failed"}:
                        await asyncio.to_thread(
                            self.store.transition,
                            session_id,
                            self.owner,
                            "failed",
                            error=str(error) or type(error).__name__,
                        )
                raise

    @staticmethod
    def public(row: dict) -> dict:
        return {
            key: row[key] for key in ("session_id", "source_run_id", "request_digest", "state", "revision", "error")
        }

    def recording_archive(self, ref):
        temporary = tempfile.TemporaryDirectory(prefix="recording-export-", dir=self.root)
        try:
            tree = self.artifacts.materialize(ref, Path(temporary.name) / "source")
            target = Path(temporary.name) / "recording.zip"
            with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for path in sorted(tree.rglob("*")):
                    if path.is_file():
                        archive.write(path, path.relative_to(tree).as_posix())
            return target, temporary
        except BaseException:
            temporary.cleanup()
            raise

    async def verify_recording(self, session_id, members, read_member):
        if self._lifecycle_state != "open":
            raise SimulationConflict("Simulation supervisor is not accepting verification")
        if self.verification_gate.locked():
            raise SimulationConflict("A recording verification is already running; retry after it finishes")
        async with self.verification_gate:
            self._verification_task = asyncio.current_task()
            try:
                return await self._verify_recording(session_id, members, read_member)
            finally:
                self._verification_task = None

    async def _verify_recording(self, session_id, members, read_member):
        row = await asyncio.to_thread(self.store.get, session_id)
        binding = recording_binding(row)
        command_id = "verify-" + secrets.token_hex(16)
        request = {
            "type": "verify_recording",
            "binding": binding,
            "partial": "recording-verification-" + command_id,
            "execution": verification_execution(),
        }
        temporary = self._verification_partial(command_id, request)
        admitted = False
        retained = False
        try:
            await self._reconcile_verifications()
            admitting = asyncio.create_task(
                asyncio.to_thread(
                    self.store.begin_verification,
                    session_id,
                    command_id,
                    request,
                    self.owner,
                )
            )
            try:
                await asyncio.shield(admitting)
            except asyncio.CancelledError:
                await admitting
                admitted = True
                raise
            admitted = True
            temporary.mkdir(mode=0o700)
            source = temporary / "source"
            await prepare_source(source, row["request"]["source"], members, read_member)

            async def register_process(identity):
                await self._verification_transition(session_id, command_id, self.owner, "running", process=identity)

            from .verify_recording import prepare_bound

            preparing = asyncio.create_task(asyncio.to_thread(
                prepare_bound, binding, self.artifacts, source,
                commands=await asyncio.to_thread(self.store.worker_commands, session_id),
                execution=request["execution"],
            ))
            try:
                payload = await asyncio.shield(preparing)
            except asyncio.CancelledError:
                await preparing
                raise
            result = await run_verification(payload, self.python, register_process=register_process)
            await self._verification_transition(session_id, command_id, self.owner, "completed", value=result)
            return result
        except BaseException as error:
            if admitted:
                retained = isinstance(error, VerificationUnresolved)
                state = (
                    "unknown" if retained else "cancelled" if isinstance(error, asyncio.CancelledError) else "failed"
                )
                latest = next(
                    item
                    for item in await asyncio.to_thread(self.store.verification_attempts, session_id)
                    if item["command_id"] == command_id
                )
                if latest["result"]["state"] not in {"completed", "failed", "cancelled"}:
                    await self._verification_transition(
                        session_id,
                        command_id,
                        self.owner,
                        state,
                        error=str(error) or type(error).__name__,
                    )
            raise
        finally:
            if admitted and not retained:
                await asyncio.to_thread(shutil.rmtree, temporary, True)

    def _verification_partial(self, command_id, request):
        name = "recording-verification-" + command_id
        if request.get("partial") != name or Path(name).name != name:
            raise SimulationConflict("Recording verification partial identity is invalid")
        path = self.root / name
        if path.is_symlink():
            raise SimulationConflict("Recording verification partial must not be a symlink")
        return path

    async def _verification_transition(self, *args, **kwargs):
        changing = asyncio.create_task(asyncio.to_thread(self.store.transition_verification, *args, **kwargs))
        try:
            return await asyncio.shield(changing)
        except asyncio.CancelledError:
            await changing
            raise

    async def _reconcile_verifications(self):
        for attempt in await asyncio.to_thread(self.store.verification_attempts):
            result = attempt["result"]
            partial = self._verification_partial(attempt["command_id"], attempt["request"])
            if result["state"] in {"completed", "failed", "cancelled"}:
                await asyncio.to_thread(shutil.rmtree, partial, True)
                continue
            identity = result["process_identity"]
            quiescent = identity is not None and await self._terminate_identity(identity)
            await self._verification_transition(
                attempt["session_id"],
                attempt["command_id"],
                result["owner_nonce"],
                "failed" if quiescent else "unknown",
                error="Verification interrupted; process group is absent"
                if quiescent
                else "Verification process group ownership is unresolved",
            )
            if quiescent:
                await asyncio.to_thread(shutil.rmtree, partial, True)

    @staticmethod
    async def _write(current, message):
        async with current.stdin_lock:
            if current.process.returncode is not None:
                raise SimulationConflict("Native simulation worker is unavailable")
            current.process.stdin.write(canonical(message) + b"\n")
            await asyncio.wait_for(current.process.stdin.drain(), timeout=2)

    async def send(self, session_id: str, command: dict, *, command_id=None) -> dict:
        current = self.running.get(session_id)
        if current is None or current.process.returncode is not None:
            raise SimulationConflict("Native simulation worker is unavailable")
        if self._lifecycle_state != "open" and command.get("type") != "stop":
            raise SimulationConflict("Simulation supervisor is not accepting input")
        if current.ending and command.get("type") != "stop":
            raise SimulationConflict("Session is stopping")
        delivering = asyncio.create_task(self._send(session_id, current, command, command_id))
        current.deliveries.add(delivering)
        try:
            return await asyncio.shield(delivering)
        except asyncio.CancelledError:
            # A cancelled HTTP/WS request must still settle its actual dispatch.
            await asyncio.gather(delivering, return_exceptions=True)
            raise
        finally:
            current.deliveries.discard(delivering)

    async def _send(self, session_id, current, command, command_id):
        if len(current.commands) >= 256:
            raise SimulationConflict("Native command window is full")
        kind = command.get("type")
        if kind not in {"frame", "connection", "estop", "heartbeat", "stop"}:
            raise ValueError("Unsupported native command")
        frame = command.get("frame")
        if kind == "frame" and not isinstance(frame, dict):
            raise ValueError("A frame command requires the original frame object")
        source = frame["source_session_id"] if kind == "frame" else "supervisor-" + self.owner
        sequence = frame["sequence"] if kind == "frame" else current.command_sequence
        current.command_sequence += 1
        envelope = {
            **command,
            "protocol": WORKER_PROTOCOL,
            "id": "operator-" + hashlib.sha256(command_id.encode()).hexdigest()
            if command_id is not None
            else "stream-" + secrets.token_hex(24),
            "source_session_id": source,
            "sequence": sequence,
            "received_monotonic_ns": time.monotonic_ns(),
        }
        if len(canonical(envelope)) > 64 * 1024:
            raise ValueError("Native command exceeds its input budget")
        if command_id is not None:
            registered = await asyncio.to_thread(
                self.store.bind_worker_command,
                session_id,
                command_id,
                self.owner,
                envelope,
            )
            if registered["state"] in {"applied", "rejected"}:
                return registered["response"]
            if registered["state"] == "not_sent":
                raise SimulationConflict(registered["error"])
            envelope = registered["envelope"]
        if current.ending and kind != "stop":
            if command_id is not None:
                await asyncio.to_thread(
                    self.store.worker_result,
                    session_id,
                    command_id,
                    "unknown",
                    error="Session ended during command registration",
                )
            raise SimulationConflict("Session is stopping")
        identifier = envelope["id"]
        pending = current.commands.get(identifier)
        if pending is None:
            pending = {
                "envelope": envelope,
                "command_id": command_id,
                "future": asyncio.get_running_loop().create_future(),
                "accepted": False,
                "acceptance": None,
            }
            current.commands[identifier] = pending
        elif pending["envelope"] != envelope:
            raise SimulationConflict("Pending native command identity changed")
        try:
            await self._write(current, envelope)
            response = await asyncio.wait_for(asyncio.shield(pending["future"]), timeout=5)
        except (TimeoutError, BrokenPipeError, ConnectionResetError, SimulationConflict) as error:
            if command_id is not None:
                await asyncio.to_thread(
                    self.store.worker_result,
                    session_id,
                    command_id,
                    "unknown",
                    error=str(error) or type(error).__name__,
                )
            raise SimulationConflict("Native command outcome is unknown; retry its original identity") from error
        if response["type"] == "error":
            raise SimulationConflict(response["message"])
        return response

    async def _response(self, session_id, current, message):
        identifier = message.get("command_id")
        pending = current.commands.get(identifier)
        if pending is None:
            if current.completed.get(identifier) == digest(message):
                return
            raise ValueError("Worker returned an unmatched command response")
        if message.get("kind", pending["envelope"]["type"]) != pending["envelope"]["type"]:
            raise ValueError("Worker command response kind changed")
        if message["type"] == "accepted":
            if message.get("accepted") is not True or message.get("applied") is not False:
                raise ValueError("Worker acceptance is not an effective control result")
            if type(message.get("accepted_tick")) is not int or message["accepted_tick"] < 0:
                raise ValueError("Worker acceptance tick is invalid")
            if pending["acceptance"] is not None and message != pending["acceptance"]:
                raise ValueError("Worker returned conflicting acceptance evidence")
            pending["acceptance"] = message
            state = "accepted"
            pending["accepted"] = True
        elif message["type"] == "applied":
            if (
                message.get("accepted") is not True
                or message.get("applied") is not True
                or message.get("mode") != "kinematic_ik"
                or type(message.get("effective_tick")) is not int
                or message["effective_tick"] < 0
                or type(message.get("effective_time_s")) not in {int, float}
                or not math.isfinite(message["effective_time_s"])
                or message["effective_time_s"] < 0
                or not isinstance(message.get("state_digest"), str)
                or len(message["state_digest"]) != 64
                or any(character not in "0123456789abcdef" for character in message["state_digest"])
            ):
                raise ValueError("Worker effective command evidence is invalid")
            state = "applied"
        else:
            state = "rejected"
        if pending["command_id"] is not None:
            await asyncio.to_thread(
                self.store.worker_result,
                session_id,
                pending["command_id"],
                state,
                response=message,
                error=message.get("message") if state == "rejected" else None,
            )
        if state != "accepted":
            current.commands.pop(identifier)
            current.completed[identifier] = digest(message)
            if len(current.completed) > 512:
                current.completed.popitem(last=False)
            if state == "applied" and pending["envelope"]["type"] == "stop":
                current.stop_applied = True
            pending["future"].set_result(message)

    async def _read(self, session_id: str) -> None:
        current = self.running[session_id]
        error = None
        try:
            while line := await current.process.stdout.readline():
                message = json.loads(line)
                if message.get("protocol") != WORKER_PROTOCOL:
                    raise ValueError("Native worker protocol differs from the fixed contract")
                if message.get("type") == "ready":
                    row = await asyncio.to_thread(self.store.get, session_id)
                    if message.get("session_id") != session_id or message.get("mode") != "kinematic_ik":
                        raise ValueError("Native worker session identity or mode changed")
                    if message["model"].get("engine") != row["request"]["source"]["engine"]["mujoco"]:
                        raise ValueError("Native worker engine differs from the verified MJCF engine")
                    if current.model is not None:
                        raise ValueError("Native worker sent duplicate model initialization")
                    current.model = {"type": "model", **message["model"]}
                    current.state = message["model"]["state"]
                    if row["state"] == "starting":
                        await asyncio.to_thread(self.store.transition, session_id, self.owner, "ready")
                elif message.get("type") == "state":
                    current.state = message["state"]
                elif message.get("type") in {"accepted", "applied"} or (
                    message.get("type") == "error" and "command_id" in message
                ):
                    await self._response(session_id, current, message)
                elif message.get("type") == "error":
                    error = message["message"]
                elif message.get("type") == "stopped":
                    current.stopped = True
                    current.ending = True
                else:
                    raise ValueError("Native worker returned an unknown protocol message")
                async with current.condition:
                    current.sequence += 1
                    current.condition.notify_all()
            current.reader_eof = True
            await self._wait_leader(current, 3)
        except asyncio.CancelledError:
            error = "Supervisor cancelled the state reader"
        except Exception as failure:
            error = str(failure)
        finally:
            current.failure = current.failure or error
            await self._settle(session_id, current)

    async def _settle(self, session_id, current):
        settling = asyncio.create_task(self._settle_owned(session_id, current))
        try:
            return await asyncio.shield(settling)
        except asyncio.CancelledError:
            await settling
            raise

    async def _settle_owned(self, session_id, current):
        async with current.settlement_lock:
            row = await asyncio.to_thread(self.store.get, session_id)
            if row["state"] in {"stopped", "failed"}:
                self.running.pop(session_id, None)
                return
            current.ending = True
            if row["state"] in {"starting", "ready"}:
                row = await asyncio.to_thread(self.store.transition, session_id, self.owner, "stopping")
            for identifier, pending in list(current.commands.items()):
                current.failure = current.failure or "Worker exited with an unknown command outcome"
                if pending["command_id"] is not None:
                    await asyncio.to_thread(
                        self.store.worker_result,
                        session_id,
                        pending["command_id"],
                        "unknown",
                        error=current.failure,
                    )
                if not pending["future"].done():
                    pending["future"].set_result({"type": "error", "message": current.failure})
                current.commands.pop(identifier)
            await asyncio.gather(*list(current.deliveries), return_exceptions=True)
            await asyncio.to_thread(self.store.settle_worker_commands, session_id, self.owner)
            observed = (
                await asyncio.to_thread(resource_state, current.identity) if current.identity is not None else "unknown"
            )
            if observed != "dead":
                current.failure = current.failure or "Worker process group did not exit cleanly"
            quiescent = await self._terminate(current)
            if not quiescent:
                # A writer may still be active: preserve the original partial directory.
                await asyncio.to_thread(
                    self.store.transition,
                    session_id,
                    self.owner,
                    "unresolved",
                    error=current.failure or "Worker process group ownership is unresolved",
                    process=current.identity,
                )
            else:
                terminal = (
                    "stopped"
                    if current.process.returncode == 0
                    and row["state"] == "stopping"
                    and current.stop_applied
                    and current.stopped
                    and not current.failure
                    else "failed"
                )
                error = current.failure or (
                    None if terminal == "stopped" else f"Worker exited: {current.process.returncode}"
                )
                unresolved = [
                    item
                    for item in await asyncio.to_thread(self.store.worker_commands, session_id)
                    if item["result"] is not None
                    and item["result"].get("state") not in {"applied", "rejected", "not_sent"}
                ]
                if unresolved:
                    terminal, error = "failed", "Session has unknown registered command outcomes"
                try:
                    recording = await self._seal_recording(
                        current.root,
                        terminal == "stopped",
                        error,
                        runtime=row["request"]["runtime"],
                        save=row["request"]["record"],
                    )
                except OSError as failure:
                    if row["request"]["record"]:
                        raise
                    terminal, error, recording = "failed", f"Session evidence cleanup failed: {failure}", None
                await asyncio.to_thread(
                    self.store.transition,
                    session_id,
                    self.owner,
                    terminal,
                    error=error,
                    recording=recording,
                    process=current.identity,
                )
                self.running.pop(session_id, None)
            async with current.condition:
                current.sequence += 1
                current.condition.notify_all()
            for task in current.tasks[1:2]:
                task.cancel()

    async def _seal_recording(self, root, complete, error, *, runtime, save):
        directory = root / "recording"
        if not directory.is_dir():
            return None
        (directory / "outcome.json").write_bytes(canonical({"complete": complete, "error": error}) + b"\n")
        if not save:
            if complete:
                removing = asyncio.create_task(asyncio.to_thread(shutil.rmtree, directory))
                try:
                    await asyncio.shield(removing)
                except asyncio.CancelledError:
                    await removing
                    raise
            return None
        artifact = await asyncio.to_thread(
            self.artifacts.seal_tree,
            directory,
            kind="teleop_recording",
            producer_run_id=None,
            producer_stage=None,
            producer_tool_sha256=digest(runtime),
        )
        return artifact.to_record()

    @staticmethod
    async def _terminate(current):
        async with current.termination_lock:
            if current.identity is None:
                return False
            return await SimulationService._terminate_identity(current.identity)

    @staticmethod
    async def _terminate_identity(identity):
        # The event loop owns the subprocess reaper; this synchronous helper
        # observes the whole group while the loop keeps collecting child exits.
        terminating = asyncio.create_task(asyncio.to_thread(terminate_owned, identity))
        try:
            return await asyncio.shield(terminating)
        except asyncio.CancelledError:
            await terminating
            raise

    @staticmethod
    async def _wait_leader(current, timeout):
        # Process.wait may also wait for a pipe inherited by a surviving child.
        async with asyncio.timeout(timeout):
            while current.process.returncode is None:
                await asyncio.sleep(0.02)

    async def _watch(self, session_id: str) -> None:
        current = self.running[session_id]
        started = time.monotonic()
        checked_admission = 0
        try:
            while True:
                if current.reader_eof:
                    return
                if current.process.returncode is not None:
                    _, pending = await asyncio.wait(current.tasks[:1], timeout=1)
                    if pending:
                        current.failure = "Worker output remained open after its leader exited"
                        current.tasks[0].cancel()
                    return
                if current.ending:
                    return
                if time.monotonic() - checked_admission >= 1:
                    if not await asyncio.to_thread(self.store.admission_open):
                        current.tasks.append(asyncio.create_task(self.stop(session_id)))
                        return
                    checked_admission = time.monotonic()
                if (current.model is None and time.monotonic() - started > 60) or time.monotonic() - started > 3600:
                    current.failure = "Simulation exceeded its startup or session time budget"
                    await self._terminate(current)
                    current.tasks[0].cancel()
                    return
                recording = current.root / "recording"
                if sum(path.stat().st_size for path in recording.rglob("*") if path.is_file()) > 512 * 1024 * 1024:
                    current.failure = "Simulation exceeded its recording size budget"
                    await self._terminate(current)
                    current.tasks[0].cancel()
                    return
                if current.model is None:
                    await asyncio.sleep(0.1)
                    continue
                enabled = min(current.viewer_until, current.input_until) > time.monotonic()
                if current.input_connected != enabled:
                    await self.send(session_id, {"type": "connection", "connected": enabled})
                    current.input_connected = enabled
                await self.send(session_id, {"type": "heartbeat"})
                await asyncio.sleep(0.1)
        except Exception as error:
            current.failure = f"Simulation supervision failed: {type(error).__name__}"
            log.exception("Simulation supervision failed")
            await self._terminate(current)
            current.tasks[0].cancel()

    async def pair(self, session_id: str) -> str:
        current = self.running.get(session_id)
        if current is None:
            raise SimulationConflict("Native simulation worker is unavailable")
        if current.model is None or current.ending:
            raise SimulationConflict("Native simulation worker is not ready")
        if current.input_until > time.monotonic():
            raise SimulationConflict("A PICO input is already connected")
        code = secrets.token_urlsafe(24)
        current.pair_digest = hashlib.sha256(code.encode()).hexdigest()
        current.pair_until = time.monotonic() + 60
        return code

    async def consume_pair(self, session_id: str, code: str) -> str:
        if not isinstance(code, str) or not 1 <= len(code) <= 128:
            raise SimulationConflict("PICO pairing is invalid or expired")
        current = self.running.get(session_id)
        if (
            current is None
            or current.pair_until < time.monotonic()
            or current.pair_digest is None
            or not secrets.compare_digest(current.pair_digest, hashlib.sha256(code.encode()).hexdigest())
        ):
            raise SimulationConflict("PICO pairing is invalid or expired")
        current.pair_digest = None
        binding = secrets.token_hex(24)
        current.source_session = binding
        return binding

    async def stop(self, session_id: str, *, command_id=None) -> None:
        current = self.running.get(session_id)
        if current is None:
            row = await asyncio.to_thread(self.store.get, session_id)
            if row["state"] in {"stopped", "failed"}:
                return
            raise SimulationConflict("Session worker ownership is unresolved")
        async with current.stop_lock:
            await self._stop_current(session_id, current, command_id=command_id)

    async def _stop_current(self, session_id, current, *, command_id=None):
        row = await asyncio.to_thread(self.store.get, session_id)
        if row["state"] in {"stopped", "failed"}:
            return
        if row["state"] != "unresolved":
            await asyncio.to_thread(self.store.transition, session_id, self.owner, "stopping")
        current.ending = True
        current.input_until = 0
        current.source_session = None
        if len(current.tasks) > 1:
            current.tasks[1].cancel()
            await asyncio.gather(current.tasks[1], return_exceptions=True)
        if row["state"] != "unresolved" and current.process.returncode is None:
            try:
                await self.send(session_id, {"type": "stop"}, command_id=command_id)
                current.process.stdin.close()
                await asyncio.wait_for(current.process.stdin.wait_closed(), 2)
                await self._wait_leader(current, 5)
            except (TimeoutError, BrokenPipeError, ConnectionResetError, SimulationConflict):
                current.failure = current.failure or "Worker did not acknowledge a clean stop"
                await self._terminate(current)
        if current.tasks:
            _, pending = await asyncio.wait(current.tasks[:1], timeout=1)
            if pending:
                current.failure = current.failure or "Worker output did not close after stop"
                current.tasks[0].cancel()
            await asyncio.gather(current.tasks[0], return_exceptions=True)
        await asyncio.gather(*list(current.deliveries), return_exceptions=True)
        await self._settle(session_id, current)
        if (await asyncio.to_thread(self.store.get, session_id))["state"] == "unresolved":
            raise SimulationConflict("Session process group is unresolved; recording and capacity are retained")

    async def close(self) -> None:
        self._lifecycle_state = "closing"
        async with self._lifecycle_lock:
            async with self.gate:
                await self._close_owned()
            self._lifecycle_state = "closed"

    async def _close_owned(self) -> None:
        try:
            if self._verification_task is not None and self._verification_task is not asyncio.current_task():
                self._verification_task.cancel()
                await asyncio.gather(self._verification_task, return_exceptions=True)
            await asyncio.gather(*(self.stop(key) for key in list(self.running)), return_exceptions=True)
        finally:
            for current in list(self.running.values()):
                await self._terminate(current)
            for session_id, current in list(self.running.items()):
                await self._settle(session_id, current)
            if self._database_lock is not None:
                await self._reconcile_verifications()
            if not await self._close_probe():
                raise SimulationConflict("Startup process group is unresolved; startup ownership is retained")
            if self._lock is not None:
                self._lock.close()
                self._lock = None
            if self._database_lock is not None:
                self._database_lock.close()
                self._database_lock = None
            self.runtime_identity = None
