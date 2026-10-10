"""Windows acquisition must not load a consumer; Linux verification must."""

import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

from description_pipeline import runtime, solidworks
from description_pipeline.io import PipelineError
from description_pipeline.verification import consumer
from description_pipeline.sources.solidworks import isolation


class RuntimeRoleTests(unittest.TestCase):
    def test_windows_doctor_does_not_probe_or_require_mujoco(self):
        with (
            patch.object(runtime.sys, "platform", "win32"),
            patch.object(solidworks.importlib.metadata, "version", side_effect=runtime.RUNTIME_VERSIONS.__getitem__),
            patch.object(solidworks, "native_readiness", return_value={"solidworks_executable": "SLDWORKS.exe"}),
            patch.object(consumer, "readiness", side_effect=AssertionError("Windows must not load MuJoCo")) as probe,
        ):
            result = solidworks.doctor()
        self.assertTrue(result["passed"])
        self.assertEqual(result["role"], "native")
        self.assertNotIn("mujoco", [row["id"] for row in result["checks"]])
        probe.assert_not_called()

    def test_native_failure_blocks_native_doctor(self):
        with (
            patch.object(runtime.sys, "platform", "win32"),
            patch.object(solidworks, "native_readiness", side_effect=PipelineError("SolidWorks not registered")),
        ):
            result = solidworks.doctor()
        self.assertFalse(result["passed"])
        self.assertFalse(result["native_capture_available"])

    def test_linux_requires_consumer_even_when_packages_exist(self):
        with (
            patch.object(runtime.sys, "platform", "linux"),
            patch.object(solidworks.importlib.metadata, "version", side_effect=runtime.RUNTIME_VERSIONS.__getitem__),
            patch.object(consumer, "readiness", side_effect=RuntimeError("consumer failed")),
        ):
            result = solidworks.doctor()
        self.assertFalse(result["passed"])
        self.assertEqual(result["role"], "portable")
        self.assertIn("consumer.urdf", [row["id"] for row in result["checks"]])

    def test_wrong_runtime_version_fails_doctor(self):
        with (
            patch.object(runtime.sys, "platform", "linux"),
            patch.object(solidworks.importlib.metadata, "version", return_value="0.0.0"),
            patch.object(consumer, "readiness", return_value={}),
        ):
            self.assertFalse(solidworks.doctor()["passed"])

    def test_wrong_consumer_version_blocks_loading_without_doctor(self):
        with (
            patch.object(consumer.importlib.metadata, "version", return_value="0.0.0"),
            patch.object(consumer.subprocess, "run") as process,
            self.assertRaisesRegex(consumer.ConsumerError, "pinned verification runtime"),
        ):
            consumer.load(Path("unused-delivery"))
        process.assert_not_called()

    def test_native_probe_never_opens_cad_and_checks_com_imports(self):
        with (
            patch.object(runtime.sys, "platform", "win32"),
            patch.object(runtime.importlib.metadata, "version", return_value="311"),
            patch.object(runtime.importlib, "import_module") as importer,
            patch.object(isolation, "registered_executable", return_value="SLDWORKS.exe"),
        ):
            self.assertEqual(runtime.native_readiness()["role"], "native")
        self.assertEqual([call.args[0] for call in importer.call_args_list], ["pythoncom", "win32com.client"])

    def test_native_probe_rejects_linux(self):
        with patch.object(runtime.sys, "platform", "linux"), self.assertRaisesRegex(PipelineError, "Windows"):
            runtime.native_readiness()

    def test_tool_records_bind_role_without_changing_code_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            module = Path(directory) / "runtime.py"
            module.write_text("source")
            with (
                patch.object(runtime, "__file__", str(module)),
                patch.object(runtime, "runtime_packages", return_value={}),
            ):
                native = runtime.tool_record("native")
                portable = runtime.tool_record("portable")
        self.assertEqual(native["source_sha256"], portable["source_sha256"])
        self.assertEqual(native["runtime"]["role"], "native")
        self.assertEqual(portable["runtime"]["role"], "portable")
        with self.assertRaises(PipelineError):
            runtime.tool_record("unknown")

    def test_package_and_lock_match_host_roles(self):
        root = Path(__file__).resolve().parents[2]
        project = tomllib.loads((root / "pyproject.toml").read_text())["project"]
        self.assertNotIn("mujoco==3.13.0", project["dependencies"])
        self.assertEqual(project["optional-dependencies"]["verify"], ["mujoco==3.13.0"])
        for role, host in (("native", "windows"), ("portable", "linux")):
            locked = {
                row.split("==")[0].lower(): row.split("==")[1]
                for row in (root / f"requirements/{host}-py312.lock").read_text().splitlines()
                if row and not row.startswith("#")
            }
            for name in runtime.required_packages(role):
                self.assertEqual(locked[name.lower()], runtime.RUNTIME_VERSIONS[name])
            self.assertEqual("mujoco" in locked, role == "portable")


if __name__ == "__main__":
    unittest.main()
