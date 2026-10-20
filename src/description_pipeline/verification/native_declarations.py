"""Re-derive native exporter claims without importing the producer's decoder.

Explicit saved memberships are authoritative. Incomplete mate inference cannot
refute them. An unselected leaf is completed only through fully reconstructed
rigidity to exactly one selected link. Native endpoints/datums supply geometry;
saved zero operating parameters remain zero, and serialized physics is ignored.
"""

from __future__ import annotations

import math
import ntpath
from pathlib import PurePosixPath
import re

import numpy as np

from ..sources.solidworks.session_artifacts import (
    SESSION_DOCUMENT_API,
    session_materialization_under_root,
)
from .solidworks_urdf import _axis, _cad_pose, _pose, _same_pose
from .v14_declarations import DeclarationError, pid_hex, validate_boundary

SCHEMA = "solidworks-to-urdf.native-declarations-verification/v1"
def _require(condition, code, message, **detail):
    if not condition:
        raise DeclarationError(code, message, detail)


def _selector(value):
    _require(isinstance(value, str) and bool(value.strip()), "v14.datum_missing", "no named native feature was saved")
    match = re.fullmatch(r"(.+?)\s*<([^<>]+)>", value.strip())
    return (match[1].strip(), match[2]) if match else (value.strip(), "")


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _all_zero_limits(limits) -> bool:
    return isinstance(limits, dict) and all(
        _number(limits.get(key)) and float(limits[key]) == 0.0
        for key in ("lower", "upper", "effort", "velocity")
    )


def _leaves(components):
    active = {name for name, item in components.items() if item["suppressed"] is False}
    return {
        name
        for name in active
        if not str(components[name].get("document") or "").lower().endswith(".sldasm")
        and not any(other.startswith(name + "/") for other in active)
    }


def _expanded(name, leaves):
    return {leaf for leaf in leaves if leaf == name or leaf.startswith(name + "/")}


def _pid_occurrence(row, components, raw):
    _require(
        row is not None and row["error_code"] == 0 and row["name2"] in components,
        "v14.pid_unread",
        "a saved component PID did not resolve in this capture",
    )
    component = components[row["name2"]]
    _require(
        row["configuration"] == component.get("configuration"),
        "v14.pid_binding",
        "the PID resolved another occurrence configuration",
        component=row["name2"],
    )
    document = row["document"]
    _require(
        isinstance(document, str) and bool(document) and document == component.get("document"),
        "v14.pid_document_unread",
        "the PID lacks a proven occurrence document",
        component=row["name2"],
    )
    left, right = row.get("source_provenance"), component.get("source_provenance")
    if left is not None or right is not None:
        _require(
            isinstance(left, dict) and isinstance(right, dict),
            "v14.imported_binding",
            "PID and occurrence imported-source proofs differ",
            component=row["name2"],
        )
        if right.get("state") == "cached_materialization":
            # A cached native materialization is a complete identity without a confined
            # package document: the PID document is the occurrence's own engine-resolved
            # session materialization, spelled exactly as the active walk records it, and
            # the delivered bytes binding lives in the sealed selection carrier.
            from .native_discovery import _cached_materialization_reason  # noqa: PLC0415

            materialization = right.get("materialization_path")
            session_root = right.get("session_temp_root")
            _require(
                left.get("state") == "cached_materialization"
                and left.get("kind") == right.get("kind")
                and left.get("used_api") == right.get("used_api")
                and left.get("materialization_path") == materialization
                and left.get("session_temp_root") == session_root
                and left.get("native_api_pid") == right.get("native_api_pid")
                and left.get("is_virtual") == right.get("is_virtual"),
                "v14.imported_binding",
                "the PID and the active occurrence record different cached materializations",
                component=row["name2"],
            )
            if left.get("author_imported_path") is not None and right.get("author_imported_path") is not None:
                _require(
                    left.get("author_imported_path") == right.get("author_imported_path")
                    and left.get("author_imported_in_source_root") == right.get("author_imported_in_source_root")
                    and left.get("author_imported_relative") == right.get("author_imported_relative"),
                    "v14.imported_binding",
                    "the PID and the active occurrence record different author metadata",
                    component=row["name2"],
                )
            _require(
                isinstance(materialization, str)
                and session_materialization_under_root(materialization, session_root)
                and document == materialization.replace("/", "\\").strip().lower(),
                "v14.imported_binding",
                "the cached PID document is not the recorded session temp materialization",
                component=row["name2"],
            )
            author = right.get("author_imported_path")
            _require(
                author is None or (isinstance(author, str) and bool(author)),
                "v14.imported_binding",
                "the cached occurrence author metadata is unreadable",
                component=row["name2"],
            )
            carrier = raw.get("source_selection")
            closure = carrier.get("closure") if isinstance(carrier, dict) else None
            slots = [
                item
                for item in (closure.get("imports") if isinstance(closure, dict) else None) or []
                if isinstance(item, dict) and item.get("state") == "cached_materialization"
            ]
            reason = _cached_materialization_reason(component, right, slots, set(), carrier, raw.get("files") or {})
            _require(
                reason is None,
                "v14.imported_binding",
                reason or "the cached occurrence is not covered by the sealed selection carrier",
                component=row["name2"],
            )
            return component
        if right.get("state") == "embedded_virtual_component":
            from .native_discovery import (  # noqa: PLC0415
                _embedded_virtual_component_reason,
            )

            session_document = right.get("session_document_path")
            session_root = right.get("session_temp_root")
            _require(
                left.get("state") == "embedded_virtual_component"
                and left.get("kind") == "native_embedded_virtual_component"
                and left.get("used_api") == SESSION_DOCUMENT_API
                and right.get("used_api") == SESSION_DOCUMENT_API
                and left.get("session_document_path") == session_document
                and left.get("native_document_path") == right.get("native_document_path")
                and left.get("materialization_path") == right.get("materialization_path")
                and left.get("session_temp_root") == session_root
                and left.get("native_api_pid") == right.get("native_api_pid")
                and left.get("is_virtual") is True
                and "author_imported_path" in left
                and "author_imported_error" in left
                and left.get("author_imported_path") is None
                and left.get("author_imported_error") is None
                and "author_imported_path" in right
                and "author_imported_error" in right
                and right.get("author_imported_path") is None
                and right.get("author_imported_error") is None,
                "v14.imported_binding",
                "the PID and the active occurrence record different embedded virtual components",
                component=row["name2"],
            )
            _require(
                isinstance(session_document, str)
                and ntpath.normcase(document) == ntpath.normcase(session_document),
                "v14.imported_binding",
                "the embedded virtual PID document is not the recorded VC~~ session document",
                component=row["name2"],
            )
            carrier = raw.get("source_selection")
            closure = carrier.get("closure") if isinstance(carrier, dict) else None
            slots = [
                item
                for item in (closure.get("virtual_components") if isinstance(closure, dict) else None) or []
                if isinstance(item, dict) and item.get("state") == "embedded_virtual_component"
            ]
            reason = _embedded_virtual_component_reason(
                component, right, slots, set(), carrier, raw.get("files") or {}
            )
            _require(
                reason is None,
                "v14.imported_binding",
                reason or "the virtual occurrence is not covered by the sealed selection carrier",
                component=row["name2"],
            )
            return component
    # Non-cached documents must stay confined package paths, with or without provenance.
    _require(
        not PurePosixPath(document).is_absolute()
        and ".." not in PurePosixPath(document).parts
        and "\\" not in document
        and ":" not in document,
        "v14.pid_binding",
        "the occurrence document is not a confined package path",
        component=row["name2"],
    )
    if left is None and right is None:
        return component
    fields = ("state", "kind", "used_api", "document", "sha256", "is_virtual", "source_root", "imported_path")
    _require(
        all(left.get(key) == right.get(key) for key in fields),
        "v14.imported_binding",
        "PID and occurrence imported-source proofs differ",
        component=row["name2"],
    )
    _require(
        left.get("state") == "resolved"
        and left.get("kind") == "native_imported_document"
        and left.get("used_api") == SESSION_DOCUMENT_API
        and left.get("document") == document
        and type(left.get("is_virtual")) is bool
        and isinstance(left.get("sha256"), str)
        and re.fullmatch(r"[0-9a-f]{64}", left["sha256"])
        and (raw.get("files") or {}).get(document) == left["sha256"],
        "v14.imported_binding",
        "the imported document is not bound to captured file bytes",
        component=row["name2"],
    )
    origin, root = left.get("imported_path"), left.get("source_root")
    _require(
        isinstance(origin, str) and ntpath.isabs(origin) and isinstance(root, str) and ntpath.isabs(root),
        "v14.imported_binding",
        "the imported-source confinement evidence is incomplete",
    )
    relative = ntpath.relpath(origin, root)
    _require(
        relative != ".."
        and not relative.startswith("..\\")
        and ntpath.normcase(relative) == ntpath.normcase(document.replace("/", "\\")),
        "v14.imported_binding",
        "GetImportedPath does not name the captured source inside the frozen root",
    )
    return component


def _valid_mate(mate, components):
    """Only measured, solved, two-occurrence evidence may prove completion/conflict."""
    entities = mate.get("entities")
    if (
        mate.get("suppressed") is not False
        or type(mate.get("error_code")) is not int
        or mate["error_code"] != 0
        or not isinstance(entities, list)
        or len(entities) != 2
        or not all(
            isinstance(item, dict) and isinstance(item.get("component"), str) and item["component"] in components
            for item in entities
        )
    ):
        return False
    try:
        for entity in entities:
            component = components[entity["component"]]
            if component["suppressed"] is not False:
                return False
            _cad_pose(component["transform"])
    except Exception:  # noqa: BLE001
        # Native pose verification raises PipelineError. No malformed reading proves a mate.
        return False
    return True


def _measured_rows(mate, frames, components):
    from .native_discovery import _rows_for

    if not _valid_mate(mate, components):
        return None
    try:
        return _rows_for(mate, frames)
    except Exception:  # noqa: BLE001 - unread geometry cannot prove rigid attachment
        return None


def _complete_membership(raw, components, members, leaves):
    from .native_discovery import _frames, _span

    grouped = {}
    frames = _frames(raw)
    for index, mate in enumerate(raw.get("mates") or []):
        if not isinstance(mate, dict) or mate.get("suppressed") is True:
            continue
        entities = mate.get("entities")
        if not isinstance(entities, list) or len(entities) != 2 or not all(isinstance(item, dict) for item in entities):
            continue
        names = [item.get("component") for item in entities]
        if not all(isinstance(name, str) and name in components for name in names) or names[0] == names[1]:
            continue
        pair = tuple(sorted(names))
        group = grouped.setdefault(pair, {"rows": [], "invalid": False, "mates": []})
        rows = _measured_rows(mate, frames, components)
        group["mates"].append(index)
        if rows is None:
            group["invalid"] = True
        else:
            group["rows"].extend(rows["rows"])
    graph = {}
    proofs = []
    for pair, group in grouped.items():
        if group["invalid"] or len(_span(group["rows"])) != 6:
            continue
        left, right = pair
        graph.setdefault(left, set()).add(right)
        graph.setdefault(right, set()).add(left)
        proofs.append({"pair": list(pair), "mates": group["mates"]})
    selected_owner = {leaf: owner for owner, names in members.items() for leaf in names}
    # A container with leaves owned by two links cannot be assigned one owner.
    initial = {}
    for name in components:
        covered = _expanded(name, leaves)
        owners = {selected_owner[leaf] for leaf in covered if leaf in selected_owner}
        if covered and covered <= selected_owner.keys() and len(owners) == 1:
            initial[name] = next(iter(owners))
    added = {}
    visited = set()
    conflicts = []
    for start in sorted(graph):
        if start in visited:
            continue
        connected, frontier = set(), [start]
        while frontier:
            node = frontier.pop()
            if node in connected:
                continue
            connected.add(node)
            frontier.extend(graph.get(node, set()) - connected)
        visited.update(connected)
        owners = {initial[node] for node in connected if node in initial}
        if len(owners) > 1:
            conflicts.append({"components": sorted(connected), "links": sorted(owners)})
        elif len(owners) == 1:
            owner = next(iter(owners))
            for leaf in sorted(connected & leaves - selected_owner.keys()):
                members[owner].add(leaf)
                added[leaf] = owner
    return added, proofs, conflicts


def verify_declarations(raw: dict, source: dict | None = None, *, purpose: str | None = None) -> dict:
    """Audit raw native declaration evidence and, optionally, generated source claims."""
    checks, errors, diagnostics, state = [], [], [], {}

    def check(identifier, callback):
        try:
            details = callback() or {}
            checks.append({"id": identifier, "passed": True, "details": details})
            return True
        except DeclarationError as error:
            failure = {"code": error.code, "message": str(error), "detail": error.detail}
        except Exception as error:  # noqa: BLE001 - malformed evidence must produce a failed gate
            failure = {"code": "v14.evidence_invalid", "message": f"{type(error).__name__}: {error}", "detail": {}}
        errors.append(failure)
        checks.append({"id": identifier, "passed": False, "details": failure})
        return False

    def boundary():
        block = raw.get("exporter")
        _require(isinstance(block, dict), "v14.unread", "native exporter evidence has not been read")
        _require(block.get("state") != "absent", "v14.absent", "no v1.4 exporter declaration was found")
        _require(
            block.get("state") == "extracted",
            "v14.unread",
            "exporter extraction is incomplete",
            native_error=block.get("error"),
        )
        declaration = validate_boundary(block)
        main = (raw.get("selection") or {}).get("main_assembly") or (raw.get("identity") or {}).get("main_assembly")
        _require(declaration["document"] == main, "v14.capture_binding", "the declaration came from another document")
        _require(
            declaration["configuration"] == raw.get("saved_configuration"),
            "v14.capture_binding",
            "the declaration was not read in the observed saved configuration",
        )
        state["declaration"] = declaration
        return {
            "raw_xml_sha256": declaration["raw_xml_sha256"],
            "document": main,
            "configuration": declaration["configuration"],
        }

    def topology():
        links, parents, joints = {}, {}, {}
        stack = [(state["declaration"]["root"], None)]
        while stack:
            link, parent = stack.pop()
            _require(isinstance(link, dict), "v14.topology", "a link tree contains a null link")
            name = link.get("name")
            _require(
                isinstance(name, str) and bool(name) and name not in links,
                "v14.topology",
                "link names must be non-empty and unique",
                name=name,
            )
            links[name] = link
            parents[name] = parent
            _require(link.get("is_incomplete") is not True, "v14.incomplete", "the saved link is incomplete", link=name)
            joint = link.get("joint")
            if parent is None:
                _require(
                    joint is None
                    or all(joint.get(key) in (None, "") for key in ("name", "type", "parent_link", "child_link")),
                    "v14.topology",
                    "the root metadata holder declares a tree edge",
                )
            else:
                _require(
                    isinstance(joint, dict) and joint.get("parent_link") == parent and joint.get("child_link") == name,
                    "v14.topology",
                    "joint endpoints disagree with the saved link tree",
                    link=name,
                )
                jname = joint.get("name")
                _require(
                    isinstance(jname, str) and bool(jname) and jname not in joints,
                    "v14.topology",
                    "joint names must be non-empty and unique",
                    joint=jname,
                )
                _require(
                    joint.get("type") in {"fixed", "revolute", "continuous", "prismatic"},
                    "v14.joint_type",
                    "unsupported saved joint type",
                    joint=jname,
                    type=joint.get("type"),
                )
                joints[jname] = joint
            stack.extend((child, name) for child in reversed(link.get("children") or []))
        state.update(links=links, parents=parents, joints=joints)
        return {"links": len(links), "joints": len(joints), "root": state["declaration"]["root"]["name"]}

    def membership():
        components = {}
        for item in raw.get("components") or []:
            _require(
                isinstance(item, dict)
                and isinstance(item.get("name2"), str)
                and item["name2"]
                and item["name2"] not in components
                and type(item.get("suppressed")) is bool,
                "v14.components",
                "the native occurrence inventory is malformed",
            )
            components[item["name2"]] = item
        _require(bool(components), "v14.components", "native occurrence inventory is empty")
        leaves = _leaves(components)
        state.update(components=components, leaves=leaves)
        bindings = {row["pid_hex"]: row for row in state["declaration"]["pid_bindings"]}
        members = {name: set() for name in state["links"]}
        selected, pids, suppressed = {}, {}, []
        main_pids = {}
        for name, link in state["links"].items():
            selected[name] = set()
            slots = link.get("component_pids")
            _require(
                isinstance(slots, list), "v14.membership_unread", "a component selection array is absent", link=name
            )
            if link.get("main_component_pid") is not None:
                main_pids[name] = pid_hex(link["main_component_pid"])
            for value in slots:
                key = pid_hex(value)
                _require(
                    key not in pids or pids[key] == name,
                    "v14.membership_overlap",
                    "the same persistent reference belongs to two links",
                    links=[pids.get(key), name],
                )
                pids[key] = name
                row = bindings.get(key)
                component = _pid_occurrence(row, components, raw)
                if component["suppressed"]:
                    suppressed.append(row["name2"])
                    continue
                selected[name].add(row["name2"])
                members[name].update(_expanded(row["name2"], leaves))
        _require(
            set(bindings) == set(pids) | set(main_pids.values()),
            "v14.pid_binding",
            "the PID binding inventory differs from the declaration",
        )
        claimed = {}
        for name, names in members.items():
            for leaf in names:
                _require(
                    leaf not in claimed,
                    "v14.membership_overlap",
                    "two expanded selections claim one leaf",
                    component=leaf,
                    links=[claimed.get(leaf), name],
                )
                claimed[leaf] = name
        for name, key in main_pids.items():
            row = bindings[key]
            _pid_occurrence(row, components, raw)
            material = _expanded(row["name2"], leaves)
            _require(
                bool(material) and material <= members[name],
                "v14.main_component",
                "the main component is outside its declared link",
                link=name,
            )
        added, proofs, conflicts = _complete_membership(raw, components, members, leaves)
        uncovered = sorted(leaves - set().union(*members.values()))
        for name in uncovered:
            component = components[name]
            diagnostics.append(
                {
                    "code": "v14.unassigned_native",
                    "blocking": False,
                    "message": (
                        "native material leaf is not attached by any declared link or proven "
                        "rigid attachment; it stays outside the selected model and its measured "
                        "mass must be accounted explicitly"
                    ),
                    "detail": {
                        "component": name,
                        "document": component.get("document"),
                        "configuration": component.get("configuration"),
                    },
                }
            )
        state.update(
            components=components,
            members=members,
            leaves=leaves,
            selected=selected,
            completion=added,
            rigid_conflicts=conflicts,
        )
        details = {
            "leaves": len(leaves),
            "covered": len(leaves) - len(uncovered),
            "uncovered": uncovered,
            "suppressed": sorted(set(suppressed)),
            "rigid_completion": added,
            "rigid_proofs": proofs,
        }
        return details

    def frame(name):
        link = state["links"][name]
        joint = link.get("joint") or {}
        feature, owner = _selector(joint.get("coordinate_system_name"))
        candidates = [
            datum for datum in raw.get("datums") or [] if datum.get("name") == feature and datum.get("owner") == owner
        ]
        _require(
            len(candidates) == 1,
            "v14.datum_unread",
            "the named coordinate system was not read uniquely",
            feature=feature,
            owner=owner,
        )
        _require(
            sum(datum.get("name") == feature for datum in raw.get("datums") or []) == 1,
            "v14.datum_ambiguous",
            "the delivery frame name is ambiguous across occurrences",
            feature=feature,
        )
        if owner:
            _require(owner in state.get("components", {}), "v14.datum_owner", "unknown coordinate-system occurrence")
            owned = _expanded(owner, state["leaves"])
            _require(owned <= state["members"][name], "v14.datum_owner", "coordinate system belongs to another link")
        else:
            _require(
                candidates[0].get("configuration") == raw.get("saved_configuration"),
                "v14.capture_binding",
                "the datum was read in another configuration",
            )
        matrix = _cad_pose(candidates[0]["array"])
        state.setdefault("frames", {})[name] = matrix
        state.setdefault("frame_names", {})[name] = feature
        return {"coordinate_system": feature, "owner": owner}

    def optional(name, joint):
        populated = {
            kind: values
            for kind, values in joint.get("optional", {}).items()
            if values is not None and any(value is not None for value in values.values())
        }
        _require(
            not populated,
            "v14.unsupported_semantics",
            "authored optional joint semantics cannot be discarded",
            joint=name,
            values=populated,
        )
        return {}

    def joint_limits(name, joint):
        if joint["type"] == "fixed":
            return {"type": "fixed"}
        limits = joint.get("limit")
        _require(isinstance(limits, dict), "v14.limits", "moving joint has no authored limit record", joint=name)
        raw_limits = dict(limits)
        invalid = [key for key in ("effort", "velocity") if not (_number(limits.get(key)) and limits[key] >= 0)]
        if joint["type"] in {"revolute", "prismatic"} and not (
            _number(limits.get("lower")) and _number(limits.get("upper")) and limits["lower"] <= limits["upper"]
        ):
            invalid.append("ordered_bounds")
        _require(
            not invalid,
            "v14.operating_parameters",
            "authored bounds must be ordered and effort/velocity non-negative",
            joint=name,
            limits=limits,
            invalid=invalid,
        )
        native_limits = []
        native_complete = True
        if joint["type"] in {"revolute", "prismatic"}:
            child = joint["child_link"]
            child_tree = {child}
            parents = state.get("parents") or {}
            while True:
                expanded_tree = child_tree | {
                    candidate for candidate, parent in parents.items() if parent in child_tree
                }
                if expanded_tree == child_tree:
                    break
                child_tree = expanded_tree
            members = state.get("members") or {}
            leaves = state.get("leaves") or set()

            def owner_for(component: str) -> str | None:
                expanded = _expanded(component, leaves)
                owners = [body for body, body_members in members.items() if expanded and expanded <= body_members]
                return owners[0] if len(owners) == 1 else None

            raw_mates = raw.get("mates")
            if not isinstance(raw_mates, list) or not raw_mates:
                native_complete = False
                raw_mates = []
            limit_types = {"distance", "angle", "limitdistance", "limitangle"}
            for mate in raw_mates:
                if not isinstance(mate, dict):
                    native_complete = False
                    continue
                suppressed = mate.get("suppressed")
                if type(suppressed) is not bool:
                    native_complete = False
                    continue
                if suppressed:
                    continue
                mate_type = str(mate.get("type") or "").strip().lower()
                range_candidate = False
                if mate_type in limit_types:
                    flag = mate.get("is_advanced_mate")
                    if type(flag) is not bool or mate.get("is_advanced_mate_error") is not None:
                        native_complete = False
                        continue
                    if flag is True:
                        if isinstance(mate.get("native_limit"), dict) and isinstance(mate.get("limits"), dict):
                            range_candidate = True
                        else:
                            native_complete = False
                    elif mate_type in {"limitangle", "limitdistance"} or mate.get("limits") is not None:
                        native_complete = False
                        if isinstance(mate.get("limits"), dict):
                            native_limits.append(
                                {
                                    "mate": str(mate.get("name") or "unknown"),
                                    "type": mate_type,
                                    "is_advanced_mate": flag,
                                    "limits": mate["limits"],
                                }
                            )
                if range_candidate and (
                    "limits" not in mate
                    or type(mate.get("error_code")) is not int
                    or mate["error_code"] != 0
                ):
                    native_complete = False
                if isinstance(mate.get("limits"), dict) and not range_candidate:
                    native_complete = False
                    native_limits.append(
                        {"mate": str(mate.get("name") or "unknown"), "type": mate_type, "limits": mate["limits"]}
                    )
                if not range_candidate and mate.get("limits") is None:
                    continue
                entities = mate.get("entities")
                if not isinstance(entities, list) or len(entities) != 2:
                    if range_candidate:
                        native_complete = False
                    continue
                owners = []
                for entity in entities:
                    component = entity.get("component") if isinstance(entity, dict) else None
                    owner = owner_for(component) if isinstance(component, str) and component else None
                    if owner is None:
                        native_complete = False
                        break
                    owners.append(owner)
                if len(owners) != 2:
                    continue
                if (owners[0] in child_tree) == (owners[1] in child_tree):
                    continue
                mate_name = str(mate.get("name") or "")
                if range_candidate:
                    native_limits.append(
                        {
                            "mate": mate_name,
                            "type": mate_type,
                            "is_advanced_mate": mate.get("is_advanced_mate"),
                            "limits": mate["limits"],
                        }
                    )
            if not native_complete:
                raise DeclarationError(
                    "v14.native_limits_unreadable",
                    "the native range observation is incomplete or unreadable",
                    {"joint": name, "native": native_limits},
                )
        if joint["type"] in {"revolute", "prismatic"}:
            ranges = {
                (float(item["limits"]["lower"]), float(item["limits"]["upper"]))
                for item in native_limits
                if isinstance(item.get("limits"), dict)
            }
            if len(ranges) > 1:
                raise DeclarationError(
                    "v14.native_limits_conflict",
                    "native range observations disagree for one joint",
                    {"joint": name, "ranges": sorted(ranges)},
                )
            if ranges:
                lower, upper = next(iter(ranges))
                if not _all_zero_limits(limits) and (
                    abs(float(limits["lower"]) - lower) > 1e-12
                    or abs(float(limits["upper"]) - upper) > 1e-12
                ):
                    raise DeclarationError(
                        "v14.native_limits_conflict",
                        "native range disagrees with the saved author declaration",
                        {"joint": name, "native": [lower, upper], "declared": raw_limits},
                    )
                limits = {
                    "lower": lower,
                    "upper": upper,
                    "effort": raw_limits.get("effort"),
                    "velocity": raw_limits.get("velocity"),
                }
                state.setdefault("effective_limits", {})[name] = dict(limits)
                state.setdefault("effective_limit_sources", {})[name] = "native"
            elif _all_zero_limits(raw_limits):
                raise DeclarationError(
                    "v14.native_limits_missing",
                    "the CAD has no active working range and the saved declaration is only a zero placeholder",
                    {"joint": name, "declared": raw_limits},
                )
            else:
                state.setdefault("effective_limits", {})[name] = dict(limits)
                state.setdefault("effective_limit_sources", {})[name] = "declared_author"
        unqualified = [key for key in ("effort", "velocity") if limits.get(key) == 0]
        if joint["type"] in {"revolute", "prismatic"} and limits.get("lower") == limits.get("upper"):
            unqualified.append("degenerate_position_range")
        if unqualified:
            diagnostics.append(
                {
                    "code": "v14.motion_unqualified",
                    "blocking": False,
                    "message": (
                        "the exporter declaration's motion/drive values are zero or degenerate; consumers "
                        "may import a zero-range joint as unlimited, so motion is unqualified and not robot-ready"
                    ),
                    "detail": {"joint": name, "values": unqualified, "limits": dict(limits)},
                }
            )
        return {
            "limits": limits,
            "declared_limits": raw_limits,
            "limit_source": state.get("effective_limit_sources", {}).get(name, "declared_author"),
        }

    def joint_geometry(name, joint):
        parent, child = joint["parent_link"], joint["child_link"]
        _require(
            parent in state.get("frames", {}) and child in state.get("frames", {}),
            "v14.datum_unread",
            "joint datums are incomplete",
            joint=name,
        )
        origin = joint.get("origin") or {}
        _require(type(origin.get("customized")) is bool, "v14.origin_unread", "origin customization state is unread")
        if origin["customized"]:
            observed = np.linalg.inv(state["frames"][parent]) @ state["frames"][child]
            _same_pose(_pose(origin.get("xyz"), origin.get("rpy")), observed)
        if joint["type"] == "fixed":
            return {"origin_customized": origin["customized"]}
        feature, owner = _selector(joint.get("axis_name"))
        reference = {"kind": "reference_axis", "component": owner, "feature_name": feature}
        candidates = [item for item in raw.get("reference_axes") or [] if item.get("selector") == reference]
        _require(len(candidates) == 1, "v14.axis_unread", "the declared native axis was not read uniquely", joint=name)
        if owner:
            material = _expanded(owner, state["leaves"])
            _require(
                material and material <= (state["members"][parent] | state["members"][child]),
                "v14.axis_owner",
                "axis occurrence belongs to a foreign declared link",
                joint=name,
            )
        components = {
            key: _cad_pose(value["transform"])
            for key, value in state["components"].items()
            if value["suppressed"] is False
        }
        details = _axis(
            {"axis_reference": reference, "axis": joint.get("axis")}, candidates[0], components, state["frames"][child]
        )
        state.setdefault("axes", {})[name] = reference
        return {**details, "origin_customized": origin["customized"]}

    def pose_diagnostics():
        from .native_discovery import _frames

        if "members" not in state:
            return [
                {
                    "code": "v14.pose_diagnostics_unread",
                    "blocking": False,
                    "message": "saved-pose diagnostics await complete PID membership reads",
                    "detail": {},
                }
            ]
        components, leaves, members = state["components"], state["leaves"], state["members"]
        owners = {}
        for component in components:
            material = _expanded(component, leaves)
            matching = [name for name, group in members.items() if material and material <= group]
            if len(matching) == 1:
                owners[component] = matching[0]
        findings = []
        frames = _frames(raw)
        measured = []
        for mate in raw.get("mates") or []:
            if not isinstance(mate, dict) or mate.get("suppressed") is True:
                continue
            rows = _measured_rows(mate, frames, components)
            if rows is None:
                findings.append(
                    {
                        "code": "v14.mate_inference_unavailable",
                        "blocking": False,
                        "message": "saved mate evidence cannot independently reconstruct a pose constraint",
                        "detail": {
                            "mate": mate.get("name"),
                            "scope": mate.get("scope"),
                            "mate_type": mate.get("type"),
                            "error_code": mate.get("error_code"),
                        },
                    }
                )
                continue
            names = [entity["component"] for entity in mate["entities"]]
            measured.append((mate, names, rows["rows"]))
        for name, joint in state["joints"].items():
            if joint["type"] == "fixed" or joint["child_link"] not in state.get("frames", {}):
                continue
            direction = np.asarray(joint.get("axis"), dtype=float)
            if direction.shape != (3,) or not np.isfinite(direction).all() or abs(np.linalg.norm(direction) - 1) > 1e-8:
                continue
            frame = state["frames"][joint["child_link"]]
            direction = frame[:3, :3] @ direction
            twist = (
                np.r_[direction, [0, 0, 0]]
                if joint["type"] == "prismatic"
                else np.r_[-np.cross(direction, frame[:3, 3]), direction]
            )
            descendants = {joint["child_link"]}
            while True:
                expanded = descendants | {child for child, parent in state["parents"].items() if parent in descendants}
                if expanded == descendants:
                    break
                descendants = expanded
            for mate, names, rows in measured:
                left, right = [owners.get(component) for component in names]
                if left is None or right is None or (left in descendants) == (right in descendants):
                    continue
                residuals = [float(np.dot(row, twist)) for row in rows]
                normalized = [
                    abs(residual) / max(float(np.linalg.norm(row)), 1e-12)
                    for row, residual in zip(rows, residuals, strict=True)
                ]
                if any(residual > 1e-6 for residual in normalized):
                    findings.append(
                        {
                            "code": "v14.saved_pose_restriction",
                            "blocking": False,
                            "message": "an active native mate restricts the declared joint in the saved CAD pose",
                            "detail": {
                                "joint": name,
                                "mate": mate.get("name"),
                                "scope": mate.get("scope"),
                                "mate_type": mate.get("type"),
                                "components": names,
                                "links": [left, right],
                                "rows": rows,
                                "declared_twist": twist.tolist(),
                                "raw_residuals": residuals,
                                "max_abs_residual": max(abs(value) for value in residuals),
                                "normalized_residuals": normalized,
                                "max_normalized_residual": max(normalized),
                                "threshold": 1e-6,
                            },
                        }
                    )
        return findings

    def claims():
        _require("members" in state, "v14.membership_unread", "claim comparison awaits complete PID membership reads")
        _require(isinstance(source, dict), "v14.claims", "generated source claims are absent")
        bodies, joints = source.get("bodies"), source.get("joints")
        _require(isinstance(bodies, list) and len(bodies) == len(state["links"]), "v14.claims", "body count differs")
        by_name = {body.get("name"): body for body in bodies}
        _require(len(by_name) == len(bodies) and set(by_name) == set(state["links"]), "v14.claims", "body names differ")
        for name, body in by_name.items():
            _require(
                isinstance(body.get("components"), list)
                and len(set(body["components"])) == len(body["components"])
                and set(body["components"]) == state["members"][name],
                "v14.claims",
                "generated membership differs from native evidence",
                link=name,
            )
            _require(
                body.get("frame") == {"coordinate_system": state["frame_names"][name]},
                "v14.claims",
                "generated body datum differs from the saved native selection",
                link=name,
            )
        _require(isinstance(joints, list) and len(joints) == len(state["joints"]), "v14.claims", "joint count differs")
        by_joint = {joint.get("name"): joint for joint in joints}
        _require(
            len(by_joint) == len(joints) and set(by_joint) == set(state["joints"]), "v14.claims", "joint names differ"
        )
        for name, saved in state["joints"].items():
            observed = by_joint[name]
            expected = {"type": saved["type"], "parent": saved["parent_link"], "child": saved["child_link"]}
            _require(
                all(observed.get(key) == value for key, value in expected.items()),
                "v14.claims",
                "generated joint topology differs",
                joint=name,
            )
            if saved["type"] != "fixed":
                limits = state.get("effective_limits", {}).get(name, saved["limit"])
                if saved["type"] == "continuous":
                    limits = {key: limits[key] for key in ("effort", "velocity")}
                _require(
                    observed.get("limits") == limits
                    and observed.get("axis") == saved["axis"]
                    and observed.get("axis_reference") == state["axes"].get(name),
                    "v14.claims",
                    "generated joint axis or operating values differ",
                    joint=name,
                )
        return {"bodies": len(bodies), "joints": len(joints)}

    if check("declarations.binding", boundary) and check("declarations.topology", topology):
        check("declarations.membership", membership)
        for name in state["links"]:
            check("declarations.frame." + name, lambda n=name: frame(n))
            metadata = state["links"][name].get("joint")
            if metadata is not None:
                check("declarations.optional." + name, lambda n=name, j=metadata: optional(n, j))
        for name, joint in state["joints"].items():
            check("declarations.limits." + name, lambda n=name, j=joint: joint_limits(n, j))
            check("declarations.axis." + name, lambda n=name, j=joint: joint_geometry(n, j))
        # Saved mates may intentionally hold an authored moving joint at its current
        # pose. Preserve the measured restriction separately; it is no pass/fail gate.
        diagnostics.extend(pose_diagnostics())
        if source is not None:
            check("declarations.claims", claims)
    return {
        "schema_version": SCHEMA,
        "passed": not errors and bool(checks),
        "checks": checks,
        "errors": errors,
        "diagnostics": diagnostics,
        "members": {name: sorted(values) for name, values in state.get("members", {}).items()},
        "joints": len(state.get("joints") or {}),
        "completion": state.get("completion", {}),
        #: The consumer expectations decoded from the sealed declaration itself (mode routing
        #: for the URDF stage): same decode the audit above verified, never the derived copy.
        "carrier": {
            "root": ((state.get("declaration") or {}).get("root") or {}).get("name"),
            "expansion": {
                name: sorted(_expanded(name, state.get("leaves") or set()))
                for name in (state.get("components") or {})
            },
            "bodies": [
                {
                    "id": name,
                    "name": name,
                    "components": sorted(state["members"][name]),
                    "frame": {"coordinate_system": state["frame_names"][name]},
                }
                for name in sorted(state.get("links") or {})
                if name in (state.get("members") or {}) and name in (state.get("frame_names") or {})
            ],
            "joints": [
                {
                    "id": name,
                    "name": name,
                    "type": joint["type"],
                    "parent": joint["parent_link"],
                    "child": joint["child_link"],
                    "axis": joint.get("axis"),
                    "axis_reference": (state.get("axes") or {}).get(name),
                    "limits": (
                        {key: joint["limit"][key] for key in ("effort", "velocity")}
                        if joint["type"] == "continuous" and isinstance(joint.get("limit"), dict)
                        else state.get("effective_limits", {}).get(name, joint.get("limit"))
                    ),
                    **(
                        {"limit_source": state["effective_limit_sources"][name]}
                        if name in state.get("effective_limit_sources", {})
                        else {}
                    ),
                }
                for name, joint in sorted((state.get("joints") or {}).items())
            ],
        },
    }
