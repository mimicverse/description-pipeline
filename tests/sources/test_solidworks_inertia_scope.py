"""Inertia sign conventions are qualified by the measurement scope.

Analytic fixture (unit test): two point masses of 0.5 kg at (+1, +1, 0) m and
(-1, -1, 0) m.

    Ixx = Σ m (y² + z²) = 1.0        Iyy = Σ m (x² + z²) = 1.0
    Izz = Σ m (x² + y²) = 2.0        ∫xy dm = 0.5·1·1 + 0.5·(−1)(−1) = 1.0

The standard inertia tensor has ``Ixy = −∫xy dm``::

    [[1, -1, 0], [-1, 1, 0], [0, 0, 2]]

A part document reports positive-product notation for the same body::

    [[1, +1, 0], [+1, 1, 0], [0, 0, 2]]

A native analytic fixture (rotated boxes, SolidWorks 34.0.0, 2026-10-06) showed
that both the part-document reading and the component-group reading are the
standard tensor as-is (residuals 4e-20 and 6.5e-19).  The scope label therefore
names the measured convention, and it must agree with any explicit convention -
historical readings that declare ``solidworks_positive`` keep their own
interpretation.
"""

from __future__ import annotations

import unittest

import numpy as np

from . import _paths  # noqa: F401  (import side effect: sys.path)

from description_pipeline.sources.solidworks import verify as verify_module  # noqa: E402
from description_pipeline.sources.solidworks.errors import ConfigError  # noqa: E402
from description_pipeline.sources.solidworks.scene import tensor_from_raw  # noqa: E402

POSITIVE_PRODUCT = [[1.0, 1.0, 0.0], [1.0, 1.0, 0.0], [0.0, 0.0, 2.0]]
STANDARD = [[1.0, -1.0, 0.0], [-1.0, 1.0, 0.0], [0.0, 0.0, 2.0]]


class GeneratorScopeTests(unittest.TestCase):
    def test_part_document_scope_keeps_the_measured_standard_tensor(self):
        tensor = tensor_from_raw(
            STANDARD,
            {"scope": "part_document", "product_convention": "solidworks_standard"},
            where="analytic fixture",
        )
        self.assertEqual(tensor, tuple(tuple(row) for row in STANDARD))

    def test_part_document_scope_supplies_the_convention(self):
        tensor = tensor_from_raw(STANDARD, {"scope": "part_document"}, where="analytic fixture")
        self.assertEqual(tensor, tuple(tuple(row) for row in STANDARD))

    def test_group_scope_keeps_the_measured_standard_tensor(self):
        tensor = tensor_from_raw(
            STANDARD,
            {"scope": "assembly_component_group", "product_convention": "solidworks_standard"},
            where="analytic fixture",
        )
        self.assertEqual(tensor, tuple(tuple(row) for row in STANDARD))

    def test_explicit_positive_convention_is_still_honoured(self):
        tensor = tensor_from_raw(POSITIVE_PRODUCT, {"product_convention": "solidworks_positive"}, where="analytic fixture")
        self.assertEqual(tensor, tuple(tuple(row) for row in STANDARD))

    def test_scope_and_convention_must_agree(self):
        for raw, reference in (
            (STANDARD, {"scope": "part_document", "product_convention": "solidworks_positive"}),
            (STANDARD, {"scope": "assembly_component_group", "product_convention": "solidworks_positive"}),
        ):
            with self.subTest(reference=reference):
                with self.assertRaises(ConfigError):
                    tensor_from_raw(raw, reference, where="analytic fixture")

    def test_unknown_scope_is_rejected(self):
        with self.assertRaises(ConfigError):
            tensor_from_raw(
                POSITIVE_PRODUCT,
                {"scope": "assembly_document", "product_convention": "solidworks_positive"},
                where="analytic fixture",
            )

    def test_unqualified_readings_still_fail_closed(self):
        tensor = tensor_from_raw(STANDARD, {"used_api": "fixture"}, where="analytic fixture")
        self.assertEqual(tensor, tuple(tuple(row) for row in STANDARD))
        with self.assertRaises(ConfigError):
            tensor_from_raw(STANDARD, {"used_api": "IMassProperty2.GetMomentOfInertia(0)"}, where="analytic fixture")

    def test_unvalidated_fallback_arrays_are_never_relabelled(self):
        # The analytic proof covers GetMomentOfInertia(0) only.  A legacy
        # GetMassProperties2 array declares no convention and stays unusable.
        fallback = {
            "used_api": "IModelDocExtension.GetMassProperties2(1, status, False)",
            "com": "not_inferred",
            "inertia": "not_inferred",
            "product_convention": None,
        }
        with self.assertRaises(ConfigError):
            tensor_from_raw(STANDARD, fallback, where="legacy fallback")


class VerifyOracleScopeTests(unittest.TestCase):
    def _payload(self, raw, reference):
        return {"mass": 1.0, "com": [0.0, 0.0, 0.0], "inertia": raw, "reference": reference}

    def test_oracle_matches_the_generator_for_both_scopes(self):
        part = verify_module._raw_tensor(
            "box", self._payload(STANDARD, {"scope": "part_document"})
        )
        group = verify_module._raw_tensor(
            "group", self._payload(STANDARD, {"scope": "assembly_component_group"})
        )
        self.assertTrue(np.allclose(part, np.array(STANDARD)))
        self.assertTrue(np.allclose(group, np.array(STANDARD)))
        positive = verify_module._raw_tensor(
            "legacy", self._payload(POSITIVE_PRODUCT, {"product_convention": "solidworks_positive"})
        )
        self.assertTrue(np.allclose(positive, np.array(STANDARD)))

    def test_oracle_rejects_disagreeing_or_unknown_scopes(self):
        with self.assertRaises(ValueError):
            verify_module._raw_tensor(
                "part",
                self._payload(
                    STANDARD,
                    {"scope": "part_document", "product_convention": "solidworks_positive"},
                ),
            )
        with self.assertRaises(ValueError):
            verify_module._raw_tensor(
                "part",
                self._payload(
                    POSITIVE_PRODUCT,
                    {"scope": "assembly_document", "product_convention": "solidworks_positive"},
                ),
            )


if __name__ == "__main__":
    unittest.main()
