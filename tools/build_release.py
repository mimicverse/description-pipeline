"""Build deterministic, source-bound distributions from a clean committed checkout."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

from packaging.utils import parse_wheel_filename
from packaging.requirements import Requirement

from description_pipeline import __version__
from description_pipeline.build.archive import normalize_sdist, normalize_zip, write_zip
from description_pipeline.delivery import PIPELINE_ID
from description_pipeline.io import PipelineError, digest, file_digest, inventory, write_json

BUILD_PACKAGES = {"setuptools", "wheel", "build"}


def locked_builders(root):
    versions = {}
    for line in (root / "requirements/build-py312.lock").read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        requirement = Requirement(line)
        if requirement.name in BUILD_PACKAGES:
            (spec,) = requirement.specifier
            if spec.operator != "==":
                raise PipelineError("Release builders must use exact versions")
            versions[requirement.name] = spec.version
    if set(versions) != BUILD_PACKAGES:
        raise PipelineError("Builder lock is incomplete")
    return versions


def git(root, *arguments):
    return subprocess.check_output(["git", *arguments], cwd=root, text=True).strip()


def offline_bundle(source, output, wheel, platform):
    """Include exactly the resolved target wheels and a SHA-256 installation lock."""
    with tempfile.TemporaryDirectory(prefix="description-offline-") as temporary:
        staging = Path(temporary)
        wheels = staging / "wheels"
        arguments = [sys.executable, "-m", "pip", "download", "--only-binary=:all:", "--dest", str(wheels)]
        if platform == "windows":
            arguments += [
                "--platform",
                "win_amd64",
                "--python-version",
                "3.12",
                "--implementation",
                "cp",
                "--abi",
                "cp312",
            ]
        arguments += ["-r", str(source / f"requirements/{platform}-py312.lock")]
        subprocess.run(arguments, check=True)
        shutil.copyfile(wheel, wheels / wheel.name)
        locked = []
        for path in sorted(wheels.glob("*.whl")):
            name, version, _, _ = parse_wheel_filename(path.name)
            locked.append(f"{name}=={version} --hash=sha256:{file_digest(path)}")
        (staging / "requirements.lock").write_text("\n".join(locked) + "\n", encoding="utf-8")
        (staging / "README.md").write_text(
            "# Offline installation\n\nRequires CPython 3.12, x86_64. Create a dedicated virtual environment, "
            "then run its Python from this directory:\n\n"
            "```sh\npython -m pip install --no-index --require-hashes --find-links wheels -r requirements.lock\n"
            "description doctor\n```\n\nFollow docs/deployment.md in the matching deployment archive.\n",
            encoding="utf-8",
        )
        write_json(staging / "files.json", inventory(staging))
        target = output / f"description-{__version__}-{platform}-cp312-x86_64.zip"
        write_zip(target, ((name, (staging / name).read_bytes()) for name in inventory(staging)))
        return target


def build(root, output, *, offline=False):
    root, output = root.resolve(), output.resolve()
    if sys.version_info[:2] != (3, 12):
        raise PipelineError("Release builds require CPython 3.12")
    if git(root, "status", "--porcelain", "--untracked-files=normal"):
        raise PipelineError("Release builds require a clean committed checkout")
    if output.exists() and any(output.iterdir()):
        raise PipelineError("Use an empty release output directory")
    builders = locked_builders(root)
    for name, expected in builders.items():
        if importlib.metadata.version(name) != expected:
            raise PipelineError(f"Install requirements/build-py312.lock: {name} must be {expected}")
    output.mkdir(parents=True, exist_ok=True)
    commit = git(root, "rev-parse", "HEAD")
    with tempfile.TemporaryDirectory(prefix="description-release-") as temporary:
        staging = Path(temporary)
        archive = staging / "source.tar"
        subprocess.run(["git", "archive", "--format=tar", "--output", str(archive), commit], cwd=root, check=True)
        source = staging / "source"
        source.mkdir()
        with tarfile.open(archive) as handle:
            handle.extractall(source, filter="data")
        package = source / "src/description_pipeline"
        files = inventory(package)
        identity = {
            "schema_version": "solidworks-to-urdf.release/v1",
            "pipeline_id": PIPELINE_ID,
            "version": __version__,
            "source_commit": commit,
            "source_sha256": digest(files),
            "package_files": files,
            "builder": {"python": sys.version.split()[0], **builders},
        }
        write_json(package / "tool-release.json", identity)
        subprocess.run(
            [sys.executable, "-m", "build", "--no-isolation", "--outdir", str(output), str(source)], check=True
        )
        (wheel,) = output.glob("*.whl")
        (sdist,) = output.glob("*.tar.gz")
        normalize_zip(wheel)
        normalize_sdist(sdist)
        deployment = output / f"description-{__version__}-deployment.zip"
        resources = [
            path
            for directory in ("deploy", "docs", "requirements")
            for path in (source / directory).rglob("*")
            if path.is_file()
        ]
        resources += [
            source / name
            for name in ("README.md", "LICENSE", "CODE_OF_CONDUCT.md", "CONTRIBUTING.md", "RELEASING.md")
        ]
        write_zip(deployment, ((path.relative_to(source).as_posix(), path.read_bytes()) for path in resources))
        if offline:
            for platform in ("linux", "windows"):
                offline_bundle(source, output, wheel, platform)
        write_json(output / "release.json", {**identity, "artifacts": inventory(output)})
        artifacts = inventory(output)
        write_json(output / "SHA256SUMS.json", artifacts)
    return {
        "passed": True,
        "source_commit": commit,
        "source_sha256": identity["source_sha256"],
        "artifacts": artifacts,
        "checksum_manifest_sha256": file_digest(output / "SHA256SUMS.json"),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--offline", action="store_true", help="also bundle the locked Linux and Windows wheels")
    args = parser.parse_args()
    result = build(Path(__file__).resolve().parent.parent, args.output, offline=args.offline)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
