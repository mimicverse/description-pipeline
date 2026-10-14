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
from .delivery import PIPELINE_ID, read_source_record, subject_digest, subject_inventory
from .io import PipelineError, confined, digest, file_digest, inventory, read_data, write_json
from .runtime import tool_record
from .sources.snapshot import verify_snapshot
from .sources.solidworks.revision import package_inventory
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
                raise PipelineError(f"The selected main assembly is not part of the frozen handoff: {main_assembly!r}")
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
    prepared=None,
    on_event=None,
    on_activity=None,
):
    """Derive mechanical semantics and bind their native observations to the handoff.

    ``on_activity`` observes the long native read for the live-activity view; it
    never changes the checks, the derived package or any failure path.
    """
    from .sources.solidworks.discovery import DiscoverySettings, prepare_native_package

    _state(on_event, "discover", "running")

    def discovery_inputs():
        _require(package_inventory(frozen) == expected_files, "Frozen engineering changed before discovery")
        if prepared is not None:
            payload = read_data(prepared.discovery_path)
            derivation = payload.get("derivation") if isinstance(payload, dict) else None
            _require(isinstance(derivation, dict), "Prepared discovery lacks its Linux derivation settings")
            _require(derivation.get("main_assembly") == main_assembly, "Linux derivation selected another assembly")
            names = derivation.get("frozen_names")
            record_roots = derivation.get("record_roots")
            _require(
                isinstance(record_roots, list) and all(isinstance(value, str) for value in record_roots),
                "Prepared discovery has invalid controlled-record root identifiers",
            )
        else:
            names = read_data(configuration["frozen_names_file"]) if configuration.get("frozen_names_file") else {}
            record_roots = configuration.get("record_roots", [])
        _require(
            isinstance(names, dict)
            and all(isinstance(key, str) and isinstance(value, str) for key, value in names.items()),
            "Frozen-name registry must map native identities to interface names",
        )
        return DiscoverySettings(
            record_roots=tuple(record_roots),
            frozen_names=names,
            main_assembly=main_assembly,
        )

    def describe_settings(value):
        described = {
            "handoff_sha256": expected_digest,
            "files_sha256": digest(expected_files),
            "frozen_names_sha256": digest(value.frozen_names),
            "record_roots": [str(root) for root in value.record_roots],
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
    if prepared is None:
        prepared = (preparer or prepare_native_package)(
            frozen, output, run_id, settings=settings, on_event=on_event, on_activity=on_activity
        )
    discovery = {
        "passed": prepared.passed,
        "findings": list(prepared.findings),
        "hardware_id": prepared.hardware_id,
        "revision": prepared.revision,
        "discovery_sha256": prepared.discovery_sha256,
    }
    target = targets.get(prepared.hardware_id)
    if target is None:
        discovery["findings"].append(
            {
                "code": "discovery.publication_target_missing",
                "object": "assembly",
                "message": "No model repository is configured for this CAD identity; publication is unavailable",
                "blocking": False,
                "detail": {"hardware_id": prepared.hardware_id, "publication_blocker": "repository_target"},
            }
        )
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
        inspected = inspect_prepared_input(package)
        _require(inspected["passed"], "Generated native inputs are invalid")
        identity = inspected["cad_revision"]
        if identity is not None:
            _require(
                identity["hardware_id"] == prepared.hardware_id and identity["revision"] == prepared.revision,
                "Generated content identity differs from native discovery",
            )
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
            "repository_slug": (value[1] or {}).get("repository_slug"),
            "base": (value[1] or {}).get("base"),
            "prepared_sha256": digest(value[2]),
            "files": {"input/" + name: checksum for name, checksum in value[2].items()},
        },
    )
    _state(on_event, "discover", "completed")
    return package, target, prepared, files


def inspect_prepared_input(package: Path) -> dict:
    """Check the prepared native package's archived content identity.

    The package carries its identity in ``source.json`` (IF-06).  A missing
    record defers publication only: acquisition evidence stays inspectable and
    the deferral is recorded.  An unreadable or inconsistent record fails.
    """

    from .interface_records import validate_source_record

    package = Path(package)
    report = {
        "passed": True,
        "errors": [],
        "warnings": [],
        "input": None,
        "resolved": None,
        "input_receipt": None,
        "cad_revision": None,
    }
    path = package / "source.json"
    if not path.is_file():
        report["warnings"].append(
            {
                "code": "input.source_identity_pending",
                "message": "CAD administrative identity is incomplete; publication is unavailable",
                "detail": {"publication_blocker": "source_identity"},
            }
        )
        return report
    try:
        source = validate_source_record(read_data(path))
        record_path = package / "discovery/native-discovery.json"
        if record_path.is_file():
            record = read_data(record_path)
            receipt = {str(name): str(value) for name, value in (record.get("native_files") or {}).items()}
        elif (package / "cad").is_dir():
            receipt = dict(inventory(package / "cad"))
        else:
            receipt = dict(package_inventory(package))
    except (PipelineError, OSError, ValueError) as error:
        report["passed"] = False
        report["errors"].append({"code": "input.source_identity", "message": str(error), "detail": None})
        return report
    identity, metadata = source["identity"], source["metadata"]
    report["main_assembly"] = identity["main_assembly"]
    report["configuration"] = identity["configuration"]
    report["input"] = {
        "hardware_id": identity["hardware_id"],
        "source": {"assembly": identity["main_assembly"], "configuration": identity["configuration"]},
    }
    report["cad_revision"] = {
        "hardware_id": identity["hardware_id"],
        "revision": metadata["revision"],
        "owner": metadata["owner"],
        "control": metadata["control"],
        "change_summary": metadata["change_summary"],
    }
    report["input_receipt"] = {
        "inventory": dict(sorted(receipt.items())),
    }
    return report


def _archive_input(package: Path, staging: Path) -> None:
    """Archive the frozen input as the delivery tree: identity, CAD originals, records.

    The prepared package keeps its own layout while it is the CAD read source;
    the delivered tree is the target layout - the content identity at
    ``input/source.json``, the CAD originals under ``input/cad`` at their
    frozen-relative paths and the embedded controlled records under
    ``input/records``.  Nothing is duplicated.
    """

    from .sources.solidworks.discovery import DISCOVERY_FILE

    payload = read_data(package / DISCOVERY_FILE)
    native_files = payload.get("native_files") if isinstance(payload.get("native_files"), dict) else {}
    destination = staging / "input"
    (destination / "cad").mkdir(parents=True, exist_ok=True)
    (destination / "records").mkdir(parents=True, exist_ok=True)
    archived: dict[str, str] = {}
    for relative in sorted(native_files):
        target = confined(destination / "cad", relative, exists=False)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(confined(package, relative), target)
        archived[relative] = file_digest(target)
    if archived != {name: str(value) for name, value in native_files.items()}:
        raise PipelineError("Archived CAD originals differ from the frozen inventory")
    entries = list(payload.get("records") or [])
    if isinstance(payload.get("budget"), dict):
        entries.append(payload["budget"])
    copied_records: set[str] = set()
    for entry in entries:
        package_file = str(entry.get("package_file") or "")
        if not package_file or package_file in copied_records:
            continue
        copied_records.add(package_file)
        target = confined(destination / "records", package_file, exists=False)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(confined(package, package_file), target)
    source_path = package / "source.json"
    if source_path.is_file():
        shutil.copyfile(source_path, destination / "source.json")


def _rehome_evidence(staging: Path, package: Path) -> None:
    """Write the frozen delivery evidence: capture, runtime and discovery records.

    The freeze keeps its own snapshot beside these records; the delivery reads
    one capture record (the verbatim blocks), one runtime record (the capture
    environment) and the frozen discovery record with its capture additions.
    """

    from .sources.solidworks.discovery import DISCOVERY_FILE

    evidence = staging / "evidence"
    blocks = {
        "scene": "raw/scene_raw.json",
        "mass_closure": "raw/mass_closure.json",
        "material_provenance": "raw/material_provenance.json",
        "declared_masses": "raw/declared_masses.json",
        "dependency_closure": "raw/dependency_closure.json",
        "link_group_mass_properties": "raw/link_group_mass_properties.json",
        "effective_instances": "raw/effective_instances.json",
        "geometry": "raw/geometry.json",
        "axes": "raw/axis_references.json",
        "collection": "evidence/collection.json",
    }
    capture = {name: read_data(evidence / relative) for name, relative in blocks.items()}
    capture["components"] = [
        {"name": str(name), "mass_properties": value}
        for name, value in sorted((read_data(evidence / "raw/mass_properties.json") or {}).items())
    ]
    capture["datums"] = [
        {"name": str(name), "matrix": matrix}
        for name, matrix in sorted((read_data(evidence / "raw/coordinate_systems.json") or {}).items())
    ]
    write_json(evidence / "capture.json", capture)
    write_json(evidence / "native-runtime.json", read_data(evidence / "evidence/environment.json"))
    payload = read_data(package / DISCOVERY_FILE)
    identity = payload.get("identity") if isinstance(payload.get("identity"), dict) else {}
    rows = [
        {"path": name, "sha256": checksum}
        for name, checksum in sorted((payload.get("native_files") or {}).items())
    ]
    write_json(
        evidence / "discovery.json",
        {
            **payload,
            "files": rows,
            "native_inventory_sha256": digest(rows),
            "robot_name": identity.get("robot_name") or identity.get("robot_name_technical") or "robot",
        },
    )
    manifest_path = evidence / "manifest.json"
    manifest = read_data(manifest_path)
    files = dict(manifest.get("files") or {})
    for name in ("capture.json", "discovery.json", "native-runtime.json"):
        files[name] = file_digest(evidence / name)
    manifest["files"] = dict(sorted(files.items()))
    write_json(manifest_path, manifest)


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
    package: Path,
    staging: Path,
    *,
    expected_inputs=None,
    handoff_sha256=None,
    backend=None,
    on_event=None,
    on_activity=None,
):
    """Inspect generated inputs, archive them and capture a complete immutable snapshot.

    ``on_activity`` optionally observes best-effort capture progress records for the
    live-activity view; it never changes the snapshot, its checks or a failure path.
    """
    from .sources.solidworks.discovery import source_definition
    from .sources.solidworks.freeze import freeze
    from .sources.solidworks.input import native_source_closure

    _state(on_event, "capture", "running")

    def inspect_and_archive():
        if expected_inputs is not None:
            _require(package_inventory(package) == expected_inputs, "Prepared inputs changed after discovery")
        report = inspect_prepared_input(package)
        if report.get("passed") is not True:
            write_json(staging / "input/handoff.json", report)
            error = PipelineError("The CAD package does not meet the input specification")
            error.details = {"errors": report.get("errors", [])}
            raise error
        source = source_definition(package)
        source["closure"] = native_source_closure(package)
        definition = {"source": source}
        prepared_files = package_inventory(package)
        _archive_input(package, staging)
        report["package_files"] = dict(sorted(inventory(staging / "input").items()))
        write_json(staging / "input/handoff.json", report)
        return definition, report, prepared_files

    definition, input_report, original_files = checked(
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
            "warnings": value[1].get("warnings", []),
            "files": {"input/" + path: checksum for path, checksum in value[1]["package_files"].items()},
        },
    )

    def ready():
        if backend is None and sys.platform != "win32":
            raise PipelineError("Native SLDASM/SLDPRT capture requires Windows with licensed SolidWorks")
        if backend is None:
            from .runtime import native_readiness

            return native_readiness()
        return {"scope": "provided CAD backend; native qualification is recorded in the capture evidence"}

    checked(on_event, "capture", "input", "runtime.ready", ready)
    _require(package_inventory(package) == original_files, "Prepared inputs changed before native capture")
    freeze(
        definition["source"],
        staging / "evidence",
        backend=backend,
        worker_version=__version__,
        on_activity=on_activity,
    )
    checked(
        on_event,
        "capture",
        "output",
        "capture.integrity",
        lambda: verify_snapshot(staging / "evidence"),
        describe=lambda value: _snapshot_details(staging, value),
    )
    _rehome_evidence(staging, package)

    def stable_inputs():
        if package_inventory(package) != original_files:
            raise PipelineError("The supplied package changed during capture; save it and rerun")
        return {"files": {"input/" + path: checksum for path, checksum in original_files.items()}}

    checked(on_event, "capture", "output", "capture.input_stability", stable_inputs)
    _state(on_event, "capture", "completed")
    return definition, input_report


def generate_model(staging: Path, definition: dict, input_report: dict, *, on_event=None) -> str:
    """Generate one canonical model and bind every derived file before independent checking."""
    from .backends.urdf import generate_urdf
    from .model import Robot
    from .sources.solidworks.scene import load_scene

    _state(on_event, "generate", "running")
    checked(
        on_event,
        "generate",
        "input",
        "generation.inputs",
        lambda: verify_snapshot(staging / "evidence"),
        describe=lambda value: _snapshot_details(staging, value),
    )
    name = definition["source"]["robot_name"]
    write_json(staging / "tool.json", tool_record())
    robot = Robot.from_dict(load_scene(staging / "evidence"))
    write_json(staging / "model/robot.json", robot.to_dict())
    generate_urdf(robot, staging / "evidence", staging, name=name)
    from .delivery import write_delivery_manifest
    from .interface_records import CHECK_POLICY_SCHEMA, check_policy_digest
    from .stages import CONTRACT, CONTRACT_SHA256

    write_delivery_manifest(
        staging,
        contract={"schema_version": CONTRACT["schema_version"], "digest": CONTRACT_SHA256},
        policy={"schema_version": CHECK_POLICY_SCHEMA, "digest": check_policy_digest()},
    )

    def generated():
        Robot.from_dict(read_data(staging / "model/robot.json"))
        files = subject_inventory(staging)
        return {"subject_sha256": digest(files), "files": files}

    generated_subject = checked(on_event, "generate", "output", "generation.artifacts", generated)["subject_sha256"]
    _state(on_event, "generate", "completed")
    return generated_subject


def verify_delivery(staging: Path, generated_subject: str, *, on_event=None) -> dict:
    """Reconstruct expectations from native evidence and inspect the actual delivery."""
    from .verification.solidworks_urdf import (
        check_bundle,
        evaluate_bundle,
        rebuilt_definition,
        require_qualified_report,
    )

    _state(on_event, "verify", "running")

    def verify_subject():
        actual = subject_digest(staging)
        if actual != generated_subject:
            raise PipelineError("Generated files changed before independent verification")
        return {"subject_sha256": actual}

    checked(on_event, "verify", "input", "verification.subject", verify_subject)
    report = evaluate_bundle(staging)
    write_json(staging / "reports/quality.json", report)
    checked(
        on_event,
        "verify",
        "output",
        "verification.gates",
        lambda: require_qualified_report(report, rebuilt_definition(staging), root=staging),
    )
    report = checked(
        on_event,
        "verify",
        "output",
        "verification.report_binding",
        lambda: require_qualified_report(check_bundle(staging), rebuilt_definition(staging), root=staging),
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
    """Recheck and submit the frozen delivery on the Linux publication stage."""

    from .repository.urdf_pr import submit_bundle

    _state(on_event, "publish", "running")
    # Fail closed on degraded installs: a source/PYTHONPATH checkout carries no
    # embedded release identity, so the split-workflow gate compares empty
    # release records.  Publication must never bind a PR to an unidentified
    # tool (ORC-04).
    tool = tool_record()
    _require(
        isinstance(tool.get("release"), dict) and bool(tool.get("release")),
        "Refusing to publish: this runtime carries no embedded release identity "
        "(tool-release.json is absent); install the released distribution",
    )
    source = read_source_record(bundle)
    hardware = source["identity"]["hardware_id"]
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
