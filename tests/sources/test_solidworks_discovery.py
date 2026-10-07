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

import yaml

from . import _paths  # noqa: F401  (import side effect: sys.path)

from description_pipeline.io import PipelineError, digest, inventory  # noqa: E402
from description_pipeline.sources.solidworks.discovery import (  # noqa: E402
    CONTRACT,
    DISCOVERY_SCHEMA,
    DiscoverySettings,
    prepare_native_package,
)
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
                "name": "shoulder_pitch__axis",
                "type": "concentric",
                "suppressed": False,
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
                "name": "shoulder_pitch__seat",
                "type": "coincident",
                "suppressed": False,
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
            {"name": "CS_arm", "owner": "arm-1", "array": _translated(0.0, 0.0, 0.1)},
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
                "shoulder_pitch__axis": {
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
        self.assertEqual([body["name"] for body in source_block["bodies"]], ["arm", "base_link"])
        self.assertEqual(
            {body["name"]: body["frame"]["coordinate_system"] for body in source_block["bodies"]},
            {"arm": "CS_arm", "base_link": "CS_base_link"},
        )
        joint = source_block["joints"][0]
        self.assertEqual(joint["name"], "shoulder_pitch")
        self.assertEqual(joint["type"], "revolute")
        self.assertEqual((joint["parent"], joint["child"]), ("base_link", "arm"))
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
        settings = DiscoverySettings(record_roots=(self._native()[1],), frozen_names={"arm-1": "right_arm_link"})
        result, _source, output = self._prepare(settings=settings)
        self.assertTrue(result.passed, result.findings)
        document = yaml.safe_load((output / "robot.yaml").read_text(encoding="utf-8"))
        names = [body["name"] for body in document["source"]["bodies"]]
        self.assertIn("right_arm_link", names)
        payload = json.loads((output / "discovery/native-discovery.json").read_text(encoding="utf-8"))
        self.assertEqual(payload["frozen_names"].get("arm-1"), "right_arm_link")
        self.assertTrue(verify_discovery(output)["passed"])

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
                {"name": "Tangent1", "type": "tangent", "suppressed": False, "limits": None, "entities": []}
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
            mutate=lambda payload: payload["properties"]["mates"]["shoulder_pitch__axis"].pop("dp.joint.limits_record")
        )
        self.assertIn("discovery.joint_limits_missing", self._codes(result))
        result, _source, _output = self._prepare(
            mutate=lambda payload: payload["properties"]["mates"]["shoulder_pitch__axis"].pop("dp.joint.drive_record")
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
            datum = next(item for item in payload["datums"] if item["name"] == "CS_arm")
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
            datum = next(item for item in payload["datums"] if item["name"] == "CS_arm")
            datum["array"] = _translated(0.4, 0.0, 0.1)

        result, _source, _output = self._prepare(mutate=mutate)
        self.assertIn("discovery.joint_frame_off_axis", self._codes(result))

    def test_mate_group_naming_is_required(self):
        def mutate(payload):
            payload["mates"][0]["name"] = "Concentric1"
            payload["properties"]["mates"]["Concentric1"] = payload["properties"]["mates"].pop("shoulder_pitch__axis")

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
            payload["properties"]["document"]["dp.joint.shoulder_pitch.drive_record"] = "other.json#drive"

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


if __name__ == "__main__":
    unittest.main()
