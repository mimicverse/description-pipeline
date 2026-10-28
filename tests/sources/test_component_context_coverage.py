"""Component-context equivalence tolerates divergences explained by effective cover."""

import unittest

from . import _paths  # noqa: F401  (import side effect: sys.path)

from description_pipeline.sources.solidworks import verify as oracle

FLAGS_OFF = {"OverrideMass": False, "OverrideCenterOfMass": False, "OverrideMomentsOfInertia": False}
FLAGS_ON = {"OverrideMass": True, "OverrideCenterOfMass": False, "OverrideMomentsOfInertia": False}


def _payload(rows, assembly=0.1):
    return {
        "component_context": {
            "status": "recorded",
            "assembly_mass_kg": assembly,
            "errors": [],
            "instances": rows,
        }
    }


def _row(name, *, depth, parent, document_type, mass, overrides):
    return {
        "name": name,
        "depth": depth,
        "parent": parent,
        "document_type": document_type,
        "context_mass_kg": mass,
        "overrides": dict(overrides),
    }


class ComponentContextCoverageTests(unittest.TestCase):
    def _fixture(self):
        rows = [
            _row("board-1", depth=0, parent=None, document_type="assembly", mass=0.1, overrides=FLAGS_ON),
            _row("board-1/dev", depth=1, parent="board-1", document_type="part", mass=0.06, overrides=FLAGS_OFF),
            _row("head", depth=0, parent=None, document_type="assembly", mass=0.04, overrides=FLAGS_OFF),
            _row("head/leaf", depth=1, parent="head", document_type="part", mass=0.004, overrides=FLAGS_OFF),
        ]
        scene = {"board-1/dev": 0.03, "head/leaf": 0.004}
        return _payload(rows), scene

    def test_divergence_explained_by_effective_cover_is_not_an_error(self):
        payload, scene = self._fixture()
        details, error = oracle._component_context_findings(
            payload, 0.1, scene, covered={"board-1", "head/leaf"}
        )
        self.assertIsNone(error)
        self.assertEqual(details["component_context"]["effective_vs_document_mismatches"], 3)

    def test_unexplained_divergence_still_errors(self):
        payload, scene = self._fixture()
        # board-1's own override is covered, so the surviving mismatch must be the
        # head subtree that no covered instance explains.
        _, error = oracle._component_context_findings(payload, 0.1, scene, covered={"board-1"})
        self.assertIsNotNone(error)
        self.assertIn("head", error)

        payload, scene = self._fixture()
        payload["component_context"]["instances"].append(
            _row("other/x", depth=0, parent=None, document_type="part", mass=0.09, overrides=FLAGS_OFF)
        )
        scene["other/x"] = 0.01
        _, error = oracle._component_context_findings(
            payload, 0.1, scene, covered={"board-1", "head/leaf"}
        )
        self.assertIsNotNone(error)
        self.assertIn("other/x", error)

    def test_numeric_helpers_refuse_boolean_and_numeric_strings(self):
        for value in (True, False, "1", "0"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                oracle._finite_positive(value, "positive")
            with self.subTest(value=value), self.assertRaises(ValueError):
                oracle._finite_non_negative(value, "non-negative")
        for value in ([True, False, True], ["1", "2", "3"]):
            with self.subTest(value=value), self.assertRaises(ValueError):
                oracle._finite_vector(value, "vector")

    def test_component_context_refuses_boolean_mass(self):
        payload, scene = self._fixture()
        payload["component_context"]["instances"][0]["context_mass_kg"] = True
        _details, error = oracle._component_context_findings(payload, 0.1, scene)
        self.assertIsNotNone(error)
        self.assertIn("component mass context mass", error)


if __name__ == "__main__":
    unittest.main()
