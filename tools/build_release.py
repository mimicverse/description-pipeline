"""Build a wheel/sdist carrying verifiable source identity; never publish from this script."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from packaging.utils import parse_wheel_filename

import description_pipeline
from description_pipeline.build import tool_identity
from description_pipeline.build.archive import normalize_sdist, normalize_zip, write_zip
from description_pipeline.io import PipelineError, file_digest, write_json
from description_pipeline.sources.solidworks.deploy import (
    export_deploy_resources,
    lock_requirements,
    resource_path,
)
from description_pipeline.sources.solidworks.deploy.build_bundle import build as build_worker_bundle

DEPLOY_RESOURCES = (
    "worker.ps1",
    "worker-host.example.json",
    "submit.ps1",
    "submit-host.example.json",
    "requirements/win-py312.lock",
    "build_bundle.py",
)
LINUX_RESOURCES = ("install.sh", "submit.sh")
#: The build tools whose version leaks into the artifact bytes (setuptools writes the wheel's
#: `WHEEL` generator line, and `RECORD` covers it).  A release built with a different builder is not
#: reproducible for anyone following RELEASING.md, so the environment is checked, not assumed.
BUILD_PACKAGES = ("setuptools", "wheel")


def locked_build_versions(root: Path) -> dict[str, str]:
    """The build-tool versions ``requirements/linux-py312.lock`` pins, in name order."""

    lock = root / "requirements" / "linux-py312.lock"
    wanted: dict[str, str] = {}
    for line in lock.read_text(encoding="utf-8").splitlines():
        name, separator, version = line.partition("==")
        if separator and name.strip() in BUILD_PACKAGES:
            wanted[name.strip()] = version.split()[0]
    return dict(sorted(wanted.items()))


def check_build_environment(root: Path) -> dict[str, str]:
    """Refuse to build a release with a builder the lock does not name."""

    builder: dict[str, str] = {}
    for name, expected in locked_build_versions(root).items():
        try:
            actual = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            raise PipelineError(
                f"The release build needs {name}=={expected}; install requirements/linux-py312.lock"
            ) from None
        if actual != expected:
            raise PipelineError(
                f"The release build needs {name}=={expected} but this environment has {actual}: the builder "
                "version is part of the artifact bytes, so install requirements/linux-py312.lock first"
            )
        builder[name] = actual
    return builder


def check_deploy_resources() -> dict:
    """Fail the release early when the packaged Windows resources are incomplete."""

    missing = []
    for relative in DEPLOY_RESOURCES:
        try:
            path = resource_path(relative)
        except FileNotFoundError:
            missing.append(relative)
            continue
        if not path.is_file() or path.stat().st_size == 0:
            missing.append(relative)
    if missing:
        raise PipelineError(f"Deployment resources missing from the package: {missing}")
    return {"resources": list(DEPLOY_RESOURCES), "requirements": lock_requirements()}


def linux_resource_paths(root: Path) -> dict[str, Path]:
    """Resolve the two user-facing Linux launchers from this exact checkout."""

    directory = Path(root) / "src/description_pipeline/deploy/linux"
    paths = {name: directory / name for name in LINUX_RESOURCES}
    missing = [name for name, path in paths.items() if not path.is_file() or path.stat().st_size == 0]
    if missing:
        raise PipelineError(f"Linux distribution resources missing from the package: {missing}")
    return paths


def build_offline_bundle(root: Path, output: Path, identity: dict) -> Path:
    """Archive this exact tool wheel and hash-locked wheels for the tested Linux environment."""
    if identity["platform"]["system"] != "Linux":
        raise PipelineError("The Linux dependency bundle must be built in its target Linux environment")
    tools = list(output.glob(f"mimicverse_description-{identity['version']}-*.whl"))
    if len(tools) != 1:
        raise PipelineError("Expected exactly one matching tool wheel")
    launchers = linux_resource_paths(root)
    with tempfile.TemporaryDirectory(prefix="description-offline-") as temporary:
        staging = Path(temporary)
        wheels = staging / "wheels"
        wheels.mkdir()
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "download",
                "--only-binary=:all:",
                "--dest",
                str(wheels),
                "-r",
                str(root / "requirements/linux-py312.lock"),
            ],
            check=True,
        )
        shutil.copyfile(tools[0], wheels / tools[0].name)
        requirements = []
        for wheel in sorted(wheels.glob("*.whl")):
            name, version, _build, _tags = parse_wheel_filename(wheel.name)
            requirements.append(f"{name}=={version} --hash=sha256:{file_digest(wheel)}")
        (staging / "requirements.lock").write_text("\n".join(requirements) + "\n", encoding="utf-8")
        write_json(staging / "toolchain.json", identity)
        (staging / "README.md").write_text(
            f"# Offline installation\n\nRequires CPython {identity['python']} "
            f"on {identity['platform']['system']} {identity['platform']['machine']}.\n\n"
            "Extract this archive, then install the pinned environment once:\n\n"
            "```sh\nbash install.sh\n```\n\n"
            "If this machine has no CPython 3.12, install one first (for example\n"
            "`uv python install 3.12`) and point the installer at it:\n\n"
            "```sh\nDESCRIPTION_PYTHON=<path/to/python3.12> bash install.sh\n```\n\n"
            "The installer is safe to repeat, writes the runtime to `.venv` beside this file, and prints\n"
            "absolute paths. Activate it to type plain `description` commands:\n\n"
            "```sh\n"
            "source .venv/bin/activate\n"
            "description --version\n"
            "description quickstart --run\n"
            "```\n\n"
            "`description quickstart --run` writes an offline demo workspace into the current directory\n"
            "and runs freeze → build → check on it; it needs no CAD, no account and no network.\n\n"
            "Installing the same pinned set by hand:\n\n"
            "```sh\npython -m pip install --no-index --require-hashes --find-links wheels -r requirements.lock\n"
            "```\n\n"
            "From a model workspace, submit one candidate with the shared pipeline:\n\n"
            "```sh\n"
            "bash /path/to/this-bundle/submit.sh --root /path/to/model --profile kinematics\n"
            "```\n\n"
            "The tool source commit and environment are recorded in toolchain.json. "
            "No CAD connection is needed to rebuild existing complete snapshots.\n",
            encoding="utf-8",
        )
        for launcher_name, path in launchers.items():
            (staging / launcher_name).write_bytes(path.read_bytes())
        sums = {p.relative_to(staging).as_posix(): file_digest(p) for p in staging.rglob("*") if p.is_file()}
        write_json(staging / "files.json", sums)
        destination = (
            output / f"mimicverse_description-{identity['version']}-linux-{identity['platform']['machine']}.zip"
        )
        entries = [
            (path.relative_to(staging).as_posix(), path.read_bytes()) for path in staging.rglob("*") if path.is_file()
        ]
        write_zip(destination, entries)
        return destination


def build_windows_bundle(root: Path, output: Path, identity: dict) -> Path:
    """Ship the same committed package and the full Windows capture/build/verification runtime."""
    with tempfile.TemporaryDirectory(prefix="description-windows-") as temporary:
        wheels = Path(temporary)
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "download",
                "--only-binary=:all:",
                "--platform",
                "win_amd64",
                "--python-version",
                "3.12",
                "--implementation",
                "cp",
                "--abi",
                "cp312",
                "--dest",
                str(wheels),
                "-r",
                str(resource_path("requirements/win-py312.lock")),
            ],
            check=True,
        )
        requirements = []
        for wheel in sorted(wheels.glob("*.whl")):
            name, version, _build, _tags = parse_wheel_filename(wheel.name)
            requirements.append(f"{name}=={version} --hash=sha256:{file_digest(wheel)}")
        destination = output / f"description-worker-{identity['version']}-windows-x86_64.zip"
        build_worker_bundle(root / "src", destination, identity["version"], wheels, tuple(requirements))
        return destination


def _pin_utf8_streams() -> None:
    """Windows encodes redirected streams with the ANSI code page; callers read UTF-8."""

    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8")


def main() -> int:
    _pin_utf8_streams()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--require-clean", action="store_true")
    parser.add_argument("--out", type=Path, default=Path("dist"))
    parser.add_argument(
        "--offline", action="store_true", help="archive locked Linux and Windows packages for offline installation"
    )
    parser.add_argument(
        "--deploy-out",
        type=Path,
        default=None,
        help="also materialise the packaged Windows deployment resources here",
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    if Path(description_pipeline.__file__).resolve().parent != root / "src/description_pipeline":
        raise PipelineError(
            "Install this checkout with pip install --no-deps --no-build-isolation -e . before packaging"
        )
    if args.out.exists() and (not args.out.is_dir() or any(args.out.iterdir())):
        raise PipelineError("Distribution output must be empty; use a new directory for each tool commit")
    identity = tool_identity()
    if args.require_clean:
        dirty = subprocess.run(
            ["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True, encoding="utf-8", check=True
        ).stdout.strip()
        if dirty or identity["development"] or not identity["source_commit"]:
            raise PipelineError("A tool release requires a clean committed checkout")
    deploy = check_deploy_resources()
    # The builder version is part of the artifact bytes: check it before anything is written.
    deploy["builder"] = check_build_environment(root)
    if args.deploy_out is not None:
        written = export_deploy_resources(args.deploy_out)
        deploy["exported"] = [str(path) for path in written]
    metadata = root / "src/description_pipeline/tool-release.json"
    if metadata.exists():
        raise PipelineError("Unexpected existing release metadata; inspect it before rebuilding")
    try:
        write_json(metadata, identity)
        subprocess.run(
            [sys.executable, "-m", "build", "--no-isolation", "--outdir", str(args.out.resolve())], cwd=root, check=True
        )
        # The same commit and lock file must produce the same bytes: setuptools stamps wheels with
        # the build time and tar records ownership and mtimes, so normalize before anything packs or
        # hashes these artifacts.  Consumers can then rebuild a release and compare digests.
        for wheel in sorted(args.out.glob("*.whl")):
            normalize_zip(wheel)
        for sdist in sorted(args.out.glob("*.tar.gz")):
            normalize_sdist(sdist)
        if args.offline:
            deploy["linux_resources"] = list(linux_resource_paths(root))
            deploy["windows_bundle"] = str(build_windows_bundle(root, args.out.resolve(), identity))
    finally:
        metadata.unlink(missing_ok=True)
    if args.offline:
        deploy["offline_bundle"] = str(build_offline_bundle(root, args.out.resolve(), identity))
    checksums = [
        f"{file_digest(path)}  {path.name}"
        for path in sorted(args.out.glob("*"))
        if path.is_file() and path.name != "SHA256SUMS"
    ]
    (args.out / "SHA256SUMS").write_text("\n".join(checksums) + "\n", encoding="utf-8")
    print(json.dumps(deploy, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
