"""Independently verify native face appearance and its delivered partition."""

from __future__ import annotations

import copy
import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from ..appearance_contract import (
    APPEARANCE_OBJECT_INTERFACE_KINDS,
    APPEARANCE_OBJECT_INTERFACE_ORDER,
    index_conversion_mismatch,
    scoped_rgba,
)
from ..geometry.native_tessellation import native_stl_subset
from ..io import PipelineError
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
        _require(
            kind in {
                "occurrence",
                "body",
                "face",
                "feature",
                "document",
            },
            "A native reference target has an unsupported kind",
        )
        footprint = None
        if kind == "occurrence":
            identity = ("occurrence", component, 0, 0, 0)
        elif kind in {"body", "feature"}:
            feature_identity = (
                _feature_target_identity(target)
                if kind == "feature"
                else None
            )
            body_identity = target.get("persistent_reference_sha256")
            _require(
                (
                    kind == "feature"
                    and feature_identity is not None
                )
                or (
                    kind == "body"
                    and isinstance(body_identity, str)
                    and len(body_identity) == 64
                    and all(
                        character in "0123456789abcdef"
                        for character in body_identity
                    )
                ),
                "A native body or feature target has no exact identity",
            )
            _require(
                isinstance(target.get("faces"), list) and bool(target["faces"]),
                "A native body or feature target has no footprint",
            )
            target_face_keys = set()
            for face in target["faces"]:
                face_key = (
                    _face_key(
                        face.get("body_type"),
                        face.get("body_index"),
                        face.get("face_index"),
                    )
                    if isinstance(face, Mapping)
                    else None
                )
                _require(
                    face_key is not None and face_key not in target_face_keys,
                    "A native footprint has an invalid or repeated face",
                )
                target_face_keys.add(face_key)
            identity = (
                kind,
                component,
                0,
                0,
                0,
                feature_identity if kind == "feature" else body_identity,
            )
            footprint = tuple(
                sorted(target_face_keys)
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
                    "One native identity carries conflicting face footprints",
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
        error_faces = identity.get("faces")
        body_key = None
        if kind == "bodies":
            if (
                type(identity.get("body_type")) is int
                and identity["body_type"] in (0, 1)
                and type(identity.get("body_index")) is int
                and identity["body_index"] >= 0
            ):
                body_key = (
                    identity["body_type"],
                    identity["body_index"],
                )
            if (
                not isinstance(error_faces, list)
                or not error_faces
                or any(not complete_face(face) for face in error_faces)
            ) and body_key is None:
                applicable.append(error)
                continue
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
                target_faces = {
                    (
                        face["body_type"],
                        face["body_index"],
                        face["face_index"],
                    )
                    for face in target.get("faces", ())
                    if complete_face(face)
                }
                error_footprint = {
                    (
                        face["body_type"],
                        face["body_index"],
                        face["face_index"],
                    )
                    for face in error_faces or ()
                    if complete_face(face)
                }
                matches = bool(error_footprint & target_faces) or (
                    body_key is not None
                    and any(face[:2] == body_key for face in target_faces)
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


def _persistent_reference_digest(value: object):
    reference_key = _persistent_reference_key(value)
    if reference_key is None:
        return None
    return hashlib.sha256(bytes(reference_key)).hexdigest()


def _body_witness_evidence(
    bodies: Sequence[object],
    *,
    component: str,
    expected_document_path: str,
    all_face_keys: set[tuple[int, int, int]],
):
    """Independently rebuild Part-body footprints from face witnesses."""

    footprints: dict[str, set[tuple[int, int, int]]] = {}
    body_errors: dict[tuple[int, int], dict] = {}
    body_conversions: dict[str, Mapping] = {}
    body_reference_values: dict[str, list[int]] = {}
    for position, body in enumerate(bodies):
        if not isinstance(body, Mapping):
            continue
        key = _face_key(body.get("body_type"), body.get("body_index"), 0)
        if key is None:
            continue
        body_key = key[:2]
        expected_faces = {
            face for face in all_face_keys if face[:2] == body_key
        }
        identity = body.get("native_identity")
        path = f"bodies[{position}].native_identity"
        identity_errors = []
        if (
            not isinstance(identity, Mapping)
            or identity.get("owner_scope") != "component_document"
            or _document_path_key(identity.get("owner_document_path"))
            != _document_path_key(expected_document_path)
            or identity.get("state") != "observed"
            or not isinstance(identity.get("read_errors"), Mapping)
            or identity.get("read_errors")
            or not isinstance(identity.get("observation_errors"), list)
            or identity.get("observation_errors")
        ):
            identity_errors.append(
                "the occurrence body does not carry a complete observed witness set"
            )
        observations = (
            identity.get("face_body_observations")
            if isinstance(identity, Mapping)
            else None
        )
        if not isinstance(observations, list):
            identity_errors.append(
                "the occurrence body carries no face-body observation inventory"
            )
            observations = []
        observations_by_face = {}
        distinct_references = set()
        for observation_position, observation in enumerate(observations):
            face = (
                _face_key(
                    body_key[0],
                    body_key[1],
                    observation.get("face_index")
                    if isinstance(observation, Mapping)
                    else None,
                )
            )
            if face is None or face not in expected_faces:
                identity_errors.append(
                    "a face-body observation is outside the delivered "
                    f"occurrence body at position {observation_position}"
                )
                continue
            observations_by_face.setdefault(face, []).append(observation)
            reference_key = _persistent_reference_key(
                observation.get("persistent_reference")
            )
            if reference_key is not None:
                distinct_references.add(reference_key)
                digest = _persistent_reference_digest(
                    observation.get("persistent_reference")
                )
                if digest is not None:
                    body_reference_values.setdefault(
                        digest,
                        copy.deepcopy(
                            observation.get("persistent_reference")
                        ),
                    )
        for face in expected_faces:
            face_observations = observations_by_face.get(face, [])
            if len(face_observations) != 1:
                identity_errors.append(
                    "each delivered face requires exactly one Part-body "
                    f"observation; face {face} has {len(face_observations)}"
                )
                continue
            observation = face_observations[0]
            reference_key = _persistent_reference_key(
                observation.get("persistent_reference")
            )
            digest = _persistent_reference_digest(
                observation.get("persistent_reference")
            )
            conversion = observation.get("conversion")
            read_errors = observation.get("read_errors")
            conversion_mismatch = index_conversion_mismatch(
                {
                    "persistent_reference": copy.deepcopy(
                        observation.get("persistent_reference")
                    ),
                    "native_interface_kind": "IBody2",
                    "conversion": conversion,
                    "target": {
                        "kind": "body",
                        "component": component,
                        "persistent_reference_sha256": digest,
                    },
                },
                component=component,
                owner_document_path=expected_document_path,
            )
            if (
                reference_key is None
                or digest is None
                or not isinstance(read_errors, Mapping)
                or read_errors
                or conversion_mismatch is not None
            ):
                identity_errors.append(
                    f"delivered face {face} has no exact successful Part-body "
                    "observation"
                )
                continue
            footprints.setdefault(digest, set()).add(face)
            body_conversions.setdefault(digest, conversion)
        reference_count = (
            identity.get("part_body_reference_count")
            if isinstance(identity, Mapping)
            else None
        )
        if (
            type(reference_count) is not int
            or reference_count < 0
            or reference_count != len(distinct_references)
        ):
            identity_errors.append(
                "the occurrence body reference count disagrees with its face "
                "observations"
            )
        if identity_errors:
            body_errors[body_key] = {
                "path": path,
                "errors": identity_errors,
            }
    return (
        footprints,
        body_errors,
        body_conversions,
        body_reference_values,
    )


def _component_native_relations(
    bodies: Sequence[object],
    *,
    component: str,
    expected_document_path: str,
    all_face_keys: set[tuple[int, int, int]],
):
    """Independently build native object relations for verification."""

    (
        body_footprints,
        body_errors_by_body,
        body_conversions,
        body_reference_values,
    ) = _body_witness_evidence(
        bodies,
        component=component,
        expected_document_path=expected_document_path,
        all_face_keys=all_face_keys,
    )
    relations = {}
    errors = {"body": [], "feature": []}

    def add(
        reference,
        target,
        *,
        native_interface_kind,
        conversion,
    ):
        reference_key = _persistent_reference_key(reference)
        if reference_key is None:
            return False
        relations.setdefault(reference_key, []).append(
            {
                "persistent_reference": copy.deepcopy(reference),
                "native_interface_kind": native_interface_kind,
                "conversion": copy.deepcopy(conversion),
                "read_errors": {},
                "target": copy.deepcopy(target),
            }
        )
        return True

    errors["body"].extend(
        {
            "occurrence_body": list(body_key),
            **copy.deepcopy(error),
        }
        for body_key, error in body_errors_by_body.items()
    )
    for digest, faces in body_footprints.items():
        add(
            body_reference_values.get(digest),
            {
                "kind": "body",
                "component": component,
                "faces": [
                    {
                        "body_type": face[0],
                        "body_index": face[1],
                        "face_index": face[2],
                    }
                    for face in sorted(faces)
                ],
                "occurrence_bodies": [
                    [body_type, body_index]
                    for body_type, body_index in sorted(
                        {face[:2] for face in faces}
                    )
                ],
                "persistent_reference_sha256": digest,
            },
            native_interface_kind="IBody2",
            conversion=body_conversions.get(digest),
        )

    feature_groups = {}
    for _position, body in enumerate(bodies):
        if not isinstance(body, Mapping):
            continue
        for face_row in body.get("faces", ()):
            if not isinstance(face_row, Mapping):
                continue
            face = _face_key(
                body.get("body_type"),
                body.get("body_index"),
                face_row.get("face_index"),
            )
            appearance = face_row.get("appearance")
            identity = (
                appearance.get("native_identity")
                if isinstance(appearance, Mapping)
                else None
            )
            if face is None or not isinstance(identity, Mapping):
                continue
            face_reference = identity.get("persistent_reference")
            face_conversion = identity.get("conversion")
            add(
                face_reference,
                {
                    "kind": "face",
                    "component": component,
                    "body_type": face[0],
                    "body_index": face[1],
                    "face_index": face[2],
                },
                native_interface_kind="IEntity",
                conversion=face_conversion,
            )

            feature = identity.get("feature")
            if not isinstance(feature, Mapping):
                errors["feature"].append(
                    {
                        "face": list(face),
                        "message": "a delivered face has no feature witness",
                    }
                )
                continue
            feature_reference = feature.get("persistent_reference")
            feature_key = _persistent_reference_key(feature_reference)
            feature_conversion = feature.get("conversion")
            feature_mismatch = index_conversion_mismatch(
                {
                    "persistent_reference": copy.deepcopy(feature_reference),
                    "native_interface_kind": None,
                    "conversion": feature_conversion,
                    "target": {
                        "kind": "feature",
                        "component": component,
                        "name": feature.get("name"),
                        "type_name": feature.get("type_name"),
                        "persistent_identity": (
                            list(feature_key)
                            if feature_key is not None
                            else None
                        ),
                        "faces": [
                            {
                                "body_type": face[0],
                                "body_index": face[1],
                                "face_index": face[2],
                            }
                        ],
                    },
                },
                component=component,
                owner_document_path=expected_document_path,
            )
            if (
                feature_key is None
                or feature_mismatch is not None
                or feature.get("read_errors")
            ):
                errors["feature"].append(
                    {
                        "face": list(face),
                        "mismatch": feature_mismatch,
                        "read_errors": copy.deepcopy(
                            feature.get("read_errors")
                        ),
                    }
                )
                continue
            group = feature_groups.setdefault(
                feature_key,
                {
                    "name": feature.get("name"),
                    "type_name": feature.get("type_name"),
                    "persistent_reference": copy.deepcopy(feature_reference),
                    "conversion": copy.deepcopy(feature_conversion),
                    "faces": set(),
                },
            )
            if (
                group["name"] != feature.get("name")
                or group["type_name"] != feature.get("type_name")
            ):
                errors["feature"].append(
                    {
                        "feature": list(feature_key),
                        "message": (
                            "one native feature reference carries different "
                            "names or types"
                        ),
                    }
                )
            group["faces"].add(face)
    for group in feature_groups.values():
        add(
            group["persistent_reference"],
            {
                "kind": "feature",
                "component": component,
                "name": group["name"],
                "type_name": group["type_name"],
                "persistent_reference": copy.deepcopy(
                    group["persistent_reference"]
                ),
                "faces": [
                    {
                        "body_type": face[0],
                        "body_index": face[1],
                        "face_index": face[2],
                    }
                    for face in sorted(group["faces"])
                ],
            },
            native_interface_kind=None,
            conversion=group["conversion"],
        )
    return relations, errors


def _validate_index_conversion(
    indexed: Mapping,
    *,
    owner_document_path: str,
    component: str,
    path: str,
) -> None:
    mismatch = index_conversion_mismatch(
        indexed,
        component=component,
        owner_document_path=owner_document_path,
    )
    _require(
        mismatch is None,
        "cad_appearance_render_conversion_unproven: "
        f"{path} lacks exact Part conversion evidence; "
        f"mismatch={json.dumps(mismatch, sort_keys=True)}",
    )


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
        _require(
            body.get("component") == component,
            f"{where}.bodies[{body_index}] names another component",
        )
        _require(
            body_key not in body_owners,
            f"{where} repeats a body appearance owner",
        )
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
    component_relations, component_relation_errors = (
        _component_native_relations(
            bodies,
            component=component,
            expected_document_path=source_document_path,
            all_face_keys=all_faces,
        )
    )
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
        _require(
            owner["owner_scope"] != "component_document"
            or (
                owner["native_reference_index"] == []
                and owner["mapping_errors"] == []
            ),
            f"{owner_path} component-document owner carries a derived "
            "footprint index or mapping answer",
        )
        reference_index = {}
        for index_position, indexed in enumerate(
            owner["native_reference_index"]
            if owner["owner_scope"] != "component_document"
            else ()
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
                in {"IEntity", "IBody2", None}
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
                    "persistent_reference": copy.deepcopy(
                        indexed.get("persistent_reference")
                    ),
                    "native_interface_kind": indexed.get(
                        "native_interface_kind"
                    ),
                    "target": target,
                    "read_errors": indexed_errors,
                    "conversion": copy.deepcopy(
                        indexed.get("conversion")
                    ),
                }
            )
        for index, material in enumerate(owner["materials"]):
            material_path = f"{owner_path}.materials[{index}]"
            _require(
                isinstance(material, Mapping)
                and material.get("material_index") == index
                and "entities" in material
                and isinstance(material.get("read_errors"), Mapping)
                and isinstance(
                    material.get("linked_display_states"),
                    (list, type(None)),
                )
                and (
                    isinstance(material.get("entities"), list)
                    or (
                        material.get("entities_state") == "null_result"
                        and material.get("entities") is None
                    )
                )
                and isinstance(material.get("entity_records"), list)
                and material.get("entities_state")
                in {"observed", "empty", "null_result", "error"}
                ,
                f"{material_path} is malformed",
            )
            if material["entities_state"] == "null_result":
                count = material.get("native_entity_count")
                errors = material["read_errors"]
                _require(
                    type(count) is int
                    and count == 0
                    and material["entities"] is None
                    and not material["entity_records"]
                    and "entities" not in errors
                    and "entity_count" not in errors,
                    f"{material_path} has no proven empty entity inventory",
                )
                continue
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
                    and isinstance(
                        entity.get("native_interface_kinds"), list
                    )
                    and entity["native_interface_kinds"]
                    == [
                        kind
                        for kind in APPEARANCE_OBJECT_INTERFACE_ORDER
                        if kind in entity["native_interface_kinds"]
                    ]
                    and set(entity["native_interface_kinds"])
                    <= APPEARANCE_OBJECT_INTERFACE_KINDS
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
                        and "IModelDoc2"
                        in entity["native_interface_kinds"]
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
                    reference_identity = (
                        native_identity
                        if isinstance(native_identity, Mapping)
                        and native_identity.get("kind") == "reference"
                        else None
                    )
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
                        and bool(entity["native_interface_kinds"])
                        and reference_identity is not None
                        and reference_identity.get("state") == "observed"
                        and reference_identity.get("proof_api")
                        == (
                            "IModelDocExtension."
                            "GetObjectByPersistReference3"
                        )
                        and type(reference_identity.get("status")) is int
                        and reference_identity.get("status") == 0
                        and _document_path_key(
                            reference_identity.get("owner_document_path")
                        )
                        == _document_path_key(owner.get("owner_document_path"))
                        and reference_identity.get("interface_kinds")
                        == entity["native_interface_kinds"]
                        and not reference_identity.get("read_errors")
                    )
                    _require(
                        source_context_proven,
                        f"{entity_path} is not bound to its actual owner document",
                    )
                    if owner["owner_scope"] == "component_document":
                        interface_target_kind = {
                            "IBody2": "body",
                            "IFeature": "feature",
                        }
                        applicable_native_errors = {
                            target_kind: component_relation_errors[target_kind]
                            for interface_kind, target_kind in (
                                interface_target_kind.items()
                            )
                            if interface_kind
                            in entity["native_interface_kinds"]
                            and component_relation_errors[target_kind]
                        }
                        _require(
                            not applicable_native_errors,
                            f"{entity_path} native object relation evidence "
                            f"is unresolved: "
                            f"{json.dumps(applicable_native_errors, sort_keys=True)}",
                        )
                        targets = component_relations.get(entity_key, ())
                    else:
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
                    target_faces_by_identity = {}
                    for identity_key, target in current_targets.items():
                        if target["kind"] in {"body", "feature"}:
                            target_faces = {
                                (
                                    face["body_type"],
                                    face["body_index"],
                                    face["face_index"],
                                )
                                for face in target["faces"]
                            }
                        elif target["kind"] == "face":
                            target_faces = {
                                (
                                    target["body_type"],
                                    target["body_index"],
                                    target["face_index"],
                                )
                            }
                        else:
                            target_faces = set()
                        target_faces_by_identity[identity_key] = target_faces
                        _require(
                            target_faces <= all_faces,
                            f"{entity_path} native target contains a face "
                            "outside the delivered geometry",
                        )
                    target_interfaces = {
                        "occurrence": "IComponent2",
                        "body": "IBody2",
                        "face": "IFace2",
                        "feature": "IFeature",
                        "document": "IModelDoc2",
                    }
                    _require(
                        all(
                            target_interfaces[target["kind"]]
                            in entity["native_interface_kinds"]
                            for target in current_targets.values()
                        ),
                        f"{entity_path} public interfaces do not match its "
                        "native target kinds",
                    )
                    for item in targets:
                        if item["target"] in current_targets.values():
                            _validate_index_conversion(
                                item,
                                owner_document_path=owner[
                                    "owner_document_path"
                                ],
                                component=component,
                                path=entity_path
                                + ".persistent_reference",
                            )
                    applicable_errors = (
                        []
                        if owner["owner_scope"] == "component_document"
                        else _mapping_errors_for_targets(
                            owner["mapping_errors"],
                            current_targets,
                            owner_scope=owner["owner_scope"],
                            owner_document_path=owner["owner_document_path"],
                            component=component,
                        )
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
                    identity[0] in {"body", "feature"}
                    for identity in current_targets
                ):
                    target_kinds = {identity[0] for identity in current_targets}
                    _require(
                        len(target_kinds) == 1,
                        f"{entity_path} native reference aliases body and "
                        "feature targets",
                    )
                    target_kind = next(iter(target_kinds))
                    native_identities = {
                        (
                            _feature_target_identity(target)
                            if target_kind == "feature"
                            else target.get("persistent_reference_sha256")
                        )
                        for target in current_targets.values()
                    }
                    _require(
                        len(native_identities) == 1,
                        f"{entity_path} native reference aliases multiple "
                        f"{target_kind}s",
                    )
                    for identity_key in current_targets:
                        faces_by_scope[target_kind].update(
                            target_faces_by_identity[identity_key]
                        )
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
                if face_key in all_faces:
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
            selected_rgba = scoped_rgba([choice["rgba"] for choice in current])
            _require(
                selected_rgba is not None,
                f"{where} has conflicting appearances at proven scope {scope}",
            )
            selected = {
                "rgba": selected_rgba,
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


def _expected_groups(_root: Path, row: Mapping, resolved: Mapping) -> list[dict]:
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
            and span["triangle_count"] >= 0,
            f"{component} face span {index} is not a contiguous partition",
        )
        if span["triangle_count"] == 0:
            evidence = span.get("zero_triangle_evidence")
            _require(
                type(span.get("body_type")) is int and span["body_type"] == 1
                and isinstance(evidence, dict)
                and set(evidence) == {
                    "get_tess_triangles_return", "get_tess_triangle_count", "get_area_m2",
                }
                and evidence.get("get_tess_triangles_return") in ("null", "empty_array")
                and type(evidence.get("get_tess_triangle_count")) is int
                and evidence["get_tess_triangle_count"] == 0
                and type(evidence.get("get_area_m2")) in (int, float)
                and math.isfinite(evidence["get_area_m2"])
                and evidence["get_area_m2"] >= 0,
                f"{component} face span {index} has invalid empty-display evidence",
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
        if span["triangle_count"] > 0:
            by_rgba.setdefault(item["rgba"], []).append(item)
        next_start += span["triangle_count"]

    source_bytes = row.get("source_bytes")
    _require(
        isinstance(source_bytes, bytes)
        and hashlib.sha256(source_bytes).hexdigest() == row.get("sha"),
        f"{component} source STL differs from its geometry record",
    )
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
            try:
                rgba_matches = (
                    scoped_rgba((shape.get("rgba"), group["rgba"])) is not None
                )
            except ValueError as error:
                raise PipelineError(f"{component} visual RGBA is invalid") from error
            _require(rgba_matches, f"{component} visual RGBA differs")
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
