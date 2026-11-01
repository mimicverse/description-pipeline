"""Supervise private native workers without occupying an Airflow task."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import hashlib
import fcntl
import json
import logging
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
from .recording import prepare_source, recording_binding, run_verification, session_identity
from .schemas import load_runtime_contract
from .warnings import warning_inventory

log = logging.getLogger(__name__)


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
    input_connected: bool = False
    source_session: str | None = None
    stdin_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    stop_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    termination_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    settlement_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    tasks: list = field(default_factory=list)
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

    async def start(self) -> None:
        if self._lock is not None or self._database_lock is not None:
            raise SimulationConflict("Simulation supervisor is already started")
        try:
            await self._start()
        except BaseException:
            await self.close()
            raise

    async def _start(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.root.is_symlink() or not self.python.is_absolute() or not self.python.is_file():
            raise ValueError("Simulation root or worker interpreter is invalid")
        if shutil.which("prlimit") is None:
            raise ValueError("Native worker resource control requires prlimit")
        self._lock = (self.root / "supervisor.lock").open("a")
        try:
            fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._lock.close()
            self._lock = None
            raise ValueError("Another simulation supervisor owns this root") from None
        contract = load_runtime_contract(Path(__file__).with_name("teleop-runtime.json"))
        probe = await asyncio.create_subprocess_exec(
            str(self.python),
            "-I",
            "-B",
            "-c",
            _IDENTITY_PROBE,
            json.dumps(list(contract["dependency_versions"])),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
        )
        try:
            stdout, stderr = await asyncio.wait_for(probe.communicate(), 30)
        except BaseException:
            if probe.returncode is None:
                probe.kill()
                await probe.wait()
            raise
        if probe.returncode != 0:
            raise ValueError(f"Teleop runtime identity failed: {stderr.decode(errors='replace')[-1000:]}")
        self.runtime_identity = json.loads(stdout)
        if {name: item["version"] for name, item in self.runtime_identity["packages"].items()} != contract[
            "dependency_versions"
        ]:
            raise ValueError("Worker dependency versions differ from the fixed integration lock")
        if self.runtime_identity.pop("teleop_files") != contract["files"]:
            raise ValueError("Installed Teleop package differs from the fixed source wheel")
        self.runtime_identity["source_commit"] = contract["source_commit"]
        self.runtime_identity["wheel_sha256"] = contract["wheel_sha256"]
        acquiring = asyncio.create_task(asyncio.to_thread(self.store.acquire_supervisor))
        try:
            self._database_lock = await asyncio.shield(acquiring)
        except asyncio.CancelledError:
            # The database call continues in its thread; cleanup must own its returned lease.
            (result,) = await asyncio.gather(acquiring, return_exceptions=True)
            if not isinstance(result, BaseException):
                self._database_lock = result
            raise
        for row in await asyncio.to_thread(self.store.orphaned, self.owner):
            identity = row["process_identity"]
            quiescent = identity is not None and await self._terminate_identity(identity)
            if not quiescent:
                await asyncio.to_thread(self.store.reconcile_orphan, row["session_id"], row["owner_nonce"])
            else:
                recording = await self._seal_recording(
                    self.root / row["session_id"], False, "Supervisor interrupted", runtime=row["request"]["runtime"]
                )
                await asyncio.to_thread(
                    self.store.transition,
                    row["session_id"],
                    row["owner_nonce"],
                    "failed",
                    error="Supervisor interrupted; previous process group is absent",
                    recording=recording,
                )

    async def create(self, actor: str, key: str, run_id: str, request: dict, members: dict, read_member) -> dict:
        if not key or len(key) > 128:
            raise ValueError("A bounded idempotency key is required")
        if self.runtime_identity is None or request.get("runtime") != self.runtime_identity:
            raise ValueError("Simulation request runtime differs from the active fixed worker")
        warning_inventory(request["source"])
        async with self.gate:
            row = await asyncio.to_thread(
                self.store.create, actor, key, run_id, request, self.owner, capacity=self.capacity
            )
            session_id = row["session_id"]
            if session_id in self.running or row["state"] != "starting" or row["owner_nonce"] != self.owner:
                return self.public(row)
            session_root = self.root / session_id
            source = session_root / "source"
            try:
                await prepare_source(source, request["source"], members, read_member)
                config = {
                    "root": str(source),
                    "inventory": request["source"]["files"],
                    "entry": request["source"]["entry"],
                    "profile": request["profile"],
                    **request["settings"],
                }
                if request["record"]:
                    output = session_root / "recording"
                    output.mkdir(mode=0o700)
                    (output / "request.json").write_bytes(canonical(request) + b"\n")
                    (output / "session.json").write_bytes(canonical(session_identity(row)) + b"\n")
                    config["record_path"] = str(output / "ticks.jsonl")
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
                await asyncio.to_thread(
                    self.store.transition, session_id, self.owner, "starting", process=current.identity
                )
                current.tasks = [
                    asyncio.create_task(self._read(session_id)),
                    asyncio.create_task(self._watch(session_id)),
                ]
                await self.send(session_id, config)
                await self.send(session_id, {"type": "connection", "connected": False})
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
        if self.verification_gate.locked():
            raise SimulationConflict("A recording verification is already running; retry after it finishes")
        async with self.verification_gate:
            row = await asyncio.to_thread(self.store.get, session_id)
            binding = recording_binding(row)
            with tempfile.TemporaryDirectory(prefix="recording-verification-", dir=self.root) as temporary:
                source = Path(temporary) / "source"
                await prepare_source(source, row["request"]["source"], members, read_member)
                return await run_verification(binding, self.artifacts.root, source)

    async def send(self, session_id: str, command: dict) -> None:
        current = self.running.get(session_id)
        if current is None or current.process.returncode is not None:
            raise SimulationConflict("Native simulation worker is unavailable")
        async with current.stdin_lock:
            current.process.stdin.write(canonical(command) + b"\n")
            await asyncio.wait_for(current.process.stdin.drain(), timeout=2)

    async def _read(self, session_id: str) -> None:
        current = self.running[session_id]
        error = None
        try:
            while line := await current.process.stdout.readline():
                message = json.loads(line)
                if message.get("type") == "model":
                    row = await asyncio.to_thread(self.store.get, session_id)
                    if message.get("engine") != row["request"]["source"]["engine"]["mujoco"]:
                        raise ValueError("Native worker engine differs from the verified MJCF engine")
                    if current.model is not None:
                        raise ValueError("Native worker sent duplicate model initialization")
                    current.model = message
                    if row["state"] == "starting":
                        await asyncio.to_thread(self.store.transition, session_id, self.owner, "ready")
                elif message.get("type") == "state":
                    current.state = message["state"]
                elif message.get("type") == "error":
                    error = message["message"]
                elif message.get("type") != "stopped":
                    raise ValueError("Native worker returned an unknown protocol message")
                async with current.condition:
                    current.sequence += 1
                    current.condition.notify_all()
            await self._wait_leader(current, 3)
        except asyncio.CancelledError:
            error = "Supervisor cancelled the state reader"
        except Exception as failure:
            error = str(failure)
        finally:
            current.failure = current.failure or error
            await self._settle(session_id, current)

    async def _settle(self, session_id, current):
        async with current.settlement_lock:
            row = await asyncio.to_thread(self.store.get, session_id)
            if row["state"] in {"stopped", "failed"}:
                self.running.pop(session_id, None)
                return
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
                    if current.process.returncode == 0 and row["state"] == "stopping" and not current.failure
                    else "failed"
                )
                error = current.failure or (
                    None if terminal == "stopped" else f"Worker exited: {current.process.returncode}"
                )
                recording = await self._seal_recording(
                    current.root, terminal == "stopped", error, runtime=row["request"]["runtime"]
                )
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

    async def _seal_recording(self, root, complete, error, *, runtime):
        directory = root / "recording"
        if not directory.is_dir():
            return None
        (directory / "outcome.json").write_bytes(canonical({"complete": complete, "error": error}) + b"\n")
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
                if current.process.returncode is not None:
                    _, pending = await asyncio.wait(current.tasks[:1], timeout=1)
                    if pending:
                        current.failure = "Worker output remained open after its leader exited"
                        current.tasks[0].cancel()
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
                recording = current.root / "recording" / "ticks.jsonl"
                if recording.exists() and recording.stat().st_size > 512 * 1024 * 1024:
                    current.failure = "Simulation exceeded its recording size budget"
                    await self._terminate(current)
                    current.tasks[0].cancel()
                    return
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

    async def stop(self, session_id: str) -> None:
        current = self.running.get(session_id)
        if current is None:
            row = await asyncio.to_thread(self.store.get, session_id)
            if row["state"] in {"stopped", "failed"}:
                return
            raise SimulationConflict("Session worker ownership is unresolved")
        async with current.stop_lock:
            await self._stop_current(session_id, current)

    async def _stop_current(self, session_id, current):
        row = await asyncio.to_thread(self.store.get, session_id)
        if row["state"] in {"stopped", "failed"}:
            return
        if row["state"] != "unresolved":
            await asyncio.to_thread(self.store.transition, session_id, self.owner, "stopping")
        if len(current.tasks) > 1:
            current.tasks[1].cancel()
            await asyncio.gather(current.tasks[1], return_exceptions=True)
        if row["state"] != "unresolved" and current.process.returncode is None:
            try:
                await self.send(session_id, {"type": "stop"})
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
        await self._settle(session_id, current)
        if (await asyncio.to_thread(self.store.get, session_id))["state"] == "unresolved":
            raise SimulationConflict("Session process group is unresolved; recording and capacity are retained")

    async def close(self) -> None:
        try:
            await asyncio.gather(*(self.stop(key) for key in list(self.running)), return_exceptions=True)
        finally:
            for current in list(self.running.values()):
                await self._terminate(current)
            for session_id, current in list(self.running.items()):
                await self._settle(session_id, current)
            if self._lock is not None:
                self._lock.close()
                self._lock = None
            if self._database_lock is not None:
                self._database_lock.close()
                self._database_lock = None
            self.runtime_identity = None


_IDENTITY_PROBE = """
import base64,hashlib,importlib.metadata as m,json,sys
import mujoco,teleop
packages={}
for name in json.loads(sys.argv[1]):
    d=m.distribution(name)
    rows=[]
    for f in d.files or []:
        if f.hash is None:
            continue
        p=d.locate_file(f)
        if not p.resolve().is_relative_to(__import__('pathlib').Path(sys.prefix).resolve()):
            raise ValueError('Runtime dependency escapes its installation')
        actual=hashlib.new(f.hash.mode,p.read_bytes()).digest()
        if base64.urlsafe_b64encode(actual).decode().rstrip('=')!=f.hash.value:
            raise ValueError('Runtime dependency bytes differ from RECORD')
        rows.append((str(f),actual.hex()))
    if not rows:
        raise ValueError('Runtime dependency has no verifiable inventory')
    packages[name]={'version':d.version,'files_sha256':hashlib.sha256(json.dumps(sorted(rows)).encode()).hexdigest()}
if packages['robot-teleop']['version']!='0.3.0' or mujoco.mj_versionString()!='3.13.0':
    raise ValueError('Unsupported Teleop runtime contract')
from pathlib import Path
root=Path(teleop.__file__).resolve().parent
if not root.is_relative_to(Path(sys.prefix).resolve()):
    raise ValueError('Teleop must be installed from a fixed noneditable wheel')
files={'teleop/'+p.relative_to(root).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
       for p in root.rglob('*') if p.is_file() and '__pycache__' not in p.parts}
print(json.dumps({'schema_version':'teleop.runtime/v1','packages':packages,
                  'teleop_files':files,'engine':mujoco.mj_versionString()}))
"""
