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

from description_pipeline.sources.solidworks.closure import derive_mass_closure  # noqa: E402
from description_pipeline.sources.solidworks.freeze import _component_context_record  # noqa: E402

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
            "schema_version": "description-pipeline.solidworks-mass-closure/v1",
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
                        "schema_version": "description-pipeline.solidworks-component-mass-context/v1",
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
        self._write("raw/source_config.json", {"assembly": "C:/source/robot.SLDASM", "bodies": bodies})

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
            {"leaf_total", "effective_total", "blockers", "delta", "totals", "overridden", "coverage", "instances"},
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
