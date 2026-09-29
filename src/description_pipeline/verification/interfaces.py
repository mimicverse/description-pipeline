"""Read effective MuJoCo interface targets and coefficients, not merely their names."""

import numpy as np


def interfaces(data: dict, model, mujoco) -> list[dict]:
    evidence = []
    for field, kind, count in (
        ("actuators", mujoco.mjtObj.mjOBJ_ACTUATOR, model.nu),
        ("sensors", mujoco.mjtObj.mjOBJ_SENSOR, model.nsensor),
    ):
        expected = [item["name"] for item in data[field]]
        actual = [mujoco.mj_id2name(model, kind, index) for index in range(count)]
        evidence.append({"object": field, "passed": expected == actual, "expected": expected, "actual": actual})
    for item in data["actuators"]:
        index = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_ACTUATOR, item["name"])
        if index < 0:
            continue
        target = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_JOINT, item["joint"])
        valid = (
            model.actuator_trntype[index] == mujoco.mjtTrn.mjTRN_JOINT
            and model.actuator_trnid[index, 0] == target
            and np.allclose(model.actuator_gear[index], [item["gear"], 0, 0, 0, 0, 0])
            and model.actuator_ctrllimited[index]
            and np.allclose(model.actuator_ctrlrange[index], item["control_range"])
            and model.actuator_dyntype[index] == mujoco.mjtDyn.mjDYN_NONE
            and model.actuator_gaintype[index] == mujoco.mjtGain.mjGAIN_FIXED
            and model.actuator_biastype[index] == mujoco.mjtBias.mjBIAS_NONE
            and np.isclose(model.actuator_gainprm[index, 0], 1)
        )
        evidence.append({"object": item["name"], "passed": bool(valid), "joint": item["joint"]})
    kinds = {
        name: getattr(mujoco.mjtSensor, "mjSENS_" + name.upper())
        for name in ("gyro", "accelerometer", "framequat", "framepos", "force", "torque")
    }
    for item in data["sensors"]:
        index = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_SENSOR, item["name"])
        if index < 0:
            continue
        target = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_SITE, item["frame"])
        valid = (
            model.sensor_type[index] == kinds[item["type"]]
            and model.sensor_objtype[index] == mujoco.mjtObj.mjOBJ_SITE
            and model.sensor_objid[index] == target
            and model.sensor_dim[index] == (4 if item["type"] == "framequat" else 3)
        )
        evidence.append({"object": item["name"], "passed": bool(valid), "frame": item["frame"]})
    expected_excludes = set()
    for pair in data["contact_excludes"]:
        ids = sorted(mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_BODY, pair[key]) for key in ("body1", "body2"))
        expected_excludes.add((ids[0] << 16) + ids[1])
    evidence.append({"object": "contact_excludes", "passed": expected_excludes == set(model.exclude_signature)})
    mimics = [joint for joint in data["joints"] if joint.get("mimic")]
    valid = model.neq == len(mimics)
    for index, joint in enumerate(mimics[: model.neq]):
        mimic = joint["mimic"]
        valid &= (
            model.eq_type[index] == mujoco.mjtEq.mjEQ_JOINT
            and bool(model.eq_active0[index])
            and model.eq_obj1id[index] == mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_JOINT, joint["name"])
            and model.eq_obj2id[index] == mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_JOINT, mimic["joint"])
            and np.allclose(model.eq_data[index, :5], [mimic["offset"], mimic["multiplier"], 0, 0, 0])
        )
    evidence.append({"object": "mimic_equalities", "passed": bool(valid)})
    return evidence
