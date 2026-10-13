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
from tools.verify_distribution import safe_members, verify_release

VERSION = "1.4.0"


def complete_release(root: Path, *, identity: dict | None = None) -> dict:
    """A minimal complete release: the five canonical assets plus release.json."""

    identity = dict(identity or {"source_commit": "a" * 40, **release_domain_identity()})
    identity.setdefault("version", VERSION)
    identity.setdefault("schema_version", "solidworks-to-urdf.release/v1")
    identity.setdefault("pipeline_id", "solidworks-to-urdf")
    identity.setdefault("package_files", {})
    identity.setdefault("source_sha256", hashlib.sha256(b"wheel bytes").hexdigest())
    wheel = root / f"mimicverse_description-{VERSION}-py3-none-any.whl"
    wheel.write_bytes(b"wheel bytes")
    deployment_members = {
        "requirements/linux-py312.lock": b"linux lock",
        "requirements/windows-py312.lock": b"windows lock",
        "deploy/operator/operatorctl.sh": b"operator tool",
    }
    identity["deployment_files"] = {
        name: hashlib.sha256(payload).hexdigest() for name, payload in deployment_members.items()
    }
    identity["runtime_locks"] = {
        "linux": identity["deployment_files"]["requirements/linux-py312.lock"],
        "windows": identity["deployment_files"]["requirements/windows-py312.lock"],
    }
    with tarfile.open(root / f"mimicverse_description-{VERSION}.tar.gz", "w:gz") as archive:
        payload = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
        member = tarfile.TarInfo(f"mimicverse_description-{VERSION}/src/description_pipeline/tool-release.json")
        member.size = len(payload)
        archive.addfile(member, io.BytesIO(payload))
    with zipfile.ZipFile(root / f"description-{VERSION}-deployment.zip", "w") as archive:
        for name, payload in deployment_members.items():
            archive.writestr(name, payload)
    for platform in ("linux", "windows"):
        with zipfile.ZipFile(root / f"description-{VERSION}-{platform}-cp312-x86_64.zip", "w") as archive:
            archive.writestr(f"wheels/{wheel.name}", wheel.read_bytes())
    write_json(root / "release.json", {**identity, "artifacts": inventory(root)})
    return identity


class DistributionTests(unittest.TestCase):
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
                with zipfile.ZipFile(archive_path, "w") as archive:
                    archive.writestr(f"wheels/mimicverse_description-{VERSION}-py3-none-any.whl", b"another wheel")
                write_json(
                    root / "release.json", {**identity, "artifacts": inventory(root, exclude=("release.json",))}
                )
                with self.assertRaises(PipelineError) as raised:
                    verify_release(root, identity)
                self.assertIn("another wheel", str(raised.exception))

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
                    return_value=SimpleNamespace(PipelineError=PipelineError, SUPPORTED_PYTHON=">=3.12,<3.13"),
                ),
                patch.object(build_release, "sys", SimpleNamespace(version_info=(3, 11, 9))),
                self.assertRaisesRegex(PipelineError, "CPython 3.12"),
            ):
                build_release.build(root, output)
            with (
                patch.object(
                    build_release,
                    "bind_source_closure",
                    return_value=SimpleNamespace(PipelineError=PipelineError, SUPPORTED_PYTHON=">=3.12,<3.13"),
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
