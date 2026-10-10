"""Native CAD-only discovery: generation, blocking findings and independent replay.

A fake native backend replays a recorded raw record; nothing here touches
SolidWorks.  The tests pin the generated package (robot.yaml + revision +
discovery record), the mandatory findings for missing native facts, and the
independent verifier that re-derives every claim from the bound record.
"""

from __future__ import annotations

import copy
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from . import _paths  # noqa: F401  (import side effect: sys.path)

from description_pipeline.io import PipelineError, digest, file_digest, inventory, parse_data  # noqa: E402
from description_pipeline.sources.solidworks.discovery import (  # noqa: E402
    CONTRACT,
    DISCOVERY_SCHEMA,
    DiscoverySettings,
    _clusters,
    _component_frames,
    _mate_rows,
    _unit,
    prepare_native_package,
)
from description_pipeline.sources.solidworks.errors import CadError  # noqa: E402
from description_pipeline.sources.solidworks.native import _merge_property_scopes  # noqa: E402
from description_pipeline.verification.native_discovery import verify_discovery  # noqa: E402
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
        "files": {},
    }


class FakeBackend:
    def __init__(self, payload: dict):
        self.payload = payload

    def discover_native(self, frozen_source: Path, settings: dict) -> dict:
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
        self.assertTrue((output / "robot.yaml").is_file())
        self.assertTrue((output / "cad-revision.json").is_file())
        document = yaml.safe_load((output / "robot.yaml").read_text(encoding="utf-8"))
        provenance = document["provenance"]
        self.assertEqual(provenance["generator"], "native-discovery")
        self.assertEqual(provenance["contract"], CONTRACT)
        self.assertEqual(provenance["native_inventory_sha256"], result.handoff_sha256)
        self.assertEqual(provenance["discovery_sha256"], result.discovery_sha256)
        # The native engineering files are copied through unmodified.
        for name in ("cad/robot.SLDASM", "cad/base.SLDPRT", "cad/arm.SLDPRT"):
            self.assertEqual((output / name).read_bytes(), (source / name).read_bytes())
        source_block = document["source"]
        self.assertEqual([body["name"] for body in source_block["bodies"]], ["arm_link", "base_link"])
        self.assertEqual(
            {body["name"]: body["frame"]["coordinate_system"] for body in source_block["bodies"]},
            {"arm_link": "CS_arm_link", "base_link": "CS_base_link"},
        )
        joint = source_block["joints"][0]
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
        revision = json.loads((output / "cad-revision.json").read_text(encoding="utf-8"))
        self.assertEqual(revision["hardware_id"], "nd_fixture")
        self.assertEqual(document["checks"]["expected_mass_kg"], [0.28, 0.32])
        self.assertEqual(document["checks"]["expected_extent_m"], [0.35, 0.5])
        self.assertEqual(
            revision["cad_files"],
            {
                name: value
                for name, value in inventory(output).items()
                if Path(name).suffix.lower() in {".sldasm", ".sldprt"}
            },
        )
        report = verify_discovery(output)
        self.assertTrue(report["passed"], report["errors"])
        self.assertEqual(report["joints"], 1)

    def test_frozen_published_name_is_preserved(self):
        settings = DiscoverySettings(record_roots=(self._native()[1],), frozen_names={"arm-1": "arm_link"})
        result, _source, output = self._prepare(settings=settings)
        self.assertTrue(result.passed, result.findings)
        document = yaml.safe_load((output / "robot.yaml").read_text(encoding="utf-8"))
        names = [body["name"] for body in document["source"]["bodies"]]
        self.assertIn("arm_link", names)
        payload = json.loads((output / "discovery/native-discovery.json").read_text(encoding="utf-8"))
        self.assertEqual(payload["frozen_names"].get("arm-1"), "arm_link")
        self.assertTrue(verify_discovery(output)["passed"])

    def test_duplicate_controlled_records_block_regardless_of_content_or_root_order(self):
        _source, records = self._native()
        other = self.tmp / "other-records"
        other.mkdir()
        for content in ((records / "budget.json").read_bytes(), b'{"robot":{"expected_mass_kg":[9,10]}}'):
            (other / "budget.json").write_bytes(content)
            for roots in ((records, other), (other, records)):
                with self.subTest(content=content, roots=roots):
                    result, _source, output = self._prepare(settings=DiscoverySettings(record_roots=roots))
                    self.assertIn("discovery.record_ambiguous", self._codes(result))
                    self.assertFalse((output / "robot.yaml").exists())
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
        self.assertTrue(verify_discovery(output)["passed"])

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
        self.assertTrue(verify_discovery(output)["passed"])

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
                self.assertEqual(
                    yaml.safe_load((output / "robot.yaml").read_text())["checks"]["expected_mass_kg"], bounds
                )
                self.assertTrue(verify_discovery(output)["passed"])

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
        self.assertEqual(
            yaml.safe_load((output / "robot.yaml").read_text())["checks"]["expected_mass_kg"], [0.28, 0.32]
        )
        self.assertEqual((records / "budget.json").read_bytes(), replacement)
        self.assertTrue(verify_discovery(output)["passed"])

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
        document = yaml.safe_load((output / "robot.yaml").read_text(encoding="utf-8"))
        base = next(body for body in document["source"]["bodies"] if body["name"] == "base_link")
        self.assertEqual(base["components"], ["a_mount-1", "base-1"])
        self.assertEqual(document["source"]["joints"][0]["parent"], "base_link")
        report = verify_discovery(output)
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
        document = yaml.safe_load((output / "robot.yaml").read_text(encoding="utf-8"))
        bodies = {body["name"]: body for body in document["source"]["bodies"]}
        self.assertEqual(bodies["base_link"]["components"], ["base-1"])
        self.assertEqual(bodies["base_link"]["frame"]["coordinate_system"], "CS_base_link")
        self.assertEqual(bodies["arm_link"]["components"], ["arm-1"])
        frames = {frame["name"]: frame for frame in document["source"]["frames"]}
        self.assertEqual(
            frames["tool_flange"],
            {
                "id": "tool_flange",
                "name": "tool_flange",
                "parent": "base_link",
                "coordinate_system": "SCS_tool_flange",
            },
        )
        joints = document["source"]["joints"]
        self.assertEqual(len(joints), 1)
        self.assertEqual((joints[0]["parent"], joints[0]["child"]), ("base_link", "arm_link"))
        # The grounding mates add no joint and never rewrite the raw evidence.
        raw = json.loads((output / "discovery" / "native-discovery.json").read_text(encoding="utf-8"))["raw"]
        self.assertEqual(
            [datum["name"] for datum in raw["datums"]], ["CS_base_link", "CS_arm_link", "SCS_tool_flange"]
        )
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
        derived = json.loads((output / "discovery" / "native-discovery.json").read_text(encoding="utf-8"))["derived"]
        self.assertEqual(
            {tuple(body["components"]): body["name"] for body in derived["bodies"]},
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
                    self.assertFalse((output / "robot.yaml").exists())
                    continue
                self.assertTrue(result.passed, result.findings)
                document = yaml.safe_load((output / "robot.yaml").read_text(encoding="utf-8"))
                base = next(body for body in document["source"]["bodies"] if body["name"] == "base_link")
                self.assertEqual(base["components"], ["base-1"])
                self.assertEqual(base["frame"]["coordinate_system"], "CS_base_link")
                report = verify_discovery(output)
                self.assertTrue(report["passed"], report["errors"])

    # ----------------------------------------------------------------- blocking

    def test_missing_identity_blocks_the_package(self):
        result, _source, output = self._prepare(mutate=lambda payload: payload["identity"].pop("hardware_id"))
        codes = self._codes(result)
        self.assertIn("discovery.identity_missing", codes)
        self.assertFalse((output / "robot.yaml").exists())
        self.assertTrue((output / "discovery/native-discovery.json").is_file())
        self.assertTrue((output / "discovery/findings.json").is_file())

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
        for body in payload["derived"]["bodies"]:
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

    def test_missing_design_budget_record_blocks(self):
        result, _source, _output = self._prepare(
            mutate=lambda payload: payload["properties"]["document"].pop("dp.design_budget_record")
        )
        self.assertIn("discovery.design_budget_missing", self._codes(result))

    def test_nonfinite_controlled_budget_retains_serializable_blocking_findings(self):
        def mutate(_payload):
            (self.tmp / "records/budget.json").write_text(
                '{"robot":{"expected_mass_kg":[0.28,1e999],"expected_extent_m":[0.35,0.5]}}', encoding="utf-8"
            )

        result, _source, output = self._prepare(mutate=mutate)
        self.assertIn("discovery.record_unreadable", self._codes(result))
        self.assertFalse((output / "robot.yaml").exists())
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
        document = yaml.safe_load((output / "robot.yaml").read_text(encoding="utf-8"))
        joint = document["source"]["joints"][0]
        self.assertEqual(joint["type"], "revolute")
        self.assertEqual(joint["axis"], [0.0, 1.0, 0.0])
        self.assertEqual(verify_discovery(output)["passed"], True)

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
        for body in payload["derived"]["bodies"]:
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
            for body in payload["derived"]["bodies"]:
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
        document = yaml.safe_load((output / "robot.yaml").read_text(encoding="utf-8"))
        frames = document["source"]["frames"]
        self.assertEqual(
            {frame["name"]: (frame["parent"], frame["coordinate_system"]) for frame in frames},
            {
                "arm_link_mount": ("arm_link", "CS_arm_link_mount"),
                "pinch": ("arm_link", "TCP_pinch"),
            },
        )
        names = [body["name"] for body in document["source"]["bodies"]]
        self.assertEqual(names, ["arm_link", "base_link"])
        self.assertTrue(verify_discovery(output)["passed"])

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
        destination = self.tmp / "no-frame"
        shutil.copytree(output, destination)
        document = yaml.safe_load((destination / "robot.yaml").read_text(encoding="utf-8"))
        document["source"].pop("frames")
        (destination / "robot.yaml").write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
        report = verify_discovery(destination)
        self.assertFalse(report["passed"])
        self.assertIn("discovery.frames", [item["code"] for item in report["errors"]])

    def test_jcs_must_alias_the_child_datum(self):
        def alias(payload):
            child = next(datum for datum in payload["datums"] if datum["name"] == "CS_arm_link")
            payload["datums"].append(
                {"name": "JCS_shoulder_pitch_joint", "owner": "arm-1", "array": list(child["array"])}
            )

        result, _source, output = self._prepare(mutate=alias)
        self.assertTrue(result.passed, result.findings)
        self.assertIn("discovery.jcs_alias", [finding["code"] for finding in result.findings])
        self.assertTrue(verify_discovery(output)["passed"])

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
        result, _source, output = self._prepare(
            mutate=lambda payload: payload["components"].append(copy.deepcopy(payload["components"][0]))
        )
        self.assertIn("discovery.component_identity_invalid", self._codes(result))
        self.assertFalse((output / "robot.yaml").exists())

    def test_duplicate_owned_datum_blocks_even_with_explicit_body_datum(self):
        def mutate(payload):
            payload["datums"].append(copy.deepcopy(payload["datums"][1]))
            payload["properties"]["components"] = {"arm-1": {"dp.body_datum": "CS_arm_link"}}

        result, _source, output = self._prepare(mutate=mutate)
        self.assertIn("discovery.datum_identity_duplicate", self._codes(result))
        self.assertFalse((output / "robot.yaml").exists())

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

        result, _source, output = self._prepare(mutate=mutate)
        self.assertIn("discovery.record_unreadable", self._codes(result))
        self.assertFalse((output / "robot.yaml").exists())
        json.dumps(list(result.findings), allow_nan=False)

    def test_frozen_names_do_not_mask_a_native_datum_rename_after_resealing(self):
        settings = DiscoverySettings(record_roots=(self._native()[1],), frozen_names={"arm-1": "arm_link"})
        result, _source, output = self._prepare(settings=settings)
        self.assertTrue(result.passed, result.findings)
        path = output / "discovery" / "native-discovery.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        next(datum for datum in payload["raw"]["datums"] if datum["name"] == "CS_arm_link")["name"] = "CS_renamed_link"
        path.write_text(json.dumps(payload), encoding="utf-8")
        robot_path = output / "robot.yaml"
        document = yaml.safe_load(robot_path.read_text(encoding="utf-8"))
        next(body for body in document["source"]["bodies"] if body["name"] == "arm_link")["frame"][
            "coordinate_system"
        ] = "CS_renamed_link"
        document["provenance"]["discovery_sha256"] = file_digest(path)
        robot_path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
        report = verify_discovery(output)
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
        document = yaml.safe_load((output / "robot.yaml").read_text(encoding="utf-8"))
        joint = document["source"]["joints"][0]
        self.assertEqual(joint["type"], "revolute")
        self.assertEqual(joint["axis"], [0.0, 0.0, 1.0])
        self.assertTrue(verify_discovery(output)["passed"])

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
            self.assertTrue(
                any(item["code"] == "discovery.mate_entities_unsupported" for item in findings), findings
            )

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
        document = yaml.safe_load((output / "robot.yaml").read_text(encoding="utf-8"))
        joint = document["source"]["joints"][0]
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
        without_record = self.tmp / "without-record"
        shutil.copytree(output, without_record)
        (without_record / "discovery" / "native-discovery.json").unlink()
        report = verify_discovery(without_record)
        self.assertFalse(report["passed"])
        self.assertIn("discovery.file", [item["code"] for item in report["errors"]])
        without_provenance = self.tmp / "without-provenance"
        shutil.copytree(output, without_provenance)
        document = yaml.safe_load((without_provenance / "robot.yaml").read_text(encoding="utf-8"))
        document.pop("provenance")
        (without_provenance / "robot.yaml").write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
        report = verify_discovery(without_provenance)
        self.assertFalse(report["passed"])
        self.assertIn("discovery.binding", [item["code"] for item in report["errors"]])

    def test_gate_is_mandatory_and_has_no_legacy_mode(self):
        result, _source, output = self._prepare()
        self.assertTrue(result.passed)
        self.assertEqual(_native_discovery(output)["bodies"], 2)
        authored = self.tmp / "authored"
        authored.mkdir()
        (authored / "robot.yaml").write_text("schema_version: x\n", encoding="utf-8")
        with self.assertRaises(PipelineError):
            _native_discovery(authored)

    def test_gate_rejects_a_tampered_record(self):
        result, _source, output = self._prepare()
        self.assertTrue(result.passed)
        path = output / "discovery" / "native-discovery.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["raw"]["mates"][0]["entities"][0]["cylinder"]["radius"] = -1.0
        path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(PipelineError):
            _native_discovery(output)


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


if __name__ == "__main__":
    unittest.main()
