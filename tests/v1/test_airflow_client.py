"""Shared endpoint configuration and result-validation tests."""

from __future__ import annotations

import tempfile
import unittest
import uuid
from pathlib import Path

from description_pipeline.orchestration.airflow_client import (
    EndpointConfig,
    EndpointProtocolError,
    PIPELINE_ID,
    ResultNotPublishable,
    check_result,
    native_run_id,
    validate_artifact_name,
    verified_result,
)

TOKEN = "test-token"
RUN_ID = "b3f1c2d4-5e6f-4a7b-8c9d-0e1f2a3b4c5d"
SHA = "a" * 64


class ClientTests(unittest.TestCase):
    def test_filesystem_root_and_relative_roots_are_refused(self) -> None:
        for roots in ("/", ["relative/root"], [123]):
            with (
                self.subTest(roots=roots),
                self.assertRaises(EndpointProtocolError),
            ):
                EndpointConfig(
                    base_url="http://127.0.0.1",
                    token=TOKEN,
                    handoff_roots=roots,
                )

    def test_discovery_settings_are_validated_and_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            records = root / "records"
            records.mkdir()
            config = EndpointConfig(
                base_url="http://127.0.0.1",
                token=TOKEN,
                discovery={"record_roots": [str(records)]},
            )
            self.assertEqual(config.discovery["record_roots"], (records,))
            for bad in (
                {"record_roots": ["relative/root"]},
                {"record_roots": [str(root / "missing")]},
                {"frozen_names_file": str(records)},
                {"record_roots": "records"},
                {"unknown": []},
                "records",
            ):
                with (
                    self.subTest(bad=str(bad)),
                    self.assertRaises(EndpointProtocolError),
                ):
                    EndpointConfig(
                        base_url="http://127.0.0.1",
                        token=TOKEN,
                        discovery=bad,
                    )
            link = root / "linked"
            link.symlink_to(records, target_is_directory=True)
            with self.assertRaises(EndpointProtocolError):
                EndpointConfig(
                    base_url="http://127.0.0.1",
                    token=TOKEN,
                    discovery={"record_roots": [str(link)]},
                )

    def test_discovery_record_paths_reject_root_and_dotdot_segments(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            records = root / "records"
            records.mkdir()
            nested = root / "sub"
            nested.mkdir()
            for value in (
                str(records / ".." / "records"),
                str(nested / ".." / "records"),
                str(nested / ".." / "missing"),
                str(nested) + "/../records",
                str(nested) + "/..",
                str(nested) + "//records",
                "/" + str(records),
                "/",
            ):
                with self.subTest(record_root=value):
                    with self.assertRaises(
                        EndpointProtocolError
                    ) as caught:
                        EndpointConfig(
                            base_url="http://127.0.0.1",
                            token=TOKEN,
                            discovery={"record_roots": [value]},
                        )
                    self.assertIn(
                        "discovery record root must be a dedicated absolute path",
                        str(caught.exception),
                    )

    def test_linux_routing_map_is_validated(self) -> None:
        config = EndpointConfig(
            base_url="http://127.0.0.1",
            token=TOKEN,
            repositories={
                "mimicverse/description": "/home/andy/Work/description"
            },
        )
        self.assertEqual(
            config.repositories,
            {"mimicverse/description": Path("/home/andy/Work/description")},
        )
        for kwargs in (
            {"repositories": {"noslug": "/srv/repo"}},
            {"repositories": {"owner/repo": "relative/repo"}},
            {"repositories": ["owner/repo"]},
        ):
            with (
                self.subTest(kwargs=kwargs),
                self.assertRaises(EndpointProtocolError),
            ):
                EndpointConfig(
                    base_url="http://127.0.0.1",
                    token=TOKEN,
                    **kwargs,
                )

    def test_native_run_id_is_canonical_and_stable(self) -> None:
        expected = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"solidworks_to_urdf:{RUN_ID}",
            )
        )
        self.assertEqual(native_run_id(RUN_ID), expected)
        self.assertEqual(native_run_id(RUN_ID), native_run_id(RUN_ID))
        with self.assertRaises(EndpointProtocolError):
            native_run_id("")

    def test_publication_failure_never_hides_the_verified_model(self) -> None:
        result = {
            "passed": False,
            "pipeline_id": PIPELINE_ID,
            "stage": "publish",
            "state": "failed",
            "subject_sha256": SHA,
            "quality": {
                "passed": True,
                "subject_sha256": SHA,
                "checks": [{"id": "x", "state": "passed"}],
            },
            "submission": {
                "passed": False,
                "subject_sha256": SHA,
                "state": "failed",
                "error": "git_failed",
                "commit": "",
                "base": "feature/m3.0",
                "branch": (
                    "work/description/m3.0/"
                    "0123456789abcdef01234567"
                ),
                "repository_slug": "example/m3.0",
            },
        }
        self.assertIs(verified_result(result), result)
        with self.assertRaises(ResultNotPublishable) as raised:
            check_result(
                result,
                expected_slug="example/m3.0",
                expected_base="feature/m3.0",
            )
        self.assertEqual(str(raised.exception), "result.passed is not true")
        for bad in (
            None,
            {**result, "pipeline_id": "other-pipeline"},
            {**result, "subject_sha256": "not-a-digest"},
            {
                key: value
                for key, value in result.items()
                if key != "quality"
            },
            {**result, "quality": None},
            {
                **result,
                "quality": {**result["quality"], "passed": False},
            },
            {
                **result,
                "quality": {
                    **result["quality"],
                    "subject_sha256": "b" * 64,
                },
            },
        ):
            with (
                self.subTest(bad=bad),
                self.assertRaises(ResultNotPublishable),
            ):
                verified_result(bad)

    def test_input_validation(self) -> None:
        with self.assertRaises(EndpointProtocolError):
            EndpointConfig(base_url="http://example.com", token=TOKEN)
        self.assertEqual(
            validate_artifact_name("urdf/robot.urdf"),
            "urdf/robot.urdf",
        )
        with self.assertRaises(EndpointProtocolError):
            validate_artifact_name("../escape.urdf")

    def test_invalid_url_timeout_and_redirect_fail_closed(self) -> None:
        for address in (
            "http://127.0.0.1:invalid",
            "http://127.0.0.1?token=x",
            "http://127.0.0.1#fragment",
        ):
            with (
                self.subTest(address=address),
                self.assertRaises(EndpointProtocolError),
            ):
                EndpointConfig(base_url=address, token=TOKEN)
        for timeout in (0, -1, float("inf"), float("nan")):
            with (
                self.subTest(timeout=timeout),
                self.assertRaises(EndpointProtocolError),
            ):
                EndpointConfig(
                    base_url="http://127.0.0.1",
                    token=TOKEN,
                    timeout=timeout,
                )


if __name__ == "__main__":
    unittest.main()
