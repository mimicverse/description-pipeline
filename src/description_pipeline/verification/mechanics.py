"""Replay kinematics against a reference explicitly selected by the consumer.

The candidate never supplies its own authority. A caller must select a validation
reference outside the workspace; its provenance and design approval are the
caller's responsibility. Execution attests agreement with that exact reference,
not measured physical accuracy, training readiness or hardware safety.
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import platform
import shutil
import tempfile
from datetime import UTC, datetime
from xml.etree import ElementTree as ET

import numpy as np

from ..io import PipelineError, confined, digest, file_digest, read_data, write_json
from . import _apply_pose, _poses
from .urdf_quality import model as parser

SCHEMA = "description.mechanical-reference/v1"
ATTESTATION = {"kind": "mechanical_reference_replay", "schema_version": "description.mechanical-replay/v1"}
REFERENCE_LIMIT = 32 * 1024 * 1024
TELEMETRY_LIMIT = 128 * 1024 * 1024
POSE_LIMIT = 512
OBSERVATION_LIMIT = 100000
CONVENTIONS = {"units": "SI", "joint_origin": "parent_link", "joint_axis": "joint", "poses": "world"}
QUALIFICATION = {
    "physical_calibration": False,
    "training_qualified": False,
    "hardware_qualified": False,
    "release_qualified": False,
}


def reference_path(path: Path, root: Path) -> Path:
    try:
        chosen = Path(path).resolve(strict=True)
    except OSError as error:
        raise PipelineError(f"Cannot read the selected mechanical reference: {path}") from error
    boundaries = [
        parent
        for parent in (Path(root).resolve(), *Path(root).resolve().parents)
        if parent == Path(root).resolve() or (parent / "config/robot.yaml").is_file()
    ]
    if any(chosen.is_relative_to(parent) for parent in boundaries) or not chosen.is_file():
        raise PipelineError("Select the approved mechanical reference outside the model workspace")
    if chosen.stat().st_size > REFERENCE_LIMIT:
        raise PipelineError("Mechanical reference exceeds the 32 MiB limit")
    return chosen


def _pairs(pairs):
    value = {}
    for key, item in pairs:
        _require(key not in value, f"Duplicate mechanical JSON key: {key}")
        value[key] = item
    return value


def _reject_constant(value):
    raise PipelineError(f"Non-finite mechanical JSON value: {value}")


def _finite_float(value):
    number = float(value)
    if not math.isfinite(number):
        _reject_constant(value)
    return number


def _read_json(path: Path, *, limit=REFERENCE_LIMIT):
    _require(path.stat().st_size <= limit, f"Mechanical JSON exceeds the {limit} byte limit")
    return json.loads(
        path.read_text(encoding="utf-8"),
        object_pairs_hook=_pairs,
        parse_constant=_reject_constant,
        parse_float=_finite_float,
    )


def _require(value, message):
    if not value:
        raise PipelineError(message)


def _fields(value, names, label):
    _require(isinstance(value, dict) and set(value) == set(names), f"{label} requires exactly {', '.join(names)}")


def _number(value, label):
    _require(type(value) in (float, int) and math.isfinite(value), f"{label} must be finite")
    return float(value)


def _matrix(value, label):
    _require(isinstance(value, list) and len(value) == 4, f"{label} must be a 4x4 SI transform")
    for row in value:
        _require(isinstance(row, list) and len(row) == 4, f"{label} must be a 4x4 SI transform")
        for cell in row:
            _number(cell, label)
    matrix = np.array(value, dtype=float)
    rotation = matrix[:3, :3]
    _require(
        np.allclose(matrix[3], [0, 0, 0, 1], atol=1e-12, rtol=0)
        and np.allclose(rotation.T @ rotation, np.eye(3), atol=1e-8, rtol=0)
        and abs(float(np.linalg.det(rotation)) - 1) <= 1e-8,
        f"{label} must contain a proper rotation and homogeneous last row",
    )
    return matrix


def _vector(value, label):
    _require(isinstance(value, list) and len(value) == 3, f"{label} must have three SI numbers")
    return np.array([_number(cell, label) for cell in value])


def _load(path: Path, root: Path, profile: dict):
    path = reference_path(path, root)
    before = file_digest(path)
    data = _read_json(path)
    _require(before == file_digest(path), "Mechanical reference changed while being read")
    _fields(
        data,
        (
            "schema_version",
            "hardware_id",
            "source_manifest_digest",
            "suite",
            "environment",
            "evidence_class",
            "data_role",
            "used_for_fitting",
            "conditions",
            "ownership",
            "joints",
            "constraints",
            "drives",
            "zero_pose",
            "poses",
            "conventions",
            "tolerances",
        ),
        "Mechanical reference",
    )
    _require(data["schema_version"] == SCHEMA, "Unsupported mechanical reference schema")
    _require(data["conventions"] == CONVENTIONS, "Mechanical reference must declare the supported SI frame conventions")
    _fields(data["tolerances"], ("position_m", "rotation_rad"), "Reference tolerances")
    for key, value in data["tolerances"].items():
        _require(_number(value, key) > 0, "Reference tolerances must be positive")
    _require(
        data["data_role"] == "validation" and data["used_for_fitting"] is False,
        "Mechanical reference must be independent validation, never fitting data",
    )
    _require(
        data["evidence_class"] in {"cad", "fixture"}, "Mechanical replay accepts CAD or analytic fixture references"
    )
    _require(
        isinstance(data["conditions"], dict) and bool(data["conditions"]),
        "Reference acquisition conditions are required",
    )
    _require(profile["purpose"] == "kinematics", "Mechanical replay supports kinematics only")
    _require(
        profile["acceptance_suites"] == [data["suite"]] and isinstance(data["suite"], str) and bool(data["suite"]),
        "The kinematics profile must require exactly the selected reference suite",
    )
    _require(data["environment"] == profile["consumer_environment"], "Reference environment differs from the profile")
    definition = read_data(confined(root, "config/robot.yaml"))
    source = read_data(confined(root, "sources/source.lock.json"))
    _require(data["hardware_id"] == definition["hardware_id"], "Reference hardware identity differs")
    _require(data["source_manifest_digest"] == source["manifest_digest"], "Reference CAD revision differs")
    _require(
        data["evidence_class"] == source["evidence_class"], "Reference evidence class differs from the frozen source"
    )
    from ..build import subject_files

    _require(before not in subject_files(root).values(), "Reference bytes are already model/fitting inputs")
    return path, before, data


def _rotation_error(actual, expected) -> float:
    # atan2 remains accurate near zero and pi; acos(trace) loses small rotations.
    delta = np.asarray(actual).T @ np.asarray(expected)
    skew = [delta[2, 1] - delta[1, 2], delta[0, 2] - delta[2, 0], delta[1, 0] - delta[0, 1]]
    return math.atan2(float(np.linalg.norm(skew)) / 2, (float(np.trace(delta)) - 1) / 2)


def _axis_error(actual, expected) -> float:
    if not np.isfinite(actual).all() or abs(float(np.linalg.norm(actual)) - 1) > 1e-8:
        return math.inf
    return math.atan2(float(np.linalg.norm(np.cross(actual, expected))), float(np.dot(actual, expected)))


def _drive_identity(drive: dict) -> dict:
    return {key: sorted(value) if key in {"stator", "rotor"} else value for key, value in drive.items()}


def _consumer_locations(mujoco, model, names, frames) -> dict:
    locations = {}
    for name in names:
        kind = "site" if name in frames else "body"
        obj = mujoco.mjtObj.mjOBJ_SITE if kind == "site" else mujoco.mjtObj.mjOBJ_BODY
        index = mujoco.mj_name2id(model, obj, name)
        _require(index >= 0, f"MuJoCo is missing reference {kind} {name}")
        locations[name] = (kind, index)
    return locations


def _consumer_pose(data, location):
    kind, index = location
    if kind == "site":
        return data.site_xpos[index], data.site_xmat[index].reshape(3, 3)
    return data.xpos[index], data.xmat[index].reshape(3, 3)


def execute(root: Path, profile: dict, reference: Path) -> dict:
    """Read the actual artifacts and compare all stable observations with the reference."""
    from ..build import subject_files, verify_toolchain
    from .simulation import _scan_mjcf

    root = Path(root).resolve()
    subject_before = digest(subject_files(root))
    path, reference_digest, expected = _load(reference, root, profile)
    position_atol = min(profile["position_atol"], expected["tolerances"]["position_m"])
    rotation_atol = min(profile["rotation_atol"], expected["tolerances"]["rotation_rad"])
    _scan_mjcf(root, "mjcf/robot.xml")
    _scan_mjcf(root, "mjcf/scene.xml")
    import mujoco

    _require(
        profile["consumer_environment"].get("mujoco") == mujoco.__version__,
        "Profile must pin the actual MuJoCo version",
    )
    urdf = parser.load_urdf(confined(root, "urdf/robot.urdf"))
    canonical = read_data(confined(root, "model/robot.json"))
    mjmodel = mujoco.MjModel.from_xml_path(str(confined(root, "mjcf/robot.xml")))
    mjdata = mujoco.MjData(mjmodel)
    scene_model = mujoco.MjModel.from_xml_path(str(confined(root, "mjcf/scene.xml")))
    scene_data = mujoco.MjData(scene_model)
    _require(not urdf.duplicate_links and not urdf.duplicate_joints, "Duplicate URDF identities")
    _require(
        all(j.type in {"fixed", "revolute", "continuous", "prismatic"} for j in urdf.joints.values()),
        "Mechanical replay supports fixed, revolute, continuous and prismatic joints only",
    )
    ownership = expected["ownership"]
    _require(
        isinstance(ownership, dict) and set(ownership) == set(urdf.links),
        "Reference must cover every rigid body exactly",
    )
    actual_owners = {
        link["name"]: sorted(link.get("provenance", {}).get("source_entities", [])) for link in canonical["links"]
    }
    actual_owners.update({frame["name"]: [] for frame in canonical["frames"]})
    flat = []
    for entities in ownership.values():
        _require(
            isinstance(entities, list) and all(isinstance(item, str) and item for item in entities),
            "Ownership requires source instance identifiers",
        )
        _require(len(entities) == len(set(entities)), "Duplicate reference ownership")
        flat.extend(entities)
    _require(len(flat) == len(set(flat)), "A source instance cannot belong to multiple rigid bodies")
    ownership_ok = {name: sorted(entities) == actual_owners.get(name) for name, entities in ownership.items()}
    joints = expected["joints"]
    _require(isinstance(joints, dict) and set(joints) == set(urdf.joints), "Reference must cover every joint exactly")
    xml_joints = {item.get("name"): item for item in ET.parse(root / "urdf/robot.urdf").getroot().findall("joint")}
    joint_checks = {}
    mimics = {}
    for name, ref in joints.items():
        _fields(ref, ("type", "parent", "child", "origin", "axis", "limits", "mimic"), f"Joint {name}")
        joint = urdf.joints[name]
        origin = _matrix(ref["origin"], f"Joint {name} origin")
        observed = np.eye(4)
        observed[:3, :3] = parser.rpy_matrix(joint.rpy)
        observed[:3, 3] = joint.origin
        axis_ok = ref["axis"] is None and not joint.moveable
        if joint.moveable:
            axis = _vector(ref["axis"], f"Joint {name} axis")
            _require(abs(float(np.linalg.norm(axis)) - 1) <= 1e-8, "Reference axes must be signed unit vectors")
            axis_ok = joint.axis is not None and _axis_error(axis, np.array(joint.axis)) <= rotation_atol
        if joint.type in {"revolute", "prismatic"}:
            _fields(ref["limits"], ("lower", "upper"), f"Joint {name} limits")
            lower = _number(ref["limits"]["lower"], name)
            upper = _number(ref["limits"]["upper"], name)
            _require(lower < upper, "Reference joint limits must be increasing")
            tolerance = position_atol if joint.type == "prismatic" else rotation_atol
            limits_ok = all(
                abs(ref["limits"][key] - (joint.limits or {}).get(key, math.inf)) <= tolerance
                for key in ("lower", "upper")
            )
        else:
            _require(ref["limits"] is None, "Fixed/continuous reference joints have no position limits")
            limits_ok = True
        node = xml_joints[name].find("mimic")
        actual_mimic = (
            None
            if node is None
            else {
                "joint": node.get("joint"),
                "multiplier": float(node.get("multiplier", "1")),
                "offset": float(node.get("offset", "0")),
            }
        )
        if ref["mimic"] is not None:
            _fields(ref["mimic"], ("joint", "multiplier", "offset"), f"Joint {name} mimic")
            _require(
                ref["mimic"]["joint"] in joints and ref["mimic"]["joint"] != name, "Invalid reference mimic identity"
            )
            _number(ref["mimic"]["multiplier"], name)
            _number(ref["mimic"]["offset"], name)
            mimics[name] = ref["mimic"]
        joint_checks[name] = {
            "identity": (ref["type"], ref["parent"], ref["child"]) == (joint.type, joint.parent, joint.child),
            "position_error_m": float(np.linalg.norm(origin[:3, 3] - observed[:3, 3])),
            "rotation_error_rad": _rotation_error(origin[:3, :3], observed[:3, :3]),
            "axis": axis_ok,
            "limits": limits_ok,
            "mimic": ref["mimic"] == actual_mimic,
        }
    _require(isinstance(expected["constraints"], list), "Reference constraints must be a list")
    constraints_ok = expected["constraints"] == canonical.get("constraints", [])
    moving = {name: item for name, item in urdf.joints.items() if item.moveable}
    drives = expected["drives"]
    _require(
        isinstance(drives, dict) and set(drives) == set(moving),
        "Reference must declare every movable joint's drive or passive role",
    )
    authored_drives = canonical.get("mechanical_drives", {})
    _require(
        set(authored_drives) == set(moving),
        "Author interfaces.mechanical_drives must identify every movable joint's drive or passive role",
    )
    drive_checks = {}
    drive_ids = set()
    for name, drive in drives.items():
        _require(isinstance(drive, dict), "Invalid drive reference")
        if drive.get("kind") == "passive":
            _fields(drive, ("kind",), f"Drive {name}")
            drive_checks[name] = drive == authored_drives[name]
        else:
            _fields(drive, ("kind", "id", "stator", "rotor"), f"Drive {name}")
            _require(
                drive["kind"] == "active" and isinstance(drive["id"], str) and bool(drive["id"]),
                "Active drive requires its hardware identity",
            )
            _require(drive["id"] not in drive_ids, "An active drive identity cannot be assigned twice")
            drive_ids.add(drive["id"])
            for key in ("stator", "rotor"):
                _require(
                    isinstance(drive[key], list)
                    and bool(drive[key])
                    and all(isinstance(v, str) and v for v in drive[key]),
                    "Drive parts require exact source instance identifiers",
                )
                _require(len(drive[key]) == len(set(drive[key])), "Duplicate drive part identity")
            _require(not set(drive["stator"]) & set(drive["rotor"]), "Stator and rotor instances must be distinct")
            parent, child = moving[name].parent, moving[name].child
            drive_checks[name] = (
                _drive_identity(drive) == _drive_identity(authored_drives[name])
                and set(drive["stator"]) <= set(actual_owners[parent])
                and set(drive["rotor"]) <= set(actual_owners[child])
            )
    poses = expected["poses"]
    _require(isinstance(poses, list) and 2 <= len(poses) <= POSE_LIMIT, "Reference requires 2..512 observed poses")
    _require(len(poses) * len(urdf.links) <= OBSERVATION_LIMIT, "Mechanical reference exceeds the observation budget")
    frame_names = {frame["name"] for frame in canonical["frames"]}
    locations = _consumer_locations(mujoco, mjmodel, urdf.links, frame_names)
    addresses = {}
    scene_addresses = {}
    scene_locations = _consumer_locations(mujoco, scene_model, urdf.links, frame_names)
    for name in moving:
        index = mujoco.mj_name2id(mjmodel, mujoco.mjtObj.mjOBJ_JOINT, name)
        _require(index >= 0, f"MuJoCo is missing reference joint {name}")
        addresses[name] = int(mjmodel.jnt_qposadr[index])
        ref = joints[name]
        kind = mujoco.mjtJoint.mjJNT_SLIDE if ref["type"] == "prismatic" else mujoco.mjtJoint.mjJNT_HINGE
        limits = ref["limits"]
        tolerance = position_atol if ref["type"] == "prismatic" else rotation_atol
        joint_checks[name]["mujoco"] = {}
        for entry, consumer in (("robot", mjmodel), ("scene", scene_model)):
            joint_id = mujoco.mj_name2id(consumer, mujoco.mjtObj.mjOBJ_JOINT, name)
            _require(joint_id >= 0, f"MuJoCo {entry} is missing reference joint {name}")
            if entry == "scene":
                scene_addresses[name] = int(consumer.jnt_qposadr[joint_id])
            joint_checks[name]["mujoco"][entry] = bool(
                consumer.jnt_type[joint_id] == kind
                and _axis_error(consumer.jnt_axis[joint_id], _vector(ref["axis"], name)) <= rotation_atol
                and bool(consumer.jnt_limited[joint_id]) == (limits is not None)
                and (
                    limits is None
                    or np.allclose(
                        consumer.jnt_range[joint_id], [limits["lower"], limits["upper"]], atol=tolerance, rtol=0
                    )
                )
            )
    equality_checks = {}
    for entry, consumer in (("robot", mjmodel), ("scene", scene_model)):
        _require(consumer.neq == len(mimics), f"MuJoCo {entry} has unsupported or extra mechanical equalities")
        for name, relation in mimics.items():
            child = mujoco.mj_name2id(consumer, mujoco.mjtObj.mjOBJ_JOINT, name)
            parent = mujoco.mj_name2id(consumer, mujoco.mjtObj.mjOBJ_JOINT, relation["joint"])
            equality_checks[f"{entry}/{name}"] = any(
                consumer.eq_type[i] == mujoco.mjtEq.mjEQ_JOINT
                and consumer.eq_obj1id[i] == child
                and consumer.eq_obj2id[i] == parent
                and bool(consumer.eq_active0[i])
                and np.allclose(
                    consumer.eq_data[i, :5], [relation["offset"], relation["multiplier"], 0, 0, 0], atol=1e-12, rtol=0
                )
                for i in range(consumer.neq)
            )
    observations = []
    seen = set()
    values_by_joint: dict[str, set[float]] = {name: set() for name in moving if name not in mimics}
    zero_found = False
    for pose in poses:
        _fields(pose, ("name", "joints", "base", "links"), "Reference pose")
        _require(
            isinstance(pose["name"], str) and bool(pose["name"]) and pose["name"] not in seen,
            "Pose names must be unique",
        )
        seen.add(pose["name"])
        values = pose["joints"]
        _require(
            isinstance(values, dict) and set(values) == set(moving),
            "Every reference pose must cover all movable joints",
        )
        for name, value in values.items():
            value = _number(value, f"Pose {pose['name']} joint {name}")
            ref = joints[name]
            if ref["limits"] is not None:
                _require(
                    ref["limits"]["lower"] <= value <= ref["limits"]["upper"],
                    "Reference pose is outside approved limits",
                )
            if name in values_by_joint:
                values_by_joint[name].add(value)
        for name, relation in mimics.items():
            _require(
                abs(values[name] - (relation["multiplier"] * values[relation["joint"]] + relation["offset"])) <= 1e-9,
                "Reference pose contradicts approved coupling",
            )
        base = _matrix(pose["base"], "Reference base")
        if profile["root_mode"] == "fixed":
            _require(np.allclose(base, np.eye(4), atol=1e-12, rtol=0), "Fixed-root reference uses the root link frame")
        if pose["name"] == expected["zero_pose"]:
            _require(
                all(values[name] == 0 for name in values_by_joint),
                "Reference zero pose must set every independent joint to zero",
            )
            zero_found = True
        _require(
            isinstance(pose["links"], dict) and set(pose["links"]) == set(urdf.links),
            "Every reference pose must observe every body",
        )
        computed = {name: base @ matrix for name, matrix in _poses(urdf, values).items()}
        _apply_pose(
            mujoco,
            mjmodel,
            mjdata,
            addresses,
            {"joints": values, "matrix": base if profile["root_mode"] == "floating" else None},
        )
        mujoco.mj_forward(mjmodel, mjdata)
        _apply_pose(
            mujoco,
            scene_model,
            scene_data,
            scene_addresses,
            {"joints": values, "matrix": base if profile["root_mode"] == "floating" else None},
        )
        mujoco.mj_forward(scene_model, scene_data)
        _require(
            np.isfinite(mjdata.xpos).all() and np.isfinite(mjdata.xmat).all(), "Non-finite MuJoCo reference replay"
        )
        _require(
            np.isfinite(scene_data.xpos).all() and np.isfinite(scene_data.xmat).all(), "Non-finite MuJoCo scene replay"
        )
        for name, raw in pose["links"].items():
            observed = _matrix(raw, f"Pose {pose['name']} body {name}")
            position, rotation = _consumer_pose(mjdata, locations[name])
            scene_position, scene_rotation = _consumer_pose(scene_data, scene_locations[name])
            observations.append(
                {
                    "pose": pose["name"],
                    "body": name,
                    "urdf_position_error_m": float(np.linalg.norm(computed[name][:3, 3] - observed[:3, 3])),
                    "urdf_rotation_error_rad": _rotation_error(computed[name][:3, :3], observed[:3, :3]),
                    "mujoco_position_error_m": float(np.linalg.norm(position - observed[:3, 3])),
                    "mujoco_rotation_error_rad": _rotation_error(rotation, observed[:3, :3]),
                    "scene_position_error_m": float(np.linalg.norm(scene_position - observed[:3, 3])),
                    "scene_rotation_error_rad": _rotation_error(scene_rotation, observed[:3, :3]),
                }
            )
    _require(zero_found, "Reference is missing the approved zero pose")
    for name, values in values_by_joint.items():
        tolerance = position_atol if moving[name].type == "prismatic" else rotation_atol
        _require(
            len(values) >= 2 and max(values) - min(values) > max(10 * tolerance, 1e-6),
            f"Reference has no observable held-out motion for {name}",
        )
    joint_ok = all(
        item["identity"]
        and item["axis"]
        and item["limits"]
        and item["mimic"]
        and all(item.get("mujoco", {}).values())
        and item["position_error_m"] <= position_atol
        and item["rotation_error_rad"] <= rotation_atol
        for item in joint_checks.values()
    )
    pose_ok = all(
        item["urdf_position_error_m"] <= position_atol
        and item["mujoco_position_error_m"] <= position_atol
        and item["urdf_rotation_error_rad"] <= rotation_atol
        and item["mujoco_rotation_error_rad"] <= rotation_atol
        and item["scene_position_error_m"] <= position_atol
        and item["scene_rotation_error_rad"] <= rotation_atol
        for item in observations
    )
    _require(
        file_digest(path) == reference_digest and digest(subject_files(root)) == subject_before,
        "Reference or model changed during mechanical replay",
    )
    return {
        "reference_sha256": reference_digest,
        "subject": subject_before,
        "suite": expected["suite"],
        "environment": expected["environment"],
        "conditions": {
            "acquisition": expected["conditions"],
            "position_atol_m": position_atol,
            "rotation_atol_rad": rotation_atol,
        },
        "runtime": {"python": platform.python_version(), "mujoco": mujoco.__version__},
        "tool": verify_toolchain(root),
        "passed": all(ownership_ok.values())
        and joint_ok
        and constraints_ok
        and all(drive_checks.values())
        and all(equality_checks.values())
        and pose_ok,
        "ownership": ownership_ok,
        "joints": joint_checks,
        "constraints": constraints_ok,
        "drives": drive_checks,
        "couplings": equality_checks,
        "observations": observations,
        "scope": (
            "Kinematics and authored mechanical drive ownership against the operator-selected reference; "
            "no physical, training, hardware or release qualification."
        ),
    }


def run_acceptance(root: Path, profile_name: str, reference: Path, out: Path) -> dict:
    from ..build import assess, profile_for

    root, out = Path(root).resolve(), Path(out).resolve()
    _require(
        not out.is_relative_to(root) and not out.exists(),
        "Acceptance requires a new output directory outside the read-only candidate",
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{out.name}.partial-", dir=out.parent))
    try:
        from .simulation import _pending_candidate, _scan_mjcf

        profile = profile_for(root, profile_name)
        _load(reference, root, profile)
        _scan_mjcf(root, "mjcf/robot.xml")
        _scan_mjcf(root, "mjcf/scene.xml")
        report = assess(root, profile_name, mechanical_reference=reference)
        allowed = {"consumer.application"}
        if _pending_candidate(root, report):
            allowed.add("bundle.report_binding")
        _require(
            not set(report["blockers"]) - allowed,
            f"Other model failures prevent mechanical acceptance: {report['blockers']}",
        )
        measured = execute(root, profile, reference)
        _require(measured["subject"] == report["subject"], "Model changed before mechanical replay")
        telemetry = "docs/acceptance/mechanical-observations.json"
        write_json(staging / telemetry, measured)
        _require((staging / telemetry).stat().st_size <= TELEMETRY_LIMIT, "Mechanical telemetry exceeds the byte limit")
        record = {
            "schema_version": "description.acceptance/v2",
            "purpose": "kinematics",
            "subject": report["subject"],
            "profile_digest": digest(profile),
            "environment": profile["consumer_environment"],
            "attestation": ATTESTATION,
            "reference_sha256": measured["reference_sha256"],
            "tool": measured["tool"],
            "runtime": measured["runtime"],
            "qualification": QUALIFICATION,
            "results": [
                {
                    "suite": measured["suite"],
                    "suite_version": SCHEMA,
                    "passed": measured["passed"],
                    "producer": "description.mechanical-reference-replay/v1",
                    "executed_at": datetime.now(UTC).isoformat(),
                    "evidence_class": "reference_replay",
                    "data_role": "validation",
                    "used_for_fitting": False,
                    "conditions": measured["conditions"],
                    "artifacts": {telemetry: file_digest(staging / telemetry)},
                    "validation_data": {telemetry: file_digest(staging / telemetry)},
                }
            ],
        }
        write_json(staging / "docs/acceptance/kinematics.json", record)
        write_json(staging / "acceptance.json", record)
        os.replace(staging, out)
        return record
    except Exception as error:
        failed = Path(tempfile.mkdtemp(prefix=f"{out.name}.failed-", dir=out.parent))
        for path in staging.iterdir():
            shutil.move(str(path), str(failed / path.name))
        write_json(failed / "failure.json", {"error": type(error).__name__, "message": str(error)})
        vars(error)["diagnostic_path"] = str(failed)
        raise
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def verify_replay(root: Path, record: dict, subject: str, profile: dict, reference: Path | None) -> dict:
    try:
        _fields(
            record,
            (
                "schema_version",
                "purpose",
                "subject",
                "profile_digest",
                "environment",
                "attestation",
                "reference_sha256",
                "tool",
                "runtime",
                "qualification",
                "results",
            ),
            "Mechanical acceptance",
        )
        _require(
            record.get("attestation") == ATTESTATION and record.get("purpose") == "kinematics",
            "Unsupported mechanical replay attestation",
        )
        _require(
            record.get("qualification") == QUALIFICATION,
            "Mechanical replay cannot grant physical, training, hardware or release qualification",
        )
        if reference is None:
            return {
                "trusted": False,
                "status": "not_run",
                "execution": "mechanical_reference_replay",
                "reason": "Consumer must select an approved external --mechanical-reference",
            }
        _require(
            record.get("subject") == subject and record.get("profile_digest") == digest(profile),
            "Mechanical acceptance identity changed",
        )
        actual = execute(root, profile, reference)
        _require(
            record.get("reference_sha256") == actual["reference_sha256"]
            and record.get("tool") == actual["tool"]
            and record.get("runtime") == actual["runtime"],
            "Reference, tool or runtime differs from acceptance",
        )
        name = "docs/acceptance/mechanical-observations.json"
        _require(
            _read_json(confined(root, name), limit=TELEMETRY_LIMIT) == actual and actual["passed"] is True,
            "Mechanical observations do not reproduce or failed",
        )
        expected = {
            "suite": actual["suite"],
            "suite_version": SCHEMA,
            "passed": True,
            "producer": "description.mechanical-reference-replay/v1",
            "evidence_class": "reference_replay",
            "data_role": "validation",
            "used_for_fitting": False,
            "conditions": actual["conditions"],
            "artifacts": {name: file_digest(root / name)},
            "validation_data": {name: file_digest(root / name)},
        }
        _require(
            isinstance(record.get("results"), list) and len(record["results"]) == 1, "Mechanical suite coverage changed"
        )
        stored = record["results"][0]
        _require(
            bool(stored.get("executed_at"))
            and {key: value for key, value in stored.items() if key != "executed_at"} == expected,
            "Mechanical result differs from replay",
        )
        return {
            "trusted": True,
            "status": "passed",
            "execution": "mechanical_reference_replay",
            "reference_sha256": actual["reference_sha256"],
            "scope": actual["scope"],
        }
    except ImportError as error:
        return {"trusted": False, "status": "not_run", "execution": "mechanical_reference_replay", "reason": str(error)}
    except (PipelineError, OSError, ValueError, KeyError, TypeError, AttributeError, RuntimeError) as error:
        from ..doctor import app_control_rejection

        blocked = app_control_rejection(error)
        if blocked is not None:
            return {
                "trusted": False,
                "status": "not_run",
                "execution": "mechanical_reference_replay",
                "reason": "Windows App Control blocked the required runtime",
                "runtime_block": blocked,
            }
        return {"trusted": False, "status": "failed", "execution": "mechanical_reference_replay", "reason": str(error)}


def complete_pending(root: Path, profile_name: str, report: dict, reference: Path) -> dict:
    """Run held-out mechanical replay during an already locked model update."""
    from ..build import author_files, build

    reference = reference_path(reference, root)
    before = author_files(root)
    parent = root / "build/acceptance"
    parent.mkdir(parents=True, exist_ok=True)
    out = Path(tempfile.mkdtemp(prefix="mechanical-", dir=parent)) / "result"
    record = run_acceptance(Path(report["diagnostic_path"]), profile_name, reference, out)
    if not all(item["passed"] for item in record["results"]):
        return {**report, "diagnostic_path": str(out)}
    _require(author_files(root) == before, "Author inputs changed during acceptance; rebuild before submitting")
    # Only a successful run may replace evidence. The following build verifies it again.
    for path in (out / "docs/acceptance").iterdir():
        destination = confined(root, f"docs/acceptance/{path.name}", exists=False)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, destination)
    return build(root, profile_name, mechanical_reference=reference)
