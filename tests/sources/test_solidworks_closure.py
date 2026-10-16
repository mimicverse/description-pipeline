"""Linux mass-closure projection: raw observations in, derived diagnostics out.

The Windows boundary keeps only what CAD answered (whole-assembly reading and
the per-instance component context); document basis, deltas, totals, coverage
and the shallowest-overridden-ancestor recombination are rebuilt on Linux from
the sealed snapshot without modifying its bytes.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from . import _paths  # noqa: F401  (import side effect: sys.path)

from description_pipeline.sources.solidworks.discovery import CONTRACT, DISCOVERY_SCHEMA  # noqa: E402
from description_pipeline.sources.solidworks.closure import derive_mass_closure  # noqa: E402
from description_pipeline.sources.solidworks.freeze import _component_context_record  # noqa: E402

from . import support  # noqa: E402

DEV = "board-1/Dev_Board-1"
EASYEDA = "board-1/EasyEDA PCB Model~Nz70-1"


def _placement(xyz=(0.0, 0.0, 0.0)):
    return [1.0, 0.0, 0.0, xyz[0], 0.0, 1.0, 0.0, xyz[1], 0.0, 0.0, 1.0, xyz[2], 0.0, 0.0, 0.0, 1.0]


def _flags(**active):
    flags = {"OverrideMass": False, "OverrideCenterOfMass": False, "OverrideMomentsOfInertia": False}
    flags.update(active)
    return flags


def _part_payload(mass, com=(0.0, 0.0, 0.0)):
    return {
        "mass": mass,
        "com": list(com),
        "inertia": [[1e-6, 0.0, 0.0], [0.0, 1e-6, 0.0], [0.0, 0.0, 1e-6]],
        "reference": {"used_api": "fixture", "product_convention": "solidworks_standard"},
    }


def _effective_payload(mass, com, *, role, document_type, name):
    return {
        "mode": "effective_instance" if document_type == "part" else "assembly_component_group",
        "state": "sheet_only" if name == EASYEDA else None,
        "measurement_basis": "cad_authored_effective_instance",
        "mass": mass,
        "com": list(com),
        "inertia": [[2e-6, 1e-8, 0.0], [1e-8, 2.5e-6, 0.0], [0.0, 0.0, 4e-6]],
        "role": role,
        "document_type": document_type,
        "reference": {
            "used_api": "IMassProperty2.GetMomentOfInertia(0)",
            "selection_api": "IMassProperty2 (SelectedItems = component instance)",
            "override_api": "IMassProperty2.GetOverrideOptions (same selection)",
            "scope": "assembly_component_group",
            "axes": "assembly_document_axes",
            "reference_point": "center_of_mass",
            "product_convention": "solidworks_standard",
            "effective_instance": True,
            "measurement_basis": "cad_authored_effective_instance",
            "selected_components": [name],
            "configuration": "Default",
            "part_document": "C:/collected/" + name.split("/")[-1] + ".SLDPRT",
            "assembly_document": "C:/collected/robot.SLDASM",
            "assembly_configuration": "Default",
            "overrides": _flags(OverrideMass=True),
        },
    }


def _instance(name, *, depth, parent, document_type, mass, overrides, effective=None, error=None):
    entry = {
        "name": name,
        "parent": parent,
        "depth": depth,
        "document": "C:/collected/robot.SLDASM",
        "document_type": document_type,
        "context_mass_kg": mass,
        "context_volume_m3": None,
        "overrides": overrides,
        "effective": effective,
        "effective_error": error,
    }
    return entry


def _carrier_transform(name: str) -> list[float]:
    """The recorded occurrence transform; EasyEDA's frame sits 0.01 m above the origin."""

    return _placement((0.0, 0.01, 0.0)) if name == EASYEDA else _placement()


def _hinge_point(name: str) -> list[float]:
    """Component-frame geometry that solves the hinge through the child datum origin.

    The assembly-space line runs through (0, 0.01, 0): the child link's datum
    origin, which the joint frame check requires the axis to pass through.
    """

    return [0.0, 0.0, 0.0] if name == EASYEDA else [0.0, 0.01, 0.0]


def _carrier_record(bodies: list[dict]) -> tuple[dict, dict[str, str]]:
    """Fixture plumbing: the native record the given bodies are rebuilt from.

    Rigid membership inside one body comes from a solved lock mate; two bodies
    hang off one solved concentric+coincident hinge so the reduced joint tree
    stays connected.  The capture always reads its base occurrence, even when
    a scenario's scene carries no mass-bearing components.
    """

    names = [str(member) for body in bodies for member in (body.get("components") or [])]
    if not names:
        names = [DEV]
    components = [
        {
            "name2": name,
            "instance_id": name,
            "document": name.split("/")[-1].split("~")[0] + ".SLDPRT",
            "configuration": "Default",
            "fixed": True,
            "suppressed": False,
            "lightweight": False,
            "suppression_state": 2,
            "transform": _carrier_transform(name),
        }
        for name in dict.fromkeys(names)
    ]
    datums = [
        {"name": "CS_" + str(body["name"]), "owner": str(members[0]), "array": _carrier_transform(str(members[0]))}
        for body in bodies
        if (members := list(body.get("components") or []))
    ]
    if not datums:
        datums = [{"name": "CS_base_link", "owner": DEV, "array": _carrier_transform(DEV)}]
    mates: list[dict] = []
    mate_properties: dict = {}
    controlled: dict[str, str] = {}
    for body in bodies:
        members = [str(member) for member in (body.get("components") or [])]
        if len(members) > 1:
            mates.append(
                {
                    "name": "board_rigid",
                    "type": "lock",
                    "suppressed": False,
                    "error_code": 0,
                    "scope": "",
                    "limits": None,
                    "entities": [{"component": member} for member in members],
                }
            )
    named = [body for body in bodies if body.get("components")]
    if len(named) > 1:
        left, right = str(named[0]["components"][0]), str(named[1]["components"][0])
        mates += [
            {
                "name": "hinge_joint__coaxial",
                "type": "concentric",
                "suppressed": False,
                "error_code": 0,
                "scope": "",
                "limits": None,
                "entities": [
                    {
                        "component": left,
                        "feature": "Cyl1",
                        "face_index": 3,
                        "cylinder": {"point": _hinge_point(left), "direction": [0.0, 0.0, 1.0], "radius": 0.005},
                    },
                    {
                        "component": right,
                        "feature": "Cyl2",
                        "face_index": 7,
                        "cylinder": {"point": _hinge_point(right), "direction": [0.0, 0.0, 1.0], "radius": 0.005},
                    },
                ],
            },
            {
                "name": "hinge_joint__locate",
                "type": "coincident",
                "suppressed": False,
                "error_code": 0,
                "scope": "",
                "limits": None,
                "entities": [
                    {
                        "component": left,
                        "feature": "Plane1",
                        "plane": {"point": _hinge_point(left), "normal": [0.0, 0.0, 1.0]},
                    },
                    {
                        "component": right,
                        "feature": "Plane2",
                        "plane": {"point": _hinge_point(right), "normal": [0.0, 0.0, -1.0]},
                    },
                ],
            },
        ]
        mate_properties["hinge_joint__coaxial"] = {
            "dp.joint.axis_sign": "+1",
            "dp.joint.limits_record": "joints/hinge.json#limits",
            "dp.joint.drive_record": "joints/hinge.json#drive",
        }
        controlled["joints/hinge.json"] = json.dumps(
            {"limits": {"lower": -1.0, "upper": 1.0}, "drive": {"effort": 2.0, "velocity": 3.0}}
        )
    masses = [
        {"component": entry["name2"], "mass_kg": 0.035 if entry["name2"] == DEV else 0.026, "material": "FR-4"}
        for entry in components
    ]
    record = {
        "schema_version": DISCOVERY_SCHEMA,
        "contract": CONTRACT,
        "namespace": "dp",
        "identity": {
            "hardware_id": "closure_fixture",
            "revision": "r1",
            "owner": "mechanical-team",
            "change_summary": "closure module fixture",
            "control": {"system": "handoff", "reference": "closure-run"},
            "delivery_configuration": "Default",
            "main_assembly": "robot.SLDASM",
            "robot_name": "robot",
        },
        "saved_configuration": "Default",
        "document_state": support.saved_document_state(),
        "components": components,
        "mates": mates,
        "datums": datums,
        "masses": masses,
        "assembly_mass_kg": 0.0632,
        "assembly_extent_m": 0.2,
        "properties": {"components": {}, "mates": mate_properties},
    }
    return record, controlled


def _hashes(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


class ComponentContextRecordTests(unittest.TestCase):
    def test_native_record_keeps_observations_only(self):
        reading = {
            "assembly": "C:/collected/robot.SLDASM",
            "configuration": "Default",
            "instances": [
                _instance(
                    DEV, depth=1, parent="board-1", document_type="part", mass=0.035,
                    overrides=_flags(), effective=None,
                ),
                _instance(
                    EASYEDA, depth=1, parent="board-1", document_type="part", mass=0.026,
                    overrides=_flags(OverrideMass=True), effective={"mass": 0.026},
                    error="cad_effective_instance_unreadable",
                ),
            ],
            "errors": [{"name": "junk", "error": "cad_component_context_invalid", "message": "bad"}],
            "reference": {"method": "fixture"},
        }
        record = _component_context_record(reading, 0.0632)
        self.assertEqual(
            set(record),
            {"schema_version", "status", "assembly_mass_kg", "instances", "errors", "reference"},
        )
        self.assertEqual(record["status"], "partial")
        self.assertEqual(record["assembly_mass_kg"], 0.0632)
        self.assertEqual(len(record["errors"]), 1)
        self.assertEqual(
            set(record["instances"][0]),
            {
                "name",
                "parent",
                "depth",
                "document",
                "document_type",
                "context_mass_kg",
                "context_volume_m3",
                "overrides",
                "effective",
                "effective_error",
            },
        )
        self.assertEqual(record["instances"][1]["effective"], {"mass": 0.026})
        self.assertEqual(record["instances"][1]["effective_error"], "cad_effective_instance_unreadable")
        for banned in (
            "document_basis_mass_kg",
            "delta_kg",
            "totals",
            "overridden",
            "leaf_documents",
            "leaf_total",
            "effective_total",
            "blockers",
        ):
            self.assertNotIn(banned, record)


class DeriveMassClosureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sw-closure-"))
        (self.tmp / "raw").mkdir()

    def tearDown(self):
        for path in sorted(self.tmp.rglob("*"), reverse=True):
            if path.is_file():
                path.unlink()
            else:
                path.rmdir()
        self.tmp.rmdir()

    def _write(self, relative, payload):
        path = self.tmp / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def _write_evidence(self, bodies, *, status="recorded", raw_extra=None):
        scene = {
            "document": "C:/collected/robot.SLDASM",
            "source_document": "C:/source/robot.SLDASM",
            "components": [
                {
                    "name": DEV,
                    "path": "C:/collected/Dev_Board.SLDPRT",
                    "transform": _placement(),
                    "is_fixed": False,
                    "document_type": "part",
                },
                {
                    "name": EASYEDA,
                    "path": "C:/collected/EasyEDA PCB Model~Nz70.SLDPRT",
                    "transform": _placement((0.0, 0.01, 0.0)),
                    "is_fixed": False,
                    "document_type": "part",
                },
            ],
            "coordinate_systems": {},
            "mass_properties": {
                DEV: _part_payload(0.035, (0.001, 0.0, 0.0)),
                EASYEDA: _effective_payload(0.026, (0.002, 0.0, 0.0), role="leaf", document_type="part", name=EASYEDA),
            },
            "notes": {},
        }
        native = {
            "schema_version": "solidworks-to-urdf.mass-closure/v1",
            "status": status,
        }
        if status == "recorded":
            native.update(
                {
                    "mode": "full",
                    "top_level": {
                        "mass": 0.0632,
                        "com": [0.0015, 0.0, 0.0],
                        "inertia": [[1.5e-6, 0.0, 0.0], [0.0, 1.5e-6, 0.0], [0.0, 0.0, 1.5e-6]],
                        "reference": {},
                    },
                    "component_context": {
                        "schema_version": "solidworks-to-urdf.component-mass-context/v1",
                        "status": "recorded",
                        "assembly_mass_kg": 0.0632,
                        "instances": [
                            _instance(
                                "board-1", depth=0, parent=None, document_type="assembly", mass=0.0632,
                                overrides=_flags(OverrideMass=True),
                                effective=_effective_payload(
                                    0.0632, (0.0015, 0.0, 0.0), role="parent", document_type="assembly", name="board-1"
                                ),
                            ),
                            _instance(
                                DEV, depth=1, parent="board-1", document_type="part", mass=0.035, overrides=_flags()
                            ),
                            _instance(
                                EASYEDA, depth=1, parent="board-1", document_type="part", mass=0.026,
                                overrides=_flags(OverrideMass=True),
                                effective=_effective_payload(
                                    0.026, (0.002, 0.0, 0.0), role="leaf", document_type="part", name=EASYEDA
                                ),
                            ),
                        ],
                        "errors": [],
                        "reference": {"method": "fixture"},
                    },
                }
            )
        else:
            native.update({"reason": "cad_mass_property_unavailable", "message": "no assembly reading"})
        if raw_extra:
            native.update(raw_extra)
        self._write("raw/mass_closure.json", native)
        self._write("raw/scene_raw.json", scene)
        record, controlled = _carrier_record(bodies)
        payload_records = []
        for relative, text in controlled.items():
            path = self.tmp / "input" / "records" / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            payload_records.append(
                {
                    "file": relative,
                    "package_file": relative,
                    "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    "key": "limits",
                }
            )
        self._write(
            "evidence/discovery.json",
            {
                "schema_version": "solidworks-to-urdf.native-discovery/v1",
                "run_id": "fixture-run",
                "derivation": {
                    "main_assembly": "robot.SLDASM",
                    "configuration": "Default",
                    "frozen_names": {},
                    "record_roots": [],
                },
                "identity": {
                    "main_assembly": "robot.SLDASM",
                    "delivery_configuration": "Default",
                    "robot_name": "robot",
                },
                "records": payload_records,
                "raw": record,
                "native_files": {},
            },
        )
        assembly = self.tmp / "input" / "cad" / "robot.SLDASM"
        assembly.parent.mkdir(parents=True, exist_ok=True)
        assembly.write_bytes(b"fixture assembly")

    def test_derive_rebuilds_every_approved_derived_key(self):
        self._write_evidence([{"name": "base_link", "components": [DEV, EASYEDA]}])
        before = _hashes(self.tmp)
        report = derive_mass_closure(self.tmp)
        self.assertEqual(report["schema_version"], "solidworks-to-urdf.mass-closure-report/v1")
        self.assertEqual(
            report["native"],
            json.loads((self.tmp / "raw" / "mass_closure.json").read_text(encoding="utf-8")),
        )
        derived = report["derived"]
        self.assertEqual(
            set(derived),
            {"leaf_total", "effective_total", "blockers", "delta", "totals", "overridden", "coverage", "instances",
             "selection"},
        )
        self.assertEqual(
            set(derived["instances"]),
            {"board-1", DEV, EASYEDA},
        )
        self.assertAlmostEqual(derived["instances"]["board-1"]["document_basis_mass_kg"], 0.061, places=12)
        self.assertAlmostEqual(derived["instances"]["board-1"]["delta_kg"], 0.0022, places=12)
        self.assertAlmostEqual(derived["leaf_total"]["mass"], 0.061, places=12)
        self.assertAlmostEqual(derived["delta"]["mass_abs"], 0.0022, places=12)
        self.assertEqual(derived["effective_total"]["substituted"], ["board-1"])
        self.assertAlmostEqual(derived["effective_total"]["mass"], 0.0632, places=12)
        self.assertEqual(derived["blockers"], [])
        self.assertEqual(derived["totals"]["context_kg"], 0.0632)
        self.assertAlmostEqual(derived["totals"]["document_kg"], 0.061, places=12)
        self.assertAlmostEqual(derived["totals"]["context_minus_assembly_kg"], 0.0, places=12)
        self.assertEqual(derived["overridden"]["mass"], 2)
        self.assertEqual(derived["overridden"]["top_level_overridden"], 1)
        self.assertEqual(
            derived["coverage"],
            {"leaf_documents": 2, "leaf_documents_covered": 2, "leaf_documents_uncovered": []},
        )
        self.assertEqual(_hashes(self.tmp), before)

    def test_selection_gap_is_exact_native_unassigned_mass_without_redistribution(self):
        self._write_evidence([{"name": "base_link", "components": [DEV]}])
        path = self.tmp / "raw/mass_closure.json"
        raw = json.loads(path.read_text())
        raw["top_level"]["mass"] = .061
        parent = raw["component_context"]["instances"][0]
        parent["overrides"] = _flags()
        parent["effective"] = None
        path.write_text(json.dumps(raw))
        before = _hashes(self.tmp)
        selection = derive_mass_closure(self.tmp)["derived"]["selection"]
        self.assertAlmostEqual(selection["selected_mass_kg"], .035, places=12)
        self.assertAlmostEqual(selection["unassigned_mass_kg"], .026, places=12)
        self.assertAlmostEqual(selection["accounting_delta_kg"], 0, places=12)
        self.assertEqual(selection["unassigned"][0]["component"], EASYEDA)
        self.assertFalse(selection["whole_mass_qualified"])
        self.assertEqual(_hashes(self.tmp), before)

    def test_derive_records_an_explicit_blocker_when_the_subtree_spans_bodies(self):
        self._write_evidence(
            [{"name": "base_link", "components": [DEV]}, {"name": "arm_link", "components": [EASYEDA]}]
        )
        report = derive_mass_closure(self.tmp)
        derived = report["derived"]
        self.assertEqual([item["code"] for item in derived["blockers"]], ["effective_instance_spans_bodies"])
        self.assertEqual(derived["effective_total"]["substituted"], [])
        self.assertAlmostEqual(derived["effective_total"]["mass"], 0.061, places=12)

    def test_derive_keeps_unavailable_evidence_with_a_null_delta(self):
        self._write_evidence([{"name": "base_link", "components": [DEV, EASYEDA]}], status="unavailable")
        report = derive_mass_closure(self.tmp)
        self.assertEqual(report["native"]["status"], "unavailable")
        self.assertEqual(report["native"]["reason"], "cad_mass_property_unavailable")
        derived = report["derived"]
        self.assertIsNone(derived["delta"])
        self.assertAlmostEqual(derived["leaf_total"]["mass"], 0.061, places=12)
        self.assertEqual(derived["instances"], {})
        self.assertEqual(derived["totals"]["assembly_kg"], None)

    def test_derive_without_mass_bearings_keeps_a_null_leaf_total(self):
        self._write_evidence([{"name": "base_link", "components": []}])
        scene = json.loads((self.tmp / "raw" / "scene_raw.json").read_text(encoding="utf-8"))
        scene["components"] = []
        scene["mass_properties"] = {}
        self._write("raw/scene_raw.json", scene)
        report = derive_mass_closure(self.tmp)
        self.assertIsNone(report["derived"]["leaf_total"])
        self.assertIsNone(report["derived"]["delta"])
        self.assertIsNone(report["derived"]["effective_total"])


if __name__ == "__main__":
    unittest.main()
