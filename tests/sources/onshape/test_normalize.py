"""机器人定义与来源语义规范化：分组、融合、运动链闭合与证据边界。"""

import copy
import json
import math
import tempfile
import unittest
from pathlib import Path

import yaml

from description_pipeline.model import Robot
from description_pipeline.sources.onshape import load_scene
from description_pipeline.sources.onshape.definition import parse_robot_definition
from description_pipeline.sources.onshape.errors import (
    ATTRIBUTION_MISMATCH,
    DEFINITION_INVALID,
    OnshapeSourceError,
)
from description_pipeline.sources.onshape.freeze import freeze
from description_pipeline.sources.onshape.normalize import normalize_scene
from tests import onshape_fixture  # noqa: E402

FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "onshape" / "cache"
DEFINITION = Path(__file__).resolve().parents[2] / "fixtures" / "onshape" / "model" / "robot.yaml"
CAPTURE = {"evidence": "fixture", "reason": "仓库内回放夹具", "at": "2026-09-17T16:00:00Z"}


def microban() -> tuple[dict, dict, Path]:
    """仓库夹具 → （原始场景, 定义, 快照目录）。"""

    tmp = Path(tempfile.mkdtemp(prefix="onshape-normalize-"))
    url = json.loads((FIXTURE / "source.json").read_text(encoding="utf-8"))["url"]
    freeze({"url": url, "cache": str(FIXTURE), "offline": True, "capture": CAPTURE}, tmp / "snapshot")
    return load_scene(tmp / "snapshot"), yaml.safe_load(DEFINITION.read_text(encoding="utf-8")), tmp / "snapshot"


@onshape_fixture.requires_fixture
class DefinitionTests(unittest.TestCase):
    definition: dict

    def setUp(self):
        self.definition = yaml.safe_load(DEFINITION.read_text(encoding="utf-8"))

    def test_microban_definition_parses_with_evidenced_fields(self):
        robot = parse_robot_definition(self.definition)
        self.assertEqual(robot.root, "trunk")
        self.assertEqual((len(robot.links), len(robot.joints), len(robot.frames)), (20, 19, 4))
        self.assertEqual(robot.link_names[0], "ankle_block__configuration_left")
        self.assertTrue(all(joint.axis_sign == 1 for joint in robot.joints))
        self.assertTrue(all(joint.limits.get("source") == "mate_limits" for joint in robot.joints))
        self.assertEqual(robot.reference["file"], "external/rhoban-microban/robot.urdf")
        self.assertFalse(robot.reference["file"].startswith("/"))
        self.assertFalse(robot.reference["evidence"].startswith("/"))
        self.assertEqual(len(robot.mass_overrides), 3)
        self.assertEqual(robot.provider, "onshape")

    def test_missing_robot_mapping_is_rejected(self):
        with self.assertRaises(OnshapeSourceError) as caught:
            parse_robot_definition({"schema_version": "description.definition/v1"})
        self.assertEqual(caught.exception.code, DEFINITION_INVALID)

    def test_unknown_field_is_rejected(self):
        definition = copy.deepcopy(self.definition)
        definition["robot"]["guess"] = True
        with self.assertRaises(OnshapeSourceError) as caught:
            parse_robot_definition(definition)
        self.assertEqual(caught.exception.code, DEFINITION_INVALID)
        self.assertIn("guess", caught.exception.detail["unknown"])

    def test_joint_with_unknown_link_is_rejected(self):
        definition = copy.deepcopy(self.definition)
        definition["robot"]["joints"][0]["parent"] = "missing_link"
        with self.assertRaises(OnshapeSourceError) as caught:
            parse_robot_definition(definition)
        self.assertEqual(caught.exception.code, DEFINITION_INVALID)


@onshape_fixture.requires_fixture
class NormalizeTests(unittest.TestCase):
    scene: dict
    definition: dict
    snapshot: Path
    model: dict

    @classmethod
    def setUpClass(cls):
        cls.scene, cls.definition, cls.snapshot = microban()
        cls.model = normalize_scene(cls.scene, cls.definition, cls.snapshot)

    def test_tree_matches_the_reference_structure(self):
        self.assertEqual(len(self.model["links"]), 24)
        self.assertEqual(len(self.model["joints"]), 23)
        self.assertEqual(sorted(joint["type"] for joint in self.model["joints"]), ["fixed"] * 4 + ["revolute"] * 19)
        roots = {link["name"] for link in self.model["links"]} - {j["child"] for j in self.model["joints"]}
        self.assertEqual(roots, {"trunk"})
        self.assertEqual(
            [link["name"] for link in self.model["links"] if link["inertial"] is None],
            ["body", "imu", "left_foot", "right_foot"],
        )

    def test_robot_contract_accepts_the_normalized_scene(self):
        robot = Robot.from_dict(self.model)
        self.assertEqual(
            {joint["name"] for joint in robot.to_dict()["joints"]}, {joint["name"] for joint in self.model["joints"]}
        )

    def test_attribution_accounts_for_every_cad_instance(self):
        attribution = self.model["provenance"]["attribution"]
        self.assertEqual(attribution["expected_occurrences"], 317)
        self.assertEqual(attribution["expected_entities"], 297)
        self.assertEqual(attribution["assigned"], 293)
        self.assertEqual(len(attribution["non_physical"]), 4)
        self.assertEqual(attribution["ignored_containers"], [])
        assigned = sorted(entity for entities in attribution["per_link"].values() for entity in entities)
        self.assertEqual(len(assigned), len(set(assigned)))
        self.assertEqual(
            set(assigned) | set(attribution["non_physical"]),
            set(self.scene["provenance"]["expected_entities"]),
        )

    def test_mass_and_geometry_are_conserved_per_group(self):
        trunk = next(link for link in self.model["links"] if link["name"] == "trunk")
        members = trunk["provenance"]["members"]
        self.assertEqual(len(members), 51)
        self.assertAlmostEqual(trunk["inertial"]["mass"], 0.316738, places=6)
        self.assertAlmostEqual(trunk["inertial"]["mass"], sum(member["mass_kg"] for member in members), places=12)
        self.assertEqual(len(trunk["visuals"]), 0)  # 仓库夹具没有网格字节
        self.assertEqual(trunk["collisions"], [])
        self.assertEqual(self.model["provenance"]["total_mass_kg"], 0.818977407)

    def test_mass_assumptions_record_their_source(self):
        assumptions = self.model["provenance"]["mass_assumptions"]
        self.assertEqual(len(assumptions), 48)
        tibia = next(item for item in assumptions if item["part_id"] == "KFzB")
        self.assertEqual(tibia["density_kg_m3"], 957.0)
        self.assertEqual(tibia["source"]["file"], "onshape/density_map.json")
        self.assertAlmostEqual(tibia["mass_kg"], 0.009237527216303732, places=12)
        self.assertAlmostEqual(tibia["inertia_scale"], 957.0 / 1240.0, places=6)

    def test_joint_frames_come_from_mate_connectors_with_definition_signs(self):
        joint = next(item for item in self.model["joints"] if item["name"] == "right_hip_yaw")
        provenance = joint["provenance"]
        self.assertEqual((joint["parent"], joint["child"]), ("trunk", "hip"))
        self.assertEqual(joint["axis"], [0.0, 0.0, 1.0])
        self.assertEqual(joint["limits"], {"lower": -4.18879, "upper": 1.0472})
        self.assertEqual(provenance["parent_child_from"], "definition")
        self.assertEqual(provenance["limits_from"], "mate_limits")
        self.assertEqual(provenance["effort_velocity"], "not_defined")
        self.assertEqual(provenance["axis_sign_from"], "official_cross_check")
        self.assertEqual(
            provenance["source_order"],
            [
                {"entity": "M5p1HyEvY6oyV5QDZ/MFeT8m/WSQo79naoB", "group": "hip"},
                {"entity": "Mz+VWY25aP0yenRIq/MFo44/CaNo8r0JV3v", "group": "trunk"},
            ],
        )

    def test_effort_velocity_and_collision_stay_undefined(self):
        self.assertEqual(self.model["provenance"]["conventions"]["collision_policy"]["policy"], "undefined")
        self.assertFalse(self.model["provenance"]["conventions"]["effort_velocity"]["defined"])
        for joint in self.model["joints"]:
            if joint["type"] == "fixed":
                continue
            with self.subTest(joint=joint["name"]):
                self.assertNotIn("effort", joint.get("limits", {}))
                self.assertEqual(joint["provenance"]["effort_velocity"], "not_defined")
        for joint in self.model["joints"]:
            if joint["type"] != "fixed":
                continue
            with self.subTest(joint=joint["name"]):
                self.assertNotIn("limits", joint)
                self.assertNotIn("axis", joint)
                self.assertEqual(joint["provenance"]["kind"], "reference_frame")

    def test_provenance_keeps_full_expectation_and_explicit_exclusions(self):
        provenance = self.model["provenance"]
        expected = set(provenance["expected_entities"])
        covered = set(provenance["covered_entities"])
        excluded = {entry["id"]: entry for entry in provenance["excluded_entities"]}
        self.assertEqual(len(provenance["expected_entities"]), 297)
        self.assertEqual(len(provenance["expected_occurrences"]), 317)
        self.assertEqual(len(covered), 293)
        self.assertEqual(len(provenance["containers"]), 20)
        self.assertEqual(len(excluded), 4)
        self.assertEqual(covered | set(excluded), expected)
        self.assertTrue(expected <= set(provenance["expected_occurrences"]))
        for entity, entry in excluded.items():
            with self.subTest(entity=entity):
                self.assertEqual(entry["reason"], "frame_anchor_jig")
                self.assertEqual(entry["evidence"]["declared_by"], "robot.non_physical")
                self.assertEqual(len(entry["evidence"]["consumed_by"]), 1)
                self.assertTrue(entry["evidence"]["detail"])
        for link in self.model["links"]:
            with self.subTest(link=link["name"]):
                self.assertTrue(link["provenance"]["source_entities"])


@onshape_fixture.requires_fixture
class DefinitionFailClosedTests(unittest.TestCase):
    """定义里写得出来但不生效的字段必须失败，而不是被静默忽略。"""

    def setUp(self):
        self.definition = yaml.safe_load(DEFINITION.read_text(encoding="utf-8"))

    def assert_rejected(self, mutate, *, field=None) -> OnshapeSourceError:
        definition = copy.deepcopy(self.definition)
        mutate(definition)
        with self.assertRaises(OnshapeSourceError) as caught:
            parse_robot_definition(definition)
        self.assertEqual(caught.exception.code, DEFINITION_INVALID)
        if field is not None:
            self.assertIn(field, caught.exception.detail)
        return caught.exception

    def test_provider_must_match_the_source(self):
        self.assert_rejected(lambda definition: definition["robot"].__setitem__("provider", "solidworks"))

    def test_axis_unknown_subfield_is_rejected(self):
        self.assert_rejected(lambda definition: definition["robot"]["joints"][0]["axis"].__setitem__("flip", True))

    def test_axis_source_must_be_supported(self):
        error = self.assert_rejected(
            lambda definition: definition["robot"]["joints"][0]["axis"].__setitem__("source", "by_name"),
            field="source",
        )
        self.assertEqual(error.detail["supported"], ["mate_connector_z"])

    def test_limits_unknown_subfield_is_rejected(self):
        self.assert_rejected(lambda definition: definition["robot"]["joints"][0]["limits"].__setitem__("damping", 0.1))

    def test_limits_half_interval_is_rejected(self):
        def mutate(definition):
            definition["robot"]["joints"][0]["limits"] = {"lower": -1.0}

        self.assert_rejected(mutate)

    def test_limits_explicit_interval_must_be_ordered(self):
        def mutate(definition):
            definition["robot"]["joints"][0]["limits"] = {"lower": 1.0, "upper": -1.0}

        self.assert_rejected(mutate)

    def test_limits_source_must_be_supported(self):
        self.assert_rejected(
            lambda definition: definition["robot"]["joints"][0]["limits"].__setitem__("source", "guess"),
            field="source",
        )

    def test_zero_unknown_subfield_and_missing_evidence_are_rejected(self):
        self.assert_rejected(lambda definition: definition["robot"]["joints"][0].__setitem__("zero", {"offset": 0.1}))
        self.assert_rejected(
            lambda definition: definition["robot"]["joints"][0].__setitem__("zero", {"value": 0.1, "source": ""})
        )

    def test_zero_value_must_be_finite(self):
        self.assert_rejected(
            lambda definition: definition["robot"]["joints"][0].__setitem__(
                "zero", {"value": math.inf, "source": "measured"}
            )
        )

    def test_density_override_must_be_finite_and_positive(self):
        for value in (math.inf, math.nan, 0.0, -957.0):
            with self.subTest(value=value):
                self.assert_rejected(
                    lambda definition, value=value: definition["robot"]["mass"]["overrides"][0].__setitem__(
                        "density_kg_m3", value
                    )
                )

    def test_density_override_requires_evidence(self):
        def mutate(definition):
            definition["robot"]["mass"]["overrides"][0].pop("source")

        error = self.assert_rejected(mutate)
        self.assertIn("source", error.message)
        self.assert_rejected(lambda definition: definition["robot"]["mass"]["overrides"][0].__setitem__("source", {}))
        self.assert_rejected(
            lambda definition: definition["robot"]["mass"]["overrides"][0].__setitem__("source", {"unknown": "x"})
        )

    def test_density_override_requires_a_target(self):
        def mutate(definition):
            override = definition["robot"]["mass"]["overrides"][0]
            override.pop("part_ids")

        self.assert_rejected(mutate)

    def test_default_density_must_be_finite(self):
        self.assert_rejected(
            lambda definition: definition["robot"]["mass"].__setitem__("density_default_kg_m3", math.inf)
        )

    def test_collision_declaration_fails_closed(self):
        self.assert_rejected(
            lambda definition: definition["robot"]["collision"].__setitem__("policy", "mesh"),
            field="policy",
        )
        self.assert_rejected(lambda definition: definition["robot"]["collision"].__setitem__("friction", 0.8))

    def test_effort_velocity_declaration_fails_closed(self):
        self.assert_rejected(lambda definition: definition["robot"].__setitem__("effort_velocity", {"defined": True}))


@onshape_fixture.requires_fixture
class NormalizeErrorTests(unittest.TestCase):
    scene: dict
    definition: dict
    snapshot: Path

    @classmethod
    def setUpClass(cls):
        cls.scene, cls.definition, cls.snapshot = microban()

    def normalize(self, definition: dict) -> dict:
        return normalize_scene(self.scene, definition, self.snapshot)

    def test_removing_a_link_group_reports_missing_entities(self):
        definition = copy.deepcopy(self.definition)
        definition["robot"]["links"] = [link for link in definition["robot"]["links"] if link["name"] != "head"]
        definition["robot"]["joints"] = [joint for joint in definition["robot"]["joints"] if joint["name"] != "head"]
        with self.assertRaises(OnshapeSourceError) as caught:
            self.normalize(definition)
        self.assertEqual(caught.exception.code, ATTRIBUTION_MISMATCH)
        self.assertEqual(len(caught.exception.detail["missing"]), 6)

    def test_unknown_member_selector_is_rejected(self):
        definition = copy.deepcopy(self.definition)
        definition["robot"]["links"][0]["members"] = ["not-a-real-instance"]
        with self.assertRaises(OnshapeSourceError) as caught:
            self.normalize(definition)
        self.assertEqual(caught.exception.code, ATTRIBUTION_MISMATCH)
        self.assertEqual(caught.exception.detail["unexpected"][0]["selector"], "not-a-real-instance")

    def test_unlisted_mate_is_rejected(self):
        definition = copy.deepcopy(self.definition)
        definition["robot"]["joints"] = [
            joint for joint in definition["robot"]["joints"] if joint["name"] != "left_hip_yaw"
        ]
        with self.assertRaises(OnshapeSourceError) as caught:
            self.normalize(definition)
        self.assertEqual(caught.exception.code, ATTRIBUTION_MISMATCH)
        self.assertEqual(caught.exception.detail["unlisted"][0]["mate"], "dof_left_hip_yaw")

    def test_duplicate_assignment_is_rejected(self):
        definition = copy.deepcopy(self.definition)
        definition["robot"]["links"][1]["members"] = definition["robot"]["links"][0]["members"]
        with self.assertRaises(OnshapeSourceError) as caught:
            self.normalize(definition)
        self.assertEqual(caught.exception.code, ATTRIBUTION_MISMATCH)

    def test_broken_tree_is_rejected(self):
        definition = copy.deepcopy(self.definition)
        for joint in definition["robot"]["joints"]:
            if joint["name"] == "left_elbow":
                joint["child"] = "left_knee"  # 让 left_knee 有两个父、left_elbow 的子系断开
        with self.assertRaises(OnshapeSourceError) as caught:
            self.normalize(definition)
        self.assertEqual(caught.exception.code, DEFINITION_INVALID)


if __name__ == "__main__":
    unittest.main()
