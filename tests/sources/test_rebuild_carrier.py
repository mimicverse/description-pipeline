"""Differential rebuild: the verifier's raw-only carrier equals the saved carrier.

The producer corpus keeps the prepared records this harness needs; the rebuild
itself is verifier-owned and never imports the generation modules.
"""

from __future__ import annotations

import copy
import json
import shutil
import tempfile
import unittest
from pathlib import Path

import yaml

from description_pipeline.sources.solidworks.discovery import DiscoverySettings, prepare_native_package
from description_pipeline.verification.native_discovery import (
    _Failure,
    rebuild_interface_frames,
    rebuild_joint_facts,
    rebuild_rigid_bodies,
)

from . import test_solidworks_discovery_oracle as oracle_fixtures


class RebuildCarrierTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="rebuild-carrier-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.serial = 0

    def _prepared(self, record: dict) -> tuple[dict, dict, Path]:
        self.serial += 1
        source = self.tmp / f"native{self.serial}"
        for name in ("cad/robot.SLDASM", "cad/base.SLDPRT", "cad/arm.SLDPRT"):
            path = source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(f"placeholder {name}\n".encode())
        records = self.tmp / f"records{self.serial}"
        (records / "joints").mkdir(parents=True, exist_ok=True)
        (records / "joints" / "arm.json").write_text(
            json.dumps({"limits": {"lower": -1.5, "upper": 1.5}, "drive": {"effort": 6.0, "velocity": 2.0}}),
            encoding="utf-8",
        )
        (records / "budget.json").write_text(
            json.dumps({"robot": {"expected_mass_kg": [0.28, 0.32], "expected_extent_m": [0.35, 0.5]}}),
            encoding="utf-8",
        )
        output = self.tmp / f"prepared{self.serial}"
        result = prepare_native_package(
            source,
            output,
            run_id="rebuild-run",
            backend=oracle_fixtures._ReplayBackend(record),
            settings=DiscoverySettings(record_roots=(records,)),
        )
        self.assertTrue(result.passed, result.findings)
        payload = json.loads((output / "discovery/native-discovery.json").read_text(encoding="utf-8"))
        robot = yaml.safe_load((output / "robot.yaml").read_text(encoding="utf-8"))
        return payload, robot["source"], output

    @staticmethod
    def _normalized(bodies: list[dict]) -> list[dict]:
        return sorted(
            (
                {
                    "id": body.get("id", body["name"]),
                    "name": body["name"],
                    "components": sorted(body["components"]),
                    "frame": body["frame"],
                }
                for body in bodies
            ),
            key=lambda body: body["name"],
        )

    def test_oracle_fixture_rebuild_matches_the_saved_carrier(self) -> None:
        payload, source, _output = self._prepared(oracle_fixtures.native_record())
        self.assertEqual(self._normalized(source["bodies"]), self._normalized(rebuild_rigid_bodies(payload["raw"])))

    def test_lock_fixture_rebuild_matches_the_saved_carrier(self) -> None:
        payload, source, _output = self._prepared(oracle_fixtures.OracleSemanticsTests._lock_record(with_point=True))
        self.assertEqual(self._normalized(source["bodies"]), self._normalized(rebuild_rigid_bodies(payload["raw"])))

    def test_joint_rebuild_matches_the_saved_carrier(self) -> None:
        cases = (
            ("oracle", oracle_fixtures.native_record()),
            ("lock", oracle_fixtures.OracleSemanticsTests._lock_record(with_point=True)),
        )
        for label, record in cases:
            with self.subTest(case=label):
                payload, source, output = self._prepared(record)
                rebuilt = rebuild_joint_facts(payload, output)
                expected = sorted(source["joints"], key=lambda joint: joint["name"])
                actual = sorted(rebuilt, key=lambda joint: joint["name"])
                self.assertEqual(expected, actual)

    @staticmethod
    def _interface_record() -> dict:
        record = copy.deepcopy(oracle_fixtures.native_record())
        twin = copy.deepcopy(record["datums"][1])
        twin["name"] = "TCP_tool_mount"
        twin["owner"] = "arm-1"
        record["datums"].append(twin)
        return record

    def test_interface_frame_rebuild_matches_the_saved_carrier(self) -> None:
        payload, source, _output = self._prepared(self._interface_record())
        self.assertEqual(
            source["frames"],
            [
                {
                    "id": "tool_mount",
                    "name": "tool_mount",
                    "parent": "arm_link",
                    "coordinate_system": "TCP_tool_mount",
                }
            ],
        )
        self.assertEqual(source["frames"], rebuild_interface_frames(payload))
        for record in (
            oracle_fixtures.native_record(),
            oracle_fixtures.OracleSemanticsTests._lock_record(with_point=True),
        ):
            payload, source, _output = self._prepared(record)
            self.assertEqual([], source["frames"])
            self.assertEqual([], rebuild_interface_frames(payload))

    def test_interface_frame_rebuild_fails_closed(self) -> None:
        base = oracle_fixtures.native_record()
        twin = base["datums"][1]

        def with_datum(name: str, owner: str) -> dict:
            record = copy.deepcopy(base)
            datum = copy.deepcopy(twin)
            datum["name"], datum["owner"] = name, owner
            record["datums"].append(datum)
            return record

        duplicate = with_datum("TCP_tool_mount", "arm-1")
        duplicate["datums"].append(copy.deepcopy(duplicate["datums"][-1]))
        cases = (
            ("duplicate frame name", duplicate),
            ("non snake suffix", with_datum("TCP_Tool-Mount", "arm-1")),
            ("unowned datum", with_datum("TCP_orphan", "ghost-1")),
            ("body name collision", with_datum("TCP_base_link", "arm-1")),
            ("ownerless without a base", with_datum("TCP_top_mount", "")),
        )
        for label, record in cases:
            with self.subTest(case=label), self.assertRaises(_Failure):
                rebuild_interface_frames(record)

    def test_missing_or_ambiguous_frame_datum_fails_closed(self) -> None:
        payload, _source, _output = self._prepared(oracle_fixtures.native_record())
        raw = payload["raw"]
        missing = copy.deepcopy(raw)
        missing["datums"] = [datum for datum in missing["datums"] if datum.get("name") != "CS_arm_link"]
        with self.assertRaises(_Failure):
            rebuild_rigid_bodies(missing)
        duplicated = copy.deepcopy(raw)
        twin = next(datum for datum in duplicated["datums"] if datum.get("name") == "CS_arm_link")
        duplicated["datums"].append(copy.deepcopy(twin))
        with self.assertRaises(_Failure):
            rebuild_rigid_bodies(duplicated)


if __name__ == "__main__":
    unittest.main()
