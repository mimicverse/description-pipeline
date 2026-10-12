"""True producer -> independent verifier integration for effective instances.

The snapshot is built through the native producer entry points
(``freeze._mass_closure`` and ``freeze._effective_instance_record``), the Linux
report through ``closure.derive_mass_closure``, and the acceptance decision
runs through the independent physics verifier and the real snapshot
``load_scene``.  Nothing here hand-builds the producer's derived block.
"""

from __future__ import annotations

import copy
import importlib
import json
import tempfile
import unittest
from pathlib import Path

import numpy as np

from description_pipeline.sources.snapshot import write_manifest
from description_pipeline.verification.solidworks_physics import verify_physics

# The package re-exports ``freeze`` as a function; bind the modules explicitly.
freeze_module = importlib.import_module("description_pipeline.sources.solidworks.freeze")
scene_module = importlib.import_module("description_pipeline.sources.solidworks.scene")
closure_module = importlib.import_module("description_pipeline.sources.solidworks.closure")

ORIGINAL = "C:\\original\\3.0 总装1008.SLDASM"
COLLECTED = "C:\\captured\\copy\\3.0 总装1008.SLDASM"
PARENT = "board-1"
DEV = "board-1/Dev_Board-1"
PCB = "board-1/EasyEDA PCB Model~Nz70-1"
CHASSIS = "chassis-1"


def placement(rotation, translation):
    grid = np.eye(4)
    grid[:3, :3] = rotation
    grid[:3, 3] = translation
    return grid.ravel().tolist()


def combine(mass_a, com_a, tensor_a, mass_b, com_b, tensor_b):
    total = mass_a + mass_b
    com = (mass_a * com_a + mass_b * com_b) / total
    delta = com_a - com
    inertia = (
        tensor_a
        + mass_a * (float(delta @ delta) * np.eye(3) - np.outer(delta, delta))
        + tensor_b
    )
    delta = com_b - com
    inertia = inertia + mass_b * (float(delta @ delta) * np.eye(3) - np.outer(delta, delta))
    return total, com, inertia


class StubProbeBackend:
    """CAD-API double for the two native mass probes."""

    def __init__(self, top, instances):
        self._top = top
        self._instances = instances

    def assembly_mass_properties(self, path):
        return copy.deepcopy(self._top)

    def assembly_component_mass_properties(self, path):
        return {
            "assembly": path,
            "configuration": "Default",
            "instances": copy.deepcopy(self._instances),
            "errors": [],
            "reference": {"method": "IMassProperty2 (SelectedItems = component instance)"},
        }


class EffectiveInstanceIntegrationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.bundle = Path(directory.name)
        self.evidence = self.bundle / "evidence"
        (self.evidence / "raw").mkdir(parents=True)

        parent_rotation = np.array([[0.0, -1.0, 0.0], [1.0, 0.0, 0.0], [0.0, 0.0, 1.0]])
        child_rotation = np.array([[0.0, 0.0, 1.0], [0.0, 1.0, 0.0], [-1.0, 0.0, 0.0]])
        self.transforms = {
            DEV: placement(child_rotation, [-0.925, -1.166, -0.888]),
            PCB: placement(child_rotation, [-1.125, -0.966, -1.088]),
            CHASSIS: placement(parent_rotation, [0.4, -0.2, 0.3]),
        }
        self.reference = {
            "used_api": "IMassProperty2.GetMomentOfInertia(0)",
            "product_convention": "solidworks_standard",
            "reference_point": "center_of_mass",
            "use_system_units": True,
            "scope": "part_document",
            "axes": "part_document_axes",
            "overrides": {
                "OverrideMass": False,
                "OverrideCenterOfMass": False,
                "OverrideMomentsOfInertia": False,
            },
            "material_assignment": {
                "part": {"name": "Steel", "database": "unit-test"},
                "bodies": [{"material": {}, "effective_source": "part"}],
            },
        }
        self.dev_payload = {
            "mass": 0.035,
            "com": [0.0032, -0.0364, 0.0271],
            "inertia": [[1.18e-5, -2.8e-7, -2.4e-7], [-2.8e-7, 1.90e-5, -2.7e-7], [-2.4e-7, -2.7e-7, 3.07e-5]],
            "reference": dict(self.reference),
        }
        self.chassis_payload = {
            "mass": 0.5,
            "com": [0.01, 0.02, 0.0],
            "inertia": [[5e-4, 0.0, 1e-5], [0.0, 6e-4, 0.0], [1e-5, 0.0, 7e-4]],
            "reference": dict(self.reference),
        }
        overrides_all = {
            "OverrideMass": True,
            "OverrideCenterOfMass": True,
            "OverrideMomentsOfInertia": True,
        }
        overrides_mass = {
            "OverrideMass": True,
            "OverrideCenterOfMass": False,
            "OverrideMomentsOfInertia": False,
        }
        effective_reference = {
            "used_api": "IMassProperty2.GetMomentOfInertia(0)",
            "selection_api": "IMassProperty2 (SelectedItems = component instance)",
            "override_api": "IMassProperty2.GetOverrideOptions (same selection)",
            "scope": "assembly_component_group",
            "axes": "assembly_document_axes",
            "reference_point": "center_of_mass",
            "product_convention": "solidworks_standard",
            "use_system_units": True,
            "effective_instance": True,
            "measurement_basis": "cad_authored_effective_instance",
            "selected_components": [PCB],
            "configuration": "Default",
            "part_document": "C:\\captured\\copy\\EasyEDA PCB Model~Nz70.SLDPRT",
            "assembly_document": COLLECTED,
            "assembly_configuration": "Default",
            "fallback_reason": "cad_part_document_mass_unavailable",
            "fallback_error_code": "cad_empty_model",
            "solid_bodies": 0,
            "sheet_bodies": 2350,
            "overrides": dict(overrides_all),
        }
        self.pcb_payload = {
            "mode": "effective_instance",
            "state": "sheet_only",
            "measurement_basis": "cad_authored_effective_instance",
            "mass": 0.026,
            "com": [-0.00494, -0.03853, 0.01665],
            "inertia": [
                [3.1826e-6, 1.66e-8, 1.4e-9],
                [1.66e-8, 3.0596e-6, 1.77e-7],
                [1.4e-9, 1.77e-7, 6.1142e-6],
            ],
            "reference": effective_reference,
        }
        parent_reference = copy.deepcopy(effective_reference)
        parent_reference.update(
            {
                "selected_components": [PARENT],
                "part_document": "C:\\captured\\copy\\board.SLDASM",
                "overrides": dict(overrides_mass),
                "fallback_reason": None,
                "fallback_error_code": None,
                "solid_bodies": None,
                "sheet_bodies": None,
            }
        )
        self.parent_reading = {
            "role": "parent",
            "document_type": "assembly",
            "mass": 0.0632,
            "com": [-0.000283, -0.037337, 0.022620],
            "inertia": [
                [1.7296e-5, -5.39e-7, -1.55e-6],
                [-5.39e-7, 2.5595e-5, -4.28e-7],
                [-1.55e-6, -4.28e-7, 3.9190e-5],
            ],
            "reference": parent_reference,
        }
        pcb_reading = {
            "role": "leaf",
            "document_type": "part",
            "mass": self.pcb_payload["mass"],
            "com": self.pcb_payload["com"],
            "inertia": self.pcb_payload["inertia"],
            "reference": effective_reference,
        }
        self.readings = {PCB: pcb_reading, PARENT: self.parent_reading}
        self.masses = {
            DEV: self.dev_payload,
            PCB: self.pcb_payload,
            CHASSIS: self.chassis_payload,
        }
        self.raw_scene = {
            "document": COLLECTED,
            "source_document": ORIGINAL,
            "source_root": "C:\\captured\\copy",
            "components": [
                {
                    "name": name,
                    "path": "C:\\captured\\copy\\" + name.rsplit("/", 1)[-1] + ".SLDPRT",
                    "transform": self.transforms[name],
                    "is_fixed": False,
                    "document_type": "part",
                }
                for name in (DEV, PCB, CHASSIS)
            ],
            "coordinate_systems": {"base_datum": np.eye(4).ravel().tolist()},
            "mass_properties": copy.deepcopy(self.masses),
            "notes": {
                "bodies:" + DEV: {"solid": 12, "sheet": 0},
                "bodies:" + PCB: {"solid": 0, "sheet": 2350},
                "bodies:" + CHASSIS: {"solid": 4, "sheet": 0},
                "effective_instances": copy.deepcopy(self.readings),
            },
        }
        self.source = {
            "provider": "solidworks",
            "assembly": ORIGINAL,
            "configuration": "Default",
            "material_source": "cad",
            "bodies": [
                {
                    "id": "base",
                    "name": "base_link",
                    "components": [DEV, PCB, CHASSIS],
                    "frame": {"coordinate_system": "base_datum"},
                }
            ],
            "joints": [],
        }

    def _expected_total(self):
        # Parent substitution replaces the board subtree once; the chassis leaf
        # is placed by its own transform.  Computed with plain numpy.
        rotation = np.array(self.transforms[CHASSIS], dtype=float).reshape(4, 4)
        chassis_com = rotation[:3, :3] @ np.array(self.chassis_payload["com"]) + rotation[:3, 3]
        chassis_tensor = rotation[:3, :3] @ np.array(self.chassis_payload["inertia"]) @ rotation[:3, :3].T
        return combine(
            float(self.parent_reading["mass"]),
            np.array(self.parent_reading["com"], dtype=float),
            np.array(self.parent_reading["inertia"], dtype=float),
            float(self.chassis_payload["mass"]),
            chassis_com,
            chassis_tensor,
        )

    def _build_snapshot(self):
        mass, com, inertia = self._expected_total()
        top_reference = {
            "used_api": "IMassProperty2.GetMomentOfInertia(0)",
            "product_convention": "solidworks_standard",
            "reference_point": "center_of_mass",
            "use_system_units": True,
            "scope": "assembly_document",
            "axes": "assembly_document_axes",
            "overrides": {
                "OverrideMass": False,
                "OverrideCenterOfMass": False,
                "OverrideMomentsOfInertia": False,
            },
        }
        instances = [
            {
                "name": PARENT,
                "parent": None,
                "depth": 0,
                "document": "C:\\captured\\copy\\board.SLDASM",
                "document_type": "assembly",
                "context_mass_kg": float(self.parent_reading["mass"]),
                "context_volume_m3": 1.9e-5,
                "overrides": dict(self.parent_reading["reference"]["overrides"]),
                "effective": copy.deepcopy(self.parent_reading),
                "effective_error": None,
            },
            {
                "name": DEV,
                "parent": PARENT,
                "depth": 1,
                "document": "C:\\captured\\copy\\Dev_Board.SLDPRT",
                "document_type": "part",
                "context_mass_kg": 0.035,
                "context_volume_m3": 9.4e-6,
                "overrides": {
                    "OverrideMass": False,
                    "OverrideCenterOfMass": False,
                    "OverrideMomentsOfInertia": False,
                },
                "effective": None,
                "effective_error": None,
            },
            {
                "name": PCB,
                "parent": PARENT,
                "depth": 1,
                "document": "C:\\captured\\copy\\EasyEDA PCB Model~Nz70.SLDPRT",
                "document_type": "part",
                "context_mass_kg": 0.026,
                "context_volume_m3": 9.74e-6,
                "overrides": dict(self.pcb_payload["reference"]["overrides"]),
                "effective": copy.deepcopy(self.readings[PCB]),
                "effective_error": None,
            },
            {
                "name": CHASSIS,
                "parent": None,
                "depth": 0,
                "document": "C:\\captured\\copy\\chassis.SLDPRT",
                "document_type": "part",
                "context_mass_kg": 0.5,
                "context_volume_m3": 6e-5,
                "overrides": {
                    "OverrideMass": False,
                    "OverrideCenterOfMass": False,
                    "OverrideMomentsOfInertia": False,
                },
                "effective": None,
                "effective_error": None,
            },
        ]
        backend = StubProbeBackend(
            {
                "mass": float(mass),
                "com": com.tolist(),
                "inertia": inertia.tolist(),
                "reference": top_reference,
            },
            instances,
        )
        native = freeze_module._mass_closure(backend, self.source, COLLECTED)
        self.assertIsInstance(native, dict)
        scene_obj = type(
            "Scene",
            (),
            {
                "components": [
                    type(
                        "Component",
                        (),
                        {
                            "name": row["name"],
                            "transform": self.transforms[row["name"]],
                            "path": row["path"],
                            "is_fixed": False,
                            "document_type": "part",
                        },
                    )()
                    for row in self.raw_scene["components"]
                ],
                "coordinate_systems": self.raw_scene["coordinate_systems"],
                "mass_properties": copy.deepcopy(self.masses),
                "notes": copy.deepcopy(self.raw_scene["notes"]),
            },
        )()
        record = freeze_module._effective_instance_record(scene_obj, self.source, self.readings)
        raw = self.evidence / "raw"
        (raw / "scene_raw.json").write_text(json.dumps(self.raw_scene), encoding="utf-8")
        (raw / "mass_properties.json").write_text(json.dumps(self.masses), encoding="utf-8")
        (raw / "mass_closure.json").write_text(json.dumps(native), encoding="utf-8")
        (raw / "effective_instances.json").write_text(json.dumps(record), encoding="utf-8")
        (raw / "declared_masses.json").write_text(
            json.dumps({"items": {}, "evidence": None}), encoding="utf-8"
        )
        (raw / "coordinate_systems.json").write_text(
            json.dumps(self.raw_scene["coordinate_systems"]), encoding="utf-8"
        )
        (raw / "source_config.json").write_text(json.dumps(self.source), encoding="utf-8")
        (raw / "material_provenance.json").write_text(
            json.dumps(
                {
                    "schema_version": "solidworks-to-urdf.material-provenance/v1",
                    "assembly": ORIGINAL,
                    "configuration": "Default",
                    "material_source": "cad",
                    "measured_source": "cad_api",
                    "warnings": [],
                }
            ),
            encoding="utf-8",
        )
        write_manifest(
            self.evidence,
            kind="solidworks",
            identity={"assembly": ORIGINAL, "configuration": "Default", "provider": "solidworks"},
            evidence_class="cad",
        )
        report = closure_module.derive_mass_closure(self.evidence)
        (self.bundle / "reports").mkdir(parents=True, exist_ok=True)
        (self.bundle / "reports/mass-closure.json").write_text(json.dumps(report), encoding="utf-8")
        self.report = report
        return record

    def _verify(self):
        loaded = scene_module.load_scene(self.evidence)
        model = {"links": [{"name": link["name"], "inertial": link["inertial"]} for link in loaded["links"]]}
        checks = verify_physics(self.evidence, self.source, model)
        return loaded, model, {check["id"]: check for check in checks}

    def test_producer_outputs_pass_the_independent_verifier(self):
        record = self._build_snapshot()
        native = json.loads((self.evidence / "raw/mass_closure.json").read_text())
        self.assertEqual(self.report["native"], native)
        self.assertEqual(self.report["derived"]["blockers"], [])
        self.assertEqual(self.report["derived"]["effective_total"]["substituted"], [PARENT])
        self.assertEqual(record["assembly"], ORIGINAL)
        self.assertEqual(record["readings"][PCB]["reference"]["assembly_document"], COLLECTED)
        codes = sorted(warning["code"] for warning in record["warnings"])
        self.assertEqual(
            codes,
            [
                "cad_instance_override_effective",
                "cad_instance_override_effective",
                "cad_surface_only_effective_instance",
            ],
        )
        loaded, _model, checks = self._verify()
        failed = [name for name, check in checks.items() if not check["passed"]]
        self.assertEqual(failed, [], checks)
        link = next(link for link in loaded["links"] if link["name"] == "base_link")
        mass, com, _inertia = self._expected_total()
        self.assertAlmostEqual(link["inertial"]["mass"], mass, places=12)
        self.assertAlmostEqual(self.report["derived"]["effective_total"]["mass"], mass, places=12)
        self.assertTrue(np.allclose(link["inertial"]["xyz"], com, atol=1e-12))

    def test_derived_mutation_is_rejected(self):
        self._build_snapshot()
        report_path = self.bundle / "reports/mass-closure.json"
        report = json.loads(report_path.read_text())
        report["derived"]["effective_total"]["mass"] += 0.01
        report_path.write_text(json.dumps(report), encoding="utf-8")
        _loaded, _model, checks = self._verify()
        self.assertFalse(checks["physics.closure"]["passed"])

    def test_native_record_mutation_is_rejected(self):
        self._build_snapshot()
        raw_path = self.evidence / "raw/mass_closure.json"
        native = json.loads(raw_path.read_text())
        native["component_context"]["instances"][2]["context_mass_kg"] = 0.03
        raw_path.write_text(json.dumps(native), encoding="utf-8")
        # Re-seal the manifest so the snapshot integrity layer cannot mask the
        # report/native drift the physics gate must detect on its own.
        write_manifest(
            self.evidence,
            kind="solidworks",
            identity={"assembly": ORIGINAL, "configuration": "Default", "provider": "solidworks"},
            evidence_class="cad",
        )
        _loaded, _model, checks = self._verify()
        self.assertFalse(checks["physics.closure"]["passed"])


if __name__ == "__main__":
    unittest.main()
