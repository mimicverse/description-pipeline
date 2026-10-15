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
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from . import _paths  # noqa: F401  (import side effect: sys.path)

from description_pipeline.io import PipelineError, digest, file_digest, inventory, parse_data  # noqa: E402
from description_pipeline.sources.solidworks.discovery import (  # noqa: E402
    CONTRACT,
    DISCOVERY_SCHEMA,
    DiscoverySettings,
    _budgets,
    _clusters,
    _component_frames,
    _identity,
    _mate_rows,
    _unit,
    SubmissionIdentity,
    derive_native_package,
    prepare_native_package,
)
from description_pipeline.sources.solidworks.errors import CadError  # noqa: E402
from description_pipeline.sources.solidworks.native import _merge_property_scopes  # noqa: E402
from description_pipeline.schemas import SOURCE_RECORD_SCHEMA  # noqa: E402
from description_pipeline.verification.native_discovery import (  # noqa: E402
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
    return {
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
                "suppressed": False,
                "error_code": 0,
                "scope": "",
                "limits": None,
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
        "files": files,
        "source_selection": source_selection,
    }


class FakeBackend:
    def __init__(self, payload: dict):
        self.payload = payload

    def discover_native(self, frozen_source: Path, settings: dict, *, on_activity=None) -> dict:
        return copy.deepcopy(self.payload)


class DiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="sw-discovery-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.serial = 0

    # ------------------------------------------------------------------ helpers

    def _native(self) -> tuple[Path, Path]:
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
            json.dumps({"limits": {"lower": -1.5, "upper": 1.5}, "drive": {"effort": 6.0, "velocity": 2.0}}),
            encoding="utf-8",
        )
        (records / "budget.json").write_text(
            json.dumps({"robot": {"expected_mass_kg": [0.28, 0.32], "expected_extent_m": [0.35, 0.5]}}),
            encoding="utf-8",
        )
        return root, records

    def _prepare(self, *, mutate=None, settings=None):
        source, records = self._native()
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

    def test_native_preparation_forwards_the_guarded_activity_sink(self):
        class ActivityBackend:
            def __init__(self):
                self.sink = None

            def discover_native(self, frozen_source, settings, *, on_activity=None):
                self.sink = on_activity
                if on_activity is not None:
                    on_activity({"phase": "discover", "action": "read_components", "completed": 1, "total": 2})
                return copy.deepcopy(record())

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
            self.assertEqual(error.detail["field"], "raw.components[0].transform[3]")
            json.dumps(error.to_dict(), allow_nan=False)

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
        joint = rebuild_joint_facts(payload, output)[0]
        self.assertEqual(joint["name"], "shoulder_pitch_joint")
        self.assertEqual(joint["type"], "revolute")
        self.assertEqual((joint["parent"], joint["child"]), ("base_link", "arm_link"))
        self.assertEqual(joint["axis"], [0.0, 0.0, 1.0])
        self.assertEqual(joint["limits"], {"lower": -1.5, "upper": 1.5, "effort": 6.0, "velocity": 2.0})
        self.assertEqual(joint["axis_reference"]["component"], "base-1")
        self.assertEqual(joint["axis_reference"]["feature_name"], "Cyl1")
        evidence = joint["limit_evidence"]
        self.assertTrue(evidence["file"].startswith("records/"))
        self.assertIn("limits", (output / evidence["file"]).read_text(encoding="utf-8"))
        self.assertEqual(evidence["anchor"], '"limits"')
        budget = json.loads((output / payload["budget"]["package_file"]).read_text(encoding="utf-8"))
        self.assertEqual(budget["robot"]["expected_mass_kg"], [0.28, 0.32])
        self.assertEqual(budget["robot"]["expected_extent_m"], [0.35, 0.5])
        report = self._verify(output)
        self.assertTrue(report["passed"], report["errors"])
        self.assertEqual(report["joints"], 1)

    def test_frozen_published_name_is_preserved(self):
        settings = DiscoverySettings(record_roots=(self._native()[1],), frozen_names={"arm-1": "arm_link"})
        result, _source, output = self._prepare(settings=settings)
        self.assertTrue(result.passed, result.findings)
        payload = json.loads((output / "discovery/native-discovery.json").read_text(encoding="utf-8"))
        names = [body["name"] for body in rebuild_rigid_bodies(payload)]
        self.assertIn("arm_link", names)
        self.assertEqual(payload["frozen_names"].get("arm-1"), "arm_link")
        self.assertTrue(self._verify(output)["passed"])

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

    def test_one_record_cannot_change_between_keys_in_the_same_job(self):
        def change_after_first_reference(content, path):
            result = parse_data(content, path)
            if path.name == "arm.json" and result["drive"]["effort"] == 6.0:
                result["drive"]["effort"] = 7.0
                path.write_text(json.dumps(result))
                return parse_data(content, path)
            return result

        with (
            mock.patch(
                "description_pipeline.sources.solidworks.discovery.parse_data", side_effect=change_after_first_reference
            ),
            self.assertRaisesRegex(PipelineError, "changed between references"),
        ):
            self._prepare()

    def test_rigid_accessory_preserves_the_joint_between_solved_bodies(self):
        def mutate(payload):
            accessory = copy.deepcopy(payload["components"][0])
            accessory.update({"name2": "a_mount-1", "instance_id": "a_mount-1"})
            payload["components"].append(accessory)
            payload["mates"].append(
                {
                    "name": "base_mount_lock",
                    "type": "lock",
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
        self.assertEqual(rebuild_joint_facts(payload, output)[0]["parent"], "base_link")
        report = self._verify(output)
        self.assertTrue(report["passed"], report["errors"])

    def test_assembly_frame_mate_is_blocked_explicitly_and_never_silently_dropped(self):
        def ground_mate() -> dict:
            return {
                "name": "base_ground_pin",
                "type": "coincident",
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
            frames["tool_flange"],
            {
                "id": "tool_flange",
                "name": "tool_flange",
                "parent": "base_link",
                "coordinate_system": "SCS_tool_flange",
            },
        )
        joints = rebuild_joint_facts(payload, output)
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
        self.assertEqual(frame_entity["component"], "")

    def test_frame_attachment_must_reach_exactly_one_rigid_cluster(self):
        def mutate(payload):
            mates = _grounding_mates()
            mates.append(
                {
                    "name": "arm_ground_axis",
                    "type": "concentric",
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
        self.assertEqual(self._codes(result), ["discovery.frame_attachment"])
        finding = result.findings[0]
        self.assertIn("exactly one rigid cluster", finding["message"])
        self.assertEqual(finding["detail"]["components"], ["arm-1", "base-1"])
        self.assertEqual(len(finding["detail"]["clusters"]), 2)

    def test_frame_entity_identity_is_the_exact_empty_string(self):
        for component in (None, 0, "base-1"):

            def mutate(payload, component=component):
                mates = _grounding_mates()
                mates[2]["entities"][1] = {
                    "component": component,
                    "assembly_frame": True,
                    "feature": "Axis2",
                    "cylinder": {"point": [0.0, 0.0, 0.0], "direction": [1.0, 0.0, 0.0], "radius": 0.006},
                }
                payload["mates"].extend(mates)

            with self.subTest(component=component):
                result, _source, _output = self._prepare(mutate=mutate)
                self.assertEqual([item["code"] for item in result.findings], ["discovery.frame_attachment"])
                self.assertIn("frozen top-scope identity", result.findings[0]["message"])
                self.assertFalse(any("full rank" in item["message"] for item in result.findings))

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
            "unknown_occurrence": (unknown_occurrence, "names an unknown occurrence"),
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

    def test_nested_frame_attachment_remains_blocking(self):
        for scope in ("sub-1", None):

            def mutate(payload, scope=scope):
                mates = _grounding_mates()
                for mate in mates:
                    if scope is None:
                        mate.pop("scope")
                    else:
                        mate["scope"] = scope
                payload["mates"].extend(mates)

            with self.subTest(scope=scope):
                result, _source, _output = self._prepare(mutate=mutate)
                self.assertFalse(result.passed)
                findings = [item for item in result.findings if item["code"] == "discovery.frame_attachment"]
                self.assertEqual(len(findings), 3)
                self.assertTrue(all("outside the frozen top assembly" in item["message"] for item in findings))
                self.assertTrue(all(item["detail"]["scope"] == scope for item in findings))

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
                container = copy.deepcopy(payload["components"][0])
                container.update({"name2": "sub-1", "instance_id": "sub-1", "document": "cad/sub.SLDASM"})
                payload["components"].append(container)
                payload["datums"][0]["owner"] = "sub-1"
                if connected:
                    payload["mates"].append(
                        {
                            "name": "base_container_lock",
                            "type": "lock",
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
        self.assertEqual(item["detail"], {"technical_name": "wire_test", "authored": False})
        self.assertIsNone(identity["robot_name"])
        self.assertEqual(identity["robot_name_technical"], "wire_test")
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
        self.assertEqual(identity["robot_name_technical"], "wrist_drive_2")

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

    def test_missing_limits_and_drive_block(self):
        result, _source, _output = self._prepare(
            mutate=lambda payload: payload["properties"]["mates"]["shoulder_pitch_joint__coaxial"].pop(
                "dp.joint.limits_record"
            )
        )
        self.assertIn("discovery.joint_limits_missing", self._codes(result))
        result, _source, _output = self._prepare(
            mutate=lambda payload: payload["properties"]["mates"]["shoulder_pitch_joint__coaxial"].pop(
                "dp.joint.drive_record"
            )
        )
        self.assertIn("discovery.joint_drive_missing", self._codes(result))

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

    def test_unspecified_budget_does_not_replace_missing_inferred_joint_drive(self):
        def mutate(payload):
            payload["properties"]["document"].pop("dp.design_budget_record")
            payload["properties"]["mates"]["shoulder_pitch_joint__coaxial"].pop("dp.joint.drive_record")

        result, _source, output = self._prepare(mutate=mutate)
        blockers = {item["code"] for item in result.findings if item["blocking"]}
        self.assertEqual(blockers, {"discovery.joint_drive_missing", "discovery.tree_disconnected"})
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
        joint = rebuild_joint_facts(payload, output)[0]
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
                "pinch": ("arm_link", "TCP_pinch"),
            },
        )
        names = [body["name"] for body in rebuild_rigid_bodies(payload)]
        self.assertEqual(names, ["arm_link", "base_link"])
        self.assertTrue(self._verify(output)["passed"])

    def test_unowned_interface_datum_blocks(self):
        result, _source, _output = self._prepare(
            mutate=lambda payload: payload["datums"].append(
                {"name": "TCP_loose", "owner": "ghost-1", "array": _translated(0.0, 0.0, 0.4)}
            )
        )
        self.assertIn("discovery.interface_unowned", self._codes(result))

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
        payload = json.loads((destination / "evidence/discovery.json").read_text(encoding="utf-8"))
        self.assertIn("pinch", [frame["name"] for frame in rebuild_interface_frames(payload)])

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

    def test_frozen_name_that_differs_from_native_blocks(self):
        settings = DiscoverySettings(record_roots=(self._native()[1],), frozen_names={"arm-1": "right_arm_link"})
        result, _source, _output = self._prepare(settings=settings)
        self.assertIn("discovery.name_frozen_mismatch", self._codes(result))

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

    def test_duplicate_occurrence_cannot_overwrite_a_native_identity(self):
        result, _source, _output = self._prepare(
            mutate=lambda payload: payload["components"].append(copy.deepcopy(payload["components"][0]))
        )
        self.assertIn("discovery.component_identity_invalid", self._codes(result))

    def test_duplicate_owned_datum_blocks_even_with_explicit_body_datum(self):
        def mutate(payload):
            payload["datums"].append(copy.deepcopy(payload["datums"][1]))
            payload["properties"]["components"] = {"arm-1": {"dp.body_datum": "CS_arm_link"}}

        result, _source, _output = self._prepare(mutate=mutate)
        self.assertIn("discovery.datum_identity_duplicate", self._codes(result))

    def test_interface_suffix_must_already_be_snake_case(self):
        def mutate(payload):
            payload["datums"].append({"name": "TCP_Tool", "owner": "arm-1", "array": _translated(0.0, 0.0, 0.3)})

        result, _source, _output = self._prepare(mutate=mutate)
        self.assertIn("discovery.interface_name_invalid", self._codes(result))

    def test_interface_frame_colliding_with_a_body_name_blocks(self):
        def mutate(payload):
            payload["datums"].append({"name": "TCP_base_link", "owner": "base-1", "array": _translated(0.0, 0.0, 0.3)})

        result, _source, _output = self._prepare(mutate=mutate)
        self.assertIn("discovery.interface_name_duplicate", self._codes(result))

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

    def test_native_limits_conflicting_with_the_record_block(self):
        def mutate(payload):
            payload["mates"][0]["limits"] = {"lower": -2.0, "upper": 2.0, "unit": "rad"}

        result, _source, _output = self._prepare(mutate=mutate)
        self.assertIn("discovery.joint_limits_conflict", self._codes(result))

    def test_axis_sign_rejects_boolean_and_float_values(self):
        for value in (True, False, 1.0, -1.0):
            with self.subTest(value=value):
                result, _source, _output = self._prepare(
                    mutate=lambda payload, value=value: payload["properties"]["mates"][
                        "shoulder_pitch_joint__coaxial"
                    ].update({"dp.joint.axis_sign": value})
                )
                self.assertIn("discovery.joint_axis_sign_missing", self._codes(result))

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

    def test_frozen_names_do_not_mask_a_native_datum_rename_after_resealing(self):
        settings = DiscoverySettings(record_roots=(self._native()[1],), frozen_names={"arm-1": "arm_link"})
        result, _source, output = self._prepare(settings=settings)
        self.assertTrue(result.passed, result.findings)
        path = output / "discovery" / "native-discovery.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        next(datum for datum in payload["raw"]["datums"] if datum["name"] == "CS_arm_link")["name"] = "CS_renamed_link"
        path.write_text(json.dumps(payload), encoding="utf-8")
        report = self._verify(output)
        self.assertTrue(next(check for check in report["checks"] if check["id"] == "discovery.binding")["passed"])
        self.assertIn("discovery.names", [item["code"] for item in report["errors"]])

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
        joint = rebuild_joint_facts(payload, output)[0]
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
        joint = rebuild_joint_facts(payload, output)[0]
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
        (without_record / "evidence" / "discovery.json").unlink()
        report = verify_discovery(without_record)
        self.assertFalse(report["passed"])
        self.assertIn("discovery.file", [item["code"] for item in report["errors"]])
        frozen = self._deliver(output)
        frozen_record = json.loads((frozen / "evidence" / "discovery.json").read_text(encoding="utf-8"))
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
        path = delivery / "evidence" / "discovery.json"
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

        self._tamper_delivered(delivery, "evidence/discovery.json", drop_authored_hardware)
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

        self._tamper_delivered(delivery, "evidence/discovery.json", rebind_hardware)
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

        self._tamper_delivered(delivery, "evidence/discovery.json", rebind_summary)
        report = verify_discovery(delivery)
        self.assertFalse(report["passed"])
        self.assertIn("discovery.identity", [item["code"] for item in report["errors"]])


    def test_verifier_refuses_an_unobserved_delivery_configuration(self):
        result, _source, output = self._prepare()
        self.assertTrue(result.passed, result.findings)

        def set_flag(payload: dict, value) -> None:
            for side in ("before", "after"):
                payload["raw"]["document_state"][side]["save_flag"] = value

        def set_observed_configuration(payload: dict, value: str) -> None:
            for side in ("before", "after"):
                payload["raw"]["document_state"][side]["configuration"] = value

        for label, mutate in (
            ("no_native_observation", lambda payload: payload["raw"].pop("document_state")),
            ("unknown_save_state", lambda payload: set_flag(payload, None)),
            ("unsaved_document", lambda payload: set_flag(payload, True)),
            ("contradicting_active_configuration", lambda payload: set_observed_configuration(payload, "Other")),
        ):
            with self.subTest(case=label):
                delivery = self._deliver(output)
                self._tamper_delivered(delivery, "evidence/discovery.json", mutate)
                report = verify_discovery(delivery)
                self.assertFalse(report["passed"])
                self.assertIn("discovery.configuration", [item["code"] for item in report["errors"]])


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

        for label, claim, observation, expected in (
            ("omitted_claim_with_observations", "omitted", "kept", True),
            ("omitted_claim_no_observation", "omitted", "absent", False),
            ("null_claim_no_observation", "null", "absent", False),
            ("omitted_claim_contradictory_observation", "omitted", "contradicting", False),
        ):
            with self.subTest(case=label):
                delivery = self._deliver(output)
                self._tamper_delivered(delivery, "evidence/discovery.json", mutation(claim, observation))
                report = verify_discovery(delivery)
                self.assertEqual(report["passed"], expected)
                if not expected:
                    self.assertIn("discovery.configuration", [item["code"] for item in report["errors"]])


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
            derived = derive_native_package(source, self.tmp / "derived", "run-1", payload, settings=settings)
        self.assertEqual(inventory(self.tmp / "prepared"), inventory(self.tmp / "derived"))
        self.assertEqual(prepared.prepared_sha256, derived.prepared_sha256)
        self.assertEqual(prepared.discovery_sha256, derived.discovery_sha256)
        self.assertEqual([f["code"] for f in prepared.findings], [f["code"] for f in derived.findings])
        self.assertEqual(prepared.passed, derived.passed)
        self.assertEqual(
            (self.tmp / "prepared" / "source.json").read_bytes(),
            (self.tmp / "derived" / "source.json").read_bytes(),
        )

    def test_derivation_refuses_a_selected_main_outside_the_record(self) -> None:
        source, records = self._scene()
        settings = DiscoverySettings(record_roots=(records,), main_assembly="other.SLDASM")
        with self.assertRaisesRegex(PipelineError, "did not open the selected main assembly"):
            derive_native_package(source, self.tmp / "derived", "run-1", record(), settings=settings)

    def test_derivation_refuses_a_changed_frozen_file(self) -> None:
        source, records = self._scene()
        (source / "cad/robot.SLDASM").unlink()
        with self.assertRaisesRegex(PipelineError, "file binding does not match"):
            derive_native_package(
                source,
                self.tmp / "derived",
                "run-1",
                record(),
                settings=DiscoverySettings(record_roots=(records,)),
            )

    def test_derivation_binds_the_payload_to_the_frozen_bytes_it_reads(self) -> None:
        # The steps-level guard (frozen inventory unchanged before/during discovery)
        # stays the refusal for a changed input; derivation itself still binds the
        # payload to the bytes present, never to a stale record value.
        source, records = self._scene()
        result = derive_native_package(
            source, self.tmp / "derived", "run-1", record(), settings=DiscoverySettings(record_roots=(records,))
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
                derive_native_package(source, output, "run-1", payload, settings=settings)
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
                record(),
                settings=settings,
                configuration="Other",
            )
        matching = derive_native_package(
            source,
            self.tmp / "derived-matching-config",
            "run-1",
            record(),
            settings=settings,
            configuration="Default",
        )
        self.assertTrue(matching.passed, matching.findings)

    def test_derivation_refuses_an_explicit_configuration_without_a_recorded_one(self) -> None:
        source, records = self._scene()
        payload = record()
        payload["identity"]["delivery_configuration"] = None
        payload.pop("saved_configuration", None)
        with self.assertRaisesRegex(PipelineError, "carries no usable configuration"):
            derive_native_package(
                source,
                self.tmp / "derived-unbound-config",
                "run-1",
                payload,
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
                payload,
                settings=DiscoverySettings(record_roots=(records,)),
            )

    def test_derivation_requires_the_saved_configuration_observation(self) -> None:
        source, records = self._scene()
        settings = DiscoverySettings(record_roots=(records,))

        def set_flag(payload, value):
            for side in ("before", "after"):
                payload["document_state"][side]["save_flag"] = value

        def set_observed_configuration(payload, value):
            for side in ("before", "after"):
                payload["document_state"][side]["configuration"] = value

        for label, mutate in (
            ("no_native_observation", lambda payload: payload.pop("document_state")),
            ("unknown_save_state", lambda payload: set_flag(payload, None)),
            ("unsaved_document", lambda payload: set_flag(payload, True)),
            ("contradicting_active_configuration", lambda payload: set_observed_configuration(payload, "Other")),
        ):
            with self.subTest(case=label):
                payload = record()
                mutate(payload)
                result = derive_native_package(
                    source,
                    self.tmp / f"derived-config-{label}",
                    "run-1",
                    payload,
                    settings=settings,
                )
                self.assertFalse(result.passed)
                self.assertIn("discovery.configuration_unproven", {item["code"] for item in result.findings})

    def test_derivation_accepts_a_saved_configuration_observed_without_the_dp_property(self) -> None:
        source, records = self._scene()
        payload = record()
        payload["identity"]["delivery_configuration"] = None
        output = self.tmp / "derived-native-saved-config"
        result = derive_native_package(
            source, output, "run-1", payload, settings=DiscoverySettings(record_roots=(records,))
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
