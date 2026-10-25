"""Fail-closed checks for raw SolidWorks appearance evidence."""

from __future__ import annotations

import copy
import math
import posixpath
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ...io import PipelineError

UNRECORDED_APPEARANCE = object()


@dataclass(frozen=True)
class AppearanceAudit:
    """The renderability decision for one occurrence's raw appearance facts."""

    component: str
    state: str
    issues: tuple[dict[str, Any], ...]
    claims: tuple[dict[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "component": self.component,
            "state": self.state,
            "issues": [dict(issue) for issue in self.issues],
            "claims": [dict(claim) for claim in self.claims],
        }


class AppearanceError(PipelineError):
    """A raw appearance record cannot be represented without inventing values."""

    def __init__(self, audit: AppearanceAudit) -> None:
        first = audit.issues[0]["code"] if audit.issues else "cad_appearance_unrepresentable"
        super().__init__(
            f"CAD appearance cannot be represented faithfully for "
            f"{audit.component}: {first}"
        )
        self.appearance = audit.to_dict()
        self.details = {"appearance": self.appearance}


def _invalid_appearance(
    component: str,
    code: str,
    path: str,
    message: str,
    **detail: Any,
) -> AppearanceError:
    return AppearanceError(
        AppearanceAudit(
            component,
            "invalid",
            (_issue(code, path, message, **detail),),
            (),
        )
    )


def _issue(
    code: str,
    path: str,
    message: str,
    **detail: Any,
) -> dict[str, Any]:
    return {
        "code": code,
        "path": path,
        "message": message,
        **detail,
    }


def _same_document_path(left: object, right: object) -> bool:
    if not isinstance(left, str) or not isinstance(right, str) or not left or not right:
        return False

    def normalized(value: str) -> str:
        return posixpath.normpath(value.replace("\\", "/")).casefold()

    return normalized(left) == normalized(right)


def _valid_values(value: object) -> bool:
    return (
        isinstance(value, list)
        and len(value) == 9
        and all(
            type(item) in (int, float) and math.isfinite(float(item))
            for item in value
        )
    )


def _requested_material_call(configuration: str) -> dict[str, Any]:
    return {
        "configuration_semantics": "requested_configuration",
        "configuration_option": 3,
        "configuration_names": [configuration],
    }


def _active_material_call(configuration: str) -> dict[str, Any]:
    return {
        "configuration_semantics": "active_configuration",
        "active_configuration": configuration,
    }


def _audit_owner(
    raw: object,
    *,
    path: str,
    owner: str,
    expected_configuration: str,
    material_call: dict[str, Any],
    claims: list[dict[str, Any]],
    issues: list[dict[str, Any]],
) -> None:
    if not isinstance(raw, Mapping):
        issues.append(
            _issue(
                "cad_appearance_owner_missing",
                path,
                "the appearance owner record is absent or malformed",
                owner=owner,
            )
        )
        return

    used_api = raw.get("used_api")
    if not isinstance(used_api, str) or not used_api:
        issues.append(
            _issue(
                "cad_appearance_api_missing",
                path + ".used_api",
                "the appearance owner record names no native API",
                owner=owner,
            )
        )

    raw_material_call = raw.get("material_call")
    if raw_material_call != material_call:
        issues.append(
            _issue(
                "cad_appearance_configuration_mismatch",
                path + ".material_call",
                "the appearance material call is not bound to the expected configuration",
                owner=owner,
                expected=material_call,
                observed=raw_material_call,
            )
        )

    presence_call = raw.get("presence_call")
    if presence_call != {
        "arguments": [],
        "configuration_semantics": "native_owner_context",
    }:
        issues.append(
            _issue(
                "cad_appearance_presence_context_invalid",
                path + ".presence_call",
                "the appearance presence call does not retain its native owner context",
                owner=owner,
                observed=presence_call,
            )
        )

    read_errors = raw.get("read_errors")
    if not isinstance(read_errors, Mapping):
        issues.append(
            _issue(
                "cad_appearance_errors_malformed",
                path + ".read_errors",
                "the appearance error record is not an object",
                owner=owner,
            )
        )
    else:
        for field, error in sorted(read_errors.items()):
            issues.append(
                _issue(
                    "cad_appearance_read_failed",
                    f"{path}.read_errors.{field}",
                    "an appearance read failed and cannot be treated as absence",
                    owner=owner,
                    field=str(field),
                    error=error,
                )
            )

    values = raw.get("material_property_values")
    has_values = raw.get("has_material_property_values")
    values_are_valid = _valid_values(values)
    if values is not None and not values_are_valid:
        issues.append(
            _issue(
                "cad_appearance_values_invalid",
                path + ".material_property_values",
                "material values are not the documented nine finite numbers",
                owner=owner,
                observed=values,
            )
        )
    if type(has_values) is not bool:
        issues.append(
            _issue(
                "cad_appearance_presence_invalid",
                path + ".has_material_property_values",
                "the appearance presence flag is not a native boolean",
                owner=owner,
                observed=has_values,
            )
        )
    elif has_values:
        claims.append(
            {
                "owner": owner,
                "path": path,
                "kind": "material_property_values",
                "material_property_values": values,
                "used_api": used_api,
            }
        )
    elif values_are_valid and any(float(value) != -1.0 for value in values):
        issues.append(
            _issue(
                "cad_appearance_presence_conflict",
                path,
                "the presence flag denies material values while the raw vector carries data",
                owner=owner,
                material_property_values=values,
            )
        )

    texture = raw.get("texture")
    if not isinstance(texture, Mapping):
        issues.append(
            _issue(
                "cad_appearance_texture_invalid",
                path + ".texture",
                "the texture record is absent or malformed",
                owner=owner,
            )
        )
        return
    texture_path = path + ".texture"
    if texture.get("configuration") != expected_configuration:
        issues.append(
            _issue(
                "cad_appearance_configuration_mismatch",
                texture_path + ".configuration",
                "the texture record is not bound to the expected configuration",
                owner=owner,
                expected=expected_configuration,
                observed=texture.get("configuration"),
            )
        )
    texture_state = texture.get("state")
    material_name = texture.get("material_name")
    if texture_state == "absent":
        if material_name is not None:
            issues.append(
                _issue(
                    "cad_appearance_texture_state_conflict",
                    texture_path,
                    "an absent texture record carries a material name",
                    owner=owner,
                    material_name=material_name,
                )
            )
    elif texture_state == "present":
        claims.append(
            {
                "owner": owner,
                "path": texture_path,
                "kind": "texture",
                "material_name": material_name,
                "used_api": used_api,
            }
        )
        issues.append(
            _issue(
                "cad_appearance_texture_unrepresentable",
                texture_path,
                "the raw texture pointer has no qualified asset, binding, and UV consumer",
                owner=owner,
                material_name=material_name,
            )
        )
    elif texture_state == "unreadable":
        issues.append(
            _issue(
                "cad_appearance_read_failed",
                texture_path,
                "the texture state is unreadable and cannot be treated as absent",
                owner=owner,
            )
        )
    else:
        issues.append(
            _issue(
                "cad_appearance_texture_state_invalid",
                texture_path + ".state",
                "the texture state is not absent, present, or unreadable",
                owner=owner,
                observed=texture_state,
            )
        )


def audit_appearance(
    raw: object,
    *,
    component: str,
    expected_document_path: object,
    expected_configuration: object,
    expected_bodies: Sequence[Mapping[str, Any]] | None = None,
) -> AppearanceAudit:
    """Audit one raw occurrence without deriving an effective color."""

    component = str(component)
    expected_configuration_text = (
        expected_configuration if isinstance(expected_configuration, str) else ""
    )
    if raw is UNRECORDED_APPEARANCE:
        return AppearanceAudit(component, "unobserved", (), ())
    if raw is None:
        issue = _issue(
            "cad_appearance_record_missing",
            "appearance_raw",
            "an explicit appearance record is missing and cannot be treated as absence",
        )
        return AppearanceAudit(component, "invalid", (issue,), ())
    if not isinstance(raw, Mapping):
        issue = _issue(
            "cad_appearance_record_invalid",
            "appearance_raw",
            "the occurrence appearance record is not an object",
        )
        return AppearanceAudit(component, "invalid", (issue,), ())

    issues: list[dict[str, Any]] = []
    claims: list[dict[str, Any]] = []
    if raw.get("component") != component:
        issues.append(
            _issue(
                "cad_appearance_object_mismatch",
                "appearance_raw.component",
                "the appearance record names another occurrence",
                expected=component,
                observed=raw.get("component"),
            )
        )
    if not _same_document_path(
        raw.get("source_document_path"),
        expected_document_path,
    ):
        issues.append(
            _issue(
                "cad_appearance_owner_mismatch",
                "appearance_raw.source_document_path",
                "the appearance record names another source document",
                expected=expected_document_path,
                observed=raw.get("source_document_path"),
            )
        )
    if (
        not expected_configuration_text
        or raw.get("referenced_configuration") != expected_configuration_text
    ):
        issues.append(
            _issue(
                "cad_appearance_configuration_mismatch",
                "appearance_raw.referenced_configuration",
                "the appearance record names another referenced configuration",
                expected=expected_configuration_text,
                observed=raw.get("referenced_configuration"),
            )
        )

    active_error = raw.get("native_active_configuration_error")
    if active_error is not None:
        issues.append(
            _issue(
                "cad_appearance_read_failed",
                "appearance_raw.native_active_configuration_error",
                "the native active configuration is unreadable",
                error=active_error,
            )
        )
    native_active = raw.get("native_active_configuration")
    if not isinstance(native_active, str) or not native_active:
        issues.append(
            _issue(
                "cad_appearance_active_configuration_invalid",
                "appearance_raw.native_active_configuration",
                "the native active configuration is malformed",
                observed=native_active,
            )
        )

    if expected_configuration_text:
        _audit_owner(
            raw.get("document"),
            path="appearance_raw.document",
            owner="document",
            expected_configuration=expected_configuration_text,
            material_call=_requested_material_call(expected_configuration_text),
            claims=claims,
            issues=issues,
        )
        _audit_owner(
            raw.get("occurrence"),
            path="appearance_raw.occurrence",
            owner="occurrence",
            expected_configuration=expected_configuration_text,
            material_call=_requested_material_call(expected_configuration_text),
            claims=claims,
            issues=issues,
        )

    bodies = raw.get("bodies")
    body_ids: set[tuple[object, object]] = set()
    if not isinstance(bodies, list):
        issues.append(
            _issue(
                "cad_appearance_bodies_invalid",
                "appearance_raw.bodies",
                "the per-body appearance inventory is not a list",
            )
        )
    else:
        active_configuration = (
            native_active if isinstance(native_active, str) and native_active else ""
        )
        for index, body in enumerate(bodies):
            path = f"appearance_raw.bodies[{index}]"
            if not isinstance(body, Mapping):
                issues.append(
                    _issue(
                        "cad_appearance_body_invalid",
                        path,
                        "the body appearance binding is not an object",
                    )
                )
                continue
            if body.get("component") != component:
                issues.append(
                    _issue(
                        "cad_appearance_object_mismatch",
                        path + ".component",
                        "the body appearance record names another occurrence",
                        expected=component,
                        observed=body.get("component"),
                    )
                )
            body_type = body.get("body_type")
            body_index = body.get("body_index")
            if body_type not in (0, 1) or type(body_index) is not int or body_index < 0:
                issues.append(
                    _issue(
                        "cad_appearance_body_identity_invalid",
                        path,
                        "the body appearance record has no exact native body identity",
                        body_type=body_type,
                        body_index=body_index,
                    )
                )
            else:
                identity = (body_type, body_index)
                if identity in body_ids:
                    issues.append(
                        _issue(
                            "cad_appearance_body_repeated",
                            path,
                            "the body appearance record repeats one native body",
                            body_type=body_type,
                            body_index=body_index,
                        )
                    )
                body_ids.add(identity)
            if body.get("enumeration_api") != "IComponent2.GetBodies2":
                issues.append(
                    _issue(
                        "cad_appearance_body_api_mismatch",
                        path + ".enumeration_api",
                        "the body appearance record is not bound to the primary body enumeration",
                        observed=body.get("enumeration_api"),
                    )
                )
            if not active_configuration:
                continue
            _audit_owner(
                body.get("appearance"),
                path=path + ".appearance",
                owner=f"body[{body_type}/{body_index}]",
                expected_configuration=active_configuration,
                material_call=_active_material_call(active_configuration),
                claims=claims,
                issues=issues,
            )

    if expected_bodies is not None:
        expected_body_ids = set()
        malformed_expected = False
        for body in expected_bodies:
            if not isinstance(body, Mapping):
                malformed_expected = True
                continue
            body_type = body.get("body_type")
            body_index = body.get("body_index")
            if body_type not in (0, 1) or type(body_index) is not int or body_index < 0:
                malformed_expected = True
                continue
            expected_body_ids.add((body_type, body_index))
        if malformed_expected or expected_body_ids != body_ids:
            issues.append(
                _issue(
                    "cad_appearance_body_inventory_mismatch",
                    "appearance_raw.bodies",
                    "appearance owners do not bind exactly the captured native bodies",
                    expected=sorted(expected_body_ids),
                    observed=sorted(body_ids),
                )
            )

    vectors = {
        tuple(float(value) for value in claim["material_property_values"])
        for claim in claims
        if claim.get("kind") == "material_property_values"
        and _valid_values(claim.get("material_property_values"))
    }
    if len(vectors) > 1:
        issues.append(
            _issue(
                "cad_appearance_owner_conflict",
                "appearance_raw",
                "appearance owners expose conflicting material-property values",
                values=[list(vector) for vector in sorted(vectors)],
            )
        )
    if claims:
        issues.append(
            _issue(
                "cad_appearance_effective_scope_unproven",
                "appearance_raw",
                "the current facts do not prove the effective appearance of every delivered face",
            )
        )

    state = "unobserved" if not issues else "unrepresentable"
    return AppearanceAudit(
        component,
        state,
        tuple(issues),
        tuple(claims),
    )


def bind_capture_appearance(
    raw_scene: object,
    geometry_rows: object,
    native_facts: object,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Bind native appearance and source scopes into copied scene projections.

    ``native_capture_facts`` is the sealed authority.  The scene and geometry
    projections remain unchanged on disk; consumers receive an in-memory view
    with exact occurrence bindings.
    """

    if not isinstance(raw_scene, Mapping):
        raise _invalid_appearance(
            "",
            "cad_appearance_scene_projection_invalid",
            "scene_raw",
            "the sealed scene projection is not an object",
        )
    if not isinstance(geometry_rows, list):
        raise _invalid_appearance(
            "",
            "cad_appearance_geometry_projection_invalid",
            "geometry",
            "the sealed geometry projection is not a list",
        )
    if not isinstance(native_facts, Mapping):
        raise _invalid_appearance(
            "",
            "cad_appearance_native_facts_missing",
            "native_capture_facts",
            "the sealed native facts are unavailable",
        )
    native_scene = native_facts.get("scene_raw")
    occurrences = (
        native_scene.get("occurrences")
        if isinstance(native_scene, Mapping)
        else None
    )
    source_scopes = native_facts.get("source_scopes")
    if not isinstance(occurrences, list) or not isinstance(source_scopes, Mapping):
        raise _invalid_appearance(
            "",
            "cad_appearance_native_facts_invalid",
            "native_capture_facts",
            "the sealed native facts carry no occurrence appearance binding",
        )

    by_occurrence: dict[str, Mapping[str, Any]] = {}
    for index, occurrence in enumerate(occurrences):
        if not isinstance(occurrence, Mapping):
            raise _invalid_appearance(
                "",
                "cad_appearance_occurrence_invalid",
                f"scene_raw.occurrences[{index}]",
                "a native occurrence appearance binding is not an object",
            )
        name = occurrence.get("name")
        if not isinstance(name, str) or not name or name in by_occurrence:
            raise _invalid_appearance(
                str(name or ""),
                "cad_appearance_occurrence_identity_invalid",
                f"scene_raw.occurrences[{index}].name",
                "a native occurrence appearance binding has no unique identity",
                observed=name,
            )
        by_occurrence[name] = occurrence

    raw = copy.deepcopy(dict(raw_scene))
    raw_components = raw.get("components")
    if not isinstance(raw_components, list):
        raise _invalid_appearance(
            "",
            "cad_appearance_scene_projection_invalid",
            "scene_raw.components",
            "the sealed scene projection carries no component list",
        )
    projected_names: set[str] = set()
    for index, component in enumerate(raw_components):
        if not isinstance(component, dict):
            raise _invalid_appearance(
                "",
                "cad_appearance_scene_component_invalid",
                f"scene_raw.components[{index}]",
                "a scene component projection is not an object",
            )
        name = component.get("name")
        if not isinstance(name, str) or not name or name in projected_names:
            raise _invalid_appearance(
                str(name or ""),
                "cad_appearance_scene_component_identity_invalid",
                f"scene_raw.components[{index}].name",
                "a scene component projection has no unique identity",
                observed=name,
            )
        projected_names.add(name)
        occurrence = by_occurrence.get(name)
        if occurrence is None:
            raise _invalid_appearance(
                name,
                "cad_appearance_occurrence_missing",
                f"scene_raw.components[{index}]",
                "a projected scene component has no native appearance occurrence",
            )
        if "appearance_raw" in occurrence:
            component["appearance_raw"] = copy.deepcopy(
                occurrence.get("appearance_raw")
            )
    attached_occurrences = {
        name
        for name, occurrence in by_occurrence.items()
        if "appearance_raw" in occurrence
    }
    if attached_occurrences and attached_occurrences != projected_names:
        raise _invalid_appearance(
            "",
            "cad_appearance_occurrence_inventory_incomplete",
            "scene_raw.occurrences",
            "only some native occurrences carry appearance facts",
            missing=sorted(projected_names - attached_occurrences),
            extra=sorted(attached_occurrences - projected_names),
        )

    geometry = copy.deepcopy(geometry_rows)
    for index, row in enumerate(geometry):
        if not isinstance(row, dict):
            raise _invalid_appearance(
                "",
                "cad_appearance_geometry_row_invalid",
                f"geometry[{index}]",
                "a geometry projection row is not an object",
            )
        name = row.get("component")
        if not isinstance(name, str) or not name:
            raise _invalid_appearance(
                "",
                "cad_appearance_geometry_component_invalid",
                f"geometry[{index}].component",
                "a geometry projection row has no component identity",
            )
        scope = source_scopes.get(name)
        if not isinstance(scope, Mapping):
            raise _invalid_appearance(
                name,
                "cad_appearance_source_scope_missing",
                f"source_scopes.{name}",
                "a native occurrence has no sealed source scope",
            )
        projected_scope = row.get("source_scope")
        if projected_scope is not None and projected_scope != scope:
            raise _invalid_appearance(
                name,
                "cad_appearance_source_scope_mismatch",
                f"geometry[{index}].source_scope",
                "the geometry projection names another source scope",
                expected=scope,
                observed=projected_scope,
            )
        row["source_scope"] = copy.deepcopy(dict(scope))
    return raw, geometry


def require_deliverable_appearance(
    raw: object,
    *,
    component: str,
    expected_document_path: object,
    expected_configuration: object,
    expected_bodies: Sequence[Mapping[str, Any]] | None = None,
) -> AppearanceAudit:
    """Reject unless the raw facts prove a faithfully renderable appearance."""

    audit = audit_appearance(
        raw,
        component=component,
        expected_document_path=expected_document_path,
        expected_configuration=expected_configuration,
        expected_bodies=expected_bodies,
    )
    if not audit.issues:
        unproven = _issue(
            "cad_appearance_effective_scope_unproven",
            "appearance_raw",
            "the current facts do not prove the effective appearance of every delivered face",
        )
        audit = AppearanceAudit(
            audit.component,
            "unrepresentable",
            (*audit.issues, unproven),
            audit.claims,
        )
    raise AppearanceError(audit)
