"""One local path from a saved CAD package to an independently checked URDF."""

from __future__ import annotations

import contextlib
import importlib.metadata
import os
import platform
import shutil
import sys
import tempfile
import uuid
from contextlib import contextmanager
from datetime import datetime, UTC
from pathlib import Path

from . import __version__
from .backends.urdf import generate_urdf
from .delivery import BUNDLE_SCHEMA, PIPELINE_ID
from .io import PipelineError, acquire_process_lock, confined, digest, file_digest, inventory, read_data, write_json
from .model import Robot
from .sources.solidworks.scene import load_scene
from .sources.solidworks.revision import package_inventory, read_revision

RUNTIME_PACKAGES = ("numpy", "PyYAML", "jsonschema", "packaging", "mujoco")


def tool_record() -> dict:
    """Identify the code and runtime actually used, without a mutable Git ref."""

    package = Path(__file__).resolve().parent
    files = {}
    for path in sorted(package.rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts and path.name != "tool-release.json":
            files[path.relative_to(package).as_posix()] = file_digest(path)
    release = package / "tool-release.json"
    identity = read_data(release) if release.is_file() else None
    source_hash = digest(files)
    if identity is not None and (
        identity.get("schema_version") != "solidworks-to-urdf.release/v1"
        or identity.get("pipeline_id") != PIPELINE_ID
        or identity.get("version") != __version__
        or identity.get("source_sha256") != source_hash
        or identity.get("package_files") != files
    ):
        raise PipelineError("Installed release files differ from the embedded source identity; reinstall the release")
    return {
        "schema_version": "solidworks-to-urdf.tool/v1",
        "pipeline_id": PIPELINE_ID,
        "version": __version__,
        "source_sha256": source_hash,
        "release": identity,
        "runtime": {
            "python": platform.python_version(),
            "system": platform.system(),
            "machine": platform.machine(),
            "packages": runtime_packages(),
        },
    }


def runtime_packages() -> dict:
    """Record runtime dependency closure, excluding unrelated developer tools."""
    from packaging.requirements import Requirement
    from packaging.utils import canonicalize_name

    pending = [(name, frozenset()) for name in RUNTIME_PACKAGES + (("pywin32",) if sys.platform == "win32" else ())]
    versions = {}
    visited = set()
    while pending:
        requested, extras = pending.pop()
        distribution = importlib.metadata.distribution(requested)
        name = canonicalize_name(distribution.metadata["Name"])
        context = name, extras
        if context in visited:
            continue
        visited.add(context)
        versions[name] = distribution.version
        for value in distribution.requires or []:
            requirement = Requirement(value)
            if requirement.marker is None or any(
                requirement.marker.evaluate({"extra": extra}) for extra in {"", *extras}
            ):
                pending.append((requirement.name, frozenset(requirement.extras)))
    return dict(sorted(versions.items()))


@contextmanager
def output_lock(output: Path):
    """Refuse overlapping runs without changing a previous delivery."""

    if output.is_symlink() or output.is_junction():
        raise PipelineError("Output cannot be a symlink or junction")
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    path = output.parent / f".{output.name}.description.lock"
    try:
        handle = acquire_process_lock(path)
    except OSError as error:
        raise PipelineError(f"Another run owns {path} or its ownership lock is unavailable") from error
    try:
        yield output
    finally:
        os.close(handle)


def _owned_output(path: Path) -> bool:
    if not path.exists():
        return True
    if path.is_symlink() or path.is_junction() or not path.is_dir():
        return False
    if not any(path.iterdir()):
        return True
    marker = path / "reports/run.json"
    if not marker.is_file():
        return False
    try:
        receipt = read_data(marker)
        return (
            isinstance(receipt, dict)
            and receipt.get("schema_version") == BUNDLE_SCHEMA
            and receipt.get("owned_files") == inventory(path, exclude=("reports/run.json",))
        )
    except (ValueError, OSError):
        return False


def _install(staging: Path, target: Path) -> None:
    """Replace only a marked pipeline output; roll back a failed directory swap."""

    if not _owned_output(target):
        raise PipelineError(f"Refusing to replace a directory not owned by this pipeline: {target}")
    backup = Path(tempfile.mkdtemp(prefix=f".{target.name}-previous-", dir=target.parent))
    backup.rmdir()
    moved = False
    try:
        if target.exists():
            os.replace(target, backup)
            moved = True
        os.replace(staging, target)
    except BaseException:
        if moved and not target.exists():
            os.replace(backup, target)
        raise
    finally:
        if backup.exists() and target.exists():
            shutil.rmtree(backup)


def _readme(hardware: str) -> str:
    return (
        f"# {hardware} URDF\n\n"
        f"Pipeline: `{PIPELINE_ID}`. Entry point: `urdf/robot.urdf`.\n"
        "Keep `meshes/` beside `urdf/`; units are metres, kilograms and radians.\n\n"
        "`reports/input.json` checks the supplied package. `reports/quality.json`\n"
        "records independent checks of the captured evidence and delivered URDF.\n"
        "`reports/tool.json` identifies the build tool and runtime.\n\n"
        "A passing report covers its stated checks and bound file bytes. It does\n"
        "not establish hardware safety, measured accuracy or training readiness.\n"
        "Rerun `description check .` before consuming a copied delivery.\n"
    )


def _stamp(root: Path, receipt: dict) -> None:
    receipt["owned_files"] = inventory(root, exclude=("reports/run.json",))
    write_json(root / "reports/run.json", receipt)


def inspect_input(package: Path) -> dict:
    """Check robot semantics and the mechanical team's sealed CAD revision."""

    from .sources.solidworks.input import inspect_package

    report = inspect_package(package)
    try:
        author = report.get("input")
        hardware = author.get("hardware_id") if isinstance(author, dict) else None
        report["cad_revision"] = read_revision(package, hardware_id=hardware)
    except (PipelineError, OSError, ValueError) as error:
        report["errors"].append({"code": "input.cad_revision", "message": str(error), "detail": None})
        report["passed"] = False
    return report


def _copy_definition(package: Path, staging: Path) -> None:
    """Archive the original handoff; evidence/source holds the relinked CAD copy."""

    destination = staging / "input"
    destination.mkdir()
    for name in package_inventory(package):
        original = confined(package, name)
        target = confined(destination, name, exists=False)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(original, target)


def _check(root: Path) -> dict:
    from .verification.solidworks_urdf import check_bundle

    return check_bundle(root)


def _candidate(staging: Path, definition: dict, input_report: dict, *, on_event=None) -> dict:
    from .verification.solidworks_urdf import evaluate_bundle

    def event(stage, state):
        if on_event is not None:
            on_event(stage, state)

    event("generate", "running")
    hardware = definition["hardware_id"]
    (staging / "README.md").write_text(_readme(hardware), encoding="utf-8", newline="\n")
    write_json(staging / "reports/input.json", input_report)
    write_json(staging / "reports/tool.json", tool_record())
    robot = Robot.from_dict(load_scene(staging / "evidence"))
    write_json(staging / "model/robot.json", robot.to_dict())
    generate_urdf(robot, staging / "evidence", staging, name=hardware)
    event("generate", "completed")
    event("verify", "running")
    report = evaluate_bundle(staging)
    write_json(staging / "reports/quality.json", report)
    if report.get("passed") is True:
        report = _check(staging)
    event("verify", "completed" if report.get("passed") is True else "failed")
    return report


def submit(bundle: Path, repository: Path, *, base: str | None = None, message: str | None = None) -> dict:
    """Recheck and submit a frozen delivery from either Windows or Linux."""

    from .repository.urdf_pr import submit_bundle

    author = read_data(Path(bundle) / "input/robot.yaml")
    hardware = author["hardware_id"]
    result = submit_bundle(
        bundle,
        repository,
        base=base or f"feature/{hardware}",
        branch=f"work/solidworks/{hardware.lower()}",
        message=message,
    )
    return {
        **result,
        "pipeline_id": PIPELINE_ID,
        "subject_sha256": result.get("subject"),
        "passed": bool(result.get("url")) and result.get("state") in {"published", "updated", "noop"},
    }


def _keep_diagnostic(staging: Path, output: Path, receipt: dict) -> Path:
    _stamp(staging, receipt)
    failed = output.with_name(output.name + ".failed")
    if not _owned_output(failed):
        failed = Path(tempfile.mkdtemp(prefix=output.name + ".failed-", dir=output.parent))
        failed.rmdir()
    _install(staging, failed)
    return failed


def run(
    package: Path,
    output: Path,
    *,
    repository: Path | None = None,
    base: str | None = None,
    message: str | None = None,
    backend=None,
    run_id: str | None = None,
    on_event=None,
) -> dict:
    """Inspect, capture, generate, independently verify and optionally submit one PR.

    ``backend`` is an internal dependency-injection seam for native regressions;
    the public CLI always uses the native SolidWorks backend. Capture or quality
    failure preserves a previous delivery. Submission failure keeps the new
    verified delivery and any publication receipt for retry without CAD.
    """

    from .sources.solidworks.freeze import freeze
    from .sources.solidworks.input import load_package

    package = Path(package)
    if package.is_symlink() or package.is_junction():
        raise PipelineError("The input package cannot be a symlink or junction")
    package = package.resolve()
    output = Path(output)
    if output.resolve().is_relative_to(package) or package.is_relative_to(output.resolve()):
        raise PipelineError("Input and output directories must be separate")
    if repository is not None:
        repo = Path(repository).resolve()
        if output.resolve().is_relative_to(repo) or repo.is_relative_to(output.resolve()):
            raise PipelineError("Build output and the model repository must be separate directories")
    with output_lock(output) as output:
        if not _owned_output(output):
            raise PipelineError(f"Output contains unrelated files: {output}")
        staging = Path(tempfile.mkdtemp(prefix=f".{output.name}-capture-", dir=output.parent))
        receipt = {
            "schema_version": BUNDLE_SCHEMA,
            "pipeline_id": PIPELINE_ID,
            "run_id": run_id or str(uuid.uuid4()),
            "state": "failed",
            "events": [],
        }
        phase = "inspect"

        def event(stage, state):
            nonlocal phase
            phase = stage
            entry = {"stage": stage, "state": state, "at": datetime.now(UTC).isoformat()}
            receipt["events"].append(entry)
            receipt["stage"] = stage
            if on_event is not None:
                on_event({**entry, "pipeline_id": PIPELINE_ID, "run_id": receipt["run_id"]})

        try:
            event("inspect", "running")
            input_report = inspect_input(package)
            write_json(staging / "reports/input.json", input_report)
            if input_report.get("passed") is not True:
                raise PipelineError("The CAD package does not meet the input specification")
            definition = load_package(package)
            original_files = package_inventory(package)
            input_report["package_files"] = original_files
            receipt["cad_revision"] = input_report["cad_revision"]["revision"]
            if backend is None and sys.platform != "win32":
                raise PipelineError("Native SLDASM/SLDPRT capture requires Windows with licensed SolidWorks")
            if backend is None:
                from .verification.consumer import readiness

                readiness()
            _copy_definition(package, staging)
            event("inspect", "completed")
            event("capture", "running")
            freeze(definition["source"], staging / "evidence", backend=backend, worker_version=__version__)
            if package_inventory(package) != original_files:
                raise PipelineError("The supplied package changed during capture; save it and rerun")
            event("capture", "completed")
            report = _candidate(staging, definition, input_report, on_event=event)
            receipt.update(
                hardware_id=definition["hardware_id"], subject_sha256=report.get("subject_sha256"), quality=report
            )
            if report.get("passed") is not True:
                raise PipelineError("URDF verification failed; see reports/quality.json")
            receipt.update(state="verified", passed=True)
            _stamp(staging, receipt)
            _install(staging, output)
            if repository is not None:
                event("submit", "running")
                submitted = submit(output, Path(repository), base=base, message=message)
                write_json(output / "reports/pr.json", submitted)
                receipt["submission"] = submitted
                receipt["state"] = submitted.get("state", "failed")
                receipt["passed"] = submitted["passed"]
                event("submit", "completed" if submitted["passed"] else "failed")
                _stamp(output, receipt)
            return {**receipt, "output": str(output)}
        except Exception as error:
            if not receipt["events"] or receipt["events"][-1]["state"] != "failed":
                with contextlib.suppress(Exception):
                    event(phase, "failed")
            receipt.update(passed=False, stage=phase, error=str(error))
            if hasattr(error, "code"):
                receipt["error_code"] = error.code
            if hasattr(error, "detail") and error.detail is not None:
                receipt["detail"] = error.detail
            elif isinstance(getattr(error, "details", None), dict):
                receipt["detail"] = error.details
            if staging.exists():
                failed = _keep_diagnostic(staging, output, receipt)
                receipt["diagnostic_path"] = str(failed)
            else:
                _stamp(output, receipt)
                receipt["diagnostic_path"] = str(output)
            return receipt
        finally:
            if staging.exists():
                shutil.rmtree(staging)


def rebuild(
    bundle: Path,
    output: Path,
    *,
    repository: Path | None = None,
    base: str | None = None,
    message: str | None = None,
) -> dict:
    """Rebuild a verified frozen capture without Windows or a CAD session."""

    bundle = Path(bundle).resolve()
    output = Path(output)
    if output.resolve().is_relative_to(bundle) or bundle.is_relative_to(output.resolve()):
        raise PipelineError("Rebuild input and output must be separate")
    if repository is not None:
        repo = Path(repository).resolve()
        if output.resolve().is_relative_to(repo) or repo.is_relative_to(output.resolve()):
            raise PipelineError("Build output and the model repository must be separate directories")
    previous = _check(bundle)
    if previous.get("passed") is not True:
        raise PipelineError("The frozen bundle failed verification; repair its author inputs and recapture")
    with output_lock(output) as output:
        if not _owned_output(output):
            raise PipelineError(f"Output contains unrelated files: {output}")
        staging = Path(tempfile.mkdtemp(prefix=f".{output.name}-rebuild-", dir=output.parent))
        receipt = {
            "schema_version": BUNDLE_SCHEMA,
            "pipeline_id": PIPELINE_ID,
            "run_id": str(uuid.uuid4()),
            "state": "failed",
            "passed": False,
            "rebuild_from": previous.get("subject_sha256"),
        }
        try:
            shutil.copytree(bundle / "input", staging / "input")
            shutil.copytree(bundle / "evidence", staging / "evidence")
            definition = read_data(bundle / "input/robot.yaml")
            report = _candidate(staging, definition, read_data(bundle / "reports/input.json"))
            receipt.update(
                state="verified" if report.get("passed") is True else "failed",
                passed=report.get("passed") is True,
                subject_sha256=report.get("subject_sha256"),
                quality=report,
            )
            _stamp(staging, receipt)
            if not receipt["passed"]:
                failed = _keep_diagnostic(staging, output, receipt)
                return {**receipt, "diagnostic_path": str(failed)}
            _install(staging, output)
            if repository is not None:
                submitted = submit(output, repository, base=base, message=message)
                write_json(output / "reports/pr.json", submitted)
                receipt.update(submission=submitted, state=submitted["state"], passed=submitted["passed"])
                _stamp(output, receipt)
            return {**receipt, "output": str(output)}
        except Exception as error:
            receipt.update(passed=False, state="failed", error=str(error))
            if staging.exists():
                receipt["diagnostic_path"] = str(_keep_diagnostic(staging, output, receipt))
            else:
                _stamp(output, receipt)
                receipt["diagnostic_path"] = str(output)
            return receipt
        finally:
            if staging.exists():
                shutil.rmtree(staging)


def doctor() -> dict:
    """Check the runtime; native capture availability is a separate explicit fact."""

    results = [{"id": "python", "passed": sys.version_info[:2] == (3, 12), "actual": platform.python_version()}]
    for name in RUNTIME_PACKAGES:
        try:
            results.append({"id": name, "passed": True, "actual": importlib.metadata.version(name)})
        except importlib.metadata.PackageNotFoundError:
            results.append({"id": name, "passed": False, "message": "Reinstall the pinned release runtime"})
    from .verification.consumer import readiness

    try:
        consumer = readiness()
        results.append({"id": "consumer.urdf", "passed": True, "details": consumer})
    except Exception as error:
        results.append({"id": "consumer.urdf", "passed": False, "message": str(error),
                        "details": getattr(error, "details", {})})
    native = False
    native_detail = "Native CAD capture requires Windows with licensed SolidWorks"
    if sys.platform == "win32":
        from .sources.solidworks.isolation import registered_executable

        try:
            native_detail = registered_executable()
            native = True
        except Exception as error:
            native_detail = str(error)
    return {
        "pipeline_id": PIPELINE_ID,
        "version": __version__,
        "passed": all(item["passed"] for item in results),
        "checks": results,
        "native_capture_available": native,
        "native_capture_detail": native_detail,
        "git_available": shutil.which("git") is not None,
        "github_cli_available": shutil.which("gh") is not None,
    }
