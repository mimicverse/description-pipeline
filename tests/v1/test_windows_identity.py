from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path

from .windows_fixtures import valid_source

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "deploy" / "operator"))

import windows_identity  # noqa: E402


class WindowsIdentityTests(unittest.TestCase):
    def test_direct_venv_child_source_is_accepted(self) -> None:
        self.assertTrue(
            windows_identity.sealed_source_listener_matches(valid_source())
        )
        self.assertFalse(hasattr(windows_identity, "listener_evidence_matches"))

    def test_same_process_schema_is_refused(self) -> None:
        source = valid_source()
        source["listener"]["relation"] = "same_process"
        self.assertFalse(windows_identity.sealed_source_listener_matches(source))

    def test_scheduler_service_must_own_the_launcher_parent(self) -> None:
        for field, value in (
            ("scheduler_service_name", "Foreign"),
            ("scheduler_service_state", "Stopped"),
            ("scheduler_service_process_id", 2513),
            ("scheduler_pid", 2513),
            ("scheduler_user_sid", "S-1-5-21-1-2-3-500"),
            ("scheduler_session_id", 1),
        ):
            with self.subTest(field=field):
                source = valid_source()
                source["listener"][field] = value
                self.assertFalse(
                    windows_identity.sealed_source_listener_matches(source)
                )

    def test_process_lineage_is_exact_and_ordered(self) -> None:
        mutations = (
            ("launcher_parent_pid", 999),
            ("listener_parent_pid", 999),
            ("launcher_pid", 15124),
            ("listener_start_utc", "2026-10-26T23:59:59Z"),
            ("launcher_session_id", 2),
            ("listener_user_sid", "S-1-5-21-1-2-3-501"),
        )
        for field, value in mutations:
            with self.subTest(field=field):
                source = valid_source()
                source["listener"][field] = value
                self.assertFalse(
                    windows_identity.sealed_source_listener_matches(source)
                )

    def test_listener_is_bound_to_the_complete_source_context(self) -> None:
        mutations = (
            ("execute", "C:/foreign/runtime/Scripts/python.exe"),
            ("config_path", "C:/foreign/endpoint.json"),
            (
                "arguments",
                '-I -B -m description_pipeline serve --config "C:/foreign/endpoint.json"',
            ),
            ("principal_sid", "S-1-5-21-1-2-3-501"),
            ("port", 70000),
        )
        for field, value in mutations:
            with self.subTest(field=field):
                source = valid_source()
                source[field] = value
                self.assertFalse(
                    windows_identity.sealed_source_listener_matches(source)
                )

    def test_closed_schema_and_digests_are_required(self) -> None:
        for field in (
            "process_arguments_sha256",
            "launcher_image_sha256",
            "venv_config_sha256",
            "base_interpreter_sha256",
        ):
            with self.subTest(field=field):
                source = valid_source()
                source["listener"][field] = "bad"
                self.assertFalse(
                    windows_identity.sealed_source_listener_matches(source)
                )
        source = valid_source()
        source["unexpected"] = True
        self.assertFalse(windows_identity.sealed_source_listener_matches(source))
        source = valid_source()
        del source["listener"]["listener_start_utc"]
        self.assertFalse(windows_identity.sealed_source_listener_matches(source))
        source = copy.deepcopy(valid_source())
        source["schema_version"] = "solidworks-to-urdf.windows-source-state/v1"
        self.assertFalse(windows_identity.sealed_source_listener_matches(source))


if __name__ == "__main__":
    unittest.main()
