"""Independently verify native face appearance and its delivered partition."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from ..geometry.native_tessellation import native_stl_subset
from ..io import PipelineError, confined, file_digest
from ..sources.solidworks.naming import (
    asset_namespace,
    material_name,
    mesh_path,
    visual_name,
)

IMODELDOC2_IID = "{B90793FB-EF3D-4B80-A5C4-99959CDB6CEB}"
DOCUMENT_BINDING_API = (
    "IModelDoc2.GetPathName"
    "+ConfigurationManager.ActiveConfiguration.Name"
)
OWNER_CONTEXT_BINDING_API = "IModelDocExtension.GetPersistReference3"
APPEARANCE_SCOPE_ORDER = (
    "occurrence",
    "face",
    "feature",
    "body",
    "document",
)


def _require(condition: object, message: str) -> None:
    if not condition:
        raise PipelineError(message)


def _face_key(body_type: object, body_index: object, face_index: object):
    if (
        type(body_type) is not int
        or body_type not in (0, 1)
        or type(body_index) is not int
        or body_index < 0
        or type(face_index) is not int
        or face_index < 0
    ):
        raise PipelineError("Native appearance has an invalid face key")
    return (body_type, body_index, face_index)


def _persistent_reference_key(value: object) -> tuple[int, ...] | None:
    if (
        not isinstance(value, list)
        or not value
        or any(
            type(item) is not int or not -128 <= item <= 255
            for item in value
        )
    ):
        return None
    return tuple(item & 0xFF for item in value)


def _document_path_key(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return value.replace("\\", "/").casefold()


def _binding_context_key(value: object):
    _require(isinstance(value, Mapping), "A native binding context is malformed")
    owner_scope = value.get("owner_scope")
    document_path = _document_path_key(value.get("owner_document_path"))
    component = value.get("component")
    _require(
        isinstance(owner_scope, str)
        and bool(owner_scope)
        and document_path is not None
        and isinstance(component, str)
        and bool(component),
        "A native binding context is incomplete",
    )
    return owner_scope, document_path, component


def _unique_current_targets(targets: Sequence[Mapping], *, component: str):
    current = {}
    for target in targets:
        target_component = target.get("component")
        _require(
            isinstance(target_component, str) and bool(target_component),
            "A native reference target has no exact component",
        )
        if target_component != component:
            continue
        kind = target["kind"]
        footprint = None
        if kind == "occurrence":
            identity = ("occurrence", component, 0, 0, 0)
        elif kind == "feature":
            feature_identity = _feature_target_identity(target)
            _require(
                isinstance(target.get("faces"), list),
                "A native feature target has no footprint",
            )
            for face in target["faces"]:
                _require(
                    isinstance(face, Mapping)
                    and _face_key(
                        face.get("body_type"),
                        face.get("body_index"),
                        face.get("face_index"),
                    )
                    is not None,
                    "A native feature footprint has an invalid face",
                )
            identity = (
                "feature",
                component,
                0,
                0,
                0,
                feature_identity,
            )
            footprint = tuple(
                sorted(
                    (
                        face["body_type"],
                        face["body_index"],
                        face["face_index"],
                    )
                    for face in target["faces"]
                )
            )
        else:
            face_key = _face_key(
                target.get("body_type"),
                target.get("body_index"),
                target.get("face_index", 0),
            )
            identity = (
                kind,
                component,
                face_key[0],
                face_key[1],
                face_key[2],
            )
        if identity in current:
            if footprint is not None:
                previous = current[identity]
                previous_footprint = tuple(
                    sorted(
                        (
                            face["body_type"],
                            face["body_index"],
                            face["face_index"],
                        )
                        for face in previous["faces"]
                    )
                )
                _require(
                    previous_footprint == footprint,
                    "One native feature identity carries conflicting face footprints",
                )
            continue
        current[identity] = target
    return current


def _mapping_errors_for_targets(
    mapping_errors: Sequence[object],
    targets: Mapping[tuple[Any, ...], Mapping],
    *,
    owner_scope: str,
    owner_document_path: str,
    component: str,
) -> list[object]:
    def complete_face(value: object) -> bool:
        return (
            isinstance(value, Mapping)
            and type(value.get("body_type")) is int
            and value["body_type"] in (0, 1)
            and type(value.get("body_index")) is int
            and value["body_index"] >= 0
            and type(value.get("face_index")) is int
            and value["face_index"] >= 0
        )

    applicable = []
    for error in mapping_errors:
        if not isinstance(error, Mapping) or not isinstance(
            error.get("identity"), Mapping
        ):
            applicable.append(error)
            continue
        error_document_path = _document_path_key(
            error.get("owner_document_path")
        )
        if (
            not isinstance(error.get("owner_scope"), str)
            or not error["owner_scope"]
            or not isinstance(error.get("component"), str)
            or not error["component"]
            or error_document_path is None
        ):
            applicable.append(error)
            continue
        if (
            error["owner_scope"] != owner_scope
            or error["component"] != component
            or error_document_path
            != _document_path_key(owner_document_path)
        ):
            continue
        identity = error["identity"]
        if (
            identity.get("component") != component
            or error.get("target_kind")
            not in {"occurrence", "body", "face", "feature"}
        ):
            applicable.append(error)
            continue
        target_kind = error["target_kind"]
        expected_entity_kind = {
            "occurrence": "occurrences",
            "body": "bodies",
            "face": "faces",
            "feature": "features",
        }[target_kind]
        kind = error.get("entity_kind")
        if kind != expected_entity_kind:
            applicable.append(error)
            continue
        if kind == "faces" and not complete_face(identity):
            applicable.append(error)
            continue
        if kind == "bodies" and not all(
            type(identity.get(field)) is int
            and (
                identity[field] in (0, 1)
                if field == "body_type"
                else identity[field] >= 0
            )
            for field in ("body_type", "body_index")
        ):
            applicable.append(error)
            continue
        error_faces = identity.get("faces")
        if kind == "features" and (
            not isinstance(identity.get("name"), str)
            or not identity["name"]
            or not isinstance(identity.get("type_name"), str)
            or not identity["type_name"]
            or not isinstance(error_faces, list)
            or not error_faces
            or any(not complete_face(face) for face in error_faces)
        ):
            applicable.append(error)
            continue
        matches = False
        for target in targets.values():
            target_kind = target.get("kind")
            if identity.get("component") != target.get("component"):
                continue
            if kind == "faces" and target_kind == "face":
                matches = all(
                    identity.get(field) == target.get(field)
                    for field in ("body_type", "body_index", "face_index")
                )
            elif kind == "bodies" and target_kind == "body":
                matches = all(
                    identity.get(field) == target.get(field)
                    for field in ("body_type", "body_index")
                )
            elif kind == "features" and target_kind == "feature":
                feature_identity = _feature_target_identity(target)
                same_name_type = (
                    identity.get("name") == target.get("name")
                    and identity.get("type_name") == target.get("type_name")
                )
                error_reference = _persistent_reference_key(
                    identity.get("persistent_reference")
                )
                target_reference = feature_identity[-1]
                error_footprint = {
                    (
                        face["body_type"],
                        face["body_index"],
                        face["face_index"],
                    )
                    for face in error_faces
                }
                target_footprint = {
                    (
                        face["body_type"],
                        face["body_index"],
                        face["face_index"],
                    )
                    for face in target.get("faces", ())
                    if complete_face(face)
                }
                matches = (
                    (
                        same_name_type
                        and error_reference is not None
                        and error_reference == target_reference
                    )
                    or bool(error_footprint & target_footprint)
                    or (
                        same_name_type
                        and (
                            error_reference is None
                            or target_reference is None
                        )
                    )
                )
            elif kind == "occurrences" and target_kind == "occurrence":
                matches = True
            elif kind == "faces" and target_kind == "feature":
                matches = any(
                    all(
                        identity.get(field) == face.get(field)
                        for field in ("body_type", "body_index", "face_index")
                    )
                    for face in target.get("faces", ())
                    if isinstance(face, Mapping)
                )
            if matches:
                break
        if matches:
            applicable.append(error)
    return applicable


def _feature_target_identity(target: Mapping):
    name = target.get("name")
    type_name = target.get("type_name")
    _require(
        isinstance(name, str)
        and bool(name)
        and isinstance(type_name, str)
        and bool(type_name),
        "A feature target has no native feature identity",
    )
    persistent_identity = target.get("persistent_identity")
    if persistent_identity is None:
        persistent_identity = target.get("persistent_reference")
    if isinstance(persistent_identity, list):
        persistent_identity = tuple(persistent_identity)
    elif isinstance(persistent_identity, Mapping):
        persistent_identity = tuple(
            sorted(
                (str(key), str(value))
                for key, value in persistent_identity.items()
            )
        )
    elif persistent_identity is not None and type(
        persistent_identity
    ) not in (int, str):
        raise PipelineError("A feature target identity is not canonical")
    return name, type_name, persistent_identity


def _valid_values(value: object) -> bool:
    return (
        isinstance(value, list)
        and len(value) == 9
        and all(
            type(item) in (int, float) and math.isfinite(float(item))
            for item in value
        )
    )


def _owner_rgba(
    raw: object,
    *,
    configuration: str,
    where: str,
    use_presence: bool = True,
):
    _require(isinstance(raw, Mapping), f"{where} is not an owner record")
    errors = raw.get("read_errors")
    _require(
        isinstance(errors, Mapping)
        and not {
            key: value
            for key, value in errors.items()
            if use_presence or key != "has_material_property_values"
        },
        f"{where} carries an appearance read error",
    )
    call = raw.get("material_call")
    names = call.get("configuration_names") if isinstance(call, Mapping) else None
    active = call.get("active_configuration") if isinstance(call, Mapping) else None
    _require(
        active == configuration
        or (isinstance(names, list) and configuration in names),
        f"{where} is bound to another configuration",
    )
    texture = raw.get("texture")
    _require(
        isinstance(texture, Mapping)
        and texture.get("state") == "absent"
        and texture.get("material_name") is None,
        f"{where} carries an unqualified appearance texture",
    )
    values = raw.get("material_property_values")
    has_values = raw.get("has_material_property_values")
    if values is None:
        _require(
            not use_presence
            or type(has_values) is bool
            or has_values is None,
            f"{where} has a malformed appearance presence flag",
        )
        return None
    _require(_valid_values(values), f"{where} has no nine-value material array")
    if all(float(value) == -1.0 for value in values):
        return None
    _require(
        not use_presence
        or (type(has_values) is bool and has_values is True),
        f"{where} carries material values without a true presence flag",
    )
    _require(
        all(0 <= float(value) <= 1 for value in values),
        f"{where} carries material values outside [0, 1]",
    )
    rgba = (
        float(values[0]),
        float(values[1]),
        float(values[2]),
        1.0 - float(values[7]),
    )
    return rgba, {
        "kind": "owner",
        "used_api": raw.get("used_api"),
        "material_property_values": list(values),
        "rgba": list(rgba),
        "alpha_rule": {
            "rule_id": "appearance.urdf_opacity_from_native_transparency",
            "rule_version": "v1",
            "native_transparency_index": 7,
            "urdf_alpha": 1.0 - float(values[7]),
        },
    }


def _state_owner_display_state(
    owner: Mapping,
    *,
    expected_configuration: str,
    expected_display_state: str,
    where: str,
) -> str:
    scope = owner.get("owner_scope")
    configuration = owner.get("configuration")
    owner_state = owner.get("display_state")
    display_states = owner.get("display_states")
    display_states_state = owner.get("display_states_state")
    _require(
        isinstance(scope, str)
        and bool(scope)
        and isinstance(configuration, str)
        and bool(configuration)
        and isinstance(owner_state, str)
        and bool(owner_state),
        f"{where} has no exact native configuration and state",
    )
    if scope == "top_assembly":
        proven = (
            display_states_state == "observed"
            and isinstance(display_states, list)
            and bool(display_states)
            and display_states[0] == owner_state
        )
    elif scope == "parent_assembly":
        proven = (
            display_states_state == "not_applicable"
            and display_states is None
        )
    elif scope == "component_document":
        proven = (
            configuration == expected_configuration
            and owner_state == expected_display_state
            and display_states_state == "not_applicable"
            and display_states is None
        )
    else:
        proven = False
    _require(
        proven,
        f"{where} is not proven from its own native configuration and state",
    )
    return owner_state


def _target_faces(
    target: object,
    all_faces: set[tuple[int, int, int]],
) -> set[tuple[int, int, int]]:
    _require(isinstance(target, Mapping), "A display-state appearance target is malformed")
    scope = target.get("scope")
    if scope == "occurrence":
        return set(all_faces)
    if scope == "body":
        key = _face_key(target.get("body_type"), target.get("body_index"), 0)
        return {face for face in all_faces if face[:2] == key[:2]}
    if scope == "face":
        key = _face_key(
            target.get("body_type"),
            target.get("body_index"),
            target.get("face_index"),
        )
        _require(key in all_faces, "A face appearance target is not a delivered face")
        return {key}
    if scope == "feature":
        source_owner = target.get("source_owner")
        face_keys = target.get("face_keys")
        _require(
            isinstance(source_owner, Mapping)
            and isinstance(face_keys, list)
            and bool(face_keys),
            "A feature appearance target has no native owner or face binding",
        )
        result = set()
        for face in face_keys:
            _require(isinstance(face, Mapping), "A feature face key is malformed")
            key = _face_key(
                face.get("body_type"),
                face.get("body_index"),
                face.get("face_index"),
            )
            _require(key in all_faces, "A feature face key is not a delivered face")
            result.add(key)
        return result
    raise PipelineError("A display-state appearance target has an unknown scope")


def _colorref(value: object, transparency: object):
    _require(
        type(value) is int
        and 0 <= value <= 0xFFFFFF
        and type(transparency) in (int, float)
        and math.isfinite(float(transparency))
        and 0 <= float(transparency) <= 1,
        "A display-state appearance color is malformed",
    )
    return (
        (value & 0xFF) / 255.0,
        ((value >> 8) & 0xFF) / 255.0,
        ((value >> 16) & 0xFF) / 255.0,
        1.0 - float(transparency),
    )


def _resolved_faces(
    raw: object,
    *,
    configuration: str,
    where: str,
    expected_component: str | None = None,
    expected_document_path: str | None = None,
) -> dict:
    _require(
        isinstance(raw, Mapping)
        and isinstance(raw.get("component"), str)
        and bool(raw.get("component"))
        and raw.get("referenced_configuration") == configuration
        and raw.get("native_active_configuration") == configuration
        and raw.get("native_active_configuration_error") is None,
        f"{where} is not bound to the referenced occurrence and configuration",
    )
    component = str(raw["component"])
    _require(
        expected_component is None or component == expected_component,
        f"{where} names another component",
    )
    _require(
        expected_document_path is None
        or _document_path_key(raw.get("source_document_path"))
        == _document_path_key(expected_document_path),
        f"{where} names another source document",
    )
    source_document_path = expected_document_path or raw.get(
        "source_document_path"
    )
    _require(
        _document_path_key(source_document_path) is not None,
        f"{where} has no source document identity",
    )
    display_state = raw.get("native_referenced_display_state")
    _require(
        isinstance(display_state, str)
        and bool(display_state)
        and raw.get("native_referenced_display_state_error") is None,
        f"{where} has no readable referenced display state",
    )
    bodies = raw.get("bodies")
    _require(isinstance(bodies, list) and bodies, f"{where} has no body inventory")
    body_owners = {}
    face_owners = {}
    all_faces = set()
    for body_index, body in enumerate(bodies):
        _require(isinstance(body, Mapping), f"{where}.bodies[{body_index}] is malformed")
        key = _face_key(body.get("body_type"), body.get("body_index"), 0)
        body_key = key[:2]
        _require(body_key not in body_owners, f"{where} repeats a body appearance owner")
        body_owners[body_key] = body.get("appearance")
        rows = body.get("faces")
        _require(
            isinstance(rows, list) and rows,
            f"{where}.bodies[{body_index}] has no face inventory",
        )
        face_owners[body_key] = {}
        for _row_index, row in enumerate(rows):
            _require(isinstance(row, Mapping), "A native face appearance record is malformed")
            face = _face_key(body_key[0], body_key[1], row.get("face_index"))
            _require(face not in all_faces, f"{where} repeats face {face}")
            all_faces.add(face)
            face_owners[body_key][face] = row.get("appearance")
    occurrence = raw.get("occurrence")
    document = raw.get("document")

    owner_records = raw.get("state_render_materials")
    _require(
        isinstance(owner_records, list) and owner_records,
        f"{where} has no display-state owner records",
    )
    state_materials = []
    for owner_index, owner in enumerate(owner_records):
        owner_path = f"{where}.state_render_materials[{owner_index}]"
        _require(
            isinstance(owner, Mapping)
            and owner.get("owner_scope")
            in {"top_assembly", "parent_assembly", "component_document"}
            and isinstance(owner.get("owner_document_path"), str)
            and owner.get("api") == "IModelDocExtension.GetRenderMaterials2"
            and owner.get("display_state_option") == 3
            and owner.get("call_state")
            in {"observed", "empty", "null_result"}
            and isinstance(owner.get("read_errors"), Mapping)
            and not owner.get("read_errors")
            and isinstance(owner.get("mapping_errors"), list)
            and isinstance(owner.get("native_reference_index"), list)
            and isinstance(owner.get("materials"), (list, type(None)))
            and owner.get("native_render_material_count_api")
            == "IModelDocExtension.GetRenderMaterialsCount2"
            and (
                (
                    owner.get("native_render_material_count") is None
                    and isinstance(
                        owner.get("native_render_material_count_error"),
                        Mapping,
                    )
                )
                or (
                    type(owner.get("native_render_material_count")) is int
                    and owner.get("native_render_material_count") >= 0
                    and owner.get("native_render_material_count_error") is None
                )
            ),
            f"{owner_path} has no successful exact display-state enumeration",
        )
        _require(
            owner.get("owner_scope") != "component_document"
            or _document_path_key(owner.get("owner_document_path"))
            == _document_path_key(source_document_path),
            f"{owner_path} names another component-document owner",
        )
        owner_display_state = _state_owner_display_state(
            owner,
            expected_configuration=configuration,
            expected_display_state=display_state,
            where=owner_path,
        )
        zero_material_null = (
            owner.get("call_state") == "null_result"
            and owner.get("native_render_material_count") == 0
            and owner.get("native_render_material_count_error") is None
            and owner.get("materials") is None
        )
        if zero_material_null:
            continue
        _require(
            (owner["call_state"] == "empty" and not owner["materials"])
            or (owner["call_state"] == "observed" and bool(owner["materials"])),
            f"{owner_path} call state and material rows disagree",
        )
        _require(
            (
                owner["call_state"] == "observed"
                and owner["native_render_material_count"]
                == len(owner["materials"])
            )
            or (
                owner["call_state"] == "empty"
                and owner["native_render_material_count"] == 0
            ),
            f"{owner_path} native material count disagrees with material rows",
        )
        reference_index = {}
        for index_position, indexed in enumerate(
            owner["native_reference_index"]
        ):
            index_path = (
                owner_path
                + f".native_reference_index[{index_position}]"
            )
            _require(
                isinstance(indexed, Mapping),
                f"{index_path} is malformed",
            )
            indexed_key = _persistent_reference_key(
                indexed.get("persistent_reference")
            )
            indexed_binding = indexed.get("binding_context")
            indexed_context = _binding_context_key(indexed_binding)
            indexed_errors = indexed.get("read_errors")
            target = indexed.get("target")
            _require(
                indexed_key is not None
                and indexed_context is not None
                and isinstance(indexed_binding, Mapping)
                and indexed_binding.get("api")
                == OWNER_CONTEXT_BINDING_API
                and indexed.get("native_interface_kind")
                in {"IEntity", None}
                and isinstance(indexed_errors, Mapping)
                and isinstance(target, Mapping)
                and target.get("kind")
                in {"occurrence", "body", "face", "feature"}
                and (
                    owner.get("owner_scope") == "component_document"
                    or target.get("kind") == "occurrence"
                ),
                f"{index_path} assembly-owned material target is not an occurrence",
            )
            reference_index.setdefault(
                (indexed_key, indexed_context),
                [],
            ).append(
                {
                    "target": target,
                    "read_errors": indexed_errors,
                }
            )
        for index, material in enumerate(owner["materials"]):
            material_path = f"{owner_path}.materials[{index}]"
            _require(
                isinstance(material, Mapping)
                and material.get("material_index") == index
                and isinstance(material.get("read_errors"), Mapping)
                and isinstance(
                    material.get("linked_display_states"),
                    (list, type(None)),
                )
                and isinstance(material.get("entities"), list)
                and isinstance(material.get("entity_records"), list)
                and material.get("entities_state")
                in {"observed", "empty", "null_result", "error"}
                ,
                f"{material_path} is malformed",
            )
            _require(
                material["entities_state"] not in {"null_result", "error"},
                f"{material_path} has no readable entity inventory",
            )
            if material["entities_state"] == "empty":
                continue
            _require(
                bool(material["entity_records"]),
                f"{material_path} has no entity records",
            )
            faces_by_scope = {
                "occurrence": set(),
                "face": set(),
                "feature": set(),
                "body": set(),
                "document": set(),
            }
            for entity_index, entity in enumerate(material["entity_records"]):
                entity_path = f"{material_path}.entities[{entity_index}]"
                _require(
                    isinstance(entity, Mapping)
                    and entity.get("entity_index") == entity_index
                    and isinstance(entity.get("native_object_type"), str)
                    and entity.get("native_interface_kind")
                    in {"IModelDoc2", "IEntity", None}
                    and isinstance(entity.get("read_errors"), Mapping),
                    f"{entity_path} is malformed",
                )
                context_key = _binding_context_key(
                    entity.get("binding_context")
                )
                _require(
                    context_key[0] == owner.get("owner_scope")
                    and context_key[1]
                    == _document_path_key(owner.get("owner_document_path"))
                    and context_key[2] == component,
                    f"{entity_path} binding context is not its actual owner",
                )
                native_identity = entity.get("native_identity")
                document_identity = (
                    native_identity
                    if isinstance(native_identity, Mapping)
                    and native_identity.get("kind") == "document"
                    else None
                )
                if document_identity is not None:
                    _require(
                        not entity["read_errors"]
                        and context_key[0] == "component_document"
                        and _document_path_key(
                            entity["binding_context"].get(
                                "owner_document_path"
                            )
                        )
                        == _document_path_key(owner.get("owner_document_path"))
                        and entity["binding_context"].get("api")
                        == DOCUMENT_BINDING_API
                        and document_identity.get("qi_iid")
                        == IMODELDOC2_IID
                        and document_identity.get("state") == "observed"
                        and not document_identity.get("read_errors")
                        and _document_path_key(
                            document_identity.get("document_path")
                        )
                        == _document_path_key(raw.get("source_document_path"))
                        and document_identity.get("active_configuration")
                        == configuration,
                        f"{entity_path} document identity is not the current leaf",
                    )
                    current_targets = {
                        ("document", component, 0, 0, 0): {}
                    }
                else:
                    entity_key = _persistent_reference_key(
                        entity.get("persistent_reference")
                    )
                    owner_context = {
                        "owner_scope": owner["owner_scope"],
                        "owner_document_path": owner["owner_document_path"],
                        "component": component,
                        "api": OWNER_CONTEXT_BINDING_API,
                    }
                    source_context_proven = (
                        entity_key is not None
                        and not entity["read_errors"]
                        and _binding_context_key(entity["binding_context"])
                        == _binding_context_key(owner_context)
                        and entity["binding_context"].get("api")
                        == OWNER_CONTEXT_BINDING_API
                        and native_identity is None
                    )
                    _require(
                        source_context_proven,
                        f"{entity_path} is not bound to its actual owner document",
                    )
                    targets = reference_index.get(
                        (entity_key, context_key)
                    )
                    _require(
                        bool(targets),
                        f"{entity_path} has no native reference-index binding; "
                        f"mapping_errors={json.dumps(owner['mapping_errors'], sort_keys=True)}",
                    )
                    _require(
                        not any(
                            item["read_errors"]
                            and set(item["read_errors"])
                            != {"interface_query"}
                            for item in targets
                        ),
                        f"{entity_path} matched native target has unresolved "
                        "identity evidence",
                    )
                    current_targets = _unique_current_targets(
                        [item["target"] for item in targets],
                        component=component,
                    )
                    applicable_errors = _mapping_errors_for_targets(
                        owner["mapping_errors"],
                        current_targets,
                        owner_scope=owner["owner_scope"],
                        owner_document_path=owner["owner_document_path"],
                        component=component,
                    )
                    _require(
                        not applicable_errors,
                        f"{owner_path} has unresolved identities for "
                        f"participating targets: "
                        f"{json.dumps(applicable_errors, sort_keys=True)}",
                    )
                if not current_targets:
                    continue
                if all(
                    identity[0] == "feature"
                    for identity in current_targets
                ):
                    feature_identities = {
                        _feature_target_identity(target)
                        for target in current_targets.values()
                    }
                    _require(
                        len(feature_identities) == 1,
                        f"{entity_path} native reference aliases multiple features",
                    )
                    for target in current_targets.values():
                        for item in target["faces"]:
                            face = (
                                item["body_type"],
                                item["body_index"],
                                item["face_index"],
                            )
                            if face in all_faces:
                                faces_by_scope["feature"].add(face)
                    continue
                _require(
                    len(current_targets) <= 1,
                    f"{entity_path} native reference aliases multiple targets",
                )
                kind, _component, body_type, body_index, face_index = next(
                    iter(current_targets)
                )
                if kind == "occurrence":
                    faces_by_scope["occurrence"] |= all_faces
                    continue
                if kind == "document":
                    faces_by_scope["document"] |= all_faces
                    continue
                face_key = (body_type, body_index, face_index)
                if kind == "body":
                    faces_by_scope["body"] |= {
                        face
                        for face in all_faces
                        if face[:2] == face_key[:2]
                    }
                elif face_key in all_faces:
                    faces_by_scope[kind].add(face_key)
            material_faces = set().union(*faces_by_scope.values())
            if not material_faces:
                continue
            _require(
                not material.get("read_errors")
                and isinstance(material.get("linked_display_states"), list)
                and owner_display_state in material["linked_display_states"]
                and not any(
                    entity.get("read_errors")
                    for entity in material["entity_records"]
                )
                and material.get("color_form") == 1
                and material.get("texture_filename") in (None, "")
                and material.get("surface_finish_file") in (None, ""),
                f"{material_path} is not a qualified solid-color appearance",
            )
            rgba = _colorref(
                material.get("primary_color"),
                material.get("transparency"),
            )
            _require(
                rgba is not None,
                f"{material_path} has invalid color values",
            )
            state_materials.append(
                {
                    "rgba": rgba,
                    "source": {
                        "kind": "display_state_render_material",
                        "owner_scope": owner.get("owner_scope"),
                        "owner_document_path": owner.get(
                            "owner_document_path"
                        ),
                        "configuration": owner.get("configuration"),
                        "display_state": owner_display_state,
                        "material_index": index,
                        "primary_color": material.get("primary_color"),
                        "transparency": material.get("transparency"),
                        "derivation": {
                            "rule_id": "appearance.primary_color_colorref",
                            "rule_version": "v1",
                            "warning_code": (
                                "appearance.primary_color_colorref_derived"
                            ),
                            "red_shift": 0,
                            "green_shift": 8,
                            "blue_shift": 16,
                            "alpha_rule": {
                                "operation": "one_minus",
                                "source_field": "transparency",
                            },
                        },
                    },
                    "faces_by_scope": {
                        kind: set(faces)
                        for kind, faces in faces_by_scope.items()
                    },
                }
            )

    resolved = {}
    for face in sorted(all_faces):
        body_key = face[:2]
        selected = None
        for scope in APPEARANCE_SCOPE_ORDER:
            current = []
            owner = {
                "occurrence": occurrence,
                "face": face_owners[body_key][face],
                "body": body_owners[body_key],
                "document": document,
            }.get(scope)
            if scope == "document":
                current.extend(
                    {
                        "rgba": material["rgba"],
                        "source": material["source"],
                    }
                    for material in state_materials
                    if face in material.get("faces_by_scope", {}).get(scope, set())
                )
            elif owner is not None:
                resolved_owner = _owner_rgba(
                    owner,
                    configuration=configuration,
                    where=f"{where}.{scope}",
                    use_presence=scope != "document",
                )
                if resolved_owner is not None:
                    current.append(
                        {
                            "rgba": resolved_owner[0],
                            "source": resolved_owner[1],
                        }
                    )
            if scope != "document":
                current.extend(
                    {
                        "rgba": material["rgba"],
                        "source": material["source"],
                    }
                    for material in state_materials
                    if face in material.get("faces_by_scope", {}).get(scope, set())
                )
            elif not current and owner is not None:
                resolved_owner = _owner_rgba(
                    owner,
                    configuration=configuration,
                    where=f"{where}.{scope}",
                    use_presence=False,
                )
                if resolved_owner is not None:
                    current.append(
                        {
                            "rgba": resolved_owner[0],
                            "source": resolved_owner[1],
                        }
                    )
            if not current:
                continue
            colors = {tuple(choice["rgba"]) for choice in current}
            _require(
                len(colors) == 1,
                f"{where} has conflicting appearances at proven scope {scope}",
            )
            selected = {
                "rgba": colors.pop(),
                "source": {
                    "scopes": [scope],
                    "configuration": configuration,
                    "display_state": display_state,
                    "owners": [choice["source"] for choice in current],
                },
            }
            break
        _require(selected is not None, f"{where} does not resolve face {face}")
        resolved[face] = selected
    return {
        "display_state": display_state,
        "faces": resolved,
    }


def _expected_groups(root: Path, row: Mapping, resolved: Mapping) -> list[dict]:
    component = str(row["component"])
    tessellation = row.get("tessellation")
    _require(isinstance(tessellation, Mapping), f"{component} has no tessellation")
    spans = tessellation.get("face_spans")
    _require(isinstance(spans, list) and spans, f"{component} has no face spans")
    by_rgba: dict[tuple[float, float, float, float], list[dict]] = {}
    next_start = 0
    face_keys = set()
    for index, span in enumerate(spans):
        _require(
            isinstance(span, Mapping)
            and span.get("triangle_start") == next_start
            and type(span.get("triangle_count")) is int
            and span["triangle_count"] > 0,
            f"{component} face span {index} is not a contiguous partition",
        )
        key = _face_key(
            span.get("body_type"),
            span.get("body_index"),
            span.get("face_index"),
        )
        _require(key not in face_keys, f"{component} repeats face {key}")
        face_keys.add(key)
        resolution = resolved.get("faces", {}).get(key)
        _require(resolution is not None, f"{component} appearance misses face {key}")
        reference = span.get("face_reference_sha256")
        _require(
            isinstance(reference, str) and len(reference) == 64,
            f"{component} face {key} has no reference digest",
        )
        item = {
            "key": key,
            "reference": reference,
            "triangle_start": span["triangle_start"],
            "triangle_count": span["triangle_count"],
            "rgba": tuple(float(value) for value in resolution["rgba"]),
            "source": resolution["source"],
        }
        by_rgba.setdefault(item["rgba"], []).append(item)
        next_start += span["triangle_count"]

    original = confined(root / "evidence", str(row.get("path") or ""))
    _require(
        file_digest(original) == row.get("sha"),
        f"{component} source STL differs from its geometry record",
    )
    source_bytes = original.read_bytes()
    groups = []
    for rgba, items in by_rgba.items():
        ranges = []
        for item in items:
            if (
                ranges
                and ranges[-1]["triangle_start"] + ranges[-1]["triangle_count"]
                == item["triangle_start"]
            ):
                ranges[-1]["triangle_count"] += item["triangle_count"]
            else:
                ranges.append(
                    {
                        "triangle_start": item["triangle_start"],
                        "triangle_count": item["triangle_count"],
                    }
                )
        face_references = sorted(item["reference"] for item in items)
        sources = {
            hashlib.sha256(
                json.dumps(
                    item["source"],
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest(): item["source"]
            for item in items
        }
        group_id = hashlib.sha256(
            (
                ",".join(face_references)
                + ":"
                + ",".join(format(value, ".17g") for value in rgba)
            ).encode("ascii")
        ).hexdigest()[:16]
        subset, count = native_stl_subset(source_bytes, ranges)
        groups.append(
            {
                "group_id": group_id,
                "rgba": list(rgba),
                "face_references": face_references,
                "triangle_ranges": ranges,
                "sources": [sources[key] for key in sorted(sources)],
                "triangle_count": count,
                "mesh_sha256": hashlib.sha256(subset).hexdigest(),
            }
        )
    return sorted(groups, key=lambda group: group["face_references"][0])


def verify_native_appearance(
    root: Path,
    raw_scene: Mapping[str, Any],
    geometry_rows: Sequence[Mapping[str, Any]],
    model: Mapping[str, Any],
) -> dict:
    """Recompute every face/color group and compare it with canonical visuals."""

    components = raw_scene.get("components")
    _require(isinstance(components, list), "Native appearance has no raw components")
    raw_by_name = {
        row["name"]: row
        for row in components
        if isinstance(row, Mapping) and isinstance(row.get("name"), str)
    }
    model_links = {
        row["name"]: row
        for row in model.get("links") or []
        if isinstance(row, Mapping)
    }
    mesh_namespace = asset_namespace(
        Path(
            str(
                (model.get("provenance") or {}).get("assembly")
                or model.get("name")
                or "robot"
            )
        ).name
    )
    verified_faces = 0
    verified_groups = 0
    verified_triangles = 0
    for row_index, row in enumerate(geometry_rows):
        _require(isinstance(row, Mapping), "A raw geometry row is malformed")
        component = str(row.get("component") or "")
        raw = raw_by_name.get(component)
        _require(raw is not None, f"{component} has no raw scene record")
        appearance = raw.get("appearance_raw")
        _require(isinstance(appearance, Mapping), f"{component} has no appearance record")
        source_scope = row.get("source_scope")
        _require(isinstance(source_scope, Mapping), f"{component} has no source scope")
        configuration = source_scope.get("referenced_configuration")
        _require(isinstance(configuration, str), f"{component} has no referenced configuration")
        resolved = _resolved_faces(
            appearance,
            configuration=configuration,
            where=f"geometry[{row_index}].appearance_raw",
            expected_component=component,
            expected_document_path=source_scope.get("source_document_path"),
        )
        groups = _expected_groups(root, row, resolved)
        expected_faces = sum(len(span_group["face_references"]) for span_group in groups)
        verified_faces += expected_faces
        verified_groups += len(groups)
        verified_triangles += sum(
            span_group["triangle_count"] for span_group in groups
        )

        shapes = [
            (link, shape)
            for link in model_links.values()
            for shape in link.get("visuals") or []
            if (shape.get("provenance") or {}).get("native_component") == component
        ]
        _require(len(shapes) == len(groups), f"{component} visual groups differ from native appearance")
        unmatched = list(shapes)
        for group in groups:
            expected_name = visual_name(
                f"{component}:appearance:{group['group_id']}",
                link=str(unmatched[0][0]["name"]) if unmatched else "link",
            )
            match = next(
                (
                    pair
                    for pair in unmatched
                    if pair[1].get("face_references") == group["face_references"]
                ),
                None,
            )
            _require(match is not None, f"{component} is missing a native appearance group")
            link, shape = match
            unmatched.remove(match)
            expected_name = visual_name(
                f"{component}:appearance:{group['group_id']}",
                link=str(link["name"]),
            )
            _require(shape.get("name") == expected_name, f"{component} visual name differs")
            _require(shape.get("rgba") == group["rgba"], f"{component} visual RGBA differs")
            _require(
                shape.get("material")
                == material_name(
                    f"{component}:appearance:{group['group_id']}",
                    link=str(link["name"]),
                ),
                f"{component} material name differs",
            )
            _require(
                shape.get("triangle_ranges") == group["triangle_ranges"],
                f"{component} triangle ranges differ",
            )
            _require(
                shape.get("mesh_sha256") == group["mesh_sha256"],
                f"{component} derived mesh digest differs",
            )
            _require(
                shape.get("mesh_name")
                == mesh_path(group["mesh_sha256"], namespace=mesh_namespace),
                f"{component} derived mesh path differs",
            )
            provenance = shape.get("provenance") or {}
            _require(
                provenance.get("material_source") == "native_appearance",
                f"{component} visual lacks native appearance provenance",
            )
            appearance_provenance = provenance.get("appearance") or {}
            _require(
                appearance_provenance.get("group_id") == group["group_id"]
                and appearance_provenance.get("face_references") == group["face_references"]
                and appearance_provenance.get("sources") == group["sources"],
                f"{component} appearance provenance differs",
            )
            _require(
                shape.get("filename") == row.get("path")
                and shape.get("sha256") == row.get("sha"),
                f"{component} visual source geometry differs",
            )
        _require(not unmatched, f"{component} carries unexpected visual groups")
    return {
        "components": len(geometry_rows),
        "faces": verified_faces,
        "groups": verified_groups,
        "triangles": verified_triangles,
    }
