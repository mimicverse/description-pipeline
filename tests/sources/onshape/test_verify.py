"""独立校验：原始装配/质量读数 vs 规范化产物（含错误注入）。"""

import copy
import hashlib
import json
import math
import os
import shutil
import tempfile
import unittest
from pathlib import Path

import numpy as np
import yaml

from description_pipeline.sources.onshape import load_scene, normalize_scene, verify_normalization
from description_pipeline.sources.onshape.errors import (
    ATTRIBUTION_MISMATCH,
    DEFINITION_INVALID,
    SOURCE_CONFIG_INVALID,
    OnshapeSourceError,
)
from description_pipeline.sources.onshape.freeze import freeze, validate_source_config

from .helpers import ROOT_ELEMENT, URL, write_cache
from tests import onshape_fixture  # noqa: E402

FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "onshape" / "cache"
DEFINITION = Path(__file__).resolve().parents[2] / "fixtures" / "onshape" / "model" / "robot.yaml"
CAPTURE = {"evidence": "fixture", "reason": "仓库内回放夹具", "at": "2026-09-17T16:00:00Z"}


@onshape_fixture.requires_fixture
class MicrobanVerificationTests(unittest.TestCase):
    """真实 Microban 快照（仓库夹具）必须四条独立检查全绿。"""

    tmp: tempfile.TemporaryDirectory
    snapshot: Path
    scene: dict
    definition: dict
    model: dict
    results: list

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="onshape-verify-")
        cls.snapshot = Path(cls.tmp.name) / "snapshot"
        url = json.loads((FIXTURE / "source.json").read_text(encoding="utf-8"))["url"]
        freeze(
            {"url": url, "cache": str(FIXTURE), "offline": True, "capture": CAPTURE},
            cls.snapshot,
        )
        cls.scene = load_scene(cls.snapshot)
        cls.definition = yaml.safe_load(DEFINITION.read_text(encoding="utf-8"))
        cls.model = normalize_scene(cls.scene, cls.definition, cls.snapshot)
        cls.results = verify_normalization(cls.scene, cls.definition, cls.snapshot, cls.model)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def by_id(self, code: str) -> dict:
        return next(item for item in self.results if item["id"] == code)

    def test_all_independent_checks_pass_on_real_data(self):
        self.assertEqual(
            {item["id"] for item in self.results},
            {"source.occurrences", "source.entities", "source.exclusions", "source.mass_conservation"},
        )
        for item in self.results:
            with self.subTest(check=item["id"]):
                self.assertEqual(item["status"], "passed", item["details"])
                self.assertEqual(item["missing"], [])
                self.assertEqual(item["version"], 1)

    def test_occurrence_check_is_anchored_to_raw_assembly(self):
        details = self.by_id("source.occurrences")["details"]
        self.assertEqual(details["counts"]["occurrences"], 317)
        self.assertEqual(details["counts"]["part_instances"], 297)
        self.assertEqual(details["counts"]["assembly_instances"], 20)
        self.assertEqual(details["unresolved_paths"], [])

    def test_conservation_matches_within_tight_tolerance(self):
        details = self.by_id("source.mass_conservation")["details"]
        self.assertLess(details["total_error_kg"], 1e-12)
        self.assertLess(max(item["com_error_m"] for item in details["links"]), 1e-9)
        self.assertLess(max(item["inertia_error"] for item in details["links"]), 1e-12)
        self.assertEqual(len(details["links"]), 20)

    def test_exclusions_report_physical_impact(self):
        details = self.by_id("source.exclusions")["details"]
        self.assertEqual(details["declared"], 4)
        self.assertEqual(details["rejected"], [])
        self.assertGreater(details["excluded_mass_total_kg"], 0)
        for item in details["impact"]:
            with self.subTest(entity=item["entity"]):
                self.assertGreater(item["mass_kg"], 0)
                self.assertTrue(item["part_id"])
                self.assertTrue(item["consumed_by"])
                self.assertIn(item["consumed_by"][0]["link"], details["frame_links"])


@onshape_fixture.requires_fixture
class VerifyErrorInjectionTests(unittest.TestCase):
    """每个检查都必须能抓到对应类型的错误。"""

    tmp: tempfile.TemporaryDirectory
    snapshot: Path
    scene: dict
    definition: dict
    model: dict

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="onshape-inject-")
        cls.snapshot = Path(cls.tmp.name) / "snapshot"
        url = json.loads((FIXTURE / "source.json").read_text(encoding="utf-8"))["url"]
        freeze({"url": url, "cache": str(FIXTURE), "offline": True, "capture": CAPTURE}, cls.snapshot)
        cls.scene = load_scene(cls.snapshot)
        cls.definition = yaml.safe_load(DEFINITION.read_text(encoding="utf-8"))
        cls.model = normalize_scene(cls.scene, cls.definition, cls.snapshot)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def verify(self, scene=None, definition=None, model=None) -> dict:
        results = verify_normalization(
            scene or self.scene,
            definition or self.definition,
            self.snapshot,
            model or self.model,
        )
        return {item["id"]: item for item in results}

    def test_dropped_occurrence_is_caught(self):
        scene = copy.deepcopy(self.scene)
        scene["provenance"]["expected_occurrences"] = scene["provenance"]["expected_occurrences"][1:]
        result = self.verify(scene=scene)["source.occurrences"]
        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["details"]["occurrences_missing"])

    def test_dropped_entity_is_caught(self):
        scene = copy.deepcopy(self.scene)
        scene["provenance"]["expected_entities"] = scene["provenance"]["expected_entities"][1:]
        result = self.verify(scene=scene)["source.occurrences"]
        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["details"]["entities_missing"])

    def test_wrong_counts_are_caught(self):
        scene = copy.deepcopy(self.scene)
        scene["provenance"]["entity_counts"]["occurrences"] = 300
        result = self.verify(scene=scene)["source.occurrences"]
        self.assertEqual(result["status"], "failed")
        self.assertIn("occurrences", result["details"]["counts_mismatch"])

    def test_unjustified_exclusion_is_caught_by_verification(self):
        model = copy.deepcopy(self.model)
        link = next(item for item in model["links"] if len(item["provenance"]["source_entities"]) > 1)
        victim = link["provenance"]["source_entities"][0]
        link["provenance"]["source_entities"] = link["provenance"]["source_entities"][1:]
        model["provenance"]["excluded_entities"].append(
            {"id": victim, "reason": "author_wants_it_gone", "evidence": {"consumed_by": []}}
        )
        results = self.verify(model=model)
        self.assertEqual(results["source.entities"]["status"], "passed")
        self.assertEqual(results["source.exclusions"]["status"], "failed")
        self.assertEqual(results["source.exclusions"]["details"]["rejected"][0]["entity"], victim)

    def test_exclusion_without_physical_impact_is_caught(self):
        model = copy.deepcopy(self.model)
        entry = model["provenance"]["excluded_entities"][0]
        entry.pop("mass_kg")
        result = self.verify(model=model)["source.exclusions"]
        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["details"]["rejected"])

    def test_exclusion_mass_claim_tamper_is_caught(self):
        """排除项质量必须由 raw 独立复算，不能只判字段存在。"""

        for value in (0.0, 1e-9, self.model["provenance"]["excluded_entities"][0]["mass_kg"] * 2):
            with self.subTest(value=value):
                model = copy.deepcopy(self.model)
                model["provenance"]["excluded_entities"][0]["mass_kg"] = value
                result = self.verify(model=model)["source.exclusions"]
                self.assertEqual(result["status"], "failed")
                self.assertIn("mass_claim_mismatch", result["details"]["rejected"][0]["problems"])

    def test_exclusion_part_id_claim_tamper_is_caught(self):
        model = copy.deepcopy(self.model)
        model["provenance"]["excluded_entities"][0]["part_id"] = "WRONG"
        result = self.verify(model=model)["source.exclusions"]
        self.assertEqual(result["status"], "failed")
        self.assertIn("part_id_claim_mismatch", result["details"]["rejected"][0]["problems"])

    def test_consumer_pair_mismatch_is_caught(self):
        model = copy.deepcopy(self.model)
        entry = model["provenance"]["excluded_entities"][0]
        entry["evidence"]["consumed_by"] = [
            {"link": "body", "joint": "left_foot_frame", "mate": "frame_left_foot_frame"}
        ]
        result = self.verify(model=model)["source.exclusions"]
        self.assertEqual(result["status"], "failed")
        problems = result["details"]["rejected"][0]["problems"]
        self.assertTrue(any(item.startswith("consumer_pair_mismatch") for item in problems))

    def test_consumer_mate_must_actually_consume_the_entity(self):
        model = copy.deepcopy(self.model)
        entry = model["provenance"]["excluded_entities"][0]
        entry["evidence"]["consumed_by"] = [{"link": "body", "joint": "body_frame", "mate": "frame_imu_frame"}]
        result = self.verify(model=model)["source.exclusions"]
        self.assertEqual(result["status"], "failed")
        problems = result["details"]["rejected"][0]["problems"]
        self.assertIn("mate_does_not_consume:frame_imu_frame", problems)

    def test_unknown_mate_is_caught(self):
        model = copy.deepcopy(self.model)
        entry = model["provenance"]["excluded_entities"][0]
        entry["evidence"]["consumed_by"] = [{"link": "body", "joint": "body_frame", "mate": "frame_not_a_mate"}]
        result = self.verify(model=model)["source.exclusions"]
        self.assertEqual(result["status"], "failed")
        self.assertIn("unknown_mate:frame_not_a_mate", result["details"]["rejected"][0]["problems"])

    def test_evidence_file_must_be_bound_and_digest_matching(self):
        """证据文件必须在快照清单内，且摘要要与清单一致。"""

        outside = Path(self.tmp.name) / "outside-evidence.json"
        outside.write_text("{}", encoding="utf-8")
        model = copy.deepcopy(self.model)
        entry = model["provenance"]["excluded_entities"][0]
        entry["evidence"]["file"] = "../../outside-evidence.json"
        result = self.verify(model=model)["source.exclusions"]
        self.assertEqual(result["status"], "failed")
        self.assertIn("evidence_file_not_bound", result["details"]["rejected"][0]["problems"])

        model = copy.deepcopy(self.model)
        entry = model["provenance"]["excluded_entities"][0]
        entry["evidence"]["file"] = "scene.json"
        entry["evidence"]["sha256"] = "0" * 64
        result = self.verify(model=model)["source.exclusions"]
        self.assertEqual(result["status"], "failed")
        self.assertIn("evidence_digest_mismatch", result["details"]["rejected"][0]["problems"])

    def test_geometry_claim_must_be_in_inventory(self):
        model = copy.deepcopy(self.model)
        entry = model["provenance"]["excluded_entities"][0]
        entry["source_geometry"] = "geometry/parts/NOPE.stl"
        result = self.verify(model=model)["source.exclusions"]
        self.assertEqual(result["status"], "failed")
        self.assertIn("geometry_not_in_inventory", result["details"]["rejected"][0]["problems"])

    def test_derived_scene_file_is_not_accepted_as_independent_evidence(self):
        """校验侧与生成侧同一判据：快照自带的 scene.json 不是独立证据。"""

        manifest = json.loads((self.snapshot / "manifest.json").read_text(encoding="utf-8"))
        model = copy.deepcopy(self.model)
        entry = model["provenance"]["excluded_entities"][0]
        entry["evidence"]["file"] = "scene.json"
        entry["evidence"]["sha256"] = manifest["files"]["scene.json"]
        result = self.verify(model=model)["source.exclusions"]
        self.assertEqual(result["status"], "failed")
        self.assertIn("evidence_not_capture_layer", result["details"]["rejected"][0]["problems"])

    def test_unrelated_capture_file_is_not_accepted_as_evidence(self):
        """采集层文件也必须与实体绑定：内容不含该实体的记录不算证据。"""

        note = self.snapshot / "raw" / "jig-note.json"
        note.write_text('{"entity": "some-other-part", "note": "与本次排除无关"}', encoding="utf-8")
        manifest_path = self.snapshot / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["files"]["raw/jig-note.json"] = hashlib.sha256(note.read_bytes()).hexdigest()
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        model = copy.deepcopy(self.model)
        entry = model["provenance"]["excluded_entities"][0]
        entry["evidence"]["file"] = "raw/jig-note.json"
        entry["evidence"]["sha256"] = manifest["files"]["raw/jig-note.json"]
        result = self.verify(model=model)["source.exclusions"]
        self.assertEqual(result["status"], "failed")
        self.assertIn("evidence_not_bound_to_entity", result["details"]["rejected"][0]["problems"])

    def test_mass_tamper_is_caught(self):
        for mutate in (
            lambda model: model["links"][0]["inertial"].__setitem__(
                "mass", model["links"][0]["inertial"]["mass"] * 1.001
            ),
            lambda model: model["links"][0]["inertial"].__setitem__(
                "inertia", [value * 1.5 for value in model["links"][0]["inertial"]["inertia"]]
            ),
            lambda model: model["links"][0]["inertial"].__setitem__("xyz", [0.05, 0.0, 0.0]),
        ):
            model = copy.deepcopy(self.model)
            mutate(model)
            result = self.verify(model=model)["source.mass_conservation"]
            self.assertEqual(result["status"], "failed")
            self.assertTrue(result["details"]["failures"])

    def test_coordinate_tamper_in_joint_frame_is_caught(self):
        model = copy.deepcopy(self.model)
        joint = next(item for item in model["joints"] if item["type"] == "revolute")
        joint["xyz"] = [joint["xyz"][0] + 0.01, joint["xyz"][1], joint["xyz"][2]]
        result = self.verify(model=model)["source.mass_conservation"]
        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["details"]["failures"])

    def test_raw_mass_tamper_is_caught(self):
        """改原始质量读数（模型不变）必须被抓到：证明校验真的锚在 raw 上。"""

        tampered = Path(self.tmp.name) / "tampered"
        shutil.copytree(self.snapshot, tampered)
        target = next((tampered / "raw").glob("mass_properties_*.json"))
        payload = json.loads(target.read_text(encoding="utf-8"))
        part_id = next(iter(payload["bodies"]))
        payload["bodies"][part_id]["mass"] = [value * 1.25 for value in payload["bodies"][part_id]["mass"]]
        # 密度假设下质量由体积推出，所以体积也要改：两者任一被改都必须被发现。
        if payload["bodies"][part_id].get("volume"):
            payload["bodies"][part_id]["volume"] = [value * 1.25 for value in payload["bodies"][part_id]["volume"]]
        target.write_text(json.dumps(payload), encoding="utf-8")
        results = verify_normalization(self.scene, self.definition, tampered, self.model)
        self.assertEqual({item["id"]: item["status"] for item in results}["source.mass_conservation"], "failed")


def synthetic_definition(*, extra_member: str | None = None) -> dict:
    """合成装配的最小定义：base/arm/extra 三个刚体 + 参考系。"""

    members = ["base"]
    if extra_member:
        members.append(extra_member)
    return {
        "robot": {
            "schema": "description.robot-definition/v1",
            "provider": "onshape",
            "root": "base",
            "links": [
                {"name": "base", "members": members},
                {"name": "arm", "members": ["arm"]},
                {"name": "extra", "members": ["extra"]},
            ],
            "frames": [
                {"name": "body_frame", "link": "body", "mate": "frame_body_frame", "parent": "base"},
                # 合成夹具里的非物理 mate：显式声明为参考系，保持"每个 mate 都有归属"
                {"name": "cyl_reference", "link": "cyl_reference", "mate": "dof_cyl", "parent": "base"},
                {"name": "orphan_reference", "link": "orphan_reference", "mate": "dof_orphan", "parent": "base"},
            ],
            "joints": [
                {
                    "name": "hinge",
                    "mate": "dof_hinge",
                    "parent": "base",
                    "child": "arm",
                    "type": "revolute",
                    "axis": {"source": "mate_connector_z", "sign": 1},
                    "limits": {"source": "mate_limits"},
                    "zero": {"value": 0.0, "source": "cad"},
                    "source": {},
                },
                {
                    "name": "free",
                    "mate": "dof_free",
                    "parent": "base",
                    "child": "extra",
                    "type": "revolute",
                    "axis": {"source": "mate_connector_z", "sign": 1},
                    "limits": {"source": "none"},
                    "zero": {"value": 0.0, "source": "cad"},
                    "source": {},
                },
            ],
            "non_physical": [],
            "mass": {"overrides": []},
        }
    }


class SuppressedInstanceTests(unittest.TestCase):
    """抑制实例不建模，但必须在 non_physical 里留痕，且不阻断守恒对账。"""

    tmp: Path
    cache: Path
    scene: dict

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="onshape-suppressed-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.cache = write_cache(self.tmp / "cache")
        # 合成夹具里的 nomass 零件没有质量读数：它不参与本用例，移除以免触发
        # "排除影响不可复算"（那属于另一条严格门禁）。
        assembly_path = self.cache / "json" / f"assembly_{ROOT_ELEMENT}.json"
        payload = json.loads(assembly_path.read_text(encoding="utf-8"))
        payload["rootAssembly"]["instances"] = [
            item for item in payload["rootAssembly"]["instances"] if item["id"] != "nomass"
        ]
        payload["rootAssembly"]["occurrences"] = [
            item for item in payload["rootAssembly"]["occurrences"] if item["path"] != ["nomass"]
        ]
        assembly_path.write_text(json.dumps(payload), encoding="utf-8")
        freeze(
            {
                "url": URL,
                "cache": str(self.cache),
                "offline": True,
                "capture": {"evidence": "fixture", "reason": "抑制实例回归"},
            },
            self.tmp / "snapshot",
        )
        self.scene = load_scene(self.tmp / "snapshot")

    def test_duplicate_mate_name_is_rejected_instead_of_binding_the_first(self):
        """同名 mate 用名字寻址必须失败：静默取第一个会把关节连到错误零件上。"""

        cache = self.tmp / "duplicate-cache"
        shutil.copytree(self.cache, cache)
        assembly_path = cache / "json" / f"assembly_{ROOT_ELEMENT}.json"
        payload = json.loads(assembly_path.read_text(encoding="utf-8"))
        for feature in payload["rootAssembly"]["features"]:
            if feature.get("featureData", {}).get("name") == "dof_free":
                feature["featureData"]["name"] = "dof_hinge"  # 与 dof_hinge 同名，名字不再唯一
        assembly_path.write_text(json.dumps(payload), encoding="utf-8")
        freeze(
            {
                "url": URL,
                "cache": str(cache),
                "offline": True,
                "capture": {"evidence": "fixture", "reason": "同名 mate 回归"},
            },
            self.tmp / "duplicate-snapshot",
        )
        scene = load_scene(self.tmp / "duplicate-snapshot")
        with self.assertRaises(OnshapeSourceError) as caught:
            normalize_scene(scene, synthetic_definition(), self.tmp / "duplicate-snapshot")
        self.assertEqual(caught.exception.code, DEFINITION_INVALID)
        self.assertEqual(caught.exception.detail["mate"], "dof_hinge")
        self.assertEqual(len(caught.exception.detail["candidates"]), 2)

    def definition(self, *, extra_member: str | None = None) -> dict:
        return synthetic_definition(extra_member=extra_member)

    def test_suppressed_instance_is_listed_and_kept_out_of_expected_entities(self):
        provenance = self.scene["provenance"]
        self.assertEqual(provenance["non_physical"]["suppressed"], ["spare"])
        self.assertNotIn("spare", provenance["expected_entities"])
        self.assertIn("spare", provenance["expected_occurrences"])
        self.assertEqual(provenance["entity_counts"]["suppressed_instances"], 1)
        self.assertEqual(provenance["entity_counts"]["part_instances"], 4)
        self.assertEqual(provenance["entity_counts"]["occurrences"], 5)
        self.assertNotIn("spare", [link["id"] for link in self.scene["links"]])

    def test_normalize_and_oracle_account_for_the_suppressed_instance(self):
        model = normalize_scene(self.scene, self.definition(), self.tmp / "snapshot")
        attribution = model["provenance"]["attribution"]
        self.assertEqual(attribution["suppressed_instances"], 1)
        self.assertNotIn("spare", attribution["non_physical"])
        checks = {
            item["id"]: item
            for item in verify_normalization(self.scene, self.definition(), self.tmp / "snapshot", model)
        }
        self.assertEqual(checks["source.occurrences"]["status"], "passed")
        self.assertEqual(checks["source.entities"]["status"], "passed")

    def test_suppressed_instance_cannot_be_silently_adopted_into_a_group(self):
        with self.assertRaises(OnshapeSourceError) as caught:
            normalize_scene(self.scene, self.definition(extra_member="spare"), self.tmp / "snapshot")
        self.assertEqual(caught.exception.code, ATTRIBUTION_MISMATCH)
        self.assertEqual(caught.exception.detail["unexpected"][0]["selector"], "spare")


class MassConservationOracleTests(unittest.TestCase):
    """惯量表达系与容限量级的解析回归（质量守恒 oracle 自己的判据）。"""

    tmp: Path
    cache: Path
    scene: dict
    definition: dict

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="onshape-conservation-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.cache = write_cache(self.tmp / "cache")
        assembly_path = self.cache / "json" / f"assembly_{ROOT_ELEMENT}.json"
        payload = json.loads(assembly_path.read_text(encoding="utf-8"))
        payload["rootAssembly"]["instances"] = [
            item for item in payload["rootAssembly"]["instances"] if item["id"] != "nomass"
        ]
        payload["rootAssembly"]["occurrences"] = [
            item for item in payload["rootAssembly"]["occurrences"] if item["path"] != ["nomass"]
        ]
        assembly_path.write_text(json.dumps(payload), encoding="utf-8")
        # 小张量回归：把零件惯量压到 1e-9 量级（质量仍是 kg 级），旧的"用质量当量级"会放松容限。
        for path in sorted((self.cache / "json").glob("mass_properties_*.json")):
            bodies = json.loads(path.read_text(encoding="utf-8"))["bodies"]
            for body in bodies.values():
                if body.get("inertia"):
                    # 各向异性小张量：旋转表达系才可分辨（各向同性转不转都一样）
                    body["inertia"] = [1e-9, 0.0, 0.0, 0.0, 2e-9, 0.0, 0.0, 0.0, 3e-9] * 3
            path.write_text(json.dumps({"bodies": bodies}), encoding="utf-8")
        freeze(
            {
                "url": URL,
                "cache": str(self.cache),
                "offline": True,
                "capture": {"evidence": "fixture", "reason": "守恒 oracle 回归"},
            },
            self.tmp / "snapshot",
        )
        self.scene = load_scene(self.tmp / "snapshot")
        self.definition = synthetic_definition()

    def model(self) -> dict:
        return normalize_scene(self.scene, self.definition, self.tmp / "snapshot")

    def conservation(self, model: dict) -> dict:
        checks = verify_normalization(self.scene, self.definition, self.tmp / "snapshot", model)
        return next(item for item in checks if item["id"] == "source.mass_conservation")

    def link_of(self, model: dict) -> dict:
        return next(link for link in model["links"] if link["inertial"])

    def test_rotated_inertial_frame_is_equivalent(self):
        model = self.model()
        link = self.link_of(model)
        rpy = (0.0, 0.0, math.pi / 2.0)
        rotation = np.asarray(_rpy_matrix(rpy))
        tensor = _tensor_from_six(link["inertial"]["inertia"])
        expressed = rotation.T @ tensor @ rotation
        link["inertial"]["rpy"] = list(rpy)
        link["inertial"]["inertia"] = _flatten_six(expressed)
        result = self.conservation(model)
        self.assertEqual(result["status"], "passed", result["details"]["links"])

    def test_wrong_inertial_frame_fails(self):
        model = self.model()
        link = self.link_of(model)
        link["inertial"]["rpy"] = [0.0, 0.0, math.pi / 2.0]  # 张量没跟着转
        result = self.conservation(model)
        self.assertEqual(result["status"], "failed")
        self.assertIn(link["name"], result["details"]["failures"])

    def test_small_tensor_relative_error_is_caught_despite_kilogram_mass(self):
        model = self.model()
        link = self.link_of(model)
        self.assertGreater(link["inertial"]["mass"], 1.0 - 1e-9)  # 质量是 kg 级
        link["inertial"]["inertia"] = [value * 2.0 for value in link["inertial"]["inertia"]]
        result = self.conservation(model)
        entry = next(item for item in result["details"]["links"] if item["link"] == link["name"])
        self.assertEqual(result["status"], "failed")
        self.assertLess(entry["tensor_scale"], 1e-8, "量级必须来自张量自身，而不是质量")
        # 旧公式会用到 max(质量=1.5 kg, 1.0) → 容限 ≥1e-6；新的只取张量范数
        self.assertLess(entry["inertia_tolerance"], 1e-10)
        self.assertGreater(entry["inertia_error"], 1e-9)
        self.assertGreater(entry["inertia_error"], entry["inertia_tolerance"])

    def test_unprocessed_link_is_reported_as_missing(self):
        model = self.model()
        link = self.link_of(model)
        link["provenance"]["source_entities"] = ["does/not/exist"]
        result = self.conservation(model)
        self.assertEqual(result["status"], "failed")
        self.assertIn(link["name"], result["missing"])
        self.assertNotIn(link["name"], result["checked"])
        self.assertIn(f"{link['name']}:no-members", result["details"]["failures"])

    def test_expected_covers_every_link_with_inertia(self):
        model = self.model()
        result = self.conservation(model)
        expected = sorted(link["name"] for link in model["links"] if link["inertial"])
        self.assertEqual(result["expected"], expected)
        self.assertEqual(result["missing"], [])
        self.assertEqual(result["status"], "passed", result["details"]["links"])


def _rpy_matrix(rpy):
    roll, pitch, yaw = rpy
    cr, sr = math.cos(roll), math.sin(roll)
    cp, sp = math.cos(pitch), math.sin(pitch)
    cy, sy = math.cos(yaw), math.sin(yaw)
    return [
        [cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr],
        [sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr],
        [-sp, cp * sr, cp * cr],
    ]


def _tensor_from_six(values):
    ixx, ixy, ixz, iyy, iyz, izz = values
    return np.array([[ixx, ixy, ixz], [ixy, iyy, iyz], [ixz, iyz, izz]])


def _flatten_six(tensor):
    return [
        float(tensor[0][0]),
        float(tensor[0][1]),
        float(tensor[0][2]),
        float(tensor[1][1]),
        float(tensor[1][2]),
        float(tensor[2][2]),
    ]


@onshape_fixture.requires_fixture
class StrictExclusionImpactTests(unittest.TestCase):
    """已排除的 active 实例必须能复算排除影响；算不出来一律阻断（未知影响不放行）。"""

    tmp: tempfile.TemporaryDirectory
    snapshot: Path
    scene: dict
    definition: dict
    model: dict

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="onshape-strict-impact-")
        cls.snapshot = Path(cls.tmp.name) / "snapshot"
        url = json.loads((FIXTURE / "source.json").read_text(encoding="utf-8"))["url"]
        freeze({"url": url, "cache": str(FIXTURE), "offline": True, "capture": CAPTURE}, cls.snapshot)
        cls.scene = load_scene(cls.snapshot)
        cls.definition = yaml.safe_load(DEFINITION.read_text(encoding="utf-8"))
        cls.model = normalize_scene(cls.scene, cls.definition, cls.snapshot)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_oracle_blocks_when_exclusion_impact_cannot_be_recomputed(self):
        """原始读数里没有质量时，排除影响不可复算 → 必须阻断（不写 null 放行）。"""

        victim = next(link for link in self.model["links"] if link["inertial"])
        entity = victim["provenance"]["source_entities"][0]
        scene_link = next(link for link in self.scene["links"] if link["id"] == entity)
        part_id = scene_link["provenance"]["source_part"]["part_id"]
        element_id = scene_link["provenance"]["source_part"]["element_id"]
        tampered = Path(self.tmp.name) / "tampered"
        shutil.copytree(self.snapshot, tampered)
        path = tampered / "raw" / f"mass_properties_{element_id}.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["bodies"][part_id] = {"hasMass": False, "massMissingCount": 1}
        path.write_text(json.dumps(payload), encoding="utf-8")
        model = copy.deepcopy(self.model)
        entry = model["provenance"]["excluded_entities"][0]
        entry["id"] = entity
        entry["part_id"] = part_id
        entry["mass_kg"] = None
        checks = {item["id"]: item for item in verify_normalization(self.scene, self.definition, tampered, model)}
        exclusions = checks["source.exclusions"]
        self.assertEqual(exclusions["status"], "failed")
        problems = [item for e in exclusions["details"]["rejected"] for item in e["problems"]]
        self.assertIn("impact_not_recomputable", problems)

    def test_oracle_blocks_when_exclusion_mass_is_not_a_number(self):
        model = copy.deepcopy(self.model)
        entry = model["provenance"]["excluded_entities"][0]
        entry["mass_kg"] = None
        checks = {item["id"]: item for item in verify_normalization(self.scene, self.definition, self.snapshot, model)}
        self.assertEqual(checks["source.exclusions"]["status"], "failed")
        problems = [item for e in checks["source.exclusions"]["details"]["rejected"] for item in e["problems"]]
        self.assertIn("mass_claim_mismatch", problems)


class StrictExclusionGenerationTests(unittest.TestCase):
    """生成侧：声明排除一个无法复算质量的零件必须直接失败，而不是写 null 通过。"""

    tmp: Path
    cache: Path
    scene: dict

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="onshape-strict-gen-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.cache = write_cache(self.tmp / "cache")
        freeze(
            {
                "url": URL,
                "cache": str(self.cache),
                "offline": True,
                "capture": {"evidence": "fixture", "reason": "严格排除门禁"},
            },
            self.tmp / "snapshot",
        )
        self.scene = load_scene(self.tmp / "snapshot")

    def test_excluding_a_part_without_mass_reading_is_rejected(self):
        definition = synthetic_definition()
        definition["robot"]["non_physical"].append(
            {"entities": ["nomass"], "basis": "no_mass", "evidence": "scene.json"}
        )
        with self.assertRaises(OnshapeSourceError) as caught:
            normalize_scene(self.scene, definition, self.tmp / "snapshot")
        self.assertEqual(caught.exception.code, "onshape_evidence_missing")


class SourceConfigAllowlistTests(unittest.TestCase):
    def test_unknown_key_is_rejected(self):
        with self.assertRaises(OnshapeSourceError) as caught:
            validate_source_config({"url": "https://x", "offline_typo": True})
        self.assertEqual(caught.exception.code, SOURCE_CONFIG_INVALID)
        self.assertEqual(caught.exception.detail["unknown"], ["offline_typo"])

    def test_wrong_types_are_rejected(self):
        for payload in (
            {"url": "https://x", "offline": "yes"},
            {"url": "https://x", "include_geometry": 1},
            {"url": "https://x", "tolerance": 0},
            {"url": "https://x", "tolerance": float("inf")},
            {"url": "https://x", "elements": "studio"},
            {"url": "https://x", "capture": []},
            {"url": "https://x", "provider": "solidworks"},
        ):
            with self.subTest(payload=payload), self.assertRaises(OnshapeSourceError) as caught:
                validate_source_config(payload)
            self.assertEqual(caught.exception.code, SOURCE_CONFIG_INVALID)

    def test_supported_keys_pass(self):
        validate_source_config(
            {
                "provider": "onshape",
                "url": "https://cad.onshape.com/documents/d/w/w1/e/e1",
                "configuration": "default",
                "cache": "/tmp/cache",
                "offline": True,
                "include_geometry": True,
                "tolerance": 0.05,
                "elements": ["studio"],
                "capture": CAPTURE,
            }
        )


@onshape_fixture.requires_fixture
class ExclusionPolicyTests(unittest.TestCase):
    """定义层的排除策略：无消费方且无独立证据的排除必须直接失败。"""

    tmp: tempfile.TemporaryDirectory
    snapshot: Path
    scene: dict
    definition: dict
    model: dict

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="onshape-exclusion-")
        cls.snapshot = Path(cls.tmp.name) / "snapshot"
        url = json.loads((FIXTURE / "source.json").read_text(encoding="utf-8"))["url"]
        freeze({"url": url, "cache": str(FIXTURE), "offline": True, "capture": CAPTURE}, cls.snapshot)
        cls.scene = load_scene(cls.snapshot)
        cls.definition = yaml.safe_load(DEFINITION.read_text(encoding="utf-8"))
        cls.model = normalize_scene(cls.scene, cls.definition, cls.snapshot)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def victim(self) -> tuple[str, str]:
        link = next(item for item in self.model["links"] if len(item["provenance"]["source_entities"]) > 1)
        return link["name"], link["provenance"]["source_entities"][0]

    def victim_evidence_file(self, victim: str) -> str:
        """该实例自己的质量读数：内容含它的 partId，才够格当绑定证据。"""

        link = next(item for item in self.scene["links"] if item["id"] == victim)
        part = link["provenance"]["source_part"]
        relative = f"raw/mass_properties_{part['element_id']}.json"
        self.assertIn(part["part_id"], (self.snapshot / relative).read_text(encoding="utf-8"))
        return relative

    def test_unjustified_exclusion_is_rejected(self):
        definition = copy.deepcopy(self.definition)
        _link, victim = self.victim()
        definition["robot"]["non_physical"].append(
            {"entities": [victim], "basis": "author_wants_it_gone", "detail": "无消费方、无证据"}
        )
        with self.assertRaises(OnshapeSourceError) as caught:
            normalize_scene(self.scene, definition, self.snapshot)
        self.assertEqual(caught.exception.code, ATTRIBUTION_MISMATCH)
        self.assertEqual(caught.exception.detail["entity"], victim)

    def test_exclusion_with_declared_evidence_file_is_allowed(self):
        definition = copy.deepcopy(self.definition)
        _link, victim = self.victim()
        evidence = self.victim_evidence_file(victim)
        definition["robot"]["non_physical"].append(
            {
                "entities": [victim],
                "basis": "documented_jig",
                "detail": "独立证据文件说明",
                "evidence": evidence,
            }
        )
        model = normalize_scene(self.scene, definition, self.snapshot)
        entry = next(item for item in model["provenance"]["excluded_entities"] if item["id"] == victim)
        self.assertEqual(entry["evidence"]["file"], evidence)
        self.assertTrue(entry["part_id"])
        self.assertGreater(entry["mass_kg"], 0)
        results = {item["id"]: item for item in verify_normalization(self.scene, definition, self.snapshot, model)}
        self.assertEqual(results["source.exclusions"]["status"], "passed")

    def test_missing_evidence_file_is_rejected(self):
        definition = copy.deepcopy(self.definition)
        _link, victim = self.victim()
        definition["robot"]["non_physical"].append(
            {"entities": [victim], "basis": "documented_jig", "evidence": "raw/does-not-exist.json"}
        )
        with self.assertRaises(OnshapeSourceError) as caught:
            normalize_scene(self.scene, definition, self.snapshot)
        self.assertEqual(caught.exception.detail["evidence"], "raw/does-not-exist.json")

    def test_escaped_evidence_path_is_rejected(self):
        """证据文件不能逃出快照目录。"""

        outside = Path(self.tmp.name) / "outside-evidence.json"
        outside.write_text("{}", encoding="utf-8")
        definition = copy.deepcopy(self.definition)
        _link, victim = self.victim()
        escaped = os.path.relpath(outside, self.snapshot)
        self.assertTrue(escaped.startswith(".."))
        definition["robot"]["non_physical"].append(
            {"entities": [victim], "basis": "documented_jig", "evidence": escaped}
        )
        with self.assertRaises(OnshapeSourceError) as caught:
            normalize_scene(self.scene, definition, self.snapshot)
        self.assertEqual(caught.exception.detail["evidence"], escaped)
        self.assertIn("越界", caught.exception.message)

    def test_unbound_evidence_file_is_rejected(self):
        """快照内但不在清单里的文件不算证据（清单绑定）。"""

        definition = copy.deepcopy(self.definition)
        _link, victim = self.victim()
        extra = self.snapshot / "raw" / "extra-evidence.json"
        extra.write_text('{"not": "in manifest"}', encoding="utf-8")
        definition["robot"]["non_physical"].append(
            {"entities": [victim], "basis": "documented_jig", "evidence": "raw/extra-evidence.json"}
        )
        with self.assertRaises(OnshapeSourceError) as caught:
            normalize_scene(self.scene, definition, self.snapshot)
        self.assertIn("清单", caught.exception.message)
        self.assertEqual(caught.exception.detail["evidence"], "raw/extra-evidence.json")

    def test_bound_evidence_records_manifest_digest(self):
        definition = copy.deepcopy(self.definition)
        _link, victim = self.victim()
        evidence = self.victim_evidence_file(victim)
        definition["robot"]["non_physical"].append(
            {"entities": [victim], "basis": "documented_jig", "evidence": evidence}
        )
        model = normalize_scene(self.scene, definition, self.snapshot)
        entry = next(item for item in model["provenance"]["excluded_entities"] if item["id"] == victim)
        manifest = json.loads((self.snapshot / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(entry["evidence"]["sha256"], manifest["files"][evidence])

    def test_derived_scene_file_is_not_independent_evidence(self):
        """流水线自己生成的 scene.json 不能当独立证据（循环论证）。"""

        definition = copy.deepcopy(self.definition)
        _link, victim = self.victim()
        definition["robot"]["non_physical"].append(
            {"entities": [victim], "basis": "documented_jig", "evidence": "scene.json"}
        )
        with self.assertRaises(OnshapeSourceError) as caught:
            normalize_scene(self.scene, definition, self.snapshot)
        self.assertEqual(caught.exception.code, "onshape_evidence_missing")
        self.assertIn("evidence_not_capture_layer", caught.exception.detail["problems"])

    def test_evidence_file_must_mention_the_excluded_entity(self):
        """清单里的原始响应只与实体绑定后才是证据：内容不含该实体一律拒绝。"""

        snapshot = Path(self.tmp.name) / "unbound-evidence"
        shutil.copytree(self.snapshot, snapshot)
        note = snapshot / "raw" / "jig-note.json"
        note.write_text('{"entity": "some-other-part", "note": "与本次排除无关"}', encoding="utf-8")
        manifest_path = snapshot / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["files"]["raw/jig-note.json"] = hashlib.sha256(note.read_bytes()).hexdigest()
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        definition = copy.deepcopy(self.definition)
        _link, victim = self.victim()
        definition["robot"]["non_physical"].append(
            {"entities": [victim], "basis": "documented_jig", "evidence": "raw/jig-note.json"}
        )
        with self.assertRaises(OnshapeSourceError) as caught:
            normalize_scene(self.scene, definition, snapshot)
        self.assertEqual(caught.exception.code, "onshape_evidence_missing")
        self.assertIn("evidence_not_bound_to_entity", caught.exception.detail["problems"])

    def test_tolerance_field_is_no_longer_accepted(self):
        definition = copy.deepcopy(self.definition)
        definition["robot"]["tolerance_m"] = 0.01
        with self.assertRaises(OnshapeSourceError) as caught:
            normalize_scene(self.scene, definition, self.snapshot)
        self.assertEqual(caught.exception.detail["unknown"], ["tolerance_m"])


if __name__ == "__main__":
    unittest.main()
