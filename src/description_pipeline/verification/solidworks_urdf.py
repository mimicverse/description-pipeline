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
from collections import Counter
from pathlib import Path
from xml.etree import ElementTree as ET

import numpy as np

from ..delivery import PIPELINE_ID, read_delivery_manifest, read_source_record, subject_digest
from ..geometry import stl
from ..io import PipelineError, confined, digest, file_digest, inventory, read_data
from ..schemas import EFFECTIVE_INSTANCES_SCHEMA, TOOL_SCHEMA, exporter_declaration_present
from ..sources.solidworks.revision import NATIVE_SUFFIXES
from ..sources.snapshot import verify_snapshot
from ..interface_records import (
    CHECK_POLICY_V1,
    PURPOSE_INSPECTION_PREVIEW,
    QUALIFICATION_SCHEMA,
    qualification_facts,
    validate_qualification,
)
from ..stages import required_rows_complete
from . import native_discovery as evidence
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


def require_qualified_report(report, definition, root=None):
    """Reject an incomplete/forged green flag before it can become a boundary pass.

    The executed gates must be exactly the required set expanded from the
    independently rebuilt definition (frozen fixed gates plus the per-object
    families its rebuilt bodies/joints/frames and sealed expectations produce).
    A self-reported subset (for example fixed gates only) or a policy-shaped gate
    for an object that does not exist in the rebuilt inventory cannot pass: the
    required set is never taken from the report and never from author-side lists.

    The report's qualification items must carry exactly the facts the rebuilt
    definition expands, with their three-state conclusions and resolvable
    evidence references: with a ``root``, a missing record or digest mismatch
    refuses the ungrounded qualification instead of passing or downgrading it.
    """

    try:
        _require(isinstance(report, dict), "Quality report must be an object")
        _require(
            isinstance(definition, dict) and isinstance(definition.get("source"), dict),
            "Quality report must bind the rebuilt definition inventory",
        )
        rows = report.get("checks") or []
        expected = report.get("required_checks") or []
        _require(
            isinstance(rows, list)
            and isinstance(expected, list)
            and all(isinstance(row, dict) and isinstance(row.get("id"), str) for row in rows)
            and all(isinstance(name, str) for name in expected),
            "Quality gate inventory is malformed",
        )
        executed = [row["id"] for row in rows]
        inventory = set(required_checks(definition))
        _require(
            report.get("passed") is True
            and set(expected) == inventory
            and len(executed) == len(expected)
            and set(executed) == set(expected)
            and required_rows_complete(expected, rows),
            "URDF verification failed or required checks were not executed; see reports/quality.json",
        )
        qualifications = report.get("qualifications")
        facts = qualification_facts(definition)
        _require(
            isinstance(qualifications, list)
            and sorted(
                (item["fact_id"], item["scope"], item["object_id"]) for item in qualifications
            )
            == sorted((fact["fact_id"], fact["scope"], fact["object_id"]) for fact in facts),
            "Quality report does not carry exactly the rebuilt definition's qualification facts",
        )
        for item in qualifications:
            validate_qualification(item, resolve=root)
    except PipelineError as error:
        error.details = report
        raise
    return report


def _frozen_source_config(root) -> dict:
    """The sealed material authority, read from the canonical capture block.

    The ``material_provenance`` block of the sealed ``capture.json`` states the
    assembly, configuration and material source; it is the same authority the
    physics gate consumes.  The verifier reads exactly that block: an unknown
    schema, missing identity or an absent/unknown material source returns
    nothing so the reconstruction and the gate fail closed.  Nothing is
    inferred from mass dictionaries, no raw side copy is preferred over a
    diverging canonical block, and no generator-side rebuild is invoked.
    """

    delivery = Path(root).resolve()
    delivery = delivery.parent if delivery.name == "evidence" else delivery
    evidence = delivery / "evidence"
    if not (evidence / "capture.json").is_file():
        return {}
    capture = read_data(confined(evidence, "capture.json"))
    record = capture.get("material_provenance") if isinstance(capture, dict) else None
    if not isinstance(record, dict):
        return {}
    if record.get("schema_version") != "solidworks-to-urdf.material-provenance/v1":
        return {}
    if not isinstance(record.get("assembly"), str) or not record["assembly"].strip():
        return {}
    if not isinstance(record.get("configuration"), str) or not record["configuration"].strip():
        return {}
    source = record.get("material_source")
    if source not in ("cad", "documented_table"):
        return {}
    return {"material_source": source}


def rebuilt_definition(root) -> dict:
    """The independently rebuilt definition a quality report's inventory binds to."""

    return _input(root)


def _request_purpose(root) -> str:
    """The frozen request's declared purpose: the single purpose authority."""

    from ..request_evidence import read_request_evidence

    record, _digest = read_request_evidence(Path(root) / "input" / "request.json")
    return str(record["purpose"])


def _input(root, diagnostics=None):
    """Bind the frozen input and the author definition carried by the discovery record.

    The CAD/PDM revision label is optional raw metadata (a missing value stays
    missing, as a readiness diagnostic); engineering evidence binds to the
    discovery record, the frozen handoff record and the archived originals under
    ``input/cad``.
    """

    diagnostics = [] if diagnostics is None else diagnostics
    discovery_record = evidence.discovery(root)
    _require(isinstance(discovery_record.get("raw"), dict), "Discovery record carries no raw capture")
    raw = discovery_record["raw"]
    from .native_discovery import rebuild_interface_frames, rebuild_joint_facts, rebuild_rigid_bodies

    if exporter_declaration_present(raw):
        # The declaration domain carries its own independent decode: the consumer
        # expectations come from the sealed declaration, and the failure cause is never
        # swallowed into zero objects.
        from .native_declarations import verify_declarations

        declarations = verify_declarations(raw, purpose=_request_purpose(root))
        _require(
            declarations["passed"],
            f"the discovery record declaration gate failed: {declarations['errors'][:1]}",
        )
        carrier = declarations["carrier"]
        bodies, joints = carrier["bodies"], carrier["joints"]
        from ..sources.solidworks.naming import carrier_names

        body_names, joint_names = carrier_names(bodies, joints, str(carrier["root"]))
        for body in bodies:
            source_name = str(body["name"])
            body["source_name"] = source_name
            body["name"] = body_names[source_name]
            body["id"] = body["name"]
        for joint in joints:
            source_name = str(joint["name"])
            joint["source_name"] = source_name
            joint["parent"] = body_names.get(str(joint["parent"]), str(joint["parent"]))
            joint["child"] = body_names.get(str(joint["child"]), str(joint["child"]))
            joint["name"] = joint_names[source_name]
            joint["id"] = joint["name"]
        carrier["root"] = body_names.get(str(carrier.get("root")), carrier.get("root"))
        # The native interface interpretation is shared across the two modes: each mode
        # proves its own members/base first, then rebuilds the CS_/TCP_/SCS_ interfaces
        # from the raw datums (a missing declaration extension never proves absence).
        base = next((body for body in bodies if body["name"] == carrier.get("root")), None)
        frames = rebuild_interface_frames(raw, bodies=bodies, base=base, expansion=carrier.get("expansion"))
    else:
        bodies = rebuild_rigid_bodies(discovery_record)
        joints = rebuild_joint_facts(discovery_record, root, purpose=_request_purpose(root))
        frames = rebuild_interface_frames(discovery_record)
    source = read_source_record(root)
    handoff = evidence.handoff(root)
    identity = source["identity"]
    _require(
        handoff.get("main_assembly") == identity["main_assembly"],
        "Handoff and source record disagree on the main assembly",
    )
    _require(
        handoff.get("configuration") == identity["configuration"],
        "Handoff and source record disagree on the configuration",
    )
    raw_identity = (discovery_record.get("raw") or {}).get("identity") or {}
    robot_name = (
        raw_identity.get("robot_name")
        or discovery_record.get("robot_name")
        or discovery_record.get("robot_name_technical")
    )
    _require(isinstance(robot_name, str) and robot_name, "Discovery record carries no robot name")
    declared = evidence.declared_masses(root)
    material_source = _frozen_source_config(root).get("material_source")
    _require(
        isinstance(material_source, str) and material_source.strip() != "",
        "The frozen source config carries no usable material_source",
    )
    recorded = (handoff.get("input_receipt") or {}).get("inventory")
    if isinstance(recorded, dict) and recorded:
        archived = inventory(root / "input/cad")
        missing = [name for name, checksum in recorded.items() if archived.get(name) != checksum]
        _require(not missing, f"Archived input differs from the frozen handoff inventory: {missing[:5]}")
    revision = source["metadata"].get("revision")
    if not isinstance(revision, str) or not revision:
        from .native_discovery import publication_metadata_state

        authored = publication_metadata_state(
            discovery_record.get("raw") or {}, str(discovery_record.get("namespace") or "dp")
        )
        diagnostics.append(
            {
                "code": "input.cad_revision_absent",
                "blocking": False,
                "message": "the CAD revision label is absent; the raw value stays missing",
                "detail": {"hardware_id": identity["hardware_id"], "missing": list(authored.get("missing") or [])},
            }
        )
    checks: dict = {}
    budget_entry = discovery_record.get("budget")
    if isinstance(budget_entry, dict):
        from .native_discovery import _record_value

        value = _record_value(
            Path(root),
            {"package_file": budget_entry.get("package_file"), "sha256": budget_entry.get("sha256")},
            str(budget_entry.get("key") or ""),
        )
        _require(isinstance(value, dict), "Discovery record budget entry is not an object")
        checks = {key: value[key] for key in ("expected_mass_kg", "expected_extent_m") if key in value}
    return {
        "schema_version": discovery_record.get("schema_version"),
        "hardware_id": identity["hardware_id"],
        "source": {
            "assembly": identity["main_assembly"],
            "configuration": identity["configuration"],
            "robot_name": robot_name,
            "robot_name_source": "cad" if raw_identity.get("robot_name") else "cad_filename_derived",
            "bodies": bodies,
            "joints": joints,
            "frames": frames,
            "material_source": material_source,
            "documented_masses": declared.get("items") or {},
            "mass_evidence": declared.get("evidence"),
        },
        "checks": checks,
    }


def _native(root, manifest):
    _require(
        manifest["kind"] == "solidworks" and manifest["evidence_class"] == "cad",
        "A release requires native SolidWorks evidence",
    )
    collection = evidence.collection(root)
    _require(collection.get("identity") == manifest["identity"], "Collection and archived source identities differ")
    environment = evidence.runtime(root)
    _require(environment == collection.get("environment"), "Environment evidence differs")
    revision = str(environment["solidworks"]["revision"])
    _require(revision.split(".")[0] == "34", "Native inertia API qualification currently covers SolidWorks 34 only")
    _require((collection.get("limits") or {}).get("native_cad") is True, "Native capture was not recorded")
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
    closure = evidence.dependency_closure(root)
    _require(not closure["declared_unresolved"], "Unresolved CAD references")
    stale_skips = _stale_stored_reference_skips(root, closure)
    _require(closure["configuration"] == definition["source"]["configuration"], "Captured another configuration")
    original = closure["original_files"]
    _require(bool(original) and bool(closure["mapping"]), "Missing dependency mapping")
    actual = {"source/" + name: checksum for name, checksum in inventory(root / "evidence/source").items()}
    _require(actual == closure["copy_files"], "Collected CAD changed")
    # The frozen tree carries the dependency-closure copies under
    # ``evidence/source``; the discovery inventory binds the frozen originals
    # under ``input/cad``.
    _handoff_native_hashes(root)
    # Original coverage is a digest-mapping decision: the originals stay in the
    # platform's frozen upload storage, so their identity is the handoff upload
    # index (name -> original digest), never an equality with the - possibly
    # engine-rewritten - copy bytes.
    handoff = evidence.handoff(root)
    package_files = handoff.get("package_files")
    _require(isinstance(package_files, dict) and bool(package_files), "The frozen upload index is missing")
    upload_hashes = {str(checksum) for checksum in package_files.values()}
    top = [row for row in closure["mapping"] if row["instance"] == "top_level"]
    _require(len(top) == 1, "Missing unique top assembly mapping")
    main = str(definition["source"]["assembly"])
    normalized_main = ntpath.normcase(main.replace("/", "\\"))
    # The frozen intake keeps the selected CAD under ``input/cad``; the main
    # assembly's package-relative path in the upload index is therefore
    # ``cad/<main>``.  It must exist exactly; there is no suffix fallback that
    # could rewrite its path.  (The native absolute path's rooted
    # original->copy mapping is a separate layer.)
    archived_key = ntpath.normcase(("cad/" + main).replace("/", "\\"))
    exact = [
        name for name in package_files if ntpath.normcase(str(name).replace("/", "\\")) == archived_key
    ]
    _require(len(exact) == 1, "The frozen upload index does not name exactly one captured main assembly")
    main_key = exact[0]
    expected = str(package_files[main_key])
    _require(
        ntpath.normcase(str(top[0].get("source") or "")).endswith("\\" + normalized_main),
        "The top mapping does not name the requested main assembly",
    )
    _require(top[0]["source_sha256"] == expected, "Captured another top assembly revision")
    for row in closure["mapping"]:
        _require(original.get(row["source"]) == row["source_sha256"], "Source mapping hash differs")
        _require(
            str(row["source_sha256"]) in upload_hashes,
            "A captured original is outside the frozen upload index",
        )
        _require(actual.get(row["copy"]) == row["copy_sha256"], "Copied mapping hash differs")
    _require(
        {str(checksum) for checksum in original.values()} <= upload_hashes,
        "Capture used CAD outside the frozen upload index",
    )
    _require(
        {ntpath.normcase(str(row["source"])) for row in closure["mapping"]}
        == {ntpath.normcase(str(path)) for path in original},
        "The copied graph does not cover the captured original files",
    )
    record = evidence.discovery(root)
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
    # The frozen tree keeps the dependency closure and the source-stability
    # record once, as capture blocks (the old evidence collection file no longer
    # enters the delivery tree).  The closure block is re-read against the
    # record these checks consumed, and the stability record must cover the
    # captured input inventory.
    capture = evidence.capture(root)
    _require(capture.get("dependency_closure") == closure, "Capture dependency record differs")
    stability = (evidence.collection(root).get("capture") or {}).get("originals_unchanged")
    _require(
        isinstance(stability, dict) and stability.get("files_checked") == len(original),
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
    record = evidence.discovery(root)
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


def _handoff_native_hashes(root: Path) -> set[str]:
    """Hashes of the frozen handoff bytes, from the sealed discovery record.

    The discovery record carries its own provenance; its byte binding is the
    delivery manifest subject entry (re-checked by ``read_delivery_manifest``),
    and ``native_inventory_sha256`` is a top-level digest over the inventory.
    The raw record may only confirm a matching subset of that inventory.
    """

    payload = evidence.discovery(root)
    native = payload.get("files") if isinstance(payload, dict) else None
    _require(isinstance(native, list) and bool(native), "The archived native file inventory is missing")
    frozen = {str(row["path"]): str(row["sha256"]) for row in native}
    for relative, checksum in sorted(frozen.items()):
        path = confined(root / "input/cad", relative)
        _require(path.is_file(), f"A frozen native file is missing from the archived input: {relative}")
        _require(
            file_digest(path) == checksum,
            f"A frozen native file differs from its recorded bytes: {relative}",
        )
    _require(
        digest(native) == payload.get("native_inventory_sha256"),
        "The native inventory digest does not match the discovery record",
    )
    raw = payload.get("raw") if isinstance(payload.get("raw"), dict) else {}
    raw_files = {str(name): str(checksum) for name, checksum in (raw.get("files") or {}).items()}
    _require(
        raw_files.items() <= frozen.items(),
        "Raw-recorded documents are not a matching subset of the frozen native inventory: "
        f"{sorted(set(raw_files) - set(frozen))}",
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
    parser = ET.XMLParser(target=ET.TreeBuilder(insert_comments=True))
    document = ET.fromstring(payload, parser=parser)
    robot_name = (definition.get("source") or {}).get("robot_name")
    _require(isinstance(robot_name, str) and bool(robot_name), "The input definition carries no robot label")
    _require(document.tag == "robot" and document.get("name") == robot_name, "Wrong URDF robot identity")
    expected_comments = [
        f"WARNING {item['code']}: {item.get('message', '')} "
        "See reports/quality.json for object and field details."
        for item in (model.get("provenance") or {}).get("value_warnings") or []
        if isinstance(item, dict) and item.get("code")
    ]
    comments = [child for child in document if child.tag is ET.Comment]
    _require(
        [child.text for child in comments] == expected_comments,
        "URDF value-provenance warnings differ from the model",
    )
    _require(
        {child.tag for child in document if child.tag is not ET.Comment} <= {"link", "joint"},
        "Unexpected top-level URDF extension",
    )
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
    _require(all(math.isfinite(value) for value in limits.values()), "Invalid joint limits")
    _require(
        all(limits.get(key, 0.0) >= 0 for key in ("effort", "velocity")),
        "Invalid joint limits",
    )
    _require(
        canonical.get("limit_source") == authored.get("limit_source"),
        "Joint limit source changed between author inputs and the canonical model",
    )
    if authored["type"] in {"revolute", "prismatic"}:
        _require("lower" in limits and "upper" in limits, "Missing position limits")
        _require(limits["lower"] <= limits["upper"], "Unordered position limits")
    else:
        _require("lower" not in limits and "upper" not in limits, "Continuous joint has position limits")
    _require(
        authored.get("limit_source") in {"native", "declared_author"},
        "Moving joint limits are not bound to a CAD native or author declaration",
    )
    unqualified = [key for key in ("effort", "velocity") if key in limits and limits[key] == 0]
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
    point, direction = _axis_line(authored, record, components)
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


def _axis_line(authored, record, components):
    """The native shaft line of one moving joint in zero-pose world coordinates."""

    reference = authored["axis_reference"]
    if reference.get("kind") == "reference_axis":
        return _reference_axis_line(reference, record, components)
    return _cylinder_axis_line(reference, record, components)


def _unit_axis(value):
    """The unit direction of one delivered joint axis; an unusable axis refuses."""

    try:
        vector = np.asarray(value, dtype=float)
    except (TypeError, ValueError):
        vector = np.zeros(0)
    norm = float(np.linalg.norm(vector)) if vector.shape == (3,) else 0.0
    _require(np.isfinite(norm) and norm > 0, "A delivered joint axis has no usable direction")
    return (vector / norm).tolist()


def _joint_sample(joint, sign):
    """One pose value for a delivered moving joint, from its own evidence only.

    The offset inside the joint's ordered interval is the canonical policy's
    analytical sampling fraction of the quarter-interval point (the frozen
    policy fixes 1.0 today); a missing-drive joint samples identically - this
    is an analysis pose, not an engineering claim.
    """

    if joint["type"] == "continuous":
        return 0.3 if sign > 0 else -0.4
    limits = joint.get("limits") or {}
    lower, upper = limits.get("lower"), limits.get("upper")
    _require(
        isinstance(lower, (int, float))
        and isinstance(upper, (int, float))
        and math.isfinite(float(lower))
        and math.isfinite(float(upper))
        and float(lower) <= float(upper),
        f"Joint {joint['name']} has no usable ordered limits",
    )
    middle = (float(lower) + float(upper)) / 2
    quarter = (float(upper) - float(lower)) / 4
    return middle + sign * quarter


def _joint_combined_sample(joint, sign):
    """A deterministic in-range NON-ZERO pose for the combined sample.

    The value comes from the joint's own ordered-limits interval (never
    outside its authored limits): the quarter point, or - when that quarter
    point sits at zero - the half-quarter point on the same side, both
    strictly inside the range.  A zero-width range has no motion to claim: it
    contributes the zero pose when zero is admissible, and a degenerate range
    that excludes its zero refuses instead of fabricating a pose.
    """

    if joint["type"] == "continuous":
        return 0.3 if sign > 0 else -0.4
    limits = joint.get("limits") or {}
    lower, upper = limits.get("lower"), limits.get("upper")
    _require(
        isinstance(lower, (int, float))
        and isinstance(upper, (int, float))
        and math.isfinite(float(lower))
        and math.isfinite(float(upper))
        and float(lower) <= float(upper),
        f"Joint {joint['name']} has no usable ordered limits",
    )
    lower, upper = float(lower), float(upper)
    middle = (lower + upper) / 2
    quarter = (upper - lower) / 4
    if quarter == 0:
        _require(lower <= 0.0 <= upper, f"Joint {joint['name']} has a zero-width range that excludes its zero")
        return 0.0
    value = middle + sign * quarter
    if abs(value) <= 1e-12:
        value = middle + sign * quarter / 2
    return value


def _signed_axis_line(joint, record, components, body_datums):
    """The native line with the evidenced signed joint convention.

    A record that explicitly declares ``direction_semantics =
    undirected_axis_line`` fixes a line, not a direction: exchanging its two
    measured endpoints keeps the same geometric object.  For those records the
    signed positive-motion convention is the delivered authored axis carried
    into the SAME frame as the native direction through the evidenced native
    child datum (the shafts gate reports the same
    ``author_axis_in_child_datum`` convention); the endpoint order can never
    decide the physical sign.  A genuinely directed record keeps its own
    direction, and an unexplained sign mismatch under a directed source still
    diverges as before.
    """

    point, direction = _axis_line(joint, record, components)
    if record.get("direction_semantics") == "undirected_axis_line":
        child_datum = body_datums.get(joint["child"])
        _require(child_datum is not None, f"Joint {joint['name']} names no delivered child datum")
        authored_axis = child_datum[:3, :3] @ np.asarray(_unit_axis(joint.get("axis")), float)
        if float(np.dot(np.asarray(direction, float), authored_axis)) < 0:
            direction = -np.asarray(direction, float)
    return point, direction


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


def _motion(authored, joints, components, body_datums, bodies, record, native_axes):
    """The motion.{joint} gate: non-zero follow re-derived from the native shaft line.

    The motion owner re-derives, from the shaft evidence alone, where every
    driven occurrence must be under positive and negative poses inside the
    authored limits - over the WHOLE delivered chain: a further moving joint
    at its zero pose (or a fixed weld) still carries its descendants exactly
    as the delivered graph does.  Besides the per-joint samples, a combined
    sample poses ALL delivered moving joints at once - each value derived
    from that joint's own ordered-limits interval (a deterministic in-range
    NON-ZERO value; a zero-width range never claims motion) - and the
    delivered graph is forward-kinematicked from its own datums and signed
    axes (a revolute/continuous joint rotates about its axis, a prismatic
    joint translates along it without rotating).  A statically green delivery
    whose parent chain, axis binding or frame relations are wrong diverges as
    soon as a pose is non-zero.

    ``native_axes`` is REQUIRED and must carry every moving joint's native
    shaft record (the production call passes the full map): the expected side
    of every sample uses each moving joint's own native line, and there is no
    single-record fallback.  Reported errors separate the translation (m)
    from the rotation (rad); the acceptance threshold still applies to the
    established element-wise consistency metric.
    """

    from .motion import axis_rotation, evaluate_motion

    name, child = authored["name"], authored["child"]
    point, direction = _signed_axis_line(authored, record, components, body_datums)
    limits = authored["limits"]
    samples = (_joint_sample(authored, 1.0), _joint_sample(authored, -1.0))
    parents = {joint["child"]: joint for joint in joints}
    moving = [joint for joint in joints if joint["type"] != "fixed"]
    missing = [joint["name"] for joint in moving if not (native_axes or {}).get(joint["name"])]
    _require(
        isinstance(native_axes, dict) and not missing,
        "The motion bridge requires the complete native axis map"
        + (f"; missing {missing}" if missing else ""),
    )
    doc_bodies = [
        {
            "id": body["name"],
            "group": body["name"],
            "frame": body_datums[body["name"]].tolist(),
            "leaves": [
                {"id": component, "world": components[component].tolist()}
                for component in body["components"]
            ],
        }
        for body in bodies
    ]
    #: The expected side carries the full delivered chain: the sampled joint
    #: uses its native shaft line; every other delivered edge (a further
    #: moving joint at its zero pose, or a fixed weld) contributes no motion
    #: but still inherits and passes on its parent's transform, exactly like
    #: the delivered graph's forward kinematics below.
    doc_joints = []
    for joint in joints:
        if joint["type"] == "fixed":
            doc_joints.append(
                {
                    "id": joint["name"],
                    "type": "fixed",
                    "parent": joint["parent"],
                    "child": joint["child"],
                    "axis_point": [0.0, 0.0, 0.0],
                    "axis_dir": [0.0, 0.0, 1.0],
                    "stored_direction": [0.0, 0.0, 1.0],
                    "limits": {},
                    "drive": {},
                    "origin_parent_child": np.eye(4).ravel().tolist(),
                }
            )
            continue
        if joint["name"] == name:
            axis_point, axis_dir = point.tolist(), direction.tolist()
            joint_limits = limits
        else:
            other_point, other_direction = _signed_axis_line(
                joint, native_axes[joint["name"]], components, body_datums
            )
            axis_point, axis_dir = other_point.tolist(), other_direction.tolist()
            joint_limits = joint.get("limits") or {}
        doc_joints.append(
            {
                "id": joint["name"],
                "type": joint["type"],
                "parent": joint["parent"],
                "child": joint["child"],
                "axis_point": axis_point,
                "axis_dir": axis_dir,
                "stored_direction": axis_dir,
                "limits": dict(joint_limits),
                "drive": {
                    "effort": joint_limits.get("effort"),
                    "velocity": joint_limits.get("velocity"),
                },
                "origin_parent_child": np.eye(4).ravel().tolist(),
            }
        )
    interface = {
        "name": "IF-" + name,
        "on_parent": authored["parent"],
        "on_child": child,
        "axis_point": point.tolist(),
        "axis_dir": direction.tolist(),
    }

    checks = [({name: value}, f"{value:g}") for value in samples]
    if len(moving) > 1:
        for sign, label in ((1.0, "combined+"), (-1.0, "combined-")):
            checks.append(({joint["name"]: _joint_combined_sample(joint, sign) for joint in moving}, label))

    def model_transform(body_name, poses, memo):
        if body_name in memo:
            return memo[body_name]
        if body_name not in parents:
            value = body_datums[body_name]
        else:
            joint = parents[body_name]
            offset = np.linalg.inv(body_datums[joint["parent"]]) @ body_datums[body_name]
            if joint["name"] in poses:
                offset = offset.copy()
                unit = np.asarray(_unit_axis(joint.get("axis")), float)
                value_pose = float(poses[joint["name"]])
                if joint["type"] == "prismatic":
                    offset[:3, 3] = offset[:3, 3] + offset[:3, :3] @ (unit * value_pose)
                else:
                    offset[:3, :3] = offset[:3, :3] @ axis_rotation(unit, value_pose)
            value = model_transform(joint["parent"], poses, memo) @ offset
        memo[body_name] = value
        return value

    worst = 0.0
    worst_translation = 0.0
    worst_rotation = 0.0
    combined_worst_translation = 0.0
    combined_worst_rotation = 0.0
    for poses, label in checks:
        result = evaluate_motion(
            bodies=doc_bodies, joints=doc_joints, interface=interface, poses=poses
        )
        _require(
            result["interface"]["line_distance_m"] <= 1e-9 and result["interface"]["parallelism"] >= 1 - 1e-6,
            f"{name}: the re-derived interface loses continuity at {label}",
        )
        memo = {}
        for body in bodies:
            transform = model_transform(body["name"], poses, memo)
            for component in body["components"]:
                candidate = transform @ np.linalg.inv(body_datums[body["name"]]) @ components[component]
                expected = np.asarray(result["world"][component], float)
                worst = max(worst, float(np.max(np.abs(candidate - expected))))
                translation = float(np.linalg.norm(candidate[:3, 3] - expected[:3, 3]))
                rotation = candidate[:3, :3].T @ expected[:3, :3]
                angle = math.acos(float(np.clip((np.trace(rotation) - 1.0) / 2.0, -1.0, 1.0)))
                worst_translation = max(worst_translation, translation)
                worst_rotation = max(worst_rotation, angle)
                if label.startswith("combined"):
                    combined_worst_translation = max(combined_worst_translation, translation)
                    combined_worst_rotation = max(combined_worst_rotation, angle)
    _require(
        worst <= POSITION_TOL_M,
        f"{name}: the delivered graph diverges from the re-derived motion by {worst:.9g} "
        f"(matrix-element delta; worst translation {worst_translation:.9g} m, "
        f"worst rotation {worst_rotation:.9g} rad)",
    )
    return {
        "samples": list(samples),
        "worst_matrix_element_delta": worst,
        "worst_translation_m": worst_translation,
        "worst_rotation_rad": worst_rotation,
        "combined_worst_translation_m": combined_worst_translation,
        "combined_worst_rotation_rad": combined_worst_rotation,
    }


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
        record = evidence.effective_instances(root)
        mass_properties = evidence.mass_properties(root)
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
            base_name = shape.get("mesh_name") or f"meshes/visual/{node.attrib['name']}_{index}.stl"
            expected_name = (
                base_name.removesuffix(".stl") + f"_part_{part}.stl" if chunks > 1 else base_name
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
    gates.add("source.native_discovery", lambda: _native_discovery(root, diagnostics))
    gates.add("tool.identity", lambda: _tool(root, native_receipts))
    manifest = gates.add("source.integrity", lambda: read_delivery_manifest(root))
    if manifest is not None:
        gates.add("source.native", lambda: _native(root, verify_snapshot(root / "evidence")))
    model = gates.add("model.schema", lambda: _model(root))
    raw = gates.add("source.raw", lambda: evidence.scene(root))
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
    if model is not None:
        for warning in (model.get("provenance") or {}).get("value_warnings") or []:
            if isinstance(warning, dict) and warning.get("code"):
                diagnostics.append(
                    {
                        "code": str(warning["code"]),
                        "blocking": False,
                        "message": str(warning.get("message") or "non-native derived value"),
                        "detail": {"objects": warning.get("objects") or []},
                    }
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
        "qualifications": qualifications(root, definition, gates.checks) if isinstance(definition, dict) else [],
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


#: The sealed records each qualification fact's conclusion is read from
#: (relative to the delivery root): every fact binds the sealed input report and
#: the delivered object record it was decided over.
_QUALIFICATION_EVIDENCE = {
    "physics.expected_mass": ("input/handoff.json", "model/robot.json"),
    "geometry.expected_extent": ("input/handoff.json", "model/robot.json"),
    "inertia": ("input/handoff.json", "model/robot.json"),
    "geometry": ("input/handoff.json", "model/robot.json"),
    "joints": ("input/handoff.json", "urdf/robot.urdf"),
    "shafts": ("input/handoff.json", "urdf/robot.urdf"),
    "frames": ("input/handoff.json", "model/robot.json"),
}


def qualifications(root, definition, checks):
    """The qualification items of the quality report (IF-14).

    Two objects of one fact stay two identities; an absent optional budget is
    ``out_of_scope``; a fact whose gate did not establish its conclusion is
    ``unqualified``.  The engineering facts (B family) never take a gate's
    colour: their conclusion is read from authoritative evidence - an
    inspection-preview delivery states all three explicitly ``unqualified``.
    Evidence references bind the sealed records the conclusion is read from;
    when a required record is absent the item carries no evidence, and the
    check boundary refuses it as ungrounded rather than downgrading it.
    """

    rows = {check["id"]: check for check in checks}
    expectations = definition.get("checks") or {}
    purpose = _request_purpose(root)
    preview = purpose == PURPOSE_INSPECTION_PREVIEW
    joints = {joint["name"]: joint for joint in (definition.get("source") or {}).get("joints", [])}
    items = []
    for fact in qualification_facts(definition):
        fact_id = fact["fact_id"]
        if fact_id in _ENGINEERING_FACTS:
            status = _engineering_status(fact_id, fact["object_id"], joints, preview)
            evidence = []
            if all((root / record).is_file() for record in _ENGINEERING_FACT_RECORDS):
                evidence = [
                    {"record": record, "sha256": file_digest(root / record)} for record in _ENGINEERING_FACT_RECORDS
                ]
                evidence.extend(
                    {"record": extra, "sha256": file_digest(root / extra)}
                    for extra in _ENGINEERING_FACT_EXTRAS
                    if (root / extra).is_file()
                )
            items.append(
                {
                    "schema_version": QUALIFICATION_SCHEMA,
                    "fact_id": fact_id,
                    "scope": fact["scope"],
                    "object_id": fact["object_id"],
                    "status": status,
                    "evidence": evidence,
                }
            )
            continue
        budget = {
            "physics.expected_mass": "expected_mass_kg",
            "geometry.expected_extent": "expected_extent_m",
        }.get(fact_id)
        if budget is not None and budget not in expectations:
            status = "out_of_scope"
        else:
            row = rows.get(fact["check"])
            status = "established" if row is not None and row["passed"] else "unqualified"
        records = _QUALIFICATION_EVIDENCE.get(fact_id, ("input/handoff.json",))
        evidence = []
        if all((root / record).is_file() for record in records):
            evidence = [{"record": record, "sha256": file_digest(root / record)} for record in records]
        items.append(
            {
                "schema_version": QUALIFICATION_SCHEMA,
                "fact_id": fact_id,
                "scope": fact["scope"],
                "object_id": fact["object_id"],
                "status": status,
                "evidence": evidence,
            }
        )
    return items


#: The engineering qualification facts (IF-14, B family): authoritative-evidence
#: conclusions only - a passing model-consistency check never establishes them.
_ENGINEERING_FACTS = frozenset(
    (*CHECK_POLICY_V1["engineering_facts"]["per_joint"], *CHECK_POLICY_V1["engineering_facts"]["per_subject"])
)
_ENGINEERING_FACT_RECORDS = ("input/handoff.json",)
_ENGINEERING_FACT_EXTRAS = ("input/request.json", "model/robot.json")


def _engineering_status(fact_id: str, object_id: str, joints: dict, preview: bool) -> str:
    """The evidence-grounded state of one engineering fact.

    Inspection-preview deliveries state all three explicitly ``unqualified``;
    under the engineering purpose the per-joint range is established only by a
    native/controlled range source and drive sizing only by a present drive
    specification.  Machine qualification has no establishment channel here:
    a passing consistency check never grants it.
    """

    if preview or fact_id == "machine_qualification":
        return "unqualified"
    joint = joints.get(object_id) or {}
    if fact_id == "engineering_position_range":
        return "established" if joint.get("limit_source") in ("native", "declared_author") else "unqualified"
    limits = joint.get("limits") or {}
    return "established" if "effort" in limits and "velocity" in limits else "unqualified"


def _mass_closure_equality(root, definition=None):
    from .solidworks_physics import verify_urdf_mass_equality

    return verify_urdf_mass_equality(root, (definition or {}).get("source"))


def _verify_model(root, gates, definition, model, raw, diagnostics):
    from .solidworks_physics import verify_physics

    source = definition["source"]
    gates.add("model.policy", lambda: _policy(source, model))
    gates.add("provenance.value_warnings", lambda: _value_warning_gate(root, source, model))
    gates.add("physics.independent", lambda: _physics(root, source, model, verify_physics))
    datums = gates.add(
        "frames.native",
        lambda: {name: _cad_pose(value) for name, value in evidence.datums(root).items()},
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
    axes = evidence.axes(root)
    joint_sources = {
        str(joint.get("source_name") or joint["name"]): joint["name"] for joint in source["joints"]
    }
    axis_map = {
        joint_sources.get(str(row["joint"]), str(row["joint"])): row
        for row in axes
    }
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
            gates.add(
                "motion." + name,
                lambda a=authored: _motion(
                    a,
                    source["joints"],
                    components,
                    body_datums,
                    source["bodies"],
                    axis_map[a["name"]],
                    native_axes=axis_map,
                ),
            )
    for frame in source.get("frames", []):
        gates.add("frames." + frame["name"], lambda f=frame: _frame(f, model, joints, links, datums, body_datums))
    geometry_rows = evidence.geometry(root)
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
    for key in ("joints", "frames"):
        actual = {(row["id"], row["name"]) for row in model[key]}
        expected = {(row["id"], row["name"]) for row in source.get(key, [])}
        _require(actual == expected, f"Canonical {key} set differs from author inputs")
    return {"scope": "SolidWorks tree, STL visuals, URDF"}


def _value_warning_gate(root, source, model):
    """Re-derive every non-native value warning from source facts and the model."""

    from ..sources.solidworks.naming import material_name, mesh_path, visual_name

    warnings = (model.get("provenance") or {}).get("value_warnings")
    _require(isinstance(warnings, list), "Non-native value provenance warnings are missing")
    actual: dict[str, list[dict]] = {}
    actual_messages: dict[str, list[str]] = {}
    for item in warnings:
        _require(
            isinstance(item, dict)
            and isinstance(item.get("code"), str)
            and bool(item["code"])
            and isinstance(item.get("message"), str)
            and bool(item["message"])
            and isinstance(item.get("objects"), list)
            and bool(item["objects"]),
            "Non-native value provenance warning is malformed",
        )
        actual.setdefault(item["code"], []).extend(item["objects"])
        actual_messages.setdefault(item["code"], []).append(item["message"])

    bodies = source.get("bodies") or []
    joints = source.get("joints") or []
    links = model.get("links") or []
    model_joints = model.get("joints") or []
    shapes = [(link, shape) for link in links for shape in link.get("visuals") or []]

    expected: dict[str, list[dict]] = {}

    def add(code, value):
        expected.setdefault(code, []).append(value)

    for body in bodies:
        source_name = str(body.get("source_name") or body["name"])
        if source_name != body["name"]:
            add(
                "naming.formatted_interface",
                {
                    "object_type": "link",
                    "object": body["name"],
                    "field": "name",
                    "actual": body["name"],
                    "source": f"CAD datum formatting for {source_name}",
                    "original": source_name,
                    "rule_id": "naming.interface",
                    "rule_version": "naming-v1",
                    "reason": "CAD datum formatting",
                },
            )
    for joint in joints:
        source_name = str(joint.get("source_name") or joint["name"])
        if source_name != joint["name"]:
            add(
                "naming.formatted_interface",
                {
                    "object_type": "joint",
                    "object": joint["name"],
                    "field": "name",
                    "actual": joint["name"],
                    "source": f"CAD reference-axis formatting for {source_name}",
                    "original": source_name,
                    "rule_id": "naming.interface",
                    "rule_version": "naming-v1",
                    "reason": "CAD reference-axis formatting",
                },
            )
        if joint.get("type") != "fixed" and not any(
            word in {"roll", "pitch", "yaw"} for word in joint["name"].split("_")
        ):
            add(
                "naming.motion_unresolved",
                {
                    "object_type": "joint",
                    "object": joint["name"],
                    "field": "name",
                    "actual": joint["name"],
                    "source": "neutral joint naming",
                    "original": source_name,
                    "rule_id": "naming.joint_motion",
                    "rule_version": "naming-v1",
                    "reason": "motion classification unavailable",
                },
            )
    for frame in source.get("frames", []):
        source_name = str(frame.get("source_name") or "")
        if source_name and source_name != frame["name"]:
            add(
                "naming.formatted_interface",
                {
                    "object_type": "frame",
                    "object": frame["name"],
                    "field": "name",
                    "actual": frame["name"],
                    "source": f"CAD interface datum formatting for {frame['coordinate_system']}",
                    "original": source_name,
                    "rule_id": "naming.frame",
                    "rule_version": "naming-v1",
                    "reason": "CAD interface datum formatting",
                },
            )
    for link, shape in shapes:
        provenance = shape.get("provenance") if isinstance(shape.get("provenance"), dict) else {}
        native_component = str(provenance.get("native_component") or "")
        _require(native_component, "Visual carries no native component identity")
        _require(
            shape.get("name") == visual_name(native_component, link=link["name"]),
            "Visual name differs from the CAD-derived rule",
        )
        current_sha256 = file_digest(confined(root / "evidence", str(shape.get("filename") or "")))
        _require(shape.get("sha256") == current_sha256, "Visual digest differs from capture evidence")
        _require(
            shape.get("mesh_name")
            == mesh_path(current_sha256, semantic=visual_name(native_component, link=link["name"])),
            "Mesh path differs from content addressing",
        )
        _require(
            shape.get("material") == material_name(native_component, link=link["name"]),
            "Material name differs from the CAD-derived rule",
        )
        add(
            "naming.derived_visual",
            {
                "object_type": "visual",
                "object": shape["name"],
                "field": "name",
                "actual": shape["name"],
                "source": "native component identity formatting",
                "original": native_component,
                "rule_id": "naming.visual",
                "rule_version": "naming-v1",
                "reason": "native component identity formatting",
            },
        )
        add(
            "naming.content_addressed_mesh",
            {
                "object_type": "visual",
                "object": shape["name"],
                "field": "mesh filename",
                "actual": shape["mesh_name"],
                "source": "current capture content addressing",
                "original": current_sha256,
                "rule_id": "naming.mesh",
                "rule_version": "naming-v1",
                "reason": "current capture content addressing",
            },
        )
        add(
            "naming.derived_material",
            {
                "object_type": "visual",
                "object": shape["name"],
                "field": "material",
                "actual": shape["material"],
                "source": "native component identity formatting",
                "original": native_component,
                "rule_id": "naming.material",
                "rule_version": "naming-v1",
                "reason": "native component identity formatting",
            },
        )
    if source.get("robot_name_source") == "cad_filename_derived":
        source_name = str(source.get("assembly") or "").replace("\\", "/").rsplit("/", 1)[-1]
        add(
            "naming.formatted_robot_name",
            {
                "object_type": "robot",
                "object": source["robot_name"],
                "field": "name",
                "actual": source["robot_name"],
                "source": "native top assembly filename formatting",
                "original": source_name,
                "rule_id": "naming.robot",
                "rule_version": "naming-v1",
                "reason": "native top assembly filename formatting",
            },
        )
    for link in links:
        add(
            "geometry.derived_from_native",
            {
                "object_type": "link",
                "object": link["name"],
                "fields": ["visual.xyz", "visual.rpy", "visual.filename", "visual.scale"],
                "actual": "derived from native occurrence transforms and capture geometry",
                "source": "native_derived",
            },
        )
    for joint in model_joints:
        add(
            "geometry.derived_from_native",
            {
                "object_type": "joint",
                "object": joint["name"],
                "fields": ["xyz", "rpy", "axis"],
                "actual": "derived from native coordinate systems and axis declarations",
                "source": "native_derived",
            },
        )
    for frame in model.get("frames") or []:
        add(
            "geometry.derived_from_native",
            {
                "object_type": "frame",
                "object": frame["name"],
                "fields": ["xyz", "rpy"],
                "actual": "derived from native coordinate systems",
                "source": "native_derived",
            },
        )
    add(
        "units.si_conversion",
        {
            "object_type": "robot",
            "object": source["robot_name"],
            "field": "units",
            "actual": "SI",
            "source": "native_derived",
        },
    )
    for link in links:
        if link.get("inertial") is not None:
            add(
                "physics.derived_from_native",
                {
                    "object_type": "link",
                    "object": link["name"],
                    "fields": ["inertial.mass", "inertial.xyz", "inertial.rpy", "inertial.inertia"],
                    "actual": "transformed or recombined from native readings",
                    "source": "native_derived",
                },
            )
    for _link, shape in shapes:
        fields = ["scale"]
        actual_value = {"scale": shape.get("scale")}
        if shape.get("rgba"):
            fields.append("material.rgba")
            actual_value["rgba"] = shape["rgba"]
        add(
            "display.constant",
            {
                "object_type": "visual",
                "object": shape["name"],
                "fields": fields,
                "actual": actual_value,
                "source": "display constant",
            },
        )

    def canonical(rows):
        return Counter(json.dumps(row, sort_keys=True, separators=(",", ":")) for row in rows)

    messages = {
        "geometry.derived_from_native": (
            "Geometry, frame transforms and signed joint axes are derived from native evidence."
        ),
        "display.constant": "Display scale and material color are platform constants, not CAD readings.",
        "naming.formatted_interface": (
            "Link, joint, and frame names are deterministic formatting of native CAD identities."
        ),
        "naming.motion_unresolved": "Joint names without a CAD motion classification retain a neutral name.",
        "naming.derived_visual": "Visual names are derived from the exact native component occurrence identity.",
        "naming.content_addressed_mesh": "Mesh paths are derived from the current captured STL content digest.",
        "naming.derived_material": "Material names are derived from the owner link and native component identity.",
        "naming.formatted_robot_name": "The robot name is formatted from the native top-assembly filename.",
        "physics.derived_from_native": (
            "Link mass properties are transformed, translated or recombined from native readings."
        ),
        "units.si_conversion": "Delivered values use SI units after conversion from native CAD units.",
    }
    _require(set(actual) == set(expected), "Non-native value provenance warning classes differ from the delivery")
    _require(
        {code: Counter(values) for code, values in actual_messages.items()}
        == {code: Counter([messages[code]]) for code in expected},
        "Non-native value provenance warning messages differ from the delivery",
    )
    for code in sorted(expected):
        _require(
            canonical(actual[code]) == canonical(expected[code]),
            f"Non-native value provenance warning objects differ for {code}",
        )
    return {
        "codes": sorted(actual),
        "objects": sum(len(rows) for rows in actual.values()),
    }


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
    _require(set(used) == actual, "Unused or multiply referenced delivery mesh")
    return {"meshes": len(actual), "references": len(used)}


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
