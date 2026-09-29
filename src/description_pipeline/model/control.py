"""Scalar SI control channels; authored order, polarity and offset stay explicit."""

from __future__ import annotations

import math

from ..io import PipelineError

SENSOR_UNITS = {
    "gyro": "rad/s",
    "accelerometer": "m/s^2",
    "framequat": "1",
    "framepos": "m",
    "force": "N",
    "torque": "N*m",
}


def validate_control(data: dict) -> None:
    control = data["control"]
    if set(control) != {"action_order", "actions", "observation_order", "observations"}:
        raise PipelineError("Control requires explicit action/observation order and channel mappings")
    joints = {item["name"]: item for item in data["joints"]}
    actuators = {item["name"]: item for item in data["actuators"]}
    sensors = {item["name"]: item for item in data["sensors"]}
    for direction in ("action", "observation"):
        order, mapping = control[direction + "_order"], control[direction + "s"]
        if (
            not isinstance(order, list)
            or not order
            or not all(isinstance(name, str) for name in order)
            or not isinstance(mapping, dict)
            or len(order) != len(set(order))
            or set(order) != set(mapping)
        ):
            raise PipelineError(f"Incomplete or duplicate {direction} channels")
        for name, channel in mapping.items():
            fields = {"unit", "polarity", "offset", "evidence"}
            if direction == "observation":
                fields |= {"source", "target", "component"}
            if not isinstance(channel, dict) or set(channel) != fields:
                raise PipelineError(f"Unsupported {direction} mapping: {name}")
            if (
                type(channel["polarity"]) is not int
                or channel["polarity"] not in {-1, 1}
                or type(channel["offset"]) not in (int, float)
                or not math.isfinite(channel["offset"])
                or not isinstance(channel["evidence"], str)
                or not channel["evidence"]
            ):
                raise PipelineError(f"Channel requires finite offset, signed polarity and evidence: {name}")
            if direction == "action":
                if name not in actuators:
                    raise PipelineError(f"Unknown action actuator: {name}")
                joint = joints[actuators[name]["joint"]]
                unit = "N" if joint["type"] == "prismatic" else "N*m"
            else:
                target, source, component = channel["target"], channel["source"], channel["component"]
                if type(component) is not int:
                    raise PipelineError(f"Observation component must be an integer: {name}")
                if (
                    source in {"joint_position", "joint_velocity"}
                    and target in joints
                    and joints[target]["type"] != "fixed"
                    and component == 0
                ):
                    unit = "m" if joints[target]["type"] == "prismatic" else "rad"
                    if source == "joint_velocity":
                        unit += "/s"
                elif (
                    source == "sensor"
                    and target in sensors
                    and 0 <= component < (4 if sensors[target]["type"] == "framequat" else 3)
                ):
                    unit = SENSOR_UNITS[sensors[target]["type"]]
                else:
                    raise PipelineError(f"Unsupported observation source or component: {name}")
            if channel["unit"] != unit:
                raise PipelineError(f"Channel {name} requires SI unit {unit}")
    if set(control["actions"]) != set(actuators):
        raise PipelineError("Action mapping must cover every actuator exactly once")


def map_actions(data: dict, values) -> list[float]:
    """Consumer command to MuJoCo control: polarity * command + offset, without clipping."""
    validate_control(data)
    control = data["control"]
    if len(values) != len(control["action_order"]) or not all(math.isfinite(float(value)) for value in values):
        raise PipelineError("Action vector shape or values do not match the declared interface")
    commands = {
        name: control["actions"][name]["polarity"] * float(value) + control["actions"][name]["offset"]
        for name, value in zip(control["action_order"], values, strict=True)
    }
    for actuator in data["actuators"]:
        low, high = actuator["control_range"]
        if not low <= commands[actuator["name"]] <= high:
            raise PipelineError(f"Action exceeds declared actuator range: {actuator['name']}")
    return [commands[item["name"]] for item in data["actuators"]]


def read_observations(data: dict, model, state, mujoco) -> list[float]:
    """Read the declared scalar channels from the actual consumer and apply calibration."""
    validate_control(data)
    control = data["control"]
    values = []
    for name in control["observation_order"]:
        channel = control["observations"][name]
        if channel["source"] == "sensor":
            index = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_SENSOR, channel["target"])
            if index < 0:
                raise PipelineError(f"Consumer is missing observation target: {name}")
            raw = state.sensordata[model.sensor_adr[index] + channel["component"]]
        else:
            index = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_JOINT, channel["target"])
            if index < 0:
                raise PipelineError(f"Consumer is missing observation target: {name}")
            raw = (
                state.qpos[model.jnt_qposadr[index]]
                if channel["source"] == "joint_position"
                else state.qvel[model.jnt_dofadr[index]]
            )
        value = channel["polarity"] * (float(raw) - channel["offset"])
        if not math.isfinite(value):
            raise PipelineError(f"Nonfinite consumer observation: {name}")
        values.append(value)
    return values
