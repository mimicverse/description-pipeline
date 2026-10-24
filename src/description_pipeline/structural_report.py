"""Generate the structural team's report from recorded pipeline evidence.

This is a projection of existing findings. It neither changes verification
decisions nor supplies engineering values to the model generator.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .geometry.native_u import (
    NATIVE_SCOPE_IDENTITY_WARNING_CODES,
    native_scope_warning_details_match,
    native_scope_warning_diagnostic_is_valid,
)
from .geometry.native_tessellation import parse_native_tessellation
from .io import PipelineError, file_digest, read_data, write_json
from .schemas import EFFECTIVE_INSTANCES_SCHEMA

REPORT_SCHEMA = "solidworks-to-urdf.structural-report/v1"
MARKDOWN_PATH = "reports/structural-team.md"
JSON_PATH = "reports/structural-team.json"
#: Diagnostic-bundle schema the native worker seals and the Linux side renders from.
DIAGNOSTIC_SCHEMA = "solidworks-to-urdf.failure-diagnostics/v1"

#: Evidence files the report builder projects into findings.
REPORT_EVIDENCE_FILES = (
    "input/handoff.json",
    "input/source.json",
    "reports/quality.json",
    "evidence/capture.json",
    "evidence/discovery.json",
    "evidence/raw/dependency_closure.json",
    "evidence/raw/scene_raw.json",
    "evidence/raw/material_provenance.json",
    "evidence/raw/effective_instances.json",
    "evidence/raw/mass_closure.json",
    "evidence/raw/geometry.json",
    "evidence/raw/source_scopes.json",
)

#: Files sealed for one failed job: the projected evidence plus the snapshot
#: manifest the builder consults for its fail-closed material checks.
SEALED_DIAGNOSTIC_FILES = (*REPORT_EVIDENCE_FILES, "evidence/manifest.json")


def sealable_diagnostic_files(evidence_root: Path | None) -> list[str]:
    """Existing diagnostic files of one delivery tree, delivery-relative paths.

    The native side seals exactly these files and the Linux side renders from the
    same bytes, so no report ever depends on where it was rendered.
    """

    return sorted(diagnostic_file_sources(evidence_root))


def diagnostic_file_sources(evidence_root: Path | None, *, failure_roots: tuple[str, ...] = ()) -> dict[str, str]:
    """Canonical sealed-diagnostic names mapped to existing sources, delivery-relative.

    Candidates are the canonical delivery name and, for a retained partial
    failure, ``<gated failure root>/partial/<name without the evidence/ prefix>``
    where the failure roots come from the owned failure receipt path.  Nothing is
    discovered recursively; a top-level ``partial/`` copy is never guessed at.
    The bundle layout, allowlist and hashes always use the canonical names, so a
    file recovered from a partial tree renders exactly like a complete one, and
    the manifest keeps the partial provenance visible.  If more than one gated
    root offers the same file the read fails instead of choosing arbitrarily.
    """

    if evidence_root is None:
        return {}
    root = Path(evidence_root)
    sources: dict[str, str] = {}
    for name in SEALED_DIAGNOSTIC_FILES:
        if (root / name).is_file():
            sources[name] = name
            continue
        # Partial trees keep the staging spelling: raw readings live under
        # partial/raw/..., other evidence-root files keep their evidence/ prefix.
        spellings = [name]
        if name.startswith("evidence/raw/"):
            spellings.insert(0, name[len("evidence/") :])
        matches = []
        for failure_root in failure_roots:
            for spelling in spellings:
                candidate = f"{failure_root}/partial/{spelling}"
                if (root / candidate).is_file():
                    matches.append(candidate)
                    break
        if len(matches) > 1:
            raise PipelineError(f"Conflicting partial evidence for {name!r}: {matches}")
        if matches:
            sources[name] = matches[0]
    urdf = root / "urdf"
    if urdf.is_dir():
        for path in sorted(urdf.rglob("*.urdf")):
            relative = path.relative_to(root).as_posix()
            sources[relative] = relative
    return sources


def is_sealed_diagnostic_member(relative: str) -> bool:
    """Whether one member name is a known sealed diagnostic (projected evidence or URDF)."""

    if relative in SEALED_DIAGNOSTIC_FILES:
        return True
    parts = relative.split("/")
    return (
        len(parts) >= 2
        and parts[0] == "urdf"
        and parts[-1].endswith(".urdf")
        and all(part not in {"", ".", ".."} for part in parts)
    )


_FINDING_LISTS = {"findings", "warnings", "errors", "problems", "diagnostics", "checks"}

#: The single canonical material-provenance record (producer: 71f489a): written for every
#: capture under ``evidence/raw/material_provenance.json``, bound by the snapshot manifest.
_MATERIAL_SCHEMA = "solidworks-to-urdf.material-provenance/v1"
_MATERIAL_TOP_KEYS = ("schema_version", "assembly", "configuration", "measured_source")
_MATERIAL_ENTRY_KEYS = (
    "code",
    "message",
    "blocking",
    "component",
    "document",
    "configuration",
    "missing_body_indices",
    "material_assignment",
    "measured",
)
_MATERIAL_MEASURED_KEYS = (
    "source",
    "scope",
    "mass_kg",
    "center_of_mass_m",
    "inertia_kg_m2",
    "volume_m3",
    "density_kg_m3",
)


def _canonical_material_warnings(value) -> list | None:
    """The canonical warning list of the material-provenance record, or ``None``."""

    if not isinstance(value, dict):
        return None
    if value.get("schema_version") != _MATERIAL_SCHEMA:
        return None
    if any(key not in value for key in _MATERIAL_TOP_KEYS):
        return None
    if value.get("measured_source") != "cad_api":
        return None
    warnings = value.get("warnings")
    if not isinstance(warnings, list):
        return None
    for item in warnings:
        if not isinstance(item, dict) or any(key not in item for key in _MATERIAL_ENTRY_KEYS):
            return None
        if item.get("code") != "cad_material_provenance_missing":
            # Unknown reason values fail closed instead of being rendered as warnings.
            return None
        if not isinstance(item.get("message"), str):
            return None
        if not isinstance(item.get("blocking"), bool):
            return None
        if not isinstance(item.get("material_assignment"), dict) or not isinstance(item.get("measured"), dict):
            return None
        if any(key not in item["measured"] for key in _MATERIAL_MEASURED_KEYS):
            return None
        if item["measured"].get("source") != "cad_api":
            return None
    return warnings


#: The single canonical effective-instance record (producer: the native freeze): written for
#: every capture under ``evidence/raw/effective_instances.json``, bound by the manifest.
_EFFECTIVE_SCHEMA = EFFECTIVE_INSTANCES_SCHEMA
_EFFECTIVE_TOP_KEYS = ("schema_version", "assembly", "configuration", "readings", "warnings")
_EFFECTIVE_CODES = (
    "cad_instance_override_effective",
    "cad_surface_only_effective_instance",
    "cad_instance_measurement_differs_from_part",
)
_EFFECTIVE_QUALIFICATION = (
    "cad_authored_effective_instance; not geometry-derived, not independently physically qualified"
)
_INSTANCE_DIFFERENCE_QUALIFICATION = (
    "cad_assembly_instance_measurement; cause_of_part_difference_unproven; "
    "not geometry-derived, not independently physically qualified"
)
_PART_MEASUREMENT_KEYS = (
    "mass_kg",
    "center_of_mass_m",
    "inertia_kg_m2",
    "scope",
    "used_api",
    "product_convention",
)


def _canonical_effective_warnings(value) -> list | None:
    """The canonical warning list of the effective-instance record, or ``None``."""

    if not isinstance(value, dict) or value.get("schema_version") != _EFFECTIVE_SCHEMA:
        return None
    if any(key not in value for key in _EFFECTIVE_TOP_KEYS):
        return None
    if not isinstance(value.get("readings"), dict):
        return None
    warnings = value.get("warnings")
    if not isinstance(warnings, list):
        return None
    for item in warnings:
        if not isinstance(item, dict):
            return None
        code = item.get("code")
        if code not in _EFFECTIVE_CODES:
            # Unknown codes fail closed instead of being rendered as warnings.
            return None
        if item.get("blocking") is not False:
            return None
        if not isinstance(item.get("component"), str) or not item["component"].strip():
            return None
        if not isinstance(item.get("measurement"), dict):
            return None
        overrides = item.get("overrides")
        if not isinstance(overrides, dict) or any(not isinstance(flag, bool) for flag in overrides.values()):
            return None
        if code == "cad_instance_measurement_differs_from_part":
            # Flagless leaf instance whose assembly-context measurement differs from
            # its part measurement: the cause is unproven, so the exact literal is
            # required and every override flag must be false.
            if item.get("qualification") != _INSTANCE_DIFFERENCE_QUALIFICATION:
                return None
            if item.get("role") != "leaf":
                return None
            if not overrides or any(flag is not False for flag in overrides.values()):
                return None
            part = item.get("part_measurement")
            if not isinstance(part, dict) or any(key not in part for key in _PART_MEASUREMENT_KEYS):
                return None
            continue
        if item.get("qualification") != _EFFECTIVE_QUALIFICATION:
            return None
        required = (
            ("document", "part_document_read", "solid_bodies", "sheet_bodies")
            if code == "cad_surface_only_effective_instance"
            else ("role", "document_type", "part_document", "assembly_document")
        )
        if any(key not in item for key in required):
            return None
    return warnings


def _canonical_untessellated_faces(
    value,
) -> list | None:
    """The canonical ``no_facets_faces`` rows of ``raw/geometry.json``, or ``None``.

    The file is a list of per-component entries; each row is returned with its
    component attached so the warning can name it.  Any schema deviation fails
    closed.  Exactly two row shapes pass, mirroring the verification gate: the
    proven-probe row (boolean ``tessellate_result`` with ``facet_count`` 0) and
    the degenerate fallback row (no ``facet_count`` and no ``tessellate_result``;
    a persistent ``com_error`` probe record) admitted only up to the same
    numerically-zero area bound (1e-12 m2).  No area claim is derived anywhere.
    """

    import math

    if not isinstance(value, list):
        return None
    pinned_api = "IBody2.GetTessellation/ITessellation.Tessellate/GetFaceFacets"
    out: list[dict] = []
    for entry in value:
        if not isinstance(entry, dict):
            return None
        if any(key not in entry for key in ("component", "triangles", "bodies", "tessellation")):
            return None
        component = entry.get("component")
        bodies = entry.get("bodies")
        tessellation = entry.get("tessellation")
        if not isinstance(component, str) or not component.strip():
            return None
        if not isinstance(bodies, dict) or not isinstance(tessellation, dict):
            return None
        for key in ("solid", "sheet"):
            count = bodies.get(key)
            if type(count) is not int or count < 0:
                return None
        try:
            spans = parse_native_tessellation(
                component,
                tessellation,
                bodies,
                entry.get("triangles"),
                native_fin_resolutions=[],
                native_identity_warnings=[],
            )
        except PipelineError:
            return None
        faces = tessellation.get("faces")
        if type(faces) is not int or faces < 0:
            return None
        rows = tessellation.get("no_facets_faces")
        if not isinstance(rows, list):
            return None
        if faces <= len(rows):
            return None
        seen = set()
        base = {
            "body_type",
            "body_index",
            "face_index",
            "triangle_count",
            "area_m2",
            "used_api",
        }
        for row in rows:
            if not isinstance(row, dict):
                return None
            if set(row) not in (base | {"facet_count", "tessellate_result"}, base | {"tessellate_probe_error"}):
                return None
            if row.get("body_type") != 1:
                return None
            for key in ("body_index", "face_index"):
                if type(row.get(key)) is not int or row[key] < 0:
                    return None
            if row["body_index"] >= bodies["sheet"]:
                return None
            if type(row.get("triangle_count")) is not int or row["triangle_count"] != 0:
                return None
            area = row.get("area_m2")
            if isinstance(area, bool) or not isinstance(area, (int, float)) or not math.isfinite(area) or area < 0:
                return None
            if "tessellate_result" in row:
                if type(row.get("facet_count")) is not int or row["facet_count"] != 0:
                    return None
                if not isinstance(row.get("tessellate_result"), bool):
                    return None
            else:
                # A persistent DISP_E_EXCEPTION on a geometrically degenerate face
                # is recorded as a probe error instead of a result: both facet_count
                # and tessellate_result are products of the probe that failed, so
                # this variant carries neither.  It is admissible only at
                # numerically zero area, exactly as in the verification gate.
                probe_error = row.get("tessellate_probe_error")
                if not isinstance(probe_error, str) or not probe_error.startswith("com_error"):
                    return None
                if area > 1e-12:
                    return None
            if row.get("used_api") != pinned_api:
                return None
            key = (row["body_type"], row["body_index"], row["face_index"])
            if key in seen:
                return None
            seen.add(key)
            out.append({"component": component, **row})
        span_counts = {
            (span["body_type"], span["body_index"], span["face_index"]): span["triangle_count"]
            for span in spans
        }
        if set(seen) != {key for key, count in span_counts.items() if count == 0}:
            return None
    return out


def _findings(value, source, *, severity=None):
    if isinstance(value, list):
        for index, entry in enumerate(value):
            yield from _findings(entry, f"{source}/{index}", severity=severity)
        return
    if not isinstance(value, dict):
        return
    code = value.get("code") or value.get("id")
    message = value.get("message") or value.get("error")
    is_finding = isinstance(code, str) and (
        isinstance(message, str) or "blocking" in value or value.get("passed") is False
    )
    if is_finding and value.get("passed") is not True:
        level = "blocker" if value.get("blocking") is True or value.get("passed") is False else severity or "warning"
        if value.get("blocking") is False:
            level = "warning"
        if code in NATIVE_SCOPE_IDENTITY_WARNING_CODES and not native_scope_warning_diagnostic_is_valid(value):
            yield {
                "severity": "blocker",
                "code": "report.native_identity_warning_invalid",
                "object": value.get("object") or value.get("component"),
                "message": (
                    f"Native scope warning {code} is forged or does not locate its component, "
                    "entity, body, indices and field"
                ),
                "source": source,
                "evidence": value,
            }
            return
        finding = {
            "severity": level,
            "code": code,
            "object": value.get("object") or value.get("instance") or value.get("document"),
            "message": message or code,
            "source": source,
            "evidence": value,
        }
        detail = value.get("detail")
        if code in NATIVE_SCOPE_IDENTITY_WARNING_CODES and isinstance(detail, dict):
            for key in (
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
            ):
                finding[key] = detail.get(key)
        yield finding
        return
    # Deterministic traversal: a persisted (sorted) receipt must render exactly
    # like the live one, so neither finding order nor the first-seen duplicate
    # anchor depends on how the job record was serialized.
    for key, child in sorted(value.items(), key=lambda item: str(item[0])):
        if key in {"structural_report", "artifacts", "owned_files", "request", "client_failure"}:
            continue
        child_severity = (
            "blocker"
            if key in {"errors", "problems"}
            else "warning"
            if key in {"warnings", "diagnostics"}
            else severity
        )
        if key in _FINDING_LISTS or isinstance(child, dict):
            yield from _findings(child, f"{source}/{key}", severity=child_severity)


def build_structural_report(receipt: dict, *, evidence_root: Path | None = None) -> dict:
    """Retain exact findings and source locations, including pre-capture refusals."""
    material_source = "evidence/raw/material_provenance.json"
    effective_source = "evidence/raw/effective_instances.json"
    sources = {"run": receipt}
    if evidence_root is not None:
        for name in REPORT_EVIDENCE_FILES:
            path = Path(evidence_root) / name
            if path.is_file():
                try:
                    sources[name] = read_data(path)
                except (OSError, ValueError) as error:
                    sources[name] = {"errors": [{"code": "report.evidence_unreadable", "message": str(error)}]}
    entries = []
    if receipt.get("error"):
        detail = receipt.get("detail")
        failure_object = receipt.get("main_assembly")
        if isinstance(detail, dict):
            # The native failure context names the exact failing leaf. Use it
            # verbatim and never invent a component the record does not carry.
            component = detail.get("component")
            document = detail.get("document")
            if isinstance(component, str) and component.strip():
                failure_object = component
            elif isinstance(document, str) and document.strip():
                failure_object = document
        entries.append(
            {
                "severity": "blocker",
                "code": receipt.get("error_code") or "pipeline.execution_failed",
                "object": failure_object,
                "message": str(receipt["error"]),
                "source": "run/error",
                "evidence": {"detail": detail, "stage": receipt.get("stage")},
            }
        )
    for source, value in sources.items():
        if source in {
            material_source,
            effective_source,
            "evidence/raw/geometry.json",
            "evidence/raw/source_scopes.json",
            "evidence/capture.json",
            "evidence/raw/scene_raw.json",
            "input/source.json",
        }:
            # Projected explicitly below: the raw warning entries carry their own
            # code/message and must not be double-reported by the generic walk.
            continue
        entries.extend(_findings(value, source))
    quality = sources.get("reports/quality.json")
    quality_diagnostics = (
        quality.get("diagnostics")
        if isinstance(quality, dict)
        else None
    )
    native_gate_warnings = None
    native_gate_passed = False
    if isinstance(quality, dict) and isinstance(quality.get("checks"), list):
        for check in quality["checks"]:
            if (
                isinstance(check, dict)
                and check.get("id") == "geometry.native_tessellation"
            ):
                native_gate_passed = (
                    check.get("state") == "passed"
                    or check.get("passed") is True
                )
                details = check.get("details")
                warnings = (
                    details.get("warnings")
                    if isinstance(details, dict)
                    else None
                )
                if isinstance(warnings, list):
                    native_gate_warnings = warnings
                break
    if quality_diagnostics is not None:
        if not isinstance(quality_diagnostics, list):
            entries.append(
                {
                    "severity": "blocker",
                    "code": "report.native_identity_warning_invalid",
                    "object": receipt.get("main_assembly"),
                    "message": "Quality diagnostics are malformed",
                    "source": "reports/quality.json/diagnostics",
                    "evidence": {"type": type(quality_diagnostics).__name__},
                }
            )
        else:
            native_scope = [
                item
                for item in quality_diagnostics
                if isinstance(item, dict)
                and item.get("code") in NATIVE_SCOPE_IDENTITY_WARNING_CODES
            ]
            raw_scope = [
                item
                for item in native_gate_warnings or []
                if isinstance(item, dict)
                and item.get("code") in NATIVE_SCOPE_IDENTITY_WARNING_CODES
            ]
            if (
                native_gate_passed
                and native_gate_warnings is None
            ) or (
                (native_scope or raw_scope)
                and (
                    native_gate_warnings is None
                    or not native_scope_warning_details_match(
                        native_scope,
                        native_gate_warnings,
                    )
                )
            ):
                entries.append(
                    {
                        "severity": "blocker",
                        "code": "report.native_identity_warning_invalid",
                        "object": receipt.get("main_assembly"),
                        "message": (
                            "Native scope warning projection is incomplete or forged"
                        ),
                        "source": "reports/quality.json/diagnostics",
                        "evidence": {"native_scope_warnings": native_scope},
                    }
                )
    closure = ((receipt.get("selection") or {}).get("manifest") or {}).get("closure") or {}
    opened = closure.get("open_status") or {}
    if opened.get("warnings"):
        entries.append(
            {
                "severity": "warning",
                "code": "native.open_warnings",
                "object": receipt.get("main_assembly"),
                "message": (
                    "SolidWorks returned warnings while opening the assembly; the raw bitmask remains in evidence."
                ),
                "source": "run/selection/manifest/closure/open_status",
                "evidence": opened,
            }
        )
    dependency_closure = sources.get("evidence/raw/dependency_closure.json")
    if isinstance(dependency_closure, dict):
        # Every recorded automation issue is reported, even when it can remain a
        # warning: evidence-covered unused stored references were never read or
        # copied, so they are projected verbatim as warnings with their exact
        # document/location evidence instead of disappearing into the raw record.
        for item in dependency_closure.get("stale_stored_references") or []:
            if not isinstance(item, dict):
                continue
            entries.append(
                {
                    "severity": "warning",
                    "code": "native.stale_stored_reference",
                    "object": item.get("document") or item.get("resolved_path"),
                    "message": (
                        "The stored SolidWorks reference is unused by the selected closure "
                        "(it resolved to no active edge, its location may be stale, and it is outside the delivery). "
                        "The pipeline neither read nor copied the external file; "
                        "it only retained the recorded text and did not block generation or independent verification."
                    ),
                    "source": "evidence/raw/dependency_closure.json/stale_stored_references",
                    "evidence": {
                        "document": item.get("document"),
                        "location": item.get("location"),
                        "resolved_path": item.get("resolved_path"),
                        "exists": item.get("exists"),
                    },
                }
            )
    material_required = False
    effective_required = False
    geometry_required = False
    if evidence_root is not None:
        snapshot_manifest = Path(evidence_root) / "evidence/manifest.json"
        if snapshot_manifest.is_file():
            try:
                snapshot = read_data(snapshot_manifest)
            except (OSError, ValueError):
                snapshot = None
            if isinstance(snapshot, dict) and isinstance(snapshot.get("files"), dict):
                material_required = "raw/material_provenance.json" in snapshot["files"]
                effective_required = "raw/effective_instances.json" in snapshot["files"]
                geometry_required = "raw/geometry.json" in snapshot["files"]
    effective_instances = sources.get(effective_source)
    if isinstance(effective_instances, dict) and effective_instances.get("errors"):
        entries.extend(_findings(effective_instances, effective_source))
        entries.append(
            {
                "severity": "blocker",
                "code": "report.effective_instances_invalid",
                "object": receipt.get("main_assembly"),
                "message": (
                    "Effective-instance evidence is unreadable. The evidence is invalid and "
                    "independent verification must fail. This report uses no defaults, offers no "
                    "substitute interpretation, and emits no effective-instance warning."
                ),
                "source": effective_source,
                "evidence": {"unreadable": True},
            }
        )
    elif effective_instances is None:
        if effective_required:
            # The manifest binds the record for every current capture; a deleted or
            # absent record is required evidence and fails closed.
            entries.append(
                {
                    "severity": "blocker",
                    "code": "report.effective_instances_missing",
                    "object": receipt.get("main_assembly"),
                    "message": (
                        "The snapshot inventory requires effective-instance evidence "
                        "(evidence/raw/effective_instances.json), but it is missing. "
                        "The evidence is invalid and independent verification must fail; "
                        "this report uses no defaults or substitute interpretation."
                    ),
                    "source": effective_source,
                    "evidence": {"required_by_manifest": True},
                }
            )
    else:
        # One canonical schema only; anything else is independently rejected instead of
        # being reinterpreted, defaulted or accepted through an alternate interface.
        effective_warnings = _canonical_effective_warnings(effective_instances)
        if effective_warnings is None:
            entries.append(
                {
                    "severity": "blocker",
                    "code": "report.effective_instances_invalid",
                    "object": receipt.get("main_assembly"),
                    "message": (
                        "Effective-instance evidence does not match the current schema. "
                        "The evidence is invalid and independent verification must fail. "
                        "This report uses no defaults or substitute interpretation and emits no "
                        "effective-instance warning; replace the evidence with a canonical record "
                        "from the current producer."
                    ),
                    "source": effective_source,
                    "evidence": {
                        "type": type(effective_instances).__name__,
                        "schema_version": (
                            effective_instances.get("schema_version") if isinstance(effective_instances, dict) else None
                        ),
                        "keys": sorted(effective_instances) if isinstance(effective_instances, dict) else None,
                    },
                }
            )
        else:
            # CAD-authored effective values are warnable: the report keeps the native
            # flags and readings verbatim, states the instance scope plainly, and never
            # claims geometric derivation or independent physical qualification.
            override_message = (
                "CAD records this component's effective override in assembly-instance scope "
                "(assembly_component_group, using assembly-document axes). "
                "The parent override replaces its subtree within one rigid body and is not "
                "distributed along the kinematic chain. "
                "The value is CAD-authored and is not geometrically derived or independently "
                "physically qualified; this report does not claim physical accuracy from it."
            )
            surface_message = (
                "This component has surfaces but no solid, so the part document cannot provide "
                "mass properties; CAD records an effective assembly-instance value "
                "(assembly_component_group, using assembly-document axes). "
                "Mass, COM, and inertia come from the CAD-authored value and are not geometrically "
                "derived or independently physically qualified; this report neither claims physical "
                "accuracy nor substitutes or distributes the value."
            )
            difference_message = (
                "This leaf instance's assembly-component-group measurement differs from its part-document measurement "
                "(basis: cad_assembly_instance_measurement), and the cause is unproven. "
                "This report retains both readings for engineering review without scaling or "
                "substitution. The value is a CAD instance-context record, not a geometric "
                "derivation or independent physical qualification, and does not establish "
                "physical accuracy."
            )
            for item in effective_warnings:
                if item["code"] == "cad_surface_only_effective_instance":
                    message = surface_message
                elif item["code"] == "cad_instance_measurement_differs_from_part":
                    message = difference_message
                else:
                    message = override_message
                entries.append(
                    {
                        "severity": "warning",
                        "code": item["code"],
                        "object": item.get("component") or item.get("document") or item.get("part_document"),
                        "message": message,
                        "source": "evidence/raw/effective_instances.json/warnings",
                        "evidence": item,
                    }
                )
    geometry_source = "evidence/raw/geometry.json"
    geometry = sources.get(geometry_source)
    if isinstance(geometry, dict) and geometry.get("errors"):
        entries.extend(_findings(geometry, geometry_source))
        entries.append(
            {
                "severity": "blocker",
                "code": "report.geometry_metadata_invalid",
                "object": receipt.get("main_assembly"),
                "message": (
                    "Geometry metadata is unreadable. The evidence is invalid and independent "
                    "verification must fail; this report applies no area tolerance or substitute "
                    "interpretation."
                ),
                "source": geometry_source,
                "evidence": {"unreadable": True},
            }
        )
    elif geometry is None:
        if geometry_required:
            entries.append(
                {
                    "severity": "blocker",
                    "code": "report.geometry_metadata_missing",
                    "object": receipt.get("main_assembly"),
                    "message": (
                        "The snapshot inventory requires geometry evidence "
                        "(evidence/raw/geometry.json), but it is missing. The evidence is invalid "
                        "and independent verification must fail; this report uses no defaults or "
                        "substitute interpretation."
                    ),
                    "source": geometry_source,
                    "evidence": {"required_by_manifest": True},
                }
            )
    else:
        untessellated = _canonical_untessellated_faces(
            geometry,
        )
        if untessellated is None:
            entries.append(
                {
                    "severity": "blocker",
                    "code": "report.geometry_metadata_invalid",
                    "object": receipt.get("main_assembly"),
                    "message": (
                        "Geometry metadata does not match the approved schema. The evidence is "
                        "invalid and independent verification must fail; this report applies no area "
                        "tolerance or substitute interpretation."
                    ),
                    "source": geometry_source,
                    "evidence": {
                        "type": type(geometry).__name__,
                        "keys": sorted(geometry) if isinstance(geometry, dict) else None,
                    },
                }
            )
        else:
            for row in untessellated:
                if "tessellate_probe_error" in row:
                    facets = (
                        "Native tessellation returned zero triangles, and the facet probe still "
                        "failed when retried on the degenerate geometry"
                        f" ({row['tessellate_probe_error']})"
                    )
                else:
                    facets = "native tessellation returned zero triangles and zero facets"
                entries.append(
                    {
                        "severity": "warning",
                        "code": "cad_sheet_face_untessellated",
                        "object": f"{row['component']} sheet body {row['body_index']} face {row['face_index']}",
                        "message": (
                            f"This surface (component {row['component']}; body_type 1; body_index {row['body_index']}, "
                            f"face_index {row['face_index']}); {facets}; "
                            f"native area is {row['area_m2']!r} m^2: CAD provided no exportable "
                            "tessellation for the surface. This report retains the raw fact without "
                            "claiming zero area; surface completeness and usability are not "
                            "independently confirmed."
                        ),
                        "source": "evidence/raw/geometry.json/tessellation/no_facets_faces",
                        "evidence": row,
                    }
                )
    material_provenance = sources.get(material_source)
    if isinstance(material_provenance, dict) and material_provenance.get("errors"):
        entries.extend(_findings(material_provenance, material_source))
        entries.append(
            {
                "severity": "blocker",
                "code": "report.material_provenance_invalid",
                "object": receipt.get("main_assembly"),
                "message": (
                    "Material-provenance evidence is unreadable. The evidence is invalid and "
                    "independent verification must fail; this report uses no defaults or substitute "
                    "interpretation and emits no material warning from it."
                ),
                "source": material_source,
                "evidence": {"unreadable": True},
            }
        )
    elif material_provenance is None:
        if material_required:
            # The snapshot manifest binds the record for every current capture; a deleted or
            # absent record is required evidence and fails closed.
            entries.append(
                {
                    "severity": "blocker",
                    "code": "report.material_provenance_missing",
                    "object": receipt.get("main_assembly"),
                    "message": (
                        "The snapshot inventory requires material-provenance evidence "
                        "(evidence/raw/material_provenance.json), but it is missing. The evidence is "
                        "invalid and independent verification must fail; this report uses no defaults "
                        "or substitute interpretation."
                    ),
                    "source": material_source,
                    "evidence": {"required_by_manifest": True},
                }
            )
    else:
        # One canonical schema only; anything else is independently rejected instead of
        # being reinterpreted, defaulted or accepted through an alternate interface.
        material_warnings = _canonical_material_warnings(material_provenance)
        if material_warnings is None:
            entries.append(
                {
                    "severity": "blocker",
                    "code": "report.material_provenance_invalid",
                    "object": receipt.get("main_assembly"),
                    "message": (
                        "Material-provenance evidence does not match the current schema. The "
                        "evidence is invalid and independent verification must fail. This report uses "
                        "no defaults or substitute interpretation and emits no material warning from "
                        "it; replace the evidence with a canonical record from the current producer."
                    ),
                    "source": material_source,
                    "evidence": {
                        "type": type(material_provenance).__name__,
                        "schema_version": (
                            material_provenance.get("schema_version") if isinstance(material_provenance, dict) else None
                        ),
                        "keys": sorted(material_provenance) if isinstance(material_provenance, dict) else None,
                    },
                }
            )
        else:
            # Missing explicit material is a warnable defect: the report renders the exact
            # reported assignment, body indices and native measured values, and never
            # claims the material or the physical/dynamic behaviour is verified.
            for item in material_warnings:
                blocking = item["blocking"] is True
                entries.append(
                    {
                        "severity": "blocker" if blocking else "warning",
                        "code": "native.material_provenance_missing",
                        "object": item.get("component") or item.get("document"),
                        "message": (
                            "The SolidWorks body has no explicit physical material assignment "
                            "(default density is not material evidence): "
                        )
                        + (
                            "The record is blocking and independent verification must fail."
                            if blocking
                            else "recorded as a warning-level defect: the pipeline preserves "
                            "measured CAD mass, COM, and inertia and continues generation and "
                            "independent verification. This report does not claim verified material; "
                            "material truth and physical/dynamic qualification remain external "
                            "engineering confirmation."
                        ),
                        "source": "evidence/raw/material_provenance.json/warnings",
                        "evidence": {
                            "code": item.get("code"),
                            "component": item.get("component"),
                            "document": item.get("document"),
                            "configuration": item.get("configuration"),
                            "missing_body_indices": item.get("missing_body_indices"),
                            "material_assignment": item.get("material_assignment"),
                            "measured": item.get("measured"),
                        },
                    }
                )
    # The same warning may appear in discovery, input validation and a stage
    # event. Preserve its first evidence location without repeating it.
    unique = {}
    for entry in entries:
        key = json.dumps({k: v for k, v in entry.items() if k != "source"}, ensure_ascii=False, sort_keys=True)
        unique.setdefault(key, entry)
    findings = list(unique.values())
    urdf_files = []
    if evidence_root is not None:
        urdf_root = Path(evidence_root) / "urdf"
        if urdf_root.is_dir():
            urdf_files = [p.relative_to(evidence_root).as_posix() for p in sorted(urdf_root.rglob("*.urdf"))]
    return {
        "schema_version": REPORT_SCHEMA,
        "run_id": receipt.get("run_id"),
        "state": receipt.get("status") or receipt.get("state"),
        "main_assembly": receipt.get("main_assembly"),
        "upload_sha256": (receipt.get("request") or {}).get("upload_sha256"),
        "handoff_sha256": receipt.get("handoff_sha256"),
        "urdf_files": urdf_files,
        "summary": {
            "warnings": sum(item["severity"] == "warning" for item in findings),
            "blockers": sum(item["severity"] == "blocker" for item in findings),
        },
        "findings": findings,
    }


def _fenced(value) -> str:
    text = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
    fence = "`" * max(3, 1 + max((len(match) for match in re.findall(r"`+", text)), default=0))
    return f"{fence}json\n{text}\n{fence}"


def render_structural_markdown(report: dict) -> str:
    summary = report["summary"]
    lines = [
        "# Structural CAD-to-URDF issue report",
        "",
        f"Run: {report.get('run_id') or 'not specified'}",
        "",
        f"Run state: {report.get('state') or 'not recorded'}; "
        f"{summary['warnings']} warnings and {summary['blockers']} blocking issues.",
        "",
        "This report is generated by the pipeline from recorded run facts. Warnings remain "
        "visible; the run quality report decides whether independent verification passed. "
        "This report does not invent missing CAD engineering parameters.",
        "",
        "## Inputs and outputs",
        "",
        _fenced({key: report.get(key) for key in ("main_assembly", "upload_sha256", "handoff_sha256", "urdf_files")}),
        "",
    ]
    if not report["urdf_files"]:
        lines.extend(
            [
                "This stage has not produced a URDF; after a successful native capture, "
                "Linux generation stages produce it.",
                "",
            ]
        )
    if not report["findings"]:
        lines.extend(["The stage records contain no issues requiring structural-team action.", ""])
    for index, item in enumerate(report["findings"], 1):
        label = "Warning" if item["severity"] == "warning" else "Blocking"
        # Evidence strings remain inside JSON fences, so CAD names cannot alter
        # report structure or turn recorded text into active markup.
        lines.extend(
            [
                f"## {index}. {label}",
                "",
                _fenced({key: item.get(key) for key in ("code", "object", "message", "source", "evidence")}),
                "",
                "Action: inspect the assembly references, exporter declarations, and related "
                "parameters using the CAD object and raw evidence above. "
                "If the reader is defective, fix the pipeline and rerun against the same CAD bytes.",
                "",
            ]
        )
    return "\n".join(lines)


def write_structural_report(root: Path, receipt: dict, *, evidence_root: Path | None = None) -> dict:
    root = Path(root)
    report = build_structural_report(receipt, evidence_root=evidence_root)
    write_json(root / JSON_PATH, report)
    markdown = root / MARKDOWN_PATH
    markdown.write_text(render_structural_markdown(report), encoding="utf-8")
    return {
        "markdown": {"path": MARKDOWN_PATH, "sha256": file_digest(markdown)},
        "json": {"path": JSON_PATH, "sha256": file_digest(root / JSON_PATH)},
    }
