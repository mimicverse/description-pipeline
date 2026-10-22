"""Schema checks for one raw native position-limit observation."""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping

NATIVE_LIMIT_KEYS = frozenset(
    {
        "type_index",
        "definition_interface",
        "definition_interface_iid",
        "nominal_dimension",
        "minimum_variation",
        "maximum_variation",
        "minimum_limit",
        "maximum_limit",
    }
)

_VALUE_KEYS = frozenset({"value", "api", "unit"})
_ADVANCED_MATE_APIS = {
    "distance": "IDistanceMateFeatureData.IsAdvancedMate",
    "angle": "IAngleMateFeatureData.IsAdvancedMate",
    "limitdistance": "IDistanceMateFeatureData.IsAdvancedMate",
    "limitangle": "IAngleMateFeatureData.IsAdvancedMate",
}
_POSITION_LIMIT_TYPES = frozenset({"limitdistance", "limitangle"})
_REQUIRED_ACTIVE_MATE_KEYS = frozenset(
    {
        "limits",
        "native_limit",
        "is_advanced_mate",
        "is_advanced_mate_error",
        "is_advanced_mate_api",
    }
)
_LIMIT_TYPES = {
    5: {
        "kind": "limitdistance",
        "interface": "IDistanceMateFeatureData",
        "iid": "{943EED8D-E13B-4459-BD59-EBCC3F60B915}",
        "nominal": "Distance",
        "minimum": "MinimumDistance",
        "maximum": "MaximumDistance",
        "unit": "m",
    },
    6: {
        "kind": "limitangle",
        "interface": "IAngleMateFeatureData",
        "iid": "{EF4303D7-B4FD-46E7-AD34-D942C420E096}",
        "nominal": "Angle",
        "minimum": "MinimumAngle",
        "maximum": "MaximumAngle",
        "unit": "rad",
    },
}

INSPECTION_PREVIEW_POLICY_ID = "inspection_rotation_full_turn/v1"
ENGINEERING_DEFAULT_POLICY_ID = "engineering_rotation_default_window/v1"
ROTATION_WINDOW_RAD = math.pi

# Existing consumers and persisted evidence use these inspection names.
PREVIEW_POLICY_ID = INSPECTION_PREVIEW_POLICY_ID
PREVIEW_WINDOW_RAD = ROTATION_WINDOW_RAD


def rotation_window_policy(
    rule_id: str,
    *,
    author_limits: object,
    native_absence: str,
    native_errors: list[dict] | None = None,
) -> dict:
    """Build the one versioned rotation-window policy used by both purposes."""

    if rule_id not in {INSPECTION_PREVIEW_POLICY_ID, ENGINEERING_DEFAULT_POLICY_ID}:
        raise ValueError("unknown rotation-window policy")
    policy = {
        "rule_id": rule_id,
        "rule_version": "v1",
        "window_rad": [-ROTATION_WINDOW_RAD, ROTATION_WINDOW_RAD],
        "native_cad_ranges": False,
        "hardware_qualified": False,
        "author_limits": dict(author_limits) if isinstance(author_limits, Mapping) else author_limits,
        "native_absence": native_absence,
    }
    if rule_id == ENGINEERING_DEFAULT_POLICY_ID:
        policy["engineering_position_range_qualified"] = False
    if native_errors is not None:
        policy["native_observation_errors"] = [dict(item) for item in native_errors]
    return policy


def native_limit_inventory_complete(mates) -> bool:
    """Whether the raw mate inventory proves no enabled native position limit."""

    if not isinstance(mates, list):
        return False
    for mate in mates:
        if not isinstance(mate, Mapping):
            return False
        if type(mate.get("suppressed")) is not bool:
            return False
        if mate["suppressed"]:
            continue
        if not set(mate) >= _REQUIRED_ACTIVE_MATE_KEYS:
            return False
        if type(mate.get("error_code")) is not int or mate["error_code"] != 0:
            return False
        if mate["native_limit"] is not None or mate["limits"] is not None:
            return False
        if not isinstance(mate.get("type"), str) or not mate["type"].strip():
            return False
        mate_type = mate["type"].strip().lower()
        if mate["is_advanced_mate_error"] is not None:
            return False
        flag = mate["is_advanced_mate"]
        if mate_type in _POSITION_LIMIT_TYPES:
            # An active position-limit type must either be enabled (so the
            # inventory proves a range) or claim false, which conflicts with
            # its native type. Neither state proves that no limit is enabled.
            return False
        if mate_type in _ADVANCED_MATE_APIS:
            if type(flag) is not bool or flag is True:
                return False
            if mate["is_advanced_mate_api"] != _ADVANCED_MATE_APIS[mate_type]:
                return False
        elif flag is not None or mate["is_advanced_mate_api"] is not None:
            # On a mate type that cannot carry an advanced limit, both fields
            # must be the explicit non-applicable null.  A corrupted type
            # (string, number, object, list) is not absence evidence.
            return False
    return True


def _finite(value) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("native limit number is not numeric")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("native limit number is not finite")
    return number


def _value_record(record, key: str, expected_api: str, unit: str) -> float:
    if not isinstance(record, Mapping) or set(record) != _VALUE_KEYS:
        raise ValueError(f"{key} has an invalid native value record")
    if record.get("api") != expected_api or record.get("unit") != unit:
        raise ValueError(f"{key} has another API or unit")
    return _finite(record.get("value"))


def _binding(entity, index: int) -> tuple[str, str | None, int | None]:
    if not isinstance(entity, Mapping) or entity.get("entity_index") != index:
        raise ValueError("limit mate entity order is not a complete two-entity binding")
    component = entity.get("component")
    if not isinstance(component, str):
        raise ValueError("limit mate entity has no component identity")
    if component == "":
        if entity.get("assembly_frame") is not True:
            raise ValueError("an empty component identity is not a proven assembly frame")
    elif not isinstance(entity.get("reference_configuration"), str) or not entity["reference_configuration"]:
        raise ValueError("limit mate entity has no referenced configuration")
    reference = entity.get("mate_entity_reference")
    if not isinstance(reference, Mapping) or reference.get("error") is not None:
        raise ValueError("limit mate entity has no complete native entity reference")
    feature = entity.get("feature")
    face_index = entity.get("face_index")
    if not ((isinstance(feature, str) and feature) or type(face_index) is int):
        raise ValueError("limit mate entity has no feature or face selector")
    return (
        component,
        feature if isinstance(feature, str) and feature else None,
        face_index if type(face_index) is int else None,
    )


def limit_entity_owners(
    mate: Mapping,
    owner_for: Callable[[str], str | None],
    *,
    assembly_owner: str | None = None,
) -> tuple[str, str] | None:
    """Resolve both mate entities to declared bodies, or return ``None``."""

    entities = mate.get("entities")
    if not isinstance(entities, list) or len(entities) != 2:
        return None
    owners: list[str] = []
    for entity in entities:
        component = entity.get("component") if isinstance(entity, Mapping) else None
        if component == "":
            owner = assembly_owner if entity.get("assembly_frame") is True else None
        else:
            owner = owner_for(component) if isinstance(component, str) and component else None
        if owner is None:
            return None
        owners.append(owner)
    return owners[0], owners[1]


def limit_mate_scope(
    mate: Mapping,
    owner_for: Callable[[str], str | None],
    child_tree: set[str],
    joint_name: str,
    *,
    parent_owner: str,
    assembly_owner: str | None = None,
) -> str:
    """Classify a limit candidate before validating its units or evidence.

    ``claimed_unreadable`` and ``claimed_same_side`` belong to the named joint
    and must fail closed.  ``crossing`` is relevant by native entity boundary,
    even when its mate name differs.  ``unrelated`` belongs to another cut and
    must not poison this joint.
    """

    name_match = str(mate.get("name") or "").split("__", 1)[0] == str(joint_name)
    owners = limit_entity_owners(mate, owner_for, assembly_owner=assembly_owner)
    if owners is None:
        return "claimed_unreadable" if name_match else "unrelated"
    if (owners[0] == parent_owner and owners[1] in child_tree) or (
        owners[1] == parent_owner and owners[0] in child_tree
    ):
        return "crossing"
    return "claimed_same_side" if name_match else "unrelated"


def validate_limit_entity_topology(
    mate: Mapping,
    owner_for: Callable[[str], str | None],
    child_tree: set[str],
    *,
    parent_owner: str,
    assembly_owner: str | None = None,
) -> tuple[str, str]:
    """Prove the two limit entities sit on opposite sides of the joint.

    ``owner_for`` maps one raw component occurrence to exactly one declared
    body; ``child_tree`` is the declared child body plus its descendants.  One
    entity must be inside that subtree and the other outside it.  A syntactically
    complete mate that connects two occurrences on the same side is not a limit
    for this joint.
    """

    owners = limit_entity_owners(mate, owner_for, assembly_owner=assembly_owner)
    if owners is None:
        raise ValueError("native limit entity is not owned by exactly one declared body")
    if not (
        (owners[0] == parent_owner and owners[1] in child_tree)
        or (owners[1] == parent_owner and owners[0] in child_tree)
    ):
        raise ValueError("native limit entities do not span the declared parent/child subtrees")
    return owners[0], owners[1]


def validate_native_limit(
    mate: Mapping, components: Mapping[str, Mapping] | None = None
) -> tuple[float, float, str]:
    """Validate one raw ``native_limit`` and return its absolute bounds and unit.

    The reader owns the vendor calls; this pure function only checks the
    serialized evidence.  It refuses the legacy shorthand where ``limits`` or
    a bare ``native_limit.type_index`` was accepted without nominal,
    variation, interface and entity-binding evidence.
    """

    if not isinstance(mate, Mapping):
        raise ValueError("native limit mate is not an object")
    native_limit = mate.get("native_limit")
    type_index = native_limit.get("type_index") if isinstance(native_limit, Mapping) else None
    spec = _LIMIT_TYPES.get(type_index)
    if spec is None or mate.get("type") != spec["kind"]:
        raise ValueError("native limit type and type_index do not agree")
    if mate.get("is_advanced_mate") is not True or mate.get("is_advanced_mate_error") is not None:
        raise ValueError("native limit flag is not a clean true reading")
    if mate.get("is_advanced_mate_api") != f"{spec['interface']}.IsAdvancedMate":
        raise ValueError("native limit flag came from another interface")
    native = mate["native_limit"]
    if set(native) != NATIVE_LIMIT_KEYS:
        raise ValueError("native limit evidence is incomplete")
    if (
        native.get("definition_interface") != spec["interface"]
        or native.get("definition_interface_iid") != spec["iid"]
    ):
        raise ValueError("native limit definition interface does not match its type")
    unit = spec["unit"]
    nominal = _value_record(
        native["nominal_dimension"],
        "nominal_dimension",
        f"{spec['interface']}.{spec['nominal']}",
        unit,
    )
    minimum_variation = _value_record(
        native["minimum_variation"], "minimum_variation", "IMate2.MinimumVariation", unit
    )
    maximum_variation = _value_record(
        native["maximum_variation"], "maximum_variation", "IMate2.MaximumVariation", unit
    )
    minimum = _value_record(
        native["minimum_limit"], "minimum_limit", f"{spec['interface']}.{spec['minimum']}", unit
    )
    maximum = _value_record(
        native["maximum_limit"], "maximum_limit", f"{spec['interface']}.{spec['maximum']}", unit
    )
    if abs(nominal + minimum_variation - minimum) > 1e-12:
        raise ValueError("minimum variation does not reconstruct the minimum limit")
    if abs(nominal + maximum_variation - maximum) > 1e-12:
        raise ValueError("maximum variation does not reconstruct the maximum limit")
    if maximum < minimum:
        raise ValueError("native limit bounds are inverted")
    limits = mate.get("limits")
    if (
        not isinstance(limits, Mapping)
        or set(limits) != {"lower", "upper", "unit"}
        or limits.get("unit") != unit
        or _finite(limits.get("lower")) != minimum
        or _finite(limits.get("upper")) != maximum
    ):
        raise ValueError("legacy limits do not equal the validated native limit")
    entities = mate.get("entities")
    if not isinstance(entities, list) or len(entities) != 2:
        raise ValueError("native limit must bind exactly two mate entities")
    bindings = [_binding(entity, index) for index, entity in enumerate(entities)]
    if len(set(bindings)) != 2:
        raise ValueError("native limit repeats one mate entity binding")
    if components is not None:
        for entity in entities:
            component = entity.get("component")
            if component and (
                component not in components
                or components[component].get("configuration") != entity.get("reference_configuration")
            ):
                raise ValueError("limit mate entity configuration does not match its component")
    return minimum, maximum, unit
