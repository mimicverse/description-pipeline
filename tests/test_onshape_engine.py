"""引擎适配层（engine.py）：写配置、缺依赖报错、调用引擎、离线分支转发。"""

import json
import sys
import tempfile
import types
from typing import Any
import unittest
from pathlib import Path
from unittest import mock

TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))

from onshape_export import engine  # noqa: E402
from onshape_export.url import DocumentRef  # noqa: E402

REF = DocumentRef(
    stack="https://cad.onshape.com",
    document_id="d",
    workspace_id="w",
    element_id="e",
)


def engine_modules(main=None):
    """最小 onshape_to_robot 包桩；`import onshape_to_robot.export` 必须成立。"""

    package: Any = types.ModuleType("onshape_to_robot")
    package.__path__ = []  # 标记为包
    export: Any = types.ModuleType("onshape_to_robot.export")
    export.main = main or (lambda: None)
    package.export = export
    return mock.patch.dict(sys.modules, {"onshape_to_robot": package, "onshape_to_robot.export": export})


class WriteConfigTests(unittest.TestCase):
    def test_config_records_every_option(self):
        with tempfile.TemporaryDirectory() as folder:
            path = engine.write_config(
                Path(folder),
                REF,
                output_format="mujoco",
                assembly_name="Main",
                color=(0.1, 0.2, 0.3, 1.0),
                ignore={"part": "all"},
                configuration="Default",
                joint_properties={"default": {"damping": 0.041}},
            )
            config = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(config["url"], REF.url())
        self.assertEqual(config["output_format"], "mujoco")
        self.assertEqual(config["assembly_name"], "Main")
        self.assertEqual(config["color"], [0.1, 0.2, 0.3, 1.0])
        self.assertEqual(config["ignore"], {"part": "all"})
        self.assertEqual(config["configuration"], "Default")
        self.assertEqual(config["joint_properties"], {"default": {"damping": 0.041}})

    def test_optional_keys_are_omitted(self):
        with tempfile.TemporaryDirectory() as folder:
            path = engine.write_config(Path(folder), REF, output_format="urdf")
            config = json.loads(path.read_text(encoding="utf-8"))
        self.assertNotIn("assembly_name", config)
        self.assertNotIn("ignore", config)
        self.assertNotIn("joint_properties", config)


class DeprecatedRunTests(unittest.TestCase):
    """engine.run 只保留明确的拒绝语义：不导入引擎、不改 argv、不写文件。"""

    def test_run_is_deprecated_with_migration_pointer(self):
        with self.assertRaises(engine.EngineDeprecated) as caught:
            engine.run(Path("/tmp/robot-dir"))
        message = str(caught.exception)
        self.assertIn("已弃用", message)
        self.assertIn("description build", message)
        self.assertIn("normalize_scene", message)

    def test_run_never_touches_the_engine_or_sys_argv(self):
        calls: list = []
        original_argv = list(sys.argv)
        with (
            engine_modules(lambda: calls.append(("export", list(sys.argv)))),
            self.assertRaises(engine.EngineDeprecated),
        ):
            engine.run(
                Path("/tmp/robot-dir"),
                offline_cache=Path("/tmp/cache"),
                auto_dof=True,
                density_overrides={"names": {"a__a": 500}},
            )
        self.assertEqual(calls, [], "弃用入口不得再调用旧引擎")
        self.assertEqual(sys.argv, original_argv)

    def test_deprecated_error_is_an_engine_error(self):
        self.assertTrue(issubclass(engine.EngineDeprecated, engine.EngineError))


if __name__ == "__main__":
    unittest.main()
