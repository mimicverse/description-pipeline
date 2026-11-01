"""Verify canonical worker/1 evidence in its original fixed PICO environment."""

from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import xml.etree.ElementTree as ET

INPUT_LIMIT = 4 * 1024 * 1024


def prepare_bound(binding: dict, artifacts, source: Path, *, commands: list, execution: dict) -> dict:
    """Check platform facts before handing non-secret immutable data to native code."""
    from ..io import digest
    from ..pipeline.contracts import ArtifactRef
    from .runtime import TELEOP_RECORDING_SCHEMA, RECORDING_VERIFICATION_SCHEMA
    from .warnings import verify_warning_inventory

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
    if set(outcome) != {"complete", "error"} or type(outcome["complete"]) is not bool:
        raise ValueError("Recording completion metadata is invalid")
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


def _independent_fk(recording: Path, source: Path, request: dict, manifest: dict) -> int:
    """Recompute model limits, rates, display transforms and TCPs without Teleop IK."""
    import mujoco
    import numpy as np

    profile = request["profile"]
    model = mujoco.MjModel.from_xml_path(str(source / request["source"]["entry"]))
    data = mujoco.MjData(model)
    urdf = ET.parse(source / profile["urdf"]).getroot()
    children = {node.find("child").get("link") for node in urdf.findall("joint")}
    roots = [node.get("name") for node in urdf.findall("link") if node.get("name") not in children]
    if len(roots) != 1:
        raise ValueError("The recorded source must have one base frame")
    base = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_BODY, roots[0])
    if base < 0:
        raise ValueError("The compiled source has no recorded base frame")
    controlled = {name for arm in profile["arms"].values() for name in arm["joints"]} | set(profile["head_joints"])
    rates = {}
    for name in controlled:
        index = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_JOINT, name)
        if index < 0 or int(model.jnt_type[index]) != int(mujoco.mjtJoint.mjJNT_HINGE):
            raise ValueError("Recorded controlled joints must be revolute")
        rates[int(model.jnt_qposadr[index])] = (
            request["settings"].get("head_speed_radps", 4.0)
            if name in profile["head_joints"]
            else request["settings"]["joint_speed_radps"]
        )
    fixed = sorted(set(range(model.nq)) - rates.keys())
    previous, count, previous_tick = np.asarray(manifest["initial_state"]["qpos"], dtype=float), 0, -1
    with (recording / "records.jsonl").open() as stream:
        for line in stream:
            row = json.loads(line)
            extension = row["extension"]["pico_session"]
            state = extension["state"]
            q = np.asarray(state["qpos"], dtype=float)
            dt = float(row["usage"]["control_interval_s"])
            if q.shape != (model.nq,) or not np.isfinite(q).all() or not np.isfinite(dt) or dt <= 0:
                raise ValueError("Recorded joint state or control interval is invalid")
            tick = state["tick"]
            if type(tick) is not int or tick <= previous_tick:
                raise ValueError("Recording control ticks are not strictly increasing")
            if not np.array_equal(q[fixed], previous[fixed]):
                raise ValueError("A fixed joint changed in the recording")
            interval = (tick - previous_tick) * min(dt, 0.1)
            if any(abs(q[index] - previous[index]) > speed * interval + 1e-8 for index, speed in rates.items()):
                raise ValueError("Recorded motion exceeds its joint-rate budget")
            for index in range(model.njnt):
                if model.jnt_limited[index] and int(model.jnt_type[index]) in {
                    int(mujoco.mjtJoint.mjJNT_HINGE), int(mujoco.mjtJoint.mjJNT_SLIDE)
                }:
                    value = q[model.jnt_qposadr[index]]
                    if not model.jnt_range[index, 0] - 1e-8 <= value <= model.jnt_range[index, 1] + 1e-8:
                        raise ValueError("Recorded joint position exceeds its source limits")
            data.qpos[:] = q
            data.qvel[:] = np.asarray(state["qvel"], dtype=float)
            mujoco.mj_forward(model, data)
            display = {
                key: getattr(data, key).reshape(-1).tolist()
                for key in ("geom_xpos", "geom_xmat", "xpos", "xmat", "site_xpos", "site_xmat")
            }
            display["ncon"] = int(data.ncon)
            display_sha = hashlib.sha256(json.dumps(display, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            if display_sha != extension["display_digest"]:
                raise ValueError("Recorded display differs from independent forward kinematics")
            basis = data.xmat[base].reshape(3, 3)
            for side, arm in profile["arms"].items():
                body = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_BODY, arm["frame"])
                if body < 0:
                    raise ValueError("The recorded TCP frame is absent")
                rotation = data.xmat[body].reshape(3, 3)
                position = basis.T @ (data.xpos[body] + rotation @ arm["tip_offset"] - data.xpos[base])
                pose = state["report"]["arms"][side]["applied_pose"]
                recorded_rotation = np.empty(9)
                mujoco.mju_quat2Mat(recorded_rotation, np.asarray(pose["quaternion_wxyz"], dtype=float))
                if not (
                    np.allclose(position, pose["position_m"], atol=1e-7, rtol=0)
                    and np.allclose(basis.T @ rotation, recorded_rotation.reshape(3, 3), atol=1e-7, rtol=0)
                ):
                    raise ValueError("Recorded applied TCP differs from independent forward kinematics")
            previous, count, previous_tick = q.copy(), count + 1, tick
    return count


def verify_native(payload: dict) -> dict:
    """One native core for both API and CLI; no platform package or DB is loaded."""
    execution = payload["execution"]
    runtime_path = Path(__file__).with_name("runtime.py")
    runtime_bytes = runtime_path.read_bytes()
    if hashlib.sha256(runtime_bytes).hexdigest() != execution["runtime_helper_sha256"]:
        raise ValueError("Runtime helper differs from its registered release bytes")
    namespace = {"__file__": str(runtime_path), "__name__": "recording_runtime_helper"}
    exec(compile(runtime_bytes, str(runtime_path), "exec"), namespace)
    runtime = namespace["verify_runtime_contract"](execution["runtime_contract"])
    request = payload["request"]
    if runtime != request["runtime"]:
        raise ValueError("Recording verification requires its original fixed runtime")
    if runtime["engine"] != request["source"]["engine"]["mujoco"]:
        raise ValueError("Recording verification requires its original MuJoCo engine")
    recording, source = Path(payload["recording"]), Path(payload["source"])
    before = payload["inventory"]

    def check_inventory():
        actual = {}
        for path in recording.rglob("*"):
            if path.is_symlink():
                raise ValueError("Recording evidence must not contain symbolic links")
            if path.is_file():
                actual[path.relative_to(recording).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != before:
            raise ValueError("Recording evidence differs from its sealed artifact")

    check_inventory()
    native = recording / "native"
    manifest = json.loads((native / "session-manifest.json").read_bytes())
    init = manifest["init"]
    if manifest["session_id"] != payload["binding"]["session_id"] or init["session_id"] != manifest["session_id"]:
        raise ValueError("Native recording belongs to another registered session")
    for key, value in {
        "protocol": "teleop.upper-body.worker/1", "mode": "kinematic_ik", "profile": request["profile"],
        "inventory": request["source"]["files"], "entry": request["source"]["entry"], **request["settings"]
    }.items():
        if init.get(key) != value:
            raise ValueError(f"Native recording initialization differs from its registered request: {key}")
    if manifest["source"]["inventory"] != request["source"]["files"] or manifest["source"]["entry"] != request["source"]["entry"]:
        raise ValueError("Native source differs from its registered artifact")
    from teleop.backends.mujoco.upper_body import validate_source

    validate_source(source, request["source"]["files"], request["source"]["entry"], exact_inventory=True)
    raw = {}
    for line in (native / "raw-input.jsonl").read_text().splitlines():
        command = json.loads(line).get("raw")
        if isinstance(command, dict) and isinstance(command.get("id"), str):
            raw.setdefault(command["id"], []).append(command)
    expected = []
    for command in payload["commands"]:
        result = command["result"]
        identifier = "operator-" + hashlib.sha256(command["command_id"].encode()).hexdigest()
        if result is None:
            raise ValueError("A registered command has no dispatch reconciliation")
        if result["state"] == "not_sent":
            if identifier in raw:
                raise ValueError("A command marked not sent has native dispatch evidence")
            continue
        envelope = result["envelope"]
        if envelope["id"] != identifier or not raw.get(identifier) or any(item != envelope for item in raw[identifier]):
            raise ValueError("Native dispatch differs from its immutable registered envelope")
        expected.append(identifier)

    from teleop.robots.m3_0.session_evidence import reconcile_session_evidence, replay_session_evidence

    reconciliation = reconcile_session_evidence(native, expected_command_ids=tuple(expected))
    if not reconciliation["complete"]:
        raise ValueError("Native command outcomes remain unknown or malformed")
    reconciled = {item["command_id"]: item for item in reconciliation["commands"]}
    terminal_events = {}
    with (native / "session-events.jsonl").open() as stream:
        for line in stream:
            event = json.loads(line)
            if event.get("result") in {"applied", "rejected"}:
                terminal_events.setdefault(event["command_id"], []).append(event)
    for command in payload["commands"]:
        result = command["result"]
        if result["state"] not in {"applied", "rejected"}:
            continue
        actual = reconciled[result["envelope"]["id"]]
        response = result["response"]
        if actual["status"] != result["state"] or any(
            actual.get(key) != response.get(key) for key in ("effective_tick", "effective_time_s", "reason")
        ):
            raise ValueError("Registered command receipt differs from its native terminal event")
        if result["state"] == "applied" and any(
            event.get("state_digest") != response.get("state_digest")
            for event in terminal_events.get(result["envelope"]["id"], [])
        ):
            raise ValueError("Registered command state digest differs from its native terminal event")
    # The extra FK check is independent of the IK and deterministic replay implementation.
    ticks = _independent_fk(native, source, request, manifest)
    replay = replay_session_evidence(native, expected_command_ids=tuple(expected), source_root=source)
    if replay.get("pass") is not True:
        raise ValueError("Native control replay failed: " + json.dumps(replay.get("failures", []))[:3500])
    if payload["outcome"]["complete"] and (manifest["status"] != "stopped" or manifest["exit_code"] != 0):
        raise ValueError("Recording completion differs from native worker termination")
    check_inventory()
    return {
        "binding": payload["binding"], "ticks": ticks, "engine": runtime["engine"], "mode": "kinematic_ik",
        "recording_complete": payload["outcome"]["complete"], "recording_error": payload["outcome"]["error"],
        "control_replay_verified": True, "warning_inventory": payload["warning_inventory"],
        "execution": {key: value for key, value in execution.items() if key != "runtime_contract"},
        "verifier_pid": os.getpid(),
        "replay": {key: replay[key] for key in ("schema_version", "records_checked", "ticks_replayed", "source_root_overridden")},
        "scope": "Original runtime/source, command receipts, canonical control replay, limits/rates, fixed joints, display and TCP FK",
    }


def worker_main(payload: dict):
    try:
        result = verify_native(payload)
        status = 0
    except Exception as error:
        result, status = {"error": str(error)[:4000]}, 1
    # Only the CLI explicitly transfers its private directory to this same PID.
    if payload.get("cleanup") is not None:
        try:
            cleanup = Path(payload["cleanup"])
            if cleanup.is_symlink() or Path(payload["source"]).parent != cleanup:
                raise ValueError("Recording verifier cleanup handoff is invalid")
            shutil.rmtree(cleanup)
        except Exception as error:
            result, status = {"error": f"Recording verifier cleanup failed: {error}"[:4000]}, 1
    print(json.dumps(result, allow_nan=False))
    return status


def main():
    from ..artifacts import ArtifactStore
    from ..io import canonical
    from ..pipeline.contracts import ArtifactRef
    from .recording import evidence_command, materialize_source, recording_binding, verification_execution
    from .store import SimulationStore

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", required=True, help="Registered simulation session identity")
    parser.add_argument("--python", required=True, type=Path, help="Absolute fixed PICO interpreter")
    args = parser.parse_args()
    dsn = os.environ.get("PLATFORM_OPERATOR_DSN")
    if not dsn:
        parser.error("PLATFORM_OPERATOR_DSN is required to read the registered verification identity")
    import psycopg2
    from ..platform.queries import read_run_projection

    store = SimulationStore(lambda: psycopg2.connect(dsn))
    artifacts = ArtifactStore.from_environment(os.environ)
    row = store.get(args.session)
    binding = recording_binding(row)
    with closing(psycopg2.connect(dsn)) as connection:
        projection = read_run_projection(connection, row["source_run_id"], artifacts=artifacts)

    def read_member(name, member):
        ref = ArtifactRef.from_record(member["artifact"])
        return artifacts.read_metadata_member(ref, name, max_bytes=64 * 1024 * 1024)[0]

    temporary = Path(tempfile.mkdtemp(prefix="teleop-recording-verification-"))
    try:
        source = materialize_source(temporary / "source", row["request"]["source"], projection["members"], read_member)
        payload = prepare_bound(
            binding, artifacts, source, commands=store.worker_commands(args.session), execution=verification_execution()
        )
        payload["cleanup"] = str(temporary)
        content = canonical(payload)
        if len(content) > INPUT_LIMIT:
            raise ValueError("Recording verifier initialization exceeds its input budget")
        with tempfile.TemporaryFile(dir=temporary) as stream:
            stream.write(content)
            stream.flush()
            stream.seek(0)
            os.dup2(stream.fileno(), 0)
            argv = evidence_command(args.python, os.urandom(24).hex(), payload["execution"])
            # exec keeps one PID/group and gives the helper cleanup ownership on success.
            os.execve(argv[0], argv, {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1"})
    finally:
        # Runs only when materialization, preparation or exec fails.
        shutil.rmtree(temporary)


if __name__ == "__main__":
    main()
