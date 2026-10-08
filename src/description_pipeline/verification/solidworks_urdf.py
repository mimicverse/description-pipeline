"""Independent gates for the bytes of a SolidWorks-to-URDF delivery.

This module reads author inputs, raw CAD evidence and XML separately. It never
calls normalization or generation, and never uses a saved green report as an
oracle. Reports are deterministic so that copying, committing and rebuilding
can repeat exactly the same verification.
"""

from __future__ import annotations

import math
import re
from pathlib import Path
from xml.etree import ElementTree as ET

import numpy as np

from ..delivery import PIPELINE_ID, subject_digest
from ..geometry import stl
from ..io import PipelineError, confined, file_digest, inventory, read_data
from ..sources.snapshot import verify_snapshot
from ..sources.solidworks.input import inspect_package
from ..sources.solidworks.revision import package_inventory, read_revision
from .consumer import load as load_consumer

QUALITY_SCHEMA = "solidworks-to-urdf.quality/v1"
POSITION_TOL_M = 5e-5
ROTATION_TOL_RAD = math.radians(0.05)
_NAME = re.compile(r"^[a-z][a-z0-9_]*$")


def _require(passed, message):
    if not bool(passed):
        raise PipelineError(message)


def _vector(value, count=3):
    result = np.asarray(value, dtype=float)
    _require(result.shape == (count,) and np.isfinite(result).all(), f"Expected {count} finite values")
    return result


def _rotation(rpy):
    roll, pitch, yaw = _vector(rpy)
    cr, sr = math.cos(roll), math.sin(roll)
    cp, sp = math.cos(pitch), math.sin(pitch)
    cy, sy = math.cos(yaw), math.sin(yaw)
    return np.array(
        [
            [cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr],
            [sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr],
            [-sp, cp * sr, cp * cr],
        ]
    )


def _pose(xyz, rpy):
    matrix = np.eye(4)
    matrix[:3, :3], matrix[:3, 3] = _rotation(rpy), _vector(xyz)
    return matrix


def _cad_pose(values):
    # The native adapter converts MathTransform into the protocol's SI,
    # row-major homogeneous matrix before writing the captured reading.
    values = _vector(values, 16)
    matrix = values.reshape(4, 4)
    rotation = matrix[:3, :3]
    _require(np.allclose(rotation.T @ rotation, np.eye(3), atol=1e-8, rtol=0), "CAD basis is not orthonormal")
    _require(abs(float(np.linalg.det(rotation)) - 1) < 1e-8, "CAD basis must be right handed")
    _require(np.allclose(matrix[3], [0, 0, 0, 1], atol=1e-9, rtol=0), "Invalid homogeneous CAD matrix")
    return matrix


def _same_pose(actual, expected):
    position = float(np.linalg.norm(actual[:3, 3] - expected[:3, 3]))
    angle = math.acos(float(np.clip((np.trace(actual[:3, :3].T @ expected[:3, :3]) - 1) / 2, -1, 1)))
    _require(
        position <= POSITION_TOL_M and angle <= ROTATION_TOL_RAD,
        f"Frame differs from CAD by {position:.9g} m / {math.degrees(angle):.9g} deg",
    )


def _tensor(values):
    xx, xy, xz, yy, yz, zz = _vector(values, 6)
    return np.array([[xx, xy, xz], [xy, yy, yz], [xz, yz, zz]])


def _numbers(element, key, count):
    _require(element is not None and key in element.attrib, f"Missing XML {key}")
    return _vector([float(value) for value in element.attrib[key].split()], count)


def _one(parent, tag):
    rows = parent.findall(tag)
    _require(len(rows) == 1, f"Expected one <{tag}>, found {len(rows)}")
    return rows[0]


def _xml_pose(parent):
    origin = _one(parent, "origin")
    return _pose(_numbers(origin, "xyz", 3), _numbers(origin, "rpy", 3))


class _Gates:
    def __init__(self):
        self.checks = []

    def add(self, identifier, callback):
        try:
            details = callback() or {}
            self.checks.append({"id": identifier, "state": "passed", "passed": True, "details": details})
            return details
        except Exception as error:
            details = {"error": f"{type(error).__name__}: {error}"}
            if isinstance(getattr(error, "details", None), dict):
                details.update(error.details)
            self.checks.append({"id": identifier, "state": "failed", "passed": False, "details": details})
            return None


def required_checks(definition=None):
    """Required independent gates, including every declared body, joint and frame."""
    identifiers = {
        "bundle.subject", "input.valid", "source.native_discovery", "tool.identity",
        "source.integrity", "source.native", "model.schema", "source.raw",
        "physics.mass_closure_equality", "source.dependencies", "source.coverage",
        "verification.complete", "model.policy", "physics.independent", "frames.native",
        "frames.components", "frames.references", "urdf.syntax_names", "urdf.topology",
        "geometry.coverage", "geometry.assets", "geometry.expected_extent", "physics.expected_mass",
        "consumer.urdf",
    }
    source = (definition or {}).get("source") or {}
    for body in source.get("bodies", []):
        identifiers.update(("inertia." + body["name"], "geometry." + body["name"]))
    for joint in source.get("joints", []):
        identifiers.add("joints." + joint["name"])
        if joint["type"] != "fixed":
            identifiers.add("shafts." + joint["name"])
    identifiers.update("frames." + frame["name"] for frame in source.get("frames", []))
    return sorted(identifiers)


def require_qualified_report(report):
    """Reject an incomplete/forged green flag before it can become a boundary pass."""
    try:
        _require(isinstance(report, dict), "Quality report must be an object")
        rows = report.get("checks") or []
        expected = report.get("required_checks") or []
        _require(isinstance(rows, list) and isinstance(expected, list)
                 and all(isinstance(row, dict) and isinstance(row.get("id"), str) for row in rows)
                 and all(isinstance(name, str) for name in expected), "Quality gate inventory is malformed")
        identifiers = {row["id"] for row in rows}
        _require(report.get("passed") is True and bool(expected) and len(set(expected)) == len(expected)
                 and set(required_checks()) <= set(expected)
                 and set(expected) <= identifiers and len(identifiers) == len(rows)
                 and all(row.get("state") == "passed" and row.get("passed") is True for row in rows),
                 "URDF verification failed or required checks were not executed; see reports/quality.json")
    except PipelineError as error:
        error.details = report
        raise
    return report


def _input(root):
    report = inspect_package(root / "input")
    _require(report.get("passed") is True, str(report.get("errors")))
    definition = report["input"]
    revision = read_revision(root / "input", hardware_id=definition["hardware_id"])
    saved = read_data(confined(root, "reports/input.json"))
    _require(
        saved.get("passed") is True and saved.get("input") == definition,
        "Input report does not bind the author definition",
    )
    _require(saved.get("cad_revision") == revision, "Input report has another CAD revision")
    files = package_inventory(root / "input")
    _require(saved.get("package_files") == files, "Archived input differs from the captured package")
    return definition


def _native(root, manifest):
    _require(
        manifest["kind"] == "solidworks" and manifest["evidence_class"] == "cad",
        "A release requires native SolidWorks evidence",
    )
    collection = read_data(confined(root / "evidence", "evidence/collection.json"))
    _require(collection["identity"] == manifest["identity"], "Collection and snapshot identities differ")
    environment = read_data(confined(root / "evidence", "evidence/environment.json"))
    _require(environment == collection["environment"], "Environment evidence differs")
    revision = str(environment["solidworks"]["revision"])
    _require(revision.split(".")[0] == "34", "Native inertia API qualification currently covers SolidWorks 34 only")
    _require(collection["limits"]["native_cad"] is True, "Native capture was not recorded")
    return {"solidworks_revision": revision, "configuration": manifest["identity"]["configuration"]}


def _native_discovery(package):
    """Mandatory native-discovery gate: an authored or stripped package fails.

    Every current v1 delivery carries the generated provenance block and the
    bound raw discovery record; deleting either must fail before publication,
    so deleting either blocks publication.
    """

    from .native_discovery import verify_discovery

    report = verify_discovery(Path(package))
    if report.get("passed") is not True:
        failures = report.get("errors") or [{"code": "discovery.missing", "message": "no native discovery record"}]
        summary = "; ".join(f"{item.get('code')}: {item.get('message')}" for item in failures[:5])
        error = PipelineError(f"native discovery verification failed: {summary}")
        error.details = {
            "failures": [
                {"code": item.get("code"), "message": item.get("message"), "detail": item.get("detail")}
                for item in failures[:8]
            ]
        }
        raise error
    return {
        "contract": report.get("contract"),
        "discovery_sha256": report.get("discovery_sha256"),
        "bodies": report.get("bodies"),
        "joints": report.get("joints"),
    }


def _dependencies(root, definition):
    closure = read_data(confined(root / "evidence", "raw/dependency_closure.json"))
    _require(not closure["declared_unresolved"], "Unresolved CAD references")
    _require(closure["configuration"] == definition["source"]["configuration"], "Captured another configuration")
    original = closure["original_files"]
    _require(bool(original) and bool(closure["mapping"]), "Missing dependency mapping")
    actual = {"source/" + name: checksum for name, checksum in inventory(root / "evidence/source").items()}
    _require(actual == closure["copy_files"], "Collected CAD changed")
    original_hashes = set(read_revision(root / "input")["cad_files"].values())
    _require(set(original.values()) <= original_hashes, "Capture used CAD outside the mechanical handoff")
    top = [row for row in closure["mapping"] if row["instance"] == "top_level"]
    _require(len(top) == 1, "Missing unique top assembly mapping")
    expected = file_digest(confined(root / "input", definition["source"]["assembly"]))
    _require(top[0]["source_sha256"] == expected, "Captured another top assembly revision")
    for row in closure["mapping"]:
        _require(original.get(row["source"]) == row["source_sha256"], "Source mapping hash differs")
        _require(actual.get(row["copy"]) == row["copy_sha256"], "Copied mapping hash differs")
    collection = read_data(root / "evidence/evidence/collection.json")
    _require(collection["dependency_closure"] == closure, "Collection dependency record differs")
    _require(
        collection["capture"]["originals_unchanged"]["files_checked"] == len(original),
        "Source stability was not verified",
    )
    return {
        "native_documents": len(original),
        "collected_documents": len(actual),
        "suppressed_instances": closure["suppressed_instances"],
    }


def _entities(source, raw, model):
    components = [row["name"] for row in raw["components"]]
    authored = [name for body in source["bodies"] for name in body["components"]]
    _require(bool(components) and len(components) == len(set(components)), "Empty/duplicate native occurrences")
    _require(
        len(authored) == len(set(authored)) and set(authored) == set(components),
        "Every native occurrence must belong to exactly one body",
    )
    links = {row["name"]: row for row in model["links"]}
    _require(set(links) == {body["name"] for body in source["bodies"]}, "Canonical body set differs")
    for body in source["bodies"]:
        _require(links[body["name"]]["id"] == body["id"], "Body identity changed")
        _require(links[body["name"]]["provenance"]["source_entities"] == body["components"], "Body occurrences changed")
    return {"occurrences": len(components), "bodies": len(links)}


def _tool(root):
    from packaging.version import Version

    tool = read_data(confined(root, "reports/tool.json"))
    _require(
        tool["pipeline_id"] == PIPELINE_ID and tool["schema_version"] == "solidworks-to-urdf.tool/v1",
        "Unknown build tool",
    )
    _require(re.fullmatch(r"[0-9a-f]{64}", tool["source_sha256"]) is not None, "Missing tool code digest")
    runtime = tool["runtime"]
    _require(runtime["python"].startswith("3.12.") and bool(runtime["packages"]), "Missing pinned build runtime")
    for version in runtime["packages"].values():
        _require(isinstance(version, str), "Invalid dependency versions")
        Version(version)
    return {"version": tool["version"], "source_sha256": tool["source_sha256"]}


def _xml(root, definition, model):
    path = confined(root, "urdf/robot.urdf")
    payload = path.read_bytes()
    _require(
        b"<!DOCTYPE" not in payload.upper() and b"<!ENTITY" not in payload.upper(), "XML declarations are forbidden"
    )
    document = ET.fromstring(payload)
    _require(document.tag == "robot" and document.get("name") == definition["hardware_id"], "Wrong URDF robot identity")
    _require({child.tag for child in document} <= {"link", "joint"}, "Unexpected top-level URDF extension")
    links = document.findall("link")
    joints = document.findall("joint")
    link_names = [element.get("name", "") for element in links]
    joint_names = [element.get("name", "") for element in joints]
    _require(
        len(set(link_names)) == len(link_names) and len(set(joint_names)) == len(joint_names), "Duplicate URDF name"
    )
    _require(all(_NAME.fullmatch(name) for name in link_names + joint_names), "URDF names must be snake_case")
    expected_links = {link["name"] for link in model["links"]} | {frame["name"] for frame in model["frames"]}
    expected_joints = {joint["name"] for joint in model["joints"]} | {
        frame["name"] + "_fixed" for frame in model["frames"]
    }
    _require(
        set(link_names) == expected_links and set(joint_names) == expected_joints,
        "URDF entity set differs from canonical model",
    )
    return document


def _link_physics(node, canonical):
    inertial = _one(node, "inertial")
    mass = float(_numbers(_one(inertial, "mass"), "value", 1)[0])
    tensor_node = _one(inertial, "inertia")
    tensor = _tensor([float(tensor_node.attrib[key]) for key in ("ixx", "ixy", "ixz", "iyy", "iyz", "izz")])
    eigenvalues = np.linalg.eigvalsh(tensor)
    _require(
        mass > 0
        and eigenvalues[0] > 0
        and eigenvalues[-1] <= eigenvalues[0] + eigenvalues[1] + 1e-10 * eigenvalues[-1],
        "Nonphysical mass or COM inertia",
    )
    data = canonical["inertial"]
    _require(
        math.isclose(mass, data["mass"], rel_tol=1e-12, abs_tol=1e-15), "URDF mass differs from the raw-verified model"
    )
    actual_pose, expected_pose = _xml_pose(inertial), _pose(data["xyz"], data["rpy"])
    _same_pose(actual_pose, expected_pose)
    actual_tensor = actual_pose[:3, :3] @ tensor @ actual_pose[:3, :3].T
    expected_tensor = expected_pose[:3, :3] @ _tensor(data["inertia"]) @ expected_pose[:3, :3].T
    _require(np.allclose(actual_tensor, expected_tensor, rtol=1e-10, atol=1e-15), "URDF full inertia tensor differs")
    return {"mass_kg": mass, "principal_inertia_kg_m2": eigenvalues.tolist()}


def _joint(node, authored, canonical, datums, body_datums):
    _require(node.get("type") == authored["type"] == canonical["type"], "Joint type changed")
    for key in ("parent", "child"):
        _require(_one(node, key).get("link") == authored[key] == canonical[key], f"Joint {key} changed")
    _require(
        {child.tag for child in node} <= {"parent", "child", "origin", "axis", "limit"},
        "Unspecified joint extension",
    )
    expected = np.linalg.inv(body_datums[authored["parent"]]) @ body_datums[authored["child"]]
    _same_pose(_pose(canonical["xyz"], canonical["rpy"]), expected)
    _same_pose(_xml_pose(node), expected)
    if authored["type"] == "fixed":
        _require(node.find("axis") is None and node.find("limit") is None, "Fixed joint cannot carry axis or limits")
        return {"type": "fixed"}
    axis = _numbers(_one(node, "axis"), "xyz", 3)
    _require(abs(float(np.linalg.norm(axis)) - 1) < 1e-9, "Joint axis is not normalized")
    _require(
        np.allclose(axis, authored["axis"], atol=1e-12, rtol=0)
        and np.allclose(axis, canonical["axis"], atol=1e-12, rtol=0),
        "Signed joint axis changed",
    )
    limits = {key: float(value) for key, value in _one(node, "limit").attrib.items()}
    _require(limits == authored["limits"] == canonical["limits"], "Joint limits differ from author inputs")
    _require(
        all(math.isfinite(value) for value in limits.values()) and limits["effort"] > 0 and limits["velocity"] > 0,
        "Invalid joint limits",
    )
    if authored["type"] in {"revolute", "prismatic"}:
        _require(limits["lower"] < limits["upper"], "Unordered position limits")
    else:
        _require("lower" not in limits and "upper" not in limits, "Continuous joint has position limits")
    return {"type": authored["type"], "axis": axis.tolist(), "limits": limits}


def _axis(authored, record, components, child_datum):
    reference = authored["axis_reference"]
    _require(isinstance(reference, dict), "Moving joints require a structured native shaft reference")
    _require(record["component"] == reference["component"], "Shaft evidence names another occurrence")
    if reference.get("feature_name"):
        _require(
            record["selector"]["feature_name"] == reference["feature_name"], "Shaft evidence names another CAD feature"
        )
    else:
        _require(record["face_index"] == reference["face_index"], "Shaft evidence names another CAD face")
    _require(record.get("body_type", "solid") == reference.get("body_type", "solid"), "Shaft body type differs")
    _require(
        record["surface"] == "cylinder" and math.isfinite(float(record["radius_m"])) and float(record["radius_m"]) > 0,
        "No cylindrical shaft reading",
    )
    point, direction = _vector(record["axis_point_m"]), _vector(record["axis_direction"])
    _require(abs(float(np.linalg.norm(direction)) - 1) < 1e-8, "Native shaft direction is not a unit vector")
    if record["coordinate_frame"] in {"component", "component_local"}:
        transform = components[record["component"]]
        point = transform[:3, :3] @ point + transform[:3, 3]
        direction = transform[:3, :3] @ direction
    else:
        _require(record["coordinate_frame"] == "assembly", "Unknown shaft coordinate frame")
    axis = child_datum[:3, :3] @ _vector(authored["axis"])
    angle = math.acos(float(np.clip(abs(direction @ axis), -1, 1)))
    delta = point - child_datum[:3, 3]
    offset = float(np.linalg.norm(delta - float(delta @ axis) * axis))
    _require(
        offset <= POSITION_TOL_M and angle <= ROTATION_TOL_RAD,
        f"Joint origin/axis misses the native shaft by {offset:.9g} m / {math.degrees(angle):.9g} deg",
    )
    return {"offset_m": offset, "angle_deg": math.degrees(angle), "positive_direction": "author_axis_in_child_datum"}


def _tree(document, base_pose):
    links = {node.attrib["name"] for node in document.findall("link")}
    edges = {}
    incoming = set()
    for node in document.findall("joint"):
        parent, child = _one(node, "parent").attrib["link"], _one(node, "child").attrib["link"]
        _require(
            parent in links and child in links and child not in incoming and parent != child, "Invalid URDF tree edge"
        )
        incoming.add(child)
        edges.setdefault(parent, []).append((child, _xml_pose(node)))
    _require(links - incoming == {"base_link"}, "URDF must have one base_link root")
    poses = {"base_link": base_pose}
    stack = ["base_link"]
    while stack:
        parent = stack.pop()
        for child, relative in edges.get(parent, []):
            _require(child not in poses, "URDF tree cycle")
            poses[child] = poses[parent] @ relative
            stack.append(child)
    _require(set(poses) == links, "Disconnected URDF tree")
    return poses


def _geometry(root, body, node, model_link, geometry, components, body_pose, urdf_pose, exclusions):
    visuals = node.findall("visual")
    canonical = model_link["visuals"]
    _require(
        len(visuals) == len(canonical) == len(body["components"]), "Visual geometry does not cover every occurrence"
    )
    _require(not node.findall("collision") and not model_link["collisions"], "Unspecified collision conversion")
    all_points, used = [], []
    for occurrence, visual, shape in zip(body["components"], visuals, canonical, strict=True):
        row = geometry[occurrence]
        original = confined(root / "evidence", row["path"])
        _require(
            file_digest(original) == row["sha256"] and shape["filename"] == row["path"], "Source mesh identity differs"
        )
        mesh = _one(_one(visual, "geometry"), "mesh")
        uri = mesh.attrib["filename"]
        _require(
            uri.startswith("../meshes/") and "\\" not in uri and ":" not in uri,
            "Mesh URI must reference delivered meshes",
        )
        delivery_name = "meshes/" + uri[len("../meshes/") :]
        delivered = confined(root, delivery_name)
        _require(file_digest(delivered) == row["sha256"], "Delivered mesh bytes differ from captured geometry")
        used.append(delivery_name)
        _require(
            np.array_equal(_numbers(mesh, "scale", 3), [1, 1, 1]) and shape["scale"] == [1, 1, 1],
            "Meshes must use native SI units without rescaling",
        )
        native_pose = np.linalg.inv(body_pose) @ components[occurrence]
        _same_pose(_xml_pose(visual), native_pose)
        _same_pose(_pose(shape["xyz"], shape["rpy"]), native_pose)
        stats = stl.read(delivered)
        _require(stats.triangles > 0 and stats.area > 0, "Empty mesh")
        _require(stats.degenerate / stats.triangles <= 0.001, "More than 0.1% degenerate triangles")
        _require(
            row["triangles"] == stats.triangles and row["degenerate_triangles"] == stats.degenerate,
            "Geometry receipt differs from actual mesh",
        )
        _require(stats.volume > 1e-15 or occurrence in exclusions, "Zero-volume geometry needs a documented exclusion")
        points = stl.vertices(delivered)
        world = urdf_pose @ _xml_pose(visual)
        native = components[occurrence]
        # Compare placement using actual vertices, not bounds written by generation.
        actual = points @ world[:3, :3].T + world[:3, 3]
        expected = points @ native[:3, :3].T + native[:3, 3]
        _require(
            np.max(np.linalg.norm(actual - expected, axis=1)) <= POSITION_TOL_M,
            "Mesh vertices are displaced from native CAD",
        )
        all_points.append(points @ native_pose[:3, :3].T + native_pose[:3, 3])
    points = np.concatenate(all_points)
    com = _vector(model_link["inertial"]["xyz"])
    _require(
        np.all(com >= points.min(axis=0) - 1e-4) and np.all(com <= points.max(axis=0) + 1e-4),
        "COM lies outside geometry bounds",
    )
    radius_squared = float(np.max(np.sum((points - com) ** 2, axis=1)))
    inertia = model_link["inertial"]
    eigenvalues = np.linalg.eigvalsh(_tensor(inertia["inertia"]))
    _require(
        eigenvalues[-1] <= inertia["mass"] * radius_squared * 1.01 + 1e-15,
        "Inertia exceeds the geometry's physical radius bound",
    )
    world_points = points @ body_pose[:3, :3].T + body_pose[:3, 3]
    return used, world_points


def evaluate_bundle(root: Path) -> dict:
    """Recompute the quality report without reading the saved quality decision."""
    root = Path(root)
    gates = _Gates()
    subject = gates.add("bundle.subject", lambda: {"sha256": subject_digest(root)})
    definition = gates.add("input.valid", lambda: _input(root))
    gates.add("source.native_discovery", lambda: _native_discovery(root / "input"))
    gates.add("tool.identity", lambda: _tool(root))
    manifest = gates.add("source.integrity", lambda: verify_snapshot(root / "evidence"))
    if manifest is not None:
        gates.add("source.native", lambda: _native(root, manifest))
    model = gates.add("model.schema", lambda: _model(root))
    raw = gates.add("source.raw", lambda: read_data(confined(root / "evidence", "raw/scene_raw.json")))
    gates.add("physics.mass_closure_equality", lambda: _mass_closure_equality(root))
    if definition is not None:
        gates.add("source.dependencies", lambda: _dependencies(root, definition))
    if definition is not None and model is not None and raw is not None:
        source = definition["source"]
        gates.add("source.coverage", lambda: _entities(source, raw, model))
        gates.add("verification.complete", lambda: _verify_model(root, gates, definition, model, raw))
    required = required_checks(definition)
    observed = {check["id"] for check in gates.checks}
    for identifier in required:
        if identifier not in observed:
            gates.checks.append({"id": identifier, "state": "not_run", "passed": False,
                                 "details": {"reason": "A required prerequisite failed; this check was not executed"}})
    # Large internal inputs are read by later gates, not copied into the report.
    internal = {
        "input.valid",
        "source.integrity",
        "model.schema",
        "source.raw",
        "frames.native",
        "frames.components",
        "frames.references",
        "urdf.syntax_names",
    }
    for check in gates.checks:
        if check["passed"] and check["id"] in internal:
            check["details"] = {"validated": True}
        check["details"] = _portable_details(check["details"], root)
    return {
        "schema_version": QUALITY_SCHEMA,
        "pipeline_id": PIPELINE_ID,
        "subject_sha256": subject["sha256"] if subject else None,
        "subject_status": "bound" if subject else "unavailable",
        "passed": bool(gates.checks) and all(check["passed"] for check in gates.checks),
        "checks": gates.checks,
        "required_checks": required,
    }


def _portable_details(value, root):
    """Keep diagnostic text useful without binding it to the replay directory."""
    if isinstance(value, str):
        for prefix in {str(root.absolute()), str(root.resolve()), root.resolve().as_posix()}:
            value = value.replace(prefix, "<bundle>").replace(prefix.replace("/", "\\"), "<bundle>")
        return value
    if isinstance(value, dict):
        return {key: _portable_details(item, root) for key, item in value.items()}
    if isinstance(value, list):
        return [_portable_details(item, root) for item in value]
    return value


def _model(root):
    from ..model import Robot

    return Robot.from_dict(read_data(confined(root, "model/robot.json"))).to_dict()


def _mass_closure_equality(root):
    from .solidworks_physics import verify_urdf_mass_equality

    return verify_urdf_mass_equality(root)


def _verify_model(root, gates, definition, model, raw):
    from .solidworks_physics import verify_physics

    source = definition["source"]
    gates.add("model.policy", lambda: _policy(source, model))
    gates.add("physics.independent", lambda: _physics(root, source, model, verify_physics))
    datums = gates.add(
        "frames.native",
        lambda: {
            name: _cad_pose(value) for name, value in read_data(root / "evidence/raw/coordinate_systems.json").items()
        },
    )
    components = gates.add(
        "frames.components", lambda: {row["name"]: _cad_pose(row["transform"]) for row in raw["components"]}
    )
    if datums is None or components is None:
        return
    body_datums = gates.add(
        "frames.references",
        lambda: {body["name"]: datums[body["frame"]["coordinate_system"]] for body in source["bodies"]},
    )
    document = gates.add("urdf.syntax_names", lambda: _xml(root, definition, model))
    for identifier in ("frames.native", "frames.components", "frames.references", "urdf.syntax_names"):
        check = next(item for item in gates.checks if item["id"] == identifier)
        if check["passed"]:
            check["details"] = {"validated": True}
    if document is None or body_datums is None:
        return
    poses = gates.add("urdf.topology", lambda: _tree(document, body_datums["base_link"]))
    if poses is not None:
        gates.checks[-1]["details"] = {"links": len(poses), "root": "base_link"}
    links = {node.attrib["name"]: node for node in document.findall("link")}
    joints = {node.attrib["name"]: node for node in document.findall("joint")}
    model_links = {link["name"]: link for link in model["links"]}
    model_joints = {joint["name"]: joint for joint in model["joints"]}
    axes = (
        read_data(root / "evidence/raw/axis_references.json")
        if (root / "evidence/raw/axis_references.json").is_file()
        else []
    )
    axis_map = {row["joint"]: row for row in axes}
    for authored in source["joints"]:
        name = authored["name"]
        gates.add(
            "joints." + name,
            lambda a=authored: _joint(joints[a["name"]], a, model_joints[a["name"]], datums, body_datums),
        )
        if authored["type"] != "fixed":
            gates.add(
                "shafts." + name, lambda a=authored: _axis(a, axis_map[a["name"]], components, body_datums[a["child"]])
            )
    for frame in source.get("frames", []):
        gates.add("frames." + frame["name"], lambda f=frame: _frame(f, model, joints, links, datums, body_datums))
    geometry_rows = read_data(confined(root / "evidence", "raw/geometry.json"))
    geometry = {row["component"]: row for row in geometry_rows}
    gates.add("geometry.coverage", lambda: _geometry_coverage(geometry_rows, components))
    points, used = [], []
    exclusions = {row["component"] for row in definition["checks"].get("documented_exclusions", [])}
    for body in source["bodies"]:
        name = body["name"]
        gates.add("inertia." + name, lambda n=name: _link_physics(links[n], model_links[n]))
        if poses is not None:
            captured = []

            def check_geometry(b=body, captured=captured):
                filenames, world = _geometry(
                    root,
                    b,
                    links[b["name"]],
                    model_links[b["name"]],
                    geometry,
                    components,
                    body_datums[b["name"]],
                    poses[b["name"]],
                    exclusions,
                )
                captured.extend([filenames, world])
                return {"occurrences": len(filenames)}

            gates.add("geometry." + name, check_geometry)
            if captured:
                used.extend(captured[0])
                points.append(captured[1])
    gates.add("geometry.assets", lambda: _assets(root, used))
    gates.add("geometry.expected_extent", lambda: _extent(points, definition["checks"]["expected_extent_m"]))
    gates.add("physics.expected_mass", lambda: _mass(model, definition["checks"]["expected_mass_kg"]))
    gates.add("consumer.urdf", lambda: load_consumer(root))


def _physics(root, source, model, verifier):
    checks = verifier(root / "evidence", source, model)
    if not checks or any(check.get("passed") is not True for check in checks):
        error = PipelineError("Independent physics verification failed")
        error.details = {"checks": checks}
        raise error
    return {"checks": checks}


def _policy(source, model):
    _require(model["name"] == source["robot_name"], "Canonical robot name differs from the author definition")
    for key in ("actuators", "sensors", "constraints", "contact_excludes"):
        _require(not model[key], f"v1 does not support {key}")
    _require(not model.get("control") and not model.get("mechanical_drives"), "Unspecified canonical control extension")
    for key in ("joints", "frames"):
        actual = {(row["id"], row["name"]) for row in model[key]}
        expected = {(row["id"], row["name"]) for row in source.get(key, [])}
        _require(actual == expected, f"Canonical {key} set differs from author inputs")
    return {"scope": "SolidWorks tree, STL visuals, URDF"}


def _frame(frame, model, joints, links, datums, body_datums):
    name = frame["name"]
    _require(not list(links[name]), "Reference frame must be massless and have no geometry")
    node = joints[name + "_fixed"]
    _require(
        node.attrib["type"] == "fixed"
        and _one(node, "parent").attrib["link"] == frame["parent"]
        and _one(node, "child").attrib["link"] == name,
        "Reference frame joint changed",
    )
    expected = np.linalg.inv(body_datums[frame["parent"]]) @ datums[frame["coordinate_system"]]
    _same_pose(_xml_pose(node), expected)
    canonical = next(item for item in model["frames"] if item["name"] == name)
    _same_pose(_pose(canonical["xyz"], canonical["rpy"]), expected)
    return {"parent": frame["parent"]}


def _geometry_coverage(rows, components):
    names = [row["component"] for row in rows]
    _require(len(names) == len(set(names)) and set(names) == set(components), "Missing/duplicate native mesh")
    return {"meshes": len(names)}


def _assets(root, used):
    actual = {"meshes/" + name for name in inventory(root / "meshes")}
    _require(set(used) == actual and len(used) == len(actual), "Unused or multiply referenced delivery mesh")
    return {"meshes": len(actual)}


def _extent(points, bounds):
    _require(bool(points), "No verified geometry")
    points = np.concatenate(points)
    extent = points.max(axis=0) - points.min(axis=0)
    largest = float(extent.max())
    _require(bounds[0] <= largest <= bounds[1], f"Extent {largest:.9g} m is outside author bounds {bounds}")
    return {"extent_m": extent.tolist(), "expected_largest_m": bounds}


def _mass(model, bounds):
    mass = sum(link["inertial"]["mass"] for link in model["links"])
    _require(bounds[0] <= mass <= bounds[1], f"Mass {mass:.9g} kg is outside author bounds {bounds}")
    return {"mass_kg": mass, "expected_kg": bounds}


def check_bundle(root: Path) -> dict:
    """Recompute all gates and require an exact binding to the saved report."""
    report = evaluate_bundle(root)
    try:
        saved = read_data(confined(Path(root), "reports/quality.json"))
        _require(saved == report, "Saved quality report differs from recomputation")
    except (OSError, ValueError) as error:
        report["checks"].append({"id": "report.binding", "state": "failed", "passed": False,
                                 "details": {"error": str(error)}})
        report["passed"] = False
    return report
