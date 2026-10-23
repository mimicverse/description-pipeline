"""The verifier recomputes selection accounting from the sealed facts."""

import copy
import json
import tempfile
import unittest
from pathlib import Path

from . import _paths  # noqa: F401  (import side effect: sys.path)
from .test_solidworks_closure import (  # noqa: E402
    DEV,
    EASYEDA,
    _carrier_record,
    _effective_payload,
    _flags,
    _instance,
    _part_payload,
    _placement,
)

from description_pipeline.io import PipelineError  # noqa: E402
from description_pipeline.verification.solidworks_physics import (  # noqa: E402
    _direct_link_sum,
    _derive_closure,
    _unassigned_mass_rows,
)

from . import support  # noqa: E402


def _write_evidence(root: Path, bodies, *, top_mass, clear_parent):
    (root / "raw").mkdir(parents=True, exist_ok=True)
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
    parent = _instance(
        "board-1",
        depth=0,
        parent=None,
        document_type="assembly",
        mass=top_mass,
        overrides=_flags() if clear_parent else _flags(OverrideMass=True),
        effective=None
        if clear_parent
        else _effective_payload(top_mass, (0.0015, 0.0, 0.0), role="parent", document_type="assembly", name="board-1"),
    )
    native = {
        "schema_version": "solidworks-to-urdf.mass-closure/v1",
        "status": "recorded",
        "mode": "full",
        "top_level": {
            "mass": top_mass,
            "com": [0.0015, 0.0, 0.0],
            "inertia": [[1.5e-6, 0.0, 0.0], [0.0, 1.5e-6, 0.0], [0.0, 0.0, 1.5e-6]],
            "reference": {},
        },
        "component_context": {
            "schema_version": "solidworks-to-urdf.component-mass-context/v1",
            "status": "recorded",
            "assembly_mass_kg": top_mass,
            "instances": [
                parent,
                _instance(DEV, depth=1, parent="board-1", document_type="part", mass=0.035, overrides=_flags()),
                _instance(
                    EASYEDA,
                    depth=1,
                    parent="board-1",
                    document_type="part",
                    mass=0.026,
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
    (root / "raw/mass_closure.json").write_text(json.dumps(native), encoding="utf-8")
    (root / "raw/scene_raw.json").write_text(json.dumps(scene), encoding="utf-8")
    record, controlled = _carrier_record(bodies)
    support.stage_discovery_inputs(
        root,
        record=record,
        controlled=controlled,
        record_keys={"joints/hinge.json": "limits"} if controlled else None,
    )
    delivery = root / "delivery"
    return support.write_target_delivery(delivery, capture={"scene": scene, "mass_closure": native})


class VerifierSelectionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def test_direct_link_sum_uses_the_configured_body_name_after_native_renaming(self):
        read = {
            "mass_kg": 1.0,
            "com_m": [0.0, 0.0, 0.0],
            "inertia_tensor_about_com": [[1.0, 0.0, 0.0], [0.0, 2.0, 0.0], [0.0, 0.0, 3.0]],
        }
        record = {"links": {"base_link": {"reads": [read, copy.deepcopy(read)]}}}
        source = {"bodies": [{"name": "base_link", "source_name": "CAD_base_body"}]}
        mass, center, inertia = _direct_link_sum(record, source)
        self.assertEqual(mass, 1.0)
        self.assertEqual(center.tolist(), [0.0, 0.0, 0.0])
        self.assertEqual(inertia.tolist(), [[1.0, 0.0, 0.0], [0.0, 2.0, 0.0], [0.0, 0.0, 3.0]])

    def _payloads(self):
        native = json.loads((self.root / "raw/mass_closure.json").read_text(encoding="utf-8"))
        scene = json.loads((self.root / "raw/scene_raw.json").read_text(encoding="utf-8"))
        # The oracle consumes the same rebuilt definition the producer derives
        # from the sealed discovery payload (the frozen carrier input).
        from description_pipeline.sources.solidworks.discovery import source_definition  # noqa: PLC0415

        config = source_definition(
            self.root,
            discovery_name="evidence/discovery.json",
            cad_prefix="input/cad/",
            records_prefix="input/records/",
        )
        return native, scene, config

    def test_unassigned_gap_is_recomputed_exactly_and_bound_to_the_producer(self):
        self.delivery = _write_evidence(
            self.root, [{"name": "base_link", "components": [DEV]}], top_mass=0.061, clear_parent=True
        )
        native, scene, config = self._payloads()
        mine = _derive_closure(native, scene, scene["mass_properties"], config)
        selection = mine["selection"]
        self.assertAlmostEqual(selection["native_mass_kg"], 0.061, places=12)
        self.assertAlmostEqual(selection["unassigned_mass_kg"], 0.026, places=12)
        self.assertAlmostEqual(selection["selected_mass_kg"], 0.035, places=12)
        self.assertAlmostEqual(selection["accounting_delta_kg"], 0.0, places=12)
        self.assertFalse(selection["whole_mass_qualified"])
        self.assertEqual(len(selection["unassigned"]), 1)
        row = selection["unassigned"][0]
        self.assertEqual(row["component"], EASYEDA)
        self.assertEqual(row["document"], "C:/collected/robot.SLDASM")
        self.assertEqual(row["configuration"], "Default")
        self.assertEqual(row["parent"], "board-1")
        self.assertAlmostEqual(row["mass_kg"], 0.026, places=12)

    def test_fully_assigned_selection_stays_qualified_and_tampering_fails(self):
        self.delivery = _write_evidence(
            self.root,
            [{"name": "base_link", "components": [DEV, EASYEDA]}],
            top_mass=0.061,
            clear_parent=True,
        )
        native, scene, config = self._payloads()
        mine = _derive_closure(native, scene, scene["mass_properties"], config)
        self.assertEqual(mine["selection"]["unassigned"], [])
        self.assertTrue(mine["selection"]["whole_mass_qualified"])

    def test_duplicate_inventories_and_instances_are_rejected_not_absorbed(self):
        self.delivery = _write_evidence(
            self.root, [{"name": "base_link", "components": [DEV]}], top_mass=0.061, clear_parent=True
        )
        source = {"bodies": [{"name": "base_link", "components": [DEV]}]}
        self.assertEqual([row["component"] for row in _unassigned_mass_rows(self.delivery, source)], [EASYEDA])
        scene = json.loads((self.root / "raw/scene_raw.json").read_text(encoding="utf-8"))
        duplicated = copy.deepcopy(scene)
        duplicated["components"].append(dict(duplicated["components"][0]))
        (self.root / "raw/scene_raw.json").write_text(json.dumps(duplicated), encoding="utf-8")
        support.store_capture_block(self.delivery, "scene", duplicated)
        with self.assertRaises(PipelineError):
            _unassigned_mass_rows(self.delivery, source)
        (self.root / "raw/scene_raw.json").write_text(json.dumps(scene), encoding="utf-8")
        support.store_capture_block(self.delivery, "scene", scene)
        native = json.loads((self.root / "raw/mass_closure.json").read_text(encoding="utf-8"))
        native["component_context"]["instances"].append(dict(native["component_context"]["instances"][1]))
        (self.root / "raw/mass_closure.json").write_text(json.dumps(native), encoding="utf-8")
        support.store_capture_block(self.delivery, "mass_closure", native)
        with self.assertRaises(PipelineError):
            _unassigned_mass_rows(self.delivery, source)
        with self.assertRaises(ValueError):
            _derive_closure(native, scene, scene["mass_properties"], {"bodies": source["bodies"]})


if __name__ == "__main__":
    unittest.main()
