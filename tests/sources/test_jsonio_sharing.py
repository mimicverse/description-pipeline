"""Atomic JSON records must survive a concurrent reader on Windows.

``os.replace`` needs the destination to be replaceable, and a reader holding the previous record open
makes both the replace and a concurrent ``open`` fail with a transient sharing violation
(``PermissionError``).  The job record is polled by clients while the runner persists it, so both
operations retry briefly; these tests pin that policy without needing a Windows host.
"""

import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

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

            with mock.patch.object(jsonio.os, "replace", side_effect=replace):
                jsonio.write_json(path, {"job_id": "a", "state": "running"})

            self.assertEqual(calls["count"], 2)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["state"], "running")
            self.assertEqual(list(path.parent.glob("*.tmp")), [])

    def test_read_retries_a_transient_sharing_violation(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "job.json"
            jsonio.write_json(path, {"job_id": "a", "state": "failed"})
            real_open = open
            failures = {"count": 0}

            def failing_once(file, *args, **kwargs):
                if str(file) == str(path) and kwargs.get("encoding") == "utf-8" and failures["count"] == 0:
                    failures["count"] += 1
                    raise PermissionError(13, "The process cannot access the file")
                return real_open(file, *args, **kwargs)

            with mock.patch("builtins.open", side_effect=failing_once):
                payload = jsonio.read_json(path)

            self.assertEqual(failures["count"], 1)
            assert isinstance(payload, dict)
            self.assertEqual(payload["state"], "failed")

    def test_a_permanent_violation_still_fails(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "job.json"
            jsonio.write_json(path, {"job_id": "a"})

            with (
                mock.patch("builtins.open", side_effect=PermissionError(13, "denied")),
                self.assertRaises(PermissionError),
            ):
                jsonio.read_json(path)


if __name__ == "__main__":
    unittest.main()
