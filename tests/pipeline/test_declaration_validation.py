"""Every fail-closed branch of the contact and control declarations.

These two validators are what turns a wrong declaration into a diagnostic before anything is built,
but the suite only reached them through a well-formed model: coverage showed every rejection branch
unexecuted.  A guard that has never been seen to reject is not a guard, so each branch is exercised
here directly.
"""

import math
import unittest
from typing import ClassVar, cast

from description_pipeline.io import PipelineError
from description_pipeline.model.contact import validate_contact
from description_pipeline.model.control import map_actions, read_observations, validate_control


def contact(**overrides) -> dict:
    value = {
        "friction": [1.0, 0.005, 0.0001],
        "condim": 3,
        "solref": [0.02, 1.0],
        "solimp": [0.9, 0.95, 0.001, 0.5, 2.0],
        "margin": 0.0,
        "gap": 0.0,
    }
    value.update(overrides)
    return value


def declaration(**overrides) -> dict:
    """A minimal but complete declaration: one actuator, one observation channel."""

    data = {
        "joints": [{"name": "j1", "type": "revolute"}],
        "actuators": [{"name": "a1", "joint": "j1", "control_range": [-1.0, 1.0]}],
        "sensors": [{"name": "s1", "type": "gyro"}],
        "control": {
            "action_order": ["a1"],
            "actions": {"a1": {"unit": "N*m", "polarity": 1, "offset": 0.0, "evidence": "spec"}},
            "observation_order": ["j1_pos"],
            "observations": {
                "j1_pos": {
                    "unit": "rad",
                    "polarity": 1,
                    "offset": 0.0,
                    "evidence": "spec",
                    "source": "joint_position",
                    "target": "j1",
                    "component": 0,
                }
            },
        },
    }
    for key, value in overrides.items():
        data[key] = value
    return data


class ContactValidationTests(unittest.TestCase):
    def test_a_complete_contact_passes(self):
        validate_contact(contact())

    def test_each_rejection_names_what_is_wrong(self):
        cases = {
            "missing field": ({"friction": [1.0, 0.0, 0.0]}, "Contact requires"),
            "not a mapping": (["friction"], "Contact requires"),
            "wrong friction length": (contact(friction=[1.0, 0.0]), "Invalid contact friction"),
            "non-finite friction": (contact(friction=[1.0, math.inf, 0.0]), "Invalid contact friction"),
            "unsupported condim": (contact(condim=2), "condim must be 1, 3, 4 or 6"),
            "boolean condim": (contact(condim=True), "condim must be 1, 3, 4 or 6"),
            "negative friction": (contact(friction=[-1.0, 0.0, 0.0]), "Friction must be nonnegative"),
            "zero solref": (contact(solref=[0.0, 1.0]), "Friction must be nonnegative"),
            "impedance out of order": (contact(solimp=[0.95, 0.9, 0.001, 0.5, 2.0]), "Invalid contact impedance"),
            "impedance power below one": (contact(solimp=[0.9, 0.95, 0.001, 0.5, 0.5]), "Invalid contact impedance"),
            "negative margin": (contact(margin=-0.001), "Invalid contact margin"),
            "non-finite gap": (contact(gap=math.nan), "Invalid contact gap"),
        }
        for label, (value, fragment) in cases.items():
            with self.subTest(case=label), self.assertRaises(PipelineError) as raised:
                # The "not a mapping" case is deliberately the wrong type.
                validate_contact(cast("dict", value))
            self.assertIn(fragment, str(raised.exception))


class ControlValidationTests(unittest.TestCase):
    def test_a_complete_declaration_passes(self):
        validate_control(declaration())

    def test_each_rejection_names_what_is_wrong(self):
        def with_action(**channel) -> dict:
            data = declaration()
            data["control"]["actions"]["a1"].update(channel)
            return data

        def with_observation(**channel) -> dict:
            data = declaration()
            data["control"]["observations"]["j1_pos"].update(channel)
            return data

        cases: dict[str, tuple[dict, str]] = {
            "missing control key": (
                {**declaration(), "control": {"action_order": [], "actions": {}}},
                "Control requires",
            ),
            "duplicate channel": (
                {
                    **declaration(),
                    "control": {
                        **declaration()["control"],
                        "action_order": ["a1", "a1"],
                    },
                },
                "Incomplete or duplicate action channels",
            ),
            "empty order": (
                {**declaration(), "control": {**declaration()["control"], "action_order": []}},
                "Incomplete or duplicate action channels",
            ),
            "order without mapping": (
                {**declaration(), "control": {**declaration()["control"], "actions": {}}},
                "Incomplete or duplicate action channels",
            ),
            "extra channel field": (with_action(scale=2.0), "Unsupported action mapping"),
            "polarity out of range": (with_action(polarity=0), "finite offset, signed polarity and evidence"),
            "offset not finite": (with_action(offset=math.inf), "finite offset, signed polarity and evidence"),
            "evidence empty": (with_action(evidence=""), "finite offset, signed polarity and evidence"),
            "unit not SI": (with_action(unit="kgf*m"), "requires SI unit N*m"),
            "unknown actuator": (
                {
                    **declaration(),
                    "control": {
                        **declaration()["control"],
                        "action_order": ["a2"],
                        "actions": {"a2": dict(declaration()["control"]["actions"]["a1"])},
                    },
                },
                "Unknown action actuator",
            ),
            "actuator not covered": (
                {
                    **declaration(),
                    "actuators": [
                        {"name": "a1", "joint": "j1", "control_range": [-1.0, 1.0]},
                        {"name": "a2", "joint": "j1", "control_range": [-1.0, 1.0]},
                    ],
                },
                "cover every actuator exactly once",
            ),
            "component not an integer": (with_observation(component=0.0), "must be an integer"),
            "unknown observation target": (with_observation(target="j9"), "Unsupported observation source"),
            "component beyond the sensor": (
                with_observation(source="sensor", target="s1", component=3),
                "Unsupported observation",
            ),
        }
        for label, (value, fragment) in cases.items():
            with self.subTest(case=label), self.assertRaises(PipelineError) as raised:
                validate_control(value)
            self.assertIn(fragment, str(raised.exception))

    def test_prismatic_and_sensor_units_follow_the_declaration(self):
        prismatic = declaration(joints=[{"name": "j1", "type": "prismatic"}])
        prismatic["control"]["actions"]["a1"]["unit"] = "N"
        prismatic["control"]["observations"]["j1_pos"]["unit"] = "m"
        validate_control(prismatic)

        sensor = declaration()
        sensor["control"]["observations"]["j1_pos"].update({"source": "sensor", "target": "s1", "component": 0})
        sensor["control"]["observations"]["j1_pos"]["unit"] = "rad/s"
        validate_control(sensor)

        velocity = declaration()
        velocity["control"]["observations"]["j1_pos"].update({"source": "joint_velocity"})
        velocity["control"]["observations"]["j1_pos"]["unit"] = "rad/s"
        validate_control(velocity)

        quaternion = declaration(sensors=[{"name": "s1", "type": "framequat"}])
        quaternion["control"]["observations"]["j1_pos"].update({"source": "sensor", "target": "s1", "component": 3})
        quaternion["control"]["observations"]["j1_pos"]["unit"] = "1"
        validate_control(quaternion)

    def test_map_actions_rejects_shape_and_range(self):
        data = declaration()
        self.assertEqual(map_actions(data, [0.5]), [0.5])
        with self.assertRaisesRegex(PipelineError, "shape or values"):
            map_actions(data, [0.5, 0.5])
        with self.assertRaisesRegex(PipelineError, "shape or values"):
            map_actions(data, [math.nan])
        with self.assertRaisesRegex(PipelineError, "exceeds declared actuator range"):
            map_actions(data, [2.0])


class FakeMuJoCo:
    """Just enough of the MuJoCo surface for `read_observations`."""

    class _Objects:
        mjOBJ_SENSOR = "sensor"
        mjOBJ_JOINT = "joint"

    mjtObj = _Objects()

    def __init__(self, missing: str = "") -> None:
        self.missing = missing

    def mj_name2id(self, _model, kind: str, name: str) -> int:
        if name == self.missing:
            return -1
        return 0 if kind == self.mjtObj.mjOBJ_SENSOR else 1


class FakeModel:
    sensor_adr: ClassVar[list[int]] = [0]
    jnt_qposadr: ClassVar[list[int]] = [0, 0]
    jnt_dofadr: ClassVar[list[int]] = [0, 0]


class FakeState:
    def __init__(self, sensordata=None, qpos=None, qvel=None) -> None:
        self.sensordata = [1.0] if sensordata is None else sensordata
        self.qpos = [0.25, 0.0] if qpos is None else qpos
        self.qvel = [0.5, 0.0] if qvel is None else qvel


class ObservationReadTests(unittest.TestCase):
    def test_declared_channels_are_read_and_calibrated(self):
        data = declaration()
        joint = read_observations(data, FakeModel(), FakeState(), FakeMuJoCo())
        self.assertEqual(joint, [0.25])

        sensor = declaration()
        sensor["control"]["observations"]["j1_pos"].update({"source": "sensor", "target": "s1", "component": 0})
        sensor["control"]["observations"]["j1_pos"]["unit"] = "rad/s"
        sensor["control"]["observations"]["j1_pos"]["offset"] = 0.25
        self.assertEqual(read_observations(sensor, FakeModel(), FakeState(), FakeMuJoCo()), [0.75])

    def test_a_missing_consumer_target_is_a_diagnostic(self):
        for kind in ("joint_position", "sensor"):
            data = declaration()
            channel = data["control"]["observations"]["j1_pos"]
            if kind == "sensor":
                channel.update({"source": "sensor", "target": "s1"})
                channel["unit"] = "rad/s"
                self.assertIsNotNone(data)
            with self.subTest(source=kind), self.assertRaisesRegex(PipelineError, "missing observation target"):
                read_observations(data, FakeModel(), FakeState(), FakeMuJoCo(missing=channel["target"]))

    def test_a_nonfinite_consumer_reading_is_a_diagnostic(self):
        with self.assertRaisesRegex(PipelineError, "Nonfinite consumer observation"):
            read_observations(declaration(), FakeModel(), FakeState(qpos=[math.nan, 0.0]), FakeMuJoCo())


if __name__ == "__main__":
    unittest.main()
