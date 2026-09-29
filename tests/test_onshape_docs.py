"""历史接口文档完整性：归档仍须覆盖保留的规则编号与命令行参数。

工具的全部规则和参数都在这里对表——新增规则或参数却忘了写文档就会失败。
规则表允许用区间（如 `OSV020`~`OSV025`）覆盖子编号。
"""

import argparse
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
sys.path.insert(0, str(TOOLS))

from onshape_export import cli  # noqa: E402

DOC = ROOT / "docs" / "history" / "onshape_export.md"
CODE = re.compile(r'"(OS[XV]\d{3}[a-z]?)"')
REFERENCE = re.compile(r"`(OS[XV]\d{3}[a-z]?)`")
RANGE = re.compile(r"`(OS[XV]\d{3}[a-z]?)`~`(OS[XV]\d{3}[a-z]?)`")


def _key(code: str) -> tuple[str, int, str]:
    match = re.fullmatch(r"(OS[XV])(\d{3})([a-z]*)", code)
    assert match is not None, f"规则编号不合法：{code}"
    return match.group(1), int(match.group(2)), match.group(3)


class DocumentationCoverageTests(unittest.TestCase):
    def setUp(self):
        self.doc = DOC.read_text(encoding="utf-8")

    def test_every_rule_code_is_documented(self):
        codes = set()
        for path in sorted((TOOLS / "onshape_export").glob("*.py")):
            codes |= set(CODE.findall(path.read_text(encoding="utf-8")))
        self.assertGreater(len(codes), 50, "规则编号收集异常，检查正则")
        singles = set(REFERENCE.findall(self.doc))
        ranges = RANGE.findall(self.doc)

        def covered(code: str) -> bool:
            if code in singles:
                return True
            family, number, letter = _key(code)
            for start, end in ranges:
                family_a, number_a, letter_a = _key(start)
                family_b, number_b, letter_b = _key(end)
                if family_a != family or family_b != family:
                    continue
                if not number_a <= number <= number_b:
                    continue
                if number == number_a and letter < letter_a:
                    continue
                if number == number_b and letter_b and letter > letter_b:
                    continue
                return True
            return False

        missing = sorted(code for code in codes if not covered(code))
        self.assertEqual(missing, [], f"这些规则没写进 {DOC.name}")

    def test_every_cli_flag_is_documented(self):
        flags: set[str] = set()

        def collect(parser: argparse.ArgumentParser) -> None:
            for action in parser._actions:  # noqa: SLF001 - argparse 没有公开遍历接口
                flags.update(action.option_strings)
                if isinstance(action, argparse._SubParsersAction):  # noqa: SLF001
                    for sub in action.choices.values():
                        collect(sub)

        parser = cli._parser()  # noqa: SLF001 - 直接拿真实解析器，避免解析帮助文本
        collect(parser)
        flags -= {"-h", "--help"}
        self.assertGreater(len(flags), 20, "参数收集异常")
        missing = sorted(flag for flag in flags if flag not in self.doc)
        self.assertEqual(missing, [], f"这些参数没写进 {DOC.name}")


if __name__ == "__main__":
    unittest.main()
