"""来源身份：请求身份绑定、修订锁定、foreign document 与混修订一律 fail closed。"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from description_pipeline.sources.onshape.cache import CachedFetcher, ResponseCache, immutable_path
from description_pipeline.sources.onshape.errors import (
    CACHE_IDENTITY_MISMATCH,
    DEPENDENCY_INCOMPLETE,
    FOREIGN_DOCUMENT,
    REVISION_MISMATCH,
    OnshapeSourceError,
)
from description_pipeline.sources.onshape.freeze import freeze, load_scene

from .helpers import FETCHED_AT, ROOT_ELEMENT, StubClient, URL, assembly_payload, write_cache

CAPTURE = {"evidence": "fixture", "reason": "合成夹具", "at": "2026-01-01T00:00:00Z"}


class MovingWorkspaceClient(StubClient):
    """第一次 /w/ 返回原微版本，采集后探针（第二次 /w/）返回移动后的微版本。"""

    def __init__(self, moved: str = "mv-moved") -> None:
        super().__init__()
        self.moved = moved
        self.workspace_calls = 0

    def request(self, method, path, query=None, body=None, ctype="application/json", *, raw=False, **kwargs):
        if path.startswith("/api/assemblies/") and "/w/" in path:
            self.workspace_calls += 1
            payload = assembly_payload()
            if self.workspace_calls > 1:
                payload["rootAssembly"]["documentMicroversion"] = self.moved
            return payload
        return super().request(method, path, query, body, ctype, raw=raw, **kwargs)


class CacheBindingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="onshape-binding-"))

    def test_same_name_different_request_is_rejected(self):
        cache = ResponseCache(self.tmp / "cache")
        cache.save_json("assembly_x", {"rootAssembly": {}})
        cache.record_request(
            "assembly_x",
            path="/api/assemblies/d/other/w/other/e/other",
            query={"configuration": "default"},
            digest="0" * 64,
            raw=False,
        )
        fetcher = CachedFetcher(None, ResponseCache(self.tmp / "cache", read_only=True))
        with self.assertRaises(OnshapeSourceError) as caught:
            fetcher.json("assembly_x", "/api/assemblies/d/doc/w/ws/e/x", {"configuration": "default"})
        self.assertEqual(caught.exception.code, CACHE_IDENTITY_MISMATCH)

    def test_digest_mismatch_is_rejected(self):
        cache = ResponseCache(self.tmp / "cache")
        cache.save_json("assembly_x", {"rootAssembly": {}})
        cache.record_request(
            "assembly_x",
            path="/api/assemblies/d/doc/w/ws/e/x",
            query={"configuration": "default"},
            digest="deadbeef",
            raw=False,
        )
        fetcher = CachedFetcher(None, ResponseCache(self.tmp / "cache", read_only=True))
        with self.assertRaises(OnshapeSourceError) as caught:
            fetcher.json("assembly_x", "/api/assemblies/d/doc/w/ws/e/x", {"configuration": "default"})
        self.assertEqual(caught.exception.code, CACHE_IDENTITY_MISMATCH)

    def test_legacy_entry_is_marked_unbound(self):
        cache = ResponseCache(write_cache(self.tmp / "cache"))
        fetcher = CachedFetcher(None, cache)
        fetcher.json(f"assembly_{ROOT_ELEMENT}", "/api/assemblies/d/doc/m/mv/e/x")
        self.assertIn(f"assembly_{ROOT_ELEMENT}", fetcher.unbound)
        self.assertFalse(fetcher.immutable_requests)

    def test_immutable_path_classification(self):
        self.assertTrue(immutable_path("/api/assemblies/d/d/m/mv/e/x"))
        self.assertTrue(immutable_path("/api/assemblies/d/d/v/ver/e/x"))
        self.assertFalse(immutable_path("/api/assemblies/d/d/w/ws/e/x"))


class RevisionIdentityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="onshape-revision-"))
        self.cache = write_cache(self.tmp / "cache")

    def freeze(self, **overrides):
        config = {"url": URL, "cache": str(self.cache), "offline": True, "capture": CAPTURE}
        config.update(overrides)
        return freeze(config, self.tmp / "snapshot")

    def patch_assembly(self, mutate) -> None:
        path = self.cache / "json" / f"assembly_{ROOT_ELEMENT}.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        mutate(payload)
        path.write_text(json.dumps(payload), encoding="utf-8")

    def test_mixed_revision_dependency_is_rejected(self):
        """依赖实例引用另一个微版本：同一快照无法同时表示 → 拒绝。"""

        def mutate(payload):
            payload["subAssemblies"][0]["instances"][0]["documentMicroversion"] = "mv-other"

        self.patch_assembly(mutate)
        with self.assertRaises(OnshapeSourceError) as caught:
            self.freeze()
        self.assertEqual(caught.exception.code, REVISION_MISMATCH)
        self.assertEqual(caught.exception.detail["dependency_microversion"], "mv-other")

    def test_foreign_document_dependency_is_rejected(self):
        """依赖来自其它文档：当前单文档命名空间无法唯一命名 → 拒绝。"""

        def mutate(payload):
            payload["rootAssembly"]["instances"][0]["documentId"] = "other-document"

        self.patch_assembly(mutate)
        with self.assertRaises(OnshapeSourceError) as caught:
            self.freeze()
        self.assertEqual(caught.exception.code, FOREIGN_DOCUMENT)
        self.assertEqual(caught.exception.detail["document_id"], "other-document")

    def test_dependency_configuration_must_match_the_request(self):
        def mutate(payload):
            payload["rootAssembly"]["instances"][0]["configuration"] = "alt"

        self.patch_assembly(mutate)
        with self.assertRaises(OnshapeSourceError) as caught:
            self.freeze()
        self.assertIn(caught.exception.code, {"onshape_identity_collision"})

    def test_missing_root_microversion_is_rejected(self):
        def mutate(payload):
            payload["rootAssembly"].pop("documentMicroversion", None)

        self.patch_assembly(mutate)
        with self.assertRaises(OnshapeSourceError) as caught:
            self.freeze()
        self.assertEqual(caught.exception.code, REVISION_MISMATCH)

    def test_features_microversion_mismatch_is_rejected(self):
        path = self.cache / "json" / f"assembly_features_{ROOT_ELEMENT}.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["sourceMicroversion"] = "mv-other"
        path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(OnshapeSourceError) as caught:
            self.freeze()
        self.assertEqual(caught.exception.code, REVISION_MISMATCH)

    def test_instance_without_element_id_is_rejected(self):
        """装配实例缺 elementId 会让整棵子树消失，必须 fail closed。"""

        def mutate(payload):
            for instance in payload["rootAssembly"]["instances"]:
                if instance["type"] == "Assembly":
                    instance.pop("elementId", None)
                    break

        self.patch_assembly(mutate)
        with self.assertRaises(OnshapeSourceError) as caught:
            self.freeze()
        self.assertEqual(caught.exception.code, DEPENDENCY_INCOMPLETE)
        self.assertIsNone(caught.exception.detail["element_id"])

    def test_instance_with_unknown_type_is_rejected(self):
        def mutate(payload):
            payload["rootAssembly"]["instances"][0]["type"] = "PartStudio"

        self.patch_assembly(mutate)
        with self.assertRaises(OnshapeSourceError) as caught:
            self.freeze()
        self.assertEqual(caught.exception.code, DEPENDENCY_INCOMPLETE)
        self.assertEqual(caught.exception.detail["type"], "PartStudio")

    def test_replay_does_not_stamp_the_run_time_as_capture_time(self):
        """回放时 at 必须来自调用方/缓存记录，否则如实写 None。"""

        manifest = self.freeze(capture={"evidence": "fixture", "reason": "无 at"})
        self.assertEqual(manifest["identity"]["capture"]["at_source"], "cache_source_json")
        self.assertEqual(manifest["identity"]["capture"]["at"], FETCHED_AT)

        stripped = Path(tempfile.mkdtemp(prefix="onshape-no-fetched-at-"))
        stripped_cache = write_cache(stripped / "cache")
        (stripped_cache / "json/source.json").unlink()
        manifest = freeze(
            {
                "url": URL,
                "cache": str(stripped_cache),
                "offline": True,
                "capture": {"evidence": "fixture", "reason": "没有采集时间记录"},
            },
            stripped / "snapshot",
        )
        self.assertIsNone(manifest["identity"]["capture"]["at"])
        self.assertEqual(manifest["identity"]["capture"]["at_source"], "unrecorded")

    def test_caller_supplied_capture_time_wins(self):
        manifest = self.freeze(capture={**CAPTURE, "at": "2026-02-02T00:00:00Z"})
        self.assertEqual(manifest["identity"]["capture"]["at_source"], "caller")
        self.assertEqual(manifest["identity"]["capture"]["at"], "2026-02-02T00:00:00Z")

    def test_live_capture_stamps_the_run_time(self):
        manifest = freeze({"url": URL, "client": StubClient()}, self.tmp / "live-stamp")
        self.assertEqual(manifest["identity"]["capture"]["at_source"], "run")
        self.assertIsNotNone(manifest["identity"]["capture"]["at"])

    def test_replay_keeps_the_original_capture_identity(self):
        """缓存记录里的原始 mode/工具版本/时间不能被本次回放覆盖。"""

        path = self.cache / "json" / "source.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["capture"] = {
            "mode": "live_api",
            "tool_version": "0.2.0",
            "at": "2026-01-01T00:00:00Z",
            "evidence": "cad",
            "reason": "2026-01-01 真实 API 采集",
        }
        path.write_text(json.dumps(payload), encoding="utf-8")
        manifest = self.freeze()
        identity = manifest["identity"]
        recorded = identity["snapshot_origin"]["recorded_capture"]
        self.assertEqual(recorded["mode"], "live_api")
        self.assertEqual(recorded["tool_version"], "0.2.0")
        self.assertEqual(recorded["at"], "2026-01-01T00:00:00Z")
        # 本次运行方式单独记录：通道、当前工具版本、实际运行时间
        import description_pipeline

        current = description_pipeline.__version__
        self.assertEqual(identity["this_run"]["method"], "cache_replay")
        self.assertEqual(identity["this_run"]["tool_version"], current)
        self.assertEqual(identity["this_run"]["toolchain"]["version"], current)
        self.assertEqual(len(identity["this_run"]["toolchain"]["package_digest"]), 64)
        self.assertEqual(identity["this_run"]["at_source"], "run")
        self.assertNotEqual(identity["this_run"]["at"], "2026-01-01T00:00:00Z")

    def test_absent_record_does_not_invent_origin_identity(self):
        """没有采集记录时，origin 不能补上本次的 mode/工具版本/时间。"""

        (self.cache / "json" / "source.json").unlink()
        manifest = self.freeze(capture={"evidence": "fixture", "reason": "无采集记录"})
        recorded = manifest["identity"]["snapshot_origin"]["recorded_capture"]
        self.assertNotIn("mode", recorded)
        self.assertNotIn("tool_version", recorded)
        self.assertNotIn("at", recorded)
        self.assertEqual(recorded, {}, "origin 必须是空的原记录，不吸收调用方声明")
        self.assertEqual(manifest["identity"]["capture"]["declared"]["reason"], "无采集记录")
        self.assertIsNone(manifest["identity"]["capture"]["at"])
        self.assertEqual(manifest["identity"]["capture"]["at_source"], "unrecorded")
        self.assertEqual(manifest["identity"]["this_run"]["method"], "cache_replay")
        self.assertIsNotNone(manifest["identity"]["this_run"]["at"])

    def test_declared_override_does_not_overwrite_the_recorded_capture(self):
        """原记录与调用方声明冲突时两者都要保留，snapshot_origin 用原记录。"""

        path = self.cache / "json" / "source.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["capture"] = {
            "mode": "live_api",
            "tool_version": "0.1.0",
            "at": "2026-01-01T00:00:00Z",
            "evidence": "cad",
            "reason": "2026-01-01 历史采集",
        }
        path.write_text(json.dumps(payload), encoding="utf-8")
        manifest = freeze(
            {
                "url": URL,
                "cache": str(self.cache),
                "offline": True,
                "capture": {
                    "mode": "cache_replay",
                    "tool_version": "9.9.9",
                    "at": "2026-03-03T00:00:00Z",
                    "evidence": "fixture",
                    "reason": "作者显式选择以夹具声明",
                },
            },
            self.tmp / "conflict",
        )
        capture = manifest["identity"]["capture"]
        recorded = manifest["identity"]["snapshot_origin"]["recorded_capture"]
        # origin 保持历史原值，绝不写成覆盖值
        self.assertEqual(recorded["mode"], "live_api")
        self.assertEqual(recorded["tool_version"], "0.1.0")
        self.assertEqual(recorded["at"], "2026-01-01T00:00:00Z")
        self.assertEqual(recorded["evidence"], "cad")
        self.assertEqual(recorded["reason"], "2026-01-01 历史采集")
        # 调用方声明单独记录，生效值取声明
        self.assertEqual(capture["declared"]["mode"], "cache_replay")
        self.assertEqual(capture["declared"]["tool_version"], "9.9.9")
        self.assertEqual(capture["declared"]["reason"], "作者显式选择以夹具声明")
        self.assertEqual(capture["effective"]["evidence"], "fixture")
        self.assertEqual(capture["effective"]["at"], "2026-03-03T00:00:00Z")
        self.assertEqual(manifest["evidence_class"], "fixture")

    def test_caller_declaration_alone_stays_out_of_origin(self):
        """没有历史记录时，声明只进 declared，origin 仍为空。"""

        (self.cache / "json" / "source.json").unlink()
        manifest = self.freeze(capture={"evidence": "cad", "at": "2026-04-04T00:00:00Z", "reason": "调用方声明"})
        capture = manifest["identity"]["capture"]
        self.assertEqual(capture["origin"], {})
        self.assertEqual(capture["declared"]["evidence"], "cad")
        self.assertEqual(capture["effective"]["at"], "2026-04-04T00:00:00Z")
        self.assertNotIn("mode", manifest["identity"]["snapshot_origin"]["recorded_capture"])
        self.assertNotIn("tool_version", manifest["identity"]["snapshot_origin"]["recorded_capture"])

    def test_live_run_records_its_own_capture_identity(self):
        client = StubClient()
        manifest = freeze({"url": URL, "client": client}, self.tmp / "live-identity")
        run = manifest["identity"]["this_run"]
        self.assertEqual(run["method"], "api")
        self.assertFalse(run["mixed"])
        self.assertTrue(run["network_requests"])
        self.assertEqual(run["capture"]["mode"], "live_api")
        self.assertEqual(run["capture"]["tool_version"], manifest["identity"]["provider_version"])
        self.assertEqual(sorted(run["capture"]["requests"]), sorted(run["network_requests"]))

    def test_mixed_cache_and_api_does_not_claim_the_whole_batch(self):
        """缓存 + API 混用时：transport=mixed，只有 network_requests 属于本次采集。"""

        client = StubClient()
        # 不可变修订下的 features 可复用，工作区头仍必须走 client 刷新。
        cache_root = self.tmp / "mixed-cache"
        (cache_root / "json").mkdir(parents=True)
        shutil.copyfile(
            self.cache / "json" / f"assembly_features_{ROOT_ELEMENT}.json",
            cache_root / "json" / f"assembly_features_{ROOT_ELEMENT}.json",
        )
        manifest = freeze(
            {"url": URL, "cache": str(cache_root), "client": client, "capture": CAPTURE},
            self.tmp / "mixed",
        )
        run = manifest["identity"]["this_run"]
        self.assertEqual(run["transport"], "mixed")
        self.assertTrue(run["mixed"])
        self.assertIn(f"assembly_features_{ROOT_ELEMENT}", run["cache_requests"])
        self.assertIn(f"assembly_{ROOT_ELEMENT}", run["network_requests"])
        self.assertTrue(run["network_requests"])
        self.assertFalse(set(run["network_requests"]) & set(run["cache_requests"]))
        self.assertIn("mixed", run["capture"]["note"])

    def test_legacy_replay_records_the_limitation(self):
        manifest = self.freeze()
        identity = manifest["identity"]
        self.assertFalse(identity["revision_locked"])
        self.assertEqual(identity["capture"]["cache_binding"], "legacy_unverified")
        scene = load_scene(self.tmp / "snapshot")
        gaps = {gap["kind"] for gap in scene["provenance"]["gaps"]}
        self.assertIn("cache_request_identity_unverified", gaps)
        self.assertIn("revision_not_locked", gaps)
        self.assertIn("part_studio_microversion_unproven", gaps)

    def test_version_url_cannot_certify_unbound_legacy_bytes(self):
        manifest = self.freeze(url=URL.replace("/w/ws0001/", "/v/version1/"))
        self.assertFalse(manifest["identity"]["revision_locked"])

    def test_root_configuration_must_match_the_request(self):
        self.patch_assembly(lambda data: data["rootAssembly"].update(configuration="wrong"))
        with self.assertRaises(OnshapeSourceError) as caught:
            self.freeze()
        self.assertEqual(caught.exception.code, REVISION_MISMATCH)

    def test_live_capture_pins_every_request(self):
        client = StubClient()
        manifest = freeze({"url": URL, "client": client}, self.tmp / "live")
        identity = manifest["identity"]
        self.assertTrue(identity["revision_locked"])
        self.assertEqual(identity["capture"]["cache_binding"], "live")
        pinned = [call for call in client.calls if "/m/mv-root/" in call]
        probes = [call for call in client.calls if "/w/" in call]
        # 两次可变的 /w/：采集前的入口探测 + 采集后的一致性探针（都不作为证据字节）
        self.assertEqual(len(probes), 2, "只允许根装配的两次 /w/ 探测请求")
        self.assertTrue(pinned)
        self.assertEqual(len(pinned) + len(probes), len(client.calls))
        post = identity["revision_evidence"]["post_probe"]
        self.assertEqual(post["status"], "passed")
        self.assertTrue(post["unchanged"])
        self.assertEqual(post["post_microversion"], "mv-root")
        self.assertTrue(identity["this_run"]["cad_api_contacted"])
        self.assertTrue(identity["this_run"]["cad_api_available_proven"])
        self.assertTrue(identity["this_run"]["cad_api_head_unchanged"])
        self.assertEqual(identity["this_run"]["cad_api_head_stability"]["status"], "passed")

    def test_workspace_moved_during_capture_is_recorded(self):
        client = MovingWorkspaceClient()
        manifest = freeze({"url": URL, "client": client}, self.tmp / "moved")
        revision = manifest["identity"]["revision_evidence"]
        self.assertEqual(revision["post_probe"]["status"], "changed")
        self.assertFalse(revision["post_probe"]["unchanged"])
        self.assertEqual(revision["post_probe"]["post_microversion"], "mv-moved")
        # 快照仍然锁定在采集时钉住的修订上，但必须显式记下工作区移动
        self.assertTrue(manifest["identity"]["revision_locked"])
        scene = load_scene(self.tmp / "moved")
        kinds = {gap["kind"] for gap in scene["provenance"]["gaps"]}
        self.assertIn("workspace_moved_during_capture", kinds)
        # 接口确实可用（拿到了响应），但工作区头在采集期间移动过——两件事分开记
        self.assertTrue(manifest["identity"]["this_run"]["cad_api_available_proven"])
        self.assertFalse(manifest["identity"]["this_run"]["cad_api_head_unchanged"])
        self.assertEqual(manifest["identity"]["this_run"]["cad_api_head_stability"]["status"], "changed")

    def test_replay_does_not_claim_current_api(self):
        manifest = self.freeze()
        identity = manifest["identity"]
        self.assertEqual(identity["revision_evidence"]["post_probe"]["status"], "not_applicable")
        self.assertFalse(identity["this_run"]["cad_api_contacted"])
        self.assertFalse(identity["this_run"]["cad_api_available_proven"])
        self.assertIn("不证明当前 CAD 接口可用", identity["this_run"]["note"])

    def test_snapshot_origin_is_separate_from_this_run(self):
        manifest = self.freeze()
        identity = manifest["identity"]
        # 原始采集身份（cad）与本次运行方式（cache_replay）分别记录
        self.assertEqual(identity["snapshot_origin"]["evidence_class"], "fixture")
        self.assertEqual(identity["this_run"]["method"], "cache_replay")
        self.assertEqual(identity["snapshot_origin"]["cache_binding"], "legacy_unverified")
        self.assertEqual(identity["this_run"]["cache_binding"], "legacy_unverified")

    def test_source_semantics_distinguishes_raw_and_scene_formats(self):
        """原始读数（标称+上下界）与 scene 的 6 分量张量必须分开写清楚。"""
        manifest = self.freeze()
        semantics = manifest["identity"]["source_semantics"]
        self.assertEqual(semantics["units"], "SI")
        self.assertEqual(semantics["inertia_reference"], "center_of_mass")
        self.assertIn("[标称, 下界, 上界]", semantics["raw_scalar_reading"])
        self.assertIn("9 个数", semantics["raw_centroid_reading"])
        self.assertIn("27 个数", semantics["raw_inertia_reading"])
        self.assertIn("3×3", semantics["raw_inertia_reading"])
        self.assertIn("6 个独立分量", semantics["scene_inertia_form"])
        self.assertIn("不下传", semantics["scene_inertia_form"])
        self.assertNotIn("inertia_form", semantics)

    def test_capture_settings_record_tolerance_without_fake_unit(self):
        manifest = self.freeze(tolerance=0.02, include_geometry=False)
        settings = manifest["identity"]["capture_settings"]
        self.assertEqual(settings["tolerance"], 0.02)
        self.assertNotIn("tolerance_m", settings)
        self.assertIn("混合量纲", settings["tolerance_meaning"])
        self.assertFalse(settings["include_geometry"])
        self.assertEqual(settings["configuration"], "default")
        self.assertTrue(settings["offline"])

    def test_version_reference_reports_api_availability_separately(self):
        """版本引用没有工作区头，但 live 采集确实证明了接口可用。"""
        version_url = URL.replace("/w/", "/v/")
        client = StubClient()
        manifest = freeze({"url": version_url, "client": client}, self.tmp / "version")
        identity = manifest["identity"]
        self.assertTrue(identity["revision_locked"])
        self.assertEqual(identity["this_run"]["cad_api_available_proven"], True)
        self.assertIsNone(identity["this_run"]["cad_api_head_unchanged"])
        self.assertEqual(identity["revision_evidence"]["post_probe"]["status"], "not_applicable")
        self.assertIn("版本引用", identity["this_run"]["cad_api_head_stability"]["reason"])


if __name__ == "__main__":
    unittest.main()
