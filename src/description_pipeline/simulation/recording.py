"""Bind recording verification to registered facts and bounded native execution."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import shutil
import secrets
import sys

from ..io import canonical, digest
from ..owned_process import process_identity, resource_state, terminate_owned
from ..pipeline.contracts import ArtifactRef
from .store import SimulationConflict
from .schemas import TELEOP_RECORDING_SCHEMA, RECORDING_VERIFICATION_SCHEMA
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


async def run_verification(binding: dict, artifacts_root: Path, source: Path, *, register_process) -> dict:
    limiter = shutil.which("prlimit")
    if limiter is None:
        raise ValueError("Recording verification requires native resource control")
    # Resolve the exact package closure used by this portal, including in isolated development tests.
    package_root = Path(__file__).resolve().parents[2]
    entry = (
        "import sys;sys.path.insert(0,sys.argv[1]);"
        "from description_pipeline.simulation.verify_recording import worker_main;"
        "raise SystemExit(worker_main())"
    )
    nonce = secrets.token_hex(24)
    spawning = asyncio.create_task(
        asyncio.create_subprocess_exec(
            limiter,
            "--as=4294967296",
            "--nofile=128",
            "--cpu=180",
            "--",
            sys.executable,
            "-I",
            "-B",
            "-c",
            entry,
            str(package_root),
            nonce,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1"},
        )
    )
    payload = {"binding": binding, "artifact_root": str(artifacts_root), "source": str(source)}
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
        stdout, stderr = await asyncio.wait_for(process.communicate(canonical(payload) + b"\n"), 240)
        if len(stdout) > 128 * 1024 or len(stderr) > 128 * 1024:
            raise ValueError("Recording verifier exceeded its output budget")
        if process.returncode != 0:
            try:
                error = json.loads(stdout)["error"]
            except (ValueError, KeyError, TypeError):
                error = f"Native recording verifier exited: {process.returncode}"
            raise ValueError(error)
        result = json.loads(stdout)
        if result.get("binding") != binding:
            raise ValueError("Recording verifier returned a different session binding")
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
