"""Static contract tests for the self-contained SolidWorks-to-URDF v1 package.

The v1 entry point is one directory: exactly one ``robot.yaml`` plus the CAD
documents it names.  Authors declare no CAD numbers twice - every body frame is
a named native coordinate system, every joint origin is derived from those
frames at build time, and limits/axes carry structured evidence.  These tests
pin that static surface without touching SolidWorks.
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

import yaml

from . import _paths  # noqa: F401  (import side effect: sys.path)

from description_pipeline.io import PipelineError, file_digest, inventory  # noqa: E402
from description_pipeline.sources.solidworks.input import (  # noqa: E402
    INSPECTION_SCHEMA,
    INPUT_SCHEMA,
    ROBOT_FILE,
    inspect_package,
    load_package,
)

SPEC_TEXT = """M3 demo documentation export (fixture)

component base-1: 0.55 kg (vendor drawing)
component upper-1: 0.21 kg (vendor drawing)
component upper-2: 0.09 kg (vendor drawing)
joint joint_1: lower -1.5 rad, upper 1.5 rad, effort 6 N m, velocity 2 rad/s

anchor MASS-base-1
anchor MASS-upper-1
anchor MASS-upper-2
anchor LIMIT-J1
anchor EXCL-upper-2
"""


def base_document(root: Path) -> dict:
    """The smallest package that should load: two datum-bound links, one joint."""

    spec_sha = file_digest(root / "evidence" / "spec.txt")
    return {
        "schema_version": INPUT_SCHEMA,
        "hardware_id": "m3_demo",
        "source": {
            "provider": "solidworks",
            "robot_name": "m3_demo",
            "assembly": "cad/robot.SLDASM",
            "configuration": "Default",
            "bodies": [
                {
                    "id": "base",
                    "name": "base_link",
                    "components": ["base-1"],
                    "frame": {"coordinate_system": "base_datum"},
                },
                {
                    "id": "upper",
                    "name": "upper_link",
                    "components": ["upper-1", "upper-2"],
                    "frame": {"coordinate_system": "tool_datum"},
                },
            ],
            "joints": [
                {
                    "id": "j1",
                    "name": "joint_1",
                    "type": "revolute",
                    "parent": "base_link",
                    "child": "upper_link",
                    "axis": [0.0, 0.0, 1.0],
                    "axis_reference": "HipYaw.SLDPRT cylindrical face, radius 6 mm",
                    "limits": {"lower": -1.5, "upper": 1.5, "effort": 6.0, "velocity": 2.0},
                    "limit_evidence": {
                        "file": "evidence/spec.txt",
                        "sha256": spec_sha,
                        "anchor": "LIMIT-J1",
                    },
                }
            ],
            "frames": [
                {
                    "id": "tool",
                    "name": "tool_frame",
                    "parent": "upper_link",
                    "coordinate_system": "tool_datum",
                }
            ],
            "material_source": "documented_table",
            "mass_evidence": {
                "reference": "M3 demo vendor table (fixture)",
                "file": "evidence/spec.txt",
                "sha256": spec_sha,
                "note": "static fixture, not a released specification",
            },
            "documented_masses": {
                "base-1": {"mass_kg": 0.55, "reason": "vendor drawing", "evidence": "MASS-base-1"},
                "upper-1": {"mass_kg": 0.21, "reason": "vendor drawing", "evidence": "MASS-upper-1"},
                "upper-2": {"mass_kg": 0.09, "reason": "vendor drawing", "evidence": "MASS-upper-2"},
            },
        },
        "checks": {"expected_mass_kg": [0.5, 2.0], "expected_extent_m": [0.05, 1.0]},
    }


class InputPackageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="sw-input-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._serial = 0

    # ------------------------------------------------------------------ helpers

    def _package(
        self,
        mutate=None,
        *,
        raw: str | None = None,
        spec_text: str = SPEC_TEXT,
        extra: tuple[tuple[str, bytes], ...] = (),
        assembly: str = "cad/robot.SLDASM",
    ) -> Path:
        self._serial += 1
        root = self.tmp / f"pack{self._serial}"
        (root / Path(assembly).parent).mkdir(parents=True, exist_ok=True)
        (root / "evidence").mkdir(parents=True, exist_ok=True)
        (root / assembly).write_bytes(b"placeholder SolidWorks assembly\n")
        (root / "evidence" / "spec.txt").write_text(spec_text, encoding="utf-8")
        document = base_document(root)
        if mutate is not None:
            mutate(document)
        text = raw if raw is not None else yaml.safe_dump(document, sort_keys=False, allow_unicode=True)
        (root / ROBOT_FILE).write_text(text, encoding="utf-8")
        for name, blob in extra:
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(blob)
        return root

    def _codes(self, root: Path) -> list[str]:
        inspection = inspect_package(root)
        self.assertFalse(inspection["passed"], "package should not have passed inspection")
        return [item["code"] for item in inspection["errors"]]

    def _assert_code(self, root: Path, code: str) -> dict:
        inspection = inspect_package(root)
        codes = [item["code"] for item in inspection["errors"]]
        self.assertIn(code, codes, inspection["errors"])
        self.assertFalse(inspection["passed"])
        return inspection

    # ----------------------------------------------------------------- positive

    def test_positive_package_is_freeze_ready_and_receipt_is_hashed(self):
        root = self._package()
        inspection = inspect_package(root)
        self.assertTrue(inspection["passed"], inspection["errors"])
        self.assertEqual(inspection["errors"], [])
        self.assertEqual(inspection["schema_version"], INSPECTION_SCHEMA)
        resolved = inspection["resolved"]
        self.assertEqual(resolved["root_link"], "base_link")
        self.assertEqual(resolved["owned_components"], ["base-1", "upper-1", "upper-2"])
        self.assertEqual(resolved["joint_names"], ["joint_1"])
        self.assertEqual(resolved["frame_names"], ["tool_frame"])
        self.assertEqual(resolved["configuration"], "Default")
        # The datum collection is derived from the frames, never authored twice.
        self.assertEqual(resolved["coordinate_systems"], ["base_datum", "tool_datum"])

        loaded = load_package(root)
        self.assertEqual(loaded["schema_version"], INPUT_SCHEMA)
        self.assertEqual(loaded["hardware_id"], "m3_demo")
        self.assertEqual(loaded["checks"]["expected_mass_kg"], [0.5, 2.0])
        self.assertEqual(loaded["checks"]["expected_extent_m"], [0.05, 1.0])

        source = loaded["source"]
        self.assertEqual(source["provider"], "solidworks")
        self.assertEqual(source["robot_name"], "m3_demo")
        self.assertEqual(source["assembly"], str((root / "cad" / "robot.SLDASM").resolve()))
        self.assertEqual(source["allowed_roots"], [str(root.resolve())])
        self.assertEqual(source["document_suffixes"], [".sldasm", ".sldprt"])
        self.assertEqual(source["geometry"], {"enabled": True, "format": "stl_binary"})
        self.assertIs(source["require_saved"], True)
        self.assertEqual(source["material_source"], "documented_table")
        self.assertEqual(source["coordinate_systems"], ["base_datum", "tool_datum"])
        for forbidden in ("worker_url", "allow_remote_worker", "job_id", "evidence_class"):
            self.assertNotIn(forbidden, source)
        joint = source["joints"][0]
        self.assertNotIn("xyz", joint)
        self.assertNotIn("rpy", joint)
        self.assertIn("cylindrical face", joint["axis_reference"])
        self.assertEqual(joint["limit_evidence"]["anchor"], "LIMIT-J1")
        self.assertEqual(source["mass_evidence"]["file"], "evidence/spec.txt")
        self.assertEqual(source["mass_evidence"]["sha256"], file_digest(root / "evidence" / "spec.txt"))
        self.assertEqual(
            source["documented_masses"]["upper-2"],
            {"mass_kg": 0.09, "reason": "vendor drawing", "evidence": "MASS-upper-2"},
        )

        receipt = loaded["input_receipt"]
        self.assertTrue(receipt["inventory_verified"])
        self.assertEqual(receipt["robot_yaml_sha256"], file_digest(root / ROBOT_FILE))
        self.assertEqual(receipt["assembly_sha256"], file_digest(root / "cad" / "robot.SLDASM"))
        self.assertEqual(receipt["file_count"], 3)
        self.assertEqual({entry["path"]: entry["sha256"] for entry in receipt["inventory"]}, inventory(root))
        self.assertEqual(loaded["resolved"]["root_link"], "base_link")

    def test_inspection_does_not_modify_the_package(self):
        root = self._package()
        before = inventory(root)
        inspect_package(root)
        self.assertEqual(inventory(root), before)

    def test_documented_exclusion_is_physically_accounted(self):
        def add(document):
            sha = document["source"]["mass_evidence"]["sha256"]
            document["checks"]["documented_exclusions"] = [
                {
                    "component": "upper-2",
                    "reason": "decorative shell excluded from collision geometry",
                    "evidence": {
                        "file": "evidence/spec.txt",
                        "sha256": sha,
                        "anchor": "EXCL-upper-2",
                    },
                }
            ]

        loaded = load_package(self._package(add))
        exclusion = loaded["source"]["geometry_exclusions"]["upper-2"]
        self.assertEqual(exclusion["reason"], "decorative shell excluded from collision geometry")
        self.assertEqual(exclusion["evidence"]["anchor"], "EXCL-upper-2")

        def typo(document):
            sha = document["source"]["mass_evidence"]["sha256"]
            document["checks"]["documented_exclusions"] = [
                {
                    "component": "ghost-1",
                    "reason": "typo",
                    "evidence": {"file": "evidence/spec.txt", "sha256": sha, "anchor": "EXCL-upper-2"},
                }
            ]

        self._assert_code(self._package(typo), "input.exclusion_invalid")

    # -------------------------------------------------------------- package I/O

    def test_duplicate_yaml_keys_are_rejected(self):
        root = self._package(
            raw=(
                'schema_version: "solidworks-to-urdf.input/v1"\n'
                "hardware_id: m3_demo\n"
                "hardware_id: m3_other\n"
                "source: {}\n"
                "checks: {}\n"
            )
        )
        self._assert_code(root, "input.yaml_invalid")

    def test_missing_or_multiple_config_files_are_rejected(self):
        root = self._package()
        (root / ROBOT_FILE).unlink()
        self._assert_code(root, "input.robot_missing")

        other = self._package(extra=(("cad/notes.yaml", b"note: 1\n"),))
        self._assert_code(other, "input.multiple_config_files")

    def test_package_symlink_and_inner_symlink_are_rejected(self):
        root = self._package()
        alias = self.tmp / "alias"
        alias.symlink_to(root, target_is_directory=True)
        self._assert_code(alias, "input.package_symlink")

        (root / "cad" / "alias.SLDASM").symlink_to(root / "cad" / "robot.SLDASM")
        self._assert_code(root, "input.inventory_invalid")

    def test_case_colliding_paths_are_rejected(self):
        root = self._package(extra=(("cad/ROBOT.SLDASM", b"other bytes\n"),))
        self._assert_code(root, "input.inventory_invalid")

    def test_missing_package_and_non_mapping_document(self):
        self._assert_code(self.tmp / "nowhere", "input.package_missing")
        root = self._package(raw="- not\n- a mapping\n")
        self.assertIn("input.top_invalid", self._codes(root))

    # ------------------------------------------------------------ top-level API

    def test_top_level_surface_is_exact(self):
        def schema(document):
            document["schema_version"] = "solidworks-to-urdf.input/v2"

        self._assert_code(self._package(schema), "input.schema_invalid")

        def unknown(document):
            document["extra"] = True

        self._assert_code(self._package(unknown), "input.top_keys")

        def no_source(document):
            document["source"] = []

        self._assert_code(self._package(no_source), "input.source_invalid")

        def bad_hardware_id(document):
            document["hardware_id"] = "m3 演示"

        self._assert_code(self._package(bad_hardware_id), "input.hardware_id_invalid")

    def test_worker_and_evidence_class_downgrade_keys_are_rejected(self):
        def mutate(document):
            document["source"]["worker_url"] = "http://10.0.0.177:8731"
            document["source"]["evidence_class"] = "fixture"
            document["source"]["worker_poll_seconds"] = 1.0

        codes = self._codes(self._package(mutate))
        self.assertIn("input.source_forbidden_key", codes)
        self.assertIn("input.source_keys", codes)

    def test_provider_and_robot_name_are_explicit(self):
        def provider(document):
            document["source"]["provider"] = "cad"

        self._assert_code(self._package(provider), "input.provider_invalid")

        def robot_name(document):
            document["source"]["robot_name"] = "M3-Demo"

        self._assert_code(self._package(robot_name), "input.robot_name_invalid")

    def test_assembly_must_stay_inside_the_package(self):
        for value in ("/abs/robot.SLDASM", "../robot.SLDASM", "cad/robot.txt"):
            with self.subTest(value=value):
                def mutate(document, value=value):
                    document["source"]["assembly"] = value

                self._assert_code(self._package(mutate), "input.assembly_invalid")

    # ------------------------------------------------------------------- bodies

    def test_bodies_and_joints_must_be_non_empty(self):
        def mutate(document):
            document["source"]["bodies"] = []
            document["source"]["joints"] = []

        codes = self._codes(self._package(mutate))
        self.assertIn("input.bodies_invalid", codes)
        self.assertIn("input.joints_invalid", codes)

    def test_names_must_be_explicit_snake_case_and_unique(self):
        def not_snake(document):
            document["source"]["bodies"][1]["name"] = "UpperLink"

        self._assert_code(self._package(not_snake), "input.body_invalid")

        def duplicated(document):
            copy = dict(document["source"]["bodies"][0])
            copy["id"] = "base_again"
            document["source"]["bodies"].append(copy)

        self._assert_code(self._package(duplicated), "input.body_duplicate")

    def test_components_have_one_owner(self):
        def shared(document):
            document["source"]["bodies"][1]["components"].append("base-1")

        self._assert_code(self._package(shared), "input.component_duplicate_owner")

        def empty(document):
            document["source"]["bodies"][0]["components"] = []

        self._assert_code(self._package(empty), "input.body_components_invalid")

    # ------------------------------------------------------------ CAD datums

    def test_authored_coordinate_systems_are_rejected(self):
        def mutate(document):
            document["source"]["coordinate_systems"] = ["base_datum", "tool_datum"]

        codes = self._codes(self._package(mutate))
        self.assertIn("input.source_forbidden_key", codes)

    def test_body_frames_bind_named_datums_only(self):
        def authored_numbers(document):
            document["source"]["bodies"][1]["frame"] = {
                "coordinate_system": "tool_datum",
                "xyz": [0.0, 0.0, 0.12],
                "rpy": [0.0, 0.0, 0.0],
            }

        self._assert_code(self._package(authored_numbers), "input.body_frame_authored_numbers")

        def empty(document):
            document["source"]["bodies"][1]["frame"] = {}

        self._assert_code(self._package(empty), "input.body_frame_missing")

        def blank_datum(document):
            document["source"]["bodies"][1]["frame"] = {"coordinate_system": ""}

        self._assert_code(self._package(blank_datum), "input.frame_reference_unknown")

    def test_datum_collection_is_derived_and_case_unambiguous(self):
        def no_datums(document):
            document["source"]["bodies"][0]["frame"] = {"xyz": [0.0, 0.0, 0.0], "rpy": [0.0, 0.0, 0.0]}
            document["source"]["bodies"][1]["frame"] = {"xyz": [0.0, 0.0, 0.1], "rpy": [0.0, 0.0, 0.0]}
            document["source"]["frames"] = []

        codes = self._codes(self._package(no_datums))
        self.assertIn("input.body_frame_authored_numbers", codes)
        self.assertIn("input.coordinate_systems_empty", codes)

        def ambiguous(document):
            document["source"]["bodies"][1]["frame"] = {"coordinate_system": "BASE_DATUM"}

        self._assert_code(self._package(ambiguous), "input.coordinate_systems_ambiguous")

    def test_link_frames_bind_named_datums_only(self):
        def blank_reference(document):
            document["source"]["frames"][0]["coordinate_system"] = ""

        self._assert_code(self._package(blank_reference), "input.frame_reference_unknown")

        def authored_numbers(document):
            document["source"]["frames"][0] = {
                "id": "tool",
                "name": "tool_frame",
                "parent": "upper_link",
                "xyz": [0.0, 0.0, 0.1],
                "rpy": [0.0, 0.0, 0.0],
            }

        self._assert_code(self._package(authored_numbers), "input.frame_authored_numbers")

        def unknown_parent(document):
            document["source"]["frames"][0]["parent"] = "ghost_link"

        self._assert_code(self._package(unknown_parent), "input.frame_parent_unknown")

    # ------------------------------------------------------------------- joints

    def test_joints_carry_no_authored_origin_numbers(self):
        def mutate(document):
            joint = document["source"]["joints"][0]
            joint["xyz"] = [0.0, 0.0, 0.05]
            joint["rpy"] = [0.0, 0.0, 0.0]

        inspection = self._assert_code(self._package(mutate), "input.joint_invalid")
        unknown = inspection["errors"][0]["detail"]["unknown"]
        self.assertEqual(unknown, ["rpy", "xyz"])

    def test_joints_need_an_axis_reference(self):
        def mutate(document):
            document["source"]["joints"][0].pop("axis_reference")

        self._assert_code(self._package(mutate), "input.joint_axis_reference_invalid")

    def test_joint_axis_and_limits_are_strict_si(self):
        def axis(document):
            document["source"]["joints"][0]["axis"] = [0.0, 0.0, 2.0]

        self._assert_code(self._package(axis), "input.joint_axis_invalid")

        def effort(document):
            document["source"]["joints"][0]["limits"]["effort"] = 0.0

        self._assert_code(self._package(effort), "input.joint_limits_invalid")

        def infinite(document):
            document["source"]["joints"][0]["limits"]["velocity"] = float("inf")

        self._assert_code(self._package(infinite), "input.joint_limits_invalid")

        def reversed_bounds(document):
            document["source"]["joints"][0]["limits"]["lower"] = 2.0

        self._assert_code(self._package(reversed_bounds), "input.joint_limits_invalid")

        def continuous_with_bounds(document):
            document["source"]["joints"][0]["type"] = "continuous"

        self._assert_code(self._package(continuous_with_bounds), "input.joint_limits_invalid")

        def evidence_inside_limits(document):
            joint = document["source"]["joints"][0]
            joint["limits"]["evidence"] = joint.pop("limit_evidence")

        self._assert_code(self._package(evidence_inside_limits), "input.joint_limits_invalid")

    def test_fixed_joints_reject_axis_and_limit_payloads(self):
        def mutate(document):
            document["source"]["joints"][0]["type"] = "fixed"

        codes = self._codes(self._package(mutate))
        self.assertIn("input.joint_invalid", codes)

    def test_joint_endpoints_and_types_are_explicit(self):
        def unknown_type(document):
            document["source"]["joints"][0]["type"] = "screw"

        self._assert_code(self._package(unknown_type), "input.joint_type_unsupported")

        def self_loop(document):
            document["source"]["joints"][0]["parent"] = "upper_link"

        self.assertIn("input.joint_cycle", self._codes(self._package(self_loop)))

        def bad_parent(document):
            document["source"]["joints"][0]["parent"] = "ghost_link"

        self.assertIn("input.joint_parent_unknown", self._codes(self._package(bad_parent)))

        def bad_child(document):
            document["source"]["joints"][0]["child"] = "ghost_link"

        self.assertIn("input.joint_child_unknown", self._codes(self._package(bad_child)))

    def test_joint_tree_must_be_one_connected_base_link_tree(self):
        def disconnected(document):
            document["source"]["bodies"].append(
                {
                    "id": "spare",
                    "name": "spare_link",
                    "components": ["spare-1"],
                    "frame": {"coordinate_system": "spare_datum"},
                }
            )
            document["source"]["documented_masses"]["spare-1"] = {
                "mass_kg": 0.05,
                "reason": "vendor drawing",
                "evidence": "MASS-spare-1",
            }

        spec = SPEC_TEXT + "anchor MASS-spare-1\n"
        self._assert_code(self._package(disconnected, spec_text=spec), "input.tree_disconnected")

        def not_rooted(document):
            document["source"]["joints"][0]["child"] = "base_link"

        codes = self._codes(self._package(not_rooted))
        self.assertIn("input.joint_cycle", codes)
        self.assertIn("input.tree_root_invalid", codes)

        def renamed_root(document):
            document["source"]["bodies"][0]["name"] = "chassis"

        self.assertIn("input.root_missing", self._codes(self._package(renamed_root)))

    # ----------------------------------------------------------------- evidence

    def test_limit_evidence_must_bind_package_bytes_and_anchor(self):
        def missing(document):
            document["source"]["joints"][0].pop("limit_evidence")

        self._assert_code(self._package(missing), "input.limit_evidence_invalid")

        def bad_hash(document):
            document["source"]["joints"][0]["limit_evidence"]["sha256"] = "0" * 64

        self._assert_code(self._package(bad_hash), "input.limit_evidence_invalid")

        def bad_anchor(document):
            document["source"]["joints"][0]["limit_evidence"]["anchor"] = "LIMIT-J1-MISSING"

        self._assert_code(self._package(bad_anchor), "input.limit_evidence_invalid")

        def outside(document):
            document["source"]["joints"][0]["limit_evidence"]["file"] = "../spec.txt"

        self._assert_code(self._package(outside), "input.limit_evidence_invalid")

    def test_shared_mass_evidence_uses_the_freeze_shape_without_anchor(self):
        def anchored(document):
            document["source"]["mass_evidence"]["anchor"] = "MASS-base-1"

        self._assert_code(self._package(anchored), "input.mass_evidence_invalid")

        def no_reference(document):
            document["source"]["mass_evidence"].pop("reference")

        self._assert_code(self._package(no_reference), "input.mass_evidence_invalid")

        def bad_hash(document):
            document["source"]["mass_evidence"]["sha256"] = "f" * 64

        self._assert_code(self._package(bad_hash), "input.mass_evidence_invalid")

        def not_in_inventory(document):
            document["source"]["mass_evidence"]["file"] = "evidence/other.txt"

        self._assert_code(self._package(not_in_inventory), "input.mass_evidence_invalid")

    def test_documented_mass_anchor_must_exist_in_the_evidence_file(self):
        def mutate(document):
            document["source"]["documented_masses"]["upper-1"]["evidence"] = "MASS-upper-1-MISSING"

        self._assert_code(self._package(mutate), "input.documented_mass_invalid")

    # ------------------------------------------------------------ mass contract

    def test_documented_table_must_cover_every_owned_component(self):
        def incomplete(document):
            document["source"]["documented_masses"].pop("upper-2")

        self._assert_code(self._package(incomplete), "input.documented_masses_incomplete")

        def unknown_component(document):
            document["source"]["documented_masses"]["ghost-1"] = {
                "mass_kg": 0.01,
                "reason": "typo",
                "evidence": "MASS-upper-2",
            }

        self._assert_code(self._package(unknown_component), "input.documented_mass_owner_unknown")

        def conflict(document):
            document["source"]["material_source"] = "cad"

        self._assert_code(self._package(conflict), "input.documented_masses_conflict")

        def required(document):
            document["source"]["documented_masses"] = {}

        self._assert_code(self._package(required), "input.documented_masses_required")

        def bad_source(document):
            document["source"]["material_source"] = "mixed"

        self._assert_code(self._package(bad_source), "input.material_source_invalid")

    # ------------------------------------------------------------------- checks

    def test_checks_ranges_are_explicit_positive_and_ordered(self):
        def reversed_range(document):
            document["checks"]["expected_mass_kg"] = [2.0, 0.5]

        self._assert_code(self._package(reversed_range), "input.checks_invalid")

        def negative(document):
            document["checks"]["expected_extent_m"] = [-1.0, 1.0]

        self._assert_code(self._package(negative), "input.checks_invalid")

        def missing(document):
            document.pop("checks")

        self._assert_code(self._package(missing), "input.checks_invalid")

        def unknown(document):
            document["checks"]["expected_mass"] = [0.5, 1.0]

        self._assert_code(self._package(unknown), "input.checks_invalid")

    # --------------------------------------------------------------------- load

    def test_load_package_raises_pipeline_error_with_findings(self):
        def mutate(document):
            document["source"]["provider"] = "onshape"

        root = self._package(mutate)
        with self.assertRaises(PipelineError) as caught:
            load_package(root)
        error = caught.exception
        findings = getattr(error, "findings", [])
        self.assertIn("input.provider_invalid", [item["code"] for item in findings])
        inspection = getattr(error, "inspection", None)
        self.assertIsInstance(inspection, dict)
        self.assertFalse(inspection["passed"])
        self.assertIn("input.provider_invalid", str(error))


if __name__ == "__main__":
    unittest.main()
