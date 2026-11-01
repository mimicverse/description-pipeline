"""Consumer isolation, fixed wheel origins and public installed API calls."""

from copy import deepcopy
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

from description_pipeline.simulation.schemas import load_runtime_contract
from description_pipeline.simulation.runtime import validate_runtime_identity

ROOT = Path(__file__).resolve().parents[2]
CONTRACT = ROOT / "src/description_pipeline/simulation/teleop-runtime.json"
INSTALLER = ROOT / "scripts/install_teleop_worker.py"
PICO = os.environ.get("TELEOP_PICO_PYTHON")
ASSESSMENT = os.environ.get("TELEOP_ASSESSMENT_PYTHON")


class TeleopRuntimeTests(unittest.TestCase):
    def test_role_locks_match_each_selected_closure_and_fixed_root_assets(self):
        for role in ("pico", "assessment"):
            with self.subTest(role=role):
                selected = load_runtime_contract(CONTRACT, consumer=role)
                lines = (ROOT / f"requirements/teleop-{role}-py312.lock").read_text().splitlines()
                parsed = [
                    re.fullmatch(r"([a-z0-9-]+)(\[[a-z]+\])?==([^ ]+) --hash=sha256:([0-9a-f]{64})", line)
                    for line in lines
                    if line and not line.startswith("#")
                ]
                self.assertTrue(all(parsed))
                self.assertEqual(selected["dependency_versions"], {m[1]: m[3] for m in parsed})
                root = next(m for m in parsed if m[1] == "robot-teleop")
                self.assertEqual(selected["wheel_sha256"], root[4])
                self.assertEqual("[evaluation]" if role == "assessment" else None, root[2])
                if role == "assessment":
                    asset = next(m for m in parsed if m[1] == "actuator-model")
                    self.assertEqual("[mujoco]", asset[2])
                    self.assertEqual(selected["actuator_model"]["wheel_sha256"], asset[4])

    def test_invalid_or_flat_contracts_do_not_create_consumer_fallbacks(self):
        original = json.loads(CONTRACT.read_bytes())
        invalid = [load_runtime_contract(CONTRACT, consumer="pico")]
        for mutate in (
            lambda d: d["consumers"]["pico"]["dependency_versions"].update({"mujoco": "3.10.0"}),
            lambda d: d["consumers"]["assessment"].update({"actuator_model": None}),
            lambda d: d["common"]["files"].update({"teleop/../outside.py": "a" * 64}),
            lambda d: d["common"].update({"wheel_sha256": "invalid"}),
        ):
            value = deepcopy(original)
            mutate(value)
            invalid.append(value)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.json"
            for value in invalid:
                path.write_text(json.dumps(value))
                with self.assertRaises(ValueError):
                    load_runtime_contract(path, consumer="pico")
        with self.assertRaises(ValueError):
            load_runtime_contract(CONTRACT, consumer="worker")

    def test_installer_rejects_retired_or_mismatched_roles_before_creating_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "environment"
            for args in (
                [],
                ["--role", "worker"],
                ["--role", "assessment"],
                ["--role", "assessment", "--wheel", "missing.whl"],
                ["--role", "pico", "--wheel", "missing.whl", "--actuator-wheel", "missing.whl"],
                ["--role", "pico-link", "--wheel", "missing.whl"],
            ):
                with self.subTest(args=args):
                    result = subprocess.run(
                        [sys.executable, "-B", str(INSTALLER), *args, "--destination", str(destination)],
                        capture_output=True,
                        timeout=10,
                    )
                    self.assertNotEqual(0, result.returncode)
                    self.assertFalse(destination.exists())
            selected = load_runtime_contract(CONTRACT, consumer="pico")
            wheel = Path(directory) / selected["wheel_filename"]
            wheel.write_bytes(b"altered wheel")
            result = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(INSTALLER),
                    "--role",
                    "pico",
                    "--wheel",
                    str(wheel),
                    "--destination",
                    str(destination),
                ],
                capture_output=True,
                timeout=10,
            )
            self.assertNotEqual(0, result.returncode)
            self.assertFalse(destination.exists())

    def inspect_installed(self, role, python):
        contract = load_runtime_contract(CONTRACT, consumer=role)
        result = subprocess.run(
            [python, "-I", "-B", str(ROOT / "src/description_pipeline/simulation/runtime.py")],
            input=json.dumps(list(contract["dependency_versions"])),
            text=True,
            capture_output=True,
            timeout=30,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        raw = json.loads(result.stdout)
        identity = validate_runtime_identity(raw, contract)
        bootstrap = (
            "import runpy,json,sys;api=runpy.run_path(sys.argv[1]);"
            "print(json.dumps(api['verify_runtime_contract'](json.load(sys.stdin))))"
        )
        checked = subprocess.run(
            [python, "-I", "-B", "-c", bootstrap, str(ROOT / "src/description_pipeline/simulation/runtime.py")],
            input=json.dumps(contract),
            text=True,
            capture_output=True,
            timeout=30,
        )
        self.assertEqual(0, checked.returncode, checked.stderr)
        self.assertEqual(identity, json.loads(checked.stdout))
        altered = deepcopy(raw)
        altered["packages"]["robot-teleop"]["archive_sha256"] = "0" * 64
        with self.assertRaises(ValueError):
            validate_runtime_identity(altered, contract)
        if role == "assessment":
            conflicting = deepcopy(raw)
            conflicting["actuator_model"]["git_commit"] = "0" * 40
            with self.assertRaisesRegex(ValueError, "commit conflicts"):
                validate_runtime_identity(conflicting, contract)
        return identity

    @unittest.skipUnless(PICO, "TELEOP_PICO_PYTHON is required")
    def test_real_pico_closure_and_protocol_replay_after_source_relocation(self):
        from tests.v1.test_pico_sessions import model_source

        identity = self.inspect_installed("pico", PICO)
        self.assertEqual("3.13.0", identity["engine"])
        self.assertIsNone(identity["actuator_model"])
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source"
            source.mkdir()
            profile, files = model_source(source)
            config = {
                "protocol": "teleop.upper-body.worker/1",
                "session_id": "role-probe",
                "mode": "kinematic_ik",
                "root": str(source),
                "inventory": files,
                "entry": profile["mjcf"],
                "profile": profile,
                "position_scale": 0.1,
                "joint_speed_radps": 1,
                "record_dir": str(Path(directory) / "records"),
            }
            result = subprocess.run(
                [PICO, "-I", "-B", str(ROOT / "tests/v1/teleop_consumer_probe.py"), "pico"],
                input=json.dumps(config),
                text=True,
                capture_output=True,
                timeout=30,
            )
            self.assertEqual(0, result.returncode, result.stderr)
            proof = json.loads(result.stdout)
            self.assertTrue(proof["replay"]["pass"])
            self.assertGreater(proof["replay"]["records_checked"], 0)
            self.assertTrue(proof["archive_unchanged"])

    @unittest.skipUnless(ASSESSMENT, "TELEOP_ASSESSMENT_PYTHON is required")
    def test_real_assessment_closure_public_controller_and_delay(self):
        identity = self.inspect_installed("assessment", ASSESSMENT)
        self.assertEqual("3.10.0", identity["engine"])
        self.assertEqual({"version": "1.3.0", "git_commit": None, "editable": False}, identity["actuator_model"])
        result = subprocess.run(
            [ASSESSMENT, "-I", "-B", str(ROOT / "tests/v1/teleop_consumer_probe.py"), "assessment"],
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(8, json.loads(result.stdout)["ticks"])
