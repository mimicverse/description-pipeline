"""Independently rebuild a saved-state inspection carrier from native facts."""

from __future__ import annotations

import math
import re
from pathlib import Path

from description_pipeline.schemas import (
    SAVED_NATIVE_REFERENCE_DOT_TOL,
    SAVED_NATIVE_REFERENCE_POSITION_TOL_M,
    SAVED_NATIVE_REFERENCE_RULE,
)
from description_pipeline.sources.solidworks.direction import canonicalize_axis
from description_pipeline.sources.solidworks.naming import (
    carrier_names_and_rules,
    disambiguated_frame_name,
    frame_identity,
    frame_name,
    token as format_token,
)
from description_pipeline.sources.solidworks.native_limits import (
    PREVIEW_POLICY_ID,
    PREVIEW_WINDOW_RAD,
    native_limit_inventory_complete,
    rotation_window_policy,
    validate_native_limit,
)
from description_pipeline.verification.native_discovery import (
    _Failure,
    _find_record_entry,
    _record_value,
    project_native_verification_record,
)

REFERENCE_TYPES = {2: "Line", 4: "Cylinder", 7: "Circle"}


def _require(condition, message, **detail):
    if not condition:
        raise _Failure("discovery.saved_native", message, detail)


def _token(value):
    try:
        return format_token(value)
    except ValueError as error:
        raise _Failure("discovery.saved_native", "native name cannot be tokenized", {"native_name": value}) from error


def _number(value):
    _require(
        type(value) in (int, float) and math.isfinite(value),
        "native number must be finite",
        value=value,
    )
    return float(value)


def _vector(value, length=3):
    _require(
        isinstance(value, (list, tuple)) and len(value) == length,
        "native vector has an invalid shape",
    )
    return [_number(item) for item in value]


def _unit(value):
    values = _vector(value)
    norm = math.sqrt(sum(item * item for item in values))
    _require(norm > 1e-12, "native axis is degenerate")
    return [item / norm for item in values]


def _dot(left, right):
    return sum(a * b for a, b in zip(left, right, strict=True))


def _same_line(point, direction, other_point, other_direction):
    delta = [a - b for a, b in zip(point, other_point, strict=True)]
    along = _dot(delta, direction)
    distance = math.sqrt(sum((item - along * axis) ** 2 for item, axis in zip(delta, direction, strict=True)))
    return (
        distance <= SAVED_NATIVE_REFERENCE_POSITION_TOL_M
        and abs(_dot(direction, other_direction)) >= 1.0 - SAVED_NATIVE_REFERENCE_DOT_TOL
    )


def _local(frame, direction):
    return _unit([sum(frame[row * 4 + column] * direction[row] for row in range(3)) for column in range(3)])


def _context(component, document, configuration, scope):
    observation = component.get("solver_observation")
    return isinstance(observation, dict) and observation.get("context") == {
        "document": document,
        "configuration": configuration,
        "scope": scope,
    }


def _semantic(datum, component):
    """Use exact native semantic tokens only inside geometric candidates."""
    if _token(datum[2:]).startswith("auto_"):
        return False
    occurrence = component["name2"].rsplit("/", 1)[-1]
    occurrence = re.sub(r"-\d+$", "", occurrence)
    document = str(component.get("document") or "").replace("\\", "/").rsplit("/", 1)[-1]
    document = re.sub(r"\.(?:sldasm|sldprt)$", "", document, flags=re.IGNORECASE)
    candidates = [re.sub(r"[_-]\d{8}$", "", value) for value in (occurrence, document)]
    semantic = _token(datum[2:])
    for candidate in candidates:
        try:
            if candidate and format_token(candidate) == semantic:
                return True
        except ValueError:
            continue
    return False


def _unique_binding(domains):
    """Prove one complete bijection, stopping once ambiguity is witnessed."""
    solutions = []

    def visit(remaining, chosen):
        if len(solutions) == 2:
            return
        if not remaining:
            solutions.append(dict(chosen))
            return
        key = min(remaining, key=lambda item: (len(remaining[item]), item))
        for candidate in sorted(remaining[key]):
            reduced = {other: values - {candidate} for other, values in remaining.items() if other != key}
            if any(not values for values in reduced.values()):
                continue
            visit(reduced, {**chosen, key: candidate})

    visit(domains, {})
    _require(
        len(solutions) == 1,
        "saved datums do not have one complete geometric binding",
        solutions=len(solutions),
    )
    return solutions[0]


def _properties(raw, mates, datum, namespace):
    """Read native declarations at the mate and named-document locations."""
    properties = raw.get("properties") or {}
    _require(isinstance(properties, dict), "native properties must be an object")
    document = properties.get("document") or {}
    by_mate = properties.get("mates") or {}
    _require(
        isinstance(document, dict) and isinstance(by_mate, dict),
        "native property buckets must be objects",
    )
    readings = {}
    consumed = set()
    prefix = namespace + ".joint."
    names = [str(mate.get("name") or "") for mate in mates]
    heads = {name.partition("__")[0] for name in names if "__" in name}
    selectors = {*names, *heads, datum[2:], "a_" + datum[2:]}
    for name in names:
        values = by_mate.get(name) or {}
        if any(isinstance(key, str) and key.startswith(prefix) for key in values):
            matches = [mate for mate in raw["mates"] if mate.get("name") == name]
            _require(len(matches) == 1, "mate declaration name is ambiguous across native scopes", mate=name)
        _require(isinstance(values, dict), "mate properties must be an object", mate=name)
        for key, value in values.items():
            if key.startswith(prefix):
                readings.setdefault(key[len(prefix) :], []).append(value)
                consumed.add(("mate", name, key))
    for key, value in document.items():
        if not isinstance(key, str):
            continue
        for selector in selectors:
            scoped = prefix + selector + "."
            if key.startswith(scoped):
                readings.setdefault(key[len(scoped) :], []).append(value)
                consumed.add(("document", "", key))
    result = {}
    for key, values in readings.items():
        _require(
            all(type(value) is type(values[0]) and value == values[0] for value in values),
            "native joint declarations conflict",
            field=key,
        )
        _require(
            key in {"type", "axis_sign", "limits_record", "drive_record"},
            "saved-state carrier cannot consume this native joint declaration",
            field=key,
        )
        result[key] = values[0]
    return result, consumed


def _controlled(reference, payload, package):
    _require(
        isinstance(reference, str) and bool(reference.strip()),
        "controlled record reference must be nonempty text",
    )
    _require(
        isinstance(payload, dict) and package is not None,
        "controlled record requires its sealed payload and package",
    )
    found = _find_record_entry(payload, reference)
    entry = found["entry"]
    _require(
        isinstance(entry.get("anchor"), str) and bool(entry["anchor"]),
        "controlled record has no sealed anchor",
    )
    value = _record_value(Path(package), entry, found["key"])
    _require(isinstance(value, dict), "controlled record value must be an object")
    evidence = {
        "kind": "controlled_record",
        "file": entry.get("package_file"),
        "sha256": entry.get("sha256"),
        "anchor": entry["anchor"],
        "key": found["key"],
        "reference": reference,
        "declared_values": {
            key: value[key] for key in ("lower", "upper", "effort", "velocity") if value.get(key) is not None
        },
    }
    return value, evidence


def _limits(mates, properties, components, direction, payload, package, *, joint_type):
    for mate in mates:
        if mate.get("suppressed") is True:
            continue
        kind = mate.get("type")
        if (
            kind in {"limitangle", "limitdistance"}
            or mate.get("native_limit") is not None
            or mate.get("limits") is not None
        ):
            try:
                lower, upper, unit = validate_native_limit(mate, components)
            except ValueError as error:
                raise _Failure(
                    "discovery.saved_native",
                    "native position limit is invalid or unreadable",
                    {
                        "mate": mate.get("name"),
                        "error": str(error),
                    },
                ) from error
            _require(
                unit == "rad",
                "saved rotational carrier requires a native angular range",
            )
            # An angular range's plane vectors alone do not identify its signed
            # motion coordinate. Never copy such a range onto a reference axis.
            _require(
                False,
                "native angular range has no proven signed coordinate binding",
                mate=mate.get("name"),
                native_range=[lower, upper],
                assembly_direction=direction,
            )
    _require(
        native_limit_inventory_complete(mates),
        "native limit absence is not completely observed",
    )
    limits = {}
    evidence = None
    drive = None
    drive_evidence = None
    source = "declared_author" if joint_type == "continuous" else None
    policy = None
    if "limits_record" in properties:
        _require(joint_type != "continuous", "continuous joint cannot bind a controlled position record")
        value, record_evidence = _controlled(properties["limits_record"], payload, package)
        evidence = {**record_evidence, "native_observation_errors": []}
        if joint_type == "continuous":
            _require(
                all(value.get(key) is None for key in ("lower", "upper")),
                "continuous joint cannot carry controlled position bounds",
            )
        else:
            lower, upper = _number(value.get("lower")), _number(value.get("upper"))
            _require(lower <= upper, "controlled position range is inverted")
            limits = {"lower": lower, "upper": upper}
            source = "declared_author"
        readings = {key: _number(value[key]) for key in ("effort", "velocity") if key in value}
        _require(all(number >= 0 for number in readings.values()), "controlled drive values must be nonnegative")
        limits.update(readings)
        if readings:
            drive_evidence = record_evidence
            drive = {
                "file": record_evidence["file"],
                "sha256": record_evidence["sha256"],
                "anchor": record_evidence["anchor"],
                "value": readings,
            }
    elif joint_type == "revolute":
        limits = {"lower": -PREVIEW_WINDOW_RAD, "upper": PREVIEW_WINDOW_RAD}
        source = "preview_policy"
        policy = rotation_window_policy(
            PREVIEW_POLICY_ID,
            author_limits=None,
            native_absence="complete_mate_inventory_no_enabled_limit",
        )
    if "drive_record" in properties:
        value, drive_evidence = _controlled(properties["drive_record"], payload, package)
        readings = {key: _number(value[key]) for key in ("effort", "velocity") if key in value}
        _require(bool(readings), "controlled drive record has no readable effort or velocity")
        _require(
            all(number >= 0 for number in readings.values()),
            "controlled drive values must be nonnegative",
        )
        _require(
            all(key not in limits or limits[key] == number for key, number in readings.items()),
            "controlled drive and range records disagree",
        )
        limits.update(readings)
        drive = {
            "file": drive_evidence["file"],
            "sha256": drive_evidence["sha256"],
            "anchor": drive_evidence["anchor"],
            "value": readings,
        }
    return limits, source, policy, evidence, drive, drive_evidence


def rebuild_saved_native_references(raw, *, purpose, payload=None, package=None):
    """Rebuild only the absent-exporter, saved-state inspection interpretation.

    No output is a solver-rank proof or a hardware qualification. All material
    ownership and all datum bindings are rebuilt from the preserved native view.
    """
    _require(
        purpose == "inspection_preview",
        "saved-native carriers only support inspection preview",
    )
    _require(
        isinstance(raw, dict) and isinstance(raw.get("exporter"), dict) and raw["exporter"].get("state") == "absent",
        "saved-native carriers require proven exporter absence",
    )
    view = project_native_verification_record(raw)
    configuration = view.get("saved_configuration")
    document = (view.get("identity") or {}).get("main_assembly")
    _require(
        isinstance(configuration, str) and bool(configuration) and isinstance(document, str) and bool(document),
        "saved-native carrier has no frozen document configuration",
    )
    exporter = view["exporter"]
    _require(
        exporter.get("document") == document
        and exporter.get("configuration") == configuration
        and exporter.get("error") is None
        and exporter.get("raw_xml") in (None, "")
        and exporter.get("registration") in (None, {})
        and exporter.get("pid_bindings") in (None, [])
        and all(value is None for key, value in exporter.items() if key.endswith("_error")),
        "absent exporter observation conflicts with its identity or declaration content",
    )
    components = {item["name2"]: item for item in view["components"]}
    active = {}
    for name, item in components.items():
        _require(
            type(item.get("suppressed")) is bool,
            "component suppression is unreadable",
            component=name,
        )
        if not item["suppressed"]:
            _require(
                len(item.get("transform") or []) == 16,
                "active component transform is unreadable",
                component=name,
            )
            active[name] = item
    for name, item in active.items():
        parent = item["parent_occurrence"]
        expected_document = document if not parent else active[parent]["document"]
        expected_configuration = configuration if not parent else active[parent]["configuration"]
        _require(
            _context(item, expected_document, expected_configuration, parent),
            "component solver observation has the wrong immediate parent context",
            component=name,
        )
    for name, item in active.items():
        extension = str(item.get("document") or "").lower()
        children = [child for child in active.values() if child["parent_occurrence"] == name]
        if extension.endswith(".sldasm"):
            _require(bool(children), "folded assembly needs a direct active child", component=name)
            solving = item["solver_observation"].get("solving")
            _require(
                isinstance(solving, dict)
                and solving.get("method") == "IComponent2.Solving"
                and solving.get("family") == "swComponentSolvingOption_e"
                and type(solving.get("value")) is int
                and solving["value"] == 0
                and solving.get("error") is None,
                "folded assembly is not a clean native rigid solving observation",
                component=name,
            )
        else:
            _require(
                extension.endswith(".sldprt") and not any(key.startswith(name + "/") for key in components),
                "a material part must have no assembly subtree",
                component=name,
            )
    tops = {name: item for name, item in active.items() if not item["parent_occurrence"]}
    _require(bool(tops), "saved-native carrier has no material bodies")
    materials = {}
    for name in tops:
        members = [key for key in active if key == name or key.startswith(name + "/")]
        leaves = sorted(key for key in members if str(active[key].get("document") or "").lower().endswith(".sldprt"))
        _require(bool(leaves), "top-level occurrence has no material part", component=name)
        materials[name] = leaves
    fixed = [name for name, item in tops.items() if item.get("fixed") is True and item.get("fixed_error") is None]
    _require(
        all(type(item.get("fixed")) is bool and item.get("fixed_error") is None for item in tops.values()),
        "top-level fixed state is unreadable",
    )
    _require(
        len(fixed) == 1,
        "saved-native preview needs exactly one saved fixed top-level occurrence",
    )
    root_occurrence = fixed[0]

    pairs = {}
    for datum in view["datums"]:
        name = datum.get("name")
        if not isinstance(name, str) or not name.startswith("c_"):
            continue
        context = (datum.get("references") or {}).get("context")
        _require(
            datum.get("owner") == ""
            and datum.get("configuration") == configuration
            and context == {"document": document, "configuration": configuration, "scope": ""}
            and datum.get("context", context) == context
            and datum.get("error") is None
            and all(value is None for key, value in datum.items() if key.endswith("_error")),
            "saved coordinate has the wrong native scope or configuration",
            datum=name,
        )
        _require(
            name not in pairs and len(name) > 2,
            "saved coordinate name is empty or duplicated",
            datum=name,
        )
        frame = _vector(datum["array"], 16)
        pairs[name] = {"frame": frame, "point": [frame[3], frame[7], frame[11]]}
    axes = {}
    for axis in view.get("reference_axes") or []:
        selector = axis.get("selector") or {}
        name = selector.get("feature_name")
        if not isinstance(name, str) or not name.startswith("a_"):
            continue
        _require(
            name not in axes
            and axis.get("component") == ""
            and selector.get("component") == ""
            and selector.get("kind") == "reference_axis"
            and axis.get("kind") == "reference_axis"
            and axis.get("coordinate_frame") == "assembly"
            and axis.get("used_api") == "IFeature.GetSpecificFeature2/IRefAxis.GetRefAxisParams"
            and axis.get("direction_semantics") == "undirected_axis_line",
            "saved axis has the wrong native identity or scope",
            axis=name,
        )
        _require(
            axis.get("configuration", configuration) == configuration
            and axis.get(
                "context",
                {"document": document, "configuration": configuration, "scope": ""},
            )
            == {"document": document, "configuration": configuration, "scope": ""}
            and axis.get("error") is None
            and all(value is None for key, value in axis.items() if key.endswith("_error")),
            "saved axis has a conflicting configuration or read error",
            axis=name,
        )
        endpoints = _vector(axis.get("raw_endpoints_m"), 6)
        direction = _unit([endpoints[index + 3] - endpoints[index] for index in range(3)])
        point = _vector(axis.get("axis_point_m"))
        recorded = _unit(axis.get("axis_direction"))
        _require(
            _same_line(endpoints[:3], direction, point, recorded),
            "saved axis endpoints disagree with the captured point or direction",
            axis=name,
        )
        axes[name] = {
            "point": point,
            "direction": direction,
            "raw_endpoints_m": endpoints,
        }
    _require(
        set(pairs) == {"c_" + name[2:] for name in axes} and "c_base_link" in pairs and len(pairs) == len(tops),
        "saved coordinates and axes must form one complete unique pair per body",
    )
    for name, pair in pairs.items():
        pair.update(axes["a_" + name[2:]])
        frame_point = [pair["frame"][index] for index in (3, 7, 11)]
        _require(
            _same_line(frame_point, pair["direction"], pair["point"], pair["direction"]),
            "saved coordinate origin is off its paired axis",
            datum=name,
        )

    def owner(entity):
        component = entity.get("component")
        if component == "" and entity.get("assembly_frame") is True:
            return None
        _require(
            component in active,
            "mate endpoint is not an active exact occurrence",
            component=component,
        )
        top = component.split("/", 1)[0]
        _require(
            top in materials,
            "mate endpoint is not owned by one material body",
            component=component,
        )
        return top

    def endpoints(mate):
        entities = mate.get("entities")
        _require(
            isinstance(entities, list)
            and type(mate.get("entity_count")) is int
            and mate["entity_count"] == len(entities)
            and bool(entities),
            "mate endpoint inventory is incomplete",
            mate=mate.get("name"),
        )
        for index, entity in enumerate(entities):
            _require(
                type(entity.get("entity_index")) is int and entity["entity_index"] == index,
                "mate endpoint order is invalid",
                mate=mate.get("name"),
            )
            reference = entity.get("mate_entity_reference")
            _require(
                isinstance(reference, dict) and reference.get("error") is None,
                "mate endpoint native reference is unreadable",
                mate=mate.get("name"),
                endpoint=index,
            )
        return entities

    edge_mates = {}
    edges = []
    poses = []
    mate_identities = set()
    for mate in view["mates"]:
        _require(
            type(mate.get("suppressed")) is bool,
            "mate suppression is unreadable",
            mate=mate.get("name"),
        )
        if mate["suppressed"]:
            continue
        _require(
            type(mate.get("error_code")) is int and mate["error_code"] == 0,
            "active native mate has an error",
            mate=mate.get("name"),
        )
        identity = (mate.get("scope"), mate.get("name"))
        _require(
            all(isinstance(value, str) for value in identity) and bool(identity[1])
            and identity not in mate_identities
            and isinstance(mate.get("type"), str) and bool(mate["type"])
            and isinstance(mate.get("configuration"), str) and bool(mate["configuration"]),
            "native mate scoped identity is missing or duplicated", mate=mate.get("name"),
        )
        mate_identities.add(identity)
        if mate["type"] == "inplace":
            _require(
                mate.get("native_feature_type") == "MateInPlace" and mate.get("native_interface") == "IMateInPlace"
                and isinstance(mate.get("inplace"), dict)
                and (identity[0] == "" or identity[0] in active)
                and mate["configuration"] == (active[identity[0]]["configuration"] if identity[0] else configuration),
                "in-place observation has no exact native owner context", mate=mate["name"],
            )
            poses.append((mate, frozenset({identity[0].split("/", 1)[0]}) if identity[0] else frozenset()))
            continue
        entities = endpoints(mate)
        owners = [owner(entity) for entity in entities]
        material_owners = frozenset(value for value in owners if value is not None)
        if mate.get("type") != "concentric" or mate.get("scope") or len(material_owners) < 2:
            poses.append((mate, material_owners))
            continue
        edge_mates.setdefault(material_owners, []).append(mate)
        _require(
            len(entities) == 2 and len(material_owners) == 2 and None not in owners,
            "concentric must bind two distinct material bodies",
            mate=mate.get("name"),
        )
        _require(
            mate.get("lock_rotation") is False
            and mate.get("lock_rotation_api") == "IConcentricMateFeatureData.LockRotation",
            "concentric rotation lock is not a clean false observation",
            mate=mate.get("name"),
        )
        geometry = []
        for entity in entities:
            _require(
                (isinstance(entity.get("feature"), str) and bool(entity["feature"]))
                or (type(entity.get("face_index")) is int and entity["face_index"] >= 0),
                "concentric endpoint has no native feature or face selector",
                mate=mate.get("name"),
            )
            reference = entity["mate_entity_reference"]
            reference_type = reference.get("reference_type")
            _require(
                type(reference_type) is int
                and reference_type in REFERENCE_TYPES
                and reference.get("source") == "mate-entity-params"
                and reference.get("space") == "mate-assembly",
                "concentric endpoint has no supported typed native mate vector",
                mate=mate.get("name"),
            )
            params = reference.get("params")
            minimum = 7 if reference_type in (4, 7) else 6
            _require(
                isinstance(params, list) and len(params) >= minimum,
                "native EntityParams are incomplete",
                mate=mate.get("name"),
            )
            values = [_number(value) for value in params]
            _require(
                reference_type == 2 or values[6] > 0,
                "native concentric endpoint radius must be positive",
                mate=mate.get("name"),
            )
            geometry.append((values[:3], _unit(values[3:6])))
        _require(
            _same_line(*geometry[0], *geometry[1]),
            "concentric endpoints disagree geometrically",
            mate=mate.get("name"),
        )
        previous = next((edge for edge in edges if edge["owners"] == material_owners), None)
        if previous is None:
            edges.append({"owners": material_owners, "geometry": geometry})
        else:
            previous["geometry"].extend(geometry)
    _require(
        len(edges) == len(tops) - 1,
        "active concentric edges do not form a complete tree",
    )
    edges.sort(key=lambda edge: tuple(sorted(edge["owners"])))
    poses.sort(key=lambda item: (item[0]["scope"], item[0]["name"]))
    parents = {root_occurrence: None}
    ordered = []
    queue = [root_occurrence]
    for current in queue:
        for edge in edges:
            if current not in edge["owners"]:
                continue
            other = next(value for value in edge["owners"] if value != current)
            if parents.get(current) == other:
                continue
            _require(other not in parents, "active concentric graph contains a cycle")
            parents[other] = current
            queue.append(other)
            ordered.append((other, edge))
    _require(set(parents) == set(tops), "active concentric graph is disconnected")
    adjacency = {edge["owners"] for edge in edges}
    for mate, owners in poses:
        _require(
            len(owners) <= 1 or owners in adjacency,
            "saved pose mate crosses nonadjacent material bodies",
            mate=mate.get("name"),
            bodies=sorted(owners),
        )

    domains = {}
    for child, edge in ordered:
        candidates = {
            name
            for name, pair in pairs.items()
            if name != "c_base_link"
            and all(_same_line(pair["point"], pair["direction"], *geometry) for geometry in edge["geometry"])
        }
        semantic = {name for name in candidates if _semantic(name, tops[child])}
        domains[child] = semantic or candidates
        _require(
            bool(domains[child]),
            "no saved datum has a two-endpoint geometric witness",
            component=child,
        )
    binding = {root_occurrence: "c_base_link", **_unique_binding(domains)}
    native_names = {occurrence: _token(datum[2:]) for occurrence, datum in binding.items()}
    _require(
        len(set(native_names.values())) == len(native_names),
        "native coordinate suffixes collide after formatting",
    )
    bodies = [
        {
            "id": native_names[occurrence],
            "name": native_names[occurrence],
            "source_name": native_names[occurrence],
            "components": materials[occurrence],
            "datum": datum,
            "frame": {"coordinate_system": datum},
        }
        for occurrence, datum in binding.items()
    ]
    diagnostics = [
        {
            "code": "saved_native.inspection_scope",
            "severity": "warning",
            "rule_id": SAVED_NATIVE_REFERENCE_RULE,
            "message": "Saved-state preview does not prove solver rank, source completeness or hardware qualification.",
            "saved_fixed_occurrence": root_occurrence,
            "native_positive_motion": False,
            "hardware_qualified": False,
        }
    ]
    if poses:
        diagnostics.append(
            {
                "code": "saved_native.pose_mates",
                "severity": "warning",
                "message": "Additional native pose mates are preserved without a mechanical rank claim.",
                "mates": [
                    {
                        "name": mate["name"],
                        "type": mate["type"],
                        "scope": mate["scope"],
                        "configuration": mate["configuration"],
                        "entities": mate["entities"],
                        "error_code": mate["error_code"],
                    }
                    for mate, _owners in poses
                ],
            }
        )
    joints = []
    consumed_properties = set()
    namespace = str(view.get("namespace") or "dp")
    for child, edge in ordered:
        parent = parents[child]
        datum = binding[child]
        pair = pairs[datum]
        members = edge_mates[edge["owners"]]
        properties, consumed = _properties(view, members, datum, namespace)
        consumed_properties.update(consumed)
        joint_type = properties.get("type", "revolute")
        _require(
            isinstance(joint_type, str) and joint_type in {"revolute", "continuous"},
            "native joint type is unsupported by the rotational preview",
            component=child,
        )
        sign_value = properties.get("axis_sign")
        sign = 1
        declared_direction = "axis_sign" in properties
        if declared_direction:
            _require(
                (type(sign_value) is int and sign_value in (-1, 1))
                or (type(sign_value) is str and sign_value in ("-1", "1", "+1")),
                "native axis_sign must be exactly +1 or -1",
                component=child,
            )
            sign = int(sign_value)
        direction = [sign * value for value in pair["direction"]]
        local = _local(pair["frame"], direction)
        axis_name = "a_" + datum[2:]
        provenance = canonicalize_axis(
            axis_name,
            local,
            pairs[binding[parent]]["frame"],
            pair["frame"],
            joint_type=joint_type,
        )
        provenance.update(
            {
                "source": "saved_native_reference",
                "point": pair["point"],
                "direction": direction,
                "assembly_direction": direction,
                "native_direction": provenance["parent_axis"],
                "original_assembly_direction": pair["direction"],
                "native_positive_motion": False,
                "hardware_qualified": False,
                "direction_rule": "native_axis_sign/v1" if declared_direction else "stored_endpoint_order/v1",
                "raw_endpoints_m": pair["raw_endpoints_m"],
            }
        )
        limits, source, policy, evidence, drive, drive_evidence = _limits(
            view["mates"], properties, components, direction, payload, package, joint_type=joint_type
        )
        joint = {
            "id": axis_name,
            "name": axis_name,
            "source_name": axis_name,
            "type": joint_type,
            "parent": native_names[parent],
            "child": native_names[child],
            "axis": local,
            "axis_reference": {
                "kind": "reference_axis",
                "component": "",
                "feature_name": axis_name,
            },
            "axis_provenance": provenance,
            "assembly_direction": direction,
            "limits": limits,
            "limit_source": source,
        }
        for key, value in (
            ("preview_policy", policy),
            ("limit_evidence", evidence),
            ("drive", drive),
            ("drive_evidence", drive_evidence),
        ):
            if value is not None:
                joint[key] = value
        if drive_evidence is not None:
            joint["drive_source"] = "controlled"
        joints.append(joint)
        diagnostics.append(
            {
                "code": "saved_native.direction",
                "severity": "warning",
                "joint": axis_name,
                "message": "Native axis sign is preserved; positive motion and hardware direction remain unqualified."
                if declared_direction
                else "Stored endpoint order defines display direction only; "
                "native positive motion and hardware direction are unqualified.",
                "rule_id": provenance["direction_rule"],
                "native_positive_motion": False,
                "hardware_qualified": False,
            }
        )
        if policy is not None:
            diagnostics.append(
                {
                    "code": "saved_native.preview_range",
                    "severity": "warning",
                    "joint": axis_name,
                    "message": "The inspection rotation window is a display policy, not a native or hardware range.",
                    **policy,
                }
            )
    properties = view.get("properties") or {}
    for key in properties.get("document") or {}:
        if isinstance(key, str) and key.startswith(namespace + ".joint."):
            _require(
                ("document", "", key) in consumed_properties,
                "native joint declaration does not bind one reconstructed joint",
                field=key,
            )
    for component_name, values in (properties.get("components") or {}).items():
        _require(isinstance(values, dict), "component properties must be objects", component=component_name)
        if "dp.body_datum" in values:
            component_owner = component_name.split("/", 1)[0]
            _require(
                component_owner in binding and values["dp.body_datum"] == binding[component_owner],
                "native body datum conflicts with the saved inspection declaration",
                component=component_name,
            )
        _require(
            not any(isinstance(key, str) and key.startswith(namespace + ".joint.") for key in values),
            "component joint declaration has no exact native joint binding",
            component=component_name,
        )
    for mate_name, values in (properties.get("mates") or {}).items():
        _require(isinstance(values, dict), "mate properties must be objects", mate=mate_name)
        for key in values:
            if isinstance(key, str) and key.startswith(namespace + ".joint."):
                _require(
                    ("mate", mate_name, key) in consumed_properties,
                    "native joint declaration does not bind one reconstructed joint",
                    mate=mate_name,
                    field=key,
                )
    link_names, joint_names, link_rules, joint_rules = carrier_names_and_rules(bodies, joints, "base_link")
    for body in bodies:
        native = body["name"]
        body["id"] = body["name"] = link_names[native]
        if native in link_rules:
            body["name_rule"] = link_rules[native]
    for joint in joints:
        native = joint["name"]
        joint["id"] = joint["name"] = joint_names[native]
        joint["parent"], joint["child"] = (
            link_names[joint["parent"]],
            link_names[joint["child"]],
        )
        if native in joint_rules:
            joint["name_rule"] = joint_rules[native]
    frame_candidates = []
    identities = set()
    for datum in view["datums"]:
        name, owner = datum.get("name"), datum.get("owner")
        if not isinstance(name, str) or not name.startswith(("CS_", "TCP_", "SCS_")):
            continue
        _require(
            owner == "" or owner in active,
            "interface frame owner is unavailable",
            datum=name,
        )
        expected_configuration = configuration if owner == "" else active[owner]["configuration"]
        _require(
            datum.get("configuration") == expected_configuration,
            "interface frame configuration conflicts with its owner",
            datum=name,
        )
        identity = frame_identity(owner, name, expected_configuration)
        _require(
            identity not in identities,
            "native interface frame identity is duplicated",
            datum=name,
        )
        identities.add(identity)
        native_parent = native_names[root_occurrence if owner == "" else owner.split("/", 1)[0]]
        try:
            formatted = frame_name(name)
        except ValueError as error:
            raise _Failure(
                "discovery.saved_native", "native interface name cannot be tokenized", {"datum": name}
            ) from error
        frame_candidates.append((identity, formatted, name, link_names[native_parent]))
    frames = []
    used_names = set(link_names.values())
    for identity, formatted, native, parent in sorted(frame_candidates):
        if sum(candidate[1] == formatted for candidate in frame_candidates) > 1 or formatted in used_names:
            formatted = disambiguated_frame_name(formatted, identity)
        _require(
            formatted not in used_names,
            "interface frame name cannot be uniquely formatted",
            datum=native,
        )
        used_names.add(formatted)
        frames.append(
            {
                "id": formatted,
                "name": formatted,
                "source_name": native,
                "parent": parent,
                "coordinate_system": native,
            }
        )
    return {
        "root": "base_link",
        "bodies": sorted(bodies, key=lambda item: item["name"]),
        "joints": sorted(joints, key=lambda item: item["name"]),
        "frames": frames,
        "diagnostics": diagnostics,
    }
