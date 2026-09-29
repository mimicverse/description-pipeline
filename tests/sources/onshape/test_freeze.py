"""freeze/load_scene：清单、证据等级、离线重放与篡改检测。"""

import json
import tempfile
import unittest
from datetime import UTC, datetime
from importlib import import_module
from pathlib import Path
from unittest import mock

from description_pipeline.sources.onshape.errors import (
    SCENE_INVALID,
    SNAPSHOT_INCOMPLETE,
    SNAPSHOT_TAMPERED,
    OnshapeSourceError,
)
from description_pipeline.sources.onshape.freeze import freeze, load_scene
from description_pipeline.sources.snapshot import write_manifest

from .helpers import FETCHED_AT, ROOT_ELEMENT, URL, StubClient, write_cache

FIXTURE_CAPTURE = {"evidence": "fixture", "reason": "合成夹具"}
#: 审计时钟：`freeze` 用秒级 `datetime.now` 记录本次运行，它进入 identity 并写进 scene.json，
#: 所以同一输入跨秒冻结会得到不同摘要——测试必须钉住时钟，而不是把 run.at 从身份里去掉。
AUDIT_AT = datetime(2026, 9, 20, 12, tzinfo=UTC)
# 包把 ``freeze`` 函数重导出成同名属性，模块要按名字取。
freeze_module = import_module("description_pipeline.sources.onshape.freeze")


class FreezeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="onshape-freeze-test-"))
        self.cache = write_cache(self.tmp / "cache")
        clock = mock.patch.object(freeze_module, "datetime")
        self.clock = clock.start()
        self.addCleanup(clock.stop)
        self.clock.now.return_value = AUDIT_AT

    def freeze(self, destination: str, **overrides) -> dict:
        config = {"url": URL, "cache": str(self.cache), "offline": True}
        config.update(overrides)
        return freeze(config, self.tmp / destination)

    def test_offline_replay_writes_a_self_describing_snapshot(self):
        manifest = self.freeze("snapshot", capture={**FIXTURE_CAPTURE, "at": FETCHED_AT})
        self.assertEqual(manifest["schema_version"], "description.source/v1")
        self.assertEqual(manifest["kind"], "onshape")
        self.assertEqual(manifest["evidence_class"], "fixture")
        self.assertEqual(manifest["scene"], "scene.json")
        self.assertEqual(manifest["identity"]["capture"]["mode"], "cache_replay")
        self.assertEqual(manifest["identity"]["capture"]["at"], FETCHED_AT)
        self.assertEqual(manifest["identity"]["provider_version"], freeze_module.__version__)
        # 旧缓存没有请求身份索引：修订锁定必须如实记为 false，并写明原因。
        self.assertFalse(manifest["identity"]["revision_locked"])
        self.assertEqual(manifest["identity"]["capture"]["cache_binding"], "legacy_unverified")
        self.assertEqual(
            manifest["identity"]["revision_evidence"]["reason"],
            "旧缓存没有请求身份索引，无法证明字节来自同一修订",
        )
        self.assertEqual(manifest["identity"]["request_bindings"]["kind"], "legacy_unverified")
        self.assertTrue(manifest["identity"]["request_bindings"]["unbound"])
        self.assertEqual(manifest["identity"]["dependency_closure"]["subassemblies"], ["sub0001"])
        self.assertEqual(manifest["identity"]["counts"]["occurrences"], 6)
        self.assertNotIn("manifest.json", manifest["files"])
        for name in ("scene.json", f"raw/assembly_{ROOT_ELEMENT}.json", "geometry/parts.json"):
            with self.subTest(name=name):
                self.assertIn(name, manifest["files"])
        self.assertIn("geometry/parts/PART_A.stl", manifest["files"])

    def test_geometry_readings_point_at_the_sanitized_file(self):
        self.freeze("snapshot", capture=FIXTURE_CAPTURE)
        readings = json.loads((self.tmp / "snapshot" / "geometry" / "parts.json").read_text(encoding="utf-8"))
        self.assertEqual(readings["parts"]["PART/B"]["file"], "geometry/parts/PART_B.stl")
        self.assertEqual(readings["parts"]["PART/B"]["part_id"], "PART/B")
        self.assertTrue((self.tmp / "snapshot" / "geometry" / "parts" / "PART_B.stl").is_file())

    def test_load_scene_returns_the_scene_object_only(self):
        self.freeze("snapshot", capture=FIXTURE_CAPTURE)
        scene = load_scene(self.tmp / "snapshot")
        self.assertEqual(scene, json.loads((self.tmp / "snapshot" / "scene.json").read_text(encoding="utf-8")))
        self.assertEqual(scene["schema_version"], "description.scene/v1")
        for wrapper_key in ("manifest", "source", "integrity"):
            self.assertNotIn(wrapper_key, scene)

    def test_undeclared_cache_replay_is_imported_with_a_gap(self):
        manifest = self.freeze("snapshot")
        self.assertEqual(manifest["evidence_class"], "imported")
        scene = load_scene(self.tmp / "snapshot")
        self.assertEqual(scene["provenance"]["evidence_class"], "imported")
        self.assertIn("capture_provenance_missing", [gap["kind"] for gap in scene["provenance"]["gaps"]])

    def test_live_api_fetch_is_cad_evidence(self):
        client = StubClient()
        manifest = freeze({"url": URL, "client": client}, self.tmp / "live")
        self.assertEqual(manifest["evidence_class"], "cad")
        self.assertEqual(manifest["identity"]["capture"]["mode"], "live_api")
        self.assertNotIn("capture_provenance_missing", str(manifest))
        # 工作区引用：先 /w/ 探测，随后全部走 /m/<microversion>
        self.assertTrue(manifest["identity"]["revision_locked"])
        self.assertEqual(manifest["identity"]["capture"]["cache_binding"], "live")
        probe = manifest["identity"]["request_bindings"]["probe"]
        bindings = manifest["identity"]["request_bindings"]["bindings"]
        self.assertTrue(bindings[probe]["path"].startswith("/api/assemblies/"))
        self.assertIn("/w/", bindings[probe]["path"])
        for name, entry in bindings.items():
            if name == probe:
                continue
            with self.subTest(name=name):
                self.assertIn("/m/mv-root/", entry["path"])
                self.assertTrue(entry["immutable"])
        self.assertTrue(manifest["identity"]["revision_evidence"]["element_microversions_proven"])

    def test_capture_override_requires_a_reason(self):
        with self.assertRaises(OnshapeSourceError) as caught:
            self.freeze("snapshot", capture={"evidence": "cad"})
        self.assertEqual(caught.exception.code, SNAPSHOT_INCOMPLETE)

    def test_capture_evidence_must_be_a_known_class(self):
        with self.assertRaises(OnshapeSourceError) as caught:
            self.freeze("snapshot", capture={"evidence": "synthetic", "reason": "写错等级"})
        self.assertEqual(caught.exception.code, SNAPSHOT_INCOMPLETE)

    def test_destination_must_be_empty(self):
        destination = self.tmp / "used"
        destination.mkdir()
        (destination / "leftover.txt").write_text("x")
        with self.assertRaises(OnshapeSourceError) as caught:
            freeze({"url": URL, "cache": str(self.cache), "offline": True}, destination)
        self.assertEqual(caught.exception.code, SNAPSHOT_INCOMPLETE)

    def test_invalid_scene_is_rejected_before_writing_evidence(self):
        self.freeze("good", capture=FIXTURE_CAPTURE)
        broken = json.loads((self.tmp / "good" / "scene.json").read_text(encoding="utf-8"))
        del broken["links"][0]["provenance"]
        with (
            mock.patch.object(freeze_module.scene_module, "build_scene", return_value=broken),
            self.assertRaises(OnshapeSourceError) as caught,
        ):
            self.freeze("snapshot", capture=FIXTURE_CAPTURE)
        self.assertEqual(caught.exception.code, SCENE_INVALID)

    def test_freeze_is_deterministic_for_the_same_inputs(self):
        capture = {**FIXTURE_CAPTURE, "at": FETCHED_AT}
        first = self.freeze("a", capture=capture)
        second = self.freeze("b", capture=capture)
        self.assertEqual(first["files"], second["files"])
        self.assertEqual(first["identity"], second["identity"])
        # 审计时刻是身份的一部分（如实记录本次冻结发生在何时），只被测试钉住、不参与比较豁免
        run = first["identity"]["capture"]["run"]
        self.assertEqual(run["at"], AUDIT_AT.isoformat(timespec="seconds"))
        self.assertEqual(run["at_source"], "run")


class LoadSceneIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="onshape-integrity-"))
        cache = write_cache(self.tmp / "cache")
        freeze(
            {
                "url": URL,
                "cache": str(cache),
                "offline": True,
                "capture": FIXTURE_CAPTURE,
            },
            self.tmp / "snapshot",
        )
        self.snapshot = self.tmp / "snapshot"

    def test_missing_file_is_rejected(self):
        (self.snapshot / "geometry" / "parts" / "PART_A.stl").unlink()
        self.assert_tampered()

    def test_changed_file_is_rejected(self):
        target = self.snapshot / "scene.json"
        target.write_text(target.read_text(encoding="utf-8").replace("description.scene/v1", "description.scene/v1 "))
        self.assert_tampered()

    def test_extra_file_is_rejected(self):
        (self.snapshot / "raw" / "extra.json").write_text("{}\n")
        self.assert_tampered()

    def test_missing_manifest_is_incomplete(self):
        (self.snapshot / "manifest.json").unlink()
        with self.assertRaises(OnshapeSourceError) as caught:
            load_scene(self.snapshot)
        self.assertEqual(caught.exception.code, SNAPSHOT_INCOMPLETE)

    def test_foreign_kind_is_rejected(self):
        manifest = json.loads((self.snapshot / "manifest.json").read_text(encoding="utf-8"))
        manifest["kind"] = "solidworks"
        (self.snapshot / "manifest.json").write_text(json.dumps(manifest))
        with self.assertRaises(OnshapeSourceError) as caught:
            load_scene(self.snapshot)
        self.assertEqual(caught.exception.code, SNAPSHOT_INCOMPLETE)

    def test_consistent_snapshot_of_another_provider_is_rejected(self):
        other = self.tmp / "other"
        other.mkdir()
        (other / "scene.json").write_text("{}\n")
        write_manifest(other, kind="solidworks", identity={"provider": "solidworks"}, evidence_class="fixture")
        with self.assertRaises(OnshapeSourceError) as caught:
            load_scene(other)
        self.assertEqual(caught.exception.code, SNAPSHOT_INCOMPLETE)

    def assert_tampered(self):
        with self.assertRaises(OnshapeSourceError) as caught:
            load_scene(self.snapshot)
        self.assertEqual(caught.exception.code, SNAPSHOT_TAMPERED)


class VersionSourceTests(unittest.TestCase):
    """工具版本必须来自安装包单一来源，避免适配器写死旧版本号。"""

    def test_adapter_version_tracks_the_package(self):
        import description_pipeline

        from description_pipeline.sources.onshape import __version__ as adapter_version

        self.assertEqual(adapter_version, description_pipeline.__version__)
        self.assertEqual(freeze_module.__version__, description_pipeline.__version__)

    def test_snapshot_records_the_package_version(self):
        tmp = Path(tempfile.mkdtemp(prefix="onshape-version-"))
        manifest = freeze(
            {
                "url": URL,
                "cache": str(write_cache(tmp / "cache")),
                "offline": True,
                "capture": {"evidence": "fixture", "reason": "版本来源回归"},
            },
            tmp / "snapshot",
        )
        import description_pipeline

        self.assertEqual(manifest["identity"]["provider_version"], description_pipeline.__version__)
        self.assertEqual(manifest["identity"]["capture"]["tool_version"], description_pipeline.__version__)


if __name__ == "__main__":
    unittest.main()
