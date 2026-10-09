"""Focused regressions for the bounded streaming folder upload (multipart admission)."""

from __future__ import annotations

import collections
import io
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from description_pipeline.io import PipelineError
from description_pipeline.orchestration import uploads
from description_pipeline.orchestration.uploads import (
    UploadGate,
    UploadRejected,
    receive_folder,
)
from description_pipeline.sources.solidworks.handoff import describe_handoff

BOUNDARY = b"testboundary"
CONTENT_TYPE = "multipart/form-data; boundary=testboundary"
Usage = collections.namedtuple("Usage", "total used free")


def multipart(files, boundary: bytes = BOUNDARY) -> bytes:
    chunks = []
    for name, data in files:
        chunks.append(b"--" + boundary + b"\r\n")
        chunks.append(b'Content-Disposition: form-data; name="files"; filename="' + name.encode("utf-8") + b'"\r\n')
        chunks.append(b"Content-Type: application/octet-stream\r\n\r\n")
        chunks.append(data)
        chunks.append(b"\r\n")
    chunks.append(b"--" + boundary + b"--\r\n")
    return b"".join(chunks)


def environ_for(payload: bytes, *, content_type: str = CONTENT_TYPE, declared: int | None = None) -> dict:
    return {
        "CONTENT_TYPE": content_type,
        "CONTENT_LENGTH": str(len(payload) if declared is None else declared),
        "wsgi.input": io.BytesIO(payload),
    }


FOLDER = [
    ("夹具/model.SLDASM", b"assembly-bytes"),
    ("夹具/parts/part1.SLDPRT", b"part-bytes-1"),
    ("夹具/parts/part2.SLDPRT", b"part-bytes-2"),
]


class UploadTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.intake = Path(temporary.name) / "intake"
        self.intake.mkdir()
        self.gate = UploadGate(2)

    def receive(self, payload: bytes, *, run_id: str = "portal-test", **overrides):
        environ = environ_for(payload)
        environ.update(overrides.pop("environ", {}))
        return receive_folder(
            environ,
            intake_root=overrides.pop("intake_root", self.intake),
            run_id=run_id,
            principal=overrides.pop("principal", "cli_app:tenant-a:ou_worker"),
            gate=self.gate,
        )

    def assert_rejected(self, payload: bytes, status: int, *, run_id: str = "portal-bad", **overrides):
        with self.assertRaises(UploadRejected) as raised:
            self.receive(payload, run_id=run_id, **overrides)
        self.assertEqual(raised.exception.status, status, raised.exception)
        self.assertFalse((self.intake / run_id).exists())
        staging = self.intake / ".staging"
        self.assertTrue(not staging.exists() or not any(staging.iterdir()))
        return raised.exception

    def test_folder_upload_finalizes_immutably_and_matches_filesystem_digest(self) -> None:
        receipt = self.receive(multipart(FOLDER))
        self.assertEqual(receipt.folder, "夹具")
        self.assertEqual(receipt.files, 3)
        self.assertEqual(receipt.bytes, sum(len(data) for _, data in FOLDER))
        final = self.intake / "portal-test" / "夹具"
        self.assertTrue(final.is_dir())
        self.assertEqual(os.stat(final / "model.SLDASM").st_mode & 0o777, 0o444)
        self.assertEqual(os.stat(final / "parts").st_mode & 0o777, 0o555)
        self.assertFalse(any((self.intake / ".staging").iterdir()))
        mirror = final.parent.parent / "mirror"
        shutil.copytree(final, mirror)
        for path in sorted(mirror.rglob("*")):
            os.chmod(path, 0o644 if path.is_file() else 0o755)
        observed = describe_handoff(mirror)["handoff_sha256"]
        self.assertEqual(observed, receipt.handoff_sha256)

    def test_duplicate_and_alias_members_are_rejected(self) -> None:
        self.assert_rejected(multipart([("top/a.SLDASM", b"x"), ("top/a.SLDASM", b"x")]), 400)
        self.assert_rejected(multipart([("top/a.SLDASM", b"x"), ("top/A.sldasm", b"x")]), 400)
        self.assert_rejected(multipart([("top/a", b"x"), ("top/a/b.SLDASM", b"x")]), 400)
        self.assert_rejected(multipart([("top/a.SLDASM", b"x"), ("TOP/b.SLDASM", b"x")]), 400)
        self.assert_rejected(multipart([("top/a.SLDASM", b"x"), ("other/b.SLDASM", b"x")]), 400)

    def test_nonportable_member_names_are_rejected(self) -> None:
        for name in (
            "/absolute/model.SLDASM",
            "top/../model.SLDASM",
            "top/./model.SLDASM",
            "top//model.SLDASM",
            "top/a\\b.SLDASM",
            "top/CON",
            "top/trailing. ",
            "top/bad<name>.SLDASM",
            "top/control\x01.SLDASM",
            "model.SLDASM",
        ):
            with self.subTest(name=name):
                self.assert_rejected(multipart([(name, b"x"), ("top/keep.SLDASM", b"x")]), 400)

    def test_transients_and_generated_inputs_are_rejected(self) -> None:
        error = self.assert_rejected(multipart([("top/~$model.SLDASM", b"x"), ("top/model.SLDASM", b"x")]), 400)
        self.assertIn("~$", str(error))
        self.assert_rejected(multipart([("top/robot.yaml", b"x"), ("top/model.SLDASM", b"x")]), 400)
        self.assert_rejected(multipart([("top/cad-revision.json", b"x"), ("top/model.SLDASM", b"x")]), 400)

    def test_assembly_admission_runs_at_finalize(self) -> None:
        self.assert_rejected(multipart([("top/part.SLDPRT", b"x")]), 400)
        self.assert_rejected(multipart([("top/model.SLDASM", b"")]), 400)
        with mock.patch.object(uploads, "describe_handoff", side_effect=PipelineError("changed during inspection")):
            self.assert_rejected(multipart(FOLDER), 400)

    def test_aggregate_and_member_limits_are_enforced_incrementally(self) -> None:
        with mock.patch.object(uploads, "MAX_FILES", 1):
            self.assert_rejected(multipart(FOLDER), 413)
        with mock.patch.object(uploads, "MAX_FILE_BYTES", 3):
            self.assert_rejected(multipart([("top/model.SLDASM", b"12345")]), 413)
        with mock.patch.object(uploads, "MAX_TOTAL_BYTES", 5):
            self.assert_rejected(multipart([("top/model.SLDASM", b"1234"), ("top/b.SLDPRT", b"5678")]), 413)
        with mock.patch.object(uploads, "MAX_BODY_BYTES", 10):
            self.assert_rejected(multipart(FOLDER), 413)

    def test_transport_shapes_are_rejected(self) -> None:
        error = self.assert_rejected(multipart(FOLDER), 400, environ={"CONTENT_TYPE": "application/json"})
        self.assertIn("文件夹", str(error))
        self.assert_rejected(multipart(FOLDER), 400, environ={"CONTENT_TYPE": "multipart/form-data"})
        environ = environ_for(multipart(FOLDER))
        environ.pop("CONTENT_LENGTH")
        with self.assertRaises(UploadRejected) as raised:
            receive_folder(environ, intake_root=self.intake, run_id="portal-bad", principal="p", gate=self.gate)
        self.assertEqual(raised.exception.status, 411)
        payload = multipart(FOLDER)
        self.assert_rejected(payload, 400, environ={"CONTENT_LENGTH": str(len(payload) + 32)})

    def test_empty_selection_and_wrong_fields_are_rejected(self) -> None:
        self.assert_rejected(b"--" + BOUNDARY + b"--\r\n", 400)
        wrong_name = (
            b"--" + BOUNDARY + b"\r\n"
            b'Content-Disposition: form-data; name="file"; filename="top/model.SLDASM"\r\n'
            b"Content-Type: application/octet-stream\r\n\r\nx\r\n"
            b"--" + BOUNDARY + b"--\r\n"
        )
        self.assert_rejected(wrong_name, 400)
        no_filename = (
            b"--" + BOUNDARY + b"\r\n"
            b'Content-Disposition: form-data; name="files"\r\n'
            b"Content-Type: application/octet-stream\r\n\r\nx\r\n"
            b"--" + BOUNDARY + b"--\r\n"
        )
        self.assert_rejected(no_filename, 400)

    def test_disk_admission_blocks_before_and_during_streaming(self) -> None:
        tiny = Usage(total=100 * 1024**3, used=100 * 1024**3, free=1)
        with mock.patch.object(uploads.shutil, "disk_usage", return_value=tiny):
            self.assert_rejected(multipart(FOLDER), 507)
        self.assertFalse((self.intake / ".staging").exists())
        healthy = Usage(total=100 * 1024**3, used=0, free=100 * 1024**3)
        with (
            mock.patch.object(uploads, "DISK_RECHECK_BYTES", 1),
            mock.patch.object(uploads.shutil, "disk_usage", side_effect=[healthy, tiny, tiny]),
        ):
            self.assert_rejected(multipart(FOLDER), 507)

    def test_gate_bounds_concurrency_per_portal_and_per_principal(self) -> None:
        gate = UploadGate(1)
        with gate.slot("p"):
            with self.assertRaises(UploadRejected) as raised, gate.slot("q"):
                pass
            self.assertEqual(raised.exception.status, 429)
        gate = UploadGate(2)
        with gate.slot("p"):
            with self.assertRaises(UploadRejected) as raised, gate.slot("p"):
                pass
            self.assertEqual(raised.exception.status, 429)
            with gate.slot("q"):
                pass


if __name__ == "__main__":
    unittest.main()
