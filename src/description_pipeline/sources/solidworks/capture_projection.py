"""Portable projection of sealed SolidWorks Capture facts.

This module contains no COM imports and never opens a native document.  It turns
the one raw Capture fact set into the portable scene, mass, link-group and axis
values consumed after the owned CAD session has closed.
"""

from __future__ import annotations

import copy
import math
import ntpath
from collections.abc import Mapping
from pathlib import Path

from ...geometry.native_scope import native_scope_identity_sha256
from ...geometry.native_tessellation import (
    VerifiedNativeTriangleStream,
    inspect_native_triangle_stream,
    native_triangle_relative_path,
)
from ...geometry.native_transform import transform_from_solidworks
from ...io import PipelineError, confined, read_data
from ...schemas import (
    EFFECTIVE_INSTANCES_SCHEMA,
    LINK_GROUP_MASS_SCHEMA,
    NATIVE_CAPTURE_FACTS,
    NATIVE_CAPTURE_FACTS_KEYS,
    NATIVE_CAPTURE_OBSERVATION_KEYS,
    NATIVE_CAPTURE_OBSERVATION_SCHEMA,
    NATIVE_SCENE_RAW_KEYS,
)
from .capture_geometry import RAW_GEOMETRY_KEYS
from .capture_package import validate_capture_plan_shape
from .errors import CadError
from .group_selection import compose_link_group_selections
from .inertia_convention import POSITIVE, STANDARD, detect_family
from .protocol import Matrix, RawComponent, RawScene


LINK_GROUP_USED_API = "IMassProperty2 (SelectedItems = component set)"
LINK_GROUP_SELECTION_RULE = "top_most_providers+uncovered_leaves"


def build_native_capture_observation(
    *,
    facts: Mapping,
    geometry_raw: Mapping,
    axis_references_raw: list,
    document_state: Mapping,
    input_stability: Mapping,
) -> dict:
    """Assemble the one raw-only observation without projecting it."""

    scene_raw = facts.get("scene_raw") if isinstance(facts, Mapping) else None
    if (
        not isinstance(facts, Mapping)
        or set(facts) != NATIVE_CAPTURE_FACTS_KEYS
        or not isinstance(scene_raw, Mapping)
        or set(scene_raw) != NATIVE_SCENE_RAW_KEYS
        or not isinstance(geometry_raw, Mapping)
        or not isinstance(axis_references_raw, list)
        or not isinstance(document_state, Mapping)
        or not isinstance(input_stability, Mapping)
    ):
        raise PipelineError("Native Capture raw observation is incomplete")
    return {
        "schema_version": NATIVE_CAPTURE_OBSERVATION_SCHEMA,
        **copy.deepcopy(dict(facts)),
        "geometry_raw": copy.deepcopy(dict(geometry_raw)),
        "axis_references_raw": copy.deepcopy(axis_references_raw),
        "document_state": copy.deepcopy(dict(document_state)),
        "input_stability": copy.deepcopy(dict(input_stability)),
    }


def load_native_capture_observation(
    evidence_root: Path,
    *,
    verified_streams: dict[str, VerifiedNativeTriangleStream] | None = None,
) -> dict:
    """Load and byte-bind the raw-only Capture observation.

    The returned mapping is the existing raw-record contract extended with
    raw geometry metadata and raw axis rows.  No projected values are read or
    persisted here.
    """

    root = Path(evidence_root)
    if root.name != "evidence" and (root / "evidence").is_dir():
        root = root / "evidence"
    record = read_data(confined(root, NATIVE_CAPTURE_FACTS))
    if (
        not isinstance(record, dict)
        or set(record) != NATIVE_CAPTURE_OBSERVATION_KEYS
        or record.get("schema_version") != NATIVE_CAPTURE_OBSERVATION_SCHEMA
    ):
        raise PipelineError("Native Capture observation is incomplete or unknown")
    source_scopes = record.get("source_scopes")
    geometry_raw = record.get("geometry_raw")
    axes_raw = record.get("axis_references_raw")
    if (
        not isinstance(source_scopes, dict)
        or not isinstance(geometry_raw, dict)
        or set(source_scopes) != set(geometry_raw)
        or not isinstance(axes_raw, list)
    ):
        raise PipelineError("Native Capture observation inventories differ")
    scene_raw = record.get("scene_raw")
    document_state = record.get("document_state")
    scene_document = (
        scene_raw.get("document") if isinstance(scene_raw, Mapping) else None
    )
    scene_configuration = (
        scene_raw.get("configuration")
        if isinstance(scene_raw, Mapping)
        else None
    )
    if (
        not isinstance(scene_document, str)
        or not scene_document
        or not isinstance(scene_configuration, str)
        or not scene_configuration
        or not isinstance(document_state, dict)
        or set(document_state) != {"before", "after"}
        or not isinstance(scene_raw, Mapping)
        or set(scene_raw) != NATIVE_SCENE_RAW_KEYS
    ):
        raise PipelineError("Native Capture document identity is incomplete")
    normalized_document = ntpath.normcase(
        ntpath.normpath(scene_document.replace("/", "\\"))
    )
    for phase in ("before", "after"):
        state = document_state.get(phase)
        state_path = state.get("path") if isinstance(state, dict) else None
        if (
            not isinstance(state_path, str)
            or ntpath.normcase(
                ntpath.normpath(state_path.replace("/", "\\"))
            )
            != normalized_document
            or state.get("read_only") is not True
            or state.get("active_configuration") != scene_configuration
        ):
            raise PipelineError(
                f"Native Capture document {phase} state is not the requested read-only configuration"
            )
    for component, raw in geometry_raw.items():
        if (
            not isinstance(component, str)
            or not component
            or not isinstance(raw, dict)
            or set(raw) != RAW_GEOMETRY_KEYS
        ):
            raise PipelineError("Native Capture geometry record is malformed")
        source_scope = source_scopes.get(component)
        if (
            not isinstance(source_scope, dict)
            or raw.get("source_scope_sha256")
            != native_scope_identity_sha256(source_scope)
        ):
            raise PipelineError(
                f"Native Capture geometry scope differs: {component}"
            )
        stream_path = raw.get("stream_path")
        stream_sha256 = raw.get("stream_sha256")
        if (
            stream_path != native_triangle_relative_path(str(stream_sha256))
            or confined(root, str(stream_path), exists=False).is_symlink()
        ):
            raise PipelineError(
                f"Native Capture triangle path is not canonical: {component}"
            )
        stream = confined(root, str(stream_path))
        verified = inspect_native_triangle_stream(
            stream,
            expected_sha256=str(stream_sha256),
            expected_count=raw.get("triangle_count"),
        )
        if verified_streams is not None:
            verified_streams[component] = verified
    return record


def validate_native_capture_plan(observation: Mapping, plan: Mapping) -> None:
    """Bind sealed plan selectors to raw member identities without projecting."""

    if (
        not isinstance(observation, Mapping)
        or set(observation) != NATIVE_CAPTURE_OBSERVATION_KEYS
        or observation.get("schema_version") != NATIVE_CAPTURE_OBSERVATION_SCHEMA
    ):
        raise PipelineError("Native Capture observation or plan is incomplete")
    plan = validate_capture_plan_shape(plan)
    source_scopes = observation.get("source_scopes")
    geometry_raw = observation.get("geometry_raw")
    part_mass = observation.get("part_mass_raw")
    assembly_mass = observation.get("assembly_mass_raw")
    component_mass = observation.get("component_mass_raw")
    link_group = observation.get("link_group_mass_raw")
    scene_raw = observation.get("scene_raw")
    if (
        not isinstance(source_scopes, dict)
        or not source_scopes
        or not isinstance(geometry_raw, dict)
        or set(geometry_raw) != set(source_scopes)
        or not isinstance(part_mass, dict)
        or not part_mass
        or not isinstance(assembly_mass, dict)
        or not isinstance(component_mass, dict)
        or not isinstance(component_mass.get("instances"), list)
        or not isinstance(link_group, dict)
        or not isinstance(link_group.get("links"), dict)
        or not link_group["links"]
        or not isinstance(scene_raw, Mapping)
        or set(scene_raw) != NATIVE_SCENE_RAW_KEYS
    ):
        raise PipelineError("Native Capture raw required inventories are incomplete")
    scene_document = str(scene_raw.get("document") or "").replace("\\", "/")
    scene_configuration = scene_raw.get("configuration")
    planned_main = str(plan["main_assembly"]).replace("\\", "/").casefold()
    if (
        not scene_document
        or not (
            scene_document.casefold() == planned_main
            or scene_document.casefold().endswith("/" + planned_main)
        )
        or scene_configuration != plan["configuration"]
    ):
        raise PipelineError(
            "Native Capture plan main assembly or configuration differs from raw facts"
        )
    scene_occurrences = scene_raw.get("occurrences")
    scene_components = scene_raw.get("components")
    if (
        not isinstance(scene_occurrences, list)
        or not scene_occurrences
        or not isinstance(scene_components, list)
        or not scene_components
    ):
        raise PipelineError("Native Capture scene inventory is incomplete")
    occurrence_by_name = {}
    for row in scene_occurrences:
        if not isinstance(row, dict):
            raise PipelineError("Native Capture scene occurrence is malformed")
        name = row.get("name")
        if not isinstance(name, str) or not name or name in occurrence_by_name:
            raise PipelineError("Native Capture scene occurrence identity is malformed")
        occurrence_by_name[name] = row
    component_names = {
        str(row.get("name"))
        for row in scene_components
        if isinstance(row, dict) and isinstance(row.get("name"), str)
    }
    if (
        len(component_names) != len(scene_components)
        or component_names != set(source_scopes)
        or set(part_mass) != component_names
    ):
        raise PipelineError("Native Capture leaf mass inventory differs from source scopes")
    objects = plan["objects"]
    object_names = {item["name"] for item in objects}
    if object_names != set(occurrence_by_name):
        raise PipelineError(
            "Native Capture plan object identities differ from raw occurrences"
        )
    for item in objects:
        raw = occurrence_by_name[item["name"]]
        if (
            type(raw.get("suppression_state")) is not int
            or raw["suppression_state"] != item["state"]
        ):
            raise PipelineError(
                f"Native Capture plan state differs from raw occurrence: {item['name']}"
            )
        document = item.get("document")
        if document is not None:
            raw_document = str(raw.get("path") or "").replace("\\", "/").casefold()
            planned_document = document.replace("\\", "/").casefold()
            if not (
                raw_document == planned_document
                or raw_document.endswith("/" + planned_document)
            ):
                raise PipelineError(
                    f"Native Capture plan document differs from raw occurrence: {item['name']}"
                )
    groups = plan["groups"]
    if {item["name"] for item in groups} != set(link_group["links"]):
        raise PipelineError(
            "Native Capture plan group identities differ from raw link groups"
        )
    seen_members = set()
    for group in groups:
        for name in group["components"]:
            if name not in source_scopes or name in seen_members:
                raise PipelineError(
                    "Native Capture plan groups are unbound or overlap"
                )
            seen_members.add(name)
    if seen_members != set(source_scopes):
        raise PipelineError(
            "Native Capture plan groups do not cover the raw source scopes"
        )
    provider_names = [
        row.get("name")
        for row in component_mass.get("instances", [])
        if isinstance(row, dict) and isinstance(row.get("effective"), dict)
    ] + [
        name
        for name, row in part_mass.items()
        if isinstance(row, dict) and row.get("mode") == "effective_instance"
    ]
    if any(
        not isinstance(name, str) or name not in occurrence_by_name
        for name in provider_names
    ):
        raise PipelineError("Native Capture effective provider is not a raw occurrence")
    selections = compose_link_group_selections(
        groups, source_scopes, provider_names
    )
    for group in groups:
        raw_group = link_group["links"].get(group["name"])
        selection = (
            raw_group.get("selection")
            if isinstance(raw_group, Mapping)
            else None
        )
        if (
            not isinstance(selection, list)
            or any(
                not isinstance(name, str) or name not in occurrence_by_name
                for name in selection
            )
            or selection != selections[group["name"]]["selection"]
        ):
            raise PipelineError(
                "Native Capture plan group differs from raw selection: "
                f"{group['name']}"
            )
    axis_rows = observation.get("axis_references_raw")
    if not isinstance(axis_rows, list):
        raise PipelineError("Native Capture raw axis inventory is malformed")
    raw_axes = {}
    for item in axis_rows:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("joint"), str)
            or not item["joint"]
            or item["joint"] in raw_axes
        ):
            raise PipelineError("Native Capture raw axis identity is malformed")
        raw_axes[item["joint"]] = item
    axes = {item["name"]: item["axis_reference"] for item in plan["axis_requests"]}
    if set(axes) != set(raw_axes):
        raise PipelineError(
            "Native Capture raw axis inventory differs from the plan"
        )
    for name, reference in axes.items():
        raw = raw_axes[name]
        if raw.get("selector") != reference:
            raise PipelineError(
                f"Native Capture plan axis selector differs from raw facts: {name}"
            )
        kind = "reference_axis" if reference.get("kind") == "reference_axis" else "cylinder"
        if raw.get("kind") != kind:
            raise PipelineError(
                f"Native Capture plan axis kind differs from raw facts: {name}"
            )
        component = reference["component"]
        if component and component not in object_names:
            raise PipelineError(
                f"Native Capture plan axis component is unbound: {name}"
            )
    coordinate_systems = (
        scene_raw.get("coordinate_systems")
        if isinstance(scene_raw, Mapping)
        else None
    )
    if (
        not isinstance(coordinate_systems, dict)
        or set(plan["datums"]) != set(coordinate_systems)
    ):
        raise PipelineError(
            "Native Capture raw datum inventory differs from the plan"
        )


def _raw_scalar(value):
    if isinstance(value, float) and not math.isfinite(value):
        return {
            "type": "float",
            "repr": repr(value),
            "nonfinite": True,
        }
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    return {
        "type": type(value).__name__,
        "repr": repr(value)[:300],
    }


def _transform_from_raw(values, *, field: str) -> Matrix:
    if not isinstance(values, (list, tuple)) or any(
        type(value) not in (int, float) for value in values
    ):
        raise CadError(
            "cad_transform_invalid",
            "expected a raw array of finite non-boolean numbers",
            {"value": _raw_scalar(values), "field": field},
        )
    try:
        return tuple(transform_from_solidworks(values))  # type: ignore[return-value]
    except (TypeError, ValueError) as error:
        message = str(error)
        if "scaled" in message:
            code = "cad_transform_scaled"
        elif "reflection" in message:
            code = "cad_transform_reflection"
        else:
            code = "cad_transform_invalid"
        raise CadError(code, message, {"values": copy.deepcopy(values), "field": field}) from error


def _compose_frames(left, right):
    """Column-vector 4x4 product of two row-major frames."""

    return [
        [
            sum(left[row][index] * right[index][column] for index in range(4))
            for column in range(4)
        ]
        for row in range(4)
    ]


def _inertia_from_raw(values, component):
    """Parse the documented nine-value full tensor without inferring geometry."""

    if not isinstance(values, (list, tuple)) or any(
        type(value) not in (int, float) for value in values
    ):
        raise CadError(
            "cad_mass_property_inertia_type",
            component,
            {"value_type": type(values).__name__},
        )
    data = [float(value) for value in values]
    if not all(math.isfinite(value) for value in data):
        raise CadError("cad_mass_property_inertia_nonfinite", component)
    if len(data) == 3:
        raise CadError("cad_mass_property_principal_only", component)
    if len(data) == 9:
        scale = max(max(map(abs, data)), 1e-30)
        if any(
            abs(data[left] - data[right]) > scale * 1e-10
            for left, right in ((1, 3), (2, 6), (5, 7))
        ):
            raise CadError("cad_mass_property_inertia_asymmetric", component)
        return tuple(tuple(data[index : index + 3]) for index in (0, 3, 6)), "full9"
    raise CadError(
        "cad_mass_property_inertia_unsupported",
        component,
        {"length": len(data)},
    )


def _project_real(value, *, component, field):
    if type(value) not in (int, float) or not math.isfinite(float(value)):
        raise CadError(
            f"cad_mass_property_{field}_invalid",
            f"{field} must be a finite non-boolean number",
            {"component": component, "value": _raw_scalar(value)},
        )
    return float(value)


def _project_mass_reading(raw):
    if not isinstance(raw, dict):
        raise CadError("cad_mass_projection_invalid", "a raw mass reading is not an object")
    read_errors = raw.get("read_errors")
    if not isinstance(read_errors, dict):
        raise CadError("cad_mass_projection_invalid", "a raw mass reading has no read-error map")
    for field, error in read_errors.items():
        if not isinstance(error, dict):
            raise CadError(
                "cad_mass_projection_invalid",
                "a raw mass read error is malformed",
                {"field": field},
            )
        if field in {"volume_m3", "density_kg_m3"}:
            continue
        if field == "material" and error.get("code") == "cad_material_provenance_missing":
            continue
        raise CadError(
            str(error.get("code") or "cad_mass_read_failed"),
            str(error.get("message") or f"native {field} read failed"),
            copy.deepcopy(error.get("detail")),
        )
    native_raw = raw.get("native_raw")
    if not isinstance(native_raw, dict):
        raise CadError(
            "cad_mass_projection_invalid",
            "a raw mass reading has no native getter values",
        )
    overrides = native_raw.get("overrides")
    if (
        not isinstance(overrides, dict)
        or set(overrides) != {
            "OverrideMass",
            "OverrideCenterOfMass",
            "OverrideMomentsOfInertia",
        }
        or any(type(value) is not bool for value in overrides.values())
    ):
        raise CadError(
            "cad_mass_override_invalid",
            "native mass override flags are malformed",
            overrides,
        )
    if raw.get("mode") != "effective_instance" and any(overrides.values()):
        raise CadError("cad_mass_override", "pure-CAD export refuses overrides", overrides)
    if (
        raw.get("mode") == "effective_instance"
        and raw.get("state") == "sheet_only"
        and not any(value is True for value in overrides.values())
    ):
        raise CadError(
            "cad_effective_instance_flags_invalid",
            "a sheet-only effective instance needs an active override",
            {"overrides": overrides},
        )
    reference_raw = raw.get("reference")
    reference = copy.deepcopy(reference_raw) if isinstance(reference_raw, dict) else {}
    component = str(
        reference.get("part_document")
        or reference.get("document")
        or "mass"
    )
    mass = _project_real(native_raw.get("mass_kg"), component=component, field="mass")
    com_raw = native_raw.get("center_of_mass_m")
    if not isinstance(com_raw, list) or len(com_raw) != 3:
        raise CadError(
            "cad_mass_property_center_invalid",
            "center_of_mass_m must be a three-value array",
            {"component": component, "value": _raw_scalar(com_raw)},
        )
    com = [
        _project_real(value, component=component, field="center")
        for value in com_raw
    ]
    if mass <= 0.0:
        raise CadError(
            "cad_mass_projection_invalid",
            "a raw mass or center is invalid",
        )
    inertia, _axis = _inertia_from_raw(native_raw.get("inertia_values"), component)
    for key in ("volume_m3", "density_kg_m3"):
        value = native_raw.get(key)
        reference[key] = (
            _project_real(value, component=component, field=key)
            if value is not None
            else None
        )
    projected = {
        "mass": mass,
        "com": com,
        "inertia": inertia,
        "reference": reference,
    }
    for key in ("mode", "role", "state", "measurement_basis", "document_type"):
        if raw.get(key) is not None:
            projected[key] = raw[key]
    return projected


def _project_effective_reading(raw):
    projected = copy.deepcopy(raw)
    native_raw = projected.pop("native_raw", None)
    part_measurement_raw = projected.pop("part_measurement_raw", None)
    if not isinstance(native_raw, dict):
        return projected
    read_errors = projected.get("read_errors")
    if not isinstance(read_errors, dict):
        raise CadError(
            "cad_mass_projection_invalid",
            "an effective-instance reading has no error map",
        )
    override_error = read_errors.get("overrides")
    if isinstance(override_error, dict):
        raise CadError(
            str(override_error.get("code") or "cad_mass_override_unreadable"),
            str(
                override_error.get("message")
                or "native mass override flags are unreadable"
            ),
            copy.deepcopy(override_error.get("detail")),
        )
    projected.pop("read_errors", None)
    overrides = native_raw.get("overrides")
    if (
        not isinstance(overrides, dict)
        or set(overrides) != {
            "OverrideMass",
            "OverrideCenterOfMass",
            "OverrideMomentsOfInertia",
        }
        or any(type(value) is not bool for value in overrides.values())
    ):
        raise CadError(
            "cad_mass_override_invalid",
            "native mass override flags are malformed",
            overrides,
        )
    reference = projected.get("reference")
    if (
        isinstance(reference, dict)
        and reference.get("measurement_basis")
        == "cad_authored_effective_instance"
        and not any(overrides.values())
    ):
        raise CadError(
            "cad_effective_instance_flags_invalid",
            "an authored effective instance needs an active override",
            {"overrides": overrides},
        )
    selected_components = (
        reference.get("selected_components")
        if isinstance(reference, dict)
        else None
    )
    component = str(
        selected_components[0]
        if isinstance(selected_components, list) and selected_components
        else "effective_instance"
    )
    mass = _project_real(
        native_raw.get("mass_kg"),
        component=component,
        field="mass",
    )
    com_raw = native_raw.get("center_of_mass_m")
    if not isinstance(com_raw, list) or len(com_raw) != 3:
        raise CadError(
            "cad_mass_property_center_invalid",
            "center_of_mass_m must be a three-value array",
            {"component": component, "value": _raw_scalar(com_raw)},
        )
    com = [
        _project_real(value, component=component, field="center")
        for value in com_raw
    ]
    if mass <= 0.0:
        raise CadError(
            "cad_mass_projection_invalid",
            "an effective-instance mass or center is invalid",
        )
    inertia, _axis = _inertia_from_raw(native_raw.get("inertia_values"), component)
    projected["mass"] = mass
    projected["com"] = com
    projected["inertia"] = [list(row) for row in inertia]
    if isinstance(part_measurement_raw, dict):
        part = _project_mass_reading(part_measurement_raw)
        part_reference = part.get("reference") or {}
        projected["part_measurement"] = {
            "mass_kg": part["mass"],
            "center_of_mass_m": list(part["com"]),
            "inertia_kg_m2": [list(row) for row in part["inertia"]],
            "scope": part_reference.get("scope"),
            "used_api": part_reference.get("used_api"),
            "product_convention": part_reference.get("product_convention"),
            **{
                key: part_reference[key]
                for key in ("volume_m3", "density_kg_m3")
                if key in part_reference
            },
        }
    return projected


def _project_component_mass(raw):
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise CadError(
            "cad_component_mass_projection_invalid",
            "raw component mass facts are not an object",
        )
    projected = copy.deepcopy(raw)
    for row in projected.get("instances") or []:
        if not isinstance(row, dict):
            raise CadError(
                "cad_component_mass_projection_invalid",
                "a raw component mass row is malformed",
            )
        name = row.get("name")
        if not isinstance(name, str) or not name:
            raise CadError(
                "cad_component_mass_projection_invalid",
                "a raw component mass row has no identity",
            )
        row["context_mass_kg"] = _project_real(
            row.get("context_mass_kg"),
            component=name,
            field="mass",
        )
        volume = row.get("context_volume_m3")
        if volume is not None:
            row["context_volume_m3"] = _project_real(
                volume,
                component=name,
                field="volume",
            )
        overrides = row.get("overrides")
        if (
            not isinstance(overrides, dict)
            or set(overrides) != {
                "OverrideMass",
                "OverrideCenterOfMass",
                "OverrideMomentsOfInertia",
            }
            or any(type(value) is not bool for value in overrides.values())
        ):
            raise CadError(
                "cad_component_mass_projection_invalid",
                "a raw component override row is malformed",
                {"component": name},
            )
        if isinstance(row.get("effective"), dict):
            row["effective"] = _project_effective_reading(row["effective"])
    return projected


def _project_link_group_mass(raw):
    if not isinstance(raw, dict) or not isinstance(raw.get("links"), dict):
        raise CadError(
            "cad_link_group_projection_invalid",
            "raw link-group mass facts are missing",
        )
    links = {}
    essential_errors = (
        "mass_kg",
        "center_of_mass_m",
        "inertia_values",
        "overrides",
    )
    for body_name, body in raw["links"].items():
        if (
            not isinstance(body, dict)
            or not isinstance(body.get("reads"), list)
            or len(body["reads"]) != 2
        ):
            raise CadError(
                "cad_link_group_projection_invalid",
                "a raw link-group reading does not contain two reads",
                {"body": body_name},
            )
        reads = []
        for read_index, read in enumerate(body["reads"]):
            if not isinstance(read, dict):
                raise CadError(
                    "cad_link_group_projection_invalid",
                    "a raw link-group read is not an object",
                )
            read_errors = read.get("read_errors")
            if not isinstance(read_errors, dict):
                raise CadError(
                    "cad_link_group_projection_invalid",
                    "a raw link-group read has no error map",
                )
            for field in essential_errors:
                native_error = read_errors.get(field)
                if native_error is None:
                    continue
                if not isinstance(native_error, dict):
                    raise CadError(
                        "cad_link_group_projection_invalid",
                        "a raw link-group field error is malformed",
                        {
                            "body": body_name,
                            "read": read_index,
                            "field": field,
                        },
                    )
                native_detail = native_error.get("detail")
                location = {
                    "body": body_name,
                    "read": read_index,
                    "field": field,
                }
                detail = (
                    {**copy.deepcopy(native_detail), **location}
                    if isinstance(native_detail, dict)
                    else {"native_detail": copy.deepcopy(native_detail), **location}
                )
                raise CadError(
                    str(native_error.get("code") or "cad_mass_read_failed"),
                    str(native_error.get("message") or f"native {field} read failed"),
                    detail,
                )
            overrides = read.get("overrides")
            if (
                not isinstance(overrides, dict)
                or set(overrides) != {
                    "OverrideMass",
                    "OverrideCenterOfMass",
                    "OverrideMomentsOfInertia",
                }
                or any(type(value) is not bool for value in overrides.values())
            ):
                raise CadError(
                    "cad_mass_override_invalid",
                    "native mass override flags are malformed",
                    overrides,
                )
            mass = _project_real(
                read.get("mass_kg"),
                component=str(body_name),
                field="mass",
            )
            com_raw = read.get("com_m")
            if not isinstance(com_raw, list) or len(com_raw) != 3:
                raise CadError(
                    "cad_mass_property_center_invalid",
                    "a raw link-group COM must be a three-value array",
                    {"body": body_name, "value": _raw_scalar(com_raw)},
                )
            com = [
                _project_real(value, component=str(body_name), field="center")
                for value in com_raw
            ]
            if mass <= 0.0:
                raise CadError(
                    "cad_link_group_projection_invalid",
                    "a raw link-group mass or center is invalid",
                )
            inertia, _axis = _inertia_from_raw(
                read.get("inertia_values_raw"),
                str(body_name),
            )
            origin = copy.deepcopy(
                read.get("inertia_values_about_origin_api")
            )
            origin_error = read_errors.get(
                "inertia_values_about_origin_api"
            )
            if origin is not None:
                if isinstance(origin, dict):
                    if (
                        not isinstance(origin.get("code"), str)
                        or not origin["code"].strip()
                    ):
                        raise CadError(
                            "cad_link_group_projection_invalid",
                            "a raw link-group origin error is malformed",
                            {"body": body_name, "read": read_index},
                        )
                    if origin_error != origin:
                        raise CadError(
                            "cad_link_group_projection_invalid",
                            "a raw link-group origin error differs from its read-error entry",
                            {"body": body_name, "read": read_index},
                        )
                elif (
                    not isinstance(origin, list)
                    or len(origin) != 9
                    or any(
                        type(value) not in (int, float)
                        or not math.isfinite(float(value))
                        for value in origin
                    )
                ):
                    raise CadError(
                        "cad_link_group_projection_invalid",
                        "a raw link-group origin tensor is invalid",
                        {"body": body_name, "read": read_index},
                    )
                if origin_error is not None and not isinstance(origin, dict):
                    raise CadError(
                        "cad_link_group_projection_invalid",
                        "a raw link-group origin error conflicts with a recorded tensor",
                        {"body": body_name, "read": read_index},
                    )
            elif origin_error is not None:
                raise CadError(
                    "cad_link_group_projection_invalid",
                    "a raw link-group origin error has no matching origin value",
                    {"body": body_name, "read": read_index},
                )
            projected_read = {
                "mass_kg": mass,
                "com_m": com,
                "inertia_tensor_about_com": [list(row) for row in inertia],
                "overrides": dict(overrides),
                "inertia_values_about_origin_api": origin,
            }
            for key in ("volume_m3", "density_kg_m3"):
                if key in read:
                    projected_read[key] = read[key]
            reads.append(projected_read)
        tensor_delta = max(
            abs(
                reads[0]["inertia_tensor_about_com"][row][column]
                - reads[1]["inertia_tensor_about_com"][row][column]
            )
            for row in range(3)
            for column in range(3)
        )
        links[body_name] = {
            "selection": [str(item) for item in body.get("selection") or []],
            "reads": reads,
            "reproducibility": {
                "mass_delta_kg": abs(reads[0]["mass_kg"] - reads[1]["mass_kg"]),
                "com_delta_norm_m": math.dist(reads[0]["com_m"], reads[1]["com_m"]),
                "tensor_delta_max_abs": tensor_delta,
            },
        }
    return {
        "assembly": str(raw.get("assembly") or ""),
        "configuration": str(raw.get("configuration") or ""),
        "used_api": str(raw.get("used_api") or ""),
        "selection_rule": str(raw.get("selection_rule") or ""),
        "links": links,
    }


def _project_scene(raw_scene):
    if not isinstance(raw_scene, dict):
        raise CadError("cad_prepared_scene_facts_invalid", "scene_raw is not an object")
    projected_by_name = {}
    occurrences = raw_scene.get("occurrences")
    components = raw_scene.get("components")
    if not isinstance(occurrences, list) or not isinstance(components, list):
        raise CadError(
            "cad_prepared_scene_facts_invalid",
            "scene_raw occurrences are missing",
        )
    for occurrence in occurrences:
        if not isinstance(occurrence, dict) or not isinstance(
            occurrence.get("name"),
            str,
        ) or not occurrence["name"]:
            raise CadError(
                "cad_prepared_scene_facts_invalid",
                "a raw occurrence has no identity",
            )
        if occurrence["name"] in projected_by_name:
            raise CadError(
                "cad_prepared_scene_facts_invalid",
                "raw occurrences repeat an identity",
                {"component": occurrence["name"]},
            )
        is_fixed = occurrence.get("is_fixed")
        if type(is_fixed) is not bool:
            raise CadError(
                "cad_component_fixed_invalid",
                "a raw occurrence has no native boolean IsFixed value",
                {
                    "component": occurrence["name"],
                    "value": repr(is_fixed),
                    "error": copy.deepcopy(occurrence.get("is_fixed_error")),
                },
            )
        if occurrence.get("is_fixed_error") is not None:
            raise CadError(
                "cad_component_fixed_invalid",
                "a raw occurrence has both a fixed state and a fixed error",
                {
                    "component": occurrence["name"],
                    "error": copy.deepcopy(occurrence.get("is_fixed_error")),
                },
            )
        projected_by_name[occurrence["name"]] = _transform_from_raw(
            occurrence.get("transform_raw"),
            field=f"occurrences[{occurrence['name']}].transform_raw",
        )
    projected_components = []
    component_names = set()
    for occurrence in components:
        name = occurrence.get("name") if isinstance(occurrence, dict) else None
        if (
            not isinstance(name, str)
            or not name
            or name in component_names
            or name not in projected_by_name
        ):
            raise CadError(
                "cad_prepared_scene_facts_invalid",
                "a component has no unique raw occurrence identity",
            )
        component_names.add(name)
        is_fixed = occurrence.get("is_fixed")
        if type(is_fixed) is not bool:
            raise CadError(
                "cad_component_fixed_invalid",
                "a raw component has no native boolean IsFixed value",
                {
                    "component": name,
                    "value": repr(is_fixed),
                    "error": copy.deepcopy(occurrence.get("is_fixed_error")),
                },
            )
        if occurrence.get("is_fixed_error") is not None:
            raise CadError(
                "cad_component_fixed_invalid",
                "a raw component has both a fixed state and a fixed error",
                {
                    "component": name,
                    "error": copy.deepcopy(occurrence.get("is_fixed_error")),
                },
            )
        projected_components.append(
            RawComponent(
                name=name,
                path=str(occurrence.get("path") or ""),
                transform=projected_by_name[name],
                is_fixed=is_fixed,
                document_type=str(occurrence.get("document_type") or ""),
            )
        )
    coordinate_systems = {}
    raw_datums = raw_scene.get("coordinate_systems")
    if not isinstance(raw_datums, dict):
        raise CadError(
            "cad_prepared_scene_facts_invalid",
            "scene_raw coordinate systems are missing",
        )
    for key, datum in raw_datums.items():
        if not isinstance(datum, dict):
            raise CadError(
                "cad_prepared_scene_facts_invalid",
                "a raw coordinate system is malformed",
            )
        datum_errors = datum.get("read_errors")
        if type(datum_errors) is not dict:
            raise CadError(
                "cad_prepared_scene_facts_invalid",
                "a raw coordinate system has no error map",
            )
        if datum_errors:
            first_error = next(iter(datum_errors.values()), {})
            if not isinstance(first_error, dict):
                first_error = {}
            raise CadError(
                str(first_error.get("code") or "cad_coordinate_system_invalid"),
                str(
                    first_error.get("message")
                    or "raw coordinate-system data is invalid"
                ),
                {
                    "coordinate_system": key,
                    "array_data": copy.deepcopy(datum.get("array_data")),
                    "error": copy.deepcopy(first_error),
                },
            )
        local = _transform_from_raw(
            datum.get("array_data"),
            field=f"coordinate_systems[{key}].array_data",
        )
        owner = datum.get("owner")
        if type(owner) is not str:
            raise CadError(
                "cad_prepared_scene_facts_invalid",
                "a raw coordinate system has no string owner",
                {
                    "coordinate_system": key,
                    "owner": copy.deepcopy(owner),
                },
            )
        if owner:
            placement_raw = datum.get("occurrence_transform_raw")
            if not isinstance(placement_raw, list) or len(placement_raw) != 16:
                raise CadError(
                    "cad_coordinate_system_owner_unbound",
                    "a component coordinate system has no raw occurrence placement",
                    {"coordinate_system": key, "component": owner},
                )
            placement = _transform_from_raw(
                placement_raw,
                field=(
                    f"coordinate_systems[{key}]"
                    ".occurrence_transform_raw"
                ),
            )
            composed = _compose_frames(
                [
                    placement[offset : offset + 4]
                    for offset in range(0, 16, 4)
                ],
                [local[offset : offset + 4] for offset in range(0, 16, 4)],
            )
            local = tuple(value for row in composed for value in row)
        coordinate_systems[str(key)] = local
    return RawScene(
        document=str(raw_scene.get("document") or ""),
        components=projected_components,
        coordinate_systems=coordinate_systems,
        mass_properties={},
        notes=copy.deepcopy(raw_scene.get("notes") or {}),
    )


def project_capture_facts(raw_facts) -> dict:
    """Project the current seven-key raw Capture record into portable values."""

    if not isinstance(raw_facts, dict) or set(raw_facts) != NATIVE_CAPTURE_FACTS_KEYS:
        raise CadError(
            "cad_prepared_capture_facts",
            "raw Capture facts have an unknown shape",
            {
                "expected": sorted(NATIVE_CAPTURE_FACTS_KEYS),
                "received": sorted(raw_facts) if isinstance(raw_facts, dict) else None,
            },
        )
    scene = _project_scene(raw_facts["scene_raw"])
    part_mass_raw = raw_facts["part_mass_raw"]
    if not isinstance(part_mass_raw, dict):
        raise CadError(
            "cad_prepared_capture_facts",
            "part_mass_raw is not an object",
        )
    notes = raw_facts["notes"] if isinstance(raw_facts["notes"], dict) else {}
    for component in scene.components:
        if component.name in part_mass_raw:
            continue
        error = notes.get("mass_error:" + component.name)
        if isinstance(error, dict):
            raise CadError(
                str(error.get("code") or "cad_mass_property_unavailable"),
                str(error.get("message") or "part mass getter failed"),
                copy.deepcopy(error.get("detail")),
            )
        raise CadError(
            "cad_prepared_capture_facts",
            "part mass getter produced neither raw facts nor an error",
            {"component": component.name},
        )
    scene.mass_properties = {
        name: _project_mass_reading(row)
        for name, row in part_mass_raw.items()
    }
    scene.notes = copy.deepcopy(raw_facts["notes"])
    assembly_mass_raw = raw_facts["assembly_mass_raw"]
    if assembly_mass_raw is None:
        error = notes.get("assembly_mass_error")
        if isinstance(error, dict):
            raise CadError(
                str(error.get("code") or "cad_mass_property_unavailable"),
                str(error.get("message") or "assembly mass getter failed"),
                copy.deepcopy(error.get("detail")),
            )
        raise CadError(
            "cad_prepared_capture_facts",
            "assembly mass getter produced neither raw facts nor an error",
        )
    if not isinstance(assembly_mass_raw, dict):
        raise CadError(
            "cad_prepared_capture_facts",
            "assembly_mass_raw is not an object",
        )
    component_mass_raw = raw_facts["component_mass_raw"]
    if component_mass_raw is None:
        error = notes.get("component_mass_error")
        if isinstance(error, dict):
            raise CadError(
                str(error.get("code") or "cad_mass_property_unavailable"),
                str(error.get("message") or "component mass getter failed"),
                copy.deepcopy(error.get("detail")),
            )
        raise CadError(
            "cad_prepared_capture_facts",
            "component mass getter produced neither raw facts nor an error",
        )
    if not isinstance(component_mass_raw, dict):
        raise CadError(
            "cad_prepared_capture_facts",
            "component_mass_raw is not an object",
        )
    component_mass = copy.deepcopy(component_mass_raw)
    effective_instances = {}
    effective_error = None
    if isinstance(notes.get("component_mass_error"), dict):
        effective_error = copy.deepcopy(notes["component_mass_error"])
    if isinstance(component_mass, dict):
        for row in component_mass.get("instances") or []:
            if not isinstance(row, dict):
                continue
            name = str(row.get("name") or "")
            effective = row.get("effective")
            if name and isinstance(effective, dict):
                effective_instances[name] = _project_effective_reading(effective)
            read_errors = row.get("read_errors")
            if name and (row.get("effective_error") is not None or read_errors):
                first_error = (
                    next(iter(read_errors.values()), {})
                    if isinstance(read_errors, dict)
                    else {}
                )
                effective_error = effective_error or {
                    "reason": str(
                        row.get("effective_error")
                        or (
                            first_error.get("code")
                            if isinstance(first_error, dict)
                            else None
                        )
                        or "cad_component_mass_read_error"
                    ),
                    "message": (
                        f"effective instance {name} was not readable"
                    ),
                }
        if component_mass.get("errors"):
            effective_error = effective_error or {
                "reason": "cad_component_mass_context_partial",
                "message": (
                    "component mass context retained native read errors"
                ),
            }
    for name, row in part_mass_raw.items():
        if isinstance(row, dict) and row.get("mode") == "effective_instance":
            effective_instances[str(name)] = _project_effective_reading(row)
    return {
        "scene": scene,
        "assembly_mass": _project_mass_reading(assembly_mass_raw),
        "component_mass": _project_component_mass(component_mass_raw),
        "effective_instances": effective_instances,
        "effective_instances_error": effective_error,
        "link_group_mass_properties": _project_link_group_mass(
            raw_facts["link_group_mass_raw"]
        ),
    }


def _component_context_record(
    reading,
    assembly_mass,
):
    instances = []
    errors = list(reading.get("errors") or [])
    for entry in reading.get("instances") or []:
        name = str(entry.get("name") or "")
        try:
            if not name:
                raise ValueError("component instance row has no name")
            raw_depth = entry.get("depth")
            if type(raw_depth) is not int:
                raise ValueError(f"row for {name} has no integer depth")
            parent = name.rsplit("/", 1)[0] if "/" in name else None
            if raw_depth != name.count("/") or entry.get("parent") != parent:
                raise ValueError(f"row for {name} has inconsistent hierarchy")
            mass = float(entry["context_mass_kg"])
            if not math.isfinite(mass) or mass <= 0.0:
                raise ValueError("component mass is not finite and positive")
            volume = entry.get("context_volume_m3")
            volume = None if volume is None else float(volume)
            if volume is not None and not math.isfinite(volume):
                raise ValueError("component volume is not finite")
            raw_overrides = entry.get("overrides")
            if (
                not isinstance(raw_overrides, dict)
                or not {
                    "OverrideMass",
                    "OverrideCenterOfMass",
                    "OverrideMomentsOfInertia",
                }
                <= set(raw_overrides)
            ):
                raise ValueError("component row has no complete override flags")
            overrides = {
                str(key): value
                for key, value in raw_overrides.items()
            }
            if any(type(value) is not bool for value in overrides.values()):
                raise ValueError("component override flags must be booleans")
        except (KeyError, TypeError, ValueError) as error:
            errors.append(
                {
                    "name": name,
                    "error": "cad_component_context_invalid",
                    "message": str(error)[:200],
                }
            )
            continue
        instances.append(
            {
                "name": name,
                "parent": parent,
                "depth": raw_depth,
                "document": entry.get("document"),
                "document_type": str(entry.get("document_type") or ""),
                "context_mass_kg": mass,
                "context_volume_m3": volume,
                "overrides": overrides,
                "effective": entry.get("effective"),
                "effective_error": entry.get("effective_error"),
            }
        )
    return {
        "schema_version": "solidworks-to-urdf.component-mass-context/v1",
        "status": "recorded" if not errors else "partial",
        "assembly_mass_kg": (
            None if assembly_mass is None else float(assembly_mass)
        ),
        "instances": instances,
        "errors": errors,
        "reference": dict(reading.get("reference") or {}),
    }


def _mass_closure(assembly_mass, component_mass):
    if assembly_mass is None and component_mass is None:
        return None
    top_mass = None
    if isinstance(assembly_mass, dict):
        try:
            top_mass = float(assembly_mass["mass"])
        except (KeyError, TypeError, ValueError):
            assembly_mass = None
    elif assembly_mass is not None:
        assembly_mass = None
    context = (
        _component_context_record(component_mass, top_mass)
        if isinstance(component_mass, dict)
        else None
    )
    if assembly_mass is None or top_mass is None:
        record = {
            "schema_version": "solidworks-to-urdf.mass-closure/v1",
            "status": "unavailable",
            "reason": "cad_mass_property_unavailable",
            "message": "no usable assembly mass reading was projected",
        }
    else:
        record = {
            "schema_version": "solidworks-to-urdf.mass-closure/v1",
            "status": "recorded",
            "mode": "full",
            "top_level": {
                "mass": top_mass,
                "com": [float(value) for value in assembly_mass["com"]],
                "inertia": [
                    [float(value) for value in row]
                    for row in assembly_mass["inertia"]
                ],
                "reference": dict(assembly_mass.get("reference") or {}),
            },
        }
    if context is not None:
        record["component_context"] = context
    return record


def _material_provenance(scene, source):
    from .scene import material_unverified

    warnings = []
    for name, payload in sorted(scene.mass_properties.items()):
        if not material_unverified(payload):
            continue
        reference = payload["reference"]
        assignment = reference["material_assignment"]
        warnings.append(
            {
                "code": str(assignment["unverified_reason"]),
                "message": str(
                    assignment.get("message")
                    or "material assignment is not verified"
                ),
                "blocking": False,
                "component": str(name),
                "document": reference.get("part_document"),
                "configuration": (
                    reference.get("configuration")
                    or assignment.get("configuration")
                ),
                "missing_body_indices": assignment.get(
                    "missing_body_indices"
                ),
                "material_assignment": assignment,
                "measured": {
                    "source": "cad_api",
                    "scope": reference.get("scope"),
                    "mass_kg": payload.get("mass"),
                    "center_of_mass_m": payload.get("com"),
                    "inertia_kg_m2": payload.get("inertia"),
                    "volume_m3": reference.get("volume_m3"),
                    "density_kg_m3": reference.get("density_kg_m3"),
                },
            }
        )
    return {
        "schema_version": "solidworks-to-urdf.material-provenance/v1",
        "assembly": str(source.get("assembly") or ""),
        "configuration": str(source.get("configuration") or ""),
        "measured_source": "cad_api",
        "warnings": warnings,
    }


def _effective_instance_record(scene, source, readings, cache_error):
    readings_payload = {
        str(name): dict(reading)
        for name, reading in sorted((readings or {}).items())
        if isinstance(reading, dict)
    }
    warnings = []
    for name, reading in sorted(readings_payload.items()):
        reference = dict(reading.get("reference") or {})
        flags = dict(reference.get("overrides") or {})
        instance_difference = (
            reference.get("measurement_basis")
            == "cad_assembly_instance_measurement"
        )
        if not any(bool(value) for value in flags.values()) and not instance_difference:
            continue
        warnings.append(
            {
                "code": (
                    "cad_instance_measurement_differs_from_part"
                    if instance_difference
                    else "cad_instance_override_effective"
                ),
                "blocking": False,
                "message": (
                    "the native assembly-instance measurement differs from the part-document measurement; "
                    "the cause is unproven and both are retained"
                    if instance_difference
                    else "the instance carries CAD-authored mass-property overrides; these are effective CAD values, "
                    "not geometry qualification"
                ),
                "component": name,
                "role": reading.get("role"),
                "document_type": reading.get("document_type"),
                "configuration": reference.get("configuration"),
                "part_document": reference.get("part_document"),
                "assembly_document": reference.get("assembly_document"),
                "assembly_configuration": reference.get(
                    "assembly_configuration"
                ),
                "selected_components": reference.get(
                    "selected_components"
                ),
                "overrides": flags,
                "qualification": (
                    "cad_assembly_instance_measurement; cause_of_part_difference_unproven; "
                    "not geometry-derived, not independently physically qualified"
                    if instance_difference
                    else "cad_authored_effective_instance; not geometry-derived, not independently physically qualified"
                ),
                **(
                    {"part_measurement": reading.get("part_measurement")}
                    if instance_difference
                    else {}
                ),
                "measurement": {
                    "scope": reference.get("scope"),
                    "axes": reference.get("axes"),
                    "used_api": reference.get("used_api"),
                    "selection_api": reference.get("selection_api"),
                    "override_api": reference.get("override_api"),
                    "mass_kg": reading.get("mass"),
                    "center_of_mass_m": reading.get("com"),
                    "inertia_kg_m2": reading.get("inertia"),
                    "volume_m3": reference.get("volume_m3"),
                    "density_kg_m3": reference.get("density_kg_m3"),
                },
            }
        )
    for name, payload in sorted(scene.mass_properties.items()):
        if not isinstance(payload, dict) or payload.get("mode") != "effective_instance":
            continue
        reference = dict(payload.get("reference") or {})
        warnings.append(
            {
                "code": "cad_surface_only_effective_instance",
                "blocking": False,
                "message": (
                    "the part is surface-only (no solid bodies); its mass properties come from the assembly "
                    "instance measurement, not from geometric qualification"
                ),
                "component": str(name),
                "document": reference.get("part_document"),
                "configuration": reference.get("configuration"),
                "assembly_document": reference.get("assembly_document"),
                "assembly_configuration": reference.get(
                    "assembly_configuration"
                ),
                "selected_components": reference.get(
                    "selected_components"
                ),
                "part_document_read": "unavailable",
                "fallback_reason": reference.get("fallback_reason"),
                "fallback_error_code": reference.get("fallback_error_code"),
                "solid_bodies": reference.get("solid_bodies"),
                "sheet_bodies": reference.get("sheet_bodies"),
                "overrides": dict(reference.get("overrides") or {}),
                "qualification": (
                    "cad_authored_effective_instance; not geometry-derived, not independently physically qualified"
                ),
                "measurement": {
                    "state": payload.get("state"),
                    "measurement_basis": payload.get("measurement_basis"),
                    "scope": reference.get("scope"),
                    "axes": reference.get("axes"),
                    "used_api": reference.get("used_api"),
                    "selection_api": reference.get("selection_api"),
                    "override_api": reference.get("override_api"),
                    "mass_kg": payload.get("mass"),
                    "center_of_mass_m": payload.get("com"),
                    "inertia_kg_m2": payload.get("inertia"),
                    "volume_m3": reference.get("volume_m3"),
                    "density_kg_m3": reference.get("density_kg_m3"),
                },
            }
        )
    return {
        "schema_version": EFFECTIVE_INSTANCES_SCHEMA,
        "assembly": str(source.get("assembly") or ""),
        "configuration": str(source.get("configuration") or ""),
        "cache_error": cache_error,
        "readings": readings_payload,
        "warnings": warnings,
    }


def _link_group_selections(source, scene, effective):
    leaves = [str(component.name) for component in scene.components]
    provider_names = {str(name) for name in effective}
    provider_names |= {
        str(name)
        for name, row in scene.mass_properties.items()
        if isinstance(row, dict) and row.get("mode") == "effective_instance"
    }
    return compose_link_group_selections(
        source.get("groups"),
        leaves,
        provider_names,
    )


def _valid_link_group_read(body, index, read):
    def invalid(field):
        raise CadError(
            "cad_link_group_payload_invalid",
            "a link group read is malformed",
            {"body": body, "read": index, "field": field},
        )

    if not isinstance(read, dict):
        invalid("read")
    mass = read.get("mass_kg")
    if (
        type(mass) not in (int, float)
        or not math.isfinite(float(mass))
        or float(mass) <= 0.0
    ):
        invalid("mass_kg")
    com = read.get("com_m")
    if (
        not isinstance(com, list)
        or len(com) != 3
        or any(
            type(value) not in (int, float)
            or not math.isfinite(float(value))
            for value in com
        )
    ):
        invalid("com_m")
    tensor = read.get("inertia_tensor_about_com")
    if (
        not isinstance(tensor, list)
        or len(tensor) != 3
        or any(
            not isinstance(row, list)
            or len(row) != 3
            or any(
                type(value) not in (int, float)
                or not math.isfinite(float(value))
                for value in row
            )
            for row in tensor
        )
    ):
        invalid("inertia_tensor_about_com")
    overrides = read.get("overrides")
    if (
        not isinstance(overrides, dict)
        or set(overrides) != {
            "OverrideMass",
            "OverrideCenterOfMass",
            "OverrideMomentsOfInertia",
        }
        or any(type(value) is not bool for value in overrides.values())
    ):
        invalid("overrides")


def _link_group_record(
    payload,
    selections,
    *,
    document,
    configuration,
):
    if (
        not isinstance(payload, dict)
        or payload.get("used_api") != LINK_GROUP_USED_API
        or payload.get("selection_rule") != LINK_GROUP_SELECTION_RULE
    ):
        raise CadError(
            "cad_link_group_payload_invalid",
            "the backend returned an unexpected link group payload",
        )
    if payload.get("configuration") != configuration:
        raise CadError(
            "cad_link_group_payload_invalid",
            "the link group payload names another configuration",
            {
                "expected": configuration,
                "received": payload.get("configuration"),
            },
        )
    links = payload.get("links")
    if not isinstance(links, dict) or set(links) != set(selections):
        raise CadError(
            "cad_link_group_payload_invalid",
            "the link group payload does not cover the composed bodies",
            {
                "expected": sorted(selections),
                "received": sorted(links or []),
            },
        )
    for name, entry in selections.items():
        row = links[name]
        reads = row.get("reads") if isinstance(row, dict) else None
        if (
            not isinstance(row, dict)
            or row.get("selection") != entry["selection"]
        ):
            raise CadError(
                "cad_link_group_payload_invalid",
                "the link group payload echoes another selection",
                {"body": name},
            )
        if not isinstance(reads, list) or len(reads) != 2:
            raise CadError(
                "cad_link_group_payload_invalid",
                "the link group payload must carry two reads",
                {"body": name},
            )
        for index, read in enumerate(reads):
            _valid_link_group_read(name, index, read)
    if document and not _same_document(
        str(payload.get("assembly") or ""),
        document,
    ):
        raise CadError(
            "capture_source_mismatch",
            "the link group read names another document",
            {
                "requested": document,
                "reported": payload.get("assembly"),
            },
        )
    return {
        "schema_version": LINK_GROUP_MASS_SCHEMA,
        "assembly": str(payload.get("assembly") or ""),
        "configuration": str(payload.get("configuration") or ""),
        "used_api": LINK_GROUP_USED_API,
        "selection_rule": LINK_GROUP_SELECTION_RULE,
        "links": {
            name: {
                "selection": list(links[name]["selection"]),
                "reads": copy.deepcopy(links[name]["reads"]),
                "reproducibility": copy.deepcopy(
                    links[name]["reproducibility"]
                ),
            }
            for name in selections
        },
        "readout_window": "single-read-only-pass",
    }


def _same_document(left: str, right: str) -> bool:
    if not left or not right:
        return False
    return left.replace("/", "\\").casefold() == right.replace(
        "/",
        "\\",
    ).casefold()


def project_native_capture_observation(
    observation: Mapping,
    source: Mapping,
) -> dict:
    """Project the v2 raw observation while keeping the existing value shape."""

    if (
        not isinstance(observation, Mapping)
        or set(observation) != NATIVE_CAPTURE_OBSERVATION_KEYS
        or observation.get("schema_version") != NATIVE_CAPTURE_OBSERVATION_SCHEMA
    ):
        raise PipelineError("Native Capture observation is incomplete or unknown")
    raw_facts = {
        key: observation[key]
        for key in NATIVE_CAPTURE_FACTS_KEYS
    }
    projected = project_capture_facts(raw_facts)
    scene = projected["scene"]
    raw_scene = observation.get("scene_raw")
    document = (
        str(raw_scene.get("document") or "")
        if isinstance(raw_scene, Mapping)
        else ""
    )
    projected["link_group_mass_properties"] = _link_group_record(
        projected["link_group_mass_properties"],
        _link_group_selections(
            source,
            scene,
            projected["effective_instances"],
        ),
        document=document,
        configuration=str(source.get("configuration") or ""),
    )
    projected["mass_closure"] = _mass_closure(
        projected["assembly_mass"],
        projected["component_mass"],
    )
    projected["material_provenance"] = _material_provenance(
        scene,
        source,
    )
    projected["effective_instances_record"] = _effective_instance_record(
        scene,
        source,
        projected["effective_instances"],
        projected["effective_instances_error"],
    )
    projected["axis_references"] = project_axis_references(
        observation["axis_references_raw"]
    )
    normalize_capture_projection(projected)
    return projected


def _negated_matrix(value):
    if (
        not isinstance(value, (list, tuple))
        or len(value) != 3
        or any(
            not isinstance(row, (list, tuple)) or len(row) != 3
            for row in value
        )
    ):
        return value
    matrix = [list(row) for row in value]
    for row, column in ((0, 1), (0, 2), (1, 2)):
        matrix[row][column] = -matrix[row][column]
        matrix[column][row] = -matrix[column][row]
    return matrix


def _negated_flat(value):
    if not isinstance(value, (list, tuple)) or len(value) != 9:
        return value
    result = [float(item) for item in value]
    for index in (1, 2, 3, 5, 6, 7):
        result[index] = -result[index]
    return result


def _negate_reading(reading) -> None:
    if not isinstance(reading, dict):
        return
    reading["inertia"] = _negated_matrix(reading.get("inertia"))
    measurement = reading.get("part_measurement")
    if isinstance(measurement, dict):
        measurement["inertia_kg_m2"] = _negated_matrix(
            measurement.get("inertia_kg_m2")
        )


def normalize_capture_projection(projected: dict) -> dict:
    """Normalize product-of-inertia signs in one in-memory projection.

    The raw record remains byte-identical.  The returned decision is observation
    data for the current producer/verifier and is not a second raw authority.
    """

    if not isinstance(projected, dict):
        raise CadError(
            "cad_prepared_capture_facts",
            "projected Capture facts are not an object",
        )
    scene = projected.get("scene")
    link_groups = projected.get("link_group_mass_properties")
    if (
        not isinstance(scene, RawScene)
        or not isinstance(link_groups, dict)
    ):
        raise CadError(
            "cad_prepared_capture_facts",
            "projected Capture facts are incomplete",
        )
    decision = detect_family(link_groups)
    applied = decision["family"] == POSITIVE
    if applied:
        for reading in scene.mass_properties.values():
            if isinstance(reading, dict):
                reading["inertia"] = _negated_matrix(reading.get("inertia"))
        notes_effective = scene.notes.get("effective_instances")
        if isinstance(notes_effective, dict):
            for reading in notes_effective.values():
                _negate_reading(reading)
        assembly_mass = projected.get("assembly_mass")
        if isinstance(assembly_mass, dict):
            assembly_mass["inertia"] = _negated_matrix(
                assembly_mass.get("inertia")
            )
        component_mass = projected.get("component_mass")
        if isinstance(component_mass, dict):
            for row in component_mass.get("instances") or []:
                if isinstance(row, dict) and isinstance(
                    row.get("effective"),
                    dict,
                ):
                    _negate_reading(row["effective"])
        effective_instances = projected.get("effective_instances")
        if isinstance(effective_instances, dict):
            for reading in effective_instances.values():
                _negate_reading(reading)
        effective_record = projected.get("effective_instances_record")
        if isinstance(effective_record, dict):
            readings = effective_record.get("readings")
            if isinstance(readings, dict):
                for reading in readings.values():
                    _negate_reading(reading)
            warnings = effective_record.get("warnings")
            if isinstance(warnings, list):
                for warning in warnings:
                    if not isinstance(warning, dict):
                        continue
                    measurement = warning.get("measurement")
                    if isinstance(measurement, dict):
                        measurement["inertia_kg_m2"] = _negated_matrix(
                            measurement.get("inertia_kg_m2")
                        )
                    part_measurement = warning.get("part_measurement")
                    if isinstance(part_measurement, dict):
                        part_measurement["inertia_kg_m2"] = _negated_matrix(
                            part_measurement.get("inertia_kg_m2")
                        )
        closure = projected.get("mass_closure")
        if isinstance(closure, dict):
            top = closure.get("top_level")
            if isinstance(top, dict):
                top["inertia"] = _negated_matrix(top.get("inertia"))
            context = closure.get("component_context")
            if isinstance(context, dict):
                for row in context.get("instances") or []:
                    if isinstance(row, dict) and isinstance(
                        row.get("effective"),
                        dict,
                    ):
                        _negate_reading(row["effective"])
        material = projected.get("material_provenance")
        if isinstance(material, dict):
            for warning in material.get("warnings") or []:
                if not isinstance(warning, dict):
                    continue
                measured = warning.get("measured")
                if isinstance(measured, dict):
                    measured["inertia_kg_m2"] = _negated_matrix(
                        measured.get("inertia_kg_m2")
                    )
        links = link_groups.get("links")
        if not isinstance(links, dict):
            raise CadError(
                "cad_link_group_projection_invalid",
                "projected link-group links are missing",
            )
        for entry in links.values():
            reads = entry.get("reads") if isinstance(entry, dict) else None
            if not isinstance(reads, list):
                continue
            for read in reads:
                if not isinstance(read, dict):
                    continue
                read["inertia_tensor_about_com"] = _negated_matrix(
                    read.get("inertia_tensor_about_com")
                )
                read["inertia_values_about_origin_api"] = _negated_flat(
                    read.get("inertia_values_about_origin_api")
                )
        recheck = detect_family(link_groups)
        if recheck["family"] != STANDARD:
            raise CadError(
                "inertia_convention_normalization_failed",
                "normalized Capture projection does not prove the standard convention",
                {"detected_after_normalization": recheck["family"]},
            )
    return {
        "detected": decision["family"],
        "applied": applied,
        "decision": decision,
    }


def _project_reference_axis(raw):
    if not isinstance(raw, dict) or raw.get("kind") != "reference_axis":
        raise CadError(
            "cad_axis_reference_unreadable",
            "raw reference-axis facts are malformed",
        )
    read_errors = raw.get("read_errors")
    if not isinstance(read_errors, dict) or read_errors:
        raise CadError(
            "cad_axis_reference_unreadable",
            "raw reference-axis read failed",
            {
                "feature_name": raw.get("feature_name"),
                "errors": copy.deepcopy(read_errors),
            },
        )
    if raw.get("suppressed_raw") is not False:
        raise CadError(
            "cad_axis_reference_unreadable",
            "reference axis is suppressed or its suppression state is not false",
            {
                "feature_name": raw.get("feature_name"),
                "suppressed": raw.get("suppressed_raw"),
            },
        )
    if (
        type(raw.get("feature_error_code_raw")) is not int
        or raw["feature_error_code_raw"] != 0
    ):
        raise CadError(
            "cad_axis_reference_unreadable",
            "reference axis has a nonzero or unreadable feature error",
            {
                "feature_name": raw.get("feature_name"),
                "error_code": raw.get("feature_error_code_raw"),
            },
        )
    raw_endpoints = raw.get("endpoints_raw")
    if not isinstance(raw_endpoints, (list, tuple)) or any(
        type(value) not in (int, float) for value in raw_endpoints
    ):
        raise CadError(
            "cad_axis_reference_unreadable",
            "GetRefAxisParams did not return native numeric endpoints",
        )
    endpoints = [float(value) for value in raw_endpoints]
    if len(endpoints) != 6 or not all(
        math.isfinite(value) for value in endpoints
    ):
        raise CadError(
            "cad_axis_reference_unreadable",
            "GetRefAxisParams must return six finite endpoint coordinates",
            {
                "feature_name": raw.get("feature_name"),
                "raw_endpoints": copy.deepcopy(raw_endpoints),
            },
        )
    vector = [endpoints[index + 3] - endpoints[index] for index in range(3)]
    norm = math.hypot(*vector)
    if norm <= 0 or not math.isfinite(norm):
        raise CadError(
            "cad_axis_reference_unreadable",
            "reference-axis endpoints do not define a finite nonzero line",
            {
                "feature_name": raw.get("feature_name"),
                "raw_endpoints": copy.deepcopy(raw_endpoints),
            },
        )
    return {
        "kind": "reference_axis",
        "component": str(raw.get("component") or ""),
        "selector": copy.deepcopy(raw.get("selector") or {}),
        "coordinate_frame": str(raw.get("coordinate_frame") or ""),
        "direction_semantics": "undirected_axis_line",
        "raw_endpoints_m": endpoints,
        "axis_point_m": endpoints[:3],
        "axis_direction": [value / norm for value in vector],
        "used_api": str(raw.get("used_api") or ""),
    }


def _project_cylinder_axis(raw):
    read_errors = raw.get("read_errors")
    if not isinstance(read_errors, dict):
        raise CadError(
            "cad_axis_reference_invalid",
            "raw cylinder axis has no error map",
        )
    if read_errors:
        raise CadError(
            "cad_axis_reference_unreadable",
            "raw cylinder-axis selection is incomplete",
            {
                "component": raw.get("component"),
                "errors": copy.deepcopy(read_errors),
            },
        )
    candidates = raw.get("face_candidates")
    if not isinstance(candidates, list):
        raise CadError(
            "cad_axis_reference_invalid",
            "raw cylinder axis has no face candidates",
        )
    cylindrical = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            raise CadError(
                "cad_axis_reference_invalid",
                "a raw axis candidate is malformed",
            )
        errors = candidate.get("read_errors")
        if not isinstance(errors, dict):
            raise CadError(
                "cad_axis_reference_invalid",
                "a raw axis candidate has no error map",
            )
        if "is_cylinder" in errors:
            raise CadError(
                "cad_axis_reference_unreadable",
                "ISurface.IsCylinder could not be read",
                copy.deepcopy(errors["is_cylinder"]),
            )
        if "surface" in errors:
            raise CadError(
                "cad_axis_reference_unreadable",
                "ISurface type classification is unreadable",
                copy.deepcopy(errors["surface"]),
            )
        is_cylinder = candidate.get("is_cylinder")
        if type(is_cylinder) is not bool:
            raise CadError(
                "cad_axis_reference_invalid",
                "ISurface.IsCylinder did not return bool",
                {"value": repr(is_cylinder)},
            )
        if not is_cylinder:
            continue
        if "cylinder_params" in errors:
            raise CadError(
                "cad_axis_reference_unreadable",
                "a native cylindrical face did not expose CylinderParams",
                copy.deepcopy(errors),
            )
        cylindrical.append(candidate)
    if len(cylindrical) != 1:
        raise CadError(
            "cad_axis_reference_ambiguous"
            if cylindrical
            else "cad_axis_reference_not_cylinder",
            "the selector does not identify exactly one native cylindrical face",
            {
                "component": raw.get("component"),
                "feature_name": raw.get("feature_name"),
                "face_index": raw.get("face_index"),
                "cylindrical_face_count": len(cylindrical),
            },
        )
    candidate = cylindrical[0]
    if (
        type(candidate.get("body_index")) is not int
        or candidate["body_index"] < 0
        or (
            candidate.get("face_index") is not None
            and (
                type(candidate.get("face_index")) is not int
                or candidate["face_index"] < 0
            )
        )
    ):
        raise CadError(
            "cad_axis_reference_invalid",
            "raw cylinder candidate identity is malformed",
        )
    raw_params = candidate.get("cylinder_params_raw")
    if not isinstance(raw_params, (list, tuple)) or any(
        type(value) not in (int, float) for value in raw_params
    ):
        raise CadError(
            "cad_axis_reference_not_cylinder",
            "CylinderParams did not return native numeric values",
        )
    params = [float(value) for value in raw_params]
    if len(params) != 7 or not all(map(math.isfinite, params)):
        raise CadError(
            "cad_axis_reference_not_cylinder",
            "CylinderParams must contain seven finite values",
            {"raw_values": copy.deepcopy(raw_params)},
        )
    point, direction, radius = params[0:3], params[3:6], params[6]
    norm = math.hypot(*direction)
    if abs(norm - 1.0) > 1e-6 or radius <= 0.0:
        raise CadError(
            "cad_axis_reference_not_cylinder",
            "native cylinder parameters require a unit axis and positive radius",
            {
                "component": raw.get("component"),
                "face_index": candidate.get("face_index"),
                "radius": radius,
                "axis_norm": norm,
            },
        )
    result = {
        "kind": "cylinder",
        "component": str(raw.get("component") or ""),
        "body_type": str(raw.get("body_type_native")),
        "selector": copy.deepcopy(raw.get("selector") or {}),
        "surface": "cylinder",
        "coordinate_frame": "component_local",
        "direction_semantics": "undirected_axis_line",
        "body_index": candidate.get("body_index"),
        "face_index": candidate.get("face_index"),
        "cylinder_params_raw": params,
        "is_cylinder_raw": candidate["is_cylinder"],
        "axis_point_m": point,
        "axis_direction": [value / norm for value in direction],
        "radius_m": radius,
        "used_api": str(raw.get("used_api") or ""),
    }
    for key in ("feature_name", "persist_reference_b64"):
        if candidate.get(key) is not None:
            result[key] = candidate[key]
    return result


def project_axis_references(raw_rows):
    """Project captured axis raw values without opening a CAD session."""

    if not isinstance(raw_rows, list):
        raise CadError(
            "cad_axis_reference_invalid",
            "raw axis rows are not a list",
        )
    projected = []
    for raw in raw_rows:
        if (
            not isinstance(raw, dict)
            or not isinstance(raw.get("joint"), str)
            or not raw["joint"]
        ):
            raise CadError(
                "cad_axis_reference_invalid",
                "a raw axis row has no joint identity",
            )
        kind = raw.get("kind")
        if kind == "reference_axis":
            result = _project_reference_axis(raw)
        elif kind == "cylinder":
            result = _project_cylinder_axis(raw)
        else:
            raise CadError(
                "cad_axis_reference_invalid",
                "a raw axis row has no supported kind",
            )
        projected.append({"joint": raw["joint"], **result})
    return projected


__all__ = [
    "NATIVE_CAPTURE_FACTS",
    "build_native_capture_observation",
    "load_native_capture_observation",
    "normalize_capture_projection",
    "project_axis_references",
    "project_capture_facts",
    "project_native_capture_observation",
    "validate_native_capture_plan",
]
