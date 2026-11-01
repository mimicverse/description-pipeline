"""Build deterministic, source-bound distributions from a clean committed checkout."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import zipfile
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace

from packaging.utils import parse_wheel_filename
from packaging.requirements import Requirement

if __package__:
    from .release_consumers import (
        CONSUMER_LOCKS,
        CONSUMER_POOL_PREFIX,
        locked_consumers,
        target_tags,
        verify_consumer_pool,
        wheel_record,
    )
else:
    from release_consumers import (
        CONSUMER_LOCKS,
        CONSUMER_POOL_PREFIX,
        locked_consumers,
        target_tags,
        verify_consumer_pool,
        wheel_record,
    )

BUILD_PACKAGES = {"setuptools", "wheel", "build"}


def bind_source_closure(source: Path) -> SimpleNamespace:
    """Load version, release identity and codecs from the specified source closure only.

    The release identity can never be decided by an ambient installation: ambient
    ``description_pipeline`` modules are set aside while the specified source root is
    the only path entry, every loaded module must actually live inside it, and the
    caller's module state is restored before returning - the returned objects stay
    bound to the source closure.
    """

    source = Path(source).resolve()

    def pipeline_modules() -> list[str]:
        return [
            name for name in sys.modules if name == "description_pipeline" or name.startswith("description_pipeline.")
        ]

    saved = {name: sys.modules[name] for name in pipeline_modules()}
    for name in saved:
        del sys.modules[name]
    sys.path.insert(0, str(source))
    try:
        import description_pipeline  # noqa: PLC0415
        from description_pipeline import io as pipeline_io  # noqa: PLC0415
        from description_pipeline.build import archive as build_archive  # noqa: PLC0415
        from description_pipeline.delivery import PIPELINE_ID  # noqa: PLC0415
        from description_pipeline import interface_records as pipeline_records  # noqa: PLC0415
        from description_pipeline import runtime as pipeline_runtime  # noqa: PLC0415
        from description_pipeline.simulation.schemas import load_runtime_contract  # noqa: PLC0415

        for module in (description_pipeline, pipeline_io, build_archive, pipeline_records, pipeline_runtime):
            module_file = getattr(module, "__file__", "") or ""
            if not Path(module_file).resolve().is_relative_to(source):
                raise pipeline_io.PipelineError(
                    f"the release build bound a module outside the source closure: {module_file}"
                )
        return SimpleNamespace(
            version=description_pipeline.__version__,
            PIPELINE_ID=PIPELINE_ID,
            PipelineError=pipeline_io.PipelineError,
            digest=pipeline_io.digest,
            file_digest=pipeline_io.file_digest,
            inventory=pipeline_io.inventory,
            write_json=pipeline_io.write_json,
            normalize_zip=build_archive.normalize_zip,
            normalize_sdist=build_archive.normalize_sdist,
            write_zip=build_archive.write_zip,
            release_domain_identity=pipeline_records.release_domain_identity,
            SUPPORTED_PYTHON=pipeline_runtime.SUPPORTED_PYTHON,
            TARGET_PYTHON=pipeline_runtime.TARGET_PYTHON,
            TARGET_ABI=pipeline_runtime.TARGET_ABI,
            WINDOWS_PLATFORMS=pipeline_runtime.WINDOWS_PLATFORMS,
            LINUX_PLATFORMS=pipeline_runtime.LINUX_PLATFORMS,
            load_runtime_contract=load_runtime_contract,
        )
    finally:
        sys.path.remove(str(source))
        for name in pipeline_modules():
            del sys.modules[name]
        sys.modules.update(saved)


def locked_builders(root, bound):
    versions = {}
    for line in (root / "requirements/build-py312.lock").read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        requirement = Requirement(line)
        if requirement.name in BUILD_PACKAGES:
            (spec,) = requirement.specifier
            if spec.operator != "==":
                raise bound.PipelineError("Release builders must use exact versions")
            versions[requirement.name] = spec.version
    if set(versions) != BUILD_PACKAGES:
        raise bound.PipelineError("Builder lock is incomplete")
    return versions


def git(root, *arguments):
    return subprocess.check_output(["git", *arguments], cwd=root, text=True).strip()


def target_arguments(platform: str, bound) -> list[str]:
    """The pip target flags of one role archive, from the single target rule.

    The Linux archive is built for the declared glibc baseline instead of the
    building host's tag set; Windows uses the declared platform.  Interpreter
    and ABI come from the same rule.
    """

    platforms = bound.WINDOWS_PLATFORMS if platform == "windows" else bound.LINUX_PLATFORMS
    arguments = [
        "--python-version",
        bound.TARGET_PYTHON,
        "--implementation",
        "cp",
        "--abi",
        bound.TARGET_ABI,
    ]
    for name in platforms:
        arguments += ["--platform", name]
    return arguments


def source_inventory(directory: Path, bound: SimpleNamespace) -> dict[str, str]:
    """The source file inventory with interpreter bytecode caches ignored.

    A committed checkout must stay buildable after running tests: ``__pycache__`` is
    never part of the source closure and never enters the archived inventory.
    """

    return {
        name: digest for name, digest in bound.inventory(directory).items() if "__pycache__" not in Path(name).parts
    }


def build_distributions(
    source: Path,
    output: Path,
    *,
    environment: Mapping[str, str],
) -> None:
    """Run the build frontend only inside the archived source staging tree."""

    source = Path(source).resolve()
    output = Path(output).resolve()
    subprocess.run(
        [
            sys.executable,
            "-m",
            "build",
            "--no-isolation",
            "--outdir",
            str(output),
            str(source),
        ],
        cwd=source,
        check=True,
        env=dict(environment),
    )


def tracked_test_inventory(root: Path, bound: SimpleNamespace) -> dict[str, str]:
    """The complete tracked test tree, keyed by its source-archive path."""

    raw = subprocess.check_output(["git", "ls-files", "-z", "--", "tests"], cwd=root)
    files = {}
    for encoded in raw.split(b"\0"):
        if not encoded:
            continue
        name = encoded.decode("utf-8")
        path = root / name
        if not path.is_file():
            raise bound.PipelineError(f"Tracked test file is missing or not a regular file: {name}")
        files[name] = bound.file_digest(path)
    if not files:
        raise bound.PipelineError("The source checkout tracks no test files")
    return files


def consumer_contracts(source, bound):
    path = source / "src/description_pipeline/simulation/teleop-runtime.json"
    return {role: bound.load_runtime_contract(path, consumer=role) for role in ("pico", "assessment")}


def model_inputs(source, bound, teleop_wheel, actuator_wheel):
    """Snapshot the two explicit model wheels after checking the existing common contract."""
    contracts = consumer_contracts(source, bound)
    locked_consumers({role: (source / name).read_bytes() for role, name in CONSUMER_LOCKS.items()}, contracts)
    models = {}
    for path, expected in (
        (teleop_wheel, contracts["pico"]),
        (actuator_wheel, contracts["assessment"]["actuator_model"]),
    ):
        if path is None:
            raise bound.PipelineError("Release builds require explicit Teleop and actuator-model wheels")
        path = Path(path)
        payload = path.read_bytes()
        if path.name != expected["wheel_filename"] or hashlib.sha256(payload).hexdigest() != expected["wheel_sha256"]:
            raise bound.PipelineError("A model wheel differs from the fixed consumer asset")
        try:
            record = wheel_record(
                path.name, payload, target_tags(bound.LINUX_PLATFORMS, bound.TARGET_PYTHON, bound.TARGET_ABI)
            )
        except (ValueError, zipfile.BadZipFile, KeyError, TypeError) as error:
            raise bound.PipelineError("A model wheel archive is invalid") from error
        if record["name"] == "robot-teleop" and record["teleop_files"] != contracts["pico"]["files"]:
            raise bound.PipelineError("Teleop wheel differs from the common runtime inventory")
        models[path.name] = payload
    return models


def consumer_pool(source, models, bound, *, wheelhouse=None, main_wheels=None):
    """Resolve each isolated role, then keep one checked pool without mixing main wheels."""
    contracts = consumer_contracts(source, bound)
    lock_data = {role: (source / name).read_bytes() for role, name in CONSUMER_LOCKS.items()}
    locked_consumers(lock_data, contracts)
    with tempfile.TemporaryDirectory(prefix="description-consumers-") as temporary:
        root = Path(temporary)
        supplied = root / "models"
        supplied.mkdir()
        for name, payload in models.items():
            (supplied / name).write_bytes(payload)
        pool = root / "wheels"
        for role, lock in CONSUMER_LOCKS.items():
            arguments = [
                sys.executable,
                "-m",
                "pip",
                "download",
                "--only-binary=:all:",
                "--require-hashes",
                "--dest",
                str(pool),
                *target_arguments("linux", bound),
                "-r",
                str(source / lock),
            ]
            if wheelhouse is not None:
                arguments += ["--no-index", "--find-links", str(Path(wheelhouse).resolve())]
            if main_wheels is not None:
                arguments += ["--find-links", str(main_wheels)]
            if role != "pico-link":
                contract = contracts[role]
                assets = [("robot-teleop[evaluation]" if role == "assessment" else "robot-teleop", contract)]
                if contract["actuator_model"] is not None:
                    assets.append(("actuator-model[mujoco]", contract["actuator_model"]))
                explicit = root / (role + ".lock")
                explicit.write_text(
                    "\n".join(
                        f"{name} @ {(supplied / asset['wheel_filename']).as_uri()} "
                        f"--hash=sha256:{asset['wheel_sha256']}"
                        for name, asset in assets
                    )
                    + "\n"
                )
                arguments += ["-r", str(explicit)]
            subprocess.run(arguments, check=True)
        members = {path.name: path.read_bytes() for path in pool.iterdir()}
        verify_consumer_pool(
            members, lock_data, contracts, target_tags(bound.LINUX_PLATFORMS, bound.TARGET_PYTHON, bound.TARGET_ABI)
        )
        return members


def offline_bundle(source, output, wheel, platform, bound, *, models=None, consumer_wheelhouse=None):
    """Include exactly the resolved target wheels and a SHA-256 installation lock."""
    with tempfile.TemporaryDirectory(prefix="description-offline-") as temporary:
        staging = Path(temporary)
        wheels = staging / "wheels"
        arguments = [sys.executable, "-m", "pip", "download", "--only-binary=:all:", "--dest", str(wheels)]
        arguments += target_arguments(platform, bound)
        lock_files = [
            source
            / ("deploy/airflow/requirements.lock" if platform == "linux" else f"requirements/{platform}-py312.lock")
        ]
        for lock_file in lock_files:
            arguments += ["-r", str(lock_file)]
        subprocess.run(arguments, check=True)
        shutil.copyfile(wheel, wheels / wheel.name)
        locked = []
        for path in sorted(wheels.glob("*.whl")):
            name, version, _, _ = parse_wheel_filename(path.name)
            locked.append(f"{name}=={version} --hash=sha256:{bound.file_digest(path)}")
        (staging / "requirements.lock").write_text("\n".join(locked) + "\n", encoding="utf-8")
        if platform == "linux":
            members = consumer_pool(source, models, bound, wheelhouse=consumer_wheelhouse, main_wheels=wheels)
            for name, payload in members.items():
                path = staging / CONSUMER_POOL_PREFIX / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(payload)
        doctor_command = (
            "description doctor"
            if platform == "linux"
            else (
                'python -I -B -c "import json; '
                "from description_pipeline.runtime import tool_record; "
                "from description_pipeline.solidworks import doctor; "
                "print(json.dumps({'tool': tool_record(role='native'), 'doctor': doctor()}))\""
            )
        )
        (staging / "README.md").write_text(
            "# Offline installation\n\nRequires CPython 3.12, x86_64. Create a dedicated virtual environment, "
            "then run its Python from this directory:\n\n"
            "```sh\npython -m pip install --no-index --require-hashes --find-links wheels -r requirements.lock\n"
            f"{doctor_command}\n```\n\nFollow docs/deployment.md in the matching deployment archive.\n",
            encoding="utf-8",
        )
        bound.write_json(staging / "files.json", bound.inventory(staging))
        target = output / f"description-{bound.version}-{platform}-cp312-x86_64.zip"
        bound.write_zip(target, ((name, (staging / name).read_bytes()) for name in bound.inventory(staging)))
        return target


def build(root, output, *, teleop_wheel=None, actuator_wheel=None, consumer_wheelhouse=None):
    root, output = root.resolve(), output.resolve()
    bound = bind_source_closure(root / "src")
    target_python = tuple(int(part) for part in bound.TARGET_PYTHON.split("."))
    if sys.version_info[:2] != target_python:
        raise bound.PipelineError(f"Release builds require CPython {bound.TARGET_PYTHON}")
    requires_python = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]["requires-python"]
    if requires_python != bound.SUPPORTED_PYTHON:
        raise bound.PipelineError("pyproject requires-python differs from the supported interpreter range")
    if git(root, "status", "--porcelain", "--untracked-files=normal"):
        raise bound.PipelineError("Release builds require a clean committed checkout")
    if output.exists() and any(output.iterdir()):
        raise bound.PipelineError("Use an empty release output directory")
    builders = locked_builders(root, bound)
    for name, expected in builders.items():
        if importlib.metadata.version(name) != expected:
            raise bound.PipelineError(f"Install requirements/build-py312.lock: {name} must be {expected}")
    models = model_inputs(root, bound, teleop_wheel, actuator_wheel)
    test_files = tracked_test_inventory(root, bound)
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
        if source_inventory(root / "src/description_pipeline", bound) != source_inventory(package, bound):
            raise bound.PipelineError("the bound source closure differs from the archived commit")
        archived_tests = {
            f"tests/{name}": checksum for name, checksum in source_inventory(source / "tests", bound).items()
        }
        if archived_tests != test_files:
            raise bound.PipelineError("the tracked test closure differs from the archived commit")
        files = bound.inventory(package)
        resources = [
            path
            for directory in ("deploy", "docs", "requirements", "scripts")
            for path in (source / directory).rglob("*")
            if path.is_file()
        ]
        resources += [
            source / name for name in ("README.md", "LICENSE", "CODE_OF_CONDUCT.md", "CONTRIBUTING.md", "RELEASING.md")
        ]
        deployment_files = {path.relative_to(source).as_posix(): bound.file_digest(path) for path in resources}
        runtime_locks = {
            platform: bound.file_digest(source / f"requirements/{platform}-py312.lock")
            for platform in ("linux", "windows")
        }
        identity = {
            "schema_version": "solidworks-to-urdf.release/v1",
            "pipeline_id": bound.PIPELINE_ID,
            "version": bound.version,
            "source_commit": commit,
            "source_tree": git(root, "rev-parse", f"{commit}^{{tree}}"),
            "source_sha256": bound.digest(files),
            "package_files": files,
            "test_files": test_files,
            "deployment_files": deployment_files,
            "runtime_locks": runtime_locks,
            **bound.release_domain_identity(),
            "builder": {"python": sys.version.split()[0], **builders},
        }
        bound.write_json(package / "tool-release.json", identity)
        build_env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
        build_distributions(
            source,
            output,
            environment=build_env,
        )
        (wheel,) = output.glob("*.whl")
        (sdist,) = output.glob("*.tar.gz")
        bound.normalize_zip(wheel)
        bound.normalize_sdist(sdist)
        deployment = output / f"description-{bound.version}-deployment.zip"
        bound.write_zip(deployment, ((path.relative_to(source).as_posix(), path.read_bytes()) for path in resources))
        for platform in ("linux", "windows"):
            offline_bundle(
                source, output, wheel, platform, bound, models=models, consumer_wheelhouse=consumer_wheelhouse
            )
        artifacts = bound.inventory(output)
        bound.write_json(output / "release.json", {**identity, "artifacts": artifacts})
    return {
        "passed": True,
        "source_commit": commit,
        "source_sha256": identity["source_sha256"],
        "artifacts": artifacts,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--teleop-wheel", type=Path, required=True)
    parser.add_argument("--actuator-wheel", type=Path, required=True)
    parser.add_argument("--consumer-wheelhouse", type=Path, help="Resolve consumers only from local wheels")
    args = parser.parse_args()
    result = build(
        Path(__file__).resolve().parent.parent,
        args.output,
        teleop_wheel=args.teleop_wheel,
        actuator_wheel=args.actuator_wheel,
        consumer_wheelhouse=args.consumer_wheelhouse,
    )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
