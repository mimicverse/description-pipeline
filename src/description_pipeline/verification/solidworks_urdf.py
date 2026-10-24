"""Independent gates for the bytes of a SolidWorks-to-URDF delivery.

This module reads author inputs, raw CAD evidence and XML separately. It never
calls normalization or generation, and never uses a saved green report as an
oracle. Reports are deterministic so that copying, committing and rebuilding
can repeat exactly the same verification.
"""

from __future__ import annotations

import json
import math
import ntpath
import posixpath
import re
import struct
from collections import Counter
from pathlib import Path
from xml.etree import ElementTree as ET

import numpy as np

from ..native_transfer import load_native_receipts
from ..delivery import PIPELINE_ID, read_delivery_manifest, read_source_record, subject_digest
from ..geometry import stl
from ..geometry.native_u import (
    NATIVE_SCOPE_IDENTITY_WARNING_CODES,
    native_scope_warning_details_match,
    native_scope_warning_projection,
)
from ..io import PipelineError, confined, digest, file_digest, inventory, read_data
from ..schemas import EFFECTIVE_INSTANCES_SCHEMA, TOOL_SCHEMA, exporter_declaration_present
from ..sources.solidworks.naming import fixed_joint_name
from ..sources.solidworks.revision import NATIVE_SUFFIXES
from ..sources.solidworks.native_limits import (
    ENGINEERING_DEFAULT_POLICY_ID,
    PREVIEW_POLICY_ID,
    PREVIEW_WINDOW_RAD,
)
from ..sources.solidworks.session_artifacts import (
    embedded_virtual_component_under_root,
    session_root_pid,
    session_materialization_under_root,
)
from ..sources.solidworks.evidence import (
    METADATA_WARNING_CODE,
    METADATA_WARNING_MESSAGE,
    REWRITE_ENVIRONMENT_EVIDENCE,
    RUNTIME_EVIDENCE,
    metadata_value_warnings,
)
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
        declaration_evidence = {
            "kind": "native_declaration",
            "file": "evidence/discovery.json",
            "sha256": file_digest(root / "evidence/discovery.json"),
            "anchor": raw["exporter"]["raw_xml_sha256"],
            "key": "raw.exporter.raw_xml",
            "reference": "evidence/discovery.json#raw.exporter.raw_xml",
        }
        for joint in joints:
            raw_limits = joint.get("declared_limits")
            raw_limits = raw_limits if isinstance(raw_limits, dict) else {}
            if joint.get("limit_source") == "declared_author" and joint.get("type") != "continuous":
                joint["limit_evidence"] = {
                    **declaration_evidence,
                    "declared_values": {
                        field: float(raw_limits[field])
                        for field in ("lower", "upper")
                    },
                    "native_observation_errors": [],
                }
            if all(
                type(raw_limits.get(field)) in (int, float) and math.isfinite(raw_limits[field])
                for field in ("effort", "velocity")
            ):
                joint["drive_evidence"] = {
                    **declaration_evidence,
                    "declared_values": {
                        field: float(raw_limits[field])
                        for field in ("effort", "velocity")
                    },
                }
                joint["drive_source"] = "controlled"
        from ..sources.solidworks.naming import carrier_names_and_rules

        carrier_snapshot = _carrier_snapshot(carrier["root"], bodies, joints, raw)
        body_names, joint_names, _body_rules, _joint_rules = carrier_names_and_rules(
            bodies,
            joints,
            str(carrier["root"]),
        )
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
        rebuild_root = next((str(body["name"]) for body in bodies if str(body["name"]) == "base_link"), None)
        carrier_snapshot = _carrier_snapshot(rebuild_root, bodies, joints, raw)
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
            "carrier": carrier_snapshot,
            "frames": frames,
        },
        "checks": checks,
    }


def _carrier_snapshot(root, bodies, joints, raw=None) -> dict:
    """Freeze the raw-decoded carrier names, members and native topology."""

    raw_rows = raw.get("reference_axes") if isinstance(raw, dict) else None
    raw_rows = raw_rows if isinstance(raw_rows, list) else []
    axes_by_selector: dict[tuple[str, str, str], dict] = {}
    axes_by_joint: dict[str, list[dict]] = {}
    for row in raw_rows:
        if not isinstance(row, dict):
            continue
        selector = row.get("selector") if isinstance(row.get("selector"), dict) else {}
        key = (
            str(selector.get("kind") or ""),
            str(selector.get("component") or ""),
            str(selector.get("feature_name") or ""),
        )
        if any(key):
            previous = axes_by_selector.get(key)
            _require(
                previous is None or previous.get("axis_direction") == row.get("axis_direction"),
                f"Native axis selector {key!r} carries conflicting directions",
            )
            axes_by_selector[key] = row
        joint_name = row.get("joint")
        if isinstance(joint_name, str) and joint_name:
            axes_by_joint.setdefault(joint_name, []).append(row)

    def assembly_axis(joint: dict) -> list[float] | None:
        selector = joint.get("axis_reference")
        selector_key = None
        if isinstance(selector, dict):
            selector_key = (
                str(selector.get("kind") or ""),
                str(selector.get("component") or ""),
                str(selector.get("feature_name") or ""),
            )
        matches: list[dict] = []
        if selector_key is not None:
            selected = axes_by_selector.get(selector_key)
            if selected is not None:
                matches.append(selected)
        matches.extend(axes_by_joint.get(str(joint.get("name") or ""), []))
        unique: dict[tuple[float, float, float], list[float]] = {}
        for row in matches:
            values = row.get("axis_direction")
            _require(
                isinstance(values, list) and len(values) == 3,
                "Native axis direction is malformed",
            )
            try:
                direction = np.asarray(values, dtype=float)
            except (TypeError, ValueError) as error:
                raise PipelineError("Native axis direction is malformed") from error
            _require(
                np.isfinite(direction).all() and float(np.linalg.norm(direction)) > 1e-12,
                "Native axis direction is invalid",
            )
            _require(
                row.get("coordinate_frame") == "assembly",
                "Functional naming requires an assembly-frame native axis",
            )
            _require(
                row.get("direction_semantics") == "undirected_axis_line",
                "Functional naming requires an undirected native axis line",
            )
            normalized = direction / float(np.linalg.norm(direction))
            unique[tuple(float(value) for value in np.round(normalized, 12))] = [
                float(value) for value in normalized
            ]
        _require(
            len(unique) <= 1,
            f"{joint.get('name')} carries conflicting native axis directions",
        )
        return next(iter(unique.values()), None)

    def axis_sign(joint: dict, properties: dict) -> int | None:
        name = str(joint.get("name") or "")
        direct_key = f"dp.joint.{name}.axis_sign"
        legacy_key = "dp.joint.axis_sign"
        candidates: list[object] = []

        def add(scope: object, key: str) -> None:
            if not isinstance(scope, dict) or key not in scope:
                return
            value = scope[key]
            if type(value) is int and value in (1, -1):
                candidates.append(value)
            elif type(value) is str and value in ("+1", "1", "-1"):
                candidates.append(-1 if value == "-1" else 1)
            else:
                raise PipelineError(f"{name} carries an invalid {key}: {value!r}")

        # The declaration form is a main-assembly property scoped by the joint
        # name.  Never scan unrelated components: a generic key on another part
        # does not describe this joint.
        add(properties.get("document") or {}, direct_key)
        mate_names: list[str] = []
        if isinstance(joint.get("mate"), str):
            mate_names.append(joint["mate"])
        if isinstance(joint.get("mates"), list):
            mate_names.extend(str(item) for item in joint["mates"] if isinstance(item, str))
        mate_properties = properties.get("mates") or {}
        for mate_name in sorted(set(mate_names)):
            scope = mate_properties.get(mate_name) or {}
            if isinstance(scope, dict):
                add(scope, legacy_key)
                add(scope, direct_key)
        signs = set(candidates)
        if len(signs) > 1:
            raise PipelineError(f"{name} carries conflicting axis signs: {sorted(signs)}")
        return signs.pop() if signs else None

    properties = raw.get("properties") or {} if isinstance(raw, dict) else {}
    joint_rows = [
        {
            "name": str(joint["name"]),
            "type": joint.get("type"),
            "parent": str(joint.get("parent") or ""),
            "child": str(joint.get("child") or ""),
            "axis_sign": axis_sign(joint, properties),
            "axis_assembly_direction": assembly_axis(joint),
        }
        for joint in joints
    ]
    by_name: dict[str, int] = {}
    for row in joint_rows:
        sign = row["axis_sign"]
        if sign is None:
            continue
        name = row["name"]
        if name in by_name and by_name[name] != sign:
            raise PipelineError(f"duplicate joint name {name} carries conflicting author signs")
        by_name[name] = sign
    return {
        "root": None if root is None else str(root),
        "bodies": [
            {
                "name": str(body["name"]),
                "frame": dict(body.get("frame") or {}),
                "components": list(body.get("components") or []),
            }
            for body in bodies
        ],
        "joints": joint_rows,
    }


def _native(root, manifest):
    _require(
        manifest["kind"] == "solidworks" and manifest["evidence_class"] == "cad",
        "A release requires native SolidWorks evidence",
    )
    collection = evidence.collection(root)
    _require(collection.get("identity") == manifest["identity"], "Collection and archived source identities differ")
    environment = evidence.runtime(root)
    _require(isinstance(environment, dict), "Capture environment is unavailable")
    _require(environment == collection.get("environment"), "Environment evidence differs")
    revision = str(environment["solidworks"]["revision"])
    _require(revision.split(".")[0] == "34", "Native inertia API qualification currently covers SolidWorks 34 only")
    _require((collection.get("limits") or {}).get("native_cad") is True, "Native capture was not recorded")
    preparation = _capture_preparation(root, manifest)
    return {
        "solidworks_revision": revision,
        "configuration": manifest["identity"]["configuration"],
        "capture_preparation": preparation,
    }


def _capture_preparation(root, manifest):
    """Independently compare capture rebuild state with native discovery."""

    schema = "solidworks-to-urdf.capture-preparation/v2"
    rebuild_api = (
        "IModelDocExtension.NeedsRebuild2/"
        "IConfiguration.NeedsRebuild/"
        "IModelDocExtension.Rebuild(swForceRebuildAll=2)"
    )
    no_rebuild_api = "IModelDocExtension.NeedsRebuild2/IConfiguration.NeedsRebuild"
    scene = evidence.scene(root)
    closure = evidence.dependency_closure(root)
    discovery = evidence.discovery(root)
    _require(isinstance(scene, dict), "Capture scene is unavailable")
    notes = scene.get("notes")
    _require(isinstance(notes, dict), "Capture scene carries no notes")
    preparation = notes.get("capture_preparation")
    _require(isinstance(preparation, dict), "Capture scene carries no rebuild preparation proof")
    required = {
        "schema_version",
        "used_api",
        "scope",
        "document",
        "configuration",
        "read_only",
        "saved_to_disk",
        "rebuild_performed",
        "component_count",
        "assembly_document_count",
        "child_rebuilds",
        "top_rebuild",
        "rebuild_calls",
        "session",
        "needs_rebuild2_before",
        "needs_rebuild2_after",
        "configuration_needs_rebuild_before",
        "configuration_needs_rebuild_after",
        "source_sha256_before",
        "source_sha256_after",
        "save_flag_before",
        "save_flag_after",
        "state_before",
        "state_after_children",
        "state_after",
        "assembly_states_before",
        "assembly_states_after",
        "part_states_after",
        "source_files_before",
        "source_files_after",
        "session_dependency_paths",
        "topology_before",
        "topology_after",
        "loaded_documents_before",
        "loaded_documents_after",
        "session_recovery",
    }
    _require(set(preparation) == required, "Capture preparation fields are incomplete or unknown")
    _require(preparation["schema_version"] == schema, "Capture preparation schema changed")
    session_recovery = preparation["session_recovery"]
    _require(
        session_recovery is None or isinstance(session_recovery, dict),
        "Capture session recovery is malformed",
    )
    environment = evidence.runtime(root)
    copy_identity = ((environment.get("solidworks") or {}).get("sessions") or {}).get("copy")
    _require(
        isinstance(copy_identity, dict)
        and type(copy_identity.get("pid")) is int
        and copy_identity["pid"] > 0
        and copy_identity.get("state") == "active",
        "Capture environment has no active owned copy process",
    )
    before_recovery_environment = None
    if session_recovery is not None:
        before_recovery_environment = read_data(
            confined(
                root,
                "evidence/capture-environment-before-recovery.json",
            )
        )
        _require(
            isinstance(before_recovery_environment, dict)
            and isinstance(
                before_recovery_environment.get("solidworks"),
                dict,
            ),
            "Capture pre-recovery environment is malformed",
        )

    def session_evidence(value, label):
        _require(
            isinstance(value, dict)
            and set(value) == {"role", "identity", "temp_root"}
            and value["role"] == "copy"
            and value["identity"] == copy_identity,
            f"{label} session differs from the captured owned process",
        )
        temp_root = value["temp_root"]
        _require(
            temp_root is None
            or (isinstance(temp_root, str) and session_root_pid(temp_root) == copy_identity["pid"]),
            f"{label} session root differs from the captured owned process",
        )
        return value

    prepared_session = session_evidence(preparation["session"], "Capture preparation")

    raw = discovery.get("raw")
    _require(isinstance(raw, dict), "Native discovery has no raw capture record")
    selection = discovery.get("source_selection")
    if not isinstance(selection, dict):
        selection = raw.get("source_selection")
    _require(isinstance(selection, dict), "Native discovery has no sealed source selection")
    selection_closure = selection.get("closure")
    _require(isinstance(selection_closure, dict), "Native discovery has no sealed dependency graph")
    documents = selection_closure.get("documents")
    main_assembly = selection.get("main_assembly")
    _require(isinstance(documents, dict) and bool(documents), "Native discovery closure has no documents")
    _require(isinstance(main_assembly, str) and bool(main_assembly), "Native discovery has no main assembly")
    raw_identity = raw.get("identity")
    _require(isinstance(raw_identity, dict), "Native discovery has no raw identity")
    _require(
        raw_identity.get("main_assembly") == main_assembly,
        "Native discovery identity and selection name different main assemblies",
    )

    def path_module(value):
        text = str(value or "")
        return ntpath if re.match(r"^[A-Za-z]:", text) or text.startswith("\\\\") or "\\" in text else posixpath

    def normalized_path(value):
        module = path_module(value)
        return module.abspath(str(value)).replace("/", "\\").strip().lower()

    def recorded_relative_path(value, root):
        module = path_module(value)
        try:
            relative = module.relpath(module.abspath(str(value)), module.abspath(str(root)))
        except (TypeError, ValueError) as error:
            raise PipelineError("A recorded path has no relative identity") from error
        parts = re.split(r"[\\/]", relative)
        _require(
            relative not in {"", "."}
            and not module.isabs(relative)
            and all(part not in {"", ".", ".."} for part in parts),
            "A recorded path escapes its capture root",
        )
        return "/".join(part.casefold() for part in parts)

    def document_key(value):
        text = str(value or "").replace("\\", "/")
        normalized = posixpath.normpath(text)
        _require(normalized not in {"", "."}, "A topology document identity is empty")
        return normalized.casefold()

    def session_path_suffix(value, marker):
        parts = [part.casefold() for part in re.split(r"[\\/]", str(value or "")) if part]
        positions = [index for index, part in enumerate(parts) if part == marker.casefold()]
        _require(len(positions) == 1, f"A session path lacks one {marker} identity")
        suffix = parts[positions[0] + 1 :]
        _require(bool(suffix), f"A session path has no path below {marker}")
        return "/".join(suffix)

    def session_path_identity(value):
        parts = [part.casefold() for part in re.split(r"[\\/]", str(value or "")) if part]
        markers = [part for part in parts if part in {"ic~~", "vc~~"}]
        _require(len(markers) == 1, "A session path lacks one session marker")
        marker = markers[0]
        return marker + "/" + session_path_suffix(value, marker)

    def copy_matches_document(copy_path, resolved):
        copy_parts = [item for item in document_key(copy_path).split("/") if item]
        document_parts = [item for item in document_key(resolved).split("/") if item]
        _require(bool(copy_parts) and bool(document_parts), "A topology copy or document identity is empty")
        return copy_parts == document_parts[-len(copy_parts) :] or document_parts == copy_parts[-len(document_parts) :]

    top_path = str(closure.get("top_level") or "")
    configuration = str(closure.get("configuration") or "")
    _require(bool(top_path) and bool(configuration), "Dependency closure has no capture identity")
    _require(
        isinstance(manifest.get("identity"), dict)
        and manifest["identity"].get("configuration") == configuration,
        "Capture preparation configuration differs from the delivery identity",
    )
    _require(
        posixpath.basename(document_key(top_path)) == posixpath.basename(document_key(main_assembly)),
        "Dependency closure top level differs from native discovery",
    )
    _require(
        isinstance(scene.get("document"), str) and _same_native_path(scene["document"], top_path),
        "Capture scene document differs from the dependency closure",
    )
    scene_source_root = scene.get("source_root")
    _require(
        isinstance(scene_source_root, str) and bool(scene_source_root),
        "Capture scene carries no source root",
    )
    _require(
        isinstance(preparation["document"], str) and _same_native_path(preparation["document"], top_path),
        "Capture preparation document differs from the dependency closure",
    )
    _require(preparation["configuration"] == configuration, "Capture preparation configuration changed")
    _require(preparation["scope"] == "collected_copy_in_memory", "Capture preparation ran outside the collected copy")
    _require(preparation["read_only"] is True, "Capture preparation did not prove top-level read-only state")
    _require(preparation["saved_to_disk"] is False, "Capture preparation claims a source save")

    def state_row(value, label, *, controls_required=True):
        _require(isinstance(value, dict), f"{label} is not an object")
        required_state = {
            "save_flag",
            "needs_rebuild2_value",
            "read_only",
            "configuration",
            "configuration_needs_rebuild",
            "path",
            "configurations",
        }
        if controls_required:
            required_state.add("controls")
        _require(set(value) == required_state, f"{label} fields are incomplete or unknown")
        _require(type(value["save_flag"]) is bool, f"{label} save flag is not boolean")
        _require(
            type(value["needs_rebuild2_value"]) is int and value["needs_rebuild2_value"] in (0, 1, 2),
            f"{label} NeedsRebuild2 is not a documented enum",
        )
        _require(type(value["read_only"]) is bool, f"{label} read-only flag is not boolean")
        _require(type(value["configuration_needs_rebuild"]) is bool, f"{label} configuration is unread")
        _require(isinstance(value["path"], str) and bool(value["path"]), f"{label} path is missing")
        _require(
            isinstance(value["configuration"], str) and bool(value["configuration"]),
            f"{label} configuration is missing",
        )
        configurations = value["configurations"]
        _require(isinstance(configurations, list) and bool(configurations), f"{label} configurations are missing")
        names = []
        for item in configurations:
            _require(
                isinstance(item, dict)
                and set(item) == {"name", "needs_rebuild", "add_rebuild_save_mark"}
                and isinstance(item["name"], str)
                and bool(item["name"])
                and type(item["needs_rebuild"]) is bool
                and type(item["add_rebuild_save_mark"]) is bool,
                f"{label} configuration row is malformed",
            )
            names.append(item["name"])
        _require(len(names) == len(set(names)), f"{label} repeats a configuration")
        _require(value["configuration"] in names, f"{label} active configuration is absent")
        if not controls_required:
            return value

        controls = value["controls"]
        _require(isinstance(controls, dict), f"{label} rebuild controls are malformed")
        allowed_controls = {
            "enable_assembly_rebuild",
            "lightweight_component_count",
            "enable_assembly_rebuild_error",
            "lightweight_component_count_error",
        }
        _require(set(controls) <= allowed_controls, f"{label} rebuild controls have unknown fields")
        _require(
            "enable_assembly_rebuild" in controls
            and (
                type(controls["enable_assembly_rebuild"]) is bool
                or (
                    controls["enable_assembly_rebuild"] is None
                    and isinstance(controls.get("enable_assembly_rebuild_error"), str)
                    and bool(controls["enable_assembly_rebuild_error"])
                )
            ),
            f"{label} EnableAssemblyRebuild evidence is incomplete",
        )
        _require(
            "lightweight_component_count" in controls
            and (
                (
                    type(controls["lightweight_component_count"]) is int
                    and controls["lightweight_component_count"] >= 0
                )
                or (
                    controls["lightweight_component_count"] is None
                    and isinstance(controls.get("lightweight_component_count_error"), str)
                    and bool(controls["lightweight_component_count_error"])
                )
            ),
            f"{label} lightweight evidence is incomplete",
        )
        if controls["enable_assembly_rebuild"] is not None:
            _require(
                "enable_assembly_rebuild_error" not in controls,
                f"{label} has both EnableAssemblyRebuild and its error",
            )
        if controls["lightweight_component_count"] is not None:
            _require(
                "lightweight_component_count_error" not in controls,
                f"{label} has both lightweight count and its error",
            )
        return value

    def source_map(value, label):
        _require(isinstance(value, dict) and bool(value), f"{label} is missing")
        normalized = {}
        for key, row in value.items():
            _require(
                isinstance(key, str)
                and isinstance(row, dict)
                and set(row) == {"path", "sha256"}
                and isinstance(row["path"], str)
                and isinstance(row["sha256"], str)
                and re.fullmatch(r"[0-9a-f]{64}", row["sha256"]) is not None,
                f"{label} carries a malformed source row",
            )
            path_key = normalized_path(row["path"])
            _require(key == path_key, f"{label} key does not match its exact path")
            _require(path_key not in normalized, f"{label} repeats a source path")
            normalized[path_key] = row
        return normalized

    topology_fields = {
        "instance",
        "depth",
        "document_path",
        "normalized_path",
        "document_type",
        "referenced_configuration",
        "suppression_state",
        "storage_scope",
        "provenance_state",
        "owner_document_path",
        "owner_sha256",
        "native_api_pid",
    }

    def topology_row(value, label, session):
        _require(
            isinstance(value, dict) and set(value) == topology_fields,
            f"{label} is malformed",
        )
        _require(isinstance(value["instance"], str) and bool(value["instance"]), f"{label} instance is invalid")
        _require(
            type(value["depth"]) is int and value["depth"] >= 0,
            f"{label} depth is invalid",
        )
        _require(
            value["storage_scope"] in {"file", "session", "suppressed"},
            f"{label} storage scope is invalid",
        )
        if value["storage_scope"] == "suppressed":
            _require(
                value["document_path"] is None
                and value["normalized_path"] is None
                and value["document_type"] is None
                and value["referenced_configuration"] is None
                and value["provenance_state"] is None
                and value["owner_document_path"] is None
                and value["owner_sha256"] is None
                and value["native_api_pid"] is None,
                f"{label} suppressed fields are contradictory",
            )
        else:
            _require(
                isinstance(value["document_path"], str) and bool(value["document_path"]),
                f"{label} document path is missing",
            )
            _require(
                value["normalized_path"] == normalized_path(value["document_path"]),
                f"{label} normalized path differs from its document path",
            )
            _require(
                type(value["document_type"]) is int and value["document_type"] in {1, 2},
                f"{label} document type is invalid",
            )
            _require(
                isinstance(value["referenced_configuration"], str)
                and bool(value["referenced_configuration"]),
                f"{label} referenced configuration is missing",
            )
            if value["storage_scope"] == "file":
                _require(
                    value["provenance_state"] is None
                    and value["owner_document_path"] is None
                    and value["owner_sha256"] is None
                    and value["native_api_pid"] is None,
                    f"{label} file provenance is contradictory",
                )
            else:
                _require(
                    value["provenance_state"] in {"cached_materialization", "embedded_virtual_component"},
                    f"{label} session provenance is invalid",
                )
                _require(
                    isinstance(value["owner_document_path"], str)
                    and bool(value["owner_document_path"])
                    and isinstance(value["owner_sha256"], str)
                    and re.fullmatch(r"[0-9a-f]{64}", value["owner_sha256"]) is not None
                    and type(value["native_api_pid"]) is int
                    and value["native_api_pid"] > 0,
                    f"{label} session ownership evidence is incomplete",
                )
                _require(
                    value["native_api_pid"] == session["identity"]["pid"],
                    f"{label} session path is not owned by its recorded native process",
                )
                if value["provenance_state"] == "embedded_virtual_component":
                    _require(
                        embedded_virtual_component_under_root(value["document_path"], session["temp_root"]),
                        f"{label} lacks a VC~~ controlled temp path",
                    )
                else:
                    _require(
                        session_materialization_under_root(value["document_path"], session["temp_root"]),
                        f"{label} lacks an IC~~ controlled temp path",
                    )
        return value

    def topology_rows(value, label, session):
        _require(isinstance(value, list) and bool(value), f"{label} is missing")
        rows = [topology_row(item, f"{label} row", session) for item in value]
        instances = [item["instance"] for item in rows]
        _require(len(instances) == len(set(instances)), f"{label} repeats an instance path")
        top_rows = [item for item in rows if item["instance"] == "top"]
        _require(len(top_rows) == 1, f"{label} must contain exactly one top occurrence")
        return rows

    def topology_semantic(rows):
        def owner_identity(value):
            if value is None:
                return None
            parts = [
                part.casefold()
                for part in re.split(r"[\\/]", str(value))
                if part
            ]
            markers = [part for part in parts if part in {"ic~~", "vc~~"}]
            if markers:
                return session_path_identity(value)
            return normalized_path(value)

        return sorted(
            [
                {
                    "instance": row["instance"],
                    "depth": row["depth"],
                    "document_type": row["document_type"],
                    "referenced_configuration": row["referenced_configuration"],
                    "suppression_state": row["suppression_state"],
                    "storage_scope": row["storage_scope"],
                    "provenance_state": row["provenance_state"],
                    "owner_sha256": row["owner_sha256"],
                    "owner_document_path": owner_identity(
                        row["owner_document_path"]
                    ),
                    **(
                        {}
                        if row["storage_scope"] == "session"
                        else {"normalized_path": row["normalized_path"]}
                    ),
                }
                for row in rows
            ],
            key=lambda row: json.dumps(row, sort_keys=True, separators=(",", ":")),
        )

    def loaded_document_signature(value, label):
        _require(
            isinstance(value, dict)
            and set(value)
            == {"file_paths", "session_paths", "document_types"},
            f"{label} is malformed",
        )
        file_paths = value["file_paths"]
        session_paths = value["session_paths"]
        document_types = value["document_types"]
        _require(
            isinstance(file_paths, list)
            and file_paths == sorted(set(file_paths))
            and all(isinstance(path, str) and path for path in file_paths),
            f"{label} file paths are malformed",
        )
        _require(
            isinstance(session_paths, list)
            and session_paths == sorted(set(session_paths))
            and all(
                isinstance(path, str)
                and path.split("/", 1)[0] in {"ic~~", "vc~~"}
                and len(path.split("/")) >= 2
                for path in session_paths
            ),
            f"{label} session paths are malformed",
        )
        _require(
            isinstance(document_types, list)
            and all(
                isinstance(row, dict)
                and set(row) == {"path", "document_type"}
                and isinstance(row["path"], str)
                and type(row["document_type"]) is int
                and row["document_type"] in {1, 2}
                for row in document_types
            ),
            f"{label} document types are malformed",
        )
        typed_paths = [row["path"] for row in document_types]
        _require(
            typed_paths == sorted(set(typed_paths))
            and set(typed_paths) == set(file_paths) | set(session_paths),
            f"{label} document types contradict the loaded paths",
        )
        return {
            "file_paths": file_paths,
            "session_paths": session_paths,
            "document_types": document_types,
        }

    def validate_loaded_inventory(value, topology, sources, label):
        inventory = loaded_document_signature(value, label)
        _require(
            set(inventory["file_paths"]) == set(sources),
            f"{label} file documents differ from the source inventory",
        )
        topology_types = {}
        for item in topology:
            if item["storage_scope"] == "suppressed":
                continue
            identity = (
                session_path_identity(item["document_path"])
                if item["storage_scope"] == "session"
                else normalized_path(item["document_path"])
            )
            prior = topology_types.setdefault(
                identity,
                item["document_type"],
            )
            _require(
                prior == item["document_type"],
                f"{label} has conflicting types for one document",
            )
        expected_sessions = sorted(
            identity
            for identity, document_type in topology_types.items()
            if any(
                item["storage_scope"] == "session"
                and session_path_identity(item["document_path"]) == identity
                for item in topology
            )
        )
        _require(
            inventory["session_paths"] == expected_sessions,
            f"{label} session documents differ from the topology",
        )
        _require(
            {
                row["path"]: row["document_type"]
                for row in inventory["document_types"]
            }
            == topology_types,
            f"{label} document types differ from the topology",
        )
        return inventory

    before = state_row(preparation["state_before"], "capture state_before")
    after_children = state_row(preparation["state_after_children"], "capture state_after_children")
    after = state_row(preparation["state_after"], "capture state_after")
    for state, label in (
        (before, "capture state_before"),
        (after_children, "capture state_after_children"),
        (after, "capture state_after"),
    ):
        _require(_same_native_path(state["path"], top_path), f"{label} names another top document")
        _require(state["configuration"] == configuration, f"{label} uses another configuration")
    _require(before["read_only"] is True, "capture state_before is not read-only")
    _require(after_children["read_only"] is True, "capture state_after_children is not read-only")
    _require(after["read_only"] is True, "capture state_after is not read-only")
    _require(
        after["needs_rebuild2_value"] == 0
        and after["configuration_needs_rebuild"] is False
        and next(
            (
                item["needs_rebuild"]
                for item in after["configurations"]
                if item["name"] == configuration
            ),
            True,
        )
        is False,
        "capture state_after is not fully rebuilt",
    )

    topology_before = topology_rows(preparation["topology_before"], "topology_before", prepared_session)
    topology_after = topology_rows(preparation["topology_after"], "topology_after", prepared_session)
    _require(topology_before == topology_after, "Capture rebuild changed the final topology")
    _require(
        topology_before == sorted(
            topology_before,
            key=lambda row: tuple(
                "" if row.get(name) is None else str(row.get(name))
                for name in (
                    "instance",
                    "normalized_path",
                    "document_type",
                    "referenced_configuration",
                    "depth",
                    "suppression_state",
                    "storage_scope",
                    "provenance_state",
                )
            ),
        ),
        "Capture topology is not deterministically ordered",
    )
    not_top = [item for item in topology_before if item["instance"] != "top"]
    _require(
        preparation["component_count"] == len(not_top),
        "Capture component count differs from its exact topology",
    )

    raw_components = raw.get("components")
    _require(isinstance(raw_components, list) and bool(raw_components), "Native discovery has no components")
    raw_active = [item for item in raw_components if isinstance(item, dict) and item.get("suppressed") is False]
    _require(
        len(raw_active) == sum(item["suppression_state"] in {2, 3} for item in not_top),
        "Capture topology differs from active native discovery components",
    )
    raw_by_instance = {}
    for item in raw_active:
        instance = str(item.get("instance_id") or "")
        _require(bool(instance) and instance not in raw_by_instance, "Native discovery repeats an instance identity")
        raw_by_instance[instance] = item
    topology_by_instance = {item["instance"]: item for item in not_top}
    _require(
        set(raw_by_instance)
        == {
            name
            for name, item in topology_by_instance.items()
            if item["suppression_state"] in {2, 3}
        },
        "Capture topology does not cover native discovery occurrences exactly",
    )
    for instance, item in raw_by_instance.items():
        row = topology_by_instance[instance]
        raw_document = str(item.get("document") or "")
        expected_type = 2 if raw_document.casefold().endswith(".sldasm") else 1
        _require(row["document_type"] == expected_type, f"Capture occurrence type changed: {instance}")
        _require(
            row["referenced_configuration"] == item.get("configuration"),
            f"Capture occurrence configuration changed: {instance}",
        )
        _require(
            row["depth"] == instance.count("/") + 1,
            f"Capture occurrence depth changed: {instance}",
        )
        _require(
            posixpath.basename(document_key(row["document_path"]))
            == posixpath.basename(document_key(raw_document)),
            f"Capture occurrence document changed: {instance}",
        )

    selection_edges = {}
    for owner, record in documents.items():
        _require(isinstance(record, dict), f"Native discovery document is malformed: {owner}")
        edges = record.get("resolved_edges")
        _require(isinstance(edges, list), f"Native discovery has no edge list: {owner}")
        for edge in edges:
            _require(isinstance(edge, dict), "Native discovery edge is malformed")
            instance = str(edge.get("instance") or "")
            _require(bool(instance) and instance not in selection_edges, "Native discovery repeats an edge instance")
            selection_edges[instance] = edge
    session_expected = {}
    virtual_components = selection_closure.get("virtual_components")
    _require(isinstance(virtual_components, list), "Native discovery virtual component list is missing")
    for item in virtual_components:
        _require(isinstance(item, dict), "Native discovery virtual component is malformed")
        instance = str(item.get("instance") or "")
        _require(bool(instance) and instance not in session_expected, "Native discovery repeats a virtual instance")
        session_expected[instance] = {
            "document_type": 2 if str(item.get("session_document_path") or "").casefold().endswith(".sldasm") else 1,
            "configuration": item.get("configuration"),
            "provenance_state": "embedded_virtual_component",
            "session_suffix": session_path_suffix(
                item.get("session_document_path"),
                "VC~~",
            ),
        }
    imports = selection_closure.get("imports")
    _require(isinstance(imports, list), "Native discovery import list is missing")
    for item in imports:
        _require(isinstance(item, dict), "Native discovery import is malformed")
        instance = str(item.get("instance") or "")
        _require(bool(instance) and instance not in session_expected, "Native discovery repeats an import instance")
        session_expected[instance] = {
            "document_type": 2 if str(item.get("materialization") or "").casefold().endswith(".sldasm") else 1,
            "configuration": item.get("configuration"),
            "provenance_state": "cached_materialization",
            "session_suffix": session_path_suffix(
                item.get("materialization"),
                "IC~~",
            ),
        }
    suppressed_entries = selection_closure.get("suppressed")
    _require(isinstance(suppressed_entries, list), "Native discovery suppressed occurrence list is missing")
    suppressed_expected = {}
    for item in suppressed_entries:
        _require(isinstance(item, dict), "Native discovery suppressed occurrence is malformed")
        instance = str(item.get("instance") or "")
        _require(
            bool(instance)
            and instance not in suppressed_expected
            and instance not in selection_edges
            and instance not in session_expected,
            "Native discovery repeats a suppressed occurrence identity",
        )
        _require(
            item.get("suppression_state") == 0,
            f"Native discovery suppressed occurrence has another state: {instance}",
        )
        suppressed_expected[instance] = item
    _require(
        {item["instance"] for item in not_top}
        == set(selection_edges) | set(session_expected) | set(suppressed_expected),
        "Capture topology differs from the sealed native occurrence graph",
    )
    for instance, row in topology_by_instance.items():
        if instance in session_expected:
            expected = session_expected[instance]
            _require(row["storage_scope"] == "session", f"Session occurrence was treated as a file: {instance}")
            _require(row["provenance_state"] == expected["provenance_state"], f"Session class changed: {instance}")
            _require(row["document_type"] == expected["document_type"], f"Session type changed: {instance}")
            marker = "VC~~" if expected["provenance_state"] == "embedded_virtual_component" else "IC~~"
            _require(
                session_path_suffix(row["document_path"], marker) == expected["session_suffix"],
                f"Session path identity changed: {instance}",
            )
            _require(
                expected["configuration"] in {None, row["referenced_configuration"]},
                f"Session configuration changed: {instance}",
            )
        elif instance in suppressed_expected:
            _require(row["storage_scope"] == "suppressed", f"Suppressed occurrence became active: {instance}")
            _require(row["suppression_state"] == 0, f"Suppressed occurrence state changed: {instance}")
        else:
            edge = selection_edges[instance]
            _require(row["storage_scope"] == "file", f"File occurrence was treated as a session document: {instance}")
            _require(
                row["document_type"] == (2 if str(edge.get("resolved") or "").casefold().endswith(".sldasm") else 1),
                f"Native occurrence type changed: {instance}",
            )
            _require(
                row["referenced_configuration"] == edge.get("configuration"),
                f"Native occurrence configuration changed: {instance}",
            )
            _require(
                row["suppression_state"] == edge.get("suppression_state"),
                f"Native occurrence suppression changed: {instance}",
            )
            _require(
                row["depth"] == instance.count("/") + 1,
                f"Native occurrence depth changed: {instance}",
            )

    top_key = normalized_path(top_path)
    top_row = next(item for item in topology_before if item["instance"] == "top")
    _require(top_row["storage_scope"] == "file", "Top assembly was not captured as a file")
    _require(top_row["document_type"] == 2, "Top capture document is not an assembly")
    _require(_same_native_path(top_row["document_path"], top_path), "Top topology path changed")
    _require(
        top_row["referenced_configuration"] == configuration,
        "Top topology configuration changed",
    )
    _require(top_row["depth"] == 0 and top_row["suppression_state"] is None, "Top topology fields changed")

    before_sources = source_map(preparation["source_files_before"], "source_files_before")
    after_sources = source_map(preparation["source_files_after"], "source_files_after")
    _require(before_sources == after_sources, "Capture rebuild changed collected source bytes")
    file_topology_rows = [item for item in not_top if item["storage_scope"] == "file"]
    file_document_paths = {normalized_path(item["document_path"]) for item in file_topology_rows}
    _require(
        set(before_sources) == file_document_paths | {top_key},
        "Source inventory differs from file-backed topology",
    )

    def verify_occurrence_owners(rows, label):
        by_instance = {item["instance"]: item for item in rows}
        for row in rows:
            if row["instance"] == "top":
                continue
            _require(
                row["depth"] == row["instance"].count("/") + 1,
                f"{label} occurrence depth differs from its instance path",
            )
            parent_instance = "top" if row["depth"] == 1 else row["instance"].rsplit("/", 1)[0]
            parent = by_instance.get(parent_instance)
            _require(
                parent is not None
                and parent["document_type"] == 2
                and parent["depth"] == row["depth"] - 1,
                f"{label} occurrence has no exact assembly parent: {row['instance']}",
            )
            if row["storage_scope"] != "session":
                continue
            while parent["storage_scope"] == "session":
                ancestor = "top" if parent["depth"] == 1 else parent["instance"].rsplit("/", 1)[0]
                ancestor_row = by_instance.get(ancestor)
                _require(
                    ancestor_row is not None
                    and ancestor_row["document_type"] == 2
                    and ancestor_row["depth"] == parent["depth"] - 1,
                    f"{label} session occurrence has no file-backed owner",
                )
                parent = ancestor_row
            owner_key = normalized_path(parent["document_path"])
            _require(
                parent["storage_scope"] == "file"
                and owner_key in before_sources
                and normalized_path(row["owner_document_path"]) == owner_key
                and row["owner_sha256"] == before_sources[owner_key]["sha256"],
                f"{label} session owner differs from its occurrence parent: {row['instance']}",
            )

    verify_occurrence_owners(topology_before, "Capture preparation")
    copied = closure.get("copy_files")
    mapping = closure.get("mapping")
    _require(isinstance(copied, dict) and bool(copied), "Dependency closure has no copied source inventory")
    _require(isinstance(mapping, list) and bool(mapping), "Dependency closure has no source mapping")
    mapping_by_instance = {}
    for item in mapping:
        _require(isinstance(item, dict), "Dependency closure mapping row is malformed")
        instance = str(item.get("instance") or "")
        _require(
            bool(instance) and instance not in mapping_by_instance,
            "Dependency closure repeats a mapping instance",
        )
        mapping_by_instance[instance] = item
    for topology in [top_row, *file_topology_rows]:
        key = normalized_path(topology["document_path"])
        row = before_sources[key]
        occurrence = "top_level" if topology["instance"] == "top" else topology["instance"]
        mapped = mapping_by_instance.get(occurrence)
        _require(mapped is not None, f"Source occurrence has no closure mapping: {occurrence}")
        copy_name = str(mapped.get("copy") or "")
        _require(copied.get(copy_name) == row["sha256"], f"Closure copy digest differs: {copy_name}")
        _require(mapped.get("copy_sha256") == row["sha256"], f"Closure copy identity differs: {copy_name}")
        copy_relative = copy_name[7:] if copy_name.casefold().startswith("source/") else copy_name
        source_relative = recorded_relative_path(topology["document_path"], scene_source_root)
        _require(
            document_key(copy_relative) == source_relative,
            f"Closure copy names another capture path: {occurrence}",
        )
        expected_edge = selection_edges.get(topology["instance"])
        expected_document = main_assembly if topology["instance"] == "top" else expected_edge.get("resolved")
        _require(
            copy_matches_document(copy_relative, expected_document),
            f"Closure copy names another document: {occurrence}",
        )
        _require(
            copy_matches_document(mapped.get("source"), expected_document),
            f"Closure source names another document: {occurrence}",
        )
    session_rows = [item for item in not_top if item["storage_scope"] == "session"]
    session_dependencies = preparation["session_dependency_paths"]
    _require(
        isinstance(session_dependencies, list)
        and all(isinstance(value, str) and value for value in session_dependencies)
        and len(session_dependencies) == len(set(session_dependencies))
        and session_dependencies == sorted(session_dependencies),
        "Session dependency inventory must be a unique sorted list",
    )
    _require(
        set(session_dependencies) == {item["document_path"] for item in session_rows},
        "Session dependency inventory differs from topology",
    )
    loaded_before = validate_loaded_inventory(
        preparation["loaded_documents_before"],
        topology_before,
        before_sources,
        "loaded_documents_before",
    )
    loaded_after = validate_loaded_inventory(
        preparation["loaded_documents_after"],
        topology_after,
        after_sources,
        "loaded_documents_after",
    )
    _require(
        loaded_before == loaded_after,
        "Capture rebuild changed the final loaded document inventory",
    )
    top_sha256 = before_sources[top_key]["sha256"]
    _require(
        preparation["source_sha256_before"] == preparation["source_sha256_after"] == top_sha256,
        "Top source digest is not stable",
    )

    assembly_before = preparation["assembly_states_before"]
    assembly_after = preparation["assembly_states_after"]
    _require(isinstance(assembly_before, list) and bool(assembly_before), "assembly_states_before is missing")
    _require(
        isinstance(assembly_after, list) and len(assembly_after) == len(assembly_before),
        "assembly final state set differs",
    )

    groups = {}
    for item in topology_before:
        if item["document_type"] != 2:
            continue
        key = normalized_path(item["document_path"])
        group = groups.setdefault(
            key,
            {
                "depth": item["depth"],
                "path": item["document_path"],
                "instances": [],
                "configurations": set(),
            },
        )
        group["depth"] = max(group["depth"], item["depth"])
        group["instances"].append(item["instance"])
        group["configurations"].add(item["referenced_configuration"])
    _require(
        preparation["assembly_document_count"] == len(groups),
        "Assembly document count differs from exact topology",
    )
    before_rows = {}
    after_rows = {}
    for rows, target, label in (
        (assembly_before, before_rows, "assembly_states_before"),
        (assembly_after, after_rows, "assembly_states_after"),
    ):
        for item in rows:
            _require(
                isinstance(item, dict) and set(item) == {"depth", "path", "instances", "state"},
                f"{label} row is malformed",
            )
            _require(type(item["depth"]) is int and item["depth"] >= 0, f"{label} depth is invalid")
            _require(isinstance(item["path"], str) and bool(item["path"]), f"{label} path is invalid")
            _require(
                isinstance(item["instances"], list)
                and bool(item["instances"])
                and all(isinstance(name, str) and bool(name) for name in item["instances"])
                and len(item["instances"]) == len(set(item["instances"])),
                f"{label} instances are invalid",
            )
            key = normalized_path(item["path"])
            _require(key not in target, f"{label} repeats a document")
            target[key] = item
            state_row(item["state"], f"{label} state")
            _require(_same_native_path(item["state"]["path"], item["path"]), f"{label} state names another document")
    _require(set(before_rows) == set(groups), "Assembly before-state set differs from exact topology")
    _require(set(after_rows) == set(groups), "Assembly final-state set differs from exact topology")
    for key, group in groups.items():
        _require(bool(group["configurations"]), "An assembly occurrence has no referenced configuration")
        for target, label in ((before_rows, "before"), (after_rows, "final")):
            row = target[key]
            _require(row["depth"] == group["depth"], f"Assembly {label} depth differs: {group['path']}")
            _require(
                sorted(row["instances"]) == sorted(group["instances"]),
                f"Assembly {label} instances differ: {group['path']}",
            )
            configuration_names = {item["name"] for item in row["state"]["configurations"]}
            _require(
                group["configurations"] <= configuration_names,
                f"Assembly {label} omits a referenced configuration: {group['path']}",
            )
    _require(before_rows[top_key]["state"] == before, "Top state_before contradicts the assembly graph")
    _require(after_rows[top_key]["state"] == after, "Top state_after contradicts the assembly graph")
    _require(after_rows[top_key]["state"]["configuration"] == configuration, "Top final configuration changed")
    _require(after_rows[top_key]["state"]["read_only"] is True, "Final graph does not bind top read-only state")
    for key, item in after_rows.items():
        group = groups[key]
        state = item["state"]
        _require(
            state["needs_rebuild2_value"] == 0
            and state["configuration_needs_rebuild"] is False
            and all(
                row["needs_rebuild"] is False
                for row in state["configurations"]
                if row["name"] in group["configurations"]
            ),
            f"Assembly did not finish fully rebuilt: {item['path']}",
        )

    part_groups = {}
    for item in topology_before:
        if item["document_type"] != 1:
            continue
        key = normalized_path(item["document_path"])
        group = part_groups.setdefault(
            key,
            {
                "path": item["document_path"],
                "instances": [],
                "configurations": set(),
            },
        )
        group["instances"].append(item["instance"])
        group["configurations"].add(item["referenced_configuration"])
    part_rows = {}
    part_states = preparation["part_states_after"]
    _require(isinstance(part_states, list), "part_states_after is missing")
    for item in part_states:
        _require(
            isinstance(item, dict) and set(item) == {"path", "instances", "state"},
            "part_states_after row is malformed",
        )
        _require(isinstance(item["path"], str) and bool(item["path"]), "part_states_after path is invalid")
        _require(
            isinstance(item["instances"], list)
            and bool(item["instances"])
            and all(isinstance(name, str) and bool(name) for name in item["instances"])
            and len(item["instances"]) == len(set(item["instances"])),
            "part_states_after instances are invalid",
        )
        key = normalized_path(item["path"])
        _require(key not in part_rows, "part_states_after repeats a document")
        part_rows[key] = item
        state_row(item["state"], "part_states_after state", controls_required=False)
        _require(
            _same_native_path(item["state"]["path"], item["path"]),
            "part_states_after state names another document",
        )
    _require(set(part_rows) == set(part_groups), "Part final-state set differs from exact topology")
    for key, group in part_groups.items():
        _require(bool(group["configurations"]), "A part occurrence has no referenced configuration")
        row = part_rows[key]
        _require(
            sorted(row["instances"]) == sorted(group["instances"]),
            f"Part final instances differ: {group['path']}",
        )
        state = row["state"]
        configuration_names = {item["name"] for item in state["configurations"]}
        _require(
            group["configurations"] <= configuration_names,
            f"Part final state omits a referenced configuration: {group['path']}",
        )
        _require(
            state["needs_rebuild2_value"] == 0
            and state["configuration_needs_rebuild"] is False
            and all(
                item["needs_rebuild"] is False
                for item in state["configurations"]
                if item["name"] in group["configurations"]
            ),
            f"Part did not finish fully rebuilt: {group['path']}",
        )

    if session_recovery is not None:
        _require(
            set(session_recovery)
            == {
                "schema_version",
                "first_attempt",
                "retirement",
                "reopen",
                "fresh_session",
            }
            and session_recovery["schema_version"]
            == "solidworks-to-urdf.rebuild-session-recovery/v2",
            "Capture session recovery fields are incomplete or unknown",
        )

        def recovery_identity(value, label, *, required_state):
            _require(
                isinstance(value, dict)
                and set(value)
                == {
                    "pid",
                    "state",
                    "executable",
                    "ownership",
                    "lease",
                },
                f"{label} fields are incomplete or unknown",
            )
            _require(
                type(value["pid"]) is int
                and value["pid"] > 0
                and value["state"] == required_state
                and isinstance(value["executable"], str)
                and bool(value["executable"])
                and value["ownership"] == "windows_job",
                f"{label} process identity is invalid",
            )
            lease = value["lease"]
            _require(
                isinstance(lease, dict)
                and set(lease) == {"path", "held", "holder_pid"}
                and isinstance(lease["path"], str)
                and bool(lease["path"])
                and type(lease["held"]) is bool
                and (
                    (
                        required_state == "active"
                        and lease["held"] is True
                        and type(lease["holder_pid"]) is int
                        and lease["holder_pid"] > 0
                    )
                    or (
                        required_state == "closed"
                        and lease["held"] is False
                        and lease["holder_pid"] is None
                    )
                ),
                f"{label} lease identity contradicts its process state",
            )
            return value

        def attempt_session(value, label):
            _require(
                isinstance(value, dict)
                and set(value) == {"role", "identity", "temp_root"},
                f"{label} fields are incomplete or unknown",
            )
            identity = recovery_identity(
                value["identity"],
                f"{label} identity",
                required_state="active",
            )
            _require(
                value["role"] == "copy",
                f"{label} is not an active owned copy process",
            )
            temp_root = value["temp_root"]
            _require(
                temp_root is None
                or (
                    isinstance(temp_root, str)
                    and session_root_pid(temp_root) == identity["pid"]
                ),
                f"{label} temp root differs from its process",
            )
            return value

        first_attempt = session_recovery["first_attempt"]
        _require(
            isinstance(first_attempt, dict)
            and set(first_attempt)
            == {
                "session",
                "rebuild_calls",
                "source_files",
                "topology",
                "loaded_documents",
            },
            "Capture first-attempt evidence is malformed",
        )
        old_session = attempt_session(
            first_attempt["session"],
            "Capture first-attempt session",
        )
        before_sessions = (
            (before_recovery_environment.get("solidworks") or {}).get("sessions")
        )
        _require(
            isinstance(before_sessions, dict)
            and set(before_sessions) == {"source", "copy"}
            and before_sessions["copy"] == old_session["identity"]
            and before_sessions["source"].get("state") == "closed",
            "Capture pre-recovery environment does not bind the first attempt",
        )
        final_session = attempt_session(
            session_recovery["fresh_session"],
            "Capture fresh-session identity",
        )
        final_identity = recovery_identity(
            prepared_session["identity"],
            "Capture prepared-session identity",
            required_state="active",
        )
        _require(
            final_session == prepared_session
            and final_identity == final_session["identity"]
            and old_session["identity"]["pid"]
            != final_session["identity"]["pid"],
            "Capture fresh session is not independently owned",
        )
        _require(
            old_session["identity"]["executable"]
            == final_session["identity"]["executable"]
            and old_session["identity"]["ownership"]
            == final_session["identity"]["ownership"]
            and old_session["identity"]["lease"]
            == final_session["identity"]["lease"],
            "Capture recovery changed the native executable or lease domain",
        )
        retirement = session_recovery["retirement"]
        _require(
            isinstance(retirement, dict)
            and set(retirement)
            == {"role", "retired", "before", "after"}
            and retirement["role"] == "copy"
            and retirement["retired"] is True
            and retirement["before"] == old_session["identity"]
            and recovery_identity(
                retirement["after"],
                "Capture retired-session identity",
                required_state="closed",
            )
            == retirement["after"]
            and retirement["after"]["pid"]
            == old_session["identity"]["pid"]
            and retirement["after"]["executable"]
            == old_session["identity"]["executable"]
            and retirement["after"]["ownership"]
            == old_session["identity"]["ownership"]
            and retirement["after"]["lease"]["path"]
            == old_session["identity"]["lease"]["path"],
            "Capture old session retirement is not proven",
        )
        reopened = session_recovery["reopen"]
        _require(
            isinstance(reopened, dict)
            and set(reopened)
            == {"opened", "path", "read_only", "errors", "warnings"}
            and isinstance(reopened["opened"], str)
            and bool(reopened["opened"])
            and _same_native_path(reopened["path"], top_path)
            and reopened["read_only"] is True
            and type(reopened["errors"]) is int
            and reopened["errors"] == 0,
            "Capture recovery did not reopen the exact top read-only",
        )
        _require(
            type(reopened["warnings"]) is int and reopened["warnings"] >= 0,
            "Capture recovery reopen warnings are invalid",
        )
        _require(
            isinstance(environment, dict)
            and isinstance(before_recovery_environment, dict),
            "Capture recovery environment evidence is malformed",
        )
        for field in ("platform", "python", "worker_version", "toolchain"):
            _require(
                before_recovery_environment.get(field) == environment.get(field),
                f"Capture recovery changed native runtime field: {field}",
            )
        before_solidworks = before_recovery_environment["solidworks"]
        final_solidworks = environment["solidworks"]
        for field in (
            "revision",
            "input_mode",
            "build",
            "license",
            "visible",
            "unavailable",
        ):
            _require(
                before_solidworks.get(field)
                == final_solidworks.get(field),
                f"Capture recovery changed SolidWorks field: {field}",
            )
        before_native_sessions = before_solidworks.get("sessions")
        final_native_sessions = final_solidworks.get("sessions")
        _require(
            isinstance(before_native_sessions, dict)
            and set(before_native_sessions) == {"source", "copy"}
            and isinstance(final_native_sessions, dict)
            and set(final_native_sessions) == {"source", "copy"}
            and before_native_sessions["source"]
            == final_native_sessions["source"]
            and before_native_sessions["copy"]
            == old_session["identity"]
            and final_native_sessions["copy"]
            == final_session["identity"],
            "Capture recovery changed native session continuity",
        )
        first_topology = topology_rows(
            first_attempt["topology"],
            "Capture first-attempt topology",
            old_session,
        )
        first_sources = source_map(
            first_attempt["source_files"],
            "Capture first-attempt source_files",
        )
        _require(
            first_sources == before_sources,
            "Capture first attempt and final recovery used different source bytes",
        )
        _require(
            topology_semantic(first_topology)
            == topology_semantic(topology_before),
            "Capture first attempt and final recovery changed native topology",
        )
        first_loaded = validate_loaded_inventory(
            first_attempt["loaded_documents"],
            first_topology,
            first_sources,
            "Capture first-attempt loaded_documents",
        )
        _require(
            first_loaded == loaded_before,
            "Capture recovery loaded a different document inventory",
        )

        first_assembly_rows = {}
        for item in first_topology:
            if item["document_type"] != 2:
                continue
            identity = (
                session_path_identity(item["document_path"])
                if item["storage_scope"] == "session"
                else normalized_path(item["document_path"])
            )
            group = first_assembly_rows.setdefault(
                identity,
                {
                    "depth": item["depth"],
                    "instances": [],
                    "configurations": set(),
                    "paths": set(),
                },
            )
            group["depth"] = max(group["depth"], item["depth"])
            group["instances"].append(item["instance"])
            group["configurations"].add(item["referenced_configuration"])
            group["paths"].add(item["document_path"])

        def first_document_identity(path):
            matches = {
                (
                    session_path_identity(item["document_path"])
                    if item["storage_scope"] == "session"
                    else normalized_path(item["document_path"])
                )
                for item in first_topology
                if (
                    session_path_identity(item["document_path"])
                    == session_path_identity(path)
                    if item["storage_scope"] == "session"
                    else normalized_path(item["document_path"])
                    == normalized_path(path)
                )
            }
            _require(
                len(matches) == 1,
                "Capture first-attempt document identity is unknown or ambiguous",
            )
            return next(iter(matches))

        first_by_instance = {
            item["instance"]: item for item in first_topology
        }
        first_top_identity = first_document_identity(top_path)
        first_parents_by_document = {}
        for item in first_topology:
            if (
                item["instance"] == "top"
                or item["storage_scope"] == "suppressed"
                or item["document_type"] != 2
            ):
                continue
            parent_instance = (
                "top"
                if item["depth"] == 1
                else item["instance"].rsplit("/", 1)[0]
            )
            parent = first_by_instance.get(parent_instance)
            _require(
                parent is not None
                and parent["document_type"] == 2
                and parent["depth"] == item["depth"] - 1,
                "Capture first-attempt assembly has no exact parent",
            )
            parent_identity = (
                first_top_identity
                if parent_instance == "top"
                else first_document_identity(parent["document_path"])
            )
            first_parents_by_document.setdefault(
                first_document_identity(item["document_path"]),
                set(),
            ).add(parent_identity)

        first_journal = first_attempt["rebuild_calls"]
        _require(
            isinstance(first_journal, list)
            and len(first_journal) >= 2
            and isinstance(first_journal[0], dict)
            and set(first_journal[0])
            == {"kind", "initial_pending", "rebuild_candidates"}
            and first_journal[0]["kind"] == "initial"
            and isinstance(first_journal[0]["initial_pending"], list)
            and first_journal[0]["initial_pending"]
            == sorted(set(first_journal[0]["initial_pending"]))
            and first_journal[0]["initial_pending"]
            and first_journal[0]["rebuild_candidates"]
            == sorted(set(first_journal[0]["rebuild_candidates"])),
            "Capture first-attempt journal has no exact dirty closure",
        )
        first_pending = {
            first_document_identity(path)
            for path in first_journal[0]["initial_pending"]
        }
        _require(
            first_top_identity not in first_pending,
            "Capture first-attempt top cannot be an initial pending candidate",
        )
        first_candidates = {
            first_document_identity(path)
            for path in first_journal[0]["rebuild_candidates"]
        }
        expected_candidates = set(first_pending)
        pending = list(first_pending)
        while pending:
            child = pending.pop()
            for parent in first_parents_by_document.get(child, set()):
                if (
                    parent == first_top_identity
                    or parent in expected_candidates
                ):
                    continue
                expected_candidates.add(parent)
                pending.append(parent)
        _require(
            first_pending <= set(first_assembly_rows)
            and first_candidates == expected_candidates,
            "Capture first-attempt candidates differ from the exact dirty ancestor closure",
        )
        sequence = 1
        for identity in sorted(
            first_candidates,
            key=lambda key: (-first_assembly_rows[key]["depth"], key),
        ):
            _require(
                sequence < len(first_journal) - 1,
                "Capture first-attempt journal ended before its candidates",
            )
            candidate = first_journal[sequence]
            group = first_assembly_rows[identity]
            _require(
                isinstance(candidate, dict)
                and set(candidate)
                == {
                    "sequence",
                    "kind",
                    "path",
                    "depth",
                    "required_configurations",
                    "state",
                    "decision",
                }
                and candidate["sequence"] == sequence
                and candidate["kind"] == "candidate"
                and first_document_identity(candidate["path"]) == identity
                and candidate["depth"] == group["depth"]
                and candidate["required_configurations"]
                == sorted(group["configurations"]),
                "Capture first-attempt candidate journal differs from its graph",
            )
            state = state_row(
                candidate["state"],
                "Capture first-attempt candidate state",
            )
            _require(
                first_document_identity(state["path"]) == identity
                and state["configuration"]
                in group["configurations"],
                "Capture first-attempt candidate changed document identity",
            )
            sequence += 1
            if state["needs_rebuild2_value"] in (1, 2):
                _require(
                    candidate["decision"] == "rebuild",
                    "Capture first-attempt pending candidate was not rebuilt",
                )
                _require(
                    sequence < len(first_journal) - 1,
                    "Capture first-attempt journal ended before a child call",
                )
                call = first_journal[sequence]
                _require(
                    isinstance(call, dict)
                    and set(call)
                    == {
                        "sequence",
                        "kind",
                        "phase",
                        "path",
                        "depth",
                        "state_before",
                        "result",
                        "state_after",
                        "elapsed_seconds",
                    }
                    and call["sequence"] == sequence
                    and call["kind"] == "child"
                    and call["phase"] == "child"
                    and first_document_identity(call["path"]) == identity
                    and call["depth"] == candidate["depth"]
                    and call["state_before"] == candidate["state"]
                    and call["result"] is True
                    and type(call["elapsed_seconds"]) in (int, float)
                    and call["elapsed_seconds"] >= 0,
                    "Capture first-attempt child-call journal is malformed",
                )
                child_after = state_row(
                    call["state_after"],
                    "Capture first-attempt child state_after",
                )
                _require(
                    first_document_identity(child_after["path"]) == identity
                    and child_after["configuration"]
                    == state["configuration"]
                    and child_after["read_only"] == state["read_only"]
                    and child_after["needs_rebuild2_value"] == 0
                    and child_after["configuration_needs_rebuild"] is False,
                    "Capture first-attempt child rebuild changed state or did not converge",
                )
                sequence += 1
            else:
                _require(
                    candidate["decision"] == "skip_clean",
                    "Capture first-attempt clean candidate was rebuilt",
                )
        _require(
            sequence < len(first_journal),
            "Capture first-attempt journal has no top failure",
        )
        failed_top = first_journal[sequence]
        _require(
            isinstance(failed_top, dict)
            and set(failed_top)
            == {
                "sequence",
                "kind",
                "phase",
                "path",
                "depth",
                "state_before",
                "result",
                "state_after",
                "elapsed_seconds",
                "error_code",
                "error_message",
            }
            and failed_top["sequence"] == sequence
            and failed_top["kind"] == "top"
            and failed_top["phase"] == "top"
            and _same_native_path(failed_top["path"], top_path)
            and failed_top["depth"] == 0
            and failed_top["result"] is True
            and failed_top["error_code"] == "cad_rebuild_incomplete"
            and failed_top["error_message"]
            == "Assembly rebuild did not end in a fully rebuilt configuration"
            and type(failed_top["elapsed_seconds"]) in (int, float)
            and failed_top["elapsed_seconds"] >= 0,
            "Capture first-attempt top failure is malformed",
        )
        before_top = state_row(
            failed_top["state_before"],
            "Capture first-attempt top state_before",
        )
        failed_state = state_row(
            failed_top["state_after"],
            "Capture first-attempt top state_after",
        )
        _require(
            _same_native_path(before_top["path"], top_path)
            and before_top["read_only"] is True
            and before_top["configuration"] == configuration
            and before_top["needs_rebuild2_value"] in (1, 2)
            and _same_native_path(failed_state["path"], top_path)
            and failed_state["configuration"] == configuration
            and failed_state["read_only"] is True
            and failed_state["needs_rebuild2_value"] in (1, 2)
            and failed_state["configuration_needs_rebuild"] is False,
            "Capture first-attempt top failure lacks the sticky state",
        )
        _require(
            sequence + 1 == len(first_journal),
            "Capture first-attempt journal has extra native calls",
        )
    def engineering_state_signature(state):
        return {
            "configuration": state["configuration"],
            "read_only": state["read_only"],
            "needs_rebuild2_value": state["needs_rebuild2_value"],
            "configuration_needs_rebuild": state["configuration_needs_rebuild"],
            "configurations": sorted(
                (
                    item["name"],
                    item["needs_rebuild"],
                )
                for item in state["configurations"]
            ),
        }

    def stable_document_key(key, document_type, bound_rows, label):
        scopes = {item["storage_scope"] for item in bound_rows}
        _require(len(scopes) == 1, f"{label} mixed file and session identity for one document")
        scope = next(iter(scopes))
        if scope == "file":
            stable_key = ("file", key)
        else:
            provenance = {item["provenance_state"] for item in bound_rows}
            owners = {item["owner_sha256"] for item in bound_rows}
            _require(
                len(provenance) == 1 and len(owners) == 1,
                f"{label} session document has ambiguous provenance",
            )
            stable_key = (
                "session",
                next(iter(provenance)),
                document_type,
                tuple(sorted(item["instance"] for item in bound_rows)),
                tuple(sorted(item["referenced_configuration"] for item in bound_rows)),
                tuple(sorted({normalized_path(item["owner_document_path"]) for item in bound_rows})),
                next(iter(owners)),
            )
            for item in bound_rows:
                owner_key = normalized_path(item["owner_document_path"])
                _require(
                    owner_key in before_sources
                    and before_sources[owner_key]["sha256"] == item["owner_sha256"],
                    f"{label} session owner bytes are not bound to the captured source",
                )
        return stable_key

    def document_snapshot(relative: str, label: str):
        payload = read_data(confined(root, "evidence/raw/" + relative))
        _require(
            isinstance(payload, dict)
            and set(payload) == {"document_count", "parts", "assemblies", "documents", "topology", "session"},
            f"{label} fields are incomplete or unknown",
        )
        documents = payload["documents"]
        _require(isinstance(documents, list) and bool(documents), f"{label} has no loaded documents")
        _require(type(payload["document_count"]) is int and payload["document_count"] == len(documents), (
            f"{label} document count differs from its rows"
        ))
        snapshot_session = session_evidence(payload["session"], label)
        _require(snapshot_session == prepared_session, f"{label} changed the controlled capture session root")
        snapshot_topology = topology_rows(payload["topology"], f"{label} topology", snapshot_session)
        verify_occurrence_owners(snapshot_topology, label)
        _require(
            topology_semantic(snapshot_topology) == topology_semantic(topology_before),
            f"{label} changed the semantic native topology",
        )
        rows = {}
        states = {}
        for item in documents:
            _require(
                isinstance(item, dict) and set(item) == {"path", "document_type", "state"},
                f"{label} document row is malformed",
            )
            _require(isinstance(item["path"], str) and bool(item["path"]), f"{label} document path is invalid")
            _require(
                type(item["document_type"]) is int and item["document_type"] in {1, 2},
                f"{label} document type is invalid",
            )
            key = normalized_path(item["path"])
            _require(key not in rows, f"{label} repeats a loaded document")
            rows[key] = item
            state = state_row(
                item["state"],
                f"{label} document state",
                controls_required=item["document_type"] == 2,
            )
            _require(
                _same_native_path(state["path"], item["path"]),
                f"{label} state names another document",
            )
            states[key] = (item["document_type"], state)
        active_topology = [
            item
            for item in snapshot_topology
            if item["storage_scope"] != "suppressed"
        ]
        topology_by_path = {}
        for item in active_topology:
            key = normalized_path(item["document_path"])
            topology_by_path.setdefault(key, []).append(item)
        _require(set(rows) == set(topology_by_path), f"{label} loaded-document set differs from native topology")
        for key, (_document_type, state) in states.items():
            required = {
                item["referenced_configuration"]
                for item in topology_by_path[key]
            }
            names = {item["name"] for item in state["configurations"]}
            _require(
                required <= names,
                f"{label} loaded document omits a referenced configuration: {state['path']}",
            )
            _require(
                state["needs_rebuild2_value"] == 0
                and state["configuration_needs_rebuild"] is False
                and all(
                    item["needs_rebuild"] is False
                    for item in state["configurations"]
                    if item["name"] in required
                ),
                f"{label} left a loaded document pending: {state['path']}",
            )
        expected_by_type = Counter(
            items[0]["document_type"]
            for items in topology_by_path.values()
        )
        _require(
            type(payload["assemblies"]) is int
            and type(payload["parts"]) is int
            and payload["assemblies"] == expected_by_type[2]
            and payload["parts"] == expected_by_type[1],
            f"{label} part and assembly counts differ from native topology",
        )
        signatures = {}
        for key, (document_type, state) in states.items():
            bound_rows = topology_by_path[key]
            _require(
                all(item["document_type"] == document_type for item in bound_rows),
                f"{label} loaded a document with conflicting native types: {state['path']}",
            )
            stable_key = stable_document_key(key, document_type, bound_rows, label)
            _require(stable_key not in signatures, f"{label} repeats a stable document identity")
            signatures[stable_key] = engineering_state_signature(state)
            if key == top_key:
                _require(state["read_only"] is True, f"{label} top assembly is not read-only")
            if document_type == 1:
                _require("controls" not in state, f"{label} part carries assembly controls")
        return signatures

    after_reads_states = document_snapshot("document_state_after_reads.json", "document_state_after_reads")
    after_pristine_states = document_snapshot(
        "document_state_after_pristine_readout.json",
        "document_state_after_pristine_readout",
    )
    _require(
        after_reads_states == after_pristine_states,
        "Capture final document states changed across the pristine read window",
    )
    baseline_states = {}
    for row in [*assembly_after, *part_rows.values()]:
        key = normalized_path(row["path"])
        bound_rows = [
            item for item in topology_after
            if item["storage_scope"] != "suppressed" and normalized_path(item["document_path"]) == key
        ]
        identity = stable_document_key(key, bound_rows[0]["document_type"], bound_rows, "Capture baseline")
        baseline_states[identity] = engineering_state_signature(row["state"])
    _require(
        set(baseline_states) == set(after_reads_states),
        "Read boundary differs from the complete rebuild baseline",
    )
    for key, signature in baseline_states.items():
        _require(
            after_reads_states[key] == signature,
            f"Loaded document state differs from its rebuild baseline: {key}",
        )

    parents_by_document = {}
    for item in not_top:
        parent_instance = "top" if item["depth"] == 1 else item["instance"].rsplit("/", 1)[0]
        parent = top_row if parent_instance == "top" else topology_by_instance.get(parent_instance)
        _require(
            parent is not None
            and parent.get("document_type") == 2
            and parent.get("depth") == item["depth"] - 1,
            f"Capture occurrence has no exact assembly parent: {item['instance']}",
        )
        parent_key = top_key if parent_instance == "top" else normalized_path(parent["document_path"])
        if item["document_type"] == 2:
            parents_by_document.setdefault(
                normalized_path(item["document_path"]),
                set(),
            ).add(parent_key)
    initial_pending = {
        key
        for key, row in before_rows.items()
        if key != top_key and row["state"]["needs_rebuild2_value"] in (1, 2)
    }
    rebuild_candidates = set(initial_pending)
    pending = list(initial_pending)
    while pending:
        child = pending.pop()
        for parent in parents_by_document.get(child, set()):
            if parent == top_key or parent in rebuild_candidates:
                continue
            rebuild_candidates.add(parent)
            pending.append(parent)

    child_rebuilds = preparation["child_rebuilds"]
    _require(isinstance(child_rebuilds, list), "child_rebuilds is not a list")
    child_paths = set()
    child_depths = []
    for item in child_rebuilds:
        _require(
            isinstance(item, dict)
            and set(item)
            == {
                "path",
                "depth",
                "phase",
                "instances",
                "elapsed_seconds",
                "result",
                "call_sequence",
                "state_before",
                "state_after",
            },
            "Child rebuild evidence is malformed",
        )
        _require(item["result"] is True, "Child rebuild did not return true")
        _require(item["phase"] == "child", "Child rebuild phase differs")
        _require(
            type(item["elapsed_seconds"]) in (int, float) and item["elapsed_seconds"] >= 0,
            "Child rebuild time is invalid",
        )
        key = normalized_path(item["path"])
        _require(key in before_rows and key != top_key, "Child rebuild names an unknown or top document")
        _require(key in rebuild_candidates, "Child rebuild lies outside the initial dirty ancestor closure")
        _require(key not in child_paths, "Child document was rebuilt more than once")
        child_paths.add(key)
        child_depths.append(item["depth"])
        _require(item["depth"] == before_rows[key]["depth"], "Child rebuild depth contradicts the graph")
        _require(
            sorted(item["instances"]) == sorted(before_rows[key]["instances"]),
            "Child rebuild instances contradict the graph",
        )
        state_before = state_row(item["state_before"], "Child state_before")
        state_after = state_row(item["state_after"], "Child state_after")
        _require(
            _same_native_path(state_before["path"], item["path"]),
            "Child state_before names another document",
        )
        _require(
            state_before["configuration"] == before_rows[key]["state"]["configuration"],
            "Child state_before changed the assembly configuration",
        )
        _require(
            _same_native_path(state_after["path"], item["path"])
            and state_after["configuration"] == state_before["configuration"],
            "Child state_after changed its document or configuration",
        )
        _require(
            state_before["needs_rebuild2_value"] in (1, 2)
            and state_after["needs_rebuild2_value"] == 0
            and state_after["configuration_needs_rebuild"] is False,
            "Child rebuild lacks converged before/after state",
        )
    _require(
        child_depths == sorted(child_depths, reverse=True),
        "Child rebuild evidence is not ordered child-first",
    )

    top_rebuild = preparation["top_rebuild"]
    rebuilt = bool(
        session_recovery is not None
        or child_rebuilds
        or top_rebuild is not None
    )
    _require(preparation["rebuild_performed"] is rebuilt, "rebuild_performed contradicts the invoked APIs")
    _require(
        preparation["used_api"] == (rebuild_api if rebuilt else no_rebuild_api),
        "Capture rebuild API identity differs",
    )
    _require(preparation["needs_rebuild2_before"] == before["needs_rebuild2_value"], "Top NeedsRebuild2 before differs")
    _require(preparation["needs_rebuild2_after"] == after["needs_rebuild2_value"], "Top NeedsRebuild2 after differs")
    _require(
        preparation["configuration_needs_rebuild_before"] == before["configuration_needs_rebuild"],
        "Top configuration NeedsRebuild before differs",
    )
    _require(
        preparation["configuration_needs_rebuild_after"] == after["configuration_needs_rebuild"],
        "Top configuration NeedsRebuild after differs",
    )
    _require(preparation["save_flag_before"] == before["save_flag"], "Top save flag before differs")
    _require(preparation["save_flag_after"] == after["save_flag"], "Top save flag after differs")
    if top_rebuild is not None:
        _require(
            isinstance(top_rebuild, dict)
            and set(top_rebuild)
            == {
                "path",
                "depth",
                "phase",
                "instances",
                "elapsed_seconds",
                "result",
                "call_sequence",
                "state_before",
                "state_after",
            },
            "Top rebuild evidence is malformed",
        )
        _require(_same_native_path(top_rebuild["path"], top_path), "Top rebuild names another document")
        _require(top_rebuild["result"] is True, "Top rebuild did not return true")
        expected_phase = (
            "top_recovery"
            if preparation["session_recovery"] is not None
            else "top"
        )
        _require(top_rebuild["phase"] == expected_phase, "Top rebuild phase differs")
        _require(top_rebuild["depth"] == 0 and top_rebuild["instances"] == ["top"], "Top rebuild identity changed")
        _require(
            top_rebuild["state_before"] == after_children,
            "Top rebuild did not use the refreshed child-complete state",
        )
        _require(top_rebuild["state_after"] == after, "Top rebuild final state contradicts the graph")
        _require(after_children["needs_rebuild2_value"] in (1, 2), "Top rebuilt although it was no longer pending")
    else:
        _require(after_children == after, "Top state changed without a recorded rebuild")
    if before["needs_rebuild2_value"] in (1, 2) and not rebuilt:
        raise PipelineError("Pending top state has no rebuild evidence")

    rebuild_calls = preparation["rebuild_calls"]
    _require(isinstance(rebuild_calls, list) and bool(rebuild_calls), "Rebuild call journal is missing")

    def verify_call(call, recorded_call, sequence, kind):
        _require(
            isinstance(call, dict)
            and set(call)
            == {
                "sequence",
                "kind",
                "phase",
                "path",
                "depth",
                "state_before",
                "result",
                "state_after",
                "elapsed_seconds",
            }
            and call["sequence"] == sequence
            and call["kind"] == kind
            and call["phase"] == recorded_call["phase"]
            and call["path"] == recorded_call["path"]
            and call["depth"] == recorded_call["depth"]
            and call["state_before"] == recorded_call["state_before"]
            and call["result"] is True
            and call["state_after"] == recorded_call["state_after"]
            and call["elapsed_seconds"]
            == recorded_call["elapsed_seconds"]
            and recorded_call["call_sequence"] == sequence,
            "Rebuild call journal differs from its recorded evidence",
        )

    child_calls = {normalized_path(item["path"]): item for item in child_rebuilds}

    def journal_row(sequence):
        _require(sequence < len(rebuild_calls), "Rebuild journal ended before its scheduled observation or call")
        return rebuild_calls[sequence]

    if session_recovery is None:
        _require(
            len(rebuild_calls)
            == len(rebuild_candidates)
            + len(child_rebuilds)
            + int(top_rebuild is not None)
            + 1,
            "Rebuild call journal differs from the recorded native calls",
        )
        initial_call = rebuild_calls[0]
        _require(
            isinstance(initial_call, dict)
            and set(initial_call)
            == {"kind", "initial_pending", "rebuild_candidates"}
            and initial_call["kind"] == "initial"
            and initial_call["initial_pending"] == sorted(initial_pending)
            and initial_call["rebuild_candidates"]
            == sorted(rebuild_candidates),
            "Rebuild journal does not bind the initial dirty ancestor closure",
        )
        sequence = 1
        for key in sorted(
            rebuild_candidates,
            key=lambda key: (-before_rows[key]["depth"], key),
        ):
            candidate = journal_row(sequence)
            row = before_rows[key]
            _require(
                isinstance(candidate, dict)
                and set(candidate)
                == {
                    "sequence",
                    "kind",
                    "path",
                    "depth",
                    "required_configurations",
                    "state",
                    "decision",
                }
                and candidate["sequence"] == sequence
                and candidate["kind"] == "candidate"
                and _same_native_path(candidate["path"], row["path"])
                and candidate["depth"] == row["depth"]
                and candidate["required_configurations"]
                == sorted(groups[key]["configurations"]),
                "Rebuild journal omits or reorders an initial dirty closure candidate",
            )
            fresh = state_row(candidate["state"], "Rebuild candidate state")
            _require(
                _same_native_path(fresh["path"], row["path"])
                and fresh["configuration"]
                == row["state"]["configuration"],
                "Rebuild candidate changed its document or configuration",
            )
            _require(
                groups[key]["configurations"]
                <= {item["name"] for item in fresh["configurations"]},
                "Rebuild candidate omits a required configuration",
            )
            sequence += 1
            if fresh["needs_rebuild2_value"] in (1, 2):
                _require(
                    candidate["decision"] == "rebuild"
                    and key in child_calls
                    and child_calls[key]["state_before"] == fresh,
                    "A pending candidate has no recorded child-first rebuild",
                )
                verify_call(
                    journal_row(sequence),
                    child_calls.pop(key),
                    sequence,
                    "child",
                )
                sequence += 1
            else:
                _require(
                    candidate["decision"] == "skip_clean"
                    and key not in child_calls
                    and fresh["configuration_needs_rebuild"] is False
                    and all(
                        item["needs_rebuild"] is False
                        for item in fresh["configurations"]
                        if item["name"] in groups[key]["configurations"]
                    ),
                    "A skipped rebuild candidate was not observed clean",
                )
        _require(
            not child_calls,
            "Child rebuild has no corresponding candidate observation",
        )
        if top_rebuild is not None:
            verify_call(
                journal_row(sequence),
                top_rebuild,
                sequence,
                "top",
            )
            sequence += 1
    else:
        _require(
            child_rebuilds == [],
            "A recovered final attempt performed child rebuilds",
        )
        _require(
            len(rebuild_calls)
            == 1 + len(rebuild_candidates) + int(top_rebuild is not None),
            "Recovery journal differs from its deferred top-only attempt",
        )
        initial_call = rebuild_calls[0]
        _require(
            isinstance(initial_call, dict)
            and set(initial_call)
            == {"kind", "initial_pending", "rebuild_candidates"}
            and initial_call["kind"] == "recovery_initial"
            and initial_call["initial_pending"] == sorted(initial_pending)
            and initial_call["rebuild_candidates"]
            == sorted(rebuild_candidates),
            "Recovery journal does not bind the fresh session graph",
        )
        sequence = 1
        for key in sorted(
            rebuild_candidates,
            key=lambda key: (-before_rows[key]["depth"], key),
        ):
            candidate = journal_row(sequence)
            row = before_rows[key]
            _require(
                isinstance(candidate, dict)
                and set(candidate)
                == {
                    "sequence",
                    "kind",
                    "path",
                    "depth",
                    "required_configurations",
                    "state",
                    "decision",
                }
                and candidate["sequence"] == sequence
                and candidate["kind"] == "candidate"
                and _same_native_path(candidate["path"], row["path"])
                and candidate["depth"] == row["depth"]
                and candidate["required_configurations"]
                == sorted(groups[key]["configurations"])
                and candidate["decision"] == "recovery_defer_to_top",
                "Recovery journal does not defer a fresh pending candidate",
            )
            state = state_row(candidate["state"], "Recovery candidate state")
            _require(
                _same_native_path(state["path"], row["path"])
                and state["configuration"]
                == row["state"]["configuration"],
                "Recovery candidate changed its document or configuration",
            )
            sequence += 1
        if top_rebuild is not None:
            verify_call(
                journal_row(sequence),
                top_rebuild,
                sequence,
                "top_recovery",
            )
            sequence += 1
    _require(
        sequence == len(rebuild_calls),
        "Rebuild journal contains an unscheduled native call",
    )

    return {
        "schema_version": schema,
        "document": preparation["document"],
        "configuration": preparation["configuration"],
        "needs_rebuild2_after": after["needs_rebuild2_value"],
        "configuration_needs_rebuild_after": after["configuration_needs_rebuild"],
        "read_only": after["read_only"],
        "rebuilt_document_count": len(child_rebuilds) + (1 if top_rebuild is not None else 0),
        "source_sha256": top_sha256,
        "component_count": len(not_top),
        "assembly_document_count": len(groups),
        "part_document_count": len(part_rows),
        "final_document_count": len(after_reads_states),
        "session_document_count": len(session_rows),
    }


def _same_native_path(left, right) -> bool:
    return bool(
        isinstance(left, str)
        and isinstance(right, str)
        and ntpath.normcase(ntpath.abspath(left)) == ntpath.normcase(ntpath.abspath(right))
    )


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
    """Delegate immutable native receipts to the transfer semantic validator."""

    from ..native_transfer import validate_native_receipts

    evidence = validate_native_receipts(Path(root), receipts)
    delivery = read_data(confined(root, "tool.json"))
    _require(
        evidence["native_tool"].get("version") == delivery.get("version")
        and evidence["native_tool"].get("source_sha256") == delivery.get("source_sha256"),
        "Native receipt does not bind this delivery's release identity",
    )
    return evidence


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
        _native_receipts_evidence(root, native_receipts)
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
    expected_comments = []
    for item in (model.get("provenance") or {}).get("value_warnings") or []:
        if not isinstance(item, dict) or not item.get("code"):
            continue
        objects = item.get("objects")
        for subject in objects if isinstance(objects, list) and objects else [{}]:
            detail = {"message": item.get("message", ""), "provenance": subject}
            payload = json.dumps(detail, ensure_ascii=True, sort_keys=True).replace("--", r"\u002d\u002d")
            code = str(item["code"]).replace("--", "- -")
            expected_comments.append(f"WARNING {code}: {payload} See reports/quality.json.")
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
        fixed_joint_name(str(frame["parent"]), str(frame["name"])) for frame in model["frames"]
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


def _joint(node, authored, canonical, datums, body_datums, diagnostics, *, preview=False):
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
    limit_source = authored.get("limit_source")
    _require(
        limit_source in {"native", "declared_author", "preview_policy", "default_policy"},
        "Moving joint limits are not bound to a CAD native or author declaration",
    )
    if limit_source == "preview_policy":
        policy = authored.get("preview_policy")
        _require(
            preview and authored["type"] == "revolute" and isinstance(policy, dict),
            "The preview window is not allowed for this purpose or joint type",
        )
        _require(
            policy.get("rule_id") == PREVIEW_POLICY_ID
            and policy.get("rule_version") == "v1"
            and policy.get("native_cad_ranges") is False
            and policy.get("hardware_qualified") is False,
            "The preview window has another rule or qualification",
        )
        _require(
            limits.get("lower") == -PREVIEW_WINDOW_RAD and limits.get("upper") == PREVIEW_WINDOW_RAD,
            "The inspection-preview window does not equal the frozen full-turn radius",
        )
    elif limit_source == "default_policy":
        policy = authored.get("default_policy")
        _require(
            not preview and authored["type"] == "revolute" and isinstance(policy, dict),
            "The engineering default window is not allowed for this purpose or joint type",
        )
        _require(
            policy.get("rule_id") == ENGINEERING_DEFAULT_POLICY_ID
            and policy.get("rule_version") == "v1"
            and policy.get("native_cad_ranges") is False
            and policy.get("hardware_qualified") is False
            and policy.get("engineering_position_range_qualified") is False,
            "The engineering default window has another rule or qualification",
        )
        _require(
            policy.get("native_absence")
            in {"complete_mate_inventory_no_enabled_limit", "native_limit_observation_unreadable"},
            "The engineering default window has no native-absence evidence",
        )
        _require(
            isinstance(policy.get("native_observation_errors"), list),
            "The engineering default window hid native observation errors",
        )
        _require(
            limits.get("lower") == -PREVIEW_WINDOW_RAD and limits.get("upper") == PREVIEW_WINDOW_RAD,
            "The engineering default window does not equal the frozen full-turn radius",
        )
    provenance = canonical.get("provenance") if isinstance(canonical.get("provenance"), dict) else {}

    def require_record_binding(source_key, model_key, fields, *, selected):
        source_evidence = authored.get(source_key)
        model_evidence = provenance.get(model_key)
        _require(
            isinstance(source_evidence, dict)
            and source_evidence == model_evidence
            and source_evidence.get("kind") in {"controlled_record", "native_declaration"}
            and re.fullmatch(r"[0-9a-f]{64}", str(source_evidence.get("sha256") or "")) is not None
            and all(
                isinstance(source_evidence.get(key), str) and bool(source_evidence[key])
                for key in ("file", "anchor", "key", "reference")
            ),
            f"Joint {model_key} does not bind the exact embedded controlled record",
        )
        declared = source_evidence.get("declared_values")
        _require(
            isinstance(declared, dict)
            and set(declared) == set(fields)
            and (
                not selected
                or all(declared[field] == limits[field] for field in fields)
            ),
            f"Joint {model_key} disagrees with its affected fields",
        )
        if source_key == "limit_evidence":
            _require(
                isinstance(source_evidence.get("native_observation_errors"), list),
                "Controlled author limits hid native observation errors",
            )

    if (
        limit_source == "declared_author" and authored["type"] != "continuous"
    ) or authored.get("limit_evidence") is not None:
        require_record_binding(
            "limit_evidence",
            "limits_evidence",
            ("lower", "upper"),
            selected=limit_source == "declared_author",
        )
    _require(
        authored.get("drive_source") != "controlled" or authored.get("drive_evidence") is not None,
        "Controlled drive is missing its embedded-record evidence",
    )
    if authored.get("drive_evidence") is not None:
        require_record_binding(
            "drive_evidence",
            "drive_evidence",
            ("effort", "velocity"),
            selected=True,
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
        _verify_native_solid_orientation(source_bytes, row)
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


def _verify_native_solid_orientation(source_bytes: bytes, row: dict) -> None:
    """Check orientation only on native bodies whose edge incidence proves closure."""

    tessellation = row.get("tessellation")
    if not isinstance(tessellation, dict):
        return
    spans = tessellation.get("face_spans")
    native_edges = tessellation.get("native_edges")
    if not isinstance(spans, list) or not isinstance(native_edges, list):
        return
    solid_bodies = sorted(
        {
            span["body_index"]
            for span in spans
            if span.get("body_type") == 0
        }
    )
    for body_index in solid_bodies:
        body_edges = [
            edge
            for edge in native_edges
            if edge.get("body_type") == 0 and edge.get("body_index") == body_index
        ]
        triangles = []
        for span in spans:
            if span.get("body_type") != 0 or span.get("body_index") != body_index:
                continue
            for triangle_index in range(
                span["triangle_start"],
                span["triangle_start"] + span["triangle_count"],
            ):
                offset = 84 + 50 * triangle_index + 12
                values = struct.unpack_from("<9f", source_bytes, offset)
                triangles.append(tuple(tuple(values[index : index + 3]) for index in range(0, 9, 3)))
        directed: dict[tuple, list[int]] = {}
        points = []
        point_index = {}
        faces = []
        for triangle in triangles:
            keys = []
            for point in triangle:
                index = point_index.get(point)
                if index is None:
                    index = len(points)
                    point_index[point] = index
                    points.append(point)
                keys.append(index)
            faces.append(tuple(keys))
            for left, right in ((keys[0], keys[1]), (keys[1], keys[2]), (keys[2], keys[0])):
                key = tuple(sorted((left, right)))
                directed.setdefault(key, []).append(1 if left < right else -1)
        _require(
            all(
                len(directions) != 2
                or directions[0] != directions[1]
                for directions in directed.values()
            ),
            "Native solid faces contain same-oriented shared triangle edges",
        )
        if not body_edges or any(
            len(edge.get("face_indices") or []) != 2
            for edge in body_edges
        ):
            continue
        adjacency = {}
        for triangle_index, face in enumerate(faces):
            for vertex in face:
                adjacency.setdefault(vertex, set()).add(triangle_index)
        pending = set(range(len(faces)))
        components = []
        while pending:
            stack = [pending.pop()]
            component = []
            while stack:
                triangle_index = stack.pop()
                component.append(triangle_index)
                for vertex in faces[triangle_index]:
                    for neighbor in adjacency[vertex]:
                        if neighbor in pending:
                            pending.remove(neighbor)
                            stack.append(neighbor)
            signed_volume = 0.0
            weighted_center = [0.0, 0.0, 0.0]
            sample = None
            sample_area = -1.0
            for triangle_index in component:
                a, b, c = (
                    tuple(float(value) for value in points[index])
                    for index in faces[triangle_index]
                )
                volume = (
                    a[0] * (b[1] * c[2] - b[2] * c[1])
                    - a[1] * (b[0] * c[2] - b[2] * c[0])
                    + a[2] * (b[0] * c[1] - b[1] * c[0])
                ) / 6.0
                signed_volume += volume
                for axis in range(3):
                    weighted_center[axis] += volume * (a[axis] + b[axis] + c[axis]) / 4.0
                normal = np.cross(np.asarray(b) - np.asarray(a), np.asarray(c) - np.asarray(a))
                area = float(np.linalg.norm(normal))
                if area > sample_area and area > 0.0:
                    triangle_center = np.asarray(
                        tuple((a[axis] + b[axis] + c[axis]) / 3.0 for axis in range(3))
                    )
                    edge_scale = min(
                        float(np.linalg.norm(np.asarray(b) - np.asarray(a))),
                        float(np.linalg.norm(np.asarray(c) - np.asarray(b))),
                        float(np.linalg.norm(np.asarray(a) - np.asarray(c))),
                    )
                    sample_area = area
                    sample = (
                        triangle_center,
                        normal / area,
                        max(edge_scale * 1.0e-6, 1.0e-12),
                    )
            if abs(signed_volume) <= 1e-18:
                continue
            center = tuple(value / signed_volume for value in weighted_center)
            triangle_center, unit_normal, epsilon = sample
            interior_sample = tuple(
                float(
                    triangle_center[axis]
                    - (1.0 if signed_volume > 0 else -1.0) * epsilon * unit_normal[axis]
                )
                for axis in range(3)
            )
            components.append(
                {
                    "triangles": component,
                    "signed_volume": signed_volume,
                    "center": center,
                    "interior_sample": interior_sample,
                }
            )
        _require(
            components and sum(component["signed_volume"] for component in components) > 0,
            "Closed native solid has no positive total material volume",
        )
        for component_index, component in enumerate(components):
            nesting_depth = sum(
                _point_inside_triangle_component(
                    component["interior_sample"],
                    points,
                    faces,
                    other["triangles"],
                )
                for other_index, other in enumerate(components)
                if other_index != component_index
            )
            expected_positive = nesting_depth % 2 == 0
            _require(
                (component["signed_volume"] > 0) == expected_positive,
                "Closed native solid shell orientation differs from its nesting depth",
            )


def _point_inside_triangle_component(point, points, faces, component) -> bool:
    """Solid-angle membership used only to distinguish outer shells from valid cavities."""

    origin = np.asarray(point, dtype=float)
    solid_angle = 0.0
    for triangle_index in component:
        a, b, c = (
            np.asarray(points[index], dtype=float) - origin
            for index in faces[triangle_index]
        )
        numerator = float(np.dot(a, np.cross(b, c)))
        denominator = (
            float(np.linalg.norm(a) * np.linalg.norm(b) * np.linalg.norm(c))
            + float(np.dot(a, b)) * float(np.linalg.norm(c))
            + float(np.dot(b, c)) * float(np.linalg.norm(a))
            + float(np.dot(c, a)) * float(np.linalg.norm(b))
        )
        if abs(denominator) <= 1e-18:
            continue
        solid_angle += 2.0 * math.atan2(numerator, denominator)
    return abs(solid_angle) > 2.0 * math.pi


def native_gate_warning_diagnostics(details: dict) -> list[dict]:
    """Project verified native-gate warnings into the quality report."""

    warnings = details.get("warnings")
    _require(isinstance(warnings, list), "Native tessellation warnings are unreadable")
    projected = []
    for warning in warnings:
        _require(
            isinstance(warning, dict) and isinstance(warning.get("code"), str),
            "Native tessellation warning has no code",
        )
        projected.append(native_scope_warning_projection(warning))
    scope_projection = [
        item
        for item in projected
        if item["code"] in NATIVE_SCOPE_IDENTITY_WARNING_CODES
    ]
    _require(
        native_scope_warning_details_match(scope_projection, warnings),
        "Native scope warning projection is incomplete, forged or mismatched",
    )
    return projected


def evaluate_bundle(
    root: Path,
    *,
    native_receipts=None,
    require_native_receipts: bool = False,
) -> dict:
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

        def _verify_native_tessellation():
            native_details = verify_native_mesh_observations(root)
            projected = native_gate_warning_diagnostics(native_details)
            diagnostics.extend(projected)
            return native_details

        native_geometry = gates.add(
            "geometry.native_tessellation",
            _verify_native_tessellation,
        )
        gates.add("source.coverage", lambda: _entities(source, raw, model))
        gates.add(
            "verification.complete",
            lambda: _verify_model(
                root,
                gates,
                definition,
                model,
                raw,
                diagnostics,
                manifest,
                native_geometry=native_geometry,
                native_receipts=native_receipts,
                require_native_receipts=require_native_receipts,
            ),
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
    return (
        "established"
        if all(
            key in limits
            and type(limits[key]) in (int, float)
            and math.isfinite(limits[key])
            and limits[key] > 0
            for key in ("effort", "velocity")
        )
        else "unqualified"
    )


def _mass_closure_equality(root, definition=None):
    from .solidworks_physics import verify_urdf_mass_equality

    return verify_urdf_mass_equality(root, (definition or {}).get("source"))


def _verify_model(
    root,
    gates,
    definition,
    model,
    raw,
    diagnostics,
    manifest,
    *,
    native_geometry=None,
    native_receipts=None,
    require_native_receipts=False,
):
    from .solidworks_physics import verify_physics

    source = definition["source"]
    carrier = source.get("carrier")
    native_geometry_rows = evidence.geometry(root)
    metadata_warnings = _metadata_value_warnings(
        root,
        source,
        manifest,
        native_receipts=native_receipts,
        require_native_receipts=require_native_receipts,
    )
    gates.add("model.policy", lambda: _policy(source, model))
    gates.add("source.carrier_binding", lambda: _carrier_binding(source, carrier))
    gates.add(
        "provenance.value_warnings",
        lambda: _value_warning_gate(
            root,
            source,
            model,
            carrier,
            native_geometry_rows=native_geometry_rows,
            metadata_warnings=metadata_warnings,
        ),
    )
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
    selector_sources = {}
    for joint in source["joints"]:
        selector = joint.get("axis_reference")
        if not isinstance(selector, dict):
            continue
        key = (
            str(selector.get("kind") or ""),
            str(selector.get("component") or ""),
            str(selector.get("feature_name") or ""),
        )
        _require(key not in selector_sources, "Native axis selectors are not unique")
        selector_sources[key] = joint["name"]
    axis_map = {}
    for row in axes:
        selector = row.get("selector") if isinstance(row.get("selector"), dict) else {}
        key = (
            str(selector.get("kind") or ""),
            str(selector.get("component") or ""),
            str(selector.get("feature_name") or ""),
        )
        name = joint_sources.get(str(row.get("joint"))) or selector_sources.get(key)
        _require(isinstance(name, str) and name, "Native axis row has no unique joint binding")
        _require(name not in axis_map, "Native axis rows repeat one joint binding")
        axis_map[name] = row
    preview = _request_purpose(root) == PURPOSE_INSPECTION_PREVIEW
    for authored in source["joints"]:
        name = authored["name"]
        gates.add(
            "joints." + name,
            lambda a=authored: _joint(
                joints[a["name"]],
                a,
                model_joints[a["name"]],
                datums,
                body_datums,
                diagnostics,
                preview=preview,
            ),
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
    geometry_rows = native_geometry_rows
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


def _metadata_value_warnings(
    root,
    source,
    manifest,
    *,
    native_receipts=None,
    require_native_receipts=False,
):
    if not isinstance(manifest, dict):
        raise PipelineError("Delivery manifest is unavailable for metadata provenance")
    records = manifest.get("subject", {}).get("files", [])
    digests = {
        record.get("path"): record.get("sha256")
        for record in records
        if isinstance(record, dict) and isinstance(record.get("path"), str)
    }
    try:
        runtime = evidence.runtime(root)
        rewrite = read_data(confined(root, REWRITE_ENVIRONMENT_EVIDENCE))
        receipts = native_receipts if native_receipts is not None else load_native_receipts(root)
        if receipts is not None:
            native_receipt = _native_receipts_evidence(root, receipts)
            native_tool = native_receipt["native_tool"]
        elif require_native_receipts:
            raise PipelineError("Native receipt bytes are required at this verification boundary")
        else:
            native_tool = runtime.get("toolchain")
            _require(
                isinstance(native_tool, dict),
                "Self-contained delivery carries no native tool identity",
            )
        closure = evidence.dependency_closure(root)
        return metadata_value_warnings(
            runtime,
            rewrite,
            expected_tool=native_tool,
            runtime_sha256=digests[RUNTIME_EVIDENCE],
            rewrite_sha256=digests[REWRITE_ENVIRONMENT_EVIDENCE],
            expected_configuration=str(source.get("configuration") or ""),
            closure_top_level=str(closure.get("top_level") or ""),
        )
    except (KeyError, OSError, TypeError) as error:
        raise PipelineError("Metadata evidence is not fully bound by the delivery manifest") from error


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


def _carrier_binding(source, carrier):
    """Bind delivered names, members and topology to the raw-decoded carrier."""

    _require(isinstance(carrier, dict), "The raw carrier snapshot is missing")
    bodies = source.get("bodies") or []
    joints = source.get("joints") or []
    native = {str(body.get("source_name") or body["name"]): body for body in bodies}
    expected = {str(body["name"]): body for body in carrier.get("bodies") or []}
    _require(
        bool(expected) and set(native) == set(expected),
        "Delivered bodies differ from the raw carrier membership",
    )
    for name, body in native.items():
        original = expected[name]
        _require(
            list(body.get("components") or []) == list(original.get("components") or []),
            f"Body {name} components differ from the raw carrier",
        )
        _require(
            str((body.get("frame") or {}).get("coordinate_system") or "")
            == str((original.get("frame") or {}).get("coordinate_system") or ""),
            f"Body {name} datum differs from the raw carrier",
        )
    output_to_native = {
        str(body["name"]): str(body.get("source_name") or body["name"]) for body in bodies
    }
    delivered = {str(joint.get("source_name") or joint["name"]): joint for joint in joints}
    expected_joints = {str(joint["name"]): joint for joint in carrier.get("joints") or []}
    _require(
        set(delivered) == set(expected_joints),
        "Delivered joints differ from the raw carrier membership",
    )
    for name, joint in delivered.items():
        original = expected_joints[name]
        _require(joint.get("type") == original.get("type"), f"Joint {name} type differs from the raw carrier")
        _require(
            output_to_native.get(str(joint.get("parent") or ""), str(joint.get("parent") or ""))
            == str(original.get("parent") or ""),
            f"Joint {name} parent differs from the raw carrier",
        )
        _require(
            output_to_native.get(str(joint.get("child") or ""), str(joint.get("child") or ""))
            == str(original.get("child") or ""),
            f"Joint {name} child differs from the raw carrier",
        )
    root = carrier.get("root")
    if root is not None:
        base = next((body for body in bodies if body.get("name") == "base_link"), None)
        _require(base is not None, "The raw carrier root is not delivered as base_link")
        _require(
            str(base.get("source_name") or base["name"]) == str(root),
            "The delivered base_link does not bind to the raw carrier root",
        )
    return {"bodies": len(native), "joints": len(delivered)}


def _value_warning_gate(
    root,
    source,
    model,
    carrier=None,
    *,
    native_geometry_rows=None,
    metadata_warnings=None,
):
    """Re-derive every non-native value warning from source facts and the model."""

    from ..sources.solidworks.naming import (
        DISPLAY_MATERIAL_FALLBACK_NAME,
        DISPLAY_MATERIAL_FALLBACK_RGBA,
        DISPLAY_MATERIAL_FALLBACK_RULE,
        asset_namespace,
        carrier_names_and_rules,
        mesh_path,
        visual_name,
    )

    mesh_namespace = asset_namespace(Path(str(source.get("assembly") or "robot")).name)
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
    carrier_signs: dict[str, int] = {}
    for row in (carrier or {}).get("joints") or []:
        sign = row.get("axis_sign")
        if sign is None:
            continue
        name = str(row["name"])
        if name in carrier_signs and carrier_signs[name] != sign:
            raise PipelineError(f"duplicate joint name {name} carries conflicting author signs")
        carrier_signs[name] = sign
    links = model.get("links") or []
    model_joints = model.get("joints") or []
    shapes = [(link, shape) for link in links for shape in link.get("visuals") or []]
    native_geometry = {}
    for row in native_geometry_rows or []:
        _require(isinstance(row, dict), "Raw geometry rows must be objects")
        component = str(row.get("component") or "")
        _require(component and component not in native_geometry, "Raw geometry components are not unique")
        _require(
            not ({"material", "rgba", "appearance", "color", "colour"} & set(row)),
            "The capture schema carries no independently verified CAD appearance",
        )
        native_geometry[component] = row

    # Re-derive the canonical names and their provenance from the native
    # topology.  A source record's own name_rule claim is never trusted: the
    # delivered names must be exactly what the shared pure rule emits, and the
    # warning metadata is computed from the same rule, not copied from source.
    output_to_native = {
        str(body["name"]): str(body.get("source_name") or body["name"]) for body in bodies
    }
    carrier_axes = {
        str(row["name"]): row.get("axis_assembly_direction")
        for row in (carrier or {}).get("joints") or []
        if row.get("axis_assembly_direction") is not None
    }
    native_bodies = [
        {
            "name": str(body.get("source_name") or body["name"]),
            "frame": body.get("frame") or {},
            "components": body.get("components"),
            "owners": body.get("owners"),
            "datum": body.get("datum"),
        }
        for body in bodies
    ]
    native_joints = [
        {
            "name": str(joint.get("source_name") or joint["name"]),
            "type": joint.get("type"),
            "parent": output_to_native.get(
                str(joint.get("parent") or ""), str(joint.get("parent") or "")
            ),
            "child": output_to_native.get(
                str(joint.get("child") or ""), str(joint.get("child") or "")
            ),
            "axis": {
                "assembly_direction": carrier_axes.get(
                    str(joint.get("source_name") or joint["name"])
                )
            },
        }
        for joint in joints
    ]
    native_root = next(
        (
            str(body.get("source_name") or body["name"])
            for body in bodies
            if body.get("name") == "base_link"
        ),
        "",
    )
    rederived_bodies, rederived_joints, body_rules, joint_rules = carrier_names_and_rules(
        native_bodies, native_joints, native_root
    )
    for body in bodies:
        native = str(body.get("source_name") or body["name"])
        _require(
            rederived_bodies.get(native) == body.get("name"),
            "A link name is not the deterministic CAD-derived rule output",
        )
    for joint in joints:
        native = str(joint.get("source_name") or joint["name"])
        _require(
            rederived_joints.get(native) == joint.get("name"),
            "A joint name is not the deterministic CAD-derived rule output",
        )

    expected: dict[str, list[dict]] = {}
    declared_author_limits = []
    controlled_drive = []
    unqualified_drive = []
    evidence_owners: dict[tuple[str, str, str, str, str], str] = {}

    def add(code, value):
        expected.setdefault(code, []).append(value)

    for warning in metadata_warnings or []:
        _require(
            isinstance(warning, dict)
            and warning.get("code") == METADATA_WARNING_CODE
            and warning.get("message") == METADATA_WARNING_MESSAGE
            and isinstance(warning.get("objects"), list)
            and bool(warning["objects"]),
            "Re-derived metadata provenance warning is malformed",
        )
        for object_row in warning["objects"]:
            add(warning["code"], object_row)

    for body in bodies:
        source_name = str(body.get("source_name") or body["name"])
        if source_name != body["name"]:
            rule = body_rules.get(source_name) or {}
            add(
                "naming.formatted_interface",
                {
                    "object_type": "link",
                    "object": body["name"],
                    "field": "name",
                    "actual": body["name"],
                    "source": f"CAD-derived interface naming for {source_name}",
                    "original": source_name,
                    "rule_id": rule.get("rule_id", "naming.interface"),
                    "rule_version": rule.get("rule_version", "naming-v1"),
                    "reason": rule.get("reason", "CAD datum formatting"),
                },
            )
    for joint in joints:
        source_name = str(joint.get("source_name") or joint["name"])
        if source_name != joint["name"]:
            rule = joint_rules.get(source_name) or {}
            add(
                "naming.formatted_interface",
                {
                    "object_type": "joint",
                    "object": joint["name"],
                    "field": "name",
                    "actual": joint["name"],
                    "source": f"CAD-derived interface naming for {source_name}",
                    "original": source_name,
                    "rule_id": rule.get("rule_id", "naming.interface"),
                    "rule_version": rule.get("rule_version", "naming-v1"),
                    "reason": rule.get("reason", "CAD reference-axis formatting"),
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
        provenance = joint.get("axis_provenance")
        native_name = str(joint.get("source_name") or joint["name"])
        model_joint = next(
            (item for item in model_joints if item.get("name") == joint.get("name")),
            None,
        )
        if carrier_signs.get(native_name) == -1:
            _require(isinstance(provenance, dict), "Author axis_sign reversal has no source provenance")
            _require(
                provenance.get("native_sign") == -1
                and provenance.get("sign_reversal") is True
                and provenance.get("sign_warning") == "discovery.direction_sign_reversed",
                "Author axis_sign reversal provenance is missing or inconsistent",
            )
            _require(model_joint is not None, "A canonical joint is missing from the model")
            native_axis = provenance.get("native_direction")
            output_axis = provenance.get("direction")
            _require(
                isinstance(native_axis, list)
                and isinstance(output_axis, list)
                and len(native_axis) == 3
                and len(output_axis) == 3
                and all(
                    abs(float(before) + float(after)) <= 1e-12
                    for before, after in zip(native_axis, output_axis, strict=True)
                ),
                "Author axis_sign reversal does not reverse the native shaft",
            )
            add(
                "direction.author_sign_applied",
                {
                    "object_type": "joint",
                    "object": joint["name"],
                    "field": "axis",
                    "actual": model_joint.get("axis"),
                    "source": "CAD author dp.joint.axis_sign",
                    "original": native_axis,
                    "rule_id": provenance.get("rule_id", "direction.native_axis_sign"),
                    "rule_version": provenance.get("rule_version", "v1"),
                    "reason": (
                        "CAD author axis_sign selects the positive direction of the "
                        "undirected shaft; q and ranges remain in author q"
                    ),
                    "native_joint_identity": native_name,
                    "native_sign": provenance.get("native_sign"),
                    "sign_reversal": provenance.get("sign_reversal"),
                    "sign_warning": provenance.get("sign_warning"),
                    "original_axis_assembly": native_axis,
                    "output_axis_assembly": output_axis,
                    "actual_child_axis": model_joint.get("axis"),
                    "original_frame": provenance.get("original_frame", "assembly"),
                    "output_frame": provenance.get("output_frame", "assembly"),
                    "model_frame": "child",
                    "q_sign": 1,
                    "range_transform": "not_applied",
                    "limits_before": joint.get("limits"),
                    "limits_after": joint.get("limits"),
                    "drive_before": joint.get("drive"),
                    "drive_after": joint.get("drive"),
                    "limits_status": "present" if joint.get("limits") is not None else "absent",
                    "drive_status": "present" if joint.get("drive") is not None else "absent",
                    "warning_does_not_authorize": True,
                    "warning_does_not_replace_evidence": True,
                },
            )
        if isinstance(provenance, dict):
            _require(model_joint is not None, "A canonical joint is missing from the model")
            row = {
                "object_type": "joint",
                "object": joint["name"],
                "field": "axis",
                "actual": model_joint.get("axis"),
                "source": "CAD signed axis and versioned positive-motion rule",
                "original": provenance.get("parent_axis"),
                "rule_id": provenance.get("rule_id", "direction.positive_semantics"),
                "rule_version": provenance.get("rule_version", "v3"),
                "reason": provenance.get(
                    "reason", provenance.get("direction_relation", provenance.get("status"))
                ),
                "motion": provenance.get("motion"),
            }
            if provenance.get("status", provenance.get("direction_relation")) != "local_class_aligned":
                add("direction.semantics_unresolved", row)
        if joint.get("limit_source") == "preview_policy":
            policy = joint.get("preview_policy") or {}
            add(
                "limits.preview_policy",
                {
                    "object_type": "joint",
                    "object": joint["name"],
                    "field": "limits",
                    "actual": joint["limits"],
                    "source": "inspection preview full-turn policy",
                    "original": policy.get("author_limits"),
                    "rule_id": PREVIEW_POLICY_ID,
                    "rule_version": "v1",
                    "reason": "inspection preview window; no native mechanical range is claimed",
                    "native_cad_ranges": False,
                    "hardware_qualified": False,
                },
            )
        elif joint.get("limit_source") == "default_policy":
            policy = joint.get("default_policy") or {}
            add(
                "limits.default_policy",
                {
                    "object_type": "joint",
                    "object": joint["name"],
                    "field": "limits",
                    "actual": joint["limits"],
                    "source": "engineering default rotation window policy",
                    "original": policy.get("author_limits"),
                    "rule_id": ENGINEERING_DEFAULT_POLICY_ID,
                    "rule_version": "v1",
                    "reason": (
                        "engineering analysis default window; no native mechanical range or engineering "
                        "position-range qualification is claimed"
                    ),
                    "native_cad_ranges": False,
                    "hardware_qualified": False,
                    "engineering_position_range_qualified": False,
                    "native_absence": policy.get("native_absence"),
                    "native_observation_errors": policy.get("native_observation_errors"),
                },
            )
        limit_evidence = joint.get("limit_evidence")
        drive_evidence = joint.get("drive_evidence")
        limit_evidence = limit_evidence if isinstance(limit_evidence, dict) else {}
        drive_evidence = drive_evidence if isinstance(drive_evidence, dict) else {}
        model_limits = (model_joint or {}).get("limits")
        model_limits = model_limits if isinstance(model_limits, dict) else {}
        model_provenance = (
            model_joint.get("provenance") if isinstance((model_joint or {}).get("provenance"), dict) else {}
        )

        def observe_evidence(
            evidence,
            fields,
            *,
            selected,
            limits=model_limits,
            joint_name=joint["name"],
        ):
            declared = evidence.get("declared_values")
            _require(
                isinstance(declared, dict)
                and set(declared) == set(fields)
                and (
                    not selected
                    or (
                        all(field in limits for field in fields)
                        and all(declared[field] == limits[field] for field in fields)
                    )
                ),
                "A controlled record no longer binds its delivered joint fields",
            )
            _require(
                evidence.get("kind") in {"controlled_record", "native_declaration"},
                "Controlled evidence has no recognized source kind",
            )
            identity = tuple(
                str(evidence.get(key) or "")
                for key in ("file", "sha256", "anchor", "key", "reference")
            )
            _require(all(identity), "Controlled record provenance lacks file, sha, anchor, or key")
            if evidence.get("kind") == "controlled_record":
                previous = evidence_owners.setdefault(identity, joint_name)
                _require(
                    previous == joint_name,
                    "One controlled record entry is reused across joint identities",
                )

        if joint.get("limit_source") == "declared_author":
            if joint.get("type") != "continuous":
                _require(
                    model_provenance.get("limits_evidence") == limit_evidence,
                    "Model limits evidence is missing, foreign, or differs from the controlled record",
                )
                observe_evidence(limit_evidence, ("lower", "upper"), selected=True)
            declared = limit_evidence.get("declared_values") or {}
            for field in (() if joint.get("type") == "continuous" else ("lower", "upper")):
                declared_author_limits.append(
                    {
                        "object_type": "joint",
                        "object": joint["name"],
                        "field": field,
                        "actual": model_limits[field],
                        "source": "controlled author dp.joint.limits_record",
                        "original": declared[field],
                        "file": limit_evidence.get("file"),
                        "sha256": limit_evidence.get("sha256"),
                        "anchor": limit_evidence.get("anchor"),
                        "key": limit_evidence.get("key"),
                        "reference": limit_evidence.get("reference"),
                        "native_observation_errors": limit_evidence.get("native_observation_errors") or [],
                        "rule_id": "limits.declared_author",
                        "rule_version": "v1",
                        "reason": "controlled author position range is not a native CAD reading",
                    }
                )
        elif limit_evidence:
            _require(
                model_provenance.get("limits_evidence") == limit_evidence,
                "Model native-selected limits evidence is missing, foreign, or forged",
            )
            observe_evidence(limit_evidence, ("lower", "upper"), selected=False)
        _require(
            joint.get("drive_source") != "controlled" or bool(drive_evidence),
            "Controlled drive evidence is missing from the independent source",
        )
        if drive_evidence:
            _require(
                model_provenance.get("drive_evidence") == drive_evidence,
                "Model drive evidence is missing, foreign, or differs from the controlled record",
            )
            observe_evidence(drive_evidence, ("effort", "velocity"), selected=True)
            declared = drive_evidence.get("declared_values") or {}
            for field in ("effort", "velocity"):
                row = {
                    "object_type": "joint",
                    "object": joint["name"],
                    "field": field,
                    "actual": model_limits[field],
                    "source": "controlled author dp.joint.drive_record",
                    "original": declared[field],
                    "file": drive_evidence.get("file"),
                    "sha256": drive_evidence.get("sha256"),
                    "anchor": drive_evidence.get("anchor"),
                    "key": drive_evidence.get("key"),
                    "reference": drive_evidence.get("reference"),
                    "rule_id": "drive.declared_author",
                    "rule_version": "v1",
                    "reason": "controlled author drive value is not a native CAD reading",
                }
                controlled_drive.append(row)
                if model_limits[field] == 0:
                    unqualified_drive.append(
                        {
                            **row,
                            "rule_id": "drive.nonzero_required",
                            "reason": (
                                "zero effort or velocity is retained but does not qualify drive sizing"
                            ),
                        }
                    )
    if declared_author_limits:
        for row in declared_author_limits:
            add("limits.declared_author", row)
    if controlled_drive:
        for row in controlled_drive:
            add("drive.declared_author", row)
    if unqualified_drive:
        for row in unqualified_drive:
            add("drive.unqualified", row)
    for frame in source.get("frames", []):
        source_name = str(frame.get("source_name") or "")
        native_suffix = str(frame.get("coordinate_system") or "").partition("_")[2]
        if source_name and native_suffix != frame["name"]:
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
        joint_name = fixed_joint_name(str(frame.get("parent") or ""), str(frame.get("name") or ""))
        add(
            "naming.derived_frame_joint",
            {
                "object_type": "joint",
                "object": joint_name,
                "field": "name",
                "actual": joint_name,
                "source": "normalized parent and frame identities",
                "original": {"parent": frame.get("parent"), "child": frame.get("name")},
                "rule_id": "naming.fixed_endpoint_pair",
                "rule_version": "naming-v1",
                "reason": "fixed auxiliary joint derived from its normalized parent and child endpoints",
            },
        )
    for link, shape in shapes:
        provenance = shape.get("provenance") if isinstance(shape.get("provenance"), dict) else {}
        native_component = str(provenance.get("native_component") or "")
        _require(native_component, "Visual carries no native component identity")
        if native_geometry_rows is not None:
            native_row = native_geometry.get(native_component)
            _require(native_row is not None, "Visual is missing from the raw geometry evidence")
            _require(
                native_row.get("path") == shape.get("filename")
                and native_row.get("sha256") == shape.get("sha256"),
                "Visual mesh identity differs from the raw geometry evidence",
            )
        _require(
            shape.get("name") == visual_name(native_component, link=link["name"]),
            "Visual name differs from the CAD-derived rule",
        )
        current_sha256 = file_digest(confined(root / "evidence", str(shape.get("filename") or "")))
        _require(shape.get("sha256") == current_sha256, "Visual digest differs from capture evidence")
        _require(
            shape.get("mesh_name")
            == mesh_path(current_sha256, namespace=mesh_namespace),
            "Mesh path differs from content addressing",
        )
        material_source = provenance.get("material_source")
        _require(
            material_source == "platform_display_fallback",
            "The capture schema carries no independently verified CAD appearance",
        )
        _require(
            shape.get("material") == DISPLAY_MATERIAL_FALLBACK_NAME
            and shape.get("rgba") == list(DISPLAY_MATERIAL_FALLBACK_RGBA)
            and all(provenance.get(key) == value for key, value in DISPLAY_MATERIAL_FALLBACK_RULE.items()),
            "Display material fallback differs from its versioned platform rule",
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
            "display.material_fallback",
            {
                "object_type": "visual",
                "object": shape["name"],
                "field": "material",
                "actual": {"name": shape["material"], "rgba": shape["rgba"]},
                "source": "platform display fallback; CAD appearance unobserved",
                "original": None,
                "rule_id": provenance.get("rule_id"),
                "rule_version": provenance.get("rule_version"),
                "reason": provenance.get("reason"),
            },
        )
    if native_geometry_rows is not None:
        _require(
            set(native_geometry)
            == {
                str((shape.get("provenance") or {}).get("native_component") or "")
                for _link, shape in shapes
            },
            "Raw geometry and delivered visual occurrences differ",
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
        "display.material_fallback": (
            "CAD appearance is unavailable; a platform display fallback is used without material semantics."
        ),
        "naming.formatted_interface": (
            "Link, joint, and frame names are deterministic formatting of native CAD identities."
        ),
        "naming.derived_frame_joint": (
            "Auxiliary frame joints derive their names from normalized parent and child identities."
        ),
        "naming.motion_unresolved": "Joint names without a CAD motion classification retain a neutral name.",
        "direction.semantics_unresolved": "Authored CAD axes are preserved; functional positive motion is not proven.",
        "direction.author_sign_applied": (
            "CAD author axis_sign selects the positive direction of the undirected "
            "shaft; q and ranges remain in author q."
        ),
        "naming.derived_visual": "Visual names are derived from the exact native component occurrence identity.",
        "naming.content_addressed_mesh": "Mesh paths are derived from the current captured STL content digest.",
        "limits.preview_policy": (
            "Inspection preview uses a versioned full-turn rotation window; "
            "no native mechanical range is claimed."
        ),
        "limits.default_policy": (
            "Engineering uses a versioned default rotation window because no usable native "
            "or authored position range was read; the engineering position range is unqualified."
        ),
        "limits.declared_author": (
            "Joint position values come from a controlled author record; they are not native CAD readings."
        ),
        "drive.declared_author": (
            "Joint drive values come from a controlled author record; "
            "they are preserved but are not native CAD readings."
        ),
        "drive.unqualified": (
            "Controlled drive values of zero are retained for delivery but do not "
            "qualify effort or velocity sizing."
        ),
        "naming.formatted_robot_name": "The robot name is formatted from the native top-assembly filename.",
        "physics.derived_from_native": (
            "Link mass properties are transformed, translated or recombined from native readings."
        ),
        "units.si_conversion": "Delivered values use SI units after conversion from native CAD units.",
        METADATA_WARNING_CODE: METADATA_WARNING_MESSAGE,
    }
    _require(
        set(actual) == set(expected),
        "Non-native value provenance warning classes differ from the delivery: "
        f"actual={sorted(actual)} expected={sorted(expected)}",
    )
    _require(
        {code: Counter(values) for code, values in actual_messages.items()}
        == {code: Counter([messages[code]]) for code in expected},
        "Non-native value provenance warning messages differ from the delivery",
    )
    for code in sorted(expected):
        actual_rows = canonical(actual[code])
        expected_rows = canonical(expected[code])
        _require(
            actual_rows == expected_rows,
            f"Non-native value provenance warning objects differ for {code}: "
            f"actual={actual_rows} expected={expected_rows}",
        )
    return {
        "codes": sorted(actual),
        "objects": sum(len(rows) for rows in actual.values()),
    }


def _frame(frame, model, joints, links, datums, body_datums):
    name = frame["name"]
    _require(not list(links[name]), "Reference frame must be massless and have no geometry")
    node = joints[fixed_joint_name(str(frame["parent"]), name)]
    _require(
        node.attrib["type"] == "fixed"
        and _one(node, "parent").attrib["link"] == frame["parent"]
        and _one(node, "child").attrib["link"] == name,
        "Reference frame joint changed",
    )
    expected = np.linalg.inv(body_datums[frame["parent"]]) @ datums[frame["coordinate_system"]]
    _same_pose(_xml_pose(node), expected)
    canonical = next(item for item in model["frames"] if item["name"] == name)
    provenance = canonical.get("provenance") if isinstance(canonical, dict) else {}
    _require(
        isinstance(provenance, dict)
        and provenance.get("derived_joint_name") == node.get("name")
        and provenance.get("joint_name_rule")
        == {
            "rule_id": "naming.fixed_endpoint_pair",
            "rule_version": "naming-v1",
            "parent": frame["parent"],
            "child": name,
        },
        "Frame auxiliary joint name provenance is not the canonical endpoint rule",
    )
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


def check_bundle(
    root: Path,
    *,
    native_receipts=None,
    require_native_receipts: bool = False,
) -> dict:
    """Recompute all gates and require an exact binding to the saved report."""
    report = evaluate_bundle(
        root,
        native_receipts=native_receipts,
        require_native_receipts=require_native_receipts,
    )
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
