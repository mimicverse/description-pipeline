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
from .delivery import BUNDLE_SCHEMA, PIPELINE_ID, subject_inventory
from .io import PipelineError, acquire_process_lock, digest, file_digest, inventory, read_data, write_json

from .runtime import RUNTIME_PACKAGES
from .stages import STAGE_IDS, stage_view


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


def _stamp(root: Path, receipt: dict) -> None:
    receipt["artifacts"] = inventory(root, exclude=("reports/run.json", "reports/stages.json"))
    write_json(root / "reports/stages.json", stage_view(receipt))
    receipt["stage_report"] = {"path": "reports/stages.json", "sha256": file_digest(root / "reports/stages.json")}
    receipt["owned_files"] = inventory(root, exclude=("reports/run.json",))
    write_json(root / "reports/run.json", receipt)


def _check(root: Path) -> dict:
    from .verification.solidworks_urdf import check_bundle

    return check_bundle(root)


def _keep_diagnostic(staging: Path, output: Path, receipt: dict) -> Path:
    _stamp(staging, receipt)
    failed = output.with_name(output.name + ".failed")
    if not _owned_output(failed):
        failed = Path(tempfile.mkdtemp(prefix=output.name + ".failed-", dir=output.parent))
        failed.rmdir()
    _install(staging, failed)
    return failed


def _event_sink(receipt, on_event=None):
    """One timestamped engineering event stream for the receipt and endpoint."""

    def event(item):
        entry = {"at": datetime.now(UTC).isoformat(), **item}
        receipt.setdefault("events", []).append(entry)
        receipt["stage"] = item["stage"]
        if on_event is not None:
            on_event({**entry, "pipeline_id": PIPELINE_ID, "run_id": receipt["run_id"]})

    return event


def _failed_run(staging, output, receipt, event, error):
    phase = receipt.get("stage", receipt["execution_scope"][0])
    if not receipt.get("events") or receipt["events"][-1]["state"] != "failed":
        with contextlib.suppress(Exception):
            event({"stage": phase, "state": "failed"})
    receipt.update(passed=False, state="failed", stage=phase, error=str(error))
    if hasattr(error, "code"):
        receipt["error_code"] = error.code
    detail = getattr(error, "detail", None) or getattr(error, "details", None)
    if detail is not None:
        receipt["detail"] = detail
    has_staging = staging is not None and staging.exists()
    root = staging if has_staging else output
    quality_path = root / "reports/quality.json"
    if quality_path.is_file():
        receipt["quality"] = read_data(quality_path)
        receipt["subject_sha256"] = receipt["quality"].get("subject_sha256")
    if has_staging:
        receipt["diagnostic_path"] = str(_keep_diagnostic(staging, output, receipt))
    else:
        _stamp(output, receipt)
        receipt["diagnostic_path"] = str(output)
    return receipt


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
    prior_events=None,
    expected_inputs=None,
    handoff_sha256=None,
) -> dict:
    """Continue a prepared native job through capture, generation, verification and publication.

    ``backend`` is an internal dependency-injection seam for native regressions;
    the endpoint always uses the native SolidWorks backend. Capture or quality
    failure preserves a previous delivery. Submission failure keeps the new
    verified delivery and any publication receipt for retry without CAD.
    """

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
            "handoff_sha256": handoff_sha256,
            "state": "failed",
            "events": list(prior_events or []),
            "execution_scope": [
                stage for stage in STAGE_IDS[:2] if any(event["stage"] == stage for event in prior_events or [])
            ]
            + ["capture", "generate", "verify"]
            + (["publish"] if repository else []),
        }
        event = _event_sink(receipt, on_event)

        try:
            from .steps import capture_evidence, generate_model, publish_model, verify_delivery

            definition, input_report = capture_evidence(
                package,
                staging,
                backend=backend,
                on_event=event,
                expected_inputs=expected_inputs,
                handoff_sha256=handoff_sha256,
            )
            receipt["cad_revision"] = input_report["cad_revision"]["revision"]
            generated_subject = generate_model(staging, definition, input_report, on_event=event)
            report = verify_delivery(staging, generated_subject, on_event=event)
            receipt.update(
                hardware_id=definition["hardware_id"], subject_sha256=report.get("subject_sha256"), quality=report
            )
            if report.get("passed") is not True:
                raise PipelineError("URDF verification failed; see reports/quality.json")
            receipt.update(state="verified", passed=True)
            _stamp(staging, receipt)
            _install(staging, output)
            if repository is not None:
                submitted = publish_model(output, Path(repository), base=base, message=message, on_event=event)
                receipt.update(submission=submitted, state=submitted.get("state", "failed"), passed=submitted["passed"])
                _stamp(output, receipt)
            return {**receipt, "output": str(output)}
        except Exception as error:
            return _failed_run(staging, output, receipt, event, error)
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
    source_files = subject_inventory(bundle)
    if digest(source_files) != previous["subject_sha256"]:
        raise PipelineError("Frozen delivery changed after verification")
    definition = read_data(bundle / "input/robot.yaml")
    source_receipt = read_data(bundle / "reports/run.json") if (bundle / "reports/run.json").is_file() else {}
    handoff_sha256 = (definition.get("provenance") or {}).get("native_inventory_sha256")
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
            "rebuild_from": {
                "subject_sha256": previous.get("subject_sha256"),
                "run_id": source_receipt.get("run_id"),
                "handoff_sha256": handoff_sha256,
            },
            "handoff_sha256": handoff_sha256,
            "execution_scope": ["generate", "verify"] + (["publish"] if repository else []),
        }
        event = _event_sink(receipt)
        try:
            shutil.copytree(bundle / "input", staging / "input")
            shutil.copytree(bundle / "evidence", staging / "evidence")
            copied = {
                prefix + "/" + name: checksum
                for prefix in ("input", "evidence")
                for name, checksum in inventory(staging / prefix).items()
            }
            if (
                copied
                != {
                    name: checksum
                    for name, checksum in source_files.items()
                    if name.startswith(("input/", "evidence/"))
                }
                or subject_inventory(bundle) != source_files
            ):
                raise PipelineError("Frozen evidence changed during rebuild preparation")
            from .steps import generate_model, publish_model, verify_delivery

            generated_subject = generate_model(
                staging, definition, read_data(bundle / "reports/input.json"), on_event=event
            )
            report = verify_delivery(staging, generated_subject, on_event=event)
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
                submitted = publish_model(output, repository, base=base, message=message, on_event=event)
                receipt.update(submission=submitted, state=submitted["state"], passed=submitted["passed"])
                _stamp(output, receipt)
            return {**receipt, "output": str(output)}
        except Exception as error:
            return _failed_run(staging, output, receipt, event, error)
        finally:
            if staging.exists():
                shutil.rmtree(staging)


def submit(bundle: Path, repository: Path, *, base=None, message=None):
    """Recover publication without repeating native work or losing the run history."""
    from .steps import publish_model

    with output_lock(Path(bundle)) as bundle:
        marker = bundle / "reports/run.json"
        receipt = (
            read_data(marker)
            if marker.is_file()
            else {
                "schema_version": BUNDLE_SCHEMA,
                "pipeline_id": PIPELINE_ID,
                "run_id": str(uuid.uuid4()),
                "execution_scope": ["publish"],
                "events": [],
            }
        )
        receipt["execution_scope"] = ["publish"]
        receipt.setdefault("publication_attempts", [receipt["submission"]] if receipt.get("submission") else [])
        event = _event_sink(receipt)
        try:
            result = publish_model(bundle, repository, base=base, message=message, on_event=event)
        except Exception as error:
            return _failed_run(None, bundle, receipt, event, error)
        receipt["publication_attempts"].append(result)
        receipt.update(
            submission=result,
            state=result["state"],
            passed=result["passed"],
            subject_sha256=result.get("subject_sha256"),
            error=result.get("error"),
            detail=result.get("detail"),
        )
        _stamp(bundle, receipt)
        return result


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
        results.append(
            {"id": "consumer.urdf", "passed": False, "message": str(error), "details": getattr(error, "details", {})}
        )
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
