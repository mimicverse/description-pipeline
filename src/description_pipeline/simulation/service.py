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
from .store import SimulationConflict, SimulationStore

log = logging.getLogger(__name__)


@dataclass
class RunningSession:
    process: asyncio.subprocess.Process
    root: Path
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
        self._lock = None
        self._database_lock = None

    async def start(self) -> None:
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
        contract = json.loads(Path(__file__).with_name("teleop-runtime.json").read_bytes())
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
            await self.close()
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
        self._database_lock = await asyncio.to_thread(self.store.acquire_supervisor)
        for row in await asyncio.to_thread(self.store.orphaned, self.owner):
            identity = row["process_identity"]
            if identity is not None and self._process_exists(identity):
                await asyncio.sleep(5.5)
            if identity is not None and self._process_exists(identity):
                await asyncio.to_thread(self.store.reconcile_orphan, row["session_id"], row["owner_nonce"])
            else:
                recording = await self._seal_recording(self.root / row["session_id"], False, "Supervisor interrupted")
                await asyncio.to_thread(
                    self.store.transition,
                    row["session_id"],
                    row["owner_nonce"],
                    "failed",
                    error="Supervisor interrupted; previous process no longer exists",
                    recording=recording,
                )

    @staticmethod
    def _process_exists(identity):
        try:
            if identity.get("boot_id") != Path("/proc/sys/kernel/random/boot_id").read_text().strip():
                return False
            fields = Path(f"/proc/{int(identity['pid'])}/stat").read_text().split(") ", 1)[1].split()
            return fields[19] == identity["start_time"] and fields[0] != "Z"
        except FileNotFoundError:
            return False

    async def create(self, actor: str, key: str, run_id: str, request: dict, members: dict, read_member) -> dict:
        if not key or len(key) > 128:
            raise ValueError("A bounded idempotency key is required")
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
                if len(request["source"]["files"]) > 2048:
                    raise ValueError("Simulation source exceeds the file-count budget")
                source.mkdir(parents=True, exist_ok=False, mode=0o700)
                source_bytes = 0
                for name, sha in request["source"]["files"].items():
                    parts = Path(name)
                    if parts.is_absolute() or ".." in parts.parts or "\\" in name:
                        raise ValueError("Simulation source path is invalid")
                    content = await asyncio.to_thread(read_member, name, members[name])
                    source_bytes += len(content)
                    if len(content) > 64 * 1024 * 1024 or source_bytes > 1024 * 1024 * 1024:
                        raise ValueError("Simulation source exceeds the materialization budget")
                    if hashlib.sha256(content).hexdigest() != sha:
                        raise ValueError("Simulation source digest changed")
                    path = source / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(content)
                    path.chmod(0o400)
                (session_root / "request.json").write_bytes(canonical(request) + b"\n")
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
                    config["record_path"] = str(output / "ticks.jsonl")
                environment = {key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL") if key in os.environ}
                environment.update({"PYTHONUNBUFFERED": "1", "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1"})
                error_log = (session_root / "worker-stderr.log").open("xb")
                try:
                    process = await asyncio.create_subprocess_exec(
                        shutil.which("prlimit"),
                        "--as=4294967296",
                        "--nofile=256",
                        "--fsize=536870912",
                        "--",
                        str(self.python),
                        "-I",
                        "-B",
                        "-m",
                        "teleop.backends.mujoco.upper_body_worker",
                        stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=error_log,
                        cwd=session_root,
                        env=environment,
                        start_new_session=True,
                        limit=64 * 1024 * 1024,
                    )
                finally:
                    error_log.close()
                current = RunningSession(process, session_root)
                self.running[session_id] = current
                identity = {
                    "pid": process.pid,
                    "owner": self.owner,
                    "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
                    "start_time": Path(f"/proc/{process.pid}/stat").read_text().split(") ", 1)[1].split()[19],
                }
                await asyncio.to_thread(self.store.transition, session_id, self.owner, "starting", process=identity)
                current.tasks = [
                    asyncio.create_task(self._read(session_id)),
                    asyncio.create_task(self._watch(session_id)),
                ]
                await self.send(session_id, config)
                await self.send(session_id, {"type": "connection", "connected": False})
                return self.public(row)
            except Exception as error:
                if session_id in self.running:
                    await self._terminate(self.running[session_id])
                    if self.running[session_id].tasks:
                        await self.running[session_id].tasks[0]
                    else:
                        self.running.pop(session_id, None)
                        await asyncio.to_thread(
                            self.store.transition, session_id, self.owner, "failed", error=str(error)
                        )
                else:
                    await asyncio.to_thread(self.store.transition, session_id, self.owner, "failed", error=str(error))
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
            await current.process.wait()
        except asyncio.CancelledError:
            error = "Supervisor cancelled the state reader"
            await self._terminate(current)
        except Exception as failure:
            error = str(failure)
            await self._terminate(current)
        finally:
            try:
                row = await asyncio.to_thread(self.store.get, session_id)
                terminal = (
                    "stopped"
                    if current.process.returncode == 0 and row["state"] == "stopping" and not error
                    else "failed"
                )
                error = (
                    error
                    or current.failure
                    or (None if terminal == "stopped" else f"Worker exited: {current.process.returncode}")
                )
                recording = await self._seal_recording(current.root, terminal == "stopped", error)
                if row["state"] not in {"stopped", "failed"}:
                    await asyncio.to_thread(
                        self.store.transition,
                        session_id,
                        self.owner,
                        terminal,
                        error=error,
                        recording=recording,
                    )
            finally:
                async with current.condition:
                    current.sequence += 1
                    current.condition.notify_all()
                self.running.pop(session_id, None)
                for task in current.tasks[1:2]:
                    task.cancel()

    async def _seal_recording(self, root, complete, error):
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
            producer_tool_sha256=digest(self.runtime_identity),
        )
        return artifact.to_record()

    @staticmethod
    async def _terminate(current):
        if current.process.returncode is None:
            current.process.terminate()
            try:
                await asyncio.wait_for(current.process.wait(), 3)
            except TimeoutError:
                current.process.kill()
                await current.process.wait()

    async def _watch(self, session_id: str) -> None:
        current = self.running[session_id]
        started = time.monotonic()
        checked_admission = 0
        try:
            while current.process.returncode is None:
                if time.monotonic() - checked_admission >= 1:
                    if not await asyncio.to_thread(self.store.admission_open):
                        current.tasks.append(asyncio.create_task(self.stop(session_id)))
                        return
                    checked_admission = time.monotonic()
                if (current.model is None and time.monotonic() - started > 60) or time.monotonic() - started > 3600:
                    current.failure = "Simulation exceeded its startup or session time budget"
                    await self._terminate(current)
                    return
                recording = current.root / "recording" / "ticks.jsonl"
                if recording.exists() and recording.stat().st_size > 512 * 1024 * 1024:
                    current.failure = "Simulation exceeded its recording size budget"
                    await self._terminate(current)
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
        await asyncio.to_thread(self.store.transition, session_id, self.owner, "stopping")
        if len(current.tasks) > 1:
            current.tasks[1].cancel()
            await asyncio.gather(current.tasks[1], return_exceptions=True)
        if current.process.returncode is None:
            try:
                await self.send(session_id, {"type": "stop"})
                current.process.stdin.close()
                await current.process.stdin.wait_closed()
                await asyncio.wait_for(current.process.wait(), 5)
            except (TimeoutError, BrokenPipeError, ConnectionResetError, SimulationConflict):
                await self._terminate(current)
        if current.tasks:
            await current.tasks[0]
            current.tasks[1].cancel()

    async def close(self) -> None:
        try:
            await asyncio.gather(*(self.stop(key) for key in list(self.running)), return_exceptions=True)
        finally:
            for current in list(self.running.values()):
                await self._terminate(current)
            if self._lock is not None:
                self._lock.close()
                self._lock = None
            if self._database_lock is not None:
                self._database_lock.close()
                self._database_lock = None


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
