"""Deterministic interface names derived from native CAD object identities."""

from __future__ import annotations

import hashlib
import math
import re
import unicodedata

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SIDE = (("l_", "left_"), ("r_", "right_"))
_FRAME_SUFFIXES = ("_mount", "_tcp", "_optical", "_frame")
_VOCABULARY = (
    ("\u4e0a\u81c2", "upper_arm"),
    ("\u524d\u81c2", "forearm"),
    ("\u5927\u817f", "thigh"),
    ("\u5c0f\u817f", "shank"),
    ("\u5173\u8282", "joint"),
    ("\u8fde\u6746", "link"),
    ("\u624b\u81c2", "arm"),
    ("\u8eaf\u5e72", "torso"),
    ("\u80a9", "shoulder"),
    ("\u8098", "elbow"),
    ("\u8155", "wrist"),
    ("\u9acb", "hip"),
    ("\u819d", "knee"),
    ("\u8e1d", "ankle"),
    ("\u5de6", "left"),
    ("\u53f3", "right"),
    ("\u81c2", "arm"),
    ("\u624b", "hand"),
    ("\u817f", "leg"),
    ("\u8db3", "foot"),
    ("\u5934", "head"),
    ("\u9888", "neck"),
    ("\u8170", "waist"),
)
_VOCABULARY_RE = re.compile("|".join(re.escape(source) for source, _ in _VOCABULARY))

#: Functional joint roles and the child segment selected by their outgoing
#: moving-joint topology.  The role is a native CAD name; the topology proves
#: which physical segment follows it.
_SEGMENT_BY_ROLE = {
    "elbow": ("forearm", ("wrist_pitch",)),
    "wrist_pitch": ("hand", ()),
    "shoulder_roll": ("upper_arm", ("shoulder_yaw",)),
    "waist": ("pelvis", ("thigh_yaw",)),
    "thigh_yaw": ("hip_yaw", ("thigh_pitch",)),
    "thigh_pitch": ("thigh", ("thigh_roll",)),
    "thigh_roll": ("hip_roll", ("knee",)),
    "knee": ("shank", ("ankle",)),
    "ankle": ("foot", ()),
    "head_yaw": ("neck_yaw", ("head_pitch",)),
    "head_pitch": ("head", ()),
}

_JOINT_BY_ROLE = {
    "thigh_yaw": "hip_yaw",
    "thigh_pitch": "hip_pitch",
    "thigh_roll": "hip_roll",
    "head_yaw": "neck_yaw",
    "head_pitch": "neck_pitch",
}

_ROLE_SEGMENT_RULE = {
    "rule_id": "naming.role_segment",
    "rule_version": "naming-v1",
    "reason": "joint-centric datum plus child-side moving-joint topology",
}

_ROLE_JOINT_RULE = {
    "rule_id": "naming.role_joint",
    "rule_version": "naming-v1",
    "reason": "native joint function plus child-side moving-joint topology",
}

_FUNCTIONAL_MOTION_RULE = {
    "rule_id": "naming.functional_motion",
    "rule_version": "v1",
    "reason": "native role, child topology and parallel signed CAD axes prove pitch motion",
}

_FUNCTIONAL_PITCH_REFERENCES = {
    "elbow": ("same_side", "wrist_pitch"),
    "knee": ("same_side", "thigh_pitch"),
    "ankle": ("same_side", "thigh_pitch"),
    "waist": ("both_sides", "thigh_pitch"),
}

_FUNCTIONAL_JOINT_BY_ROLE = {
    "elbow": "elbow_pitch",
    "knee": "knee_pitch",
    "ankle": "ankle_pitch",
    "waist": "waist_pitch",
}

_ROLE_VOCABULARY = frozenset(_SEGMENT_BY_ROLE) | frozenset(
    role for _segment, outgoing in _SEGMENT_BY_ROLE.values() for role in outgoing
)


def token(value: str) -> str:
    """Normalize one native name into the single output snake_case vocabulary."""

    text = str(value)
    text = _VOCABULARY_RE.sub(lambda match: f"_{dict(_VOCABULARY)[match.group(0)]}_", text)
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", text)
    text = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", "_", text)
    normalized = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    normalized = re.sub(r"[^A-Za-z0-9]+", "_", normalized).strip("_").lower()
    normalized = re.sub(r"_+", "_", normalized)
    if not normalized:
        raise ValueError("native name has no ASCII interface token")
    if normalized[0].isdigit():
        normalized = "n_" + normalized
    for prefix, replacement in _SIDE:
        if normalized == prefix[:-1]:
            break
        if normalized.startswith(prefix):
            normalized = replacement + normalized[len(prefix) :]
            break
    return normalized


def link_name(native_name: str, *, root: bool = False) -> str:
    source = re.sub(r"^(?:c_|cs_)", "", str(native_name), flags=re.IGNORECASE)
    name = token(source)
    if root or name == "base_link":
        return "base_link"
    return name if name.endswith("_link") else name + "_link"


def joint_name(native_name: str) -> str:
    source = re.sub(r"^(?:a_|axis_)", "", str(native_name), flags=re.IGNORECASE)
    name = token(source)
    stem = name[: -len("_joint")] if name.endswith("_joint") else name
    return stem + "_joint"


def _role_token(native_name: str) -> tuple[str, str] | None:
    """Return ``(side_prefix, role)`` for a recognised native joint role."""

    source = re.sub(r"^(?:c_|cs_|a_|axis_)", "", str(native_name), flags=re.IGNORECASE)
    try:
        name = token(source)
    except ValueError:
        return None
    side = ""
    for prefix in ("left_", "right_"):
        if name.startswith(prefix):
            side = prefix
            name = name[len(prefix) :]
            break
    for role in sorted(_ROLE_VOCABULARY, key=len, reverse=True):
        if name == role:
            return side, role
    return None


def _segment_for(
    role: str,
    outgoing: list[tuple[str, str] | None],
    *,
    side: str,
) -> str | None:
    # An unrecognised outgoing joint is unknown topology, never absence: it
    # cannot strengthen any segment proof (including a terminal role).
    if any(entry is None for entry in outgoing):
        return None
    entries = list(outgoing)
    segment, expected = _SEGMENT_BY_ROLE[role]
    if not expected:
        return segment if not entries else None
    if role == "waist":
        # A pelvis rename requires a proven leg on both sides: exactly two
        # thigh_yaw joints, one left and one right.  A single leg or a
        # duplicated same-side entry never proves the segment.
        sides = sorted(item_side for item_side, item_role in entries if item_role == "thigh_yaw")
        if len(entries) != 2 or len(sides) != 2 or sides != ["left_", "right_"]:
            return None
        return segment
    if len(entries) != len(expected) or [item_role for _item_side, item_role in entries] != list(expected):
        return None
    if any(item_side != side for item_side, _item_role in entries):
        return None
    return segment


def role_link_name(
    native_joint: str,
    outgoing: list[tuple[str, str] | None],
) -> str | None:
    """Return the semantic child segment for a native joint role, if proven."""

    parsed = _role_token(native_joint)
    if parsed is None or parsed[1] not in _SEGMENT_BY_ROLE:
        return None
    side, role = parsed
    segment = _segment_for(role, outgoing, side=side)
    return f"{side}{segment}_link" if segment is not None else None


def role_joint_name(
    native_joint: str,
    outgoing: list[tuple[str, str] | None],
) -> str | None:
    """Return the semantic joint name for a native function role, if proven."""

    parsed = _role_token(native_joint)
    if parsed is None or parsed[1] not in _JOINT_BY_ROLE:
        return None
    side, role = parsed
    if _segment_for(role, outgoing, side=side) is None:
        return None
    return f"{side}{_JOINT_BY_ROLE[role]}_joint"


def _axis_unit(joint: dict) -> tuple[float, float, float] | None:
    axis = joint.get("axis")
    provenance = joint.get("axis_provenance")
    values = axis.get("assembly_direction") if isinstance(axis, dict) else None
    if values is None and isinstance(provenance, dict):
        values = provenance.get("assembly_direction")
    if not isinstance(values, (list, tuple)) or len(values) != 3:
        return None
    try:
        vector = tuple(float(value) for value in values)
    except (TypeError, ValueError):
        return None
    length = sum(value * value for value in vector) ** 0.5
    if not all(math.isfinite(value) for value in vector) or length <= 1e-12:
        return None
    return tuple(value / length for value in vector)


def _functional_pitch_proof(
    native_joint: str,
    outgoing: list[tuple[str, str] | None],
    by_role_side: dict[tuple[str, str], list[dict]],
) -> dict | None:
    parsed = _role_token(native_joint)
    if parsed is None or parsed[1] not in _FUNCTIONAL_PITCH_REFERENCES:
        return None
    side, role = parsed
    if _segment_for(role, outgoing, side=side) is None:
        return None
    current = by_role_side.get((side, role))
    if current is None or len(current) != 1:
        return None
    joint_axis = _axis_unit(current[0])
    if joint_axis is None:
        return None
    scope, reference_role = _FUNCTIONAL_PITCH_REFERENCES[role]
    sides = ("left_", "right_") if scope == "both_sides" else (side,)
    references = []
    for reference_side in sides:
        matches = by_role_side.get((reference_side, reference_role))
        if matches is None or len(matches) != 1:
            return None
        reference_axis = _axis_unit(matches[0])
        if reference_axis is None:
            return None
        alignment = abs(sum(left * right for left, right in zip(joint_axis, reference_axis, strict=True)))
        if alignment < 1.0 - 1e-8:
            return None
        references.append(
            {
                "native": str(matches[0].get("name") or ""),
                "role": reference_role,
                "assembly_direction": list(reference_axis),
                "abs_dot": alignment,
            }
        )
    return {
        **_FUNCTIONAL_MOTION_RULE,
        "role": role,
        "side": side,
        "joint_assembly_direction": list(joint_axis),
        "references": references,
        "tolerance": 1e-8,
    }


def joint_role(native_joint: str) -> str | None:
    """Return the recognised function role of a native joint name."""

    parsed = _role_token(native_joint)
    return parsed[1] if parsed is not None else None


def joint_role_pair(native_joint: str) -> tuple[str, str] | None:
    """Return the native joint's ``(side, role)`` pair."""

    return _role_token(native_joint)


def fixed_joint_name(parent: str, child: str) -> str:
    """Fixed interface name from the two normalized endpoint names."""

    return f"{token(parent)}_to_{token(child)}_joint"


def frame_name(native_name: str) -> str:
    """Normalize a carrier datum while retaining its interface purpose."""

    normalized_full = token(str(native_name))
    carrier, separator, suffix = str(native_name).partition("_")
    if carrier.upper() not in {"CS", "TCP", "SCS"}:
        if normalized_full.endswith(_FRAME_SUFFIXES):
            return normalized_full
        return normalized_full + "_frame"
    if not separator or not suffix:
        raise ValueError("native interface datum has no carrier and suffix")
    name = token(suffix)
    if name.endswith(_FRAME_SUFFIXES):
        return name
    if carrier.upper() == "TCP":
        return name + "_tcp"
    return name + "_frame"


def frame_identity(owner: str, name: str, configuration: str = "") -> str:
    """Stable native identity for one interface datum."""

    return "\x00".join((str(owner), str(name), str(configuration)))


def disambiguated_frame_name(base_name: str, native_identity: str) -> str:
    """Add a stable identity without discarding the frame's purpose suffix."""

    role = next((suffix for suffix in _FRAME_SUFFIXES if base_name.endswith(suffix)), "")
    stem = base_name[: -len(role)] if role else base_name
    digest = hashlib.sha256(str(native_identity).encode("utf-8")).hexdigest()[:16]
    return f"{stem}_{digest}{role}"


def robot_name(native_name: str) -> str:
    """Format the native top-assembly name as the model identity."""

    source = re.sub(r"\.(?:sldasm|sldprt)$", "", str(native_name), flags=re.IGNORECASE)
    return token(source)


def asset_namespace(native_name: str) -> str:
    """Normalize a native top-assembly stem for content-addressed assets."""

    stem = re.sub(r"\.(?:sldasm|sldprt)$", "", str(native_name), flags=re.IGNORECASE)
    stem = stem.lower().replace(".", "")
    normalized = re.sub(r"[^a-z0-9]+", "_", stem).strip("_")
    normalized = re.sub(r"_+", "_", normalized)
    if not normalized:
        raise ValueError("native asset namespace has no ASCII token")
    if normalized[0].isdigit():
        normalized = "n_" + normalized
    return normalized


def _component_token(native_component_identity: str) -> str:
    leaf = str(native_component_identity).replace("\\", "/").rsplit("/", 1)[-1]
    try:
        return token(leaf)
    except ValueError:
        return "component"


def visual_name(native_component_identity: str, *, link: str = "link") -> str:
    """Stable visual name from owner link and exact native occurrence identity."""

    identity = str(native_component_identity)
    if not identity:
        raise ValueError("native visual identity is empty")
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12]
    return f"{token(link)}_{_component_token(identity)}_{digest}_visual"


def material_name(native_component_identity: str, *, link: str = "link") -> str:
    """Stable display material name for one visual component."""

    identity = str(native_component_identity)
    if not identity:
        raise ValueError("native material identity is empty")
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12]
    return f"{token(link)}_{_component_token(identity)}_{digest}_material"


def mesh_path(content_sha256: str, *, category: str = "visual", namespace: str) -> str:
    """One content-addressed mesh path under the native assembly namespace."""

    digest = str(content_sha256)
    if _SHA256.fullmatch(digest) is None:
        raise ValueError("mesh content identity is not a lowercase SHA-256")
    if category not in {"visual", "collision"}:
        raise ValueError("mesh category must be visual or collision")
    return f"meshes/{category}/{asset_namespace(namespace)}/native_{digest}.stl"


def fallback_name(kind: str, native_identity: str, *, semantic: str | None = None) -> str:
    """A semantic fallback with a stable identity suffix for a collision."""

    if kind not in {"link", "joint", "frame"}:
        raise ValueError("unknown interface name kind")
    semantic = semantic or {"link": "body", "joint": "joint", "frame": "frame"}[kind]
    stem = token(semantic)
    digest = hashlib.sha256(str(native_identity).encode("utf-8")).hexdigest()[:16]
    suffix = {"link": "_link", "joint": "_joint", "frame": "_frame"}[kind]
    return f"{stem}_{digest}{suffix}"


def neutral_link_name(native_identity: str, *, root: bool = False) -> str:
    """A category name for a link whose native datum has no usable token."""

    if root:
        return "base_link"
    return "body_link"


def neutral_joint_name(parent: str, child: str, *, fixed: bool = False) -> str:
    """A relationship name for a joint whose native name has no usable token."""

    if fixed:
        return fixed_joint_name(parent, child)
    return f"{token(child)}_joint"


def deduplicate_names(
    names: dict[str, str],
    kind: str,
    *,
    reserved: dict[str, str] | None = None,
) -> dict[str, str]:
    """Make a stable semantic fallback for duplicate normalized names."""

    if kind not in {"link", "joint"}:
        raise ValueError("unknown interface name kind")
    result: dict[str, str] = dict(reserved or {})
    used: set[str] = set(result.values())
    for native in sorted(names):
        if native in result:
            continue
        target = names[native]
        if target not in used:
            result[native] = target
            used.add(target)
            continue
        attempt = 0
        while True:
            candidate = fallback_name(
                kind,
                f"{native}|{target}|{attempt}",
                semantic="body" if kind == "link" else "joint",
            )
            if candidate not in used:
                result[native] = candidate
                used.add(candidate)
                break
            attempt += 1
    return result


def carrier_names_and_rules(
    bodies, joints, root: str
) -> tuple[dict[str, str], dict[str, str], dict[str, dict], dict[str, dict]]:
    """Canonical link/joint names from native identities and joint topology.

    A semantic segment rename is allowed only when the body datum is the
    incoming joint's own alias and the child-side topology proves the segment.
    An independently compliant physical datum (for example
    ``CS_sensor_housing_link``) is preserved unchanged.

    The returned rule dictionaries are the versioned provenance of every
    semantic rename that survived collision disambiguation.  Producer and
    verifier both consume this function so the delivered warning metadata is
    re-derived from native topology instead of being trusted from the source.
    """

    incoming: dict[str, dict] = {}
    outgoing: dict[str, list[str | None]] = {}
    by_role_side: dict[tuple[str, str], list[dict]] = {}
    for joint in joints:
        parsed_role = _role_token(str(joint.get("name") or ""))
        if parsed_role is not None:
            by_role_side.setdefault(parsed_role, []).append(joint)
        if joint.get("type") == "fixed":
            continue
        parent = str(joint.get("parent") or "")
        child = str(joint.get("child") or "")
        parsed = _role_token(str(joint.get("name") or ""))
        if parent:
            outgoing.setdefault(parent, []).append(parsed)
        if child:
            incoming[child] = joint

    body_names: dict[str, str] = {}
    semantic_links: dict[str, str] = {}
    for body in bodies:
        native = str(body["name"])
        frame = body.get("frame") if isinstance(body.get("frame"), dict) else {}
        datum = str(frame.get("coordinate_system") or body.get("datum") or native)
        semantic = re.sub(r"^(?:c_|cs_)", "", datum, flags=re.IGNORECASE)
        target: str | None = None
        if native == str(root) or semantic.lower() in {"base_link", "cs_base_link"}:
            target = "base_link"
        else:
            joint = incoming.get(native)
            if joint is not None:
                joint_source = str(joint.get("name") or "")
                parsed = _role_token(joint_source)
                material = body.get("components")
                if not isinstance(material, list):
                    material = body.get("owners")
                has_material = isinstance(material, list) and bool(material)
                try:
                    datum_token = token(semantic)
                    joint_token = token(joint_source)
                except ValueError:
                    datum_token = joint_token = None
                if datum_token == joint_token and has_material:
                    target = role_link_name(joint_source, outgoing.get(native, []))
                    if target is not None:
                        semantic_links[native] = target
            if target is None:
                try:
                    target = link_name(semantic)
                except ValueError:
                    target = neutral_link_name(f"{native}|{datum}", root=False)
        body_names[native] = target
    root_key = str(root)
    reserved = {root_key: "base_link"} if root_key in body_names else {}
    body_names = deduplicate_names(body_names, "link", reserved=reserved)

    joint_names: dict[str, str] = {}
    semantic_joints: dict[str, str] = {}
    functional_rules: dict[str, dict] = {}
    for joint in joints:
        if "name" not in joint:
            continue
        native = str(joint["name"])
        target: str | None = None
        try:
            if joint.get("type") == "fixed":
                parent = body_names.get(str(joint.get("parent")), str(joint.get("parent") or "parent"))
                child = body_names.get(str(joint.get("child")), str(joint.get("child") or "child"))
                target = fixed_joint_name(parent, child)
            else:
                child = str(joint.get("child") or "")
                target = role_joint_name(native, outgoing.get(child, []))
                functional = _functional_pitch_proof(native, outgoing.get(child, []), by_role_side)
                if target is None and functional is not None:
                    target = f"{functional['side']}{_FUNCTIONAL_JOINT_BY_ROLE[functional['role']]}_joint"
                    functional_rules[native] = functional
                if target is not None:
                    semantic_joints[native] = target
                if target is None:
                    target = joint_name(native)
        except ValueError:
            parent = body_names.get(str(joint.get("parent")), str(joint.get("parent") or "parent"))
            child = body_names.get(str(joint.get("child")), str(joint.get("child") or "child"))
            try:
                target = neutral_joint_name(parent, child, fixed=joint.get("type") == "fixed")
            except ValueError:
                target = fallback_name("joint", native, semantic="joint")
        joint_names[native] = target
    joint_names = deduplicate_names(joint_names, "joint")
    body_rules = {
        native: dict(_ROLE_SEGMENT_RULE)
        for native, target in semantic_links.items()
        if body_names.get(native) == target
    }
    joint_rules = {
        native: dict(functional_rules.get(native) or _ROLE_JOINT_RULE)
        for native, target in semantic_joints.items()
        if joint_names.get(native) == target
    }
    return body_names, joint_names, body_rules, joint_rules
