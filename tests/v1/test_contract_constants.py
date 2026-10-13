"""Contract-convergence guards: one owner per shared list or constant (VER-02/03/OC-02)."""

import unittest

from description_pipeline.orchestration import report_view
from description_pipeline.stages import FIXED_REQUIRED_CHECKS, HOST_BY_STAGE, PORTABLE_STAGES, required_rows_complete
from description_pipeline.verification import solidworks_urdf


class ContractConvergenceTests(unittest.TestCase):
    def test_portal_labels_cover_the_fixed_required_checks(self) -> None:
        self.assertEqual(report_view._UNLABELLED_REQUIRED_CHECKS, ())
        self.assertLessEqual(FIXED_REQUIRED_CHECKS, set(report_view._INDEPENDENT_LABELS_ZH))

    def test_required_check_inventory_is_single_sourced(self) -> None:
        self.assertEqual(set(solidworks_urdf.required_checks()), set(FIXED_REQUIRED_CHECKS))
        definition = {
            "checks": {"expected_mass_kg": [0.5, 1.0], "expected_extent_m": [0.1, 1.0]},
            "source": {"bodies": [{"name": "base_link"}], "joints": [{"name": "waist_pitch", "type": "revolute"}]},
        }
        identifiers = set(solidworks_urdf.required_checks(definition))
        self.assertLessEqual(FIXED_REQUIRED_CHECKS, identifiers)
        self.assertIn("physics.expected_mass", identifiers)
        self.assertIn("inertia.base_link", identifiers)
        self.assertIn("shafts.waist_pitch", identifiers)

    def test_portable_stages_derive_from_the_contract_hosts(self) -> None:
        self.assertEqual(PORTABLE_STAGES, ("generate", "verify", "publish"))
        self.assertEqual(tuple(stage for stage, host in HOST_BY_STAGE.items() if host == "portable"), PORTABLE_STAGES)

    def test_completeness_helper_matches_the_nested_gate_contract(self) -> None:
        passed = {"id": "a", "state": "passed", "passed": True}
        self.assertTrue(required_rows_complete(["a"], [dict(passed)]))
        for required, rows in (
            ([], []),
            (["a"], []),
            (["a"], [{"id": "a", "state": "failed", "passed": False}]),
            (["a"], [dict(passed, id="b")]),
            (["a"], [dict(passed), dict(passed)]),
            ([1], [dict(passed, id=1)]),
        ):
            with self.subTest(required=required, rows=rows):
                self.assertFalse(required_rows_complete(required, rows))
