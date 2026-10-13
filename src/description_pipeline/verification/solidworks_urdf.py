"""Independent gates for the bytes of a SolidWorks-to-URDF delivery.

This module reads author inputs, raw CAD evidence and XML separately. It never
calls normalization or generation, and never uses a saved green report as an
oracle. Reports are deterministic so that copying, committing and rebuilding
can repeat exactly the same verification.
"""

from __future__ import annotations

import hashlib
import json
import math
import ntpath
import re
import struct
from pathlib import Path
from xml.etree import ElementTree as ET

import numpy as np
import yaml

from ..delivery import PIPELINE_ID, subject_digest
from ..geometry import stl
from ..io import PipelineError, confined, file_digest, inventory, read_data
from ..schemas import EFFECTIVE_INSTANCES_SCHEMA, TOOL_SCHEMA
from ..sources.snapshot import verify_snapshot
from ..sources.solidworks.input import inspect_package
from ..sources.solidworks.revision import NATIVE_SUFFIXES
from ..sources.solidworks.revision import package_inventory, read_revision
from ..interface_records import CHECK_POLICY_V1
from ..stages import required_rows_complete
from .consumer import load as load_consumer

QUALITY_SCHEMA = "solidworks-to-urdf.quality/v1"
POSITION_TOL_M = 5e-5
ROTATION_TOL_RAD = math.radians(0.05)
#: Deterministic MuJoCo-oriented partition limit for one delivered mesh visual.
CHUNK_TRIANGLES_MAX = stl.MESH_PART_TRIANGLES_MAX
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
    """Required independent gates expanded from the canonical check policy.

    The expansion rules are data in ``interface_records.CHECK_POLICY_V1``; this
    evaluator applies them to the sealed definition so generation, verification
    and publication all expand the same single-source policy.
    """

    definition = definition or {}
    identifiers = set(CHECK_POLICY_V1["fixed_checks"])
    expectations = definition.get("checks") or {}
    source = definition.get("source") or {}
    for rule in CHECK_POLICY_V1["dynamic_expansions"]:
        selector, family = rule["selector"], rule["family"]
        if selector == "expectation:expected_mass_kg":
            if "expected_mass_kg" in expectations:
                identifiers.add(family)
        elif selector == "expectation:expected_extent_m":
            if "expected_extent_m" in expectations:
                identifiers.add(family)
        elif selector == "source.bodies[*].name":
            identifiers.update(family.format(body=body["name"]) for body in source.get("bodies", []))
        elif selector == "source.joints[*].name":
            for joint in source.get("joints", []):
                if rule.get("when") == "type != fixed" and joint["type"] == "fixed":
                    continue
                identifiers.add(family.format(joint=joint["name"]))
        elif selector == "source.frames[*].name":
            identifiers.update(family.format(frame=frame["name"]) for frame in source.get("frames", []))
        else:
            raise PipelineError(f"Unknown check-policy selector: {selector}")
    return sorted(identifiers)


def require_qualified_report(report):
    """Reject an incomplete/forged green flag before it can become a boundary pass."""
    try:
        _require(isinstance(report, dict), "Quality report must be an object")
        rows = report.get("checks") or []
        expected = report.get("required_checks") or []
        _require(
            isinstance(rows, list)
            and isinstance(expected, list)
            and all(isinstance(row, dict) and isinstance(row.get("id"), str) for row in rows)
            and all(isinstance(name, str) for name in expected),
            "Quality gate inventory is malformed",
        )
        _require(
            report.get("passed") is True
            and set(required_checks()) <= set(expected)
            and required_rows_complete(expected, rows),
            "URDF verification failed or required checks were not executed; see reports/quality.json",
        )
    except PipelineError as error:
        error.details = report
        raise
    return report


def _input(root, diagnostics=None):
    """Bind the archived input; an absent publication seal is a readiness warning, not a failure.

    A supplied cad-revision.json stays strictly validated (malformed or tampered metadata
    fails).  When the seal is absent the delivery is only acceptable while the authored
    administrative identity is incomplete; engineering evidence is bound to the archived
    native record and the captured package inventory instead.
    """

    from .native_discovery import REVISION_FILE

    diagnostics = [] if diagnostics is None else diagnostics
    report = inspect_package(root / "input")
    _require(report.get("passed") is True, str(report.get("errors")))
    definition = report["input"]
    saved = read_data(confined(root, "reports/input.json"))
    _require(
        saved.get("passed") is True and saved.get("input") == definition,
        "Input report does not bind the author definition",
    )
    seal = root / "input" / REVISION_FILE
    _require(not seal.is_symlink(), "The revision manifest cannot be a symlink")
    if seal.is_file():
        revision = read_revision(root / "input", hardware_id=definition.get("hardware_id"))
        _require(saved.get("cad_revision") == revision, "Input report has another CAD revision")
    else:
        from .native_discovery import DISCOVERY_FILE, publication_metadata_state

        payload = read_data(confined(root / "input", DISCOVERY_FILE))
        authored = publication_metadata_state(payload.get("raw") or {}, str(payload.get("namespace") or "dp"))
        _require(
            authored["missing"],
            "Missing generated cad-revision.json for complete authored publication metadata",
        )
        diagnostics.append(
            {
                "code": "input.cad_revision_not_sealed",
                "blocking": False,
                "message": (
                    "cad-revision.json is absent while authored publication metadata is incomplete; "
                    "engineering checks bind the archived native evidence instead"
                ),
                "detail": {"hardware_id": definition.get("hardware_id"), "missing": list(authored["missing"])},
            }
        )
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


def _native_discovery(package, diagnostics=None):
    """Mandatory native-discovery gate: an authored or stripped package fails.

    Every current v1 delivery carries the generated provenance block and the
    bound raw discovery record; deleting either must fail before publication,
    so deleting either blocks publication.
    """

    from .native_discovery import verify_discovery

    diagnostics = [] if diagnostics is None else diagnostics
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
    for item in report.get("diagnostics") or []:
        if isinstance(item, dict) and item.get("code"):
            diagnostics.append(
                {
                    "code": item.get("code"),
                    "blocking": False,
                    "message": str(item.get("message") or ""),
                    "detail": item.get("detail"),
                }
            )
    return {
        "contract": report.get("contract"),
        "discovery_sha256": report.get("discovery_sha256"),
        "bodies": report.get("bodies"),
        "joints": report.get("joints"),
    }


def _dependencies(root, definition):
    closure = read_data(confined(root / "evidence", "raw/dependency_closure.json"))
    _require(not closure["declared_unresolved"], "Unresolved CAD references")
    stale_skips = _stale_stored_reference_skips(root, closure)
    _require(closure["configuration"] == definition["source"]["configuration"], "Captured another configuration")
    original = closure["original_files"]
    _require(bool(original) and bool(closure["mapping"]), "Missing dependency mapping")
    actual = {"source/" + name: checksum for name, checksum in inventory(root / "evidence/source").items()}
    _require(actual == closure["copy_files"], "Collected CAD changed")
    original_hashes = _handoff_native_hashes(root / "input")
    _require(set(original.values()) <= original_hashes, "Capture used CAD outside the mechanical handoff")
    top = [row for row in closure["mapping"] if row["instance"] == "top_level"]
    _require(len(top) == 1, "Missing unique top assembly mapping")
    expected = file_digest(confined(root / "input", definition["source"]["assembly"]))
    _require(top[0]["source_sha256"] == expected, "Captured another top assembly revision")
    for row in closure["mapping"]:
        _require(original.get(row["source"]) == row["source_sha256"], "Source mapping hash differs")
        _require(actual.get(row["copy"]) == row["copy_sha256"], "Copied mapping hash differs")
    _require(
        {ntpath.normcase(str(row["source"])) for row in closure["mapping"]}
        == {ntpath.normcase(str(path)) for path in original},
        "The copied graph does not cover the captured original files",
    )
    from .native_discovery import DISCOVERY_FILE  # noqa: PLC0415 - mirrors _handoff_native_hashes

    record = read_data(confined(root / "input", DISCOVERY_FILE))
    selected = ((record.get("raw") or {}).get("source_selection") or {}).get("files") or {}
    for name, checksum in sorted(selected.items()):
        suffix = ntpath.normcase(str(name).replace("/", "\\"))
        matches = [
            row
            for row in closure["mapping"]
            if isinstance(row, dict)
            and ntpath.normcase(str(row.get("source") or "").replace("/", "\\")).endswith("\\" + suffix)
        ]
        _require(matches, f"A retained source is missing from the copied graph: {name}")
        _require(
            any(row.get("source_sha256") == checksum for row in matches),
            f"A retained source does not bind its selected original bytes: {name}",
        )
        if Path(str(name)).suffix.lower() not in NATIVE_SUFFIXES:
            _require(
                any(actual.get(row.get("copy")) == checksum for row in matches),
                f"A retained non-native source differs from its selected bytes in the copied tree: {name}",
            )
    if "seeded_required_leaves" not in closure:
        seeded: list = []
    else:
        seeded = closure["seeded_required_leaves"]
        _require(
            isinstance(seeded, list) and all(isinstance(item, str) and item for item in seeded),
            "Seeded required leaves are malformed",
        )
    mapping_sources = {ntpath.normcase(str(row["source"])) for row in closure["mapping"]}
    selected_suffixes = {ntpath.normcase(str(name).replace("/", "\\")) for name in selected}
    for path in seeded:
        _require(
            ntpath.normcase(path) in mapping_sources,
            f"A seeded required leaf is not covered by the copied graph: {path}",
        )
        tail = ntpath.normcase(path.replace("/", "\\"))
        _require(
            any(tail == suffix or tail.endswith("\\" + suffix) for suffix in selected_suffixes),
            f"A seeded required leaf is not a selected source: {path}",
        )
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
        "stale_stored_reference_skips": stale_skips,
    }


def _stale_stored_reference_skips(root: Path, closure: dict) -> int:
    """Capture skips of dangling stored references must be exact closure evidence.

    Every recorded skip must be covered by the sealed closure: a validated entry names
    a closure document and a verbatim stored location recorded for it; a replay entry
    from the post-copy reopening (document/location absent) must repeat a validated
    absolute path.  No entry may name a required captured source (original files or
    mapping sources, exact paths) or an actively resolved edge path, and a contained
    artifact must not be a selected source or a resolved target.  Fake coverage,
    missing coverage, selected targets and active resolutions refuse; no basename
    matching anywhere.
    """

    if "stale_stored_references" not in closure:
        return 0
    skips = closure["stale_stored_references"]
    _require(isinstance(skips, list), "Stored-reference skip record is malformed")
    from .native_discovery import DISCOVERY_FILE  # noqa: PLC0415 - mirrors _handoff_native_hashes

    record = read_data(confined(root / "input", DISCOVERY_FILE))
    manifest = (record.get("raw") or {}).get("source_selection") or {}
    sealed = manifest.get("closure") or {}
    files = manifest.get("files") or {}
    documents = sealed.get("documents") or {}
    folded_docs = {
        str(name).replace("\\", "/").casefold(): entry for name, entry in documents.items() if isinstance(entry, dict)
    }
    selected_names = {str(name).replace("\\", "/").casefold() for name in files}
    required_paths = {ntpath.normcase(str(path)) for path in (closure.get("original_files") or {})}
    source_paths = [str(path) for path in (closure.get("original_files") or {})]
    for row in closure.get("mapping") or []:
        if isinstance(row, dict) and isinstance(row.get("source"), str):
            required_paths.add(ntpath.normcase(row["source"]))
            source_paths.append(row["source"])
    edge_paths = set()
    for entry in folded_docs.values():
        for edge in entry.get("resolved_edges") or []:
            resolved_path = edge.get("resolved_path") if isinstance(edge, dict) else None
            if isinstance(resolved_path, str) and resolved_path:
                edge_paths.add(ntpath.normcase(resolved_path))

    def _same_path(left: object, right: object) -> bool:
        return (
            isinstance(left, str)
            and isinstance(right, str)
            and bool(left)
            and bool(right)
            and ntpath.normcase(left) == ntpath.normcase(right)
        )

    def _bind_location(document: str, location: str, resolved_path: str) -> None:
        """The resolved path must be the recorded location joined to its scanning document."""

        if ntpath.isabs(location):
            _require(
                ntpath.normcase(ntpath.normpath(location)) == ntpath.normcase(ntpath.normpath(resolved_path)),
                "Stored-reference skip does not bind its resolved path to the recorded location",
            )
            return
        fragment = ntpath.normcase(document.replace("/", "\\"))
        derived = [
            ntpath.normcase(ntpath.normpath(ntpath.join(ntpath.dirname(source), location)))
            for source in source_paths
            if fragment and ntpath.normcase(ntpath.normpath(source)).endswith("\\" + fragment)
        ]
        _require(
            ntpath.normcase(ntpath.normpath(resolved_path)) in derived,
            "Stored-reference skip does not bind its resolved path to the recorded location",
        )

    def _validated(document: object, location: object, resolved_path: object) -> bool:
        _require(
            isinstance(resolved_path, str) and ntpath.isabs(resolved_path),
            "Stored-reference skip entry is malformed",
        )
        if not (isinstance(document, str) and document and isinstance(location, str) and location):
            return False
        entry = folded_docs.get(document.replace("\\", "/").casefold())
        _require(entry is not None, "Stored-reference skip names a document outside the sealed closure")
        recorded = [
            str(reference["location"])
            for reference in entry.get("stored_references") or []
            if isinstance(reference, dict) and isinstance(reference.get("location"), str) and reference["location"]
        ]
        _require(
            location in recorded,
            "Stored-reference skip is not recorded by the sealed closure",
        )
        _bind_location(document, location, str(resolved_path))
        _require(
            ntpath.normcase(str(resolved_path)) not in required_paths
            and ntpath.normcase(str(location)) not in required_paths,
            "Stored-reference skip names a required captured source",
        )
        _require(
            ntpath.normcase(str(resolved_path)) not in edge_paths and ntpath.normcase(str(location)) not in edge_paths,
            "Stored-reference skip covers an actively resolved reference",
        )
        edges = [edge for edge in entry.get("resolved_edges") or [] if isinstance(edge, dict)]
        for edge in edges:
            _require(
                not _same_path(edge.get("resolved_path"), resolved_path)
                and not _same_path(edge.get("resolved_path"), location),
                "Stored-reference skip covers an actively resolved reference",
            )
        stage_root = sealed.get("stage_root")
        if isinstance(stage_root, str) and ntpath.isabs(stage_root):
            try:
                relative = ntpath.relpath(str(resolved_path), stage_root)
            except ValueError:
                relative = None
            if relative and not relative.startswith("..") and not ntpath.isabs(relative):
                artifact = relative.replace("\\", "/")
                _require(
                    artifact.casefold() not in selected_names,
                    "Stored-reference skip names a selected source",
                )
                _require(
                    all(str(edge.get("resolved") or "").casefold() != artifact.casefold() for edge in edges),
                    "Stored-reference skip covers an actively resolved reference",
                )
        return True

    validated: set[str] = set()
    replays: list[dict] = []
    for item in skips:
        _require(isinstance(item, dict), "Stored-reference skip entry is malformed")
        if "exists" in item:
            _require(type(item["exists"]) is bool, "Stored-reference skip entry is malformed")
        if _validated(item.get("document"), item.get("location"), item.get("resolved_path")):
            validated.add(ntpath.normcase(str(item["resolved_path"])))
        else:
            replays.append(item)
    for item in replays:
        path = ntpath.normcase(str(item["resolved_path"]))
        _require(path in validated, "Stored-reference replay is not covered by a validated skip")
        _require(path not in required_paths, "Stored-reference skip names a required captured source")
        _require(path not in edge_paths, "Stored-reference skip covers an actively resolved reference")
    return len(skips)


def _handoff_native_hashes(input_root: Path) -> set[str]:
    """Hashes of the frozen handoff bytes, strictly from the proven native inventory.

    Every named native file must still match its recorded bytes in the archived input,
    and the inventory digest and discovery-record digest must match robot.yaml
    provenance. The raw record and a supplied publication seal may only confirm a
    matching subset of that inventory; they never widen the allowed dependency sources.
    """

    from .native_discovery import DISCOVERY_FILE, REVISION_FILE

    record_path = input_root / DISCOVERY_FILE
    payload = read_data(confined(input_root, DISCOVERY_FILE))
    native = payload.get("native_files") if isinstance(payload, dict) else None
    _require(isinstance(native, dict) and bool(native), "The archived native file inventory is missing")
    frozen = {str(name): str(checksum) for name, checksum in native.items()}
    for relative, checksum in sorted(frozen.items()):
        path = confined(input_root, relative)
        _require(path.is_file(), f"A frozen native file is missing from the archived input: {relative}")
        _require(
            file_digest(path) == checksum,
            f"A frozen native file differs from its recorded bytes: {relative}",
        )
    robot = yaml.safe_load((input_root / "robot.yaml").read_text(encoding="utf-8"))
    provenance = robot.get("provenance") if isinstance(robot, dict) else None
    _require(isinstance(provenance, dict), "Archived robot.yaml carries no provenance")
    canonical = hashlib.sha256(
        (json.dumps(frozen, sort_keys=True, ensure_ascii=False, allow_nan=False, indent=2) + "\n").encode()
    ).hexdigest()
    _require(
        canonical == provenance.get("native_inventory_sha256"),
        "The native inventory digest does not match provenance",
    )
    _require(
        hashlib.sha256(record_path.read_bytes()).hexdigest() == provenance.get("discovery_sha256"),
        "The archived discovery record does not match provenance",
    )
    raw = payload.get("raw") if isinstance(payload.get("raw"), dict) else {}
    raw_files = {str(name): str(checksum) for name, checksum in (raw.get("files") or {}).items()}
    _require(
        raw_files.items() <= frozen.items(),
        "Raw-recorded documents are not a matching subset of the frozen native inventory: "
        f"{sorted(set(raw_files) - set(frozen))}",
    )
    seal = input_root / REVISION_FILE
    _require(not seal.is_symlink(), "The revision manifest cannot be a symlink")
    if seal.is_file():
        cad_files = {str(name): str(checksum) for name, checksum in read_revision(input_root)["cad_files"].items()}
        _require(
            cad_files.items() <= frozen.items(),
            "The revision seal is not a matching subset of the frozen native inventory: "
            f"{sorted(set(cad_files) - set(frozen))}",
        )
    return set(frozen.values())


def _entities(source, raw, model):
    components = [row["name"] for row in raw["components"]]
    authored = [name for body in source["bodies"] for name in body["components"]]
    _require(bool(components) and len(components) == len(set(components)), "Empty/duplicate native occurrences")
    _require(
        len(authored) == len(set(authored)) and set(authored) <= set(components),
        "Every selected occurrence must be one unique captured native component",
    )
    links = {row["name"]: row for row in model["links"]}
    _require(set(links) == {body["name"] for body in source["bodies"]}, "Canonical body set differs")
    for body in source["bodies"]:
        _require(links[body["name"]]["id"] == body["id"], "Body identity changed")
        _require(links[body["name"]]["provenance"]["source_entities"] == body["components"], "Body occurrences changed")
    provenance = model.get("provenance")
    _require(isinstance(provenance, dict), "The sealed scene carries no entity provenance")
    unassigned = sorted(set(components) - set(authored))
    _require(
        provenance.get("expected_entities") == sorted(authored)
        and provenance.get("native_entities") == sorted(components)
        and provenance.get("unassigned_entities") == unassigned,
        "Scene entity partitions differ from the selected bodies and the native capture",
    )
    return {
        "occurrences": len(components),
        "bodies": len(links),
        "selected": len(authored),
        "unassigned": len(unassigned),
    }


def _native_receipts_evidence(root, receipts):
    """Independently validate native receipts supplied as immutable bytes.

    The orchestration adapter resolves and transports the sealed receipt bytes;
    verification never imports the store or trusts a stored status.  Identity,
    schema, byte digests and release binding are re-checked here, and the parsed
    bytes are the evidence (never a re-serialized copy).
    """

    import hashlib

    names = ("native_tool_bytes", "native_stages_bytes", "transfer_manifest_bytes")
    _require(isinstance(receipts, dict), "Native receipts must be supplied as an object")
    blobs = {}
    for name in names:
        blob = receipts.get(name)
        _require(
            isinstance(blob, (bytes, bytearray)) and bool(blob),
            f"Native receipt {name} is missing",
        )
        blobs[name] = bytes(blob)
    parsed = {}
    for name, blob in blobs.items():
        try:
            parsed[name] = json.loads(blob.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as error:
            raise PipelineError(f"Native receipt {name} is not sealed JSON") from error
    native_tool = parsed["native_tool_bytes"]
    _require(
        isinstance(native_tool, dict) and native_tool.get("schema_version") == TOOL_SCHEMA,
        "Native tool receipt schema changed",
    )
    runtime = native_tool.get("runtime") or {}
    _require(runtime.get("role") == "native", "Native tool receipt carries another role")
    delivery = read_data(confined(root, "tool.json"))
    _require(
        native_tool.get("version") == delivery.get("version")
        and native_tool.get("source_sha256") == delivery.get("source_sha256"),
        "Native receipt does not bind this delivery's release identity",
    )
    stages = parsed["native_stages_bytes"]
    transfer = parsed["transfer_manifest_bytes"]
    _require(isinstance(stages, dict) and isinstance(transfer, dict), "Native receipts are not sealed records")
    return {
        "digests": {name: hashlib.sha256(blob).hexdigest() for name, blob in blobs.items()},
        "native_tool": {"version": native_tool.get("version"), "source_sha256": native_tool.get("source_sha256")},
        "native_stages": stages,
        "transfer_manifest": transfer,
    }


def _tool(root, native_receipts=None):
    from packaging.version import Version

    tool = read_data(confined(root, "tool.json"))
    _require(
        tool["pipeline_id"] == PIPELINE_ID and tool["schema_version"] == TOOL_SCHEMA,
        "Unknown build tool",
    )
    _require(re.fullmatch(r"[0-9a-f]{64}", tool["source_sha256"]) is not None, "Missing tool code digest")
    runtime = tool["runtime"]
    _require(runtime["python"].startswith("3.12.") and bool(runtime["packages"]), "Missing pinned build runtime")
    for version in runtime["packages"].values():
        _require(isinstance(version, str), "Invalid dependency versions")
        Version(version)
    result = {"version": tool["version"], "source_sha256": tool["source_sha256"]}
    if native_receipts is not None:
        result["transfer"] = _native_receipts_evidence(root, native_receipts)
    return result


def _xml(root, definition, model):
    path = confined(root, "urdf/robot.urdf")
    payload = path.read_bytes()
    _require(
        b"<!DOCTYPE" not in payload.upper() and b"<!ENTITY" not in payload.upper(), "XML declarations are forbidden"
    )
    document = ET.fromstring(payload)
    robot_name = (definition.get("source") or {}).get("robot_name")
    _require(isinstance(robot_name, str) and bool(robot_name), "The input definition carries no robot label")
    _require(document.tag == "robot" and document.get("name") == robot_name, "Wrong URDF robot identity")
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


def _joint(node, authored, canonical, datums, body_datums, diagnostics):
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
        all(math.isfinite(value) for value in limits.values()) and limits["effort"] >= 0 and limits["velocity"] >= 0,
        "Invalid joint limits",
    )
    if authored["type"] in {"revolute", "prismatic"}:
        _require(limits["lower"] <= limits["upper"], "Unordered position limits")
    else:
        _require("lower" not in limits and "upper" not in limits, "Continuous joint has position limits")
    unqualified = [key for key in ("effort", "velocity") if limits[key] == 0]
    if authored["type"] in {"revolute", "prismatic"} and limits["lower"] == limits["upper"]:
        unqualified.append("degenerate_position_range")
    if unqualified:
        diagnostics.append(
            {
                "code": "urdf.motion_unqualified",
                "blocking": False,
                "message": (
                    "authored motion/drive values are zero or degenerate; consumers import the zero-range "
                    "joint as unlimited while effort/velocity are dropped, so motion is unqualified and not "
                    "robot-ready"
                ),
                "detail": {"joint": authored["name"], "values": unqualified, "limits": limits},
            }
        )
    return {"type": authored["type"], "axis": axis.tolist(), "limits": limits}


def _axis(authored, record, components, child_datum):
    reference = authored["axis_reference"]
    _require(isinstance(reference, dict), "Moving joints require a structured native shaft reference")
    _require(record["component"] == reference["component"], "Shaft evidence names another occurrence")
    if reference.get("kind") == "reference_axis":
        point, direction = _reference_axis_line(reference, record, components)
    else:
        point, direction = _cylinder_axis_line(reference, record, components)
    authored_axis = _vector(authored["axis"])
    _require(abs(float(np.linalg.norm(authored_axis)) - 1) < 1e-8, "Authored joint axis is not a unit vector")
    axis = child_datum[:3, :3] @ authored_axis
    angle = math.acos(float(np.clip(abs(direction @ axis), -1, 1)))
    delta = point - child_datum[:3, 3]
    offset = float(np.linalg.norm(delta - float(delta @ axis) * axis))
    _require(
        offset <= POSITION_TOL_M and angle <= ROTATION_TOL_RAD,
        f"Joint origin/axis misses the native shaft by {offset:.9g} m / {math.degrees(angle):.9g} deg",
    )
    return {"offset_m": offset, "angle_deg": math.degrees(angle), "positive_direction": "author_axis_in_child_datum"}


def _reference_axis_line(reference, record, components):
    """Re-derive the native undirected RefAxis from its two measured endpoints."""
    _require(
        record.get("kind") == "reference_axis"
        and record.get("selector") == reference
        and record.get("direction_semantics") == "undirected_axis_line"
        and record.get("used_api") == "IFeature.GetSpecificFeature2/IRefAxis.GetRefAxisParams",
        "No matching native reference-axis reading",
    )
    component = reference.get("component")
    _require(isinstance(component, str) and (component == "" or component in components), "Unknown axis occurrence")
    _require(
        isinstance(reference.get("feature_name"), str) and bool(reference["feature_name"]), "Unnamed reference axis"
    )
    endpoints = _vector(record["raw_endpoints_m"], 6)
    point, endpoint = endpoints[:3], endpoints[3:]
    delta = endpoint - point
    length = float(np.linalg.norm(delta))
    _require(math.isfinite(length) and length > 0, "Native reference-axis endpoints coincide")
    direction = delta / length
    _require(
        np.allclose(_vector(record["axis_point_m"]), point, atol=1e-12, rtol=0)
        and np.allclose(_vector(record["axis_direction"]), direction, atol=1e-12, rtol=0),
        "Reference-axis summary differs from native endpoints",
    )
    if record.get("coordinate_frame") == "component_local":
        _require(component != "", "A top-assembly axis cannot claim a component-local frame")
        transform = components[component]
        point = transform[:3, :3] @ point + transform[:3, 3]
        direction = transform[:3, :3] @ direction
    else:
        _require(record.get("coordinate_frame") == "assembly", "Unknown reference-axis coordinate frame")
    return point, direction


def _cylinder_axis_line(reference, record, components):
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
    return point, direction


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


def _qualified_surface_only(root, occurrence: str) -> bool:
    """Whether the sealed evidence carries this occurrence's qualified surface-only CAD record.

    Sheet-only components (no solid bodies) legitimately tessellate to zero volume.
    Acceptance requires the canonical sealed record: a leaf effective-instance
    reading for this exact occurrence, its surface-only warning with the exact
    qualification string, and the raw mass row in effective-instance mode with a
    state and reference counts of zero solid bodies and at least one sheet body.
    Integer identity checks reject booleans, so ``False`` never satisfies a zero
    count; nothing is inferred from mass.
    """

    try:
        record = read_data(confined(root / "evidence", "raw/effective_instances.json"))
        mass_properties = read_data(confined(root / "evidence", "raw/mass_properties.json"))
    except (PipelineError, OSError, ValueError):
        return False
    if not isinstance(record, dict) or record.get("schema_version") != EFFECTIVE_INSTANCES_SCHEMA:
        return False
    readings = record.get("readings")
    if not isinstance(readings, dict):
        return False
    reading = readings.get(occurrence)
    if not isinstance(reading, dict) or reading.get("role") != "leaf":
        return False
    warnings = record.get("warnings")
    if not isinstance(warnings, list):
        return False
    qualification = "cad_authored_effective_instance; not geometry-derived, not independently physically qualified"
    qualified = False
    for item in warnings:
        if (
            isinstance(item, dict)
            and item.get("code") == "cad_surface_only_effective_instance"
            and item.get("component") == occurrence
            and item.get("blocking") is False
            and item.get("qualification") == qualification
            and item.get("part_document_read") == "unavailable"
            and type(item.get("solid_bodies")) is int
            and item["solid_bodies"] == 0
            and type(item.get("sheet_bodies")) is int
            and item["sheet_bodies"] > 0
        ):
            qualified = True
            break
    if not qualified:
        return False
    row = mass_properties.get(occurrence) if isinstance(mass_properties, dict) else None
    if not isinstance(row, dict) or row.get("mode") != "effective_instance":
        return False
    state = row.get("state")
    if not isinstance(state, str) or not state.strip():
        return False
    reference = row.get("reference")
    if not isinstance(reference, dict):
        return False
    solids = reference.get("solid_bodies")
    sheets = reference.get("sheet_bodies")
    return type(solids) is int and solids == 0 and type(sheets) is int and sheets > 0


def _mesh_chunk_count(triangles: int) -> int:
    """The deterministic delivered-chunk count of one canonical mesh visual."""

    _require(triangles > 0, "Empty mesh")
    return (triangles + CHUNK_TRIANGLES_MAX - 1) // CHUNK_TRIANGLES_MAX


def _check_mesh_part(data: bytes, source_bytes: bytes, start: int, count: int) -> None:
    """One delivered mesh part: copied header, its own count, contiguous source records."""

    _require(len(data) == 84 + 50 * count, "A mesh part does not match its declared triangle count")
    _require(data[:80] == source_bytes[:80], "A mesh part header was not copied from the captured source mesh")
    _require(struct.unpack("<I", data[80:84])[0] == count, "A mesh part header count differs from its records")
    _require(
        data[84:] == source_bytes[84 + 50 * start : 84 + 50 * (start + count)],
        "Mesh parts do not reproduce the captured source triangle records in order",
    )


def _geometry(root, body, node, model_link, geometry, components, body_pose, urdf_pose, exclusions):
    visuals = node.findall("visual")
    canonical = model_link["visuals"]
    _require(len(canonical) == len(body["components"]), "Canonical visuals differ from the selected occurrences")
    expected = 0
    for occurrence in body["components"]:
        row = geometry[occurrence]
        _require(
            type(row.get("triangles")) is int and row["triangles"] > 0,
            "Geometry receipt carries no triangle count",
        )
        expected += _mesh_chunk_count(row["triangles"])
    _require(len(visuals) == expected, "Visual geometry does not cover every occurrence")
    _require(not node.findall("collision") and not model_link["collisions"], "Unspecified collision conversion")
    all_points, used = [], []
    cursor = 0
    for index, (occurrence, shape) in enumerate(zip(body["components"], canonical, strict=True)):
        row = geometry[occurrence]
        original = confined(root / "evidence", row["path"])
        _require(
            file_digest(original) == row["sha256"] and shape["filename"] == row["path"], "Source mesh identity differs"
        )
        source_bytes = original.read_bytes()
        stats = stl.read_bytes(source_bytes)
        _require(
            stats.binary and stats.triangles == row["triangles"],
            "Geometry receipt differs from the captured source mesh",
        )
        _require(stats.area > 0, "Empty mesh")
        _require(
            stats.degenerate_area / stats.area <= 1e-6 and stats.degenerate / stats.triangles <= 1e-2,
            "degenerate triangles exceed the area or count bound "
            f"(area ratio {stats.degenerate_area / stats.area:.3e}, bound 1.0e-06; "
            f"count ratio {stats.degenerate / stats.triangles:.3e}, bound 1.0e-02)",
        )
        chunks = _mesh_chunk_count(stats.triangles)
        group = visuals[cursor : cursor + chunks]
        cursor += chunks
        materials, origins = set(), []
        for part, visual in enumerate(group):
            mesh = _one(_one(visual, "geometry"), "mesh")
            uri = mesh.attrib["filename"]
            _require(
                uri.startswith("../meshes/") and "\\" not in uri and ":" not in uri,
                "Mesh URI must reference delivered meshes",
            )
            delivery_name = "meshes/" + uri[len("../meshes/") :]
            expected_name = f"meshes/visual/{node.attrib['name']}_{index}" + (
                f"_part_{part}.stl" if chunks > 1 else ".stl"
            )
            _require(
                delivery_name == expected_name,
                "Delivered mesh naming differs from the canonical mesh contract",
            )
            delivered = confined(root, delivery_name)
            if chunks == 1:
                _require(file_digest(delivered) == row["sha256"], "Delivered mesh bytes differ from captured geometry")
            else:
                _check_mesh_part(
                    delivered.read_bytes(),
                    source_bytes,
                    CHUNK_TRIANGLES_MAX * part,
                    min(CHUNK_TRIANGLES_MAX, stats.triangles - CHUNK_TRIANGLES_MAX * part),
                )
            used.append(delivery_name)
            _require(
                np.array_equal(_numbers(mesh, "scale", 3), [1, 1, 1]) and shape["scale"] == [1, 1, 1],
                "Meshes must use native SI units without rescaling",
            )
            material = visual.find("material")
            materials.add(ET.tostring(material, encoding="unicode") if material is not None else "")
            origins.append(_xml_pose(visual))
        _require(len(materials) == 1, "Mesh parts carry different visual materials")
        native_pose = np.linalg.inv(body_pose) @ components[occurrence]
        for origin in origins:
            _same_pose(origin, native_pose)
        _same_pose(_pose(shape["xyz"], shape["rpy"]), native_pose)
        _require(
            stats.volume > 1e-15 or occurrence in exclusions or _qualified_surface_only(root, occurrence),
            "Zero-volume geometry needs a documented exclusion",
        )
        points = stl.vertices(original)
        world = urdf_pose @ origins[0]
        native = components[occurrence]
        # Compare placement using actual vertices, not bounds written by generation.
        actual = points @ world[:3, :3].T + world[:3, 3]
        desired = points @ native[:3, :3].T + native[:3, 3]
        _require(
            np.max(np.linalg.norm(actual - desired, axis=1)) <= POSITION_TOL_M,
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


def evaluate_bundle(root: Path, *, native_receipts=None) -> dict:
    """Recompute the quality report without reading the saved quality decision.

    ``native_receipts`` are the immutable receipt bytes resolved by the
    orchestration adapter; verification validates them independently and never
    imports the store or treats a stored status as evidence.
    """
    root = Path(root)
    diagnostics: list[dict] = []
    gates = _Gates()
    subject = gates.add("bundle.subject", lambda: {"sha256": subject_digest(root)})
    definition = gates.add("input.valid", lambda: _input(root, diagnostics))
    gates.add("source.native_discovery", lambda: _native_discovery(root / "input", diagnostics))
    gates.add("tool.identity", lambda: _tool(root, native_receipts))
    manifest = gates.add("source.integrity", lambda: verify_snapshot(root / "evidence"))
    if manifest is not None:
        gates.add("source.native", lambda: _native(root, manifest))
    model = gates.add("model.schema", lambda: _model(root))
    raw = gates.add("source.raw", lambda: read_data(confined(root / "evidence", "raw/scene_raw.json")))
    gates.add("physics.mass_closure_equality", lambda: _mass_closure_equality(root, definition))
    if definition is not None:
        gates.add("source.dependencies", lambda: _dependencies(root, definition))
    if definition is not None and model is not None and raw is not None:
        source = definition["source"]
        from .native_mesh_observations import verify_native_mesh_observations

        gates.add("geometry.native_tessellation", lambda: verify_native_mesh_observations(root))
        gates.add("source.coverage", lambda: _entities(source, raw, model))
        gates.add(
            "verification.complete",
            lambda: _verify_model(root, gates, definition, model, raw, diagnostics),
        )
    required = required_checks(definition)
    observed = {check["id"] for check in gates.checks}
    for identifier in required:
        if identifier not in observed:
            gates.checks.append(
                {
                    "id": identifier,
                    "state": "not_run",
                    "passed": False,
                    "details": {"reason": "A required prerequisite failed; this check was not executed"},
                }
            )
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
        "diagnostics": diagnostics,
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


def _mass_closure_equality(root, definition=None):
    from .solidworks_physics import verify_urdf_mass_equality

    return verify_urdf_mass_equality(root, (definition or {}).get("source"))


def _verify_model(root, gates, definition, model, raw, diagnostics):
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
            lambda a=authored: _joint(joints[a["name"]], a, model_joints[a["name"]], datums, body_datums, diagnostics),
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
    # ``checks`` is optional; omission means no documented exclusions.
    exclusions = {row["component"] for row in (definition.get("checks") or {}).get("documented_exclusions", [])}
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
    _author_expectations(gates, definition, model, points)
    gates.add("consumer.urdf", lambda: load_consumer(root))


def _author_expectations(gates, definition, model, points):
    """Execute only supplied design expectations; absence is not a passed check."""
    expectations = definition.get("checks") or {}
    if "expected_extent_m" in expectations:
        gates.add("geometry.expected_extent", lambda: _extent(points, expectations["expected_extent_m"]))
    if "expected_mass_kg" in expectations:
        gates.add("physics.expected_mass", lambda: _mass(model, expectations["expected_mass_kg"]))


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
    bounds = _expectation_bounds(bounds)
    _require(bool(points), "No verified geometry")
    points = np.concatenate(points)
    extent = points.max(axis=0) - points.min(axis=0)
    largest = float(extent.max())
    _require(bounds[0] <= largest <= bounds[1], f"Extent {largest:.9g} m is outside author bounds {bounds}")
    return {"extent_m": extent.tolist(), "expected_largest_m": bounds}


def _mass(model, bounds):
    bounds = _expectation_bounds(bounds)
    mass = sum(link["inertial"]["mass"] for link in model["links"])
    _require(bounds[0] <= mass <= bounds[1], f"Mass {mass:.9g} kg is outside author bounds {bounds}")
    return {"mass_kg": mass, "expected_kg": bounds}


def _expectation_bounds(bounds):
    _require(isinstance(bounds, (list, tuple)) and len(bounds) == 2, "Expected a two-number design range")
    _require(
        all(type(value) in (int, float) and math.isfinite(value) for value in bounds),
        "Design bounds must be finite numbers",
    )
    _require(0 <= bounds[0] < bounds[1], "Design bounds must be nonnegative and strictly ordered")
    return list(bounds)


def check_bundle(root: Path, *, native_receipts=None) -> dict:
    """Recompute all gates and require an exact binding to the saved report."""
    report = evaluate_bundle(root, native_receipts=native_receipts)
    try:
        saved = read_data(confined(Path(root), "reports/quality.json"))
        # The quality report is a JSON document: bind in the JSON data model so a
        # recomputed tuple cannot make every saved report differ from itself.
        _require(saved == json.loads(json.dumps(report)), "Saved quality report differs from recomputation")
    except (OSError, ValueError) as error:
        report["checks"].append(
            {"id": "report.binding", "state": "failed", "passed": False, "details": {"error": str(error)}}
        )
        report["passed"] = False
    return report
