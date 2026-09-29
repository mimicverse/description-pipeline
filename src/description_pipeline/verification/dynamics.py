"""Independent rigid-body Jacobian oracle for effective mass and gravity forces."""

from __future__ import annotations

import numpy as np

from .urdf_quality import model as parser


def dynamics_evidence(urdf, poses: dict, model, state, mujoco, profile: dict) -> dict:
    moving = {joint.name: joint for joint in urdf.joints.values() if joint.moveable}
    expected = set(moving)
    floating = profile["root_mode"] == "floating"
    if floating:
        expected.add("root_free_joint")
    actual = {mujoco.mj_id2name(model, mujoco.mjtObj.mjOBJ_JOINT, index) for index in range(model.njnt)}
    if actual != expected or model.nv != len(moving) + (6 if floating else 0):
        return {
            "passed": False,
            "reason": "Unexpected generalized coordinates",
            "expected": sorted(expected),
            "actual": sorted(actual),
        }
    columns = {
        name: int(model.jnt_dofadr[mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_JOINT, name)]) for name in moving
    }
    parents = {joint.child: joint for joint in urdf.joints.values()}
    root = next(name for name in urdf.links if name not in parents)
    base_column = 0
    if floating:
        index = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_JOINT, "root_free_joint")
        if model.jnt_type[index] != mujoco.mjtJoint.mjJNT_FREE:
            return {"passed": False, "reason": "Floating base is not a free joint"}
        base_column = int(model.jnt_dofadr[index])
    mass = np.zeros((model.nv, model.nv))
    gravity = np.zeros(model.nv)
    for name, body in urdf.links.items():
        inertia = body.inertial
        if inertia is None:
            continue
        transform = poses[name]
        com = transform[:3, 3] + transform[:3, :3] @ np.asarray(inertia.origin)
        rotation = transform[:3, :3] @ np.asarray(parser.rpy_matrix(inertia.rpy))
        tensor = rotation @ np.asarray(inertia.matrix()) @ rotation.T
        linear = np.zeros((3, model.nv))
        angular = np.zeros((3, model.nv))
        if floating:
            linear[:, base_column : base_column + 3] = np.eye(3)
            for component in range(3):
                axis = poses[root][:3, component]
                column = base_column + 3 + component
                linear[:, column] = np.cross(axis, com - poses[root][:3, 3])
                angular[:, column] = axis
        current = name
        while current in parents:
            joint = parents[current]
            if joint.moveable:
                axis = poses[joint.child][:3, :3] @ np.asarray(joint.axis)
                column = columns[joint.name]
                if joint.type == "prismatic":
                    linear[:, column] = axis
                else:
                    linear[:, column] = np.cross(axis, com - poses[joint.child][:3, 3])
                    angular[:, column] = axis
            current = joint.parent
        mass += inertia.mass * linear.T @ linear + angular.T @ tensor @ angular
        gravity -= inertia.mass * linear.T @ np.asarray(profile["gravity"])
    effective = np.empty_like(mass)
    mujoco.mj_fullM(model, state, effective)
    tolerance = {"atol": profile["dynamics_atol"], "rtol": profile["dynamics_rtol"]}
    passed = bool(np.allclose(mass, effective, **tolerance) and np.allclose(gravity, state.qfrc_bias, **tolerance))
    evidence = {
        "passed": passed,
        "mass_matrix_max_abs_error": float(np.max(np.abs(mass - effective), initial=0)),
        "gravity_force_max_abs_error": float(np.max(np.abs(gravity - state.qfrc_bias), initial=0)),
    }
    if not passed:
        evidence.update(
            expected_mass=mass.tolist(),
            actual_mass=effective.tolist(),
            expected_gravity=gravity.tolist(),
            actual_gravity=state.qfrc_bias.tolist(),
        )
    return evidence
