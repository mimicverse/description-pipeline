"""Exercise authored scalar control mappings against the compiled consumer."""

import numpy as np

from ..io import PipelineError, confined, file_digest
from ..model.control import map_actions, read_observations, validate_control


def _expected_observations(control, model, state, mujoco) -> list[float]:
    """Decode raw engine arrays independently of the consumer mapping function."""
    values = {}
    for name, channel in control["observations"].items():
        if channel["source"] == "sensor":
            index = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_SENSOR, channel["target"])
            if index < 0:
                raise PipelineError(f"Missing sensor: {channel['target']}")
            value = state.sensordata[int(model.sensor_adr[index]) + channel["component"]]
        else:
            index = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_JOINT, channel["target"])
            if index < 0:
                raise PipelineError(f"Missing joint: {channel['target']}")
            value = (
                state.qpos[int(model.jnt_qposadr[index])]
                if channel["source"] == "joint_position"
                else state.qvel[int(model.jnt_dofadr[index])]
            )
        values[name] = (float(value) - channel["offset"]) * channel["polarity"]
    return [values[name] for name in control["observation_order"]]


def control_evidence(data: dict, model, mujoco, root) -> dict:
    try:
        validate_control(data)
        control = data["control"]
        evidence = {
            channel["evidence"]: file_digest(confined(root, channel["evidence"]))
            for direction in ("actions", "observations")
            for channel in control[direction].values()
        }
        state = mujoco.MjData(model)
        mujoco.mj_resetData(model, state)
        for index, joint in enumerate(data["joints"]):
            if joint["type"] == "fixed":
                continue
            target = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_JOINT, joint["name"])
            if target < 0:
                raise PipelineError(f"Missing joint: {joint['name']}")
            limits = joint.get("limits", {})
            low, high = limits.get("lower", -0.2), limits.get("upper", 0.2)
            state.qpos[int(model.jnt_qposadr[target])] = low + 0.37 * (high - low)
            state.qvel[int(model.jnt_dofadr[target])] = 0.003 * (index + 1)
        actuators = {item["name"]: item for item in data["actuators"]}
        commands = []
        expected_commands = np.zeros(model.nu)
        for index, name in enumerate(control["action_order"]):
            low, high = actuators[name]["control_range"]
            channel = control["actions"][name]
            target = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_ACTUATOR, name)
            if target < 0:
                raise PipelineError(f"Missing actuator: {name}")
            expected_commands[target] = low + (index + 1) / (len(actuators) + 2) * (high - low)
            commands.append((expected_commands[target] - channel["offset"]) / channel["polarity"])
        state.ctrl[:] = map_actions(data, commands)
        mapping_valid = np.allclose(state.ctrl, expected_commands, atol=1e-12, rtol=1e-12)
        mujoco.mj_forward(model, state)
        before = read_observations(data, model, state, mujoco)
        mapping_valid &= np.allclose(
            before, _expected_observations(control, model, state, mujoco), atol=1e-12, rtol=1e-12
        )
        mujoco.mj_step(model, state)
        mujoco.mj_forward(model, state)
        after = read_observations(data, model, state, mujoco)
        mapping_valid &= np.allclose(
            after, _expected_observations(control, model, state, mujoco), atol=1e-12, rtol=1e-12
        )
        return {
            "passed": bool(
                mapping_valid
                and np.isfinite(state.qpos).all()
                and np.isfinite(state.qvel).all()
                and not state.warning.number.any()
            ),
            "action_order": control["action_order"],
            "observation_order": control["observation_order"],
            "commands": commands,
            "expected_controls": expected_commands.tolist(),
            "observations_before": before,
            "observations_after": after,
            "evidence": evidence,
        }
    except (PipelineError, KeyError, ValueError, TypeError) as error:
        return {"passed": False, "reason": str(error)}
