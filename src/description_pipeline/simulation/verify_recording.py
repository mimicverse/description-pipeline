"""Independently recompute recorded kinematics with native MuJoCo, without Teleop IK."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import tempfile
import hashlib
import json
from pathlib import Path
import xml.etree.ElementTree as ET

import mujoco
import numpy as np

from ..artifacts import ArtifactStore
from ..io import digest
from ..pipeline.contracts import ArtifactRef
from .recording import materialize_source, recording_binding, run_verification
from .store import SimulationStore
from .schemas import TELEOP_RECORDING_SCHEMA, RECORDING_VERIFICATION_SCHEMA
from .warnings import verify_warning_inventory


def verify(recording: Path, source: Path, *, expected_request_digest: str) -> dict:
    request = json.loads((recording / "request.json").read_bytes())
    if digest(request) != expected_request_digest:
        raise ValueError("Recording request differs from the registered session")
    inventory, profile = request["source"]["files"], request["profile"]
    source = source.resolve()
    warning_identity = verify_warning_inventory(request["source"], source)
    for name, sha in inventory.items():
        path = (source / name).resolve()
        if not path.is_relative_to(source) or hashlib.sha256(path.read_bytes()).hexdigest() != sha:
            raise ValueError("Recorded source identity does not match the supplied files")
        if path.suffix not in {".xml", ".urdf"}:
            continue
        xml = ET.parse(path).getroot()
        if xml.find(".//plugin") is not None:
            raise ValueError("Recorded source may not load plugins")
        compiler = xml.find("compiler")
        for node in xml.iter():
            if "file" not in node.attrib:
                continue
            directory = (
                compiler.get("meshdir" if node.tag == "mesh" else "texturedir", compiler.get("assetdir", ""))
                if compiler is not None and node.tag != "include"
                else ""
            )
            target = (path.parent / directory / node.get("file")).resolve()
            if not target.is_relative_to(source) or target.relative_to(source).as_posix() not in inventory:
                raise ValueError("Recorded source references an unregistered resource")
    if mujoco.mj_versionString() != request["source"]["engine"]["mujoco"]:
        raise ValueError("Recording verification requires the original MuJoCo engine")
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
    previous, count = None, 0
    with (recording / "ticks.jsonl").open() as stream:
        for line in stream:
            result = json.loads(line)["result"]
            if result.get("schema_version") != "teleop.upper-body/v1" or result.get("mode") != "kinematic_ik":
                raise ValueError("Recording schema or mode is unsupported by this verifier")
            q = np.asarray(result["qpos"], dtype=float)
            dt = float(result["control"]["dt_s"])
            if q.shape != (model.nq,) or not np.isfinite(q).all() or not np.isfinite(dt) or dt < 0:
                raise ValueError("Recorded joint state or control interval is invalid")
            if result["control"]["tick"] != count:
                raise ValueError("Recording control ticks are not contiguous")
            if previous is not None:
                if not np.array_equal(q[fixed], previous[fixed]):
                    raise ValueError("A fixed joint changed in the recording")
                if any(abs(q[index] - previous[index]) > speed * min(dt, 0.1) + 1e-8 for index, speed in rates.items()):
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
            mujoco.mj_forward(model, data)
            for key in ("geom_xpos", "geom_xmat", "xpos", "xmat", "site_xpos", "site_xmat"):
                if not np.allclose(getattr(data, key).reshape(-1), result[key], atol=1e-8, rtol=0):
                    raise ValueError("Recorded render transforms differ from independent forward kinematics")
            basis = data.xmat[base].reshape(3, 3)
            for side, arm in profile["arms"].items():
                body = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_BODY, arm["frame"])
                if body < 0:
                    raise ValueError("The recorded TCP frame is absent")
                rotation = data.xmat[body].reshape(3, 3)
                position = basis.T @ (data.xpos[body] + rotation @ arm["tip_offset"] - data.xpos[base])
                pose = result["report"]["arms"][side]["applied_pose"]
                recorded_rotation = np.empty(9)
                mujoco.mju_quat2Mat(recorded_rotation, np.asarray(pose["quaternion_wxyz"], dtype=float))
                if not (
                    np.allclose(position, pose["position_m"], atol=1e-7, rtol=0)
                    and np.allclose(basis.T @ rotation, recorded_rotation.reshape(3, 3), atol=1e-7, rtol=0)
                ):
                    raise ValueError("Recorded applied TCP differs from independent forward kinematics")
            previous, count = q.copy(), count + 1
    if count == 0:
        raise ValueError("The recording contains no complete control ticks")
    return {
        "ticks": count,
        "engine": mujoco.mj_versionString(),
        "scope": "Source identity, joint limits/rates, fixed joints, render transforms and applied arm TCP FK",
        "warning_inventory": warning_identity,
    }


def verify_bound(binding: dict, artifacts: ArtifactStore, source: Path) -> dict:
    if binding.get("schema_version") != RECORDING_VERIFICATION_SCHEMA:
        raise ValueError("Recording verification binding is unsupported")
    artifact = ArtifactRef.from_record(binding["recording_artifact"])
    if artifact.kind != "teleop_recording" or artifact.producer_tool_sha256 != binding["runtime_digest"]:
        raise ValueError("Recording artifact producer differs from the registered session")
    artifacts.verify(artifact)
    root = artifacts.objects / artifact.artifact_id
    identity = json.loads((root / "session.json").read_bytes())
    expected_identity = {key: value for key, value in binding.items() if key != "recording_artifact"}
    expected_identity["schema_version"] = TELEOP_RECORDING_SCHEMA
    if identity != expected_identity:
        raise ValueError("Recording session identity differs from the registered session")
    result = verify(root, source, expected_request_digest=binding["request_digest"])
    request = json.loads((root / "request.json").read_bytes())
    if digest(request["runtime"]) != binding["runtime_digest"]:
        raise ValueError("Recording runtime differs from the registered session")
    outcome = json.loads((root / "outcome.json").read_bytes())
    if type(outcome.get("complete")) is not bool or set(outcome) != {"complete", "error"}:
        raise ValueError("Recording completion metadata is invalid")
    result.update(
        binding=binding,
        recording_complete=outcome["complete"],
        recording_error=outcome["error"],
        mode="kinematic_ik",
        control_replay_verified=False,
    )
    return result


def worker_main():
    try:
        raw = sys.stdin.buffer.readline(128 * 1024 + 1)
        if not raw or len(raw) > 128 * 1024:
            raise ValueError("Recording verifier initialization is missing or oversized")
        payload = json.loads(raw)
        if set(payload) != {"binding", "artifact_root", "source"}:
            raise ValueError("Recording verifier initialization is invalid")
        result = verify_bound(
            payload["binding"], ArtifactStore(Path(payload["artifact_root"])), Path(payload["source"])
        )
        print(json.dumps(result, allow_nan=False))
        return 0
    except Exception as error:
        print(json.dumps({"error": str(error)[:4000]}, allow_nan=False))
        return 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", required=True, help="Registered simulation session identity")
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
    with psycopg2.connect(dsn) as connection:
        projection = read_run_projection(connection, row["source_run_id"], artifacts=artifacts)

    def read_member(name, member):
        ref = ArtifactRef.from_record(member["artifact"])
        return artifacts.read_metadata_member(ref, name, max_bytes=64 * 1024 * 1024)[0]

    with tempfile.TemporaryDirectory(prefix="teleop-recording-verification-") as temporary:
        source = materialize_source(
            Path(temporary) / "source", row["request"]["source"], projection["members"], read_member
        )
        print(json.dumps(asyncio.run(run_verification(binding, artifacts.root, source))))


if __name__ == "__main__":
    main()
