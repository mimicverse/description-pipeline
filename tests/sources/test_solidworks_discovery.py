"""Native CAD-only discovery: generation, blocking findings and independent replay.

A fake native backend replays a recorded raw record; nothing here touches
SolidWorks.  The tests pin the generated package (source.json + discovery
record), the mandatory findings for missing native facts, and the independent
verifier that re-derives every claim from the bound record.
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

from . import _paths  # noqa: F401  (import side effect: sys.path)

from description_pipeline.sources.solidworks import discovery as discovery_module  # noqa: E402
from description_pipeline.io import PipelineError, digest, file_digest, inventory, parse_data  # noqa: E402
from tests.sources.preparation_support import prepare_native_package  # noqa: E402
from description_pipeline.sources.solidworks.discovery import (  # noqa: E402
    CONTRACT,
    DISCOVERY_SCHEMA,
    DiscoverySettings,
    _budgets,
    _clusters,
    _component_frames,
    _Clusters,
    _joint_facts,
    _identity,
    _interface_frames,
    _mate_pairs,
    _mate_rows,
    _resolve_record,
    _unit,
    SubmissionIdentity,
    derive_native_package,
    source_definition,
)
from description_pipeline.sources.solidworks.errors import CadError  # noqa: E402
from description_pipeline.sources.solidworks.native import _merge_property_scopes  # noqa: E402
from description_pipeline.sources.solidworks.native_limits import validate_native_limit  # noqa: E402
from description_pipeline.sources.solidworks.scene import _value_warnings  # noqa: E402
from description_pipeline.verification.solidworks_urdf import _carrier_snapshot  # noqa: E402
from description_pipeline.schemas import SOURCE_RECORD_SCHEMA  # noqa: E402
from description_pipeline.verification.native_discovery import (  # noqa: E402
    _Failure,
    _record_value,
    rebuild_interface_frames,
    rebuild_joint_facts,
    rebuild_rigid_bodies,
    verify_discovery,
)

from .selection_support import sealed_selection  # noqa: E402
from . import support  # noqa: E402
from description_pipeline.verification.solidworks_urdf import _native_discovery  # noqa: E402

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


def _grounding_mates() -> list[dict]:
    """The M3-shaped grounding set: concentric + coincident + parallel, rank 6.

    Every mate grounds ``base-1`` to the frozen top assembly's own frame; each
    frame entity carries the exact ``component: ""`` identity marker.
    """

    return [
        {
            "name": "base_ground_axis",
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
                    "cylinder": {"point": [0.0, 0.0, 0.0], "direction": [0.0, 0.0, 1.0], "radius": 0.006},
                },
                {
                    "component": "",
                    "assembly_frame": True,
                    "feature": "Axis1",
                    "cylinder": {"point": [0.0, 0.0, 0.0], "direction": [0.0, 0.0, 1.0], "radius": 0.006},
                },
            ],
        },
        {
            "name": "base_ground_plane",
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
                    "plane": {"point": [0.0, 0.0, 0.0], "normal": [0.0, 0.0, 1.0]},
                },
                {
                    "component": "",
                    "assembly_frame": True,
                    "feature": "Plane2",
                    "plane": {"point": [0.0, 0.0, 0.0], "normal": [0.0, 0.0, -1.0]},
                },
            ],
        },
        {
            "name": "base_ground_parallel",
            "type": "parallel",
            "entity_count": 2,
            "suppressed": False,
            "error_code": 0,
            "scope": "",
            "limits": None,
            "entities": [
                {
                    "component": "base-1",
                    "feature": "Side1",
                    "plane": {"point": [0.0, 0.0, 0.0], "normal": [0.0, 1.0, 0.0]},
                },
                {
                    "component": "",
                    "assembly_frame": True,
                    "feature": "Axis2",
                    "cylinder": {"point": [0.0, 0.0, 0.0], "direction": [1.0, 0.0, 0.0], "radius": 0.006},
                },
            ],
        },
    ]


def record() -> dict:
    """Two rigid bodies and one hinge: concentric shaft plus a coincident plane."""

    files = {
        name: hashlib.sha256(f"placeholder {name}\n".encode()).hexdigest()
        for name in ("cad/robot.SLDASM", "cad/base.SLDPRT", "cad/arm.SLDPRT")
    }
    source_selection = sealed_selection(
        files,
        "cad/robot.SLDASM",
        edges={
            "cad/robot.SLDASM": ["cad/base.SLDPRT", "cad/arm.SLDPRT"],
            "cad/base.SLDPRT": [],
            "cad/arm.SLDPRT": [],
        },
    )
    result = {
        "schema_version": DISCOVERY_SCHEMA,
        "contract": CONTRACT,
        "namespace": "dp",
        "identity": {
            "hardware_id": "nd_fixture",
            "revision": "r1",
            "owner": "mechanical-team",
            "change_summary": "neutral discovery fixture",
            "control": {"system": "handoff", "reference": "handoff-20261007"},
            "delivery_configuration": "Default",
            "main_assembly": "cad/robot.SLDASM",
            "robot_name": "nd_fixture",
        },
        "saved_configuration": "Default",
        "document_state": support.saved_document_state(),
        "components": [
            {
                "name2": "base-1",
                "instance_id": "base-1",
                "document": "cad/base.SLDPRT",
                "configuration": "Default",
                "fixed": True,
                "suppressed": False,
                "lightweight": False,
                "suppression_state": 2,
                "transform": copy.deepcopy(IDENTITY),
            },
            {
                "name2": "arm-1",
                "instance_id": "arm-1",
                "document": "cad/arm.SLDPRT",
                "configuration": "Default",
                "fixed": False,
                "suppressed": False,
                "lightweight": False,
                "suppression_state": 2,
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
                "limits": {"lower": -1.5, "upper": 1.5, "unit": "rad"},
                "native_limit": None,
                "is_advanced_mate": None,
                "is_advanced_mate_error": None,
                "is_advanced_mate_api": None,
                "entities": [
                    {
                        "component": "base-1",
                        "feature": "Cyl1",
                        "face_index": 3,
                        "cylinder": {"point": [0.0, 0.0, 0.1], "direction": [0.0, 0.0, 1.0], "radius": 0.006},
                    },
                    {
                        "component": "arm-1",
                        "feature": "Cyl2",
                        "face_index": 7,
                        "cylinder": {"point": [0.0, 0.0, 0.1], "direction": [0.0, 0.0, 1.0], "radius": 0.006},
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
                "native_limit": None,
                "is_advanced_mate": None,
                "is_advanced_mate_error": None,
                "is_advanced_mate_api": None,
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
        "assembly_mass_kg": 0.2976548667764616,
        "assembly_extent_m": 0.42,
        "properties": {
            "document": {"dp.design_budget_record": "budget.json#robot"},
            "components": {},
            "mates": {
                "shoulder_pitch_joint__coaxial": {
                    "dp.joint.axis_sign": "+1",
                    "dp.joint.drive_record": "joints/arm.json#drive",
                }
            },
        },
        "files": files,
        "source_selection": source_selection,
    }

    from .selection_support import seal_fixture_open_observation

    seal_fixture_open_observation(result)
    return result


class FakeBackend:
    def __init__(self, payload: dict):
        self.payload = payload

    def discover_native(self, frozen_source: Path, settings: dict, *, on_activity=None) -> dict:
        return support.local_native_record_payload(
            copy.deepcopy(self.payload),
            default_mate_scope=True,
        )


class DiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="sw-discovery-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.serial = 0

    # ------------------------------------------------------------------ helpers

    def _native(self, joint_record: dict | None = None) -> tuple[Path, Path]:
        self.serial += 1
        root = self.tmp / f"native{self.serial}"
        for name in ("cad/robot.SLDASM", "cad/base.SLDPRT", "cad/arm.SLDPRT"):
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(f"placeholder {name}\n".encode())
        records = self.tmp / "records"
        path = records / "joints" / "arm.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                joint_record
                or {"limits": {"lower": -1.5, "upper": 1.5}, "drive": {"effort": 6.0, "velocity": 2.0}}
            ),
            encoding="utf-8",
        )
        (records / "budget.json").write_text(
            json.dumps({"robot": {"expected_mass_kg": [0.28, 0.32], "expected_extent_m": [0.35, 0.5]}}),
            encoding="utf-8",
        )
        return root, records

    def _prepare(self, *, mutate=None, settings=None, joint_record=None):
        source, records = self._native(joint_record)
        payload = record()
        if mutate is not None:
            mutate(payload)
        output = self.tmp / f"prepared{self.serial}"
        result = prepare_native_package(
            source,
            output,
            run_id="run-1",
            backend=FakeBackend(payload),
            settings=settings or DiscoverySettings(record_roots=(records,)),
        )
        return result, source, output

    def _codes(self, result) -> list[str]:
        self.assertFalse(result.passed)
        return [finding["code"] for finding in result.findings]

    def _deliver(self, output: Path) -> Path:
        """The target-tree delivery the frozen producer conversion yields."""

        self.serial += 1
        return support.native_discovery_delivery(self.tmp / f"delivery{self.serial}", output)

    def _verify(self, output: Path) -> dict:
        return verify_discovery(self._deliver(output))

    def _verify_mutated_record(self, output: Path, mutate) -> dict:
        payload = json.loads((output / "discovery/native-discovery.json").read_text(encoding="utf-8"))
        canonical = copy.deepcopy(payload)
        canonical["raw"] = support.native_consumer_view(payload["raw"])
        mutate(canonical)
        payload["raw"] = support.local_native_record_payload(
            canonical["raw"],
            default_mate_scope=True,
        )
        self.serial += 1
        delivery = support.native_discovery_delivery(
            self.tmp / f"mutated-delivery{self.serial}",
            output,
            record=payload,
        )
        return verify_discovery(delivery)

    def _rewrite_delivery_controlled_record(self, delivery: Path, mutate) -> None:
        discovery_path = delivery / "input/discovery/native-discovery.json"
        discovery = json.loads(discovery_path.read_text(encoding="utf-8"))
        entries = [
            item
            for item in discovery["records"]
            if item.get("file") == "joints/arm.json"
        ]
        self.assertTrue(entries)
        package_file = str(entries[0]["package_file"])
        record_path = delivery / "input" / package_file
        value = json.loads(record_path.read_text(encoding="utf-8"))
        mutate(value)
        content = json.dumps(value, indent=2).encode("utf-8")
        record_path.write_bytes(content)
        checksum = hashlib.sha256(content).hexdigest()
        for entry in entries:
            entry["sha256"] = checksum
            entry["source_sha256"] = checksum
        discovery_path.write_text(json.dumps(discovery, indent=2) + "\n", encoding="utf-8")

        source_path = delivery / "input/source.json"
        source = json.loads(source_path.read_text(encoding="utf-8"))
        for row in source["identity"]["records"]:
            if str(row.get("reference") or "").startswith("joints/arm.json#"):
                row["file_sha256"] = checksum
        source["source_revision_id"] = digest(source["identity"])
        source_path.write_text(json.dumps(source, indent=2) + "\n", encoding="utf-8")
        support.seal_delivery(delivery)

    def test_source_definition_rejects_malformed_raw_boundaries(self) -> None:
        result, _source, output = self._prepare()
        self.assertTrue(result.passed, result.findings)
        delivery = self._deliver(output)
        discovery_path = (
            delivery / "input/discovery/native-discovery.json"
        )
        baseline = json.loads(discovery_path.read_text(encoding="utf-8"))

        def set_raw(key, value):
            def mutate(payload):
                payload["raw"][key] = copy.deepcopy(value)

            return mutate

        malformed = (
            ("properties-list", set_raw("properties", [])),
            (
                "document-properties-false",
                lambda payload: payload["raw"]["properties"].__setitem__(
                    "document",
                    False,
                ),
            ),
            (
                "component-properties-zero",
                lambda payload: payload["raw"]["properties"].setdefault(
                    "components",
                    {},
                ).__setitem__("malformed-component", 0),
            ),
            ("identity-list", set_raw("identity", [])),
            (
                "identity-numeric",
                lambda payload: payload["raw"].setdefault(
                    "identity",
                    {},
                ).__setitem__("hardware_id", 0),
            ),
            (
                "identity-control-list",
                lambda payload: payload["raw"].setdefault(
                    "identity",
                    {},
                ).__setitem__("control", []),
            ),
        )
        for label, mutate in malformed:
            with self.subTest(label=label):
                payload = copy.deepcopy(baseline)
                mutate(payload)
                discovery_path.write_text(
                    json.dumps(payload, indent=2) + "\n",
                    encoding="utf-8",
                )
                with self.assertRaises(CadError):
                    source_definition(
                        delivery,
                        discovery_name=(
                            "input/discovery/native-discovery.json"
                        ),
                        cad_prefix="input/cad/",
                        records_prefix="input/",
                    )

        for label, mutate in (
            (
                "top-level-identity-list",
                lambda payload: payload.__setitem__("identity", []),
            ),
            (
                "top-level-derivation-string",
                lambda payload: payload.__setitem__("derivation", ""),
            ),
            (
                "record-entry-not-object",
                lambda payload: payload.setdefault(
                    "records",
                    [],
                ).append(False),
            ),
        ):
            with self.subTest(label=label):
                payload = copy.deepcopy(baseline)
                mutate(payload)
                discovery_path.write_text(
                    json.dumps(payload, indent=2) + "\n",
                    encoding="utf-8",
                )
                with self.assertRaises(PipelineError):
                    source_definition(
                        delivery,
                        discovery_name=(
                            "input/discovery/native-discovery.json"
                        ),
                        cad_prefix="input/cad/",
                        records_prefix="input/",
                    )

        for label, update in (
            ("lightweight-incoherent", {"lightweight": None}),
            ("suppression-state", {"suppression_state": 5}),
        ):
            with self.subTest(label=label):
                payload = copy.deepcopy(baseline)
                component = payload["raw"]["components"][0]
                if label == "lightweight-incoherent":
                    update = {
                        "lightweight": not bool(component["lightweight"])
                    }
                component.update(update)
                discovery_path.write_text(
                    json.dumps(payload, indent=2) + "\n",
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(
                    PipelineError,
                    "suppression_state",
                ):
                    source_definition(
                        delivery,
                        discovery_name=(
                            "input/discovery/native-discovery.json"
                        ),
                        cad_prefix="input/cad/",
                        records_prefix="input/",
                    )

    def test_native_preparation_forwards_the_guarded_activity_sink(self):
        class ActivityBackend:
            def __init__(self):
                self.sink = None

            def discover_native(self, frozen_source, settings, *, on_activity=None):
                self.sink = on_activity
                if on_activity is not None:
                    on_activity({"phase": "discover", "action": "read_components", "completed": 1, "total": 2})
                return support.local_native_record_payload(
                    copy.deepcopy(record()),
                    default_mate_scope=True,
                )

        source, records = self._native()
        received = []

        def observer(item):
            received.append(item)
            raise RuntimeError("observer failures must never propagate")

        backend = ActivityBackend()
        prepare_native_package(
            source,
            self.tmp / "prepared-activity",
            run_id="run-1",
            backend=backend,
            settings=DiscoverySettings(record_roots=(records,)),
            on_activity=observer,
        )
        self.assertEqual(received[0]["action"], "read_components")

        plain = ActivityBackend()
        prepare_native_package(
            source,
            self.tmp / "prepared-no-activity",
            run_id="run-1",
            backend=plain,
            settings=DiscoverySettings(record_roots=(records,)),
        )
        self.assertIsNone(plain.sink)

    # ----------------------------------------------------------------- positive

    def test_nonfinite_raw_evidence_is_rejected_with_a_json_safe_field_path(self):
        for value in (float("inf"), float("-inf"), float("nan")):

            def nonfinite(payload, value=value):
                payload["components"][0]["transform"][3] = value

            with self.subTest(value=value), self.assertRaises(CadError) as caught:
                self._prepare(mutate=nonfinite)
            error = caught.exception
            self.assertEqual(error.code, "cad_native_record_invalid")
            self.assertEqual(
                error.detail["field"],
                "raw.components[0].transform_raw[9]",
            )
            json.dumps(error.to_dict(), allow_nan=False)

    def test_prepare_and_verify_refuse_a_nonrigid_component_transform(self):
        def mutate(payload):
            raw = payload.get("raw", payload)
            raw["components"][0]["transform"][0] = 2.0

        with self.assertRaises(PipelineError):
            self._prepare(mutate=mutate)

        baseline, _source, output = self._prepare()
        self.assertTrue(baseline.passed, baseline.findings)
        report = self._verify_mutated_record(output, mutate)
        self.assertFalse(report["passed"])

    def test_source_definition_requires_local_raw_projection(self) -> None:
        baseline, _source, output = self._prepare()
        self.assertTrue(baseline.passed, baseline.findings)
        delivery = self._deliver(output)
        discovery_path = (
            delivery
            / "input/discovery/native-discovery.json"
        )
        payload = json.loads(
            discovery_path.read_text(encoding="utf-8")
        )
        canonical = support.native_consumer_view(payload["raw"])
        component = canonical["components"][0]
        component["transform"] = component.pop("transform_raw")
        payload["raw"] = canonical
        discovery_path.write_text(
            json.dumps(payload, indent=2) + "\n",
            encoding="utf-8",
        )
        support.seal_delivery(delivery)
        with self.assertRaisesRegex(
            PipelineError,
            "raw cannot be projected",
        ):
            source_definition(
                delivery,
                discovery_name="input/discovery/native-discovery.json",
                cad_prefix="input/cad/",
                records_prefix="input/",
            )

    def test_prepare_and_verify_refuse_a_nonrigid_datum(self):
        def mutate(payload):
            raw = payload.get("raw", payload)
            datum = next(item for item in raw["datums"] if item["name"] == "CS_base_link")
            datum["array"][0] = 2.0

        with self.assertRaises(PipelineError):
            self._prepare(mutate=mutate)

        baseline, _source, output = self._prepare()
        self.assertTrue(baseline.passed, baseline.findings)
        report = self._verify_mutated_record(output, mutate)
        self.assertFalse(report["passed"])
        self.assertIn(
            "discovery.local_transform",
            {item["code"] for item in report["errors"]},
        )

    def test_prepare_and_verify_refuse_lightweight_occurrences(self):
        def mutate(payload):
            raw = payload.get("raw", payload)
            raw["components"][0].update(
                suppression_state=1,
                suppressed=False,
                lightweight=True,
            )

        result, _source, _output = self._prepare(mutate=mutate)
        self.assertIn("discovery.suppression_lightweight", self._codes(result))

        baseline, _source, output = self._prepare()
        self.assertTrue(baseline.passed, baseline.findings)
        report = self._verify_mutated_record(output, mutate)
        self.assertFalse(report["passed"])
        self.assertIn(
            "discovery.suppression_lightweight",
            {item["code"] for item in report["errors"]},
        )

    def test_prepare_and_verify_refuse_nonpositive_cylinder_radii(self):
        def mutate(payload):
            raw = payload.get("raw", payload)
            for mate in raw["mates"]:
                for entity in mate["entities"]:
                    cylinder = entity.get("cylinder")
                    if isinstance(cylinder, dict):
                        cylinder["radius"] = -0.006

        result, _source, _output = self._prepare(mutate=mutate)
        self.assertIn("discovery.mate_entities_unsupported", self._codes(result))

        baseline, _source, output = self._prepare()
        self.assertTrue(baseline.passed, baseline.findings)
        report = self._verify_mutated_record(output, mutate)
        self.assertFalse(report["passed"])
        self.assertIn("discovery.graph", {item["code"] for item in report["errors"]})

    def test_large_finite_axis_normalizes_without_squared_norm_overflow(self):
        axis = _unit([1e308, 1e308, 0.0])
        self.assertAlmostEqual(axis[0], 2**-0.5)
        self.assertAlmostEqual(axis[1], 2**-0.5)
        self.assertEqual(axis[2], 0.0)

    def test_positive_package_is_generated_and_independently_verified(self):
        result, source, output = self._prepare()
        self.assertTrue(result.passed, result.findings)
        self.assertEqual(result.hardware_id, "nd_fixture")
        self.assertEqual(result.revision, "r1")
        self.assertRegex(result.handoff_sha256, r"^[0-9a-f]{64}$")
        self.assertNotEqual(result.handoff_sha256, result.prepared_sha256)
        self.assertEqual(result.prepared_sha256, digest(inventory(output)))
        self.assertTrue((output / "source.json").is_file())
        self.assertFalse((output / "robot.yaml").exists())
        self.assertFalse((output / "cad-revision.json").exists())
        record = json.loads((output / "source.json").read_text(encoding="utf-8"))
        self.assertEqual(record["schema_version"], SOURCE_RECORD_SCHEMA)
        self.assertEqual(record["identity"]["kind"], "source-revision")
        self.assertEqual(record["identity"]["hardware_id"], "nd_fixture")
        self.assertEqual(record["identity"]["handoff_digest"], result.handoff_sha256)
        self.assertEqual(record["metadata"]["revision"], "r1")
        self.assertEqual(record["selection"]["hardware_source"], "cad")
        payload = json.loads((output / "discovery" / "native-discovery.json").read_text(encoding="utf-8"))
        self.assertEqual(payload["generator"], "native-discovery")
        self.assertEqual(payload["contract"], CONTRACT)
        self.assertEqual(payload["handoff_sha256"], result.handoff_sha256)
        self.assertEqual(result.discovery_sha256, file_digest(output / "discovery/native-discovery.json"))
        # The native engineering files are copied through unmodified.
        for name in ("cad/robot.SLDASM", "cad/base.SLDPRT", "cad/arm.SLDPRT"):
            self.assertEqual((output / name).read_bytes(), (source / name).read_bytes())
        bodies = rebuild_rigid_bodies(payload)
        self.assertEqual([body["name"] for body in bodies], ["arm_link", "base_link"])
        self.assertEqual(
            {body["name"]: body["frame"]["coordinate_system"] for body in bodies},
            {"arm_link": "CS_arm_link", "base_link": "CS_base_link"},
        )
        joint = rebuild_joint_facts(payload, output, purpose="engineering")[0]
        self.assertEqual(joint["name"], "shoulder_pitch_joint")
        self.assertEqual(joint["type"], "revolute")
        self.assertEqual((joint["parent"], joint["child"]), ("base_link", "arm_link"))
        self.assertEqual(joint["axis"], [0.0, 0.0, 1.0])
        self.assertEqual(joint["limits"], {"lower": -1.5, "upper": 1.5, "effort": 6.0, "velocity": 2.0})
        self.assertEqual(joint["axis_reference"]["component"], "base-1")
        self.assertEqual(joint["axis_reference"]["feature_name"], "Cyl1")
        self.assertEqual(joint["limit_source"], "native")
        self.assertIsNone(joint["limit_evidence"])
        budget = json.loads((output / payload["budget"]["package_file"]).read_text(encoding="utf-8"))
        self.assertEqual(budget["robot"]["expected_mass_kg"], [0.28, 0.32])
        self.assertEqual(budget["robot"]["expected_extent_m"], [0.35, 0.5])
        report = self._verify(output)
        self.assertTrue(report["passed"], report["errors"])
        self.assertEqual(report["joints"], 1)

    def test_aligned_status_does_not_suppress_mate_unresolved_warning(self):
        original = discovery_module.canonicalize_axis

        def aligned_status(*args, **kwargs):
            decision = original(*args, **kwargs)
            decision["status"] = "local_class_aligned"
            return decision

        with mock.patch.object(
            discovery_module,
            "canonicalize_axis",
            side_effect=aligned_status,
        ):
            result, _source, _output = self._prepare()

        self.assertTrue(result.passed, result.findings)
        finding = next(
            item
            for item in result.findings
            if item["code"] == "direction.semantics_unresolved"
        )
        self.assertEqual(finding["detail"]["status"], "local_class_aligned")
        self.assertEqual(finding["detail"]["delivered_local_axis"], [0.0, 0.0, 1.0])

    def test_duplicate_controlled_records_block_regardless_of_content_or_root_order(self):
        _source, records = self._native()
        other = self.tmp / "other-records"
        other.mkdir()
        for content in ((records / "budget.json").read_bytes(), b'{"robot":{"expected_mass_kg":[9,10]}}'):
            (other / "budget.json").write_bytes(content)
            for roots in ((records, other), (other, records)):
                with self.subTest(content=content, roots=roots):
                    result, _source, _output = self._prepare(settings=DiscoverySettings(record_roots=roots))
                    self.assertIn("discovery.record_ambiguous", self._codes(result))
                    finding = next(f for f in result.findings if f["code"] == "discovery.record_ambiguous")
                    self.assertEqual(
                        finding["detail"],
                        {
                            "reference": "budget.json#robot",
                            "candidates": [
                                {"root_index": 0, "file": "budget.json"},
                                {"root_index": 1, "file": "budget.json"},
                            ],
                        },
                    )

    def test_invalid_duplicate_does_not_fall_back_to_a_readable_record(self):
        _source, records = self._native()
        other = self.tmp / "other-records"
        other.mkdir()
        (other / "budget.json").write_bytes(b"not JSON")
        result, _source, _output = self._prepare(settings=DiscoverySettings(record_roots=(other, records)))
        self.assertIn("discovery.record_ambiguous", self._codes(result))

    def test_repeated_identical_root_is_one_authority(self):
        _source, records = self._native()
        result, _source, output = self._prepare(settings=DiscoverySettings(record_roots=(records, records / ".")))
        self.assertTrue(result.passed, result.findings)
        self.assertTrue(self._verify(output)["passed"])

    def test_hardlinked_files_in_distinct_roots_are_competing_authorities(self):
        _source, records = self._native()
        other = self.tmp / "other-records"
        other.mkdir()
        (other / "budget.json").hardlink_to(records / "budget.json")
        result, _source, _output = self._prepare(settings=DiscoverySettings(record_roots=(records, other)))
        self.assertIn("discovery.record_ambiguous", self._codes(result))

    def test_handoff_record_does_not_supply_controlled_authority(self):
        source, records = self._native()
        shutil.copytree(records, source / "records")
        result = prepare_native_package(
            source,
            self.tmp / "prepared-no-library",
            "run-1",
            backend=FakeBackend(record()),
            settings=DiscoverySettings(),
        )
        self.assertIn("discovery.record_missing", self._codes(result))

    def test_invalid_record_encoding_retains_a_blocking_finding(self):
        source, records = self._native()
        (records / "budget.json").write_bytes(b"\xff\xfe")
        result = prepare_native_package(
            source,
            self.tmp / "prepared-invalid-encoding",
            "run-1",
            backend=FakeBackend(record()),
            settings=DiscoverySettings(record_roots=(records,)),
        )
        self.assertIn("discovery.record_unreadable", self._codes(result))

    def test_production_and_independent_readers_reject_same_invalid_bytes(self):
        cases = {
            "invalid_utf8": b'{"limits":{"lower":0.0,"upper":0.0},"note":"\xff"}',
            "duplicate_key": b'{"limits":{"lower":0.0,"lower":-1.0,"upper":0.0}}',
        }
        for label, payload in cases.items():
            with self.subTest(case=label):
                root = self.tmp / label
                records = root / "source-records"
                original = records / "joints/arm.json"
                original.parent.mkdir(parents=True)
                original.write_bytes(payload)
                findings = []
                self.assertIsNone(
                    _resolve_record(
                        DiscoverySettings(record_roots=(records,)),
                        "joints/arm.json#limits",
                        findings,
                        "joint:test",
                    )
                )
                self.assertIn(
                    "discovery.record_unreadable",
                    {item["code"] for item in findings},
                )

                target = root / "records/record.txt"
                target.parent.mkdir(parents=True)
                target.write_bytes(payload)
                entry = {
                    "file": "joints/arm.json",
                    "package_file": "records/record.txt",
                    "sha256": hashlib.sha256(payload).hexdigest(),
                }
                with self.assertRaises(PipelineError):
                    parse_data(payload, Path("joints/arm.json"))
                with self.assertRaises(_Failure) as raised:
                    _record_value(root, entry, "limits")
                self.assertEqual(raised.exception.code, "discovery.record")

    def test_discovery_binding_rejects_duplicate_json_keys(self):
        result, _source, output = self._prepare()
        self.assertTrue(result.passed, result.findings)
        delivery = self._deliver(output)
        discovery_path = delivery / "input/discovery/native-discovery.json"
        text = discovery_path.read_text(encoding="utf-8")
        marker = '"schema_version":' + json.dumps(DISCOVERY_SCHEMA)
        self.assertIn(marker, text)
        discovery_path.write_text(
            text.replace(marker, marker + "," + marker, 1),
            encoding="utf-8",
        )
        support.seal_delivery(delivery)

        report = verify_discovery(delivery)

        self.assertFalse(report["passed"])
        failure = next(
            item for item in report["errors"] if item["code"] == "discovery.binding"
        )
        self.assertIn("Duplicate input key", failure["message"])

    def test_independent_verify_rejects_ambiguous_embedded_record(self):
        record_value = {
            "limits": {"lower": 0.0, "upper": 0.0},
            "drive": {"effort": 6.0, "velocity": 2.0},
        }

        def baseline_mutation(payload):
            payload["mates"][0]["limits"] = None
            payload["properties"]["mates"][
                "shoulder_pitch_joint__coaxial"
            ]["dp.joint.limits_record"] = "joints/arm.json#limits"

        result, _source, output = self._prepare(
            mutate=baseline_mutation,
            joint_record=record_value,
        )
        self.assertTrue(result.passed, result.findings)
        delivery = self._deliver(output)
        discovery_path = delivery / "input/discovery/native-discovery.json"
        discovery = json.loads(discovery_path.read_text(encoding="utf-8"))
        entry = next(
            item
            for item in discovery["records"]
            if item.get("file") == "joints/arm.json"
        )
        record_path = delivery / "input" / entry["package_file"]
        original = record_path.read_text(encoding="utf-8")
        changed = original.replace(
            '"lower": 0.0',
            '"lower": 0.0, "lower": -1.0',
            1,
        )
        if changed == original:
            changed = original.replace(
                '"lower":0.0',
                '"lower":0.0,"lower":-1.0',
                1,
            )
        self.assertNotEqual(changed, original)
        payload = changed.encode("utf-8")
        with self.assertRaises(PipelineError):
            parse_data(payload, Path(entry["file"]))
        record_path.write_bytes(payload)
        checksum = hashlib.sha256(payload).hexdigest()
        for item in discovery["records"]:
            if item.get("file") == entry["file"]:
                item["sha256"] = checksum
                item["source_sha256"] = checksum
        discovery_path.write_text(
            json.dumps(discovery, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        source_path = delivery / "input/source.json"
        source = json.loads(source_path.read_text(encoding="utf-8"))
        for item in source["identity"]["records"]:
            if item.get("reference", "").startswith(entry["file"] + "#"):
                item["file_sha256"] = checksum
        source["source_revision_id"] = digest(source["identity"])
        source_path.write_text(
            json.dumps(source, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        support.seal_delivery(delivery)

        report = verify_discovery(delivery)

        self.assertFalse(report["passed"])
        self.assertTrue(
            any(
                item["code"] == "discovery.record"
                and "Duplicate input key" in item["message"]
                for item in report["errors"]
            )
        )

    def test_missing_file_in_one_root_does_not_hide_a_unique_record(self):
        _source, records = self._native()
        empty = self.tmp / "empty-records"
        empty.mkdir()
        result, _source, output = self._prepare(settings=DiscoverySettings(record_roots=(empty, records)))
        self.assertTrue(result.passed, result.findings)
        self.assertTrue(self._verify(output)["passed"])

    def test_versioned_references_coexist_without_changing_the_library(self):
        _source, records = self._native()
        for version, bounds in (("r2", [0.28, 0.32]), ("r3", [9.0, 10.0])):
            path = records / "nd_fixture" / version / "budget.json"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({"robot": {"expected_mass_kg": bounds, "expected_extent_m": [0.35, 0.5]}}))
        for version, bounds in (("r2", [0.28, 0.32]), ("r3", [9.0, 10.0])):
            with self.subTest(version=version):

                def mutate(payload, version=version):
                    payload["properties"]["document"]["dp.design_budget_record"] = (
                        f"nd_fixture/{version}/budget.json#robot"
                    )

                result, _source, output = self._prepare(
                    mutate=mutate, settings=DiscoverySettings(record_roots=(records,))
                )
                self.assertTrue(result.passed, result.findings)
                payload = json.loads((output / "discovery" / "native-discovery.json").read_text(encoding="utf-8"))
                budget = json.loads((output / payload["budget"]["package_file"]).read_text(encoding="utf-8"))
                self.assertEqual(budget["robot"]["expected_mass_kg"], bounds)
                self.assertTrue(self._verify(output)["passed"])

    def test_record_bytes_are_parsed_hashed_and_embedded_from_one_read(self):
        _source, records = self._native()
        original = b"\xef\xbb\xbf" + (records / "budget.json").read_bytes()
        replacement = b'{"robot":{"expected_mass_kg":[9,10],"expected_extent_m":[0.35,0.5]}}'

        def read_once(content, path):
            result = parse_data(content, path)
            if path.name == "budget.json":
                path.write_bytes(replacement)
            return result

        # _prepare creates the native fixture; keep the BOM on the exact read.
        source, records = self._native()
        (records / "budget.json").write_bytes(original)
        output = self.tmp / "prepared-one-read"
        with mock.patch("description_pipeline.sources.solidworks.discovery.parse_data", side_effect=read_once):
            result = prepare_native_package(
                source,
                output,
                "run-1",
                backend=FakeBackend(record()),
                settings=DiscoverySettings(record_roots=(records,)),
            )
        self.assertTrue(result.passed, result.findings)
        payload = json.loads((output / "discovery/native-discovery.json").read_text())
        budget = next(item for item in payload["records"] if item["reference"] == "budget.json#robot")
        self.assertEqual((output / budget["package_file"]).read_bytes(), original)
        self.assertEqual(budget["sha256"], file_digest(output / budget["package_file"]))
        budget_entry = json.loads((output / payload["budget"]["package_file"]).read_text(encoding="utf-8-sig"))
        self.assertEqual(budget_entry["robot"]["expected_mass_kg"], [0.28, 0.32])
        self.assertEqual((records / "budget.json").read_bytes(), replacement)
        self.assertTrue(self._verify(output)["passed"])

    def test_source_revision_records_must_bind_embedded_records(self):
        result, _source, output = self._prepare()
        self.assertTrue(result.passed, result.findings)
        payload = json.loads((output / "discovery/native-discovery.json").read_text(encoding="utf-8"))
        self.assertTrue(payload["records"])
        source_path = output / "source.json"
        source = json.loads(source_path.read_text(encoding="utf-8"))
        self.assertTrue(source["identity"]["records"])
        source["identity"]["records"] = []
        source["source_revision_id"] = digest(source["identity"])
        source_path.write_text(json.dumps(source), encoding="utf-8")
        delivery = self._deliver(output)
        report = verify_discovery(delivery)
        self.assertFalse(report["passed"])
        self.assertIn("discovery.record_binding", {item["code"] for item in report["errors"]}, report["errors"])

    def test_rigid_accessory_preserves_the_joint_between_solved_bodies(self):
        def mutate(payload):
            accessory = copy.deepcopy(payload["components"][0])
            accessory.update({"name2": "a_mount-1", "instance_id": "a_mount-1"})
            payload["components"].append(accessory)
            from .selection_support import seal_fixture_open_observation

            seal_fixture_open_observation(payload)
            payload["mates"].append(
                {
                    "name": "base_mount_lock",
                    "type": "lock",
                    "entity_count": 2,
                    "suppressed": False,
                    "error_code": 0,
                    "limits": None,
                    "entities": [{"component": "base-1"}, {"component": "a_mount-1"}],
                }
            )

        result, _source, output = self._prepare(mutate=mutate)
        self.assertTrue(result.passed, result.findings)
        payload = json.loads((output / "discovery" / "native-discovery.json").read_text(encoding="utf-8"))
        base = next(body for body in rebuild_rigid_bodies(payload) if body["name"] == "base_link")
        self.assertEqual(base["components"], ["a_mount-1", "base-1"])
        self.assertEqual(rebuild_joint_facts(payload, output, purpose="engineering")[0]["parent"], "base_link")
        report = self._verify(output)
        self.assertTrue(report["passed"], report["errors"])

    def test_assembly_frame_mate_is_blocked_explicitly_and_never_silently_dropped(self):
        def ground_mate() -> dict:
            return {
                "name": "base_ground_pin",
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
                        "plane": {"point": [0.0, 0.0, 0.1], "normal": [0.0, 0.0, -1.0]},
                    },
                    {
                        "component": "",
                        "assembly_frame": True,
                        "feature": "Plane2",
                        "plane": {"point": [0.0, 0.0, 0.1], "normal": [0.0, 0.0, 1.0]},
                    },
                ],
            }

        def mutate(payload):
            payload["mates"].append(ground_mate())

        result, _source, _output = self._prepare(mutate=mutate)
        codes = [finding["code"] for finding in result.findings]
        self.assertIn("discovery.frame_attachment", codes)
        self.assertFalse(result.passed)
        self.assertNotIn("discovery.component_transform_missing", codes)
        rank = next(finding for finding in result.findings if "full rank" in finding["message"])
        self.assertEqual(rank["detail"]["rank"], 3)

        findings: list[dict] = []
        with_mate = _clusters({**record(), "mates": [*record()["mates"], ground_mate()]}, findings)
        control = _clusters(record(), [])
        self.assertEqual(with_mate.members, control.members)
        self.assertEqual(set(with_mate.pairs), set(control.pairs))
        self.assertIsNone(with_mate.frame)
        self.assertNotIn("", with_mate.of)
        self.assertTrue(any(item["code"] == "discovery.frame_attachment" for item in findings))

    def test_mate_rows_resolve_the_assembly_frame_as_identity(self):
        mate = {
            "name": "base_ground_pin",
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
                    "plane": {"point": [0.0, 0.0, 0.1], "normal": [0.0, 0.0, -1.0]},
                },
                {
                    "component": "",
                    "assembly_frame": True,
                    "feature": "Plane2",
                    "plane": {"point": [0.0, 0.0, 0.1], "normal": [0.0, 0.0, 1.0]},
                },
            ],
        }
        findings: list[dict] = []
        frames = _component_frames(record(), findings)
        self.assertFalse(findings)
        rows = _mate_rows(mate, frames, findings, "mate:base_ground_pin")
        self.assertIsNotNone(rows)
        self.assertFalse(findings)
        self.assertTrue(rows["rows"])

    def test_unmarked_empty_component_is_not_silently_the_assembly_frame(self):
        mate = {
            "name": "unmarked_empty",
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
                    "plane": {"point": [0.0, 0.0, 0.1], "normal": [0.0, 0.0, -1.0]},
                },
                {
                    "component": "",
                    "feature": "Plane2",
                    "plane": {"point": [0.0, 0.0, 0.1], "normal": [0.0, 0.0, 1.0]},
                },
            ],
        }
        findings: list[dict] = []
        frames = _component_frames(record(), findings)
        rows = _mate_rows(mate, frames, findings, "mate:unmarked_empty")
        self.assertIsNone(rows)
        self.assertTrue(any(item["code"] == "discovery.component_transform_missing" for item in findings))
        self.assertFalse(any(item["code"] == "discovery.frame_attachment" for item in findings))

    def test_top_frame_attachment_grounds_the_base_and_binds_ownerless_datums(self):
        def mutate(payload):
            payload["mates"].extend(_grounding_mates())
            payload["datums"][0] = {"name": "CS_base_link", "owner": "", "array": copy.deepcopy(IDENTITY)}
            payload["datums"].append({"name": "SCS_tool_flange", "owner": "", "array": _translated(0.0, 0.0, 0.2)})

        findings: list[dict] = []
        clusters = _clusters({**record(), "mates": [*record()["mates"], *_grounding_mates()]}, findings)
        self.assertFalse(findings, findings)
        self.assertIsNotNone(clusters.frame)
        self.assertEqual(clusters.frame.components, ("base-1",))
        self.assertEqual(clusters.frame.members, frozenset({"base-1"}))
        self.assertEqual(clusters.frame.rank, 6)

        result, _source, output = self._prepare(mutate=mutate)
        self.assertTrue(result.passed, result.findings)
        payload = json.loads((output / "discovery" / "native-discovery.json").read_text(encoding="utf-8"))
        bodies = {body["name"]: body for body in rebuild_rigid_bodies(payload)}
        self.assertEqual(bodies["base_link"]["components"], ["base-1"])
        self.assertEqual(bodies["base_link"]["frame"]["coordinate_system"], "CS_base_link")
        self.assertEqual(bodies["arm_link"]["components"], ["arm-1"])
        frames = {frame["name"]: frame for frame in rebuild_interface_frames(payload)}
        self.assertEqual(
            frames["tool_flange_frame"],
            {
                "id": "tool_flange_frame",
                "name": "tool_flange_frame",
                "parent": "base_link",
                "coordinate_system": "SCS_tool_flange",
                "source_name": "SCS_tool_flange",
            },
        )
        joints = rebuild_joint_facts(payload, output, purpose="engineering")
        self.assertEqual(len(joints), 1)
        self.assertEqual((joints[0]["parent"], joints[0]["child"]), ("base_link", "arm_link"))
        # The grounding mates add no joint and never rewrite the raw evidence.
        raw = json.loads((output / "discovery" / "native-discovery.json").read_text(encoding="utf-8"))["raw"]
        self.assertEqual([datum["name"] for datum in raw["datums"]], ["CS_base_link", "CS_arm_link", "SCS_tool_flange"])
        self.assertEqual([datum["owner"] for datum in raw["datums"]], ["", "arm-1", ""])
        frame_entity = next(
            entity
            for mate in raw["mates"]
            if mate["name"] == "base_ground_parallel"
            for entity in mate["entities"]
            if entity.get("assembly_frame")
        )
        self.assertEqual(frame_entity["reference_name"], "assembly_root")
        self.assertNotIn("component", frame_entity)

    def test_frame_attachment_must_reach_exactly_one_rigid_cluster(self):
        def mutate(payload):
            mates = _grounding_mates()
            mates.append(
                {
                    "name": "arm_ground_axis",
                    "type": "concentric",
                    "entity_count": 2,
                    "suppressed": False,
                    "error_code": 0,
                    "scope": "",
                    "limits": None,
                    "entities": [
                        {
                            "component": "arm-1",
                            "feature": "Cyl2",
                            "cylinder": {
                                "point": [0.0, 0.0, 0.1],
                                "direction": [0.0, 0.0, 1.0],
                                "radius": 0.006,
                            },
                        },
                        {
                            "component": "",
                            "assembly_frame": True,
                            "feature": "Axis1",
                            "cylinder": {"point": [0.0, 0.0, 0.1], "direction": [0.0, 0.0, 1.0], "radius": 0.006},
                        },
                    ],
                }
            )
            payload["mates"].extend(mates)

        result, _source, _output = self._prepare(mutate=mutate)
        codes = self._codes(result)
        self.assertIn("discovery.frame_attachment", codes)
        self.assertIn("direction.semantics_unresolved", codes)
        finding = next(item for item in result.findings if item["code"] == "discovery.frame_attachment")
        self.assertIn("exactly one rigid cluster", finding["message"])
        self.assertEqual(finding["detail"]["components"], ["arm-1", "base-1"])
        self.assertEqual(len(finding["detail"]["clusters"]), 2)

    def test_frame_entity_identity_is_the_exact_empty_string(self):
        for component in (None, 0):

            def mutate(payload, component=component):
                mates = _grounding_mates()
                mates[2]["entities"][1] = {
                    "component": component,
                    "assembly_frame": True,
                    "feature": "Axis2",
                    "cylinder": {"point": [0.0, 0.0, 0.0], "direction": [1.0, 0.0, 0.0], "radius": 0.006},
                }
                payload["mates"].extend(mates)

            with (
                self.subTest(component=component),
                self.assertRaises(PipelineError),
            ):
                self._prepare(mutate=mutate)

        def wrong_occurrence(payload):
            mates = _grounding_mates()
            mates[2]["entities"][1].update(
                component="base-1",
                assembly_frame=True,
            )
            payload["mates"].extend(mates)

        result, _source, _output = self._prepare(
            mutate=wrong_occurrence
        )
        codes = [item["code"] for item in result.findings]
        self.assertIn("discovery.frame_attachment", codes)
        raw = json.loads(
            (_output / "discovery/native-discovery.json").read_text(
                encoding="utf-8"
            )
        )["raw"]
        grounded = next(
            mate
            for mate in raw["mates"]
            if mate["name"] == "base_ground_parallel"
        )
        self.assertIs(grounded["entities"][1]["assembly_frame"], False)

    def test_frame_attachment_arity_and_endpoints_are_exact(self):
        def extra_occurrence(payload):
            mates = _grounding_mates()
            mates[2]["entities"].insert(
                1,
                {
                    "component": "base-1",
                    "feature": "Cyl9",
                    "cylinder": {"point": [0.0, 0.0, 0.0], "direction": [1.0, 0.0, 0.0], "radius": 0.006},
                },
            )
            payload["mates"].extend(mates)

        def frame_only(payload):
            payload["mates"].append(
                {
                    "name": "base_ground_only_frame",
                    "type": "coincident",
                    "entity_count": 1,
                    "suppressed": False,
                    "error_code": 0,
                    "scope": "",
                    "limits": None,
                    "entities": [
                        {
                            "component": "",
                            "assembly_frame": True,
                            "feature": "Plane1",
                            "plane": {"point": [0.0, 0.0, 0.0], "normal": [0.0, 0.0, 1.0]},
                        }
                    ],
                }
            )

        def two_frame_entities(payload):
            payload["mates"].append(
                {
                    "name": "base_ground_two_frames",
                    "type": "coincident",
                    "entity_count": 2,
                    "suppressed": False,
                    "error_code": 0,
                    "scope": "",
                    "limits": None,
                    "entities": [
                        {
                            "component": "",
                            "assembly_frame": True,
                            "feature": "Plane1",
                            "plane": {"point": [0.0, 0.0, 0.0], "normal": [0.0, 0.0, 1.0]},
                        },
                        {
                            "component": "",
                            "assembly_frame": True,
                            "feature": "Plane2",
                            "plane": {"point": [0.0, 0.0, 0.0], "normal": [0.0, 0.0, -1.0]},
                        },
                    ],
                }
            )

        def unknown_occurrence(payload):
            mates = _grounding_mates()
            mates[0]["entities"][0]["component"] = "ghost-1"
            payload["mates"].extend(mates)

        def unreconstructable(payload):
            payload["mates"].append(
                {
                    "name": "base_ground_vertex",
                    "type": "coincident",
                    "entity_count": 2,
                    "suppressed": False,
                    "error_code": 0,
                    "scope": "",
                    "limits": None,
                    "entities": [
                        {"component": "base-1", "feature": "Vertex1"},
                        {
                            "component": "",
                            "assembly_frame": True,
                            "feature": "Plane2",
                            "plane": {"point": [0.0, 0.0, 0.0], "normal": [0.0, 0.0, 1.0]},
                        },
                    ],
                }
            )

        cases = {
            "extra_occurrence": (extra_occurrence, "exactly one frozen top-frame entity and one occurrence"),
            "frame_only": (frame_only, "no occurrence endpoint"),
            "two_frame_entities": (two_frame_entities, "no occurrence endpoint"),
            "unreconstructable": (unreconstructable, "cannot be reconstructed"),
        }
        for name, (mutate, fragment) in cases.items():
            with self.subTest(name=name):
                result, _source, _output = self._prepare(mutate=mutate)
                self.assertFalse(result.passed)
                self.assertTrue(
                    any(
                        item["code"] == "discovery.frame_attachment" and fragment in item["message"]
                        for item in result.findings
                    ),
                    result.findings,
                )
        with self.assertRaises(PipelineError):
            self._prepare(mutate=unknown_occurrence)

    def test_nested_frame_attachment_remains_blocking(self):
        for scope in ("sub-1",):

            def mutate(payload, scope=scope):
                mates = _grounding_mates()
                for mate in mates:
                    mate["scope"] = scope
                payload["mates"].extend(mates)

            with self.subTest(scope=scope), self.assertRaises(PipelineError):
                self._prepare(mutate=mutate)

    def test_frame_attachment_without_cs_base_link_keeps_the_ownerless_channel_closed(self):
        def mutate(payload):
            payload["mates"].extend(_grounding_mates())
            payload["datums"][0] = {"name": "CS_torso", "owner": "base-1", "array": copy.deepcopy(IDENTITY)}
            payload["datums"][1] = {"name": "CS_base_link", "owner": "arm-1", "array": _translated(0.0, 0.0, 0.1)}
            payload["datums"].append({"name": "SCS_tool_flange", "owner": "", "array": _translated(0.0, 0.0, 0.2)})

        result, _source, output = self._prepare(mutate=mutate)
        codes = self._codes(result)
        self.assertIn("discovery.frame_attachment", codes)
        self.assertIn("discovery.interface_unowned", codes)
        must_become = next(item for item in result.findings if item["code"] == "discovery.frame_attachment")
        self.assertIn("must become the CS_base_link body", must_become["message"])
        self.assertEqual(must_become["detail"]["datum"], "CS_torso")
        self.assertFalse(any("full rank" in item["message"] for item in result.findings))
        payload = json.loads((output / "discovery" / "native-discovery.json").read_text(encoding="utf-8"))
        self.assertEqual(
            {tuple(body["components"]): body["name"] for body in rebuild_rigid_bodies(payload)},
            {("arm-1",): "base_link", ("base-1",): "torso"},
        )

    def test_suppressed_frame_attachment_cannot_ground_the_record(self):
        def mutate(payload):
            mates = _grounding_mates()
            for mate in mates:
                mate["suppressed"] = True
            payload["mates"].extend(mates)
            payload["datums"][0] = {"name": "CS_base_link", "owner": "", "array": copy.deepcopy(IDENTITY)}

        result, _source, _output = self._prepare(mutate=mutate)
        codes = self._codes(result)
        self.assertNotIn("discovery.frame_attachment", codes)
        self.assertIn("discovery.link_name_missing", codes)
        self.assertIn("discovery.interface_unowned", codes)

    def test_container_datum_requires_solved_rigidity_and_excludes_container_material(self):
        for connected in (True, False):

            def mutate(payload, connected=connected):
                native = self.tmp / f"native{self.serial}" / "cad/sub.SLDASM"
                native.write_bytes(b"neutral container fixture\n")
                files = {
                    **payload["files"],
                    "cad/sub.SLDASM": hashlib.sha256(native.read_bytes()).hexdigest(),
                }
                payload["files"] = files
                payload["source_selection"] = sealed_selection(
                    files,
                    "cad/robot.SLDASM",
                    edges={
                        "cad/robot.SLDASM": ["cad/base.SLDPRT", "cad/arm.SLDPRT", "cad/sub.SLDASM"],
                        "cad/base.SLDPRT": [],
                        "cad/arm.SLDPRT": [],
                        "cad/sub.SLDASM": [],
                    },
                )
                container = copy.deepcopy(payload["components"][0])
                container.update({"name2": "sub-1", "instance_id": "sub-1", "document": "cad/sub.SLDASM"})
                payload["components"].append(container)
                from .selection_support import seal_fixture_open_observation

                seal_fixture_open_observation(payload)
                payload["datums"][0]["owner"] = "sub-1"
                if connected:
                    payload["mates"].append(
                        {
                            "name": "base_container_lock",
                            "type": "lock",
                            "entity_count": 2,
                            "suppressed": False,
                            "error_code": 0,
                            "limits": None,
                            "entities": [{"component": "base-1"}, {"component": "sub-1"}],
                        }
                    )

            with self.subTest(connected=connected):
                result, _source, output = self._prepare(mutate=mutate)
                if not connected:
                    self.assertIn("discovery.root_missing", self._codes(result))
                    continue
                self.assertTrue(result.passed, result.findings)
                payload = json.loads((output / "discovery" / "native-discovery.json").read_text(encoding="utf-8"))
                base = next(body for body in rebuild_rigid_bodies(payload) if body["name"] == "base_link")
                self.assertEqual(base["components"], ["base-1"])
                self.assertEqual(base["frame"]["coordinate_system"], "CS_base_link")
                report = self._verify(output)
                self.assertTrue(report["passed"], report["errors"])

    # ----------------------------------------------------------------- blocking

    def test_missing_identity_defers_to_publication_readiness(self):
        result, _source, output = self._prepare(mutate=lambda payload: payload["identity"].pop("hardware_id"))
        self.assertTrue(result.passed, result.findings)
        finding = next(item for item in result.findings if item["code"] == "discovery.identity_missing")
        self.assertFalse(finding["blocking"])
        self.assertEqual(finding["detail"]["publication_blocker"], "hardware_id")
        self.assertFalse((output / "cad-revision.json").exists())
        self.assertFalse((output / "source.json").exists())
        self.assertTrue((output / "discovery/native-discovery.json").is_file())
        self.assertTrue((output / "discovery/findings.json").is_file())
        payload = json.loads(result.discovery_path.read_text())
        self.assertIsNone(payload["identity"]["hardware_id"])

    def test_each_absent_administrative_field_defers_publication(self):
        complete = {
            "identity": {
                "hardware_id": "wire_test",
                "revision": "r1",
                "owner": "owner",
                "change_summary": "summary",
                "control": {"system": "handoff", "reference": "ref"},
                "robot_name": "wire_test",
            },
            "saved_configuration": "Default",
            "document_state": support.saved_document_state(),
            "properties": {"document": {}},
            "files": {"robot.SLDASM": "0" * 64},
            "source_selection": {"files": {"robot.SLDASM": "0" * 64}},
        }
        for field, code, blocker in (
            ("hardware_id", "discovery.identity_missing", "hardware_id"),
            ("revision", "discovery.revision_missing", "revision"),
            ("owner", "discovery.owner_missing", "owner"),
            ("change_summary", "discovery.summary_missing", "change_summary"),
            ("control", "discovery.control_missing", "control"),
        ):
            with self.subTest(field=field):
                record = copy.deepcopy(complete)
                record["identity"].pop(field)
                findings: list[dict] = []
                identity, _identity_sources = _identity(
                    record,
                    DiscoverySettings(),
                    findings,
                    configuration="Default",
                    assembly="robot.SLDASM",
                    native_files={"robot.SLDASM": "0" * 64},
                )
                item = next(finding for finding in findings if finding["code"] == code)
                self.assertFalse(item["blocking"])
                self.assertEqual(item["detail"]["publication_blocker"], blocker)
                self.assertFalse([finding for finding in findings if finding["blocking"]])
                if field == "control":
                    self.assertIsNone(identity["control"])
                else:
                    self.assertIsNone(identity[field])
        record = copy.deepcopy(complete)
        record["identity"].pop("robot_name")
        findings = []
        identity, _identity_sources = _identity(
            record,
            DiscoverySettings(),
            findings,
            configuration="Default",
            assembly="robot.SLDASM",
            native_files={"robot.SLDASM": "0" * 64},
        )
        item = next(finding for finding in findings if finding["code"] == "discovery.robot_name_missing")
        self.assertFalse(item["blocking"])
        self.assertEqual(item["detail"], {"technical_name": "robot", "authored": False})
        self.assertIsNone(identity["robot_name"])
        self.assertEqual(identity["robot_name_technical"], "robot")
        self.assertFalse([finding for finding in findings if finding["blocking"]])

    def test_absent_identity_fields_use_a_technical_robot_name(self):
        findings: list[dict] = []
        identity, _identity_sources = _identity(
            {"identity": {}, "properties": {"document": {}}},
            DiscoverySettings(),
            findings,
            configuration="Default",
            assembly="robot.SLDASM",
            native_files={"robot.SLDASM": "0" * 64},
        )
        self.assertIsNone(identity["robot_name"])
        self.assertEqual(identity["robot_name_technical"], "robot")
        item = next(finding for finding in findings if finding["code"] == "discovery.robot_name_missing")
        self.assertEqual(item["detail"], {"technical_name": "robot", "authored": False})
        findings = []
        identity, _identity_sources = _identity(
            {"identity": {"hardware_id": "Wrist_Drive-2"}, "properties": {"document": {}}},
            DiscoverySettings(),
            findings,
            configuration="Default",
            assembly="robot.SLDASM",
            native_files={"robot.SLDASM": "0" * 64},
        )
        self.assertIsNone(identity["robot_name"])
        self.assertEqual(identity["robot_name_technical"], "robot")

    def test_malformed_supplied_identity_still_blocks(self):
        complete = {
            "identity": {
                "hardware_id": "wire_test",
                "revision": "r1",
                "owner": "owner",
                "change_summary": "summary",
                "control": {"system": "handoff", "reference": "ref"},
                "robot_name": "wire_test",
            },
            "properties": {"document": {}},
        }
        for field, value, code in (
            ("hardware_id", "1bad", "discovery.identity_invalid"),
            ("revision", "bad rev", "discovery.revision_invalid"),
            ("owner", "bad\x01owner", "discovery.owner_invalid"),
            ("change_summary", "\x7f", "discovery.summary_invalid"),
            ("robot_name", "Bad Name", "discovery.robot_name_invalid"),
            ("control", {"system": "svn", "reference": "ref"}, "discovery.control_invalid"),
            ("control", {"system": "git", "reference": "branch-main"}, "discovery.control_invalid"),
        ):
            with self.subTest(field=field, value=value):
                record = copy.deepcopy(complete)
                record["identity"][field] = value
                findings: list[dict] = []
                _identity(
                    record,
                    DiscoverySettings(),
                    findings,
                    configuration="Default",
                    assembly="robot.SLDASM",
                    native_files={"robot.SLDASM": "0" * 64},
                )
                self.assertIn(code, [finding["code"] for finding in findings if finding["blocking"]])

    def test_falsey_property_objects_cannot_be_resealed_as_missing_evidence(self):
        baseline, _source, output = self._prepare()
        self.assertTrue(baseline.passed, baseline.findings)
        paths = (
            ("properties",),
            ("properties", "document"),
            ("properties", "components"),
            ("properties", "components", "arm-1"),
            ("properties", "mates"),
            ("properties", "mates", "shoulder_pitch_joint__coaxial"),
        )
        for path in paths:
            for value in ([], False, 0, ""):
                with self.subTest(path=path, value=value):
                    def mutate(payload, path=path, value=value):
                        target = payload.get("raw", payload)
                        for key in path[:-1]:
                            target = target.setdefault(key, {})
                        target[path[-1]] = value

                    with self.assertRaises(CadError) as caught:
                        self._prepare(mutate=mutate)
                    self.assertEqual(caught.exception.code, "cad_property_unreadable")
                    report = self._verify_mutated_record(output, mutate)
                    self.assertFalse(report["passed"])
                    self.assertIn("discovery.properties", {item["code"] for item in report["errors"]})

    def test_present_nontext_identity_is_invalid_instead_of_missing(self):
        complete = {
            "identity": {
                "hardware_id": "wire_test",
                "revision": "r1",
                "owner": "owner",
                "change_summary": "summary",
                "control": {"system": "handoff", "reference": "ref"},
                "robot_name": "wire_test",
            },
            "properties": {"document": {}},
        }
        for field, value in (
            ("hardware_id", 1),
            ("revision", []),
            ("owner", True),
            ("change_summary", {}),
            ("robot_name", 123),
            ("control", "handoff:ref"),
        ):
            with self.subTest(field=field, value=value):
                record_value = copy.deepcopy(complete)
                record_value["identity"][field] = value
                with self.assertRaises(CadError) as caught:
                    _identity(
                        record_value,
                        DiscoverySettings(),
                        [],
                        configuration="Default",
                        assembly="robot.SLDASM",
                        native_files={"robot.SLDASM": "0" * 64},
                    )
                self.assertEqual(caught.exception.code, "cad_identity_invalid")

        property_record = copy.deepcopy(complete)
        property_record["identity"].pop("robot_name")
        property_record["properties"]["document"]["dp.robot_name"] = False
        with self.assertRaises(CadError) as caught:
            _identity(
                property_record,
                DiscoverySettings(),
                [],
                configuration="Default",
                assembly="robot.SLDASM",
                native_files={"robot.SLDASM": "0" * 64},
            )
        self.assertEqual(caught.exception.code, "cad_identity_invalid")

    def test_unknown_mate_type_blocks_instead_of_going_rigid(self):
        result, _source, output = self._prepare(
            mutate=lambda payload: payload["mates"].append(
                {
                    "name": "Tangent1",
                    "type": "tangent",
                    "suppressed": False,
                    "error_code": 0,
                    "scope": "",
                    "limits": None,
                    "entities": [],
                }
            )
        )
        codes = self._codes(result)
        self.assertIn("discovery.mate_unsupported", codes)
        payload = json.loads((output / "discovery/native-discovery.json").read_text(encoding="utf-8"))
        for body in rebuild_rigid_bodies(payload):
            self.assertNotEqual(set(body["components"]), {"base-1", "arm-1"})

    def test_unsupported_freedom_pattern_blocks(self):
        result, _source, _output = self._prepare(
            mutate=lambda payload: payload["mates"].pop(1)  # concentric only: free spin and slide
        )
        codes = self._codes(result)
        self.assertIn("discovery.joint_unsupported_pattern", codes)

    def test_missing_native_limits_use_default_and_missing_drive_stays_unqualified(self):
        result, _source, output = self._prepare(
            mutate=lambda payload: payload["mates"][0].update({"limits": None})
        )
        self.assertTrue(result.passed, result.findings)
        self.assertNotIn("discovery.joint_limits_missing", {item["code"] for item in result.findings})
        self.assertIn("discovery.joint_default_policy", {item["code"] for item in result.findings})
        payload = json.loads((output / "discovery/native-discovery.json").read_text())
        joint = rebuild_joint_facts(payload, output, purpose="engineering")[0]
        self.assertEqual(joint["limit_source"], "default_policy")
        self.assertEqual((joint["limits"]["lower"], joint["limits"]["upper"]), (-math.pi, math.pi))
        self.assertEqual((joint["limits"]["effort"], joint["limits"]["velocity"]), (6.0, 2.0))
        result, _source, output = self._prepare(
            mutate=lambda payload: payload["properties"]["mates"]["shoulder_pitch_joint__coaxial"].pop(
                "dp.joint.drive_record"
            )
        )
        self.assertTrue(result.passed, result.findings)
        payload = json.loads((output / "discovery/native-discovery.json").read_text())
        joint = rebuild_joint_facts(payload, output, purpose="engineering")[0]
        self.assertNotIn("effort", joint["limits"])
        self.assertNotIn("velocity", joint["limits"])

    def test_present_nontext_record_references_block_instead_of_becoming_absent(self):
        cases = (
            ("dp.joint.limits_record", True, "discovery.joint_limits_invalid"),
            ("dp.joint.drive_record", False, "discovery.joint_drive_invalid"),
        )
        baseline, _source, output = self._prepare()
        self.assertTrue(baseline.passed, baseline.findings)
        for key, value, code in cases:
            with self.subTest(key=key, value=value):
                def mutate(payload: dict, key=key, value=value) -> None:
                    raw = payload.get("raw", payload)
                    raw["mates"][0]["limits"] = None
                    raw["properties"]["mates"]["shoulder_pitch_joint__coaxial"][key] = value

                result, _source, _output = self._prepare(mutate=mutate)
                self.assertIn(code, self._codes(result))
                if key == "dp.joint.limits_record":
                    self.assertNotIn(
                        "discovery.joint_default_policy",
                        {item["code"] for item in result.findings},
                    )
                report = self._verify_mutated_record(output, mutate)
                self.assertFalse(report["passed"])
                self.assertIn(code, {item["code"] for item in report["errors"]})

    def test_mate_author_limits_precede_engineering_default(self):
        def mutate(payload):
            payload["mates"][0]["limits"] = None
            payload["properties"]["mates"]["shoulder_pitch_joint__coaxial"][
                "dp.joint.limits_record"
            ] = "joints/arm.json#limits"

        result, _source, output = self._prepare(mutate=mutate)
        self.assertTrue(result.passed, result.findings)
        payload = json.loads((output / "discovery/native-discovery.json").read_text(encoding="utf-8"))
        joint = rebuild_joint_facts(payload, output, purpose="engineering")[0]
        self.assertEqual(joint["limit_source"], "declared_author")
        self.assertEqual(joint["limits"], {"lower": -1.5, "upper": 1.5, "effort": 6.0, "velocity": 2.0})
        self.assertNotIn("default_policy", joint)
        self.assertNotIn("discovery.joint_default_policy", {item["code"] for item in result.findings})
        self.assertTrue(self._verify(output)["passed"])

    def test_explicit_zero_author_range_is_preserved_not_treated_as_missing(self):
        record_value = {
            "limits": {"lower": 0.0, "upper": 0.0},
            "drive": {"effort": 6.0, "velocity": 2.0},
        }

        def mutate(payload):
            payload["mates"][0]["limits"] = None
            payload["properties"]["mates"]["shoulder_pitch_joint__coaxial"][
                "dp.joint.limits_record"
            ] = "joints/arm.json#limits"

        result, _source, output = self._prepare(mutate=mutate, joint_record=record_value)
        self.assertTrue(result.passed, result.findings)
        payload = json.loads((output / "discovery/native-discovery.json").read_text(encoding="utf-8"))
        joint = rebuild_joint_facts(payload, output, purpose="engineering")[0]
        self.assertEqual(joint["limit_source"], "declared_author")
        self.assertEqual((joint["limits"]["lower"], joint["limits"]["upper"]), (0.0, 0.0))
        self.assertNotIn("default_policy", joint)
        finding = next(item for item in result.findings if item["code"] == "discovery.joint_limits_degenerate")
        self.assertFalse(finding["blocking"])
        self.assertTrue(self._verify(output)["passed"])

    def test_invalid_author_range_blocks_before_engineering_default(self):
        record_value = {
            "limits": {"lower": 2.0, "upper": -2.0},
            "drive": {"effort": 6.0, "velocity": 2.0},
        }

        def mutate(payload):
            payload["mates"][0]["limits"] = None
            payload["properties"]["mates"]["shoulder_pitch_joint__coaxial"][
                "dp.joint.limits_record"
            ] = "joints/arm.json#limits"

        result, _source, _output = self._prepare(mutate=mutate, joint_record=record_value)
        self.assertIn("discovery.joint_limits_invalid", self._codes(result))
        self.assertNotIn("discovery.joint_default_policy", {item["code"] for item in result.findings})

        baseline, _source, baseline_output = self._prepare(mutate=mutate)
        self.assertTrue(baseline.passed, baseline.findings)
        payload = json.loads((baseline_output / "discovery/native-discovery.json").read_text(encoding="utf-8"))
        limits_entry = next(item for item in payload["records"] if item["key"] == "limits")
        invalid_bytes = json.dumps(record_value).encode("utf-8")
        (baseline_output / limits_entry["package_file"]).write_bytes(invalid_bytes)
        limits_entry["sha256"] = hashlib.sha256(invalid_bytes).hexdigest()
        with self.assertRaisesRegex(Exception, "finite and ordered"):
            rebuild_joint_facts(payload, baseline_output, purpose="engineering")

    def test_controlled_numbers_reject_boolean_string_and_nonfinite_fields(self):
        def author_limits(payload):
            payload["mates"][0]["limits"] = None
            payload["properties"]["mates"]["shoulder_pitch_joint__coaxial"][
                "dp.joint.limits_record"
            ] = "joints/arm.json#limits"

        cases = (
            ("boolean position", {"lower": False, "upper": True}, author_limits),
            ("string position", {"lower": "0", "upper": "1"}, author_limits),
            ("nan position", {"lower": float("nan"), "upper": 1.0}, author_limits),
            ("infinite position", {"lower": 0.0, "upper": float("inf")}, author_limits),
            ("boolean effort", {"effort": True, "velocity": 1.0}, None),
            ("boolean velocity", {"effort": 1.0, "velocity": False}, None),
            ("string effort", {"effort": "1", "velocity": 1.0}, None),
            ("string velocity", {"effort": 1.0, "velocity": "1"}, None),
            ("nan effort", {"effort": float("nan"), "velocity": 1.0}, None),
            ("infinite velocity", {"effort": 1.0, "velocity": float("inf")}, None),
            ("negative effort", {"effort": -1.0, "velocity": 1.0}, None),
            ("negative velocity", {"effort": 1.0, "velocity": -1.0}, None),
        )
        for label, limits_value, mutate in cases:
            with self.subTest(label=label):
                record_value = {
                    "limits": (
                        limits_value
                        if mutate is author_limits
                        else {"lower": -1.5, "upper": 1.5}
                    ),
                    "drive": (
                        limits_value
                        if mutate is None
                        else {"effort": 6.0, "velocity": 2.0}
                    ),
                }
                result, _source, _output = self._prepare(
                    mutate=mutate,
                    joint_record=record_value,
                )
                self.assertFalse(result.passed)
                self.assertNotIn(
                    "discovery.joint_default_policy",
                    {item["code"] for item in result.findings},
                )

                baseline, _source, output = self._prepare(
                    mutate=mutate,
                    joint_record={
                        "limits": {"lower": -1.5, "upper": 1.5},
                        "drive": {"effort": 6.0, "velocity": 2.0},
                    },
                )
                self.assertTrue(baseline.passed, baseline.findings)
                delivery = self._deliver(output)
                self._rewrite_delivery_controlled_record(
                    delivery,
                    lambda record, limits_value=limits_value, mutate=mutate: record.update(
                        {
                            "limits": (
                                limits_value
                                if mutate is author_limits
                                else {"lower": -1.5, "upper": 1.5}
                            ),
                            "drive": (
                                limits_value
                                if mutate is None
                                else {"effort": 6.0, "velocity": 2.0}
                            ),
                        }
                    ),
                )
                report = verify_discovery(delivery)
                self.assertFalse(report["passed"])

    def test_controlled_drive_zero_is_preserved_for_revolute_and_continuous(self):
        record_value = {
            "limits": {"lower": -1.5, "upper": 1.5},
            "drive": {"effort": 0.0, "velocity": 0.0},
        }
        result, _source, output = self._prepare(joint_record=record_value)
        self.assertTrue(result.passed, result.findings)
        payload = json.loads((output / "discovery/native-discovery.json").read_text(encoding="utf-8"))
        joint = rebuild_joint_facts(payload, output, purpose="engineering")[0]
        self.assertEqual((joint["limits"]["effort"], joint["limits"]["velocity"]), (0.0, 0.0))
        self.assertTrue(self._verify(output)["passed"])

        continuous_record = {
            "limits": {"lower": -1.5, "upper": 1.5},
            "drive": {"effort": 0.0, "velocity": 0.0},
        }

        def continuous(payload):
            payload["mates"][0]["limits"] = None
            payload["properties"]["mates"]["shoulder_pitch_joint__coaxial"]["dp.joint.type"] = "continuous"

        result, _source, output = self._prepare(
            mutate=continuous,
            joint_record=continuous_record,
        )
        self.assertTrue(result.passed, result.findings)
        payload = json.loads((output / "discovery/native-discovery.json").read_text(encoding="utf-8"))
        joint = rebuild_joint_facts(payload, output, purpose="engineering")[0]
        self.assertEqual(joint["limits"], {"effort": 0.0, "velocity": 0.0})
        self.assertTrue(self._verify(output)["passed"])

    def test_controlled_evidence_warns_and_cannot_mask_native_read_errors(self):
        record_value = {
            "limits": {"lower": -1.5, "upper": 1.5},
            "drive": {"effort": 0.0, "velocity": 0.0},
        }

        def native_with_author(payload):
            payload["mates"][0].update(
                {
                    "limits": None,
                    "is_advanced_mate": None,
                    "is_advanced_mate_error": None,
                    "is_advanced_mate_api": None,
                    "native_limit": None,
                }
            )
            payload["properties"]["mates"]["shoulder_pitch_joint__coaxial"][
                "dp.joint.limits_record"
            ] = "joints/arm.json#limits"

        result, _source, output = self._prepare(
            mutate=native_with_author,
            joint_record=record_value,
        )
        self.assertTrue(result.passed, result.findings)
        definition = source_definition(
            self._deliver(output),
            discovery_name="input/discovery/native-discovery.json",
            cad_prefix="input/cad/",
            records_prefix="input/",
        )
        joint = definition["joints"][0]
        self.assertEqual(joint["limit_source"], "declared_author")
        self.assertEqual(joint["limits"], record_value["limits"] | record_value["drive"])
        self.assertRegex(joint["limit_evidence"]["sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(joint["limit_evidence"]["key"], "limits")
        self.assertEqual(
            joint["limit_evidence"]["declared_values"],
            record_value["limits"],
        )
        self.assertEqual(joint["limit_evidence"]["native_observation_errors"], [])
        self.assertEqual(joint["drive_evidence"]["key"], "drive")
        self.assertEqual(joint["drive_evidence"]["declared_values"], record_value["drive"])

        warnings = _value_warnings(
            definition,
            definition["bodies"],
            [],
            [{"name": joint["name"], "axis": [0.0, 0.0, 1.0], "limits": joint["limits"]}],
            [],
        )
        declared = next(item for item in warnings if item["code"] == "limits.declared_author")
        self.assertEqual([row["field"] for row in declared["objects"]], ["lower", "upper"])
        self.assertEqual(declared["objects"][0]["native_observation_errors"], [])
        drive = next(item for item in warnings if item["code"] == "drive.declared_author")
        self.assertEqual([row["field"] for row in drive["objects"]], ["effort", "velocity"])
        self.assertIn("drive.unqualified", {item["code"] for item in warnings})

        def unreadable_native_with_author(payload):
            native_with_author(payload)
            payload["mates"][0]["is_advanced_mate_error"] = "native failure"

        failed, _source, _output = self._prepare(
            mutate=unreadable_native_with_author,
            joint_record=record_value,
        )
        self.assertFalse(failed.passed)
        finding = next(item for item in failed.findings if item["code"] == "discovery.joint_limits_unreadable")
        self.assertTrue(finding["blocking"])
        self.assertTrue(finding["detail"]["errors"])
        self.assertNotIn("discovery.joint_default_policy", {item["code"] for item in failed.findings})
        report = self._verify_mutated_record(output, lambda payload: unreadable_native_with_author(payload["raw"]))
        self.assertFalse(report["passed"])
        self.assertIn("discovery.joint_limits_unreadable", {item["code"] for item in report["errors"]})

    def test_native_valid_range_conflicts_with_nonzero_author_record(self):
        record_value = {
            "limits": {"lower": -2.0, "upper": 2.0},
            "drive": {"effort": 6.0, "velocity": 2.0},
        }

        def mutate(payload):
            payload["mates"][0]["limits"] = {"lower": -1.0, "upper": 1.0, "unit": "rad"}
            payload["properties"]["mates"]["shoulder_pitch_joint__coaxial"][
                "dp.joint.limits_record"
            ] = "joints/arm.json#limits"

        result, _source, _output = self._prepare(mutate=mutate, joint_record=record_value)
        self.assertIn("discovery.joint_limits_conflict", self._codes(result))

    def test_native_valid_range_conflicts_with_zero_author_record(self):
        record_value = {
            "limits": {"lower": 0.0, "upper": 0.0},
            "drive": {"effort": 6.0, "velocity": 2.0},
        }

        def mutate(payload):
            payload["mates"][0]["limits"] = {
                "lower": -1.0,
                "upper": 1.0,
                "unit": "rad",
            }
            payload["properties"]["mates"][
                "shoulder_pitch_joint__coaxial"
            ]["dp.joint.limits_record"] = "joints/arm.json#limits"

        result, _source, _output = self._prepare(
            mutate=mutate,
            joint_record=record_value,
        )
        self.assertIn("discovery.joint_limits_conflict", self._codes(result))

    def test_independent_verify_rejects_native_range_over_zero_author_record(
        self,
    ):
        record_value = {
            "limits": {"lower": 0.0, "upper": 0.0},
            "drive": {"effort": 6.0, "velocity": 2.0},
        }

        def baseline_mutation(payload):
            payload["mates"][0]["limits"] = None
            payload["properties"]["mates"][
                "shoulder_pitch_joint__coaxial"
            ]["dp.joint.limits_record"] = "joints/arm.json#limits"

        result, _source, output = self._prepare(
            mutate=baseline_mutation,
            joint_record=record_value,
        )
        self.assertTrue(result.passed, result.findings)

        def add_native_limit(payload):
            mate = copy.deepcopy(payload["raw"]["mates"][1])
            for index, entity in enumerate(mate["entities"]):
                entity.update(
                    {
                        "entity_index": index,
                        "reference_configuration": "Default",
                        "mate_entity_reference": {
                            "error": None,
                            "geometry": {
                                "kind": "point",
                                "point": [0.0, 0.0, 0.0],
                                "frame": "component-local",
                            },
                        },
                    }
                )
            mate.update(
                {
                    "name": "shoulder_pitch_joint__limit",
                    "type": "limitangle",
                    "limits": {
                        "lower": -1.0,
                        "upper": 1.0,
                        "unit": "rad",
                    },
                    "is_advanced_mate": True,
                    "is_advanced_mate_error": None,
                    "is_advanced_mate_api": (
                        "IAngleMateFeatureData.IsAdvancedMate"
                    ),
                    "native_limit": {
                        "type_index": 6,
                        "definition_interface": (
                            "IAngleMateFeatureData"
                        ),
                        "definition_interface_iid": (
                            "{EF4303D7-B4FD-46E7-AD34-D942C420E096}"
                        ),
                        "nominal_dimension": {
                            "value": 1.0,
                            "api": "IAngleMateFeatureData.Angle",
                            "unit": "rad",
                        },
                        "minimum_variation": {
                            "value": -2.0,
                            "api": "IMate2.MinimumVariation",
                            "unit": "rad",
                        },
                        "maximum_variation": {
                            "value": 0.0,
                            "api": "IMate2.MaximumVariation",
                            "unit": "rad",
                        },
                        "minimum_limit": {
                            "value": -1.0,
                            "api": (
                                "IAngleMateFeatureData.MinimumAngle"
                            ),
                            "unit": "rad",
                        },
                        "maximum_limit": {
                            "value": 1.0,
                            "api": (
                                "IAngleMateFeatureData.MaximumAngle"
                            ),
                            "unit": "rad",
                        },
                    },
                }
            )
            payload["raw"]["mates"].append(mate)

        report = self._verify_mutated_record(output, add_native_limit)
        self.assertFalse(report["passed"])
        self.assertIn(
            "discovery.joint_limits_conflict",
            {item["code"] for item in report["errors"]},
        )

    def test_unreadable_native_limit_blocks_without_a_default(self):
        payload = record()
        cluster_findings: list[dict] = []
        clusters = _clusters(payload, cluster_findings)
        self.assertFalse(cluster_findings)
        payload["mates"][0].update(
            {
                "type": "limitangle",
                "limits": None,
                "is_advanced_mate": True,
                "is_advanced_mate_error": None,
                "is_advanced_mate_api": "IAngleMateFeatureData.IsAdvancedMate",
                "native_limit": {"type_index": 6},
            }
        )
        payload["properties"]["mates"]["shoulder_pitch_joint__coaxial"].pop("dp.joint.drive_record")
        clusters.pairs[("arm-1", "base-1")]["limits"] = None
        findings: list[dict] = []
        joints = _joint_facts(payload, clusters, DiscoverySettings(), findings)
        self.assertEqual(joints, [])
        self.assertIn("discovery.joint_limits_unreadable", {item["code"] for item in findings if item["blocking"]})
        self.assertNotIn("discovery.joint_default_policy", {item["code"] for item in findings})

    def test_continuous_mate_joint_without_position_range_keeps_only_drive(self):
        def mutate(payload):
            payload["mates"][0]["limits"] = None
            payload["properties"]["mates"]["shoulder_pitch_joint__coaxial"]["dp.joint.type"] = "continuous"

        result, _source, output = self._prepare(mutate=mutate)
        self.assertTrue(result.passed, result.findings)
        self.assertNotIn("discovery.joint_limits_missing", {item["code"] for item in result.findings})
        payload = json.loads((output / "discovery/native-discovery.json").read_text(encoding="utf-8"))
        joint = rebuild_joint_facts(payload, output, purpose="engineering")[0]
        self.assertEqual(joint["type"], "continuous")
        self.assertEqual(joint["limits"], {"effort": 6.0, "velocity": 2.0})
        self.assertNotIn("default_policy", joint)
        self.assertTrue(self._verify(output)["passed"])

    def test_continuous_mate_joint_rejects_position_range_evidence(self):
        def mutate(payload):
            payload["mates"][0]["limits"] = {"lower": -1.0, "upper": 1.0, "unit": "rad"}
            payload["properties"]["mates"]["shoulder_pitch_joint__coaxial"]["dp.joint.type"] = "continuous"

        result, _source, _output = self._prepare(mutate=mutate)
        self.assertIn("discovery.joint_limits_unexpected", self._codes(result))

        def declared_range(payload):
            payload["mates"][0]["limits"] = None
            payload["properties"]["mates"]["shoulder_pitch_joint__coaxial"].update(
                {
                    "dp.joint.type": "continuous",
                    "dp.joint.limits_record": "joints/arm.json#limits",
                }
            )

        result, _source, _output = self._prepare(mutate=declared_range)
        self.assertIn("discovery.joint_limits_unexpected", self._codes(result))

    def test_nonlimit_mate_with_native_limit_payload_blocks_both_paths(self):
        result, _source, _output = self._prepare(
            mutate=lambda payload: payload["mates"][1].update({"native_limit": {"type_index": 6}})
        )
        self.assertIn("discovery.joint_limits_invalid", self._codes(result))

        baseline, _source, baseline_output = self._prepare()
        self.assertTrue(baseline.passed, baseline.findings)
        report = self._verify_mutated_record(
            baseline_output,
            lambda payload: payload["raw"]["mates"][1].update({"native_limit": {"type_index": 6}}),
        )
        self.assertFalse(report["passed"])
        self.assertIn(
            "discovery.joint_limits_invalid",
            {item["code"] for item in report["errors"]},
        )

    def test_inverted_native_mate_range_blocks_prepare_and_verification(self):
        inverted = {"lower": 2.0, "upper": -2.0, "unit": "rad"}
        result, _source, _output = self._prepare(
            mutate=lambda payload: payload["mates"][0].update({"limits": inverted})
        )
        self.assertIn("discovery.joint_limits_invalid", self._codes(result))

        baseline, _source, baseline_output = self._prepare()
        self.assertTrue(baseline.passed, baseline.findings)
        report = self._verify_mutated_record(
            baseline_output,
            lambda payload: payload["raw"]["mates"][0].update({"limits": inverted}),
        )
        self.assertFalse(report["passed"])
        self.assertIn("discovery.graph", {item["code"] for item in report["errors"]})

    def test_negative_native_limit_face_selector_is_unreadable(self):
        mate = support.native_limit_mate("base-1", "arm-1")
        for index, entity in enumerate(mate["entities"]):
            entity.update(
                feature=None,
                face_index=-(index + 1),
            )
        with self.assertRaisesRegex(ValueError, "face selector"):
            validate_native_limit(mate)

        def mutate(payload):
            payload["mates"][0]["limits"] = None
            observed = support.native_limit_mate(
                "base-1",
                "arm-1",
                name="shoulder_pitch_joint",
            )
            for index, entity in enumerate(observed["entities"]):
                entity.update(
                    feature=None,
                    face_index=-(index + 1),
                )
            payload["mates"].append(observed)

        result, _source, _output = self._prepare(mutate=mutate)
        self.assertIn("discovery.joint_limits_unreadable", self._codes(result))

        baseline, _source, baseline_output = self._prepare()
        self.assertTrue(baseline.passed, baseline.findings)
        report = self._verify_mutated_record(
            baseline_output,
            lambda payload: mutate(payload["raw"]),
        )
        self.assertFalse(report["passed"])
        self.assertIn(
            "discovery.joint_limits_unreadable",
            {item["code"] for item in report["errors"]},
        )

    def test_incomplete_limited_mate_payload_is_unreadable(self):
        payload = record()
        cluster_findings: list[dict] = []
        clusters = _clusters(payload, cluster_findings)
        self.assertFalse(cluster_findings)
        payload["mates"][0].update(
            {
                "type": "limitangle",
                "is_advanced_mate": True,
                "is_advanced_mate_error": None,
                "is_advanced_mate_api": "IAngleMateFeatureData.IsAdvancedMate",
                "native_limit": {"type_index": 6},
            }
        )
        findings: list[dict] = []
        joints = _joint_facts(payload, clusters, DiscoverySettings(), findings)
        self.assertEqual(joints, [])
        self.assertEqual(
            [(item["code"], item["blocking"]) for item in findings],
            [("discovery.joint_limits_unreadable", True)],
        )

    def test_default_policy_records_whether_limit_inventory_was_complete(self):
        def complete_inventory(payload):
            for mate in payload["mates"]:
                mate.update(
                    {
                        "limits": None,
                        "native_limit": None,
                        "is_advanced_mate": None,
                        "is_advanced_mate_error": None,
                        "is_advanced_mate_api": None,
                    }
                )

        result, _source, output = self._prepare(mutate=complete_inventory)
        self.assertTrue(result.passed, result.findings)
        finding = next(item for item in result.findings if item["code"] == "discovery.joint_default_policy")
        self.assertEqual(finding["detail"]["native_absence"], "complete_mate_inventory_no_enabled_limit")
        self.assertTrue(self._verify(output)["passed"])

        def incomplete_inventory(payload):
            payload["mates"][0]["limits"] = None
            payload["mates"][0].pop("native_limit")

        result, _source, _output = self._prepare(mutate=incomplete_inventory)
        self.assertFalse(result.passed)
        self.assertIn(
            "discovery.joint_limits_unreadable",
            {item["code"] for item in result.findings if item["blocking"]},
        )
        self.assertNotIn("discovery.joint_default_policy", {item["code"] for item in result.findings})

        report = self._verify_mutated_record(output, lambda payload: incomplete_inventory(payload["raw"]))
        self.assertFalse(report["passed"])
        self.assertIn("discovery.joint_limits_unreadable", {item["code"] for item in report["errors"]})

    def test_missing_shaft_selector_blocks(self):
        def mutate(payload):
            for entity in payload["mates"][0]["entities"]:
                entity.pop("feature", None)
                entity.pop("face_index", None)

        result, _source, _output = self._prepare(mutate=mutate)
        self.assertIn("discovery.joint_axis_selector_missing", self._codes(result))

    def test_unspecified_design_budget_adds_no_ranges_and_no_blocker(self):
        payload = record()
        payload["properties"]["document"].pop("dp.design_budget_record")
        findings = []
        with mock.patch("description_pipeline.sources.solidworks.discovery._resolve_record") as resolve:
            checks, evidence = _budgets(payload, DiscoverySettings(), findings)
        resolve.assert_not_called()
        self.assertEqual(checks, {})
        self.assertIsNone(evidence)
        self.assertEqual([item["code"] for item in findings], ["discovery.design_budget_unspecified"])
        self.assertFalse(findings[0]["blocking"])

    def test_unspecified_budget_does_not_create_joint_drive(self):
        def mutate(payload):
            payload["properties"]["document"].pop("dp.design_budget_record")
            payload["properties"]["mates"]["shoulder_pitch_joint__coaxial"].pop("dp.joint.drive_record")

        result, _source, output = self._prepare(mutate=mutate)
        self.assertTrue(result.passed, result.findings)
        budget = next(item for item in result.findings if item["code"] == "discovery.design_budget_unspecified")
        self.assertFalse(budget["blocking"])
        payload = json.loads((output / "discovery/native-discovery.json").read_text())
        self.assertNotIn("budget", payload)

    def test_explicit_missing_design_budget_record_still_blocks(self):
        def mutate(payload):
            payload["properties"]["document"]["dp.design_budget_record"] = "missing-budget.json#robot"

        result, _source, _output = self._prepare(mutate=mutate)
        self.assertIn("discovery.record_missing", self._codes(result))
        self.assertNotIn("discovery.design_budget_unspecified", {item["code"] for item in result.findings})

    def test_explicit_invalid_budget_reference_is_not_treated_as_unspecified(self):
        for reference in (False, 42, [], {}):
            payload = record()
            payload["properties"]["document"]["dp.design_budget_record"] = reference
            findings = []
            checks, evidence = _budgets(payload, DiscoverySettings(), findings)
            self.assertIsNone(checks)
            self.assertIsNone(evidence)
            self.assertEqual([item["code"] for item in findings], ["discovery.design_budget_invalid"])
            self.assertTrue(findings[0]["blocking"])

    def test_nonfinite_controlled_budget_retains_serializable_blocking_findings(self):
        def mutate(_payload):
            (self.tmp / "records/budget.json").write_text(
                '{"robot":{"expected_mass_kg":[0.28,1e999],"expected_extent_m":[0.35,0.5]}}', encoding="utf-8"
            )

        result, _source, output = self._prepare(mutate=mutate)
        self.assertIn("discovery.record_unreadable", self._codes(result))
        payload = json.loads((output / "discovery/findings.json").read_text(encoding="utf-8"))
        json.dumps(payload, allow_nan=False)

    def test_off_origin_rotated_hinge_reconstructs_the_axis(self):
        """A hinge whose shaft is off-origin and whose child is rotated."""

        def rotate_x_minus_90(vector):
            x, y, z = vector
            return [x, z, -y]

        def mutate(payload):
            arm = next(item for item in payload["components"] if item["name2"] == "arm-1")
            arm["transform"] = [
                1.0,
                0.0,
                0.0,
                0.0,
                0.0,
                0.0,
                1.0,
                0.0,
                0.0,
                -1.0,
                0.0,
                0.0,
                0.0,
                0.0,
                0.0,
                1.0,
            ]
            axis_mate, seat = payload["mates"]
            axis_mate["entities"][0]["cylinder"] = {
                "point": [0.3, 0.3, 0.5],
                "direction": [0.0, 1.0, 0.0],
                "radius": 0.006,
            }
            axis_mate["entities"][1]["cylinder"] = {
                "point": rotate_x_minus_90([0.3, 0.3, 0.5]) if False else [0.3, -0.5, 0.3],
                "direction": [0.0, 0.0, 1.0],
                "radius": 0.006,
            }
            seat["entities"][0]["plane"] = {"point": [0.3, 0.3, 0.5], "normal": [0.0, 1.0, 0.0]}
            seat["entities"][1]["plane"] = {"point": [0.3, -0.5, 0.3], "normal": [0.0, 0.0, 1.0]}
            datum = next(item for item in payload["datums"] if item["name"] == "CS_arm_link")
            datum["array"] = _translated(0.3, 0.35, 0.5)

        result, _source, output = self._prepare(mutate=mutate)
        self.assertTrue(result.passed, result.findings)
        payload = json.loads((output / "discovery" / "native-discovery.json").read_text(encoding="utf-8"))
        joint = rebuild_joint_facts(payload, output, purpose="engineering")[0]
        self.assertEqual(joint["type"], "revolute")
        self.assertEqual(joint["axis"], [0.0, 1.0, 0.0])
        self.assertEqual(self._verify(output)["passed"], True)

    def test_child_frame_off_the_native_axis_blocks(self):
        def mutate(payload):
            datum = next(item for item in payload["datums"] if item["name"] == "CS_arm_link")
            datum["array"] = _translated(0.4, 0.0, 0.1)

        result, _source, _output = self._prepare(mutate=mutate)
        self.assertIn("discovery.joint_frame_off_axis", self._codes(result))

    def test_mate_group_naming_is_required(self):
        def mutate(payload):
            payload["mates"][0]["name"] = "Concentric1"
            payload["properties"]["mates"]["Concentric1"] = payload["properties"]["mates"].pop(
                "shoulder_pitch_joint__coaxial"
            )

        result, _source, _output = self._prepare(mutate=mutate)
        self.assertIn("discovery.joint_name_missing", self._codes(result))

    def test_link_name_must_be_a_CS_datum_owned_by_the_body(self):
        def mutate(payload):
            for datum in payload["datums"]:
                datum["name"] = datum["name"].replace("CS_", "AX_")

        result, _source, _output = self._prepare(mutate=mutate)
        self.assertIn("discovery.link_name_missing", self._codes(result))

    def test_link_name_missing_detail_carries_context_bound_solver_observations(self):
        observation = {
            "context": {"document": "robot.SLDASM", "configuration": "Default", "scope": ""},
            "solving": {
                "method": "IComponent2.Solving",
                "family": "swComponentSolvingOption_e",
                "value": 0,
                "error": None,
            },
            "constrained_status": {
                "method": "IComponent2.GetConstrainedStatus",
                "family": "swConstrainedStatus_e",
                "value": 3,
                "error": None,
            },
        }

        def mutate(payload):
            for datum in payload["datums"]:
                datum["name"] = datum["name"].replace("CS_", "AX_")
            for component in payload["components"]:
                component["solver_observation"] = copy.deepcopy(observation)

        result, _source, _output = self._prepare(mutate=mutate)
        findings = [item for item in result.findings if item["code"] == "discovery.link_name_missing"]
        self.assertTrue(findings)
        for item in findings:
            # Wording stays neutral: an unproven body, never a loose-CAD claim.
            self.assertEqual(item["message"], "no CS_<link> coordinate system is owned by this body")
            observations = item["detail"]["solver_observations"]
            for member in item["detail"]["components"]:
                self.assertEqual(observations[member], observation)

    def test_link_name_missing_detail_is_unchanged_without_solver_observations(self):
        def mutate(payload):
            for datum in payload["datums"]:
                datum["name"] = datum["name"].replace("CS_", "AX_")

        result, _source, _output = self._prepare(mutate=mutate)
        findings = [item for item in result.findings if item["code"] == "discovery.link_name_missing"]
        self.assertTrue(findings)
        self.assertTrue(all(set(item["detail"]) == {"components"} for item in findings))

    def test_point_plane_coincident_does_not_rigidify(self):
        """A vertex-on-face mate removes one translation, never orientation."""

        def mutate(payload):
            seat = payload["mates"][1]
            seat["entities"][0]["plane"] = {"point": [0.0, 0.0, 0.1], "normal": [1.0, 0.0, 0.0]}
            seat["entities"][1].pop("plane", None)
            seat["entities"][1]["point"] = [0.0, 0.0, 0.1]

        result, _source, output = self._prepare(mutate=mutate)
        codes = self._codes(result)
        self.assertIn("discovery.joint_unsupported_pattern", codes)
        payload = json.loads((output / "discovery" / "native-discovery.json").read_text(encoding="utf-8"))
        for body in rebuild_rigid_bodies(payload):
            self.assertNotEqual(set(body["components"]), {"base-1", "arm-1"})

    def test_bounded_travel_mates_keep_their_freedom(self):
        """limitdistance keeps the bounded translation free instead of rigid."""

        for mate_type in ("limitdistance", "limitangle"):
            result, _source, output = self._prepare(
                mutate=lambda payload, kind=mate_type: payload["mates"][1].update({"type": kind})
            )
            codes = self._codes(result)
            self.assertIn("discovery.joint_unsupported_pattern", codes, (mate_type, codes))
            payload = json.loads((output / "discovery" / "native-discovery.json").read_text(encoding="utf-8"))
            for body in rebuild_rigid_bodies(payload):
                self.assertNotEqual(set(body["components"]), {"base-1", "arm-1"}, mate_type)

    def test_conflicting_joint_scalar_sources_block(self):
        def mutate(payload):
            payload["properties"]["document"]["dp.joint.shoulder_pitch_joint.drive_record"] = "other.json#drive"

        result, _source, _output = self._prepare(mutate=mutate)
        self.assertIn("discovery.joint_property_conflict", self._codes(result))

    def test_root_comes_from_cs_base_link_not_a_fixed_flag(self):
        def unfixed(payload):
            for component in payload["components"]:
                component["fixed"] = False

        result, _source, _output = self._prepare(mutate=unfixed)
        self.assertTrue(result.passed, result.findings)

        def renamed(payload):
            for datum in payload["datums"]:
                if datum["name"] == "CS_base_link":
                    datum["name"] = "CS_frame"

        result, _source, _output = self._prepare(mutate=renamed)
        self.assertIn("discovery.root_missing", self._codes(result))

    def test_radially_displaced_concentric_shaft_blocks(self):
        def mutate(payload):
            payload["mates"][0]["entities"][1]["cylinder"]["point"] = [0.01, 0.0, 0.1]

        result, _source, _output = self._prepare(mutate=mutate)
        self.assertIn("discovery.joint_axis_misaligned", self._codes(result))

    def test_interface_datums_become_frames_and_never_bind_links(self):
        def mutate(payload):
            payload["datums"].append(
                {"name": "CS_arm_link_mount", "owner": "arm-1", "array": _translated(0.0, 0.0, 0.2)}
            )
            payload["datums"].append({"name": "TCP_pinch", "owner": "arm-1", "array": _translated(0.0, 0.0, 0.3)})
            payload["properties"]["components"] = {"arm-1": {"dp.body_datum": "CS_arm_link"}}

        result, _source, output = self._prepare(mutate=mutate)
        self.assertTrue(result.passed, result.findings)
        payload = json.loads((output / "discovery" / "native-discovery.json").read_text(encoding="utf-8"))
        frames = rebuild_interface_frames(payload)
        self.assertEqual(
            {frame["name"]: (frame["parent"], frame["coordinate_system"]) for frame in frames},
            {
                "arm_link_mount": ("arm_link", "CS_arm_link_mount"),
                "pinch_tcp": ("arm_link", "TCP_pinch"),
            },
        )
        names = [body["name"] for body in rebuild_rigid_bodies(payload)]
        self.assertEqual(names, ["arm_link", "base_link"])
        self.assertTrue(self._verify(output)["passed"])

    def test_unowned_interface_datum_blocks(self):
        with self.assertRaises(PipelineError):
            self._prepare(
                mutate=lambda payload: payload["datums"].append(
                    {
                        "name": "TCP_loose",
                        "owner": "ghost-1",
                        "array": _translated(0.0, 0.0, 0.4),
                    }
                )
            )

    def test_dropped_frame_is_rejected_by_the_oracle(self):
        def mutate(payload):
            payload["datums"].append({"name": "TCP_pinch", "owner": "arm-1", "array": _translated(0.0, 0.0, 0.3)})

        result, _source, output = self._prepare(mutate=mutate)
        self.assertTrue(result.passed, result.findings)
        destination = self._deliver(output)
        report = verify_discovery(destination)
        # The engineered interface is derived from the raw datum alone; no saved carrier
        # exists that dropping a frame could suppress.
        self.assertTrue(report["passed"], report["errors"])
        payload = json.loads((destination / "input/discovery/native-discovery.json").read_text(encoding="utf-8"))
        self.assertIn("pinch_tcp", [frame["name"] for frame in rebuild_interface_frames(payload)])

    def test_jcs_must_alias_the_child_datum(self):
        def alias(payload):
            child = next(datum for datum in payload["datums"] if datum["name"] == "CS_arm_link")
            payload["datums"].append(
                {"name": "JCS_shoulder_pitch_joint", "owner": "arm-1", "array": list(child["array"])}
            )

        result, _source, output = self._prepare(mutate=alias)
        self.assertTrue(result.passed, result.findings)
        self.assertIn("discovery.jcs_alias", [finding["code"] for finding in result.findings])
        self.assertTrue(self._verify(output)["passed"])

        def mismatch(payload):
            child = next(datum for datum in payload["datums"] if datum["name"] == "CS_arm_link")
            payload["datums"].append(
                {
                    "name": "JCS_shoulder_pitch_joint",
                    "owner": "arm-1",
                    "array": _translated(0.05, 0.0, 0.1),
                }
            )
            del child

        result, _source, _output = self._prepare(mutate=mismatch)
        self.assertIn("discovery.jcs_mismatch", self._codes(result))

    def test_body_marker_is_not_an_accepted_membership_channel(self):
        def mutate(payload):
            payload["properties"]["components"] = {"arm-1": {"dp.body_marker": "left"}}

        result, _source, _output = self._prepare(mutate=mutate)
        self.assertIn("discovery.body_marker_unsupported", self._codes(result))

    def test_explicit_body_datum_must_be_owned_by_the_body(self):
        def mutate(payload):
            payload["properties"]["components"] = {"arm-1": {"dp.body_datum": "CS_base_link"}}

        result, _source, _output = self._prepare(mutate=mutate)
        self.assertIn("discovery.body_datum_owner_mismatch", self._codes(result))

    def test_present_nontext_body_datum_blocks_instead_of_falling_back(self):
        def mutate(payload):
            raw = payload.get("raw", payload)
            raw["properties"]["components"] = {"arm-1": {"dp.body_datum": True}}

        result, _source, _output = self._prepare(mutate=mutate)
        self.assertIn("discovery.body_datum_invalid", self._codes(result))
        baseline, _source, baseline_output = self._prepare()
        self.assertTrue(baseline.passed, baseline.findings)
        report = self._verify_mutated_record(baseline_output, mutate)
        self.assertFalse(report["passed"])
        self.assertIn("discovery.body_rebuild", {item["code"] for item in report["errors"]})

    def test_duplicate_occurrence_cannot_overwrite_a_native_identity(self):
        with self.assertRaises(PipelineError):
            self._prepare(
                mutate=lambda payload: payload["components"].append(
                    copy.deepcopy(payload["components"][0])
                )
            )

    def test_duplicate_owned_datum_blocks_even_with_explicit_body_datum(self):
        def mutate(payload):
            payload["datums"].append(copy.deepcopy(payload["datums"][1]))
            payload["properties"]["components"] = {"arm-1": {"dp.body_datum": "CS_arm_link"}}

        result, _source, _output = self._prepare(mutate=mutate)
        self.assertIn("discovery.datum_identity_duplicate", self._codes(result))

    def test_oracle_requires_exactly_one_explicit_body_datum_match(self):
        result, _source, output = self._prepare()
        self.assertTrue(result.passed, result.findings)
        payload = json.loads((output / "discovery/native-discovery.json").read_text(encoding="utf-8"))
        arm_datum = copy.deepcopy(
            next(
                datum
                for datum in payload["raw"]["datums"]
                if datum["name"] == "CS_arm_link" and datum["owner"] == "arm-1"
            )
        )
        payload["raw"]["datums"].append(arm_datum)
        payload["raw"]["properties"]["components"] = {
            "arm-1": {"dp.body_datum": "CS_arm_link"},
        }
        with self.assertRaises(_Failure) as caught:
            rebuild_rigid_bodies(payload)
        self.assertEqual(caught.exception.code, "discovery.body_rebuild")
        self.assertEqual(caught.exception.detail["matches"], 2)

    def test_interface_suffix_is_corrected_by_the_shared_naming_rule(self):
        def mutate(payload):
            payload["datums"].append({"name": "TCP_Tool", "owner": "arm-1", "array": _translated(0.0, 0.0, 0.3)})

        result, _source, output = self._prepare(mutate=mutate)
        self.assertTrue(result.passed, result.findings)
        finding = next(item for item in result.findings if item["code"] == "discovery.interface_name_invalid")
        self.assertFalse(finding["blocking"])
        self.assertEqual(finding["detail"]["output"], "tool_tcp")
        self.assertTrue(self._verify(output)["passed"])

    def test_interface_frame_purpose_suffix_avoids_a_body_name_collision(self):
        def mutate(payload):
            payload["datums"].append({"name": "TCP_base_link", "owner": "base-1", "array": _translated(0.0, 0.0, 0.3)})

        result, _source, _output = self._prepare(mutate=mutate)
        self.assertTrue(result.passed, result.findings)
        finding = next(item for item in result.findings if item["code"] == "discovery.interface_name_invalid")
        self.assertFalse(finding["blocking"])
        self.assertEqual(finding["detail"]["output"], "base_link_tcp")

    def test_interface_frame_collision_is_independent_of_datum_order(self):
        bodies = [
            {
                "name": "arm_link",
                "owners": ["arm-1"],
                "datum": "CS_arm_link",
                "datum_owner": "arm-1",
            }
        ]
        clusters = _Clusters(members={"arm_link": ["arm-1"]}, of={"arm-1": "arm_link"}, pairs={})

        def mapping(order):
            record = {
                "datums": [
                    {"name": name, "owner": "arm-1"}
                    for name in order
                ]
            }
            findings: list[dict] = []
            frames = _interface_frames(record, clusters, bodies, None, findings)
            self.assertFalse([item for item in findings if item["blocking"]], findings)
            self.assertEqual(
                {item["name"]: item["source_name"] for item in frames},
                {
                    item["name"]: item["coordinate_system"]
                    for item in frames
                },
            )
            return {item["coordinate_system"]: item["name"] for item in frames}

        first = mapping(["TCP_Tool", "CS_tool_tcp"])
        reordered = mapping(["CS_tool_tcp", "TCP_Tool"])
        self.assertEqual(first, reordered)
        self.assertEqual(len(set(first.values())), 2)
        self.assertTrue(all(name.endswith("_tcp") for name in first.values()))

    def test_interface_frame_collision_replays_after_datum_reordering(self):
        def prepared(order):
            def mutate(payload):
                for name in order:
                    payload["datums"].append(
                        {"name": name, "owner": "arm-1", "array": _translated(0.0, 0.0, 0.3)}
                    )

            result, _source, output = self._prepare(mutate=mutate)
            self.assertTrue(result.passed, result.findings)
            self.assertTrue(self._verify(output)["passed"])
            payload = json.loads((output / "discovery/native-discovery.json").read_text(encoding="utf-8"))
            return {
                frame["coordinate_system"]: frame["name"]
                for frame in rebuild_interface_frames(payload)
                if frame["coordinate_system"] in {"TCP_Tool", "SCS_tool_tcp"}
            }

        first = prepared(["TCP_Tool", "SCS_tool_tcp"])
        reordered = prepared(["SCS_tool_tcp", "TCP_Tool"])
        self.assertEqual(first, reordered)
        self.assertEqual(len(set(first.values())), 2)
        self.assertTrue(all(name.endswith("_tcp") for name in first.values()))

    def test_duplicate_interface_frame_identity_blocks_before_generation(self):
        def mutate(payload):
            datum = {"name": "TCP_Tool", "owner": "arm-1", "array": _translated(0.0, 0.0, 0.3)}
            payload["datums"].extend([copy.deepcopy(datum), copy.deepcopy(datum)])

        result, _source, _output = self._prepare(mutate=mutate)
        self.assertIn("discovery.interface_name_duplicate", self._codes(result))

    def test_interface_frame_configuration_mismatch_blocks_before_generation(self):
        def mutate(payload):
            payload["datums"].append(
                {
                    "name": "TCP_Tool",
                    "owner": "arm-1",
                    "configuration": "Other",
                    "array": _translated(0.0, 0.0, 0.3),
                }
            )

        result, _source, _output = self._prepare(mutate=mutate)
        self.assertIn("discovery.interface_configuration_mismatch", self._codes(result))

    def test_two_owned_link_datums_need_an_explicit_body_datum(self):
        def mutate(payload):
            payload["datums"].append(
                {"name": "CS_arm_link_backup", "owner": "arm-1", "array": _translated(0.0, 0.0, 0.2)}
            )

        result, _source, _output = self._prepare(mutate=mutate)
        self.assertIn("discovery.link_name_conflict", self._codes(result))

    def test_missing_axis_sign_blocks(self):
        result, _source, _output = self._prepare(
            mutate=lambda payload: payload["properties"]["mates"]["shoulder_pitch_joint__coaxial"].pop(
                "dp.joint.axis_sign"
            )
        )
        self.assertIn("discovery.joint_axis_sign_missing", self._codes(result))

    def test_native_limits_override_the_saved_export_placeholder(self):
        def mutate(payload):
            payload["mates"][0]["limits"] = {"lower": -2.0, "upper": 2.0, "unit": "rad"}

        result, _source, output = self._prepare(mutate=mutate)
        self.assertTrue(result.passed, result.findings)
        payload = json.loads((output / "discovery/native-discovery.json").read_text())
        joint = rebuild_joint_facts(payload, output, purpose="engineering")[0]
        self.assertEqual(joint["limits"]["lower"], -2.0)
        self.assertEqual(joint["limits"]["upper"], 2.0)
        self.assertEqual(joint["limit_source"], "native")

    def test_axis_sign_rejects_boolean_and_float_values(self):
        for value in (True, False, 1.0, -1.0):
            with self.subTest(value=value):
                result, _source, _output = self._prepare(
                    mutate=lambda payload, value=value: payload["properties"]["mates"][
                        "shoulder_pitch_joint__coaxial"
                    ].update({"dp.joint.axis_sign": value})
                )
                self.assertIn("discovery.joint_axis_sign_missing", self._codes(result))

    def test_author_negative_axis_sign_is_applied_with_warning(self):
        baseline, _source, baseline_output = self._prepare()
        self.assertTrue(baseline.passed, baseline.findings)
        baseline_payload = json.loads((baseline_output / "discovery/native-discovery.json").read_text())
        plus = rebuild_joint_facts(baseline_payload, baseline_output, purpose="engineering")[0]

        result, _source, output = self._prepare(
            mutate=lambda payload: payload["properties"]["mates"][
                "shoulder_pitch_joint__coaxial"
            ].update({"dp.joint.axis_sign": "-1"})
        )
        self.assertTrue(result.passed, result.findings)
        reversal = next(
            finding
            for finding in result.findings
            if finding["code"] == "discovery.direction_sign_reversed"
        )
        self.assertEqual(
            reversal["detail"]["native_identity"], "mate:shoulder_pitch_joint__coaxial"
        )
        self.assertEqual(reversal["detail"]["before_after"]["q_sign"], 1)
        self.assertEqual(reversal["detail"]["before_after"]["range_transform"], "not_applied")
        self.assertTrue(reversal["detail"]["warning_does_not_replace_evidence"])
        payload = json.loads((output / "discovery/native-discovery.json").read_text())
        minus = rebuild_joint_facts(payload, output, purpose="engineering")[0]
        self.assertEqual(minus["axis"], [-value for value in plus["axis"]])
        self.assertEqual(minus["limits"], plus["limits"])

    def test_author_negative_axis_sign_keeps_asymmetric_native_limits(self):
        def limits(payload):
            payload["mates"][0]["limits"] = {"lower": -0.3, "upper": 0.7, "unit": "rad"}

        baseline, _source, baseline_output = self._prepare(mutate=limits)
        self.assertTrue(baseline.passed, baseline.findings)
        baseline_payload = json.loads((baseline_output / "discovery/native-discovery.json").read_text())
        plus = rebuild_joint_facts(baseline_payload, baseline_output, purpose="engineering")[0]

        def reversed_limits(payload):
            limits(payload)
            payload["properties"]["mates"]["shoulder_pitch_joint__coaxial"].update(
                {"dp.joint.axis_sign": "-1"}
            )

        result, _source, output = self._prepare(mutate=reversed_limits)
        self.assertTrue(result.passed, result.findings)
        payload = json.loads((output / "discovery/native-discovery.json").read_text())
        minus = rebuild_joint_facts(payload, output, purpose="engineering")[0]
        self.assertEqual(minus["axis"], [-value for value in plus["axis"]])
        self.assertEqual(
            {key: minus["limits"][key] for key in ("lower", "upper")},
            {"lower": -0.3, "upper": 0.7},
        )
        self.assertEqual(minus["limits"], plus["limits"])

    def test_rebuild_carrier_snapshot_binds_author_negative_sign(self):
        def reversed_limits(payload):
            payload["mates"][0]["limits"] = {"lower": -0.3, "upper": 0.7, "unit": "rad"}
            payload["properties"]["mates"]["shoulder_pitch_joint__coaxial"].update(
                {"dp.joint.axis_sign": "-1"}
            )

        result, _source, output = self._prepare(mutate=reversed_limits)
        self.assertTrue(result.passed, result.findings)
        payload = json.loads((output / "discovery/native-discovery.json").read_text())
        raw = payload.get("raw", payload)
        carrier = _carrier_snapshot(
            None,
            rebuild_rigid_bodies(payload),
            rebuild_joint_facts(payload, output, purpose="engineering"),
            raw,
        )
        joint = next(
            row for row in carrier["joints"] if row["name"] == "shoulder_pitch_joint"
        )
        self.assertEqual(joint["axis_sign"], -1)

    def test_carrier_snapshot_binds_declaration_axis_sign_from_raw_properties(self):
        joint = {"name": "shoulder_pitch_joint"}
        carrier = _carrier_snapshot(
            None,
            [],
            [joint],
            {"properties": {"document": {"dp.joint.shoulder_pitch_joint.axis_sign": "-1"}}},
        )
        self.assertEqual(carrier["joints"][0]["axis_sign"], -1)

    def test_carrier_axis_sign_owner_scopes_match_producer_and_independent_verifier(self):
        joint_name = "hinge_joint"
        mate_name = "hinge_joint__coaxial"
        direct_key = f"dp.joint.{joint_name}.axis_sign"
        mate_key = "dp.joint.axis_sign"

        def root_shape(payload):
            source_mate = "shoulder_pitch_joint__coaxial"
            payload["mates"][0]["name"] = mate_name
            payload["mates"][1]["name"] = f"{joint_name}__locate"
            payload["properties"]["mates"][mate_name] = payload["properties"]["mates"].pop(
                source_mate
            )

        def prepare(mutate):
            def mutate_root(payload):
                root_shape(payload)
                mutate(payload)

            result, _source, output = self._prepare(mutate=mutate_root)
            self.assertTrue(result.passed, result.findings)
            report = self._verify(output)
            self.assertTrue(report["passed"], report["errors"])
            payload = json.loads(
                (output / "discovery/native-discovery.json").read_text(encoding="utf-8")
            )
            raw = payload.get("raw", payload)
            joints = rebuild_joint_facts(payload, output, purpose="engineering")
            carrier = _carrier_snapshot(
                None,
                rebuild_rigid_bodies(payload),
                joints,
                raw,
            )
            return joints[0], carrier["joints"][0]

        def unsupported_mate_direct(payload):
            payload["properties"]["document"][direct_key] = "+1"
            payload["properties"]["mates"][mate_name][direct_key] = "-1"

        joint, carrier_joint = prepare(unsupported_mate_direct)
        self.assertEqual(joint["axis"], [0.0, 0.0, 1.0])
        self.assertEqual(carrier_joint["axis_sign"], 1)

        for owner in ("document", "mate"):

            def supported_owner(payload, owner=owner):
                payload["properties"]["document"].pop(direct_key, None)
                payload["properties"]["mates"][mate_name][mate_key] = "+1"
                if owner == "document":
                    payload["properties"]["mates"][mate_name].pop(mate_key)
                    payload["properties"]["document"][direct_key] = "-1"
                else:
                    payload["properties"]["mates"][mate_name][mate_key] = "-1"

            with self.subTest(owner=owner):
                joint, carrier_joint = prepare(supported_owner)
                self.assertEqual(joint["axis"], [0.0, 0.0, -1.0])
                self.assertEqual(carrier_joint["axis_sign"], -1)

        def owner_conflict(payload):
            payload["properties"]["document"][direct_key] = "-1"
            payload["properties"]["mates"][mate_name][mate_key] = "+1"

        def root_owner_conflict(payload):
            root_shape(payload)
            owner_conflict(payload)

        result, _source, _output = self._prepare(mutate=root_owner_conflict)
        self.assertIn("discovery.joint_property_conflict", self._codes(result))

        baseline, _source, baseline_output = self._prepare(mutate=root_shape)
        self.assertTrue(baseline.passed, baseline.findings)
        report = self._verify_mutated_record(
            baseline_output,
            lambda payload: owner_conflict(payload["raw"]),
        )
        self.assertFalse(report["passed"])
        raw = record()
        root_shape(raw)
        owner_conflict(raw)
        with self.assertRaises(PipelineError):
            _carrier_snapshot(
                None,
                [],
                [
                    {
                        "name": joint_name,
                        "mate": mate_name,
                        "mates": [mate_name],
                    }
                ],
                raw,
            )

    def test_carrier_snapshot_scope_and_type_boundaries(self):
        joint = {"name": "arm_joint"}

        def sign(raw, candidate=joint):
            return _carrier_snapshot(None, [], [candidate], raw)["joints"][0]["axis_sign"]

        self.assertIsNone(
            sign({"properties": {"components": {"part": {"dp.joint.axis_sign": "-1"}}}})
        )
        self.assertEqual(
            sign(
                {
                    "properties": {
                        "document": {"dp.joint.arm_joint.axis_sign": "-1"},
                        "components": {"part": {"dp.joint.axis_sign": "+1"}},
                    }
                }
            ),
            -1,
        )
        self.assertIsNone(
            sign(
                {
                    "properties": {
                        "components": {
                            "arm_joint": {"dp.joint.arm_joint.axis_sign": "-1"}
                        }
                    }
                }
            )
        )
        self.assertIsNone(
            sign({"properties": {"document": {"dp.joint.other_joint.axis_sign": "-1"}}})
        )
        for value in (True, False, -1.0, 1.0):
            with self.subTest(value=value), self.assertRaises(PipelineError):
                sign({"properties": {"document": {"dp.joint.arm_joint.axis_sign": value}}})
        with self.assertRaises(PipelineError):
            sign(
                {
                    "properties": {
                        "mates": {
                            "arm_joint__a": {"dp.joint.axis_sign": "-1"},
                            "arm_joint__b": {"dp.joint.axis_sign": "+1"},
                        }
                    }
                },
                {
                    "name": "arm_joint",
                    "mate": "arm_joint__a",
                    "mates": ["arm_joint__a", "arm_joint__b"],
                },
            )
        with self.assertRaises(PipelineError):
            _carrier_snapshot(
                None,
                [],
                [
                    {"name": "arm_joint", "mate": "m1"},
                    {"name": "arm_joint", "mate": "m2"},
                ],
                {
                    "properties": {
                        "mates": {
                            "m1": {"dp.joint.axis_sign": "-1"},
                            "m2": {"dp.joint.axis_sign": "+1"},
                        }
                    }
                },
            )

    def test_native_limit_record_must_be_finite(self):
        def mutate(payload):
            payload["mates"][0]["limits"] = {"lower": -1.5, "upper": 1.5, "unit": "rad"}
            path = self.tmp / "records" / "joints" / "arm.json"
            values = json.loads(path.read_text(encoding="utf-8"))
            values["limits"]["lower"] = float("nan")
            path.write_text(json.dumps(values), encoding="utf-8")

        result, _source, _output = self._prepare(mutate=mutate)
        self.assertIn("discovery.record_unreadable", self._codes(result))
        json.dumps(list(result.findings), allow_nan=False)

    def test_circle_edge_coincident_keeps_the_hinge(self):
        def mutate(payload):
            seat = payload["mates"][1]
            seat["entities"][0] = {
                "component": "base-1",
                "feature": "Edge1",
                "circle": {"center": [0.0, 0.0, 0.1], "normal": [0.0, 0.0, 1.0], "radius": 0.006},
            }
            seat["entities"][1] = {
                "component": "arm-1",
                "feature": "Edge2",
                "circle": {"center": [0.0, 0.0, 0.1], "normal": [0.0, 0.0, 1.0], "radius": 0.006},
            }

        result, _source, output = self._prepare(mutate=mutate)
        self.assertTrue(result.passed, result.findings)
        payload = json.loads((output / "discovery" / "native-discovery.json").read_text(encoding="utf-8"))
        joint = rebuild_joint_facts(payload, output, purpose="engineering")[0]
        self.assertEqual(joint["type"], "revolute")
        self.assertEqual(joint["axis"], [0.0, 0.0, 1.0])
        self.assertTrue(self._verify(output)["passed"])

    def test_nonparallel_circle_edges_block(self):
        def mutate(payload):
            seat = payload["mates"][1]
            seat["entities"][0] = {
                "component": "base-1",
                "feature": "Edge1",
                "circle": {"center": [0.0, 0.0, 0.1], "normal": [0.0, 0.0, 1.0], "radius": 0.006},
            }
            seat["entities"][1] = {
                "component": "arm-1",
                "feature": "Edge2",
                "circle": {"center": [0.0, 0.0, 0.1], "normal": [1.0, 0.0, 0.0], "radius": 0.006},
            }

        result, _source, _output = self._prepare(mutate=mutate)
        self.assertIn("discovery.mate_geometry_mismatch", self._codes(result))

    def test_concentric_accepts_circular_edges_as_the_axis(self):
        findings: list[dict] = []
        frames = _component_frames(record(), findings)
        self.assertFalse(findings)

        def mate(entities) -> dict:
            return {
                "name": "shaft",
                "type": "concentric",
                "entity_count": 2,
                "suppressed": False,
                "error_code": 0,
                "scope": "",
                "limits": None,
                "entities": entities,
            }

        cylinder = {
            "component": "base-1",
            "feature": "Cyl1",
            "cylinder": {"point": [0.0, 0.0, 0.1], "direction": [0.0, 0.0, 1.0], "radius": 0.006},
        }
        circle = {
            "component": "arm-1",
            "feature": "Edge1",
            "circle": {"center": [0.0, 0.0, 0.1], "normal": [0.0, 0.0, 1.0], "radius": 0.006},
        }
        flipped = {
            "component": "arm-1",
            "feature": "Edge1",
            "circle": {"center": [0.0, 0.0, 0.1], "normal": [0.0, 0.0, -1.0], "radius": 0.006},
        }
        far_cylinder = {
            "component": "arm-1",
            "feature": "Cyl2",
            "cylinder": {"point": [0.0, 0.0, 0.2], "direction": [0.0, 0.0, 1.0], "radius": 0.006},
        }

        baseline = _mate_rows(mate([cylinder, cylinder]), frames, [], "mate:shaft")
        self.assertIsNotNone(baseline)
        circle_second = _mate_rows(mate([cylinder, circle]), frames, [], "mate:shaft")
        circle_first = _mate_rows(mate([circle, far_cylinder]), frames, [], "mate:shaft")
        circle_only = _mate_rows(mate([circle, circle]), frames, [], "mate:shaft")
        flipped_second = _mate_rows(mate([cylinder, flipped]), frames, [], "mate:shaft")
        for rows in (circle_second, circle_first, circle_only, flipped_second):
            self.assertIsNotNone(rows)
            self.assertEqual(rows["rows"], baseline["rows"])

        # Shaft evidence is the actual cylinder, never a circle normal: a flipped
        # circle-first normal must not change the returned axis or its point.
        self.assertEqual(circle_second["axis"], baseline["axis"])
        self.assertEqual(circle_second["point"], baseline["point"])
        self.assertEqual(circle_second["entity"]["component"], "base-1")
        self.assertEqual(circle_first["axis"], [0.0, 0.0, 1.0])
        self.assertEqual(circle_first["point"], [0.0, 0.0, 0.2])
        self.assertEqual(circle_first["entity"]["component"], "arm-1")
        self.assertEqual(flipped_second["axis"], baseline["axis"])

        # Circle-only reconstruction carries rows but no cylinder evidence at all.
        self.assertIsNone(circle_only["axis"])
        self.assertIsNone(circle_only["point"])
        self.assertIsNone(circle_only["entity"])

    def test_multi_entity_mate_is_rejected_without_pair_expansion(self):
        frames = _component_frames(record(), [])
        entity = {
            "component": "base-1",
            "feature": "Cyl1",
            "cylinder": {
                "point": [0.0, 0.0, 0.1],
                "direction": [0.0, 0.0, 1.0],
                "radius": 0.006,
            },
        }
        mate = {
            "name": "three_entities",
            "type": "concentric",
            "entity_count": 3,
            "suppressed": False,
            "error_code": 0,
            "scope": "",
            "limits": None,
            "entities": [entity, entity, entity],
        }
        findings = []

        self.assertIsNone(_mate_rows(mate, frames, findings, "mate:three_entities"))
        self.assertEqual(_mate_pairs(mate), [])
        self.assertEqual(findings[0]["code"], "discovery.mate_entities_unsupported")
        self.assertEqual(findings[0]["detail"]["entity_count"], 3)

    def test_is_fixed_must_be_a_native_boolean(self):
        result, _source, _output = self._prepare(
            mutate=lambda payload: payload["components"][0].update(
                {"fixed": 1, "fixed_error": {"code": "cad_component_fixed_invalid"}}
            )
        )
        finding = next(
            item
            for item in result.findings
            if item["code"] == "discovery.component_fixed_invalid"
        )
        self.assertEqual(finding["object"], "component:base-1")
        self.assertFalse(result.passed)

    def test_concentric_circle_geometry_gates_stay_fail_closed(self):
        frames = _component_frames(record(), [])
        base = {
            "component": "base-1",
            "feature": "Cyl1",
            "cylinder": {"point": [0.0, 0.0, 0.1], "direction": [0.0, 0.0, 1.0], "radius": 0.006},
        }

        def run(circle: dict):
            findings: list[dict] = []
            rows = _mate_rows(
                {
                    "name": "shaft",
                    "type": "concentric",
                    "entity_count": 2,
                    "suppressed": False,
                    "error_code": 0,
                    "scope": "",
                    "limits": None,
                    "entities": [base, {"component": "arm-1", "feature": "Edge1", "circle": circle}],
                },
                frames,
                findings,
                "mate:shaft",
            )
            return rows, findings

        rows, findings = run({"center": [0.0, 0.0, 0.1], "normal": [0.0, 1.0, 0.0], "radius": 0.006})
        self.assertIsNone(rows)
        self.assertTrue(any(item["code"] == "discovery.mate_geometry_mismatch" for item in findings))

        rows, findings = run({"center": [0.001, 0.0, 0.1], "normal": [0.0, 0.0, 1.0], "radius": 0.006})
        self.assertIsNone(rows)
        misaligned = next(item for item in findings if item["code"] == "discovery.joint_axis_misaligned")
        self.assertAlmostEqual(misaligned["detail"]["radial_gap_m"], 0.001)

        for broken in (
            {"center": [0.0, 0.0, 0.1], "radius": 0.006},
            {"center": [0.0, 0.0, 0.1], "normal": [0.0, 0.0, 0.0], "radius": 0.006},
            {"normal": [0.0, 0.0, 1.0], "radius": 0.006},
        ):
            rows, findings = run(broken)
            self.assertIsNone(rows)
            self.assertTrue(any(item["code"] == "discovery.mate_entities_unsupported" for item in findings), findings)

    def test_coincident_circle_with_plane_locks_alignment_and_position(self):
        frames = _component_frames(record(), [])
        plane = {
            "component": "base-1",
            "feature": "Plane1",
            "plane": {"point": [0.0, 0.0, 0.1], "normal": [0.0, 0.0, 1.0]},
        }
        other_plane = {
            "component": "arm-1",
            "feature": "Plane2",
            "plane": {"point": [0.0, 0.0, 0.1], "normal": [0.0, 0.0, 1.0]},
        }
        circle = {
            "component": "arm-1",
            "feature": "Edge1",
            "circle": {"center": [0.0, 0.0, 0.1], "normal": [0.0, 0.0, 1.0], "radius": 0.006},
        }

        def coincident(entities) -> dict:
            return {
                "name": "seat",
                "type": "coincident",
                "entity_count": 2,
                "suppressed": False,
                "error_code": 0,
                "scope": "",
                "limits": None,
                "entities": entities,
            }

        plane_plane = _mate_rows(coincident([plane, other_plane]), frames, [], "mate:seat")
        circle_plane = _mate_rows(coincident([plane, circle]), frames, [], "mate:seat")
        mirrored = _mate_rows(
            coincident(
                [
                    {"component": "base-1", "feature": "Edge1", "circle": circle["circle"]},
                    {"component": "arm-1", "feature": "Plane2", "plane": other_plane["plane"]},
                ]
            ),
            frames,
            [],
            "mate:seat",
        )
        self.assertEqual(len(circle_plane["rows"]), 3)
        self.assertEqual(circle_plane["rows"], plane_plane["rows"])
        self.assertEqual(mirrored["rows"], plane_plane["rows"])
        self.assertEqual(circle_plane["point"], [0.0, 0.0, 0.1])

        anti_parallel = {**circle, "circle": {"center": [0.0, 0.0, 0.1], "normal": [0.0, 0.0, -1.0], "radius": 0.006}}
        flipped = _mate_rows(coincident([plane, anti_parallel]), frames, [], "mate:seat")
        self.assertEqual(len(flipped["rows"]), 3)
        self.assertAlmostEqual(abs(flipped["rows"][0][2]), 1.0)

    def test_coincident_circle_plane_geometry_gates_stay_fail_closed(self):
        frames = _component_frames(record(), [])
        plane = {
            "component": "base-1",
            "feature": "Plane1",
            "plane": {"point": [0.0, 0.0, 0.1], "normal": [0.0, 0.0, 1.0]},
        }

        def run(circle: dict):
            findings: list[dict] = []
            rows = _mate_rows(
                {
                    "name": "seat",
                    "type": "coincident",
                    "entity_count": 2,
                    "suppressed": False,
                    "error_code": 0,
                    "scope": "",
                    "limits": None,
                    "entities": [plane, {"component": "arm-1", "feature": "Edge1", "circle": circle}],
                },
                frames,
                findings,
                "mate:seat",
            )
            return rows, findings

        rows, findings = run({"center": [0.0, 0.0, 0.1], "normal": [0.0, 1.0, 0.0], "radius": 0.006})
        self.assertIsNone(rows)
        self.assertTrue(any(item["code"] == "discovery.mate_geometry_mismatch" for item in findings))

        rows, findings = run({"center": [0.0, 0.0, 0.1005], "normal": [0.0, 0.0, 1.0], "radius": 0.006})
        self.assertIsNone(rows)
        mismatch = next(item for item in findings if item["code"] == "discovery.mate_geometry_mismatch")
        self.assertAlmostEqual(mismatch["detail"]["separation_m"], 0.0005)

        rows, findings = run({"center": [0.0, 0.0, 0.1], "radius": 0.006})
        self.assertIsNone(rows)
        self.assertTrue(any(item["code"] == "discovery.mate_entities_unsupported" for item in findings))

    def test_hinge_recorded_with_circular_edges_generates_the_same_joint(self):
        def mutate(payload):
            payload["mates"][0]["entities"][0] = {
                "component": "base-1",
                "feature": "Edge1",
                "circle": {"center": [0.0, 0.0, 0.1], "normal": [0.0, 0.0, 1.0], "radius": 0.006},
            }
            payload["mates"][1]["entities"][0] = {
                "component": "base-1",
                "feature": "Edge2",
                "circle": {"center": [0.0, 0.0, 0.1], "normal": [0.0, 0.0, 1.0], "radius": 0.006},
            }

        result, _source, output = self._prepare(mutate=mutate)
        self.assertTrue(result.passed, result.findings)
        payload = json.loads((output / "discovery" / "native-discovery.json").read_text(encoding="utf-8"))
        joint = rebuild_joint_facts(payload, output, purpose="engineering")[0]
        self.assertEqual(joint["type"], "revolute")
        self.assertEqual(joint["axis"], [0.0, 0.0, 1.0])
        self.assertEqual((joint["parent"], joint["child"]), ("base_link", "arm_link"))
        # Cylinder evidence is required for the published axis reference; the circle
        # supplies rows, never the interface record.
        self.assertEqual(joint["axis_reference"]["component"], "arm-1")
        # The independent verifier's row model pins circle support to C's parity
        # patch, so verification is asserted in the combined tree, not here.

    def test_circle_only_joint_still_fails_the_cylinder_shaft_gate(self):
        def mutate(payload):
            payload["mates"][0]["entities"] = [
                {
                    "component": "base-1",
                    "feature": "Edge1",
                    "circle": {"center": [0.0, 0.0, 0.1], "normal": [0.0, 0.0, 1.0], "radius": 0.006},
                },
                {
                    "component": "arm-1",
                    "feature": "Edge2",
                    "circle": {"center": [0.0, 0.0, 0.1], "normal": [0.0, 0.0, 1.0], "radius": 0.006},
                },
            ]
            payload["mates"][1]["entities"] = [
                {
                    "component": "base-1",
                    "feature": "Edge3",
                    "circle": {"center": [0.0, 0.0, 0.1], "normal": [0.0, 0.0, 1.0], "radius": 0.006},
                },
                {
                    "component": "arm-1",
                    "feature": "Edge4",
                    "circle": {"center": [0.0, 0.0, 0.1], "normal": [0.0, 0.0, 1.0], "radius": 0.006},
                },
            ]

        result, _source, _output = self._prepare(mutate=mutate)
        self.assertIn("discovery.joint_axis_selector_missing", self._codes(result))
        finding = next(item for item in result.findings if item["code"] == "discovery.joint_axis_selector_missing")
        self.assertIn("no cylindrical mate entity carries the joint shaft", finding["message"])

    def test_missing_or_nonzero_native_error_state_blocks(self):
        for payload_mutate in (
            lambda payload: payload["mates"][0].pop("error_code"),
            lambda payload: payload["mates"][0].update({"error_code": 5}),
        ):
            result, _source, _output = self._prepare(mutate=payload_mutate)
            self.assertIn("discovery.mate_error_state", self._codes(result))

    def test_suppressed_mate_is_ignored(self):
        def mutate(payload):
            payload["mates"][1]["suppressed"] = True

        result, _source, _output = self._prepare(mutate=mutate)
        self.assertFalse(result.passed)
        self.assertIn("discovery.joint_unsupported_pattern", self._codes(result))

    # ------------------------------------------------------------ verifier gates

    def test_verifier_rejects_deleted_or_stripped_metadata(self):
        result, _source, output = self._prepare()
        self.assertTrue(result.passed)
        without_record = self._deliver(output)
        (
            without_record
            / "input/discovery/native-discovery.json"
        ).unlink()
        report = verify_discovery(without_record)
        self.assertFalse(report["passed"])
        self.assertIn("discovery.file", [item["code"] for item in report["errors"]])
        frozen = self._deliver(output)
        frozen_record = json.loads(
            (
                frozen / "input/discovery/native-discovery.json"
            ).read_text(encoding="utf-8")
        )
        # One current model: nothing is sealed beside the record the verifier rebuilds from.
        self.assertNotIn("derived", frozen_record)

    def test_gate_is_mandatory_and_has_no_legacy_mode(self):
        result, _source, output = self._prepare()
        self.assertTrue(result.passed)
        self.assertEqual(_native_discovery(self._deliver(output))["bodies"], 2)
        authored = self.tmp / "authored"
        authored.mkdir()
        (authored / "source.json").write_text("schema_version: x\n", encoding="utf-8")
        with self.assertRaises(PipelineError):
            _native_discovery(authored)

    def test_gate_rejects_a_tampered_record(self):
        result, _source, output = self._prepare()
        self.assertTrue(result.passed)
        delivery = self._deliver(output)
        path = delivery / "input/discovery/native-discovery.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["raw"]["mates"][0]["entities"][0]["cylinder"]["radius"] = -1.0
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        support.seal_delivery(delivery)
        with self.assertRaises(PipelineError):
            _native_discovery(delivery)

    # ------------------------------------------------ identity chain (IF-06)

    def _derive_pure(self, raw: dict, submission=None):
        """Derive one package straight from a raw record (the probe's producer path)."""

        native, records = self._native()
        output = self.tmp / f"prepared{self.serial}"
        if all(
            "transform_raw" not in item
            for item in raw.get("components") or []
            if isinstance(item, dict)
        ):
            raw = support.local_native_record_payload(
                raw,
                default_mate_scope=True,
            )
        return (
            derive_native_package(
                native,
                output,
                "run-1",
                raw,
                settings=DiscoverySettings(record_roots=(records,)),
                submission_identity=submission,
            ),
            output,
        )

    def _tamper_delivered(self, delivery: Path, relative: str, mutate) -> None:
        path = delivery / relative
        payload = json.loads(path.read_text(encoding="utf-8"))
        mutate(payload)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        support.seal_delivery(delivery)

    def test_configured_target_and_submitted_summary_verify_end_to_end(self):
        raw = record()
        target = raw["identity"]["hardware_id"]
        raw["identity"].pop("hardware_id")
        raw["identity"].pop("change_summary")
        result, output = self._derive_pure(
            raw,
            submission=SubmissionIdentity(hardware_id=target, change_summary="提交说明\n第二行"),
        )
        self.assertTrue(result.passed, result.findings)
        source_record = json.loads((output / "source.json").read_text(encoding="utf-8"))
        self.assertEqual(source_record["selection"]["hardware_source"], "platform-config")
        self.assertEqual(source_record["metadata"]["change_summary_source"], "submission")
        self.assertEqual(source_record["metadata"]["change_summary"], "提交说明\n第二行")
        report = verify_discovery(self._deliver(output))
        self.assertTrue(report["passed"], report["errors"])

    def test_verifier_recomputes_owner_and_control_source_metadata(self):
        result, _source, output = self._prepare()
        self.assertTrue(result.passed, result.findings)
        for field, value in (
            ("owner", "forged_owner"),
            (
                "control",
                {
                    "system": "git",
                    "reference": "0" * 40,
                },
            ),
        ):
            with self.subTest(field=field):
                delivery = self._deliver(output)

                def forge(source_record: dict, field=field, value=value) -> None:
                    source_record["metadata"][field] = value

                self._tamper_delivered(delivery, "input/source.json", forge)
                report = verify_discovery(delivery)
                self.assertFalse(report["passed"])
                self.assertIn("discovery.identity", [item["code"] for item in report["errors"]])

    def test_verifier_checks_revision_even_when_other_metadata_is_missing(self):
        raw = record()
        raw["identity"].pop("owner")
        result, output = self._derive_pure(raw)
        self.assertTrue(result.passed, result.findings)
        delivery = self._deliver(output)

        def forge(source_record: dict) -> None:
            source_record["metadata"]["revision"] = "forged_revision"

        self._tamper_delivered(delivery, "input/source.json", forge)
        report = verify_discovery(delivery)
        self.assertFalse(report["passed"])
        self.assertIn("discovery.revision", [item["code"] for item in report["errors"]])

    def test_verifier_recomputes_an_absent_change_summary_and_its_origin(self):
        raw = record()
        raw["identity"].pop("change_summary")
        result, output = self._derive_pure(raw)
        self.assertTrue(result.passed, result.findings)
        for field, value, detail in (
            ("change_summary", "forged summary", "change_summary"),
            ("change_summary_source", "cad", "change_summary_source"),
        ):
            with self.subTest(field=field):
                delivery = self._deliver(output)

                def forge(source_record: dict, field=field, value=value) -> None:
                    source_record["metadata"][field] = value

                self._tamper_delivered(delivery, "input/source.json", forge)
                report = verify_discovery(delivery)
                self.assertFalse(report["passed"])
                failure = next(item for item in report["errors"] if item["code"] == "discovery.identity")
                self.assertEqual(failure["detail"]["field"], detail)

    def test_verifier_refuses_a_configured_target_over_authored_cad_identity(self):
        result, _source, output = self._prepare()
        self.assertTrue(result.passed, result.findings)
        delivery = self._deliver(output)

        def relabel(source_record: dict) -> None:
            source_record["selection"]["hardware_source"] = "platform-config"

        self._tamper_delivered(delivery, "input/source.json", relabel)
        report = verify_discovery(delivery)
        self.assertFalse(report["passed"])
        self.assertIn("discovery.identity", [item["code"] for item in report["errors"]])

    def test_verifier_refuses_a_cad_label_when_the_cad_records_carry_no_value(self):
        raw = record()
        target = raw["identity"]["hardware_id"]
        raw["identity"].pop("hardware_id")
        result, output = self._derive_pure(
            raw,
            submission=SubmissionIdentity(hardware_id=target, change_summary="提交说明\n第二行"),
        )
        self.assertTrue(result.passed, result.findings)
        delivery = self._deliver(output)

        def relabel(source_record: dict) -> None:
            source_record["selection"]["hardware_source"] = "cad"

        self._tamper_delivered(delivery, "input/source.json", relabel)
        report = verify_discovery(delivery)
        self.assertFalse(report["passed"])
        self.assertIn("discovery.identity", [item["code"] for item in report["errors"]])

    def test_verifier_refuses_a_cad_claim_without_cad_evidence(self):
        result, _source, output = self._prepare()
        self.assertTrue(result.passed, result.findings)
        delivery = self._deliver(output)

        def drop_authored_hardware(payload: dict) -> None:
            payload["raw"]["identity"].pop("hardware_id")

        self._tamper_delivered(delivery, "input/discovery/native-discovery.json", drop_authored_hardware)
        report = verify_discovery(delivery)
        self.assertFalse(report["passed"])
        self.assertIn("discovery.identity", [item["code"] for item in report["errors"]])

    def test_verifier_refuses_a_claim_that_differs_from_the_sealed_target(self):
        raw = record()
        target = raw["identity"]["hardware_id"]
        raw["identity"].pop("hardware_id")
        result, output = self._derive_pure(
            raw,
            submission=SubmissionIdentity(hardware_id=target, change_summary="提交说明\n第二行"),
        )
        self.assertTrue(result.passed, result.findings)
        delivery = self._deliver(output)

        def rebind_hardware(payload: dict) -> None:
            payload["identity"]["hardware_id"] = "m9.0"

        self._tamper_delivered(delivery, "input/discovery/native-discovery.json", rebind_hardware)
        report = verify_discovery(delivery)
        self.assertFalse(report["passed"])
        self.assertIn("discovery.identity", [item["code"] for item in report["errors"]])

    def test_verifier_refuses_a_submitted_summary_outside_the_sealed_record(self):
        raw = record()
        raw["identity"].pop("change_summary")
        submission = SubmissionIdentity(
            hardware_id=raw["identity"]["hardware_id"], change_summary="提交说明\n第二行"
        )
        result, output = self._derive_pure(raw, submission=submission)
        self.assertTrue(result.passed, result.findings)
        delivery = self._deliver(output)

        def rebind_summary(payload: dict) -> None:
            payload["identity"]["change_summary"] = "被换掉的说明"

        self._tamper_delivered(delivery, "input/discovery/native-discovery.json", rebind_summary)
        report = verify_discovery(delivery)
        self.assertFalse(report["passed"])
        self.assertIn("discovery.identity", [item["code"] for item in report["errors"]])


    def test_verifier_requires_the_configuration_proof_without_a_generated_claim(self):
        result, _source, output = self._prepare()
        self.assertTrue(result.passed, result.findings)

        def mutation(claim: str, observation: str):
            def apply(payload: dict) -> None:
                if claim == "omitted":
                    payload["identity"].pop("delivery_configuration", None)
                elif claim == "null":
                    payload["identity"]["delivery_configuration"] = None
                if observation == "absent":
                    payload["raw"].pop("document_state", None)
                    payload["raw"].pop("saved_configuration", None)
                elif observation == "contradicting":
                    for side in ("before", "after"):
                        payload["raw"]["document_state"][side]["configuration"] = "Other"

            return apply

        for label, claim, observation in (
            ("omitted_claim_with_observations", "omitted", "kept"),
            ("omitted_claim_no_observation", "omitted", "absent"),
            ("null_claim_no_observation", "null", "absent"),
            ("omitted_claim_contradictory_observation", "omitted", "contradicting"),
        ):
            with self.subTest(case=label):
                delivery = self._deliver(output)
                self._tamper_delivered(delivery, "input/discovery/native-discovery.json", mutation(claim, observation))
                report = verify_discovery(delivery)
                self.assertFalse(report["passed"])
                self.assertTrue(
                    {
                        "discovery.binding",
                        "discovery.configuration",
                        "discovery.identity",
                        "discovery.mate_configuration",
                    }
                    & {item["code"] for item in report["errors"]}
                )

    def test_discovery_exact_keys_are_shared_by_rebuild_and_verifier(self) -> None:
        result, _source, output = self._prepare()
        self.assertTrue(result.passed, result.findings)
        mutations = (
            (
                "envelope",
                lambda payload: payload.__setitem__(
                    "unknown_envelope_field",
                    True,
                ),
            ),
            (
                "identity",
                lambda payload: payload["identity"].__setitem__(
                    "unknown_identity_field",
                    True,
                ),
            ),
            (
                "derivation",
                lambda payload: payload["derivation"].__setitem__(
                    "unknown_derivation_field",
                    True,
                ),
            ),
            (
                "record",
                lambda payload: payload["records"][0].__setitem__(
                    "unknown_record_field",
                    True,
                ),
            ),
        )
        for label, mutate in mutations:
            with self.subTest(label=label):
                delivery = self._deliver(output)
                self._tamper_delivered(
                    delivery,
                    "input/discovery/native-discovery.json",
                    mutate,
                )
                report = verify_discovery(delivery)
                self.assertFalse(report["passed"])
                self.assertIn(
                    "discovery.binding",
                    {item["code"] for item in report["errors"]},
                )
                with self.assertRaises(PipelineError):
                    source_definition(
                        delivery,
                        discovery_name=(
                            "input/discovery/native-discovery.json"
                        ),
                        cad_prefix="input/cad/",
                        records_prefix="input/",
                    )

    def test_verifier_separates_full_upload_and_selected_closure_handoff(self) -> None:
        raw = record()
        main = "cad/robot.SLDASM"
        selected = raw["source_selection"]["files"]
        upload = {**selected, "cad/excluded.SLDPRT": "f" * 64}
        raw["source_selection"] = sealed_selection(
            upload,
            main,
            edges={
                main: ["cad/base.SLDPRT", "cad/arm.SLDPRT"],
                "cad/base.SLDPRT": [],
                "cad/arm.SLDPRT": [],
            },
        )
        from .selection_support import seal_fixture_open_observation

        seal_fixture_open_observation(raw)
        result, output = self._derive_pure(raw)
        self.assertTrue(result.passed, result.findings)
        delivery = self._deliver(output)
        request = json.loads((delivery / "input/request.json").read_text(encoding="utf-8"))
        payload = json.loads((delivery / "input/discovery/native-discovery.json").read_text(encoding="utf-8"))
        upload_sha256 = payload["raw"]["source_selection"]["upload_sha256"]
        selected_sha256 = payload["raw"]["source_selection"]["handoff_sha256"]
        self.assertNotEqual(upload_sha256, selected_sha256)
        self.assertEqual(request["handoff_digest"], upload_sha256)
        source_record = json.loads((delivery / "input/source.json").read_text(encoding="utf-8"))
        self.assertEqual(source_record["identity"]["handoff_digest"], upload_sha256)

        self._tamper_delivered(
            delivery,
            "input/request.json",
            lambda value: value.__setitem__("handoff_digest", selected_sha256),
        )
        report = verify_discovery(delivery)
        self.assertFalse(report["passed"])
        self.assertIn("discovery.request", [item["code"] for item in report["errors"]])

        self._tamper_delivered(
            delivery,
            "input/request.json",
            lambda value: value.__setitem__("handoff_digest", upload_sha256),
        )
        report = verify_discovery(delivery)
        self.assertTrue(report["passed"], report["errors"])
        self.assertEqual(
            json.loads((delivery / "input/request.json").read_text(encoding="utf-8"))["handoff_digest"],
            upload_sha256,
        )
        self.assertEqual(payload["handoff_sha256"], selected_sha256)


    def _delivery_with_frozen_request(self, *, drop: tuple[str, ...]):
        """One fallback delivery whose request record is written before any tamper."""

        raw = record()
        target = raw["identity"]["hardware_id"]
        for field in drop:
            raw["identity"].pop(field)
        result, output = self._derive_pure(
            raw,
            submission=SubmissionIdentity(hardware_id=target, change_summary="真实提交说明\n第二行"),
        )
        self.assertTrue(result.passed, result.findings)
        delivery = self._deliver(output)
        payload = json.loads(
            (
                delivery / "input/discovery/native-discovery.json"
            ).read_text(encoding="utf-8")
        )
        original = json.dumps(
            {
                "schema_version": "solidworks-to-urdf.request/v2",
                "purpose": "engineering",
                "request_id": "89e1c51f-e6c9-4d01-9467-f3c403c081e1",
                "target_hardware": target,
                "change_summary": "真实提交说明\n第二行",
                "main_assembly": "cad/robot.SLDASM",
                "handoff_digest": payload["raw"]["source_selection"]["upload_sha256"],
                "platform_config_digest": "d" * 64,
            },
            ensure_ascii=False,
        ).encode("utf-8")
        (delivery / "input" / "request.json").write_bytes(original)
        return delivery, original

    def test_verifier_refuses_a_dual_claim_hardware_change_against_the_frozen_request(self):
        delivery, original = self._delivery_with_frozen_request(drop=("hardware_id",))

        def retarget(payload: dict) -> None:
            payload["identity"]["hardware_id"] = "different_target"

        def rebind(source_record: dict) -> None:
            source_record["selection"]["hardware_id"] = "different_target"
            source_record["identity"]["hardware_id"] = "different_target"
            source_record["source_revision_id"] = digest(source_record["identity"])

        self._tamper_delivered(delivery, "input/discovery/native-discovery.json", retarget)
        self._tamper_delivered(delivery, "input/source.json", rebind)
        self.assertEqual((delivery / "input" / "request.json").read_bytes(), original)
        report = verify_discovery(delivery)
        self.assertFalse(report["passed"])
        self.assertIn("discovery.identity", [item["code"] for item in report["errors"]])

    def test_verifier_refuses_a_dual_claim_summary_change_against_the_frozen_request(self):
        delivery, original = self._delivery_with_frozen_request(drop=("change_summary",))

        def rebind_summary(payload: dict) -> None:
            payload["identity"]["change_summary"] = "被生成器共同换掉的说明"

        def rebind_source_summary(source_record: dict) -> None:
            source_record["metadata"]["change_summary"] = "被生成器共同换掉的说明"

        self._tamper_delivered(delivery, "input/discovery/native-discovery.json", rebind_summary)
        self._tamper_delivered(delivery, "input/source.json", rebind_source_summary)
        self.assertEqual((delivery / "input" / "request.json").read_bytes(), original)
        report = verify_discovery(delivery)
        self.assertFalse(report["passed"])
        self.assertIn("discovery.identity", [item["code"] for item in report["errors"]])

    def test_verifier_refuses_a_fallback_without_the_frozen_request(self):
        delivery, _original = self._delivery_with_frozen_request(drop=("hardware_id",))
        (delivery / "input" / "request.json").unlink()
        support.seal_delivery(delivery)
        report = verify_discovery(delivery)
        self.assertFalse(report["passed"])
        self.assertIn("discovery.request", [item["code"] for item in report["errors"]])


    def test_verifier_refuses_a_malformed_frozen_request(self):
        delivery, original = self._delivery_with_frozen_request(drop=("hardware_id",))
        request_path = delivery / "input" / "request.json"
        request_path.write_bytes(original)
        support.seal_delivery(delivery)
        self.assertTrue(verify_discovery(delivery)["passed"])
        valid = json.loads(original.decode("utf-8"))

        def rewrite(changed: dict) -> None:
            request_path.write_text(json.dumps(changed, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            support.seal_delivery(delivery)

        mutations = [
            (f"omit_{field}", {key: value for key, value in valid.items() if key != field})
            for field in sorted(valid)
        ]
        mutations += [
            (f"null_{field}", {**valid, field: None})
            for field in ("request_id", "main_assembly", "handoff_digest", "platform_config_digest")
        ]
        mutations += [
            ("bad_request_id_type", {**valid, "request_id": 123}),
            ("bad_platform_digest", {**valid, "platform_config_digest": "not-a-sha256"}),
        ]
        for name, changed in mutations:
            with self.subTest(case=name):
                rewrite(changed)
                report = verify_discovery(delivery)
                self.assertFalse(report["passed"])
                self.assertIn("discovery.request", [item["code"] for item in report["errors"]])
                rewrite(valid)


class NativePropertyScopeTests(unittest.TestCase):
    def test_conflicting_dp_values_across_scopes_block(self):
        with self.assertRaises(CadError) as raised:
            _merge_property_scopes({"dp.hardware_id": "left"}, {"dp.hardware_id": "right"})
        self.assertEqual(getattr(raised.exception, "code", ""), "cad_property_conflict")

    def test_equal_dp_values_and_ordinary_overrides_merge(self):
        merged = _merge_property_scopes(
            {"dp.hardware_id": "fixture", "Weight": "1"},
            {"dp.hardware_id": "fixture", "Weight": "2"},
        )
        self.assertEqual(merged, {"dp.hardware_id": "fixture", "Weight": "2"})


class FakeMateFeature:
    """Minimal stand-in for a late-bound mate feature."""

    def __init__(self, type_name, count=None, traversable=True):
        self.type_name = type_name
        self.count = count
        self.traversable = traversable
        self.name = f"mate-{type_name}"

    def _FlagAsMethod(self, name):  # noqa: N802 - mimic the win32com dynamic object
        return None

    def __getattr__(self, name):
        if name == "_oleobj_":
            raise AttributeError(name)
        if name == "GetTypeName2":
            return lambda: self.type_name
        if name == "Name":
            return self.name
        if name == "GetSpecificFeature2":
            if self.count is None:

                def boom():
                    raise AttributeError("no interface")

                return boom
            inner = self

            class Specific:
                def _FlagAsMethod(self, name):  # noqa: N802
                    return None

                def __getattr__(self, attr):
                    if attr == "GetMateEntityCount":
                        return lambda: inner.count
                    raise AttributeError(attr)

            return lambda: Specific()
        if self.traversable:
            return lambda *args: None

        def unreadable(*args):
            raise AttributeError("no interface")

        return unreadable


class MateReaderTests(unittest.TestCase):
    def test_unreadable_recognised_mate_blocks(self):
        from description_pipeline.sources.solidworks.native import _mate_specific

        with self.assertRaises(CadError) as raised:
            _mate_specific(FakeMateFeature("MateConcentric"), strict=True)
        self.assertEqual(getattr(raised.exception, "code", ""), "cad_mate_unreadable")

    def test_readable_unknown_mate_is_captured_for_rejection(self):
        from description_pipeline.sources.solidworks.native import _mate_specific

        specific, count = _mate_specific(FakeMateFeature("MateGear", count=2), strict=True)
        self.assertIsNotNone(specific)
        self.assertEqual(count, 2)

    def test_invalid_entity_count_blocks(self):
        from description_pipeline.sources.solidworks.native import _mate_specific

        for value in (0, -1, 2.5, True):
            with self.assertRaises(CadError):
                _mate_specific(FakeMateFeature("MateConcentric", count=value), strict=True)

    def test_other_features_are_skipped(self):
        from description_pipeline.sources.solidworks.native import _mate_specific

        self.assertIsNone(_mate_specific(FakeMateFeature("RefPlane"), strict=False))


class SuppressionStateTests(unittest.TestCase):
    """The documented raw GetSuppression2 state is required, admitted and coherent."""

    def test_valid_record_states_pass_the_verifier_gate(self) -> None:
        from description_pipeline.verification.native_discovery import _require_suppression_states

        _require_suppression_states(record())

    def test_shape_checks_are_not_vacuous(self) -> None:
        from description_pipeline.verification.native_discovery import _Failure, _require_suppression_states

        for label, value in (
            ("no_components", {"components": None}),
            ("missing_components", {}),
            ("non_dict_entry", {"components": [None]}),
        ):
            with self.subTest(case=label), self.assertRaises(_Failure):
                _require_suppression_states(value)
        # An empty component list is allowed by the existing graph check.
        _require_suppression_states({"components": []})

    def test_verifier_refuses_invalid_or_incoherent_states(self) -> None:
        from description_pipeline.verification.native_discovery import _Failure, _require_suppression_states

        for label, state, suppressed in (
            ("state5", 5, None),
            ("missing", None, None),
            ("boolean", True, None),
            ("unknown", 7, None),
            ("incoherent-suppressed", 2, True),
            ("incoherent-lightweight", 2, None),
        ):
            with self.subTest(case=label):
                payload = record()
                component = payload["components"][0]
                if label == "missing":
                    component.pop("suppression_state")
                else:
                    component["suppression_state"] = state
                if suppressed is not None:
                    component["suppressed"] = suppressed
                if label == "incoherent-lightweight":
                    component["lightweight"] = True
                with self.assertRaises(_Failure):
                    _require_suppression_states(payload)

    def test_verifier_refuses_a_lightweight_occurrence_for_measurement(self) -> None:
        from description_pipeline.verification.native_discovery import _Failure, _require_suppression_states

        for state in (1, 4):
            with self.subTest(state=state):
                payload = record()
                payload["components"][0].update(suppression_state=state, suppressed=False, lightweight=True)
                with self.assertRaises(_Failure):
                    _require_suppression_states(payload)


class DerivationSplitTests(unittest.TestCase):
    """The pure derivation runs without any native module or CAD session."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="sw-derive-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _scene(self) -> tuple[Path, Path]:
        root = self.tmp / "native"
        for name in ("cad/robot.SLDASM", "cad/base.SLDPRT", "cad/arm.SLDPRT"):
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(f"placeholder {name}\n".encode())
        records = self.tmp / "records"
        path = records / "joints" / "arm.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"limits": {"lower": -1.5, "upper": 1.5}, "drive": {"effort": 6.0, "velocity": 2.0}}),
            encoding="utf-8",
        )
        (records / "budget.json").write_text(
            json.dumps({"robot": {"expected_mass_kg": [0.28, 0.32], "expected_extent_m": [0.35, 0.5]}}),
            encoding="utf-8",
        )
        return root, records

    def test_derivation_matches_preparation_without_native_readiness(self) -> None:
        source, records = self._scene()
        payload = record()
        settings = DiscoverySettings(record_roots=(records,))
        prepared = prepare_native_package(
            source, self.tmp / "prepared", run_id="run-1", backend=FakeBackend(payload), settings=settings
        )
        with mock.patch(
            "description_pipeline.runtime.native_readiness",
            side_effect=AssertionError("derivation must not consult the native runtime"),
        ):
            derived = derive_native_package(
                source,
                self.tmp / "derived",
                "run-1",
                support.local_native_record_payload(payload),
                settings=settings,
            )
        self.assertEqual(inventory(self.tmp / "prepared"), inventory(self.tmp / "derived"))
        self.assertEqual(prepared.prepared_sha256, derived.prepared_sha256)
        self.assertEqual(prepared.discovery_sha256, derived.discovery_sha256)
        self.assertEqual([f["code"] for f in prepared.findings], [f["code"] for f in derived.findings])
        self.assertEqual(prepared.passed, derived.passed)
        self.assertEqual(
            (self.tmp / "prepared" / "source.json").read_bytes(),
            (self.tmp / "derived" / "source.json").read_bytes(),
        )

    def test_derived_embedded_virtual_plan_is_consumed_by_the_capture_boundary(self) -> None:
        from description_pipeline.sources.solidworks.capture_package import (
            consume_sealed_definition,
        )

        source, records = self._scene()
        payload = record()
        component = next(item for item in payload["components"] if item["name2"] == "arm-1")
        owner_sha = payload["files"]["cad/robot.SLDASM"]
        component["document"] = "c:/users/operator/appdata/local/temp/swx1/vc~~/arm/arm.sldprt"
        component["suppression_state"] = 2
        component["source_provenance"] = {
            "kind": "native_embedded_virtual_component",
            "state": "embedded_virtual_component",
            "is_virtual": True,
            "occurrence": "arm-1",
            "owner": "cad/robot.SLDASM",
            "owner_sha256": owner_sha,
            "session_document_path": "C:\\Users\\operator\\AppData\\Local\\Temp\\swx1\\VC~~\\arm\\arm.SLDPRT",
            "session_temp_root": "C:\\Users\\operator\\AppData\\Local\\Temp\\swx1",
            "native_document_path": "C:\\Users\\operator\\AppData\\Local\\Temp\\swx1\\VC~~\\arm\\arm.SLDPRT",
            "materialization_path": "C:\\Users\\operator\\AppData\\Local\\Temp\\swx1\\VC~~\\arm\\arm.SLDPRT",
            "native_api_pid": 1,
            "used_api": "IComponent2.GetPathName/IComponent2.GetImportedPath/IComponent2.IsVirtual",
            "author_imported_path": None,
            "author_imported_error": None,
            "document": None,
        }
        payload["source_selection"]["closure"]["virtual_components"] = [
                {
                    "instance": "arm-1",
                    "name": "arm-1",
                    "kind": "native_embedded_virtual_component",
                    "state": "embedded_virtual_component",
                    "is_virtual": True,
                    "owner": "cad/robot.SLDASM",
                    "owner_sha256": owner_sha,
                    "configuration": "Default",
                    "binding": "owner_embedded_geometry",
                    "author_imported_path": None,
                    "author_imported_error": None,
                    "owner_anchor": None,
                    "suppression_state": 2,
                    "session_document_path": component["source_provenance"]["session_document_path"],
                    "session_temp_root": component["source_provenance"]["session_temp_root"],
                    "native_api_pid": 1,
                    "used_api": component["source_provenance"]["used_api"],
                }
            ]
        from description_pipeline.sources.solidworks.selection import seal_closure_selection

        selection = payload["source_selection"]
        payload["source_selection"] = seal_closure_selection(
            {**selection["excluded_files"], **selection["files"]},
            selection["main_assembly"],
            selection["closure"],
            reader_sha256=selection["reader_sha256"],
        )
        output = self.tmp / "derived-virtual"
        result = derive_native_package(
            source,
            output,
            "run-1",
            support.local_native_record_payload(payload),
            settings=DiscoverySettings(record_roots=(records,)),
        )
        self.assertTrue(result.passed, result.findings)
        plan = json.loads((output / "capture-plan.json").read_text(encoding="utf-8"))
        selector = next(item for item in plan["objects"] if item["name"] == "arm-1")
        self.assertNotIn("document", selector)
        self.assertIn("arm-1", next(item for item in plan["groups"] if item["name"] == "arm_link")["components"])
        _source, consumed = consume_sealed_definition(output)
        self.assertEqual(consumed, plan)

    def test_derivation_refuses_a_selected_main_outside_the_record(self) -> None:
        source, records = self._scene()
        settings = DiscoverySettings(record_roots=(records,), main_assembly="other.SLDASM")
        with self.assertRaisesRegex(PipelineError, "did not open the selected main assembly"):
            derive_native_package(
                source,
                self.tmp / "derived",
                "run-1",
                support.local_native_record_payload(record()),
                settings=settings,
            )

    def test_derivation_refuses_a_changed_frozen_file(self) -> None:
        source, records = self._scene()
        (source / "cad/robot.SLDASM").unlink()
        with self.assertRaisesRegex(PipelineError, "file binding does not match"):
            derive_native_package(
                source,
                self.tmp / "derived",
                "run-1",
                support.local_native_record_payload(record()),
                settings=DiscoverySettings(record_roots=(records,)),
            )

    def test_derivation_binds_the_payload_to_the_frozen_bytes_it_reads(self) -> None:
        # The steps-level guard (frozen inventory unchanged before/during discovery)
        # stays the refusal for a changed input; derivation itself still binds the
        # payload to the bytes present, never to a stale record value.
        source, records = self._scene()
        result = derive_native_package(
            source,
            self.tmp / "derived",
            "run-1",
            support.local_native_record_payload(record()),
            settings=DiscoverySettings(record_roots=(records,)),
        )
        payload = json.loads((self.tmp / "derived/discovery/native-discovery.json").read_text(encoding="utf-8"))
        self.assertEqual(payload["native_files"], inventory(source))
        self.assertEqual(result.handoff_sha256, digest(inventory(source)))

    def test_derivation_refuses_record_files_that_do_not_bind_the_frozen_bytes(self) -> None:
        source, records = self._scene()
        settings = DiscoverySettings(record_roots=(records,))
        mutations = (
            ({"files": {}}, "carries no bound file inventory"),
            (
                {"files": {**record()["files"], "cad/base.SLDPRT": "f" * 64}},
                "file binding does not match",
            ),
            (
                {"files": {key: value for key, value in record()["files"].items() if key != "cad/robot.SLDASM"}},
                "recorded main assembly is not bound",
            ),
            (
                {"files": {**record()["files"], "cad/unknown.SLDPRT": "e" * 64}},
                "file binding does not match",
            ),
        )
        for index, (patch, message) in enumerate(mutations):
            payload = record()
            payload.update(patch)
            output = self.tmp / f"derived-refused-{index}"
            with self.subTest(patch=patch), self.assertRaisesRegex(PipelineError, message):
                derive_native_package(
                    source,
                    output,
                    "run-1",
                    support.local_native_record_payload(payload),
                    settings=settings,
                )
            # every refused attempt must leave its output untouched
            self.assertFalse(output.exists() and any(output.iterdir()))

    def test_derivation_refuses_a_configuration_that_differs_from_the_record(self) -> None:
        source, records = self._scene()
        settings = DiscoverySettings(record_roots=(records,))
        with self.assertRaisesRegex(PipelineError, "requested configuration does not match"):
            derive_native_package(
                source,
                self.tmp / "derived-other-config",
                "run-1",
                support.local_native_record_payload(record()),
                settings=settings,
                configuration="Other",
            )
        matching = derive_native_package(
            source,
            self.tmp / "derived-matching-config",
            "run-1",
            support.local_native_record_payload(record()),
            settings=settings,
            configuration="Default",
        )
        self.assertTrue(matching.passed, matching.findings)

    def test_derivation_refuses_an_explicit_configuration_without_a_recorded_one(self) -> None:
        source, records = self._scene()
        payload = record()
        payload["identity"]["delivery_configuration"] = None
        payload.pop("saved_configuration", None)
        with self.assertRaisesRegex(
            PipelineError,
            "mate configuration",
        ):
            derive_native_package(
                source,
                self.tmp / "derived-unbound-config",
                "run-1",
                support.local_native_record_payload(payload),
                settings=DiscoverySettings(record_roots=(records,)),
                configuration="Default",
            )

    def test_derivation_refuses_a_declared_configuration_that_contradicts_the_saved_one(self) -> None:
        source, records = self._scene()
        payload = record()
        payload["identity"]["delivery_configuration"] = "Other"
        payload["saved_configuration"] = "Default"
        with self.assertRaisesRegex(PipelineError, "disagrees with the saved assembly configuration"):
            derive_native_package(
                source,
                self.tmp / "derived-conflicting-config",
                "run-1",
                support.local_native_record_payload(payload),
                settings=DiscoverySettings(record_roots=(records,)),
            )

    def test_derivation_accepts_a_saved_configuration_observed_without_the_dp_property(self) -> None:
        source, records = self._scene()
        payload = record()
        payload["identity"]["delivery_configuration"] = None
        output = self.tmp / "derived-native-saved-config"
        result = derive_native_package(
            source,
            output,
            "run-1",
            support.local_native_record_payload(payload),
            settings=DiscoverySettings(record_roots=(records,)),
        )
        self.assertTrue(result.passed, result.findings)
        source_record = json.loads((output / "source.json").read_text(encoding="utf-8"))
        self.assertEqual(source_record["selection"]["configuration"], "Default")

    def test_preparation_refuses_authored_files_before_any_native_read(self) -> None:
        source, records = self._scene()
        (source / "source.json").write_text("schema_version: x\n", encoding="utf-8")

        class ExplodingBackend:
            def discover_native(self, *args, **kwargs):  # pragma: no cover - must never run
                raise AssertionError("the native backend must not be invoked for a preflight refusal")

        with self.assertRaisesRegex(PipelineError, "authored package files"):
            prepare_native_package(
                source,
                self.tmp / "prepared",
                "run-1",
                backend=ExplodingBackend(),
                settings=DiscoverySettings(record_roots=(records,)),
            )

    def test_preparation_refuses_a_nonempty_output_before_any_native_read(self) -> None:
        source, records = self._scene()
        output = self.tmp / "prepared"
        output.mkdir()
        (output / "leftover.txt").write_text("x", encoding="utf-8")

        class ExplodingBackend:
            def discover_native(self, *args, **kwargs):  # pragma: no cover - must never run
                raise AssertionError("the native backend must not be invoked for a preflight refusal")

        with self.assertRaisesRegex(PipelineError, "prepared output directory must be empty"):
            prepare_native_package(
                source,
                output,
                "run-1",
                backend=ExplodingBackend(),
                settings=DiscoverySettings(record_roots=(records,)),
            )


if __name__ == "__main__":
    unittest.main()
