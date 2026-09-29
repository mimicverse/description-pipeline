"""The normalization oracle must re-derive the numbers and catch injected errors."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from . import _paths  # noqa: F401  (import side effect: sys.path)

from description_pipeline.sources.solidworks.freeze import freeze  # noqa: E402
from description_pipeline.sources.solidworks.scene import normalize_scene as canonical_for  # noqa: E402
from description_pipeline.sources.solidworks.verify import verify_normalization  # noqa: E402

from . import support  # noqa: E402


class NormalizationOracleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="sw-oracle-"))
        self.assembly = support.make_cad_tree(self.tmp / "cad")
        self.backend = support.FixtureCadBackend(
            self.assembly,
            [
                {
                    "name": "base-1",
                    "transform": support.placement(),
                    "mass": support.mass_payload(1.0, (0.0, 0.0, 0.0)),
                },
                {
                    "name": "arm-1",
                    "transform": support.placement((0.0, 0.0, 0.2), (0.0, 0.0, 0.0)),
                    "mass": support.mass_payload(
                        0.5, (0.0, 0.0, 0.05), [[0.002, 0.0, 0.0], [0.0, 0.001, 0.0], [0.0, 0.0, 0.001]]
                    ),
                },
            ],
            dependencies=[self.tmp / "cad" / "base.SLDPRT", self.tmp / "cad" / "arm.SLDPRT"],
        )
        self.config = {
            "provider": "solidworks",
            "assembly": str(self.assembly),
            "configuration": "Default",
            "allowed_roots": [str(self.tmp / "cad")],
            "geometry": {"enabled": True},
            "bodies": [
                {"id": "base", "name": "base_link", "components": ["base-1"]},
                {
                    "id": "arm",
                    "name": "arm_link",
                    "components": ["arm-1"],
                    "frame": {"xyz": [0.0, 0.0, 0.2], "rpy": [0.0, 0.0, 0.0]},
                },
            ],
            "joints": [
                {
                    "id": "hinge",
                    "name": "hinge_joint",
                    "type": "revolute",
                    "parent": "base_link",
                    "child": "arm_link",
                    "xyz": [0.0, 0.0, 0.2],
                    "rpy": [0.0, 0.0, 0.0],
                    "axis": [0.0, 0.0, 1.0],
                    "limits": {"lower": -1.0, "upper": 1.0, "effort": 2.0, "velocity": 3.0},
                }
            ],
        }
        self.snapshot = self.tmp / "snapshot"
        freeze(self.config, self.snapshot, backend=self.backend)
        self.raw_scene = support.read_scene(self.snapshot)

    def tearDown(self) -> None:
        support.cleanup(self.tmp)

    def _definition(self, **source_overrides):
        source = dict(self.config)
        source.update(source_overrides)
        return {
            "schema_version": "description.definition/v1",
            "hardware_id": "fixture",
            "source": source,
            "overrides": [],
        }

    def test_oracle_agrees_with_the_canonical_model(self) -> None:
        definition = self._definition()
        canonical = canonical_for(self.raw_scene, definition, self.snapshot)

        results = verify_normalization(self.raw_scene, definition, self.snapshot, canonical)

        statuses = {entry["id"]: entry["status"] for entry in results}
        self.assertEqual(statuses.get("source.normalization.base_link"), "passed", results)
        self.assertEqual(statuses.get("source.normalization.arm_link"), "passed", results)
        self.assertEqual(statuses.get("source.normalization.entities"), "passed", results)

    def test_declared_masses_are_applied_by_the_oracle_too(self) -> None:
        definition = self._definition(
            material_source="documented_table",
            documented_masses={"arm-1": {"mass_kg": 1.5, "reason": "vendor drawing", "evidence": "spec://arm"}},
        )
        canonical = canonical_for(self.raw_scene, definition, self.snapshot)

        results = verify_normalization(self.raw_scene, definition, self.snapshot, canonical)
        arm = next(entry for entry in results if entry["id"].endswith("arm_link"))

        self.assertEqual(arm["status"], "passed")
        self.assertEqual(arm["details"]["recomputed"]["mass_kg"], 1.5)
        self.assertEqual(arm["details"]["canonical"]["mass_kg"], 1.5)

    def test_world_reconciliation_catches_a_frame_joint_conflict(self) -> None:
        # the author frame puts the arm link at z=0.2 but the joint says z=0.0:
        # both link-local comparisons still agree, only world coordinates disagree
        definition = self._definition()
        canonical = canonical_for(self.raw_scene, definition, self.snapshot)
        # the model says the arm link sits at the parent origin while the raw
        # assembly (and the author frame) put it at z=0.2
        for joint in canonical["joints"]:
            if joint["child"] == "arm_link":
                joint["xyz"] = [0.0, 0.0, 0.0]

        results = verify_normalization(self.raw_scene, definition, self.snapshot, canonical)

        world = next(entry for entry in results if entry["id"] == "source.normalization.world")
        self.assertEqual(world["status"], "failed")
        self.assertFalse(world["details"]["links"]["arm_link"]["com_ok"])
        # the link-local comparison cannot see it, which is why the world check exists
        arm = next(entry for entry in results if entry["id"].endswith("arm_link"))
        self.assertEqual(arm["status"], "passed")

    def test_nonzero_root_pose_is_anchored_and_reconciled(self) -> None:
        # the whole robot sits at a non-zero root pose: link frames, joint
        # offsets and raw placements must all agree about it
        offset_backend = support.FixtureCadBackend(
            self.assembly,
            [
                {
                    "name": "base-1",
                    "transform": support.placement((0.1, 0.0, 0.05)),
                    "mass": support.mass_payload(1.0),
                },
                {
                    "name": "arm-1",
                    "transform": support.placement((0.1, 0.0, 0.25)),
                    "mass": support.mass_payload(0.5, (0.0, 0.0, 0.05)),
                },
            ],
            dependencies=[self.tmp / "cad" / "base.SLDPRT", self.tmp / "cad" / "arm.SLDPRT"],
        )
        definition = self._definition(
            bodies=[
                {
                    "id": "base",
                    "name": "base_link",
                    "components": ["base-1"],
                    "frame": {"xyz": [0.1, 0.0, 0.05], "rpy": [0.0, 0.0, 0.0]},
                },
                {
                    "id": "arm",
                    "name": "arm_link",
                    "components": ["arm-1"],
                    "frame": {"xyz": [0.1, 0.0, 0.25], "rpy": [0.0, 0.0, 0.0]},
                },
            ],
            joints=[
                {
                    "id": "hinge",
                    "name": "hinge_joint",
                    "type": "revolute",
                    "parent": "base_link",
                    "child": "arm_link",
                    "xyz": [0.0, 0.0, 0.2],
                    "rpy": [0.0, 0.0, 0.0],
                    "axis": [0.0, 0.0, 1.0],
                    "limits": {"lower": -1.0, "upper": 1.0, "effort": 2.0, "velocity": 3.0},
                }
            ],
        )
        snapshot = self.tmp / "rooted"
        freeze(definition["source"], snapshot, backend=offset_backend)
        raw_scene = support.read_scene(snapshot)
        recorded = raw_scene["provenance"]["world_from_root"]
        self.assertEqual(recorded["source"], "author_declared")
        self.assertAlmostEqual(recorded["xyz"][0], 0.1, places=9)
        self.assertAlmostEqual(recorded["xyz"][2], 0.05, places=9)

        canonical = canonical_for(raw_scene, definition, snapshot)
        results = verify_normalization(raw_scene, definition, snapshot, canonical)

        world = next(entry for entry in results if entry["id"] == "source.normalization.world")
        self.assertEqual(world["status"], "passed", world["details"]["links"])
        self.assertEqual(world["details"]["root_pose"]["source"], "author_declared")
        self.assertAlmostEqual(world["details"]["root_pose"]["xyz"][0], 0.1, places=9)
        self.assertTrue(world["details"]["links"]["arm_link"]["com_ok"])

    def test_tampered_root_anchor_is_caught(self) -> None:
        definition = self._definition()
        canonical = canonical_for(self.raw_scene, definition, self.snapshot)
        canonical["provenance"]["world_from_root"] = {
            "xyz": [9.0, 0.0, 0.0],
            "rpy": [0.0, 0.0, 0.0],
            "source": "author_declared",
        }

        results = verify_normalization(self.raw_scene, definition, self.snapshot, canonical)

        root_frame = next(entry for entry in results if entry["id"] == "source.normalization.root_frame")
        self.assertEqual(root_frame["status"], "failed")

    def test_world_reconciliation_passes_for_a_consistent_model(self) -> None:
        definition = self._definition()
        canonical = canonical_for(self.raw_scene, definition, self.snapshot)

        results = verify_normalization(self.raw_scene, definition, self.snapshot, canonical)

        world = next(entry for entry in results if entry["id"] == "source.normalization.world")
        self.assertEqual(world["status"], "passed", world["details"])
        self.assertTrue(world["details"]["links"]["arm_link"]["inertia_ok"])

    def test_declared_mass_without_evidence_is_flagged(self) -> None:
        definition = self._definition(
            material_source="documented_table",
            documented_masses={"arm-1": {"mass_kg": 1.5, "reason": "vendor drawing"}},
        )
        canonical = canonical_for(self.raw_scene, definition, self.snapshot)

        results = verify_normalization(self.raw_scene, definition, self.snapshot, canonical)

        evidence = next(entry for entry in results if entry["id"] == "source.normalization.declared_masses")
        self.assertEqual(evidence["status"], "failed")
        self.assertEqual(evidence["details"]["without_evidence"], ["arm-1"])

    def test_raw_entity_dropped_from_everywhere_is_caught(self) -> None:
        definition = self._definition()
        # simulate a freeze path that lost arm-1 from the derived provenance and an
        # author list that never mentioned it
        scene = json.loads(json.dumps(self.raw_scene))
        scene["provenance"]["expected_entities"] = ["base-1"]
        definition["source"]["bodies"] = [definition["source"]["bodies"][0]]
        canonical = canonical_for(scene, definition, self.snapshot)

        results = verify_normalization(scene, definition, self.snapshot, canonical)

        entities = next(entry for entry in results if entry["id"] == "source.normalization.entities")
        self.assertEqual(entities["status"], "failed")
        self.assertEqual(entities["details"]["unexpected"], ["arm-1"])

    def test_strict_source_validation(self) -> None:
        from description_pipeline.sources.solidworks.errors import ConfigError
        from description_pipeline.sources.solidworks.freeze import validate_source_config

        bad_number = dict(self.config, material_source="documented_table", documented_masses={"arm-1": 1.5})
        with self.assertRaises(ConfigError) as raised:
            validate_source_config(bad_number)
        self.assertEqual(raised.exception.code, "invalid_config")

        bad_key = dict(self.config, assembly_typo=str(self.assembly))
        with self.assertRaises(ConfigError):
            validate_source_config(bad_key)

        bad_field = dict(
            self.config,
            material_source="documented_table",
            documented_masses={"arm-1": {"mass": 1.5, "reason": "typo"}},
        )
        with self.assertRaises(ConfigError):
            validate_source_config(bad_field)

    def test_flipped_sign_in_a_raw_tensor_fails(self) -> None:
        definition = self._definition()
        canonical = canonical_for(self.raw_scene, definition, self.snapshot)
        tampered = json.loads(json.dumps(self.raw_scene))
        masses_path = self.snapshot / "raw" / "mass_properties.json"
        payload = json.loads(masses_path.read_text(encoding="utf-8"))
        original = json.loads(masses_path.read_text(encoding="utf-8"))
        # a sign error in the raw COM: the tensor alone would hide it
        payload["arm-1"]["com"][2] *= -1.0
        masses_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        try:
            results = verify_normalization(tampered, definition, self.snapshot, canonical)
        finally:
            masses_path.write_text(json.dumps(original, indent=2, sort_keys=True), encoding="utf-8")

        arm = next(entry for entry in results if entry["id"].endswith("arm_link"))
        self.assertEqual(arm["status"], "failed")
        self.assertFalse(arm["details"]["com_ok"])

    def test_dropped_component_fails_the_entity_closure(self) -> None:
        definition = self._definition()
        definition["source"]["bodies"] = [definition["source"]["bodies"][0]]
        canonical = canonical_for(self.raw_scene, definition, self.snapshot)

        results = verify_normalization(self.raw_scene, definition, self.snapshot, canonical)

        entities = next(entry for entry in results if entry["id"] == "source.normalization.entities")
        self.assertEqual(entities["status"], "failed")
        self.assertEqual(entities["missing"], ["arm-1"])


if __name__ == "__main__":
    unittest.main()
