"""Consumer archive closure tests; synthetic wheels never claim native execution."""

from __future__ import annotations

import base64
import copy
import csv
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile

from tools.release_consumers import locked_consumers, target_tags, verify_consumer_pool, wheel_record
from description_pipeline.simulation.runtime import load_runtime_contract
from description_pipeline.runtime import LINUX_PLATFORMS


def synthetic_wheel(name, version, *, files=None, requires=()):
    filename = name.replace("-", "_") + "-" + version + "-py3-none-any.whl"
    info = name.replace("-", "_") + "-" + version + ".dist-info/"
    members = dict(files or {})
    members[info + "METADATA"] = (
        "Metadata-Version: 2.1\nName: "
        + name
        + "\nVersion: "
        + version
        + "\n"
        + "".join("Requires-Dist: " + row + "\n" for row in requires)
    ).encode()
    members[info + "WHEEL"] = b"Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
    rows = io.StringIO()
    writer = csv.writer(rows)
    for path, value in members.items():
        sha = base64.urlsafe_b64encode(hashlib.sha256(value).digest()).rstrip(b"=").decode()
        writer.writerow((path, "sha256=" + sha, str(len(value))))
    writer.writerow((info + "RECORD", "", ""))
    members[info + "RECORD"] = rows.getvalue().encode()
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        for path, value in members.items():
            archive.writestr(path, value)
    return filename, stream.getvalue()


def consumer_fixture():
    members = {}
    teleop_files = {"teleop/__init__.py": b"# Synthetic archive contract fixture.\n"}
    for name, version, requires, files in (
        (
            "robot-teleop",
            "0.3.0",
            ["numpy==1.0", "mujoco>=3.10,<3.14", 'scipy==1.0; extra == "evaluation"'],
            teleop_files,
        ),
        ("actuator-model", "1.3.0", ['mujoco>=3.10,<3.11; extra == "mujoco"'], {}),
        ("mujoco", "3.13.0", [], {}),
        ("mujoco", "3.10.0", [], {}),
        ("numpy", "1.0", [], {}),
        ("scipy", "1.0", ["numpy==1.0"], {}),
        ("pyzmq", "1.0", [], {}),
        ("websockets", "1.0", [], {}),
    ):
        filename, payload = synthetic_wheel(name, version, files=files, requires=requires)
        members[filename] = payload
    common = {
        "source_commit": "a" * 40,
        "source_tree": "b" * 40,
        "wheel_filename": "robot_teleop-0.3.0-py3-none-any.whl",
        "files": {name: hashlib.sha256(value).hexdigest() for name, value in teleop_files.items()},
    }
    common["wheel_sha256"] = hashlib.sha256(members[common["wheel_filename"]]).hexdigest()
    asset = {"source_commit": "c" * 40, "wheel_filename": "actuator_model-1.3.0-py3-none-any.whl"}
    asset["wheel_sha256"] = hashlib.sha256(members[asset["wheel_filename"]]).hexdigest()
    versions = {
        "pico": {"robot-teleop": "0.3.0", "numpy": "1.0", "mujoco": "3.13.0"},
        "assessment": {
            "robot-teleop": "0.3.0",
            "numpy": "1.0",
            "mujoco": "3.10.0",
            "actuator-model": "1.3.0",
            "scipy": "1.0",
        },
        "pico-link": {"pyzmq": "1.0", "websockets": "1.0"},
    }
    locks = {}
    for role, pins in versions.items():
        rows = []
        for name, version in pins.items():
            extra = "[evaluation]" if role == "assessment" and name == "robot-teleop" else ""
            if name == "actuator-model":
                extra = "[mujoco]"
            filename = name.replace("-", "_") + "-" + version + "-py3-none-any.whl"
            rows.append(f"{name}{extra}=={version} --hash=sha256:{hashlib.sha256(members[filename]).hexdigest()}")
        locks[role] = ("\n".join(rows) + "\n").encode()
    contract = {
        "schema_version": "solidworks-to-urdf.teleop-runtime/v1",
        "common": common,
        "consumers": {
            role: {"dependency_versions": pins, "actuator_model": asset if role == "assessment" else None}
            for role, pins in versions.items()
            if role != "pico-link"
        },
    }
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "runtime.json"
        path.write_text(json.dumps(contract))
        contracts = {role: load_runtime_contract(path, consumer=role) for role in ("pico", "assessment")}
    return members, locks, contract, contracts


class ConsumerPoolTests(unittest.TestCase):
    def setUp(self):
        self.members, self.locks, self.contract, self.contracts = consumer_fixture()
        self.tags = target_tags(LINUX_PLATFORMS)

    def test_shared_pool_preserves_both_isolated_engine_versions_and_one_teleop(self):
        versions = verify_consumer_pool(self.members, self.locks, self.contracts, self.tags)
        self.assertEqual(versions["pico"]["mujoco"], "3.13.0")
        self.assertEqual(versions["assessment"]["mujoco"], "3.10.0")
        self.assertEqual(sum(name.startswith("robot_teleop-") for name in self.members), 1)

    def test_record_covers_files_while_empty_archive_directories_need_no_record(self):
        filename, payload = synthetic_wheel("example", "1.0", files={"example/__init__.py": b""})
        stream = io.BytesIO(payload)
        with zipfile.ZipFile(stream, "a") as archive:
            archive.writestr("example/", b"")
        wheel_record(filename, stream.getvalue(), self.tags)
        with zipfile.ZipFile(stream, "a") as archive:
            archive.writestr("example/unrecorded.py", b"value = 1\n")
        with self.assertRaisesRegex(ValueError, "RECORD"):
            wheel_record(filename, stream.getvalue(), self.tags)

    def test_missing_extra_foreign_and_altered_wheels_are_refused(self):
        for fault in ("missing", "extra", "foreign", "bytes"):
            members = dict(self.members)
            key = "numpy-1.0-py3-none-any.whl"
            if fault == "missing":
                members.pop(key)
            elif fault == "extra":
                members["extra-1.0-py3-none-any.whl"] = synthetic_wheel("extra", "1.0")[1]
            elif fault == "foreign":
                members["numpy-1.0-cp312-cp312-win_amd64.whl"] = members.pop(key)
            else:
                members[key] = b"changed archive"
            with self.subTest(fault=fault), self.assertRaises(ValueError):
                verify_consumer_pool(members, self.locks, self.contracts, self.tags)

    def test_nested_common_inventory_and_model_hashes_cannot_be_resealed_by_the_pool(self):
        for fault in ("inventory", "model", "role_hash"):
            contracts = copy.deepcopy(self.contracts)
            locks = dict(self.locks)
            if fault == "inventory":
                for contract in contracts.values():
                    contract["files"]["teleop/__init__.py"] = "a" * 64
            elif fault == "model":
                contracts["pico"]["wheel_sha256"] = "b" * 64
            else:
                locks["assessment"] = locks["assessment"].replace(
                    hashlib.sha256(self.members["numpy-1.0-py3-none-any.whl"]).hexdigest().encode(),
                    b"c" * 64,
                )
            with self.subTest(fault=fault), self.assertRaises(ValueError):
                verify_consumer_pool(self.members, locks, contracts, self.tags)

    def test_rehashed_wheel_metadata_must_still_close_over_source_locks(self):
        members = dict(self.members)
        filename, changed = synthetic_wheel("numpy", "1.0", requires=["unlocked==1.0"])
        old_sha = hashlib.sha256(members[filename]).hexdigest().encode()
        new_sha = hashlib.sha256(changed).hexdigest().encode()
        members[filename] = changed
        locks = {role: data.replace(old_sha, new_sha) for role, data in self.locks.items()}
        with self.assertRaisesRegex(ValueError, "unlocked dependency"):
            verify_consumer_pool(members, locks, self.contracts, self.tags)

    def test_required_consumer_extras_are_not_replaced_by_version_only_locks(self):
        locks = dict(self.locks)
        locks["assessment"] = locks["assessment"].replace(b"robot-teleop[evaluation]", b"robot-teleop")
        with self.assertRaisesRegex(ValueError, "extras"):
            locked_consumers(locks, self.contracts)

    def test_builder_requires_both_explicit_model_inputs_bound_to_the_common_contract(self):
        from types import SimpleNamespace

        from description_pipeline.io import PipelineError
        from tools.build_release import model_inputs
        from tools.release_consumers import CONSUMER_LOCKS

        bound = SimpleNamespace(
            load_runtime_contract=load_runtime_contract,
            PipelineError=PipelineError,
            LINUX_PLATFORMS=LINUX_PLATFORMS,
            TARGET_PYTHON="3.12",
            TARGET_ABI="cp312",
        )
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            path = source / "src/description_pipeline/simulation/teleop-runtime.json"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps(self.contract))
            for role, relative in CONSUMER_LOCKS.items():
                lock = source / relative
                lock.parent.mkdir(exist_ok=True)
                lock.write_bytes(self.locks[role])
            teleop = source / self.contracts["pico"]["wheel_filename"]
            actuator = source / self.contracts["assessment"]["actuator_model"]["wheel_filename"]
            for model in (teleop, actuator):
                model.write_bytes(self.members[model.name])
            self.assertEqual(set(model_inputs(source, bound, teleop, actuator)), {teleop.name, actuator.name})
            for inputs in ((None, actuator), (teleop, None)):
                with self.subTest(inputs=inputs), self.assertRaisesRegex(PipelineError, "explicit"):
                    model_inputs(source, bound, *inputs)
            teleop.write_bytes(b"changed model wheel")
            with self.assertRaisesRegex(PipelineError, "fixed consumer asset"):
                model_inputs(source, bound, teleop, actuator)


class InstallerCheckerTests(unittest.TestCase):
    def test_installer_executes_only_the_release_checker_snapshot_after_package_mutation(self):
        from scripts import install_teleop_worker as installer

        approved = (
            b"import json\n"
            b"def load_runtime_contract(path, *, consumer): return json.loads(path.read_bytes())\n"
            b"def verify_runtime_contract(contract): return {'checker': 'approved'}\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package, temporary = root / "package", root / "temporary"
            (package / "simulation").mkdir(parents=True)
            temporary.mkdir()
            contract_path = package / "simulation/teleop-runtime.json"
            contract_path.write_text(json.dumps({"scope": "software_checker_binding_only"}))
            marker = root / "unverified-module-executed"
            changed = (
                "from pathlib import Path\n"
                f"Path({str(marker)!r}).write_text('unverified')\n"
                "def verify_runtime_contract(contract): return {'checker': 'unverified'}\n"
            ).encode()
            checker = package / "simulation/runtime.py"
            checker.write_bytes(approved)
            identity = {
                "package_files": {
                    "simulation/teleop-runtime.json": hashlib.sha256(contract_path.read_bytes()).hexdigest(),
                    "simulation/runtime.py": hashlib.sha256(approved).hexdigest(),
                }
            }
            selected, snapshot = installer.installed_consumer(package, identity, "pico", temporary)
            checker.write_bytes(changed)
            command = [
                sys.executable,
                "-I",
                "-B",
                "-c",
                installer._CONSUMER_PROBE,
                str(snapshot),
                identity["package_files"]["simulation/runtime.py"],
            ]
            accepted = subprocess.run(command, input=json.dumps(selected), text=True, capture_output=True, timeout=10)
            self.assertEqual(accepted.returncode, 0, accepted.stderr)
            self.assertEqual(json.loads(accepted.stdout), {"checker": "approved"})
            self.assertFalse(marker.exists())
            snapshot.chmod(0o600)
            snapshot.write_bytes(changed)
            rejected = subprocess.run(command, input=json.dumps(selected), text=True, capture_output=True, timeout=10)
            self.assertNotEqual(rejected.returncode, 0)
            self.assertEqual(rejected.stdout, "")
            self.assertIn("snapshot differs from the release", rejected.stderr)
            self.assertFalse(marker.exists())
            with self.assertRaisesRegex(ValueError, "differs from the installed release"):
                installer.installed_consumer(package, identity, "pico", temporary)
            self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
