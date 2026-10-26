"""Field-level provenance for non-native capture environment metadata."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from . import _paths  # noqa: F401

from description_pipeline.io import PipelineError  # noqa: E402
from description_pipeline.schemas import TOOL_SCHEMA  # noqa: E402
from description_pipeline.sources.solidworks.evidence import (  # noqa: E402
    METADATA_WARNING_CODE,
    METADATA_WARNING_MESSAGE,
    metadata_value_warnings,
)
from description_pipeline.sources.solidworks.native import SolidWorksBackend  # noqa: E402


def environment(*, copy_pid: int = 100) -> dict:
    return {
        "captured_at": "2026-10-23T07:00:00Z",
        "platform": "Windows-11",
        "python": "3.12.9",
        "worker_version": "1.2.3",
        "toolchain": {
            "schema_version": TOOL_SCHEMA,
            "pipeline_id": "solidworks-to-urdf",
            "version": "1.2.3",
            "source_sha256": "a" * 64,
            "release": {"package_files": {"worker.py": "b" * 64}},
            "runtime": {
                "role": "native",
                "python": "3.12.9",
                "system": "Windows",
                "machine": "AMD64",
                "packages": {"packaging": "26.3", "pywin32": "311"},
            },
        },
        "solidworks": {
            "revision": "34.4.1",
            "input_mode": "saved_documents",
            "sessions": {
                "copy": {
                    "pid": copy_pid,
                    "state": "active",
                    "lease": {"held": True, "holder_pid": 200, "path": r"C:\state\cad.lease"},
                },
            },
            "build": "2026.1",
            "license": 1,
            "visible": True,
        },
        "prepared_capture": {
            "mode": "single_read_only_prepared_capture",
            "policy": {
                "mode": "prepared_read_only_copy",
                "source_root": r"C:\captures",
                "assembly": r"C:\captures\robot.SLDASM",
                "requested_configuration": "Default",
                "session_role": "copy",
                "search_root": r"C:\captures",
                "sealed_documents": [r"C:\captures\robot.SLDASM"],
                "com_started": False,
            },
            "opened": {
                "opened": "robot.SLDASM",
                "path": r"C:\captures\robot.SLDASM",
                "read_only": True,
                "errors": 0,
                "warnings": 0,
            },
            "state_before": {
                "path": r"C:\captures\robot.SLDASM",
                "title": None,
                "title_error": {
                    "code": "cad_title_unavailable",
                    "message": "title is diagnostic only",
                },
                "save_flag": None,
                "save_flag_error": {
                    "code": "cad_save_flag_unavailable",
                    "message": "save flag is diagnostic only",
                },
                "saved": None,
                "active_configuration": "Default",
                "configurations": None,
                "configurations_error": {
                    "code": "cad_configuration_names_unavailable",
                    "message": "configuration names are diagnostic only",
                },
                "read_only": True,
                "lightweight": None,
                "unsupported": ["lightweight"],
            },
        },
    }


def runtime_environment() -> dict:
    return environment(copy_pid=110)


class MetadataProvenanceTests(unittest.TestCase):
    def warnings(self, runtime=None, expected_tool=None) -> list[dict]:
        return metadata_value_warnings(
            runtime or runtime_environment(),
            expected_tool=expected_tool or environment()["toolchain"],
            runtime_sha256="c" * 64,
            expected_configuration="Default",
        )

    def objects(self, runtime=None) -> list[dict]:
        warnings = self.warnings(runtime=runtime)
        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0]["code"], METADATA_WARNING_CODE)
        self.assertEqual(warnings[0]["message"], METADATA_WARNING_MESSAGE)
        return warnings[0]["objects"]

    def test_metadata_warnings_cover_non_native_fields_without_claiming_native_reads(self) -> None:
        objects = self.objects()
        by_rule = {row["rule_id"]: row for row in objects if row["file"].endswith("native-runtime.json")}
        all_fields = {field for row in objects for field in row["fields"]}

        self.assertNotIn("/solidworks/revision", all_fields)
        self.assertEqual(
            by_rule["metadata.platform_diagnostic"]["rule_version"],
            "v1",
        )
        self.assertEqual(by_rule["metadata.source_configuration_echo"]["rule_version"], "v1")

    def test_getter_failure_placeholders_are_bound_to_their_actual_fields(self) -> None:
        runtime = runtime_environment()
        runtime["solidworks"].update(
            build=None,
            license=None,
            visible=None,
            unavailable={
                "GetBuildNumbers": "build getter failed",
                "GetCurrentLicenseType": "license getter failed",
            },
        )
        objects = self.objects(runtime=runtime)
        unavailable = {
            row["file"]: set(row["fields"])
            for row in objects
            if row["rule_id"] == "metadata.platform_diagnostic"
        }
        self.assertTrue(
            {
                "/solidworks/build",
                "/solidworks/license",
                "/solidworks/visible",
                "/solidworks/unavailable",
            }
            <= unavailable["evidence/native-runtime.json"]
        )

    def test_optional_api_diagnostics_are_retained_without_type_coercion(self) -> None:
        for key, value in (
            ("build", ""),
            ("build", ["2026", "1"]),
            ("license", True),
            ("visible", 1),
        ):
            with self.subTest(key=key):
                runtime = runtime_environment()
                runtime["solidworks"][key] = value
                objects = self.objects(runtime=runtime)
                self.assertIn(
                    f"/solidworks/{key}",
                    {
                        field
                        for row in objects
                        if row["rule_id"] == "metadata.platform_diagnostic"
                        for field in row["fields"]
                    },
                )

    def test_unknown_or_mismatched_toolchain_is_rejected(self) -> None:
        mutations = (
            ("schema_version", "unknown-tool/v999"),
            ("pipeline_id", "another-pipeline"),
            ("version", "999.0.0"),
            ("source_sha256", "f" * 64),
        )
        for key, value in mutations:
            with self.subTest(key=key):
                runtime = runtime_environment()
                runtime["toolchain"][key] = value
                with self.assertRaises(PipelineError):
                    self.warnings(runtime=runtime)

        runtime = runtime_environment()
        runtime["toolchain"]["runtime"]["packages"]["pywin32"] = "999"
        with self.assertRaisesRegex(PipelineError, "runtime differs"):
            self.warnings(runtime=runtime)

    def test_unknown_state_diagnostics_are_allowed_without_fabricated_success(self) -> None:
        runtime = runtime_environment()
        state = runtime["prepared_capture"]["state_before"]
        self.assertIsNone(state["title"])
        self.assertIsNone(state["configurations"])
        self.assertIsNone(state["save_flag"])
        self.assertIsNone(state["saved"])

        warnings = self.warnings(runtime=runtime)
        self.assertTrue(warnings)
        files = {row["file"] for row in warnings[0]["objects"]}
        self.assertEqual(files, {"evidence/native-runtime.json"})

    def test_single_prepared_copy_session_fields_are_projected(self) -> None:
        objects = self.objects(runtime=runtime_environment())
        fields = {
            field
            for row in objects
            if row["file"] == "evidence/native-runtime.json"
            for field in row["fields"]
        }
        self.assertIn("/solidworks/sessions/copy", fields)
        self.assertNotIn("/solidworks/sessions/source", fields)
        self.assertIn("/prepared_capture/opened/opened", fields)
        self.assertIn("/prepared_capture/policy/mode", fields)


class NativeMetadataTests(unittest.TestCase):
    def document(self, save_flag):
        return SimpleNamespace(
            GetPathName=Mock(spec=lambda: None, return_value=r"C:\captures\robot.SLDASM"),
            GetTitle=Mock(spec=lambda: None, return_value="robot.SLDASM"),
            GetSaveFlag=Mock(spec=lambda: None, return_value=save_flag),
            IsOpenedReadOnly=Mock(spec=lambda: None, return_value=True),
            ConfigurationManager=SimpleNamespace(
                ActiveConfiguration=SimpleNamespace(Name="Default")
            ),
        )

    def test_document_state_retains_the_same_save_flag_observation(self) -> None:
        doc = self.document(False)
        backend = SolidWorksBackend()
        backend._ensure_document = lambda path: doc

        state = backend.document_state(r"C:\captures\robot.SLDASM")

        self.assertIs(state["save_flag"], False)
        self.assertIs(state["saved"], True)
        self.assertEqual(doc.GetSaveFlag.call_count, 1)

    def test_document_state_retains_unknown_save_flag_diagnostics(self) -> None:
        for value in (None, 0, 1, "", "false", []):
            with self.subTest(value=value):
                backend = SolidWorksBackend()
                doc = self.document(value)
                backend._ensure_document = lambda path, doc=doc: doc
                state = backend.document_state(r"C:\captures\robot.SLDASM")
                self.assertIn("save_flag", state)
                self.assertIsNone(state["saved"])
                self.assertTrue(state.get("save_flag_error"))

    def test_environment_retains_optional_failures_and_invalid_types(self) -> None:
        def unavailable():
            raise RuntimeError("unavailable")

        app = SimpleNamespace(
            RevisionNumber=lambda: "34.4.1",
            GetBuildNumbers=unavailable,
            GetCurrentLicenseType=unavailable,
            Visible=unavailable,
        )
        backend = SolidWorksBackend()
        backend._app_obj = lambda: app
        facts = backend.environment()
        self.assertIsNone(facts["build"])
        self.assertIsNone(facts["license"])
        self.assertIsNone(facts["visible"])
        self.assertEqual(facts["unavailable"]["GetBuildNumbers"], "unavailable")
        self.assertEqual(facts["unavailable"]["GetCurrentLicenseType"], "unavailable")

        for member, key, value in (
            ("GetBuildNumbers", "build", ["2026", "1"]),
            ("GetCurrentLicenseType", "license", True),
            ("Visible", "visible", 1),
        ):
            with self.subTest(member=member):
                app = SimpleNamespace(
                    RevisionNumber=lambda: "34.4.1",
                    GetBuildNumbers=lambda: "2026.1",
                    GetCurrentLicenseType=lambda: 1,
                    Visible=True,
                )
                setattr(app, member, value)
                backend = SolidWorksBackend()
                backend._app_obj = lambda app=app: app
                facts = backend.environment()
                self.assertIn(key, facts)
                self.assertIn(member, facts["unavailable"])


if __name__ == "__main__":
    unittest.main()
