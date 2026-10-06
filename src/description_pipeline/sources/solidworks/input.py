"""Static, self-contained input package for the SolidWorks-to-URDF v1 workflow.

One package directory contains one ``robot.yaml`` and the CAD documents it
names.  ``inspect_package`` performs every validation that does not need
SolidWorks and returns all findings; ``load_package`` raises
``PipelineError`` when the static input is not usable and otherwise returns a
freeze-ready source mapping plus the immutable input receipt.

Nothing here writes to the package, starts a CAD session, or consults a final
URDF.  The receipt binds the package inventory by SHA-256 so the later native
freeze can prove it read the same bytes.
"""

from __future__ import annotations

import math
import re
import time
from pathlib import Path
from typing import Any

from ...io import PipelineError, confined, digest, inventory, read_data

INPUT_SCHEMA = "solidworks-to-urdf.input/v1"
INSPECTION_SCHEMA = "solidworks-to-urdf.input-inspection/v1"
ROBOT_FILE = "robot.yaml"
CONFIG_SUFFIXES = (".yaml", ".yml")
SUPPORTED_JOINT_TYPES = ("fixed", "revolute", "continuous", "prismatic")
SUPPORTED_PRODUCT_SOURCES = ("cad", "documented_table")
FORMAT_SUFFIXES = (".sldasm", ".sldprt")

_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")
_SNAKE_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


class _Report:
    """Collect all static findings instead of stopping at the first one."""

    def __init__(self) -> None:
        self.errors: list[dict[str, Any]] = []
        self.warnings: list[dict[str, Any]] = []

    @property
    def passed(self) -> bool:
        return not self.errors

    def error(self, code: str, message: str, detail: Any = None) -> None:
        self.errors.append({"code": code, "message": message, "detail": detail})

    def warn(self, code: str, message: str, detail: Any = None) -> None:
        self.warnings.append({"code": code, "message": message, "detail": detail})


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def _is_snake(value: Any) -> bool:
    return isinstance(value, str) and _SNAKE_RE.match(value) is not None


def _is_id(value: Any) -> bool:
    return isinstance(value, str) and value.isascii() and _ID_RE.match(value) is not None


def _is_text(value: Any) -> bool:
    return isinstance(value, str) and value.strip() != "" and _CONTROL_RE.search(value) is None


def _as_mapping(value: Any, report: _Report, code: str, where: str) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        report.error(code, f"{where} must be a mapping", {"value": type(value).__name__})
        return None
    return value


def _unknown_keys(value: dict[str, Any], allowed: set[str], report: _Report, code: str, where: str) -> list[str]:
    unknown = sorted(set(value) - allowed)
    if unknown:
        report.error(code, f"{where} has unknown keys", {"unknown": unknown, "allowed": sorted(allowed)})
    return unknown


def _vector3(value: Any, report: _Report, code: str, where: str) -> tuple[float, float, float] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 3 or not all(_is_number(item) for item in value):
        report.error(code, f"{where} must contain exactly three finite SI numbers", {"value": value})
        return None
    return (float(value[0]), float(value[1]), float(value[2]))


def _unit_axis(value: Any, report: _Report, code: str, where: str) -> tuple[float, float, float] | None:
    axis = _vector3(value, report, code, where)
    if axis is None:
        return None
    length = math.sqrt(sum(component * component for component in axis))
    if abs(length - 1.0) > 1e-9:
        report.error(code, f"{where} must be a normalized unit vector", {"axis": list(axis), "norm": length})
        return None
    return axis


def _range2(
    value: Any, report: _Report, code: str, where: str, *, positive: bool = True
) -> tuple[float, float] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        report.error(code, f"{where} must be a two-element [min, max] range", {"value": value})
        return None
    low, high = value
    if not _is_number(low) or not _is_number(high):
        report.error(code, f"{where} must contain finite numbers", {"value": value})
        return None
    low_value, high_value = float(low), float(high)
    if positive and (low_value <= 0.0 or high_value <= 0.0):
        report.error(code, f"{where} must be positive", {"value": value})
        return None
    if low_value > high_value:
        report.error(code, f"{where} min must not exceed max", {"value": value})
        return None
    return (low_value, high_value)


def _inventory_entries(root: Path, report: _Report) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Return sorted receipt entries and the hash map; reject symlinks/case collisions."""

    try:
        hashes = inventory(root)
    except PipelineError as error:
        report.error("input.inventory_invalid", str(error))
        return [], {}
    entries: list[dict[str, Any]] = []
    for relative in sorted(hashes):
        path = root / relative
        try:
            size = path.stat().st_size
        except OSError as error:
            report.error("input.file_unreadable", str(error), {"path": relative})
            continue
        entries.append({"path": relative, "sha256": hashes[relative], "bytes": size})
    return entries, hashes


def _file_text(path: Path) -> str:
    """Read text for evidence-anchor checks without assuming a text encoding."""

    data = path.read_bytes()
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("utf-8", errors="ignore")


def _structured_evidence(
    value: Any,
    *,
    root: Path,
    hashes: dict[str, str],
    report: _Report,
    code: str,
    where: str,
) -> dict[str, str] | None:
    """Validate ``{file, sha256, anchor}`` and bind it to the package inventory."""

    mapping = _as_mapping(value, report, code, where)
    if mapping is None:
        return None
    _unknown_keys(mapping, {"file", "sha256", "anchor", "note"}, report, code, where)
    relative = mapping.get("file")
    checksum = mapping.get("sha256")
    anchor = mapping.get("anchor")
    if not _is_text(relative):
        report.error(code, f"{where}.file must be a non-empty package-relative path", {"file": relative})
        return None
    if not isinstance(checksum, str) or _SHA_RE.match(checksum) is None:
        report.error(code, f"{where}.sha256 must be a lowercase 64-hex digest", {"sha256": checksum})
        return None
    if not _is_text(anchor):
        report.error(code, f"{where}.anchor must be a non-empty evidence anchor", {"anchor": anchor})
        return None
    try:
        resolved = confined(root, str(relative))
    except PipelineError as error:
        report.error(code, f"{where}.file is not confined to the package: {error}", {"file": relative})
        return None
    if str(relative) not in hashes:
        report.error(code, f"{where}.file is not part of the package inventory", {"file": relative})
        return None
    if hashes[str(relative)] != checksum:
        report.error(
            code,
            f"{where}.sha256 does not match the package bytes",
            {"file": relative, "expected": checksum, "actual": hashes[str(relative)]},
        )
        return None
    if str(anchor) not in _file_text(resolved):
        report.error(code, f"{where}.anchor was not found in its evidence file", {"file": relative, "anchor": anchor})
        return None
    return {"file": str(relative), "sha256": checksum, "anchor": str(anchor)}


def _mass_evidence_binding(
    value: Any,
    *,
    root: Path,
    hashes: dict[str, str],
    report: _Report,
) -> dict[str, str] | None:
    """Validate the shared ``{reference, file, sha256, note}`` mass-evidence binding.

    Mirrors the freeze-side contract while also binding the archived file to the
    package inventory: the file must be confined to the package, present in the
    inventory, and its recorded digest must match the recorded package bytes.
    Per-component anchors live in ``documented_masses[*].evidence`` and are
    checked against this file's text.
    """

    code = "input.mass_evidence_invalid"
    where = "source.mass_evidence"
    mapping = _as_mapping(value, report, code, where)
    if mapping is None:
        return None
    _unknown_keys(mapping, {"reference", "file", "sha256", "note"}, report, code, where)
    reference = mapping.get("reference")
    if not _is_text(reference):
        report.error(code, f"{where}.reference must be a non-empty description", {"reference": reference})
        return None
    relative = mapping.get("file")
    checksum = mapping.get("sha256")
    if not _is_text(relative):
        report.error(code, f"{where}.file must be a non-empty package-relative path", {"file": relative})
        return None
    if not isinstance(checksum, str) or _SHA_RE.match(checksum) is None:
        report.error(code, f"{where}.sha256 must be a lowercase 64-hex digest", {"sha256": checksum})
        return None
    try:
        confined(root, str(relative))
    except PipelineError as error:
        report.error(code, f"{where}.file is not confined to the package: {error}", {"file": relative})
        return None
    if str(relative) not in hashes:
        report.error(code, f"{where}.file is not part of the package inventory", {"file": relative})
        return None
    if hashes[str(relative)] != checksum:
        report.error(
            code,
            f"{where}.sha256 does not match the package bytes",
            {"file": relative, "expected": checksum, "actual": hashes[str(relative)]},
        )
        return None
    binding = {"reference": str(reference), "file": str(relative), "sha256": checksum}
    note = mapping.get("note")
    if isinstance(note, str):
        binding["note"] = note
    return binding


def _validate_documented_masses(
    source: dict[str, Any],
    *,
    root: Path,
    hashes: dict[str, str],
    report: _Report,
    owned_components: set[str],
) -> tuple[dict[str, dict[str, Any]], dict[str, str] | None]:
    raw = source.get("documented_masses")
    material_source = source.get("material_source")
    if not isinstance(material_source, str) or material_source not in SUPPORTED_PRODUCT_SOURCES:
        report.error(
            "input.material_source_invalid",
            "source.material_source must be explicitly 'cad' or 'documented_table'",
            {"value": material_source},
        )
        return {}, None
    if raw is None:
        raw = {}
    mapping = _as_mapping(raw, report, "input.documented_masses_invalid", "source.documented_masses")
    if mapping is None:
        return {}, None
    documented: dict[str, dict[str, Any]] = {}
    shared_binding = None
    if mapping:
        shared_binding = _mass_evidence_binding(
            source.get("mass_evidence"),
            root=root,
            hashes=hashes,
            report=report,
        )
        if shared_binding is None:
            return {}, None
    for component, payload in mapping.items():
        item = _as_mapping(payload, report, "input.documented_mass_invalid", f"source.documented_masses.{component}")
        if item is None:
            return {}, None
        _unknown_keys(item, {"mass_kg", "reason", "evidence"}, report, "input.documented_mass_invalid",
                      f"source.documented_masses.{component}")
        mass = item.get("mass_kg")
        if not _is_number(mass) or float(mass) <= 0.0:
            report.error(
                "input.documented_mass_invalid",
                f"source.documented_masses.{component}.mass_kg must be positive",
                {"mass_kg": mass},
            )
            continue
        reason = item.get("reason")
        if not _is_text(reason):
            report.error(
                "input.documented_mass_invalid",
                f"source.documented_masses.{component}.reason must be non-empty",
                {"reason": reason},
            )
            continue
        anchor = item.get("evidence")
        if not _is_text(anchor):
            report.error(
                "input.documented_mass_invalid",
                f"source.documented_masses.{component}.evidence must be a non-empty anchor",
                {"evidence": anchor},
            )
            continue
        if shared_binding is not None:
            evidence_file = confined(root, shared_binding["file"])
            if str(anchor) not in _file_text(evidence_file):
                report.error(
                    "input.documented_mass_invalid",
                    f"source.documented_masses.{component}.evidence anchor is missing from its mass-evidence file",
                    {"component": component, "anchor": anchor},
                )
                continue
        if component not in owned_components:
            report.error(
                "input.documented_mass_owner_unknown",
                f"documented mass component {component!r} is not owned by any body",
                {"component": component},
            )
            continue
        documented[str(component)] = {
            "mass_kg": float(mass),
            "reason": str(reason),
            "evidence": str(anchor),
        }
    if material_source == "documented_table" and not documented:
        report.error(
            "input.documented_masses_required",
            "source.material_source=documented_table requires a non-empty source.documented_masses table",
        )
    if material_source == "cad" and documented:
        report.error(
            "input.documented_masses_conflict",
            "source.documented_masses is only valid with material_source=documented_table",
            {"components": sorted(documented)},
        )
    if material_source == "documented_table":
        missing = sorted(owned_components - set(documented))
        if missing:
            report.error(
                "input.documented_masses_incomplete",
                "documented_table requires a mass for every owned component",
                {"missing": missing[:20]},
            )
    return documented, shared_binding


def _validate_source(
    source: dict[str, Any],
    *,
    root: Path,
    hashes: dict[str, str],
    report: _Report,
) -> tuple[dict[str, Any], dict[str, Any]]:
    _unknown_keys(
        source,
        {
            "provider",
            "robot_name",
            "assembly",
            "configuration",
            "bodies",
            "joints",
            "frames",
            "material_source",
            "documented_masses",
            "mass_evidence",
        },
        report,
        "input.source_keys",
        "source",
    )
    for forbidden in (
        "worker_url",
        "allow_remote_worker",
        "worker_http_timeout_seconds",
        "worker_poll_seconds",
        "worker_job_timeout_seconds",
        "job_id",
        "evidence_class",
        "allowed_roots",
        "document_suffixes",
        "coordinate_systems",
    ):
        if forbidden in source:
            report.error(
                "input.source_forbidden_key",
                f"source.{forbidden} is not allowed in the v1 self-contained input contract",
                {"key": forbidden},
            )
    if source.get("provider") != "solidworks":
        report.error("input.provider_invalid", "source.provider must be exactly 'solidworks'", {
            "value": source.get("provider")
        })
    robot_name = source.get("robot_name")
    if not _is_snake(robot_name):
        report.error("input.robot_name_invalid", "source.robot_name must be an explicit snake_case name", {
            "value": robot_name
        })
    assembly = source.get("assembly")
    assembly_path = None
    if not _is_text(assembly):
        report.error("input.assembly_invalid", "source.assembly must be a non-empty package-relative path", {
            "value": assembly
        })
    else:
        if Path(str(assembly)).suffix.lower() not in FORMAT_SUFFIXES:
            report.error("input.assembly_invalid", "source.assembly must name a .SLDASM or .SLDPRT document", {
                "value": assembly
            })
        try:
            assembly_path = confined(root, str(assembly))
        except PipelineError as error:
            report.error("input.assembly_invalid", f"source.assembly is not confined to the package: {error}", {
                "value": assembly
            })
    configuration = source.get("configuration")
    if not _is_text(configuration):
        report.error("input.configuration_invalid", "source.configuration must be an explicit non-empty string", {
            "value": configuration
        })
    bodies = source.get("bodies")
    if not isinstance(bodies, list) or not bodies:
        report.error("input.bodies_invalid", "source.bodies must be a non-empty list", {"value": bodies})
        bodies = []
    joints = source.get("joints")
    if not isinstance(joints, list) or not joints:
        report.error("input.joints_invalid", "source.joints must be a non-empty list", {"value": joints})
        joints = []
    frames = source.get("frames") or []
    if not isinstance(frames, list):
        report.error("input.frames_invalid", "source.frames must be a list when present", {"value": frames})
        frames = []

    # v1 keeps one authority: every body frame names a native CAD coordinate
    # system, and the datum collection is derived from those frames instead of
    # being maintained as a second author-owned list.
    datums: set[str] = set()
    for raw_body in bodies:
        if not isinstance(raw_body, dict):
            continue
        frame = raw_body.get("frame")
        if isinstance(frame, dict) and _is_text(frame.get("coordinate_system")):
            datums.add(str(frame["coordinate_system"]))
    for raw_frame in frames:
        if not isinstance(raw_frame, dict):
            continue
        if _is_text(raw_frame.get("coordinate_system")):
            datums.add(str(raw_frame["coordinate_system"]))
    folded = [name.casefold() for name in datums]
    if len(set(folded)) != len(folded):
        report.error(
            "input.coordinate_systems_ambiguous",
            "body frames reference case-insensitively duplicate coordinate systems",
            {"value": sorted(datums)},
        )
    coordinate_systems = sorted(datums)
    if not coordinate_systems:
        report.error(
            "input.coordinate_systems_empty",
            "every body frame must bind a native CAD coordinate_system; none were declared",
        )
    else:
        report.warn(
            "input.coordinate_systems_unverified",
            "datum existence is verified against the CAD assembly during the native capture",
            {"coordinate_systems": coordinate_systems},
        )

    body_names: dict[str, dict[str, Any]] = {}
    body_ids: dict[str, str] = {}
    component_owner: dict[str, str] = {}
    owned_components: set[str] = set()
    for index, raw_body in enumerate(bodies):
        where = f"source.bodies[{index}]"
        body = _as_mapping(raw_body, report, "input.body_invalid", where)
        if body is None:
            continue
        _unknown_keys(body, {"id", "name", "components", "frame"}, report, "input.body_invalid", where)
        body_id = body.get("id")
        name = body.get("name")
        if not _is_snake(body_id):
            report.error("input.body_invalid", f"{where}.id must be snake_case", {"id": body_id})
        elif str(body_id) in body_ids:
            report.error("input.body_duplicate", f"{where}.id is duplicated", {"id": body_id})
        else:
            body_ids[str(body_id)] = where
        if not _is_snake(name):
            report.error("input.body_invalid", f"{where}.name must be snake_case", {"name": name})
        elif str(name) in body_names:
            report.error("input.body_duplicate", f"{where}.name is duplicated", {"name": name})
        else:
            body_names[str(name)] = body
        components = body.get("components")
        if not isinstance(components, list) or not components or not all(_is_text(item) for item in components):
            report.error(
                "input.body_components_invalid",
                f"{where}.components must be a non-empty list of exact component occurrence names",
                {"components": components},
            )
        else:
            for component in components:
                folded = str(component).casefold()
                if folded in {item.casefold() for item in component_owner}:
                    report.error(
                        "input.component_duplicate_owner",
                        f"component {component!r} is owned more than once",
                        {"component": component, "owners": [component_owner.get(str(component)), name]},
                    )
                component_owner[str(component)] = str(name)
                owned_components.add(str(component))
        frame = body.get("frame")
        if not isinstance(frame, dict) or not frame:
            report.error(
                "input.body_frame_missing",
                f"{where}.frame must bind a native CAD coordinate_system",
                {"body": name},
            )
        else:
            _unknown_keys(frame, {"coordinate_system", "xyz", "rpy"}, report, "input.body_frame_invalid", f"{where}.frame")
            reference = frame.get("coordinate_system")
            authored = sorted({"xyz", "rpy"} & set(frame))
            if authored:
                report.error(
                    "input.body_frame_authored_numbers",
                    f"{where}.frame must not repeat CAD numbers; v1 derives link frames from the named datum",
                    {"body": name, "fields": authored},
                )
            if reference is None:
                report.error(
                    "input.body_frame_missing",
                    f"{where}.frame must bind a native CAD coordinate_system",
                    {"body": name},
                )
            elif not _is_text(reference):
                report.error(
                    "input.frame_reference_unknown",
                    f"{where}.frame.coordinate_system must be a non-empty native datum name",
                    {"body": name, "coordinate_system": reference},
                )

    joint_names: dict[str, dict[str, Any]] = {}
    child_owner: dict[str, str] = {}
    parent_edges: dict[str, set[str]] = {name: set() for name in body_names}
    for index, raw_joint in enumerate(joints):
        where = f"source.joints[{index}]"
        joint = _as_mapping(raw_joint, report, "input.joint_invalid", where)
        if joint is None:
            continue
        _unknown_keys(
            joint,
            {"id", "name", "type", "parent", "child", "axis", "limits", "axis_reference", "limit_evidence"},
            report,
            "input.joint_invalid",
            where,
        )
        joint_id = joint.get("id")
        name = joint.get("name")
        if not _is_snake(joint_id):
            report.error("input.joint_invalid", f"{where}.id must be snake_case", {"id": joint_id})
        if not _is_snake(name):
            report.error("input.joint_invalid", f"{where}.name must be snake_case", {"name": name})
        elif str(name) in joint_names:
            report.error("input.joint_duplicate", f"{where}.name is duplicated", {"name": name})
        else:
            joint_names[str(name)] = joint
        joint_type = joint.get("type")
        if joint_type not in SUPPORTED_JOINT_TYPES:
            report.error(
                "input.joint_type_unsupported",
                f"{where}.type must be one of {list(SUPPORTED_JOINT_TYPES)}",
                {"type": joint_type},
            )
            continue
        parent = joint.get("parent")
        child = joint.get("child")
        if not _is_snake(parent) or str(parent) not in body_names:
            report.error("input.joint_parent_unknown", f"{where}.parent must name a declared body", {"parent": parent})
            parent = None
        if not _is_snake(child) or str(child) not in body_names:
            report.error("input.joint_child_unknown", f"{where}.child must name a declared body", {"child": child})
            child = None
        if parent is not None and child is not None:
            if parent == child:
                report.error("input.joint_cycle", f"{where} connects a body to itself", {"body": parent})
            if child in child_owner:
                report.error(
                    "input.joint_multiple_parents",
                    f"body {child!r} is the child of more than one joint",
                    {"child": child, "parents": [child_owner[child], name]},
                )
            child_owner[str(child)] = str(name)
            parent_edges.setdefault(str(parent), set()).add(str(child))
        if joint_type == "fixed":
            for key in ("axis", "limits", "axis_reference", "limit_evidence"):
                if key in joint:
                    report.error(
                        "input.joint_invalid",
                        f"{where}.{key} is not meaningful for a fixed joint",
                        {"joint": name},
                    )
            continue
        if not _is_text(joint.get("axis_reference")):
            report.error(
                "input.joint_axis_reference_invalid",
                f"{where}.axis_reference must name the native component/face that defines the axis",
                {"joint": name},
            )
        axis = _unit_axis(joint.get("axis"), report, "input.joint_axis_invalid", f"{where}.axis")
        _structured_evidence(
            joint.get("limit_evidence"),
            root=root,
            hashes=hashes,
            report=report,
            code="input.limit_evidence_invalid",
            where=f"{where}.limit_evidence",
        )
        limits = _as_mapping(joint.get("limits"), report, "input.joint_limits_invalid", f"{where}.limits")
        if limits is None:
            continue
        _unknown_keys(
            limits,
            {"lower", "upper", "effort", "velocity"},
            report,
            "input.joint_limits_invalid",
            f"{where}.limits",
        )
        effort = limits.get("effort")
        velocity = limits.get("velocity")
        for key, value in (("effort", effort), ("velocity", velocity)):
            if not _is_number(value) or float(value) <= 0.0:
                report.error(
                    "input.joint_limits_invalid",
                    f"{where}.limits.{key} must be finite and positive",
                    {key: value},
                )
        if joint_type in ("revolute", "prismatic"):
            lower, upper = limits.get("lower"), limits.get("upper")
            if not _is_number(lower) or not _is_number(upper):
                report.error(
                    "input.joint_limits_invalid",
                    f"{where}.limits.lower and .upper must be finite SI numbers",
                    {"lower": lower, "upper": upper},
                )
            elif float(lower) > float(upper):
                report.error(
                    "input.joint_limits_invalid",
                    f"{where}.limits.lower must not exceed .upper",
                    {"lower": lower, "upper": upper},
                )
        elif joint_type == "continuous":
            if "lower" in limits or "upper" in limits:
                report.error(
                    "input.joint_limits_invalid",
                    f"{where}.limits must not declare position bounds for a continuous joint",
                    {"joint": name},
                )

    if "base_link" not in body_names:
        report.error("input.root_missing", "source.bodies must declare exactly one body named 'base_link'")
    roots = [name for name in body_names if name not in child_owner]
    if roots != ["base_link"]:
        report.error(
            "input.tree_root_invalid",
            "the joint tree must have exactly one root and it must be base_link",
            {"roots": roots},
        )
    reachable = {"base_link"} if "base_link" in body_names else set()
    frontier = list(reachable)
    while frontier:
        current = frontier.pop()
        for child in parent_edges.get(current, set()):
            if child not in reachable:
                reachable.add(child)
                frontier.append(child)
    missing = sorted(set(body_names) - reachable)
    if missing:
        report.error("input.tree_disconnected", "every body must be connected to base_link", {"unreachable": missing})

    frame_names: dict[str, dict[str, Any]] = {}
    for index, raw_frame in enumerate(frames):
        where = f"source.frames[{index}]"
        frame = _as_mapping(raw_frame, report, "input.frame_invalid", where)
        if frame is None:
            continue
        _unknown_keys(frame, {"id", "name", "parent", "xyz", "rpy", "coordinate_system"}, report,
                      "input.frame_invalid", where)
        name = frame.get("name")
        if not _is_snake(name):
            report.error("input.frame_invalid", f"{where}.name must be snake_case", {"name": name})
        elif str(name) in frame_names:
            report.error("input.frame_duplicate", f"{where}.name is duplicated", {"name": name})
        else:
            frame_names[str(name)] = frame
        parent = frame.get("parent")
        if not _is_snake(parent) or str(parent) not in body_names:
            report.error("input.frame_parent_unknown", f"{where}.parent must name a declared body", {"parent": parent})
        reference = frame.get("coordinate_system")
        authored = sorted({"xyz", "rpy"} & set(frame))
        if authored:
            report.error(
                "input.frame_authored_numbers",
                f"{where} must not repeat CAD numbers; v1 derives frames from the named datum",
                {"frame": name, "fields": authored},
            )
        if not _is_text(reference):
            report.error(
                "input.frame_reference_unknown",
                f"{where}.coordinate_system must be a non-empty native datum name",
                {"frame": name, "coordinate_system": reference},
            )

    documented, mass_evidence = _validate_documented_masses(
        source,
        root=root,
        hashes=hashes,
        report=report,
        owned_components=owned_components,
    )
    return {
        "provider": "solidworks",
        "robot_name": robot_name,
        "assembly": str(assembly),
        "assembly_resolved": str(assembly_path) if assembly_path is not None else None,
        "assembly_sha256": hashes.get(str(assembly)) if assembly is not None else None,
        "configuration": configuration,
        "coordinate_systems": [str(item) for item in coordinate_systems],
        "bodies": bodies,
        "joints": joints,
        "frames": frames,
        "material_source": source.get("material_source"),
        "documented_masses": documented,
        "mass_evidence": mass_evidence,
        "owned_components": sorted(owned_components),
        "body_names": sorted(body_names),
        "joint_names": sorted(joint_names),
        "frame_names": sorted(frame_names),
        "root_link": "base_link" if "base_link" in body_names else None,
    }, {
        "provider": "solidworks",
        "robot_name": robot_name,
        "assembly": str(assembly_path) if assembly_path is not None else str(assembly),
        "configuration": configuration,
        "allowed_roots": [str(root)],
        "document_suffixes": [".sldasm", ".sldprt"],
        "geometry": {"enabled": True, "format": "stl_binary"},
        "coordinate_systems": [str(item) for item in coordinate_systems],
        "bodies": bodies,
        "joints": joints,
        "frames": frames,
        "material_source": source.get("material_source"),
        "documented_masses": documented,
        "mass_evidence": mass_evidence,
        "require_saved": True,
    }


def _validate_checks(
    checks: Any,
    *,
    root: Path,
    hashes: dict[str, str],
    owned_components: set[str],
    report: _Report,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    mapping = _as_mapping(checks, report, "input.checks_invalid", "checks")
    if mapping is None:
        return {}, {}
    _unknown_keys(
        mapping,
        {"expected_mass_kg", "expected_extent_m", "documented_exclusions"},
        report,
        "input.checks_invalid",
        "checks",
    )
    mass_range = _range2(mapping.get("expected_mass_kg"), report, "input.checks_invalid",
                         "checks.expected_mass_kg")
    extent_range = _range2(mapping.get("expected_extent_m"), report, "input.checks_invalid",
                           "checks.expected_extent_m")
    exclusions: dict[str, dict[str, Any]] = {}
    raw_exclusions = mapping.get("documented_exclusions") or []
    if not isinstance(raw_exclusions, list):
        report.error("input.checks_invalid", "checks.documented_exclusions must be a list", {
            "value": raw_exclusions
        })
        raw_exclusions = []
    for index, raw in enumerate(raw_exclusions):
        where = f"checks.documented_exclusions[{index}]"
        exclusion = _as_mapping(raw, report, "input.exclusion_invalid", where)
        if exclusion is None:
            continue
        _unknown_keys(exclusion, {"component", "reason", "evidence"}, report, "input.exclusion_invalid", where)
        component = exclusion.get("component")
        if not _is_text(component) or str(component) not in owned_components:
            report.error(
                "input.exclusion_invalid",
                f"{where}.component must be an exact component occurrence owned by a body",
                {"component": component},
            )
            continue
        reason = exclusion.get("reason")
        if not _is_text(reason):
            report.error("input.exclusion_invalid", f"{where}.reason must be non-empty", {"reason": reason})
            continue
        evidence = _structured_evidence(
            exclusion.get("evidence"),
            root=root,
            hashes=hashes,
            report=report,
            code="input.exclusion_evidence_invalid",
            where=f"{where}.evidence",
        )
        if evidence is None:
            continue
        exclusions[str(component)] = {
            "component": str(component),
            "reason": str(reason),
            "evidence": evidence,
        }
    return {
        "expected_mass_kg": list(mass_range) if mass_range is not None else None,
        "expected_extent_m": list(extent_range) if extent_range is not None else None,
        "documented_exclusions": exclusions,
    }, exclusions


def inspect_package(path: Path) -> dict[str, Any]:
    """Return every static finding for one self-contained v1 input package."""

    report = _Report()
    package = Path(path)
    if package.is_symlink():
        report.error("input.package_symlink", "the package root must not be a symlink", {"path": str(package)})
        return {
            "schema_version": INSPECTION_SCHEMA,
            "passed": False,
            "errors": report.errors,
            "warnings": report.warnings,
            "input": None,
            "resolved": None,
            "input_receipt": None,
        }
    if not package.is_dir():
        report.error("input.package_missing", "the package root must be an existing directory", {"path": str(package)})
        return {
            "schema_version": INSPECTION_SCHEMA,
            "passed": False,
            "errors": report.errors,
            "warnings": report.warnings,
            "input": None,
            "resolved": None,
            "input_receipt": None,
        }
    root = package.resolve()
    entries, hashes = _inventory_entries(root, report)
    config_path = root / ROBOT_FILE
    if ROBOT_FILE not in hashes:
        report.error("input.robot_missing", f"the package must contain exactly one root {ROBOT_FILE}", {
            "path": str(config_path)
        })
        data = None
    else:
        other_configs = sorted(
            name for name in hashes if Path(name).suffix.lower() in CONFIG_SUFFIXES and name != ROBOT_FILE
        )
        if other_configs:
            report.error(
                "input.multiple_config_files",
                "the v1 package must contain exactly one YAML config file",
                {"files": other_configs},
            )
        try:
            data = read_data(config_path)
        except PipelineError as error:
            report.error("input.yaml_invalid", str(error))
            data = None
    resolved: dict[str, Any] = {}
    exclusions: dict[str, dict[str, Any]] = {}
    checks: dict[str, Any] = {}
    source_mapping: dict[str, Any] | None = None
    if isinstance(data, dict):
        _unknown_keys(data, {"schema_version", "hardware_id", "source", "checks"}, report, "input.top_keys", "input")
        if data.get("schema_version") != INPUT_SCHEMA:
            report.error(
                "input.schema_invalid",
                f"schema_version must be exactly {INPUT_SCHEMA!r}",
                {"value": data.get("schema_version")},
            )
        if not _is_id(data.get("hardware_id")):
            report.error(
                "input.hardware_id_invalid",
                "hardware_id must be an ASCII identifier",
                {"value": data.get("hardware_id")},
            )
        source = _as_mapping(data.get("source"), report, "input.source_invalid", "source")
        if source is not None:
            resolved, source_mapping = _validate_source(source, root=root, hashes=hashes, report=report)
            checks, exclusions = _validate_checks(
                data.get("checks"),
                root=root,
                hashes=hashes,
                owned_components=set(resolved.get("owned_components") or []),
                report=report,
            )
        else:
            report.error("input.source_invalid", "input.source must be a mapping")
    elif data is not None:
        report.error("input.top_invalid", "the input document must be a mapping", {"value": type(data).__name__})

    if source_mapping is not None:
        source_mapping["geometry_exclusions"] = exclusions
        source_mapping["expected_mass_kg"] = checks.get("expected_mass_kg")
        source_mapping["expected_extent_m"] = checks.get("expected_extent_m")
    assembly_sha = None
    if isinstance(data, dict) and isinstance(data.get("source"), dict):
        assembly_name = data["source"].get("assembly")
        if isinstance(assembly_name, str):
            assembly_sha = hashes.get(assembly_name)
    receipt = {
        "schema_version": INPUT_SCHEMA,
        "package_root": str(package),
        "resolved_package_root": str(root),
        "robot_yaml": ROBOT_FILE,
        "robot_yaml_sha256": hashes.get(ROBOT_FILE),
        "assembly": data.get("source", {}).get("assembly") if isinstance(data, dict) and isinstance(data.get("source"), dict) else None,
        "assembly_sha256": assembly_sha,
        "file_count": len(entries),
        "inventory": entries,
        "inventory_digest": digest(entries),
        "loaded_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    return {
        "schema_version": INSPECTION_SCHEMA,
        "passed": report.passed,
        "errors": report.errors,
        "warnings": report.warnings,
        "input": data,
        "resolved": {
            "package_root": str(root),
            "source": source_mapping,
            "checks": checks,
            "documented_exclusions": exclusions,
            **{key: resolved.get(key) for key in (
                "assembly_resolved", "assembly_sha256", "configuration", "coordinate_systems",
                "owned_components", "body_names", "joint_names", "frame_names", "root_link",
            )},
        },
        "input_receipt": receipt,
    }


def load_package(path: Path) -> dict[str, Any]:
    """Load one static v1 package or raise ``PipelineError`` with all findings."""

    inspection = inspect_package(path)
    if not inspection["passed"]:
        errors = inspection.get("errors") or []
        message = "; ".join(f"{item['code']}: {item['message']}" for item in errors[:8])
        error = PipelineError(f"SolidWorks v1 input package failed static validation: {message}")
        error.findings = errors  # type: ignore[attr-defined]
        error.inspection = inspection  # type: ignore[attr-defined]
        raise error
    receipt = inspection.get("input_receipt") or {}
    # Re-inventory after validation: the freeze must read the same bytes that
    # were statically inspected.  A change invalidates the receipt.
    try:
        current = inventory(Path(receipt["resolved_package_root"]))
    except (KeyError, PipelineError) as error:
        raise PipelineError(f"SolidWorks v1 input package could not be re-inventoried: {error}") from error
    if current != {entry["path"]: entry["sha256"] for entry in receipt.get("inventory", [])}:
        raise PipelineError("SolidWorks v1 input package changed during static validation")
    resolved = inspection.get("resolved") or {}
    return {
        "schema_version": INPUT_SCHEMA,
        "hardware_id": inspection["input"]["hardware_id"],
        "source": resolved.get("source"),
        "checks": resolved.get("checks"),
        "resolved": {key: value for key, value in resolved.items() if key not in {"source", "checks"}},
        "input_receipt": {**receipt, "inventory_verified": True},
    }
