"""Field-level provenance for non-native capture environment metadata."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from . import _paths  # noqa: F401

from description_pipeline.io import PipelineError  # noqa: E402
from description_pipeline.schemas import TOOL_SCHEMA  # noqa: E402
from description_pipeline.sources.solidworks.errors import CadError  # noqa: E402
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
                "source": {
                    "pid": 80,
                    "state": "closed",
                    "lease": {"held": False, "holder_pid": None, "path": r"C:\state\cad.lease"},
                },
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
    }


def runtime_environment() -> dict:
    payload = environment(copy_pid=110)
    payload["session_boundary"] = {
        "closure_top_level": r"C:\captures\robot.SLDASM",
        "configuration": "Default",
        "observed_configuration": "Default",
        "capture_state": {
            "path": r"C:\captures\robot.SLDASM",
            "title": "robot.SLDASM",
            "saved": True,
            "save_flag": False,
            "active_configuration": "Default",
            "configurations": ["Default"],
            "read_only": True,
            "lightweight": None,
            "unsupported": ["lightweight"],
        },
        "retirement": {
            "role": "copy",
            "retired": True,
            "before": {"pid": 100, "state": "active"},
            "after": {"pid": 100, "state": "closed"},
        },
        "opened": {
            "opened": "robot.SLDASM",
            "path": r"C:\captures\robot.SLDASM",
            "read_only": True,
            "errors": 0,
            "warnings": 0,
        },
    }
    return payload


class MetadataProvenanceTests(unittest.TestCase):
    def warnings(self, runtime=None, rewrite=None, expected_tool=None) -> list[dict]:
        return metadata_value_warnings(
            runtime or runtime_environment(),
            rewrite or environment(),
            expected_tool=expected_tool or environment()["toolchain"],
            runtime_sha256="c" * 64,
            rewrite_sha256="d" * 64,
            expected_configuration="Default",
            closure_top_level=r"C:\captures\robot.SLDASM",
        )

    def objects(self, runtime=None, rewrite=None) -> list[dict]:
        warnings = self.warnings(runtime=runtime, rewrite=rewrite)
        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0]["code"], METADATA_WARNING_CODE)
        self.assertEqual(warnings[0]["message"], METADATA_WARNING_MESSAGE)
        return warnings[0]["objects"]

    def test_metadata_warnings_cover_non_native_fields_without_claiming_native_reads(self) -> None:
        objects = self.objects()
        by_rule = {row["rule_id"]: row for row in objects if row["file"].endswith("native-runtime.json")}
        all_fields = {field for row in objects for field in row["fields"]}

        self.assertIn("/session_boundary/capture_state/saved", all_fields)
        self.assertIn("/session_boundary/opened/read_only", all_fields)
        self.assertIn("/session_boundary/observed_configuration", all_fields)
        self.assertIn("/session_boundary/configuration", all_fields)
        self.assertNotIn("/session_boundary/capture_state/save_flag", all_fields)
        self.assertNotIn("/session_boundary/capture_state/read_only", all_fields)
        self.assertNotIn("/solidworks/revision", all_fields)
        self.assertEqual(by_rule["metadata.save_flag_inversion"]["rule_version"], "v1")
        self.assertEqual(by_rule["metadata.operation_claim"]["rule_version"], "v1")

    def test_recovered_capture_keeps_the_initial_session_boundary(self) -> None:
        runtime = runtime_environment()
        runtime["solidworks"]["sessions"]["copy"]["pid"] = 202
        self.objects(runtime=runtime)

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
        rewrite = environment()
        rewrite["solidworks"].update(
            build=None,
            license=None,
            visible=None,
            unavailable={
                "GetBuildNumbers": "build getter failed",
                "GetCurrentLicenseType": "license getter failed",
            },
        )
        objects = self.objects(runtime=runtime, rewrite=rewrite)
        unavailable = {
            row["file"]: set(row["fields"])
            for row in objects
            if row["rule_id"] == "metadata.platform_unavailable"
        }
        self.assertEqual(
            unavailable["evidence/native-runtime.json"],
            {"/solidworks/build", "/solidworks/license", "/solidworks/visible"},
        )
        self.assertEqual(
            unavailable["evidence/evidence/rewrite-environment.json"],
            {"/solidworks/build", "/solidworks/license", "/solidworks/visible"},
        )

    def test_invalid_optional_api_types_and_unbound_null_fail_closed(self) -> None:
        for key, value in (
            ("build", ""),
            ("build", ["2026", "1"]),
            ("license", True),
            ("visible", 1),
        ):
            with self.subTest(key=key):
                runtime = runtime_environment()
                runtime["solidworks"][key] = value
                with self.assertRaisesRegex(PipelineError, "documented native type"):
                    self.warnings(runtime=runtime)

        runtime = runtime_environment()
        runtime["solidworks"]["visible"] = None
        self.assertIn(
            "/solidworks/visible",
            {
                field
                for row in self.objects(runtime=runtime)
                if row["rule_id"] == "metadata.platform_unavailable"
                for field in row["fields"]
            },
        )

        runtime = runtime_environment()
        runtime["solidworks"]["build"] = None
        with self.assertRaisesRegex(PipelineError, "no bound unavailable diagnostic"):
            self.warnings(runtime=runtime)

    def test_session_projection_and_save_inversion_are_revalidated(self) -> None:
        runtime = runtime_environment()
        runtime["session_boundary"]["observed_configuration"] = "Other"
        with self.assertRaisesRegex(PipelineError, "differs from the native active configuration"):
            self.warnings(runtime=runtime)

        runtime = runtime_environment()
        runtime["session_boundary"]["capture_state"]["saved"] = False
        with self.assertRaisesRegex(PipelineError, "boolean inversion"):
            self.warnings(runtime=runtime)

        runtime = runtime_environment()
        runtime["session_boundary"]["capture_state"]["read_only"] = None
        with self.assertRaisesRegex(PipelineError, "observed true boolean"):
            self.warnings(runtime=runtime)

    def test_boundary_conflicts_are_rejected_instead_of_warninged(self) -> None:
        runtime = runtime_environment()
        runtime["session_boundary"]["observed_configuration"] = "Other"
        runtime["session_boundary"]["capture_state"]["active_configuration"] = "Other"
        runtime["session_boundary"]["capture_state"]["configurations"] = ["Other"]
        with self.assertRaisesRegex(PipelineError, "differs from the frozen source configuration"):
            self.warnings(runtime=runtime)

        runtime = runtime_environment()
        other = r"C:\captures\other.SLDASM"
        runtime["session_boundary"]["opened"]["path"] = other
        runtime["session_boundary"]["capture_state"]["path"] = other
        with self.assertRaisesRegex(PipelineError, "do not match the dependency closure"):
            self.warnings(runtime=runtime)

        runtime = runtime_environment()
        runtime["session_boundary"]["opened"]["errors"] = 1
        with self.assertRaisesRegex(PipelineError, "failed or invalid open"):
            self.warnings(runtime=runtime)

        runtime = runtime_environment()
        runtime["session_boundary"]["opened"]["warnings"] = 1
        self.assertTrue(self.objects(runtime=runtime))

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

        rewrite = environment()
        rewrite["toolchain"]["runtime"]["packages"]["pywin32"] = "999"
        with self.assertRaisesRegex(PipelineError, "runtime differs"):
            self.warnings(rewrite=rewrite)

    def test_fresh_session_boundary_is_revalidated(self) -> None:
        runtime = runtime_environment()
        runtime["session_boundary"]["retirement"]["role"] = "source"
        with self.assertRaisesRegex(PipelineError, "retirement.role"):
            self.warnings(runtime=runtime)

        runtime = runtime_environment()
        runtime["session_boundary"]["retirement"]["after"]["pid"] = 101
        with self.assertRaisesRegex(PipelineError, "changed the retired process pid"):
            self.warnings(runtime=runtime)

        runtime = runtime_environment()
        runtime["solidworks"]["sessions"]["copy"]["pid"] = 100
        with self.assertRaisesRegex(PipelineError, "capture reused the retired rewrite process"):
            self.warnings(runtime=runtime)

        runtime = runtime_environment()
        runtime["solidworks"]["sessions"]["source"]["state"] = "active"
        runtime["solidworks"]["sessions"]["source"]["lease"]["held"] = True
        with self.assertRaisesRegex(PipelineError, "source.*closed"):
            self.warnings(runtime=runtime)

    def test_session_process_fields_are_projected(self) -> None:
        objects = self.objects(runtime=runtime_environment(), rewrite=environment())
        rewrite_fields = {
            field
            for row in objects
            if row["file"] == "evidence/evidence/rewrite-environment.json"
            for field in row["fields"]
        }
        self.assertIn("/solidworks/sessions/source", rewrite_fields)
        self.assertIn("/solidworks/sessions/copy", rewrite_fields)


class NativeMetadataTests(unittest.TestCase):
    def document(self, save_flag):
        return SimpleNamespace(
            GetPathName=Mock(spec=lambda: None, return_value=r"C:\captures\robot.SLDASM"),
            GetTitle=Mock(spec=lambda: None, return_value="robot.SLDASM"),
            GetSaveFlag=Mock(spec=lambda: None, return_value=save_flag),
            IsOpenedReadOnly=True,
            ConfigurationManager=SimpleNamespace(
                ActiveConfiguration=SimpleNamespace(Name="Default")
            ),
        )

    def test_document_state_retains_the_same_save_flag_observation(self) -> None:
        doc = self.document(False)
        backend = SolidWorksBackend()
        backend._ensure_document = lambda path: doc
        backend.list_configurations = lambda path: ["Default"]

        state = backend.document_state(r"C:\captures\robot.SLDASM")

        self.assertIs(state["save_flag"], False)
        self.assertIs(state["saved"], True)
        self.assertEqual(doc.GetSaveFlag.call_count, 1)

    def test_document_state_rejects_non_native_save_flag_types(self) -> None:
        for value in (None, 0, 1, "", "false", []):
            with self.subTest(value=value):
                backend = SolidWorksBackend()
                doc = self.document(value)
                backend._ensure_document = lambda path, doc=doc: doc
                backend.list_configurations = lambda path: ["Default"]
                with self.assertRaises(CadError) as raised:
                    backend.document_state(r"C:\captures\robot.SLDASM")
                self.assertEqual(raised.exception.code, "cad_save_flag_invalid")
                self.assertIn("value", raised.exception.detail)

    def test_environment_retains_optional_failures_and_rejects_invalid_success_types(self) -> None:
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

        for member, value in (
            ("GetBuildNumbers", ["2026", "1"]),
            ("GetCurrentLicenseType", True),
            ("Visible", 1),
        ):
            with self.subTest(member=member):
                app = SimpleNamespace(
                    RevisionNumber=lambda: "34.4.1",
                    GetBuildNumbers=lambda: "2026.1",
                    GetCurrentLicenseType=lambda: 1,
                    Visible=lambda: True,
                )
                setattr(app, member, lambda value=value: value)
                backend = SolidWorksBackend()
                backend._app_obj = lambda app=app: app
                with self.assertRaises(CadError) as raised:
                    backend.environment()
                self.assertEqual(raised.exception.code, "cad_environment_value_invalid")


if __name__ == "__main__":
    unittest.main()
