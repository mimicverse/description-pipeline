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

from .runtime import RUNTIME_VERSIONS, native_readiness, required_packages, runtime_role
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


def _seed_resume(staging: Path, seed_dir: Path | None, resume_from: str) -> None:
    """Copy reusable upstream checkpoints from a prior owned delivery (read-only)."""
    if seed_dir is None or not Path(seed_dir).is_dir():
        raise PipelineError("Resume checkpoints are unavailable")
    seed = Path(seed_dir)
    parts = {
        "generate": ("input", "evidence", "reports/input.json"),
        "verify": (
            "README.md",
            "input",
            "evidence",
            "model",
            "urdf",
            "meshes",
            "reports/input.json",
            "reports/tool.json",
        ),
        "publish": ("README.md", "input", "evidence", "model", "urdf", "meshes", "reports"),
    }[resume_from]
    for part in parts:
        source = seed / part
        target = staging / part
        if source.is_dir():
            shutil.copytree(source, target)
        elif source.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        else:
            raise PipelineError(f"Resume checkpoint is missing: {part}")
    for name in ("reports/native-tool.json", "transfer-manifest.json"):
        source = seed / name
        if source.is_file():
            target = staging / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)


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


def _native_tool_record() -> dict:
    """The native-role tool identity; role-aware runtimes land with the host split."""

    from .runtime import tool_record

    try:
        return tool_record(role="native")
    except TypeError:  # pragma: no cover - transitional runtimes without role support
        return tool_record()


def _finish_native_capture(staging, output, receipt, *, run_id, handoff_sha256, main_assembly):
    """Close a capture-only run: provenance reports, sealed transfer, native_complete."""

    tool = _native_tool_record()
    write_json(staging / "reports/native-tool.json", tool)
    receipt.update(state="native_complete", passed=False, native_complete=True, native_tool=tool, stage="capture")
    _stamp(staging, receipt)
    from .stage_transfer import CAPTURE_ARCHIVE, CAPTURE_MANIFEST, seal_capture

    archive = staging / CAPTURE_ARCHIVE
    manifest = seal_capture(
        staging, archive, run_id=run_id, handoff_sha256=handoff_sha256, main_assembly=main_assembly, native_tool=tool
    )
    write_json(staging / CAPTURE_MANIFEST, manifest)
    receipt["capture_archive"] = {
        "name": CAPTURE_ARCHIVE,
        "sha256": file_digest(archive),
        "size": archive.stat().st_size,
        "manifest_name": CAPTURE_MANIFEST,
        "manifest_sha256": file_digest(staging / CAPTURE_MANIFEST),
    }
    _stamp(staging, receipt)
    _install(staging, output)
    return {**receipt, "output": str(output)}


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
    resume_from: str | None = None,
    seed_dir: Path | None = None,
    expected_subject: str | None = None,
    resume: dict | None = None,
    capture_only: bool = False,
    main_assembly: str | None = None,
) -> dict:
    """Continue a prepared native job through capture, generation, verification and publication.

    ``capture_only`` stops after the capture stage with a sealed ``native-evidence.zip``
    transfer (``native_complete``, never ``passed``); the Windows endpoint uses it so the
    Linux side owns generation, verification and publication.  The remaining arguments are
    the portable continuation contract used by the Linux runner (``resume_from`` >=
    ``generate`` with a seeded staging directory).

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
    if capture_only and (repository is not None or resume_from not in (None, "capture")):
        raise PipelineError("Capture-only runs cannot publish or resume past capture")
    with output_lock(output) as output:
        if not _owned_output(output):
            raise PipelineError(f"Output contains unrelated files: {output}")
        staging = Path(tempfile.mkdtemp(prefix=f".{output.name}-capture-", dir=output.parent))
        prior = [event for event in (prior_events or []) if isinstance(event, dict)]
        restart = STAGE_IDS.index(resume_from) if resume_from in STAGE_IDS else STAGE_IDS.index("capture")
        receipt = {
            "schema_version": BUNDLE_SCHEMA,
            "pipeline_id": PIPELINE_ID,
            "run_id": run_id or str(uuid.uuid4()),
            "handoff_sha256": handoff_sha256,
            "state": "failed",
            "events": prior,
            "execution_scope": [stage for stage in STAGE_IDS if any(event.get("stage") == stage for event in prior)]
            + (
                ["capture"]
                if capture_only
                else [stage for stage in ("capture", "generate", "verify") if STAGE_IDS.index(stage) >= restart]
                + (["publish"] if repository else [])
            ),
        }
        if isinstance(resume, dict) and resume:
            receipt["resume"] = {"parent_run": resume.get("parent_run"), "from_stage": resume.get("from_stage")}
        event = _event_sink(receipt, on_event)

        try:
            from .sources.solidworks.input import resolve_package
            from .steps import capture_evidence, generate_model, publish_model, verify_delivery

            if resume_from is not None and resume_from not in {"capture", "generate", "verify", "publish"}:
                raise PipelineError(f"Resume is not supported from {resume_from!r}")
            if resume_from in (None, "capture"):
                definition, input_report = capture_evidence(
                    package,
                    staging,
                    backend=backend,
                    on_event=event,
                    expected_inputs=expected_inputs,
                    handoff_sha256=handoff_sha256,
                )
                receipt["cad_revision"] = input_report["cad_revision"]["revision"]
                if capture_only:
                    return _finish_native_capture(
                        staging,
                        output,
                        receipt,
                        run_id=receipt["run_id"],
                        handoff_sha256=handoff_sha256,
                        main_assembly=main_assembly,
                    )
            else:
                _seed_resume(staging, seed_dir, resume_from)
                input_report = read_data(staging / "reports/input.json")
                from .steps import inspect_prepared_input

                replayed = inspect_prepared_input(staging / "input")
                if replayed.get("passed") is not True:
                    raise PipelineError("Retained native inputs no longer pass static validation")
                if inventory(staging / "input") != input_report.get("package_files"):
                    raise PipelineError("Retained input bytes do not match the recorded inspection receipt")
                for key in ("input", "cad_revision"):
                    if replayed.get(key) != input_report.get(key):
                        raise PipelineError("Retained input report does not match its archived native inputs")
                receipt_rows = (input_report.get("input_receipt") or {}).get("inventory")
                if receipt_rows and (replayed.get("input_receipt") or {}).get("inventory") != receipt_rows:
                    raise PipelineError("Retained input inspection receipt does not match its archived native inputs")
                definition = resolve_package(input_report)
                receipt["cad_revision"] = input_report["cad_revision"]["revision"]
            if resume_from in (None, "capture", "generate"):
                generated_subject = generate_model(staging, definition, input_report, on_event=event)
            else:
                if not expected_subject:
                    raise PipelineError("Resumed verification requires the recorded subject")
                generated_subject = digest(subject_inventory(staging))
                if generated_subject != expected_subject:
                    raise PipelineError("Generated subject does not match the recorded delivery")
            if resume_from in (None, "capture", "generate", "verify"):
                report = verify_delivery(staging, generated_subject, on_event=event)
            else:
                from .verification.solidworks_urdf import check_bundle, require_qualified_report

                report = require_qualified_report(check_bundle(staging))
                if expected_subject and report.get("subject_sha256") != expected_subject:
                    raise PipelineError("Verified report is bound to a different subject")
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
    """Check the pinned runtime and readiness required by this host's role."""
    role = runtime_role()
    results = [{"id": "python", "passed": sys.version_info[:2] == (3, 12), "actual": platform.python_version()}]
    for name in required_packages(role):
        expected = RUNTIME_VERSIONS[name]
        try:
            actual = importlib.metadata.version(name)
            results.append({"id": name, "passed": actual == expected, "actual": actual, "expected": expected})
        except importlib.metadata.PackageNotFoundError:
            results.append({"id": name, "passed": False, "message": "Reinstall the pinned release runtime"})
    native = False
    native_detail = "Native CAD capture runs on the Windows worker"
    if role == "native":
        try:
            detail = native_readiness()
            native, native_detail = True, detail["solidworks_executable"]
            results.append({"id": "native.solidworks", "passed": True, "details": detail})
        except Exception as error:
            native_detail = str(error)
            results.append({"id": "native.solidworks", "passed": False, "message": str(error)})
    else:
        from .verification.consumer import readiness

        try:
            results.append({"id": "consumer.urdf", "passed": True, "details": readiness()})
        except Exception as error:
            results.append(
                {
                    "id": "consumer.urdf", "passed": False,
                    "message": str(error), "details": getattr(error, "details", {}),
                }
            )
    result = {
        "pipeline_id": PIPELINE_ID,
        "version": __version__,
        "role": role,
        "passed": all(item["passed"] for item in results),
        "checks": results,
        "native_capture_available": native,
        "native_capture_detail": native_detail,
    }
    if role == "portable":
        result.update(
            git_available=shutil.which("git") is not None, github_cli_available=shutil.which("gh") is not None
        )
    return result
