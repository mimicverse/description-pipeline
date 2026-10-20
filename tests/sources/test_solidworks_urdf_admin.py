"""Focused admin-deferral tests for the bundle verifier (no CAD, no pipeline)."""

from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from description_pipeline.io import PipelineError
from description_pipeline.verification import solidworks_urdf

from . import support


def full_identity():
    return {
        "hardware_id": "fixture",
        "revision": "r1",
        "owner": "test-owner",
        "change_summary": "summary",
        "control": {"system": "handoff", "reference": "fixture"},
        "robot_name": "fixture",
    }


def canonical_digest(files):
    return hashlib.sha256(
        (json.dumps(files, sort_keys=True, ensure_ascii=False, allow_nan=False, indent=2) + "\n").encode()
    ).hexdigest()


class InputSealReadinessTests(unittest.TestCase):
    def _root(self, directory, *, raw_identity, revision):
        root = Path(directory) / "delivery"
        content = b"native bytes for the admin-deferral tests"
        record = support.frozen_discovery_payload(
            {
                "namespace": "dp",
                "raw": {
                    "identity": raw_identity,
                    "components": [
                        {
                            "name2": "base_part-1",
                            "document": "robot.SLDPRT",
                            "suppressed": False,
                            "lightweight": False,
                            "suppression_state": 2,
                        }
                    ],
                    "datums": [
                        {
                            "name": "CS_base_link",
                            "owner": "base_part-1",
                            "array": [
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
                            ],
                        }
                    ],
                    "mates": [],
                    "properties": {"components": {}},
                },
                "identity": raw_identity,
            },
            files={"robot.SLDASM": hashlib.sha256(content).hexdigest()},
        )
        source = support.default_source_record(
            main_assembly="robot.SLDASM",
            configuration="Default",
            hardware_id="fixture",
            revision=revision,
        )
        return support.write_target_delivery(
            root,
            capture={"declared_masses": {"items": {}, "evidence": None}},
            discovery=record,
            source=source,
            originals={"robot.SLDASM": content.decode("utf-8")},
        )

    def test_absent_seal_with_incomplete_admin_metadata_is_a_readiness_warning(self):
        # Valid hardware but a missing owner/revision keeps the seal optional.
        with tempfile.TemporaryDirectory() as directory:
            identity = full_identity()
            identity["revision"] = None
            identity["owner"] = None
            root = self._root(directory, raw_identity=identity, revision=None)
            diagnostics: list[dict] = []
            returned = solidworks_urdf._input(root, diagnostics)
            self.assertEqual(returned["source"]["assembly"], "robot.SLDASM")
            self.assertEqual(returned["hardware_id"], "fixture")
            self.assertEqual([item["code"] for item in diagnostics], ["input.cad_revision_absent"])
            self.assertEqual(diagnostics[0]["detail"]["missing"], ["revision", "owner"])
            self.assertTrue(all(item["blocking"] is False for item in diagnostics))

    def test_absent_seal_with_null_identity_is_a_readiness_warning(self):
        with tempfile.TemporaryDirectory() as directory:
            identity = dict.fromkeys(("hardware_id", "revision", "owner", "change_summary", "control", "robot_name"))
            root = self._root(directory, raw_identity=identity, revision=None)
            diagnostics: list[dict] = []
            solidworks_urdf._input(root, diagnostics)
            self.assertEqual(
                diagnostics[0]["detail"]["missing"],
                ["hardware_id", "revision", "owner", "change_summary", "control"],
            )

    def test_absent_revision_stays_missing_with_complete_admin_metadata(self):
        """The revision label is optional raw metadata; a complete authored
        identity does not make its absence blocking."""

        with tempfile.TemporaryDirectory() as directory:
            root = self._root(directory, raw_identity=full_identity(), revision=None)
            diagnostics: list[dict] = []
            returned = solidworks_urdf._input(root, diagnostics)
            self.assertEqual(returned["source"]["assembly"], "robot.SLDASM")
            self.assertEqual([item["code"] for item in diagnostics], ["input.cad_revision_absent"])
            self.assertEqual(diagnostics[0]["detail"]["missing"], [])
            self.assertTrue(all(item["blocking"] is False for item in diagnostics))

    def test_malformed_revision_metadata_keeps_failing_with_incomplete_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            identity = full_identity()
            identity["revision"] = None
            identity["owner"] = None
            root = self._root(directory, raw_identity=identity, revision="")
            with self.assertRaises(PipelineError):
                solidworks_urdf._input(root, [])


class UrdfRobotLabelTests(unittest.TestCase):
    @staticmethod
    def _root(directory, name):
        root = Path(directory)
        (root / "urdf").mkdir()
        (root / "urdf/robot.urdf").write_text(f'<robot name="{name}"><link name="base"/></robot>')
        return root

    def test_robot_tag_must_match_the_definition_robot_name(self):
        model = {"links": [{"name": "base"}], "frames": [], "joints": []}
        with tempfile.TemporaryDirectory() as directory:
            root = self._root(directory, "robot")
            definition = {"hardware_id": None, "source": {"robot_name": "robot"}}
            document = solidworks_urdf._xml(root, definition, model)
            self.assertEqual(document.get("name"), "robot")
        with tempfile.TemporaryDirectory() as directory:
            # A hardware label is not the URDF identity anymore.
            root = self._root(directory, "fixture")
            definition = {"hardware_id": "fixture", "source": {"robot_name": "robot"}}
            with self.assertRaises(PipelineError):
                solidworks_urdf._xml(root, definition, model)

    def test_urdf_warning_comments_must_match_the_model_exactly(self):
        warning = {
            "code": "units.si_conversion",
            "message": "Delivered values use SI units.",
            "objects": [{"object": "robot", "source": "native_derived", "field": "units"}],
        }
        model = {
            "links": [{"name": "base"}],
            "frames": [],
            "joints": [],
            "provenance": {"value_warnings": [warning]},
        }
        definition = {"hardware_id": None, "source": {"robot_name": "robot"}}
        exact = (
            '<robot name="robot">'
            "<!--WARNING units.si_conversion: Delivered values use SI units. "
            "See reports/quality.json for object and field details.-->"
            '<link name="base"/></robot>'
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "urdf").mkdir()
            (root / "urdf/robot.urdf").write_text(exact)
            self.assertEqual(solidworks_urdf._xml(root, definition, model).get("name"), "robot")
        for payload in (
            '<robot name="robot"><link name="base"/></robot>',
            exact.replace("Delivered values use SI units.", "All values were read directly from CAD."),
        ):
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "urdf").mkdir()
                (root / "urdf/robot.urdf").write_text(payload)
                with self.assertRaisesRegex(PipelineError, "value-provenance"):
                    solidworks_urdf._xml(root, definition, model)


class ValueWarningGateTests(unittest.TestCase):
    def setUp(self):
        super().setUp()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        (self.root / "evidence/geometry").mkdir(parents=True)
        (self.root / "evidence/geometry/mesh.stl").write_bytes(b"mesh")

    @staticmethod
    def _facts():
        digest = hashlib.sha256(b"mesh").hexdigest()
        source = {
            "robot_name": "registered_robot",
            "interface_names": {
                "robot_name": "registered_robot",
                "visual:part-1": "native_0",
                "material:part-1": "cad_gray",
                "mesh:part-1": f"../meshes/visual/reference/native_{digest}.stl",
            },
            "bodies": [{"name": "base_link"}],
            "joints": [],
        }
        model = {
            "links": [
                {
                    "name": "base_link",
                    "inertial": {"mass": 1.0},
                    "visuals": [
                        {
                            "name": "native_0",
                            "material": "cad_gray",
                            "mesh_name": f"meshes/visual/reference/native_{digest}.stl",
                            "sha256": digest,
                            "filename": "geometry/mesh.stl",
                            "scale": [1.0, 1.0, 1.0],
                            "rgba": [0.7, 0.7, 0.7, 1.0],
                        }
                    ],
                }
            ],
            "joints": [],
            "provenance": {
                "value_warnings": [
                    {
                        "code": "naming.registered_interface",
                        "message": "Delivered names or display values come from a registered naming mapping.",
                        "objects": [
                            {
                                "object_type": "visual",
                                "object": "native_0",
                                "field": "name",
                                "actual": "native_0",
                                "source": "registered display name",
                            },
                            {
                                "object_type": "material",
                                "object": "base_link:native_0",
                                "field": "material",
                                "actual": "cad_gray",
                                "source": "registered display material",
                            },
                        ],
                    },
                    {
                        "code": "naming.historical_mesh_alias",
                        "message": (
                            "Mesh paths use controlled historical labels; the manifest current SHA-256 "
                            "is the content identity."
                        ),
                        "objects": [
                            {
                                "object_type": "visual",
                                "object": "native_0",
                                "field": "mesh filename",
                                "actual": f"meshes/visual/reference/native_{digest}.stl",
                                "source": "registered historical mesh label",
                                "current_sha256": digest,
                            }
                        ],
                    },
                    {
                        "code": "naming.registered_robot_name",
                        "message": (
                            "The robot name comes from the registered engineering identity because the "
                            "CAD declaration is absent."
                        ),
                        "objects": [
                            {
                                "object_type": "robot",
                                "object": "registered_robot",
                                "field": "name",
                                "actual": "registered_robot",
                                "source": "registered engineering identity",
                            }
                        ],
                    },
                    {
                        "code": "geometry.derived_from_native",
                        "message": "Geometry, frame transforms and signed joint axes are derived from native evidence.",
                        "objects": [
                            {
                                "object_type": "link",
                                "object": "base_link",
                                "fields": ["visual.xyz", "visual.rpy", "visual.filename", "visual.scale"],
                                "actual": "derived from native occurrence transforms and capture geometry",
                                "source": "native_derived",
                            }
                        ],
                    },
                    {
                        "code": "units.si_conversion",
                        "message": "Delivered values use SI units after conversion from native CAD units.",
                        "objects": [
                            {
                                "object_type": "robot",
                                "object": "registered_robot",
                                "field": "units",
                                "actual": "SI",
                                "source": "native_derived",
                            }
                        ],
                    },
                    {
                        "code": "physics.derived_from_native",
                        "message": (
                            "Link mass properties are transformed, translated or recombined from native readings."
                        ),
                        "objects": [
                            {
                                "object_type": "link",
                                "object": "base_link",
                                "fields": ["inertial.mass", "inertial.xyz", "inertial.rpy", "inertial.inertia"],
                                "actual": "transformed or recombined from native readings",
                                "source": "native_derived",
                            }
                        ],
                    },
                    {
                        "code": "display.constant",
                        "message": "Display scale and material color are platform constants, not CAD readings.",
                        "objects": [
                            {
                                "object_type": "visual",
                                "object": "native_0",
                                "fields": ["scale", "material.rgba"],
                                "actual": {"scale": [1.0, 1.0, 1.0], "rgba": [0.7, 0.7, 0.7, 1.0]},
                                "source": "display constant",
                            }
                        ],
                    },
                ]
            },
        }
        return source, model

    def test_valid_warning_inventory_is_reconstructed(self):
        source, model = self._facts()
        result = solidworks_urdf._value_warning_gate(self.root, source, model)
        self.assertEqual(result["objects"], 8)

    def test_missing_fabricated_or_misaddressed_entries_are_rejected(self):
        mutations = {
            "missing-class": lambda warnings: warnings.pop(4),
            "fabricated-class": lambda warnings: warnings.append(
                {
                    "code": "axis.registered_sign_mapping",
                    "message": "fabricated",
                    "objects": [{"object": "x", "source": "y", "field": "z"}],
                }
            ),
            "wrong-object": lambda warnings: warnings[0]["objects"][0].update(object="native_999"),
            "wrong-field": lambda warnings: warnings[3]["objects"][0].update(fields=["rpy"]),
            "wrong-message": lambda warnings: warnings[3].update(message="All values were read directly from CAD"),
            "omitted-derived-value": lambda warnings: warnings[5]["objects"].pop(),
            "extra-derived-value": lambda warnings: warnings[3]["objects"].append(
                {
                    "object_type": "link",
                    "object": "native_999",
                    "fields": ["visual.xyz", "visual.rpy", "visual.filename", "visual.scale"],
                    "actual": "derived from native occurrence transforms and capture geometry",
                    "source": "native_derived",
                }
            ),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                source, model = self._facts()
                warnings = copy.deepcopy(model["provenance"]["value_warnings"])
                mutate(warnings)
                model["provenance"]["value_warnings"] = warnings
                with self.assertRaises(PipelineError):
                    solidworks_urdf._value_warning_gate(self.root, source, model)

    def test_historical_alias_must_expose_the_current_digest(self):
        source, model = self._facts()
        model["provenance"]["value_warnings"][1]["objects"][0]["current_sha256"] = "b" * 64
        with self.assertRaisesRegex(PipelineError, "historical_mesh_alias"):
            solidworks_urdf._value_warning_gate(self.root, source, model)
        source, model = self._facts()
        model["links"][0]["visuals"][0]["sha256"] = "b" * 64
        model["provenance"]["value_warnings"][1]["objects"][0]["current_sha256"] = "b" * 64
        with self.assertRaisesRegex(PipelineError, "current digest"):
            solidworks_urdf._value_warning_gate(self.root, source, model)


class MeshAssetReuseTests(unittest.TestCase):
    def test_every_actual_asset_may_be_referenced_more_than_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "meshes/visual").mkdir(parents=True)
            (root / "meshes/visual/a.stl").write_bytes(b"a")
            (root / "meshes/visual/b.stl").write_bytes(b"b")
            result = solidworks_urdf._assets(
                root,
                ["meshes/visual/a.stl", "meshes/visual/a.stl", "meshes/visual/b.stl"],
            )
            self.assertEqual(result, {"meshes": 2, "references": 3})
            with self.assertRaisesRegex(PipelineError, "Unused or multiply"):
                solidworks_urdf._assets(root, ["meshes/visual/a.stl", "meshes/visual/a.stl"])
            with self.assertRaisesRegex(PipelineError, "Unused or multiply"):
                solidworks_urdf._assets(
                    root,
                    ["meshes/visual/a.stl", "meshes/visual/b.stl", "meshes/visual/missing.stl"],
                )


class HandoffHashBindingTests(unittest.TestCase):
    def _package(self, directory, contents, *, raw_files=None, inventory_digest=None):
        root = Path(directory) / "delivery"
        native = {name: hashlib.sha256(data).hexdigest() for name, data in contents.items()}
        payload = support.frozen_discovery_payload(
            {"namespace": "dp", "raw": {"files": dict(raw_files or {})}, "identity": {}},
            files=native,
        )
        if inventory_digest is not None:
            payload["native_inventory_sha256"] = inventory_digest
        return support.write_target_delivery(
            root,
            capture={},
            discovery=payload,
            source=support.default_source_record(),
            originals={name: data.decode("utf-8") for name, data in contents.items()},
        ), native

    def test_frozen_native_inventory_binds_without_a_seal(self):
        contents = {"a.SLDPRT": b"aaa", "b.SLDPRT": b"bbb"}
        with tempfile.TemporaryDirectory() as directory:
            hashes = {"a": hashlib.sha256(b"aaa").hexdigest(), "b": hashlib.sha256(b"bbb").hexdigest()}
            input_root, native = self._package(directory, contents, raw_files={"a.SLDPRT": hashes["a"]})
            self.assertEqual(solidworks_urdf._handoff_native_hashes(input_root), set(native.values()))

    def test_foreign_raw_hash_cannot_widen_allowed_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            input_root, _ = self._package(
                directory,
                {"a.SLDPRT": b"aaa"},
                raw_files={"foreign.SLDPRT": "f" * 64},
            )
            with self.assertRaises(PipelineError):
                solidworks_urdf._handoff_native_hashes(input_root)

    def test_tampered_or_unproven_evidence_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            input_root, _ = self._package(directory, {"a.SLDPRT": b"aaa"})
            (input_root / "input/cad/a.SLDPRT").write_bytes(b"tampered")
            with self.assertRaises(PipelineError):
                solidworks_urdf._handoff_native_hashes(input_root)
        with tempfile.TemporaryDirectory() as directory:
            input_root, _ = self._package(directory, {"a.SLDPRT": b"aaa"}, inventory_digest="0" * 64)
            with self.assertRaises(PipelineError):
                solidworks_urdf._handoff_native_hashes(input_root)

    def test_missing_or_empty_archived_evidence_fails(self):
        with tempfile.TemporaryDirectory() as directory, self.assertRaises((PipelineError, OSError)):
            solidworks_urdf._handoff_native_hashes(Path(directory))
        with tempfile.TemporaryDirectory() as directory:
            input_root, _native = self._package(directory, {})
            with self.assertRaises(PipelineError):
                solidworks_urdf._handoff_native_hashes(input_root)


if __name__ == "__main__":
    unittest.main()
