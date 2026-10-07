"""Independent-oracle semantics: resealed adversarial records must fail on meaning.

Each test builds one genuinely valid package with the generator, then mutates the bound native
record and *reseals* it: the record's own digest and the native inventory digest in ``robot.yaml``
are recomputed, so ``discovery.binding`` passes and the only possible failure is the semantic rule
under test.  Nothing here touches SolidWorks; the verification module itself imports no generator.
"""

from __future__ import annotations

import copy
import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path

import yaml

from description_pipeline.sources.solidworks.discovery import (
    CONTRACT,
    DISCOVERY_SCHEMA,
    DiscoverySettings,
    prepare_native_package,
)
from description_pipeline.verification.native_discovery import verify_discovery

RECORD_FILE = "discovery/native-discovery.json"

IDENTITY = [
    1.0,
    0.0,
    0.0,
    0.0,
    0.0,
    1.0,
    0.0,
    0.0,
    0.0,
    0.0,
    1.0,
    0.0,
    0.0,
    0.0,
    0.0,
    1.0,
]


def _translated(x: float, y: float, z: float) -> list[float]:
    return [
        1.0,
        0.0,
        0.0,
        x,
        0.0,
        1.0,
        0.0,
        y,
        0.0,
        0.0,
        1.0,
        z,
        0.0,
        0.0,
        0.0,
        1.0,
    ]


def native_record() -> dict:
    """Two bodies joined by a concentric shaft and a coincident plane (analytic fixture)."""
    return {
        "schema_version": DISCOVERY_SCHEMA,
        "contract": CONTRACT,
        "namespace": "dp",
        "identity": {
            "hardware_id": "oracle_fixture",
            "revision": "r1",
            "owner": "mechanical-team",
            "change_summary": "oracle semantics fixture",
            "control": {"system": "handoff", "reference": "handoff-oracle"},
            "delivery_configuration": "Default",
            "main_assembly": "cad/robot.SLDASM",
            "robot_name": "oracle_fixture",
        },
        "components": [
            {
                "name2": "base-1",
                "instance_id": "base-1",
                "document": "cad/base.SLDPRT",
                "configuration": "Default",
                "fixed": True,
                "suppressed": False,
                "transform": copy.deepcopy(IDENTITY),
            },
            {
                "name2": "arm-1",
                "instance_id": "arm-1",
                "document": "cad/arm.SLDPRT",
                "configuration": "Default",
                "fixed": False,
                "suppressed": False,
                "transform": copy.deepcopy(IDENTITY),
            },
        ],
        "mates": [
            {
                "name": "shoulder_pitch_joint__coaxial",
                "type": "concentric",
                "suppressed": False,
                "error_code": 0,
                "scope": "",
                "limits": None,
                "entities": [
                    {
                        "component": "base-1",
                        "feature": "Cyl1",
                        "face_index": 3,
                        "cylinder": {
                            "point": [0.0, 0.0, 0.1],
                            "direction": [0.0, 0.0, 1.0],
                            "radius": 0.006,
                        },
                    },
                    {
                        "component": "arm-1",
                        "feature": "Cyl2",
                        "face_index": 7,
                        "cylinder": {
                            "point": [0.0, 0.0, 0.1],
                            "direction": [0.0, 0.0, 1.0],
                            "radius": 0.006,
                        },
                    },
                ],
            },
            {
                "name": "shoulder_pitch_joint__locate",
                "type": "coincident",
                "suppressed": False,
                "error_code": 0,
                "scope": "",
                "limits": None,
                "entities": [
                    {
                        "component": "base-1",
                        "feature": "Plane1",
                        "plane": {"point": [0.0, 0.0, 0.1], "normal": [0.0, 0.0, 1.0]},
                    },
                    {
                        "component": "arm-1",
                        "feature": "Plane2",
                        "plane": {"point": [0.0, 0.0, 0.1], "normal": [0.0, 0.0, -1.0]},
                    },
                ],
            },
        ],
        "datums": [
            {"name": "CS_base_link", "owner": "base-1", "array": copy.deepcopy(IDENTITY)},
            {"name": "CS_arm_link", "owner": "arm-1", "array": _translated(0.0, 0.0, 0.1)},
        ],
        "masses": [
            {"component": "base-1", "mass_kg": 0.192, "material": "Alloy Steel"},
            {"component": "arm-1", "mass_kg": 0.105654866776462, "material": "Alloy Steel"},
        ],
        "assembly_mass_kg": 0.2976548667764616,
        "assembly_extent_m": 0.42,
        "properties": {
            "document": {"dp.design_budget_record": "budget.json#robot"},
            "components": {},
            "mates": {
                "shoulder_pitch_joint__coaxial": {
                    "dp.joint.axis_sign": "+1",
                    "dp.joint.limits_record": "joints/arm.json#limits",
                    "dp.joint.drive_record": "joints/arm.json#drive",
                }
            },
        },
        "files": {},
    }


class _ReplayBackend:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def discover_native(self, frozen_source: Path, settings: dict) -> dict:
        return copy.deepcopy(self.payload)


class OracleSemanticsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="oracle-semantics-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.serial = 0

    def baseline(self) -> Path:
        """One package produced by the generator from the analytic fixture."""
        self.serial += 1
        source = self.tmp / f"native{self.serial}"
        for name in ("cad/robot.SLDASM", "cad/base.SLDPRT", "cad/arm.SLDPRT"):
            path = source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(f"placeholder {name}\n".encode())
        records = self.tmp / "records"
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
            run_id="oracle-run",
            backend=_ReplayBackend(native_record()),
            settings=DiscoverySettings(record_roots=(records,)),
        )
        self.assertTrue(result.passed, result.findings)
        return output

    @staticmethod
    def _native(package: Path, mutate) -> Path:
        """Mutate the bound record and reseal both digests, keeping the binding check honest."""
        record_path = package / RECORD_FILE
        payload = json.loads(record_path.read_text(encoding="utf-8"))
        mutate(payload["raw"], payload)
        # allow_nan keeps adversarial Infinity/NaN literals in the record on purpose: the oracle
        # must reject them semantically, not only because the bytes changed.
        record_path.write_text(
            json.dumps(payload, sort_keys=True, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        robot_path = package / "robot.yaml"
        robot = yaml.safe_load(robot_path.read_text(encoding="utf-8"))
        robot["provenance"]["discovery_sha256"] = hashlib.sha256(record_path.read_bytes()).hexdigest()
        robot["provenance"]["native_inventory_sha256"] = hashlib.sha256(
            (
                json.dumps(payload["native_files"], sort_keys=True, ensure_ascii=False, allow_nan=False, indent=2)
                + "\n"
            ).encode()
        ).hexdigest()
        robot_path.write_text(yaml.safe_dump(robot, sort_keys=False), encoding="utf-8")
        return package

    def check(self, package: Path) -> tuple[bool, list[dict], dict]:
        report = verify_discovery(package)
        checks = {entry["id"]: entry for entry in report["checks"]}
        # The reseal itself must be sound: a failure may only come from the semantic rule.
        self.assertTrue(checks["discovery.binding"]["passed"], checks["discovery.binding"])
        self.assertNotIn("discovery.internal", [error["code"] for error in report["errors"]])
        return report["passed"], report["errors"], checks

    def reject(self, mutate, expected: str) -> None:
        package = self.baseline()
        self._native(package, mutate)
        passed, errors, _checks = self.check(package)
        self.assertFalse(passed, errors)
        graph = [error for error in errors if error["code"] == "discovery.graph"]
        self.assertTrue(any(expected in error["message"] for error in graph), errors)

    # ---------------------------------------------------------------- controls

    def test_resealed_baseline_still_passes(self) -> None:
        package = self.baseline()
        self._native(package, lambda raw, payload: None)
        passed, errors, _checks = self.check(package)
        self.assertTrue(passed, errors)

    def test_suppressed_mate_is_excluded_not_checked(self) -> None:
        package = self.baseline()
        self._native(package, lambda raw, payload: raw["mates"][1].update({"suppressed": True}))
        _passed, errors, checks = self.check(package)
        # The graph check itself must pass; later checks may disagree with the stale robot.yaml.
        self.assertTrue(checks["discovery.graph"]["passed"], checks["discovery.graph"])
        self.assertNotIn("discovery.graph", [error["code"] for error in errors])

    # ------------------------------------------------------------ finite data

    def test_nonfinite_cylinder_point_blocks(self) -> None:
        self.reject(
            lambda raw, payload: raw["mates"][0]["entities"][0]["cylinder"].update({"point": [float("inf"), 0.0, 0.1]}),
            "a cylinder point is not finite",
        )

    def test_nonfinite_or_nonpositive_cylinder_radius_blocks(self) -> None:
        for radius in (float("inf"), float("nan"), 0.0, -0.006, True):
            with self.subTest(radius=radius):
                self.reject(
                    lambda raw, payload, value=radius: raw["mates"][0]["entities"][0]["cylinder"].update(
                        {"radius": value}
                    ),
                    "a cylinder radius is not a finite positive number",
                )

    def test_nonfinite_plane_geometry_blocks(self) -> None:
        self.reject(
            lambda raw, payload: raw["mates"][1]["entities"][1]["plane"].update({"point": [0.0, float("nan"), 0.1]}),
            "a plane entity is not a finite point and normal",
        )
        self.reject(
            lambda raw, payload: raw["mates"][1]["entities"][0]["plane"].update({"normal": [0.0, 0.0, float("inf")]}),
            "a plane entity is not a finite point and normal",
        )

    def test_nonfinite_point_entity_blocks(self) -> None:
        def mutate(raw, payload):
            raw["mates"][1]["entities"][1].clear()
            raw["mates"][1]["entities"][1].update({"component": "arm-1", "point": [0.0, 0.0, float("inf")]})

        self.reject(mutate, "a point entity is not a finite point")

    def test_nonfinite_circle_geometry_blocks(self) -> None:
        for circle in (
            {"center": [0.0, 0.0, float("inf")], "normal": [0.0, 0.0, 1.0], "radius": 0.006},
            {"center": [0.0, 0.0, 0.1], "normal": [0.0, 0.0, float("nan")], "radius": 0.006},
            {"center": [0.0, 0.0, 0.1], "normal": [0.0, 0.0, 1.0], "radius": float("inf")},
            {"center": [0.0, 0.0, 0.1], "normal": [0.0, 0.0, 1.0], "radius": 0.0},
            # A zero normal is finite but unusable, a boolean radius is not a radius, and a
            # missing radius must fail semantically rather than as an internal error.
            {"center": [0.0, 0.0, 0.1], "normal": [0.0, 0.0, 0.0], "radius": 0.006},
            {"center": [0.0, 0.0, 0.1], "normal": [0.0, 0.0, 1.0], "radius": True},
            {"center": [0.0, 0.0, 0.1], "normal": [0.0, 0.0, 1.0]},
        ):
            with self.subTest(circle=circle):
                self.reject(
                    lambda raw, payload, value=circle: raw["mates"][1]["entities"][0].update({"circle": value}),
                    "a circle entity is not a finite circle",
                )

    def test_numeric_strings_are_not_numbers(self) -> None:
        def mutate(raw, payload):
            raw["mates"][1]["entities"][1].clear()
            raw["mates"][1]["entities"][1].update({"component": "arm-1", "point": ["0", 0.0, 0.1]})

        self.reject(mutate, "a point entity is not a finite point")

        def string_transform(raw, payload):
            raw["components"][1]["transform"][3] = "0.0"

        self.reject(string_transform, "outside the supported constraint scope")

    def test_large_finite_axis_cannot_overflow_to_a_zero_vector(self) -> None:
        """hypot normalization keeps a huge finite direction usable instead of collapsing to zero."""

        def mutate(raw, payload):
            for entity in raw["mates"][0]["entities"]:
                entity["cylinder"]["direction"] = [1e200, 0.0, 0.0]

        package = self.baseline()
        self._native(package, mutate)
        _passed, _errors, checks = self.check(package)
        self.assertTrue(checks["discovery.graph"]["passed"], checks["discovery.graph"])

    # ------------------------------------------------------- solve state flags

    def test_non_boolean_suppression_flag_blocks(self) -> None:
        for value in ("false", 0, 1, None):
            with self.subTest(suppressed=value):
                self.reject(
                    lambda raw, payload, flag=value: raw["mates"][0].update({"suppressed": flag}),
                    "a mate suppression flag is not a boolean",
                )

    def test_solve_state_must_be_integer_zero(self) -> None:
        for value in (0.0, True, 1, None, "0"):
            with self.subTest(error_code=value):
                self.reject(
                    lambda raw, payload, code=value: raw["mates"][0].update({"error_code": code}),
                    "reports a native error or an unreadable solve state",
                )

    # --------------------------------------------------------------- limits

    def test_bounded_limits_must_be_finite_ordered_and_unit_checked(self) -> None:
        for limits in (
            {"lower": -0.05, "upper": float("inf"), "unit": "m"},
            {"lower": -0.05, "upper": float("nan"), "unit": "m"},
            {"lower": 0.05, "upper": -0.05, "unit": "m"},
            {"lower": 0.05, "upper": 0.05, "unit": "m"},
            {"lower": -0.05, "upper": 0.05, "unit": "mm"},
            {"lower": "0", "upper": "1", "unit": "m"},
            ["lower", "upper"],
        ):
            with self.subTest(limits=limits):
                self.reject(
                    lambda raw, payload, value=limits: raw["mates"][0].update({"limits": value}),
                    "a bounded mate limit is not a finite ordered range with a unit",
                )

    def test_typed_bounded_mates_require_matching_bounds(self) -> None:
        for kind, limits in (
            ("limitdistance", None),
            ("limitangle", None),
            ("limitdistance", {"lower": -0.05, "upper": 0.05, "unit": "rad"}),
            ("limitangle", {"lower": -0.05, "upper": 0.05, "unit": "m"}),
            ("limitdistance", {"lower": 0.05, "upper": 0.05, "unit": "m"}),
        ):
            unit = "m" if kind == "limitdistance" else "rad"
            with self.subTest(kind=kind, limits=limits):
                self.reject(
                    lambda raw, payload, mate_type=kind, value=limits: raw["mates"][1].update(
                        {"type": mate_type, "limits": value}
                    ),
                    f"a {kind} mate must carry finite lower < upper bounds in {unit}",
                )

    def test_typed_bounded_mate_with_matching_bounds_keeps_the_graph_valid(self) -> None:
        def mutate(raw, payload):
            raw["mates"][1].update({"type": "limitdistance", "limits": {"lower": -0.05, "upper": 0.05, "unit": "m"}})

        package = self.baseline()
        self._native(package, mutate)
        _passed, _errors, checks = self.check(package)
        self.assertTrue(checks["discovery.graph"]["passed"], checks["discovery.graph"])

    # -------------------------------------------------- shaft and circle semantics

    def test_concentric_mate_requires_true_cylinders(self) -> None:
        def circles(raw, payload):
            for entity in raw["mates"][0]["entities"]:
                cylinder = entity.pop("cylinder")
                entity["circle"] = {
                    "center": list(cylinder["point"]),
                    "normal": list(cylinder["direction"]),
                    "radius": cylinder["radius"],
                }

        self.reject(circles, "outside the supported constraint scope")

    def test_coincident_circles_must_share_one_centre(self) -> None:
        def far_apart(raw, payload):
            for entity, centre in zip(
                raw["mates"][1]["entities"],
                ([0.0, 0.0, 0.1], [0.001, 0.0, 0.1]),
                strict=True,
            ):
                entity.pop("plane")
                entity["circle"] = {"center": centre, "normal": [0.0, 0.0, 1.0], "radius": 0.006}

        self.reject(far_apart, "outside the supported constraint scope")

        def within_tolerance(raw, payload):
            for entity in raw["mates"][1]["entities"]:
                entity.pop("plane")
                entity["circle"] = {"center": [0.0, 0.0, 0.1], "normal": [0.0, 0.0, 1.0], "radius": 0.006}

        package = self.baseline()
        self._native(package, within_tolerance)
        _passed, _errors, checks = self.check(package)
        self.assertTrue(checks["discovery.graph"]["passed"], checks["discovery.graph"])

    # ------------------------------------------------ occurrence and datum ownership

    def test_duplicate_component_occurrence_blocks(self) -> None:
        def mutate(raw, payload):
            raw["components"].append(copy.deepcopy(raw["components"][1]))

        self.reject(mutate, "component occurrence names must be unique and non-empty")

    def test_body_frame_datum_must_be_owned_by_that_body(self) -> None:
        def mutate(raw, payload):
            raw["datums"][0]["owner"] = "arm-1"

        package = self.baseline()
        self._native(package, mutate)
        passed, errors, _checks = self.check(package)
        self.assertFalse(passed, errors)
        self.assertTrue(
            any(
                error["code"] == "discovery.bodies" and "owned uniquely by that body" in error["message"]
                for error in errors
            ),
            errors,
        )

    def test_ambiguous_same_named_datum_for_one_body_blocks(self) -> None:
        def mutate(raw, payload):
            twin = copy.deepcopy(raw["datums"][1])
            twin["array"] = [1.0, 0.0, 0.0, 9.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0]
            raw["datums"].append(twin)

        package = self.baseline()
        self._native(package, mutate)
        passed, errors, _checks = self.check(package)
        self.assertFalse(passed, errors)
        self.assertTrue(
            any(
                error["code"] in {"discovery.bodies", "discovery.joints", "discovery.frames"}
                and "owned uniquely" in error["message"]
                for error in errors
            ),
            errors,
        )

    def test_nested_occurrence_paths_need_a_real_assembly_parent(self) -> None:
        def orphan(raw, payload):
            ghost = copy.deepcopy(raw["components"][1])
            ghost.update({"name2": "ghost-1/arm-2", "instance_id": "ghost-1/arm-2"})
            raw["components"].append(ghost)

        def under_a_part(raw, payload):
            ghost = copy.deepcopy(raw["components"][1])
            ghost.update({"name2": "base-1/arm-2", "instance_id": "base-1/arm-2"})
            raw["components"].append(ghost)

        for mutate in (orphan, under_a_part):
            with self.subTest(mutate=mutate.__name__):
                package = self.baseline()
                self._native(package, mutate)
                passed, errors, _checks = self.check(package)
                self.assertFalse(passed, errors)


if __name__ == "__main__":
    unittest.main()
