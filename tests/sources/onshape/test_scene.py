"""来源语义 → ``description.scene/v1``：覆盖、命名、读数与 gaps。"""

import hashlib
import json
import re
import tempfile
import unittest
from pathlib import Path

from description_pipeline.sources.onshape import scene as scene_module
from description_pipeline.sources.onshape.cache import CachedFetcher, ResponseCache
from description_pipeline.sources.onshape.collector import Collection, collect
from description_pipeline.sources.onshape.freeze import _validate_scene
from description_pipeline.sources.onshape.reference import parse_reference

from .helpers import URL, write_cache

NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")


def synthetic_scene(root: Path, prepare=None) -> tuple[dict, Collection]:
    cache_root = write_cache(root / "cache")
    if prepare is not None:
        prepare(cache_root)
    cache = ResponseCache(cache_root, read_only=True)
    collection = collect(
        parse_reference(URL),
        CachedFetcher(None, cache),
        configuration="default",
        tolerance=0.05,
        include_geometry=True,
    )
    return scene_module.build_scene(collection), collection


def gap_kinds(scene: dict) -> list[str]:
    return [gap["kind"] for gap in scene["provenance"]["gaps"]]


def link_by_id(scene: dict, link_id: str) -> dict:
    return next(link for link in scene["links"] if link["id"] == link_id)


class SceneTests(unittest.TestCase):
    tmp: tempfile.TemporaryDirectory
    scene: dict
    collection: Collection

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="onshape-scene-")
        cls.scene, cls.collection = synthetic_scene(Path(cls.tmp.name))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_scene_is_the_shared_contract_shape(self):
        self.assertEqual(self.scene["schema_version"], "description.scene/v1")
        self.assertEqual(self.scene["name"], "robot")
        self.assertEqual(self.scene["units"], "SI")
        _validate_scene(self.scene)

    def test_every_leaf_instance_is_a_link_and_every_link_is_expected(self):
        provenance = self.scene["provenance"]
        expected = provenance["expected_entities"]
        ids = [link["id"] for link in self.scene["links"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(set(ids), {"base", "arm/arm_1/child", "extra", "nomass"})
        for link in self.scene["links"]:
            self.assertEqual(link["provenance"]["source_entities"], [link["id"]])
            self.assertIn(link["id"], expected)
        # 物理零件实例、装配容器与抑制实例分开对账：后两者都不进模型但必须留痕。
        self.assertEqual(expected, ["base", "arm/arm_1/child", "extra", "nomass"])
        self.assertEqual(
            provenance["expected_occurrences"],
            ["base", "arm", "arm/arm_1/child", "extra", "nomass", "spare"],
        )
        self.assertEqual(provenance["non_physical"]["containers"], ["arm"])
        self.assertEqual(provenance["non_physical"]["suppressed"], ["spare"])
        self.assertEqual(
            provenance["entity_counts"],
            {
                "occurrences": 6,
                "part_instances": 5,
                "assembly_instances": 1,
                "suppressed_instances": 1,
                "links": 4,
                "part_studios": 2,
            },
        )

    def test_names_are_schema_safe_unique_and_keep_the_source_name(self):
        names = [link["name"] for link in self.scene["links"]]
        self.assertEqual(names, ["Base", "Arm_link", "Extra_1", "No_mass"])
        self.assertEqual(len(names), len(set(names)))
        for name in names:
            self.assertRegex(name, NAME_PATTERN)
        self.assertEqual(link_by_id(self.scene, "arm/arm_1/child")["provenance"]["source_name"], "Arm link <1>")

    def test_inertial_uses_the_nominal_reading_in_contract_order(self):
        link = link_by_id(self.scene, "base")
        self.assertEqual(link["inertial"]["mass"], 1.5)
        self.assertEqual(link["inertial"]["xyz"], [0.0, 0.0, 0.05])
        self.assertEqual(link["inertial"]["rpy"], [0.0, 0.0, 0.0])
        self.assertEqual(link["inertial"]["inertia"], [1e-4, 0.0, 0.0, 1e-4, 0.0, 1e-4])
        self.assertEqual(link["provenance"]["source_part"]["part_id"], "PART_A")
        self.assertEqual(link["provenance"]["geometry_source"], "cached_part_stl")

    def test_missing_mass_leaves_inertial_empty_and_is_recorded(self):
        link = link_by_id(self.scene, "nomass")
        self.assertIsNone(link["inertial"])
        self.assertEqual(link["visuals"], [])
        self.assertIn("mass_reading_missing", gap_kinds(self.scene))
        self.assertIn("geometry_missing", gap_kinds(self.scene))

    def test_mesh_names_are_sanitized_but_ids_stay_raw(self):
        link = link_by_id(self.scene, "extra")
        self.assertEqual(link["provenance"]["source_part"]["part_id"], "PART/B")
        self.assertEqual(link["visuals"][0]["filename"], "geometry/parts/PART_B.stl")
        self.assertEqual(link["provenance"]["source_geometry"], "geometry/parts/PART_B.stl")

    def test_joint_frame_axis_and_limits_follow_the_source(self):
        joint = next(item for item in self.scene["joints"] if item["name"] == "hinge")
        self.assertEqual(joint["type"], "revolute")
        self.assertEqual(joint["parent"], "Base")
        self.assertEqual(joint["child"], "Arm_link")
        self.assertEqual(joint["xyz"], [0.0, 0.0, 0.2])
        self.assertEqual(joint["axis"], [0.0, 0.0, 1.0])
        self.assertEqual(joint["limits"], {"lower": -0.5, "upper": 0.75})
        self.assertEqual(joint["provenance"]["limits_from"], "assembly_features")
        self.assertEqual(joint["provenance"]["parent_child_from"], "mate_entity_order")
        self.assertEqual(joint["provenance"]["effort_velocity"], "not_in_source")
        frame = self.scene["frames"][0]
        self.assertEqual((frame["name"], frame["parent"]), ("body_frame", "Base"))

    def test_unsupported_and_unresolved_mates_become_gaps_not_guesses(self):
        kinds = gap_kinds(self.scene)
        for kind in (
            "joint_limits_missing",
            "mate_type_unsupported",
            "mate_parent_unresolved",
            "gltf_unavailable",
        ):
            with self.subTest(kind=kind):
                self.assertIn(kind, kinds)
        self.assertEqual(
            [item["name"] for item in self.scene["joints"] if "limits" not in item],
            ["free"],
        )
        # 抑制实例不建模，但必须在 non_physical 里留痕（不删除其存在）
        self.assertNotIn("spare", [link["id"] for link in self.scene["links"]])
        self.assertIn("spare", self.scene["provenance"]["non_physical"]["suppressed"])

    def test_provenance_records_the_assembly_conventions(self):
        provenance = self.scene["provenance"]
        self.assertEqual(provenance["provider"], "onshape")
        self.assertEqual(provenance["microversion"], "mv-root")
        self.assertEqual(provenance["configuration"], "default")
        self.assertEqual(provenance["subassemblies"], ["sub0001"])
        self.assertEqual(
            provenance["expected_joints"],
            ["dof_hinge", "dof_free", "frame_body_frame", "dof_cyl", "dof_orphan"],
        )
        self.assertTrue(any("由机器人定义" in note for note in provenance["notes"]))


class SceneContractGateTests(unittest.TestCase):
    def test_schema_gate_rejects_a_link_without_provenance(self):
        tmp = Path(tempfile.mkdtemp(prefix="onshape-gate-"))
        scene, _ = synthetic_scene(tmp)
        broken = json.loads(json.dumps(scene))
        del broken["links"][0]["provenance"]
        with self.assertRaises(Exception) as caught:
            _validate_scene(broken)
        self.assertEqual(getattr(caught.exception, "code", None), "onshape_scene_invalid")


class InvalidGeometryTests(unittest.TestCase):
    def test_broken_mesh_bytes_stay_as_evidence_but_never_become_visuals(self):
        root = Path(tempfile.mkdtemp(prefix="onshape-invalid-"))

        def break_mesh(cache_root: Path) -> None:
            (cache_root / "bytes" / "stl_PART_A.stl").write_bytes(b"solid broken\nendsolid\n")

        scene, collection = synthetic_scene(root, prepare=break_mesh)
        self.assertEqual(link_by_id(scene, "base")["visuals"], [])
        self.assertIn("PART_A", collection.geometry)
        gap = next(item for item in scene["provenance"]["gaps"] if item["kind"] == "geometry_invalid")
        self.assertEqual(gap["part_id"], "PART_A")
        self.assertEqual(gap["sha256"], hashlib.sha256(collection.geometry["PART_A"]).hexdigest())
        duplicated = [
            item
            for item in scene["provenance"]["gaps"]
            if item["kind"] == "geometry_missing" and item.get("part_id") == "PART_A"
        ]
        self.assertEqual(duplicated, [])


if __name__ == "__main__":
    unittest.main()
