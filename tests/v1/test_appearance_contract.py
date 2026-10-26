"""Shared pure numeric rules for native appearance colors."""

from __future__ import annotations

import math
import unittest
from itertools import permutations

from description_pipeline.appearance_contract import (
    RGBA_ATOL,
    scoped_rgba,
)


class AppearanceContractTests(unittest.TestCase):
    def test_owner_and_colorref_neighbors_choose_an_observed_rgba(self):
        channel = 57 / 255.0
        owner = [
            math.nextafter(channel, -math.inf),
            57 / 255.0,
            math.nextafter(57 / 255.0, -math.inf),
            1.0,
        ]
        colorref = [57 / 255.0, 57 / 255.0, 57 / 255.0, 1.0]

        self.assertEqual(
            scoped_rgba((owner, colorref)),
            tuple(owner),
        )
        self.assertEqual(scoped_rgba((owner, colorref)), scoped_rgba((colorref, owner)))

    def test_absolute_tolerance_boundary_is_inclusive(self):
        lower = [0.0, 0.0, 0.0, 1.0]
        at_bound = [RGBA_ATOL, 0.0, 0.0, 1.0]
        beyond = [math.nextafter(RGBA_ATOL, math.inf), 0.0, 0.0, 1.0]

        self.assertEqual(scoped_rgba((lower, at_bound)), (0.0, 0.0, 0.0, 1.0))
        self.assertIsNone(scoped_rgba((lower, beyond)))

    def test_every_colorref_channel_and_adjacent_float_choose_one_observed_value(
        self,
    ):
        for channel in range(256):
            exact = channel / 255.0
            neighbors = (
                *(
                    (math.nextafter(exact, -math.inf),)
                    if channel > 0
                    else ()
                ),
                exact,
                *(
                    (math.nextafter(exact, math.inf),)
                    if channel < 255
                    else ()
                ),
            )
            observed = tuple((value, value, value, 1.0) for value in neighbors)
            with self.subTest(channel=channel):
                self.assertEqual(scoped_rgba(observed), min(observed))
                self.assertEqual(
                    scoped_rgba(observed),
                    scoped_rgba(tuple(reversed(observed))),
                )

    def test_scoped_rgba_rejects_invalid_values_and_cross_tolerance_chains(self):
        anchor = 0.25
        spanning = (
            (anchor, 0.0, 0.0, 1.0),
            (anchor + 0.75 * RGBA_ATOL, 0.0, 0.0, 1.0),
            (anchor - 0.75 * RGBA_ATOL, 0.0, 0.0, 1.0),
        )
        for ordered in permutations(spanning):
            with self.subTest(ordered=ordered):
                self.assertIsNone(scoped_rgba(ordered))
        self.assertIsNone(
            scoped_rgba(
                (
                    (0.0, 0.0, 0.0, 0.0),
                    (0.0, 0.0, 0.0, 2 * RGBA_ATOL),
                )
            )
        )
        for invalid in (
            (float("nan"), 0.0, 0.0, 1.0),
            (0.0, 0.0, 0.0, 1.0, 1.0),
            (True, 0.0, 0.0, 1.0),
            (-0.01, 0.0, 0.0, 1.0),
        ):
            with self.subTest(value=invalid), self.assertRaises(ValueError):
                scoped_rgba((invalid,))


if __name__ == "__main__":
    unittest.main()
