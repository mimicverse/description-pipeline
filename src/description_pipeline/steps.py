"""The six engineering steps, each with real input and output checks.

The endpoint serializes freeze/discovery; the delivery driver sequences
capture/generation/verification/publication. Native adapters and the independent
verifier retain their own rules. No step is an Airflow polling operation.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
from pathlib import Path

from . import __version__
from .backends.urdf import generate_urdf
from .delivery import PIPELINE_ID, subject_digest, subject_inventory
from .io import PipelineError, confined, digest, file_digest, read_data, write_json
from .model import Robot
from .runtime import tool_record
from .sources.snapshot import verify_snapshot
from .sources.solidworks.revision import package_inventory, read_revision
from .sources.solidworks.scene import load_scene
from .stages import checked


def _state(emit, stage, state, **details):
    if emit is not None:
        emit({"stage": stage, "state": state, **details})


def _require(passed, message):
    if not passed:
        raise PipelineError(message)


def freeze_inputs(
    package: Path,
    expected_digest: str,
    expected_files: dict,
    *,
    main_assembly: str | None = None,
    on_event=None,
) -> dict:
    """Admit the frozen native bytes before the queue consumes them."""
    from .sources.solidworks.handoff import describe_handoff

    _state(on_event, "freeze", "running")

    def admitted():
        identity = describe_handoff(package)
        _require(identity["handoff_sha256"] == expected_digest, "Native handoff differs from its requested digest")
        return {"handoff_sha256": expected_digest, "package": str(package)}

    checked(on_event, "freeze", "input", "handoff.admission", admitted)

    def bound():
        files = package_inventory(package)
        _require(files == expected_files, "Native inputs changed while the job was queued")
        detail = {
            "handoff_sha256": expected_digest,
            "files": {"handoff/" + path: checksum for path, checksum in files.items()},
        }
        if main_assembly is not None:
            if main_assembly not in files:
                mismatched = sorted(name for name in files if name.casefold() == main_assembly.casefold())
                if mismatched:
                    raise PipelineError(
                        "The selected main assembly case does not match the frozen handoff file: "
                        f"{main_assembly!r} vs {mismatched[0]!r}"
                    )
                raise PipelineError(
                    f"The selected main assembly is not part of the frozen handoff: {main_assembly!r}"
                )
            detail["main_assembly"] = main_assembly
            detail["main_assembly_sha256"] = files[main_assembly]
        return detail

    identity = checked(on_event, "freeze", "output", "handoff.integrity", bound)
    _state(on_event, "freeze", "completed")
    return identity


def discover_structure(
    frozen: Path,
    output: Path,
    run_id: str,
    *,
    expected_digest: str,
    expected_files: dict,
    main_assembly: str | None = None,
    configuration: dict,
    targets: dict,
    preparer=None,
    on_event=None,
):
    """Derive mechanical semantics and bind their native observations to the handoff."""
    from .repository.urdf_pr import _origin_slug
    from .sources.solidworks.discovery import DiscoverySettings, prepare_native_package

    _state(on_event, "discover", "running")

    def discovery_inputs():
        _require(package_inventory(frozen) == expected_files, "Frozen engineering changed before discovery")
        names = read_data(configuration["frozen_names_file"]) if configuration.get("frozen_names_file") else {}
        _require(
            isinstance(names, dict)
            and all(isinstance(key, str) and isinstance(value, str) for key, value in names.items()),
            "Frozen-name registry must map native identities to interface names",
        )
        return DiscoverySettings(
            record_roots=tuple(configuration.get("record_roots", [])),
            frozen_names=names,
            main_assembly=main_assembly,
        )

    def describe_settings(value):
        described = {
            "handoff_sha256": expected_digest,
            "files_sha256": digest(expected_files),
            "frozen_names_sha256": digest(value.frozen_names),
        }
        if main_assembly is not None:
            described["main_assembly"] = main_assembly
        return described

    settings = checked(
        on_event,
        "discover",
        "input",
        "discovery.inputs",
        discovery_inputs,
        describe=describe_settings,
    )
    prepared = (preparer or prepare_native_package)(frozen, output, run_id, settings=settings, on_event=on_event)
    discovery = {
        "passed": prepared.passed,
        "findings": list(prepared.findings),
        "hardware_id": prepared.hardware_id,
        "revision": prepared.revision,
        "discovery_sha256": prepared.discovery_sha256,
    }
    _state(on_event, "discover", "running", discovery=discovery)
    checked(on_event, "discover", "output", "discovery.definition", lambda: discovery)

    def bind_discovery():
        _require(prepared.handoff_sha256 == expected_digest, "Native discovery is bound to a different handoff")
        package = Path(prepared.package).resolve()
        _require(package.is_relative_to(output.resolve()), "Native preparation returned an unmanaged package")
        record = Path(prepared.discovery_path).resolve()
        _require(
            record.is_relative_to(package) and file_digest(record) == prepared.discovery_sha256,
            "Prepared inputs lack the bound raw native discovery record",
        )
        _require(package_inventory(frozen) == expected_files, "Frozen engineering changed during discovery")
        for name, checksum in expected_files.items():
            _require(file_digest(confined(package, name)) == checksum, "Native preparation changed engineering files")
        revision = read_revision(package)
        _require(
            revision["hardware_id"] == prepared.hardware_id and revision["revision"] == prepared.revision,
            "Generated revision differs from native discovery",
        )
        target = targets.get(prepared.hardware_id)
        _require(target is not None, f"Hardware {prepared.hardware_id!r} has no configured model repository")
        files = package_inventory(package)
        _require(digest(files) == prepared.prepared_sha256, "Prepared files differ from native discovery")
        return package, target, files

    package, target, files = checked(
        on_event,
        "discover",
        "output",
        "discovery.binding",
        bind_discovery,
        describe=lambda value: {
            "discovery_sha256": prepared.discovery_sha256,
            "hardware_id": prepared.hardware_id,
            "revision": prepared.revision,
            "repository_slug": _origin_slug(value[1]["repository"]),
            "base": value[1]["base"],
            "prepared_sha256": digest(value[2]),
            "files": {"input/" + name: checksum for name, checksum in value[2].items()},
        },
    )
    _state(on_event, "discover", "completed")
    return package, target, prepared, files


def inspect_prepared_input(package: Path) -> dict:
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


def _archive_input(package: Path, staging: Path) -> None:
    """Archive the original handoff; evidence/source holds the relinked CAD copy."""

    destination = staging / "input"
    destination.mkdir()
    for name in package_inventory(package):
        original = confined(package, name)
        target = confined(destination, name, exists=False)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(original, target)


def _delivery_readme(hardware: str) -> str:
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


def _snapshot_details(staging, manifest):
    manifest_path = staging / "evidence/manifest.json"
    raw = manifest_path.read_bytes()
    try:
        parsed = json.loads(raw)
    except ValueError as error:
        raise PipelineError(f"Evidence manifest is not readable JSON: {error}") from error
    if parsed != manifest:
        raise PipelineError("Evidence manifest changed after verification")
    manifest_hash = hashlib.sha256(raw).hexdigest()
    return {
        "manifest_sha256": manifest_hash,
        "files": {
            "evidence/manifest.json": manifest_hash,
            **{"evidence/" + path: checksum for path, checksum in manifest["files"].items()},
        },
    }


def capture_evidence(
    package: Path, staging: Path, *, expected_inputs=None, handoff_sha256=None, backend=None, on_event=None
):
    """Inspect generated inputs, archive them and capture a complete immutable snapshot."""
    from .sources.solidworks.freeze import freeze
    from .sources.solidworks.input import resolve_package

    _state(on_event, "capture", "running")

    def inspect_and_archive():
        if expected_inputs is not None:
            _require(package_inventory(package) == expected_inputs, "Prepared inputs changed after discovery")
        report = inspect_prepared_input(package)
        write_json(staging / "reports/input.json", report)
        if report.get("passed") is not True:
            error = PipelineError("The CAD package does not meet the input specification")
            error.details = {"errors": report.get("errors", [])}
            raise error
        definition = resolve_package(report)
        files = {row["path"]: row["sha256"] for row in report["input_receipt"]["inventory"]}
        if expected_inputs is not None:
            _require(files == expected_inputs, "Inspected inputs differ from native discovery")
        _archive_input(package, staging)
        if package_inventory(staging / "input") != files:
            raise PipelineError("Archived input differs from its supplied package")
        report["package_files"] = files
        write_json(staging / "reports/input.json", report)
        return definition, report

    definition, input_report = checked(
        on_event,
        "capture",
        "input",
        "input.valid",
        inspect_and_archive,
        describe=lambda value: {
            "passed": value[1]["passed"],
            "handoff_sha256": handoff_sha256,
            "files_sha256": digest(value[1]["package_files"]),
            "cad_revision": value[1]["cad_revision"],
            "files": {"input/" + path: checksum for path, checksum in value[1]["package_files"].items()},
        },
    )
    original_files = input_report["package_files"]

    def ready():
        if backend is None and sys.platform != "win32":
            raise PipelineError("Native SLDASM/SLDPRT capture requires Windows with licensed SolidWorks")
        if backend is None:
            from .runtime import native_readiness

            return native_readiness()
        return {"scope": "injected regression backend; no native qualification"}

    checked(on_event, "capture", "input", "runtime.ready", ready)
    _require(package_inventory(package) == original_files, "Prepared inputs changed before native capture")
    freeze(definition["source"], staging / "evidence", backend=backend, worker_version=__version__)
    checked(
        on_event,
        "capture",
        "output",
        "capture.integrity",
        lambda: verify_snapshot(staging / "evidence"),
        describe=lambda value: _snapshot_details(staging, value),
    )

    def stable_inputs():
        if package_inventory(package) != original_files:
            raise PipelineError("The supplied package changed during capture; save it and rerun")
        return {"files": {"input/" + path: checksum for path, checksum in original_files.items()}}

    checked(on_event, "capture", "output", "capture.input_stability", stable_inputs)
    _state(on_event, "capture", "completed")
    return definition, input_report


def generate_model(staging: Path, definition: dict, input_report: dict, *, on_event=None) -> str:
    """Generate one canonical model and bind every derived file before independent checking."""
    _state(on_event, "generate", "running")
    checked(
        on_event,
        "generate",
        "input",
        "generation.inputs",
        lambda: verify_snapshot(staging / "evidence"),
        describe=lambda value: _snapshot_details(staging, value),
    )
    hardware = definition["hardware_id"]
    (staging / "README.md").write_text(_delivery_readme(hardware), encoding="utf-8", newline="\n")
    write_json(staging / "reports/input.json", input_report)
    write_json(staging / "reports/tool.json", tool_record())
    robot = Robot.from_dict(load_scene(staging / "evidence"))
    write_json(staging / "model/robot.json", robot.to_dict())
    generate_urdf(robot, staging / "evidence", staging, name=hardware)

    def generated():
        Robot.from_dict(read_data(staging / "model/robot.json"))
        files = subject_inventory(staging)
        return {"subject_sha256": digest(files), "files": files}

    generated_subject = checked(on_event, "generate", "output", "generation.artifacts", generated)["subject_sha256"]
    _state(on_event, "generate", "completed")
    return generated_subject


def verify_delivery(staging: Path, generated_subject: str, *, on_event=None) -> dict:
    """Reconstruct expectations from native evidence and inspect the actual delivery."""
    from .verification.solidworks_urdf import check_bundle, evaluate_bundle, require_qualified_report

    _state(on_event, "verify", "running")

    def verify_subject():
        actual = subject_digest(staging)
        if actual != generated_subject:
            raise PipelineError("Generated files changed before independent verification")
        return {"subject_sha256": actual}

    checked(on_event, "verify", "input", "verification.subject", verify_subject)
    report = evaluate_bundle(staging)
    write_json(staging / "reports/quality.json", report)
    checked(on_event, "verify", "output", "verification.gates", lambda: require_qualified_report(report))
    report = checked(
        on_event,
        "verify",
        "output",
        "verification.report_binding",
        lambda: require_qualified_report(check_bundle(staging)),
        describe=lambda value: {
            "passed": value.get("passed"),
            "subject_sha256": value.get("subject_sha256"),
            "report_sha256": file_digest(staging / "reports/quality.json"),
        },
    )
    _state(on_event, "verify", "completed")
    return report


def publish_model(
    bundle: Path, repository: Path, *, base: str | None = None, message: str | None = None, on_event=None
) -> dict:
    """Recheck and submit a frozen delivery from either Windows or Linux."""

    from .repository.urdf_pr import submit_bundle

    _state(on_event, "publish", "running")
    author = read_data(Path(bundle) / "input/robot.yaml")
    hardware = author["hardware_id"]
    result = submit_bundle(
        bundle,
        repository,
        base=base or f"feature/{hardware}",
        branch=f"work/solidworks/{hardware.lower()}",
        message=message,
        on_event=on_event,
    )
    result = {
        **result,
        "pipeline_id": PIPELINE_ID,
        "subject_sha256": result.get("subject"),
        "passed": bool(result.get("url")) and result.get("state") in {"published", "updated", "noop"},
    }

    write_json(Path(bundle) / "reports/pr.json", result)
    _state(on_event, "publish", "completed" if result["passed"] else "failed", error=result.get("error"))
    return result
