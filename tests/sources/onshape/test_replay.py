"""仓库内 Onshape 夹具（2026-09-17 真实 API 缓存）的离线重放。"""

import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

from description_pipeline.sources.onshape.freeze import freeze, load_scene
from tests import onshape_fixture  # noqa: E402

FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "onshape" / "cache"
CAPTURE = {"evidence": "fixture", "reason": "仓库内回放夹具", "at": "2026-09-17T16:00:00Z"}
RUN_AT = datetime(2026, 9, 20, 12, tzinfo=UTC)


def fixture_url() -> str:
    return json.loads((FIXTURE / "source.json").read_text(encoding="utf-8"))["url"]


@onshape_fixture.requires_fixture
class ReplayTests(unittest.TestCase):
    clock: MagicMock
    tmp: tempfile.TemporaryDirectory
    root: Path
    manifest: dict
    scene: dict

    @classmethod
    def setUpClass(cls):
        clock_patch = patch("description_pipeline.sources.onshape.freeze.datetime")
        cls.clock = clock_patch.start()
        cls.addClassCleanup(clock_patch.stop)
        cls.clock.now.return_value = RUN_AT
        cls.tmp = tempfile.TemporaryDirectory(prefix="onshape-replay-")
        cls.root = Path(cls.tmp.name)
        cls.manifest = freeze(
            {"url": fixture_url(), "cache": str(FIXTURE), "offline": True, "capture": CAPTURE},
            cls.root / "snapshot",
        )
        cls.scene = load_scene(cls.root / "snapshot")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_manifest_binds_the_whole_dependency_closure(self):
        self.assertEqual(self.manifest["kind"], "onshape")
        self.assertEqual(self.manifest["evidence_class"], "fixture")
        self.assertEqual(self.manifest["identity"]["capture"]["mode"], "cache_replay")
        counts = self.manifest["identity"]["counts"]
        self.assertEqual(counts["assemblies"], 1)
        self.assertEqual(counts["subassemblies"], 20)
        self.assertEqual(counts["part_studios"], 2)
        self.assertEqual(counts["occurrences"], 317)
        self.assertEqual(self.manifest["identity"]["microversion_id"], "128a9e0c1e236e23f64aed8d")

    def test_scene_covers_every_instance_exactly_once(self):
        provenance = self.scene["provenance"]
        counts = provenance["entity_counts"]
        self.assertEqual(len(provenance["expected_occurrences"]), 317)
        self.assertEqual(len(provenance["expected_entities"]), 297)
        self.assertEqual(len(provenance["non_physical"]["containers"]), 20)
        self.assertEqual(counts["occurrences"], 317)
        self.assertEqual(counts["part_instances"], 297)
        self.assertEqual(counts["assembly_instances"], 20)
        self.assertEqual(counts["links"], 297)
        assigned = [link["provenance"]["source_entities"][0] for link in self.scene["links"]]
        self.assertEqual(len(assigned), len(set(assigned)))
        self.assertEqual(set(assigned) - set(provenance["expected_entities"]), set())
        self.assertEqual(
            set(provenance["expected_occurrences"]) - set(provenance["expected_entities"]),
            set(provenance["non_physical"]["containers"]),
        )

    def test_root_level_part_instances_are_links(self):
        """回归：根层零件的实例路径只有一段，不能被长度过滤掉。"""

        roots = [link for link in self.scene["links"] if len(link["provenance"]["source_instance"]) == 1]
        self.assertEqual(len(roots), 4)
        self.assertEqual({link["provenance"]["source_part"]["part_id"] for link in roots}, {"JHD"})

    def test_kinematics_match_the_audited_document(self):
        self.assertEqual(len(self.scene["joints"]), 19)
        self.assertEqual(len(self.scene["frames"]), 4)
        self.assertEqual(
            sorted(joint["type"] for joint in self.scene["joints"]),
            ["revolute"] * 19,
        )
        self.assertTrue(all(joint["limits"] for joint in self.scene["joints"]))
        self.assertEqual(
            sorted(joint["name"] for joint in self.scene["joints"])[:4],
            ["head", "left_ankle_pitch", "left_ankle_roll", "left_elbow"],
        )
        self.assertEqual(
            sorted(frame["name"] for frame in self.scene["frames"]),
            ["body_frame", "imu_frame", "left_foot_frame", "right_foot_frame"],
        )

    def test_missing_geometry_is_reported_not_fabricated(self):
        self.assertEqual([link for link in self.scene["links"] if link["visuals"]], [])
        self.assertEqual([link for link in self.scene["links"] if link["inertial"] is None], [])
        kinds = [gap["kind"] for gap in self.scene["provenance"]["gaps"]]
        self.assertEqual(kinds.count("gltf"), 2)
        self.assertEqual([name for name in self.manifest["files"] if name.startswith("geometry/parts/")], [])
        self.assertIn("geometry/parts.json", self.manifest["files"])

    def test_replay_is_reproducible(self):
        second = freeze(
            {"url": fixture_url(), "cache": str(FIXTURE), "offline": True, "capture": CAPTURE},
            self.root / "snapshot-2",
        )
        self.assertEqual(self.manifest["files"], second["files"])
        self.assertEqual(load_scene(self.root / "snapshot-2"), self.scene)

    def test_replay_records_each_run_time_without_changing_readings(self):
        later = RUN_AT + timedelta(seconds=2)
        self.clock.now.return_value = later
        try:
            third = freeze(
                {"url": fixture_url(), "cache": str(FIXTURE), "offline": True, "capture": CAPTURE},
                self.root / "snapshot-later",
            )
        finally:
            self.clock.now.return_value = RUN_AT
        changed = [name for name, value in self.manifest["files"].items() if third["files"][name] != value]
        self.assertEqual(changed, ["scene.json"])
        before = json.loads((self.root / "snapshot/scene.json").read_text(encoding="utf-8"))
        after = json.loads((self.root / "snapshot-later/scene.json").read_text(encoding="utf-8"))
        self.assertEqual(after["provenance"]["capture"]["run"]["at"], later.isoformat(timespec="seconds"))
        after["provenance"]["capture"]["run"]["at"] = before["provenance"]["capture"]["run"]["at"]
        self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()
