"""Release and process entry boundaries for native and portable runtimes."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

from description_pipeline import cli
from description_pipeline.orchestration import windows

ROOT = Path(__file__).resolve().parents[2]
_PORTABLE_IMPORT_BLOCKERS = (
    "numpy",
    "jsonschema",
    "yaml",
    "psycopg2",
    "mujoco",
    "airflow",
    "fastapi",
    "python_multipart",
    "description_pipeline.model",
    "description_pipeline.verification",
    "description_pipeline.pipeline.application",
    "description_pipeline.pipeline.stages",
    "description_pipeline.pipeline.verify",
    "description_pipeline.pipeline.publish",
)


class EntryBoundaryTests(unittest.TestCase):
    def test_windows_module_launcher_routes_to_the_native_server(self) -> None:
        config = Path("C:/description/endpoint.json")
        with patch.object(windows, "serve") as serve:
            self.assertEqual(windows.main(["serve", "--config", str(config)]), 0)
        serve.assert_called_once_with(config)

    def test_windows_module_help_imports_without_portable_runtime_packages(self) -> None:
        blocked = _PORTABLE_IMPORT_BLOCKERS
        program = f"""
import importlib.abc
import sys

blocked = {blocked!r}
sys.path.insert(0, {str(ROOT / "src")!r})

class Blocker(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if any(fullname == item or fullname.startswith(item + ".") for item in blocked):
            raise ImportError("blocked portable dependency: " + fullname)
        return None

sys.meta_path.insert(0, Blocker())
sys.argv = ["description_pipeline.orchestration.windows", "--help"]
from description_pipeline.orchestration import windows
try:
    windows.main()
except SystemExit as error:
    raise SystemExit(0 if error.code == 0 else error.code)
"""
        environment = {
            **os.environ,
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": str(ROOT / "src"),
        }
        completed = subprocess.run(
            [sys.executable, "-I", "-B", "-c", program],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=15,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("description-windows", completed.stdout)
        self.assertNotIn("blocked portable dependency", completed.stderr)

    def test_native_capture_leaf_does_not_eager_load_portable_stages(self) -> None:
        program = f"""
import importlib.abc
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, {str(ROOT / "src")!r})
blocked = {json.dumps(_PORTABLE_IMPORT_BLOCKERS)}

class Blocker(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if any(fullname == item or fullname.startswith(item + ".") for item in blocked):
            raise ImportError("blocked portable dependency: " + fullname)
        return None

sys.meta_path.insert(0, Blocker())
from description_pipeline.orchestration import windows
from description_pipeline.pipeline.capture import CaptureRequest, _capture_native_transaction
with tempfile.TemporaryDirectory() as root:
    os.environ["DESCRIPTION_CAD_OWNERSHIP_FILE"] = str(Path(root) / "ownership.json")
    runtime = windows.build_runtime({{"state_root": Path(root)}})
    runtime.close()
print(json.dumps(sorted(
    name for name in sys.modules
    if name.startswith("description_pipeline.pipeline.")
)))
"""
        environment = {
            **os.environ,
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": str(ROOT / "src"),
        }
        completed = subprocess.run(
            [sys.executable, "-I", "-B", "-c", program],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=15,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        modules = set(json.loads(completed.stdout))
        self.assertNotIn("description_pipeline.pipeline.application", modules)
        self.assertNotIn("description_pipeline.pipeline.stages.generate", modules)
        self.assertNotIn("description_pipeline.pipeline.verify", modules)
        self.assertNotIn("description_pipeline.pipeline.publish", modules)

    def test_pyproject_keeps_native_dependencies_out_of_portable_headers(self) -> None:
        project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
        self.assertEqual(
            project["dependencies"],
            [
                "packaging==26.3",
                "pywin32==311; sys_platform == 'win32'",
            ],
        )
        self.assertEqual(
            set(project["optional-dependencies"]["portable"]),
            {
                "PyYAML==6.0.3",
                "numpy==2.5.3",
                "jsonschema==4.26.0",
                "python-multipart==0.0.32",
                "starlette==1.6.0",
                "anyio==4.15.1",
                "uvicorn==0.52.4",
                "a2wsgi==1.10.10",
                "websockets==16.1.1",
            },
        )
        self.assertEqual(project["scripts"], {"description": "description_pipeline.cli:main"})
        self.assertEqual(
            project["entry-points"]["airflow.plugins"],
            {
                "description_pipeline": (
                    "description_pipeline.orchestration.airflow_plugin:"
                    "DescriptionPipelinePlugin"
                )
            },
        )

    def test_cli_has_no_removed_workflow_aliases(self) -> None:
        operations = cli.parser()._subparsers._group_actions[0].choices
        self.assertEqual(set(operations), {"admit", "stage", "run", "doctor"})

    def test_windows_lifecycle_uses_the_existing_native_module_route(self) -> None:
        source = (ROOT / "deploy/windows/instance-lifecycle.ps1").read_text(encoding="utf-8")
        self.assertIn("-I -B -m description_pipeline.orchestration.windows serve --config", source)
        self.assertNotIn("'^(-I -m description_pipeline serve --config )\"([^\"]+)\"$'", source)
        self.assertNotIn("140d39e51be5f0c39c32d7cfea9ca25fe642c273", source)


if __name__ == "__main__":
    unittest.main()
