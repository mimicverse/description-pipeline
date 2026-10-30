"""Atomic JSON records must survive a concurrent reader on Windows.

``os.replace`` needs the destination to be replaceable, and a reader holding the previous record open
makes both the replace and a concurrent ``open`` fail with a transient sharing violation
(``PermissionError``).  The job record is polled by clients while the runner persists it, so both
operations retry briefly; these tests pin that policy without needing a Windows host.
"""

import gzip
import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from description_pipeline import io as io_module
from description_pipeline.io import PipelineError
from description_pipeline.sources.solidworks import jsonio


class AtomicJsonSharingTests(unittest.TestCase):
    def test_write_retries_a_transient_sharing_violation(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "job.json"
            real_replace = os.replace
            calls = {"count": 0}

            def replace(source, destination):
                calls["count"] += 1
                if calls["count"] == 1:
                    raise PermissionError(13, "The process cannot access the file")
                return real_replace(source, destination)

            # The canonical bytes and the atomic replace are owned by io.write_json;
            # jsonio keeps only the sharing-violation retry around that write.
            with mock.patch.object(io_module.os, "replace", side_effect=replace):
                jsonio.write_json(path, {"job_id": "a", "state": "running"})

            self.assertEqual(calls["count"], 2)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["state"], "running")
            self.assertEqual([entry.name for entry in path.parent.iterdir()], ["job.json"])

    def test_read_retries_a_transient_sharing_violation(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "job.json"
            jsonio.write_json(path, {"job_id": "a", "state": "failed"})
            real_open = open
            failures = {"count": 0}

            def failing_once(file, *args, **kwargs):
                if str(file) == str(path) and failures["count"] == 0:
                    failures["count"] += 1
                    raise PermissionError(13, "The process cannot access the file")
                return real_open(file, *args, **kwargs)

            with mock.patch("builtins.open", side_effect=failing_once):
                payload = jsonio.read_json(path)

            self.assertEqual(failures["count"], 1)
            assert isinstance(payload, dict)
            self.assertEqual(payload["state"], "failed")

    def test_read_json_delegates_the_strict_foundation_parse(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "record.json"
            for payload in ('{"a": 1, "a": 2}', '{"a": NaN}', '{"a": Infinity}', '{"a": 1e999}'):
                with self.subTest(payload=payload):
                    path.write_text(payload, encoding="utf-8")
                    with self.assertRaises(PipelineError):
                        jsonio.read_json(path)
            path.write_text('{"a": 1}', encoding="utf-8")
            self.assertEqual(jsonio.read_json(path), {"a": 1})

    def test_a_permanent_violation_still_fails(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "job.json"
            jsonio.write_json(path, {"job_id": "a"})

            with (
                mock.patch("builtins.open", side_effect=PermissionError(13, "denied")),
                self.assertRaises(PermissionError),
            ):
                jsonio.read_json(path)


class CompressedJsonTests(unittest.TestCase):
    def test_canonical_gzip_roundtrip_is_independent_of_filename_and_time(self):
        payload = {
            "component": "\u90e8\u4ef6-1",
            "values": [0, -0.0, 1.25, 1e-150],
            "nested": {"enabled": True, "missing": None},
        }
        with TemporaryDirectory() as temporary:
            first = Path(temporary) / "first.json.gz"
            second = Path(temporary) / "second.json.gz"
            with mock.patch("time.time", return_value=1):
                io_module.write_json(first, payload)
            with mock.patch("time.time", return_value=2):
                io_module.write_json(second, payload)
            compressed = first.read_bytes()
            self.assertEqual(compressed, second.read_bytes())
            self.assertEqual(gzip.decompress(compressed), io_module.canonical(payload))
            self.assertEqual(io_module.read_data(first), payload)
            self.assertEqual(io_module.parse_data(compressed, second), payload)
            self.assertEqual(sorted(entry.name for entry in first.parent.iterdir()),
                             ["first.json.gz", "second.json.gz"])

    def test_corrupt_or_truncated_gzip_is_refused(self):
        compressed = gzip.compress(b'{"value": 1}\n', mtime=0)
        corrupt_crc = compressed[:-8] + bytes([compressed[-8] ^ 1]) + compressed[-7:]
        for payload in (b"not gzip", compressed[:-1], corrupt_crc):
            with self.subTest(payload=payload), self.assertRaises(PipelineError):
                io_module.parse_data(payload, Path("record.json.gz"))

    def test_gzip_suffix_requires_compressed_json(self):
        with self.assertRaises(PipelineError):
            io_module.parse_data(b'{"value": 1}\n', Path("record.json.gz"))
        with self.assertRaises(PipelineError):
            io_module.parse_data(gzip.compress(b'{"value": 1}\n'), Path("record.json"))

    def test_compressed_json_preserves_strict_number_and_key_validation(self):
        for payload in ('{"a": 1, "a": 2}', '{"a": NaN}', '{"a": Infinity}',
                        '{"a": -Infinity}', '{"a": 1e999}'):
            with self.subTest(payload=payload), self.assertRaises(PipelineError):
                io_module.parse_data(gzip.compress(payload.encode("utf-8")), Path("record.json.gz"))


if __name__ == "__main__":
    unittest.main()
