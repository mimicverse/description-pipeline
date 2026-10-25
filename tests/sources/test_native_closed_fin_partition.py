"""Complete closed-edge partitions use topology and parameter adjacency."""
from __future__ import annotations
import copy
import math
import unittest
from . import _paths  # noqa: F401
from description_pipeline.geometry.native_fin_topology import NativeFinRelationError, closed_edge_fin_partition


def cycle(values):
    return {index: {"vertex_ids": [index, (index + 1) % len(values)],
                    "start_u": value, "end_u": values[(index + 1) % len(values)]}
            for index, value in enumerate(values)}


class ClosedPartitionTests(unittest.TestCase):
    def partition(self, fins):
        return closed_edge_fin_partition(component="part", fins=fins, lower=0.0, upper=4.0)

    def test_long_parameter_segment_is_proved_by_all_adjacent_nodes(self):
        proof = self.partition(cycle([0.0, 0.1, 0.2, 0.3]))
        self.assertEqual(proof["parameter_node_count"], 4)
        self.assertEqual(proof["segments"][-1]["intervals"], [[0.3, 4.0]])

    def test_raw_pair_directions_do_not_choose_the_derived_traversal(self):
        fins = cycle([0.0, 1.0, 2.0, 3.0])
        baseline = self.partition(fins)
        for index in (0, 2):
            fins[index]["vertex_ids"].reverse()
            fins[index]["start_u"], fins[index]["end_u"] = fins[index]["end_u"], fins[index]["start_u"]
        self.assertEqual(self.partition(fins), baseline)

    def test_upper_and_lower_are_one_seam_parameter(self):
        fins = cycle([0.0, 1.0, 2.0, 3.0])
        fins[3]["end_u"] = 4.0
        proof = self.partition(fins)
        self.assertEqual(proof["parameter_node_count"], 4)

    def test_collapsed_parameter_does_not_merge_topological_vertices(self):
        proof = self.partition(cycle([0.0, 1.0, 1.0, 2.0, 3.0]))
        self.assertEqual(len(proof["vertex_cycle"]), 6)
        self.assertEqual(proof["parameter_node_count"], 4)
        self.assertEqual(proof["segments"][1]["intervals"], [])

    def test_two_unique_parameters_are_ambiguous(self):
        with self.assertRaisesRegex(NativeFinRelationError, "insufficient unique"):
            self.partition(cycle([0.0, 1.0, 1.0]))

    def test_node_skips_and_duplicate_segments_are_rejected(self):
        for values in ([0.0, 2.0, 1.0, 3.0], [0.0, 1.0, 2.0, 1.0, 3.0]):
            with self.subTest(values=values), self.assertRaises(NativeFinRelationError):
                self.partition(cycle(values))

    def test_same_vertex_with_conflicting_parameters_is_rejected(self):
        fins = cycle([0.0, 1.0, 2.0, 3.0])
        fins[0]["end_u"] = 1.25
        with self.assertRaisesRegex(NativeFinRelationError, "conflicting parameters"):
            self.partition(fins)

    def test_nontransitive_parameter_identity_cannot_bridge_two_nodes(self):
        first = 1.0
        middle = first + 100 * math.ulp(first)
        last = first + 200 * math.ulp(first)
        fins = cycle([0.0, first, last, 3.0])
        fins[1]["start_u"] = middle
        with self.assertRaisesRegex(NativeFinRelationError, "non-transitive"):
            self.partition(fins)

    def test_ulp_exponent_boundary_cannot_hide_cross_cluster_identity(self):
        before_one = math.nextafter(1.0, 0.0)
        first = 1.0 - 200 * math.ulp(before_one)
        fins = cycle([0.0, first, before_one, 1.0, 3.0])
        with self.assertRaisesRegex(NativeFinRelationError, "non-transitive"):
            self.partition(fins)

    def test_seam_identity_cannot_erase_a_noncollapsed_fin(self):
        for endpoint in (4.0, math.nextafter(4.0, 0.0)):
            fins = cycle([0.0, endpoint, 1.0, 3.0])
            with self.subTest(endpoint=endpoint), self.assertRaisesRegex(
                NativeFinRelationError, "changes a fin's nonzero span"
            ):
                self.partition(fins)

    def test_outside_domain_is_not_clamped_by_boundary_identity(self):
        for endpoint in (math.nextafter(0.0, -math.inf), math.nextafter(4.0, math.inf)):
            fins = cycle([endpoint, 1.0, 2.0, 3.0])
            with self.subTest(endpoint=endpoint), self.assertRaisesRegex(
                NativeFinRelationError, "outside its domain"
            ):
                self.partition(fins)

    def test_consistent_roundoff_observations_preserve_original_input(self):
        fins = cycle([0.0, 1.0, 2.0, 3.0])
        fins[1]["start_u"] = 1.0 + 100 * math.ulp(1.0)
        original = copy.deepcopy(fins)
        proof = self.partition(fins)
        self.assertEqual(proof["parameter_node_count"], 4)
        self.assertEqual(fins, original)

    def test_disconnected_cycles_and_branches_are_rejected(self):
        fins = cycle([0.0, 1.0, 2.0])
        for index, row in cycle([0.0, 1.0, 2.0]).items():
            fins[index + 3] = {**row, "vertex_ids": [vertex + 3 for vertex in row["vertex_ids"]]}
        with self.assertRaisesRegex(NativeFinRelationError, "disconnected cycles"):
            self.partition(fins)
        fins = cycle([0.0, 1.0, 2.0, 3.0])
        fins[4] = {"vertex_ids": [0, 2], "start_u": 0.0, "end_u": 2.0}
        with self.assertRaisesRegex(NativeFinRelationError, "unambiguous cycle"):
            self.partition(fins)


if __name__ == "__main__":
    unittest.main()
