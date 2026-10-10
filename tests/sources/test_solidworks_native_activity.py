"""Portable producer-contract regressions for ``discover_native`` activity.

The activity stream is an emitter-side contract only: records carry phase/action
tokens plus optional safe object names and honest completed/total pairs.  The
collector owns timestamps and sequencing, activity never gates a run, and a
missing or failing callback must not change the discovery record.  No CAD and
no Windows is involved.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from description_pipeline.sources.solidworks import native as native_module
from description_pipeline.sources.solidworks.native import SolidWorksBackend

from tests.sources.test_solidworks_native_reader import (
    _App,
    _Component,
    _Doc,
    _Session,
    _com_stubs,
    _write,
)

ACTION_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
ALLOWED_KEYS = {"phase", "action", "current_object", "params", "completed", "total"}


def _scene(root: Path, count: int = 1):
    base = _write(root, "base.SLDPRT")
    components = [_Component(f"base-{index}", base) for index in range(1, count + 1)]
    assembly = _write(root, "robot.SLDASM")
    doc = _Doc(assembly, children=components)
    return _App({assembly: doc})


def _discover(root: Path, app, callback=None):
    backend = SolidWorksBackend(session_factory=lambda: _Session(app))
    with _com_stubs():
        return backend.discover_native(root, {}, on_activity=callback)


class ActivityObjectTests(unittest.TestCase):
    def test_unsafe_names_are_rejected_or_reduced_to_safe_relative_names(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            part = _write(root, "base.SLDPRT")
            self.assertEqual(native_module._activity_object(part, root), "base.SLDPRT")
        obj = native_module._activity_object
        self.assertIsNone(obj(None))
        self.assertIsNone(obj(""))
        self.assertIsNone(obj(".."))
        self.assertIsNone(obj("../escape"))
        self.assertIsNone(obj("upper/../../etc"))
        self.assertEqual(obj(r"C:\Users\33985\AppData\Local\Temp\IC~~\part.step.SLDPRT"), "part.step.SLDPRT")
        self.assertEqual(obj("sub dir/child-1"), "sub dir/child-1")
        long_name = "a" * 200
        reduced = obj(long_name)
        self.assertEqual(len(reduced), 160)
        self.assertIn("…", reduced)


class ActivityEmissionTests(unittest.TestCase):
    def test_stream_shape_counts_and_sanitization(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            records = []
            record = _discover(root, _scene(root), records.append)

            self.assertTrue(records)
            last_completed = {}
            seen = set()
            for item in records:
                self.assertLessEqual(set(item), ALLOWED_KEYS)
                self.assertEqual(item["phase"], "discover")
                self.assertTrue(ACTION_RE.match(item["action"]), item["action"])
                if "completed" in item or "total" in item:
                    self.assertIn("completed", item)
                    self.assertIn("total", item)
                    self.assertIs(type(item["completed"]), int)
                    self.assertIs(type(item["total"]), int)
                    self.assertGreaterEqual(item["completed"], 0)
                    self.assertLessEqual(item["completed"], item["total"])
                    key = (item["phase"], item["action"])
                    self.assertGreaterEqual(item["completed"], last_completed.get(key, 0))
                    last_completed[key] = item["completed"]
                if "current_object" in item:
                    name = item["current_object"]
                    self.assertLessEqual(len(name), 160)
                    self.assertNotIn(":", name)
                    self.assertNotIn("..", name.split("/"))
                if "params" in item:
                    self.assertLessEqual(len(item["params"]), 6)
                seen.add(item["action"])

            for expected in (
                "scan_documents",
                "rebuild",
                "read_components",
                "read_mates",
                "read_datums",
                "hash_sources",
                "build_record",
            ):
                self.assertIn(expected, seen)

            component_reads = [item for item in records if item["action"] == "read_components"]
            self.assertEqual(component_reads[-1]["completed"], len(record["components"]))
            self.assertEqual(component_reads[-1]["total"], len(record["components"]))
            hash_reads = [item for item in records if item["action"] == "hash_sources"]
            self.assertEqual(hash_reads[-1]["completed"], hash_reads[-1]["total"])

    def test_recording_callback_never_changes_the_record(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            baseline = _discover(root, _scene(root), None)
            records = []
            with_callback = _discover(root, _scene(root), records.append)
            self.assertTrue(records)
            self.assertEqual(baseline, with_callback)

    def test_failing_callback_never_changes_the_record(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            baseline = _discover(root, _scene(root), None)

            def exploded(_record):
                raise RuntimeError("collector exploded")

            with_callback = _discover(root, _scene(root), exploded)
            self.assertEqual(baseline, with_callback)

    def test_zero_interval_emits_every_component_exactly_once(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = native_module._ACTIVITY_MIN_INTERVAL_SECONDS
            try:
                native_module._ACTIVITY_MIN_INTERVAL_SECONDS = 0.0
                records = []
                record = _discover(root, _scene(root, count=3), records.append)
            finally:
                native_module._ACTIVITY_MIN_INTERVAL_SECONDS = original
            reads = [item for item in records if item["action"] == "read_components"]
            self.assertEqual([item["completed"] for item in reads], [0, 1, 2, 3])
            self.assertEqual({item["total"] for item in reads}, {3})
            self.assertEqual(len(record["components"]), 3)
            objects = [item["current_object"] for item in reads if "current_object" in item]
            self.assertEqual(sorted(set(objects)), ["base-1", "base-2", "base-3"])

    def test_large_interval_coalesces_component_events_but_keeps_boundaries(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = native_module._ACTIVITY_MIN_INTERVAL_SECONDS
            try:
                native_module._ACTIVITY_MIN_INTERVAL_SECONDS = 10_000.0
                records = []
                _discover(root, _scene(root, count=3), records.append)
            finally:
                native_module._ACTIVITY_MIN_INTERVAL_SECONDS = original
            reads = [item for item in records if item["action"] == "read_components"]
            self.assertLessEqual(len(reads), 2)
            self.assertGreaterEqual(len(reads), 1)
            self.assertEqual(reads[0]["completed"], 0)
            self.assertEqual(reads[-1]["completed"], 3)


if __name__ == "__main__":
    unittest.main()
