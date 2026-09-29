"""离线缓存通道（offline.py）的回归：这是配额耗尽后的生产路径。"""

import json
import shutil
import sys
import tempfile
import types
from typing import Any
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))

from onshape_export import offline  # noqa: E402
from tests import onshape_fixture  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "onshape"
CACHE = FIXTURES / "cache"
SOURCE = json.loads((CACHE / "source.json").read_text(encoding="utf-8")) if onshape_fixture.AVAILABLE else {}
ELEMENT = SOURCE.get("element_id", "")
PART = "KFzB"
# 夹具里带该 body 的 partStudio 元素（mass_properties_<id>.json）
STUDIO = onshape_fixture.mass_properties_ids(PART)[0] if onshape_fixture.AVAILABLE else ""


def fake_client_class():
    """最小的引擎 Client 桩：只要能被继承即可。"""

    class Client:
        def __init__(self, *args, **kwargs):  # pragma: no cover - 子类不调用
            raise AssertionError("缓存的 Client 不应初始化 HTTP 层")

    return Client


class FakeEngineModules:
    """把 onshape_to_robot.* 替换成桩模块，用于离线通道测试。"""

    def __enter__(self):
        self.saved = {
            name: sys.modules.get(name)
            for name in (
                "onshape_to_robot",
                "onshape_to_robot.onshape_api",
                "onshape_to_robot.onshape_api.client",
                "onshape_to_robot.assembly",
                "onshape_to_robot.export",
            )
        }
        package: Any = types.ModuleType("onshape_to_robot")
        api: Any = types.ModuleType("onshape_to_robot.onshape_api")
        client_module: Any = types.ModuleType("onshape_to_robot.onshape_api.client")
        client_module.Client = fake_client_class()
        assembly_module: Any = types.ModuleType("onshape_to_robot.assembly")
        assembly_module.Client = None
        export_module: Any = types.ModuleType("onshape_to_robot.export")
        self.calls: list[str] = []
        export_module.main = lambda: self.calls.append("export")
        package.onshape_api = api
        api.client = client_module
        sys.modules.update(
            {
                "onshape_to_robot": package,
                "onshape_to_robot.onshape_api": api,
                "onshape_to_robot.onshape_api.client": client_module,
                "onshape_to_robot.assembly": assembly_module,
                "onshape_to_robot.export": export_module,
            }
        )
        self.assembly_module = assembly_module
        return self

    def __exit__(self, *exc_info):
        for name, module in self.saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module
        return False


@onshape_fixture.requires_fixture
class PureHelperTests(unittest.TestCase):
    def test_safe_name_keeps_alnum_only(self):
        self.assertEqual(offline.safe_name("a-b/c d"), "a_b_c_d")
        self.assertEqual(offline.safe_name("JF1"), "JF1")

    def test_element_id_of_reads_the_url(self):
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder) / "config.json"
            config.write_text(
                json.dumps({"url": f"https://cad.onshape.com/documents/x/w/y/e/{ELEMENT}"}),
                encoding="utf-8",
            )
            self.assertEqual(offline.element_id_of(config), ELEMENT)
            config.write_text(json.dumps({"url": "https://example.invalid/none"}), encoding="utf-8")
            with self.assertRaises(ValueError):
                offline.element_id_of(config)

    def test_legacy_features_wraps_flat_v17_payload(self):
        flat = {
            "features": [
                {
                    "btType": "BTMFeature-134",
                    "featureType": "mate",
                    "parameters": [
                        {"btType": "BTMParameterEnum-145", "enumName": "REVOLUTE"},
                    ],
                },
                {"btType": "BTMFeature-134", "message": {"already": "wrapped"}},
            ]
        }
        wrapped = offline.legacy_features(flat)["features"]
        self.assertEqual(wrapped[0]["typeName"], "BTMFeature")
        self.assertEqual(wrapped[0]["message"]["parameters"][0]["typeName"], "BTMParameterEnum")
        self.assertNotIn("btType", wrapped[0]["message"]["parameters"][0]["message"])
        self.assertEqual(wrapped[1], {"btType": "BTMFeature-134", "message": {"already": "wrapped"}})

    def test_legacy_features_tolerates_empty_payload(self):
        self.assertEqual(offline.legacy_features({}), {"features": []})
        self.assertEqual(offline.legacy_features({"features": None}), {"features": []})


@onshape_fixture.requires_fixture
class CachedClientTests(unittest.TestCase):
    def test_cached_client_serves_every_endpoint_from_cache(self):
        with FakeEngineModules():
            client_class = offline.cached_client_class(CACHE, ELEMENT)
            client = client_class()
            assembly = client.get_assembly("d", "w", ELEMENT)
            self.assertEqual(len(assembly["rootAssembly"]["instances"]), 24)
            features = client.get_features("d", "w", ELEMENT)
            self.assertIn("features", features)
            self.assertEqual(
                client.matevalues("d", "w", ELEMENT),
                json.loads((CACHE / "json" / f"mate_values_{ELEMENT}.json").read_text(encoding="utf-8")),
            )
            bodies = client.part_mass_properties("d", "w", STUDIO, PART)["bodies"]
            self.assertEqual(list(bodies), [PART])
            with self.assertRaises(FileNotFoundError):
                client.part_mass_properties("d", "w", STUDIO, "missing")
            with self.assertRaises(FileNotFoundError):
                client.part_mass_properties("d", "w", "unknown-studio", PART)
            with self.assertRaises(FileNotFoundError):
                client.part_studio_stl_m("d", "w", ELEMENT, PART)

    def test_density_overrides_are_applied_before_the_engine_sees_them(self):
        overrides = {"names": {"head__head": 500}}
        with FakeEngineModules():
            client_class = offline.cached_client_class(CACHE, ELEMENT, density_overrides=overrides)
            client = client_class()
            body = client.part_mass_properties("d", "w", STUDIO, "JFT")["bodies"]["JFT"]
            # 覆盖按名字生效：head__head 的密度被改写，质量随之变化
            self.assertNotEqual(body["mass"][0], 0)

    def test_missing_assembly_in_cache_fails_loudly(self):
        with tempfile.TemporaryDirectory() as folder, FakeEngineModules():
            empty = Path(folder)
            shutil.copytree(CACHE / "json", empty / "json")
            (empty / "json" / f"assembly_{ELEMENT}.json").unlink()
            with self.assertRaises(FileNotFoundError):
                offline.cached_client_class(empty, ELEMENT)


@onshape_fixture.requires_fixture
class RunWithCacheTests(unittest.TestCase):
    def test_run_with_cache_swaps_the_client_and_calls_the_engine(self):
        with tempfile.TemporaryDirectory() as folder:
            robot_dir = Path(folder) / "robot"
            robot_dir.mkdir()
            (robot_dir / "config.json").write_text(
                json.dumps({"url": f"https://cad.onshape.com/documents/x/w/y/e/{ELEMENT}"}),
                encoding="utf-8",
            )
            with FakeEngineModules() as fake:
                offline.run_with_cache(robot_dir, CACHE)
                self.assertEqual(fake.calls, ["export"])
                self.assertIsNotNone(fake.assembly_module.Client)


if __name__ == "__main__":
    unittest.main()
