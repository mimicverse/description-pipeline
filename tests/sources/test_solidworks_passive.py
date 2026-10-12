"""Native-only passive completion preserves the free rotation and offset axis."""

import copy
import unittest

from . import _paths  # noqa: F401

from description_pipeline.sources.solidworks.passive import complete_passive_leaves, resolve_mate_frame
from description_pipeline.sources.solidworks.scene import _link_frame
from description_pipeline.sources.solidworks.freeze import _capture_axis_references


def native_record():
    placement = [0., -1., 0., 2., 1., 0., 0., 3., 0., 0., 1., 4., 0., 0., 0., 1.]
    center = [0.01, 0.02, 0.]
    def entity(component, shape):
        geometry = {"center": center, "normal": [0., 0., 1.], "radius": 0.004} if shape == "circle" else {
            "point": center, "normal": [0., 0., 1.],
        }
        return {"component": component, shape: geometry}
    return {
        "components": [
            {"name2": name, "document": name + ".SLDPRT", "suppressed": False,
             "fixed": name == "base", "transform": placement}
            for name in ("base", "washer")
        ],
        "mates": [
            {"scope": "", "name": "axis", "type": "concentric", "suppressed": False,
             "error_code": 0, "limits": None, "entities": [entity(name, "circle") for name in ("base", "washer")]},
            {"scope": "", "name": "seat", "type": "coincident", "suppressed": False,
             "error_code": 0, "limits": None, "entities": [entity(name, "plane") for name in ("base", "washer")]},
        ],
    }


class PassiveCompletionTests(unittest.TestCase):
    def setUp(self):
        self.raw = native_record()
        self.assigned = {"base": "base_link"}

    def complete(self):
        return complete_passive_leaves(self.raw, self.assigned, {"base_link"})

    def test_native_offset_axis_is_preserved(self):
        before = copy.deepcopy(self.raw)
        result, = self.complete()
        self.assertEqual(result["rank"], 5)
        self.assertEqual(result["parent"], "base_link")
        self.assertEqual(result["axis"]["local_point"], [.01, .02, 0.])
        self.assertEqual(result["axis"]["point"], [1.98, 3.01, 4.])
        self.assertEqual(result["axis"]["direction"], [0., 0., 1.])
        self.assertEqual(self.raw, before)
        self.assertNotIn("limits", result)
        frame = {"frame": {"mate_axis": result["axis_reference"]}}
        rotation, point = _link_frame(frame, {}, {
            "washer": {"selector": result["axis_reference"], **result["axis"]},
        })
        self.assertEqual(point, (1.98, 3.01, 4.))
        self.assertEqual(rotation[0], (0., -1., 0.))

    def test_no_completion_without_axial_position_constraint(self):
        self.raw["mates"].pop()
        self.assertEqual(self.complete(), [])

    def test_unknown_active_mate_prevents_completion(self):
        other = copy.deepcopy(self.raw["mates"][0])
        other.update(name="unknown", type="gear")
        self.raw["mates"].append(other)
        self.assertEqual(self.complete(), [])

    def test_suppressed_extra_mate_does_not_change_native_free_dof(self):
        other = copy.deepcopy(self.raw["mates"][0])
        other.update(name="unknown", type="gear", suppressed=True)
        self.raw["mates"].append(other)
        self.assertEqual(len(self.complete()), 1)

    def test_unreadable_solver_and_authored_limits_prevent_completion(self):
        for key, value in (
            ("error_code", None), ("error_code", 1), ("error_code", False), ("suppressed", None),
            ("limits", {"lower": 0., "upper": 1.}),
        ):
            with self.subTest(key=key, value=value):
                self.raw = native_record()
                self.raw["mates"][0][key] = value
                self.assertEqual(self.complete(), [])

    def test_another_peer_body_prevents_completion(self):
        other = copy.deepcopy(self.raw["components"][0])
        other["name2"] = "second"
        self.raw["components"].append(other)
        extra = copy.deepcopy(self.raw["mates"][1])
        extra["name"] = "other_seat"
        extra["entities"][0]["component"] = "second"
        self.raw["mates"].append(extra)
        self.assigned["second"] = "another_link"
        self.assertEqual(self.complete(), [])

    def test_displaced_or_tilted_coincident_plane_prevents_completion(self):
        for field, value in (("point", [.01, .02, .003]), ("normal", [1., 0., 0.])):
            with self.subTest(field=field):
                self.raw = native_record()
                self.raw["mates"][1]["entities"][0]["plane"][field] = value
                self.assertEqual(self.complete(), [])

    def test_noncoaxial_circles_prevent_completion(self):
        self.raw["mates"][0]["entities"][0]["circle"]["center"] = [.2, .3, 0.]
        self.assertEqual(self.complete(), [])

    def test_duplicate_selector_identity_prevents_completion(self):
        self.raw["mates"].append(copy.deepcopy(self.raw["mates"][0]))
        self.assertEqual(self.complete(), [])

    def test_declared_leaf_and_fixed_leaf_are_never_reassigned(self):
        self.assigned["washer"] = "declared_link"
        self.assertEqual(self.complete(), [])
        self.assigned.pop("washer")
        self.raw["components"][1]["fixed"] = True
        self.assertEqual(self.complete(), [])

    def test_selector_does_not_accept_missing_or_duplicate_mates(self):
        result, = self.complete()
        selector = result["axis_reference"]
        self.raw["mates"].pop(0)
        with self.assertRaisesRegex(Exception, "uniquely"):
            resolve_mate_frame(self.raw, selector)

    def test_passive_capture_does_not_repeat_native_axis_reads(self):
        class NoNativeRead:
            def capture_axis_reference(self, reference):
                raise AssertionError("sealed mate geometry must be resolved on Linux")

        result, = self.complete()
        self.assertEqual(_capture_axis_references(NoNativeRead(), {
            "joints": [{"name": result["joint"], "axis_reference": result["axis_reference"]}],
        }), [])

