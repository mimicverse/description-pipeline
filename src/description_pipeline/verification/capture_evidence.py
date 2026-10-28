"""Standard-library readers and independent validation for frozen capture evidence."""

from __future__ import annotations

import copy
import hashlib
import math
import ntpath
import re
import tempfile
from pathlib import Path

from ..delivery import read_source_record
from ..geometry.native_scope import native_source_scope_is_valid
from ..geometry.native_tessellation import (
    build_native_tessellation,
    materialize_native_stl,
    native_stl_relative_path,
    parse_native_tessellation,
)
from ..geometry.native_transform import transform_from_solidworks
from ..io import PipelineError, confined, file_digest, read_data
from ..schemas import EFFECTIVE_INSTANCES_SCHEMA, LINK_GROUP_MASS_SCHEMA
from ..sources.snapshot import verify_snapshot
from ..sources.solidworks.capture_projection import load_native_capture_observation
from ..sources.solidworks.inertia_convention import POSITIVE, detect_family
from ..sources.solidworks.session_artifacts import (
    embedded_virtual_component_under_root,
    session_root_pid,
)


def is_container(component: dict | None) -> bool:
    """An assembly-document occurrence is a container, never a physical member."""

    if not isinstance(component, dict):
        return False
    return str(component.get("document") or "").strip().lower().endswith(".sldasm")


# Delivery evidence reads (IF-13 tree): one owner for the frozen block shapes.

DISCOVERY = "input/discovery/native-discovery.json"
RUNTIME = "evidence/native-runtime.json"
HANDOFF = "input/handoff.json"


def _delivery_root(root: Path) -> Path:
    root = Path(root).resolve()
    return root.parent if root.name == "evidence" else root


def _observation(root) -> dict:
    delivery = _delivery_root(Path(root))
    return load_native_capture_observation(delivery)


def _finite_number(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _finite_vector(value: object, length: int) -> bool:
    return (
        isinstance(value, list)
        and len(value) == length
        and all(_finite_number(item) for item in value)
    )


def _negated_matrix(value: list[list[float]]) -> list[list[float]]:
    result = [list(row) for row in value]
    for row, column in ((0, 1), (0, 2), (1, 2)):
        result[row][column] = -result[row][column]
        result[column][row] = -result[column][row]
    return result


def _raw_inertia(
    value: object,
    *,
    positive_products: bool,
    label: str,
) -> list[list[float]]:
    if (
        not isinstance(value, list)
        or len(value) != 9
        or any(not _finite_number(item) for item in value)
    ):
        raise PipelineError(f"{label}: raw inertia is not nine finite values")
    matrix = [
        [float(value[row * 3 + column]) for column in range(3)]
        for row in range(3)
    ]
    return _negated_matrix(matrix) if positive_products else matrix


def _raw_link_family(observation: dict) -> str:
    raw = observation.get("link_group_mass_raw")
    links = raw.get("links") if isinstance(raw, dict) else None
    if not isinstance(links, dict) or not links:
        raise PipelineError("raw link-group facts carry no links")
    detector = {"links": {}}
    for name, entry in links.items():
        reads = entry.get("reads") if isinstance(entry, dict) else None
        if not isinstance(reads, list) or len(reads) != 2:
            raise PipelineError(f"raw link-group {name} does not carry two reads")
        detector["links"][str(name)] = {
            "reads": [
                {
                    "mass_kg": read.get("mass_kg"),
                    "com_m": read.get("com_m"),
                    "inertia_tensor_about_com": _raw_inertia(
                        read.get("inertia_values_raw"),
                        positive_products=False,
                        label=f"raw link-group {name}",
                    ),
                    "inertia_values_about_origin_api": read.get(
                        "inertia_values_about_origin_api"
                    ),
                    "read_errors": copy.deepcopy(read.get("read_errors")),
                }
                for read in reads
                if isinstance(read, dict)
            ],
        }
        if len(detector["links"][str(name)]["reads"]) != 2:
            raise PipelineError(f"raw link-group {name} has a malformed read")
    return detect_family(detector)["family"]


def _raw_measurement(
    raw: object,
    *,
    positive_products: bool,
    label: str,
) -> dict:
    if not isinstance(raw, dict):
        raise PipelineError(f"{label}: raw mass reading is not an object")
    read_errors = raw.get("read_errors")
    if not isinstance(read_errors, dict):
        raise PipelineError(f"{label}: raw mass reading has no error map")
    for field, error in read_errors.items():
        if not isinstance(error, dict):
            raise PipelineError(
                f"{label}: raw {field} read error is malformed"
            )
        if field in {"volume_m3", "density_kg_m3"}:
            continue
        if (
            field == "material"
            and isinstance(error, dict)
            and error.get("code") == "cad_material_provenance_missing"
        ):
            continue
        raise PipelineError(
            f"{label}: raw {field} read failed: "
            f"{error.get('code') or 'cad_mass_read_failed'}"
        )
    native = raw.get("native_raw")
    if not isinstance(native, dict):
        raise PipelineError(f"{label}: raw mass reading has no native values")
    mass = native.get("mass_kg")
    com = native.get("center_of_mass_m")
    if not _finite_number(mass) or float(mass) <= 0.0:
        raise PipelineError(f"{label}: raw mass is not finite and positive")
    if not _finite_vector(com, 3):
        raise PipelineError(f"{label}: raw COM is not a finite 3-vector")
    reference = copy.deepcopy(raw.get("reference") or {})
    for field in ("volume_m3", "density_kg_m3"):
        value = native.get(field)
        if value is not None and not _finite_number(value):
            raise PipelineError(f"{label}: raw {field} is not finite")
        reference[field] = None if value is None else float(value)
    result = {
        "mass": float(mass),
        "com": [float(item) for item in com],
        "inertia": _raw_inertia(
            native.get("inertia_values"),
            positive_products=positive_products,
            label=label,
        ),
        "reference": reference,
    }
    for field in ("mode", "role", "state", "measurement_basis", "document_type"):
        if raw.get(field) is not None:
            result[field] = raw[field]
    return result


def _matrix4(values: object, *, label: str) -> list[list[float]]:
    if (
        not isinstance(values, (list, tuple))
        or len(values) != 16
        or any(not _finite_number(value) for value in values)
    ):
        raise PipelineError(f"{label} is not a finite row-major matrix")
    matrix = [float(value) for value in values]
    return [matrix[offset : offset + 4] for offset in range(0, 16, 4)]


def _compose_matrix(left: list[float], right: list[float]) -> list[float]:
    if len(left) != 16 or len(right) != 16:
        raise PipelineError("raw scene matrix composition requires two 4x4 matrices")
    return [
        math.fsum(
            float(left[row * 4 + index])
            * float(right[index * 4 + column])
            for index in range(4)
        )
        for row in range(4)
        for column in range(4)
    ]


def _raw_geometry(root: Path, observation: dict) -> dict[str, dict]:
    delivery = _delivery_root(Path(root))
    geometry_raw = observation["geometry_raw"]
    source_scopes = observation["source_scopes"]
    result: dict[str, dict] = {}
    with tempfile.TemporaryDirectory(prefix="verify-raw-geometry-") as directory:
        scratch = Path(directory)
        for component, raw in sorted(geometry_raw.items()):
            source_scope = source_scopes[component]
            raw_path = confined(delivery / "evidence", raw["stream_path"])
            triangle_count = raw["triangle_count"]
            warnings: list[dict] = []
            tessellation = build_native_tessellation(
                component,
                raw["body_inventory"],
                raw["face_inventory"],
                source_scope=source_scope,
            )
            spans = parse_native_tessellation(
                component,
                tessellation,
                raw["body_counts"],
                triangle_count,
                source_scope=source_scope,
                warnings=warnings,
            )
            target = scratch / f"{len(result)}.stl"
            materialize_native_stl(raw_path, target, spans)
            source_bytes = target.read_bytes()
            source_sha = hashlib.sha256(source_bytes).hexdigest()
            appearance = copy.deepcopy(raw.get("appearance_raw"))
            if appearance is not None:
                appearance = {
                    "component": component,
                    "source_document_path": source_scope.get(
                        "source_document_path"
                    ),
                    "referenced_configuration": source_scope.get(
                        "referenced_configuration"
                    ),
                    **appearance,
                }
            result[component] = {
                "component": component,
                "raw_path": raw["stream_path"],
                "raw_sha": raw["stream_sha256"],
                "path": native_stl_relative_path(source_sha),
                "sha": source_sha,
                "triangles": triangle_count,
                "bodies": copy.deepcopy(raw["body_counts"]),
                "used_api": raw["used_api"],
                "tessellation": tessellation,
                "transform_raw": copy.deepcopy(raw["transform_raw"]),
                "source_scope": copy.deepcopy(source_scope),
                "source_bytes": source_bytes,
                "appearance_raw": appearance,
                "warnings": warnings,
            }
    return result


def _effective_readings(
    observation: dict,
    positive_products: bool,
) -> dict[str, dict]:
    readings: dict[str, dict] = {}
    component = observation.get("component_mass_raw")
    if isinstance(component, dict):
        rows = component.get("instances")
        if not isinstance(rows, list):
            raise PipelineError("raw component mass contexts are malformed")
        for raw in rows:
            if not isinstance(raw, dict) or not isinstance(raw.get("name"), str):
                raise PipelineError("raw component mass context has no identity")
            effective = raw.get("effective")
            if isinstance(effective, dict):
                reading = _raw_measurement(
                    effective,
                    positive_products=positive_products,
                    label=f"effective instance {raw['name']}",
                )
                part_raw = effective.get("part_measurement_raw")
                if isinstance(part_raw, dict):
                    part = _raw_measurement(
                        part_raw,
                        positive_products=positive_products,
                        label=(
                            f"effective instance {raw['name']} "
                            "part measurement"
                        ),
                    )
                    part_reference = part.get("reference") or {}
                    reading["part_measurement"] = {
                        "mass_kg": part["mass"],
                        "center_of_mass_m": part["com"],
                        "inertia_kg_m2": part["inertia"],
                        "scope": part_reference.get("scope"),
                        "used_api": part_reference.get("used_api"),
                        "product_convention": part_reference.get(
                            "product_convention"
                        ),
                        **{
                            key: part_reference.get(key)
                            for key in ("volume_m3", "density_kg_m3")
                            if key in part_reference
                        },
                    }
                readings[raw["name"]] = reading
    parts = observation.get("part_mass_raw")
    if not isinstance(parts, dict):
        raise PipelineError("raw part mass facts are malformed")
    for name, raw in sorted(parts.items()):
        if not isinstance(raw, dict) or raw.get("mode") != "effective_instance":
            continue
        reading = _raw_measurement(
            raw,
            positive_products=positive_products,
            label=f"effective part {name}",
        )
        previous = readings.get(str(name))
        if previous is not None and previous != reading:
            raise PipelineError(
                f"effective instance {name} has conflicting raw readings"
            )
        readings[str(name)] = reading
    return readings


def discovery(root) -> dict:
    record = read_data(confined(_delivery_root(root), DISCOVERY))
    if not isinstance(record, dict):
        raise PipelineError("Discovery evidence is not an object")
    return record


def runtime(root) -> dict:
    record = read_data(confined(_delivery_root(root), RUNTIME))
    if not isinstance(record, dict):
        raise PipelineError("Native runtime evidence is not an object")
    return record


def handoff(root) -> dict:
    record = read_data(confined(_delivery_root(root), HANDOFF))
    if not isinstance(record, dict):
        raise PipelineError("Frozen handoff record is not an object")
    return record


def source(root) -> dict:
    return read_source_record(_delivery_root(root))


def scene(root) -> dict:
    observation = _observation(root)
    positive_products = _raw_link_family(observation) == POSITIVE
    raw_scene = observation["scene_raw"]
    geometry_raw = observation["geometry_raw"]
    source_scopes = observation["source_scopes"]
    occurrences = {}
    for index, row in enumerate(raw_scene.get("occurrences") or []):
        name = row.get("name") if isinstance(row, dict) else None
        if not isinstance(name, str) or not name or name in occurrences:
            raise PipelineError(
                f"raw scene occurrence {index} has no unique identity"
            )
        fixed = row.get("is_fixed")
        if type(fixed) is not bool:
            raise PipelineError(
                f"raw scene occurrence {name} has no native fixed state"
            )
        if row.get("is_fixed_error") is not None:
            raise PipelineError(
                f"raw scene occurrence {name} carries a contradictory fixed error"
            )
        values = transform_from_solidworks(row.get("transform_raw"))
        occurrences[name] = list(values)
    components = []
    names = set()
    for index, row in enumerate(raw_scene.get("components") or []):
        name = row.get("name") if isinstance(row, dict) else None
        if not isinstance(name, str) or not name or name in names:
            raise PipelineError(
                f"raw scene component {index} has no unique identity"
            )
        occurrence = occurrences.get(name)
        if occurrence is None:
            raise PipelineError(
                f"raw scene component {name} has no occurrence transform"
            )
        fixed = row.get("is_fixed")
        if type(fixed) is not bool:
            raise PipelineError(
                f"raw scene component {name} has no native fixed state"
            )
        if row.get("is_fixed_error") is not None:
            raise PipelineError(
                f"raw scene component {name} carries a contradictory fixed error"
            )
        names.add(name)
        components.append(
            {
                "name": name,
                "path": str(row.get("path") or ""),
                "transform": occurrence,
                "is_fixed": fixed,
                "document_type": str(row.get("document_type") or ""),
            }
        )
    raw_datums = raw_scene.get("coordinate_systems")
    if type(raw_datums) is not dict:
        raise PipelineError("raw scene coordinate systems have no object inventory")
    datums = {}
    for key, datum in sorted(raw_datums.items()):
        if not isinstance(datum, dict):
            raise PipelineError(f"raw coordinate system {key} is malformed")
        read_errors = datum.get("read_errors")
        if type(read_errors) is not dict:
            raise PipelineError(
                f"raw coordinate system {key} has no error map"
            )
        if read_errors:
            raise PipelineError(f"raw coordinate system {key} carries a read error")
        local = list(transform_from_solidworks(datum.get("array_data")))
        owner = datum.get("owner")
        if type(owner) is not str:
            raise PipelineError(
                f"raw coordinate system {key} has no string owner"
            )
        if owner:
            placement = occurrences.get(owner)
            if placement is None:
                raise PipelineError(
                    f"raw coordinate system {key} names an unknown owner"
                )
            local = _compose_matrix(placement, local)
        datums[str(key)] = local
    part_mass = observation["part_mass_raw"]
    masses = {
        str(name): _raw_measurement(
            raw,
            positive_products=positive_products,
            label=f"part {name}",
        )
        for name, raw in sorted(part_mass.items())
    }
    if set(masses) != names:
        raise PipelineError("raw part masses differ from the scene component set")
    appearance = {}
    for name, row in geometry_raw.items():
        raw_appearance = copy.deepcopy(row.get("appearance_raw"))
        if raw_appearance is None:
            continue
        source_scope = source_scopes[name]
        appearance[name] = {
            "appearance_raw": {
                "component": name,
                "source_document_path": source_scope.get(
                    "source_document_path"
                ),
                "referenced_configuration": source_scope.get(
                    "referenced_configuration"
                ),
                **raw_appearance,
            }
        }
    notes = copy.deepcopy(observation.get("notes") or {})
    effective = _effective_readings(observation, positive_products)
    notes["effective_instances"] = copy.deepcopy(effective)
    return {
        "document": str(raw_scene.get("document") or ""),
        "components": [
            {**row, **appearance.get(row["name"], {})}
            for row in components
        ],
        "coordinate_systems": datums,
        "mass_properties": masses,
        "notes": notes,
        "source_scopes": copy.deepcopy(source_scopes),
    }


def mass_closure(root) -> dict:
    observation = _observation(root)
    positive_products = _raw_link_family(observation) == POSITIVE
    assembly_raw = observation.get("assembly_mass_raw")
    if not isinstance(assembly_raw, dict):
        raise PipelineError("raw assembly mass facts are missing")
    assembly = _raw_measurement(
        assembly_raw,
        positive_products=positive_products,
        label="assembly",
    )
    component = observation.get("component_mass_raw")
    if not isinstance(component, dict):
        raise PipelineError("raw component mass context is missing")
    errors = component.get("errors")
    rows = component.get("instances")
    if not isinstance(errors, list) or not isinstance(rows, list):
        raise PipelineError("raw component mass context is malformed")
    instances = []
    for raw in rows:
        if not isinstance(raw, dict) or not isinstance(raw.get("name"), str):
            raise PipelineError("raw component mass instance is malformed")
        context_mass = raw.get("context_mass_kg")
        if not _finite_number(context_mass) or float(context_mass) <= 0.0:
            raise PipelineError(
                f"raw component context {raw['name']} mass is invalid"
            )
        volume = raw.get("context_volume_m3")
        if volume is not None and not _finite_number(volume):
            raise PipelineError(
                f"raw component context {raw['name']} volume is invalid"
            )
        effective = raw.get("effective")
        instances.append(
            {
                **copy.deepcopy(raw),
                "context_mass_kg": float(context_mass),
                "context_volume_m3": (
                    None if volume is None else float(volume)
                ),
                "effective": (
                    _raw_measurement(
                        effective,
                        positive_products=positive_products,
                        label=f"effective instance {raw['name']}",
                    )
                    if isinstance(effective, dict)
                    else None
                ),
            }
        )
    return {
        "schema_version": "solidworks-to-urdf.mass-closure/v1",
        "status": "recorded" if not errors else "partial",
        "mode": "full",
        "top_level": {
            "mass": assembly["mass"],
            "com": assembly["com"],
            "inertia": assembly["inertia"],
            "reference": assembly["reference"],
        },
        "component_context": {
            "schema_version": "solidworks-to-urdf.component-mass-context/v1",
            "status": "recorded" if not errors else "partial",
            "assembly_mass_kg": assembly["mass"],
            "instances": instances,
            "errors": copy.deepcopy(errors),
            "reference": copy.deepcopy(component.get("reference") or {}),
        },
    }


def material_provenance(root) -> dict:
    scene_record = scene(root)
    assembly = (
        (_observation(root).get("component_mass_raw") or {}).get("assembly")
        or scene_record.get("document")
        or ""
    )
    configuration = (
        (_observation(root).get("scene_raw") or {}).get("configuration")
        or ""
    )
    warnings = []
    for name, reading in sorted(scene_record["mass_properties"].items()):
        reference = reading.get("reference") or {}
        assignment = reference.get("material_assignment")
        if not isinstance(assignment, dict) or "unverified_reason" not in assignment:
            continue
        warnings.append(
            {
                "code": str(assignment["unverified_reason"]),
                "message": str(
                    assignment.get("message")
                    or "material assignment is not verified"
                ),
                "blocking": False,
                "component": name,
                "document": reference.get("part_document"),
                "configuration": (
                    reference.get("configuration")
                    or assignment.get("configuration")
                ),
                "missing_body_indices": assignment.get(
                    "missing_body_indices"
                ),
                "material_assignment": copy.deepcopy(assignment),
                "measured": {
                    "source": "cad_api",
                    "scope": reference.get("scope"),
                    "mass_kg": reading["mass"],
                    "center_of_mass_m": reading["com"],
                    "inertia_kg_m2": reading["inertia"],
                    "volume_m3": reference.get("volume_m3"),
                    "density_kg_m3": reference.get("density_kg_m3"),
                },
            }
        )
    return {
        "schema_version": "solidworks-to-urdf.material-provenance/v1",
        "assembly": str(assembly),
        "configuration": str(configuration),
        "measured_source": "cad_api",
        "warnings": warnings,
    }


def dependency_closure(root) -> dict:
    record = read_data(
        confined(
            _delivery_root(root),
            "evidence/raw/dependency_closure.json",
        )
    )
    if not isinstance(record, dict):
        raise PipelineError("Dependency closure is not an object")
    return record


def link_group_mass_properties(root) -> dict:
    observation = _observation(root)
    positive_products = _raw_link_family(observation) == POSITIVE
    raw = observation["link_group_mass_raw"]
    links = raw.get("links")
    if not isinstance(links, dict):
        raise PipelineError("raw link-group facts carry no links")
    projected_links = {}
    for name, entry in sorted(links.items()):
        reads = entry.get("reads") if isinstance(entry, dict) else None
        if not isinstance(reads, list) or len(reads) != 2:
            raise PipelineError(
                f"raw link-group {name} does not carry two reads"
            )
        projected_reads = []
        for index, read in enumerate(reads):
            if not isinstance(read, dict):
                raise PipelineError(
                    f"raw link-group {name} read {index} is malformed"
                )
            read_errors = read.get("read_errors")
            if not isinstance(read_errors, dict):
                raise PipelineError(
                    f"raw link-group {name} read {index} has no error map"
                )
            for field, error in read_errors.items():
                if not isinstance(error, dict):
                    raise PipelineError(
                        f"raw link-group {name} read {index} {field} error is malformed"
                    )
                if field == "inertia_values_about_origin_api":
                    if (
                        read.get("inertia_values_about_origin_api") != error
                    ):
                        raise PipelineError(
                            f"raw link-group {name} read {index} origin error "
                            "differs from its read-error entry"
                        )
                    continue
                if field in {
                    "volume_m3",
                    "density_kg_m3",
                }:
                    continue
                raise PipelineError(
                    f"raw link-group {name} read {index} {field} failed: "
                    f"{error.get('code') or 'cad_mass_read_failed'}"
                )
            mass = read.get("mass_kg")
            com = read.get("com_m")
            if not _finite_number(mass) or float(mass) <= 0.0:
                raise PipelineError(
                    f"raw link-group {name} read {index} mass is invalid"
                )
            if not _finite_vector(com, 3):
                raise PipelineError(
                    f"raw link-group {name} read {index} COM is invalid"
                )
            tensor = _raw_inertia(
                read.get("inertia_values_raw"),
                positive_products=positive_products,
                label=f"raw link-group {name} read {index}",
            )
            origin = copy.deepcopy(
                read.get("inertia_values_about_origin_api")
            )
            if isinstance(origin, dict):
                if (
                    not isinstance(origin.get("code"), str)
                    or not origin["code"].strip()
                ):
                    raise PipelineError(
                        f"raw link-group {name} read {index} origin error is malformed"
                    )
                if (
                    read_errors.get("inertia_values_about_origin_api")
                    != origin
                ):
                    raise PipelineError(
                        f"raw link-group {name} read {index} origin error "
                        "differs from its read-error entry"
                    )
            elif origin is not None:
                if (
                    not isinstance(origin, list)
                    or len(origin) != 9
                    or any(not _finite_number(value) for value in origin)
                ):
                    raise PipelineError(
                        f"raw link-group {name} read {index} origin tensor is invalid"
                    )
                if positive_products:
                    for offset in (1, 2, 3, 5, 6, 7):
                        origin[offset] = -float(origin[offset])
            projected_reads.append(
                {
                    "mass_kg": float(mass),
                    "com_m": [float(value) for value in com],
                    "inertia_tensor_about_com": tensor,
                    "overrides": copy.deepcopy(read.get("overrides")),
                    "inertia_values_about_origin_api": origin,
                    **{
                        key: copy.deepcopy(read.get(key))
                        for key in ("volume_m3", "density_kg_m3")
                        if key in read
                    },
                }
            )
        tensor_delta = max(
            abs(
                projected_reads[0]["inertia_tensor_about_com"][row][column]
                - projected_reads[1]["inertia_tensor_about_com"][row][column]
            )
            for row in range(3)
            for column in range(3)
        )
        projected_links[str(name)] = {
            "selection": [
                str(item) for item in (entry.get("selection") or [])
            ],
            "reads": projected_reads,
            "reproducibility": {
                "mass_delta_kg": abs(
                    projected_reads[0]["mass_kg"]
                    - projected_reads[1]["mass_kg"]
                ),
                "com_delta_norm_m": math.dist(
                    projected_reads[0]["com_m"],
                    projected_reads[1]["com_m"],
                ),
                "tensor_delta_max_abs": tensor_delta,
            },
        }
    return {
        "schema_version": LINK_GROUP_MASS_SCHEMA,
        "assembly": str(raw.get("assembly") or ""),
        "configuration": str(raw.get("configuration") or ""),
        "used_api": str(raw.get("used_api") or ""),
        "selection_rule": str(raw.get("selection_rule") or ""),
        "links": projected_links,
        "readout_window": "single-read-only-pass",
    }


def collection(root) -> dict:
    """The source manifest identity, the one surviving collection authority."""

    return verify_snapshot(_delivery_root(root) / "evidence")


def effective_instances(root) -> dict:
    """Independently reconstruct effective-instance readings from raw facts."""

    observation = _observation(root)
    positive_products = _raw_link_family(observation) == POSITIVE
    readings = _effective_readings(observation, positive_products)
    warnings = []
    for name, reading in sorted(readings.items()):
        reference = reading.get("reference") or {}
        overrides = reference.get("overrides") or {}
        instance_difference = (
            reference.get("measurement_basis")
            == "cad_assembly_instance_measurement"
        )
        if any(value is True for value in overrides.values()) or instance_difference:
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
                        else (
                            "the instance carries CAD-authored mass-property overrides; "
                            "these are effective CAD values, not geometry qualification"
                        )
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
                    "overrides": copy.deepcopy(overrides),
                    "qualification": (
                        "cad_assembly_instance_measurement; cause_of_part_difference_unproven; "
                        "not geometry-derived, not independently physically qualified"
                        if instance_difference
                        else (
                            "cad_authored_effective_instance; not geometry-derived, "
                            "not independently physically qualified"
                        )
                    ),
                    **(
                        {
                            "part_measurement": copy.deepcopy(
                                reading["part_measurement"]
                            )
                        }
                        if instance_difference
                        else {}
                    ),
                    "measurement": {
                        "scope": reference.get("scope"),
                        "axes": reference.get("axes"),
                        "used_api": reference.get("used_api"),
                        "selection_api": reference.get("selection_api"),
                        "override_api": reference.get("override_api"),
                        "mass_kg": reading["mass"],
                        "center_of_mass_m": reading["com"],
                        "inertia_kg_m2": reading["inertia"],
                        "volume_m3": reference.get("volume_m3"),
                        "density_kg_m3": reference.get("density_kg_m3"),
                    },
                }
            )
    for name, reading in sorted(scene(root)["mass_properties"].items()):
        if reading.get("mode") != "effective_instance":
            continue
        reference = reading.get("reference") or {}
        warnings.append(
            {
                "code": "cad_surface_only_effective_instance",
                "blocking": False,
                "message": (
                    "the part is surface-only (no solid bodies); its mass properties come from the assembly "
                    "instance measurement, not from geometric qualification"
                ),
                "component": name,
                "document": reference.get("part_document"),
                "configuration": reference.get("configuration"),
                "assembly_document": reference.get("assembly_document"),
                "assembly_configuration": reference.get(
                    "assembly_configuration"
                ),
                "selected_components": reference.get("selected_components"),
                "part_document_read": "unavailable",
                "fallback_reason": reference.get("fallback_reason"),
                "fallback_error_code": reference.get("fallback_error_code"),
                "solid_bodies": reference.get("solid_bodies"),
                "sheet_bodies": reference.get("sheet_bodies"),
                "overrides": copy.deepcopy(reference.get("overrides") or {}),
                "qualification": (
                    "cad_authored_effective_instance; not geometry-derived, "
                    "not independently physically qualified"
                ),
                "measurement": {
                    "state": reading.get("state"),
                    "measurement_basis": reading.get("measurement_basis"),
                    "scope": reference.get("scope"),
                    "axes": reference.get("axes"),
                    "used_api": reference.get("used_api"),
                    "selection_api": reference.get("selection_api"),
                    "override_api": reference.get("override_api"),
                    "mass_kg": reading["mass"],
                    "center_of_mass_m": reading["com"],
                    "inertia_kg_m2": reading["inertia"],
                    "volume_m3": reference.get("volume_m3"),
                    "density_kg_m3": reference.get("density_kg_m3"),
                },
            }
        )
    component = observation.get("component_mass_raw") or {}
    assembly = (
        component.get("assembly")
        or (observation.get("scene_raw") or {}).get("document")
        or ""
    )
    configuration = (
        (observation.get("scene_raw") or {}).get("configuration") or ""
    )
    cache_error = (
        copy.deepcopy(observation.get("notes", {}).get("component_mass_error"))
        if isinstance(observation.get("notes"), dict)
        else None
    )
    return {
        "schema_version": EFFECTIVE_INSTANCES_SCHEMA,
        "assembly": str(assembly),
        "configuration": str(configuration),
        "cache_error": cache_error,
        "readings": readings,
        "warnings": warnings,
    }


def native_capture_facts(root) -> dict:
    """Read the one frozen raw native fact member."""

    delivery = _delivery_root(root)
    record = load_native_capture_observation(delivery)
    if not isinstance(record, dict):
        raise PipelineError("Sealed native capture facts are not an object")
    return record


def geometry(root):
    return list(_raw_geometry(Path(root), _observation(root)).values())


def _offline_virtual_component_root(value: object) -> str | None:
    """Recover one ``swxNNN`` root from sealed Windows or POSIX evidence."""

    if not isinstance(value, str) or not value.strip():
        return None
    text = value.replace("\\", "/")
    folded = text.casefold()
    marker = "/vc~~/"
    if folded.count(marker) != 1:
        return None
    position = folded.index(marker)
    prefix = text[:position]
    root = prefix.rsplit("/", 1)[-1]
    if re.fullmatch(r"swx[0-9]+", root, re.IGNORECASE) is None:
        return None
    return text[: position].replace("/", "\\") if "\\" in value else prefix


def _canonical_evidence_path(value: object) -> str | None:
    """Canonicalize one recorded path without host-dependent casing."""

    if not isinstance(value, str) or not value.strip():
        return None
    normalized = ntpath.normpath(value.replace("/", "\\")).replace(
        "\\",
        "/",
    )
    if normalized in {"", ".", ".."} or normalized.startswith("../"):
        return None
    return normalized.casefold()


def source_scopes(root) -> dict:
    """Read source scopes and cross-check them against finite sealed anchors."""

    observation = load_native_capture_observation(
        _delivery_root(root)
    )
    record = observation.get("source_scopes")
    if not isinstance(record, dict):
        raise PipelineError("Source-scope evidence is not an object")
    discovery_record = discovery(root)
    discovery_raw = (
        discovery_record.get("raw")
        if isinstance(discovery_record.get("raw"), dict)
        else {}
    )
    discovery_components = discovery_raw.get("components")
    if not isinstance(discovery_components, list):
        raise PipelineError("Discovery source components are unavailable")
    discovery_by_name = {}
    for row in discovery_components:
        if not isinstance(row, dict):
            raise PipelineError("Discovery source component is malformed")
        name = row.get("instance_id") or row.get("name2")
        if not isinstance(name, str) or not name:
            raise PipelineError("Discovery source component has no identity")
        if name in discovery_by_name:
            raise PipelineError(
                f"Discovery source component is duplicated: {name}"
            )
        discovery_by_name[name] = row
    scene_record = observation.get("scene_raw")
    if not isinstance(scene_record, dict):
        raise PipelineError("Native scene record is not an object")
    scene_components = scene_record.get("components")
    if not isinstance(scene_components, list):
        raise PipelineError("Scene source components are unavailable")
    scene_by_name = {}
    for row in scene_components:
        if not isinstance(row, dict):
            raise PipelineError("Scene source component is malformed")
        name = row.get("name")
        if not isinstance(name, str) or not name:
            raise PipelineError("Scene source component has no identity")
        if name in scene_by_name:
            raise PipelineError(
                f"Scene source component is duplicated: {name}"
            )
        scene_by_name[name] = row
    geometry_raw = observation.get("geometry_raw")
    if not isinstance(geometry_raw, dict):
        raise PipelineError("Geometry component inventory is malformed")
    geometry_names = set(geometry_raw)
    scope_names = set(record)
    scene_names = set(scene_by_name)
    discovery_exported_names = {
        name
        for name, row in discovery_by_name.items()
        if not is_container(row) and row.get("suppressed") is not True
    }
    if (
        scope_names != scene_names
        or discovery_exported_names != scene_names
            or scope_names != geometry_names
    ):
        raise PipelineError(
            "Source scopes do not cover the native component inventories"
        )
    source_record = source(root)
    if not isinstance(source_record, dict):
        raise PipelineError("Source record is malformed")
    selection = source_record.get("selection")
    if (
        not isinstance(selection, dict)
        or not isinstance(selection.get("configuration"), str)
        or not selection["configuration"]
    ):
        raise PipelineError("Source record has no requested configuration")
    native_files = discovery_record.get("native_files")
    if not isinstance(native_files, dict):
        raise PipelineError("Discovery source files are unavailable")

    def unique_path_index(entries, label):
        result = {}
        for raw_path, checksum in entries:
            if (
                not isinstance(raw_path, str)
                or not raw_path
                or not isinstance(checksum, str)
            ):
                raise PipelineError(f"{label} path index is malformed")
            key = ntpath.normpath(raw_path).replace("\\", "/").casefold()
            if key in result:
                raise PipelineError(f"{label} path index is duplicated: {key}")
            result[key] = checksum
        return result

    file_rows = list(native_files.items())
    discovery_files_case = unique_path_index(
        file_rows,
        "Discovery source files",
    )
    source_files = discovery_raw.get("files")
    if not isinstance(source_files, dict):
        raise PipelineError("Discovery source file hashes are unavailable")
    source_files_case = unique_path_index(
        source_files.items(),
        "Discovery source files",
    )
    dependency_path = confined(
        _delivery_root(root),
        "evidence/raw/dependency_closure.json",
    )
    dependency = read_data(dependency_path)
    if not isinstance(dependency, dict):
        raise PipelineError("Dependency closure is malformed")
    original_files = dependency.get("original_files")
    copy_files = dependency.get("copy_files")
    original_files_case = (
        unique_path_index(
            original_files.items(),
            "Dependency original files",
        )
        if isinstance(original_files, dict)
        else {}
    )
    copy_files_case = (
        unique_path_index(
            copy_files.items(),
            "Dependency copy files",
        )
        if isinstance(copy_files, dict)
        else {}
    )
    delivery_root = _delivery_root(root)
    frozen_source_digests = {}
    for name, scope in record.items():
        if not native_source_scope_is_valid(scope, name):
            raise PipelineError(
                f"Source scope is malformed or unbound: {name}"
            )
        discovery_row = discovery_by_name[name]
        scene_row = scene_by_name[name]
        configuration = discovery_row.get("configuration")
        if (
            not isinstance(configuration, str)
            or configuration != scope["referenced_configuration"]
        ):
            raise PipelineError(
                f"Source configuration differs from discovery: {name}"
            )
        mass_properties_raw = observation.get("part_mass_raw")
        mass_row = (
            mass_properties_raw.get(name)
            if isinstance(mass_properties_raw, dict)
            else None
        )
        mass_reference = (
            mass_row.get("reference")
            if isinstance(mass_row, dict)
            else None
        )
        if (
            isinstance(mass_reference, dict)
            and mass_reference.get("configuration")
            != scope["referenced_configuration"]
        ):
            raise PipelineError(
                f"Source configuration differs from capture mass: {name}"
            )
        if (
            "configuration" in scene_row
            and scene_row["configuration"]
            != scope["referenced_configuration"]
        ):
            raise PipelineError(
                f"Source configuration differs from scene: {name}"
            )
        source_path = scope["source_document_path"]
        scene_path = scene_row.get("path")
        if (
            not isinstance(scene_path, str)
            or ntpath.normcase(ntpath.normpath(scene_path))
            != ntpath.normcase(ntpath.normpath(source_path))
        ):
            raise PipelineError(
                f"Source path differs from scene: {name}"
            )
        if scope["source_document_identity_kind"] == "file_sha256":
            document = discovery_row.get("document")
            if not isinstance(document, str) or not document:
                raise PipelineError(
                    f"File source document is missing: {name}"
                )
            document_path = ntpath.normpath(document).replace("\\", "/")
            document_key = document_path.casefold()
            expected_source_sha = discovery_files_case.get(document_key)
            raw_source_sha = source_files_case.get(document_key)
            mapping_copy = _canonical_evidence_path(
                scope["source_copy_path"]
            )
            mapping_copy_key = (
                mapping_copy.casefold() if mapping_copy is not None else None
            )
            mapping_rows = [
                row
                for row in dependency.get("mapping") or []
                if isinstance(row, dict)
                and ntpath.normpath(str(row.get("copy") or ""))
                .replace("\\", "/")
                .casefold()
                == (mapping_copy_key or "")
            ]
            mapping_source = (
                mapping_rows[0].get("source")
                if len(mapping_rows) == 1
                else None
            )
            original_sha = (
                original_files_case.get(
                    ntpath.normpath(str(mapping_source))
                    .replace("\\", "/")
                    .casefold()
                )
                if isinstance(mapping_source, str)
                else None
            )
            copy_sha = (
                copy_files_case.get(mapping_copy_key)
                if mapping_copy_key is not None
                else None
            )
            normalized_source_path = (
                ntpath.normpath(source_path).replace("\\", "/").casefold()
            )
            normalized_mapping_source = (
                ntpath.normpath(str(mapping_source))
                .replace("\\", "/")
                .casefold()
                if isinstance(mapping_source, str)
                else ""
            )
            if (
                expected_source_sha is None
                or expected_source_sha != scope["source_document_sha256"]
                or raw_source_sha is None
                or raw_source_sha != scope["source_document_sha256"]
                or mapping_copy_key is None
                or len(mapping_rows) != 1
                or not isinstance(mapping_source, str)
                or original_sha != scope["source_document_sha256"]
                or mapping_rows[0].get("copy_sha256")
                != scope["source_copy_sha256"]
                or copy_sha != scope["source_copy_sha256"]
                or ntpath.basename(mapping_source).casefold()
                != ntpath.basename(document).casefold()
                or not normalized_mapping_source.endswith(
                    "/" + document_key
                )
                or not normalized_source_path.endswith(
                    "/" + mapping_copy_key
                )
                or ntpath.basename(source_path).casefold()
                != ntpath.basename(document).casefold()
            ):
                raise PipelineError(
                    f"File source identity differs from discovery: {name}"
                )
            frozen_source = confined(
                delivery_root,
                f"evidence/{scope['source_copy_path']}",
            )
            frozen_source_key = str(frozen_source.resolve())
            if not frozen_source.is_file():
                raise PipelineError(
                    f"Frozen source bytes differ from the source scope: {name}"
                )
            if frozen_source_key not in frozen_source_digests:
                frozen_source_digests[frozen_source_key] = file_digest(
                    frozen_source
                )
            if (
                frozen_source_digests[frozen_source_key]
                != scope["source_copy_sha256"]
            ):
                raise PipelineError(
                    f"Frozen source bytes differ from the source scope: {name}"
                )
        else:
            provenance = discovery_row.get("source_provenance")
            if not isinstance(provenance, dict):
                raise PipelineError(
                    f"Virtual source provenance is missing: {name}"
                )
            normalized_scope_path = _canonical_evidence_path(source_path)
            normalized_scope_root = _canonical_evidence_path(
                scope["virtual_session_temp_root"]
            )
            normalized_session_path = _canonical_evidence_path(
                provenance.get("session_document_path")
            )
            normalized_session_root = _canonical_evidence_path(
                provenance.get("session_temp_root")
            )
            scope_source_root = _offline_virtual_component_root(source_path)
            canonical_scope_source_root = _canonical_evidence_path(
                scope_source_root
            )
            scope_relative = (
                _canonical_evidence_path(
                    ntpath.relpath(
                        normalized_scope_path,
                        canonical_scope_source_root,
                    )
                )
                if (
                    normalized_scope_path is not None
                    and canonical_scope_source_root is not None
                )
                else None
            )
            provenance_relative = (
                _canonical_evidence_path(
                    ntpath.relpath(
                        normalized_session_path,
                        normalized_session_root,
                    )
                )
                if (
                    normalized_session_path is not None
                    and normalized_session_root is not None
                )
                else None
            )
            owner = provenance.get("owner")
            owner_file_sha = source_files_case.get(
                ntpath.normpath(str(owner))
                .replace("\\", "/")
                .casefold()
            )
            if (
                not embedded_virtual_component_under_root(
                    source_path,
                    scope_source_root,
                )
                or provenance.get("is_virtual") is not True
                or provenance.get("state")
                != "embedded_virtual_component"
                or provenance.get("kind")
                != "native_embedded_virtual_component"
                or provenance.get("owner") != scope["virtual_owner"]
                or provenance.get("owner_sha256")
                != scope["virtual_owner_sha256"]
                or owner_file_sha != scope["virtual_owner_sha256"]
                or provenance.get("occurrence") != name
                or scope_source_root is None
                or canonical_scope_source_root != normalized_scope_root
                or session_root_pid(scope["virtual_session_temp_root"])
                != scope["virtual_native_api_pid"]
                or session_root_pid(provenance.get("session_temp_root"))
                != provenance.get("native_api_pid")
                or scope_relative != provenance_relative
                or not embedded_virtual_component_under_root(
                    provenance.get("session_document_path"),
                    provenance.get("session_temp_root"),
                )
            ):
                raise PipelineError(
                    f"Virtual source owner/path/root differs: {name}"
                )
    return record


def components(root) -> list:
    masses = scene(root).get("mass_properties")
    if not isinstance(masses, dict) or not masses:
        raise PipelineError("Capture evidence carries no component readings")
    return [
        {"name": str(name), "mass_properties": reading}
        for name, reading in sorted(masses.items())
    ]


def mass_properties(root) -> dict:
    """Component-name to mass-property reading, as carried by the capture rows."""

    rows = components(root)
    readings = {}
    for row in rows:
        name = row["name"]
        reading = row["mass_properties"]
        if not isinstance(reading, dict):
            raise PipelineError(f"Capture component mass reading is malformed: {name}")
        readings[name] = reading
    if not readings:
        raise PipelineError("Component mass-property readings are missing")
    return readings


def datums(root) -> dict:
    """Datum name to 4x4 row-major matrix, as carried by the capture rows."""

    return {
        str(name): list(matrix)
        for name, matrix in scene(root)["coordinate_systems"].items()
    }


def _axis_reference(raw: dict) -> dict:
    errors = raw.get("read_errors")
    if not isinstance(errors, dict) or errors:
        raise PipelineError("raw reference-axis reading is incomplete")
    if raw.get("suppressed_raw") is not False:
        raise PipelineError("raw reference axis is suppressed or unread")
    if (
        type(raw.get("feature_error_code_raw")) is not int
        or raw["feature_error_code_raw"] != 0
    ):
        raise PipelineError("raw reference axis has a native feature error")
    endpoints = raw.get("endpoints_raw")
    if (
        not isinstance(endpoints, list)
        or len(endpoints) != 6
        or any(not _finite_number(value) for value in endpoints)
    ):
        raise PipelineError("raw reference-axis endpoints are invalid")
    values = [float(value) for value in endpoints]
    direction = [
        values[3] - values[0],
        values[4] - values[1],
        values[5] - values[2],
    ]
    length = math.hypot(*direction)
    if length <= 0.0 or not math.isfinite(length):
        raise PipelineError("raw reference-axis endpoints coincide")
    return {
        "kind": "reference_axis",
        "component": str(raw.get("component") or ""),
        "selector": copy.deepcopy(raw.get("selector") or {}),
        "coordinate_frame": str(raw.get("coordinate_frame") or ""),
        "direction_semantics": "undirected_axis_line",
        "raw_endpoints_m": values,
        "axis_point_m": values[:3],
        "axis_direction": [value / length for value in direction],
        "used_api": str(raw.get("used_api") or ""),
        **(
            {"feature_name": raw["feature_name"]}
            if isinstance(raw.get("feature_name"), str)
            and raw["feature_name"]
            else {}
        ),
    }


def _axis_cylinder(raw: dict) -> dict:
    errors = raw.get("read_errors")
    if not isinstance(errors, dict):
        raise PipelineError("raw cylinder-axis record has no error map")
    if errors:
        raise PipelineError("raw cylinder-axis selection is incomplete")
    candidates = raw.get("face_candidates")
    if not isinstance(candidates, list):
        raise PipelineError("raw cylinder-axis record has no candidates")
    cylindrical = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            raise PipelineError("raw cylinder-axis candidate is malformed")
        candidate_errors = candidate.get("read_errors")
        if not isinstance(candidate_errors, dict):
            raise PipelineError("raw cylinder candidate has no error map")
        if "surface" in candidate_errors or "is_cylinder" in candidate_errors:
            raise PipelineError("raw cylinder candidate type is unreadable")
        is_cylinder = candidate.get("is_cylinder")
        if type(is_cylinder) is not bool:
            raise PipelineError("raw cylinder candidate has no native boolean")
        if not is_cylinder:
            continue
        if "cylinder_params" in candidate_errors:
            raise PipelineError("raw cylinder candidate parameters are unreadable")
        cylindrical.append(candidate)
    if len(cylindrical) != 1:
        raise PipelineError(
            "raw axis selector does not identify exactly one cylindrical face"
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
        raise PipelineError("raw cylinder candidate identity is malformed")
    params = candidate.get("cylinder_params_raw")
    if (
        not isinstance(params, list)
        or len(params) != 7
        or any(not _finite_number(value) for value in params)
    ):
        raise PipelineError("raw cylinder parameters are invalid")
    values = [float(value) for value in params]
    length = math.hypot(*values[3:6])
    if abs(length - 1.0) > 1e-6 or values[6] <= 0.0:
        raise PipelineError("raw cylinder parameters are not a qualified line")
    result = {
        "kind": "cylinder",
        "component": str(raw.get("component") or ""),
        "body_type": str(raw.get("body_type_native") or ""),
        "selector": copy.deepcopy(raw.get("selector") or {}),
        "surface": "cylinder",
        "coordinate_frame": "component_local",
        "direction_semantics": "undirected_axis_line",
        "body_index": candidate.get("body_index"),
        "face_index": candidate.get("face_index"),
        "cylinder_params_raw": values,
        "is_cylinder_raw": True,
        "axis_point_m": values[:3],
        "axis_direction": [value / length for value in values[3:6]],
        "radius_m": values[6],
        "used_api": str(raw.get("used_api") or ""),
    }
    for key in ("feature_name", "persist_reference_b64"):
        if candidate.get(key) is not None:
            result[key] = candidate[key]
    return result


def axes(root) -> list:
    """Axis re-read records consumed by independent verification."""

    rows = _observation(root).get("axis_references_raw")
    if not isinstance(rows, list):
        raise PipelineError("Capture evidence carries no axis references")
    result = []
    names = set()
    for index, raw in enumerate(rows):
        if not isinstance(raw, dict):
            raise PipelineError(f"raw axis record {index} is malformed")
        name = raw.get("joint")
        if not isinstance(name, str) or not name or name in names:
            raise PipelineError(
                f"raw axis record {index} has no unique joint identity"
            )
        names.add(name)
        if raw.get("kind") == "reference_axis":
            projected = _axis_reference(raw)
        elif raw.get("kind") == "cylinder":
            projected = _axis_cylinder(raw)
        else:
            raise PipelineError(
                f"raw axis record {name} has an unsupported kind"
            )
        result.append({"joint": name, **projected})
    return result
