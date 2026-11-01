"""Verify canonical worker/1 evidence in its original fixed PICO environment."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import xml.etree.ElementTree as ET
import zlib


STREAM_NAMES = {"raw": "raw-input.jsonl.gz", "events": "session-events.jsonl.gz", "records": "records.jsonl.gz"}


def inspect_containers(native: Path, manifest: dict, *, streams: dict | None = None) -> dict:
    """Validate only gzip containers and exact compressed identities; never decode business rows."""
    files = manifest["files"]
    if not isinstance(files, dict) or files.keys() != STREAM_NAMES.keys():
        raise ValueError("Native manifest does not declare exactly three evidence streams")
    if {path.name for path in native.iterdir()} != {*STREAM_NAMES.values(), "session-manifest.json"}:
        raise ValueError("Native recording contains unexpected evidence files")
    if streams is None:
        streams = {}
    for key, name in STREAM_NAMES.items():
        expected, path = files[key], native / name
        if (
            not isinstance(expected, dict)
            or set(expected) != {"name", "encoding", "compressed_bytes", "sha256"}
            or expected["name"] != name
            or expected["encoding"] != "gzip-jsonl"
            or type(expected["compressed_bytes"]) is not int
            or expected["compressed_bytes"] < 0
            or not isinstance(expected["sha256"], str)
            or re.fullmatch(r"[0-9a-f]{64}", expected["sha256"]) is None
            or path.is_symlink()
            or not path.is_file()
        ):
            raise ValueError(f"Native stream identity is invalid: {key}")
        before = path.stat()
        with path.open("rb") as handle:
            if handle.read(3) != b"\x1f\x8b\x08":
                raise ValueError(f"Native gzip container has no complete header: {key}")
            handle.seek(0)
            observed_sha = hashlib.file_digest(handle, "sha256").hexdigest()
        if before.st_size != expected["compressed_bytes"] or observed_sha != expected["sha256"]:
            raise ValueError(f"Native compressed stream differs from its manifest: {key}")
        decompressed = 0
        try:
            with gzip.open(path, "rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    decompressed += len(chunk)
        except (EOFError, OSError, zlib.error) as error:
            raise ValueError(f"Native gzip container is incomplete: {key}: {type(error).__name__}") from error
        after = path.stat()
        if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
            raise ValueError(f"Native evidence changed during container verification: {key}")
        streams[key] = {
            **expected,
            "observed_compressed_bytes": before.st_size,
            "observed_sha256": observed_sha,
            "decompressed_bytes": decompressed,
            "container_complete": True,
        }
    return {"container_complete": True, "streams": streams}


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
    for row in _record_stream(recording, manifest):
        extension = row["extension"]["pico_session"]
        state = extension["state"]
        q = np.asarray(state["qpos"], dtype=float)
        dt = float(row["usage"]["control_interval_s"])
        if q.shape != (model.nq,) or not np.isfinite(q).all() or not np.isfinite(dt) or dt <= 0:
            raise ValueError("Recorded joint state or control interval is invalid")
        tick = state["tick"]
        if not np.array_equal(q[fixed], previous[fixed]):
            raise ValueError("A fixed joint changed in the recording")
        interval = (tick - previous_tick) * min(dt, 0.1)
        if any(abs(q[index] - previous[index]) > speed * interval + 1e-8 for index, speed in rates.items()):
            raise ValueError("Recorded motion exceeds its joint-rate budget")
        for index in range(model.njnt):
            if model.jnt_limited[index] and int(model.jnt_type[index]) in {
                int(mujoco.mjtJoint.mjJNT_HINGE),
                int(mujoco.mjtJoint.mjJNT_SLIDE),
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


def _record_stream(recording: Path, manifest: dict):
    """Use the public decoder and independently require every actual control tick."""
    from teleop.robots.m3_0.session_evidence import iter_record_evidence

    count = 0
    for row in iter_record_evidence(recording):
        tick = row["extension"]["pico_session"]["state"]["tick"]
        if type(tick) is not int or tick != count:
            raise ValueError("Recording control ticks are not contiguous from tick zero")
        count += 1
        if count > manifest["resource_policy"]["record_row_cap"]:
            raise ValueError("Recording exceeds its frozen record row cap")
        yield row
    continuity = manifest["record_continuity"]
    if (
        count == 0
        or not isinstance(continuity, dict)
        or set(continuity) != {"first_tick", "last_tick", "record_count", "contiguous"}
        or any(type(continuity[key]) is not int for key in ("first_tick", "last_tick", "record_count"))
        or continuity != {"first_tick": 0, "last_tick": count - 1, "record_count": count, "contiguous": True}
        or continuity["contiguous"] is not True
        or type(manifest["counts"]["records"]) is not int
        or manifest["counts"]["records"] != count
    ):
        raise ValueError("Recorded control ticks differ from the manifest continuity")


def _verify_commands(native: Path, commands: list[dict]) -> tuple[tuple[str, ...], dict]:
    """Close operator facts in both directions while streaming unregistered input."""
    from teleop.robots.m3_0.session_evidence import (
        iter_raw_input_evidence,
        iter_session_event_evidence,
        reconcile_session_evidence,
    )

    registered, expected, seen, raw_count = {}, {}, set(), 0
    for command in commands:
        result = command["result"]
        if result is None:
            raise ValueError("A registered command has no dispatch reconciliation")
        identifier = "operator-" + hashlib.sha256(command["command_id"].encode()).hexdigest()
        if identifier in registered:
            raise ValueError("A registered command identity is duplicated")
        registered[identifier] = result
        if result["state"] != "not_sent":
            if result["envelope"]["id"] != identifier:
                raise ValueError("Native dispatch differs from its immutable registered envelope")
            expected[identifier] = result
    for row in iter_raw_input_evidence(native):
        raw_count += 1
        envelope = row.get("raw")
        if not isinstance(envelope, dict):
            continue
        identifier = envelope.get("id")
        if isinstance(identifier, str) and identifier.startswith("operator-") and identifier not in registered:
            raise ValueError("Native operator command has no registered platform fact")
        if not isinstance(identifier, str) or identifier not in registered:
            continue
        result = registered[identifier]
        if result["state"] == "not_sent":
            raise ValueError("A command marked not sent has native dispatch evidence")
        if envelope != result["envelope"]:
            raise ValueError("Native dispatch differs from its immutable registered envelope")
        seen.add(identifier)
    if seen != expected.keys():
        raise ValueError("Native dispatch differs from its immutable registered envelope")
    event_seen, event_count = set(), 0
    for row in iter_session_event_evidence(native):
        event_count += 1
        identifier = row.get("command_id")
        if isinstance(identifier, str) and identifier.startswith("operator-") and identifier not in registered:
            raise ValueError("Native operator event has no registered platform fact")
        if not isinstance(identifier, str) or identifier not in registered:
            continue
        if registered[identifier]["state"] == "not_sent":
            raise ValueError("A command marked not sent has native event evidence")
        event_seen.add(identifier)
    if event_seen != expected.keys():
        raise ValueError("Native events differ from the registered command set")
    identifiers = tuple(expected)
    reconciliation = reconcile_session_evidence(native, expected_command_ids=identifiers)
    if (
        reconciliation["schema_version"] != "teleop-pico-session-reconciliation/1"
        or reconciliation["complete"] is not True
        or reconciliation["unknown"]
        or reconciliation["malformed"] != 0
        or reconciliation["incomplete"]
    ):
        raise ValueError("Native command outcomes remain unknown or malformed")
    reconciled = {item["command_id"]: item for item in reconciliation["commands"]}
    if reconciled.keys() != expected.keys() or len(reconciled) != len(reconciliation["commands"]):
        raise ValueError("Native command reconciliation differs from the registered command set")
    for identifier, result in expected.items():
        if result["state"] not in {"applied", "rejected"}:
            continue
        actual, response = reconciled[identifier], result["response"]
        if actual["status"] != result["state"] or any(
            actual.get(key) != response.get(key) for key in ("effective_tick", "effective_time_s", "reason")
        ):
            raise ValueError("Registered command receipt differs from its native terminal event")
        if result["state"] == "applied" and actual["state_digest"] != response["state_digest"]:
            raise ValueError("Registered command state digest differs from its native terminal event")
    counts = reconciliation["counts"]
    if (
        set(counts) != {"raw_input", "session_events", "commands", "unknown", "malformed"}
        or any(type(value) is not int or value < 0 for value in counts.values())
        or counts["raw_input"] != raw_count
        or counts["session_events"] != event_count
        or counts["unknown"] != 0
        or counts["malformed"] != 0
    ):
        raise ValueError("Native reconciliation counts differ from the public input stream")
    return identifiers, counts


def _verified_runtime(execution: dict) -> dict:
    runtime_path = Path(__file__).with_name("runtime.py")
    runtime_bytes = runtime_path.read_bytes()
    if hashlib.sha256(runtime_bytes).hexdigest() != execution["runtime_helper_sha256"]:
        raise ValueError("Runtime helper differs from its registered release bytes")
    namespace = {"__file__": str(runtime_path), "__name__": "recording_runtime_helper"}
    exec(compile(runtime_bytes, str(runtime_path), "exec"), namespace)
    return namespace["verify_runtime_contract"](execution["runtime_contract"])


def inspect_native(payload: dict) -> dict:
    """Inspect containers and public business streams before seal; never qualify controller or FK."""
    runtime = _verified_runtime(payload["execution"])
    if runtime != payload["runtime"]:
        raise ValueError("Evidence inspection requires its original fixed runtime")
    native = Path(payload["native"])
    report = {
        "runtime": runtime,
        "manifest_sha256": payload["manifest_sha256"],
        "manifest": None,
        "container_complete": False,
        "business_complete": False,
        "finalization_verification": None,
        "counts": None,
        "streams": {key: {"name": name, "container_complete": False} for key, name in STREAM_NAMES.items()},
        "error": None,
        "error_type": None,
    }
    try:
        manifest_path = native / "session-manifest.json"
        manifest_bytes = manifest_path.read_bytes()
        if hashlib.sha256(manifest_bytes).hexdigest() != payload["manifest_sha256"]:
            raise ValueError("Native manifest changed before business-stream inspection")
        manifest = json.loads(manifest_bytes)
        if "commands" in manifest:
            raise ValueError("The native manifest must not embed a command ledger")
        if manifest["session_id"] != payload["session_id"] or manifest["init"]["session_id"] != payload["session_id"]:
            raise ValueError("Native evidence belongs to another registered session")
        report["manifest"] = {
            key: manifest[key]
            for key in (
                "schema_version",
                "protocol",
                "session_id",
                "mode",
                "counts",
                "phase_timeline",
                "timing_contract",
                "run_window",
                "record_continuity",
                "files",
                "resource_policy",
                "finalization",
                "status",
                "reason",
                "exit_code",
            )
        }
        inspect_containers(native, manifest, streams=report["streams"])
        report["container_complete"] = True
        _identifiers, counts = _verify_commands(native, payload["commands"])
        records = sum(1 for _row in _record_stream(native, manifest))
        if any(manifest["counts"][key] != counts[key] for key in ("raw_input", "session_events")):
            raise ValueError("Native manifest counts differ from public business streams")
        with manifest_path.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != payload["manifest_sha256"]:
                raise ValueError("Native manifest changed during business-stream inspection")
        report["counts"] = {**counts, "records": records}
        report["business_complete"] = True
        from teleop.robots.m3_0.session_evidence import verify_session_finalization

        report["finalization_verification"] = verify_session_finalization(native)
    except Exception as error:
        report["error"], report["error_type"] = str(error)[:4000], type(error).__name__
    return report


def verify_native(payload: dict) -> dict:
    """One native core for both API and CLI; no platform package or DB is loaded."""
    execution = payload["execution"]
    runtime = _verified_runtime(execution)
    if any(payload["outcome"].get(key) is not True for key in ("complete", "container_complete", "session_complete")):
        raise ValueError("Diagnostic recording is not a complete session verification input")
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
                with path.open("rb") as stream:
                    actual[path.relative_to(recording).as_posix()] = hashlib.file_digest(stream, "sha256").hexdigest()
        if actual != before:
            raise ValueError("Recording evidence differs from its sealed artifact")

    check_inventory()
    native = recording / "native"
    if before.get("native/session-manifest.json") != payload["outcome"]["manifest_sha256"]:
        raise ValueError("Platform outcome differs from its committed native manifest")
    manifest = json.loads((native / "session-manifest.json").read_bytes())
    if "commands" in manifest:
        raise ValueError("The native manifest must not embed a command ledger")
    init = manifest["init"]
    if manifest["session_id"] != payload["binding"]["session_id"] or init["session_id"] != manifest["session_id"]:
        raise ValueError("Native recording belongs to another registered session")
    for key, value in {
        "protocol": "teleop.upper-body.worker/1",
        "mode": "kinematic_ik",
        "profile": request["profile"],
        "inventory": request["source"]["files"],
        "entry": request["source"]["entry"],
        **request["settings"],
    }.items():
        if init.get(key) != value:
            raise ValueError(f"Native recording initialization differs from its registered request: {key}")
    if (
        manifest["source"]["inventory"] != request["source"]["files"]
        or manifest["source"]["entry"] != request["source"]["entry"]
    ):
        raise ValueError("Native source differs from its registered artifact")
    from teleop.backends.mujoco.upper_body import validate_source

    validate_source(source, request["source"]["files"], request["source"]["entry"], exact_inventory=True)
    expected, counts = _verify_commands(native, payload["commands"])
    from teleop.robots.m3_0.session_evidence import replay_session_evidence

    if any(manifest["counts"][key] != counts[key] for key in ("raw_input", "session_events")):
        raise ValueError("Native manifest counts differ from public business streams")
    # The extra FK check is independent of the IK and deterministic replay implementation.
    ticks = _independent_fk(native, source, request, manifest)
    replay = replay_session_evidence(native, expected_command_ids=tuple(expected), source_root=source)
    if replay.get("pass") is not True:
        raise ValueError("Native control replay failed: " + json.dumps(replay.get("failures", []))[:3500])
    if replay["records_checked"] != ticks or replay["expected_records"] != ticks:
        raise ValueError("Native replay and independent FK did not verify every recorded tick")
    if payload["outcome"]["complete"] and (manifest["status"] != "stopped" or manifest["exit_code"] != 0):
        raise ValueError("Recording completion differs from native worker termination")
    check_inventory()
    window = manifest["run_window"]
    duration_eligible = (
        window["minimum_duration_met"] is True
        and type(window["elapsed_monotonic_ns"]) is int
        and window["elapsed_monotonic_ns"] >= 3600 * 1_000_000_000
        and window["minimum_duration_s"] >= 3600
    )
    formal_policy_fields = {
        "session_dir",
        "quota_bytes",
        "minimum_duration_s",
        "raw_rows_per_second",
        "event_rows_per_second",
        "traffic_headroom",
        "raw_row_cap",
        "event_row_cap",
        "record_row_cap",
        "setup_tick_cap",
        "max_row_bytes",
        "finalization_reserve_bytes",
        "failure_reserve_bytes",
        "setup_reserve_bytes",
        "compiled_bytes",
        "stderr_request_session_bytes",
        "declared_source_bytes",
    }
    return {
        "binding": payload["binding"],
        "ticks": ticks,
        "engine": runtime["engine"],
        "mode": "kinematic_ik",
        "recording_complete": payload["outcome"]["complete"],
        "recording_error": payload["outcome"]["error"],
        "control_replay_verified": True,
        "independent_fk": {
            "schema_version": "teleop-pico-session-fk-verification/1",
            "pass": True,
            "records_checked": ticks,
            "failures": [],
        },
        "duration_eligible": duration_eligible,
        "formal_one_hour_qualified": (
            duration_eligible
            and isinstance(init.get("resource_policy"), dict)
            and formal_policy_fields <= init["resource_policy"].keys()
        ),
        "warning_inventory": payload["warning_inventory"],
        "execution": {key: value for key, value in execution.items() if key != "runtime_contract"},
        "verifier_pid": os.getpid(),
        "replay": {
            key: replay[key]
            for key in (
                "schema_version",
                "records_checked",
                "expected_records",
                "ticks_replayed",
                "source_root_overridden",
                "pass",
                "failures",
            )
        }
        | {
            "reconciliation": {
                key: replay["reconciliation"][key]
                for key in (
                    "schema_version",
                    "complete",
                    "counts",
                    "unknown",
                    "malformed",
                    "incomplete",
                )
            }
        },
        "scope": (
            "Original runtime/source, command receipts, canonical control replay, "
            "limits/rates, fixed joints, display and TCP FK"
        ),
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
    from .recording import main as recording_main

    recording_main()


if __name__ == "__main__":
    main()
