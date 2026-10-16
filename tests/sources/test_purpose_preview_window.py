"""Inspection-preview purpose: the policy window, the bound prismatic declaration and missing-means-missing."""

from __future__ import annotations

import copy
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from . import _paths  # noqa: F401  (import side effect: sys.path)

from description_pipeline.interface_records import (  # noqa: E402
    PURPOSE_ENGINEERING,
    PURPOSE_INSPECTION_PREVIEW,
    preview_revolute_window_rad,
)
from description_pipeline.sources.solidworks.discovery import (  # noqa: E402
    DiscoverySettings,
    _declared_preview_range,
    prepare_native_package,
)
from description_pipeline.verification.native_discovery import _Failure, rebuild_joint_facts  # noqa: E402

from .test_solidworks_discovery import FakeBackend, record as native_record  # noqa: E402


def _strip_joint_specs(payload: dict) -> dict:
    """Remove the hinge's engineering range/drive claims so only the purpose can supply them."""

    stripped = copy.deepcopy(payload)
    mates = (stripped.get("properties") or {}).get("mates") or {}
    for spec in mates.values():
        spec.pop("dp.joint.limits_record", None)
        spec.pop("dp.joint.drive_record", None)
    return stripped


class PurposePreviewWindowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="purpose-preview-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _prepared(self, payload: dict, purpose: str):
        frozen = self.tmp / f"native-{purpose}"
        for name in ("cad/robot.SLDASM", "cad/base.SLDPRT", "cad/arm.SLDPRT"):
            path = frozen / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(f"placeholder {name}\n".encode())
        records = self.tmp / f"records-{purpose}"
        records.mkdir(exist_ok=True)
        (records / "budget.json").write_text(
            json.dumps({"robot": {"expected_mass_kg": [0.28, 0.32], "expected_extent_m": [0.35, 0.5]}}),
            encoding="utf-8",
        )
        output = self.tmp / f"prepared-{purpose}"
        prepared = prepare_native_package(
            frozen,
            output,
            f"run-{purpose}",
            backend=FakeBackend(payload),
            settings=DiscoverySettings(record_roots=(records,), purpose=purpose),
        )
        return prepared, output

    def test_missing_revolute_range_uses_the_policy_window_and_missing_drive_stays_absent(self) -> None:
        payload = _strip_joint_specs(native_record())
        prepared, output = self._prepared(payload, PURPOSE_INSPECTION_PREVIEW)
        self.assertTrue(prepared.passed, prepared.findings)
        discovery_payload = json.loads((output / "discovery/native-discovery.json").read_text(encoding="utf-8"))
        joints = rebuild_joint_facts(discovery_payload, output, purpose=PURPOSE_INSPECTION_PREVIEW)
        joint = joints[0]
        radius = preview_revolute_window_rad()
        self.assertEqual(joint["limits"], {"lower": -radius, "upper": radius})
        self.assertNotIn("effort", joint["limits"])
        self.assertNotIn("velocity", joint["limits"])
        self.assertEqual(joint["limit_source"], "policy_preview_window")

    def test_engineering_purpose_still_refuses_the_missing_range(self) -> None:
        payload = _strip_joint_specs(native_record())
        prepared, _output = self._prepared(payload, PURPOSE_ENGINEERING)
        self.assertFalse(prepared.passed)
        blocking = {finding["code"] for finding in prepared.findings if finding.get("blocking")}
        self.assertIn("discovery.joint_limits_missing", blocking)
        # The preview-free copy is refused by the independent rebuild under engineering.
        preview_prepared, preview_output = self._prepared(payload, PURPOSE_INSPECTION_PREVIEW)
        self.assertTrue(preview_prepared.passed, preview_prepared.findings)
        discovery_payload = json.loads((preview_output / "discovery/native-discovery.json").read_text(encoding="utf-8"))
        with self.assertRaises(_Failure):
            rebuild_joint_facts(discovery_payload, preview_output, purpose=PURPOSE_ENGINEERING)


class DeclaredPreviewRangeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="declared-preview-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _value(self, **overrides):
        value = {
            "schema_version": "solidworks-to-urdf.preview-range/v1",
            "joint": "slide_joint",
            "instance": "arm-1",
            "configuration": "Default",
            "unit": "m",
            "zero": 0.0,
            "direction": "+1",
            "purpose": PURPOSE_INSPECTION_PREVIEW,
            "lower": -0.1,
            "upper": 0.2,
            "source_sha256": "c" * 64,
        }
        value.update(overrides)
        return value

    def _resolve(self, value, *, reference: str = "preview.json#range"):
        (self.tmp / "preview.json").write_text(json.dumps({"range": value}), encoding="utf-8")
        findings: list[dict] = []
        settings = DiscoverySettings(record_roots=(self.tmp,), purpose=PURPOSE_INSPECTION_PREVIEW)
        record = {"properties": {"dp.joint.preview_range_record": reference}, "saved_configuration": "Default"}
        limits, evidence = _declared_preview_range(
            record, settings, findings, "joint:slide_joint", joint="slide_joint", instances=("base-1", "arm-1")
        )
        return limits, evidence, findings

    def test_bound_declaration_supplies_the_prismatic_range(self) -> None:
        limits, evidence, findings = self._resolve(self._value())
        self.assertEqual(limits, {"lower": -0.1, "upper": 0.2})
        self.assertEqual(findings, [])
        self.assertRegex(str(evidence["sha256"]), r"^[0-9a-f]{64}$")

    def test_misshaped_declarations_are_refused(self) -> None:
        cases = (
            {"unit": "rad"},
            {"purpose": PURPOSE_ENGINEERING},
            {"instance": "spare-9"},
            {"configuration": "Other"},
            {"direction": "both"},
            {"lower": 0.5, "upper": 0.5},
            {"source_sha256": "xyz"},
            {"extra": 1},
        )
        for overrides in cases:
            with self.subTest(overrides=overrides):
                limits, _evidence, findings = self._resolve(self._value(**overrides))
                self.assertIsNone(limits)
                self.assertTrue(
                    any(finding["code"] == "discovery.joint_preview_range_invalid" for finding in findings),
                    findings,
                )
        limits, _evidence, findings = self._resolve(["not-a-mapping"])
        self.assertIsNone(limits)
        self.assertTrue(findings)
        self.assertTrue(
            all(
                finding["code"] in {"discovery.record_shape", "discovery.joint_preview_range_invalid"}
                for finding in findings
            ),
            findings,
        )
        # A file that merely contains the wording (wrong key shape) never passes.
        (self.tmp / "preview.json").write_text(json.dumps({"note": "range -0.1..0.2"}), encoding="utf-8")
        findings = []
        settings = DiscoverySettings(record_roots=(self.tmp,), purpose=PURPOSE_INSPECTION_PREVIEW)
        record = {
            "properties": {"dp.joint.preview_range_record": "preview.json#note"},
            "saved_configuration": "Default",
        }
        limits, _evidence = _declared_preview_range(
            record, settings, findings, "joint:slide_joint", joint="slide_joint", instances=("base-1", "arm-1")
        )
        self.assertIsNone(limits)
        self.assertTrue(findings)
        self.assertTrue(
            all(
                finding["code"] in {"discovery.record_shape", "discovery.joint_preview_range_invalid"}
                for finding in findings
            ),
            findings,
        )

    def test_missing_declaration_reference_is_a_missing_finding(self) -> None:
        findings: list[dict] = []
        settings = DiscoverySettings(record_roots=(self.tmp,), purpose=PURPOSE_INSPECTION_PREVIEW)
        limits, evidence = _declared_preview_range(
            {"properties": {}, "saved_configuration": "Default"},
            settings,
            findings,
            "joint:slide_joint",
            joint="slide_joint",
            instances=("base-1", "arm-1"),
        )
        self.assertIsNone(limits)
        self.assertIsNone(evidence)
        self.assertEqual([finding["code"] for finding in findings], ["discovery.joint_preview_range_missing"])


if __name__ == "__main__":
    unittest.main()
