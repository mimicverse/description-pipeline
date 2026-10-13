"""One stage-bounded path from a saved CAD package to an independently checked URDF.

The native host freezes, discovers and captures; the sealed transfer then continues on the
portable host through generation, independent verification and publication.
"""

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
from .stages import DELIVERY_STAGES, STAGE_IDS, stage_view


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


#: The checkpoint parts each portable stage seeds from its predecessor.  A stage
#: only consumes the boundary recorded here; the caller names the stage and its
#: checkpoint explicitly instead of switching behaviour inside a runner.
_SEED_PARTS = {
    "generate": ("input", "evidence"),
    "verify": ("input", "evidence", "model", "urdf", "meshes", "tool.json", "manifest.json"),
    "publish": (
        "input",
        "evidence",
        "model",
        "urdf",
        "meshes",
        "tool.json",
        "manifest.json",
        "reports/quality.json",
    ),
}


def _seed_checkpoint(staging: Path, seed_dir: Path | None, boundary: str) -> None:
    """Copy the recorded boundary checkpoint of a prior owned stage (read-only)."""

    if seed_dir is None or not Path(seed_dir).is_dir():
        raise PipelineError("Stage checkpoints are unavailable")
    seed = Path(seed_dir)
    for part in _SEED_PARTS[boundary]:
        source = seed / part
        target = staging / part
        if source.is_dir():
            shutil.copytree(source, target)
        elif source.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        else:
            raise PipelineError(f"Stage checkpoint is missing: {part}")
    for name in ("reports/native-tool.json", "reports/native-stages.json", "transfer-manifest.json"):
        source = seed / name
        if source.is_file():
            target = staging / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)


def _replay_seeded_inputs(staging: Path) -> tuple[dict, dict]:
    """Rebind a seeded checkpoint to its frozen native inputs.

    Reuse only stands after the retained inputs still pass their static
    inspection and match the recorded report row for row.
    """

    input_report = read_data(staging / "input/handoff.json")
    from .steps import inspect_prepared_input

    replayed = inspect_prepared_input(staging / "input")
    if replayed.get("passed") is not True:
        raise PipelineError("Retained native inputs no longer pass static validation")
    retained_files = {
        path: checksum for path, checksum in inventory(staging / "input").items() if path != "handoff.json"
    }
    if retained_files != input_report.get("package_files"):
        raise PipelineError("Retained input bytes do not match the recorded inspection receipt")
    for key in ("input", "cad_revision"):
        if replayed.get(key) != input_report.get(key):
            raise PipelineError("Retained input report does not match its archived native inputs")
    receipt_rows = (input_report.get("input_receipt") or {}).get("inventory")
    replayed_rows = (replayed.get("input_receipt") or {}).get("inventory")
    if isinstance(replayed_rows, list):
        # The frozen handoff record lives inside input/ but is not part of the
        # archived native package inventory.
        replayed_rows = [row for row in replayed_rows if row.get("path") != "handoff.json"]
    if receipt_rows and replayed_rows != receipt_rows:
        raise PipelineError("Retained input inspection receipt does not match its archived native inputs")
    # Resolve from the fresh inspection of the archived input: its receipt root is
    # this host's local package directory, while the frozen report records the
    # producing host's absolute root (a Windows path on a portable continuation).
    # The equality checks above already bound the replay to the frozen report and
    # its inventory rows, so no inventory check is skipped.
    from .sources.solidworks.input import resolve_package

    return resolve_package(replayed), input_report


def _require_subject(staging: Path, expected_subject: str | None) -> str:
    """The stage stays bound to the recorded subject of its seeded checkpoint."""

    if not expected_subject:
        raise PipelineError("The stage requires the recorded subject of the previous checkpoint")
    generated_subject = digest(subject_inventory(staging))
    if generated_subject != expected_subject:
        raise PipelineError("Generated subject does not match the recorded delivery")
    return generated_subject


def _scope(prior_events, stage: str) -> list[str]:
    """The executed scope of one stage call: the prior stages plus the stage itself."""

    events = [event for event in (prior_events or []) if isinstance(event, dict)]
    return [item for item in STAGE_IDS if any(event.get("stage") == item for event in events)] + [stage]


def _stage_receipt(stage: str, run_id, prior_events, *, resume, handoff_sha256) -> dict:
    """The receipt one stage call starts from; the run store keeps the recorded copy."""

    receipt = {
        "schema_version": BUNDLE_SCHEMA,
        "pipeline_id": PIPELINE_ID,
        "run_id": run_id or str(uuid.uuid4()),
        "handoff_sha256": handoff_sha256,
        "state": "failed",
        "events": [event for event in (prior_events or []) if isinstance(event, dict)],
        "execution_scope": _scope(prior_events, stage),
    }
    if isinstance(resume, dict) and resume:
        receipt["resume"] = {"parent_run": resume.get("parent_run"), "from_stage": resume.get("from_stage")}
    return receipt


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


#: The delivery stage set; a receipt renders a report only when its executed
#: scope declares at least one of these stages (the native boundary receipts
#: carry no delivery scope, so the native host renders nothing).
_PORTABLE_SCOPE = frozenset(DELIVERY_STAGES)


def _stamp(root: Path, receipt: dict) -> None:
    # The native host renders no report: only a receipt whose executed scope
    # includes a portable stage (generate/verify/publish) renders one, and the
    # Linux portable stages re-create the structural-team report from this same
    # receipt inside their own delivery.
    scope = receipt.get("execution_scope")
    if isinstance(scope, (list, tuple)) and any(stage in _PORTABLE_SCOPE for stage in scope):
        from .structural_report import write_structural_report

        receipt["structural_report"] = write_structural_report(root, receipt, evidence_root=root)
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
    """The native-role tool identity for the sealed capture provenance."""

    from .runtime import tool_record

    return tool_record(role="native")


def _finish_native_capture(staging, output, receipt, *, run_id, handoff_sha256, main_assembly):
    """Close a capture-only run: provenance reports, sealed transfer, native_complete."""

    tool = _native_tool_record()
    write_json(staging / "reports/native-tool.json", tool)
    receipt.update(state="native_complete", passed=False, native_complete=True, native_tool=tool, stage="capture")
    _stamp(staging, receipt)
    # The native receipt must stay stable even when Linux rewrites reports/stages.json, and
    # it keeps the raw events (with timestamps) so the portable side can seed prior_events.
    native_stages = read_data(staging / "reports/stages.json")
    native_stages["events"] = list(receipt["events"])
    write_json(staging / "reports/native-stages.json", native_stages)
    from .orchestration.stage_transfer import CAPTURE_ARCHIVE, CAPTURE_MANIFEST, seal_capture

    archive = staging / CAPTURE_ARCHIVE
    manifest = seal_capture(
        staging, archive, run_id=run_id, handoff_sha256=handoff_sha256, main_assembly=main_assembly, native_tool=tool
    )
    write_json(staging / CAPTURE_MANIFEST, manifest)
    receipt["capture_archive"] = {
        "name": CAPTURE_ARCHIVE,
        "sha256": file_digest(archive),
        "size": archive.stat().st_size,
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


def capture(
    package: Path,
    output: Path,
    *,
    backend=None,
    run_id: str | None = None,
    on_event=None,
    on_activity=None,
    prior_events=None,
    expected_inputs=None,
    handoff_sha256=None,
    main_assembly: str | None = None,
    resume: dict | None = None,
) -> dict:
    """The native boundary entry: freeze, discover and capture one prepared package.

    The stage seals the native transfer archive and closes at ``native_complete``
    (never ``passed``): generation, independent verification and publication are
    separate portable stages on Linux.  ``backend`` is an internal
    dependency-injection seam for native regressions; the endpoint always uses
    the native SolidWorks backend.  ``on_activity`` optionally observes
    best-effort capture progress for the live-activity view; progress can never
    change a run.
    """

    package = Path(package)
    if package.is_symlink() or package.is_junction():
        raise PipelineError("The input package cannot be a symlink or junction")
    package = package.resolve()
    output = Path(output)
    if output.resolve().is_relative_to(package) or package.is_relative_to(output.resolve()):
        raise PipelineError("Input and output directories must be separate")
    with output_lock(output) as output:
        if not _owned_output(output):
            raise PipelineError(f"Output contains unrelated files: {output}")
        staging = Path(tempfile.mkdtemp(prefix=f".{output.name}-capture-", dir=output.parent))
        receipt = _stage_receipt("capture", run_id, prior_events, resume=resume, handoff_sha256=handoff_sha256)
        event = _event_sink(receipt, on_event)
        try:
            from .steps import capture_evidence

            _, input_report = capture_evidence(
                package,
                staging,
                backend=backend,
                on_event=event,
                on_activity=on_activity,
                expected_inputs=expected_inputs,
                handoff_sha256=handoff_sha256,
            )
            receipt["cad_revision"] = (input_report["cad_revision"] or {}).get("revision")
            return _finish_native_capture(
                staging,
                output,
                receipt,
                run_id=receipt["run_id"],
                handoff_sha256=handoff_sha256,
                main_assembly=main_assembly,
            )
        except Exception as error:
            return _failed_run(staging, output, receipt, event, error)
        finally:
            if staging.exists():
                shutil.rmtree(staging)


def generate(
    seed_dir: Path,
    output: Path,
    *,
    run_id: str | None = None,
    prior_events=None,
    resume: dict | None = None,
    handoff_sha256=None,
    on_event=None,
) -> dict:
    """The generation stage: seed the capture checkpoint and build the delivery.

    The result is the unverified ``generated`` checkpoint; independent
    verification is always a separate stage bound to its recorded subject.
    """

    seed_dir = Path(seed_dir)
    output = Path(output)
    if output.resolve().is_relative_to(seed_dir.resolve()) or seed_dir.resolve().is_relative_to(output.resolve()):
        raise PipelineError("Stage checkpoints and output must be separate")
    with output_lock(output) as output:
        if not _owned_output(output):
            raise PipelineError(f"Output contains unrelated files: {output}")
        staging = Path(tempfile.mkdtemp(prefix=f".{output.name}-generate-", dir=output.parent))
        receipt = _stage_receipt("generate", run_id, prior_events, resume=resume, handoff_sha256=handoff_sha256)
        event = _event_sink(receipt, on_event)
        try:
            _seed_checkpoint(staging, seed_dir, "generate")
            definition, input_report = _replay_seeded_inputs(staging)
            receipt["cad_revision"] = (input_report["cad_revision"] or {}).get("revision")
            from .steps import generate_model

            generated_subject = generate_model(staging, definition, input_report, on_event=event)
            receipt.update(
                state="generated",
                passed=False,
                subject_sha256=generated_subject,
                hardware_id=definition["hardware_id"],
            )
            _stamp(staging, receipt)
            _install(staging, output)
            return {**receipt, "output": str(output)}
        except Exception as error:
            return _failed_run(staging, output, receipt, event, error)
        finally:
            if staging.exists():
                shutil.rmtree(staging)


def verify(
    seed_dir: Path,
    output: Path,
    *,
    run_id: str | None = None,
    expected_subject: str,
    prior_events=None,
    resume: dict | None = None,
    handoff_sha256=None,
    on_event=None,
) -> dict:
    """The independent-verification stage: recheck a seeded generated checkpoint."""

    seed_dir = Path(seed_dir)
    output = Path(output)
    if output.resolve().is_relative_to(seed_dir.resolve()) or seed_dir.resolve().is_relative_to(output.resolve()):
        raise PipelineError("Stage checkpoints and output must be separate")
    with output_lock(output) as output:
        if not _owned_output(output):
            raise PipelineError(f"Output contains unrelated files: {output}")
        staging = Path(tempfile.mkdtemp(prefix=f".{output.name}-verify-", dir=output.parent))
        receipt = _stage_receipt("verify", run_id, prior_events, resume=resume, handoff_sha256=handoff_sha256)
        event = _event_sink(receipt, on_event)
        try:
            _seed_checkpoint(staging, seed_dir, "verify")
            definition, input_report = _replay_seeded_inputs(staging)
            receipt["cad_revision"] = (input_report["cad_revision"] or {}).get("revision")
            generated_subject = _require_subject(staging, expected_subject)
            from .steps import verify_delivery

            report = verify_delivery(staging, generated_subject, on_event=event)
            receipt.update(
                hardware_id=definition["hardware_id"],
                subject_sha256=report.get("subject_sha256"),
                quality=report,
            )
            if report.get("passed") is not True:
                raise PipelineError("URDF verification failed; see reports/quality.json")
            receipt.update(state="verified", passed=True)
            _stamp(staging, receipt)
            _install(staging, output)
            return {**receipt, "output": str(output)}
        except Exception as error:
            return _failed_run(staging, output, receipt, event, error)
        finally:
            if staging.exists():
                shutil.rmtree(staging)


def publish(
    seed_dir: Path,
    output: Path,
    *,
    run_id: str | None = None,
    expected_subject: str,
    repository: Path,
    base: str | None = None,
    message: str | None = None,
    prior_events=None,
    resume: dict | None = None,
    handoff_sha256=None,
    on_event=None,
) -> dict:
    """The publication stage: seed the verified checkpoint and push its candidate PR.

    Publication is always a separate stage bound to the verified subject; a
    failed submission keeps the new verified delivery and its receipt so a
    linked attempt can reconcile without CAD.
    """

    seed_dir = Path(seed_dir)
    output = Path(output)
    if repository is None:
        raise PipelineError("Publication requires the configured model repository checkout")
    repository = Path(repository)
    if output.resolve().is_relative_to(seed_dir.resolve()) or seed_dir.resolve().is_relative_to(output.resolve()):
        raise PipelineError("Stage checkpoints and output must be separate")
    if output.resolve().is_relative_to(repository.resolve()) or repository.resolve().is_relative_to(output.resolve()):
        raise PipelineError("Build output and the model repository must be separate directories")
    with output_lock(output) as output:
        if not _owned_output(output):
            raise PipelineError(f"Output contains unrelated files: {output}")
        staging = Path(tempfile.mkdtemp(prefix=f".{output.name}-publish-", dir=output.parent))
        receipt = _stage_receipt("publish", run_id, prior_events, resume=resume, handoff_sha256=handoff_sha256)
        event = _event_sink(receipt, on_event)
        try:
            _seed_checkpoint(staging, seed_dir, "publish")
            definition, input_report = _replay_seeded_inputs(staging)
            receipt["cad_revision"] = (input_report["cad_revision"] or {}).get("revision")
            _require_subject(staging, expected_subject)
            from .verification.solidworks_urdf import check_bundle, require_qualified_report

            report = require_qualified_report(check_bundle(staging))
            if expected_subject and report.get("subject_sha256") != expected_subject:
                raise PipelineError("Verified report is bound to a different subject")
            receipt.update(
                hardware_id=definition["hardware_id"],
                subject_sha256=report.get("subject_sha256"),
                quality=report,
            )
            if report.get("passed") is not True:
                raise PipelineError("URDF verification failed; see reports/quality.json")
            receipt.update(state="verified", passed=True)
            _stamp(staging, receipt)
            _install(staging, output)
            from .steps import publish_model

            submitted = publish_model(output, repository, base=base, message=message, on_event=event)
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
                staging, definition, read_data(bundle / "input/handoff.json"), on_event=event
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
                    "id": "consumer.urdf",
                    "passed": False,
                    "message": str(error),
                    "details": getattr(error, "details", {}),
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
