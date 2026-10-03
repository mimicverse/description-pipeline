"""Independent analytic observations, real consumer replay and authority rejection."""

import contextlib
import copy
import io
import json
import math
import os
import shlex
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree as ET

import mujoco

from description_pipeline.build import assess, author_files, build, normalize, profile_for, subject_files
from description_pipeline.cli import main, parser
from description_pipeline.io import PipelineError, file_digest, read_data, write_json
from description_pipeline.model import Robot
from description_pipeline.repository import promote, promotion_plan, submit, update, validate_commit
from description_pipeline.verification.acceptance import verify_acceptance
from description_pipeline.verification.mechanics import (
    CONVENTIONS,
    SCHEMA,
    _read_json,
    execute,
    reference_path,
    run_acceptance,
)
from tests.pipeline.test_simulation_acceptance import Workspace, scene


def yaw(q, height=0.15):
    # Independent analytic fixture equations; no pipeline FK, output or canonical model is read.
    c, s = math.cos(q), math.sin(q)
    return [[c, -s, 0, 0], [s, c, 0, 0], [0, 0, 1, height], [0, 0, 0, 1]]


IDENTITY = yaw(0, 0)
DRIVES = {"arm_joint": {"kind": "active", "id": "fixture-motor", "stator": ["base_link"], "rotor": ["arm_link"]}}
ENVIRONMENT = {"mujoco": mujoco.__version__, "mechanism": "analytic-yaw/v1"}


def reference(manifest):
    return {
        "schema_version": SCHEMA,
        "hardware_id": "fixture",
        "source_manifest_digest": manifest,
        "suite": "mechanical-fixture",
        "environment": ENVIRONMENT,
        "evidence_class": "fixture",
        "data_role": "validation",
        "used_for_fitting": False,
        "conditions": {"producer": "independent analytic equations", "procedure": "Rz(q) at z=0.15 m"},
        "conventions": CONVENTIONS,
        "tolerances": {"position_m": 1e-6, "rotation_rad": 1e-6},
        "ownership": {"base_link": ["base_link"], "arm_link": ["arm_link"]},
        "joints": {
            "arm_joint": {
                "type": "revolute",
                "parent": "base_link",
                "child": "arm_link",
                "origin": yaw(0),
                "axis": [0, 0, 1],
                "limits": {"lower": -0.1, "upper": 0.2},
                "mimic": None,
            }
        },
        "drives": DRIVES,
        "constraints": [],
        "zero_pose": "zero",
        "poses": [
            {
                "name": name,
                "joints": {"arm_joint": q},
                "base": IDENTITY,
                "links": {"base_link": IDENTITY, "arm_link": yaw(q)},
            }
            for name, q in (("zero", 0), ("negative", -0.04), ("positive", 0.12))
        ],
    }


class MechanicalAcceptanceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.workspace = Workspace(self.base / "fixture")
        self.root = self.workspace.root
        self.profile_path = self.root / "config/profiles/kinematics.json"
        profile = read_data(self.profile_path)
        profile.update(acceptance_suites=["mechanical-fixture"], consumer_environment=ENVIRONMENT)
        write_json(self.profile_path, profile)
        config = read_data(self.root / "config/robot.yaml")
        config["interfaces"] = {"mechanical_drives": DRIVES}
        write_json(self.root / "config/robot.yaml", config)
        self.reference = self.base / "approved-reference.json"
        self.expected = reference(read_data(self.root / "sources/source.lock.json")["manifest_digest"])
        write_json(self.reference, self.expected)
        pending = build(self.root, "kinematics", mechanical_reference=self.reference)
        self.assertEqual(pending["blockers"], ["consumer.application"])
        self.candidate = Path(pending["diagnostic_path"])
        self.profile = profile_for(self.candidate, "kinematics")
        self.out = self.base / "result"
        self.record = run_acceptance(self.candidate, "kinematics", self.reference, self.out)
        self.assertTrue(
            self.record["results"][0]["passed"], read_data(self.out / "docs/acceptance/mechanical-observations.json")
        )
        shutil.copytree(self.out / "docs/acceptance", self.root / "docs/acceptance", dirs_exist_ok=True)

    def verdict(self, record=None, **kwargs):
        shutil.copytree(self.out / "docs/acceptance", self.candidate / "docs/acceptance", dirs_exist_ok=True)
        write_json(self.candidate / "docs/acceptance/kinematics.json", record or self.record)
        kwargs.setdefault("mechanical_reference", self.reference)
        return verify_acceptance(self.candidate, self.record["subject"], self.profile, **kwargs)

    def replay_new_reference(self, expected):
        write_json(self.reference, expected)
        pending = build(self.root, "kinematics", mechanical_reference=self.reference)
        self.assertEqual(pending["blockers"], ["consumer.application"])
        candidate = Path(pending["diagnostic_path"])
        record = run_acceptance(candidate, "kinematics", self.reference, self.base / "new-result")
        self.assertTrue(record["results"][0]["passed"])
        return candidate

    def test_prismatic_motion_uses_metres(self):
        payload = scene()
        payload["joints"][0].update(type="prismatic", axis=[1, 0, 0])
        self.workspace.write_source(payload)
        expected = copy.deepcopy(self.expected)
        expected["source_manifest_digest"] = read_data(self.root / "sources/source.lock.json")["manifest_digest"]
        expected["joints"]["arm_joint"].update(type="prismatic", axis=[1, 0, 0])
        for pose in expected["poses"]:
            pose["links"]["arm_link"] = yaw(0)
            pose["links"]["arm_link"][0][3] = pose["joints"]["arm_joint"]
        self.replay_new_reference(expected)

    def test_floating_base_observations_are_world_transforms(self):
        profile = read_data(self.profile_path)
        profile["root_mode"] = "floating"
        write_json(self.profile_path, profile)
        expected = copy.deepcopy(self.expected)
        for pose in expected["poses"]:
            pose["base"][0][3] = 0.5
            pose["links"]["base_link"][0][3] = 0.5
            pose["links"]["arm_link"][0][3] = 0.5
        self.replay_new_reference(expected)

    def test_derived_reference_frames_have_no_cad_ownership(self):
        config = read_data(self.root / "config/robot.yaml")
        config["interfaces"]["frames"] = [
            {"id": "tip", "name": "tip", "parent": "arm_link", "xyz": [0.1, 0, 0], "rpy": [0, 0, 0], "provenance": {}}
        ]
        write_json(self.root / "config/robot.yaml", config)
        expected = copy.deepcopy(self.expected)
        expected["ownership"]["tip"] = []
        origin = copy.deepcopy(IDENTITY)
        origin[0][3] = 0.1
        expected["joints"]["tip_fixed"] = {
            "type": "fixed",
            "parent": "arm_link",
            "child": "tip",
            "origin": origin,
            "axis": None,
            "limits": None,
            "mimic": None,
        }
        for pose in expected["poses"]:
            q = pose["joints"]["arm_joint"]
            matrix = yaw(q)
            matrix[0][3], matrix[1][3] = 0.1 * math.cos(q), 0.1 * math.sin(q)
            pose["links"]["tip"] = matrix
        self.replay_new_reference(expected)

    def test_mimic_zero_offset_motion_and_actual_equality(self):
        payload = scene(hand=True)
        payload["joints"][1]["mimic"] = {"joint": "arm_joint", "multiplier": 0.5, "offset": 0.01}
        payload["actuators"] = payload["actuators"][:1]
        self.workspace.write_source(payload)
        config = read_data(self.root / "config/robot.yaml")
        config["interfaces"]["mechanical_drives"]["hand_joint"] = {"kind": "passive"}
        write_json(self.root / "config/robot.yaml", config)
        expected = copy.deepcopy(self.expected)
        expected["source_manifest_digest"] = read_data(self.root / "sources/source.lock.json")["manifest_digest"]
        expected["ownership"]["hand_link"] = ["hand_link"]
        expected["drives"]["hand_joint"] = {"kind": "passive"}
        origin = copy.deepcopy(IDENTITY)
        origin[0][3] = 0.27
        expected["joints"]["hand_joint"] = {
            "type": "revolute",
            "parent": "arm_link",
            "child": "hand_link",
            "origin": origin,
            "axis": [0, 1, 0],
            "limits": {"lower": -2, "upper": 2},
            "mimic": {"joint": "arm_joint", "multiplier": 0.5, "offset": 0.01},
        }
        for pose in expected["poses"]:
            qa = pose["joints"]["arm_joint"]
            qh = 0.5 * qa + 0.01
            pose["joints"]["hand_joint"] = qh
            ca, sa, ch, sh = math.cos(qa), math.sin(qa), math.cos(qh), math.sin(qh)
            pose["links"]["hand_link"] = [
                [ca * ch, -sa, ca * sh, 0.27 * ca],
                [sa * ch, ca, sa * sh, 0.27 * sa],
                [-sh, 0, ch, 0.15],
                [0, 0, 0, 1],
            ]
        candidate = self.replay_new_reference(expected)
        path = candidate / "mjcf/robot.xml"
        tree = ET.parse(path)
        equality = tree.getroot().find("equality/joint")
        assert equality is not None
        equality.set("polycoef", "0.01 0.7 0 0 0")
        tree.write(path, encoding="unicode")
        self.assertFalse(execute(candidate, profile_for(candidate, "kinematics"), self.reference)["passed"])

    def test_mechanical_drive_inputs_fail_early_and_have_set_semantics(self):
        payload = scene()
        payload["mechanical_drives"] = copy.deepcopy(DRIVES)
        for bad in (
            {},
            {"arm_joint": {"kind": "passive"}},
            {"arm_joint": {"kind": "active", "id": "a", "stator": ["unknown"], "rotor": ["arm_link"]}},
            {"arm_joint": {"kind": "active", "id": "a", "stator": ["base_link"], "rotor": ["base_link"]}},
        ):
            with self.subTest(bad=bad), self.assertRaises(PipelineError):
                Robot.from_dict({**payload, "mechanical_drives": bad})
        payload["links"][0]["provenance"]["source_entities"].append("base-second")
        payload["provenance"]["expected_entities"].append("base-second")
        payload["mechanical_drives"]["arm_joint"]["stator"].append("base-second")
        config = {"source": {"provider": "fixture"}}
        before = normalize(payload, config, self.root)
        payload["mechanical_drives"]["arm_joint"]["stator"].reverse()
        self.assertEqual(normalize(payload, config, self.root), before)

    def test_author_edits_during_update_prevent_evidence_replacement(self):
        old = (self.root / "docs/acceptance/kinematics.json").read_bytes()
        before = author_files(self.root)
        from description_pipeline.verification.mechanics import complete_pending

        pending = build(self.root, "kinematics")
        with (
            patch("description_pipeline.build.author_files", side_effect=[before, {**before, "config/change": "new"}]),
            self.assertRaisesRegex(PipelineError, "changed during acceptance"),
        ):
            complete_pending(self.root, "kinematics", pending, self.reference)
        self.assertEqual((self.root / "docs/acceptance/kinematics.json").read_bytes(), old)

    def test_publish_relocation_and_explicit_consumer_selection(self):
        report = build(self.root, "kinematics", mechanical_reference=self.reference)
        self.assertTrue(report["passed"], report["blockers"])
        self.assertEqual(report["qualified_for"], ["kinematics"])
        relocated = self.base / "consumer"
        shutil.copytree(self.root, relocated, ignore=shutil.ignore_patterns("build"))
        shutil.rmtree(self.root)
        shutil.rmtree(self.workspace.source)
        checked = assess(relocated, "kinematics", mechanical_reference=self.reference)
        self.assertTrue(checked["passed"], checked["blockers"])
        self.assertEqual(checked["subject"], self.record["subject"])
        without = assess(relocated, "kinematics")
        self.assertIn("consumer.application", without["blockers"])
        self.assertEqual(next(c for c in without["checks"] if c["id"] == "consumer.application")["status"], "not_run")

    def test_replay_is_deterministic_and_reads_both_consumers(self):
        self.assertEqual(
            execute(self.candidate, self.profile, self.reference), execute(self.candidate, self.profile, self.reference)
        )
        measured = read_data(self.out / "docs/acceptance/mechanical-observations.json")
        self.assertEqual(measured["joints"]["arm_joint"]["mujoco"], {"robot": True, "scene": True})
        self.assertEqual(len(measured["observations"]), 6)
        self.assertTrue(all(item["scene_position_error_m"] <= 1e-7 for item in measured["observations"]))

    def test_public_cli_accept_and_check(self):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            status = main(
                [
                    "model",
                    "accept",
                    "--root",
                    str(self.candidate),
                    "--profile",
                    "kinematics",
                    "--mechanical-reference",
                    str(self.reference),
                    "--out",
                    str(self.base / "cli-result"),
                ]
            )
        value = json.loads(stdout.getvalue())
        self.assertEqual(status, 0, value)
        self.assertFalse(value["release_qualified"])
        self.assertEqual(value["subject"], self.record["subject"])
        build(self.root, "kinematics", mechanical_reference=self.reference)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(
                main(["check", "--root", str(self.root), "--mechanical-reference", str(self.reference)]), 0
            )
            self.assertEqual(main(["check", "--root", str(self.root)]), 1)

    def test_every_author_and_consumer_entry_accepts_the_reference_option(self):
        common = ["--root", str(self.root), "--mechanical-reference", str(self.reference)]
        for command, extra in (
            (["build"], []),
            (["check"], []),
            (["model", "update"], []),
            (["model", "submit"], ["--message", "update"]),
            (["model", "validate"], ["--candidate", "1" * 40]),
            (["model", "promote"], ["--candidate", "1" * 40, "--hardware", "fixture"]),
        ):
            with self.subTest(command=command):
                self.assertEqual(parser().parse_args([*command, *common, *extra]).mechanical_reference, self.reference)

    def test_reference_cannot_come_from_original_workspace(self):
        internal = self.root / "build/operator-reference.json"
        write_json(internal, self.expected)
        with self.assertRaisesRegex(PipelineError, "outside"):
            build(self.root, "kinematics", mechanical_reference=internal)
        subprocess.run(["git", "init", "--quiet", str(self.root)], check=True)
        with self.assertRaisesRegex(PipelineError, "outside"):
            validate_commit(self.root, "1" * 40, "kinematics", mechanical_reference=internal)

    def test_reference_symlink_cannot_bypass_workspace_boundary(self):
        internal = self.root / "build/operator-reference.json"
        write_json(internal, self.expected)
        outside = self.base / "symlink.json"
        try:
            outside.symlink_to(internal)
        except OSError as error:
            if os.name == "nt" and getattr(error, "winerror", None) == 1314:
                self.skipTest("Windows account lacks symbolic-link privilege")
            raise
        with self.assertRaisesRegex(PipelineError, "outside"):
            reference_path(outside, self.root)

    def test_missing_changed_and_operator_unselected_reference_are_rejected(self):
        self.assertEqual(self.verdict()["status"], "passed")
        self.assertEqual(self.verdict(mechanical_reference=None)["status"], "not_run")
        write_json(self.reference, {**self.expected, "conditions": {"producer": "different acquisition"}})
        self.assertEqual(self.verdict()["status"], "failed")
        self.reference.unlink()
        self.assertEqual(self.verdict()["status"], "failed")

    def test_reference_json_is_strict_and_resource_bounded(self):
        for raw in (
            '{"schema_version": 1, "schema_version": 2}',
            '{"bad": NaN}',
            '{"bad": Infinity}',
            '{"conditions": {"unused": 1e999}}',
            "[1]",
            "{",
        ):
            with self.subTest(raw=raw):
                self.reference.write_text(raw, encoding="utf-8")
                self.assertEqual(self.verdict()["status"], "failed")
        with self.reference.open("wb") as stream:
            stream.truncate(32 * 1024 * 1024 + 1)
        with self.assertRaisesRegex(PipelineError, "32 MiB"):
            reference_path(self.reference, self.root)

    def test_json_overflow_is_rejected_even_in_metadata(self):
        self.reference.write_text('{"conditions": {"unused": 1e999}}', encoding="utf-8")
        with self.assertRaisesRegex(PipelineError, "Non-finite"):
            _read_json(self.reference)

    def test_oversized_telemetry_is_rejected_before_parsing(self):
        with patch("description_pipeline.verification.mechanics.TELEMETRY_LIMIT", 1):
            verdict = self.verdict()
        self.assertEqual(verdict["status"], "failed")
        self.assertIn("byte limit", verdict["details"]["authority"]["reason"])

    def test_input_hash_and_fitting_reference_cannot_be_reused_as_validation(self):
        write_json(self.root / "docs/fitted-reference.json", self.expected)
        pending = build(self.root, "kinematics", mechanical_reference=self.reference)
        with self.assertRaisesRegex(PipelineError, "model/fitting inputs"):
            run_acceptance(Path(pending["diagnostic_path"]), "kinematics", self.reference, self.base / "reused")
        checksum = next(iter(self.record["results"][0]["validation_data"].values()))
        self.assertEqual(self.verdict(input_hashes={checksum})["status"], "failed")

    def test_reference_identity_semantics_and_coverage_are_required(self):
        mutations = [
            lambda r: r.update(schema_version="unknown"),
            lambda r: r.update(hardware_id="wrong"),
            lambda r: r.update(source_manifest_digest="0" * 64),
            lambda r: r.update(suite="different"),
            lambda r: r.update(environment={"mujoco": "wrong"}),
            lambda r: r.update(used_for_fitting=True),
            lambda r: r.update(data_role="fitting"),
            lambda r: r.update(evidence_class="physical_measurement"),
            lambda r: r.update(conditions={}),
            lambda r: r["conventions"].update(joint_axis="unsigned"),
            lambda r: r["tolerances"].update(rotation_rad=0),
            lambda r: r["tolerances"].update(position_m=True),
            lambda r: r["ownership"].pop("arm_link"),
            lambda r: r["ownership"]["base_link"].append("arm_link"),
            lambda r: r["ownership"]["base_link"].append("base_link"),
            lambda r: r["joints"].clear(),
            lambda r: r["drives"].clear(),
            lambda r: r["drives"]["arm_joint"]["stator"].clear(),
            lambda r: r["drives"]["arm_joint"].update(id="unmatched-drive"),
            lambda r: r["drives"].update(arm_joint={"kind": "passive"}),
            lambda r: r.update(zero_pose="missing"),
            lambda r: r.update(poses=r["poses"][:1]),
            lambda r: r["poses"][1].update(name="zero"),
            lambda r: r["poses"][1]["links"].pop("arm_link"),
            lambda r: r["poses"][1]["joints"].update(arm_joint=0.5),
            lambda r: r["poses"][0]["joints"].update(arm_joint=0.01),
            lambda r: r["poses"][1]["links"]["arm_link"][0].__setitem__(0, 3),
            lambda r: r["poses"][1]["base"][3].__setitem__(3, 0),
        ]
        for index, mutate in enumerate(mutations):
            payload = copy.deepcopy(self.expected)
            mutate(payload)
            write_json(self.reference, payload)
            with self.subTest(index=index):
                self.assertNotEqual(self.verdict()["status"], "passed")

    def test_no_travel_allows_no_mechanical_verdict(self):
        payload = copy.deepcopy(self.expected)
        for pose in payload["poses"]:
            pose["joints"]["arm_joint"] = 0
            pose["links"]["arm_link"] = yaw(0)
        write_json(self.reference, payload)
        with self.assertRaisesRegex(PipelineError, "held-out motion"):
            execute(self.candidate, self.profile, self.reference)

    def test_axis_ownership_origin_limits_and_observations_must_agree(self):
        mutations = [
            lambda r: r["joints"]["arm_joint"].update(axis=[0, 1, 0]),
            lambda r: r["joints"]["arm_joint"].update(axis=[0, 0, -1]),
            lambda r: r["joints"]["arm_joint"]["origin"][0].__setitem__(3, 0.01),
            lambda r: r["joints"]["arm_joint"]["limits"].update(lower=-0.2),
            lambda r: r["ownership"].update(base_link=["arm_link"], arm_link=["base_link"]),
            lambda r: r["drives"]["arm_joint"].update(stator=["arm_link"], rotor=["base_link"]),
            lambda r: r["poses"][1]["links"]["arm_link"][0].__setitem__(3, 0.01),
        ]
        for index, mutate in enumerate(mutations):
            payload = copy.deepcopy(self.expected)
            mutate(payload)
            write_json(self.reference, payload)
            with self.subTest(index=index):
                measured = execute(self.candidate, self.profile, self.reference)
                self.assertFalse(measured["passed"])

    def test_author_cannot_relax_the_operator_reference_tolerance(self):
        profile = {**self.profile, "position_atol": 1, "rotation_atol": 1}
        payload = copy.deepcopy(self.expected)
        payload["poses"][1]["links"]["arm_link"][0][3] = 0.0001
        write_json(self.reference, payload)
        measured = execute(self.candidate, profile, self.reference)
        self.assertFalse(measured["passed"])
        self.assertEqual(measured["conditions"]["position_atol_m"], 1e-6)

    def test_record_forgery_and_purpose_escalation_never_qualify(self):
        mutations = [
            lambda r: r.update(subject="0" * 64),
            lambda r: r.update(profile_digest="0" * 64),
            lambda r: r.update(purpose="hardware"),
            lambda r: r.update(reference_sha256="0" * 64),
            lambda r: r["tool"].update(development=False),
            lambda r: r["runtime"].update(python="wrong"),
            lambda r: r["qualification"].update(hardware_qualified=True),
            lambda r: r.update(approved=True),
            lambda r: r["attestation"].update(trusted=True),
            lambda r: r.update(results=[]),
            lambda r: r["results"][0].update(evidence_class="simulation"),
            lambda r: r["results"][0].update(passed=False),
            lambda r: r["results"][0].update(used_for_fitting=True),
            lambda r: r["results"].append(copy.deepcopy(r["results"][0])),
        ]
        for index, mutate in enumerate(mutations):
            record = copy.deepcopy(self.record)
            mutate(record)
            with self.subTest(index=index):
                self.assertEqual(self.verdict(record)["status"], "failed")
        for purpose in ("simulation", "training", "hardware"):
            record = {**self.record, "purpose": purpose}
            write_json(self.candidate / f"docs/acceptance/{purpose}.json", record)
            verdict = verify_acceptance(
                self.candidate,
                self.record["subject"],
                {**self.profile, "purpose": purpose},
                mechanical_reference=self.reference,
            )
            self.assertEqual(verdict["status"], "failed")

    def test_forged_rehashed_measurements_are_rejected(self):
        record = copy.deepcopy(self.record)
        name = "docs/acceptance/mechanical-observations.json"
        measured = read_data(self.out / name)
        measured["observations"][1]["urdf_position_error_m"] = 0.01
        write_json(self.out / name, measured)
        checksum = file_digest(self.out / name)
        record["results"][0]["artifacts"][name] = checksum
        record["results"][0]["validation_data"][name] = checksum
        self.assertEqual(self.verdict(record)["status"], "failed")

    def test_actual_urdf_and_scene_changes_are_detected(self):
        for entry in ("urdf/robot.urdf", "mjcf/robot.xml", "mjcf/scene.xml"):
            path = self.candidate / entry
            original = path.read_bytes()
            with self.subTest(entry=entry):
                tree = ET.parse(path)
                if entry.startswith("urdf"):
                    node = tree.getroot().find("joint/axis")
                    assert node is not None
                    node.set("xyz", "0 1 0")
                elif entry.endswith("robot.xml"):
                    node = tree.getroot().find("worldbody/body/body/joint")
                    assert node is not None
                    node.set("range", "-0.2 0.2")
                else:
                    node = tree.getroot().find("include")
                    assert node is not None
                    node.set("file", "missing.xml")
                tree.write(path, encoding="unicode")
                self.assertEqual(self.verdict()["status"], "failed")
            path.write_bytes(original)

    def test_mid_run_mutations_and_other_model_failures_are_rejected(self):
        original = subject_files(self.candidate)
        changed = {**original, "config/mutated.json": "0" * 64}
        with (
            patch("description_pipeline.build.subject_files", side_effect=[original, original, changed]),
            self.assertRaisesRegex(PipelineError, "changed during"),
        ):
            execute(self.candidate, self.profile, self.reference)
        path = self.candidate / "urdf/robot.urdf"
        path.write_text(path.read_text().replace('xyz="0 0 1"', 'xyz="0 1 0"'), encoding="utf-8")
        with self.assertRaises(PipelineError) as rejected:
            run_acceptance(self.candidate, "kinematics", self.reference, self.base / "broken")
        self.assertTrue((Path(str(rejected.exception.diagnostic_path)) / "failure.json").is_file())
        self.assertFalse((self.base / "broken").exists())

    def test_execution_error_preserves_diagnostic_and_never_publishes(self):
        with (
            patch("description_pipeline.verification.mechanics.execute", side_effect=RuntimeError("consumer failed")),
            self.assertRaisesRegex(RuntimeError, "consumer failed") as rejected,
        ):
            run_acceptance(self.candidate, "kinematics", self.reference, self.base / "crashed")
        self.assertTrue((Path(str(getattr(rejected.exception, "diagnostic_path", None))) / "failure.json").is_file())
        self.assertFalse((self.base / "crashed").exists())

    def test_windows_application_control_is_unexecuted(self):
        class BlockedRuntime(OSError):
            winerror = 4551

        blocked = BlockedRuntime("Windows App Control rejected elasticity.dll")
        with patch("description_pipeline.verification.mechanics.execute", side_effect=blocked):
            verdict = self.verdict()
        self.assertEqual(verdict["status"], "not_run")
        self.assertFalse(verdict["details"]["authority"]["trusted"])

    def test_one_click_update_completes_acceptance_and_holds_the_lock(self):
        shutil.rmtree(self.root / "docs/acceptance")
        subprocess.run(["git", "init", "--quiet", str(self.root)], check=True)

        def submit_verified(root, profile, message, *, ci=False, mechanical_reference=None):
            self.assertTrue((root / ".git/description-update.lock").exists())
            verdict = assess(root, profile, mechanical_reference=mechanical_reference)
            self.assertTrue(verdict["passed"], verdict["blockers"])
            return {"passed": True, "state": "pull_request_open", "pull_request": "https://example/pr/1"}

        with (
            patch(
                "description_pipeline.repository.update_preflight",
                return_value={"hardware": "fixture", "branch": "feature/fixture"},
            ),
            patch("description_pipeline.repository._submit", side_effect=submit_verified) as submitted,
        ):
            result = update(self.root, "kinematics", reuse_source=True, mechanical_reference=self.reference)
        self.assertTrue(result["ok"])
        submitted.assert_called_once()
        self.assertFalse((self.root / ".git/description-update.lock").exists())

    def test_failed_update_preserves_existing_evidence_and_never_submits(self):
        old = (self.root / "docs/acceptance/kinematics.json").read_bytes()
        payload = copy.deepcopy(self.expected)
        payload["joints"]["arm_joint"]["axis"] = [0, 1, 0]
        write_json(self.reference, payload)
        subprocess.run(["git", "init", "--quiet", str(self.root)], check=True)
        with (
            patch(
                "description_pipeline.repository.update_preflight",
                return_value={"hardware": "fixture", "branch": "feature/fixture"},
            ),
            patch("description_pipeline.repository._submit") as submitted,
            self.assertRaisesRegex(PipelineError, "does not qualify") as rejected,
        ):
            update(self.root, "kinematics", reuse_source=True, mechanical_reference=self.reference)
        submitted.assert_not_called()
        self.assertEqual((self.root / "docs/acceptance/kinematics.json").read_bytes(), old)
        self.assertTrue((Path(str(rejected.exception.diagnostic_path)) / "acceptance.json").is_file())
        self.assertFalse((self.root / ".git/description-update.lock").exists())

    def test_validation_relocates_and_replays_the_exact_git_commit(self):
        build(self.root, "kinematics", mechanical_reference=self.reference)
        subprocess.run(["git", "init", "--quiet", str(self.root)], check=True)
        for args in (
            ("config", "user.name", "Test"),
            ("config", "user.email", "test@example.com"),
            ("add", "."),
            ("commit", "--quiet", "-m", "fixture"),
        ):
            subprocess.run(["git", "-C", str(self.root), *args], check=True, capture_output=True)
        sha = subprocess.check_output(["git", "-C", str(self.root), "rev-parse", "HEAD"], text=True).strip()
        checked = validate_commit(self.root, sha, "kinematics", mechanical_reference=self.reference)
        self.assertTrue(checked["passed"], checked["blockers"])
        self.assertEqual(checked["subject"], self.record["subject"])
        self.assertFalse(validate_commit(self.root, sha, "kinematics")["passed"])

    def test_pr_failure_retry_keeps_the_operator_reference_and_real_qualification(self):
        build(self.root, "kinematics", mechanical_reference=self.reference)
        subprocess.run(["git", "init", "--quiet", str(self.root)], check=True)
        for args in (
            ("config", "user.name", "Test"),
            ("config", "user.email", "test@example.com"),
            ("switch", "-c", "feature/fixture"),
        ):
            subprocess.run(["git", "-C", str(self.root), *args], check=True, capture_output=True)
        with (
            patch("description_pipeline.repository.push", return_value=[]),
            patch("description_pipeline.repository._base_moved_advisory", return_value=None),
            patch("description_pipeline.repository.repository_slug", return_value="test/fixture"),
            patch("description_pipeline.repository._review_request", side_effect=PipelineError("review unavailable")),
        ):
            response = submit(self.root, "kinematics", "Update mechanism", mechanical_reference=self.reference)
        self.assertEqual(response["state"], "pushed_pending_review")
        self.assertEqual(response["mechanical_reference_sha256"], file_digest(self.reference))
        if __import__("os").name != "nt":
            retry = parser().parse_args(shlex.split(response["retry"].split(" # ")[0])[1:])
            self.assertEqual(retry.mechanical_reference, self.reference)

    def test_promotion_requires_fresh_operator_selection_and_reference_binding(self):
        build(self.root, "kinematics", mechanical_reference=self.reference)
        report = assess(self.root, "kinematics", mechanical_reference=self.reference)
        # Only Git transport/native eligibility are substituted: the reference authority above ran for real.
        report["source"]["evidence_class"] = "cad"
        report["toolchain"] = {"source_commit": "a" * 40, "development": False}
        git_result = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        with (
            patch("description_pipeline.repository._require_checkout"),
            patch("description_pipeline.repository.git", return_value=git_result),
            patch("description_pipeline.repository.validate_commit", return_value=report) as validate,
            patch("description_pipeline.repository._accepted_tool_main", return_value="public-main"),
        ):
            plan = promotion_plan(self.root, "fixture", "1" * 40, "kinematics", mechanical_reference=self.reference)
        validate.assert_called_once_with(
            self.root, "1" * 40, "kinematics", remote=True, mechanical_reference=self.reference
        )
        self.assertEqual(plan["mechanical_reference_sha256"], file_digest(self.reference))
        with (
            patch(
                "description_pipeline.repository.promotion_plan",
                return_value={**plan, "mechanical_reference_sha256": "0" * 64},
            ) as fresh,
            patch("description_pipeline.repository.push") as pushed,
            self.assertRaisesRegex(PipelineError, "mechanical_reference_sha256"),
        ):
            promote(self.root, plan, mechanical_reference=self.reference)
        self.assertEqual(fresh.call_args.kwargs["mechanical_reference"], self.reference)
        pushed.assert_not_called()
