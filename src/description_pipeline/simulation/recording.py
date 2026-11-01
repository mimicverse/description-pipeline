"""Bind recording verification to registered facts and bounded native execution."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import shutil
import secrets

from ..io import canonical, digest
from ..owned_process import process_identity, resource_state, terminate_owned
from ..pipeline.contracts import ArtifactRef
from .store import SimulationConflict
from .runtime import TELEOP_RECORDING_SCHEMA, RECORDING_VERIFICATION_SCHEMA
from .warnings import warning_inventory, verify_warning_inventory


def materialize_source(destination: Path, source: dict, members: dict, read_member) -> Path:
    files = source["files"]
    certificate = warning_inventory(source)["certificate"]
    if files.get(certificate["path"]) != certificate["sha256"]:
        raise ValueError("Source files do not bind the original warning certificate")
    if len(files) > 2048:
        raise ValueError("Simulation source exceeds the file-count budget")
    destination.mkdir(parents=True, exist_ok=False, mode=0o700)
    total = 0
    for name, expected in files.items():
        relative = Path(name)
        if (
            not name
            or relative.is_absolute()
            or relative.as_posix() != name
            or ".." in relative.parts
            or "\\" in name
            or ":" in name
        ):
            raise ValueError("Simulation source path is invalid")
        member = certificate if name == certificate["path"] else members[name]
        content = read_member(name, member)
        total += len(content)
        if len(content) > 64 * 1024 * 1024 or total > 1024 * 1024 * 1024:
            raise ValueError("Simulation source exceeds the materialization budget")
        if hashlib.sha256(content).hexdigest() != expected:
            raise ValueError("Simulation source digest changed")
        path = destination / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        path.chmod(0o400)
    verify_warning_inventory(source, destination)
    return destination


def session_identity(row: dict) -> dict:
    if digest(row["request"]) != row["request_digest"]:
        raise SimulationConflict("Stored simulation request digest is inconsistent")
    return {
        "schema_version": TELEOP_RECORDING_SCHEMA,
        "session_id": row["session_id"],
        "actor": row["actor"],
        "source_run_id": row["source_run_id"],
        "request_digest": row["request_digest"],
        "runtime_digest": digest(row["request"]["runtime"]),
    }


def recording_binding(row: dict) -> dict:
    """The caller supplies the current row read from SimulationStore, never request JSON."""
    if row["state"] not in {"stopped", "failed"} or row["recording_artifact"] is None:
        raise SimulationConflict("A terminal session with a sealed recording is required")
    identity = session_identity(row)
    artifact = ArtifactRef.from_record(row["recording_artifact"])
    if artifact.kind != "teleop_recording" or artifact.producer_tool_sha256 != identity["runtime_digest"]:
        raise SimulationConflict("Recording producer differs from the registered session runtime")
    return {
        **identity,
        "schema_version": RECORDING_VERIFICATION_SCHEMA,
        "recording_artifact": artifact.to_record(),
    }


async def prepare_source(destination: Path, source: dict, members: dict, read_member) -> Path:
    task = asyncio.create_task(asyncio.to_thread(materialize_source, destination, source, members, read_member))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # A filesystem reader cannot be cancelled halfway through cleanup of its private directory.
        await asyncio.gather(task, return_exceptions=True)
        raise


class VerificationUnresolved(SimulationConflict):
    """A managed verifier still owns its original partial inputs and capacity."""


def verification_execution(*, consumer="pico") -> dict:
    from .. import runtime as product_runtime
    from .runtime import load_runtime_contract

    tool = product_runtime.tool_record(role="portable")
    release = tool["release"]
    directory = Path(__file__).parent
    snapshots = {}
    for name in ("verify_recording.py", "runtime.py", "teleop-runtime.json"):
        path = directory / name
        if path.is_symlink():
            raise ValueError("A consumer resource must not be a symbolic link")
        content = path.read_bytes()
        actual = hashlib.sha256(content).hexdigest()
        # Installed production uses the existing package inventory. Source-mode
        # software tests retain their existing unqualified, construction-time basis.
        expected = release["package_files"].get("simulation/" + name) if release is not None else actual
        if actual != expected:
            raise ValueError("Consumer bytes differ from the installed release inventory")
        snapshots[name] = (content, expected)
    return {
        "helper_sha256": snapshots["verify_recording.py"][1],
        "runtime_helper_sha256": snapshots["runtime.py"][1],
        "runtime_contract": load_runtime_contract(snapshots["teleop-runtime.json"][0], consumer=consumer),
        "pipeline_source_sha256": tool["source_sha256"],
        "release_inventory_bound": release is not None,
    }


def _helper_command(python: Path, nonce: str, helper: Path, expected: str, operation: str) -> list[str]:
    if not python.is_absolute() or not python.is_file():
        raise ValueError("Native verification requires an absolute fixed PICO interpreter")
    limiter = shutil.which("prlimit")
    if limiter is None:
        raise ValueError("Native verification requires resource control")
    # Only standard-library bootstrap code runs before the registered stdin gate.
    entry = """import hashlib,json,shutil,sys
from pathlib import Path
payload = None
try:
    raw = sys.stdin.buffer.read(4194305)
    if not raw or len(raw) > 4194304:
        raise ValueError('Recording verifier initialization is missing or oversized')
    payload = json.loads(raw)
    path = Path(sys.argv[1])
    code = path.read_bytes()
    if hashlib.sha256(code).hexdigest() != sys.argv[2]:
        raise ValueError(('Runtime' if sys.argv[3] == 'runtime' else 'Recording') + ' helper bytes changed')
    scope = {'__file__': str(path), '__name__': 'consumer_native_helper'}
    exec(compile(code, str(path), 'exec'), scope)
    if sys.argv[3] == 'runtime':
        print(json.dumps(scope['verify_runtime_contract'](payload['runtime_contract'])))
    else:
        raise SystemExit(scope['worker_main'](payload))
except Exception as error:
    if isinstance(payload, dict) and payload.get('cleanup') is not None:
        cleanup = Path(payload['cleanup'])
        if not cleanup.is_symlink() and Path(payload['source']).parent == cleanup:
            shutil.rmtree(cleanup)
    print(json.dumps({'error': str(error)[:4000]}))
    raise SystemExit(1)
"""
    return [
        limiter, "--as=4294967296", "--nofile=128", "--cpu=180", "--",
        str(python), "-I", "-B", "-c", entry,
        str(helper), expected, operation, nonce,
    ]


def evidence_command(python: Path, nonce: str, execution: dict) -> list[str]:
    return _helper_command(
        python, nonce, Path(__file__).with_name("verify_recording.py"), execution["helper_sha256"], "recording"
    )


def runtime_command(python: Path, nonce: str, execution: dict) -> list[str]:
    return _helper_command(
        python, nonce, Path(__file__).with_name("runtime.py"), execution["runtime_helper_sha256"], "runtime"
    )


async def run_verification(payload: dict, python: Path, *, register_process) -> dict:
    from .verify_recording import INPUT_LIMIT

    content = canonical(payload)
    if len(content) > INPUT_LIMIT:
        raise ValueError("Recording verifier initialization exceeds its input budget")
    nonce = secrets.token_hex(24)
    spawning = asyncio.create_task(
        asyncio.create_subprocess_exec(
            *evidence_command(python, nonce, payload["execution"]),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1"},
        )
    )
    process = None
    identity = None
    try:
        interrupted = False
        try:
            process = await asyncio.shield(spawning)
        except asyncio.CancelledError:
            process = await spawning
            interrupted = True
        identity = {"pid": process.pid, "nonce": nonce}
        try:
            identity = process_identity(process.pid, nonce)
        finally:
            await register_process(identity)
        if interrupted:
            raise asyncio.CancelledError
        stdout, stderr = await asyncio.wait_for(process.communicate(content), 240)
        if len(stdout) > 128 * 1024 or len(stderr) > 128 * 1024:
            raise ValueError("Recording verifier exceeded its output budget")
        if process.returncode != 0:
            try:
                error = json.loads(stdout)["error"]
            except (ValueError, KeyError, TypeError):
                error = f"Native recording verifier exited: {process.returncode}"
            raise ValueError(error)
        result = json.loads(stdout)
        if result.get("binding") != payload["binding"]:
            raise ValueError("Recording verifier returned a different session binding")
        expected_execution = {key: value for key, value in payload["execution"].items() if key != "runtime_contract"}
        if result.get("execution") != expected_execution or result.get("verifier_pid") != process.pid:
            raise ValueError("Recording verifier returned a different execution identity")
        if await asyncio.to_thread(resource_state, identity) != "dead":
            raise ValueError("Recording verifier process group did not exit cleanly")
        return result
    finally:
        if process is not None:
            terminating = asyncio.create_task(asyncio.to_thread(terminate_owned, identity))
            try:
                quiescent = await asyncio.shield(terminating)
            except asyncio.CancelledError:
                quiescent = await terminating
                if quiescent:
                    raise
            if not quiescent:
                raise VerificationUnresolved("Recording verifier process group is unresolved")
