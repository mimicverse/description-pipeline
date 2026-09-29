#!/usr/bin/env python3
"""Run a declared MuJoCo *simulation* acceptance against one immutable model bundle.

    description model accept --root MODEL --profile simulation --out RESULT

Only ``purpose: simulation`` is supported: this entry never claims training or hardware
qualification. Local records qualify only after fresh deterministic replay. The
bundle is read-only input; nothing inside it is executed.

The bundle is re-qualified with the pipeline oracle (``build.assess``) before a single
step is simulated, so a stale, hand-edited or non-simulation bundle cannot produce a
record.  The only pending check tolerated here is ``consumer.application``: it is
exactly the application evidence that a simulation record exists to feed, and the same
single check is what lets the pending candidate of a ``build`` run be measured before the
attestation exists.  The simulated entry is the consumer scene the oracle qualifies
(``mjcf/scene.xml``, never a model path of the subject's choosing), and it is parsed
through its whole ``<include>`` closure - absolute, escaping, missing or repeated
includes, and a plugin/extension in any included file, are refused *before* any engine
loads the model.

Every declared test then runs a real MuJoCo simulation with a PD torque loop on the
model's **motor** actuators, so the reported tracking error, torque and contact facts
come from the run, not from a copy of the inputs.  Every physics step is checked for
finiteness, penetration and joint range *in the state the step ends in* - ``mj_step``
solves its contacts before integrating, so the position-dependent quantities are
refreshed with ``mj_fwdPosition`` before they are judged - and any MuJoCo warning fails
the run.  Telemetry names the exact instant its state readings were taken.  Tracking
thresholds apply to the driven joint of a ``joint_sine`` test and to the worst joint of a
``hold`` test; every joint is reported individually.  Results land in a *new* output
directory: an existing directory is never overwritten, and a rejected run keeps its
partial telemetry in a unique ``<out>.failed-*`` diagnostic instead.

Configuration: ``config/simulation-acceptance.json`` (schema
``description.simulation-acceptance/v1``) declares the complete initial base/joint state,
SI PD gains, per-joint torque limits, the control period, and the hold/joint-sine tests
with their thresholds.  Unknown fields, non-finite numbers, out-of-range parameters,
empty test lists and zero-length tests are rejected.  ``max_saturation_steps_ratio`` is
optional: torque saturation (``|required| > cap``) is reported as a metric - the share of
control steps in which any joint needed more than its cap, plus every joint's own share -
and only fails a test when the author declares such a bound.  Telemetry is stored per
test as deterministic gzip (``docs/acceptance/<id>.jsonl.gz``, ``mtime=0``) and bound by
the digest of exactly those bytes, so a long run stays inside the attestation artifact
budget without relaxing a single hard check.
"""

from __future__ import annotations

import argparse
import gzip
import json
import math
import os
import platform
import posixpath
import re
import shutil
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path, PureWindowsPath
from typing import Any, NoReturn
from xml.etree import ElementTree

import mujoco
import numpy as np

from description_pipeline import __version__
from description_pipeline.build import assess, profile_for
from description_pipeline.io import (
    PipelineError,
    confined,
    file_digest,
    pin_utf8_streams,
    read_data,
    write_json,
)

CONFIG_SCHEMA = "description.simulation-acceptance/v1"
RECORD_SCHEMA = "description.acceptance/v2"
KINDS = ("hold", "joint_sine")
TRACKING_KEYS = ("tracking_rmse_rad", "max_tracking_error_rad")
CONTROLLER_ID = "pd-torque/v1"
ENVIRONMENT_KEYS = ("mujoco", "python", "numpy", "platform", "controller")
# Threshold key -> (low, high).  ``max_saturation_steps_ratio`` may be 0 because
# saturation is reported, not forbidden: the entry clamps the command at the declared
# cap, so an author bounds it explicitly when a saturated step is not acceptable.
THRESHOLDS = {
    "tracking_rmse_rad": (1e-12, math.pi),
    "max_tracking_error_rad": (1e-12, math.pi),
    "max_torque_nm": (1e-12, 1e4),
    "max_saturation_steps_ratio": (0.0, 1.0),
    "max_base_tilt_deg": (1e-12, 180.0),
    "penetration_tol_m": (1e-12, 0.1),
}
IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")
MAX_DURATION_S = 60.0
MAX_SUITE_TESTS = 256
MAX_PHYSICS_STEPS = 1_000_000
MAX_TELEMETRY_ROWS = 250_000
ACCEPTANCE_CONFIG = "config/simulation-acceptance.json"
# The consumer scene the pipeline oracle compiles and checks; this entry measures that
# exact artifact, never a path the subject chooses for itself.
CONSUMER_MJCF = "mjcf/scene.xml"
# The single check a measurement may still have pending: application acceptance.
PENDING_CONSUMER = ["consumer.application"]


class AcceptanceError(PipelineError):
    """Rejected input or unmet requirement, with a stable ``code`` for tests."""

    def __init__(self, code: str, message: str, detail: Any = None) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.detail = detail
        # Telemetry measured before the rejection; the caller keeps it as evidence.
        self.telemetry: list[dict] = []


def _fail(code: str, message: str, detail: Any = None) -> NoReturn:
    raise AcceptanceError(code, message, detail)


# --- strict configuration loading -----------------------------------------


def _strict_pairs(pairs: list[tuple[str, Any]]) -> dict:
    payload: dict[str, Any] = {}
    for key, value in pairs:
        if key in payload:
            _fail("config_duplicate_key", f"duplicate key {key!r} in configuration", {"key": key})
        payload[key] = value
    return payload


def _reject_constant(name: str) -> None:
    _fail("config_non_finite", f"non-finite number {name!r} in configuration", {"value": name})


def load_config(path: Path) -> dict:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        _fail("config_unreadable", f"cannot read acceptance configuration {path}", {"error": str(error)})
    try:
        payload = json.loads(text, object_pairs_hook=_strict_pairs, parse_constant=_reject_constant)
    except AcceptanceError:
        raise
    except json.JSONDecodeError as error:
        _fail("config_invalid_json", f"configuration is not JSON: {error}", {"path": str(path)})
    return _validate_config(payload)


def _mapping(value: Any, where: str) -> dict:
    if not isinstance(value, dict):
        _fail("config_type", f"{where} must be an object", {"where": where, "got": type(value).__name__})
    return value


def _unknown(payload: dict, allowed: set[str], where: str) -> None:
    extra = sorted(set(payload) - allowed)
    if extra:
        _fail("config_unknown_field", f"{where} has unknown fields", {"where": where, "unknown": extra})


def _number(value: Any, where: str, *, low: float | None = None, high: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail("config_type", f"{where} must be a number", {"where": where, "value": value})
    number = float(value)
    if not math.isfinite(number):
        _fail("config_non_finite", f"{where} must be finite", {"where": where, "value": value})
    if low is not None and number < low:
        _fail("config_out_of_range", f"{where} must be >= {low}", {"where": where, "value": number})
    if high is not None and number > high:
        _fail("config_out_of_range", f"{where} must be <= {high}", {"where": where, "value": number})
    return number


def _vector(value: Any, where: str, size: int) -> list[float]:
    if not isinstance(value, list) or len(value) != size:
        _fail("config_type", f"{where} must be a list of {size} numbers", {"where": where, "value": value})
    return [_number(item, f"{where}[{index}]") for index, item in enumerate(value)]


def _identifier(value: Any, where: str) -> str:
    if not isinstance(value, str) or not IDENTIFIER.match(value):
        _fail("config_identifier", f"{where} must be a safe identifier", {"where": where, "value": value})
    return value


def _joint_map(value: Any, where: str, keys: tuple[str, ...]) -> dict[str, dict[str, float]]:
    entries = _mapping(value or {}, where)
    resolved: dict[str, dict[str, float]] = {}
    for name, entry in entries.items():
        _identifier(name, f"{where}.{name}")
        payload = _mapping(entry, f"{where}.{name}")
        _unknown(payload, set(keys), f"{where}.{name}")
        missing = sorted(set(keys) - set(payload))
        if missing:
            _fail("config_missing_field", f"{where}.{name} lacks {missing}", {"where": where, "missing": missing})
        resolved[name] = {key: _number(payload[key], f"{where}.{name}.{key}", low=0.0) for key in keys}
    return resolved


def _validate_tests(value: Any, joints: set[str]) -> list[dict]:
    if not isinstance(value, list) or not value:
        _fail("config_tests_empty", "tests must be a non-empty list", {"tests": value})
    if len(value) > MAX_SUITE_TESTS:
        _fail("config_workload", "acceptance suite exceeds the test count limit")
    tests: list[dict] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        where = f"tests[{index}]"
        payload = _mapping(item, where)
        _unknown(
            payload,
            {"id", "kind", "duration_s", "joint", "amplitude_rad", "frequency_hz", "support", "thresholds"},
            where,
        )
        test_id = _identifier(payload.get("id"), f"{where}.id")
        if test_id in seen:
            _fail("config_duplicate_test", f"duplicate test id {test_id}", {"id": test_id})
        seen.add(test_id)
        kind = payload.get("kind")
        if kind not in KINDS:
            _fail("config_test_kind", f"{where}.kind must be one of {list(KINDS)}", {"kind": kind})
        duration = _number(payload.get("duration_s"), f"{where}.duration_s", low=1e-9, high=MAX_DURATION_S)
        thresholds = _mapping(payload.get("thresholds") or {}, f"{where}.thresholds")
        _unknown(thresholds, set(THRESHOLDS), f"{where}.thresholds")
        values = {
            key: _number(thresholds[key], f"{where}.thresholds.{key}", low=THRESHOLDS[key][0], high=THRESHOLDS[key][1])
            for key in thresholds
        }
        missing_tracking = [key for key in TRACKING_KEYS if key not in values]
        if missing_tracking:
            _fail(
                "config_threshold_missing",
                f"{where}.thresholds must declare {list(TRACKING_KEYS)}",
                {"id": test_id, "missing": missing_tracking},
            )
        test: dict[str, Any] = {"id": test_id, "kind": kind, "duration_s": duration, "thresholds": values}
        if kind == "joint_sine":
            joint = _identifier(payload.get("joint"), f"{where}.joint")
            if joint not in joints:
                _fail("config_joint_unknown", f"{where}.joint is not an actuated joint", {"joint": joint})
            test["joint"] = joint
            test["amplitude_rad"] = _number(
                payload.get("amplitude_rad"), f"{where}.amplitude_rad", low=1e-9, high=math.pi
            )
            test["frequency_hz"] = _number(payload.get("frequency_hz"), f"{where}.frequency_hz", low=1e-9, high=10.0)
        elif "joint" in payload or "amplitude_rad" in payload or "frequency_hz" in payload:
            _fail("config_test_field", f"{where} carries sine-only fields", {"id": test_id})
        support = payload.get("support")
        if support is not None:
            if kind != "hold":
                _fail("config_support_kind", f"{where}.support is only supported for hold tests", {"id": test_id})
            entry = _mapping(support, f"{where}.support")
            _unknown(entry, {"ground", "min_contact_steps_ratio", "min_base_height_m"}, f"{where}.support")
            if entry.get("ground") is not True:
                _fail("config_support_ground", f"{where}.support.ground must be true", {"id": test_id})
            test["support"] = {
                "ground": True,
                "min_contact_steps_ratio": _number(
                    entry.get("min_contact_steps_ratio", 0.9),
                    f"{where}.support.min_contact_steps_ratio",
                    low=0.0,
                    high=1.0,
                ),
                "min_base_height_m": _number(
                    entry.get("min_base_height_m", 0.0), f"{where}.support.min_base_height_m", low=0.0
                ),
            }
        tests.append(test)
    return tests


def _validate_config(payload: Any) -> dict:
    root = _mapping(payload, "config")
    _unknown(
        root,
        {
            "schema",
            "purpose",
            "model",
            "timestep_s",
            "control_period_s",
            "initial_state",
            "pd",
            "torque_limits",
            "tests",
        },
        "config",
    )
    if root.get("schema") != CONFIG_SCHEMA:
        _fail("config_schema", "unsupported acceptance configuration schema", {"schema": root.get("schema")})
    if root.get("purpose") != "simulation":
        _fail("config_purpose", "only purpose=simulation is supported", {"purpose": root.get("purpose")})
    model = _mapping(root.get("model") or {}, "config.model")
    _unknown(model, {"mjcf"}, "config.model")
    # The measured artifact is the consumer scene the oracle qualifies; a configuration
    # may name it explicitly but never point the run at a different model file.
    mjcf = model.get("mjcf", CONSUMER_MJCF)
    if mjcf != CONSUMER_MJCF:
        _fail(
            "config_model_path",
            f"model.mjcf must be the qualified consumer scene {CONSUMER_MJCF!r}",
            {"mjcf": mjcf, "expected": CONSUMER_MJCF},
        )
    timestep = _number(root.get("timestep_s"), "config.timestep_s", low=1e-6, high=0.1)
    period = _number(root.get("control_period_s"), "config.control_period_s", low=1e-6, high=1.0)
    ratio = period / timestep
    substeps = round(ratio)
    if substeps < 1 or abs(ratio - substeps) > 1e-9 * max(1.0, abs(ratio)):
        _fail(
            "config_period_ratio",
            "control_period_s must be an integer multiple of timestep_s",
            {"control_period_s": period, "timestep_s": timestep, "ratio": ratio},
        )
    state = _mapping(root.get("initial_state"), "config.initial_state")
    _unknown(state, {"base", "joints"}, "config.initial_state")
    base = state.get("base")
    initial_base: dict[str, list[float]] | None = None
    if base is not None:
        entry = _mapping(base, "config.initial_state.base")
        _unknown(entry, {"position", "quaternion"}, "config.initial_state.base")
        position = _vector(entry.get("position"), "config.initial_state.base.position", 3)
        quaternion = _vector(entry.get("quaternion"), "config.initial_state.base.quaternion", 4)
        if abs(max(abs(value) for value in quaternion) - 1.0) > 1e-6:
            _fail("config_quaternion", "base quaternion must be unit length", {"quaternion": quaternion})
        initial_base = {"position": position, "quaternion": quaternion}
    joints = _mapping(state.get("joints"), "config.initial_state.joints")
    initial_joints = {
        _identifier(name, "config.initial_state.joints"): _number(value, f"config.initial_state.joints.{name}")
        for name, value in joints.items()
    }
    if not initial_joints:
        _fail("config_state_empty", "initial_state.joints must not be empty")
    pd = _mapping(root.get("pd"), "config.pd")
    _unknown(pd, {"default", "joints"}, "config.pd")
    default_gain = _mapping(pd.get("default"), "config.pd.default")
    _unknown(default_gain, {"kp", "kd"}, "config.pd.default")
    gain_default = {key: _number(default_gain.get(key), f"config.pd.default.{key}", low=0.0) for key in ("kp", "kd")}
    gains = _joint_map(pd.get("joints", {}), "config.pd.joints", ("kp", "kd"))
    if gain_default["kp"] <= 0.0 and gain_default["kd"] <= 0.0 and not gains:
        _fail("config_pd_zero", "pd.default must not be zero when no joint override is given")
    limits = _mapping(root.get("torque_limits"), "config.torque_limits")
    _unknown(limits, {"default", "joints"}, "config.torque_limits")
    limit_default = _number(limits.get("default"), "config.torque_limits.default", low=1e-12)
    limit_joints = _joint_map(limits.get("joints", {}), "config.torque_limits.joints", ("effort",))
    resolved_limits = {name: entry["effort"] for name, entry in limit_joints.items()}
    tests = _validate_tests(root.get("tests"), set(initial_joints))
    rows = sum(math.ceil(test["duration_s"] / period) for test in tests)
    if rows > MAX_TELEMETRY_ROWS or rows * substeps > MAX_PHYSICS_STEPS:
        _fail("config_workload", "acceptance suite exceeds the simulation or telemetry budget")
    return {
        "mjcf": mjcf,
        "timestep_s": timestep,
        "control_period_s": period,
        "substeps": substeps,
        "initial_base": initial_base,
        "initial_joints": initial_joints,
        "pd_default": gain_default,
        "pd_joints": {name: dict(entry) for name, entry in gains.items()},
        "torque_default": limit_default,
        "torque_joints": resolved_limits,
        "tests": tests,
    }


def gains_for(config: dict, joint: str) -> dict[str, float]:
    return dict(config["pd_default"], **config["pd_joints"].get(joint, {}))


def limit_for(config: dict, joint: str) -> float:
    return float(config["torque_joints"].get(joint, config["torque_default"]))


# --- model inspection ------------------------------------------------------


def _scan_mjcf(root: Path, entry: str) -> list[str]:
    """Refuse model files that would pull executable extensions into the run.

    MuJoCo resolves ``<include file="...">`` relative to the *including* file, so the
    whole closure is walked iteratively: absolute or escaping paths, missing files,
    repeated files (cycles and diamonds) and plugins/extensions in any included file
    are refused.  Returns the visited bundle-relative MJCF files in traversal order.
    """

    pending = [entry]
    visited: set[str] = set()
    order: list[str] = []
    while pending:
        relative = pending.pop()
        if relative in visited:
            _fail("model_include_cycle", "include closure repeats a model file", {"mjcf": relative})
        visited.add(relative)
        order.append(relative)
        try:
            path = confined(root, relative)
        except PipelineError as error:
            _fail("model_unreadable", f"cannot open MJCF {relative}", {"mjcf": relative, "error": str(error)})
        try:
            tree = ElementTree.parse(path)
        except (OSError, ElementTree.ParseError) as error:
            _fail("model_unreadable", f"cannot parse MJCF {relative}: {error}", {"mjcf": relative})
        for node in tree.iter():
            tag = node.tag.rsplit("}", 1)[-1]
            if tag in {"plugin", "extension"} or any("plugin" in key for key in node.attrib):
                _fail(
                    "model_plugin",
                    "model declares plugins/extensions; this entry never loads executable model code",
                    {"mjcf": relative},
                )
            if tag != "include":
                continue
            target = node.get("file")
            if (
                not isinstance(target, str)
                or not target
                or posixpath.isabs(target)
                or PureWindowsPath(target).drive
                or "\\" in target
            ):
                _fail(
                    "model_include_escape",
                    "model include must be a relative bundle path",
                    {"mjcf": relative, "file": target},
                )
            joined = posixpath.normpath(posixpath.join(posixpath.dirname(relative), target))
            if joined in {"", ".."} or joined.startswith("../"):
                _fail(
                    "model_include_escape",
                    "model include leaves the bundle",
                    {"mjcf": relative, "file": target},
                )
            try:
                confined(root, joined)
            except PipelineError as error:
                _fail(
                    "model_include_missing",
                    "included model file is not a file inside the bundle",
                    {"mjcf": relative, "file": target, "error": str(error)},
                )
            pending.append(joined)
    return order


class Actuator:
    __slots__ = ("ctrl_high", "ctrl_low", "gear", "hard_limit_nm", "index", "joint", "name", "ranges")

    def __init__(
        self,
        index: int,
        joint: str,
        name: str,
        gear: float,
        ctrl: tuple[float, float],
        hard_limit_nm: float | None,
        ranges: dict[str, list[float]],
    ) -> None:
        self.index = index
        self.joint = joint
        self.name = name
        self.gear = gear
        self.ctrl_low = ctrl[0]
        self.ctrl_high = ctrl[1]
        self.hard_limit_nm = hard_limit_nm
        self.ranges = ranges


def _symmetric_limit(bounds: list[float]) -> float:
    """Largest symmetric |τ| a declared interval [low, high] can carry."""

    return min(-bounds[0], bounds[1])


def inspect_model(model: mujoco.MjModel, config: dict) -> dict:
    if abs(model.opt.timestep - config["timestep_s"]) > 1e-12:
        _fail(
            "model_timestep",
            "model timestep differs from the declared value",
            {"model": model.opt.timestep, "config": config["timestep_s"]},
        )
    actuators: list[Actuator] = []
    seen: set[str] = set()
    for index in range(model.nu):
        if model.actuator_trntype[index] != mujoco.mjtTrn.mjTRN_JOINT:
            _fail("model_actuator_type", "only joint-transmission motor actuators are supported", {"actuator": index})
        if (
            model.actuator_gaintype[index] != mujoco.mjtGain.mjGAIN_FIXED
            or model.actuator_biastype[index] != mujoco.mjtBias.mjBIAS_NONE
            or float(model.actuator_gainprm[index, 0]) != 1.0
        ):
            _fail(
                "model_actuator_kind",
                "actuators must be plain torque motors (unit fixed gain, no bias)",
                {"actuator": index},
            )
        if model.actuator_dyntype[index] != mujoco.mjtDyn.mjDYN_NONE:
            _fail(
                "model_actuator_dynamics",
                "actuator dynamics would decouple the commanded control from the applied torque",
                {"actuator": index},
            )
        joint_id = int(model.actuator_trnid[index, 0])
        if model.jnt_type[joint_id] != mujoco.mjtJoint.mjJNT_HINGE:
            _fail("model_joint_type", "only hinge joints are supported", {"actuator": index})
        gear = float(model.actuator_gear[index, 0])
        if gear != 1.0:
            _fail(
                "model_gear_unsupported",
                "this entry only supports motor actuators with gear=1 (units stay N·m)",
                {"actuator": index, "gear": gear},
            )
        name = mujoco.mj_id2name(model, mujoco.mjtObj.mjOBJ_ACTUATOR, index) or f"actuator-{index}"
        joint = mujoco.mj_id2name(model, mujoco.mjtObj.mjOBJ_JOINT, joint_id) or f"joint-{joint_id}"
        if joint in seen:
            _fail("model_actuator_duplicate", "each joint must be driven by exactly one actuator", {"joint": joint})
        seen.add(joint)
        # Every declared range must carry the full declared cap: the ctrl range limits
        # the command, the two force ranges limit what the model may apply.
        model_ranges: dict[str, list[float]] = {}
        if model.actuator_ctrllimited[index]:
            model_ranges["actuator_ctrlrange"] = [float(value) for value in model.actuator_ctrlrange[index]]
        if model.actuator_forcelimited[index]:
            model_ranges["actuator_forcerange"] = [float(value) for value in model.actuator_forcerange[index]]
        if model.jnt_actfrclimited[joint_id]:
            model_ranges["joint_actuatorfrcrange"] = [float(value) for value in model.jnt_actfrcrange[joint_id]]
        hard_limit = min(_symmetric_limit(bounds) for bounds in model_ranges.values()) if model_ranges else None
        ctrl = (-float("inf"), float("inf"))
        if model.actuator_ctrllimited[index]:
            ctrl = (float(model.actuator_ctrlrange[index, 0]), float(model.actuator_ctrlrange[index, 1]))
        actuators.append(Actuator(index, joint, name, gear, ctrl, hard_limit, model_ranges))
    if not actuators:
        _fail("model_no_actuators", "model has no actuators; a simulation acceptance cannot run")
    model_joints = {item.joint for item in actuators}
    declared = set(config["initial_joints"])
    if model_joints != declared:
        _fail(
            "model_joint_coverage",
            "declared joints and model actuators differ",
            {"missing": sorted(model_joints - declared), "extra": sorted(declared - model_joints)},
        )
    for item in actuators:
        cap = limit_for(config, item.joint)
        if item.hard_limit_nm is not None and cap > item.hard_limit_nm * (1.0 + 1e-9):
            _fail(
                "config_torque_cap_exceeds_model",
                "declared torque cap does not fit the model's declared ranges",
                {
                    "joint": item.joint,
                    "cap_nm": cap,
                    "model_limit_nm": item.hard_limit_nm,
                    "model_ranges": item.ranges,
                },
            )
    free_joint = any(model.jnt_type[index] == mujoco.mjtJoint.mjJNT_FREE for index in range(model.njnt))
    if free_joint and config["initial_base"] is None:
        _fail("model_base_state", "model has a free joint; initial_state.base is required")
    if not free_joint and config["initial_base"] is not None:
        _fail("model_base_state", "initial_state.base given but the model has no free joint")
    collidable = [
        index for index in range(model.ngeom) if model.geom_contype[index] != 0 or model.geom_conaffinity[index] != 0
    ]
    ground = [index for index in collidable if model.geom_bodyid[index] == 0]
    ranges: dict[str, tuple[float, float] | None] = {}
    for item in actuators:
        joint_id = int(model.actuator_trnid[item.index, 0])
        ranges[item.joint] = (
            (float(model.jnt_range[joint_id, 0]), float(model.jnt_range[joint_id, 1]))
            if model.jnt_limited[joint_id]
            else None
        )
    return {
        "actuators": actuators,
        "free_joint": free_joint,
        "collidable_geoms": collidable,
        "ground_geoms": ground,
        "ranges": ranges,
    }


def _joint_addresses(model: mujoco.MjModel, actuators: list[Actuator]) -> dict[str, tuple[int, int]]:
    addresses: dict[str, tuple[int, int]] = {}
    for item in actuators:
        joint_id = int(model.actuator_trnid[item.index, 0])
        addresses[item.joint] = (int(model.jnt_qposadr[joint_id]), int(model.jnt_dofadr[joint_id]))
    return addresses


# --- one test run ----------------------------------------------------------


def _target_for(test: dict, joint: str, base: float, time_s: float) -> float:
    if test["kind"] == "joint_sine" and test["joint"] == joint:
        return base + test["amplitude_rad"] * math.sin(2.0 * math.pi * test["frequency_hz"] * time_s)
    return base


def _world_contact_count(data: mujoco.MjData, ground_geoms: list[int]) -> int:
    ground = set(ground_geoms)
    return sum(
        1 for contact in data.contact[: data.ncon] if int(contact.geom1) in ground or int(contact.geom2) in ground
    )


def _contact_counts(data: mujoco.MjData, ground_geoms: list[int]) -> tuple[int, int]:
    """(contacts with world ground, other contacts) at the current state.

    The two are independent: a step that holds the robot up *and* presses two links
    together is both a support step and a self-contact step.
    """

    world = _world_contact_count(data, ground_geoms)
    return world, int(data.ncon) - world


def _warnings(data: mujoco.MjData) -> dict[str, int]:
    return {mujoco.mjtWarning(index).name: int(count) for index, count in enumerate(data.warning.number) if int(count)}


def _check_warnings(data: mujoco.MjData, where: dict) -> None:
    """MuJoCo warnings mean the numbers below them are not trustworthy evidence."""

    found = _warnings(data)
    if found:
        _fail("model_warning", "MuJoCo reported a warning during the run", {**where, "warnings": found})


def _step_once(
    model: mujoco.MjModel,
    data: mujoco.MjData,
    config: dict,
    test: dict,
    info: dict,
    addresses: dict[str, tuple[int, int]],
    where: dict,
) -> None:
    """Advance every physics step, checking the hard facts of the state it ends in.

    ``mj_step`` runs its position stage - including collision - *before* it integrates,
    so right after the call ``data.contact`` and the global poses still describe the
    start of the step while ``qpos``/``qvel`` are already at its end.  Refreshing the
    position-dependent quantities with ``mj_fwdPosition`` makes every check below (and
    every telemetry row) describe one and the same instant, and it cannot perturb the
    trajectory: the next ``mj_step`` recomputes exactly these arrays itself.
    """

    penetration_tol = float(test["thresholds"].get("penetration_tol_m", 1e-3))
    for substep in range(config["substeps"]):
        mujoco.mj_step(model, data)
        moment = {**where, "substep": substep}
        if not (bool(np.isfinite(data.qpos).all()) and bool(np.isfinite(data.qvel).all())):
            _fail("state_not_finite", "simulation state became non-finite", moment)
        _check_warnings(data, moment)
        mujoco.mj_fwdPosition(model, data)
        _check_warnings(data, moment)
        worst = min((float(contact.dist) for contact in data.contact[: data.ncon]), default=0.0)
        if worst < -penetration_tol:
            _fail(
                "penetration",
                "contact penetration exceeded the declared tolerance during the run",
                {**moment, "min_contact_m": worst, "tolerance_m": penetration_tol},
            )
        for item in info["actuators"]:
            jnt_range = info["ranges"].get(item.joint)
            if jnt_range is None:
                continue
            value = float(data.qpos[addresses[item.joint][0]])
            if not (jnt_range[0] - 1e-9 <= value <= jnt_range[1] + 1e-9):
                _fail(
                    "joint_range_exceeded",
                    "joint left its model range during the run",
                    {**moment, "joint": item.joint, "value": value, "range": list(jnt_range)},
                )


def run_test(model: mujoco.MjModel, config: dict, test: dict, info: dict) -> dict:
    """Run one declared test; a rejection keeps the telemetry up to that instant."""

    telemetry: list[dict] = []
    try:
        return _measure_test(model, config, test, info, telemetry)
    except AcceptanceError as error:
        error.telemetry = telemetry
        raise


def _measure_test(model: mujoco.MjModel, config: dict, test: dict, info: dict, telemetry: list[dict]) -> dict:
    data = mujoco.MjData(model)
    addresses = _joint_addresses(model, info["actuators"])
    for item in info["actuators"]:
        base = config["initial_joints"][item.joint]
        jnt_range = info["ranges"].get(item.joint)
        if jnt_range is not None and not (jnt_range[0] <= base <= jnt_range[1]):
            _fail(
                "state_out_of_range",
                "initial joint value is outside the model range",
                {"joint": item.joint, "value": base, "range": list(jnt_range)},
            )
        if test["kind"] == "joint_sine" and test.get("joint") == item.joint and jnt_range is not None:
            for value in (base - test["amplitude_rad"], base + test["amplitude_rad"]):
                if not (jnt_range[0] <= value <= jnt_range[1]):
                    _fail(
                        "test_out_of_range",
                        "sine amplitude leaves the model range",
                        {"joint": item.joint, "value": value, "range": list(jnt_range)},
                    )
    if config["initial_base"] is not None:
        free_id = next(int(index) for index in range(model.njnt) if model.jnt_type[index] == mujoco.mjtJoint.mjJNT_FREE)
        address = int(model.jnt_qposadr[free_id])
        data.qpos[address : address + 3] = config["initial_base"]["position"]
        data.qpos[address + 3 : address + 7] = config["initial_base"]["quaternion"]
    for item in info["actuators"]:
        qpos_adr, _ = addresses[item.joint]
        data.qpos[qpos_adr] = config["initial_joints"][item.joint]
    mujoco.mj_forward(model, data)
    _check_warnings(data, {"test": test["id"], "phase": "initial_state"})

    penetration_tol = float(test["thresholds"].get("penetration_tol_m", 1e-3))
    worst_initial = min((float(contact.dist) for contact in data.contact[: data.ncon]), default=0.0)
    if worst_initial < -penetration_tol:
        _fail(
            "initial_penetration",
            "initial state already penetrates the environment",
            {"worst_contact_m": worst_initial, "tolerance_m": penetration_tol},
        )

    steps = max(1, round(test["duration_s"] / config["control_period_s"]))
    errors: dict[str, list[float]] = {item.joint: [] for item in info["actuators"]}
    final = np.zeros(0)
    ground_steps = 0
    self_contact_steps = 0
    saturated_joints = 0
    saturated_steps = 0
    joint_limit = {item.joint: limit_for(config, item.joint) for item in info["actuators"]}
    saturated: dict[str, int] = dict.fromkeys(joint_limit, 0)
    max_command = 0.0
    max_required = 0.0
    max_applied = 0.0
    for step in range(steps):
        start_s = step * config["control_period_s"]
        end_s = start_s + config["control_period_s"]
        row: dict[str, Any] = {
            "t": round(end_s, 9),
            "t_command": round(start_s, 9),
            "q": {},
            "qd": {},
            "q_target": {},
            "tau": {},
            "tau_applied": {},
            "tau_required": {},
        }
        saturated_any = False
        for item in info["actuators"]:
            qpos_adr, dof_adr = addresses[item.joint]
            q = float(data.qpos[qpos_adr])
            qd = float(data.qvel[dof_adr])
            target = _target_for(test, item.joint, config["initial_joints"][item.joint], start_s)
            gains = gains_for(config, item.joint)
            required = gains["kp"] * (target - q) - gains["kd"] * qd
            limit = joint_limit[item.joint]
            tau = min(max(required, -limit), limit)
            if item.ctrl_low != -float("inf") or item.ctrl_high != float("inf"):
                tau = min(max(tau, item.ctrl_low), item.ctrl_high)
            data.ctrl[item.index] = tau
            row["q_target"][item.joint] = round(target, 9)
            row["tau"][item.joint] = round(tau, 9)
            row["tau_required"][item.joint] = round(required, 9)
            # Tracked at the sampling instant the command was computed from, so the
            # error and the torque it required describe the same measurement.
            errors[item.joint].append(abs(target - q))
            max_command = max(max_command, abs(tau))
            max_required = max(max_required, abs(required))
            if abs(required) > joint_limit[item.joint] * (1.0 + 1e-9):
                # Saturation is measured, not forbidden: the command is clamped at the
                # declared cap, so the test reports how often the requirement needed more.
                saturated_joints += 1
                saturated[item.joint] += 1
                saturated_any = True
        if saturated_any:
            saturated_steps += 1
        _step_once(
            model,
            data,
            config,
            test,
            info,
            addresses,
            {"test": test["id"], "step": step, "time_s": round(end_s, 9)},
        )
        # Readings belong to the end of the interval the row names, and the applied
        # torque keeps its sign so the recorded direction stays auditable.
        for item in info["actuators"]:
            qpos_adr, dof_adr = addresses[item.joint]
            row["q"][item.joint] = round(float(data.qpos[qpos_adr]), 9)
            row["qd"][item.joint] = round(float(data.qvel[dof_adr]), 9)
            limit = limit_for(config, item.joint)
            applied = float(data.actuator_force[item.index])
            row["tau_applied"][item.joint] = round(applied, 9)
            max_applied = max(max_applied, abs(applied))
            if not math.isfinite(applied) or abs(applied) > limit * (1.0 + 1e-6):
                _fail(
                    "torque_over_limit",
                    "applied actuator force exceeds the declared limit",
                    {"joint": item.joint, "force_nm": applied, "limit_nm": limit, "step": step},
                )
        final = np.concatenate([data.qpos, data.qvel])
        row["ncon"] = int(data.ncon)
        row["min_contact_m"] = round(min((float(c.dist) for c in data.contact[: data.ncon]), default=0.0), 9)
        world_contacts, self_contacts = _contact_counts(data, info["ground_geoms"])
        if world_contacts > 0:
            ground_steps += 1
        if self_contacts > 0:
            self_contact_steps += 1
        if config["initial_base"] is not None:
            row["base_position"] = [round(float(value), 9) for value in data.qpos[:3]]
            row["base_quaternion"] = [round(float(value), 9) for value in data.qpos[3:7]]
        telemetry.append(row)
    if not math.isfinite(float(np.abs(final).sum())):
        _fail("state_not_finite", "final simulation state is not finite")

    per_joint = {
        joint: {
            "tracking_rmse_rad": round(float(np.sqrt(np.mean(np.square(values)))), 9),
            "max_tracking_error_rad": round(max(values, default=0.0), 9),
            "saturation_steps_ratio": round(saturated[joint] / steps, 9),
        }
        for joint, values in errors.items()
    }
    # A sine test is judged on the joint it drives; a hold test is judged on every joint,
    # so no static joint can dilute the one that moves.
    tracked = [test["joint"]] if test["kind"] == "joint_sine" else sorted(per_joint)
    metrics = {
        "steps": steps,
        "tracked_joints": tracked,
        "tracking_rmse_rad": round(max(per_joint[joint]["tracking_rmse_rad"] for joint in tracked), 9),
        "max_tracking_error_rad": round(max(per_joint[joint]["max_tracking_error_rad"] for joint in tracked), 9),
        "worst_joint_tracking_rmse_rad": round(max(item["tracking_rmse_rad"] for item in per_joint.values()), 9),
        "per_joint": per_joint,
        # The threshold key reports what the model actually applied; the command and the
        # uncapped requirement stay visible as information for the same run.
        "max_torque_nm": round(max_applied, 9),
        "max_command_torque_nm": round(max_command, 9),
        "max_required_torque_nm": round(max_required, 9),
        # A step counts as saturated when *any* joint needed more than its cap, so one
        # joint's saturation is never diluted by the joints that were fine.
        "saturation_steps_ratio": round(saturated_steps / steps, 9),
        "saturation_joint_steps_ratio": round(saturated_joints / (steps * len(info["actuators"])), 9),
        "ground_contact_steps_ratio": round(ground_steps / steps, 9),
        "self_contact_steps_ratio": round(self_contact_steps / steps, 9),
        "min_base_height_m": None,
        "max_base_tilt_deg": None,
    }
    if config["initial_base"] is not None:
        heights = [row["base_position"][2] for row in telemetry]
        tilts = []
        for row in telemetry:
            w, x, y, z = row["base_quaternion"]
            up_z = float(w * w - x * x - y * y + z * z)
            tilts.append(math.degrees(math.acos(max(-1.0, min(1.0, up_z)))))
        metrics["min_base_height_m"] = round(min(heights, default=0.0), 9)
        metrics["max_base_tilt_deg"] = round(max(tilts, default=0.0), 9)
    support = test.get("support")
    if support is not None:
        if not info["ground_geoms"]:
            _fail(
                "support_not_verifiable",
                "support requested but the model has no collidable ground geoms",
                {"test": test["id"]},
            )
        if metrics["ground_contact_steps_ratio"] < support["min_contact_steps_ratio"]:
            _fail(
                "support_contact_missing",
                "declared support contact with the world ground was not held for enough steps",
                {
                    "ratio": metrics["ground_contact_steps_ratio"],
                    "required": support["min_contact_steps_ratio"],
                    "self_contact_ratio": metrics["self_contact_steps_ratio"],
                },
            )
        if metrics["min_base_height_m"] is None or metrics["min_base_height_m"] < support["min_base_height_m"]:
            _fail(
                "support_height",
                "base height fell below the declared support threshold",
                {"min_base_height_m": metrics["min_base_height_m"], "required": support["min_base_height_m"]},
            )
    failures = []
    thresholds = test["thresholds"]
    if "tracking_rmse_rad" in thresholds and metrics["tracking_rmse_rad"] > thresholds["tracking_rmse_rad"]:
        failures.append(
            {
                "check": "tracking_rmse_rad",
                "value": metrics["tracking_rmse_rad"],
                "limit": thresholds["tracking_rmse_rad"],
            }
        )
    if (
        "max_tracking_error_rad" in thresholds
        and metrics["max_tracking_error_rad"] > thresholds["max_tracking_error_rad"]
    ):
        failures.append(
            {
                "check": "max_tracking_error_rad",
                "value": metrics["max_tracking_error_rad"],
                "limit": thresholds["max_tracking_error_rad"],
            }
        )
    if "max_torque_nm" in thresholds and metrics["max_torque_nm"] > thresholds["max_torque_nm"]:
        failures.append(
            {"check": "max_torque_nm", "value": metrics["max_torque_nm"], "limit": thresholds["max_torque_nm"]}
        )
    if (
        "max_saturation_steps_ratio" in thresholds
        and metrics["saturation_steps_ratio"] > thresholds["max_saturation_steps_ratio"]
    ):
        failures.append(
            {
                "check": "max_saturation_steps_ratio",
                "value": metrics["saturation_steps_ratio"],
                "limit": thresholds["max_saturation_steps_ratio"],
            }
        )
    if (
        "max_base_tilt_deg" in thresholds
        and metrics["max_base_tilt_deg"] is not None
        and metrics["max_base_tilt_deg"] > thresholds["max_base_tilt_deg"]
    ):
        failures.append(
            {
                "check": "max_base_tilt_deg",
                "value": metrics["max_base_tilt_deg"],
                "limit": thresholds["max_base_tilt_deg"],
            }
        )
    return {
        "id": test["id"],
        "kind": test["kind"],
        "passed": not failures,
        "metrics": metrics,
        "thresholds": thresholds,
        "failures": failures,
        "telemetry": telemetry,
    }


# --- environment, record, output ------------------------------------------


def measure_environment() -> dict:
    """The real runtime the tests ran in; never copied from the configuration."""

    return {
        "mujoco": str(mujoco.__version__),
        "python": platform.python_version(),
        "numpy": str(np.__version__),
        "platform": platform.platform(),
        "controller": CONTROLLER_ID,
    }


def check_environment(measured: dict, declared: Any) -> dict:
    declared_map = _mapping(declared, "profile.consumer_environment")
    if not declared_map:
        _fail(
            "profile_environment_empty",
            "profile.consumer_environment must declare the supported runtime keys",
            {"supported": list(ENVIRONMENT_KEYS)},
        )
    unknown = sorted(set(declared_map) - set(ENVIRONMENT_KEYS))
    if unknown:
        _fail(
            "profile_environment_unknown",
            "profile.consumer_environment has unsupported keys",
            {"unknown": unknown, "supported": list(ENVIRONMENT_KEYS)},
        )
    non_string = sorted(key for key, value in declared_map.items() if not isinstance(value, str))
    if non_string:
        _fail(
            "profile_environment_type",
            "profile.consumer_environment values must be strings so the record can match exactly",
            {"keys": non_string},
        )
    mismatched = {
        key: {"declared": declared_map[key], "measured": measured[key]}
        for key in declared_map
        if str(declared_map[key]) != str(measured[key])
    }
    if mismatched:
        _fail("profile_environment_mismatch", "declared consumer environment differs from the run", mismatched)
    return {key: measured[key] for key in declared_map}


def _pending_candidate(root: Path, report: dict) -> bool:
    """A ``build`` candidate whose committed report is exactly this pending verdict.

    ``build`` cannot publish a bundle whose only pending check is the external
    attestation, so it hands that exact candidate to the run that produces the evidence.
    Accepting it is bound to the value, not to the name: the committed report must be
    unpassed for this very subject and profile, with ``consumer.application`` as its only
    blocker.  Anything else stays a tampering failure.
    """

    try:
        committed = read_data(confined(root, "docs/quality.json"))
    except (PipelineError, OSError):
        return False
    return (
        isinstance(committed, dict)
        and committed.get("passed") is False
        and committed.get("blockers") == PENDING_CONSUMER
        and committed.get("subject") == report["subject"]
        and committed.get("profile_digest") == report["profile_digest"]
    )


def check_profile(root: Path, profile_name: str) -> dict:
    """The declared profile must exist and be a simulation profile; nothing else."""

    try:
        profile = profile_for(root, profile_name)
    except PipelineError as error:
        _fail("bundle_incomplete", "bundle has no usable profile", {"error": str(error), "root": str(root)})
    if profile.get("purpose") != "simulation":
        _fail("profile_purpose", "only purpose=simulation is supported", {"purpose": profile.get("purpose")})
    return profile


def check_bundle(root: Path, profile_name: str) -> tuple[dict, str]:
    """Re-qualify the bundle with the pipeline oracle, not with its own manifest.

    A published bundle is the output of ``build``: the manifest, the committed quality
    report and the subject binding are re-derived here from the frozen inputs, so a
    stale, hand-edited or non-simulation bundle cannot produce a record.  Every blocker
    is fatal except ``consumer.application``, the application acceptance this entry
    exists to feed, and the report binding of the pending candidate that carries it.
    Returns the assessed report and how the bundle is bound (published or pending).
    """

    try:
        report = assess(root, profile_name)
    except (PipelineError, OSError) as error:
        _fail("bundle_incomplete", "bundle cannot be re-derived from its frozen inputs", {"error": str(error)})
    blockers = report["blockers"]
    tolerated = set(PENDING_CONSUMER)
    binding = "published"
    if "bundle.report_binding" in blockers and _pending_candidate(root, report):
        tolerated.add("bundle.report_binding")
        binding = "pending_candidate"
    unexpected = [name for name in blockers if name not in tolerated]
    if any(name.startswith("bundle.") for name in unexpected):
        _fail("bundle_tampered", "bundle files do not match the subject its manifest declares", {"blockers": blockers})
    if unexpected:
        _fail("bundle_unqualified", "bundle does not qualify for a simulation acceptance", {"blockers": blockers})
    return report, binding


def check_suites(tests: list[dict], profile: dict) -> None:
    suites = profile.get("acceptance_suites")
    if not isinstance(suites, list) or not suites:
        _fail("profile_suites_empty", "profile.acceptance_suites must list the executed test ids")
    declared = [str(item) for item in suites]
    executed = [test["id"] for test in tests]
    if sorted(declared) != sorted(executed) or len(set(declared)) != len(declared):
        _fail(
            "profile_suites_mismatch",
            "profile.acceptance_suites must exactly cover the declared tests",
            {"declared": sorted(declared), "executed": sorted(executed)},
        )


def _config_file(root: Path, config_path: Path) -> Path:
    """Resolve the acceptance configuration as a bundle-relative artifact."""

    candidate = Path(config_path)
    if candidate.is_absolute():
        if not candidate.resolve().is_relative_to(root):
            _fail("config_outside_bundle", "configuration must live inside the bundle", {"config": str(candidate)})
        relative = candidate.resolve().relative_to(root).as_posix()
    else:
        relative = candidate.as_posix()
    try:
        config_file = confined(root, relative)
    except PipelineError as error:
        _fail("config_outside_bundle", "configuration path is not a bundle-relative artifact", {"error": str(error)})
    if not config_file.is_file():
        _fail("config_unreadable", "acceptance configuration is missing", {"config": str(config_path)})
    return config_file


def _preserve_failure(staging: Path, out: Path, error: AcceptanceError) -> Path:
    """Keep this run's diagnostic, uniquely; never leave a partial result CI could pick up."""

    out.parent.mkdir(parents=True, exist_ok=True)
    diagnostic = Path(tempfile.mkdtemp(prefix=f"{out.name}.failed-", dir=out.parent))
    if staging.exists():
        for entry in staging.iterdir():
            shutil.move(str(entry), str(diagnostic / entry.name))
        shutil.rmtree(staging)
    write_json(diagnostic / "failure.json", {"code": error.code, "message": str(error), "detail": error.detail})
    detail = error.detail if isinstance(error.detail, dict) else {"detail": error.detail}
    error.detail = {**detail, "diagnostic_path": str(diagnostic)}
    error.diagnostic_path = str(diagnostic)
    return diagnostic


def _write_telemetry(destination: Path, test_id: str, rows: list[dict]) -> str:
    """Store one test's telemetry as deterministic gzip and return its bundle-relative name.

    A long run over many joints would otherwise push the evidence past the attestation
    artifact budget.  The bytes are reproducible (``mtime`` is pinned to 0, no file name)
    and the record binds the digest of exactly these compressed bytes.
    """

    name = f"docs/acceptance/{test_id}.jsonl.gz"
    if rows:
        path = destination / name
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows)
        path.write_bytes(gzip.compress(payload.encode("utf-8"), mtime=0))
    return name


def run_tests(model: mujoco.MjModel, config: dict, info: dict, destination: Path) -> list[dict]:
    """Execute every declared test and store its telemetry under ``destination``."""

    results: list[dict] = []
    for test in config["tests"]:
        try:
            outcome = run_test(model, config, test, info)
        except AcceptanceError as error:
            # Keep what the run had measured when it was rejected; a failed test is a
            # diagnosis, not a dead end.
            _write_telemetry(destination, test["id"], error.telemetry)
            raise
        telemetry_name = _write_telemetry(destination, test["id"], outcome.pop("telemetry"))
        telemetry_path = destination / telemetry_name
        results.append(
            {
                "suite": test["id"],
                "kind": test["kind"],
                "suite_version": 1,
                "producer": "description_pipeline.verification.simulation",
                "executed_at": datetime.now(UTC).isoformat(timespec="seconds"),
                "passed": outcome["passed"],
                "evidence_class": "simulation",
                "data_role": "validation",
                "used_for_fitting": False,
                "conditions": {
                    "control_period_s": config["control_period_s"],
                    "timestep_s": config["timestep_s"],
                    "duration_s": test["duration_s"],
                    "torque_limits_nm": {item.joint: limit_for(config, item.joint) for item in info["actuators"]},
                    "pd": {item.joint: gains_for(config, item.joint) for item in info["actuators"]},
                },
                "metrics": outcome["metrics"],
                "thresholds": outcome["thresholds"],
                "failures": outcome["failures"],
                "artifacts": {telemetry_name: file_digest(telemetry_path)},
                "validation_data": {telemetry_name: file_digest(telemetry_path)},
            }
        )
    return results


def execute_tests(root: Path, profile: dict, config_path: Path, destination: Path) -> dict:
    """Measure a data-only scene; the caller independently verifies model derivation.

    This entry does not qualify a model; its caller must independently assess it.
    It never calls assess, so acceptance verification can replay the tests
    during an ordinary build/check without recursive qualification or a bypass flag.
    """
    scanned = _scan_mjcf(root, CONSUMER_MJCF)
    config_file = _config_file(root, config_path)
    if not config_file.is_relative_to(root / "config"):
        _fail("config_outside_inputs", "acceptance configuration must be under config/")
    config = load_config(config_file)
    check_suites(config["tests"], profile)
    measured = measure_environment()
    environment = check_environment(measured, profile.get("consumer_environment"))
    mjcf_path = confined(root, config["mjcf"])
    warnings: list[str] = []
    previous_warning = mujoco.get_mju_user_warning()
    mujoco.set_mju_user_warning(lambda message: warnings.append(str(message)))
    try:
        try:
            model = mujoco.MjModel.from_xml_path(str(mjcf_path))
        except (ValueError, RuntimeError) as error:
            _fail("model_load_failed", f"MuJoCo rejected the model: {error}", {"mjcf": config["mjcf"]})
        if warnings:
            _fail("model_warning", "MuJoCo warned while loading the model", {"warnings": list(warnings)})
        info = inspect_model(model, config)
        runtime = {"mujoco": mujoco.__version__, "ndof": int(model.nv), "nq": int(model.nq), "nu": int(model.nu)}
        results = run_tests(model, config, info, destination)
        if warnings:
            _fail("model_warning", "MuJoCo warned during the run", {"warnings": list(warnings)})
        return {
            "config": config,
            "config_file": config_file,
            "mjcf_path": mjcf_path,
            "includes": scanned,
            "environment": environment,
            "measured": measured,
            "runtime": runtime,
            "warnings": warnings,
            "results": results,
        }
    finally:
        mujoco.set_mju_user_warning(previous_warning)


def run_acceptance(root: Path, profile_name: str, config_path: Path, out_dir: Path, *, external: bool = False) -> dict:
    root = Path(root).resolve()
    out = Path(out_dir).resolve()
    if out == root or out.is_relative_to(root):
        _fail("output_inside_bundle", "output directory must be independent of the read-only bundle", {"out": str(out)})
    if out.exists():
        _fail("output_exists", "output directory already exists; a result is never overwritten", {"out": str(out)})
    check_profile(root, profile_name)
    if not external and _config_file(root, config_path) != confined(root, ACCEPTANCE_CONFIG):
        _fail("config_not_canonical", "local acceptance requires config/simulation-acceptance.json")
    # The plugin scan closes the consumer scene's <include> closure before any engine
    # sees it; only after that does the pipeline oracle load MuJoCo and the bundle get
    # trusted.
    _scan_mjcf(root, CONSUMER_MJCF)
    report, binding = check_bundle(root, profile_name)
    out.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{out.name}.partial-", dir=out.parent))
    started = time.time()
    try:
        measured = execute_tests(root, report["profile"], config_path, staging)
        record = _record(
            report=report,
            binding=binding,
            root=root,
            started=started,
            **{key: value for key, value in measured.items() if key != "measured"},
        )
        if not external:
            record["attestation"] = {"kind": "local_replay", "schema_version": "description.local-replay/v1"}
            record["qualification"]["reason"] = (
                "Simulation-only measurements. Build/check must independently replay the declared tests "
                "before qualification; training and hardware remain unqualified."
            )
        from ..build import subject_files
        from ..io import digest

        if digest(subject_files(root)) != report["subject"]:
            _fail("inputs_changed", "model inputs changed during acceptance")
        write_json(staging / "docs/acceptance/simulation.json", record)
        write_json(staging / "acceptance.json", record)
        write_json(
            staging / "environment.json",
            {"measured": measured["measured"], "declared": measured["environment"], "runtime": measured["runtime"]},
        )
        (staging / "docs/acceptance/simulation.md").write_text(
            _markdown_summary(record, measured["config"]), encoding="utf-8", newline="\n"
        )
        os.replace(staging, out)
    except AcceptanceError as error:
        _preserve_failure(staging, out, error)
        raise
    return record


def complete_pending(root: Path, profile_name: str, report: dict) -> dict:
    """Complete declared simulation tests during an already locked model update.

    Only application acceptance may be pending. Other model failures never reach
    the runner, and failed experiments never replace the workspace's evidence.
    """
    from ..build import author_files, build

    before = author_files(root)
    parent = root / "build/acceptance"
    parent.mkdir(parents=True, exist_ok=True)
    out = Path(tempfile.mkdtemp(prefix="simulation-", dir=parent)) / "result"
    try:
        record = run_acceptance(Path(report["diagnostic_path"]), profile_name, Path(ACCEPTANCE_CONFIG), out)
    except AcceptanceError as error:
        detail = error.detail if isinstance(error.detail, dict) else {}
        diagnostic = Path(detail.get("diagnostic_path", out.parent))
        write_json(diagnostic / "failure.json", {"code": error.code, "message": str(error), "detail": error.detail})
        error.diagnostic_path = str(diagnostic)
        raise
    if not all(item["passed"] for item in record["results"]):
        return {**report, "diagnostic_path": str(out)}
    if author_files(root) != before:
        raise PipelineError("Author inputs changed during acceptance; rebuild before submitting")
    for path in (out / "docs/acceptance").iterdir():
        destination = confined(root, f"docs/acceptance/{path.name}", exists=False)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, destination)
    return build(root, profile_name)


def _record(
    *,
    report: dict,
    binding: str,
    config: dict,
    config_file: Path,
    root: Path,
    mjcf_path: Path,
    includes: list[str],
    runtime: dict,
    warnings: list[str],
    environment: dict,
    results: list[dict],
    started: float,
) -> dict:
    """Bind the measured results to the qualified subject, profile and oracle verdict."""

    record = {
        "schema_version": RECORD_SCHEMA,
        "purpose": "simulation",
        "subject": report["subject"],
        "profile_digest": report["profile_digest"],
        "environment": environment,
        "producer": "description_pipeline.verification.simulation",
        "executed_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "duration_s": round(time.time() - started, 3),
        "tool": {"package_version": __version__, "controller": CONTROLLER_ID, "identity": report["toolchain"]},
        "model": {
            "root": str(root),
            "mjcf": config["mjcf"],
            "mjcf_sha256": file_digest(mjcf_path),
            "mjcf_includes": includes,
            "config": config_file.relative_to(root).as_posix(),
            "config_sha256": file_digest(config_file),
            "subject_files": len(report["files"]),
            # The verdict of the pipeline oracle this record was measured under.
            "oracle": {
                "profile_name": report["profile_name"],
                "binding": binding,
                "pending": report["blockers"],
                "checks": {item["id"]: item["status"] for item in report["checks"]},
            },
        },
        "runtime": {**runtime, "python": platform.python_version(), "mujoco_warnings": warnings},
        "results": results,
        "qualification": {
            "simulation_measured": True,
            "physical_calibration": False,
            "training_qualified": False,
            "hardware_qualified": False,
            "release_qualified": False,
            "reason": "Simulation-only evidence without physical calibration or a trusted external attestation; "
            "consumer.application stays pending until a trusted CI artifact is bound.",
            "pending": ["consumer.application"],
        },
    }
    return record


def _markdown_summary(record: dict, config: dict) -> str:
    lines = [
        "# Simulation acceptance",
        "",
        f"* subject: `{record['subject']}`",
        f"* profile digest: `{record['profile_digest']}`",
        "* environment: " + ", ".join(f"{key}={value}" for key, value in record["environment"].items()),
        f"* control period: {config['control_period_s']} s"
        f"（timestep {config['timestep_s']} s，子步 {config['substeps']}）",
        f"* mujoco warnings: {len(record['runtime']['mujoco_warnings'])}",
        "",
        "| test | kind | passed | rmse (rad) | max err (rad) | max τ applied (N·m) | saturation | "
        "max τ required (N·m) | ground contact | self contact |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for item in record["results"]:
        metrics = item["metrics"]
        lines.append(
            f"| {item['suite']} | {item['kind']} | {'yes' if item['passed'] else 'NO'} | "
            f"{metrics['tracking_rmse_rad']} | {metrics['max_tracking_error_rad']} | "
            f"{metrics['max_torque_nm']} | {metrics['saturation_steps_ratio']} | "
            f"{metrics['max_required_torque_nm']} | "
            f"{metrics['ground_contact_steps_ratio']} | {metrics['self_contact_steps_ratio']} |"
        )
        lines.append(
            f"* {item['suite']}: tracked {', '.join(metrics['tracked_joints'])}; "
            f"worst joint RMSE {metrics['worst_joint_tracking_rmse_rad']} rad"
        )
    for item in record["results"]:
        for failure in item["failures"]:
            lines.append(f"* {item['suite']}: {failure['check']} = {failure['value']} > {failure['limit']}")
    lines += ["", record["qualification"]["reason"], ""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    pin_utf8_streams()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", type=Path, required=True, help="Model bundle (read-only)")
    parser.add_argument("--profile", default="simulation")
    parser.add_argument("--config", type=Path, default=Path("config/simulation-acceptance.json"))
    parser.add_argument("--out", type=Path, required=True, help="Independent output directory")
    parser.add_argument("--external", action="store_true", help="Produce evidence for an optional hosted attestation")
    args = parser.parse_args(argv)
    try:
        record = run_acceptance(args.root, args.profile, args.config, args.out, external=args.external)
    except AcceptanceError as error:
        print(
            json.dumps(
                {"ok": False, "code": error.code, "message": str(error), "detail": error.detail}, ensure_ascii=False
            )
        )
        return 2
    passed = bool(record["results"]) and all(item["passed"] for item in record["results"])
    print(
        json.dumps(
            {
                "ok": passed,
                "subject": record["subject"],
                "tests": {item["suite"]: item["passed"] for item in record["results"]},
                "release_qualified": record["qualification"]["release_qualified"],
                "output": str(Path(args.out)),
            },
            ensure_ascii=False,
        )
    )
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
