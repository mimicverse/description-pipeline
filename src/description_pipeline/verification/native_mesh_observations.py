"""Validate native tessellation accounting without silently completing surfaces."""

import hashlib
import math
import struct
from pathlib import Path

from ..geometry.native_tessellation import (
    HEX64,
    parse_native_tessellation,
    summarize_native_topology,
)
from ..geometry.native_u import (
    NATIVE_EDGE_FIN_IDS_DERIVED,
    NATIVE_FACE_FACET_IDS_DERIVED,
)
from ..io import PipelineError, confined
from ..schemas import (
    NATIVE_FACETLESS_EVIDENCE_API,
    NATIVE_ID_ARRAY_SOURCE_BODY_GLOBAL_NULL_ZERO,
    NATIVE_ID_ARRAY_SOURCE_FLAT,
    NATIVE_ID_ARRAY_SOURCE_INCIDENT_FACES_NULL_ZERO,
)
from ..sources.snapshot import verify_snapshot
from . import native_discovery as evidence


def _require(condition, message):
    """One stable verifier failure boundary for all native mesh evidence."""

    if not condition:
        raise PipelineError(message)


def _integer(value) -> bool:
    return type(value) is int


def _stl_binding(bundle: Path, entry: dict, triangles: int) -> Path:
    relative = entry.get("path")
    digest = entry.get("sha256")
    _require(
        isinstance(relative, str)
        and isinstance(digest, str)
        and bool(HEX64.fullmatch(digest)),
        entry["component"] + ": native STL identity is missing",
    )
    path = confined(Path(bundle) / "evidence", relative)
    actual = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            actual.update(chunk)
    size = path.stat().st_size
    with path.open("rb") as stream:
        header = stream.read(84)
    _require(
        actual.hexdigest() == digest
        and len(header) == 84
        and size == 84 + 50 * triangles
        and struct.unpack_from("<I", header, 80)[0] == triangles,
        entry["component"] + ": native STL bytes do not bind the recorded triangle count",
    )
    return path


def _span_digests(path: Path, spans: list[dict]) -> None:
    with path.open("rb") as stream:
        for span in spans:
            stream.seek(84 + 50 * span["triangle_start"])
            payload = stream.read(50 * span["triangle_count"])
            owners = {
                struct.unpack_from("<H", payload, 50 * index + 48)[0]
                for index in range(span["triangle_count"])
            }
            _require(
                len(payload) == 50 * span["triangle_count"]
                and owners <= {span["face_owner_id"]}
                and hashlib.sha256(payload).hexdigest() == span["triangle_records_sha256"],
                "a native face span does not bind its exact STL triangle owners",
            )


def _stl_triangle_vertices(path: Path, triangles: int) -> list[tuple[tuple[float, float, float], ...]]:
    """Read the binary STL vertices independently of CAD tessellation objects."""

    data = path.read_bytes()
    _require(
        len(data) == 84 + 50 * triangles,
        "native STL bytes do not match the recorded triangle count",
    )
    return [
        tuple(
            struct.unpack_from("<3f", data, 84 + 50 * triangle_index + 12 + 12 * index)
            for index in range(3)
        )
        for triangle_index in range(triangles)
    ]


def verify_native_mesh_observations(bundle: Path) -> dict:
    manifest = verify_snapshot(Path(bundle) / "evidence")
    evidence.source_scopes(bundle)
    native = ((manifest.get("identity") or {}).get("capture") or {}).get("evidence_class") == "cad"
    rows = evidence.geometry(bundle)
    warnings = []
    untessellated_warnings = []
    topologies = []
    observed = 0
    for entry in rows:
        tessellation = entry.get("tessellation")
        if tessellation is None and not native:
            continue
        component = entry["component"]
        _require(isinstance(tessellation, dict), component + ": native tessellation accounting is missing")
        faces = tessellation["faces"]
        omitted = tessellation["no_facets_faces"]
        _require(type(faces) is int and faces > 0, component + ": invalid native face count")
        _require(isinstance(omitted, list) and len(omitted) < faces, component + ": no tessellated native faces remain")
        triangles = entry.get("triangles")
        _require(_integer(triangles) and triangles > 0, component + ": invalid declared STL triangle count")
        bodies = entry.get("bodies")
        _require(isinstance(bodies, dict) and set(bodies) == {"solid", "sheet"}, component + ": body counts missing")
        _require(
            all(type(count) is int and count >= 0 for count in bodies.values()), component + ": invalid body counts"
        )
        _require(sum(bodies.values()) > 0, component + ": no native bodies recorded")
        path = _stl_binding(bundle, entry, triangles)
        native_fin_resolutions = []
        native_identity_warnings = []
        spans = parse_native_tessellation(
            component,
            tessellation,
            bodies,
            triangles,
            _stl_triangle_vertices(path, triangles),
            native_fin_resolutions=native_fin_resolutions,
            native_identity_warnings=native_identity_warnings,
        )
        warnings.extend(native_fin_resolutions)
        warnings.extend(native_identity_warnings)
        topology = summarize_native_topology(
            tessellation,
            spans,
            component=component,
        )
        _require(
            topology["status"] == "evidence_complete",
            component + ": native body-local topology evidence is unresolved",
        )
        topologies.append({"component": component, **topology})
        _span_digests(path, spans)
        seen = set()
        component_untessellated = []
        face_evidence = {
            (
                body["body_type"],
                body["body_index"],
                face["face_index"],
            ): face
            for body in tessellation["itessellation"]["bodies"]
            for face in body["faces"]
        }
        edge_evidence = {
            (
                edge["body_type"],
                edge["body_index"],
                edge["edge_reference_sha256"],
            ): edge
            for edge in tessellation["native_edges"]
        }
        base = {
            "body_type",
            "body_index",
            "face_index",
            "triangle_count",
            "area_m2",
            "used_api",
            "source_rule",
            "warnings",
            "derived_empty_edges",
        }
        for face in omitted:
            keys = set(face) if isinstance(face, dict) else None
            _require(
                keys in (base | {"facet_count", "tessellate_result"}, base | {"tessellate_probe_error"}),
                component + ": malformed untessellated-face observation",
            )
            _require(
                _integer(face["body_type"]) and face["body_type"] == 1,
                component + ": only native sheet faces may lack tessellation",
            )
            _require(
                _integer(face["body_index"]) and 0 <= face["body_index"] < bodies["sheet"],
                component + ": untessellated body is outside the native sheet inventory",
            )
            _require(
                _integer(face["face_index"]) and face["face_index"] >= 0,
                component + ": invalid native face index",
            )
            key = (face["body_type"], face["body_index"], face["face_index"])
            _require(key not in seen, component + ": duplicate untessellated face")
            seen.add(key)
            _require(
                type(face["triangle_count"]) is int and face["triangle_count"] == 0,
                component + ": native triangles cannot be omitted",
            )
            _require(
                type(face["area_m2"]) in (int, float) and math.isfinite(face["area_m2"]) and face["area_m2"] >= 0,
                component + ": invalid untessellated native area",
            )
            if "tessellate_result" in face:
                _require(
                    type(face["facet_count"]) is int and face["facet_count"] == 0,
                    component + ": native facets cannot be omitted",
                )
                _require(
                    type(face["tessellate_result"]) is bool, component + ": native tessellation result is unreadable"
                )
            else:
                # A persistent DISP_E_EXCEPTION on geometrically degenerate faces is
                # recorded as a probe error instead of a result: both facet_count and
                # tessellate_result are products of the probe that failed, so the
                # variant carries neither; it is admissible only for numerically zero
                # area and never loosens any other check.
                probe_error = face["tessellate_probe_error"]
                _require(
                    isinstance(probe_error, str) and probe_error.startswith("com_error"),
                    component + ": native tessellation probe error is unreadable",
                )
                _require(
                    face["area_m2"] <= 1e-12,
                    component + ": a degenerate-face probe error is only admissible at zero native area",
                )
            _require(
                face["used_api"] == NATIVE_FACETLESS_EVIDENCE_API,
                component + ": unsupported untessellated-face evidence API",
            )
            evidence_face = face_evidence.get(key)
            _require(
                isinstance(evidence_face, dict),
                component + ": untessellated face has no native face evidence",
            )
            _require(
                face["source_rule"] == evidence_face["facet_ids_source"]
                and face["warnings"] == evidence_face["warnings"]
                and face["source_rule"]
                in {
                    NATIVE_ID_ARRAY_SOURCE_FLAT,
                    NATIVE_ID_ARRAY_SOURCE_BODY_GLOBAL_NULL_ZERO,
                }
                and face["warnings"]
                == (
                    [NATIVE_FACE_FACET_IDS_DERIVED]
                    if face["source_rule"]
                    == NATIVE_ID_ARRAY_SOURCE_BODY_GLOBAL_NULL_ZERO
                    else []
                )
                and isinstance(face["derived_empty_edges"], list),
                component + ": untessellated face source evidence is inconsistent",
            )
            derived_references = []
            for derived in face["derived_empty_edges"]:
                reference = (
                    derived.get("edge_reference_sha256")
                    if isinstance(derived, dict)
                    else None
                )
                edge = edge_evidence.get(
                    (
                        face["body_type"],
                        face["body_index"],
                        reference,
                    )
                )
                _require(
                    isinstance(derived, dict)
                    and set(derived)
                    == {
                        "edge_reference_sha256",
                        "source_rule",
                        "warnings",
                    }
                    and isinstance(edge, dict)
                    and face["face_index"] in edge["face_indices"]
                    and derived["source_rule"]
                    == NATIVE_ID_ARRAY_SOURCE_INCIDENT_FACES_NULL_ZERO
                    and derived["warnings"]
                    == [NATIVE_EDGE_FIN_IDS_DERIVED]
                    and edge["fin_ids_source"] == derived["source_rule"]
                    and edge["warnings"] == derived["warnings"],
                    component + ": derived empty-edge source evidence is inconsistent",
                )
                derived_references.append(reference)
            expected_derived_references = sorted(
                reference
                for (body_type, body_index, reference), edge
                in edge_evidence.items()
                if (
                    body_type == face["body_type"]
                    and body_index == face["body_index"]
                    and face["face_index"] in edge["face_indices"]
                    and edge["fin_ids_source"]
                    == NATIVE_ID_ARRAY_SOURCE_INCIDENT_FACES_NULL_ZERO
                )
            )
            _require(
                derived_references == expected_derived_references,
                component
                + ": derived empty-edge references do not exactly cover incident empty-fin edges",
            )
            component_untessellated.append(
                {
                    "code": "cad_sheet_face_untessellated",
                    "component": component,
                    **face,
                    "qualification": "native_facets_absent; complete_surface_geometry_unqualified",
                }
            )
        warnings.extend(component_untessellated)
        untessellated_warnings.extend(component_untessellated)
        span_counts = {
            (span["body_type"], span["body_index"], span["face_index"]): span["triangle_count"]
            for span in spans
        }
        _require(
            set(seen)
            == {
                key
                for key, count in span_counts.items()
                if count == 0
            },
            component + ": untessellated-face evidence differs from zero-length face spans",
        )
        observed += 1
    return {
        "components": observed,
        "warnings": warnings,
        "topologies": topologies,
        "untessellated_native_area_m2": math.fsum(
            row["area_m2"] for row in untessellated_warnings
        ),
    }
