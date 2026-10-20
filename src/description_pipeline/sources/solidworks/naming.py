"""Deterministic interface names derived from native CAD object identities."""

from __future__ import annotations

import hashlib
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


def robot_name(native_name: str) -> str:
    """Format the native top-assembly name as the model identity."""

    source = re.sub(r"\.(?:sldasm|sldprt)$", "", str(native_name), flags=re.IGNORECASE)
    return token(source)


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


def collision_name(native_component_identity: str, *, link: str = "link") -> str:
    """Stable collision name from owner link and exact native occurrence identity."""

    identity = str(native_component_identity)
    if not identity:
        raise ValueError("native collision identity is empty")
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12]
    return f"{token(link)}_{_component_token(identity)}_{digest}_collision"


def material_name(native_component_identity: str, *, link: str = "link") -> str:
    """Stable display material name for one visual component."""

    identity = str(native_component_identity)
    if not identity:
        raise ValueError("native material identity is empty")
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12]
    return f"{token(link)}_{_component_token(identity)}_{digest}_material"


def mesh_path(content_sha256: str, *, category: str = "visual", semantic: str = "asset") -> str:
    digest = str(content_sha256)
    if _SHA256.fullmatch(digest) is None:
        raise ValueError("mesh content identity is not a lowercase SHA-256")
    if category not in {"visual", "collision"}:
        raise ValueError("mesh category must be visual or collision")
    return f"meshes/{category}/{token(semantic)}_{digest[:16]}.stl"


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


def carrier_names(bodies, joints, root: str) -> tuple[dict[str, str], dict[str, str]]:
    """Canonical link/joint names from native datums and reference axes."""

    body_names: dict[str, str] = {}
    for body in bodies:
        native = str(body["name"])
        frame = body.get("frame") if isinstance(body.get("frame"), dict) else {}
        datum = str(frame.get("coordinate_system") or body.get("datum") or native)
        semantic = re.sub(r"^(?:c_|cs_)", "", datum, flags=re.IGNORECASE)
        try:
            body_names[native] = link_name(semantic, root=native == root or token(semantic) == "base_link")
        except ValueError:
            body_names[native] = neutral_link_name(f"{native}|{datum}", root=native == root)
    root_key = str(root)
    reserved = {root_key: "base_link"} if root_key in body_names else {}
    body_names = deduplicate_names(body_names, "link", reserved=reserved)
    joint_names: dict[str, str] = {}
    for joint in joints:
        native = str(joint["name"])
        try:
            if joint.get("type") == "fixed":
                joint_names[native] = fixed_joint_name(
                    body_names.get(str(joint.get("parent")), str(joint.get("parent") or "parent")),
                    body_names.get(str(joint.get("child")), str(joint.get("child") or "child")),
                )
            else:
                joint_names[native] = joint_name(native)
        except ValueError:
            parent = body_names.get(str(joint.get("parent")), str(joint.get("parent") or "parent"))
            child = body_names.get(str(joint.get("child")), str(joint.get("child") or "child"))
            try:
                joint_names[native] = neutral_joint_name(
                    parent, child, fixed=joint.get("type") == "fixed"
                )
            except ValueError:
                joint_names[native] = fallback_name("joint", native, semantic="joint")
    return body_names, deduplicate_names(joint_names, "joint")
