"""Portable producer-contract regressions for ``discover_native`` activity.

The activity stream is an emitter-side contract only: records carry phase/action
tokens plus optional safe object names and honest completed/total pairs.  The
collector owns timestamps and sequencing, activity never gates a run, and a
missing or failing callback must not change the discovery record.  No CAD and
no Windows is involved.
"""

from __future__ import annotations

import re
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from description_pipeline.sources.solidworks import native as native_module
from description_pipeline.sources.solidworks.native import SolidWorksBackend
from description_pipeline.sources.solidworks.errors import CadError

from tests.sources.test_solidworks_native_reader import (
    _App,
    _CaptureBackend,
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
        return backend.discover_native(root, {"main_assembly": "robot.SLDASM"}, on_activity=callback)


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
        self.assertEqual(obj(r"C:\Users\operator\AppData\Local\Temp\IC~~\part.step.SLDPRT"), "part.step.SLDPRT")
        self.assertEqual(obj("/var/tmp/secret/part.step.SLDPRT"), "part.step.SLDPRT")
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
            record = _discover(root, _scene(root, count=2), records.append)

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
                "session_start",
                "read_document_state",
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
            self.assertEqual(hash_reads[-1]["completed"], len(record["files"]))
            self.assertEqual(hash_reads[-1]["total"], len(record["files"]))
            property_events = [item for item in records if item["action"] == "read_properties"]
            self.assertEqual(property_events[0]["current_object"], "robot.SLDASM")

    def test_transitions_always_emit_even_when_repeats_are_throttled(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            arm_path = _write(root, "arm.SLDPRT")
            sub_path = _write(root, "sub.SLDASM")
            assembly = _write(root, "robot.SLDASM")
            arm_doc = _Doc(arm_path)
            child = _Component("arm-1", arm_path, doc=arm_doc)
            sub_doc = _Doc(sub_path, doc_type=2, children=[child])
            occurrence = _Component("sub-1", sub_path, doc=sub_doc, children=[child])
            main_doc = _Doc(assembly, doc_type=2, children=[occurrence])
            original = native_module._ACTIVITY_MIN_INTERVAL_SECONDS
            try:
                native_module._ACTIVITY_MIN_INTERVAL_SECONDS = 10_000.0
                records = []
                _discover(root, _App({assembly: main_doc, sub_path: sub_doc, arm_path: arm_doc}), records.append)
            finally:
                native_module._ACTIVITY_MIN_INTERVAL_SECONDS = original
            opens = [item for item in records if item["action"] == "open_document"]
            self.assertGreaterEqual(len(opens), 2)
            self.assertIn("sub.SLDASM", [item.get("current_object") for item in opens])

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
            self.assertEqual(len(reads), 4)
            self.assertTrue(all("completed" not in item and "total" not in item for item in reads[:-1]))
            self.assertEqual(reads[-1], {"phase": "discover", "action": "read_components", "completed": 3, "total": 3})
            self.assertEqual(len(record["components"]), 3)
            objects = [item["current_object"] for item in reads if "current_object" in item]
            self.assertEqual(objects, ["base-1", "base-2", "base-3"])
            self.assertLess(
                records.index(reads[-1]),
                next(index for index, item in enumerate(records) if item["action"] == "read_mates"),
            )

    def test_large_interval_emits_each_object_transition_and_final_count(self) -> None:
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
            self.assertEqual(len(reads), 4)
            self.assertEqual(
                [item.get("current_object") for item in reads[:3]],
                ["base-1", "base-2", "base-3"],
            )
            self.assertTrue(all("completed" not in item and "total" not in item for item in reads[:-1]))
            self.assertEqual(reads[-1], {"phase": "discover", "action": "read_components", "completed": 3, "total": 3})
            self.assertLess(
                records.index(reads[-1]),
                next(index for index, item in enumerate(records) if item["action"] == "read_mates"),
            )

    def test_nested_suppressed_and_unresolved_progress_counts_only_returned_primitives(self) -> None:
        for shape in ("nested", "suppressed_and_unresolved"):
            with self.subTest(shape=shape), TemporaryDirectory() as tmp:
                root = Path(tmp)
                leaf = _Component("sub-1/part-1" if shape == "nested" else "part-1", _write(root, "part.SLDPRT"))
                if shape == "nested":
                    sub_path = _write(root, "sub.SLDASM")
                    children = [_Component("sub-1", sub_path, children=[leaf], doc=_Doc(sub_path, children=[leaf]))]
                    transitions = ["sub-1", "sub-1/part-1"]
                else:
                    parked = _Component("parked-1", _write(root, "parked.SLDPRT"), suppressed=True)
                    missing = _Component("missing-1", "", suppressed=True)
                    missing._doc = None
                    missing.GetImportedPath = lambda: None
                    children = [leaf, parked, missing]
                    transitions = ["part-1", "parked-1", "missing-1"]
                assembly = _write(root, "robot.SLDASM")
                records = []
                raw = _discover(root, _App({assembly: _Doc(assembly, children=children)}), records.append)
                reads = [item for item in records if item["action"] == "read_components"]
                self.assertEqual([item.get("current_object") for item in reads[:-1]], transitions)
                self.assertTrue(all("completed" not in item and "total" not in item for item in reads[:-1]))
                self.assertEqual(reads[-1], {
                    "phase": "discover", "action": "read_components", "completed": 2, "total": 2,
                })
                self.assertEqual(len(raw["components"]), 2)
                self.assertEqual(len(raw["unresolved_components"]), 0 if shape == "nested" else 1)
                self.assertLess(
                    records.index(reads[-1]),
                    next(index for index, item in enumerate(records) if item["action"] == "read_mates"),
                )

    def test_failed_instance_walk_emits_no_completion(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            part = _write(root, "part.SLDPRT")
            assembly = _write(root, "robot.SLDASM")
            children = [_Component("good-1", part), _Component("bad-1", part, suppression=7)]
            records = []
            with self.assertRaises(CadError) as caught:
                _discover(root, _App({assembly: _Doc(assembly, children=children)}), records.append)
            self.assertEqual(caught.exception.code, "cad_component_state_unsupported")
            reads = [item for item in records if item["action"] == "read_components"]
            self.assertEqual(reads, [{"phase": "discover", "action": "read_components", "current_object": "good-1"}])

    def test_object_transition_is_emitted_before_repeat_interval(self) -> None:
        records = []
        emitter = native_module._ActivityEmitter(records.append)
        with patch.object(
            native_module.time,
            "monotonic",
            side_effect=[10.0, 10.1, 10.2],
        ):
            emitter.emit(
                "mesh_export",
                phase="capture",
                current_object="object-a",
                completed=2,
                total=85,
            )
            emitter.emit(
                "mesh_export",
                phase="capture",
                current_object="object-b",
                completed=2,
                total=85,
            )
            emitter.emit(
                "mesh_export",
                phase="capture",
                current_object="object-b",
                completed=2,
                total=85,
            )
        emitter.refresh()

        self.assertEqual(len(records), 3)
        self.assertEqual(records[0]["current_object"], "object-a")
        self.assertEqual(records[1]["current_object"], "object-b")
        self.assertEqual(records[-1]["current_object"], "object-b")
        self.assertEqual(records[-1]["completed"], 2)
        self.assertEqual(records[-1]["total"], 85)

    def test_capture_emits_the_walk_and_per_instance_reads_with_names(self) -> None:
        with TemporaryDirectory() as tmp, _com_stubs():
            root = Path(tmp)
            documents = {}
            components = []
            for name in ("arm1", "arm2"):
                path = _write(root, name + ".SLDPRT")
                document = _Doc(path)
                documents[path] = document
                components.append(_Component(name, path, doc=document))
            assembly = _write(root, "robot.SLDASM")
            documents[assembly] = _Doc(assembly, doc_type=2, children=components)
            backend = _CaptureBackend(session_factory=lambda: _Session(_App(documents)))
            original = native_module._ACTIVITY_MIN_INTERVAL_SECONDS
            try:
                native_module._ACTIVITY_MIN_INTERVAL_SECONDS = 0.0
                records = []
                scene = backend.capture_scene_for_test(
                    str(assembly),
                    [],
                    on_activity=records.append,
                )
            finally:
                native_module._ACTIVITY_MIN_INTERVAL_SECONDS = original

            self.assertEqual({component.name for component in scene.components}, {"arm1", "arm2"})
            self.assertTrue(records)
            self.assertTrue(all(item["phase"] == "capture" for item in records))
            walks = [item for item in records if item["action"] == "walk"]
            self.assertEqual(
                sorted(item["current_object"] for item in walks if "current_object" in item), ["arm1", "arm2"]
            )
            self.assertEqual((walks[-1]["completed"], walks[-1]["total"]), (2, 2))
            reads = [item for item in records if item["action"] == "instance_reads"]
            self.assertEqual([item["completed"] for item in reads], [0, 1, 1, 2, 2])
            self.assertEqual({item["total"] for item in reads}, {2})
            self.assertEqual(
                sorted(item["current_object"] for item in reads if "current_object" in item),
                ["arm1", "arm1", "arm2", "arm2"],
            )

    def test_capture_progress_failures_never_fail_the_read(self) -> None:
        with TemporaryDirectory() as tmp, _com_stubs():
            root = Path(tmp)
            path = _write(root, "arm1.SLDPRT")
            document = _Doc(path)
            assembly = _write(root, "robot.SLDASM")
            main = _Doc(assembly, doc_type=2, children=[_Component("arm1", path, doc=document)])
            backend = _CaptureBackend(session_factory=lambda: _Session(_App({assembly: main, path: document})))

            def exploded(_record):
                raise RuntimeError("collector exploded")

            scene = backend.capture_scene_for_test(
                str(assembly),
                [],
                on_activity=exploded,
            )
            self.assertEqual([component.name for component in scene.components], ["arm1"])

    def test_keepalive_refreshes_the_last_record_until_stopped(self) -> None:
        records = []
        emitter = native_module._ActivityEmitter(records.append, keepalive_seconds=0.05)
        emitter.emit("start", phase="capture")
        self.assertEqual(len(records), 1)
        emitter.start_keepalive()
        deadline = time.time() + 5.0
        while len(records) < 3 and time.time() < deadline:
            time.sleep(0.01)
        self.assertGreaterEqual(len(records), 3)
        emitter.stop_keepalive()
        self.assertIsNone(emitter._keepalive_thread)
        settled = len(records)
        time.sleep(0.15)
        self.assertEqual(len(records), settled)
        idle = native_module._ActivityEmitter(None, keepalive_seconds=0.01)
        idle.start_keepalive()
        self.assertIsNone(idle._keepalive_thread)
        idle.stop_keepalive()


if __name__ == "__main__":
    unittest.main()
