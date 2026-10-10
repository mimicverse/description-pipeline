"""Analytic native RefAxis oracle; measured endpoints, never radius or inferred sign."""

import copy
import math
import unittest

import numpy as np

from description_pipeline.io import PipelineError
from description_pipeline.verification.solidworks_urdf import _axis


def example():
    reference = {"kind": "reference_axis", "component": "", "feature_name": "hinge_axis"}
    authored = {"axis_reference": reference, "axis": [0, 0, -1]}
    record = {
        "kind": "reference_axis",
        "selector": dict(reference),
        "component": "",
        "coordinate_frame": "assembly",
        "raw_endpoints_m": [0, 0, 2, 0, 0, 5],
        "axis_point_m": [0, 0, 2],
        "axis_direction": [0, 0, 1],
        "direction_semantics": "undirected_axis_line",
        "used_api": "IFeature.GetSpecificFeature2/IRefAxis.GetRefAxisParams",
    }
    return authored, record


class NativeReferenceAxisTests(unittest.TestCase):
    def test_top_axis_needs_no_radius_and_native_direction_does_not_set_sign(self):
        authored, record = example()
        result = _axis(authored, record, {}, np.eye(4))
        self.assertEqual(result["offset_m"], 0)
        self.assertEqual(result["angle_deg"], 0)
        self.assertEqual(authored["axis"], [0, 0, -1])

    def test_occurrence_local_endpoints_use_exact_component_transform(self):
        authored, record = example()
        authored["axis_reference"]["component"] = "module-2/pin-1"
        record["selector"] = dict(authored["axis_reference"])
        record["component"] = "module-2/pin-1"
        record["coordinate_frame"] = "component_local"
        transform = np.array([[0, 0, 1, 2], [0, 1, 0, 3], [-1, 0, 0, 4], [0, 0, 0, 1]], float)
        result = _axis(authored, record, {"module-2/pin-1": transform}, transform)
        self.assertEqual(result["offset_m"], 0)
        self.assertEqual(result["angle_deg"], 0)
        with self.assertRaises(PipelineError):
            _axis(authored, record, {"module-1/pin-1": transform}, transform)

    def test_native_summary_cannot_override_endpoints_or_provenance(self):
        for patch in [
            {"axis_direction": [0, 1, 0]},
            {"axis_point_m": [1, 0, 2]},
            {"raw_endpoints_m": [0, 0, 2, 0, 0, 2]},
            {"raw_endpoints_m": [0, 0, 2, 0, 0, math.nan]},
            {"kind": "plane"},
            {"used_api": "IRefAxisFeatureData.GetSelections"},
            {"coordinate_frame": "component_local"},
            {"direction_semantics": "signed"},
            {"selector": {"kind": "reference_axis", "component": "", "feature_name": "other"}},
        ]:
            authored, record = example()
            with self.subTest(patch=patch), self.assertRaises(PipelineError):
                _axis(authored, {**record, **patch}, {}, np.eye(4))

    def test_line_position_and_angle_tolerances(self):
        authored, record = example()
        for displacement, passed in [(4.9e-5, True), (5.1e-5, False)]:
            child = np.eye(4)
            child[0, 3] = displacement
            if passed:
                _axis(authored, record, {}, child)
            else:
                with self.assertRaises(PipelineError):
                    _axis(authored, record, {}, child)
        for degrees, passed in [(0.049, True), (0.051, False)]:
            radians = math.radians(degrees)
            candidate = copy.deepcopy(authored)
            candidate["axis"] = [math.sin(radians), 0, -math.cos(radians)]
            # Put the joint at the first measured endpoint, isolating angular error.
            child = np.eye(4)
            child[2, 3] = 2
            if passed:
                _axis(candidate, record, {}, child)
            else:
                with self.assertRaises(PipelineError):
                    _axis(candidate, record, {}, child)


if __name__ == "__main__":
    unittest.main()
