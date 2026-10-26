"""Pure structural checks for native appearance conversion evidence."""

from __future__ import annotations

import hashlib
import math
import posixpath
from collections.abc import Mapping, Sequence

RGBA_ATOL = 1e-12
APPEARANCE_OBJECT_INTERFACE_ORDER = (
    "IModelDoc2",
    "IPartDoc",
    "IComponent2",
    "IBody2",
    "IFeature",
    "IFace2",
    "IEntity",
)
APPEARANCE_OBJECT_INTERFACE_KINDS = frozenset(
    APPEARANCE_OBJECT_INTERFACE_ORDER
)


def _rgba_values(value: object) -> tuple[float, float, float, float]:
    """Validate and return one unquantized RGBA tuple."""

    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes))
        or len(value) != 4
    ):
        raise ValueError("RGBA must contain four channels")
    channels = []
    for item in value:
        if type(item) not in (int, float):
            raise ValueError("RGBA channels must be numeric")
        channel = float(item)
        if not math.isfinite(channel) or not 0 <= channel <= 1:
            raise ValueError("RGBA channels must be finite and within [0, 1]")
        channels.append(channel)
    return tuple(channels)


def scoped_rgba(values: Sequence[object]) -> tuple[float, float, float, float] | None:
    """Return one observed RGBA when every channel agrees within the bound."""

    if (
        not isinstance(values, Sequence)
        or isinstance(values, (str, bytes))
        or not values
    ):
        raise ValueError("At least one RGBA candidate is required")
    candidates = tuple(_rgba_values(value) for value in values)
    if any(
        max(candidate[index] for candidate in candidates)
        - min(candidate[index] for candidate in candidates)
        > RGBA_ATOL
        for index in range(4)
    ):
        return None
    return min(candidates)


def _path_key(value):
    if not isinstance(value, str) or not value:
        return None
    return posixpath.normpath(value.replace("\\", "/")).casefold()


def index_conversion_mismatch(
    indexed,
    *,
    component,
    owner_document_path,
):
    """Return expected/observed values when one target's evidence is invalid."""

    if not isinstance(indexed, Mapping):
        return {"error": "index row is not an object"}
    target = indexed.get("target")
    conversion = indexed.get("conversion")
    native_interface_kind = indexed.get("native_interface_kind")
    if not isinstance(target, Mapping):
        return {"error": "target is not an object"}
    kind = target.get("kind")
    if kind == "occurrence":
        if conversion is None and native_interface_kind is None:
            return None
        return {
            "expected": {
                "conversion": None,
                "native_interface_kind": None,
            },
            "observed": {
                "conversion": conversion,
                "native_interface_kind": native_interface_kind,
            },
        }
    expected_source_api = (
        "IComponent2.GetBodies3/IBody2.GetFaces"
        if kind in {"body", "face", "feature"}
        else "IComponent2.GetBodies3"
    )
    expected_api = {
        "body": (
            "IFace2.GetBody+IModelDocExtension.GetPersistReference3"
        ),
        "face": "IModelDocExtension.GetCorrespondingEntity2",
        "feature": (
            "IFace2.GetFeature+"
            "IModelDocExtension.GetPersistReference3"
        ),
    }.get(kind)
    expected_interface = {
        "body": "IBody2",
        "face": "IFace2",
        "feature": "IFeature",
    }.get(kind)
    expected_indexed_interface = {
        "body": "IBody2",
        "face": "IEntity",
        "feature": None,
    }.get(kind)
    if expected_api is None:
        return {"error": f"unsupported target kind: {kind!r}"}
    input_context = (
        conversion.get("input_context")
        if isinstance(conversion, Mapping)
        else None
    )
    receiver = (
        conversion.get("receiver")
        if isinstance(conversion, Mapping)
        else None
    )
    output_domain = (
        conversion.get("output_domain")
        if isinstance(conversion, Mapping)
        else None
    )
    expected = {
        "source_api": expected_source_api,
        "api": expected_api,
        "output_interface_kind": expected_interface,
        "native_interface_kind": expected_indexed_interface,
        "owner_document_path": owner_document_path,
    }
    body_target_reference_matches = True
    if kind == "body":
        reference = indexed.get("persistent_reference")
        body_target_reference_matches = (
            isinstance(reference, list)
            and bool(reference)
            and all(type(item) is int for item in reference)
            and target.get("persistent_reference_sha256")
            == hashlib.sha256(
                bytes(item & 0xFF for item in reference)
            ).hexdigest()
        )
    valid = (
        body_target_reference_matches
        and
        native_interface_kind == expected_indexed_interface
        and isinstance(conversion, Mapping)
        and conversion.get("component") == component
        and conversion.get("api") == expected_api
        and conversion.get("output_interface_kind") == expected_interface
        and conversion.get("state") == "observed"
        and not conversion.get("read_errors")
        and isinstance(input_context, Mapping)
        and input_context.get("owner_scope") == "assembly_occurrence"
        and input_context.get("object_kind") == "assembly_occurrence"
        and input_context.get("source_api") == expected_source_api
        and input_context.get("traversal_api")
        == "IConfiguration.GetRootComponent3/IComponent2.GetChildren"
        and isinstance(receiver, Mapping)
        and receiver.get("owner_scope") == "component_document"
        and _path_key(receiver.get("owner_document_path"))
        == _path_key(owner_document_path)
        and receiver.get("interface") == "IModelDocExtension"
        and receiver.get("object_source")
        == "captured_component_source_document"
        and isinstance(output_domain, Mapping)
        and output_domain.get("proof_api")
        == "IModelDocExtension.GetObjectByPersistReference3"
        and type(output_domain.get("status")) is int
        and output_domain.get("status") == 0
        and output_domain.get("interface_kind") == expected_interface
        and _path_key(output_domain.get("expected_owner_document_path"))
        == _path_key(owner_document_path)
    )
    if valid:
        return None
    return {
        "expected": expected,
        "observed": {
            "native_interface_kind": native_interface_kind,
            "conversion": conversion,
        },
    }
