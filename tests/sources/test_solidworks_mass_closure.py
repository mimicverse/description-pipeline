"""Capture-time mass closure: the assembly's own reading next to the recombined leaf readings.

The record is evidence, and the verification side reports a whole-assembly mismatch as an advisory:
a leaf that no longer matches the tree the assembly was built from becomes visible without turning
into a release gate.  The component-context source guard is stricter on purpose: a recorded instance
override, or an effective mass the selected part documents cannot explain, fails a pure-CAD
(`material_source: cad`) model, while a documented table keeps it as a note.  Snapshots that predate
the record stay not_applicable.

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
from types import SimpleNamespace
from typing import Any

from . import _paths  # noqa: F401  (import side effect: sys.path)

from description_pipeline.build import report_advisories  # noqa: E402
from description_pipeline.sources.solidworks.errors import CadError  # noqa: E402
from description_pipeline.sources.solidworks.freeze import _component_context_record, freeze  # noqa: E402
from description_pipeline.sources.solidworks.native import (  # noqa: E402
    legacy_mass_reading,
    unverified_material_record,
)
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


class ClosureFixture(unittest.TestCase):
    """A fixture capture: one assembly, two leaves, and the shared config/build helpers."""

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


class MassClosureTests(ClosureFixture):
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


def context_row(
    name: str,
    mass: float,
    *,
    depth: int = 0,
    parent: str | None = None,
    document_type: str = "part",
    overrides: dict | None = None,
) -> dict:
    flags = {"OverrideMass": False, "OverrideCenterOfMass": False, "OverrideMomentsOfInertia": False}
    flags.update(overrides or {})
    return {
        "name": name,
        "parent": parent,
        "depth": depth,
        "document": f"{name.replace('/', '-')}.SLDPRT",
        "document_type": document_type,
        "context_mass_kg": mass,
        "context_volume_m3": 1e-6,
        "overrides": flags,
    }


class ContextBackend(ClosureBackend):
    """A fixture backend that also answers the per-instance assembly-context reading."""

    def __init__(
        self,
        *arguments,
        component_context: dict | None = None,
        context_error: Exception | None = None,
        **keywords,
    ) -> None:
        super().__init__(*arguments, **keywords)
        self.component_context = component_context
        self.context_error = context_error

    def assembly_component_mass_properties(self, path: str) -> dict:
        if self.context_error is not None:
            raise self.context_error
        return {**(self.component_context or {"instances": [], "errors": []}), "reference": {"used_api": "fixture"}}


class MassContextTests(ClosureFixture):
    """The component-context record: evidence, gating, and explicit error reporting."""

    def context(
        self, *, rows: list[dict], errors: list[dict] | None = None, error: Exception | None = None
    ) -> ContextBackend:
        return ContextBackend(
            self.assembly,
            self.components,
            dependencies=self.dependencies,
            assembly_reading={
                "mass": 1.75,
                "com": list(LEAF_TOTAL["com"]),
                "inertia": [list(row) for row in LEAF_TOTAL["inertia"]],
            },
            component_context={
                "assembly": str(self.assembly),
                "configuration": "Default",
                "instances": rows,
                "errors": errors or [],
            },
            context_error=error,
        )

    def test_component_context_is_recorded_and_stays_advisory_for_documented_tables(self):
        rows = [context_row("base-1", 1.0), context_row("arm-1", 0.75, overrides={"OverrideMass": True})]
        snapshot = self.build("context-recorded", self.context(rows=rows))
        context = self.record(snapshot)["component_context"]
        self.assertEqual(context["schema_version"], "description-pipeline.solidworks-component-mass-context/v1")
        self.assertEqual(context["status"], "recorded")
        self.assertEqual(context["overridden"]["top_level_instances"], 2)
        self.assertEqual(context["leaf_documents_covered"], 2)
        self.assertEqual(context["leaf_documents_uncovered"], [])
        self.assertEqual(context["overridden"]["mass"], 1)
        self.assertEqual(context["overridden"]["top_level_overridden"], 1)
        self.assertAlmostEqual(context["totals"]["context_kg"], 1.75, places=12)
        self.assertAlmostEqual(context["totals"]["document_kg"], 1.5, places=12)
        self.assertAlmostEqual(context["totals"]["context_minus_document_kg"], 0.25, places=12)
        arm = next(row for row in context["instances"] if row["name"] == "arm-1")
        self.assertAlmostEqual(arm["document_basis_mass_kg"], 0.5, places=12)
        self.assertAlmostEqual(arm["delta_kg"], 0.25, places=12)
        check = _mass_closure_check(snapshot, "documented_table")
        self.assertEqual(check["status"], "passed", check)
        self.assertEqual(check["details"]["component_context"]["mass_overridden"], 1)
        self.assertIn("overrides on", check["details"]["advisory"])
        self.assertIn("no cause is inferred", check["details"]["advisory"])
        self.assertEqual([note["code"] for note in report_advisories([check])], ["source.normalization.mass_closure"])

    def test_component_context_overrides_fail_a_pure_cad_model(self):
        rows = [context_row("base-1", 1.0), context_row("arm-1", 1.5, overrides={"OverrideMass": True})]
        snapshot = self.build("context-cad", self.context(rows=rows))
        check = _mass_closure_check(snapshot, "cad")
        self.assertEqual(check["status"], "failed", check)
        self.assertIn("component-level overrides", check["details"]["error"])
        self.assertIn("center of mass", check["details"]["error"])

    def test_an_unavailable_component_context_only_blocks_a_pure_cad_model(self):
        backend = self.context(rows=[], error=CadError("cad_empty_mass_property", "no root component"))
        snapshot = self.build("context-unavailable", backend)
        context = self.record(snapshot)["component_context"]
        self.assertEqual(context["status"], "unavailable")
        self.assertEqual(context["reason"], "cad_empty_mass_property")
        documented = _mass_closure_check(snapshot, "documented_table")
        self.assertEqual(documented["status"], "passed", documented)
        self.assertEqual(documented["details"]["component_context"]["status"], "unavailable")
        cad = _mass_closure_check(snapshot, "cad")
        self.assertEqual(cad["status"], "failed")
        self.assertIn("could not be read", cad["details"]["error"])

    def test_a_partial_component_context_is_reported_and_blocks_only_pure_cad(self):
        rows = [context_row("base-1", 1.0)]
        errors = [{"name": "arm-1", "error": "cad_mass_property_invalid", "message": "no"}]
        snapshot = self.build("context-partial", self.context(rows=rows, errors=errors))
        context = self.record(snapshot)["component_context"]
        self.assertEqual(context["status"], "partial")
        self.assertEqual(len(context["errors"]), 1)
        self.assertEqual(context["leaf_documents_uncovered"], ["arm-1"])
        documented = _mass_closure_check(snapshot, "documented_table")
        self.assertEqual(documented["status"], "passed", documented)
        self.assertIn("component mass context is incomplete", documented["details"]["advisory"])
        cad = _mass_closure_check(snapshot, "cad")
        self.assertEqual(cad["status"], "failed")
        self.assertIn("incomplete", cad["details"]["error"])

    def test_an_invalid_instance_reading_becomes_a_row_error_at_capture(self):
        backend = self.context(rows=[context_row("base-1", 1.0), context_row("arm-1", float("nan"))])
        snapshot = self.build("context-invalid-row", backend)
        context = self.record(snapshot)["component_context"]
        self.assertEqual(context["status"], "partial")
        self.assertEqual([entry["name"] for entry in context["errors"]], ["arm-1"])
        self.assertEqual(context["leaf_documents_uncovered"], ["arm-1"])
        cad = _mass_closure_check(snapshot, "cad")
        self.assertEqual(cad["status"], "failed")
        self.assertIn("incomplete", cad["details"]["error"])

    def test_a_malformed_component_context_is_a_failure(self):
        inertia = [[0.001, 0.0, 0.0], [0.0, 0.001, 0.0], [0.0, 0.0, 0.001]]
        base = {
            "status": "recorded",
            "mode": "full",
            "top_level": {"mass": 1.5, "com": [0.0, 0.0, 0.0], "inertia": inertia},
            "leaf_total": {"mass": 1.5, "com": [0.0, 0.0, 0.0], "inertia": inertia},
            "component_context": {
                "status": "recorded",
                "assembly_mass_kg": 1.5,
                "instances": [
                    {
                        "name": "base-1",
                        "depth": 0,
                        "context_mass_kg": 1.5,
                        "overrides": {
                            "OverrideMass": False,
                            "OverrideCenterOfMass": False,
                            "OverrideMomentsOfInertia": False,
                        },
                    }
                ],
            },
        }
        cases = {
            "non-finite instance mass": lambda record: record["component_context"]["instances"][0].__setitem__(
                "context_mass_kg", float("nan")
            ),
            "missing override flags": lambda record: record["component_context"]["instances"][0].pop("overrides"),
            "unknown context status": lambda record: record["component_context"].__setitem__("status", "later"),
            "empty instances": lambda record: record["component_context"].__setitem__("instances", []),
            "wrong depth": lambda record: record["component_context"]["instances"][0].__setitem__("depth", 1),
            "duplicate rows": lambda record: record["component_context"]["instances"].append(
                json.loads(json.dumps(record["component_context"]["instances"][0]))
            ),
            "non-boolean flag": lambda record: record["component_context"]["instances"][0]["overrides"].__setitem__(
                "OverrideMass", "false"
            ),
            "recorded with row errors": lambda record: record["component_context"].__setitem__(
                "errors", [{"name": "base-1", "error": "cad_component_context_invalid"}]
            ),
        }
        for index, (label, mutate) in enumerate(cases.items()):
            with self.subTest(case=label):
                snapshot = self.tmp / f"malformed-context-{index}"
                (snapshot / "raw").mkdir(parents=True)
                record = json.loads(json.dumps(base))
                mutate(record)
                (snapshot / "raw/mass_closure.json").write_text(json.dumps(record), encoding="utf-8")
                check = _mass_closure_check(snapshot, "documented_table")
                self.assertEqual(check["status"], "failed", check)
                self.assertIn("component mass context", check["details"]["error"])

    def closure_record(self, instances: list[dict], *, assembly_mass: float = 1.5) -> dict:
        inertia = [[0.001, 0.0, 0.0], [0.0, 0.001, 0.0], [0.0, 0.0, 0.001]]
        for row in instances:
            if row["depth"] == 0:
                row.setdefault("document_basis_mass_kg", row["context_mass_kg"])
        return {
            "status": "recorded",
            "mode": "full",
            "top_level": {"mass": assembly_mass, "com": [0.0, 0.0, 0.0], "inertia": inertia},
            "leaf_total": {"mass": assembly_mass, "com": [0.0, 0.0, 0.0], "inertia": inertia},
            "component_context": {
                "status": "recorded",
                "assembly_mass_kg": assembly_mass,
                "instances": instances,
                "errors": [],
            },
        }

    def handwritten(self, name: str, record: dict, leaves: dict[str, float]) -> Path:
        snapshot = self.tmp / name
        (snapshot / "raw").mkdir(parents=True)
        (snapshot / "raw/mass_closure.json").write_text(json.dumps(record), encoding="utf-8")
        (snapshot / "raw/scene_raw.json").write_text(
            json.dumps({"components": [{"name": leaf} for leaf in leaves]}), encoding="utf-8"
        )
        (snapshot / "raw/mass_properties.json").write_text(
            json.dumps({leaf: {"mass": mass} for leaf, mass in leaves.items()}), encoding="utf-8"
        )
        return snapshot

    def nested_rows(self) -> list[dict]:
        return [
            context_row("p-1", 1.5, document_type="assembly"),
            context_row("p-1/a", 0.5, depth=1, parent="p-1"),
            context_row("p-1/b", 1.0, depth=1, parent="p-1"),
        ]

    def test_a_nested_override_under_a_clean_parent_fails_pure_cad(self):
        rows = self.nested_rows()
        rows[1]["overrides"]["OverrideCenterOfMass"] = True
        snapshot = self.handwritten("nested-override", self.closure_record(rows), {"p-1/a": 0.5, "p-1/b": 1.0})
        documented = _mass_closure_check(snapshot, "documented_table")
        self.assertEqual(documented["status"], "passed", documented)
        context = documented["details"]["component_context"]
        self.assertEqual(context["top_level_instances"], 1)
        self.assertEqual(context["instances"], 3)
        self.assertEqual(context["com_overridden"], 1)
        self.assertEqual(context["any_override"], 1)
        self.assertIn("overrides on", documented["details"]["advisory"])
        cad = _mass_closure_check(snapshot, "cad")
        self.assertEqual(cad["status"], "failed", cad)
        self.assertIn("component-level overrides", cad["details"]["error"])

    def test_an_inertia_only_override_fails_pure_cad(self):
        rows = [context_row("base-1", 1.0), context_row("arm-1", 0.5, overrides={"OverrideMomentsOfInertia": True})]
        snapshot = self.handwritten("inertia-override", self.closure_record(rows), {"base-1": 1.0, "arm-1": 0.5})
        self.assertEqual(_mass_closure_check(snapshot, "documented_table")["status"], "passed")
        cad = _mass_closure_check(snapshot, "cad")
        self.assertEqual(cad["status"], "failed", cad)
        self.assertEqual(cad["details"]["component_context"]["inertia_overridden"], 1)

    def test_totals_use_only_the_disjoint_top_level_rows(self):
        snapshot = self.handwritten(
            "nested-totals", self.closure_record(self.nested_rows()), {"p-1/a": 0.5, "p-1/b": 1.0}
        )
        check = _mass_closure_check(snapshot, "cad")
        self.assertEqual(check["status"], "passed", check)
        context = check["details"]["component_context"]
        self.assertAlmostEqual(context["context_total_kg"], 1.5, places=12)
        self.assertAlmostEqual(context["context_minus_assembly_kg"], 0.0, places=12)

    def test_an_omitted_leaf_row_blocks_pure_cad_and_is_named(self):
        rows = self.nested_rows()[:2]
        snapshot = self.handwritten("omitted-leaf", self.closure_record(rows), {"p-1/a": 0.5, "p-1/b": 1.0})
        documented = _mass_closure_check(snapshot, "documented_table")
        self.assertEqual(documented["status"], "passed", documented)
        self.assertIn("not recorded", documented["details"]["advisory"])
        cad = _mass_closure_check(snapshot, "cad")
        self.assertEqual(cad["status"], "failed")
        self.assertIn("not recorded", cad["details"]["error"])
        self.assertEqual(cad["details"]["component_context"]["coverage"]["missing_nodes"], ["p-1/b"])

    def test_a_missing_intermediate_ancestor_row_blocks_pure_cad(self):
        rows = [
            context_row("module", 1.5, document_type="assembly"),
            context_row("module/drive/part", 1.5, depth=2, parent="module/drive"),
        ]
        snapshot = self.handwritten("ancestor-omitted", self.closure_record(rows), {"module/drive/part": 1.5})
        documented = _mass_closure_check(snapshot, "documented_table")
        self.assertEqual(documented["status"], "passed", documented)
        self.assertIn("not recorded", documented["details"]["advisory"])
        cad = _mass_closure_check(snapshot, "cad")
        self.assertEqual(cad["status"], "failed", cad)
        coverage = cad["details"]["component_context"]["coverage"]
        self.assertEqual(coverage["missing_nodes"], ["module/drive"])
        self.assertEqual(coverage["missing_parent_rows"], ["module/drive/part"])

    def test_an_extra_unknown_assembly_row_blocks_pure_cad(self):
        rows = [
            *self.nested_rows(),
            context_row("module/other", 0.5, depth=1, parent="module", document_type="assembly"),
        ]
        record = self.closure_record(rows)
        extra = next(row for row in record["component_context"]["instances"] if row["name"] == "module/other")
        extra["document_basis_mass_kg"] = 0.0
        snapshot = self.handwritten("extra-row", record, {"p-1/a": 0.5, "p-1/b": 1.0})
        documented = _mass_closure_check(snapshot, "documented_table")
        self.assertEqual(documented["status"], "passed", documented)
        self.assertIn("unexpected node row", documented["details"]["advisory"])
        cad = _mass_closure_check(snapshot, "cad")
        self.assertEqual(cad["status"], "failed", cad)
        self.assertEqual(cad["details"]["component_context"]["coverage"]["unexpected_nodes"], ["module/other"])

    def test_a_wrong_node_type_is_malformed(self):
        rows = self.nested_rows()
        rows[1]["document_type"] = "assembly"
        snapshot = self.handwritten("wrong-type", self.closure_record(rows), {"p-1/a": 0.5, "p-1/b": 1.0})
        check = _mass_closure_check(snapshot, "documented_table")
        self.assertEqual(check["status"], "failed", check)
        self.assertIn("invalid node types", check["details"]["error"])

    def test_an_effective_mass_the_documents_cannot_explain_blocks_pure_cad(self):
        rows = [context_row("base-1", 1.0)]
        record = self.closure_record(rows)
        record["component_context"]["instances"][0]["document_basis_mass_kg"] = 0.5
        snapshot = self.handwritten("effective-mismatch", record, {"base-1": 0.5})
        documented = _mass_closure_check(snapshot, "documented_table")
        self.assertEqual(documented["status"], "passed", documented)
        self.assertEqual(documented["details"]["component_context"]["effective_vs_document_mismatches"], 1)
        self.assertIn("differs from the recomputed part-document basis", documented["details"]["advisory"])
        cad = _mass_closure_check(snapshot, "cad")
        self.assertEqual(cad["status"], "failed", cad)
        self.assertIn("differs from the selected part-document reading", cad["details"]["error"])

    def test_a_cached_document_basis_that_contradicts_the_raw_readings_is_rejected(self):
        rows = [context_row("base-1", 1.0)]
        rows[0]["document_basis_mass_kg"] = 1000.0
        snapshot = self.handwritten("cached-basis", self.closure_record(rows), {"base-1": 1.0})
        check = _mass_closure_check(snapshot, "documented_table")
        self.assertEqual(check["status"], "failed", check)
        self.assertIn("cached document basis contradicts", check["details"]["error"])
        self.assertNotIn("document_total_kg", check["details"])

    def test_a_missing_cached_basis_displays_the_recomputed_total(self):
        record = self.closure_record([context_row("base-1", 1.0)])
        record["component_context"]["instances"][0].pop("document_basis_mass_kg")
        snapshot = self.handwritten("cached-basis-missing", record, {"base-1": 1.0})
        cad = _mass_closure_check(snapshot, "cad")
        self.assertEqual(cad["status"], "passed", cad)
        self.assertEqual(cad["details"]["component_context"]["document_total_kg"], 1.0)

    def test_an_unavailable_assembly_probe_still_records_and_gates_the_context(self):
        class Raised(ContextBackend):
            def assembly_mass_properties(self, path: str) -> dict:
                raise CadError("cad_empty_mass_property", "no CreateMassProperty2")

        rows = [context_row("base-1", 1.0), context_row("arm-1", 0.5, overrides={"OverrideMass": True})]
        backend = Raised(
            self.assembly,
            self.components,
            dependencies=self.dependencies,
            assembly_reading={},
            component_context={
                "assembly": str(self.assembly),
                "configuration": "Default",
                "instances": rows,
                "errors": [],
            },
        )
        snapshot = self.build("context-no-assembly", backend)
        record = self.record(snapshot)
        self.assertEqual(record["status"], "unavailable")
        self.assertEqual(record["component_context"]["status"], "recorded")
        documented = _mass_closure_check(snapshot, "documented_table")
        self.assertEqual(documented["status"], "not_applicable", documented)
        self.assertIn("overrides on", documented["details"]["advisory"])
        cad = _mass_closure_check(snapshot, "cad")
        self.assertEqual(cad["status"], "failed", cad)
        self.assertIn("component-level overrides", cad["details"]["error"])

    def test_a_backend_without_the_context_reader_leaves_no_section(self):
        backend = ClosureBackend(
            self.assembly, self.components, dependencies=self.dependencies, assembly_reading=LEAF_TOTAL
        )
        snapshot = self.build("context-absent", backend)
        self.assertNotIn("component_context", self.record(snapshot))
        self.assertEqual(_mass_closure_check(snapshot, "documented_table")["status"], "passed")
        self.assertEqual(_mass_closure_check(snapshot, "cad")["status"], "passed")


class ComponentContextRecordTests(unittest.TestCase):
    """The capture-side record: disjoint top-level totals, nested rows kept for override detection."""

    def test_totals_use_only_the_disjoint_top_level_rows(self):
        scene = SimpleNamespace(
            components=[SimpleNamespace(name="p-1/a"), SimpleNamespace(name="p-1/b")],
            mass_properties={"p-1/a": {"mass": 0.5}, "p-1/b": {"mass": 1.0}},
        )
        reading = {
            "instances": [
                context_row("p-1", 1.5, document_type="assembly"),
                context_row("p-1/a", 0.5, depth=1, parent="p-1"),
                context_row("p-1/b", 1.0, depth=1, parent="p-1"),
            ],
            "errors": [],
        }
        record = _component_context_record(reading, scene, 1.5)
        self.assertEqual(record["totals"]["context_kg"], 1.5)
        self.assertEqual(record["totals"]["document_kg"], 1.5)
        self.assertEqual(record["totals"]["context_minus_assembly_kg"], 0.0)
        self.assertEqual(record["overridden"]["top_level_instances"], 1)
        self.assertEqual(record["leaf_documents_covered"], 2)
        self.assertEqual(record["leaf_documents_uncovered"], [])

    def test_a_non_boolean_flag_becomes_a_row_error(self):
        scene = SimpleNamespace(components=[], mass_properties={})
        row = context_row("p-1", 1.5, document_type="assembly")
        row["overrides"]["OverrideMass"] = "false"
        record = _component_context_record({"instances": [row], "errors": []}, scene, 1.5)
        self.assertEqual(record["status"], "partial")
        self.assertEqual([entry["name"] for entry in record["errors"]], ["p-1"])
        self.assertEqual(record["instances"], [])


class ClosureRecordValidationTests(ClosureFixture):
    """A corrupt full record is a defect, not a silently passing comparison."""

    def set_field(self, record: dict, path: list, value) -> None:
        target = record
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value

    def test_non_finite_or_misshaped_fields_are_rejected(self):
        backend = ClosureBackend(
            self.assembly, self.components, dependencies=self.dependencies, assembly_reading=LEAF_TOTAL
        )
        snapshot = self.build("validation", backend)
        path = snapshot / "raw/mass_closure.json"
        baseline = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(_mass_closure_check(snapshot)["status"], "passed")
        mutations: list[tuple[str, list[Any], Any]] = [
            ("nan top mass", ["top_level", "mass"], float("nan")),
            ("inf top mass", ["top_level", "mass"], float("inf")),
            ("zero top mass", ["top_level", "mass"], 0.0),
            ("negative top mass", ["top_level", "mass"], -1.0),
            ("nan leaf mass", ["leaf_total", "mass"], float("nan")),
            ("nan top com", ["top_level", "com", 0], float("nan")),
            ("inf top com", ["top_level", "com", 1], float("inf")),
            ("short top com", ["top_level", "com"], [1.0, 2.0]),
            ("nan top inertia", ["top_level", "inertia", 0, 0], float("nan")),
            ("inf leaf inertia", ["leaf_total", "inertia", 0, 0], float("inf")),
            ("short top inertia", ["top_level", "inertia"], [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]]),
        ]
        for label, field_path, value in mutations:
            with self.subTest(case=label):
                record = json.loads(json.dumps(baseline))
                self.set_field(record, field_path, value)
                path.write_text(json.dumps(record), encoding="utf-8")
                check = _mass_closure_check(snapshot)
                self.assertEqual(check["status"], "failed", check)
                self.assertNotIn("advisory", check["details"])
                error = check["details"]["error"]
                self.assertTrue(any(word in error for word in ("finite", "3-vector", "3x3 tensor")), error)
        path.write_text(json.dumps(baseline), encoding="utf-8")


class MaterialFallbackTests(unittest.TestCase):
    """``require_material=False`` keeps the assignment detail the CAD error already carried."""

    def test_the_unverified_record_keeps_the_full_assignment(self):
        assignment = {
            "schema_version": "swbridge.material-assignment/v1",
            "configuration": "Default",
            "part": {"name": "", "database": ""},
            "body_count": 1,
            "bodies": [{"index": 0, "name": "b", "material": {"name": "", "database": ""}}],
        }
        error = CadError(
            "cad_material_provenance_missing",
            "Every solid requires an explicit physical material; default density is not evidence",
            {"missing_body_indices": [0], "material_assignment": assignment},
        )
        record = unverified_material_record(error, "Default")
        self.assertEqual(record["unverified_reason"], "cad_material_provenance_missing")
        self.assertEqual(record["configuration"], "Default")
        self.assertEqual(record["missing_body_indices"], [0])
        self.assertEqual(record["material_assignment"], assignment)

    def test_a_detail_less_error_still_produces_the_fallback(self):
        record = unverified_material_record(CadError("cad_material_read_failed", "boom"), "Default")
        self.assertEqual(record["unverified_reason"], "cad_material_read_failed")
        self.assertEqual(record["message"], "boom")
        self.assertNotIn("material_assignment", record)
        self.assertNotIn("missing_body_indices", record)


if __name__ == "__main__":
    unittest.main()
