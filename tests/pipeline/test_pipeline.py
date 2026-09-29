from __future__ import annotations

import copy
import errno
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree as ET

from description_pipeline.build import assess, build, failure_record, freeze, lock_toolchain
from description_pipeline.io import PipelineError, digest, read_data, write_json
from description_pipeline.model import Robot
from description_pipeline.model.control import map_actions, read_observations
from description_pipeline.repository import compare_models, git, init_model
from description_pipeline.sources.snapshot import verify_snapshot, write_manifest
from description_pipeline.verification import inspect
from tests.pipeline import ledger as joint_ledger


def scene() -> dict:
    geometry = {"kind": "box", "size": [0.1, 0.08, 0.06], "xyz": [0, 0, 0], "rpy": [0, 0, 0]}
    links = []
    for name in ("base_link", "arm_link", "slider_link"):
        links.append(
            {
                "id": name,
                "name": name,
                "inertial": {
                    "mass": 1.0,
                    "xyz": [0, 0, 0],
                    "rpy": [0.2, 0.3, 0.4],
                    "inertia": [0.002, 0, 0, 0.003, 0, 0.004],
                },
                "visuals": [copy.deepcopy(geometry)],
                "collisions": [copy.deepcopy(geometry)],
                "provenance": {"source_entities": [name]},
            }
        )
    joints = []
    for name, parent, child, kind in (
        ("arm_joint", "base_link", "arm_link", "revolute"),
        ("slider_joint", "arm_link", "slider_link", "prismatic"),
    ):
        joints.append(
            {
                "id": name,
                "name": name,
                "type": kind,
                "parent": parent,
                "child": child,
                "xyz": [0, 0, 0.3],
                "rpy": [0.1, 0.2, 0.3],
                "axis": [0, 0, 1],
                "limits": {"lower": -0.1, "upper": 0.2, "effort": 2, "velocity": 3},
                "dynamics": {"damping": 0.01, "friction": 0},
                "provenance": {"reference": "analytic_fixture"},
            }
        )
    return {
        "schema_version": "description.scene/v1",
        "name": "robot",
        "units": "SI",
        "links": links,
        "joints": joints,
        "frames": [
            {
                "id": "imu",
                "name": "imu",
                "parent": "arm_link",
                "xyz": [0.01, 0.02, 0.03],
                "rpy": [0.2, 0.1, 0.4],
                "provenance": {},
            }
        ],
        "actuators": [],
        "sensors": [],
        "constraints": [],
        "control": {},
        "contact_excludes": [],
        "provenance": {"expected_entities": [item["name"] for item in links], "fixture": True},
    }


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        base = Path(self.temporary.name)
        self.source = base / "source"
        self.source.mkdir()
        write_json(self.source / "scene.json", scene())
        write_manifest(
            self.source, kind="fixture", identity={"case": "offset-tensor-hinge-slider"}, evidence_class="fixture"
        )
        self.root = base / "model"
        init_model(self.root, "fixture", {"provider": "fixture", "path": str(self.source)})
        # The ledger is a delivery file: the build refuses a movable joint that is not registered,
        # and every workspace that builds the fixture has to declare them in design order.
        joint_ledger.write(self.root, "arm_joint", "slider_joint")
        freeze(self.root)
        lock_toolchain(self.root)

    def successful_build(self):
        report = build(self.root, "kinematics")
        self.assertTrue(report["passed"], report["blockers"] + [report.get("diagnostic_path", "")])
        return report

    def commit_candidate(self, message: str = "first candidate") -> str:
        """Commit the built candidate in place and return its SHA; ``diff`` resolves revisions in Git."""

        if not (self.root / ".git").exists():
            git(self.root, "init", "-b", "feature/fixture")
            git(self.root, "config", "user.name", "Fixture")
            git(self.root, "config", "user.email", "fixture@example.invalid")
        git(self.root, "add", ".")
        git(self.root, "commit", "-m", message)
        return git(self.root, "rev-parse", "HEAD").stdout.strip()

    def test_complete_build_and_independent_check(self):
        report = self.successful_build()
        self.assertTrue(assess(self.root, "kinematics")["passed"])
        checks = {item["id"]: item for item in report["checks"]}
        self.assertEqual(checks["consumer.joints"]["expected"], ["arm_joint", "slider_joint"])
        self.assertFalse(checks["consumer.kinematics"]["missing"])
        self.assertEqual(checks["physics.uniform_density_oracle"]["status"], "not_applicable")

    def test_declared_uniform_density_must_be_verified_or_block_publication(self):
        for variant in ("valid", "wrong_tensor", "overlap_unspecified"):
            with self.subTest(variant=variant):
                source = scene()
                link = source["links"][0]
                link["provenance"]["inertia_model"] = "uniform_density_visual"
                link["inertial"]["rpy"] = [0, 0, 0]
                link["inertial"]["inertia"] = [
                    (0.08**2 + 0.06**2) / 12,
                    0,
                    0,
                    (0.1**2 + 0.06**2) / 12,
                    0,
                    (0.1**2 + 0.08**2) / 12,
                ]
                if variant == "wrong_tensor":
                    link["inertial"]["inertia"][0] *= 1.1
                if variant == "overlap_unspecified":
                    link["visuals"].append(copy.deepcopy(link["visuals"][0]))
                write_json(self.source / "scene.json", source)
                write_manifest(self.source, kind="fixture", identity={"case": variant}, evidence_class="fixture")
                freeze(self.root)
                report = build(self.root, "kinematics")
                check = next(c for c in report["checks"] if c["id"] == "physics.uniform_density_oracle")
                self.assertEqual(check["status"], "passed" if variant == "valid" else "failed")
                self.assertEqual(report["passed"], variant == "valid")
                if variant == "overlap_unspecified":
                    self.assertEqual(check["missing"], ["base_link"])
                    self.assertEqual(check["details"]["coverage"]["not_run"], ["base_link"])

    def test_commit_to_workspace_diff_includes_author_changes(self):
        self.successful_build()
        sha = self.commit_candidate()
        definition = read_data(self.root / "config/robot.yaml")
        definition["hardware_id"] = "revised-fixture"
        write_json(self.root / "config/robot.yaml", definition)
        self.successful_build()
        diff = compare_models(self.root, Path(sha), self.root)
        self.assertEqual(diff["references"][0]["commit"], sha)
        self.assertNotEqual(diff["before"], diff["after"])
        self.assertEqual(diff["changes"]["config/robot.yaml"][0]["path"], "/hardware_id")
        self.assertEqual(
            diff["summary"],
            {
                "changed": True,
                "changed_areas": ["config/robot.yaml"],
                "robot_objects": {"added": 0, "removed": 0, "modified": 0},
                "subject_changed": True,
            },
        )

    def test_diff_summary_counts_objects_and_keeps_the_two_kinds_of_change_apart(self):
        """A review reads the summary first: which objects moved, and whether the delivery moved.

        The five object categories are always present in ``changes``; the summary has to say
        "nothing changed" for two identical deliveries anyway.  A quality attribute is the other
        way round: it is compared without being part of the delivery digest.
        """

        self.successful_build()
        sha = self.commit_candidate()
        unchanged = compare_models(self.root, self.root, self.root)
        self.assertFalse(unchanged["summary"]["changed"])
        self.assertEqual(unchanged["summary"]["changed_areas"], [])
        self.assertFalse(unchanged["summary"]["subject_changed"])
        source = scene()
        source["joints"][0]["dynamics"]["damping"] = 0.02
        write_json(self.source / "scene.json", source)
        write_manifest(
            self.source, kind="fixture", identity={"case": "offset-tensor-hinge-slider"}, evidence_class="fixture"
        )
        freeze(self.root)
        self.successful_build()
        revision = self.commit_candidate("damped")
        diff = compare_models(self.root, Path(sha), self.root)
        self.assertEqual(diff["summary"]["robot_objects"], {"added": 0, "removed": 0, "modified": 1})
        self.assertEqual(diff["summary"]["changed_areas"], ["joints", "sources/source.lock.json"])
        self.assertTrue(diff["summary"]["subject_changed"])
        quality = read_data(self.root / "docs/quality.json")
        committed_quality = copy.deepcopy(quality)
        quality["profile"] = "hand-edited by this test"
        write_json(self.root / "docs/quality.json", quality)
        comparison_only = compare_models(self.root, Path(revision), self.root)
        self.assertEqual(comparison_only["summary"]["changed_areas"], ["profile"])
        self.assertFalse(comparison_only["summary"]["subject_changed"])
        write_json(self.root / "docs/quality.json", committed_quality)
        # Author evidence is delivered without being compared field by field, so the delivery
        # digest moves while the report stays empty: "nothing in the compared areas" is not the
        # same claim as "the two deliveries are identical".
        (self.root / "docs/notes.md").write_text("# notes\n", encoding="utf-8")
        delivery_only = compare_models(self.root, Path(revision), self.root)
        self.assertFalse(delivery_only["summary"]["changed"])
        self.assertEqual(delivery_only["summary"]["changed_areas"], [])
        self.assertTrue(delivery_only["summary"]["subject_changed"])

    def test_tool_lock_cannot_relabel_identical_code_as_another_source_commit(self):
        path = self.root / "config/toolchain.lock.json"
        locked = read_data(path)
        locked["source_commit"] = "f" * 40
        write_json(path, locked)
        with self.assertRaisesRegex(PipelineError, "source_commit"):
            build(self.root, "kinematics")

    def test_same_frozen_input_has_same_artifact_identity(self):
        first = self.successful_build()["subject"]
        second = self.successful_build()["subject"]
        self.assertEqual(first, second)

    def test_cache_reuse_and_original_evidence_scope_are_reported(self):
        first = self.successful_build()
        second = self.successful_build()
        self.assertEqual(first["execution"]["generation"], "executed")
        self.assertEqual(second["execution"]["generation"], "cache_reuse")
        self.assertEqual(second["execution"]["verification"], "executed")
        self.assertFalse(second["evidence_scope"]["live_cad_connection_tested"])
        self.assertEqual(second["evidence_scope"]["source_evidence_class"], "fixture")

    def test_a_failed_capture_keeps_the_reason_in_its_package(self):
        """What a user opens to understand a failed capture has to name the code and the document."""

        from description_pipeline.sources.solidworks.errors import CadError

        refusal = CadError(
            "document_not_open",
            "requested document is not open in the CAD session the adapter owns; "
            "the adapter reads the revision from disk in a session of its own and never uses yours",
            {"path": "D:/models/robot.SLDASM"},
        )
        adapter = unittest.mock.Mock()
        adapter.freeze.side_effect = refusal
        with (
            patch("description_pipeline.build.definition", return_value={"source": {"provider": "solidworks"}}),
            patch("description_pipeline.build.importlib.import_module", return_value=adapter),
            self.assertRaises(CadError) as raised,
        ):
            freeze(self.root)
        package = Path(vars(raised.exception)["diagnostic_path"])
        self.assertEqual(
            read_data(package / "failure.json"),
            {
                "error": "CadError",
                "message": str(refusal),
                "code": "document_not_open",
                "detail": {"path": "D:/models/robot.SLDASM"},
            },
        )

    def test_a_failure_without_a_bridge_code_keeps_the_two_plain_fields(self):
        self.assertEqual(
            failure_record(PipelineError("no code here")),
            {"error": "PipelineError", "message": "no code here"},
        )

    def test_a_snapshot_made_elsewhere_keeps_its_identity_when_it_is_frozen_again(self):
        """The capture runs where SolidWorks is and the build runs where the model is.

        A snapshot's identity is the digest of its manifest, so importing one and freezing it again
        has to reproduce that digest exactly: the lock, the cache directory and every published
        model subject are named after it.  This is the path a Windows capture takes on its way to a
        Linux build, exercised here with a snapshot of the same shape (an assembly, `cad` evidence).
        """

        captured = Path(self.temporary.name) / "captured"
        captured.mkdir()
        write_json(captured / "scene.json", scene())
        write_manifest(
            captured,
            kind="solidworks",
            identity={"assembly": "D:/models/robot.SLDASM", "configuration": "Default"},
            evidence_class="cad",
        )
        self.assertEqual(digest(verify_snapshot(captured)), digest(read_data(captured / "manifest.json")))

        imported = Path(self.temporary.name) / "imported-model"
        init_model(imported, "carried-over", {"provider": "snapshot", "path": str(captured)})
        lock = freeze(imported)
        self.assertEqual(lock["manifest_digest"], digest(verify_snapshot(captured)))
        self.assertEqual(lock["provider"], "solidworks")
        self.assertEqual(lock["evidence_class"], "cad")
        self.assertTrue((imported / lock["snapshot"]).is_dir())

        # The control: one changed byte in the captured scene has to move the identity, or the
        # comparison above would hold for anything.
        edited = Path(self.temporary.name) / "edited"
        shutil.copytree(captured, edited)
        altered = read_data(edited / "scene.json")
        altered["links"][0]["inertial"]["mass"] = 1.5
        write_json(edited / "scene.json", altered)
        write_manifest(
            edited,
            kind="solidworks",
            identity={"assembly": "D:/models/robot.SLDASM", "configuration": "Default"},
            evidence_class="cad",
        )
        self.assertNotEqual(digest(verify_snapshot(edited)), lock["manifest_digest"])

    def test_overlapping_author_definitions_fail_before_generation(self):
        config = read_data(self.root / "config/robot.yaml")
        evidence = self.root / "docs/measurement.md"
        evidence.write_text("fixture measurement")
        override = {
            "kind": "links",
            "id": "base_link",
            "field": "inertial.mass",
            "value": 1.2,
            "reason": "fixture",
            "evidence": "docs/measurement.md",
        }
        config["overrides"] = [override, {**override, "value": 1.3}]
        write_json(self.root / "config/robot.yaml", config)
        with self.assertRaisesRegex(PipelineError, "one effective definition"):
            build(self.root, "kinematics")
        diagnostics = list((self.root / "build/failed").glob("*/failure.json"))
        self.assertTrue(diagnostics)
        self.assertLessEqual(len(diagnostics[0].parent.name), 32)
        self.assertFalse((self.root / "manifest.json").exists())

    def test_generation_exception_preserves_diagnostics_and_previous_delivery(self):
        self.successful_build()
        previous = (self.root / "manifest.json").read_bytes()
        # Force a genuine generation rather than a cache hit.
        with (
            patch("description_pipeline.build.cache.restore", return_value=False),
            patch("description_pipeline.build.generate", side_effect=PipelineError("geometry interrupted")),
            self.assertRaisesRegex(PipelineError, "geometry interrupted"),
        ):
            build(self.root, "kinematics")
        self.assertEqual(previous, (self.root / "manifest.json").read_bytes())
        diagnostics = list((self.root / "build/failed").glob("*/failure.json"))
        self.assertIn("geometry interrupted", diagnostics[0].read_text(encoding="utf-8"))

    def test_input_edit_during_build_does_not_replace_previous_delivery(self):
        from description_pipeline.build import assess as real_assess

        self.successful_build()
        previous = (self.root / "manifest.json").read_bytes()

        def changing_assess(*args, **kwargs):
            report = real_assess(*args, **kwargs)
            config = read_data(self.root / "config/robot.yaml")
            config["hardware_id"] = "edited-during-build"
            write_json(self.root / "config/robot.yaml", config)
            return report

        with (
            patch("description_pipeline.build.assess", side_effect=changing_assess),
            self.assertRaisesRegex(PipelineError, "inputs changed during build"),
        ):
            build(self.root, "kinematics")
        self.assertEqual(previous, (self.root / "manifest.json").read_bytes())

    def test_external_output_failure_preserves_diagnostics_across_filesystems(self):
        real_rename = os.rename

        def cross_device(source, target, *args, **kwargs):
            if Path(source).is_dir():
                raise OSError(errno.EXDEV, "Cross-device link")
            return real_rename(source, target, *args, **kwargs)

        with (
            patch("description_pipeline.build.generate", side_effect=PipelineError("external output interrupted")),
            patch("os.rename", side_effect=cross_device),
            self.assertRaisesRegex(PipelineError, "external output interrupted") as raised,
        ):
            build(self.root, "kinematics", self.root.parent / "output")
        diagnostic = Path(vars(raised.exception)["diagnostic_path"])
        self.assertTrue((diagnostic / "failure.json").is_file())
        self.assertFalse((self.root.parent / "output").exists())

    def test_effective_dynamics_detects_unexplained_rotor_inertia(self):
        report = self.successful_build()
        self.assertIn("consumer.dynamics", {item["id"] for item in report["checks"]})
        path = self.root / "mjcf/robot.xml"
        xml = ET.parse(path)
        joint = xml.find(".//joint")
        assert joint is not None
        joint.set("armature", "0.1")
        xml.write(path)
        self.assertIn("consumer.dynamics", assess(self.root, "kinematics")["blockers"])

    def test_scene_cannot_override_verified_robot_physics(self):
        report = self.successful_build()
        path = self.root / "mjcf/scene.xml"
        xml = ET.parse(path)
        defaults = ET.SubElement(xml.getroot(), "default")
        ET.SubElement(defaults, "joint", armature="0.1")
        xml.write(path)
        checks = {item["id"]: item for item in inspect(self.root, report["profile"])}
        self.assertEqual(checks["consumer.compile"]["status"], "passed")
        self.assertEqual(checks["consumer.dynamics"]["status"], "passed")
        self.assertEqual(checks["consumer.scene_contract"]["status"], "failed")

    def test_matching_frame_errors_in_both_formats_still_disagree_with_definition(self):
        report = self.successful_build()
        urdf_path = self.root / "urdf/robot.urdf"
        urdf = ET.parse(urdf_path)
        attachment = urdf.find("joint[@name='imu_fixed']/origin")
        assert attachment is not None
        attachment.set("xyz", "0.11 0.02 0.03")
        urdf.write(urdf_path)
        mjcf_path = self.root / "mjcf/robot.xml"
        mjcf = ET.parse(mjcf_path)
        site = mjcf.find(".//site[@name='imu']")
        assert site is not None
        site.set("pos", "0.11 0.02 0.03")
        mjcf.write(mjcf_path)
        checks = {item["id"]: item for item in inspect(self.root, report["profile"])}
        self.assertEqual(checks["consumer.kinematics"]["status"], "passed")
        self.assertEqual(checks["urdf.frames"]["status"], "failed")

    def test_consumer_cannot_add_limits_to_continuous_joint(self):
        data = scene()
        joint = data["joints"][0]
        joint["type"] = "continuous"
        joint["limits"] = {"effort": 2, "velocity": 3}
        write_json(self.source / "scene.json", data)
        write_manifest(self.source, kind="fixture", identity={"case": "continuous"}, evidence_class="fixture")
        freeze(self.root)
        report = self.successful_build()
        path = self.root / "mjcf/robot.xml"
        xml = ET.parse(path)
        moving = xml.find(".//joint[@name='arm_joint']")
        assert moving is not None
        moving.set("limited", "true")
        moving.set("range", "-1 1")
        xml.write(path)
        checks = {item["id"]: item for item in inspect(self.root, report["profile"])}
        self.assertEqual(checks["consumer.joints"]["status"], "failed")

    def test_declared_mimic_cannot_be_inactive_in_consumer(self):
        data = scene()
        data["joints"][1]["mimic"] = {"joint": "arm_joint", "multiplier": 0.5, "offset": 0}
        write_json(self.source / "scene.json", data)
        write_manifest(self.source, kind="fixture", identity={"case": "mimic"}, evidence_class="fixture")
        freeze(self.root)
        report = self.successful_build()
        path = self.root / "mjcf/robot.xml"
        xml = ET.parse(path)
        equality = xml.find("equality/joint")
        assert equality is not None
        equality.set("active", "false")
        xml.write(path)
        checks = {item["id"]: item for item in inspect(self.root, report["profile"])}
        self.assertEqual(checks["consumer.interfaces"]["status"], "failed")

    def test_floating_base_full_mass_matrix_and_gravity_are_verified(self):
        profile_path = self.root / "config/profiles/kinematics.json"
        profile = read_data(profile_path)
        profile["root_mode"] = "floating"
        write_json(profile_path, profile)
        report = self.successful_build()
        check = next(item for item in report["checks"] if item["id"] == "consumer.kinematics")
        poses = check["details"]["base_poses"]
        self.assertGreater(len(poses), 1)
        self.assertNotEqual(poses[0], poses[-1])

    def test_contact_coefficients_are_explicit_and_checked_after_loading(self):
        profile_path = self.root / "config/profiles/kinematics.json"
        profile = read_data(profile_path)
        profile["contact"] = {
            "friction": [0.7, 0.004, 0.0002],
            "condim": 3,
            "solref": [0.03, 1],
            "solimp": [0.85, 0.95, 0.001, 0.5, 2],
            "margin": 0,
            "gap": 0,
        }
        write_json(profile_path, profile)
        self.successful_build()
        path = self.root / "mjcf/robot.xml"
        xml = ET.parse(path)
        node = next(node for node in xml.findall(".//geom") if node.get("contype") == "1")
        node.set("friction", "0.9 0.004 0.0002")
        xml.write(path)
        self.assertIn("consumer.contact_parameters", assess(self.root, "kinematics")["blockers"])

    def test_simulation_requires_declared_contact_and_task_acceptance(self):
        report = build(self.root, "simulation")
        self.assertFalse(report["passed"])
        self.assertIn("consumer.contact_parameters", report["blockers"])
        self.assertIn("consumer.application", report["blockers"])

    def test_control_order_polarity_units_and_offsets_reach_the_real_consumer(self):
        import mujoco

        data = scene()
        data["actuators"] = [
            {
                "id": name,
                "name": name,
                "joint": joint,
                "type": "motor",
                "gear": 1,
                "control_range": [-2, 2],
                "provenance": {},
            }
            for name, joint in (("arm", "arm_joint"), ("slide", "slider_joint"))
        ]
        evidence = "docs/calibration.md"
        (self.root / evidence).write_text("Synthetic calibration for an interface regression test.")
        data["control"] = {
            "action_order": ["slide", "arm"],
            "actions": {
                "arm": {"unit": "N*m", "polarity": -1, "offset": 0.1, "evidence": evidence},
                "slide": {"unit": "N", "polarity": 1, "offset": 0.2, "evidence": evidence},
            },
            "observation_order": ["slide_velocity", "arm_position"],
            "observations": {
                "arm_position": {
                    "source": "joint_position",
                    "target": "arm_joint",
                    "component": 0,
                    "unit": "rad",
                    "polarity": -1,
                    "offset": 0.01,
                    "evidence": evidence,
                },
                "slide_velocity": {
                    "source": "joint_velocity",
                    "target": "slider_joint",
                    "component": 0,
                    "unit": "m/s",
                    "polarity": 1,
                    "offset": 0.03,
                    "evidence": evidence,
                },
            },
        }
        write_json(self.source / "scene.json", data)
        write_manifest(self.source, kind="fixture", identity={"case": "calibrated-controls"}, evidence_class="fixture")
        freeze(self.root)
        self.successful_build()
        commands = map_actions(data, [0.5, 0.7])
        self.assertAlmostEqual(commands[0], -0.6)
        self.assertAlmostEqual(commands[1], 0.7)
        model = mujoco.MjModel.from_xml_path(str(self.root / "mjcf/robot.xml"))
        state = mujoco.MjData(model)
        state.qpos[0] = 0.05
        state.qvel[1] = 0.2
        mujoco.mj_forward(model, state)
        observed = read_observations(data, model, state, mujoco)
        self.assertAlmostEqual(observed[0], 0.17)
        self.assertAlmostEqual(observed[1], -0.04)
        for target in ("map_actions", "read_observations"):
            with patch("description_pipeline.verification.control." + target, return_value=[0.0, 0.0]):
                self.assertIn("consumer.control", assess(self.root, "kinematics")["blockers"])
        data["control"]["actions"]["arm"]["unit"] = "degrees"
        with self.assertRaisesRegex(PipelineError, "SI unit"):
            map_actions(data, [0.5, 0.7])

    def test_cached_generation_still_runs_consumer_and_rejects_corrupt_cache(self):
        self.successful_build()
        with patch("description_pipeline.build.generate", side_effect=AssertionError("cache miss")):
            report = build(self.root, "kinematics")
        self.assertTrue(report["passed"])
        self.assertIn("consumer.inertia", {item["id"] for item in report["checks"]})
        cached = next((self.root / "build/cache/generate").glob("*/urdf/robot.urdf"))
        cached.write_text("corrupt")
        with self.assertRaisesRegex(PipelineError, "cache"):
            build(self.root, "kinematics")

    def test_geometry_semantic_tampering_is_detected_by_actual_engine(self):
        self.successful_build()
        path = self.root / "mjcf/robot.xml"
        xml = ET.parse(path)
        geom = xml.find(".//geom")
        assert geom is not None
        geom.set("size", "0.5 0.4 0.3")
        xml.write(path)
        report = assess(self.root, "kinematics")
        self.assertIn("consumer.geometry", report["blockers"])

    def test_removing_report_hashes_cannot_bypass_bundle_gate(self):
        self.successful_build()
        path = self.root / "manifest.json"
        manifest = read_data(path)
        manifest["reports"] = {}
        write_json(path, manifest)
        self.assertIn("bundle.report_coverage", assess(self.root, "kinematics")["blockers"])

    def test_model_rejects_invalid_sensor_references_and_mimic_cycles(self):
        data = scene()
        data["sensors"] = [{"id": "gyro", "name": "gyro", "type": "gyro", "frame": "missing", "provenance": {}}]
        with self.assertRaisesRegex(PipelineError, "sensor frame"):
            Robot.from_dict(data)
        data = scene()
        data["joints"][0]["mimic"] = {"joint": "slider_joint", "multiplier": 1, "offset": 0}
        data["joints"][1]["mimic"] = {"joint": "arm_joint", "multiplier": 1, "offset": 0}
        with self.assertRaisesRegex(PipelineError, "Cyclic mimic"):
            Robot.from_dict(data)

    def test_effective_actuator_gear_and_sensor_targets_are_checked(self):
        data = scene()
        data["actuators"] = [
            {
                "id": "motor",
                "name": "motor",
                "joint": "arm_joint",
                "type": "motor",
                "gear": 2,
                "control_range": [-1, 1],
                "provenance": {},
            }
        ]
        data["sensors"] = [{"id": "gyro", "name": "gyro", "type": "gyro", "frame": "imu", "provenance": {}}]
        write_json(self.source / "scene.json", data)
        write_manifest(self.source, kind="fixture", identity={"case": "interfaces"}, evidence_class="fixture")
        freeze(self.root)
        self.successful_build()
        path = self.root / "mjcf/robot.xml"
        xml = ET.parse(path)
        motor = xml.find("actuator/motor")
        assert motor is not None
        motor.set("gear", "3")
        xml.write(path)
        self.assertIn("consumer.interfaces", assess(self.root, "kinematics")["blockers"])

    def test_missing_physical_mass_cannot_be_relabelled_as_a_reference(self):
        data = scene()
        data["links"][0]["inertial"] = None
        with self.assertRaisesRegex(PipelineError, "Missing physical inertia"):
            Robot.from_dict(data)

    def test_snapshot_tampering_and_extra_files_are_rejected(self):
        (self.source / "unexpected.txt").write_text("unbound")
        with self.assertRaises(PipelineError):
            verify_snapshot(self.source)

    def test_source_definition_change_requires_refreeze(self):
        definition = read_data(self.root / "config/robot.yaml")
        definition["source"]["revision"] = "changed"
        write_json(self.root / "config/robot.yaml", definition)
        with self.assertRaisesRegex(PipelineError, "freeze"):
            build(self.root, "kinematics")

    def test_freeze_failure_keeps_diagnostic_when_destination_is_precreated(self):
        """The Windows failure path must preserve the original exception and evidence."""

        with (
            patch("description_pipeline.build.load_scene", side_effect=PipelineError("capture interrupted")),
            self.assertRaisesRegex(PipelineError, "capture interrupted") as caught,
        ):
            freeze(self.root)
        diagnostic = Path(vars(caught.exception)["diagnostic_path"])
        self.assertTrue(diagnostic.is_dir())
        failure = read_data(diagnostic / "failure.json")
        self.assertEqual(failure["error"], "PipelineError")
        self.assertIn("capture interrupted", failure["message"])

    def test_same_eigenvalues_wrong_inertial_orientation_is_caught(self):
        self.successful_build()
        path = self.root / "mjcf/robot.xml"
        xml = ET.parse(path)
        inertia = xml.find(".//body/inertial")
        assert inertia is not None
        inertia.attrib.pop("fullinertia")
        inertia.set("diaginertia", "0.002 0.003 0.004")
        inertia.set("quat", "1 0 0 0")
        xml.write(path)
        profile = read_data(self.root / "config/profiles/kinematics.json")
        checks = {item["id"]: item for item in inspect(self.root, profile)}
        self.assertEqual(checks["consumer.inertia"]["status"], "failed")

    def test_missing_body_cannot_turn_into_zero_error(self):
        self.successful_build()
        path = self.root / "mjcf/robot.xml"
        xml = ET.parse(path)
        body = xml.find(".//body")
        assert body is not None
        body.set("name", "renamed_base")
        xml.write(path)
        report = assess(self.root, "kinematics")
        check = next(item for item in report["checks"] if item["id"] == "consumer.inertia")
        self.assertIn("base_link", check["missing"])
        self.assertFalse(report["passed"])

    def test_rotated_frame_and_prismatic_axis_errors_are_caught(self):
        self.successful_build()
        path = self.root / "mjcf/robot.xml"
        xml = ET.parse(path)
        for joint in xml.findall(".//joint"):
            if joint.get("name") == "slider_joint":
                joint.set("axis", "1 0 0")
        site = xml.find(".//site")
        assert site is not None
        site.set("euler", "0 0 0")
        xml.write(path)
        profile = read_data(self.root / "config/profiles/kinematics.json")
        checks = {item["id"]: item for item in inspect(self.root, profile)}
        self.assertEqual(checks["consumer.kinematics"]["status"], "failed")
        self.assertEqual(checks["consumer.joints"]["status"], "failed")

    def test_required_consumer_missing_blocks_qualification(self):
        with patch.dict("sys.modules", {"mujoco": None}):
            report = build(self.root, "kinematics")
        self.assertFalse(report["passed"])
        self.assertIn("consumer.available", report["blockers"])
        self.assertFalse((self.root / "manifest.json").exists())
        self.assertTrue(Path(report["diagnostic_path"]).is_dir())

    def test_repeated_failure_preserves_each_candidate(self):
        with patch.dict("sys.modules", {"mujoco": None}):
            first = build(self.root, "kinematics")
            second = build(self.root, "kinematics")
        self.assertEqual(first["subject"], second["subject"])
        self.assertNotEqual(first["diagnostic_path"], second["diagnostic_path"])
        for report in (first, second):
            path = Path(report["diagnostic_path"])
            self.assertLess(len(path.name), 24)
            self.assertEqual(read_data(path / "manifest.json")["subject"], report["subject"])

    def test_unsupported_constraints_are_not_silently_discarded(self):
        data = scene()
        data["constraints"] = [{"type": "closed_loop", "body1": "base_link", "body2": "slider_link"}]
        write_json(self.source / "scene.json", data)
        write_manifest(self.source, kind="fixture", identity={"case": "closed-loop"}, evidence_class="fixture")
        freeze(self.root)
        with self.assertRaisesRegex(PipelineError, "unsupported"):
            build(self.root, "kinematics")

    def test_physical_inertia_and_duplicate_identity_rejected(self):
        data = scene()
        data["links"][0]["inertial"]["inertia"] = [1, 0, 0, 1, 0, 10]
        with self.assertRaises(PipelineError):
            Robot.from_dict(data)
        data = scene()
        data["links"][1]["id"] = "base_link"
        with self.assertRaises(PipelineError):
            Robot.from_dict(data)


if __name__ == "__main__":
    unittest.main()
