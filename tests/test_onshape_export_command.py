"""旧引擎导出通道的弃用契约：拒绝新导出、不请求 API、不落盘。

历史编排（check → 引擎 → 布局归一 → 核验）已由 description_pipeline 取代：
来源采集用 `description source freeze`，出模型用 `description build`（来源包负责
normalize_scene），因此这里只验证旧入口**明确失败且无副作用**。
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))

from onshape_export import cli, engine  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "onshape"
CACHE = FIXTURES / "cache"


def export_argv(out: Path) -> list[str]:
    return ["export", "--cache", str(CACHE), "--offline", "--out", str(out), "--format", "both", "--json"]


class ExportCommandTests(unittest.TestCase):
    def test_export_is_deprecated_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as folder:
            out = Path(folder) / "out"
            code = cli.main(export_argv(out))
            self.assertEqual(code, cli.EXIT_DEPRECATED)
            self.assertFalse(out.exists(), "旧通道不得再产生任何产物")

    def test_export_never_contacts_api_or_engine(self):
        with tempfile.TemporaryDirectory() as folder:
            out = Path(folder) / "out"
            runner = mock.MagicMock()
            with (
                mock.patch.object(engine, "run", runner),
                mock.patch.object(cli, "_client", side_effect=AssertionError("不得发起 API 调用")),
            ):
                code = cli.main(export_argv(out))
            self.assertEqual(code, cli.EXIT_DEPRECATED)
            runner.assert_not_called()


if __name__ == "__main__":
    unittest.main()
