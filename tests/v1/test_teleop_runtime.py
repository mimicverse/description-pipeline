"""Consumer isolation, fixed wheel origins and public installed API calls."""

from copy import deepcopy
import base64
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid

from description_pipeline.simulation.runtime import load_runtime_contract
from description_pipeline.simulation import runtime as runtime_helper
from description_pipeline.simulation.runtime import validate_runtime_identity
from description_pipeline import owned_process
from description_pipeline.simulation.recording import runtime_command, verification_execution

ROOT = Path(__file__).resolve().parents[2]
CONTRACT = Path(runtime_helper.__file__).with_name("teleop-runtime.json")
INSTALLER = ROOT / "scripts/install_teleop_worker.py"
PICO = os.environ.get("TELEOP_PICO_PYTHON")
ASSESSMENT = os.environ.get("TELEOP_ASSESSMENT_PYTHON")


class TeleopRuntimeTests(unittest.TestCase):
    def owned_probe(self, python, execution):
        nonce = uuid.uuid4().hex
        process = subprocess.Popen(
            runtime_command(Path(python), nonce, execution),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1"},
        )
        identity = {"pid": process.pid, "nonce": nonce}
        try:
            identity = owned_process.process_identity(process.pid, nonce)
            stdout, stderr = process.communicate(json.dumps(execution), timeout=30)
        finally:
            self.assertTrue(owned_process.terminate_owned(identity, reap=process.poll))
            process.wait(timeout=5)
        self.assertEqual("dead", owned_process.resource_state(identity))
        return {
            "returncode": process.returncode,
            "stdout": stdout,
            "stderr": stderr,
            "process": identity,
            "group": "dead",
        }

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
            lambda d: d["consumers"]["assessment"]["actuator_model"].pop("record_payload_sha256"),
            lambda d: d["consumers"]["assessment"]["actuator_model"].update({"record_payload_sha256": "invalid"}),
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
        execution = verification_execution(consumer=role)
        checked = self.owned_probe(python, execution)
        self.assertEqual(0, checked["returncode"], checked["stdout"] + checked["stderr"])
        identity = json.loads(checked["stdout"])
        raw = {**identity, "teleop_files": contract["files"]}
        self.assertEqual(identity, validate_runtime_identity(raw, contract))
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

    @unittest.skipUnless(ASSESSMENT, "TELEOP_ASSESSMENT_PYTHON is required")
    def test_real_assessment_rejects_coherent_record_stale_record_shadow_and_archive(self):
        original = Path(ASSESSMENT).parent.parent
        site = original / "lib/python3.12/site-packages"
        package = site / "actuator_model"
        dist = next(site.glob("actuator_model-*.dist-info"))

        def inventory():
            return {
                path.relative_to(site).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                for directory in (package, dist)
                for path in directory.rglob("*")
                if path.is_file() and "__pycache__" not in path.parts
            }

        before = inventory()
        self.evidence = {"original_inventory": before, "cases": {}}
        execution = verification_execution(consumer="assessment")
        with tempfile.TemporaryDirectory() as directory:
            private = Path(directory) / "environment"

            def copy_member(source, destination):
                path = Path(source)
                if (
                    any(path.is_relative_to(p) for p in (package, dist))
                    or path.stat().st_dev != Path(destination).parent.stat().st_dev
                ):
                    return shutil.copy2(source, destination)
                return os.link(source, destination)

            shutil.copytree(original, private, symlinks=True, copy_function=copy_member)
            private_site = private / site.relative_to(original)
            private_dist = private_site / dist.name
            parameter = private_site / "actuator_model/_parameters/hd1910/sampled_pd_v1_20261019/m5.json"
            record, direct = private_dist / "RECORD", private_dist / "direct_url.json"
            originals = {path: path.read_bytes() for path in (parameter, record, direct)}
            for path in originals:
                self.assertNotEqual(path.stat().st_ino, (original / path.relative_to(private)).stat().st_ino)

            def probe(name, error=None):
                observed = self.owned_probe(private / "bin/python", execution)
                self.evidence["cases"][name] = observed
                if error is None:
                    self.assertEqual(0, observed["returncode"], observed)
                else:
                    self.assertNotEqual(0, observed["returncode"], observed)
                    self.assertRegex(json.loads(observed["stdout"])["error"], error)

            def coherent_record(path):
                rows = list(csv.reader(io.StringIO(originals[record].decode())))
                row = next(row for row in rows if row[0] == path.relative_to(private_site).as_posix())
                data = path.read_bytes()
                row[1:] = [
                    "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode().rstrip("="),
                    str(len(data)),
                ]
                output = io.StringIO(newline="")
                csv.writer(output).writerows(rows)
                record.write_text(output.getvalue())

            probe("baseline")
            value = json.loads(originals[parameter])
            value["kt"] *= 1.125
            parameter.write_text(json.dumps(value, indent=2) + "\n")
            self.evidence["parameter_original_sha256"] = hashlib.sha256(originals[parameter]).hexdigest()
            self.evidence["parameter_altered_sha256"] = hashlib.sha256(parameter.read_bytes()).hexdigest()
            probe("stale_record", "bytes differ from RECORD")
            coherent_record(parameter)
            self.assertEqual(originals[direct], direct.read_bytes())
            probe("coherent_record", "artifact differs from the fixed consumer asset")
            parameter.write_bytes(originals[parameter])
            record.write_bytes(originals[record])
            origin = json.loads(originals[direct])
            origin["archive_info"] = {"hashes": {"sha256": "0" * 64}}
            direct.write_text(json.dumps(origin))
            coherent_record(direct)
            probe("wrong_archive", "archive differs from the fixed consumer asset")
            direct.write_bytes(originals[direct])
            record.write_bytes(originals[record])
            foreign = private / "foreign"
            shutil.copytree(private_site / "actuator_model", foreign / "actuator_model")
            (private_site / "w3_shadow_probe.pth").write_text(f"import sys; sys.path.insert(0, {str(foreign)!r})\n")
            probe("module_shadow", "module file is outside the selected distribution root")
        self.assertEqual(before, inventory())
        self.evidence["original_unchanged"] = True

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
                [PICO, "-I", "-B", str(ROOT / "tests/v1/teleop_consumer_probe.py"), "pico", owned_process.__file__],
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
            [
                ASSESSMENT,
                "-I",
                "-B",
                str(ROOT / "tests/v1/teleop_consumer_probe.py"),
                "assessment",
                owned_process.__file__,
            ],
            input=json.dumps(identity),
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(8, json.loads(result.stdout)["ticks"])
