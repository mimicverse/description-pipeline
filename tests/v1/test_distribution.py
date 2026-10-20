"""Adversarial archive paths and changed release assets must fail before installation."""

from __future__ import annotations

import hashlib
import io
import json
import re
import shutil
import sys
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path

from description_pipeline.interface_records import release_domain_identity
from description_pipeline.io import PipelineError, inventory, write_json
from tools.verify_distribution import _locked_pins, safe_members, validate_deployment_boundary, verify_release

VERSION = "1.4.0"
ROOT = Path(__file__).resolve().parents[2]


def complete_release(root: Path, *, identity: dict | None = None) -> dict:
    """A minimal complete release: the five canonical assets plus release.json.

    The assets are shaped like the real ones: the source archive carries every
    deployment input, and each role archive carries exactly its locked wheel set
    with a hash-locked requirements.lock.
    """

    identity = dict(identity or {"source_commit": "a" * 40, **release_domain_identity()})
    identity.setdefault("version", VERSION)
    identity.setdefault("schema_version", "solidworks-to-urdf.release/v1")
    identity.setdefault("pipeline_id", "solidworks-to-urdf")
    identity.setdefault("package_files", {})
    identity.setdefault("source_sha256", hashlib.sha256(b"wheel bytes").hexdigest())
    test_members = {"tests/v1/test_demo.py": b"def test_demo():\n    assert True\n"}
    identity["test_files"] = {
        name: hashlib.sha256(payload).hexdigest() for name, payload in test_members.items()
    }
    wheel = root / f"mimicverse_description-{VERSION}-py3-none-any.whl"
    wheel.write_bytes(b"wheel bytes")
    demo_wheel = b"demo wheel bytes"
    deployment_members = {
        "deploy/airflow/requirements.lock": b"demo==1.0\n",
        "requirements/linux-py312.lock": b"demo==1.0\n",
        "requirements/windows-py312.lock": b"demo==1.0\n",
        "deploy/operator/operatorctl.sh": b"operator tool",
        "deploy/operator/example.py": b"value = 1\n",
    }
    identity["deployment_files"] = {
        name: hashlib.sha256(payload).hexdigest() for name, payload in deployment_members.items()
    }
    identity["runtime_locks"] = {
        "linux": identity["deployment_files"]["requirements/linux-py312.lock"],
        "windows": identity["deployment_files"]["requirements/windows-py312.lock"],
    }
    with tarfile.open(root / f"mimicverse_description-{VERSION}.tar.gz", "w:gz") as archive:
        def add(member_name: str, payload: bytes) -> None:
            member = tarfile.TarInfo(member_name)
            member.size = len(payload)
            archive.addfile(member, io.BytesIO(payload))

        add(
            f"mimicverse_description-{VERSION}/src/description_pipeline/tool-release.json",
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode(),
        )
        for member_name, payload in deployment_members.items():
            add(f"mimicverse_description-{VERSION}/{member_name}", payload)
        for member_name, payload in test_members.items():
            add(f"mimicverse_description-{VERSION}/{member_name}", payload)
    with zipfile.ZipFile(root / f"description-{VERSION}-deployment.zip", "w") as archive:
        for name, payload in deployment_members.items():
            archive.writestr(name, payload)
    product_line = f"mimicverse-description=={VERSION} --hash=sha256:{hashlib.sha256(wheel.read_bytes()).hexdigest()}"
    demo_line = f"demo==1.0 --hash=sha256:{hashlib.sha256(demo_wheel).hexdigest()}"
    for platform in ("linux", "windows"):
        members = {
            f"wheels/{wheel.name}": wheel.read_bytes(),
            "wheels/demo-1.0-py3-none-any.whl": demo_wheel,
            "requirements.lock": f"{product_line}\n{demo_line}\n".encode(),
        }
        members["files.json"] = json.dumps(
            {name: hashlib.sha256(payload).hexdigest() for name, payload in members.items()}, sort_keys=True
        ).encode()
        with zipfile.ZipFile(root / f"description-{VERSION}-{platform}-cp312-x86_64.zip", "w") as archive:
            for name, payload in members.items():
                archive.writestr(name, payload)
    write_json(root / "release.json", {**identity, "artifacts": inventory(root)})
    return identity


class DistributionTests(unittest.TestCase):
    def test_hash_bearing_lock_lines_bind_the_package_version_only(self):
        self.assertEqual(
            _locked_pins(b"packaging==26.3 --hash=sha256:" + b"a" * 64 + b"\n"),
            {"packaging": "26.3"},
        )

    def test_ci_installs_the_linux_runner_from_its_airflow_lock(self):
        workflow = (ROOT / ".github/workflows/verify.yml").read_text(encoding="utf-8")
        self.assertIn(
            "--runtime-lock deploy/airflow/requirements.lock",
            workflow,
        )
        self.assertEqual(
            workflow.count("--runtime-lock requirements/linux-py312.lock"),
            0,
        )

    def test_windows_paths_and_case_collisions_are_rejected_on_linux(self):
        for names in (["../outside"], ["..\\..\\outside"], ["C:\\outside"], ["/outside"], ["a.py", "A.py"]):
            with self.subTest(names=names), self.assertRaises(PipelineError):
                safe_members(names)

    def test_release_checks_every_asset_and_its_source_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            identity = complete_release(root)
            verify_release(root, identity)
            with self.assertRaises(PipelineError):
                verify_release(root, {**identity, "source_commit": "b" * 40})
            with zipfile.ZipFile(root / f"description-{VERSION}-deployment.zip", "a") as archive:
                archive.writestr("docs/unknown.md", "extra\n")
            with self.assertRaises(PipelineError):
                verify_release(root, identity)

    def test_the_manifest_is_always_required(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            identity = complete_release(root)
            (root / "release.json").unlink()
            with self.assertRaises(PipelineError) as raised:
                verify_release(root, identity)
            self.assertIn("manifest", str(raised.exception))

    def test_legacy_second_manifest_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            identity = complete_release(root)
            write_json(root / "SHA256SUMS.json", {})
            with self.assertRaises(PipelineError) as raised:
                verify_release(root, identity)
            self.assertIn("SHA256SUMS.json", str(raised.exception))

    def test_the_complete_release_set_is_required(self):
        assets = (
            f"mimicverse_description-{VERSION}-py3-none-any.whl",
            f"mimicverse_description-{VERSION}.tar.gz",
            f"description-{VERSION}-deployment.zip",
            f"description-{VERSION}-linux-cp312-x86_64.zip",
            f"description-{VERSION}-windows-cp312-x86_64.zip",
        )
        for missing in assets:
            with tempfile.TemporaryDirectory() as directory, self.subTest(missing=missing):
                root = Path(directory)
                identity = complete_release(root)
                (root / missing).unlink()
                write_json(
                    root / "release.json", {**identity, "artifacts": inventory(root, exclude=("release.json",))}
                )
                with self.assertRaises(PipelineError) as raised:
                    verify_release(root, identity)
                self.assertIn("incomplete", str(raised.exception))

    def test_byte_tampered_asset_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            identity = complete_release(root)
            wheel = root / f"mimicverse_description-{VERSION}-py3-none-any.whl"
            wheel.write_bytes(wheel.read_bytes() + b"tampered")
            with self.assertRaises(PipelineError) as raised:
                verify_release(root, identity)
            self.assertIn("Release assets differ", str(raised.exception))

    def test_a_manifest_registered_seventh_asset_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            identity = complete_release(root)
            extra = root / "unexpected-review-note.txt"
            extra.write_text("A seventh asset with a valid manifest digest.\n", encoding="utf-8")
            manifest = json.loads((root / "release.json").read_text(encoding="utf-8"))
            manifest["artifacts"][extra.name] = hashlib.sha256(extra.read_bytes()).hexdigest()
            write_json(root / "release.json", manifest)
            with self.assertRaises(PipelineError) as raised:
                verify_release(root, identity)
            self.assertIn("outside the canonical five", str(raised.exception))

    def test_an_unregistered_seventh_file_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            identity = complete_release(root)
            (root / "unexpected-review-note.txt").write_text("seventh file\n", encoding="utf-8")
            with self.assertRaises(PipelineError) as raised:
                verify_release(root, identity)
            self.assertIn("Release assets differ", str(raised.exception))

    def test_deployment_members_are_bound_to_the_source_release(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            identity = complete_release(root)
            archive_path = root / f"description-{VERSION}-deployment.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("requirements/linux-py312.lock", b"linux lock")
                archive.writestr("requirements/windows-py312.lock", b"windows lock")
                archive.writestr("deploy/operator/operatorctl.sh", b"sneaky source note\n")
            write_json(root / "release.json", {**identity, "artifacts": inventory(root, exclude=("release.json",))})
            with self.assertRaises(PipelineError) as raised:
                verify_release(root, identity)
            self.assertIn("Deployment members", str(raised.exception))

    def test_role_archives_carry_the_release_wheel(self):
        for platform in ("linux", "windows"):
            with tempfile.TemporaryDirectory() as directory, self.subTest(platform=platform):
                root = Path(directory)
                identity = complete_release(root)
                archive_path = root / f"description-{VERSION}-{platform}-cp312-x86_64.zip"
                with zipfile.ZipFile(archive_path) as source:
                    members = {name: source.read(name) for name in source.namelist()}
                members[f"wheels/mimicverse_description-{VERSION}-py3-none-any.whl"] = b"another wheel"
                with zipfile.ZipFile(archive_path, "w") as archive:
                    for name, payload in members.items():
                        archive.writestr(name, payload)
                write_json(
                    root / "release.json", {**identity, "artifacts": inventory(root, exclude=("release.json",))}
                )
                with self.assertRaises(PipelineError) as raised:
                    verify_release(root, identity)
                self.assertIn("another wheel", str(raised.exception))

    def test_role_locks_must_pin_exactly_their_wheel_set(self):
        for platform in ("linux", "windows"):
            with tempfile.TemporaryDirectory() as directory, self.subTest(platform=platform):
                root = Path(directory)
                identity = complete_release(root)
                archive_path = root / f"description-{VERSION}-{platform}-cp312-x86_64.zip"
                with zipfile.ZipFile(archive_path) as source:
                    members = {name: source.read(name) for name in source.namelist()}
                product = f"wheels/mimicverse_description-{VERSION}-py3-none-any.whl"
                wheel_digest = hashlib.sha256(members[product]).hexdigest()
                members["requirements.lock"] = (
                    f"mimicverse-description=={VERSION} --hash=sha256:{wheel_digest}\n".encode()
                )
                with zipfile.ZipFile(archive_path, "w") as archive:
                    for name, payload in members.items():
                        archive.writestr(name, payload)
                write_json(
                    root / "release.json", {**identity, "artifacts": inventory(root, exclude=("release.json",))}
                )
                with self.assertRaises(PipelineError) as raised:
                    verify_release(root, identity)
                self.assertIn("lock does not pin exactly", str(raised.exception))

    def test_role_locks_refuse_normalized_duplicates(self):
        for platform in ("linux", "windows"):
            with tempfile.TemporaryDirectory() as directory, self.subTest(platform=platform):
                root = Path(directory)
                identity = complete_release(root)
                archive_path = root / f"description-{VERSION}-{platform}-cp312-x86_64.zip"
                with zipfile.ZipFile(archive_path) as source:
                    members = {name: source.read(name) for name in source.namelist()}
                lines = members["requirements.lock"].decode().splitlines()
                corrupted = [f"demo==0.0 --hash=sha256:{'0' * 64}", *lines]
                members["requirements.lock"] = ("\n".join(corrupted) + "\n").encode()
                with zipfile.ZipFile(archive_path, "w") as archive:
                    for name, payload in members.items():
                        archive.writestr(name, payload)
                write_json(
                    root / "release.json", {**identity, "artifacts": inventory(root, exclude=("release.json",))}
                )
                with self.assertRaises(PipelineError) as raised:
                    verify_release(root, identity)
                self.assertIn("repeats demo", str(raised.exception))

    def test_role_wheels_must_match_the_archive_target(self):
        cases = {
            "windows_tag_on_linux": "demo-1.0-cp312-cp312-win_amd64.whl",
            "any_platform_with_cp_abi": "demo-1.0-cp312-cp312-any.whl",
            "abi3_with_generic_interpreter": "demo-1.0-py3-abi3-any.whl",
            "future_glibc_baseline": "demo-1.0-cp312-cp312-manylinux_999_999_x86_64.whl",
        }
        for label, member in cases.items():
            with tempfile.TemporaryDirectory() as directory, self.subTest(case=label):
                root = Path(directory)
                identity = complete_release(root)
                archive_path = root / f"description-{VERSION}-linux-cp312-x86_64.zip"
                with zipfile.ZipFile(archive_path) as source:
                    members = {name: source.read(name) for name in source.namelist()}
                members[f"wheels/{member}"] = members.pop("wheels/demo-1.0-py3-none-any.whl")
                members["files.json"] = json.dumps(
                    {
                        name: hashlib.sha256(payload).hexdigest()
                        for name, payload in members.items()
                        if name != "files.json"
                    },
                    sort_keys=True,
                ).encode()
                with zipfile.ZipFile(archive_path, "w") as archive:
                    for name, payload in members.items():
                        archive.writestr(name, payload)
                write_json(
                    root / "release.json", {**identity, "artifacts": inventory(root, exclude=("release.json",))}
                )
                with self.assertRaises(PipelineError) as raised:
                    verify_release(root, identity)
                self.assertIn("another target", str(raised.exception))

    def test_older_cpython_abi3_tags_stay_compatible(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            identity = complete_release(root)
            archive_path = root / f"description-{VERSION}-linux-cp312-x86_64.zip"
            with zipfile.ZipFile(archive_path) as source:
                members = {name: source.read(name) for name in source.namelist()}
            members["wheels/demo-1.0-cp311-abi3-manylinux_2_28_x86_64.whl"] = members.pop(
                "wheels/demo-1.0-py3-none-any.whl"
            )
            members["files.json"] = json.dumps(
                {
                    name: hashlib.sha256(payload).hexdigest()
                    for name, payload in members.items()
                    if name != "files.json"
                },
                sort_keys=True,
            ).encode()
            with zipfile.ZipFile(archive_path, "w") as archive:
                for name, payload in members.items():
                    archive.writestr(name, payload)
            write_json(root / "release.json", {**identity, "artifacts": inventory(root, exclude=("release.json",))})
            verify_release(root, identity)

    def test_deployment_inputs_must_match_the_source_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            identity = complete_release(root)
            archive_path = root / f"description-{VERSION}-deployment.zip"
            with zipfile.ZipFile(archive_path) as source:
                members = {name: source.read(name) for name in source.namelist()}
            members["deploy/operator/operatorctl.sh"] = b"sneaky source note\n"
            with zipfile.ZipFile(archive_path, "w") as archive:
                for name, payload in members.items():
                    archive.writestr(name, payload)
            identity = {**identity, "deployment_files": {
                name: hashlib.sha256(payload).hexdigest() for name, payload in members.items()
            }}
            # The private copy reseals the embedded identities too; the deployment
            # copies inside the source archive stay untouched.
            sdist = root / f"mimicverse_description-{VERSION}.tar.gz"
            with tarfile.open(sdist) as source:
                entries = [
                    (member, source.extractfile(member).read() if member.isfile() else None)
                    for member in source.getmembers()
                ]
            with tarfile.open(sdist, "w:gz") as target:
                for member, payload in entries:
                    if member.name.endswith("src/description_pipeline/tool-release.json"):
                        payload = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
                        member.size = len(payload)
                    target.addfile(member, io.BytesIO(payload) if payload is not None else None)
            write_json(root / "release.json", {**identity, "artifacts": inventory(root, exclude=("release.json",))})
            with self.assertRaises(PipelineError) as raised:
                verify_release(root, identity)
            self.assertIn("source archive differ", str(raised.exception))

    def test_source_archive_carries_the_exact_tracked_test_tree(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            identity = complete_release(root)
            sdist = root / f"mimicverse_description-{VERSION}.tar.gz"
            with tarfile.open(sdist) as source:
                entries = [
                    (member, source.extractfile(member).read() if member.isfile() else None)
                    for member in source.getmembers()
                ]
            with tarfile.open(sdist, "w:gz") as target:
                for member, payload in entries:
                    if member.name.endswith("tests/v1/test_demo.py"):
                        payload = b"def test_changed():\n    assert False\n"
                        member.size = len(payload)
                    target.addfile(member, io.BytesIO(payload) if payload is not None else None)
            write_json(root / "release.json", {**identity, "artifacts": inventory(root, exclude=("release.json",))})
            with self.assertRaisesRegex(PipelineError, "tracked test closure"):
                verify_release(root, identity)

    def test_deployment_rejects_ci_only_scripts_and_test_imports(self):
        for members in (
            {"tools/scheduled_smoke.py": b"pass\n"},
            {"deploy/example.py": b"from tests.v1 import fixture\n"},
            {"deploy/example.py": b"import tests.sources.support\n"},
        ):
            with self.subTest(members=members), self.assertRaises(PipelineError):
                validate_deployment_boundary(members)

    def test_the_manifest_binds_the_live_contract_and_support_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            identity = complete_release(root)
            artifacts = inventory(root, exclude=("release.json",))
            verify_release(root, identity)
            cases = [
                ("contract", "schema"),
                ("contract", "stage"),
                ("contract", "check_policy"),
                ("contract", "resource_policy"),
                ("support", "python"),
            ]
            for field, key in cases:
                broken = {**identity, field: {**identity[field], key: "0" * 64}}
                with self.subTest(field=field, key=key):
                    write_json(root / "release.json", {**broken, "artifacts": artifacts})
                    with self.assertRaises(PipelineError):
                        verify_release(root, broken)
            without_support = {key: value for key, value in identity.items() if key != "support"}
            write_json(root / "release.json", {**without_support, "artifacts": artifacts})
            with self.assertRaises(PipelineError):
                verify_release(root, without_support)

    def test_builder_binds_the_specified_source_closure(self):
        from tools import build_release

        root = Path(__file__).resolve().parents[2]
        version = re.search(
            r'__version__ = "([^"]+)"', (root / "src/description_pipeline/__init__.py").read_text()
        ).group(1)
        with tempfile.TemporaryDirectory() as directory:
            decoy = Path(directory) / "ambient"
            shutil.copytree(root / "src/description_pipeline", decoy / "description_pipeline")
            init = decoy / "description_pipeline/__init__.py"
            init.write_text(init.read_text().replace(f'__version__ = "{version}"', '__version__ = "0.0.0"'))
            saved = {
                name: module
                for name, module in sys.modules.items()
                if name == "description_pipeline" or name.startswith("description_pipeline.")
            }
            sys.path.insert(0, str(decoy))
            try:
                import importlib

                for name in [
                    name
                    for name in list(sys.modules)
                    if name == "description_pipeline" or name.startswith("description_pipeline.")
                ]:
                    del sys.modules[name]
                shadow = importlib.import_module("description_pipeline")
                self.assertEqual(shadow.__version__, "0.0.0")
                bound = build_release.bind_source_closure(root / "src")
                self.assertEqual(bound.version, version)
            finally:
                sys.path.remove(str(decoy))
                for name in [
                    name
                    for name in list(sys.modules)
                    if name == "description_pipeline" or name.startswith("description_pipeline.")
                ]:
                    del sys.modules[name]
                sys.modules.update(saved)

    def test_build_refuses_a_missing_runtime_or_unlocked_builders(self):
        from types import SimpleNamespace
        from unittest.mock import patch

        from tools import build_release

        root = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "out"
            with (
                patch.object(
                    build_release,
                    "bind_source_closure",
                    return_value=SimpleNamespace(
                        PipelineError=PipelineError, SUPPORTED_PYTHON=">=3.12,<3.13", TARGET_PYTHON="3.12"
                    ),
                ),
                patch.object(build_release, "sys", SimpleNamespace(version_info=(3, 11, 9))),
                self.assertRaisesRegex(PipelineError, "CPython 3.12"),
            ):
                build_release.build(root, output)
            with (
                patch.object(
                    build_release,
                    "bind_source_closure",
                    return_value=SimpleNamespace(
                        PipelineError=PipelineError, SUPPORTED_PYTHON=">=3.12,<3.13", TARGET_PYTHON="3.12"
                    ),
                ),
                patch.object(build_release, "git", return_value=""),
                patch.object(build_release.importlib.metadata, "version", return_value="0.0.0"),
                self.assertRaisesRegex(PipelineError, "Install requirements/build-py312.lock"),
            ):
                build_release.build(root, output)

    def test_bytecode_caches_are_not_part_of_the_source_closure(self):
        from types import SimpleNamespace

        from tools import build_release

        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory)
            (package / "module.py").write_text("value = 1\n", encoding="utf-8")
            (package / "__pycache__").mkdir()
            (package / "__pycache__" / "module.cpython-312.pyc").write_bytes(b"bytecode")
            self.assertEqual(
                build_release.source_inventory(package, SimpleNamespace(inventory=inventory)),
                {"module.py": hashlib.sha256(b"value = 1\n").hexdigest()},
            )
