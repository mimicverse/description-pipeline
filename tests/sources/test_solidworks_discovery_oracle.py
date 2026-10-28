"""Independent-oracle semantics: resealed adversarial records must fail on meaning.

Each test builds one genuinely valid package with the generator, then mutates the bound native
record and *reseals* it: the record's own digest and the delivery binding are recomputed, so
``discovery.binding`` passes and the only possible failure is the semantic rule under test.
Nothing here touches SolidWorks; the verification module itself imports no generator.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from description_pipeline.sources.solidworks.discovery import (
    CONTRACT,
    DISCOVERY_SCHEMA,
    DiscoverySettings,
    prepare_native_package,
)
from description_pipeline.verification import native_discovery
from description_pipeline.verification.native_discovery import (
    rebuild_joint_facts,
    rebuild_rigid_bodies,
    verify_discovery,
)

from . import support
from .selection_support import sealed_selection

RECORD_FILE = "input/discovery/native-discovery.json"

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


def _matrix4(values: list[float]) -> list[list[float]]:
    return [values[0:4], values[4:8], values[8:12], values[12:16]]


def _in_span(vector: list[float], basis: list[list[float]], tolerance: float = 1e-9) -> bool:
    residual = [float(value) for value in vector]
    for item in basis:
        weight = sum(one * two for one, two in zip(residual, item, strict=True))
        residual = [one - weight * two for one, two in zip(residual, item, strict=True)]
    return math.sqrt(sum(value * value for value in residual)) <= tolerance


def native_record() -> dict:
    """Two bodies joined by a concentric shaft and a coincident plane (analytic fixture)."""

    files = {
        name: hashlib.sha256(f"placeholder {name}\n".encode()).hexdigest()
        for name in (
            "cad/robot.SLDASM",
            "cad/base.SLDPRT",
            "cad/sub.SLDASM",
            "cad/arm.SLDPRT",
        )
    }
    source_selection = sealed_selection(
        files,
        "cad/robot.SLDASM",
        edges={
            "cad/robot.SLDASM": [
                "cad/base.SLDPRT",
                "cad/sub.SLDASM",
                "cad/arm.SLDPRT",
            ],
            "cad/base.SLDPRT": [],
            "cad/sub.SLDASM": [],
            "cad/arm.SLDPRT": [],
        },
    )
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
        "saved_configuration": "Default",
        "document_state": support.saved_document_state(),
        "components": [
            {
                "name2": "base-1",
                "lightweight": False,
                "suppression_state": 2,
                "instance_id": "base-1",
                "document": "cad/base.SLDPRT",
                "configuration": "Default",
                "fixed": True,
                "suppressed": False,
                "transform": copy.deepcopy(IDENTITY),
            },
            {
                "name2": "arm-1",
                "lightweight": False,
                "suppression_state": 2,
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
                "entity_count": 2,
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
                "entity_count": 2,
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
            {
                "name": "shoulder_pitch_joint__limit",
                "type": "limitangle",
                "entity_count": 2,
                "suppressed": False,
                "error_code": 0,
                "scope": "",
                "limits": {"lower": -1.5, "upper": 1.5, "unit": "rad"},
                "is_advanced_mate": True,
                "is_advanced_mate_error": None,
                "is_advanced_mate_api": "IAngleMateFeatureData.IsAdvancedMate",
                "native_limit": support.native_limit_observation(6, nominal=0.0, lower=-1.5, upper=1.5),
                "entities": [
                    {
                        "entity_index": 0,
                        "component": "base-1",
                        "reference_configuration": "Default",
                        "feature": "LimitPlane1",
                        "mate_entity_reference": {"error": None, "geometry": None},
                        "plane": {"point": [0.0, 0.0, 0.1], "normal": [0.0, 0.0, 1.0]},
                    },
                    {
                        "entity_index": 1,
                        "component": "arm-1",
                        "reference_configuration": "Default",
                        "feature": "LimitPlane2",
                        "mate_entity_reference": {"error": None, "geometry": None},
                        "plane": {"point": [0.0, 0.0, 0.1], "normal": [0.0, 0.0, 1.0]},
                    },
                ],
            },
        ],
        "datums": [
            {"name": "CS_base_link", "owner": "base-1", "array": copy.deepcopy(IDENTITY)},
            {"name": "CS_arm_link", "owner": "arm-1", "array": _translated(0.0, 0.0, 0.1)},
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
        # The fixture's frozen bytes are the deterministic placeholders written by
        # ``baseline``; the derive contract requires the record to bind them.
        "files": files,
        "source_selection": source_selection,
    }


def frame_ground_mates(component: str = "base-1") -> list[dict]:
    """Three solved mates that rigidly ground ``component`` in the top assembly frame.

    The aggregate is rank 6 (concentric 4 rows + coincident 3 + parallel 2 = 9 rows),
    the same structure the M3 torso exhibits, expressed in the assembly frame that the
    fixture's identity transforms already use.
    """

    def entity(component_name: str, feature: str, geometry: dict) -> dict:
        item = {"component": component_name, "feature": feature}
        if component_name == "":
            item["assembly_frame"] = True
        item.update(copy.deepcopy(geometry))
        return item

    def mate(name: str, kind: str, geometry: dict) -> dict:
        return {
            "name": name,
            "type": kind,
            "entity_count": 2,
            "suppressed": False,
            "error_code": 0,
            "scope": "",
            "limits": None,
            "entities": [entity(component, "FaceA", geometry), entity("", "FaceB", geometry)],
        }

    cylinder = {"cylinder": {"point": [0.0, -0.088001, 0.0], "direction": [0.0, 1.0, 0.0], "radius": 0.006}}
    plane = {"plane": {"point": [-0.027, 0.0, 0.03675756117070245], "normal": [0.0, 1.0, 0.0]}}
    parallel = {"plane": {"point": [0.0, -0.034, -0.017], "normal": [0.0, 0.0, -1.0]}}
    return [
        mate("base_ground__coaxial", "concentric", cylinder),
        mate("base_ground__locate", "coincident", plane),
        mate("base_ground__align", "parallel", parallel),
    ]


class _ReplayBackend:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def discover_native(self, frozen_source: Path, settings: dict, *, on_activity=None) -> dict:
        return support.local_native_record_payload(
            copy.deepcopy(self.payload)
        )


class OracleSemanticsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="oracle-semantics-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.serial = 0

    def baseline(self, record: dict | None = None) -> Path:
        """One delivery produced by the generator from the analytic fixture (or a variant)."""
        self.serial += 1
        record = native_record() if record is None else record
        source = self.tmp / f"native{self.serial}"
        for name in record["files"]:
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
            backend=_ReplayBackend(record),
            settings=DiscoverySettings(record_roots=(records,)),
        )
        self.assertTrue(result.passed, result.findings)
        delivery = self.tmp / f"delivery{self.serial}"
        return support.native_discovery_delivery(delivery, output)

    @staticmethod
    def _native(package: Path, mutate) -> Path:
        """Mutate the bound record and reseal the manifest, keeping the binding check honest."""
        record_path = package / RECORD_FILE
        payload = json.loads(record_path.read_text(encoding="utf-8"))
        canonical_raw = support.native_consumer_view(payload["raw"])
        mutate(canonical_raw, payload)
        payload["raw"] = support.local_native_record_payload(canonical_raw)
        # Allow non-finite literals when callers inject them so the shared strict
        # reader, rather than manifest resealing, is the rejection boundary.
        record_path.write_text(
            json.dumps(payload, sort_keys=True, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        OracleSemanticsTests._seal(package, payload)
        return package

    @staticmethod
    def _seal(package: Path, payload: dict | None = None) -> None:
        """Write the record (when given) and refresh the delivery manifest binding."""
        record_path = package / RECORD_FILE
        if payload is not None:
            record_path.write_text(
                json.dumps(payload, sort_keys=True, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
        support.seal_delivery(package)

    def check(self, package: Path) -> tuple[bool, list[dict], dict]:
        report = verify_discovery(package)
        checks = {entry["id"]: entry for entry in report["checks"]}
        self.assertNotIn("discovery.internal", [error["code"] for error in report["errors"]])
        # Diagnostics must stay JSON-serializable even when the raw payload carried Infinity/NaN,
        # and computed sets must already be sorted lists.
        json.dumps(report, allow_nan=False)
        return report["passed"], report["errors"], checks

    def reject(self, mutate, expected: str) -> None:
        package = self.baseline()
        self._native(package, mutate)
        passed, errors, _checks = self.check(package)
        self.assertFalse(passed, errors)
        binding = next(
            (error for error in errors if error["code"] == "discovery.binding"),
            None,
        )
        if binding is not None:
            self.assertIn(
                "the JSON document carries the non-finite constant",
                binding["message"],
            )
            return
        self.assertTrue(
            any(expected in error["message"] for error in errors),
            errors,
        )

    # ---------------------------------------------------------------- controls

    def test_resealed_baseline_still_passes(self) -> None:
        package = self.baseline()
        self._native(package, lambda raw, payload: None)
        passed, errors, _checks = self.check(package)
        self.assertTrue(passed, errors)

    def test_unrecorded_controlled_joint_records_do_not_override_native_limits(self):
        """Controlled records are optional once the native limit is directly observed."""

        package = self.baseline()

        def strip_annotations(raw, payload):
            for group in (raw.get("properties") or {}).get("mates", {}).values():
                if isinstance(group, dict):
                    for key in ("dp.joint.limits_record", "dp.joint.drive_record"):
                        group.pop(key, None)

        self._native(package, strip_annotations)
        report = verify_discovery(package)
        self.assertTrue(report["passed"], report["errors"])

    def test_absent_exporter_state_keeps_the_mate_suite_and_skips_declarations(self) -> None:
        """Only a proven absent exporter state means the capture has no declarations.

        The native capture writes the exporter block unconditionally; the verifier must
        mirror the producer predicate so an absent-state capture runs the mate-inference
        suite it was derived for instead of failing closed in the declaration boundary.
        """

        package = self.baseline()

        def declare_absent(raw, _payload):
            raw["exporter"] = {
                "state": "absent",
                "attribute_name": "URDF Export Configuration (v1.4)",
                "document": "cad/robot.SLDASM",
                "configuration": "Default",
                "registration": {},
                "pid_bindings": [],
            }

        self._native(package, declare_absent)
        passed, errors, checks = self.check(package)
        self.assertTrue(passed, errors)
        self.assertIn("discovery.joints", checks)
        self.assertIn("discovery.frames", checks)
        self.assertNotIn("discovery.declarations", checks)

    def test_unread_exporter_state_stays_in_declarations_and_fails_closed(self) -> None:
        """Every present non-absent state keeps the declarations suite and its refusal."""

        package = self.baseline()

        def declare_unread(raw, _payload):
            raw["exporter"] = {
                "state": "unread",
                "attribute_name": "URDF Export Configuration (v1.4)",
                "document": "cad/robot.SLDASM",
                "configuration": "Default",
                "registration": {},
                "pid_bindings": [],
                "error": "native read failed",
            }

        self._native(package, declare_unread)
        passed, _errors, checks = self.check(package)
        self.assertFalse(passed)
        self.assertIn("discovery.declarations", checks)
        self.assertFalse(checks["discovery.declarations"]["passed"])
        self.assertNotIn("discovery.joints", checks)

    def test_suppressed_mate_is_excluded_not_checked(self) -> None:
        package = self.baseline()
        self._native(package, lambda raw, payload: raw["mates"][1].update({"suppressed": True}))
        _passed, errors, checks = self.check(package)
        # The graph check itself must pass; later checks may disagree with the stale carrier.
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

        self.reject(
            string_transform,
            "raw local SolidWorks transform is invalid",
        )

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
        ):
            unit = "m" if kind == "limitdistance" else "rad"
            with self.subTest(kind=kind, limits=limits):
                self.reject(
                    lambda raw, payload, mate_type=kind, value=limits: raw["mates"][1].update(
                        {"type": mate_type, "limits": value}
                    ),
                    f"a {kind} mate must carry finite ordered bounds in {unit}",
                )

    def test_typed_bounded_mate_with_matching_bounds_keeps_the_graph_valid(self) -> None:
        for kind, bounds in (
            ("limitdistance", {"lower": -0.05, "upper": 0.05, "unit": "m"}),
            ("limitdistance", {"lower": 0.0, "upper": 0.0, "unit": "m"}),
            ("coincident", {"lower": 0.0, "upper": 0.0, "unit": "m"}),
        ):
            with self.subTest(kind=kind, bounds=bounds):
                def mutate(raw, payload, mate_type=kind, limits=bounds):
                    raw["mates"][1].update({"type": mate_type, "limits": limits})

                package = self.baseline()
                self._native(package, mutate)
                _passed, _errors, checks = self.check(package)
                self.assertTrue(checks["discovery.graph"]["passed"], checks["discovery.graph"])

    def test_joint_rebuild_passes_the_proven_root_owner_to_limit_resolution(self) -> None:
        package = self.baseline()

        def mutate(raw, payload):
            raw["datums"].append(
                {"name": "CS_base_link", "owner": "", "array": copy.deepcopy(IDENTITY)}
            )

        self._native(package, mutate)
        payload = json.loads((package / RECORD_FILE).read_text(encoding="utf-8"))
        observed = []
        original = native_discovery.limit_entity_owners

        def resolve(mate, owner_for, *, assembly_owner=None):
            observed.append(assembly_owner)
            return original(mate, owner_for, assembly_owner=assembly_owner)

        with mock.patch(
            "description_pipeline.verification.native_discovery.limit_entity_owners",
            side_effect=resolve,
        ):
            rebuild_joint_facts(payload, package, purpose="engineering")
        self.assertEqual(observed, ["base_link"])

    # -------------------------------------------------- shaft and circle semantics

    def test_concentric_circles_reconstruct_constraints_but_never_evidence_the_shaft(self) -> None:
        """Circles are axis geometry for reconstruction; the final shaft gate stays cylindrical."""

        def circles(raw, payload):
            for entity in raw["mates"][0]["entities"]:
                cylinder = entity.pop("cylinder")
                entity["circle"] = {
                    "center": list(cylinder["point"]),
                    "normal": list(cylinder["direction"]),
                    "radius": cylinder["radius"],
                }

        package = self.baseline()
        self._native(package, circles)
        passed, errors, _checks = self.check(package)
        self.assertFalse(passed, errors)
        self.assertTrue(
            any(
                error["code"] in {"discovery.joints", "discovery.joint_rebuild"}
                and "no cylindrical mate entity carries the joint shaft" in error["message"]
                for error in errors
            ),
            errors,
        )

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

        self.reject(mutate, "share one stable identity")

    def test_body_frame_datum_must_be_owned_by_that_body(self) -> None:
        def mutate(raw, payload):
            raw["datums"][0]["owner"] = "arm-1"

        package = self.baseline()
        self._native(package, mutate)
        passed, errors, _checks = self.check(package)
        self.assertFalse(passed, errors)
        self.assertTrue(
            any(
                error["code"] == "discovery.body_rebuild"
                and (
                    "owned by one body" in error["message"]
                    or "no CS_<link> coordinate system is owned" in error["message"]
                )
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
                error["code"] == "discovery.body_rebuild" and "owned by one body" in error["message"]
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

    # ------------------------------------------------- assemblies as containers

    @staticmethod
    def _container(name: str = "sub-1", document: str = "cad/sub.SLDASM") -> dict:
        return {
            "name2": name,
            "lightweight": False,
            "suppression_state": 2,
            "instance_id": name,
            "document": document,
            "configuration": "Default",
            "fixed": False,
            "suppressed": False,
            "transform": copy.deepcopy(IDENTITY),
        }

    def test_body_must_not_list_an_assembly_container(self) -> None:
        """A rigid container cannot become material of the body it is attached to."""

        def mutate(raw, payload):
            raw["components"].append(self._container())
            raw["mates"].append(
                {
                    "name": "base_to_sub_lock",
                    "type": "lock",
                    "entity_count": 2,
                    "suppressed": False,
                    "error_code": 0,
                    "scope": "",
                    "limits": None,
                    "entities": [
                        {
                            "component": "base-1",
                            "feature": "P1",
                            "plane": {"point": [0.0, 0.0, 0.0], "normal": [0.0, 0.0, 1.0]},
                        },
                        {
                            "component": "sub-1",
                            "feature": "P2",
                            "plane": {"point": [0.0, 0.0, 0.0], "normal": [0.0, 0.0, 1.0]},
                        },
                    ],
                }
            )

        package = self.baseline()
        self._native(package, mutate)
        passed, errors, _checks = self.check(package)
        # The container rule lives in the rebuild: a rigidly attached container stays a
        # connector and never becomes material of the body it is solved against, while the
        # raw evidence itself is legal (the saved carrier is not a verification input).
        self.assertTrue(passed, errors)
        from description_pipeline.verification.native_discovery import rebuild_rigid_bodies

        record = json.loads((package / RECORD_FILE).read_text(encoding="utf-8"))
        base = next(body for body in rebuild_rigid_bodies(record) if body["name"] == "base_link")
        self.assertEqual(base["components"], ["base-1"])

    def test_hierarchy_and_fixed_flag_do_not_prove_rigidity(self) -> None:
        from description_pipeline.verification.native_discovery import _independent_clusters

        raw = native_record()
        raw["components"].append(self._container())
        child = copy.deepcopy(raw["components"][1])
        child.update({"name2": "sub-1/arm-2", "instance_id": "sub-1/arm-2", "fixed": True})
        raw["components"].append(child)
        members, _pairs = _independent_clusters(raw)
        for group in members.values():
            merged = {"sub-1", "sub-1/arm-2"} <= set(group)
            self.assertFalse(merged, group)

    def test_container_datum_binds_only_through_a_solved_rigid_cluster(self) -> None:
        """A flexible sub-assembly's container owns no descendant body frame by path alone."""

        def move_frame_to_container(raw, payload):
            raw["components"].append(self._container())
            for datum in raw["datums"]:
                if datum["name"] == "CS_arm_link":
                    datum["owner"] = "sub-1"

        def lock_container_to_arm(raw, payload):
            move_frame_to_container(raw, payload)
            raw["mates"].append(
                {
                    "name": "sub_to_arm_lock",
                    "type": "lock",
                    "entity_count": 2,
                    "suppressed": False,
                    "error_code": 0,
                    "scope": "",
                    "limits": None,
                    "entities": [
                        {
                            "component": "arm-1",
                            "feature": "P1",
                            "plane": {"point": [0.0, 0.0, 0.1], "normal": [0.0, 0.0, 1.0]},
                        },
                        {
                            "component": "sub-1",
                            "feature": "P2",
                            "plane": {"point": [0.0, 0.0, 0.1], "normal": [0.0, 0.0, 1.0]},
                        },
                    ],
                }
            )

        from description_pipeline.verification.native_discovery import _cluster_bindings

        flexible = native_record()
        move_frame_to_container(flexible, {})
        flexible_bindings, _by_component = _cluster_bindings(flexible)
        # The container is connected by no rank-6 evidence, so it belongs to no material cluster.
        self.assertNotIn("sub-1", {name for full in flexible_bindings.values() for name in full})

        rigid = native_record()
        lock_container_to_arm(rigid, {})
        rigid_bindings, _by_component = _cluster_bindings(rigid)
        self.assertIn("sub-1", rigid_bindings[frozenset({"arm-1"})])

        # Negative: the arm body's frame moved to the container while the container is flexible.
        package = self.baseline()
        self._native(package, move_frame_to_container)
        passed, errors, _checks = self.check(package)
        self.assertFalse(passed, errors)
        self.assertTrue(
            any(
                error["code"] == "discovery.body_rebuild"
                and "no CS_<link> coordinate system is owned" in error["message"]
                for error in errors
            ),
            errors,
        )

        # Positive: once the container is rigidly locked to the arm, the same datum binds.
        package = self.baseline()
        self._native(package, lock_container_to_arm)
        passed, errors, _checks = self.check(package)
        self.assertTrue(passed, errors)

    def test_foreign_same_named_link_datum_stays_an_interface(self) -> None:
        """A foreign datum sharing a link name never vanishes silently.

        The rebuild mirrors the producer's body-datum selection: with the foreign datum the
        base cluster owns two CS_ datums and none is declared, so the ambiguity blocks
        (the frame rebuild keeps the exact (owner, name) exclusion for the body's own datum).
        """

        def mutate(raw, payload):
            raw["datums"].append({"name": "CS_arm_link", "owner": "base-1", "array": _translated(0.0, 0.0, 0.2)})

        package = self.baseline()
        self._native(package, mutate)
        passed, errors, _checks = self.check(package)
        self.assertFalse(passed, errors)
        self.assertTrue(
            any(
                error["code"] == "discovery.body_rebuild" and "owned by one body" in error["message"]
                for error in errors
            ),
            errors,
        )

    # ------------------------------------------------------------ lock evidence

    @staticmethod
    def _lock_record(*, with_point: bool) -> dict:
        """One rigid pair: two parts locked together, with a single body datum."""
        record = native_record()
        entities = [
            {"component": "base-1", "feature": "V1"},
            {"component": "arm-1", "feature": "V2"},
        ]
        if with_point:
            entities[0]["point"] = [0.0, 0.0, 0.1]
            entities[1]["point"] = [0.0, 0.0, 0.1]
        record["mates"] = [
            {
                "name": "base_arm_lock",
                "type": "lock",
                "entity_count": 2,
                "suppressed": False,
                "error_code": 0,
                "scope": "",
                "limits": None,
                "entities": entities,
            }
        ]
        record["properties"]["mates"] = {}
        record["datums"] = [datum for datum in record["datums"] if datum["name"] == "CS_base_link"]
        return record

    def test_lock_needs_only_both_occurrence_frames(self) -> None:
        """A solved lock removes six DOF from the captured frames alone; faces are not evidence."""
        package = self.baseline(record=self._lock_record(with_point=True))

        def strip_point_geometry(raw, payload):
            for entity in raw["mates"][0]["entities"]:
                entity.pop("point", None)

        self._native(package, strip_point_geometry)
        passed, errors, _checks = self.check(package)
        self.assertTrue(passed, errors)
        record = json.loads((package / RECORD_FILE).read_text(encoding="utf-8"))
        bodies = [(body["name"], sorted(body["components"])) for body in rebuild_rigid_bodies(record)]
        self.assertEqual(bodies, [("base_link", ["arm-1", "base-1"])])

    def test_lock_with_a_missing_occurrence_frame_blocks(self) -> None:
        package = self.baseline(record=self._lock_record(with_point=True))

        def strip_point_and_arm_frame(raw, payload):
            for entity in raw["mates"][0]["entities"]:
                entity.pop("point", None)
            for component in raw["components"]:
                if component["name2"] == "arm-1":
                    component["transform"] = []

        self._native(package, strip_point_and_arm_frame)
        passed, errors, _checks = self.check(package)
        self.assertFalse(passed, errors)
        self.assertTrue(
            any(
                error["code"] == "discovery.graph" and "supported constraint scope" in error["message"]
                for error in errors
            ),
            errors,
        )

    # ------------------------------------------------------- frame attachment

    def test_full_rank_frame_attachment_is_verified_without_inventing_grounding(self) -> None:
        """A rank-6 rigid attachment to the top frame is accepted; no joint or mass appears."""

        def mutate(raw, payload):
            raw["mates"].extend(frame_ground_mates())

        package = self.baseline()
        self._native(package, mutate)
        passed, errors, checks = self.check(package)
        self.assertTrue(passed, errors)
        self.assertTrue(checks["discovery.frame_attachment"]["passed"], checks["discovery.frame_attachment"])
        self.assertEqual(checks["discovery.frame_attachment"]["details"]["rank"], 6)
        self.assertTrue(checks["discovery.bodies"]["passed"], checks["discovery.bodies"])
        # The rigid attachment invents no world joint and no extra movement.
        self.assertEqual(checks["discovery.joints"]["details"]["joints"], 1)
    def test_top_owned_datum_binds_only_the_proven_frame_cluster(self) -> None:
        """The ownerless top-assembly datum channel opens exactly for the proven attachment."""

        def move_frame_to_assembly(raw, payload):
            for datum in raw["datums"]:
                if datum["name"] == "CS_base_link":
                    datum["owner"] = ""

        # Control: without a proven frame attachment the channel stays closed.
        control = self.baseline()
        self._native(control, move_frame_to_assembly)
        passed, errors, _checks = self.check(control)
        self.assertFalse(passed, errors)
        self.assertTrue(
            any(
                error["code"] == "discovery.body_rebuild"
                and "no CS_<link> coordinate system is owned" in error["message"]
                for error in errors
            ),
            errors,
        )

        def ground_and_move(raw, payload):
            move_frame_to_assembly(raw, payload)
            raw["mates"].extend(frame_ground_mates())

        package = self.baseline()
        self._native(package, ground_and_move)
        passed, errors, checks = self.check(package)
        self.assertTrue(passed, errors)
        self.assertTrue(checks["discovery.frame_attachment"]["passed"], checks["discovery.frame_attachment"])
        self.assertTrue(checks["discovery.bodies"]["passed"], checks["discovery.bodies"])

    def test_incomplete_frame_attachment_blocks(self) -> None:
        def mutate(raw, payload):
            raw["mates"].append(frame_ground_mates()[0])

        package = self.baseline()
        self._native(package, mutate)
        passed, errors, checks = self.check(package)
        self.assertFalse(passed, errors)
        self.assertFalse(checks["discovery.frame_attachment"]["passed"], checks["discovery.frame_attachment"])
        self.assertTrue(
            any(error["code"] == "discovery.frame_attachment" and "full rank" in error["message"] for error in errors),
            errors,
        )

    def test_two_frame_attached_clusters_block(self) -> None:
        def mutate(raw, payload):
            raw["mates"].extend(frame_ground_mates("base-1"))
            raw["mates"].extend(frame_ground_mates("arm-1"))

        package = self.baseline()
        self._native(package, mutate)
        passed, errors, _checks = self.check(package)
        self.assertFalse(passed, errors)
        self.assertTrue(
            any(
                error["code"] == "discovery.frame_attachment" and "exactly one rigid cluster" in error["message"]
                for error in errors
            ),
            errors,
        )

    def test_nested_self_frame_is_an_ordinary_pair_and_not_a_frame_problem(self) -> None:
        """The scope's own frame entity is an ordinary in-scope endpoint (design contract).

        A nested self-frame relation reconstructs as a normal in-scope pair and must not
        raise the frozen-top frame-attachment block.  The undeclared material component
        added alongside it is still caught by the independent body derivation, which is
        a different channel and stays untouched.
        """

        def mutate(raw, payload):
            raw["components"].append(self._container())
            child = copy.deepcopy(raw["components"][1])
            child.update({"name2": "sub-1/arm-2", "instance_id": "sub-1/arm-2"})
            raw["components"].append(child)
            raw["mates"].append(
                {
                    "name": "nested_self__coaxial",
                    "type": "concentric",
                    "entity_count": 2,
                    "suppressed": False,
                    "error_code": 0,
                    "scope": "sub-1",
                    "limits": None,
                    "entities": [
                        {
                            "component": "sub-1/arm-2",
                            "feature": "Cyl1",
                            "cylinder": {
                                "point": [0.0, 0.0, 0.1],
                                "direction": [0.0, 0.0, 1.0],
                                "radius": 0.006,
                            },
                        },
                        {
                            "component": "sub-1",
                            "assembly_frame": True,
                            "feature": "Cyl2",
                            "cylinder": {
                                "point": [0.0, 0.0, 0.1],
                                "direction": [0.0, 0.0, 1.0],
                                "radius": 0.006,
                            },
                        },
                    ],
                }
            )

        package = self.baseline()
        self._native(package, mutate)
        _passed, errors, _checks = self.check(package)
        self.assertFalse(
            any(error["code"] == "discovery.frame_attachment" for error in errors),
            errors,
        )
        self.assertTrue(
            any(error["code"] in {"discovery.bodies", "discovery.body_rebuild"} for error in errors), errors
        )

    def test_assembly_frame_identity_requires_the_explicit_marker(self) -> None:
        from description_pipeline.verification.native_discovery import _frame_attachment, _frames, _rows_for

        raw = native_record()
        frames = _frames(raw)
        cylinder = {"point": [0.0, 0.0, 0.1], "direction": [0.0, 0.0, 1.0], "radius": 0.006}
        base = {"component": "base-1", "feature": "Cyl1", "cylinder": copy.deepcopy(cylinder)}
        marked = {"component": "", "assembly_frame": True, "feature": "Cyl2", "cylinder": copy.deepcopy(cylinder)}
        unmarked = {"component": "", "feature": "Cyl2", "cylinder": copy.deepcopy(cylinder)}
        mate = {
            "name": "probe__coaxial",
            "type": "concentric",
            "entity_count": 2,
            "suppressed": False,
            "error_code": 0,
            "scope": "",
            "limits": None,
            "entities": [copy.deepcopy(base), marked],
        }

        self.assertIsNotNone(_rows_for(mate, frames))
        summary = _frame_attachment({**raw, "mates": [mate]})
        self.assertEqual(summary["mates"], 1)
        self.assertEqual(summary["rank"], 4)
        self.assertTrue(any("full rank" in item["message"] for item in summary["problems"]))

        unmarked_mate = {**mate, "entities": [copy.deepcopy(base), unmarked]}
        self.assertIsNone(_rows_for(unmarked_mate, frames))
        self.assertEqual(_frame_attachment({**raw, "mates": [unmarked_mate]})["mates"], 0)

    def test_datum_owner_allowance_is_bound_to_the_proven_cluster(self) -> None:
        from description_pipeline.verification.native_discovery import _datum_owners

        self.assertEqual(_datum_owners(frozenset({"base-1"}), frozenset({"base-1"})), {"base-1", ""})
        self.assertEqual(_datum_owners(frozenset({"arm-1"}), frozenset({"base-1"})), {"arm-1"})
        self.assertEqual(_datum_owners(frozenset(), frozenset()), set())

    def test_frame_marker_requires_the_exact_empty_component(self) -> None:
        missing = object()

        def mutate(raw, payload, value):
            mate = frame_ground_mates()[0]
            for entity in mate["entities"]:
                if entity.get("assembly_frame") is True:
                    if value is missing:
                        entity.pop("component", None)
                    else:
                        entity["component"] = value
            raw["mates"].append(mate)

        for label, value in (("missing", missing), ("null", None), ("zero", 0)):
            with self.subTest(component=label):
                package = self.baseline()
                self._native(package, lambda raw, payload, value=value: mutate(raw, payload, value))
                passed, errors, _checks = self.check(package)
                self.assertFalse(passed, errors)
                self.assertTrue(
                    any(
                        error["code"] == "discovery.mate_scope"
                        for error in errors
                    ),
                    errors,
                )

    def test_frame_mate_requires_exactly_one_frame_and_one_occurrence(self) -> None:
        def three_entities(raw, payload):
            mate = frame_ground_mates()[0]
            mate["entities"].append(copy.deepcopy(mate["entities"][0]))
            raw["mates"].append(mate)

        def two_frame_entities(raw, payload):
            mate = frame_ground_mates()[0]
            mate["entities"][0].update({"component": "", "assembly_frame": True})
            raw["mates"].append(mate)

        def malformed_extra_entity(raw, payload):
            mate = frame_ground_mates()[0]
            mate["entities"].append(None)
            raw["mates"].append(mate)

        for label, mutate in (
            ("three_entities", three_entities),
            ("two_frame_entities", two_frame_entities),
            ("malformed_extra_entity", malformed_extra_entity),
        ):
            with self.subTest(case=label):
                package = self.baseline()
                self._native(package, mutate)
                passed, errors, _checks = self.check(package)
                self.assertFalse(passed, errors)
                self.assertTrue(
                    any(
                        (
                            error["code"] == "discovery.frame_attachment"
                            and "exactly one frame entity and one occurrence entity" in error["message"]
                        )
                        or error["code"] == "discovery.mate_scope"
                        for error in errors
                    ),
                    errors,
                )

    def test_frame_mate_scope_must_be_exactly_the_frozen_top(self) -> None:
        missing = object()

        def mutate(raw, payload, value):
            mate = frame_ground_mates()[0]
            if value is missing:
                mate.pop("scope", None)
            else:
                mate["scope"] = value
            raw["mates"].append(mate)

        for label, value in (("missing", missing), ("null", None), ("zero", 0), ("nested", "sub-1")):
            with self.subTest(scope=label):
                package = self.baseline()
                self._native(package, lambda raw, payload, value=value: mutate(raw, payload, value))
                passed, errors, _checks = self.check(package)
                self.assertFalse(passed, errors)
                self.assertTrue(
                    any(
                        error["code"] == "discovery.mate_scope"
                        for error in errors
                    ),
                    errors,
                )

    def test_datum_owner_must_be_an_occurrence_or_the_top_assembly(self) -> None:
        missing = object()

        def mutate(raw, payload, value):
            for datum in raw["datums"]:
                if datum["name"] == "CS_arm_link":
                    if value is missing:
                        datum.pop("owner", None)
                    else:
                        datum["owner"] = value

        for label, value in (("missing", missing), ("null", None), ("zero", 0)):
            with self.subTest(owner=label):
                package = self.baseline()
                self._native(package, lambda raw, payload, value=value: mutate(raw, payload, value))
                passed, errors, _checks = self.check(package)
                self.assertFalse(passed, errors)
                self.assertTrue(
                    any(
                        error["code"] == "discovery.datum_owner"
                        for error in errors
                    ),
                    errors,
                )

    def test_top_owned_interface_datum_binds_the_proven_base(self) -> None:
        def publish_frame(package: Path, parent: str) -> None:
            # A planted legacy second-model carrier must never steer the rebuild.
            record = json.loads((package / RECORD_FILE).read_text(encoding="utf-8"))
            record.setdefault("raw", {}).setdefault("derived", {}).setdefault("frames", []).append(
                {"name": "tool", "parent": parent, "coordinate_system": "TCP_tool"}
            )
            OracleSemanticsTests._seal(package, record)

        def add_tool_datum(raw, payload):
            raw["datums"].append({"name": "TCP_tool", "owner": "", "array": copy.deepcopy(IDENTITY)})

        def ground_base(raw, payload):
            add_tool_datum(raw, payload)
            raw["mates"].extend(frame_ground_mates())

        def ground_moving_body(raw, payload):
            add_tool_datum(raw, payload)
            raw["mates"].extend(frame_ground_mates("arm-1"))

        # Positive: the proven base attachment resolves the top-owned TCP interface.
        package = self.baseline()
        self._native(package, ground_base)
        publish_frame(package, "base_link")
        passed, errors, checks = self.check(package)
        self.assertTrue(passed, errors)
        self.assertTrue(checks["discovery.frames"]["passed"], checks["discovery.frames"])
        self.assertEqual(checks["discovery.frames"]["details"]["frames"], 1)

        # Control: without a proven attachment the ownerless channel stays closed.
        package = self.baseline()
        self._native(package, add_tool_datum)
        publish_frame(package, "base_link")
        passed, errors, _checks = self.check(package)
        self.assertFalse(passed, errors)
        self.assertTrue(
            any(
                error["code"] in {"discovery.frames", "discovery.interface_frame_rebuild"}
                and "not owned by any" in error["message"]
                for error in errors
            ),
            errors,
        )

        # Control: an attachment on a moving body never becomes the top datum owner.
        package = self.baseline()
        self._native(package, ground_moving_body)
        publish_frame(package, "arm_link")
        passed, errors, _checks = self.check(package)
        self.assertFalse(passed, errors)
        self.assertTrue(
            any(
                error["code"] == "discovery.frame_attachment"
                and "must become the CS_base_link body" in error["message"]
                for error in errors
            ),
            errors,
        )
        self.assertTrue(
            any(
                error["code"] in {"discovery.frames", "discovery.interface_frame_rebuild"}
                and "not owned by any" in error["message"]
                for error in errors
            ),
            errors,
        )

    # ------------------------------------------- circle constraint reconstruction

    def test_circle_circle_concentric_reconstructs_locked_tilts_and_free_spin(self) -> None:
        from description_pipeline.verification.native_discovery import _null_space, _rows_for, _span

        frames = {"a-1": _matrix4(IDENTITY), "b-1": _matrix4(IDENTITY)}
        mate = {
            "name": "circle_axis__coaxial",
            "type": "concentric",
            "entity_count": 2,
            "suppressed": False,
            "error_code": 0,
            "scope": "",
            "limits": None,
            "entities": [
                {
                    "component": "a-1",
                    "feature": "Edge1",
                    "circle": {"center": [0.0, 0.0, 0.02], "normal": [0.0, 0.0, 1.0], "radius": 0.003},
                },
                {
                    "component": "b-1",
                    "feature": "Edge2",
                    "circle": {"center": [0.0, 0.0, 0.05], "normal": [0.0, 0.0, 1.0], "radius": 0.004},
                },
            ],
        }

        rows = _rows_for(mate, frames)
        self.assertIsNotNone(rows)
        self.assertEqual(len(rows["rows"]), 4)
        self.assertEqual(len(_span(rows["rows"])), 4)
        # No cylinder anywhere: the constraint reconstructs, the shaft gate stays shut.
        self.assertIsNone(rows["axis"])

        free = _null_space(rows["rows"])
        self.assertEqual(len(free), 2)
        # Free: slide along the axis and spin about it.  (Whether the CAD locks the
        # spin via LockRotation is not carried by the record; the row model assumes not.)
        self.assertTrue(_in_span([0.0, 0.0, 1.0, 0.0, 0.0, 0.0], free))
        self.assertTrue(_in_span([0.0, 0.0, 0.0, 0.0, 0.0, 1.0], free))
        # Locked: tilting out of the axis is constrained.
        self.assertFalse(_in_span([0.0, 0.0, 0.0, 1.0, 0.0, 0.0], free))

    def test_circle_cylinder_concentric_keeps_the_shaft_evidence(self) -> None:
        from description_pipeline.verification.native_discovery import _independent_clusters, _rows_for

        frames = {"a-1": _matrix4(IDENTITY), "b-1": _matrix4(IDENTITY)}
        circle = {"center": [0.0, 0.0, 0.02], "normal": [0.0, 0.0, 1.0], "radius": 0.003}
        second_circle = {"center": [0.0, 0.0, 0.05], "normal": [0.0, 0.0, 1.0], "radius": 0.004}
        cylinder = {"point": [0.0, 0.0, 0.05], "direction": [0.0, 0.0, 1.0], "radius": 0.004}

        def mate(second_geometry: dict) -> dict:
            entity = {"component": "b-1", "feature": "F2"}
            entity.update(copy.deepcopy(second_geometry))
            return {
                "name": "mixed_axis__coaxial",
                "type": "concentric",
                "entity_count": 2,
                "suppressed": False,
                "error_code": 0,
                "scope": "",
                "limits": None,
                "entities": [
                    {"component": "a-1", "feature": "Edge1", "circle": copy.deepcopy(circle)},
                    entity,
                ],
            }

        mixed = _rows_for(mate({"cylinder": copy.deepcopy(cylinder)}), frames)
        self.assertIsNotNone(mixed)
        self.assertEqual(mixed["axis"], [0.0, 0.0, 1.0])
        self.assertEqual(mixed["point"], [0.0, 0.0, 0.05])

        circle_only = _rows_for(mate({"circle": copy.deepcopy(second_circle)}), frames)
        self.assertIsNotNone(circle_only)
        self.assertEqual(len(circle_only["rows"]), 4)
        self.assertIsNone(circle_only["axis"])

        # The independent pair derivation keeps the same distinction: rows from circle-only
        # mates count, but they never carry the shaft the interface gate looks for.
        components = [
            {
                "name2": name,
                "lightweight": False,
                "suppression_state": 2,
                "instance_id": name,
                "document": f"cad/{name}.SLDPRT",
                "configuration": "Default",
                "fixed": False,
                "suppressed": False,
                "transform": list(IDENTITY),
            }
            for name in ("a-1", "b-1")
        ]
        _members, pairs = _independent_clusters({"components": components, "mates": [mate({"cylinder": cylinder})]})
        self.assertEqual(pairs[("a-1", "b-1")]["axis"], [0.0, 0.0, 1.0])
        _members, pairs = _independent_clusters({"components": components, "mates": [mate({"circle": second_circle})]})
        self.assertEqual(pairs[("a-1", "b-1")]["rank"], 4)
        self.assertIsNone(pairs[("a-1", "b-1")]["axis"])

    def test_concentric_axis_entities_are_compared_in_the_assembly_frame(self) -> None:
        from description_pipeline.verification.native_discovery import _rows_for

        # b's circle is authored in a frame rotated +90° about X and translated z+0.05:
        # local normal (0,1,0) becomes the assembly axis (0,0,1); the centre maps to (0,0,0.05).
        rotated = [
            1.0,
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
            -1.0,
            0.0,
            0.0,
            1.0,
            0.0,
            0.05,
            0.0,
            0.0,
            0.0,
            1.0,
        ]
        frames = {"a-1": _matrix4(IDENTITY), "b-1": _matrix4(rotated)}

        def mate(normal: list[float]) -> dict:
            return {
                "name": "framed_axis__coaxial",
                "type": "concentric",
                "entity_count": 2,
                "suppressed": False,
                "error_code": 0,
                "scope": "",
                "limits": None,
                "entities": [
                    {
                        "component": "a-1",
                        "feature": "Edge1",
                        "circle": {"center": [0.0, 0.0, 0.02], "normal": [0.0, 0.0, 1.0], "radius": 0.003},
                    },
                    {
                        "component": "b-1",
                        "feature": "Edge2",
                        "circle": {"center": [0.0, 0.0, 0.0], "normal": normal, "radius": 0.004},
                    },
                ],
            }

        self.assertIsNotNone(_rows_for(mate([0.0, 1.0, 0.0]), frames))
        self.assertIsNone(_rows_for(mate([1.0, 0.0, 0.0]), frames))

    def test_circle_plane_coincident_locks_tilts_and_leaves_in_plane_motion(self) -> None:
        from description_pipeline.verification.native_discovery import _null_space, _rows_for, _span

        frames = {"a-1": _matrix4(IDENTITY), "b-1": _matrix4(IDENTITY)}

        def mate(plane_point: list[float], plane_normal: list[float]) -> dict:
            return {
                "name": "edge_in_plane__locate",
                "type": "coincident",
                "entity_count": 2,
                "suppressed": False,
                "error_code": 0,
                "scope": "",
                "limits": None,
                "entities": [
                    {
                        "component": "a-1",
                        "feature": "Edge1",
                        "circle": {
                            "center": [0.01, 0.02, 0.05],
                            "normal": [0.0, 0.0, 1.0],
                            "radius": 0.003,
                        },
                    },
                    {
                        "component": "b-1",
                        "feature": "Plane1",
                        "plane": {"point": plane_point, "normal": plane_normal},
                    },
                ],
            }

        rows = _rows_for(mate([0.0, 0.0, 0.05], [0.0, 0.0, -1.0]), frames)
        self.assertIsNotNone(rows)
        self.assertEqual(len(rows["rows"]), 3)
        self.assertEqual(len(_span(rows["rows"])), 3)
        self.assertIsNone(rows["axis"])

        free = _null_space(rows["rows"])
        self.assertEqual(len(free), 3)
        # Free: slide in the plane and spin about the normal; locked: both tilts.
        self.assertTrue(_in_span([1.0, 0.0, 0.0, 0.0, 0.0, 0.0], free))
        self.assertTrue(_in_span([0.0, 1.0, 0.0, 0.0, 0.0, 0.0], free))
        self.assertTrue(_in_span([0.02, -0.01, 0.0, 0.0, 0.0, 1.0], free))
        self.assertFalse(_in_span([0.0, 0.0, 0.0, 1.0, 0.0, 0.0], free))

        # Solved-state consistency: a skew plane or a centre off the plane blocks.
        self.assertIsNone(_rows_for(mate([0.0, 0.0, 0.05], [1.0, 0.0, 0.0]), frames))
        self.assertIsNone(_rows_for(mate([0.0, 0.0, 0.051], [0.0, 0.0, -1.0]), frames))

    def test_concentric_rejects_planes_and_unusable_geometry(self) -> None:
        from description_pipeline.verification.native_discovery import _rows_for

        frames = {"a-1": _matrix4(IDENTITY), "b-1": _matrix4(IDENTITY)}

        def mate(first_entity: dict, second_entity: dict) -> dict:
            return {
                "name": "probe__coaxial",
                "type": "concentric",
                "entity_count": 2,
                "suppressed": False,
                "error_code": 0,
                "scope": "",
                "limits": None,
                "entities": [first_entity, second_entity],
            }

        circle = {
            "component": "a-1",
            "feature": "Edge1",
            "circle": {"center": [0.0, 0.0, 0.02], "normal": [0.0, 0.0, 1.0], "radius": 0.003},
        }
        # A plane is not an axis-bearing entity, even with usable geometry.
        plane = {
            "component": "b-1",
            "feature": "Plane1",
            "plane": {"point": [0.0, 0.0, 0.05], "normal": [0.0, 0.0, 1.0]},
        }
        self.assertIsNone(_rows_for(mate(copy.deepcopy(circle), plane), frames))

        zero_normal = copy.deepcopy(circle)
        zero_normal["circle"]["normal"] = [0.0, 0.0, 0.0]
        cylinder = {
            "component": "b-1",
            "feature": "Cyl1",
            "cylinder": {"point": [0.0, 0.0, 0.05], "direction": [0.0, 0.0, 1.0], "radius": 0.004},
        }
        self.assertIsNone(_rows_for(mate(zero_normal, cylinder), frames))

        infinite_centre = copy.deepcopy(circle)
        infinite_centre["circle"]["center"] = [float("inf"), 0.0, 0.0]
        self.assertIsNone(_rows_for(mate(infinite_centre, copy.deepcopy(cylinder)), frames))

    def test_coincident_plane_with_missing_geometry_is_not_a_vertex(self) -> None:
        from description_pipeline.verification.native_discovery import _rows_for

        frames = {"a-1": _matrix4(IDENTITY), "b-1": _matrix4(IDENTITY)}
        plane = {
            "component": "a-1",
            "feature": "Plane1",
            "plane": {"point": [0.0, 0.0, 0.05], "normal": [0.0, 1.0, 0.0]},
        }

        def mate(second_entity: dict) -> dict:
            return {
                "name": "plane_probe__locate",
                "type": "coincident",
                "entity_count": 2,
                "suppressed": False,
                "error_code": 0,
                "scope": "",
                "limits": None,
                "entities": [copy.deepcopy(plane), second_entity],
            }

        # Regression for the M3 重合115 shape: a plane plus an entity that recorded no
        # geometry at all must fail closed, never become a vertex-on-face constraint.
        self.assertIsNone(_rows_for(mate({"component": "b-1", "feature": None}), frames))

        # Regression for the M3 重合16 shape (scope headM3.0-1): the part-side entity
        # recorded no geometry next to a plane; the scope itself is irrelevant here.
        nested = mate({"component": "headM3.0-1/PDA Camera - Parametric Parts-1", "feature": None})
        nested["scope"] = "headM3.0-1"
        self.assertIsNone(_rows_for(nested, frames))

        # Only an explicit plane-plus-point form is a constraint; a cylinder or a
        # circle must never be treated as the vertex side.
        cylinder_entity = {
            "component": "b-1",
            "feature": "Cyl1",
            "cylinder": {"point": [0.0, 0.0, 0.06], "direction": [0.0, 1.0, 0.0], "radius": 0.004},
        }
        self.assertIsNone(_rows_for(mate(cylinder_entity), frames))
        circle_entity = {
            "component": "b-1",
            "feature": "Edge1",
            "circle": {"center": [0.0, 0.0, 0.06], "normal": [0.0, 1.0, 0.0], "radius": 0.003},
        }
        # A circle opposite a plane is the supported circle-in-plane constraint (the
        # M3 重合35 change), never a vertex interpretation.
        in_plane = _rows_for(mate(circle_entity), frames)
        self.assertIsNotNone(in_plane)
        self.assertEqual(len(in_plane["rows"]), 3)

        # Malformed point records do not become vertices.
        self.assertIsNone(_rows_for(mate({"component": "b-1", "feature": "V", "point": [0.0, 0.0]}), frames))
        self.assertIsNone(_rows_for(mate({"component": "b-1", "feature": "V", "point": [0.0, 0.0, "x"]}), frames))
        self.assertIsNone(_rows_for(mate({"component": "b-1", "feature": "V", "point": None}), frames))

        # The legitimate vertex-on-face fallback still works when both sides contribute.
        vertex = _rows_for(mate({"component": "b-1", "feature": "Vertex1", "point": [0.0, 0.0, 0.06]}), frames)
        self.assertIsNotNone(vertex)
        self.assertEqual(len(vertex["rows"]), 1)


if __name__ == "__main__":
    unittest.main()
