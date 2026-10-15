"""W6 CU pre-verification tool: Linux checks, args naming, native phase wiring."""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from tools.preverify_cu import DEFAULT_ARGS_NAME, linux_checks, load_args, main, sw_checks

from description_pipeline.sources.solidworks.selection import SelectionError


def _package(root: Path, *, lock: bool = False, zero: bool = False) -> Path:
    folder = root / "package"
    folder.mkdir()
    (folder / "3.0 总装.SLDASM").write_bytes(b"assembly-bytes")
    (folder / "arm.SLDPRT").write_bytes(b"arm-bytes")
    if lock:
        (folder / "~$3.0 总装.SLDASM").write_bytes(b"lock")
    if zero:
        (folder / "empty.SLDPRT").write_bytes(b"")
    return folder


class LinuxChecksTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_complete_folder_passes(self) -> None:
        folder = _package(self.root)
        check = linux_checks(folder, main="3.0 总装.SLDASM")
        self.assertEqual(check["verdict"], "pass")
        self.assertTrue(check["main"]["exact_name_present"])
        self.assertEqual(check["count"], 2)

    def test_lock_and_zero_byte_files_are_attention(self) -> None:
        folder = _package(self.root, lock=True, zero=True)
        check = linux_checks(folder, main="3.0 总装.SLDASM")
        self.assertEqual(check["verdict"], "attention")
        self.assertEqual(check["lock_files"], ["~$3.0 总装.SLDASM"])
        self.assertEqual(check["zero_byte"], ["empty.SLDPRT"])

    def test_main_rule_requires_the_exact_saved_name(self) -> None:
        folder = _package(self.root)
        check = linux_checks(folder, main="3.0 总装-renamed.SLDASM")
        self.assertEqual(check["verdict"], "attention")
        self.assertFalse(check["main"]["exact_name_present"])
        self.assertEqual(check["main"]["matches"], [])

    def test_receipt_comparison(self) -> None:
        folder = _package(self.root)
        good = self.root / "good.receipt"
        good.write_text(
            "\n".join(
                f"{linux_checks(folder)['inventory'][name]['sha256']}  {name}"
                for name in ("3.0 总装.SLDASM", "arm.SLDPRT")
            )
            + "\n",
            encoding="utf-8",
        )
        self.assertEqual(linux_checks(folder, main="3.0 总装.SLDASM", receipt=good)["verdict"], "pass")
        bad = self.root / "bad.receipt"
        bad.write_text("0" * 64 + "  arm.SLDPRT\n", encoding="utf-8")
        check = linux_checks(folder, main="3.0 总装.SLDASM", receipt=bad)
        self.assertEqual(check["verdict"], "attention")
        self.assertEqual(check["receipt"]["mismatched"], ["arm.SLDPRT"])


class ArgsNamingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _args(self, name: str) -> Path:
        path = self.root / name
        path.write_text(
            json.dumps(
                {
                    "tag": "U1-dryrun",
                    "folder": str(self.root / "package"),
                    "main_assembly": "3.0 总装.SLDASM",
                    "expected_hardware_id": "m3.0",
                }
            ),
            encoding="utf-8",
        )
        return path

    def test_explicit_args_file_feeds_folder_main_and_target(self) -> None:
        loaded = load_args(self._args("probe_v4_args_u1.json"), folder=None, main=None, target=None)
        self.assertEqual(loaded["main_assembly"], "3.0 总装.SLDASM")
        self.assertEqual(loaded["expected_hardware_id"], "m3.0")
        self.assertEqual(loaded["tag"], "U1-dryrun")

    def test_missing_canonical_file_names_its_candidates(self) -> None:
        self._args("probe_v4_args_u1.json")
        with self.assertRaises(FileNotFoundError) as raised:
            load_args(self.root / DEFAULT_ARGS_NAME, folder=None, main=None, target=None)
        message = str(raised.exception)
        self.assertIn(DEFAULT_ARGS_NAME, message)
        self.assertIn("probe_v4_args_u1.json", message)


class NativePhaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.folder = _package(Path(self._tmp.name))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_pass_payload_carries_the_preverify_block(self) -> None:
        class Reader:
            def observe(self, stage, main, files):
                return {"preverify": {"verdict": "pass", "open_errors": 0, "declarations_missing": []}}

        result = sw_checks(self.folder, "3.0 总装.SLDASM", reader=Reader())
        self.assertEqual(result["verdict"], "pass")
        self.assertEqual(result["open_errors"], 0)

    def test_selection_refusal_becomes_attention_with_evidence(self) -> None:
        class Reader:
            def observe(self, stage, main, files):
                raise SelectionError(
                    "selection_declarations_missing",
                    "the staged main carries no complete declared delivery identity (dp.*)",
                    problems=[{"code": "selection_declarations_missing", "missing": ["dp.hardware_id"]}],
                )

        result = sw_checks(self.folder, "3.0 总装.SLDASM", reader=Reader())
        self.assertEqual(result["verdict"], "attention")
        self.assertEqual(result["code"], "selection_declarations_missing")
        self.assertEqual(result["problems"][0]["missing"], ["dp.hardware_id"])


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_cli_writes_the_report_and_exits_zero_on_pass(self) -> None:
        folder = _package(self.root)
        out = self.root / "preverify.json"
        with contextlib.redirect_stdout(io.StringIO()):
            code = main(["--folder", str(folder), "--main", "3.0 总装.SLDASM", "--out", str(out)])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.read_text(encoding="utf-8"))["verdict"], "pass")

    def test_cli_exits_two_on_attention(self) -> None:
        folder = _package(self.root, lock=True)
        out = self.root / "preverify.json"
        with contextlib.redirect_stdout(io.StringIO()):
            code = main(["--folder", str(folder), "--main", "3.0 总装.SLDASM", "--out", str(out)])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(out.read_text(encoding="utf-8"))["verdict"], "attention")

    def test_cli_exits_three_on_missing_args_file(self) -> None:
        stream = io.StringIO()
        with contextlib.redirect_stderr(stream):
            code = main(["--args", str(self.root / DEFAULT_ARGS_NAME), "--out", str(self.root / "preverify.json")])
        self.assertEqual(code, 3)
        self.assertIn(DEFAULT_ARGS_NAME, stream.getvalue())


if __name__ == "__main__":
    unittest.main()
