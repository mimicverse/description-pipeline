"""Doctor separates installed, reachable and collectable state."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from . import _paths  # noqa: F401  (import side effect: sys.path)

from description_pipeline.sources.solidworks import doctor  # noqa: E402
from description_pipeline.sources.solidworks.errors import EnvironmentError_  # noqa: E402

from . import support  # noqa: E402


class DoctorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="sw-doctor-"))

    def tearDown(self) -> None:
        support.cleanup(self.tmp)

    def test_without_backend_collection_is_not_run(self) -> None:
        report = doctor.diagnose()
        checks = {check["name"]: check for check in report["checks"]}
        self.assertEqual(checks["host"]["status"], "passed")
        self.assertTrue(checks["dependencies"]["python_supported"])
        self.assertIn("missing", checks["dependencies"])
        self.assertIn("required", checks["dependencies"])
        self.assertTrue(report["worker_alive"])
        self.assertFalse(report["solidworks_reachable"])
        self.assertFalse(report["cad_collectable"])
        checks = {check["name"]: check for check in report["checks"]}
        self.assertNotIn("solidworks", checks)

    def test_missing_solidworks_is_unavailable_not_failed(self) -> None:
        class NoSolidWorks:
            def health(self):
                raise EnvironmentError_("no_active_instance", "no SolidWorks process")

        report = doctor.diagnose(NoSolidWorks(), assembly=str(self.tmp / "robot.SLDASM"))
        checks = {check["name"]: check for check in report["checks"]}
        self.assertEqual(checks["solidworks"]["status"], "unavailable")
        self.assertEqual(checks["solidworks"]["reason"], "no_active_instance")
        self.assertEqual(checks["collection"]["status"], "not_run")
        self.assertFalse(report["cad_collectable"])

    def test_unwrapped_com_failure_is_a_structured_diagnostic(self) -> None:
        class BrokenApi:
            def health(self):
                raise OSError("RPC server unavailable")

        report = doctor.diagnose(BrokenApi(), assembly="robot.SLDASM")
        self.assertFalse(report["cad_collectable"])
        self.assertEqual(report["checks"][-2]["reason"], "cad_api_error")

    def test_fixture_backend_is_collectable(self) -> None:
        assembly = support.make_cad_tree(self.tmp / "cad")
        parts = [self.tmp / "cad" / "base.SLDPRT", self.tmp / "cad" / "arm.SLDPRT"]
        backend = support.FixtureCadBackend(
            assembly,
            [{"name": "base-1", "transform": support.placement(), "mass": support.mass_payload(1.0)}],
            dependencies=parts,
        )
        report = doctor.diagnose(backend, assembly=str(assembly), configuration="Default")
        checks = {check["name"]: check for check in report["checks"]}
        self.assertEqual(checks["collection"]["status"], "passed")
        self.assertEqual(checks["collection"]["components"], 1)
        self.assertTrue(report["cad_collectable"])
        self.assertEqual(report["advisories"], [])

        wrong = doctor.diagnose(backend, assembly=str(assembly), configuration="missing-configuration")
        self.assertFalse(wrong["cad_collectable"])
        self.assertEqual(wrong["checks"][-1]["reason"], "cad_configuration_not_active")

    def test_a_save_flag_is_an_advisory_not_a_refusal(self) -> None:
        # SolidWorks sets `GetSaveFlag` for many operations and for documents created by
        # an older release, so it cannot decide collectability.  The operator still gets
        # told, because their own unsaved edit is the one case where it may matter.
        assembly = support.make_cad_tree(self.tmp / "cad")
        parts = [self.tmp / "cad" / "base.SLDPRT", self.tmp / "cad" / "arm.SLDPRT"]
        backend = support.FixtureCadBackend(
            assembly,
            [{"name": "base-1", "transform": support.placement(), "mass": support.mass_payload(1.0)}],
            dependencies=parts,
        )
        backend.unsaved_paths = {str(assembly)}

        report = doctor.diagnose(backend, assembly=str(assembly), configuration="Default")

        self.assertTrue(report["cad_collectable"])
        self.assertEqual(report["checks"][-1]["status"], "passed")
        self.assertEqual([note["code"] for note in report["advisories"]], ["cad_save_flag_set"])
        self.assertEqual(report["advisories"][0]["documents"], [str(assembly)])
        self.assertEqual(report["advisories"][0]["count"], 1)


if __name__ == "__main__":
    unittest.main()
