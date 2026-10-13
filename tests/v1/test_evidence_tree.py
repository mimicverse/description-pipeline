"""Guards for the verification evidence-tree accessors (frozen block shapes)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from description_pipeline.delivery import write_delivery_manifest
from description_pipeline.interface_records import check_policy_digest
from description_pipeline.io import PipelineError, digest
from description_pipeline.schemas import CHECK_POLICY_SCHEMA, SOURCE_RECORD_SCHEMA
from description_pipeline.stages import CONTRACT
from description_pipeline.verification import evidence_tree as ev

HEX = "a" * 64


def source_record() -> dict:
    identity = {
        "kind": "source-revision",
        "handoff_digest": HEX,
        "main_assembly": "M3.0.SLDASM",
        "configuration": "Default",
        "hardware_id": "m3.0",
        "records": [],
    }
    return {
        "schema_version": SOURCE_RECORD_SCHEMA,
        "source_revision_id": digest(identity),
        "identity": identity,
        "selection": {
            "main_assembly": "M3.0.SLDASM",
            "configuration": "Default",
            "hardware_id": "m3.0",
            "hardware_source": "cad",
        },
        "metadata": {
            "revision": "r1",
            "owner": "t",
            "control": {"system": "handoff", "reference": "local"},
            "change_summary": "t",
            "change_summary_source": "submission",
        },
    }


class EvidenceTreeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "delivery"
        for name in ("input", "evidence", "model", "urdf", "meshes"):
            (self.root / name).mkdir(parents=True)
        (self.root / "input/handoff.json").write_text("{}")
        (self.root / "input/source.json").write_text(json.dumps(source_record()))
        (self.root / "tool.json").write_text("{}")
        (self.root / "reports").mkdir()
        (self.root / "reports/quality.json").write_text("{}")
        (self.root / "evidence/discovery.json").write_text(json.dumps({"derived": {}}))
        (self.root / "evidence/native-runtime.json").write_text(json.dumps({"solidworks": {"revision": "34.0"}}))
        capture = {
            "scene": {"mass_properties": {"a": {"mass": 1}}, "coordinate_systems": {"CS_a": [[1, 0, 0, 0]] * 4}},
            "instances": [{"name": "a", "mass": 1, "com": [0, 0, 0], "inertia": [0] * 9}],
            "components": [{"name": "a", "mass_properties": {"mass": 1}}],
            "datums": [{"name": "CS_a", "matrix": [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]}],
            "axes": [{"entity": "AX_j", "kinds": ["reference_axis"]}],
            "meshes": [
                {"triangles": 4, "path": "a.stl", "sha256": HEX, "tessellation": {"faces": [], "no_facets_faces": []}}
            ],
            "geometry": [{"component": "a", "faces": 1}],
            "mass_closure": {"top_level": {}, "component_context": {"instances": []}, "unassigned": []},
            "material_provenance": {"a": {"density": 1}},
            "declared_masses": {"items": {}, "evidence": {}},
            "dependency_closure": {"declared_unresolved": [], "configuration": "Default"},
            "link_group_mass_properties": {"a": {"mass": 1}},
            "collection": {
                "kind": "solidworks",
                "evidence_class": "cad",
                "identity": source_record()["identity"],
                "limits": {"native_cad": True},
                "environment": {"solidworks": {"revision": "34.0"}},
            },
        }
        (self.root / "evidence/capture.json").write_text(json.dumps(capture))
        write_delivery_manifest(
            self.root,
            contract={"schema_version": CONTRACT["schema_version"], "digest": HEX},
            policy={"schema_version": CHECK_POLICY_SCHEMA, "digest": check_policy_digest()},
        )

    def test_accessors_read_the_frozen_blocks(self) -> None:
        self.assertEqual(ev.source(self.root)["selection"]["hardware_id"], "m3.0")
        self.assertEqual(ev.handoff(self.root), {})
        self.assertEqual(ev.runtime(self.root)["solidworks"]["revision"], "34.0")
        self.assertEqual(ev.discovery(self.root), {"derived": {}})
        self.assertEqual(ev.scene(self.root)["mass_properties"], {"a": {"mass": 1}})
        self.assertEqual(ev.mass_properties(self.root), {"a": {"mass": 1}})
        self.assertEqual(ev.datums(self.root)["CS_a"][0], 1)
        self.assertEqual(ev.axes(self.root)[0]["kinds"], ["reference_axis"])
        self.assertEqual(len(ev.instances(self.root)), 1)
        self.assertEqual(len(ev.components(self.root)), 1)
        self.assertEqual(ev.meshes(self.root)[0]["triangles"], 4)
        self.assertEqual(ev.geometry(self.root)[0]["component"], "a")
        self.assertEqual(ev.mass_closure(self.root)["unassigned"], [])
        self.assertEqual(ev.material_provenance(self.root)["a"]["density"], 1)
        self.assertEqual(ev.declared_masses(self.root)["items"], {})
        self.assertEqual(ev.dependency_closure(self.root)["configuration"], "Default")
        self.assertEqual(ev.link_group_mass_properties(self.root)["a"]["mass"], 1)
        self.assertEqual(ev.collection(self.root)["limits"]["native_cad"], True)
        # Both root conventions resolve the same tree.
        self.assertEqual(ev.scene(self.root / "evidence"), ev.scene(self.root))

    def test_missing_blocks_fail_closed(self) -> None:
        capture = json.loads((self.root / "evidence/capture.json").read_text())
        del capture["material_provenance"]
        (self.root / "evidence/capture.json").write_text(json.dumps(capture))
        with self.assertRaises(PipelineError):
            ev.material_provenance(self.root)
        with self.assertRaises(PipelineError):
            ev.datums(self.root / "does-not-exist")


if __name__ == "__main__":
    unittest.main()
