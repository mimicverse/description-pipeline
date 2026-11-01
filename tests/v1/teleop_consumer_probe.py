"""Exercise public consumer APIs in their actual isolated installed closures."""

import asyncio
import hashlib
import json
from pathlib import Path
import runpy
import sys
import time
import uuid

owned = runpy.run_path(sys.argv[2])
module_command, process_identity, terminate_owned = (
    owned[key] for key in ("module_command", "process_identity", "terminate_owned")
)


async def pico(config):
    from teleop.robots.m3_0.session_evidence import replay_session_evidence

    nonce = uuid.uuid4().hex
    process = await asyncio.create_subprocess_exec(
        *module_command(Path(sys.executable), "teleop.backends.mujoco.upper_body_worker", nonce),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
        limit=4 * 1024 * 1024,
    )
    identity = process_identity(process.pid, nonce)
    accepted, applied = {}, {}

    async def receive():
        raw = await asyncio.wait_for(process.stdout.readline(), 10)
        if not raw:
            raise ValueError("Worker output closed before the expected response")
        message = json.loads(raw)
        if message["type"] == "error":
            raise ValueError(str(message))
        return message

    try:
        process.stdin.write((json.dumps(config) + "\n").encode())
        await process.stdin.drain()
        ready = await receive()
        assert ready["type"] == "ready" and ready["session_id"] == config["session_id"], ready
        for sequence, kind in enumerate(("connection", "estop", "stop")):
            command = {
                "protocol": "teleop.upper-body.worker/1",
                "id": kind,
                "type": kind,
                "source_session_id": "consumer-probe",
                "sequence": sequence,
                "received_monotonic_ns": time.monotonic_ns(),
            }
            if kind == "connection":
                command["connected"] = False
            process.stdin.write((json.dumps(command) + "\n").encode())
            await process.stdin.drain()
            while kind not in applied:
                message = await receive()
                if message["type"] == "accepted":
                    assert message["applied"] is False, message
                    accepted[message["command_id"]] = message
                elif message["type"] == "applied":
                    assert message["accepted"] and message["applied"], message
                    applied[message["command_id"]] = message
        while (message := await receive())["type"] != "stopped":
            pass
        process.stdin.close()
        await asyncio.wait_for(process.wait(), 10)
        assert process.returncode == 0, await process.stderr.read()
    finally:
        assert await asyncio.to_thread(terminate_owned, identity), "Fixture worker group remained unresolved"
    record = Path(config["record_dir"])

    def inventory():
        return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in record.iterdir() if p.is_file()}

    before = inventory()
    original = Path(config["root"])
    relocated = original.with_name("relocated-source")
    original.rename(relocated)
    replay = replay_session_evidence(record, expected_command_ids=tuple(applied), source_root=relocated)
    assert replay["pass"], replay
    assert inventory() == before
    assert set(accepted) == set(applied) == {"connection", "estop", "stop"}
    return {"role": "pico", "commands": applied, "replay": replay, "archive_unchanged": True}


def assessment():
    import actuator_model
    import mujoco
    import numpy as np
    from teleop.evaluation.continuous_dynamics.actuator_model import create_actuator_model_runtime, load_public_api

    model = mujoco.MjModel.from_xml_string("""<mujoco><option timestep="0.005"/>
      <worldbody><body name="base"><body name="arm"><joint name="joint" type="hinge"/>
      <geom type="box" size="0.05 0.02 0.02" mass="0.1"/></body></body></worldbody>
      <actuator><motor name="motor" joint="joint" gear="1" ctrllimited="false"
      forcelimited="true" forcerange="-2 2"/></actuator></mujoco>""")
    data = mujoco.MjData(model)
    mujoco.mj_forward(model, data)
    package = actuator_model.package_info()
    config = {
        "package": package,
        "selector": {"actuator": "hd1910", "parameter_version": "pd_offset_v1", "friction_model": "m5"},
        "gains": {"kp_fw": 4, "kd_fw": 2},
        "ownership": {"armature": "actuator_model", "damping": "actuator_model", "frictionloss": "actuator_model"},
        "supply": {"voltage_v": 6.0, "min_voltage_v": 5.9, "drop_gain_v_per_nm": 0.0},
        "runtime": {"command_delay_steps": 2, "stiff_frictionloss": True},
    }
    runtime = create_actuator_model_runtime(
        config,
        model,
        data,
        joint_names=("joint",),
        actuator_names=("motor",),
        api=load_public_api(),
    )
    runtime.reset_and_prime(np.array([0.0]))
    commands = []
    for _ in range(8):
        update = runtime.update(np.array([0.2]))
        commands.append(update.commanded_target.tolist())
        assert np.isfinite(update.telemetry.motor_torque_nm).all()
        mujoco.mj_step(model, data)
    assert commands[:2] == [[0.0], [0.0]] and commands[2:] == [[0.2]] * 6, commands
    assert np.isfinite(data.qpos).all()
    return {
        "role": "assessment",
        "engine": mujoco.mj_versionString(),
        "package": package,
        "ticks": 8,
        "commands": commands,
        "qpos": data.qpos.tolist(),
    }


if __name__ == "__main__":
    result = asyncio.run(pico(json.load(sys.stdin))) if sys.argv[1] == "pico" else assessment()
    print(json.dumps(result))
