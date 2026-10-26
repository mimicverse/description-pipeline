"""The native Capture route must run without the portable dependency closure."""

from __future__ import annotations

import importlib.metadata
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import packaging


class NativeMinimalDependencyClosureTests(unittest.TestCase):
    def test_real_capture_route_runs_with_only_packaging(self) -> None:
        source = Path(
            os.environ.get(
                "NATIVE_CAPTURE_SOURCE",
                Path(__file__).resolve().parents[2],
            )
        ).resolve()
        self.assertEqual(packaging.__version__, "26.3")
        distribution = importlib.metadata.distribution("packaging")
        package_root = Path(packaging.__file__).resolve().parent
        with tempfile.TemporaryDirectory(prefix="native-minimal-closure-") as directory:
            temporary = Path(directory)
            site = temporary / "site"
            site.mkdir()
            shutil.copytree(package_root, site / "packaging")
            for entry in distribution.files or ():
                if entry.parts and entry.parts[0].endswith(".dist-info"):
                    artifact = distribution.locate_file(entry)
                    target = site / entry
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(artifact, target)

            workspace = temporary / "workspace"
            receipt = temporary / "receipt.json"
            environment = {
                "HOME": str(temporary / "home"),
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "PATH": os.environ.get("PATH", ""),
                "PYTHONNOUSERSITE": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
                "TMPDIR": str(temporary / "tmp"),
            }
            result = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    "-S",
                    "-B",
                    str(Path(__file__).resolve().parent / "native_minimal_closure_probe.py"),
                    "--source",
                    str(source),
                    "--site",
                    str(site),
                    "--workspace",
                    str(workspace),
                    "--receipt",
                    str(receipt),
                ],
                cwd=temporary,
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )
            if not receipt.is_file():
                self.fail(
                    f"The minimal native probe produced no receipt.\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
                )
            evidence = json.loads(receipt.read_text(encoding="utf-8"))
            self.assertEqual(
                result.returncode,
                0,
                json.dumps(evidence, indent=2, sort_keys=True),
            )
            self.assertEqual(evidence["status"], "pass")
            self.assertEqual(evidence["banned_imports"], [])
            self.assertTrue(
                all(origin is None for origin in evidence["module_specs"].values()),
                evidence["module_specs"],
            )
            self.assertEqual(evidence["route"]["capture_components"], 2)
            self.assertEqual(evidence["route"]["source_scope_components"], 2)
            self.assertEqual(evidence["route"]["mesh_components"], 2)
        self.assertEqual(
            evidence["route"]["transfer_main_assembly"],
            "cad/robot.SLDASM",
        )


if __name__ == "__main__":
    unittest.main()
