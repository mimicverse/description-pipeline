"""Entry points: ``python -m solidworks_export``, the bundle builder and dispatch."""

import contextlib
import io
import unittest
from pathlib import Path
from unittest import mock

from tools.solidworks_export import __main__, build_bundle


class EntryPointTests(unittest.TestCase):
    def run_main(self, argv):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = __main__.main(argv)
        return code, buffer.getvalue()

    def test_version_flag(self):
        code, output = self.run_main(["--version"])
        self.assertEqual(code, 0)
        self.assertEqual(output.strip(), "solidworks_export 1.0.0")

    def test_serve_is_dispatched_to_the_server(self):
        with mock.patch.object(__main__.server, "serve_main", return_value=7) as serve:
            code = __main__.main(["serve", "--port", "1234"])
        self.assertEqual(code, 7)
        serve.assert_called_once_with(["--port", "1234"])

    def test_other_commands_are_dispatched_to_the_client(self):
        with mock.patch.object(__main__.cli, "main", return_value=0) as client:
            self.assertEqual(__main__.main(["ping"]), 0)
        client.assert_called_once_with(["ping"])


class BundleTests(unittest.TestCase):
    def test_retired_bundle_builder_directs_operators_to_current_distribution(self):
        error = io.StringIO()
        with contextlib.redirect_stderr(error):
            self.assertEqual(build_bundle.main(), 3)
        self.assertIn("tools/build_release.py", error.getvalue())


class DocumentationTests(unittest.TestCase):
    def test_documented_error_codes_exist_in_the_code(self):
        """The error-code table in docs must not drift from the implementation."""
        repo = Path(__file__).resolve().parents[2]
        docs = (repo / "docs" / "history" / "solidworks_export.md").read_text(encoding="utf-8")
        # The contact layer moved to the public pipeline package; the compat
        # entry points in tools/ are shims, so the inventory has to look in both.
        roots = [repo / "tools" / "solidworks_export", repo / "src" / "description_pipeline" / "sources" / "solidworks"]
        sources = "\n".join(
            path.read_text(encoding="utf-8")
            for root in roots
            if root.is_dir()
            for path in root.rglob("*.py")
            if "__pycache__" not in path.parts
        )
        documented = []
        for line in docs.splitlines():
            if line.startswith("| `") and line.count("|") >= 3:
                documented.append(line.split("|")[1].strip().strip("`"))
        self.assertTrue(documented, "no error-code table found in docs")
        missing = [code for code in documented if code not in sources]
        self.assertEqual(missing, [], f"error codes documented but not raised: {missing}")


if __name__ == "__main__":
    unittest.main()
