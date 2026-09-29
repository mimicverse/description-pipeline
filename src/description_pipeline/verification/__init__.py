"""Independent evidence from serialized files and the actual consumer runtime."""

from __future__ import annotations

import importlib.metadata
import math
from datetime import date
from pathlib import Path
from xml.etree import ElementTree as ET

import numpy as np

from ..io import PipelineError, confined, read_data
from ..model import Robot
from .urdf_quality import model as parser
from .urdf_quality import rules
from .urdf_quality import waivers
from .urdf_quality import ledger as joint_ledger
from .consumer_geometry import geometry_evidence
from .interfaces import interfaces
from .dynamics import dynamics_evidence
from .control import control_evidence
from .poses import load as load_validation_poses
from .uniform_density import summarize, uniform_density_evidence


def result(code: str, passed: bool, *, expected=(), checked=(), details=None, status=None) -> dict:
    missing = sorted(set(expected) - set(checked))
    return {
        "id": code,
        "version": 1,
        "status": status or ("passed" if passed and not missing else "failed"),
        "expected": sorted(expected),
        "checked": sorted(checked),
        "missing": missing,
        "details": {} if details is None else details,
    }


def environment() -> dict:
    import platform

    versions = {"python": platform.python_version(), "platform": platform.platform()}
    for name in ("numpy", "mujoco", "mimicverse-description", "jsonschema", "PyYAML"):
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = "unavailable"
    return versions


def _close(left, right, *, atol=1e-10, rtol=1e-7) -> bool:
    return bool(np.allclose(left, right, atol=atol, rtol=rtol))


def _xml_roundtrip(data: dict, urdf: parser.UrdfModel, root: Path) -> list[dict]:
    expected = {link["name"] for link in data["links"]} | {frame["name"] for frame in data["frames"]}
    seen = set(urdf.links)
    evidence = []
    for link in data["links"]:
        actual = urdf.links.get(link["name"])
        if actual is None:
            continue
        reference = link["inertial"]
        valid = (reference is None) == (actual.inertial is None)
        if reference and actual.inertial:
            valid &= _close(reference["mass"], actual.inertial.mass)
            valid &= _close(reference["xyz"], actual.inertial.origin)
            valid &= _close(reference["rpy"], actual.inertial.rpy)
            valid &= _close(reference["inertia"], actual.inertial.inertia)
        evidence.append({"object": link["name"], "passed": bool(valid)})
    results = [
        result(
            "urdf.links",
            seen == expected and all(item["passed"] for item in evidence),
            expected=expected,
            checked=seen,
            details=evidence,
        )
    ]
    joint_details = []
    for joint in data["joints"]:
        actual_joint = urdf.joints.get(joint["name"])
        if actual_joint is None:
            continue
        valid = (actual_joint.parent, actual_joint.child, actual_joint.type) == (
            joint["parent"],
            joint["child"],
            joint["type"],
        )
        valid &= _close(actual_joint.origin, joint["xyz"]) and _close(actual_joint.rpy, joint["rpy"])
        if joint["type"] != "fixed":
            valid &= actual_joint.axis is not None and _close(actual_joint.axis, joint["axis"])
        limits = joint.get("limits", {})
        valid &= set(actual_joint.limits or {}) == set(limits)
        for key, value in limits.items():
            valid &= (
                actual_joint.limits is not None
                and key in actual_joint.limits
                and _close(actual_joint.limits[key], value)
            )
        joint_details.append({"object": joint["name"], "passed": bool(valid)})
    expected_joints = {joint["name"] for joint in data["joints"]} | {
        frame["name"] + "_fixed" for frame in data["frames"]
    }
    results.append(
        result(
            "urdf.joints",
            set(urdf.joints) == expected_joints and all(item["passed"] for item in joint_details),
            expected=expected_joints,
            checked=urdf.joints,
            details=joint_details,
        )
    )
    frame_details = []
    for frame in data["frames"]:
        actual_frame = urdf.links.get(frame["name"])
        attachment = urdf.joints.get(frame["name"] + "_fixed")
        valid = (
            actual_frame is not None
            and actual_frame.inertial is None
            and actual_frame.visuals == actual_frame.collisions == 0
            and attachment is not None
            and (attachment.parent, attachment.child, attachment.type) == (frame["parent"], frame["name"], "fixed")
            and _close(attachment.origin, frame["xyz"])
            and _close(attachment.rpy, frame["rpy"])
        )
        frame_details.append({"object": frame["name"], "passed": bool(valid)})
    results.append(result("urdf.frames", all(item["passed"] for item in frame_details), details=frame_details))
    xml = ET.parse(root / "urdf/robot.urdf").getroot()
    supplemental = []
    for joint in data["joints"]:
        node = next((item for item in xml.findall("joint") if item.get("name") == joint["name"]), None)
        valid = node is not None
        if node is not None:
            for field in ("dynamics", "mimic"):
                target = joint.get(field, {})
                element = node.find(field)
                actual_fields = {} if element is None else element.attrib
                valid &= set(target) == set(actual_fields)
                for key in target.keys() & actual_fields.keys():
                    valid &= (
                        actual_fields[key] == target[key]
                        if key == "joint"
                        else _close(float(actual_fields[key]), target[key])
                    )
        supplemental.append({"object": joint["name"], "passed": bool(valid)})
    results.append(result("urdf.dynamics_mimic", all(item["passed"] for item in supplemental), details=supplemental))
    mesh_evidence = []
    for link in data["links"]:
        node = next((node for node in xml.findall("link") if node.get("name") == link["name"]), None)
        if node is None:
            continue
        for role, tag in (("visuals", "visual"), ("collisions", "collision")):
            shapes = node.findall(tag)
            valid = len(shapes) == len(link[role])
            for shape, expected_shape in zip(shapes, link[role], strict=False):
                o = shape.find("origin")
                valid &= o is not None and _close([float(x) for x in o.get("xyz", "").split()], expected_shape["xyz"])
                valid &= o is not None and _close([float(x) for x in o.get("rpy", "").split()], expected_shape["rpy"])
                geometry = shape.find(f"geometry/{expected_shape['kind']}")
                valid &= geometry is not None
                if geometry is not None:
                    for key in ("size", "scale", "radius", "length"):
                        if key in expected_shape:
                            target = expected_shape[key]
                            valid &= _close(
                                [float(x) for x in geometry.get(key, "").split()],
                                target if isinstance(target, list) else [target],
                            )
                    if expected_shape["kind"] == "mesh":
                        reference = (root / expected_shape["filename"]).resolve()
                        actual_path = (root / "urdf" / geometry.get("filename", "")).resolve()
                        valid &= (
                            actual_path == reference
                            and actual_path.is_relative_to(root.resolve())
                            and actual_path.is_file()
                        )
            mesh_evidence.append({"object": f"{link['name']}/{role}", "passed": bool(valid)})
    results.append(result("urdf.geometry", all(item["passed"] for item in mesh_evidence), details=mesh_evidence))
    return results


def _poses(urdf: parser.UrdfModel, values: dict) -> dict:
    children: dict[str, list] = {}
    for joint in urdf.joints.values():
        children.setdefault(joint.parent, []).append(joint)
    poses = {}

    def walk(name, matrix):
        poses[name] = matrix
        for joint in children.get(name, []):
            local = np.eye(4)
            local[:3, :3] = parser.rpy_matrix(joint.rpy)
            local[:3, 3] = joint.origin
            motion = np.eye(4)
            amount = values.get(joint.name, 0.0)
            if joint.type == "prismatic":
                motion[:3, 3] = np.asarray(joint.axis) * amount
            elif joint.type in {"revolute", "continuous"}:
                axis = np.asarray(joint.axis, dtype=float)
                cross = np.array([[0, -axis[2], axis[1]], [axis[2], 0, -axis[0]], [-axis[1], axis[0], 0]])
                motion[:3, :3] = np.eye(3) + math.sin(amount) * cross + (1 - math.cos(amount)) * (cross @ cross)
            walk(joint.child, matrix @ local @ motion)

    for name in set(urdf.links) - {joint.child for joint in urdf.joints.values()}:
        walk(name, np.eye(4))
    return poses


def _samples(urdf: parser.UrdfModel, profile: dict) -> list[dict]:
    joints = [joint for joint in urdf.joints.values() if joint.moveable]
    zero = {
        joint.name: min(
            max(0.0, (joint.limits or {}).get("lower", -math.pi)), (joint.limits or {}).get("upper", math.pi)
        )
        for joint in joints
    }
    samples = [zero]
    for joint in joints:
        lo = (joint.limits or {}).get("lower", -math.pi)
        hi = (joint.limits or {}).get("upper", math.pi)
        for fraction in (0.01, 0.5, 0.99):
            samples.append({**zero, joint.name: lo + fraction * (hi - lo)})
    random = np.random.default_rng(profile.get("seed", 0))
    for _ in range(profile.get("random_poses", 12)):
        samples.append(
            {
                joint.name: float(
                    random.uniform(
                        (joint.limits or {}).get("lower", -math.pi), (joint.limits or {}).get("upper", math.pi)
                    )
                )
                for joint in joints
            }
        )
    return samples


def _scene_contract(root: Path, profile: dict) -> dict:
    """The v1 scene may include the verified robot and a declared ground plane only.

    MuJoCo defaults/compiler/equality additions can change effective robot physics
    even when the body counts and inertia arrays still match.
    """
    scene = ET.parse(root / "mjcf/scene.xml").getroot()
    valid = scene.tag == "mujoco" and set(scene.attrib) <= {"model"}
    valid &= [child.tag for child in scene] == (["include", "worldbody"] if profile["ground"] else ["include"])
    include = scene.find("include")
    valid &= include is not None and include.attrib == {"file": "robot.xml"} and len(include) == 0
    if profile["ground"]:
        world = scene.find("worldbody")
        valid &= world is not None and not world.attrib and [child.tag for child in world] == ["geom"]
        plane = scene.find("worldbody/geom")
        expected = {"size": [0, 0, 0.1], **(profile["contact"] or {})}
        valid &= plane is not None and len(plane) == 0
        if plane is not None:
            valid &= set(plane.attrib) == {"name", "type", *expected}
            valid &= plane.get("name") == "ground" and plane.get("type") == "plane"
            try:
                for field, value in expected.items():
                    actual = [float(part) for part in plane.get(field, "").split()]
                    target = value if isinstance(value, list) else [value]
                    valid &= len(actual) == len(target) and _close(actual, target)
            except ValueError:
                valid = False
    return result("consumer.scene_contract", bool(valid), details={"format": "robot include and optional ground"})


def _apply_pose(mujoco, model, data, addresses: dict, pose: dict) -> None:
    """Reset ``data`` to one declared working pose (joints plus optional base)."""

    mujoco.mj_resetData(model, data)
    for name, value in pose["joints"].items():
        if name in addresses:
            data.qpos[addresses[name]] = value
    matrix = pose.get("matrix")
    if matrix is None:
        return
    free = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_JOINT, "root_free_joint")
    if free >= 0 and model.jnt_type[free] == mujoco.mjtJoint.mjJNT_FREE:
        address = int(model.jnt_qposadr[free])
        data.qpos[address : address + 3] = matrix[:3, 3]
        mujoco.mju_mat2Quat(data.qpos[address + 3 : address + 7], np.asarray(matrix)[:3, :3].ravel())


def _consumer(data: dict, urdf: parser.UrdfModel, root: Path, profile: dict) -> list[dict]:
    try:
        import mujoco
    except ImportError:
        return [
            result("consumer.available", False, details="MuJoCo is required and is not installed", status="not_run")
        ]
    try:
        model = mujoco.MjModel.from_xml_path(str(root / "mjcf/robot.xml"))
        scene = mujoco.MjModel.from_xml_path(str(root / "mjcf/scene.xml"))
    except Exception as error:
        return [result("consumer.compile", False, details=str(error))]
    results = [
        _scene_contract(root, profile),
        result(
            "consumer.compile",
            True,
            details={"version": mujoco.__version__, "nq": model.nq, "nv": model.nv, "nu": model.nu},
        ),
    ]
    names = {mujoco.mj_id2name(model, mujoco.mjtObj.mjOBJ_BODY, i): i for i in range(1, model.nbody)}
    expected = {link["name"] for link in data["links"]}
    inertia_details = []
    for name in sorted(expected & names.keys()):
        link = urdf.links[name]
        i = names[name]
        if link.inertial is None:
            valid = abs(float(model.body_mass[i])) < 1e-12
            inertia_details.append({"object": name, "passed": valid, "massless": True})
            continue
        inertial = link.inertial
        r = np.asarray(parser.rpy_matrix(inertial.rpy))
        expected_tensor = r @ np.asarray(inertial.matrix()) @ r.T
        flat = np.zeros(9)
        mujoco.mju_quat2Mat(flat, model.body_iquat[i])
        compiled_r = flat.reshape(3, 3)
        actual_tensor = compiled_r @ np.diag(model.body_inertia[i]) @ compiled_r.T
        gap = float(np.linalg.norm(actual_tensor - expected_tensor))
        valid = _close(actual_tensor, expected_tensor, atol=profile["inertia_atol"], rtol=profile["inertia_rtol"])
        valid &= _close(model.body_mass[i], inertial.mass) and _close(model.body_ipos[i], inertial.origin)
        inertia_details.append(
            {
                "object": name,
                "passed": bool(valid),
                "tensor_error_kg_m2": gap,
                "expected_tensor": expected_tensor.tolist(),
                "compiled_tensor": actual_tensor.tolist(),
            }
        )
    results.append(
        result(
            "consumer.inertia",
            expected == set(names) and all(item["passed"] for item in inertia_details),
            expected=expected,
            checked=names,
            details=inertia_details,
        )
    )
    moving = {joint.name: joint for joint in urdf.joints.values() if joint.moveable}
    addresses = {}
    joint_details = []
    for name, joint in moving.items():
        index = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_JOINT, name)
        if index < 0:
            continue
        addresses[name] = int(model.jnt_qposadr[index])
        expected_type = mujoco.mjtJoint.mjJNT_SLIDE if joint.type == "prismatic" else mujoco.mjtJoint.mjJNT_HINGE
        valid = model.jnt_type[index] == expected_type
        valid &= _close(model.jnt_axis[index], joint.axis)
        if joint.type != "continuous":
            valid &= bool(model.jnt_limited[index]) and _close(
                model.jnt_range[index], [(joint.limits or {})["lower"], (joint.limits or {})["upper"]]
            )
        else:
            valid &= not bool(model.jnt_limited[index])
        reference = next(item for item in data["joints"] if item["name"] == name)
        dof = int(model.jnt_dofadr[index])
        dynamics = reference.get("dynamics", {})
        valid &= _close(model.dof_damping[dof], dynamics.get("damping", 0))
        valid &= _close(model.dof_frictionloss[dof], dynamics.get("friction", 0))
        effort = reference.get("limits", {}).get("effort")
        if effort is not None and effort > 0:
            valid &= bool(model.jnt_actfrclimited[index]) and _close(model.jnt_actfrcrange[index], [-effort, effort])
        else:
            valid &= not bool(model.jnt_actfrclimited[index])
        joint_details.append({"object": name, "passed": bool(valid)})
    actual_joint_names = {mujoco.mj_id2name(model, mujoco.mjtObj.mjOBJ_JOINT, i) for i in range(model.njnt)}
    permitted = set(moving) | ({"root_free_joint"} if profile.get("root_mode") == "floating" else set())
    results.append(
        result(
            "consumer.joints",
            actual_joint_names == permitted and all(item["passed"] for item in joint_details),
            expected=moving,
            checked=addresses,
            details=joint_details,
        )
    )
    mjdata = mujoco.MjData(model)
    worst_position = 0.0
    worst_rotation = 0.0
    contacts = []
    dynamics = []
    sites_checked = set()
    frame_names = {frame["name"] for frame in data["frames"]}
    samples = _samples(urdf, profile)
    base_poses = []
    mimics = {item["name"]: item["mimic"] for item in data["joints"] if item.get("mimic")}
    # Declared poses govern the contact check; an unusable declaration fails instead
    # of falling back to the legacy all-sample policy.
    declared: list[dict] = []
    poses_error: str | None = None
    try:
        declared = load_validation_poses(root, profile, urdf, mimics)
    except PipelineError as error:
        poses_error = str(error)
    poses_status = "not_applicable" if poses_error is None and not declared else None
    results.append(
        result(
            "consumer.validation_poses",
            poses_error is None,
            status=poses_status,
            details=(
                {"error": poses_error, "declared": profile.get("validation_poses")}
                if poses_error is not None
                else {
                    "file": profile.get("validation_poses"),
                    "poses": [pose["name"] for pose in declared],
                    "contact_policy": "declared_poses" if declared else "all_samples",
                    "reset_source": declared[0]["name"] if declared else "default_qpos",
                    "note": (
                        "collision and reset checks use these declared working poses"
                        if declared
                        else "no validation poses declared: the legacy all-sample contact policy applies"
                    ),
                }
            ),
        )
    )
    for values in samples:

        def position(name, values=values):
            if name in mimics:
                relation = mimics[name]
                return relation["multiplier"] * position(relation["joint"]) + relation["offset"]
            return values[name]

        values.update({name: position(name) for name in mimics})
    for pose_index, values in enumerate(samples):
        mujoco.mj_resetData(model, mjdata)
        for name, value in values.items():
            if name in addresses:
                mjdata.qpos[addresses[name]] = value
        base_pose = np.eye(4)
        if profile["root_mode"] == "floating":
            free = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_JOINT, "root_free_joint")
            if free >= 0 and model.jnt_type[free] == mujoco.mjtJoint.mjJNT_FREE:
                fraction = pose_index / max(len(samples) - 1, 1)
                base_pose[:3, 3] = np.array([0.13, -0.07, 0.23]) * fraction
                base_pose[:3, :3] = parser.rpy_matrix(tuple(value * fraction for value in (0.41, -0.32, 0.67)))
                address = int(model.jnt_qposadr[free])
                mjdata.qpos[address : address + 3] = base_pose[:3, 3]
                mujoco.mju_mat2Quat(mjdata.qpos[address + 3 : address + 7], base_pose[:3, :3].ravel())
                base_poses.append(base_pose.tolist())
        mujoco.mj_forward(model, mjdata)
        expected_poses = {name: base_pose @ pose for name, pose in _poses(urdf, values).items()}
        dynamics.append({"pose": pose_index, **dynamics_evidence(urdf, expected_poses, model, mjdata, mujoco, profile)})
        for name in expected & names.keys():
            transform = expected_poses[name]
            worst_position = max(worst_position, float(np.linalg.norm(mjdata.xpos[names[name]] - transform[:3, 3])))
            worst_rotation = max(
                worst_rotation, float(np.linalg.norm(mjdata.xmat[names[name]].reshape(3, 3) - transform[:3, :3]))
            )
        for name in frame_names:
            index = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_SITE, name)
            if index >= 0 and name in expected_poses:
                sites_checked.add(name)
                transform = expected_poses[name]
                worst_position = max(worst_position, float(np.linalg.norm(mjdata.site_xpos[index] - transform[:3, 3])))
                worst_rotation = max(
                    worst_rotation, float(np.linalg.norm(mjdata.site_xmat[index].reshape(3, 3) - transform[:3, :3]))
                )
        if pose_index == 0:
            geoms = geometry_evidence(root, model, mjdata, mujoco, profile)
            results.append(result("consumer.geometry", all(item["passed"] for item in geoms), details=geoms))
        if not declared:  # legacy policy: every sample has to be collision free
            for contact in mjdata.contact[: mjdata.ncon]:
                if contact.dist < -profile["penetration_m"]:
                    contacts.append(
                        {
                            "pose": f"sample-{pose_index}",
                            "depth_m": float(-contact.dist),
                            "position": contact.pos.tolist(),
                            "geom1": int(contact.geom1),
                            "geom2": int(contact.geom2),
                        }
                    )
    results.append(
        result(
            "consumer.kinematics",
            worst_position <= profile["position_atol"] and worst_rotation <= profile["rotation_atol"],
            expected=expected | frame_names,
            checked=set(names) | sites_checked,
            details={
                "position_error_m": worst_position,
                "rotation_matrix_error": worst_rotation,
                "samples": samples,
                "base_poses": base_poses,
            },
        )
    )
    results.append(
        result(
            "consumer.dynamics",
            all(item["passed"] for item in dynamics),
            details={
                "oracle": "URDF rigid-body Jacobians; full mass matrix and zero-velocity gravity forces",
                "samples": dynamics,
                "atol": profile["dynamics_atol"],
                "rtol": profile["dynamics_rtol"],
            },
        )
    )
    scene_addresses = {}
    for name in moving:
        index = mujoco.mj_name2id(scene, mujoco.mjtObj.mjOBJ_JOINT, name)
        if index >= 0:
            scene_addresses[name] = int(scene.jnt_qposadr[index])
    if declared:
        # real scene, so ground contact counts, and only at declared poses
        scene_data = mujoco.MjData(scene)
        for pose in declared:
            _apply_pose(mujoco, scene, scene_data, scene_addresses, pose)
            mujoco.mj_forward(scene, scene_data)
            for contact in scene_data.contact[: scene_data.ncon]:
                if contact.dist < -profile["penetration_m"]:
                    contacts.append(
                        {
                            "pose": pose["name"],
                            "depth_m": float(-contact.dist),
                            "position": contact.pos.tolist(),
                            "geom1": int(contact.geom1),
                            "geom2": int(contact.geom2),
                            "geom1_name": mujoco.mj_id2name(scene, mujoco.mjtObj.mjOBJ_GEOM, int(contact.geom1)),
                            "geom2_name": mujoco.mj_id2name(scene, mujoco.mjtObj.mjOBJ_GEOM, int(contact.geom2)),
                        }
                    )
    contacts_status = None
    if poses_error is not None:
        contacts_status = None
    elif not declared and profile["purpose"] == "kinematics":
        contacts_status = "not_applicable"
    results.append(
        result(
            "consumer.contacts",
            poses_error is None and not contacts,
            details={
                "violations": contacts,
                "max_penetration_m": profile["penetration_m"],
                "policy": "declared_poses" if declared else "all_samples",
                "poses_checked": [pose["name"] for pose in declared] if declared else len(samples),
                "fk_samples": len(samples),
                "error": poses_error,
            },
            status=contacts_status,
        )
    )
    interface_details = interfaces(data, model, mujoco)
    results.append(
        result("consumer.interfaces", all(item["passed"] for item in interface_details), details=interface_details)
    )
    if data["control"] or profile["purpose"] in {"training", "hardware"}:
        control = control_evidence(data, model, mujoco, root)
        results.append(result("consumer.control", control["passed"], details=control))
    contact_policy = profile.get("contact")
    contact_details = []
    for index in range(scene.ngeom):
        if not (scene.geom_contype[index] or scene.geom_conaffinity[index]):
            continue
        valid = bool(contact_policy)
        if contact_policy:
            valid = all(_close(getattr(scene, "geom_" + name)[index], value) for name, value in contact_policy.items())
        contact_details.append({"object": mujoco.mj_id2name(scene, mujoco.mjtObj.mjOBJ_GEOM, index), "passed": valid})
    results.append(
        result(
            "consumer.contact_parameters",
            bool(contact_policy) and all(item["passed"] for item in contact_details),
            status="not_applicable" if profile["purpose"] == "kinematics" and not contact_policy else None,
            details={"declared": contact_policy, "objects": contact_details},
        )
    )
    scene_valid = _close(model.opt.gravity, profile["gravity"]) and _close(scene.opt.gravity, profile["gravity"])
    scene_valid &= _close(model.opt.timestep, profile["timestep"]) and _close(scene.opt.timestep, profile["timestep"])
    scene_valid &= model.nbody == scene.nbody and model.njnt == scene.njnt and model.nu == scene.nu
    for field in ("body_mass", "body_inertia", "body_iquat", "body_pos", "body_quat", "jnt_axis", "jnt_range"):
        left, right = getattr(model, field), getattr(scene, field)
        scene_valid &= left.shape == right.shape and _close(left, right)
    scene_valid &= scene.ngeom == model.ngeom + int(profile["ground"])
    if profile["ground"]:
        plane = mujoco.mj_name2id(scene, mujoco.mjtObj.mjOBJ_GEOM, "ground")
        scene_valid &= plane >= 0 and scene.geom_type[plane] == mujoco.mjtGeom.mjGEOM_PLANE
    results.append(result("consumer.scene", bool(scene_valid)))
    runtime = mujoco.MjData(scene)
    reset_source = "default_qpos"
    start_min_contact_m = None
    if declared:
        # a floating robot at the default all-zero qpos starts below the ground plane
        _apply_pose(mujoco, scene, runtime, scene_addresses, declared[0])
        reset_source = f"validation_pose:{declared[0]['name']}"
        mujoco.mj_forward(scene, runtime)
        start_min_contact_m = min((float(item.dist) for item in runtime.contact[: runtime.ncon]), default=0.0)
    warnings_before = runtime.warning.number.copy()
    for _ in range(profile.get("steps", 100)):
        mujoco.mj_step(scene, runtime)
    stable = bool(
        np.isfinite(runtime.qpos).all()
        and np.isfinite(runtime.qvel).all()
        and np.all(runtime.warning.number == warnings_before)
    )
    if declared:
        # a start pose that already penetrates is not a valid reset state
        stable &= start_min_contact_m is not None and start_min_contact_m >= -profile["penetration_m"]
    if poses_error is not None:
        stable = False
    results.append(
        result(
            "consumer.reset_step",
            stable,
            details={
                "steps": profile.get("steps", 100),
                "finite": stable,
                "pose_source": reset_source,
                "start_min_contact_m": start_min_contact_m,
                "max_penetration_m": profile["penetration_m"],
                "error": poses_error,
                "warning_counts": runtime.warning.number.tolist(),
            },
        )
    )
    return results


def _declared_joint_names(root: Path) -> list[str] | None:
    """台账里声明的关节名；工作区没有这份台账时返回 ``None``，由 URDF208 判死。

    ``config/joint_names.yaml`` 是交付布局与 URDF208 的共同声明项：工作区带这份台账时，检查的
    就是台账本身，所以一份已经和 URDF 脱节的清单会让资格判定失败，而不是被无声忽略。没有台账
    时**不能**退回"模型自己声明的关节"；缺失即 `URDF208` error，`model init` 会生成
    带空清单的模板。
    """

    path = root / "config/joint_names.yaml"
    if path.is_file():
        return joint_ledger.load(path)
    return None


def inspect(root: Path, profile: dict) -> list[dict]:
    data = read_data(confined(root, "model/robot.json"))
    Robot.from_dict(data)
    urdf = parser.load_urdf(root / "urdf/robot.urdf")
    results = _xml_roundtrip(data, urdf, root)
    mapping = {
        key: {item["id"]: item["name"] for item in data[key]}
        for key in ("links", "joints", "frames", "actuators", "sensors")
    }
    consumer = {key: data[key] for key in ("frames", "actuators", "sensors", "control", "contact_excludes")}
    results.append(
        result(
            "bundle.interfaces",
            read_data(root / "model/mapping.json") == mapping
            and read_data(root / "config/consumer.json") == {**consumer, "profile": profile},
        )
    )
    context = rules.Context(
        root=root,
        urdf=urdf,
        mjcf=parser.load_mjcf(root / "mjcf/robot.xml"),
        joint_ledger=_declared_joint_names(root),
        massless_links={link["name"] for link in data["links"] if link["inertial"] is None}
        | {frame["name"] for frame in data["frames"]},
        template=False,
        mujoco=False,
        require_collision=profile["purpose"] != "kinematics",
        # 网格缩放与交付审计同一条边界：单位换算属于导出阶段，非单位缩放两个工具都拒绝。
        require_unit_scale=True,
        require_mirror_symmetry=bool(data["provenance"].get("mirror_symmetry_required", False)),
        # The full-tensor oracle below replaces the legacy eigenvalue/axis approximation.
        uniform_density_links=set(),
    )
    if profile["purpose"] == "kinematics":
        results.append(
            result(
                "simulation.collision_policy",
                True,
                status="not_applicable",
                details="Kinematic qualification does not attest contact behavior",
            )
        )
    density_evidence = uniform_density_evidence(root, data, profile)
    density_summary = summarize(density_evidence)
    results.append(
        result(
            "physics.uniform_density_oracle",
            density_summary["ok"],
            status="not_applicable" if not density_summary["applicable"] else None,
            expected=density_summary["applicable"],
            checked=density_summary["passed"] + density_summary["failed"],
            details={
                "coverage": density_summary,
                "links": density_evidence,
            },
        )
    )
    ledger = waivers.load(root / "config/urdf_quality.json")
    for message in ledger.errors:
        results.append(result("URDF701", False, details=message))
    kept, waived, _unused = waivers.apply(
        rules.run(context), ledger, date.today(), not_evaluated=rules.not_evaluated(context)
    )
    for finding in kept:
        results.append(result(finding.code, finding.severity not in {"error", "warning"}, details=finding.as_dict()))
    for waived_record in waived:
        results.append(result("waiver.applied", True, details=waived_record))
    results.extend(_consumer(data, urdf, root, profile))
    return results
