"""The six engineering steps, each with real input and output checks.

The endpoint serializes freeze/discovery; the delivery driver sequences
capture/generation/verification/publication. Native adapters and the independent
verifier retain their own rules. No step is an Airflow polling operation.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .delivery import read_delivery_manifest, subject_digest, subject_inventory
from .interface_records import PURPOSE_ENGINEERING, REQUEST_PURPOSES
from .io import PipelineError, confined, digest, file_digest, inventory, read_data, write_json
from .native_transfer import load_native_receipts
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
            record_roots = derivation.get("record_roots")
            _require(
                isinstance(record_roots, list) and all(isinstance(value, str) for value in record_roots),
                "Prepared discovery has invalid controlled-record root identifiers",
            )
        else:
            record_roots = configuration.get("record_roots", [])
        purpose = configuration.get("purpose") if isinstance(configuration, dict) else None
        if prepared is None:
            _require(
                isinstance(purpose, str) and purpose in REQUEST_PURPOSES,
                "The derivation purpose must come from the frozen request",
            )
        else:
            # The prepared package was already derived under the request-bound
            # purpose; this echo is inert for the validation-only path.
            purpose = purpose if isinstance(purpose, str) and purpose in REQUEST_PURPOSES else PURPOSE_ENGINEERING
        return DiscoverySettings(
            record_roots=tuple(record_roots),
            main_assembly=main_assembly,
            purpose=str(purpose),
        )

    def describe_settings(value):
        described = {
            "handoff_sha256": expected_digest,
            "files_sha256": digest(expected_files),
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
    # The publication-target decision is the platform request's authority; a
    # missing native routing entry is no longer a discovery finding.
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


def generate_model(staging: Path, definition: dict, input_report: dict, *, on_event=None) -> str:
    """Generate one canonical model and bind every derived file before independent checking."""
    from .model import EngineeringModel, compile, emit
    from .sources.solidworks.captured import derive_engineering_input
    from .sources.solidworks.generation_warnings import generation_value_warnings

    _state(on_event, "generate", "running")
    manifest = checked(
        on_event,
        "generate",
        "input",
        "generation.inputs",
        lambda: verify_snapshot(staging / "evidence"),
        describe=lambda value: _snapshot_details(staging, value),
    )
    write_json(staging / "tool.json", tool_record())
    model_input = derive_engineering_input(staging / "evidence", manifest)
    # URDF comments are written before the verify stage.  Derive their native
    # geometry claims from evidence here; _value_warning_gate recomputes them
    # independently and refuses any missing, forged, or stale model warning.
    generation_warnings = generation_value_warnings(staging)
    model = compile(model_input, native_warnings=generation_warnings)
    if model.name != definition["source"]["robot_name"]:
        raise PipelineError("Compiled model name differs from the frozen source identity")
    emit(model, evidence_root=staging / "evidence", destination=staging)
    from .delivery import write_delivery_manifest
    from .interface_records import CHECK_POLICY_SCHEMA, check_policy_digest
    from .stages import CONTRACT, CONTRACT_SHA256

    write_delivery_manifest(
        staging,
        contract={"schema_version": CONTRACT["schema_version"], "digest": CONTRACT_SHA256},
        policy={"schema_version": CHECK_POLICY_SCHEMA, "digest": check_policy_digest()},
    )

    def generated():
        EngineeringModel.from_dict(read_data(staging / "model/robot.json"))
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
    receipts = load_native_receipts(staging)
    report = evaluate_bundle(
        staging,
        native_receipts=receipts,
        require_native_receipts=True,
    )
    write_json(staging / "reports/quality.json", report)
    checked(
        on_event,
        "verify",
        "output",
        "verification.gates",
        lambda: require_qualified_report(report, rebuilt_definition(staging), root=staging),
    )
    def replay_and_reread():
        replayed = require_qualified_report(
            check_bundle(
                staging,
                native_receipts=receipts,
                require_native_receipts=True,
            ),
            rebuilt_definition(staging),
            root=staging,
        )
        current_subject = subject_digest(staging)
        current_receipts = load_native_receipts(staging)
        if (
            current_subject != generated_subject
            or replayed.get("subject_sha256") != generated_subject
            or current_receipts != receipts
        ):
            raise PipelineError("Verification inputs changed after the final report was replayed")
        manifest = read_delivery_manifest(staging)
        if manifest["subject"]["digest"] != generated_subject:
            raise PipelineError("Verification inputs changed after the final report was replayed")
        return replayed

    report = checked(
        on_event,
        "verify",
        "output",
        "verification.report_binding",
        replay_and_reread,
        describe=lambda value: {
            "passed": value.get("passed"),
            "subject_sha256": value.get("subject_sha256"),
            "report_sha256": file_digest(staging / "reports/quality.json"),
        },
    )
    _state(on_event, "verify", "completed")
    return report
