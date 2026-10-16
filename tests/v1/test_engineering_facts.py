"""The engineering qualifications stay separate facts: evidence, not check colours."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from description_pipeline import request_evidence
from description_pipeline.io import write_json
from description_pipeline.verification import solidworks_urdf as quality


class EngineeringFactTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root / "input").mkdir()
        (self.root / "model").mkdir()
        (self.root / "urdf").mkdir()
        write_json(self.root / "input/handoff.json", {"probe": "sealed input"})
        write_json(self.root / "model/robot.json", {"probe": "model"})
        (self.root / "urdf/robot.urdf").write_text("<robot/>", encoding="utf-8")

    @staticmethod
    def _definition(*, limit_source: str | None = "native", drive: bool = True, fixed: bool = False) -> dict:
        limits: dict = {"lower": -1.0, "upper": 1.0}
        if drive:
            limits.update({"effort": 1.0, "velocity": 1.0})
        joint: dict = {"name": "hinge", "type": "fixed" if fixed else "revolute", "limits": limits}
        if limit_source is not None:
            joint["limit_source"] = limit_source
        return {"checks": {}, "source": {"bodies": [], "joints": [joint], "frames": []}}

    @staticmethod
    def _checks(definition) -> list:
        return [{"id": name, "state": "passed", "passed": True} for name in quality.required_checks(definition)]

    def _items(self, definition) -> list:
        return quality.qualifications(self.root, definition, self._checks(definition))

    def _statuses(self, definition) -> dict:
        return {(item["fact_id"], item["object_id"]): item["status"] for item in self._items(definition)}

    def _write_request(self, purpose: str) -> None:
        payload = request_evidence.request_evidence_bytes(
            request_evidence.build_request_evidence(
                purpose=purpose,
                request_id="fixture-engineering-facts",
                target_hardware="nd_fixture",
                change_summary="fixture",
                main_assembly="cad/robot.SLDASM",
                handoff_digest="a" * 64,
                platform_config_digest="b" * 64,
            )
        )
        (self.root / "input" / "request.json").write_bytes(payload)

    def test_engineering_purpose_establishes_only_on_authoritative_evidence(self) -> None:
        statuses = self._statuses(self._definition())
        self.assertEqual(statuses[("engineering_position_range", "hinge")], "established")
        self.assertEqual(statuses[("drive_sizing", "hinge")], "established")
        self.assertEqual(statuses[("machine_qualification", "")], "unqualified")

        statuses = self._statuses(self._definition(limit_source=None, drive=False))
        self.assertEqual(statuses[("engineering_position_range", "hinge")], "unqualified")
        self.assertEqual(statuses[("drive_sizing", "hinge")], "unqualified")
        statuses = self._statuses(self._definition(limit_source="policy_preview_window"))
        self.assertEqual(statuses[("engineering_position_range", "hinge")], "unqualified")

    def test_preview_states_all_three_unqualified_and_never_blocks(self) -> None:
        self._write_request("inspection_preview")
        definition = self._definition()
        statuses = self._statuses(definition)
        self.assertEqual(statuses[("engineering_position_range", "hinge")], "unqualified")
        self.assertEqual(statuses[("drive_sizing", "hinge")], "unqualified")
        self.assertEqual(statuses[("machine_qualification", "")], "unqualified")
        checks = self._checks(definition)
        report = {
            "passed": True,
            "required_checks": quality.required_checks(definition),
            "checks": checks,
            "qualifications": self._items(definition),
        }
        qualified = quality.require_qualified_report(report, definition, root=self.root)
        self.assertTrue(qualified["passed"])

    def test_evidence_binds_the_request_and_the_delivered_model(self) -> None:
        self._write_request("inspection_preview")
        items = self._items(self._definition())
        fact = next(item for item in items if item["fact_id"] == "engineering_position_range")
        records = {reference["record"] for reference in fact["evidence"]}
        self.assertIn("input/handoff.json", records)
        self.assertIn("input/request.json", records)
        self.assertIn("model/robot.json", records)
        from description_pipeline.interface_records import validate_qualification

        validate_qualification(fact, resolve=self.root)

    def test_fixed_joints_carry_no_engineering_facts(self) -> None:
        statuses = self._statuses(self._definition(fixed=True))
        self.assertNotIn(("engineering_position_range", "hinge"), statuses)
        self.assertNotIn(("drive_sizing", "hinge"), statuses)


if __name__ == "__main__":
    unittest.main()
