"""Effective CAD-authored instance measurements: rows, substitution, records.

Covers the sheet-only/overridden-instance fallback contract: raw
assembly-scope rows are converted (never relabelled), build_scene substitutes
the shallowest overridden ancestor per rigid body while keeping child
visuals, and the freeze records carry the canonical readings/warnings plus
the effective recombination and its explicit blockers.
"""

from __future__ import annotations

import types
import unittest

from . import _paths  # noqa: F401  (import side effect: sys.path)

from description_pipeline.sources.solidworks.freeze import _effective_instance_record  # noqa: E402
from description_pipeline.sources.solidworks.scene import (  # noqa: E402
    _PartReading,
    build_scene,
    identity_matrix,
)

IDENTITY = identity_matrix()
DATUM_ROW = [1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0]
DEV = "board-1/Dev_Board-1"
EASYEDA = "board-1/EasyEDA PCB Model~Nz70-1"


def _placement(xyz=(0.0, 0.0, 0.0)):
    return [1.0, 0.0, 0.0, xyz[0], 0.0, 1.0, 0.0, xyz[1], 0.0, 0.0, 1.0, xyz[2], 0.0, 0.0, 0.0, 1.0]


def _part_payload(mass, com=(0.0, 0.0, 0.0)):
    return {
        "mass": mass,
        "com": list(com),
        "inertia": [[1e-6, 0.0, 0.0], [0.0, 1e-6, 0.0], [0.0, 0.0, 1e-6]],
        "reference": {"used_api": "fixture", "product_convention": "solidworks_standard"},
    }


def _flags(**active):
    flags = {"OverrideMass": False, "OverrideCenterOfMass": False, "OverrideMomentsOfInertia": False}
    flags.update(active)
    return flags


def _effective_reading(mass, com, *, role, document_type, name):
    return {
        "role": role,
        "document_type": document_type,
        "mass": mass,
        "com": list(com),
        "inertia": [[2e-6, 1e-8, 0.0], [1e-8, 2.5e-6, 0.0], [0.0, 0.0, 4e-6]],
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
            "part_document": "C:/cad/" + name.split("/")[-1] + ".SLDPRT",
            "assembly_document": "C:/cad/robot.SLDASM",
            "assembly_configuration": "Default",
            "overrides": _flags(OverrideMass=True),
        },
    }


def _leaf(name, transform, payload):
    return types.SimpleNamespace(
        name=name,
        transform=transform,
        path="C:/cad/" + name.split("/")[-1] + ".SLDPRT",
        is_fixed=False,
        document_type="part",
    )


class EffectiveInstanceSceneTests(unittest.TestCase):
    def test_part_reading_uses_assembly_scope_values_verbatim(self):
        payload = _effective_reading(0.026, (0.01, 0.02, 0.03), role="leaf", document_type="part", name=EASYEDA)
        reading = _PartReading(EASYEDA, _placement((0.12, 0.0, 0.0)), payload, IDENTITY, (0.0, 0.0, 0.0))
        self.assertEqual(reading.assembly_com, (0.01, 0.02, 0.03))
        self.assertEqual(
            [list(row) for row in reading.assembly_inertia],
            [[2e-6, 1e-8, 0.0], [1e-8, 2.5e-6, 0.0], [0.0, 0.0, 4e-6]],
        )
        self.assertEqual(reading.link_com, (0.01, 0.02, 0.03))
        self.assertEqual(reading.link_inertia, reading.assembly_inertia)

    def test_part_document_rows_keep_the_part_frame_pipeline(self):
        reading = _PartReading(
            DEV, _placement((0.1, 0.0, 0.0)), _part_payload(0.035, (0.01, 0.0, 0.0)), IDENTITY, (0.0, 0.0, 0.0)
        )
        self.assertEqual(reading.assembly_com, (0.11, 0.0, 0.0))

    def _scene(self, notes):
        dev_payload = _part_payload(0.035, (0.001, 0.0, 0.0))
        easyeda_payload = _effective_reading(0.026, (0.002, 0.0, 0.0), role="leaf", document_type="part", name=EASYEDA)
        return types.SimpleNamespace(
            components=[
                _leaf(DEV, _placement((0.0, 0.0, 0.0)), dev_payload),
                _leaf(EASYEDA, _placement((0.0, 0.01, 0.0)), easyeda_payload),
            ],
            coordinate_systems={"base_datum": DATUM_ROW},
            mass_properties={DEV: dev_payload, EASYEDA: easyeda_payload},
            notes=notes,
        )

    def _cfg(self):
        return {
            "bodies": [
                {
                    "id": "base",
                    "name": "base_link",
                    "components": [DEV, EASYEDA],
                    "frame": {"coordinate_system": "base_datum"},
                }
            ],
            "joints": [],
        }

    def test_build_scene_substitutes_the_overridden_parent_subtree(self):
        parent = _effective_reading(0.0632, (0.0015, 0.0, 0.0), role="parent", document_type="assembly", name="board-1")
        scene = build_scene(self._cfg(), self._scene({"effective_instances": {"board-1": parent}}), [])
        link = scene["links"][0]
        self.assertAlmostEqual(link["inertial"]["mass"], 0.0632, places=12)
        self.assertEqual(link["provenance"]["effective_instances"], ["board-1"])
        # Every child stays in the evidence (visuals) even when its mass is
        # replaced wholesale by the parent reading.
        self.assertEqual(sorted(link["provenance"]["source_entities"]), sorted([DEV, EASYEDA]))

    def test_build_scene_without_notes_combines_the_leaf_readings(self):
        scene = build_scene(self._cfg(), self._scene({}), [])
        link = scene["links"][0]
        self.assertAlmostEqual(link["inertial"]["mass"], 0.061, places=12)
        self.assertNotIn("effective_instances", link["provenance"])

    def test_flagless_native_context_is_primary_and_keeps_part_diagnostic(self):
        reading = _effective_reading(0.04, (0.01, 0, 0), role="leaf", document_type="part", name=DEV)
        reading["reference"]["overrides"] = _flags()
        reading["reference"]["measurement_basis"] = "cad_assembly_instance_measurement"
        raw = self._scene({"effective_instances": {DEV: reading}})
        result = build_scene(self._cfg(), raw, [])
        self.assertAlmostEqual(result["links"][0]["inertial"]["mass"], 0.066)
        self.assertEqual(raw.mass_properties[DEV]["mass"], 0.035)
        self.assertEqual(result["links"][0]["provenance"]["effective_instances"], [DEV])

    def test_flagless_context_uses_the_same_primary_value_in_closure_and_warning(self):
        from description_pipeline.sources.solidworks.closure import _derive

        reading = _effective_reading(0.04, (0.01, 0, 0), role="leaf", document_type="part", name=DEV)
        reading["reference"]["overrides"] = _flags()
        reading["reference"]["measurement_basis"] = "cad_assembly_instance_measurement"
        reading["part_measurement"] = {"mass_kg": 0.035}
        scene = self._scene({"effective_instances": {DEV: reading}})
        record = _effective_instance_record(scene, {"assembly": "robot.SLDASM"}, {DEV: reading})
        warning = record["warnings"][0]
        self.assertEqual(warning["code"], "cad_instance_measurement_differs_from_part")
        self.assertEqual(warning["measurement"]["mass_kg"], 0.04)
        self.assertEqual(warning["part_measurement"]["mass_kg"], 0.035)
        raw = {"components": [{"name": c.name, "transform": c.transform} for c in scene.components],
               "mass_properties": scene.mass_properties}
        native = {"component_context": {"instances": [
            {"name": DEV, "depth": 1, "document_type": "part", "context_mass_kg": 0.04,
             "overrides": _flags(), "effective": reading},
        ]}}
        derived = _derive(native, raw, self._cfg())
        self.assertAlmostEqual(derived["leaf_total"]["mass"], 0.061)
        self.assertAlmostEqual(derived["effective_total"]["mass"], 0.066)
        self.assertEqual(derived["effective_total"]["substituted"], [])


class EffectiveInstanceRecordTests(unittest.TestCase):
    def _instance(self, name, *, depth, parent, document_type, mass, overrides, effective=None, error=None):
        entry = {
            "name": name,
            "parent": parent,
            "depth": depth,
            "document": "C:/cad/robot.SLDASM",
            "document_type": document_type,
            "context_mass_kg": mass,
            "context_volume_m3": None,
            "overrides": overrides,
        }
        if effective is not None:
            entry["effective"] = effective
        if error is not None:
            entry["effective_error"] = error
        return entry

    def _reading(self, instances):
        return {
            "assembly": "C:/cad/robot.SLDASM",
            "configuration": "Default",
            "instances": instances,
            "errors": [],
            "reference": {"method": "fixture"},
        }

    def _scene(self):
        dev_payload = _part_payload(0.035, (0.001, 0.0, 0.0))
        easyeda_payload = _effective_reading(0.026, (0.002, 0.0, 0.0), role="leaf", document_type="part", name=EASYEDA)
        return types.SimpleNamespace(
            components=[_leaf(DEV, _placement(), dev_payload), _leaf(EASYEDA, _placement(), easyeda_payload)],
            mass_properties={DEV: dev_payload, EASYEDA: easyeda_payload},
        )

    def _parent(self):
        return _effective_reading(0.0632, (0.0015, 0.0, 0.0), role="parent", document_type="assembly", name="board-1")

    def _leaf_effective(self):
        return _effective_reading(0.026, (0.002, 0.0, 0.0), role="leaf", document_type="part", name=EASYEDA)

    def test_effective_instance_record_carries_identity_and_the_override_warning(self):
        scene = self._scene()
        easyeda_row = scene.mass_properties[EASYEDA]
        easyeda_row["reference"].update(
            {
                "fallback_reason": "cad_part_document_mass_unavailable",
                "fallback_error_code": "cad_empty_model",
                "solid_bodies": 0,
                "sheet_bodies": 2350,
                "overrides": _flags(OverrideMass=True, OverrideCenterOfMass=True, OverrideMomentsOfInertia=True),
            }
        )
        easyeda_row["mode"] = "effective_instance"
        easyeda_row["state"] = "sheet_only"
        easyeda_row["measurement_basis"] = "cad_authored_effective_instance"
        record = _effective_instance_record(
            scene, {"assembly": "C:/cad/robot.SLDASM", "configuration": "Default"}, {"board-1": self._parent()}
        )
        self.assertEqual(record["schema_version"], "solidworks-to-urdf.effective-instances/v1")
        self.assertEqual(sorted(record["readings"]), ["board-1"])
        self.assertEqual(
            [warning["code"] for warning in record["warnings"]],
            ["cad_instance_override_effective", "cad_surface_only_effective_instance"],
        )
        override = record["warnings"][0]
        self.assertFalse(override["blocking"])
        self.assertTrue(override["message"])
        self.assertEqual(override["selected_components"], ["board-1"])
        self.assertEqual(override["measurement"]["scope"], "assembly_component_group")
        surface = record["warnings"][1]
        self.assertTrue(surface["message"])
        self.assertEqual(surface["component"], EASYEDA)
        self.assertEqual(surface["sheet_bodies"], 2350)
        self.assertEqual(surface["fallback_error_code"], "cad_empty_model")
        self.assertEqual(surface["measurement"]["measurement_basis"], "cad_authored_effective_instance")

    def test_effective_instance_record_is_written_empty_when_nothing_is_effective(self):
        scene = types.SimpleNamespace(mass_properties={DEV: _part_payload(0.035)})
        record = _effective_instance_record(scene, {"assembly": "A", "configuration": "Default"}, {})
        self.assertEqual(record["readings"], {})
        self.assertEqual(record["warnings"], [])


if __name__ == "__main__":
    unittest.main()
