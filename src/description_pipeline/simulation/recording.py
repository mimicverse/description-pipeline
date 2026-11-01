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
from .resources import SESSION_QUOTA_BYTES
from .evidence import validate_finalization_proof
from .tasks import settle_task, wait_task, run_thread
from .warnings import warning_inventory, verify_warning_inventory


INPUT_LIMIT = 4 * 1024 * 1024
TELEOP_RECORDING_SCHEMA = "solidworks-to-urdf.teleop-recording/v1"
RECORDING_VERIFICATION_SCHEMA = "solidworks-to-urdf.recording-verification/v1"


def prepare_bound(binding: dict, artifacts, source: Path, *, commands: list, execution: dict) -> dict:
    """Check platform facts before handing non-secret immutable data to native code."""

    if binding.get("schema_version") != RECORDING_VERIFICATION_SCHEMA:
        raise ValueError("Recording verification binding is unsupported")
    artifact = ArtifactRef.from_record(binding["recording_artifact"])
    if artifact.kind != "teleop_recording" or artifact.producer_tool_sha256 != binding["runtime_digest"]:
        raise ValueError("Recording artifact producer differs from the registered session")
    inventory = {item["path"]: item["sha256"] for item in artifacts.verify(artifact)["members"]}
    inventory["manifest.json"] = artifact.manifest_sha256
    root = artifacts.objects / artifact.artifact_id
    identity = json.loads((root / "session.json").read_bytes())
    expected_identity = {key: value for key, value in binding.items() if key != "recording_artifact"}
    expected_identity["schema_version"] = TELEOP_RECORDING_SCHEMA
    if identity != expected_identity:
        raise ValueError("Recording session identity differs from the registered session")
    request = json.loads((root / "request.json").read_bytes())
    if digest(request) != binding["request_digest"]:
        raise ValueError("Recording request differs from the registered session")
    if digest(request["runtime"]) != binding["runtime_digest"]:
        raise ValueError("Recording runtime differs from the registered session")
    outcome = json.loads((root / "outcome.json").read_bytes())
    if set(outcome) != {
        "complete",
        "error",
        "manifest_sha256",
        "container_complete",
        "streams",
        "session_complete",
        "formal_one_hour_qualified",
        "finalization_verification",
    } or any(
        type(outcome[key]) is not bool
        for key in (
            "complete",
            "container_complete",
            "session_complete",
            "formal_one_hour_qualified",
        )
    ):
        raise ValueError("Recording completion metadata is invalid")
    if not (outcome["complete"] and outcome["container_complete"] and outcome["session_complete"]):
        raise ValueError("Diagnostic recording is not a complete session verification input")
    if outcome["manifest_sha256"] != inventory.get("native/session-manifest.json"):
        raise ValueError("Platform outcome differs from its committed native manifest")
    manifest = json.loads((root / "native/session-manifest.json").read_bytes())
    validate_finalization_proof(
        Path(manifest["resource_policy"]["session_dir"]), manifest, outcome["finalization_verification"]
    )
    warning_identity = verify_warning_inventory(request["source"], source)
    return {
        "binding": binding,
        "recording": str(root),
        "source": str(source),
        "request": request,
        "inventory": inventory,
        "commands": commands,
        "execution": execution,
        "outcome": outcome,
        "warning_inventory": warning_identity,
    }


def _source_members(source: dict, members: dict):
    files = source["files"]
    certificate = warning_inventory(source)["certificate"]
    if files.get(certificate["path"]) != certificate["sha256"]:
        raise ValueError("Source files do not bind the original warning certificate")
    if len(files) > 2048:
        raise ValueError("Simulation source exceeds the file-count budget")
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
        if (
            member["sha256"] != expected
            or type(member["bytes"]) is not int
            or not 0 <= member["bytes"] <= 64 * 1024 * 1024
        ):
            raise ValueError("Simulation source member identity or declared size is invalid")
        yield relative, expected, member


def declared_source_bytes(source: dict, members: dict) -> int:
    """Read only exact member sizes from the bound immutable artifact inventory."""
    return sum(member["bytes"] for _relative, _expected, member in _source_members(source, members))


def materialize_source(
    destination: Path, source: dict, members: dict, read_member, *, max_bytes=SESSION_QUOTA_BYTES
) -> Path:
    if type(max_bytes) is not int or not 0 < max_bytes <= SESSION_QUOTA_BYTES:
        raise ValueError("Simulation source requires an admitted byte budget")
    destination.mkdir(parents=True, exist_ok=False, mode=0o700)
    total = 0
    for relative, expected, member in _source_members(source, members):
        content = read_member(relative.as_posix(), member)
        total += len(content)
        if len(content) > 64 * 1024 * 1024 or total > max_bytes:
            raise ValueError("Simulation source exceeds the materialization budget")
        if member["bytes"] != len(content):
            raise ValueError("Simulation source differs from its declared member size")
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


async def prepare_source(
    destination: Path, source: dict, members: dict, read_member, *, max_bytes=SESSION_QUOTA_BYTES
) -> Path:
    return await run_thread(materialize_source, destination, source, members, read_member, max_bytes=max_bytes)


class VerificationUnresolved(SimulationConflict):
    """A managed verifier still owns its original partial inputs and capacity."""


def verification_execution(*, consumer="pico") -> dict:
    from .. import runtime as product_runtime

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
    helper = directory / "runtime.py"
    namespace = {"__file__": str(helper), "__name__": "verified_consumer_contract"}
    exec(compile(snapshots["runtime.py"][0], str(helper), "exec"), namespace)
    return {
        "helper_sha256": snapshots["verify_recording.py"][1],
        "runtime_helper_sha256": snapshots["runtime.py"][1],
        "runtime_contract": namespace["load_runtime_contract"](snapshots["teleop-runtime.json"][0], consumer=consumer),
        "pipeline_source_sha256": tool["source_sha256"],
        "release_inventory_bound": release is not None,
    }


def _helper_command(python: Path, nonce: str, helper: Path, expected: str, operation: str) -> list[str]:
    if not python.is_absolute() or not python.is_file():
        raise ValueError("Native verification requires an absolute fixed PICO interpreter")
    if not os.access(python, os.X_OK):
        raise PermissionError("Native consumer interpreter is not executable")
    limiter = shutil.which("prlimit")
    if limiter is None:
        raise ValueError("Native verification requires resource control")
    # Only standard-library bootstrap code runs before the registered stdin gate.
    entry = """import hashlib,json,sys
from pathlib import Path
payload = None
try:
    limit = int(sys.argv[4])
    raw = sys.stdin.buffer.read(limit + 1)
    if not raw or len(raw) > limit:
        raise ValueError('Recording verifier initialization is missing or oversized')
    payload = json.loads(raw)
    path = Path(sys.argv[1])
    code = path.read_bytes()
    if hashlib.sha256(code).hexdigest() != sys.argv[2]:
        raise ValueError(('Runtime' if sys.argv[3] == 'runtime' else 'Recording') + ' helper bytes changed')
    scope = {'__file__': str(path), '__name__': 'consumer_native_helper'}
    exec(compile(code, str(path), 'exec'), scope)
    if sys.argv[3] == 'runtime':
        print(json.dumps(scope['verify_runtime_contract'](payload['execution']['runtime_contract'])))
    elif sys.argv[3] == 'resources':
        print(json.dumps(scope['resolve_resources'](payload)))
    elif sys.argv[3] == 'inspect':
        print(json.dumps(scope['inspect_native'](payload)))
    else:
        raise SystemExit(scope['worker_main'](payload))
except Exception as error:
    print(json.dumps({'error': str(error)[:4000]}))
    raise SystemExit(1)
"""
    return [
        limiter,
        "--as=4294967296",
        "--nofile=128",
        "--cpu=180",
        "--",
        str(python),
        "-I",
        "-B",
        "-c",
        entry,
        str(helper),
        expected,
        operation,
        str(INPUT_LIMIT),
        nonce,
    ]


def _consumer_command(python: Path, nonce: str, payload: dict, operation: str) -> list[str]:
    """All operations carry the same nested immutable execution binding."""
    name, digest_key = {
        "runtime": ("runtime.py", "runtime_helper_sha256"),
        "resources": ("runtime.py", "runtime_helper_sha256"),
        "inspect": ("verify_recording.py", "helper_sha256"),
        "recording": ("verify_recording.py", "helper_sha256"),
    }[operation]
    return _helper_command(python, nonce, Path(__file__).with_name(name), payload["execution"][digest_key], operation)


def evidence_command(python: Path, nonce: str, payload: dict) -> list[str]:
    return _consumer_command(python, nonce, payload, "recording")


def runtime_command(python: Path, nonce: str, payload: dict) -> list[str]:
    return _consumer_command(python, nonce, payload, "runtime")


def resource_command(python: Path, nonce: str, payload: dict) -> list[str]:
    return _consumer_command(python, nonce, payload, "resources")


def inspection_command(python: Path, nonce: str, payload: dict) -> list[str]:
    return _consumer_command(python, nonce, payload, "inspect")


async def run_verification(payload: dict, python: Path, *, register_process) -> dict:
    content = canonical(payload)
    if len(content) > INPUT_LIMIT:
        raise ValueError("Recording verifier initialization exceeds its input budget")
    nonce = secrets.token_hex(24)
    spawning = asyncio.create_task(
        asyncio.create_subprocess_exec(
            *evidence_command(python, nonce, payload),
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
        interrupted = await settle_task(spawning)
        process = spawning.result()
        identity = {"pid": process.pid, "nonce": nonce}
        try:
            identity = process_identity(process.pid, nonce)
        finally:
            await wait_task(asyncio.create_task(register_process(identity)))
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
        if await run_thread(resource_state, identity) != "dead":
            raise ValueError("Recording verifier process group did not exit cleanly")
        return result
    finally:
        if process is not None:
            terminating = asyncio.create_task(asyncio.to_thread(terminate_owned, identity))
            interrupted = await settle_task(terminating)
            quiescent = terminating.result()
            if not quiescent:
                raise VerificationUnresolved("Recording verifier process group is unresolved")
            if interrupted:
                raise asyncio.CancelledError


def main():
    import argparse
    from contextlib import closing
    import signal

    from ..artifacts import ArtifactStore
    from ..orchestration.portal import load_simulation_config
    from .store import SimulationStore
    from .verification import ManagedRecordingVerification

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", required=True, help="Registered simulation session identity")
    parser.add_argument("--config", required=True, type=Path, help="Rendered authoritative portal configuration")
    args = parser.parse_args()
    simulation = load_simulation_config(args.config)
    if simulation is None:
        parser.error("The portal configuration does not enable simulation")
    dsn = os.environ.get("PLATFORM_OPERATOR_DSN")
    if not dsn:
        parser.error("PLATFORM_OPERATOR_DSN is required to read the registered verification identity")
    import psycopg2
    from ..platform.queries import read_run_projection

    store = SimulationStore(lambda: psycopg2.connect(dsn))
    artifacts = ArtifactStore.from_environment(os.environ)
    row = store.get(args.session)
    with closing(psycopg2.connect(dsn)) as connection:
        projection = read_run_projection(connection, row["source_run_id"], artifacts=artifacts)

    def read_member(name, member):
        ref = ArtifactRef.from_record(member["artifact"])
        return artifacts.read_metadata_member(ref, name, max_bytes=64 * 1024 * 1024)[0]

    verifier = ManagedRecordingVerification(store, artifacts, Path(simulation["root"]), Path(simulation["python"]))

    async def execute():
        loop = asyncio.get_running_loop()
        current = asyncio.current_task()
        loop.add_signal_handler(signal.SIGTERM, current.cancel)
        try:
            return await verifier.verify_recording(args.session, projection["members"], read_member)
        finally:
            loop.remove_signal_handler(signal.SIGTERM)

    try:
        result = asyncio.run(execute())
    except (asyncio.CancelledError, KeyboardInterrupt):
        raise SystemExit(130) from None
    print(json.dumps(result, sort_keys=True))
