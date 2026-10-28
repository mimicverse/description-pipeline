"""Standard-library verification of sealed native capture facts."""

from __future__ import annotations

import copy
import math
from pathlib import Path

from ..geometry.native_scope import native_source_scope_is_valid
from ..geometry.native_transform import transform_from_solidworks
from ..io import PipelineError
from ..schemas import NATIVE_CAPTURE_OBSERVATION_KEYS
from ..sources.solidworks.inertia_convention import POSITIVE, detect_family
from . import capture_evidence as evidence


def _require(passed, message):
    if not bool(passed):
        raise PipelineError(message)


def _finite_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def _finite_vector3(value: object) -> bool:
    return isinstance(value, list) and len(value) == 3 and all(_finite_number(item) for item in value)


def _finite_matrix3(value: object) -> bool:
    return isinstance(value, list) and len(value) == 3 and all(_finite_vector3(row) for row in value)


def _raw_matrix(values: object, *, positive_products: bool, label: str) -> list[list[float]]:
    if (
        not isinstance(values, list)
        or len(values) != 9
        or any(not _finite_number(value) for value in values)
    ):
        raise PipelineError(f"{label}: raw inertia is not nine finite values")
    matrix = [[float(values[3 * row + column]) for column in range(3)] for row in range(3)]
    if positive_products:
        for row, column in ((0, 1), (0, 2), (1, 2)):
            matrix[row][column] = -matrix[row][column]
            matrix[column][row] = -matrix[column][row]
    return matrix


def _raw_measurement(
    raw: object,
    *,
    positive_products: bool,
    label: str,
) -> dict[str, object]:
    if not isinstance(raw, dict):
        raise PipelineError(f"{label}: raw mass reading is not an object")
    native = raw.get("native_raw")
    if not isinstance(native, dict):
        raise PipelineError(f"{label}: raw mass reading has no native values")
    mass = native.get("mass_kg")
    com = native.get("center_of_mass_m")
    values = native.get("inertia_values")
    if not _finite_number(mass) or float(mass) <= 0.0:
        raise PipelineError(f"{label}: raw mass is not finite and positive")
    if not _finite_vector3(com):
        raise PipelineError(f"{label}: raw COM is not a finite 3-vector")
    return {
        "mass": float(mass),
        "com": [float(value) for value in com],
        "inertia": _raw_matrix(values, positive_products=positive_products, label=label),
    }


def _same_number(left: object, right: object) -> bool:
    return _finite_number(left) and _finite_number(right) and float(left) == float(right)


def _same_vector(left: object, right: object) -> bool:
    return (
        isinstance(left, (list, tuple))
        and isinstance(right, (list, tuple))
        and len(left) == len(right)
        and all(_same_number(actual, expected) for actual, expected in zip(left, right, strict=True))
    )


def _same_matrix(left: object, right: object) -> bool:
    return (
        isinstance(left, list)
        and isinstance(right, list)
        and len(left) == len(right)
        and all(_same_vector(actual, expected) for actual, expected in zip(left, right, strict=True))
    )


def _compare_measurement(
    projected: object,
    expected: dict[str, object],
    *,
    label: str,
    mass_key: str = "mass",
    com_key: str = "com",
    inertia_key: str = "inertia",
) -> None:
    if not isinstance(projected, dict):
        raise PipelineError(f"{label}: projected reading is not an object")
    if not _same_number(projected.get(mass_key), expected["mass"]):
        raise PipelineError(f"{label}: projected mass differs from raw native facts")
    if not _same_vector(projected.get(com_key), expected["com"]):
        raise PipelineError(f"{label}: projected COM differs from raw native facts")
    if not _same_matrix(projected.get(inertia_key), expected["inertia"]):
        raise PipelineError(f"{label}: projected inertia differs from raw native facts")


def _compose_matrix(left: list[float], right: list[float]) -> list[float]:
    if len(left) != 16 or len(right) != 16:
        raise PipelineError("raw scene matrix composition requires two 4x4 matrices")
    result = []
    for row in range(4):
        for column in range(4):
            result.append(
                sum(
                    float(left[4 * row + index]) * float(right[4 * index + column])
                    for index in range(4)
                )
            )
    return result


def _raw_link_detector_record(raw: object) -> dict:
    if not isinstance(raw, dict) or not isinstance(raw.get("links"), dict):
        raise PipelineError("raw link-group facts carry no links")
    links = {}
    for name, row in raw["links"].items():
        if not isinstance(row, dict) or not isinstance(row.get("reads"), list):
            raise PipelineError(f"raw link-group {name} has no reads")
        reads = []
        for read in row["reads"]:
            if not isinstance(read, dict):
                raise PipelineError(f"raw link-group {name} has a malformed read")
            reads.append(
                {
                    "mass_kg": read.get("mass_kg"),
                    "com_m": read.get("com_m"),
                    "inertia_tensor_about_com": _raw_matrix(
                        read.get("inertia_values_raw"),
                        positive_products=False,
                        label=f"raw link-group {name}",
                    ),
                    "inertia_values_about_origin_api": read.get(
                        "inertia_values_about_origin_api"
                    ),
                    "read_errors": copy.deepcopy(read.get("read_errors")),
                }
            )
        links[str(name)] = {"reads": reads}
    return {"links": links}


def _verify_raw_scene(root: Path, raw_facts: dict) -> None:
    raw_scene = raw_facts.get("scene_raw")
    if not isinstance(raw_scene, dict):
        raise PipelineError("native capture facts carry no scene_raw object")
    projected = evidence.scene(root)
    if not isinstance(projected, dict):
        raise PipelineError("projected scene is not an object")
    raw_occurrences = raw_scene.get("occurrences")
    raw_components = raw_scene.get("components")
    if not isinstance(raw_occurrences, list) or not isinstance(raw_components, list):
        raise PipelineError("native scene facts carry no occurrence/component inventory")
    occurrences = {}
    for row in raw_occurrences:
        if not isinstance(row, dict) or not isinstance(row.get("name"), str):
            raise PipelineError("native scene occurrence has no identity")
        name = row["name"]
        if name in occurrences:
            raise PipelineError(f"native scene repeats occurrence {name}")
        if type(row.get("is_fixed")) is not bool:
            raise PipelineError(
                f"native scene occurrence {name} has no native fixed state"
            )
        if row.get("is_fixed_error") is not None:
            raise PipelineError(
                f"native scene occurrence {name} carries a contradictory fixed error"
            )
        transform = transform_from_solidworks(row.get("transform_raw"))
        occurrences[name] = (row, transform)
    projected_components = projected.get("components")
    if not isinstance(projected_components, list):
        raise PipelineError("projected scene has no component inventory")
    projected_by_name = {}
    for row in projected_components:
        if not isinstance(row, dict) or not isinstance(row.get("name"), str):
            raise PipelineError("projected scene component has no identity")
        name = row["name"]
        if name in projected_by_name:
            raise PipelineError(f"projected scene repeats component {name}")
        projected_by_name[name] = row
    raw_leaf_names = set()
    for row in raw_components:
        if not isinstance(row, dict) or not isinstance(row.get("name"), str):
            raise PipelineError("native leaf component has no identity")
        name = row["name"]
        raw_leaf_names.add(name)
        if type(row.get("is_fixed")) is not bool:
            raise PipelineError(
                f"native leaf {name} has no native fixed state"
            )
        if row.get("is_fixed_error") is not None:
            raise PipelineError(
                f"native leaf {name} carries a contradictory fixed error"
            )
        occurrence = occurrences.get(name)
        if occurrence is None:
            raise PipelineError(f"native leaf {name} has no occurrence transform")
        projected_row = projected_by_name.get(name)
        if not isinstance(projected_row, dict):
            raise PipelineError(f"projected scene is missing occurrence {name}")
        raw_row, expected_transform = occurrence
        if not _same_vector(projected_row.get("transform"), expected_transform):
            raise PipelineError(f"{name}: projected occurrence transform differs from raw16")
        for field in ("path", "document_type", "is_fixed"):
            if projected_row.get(field) != raw_row.get(field):
                raise PipelineError(f"{name}: projected occurrence {field} differs from raw facts")
    if set(projected_by_name) != raw_leaf_names:
        raise PipelineError("projected scene leaf inventory differs from raw component facts")
    raw_scopes = raw_facts.get("source_scopes")
    if not isinstance(raw_scopes, dict) or set(raw_scopes) != raw_leaf_names:
        raise PipelineError("raw source-scope inventory differs from native scene leaves")
    for name, scope in raw_scopes.items():
        if not native_source_scope_is_valid(scope, name):
            raise PipelineError(f"{name}: raw source scope is invalid")

    raw_datums = raw_scene.get("coordinate_systems")
    projected_datums = projected.get("coordinate_systems")
    if not isinstance(raw_datums, dict) or not isinstance(projected_datums, dict):
        raise PipelineError("native scene facts carry no coordinate-system inventory")
    if set(raw_datums) != set(projected_datums):
        raise PipelineError("projected coordinate-system inventory differs from raw facts")
    for key, datum in raw_datums.items():
        if not isinstance(datum, dict):
            raise PipelineError(f"{key}: raw coordinate system is malformed")
        read_errors = datum.get("read_errors")
        if type(read_errors) is not dict:
            raise PipelineError(f"{key}: raw coordinate system has no error map")
        if read_errors:
            raise PipelineError(f"{key}: raw coordinate system carries a read error")
        local = transform_from_solidworks(datum.get("array_data"))
        owner = datum.get("owner")
        if type(owner) is not str:
            raise PipelineError(f"{key}: raw coordinate-system owner is not a string")
        if owner:
            occurrence = occurrences.get(owner)
            if occurrence is None:
                raise PipelineError(f"{key}: raw coordinate-system owner is not an occurrence")
            local = _compose_matrix(occurrence[1], local)
        if not _same_vector(projected_datums.get(key), local):
            raise PipelineError(f"{key}: projected datum matrix differs from raw array data")


def _verify_raw_masses(root: Path, raw_facts: dict, *, positive_products: bool) -> None:
    raw_parts = raw_facts.get("part_mass_raw")
    projected = evidence.mass_properties(root)
    if not isinstance(raw_parts, dict) or not isinstance(projected, dict):
        raise PipelineError("native part mass facts are missing")
    if set(raw_parts) != set(projected):
        raise PipelineError("projected part mass inventory differs from raw facts")
    for name, raw in raw_parts.items():
        expected = _raw_measurement(
            raw,
            positive_products=positive_products,
            label=f"part {name}",
        )
        row = projected.get(name)
        _compare_measurement(row, expected, label=f"part {name}")
        if isinstance(raw, dict) and raw.get("part_measurement_raw") is not None:
            if not isinstance(row, dict) or "part_measurement" not in row:
                raise PipelineError(f"part {name}: projected part measurement is missing")
            _compare_measurement(
                row["part_measurement"],
                expected,
                label=f"part {name} measurement",
                mass_key="mass_kg",
                com_key="center_of_mass_m",
                inertia_key="inertia_kg_m2",
            )

    assembly_raw = raw_facts.get("assembly_mass_raw")
    closure = evidence.mass_closure(root)
    if not isinstance(closure, dict):
        raise PipelineError("projected mass closure is missing")
    top = closure.get("top_level")
    if assembly_raw is None:
        if top is not None:
            raise PipelineError("projected assembly mass exists without raw assembly facts")
    else:
        expected = _raw_measurement(
            assembly_raw,
            positive_products=positive_products,
            label="assembly",
        )
        _compare_measurement(top, expected, label="assembly")
        context = closure.get("component_context")
        if isinstance(context, dict) and not _same_number(
            context.get("assembly_mass_kg"), expected["mass"]
        ):
            raise PipelineError("projected component context assembly mass differs from raw facts")


def _verify_raw_effective(root: Path, raw_facts: dict, *, positive_products: bool) -> None:
    raw_component = raw_facts.get("component_mass_raw")
    closure = evidence.mass_closure(root)
    record = evidence.effective_instances(root)
    scene = evidence.scene(root)
    if not isinstance(raw_component, dict):
        raise PipelineError("native component mass facts are missing")
    if not isinstance(record, dict) or not isinstance(record.get("readings"), dict):
        raise PipelineError("projected effective-instance readings are missing")
    readings = record["readings"]
    notes = scene.get("notes") if isinstance(scene, dict) else None
    mirrored = notes.get("effective_instances") if isinstance(notes, dict) else None
    if mirrored != readings:
        raise PipelineError("projected effective-instance readings differ from scene notes")
    context = closure.get("component_context") if isinstance(closure, dict) else None
    if not isinstance(context, dict):
        raise PipelineError("projected component context is missing")
    projected_instances = {
        str(row.get("name")): row
        for row in context.get("instances") or []
        if isinstance(row, dict)
    }
    raw_instances = raw_component.get("instances")
    if not isinstance(raw_instances, list):
        raise PipelineError("raw component context has no instances")
    if set(projected_instances) != {
        str(row.get("name")) for row in raw_instances if isinstance(row, dict)
    }:
        raise PipelineError("projected component context inventory differs from raw facts")
    if context.get("errors") != raw_component.get("errors"):
        raise PipelineError("projected component context errors differ from raw facts")
    for raw in raw_instances:
        if not isinstance(raw, dict) or not isinstance(raw.get("name"), str):
            raise PipelineError("raw component context instance is malformed")
        name = raw["name"]
        projected = projected_instances.get(name)
        if not isinstance(projected, dict):
            raise PipelineError(f"component context {name} is missing from projection")
        for field in ("name", "parent", "depth", "document", "document_type"):
            if projected.get(field) != raw.get(field):
                raise PipelineError(f"component context {name} {field} differs from raw facts")
        if not _same_number(projected.get("context_mass_kg"), raw.get("context_mass_kg")):
            raise PipelineError(f"component context {name} mass differs from raw facts")
        raw_volume = raw.get("context_volume_m3")
        projected_volume = projected.get("context_volume_m3")
        if (raw_volume is None) != (projected_volume is None) or (
            raw_volume is not None and not _same_number(projected_volume, raw_volume)
        ):
            raise PipelineError(f"component context {name} volume differs from raw facts")
        if projected.get("overrides") != raw.get("overrides"):
            raise PipelineError(f"component context {name} overrides differ from raw facts")
        effective = raw.get("effective")
        if effective is not None:
            expected = _raw_measurement(
                effective,
                positive_products=positive_products,
                label=f"effective instance {name}",
            )
            if not isinstance(readings.get(name), dict):
                raise PipelineError(f"effective instance {name} is missing from projection")
            _compare_measurement(readings.get(name), expected, label=f"effective instance {name}")
            projected_effective = projected.get("effective")
            if not isinstance(projected_effective, dict):
                raise PipelineError(f"component context {name} effective reading is missing")
            _compare_measurement(
                projected_effective,
                expected,
                label=f"component context effective instance {name}",
            )
            part_raw = effective.get("part_measurement_raw")
            if part_raw is not None:
                nested = _raw_measurement(
                    part_raw,
                    positive_products=positive_products,
                    label=f"effective instance {name} part measurement",
                )
                if "part_measurement" not in readings[name]:
                    raise PipelineError(f"effective instance {name} lacks projected part measurement")
                _compare_measurement(
                    readings[name]["part_measurement"],
                    nested,
                    label=f"effective instance {name} part measurement",
                    mass_key="mass_kg",
                    com_key="center_of_mass_m",
                    inertia_key="inertia_kg_m2",
                )
        elif name in readings:
            raise PipelineError(f"effective instance {name} is projected without raw values")


def _verify_raw_links(root: Path, raw_facts: dict, *, positive_products: bool) -> None:
    raw = raw_facts.get("link_group_mass_raw")
    projected = evidence.link_group_mass_properties(root)
    if not isinstance(raw, dict) or not isinstance(projected, dict):
        raise PipelineError("native link-group mass facts are missing")
    for field in ("assembly", "configuration", "used_api", "selection_rule"):
        if projected.get(field) != raw.get(field):
            raise PipelineError(f"projected link-group {field} differs from raw facts")
    raw_links = raw.get("links")
    projected_links = projected.get("links")
    if not isinstance(raw_links, dict) or not isinstance(projected_links, dict):
        raise PipelineError("link-group links are missing")
    if set(raw_links) != set(projected_links):
        raise PipelineError("projected link-group inventory differs from raw facts")
    for name, raw_link in raw_links.items():
        if not isinstance(raw_link, dict) or not isinstance(projected_links.get(name), dict):
            raise PipelineError(f"link-group {name} is malformed")
        if projected_links[name].get("selection") != raw_link.get("selection"):
            raise PipelineError(f"link-group {name} selection differs from raw facts")
        raw_reads = raw_link.get("reads")
        projected_reads = projected_links[name].get("reads")
        if not isinstance(raw_reads, list) or not isinstance(projected_reads, list):
            raise PipelineError(f"link-group {name} reads are missing")
        if len(raw_reads) != len(projected_reads):
            raise PipelineError(f"link-group {name} read count differs from raw facts")
        expected_reads = []
        for index, (raw_read, projected_read) in enumerate(zip(raw_reads, projected_reads, strict=True)):
            if not isinstance(raw_read, dict):
                raise PipelineError(f"link-group {name} read {index} is malformed")
            expected = {
                "mass": float(raw_read["mass_kg"]),
                "com": [float(value) for value in raw_read["com_m"]],
                "inertia": _raw_matrix(
                    raw_read.get("inertia_values_raw"),
                    positive_products=positive_products,
                    label=f"link-group {name} read {index}",
                ),
            }
            _compare_measurement(
                projected_read,
                expected,
                label=f"link-group {name} read {index}",
                mass_key="mass_kg",
                com_key="com_m",
                inertia_key="inertia_tensor_about_com",
            )
            raw_origin = raw_read.get("inertia_values_about_origin_api")
            projected_origin = projected_read.get("inertia_values_about_origin_api")
            raw_read_errors = raw_read.get("read_errors")
            if not isinstance(raw_read_errors, dict):
                raise PipelineError(
                    f"link-group {name} read {index} has no error map"
                )
            recorded_origin_error = raw_read_errors.get(
                "inertia_values_about_origin_api"
            )
            if raw_origin is None:
                if recorded_origin_error is not None:
                    raise PipelineError(
                        f"link-group {name} read {index} origin error has no matching origin value"
                    )
                if projected_origin is not None:
                    raise PipelineError(f"link-group {name} read {index} invents an origin tensor")
            elif isinstance(raw_origin, dict):
                if (
                    not isinstance(raw_origin.get("code"), str)
                    or not raw_origin["code"].strip()
                ):
                    raise PipelineError(
                        f"link-group {name} read {index} origin error is malformed"
                    )
                if recorded_origin_error != raw_origin:
                    raise PipelineError(
                        f"link-group {name} read {index} origin error differs from its read-error entry"
                    )
                if projected_origin != raw_origin:
                    raise PipelineError(
                        f"link-group {name} read {index} origin error differs from raw facts"
                    )
            else:
                if recorded_origin_error is not None:
                    raise PipelineError(
                        f"link-group {name} read {index} origin error conflicts with a recorded tensor"
                    )
                if not isinstance(raw_origin, list) or len(raw_origin) != 9:
                    raise PipelineError(f"link-group {name} read {index} origin tensor is malformed")
                expected_origin = [float(value) for value in raw_origin]
                if positive_products:
                    for offset in (1, 2, 3, 5, 6, 7):
                        expected_origin[offset] = -expected_origin[offset]
                if not _same_vector(projected_origin, expected_origin):
                    raise PipelineError(f"link-group {name} read {index} origin tensor differs from raw facts")
            expected_reads.append(expected)
        if len(expected_reads) >= 2:
            reproducibility = projected_links[name].get("reproducibility")
            if not isinstance(reproducibility, dict):
                raise PipelineError(f"link-group {name} reproducibility is missing")
            if not _same_number(
                reproducibility.get("mass_delta_kg"),
                abs(expected_reads[0]["mass"] - expected_reads[1]["mass"]),
            ):
                raise PipelineError(f"link-group {name} mass reproducibility differs from raw facts")
            if not _same_number(
                reproducibility.get("com_delta_norm_m"),
                math.dist(expected_reads[0]["com"], expected_reads[1]["com"]),
            ):
                raise PipelineError(f"link-group {name} COM reproducibility differs from raw facts")
            tensor_delta = max(
                abs(expected_reads[0]["inertia"][row][column] - expected_reads[1]["inertia"][row][column])
                for row in range(3)
                for column in range(3)
            )
            if not _same_number(reproducibility.get("tensor_delta_max_abs"), tensor_delta):
                raise PipelineError(f"link-group {name} tensor reproducibility differs from raw facts")


def verify_native_capture_facts(bundle_root: Path) -> dict:
    """Verify projected scene/mass records against sealed pre-projection facts."""

    root = Path(bundle_root)
    raw_facts = evidence.native_capture_facts(root)
    if (
        not isinstance(raw_facts, dict)
        or set(raw_facts) != NATIVE_CAPTURE_OBSERVATION_KEYS
    ):
        raise PipelineError("native_capture_facts has an unknown field set")
    try:
        raw_family = detect_family(_raw_link_detector_record(raw_facts.get("link_group_mass_raw")))["family"]
    except Exception as error:
        raise PipelineError(f"raw native link-group inertia convention is unverified: {error}") from error
    applied = raw_family == POSITIVE
    _verify_raw_scene(root, raw_facts)
    _verify_raw_masses(root, raw_facts, positive_products=applied)
    _verify_raw_effective(root, raw_facts, positive_products=applied)
    _verify_raw_links(root, raw_facts, positive_products=applied)
    return {
        "native_capture_facts": True,
        "inertia_family": raw_family,
        "inertia_transform_applied": applied,
    }
