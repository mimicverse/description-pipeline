"""Capture-time mass closure: the assembly's own reading next to the recombined leaf readings.

The record is evidence, and the verification side reports a mismatch as an advisory: a repaired or
re-materialed leaf (a leaf that no longer matches the tree the assembly was built from) becomes
visible without turning into a release gate.  Snapshots that predate the record stay not_applicable.

The native assembly reading itself is exercised on Windows by the release rehearsal; these tests
cover the combination, the recorded evidence, the advisory and the backward-compatible absence.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from . import _paths  # noqa: F401  (import side effect: sys.path)

from description_pipeline.build import report_advisories  # noqa: E402
from description_pipeline.sources.solidworks.freeze import freeze  # noqa: E402
from description_pipeline.sources.solidworks.verify import _mass_closure_check  # noqa: E402

from . import support  # noqa: E402

#: The factor a repaired leaf showed in the M3.0 servo investigation (assembly vs recovered leaf).
DENSITY_SCALE = 4.87415040334823


def combine(entries: list[dict]) -> dict:
    """Independent parallel-axis combination, written out here so the test does not reuse the tool."""

    total = sum(float(entry["mass"]) for entry in entries)
    com = [sum(float(entry["mass"]) * float(entry["com"][axis]) for entry in entries) / total for axis in range(3)]
    inertia = [[0.0] * 3 for _ in range(3)]
    for entry in entries:
        mass = float(entry["mass"])
        delta = [float(entry["com"][axis]) - com[axis] for axis in range(3)]
        squared = sum(value * value for value in delta)
        for i in range(3):
            for j in range(3):
                shift = mass * ((squared if i == j else 0.0) - delta[i] * delta[j])
                inertia[i][j] += float(entry["inertia"][i][j]) + shift
    return {"mass": total, "com": com, "inertia": inertia}


LEAF_TOTAL = combine(
    [
        {"mass": 1.0, "com": (0.0, 0.0, 0.0), "inertia": [[0.001, 0.0, 0.0], [0.0, 0.001, 0.0], [0.0, 0.0, 0.001]]},
        {"mass": 0.5, "com": (0.0, 0.0, 0.25), "inertia": [[0.002, 0.0, 0.0], [0.0, 0.001, 0.0], [0.0, 0.0, 0.001]]},
    ]
)


class ClosureBackend(support.FixtureCadBackend):
    """A fixture backend that also answers the whole-assembly reading."""

    def __init__(self, *arguments, assembly_reading: dict, **keywords) -> None:
        super().__init__(*arguments, **keywords)
        self.assembly_reading = assembly_reading

    def assembly_mass_properties(self, path: str) -> dict:
        return {**self.assembly_reading, "reference": {"used_api": "fixture"}}


class MassClosureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="sw-closure-"))
        self.assembly = support.make_cad_tree(self.tmp / "cad")
        self.components = [
            {"name": "base-1", "transform": support.placement(), "mass": support.mass_payload(1.0, (0.0, 0.0, 0.0))},
            {
                "name": "arm-1",
                "transform": support.placement((0.0, 0.0, 0.2), (0.0, 0.0, 0.0)),
                "mass": support.mass_payload(
                    0.5, (0.0, 0.0, 0.05), [[0.002, 0.0, 0.0], [0.0, 0.001, 0.0], [0.0, 0.0, 0.001]]
                ),
            },
        ]
        self.config = {
            "provider": "solidworks",
            "assembly": str(self.assembly),
            "configuration": "Default",
            "allowed_roots": [str(self.tmp / "cad")],
            "geometry": {"enabled": True},
            "bodies": [
                {"id": "base", "name": "base_link", "components": ["base-1"]},
                {"id": "arm", "name": "arm_link", "components": ["arm-1"]},
            ],
            "joints": [],
        }
        self.dependencies = [self.tmp / "cad" / "base.SLDPRT", self.tmp / "cad" / "arm.SLDPRT"]

    def tearDown(self) -> None:
        support.cleanup(self.tmp)

    def build(self, name: str, backend) -> Path:
        snapshot = self.tmp / name
        freeze(self.config, snapshot, backend=backend)
        return snapshot

    def record(self, snapshot: Path) -> dict:
        return json.loads((snapshot / "raw/mass_closure.json").read_text(encoding="utf-8"))

    def test_a_matching_assembly_records_an_exact_closure_and_no_advisory(self):
        backend = ClosureBackend(
            self.assembly, self.components, dependencies=self.dependencies, assembly_reading=LEAF_TOTAL
        )
        snapshot = self.build("matching", backend)
        record = self.record(snapshot)
        self.assertEqual(record["schema_version"], "description-pipeline.solidworks-mass-closure/v1")
        self.assertEqual(record["leaf_components"], 2)
        self.assertAlmostEqual(record["delta"]["mass_rel"], 0.0, places=12)
        self.assertAlmostEqual(record["delta"]["com_abs_max"], 0.0, places=12)
        self.assertAlmostEqual(record["delta"]["inertia_rel"], 0.0, places=12)
        check = _mass_closure_check(snapshot)
        self.assertEqual(check["status"], "passed")
        self.assertNotIn("advisory", check["details"])
        self.assertEqual(report_advisories([check]), [])

    def test_a_density_scale_discrepancy_becomes_an_advisory(self):
        scaled = {
            "mass": LEAF_TOTAL["mass"] * DENSITY_SCALE,
            "com": list(LEAF_TOTAL["com"]),
            "inertia": [[value * DENSITY_SCALE for value in row] for row in LEAF_TOTAL["inertia"]],
        }
        backend = ClosureBackend(
            self.assembly, self.components, dependencies=self.dependencies, assembly_reading=scaled
        )
        snapshot = self.build("scaled", backend)
        record = self.record(snapshot)
        self.assertAlmostEqual(record["delta"]["mass_rel"], 1.0 - 1.0 / DENSITY_SCALE, places=9)
        check = _mass_closure_check(snapshot)
        self.assertEqual(check["status"], "passed", "a mismatch is an advisory, never a blocker")
        self.assertIn("advisory", check["details"])
        self.assertIn(
            f"{DENSITY_SCALE:.4g}", f"{check['details']['top_level_mass_kg'] / check['details']['leaf_total_mass_kg']}"
        )
        notes = report_advisories([check])
        self.assertEqual([note["code"] for note in notes], ["source.normalization.mass_closure"])
        self.assertIn("disagree", notes[0]["message"])

    def test_a_backend_without_the_assembly_reader_stays_not_applicable(self):
        backend = support.FixtureCadBackend(
            self.assembly,
            self.components,
            dependencies=self.dependencies,
        )
        snapshot = self.build("legacy", backend)
        self.assertFalse((snapshot / "raw/mass_closure.json").exists())
        check = _mass_closure_check(snapshot)
        self.assertEqual(check["status"], "not_applicable")
        self.assertEqual(report_advisories([check]), [])


if __name__ == "__main__":
    unittest.main()
