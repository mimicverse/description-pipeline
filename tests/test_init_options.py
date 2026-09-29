"""``model init`` must be usable without hand-written YAML, and say what to do next.

The first-use guide used to open with a hand-written ``source.yaml``: the first place a new user can
go wrong, and one that only fails after SolidWorks has been started.  These tests pin the option form
(including Windows paths authored on Linux), its refusals, and the "next step" hints the CLI prints.
"""

import contextlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from description_pipeline import cli
from description_pipeline.build import build, freeze, lock_toolchain

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "demo-arm"
IGNORED = shutil.ignore_patterns("build", ".venv", "__pycache__")


def run_cli(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(argv)
    return code, out.getvalue(), err.getvalue()


def run_init(root: Path, *extra: str) -> tuple[int, str, str]:
    return run_cli(["model", "init", "--root", str(root), "--hardware", "demo", *extra])


class SourceOptionTests(unittest.TestCase):
    def test_solidworks_options_write_the_documented_mapping(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "demo"
            code, out, err = run_init(
                root,
                "--provider",
                "solidworks",
                "--assembly",
                "D:\\robots\\demo\\robot.SLDASM",
                "--configuration",
                "Default",
            )
            payload = json.loads(out)
            source = json.loads((root / "config/robot.yaml").read_text(encoding="utf-8"))["source"]

        self.assertEqual(code, 0)
        self.assertEqual(source["provider"], "solidworks")
        self.assertEqual(source["assembly"], "D:/robots/demo/robot.SLDASM")
        self.assertEqual(source["allowed_roots"], ["D:/robots/demo"])
        self.assertEqual(source["worker_url"], "http://127.0.0.1:8765")
        self.assertTrue(source["require_saved"])
        self.assertEqual(payload["next"][1].split()[0], "description")
        self.assertIn("next:", err)

    def test_an_explicit_allowed_root_is_honoured(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "demo"
            code, _out, _err = run_init(
                root,
                "--provider",
                "solidworks",
                "--assembly",
                "D:/robots/demo/robot.SLDASM",
                "--configuration",
                "Default",
                "--allowed-roots",
                "D:/robots",
                "--worker-url",
                "http://127.0.0.1:9100",
            )
            source = json.loads((root / "config/robot.yaml").read_text(encoding="utf-8"))["source"]

        self.assertEqual(code, 0)
        self.assertEqual(source["allowed_roots"], ["D:/robots"])
        self.assertEqual(source["worker_url"], "http://127.0.0.1:9100")

    def test_onshape_accepts_a_url_or_explicit_ids(self):
        with tempfile.TemporaryDirectory() as temporary:
            first = Path(temporary) / "by-url"
            code, _out, _err = run_init(
                first, "--provider", "onshape", "--url", "https://cad.onshape.com/documents/d/w/w/e/e"
            )
            url_source = json.loads((first / "config/robot.yaml").read_text(encoding="utf-8"))["source"]
            second = Path(temporary) / "by-ids"
            code2, _out2, _err2 = run_init(
                second,
                "--provider",
                "onshape",
                "--document-id",
                "d",
                "--element-id",
                "e",
                "--workspace-id",
                "w",
                "--configuration",
                "default",
            )
            id_source = json.loads((second / "config/robot.yaml").read_text(encoding="utf-8"))["source"]

        self.assertEqual((code, code2), (0, 0))
        self.assertEqual(url_source, {"provider": "onshape", "url": "https://cad.onshape.com/documents/d/w/w/e/e"})
        self.assertEqual(id_source["document_id"], "d")
        self.assertEqual(id_source["workspace_id"], "w")
        self.assertEqual(id_source["configuration"], "default")

    def test_refusals_name_what_is_missing(self):
        cases = [
            (["--provider", "solidworks", "--configuration", "Default"], "--assembly"),
            (["--provider", "solidworks", "--assembly", "D:/a/robot.SLDASM"], "--configuration"),
            (
                ["--provider", "solidworks", "--assembly", "relative/robot.SLDASM", "--configuration", "Default"],
                "absolute",
            ),
            (["--provider", "onshape"], "--url"),
            ([], "--source-config"),
        ]
        for extra, expected in cases:
            with self.subTest(extra=extra), tempfile.TemporaryDirectory() as temporary:
                code, _out, err = run_init(Path(temporary) / "demo", *extra)
                payload = json.loads(err)
            self.assertEqual(code, 2)
            self.assertEqual(payload["error"], "PipelineError")
            self.assertIn(expected, payload["message"])

    def test_a_source_config_and_provider_options_are_mutually_exclusive(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "source.yaml"
            config.write_text("provider: onshape\nurl: https://cad.onshape.com/documents/d/w/w/e/e\n", encoding="utf-8")
            code, _out, err = run_init(
                root / "demo",
                "--source-config",
                str(config),
                "--provider",
                "onshape",
                "--url",
                "https://cad.onshape.com/documents/d/w/w/e/e",
            )
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(err)["error"], "PipelineError")
        self.assertIn("Describe the source with", json.loads(err)["message"])


class NextHintTests(unittest.TestCase):
    def test_check_prints_the_submit_command(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "demo-arm"
            shutil.copytree(EXAMPLE, workspace, ignore=IGNORED)
            lock_toolchain(workspace)
            freeze(workspace)
            build(workspace, "kinematics")
            code, out, err = run_cli(["check", "--root", str(workspace), "--profile", "kinematics"])
            payload = json.loads(out)

        self.assertEqual(code, 0)
        self.assertTrue(payload["passed"])
        self.assertIn("description model submit", " ".join(payload["next"]))
        self.assertIn("next: description model submit", err)


if __name__ == "__main__":
    unittest.main()
