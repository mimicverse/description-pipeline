"""Windows acquisition must not load a consumer; Linux verification must."""

import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

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
            if role == "native":
                self.assertEqual(
                    set(locked),
                    {name.lower() for name in runtime.required_packages(role)},
                    "the Windows lock must carry exactly the native closure - no unused packages",
                )
                self.assertFalse(
                    set(locked) & {"numpy", "jsonschema", "python-multipart", "attrs", "referencing", "rpds-py"},
                    "packages owned by the portable surfaces must not return to the Windows lock",
                )

    def test_wheel_metadata_carries_the_exact_role_closures(self):
        """The wheel's base set and role extras are the closures the runtime enforces."""

        root = Path(__file__).resolve().parents[2]
        project = tomllib.loads((root / "pyproject.toml").read_text())["project"]
        extras = project["optional-dependencies"]
        base = [Requirement(value) for value in project["dependencies"]]

        def platform_names(platform: str) -> set[str]:
            names = set()
            for requirement in base:
                if requirement.marker is None or requirement.marker.evaluate({"sys_platform": platform}):
                    names.add(canonicalize_name(requirement.name))
            return names

        def extra_names(extra: str) -> set[str]:
            return {canonicalize_name(Requirement(value).name) for value in extras[extra]}

        self.assertEqual(set(extras), {"portable", "verify", "dev"})
        self.assertEqual(
            platform_names("win32"),
            {canonicalize_name(name) for name in runtime.required_packages("native")},
            "the wheel's Windows base requirements must be exactly the native role closure",
        )
        portable = platform_names("linux") | extra_names("portable") | extra_names("verify")
        self.assertEqual(
            portable,
            {canonicalize_name(name) for name in runtime.required_packages("portable")},
            "the base plus the portable and verify extras must be exactly the portable closure",
        )
        self.assertFalse(
            (platform_names("linux") | platform_names("win32"))
            & {"numpy", "jsonschema", "python-multipart", "mujoco"},
            "packages owned by the portable surfaces must never be base requirements",
        )

    def test_pyproject_python_range_is_the_support_single_source(self):
        root = Path(__file__).resolve().parents[2]
        project = tomllib.loads((root / "pyproject.toml").read_text())["project"]
        self.assertEqual(project["requires-python"], runtime.SUPPORTED_PYTHON)

    def test_release_domain_identity_comes_from_the_machine_definitions(self):
        from description_pipeline import interface_records, stages

        contract = interface_records.release_contract()
        self.assertEqual(contract["stage"], stages.CONTRACT_SHA256)
        self.assertEqual(contract["check_policy"], interface_records.check_policy_digest())
        self.assertEqual(contract["resource_policy"], interface_records.resource_policy_v1_digest())
        self.assertRegex(contract["schema"], r"\A[0-9a-f]{64}\Z")
        support = interface_records.release_support()
        self.assertEqual(support["python"], runtime.SUPPORTED_PYTHON)
        self.assertEqual([block["host"] for block in support["roles"].values()], ["windows", "linux"])
        self.assertEqual(support["roles"]["native"]["os"], "Windows")
        self.assertEqual(support["roles"]["native"]["arch"], "x86_64")
        self.assertEqual(support["roles"]["native"]["api"], "SolidWorks COM API")
        self.assertEqual(support["roles"]["portable"]["os"], "Linux")
        self.assertEqual(support["roles"]["portable"]["arch"], "x86_64")
        for role, block in support["roles"].items():
            self.assertEqual(
                block["packages"],
                {name: runtime.RUNTIME_VERSIONS[name] for name in runtime.required_packages(role)},
            )
        self.assertEqual(support["hosts"]["windows"], ["discover", "capture"])
        self.assertEqual(
            support["hosts"]["linux"],
            ["freeze", "discover", "capture", "generate", "verify", "publish"],
        )

    def test_the_schema_digest_covers_the_actual_definitions(self):
        from description_pipeline import interface_records
        from description_pipeline.io import file_digest

        root = Path(__file__).resolve().parents[2]
        package = root / "src/description_pipeline"
        baseline = interface_records.schema_definition_digest()
        self.assertEqual(baseline, interface_records.SCHEMA_DEFINITION_DIGEST)
        self.assertEqual(
            baseline,
            interface_records.digest(
                {
                    "model/schema.json": file_digest(package / "model/schema.json"),
                    "schemas.py": file_digest(package / "schemas.py"),
                    "interface_records.py": file_digest(package / "interface_records.py"),
                }
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            copy = Path(directory)
            (copy / "model").mkdir()
            model = (package / "model/schema.json").read_text()
            flipped = model.replace('"additionalProperties": false', '"additionalProperties": true', 1)
            self.assertNotEqual(flipped, model, "the model schema pins additionalProperties")
            (copy / "model/schema.json").write_text(flipped, encoding="utf-8")
            (copy / "schemas.py").write_bytes((package / "schemas.py").read_bytes())
            (copy / "interface_records.py").write_bytes((package / "interface_records.py").read_bytes())
            self.assertNotEqual(interface_records.schema_definition_digest(Path(directory)), baseline)
            records = (package / "interface_records.py").read_text()
            renamed = records.replace('"ready"', '"readiness_flag"', 1)
            self.assertNotEqual(renamed, records, "the health key set lives in the owner module")
            (copy / "interface_records.py").write_text(renamed, encoding="utf-8")
            self.assertNotEqual(interface_records.schema_definition_digest(Path(directory)), baseline)


if __name__ == "__main__":
    unittest.main()
