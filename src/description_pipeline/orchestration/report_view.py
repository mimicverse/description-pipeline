"""Readable report projection over recorded run evidence (display only).

Everything here is derived from data the pipeline already records — the stage contract view
(``stages.stage_view``), the delivered quality document and the job error/detail fields. This
module adds NO verification decisions: it relabels recorded states, distinguishes a failed
stage from downstream stages that were never executed, and surfaces exact measured values
(mass window and mass closure separately). Labels are display metadata keyed by existing check
identifiers; the stage contract itself is never changed, so historical evidence keeps its
recorded contract hashes.
"""

from __future__ import annotations

from pathlib import PureWindowsPath

from ..geometry.native_u import (
    NATIVE_SCOPE_IDENTITY_WARNING_CODES,
    native_scope_warning_details_match,
)
from ..io import PipelineError
from ..stages import FIXED_REQUIRED_CHECKS, stage_view

REPORT_SCHEMA = "solidworks-to-urdf.report/v1"

_STAGE_STATE_LABELS = {
    "completed": "Completed",
    "running": "In progress",
    "failed": "Failed",
    "blocked": "Blocked by upstream failure",
    "not_run": "Not run",
}
_CHECK_STATE_LABELS = {
    "passed": "Passed",
    "failed": "Failed",
    "not_run": "Not run (no result)",
}
_OVERALL_STATE_LABELS = {
    "passed": "Passed",
    "running": "In progress",
    "failed": "Failed",
    "not_run": "Not started",
}

_BOUNDARY_LABELS = {
    "handoff.admission": "Handoff admission (path, regular files, native package)",
    "handoff.integrity": "Handoff integrity (digest and file inventory)",
    "discovery.inputs": "Discovery inputs (native folder available)",
    "discovery.definition": "Structure definition generation",
    "discovery.binding": "Structure and identity binding",
    "input.valid": "Input validity (read-only snapshot semantics)",
    "runtime.ready": "Native runtime readiness",
    "capture.integrity": "Capture integrity (bytes and inventory)",
    "capture.input_stability": "Input-stability recheck",
    "generation.inputs": "Generation input validity",
    "generation.artifacts": "Generated artifact validation (URDF, meshes, report)",
    "verification.subject": "Delivery subject digest binding",
    "verification.gates": "Independent verification gates (aggregate)",
    "verification.report_binding": "Report and subject binding",
    "publication.inputs": "Publication inputs (branch and baseline)",
    "publication.git": "Published commit byte consistency (copy/stage/commit)",
    "publication.receipt": "Review-branch receipt confirmation",
}

#: Readable names for the native discovery finding codes (fallback: the raw code).
_DISCOVERY_CODE_LABELS = {
    "discovery.identity_missing": "Hardware identity is missing",
    "discovery.revision_missing": "Structural revision record is missing",
    "discovery.owner_missing": "Revision owner is missing",
    "discovery.summary_missing": "Change summary is missing",
    "discovery.control_missing": "Delivery version-control reference is missing (system/reference)",
    "discovery.configuration_missing": "Delivery configuration declaration is missing",
    "discovery.robot_name_invalid": "Robot name is not snake_case",
    "discovery.design_budget_missing": "Design-budget record is missing",
    "discovery.link_name_missing": "Rigid-body coordinate system is unidentified",
    "discovery.joint_unsupported_pattern": "Recorded constraints do not identify a one-DOF joint",
    "discovery.joint_axis_sign_missing": "dp.joint.axis_sign (+/-1) is missing",
    "discovery.direction_sign_reversed": "CAD author axis sign is applied; q and ranges remain in author q",
    "direction.author_sign_applied": (
        "CAD author axis_sign selects the undirected shaft positive direction; q and ranges stay in author q"
    ),
    "discovery.interface_name_invalid": "CAD interface name was normalized with a warning",
    "discovery.interface_motion_unresolved": "CAD joint motion is unclassified; a neutral name is retained",
    "direction.semantics_unresolved": "Authored CAD axis is preserved; functional positive motion is not proven",
    "limits.preview_policy": "Inspection preview full-turn window; no native mechanical range is claimed",
    "direction.reference_frame_missing": "The local parent mechanical reference frame is missing",
    "direction.reference_frame_invalid": "The local parent mechanical reference frame is invalid",
    "direction.invalid": "Positive-motion semantics could not be evaluated",
    "v14.direction_unresolved": "Saved joint has no canonical positive-motion sign",
    "discovery.joint_axis_selector_missing": "No readable joint-axis selector",
    "discovery.mate_entities_unsupported": "Mate entity combination is not supported",
    "discovery.mate_geometry_mismatch": "Recorded mate geometry conflicts with the constraint",
    "discovery.frame_attachment": "Assembly-datum ownership is unresolved",
    "discovery.root_missing": "Root rigid-body coordinate system is unresolved",
    "discovery.inputs": "Discovery inputs (native folder available)",
    "discovery.main_assembly_ambiguous": "Main assembly is not unique",
    # Declared-native-evidence observations: recorded warnings, never passes.
    "discovery.exporter_unread": "Exporter declaration is unreadable",
    "discovery.exporter_rigid_attachment": "Instance ownership is established by native rigidity",
    "discovery.exporter_membership_uncovered": "Unassigned instances remain (geometry and mass retained)",
    "discovery.exporter_mate_conflict": "Native mate constraints conflict with declared joint motion in the saved pose",
    "discovery.joint_limits_degenerate": "Joint travel has an equal zero range",
    "discovery.joint_drive_unspecified": "Joint effort and velocity are zero",
    "discovery.jcs_alias": "JCS_ coincides with the body datum (alias)",
    "discovery.design_budget_unspecified": "Design budget is not specified (optional range not applicable)",
    "discovery.identity_derived": "Publication identity is derived from submission data (optional CAD fields absent)",
    "discovery.identity_conflict": "Submitted hardware identity conflicts with CAD records (publication blocked)",
}

#: Readable names for verifier diagnostics carried on check details (recorded warnings).
_DIAGNOSTIC_CODE_LABELS = {
    "cad_component_persistent_reference_aliased": "CAD component persistent reference is aliased (diagnostic)",
    "cad_scope_local_identity_derived": "Scope-local native identity is derived (diagnostic)",
    "cad_part_identity_witness_derived": "Part identity witness is derived (diagnostic)",
    "cad_topology_identity_derived": "Topology identity is derived (diagnostic)",
    "naming.formatted_interface": "Interface name normalized from CAD identity (diagnostic)",
    "naming.motion_unresolved": "Joint motion classification unavailable; neutral name retained (diagnostic)",
    "v14.saved_pose_restriction": "Saved-pose restriction (diagnostic)",
    "v14.mate_inference_unavailable": "Mate inference unavailable (diagnostic)",
    "v14.pose_diagnostics_unread": "Saved-pose diagnostics not read (diagnostic)",
    "v14.membership_unread": "Link membership not read (diagnostic)",
}

#: Readable descriptions for the contract's declared input/output records.
_FILE_PATH_LABELS = {
    "handoff/": "Saved SolidWorks engineering folder",
    "input/": "Prepared input definition and saved CAD",
    "evidence/": "Captured CAD evidence, raw measurements, and inventory",
    "model/robot.json": "Canonical model",
    "urdf/ + meshes/": "URDF and local meshes",
    "reports/tool.json": "Tool identity and file subject",
    "input/ + evidence/ + model/ + urdf/ + meshes/": "Raw evidence and generated artifacts",
    "reports/quality.json": "Deterministic per-object quality report",
    "urdf/ + meshes/ + reports/quality.json": "Verified delivery and configured model repository",
    "reports/pr.json": "Candidate commit and review receipt",
    "reports/structural-team.md": "Structural-team report (readable Markdown)",
    "reports/structural-team.json": "Structural-team report (structured findings and parameters)",
}

_INDEPENDENT_LABELS = {
    # The fixed required checks (solidworks_urdf.required_checks).
    "bundle.subject": "Delivery subject digest binding",
    "input.valid": "Input validity (independent recheck)",
    "source.native_discovery": "Native structure-discovery evidence",
    "tool.identity": "Tool release identity",
    "source.integrity": "Native source integrity",
    "source.native": "Native source presence",
    "model.schema": "Model schema conformance",
    "source.raw": "Native raw-evidence presence",
    "provenance.value_warnings": "Non-native value provenance warnings",
    "physics.mass_closure_equality": "Mass closure (URDF mass = whole CAD mass)",
    "source.dependencies": "Native dependency closure",
    "source.coverage": "Native source coverage",
    "verification.complete": "Independent verification completion marker",
    "model.policy": "Model policy conformance",
    "source.carrier_binding": "Raw carrier identity and topology binding",
    "physics.independent": "Independent physical recomputation",
    "frames.native": "Native coordinate systems",
    "frames.components": "Component coordinate systems",
    "frames.references": "Reference coordinate systems",
    "urdf.syntax_names": "URDF syntax and names",
    "urdf.topology": "URDF topology",
    "geometry.coverage": "Geometry mesh coverage",
    "geometry.assets": "Geometry mesh assets",
    "geometry.native_tessellation": "Native geometry tessellation",
    "geometry.expected_extent": "Expected overall extent",
    "physics.expected_mass": "Expected mass range",
    "consumer.urdf": "Consumer URDF loadability",
    # Physics-module variants that can accompany the fixed set.
    "physics.authority": "Physical-evidence authority",
    "physics.closure": "Physical mass closure",
    "physics.link": "Physical link consistency",
    "physics.raw_inputs": "Physical raw inputs",
    "physics.reading": "Physical reading",
}

#: VER-02 guard: the label table must cover the verifier's fixed inventory; the
#: regression suite asserts this stays empty.
_UNLABELLED_REQUIRED_CHECKS = tuple(sorted(FIXED_REQUIRED_CHECKS - set(_INDEPENDENT_LABELS)))

_INDEPENDENT_FAMILIES = (
    ("inertia.", "Inertia: {name}"),
    ("geometry.", "Geometry: {name}"),
    ("joints.", "Joint: {name}"),
    ("shafts.", "Axis: {name}"),
    ("frames.", "Coordinate system: {name}"),
)


def independent_label(identifier: object) -> str:
    """Readable label for one independent check id; family patterns, then the raw id."""
    name = str(identifier or "")
    if name in _INDEPENDENT_LABELS:
        return _INDEPENDENT_LABELS[name]
    for prefix, template in _INDEPENDENT_FAMILIES:
        if name.startswith(prefix) and len(name) > len(prefix):
            return template.format(name=name[len(prefix) :])
    return name


def _short(value: object, length: int = 12) -> str:
    text = str(value or "not recorded")
    return text[:length] + "..." if len(text) > length else text


def _failure_object(raw_type: str, message: str, raw_detail: dict | None) -> str | None:
    """Basename of the failing document, only when safely parsed; never invented."""
    if isinstance(raw_detail, dict):
        candidate = raw_detail.get("document") or raw_detail.get("object")
        if isinstance(candidate, str) and candidate.strip():
            name = PureWindowsPath(candidate.strip()).name
            if name:
                return name
    if raw_type == "CadError":
        text = message.strip().strip('"')
        if ("\\" in text or "/" in text) and not text.startswith("{"):
            name = PureWindowsPath(text).name
            if name:
                return name
    return None


def _range_text(values: object) -> str | None:
    if (
        isinstance(values, (list, tuple))
        and len(values) == 2
        and all(isinstance(item, (int, float)) and not isinstance(item, bool) for item in values)
    ):
        return f"{values[0]}–{values[1]} kg"
    return None


def _boundary_summary(identifier: object, details: dict) -> dict | None:
    """Scope/expected/actual for one recorded boundary check, when the evidence supports it."""
    name = str(identifier or "")
    if name == "verification.gates":
        required = details.get("required_checks")
        rows = details.get("checks")
        if isinstance(required, list) and isinstance(rows, list):
            passed = sum(
                1
                for row in rows
                if isinstance(row, dict)
                and row.get("id") in required
                and row.get("state") == "passed"
                and row.get("passed") is True
            )
            return {
                "scope": "All required independent checks",
                "expected": f"All {len(required)} required checks pass",
                "actual": f"{passed}/{len(required)} passed ({len(rows)} recorded)",
            }
        subject = details.get("subject_sha256")
        status = details.get("subject_status")
        if isinstance(subject, str) or status:
            return {
                "scope": "Independent verification report",
                "expected": "All required checks pass and the subject is bound",
                "actual": f"Subject {_short(subject)} ({status or 'not marked'})",
            }
        return None
    if name == "handoff.admission":
        return {
            "scope": "Handoff import",
            "expected": "Imported package and digest are recorded",
            "actual": f"Digest {_short(details.get('handoff_sha256'))}; imported package is frozen",
        }
    if name == "handoff.integrity":
        files = details.get("files")
        count = len(files) if isinstance(files, dict) else None
        return {
            "scope": "Handoff file inventory",
            "expected": "Inventory agrees with the handoff digest",
            "actual": f"{count} files; inventory verified"
            if count is not None
            else _short(details.get("handoff_sha256")),
        }
    if name == "discovery.inputs":
        return {
            "scope": "Frozen files and derived interface names",
            "expected": "Frozen inventory agrees with the CAD-derived interface names",
            "actual": f"Inventory {_short(details.get('files_sha256'))}",
        }
    if name == "discovery.definition":
        findings = details.get("findings")
        blocked = (
            sum(1 for item in findings if isinstance(item, dict) and item.get("blocking") is not False)
            if isinstance(findings, list)
            else None
        )
        return {
            "scope": "Native structure definition",
            "expected": "A generated native structure definition that passes",
            "actual": (
                f"Hardware {details.get('hardware_id') or 'not recorded'}; "
                f"revision {details.get('revision') or 'not recorded'}; "
                f"blocking findings {blocked if blocked is not None else 'not recorded'}"
            ),
        }
    if name == "discovery.binding":
        files = details.get("files")
        count = len(files) if isinstance(files, dict) else None
        actual = (
            f"Hardware {details.get('hardware_id') or 'not recorded'} -> "
            f"{details.get('repository_slug') or 'not recorded'}@{details.get('base') or 'not recorded'}"
        )
        if count is not None:
            actual += f"; {count} prepared files"
        return {
            "scope": "Structure definition and target-repository binding",
            "expected": "Bound digest agrees with the target",
            "actual": actual,
        }
    if name == "input.valid":
        revision = details.get("cad_revision")
        if isinstance(revision, dict):
            revision = revision.get("revision")
        files = details.get("files")
        count = len(files) if isinstance(files, dict) else None
        return {
            "scope": "CAD package input specification",
            "expected": "The CAD package passes the input specification and its archive agrees",
            "actual": f"CAD revision {revision or 'not recorded'}" + (f"; {count} files" if count is not None else ""),
        }
    if name == "runtime.ready":
        return {
            "scope": "Native runtime environment",
            "expected": "Native platform and SolidWorks are ready",
            "actual": (
                "Windows, COM, and SolidWorks prerequisites were checked; "
                "the capture session verifies actual CAD access and document readiness"
            ),
        }
    if name == "capture.integrity":
        files = details.get("files")
        count = len(files) if isinstance(files, dict) else None
        return {
            "scope": "Evidence snapshot",
            "expected": "Snapshot and inventory agree byte for byte",
            "actual": f"{count if count is not None else 'not recorded'} evidence files; "
            f"inventory {_short(details.get('manifest_sha256'))}",
        }
    if name == "capture.input_stability":
        files = details.get("files")
        count = len(files) if isinstance(files, dict) else None
        return {
            "scope": "Input stability",
            "expected": "Input files do not change during capture",
            "actual": f"{count if count is not None else 'not recorded'} input files remained unchanged",
        }
    if name == "generation.inputs":
        files = details.get("files")
        count = len(files) if isinstance(files, dict) else None
        return {
            "scope": "Pre-generation evidence snapshot",
            "expected": "Snapshot is valid and unchanged",
            "actual": f"Evidence snapshot: {count if count is not None else 'not recorded'} files",
        }
    if name == "generation.artifacts":
        files = details.get("files")
        count = len(files) if isinstance(files, dict) else None
        return {
            "scope": "Generated artifacts",
            "expected": "Artifacts are complete and bound to the delivery subject",
            "actual": f"{count if count is not None else 'not recorded'} artifact files; "
            f"subject {_short(details.get('subject_sha256'))}",
        }
    if name == "verification.subject":
        return {
            "scope": "Delivery subject",
            "expected": "The subject agrees with the generated result",
            "actual": _short(details.get("subject_sha256")),
        }
    if name == "verification.report_binding":
        return {
            "scope": "Quality-report binding",
            "expected": "The report digest is bound to the delivery subject",
            "actual": (
                f"Report {_short(details.get('report_sha256'))} is bound to subject "
                f"{_short(details.get('subject_sha256'))}"
            ),
        }
    if name == "publication.inputs":
        return {
            "scope": "Review baseline and deterministic branch",
            "expected": "Baseline and deterministic branch verify",
            "actual": f"{details.get('repository_slug') or 'not recorded'}: "
            f"{details.get('base') or 'not recorded'} <- {details.get('branch') or 'not recorded'}",
        }
    if name == "publication.git":
        return {
            "scope": "Copy, stage, and commit byte consistency",
            "expected": "Committed bytes agree with the delivery",
            "actual": f"Commit {_short(details.get('commit'))}; "
            f"byte recheck {details.get('copied_staged_committed') or 'not recorded'}",
        }
    if name == "publication.receipt":
        return {
            "scope": "Review-branch receipt",
            "expected": "Receipt state agrees with the commit",
            "actual": f"State {details.get('state') or 'not recorded'}; commit {_short(details.get('commit'))}",
        }
    return None


def _independent_summary(identifier: object, details: dict) -> dict | None:
    """Scope/expected/actual for one independent check, exact values only when recorded."""
    name = str(identifier or "")
    if name == "physics.expected_mass":
        mass = details.get("mass_kg")
        window = _range_text(details.get("expected_kg"))
        if isinstance(mass, (int, float)) and not isinstance(mass, bool):
            return {
                "scope": "Mass within the expected range",
                "expected": window or "expected range",
                "actual": f"{mass} kg",
            }
        return None
    if name in {"physics.mass_closure_equality", "physics.closure"}:
        urdf, whole = details.get("urdf_mass_kg"), details.get("whole_cad_mass_kg")
        if isinstance(urdf, (int, float)) and isinstance(whole, (int, float)):
            delta = details.get("delta_kg")
            return {
                "scope": "URDF mass agrees with whole-CAD mass",
                "expected": f"absolute error <= {details['atol_kg']} kg"
                if isinstance(details.get("atol_kg"), (int, float)) and details.get("rtol") == 0
                else "agrees with whole-CAD mass within the recorded numeric tolerance",
                "actual": f"URDF {urdf} kg / CAD {whole} kg"
                + (f" (difference {delta} kg)" if isinstance(delta, (int, float)) else " (difference not recorded)"),
            }
        return None
    if name == "consumer.urdf":
        bodies = details.get("bodies")
        version = details.get("version")
        if isinstance(bodies, int):
            return {
                "scope": "Consumer loadability (MuJoCo)",
                "expected": "Loads with the complete body set",
                "actual": f"{bodies} consumer bodies (including the world body); version {version or 'not recorded'}",
            }
        return None
    if name == "geometry.expected_extent":
        extent = details.get("extent_m")
        expected = details.get("expected_largest_m")
        if isinstance(extent, list) and isinstance(expected, list):
            return {
                "scope": "Overall extent",
                "expected": f"largest edge {expected} m",
                "actual": f"three-axis extent {extent} m",
            }
        return None
    if name == "bundle.subject":
        return {
            "scope": "Delivery subject digest",
            "expected": "The subject digest agrees with the delivery",
            "actual": _short(details.get("sha256")),
        }
    if name == "tool.identity":
        return {
            "scope": "Tool identity",
            "expected": "Version and source digest are recorded",
            "actual": (
                f"Version {details.get('version') or 'not recorded'}; source {_short(details.get('source_sha256'))}"
            ),
        }
    if name == "source.native":
        return {
            "scope": "Native source information",
            "expected": "SolidWorks revision and configuration are recorded",
            "actual": f"SolidWorks {details.get('solidworks_revision') or 'not recorded'}; "
            f"configuration {details.get('configuration') or 'not recorded'}",
        }
    if name == "source.dependencies" and isinstance(details.get("native_documents"), int):
        return {
            "scope": "Native dependency and captured-copy coverage",
            "expected": "Every native document has a complete, traceable captured copy",
            "actual": f"{details['native_documents']} native documents; "
            f"{details.get('collected_documents', 'not recorded')} collected copies; "
            f"{details.get('suppressed_instances', 'not recorded')} suppressed instances",
        }
    if name in {"source.coverage", "source.native_discovery"} and isinstance(details.get("bodies"), int):
        field, label = ("occurrences", "occurrences") if name == "source.coverage" else ("joints", "joints")
        return {
            "scope": "Instance and rigid-body coverage" if name == "source.coverage" else "Native structure definition",
            "expected": "Independent reconstruction agrees with the definition",
            "actual": f"{details['bodies']} bodies; {details.get(field, 'not recorded')} {label}",
        }
    if name.startswith("shafts.") and isinstance(details.get("angle_deg"), (int, float)):
        return {
            "scope": "Joint axis alignment with the native cylinder axis",
            "expected": "Collinear axes with the origin on the axis, within the quality-spec tolerances",
            "actual": f"angle {details['angle_deg']} deg; axis offset {details.get('offset_m', 'not recorded')} m",
        }
    if name.startswith("inertia.") and isinstance(details.get("mass_kg"), (int, float)):
        return {
            "scope": "Body mass and inertia",
            "expected": (
                "Positive mass and principal inertias; the tensor satisfies the triangle "
                "inequality and agrees with the independent result"
            ),
            "actual": (
                f"mass {details['mass_kg']} kg; principal inertia "
                f"{details.get('principal_inertia_kg_m2', 'not recorded')} kg*m^2"
            ),
        }
    if name.startswith("joints.") and isinstance(details.get("type"), str):
        kind = details["type"]
        limits = details.get("limits") or {}
        unit = "m" if kind == "prismatic" else "rad"
        actual = f"type {kind}; signed axis {details.get('axis', 'not recorded')}"
        if isinstance(limits, dict) and "lower" in limits and "upper" in limits:
            actual += f"; range [{limits['lower']}, {limits['upper']}] {unit}"
        return {
            "scope": "Joint type, axis, and limits",
            "expected": "Output agrees with the native structure definition",
            "actual": actual,
        }
    if name == "urdf.syntax_names":
        if details.get("validated") is True:
            return {"scope": "URDF syntax and names", "expected": "Syntax and naming pass", "actual": "Passed"}
        return None
    if name == "urdf.topology":
        links, root = details.get("links"), details.get("root")
        if isinstance(links, int):
            return {
                "scope": "URDF topology",
                "expected": "One connected tree with a single root",
                "actual": f"{links} links; root {root}",
            }
        return None
    if name in {"geometry.coverage", "geometry.assets"}:
        meshes = details.get("meshes")
        if isinstance(meshes, int):
            return {
                "scope": "Geometry meshes",
                "expected": "Every rigid-body mesh is present",
                "actual": f"{meshes} meshes",
            }
        return None
    if name == "input.valid":
        if details.get("passed") is True or details.get("validated") is True:
            return {
                "scope": "Input specification",
                "expected": "The input package conforms",
                "actual": "Specification passed",
            }
        return None
    return None


def _failure(job: dict, stage: dict) -> dict:
    """Safe, non-asserting failure meaning; raw evidence always preserved."""
    raw_error = str(stage.get("error") or "")
    raw_type, _, message = raw_error.partition(": ")
    diagnostic = stage.get("diagnostic")
    raw_detail = diagnostic if isinstance(diagnostic, dict) and diagnostic else None
    codes = None
    kind = "unknown"
    structured_groups = None
    structured_samples = None
    discovery = job.get("discovery") if isinstance(job.get("discovery"), dict) else {}
    blocking_findings = [
        item
        for item in discovery.get("findings") or []
        if isinstance(item, dict) and item.get("blocking", True) and item.get("code")
    ]
    if stage.get("error_code") == "native_discovery_main_assembly_ambiguous":
        title = "Main assembly is not unique"
        meaning = (
            "Save dp.hardware_id and dp.delivery_configuration in the delivered main assembly. "
            "When multiple assemblies declare delivery identity, one entry must be explicit; "
            "the pipeline does not guess by filename or size."
        )
        kind = "definition"
    elif stage.get("error_code") == "cad_link_group_payload_invalid":
        kind = "runtime"
        title = "Native link-group mass read rejected"
        meaning = (
            "The native link-group payload did not match the requested instance, configuration, "
            "and selection identity. The stage did not generate or verify a delivery; review the "
            "raw diagnostic and retry from a corrected native run."
        )
    elif stage.get("id") == "discover" and "discovery.definition" in raw_error and blocking_findings:
        counts: dict[str, int] = {}
        samples: dict[str, list[dict]] = {}
        for item in blocking_findings:
            code = str(item.get("code"))
            counts[code] = counts.get(code, 0) + 1
            rows = samples.setdefault(code, [])
            if len(rows) < 3:
                rows.append({"object": str(item.get("object") or ""), "message": str(item.get("message") or "")})
        structured_groups = [
            {"code": code, "label": _DISCOVERY_CODE_LABELS.get(code, code), "count": count}
            for code, count in sorted(counts.items(), key=lambda entry: (-entry[1], entry[0]))
        ]
        structured_samples = samples
        title = "Structure discovery checks failed"
        meaning = (
            f"{len(blocking_findings)} checks failed. Required definitions may be missing, or "
            "the record may exceed current discovery capability. Expand the issue list for the "
            "object, cause, and evidence; an unsupported pattern does not mean the design is wrong."
        )
        kind = "definition"
    elif raw_type == "CadError" and raw_detail and {"errors", "warnings"} <= set(raw_detail):
        kind = "runtime"
        codes = {"errors": raw_detail.get("errors"), "warnings": raw_detail.get("warnings")}
        if raw_detail.get("errors") == 2:
            title = "Native CAD open failed"
            meaning = (
                "A file or one of its referenced documents cannot be located. The impact is not "
                "yet proven; platform maintenance must verify whether it is required for this delivery."
            )
        else:
            title = "Native CAD open error"
            meaning = (
                f"The native session returned error code {raw_detail.get('errors')} with "
                f"{raw_detail.get('warnings')} warnings. This does not prove a missing file; "
                "platform maintenance must verify the condition before retrying."
            )
    elif raw_type == "CadError":
        kind = "runtime"
        title = "Native CAD stage failed"
        meaning = (
            "The document or one of its dependencies could not be read; the stage did not "
            "complete. Check the diagnostic before retrying."
        )
    elif raw_type in {"EnvironmentError_", "EnvironmentError"}:
        kind = "runtime"
        title = "Native runtime environment unavailable"
        meaning = (
            "The Windows native environment is not ready (service, license, or similar); "
            "the stage did not complete. Contact platform maintenance."
        )
    elif raw_type == "TimeoutError" or "modal" in message.lower():
        kind = "runtime"
        title = "Native session blocked"
        meaning = (
            "A dialog or timeout blocked the native session; the stage did not complete. "
            "Resolve it on the Windows session before retrying."
        )
    else:
        title = "Stage failed (cause undetermined)"
        meaning = (
            "The stage did not complete and no determinable cause was recorded. "
            "Provide the run ID when contacting platform maintenance."
        )
    failure = {
        "stage": stage.get("id"),
        "stage_name": stage.get("name"),
        "title": title,
        "meaning": meaning,
        "raw_type": raw_type or None,
        "error_code": stage.get("error_code"),
        "raw_error": raw_error or None,
        "raw_detail": raw_detail,
        "kind": kind,
    }
    if structured_groups is not None:
        failure["discovery_sha256"] = discovery.get("discovery_sha256")
        failure["finding_total"] = len(blocking_findings)
        failure["finding_counts"] = structured_groups
        failure["finding_samples"] = structured_samples
    failed_object = _failure_object(raw_type, message, raw_detail)
    if failed_object:
        failure["object"] = failed_object
    if codes is not None:
        failure["solidworks_codes"] = codes
    unresolved = _unresolved_dependencies(raw_detail)
    if unresolved:
        failure["unresolved_dependencies"] = unresolved
        names = ", ".join(item.get("name") or item.get("path") or "" for item in unresolved)
        failure["meaning"] = (
            f"A file or referenced document cannot be located; the native session reported "
            f"these unresolved references: {names}. Whether the active configuration uses "
            "them is not yet proven; maintenance must verify the impact before acting."
        )
    if codes is not None:
        applicability = raw_detail.get("applicability") if isinstance(raw_detail, dict) else None
        failure["applicability"] = applicability if applicability in {"proven", "unproven"} else "unproven"
    return failure


def _unresolved_dependencies(raw_detail: dict | None) -> list[dict] | None:
    """Exact unresolved reference rows when the native diagnostic carries them (never invented)."""
    if not isinstance(raw_detail, dict):
        return None
    for key in ("unresolved_dependencies", "unresolved_references", "unresolved"):
        value = raw_detail.get(key)
        if not isinstance(value, list):
            continue
        rows = []
        for item in value:
            if isinstance(item, dict):
                name = item.get("name") or item.get("file") or item.get("document") or item.get("id")
                path = item.get("path") or item.get("last_known_path") or item.get("reference")
                if name or path:
                    rows.append({"name": name, "path": path})
            elif isinstance(item, str) and item:
                rows.append({"name": item, "path": None})
        if rows:
            return rows
    return None


def _counts(rows: list[dict]) -> dict:
    executed = [row for row in rows if row.get("executed")]
    return {
        "passed": sum(1 for row in executed if row.get("state") == "passed"),
        "executed": len(executed),
        "total": len(rows),
    }


def _failed_summary(label: str, summary: dict | None, details: dict) -> dict:
    """Failed rows must carry the recorded reason, never a blank or digest-only actual."""
    reason = details.get("error") or details.get("message") or details.get("reason")
    errors = details.get("errors")
    if not isinstance(reason, str) and isinstance(errors, list) and errors:
        first = errors[0]
        if isinstance(first, dict):
            reason = first.get("message") or first.get("code")
    row = dict(summary) if summary else {"scope": f"{label} failed"}
    row.setdefault("expected", "check passes")
    if isinstance(reason, str) and reason.strip():
        text = reason.strip()
        pointer = "... (see the issue list and raw evidence for the full diagnostic)" if len(text) > 160 else ""
        row["actual"] = f"Failed: {text[:160]}{pointer}"
    else:
        row["actual"] = "Failed (reason not recorded; see the issue list and raw evidence)"
    return row


_WARNING_DETAIL_KEYS = (
    "limits",
    "components",
    "links",
    "joint",
    "mate",
    "scope",
    "mate_type",
    "qualification_blocker",
    "publication_blocker",
    "missing",
    "technical_label",
    "label_source",
    "mass_kg",
    "masses",
    "objects",
    "component",
    "entity_kind",
    "body_type",
    "body_index",
    "owner_scope_key",
    "object_indices",
    "field",
    "native_reference_sha256",
    "mapping_source_part_document_sha256",
    "mapping_source_body_reference_sha256",
    "mapped_source_indices",
    "mapped_source_persistent_reference_sha256",
    "topology_signature_sha256s",
)
_WARNING_RESIDUAL_KEYS = ("max_abs_residual", "max_normalized_residual", "threshold")


def _warning_row(stage: dict, item: dict) -> dict:
    """One recorded non-blocking observation as a warning row (never a pass, never a gate)."""

    code = str(item.get("code") or "")
    detail = item.get("detail") if isinstance(item.get("detail"), dict) else {}
    context = {**item, **detail}
    row: dict = {
        "stage": stage.get("id"),
        "stage_name": stage.get("name"),
        "code": code,
        "label": _DIAGNOSTIC_CODE_LABELS.get(code, _DISCOVERY_CODE_LABELS.get(code, code)),
        "state_label": "Warning",
        "blocking": False,
    }
    object_id = (
        item.get("object")
        or (f"joint:{detail['joint']}" if detail.get("joint") else None)
        or (
            f"{context['component']}:{context['entity_kind']}:"
            f"{context['body_type']}/{context['body_index']}"
            if all(
                key in context
                for key in (
                    "component",
                    "entity_kind",
                    "body_type",
                    "body_index",
                )
            )
            else context.get("component")
        )
    )
    if object_id:
        row["object"] = str(object_id)
    if item.get("message"):
        row["message"] = str(item["message"])
    elif code in NATIVE_SCOPE_IDENTITY_WARNING_CODES:
        row["message"] = str(row["label"])
    for key in _WARNING_DETAIL_KEYS:
        value = context.get(key)
        if value is not None:
            # Authored zeros (for example an equal zero joint range) stay exact values.
            row[key] = value
    residual = {
        key: detail[key]
        for key in _WARNING_RESIDUAL_KEYS
        if isinstance(detail.get(key), (int, float)) and not isinstance(detail.get(key), bool)
    }
    if residual:
        row["residual"] = residual
    return row


def _recorded_warnings(
    stage: dict,
    findings: list,
    rows: list[dict],
    quality_diagnostics: list[dict] | None = None,
) -> list[dict]:
    """Warnings from recorded evidence only: non-blocking findings and check diagnostics."""

    warnings: list[dict] = []
    if quality_diagnostics is not None and not isinstance(
        quality_diagnostics,
        list,
    ):
        raise PipelineError("Quality diagnostics are malformed")
    native_diagnostics = [
        item
        for item in quality_diagnostics or []
        if isinstance(item, dict)
        and item.get("code") in NATIVE_SCOPE_IDENTITY_WARNING_CODES
    ]
    scope_compared = False
    native_gate_seen = False
    if stage.get("id") == "discover":
        warnings.extend(
            _warning_row(stage, item)
            for item in findings
            if isinstance(item, dict) and item.get("blocking") is False and item.get("code")
        )
    for row in rows:
        details = row.get("raw_details")
        row_passed = (
            row.get("state") == "passed" or row.get("passed") is True
        )
        if row.get("id") == "geometry.native_tessellation" and row_passed:
            native_gate_seen = True
        recorded = details.get("warnings") if isinstance(details, dict) else None
        if isinstance(recorded, list):
            raw_scope = [
                item
                for item in recorded
                if isinstance(item, dict)
                and item.get("code") in NATIVE_SCOPE_IDENTITY_WARNING_CODES
            ]
            if raw_scope:
                if not native_scope_warning_details_match(
                    native_diagnostics,
                    recorded,
                ):
                    raise PipelineError(
                        "Native scope warnings differ from quality diagnostics"
                    )
                scope_compared = True
                recorded = [
                    next(
                        item
                        for item in native_diagnostics
                        if item["code"] == row["code"]
                        and item["detail"] == row.get("detail")
                    )
                    if row.get("code")
                    in NATIVE_SCOPE_IDENTITY_WARNING_CODES
                    else row
                    for row in recorded
                ]
            warnings.extend(
                _warning_row(stage, item) for item in recorded if isinstance(item, dict) and item.get("code")
            )
        elif row.get("id") == "geometry.native_tessellation" and row_passed:
            raise PipelineError(
                "Passed native tessellation gate carries no warning list"
            )
        diagnostics = details.get("diagnostics") if isinstance(details, dict) else None
        if isinstance(diagnostics, list):
            warnings.extend(
                _warning_row(stage, item) for item in diagnostics if isinstance(item, dict) and item.get("code")
            )
    if native_diagnostics and (
        not scope_compared or not native_gate_seen
    ):
        raise PipelineError(
            "Native scope diagnostics have no matching gate warnings"
        )
    return warnings


def build_report(job: dict | None = None, *, view: dict | None = None) -> dict:
    """Readable report projection; pure display mapping over recorded evidence."""
    job = job if isinstance(job, dict) else {}
    view = view if isinstance(view, dict) else stage_view(job)
    result = job.get("result") if isinstance(job.get("result"), dict) else {}
    quality = result.get("quality") if isinstance(result.get("quality"), dict) else {}
    discovery = job.get("discovery") if isinstance(job.get("discovery"), dict) else {}
    discovery_findings = discovery.get("findings") if isinstance(discovery.get("findings"), list) else []
    quality_rows = [row for row in (quality.get("checks") or []) if isinstance(row, dict)]
    stages_out: list[dict] = []
    first_failed: dict | None = None
    measured_window: dict | None = None
    measured_closure: dict | None = None
    for stage in view.get("stages", []):
        boundary_rows = []
        for boundary in ("input", "output"):
            for item in stage.get(f"{boundary}_qc", []):
                details = item.get("details") if isinstance(item.get("details"), dict) else {}
                state = item.get("state") if item.get("state") in {"passed", "failed"} else "not_run"
                row = {
                    "id": item.get("id"),
                    "boundary": boundary,
                    "label": _BOUNDARY_LABELS.get(item.get("id"), str(item.get("label") or item.get("id") or "")),
                    "state": state,
                    "state_label": _CHECK_STATE_LABELS[state],
                    "executed": state != "not_run",
                    "raw_details": details,
                    **({"reuse": stage["reuse"]} if stage.get("reuse") else {}),
                }
                summary = _boundary_summary(item.get("id"), details)
                if state == "failed":
                    summary = _failed_summary(row["label"], summary, details)
                elif state == "not_run":
                    summary = {**(summary or {}), "actual": "Not run; no check result is available"}
                if summary is not None:
                    row["summary"] = summary
                boundary_rows.append(row)
        independent_rows: list[dict] = []
        if stage.get("id") == "verify":
            for item in quality_rows:
                details = item.get("details") if isinstance(item.get("details"), dict) else {}
                declared = item.get("state")
                if declared in {"passed", "failed", "not_run"}:
                    state = declared
                    if state == "passed" and item.get("passed") is False:
                        state = "failed"
                elif item.get("passed") is True:
                    state = "passed"
                elif item.get("passed") is False:
                    state = "failed"
                else:
                    state = "not_run"
                row = {
                    "id": item.get("id"),
                    "label": independent_label(item.get("id")),
                    "state": state,
                    "state_label": _CHECK_STATE_LABELS[state],
                    "executed": state != "not_run",
                    "raw_details": details,
                    **({"reuse": stage["reuse"]} if stage.get("reuse") else {}),
                }
                summary = _independent_summary(item.get("id"), details)
                if state == "failed":
                    summary = _failed_summary(row["label"], summary, details)
                elif state == "not_run":
                    summary = {**(summary or {}), "actual": "Not run; no check result is available"}
                if summary is not None:
                    row["summary"] = summary
                independent_rows.append(row)
                if item.get("id") == "physics.expected_mass" and isinstance(details.get("mass_kg"), (int, float)):
                    measured_window = {"mass_kg": details.get("mass_kg"), "expected_kg": details.get("expected_kg")}
                if item.get("id") in {"physics.mass_closure_equality", "physics.closure"} and isinstance(
                    details.get("urdf_mass_kg"), (int, float)
                ):
                    measured_closure = {
                        "urdf_mass_kg": details.get("urdf_mass_kg"),
                        "whole_cad_mass_kg": details.get("whole_cad_mass_kg"),
                        "delta_kg": details.get("delta_kg"),
                    }
        warning_rows = _recorded_warnings(
            stage,
            discovery_findings,
            [*boundary_rows, *independent_rows],
            quality.get("diagnostics"),
        )
        stage_out = {
            "id": stage.get("id"),
            "name": stage.get("name"),
            "state": stage.get("state", "not_run"),
            "state_label": _STAGE_STATE_LABELS.get(stage.get("state"), str(stage.get("state") or "Not run")),
            "at": stage.get("at"),
            **({"reuse": stage["reuse"]} if stage.get("reuse") else {}),
            "in_scope": stage.get("in_scope"),
            "counts": {
                "boundary": _counts(boundary_rows),
                "independent": _counts(independent_rows) if independent_rows else None,
                "warnings": len(warning_rows),
            },
            "boundary": boundary_rows,
            "files": [
                {
                    "boundary": boundary,
                    "label": "Frozen handoff and inventory"
                    if stage.get("id") == "freeze" and boundary == "output"
                    else _FILE_PATH_LABELS.get(
                        str(record.get("path") or ""), str(record.get("label") or record.get("path") or "")
                    ),
                    "path": record.get("path"),
                    "availability": record.get("availability"),
                    "files": len(record.get("files") or {}),
                }
                for boundary, records in (("input", stage.get("inputs", [])), ("output", stage.get("outputs", [])))
                for record in records
            ],
            "automatic": {
                "passed_labels": [row["label"] for row in boundary_rows if row["executed"] and row["state"] == "passed"]
                + [row["label"] for row in independent_rows if row["executed"] and row["state"] == "passed"],
                "failed_labels": [row["label"] for row in boundary_rows if row["executed"] and row["state"] == "failed"]
                + [row["label"] for row in independent_rows if row["executed"] and row["state"] == "failed"],
            },
            "unsupported": [
                {"id": item.get("id"), "label": item.get("label")} for item in stage.get("unsupported", [])
            ],
            **({"warnings": warning_rows} if warning_rows else {}),
            "confirmations": [
                {
                    key: item.get(key)
                    for key in (
                        "id",
                        "label",
                        "reference",
                        "review_stage",
                        "scope",
                        "automatic_exclusion",
                        "state",
                        "approval_tracking",
                    )
                }
                for item in stage.get("confirmations", [])
            ],
        }
        if stage.get("state") in {"completed", "running"}:
            stage_out["manual_scope_note"] = (
                "The following fact scopes require external review. The platform does not read "
                "review approvals; a rerun only requires re-confirmation where relevant facts changed."
            )
        else:
            stage_out["manual_scope_note"] = (
                "Automatic checks failed or did not run. Fix the issues and complete automatic "
                "verification first; manual confirmations are not applicable yet."
            )
        if independent_rows:
            stage_out["independent"] = independent_rows
        if stage.get("state") == "failed" and first_failed is None:
            first_failed = stage
        stages_out.append(stage_out)
    states = {stage["state"] for stage in stages_out}
    if "failed" in states:
        overall_state, headline = "failed", f"{first_failed['name']} stage failed; downstream stages did not run"
    elif job.get("status") == "failed":
        overall_state, headline = "failed", "Job failed; see the raw diagnostic"
    elif "running" in states:
        overall_state, headline = "running", "Run is in progress; not all stages have completed"
    elif stages_out and states == {"completed"}:
        overall_state, headline = "passed", "All six stages completed and independent verification passed"
    else:
        overall_state, headline = "not_run", "No stage records yet"
    warning_items = [item for stage in stages_out for item in stage.get("warnings") or []]
    report = {
        "schema_version": REPORT_SCHEMA,
        "overall": {
            "state": overall_state,
            "state_label": _OVERALL_STATE_LABELS[overall_state],
            "headline": headline,
            "stages_completed": sum(1 for stage in stages_out if stage["state"] == "completed"),
            "stages_total": len(stages_out),
            "engineering_state": "not_ready",
        },
        "failure": _failure(job, first_failed) if first_failed is not None else None,
        "measured": {
            "subject_sha256": result.get("subject_sha256") or view.get("subject_sha256"),
            "handoff_sha256": view.get("handoff_sha256"),
            "expected_mass_window": measured_window,
            "mass_closure": measured_closure,
        },
        "warnings": {
            "total": len(warning_items),
            "items": warning_items,
            "note": (
                "Warnings are recorded, non-blocking observations. They are neither passes nor "
                "acceptance conclusions; review them through the corresponding stage checklist."
            ),
        },
        "stages": stages_out,
    }
    verified = (
        any(stage["id"] == "verify" and stage["state"] == "completed" for stage in stages_out)
        and quality.get("passed") is True
    )
    report["overall"]["engineering_state"] = "external_review" if verified else "not_ready"
    report["external_review"] = {
        "tracking": "external",
        "scopes": [
            {**confirmation, "stage": stage["id"]} for stage in stages_out for confirmation in stage["confirmations"]
        ],
        "note": (
            "The platform does not read or manage review approvals; unchanged facts may retain "
            "existing approval on rerun, while changed items require confirmation."
        ),
    }
    return report
