"""Fail-closed checks for raw SolidWorks appearance evidence."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import posixpath
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ...appearance_contract import (
    APPEARANCE_OBJECT_INTERFACE_KINDS,
    APPEARANCE_OBJECT_INTERFACE_ORDER,
    index_conversion_mismatch,
    scoped_rgba,
)
from ...io import PipelineError

UNRECORDED_APPEARANCE = object()
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


@dataclass(frozen=True, slots=True)
class FaceAppearance:
    """One delivered face bound to its exact native triangle span and RGBA."""

    body_type: int
    body_index: int
    face_index: int
    face_reference_sha256: str
    triangle_start: int
    triangle_count: int
    rgba: tuple[float, float, float, float]
    source: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class AppearanceResolution:
    """The successful per-face appearance resolution for one occurrence."""

    component: str
    referenced_configuration: str
    referenced_display_state: str
    faces: tuple[FaceAppearance, ...]


def appearance_groups(
    faces: Sequence[FaceAppearance],
) -> tuple[dict[str, Any], ...]:
    """Partition one occurrence's exact face inventory by resolved RGBA."""

    if not faces:
        raise PipelineError("Native appearance has no delivered faces")
    ordered = sorted(faces, key=lambda face: face.triangle_start)
    seen_faces: set[str] = set()
    next_triangle = 0
    by_rgba: dict[tuple[float, float, float, float], list[FaceAppearance]] = {}
    for face in ordered:
        if (
            type(face.body_type) is not int
            or face.body_type not in (0, 1)
            or type(face.body_index) is not int
            or face.body_index < 0
            or type(face.face_index) is not int
            or face.face_index < 0
            or not isinstance(face.face_reference_sha256, str)
            or len(face.face_reference_sha256) != 64
            or any(
                character not in "0123456789abcdef"
                for character in face.face_reference_sha256
            )
            or type(face.triangle_start) is not int
            or type(face.triangle_count) is not int
            or face.triangle_count <= 0
            or face.triangle_start != next_triangle
            or len(face.rgba) != 4
            or any(
                type(value) not in (int, float)
                or not math.isfinite(float(value))
                or not 0 <= value <= 1
                for value in face.rgba
            )
        ):
            raise PipelineError(
                "Resolved face appearance has invalid identity, span, or RGBA"
            )
        if face.face_reference_sha256 in seen_faces:
            raise PipelineError("Resolved appearance repeats a delivered face")
        seen_faces.add(face.face_reference_sha256)
        next_triangle += face.triangle_count
        by_rgba.setdefault(tuple(float(value) for value in face.rgba), []).append(face)

    groups = []
    for rgba, group_faces in by_rgba.items():
        ranges = []
        for face in group_faces:
            if (
                ranges
                and ranges[-1]["triangle_start"]
                + ranges[-1]["triangle_count"]
                == face.triangle_start
            ):
                ranges[-1]["triangle_count"] += face.triangle_count
            else:
                ranges.append(
                    {
                        "triangle_start": face.triangle_start,
                        "triangle_count": face.triangle_count,
                    }
                )
        face_references = sorted(
            face.face_reference_sha256 for face in group_faces
        )
        sources = {
            hashlib.sha256(
                json.dumps(
                    face.source,
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest(): copy.deepcopy(dict(face.source))
            for face in group_faces
        }
        groups.append(
            {
                "group_id": hashlib.sha256(
                    (
                        ",".join(face_references)
                        + ":"
                        + ",".join(format(value, ".17g") for value in rgba)
                    ).encode("ascii")
                ).hexdigest()[:16],
                "rgba": list(rgba),
                "face_references": face_references,
                "triangle_ranges": ranges,
                "sources": [sources[key] for key in sorted(sources)],
            }
        )
    return tuple(sorted(groups, key=lambda group: group["face_references"][0]))


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


def _document_path_key(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return posixpath.normpath(value.replace("\\", "/")).casefold()


def _same_document_path(left: object, right: object) -> bool:
    left_key = _document_path_key(left)
    return left_key is not None and left_key == _document_path_key(right)


def _binding_context_key(value: object):
    if not isinstance(value, Mapping):
        return None
    owner_scope = value.get("owner_scope")
    owner_document_path = _document_path_key(
        value.get("owner_document_path")
    )
    component = value.get("component")
    if (
        not isinstance(owner_scope, str)
        or not owner_scope
        or owner_document_path is None
        or not isinstance(component, str)
        or not component
    ):
        return None
    return owner_scope, owner_document_path, component


def _valid_values(value: object) -> bool:
    return (
        isinstance(value, list)
        and len(value) == 9
        and all(
            type(item) in (int, float) and math.isfinite(float(item))
            for item in value
        )
    )


def _appearance_resolution_error(
    component: str,
    code: str,
    path: str,
    message: str,
    **detail: Any,
) -> AppearanceError:
    return AppearanceError(
        AppearanceAudit(
            component,
            "unrepresentable",
            (_issue(code, path, message, **detail),),
            (),
        )
    )


def _face_key(body_type: object, body_index: object, face_index: object):
    if (
        type(body_type) is not int
        or body_type not in (0, 1)
        or type(body_index) is not int
        or body_index < 0
        or type(face_index) is not int
        or face_index < 0
    ):
        return None
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


def _unique_current_targets(
    targets: Sequence[Mapping[str, Any]],
    *,
    component: str,
    path: str,
) -> dict[tuple, Mapping[str, Any]]:
    current: dict[tuple, Mapping[str, Any]] = {}
    for target in targets:
        target_component = target.get("component")
        if not isinstance(target_component, str) or not target_component:
            raise _appearance_resolution_error(
                component,
                "cad_appearance_render_entity_invalid",
                path,
                "a native reference target has no exact component",
            )
        if target_component != component:
            continue
        kind = target["kind"]
        if kind not in {
            "occurrence",
            "body",
            "face",
            "feature",
            "document",
        }:
            raise _appearance_resolution_error(
                component,
                "cad_appearance_render_entity_invalid",
                path,
                "a native reference target has an unsupported kind",
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
            if (
                (
                    kind == "feature"
                    and feature_identity is None
                )
                or (
                    kind == "body"
                    and (
                        not isinstance(body_identity, str)
                        or len(body_identity) != 64
                        or any(
                            character not in "0123456789abcdef"
                            for character in body_identity
                        )
                    )
                )
                or not isinstance(target.get("faces"), list)
                or not target["faces"]
            ):
                raise _appearance_resolution_error(
                    component,
                    "cad_appearance_render_entity_invalid",
                    path,
                    "a native body or feature target has no exact footprint",
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
                if face_key is None or face_key in target_face_keys:
                    raise _appearance_resolution_error(
                        component,
                        "cad_appearance_render_entity_invalid",
                        path,
                        "a native footprint has an invalid or repeated face",
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
            if face_key is None:
                raise _appearance_resolution_error(
                    component,
                    "cad_appearance_render_entity_invalid",
                    path,
                    "a native reference target identity is invalid",
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
                if previous_footprint != footprint:
                    raise _appearance_resolution_error(
                        component,
                        "cad_appearance_render_entity_ambiguous",
                        path,
                        "one native identity carries conflicting face footprints",
                    )
            continue
        current[identity] = target
    return current


def _mapping_errors_for_targets(
    mapping_errors: Sequence[object],
    targets: Mapping[tuple[Any, ...], Mapping[str, Any]],
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
                    feature_identity is not None
                    and identity.get("name") == target.get("name")
                    and identity.get("type_name") == target.get("type_name")
                )
                error_reference = _persistent_reference_key(
                    identity.get("persistent_reference")
                )
                target_reference = (
                    feature_identity[-1]
                    if feature_identity is not None
                    else None
                )
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


def _feature_target_identity(target: Mapping[str, Any]):
    name = target.get("name")
    type_name = target.get("type_name")
    if (
        not isinstance(name, str)
        or not name
        or not isinstance(type_name, str)
        or not type_name
    ):
        return None
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
        return None
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
    """Rebuild Part-body footprints only from per-face native witnesses."""

    footprints: dict[str, set[tuple[int, int, int]]] = {}
    body_errors: dict[tuple[int, int], dict] = {}
    body_conversions: dict[str, Mapping[str, Any]] = {}
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
        path = f"appearance_raw.bodies[{position}].native_identity"
        identity_errors = []
        if (
            not isinstance(identity, Mapping)
            or identity.get("owner_scope") != "component_document"
            or not _same_document_path(
                identity.get("owner_document_path"),
                expected_document_path,
            )
            or identity.get("state") != "observed"
            or not isinstance(identity.get("read_errors"), Mapping)
            or identity.get("read_errors")
            or not isinstance(identity.get("observation_errors"), list)
            or identity.get("observation_errors")
        ):
            identity_errors.append(
                {
                    "code": "cad_appearance_part_body_witness_unproven",
                    "message": (
                        "the occurrence body does not carry a complete "
                        "observed witness set"
                    ),
                }
            )
        observations = (
            identity.get("face_body_observations")
            if isinstance(identity, Mapping)
            else None
        )
        if not isinstance(observations, list):
            identity_errors.append(
                {
                    "code": "cad_appearance_part_body_observation_invalid",
                    "message": (
                        "the occurrence body carries no face-body observation "
                        "inventory"
                    ),
                }
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
                    {
                        "code": "cad_appearance_part_body_observation_invalid",
                        "message": (
                            "a face-body observation is outside the delivered "
                            "occurrence body"
                        ),
                        "position": observation_position,
                    }
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
                    {
                        "code": "cad_appearance_part_body_observation_invalid",
                        "message": (
                            "each delivered face requires exactly one Part-body "
                            "observation"
                        ),
                        "face": {
                            "body_type": face[0],
                            "body_index": face[1],
                            "face_index": face[2],
                        },
                        "observation_count": len(face_observations),
                    }
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
                    {
                        "code": "cad_appearance_part_body_observation_invalid",
                        "message": (
                            "a delivered face has no exact successful Part-body "
                            "observation"
                        ),
                        "face": {
                            "body_type": face[0],
                            "body_index": face[1],
                            "face_index": face[2],
                        },
                    }
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
                {
                    "code": "cad_appearance_part_body_reference_count_invalid",
                    "message": (
                        "the occurrence body reference count disagrees with its "
                        "face observations"
                    ),
                }
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
    """Build one in-memory native object relation index for this consumer."""

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
    relations: dict[tuple[int, ...], list[dict[str, Any]]] = {}
    errors = {"body": [], "feature": []}

    def add(
        reference,
        target,
        *,
        native_interface_kind,
        conversion,
        read_errors=None,
    ):
        reference_key = _persistent_reference_key(reference)
        if reference_key is None:
            return False
        relations.setdefault(reference_key, []).append(
            {
                "persistent_reference": copy.deepcopy(reference),
                "native_interface_kind": native_interface_kind,
                "conversion": copy.deepcopy(conversion),
                "read_errors": copy.deepcopy(read_errors or {}),
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
        occurrence_bodies = sorted({face[:2] for face in faces})
        reference = body_reference_values.get(digest)
        add(
            reference,
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
                    for body_type, body_index in occurrence_bodies
                ],
                "persistent_reference_sha256": digest,
            },
            native_interface_kind="IBody2",
            conversion=body_conversions.get(digest),
        )

    feature_groups: dict[
        tuple[int, ...],
        dict[str, Any],
    ] = {}
    for _position, body in enumerate(bodies):
        if not isinstance(body, Mapping):
            continue
        body_type = body.get("body_type")
        body_index = body.get("body_index")
        for face_row in body.get("faces", ()):
            if not isinstance(face_row, Mapping):
                continue
            face_index = face_row.get("face_index")
            face = _face_key(body_type, body_index, face_index)
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
                        "face": {
                            "body_type": face[0],
                            "body_index": face[1],
                            "face_index": face[2],
                        },
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
                        "face": {
                            "body_type": face[0],
                            "body_index": face[1],
                            "face_index": face[2],
                        },
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
    for _feature_key, group in feature_groups.items():
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
    component: str,
    indexed: Mapping[str, Any],
    *,
    owner_document_path: str,
    path: str,
) -> None:
    mismatch = index_conversion_mismatch(
        indexed,
        component=component,
        owner_document_path=owner_document_path,
    )
    if mismatch is None:
        return
    raise _appearance_resolution_error(
        component,
        "cad_appearance_render_conversion_unproven",
        path + ".conversion",
        "the participating target lacks exact Part conversion evidence",
        mismatch=copy.deepcopy(mismatch),
    )


def _rgba_from_material_values(
    values: Sequence[object],
) -> tuple[float, float, float, float] | None:
    if not _valid_values(values) or all(float(value) == -1.0 for value in values):
        return None
    if any(float(value) < 0 or float(value) > 1 for value in values):
        return None
    return (
        float(values[0]),
        float(values[1]),
        float(values[2]),
        1.0 - float(values[7]),
    )


def _rgba_from_colorref(value: object, transparency: object):
    if (
        type(value) is not int
        or value < 0
        or value > 0xFFFFFF
        or type(transparency) not in (int, float)
        or not math.isfinite(float(transparency))
        or not 0 <= float(transparency) <= 1
    ):
        return None
    return (
        (value & 0xFF) / 255.0,
        ((value >> 8) & 0xFF) / 255.0,
        ((value >> 16) & 0xFF) / 255.0,
        1.0 - float(transparency),
    )


def _owner_color(
    raw: object,
    *,
    component: str,
    path: str,
    expected_configuration: str,
    use_presence: bool = True,
) -> tuple[tuple[float, float, float, float], dict[str, Any]] | None:
    if not isinstance(raw, Mapping):
        raise _appearance_resolution_error(
            component,
            "cad_appearance_owner_missing",
            path,
            "the appearance owner record is absent or malformed",
        )
    used_api = raw.get("used_api")
    if not isinstance(used_api, str) or not used_api:
        raise _appearance_resolution_error(
            component,
            "cad_appearance_api_missing",
            path + ".used_api",
            "the appearance owner record names no native API",
        )
    material_call = raw.get("material_call")
    if not isinstance(material_call, Mapping):
        raise _appearance_resolution_error(
            component,
            "cad_appearance_configuration_mismatch",
            path + ".material_call",
            "the appearance material call has no configuration identity",
        )
    active = material_call.get("active_configuration")
    names = material_call.get("configuration_names")
    if (
        active != expected_configuration
        and not (isinstance(names, list) and expected_configuration in names)
    ):
        raise _appearance_resolution_error(
            component,
            "cad_appearance_configuration_mismatch",
            path + ".material_call",
            "the appearance material call is not bound to the referenced configuration",
            expected=expected_configuration,
            observed=dict(material_call),
        )
    read_errors = raw.get("read_errors")
    if not isinstance(read_errors, Mapping):
        raise _appearance_resolution_error(
            component,
            "cad_appearance_errors_malformed",
            path + ".read_errors",
            "the appearance error record is not an object",
        )
    relevant_errors = {
        field: error
        for field, error in read_errors.items()
        if use_presence or field != "has_material_property_values"
    }
    if relevant_errors:
        field, error = sorted(relevant_errors.items())[0]
        raise _appearance_resolution_error(
            component,
            "cad_appearance_read_failed",
            f"{path}.read_errors.{field}",
            "an appearance read failed and cannot be treated as absence",
            error=error,
        )
    texture = raw.get("texture")
    if not isinstance(texture, Mapping):
        raise _appearance_resolution_error(
            component,
            "cad_appearance_texture_invalid",
            path + ".texture",
            "the texture record is absent or malformed",
        )
    if texture.get("state") != "absent":
        raise _appearance_resolution_error(
            component,
            "cad_appearance_texture_unrepresentable",
            path + ".texture",
            "the owner carries a texture that is not qualified for delivery",
            observed=dict(texture),
        )
    if texture.get("material_name") is not None:
        raise _appearance_resolution_error(
            component,
            "cad_appearance_texture_state_conflict",
            path + ".texture.material_name",
            "an absent texture carries a material name",
            observed=texture.get("material_name"),
        )
    values = raw.get("material_property_values")
    has_values = raw.get("has_material_property_values")
    if values is None:
        if use_presence and has_values is not None and type(has_values) is not bool:
            raise _appearance_resolution_error(
                component,
                "cad_appearance_presence_invalid",
                path + ".has_material_property_values",
                "the appearance presence flag is not a native boolean",
                observed=has_values,
            )
        return None
    if not _valid_values(values):
        raise _appearance_resolution_error(
            component,
            "cad_appearance_values_invalid",
            path + ".material_property_values",
            "material values are not the documented nine finite numbers",
            observed=values,
        )
    rgba = _rgba_from_material_values(values)
    if rgba is None:
        if all(float(value) == -1.0 for value in values):
            return None
        raise _appearance_resolution_error(
            component,
            "cad_appearance_values_invalid",
            path + ".material_property_values",
            "material values are outside the documented [0, 1] range",
            observed=values,
        )
    if use_presence and has_values is False:
        raise _appearance_resolution_error(
            component,
            "cad_appearance_presence_conflict",
            path,
            "the presence flag denies explicit material values",
            material_property_values=values,
        )
    if use_presence and has_values is not None and type(has_values) is not bool:
        raise _appearance_resolution_error(
            component,
            "cad_appearance_presence_invalid",
            path + ".has_material_property_values",
            "the appearance presence flag is not a native boolean",
            observed=has_values,
        )
    return rgba, {
        "kind": "owner",
        "used_api": used_api,
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
    owner: Mapping[str, Any],
    *,
    component: str,
    path: str,
    expected_configuration: str,
    expected_display_state: str,
) -> str:
    scope = owner.get("owner_scope")
    configuration = owner.get("configuration")
    owner_state = owner.get("display_state")
    display_states = owner.get("display_states")
    display_states_state = owner.get("display_states_state")
    if (
        not isinstance(scope, str)
        or not isinstance(configuration, str)
        or not configuration
        or not isinstance(owner_state, str)
        or not owner_state
    ):
        raise _appearance_resolution_error(
            component,
            "cad_appearance_render_owner_state_invalid",
            path,
            "a display-state owner has no exact native configuration and state",
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
    if not proven:
        raise _appearance_resolution_error(
            component,
            "cad_appearance_render_owner_state_invalid",
            path,
            "a display-state owner is not proven from its own native configuration and state",
            owner_scope=scope,
            expected_configuration=(
                expected_configuration
                if scope == "component_document"
                else None
            ),
            expected_display_state=(
                expected_display_state
                if scope == "component_document"
                else None
            ),
            observed={
                "configuration": configuration,
                "display_state": owner_state,
                "display_states": display_states,
                "display_states_state": display_states_state,
            },
        )
    return owner_state


def resolve_deliverable_appearance(
    raw: object,
    *,
    component: str,
    expected_document_path: object,
    expected_configuration: object,
    face_spans: Sequence[Mapping[str, Any]],
) -> AppearanceResolution:
    """Resolve one RGBA for every delivered face from the frozen owner/state facts."""

    component = str(component)
    if raw is UNRECORDED_APPEARANCE or raw is None or not isinstance(raw, Mapping):
        raise _appearance_resolution_error(
            component,
            "cad_appearance_record_missing",
            "appearance_raw",
            "an explicit appearance record is missing",
        )
    configuration = (
        expected_configuration
        if isinstance(expected_configuration, str)
        else ""
    )
    if (
        raw.get("component") != component
        or not _same_document_path(
            raw.get("source_document_path"),
            expected_document_path,
        )
        or not configuration
        or raw.get("referenced_configuration") != configuration
    ):
        raise _appearance_resolution_error(
            component,
            "cad_appearance_owner_mismatch",
            "appearance_raw",
            "the appearance record is not bound to the requested occurrence and configuration",
        )
    if (
        raw.get("native_active_configuration_error") is not None
        or raw.get("native_active_configuration") != configuration
    ):
        raise _appearance_resolution_error(
            component,
            "cad_appearance_active_configuration_invalid",
            "appearance_raw.native_active_configuration",
            "the active configuration is not the referenced configuration",
            expected=configuration,
            observed=raw.get("native_active_configuration"),
            error=raw.get("native_active_configuration_error"),
        )
    if (
        raw.get("native_referenced_display_state_error") is not None
        or not isinstance(raw.get("native_referenced_display_state"), str)
        or not raw.get("native_referenced_display_state")
    ):
        raise _appearance_resolution_error(
            component,
            "cad_appearance_display_state_invalid",
            "appearance_raw.native_referenced_display_state",
            "the referenced display state is unreadable",
            observed=raw.get("native_referenced_display_state"),
            error=raw.get("native_referenced_display_state_error"),
        )
    display_state = str(raw["native_referenced_display_state"])

    spans = []
    next_triangle = 0
    face_by_key: dict[tuple[int, int, int], Mapping[str, Any]] = {}
    for index, span in enumerate(face_spans):
        if not isinstance(span, Mapping):
            raise _appearance_resolution_error(
                component,
                "cad_appearance_face_span_invalid",
                f"face_spans[{index}]",
                "a delivered face span is malformed",
            )
        key = _face_key(
            span.get("body_type"),
            span.get("body_index"),
            span.get("face_index"),
        )
        reference = span.get("face_reference_sha256")
        if (
            key is None
            or key in face_by_key
            or span.get("triangle_start") != next_triangle
            or type(span.get("triangle_count")) is not int
            or span.get("triangle_count") <= 0
            or not isinstance(reference, str)
            or len(reference) != 64
        ):
            raise _appearance_resolution_error(
                component,
                "cad_appearance_face_span_invalid",
                f"face_spans[{index}]",
                "a delivered face span is not a unique contiguous face identity",
            )
        face_by_key[key] = span
        spans.append(span)
        next_triangle += span["triangle_count"]
    if not spans:
        raise _appearance_resolution_error(
            component,
            "cad_appearance_face_inventory_missing",
            "face_spans",
            "the delivered component has no native faces",
        )
    all_faces = tuple(face_by_key)
    all_face_keys = set(all_faces)

    body_owners = {}
    bodies = raw.get("bodies")
    if not isinstance(bodies, list):
        raise _appearance_resolution_error(
            component,
            "cad_appearance_bodies_invalid",
            "appearance_raw.bodies",
            "the body appearance inventory is not a list",
        )
    for index, body in enumerate(bodies):
        path = f"appearance_raw.bodies[{index}]"
        if not isinstance(body, Mapping):
            raise _appearance_resolution_error(
                component,
                "cad_appearance_body_invalid",
                path,
                "the body appearance record is not an object",
            )
        key = _face_key(body.get("body_type"), body.get("body_index"), 0)
        if (
            body.get("component") != component
            or key is None
            or key[:2] in body_owners
        ):
            raise _appearance_resolution_error(
                component,
                "cad_appearance_body_identity_invalid",
                path,
                (
                    "the body appearance record is not bound to the current "
                    "component with a unique native identity"
                ),
            )
        face_rows = body.get("faces")
        if not isinstance(face_rows, list):
            raise _appearance_resolution_error(
                component,
                "cad_appearance_body_faces_invalid",
                path + ".faces",
                "the body appearance record carries no face inventory",
            )
        owner = body.get("appearance")
        faces = {}
        for face_index, face in enumerate(face_rows):
            face_path = f"{path}.faces[{face_index}]"
            if not isinstance(face, Mapping):
                raise _appearance_resolution_error(
                    component,
                    "cad_appearance_face_invalid",
                    face_path,
                    "a face appearance record is not an object",
                )
            key = _face_key(key[0], key[1], face.get("face_index"))
            if key is None or key in faces:
                raise _appearance_resolution_error(
                    component,
                    "cad_appearance_face_identity_invalid",
                    face_path,
                    "a face appearance record has no unique identity",
                )
            faces[key] = face.get("appearance")
        body_owners[key[:2]] = (owner, faces)
    if {key[:2] for key in all_faces} != set(body_owners):
        raise _appearance_resolution_error(
            component,
            "cad_appearance_body_inventory_mismatch",
            "appearance_raw.bodies",
            "appearance body owners differ from delivered body spans",
        )
    for body_key, (_owner, faces) in body_owners.items():
        expected_faces = {key for key in all_faces if key[:2] == body_key}
        if set(faces) != expected_faces:
            raise _appearance_resolution_error(
                component,
                "cad_appearance_face_inventory_mismatch",
                f"appearance_raw.bodies[{body_key[0]}/{body_key[1]}].faces",
                "face appearance owners differ from delivered face spans",
            )

    component_relations, component_relation_errors = (
        _component_native_relations(
            bodies,
            component=component,
            expected_document_path=expected_document_path,
            all_face_keys=all_face_keys,
        )
    )

    occurrence_owner = raw.get("occurrence")
    document_owner = raw.get("document")

    owner_records = raw.get("state_render_materials")
    if not isinstance(owner_records, list):
        raise _appearance_resolution_error(
            component,
            "cad_appearance_render_materials_missing",
            "appearance_raw.state_render_materials",
            "the display-state appearance owner records are missing",
        )

    parsed_materials = []
    owner_scope_rank = {
        "top_assembly": 0,
        "parent_assembly": 1,
        "component_document": 2,
    }
    for owner_index, owner in enumerate(owner_records):
        owner_path = f"appearance_raw.state_render_materials[{owner_index}]"
        if (
            not isinstance(owner, Mapping)
            or owner.get("owner_scope") not in owner_scope_rank
            or not isinstance(owner.get("owner_document_path"), str)
            or owner.get("api") != "IModelDocExtension.GetRenderMaterials2"
            or owner.get("display_state_option") != 3
            or owner.get("call_state")
            not in {
                "observed",
                "empty",
                "null_result",
                "error",
                "state_unavailable",
            }
            or (
                owner.get("materials") is not None
                and not isinstance(owner.get("materials"), list)
            )
            or (
                owner.get("native_render_material_count") is None
                and not isinstance(
                    owner.get("native_render_material_count_error"),
                    Mapping,
                )
            )
            or (
                owner.get("native_render_material_count") is not None
                and (
                    type(owner.get("native_render_material_count")) is not int
                    or owner.get("native_render_material_count") < 0
                    or owner.get("native_render_material_count_error") is not None
                )
            )
            or owner.get("native_render_material_count_api")
            != "IModelDocExtension.GetRenderMaterialsCount2"
            or not isinstance(owner.get("mapping_errors"), list)
            or not isinstance(owner.get("native_reference_index"), list)
            or not isinstance(owner.get("read_errors"), Mapping)
        ):
            raise _appearance_resolution_error(
                component,
                "cad_appearance_render_owner_invalid",
                owner_path,
                "a display-state render-material owner record is malformed",
            )
        if (
            owner.get("owner_scope") == "component_document"
            and not _same_document_path(
                owner.get("owner_document_path"),
                expected_document_path,
            )
        ):
            raise _appearance_resolution_error(
                component,
                "cad_appearance_render_entity_binding_unproven",
                owner_path + ".owner_document_path",
                "a component-document owner does not name the exact source document",
                expected=expected_document_path,
                observed=owner.get("owner_document_path"),
            )
        owner_display_state = _state_owner_display_state(
            owner,
            component=component,
            path=owner_path,
            expected_configuration=configuration,
            expected_display_state=display_state,
        )
        zero_material_null = (
            owner.get("call_state") == "null_result"
            and owner.get("native_render_material_count") == 0
            and owner.get("native_render_material_count_error") is None
            and owner.get("materials") is None
            and not owner.get("read_errors")
        )
        if (
            (
                owner.get("call_state") not in {"observed", "empty"}
                and not zero_material_null
            )
            or owner.get("read_errors")
        ):
            raise _appearance_resolution_error(
                component,
                "cad_appearance_render_enumeration_failed",
                owner_path,
                "a relevant display-state render-material owner was not read successfully",
                call_state=owner.get("call_state"),
                errors=dict(owner["read_errors"]),
            )
        if zero_material_null:
            continue
        if owner.get("call_state") == "empty" and owner.get("materials"):
            raise _appearance_resolution_error(
                component,
                "cad_appearance_render_owner_invalid",
                owner_path,
                "an empty render-material call carries material rows",
            )
        if owner.get("call_state") == "observed" and not owner.get("materials"):
            raise _appearance_resolution_error(
                component,
                "cad_appearance_render_owner_invalid",
                owner_path,
                "an observed render-material call carries no material rows",
            )
        if (
            owner.get("call_state") == "observed"
            and owner.get("native_render_material_count")
            != len(owner["materials"])
        ) or (
            owner.get("call_state") == "empty"
            and owner.get("native_render_material_count") != 0
        ):
            raise _appearance_resolution_error(
                component,
                "cad_appearance_render_material_count_mismatch",
                owner_path + ".native_render_material_count",
                "the native render-material count disagrees with the material rows",
                native_render_material_count=owner.get(
                    "native_render_material_count"
                ),
                material_rows=(
                    len(owner["materials"])
                    if isinstance(owner.get("materials"), list)
                    else None
                ),
            )
        if owner["owner_scope"] == "component_document" and (
            owner["native_reference_index"] or owner["mapping_errors"]
        ):
            raise _appearance_resolution_error(
                component,
                "cad_appearance_render_owner_invalid",
                owner_path,
                (
                    "a component-document owner cannot carry a derived native "
                    "footprint index or mapping answer"
                ),
            )
        reference_index: dict[
            tuple[tuple[int, ...], tuple[str, str, str]],
            list[dict[str, Any]],
        ] = {}
        for index_position, indexed in enumerate(
            owner["native_reference_index"]
            if owner["owner_scope"] != "component_document"
            else ()
        ):
            index_path = (
                owner_path
                + f".native_reference_index[{index_position}]"
            )
            if not isinstance(indexed, Mapping):
                raise _appearance_resolution_error(
                    component,
                    "cad_appearance_render_entity_invalid",
                    index_path,
                    "a native reference index entry is malformed",
                )
            indexed_key = _persistent_reference_key(
                indexed.get("persistent_reference")
            )
            indexed_binding = indexed.get("binding_context")
            indexed_context = _binding_context_key(indexed_binding)
            indexed_api = (
                indexed_binding.get("api")
                if isinstance(indexed_binding, Mapping)
                else None
            )
            indexed_errors = indexed.get("read_errors")
            target = indexed.get("target")
            if (
                indexed_key is None
                or indexed_context is None
                or indexed_api
                != OWNER_CONTEXT_BINDING_API
                or indexed.get("native_interface_kind")
                not in {"IEntity", "IBody2", None}
                or not isinstance(indexed_errors, Mapping)
                or not isinstance(target, Mapping)
                or target.get("kind")
                not in {"occurrence", "body", "face", "feature"}
                or (
                    owner.get("owner_scope") != "component_document"
                    and target.get("kind") != "occurrence"
                )
            ):
                raise _appearance_resolution_error(
                    component,
                    "cad_appearance_render_entity_invalid",
                    index_path,
                    "an assembly-owned material index target is not an occurrence",
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
        for material_position, material in enumerate(owner["materials"]):
            path = f"{owner_path}.materials[{material_position}]"
            if not isinstance(material, Mapping):
                raise _appearance_resolution_error(
                    component,
                    "cad_appearance_render_material_invalid",
                    path,
                    "a display-state render material is not an object",
                )
            errors = material.get("read_errors")
            linked = material.get("linked_display_states")
            entities = material.get("entity_records")
            entities_state = material.get("entities_state")
            if (
                material.get("material_index") != material_position
                or not isinstance(errors, Mapping)
                or not isinstance(linked, (list, type(None)))
                or not isinstance(entities, list)
                or entities_state
                not in {"observed", "empty", "null_result", "error"}
            ):
                raise _appearance_resolution_error(
                    component,
                    "cad_appearance_render_material_invalid",
                    path,
                    "a display-state render material cannot be bound exactly",
                    errors=dict(errors) if isinstance(errors, Mapping) else errors,
                    linked_display_states=linked,
                )
            if entities_state in {"null_result", "error"}:
                raise _appearance_resolution_error(
                    component,
                    "cad_appearance_render_entities_unresolved",
                    path + ".entities_state",
                    "a display-state appearance has no readable entity inventory",
                    entities_state=entities_state,
                )
            if entities_state == "empty":
                continue
            if not entities:
                raise _appearance_resolution_error(
                    component,
                    "cad_appearance_render_entity_invalid",
                    path + ".entity_records",
                    "an observed appearance carries no entity records",
                )
            material_faces: set[tuple[int, int, int]] = set()
            faces_by_scope: dict[str, set[tuple[int, int, int]]] = {
                "occurrence": set(),
                "face": set(),
                "feature": set(),
                "body": set(),
                "document": set(),
            }
            for entity_index, entity in enumerate(entities):
                entity_path = f"{path}.entities[{entity_index}]"
                if (
                    not isinstance(entity, Mapping)
                    or entity.get("entity_index") != entity_index
                    or not isinstance(entity.get("native_object_type"), str)
                    or not isinstance(
                        entity.get("native_interface_kinds"), list
                    )
                    or entity["native_interface_kinds"]
                    != [
                        kind
                        for kind in APPEARANCE_OBJECT_INTERFACE_ORDER
                        if kind in entity["native_interface_kinds"]
                    ]
                    or not set(entity["native_interface_kinds"])
                    <= APPEARANCE_OBJECT_INTERFACE_KINDS
                    or not isinstance(entity.get("read_errors"), Mapping)
                ):
                    raise _appearance_resolution_error(
                        component,
                        "cad_appearance_render_entity_invalid",
                        entity_path,
                        "a display-state render-material entity cannot be bound exactly",
                    )
                context_key = _binding_context_key(
                    entity.get("binding_context")
                )
                if context_key is None or (
                    context_key[0] != owner.get("owner_scope")
                    or context_key[1]
                    != _document_path_key(owner.get("owner_document_path"))
                    or context_key[2] != component
                ):
                    raise _appearance_resolution_error(
                        component,
                        "cad_appearance_render_entity_binding_unproven",
                        entity_path + ".binding_context",
                        "a display-state entity has no exact current binding context",
                    )
                native_identity = entity.get("native_identity")
                document_identity = (
                    native_identity
                    if isinstance(native_identity, Mapping)
                    and native_identity.get("kind") == "document"
                    else None
                )
                if document_identity is not None:
                    if (
                        entity["read_errors"]
                        or context_key[0] != "component_document"
                        or not _same_document_path(
                            entity["binding_context"].get(
                                "owner_document_path"
                            ),
                            owner["owner_document_path"],
                        )
                        or entity["binding_context"].get("api")
                        != DOCUMENT_BINDING_API
                        or "IModelDoc2"
                        not in entity["native_interface_kinds"]
                        or document_identity.get("qi_iid")
                        != IMODELDOC2_IID
                        or document_identity.get("state") != "observed"
                        or document_identity.get("read_errors")
                        or _document_path_key(
                            document_identity.get("document_path")
                        )
                        != _document_path_key(expected_document_path)
                        or document_identity.get("active_configuration")
                        != configuration
                    ):
                        raise _appearance_resolution_error(
                            component,
                            "cad_appearance_render_entity_binding_unproven",
                            entity_path + ".native_identity",
                            "a document entity is not the exact current leaf target",
                            observed=dict(document_identity),
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
                        and _same_document_path(
                            reference_identity.get("owner_document_path"),
                            owner["owner_document_path"],
                        )
                        and reference_identity.get("interface_kinds")
                        == entity["native_interface_kinds"]
                        and not reference_identity.get("read_errors")
                    )
                    if not source_context_proven:
                        raise _appearance_resolution_error(
                            component,
                            "cad_appearance_render_entity_binding_unproven",
                            entity_path + ".persistent_reference",
                            "the material entity is not bound to its actual owner document",
                            observed={
                                "binding_context": entity["binding_context"],
                                "identity": native_identity,
                                "interface_kinds": entity[
                                    "native_interface_kinds"
                                ],
                                "persistent_reference": entity_key,
                                "read_errors": entity["read_errors"],
                            },
                            expected=owner_context,
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
                        if applicable_native_errors:
                            raise _appearance_resolution_error(
                                component,
                                "cad_appearance_render_identity_unresolved",
                                entity_path + ".persistent_reference",
                                (
                                    "a participating native object kind has "
                                    "unresolved relation evidence"
                                ),
                                errors=copy.deepcopy(
                                    applicable_native_errors
                                ),
                            )
                        targets = component_relations.get(entity_key, ())
                    else:
                        targets = reference_index.get(
                            (entity_key, context_key)
                        )
                    if not targets:
                        raise _appearance_resolution_error(
                            component,
                            "cad_appearance_render_entity_binding_unproven",
                            entity_path + ".persistent_reference",
                            "the native reference index does not bind this entity",
                            mapping_errors=copy.deepcopy(
                                owner["mapping_errors"]
                            ),
                        )
                    blocking_index_errors = [
                        item["read_errors"]
                        for item in targets
                        if item["read_errors"]
                        and set(item["read_errors"]) != {"interface_query"}
                    ]
                    if blocking_index_errors:
                        raise _appearance_resolution_error(
                            component,
                            "cad_appearance_render_identity_unresolved",
                            entity_path + ".persistent_reference",
                            "the matched native target has unresolved identity evidence",
                            errors=copy.deepcopy(blocking_index_errors),
                        )
                    current_targets = _unique_current_targets(
                        [item["target"] for item in targets],
                        component=component,
                        path=entity_path + ".persistent_reference",
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
                        if not target_faces <= all_face_keys:
                            raise _appearance_resolution_error(
                                component,
                                "cad_appearance_render_entity_binding_unproven",
                                entity_path + ".persistent_reference",
                                (
                                    "a native reference target contains a "
                                    "face outside the delivered geometry"
                                ),
                                target_kind=target["kind"],
                            )
                    target_interfaces = {
                        "occurrence": "IComponent2",
                        "body": "IBody2",
                        "face": "IFace2",
                        "feature": "IFeature",
                        "document": "IModelDoc2",
                    }
                    if any(
                        target_interfaces[target["kind"]]
                        not in entity["native_interface_kinds"]
                        for target in current_targets.values()
                    ):
                        raise _appearance_resolution_error(
                            component,
                            "cad_appearance_render_entity_interface_mismatch",
                            entity_path + ".native_interface_kinds",
                            (
                                "the material entity public interfaces do not "
                                "match its native target kinds"
                            ),
                            interface_kinds=entity[
                                "native_interface_kinds"
                            ],
                            target_kinds=sorted(
                                {
                                    target["kind"]
                                    for target in current_targets.values()
                                }
                            ),
                        )
                    for item in targets:
                        if item["target"] in current_targets.values():
                            _validate_index_conversion(
                                component,
                                item,
                                owner_document_path=owner[
                                    "owner_document_path"
                                ],
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
                    if applicable_errors:
                        raise _appearance_resolution_error(
                            component,
                            "cad_appearance_render_identity_unresolved",
                            owner_path + ".mapping_errors",
                            "a participating native target has unresolved identities",
                            errors=copy.deepcopy(applicable_errors),
                        )
                if not current_targets:
                    continue
                if all(
                    identity[0] in {"body", "feature"}
                    for identity in current_targets
                ):
                    target_kinds = {identity[0] for identity in current_targets}
                    if len(target_kinds) != 1:
                        raise _appearance_resolution_error(
                            component,
                            "cad_appearance_render_entity_ambiguous",
                            entity_path + ".persistent_reference",
                            "a native reference aliases body and feature targets",
                        )
                    target_kind = next(iter(target_kinds))
                    native_identities = set()
                    for target in current_targets.values():
                        native_identity = (
                            _feature_target_identity(target)
                            if target_kind == "feature"
                            else target.get("persistent_reference_sha256")
                        )
                        if native_identity is None:
                            raise _appearance_resolution_error(
                                component,
                                "cad_appearance_render_entity_invalid",
                                entity_path + ".persistent_reference",
                                "a body or feature target has no native identity",
                            )
                        native_identities.add(native_identity)
                    if len(native_identities) != 1:
                        raise _appearance_resolution_error(
                            component,
                            "cad_appearance_render_entity_ambiguous",
                            entity_path + ".persistent_reference",
                            (
                                "a native reference aliases multiple distinct "
                                f"{target_kind}s"
                            ),
                            native_identities=[
                                list(identity)
                                for identity in sorted(native_identities)
                                if isinstance(identity, tuple)
                            ],
                        )
                    for identity_key in current_targets:
                        faces_by_scope[target_kind].update(
                            target_faces_by_identity[identity_key]
                        )
                    continue
                if len(current_targets) > 1:
                    raise _appearance_resolution_error(
                        component,
                        "cad_appearance_render_entity_ambiguous",
                        entity_path + ".persistent_reference",
                        "a native reference aliases multiple distinct current targets",
                        targets=[
                            {
                                "kind": identity[0],
                                "component": identity[1],
                                "body_type": identity[2],
                                "body_index": identity[3],
                                "face_index": identity[4],
                            }
                            for identity in sorted(current_targets)
                        ],
                    )
                kind, _component, body_type, body_index, face_index = next(
                    iter(current_targets)
                )
                if kind == "occurrence":
                    faces_by_scope["occurrence"] |= set(all_faces)
                    continue
                if kind == "document":
                    faces_by_scope["document"] |= set(all_faces)
                    continue
                face_key = (body_type, body_index, face_index)
                if face_key in all_faces:
                    faces_by_scope[kind].add(face_key)
            for scoped_faces in faces_by_scope.values():
                material_faces |= scoped_faces
            if not material_faces:
                continue
            if (
                errors
                or not isinstance(linked, list)
                or owner_display_state not in linked
                or any(entity.get("read_errors") for entity in entities)
                or material.get("color_form") != 1
                or material.get("texture_filename") not in (None, "")
                or material.get("surface_finish_file") not in (None, "")
            ):
                raise _appearance_resolution_error(
                    component,
                    "cad_appearance_render_material_unsupported",
                    path,
                    "a participating display-state appearance is not a qualified solid color",
                    errors=dict(errors),
                    observed=dict(material),
                )
            rgba = _rgba_from_colorref(
                material.get("primary_color"),
                material.get("transparency"),
            )
            if rgba is None:
                raise _appearance_resolution_error(
                    component,
                    "cad_appearance_render_material_unsupported",
                    path,
                    "a participating display-state appearance has invalid color values",
                    primary_color=material.get("primary_color"),
                    transparency=material.get("transparency"),
                )
            parsed_materials.append(
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
                        "material_index": material_position,
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

    resolved = []
    for key, span in zip(all_faces, spans, strict=True):
        body_key = key[:2]
        body_owner_raw, body_faces_raw = body_owners[body_key]
        selected = None
        for scope in APPEARANCE_SCOPE_ORDER:
            choices = []
            if scope == "occurrence" and occurrence_owner is not None:
                owner = _owner_color(
                    occurrence_owner,
                    component=component,
                    path="appearance_raw.occurrence",
                    expected_configuration=configuration,
                )
                if owner is not None:
                    choices.append({"rgba": owner[0], "source": owner[1]})
            elif scope == "face":
                owner = _owner_color(
                    body_faces_raw[key],
                    component=component,
                    path=(
                        "appearance_raw.bodies"
                        f"[{body_key[0]}/{body_key[1]}]"
                        f".faces[{key[2]}].appearance"
                    ),
                    expected_configuration=configuration,
                )
                if owner is not None:
                    choices.append({"rgba": owner[0], "source": owner[1]})
            elif scope == "body":
                owner = (
                    None
                    if body_owner_raw is None
                    else _owner_color(
                        body_owner_raw,
                        component=component,
                        path=(
                            "appearance_raw.bodies"
                            f"[{body_key[0]}/{body_key[1]}].appearance"
                        ),
                        expected_configuration=configuration,
                    )
                )
                if owner is not None:
                    choices.append({"rgba": owner[0], "source": owner[1]})
            elif scope == "document":
                choices.extend(
                    {
                        "rgba": material["rgba"],
                        "source": material["source"],
                    }
                    for material in parsed_materials
                    if key in material["faces_by_scope"].get(scope, set())
                )
                if not choices:
                    owner = _owner_color(
                        document_owner,
                        component=component,
                        path="appearance_raw.document",
                        expected_configuration=configuration,
                        use_presence=False,
                    )
                    if owner is not None:
                        choices.append({"rgba": owner[0], "source": owner[1]})
            if scope != "document":
                choices.extend(
                    {
                        "rgba": material["rgba"],
                        "source": material["source"],
                    }
                    for material in parsed_materials
                    if key in material["faces_by_scope"].get(scope, set())
                )
            if not choices:
                continue
            colors = {
                tuple(float(value) for value in choice["rgba"])
                for choice in choices
            }
            selected_rgba = scoped_rgba(
                [choice["rgba"] for choice in choices]
            )
            if selected_rgba is None:
                code = (
                    "cad_appearance_ancestor_precedence_unproven"
                    if scope == "occurrence"
                    else "cad_appearance_owner_conflict"
                )
                raise _appearance_resolution_error(
                    component,
                    code,
                    f"face:{body_key[0]}/{body_key[1]}/{key[2]}",
                    "applicable appearances disagree at one proven hierarchy scope",
                    rgba=[list(color) for color in sorted(colors)],
                    sources=[choice["source"] for choice in choices],
                )
            selected = {
                "rgba": selected_rgba,
                "sources": [choice["source"] for choice in choices],
                "scopes": [scope],
            }
            break
        if selected is None:
            raise _appearance_resolution_error(
                component,
                "cad_appearance_effective_face_unresolved",
                f"face:{body_key[0]}/{body_key[1]}/{key[2]}",
                "no recorded native owner or display-state material resolves this face",
            )
        resolved.append(
            FaceAppearance(
                body_type=key[0],
                body_index=key[1],
                face_index=key[2],
                face_reference_sha256=str(span["face_reference_sha256"]),
                triangle_start=int(span["triangle_start"]),
                triangle_count=int(span["triangle_count"]),
                rgba=selected["rgba"],
                source={
                    "scopes": selected["scopes"],
                    "configuration": configuration,
                    "display_state": display_state,
                    "owners": selected["sources"],
                },
            )
        )
    return AppearanceResolution(
        component=component,
        referenced_configuration=configuration,
        referenced_display_state=display_state,
        faces=tuple(resolved),
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
    geometry_raw = native_facts.get("geometry_raw")
    if (
        not isinstance(occurrences, list)
        or not isinstance(source_scopes, Mapping)
        or not isinstance(geometry_raw, Mapping)
    ):
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
    attached_occurrences: set[str] = set()
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
        geometry = geometry_raw.get(name)
        if not isinstance(geometry, Mapping):
            raise _invalid_appearance(
                name,
                "cad_appearance_geometry_missing",
                f"geometry_raw.{name}",
                "a native occurrence has no sealed geometry appearance",
            )
        appearance = geometry.get("appearance_raw")
        scope = source_scopes.get(name)
        if appearance is None:
            continue
        if not isinstance(appearance, Mapping) or not isinstance(
            scope,
            Mapping,
        ):
            raise _invalid_appearance(
                name,
                "cad_appearance_native_facts_invalid",
                f"geometry_raw.{name}.appearance_raw",
                "a native occurrence has no sealed appearance or source scope",
            )
        component["appearance_raw"] = {
            "component": name,
            "source_document_path": scope.get("source_document_path"),
            "referenced_configuration": scope.get(
                "referenced_configuration"
            ),
            **copy.deepcopy(dict(appearance)),
        }
        attached_occurrences.add(name)
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
