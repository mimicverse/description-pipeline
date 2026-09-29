"""Capture-time mass closure: the assembly's own reading next to the recombined leaf readings.

The record is evidence, and the verification side reports a mismatch as an advisory: a repaired or
re-materialed leaf (a leaf that no longer matches the tree the assembly was built from) becomes
visible without turning into a release gate.  Snapshots that predate the record stay not_applicable.

A build whose legacy ``Extension.GetMassProperties2`` answers instead of ``CreateMassProperty2``
records a **mass-only** closure: only the mass the existing recovery reports (and the 2026-09-29
native pairing) corroborate is recorded and evaluated, with volume as context, and COM/inertia are
never inferred from the legacy vector.

The native assembly reading itself is exercised on Windows by the release rehearsal; these tests
cover the combination, the recorded evidence, the advisory, the mass-only fallback and the
backward-compatible absence.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from . import _paths  # noqa: F401  (import side effect: sys.path)

from description_pipeline.build import report_advisories  # noqa: E402
from description_pipeline.sources.solidworks.errors import CadError  # noqa: E402
from description_pipeline.sources.solidworks.freeze import freeze  # noqa: E402
from description_pipeline.sources.solidworks.native import legacy_mass_reading  # noqa: E402
from description_pipeline.sources.solidworks.verify import _mass_closure_check  # noqa: E402

from . import support  # noqa: E402

#: The factor a repaired leaf showed in the M3.0 servo investigation (assembly vs recovered leaf).
DENSITY_SCALE = 4.87415040334823

#: The pre-restore servo vector as ``Extension.GetMassProperties2`` returned it during the M3.0
#: recovery (``servo-finalize-recover2.json``): volume is index 3 and the corroborated mass is
#: index 5 — the one value that matches the leaf sums independently.
LEGACY_VECTOR = [
    -1.1136424265340597e-05,
    -0.005421601792710847,
    -0.0061814341755483835,
    5.067549822493321e-06,
    0.011036393283905778,
    0.0247,
    4.653682589300229e-06,
    3.095646038335123e-06,
    3.333900655475123e-06,
    -3.422827134582382e-09,
    -9.15973707170355e-10,
    3.100156271697507e-08,
    1.0,
]

#: The top assembly's vector from the 2026-09-29 native run on the released SolidWorks session
#: (revision 34.0.0).  ``IMassProperty2`` answered the same document with the same mass
#: (0.9725657158217046 kg), volume (0.00039948748506975424 m3), COM and flat inertia group, which is
#: the pairing that justifies reading indices 3 and 5 from this legacy layout.
NATIVE_PAIRED_VECTOR = [
    0.03946502173048552,
    -0.11223884967499032,
    0.014740075853236983,
    0.00039948748506975424,
    0.4976448806684602,
    0.9725657158217046,
    0.0054004439999385916,
    0.004873310164924872,
    0.009637775942651533,
    -4.479127033952855e-05,
    -1.5587880809548364e-05,
    -0.000585822484508229,
    1.0,
]


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


def unsupported(error: Exception) -> type:
    """A backend whose whole-assembly read fails the way a missing COM API does."""

    class Unsupported(support.FixtureCadBackend):
        def assembly_mass_properties(self, path: str) -> dict:
            raise error

    return Unsupported


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

    def test_an_unsupported_assembly_mass_api_never_blocks_the_freeze(self):
        """A SolidWorks build without CreateMassProperty2 records 'unavailable'; the capture stands."""

        cases = (
            CadError("cad_empty_mass_property", "CreateMassProperty2 returned null"),
            AttributeError("CreateMassProperty2"),
        )
        for error in cases:
            with self.subTest(error=type(error).__name__):
                backend = unsupported(error)(self.assembly, self.components, dependencies=self.dependencies)
                snapshot = self.build(f"unsupported-{type(error).__name__}", backend)
                record = self.record(snapshot)
                self.assertEqual(record["status"], "unavailable")
                self.assertTrue(record["reason"], record)
                self.assertNotIn("top_level", record)
                check = _mass_closure_check(snapshot)
                self.assertEqual(check["status"], "not_applicable")
                self.assertEqual(check["details"]["unavailable"], record["reason"])
                self.assertEqual(report_advisories([check]), [])

    def test_a_malformed_closure_record_is_a_failure(self):
        """A recorded reading that is not a number is corrupt evidence, unlike an absent probe."""

        backend = ClosureBackend(
            self.assembly, self.components, dependencies=self.dependencies, assembly_reading=LEAF_TOTAL
        )
        snapshot = self.build("malformed", backend)
        (snapshot / "raw/mass_closure.json").write_text(
            json.dumps({"status": "recorded", "top_level": {"mass": "not-a-number"}, "leaf_total": {}}),
            encoding="utf-8",
        )
        check = _mass_closure_check(snapshot)
        self.assertEqual(check["status"], "failed")
        self.assertNotIn("advisory", check["details"])

    def mass_only_backend(self, mass: float, volume: float) -> ClosureBackend:
        """A backend whose assembly reading is the legacy mass-only shape, leaves carrying volumes."""

        components = [
            {
                "name": "base-1",
                "transform": support.placement(),
                "mass": {
                    **support.mass_payload(1.0, (0.0, 0.0, 0.0)),
                    "reference": {"used_api": "fixture", "volume_m3": 2.0e-06},
                },
            },
            {
                "name": "arm-1",
                "transform": support.placement((0.0, 0.0, 0.2), (0.0, 0.0, 0.0)),
                "mass": {
                    **support.mass_payload(
                        0.5, (0.0, 0.0, 0.05), [[0.002, 0.0, 0.0], [0.0, 0.001, 0.0], [0.0, 0.0, 0.001]]
                    ),
                    "reference": {"used_api": "fixture", "volume_m3": 1.0e-06},
                },
            },
        ]
        return ClosureBackend(
            self.assembly,
            components,
            dependencies=self.dependencies,
            assembly_reading={"mass": mass, "volume_m3": volume, "mode": "mass_only"},
        )

    def test_a_legacy_reading_records_a_mass_only_closure(self):
        """The fallback records the mass and volume it can corroborate, and infers nothing else."""

        snapshot = self.build("mass-only", self.mass_only_backend(1.5, 3.0e-06))
        record = self.record(snapshot)
        self.assertEqual(record["status"], "recorded")
        self.assertEqual(record["mode"], "mass_only")
        self.assertEqual(record["not_inferred"], ["com", "inertia"])
        self.assertEqual(set(record["delta"]), {"mass_abs", "mass_rel"})
        self.assertNotIn("com", record["top_level"])
        self.assertNotIn("inertia", record["top_level"])
        self.assertAlmostEqual(record["top_level"]["mass"], 1.5, places=12)
        self.assertAlmostEqual(record["top_level"]["volume_m3"], 3.0e-06, places=18)
        self.assertAlmostEqual(record["leaf_total"]["mass"], 1.5, places=12)
        self.assertAlmostEqual(record["leaf_total"]["volume_m3"], 3.0e-06, places=18)
        self.assertAlmostEqual(record["delta"]["mass_rel"], 0.0, places=12)
        check = _mass_closure_check(snapshot)
        self.assertEqual(check["status"], "passed")
        self.assertEqual(check["details"]["mode"], "mass_only")
        self.assertNotIn("advisory", check["details"])
        self.assertEqual(report_advisories([check]), [])

    def test_a_mass_only_density_gap_is_an_advisory(self):
        snapshot = self.build("mass-only-scaled", self.mass_only_backend(1.5 * DENSITY_SCALE, 3.0e-06))
        check = _mass_closure_check(snapshot)
        self.assertEqual(check["status"], "passed", "a mismatch is an advisory, never a blocker")
        self.assertIn("advisory", check["details"])
        self.assertAlmostEqual(check["details"]["delta"]["mass_rel"], 1.0 - 1.0 / DENSITY_SCALE, places=9)
        notes = report_advisories([check])
        self.assertEqual([note["code"] for note in notes], ["source.normalization.mass_closure"])
        self.assertIn("disagree", notes[0]["message"])
        self.assertIn("mass only", notes[0]["message"])

    def test_a_mass_only_record_is_evaluated_without_com_or_inertia(self):
        """The check reads the two masses only: a record stripped to them still evaluates."""

        snapshot = self.build("mass-only-stripped", self.mass_only_backend(1.5, 3.0e-06))
        record = self.record(snapshot)
        record["top_level"] = {"mass": record["top_level"]["mass"]}
        record["leaf_total"] = {"mass": record["leaf_total"]["mass"], "volume_m3": None}
        (snapshot / "raw/mass_closure.json").write_text(json.dumps(record), encoding="utf-8")
        check = _mass_closure_check(snapshot)
        self.assertEqual(check["status"], "passed")
        self.assertNotIn("advisory", check["details"])
        self.assertNotIn("top_level_volume_m3", check["details"], "an absent volume stays absent context")
        self.assertNotIn("leaf_total_volume_m3", check["details"])

    def test_a_malformed_mass_only_record_is_a_failure(self):
        """Absent or unusable mass is corrupt evidence, unlike an absent probe or an unavailable API."""

        cases = (
            {"status": "recorded", "mode": "mass_only", "top_level": {"mass": "not-a-number"}, "leaf_total": {}},
            {"status": "recorded", "mode": "mass_only", "top_level": {"mass": -1.5}, "leaf_total": {"mass": 1.5}},
            {"status": "recorded", "mode": "mass_only", "top_level": {}, "leaf_total": {"mass": 1.5}},
            {
                "status": "recorded",
                "mode": "mass_only",
                "top_level": {"mass": float("nan")},
                "leaf_total": {"mass": 1.5},
            },
        )
        for index, payload in enumerate(cases):
            with self.subTest(case=index):
                snapshot = self.tmp / f"malformed-mass-only-{index}"
                (snapshot / "raw").mkdir(parents=True)
                (snapshot / "raw/mass_closure.json").write_text(json.dumps(payload), encoding="utf-8")
                check = _mass_closure_check(snapshot)
                self.assertEqual(check["status"], "failed")
                self.assertNotIn("advisory", check["details"])
                self.assertEqual(check["details"]["mode"], "mass_only")


class LegacyMassReadingTests(unittest.TestCase):
    """The pure parser for ``Extension.GetMassProperties2``'s 13-value vector — no COM needed."""

    def test_the_recovery_vector_yields_its_corroborated_mass_and_volume(self):
        reading = legacy_mass_reading(LEGACY_VECTOR, 0)
        self.assertEqual(reading["mode"], "mass_only")
        self.assertAlmostEqual(reading["mass"], 0.0247, places=12)
        self.assertAlmostEqual(reading["volume_m3"], 5.067549822493321e-06, places=18)
        self.assertNotIn("com", reading)
        self.assertNotIn("inertia", reading)

    def test_the_native_pairing_vector_parses_to_what_imassproperty2_returned(self):
        """The 2026-09-29 run paired both APIs on one document; indices 3 and 5 have to hold up."""

        reading = legacy_mass_reading(NATIVE_PAIRED_VECTOR, 0)
        self.assertEqual(reading["mode"], "mass_only")
        self.assertAlmostEqual(reading["mass"], 0.9725657158217046, places=15)
        self.assertAlmostEqual(reading["volume_m3"], 0.00039948748506975424, places=18)

    def test_malformed_vectors_and_statuses_are_capture_errors(self):
        cases = {
            "short": (LEGACY_VECTOR[:5], 0),
            "non-numeric": ([*LEGACY_VECTOR[:5], "heavy"], 0),
            "reported-status": (LEGACY_VECTOR, 1),
            "missing": (None, 0),
        }
        for name, (values, status) in cases.items():
            with self.subTest(case=name), self.assertRaises(CadError):
                legacy_mass_reading(values, status)

    def test_a_non_finite_or_non_positive_mass_is_invalid(self):
        for mass in (0.0, -1.0, float("nan"), float("inf")):
            with self.subTest(mass=mass):
                values = list(LEGACY_VECTOR)
                values[5] = mass
                with self.assertRaises(CadError) as caught:
                    legacy_mass_reading(values, 0)
                self.assertEqual(caught.exception.code, "cad_mass_property_invalid")


if __name__ == "__main__":
    unittest.main()
