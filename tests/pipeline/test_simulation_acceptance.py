"""Simulation acceptance entry (tools/run_simulation_acceptance.py) regression.

Every end-to-end fixture is a real bundle: the pipeline freezes a fixture source,
normalizes it, generates and compiles the MuJoCo model, publishes the bundle, and the
entry then re-qualifies that bundle through ``assess``.  A hand-written manifest cannot
qualify a model, so no test fakes one.

The single input a local test cannot produce is the external attestation that turns
``consumer.application`` into a passing check, so publishing a simulation bundle patches
that one trust oracle; source freeze, normalization, generation, compilation, subject
binding and the acceptance run itself all execute for real.  Faults are injected into
the frozen scene, the acceptance configuration or the published files, and every one of
them must be rejected exactly once - silently passing is the failure this suite exists
to catch.
"""

from __future__ import annotations

import contextlib
import copy
import gzip
import io
import json
import platform
import runpy
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import mujoco
from description_pipeline.verification import simulation

ROOT = Path(__file__).resolve().parents[2]

from description_pipeline.build import build, freeze, profile_for, subject_files  # noqa: E402
from description_pipeline.io import PipelineError, digest, file_digest, write_json  # noqa: E402
from description_pipeline.repository import init_model  # noqa: E402
from description_pipeline.sources.snapshot import write_manifest  # noqa: E402
from tests.pipeline import ledger as joint_ledger  # noqa: E402

CONTACT = {
    "friction": [1.0, 0.005, 0.0001],
    "condim": 3,
    "solref": [0.02, 1.0],
    "solimp": [0.9, 0.95, 0.001, 0.5, 2.0],
    "margin": 0.0,
    "gap": 0.0,
}
SUITES = ("hold-fixture", "sine-fixture")
ENVIRONMENT = {
    "mujoco": mujoco.__version__,
    "python": platform.python_version(),
    "controller": "pd-torque/v1",
}
SIMULATION_PROFILE = {
    "root_mode": "floating",
    "ground": True,
    "contact": CONTACT,
    "acceptance_suites": list(SUITES),
    "consumer_environment": dict(ENVIRONMENT),
}


def scene(
    *,
    effort: float = 0.6,
    gear: float = 1.0,
    control_range: tuple[float, float] = (-1.0, 1.0),
    joint_range: tuple[float, float] = (-0.1, 0.2),
    motors: int = 1,
    hand: bool = False,
) -> dict:
    """A wide-based fixture robot on a floating base, resting on the ground.

    ``arm_joint`` is a yaw hinge that carries ``arm_link``; ``hand`` adds a second hinge
    with an offset centre of mass, which is what makes per-joint tracking visible.
    """

    base_geometry = {"kind": "box", "size": [0.3, 0.3, 0.06], "xyz": [0, 0, 0], "rpy": [0, 0, 0]}
    arm_geometry = {"kind": "box", "size": [0.2, 0.06, 0.06], "xyz": [0.1, 0, 0], "rpy": [0, 0, 0]}
    links = [
        {
            "id": "base_link",
            "name": "base_link",
            "inertial": {
                "mass": 1.0,
                "xyz": [0, 0, 0],
                "rpy": [0, 0, 0],
                "inertia": [0.0076, 0, 0, 0.0076, 0, 0.015],
            },
            "visuals": [copy.deepcopy(base_geometry)],
            "collisions": [copy.deepcopy(base_geometry)],
            "provenance": {"source_entities": ["base_link"]},
        },
        {
            "id": "arm_link",
            "name": "arm_link",
            "inertial": {
                "mass": 1.0,
                "xyz": [0.1, 0, 0],
                "rpy": [0, 0, 0],
                "inertia": [0.0003, 0, 0, 0.0034, 0, 0.0034],
            },
            "visuals": [copy.deepcopy(arm_geometry)],
            "collisions": [copy.deepcopy(arm_geometry)],
            "provenance": {"source_entities": ["arm_link"]},
        },
    ]
    joints = [
        {
            "id": "arm_joint",
            "name": "arm_joint",
            "type": "revolute",
            "parent": "base_link",
            "child": "arm_link",
            "xyz": [0, 0, 0.15],
            "rpy": [0, 0, 0],
            "axis": [0, 0, 1],
            "limits": {"lower": joint_range[0], "upper": joint_range[1], "effort": effort, "velocity": 3},
            "dynamics": {"damping": 0.01, "friction": 0},
            "provenance": {"reference": "analytic_fixture"},
        }
    ]
    actuators = [
        {
            "id": f"arm_{index}",
            "name": f"arm_{index}",
            "joint": "arm_joint",
            "type": "motor",
            "gear": gear,
            "control_range": list(control_range),
            "provenance": {},
        }
        for index in range(motors)
    ]
    if hand:
        links.append(
            {
                "id": "hand_link",
                "name": "hand_link",
                "inertial": {
                    "mass": 0.1,
                    "xyz": [0.05, 0, 0],
                    "rpy": [0, 0, 0],
                    "inertia": [0.00002, 0, 0, 0.0001, 0, 0.0001],
                },
                "visuals": [{"kind": "box", "size": [0.05, 0.02, 0.02], "xyz": [0.05, 0, 0], "rpy": [0, 0, 0]}],
                "collisions": [{"kind": "box", "size": [0.1, 0.04, 0.04], "xyz": [0.05, 0, 0], "rpy": [0, 0, 0]}],
                "provenance": {"source_entities": ["hand_link"]},
            }
        )
        joints.append(
            {
                "id": "hand_joint",
                "name": "hand_joint",
                "type": "revolute",
                "parent": "arm_link",
                "child": "hand_link",
                "xyz": [0.27, 0, 0],
                "rpy": [0, 0, 0],
                "axis": [0, 1, 0],
                "limits": {"lower": -2.0, "upper": 2.0, "effort": effort, "velocity": 3},
                "dynamics": {"damping": 0.001, "friction": 0},
                "provenance": {"reference": "analytic_fixture"},
            }
        )
        actuators.append(
            {
                "id": "hand_0",
                "name": "hand_0",
                "joint": "hand_joint",
                "type": "motor",
                "gear": gear,
                "control_range": list(control_range),
                "provenance": {},
            }
        )
    return {
        "schema_version": "description.scene/v1",
        "name": "robot",
        "units": "SI",
        "links": links,
        "joints": joints,
        "frames": [],
        "actuators": actuators,
        "sensors": [],
        "constraints": [],
        "control": {},
        "contact_excludes": [],
        "provenance": {
            "expected_entities": [link["id"] for link in links],
            "fixture": True,
        },
    }


def config_payload(**overrides) -> dict:
    payload = {
        "schema": "description.simulation-acceptance/v1",
        "purpose": "simulation",
        "timestep_s": 0.002,
        "control_period_s": 0.02,
        "initial_state": {
            "base": {"position": [0.0, 0.0, 0.03], "quaternion": [1.0, 0.0, 0.0, 0.0]},
            "joints": {"arm_joint": 0.0},
        },
        "pd": {"default": {"kp": 4.0, "kd": 0.3}},
        "torque_limits": {"default": 0.3},
        "tests": [
            {
                "id": "hold-fixture",
                "kind": "hold",
                "duration_s": 0.4,
                "support": {"ground": True, "min_contact_steps_ratio": 0.9, "min_base_height_m": 0.02},
                "thresholds": {
                    "tracking_rmse_rad": 0.02,
                    "max_tracking_error_rad": 0.05,
                    "max_base_tilt_deg": 20.0,
                },
            },
            {
                "id": "sine-fixture",
                "kind": "joint_sine",
                "joint": "arm_joint",
                "amplitude_rad": 0.05,
                "frequency_hz": 0.5,
                "duration_s": 0.4,
                "thresholds": {"tracking_rmse_rad": 0.02, "max_tracking_error_rad": 0.05},
            },
        ],
    }
    payload.update(overrides)
    return payload


class Workspace:
    """A frozen fixture workspace that publishes real simulation bundles."""

    def __init__(self, base: Path) -> None:
        self.base = base
        self.source = base / "source"
        self.root = base / "model"
        self.published = 0
        self.source.mkdir(parents=True)
        payload = scene()
        self.write_source(payload)
        init_model(self.root, "fixture", {"provider": "fixture", "path": str(self.source)})
        # `init_model` scaffolds the ledger empty; the fixture registers the joints it really builds.
        self.declare_ledger(payload)
        freeze(self.root)
        self.default_profile = json.loads((self.root / "config/profiles/simulation.json").read_text(encoding="utf-8"))
        self.profile(**SIMULATION_PROFILE)

    def declare_ledger(self, payload: dict) -> None:
        """Keep the delivery ledger in step with the scene; before ``init_model`` there is nowhere to write it."""

        if not (self.root / "config/robot.yaml").is_file():
            return
        joint_ledger.write(self.root, *(joint["name"] for joint in payload["joints"] if joint["type"] != "fixed"))

    def write_source(self, payload: dict) -> None:
        """Re-freeze the fixture source and drop stale snapshots so only the live one remains."""

        write_json(self.source / "scene.json", payload)
        write_manifest(
            self.source, kind="fixture", identity={"case": "simulation-acceptance"}, evidence_class="fixture"
        )
        # The ledger is delivery state, so it follows the scene the fixture just wrote.
        self.declare_ledger(payload)
        if not (self.root / "config/robot.yaml").exists():
            return
        keep = Path(freeze(self.root)["snapshot"]).name
        for snapshot in (self.root / "sources/snapshots").iterdir():
            if snapshot.name != keep:
                shutil.rmtree(snapshot)

    def profile(self, **overrides) -> dict:
        path = self.root / "config/profiles/simulation.json"
        payload = {**self.default_profile, **SIMULATION_PROFILE, **overrides}
        write_json(path, payload)
        return profile_for(self.root, "simulation")

    def write_config(self, config: dict | None = None) -> None:
        write_json(self.root / "config/simulation-acceptance.json", config or config_payload())

    def publish(self, config: dict | None = None, profile: dict | None = None, source: dict | None = None) -> Path:
        """Publish a simulation bundle; the seeded record stands in for the attestation."""

        if source is not None:
            self.write_source(source)
        resolved = self.profile(**(profile or {}))
        self.write_config(config)
        unqualified = build(self.root, "simulation")
        self.seed_record(unqualified["subject"], resolved)
        destination = self.base / f"bundle-{self.published}"
        self.published += 1
        with mock.patch(
            "description_pipeline.verification.acceptance.verify_external_record",
            return_value={"trusted": True, "status": "passed"},
        ):
            published = build(self.root, "simulation", destination=destination)
        if not published["passed"]:
            raise AssertionError(f"fixture bundle did not publish: {published['blockers']}")
        return destination

    def seed_record(self, subject: str, profile: dict) -> None:
        """Bind external simulation evidence to the subject the pipeline just derived."""

        results = []
        for suite in profile["acceptance_suites"]:
            log = self.root / f"docs/acceptance/{suite}.log"
            log.parent.mkdir(parents=True, exist_ok=True)
            log.write_text(f"external fixture evidence for {suite}\n", encoding="utf-8")
            name = f"docs/acceptance/{suite}.log"
            results.append(
                {
                    "suite": suite,
                    "suite_version": 1,
                    "producer": "fixture-runner",
                    "executed_at": "2026-09-20T00:00:00Z",
                    "passed": True,
                    "evidence_class": "simulation",
                    "data_role": "validation",
                    "used_for_fitting": False,
                    "conditions": {"scenario": "external fixture"},
                    "artifacts": {name: file_digest(log)},
                    "validation_data": {name: file_digest(log)},
                }
            )
        write_json(
            self.root / "docs/acceptance/simulation.json",
            {
                "schema_version": "description.acceptance/v2",
                "subject": subject,
                "profile_digest": digest(profile),
                "environment": profile["consumer_environment"],
                "results": results,
            },
        )

    def accept(self, bundle: Path, name: str = "result") -> dict:
        return simulation.run_acceptance(
            bundle, "simulation", Path("config/simulation-acceptance.json"), self.base / name
        )

    def reject(self, case: unittest.TestCase, code: str, name: str = "result", **published) -> Any:
        bundle = self.publish(**published)
        with case.assertRaises(simulation.AcceptanceError) as caught:
            self.accept(bundle, name)
        case.assertEqual(caught.exception.code, code, str(caught.exception))
        return caught.exception


def run_cli(*arguments: object) -> tuple[int, dict]:
    stream = io.StringIO()
    with contextlib.redirect_stdout(stream):
        status = simulation.main([str(item) for item in arguments])
    return status, json.loads(stream.getvalue())


HAND_WRITTEN = """<mujoco model="fixture">
  <compiler angle="radian"/>
  <option timestep="0.002"/>
  <worldbody>
    <geom name="floor" type="plane" size="2 2 0.1"/>
    <body name="arm" pos="0 0 0.5">
      <joint name="hinge" type="hinge" axis="0 1 0" range="-1.5 1.5" limited="true"/>
      <inertial pos="0.1 0 0" mass="0.2" diaginertia="0.002 0.002 0.002"/>
      <geom name="arm_geom" type="capsule" fromto="0 0 0 0.2 0 0" size="0.02" contype="1" conaffinity="1"/>
      <site name="tip" pos="0.2 0 0"/>
    </body>
  </worldbody>
  <actuator>{actuator}</actuator>
</mujoco>
"""


def inspect_xml(actuator: str, **overrides) -> dict:
    """Load a hand-written motor MJCF and run only the model inspector on it."""

    model = mujoco.MjModel.from_xml_string(HAND_WRITTEN.format(actuator=actuator))
    config = {
        "timestep_s": 0.002,
        "initial_joints": {"hinge": 0.0},
        "initial_base": None,
        "torque_default": 0.5,
        "torque_joints": {},
        **overrides,
    }
    return simulation.inspect_model(model, config)


def rejects(case: unittest.TestCase, code: str, **kwargs) -> Any:
    with case.assertRaises(simulation.AcceptanceError) as caught:
        inspect_xml(**kwargs)
    case.assertEqual(caught.exception.code, code, str(caught.exception))
    return caught.exception


MOTOR = '<motor name="arm_0" joint="hinge" gear="1" ctrllimited="true" ctrlrange="-1 1"/>'


class SimulationAcceptanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="sim-accept-"))
        self.workspace = Workspace(self.tmp)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    # --- positive path ---------------------------------------------------

    def test_positive_run_qualifies_a_real_bundle(self) -> None:
        bundle = self.workspace.publish()
        before = dict(subject_files(bundle))
        acceptance_before = sorted(path.name for path in (bundle / "docs/acceptance").iterdir())
        record = self.workspace.accept(bundle, "positive")

        self.assertEqual(record["schema_version"], "description.acceptance/v2")
        self.assertEqual([item["suite"] for item in record["results"]], list(SUITES))
        for item in record["results"]:
            with self.subTest(suite=item["suite"]):
                self.assertTrue(item["passed"], item["failures"])
                self.assertEqual(item["evidence_class"], "simulation")
                self.assertFalse(item["used_for_fitting"])
                self.assertLess(item["metrics"]["tracking_rmse_rad"], item["thresholds"]["tracking_rmse_rad"])
                self.assertGreaterEqual(item["metrics"]["ground_contact_steps_ratio"], 0.9)
                (name,) = item["artifacts"]
                self.assertTrue(name.startswith("docs/acceptance/"))
                self.assertEqual(item["validation_data"][name], item["artifacts"][name])

        self.assertEqual(record["subject"], digest(before))
        self.assertEqual(
            record["subject"], json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))["subject"]
        )
        self.assertEqual(record["profile_digest"], digest(profile_for(bundle, "simulation")))
        self.assertEqual(record["environment"], ENVIRONMENT)
        self.assertEqual(record["model"]["mjcf"], "mjcf/scene.xml")
        self.assertEqual(record["model"]["mjcf_includes"], ["mjcf/scene.xml", "mjcf/robot.xml"])
        self.assertEqual(record["model"]["oracle"]["binding"], "published")
        self.assertEqual(record["model"]["oracle"]["pending"], ["consumer.application"])
        self.assertEqual(record["model"]["oracle"]["checks"]["bundle.identity"], "passed")
        self.assertFalse(record["qualification"]["release_qualified"])
        self.assertFalse(record["qualification"]["training_qualified"])
        self.assertFalse(record["qualification"]["physical_calibration"])
        self.assertEqual(record["qualification"]["pending"], ["consumer.application"])

        for name in ("acceptance.json", "environment.json", "docs/acceptance/simulation.json"):
            self.assertTrue((self.tmp / "positive" / name).is_file(), name)
        telemetry = self.tmp / "positive" / "docs/acceptance/sine-fixture.jsonl.gz"
        self.assertTrue(telemetry.is_file())
        # Deterministic gzip: the same rows always produce the same bytes.
        self.assertEqual(gzip.compress(gzip.decompress(telemetry.read_bytes()), mtime=0), telemetry.read_bytes())
        rows = [json.loads(line) for line in gzip.decompress(telemetry.read_bytes()).decode().splitlines()]
        self.assertEqual(len(rows), record["results"][1]["metrics"]["steps"])
        self.assertLessEqual(
            {
                "t",
                "t_command",
                "q",
                "qd",
                "q_target",
                "tau",
                "tau_applied",
                "tau_required",
                "ncon",
                "min_contact_m",
            },
            set(rows[0]),
        )
        self.assertIn("base_quaternion", rows[0])
        self.assertEqual(list(rows[0]["tau_applied"]), ["arm_joint"])
        self.assertEqual(rows[0]["t"], 0.02)
        self.assertEqual(rows[0]["t_command"], 0.0)
        self.assertEqual(record["results"][1]["metrics"]["tracked_joints"], ["arm_joint"])
        self.assertEqual(set(record["results"][1]["metrics"]["per_joint"]), {"arm_joint"})

        # The bundle stays read-only, and neither a partial nor a failed directory is left behind.
        self.assertEqual(subject_files(bundle), before)
        self.assertEqual(sorted(path.name for path in (bundle / "docs/acceptance").iterdir()), acceptance_before)
        self.assertEqual(list(self.tmp.glob("*.partial-*")), [])
        self.assertEqual(list(self.tmp.glob("positive.failed-*")), [])

        status, summary = run_cli("--root", bundle, "--profile", "simulation", "--out", self.tmp / "cli")
        self.assertEqual(status, 0)
        self.assertTrue(summary["ok"])
        self.assertEqual(summary["tests"], dict.fromkeys(SUITES, True))

    def test_failed_threshold_is_reported_with_exit_code_one(self) -> None:
        config = config_payload(
            pd={"default": {"kp": 0.5, "kd": 0.0}},
            tests=[
                {
                    "id": "sine-fixture",
                    "kind": "joint_sine",
                    "joint": "arm_joint",
                    "amplitude_rad": 0.05,
                    "frequency_hz": 5.0,
                    "duration_s": 0.4,
                    "thresholds": {"tracking_rmse_rad": 0.01, "max_tracking_error_rad": 0.02},
                }
            ],
        )
        bundle = self.workspace.publish(config=config, profile={"acceptance_suites": ["sine-fixture"]})
        record = self.workspace.accept(bundle, "failed-threshold")

        sine = record["results"][0]
        self.assertFalse(sine["passed"])
        self.assertIn("tracking_rmse_rad", {item["check"] for item in sine["failures"]})
        self.assertTrue((self.tmp / "failed-threshold/acceptance.json").is_file())

        status, summary = run_cli("--root", bundle, "--out", self.tmp / "cli-failed")
        self.assertEqual(status, 1)
        self.assertFalse(summary["ok"])
        self.assertEqual(summary["tests"], {"sine-fixture": False})

    def test_sine_tracking_is_judged_on_the_driven_joint_only(self) -> None:
        """A static joint that sags is reported per joint, but never dilutes the driven one."""

        loose_hand = {
            "base": {"position": [0.0, 0.0, 0.03], "quaternion": [1.0, 0.0, 0.0, 0.0]},
            "joints": {"arm_joint": 0.0, "hand_joint": 0.0},
        }
        config = config_payload(
            initial_state=loose_hand,
            pd={"default": {"kp": 4.0, "kd": 0.3}, "joints": {"hand_joint": {"kp": 0.0, "kd": 0.2}}},
            tests=[
                {
                    "id": "sine-fixture",
                    "kind": "joint_sine",
                    "joint": "arm_joint",
                    "amplitude_rad": 0.05,
                    "frequency_hz": 0.5,
                    "duration_s": 0.4,
                    "thresholds": {"tracking_rmse_rad": 0.05, "max_tracking_error_rad": 0.1},
                }
            ],
        )
        bundle = self.workspace.publish(
            config=config, profile={"acceptance_suites": ["sine-fixture"]}, source=scene(hand=True)
        )
        sine = self.workspace.accept(bundle, "per-joint")["results"][0]

        self.assertTrue(sine["passed"], sine["failures"])
        self.assertEqual(sine["metrics"]["tracked_joints"], ["arm_joint"])
        self.assertLess(sine["metrics"]["tracking_rmse_rad"], 0.05)
        self.assertGreater(sine["metrics"]["per_joint"]["hand_joint"]["tracking_rmse_rad"], 0.5)
        self.assertGreater(sine["metrics"]["worst_joint_tracking_rmse_rad"], 0.5)

    # --- torque semantics ------------------------------------------------

    def test_saturation_is_measured_and_only_fails_a_declared_bound(self) -> None:
        saturated = config_payload(
            pd={"default": {"kp": 200.0, "kd": 0.3}},
            torque_limits={"default": 0.05},
            tests=[
                {
                    "id": "hold-fixture",
                    "kind": "hold",
                    "duration_s": 0.4,
                    "thresholds": {"tracking_rmse_rad": 3.0, "max_tracking_error_rad": 3.0},
                },
                {
                    "id": "sine-fixture",
                    "kind": "joint_sine",
                    "joint": "arm_joint",
                    "amplitude_rad": 0.5,
                    "frequency_hz": 2.0,
                    "duration_s": 0.4,
                    "thresholds": {"tracking_rmse_rad": 3.0, "max_tracking_error_rad": 3.0},
                },
            ],
        )
        bundle = self.workspace.publish(config=saturated, source=scene(joint_range=(-1.0, 1.0)))
        record = self.workspace.accept(bundle, "saturated")

        sine = record["results"][1]
        self.assertTrue(sine["passed"], sine["failures"])
        self.assertEqual(sine["failures"], [])
        self.assertGreater(sine["metrics"]["saturation_steps_ratio"], 0.5)
        self.assertEqual(sine["metrics"]["saturation_steps_ratio"], sine["metrics"]["saturation_joint_steps_ratio"])
        self.assertGreater(sine["metrics"]["per_joint"]["arm_joint"]["saturation_steps_ratio"], 0.5)
        self.assertGreater(sine["metrics"]["max_required_torque_nm"], 0.05)
        self.assertLessEqual(sine["metrics"]["max_torque_nm"], 0.05)
        self.assertLessEqual(sine["metrics"]["max_command_torque_nm"], 0.05)

        bounded = config_payload(
            pd={"default": {"kp": 200.0, "kd": 0.3}},
            torque_limits={"default": 0.05},
            tests=[
                {
                    "id": "hold-fixture",
                    "kind": "hold",
                    "duration_s": 0.4,
                    "thresholds": {"tracking_rmse_rad": 3.0, "max_tracking_error_rad": 3.0},
                },
                {
                    "id": "sine-fixture",
                    "kind": "joint_sine",
                    "joint": "arm_joint",
                    "amplitude_rad": 0.5,
                    "frequency_hz": 2.0,
                    "duration_s": 0.4,
                    "thresholds": {
                        "tracking_rmse_rad": 3.0,
                        "max_tracking_error_rad": 3.0,
                        "max_saturation_steps_ratio": 0.05,
                    },
                },
            ],
        )
        bundle = self.workspace.publish(config=bounded, source=scene(joint_range=(-1.0, 1.0)))
        bounded_sine = self.workspace.accept(bundle, "bounded")["results"][1]
        self.assertFalse(bounded_sine["passed"])
        self.assertIn("max_saturation_steps_ratio", {item["check"] for item in bounded_sine["failures"]})

    def test_torque_cap_must_fit_every_declared_model_range(self) -> None:
        bundle = self.workspace.publish(config=config_payload(torque_limits={"default": 0.5}))
        record = self.workspace.accept(bundle, "caps")
        self.assertEqual(record["results"][0]["conditions"]["torque_limits_nm"], {"arm_joint": 0.5})

        error = self.workspace.reject(
            self, "config_torque_cap_exceeds_model", config=config_payload(torque_limits={"default": 0.7})
        )
        self.assertAlmostEqual(error.detail["model_limit_nm"], 0.6, places=9)
        self.assertEqual(error.detail["model_ranges"]["joint_actuatorfrcrange"], [-0.6, 0.6])

        # An asymmetric control range cannot carry the full symmetric cap either.
        error = self.workspace.reject(
            self,
            "config_torque_cap_exceeds_model",
            config=config_payload(torque_limits={"default": 0.5}),
            source=scene(control_range=(-1.0, 0.4)),
        )
        self.assertAlmostEqual(error.detail["model_limit_nm"], 0.4, places=9)
        self.assertEqual(error.detail["model_ranges"]["actuator_ctrlrange"], [-1.0, 0.4])

    def test_duplicate_actuators_are_refused_end_to_end(self) -> None:
        self.workspace.reject(self, "model_actuator_duplicate", source=scene(motors=2))

    def test_non_torque_actuators_are_refused_by_the_inspector(self) -> None:
        rejects(self, "model_gear_unsupported", actuator=MOTOR.replace('gear="1"', 'gear="2"'))
        general = '<general name="arm_0" joint="hinge" gear="1" ctrllimited="true" ctrlrange="-1 1" gainprm="2 0 0"/>'
        rejects(self, "model_actuator_kind", actuator=general)
        filtered = MOTOR.replace("<motor ", '<general dyntype="filter" ')
        rejects(self, "model_actuator_dynamics", actuator=filtered)
        rejects(self, "model_actuator_type", actuator='<motor name="arm_0" site="tip" gear="1"/>')
        error = rejects(
            self,
            "config_torque_cap_exceeds_model",
            actuator=MOTOR.replace('gear="1"', 'gear="1" forcerange="-0.4 0.4"'),
        )
        self.assertAlmostEqual(error.detail["model_limit_nm"], 0.4, places=9)

        info = inspect_xml(MOTOR)
        self.assertEqual([item.joint for item in info["actuators"]], ["hinge"])
        self.assertAlmostEqual(info["actuators"][0].hard_limit_nm, 1.0, places=9)
        self.assertTrue(info["ground_geoms"])

    # --- MJCF include closure -------------------------------------------

    def test_include_closure_is_walked_and_plugins_refused(self) -> None:
        root = self.tmp / "mjcf-tree"

        def write(relative: str, text: str) -> None:
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")

        def model(*children: str) -> str:
            return "<mujoco model='t'>" + "".join(children) + "</mujoco>"

        write("mjcf/scene.xml", model("<include file='parts/robot.xml'/>"))
        write("mjcf/parts/robot.xml", model("<include file='base.xml'/>"))
        write("mjcf/parts/base.xml", model("<worldbody/>"))
        self.assertEqual(
            simulation._scan_mjcf(root, "mjcf/scene.xml"),
            ["mjcf/scene.xml", "mjcf/parts/robot.xml", "mjcf/parts/base.xml"],
        )

        write("mjcf/parts/base.xml", model("<extension><plugin plugin='escape.test'/></extension>"))
        with self.assertRaises(simulation.AcceptanceError) as caught:
            simulation._scan_mjcf(root, "mjcf/scene.xml")
        self.assertEqual(caught.exception.code, "model_plugin")
        self.assertEqual(caught.exception.detail["mjcf"], "mjcf/parts/base.xml")

        write("mjcf/parts/base.xml", model("<worldbody/>"))
        write("mjcf/escape.xml", model("<include file='../../outside.xml'/>"))
        write("mjcf/absolute.xml", model("<include file='/etc/hostname'/>"))
        write("mjcf/windows-absolute.xml", model("<include file='C:/Windows/win.ini'/>"))
        write("mjcf/windows-drive.xml", model("<include file='C:win.ini'/>"))
        write("mjcf/network.xml", model("<include file='//server/share/model.xml'/>"))
        write("mjcf/missing.xml", model("<include file='parts/absent.xml'/>"))
        write("mjcf/loop-a.xml", model("<include file='loop-b.xml'/>"))
        write("mjcf/loop-b.xml", model("<include file='loop-a.xml'/>"))
        write("mjcf/twice.xml", model("<include file='parts/base.xml'/><include file='parts/base.xml'/>"))
        for relative, code in (
            ("mjcf/escape.xml", "model_include_escape"),
            ("mjcf/absolute.xml", "model_include_escape"),
            ("mjcf/windows-absolute.xml", "model_include_escape"),
            ("mjcf/windows-drive.xml", "model_include_escape"),
            ("mjcf/network.xml", "model_include_escape"),
            ("mjcf/missing.xml", "model_include_missing"),
            ("mjcf/loop-a.xml", "model_include_cycle"),
            ("mjcf/twice.xml", "model_include_cycle"),
        ):
            with self.subTest(model=relative):
                with self.assertRaises(simulation.AcceptanceError) as caught:
                    simulation._scan_mjcf(root, relative)
                self.assertEqual(caught.exception.code, code)

    # --- bundle gate -----------------------------------------------------

    def test_plugin_scan_refuses_before_the_oracle_loads_the_model(self) -> None:
        """A plugin in the include closure is refused before ``assess`` would call it tampering."""

        bundle = self.workspace.publish()
        for name in ("mjcf/robot.xml", "mjcf/scene.xml"):
            with self.subTest(model=name):
                poisoned = self.tmp / f"poisoned-{Path(name).stem}"
                shutil.copytree(bundle, poisoned)
                path = poisoned / name
                path.write_text(
                    path.read_text(encoding="utf-8").replace(
                        "</mujoco>", "<extension><plugin plugin='escape.test'/></extension></mujoco>"
                    ),
                    encoding="utf-8",
                )
                with self.assertRaises(simulation.AcceptanceError) as caught:
                    self.workspace.accept(poisoned, f"out-{Path(name).stem}")
                self.assertEqual(caught.exception.code, "model_plugin")

    def test_pending_build_candidate_is_measured_without_forging_a_bundle(self) -> None:
        """The trusted workflow step hands over the pending candidate; the runner re-qualifies it."""

        step = runpy.run_path(str(ROOT / ".github/scripts/build_simulation_candidate.py"))

        self.workspace.publish()
        with mock.patch(
            "description_pipeline.verification.acceptance.verify_external_record",
            return_value={"trusted": True, "status": "passed"},
        ):
            published = step["candidate"](self.workspace.root, "simulation", self.tmp / "candidate")
        self.assertEqual(published["state"], "published")
        self.assertEqual(published["path"], str(self.tmp / "candidate"))
        self.assertTrue((self.tmp / "candidate/manifest.json").is_file())
        record = self.workspace.accept(self.tmp / "candidate", "published")
        self.assertEqual(record["subject"], published["report"]["subject"])

        fresh = Workspace(self.tmp / "pending")
        fresh.write_config()
        pending = step["candidate"](fresh.root, "simulation", self.tmp / "candidate-pending")
        self.assertEqual(pending["state"], "pending")
        self.assertEqual(pending["report"]["blockers"], ["consumer.application"])
        candidate = Path(pending["path"])
        self.assertTrue((candidate / "manifest.json").is_file())
        self.assertFalse((self.tmp / "candidate-pending").exists())
        record = fresh.accept(candidate, "out-pending")
        self.assertEqual(record["model"]["oracle"]["binding"], "pending_candidate")
        self.assertEqual(
            sorted(record["model"]["oracle"]["pending"]), ["bundle.report_binding", "consumer.application"]
        )
        self.assertEqual(record["subject"], digest(subject_files(candidate)))

        blocked = Workspace(self.tmp / "blocked")
        blocked.profile(contact=None)
        with self.assertRaises(PipelineError) as caught:
            step["candidate"](blocked.root, "simulation", self.tmp / "candidate-blocked")
        self.assertIn("consumer.contact_parameters", str(caught.exception))

    def test_workflow_verifier_binds_record_to_subject_and_candidate(self) -> None:
        """The workflow verifies the candidate it built, not a scene the checkout happens to have."""

        step = runpy.run_path(str(ROOT / ".github/scripts/verify_simulation_record.py"))
        bundle = self.workspace.publish()
        record = self.workspace.accept(bundle, "verifier")
        path = self.tmp / "verifier/acceptance.json"

        summary: dict = {}
        step["verify"](record=path, subject=record["subject"], candidate=bundle, summary=summary)
        self.assertEqual(summary["tests"], dict.fromkeys(SUITES, True))
        self.assertFalse(summary["release_qualified"])

        with self.assertRaises(ValueError):
            step["verify"](record=path, subject="a" * 64, candidate=bundle, summary={})

        # A kinematics checkout may carry a different scene; the measured one is the candidate's.
        other = self.tmp / "other-candidate"
        shutil.copytree(bundle, other)
        (other / "mjcf/scene.xml").write_text("<mujoco model='other'/>", encoding="utf-8")
        with self.assertRaises(ValueError):
            step["verify"](record=path, subject=record["subject"], candidate=other, summary={})

        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["model"]["oracle"]["pending"] = ["consumer.application", "source.coverage"]
        path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(ValueError):
            step["verify"](record=path, subject=record["subject"], candidate=bundle, summary={})

    def test_bundle_gate_re_derives_the_subject_and_verdict(self) -> None:
        bundle = self.workspace.publish()

        tampered = self.tmp / "tampered"
        shutil.copytree(bundle, tampered)
        path = tampered / "mjcf/robot.xml"
        path.write_text(path.read_text(encoding="utf-8").replace("<mujoco", "<mujoco edited='1'"), encoding="utf-8")
        with self.assertRaises(simulation.AcceptanceError) as caught:
            self.workspace.accept(tampered, "out-tampered")
        self.assertEqual(caught.exception.code, "bundle_tampered")

        incomplete = self.tmp / "incomplete"
        shutil.copytree(bundle, incomplete)
        (incomplete / "manifest.json").unlink()
        with self.assertRaises(simulation.AcceptanceError) as caught:
            self.workspace.accept(incomplete, "out-incomplete")
        self.assertEqual(caught.exception.code, "bundle_incomplete")

        # The gate tolerates exactly one pending check: the external attestation.
        oracle = {"profile": profile_for(bundle, "simulation"), "blockers": ["source.coverage"]}
        with (
            mock.patch.object(simulation, "assess", return_value=oracle),
            self.assertRaises(simulation.AcceptanceError) as caught,
        ):
            self.workspace.accept(bundle, "out-unqualified")
        self.assertEqual(caught.exception.code, "bundle_unqualified")

    def test_profile_gate_precedes_the_oracle(self) -> None:
        bare = self.tmp / "bare"
        (bare / "config/profiles").mkdir(parents=True)
        with self.assertRaises(simulation.AcceptanceError) as caught:
            self.workspace.accept(bare, "no-profile")
        self.assertEqual(caught.exception.code, "bundle_incomplete")

        # A directory with a profile but no workspace contract is not a bundle either; give it the
        # contract so this keeps testing the profile-purpose gate rather than the workspace one.
        write_json(bare / "config/robot.yaml", {"schema_version": "description.definition/v1", "hardware_id": "bare"})
        write_json(bare / "config/profiles/simulation.json", {"purpose": "training"})
        with self.assertRaises(simulation.AcceptanceError) as caught:
            self.workspace.accept(bare, "training")
        self.assertEqual(caught.exception.code, "profile_purpose")

    def test_profile_suites_and_environment_are_bound(self) -> None:
        self.workspace.reject(self, "profile_suites_mismatch", profile={"acceptance_suites": ["hold-fixture"]})
        self.workspace.reject(
            self, "profile_environment_mismatch", profile={"consumer_environment": {**ENVIRONMENT, "mujoco": "0.0.0"}}
        )
        self.workspace.reject(self, "profile_environment_unknown", profile={"consumer_environment": {"torch": "1.0"}})

    # --- model state and support ----------------------------------------

    def test_ground_and_self_contact_are_counted_independently(self) -> None:
        """A step that rests on the ground may also press two links together."""

        xml = """<mujoco model="contacts">
          <worldbody>
            <geom name="floor" type="plane" size="2 2 0.1"/>
            <body name="standing" pos="0 0 0.02">
              <freejoint/>
              <geom name="foot" type="box" size="0.05 0.05 0.02" contype="1" conaffinity="1"/>
            </body>
            <body name="left" pos="1 0 0.5"><freejoint/>
              <geom name="a" type="sphere" size="0.05" contype="1" conaffinity="1"/>
            </body>
            <body name="right" pos="1.02 0 0.5"><freejoint/>
              <geom name="b" type="sphere" size="0.05" contype="1" conaffinity="1"/>
            </body>
          </worldbody>
        </mujoco>
        """
        model = mujoco.MjModel.from_xml_string(xml)
        data = mujoco.MjData(model)
        mujoco.mj_forward(model, data)
        ground = [index for index in range(model.ngeom) if model.geom_bodyid[index] == 0]
        world, self_contacts = simulation._contact_counts(data, ground)
        self.assertGreaterEqual(world, 1)
        self.assertGreaterEqual(self_contacts, 1)
        self.assertEqual(world + self_contacts, int(data.ncon))

        data.warning.number[mujoco.mjtWarning.mjWARN_BADQACC] = 1
        with self.assertRaises(simulation.AcceptanceError) as caught:
            simulation._check_warnings(data, {"test": "fixture"})
        self.assertEqual(caught.exception.code, "model_warning")
        self.assertEqual(caught.exception.detail["warnings"], {"mjWARN_BADQACC": 1})

    def test_declared_base_state_must_match_the_model(self) -> None:
        self.workspace.reject(self, "model_base_state", profile={"root_mode": "fixed"})

    def test_initial_penetration_and_support_are_measured(self) -> None:
        penetrating = config_payload()
        penetrating["initial_state"]["base"]["position"] = [0.0, 0.0, 0.0]
        self.workspace.reject(self, "initial_penetration", config=penetrating)

        falling = config_payload()
        falling["initial_state"]["base"]["position"] = [0.0, 0.0, 2.0]
        self.workspace.reject(self, "support_contact_missing", config=falling)

        self.workspace.reject(self, "support_not_verifiable", profile={"ground": False})

    def test_penetration_is_judged_in_the_state_each_step_ends_in(self) -> None:
        """A collision created *by* a step is seen in that step, not one step later or never."""

        config = config_payload(
            timestep_s=0.02,
            control_period_s=0.02,
            tests=[
                {
                    "id": "hold-fixture",
                    "kind": "hold",
                    "duration_s": 0.02,
                    "thresholds": {
                        "tracking_rmse_rad": 3.0,
                        "max_tracking_error_rad": 3.0,
                        "penetration_tol_m": 1e-4,
                    },
                }
            ],
        )
        # The base starts 1 mm above the floor and free-falls through it inside one step:
        # the pre-integration contact set is empty, the post-integration state is not.
        config["initial_state"]["base"]["position"] = [0.0, 0.0, 0.031]
        # One physics step per control step, so only the end-of-step state can see it.
        profile = {"acceptance_suites": ["hold-fixture"], "timestep": 0.02}
        bundle = self.workspace.publish(config=config, profile=profile)
        with self.assertRaises(simulation.AcceptanceError) as caught:
            self.workspace.accept(bundle, "free-fall")
        self.assertEqual(caught.exception.code, "penetration")
        self.assertEqual(caught.exception.detail["step"], 0)
        self.assertEqual(caught.exception.detail["substep"], 0)
        self.assertLess(caught.exception.detail["min_contact_m"], -1e-4)

        # The same run is evidence when the author tolerates that depth, so the check
        # above really is what refuses the first one.
        tolerated = copy.deepcopy(config)
        tolerated["tests"][0]["thresholds"]["penetration_tol_m"] = 0.05
        bundle = self.workspace.publish(config=tolerated, profile=profile)
        record = self.workspace.accept(bundle, "free-fall-tolerated")
        self.assertEqual(record["results"][0]["metrics"]["steps"], 1)
        self.assertLess(record["results"][0]["metrics"]["min_base_height_m"], 0.03)

    # --- output directory -------------------------------------------------

    def test_output_is_never_overwritten_or_left_partial(self) -> None:
        bundle = self.workspace.publish()
        existing = self.tmp / "existing"
        existing.mkdir()
        (existing / "acceptance.json").write_text("stale\n", encoding="utf-8")
        with self.assertRaises(simulation.AcceptanceError) as caught:
            self.workspace.accept(bundle, "existing")
        self.assertEqual(caught.exception.code, "output_exists")
        self.assertEqual((existing / "acceptance.json").read_text(encoding="utf-8"), "stale\n")

        broken = config_payload()
        broken["tests"][1]["amplitude_rad"] = 0.5
        bundle = self.workspace.publish(config=broken)
        with self.assertRaises(simulation.AcceptanceError) as caught:
            self.workspace.accept(bundle, "aborted")
        self.assertEqual(caught.exception.code, "test_out_of_range")
        self.assertFalse((self.tmp / "aborted").exists())
        (diagnostic,) = self.tmp.glob("aborted.failed-*")
        failure = json.loads((diagnostic / "failure.json").read_text(encoding="utf-8"))
        self.assertEqual(failure["code"], "test_out_of_range")
        self.assertEqual(caught.exception.detail["diagnostic_path"], str(diagnostic))
        self.assertEqual(list(self.tmp.glob("*.partial-*")), [])
        self.assertEqual(list(diagnostic.rglob("acceptance.json")), [])
        # Evidence measured before the rejection is kept, not dropped with the run.
        self.assertTrue((diagnostic / "docs/acceptance/hold-fixture.jsonl.gz").is_file())

        # A rejection in the middle of a run keeps that run's telemetry up to the failure.
        dropping = config_payload()
        dropping["initial_state"]["base"]["position"] = [0.0, 0.0, 0.05]
        dropping["tests"] = [dropping["tests"][0]]
        dropping["tests"][0]["thresholds"]["penetration_tol_m"] = 1e-12
        bundle = self.workspace.publish(config=dropping, profile={"acceptance_suites": ["hold-fixture"]})
        with self.assertRaises(simulation.AcceptanceError) as caught:
            self.workspace.accept(bundle, "mid-run")
        self.assertEqual(caught.exception.code, "penetration")
        self.assertGreaterEqual(len(caught.exception.telemetry), 1)
        (diagnostic,) = self.tmp.glob("mid-run.failed-*")
        rows = (
            gzip.decompress((diagnostic / "docs/acceptance/hold-fixture.jsonl.gz").read_bytes()).decode().splitlines()
        )
        self.assertEqual(len(rows), len(caught.exception.telemetry))
        self.assertEqual(json.loads(rows[0])["ncon"], 0)

    # --- configuration contract ------------------------------------------

    def write_config(self, payload) -> Path:
        path = self.tmp / "config.json"
        if isinstance(payload, str):
            path.write_text(payload, encoding="utf-8")
        else:
            write_json(path, payload)
        return path

    def reject_config(self, payload, code: str) -> None:
        with self.assertRaises(simulation.AcceptanceError) as caught:
            simulation.load_config(self.write_config(payload))
        self.assertEqual(caught.exception.code, code, str(caught.exception))

    def test_configuration_rejects_unknown_duplicate_and_non_finite_fields(self) -> None:
        self.reject_config(config_payload(extra=1), "config_unknown_field")
        self.reject_config('{"schema": "description.simulation-acceptance/v1", "schema": "x"}', "config_duplicate_key")
        self.reject_config(
            json.dumps(config_payload()).replace('"duration_s": 0.4', '"duration_s": NaN'), "config_non_finite"
        )
        self.reject_config(config_payload(tests=[]), "config_tests_empty")
        self.reject_config(config_payload(initial_state={"base": None, "joints": {}}), "config_state_empty")

    def test_configuration_values_are_bounded(self) -> None:
        duration = config_payload()
        duration["tests"][0]["duration_s"] = 0.0
        self.reject_config(duration, "config_out_of_range")
        period = config_payload()
        period["control_period_s"] = 0.021
        self.reject_config(period, "config_period_ratio")
        missing = config_payload()
        missing["tests"][0]["thresholds"].pop("max_tracking_error_rad")
        self.reject_config(missing, "config_threshold_missing")
        loose = config_payload()
        loose["tests"][0]["thresholds"]["tracking_rmse_rad"] = 5.0
        self.reject_config(loose, "config_out_of_range")
        ratio = config_payload()
        ratio["tests"][1]["thresholds"]["max_saturation_steps_ratio"] = 1.5
        self.reject_config(ratio, "config_out_of_range")
        unknown_joint = config_payload()
        unknown_joint["tests"][1]["joint"] = "other_joint"
        self.reject_config(unknown_joint, "config_joint_unknown")
        support = config_payload()
        support["tests"][1]["support"] = {"ground": True}
        self.reject_config(support, "config_support_kind")
        # The measured artifact is fixed to the qualified consumer scene.
        other_model = config_payload()
        other_model["model"] = {"mjcf": "mjcf/robot.xml"}
        self.reject_config(other_model, "config_model_path")
        absolute = config_payload()
        absolute["model"] = {"mjcf": "/etc/hostname"}
        self.reject_config(absolute, "config_model_path")


if __name__ == "__main__":
    unittest.main()
