"""Explicit, fail-closed SolidWorks COM access.

Each capture owns separate source and copy applications. Documents open read-only
from saved files; the user's application is never used as a collection server.
Failures raise stable error codes and only owned process trees are reclaimed.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import ntpath
import os
import posixpath
import re
import shutil
import struct
import sys
import threading
import time
from collections import defaultdict
from contextlib import contextmanager
from pathlib import Path

from ...geometry.native_u import (
    NATIVE_CURVE_DOMAIN_RULE,
    NATIVE_CURVE_ENDPOINT_OPERATION_RULE,
    NATIVE_COMPONENT_REFERENCE_ALIASED,
    NATIVE_FACE_IDENTITY_KEY_RULE,
    NATIVE_IDENTITY_POLICY_RULE,
    NATIVE_PART_WITNESS_DERIVED,
    NATIVE_POINT_ROUNDTRIP_RULE,
    NATIVE_PERSISTENT_IDENTITY_RULE,
    NATIVE_SOURCE_SCOPE_RULE,
    NATIVE_SCOPE_IDENTITY_RULE,
    NATIVE_SCOPE_LOCAL_IDENTITY_DERIVED,
    NATIVE_SCOPE_MAPPING_METHOD,
    NATIVE_SCOPE_SOURCE_AUTHORITY,
    NATIVE_SCOPE_VERTEX_MAPPING_METHOD,
    NATIVE_TOPOLOGY_IDENTITY_DERIVED,
    NATIVE_TESSELLATION_EDGE_PROJECTION_METRIC,
    NATIVE_TESSELLATION_EDGE_PROJECTION_RULE,
    NATIVE_TESSELLATION_EDGE_PROJECTION_TOLERANCE_M,
    NATIVE_U_CLOSEST_POINT_PARAMETER_RULE,
    NATIVE_U_IDENTITY_RULE,
    NATIVE_U_RESOLUTION_RULE,
    native_curve_endpoint_operation_tolerance,
    native_curve_parameter_endpoints,
    native_curve_topology_endpoint_set_assignment,
    native_evaluate2_evidence,
    native_fin_interval,
    native_point_error,
    native_point_roundtrip_tolerance,
    native_point_within_max_coordinate_bound,
    native_scope_identity_sha256,
    native_u_equal,
    native_u_in_closed_interval,
    native_u_intervals_cover,
    native_curve_domain_transform,
)
from ...geometry.native_fin_topology import (
    NativeFinRelationError,
    validate_native_edge_fin_relations,
)
from ...schemas import NATIVE_DISCOVERY_SCHEMA, NATIVE_TESSELLATION_SCHEMA
from .errors import CadError, EnvironmentError_
from .protocol import CadBackend, RawComponent, RawScene
from .session_artifacts import (
    SESSION_DOCUMENT_API,
    embedded_virtual_component_marker,
    embedded_virtual_component_path,
    embedded_virtual_component_root,
    session_artifact_path,
    session_document_marker,
    session_materialization_path,
    session_root_pid,
    session_temp_root,
)

# Published dual-interface IIDs in the SolidWorks type library.
ICOMPONENT2_IID = "{655D6F2A-5441-45D1-8CBA-D35FB26988E4}"
IMODELDOC2_IID = "{B90793FB-EF3D-4B80-A5C4-99959CDB6CEB}"
IPARTDOC_IID = "{83A33D32-27C5-11CE-BFD4-00400513BB57}"
IASSEMBLYDOC_IID = "{83A33D35-27C5-11CE-BFD4-00400513BB57}"
ICONFIGURATIONMANAGER_IID = "{8DB64337-F36E-47CC-BBBC-B4B979D31505}"
ICONFIGURATION_IID = "{83A33D98-27C5-11CE-BFD4-00400513BB57}"
IMODELDOCEXTENSION_IID = "{99F4D4AF-F268-4EE1-8C55-041F7BECF879}"
#: Published ITessellation dual-interface identity, checked against sldworks.tlb.
ITESSELLATION_IID = "{699C7492-DB56-4CC2-B7A2-9FFD56698434}"
#: IConcentricMateFeatureData (dual interface, same offline typelib owners
#: dump line 87); its documented LockRotation property is the native evidence
#: for "Lock rotation" on a concentric mate.
ICONCENTRICMATEFEATUREDATA_IID = "{D9E1EBDD-2E8B-424F-9401-0A3C8ED7FAAD}"
#: IAngleMateFeatureData / IDistanceMateFeatureData (offline SolidWorks 2026
#: typelib owners: indices 391 and 390); IsAdvancedMate distinguishes a limit
#: mate from a standard angle/distance mate.
IANGLEMATEFEATUREDATA_IID = "{EF4303D7-B4FD-46E7-AD34-D942C420E096}"
IDISTANCEMATEFEATUREDATA_IID = "{943EED8D-E13B-4459-BD59-EBCC3F60B915}"
IMASSPROPERTY2_IID = "{5E4EAE4A-65E6-4CF0-AD72-55EDEF5EB96A}"
IMASSPROPERTYOVERRIDEOPTIONS_IID = "{82E1635E-B650-4C8B-84C8-D5FFBF660E49}"
ICUSTOMPROPERTYMANAGER_IID = "{F1938D94-0D1F-4E88-9874-164B1E6568F1}"
IFEATURE_IID = "{83A33D38-27C5-11CE-BFD4-00400513BB57}"
IMATE2_IID = "{B09D234A-7581-408E-B3B3-FC0A514AAFA8}"
IMATEENTITY2_IID = "{CE7FE69D-BCFA-441F-A37D-E740546458A4}"
IBODY2_IID = "{3A075BFD-9962-4431-8321-7AF4903C55AD}"
IFACE2_IID = "{4A8BA4D8-DA25-4B75-8E2D-4922B74D81ED}"
IEDGE_IID = "{83A33D42-27C5-11CE-BFD4-00400513BB57}"
IVERTEX_IID = "{83A33D63-27C5-11CE-BFD4-00400513BB57}"
ISURFACE_IID = "{83A33D40-27C5-11CE-BFD4-00400513BB57}"
ICURVE_IID = "{83A33D44-27C5-11CE-BFD4-00400513BB57}"
ICURVEPARAMDATA_IID = "{03DFD291-2C77-494F-A81A-8C3653BAB985}"
IMATHTRANSFORM_IID = "{F7D97F82-162E-11D4-AEAB-00C04FA0AC51}"

# Members whose declared return is one published interface of the installed
# SolidWorks type library. Bound inside the adapter so no caller can keep a
# generic view of a declared return type.
RETURN_VIEWS = {
    "Extension": IMODELDOCEXTENSION_IID,
    "CreateMassProperty2": IMASSPROPERTY2_IID,
    "CustomPropertyManager": ICUSTOMPROPERTYMANAGER_IID,
    "GetCoordinateSystemTransformByName": IMATHTRANSFORM_IID,
    "GetOverrideOptions": IMASSPROPERTYOVERRIDEOPTIONS_IID,
    "FirstFeature": IFEATURE_IID,
    "GetNextFeature": IFEATURE_IID,
    "GetNextSubFeature": IFEATURE_IID,
    "GetFirstSubFeature": IFEATURE_IID,
    "GetFeature": IFEATURE_IID,
    "GetSpecificFeature2": IMATE2_IID,
    "MateEntity": IMATEENTITY2_IID,
    "GetSurface": ISURFACE_IID,
    "GetCurve": ICURVE_IID,
    "GetCurveParams3": ICURVEPARAMDATA_IID,
    "GetStartVertex": IVERTEX_IID,
    "GetEndVertex": IVERTEX_IID,
    "GetTotalTransform": IMATHTRANSFORM_IID,
    "OpenDoc6": IMODELDOC2_IID,
}

# Members whose declared return is an array of one published interface.
RETURN_ARRAY_VIEWS = {
    "GetBodies2": IBODY2_IID,
    "GetFaces": IFACE2_IID,
    "GetEdges": IEDGE_IID,
}


def co_initialize():
    if os.name != "nt":
        return False
    import pythoncom

    pythoncom.CoInitializeEx(pythoncom.COINIT_APARTMENTTHREADED)
    return True


def _win32():
    try:
        import win32com.client
    except ImportError as exc:
        raise EnvironmentError_("no_pywin32", str(exc)) from exc
    return win32com.client


def _whole_body_face_list(faces, face_reference_sha256):
    """Build one explicit all-face dispatch array for a whole-body read."""

    import pythoncom

    typed_faces = tuple(faces)
    references = tuple(face_reference_sha256)
    if not typed_faces or len(typed_faces) != len(references):
        raise RuntimeError("whole-body FaceList requires every typed native face")
    variant_type = pythoncom.VT_ARRAY | pythoncom.VT_DISPATCH
    face_list = _win32().VARIANT(variant_type, typed_faces)
    if getattr(face_list, "varianttype", None) != variant_type:
        raise RuntimeError(
            "whole-body FaceList did not marshal as VT_ARRAY|VT_DISPATCH"
        )
    return face_list, {
        "api": "IBody2.GetTessellation",
        "argument_count": 1,
        "face_list": {
            "scope": "whole_body",
            "variant_type": "VT_ARRAY|VT_DISPATCH",
            "face_count": len(typed_faces),
            "first_face_reference_sha256": references[0],
            "last_face_reference_sha256": references[-1],
        },
    }


def _dynamic(value):
    # A COM interface can be callable through DISPID_VALUE. It is not a method.
    if hasattr(value, "_oleobj_"):
        import win32com.client.dynamic

        return win32com.client.dynamic.DumbDispatch(value._oleobj_)
    return value


def _interface(value, iid):
    """Bind a native dispatch to one published dual interface; fail closed."""
    if not hasattr(value, "_oleobj_"):
        return value
    import pythoncom
    import pywintypes
    import win32com.client.dynamic

    dispatch = value._oleobj_.QueryInterface(pywintypes.IID(iid), pythoncom.IID_IDispatch)
    return win32com.client.dynamic.DumbDispatch(dispatch)


def _com_object_identity(value) -> int:
    """Process-local identity for repeated reads of one native COM object."""

    return id(getattr(value, "_oleobj_", value))


_COM_OBJECT_POINTER = re.compile(r"obj at 0x([0-9a-fA-F]+)")


def _com_object_pointer(value) -> int:
    """Return the COM object pointer exposed by pywin32's wrapper repr."""

    token = getattr(value, "_oleobj_", value)
    match = _COM_OBJECT_POINTER.search(repr(token))
    if match is None:
        return _com_object_identity(value)
    return int(match.group(1), 16)


def _com_iunknown_pointer(value) -> int:
    """Return the canonical IUnknown pointer for a live pywin32 object."""

    token = getattr(value, "_oleobj_", None)
    if token is None:
        return _com_object_pointer(value)
    import pythoncom
    import pywintypes

    unknown = token.QueryInterface(
        pywintypes.IID(pythoncom.IID_IUnknown),
        pythoncom.IID_IUnknown,
    )
    return _com_object_pointer(unknown)


def _binary64_bits(value) -> str:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("the native identity value is not finite")
    return struct.pack("<d", number).hex()


def _face_scope_geometry(face) -> dict:
    """Read exact face area, tessellation bbox and coordinate digest."""

    area = _member(face, "GetArea")
    if type(area) not in (int, float) or not math.isfinite(float(area)):
        raise ValueError("the native face area is not finite")
    values = _as_list(_method(face, "GetTessTriangles", True))
    if (
        not values
        or len(values) % 9
        or any(
            type(value) not in (int, float)
            or not math.isfinite(float(value))
            for value in values
        )
    ):
        raise ValueError("the native face tessellation is empty or malformed")
    points = [
        tuple(float(value) for value in values[offset : offset + 3])
        for offset in range(0, len(values), 3)
    ]
    bbox = tuple(
        min(point[axis] for point in points)
        for axis in range(3)
    ) + tuple(
        max(point[axis] for point in points)
        for axis in range(3)
    )
    coordinate_payload = b"".join(
        struct.pack("<d", float(value)) for value in values
    )
    return {
        "area_binary64": _binary64_bits(area),
        "bbox_binary64": [_binary64_bits(value) for value in bbox],
        "coordinate_sha256_float64_le": hashlib.sha256(
            coordinate_payload
        ).hexdigest(),
        "triangle_count": len(values) // 9,
    }


def _persistent_reference_sha256(extension, value) -> str:
    raw = _persistent_reference_bytes(extension, value)
    if not raw:
        raise ValueError("the native persistent reference is empty")
    return hashlib.sha256(raw).hexdigest()


def _persistent_reference_bytes(extension, value) -> bytes:
    """Return one native persistent reference without normalizing its bytes."""

    return bytes(
        int(item) & 0xFF
        for item in _member(extension, "GetPersistReference3", value)
    )


def _scope_face_rows(faces, extension) -> list[dict]:
    rows = []
    for face_index, raw_face in enumerate(faces):
        face = _interface(_dynamic(raw_face), IFACE2_IID)
        geometry = _face_scope_geometry(face)
        rows.append(
            {
                "face_index": face_index,
                "object_pointer": _com_object_pointer(face),
                "reference_sha256": _persistent_reference_sha256(
                    extension,
                    face,
                ),
                "geometry": geometry,
                "mapping_key": (
                    geometry["area_binary64"],
                    tuple(geometry["bbox_binary64"]),
                ),
                "mapping_index": face_index,
            }
        )
    return rows


def _scope_edge_rows(body, extension, face_rows) -> list[dict]:
    face_by_pointer = {
        row["object_pointer"]: row["face_index"]
        for row in face_rows
    }
    rows = []
    for edge_index, raw_edge in enumerate(
        _as_list(_member(body, "GetEdges"))
    ):
        edge = _interface(_dynamic(raw_edge), IEDGE_IID)
        endpoints = {}
        for endpoint in ("start", "end"):
            raw_vertex = _member(
                edge,
                "GetStartVertex"
                if endpoint == "start"
                else "GetEndVertex",
            )
            if raw_vertex is None:
                endpoints[endpoint] = {
                    "state": "absent",
                    "object_pointer": None,
                    "reference_sha256": None,
                    "point_m": None,
                }
                continue
            vertex = _interface(_dynamic(raw_vertex), IVERTEX_IID)
            endpoints[endpoint] = {
                "state": "present",
                "object_pointer": _com_object_pointer(vertex),
                # Keep the dispatch alive for the complete witness scan.  Without
                # this owner, SolidWorks may recycle the COM pointer after this
                # endpoint wrapper is released and a later edge can appear to
                # reuse the same pointer for a different vertex.
                "native_object": vertex,
                "reference_sha256": _persistent_reference_sha256(
                    extension,
                    vertex,
                ),
                "point_m": _binary64_xyz(
                    _as_list(_member(vertex, "GetPoint"))
                ),
            }
        adjacent_faces = []
        for raw_face in _as_list(
            _member(edge, "GetTwoAdjacentFaces2")
        ):
            if raw_face is None:
                continue
            face = _interface(_dynamic(raw_face), IFACE2_IID)
            pointer = _com_object_pointer(face)
            if pointer not in face_by_pointer:
                raise ValueError(
                    "a native edge names a face outside its body"
                )
            adjacent_faces.append(face_by_pointer[pointer])
        adjacent_faces = sorted(adjacent_faces)
        if (
            not adjacent_faces
            or len(adjacent_faces) != len(set(adjacent_faces))
        ):
            raise ValueError("a native edge has invalid face incidence")
        curve_params = _interface(
            _dynamic(_member(edge, "GetCurveParams3")),
            ICURVEPARAMDATA_IID,
        )
        curve = _interface(
            _dynamic(_member(edge, "GetCurve")),
            ICURVE_IID,
        )
        end_params = _read_curve_end_params(curve)
        endpoint_states = {
            endpoints[name]["state"] for name in ("start", "end")
        }
        if endpoint_states == {"absent"}:
            if not end_params["is_closed"]:
                raise ValueError(
                    "both edge endpoints are absent without a closed curve"
                )
        elif endpoint_states != {"present"}:
            raise ValueError("edge endpoint states differ")
        rows.append(
            {
                "edge_index": edge_index,
                "reference_sha256": _persistent_reference_sha256(
                    extension,
                    edge,
                ),
                "endpoints": endpoints,
                "face_indices": adjacent_faces,
                "u_min": float(_member(curve_params, "UMinValue")),
                "u_max": float(_member(curve_params, "UMaxValue")),
                "curve_start_point_m": _binary64_xyz(
                    _as_list(_member(curve_params, "StartPoint"))
                ),
                "curve_end_point_m": _binary64_xyz(
                    _as_list(_member(curve_params, "EndPoint"))
                ),
                "curve_sense": _member(curve_params, "Sense"),
                "curve_type": _member(curve_params, "CurveType"),
                "curve_closed": end_params["is_closed"],
                "curve_periodic": end_params["is_periodic"],
            }
        )
    return rows


def _scope_edge_mapping_key(
    row: dict,
    adjacent_face_indices: list[int],
) -> str:
    endpoints = row["endpoints"]
    def point_bits(point):
        return (
            None
            if point is None
            else [_binary64_bits(value) for value in point]
        )
    return native_scope_identity_sha256(
        {
            "start_point_binary64": point_bits(
                endpoints["start"]["point_m"]
            ),
            "end_point_binary64": point_bits(
                endpoints["end"]["point_m"]
            ),
            "curve_u_min_binary64": _binary64_bits(row["u_min"]),
            "curve_u_max_binary64": _binary64_bits(row["u_max"]),
            "curve_start_point_binary64": [
                _binary64_bits(value)
                for value in row["curve_start_point_m"]
            ],
            "curve_end_point_binary64": [
                _binary64_bits(value)
                for value in row["curve_end_point_m"]
            ],
            "curve_sense": row["curve_sense"],
            "curve_type": row["curve_type"],
            "curve_closed": row["curve_closed"],
            "curve_periodic": row["curve_periodic"],
            "adjacent_mapped_face_ids": adjacent_face_indices,
        }
    )


def _group_vertex_observations(observations) -> list[dict]:
    """Group vertices by exact point and persistent reference, never by address."""

    groups = {}
    for (
        point_m,
        raw_reference_sha256,
        typed_reference_sha256,
        edge_index,
        endpoint_order,
        object_pointer,
    ) in observations:
        point = tuple(float(value) for value in point_m)
        point_bits = tuple(_binary64_bits(value) for value in point)
        if (
            not isinstance(raw_reference_sha256, str)
            or not raw_reference_sha256
            or not isinstance(typed_reference_sha256, str)
            or not typed_reference_sha256
        ):
            raise ValueError("a present native vertex has no persistent reference")
        key = (point_bits, raw_reference_sha256)
        group = groups.setdefault(
            key,
            {
                "point_m": point,
                "point_bits": point_bits,
                "raw_reference_sha256": raw_reference_sha256,
                "typed_references": set(),
                "object_pointers": set(),
                "occurrences": [],
            },
        )
        if group["point_m"] != point:
            raise ValueError("one native vertex key has inconsistent points")
        group["typed_references"].add(typed_reference_sha256)
        if len(group["typed_references"]) != 1:
            raise ValueError(
                "one native vertex key has inconsistent typed references"
            )
        if object_pointer is not None:
            group["object_pointers"].add(object_pointer)
        group["occurrences"].append((edge_index, endpoint_order))

    result = []
    for group in groups.values():
        occurrences = tuple(sorted(group["occurrences"]))
        result.append(
            {
                "point_m": group["point_m"],
                "point_bits": group["point_bits"],
                "raw_reference_sha256": group["raw_reference_sha256"],
                "typed_reference_sha256": next(
                    iter(group["typed_references"])
                ),
                "object_pointers": tuple(sorted(group["object_pointers"])),
                "occurrences": occurrences,
            }
        )
    result.sort(
        key=lambda group: (
            group["point_bits"],
            group["raw_reference_sha256"],
            group["typed_reference_sha256"],
            group["occurrences"],
        )
    )
    return result


def _scope_vertex_rows(edge_rows, edge_index_map=None) -> list[dict]:
    observations = []
    for row in edge_rows:
        for order, endpoint_name in enumerate(("start", "end")):
            endpoint = row["endpoints"][endpoint_name]
            if endpoint["state"] != "present":
                continue
            observations.append(
                (
                    endpoint["point_m"],
                    endpoint["reference_sha256"],
                    endpoint["reference_sha256"],
                    row["edge_index"],
                    order,
                    endpoint["object_pointer"],
                )
            )
    rows_by_point = {}
    for group in _group_vertex_observations(observations):
        point_bits = group["point_bits"]
        rows_by_point.setdefault(point_bits, []).append(group)
    ordered = sorted(
        (
            (
                point_bits,
                occurrence,
                group,
            )
            for point_bits, groups in rows_by_point.items()
            for occurrence, group in enumerate(groups)
        ),
        key=lambda row: (row[0], row[1]),
    )
    return [
        {
            "vertex_index": vertex_index,
            "mapping_index": vertex_index,
            "point_m": list(group["point_m"]),
            "coincident_occurrence_index": occurrence,
            "mapping_key": (
                *(
                    _binary64_bits(value)
                    for value in group["point_m"]
                ),
                tuple(
                    sorted(
                        {
                            (edge_index_map or {}).get(edge_index, edge_index)
                            for edge_index, _ in group["occurrences"]
                        }
                    )
                ),
            ),
            "reference_sha256": group["raw_reference_sha256"],
            "object_pointers": list(group["object_pointers"]),
        }
        for vertex_index, (_, occurrence, group) in enumerate(ordered)
    ]

def _scope_bijection(
    left: list[dict],
    right: list[dict],
) -> dict[int, int]:
    def index(rows):
        grouped = defaultdict(list)
        for row in rows:
            grouped[row["mapping_key"]].append(row["mapping_index"])
        return grouped

    left_by_key = index(left)
    right_by_key = index(right)
    if (
        set(left_by_key) != set(right_by_key)
        or any(len(left_by_key[key]) != 1 for key in left_by_key)
        or any(len(right_by_key[key]) != 1 for key in right_by_key)
    ):
        raise ValueError("the native scope mapping is incomplete or ambiguous")
    return {
        key: right_by_key[key][0]
        for key in left_by_key
    }


def _fin_edge_row_map(edge_rows, fin_ids_by_edge_index):
    """Bind each native fin to its exact edge row, independent of aliases."""

    result = {}
    for edge_row in edge_rows:
        for fin_id in fin_ids_by_edge_index[edge_row["index"]]:
            if fin_id in result:
                raise ValueError("a fin belongs to multiple native edges")
            result[fin_id] = edge_row
    return result


_SCOPE_OWNER_KEYS = {
    "occurrence",
    "source_document_path",
    "source_document_sha256",
    "source_document_identity_sha256",
    "source_document_identity_kind",
    "active_configuration",
    "referenced_configuration",
}


def _require_scope_owner(owner_scope: dict, base: dict) -> None:
    if (
        set(owner_scope) == _SCOPE_OWNER_KEYS
        and isinstance(owner_scope["occurrence"], str)
        and bool(owner_scope["occurrence"])
        and isinstance(owner_scope["source_document_path"], str)
        and bool(owner_scope["source_document_path"])
        and isinstance(
            owner_scope["source_document_identity_sha256"],
            str,
        )
        and re.fullmatch(
            r"[0-9a-f]{64}",
            owner_scope["source_document_identity_sha256"],
        )
        and owner_scope["source_document_identity_kind"] == "file_sha256"
        and isinstance(owner_scope["source_document_sha256"], str)
        and re.fullmatch(
            r"[0-9a-f]{64}",
            owner_scope["source_document_sha256"],
        )
        and owner_scope["source_document_sha256"]
        == owner_scope["source_document_identity_sha256"]
    ):
        return
    raise CadError(
        "cad_scope_owner_unavailable",
        "an aliased native object has no complete owner scope",
        base,
    )


def _scope_owner_key(
    owner_scope: dict,
    *,
    body_type: int,
    body_index: int,
    body_name: str,
) -> str:
    return native_scope_identity_sha256(
        {
            "occurrence_scope": owner_scope["occurrence"],
            "source_document_sha256": owner_scope[
                "source_document_sha256"
            ],
            "referenced_configuration": owner_scope[
                "referenced_configuration"
            ],
            "body_type": body_type,
            "body_index": body_index,
            "body_name": body_name,
        }
    )


def _component(value):
    """Bind a native occurrence to its published component interface."""
    return _interface(value, ICOMPONENT2_IID)


def _modeldoc2(value):
    """Bind a native document dispatch to the common IModelDoc2 interface."""
    return _interface(value, IMODELDOC2_IID)


def _partdoc(value):
    """Bind a part document dispatch to the IPartDoc domain interface."""
    return _interface(value, IPARTDOC_IID)


def _edge(value):
    """Bind a native edge to its published IEdge interface."""

    return _interface(value, IEDGE_IID)


def _vertex(value):
    """Bind a native vertex to its published IVertex interface."""

    return _interface(value, IVERTEX_IID)


def _assemblydoc(value):
    """Bind an assembly document dispatch to the IAssemblyDoc domain interface."""
    return _interface(value, IASSEMBLYDOC_IID)


def _configurationmanager(value):
    """Bind a configuration-manager dispatch to its published interface."""
    return _interface(value, ICONFIGURATIONMANAGER_IID)


def _configuration(value):
    """Bind a configuration dispatch to its published interface."""
    return _interface(value, ICONFIGURATION_IID)


def _active_configuration_view(doc):
    """Bound manager/configuration views; both stay alive in the caller's frame."""
    manager = _configurationmanager(_member(doc, "ConfigurationManager"))
    if manager is None:
        return None, None
    return manager, _configuration(_member(manager, "ActiveConfiguration"))


def _configuration_context(doc):
    """Configuration view plus the primitive name captured at acquisition.

    The name is a provenance primitive: readers that later run stateful native
    operations (mass-property recalculations, selections) must use this
    captured string instead of re-reading a borrowed configuration dispatch.
    """

    manager, configuration = _active_configuration_view(doc)
    if configuration is None:
        raise CadError("cad_configuration_missing", "the document has no active configuration")
    return manager, configuration, str(_member(configuration, "Name"))


def _return_view(name, value):
    """Published view for a mapped member return; ``None`` keeps the value as-is."""

    iid = RETURN_VIEWS.get(name)
    if iid is not None and hasattr(value, "_oleobj_"):
        return _interface(value, iid)
    element_iid = RETURN_ARRAY_VIEWS.get(name)
    if element_iid is not None and isinstance(value, (tuple, list)):
        return [_interface(item, element_iid) for item in value]
    return None


def _part_bodies(doc, body_type=0):
    """Part bodies through the published IPartDoc interface (memid 132)."""
    return _as_list(_member(_partdoc(doc), "GetBodies2", body_type, False) or ())


def _part_material(doc, query):
    """Part-level material read through the published IPartDoc interface (memid 141)."""
    return _read_material(_partdoc(doc), "GetMaterialPropertyName2", query)


def _assembly_components(doc):
    """Assembly component traversal through the published IAssemblyDoc interface (memid 118)."""
    return _as_list(_member(_assemblydoc(doc), "GetComponents", False))


def _component_document(component):
    """Model document behind a captured occurrence, on the common IModelDoc2 interface."""
    return _modeldoc2(_method(component, "GetModelDoc2"))


#: ``swComponentSuppressionState_e`` members (SolidWorks type library, verified in the
#: local typelib dump api-probe-output.txt): 0 suppressed, 1 lightweight, 2 fully
#: resolved, 3 resolved, 4 fully lightweight, 5 internal-ID mismatch.
COMPONENT_SUPPRESSED = 0
COMPONENT_LIGHTWEIGHT = 1
COMPONENT_FULLY_LIGHTWEIGHT = 4
COMPONENT_INTERNAL_ID_MISMATCH = 5
REBUILD_OPTIONS_FORCE_ALL = 2
_COMPONENT_STATES = (0, 1, 2, 3, 4)


def _component_state(component):
    """Raw ``IComponent2::GetSuppression2`` state, fail-closed.

    The vendor type library documents ``GetSuppression2`` (no deprecated-method
    fallback is used).  A mismatched internal-ID binding must never be treated as
    a resolved occurrence, and an unreadable or undocumented state is reported as
    such instead of being coerced to resolved or suppressed.
    """

    try:
        state = _method(component, "GetSuppression2")
    except Exception as error:  # noqa: BLE001 - precise refusal, never a guess
        raise CadError(
            "cad_component_state_unreadable",
            "component suppression state could not be read",
            {"error": f"{type(error).__name__}: {error}"[:200]},
        ) from error
    if type(state) is not int:
        raise CadError(
            "cad_component_state_unreadable",
            "component suppression state is not an integer",
            {"state": repr(state)[:200]},
        )
    if state == COMPONENT_INTERNAL_ID_MISMATCH:
        raise CadError(
            "cad_component_internal_id_mismatch",
            "component is bound to a document with a different internal ID",
            {"state": state, "enum": "swComponentInternalIdMismatch"},
        )
    if state not in _COMPONENT_STATES:
        raise CadError(
            "cad_component_state_unsupported",
            "component suppression state is not a documented value",
            {"state": state},
        )
    return state


def _measured_component_state(component):
    """Suppression state for a *measurement* path (mass or geometry).

    A genuinely suppressed occurrence contributes no measurement and is still
    skipped by the caller; a lightweight occurrence (state 1/4) is **not**
    suppressed and has no fully loaded bodies or mass, so it is refused instead
    of being silently dropped as though it were suppressed.
    """

    state = _component_state(component)
    if state in (COMPONENT_LIGHTWEIGHT, COMPONENT_FULLY_LIGHTWEIGHT):
        raise CadError(
            "cad_component_lightweight_unresolved",
            "a lightweight occurrence cannot be measured; resolve it before capture",
            {"state": state},
        )
    return state


def _hint_method(obj, name):
    """Resolve a method name once per dispatch; never cache its return values."""
    flag = getattr(obj, "_FlagAsMethod", None)
    if flag is not None:
        hints = vars(obj).setdefault("_description_method_hints_", set())
        if name not in hints:
            # pywin32 resolves GetIDsOfNames on every _FlagAsMethod call.
            # Reuse successful metadata; initial resolution errors still block.
            flag(name)
            hints.add(name)


def _member(obj, name, *args):
    # Flag methods BEFORE getattr: late binding may otherwise invoke them as
    # zero-argument properties, even crashing an incorrectly invoked CAD API.
    if args:
        _hint_method(obj, name)
    try:
        value = getattr(obj, name)
    except AttributeError as error:  # stable error code instead of leaking a raw AttributeError
        raise CadError("cad_member_missing", name) from error
    if hasattr(value, "_oleobj_"):
        if args:
            raise CadError("cad_member_not_callable", name)
        bound = _return_view(name, value)
        return bound if bound is not None else _dynamic(value)
    if callable(value):
        result = value(*args)
        bound = _return_view(name, result)
        return bound if bound is not None else _dynamic(result)
    if args:
        raise CadError("cad_member_not_callable", name)
    # Late-bound property results can be arrays without being callable; bind
    # mapped arrays here too instead of skipping them.
    bound = _return_view(name, value)
    return bound if bound is not None else value


def _as_list(value):
    if value is None:
        return []
    if isinstance(value, (tuple, list)):
        return [v for item in value for v in _as_list(item)]
    return [value]


def _is_text_name(value):
    return isinstance(value, str) and value.strip() != ""


def _read_only_document(doc):
    # OpenDoc6's read-only option does not propagate to every loaded reference.
    # Restrict the owned in-memory document without saving or changing its file.
    if not _member(doc, "IsOpenedReadOnly"):
        changed = _member(doc, "SetReadOnlyState", True)
        if not changed or not _member(doc, "IsOpenedReadOnly"):
            raise CadError(
                "cad_read_only_failed",
                "Cannot make the capture document read-only",
                {"path": _member(doc, "GetPathName")},
            )
    return doc


def _method(obj, name, *args):
    """Invoke ``name`` as a COM *method*, including zero-argument methods.

    Late binding needs the method hint before ``getattr``.  Without it win32com
    dispatches a zero-argument method as a property get, and SolidWorks answers
    ``DISP_E_PARAMNOTOPTIONAL`` (-2147352561) instead of returning the object.
    ``_member`` only flags names it is given arguments for, so every parameterless
    call that must reach a method (``GetProcessID`` and reference queries) goes
    through here.
    """

    _hint_method(obj, name)
    try:
        value = getattr(obj, name)
    except AttributeError as error:  # stable error code instead of leaking a raw AttributeError
        raise CadError("cad_member_missing", name) from error
    if hasattr(value, "_oleobj_"):
        raise CadError("cad_member_not_callable", name)
    if callable(value):
        result = value(*args)
        bound = _return_view(name, result)
        return bound if bound is not None else _dynamic(result)
    raise CadError("cad_member_not_callable", name)


_CURVE_END_PARAMS_FIELDS = frozenset(
    {
        "call_form",
        "success",
        "error",
        "start",
        "end",
        "is_closed",
        "is_periodic",
        "raw_outputs",
    }
)


def _read_curve_end_params(
    curve,
    *,
    variant_factory=None,
    vt_byref=None,
    vt_r8=None,
    vt_bool=None,
):
    """Read ICurve.GetEndParams through four pywin32 by-ref VARIANT outputs."""

    evidence = {
        "call_form": "pywin32-byref-variant",
        "success": False,
        "error": None,
        "start": None,
        "end": None,
        "is_closed": None,
        "is_periodic": None,
        "raw_outputs": [],
    }
    try:
        if (
            variant_factory is None
            or vt_byref is None
            or vt_r8 is None
            or vt_bool is None
        ):
            import pythoncom
            from win32com.client import VARIANT

            variant_factory = VARIANT if variant_factory is None else variant_factory
            vt_byref = pythoncom.VT_BYREF if vt_byref is None else vt_byref
            vt_r8 = pythoncom.VT_R8 if vt_r8 is None else vt_r8
            vt_bool = pythoncom.VT_BOOL if vt_bool is None else vt_bool

        start = variant_factory(vt_byref | vt_r8, 0.0)
        end = variant_factory(vt_byref | vt_r8, 0.0)
        is_closed = variant_factory(vt_byref | vt_bool, False)
        is_periodic = variant_factory(vt_byref | vt_bool, False)
        _hint_method(curve, "GetEndParams")
        success = _method(
            curve,
            "GetEndParams",
            start,
            end,
            is_closed,
            is_periodic,
        )
    except Exception as error:  # noqa: BLE001
        evidence["error"] = f"{type(error).__name__}: {error}"[:300]
        return evidence
    if type(success) is not bool:
        evidence["error"] = (
            f"GetEndParams return is not bool: {type(success).__name__}"
        )
        return evidence
    evidence["success"] = success
    if success is not True:
        evidence["error"] = "GetEndParams returned false"
        return evidence
    outputs = (start, end, is_closed, is_periodic)
    evidence["raw_outputs"] = [
        {
            "value_type": getattr(item, "varianttype", None),
            "value": getattr(item, "value", None),
        }
        for item in outputs
    ]
    values = tuple(getattr(item, "value", None) for item in outputs)
    if (
        type(values[0]) not in (int, float)
        or type(values[1]) not in (int, float)
        or not math.isfinite(float(values[0]))
        or not math.isfinite(float(values[1]))
        or type(values[2]) is not bool
        or type(values[3]) is not bool
    ):
        evidence["success"] = False
        evidence["error"] = "GetEndParams by-ref outputs have unsupported types"
        return evidence
    evidence.update(
        {
            "start": float(values[0]),
            "end": float(values[1]),
            "is_closed": values[2],
            "is_periodic": values[3],
        }
    )
    return evidence


def _active_configuration(doc):
    """Name of the configuration a document currently has active."""

    _manager, active = _active_configuration_view(doc)
    name = _member(active, "Name") if active is not None else None
    return str(name) if name else None


def _capture_document_identity(doc, path, phase, state=None):
    """Read and preserve capture identity without leaking a raw COM failure."""

    detail = {"path": str(path), "phase": phase}
    if state is not None:
        detail["state"] = state
    try:
        document = str(_member(doc, "GetPathName") or "")
    except Exception as error:  # noqa: BLE001 - raw COM failures need stable evidence
        detail["error"] = f"{type(error).__name__}: {error}"[:300]
        if state is None:
            detail["state"] = _discovery_document_state(doc)
        raise CadError(
            "cad_document_identity",
            "Capture document path could not be read",
            detail,
        ) from error
    if not document or not document_paths_match(document, path):
        detail["document"] = document
        raise CadError(
            "cad_document_identity",
            "Capture document has no matching path identity",
            detail,
        )
    try:
        configuration = _active_configuration(doc)
    except Exception as error:  # noqa: BLE001 - raw COM failures need stable evidence
        detail["document"] = document
        detail["error"] = f"{type(error).__name__}: {error}"[:300]
        if state is None:
            detail["state"] = _discovery_document_state(doc)
        raise CadError(
            "cad_configuration_mismatch",
            "Capture document configuration could not be read",
            detail,
        ) from error
    if not configuration:
        detail["document"] = document
        raise CadError(
            "cad_configuration_mismatch",
            "Capture document has no active configuration",
            detail,
        )
    return document, configuration


def _capture_topology_sort_key(row):
    """Stable ordering for exact before/after capture topology comparison."""

    return tuple(
        "" if row.get(key) is None else str(row.get(key))
        for key in (
            "instance",
            "normalized_path",
            "document_type",
            "referenced_configuration",
            "depth",
            "suppression_state",
            "storage_scope",
            "provenance_state",
        )
    )


def _discovery_document_state(doc):
    """Observe saved/rebuild status; never refresh or rewrite engineering data."""

    state = {}
    for name, reader, expected in (
        ("save_flag", lambda: _method(doc, "GetSaveFlag"), bool),
        ("needs_rebuild2_value", lambda: _member(_member(doc, "Extension"), "NeedsRebuild2"), int),
        ("read_only", lambda: _member(doc, "IsOpenedReadOnly"), bool),
    ):
        try:
            value = reader()
            if type(value) is not expected:
                raise ValueError(f"unexpected native value: {value!r}")
            state[name] = value
        except Exception as error:  # noqa: BLE001 - unread state is explicit, never assumed clean
            state[name] = None
            state[name + "_error"] = f"{type(error).__name__}: {error}"[:300]
    try:
        _manager, active = _active_configuration_view(doc)
        state["configuration"] = str(_member(active, "Name") or "")
        value = _member(active, "NeedsRebuild")
        if type(value) is not bool:
            raise ValueError(f"unexpected native value: {value!r}")
        state["configuration_needs_rebuild"] = value
    except Exception as error:  # noqa: BLE001
        state["configuration_needs_rebuild"] = None
        state["configuration_needs_rebuild_error"] = f"{type(error).__name__}: {error}"[:300]
    return state


def _configuration_rebuild_rows(doc):
    """Return every configuration's raw rebuild state without changing the document."""

    names = []
    for item in _as_list(_member(doc, "GetConfigurationNames")):
        name = item if isinstance(item, str) else _member(_dynamic(item), "Name")
        if not isinstance(name, str) or not name:
            raise ValueError("configuration name is not text")
        names.append(name)
    if not names or len(set(names)) != len(names):
        raise ValueError("configuration names are empty or duplicated")
    rows = []
    for name in names:
        configuration = _member(doc, "GetConfigurationByName", name)
        needs_rebuild = _member(configuration, "NeedsRebuild")
        add_rebuild_save_mark = _member(configuration, "AddRebuildSaveMark")
        if type(needs_rebuild) is not bool or type(add_rebuild_save_mark) is not bool:
            raise ValueError(f"configuration flags are invalid: {name}")
        rows.append(
            {
                "name": name,
                "needs_rebuild": needs_rebuild,
                "add_rebuild_save_mark": add_rebuild_save_mark,
            }
        )
    return rows


def _assembly_rebuild_controls(doc):
    """Record rebuild controls; capture must never change these authored flags."""

    assembly = _assemblydoc(doc)
    try:
        enable = _member(assembly, "EnableAssemblyRebuild")
        enable_value = enable if type(enable) is bool else None
    except Exception as error:  # noqa: BLE001 - diagnostic-only native property
        enable_value = None
        enable_error = f"{type(error).__name__}: {error}"[:300]
    else:
        enable_error = None
    try:
        lightweight = _method(assembly, "GetLightWeightComponentCount")
        lightweight_value = lightweight if type(lightweight) is int else None
    except Exception as error:  # noqa: BLE001 - diagnostic-only native property
        lightweight_value = None
        lightweight_error = f"{type(error).__name__}: {error}"[:300]
    else:
        lightweight_error = None
    row = {
        "enable_assembly_rebuild": enable_value,
        "lightweight_component_count": lightweight_value,
    }
    if enable_error is not None:
        row["enable_assembly_rebuild_error"] = enable_error
    if lightweight_error is not None:
        row["lightweight_component_count_error"] = lightweight_error
    return row


def _select_configuration(doc, configuration, occurrence):
    """Select and check the configuration before reading a shared document."""
    detail = {"component": occurrence, "configuration": configuration}
    try:
        if not _is_text_name(configuration):
            raise ValueError("the referenced configuration is empty")
        if _active_configuration(doc) != configuration:
            selected = _method(doc, "ShowConfiguration2", configuration)
            if selected is not True:
                raise ValueError("ShowConfiguration2 did not report success")
        actual = _active_configuration(doc)
        if actual != configuration:
            raise ValueError(f"the active configuration is {actual!r}")
    except Exception as error:
        raise CadError(
            "cad_configuration_unreadable",
            "the document could not be read in its referenced configuration",
            {**detail, "error": str(error)},
        ) from error


@contextmanager
def _temporary_configuration(get_document, configuration, occurrence):
    """Acquire the exact owned document at each configuration boundary.

    Do not retain a shared document's dispatch interface across native reads.
    The getter resolves the recorded path again; it never opens another
    document or retries a failed selection. A lost document blocks restoration.
    """
    previous = _active_configuration(get_document())
    if not _is_text_name(previous):
        raise CadError(
            "cad_configuration_unreadable",
            "the document has no readable configuration to restore",
            {"component": occurrence, "configuration": previous, "phase": "before_read"},
        )
    try:
        doc = get_document()
        _select_configuration(doc, configuration, occurrence)
        yield doc, previous
    finally:
        pending = sys.exception()
        try:
            _select_configuration(get_document(), previous, occurrence)
        except Exception as error:
            restoration = (
                error
                if isinstance(error, CadError)
                else CadError(
                    "cad_configuration_unreadable",
                    "the document could not be restored to its prior configuration",
                    {"component": occurrence, "configuration": previous, "error": str(error)},
                )
            )
            detail = restoration.detail if isinstance(restoration.detail, dict) else {"detail": restoration.detail}
            restoration.detail = {**detail, "phase": "restore"}
            if pending is None:
                if restoration is error:
                    raise
                raise restoration from error
            pending.add_note(f"Configuration restoration also failed: {restoration} ({restoration.detail!r})")


def _looks_like_path(value):
    text = str(value)
    if not text:
        return False
    if os.path.isabs(text) or (len(text) > 1 and text[1] == ":") or text.startswith("\\\\"):
        return True
    return ("\\" in text) or ("/" in text)


def normalise_dependency_entries(items, document_path):
    """Absolute paths from a ``GetDependencies2`` result array.

    With ``Searchflag=True`` SolidWorks returns ``[display name, full path]``
    pairs, so taking every element as a path turns the display names into bogus
    (and often truncated) dependencies.  Pair them up and keep the full path.
    """

    values = [str(item) for item in _as_list(items) if item]
    collected = []
    index = 0
    while index < len(values):
        first = values[index]
        second = values[index + 1] if index + 1 < len(values) else None
        if second is not None and _looks_like_path(second) and not _looks_like_path(first):
            collected.append(second)
            index += 2
            continue
        collected.append(first)
        index += 1
    return sorted({resolve_dependency_path(value, document_path) for value in collected})


def resolve_dependency_path(value, document_path):
    """Absolute path of one ``GetDependencies2`` entry.

    SolidWorks returns bare file names for references it resolved relative to the
    assembly, so joining them against the document directory is what makes the
    allowed-roots check and the closure verification meaningful.  Windows paths
    are handled with ``ntpath`` even when this runs on a build host, so the same
    input produces the same answer everywhere.

    Already absolute entries (drive-qualified or UNC) pass through unchanged.
    """

    if not value:
        return value
    text = str(value)
    document = str(document_path or "")
    windows_style = bool(re.match(r"^[A-Za-z]:", text)) or text.startswith("\\\\") or "\\" in text
    windows_style = windows_style or bool(re.match(r"^[A-Za-z]:", document)) or "\\" in document
    module = ntpath if windows_style else posixpath
    if module.isabs(text) or (len(text) > 1 and text[1] == ":") or text.startswith("\\\\"):
        return module.normpath(text)
    base = module.dirname(document)
    if not base:
        return module.normpath(text)
    return module.normpath(module.join(base, text))


def closed_document_dependencies(app, document_path, *, max_documents=4096):
    """Stored direct dependencies of a saved document, without opening it.

    Exactly the documented no-search form is used:
    ``GetDocumentDependencies2(path, False, False, False)``.  With Searchflag
    false SolidWorks reports "where the documents were last saved", so the
    result is the saved document's stored references: two strings per document
    (the stored file name and the stored location).  The stored fields are
    preserved verbatim; a missing or empty location is refused instead of
    being guessed from the name, because a name is not an authoritative path.
    The returned ``path`` is a normalized convenience value, not proof that
    the reference belongs to the admitted source inventory.

    No document is opened, no search rules run, no geometry is read.
    """

    if isinstance(max_documents, bool) or not isinstance(max_documents, int) or max_documents <= 0:
        raise CadError(
            "dependency_limit_invalid",
            "max_documents must be a positive integer",
            {"max_documents": repr(max_documents)},
        )
    result = _method(app, "GetDocumentDependencies2", str(document_path), False, False, False)
    if result is None:
        return []
    if isinstance(result, (str, bytes)) or not isinstance(result, (list, tuple)):
        raise CadError(
            "dependency_list_invalid",
            "the dependency result must be an array of strings",
            {"type": type(result).__name__},
        )
    raw = list(result)
    if len(raw) % 2:
        raise CadError("dependency_list_invalid", str(document_path), {"entries": len(raw)})
    documents = len(raw) // 2
    if documents > max_documents:
        raise CadError(
            "dependency_list_too_large",
            str(document_path),
            {"documents": documents, "limit": max_documents},
        )
    entries = []
    for index in range(0, len(raw), 2):
        name = raw[index]
        location = raw[index + 1]
        if not isinstance(name, str) or not name.strip():
            raise CadError("dependency_list_invalid", str(document_path), {"index": index})
        if not isinstance(location, str) or not location.strip():
            raise CadError(
                "dependency_location_missing",
                "a stored reference has no usable location; a name is not a path",
                {"index": index + 1, "name": name},
            )
        entries.append(
            {
                "name": name,
                "location": location,
                "path": resolve_dependency_path(location, str(document_path)),
            }
        )
    return entries


def normalize_document_path(path):
    return (path or "").replace("/", "\\").strip().lower()


def document_paths_match(active, requested):
    a, b = normalize_document_path(active), normalize_document_path(requested)
    return bool(a and b and (a == b or ("\\" not in b and a.rsplit("\\", 1)[-1] == b)))


#: FolderType value used to pin and read the *session* search folders
#: (documented ``ISldWorks.GetSearchFolders(FolderType)`` /
#: ``SetSearchFolders(FolderType, Folders)``; local typelib dump
#: reviews/urdf-pr277-20261006/sw-session-methods.json, CHM ids
#: 1048618/1048619).  0 is what the local dump carried (member named
#: ``swDocumentType``), but the documented enum name/value association is not
#: independently verified and may be reserved; the Set return value and the
#: Get read-back are the evidence, never this constant.
SEARCH_FOLDER_DOCUMENT_TYPE = 0


def _search_folder_list(value):
    if value is None:
        return []
    if isinstance(value, str):
        return [item for item in value.split(";") if item]
    if isinstance(value, (tuple, list)):
        return [str(item) for item in value if str(item)]
    return [str(value)]


def read_search_folders(app, folder_type=SEARCH_FOLDER_DOCUMENT_TYPE):
    """The session search folders as SolidWorks reports them, with the raw value."""

    try:
        raw = _member(app, "GetSearchFolders", folder_type)
    except Exception as error:  # noqa: BLE001 - an unreadable configuration is a refusal
        raise CadError(
            "cad_search_folders_unreadable",
            "session search folders could not be read",
            {"error": f"{type(error).__name__}: {error}"[:200]},
        ) from error
    return {"raw": repr(raw)[:300], "folders": _search_folder_list(raw)}


def pin_search_folders(app, root, folder_type=SEARCH_FOLDER_DOCUMENT_TYPE):
    """Pin the session search scope to exactly ``root`` and read back the result.

    Only the staged root is written; a readable ``False`` return and any
    read-back that does not show exactly the pinned root refuse the run, so
    reference resolution can never proceed on machine search settings by
    accident.
    """

    before = read_search_folders(app, folder_type)
    try:
        written = _member(app, "SetSearchFolders", folder_type, str(root))
    except Exception as error:  # noqa: BLE001 - fail closed instead of resolving by chance
        raise CadError(
            "cad_search_folders_unwritable",
            "staged search folders could not be pinned",
            {"root": str(root), "error": f"{type(error).__name__}: {error}"[:200]},
        ) from error
    after = read_search_folders(app, folder_type)
    evidence = {
        "folder_type": folder_type,
        "before": before["folders"],
        "before_raw": before["raw"],
        "pinned": [str(root)],
        "observed_after_pin": after["folders"],
        "after_raw": after["raw"],
        "set_return": repr(written)[:80],
    }
    observed = [str(item).casefold() for item in after["folders"]]
    if written is False or observed != [str(root).casefold()]:
        raise CadError(
            "cad_search_folders_not_applied",
            "the session did not report the pinned staged search root",
            evidence,
        )
    return evidence


def _stage_relative(root, value) -> str | None:
    """Path of ``value`` under ``root`` in portable form, or ``None``."""

    try:
        relative = Path(str(value)).resolve().relative_to(Path(str(root)).resolve())
    except (OSError, ValueError):
        return None
    return relative.as_posix()


def _index_spelling(index, relative) -> str | None:
    """The upload-index spelling of a staged-relative path (case-tolerant)."""

    if relative in index:
        return relative
    folded = relative.casefold()
    matches = [name for name in index if name.casefold() == folded]
    return matches[0] if len(matches) == 1 else None


def _matches_assembly_root(reference, root, reference_full, reference_name, assembly_full) -> bool:
    """True when a mate entity references the owning assembly's own root component.

    Identity is strict: the walked assembly document, the root object read from
    ``GetRootComponent3`` under the assembly's current configuration, and the
    entity's reference must all carry the same normalized full document path,
    and the reference ``Name2`` must equal the root ``Name2``.  A bare name or
    basename match is never accepted.  ``IsRoot`` must read as exactly the
    boolean True: a readable False, a non-boolean value and an unreadable
    reading all fail closed.
    """

    if root is None:
        return False
    try:
        root_full = str(_method(root, "GetPathName") or "")
        root_name = str(_member(root, "Name2") or "")
    except Exception:  # noqa: BLE001 - an unreadable root object cannot prove identity
        return False
    reference_key = normalize_document_path(reference_full)
    root_key = normalize_document_path(root_full)
    assembly_key = normalize_document_path(str(assembly_full))
    if not reference_key or reference_key != root_key or reference_key != assembly_key:
        return False
    if not root_name or reference_name != root_name:
        return False
    try:
        flag = _member(reference, "IsRoot")
    except Exception:  # noqa: BLE001 - an unreadable IsRoot is no proof
        return False
    return type(flag) is bool and flag is True


def _inertia_from_raw(values, component):
    """Parse the documented nine-value ``GetMomentOfInertia(0)`` full tensor.

    Anything else is rejected: an array length never determines an unknown
    API's reference point, axes or product convention.
    """
    data = list(map(float, values))
    if not all(math.isfinite(v) for v in data):
        raise CadError("cad_mass_property_inertia_nonfinite", component)
    if len(data) == 3:
        raise CadError("cad_mass_property_principal_only", component)
    if len(data) == 9:
        scale = max(max(map(abs, data)), 1e-30)
        if any(abs(data[i] - data[j]) > scale * 1e-10 for i, j in ((1, 3), (2, 6), (5, 7))):
            raise CadError("cad_mass_property_inertia_asymmetric", component)
        return tuple(tuple(data[i : i + 3]) for i in (0, 3, 6)), "full9"
    raise CadError("cad_mass_property_inertia_unsupported", component, {"length": len(data)})


def transform_from_solidworks(values):
    """SW row-vector rotation + xyz + scale -> URDF column-vector 4x4."""
    a = tuple(map(float, values))
    if len(a) != 16 or not all(math.isfinite(v) for v in a):
        raise CadError("cad_transform_invalid", "expected 16 finite MathTransform values")
    if abs(a[12] - 1.0) > 1e-10:
        raise CadError("cad_transform_scaled", "scaled instances need explicit handling", a)
    r = [[a[j * 3 + i] for j in range(3)] for i in range(3)]
    if any(
        abs(sum(r[k][i] * r[k][j] for k in range(3)) - (1 if i == j else 0)) > 1e-9 for i in range(3) for j in range(3)
    ):
        raise CadError("cad_transform_invalid", "rotation is not orthonormal")
    det = (
        r[0][0] * (r[1][1] * r[2][2] - r[1][2] * r[2][1])
        - r[0][1] * (r[1][0] * r[2][2] - r[1][2] * r[2][0])
        + r[0][2] * (r[1][0] * r[2][1] - r[1][1] * r[2][0])
    )
    if abs(det - 1.0) > 1e-9:
        raise CadError("cad_transform_reflection", "reflection is not a rigid rotation")
    return (*tuple(v for i in range(3) for v in (*r[i], a[9 + i])), 0.0, 0.0, 0.0, 1.0)


def _hash(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_material(obj, method, configuration):
    """Physical material API, not the similarly named appearance/color API."""
    import pythoncom

    database = _win32().VARIANT(pythoncom.VT_BYREF | pythoncom.VT_BSTR, "")
    name = _member(obj, method, configuration, database)
    if not isinstance(name, str) or not isinstance(database.value, str):
        raise TypeError("Unexpected material name/database result")
    return {"name": name.strip(), "database": database.value.strip()}


def _material_assignments_document(doc, bodies):
    """Read the physical material assignment of every included solid, without editing CAD.

    A positive Density is insufficient: an unassigned imported solid can still
    have a default density. Body material takes precedence over part material.
    This verifies assignment coverage, not correctness against real hardware.

    A missing assignment raises ``cad_material_provenance_missing`` with the full
    row evidence in the detail; callers decide whether that deficiency is fatal
    (strict capture) or degrades to a recorded warning beside the measured values.
    """
    try:
        _manager, active = _active_configuration_view(doc)
        configuration = str(_member(active, "Name"))
        configurations = list(_member(doc, "GetConfigurationNames") or ())
        if not configuration or configuration not in configurations:
            raise ValueError("Active configuration is not in the document")
        # The API documents an empty argument for a sole Default configuration.
        query = "" if configurations == ["Default"] else configuration
        part = _part_material(doc, query)
        rows = []
        for index, body in enumerate(bodies):
            body = _dynamic(body)
            material = _read_material(body, "GetMaterialPropertyName", query)
            rows.append(
                {
                    "index": index,
                    "name": str(_member(body, "Name")),
                    "material": material,
                    "effective_source": "body" if material["name"] else "part",
                }
            )
    except Exception as exc:
        raise CadError(
            "cad_material_read_failed", "Cannot verify physical material assignment", {"error": str(exc)}
        ) from exc
    evidence = {
        "schema_version": "swbridge.material-assignment/v1",
        "configuration": configuration,
        "query_configuration": query,
        "part_api": "IPartDoc.GetMaterialPropertyName2",
        "body_api": "IBody2.GetMaterialPropertyName",
        "part": part,
        "body_count": len(rows),
        "bodies": rows,
    }
    missing = [
        r["index"]
        for r in rows
        if not (
            (r["material"] if r["effective_source"] == "body" else part)["name"]
            and (r["material"] if r["effective_source"] == "body" else part)["database"]
        )
    ]
    if not rows or missing:
        raise CadError(
            "cad_material_provenance_missing",
            "Every solid requires an explicit physical material; default density is not evidence",
            {"missing_body_indices": missing, "material_assignment": evidence},
        )
    return evidence


def unverified_material_record(exc, configuration) -> dict:
    """The material evidence kept when a capture cannot verify a part's assignment.

    A missing explicit material is a warnable deficiency of the document: the measured CAD values
    are still recorded, and the unverified assignment travels verbatim beside them.  In
    documented-table mode an unreadable assignment also degrades to this record.  The CAD error
    carries the full part/body assignment in its detail; reducing the fallback to a code and a
    message would throw away what the document actually held.  Every detail key the reader
    produced travels with the record instead.
    """

    record = {
        "schema_version": "swbridge.material-assignment/v1",
        "configuration": configuration,
        "query_configuration": "",
        "unverified_reason": exc.code,
        "message": exc.message,
    }
    detail = getattr(exc, "detail", None)
    if isinstance(detail, dict):
        for key in ("missing_body_indices", "material_assignment"):
            if key in detail:
                record[key] = detail[key]
    return record


def _instance_error(name: str, parent: str | None, depth: int, stage: str, error: BaseException) -> dict:
    """One unreadable component instance, naming the traversal stage that failed."""

    return {
        "name": name,
        "parent": parent,
        "depth": depth,
        "stage": stage,
        "error": str(getattr(error, "code", "") or type(error).__name__),
        "message": " ".join(str(error).split())[:200],
    }


#: SW2026 ``swMateType_e`` values, verified against the installed typelib.
_SW_MATE_TYPES = {
    0: "coincident",
    1: "concentric",
    2: "perpendicular",
    3: "parallel",
    4: "tangent",
    5: "distance",
    6: "angle",
    7: "unknown",
    8: "symmetric",
    9: "camfollower",
    10: "gear",
    11: "width",
    12: "locktosketch",
    13: "rackpinion",
    14: "maxmates",
    15: "path",
    16: "lock",
    17: "screw",
    18: "linearcoupler",
    19: "universaljoint",
    20: "coordinate",
    21: "slot",
    22: "hinge",
    23: "slider",
    24: "profilecenter",
    25: "magnetic",
}


def _merge_property_scopes(document: dict, configuration: dict) -> dict:
    """Merge document/configuration properties; conflicting ``dp.*`` values block.

    Ordinary CAD properties keep the usual configuration-overrides-document
    semantics.  A ``dp.*`` key declared with different non-empty values in the
    two scopes is ambiguous by contract and raises instead of picking one.
    """

    merged = dict(document)
    for key, value in configuration.items():
        if key.startswith("dp.") and key in document:
            left = str(document[key]).strip()
            right = str(value).strip()
            if left and right and left != right:
                raise CadError(
                    "cad_property_conflict",
                    "a dp.* property differs between the document and configuration scopes",
                    {"field": key, "document": left, "configuration": right},
                )
        merged[key] = value
    return merged


def _read_property_scope(doc, scope: str) -> dict:
    detail = {"configuration": scope}
    try:
        detail["document"] = _member(doc, "GetPathName")
        manager = _member(_member(doc, "Extension"), "CustomPropertyManager", scope)
        if manager is None:
            raise ValueError("the custom property manager is unavailable")
        names = _as_list(_method(manager, "GetNames"))
        if any(not _is_text_name(name) for name in names):
            raise ValueError("custom property names are not nonempty strings")
    except Exception as error:
        raise CadError(
            "cad_property_unreadable", "custom properties could not be enumerated", {**detail, "error": str(error)}
        ) from error
    values: dict = {}
    for name in names:
        try:
            import pythoncom

            variant = _win32().VARIANT
            raw = variant(pythoncom.VT_BYREF | pythoncom.VT_BSTR, "")
            resolved = variant(pythoncom.VT_BYREF | pythoncom.VT_BSTR, "")
            was_resolved = variant(pythoncom.VT_BYREF | pythoncom.VT_BOOL, False)
            linked = variant(pythoncom.VT_BYREF | pythoncom.VT_BOOL, False)
            status = _method(manager, "Get6", name, False, raw, resolved, was_resolved, linked)
            if type(status) is not int or status != 2:
                raise ValueError(f"Get6 did not return a resolved value (status {status!r})")
            if was_resolved.value is not True or type(linked.value) is not bool:
                raise ValueError("Get6 did not confirm a resolved property and readable link state")
            if not isinstance(raw.value, str) or not isinstance(resolved.value, str):
                raise ValueError("Get6 returned a non-string property value")
            values[name] = resolved.value
        except Exception as error:
            raise CadError(
                "cad_property_unreadable",
                "a custom property could not be resolved",
                {**detail, "property": name, "error": str(error)},
            ) from error
    return values


def _custom_properties(doc, configuration=None):
    """Document and configuration custom properties with conflict detection."""

    document = _read_property_scope(doc, "")
    if not configuration:
        return document
    configuration_values = _read_property_scope(doc, str(configuration))
    if not configuration_values:
        return document
    return _merge_property_scopes(document, configuration_values)


def _plane_or_cylinder(target):
    """Recorded surface geometry of one mate entity, in its component frame."""

    try:
        surface = _dynamic(_member(target, "GetSurface"))
    except Exception:  # noqa: BLE001
        surface = None
    if surface is not None:
        try:
            if _member(surface, "IsCylinder"):
                params = [float(value) for value in (_as_list(_member(surface, "CylinderParams")) or [])]
                if len(params) == 7:
                    if not all(math.isfinite(value) for value in params):
                        raise CadError(
                            "cad_geometry_nonfinite",
                            "a recorded cylinder parameter is not finite",
                            {"feature": _feature_name(target)},
                        )
                    direction = params[3:6]
                    norm = math.hypot(*direction)
                    if norm > 0 and params[6] > 0:
                        return {
                            "cylinder": {
                                "point": params[0:3],
                                "direction": [value / norm for value in direction],
                                "radius": params[6],
                            }
                        }
        except CadError as error:
            if error.code != "cad_member_missing":
                raise
        except Exception:  # noqa: BLE001
            pass
        try:
            if _member(surface, "IsPlane"):
                params = [float(value) for value in (_as_list(_member(surface, "PlaneParams")) or [])]
                if len(params) == 6:
                    if not all(math.isfinite(value) for value in params):
                        raise CadError(
                            "cad_geometry_nonfinite",
                            "a recorded plane parameter is not finite",
                            {"feature": _feature_name(target)},
                        )
                    # ISurface.PlaneParams is normal xyz, then point xyz.
                    # A point's length cannot identify its role in the API.
                    normal = params[0:3]
                    norm = math.hypot(*normal)
                    if norm > 0:
                        return {"plane": {"normal": [value / norm for value in normal], "point": params[3:6]}}
        except CadError as error:
            if error.code != "cad_member_missing":
                raise
        except Exception:  # noqa: BLE001
            pass
    try:
        curve = _dynamic(_member(target, "GetCurve"))
        if curve is not None and _member(curve, "IsCircle"):
            values = [float(value) for value in (_as_list(_member(curve, "CircleParams")) or [])]
            if len(values) == 7:
                normal = values[3:6]
                center = values[0:3]
                if not all(math.isfinite(value) for value in (*center, *normal, values[6])):
                    raise CadError("cad_geometry_nonfinite", "a recorded circle parameter is not finite")
                norm = math.hypot(*normal)
                if norm > 0 and values[6] > 0:
                    return {
                        "circle": {
                            "center": values[0:3],
                            "normal": [value / norm for value in normal],
                            "radius": values[6],
                        }
                    }
    except CadError as error:
        if error.code != "cad_member_missing":
            raise
    except Exception:  # noqa: BLE001
        pass
    try:
        point = [float(value) for value in (_as_list(_method(target, "GetPoint")) or [])]
        if len(point) == 3:
            if not all(math.isfinite(value) for value in point):
                raise CadError("cad_geometry_nonfinite", "a recorded vertex is not finite", {})
            return {"point": point}
    except CadError as error:
        if error.code != "cad_member_missing":
            raise
    except Exception:  # noqa: BLE001
        pass
    return {}


def _feature_name(target):
    try:
        feature = _dynamic(_member(target, "GetFeature"))
        name = _member(feature, "Name")
        return str(name) if _is_text_name(name) else None
    except Exception:  # noqa: BLE001
        return None


#: Geometric classes of a mate entity reference read from the native
#: ``ReferenceType`` member for the documented point/line/plane/cylinder/cone
#: kinds; every other value is recorded raw so no class is ever guessed.
#: ``IMateEntity2::ReferenceType`` is documented as obsolete (superseded by
#: ``IMate2Entity::Reference2``), and the published ``IMateEntity2::EntityParams``
#: layout table documents no Circle row — circle-class values therefore stay raw
#: until a controlled probe pins the interface/enum/layout triple.
MATE_ENTITY_REFERENCE_TYPES = {
    0: "Point",
    1: "Line",
    3: "Plane",
    4: "Cylinder",
    7: "Cone",
}

#: Minimum documented ``EntityParams`` length per geometric class.  Published
#: contract (IMateEntity2.EntityParams, with the layout remarks on
#: IGetEntityParams): the array is ``[point xyz, vector ijk, radius1,
#: radius2]`` and all coordinate information is in the owning (mate)
#: assembly's model space — the vector is the line direction, the plane
#: normal, or the cylinder/cone axis.  Documented property:
#: https://help.solidworks.com/2026/english/api/sldworksapi/SOLIDWORKS.Interop.sldworks~SOLIDWORKS.Interop.sldworks.IMateEntity2~EntityParams.html  # noqa: E501
MATE_ENTITY_PARAM_MINIMUMS = {
    0: 3,
    1: 6,
    3: 6,
    4: 7,
    7: 8,
}

_IDENTITY_FRAME = (
    (1.0, 0.0, 0.0, 0.0),
    (0.0, 1.0, 0.0, 0.0),
    (0.0, 0.0, 1.0, 0.0),
    (0.0, 0.0, 0.0, 1.0),
)


def _decode_mate_entity_params(reference_type, params):
    """Decode the documented EntityParams layout; nothing is guessed."""

    minimum = MATE_ENTITY_PARAM_MINIMUMS.get(reference_type)
    if minimum is None:
        return None, None
    if len(params) < minimum:
        return None, (
            "EntityParams has "
            + str(len(params))
            + " values; the documented layout for "
            + MATE_ENTITY_REFERENCE_TYPES[reference_type]
            + " needs at least "
            + str(minimum)
        )
    point = params[0:3]
    if reference_type == 0:
        return {"kind": "point", "point": point}, None
    vector = params[3:6]
    norm = math.hypot(*vector)
    if norm <= 0:
        return None, "EntityParams vector is zero-length"
    unit = [value / norm for value in vector]
    if reference_type == 1:
        return {"kind": "line", "point": point, "direction": unit}, None
    if reference_type == 3:
        return {"kind": "plane", "point": point, "normal": unit}, None
    radius = params[6]
    if radius <= 0:
        return None, "EntityParams radius is not positive"
    if reference_type == 4:
        return {"kind": "cylinder", "point": point, "direction": unit, "radius": radius}, None
    radius2 = params[7]
    if radius2 <= 0:
        return None, "EntityParams second radius is not positive"
    return {"kind": "cone", "point": point, "direction": unit, "radius": radius, "radius2": radius2}, None


def _mate_entity_reference(entity):
    """Documented mate-entity provenance and geometry for one entity.

    ``EntityParams`` is the published ``[point xyz, vector ijk, radius1,
    radius2]`` array in the owning assembly's model space; the layout is
    decoded only for the documented point/line/plane/cylinder/cone kinds and
    geometric classes are never conflated.  ``ReferenceType`` (legacy
    geometric class) and ``ReferenceType2`` are recorded as provenance.  A
    missing or malformed reading is recorded explicitly and never silently
    dropped; decoded geometry is withheld whenever any reading is malformed.
    """

    record = {
        "source": "mate-entity-params",
        "reference_type": None,
        "reference_type_name": None,
        "reference_type2": None,
        "reference_type2_error": None,
        "space": "mate-assembly",
        "params": None,
        "geometry": None,
        "error": None,
    }
    try:
        reference_type = _member(entity, "ReferenceType")
    except Exception as error:  # noqa: BLE001 - provenance must not abort the entity read
        reference_type = None
        record["error"] = f"{type(error).__name__}: {error}"
    try:
        reference_type2 = _member(entity, "ReferenceType2")
    except Exception as error:  # noqa: BLE001 - provenance-only reading
        reference_type2 = None
        record["reference_type2_error"] = f"{type(error).__name__}: {error}"
    try:
        raw_params = _member(entity, "EntityParams")
    except Exception as error:  # noqa: BLE001 - provenance must not abort the entity read
        raw_params = None
        if record["error"] is None:
            record["error"] = f"{type(error).__name__}: {error}"
    if type(reference_type) is int:
        record["reference_type"] = reference_type
        record["reference_type_name"] = MATE_ENTITY_REFERENCE_TYPES.get(reference_type, f"raw:{reference_type}")
    else:
        record["error"] = record["error"] or f"ReferenceType is not a native integer ({type(reference_type).__name__})"
    if type(reference_type2) is int:
        record["reference_type2"] = reference_type2
    else:
        record["reference_type2_error"] = (
            record["reference_type2_error"]
            or f"ReferenceType2 is not a native integer ({type(reference_type2).__name__})"
        )
    if raw_params is None:
        if record["error"] is None:
            record["error"] = "EntityParams is unavailable"
        return record
    values = []
    for item in _as_list(raw_params):
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            if record["error"] is None:
                record["error"] = f"EntityParams contains a non-numeric value ({type(item).__name__})"
            return record
        number = float(item)
        if not math.isfinite(number):
            if record["error"] is None:
                record["error"] = "EntityParams contains a non-finite value"
            return record
        values.append(number)
    record["params"] = values
    if record["error"] is None:
        record["geometry"], record["error"] = _decode_mate_entity_params(record["reference_type"], values)
    return record


def _frame_matrix(values):
    """Recorded flattened 4x4 occurrence transform, or None when unusable.

    Only a proper rigid placement is accepted: finite 16 values, a final
    ``[0, 0, 0, 1]`` row, an orthonormal rotation block and a right-handed
    (determinant +1) rotation — the same conditions ``transform_from_solidworks``
    proves at capture time.
    """

    try:
        numbers = [float(value) for value in values or ()]
    except (TypeError, ValueError):
        return None
    if len(numbers) != 16 or not all(math.isfinite(value) for value in numbers):
        return None
    rows = [numbers[0:4], numbers[4:8], numbers[8:12], numbers[12:16]]
    if any(abs(rows[3][i] - (1.0 if i == 3 else 0.0)) > 1e-9 for i in range(4)):
        return None
    rotation = [[rows[i][j] for j in range(3)] for i in range(3)]
    if any(
        abs(sum(rotation[k][i] * rotation[k][j] for k in range(3)) - (1.0 if i == j else 0.0)) > 1e-9
        for i in range(3)
        for j in range(3)
    ):
        return None
    determinant = (
        rotation[0][0] * (rotation[1][1] * rotation[2][2] - rotation[1][2] * rotation[2][1])
        - rotation[0][1] * (rotation[1][0] * rotation[2][2] - rotation[1][2] * rotation[2][0])
        + rotation[0][2] * (rotation[1][0] * rotation[2][1] - rotation[1][1] * rotation[2][0])
    )
    if abs(determinant - 1.0) > 1e-9:
        return None
    return rows


def _compose_frames(left, right):
    """Column-vector 4x4 product of two row-major frames."""

    return [[sum(left[row][k] * right[k][column] for k in range(4)) for column in range(4)] for row in range(4)]


def _rigid_inverse(frame):
    """Inverse of a rigid row-major [R|t] frame."""

    rotation = [[frame[i][j] for j in range(3)] for i in range(3)]
    translation = [frame[i][3] for i in range(3)]
    inverse_rotation = [[rotation[j][i] for j in range(3)] for i in range(3)]
    inverse_translation = [-sum(inverse_rotation[i][k] * translation[k] for k in range(3)) for i in range(3)]
    return [
        [inverse_rotation[0][0], inverse_rotation[0][1], inverse_rotation[0][2], inverse_translation[0]],
        [inverse_rotation[1][0], inverse_rotation[1][1], inverse_rotation[1][2], inverse_translation[1]],
        [inverse_rotation[2][0], inverse_rotation[2][1], inverse_rotation[2][2], inverse_translation[2]],
        [0.0, 0.0, 0.0, 1.0],
    ]


def _frame_point(frame, point):
    return [
        frame[0][0] * point[0] + frame[0][1] * point[1] + frame[0][2] * point[2] + frame[0][3],
        frame[1][0] * point[0] + frame[1][1] * point[1] + frame[1][2] * point[2] + frame[1][3],
        frame[2][0] * point[0] + frame[2][1] * point[1] + frame[2][2] * point[2] + frame[2][3],
    ]


def _frame_vector(frame, vector):
    return [
        frame[0][0] * vector[0] + frame[0][1] * vector[1] + frame[0][2] * vector[2],
        frame[1][0] * vector[0] + frame[1][1] * vector[1] + frame[1][2] * vector[2],
        frame[2][0] * vector[0] + frame[2][1] * vector[1] + frame[2][2] * vector[2],
    ]


def _localize_mate_entity_reference(block, component, owner, frames):
    """Re-express EntityParams geometry in the referenced component's frame.

    Documented EntityParams coordinates are in the owning (mate) assembly's
    model space; the record protocol stores decoded entity geometry in the
    referenced component's local frame (discovery applies the recorded
    component frame to recover assembly coordinates).  The conversion is
    ``local = inv(frame(component)) composed with frame(owner)`` with the
    frozen root (``""``) as identity, so a frame entity (component == owner)
    keeps its parameters unchanged.  A missing or non-finite occurrence frame
    rejects the geometry instead of guessing.
    """

    if not isinstance(block, dict) or block.get("error") is not None or block.get("geometry") is None:
        return block
    component_frame = frames.get(component) if component else _IDENTITY_FRAME
    owner_frame = frames.get(owner) if owner else _IDENTITY_FRAME
    if component_frame is None or owner_frame is None:
        block["geometry"] = None
        block["error"] = "the recorded occurrence frame for this reference is unavailable"
        return block
    local = _compose_frames(_rigid_inverse(component_frame), owner_frame)
    geometry = dict(block["geometry"])
    if "point" in geometry:
        point = _frame_point(local, [float(value) for value in geometry["point"]])
        if not all(math.isfinite(value) for value in point):
            block["geometry"] = None
            block["error"] = "the localized reference point is not finite"
            return block
        geometry["point"] = point
    for key in ("direction", "normal"):
        if key in geometry:
            value = _frame_vector(local, [float(value) for value in geometry[key]])
            norm = math.hypot(*value)
            if norm <= 0 or not all(math.isfinite(item) for item in value):
                block["geometry"] = None
                block["error"] = "the recorded reference direction is unusable"
                return block
            geometry[key] = [item / norm for item in value]
    geometry["frame"] = "component-local"
    block["geometry"] = geometry
    return block


def _coordinate_system_features(doc):
    """Active datums only; suppressed features remain in the native tree."""
    names = []
    try:
        feature = _dynamic(_method(doc, "FirstFeature"))
    except Exception as error:  # noqa: BLE001
        raise CadError(
            "cad_geometry_unreadable", "the feature tree could not be read for datums", {"error": str(error)}
        ) from error
    while feature is not None:
        try:
            type_name = str(_method(feature, "GetTypeName2") or "")
            name = _member(feature, "Name")
        except Exception as error:  # noqa: BLE001
            raise CadError(
                "cad_geometry_unreadable", "a datum feature could not be classified", {"error": str(error)}
            ) from error
        if type_name in ("CoordSys", "CoordinateSystem") and _is_text_name(name):
            try:
                suppressed = _method(feature, "IsSuppressed")
                if type(suppressed) is not bool:
                    raise ValueError("datum suppression state is not a native boolean")
            except Exception as error:  # noqa: BLE001
                raise CadError(
                    "cad_geometry_unreadable",
                    "datum suppression state could not be read",
                    {"datum": str(name), "phase": "suppression", "error": str(error)},
                ) from error
            if not suppressed:
                names.append(str(name))
        try:
            feature = _dynamic(_method(feature, "GetNextFeature"))
        except Exception as error:  # noqa: BLE001
            raise CadError("cad_geometry_unreadable", "the datum traversal failed", {"error": str(error)}) from error
    return names


def _coordinate_system_feature(doc, name):
    """The active coordinate-system feature with this exact name, or ``None``."""

    try:
        feature = _dynamic(_method(doc, "FirstFeature"))
    except Exception as error:  # noqa: BLE001
        raise CadError(
            "cad_geometry_unreadable", "the feature tree could not be read for datums", {"error": str(error)}
        ) from error
    while feature is not None:
        try:
            type_name = str(_method(feature, "GetTypeName2") or "")
            feature_name = _member(feature, "Name")
        except Exception as error:  # noqa: BLE001
            raise CadError(
                "cad_geometry_unreadable", "a datum feature could not be classified", {"error": str(error)}
            ) from error
        if type_name in ("CoordSys", "CoordinateSystem") and _is_text_name(feature_name) and str(feature_name) == name:
            return feature
        try:
            feature = _dynamic(_method(feature, "GetNextFeature"))
        except Exception as error:  # noqa: BLE001
            raise CadError("cad_geometry_unreadable", "the datum traversal failed", {"error": str(error)}) from error
    return None


def _coordinate_system_references(doc, name, *, source_root, document, configuration, scope):
    """Mark definition ownership unread without changing the native saved state.

    ICoordinateSystemFeatureData entities require AccessSelections. On the
    installed SolidWorks API that call sets GetSaveFlag even after balanced
    ReleaseSelectionAccess. Production reads the coordinate transform only;
    empty entity arrays without access must never imply absent references.
    """

    return {
        "state": "unread",
        "source": "IFeature.GetDefinition / ICoordinateSystemFeatureData",
        "context": {
            "document": str(document or ""),
            "configuration": str(configuration or ""),
            "scope": str(scope or ""),
        },
        "reason": "AccessSelections changes the document saved state",
        "required_api": "ICoordinateSystemFeatureData.AccessSelections/ReleaseSelectionAccess",
    }


def _features_of_type(doc, type_names):
    """Read matching top-level features without changing selection or rollback."""

    feature = _dynamic(_method(doc, "FirstFeature"))
    while feature is not None:
        if str(_method(feature, "GetTypeName2") or "") in type_names:
            yield feature
        feature = _dynamic(_method(feature, "GetNextFeature"))


def _specific_feature_object(feature):
    """Read the polymorphic return without forcing it through the mate IID.

    GetSpecificFeature2 can return IAttribute or IRefAxis as well as IMate2.
    VT_UNKNOWN preserves the native return, which pywin32 then wraps as the
    object's own IDispatch. No interface name is guessed from a failed query.
    """

    ole = getattr(feature, "_oleobj_", None)
    if ole is None:
        return _method(feature, "GetSpecificFeature2")
    import pythoncom

    raw = ole.InvokeTypes(
        ole.GetIDsOfNames("GetSpecificFeature2"), 0, pythoncom.DISPATCH_METHOD, (pythoncom.VT_UNKNOWN, 0), ()
    )
    return _dynamic(_win32().Dispatch(raw)) if raw is not None else None


def _reference_axis_record(doc, name, component="", selector=None):
    """Exact native RefAxis endpoints and the line rederived from those endpoints."""

    detail = {"component": component, "feature_name": name}
    try:
        matching = [f for f in _features_of_type(doc, {"RefAxis"}) if str(_member(f, "Name") or "") == name]
        if len(matching) != 1:
            raise ValueError(f"expected one exact RefAxis feature, found {len(matching)}")
        feature = matching[0]
        suppressed = _method(feature, "IsSuppressed")
        if type(suppressed) is not bool or suppressed:
            raise ValueError("reference axis is suppressed or its state is unreadable")
        error_code = _method(feature, "GetErrorCode")
        if type(error_code) is not int or error_code != 0:
            raise ValueError(f"reference axis has an unreadable or nonzero feature error: {error_code!r}")
        axis = _specific_feature_object(feature)
        endpoints = [float(value) for value in _as_list(_method(axis, "GetRefAxisParams"))]
        if len(endpoints) != 6 or not all(math.isfinite(value) for value in endpoints):
            raise ValueError("GetRefAxisParams must return six finite endpoint coordinates")
        vector = [endpoints[index + 3] - endpoints[index] for index in range(3)]
        norm = math.hypot(*vector)
        if norm <= 0 or not math.isfinite(norm):
            raise ValueError("reference-axis endpoints do not define a finite nonzero line")
    except Exception as error:
        raise CadError("cad_axis_reference_unreadable", str(error), detail) from error
    return {
        "kind": "reference_axis",
        "component": component,
        "selector": dict(selector or {"kind": "reference_axis", **detail}),
        "coordinate_frame": "component_local" if component else "assembly",
        "direction_semantics": "undirected_axis_line",
        "raw_endpoints_m": endpoints,
        "axis_point_m": endpoints[:3],
        "axis_direction": [value / norm for value in vector],
        "used_api": "IFeature.GetSpecificFeature2/IRefAxis.GetRefAxisParams",
    }


URDF_EXPORTER_ATTRIBUTE_NAME = "URDF Export Configuration (v1.4)"
_URDF_EXPORTER_PARAMETERS = (
    ("data", 1, 0),
    ("name", 1, 0),
    ("date", 1, 0),
    ("exporterVersion", 0, 1.0),
)


def _urdf_exporter_attribute(app, doc, source_root, configuration):
    """Read the stored exporter XML and bind its exact persistent-reference bytes.

    Schema registration is session-local (no CreateInstance, SetParameter or
    save). This layer records native facts only; the model decoder owns the
    interpretation of links, joints and engineering semantics.
    """

    result = {
        "state": "unread",
        "attribute_name": URDF_EXPORTER_ATTRIBUTE_NAME,
        "document": _relative_document(str(_method(doc, "GetPathName") or ""), source_root),
        "configuration": configuration,
        "registration": {},
        "pid_bindings": [],
    }
    try:
        features = [
            feature
            for feature in _features_of_type(doc, {"Attribute"})
            if str(_member(feature, "Name") or "").startswith(URDF_EXPORTER_ATTRIBUTE_NAME)
        ]
    except Exception as error:  # noqa: BLE001 - unread must never become absent
        result["error"] = f"attribute feature traversal failed: {type(error).__name__}: {error}"[:300]
        return result
    if not features:
        result["state"] = "absent"
        return result
    if len(features) != 1:
        result["error"] = "multiple exporter attributes matched the stored schema name"
        return result
    try:
        definition = _method(app, "DefineAttribute", URDF_EXPORTER_ATTRIBUTE_NAME)
        result["registration"]["defined"] = definition is not None
        for name, kind, default in _URDF_EXPORTER_PARAMETERS:
            result["registration"][name] = bool(_method(definition, "AddParameter", name, kind, default, 0))
        result["registration"]["register"] = bool(_method(definition, "Register"))
    except Exception as error:  # noqa: BLE001 - a registered schema may already exist
        result["registration"]["error"] = f"{type(error).__name__}: {error}"[:300]
    try:
        attribute = _specific_feature_object(features[0])
        actual_name = str(_method(attribute, "GetName") or "")
        if actual_name != URDF_EXPORTER_ATTRIBUTE_NAME:
            raise ValueError(f"stored attribute schema name mismatch: {actual_name!r}")
        data = _dynamic(_method(attribute, "GetParameter", "data"))
        xml = _method(data, "GetStringValue")
        if not isinstance(xml, str) or not xml.strip():
            raise ValueError("stored exporter XML is empty or not a string")
        result["raw_xml"] = xml
        result["raw_xml_sha256"] = hashlib.sha256(xml.encode("utf-8")).hexdigest()
        version_parameter = _dynamic(_method(attribute, "GetParameter", "exporterVersion"))
        version = float(_method(version_parameter, "GetDoubleValue"))
        if not math.isfinite(version):
            raise ValueError("stored exporter version is not finite")
        try:
            native_api_pid = int(_method(app, "GetProcessID"))
        except Exception:  # noqa: BLE001 - PID provenance is optional on legacy test surfaces
            native_api_pid = None
        result.update(
            {
                "exporter_version": version,
                "pid_bindings": _urdf_exporter_pid_bindings(
                    xml,
                    _member(doc, "Extension"),
                    source_root,
                    native_api_pid=native_api_pid,
                ),
                "state": "extracted",
            }
        )
    except Exception as error:  # noqa: BLE001 - preserve the unread attribute explicitly
        result["error"] = f"{type(error).__name__}: {error}"[:300]
    return result


def _urdf_exporter_pid_bindings(xml_text, extension, source_root, *, native_api_pid=None):
    """Resolve each unique declared PID as exact bytes, preserving XML separately."""

    import base64
    import xml.etree.ElementTree as ElementTree

    import pythoncom

    root = ElementTree.fromstring(xml_text)
    bindings = []
    seen = set()
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1] not in {"SWComponentPIDs", "SWMainComponentPID", "SWComponentPID"}:
            continue
        for node in element.iter():
            if node.tag.rsplit("}", 1)[-1] != "base64Binary":
                continue
            encoded = "".join((node.text or "").split())
            data = base64.b64decode(encoded, validate=True)
            if not data:
                raise ValueError("stored component PID is empty")
            if data in seen:
                continue
            seen.add(data)
            row = {
                "pid_hex": data.hex(),
                "pid_base64": encoded,
                "pid_sha256": hashlib.sha256(data).hexdigest(),
                "name2": None,
                "document": None,
                "configuration": None,
                "error_code": None,
            }
            try:
                errors = _win32().VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
                variant = _win32().VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_UI1, data)
                resolved = _method(extension, "GetObjectByPersistReference3", variant, errors)
                row["error_code"] = errors.value
                if resolved is None:
                    raise ValueError("GetObjectByPersistReference3 returned no object")
                component = _component(_dynamic(resolved))
                row["name2"] = str(_member(component, "Name2") or "") or None
                row.update(_component_source_document(component, source_root))
                if isinstance(row.get("source_provenance"), dict) and type(native_api_pid) is int:
                    row["source_provenance"]["native_api_pid"] = native_api_pid
                row["configuration"] = str(_member(component, "ReferencedConfiguration") or "") or None
                provenance = row.get("source_provenance")
                session_document = (
                    isinstance(provenance, dict)
                    and provenance.get("state")
                    in {"cached_materialization", "embedded_virtual_component"}
                    and bool(provenance.get("session_document_path"))
                )
                if session_document and not row["document"]:
                    # The exact active occurrence identity is the engine-resolved
                    # session document; record it in the same normalized spelling
                    # the component walk uses. This is never an author path or a
                    # name substitution.
                    row["document"] = normalize_document_path(str(provenance["session_document_path"]))
                bound = bool(row["document"])
                if (
                    type(row["error_code"]) is not int
                    or row["error_code"] != 0
                    or not row["name2"]
                    or not row["configuration"]
                    or not bound
                ):
                    raise ValueError("persistent reference did not resolve to a complete current component identity")
            except Exception as error:  # noqa: BLE001 - every failed binding stays explicit
                row["resolution_error"] = f"{type(error).__name__}: {error}"[:300]
            bindings.append(row)
    return bindings


def _mate_specific(feature, strict=False):
    """Return ``(IMate2, entity_count)`` for a mate feature.

    ``GetSpecificFeature2`` and ``GetMateEntityCount`` are parameterless vendor
    *methods* (SW2026 typelib: ``IMate2.GetMateEntityCount()``).  A recognised
    mate feature whose interface or entity count cannot be read raises instead
    of vanishing from the observations: a missed mate could evade the
    constraint-completeness check.
    """

    try:
        type_name = str(_method(feature, "GetTypeName2") or "")
    except Exception as error:  # noqa: BLE001
        if strict:
            raise CadError("cad_mate_unreadable", "mate feature type is unreadable", {"error": str(error)}) from error
        return None
    if type_name == "MateGroup":
        return None
    if not type_name.startswith("Mate"):
        if strict:
            raise CadError(
                "cad_mate_unreadable",
                "a feature inside the mate group is not a readable mate",
                {"feature": str(_member(feature, "Name") or ""), "type": type_name},
            )
        return None
    try:
        specific = _method(feature, "GetSpecificFeature2")
        raw_count = _method(specific, "GetMateEntityCount")
    except Exception as error:  # noqa: BLE001
        raise CadError(
            "cad_mate_unreadable",
            "a recognised mate feature could not be read",
            {"feature": str(_member(feature, "Name") or ""), "type": type_name, "error": str(error)},
        ) from error
    if type(raw_count) is not int or raw_count <= 0:
        raise CadError(
            "cad_mate_unreadable",
            "mate entity count is not a positive integer",
            {"feature": type_name, "value": repr(raw_count)},
        )
    return specific, raw_count


def _mate_features(doc):
    """Mate features, including the ones nested under the mate group feature."""

    features = []
    try:
        top = _dynamic(_method(doc, "FirstFeature"))
    except Exception as error:  # noqa: BLE001
        raise CadError("cad_mate_unreadable", "the feature tree could not be read", {"error": str(error)}) from error

    def walk(feature, step, inside_group=False):
        while feature is not None:
            try:
                type_name = str(_method(feature, "GetTypeName2") or "")
            except Exception as error:  # noqa: BLE001
                raise CadError(
                    "cad_mate_unreadable", "a feature type could not be read", {"error": str(error)}
                ) from error
            found = _mate_specific(feature, strict=inside_group)
            if found is not None:
                features.append((feature, found[0], found[1]))
            try:
                sub = _dynamic(_method(feature, "GetFirstSubFeature"))
            except Exception as error:  # noqa: BLE001
                raise CadError("cad_mate_unreadable", "mate sub-feature walk failed", {"error": str(error)}) from error
            if sub is not None:
                walk(sub, "GetNextSubFeature", inside_group or type_name == "MateGroup")
            try:
                feature = _dynamic(_method(feature, step))
            except Exception as error:  # noqa: BLE001
                raise CadError("cad_mate_unreadable", "mate traversal failed", {"error": str(error)}) from error

    walk(top, "GetNextFeature")
    return features


def _relative_document(path_value, source_root):
    try:
        candidate = Path(str(path_value))
        if not candidate.is_file():
            return None
        relative = candidate.resolve().relative_to(Path(source_root).resolve())
        return relative.as_posix()
    except (OSError, ValueError):
        return None


def _imported_path_reading(component) -> tuple[str | None, str | None]:
    """One imported-path reading: text, API error, or non-text API failure."""

    try:
        value = _method(component, "GetImportedPath")
    except Exception as error:  # noqa: BLE001 - recorded verbatim for the policy gate
        return None, f"{type(error).__name__}: {error}"[:300]
    if value is None:
        return None, None
    if not isinstance(value, str):
        return None, f"non-text reading: {type(value).__name__}: {value!r}"[:300]
    return (value if value.strip() else None), None


def _component_source_document(component, source_root, *, native_path=None):
    """Bind an occurrence to a delivered file, including native linked imports.

    SolidWorks materializes 3D Interconnect parts in an owned temporary path.
    A resident delivered document binds directly; otherwise the raw readings
    are recorded independently of any selection carrier: the engine-reported
    materialization path, the virtual flag and the author import path
    (GetImportedPath is author metadata, never an in-package binding).  An
    occurrence whose engine-resolved document is a structural session
    materialization -- IC~~ path component under the owned session temp area --
    is always classified ``cached_materialization`` on both the raw and the
    carrier side, even when the author import path itself points into the
    source root; that in-stage source is then reported as validated author
    metadata (``author_imported_in_source_root``/``author_imported_relative``)
    and retained as a selection-level bound edge, never as a raw binding.
    A ``VC~~`` document is a separate class: it is accepted only when
    ``IComponent2.IsVirtual`` is exactly ``True``, it lies in the owning
    session's temporary root, and the API reports no imported path.  The
    sealed carrier must additionally bind its ultimate owner, occurrence, and
    configuration.  Temporary names and basenames are never proof.
    """

    native_path = str(native_path if native_path is not None else _method(component, "GetPathName") or "")
    provenance = {
        "state": "unread",
        "source_root": str(Path(source_root).resolve()),
        "native_document_path": native_path,
        "materialization_path": native_path or None,
        "session_document_path": native_path or None,
        "is_virtual": None,
        "author_imported_path": None,
        "author_imported_error": None,
        "used_api": SESSION_DOCUMENT_API,
    }
    result = {"document": None, "source_provenance": provenance}
    virtual = None
    try:
        reading = _member(component, "IsVirtual")
        virtual = reading if type(reading) is bool else None
    except Exception as error:  # noqa: BLE001 - an unreadable virtual flag stays unknown
        provenance["virtual_error"] = f"{type(error).__name__}: {error}"[:200]
    provenance["is_virtual"] = virtual
    relative = _relative_document(native_path, source_root)
    if relative and virtual is False:
        return {"document": relative}
    if relative and virtual is None:
        provenance["error"] = (
            "ValueError: a delivered document requires an explicit False IsVirtual reading"
        )
        return result
    imported_path, imported_error = _imported_path_reading(component)
    provenance["author_imported_path"] = imported_path
    if imported_error is not None:
        provenance["author_imported_error"] = imported_error
    if virtual is True:
        if provenance.get("author_imported_error") is not None:
            provenance["error"] = (
                "ValueError: a virtual component's imported-path absence could not be proven"
            )
        elif provenance["author_imported_path"] is not None:
            provenance["error"] = (
                "ValueError: a virtual component reports an imported path; virtual geometry "
                "and a 3D Interconnect source cannot be conflated"
            )
        elif session_materialization_path(native_path):
            provenance["error"] = (
                "ValueError: an IC~~ import materialization is marked virtual; "
                "the two session document classes cannot be conflated"
            )
        elif embedded_virtual_component_path(native_path):
            provenance.update(
                {
                    "state": "embedded_virtual_component",
                    "kind": "native_embedded_virtual_component",
                    "document": None,
                    "session_temp_root": embedded_virtual_component_root(native_path),
                }
            )
        else:
            provenance["error"] = (
                "ValueError: IsVirtual=True is not sufficient without an exact VC~~ document "
                "inside the owning session temporary root"
            )
    elif session_materialization_path(native_path) and virtual is False:
        author_relative = _relative_document(imported_path, source_root) if imported_path else None
        provenance.update(
            {
                "state": "cached_materialization",
                "kind": "native_import_materialization",
                "document": None,
                "session_temp_root": session_temp_root(native_path),
                "author_imported_in_source_root": author_relative is not None,
                "author_imported_relative": author_relative,
            }
        )
    else:
        if session_materialization_path(native_path):
            provenance["error"] = (
                "ValueError: an IC~~ materialization requires an explicit False IsVirtual reading"
            )
        elif embedded_virtual_component_path(native_path):
            provenance["error"] = (
                "ValueError: a VC~~ document requires an explicit True IsVirtual reading"
            )
        else:
            provenance["error"] = (
                "ValueError: the resolved document is not a delivered file, an IC~~ import "
                "materialization, or an API-proven VC~~ embedded virtual component"
            )
    return result


def _solver_observation(component, defining_document, source_root, *, configuration, scope):
    """Context-bound solver readings for one occurrence (diagnostics only).

    The documented ``IComponent2.Solving`` property and
    ``IComponent2.GetConstrainedStatus()`` method are recorded with their enum
    families and the context the occurrence was read in: the *defining*
    document, its configuration and the occurrence scope.  That context is kept
    distinct from the occurrence's referenced child configuration and from
    ``IsFixed``.  An unavailable or non-integer reading is preserved explicitly
    as ``value: None`` plus an error string — never coerced to ``False`` or
    ``0``, never defaulted, and never a capture failure.
    """

    def relative_document(value) -> str:
        found = _relative_document(value, source_root)
        if found is not None:
            return found
        return normalize_document_path(str(value)) if _is_text_name(value) else ""

    def reading(method: str, family: str, read) -> dict:
        try:
            value = read()
        except Exception as error:  # noqa: BLE001 - a diagnostic read must not fail the capture
            return {
                "method": method,
                "family": family,
                "value": None,
                "error": f"{type(error).__name__}: {error}"[:200],
            }
        if type(value) is int:
            return {"method": method, "family": family, "value": value, "error": None}
        return {
            "method": method,
            "family": family,
            "value": None,
            "error": f"non-integer reading: {type(value).__name__}: {value!r}"[:200],
        }

    return {
        "context": {
            "document": relative_document(defining_document),
            "configuration": str(configuration or ""),
            "scope": str(scope or ""),
        },
        "solving": reading("IComponent2.Solving", "swComponentSolvingOption_e", lambda: _member(component, "Solving")),
        "constrained_status": reading(
            "IComponent2.GetConstrainedStatus",
            "swConstrainedStatus_e",
            lambda: _method(component, "GetConstrainedStatus"),
        ),
    }


_ACTIVITY_ACTION = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_ACTIVITY_OBJECT_LIMIT = 160
_ACTIVITY_MIN_INTERVAL_SECONDS = 0.25
#: Capture keepalive: while a capture is in flight the last accepted activity
#: record is re-emitted at this interval, so one blocking native call (a document
#: close/reopen, a whole-assembly read, a heavy mesh) cannot leave the portal's
#: live activity without a fresh observation for longer than this bound.
ACTIVITY_KEEPALIVE_SECONDS = 30.0


def _activity_object(path_value, source_root=None):
    """Safe relative object name for activity records, or ``None``.

    Prefers the path relative to the frozen source root; an absolute or temp
    path falls back to its bare leaf name so the public UI never receives a
    machine path.  Drive letters, ``..`` segments and ``:`` never pass.
    """

    if path_value is None:
        return None
    text = str(path_value).strip()
    if not text:
        return None
    relative = _relative_document(path_value, source_root) if source_root is not None else None
    if relative is not None:
        text = relative
    else:
        text = text.replace("\\", "/")
        if re.match(r"^[A-Za-z]:", text) or text.startswith("/"):
            text = posixpath.basename(text)
    parts = [part for part in text.split("/") if part]
    if not parts or any(part == ".." for part in parts):
        return None
    text = "/".join(parts)
    if ":" in text:
        return None
    if len(text) > _ACTIVITY_OBJECT_LIMIT:
        keep_head = _ACTIVITY_OBJECT_LIMIT // 2
        keep_tail = _ACTIVITY_OBJECT_LIMIT - keep_head - 1
        text = text[:keep_head] + "…" + text[-keep_tail:]
    return text


class _ActivityEmitter:
    """Best-effort bounded activity emission for one ``discover_native`` call.

    Emits only the contract's emitter-side fields — ``phase``, ``action`` and
    the optional ``params`` / ``current_object`` / ``completed``+``total`` —
    through an optional callback.  Counts are emitted only as known integer
    pairs, are never fabricated, and stay monotonic per (phase, action);
    totals may grow while the walk discovers sub-assembly scope but never
    shrink below ``completed``.  The collector owns timestamps and sequencing.
    Operation or object transitions always pass; only repeats of the same
    phase/action/current-object observation are throttled.  A missing or
    failing callback can never affect the capture.

    ``freeze`` reuses one emitter for a whole capture: its own stage notes go
    through :meth:`emit`, the backend capture methods receive :meth:`publish`
    as their record sink (records produced by a nested emitter then also
    refresh the last observation), and :meth:`start_keepalive` re-emits that
    last record while one blocking native call runs.  ``publish`` and
    ``refresh`` are guarded exactly like ``emit``.
    """

    def __init__(self, callback=None, *, keepalive_seconds=None):
        self._callback = callback
        self._last_emitted: dict = {}
        self._completed: dict = {}
        self._last_action = None
        self._last_record: dict | None = None
        self._keepalive_seconds = keepalive_seconds
        self._keepalive_stop: threading.Event | None = None
        self._keepalive_thread: threading.Thread | None = None

    def emit(
        self,
        action: str,
        *,
        phase: str = "discover",
        current_object=None,
        completed=None,
        total=None,
        params=None,
        force: bool = False,
    ) -> None:
        if self._callback is None or not isinstance(action, str) or not _ACTIVITY_ACTION.match(action):
            return
        if (completed is None) != (total is None):
            return
        if completed is not None:
            if type(completed) is not int or type(total) is not int or completed < 0 or total < completed:
                return
            previous = self._completed.get((phase, action))
            if previous is not None and completed < previous:
                return
            self._completed[(phase, action)] = completed
        record: dict = {"phase": phase, "action": action}
        if current_object is not None:
            record["current_object"] = current_object
        if params:
            clean: dict = {}
            for name, value in params.items():
                if len(clean) >= 6:
                    break
                if not isinstance(name, str):
                    continue
                if type(value) in (bool, int) or (isinstance(value, str) and len(value) <= 120):
                    clean[name] = value
            if clean:
                record["params"] = clean
        if completed is not None:
            record["completed"] = completed
            record["total"] = total
        if not force:
            now = time.monotonic()
            if (phase, action) == self._last_action:
                previous_at = self._last_emitted.get((phase, action))
                previous_object = (
                    self._last_record.get("current_object")
                    if isinstance(self._last_record, dict)
                    else None
                )
                if (
                    previous_at is not None
                    and now - previous_at < _ACTIVITY_MIN_INTERVAL_SECONDS
                    and previous_object == current_object
                ):
                    return
            self._last_emitted[(phase, action)] = now
        try:
            self._callback(record)
        except Exception:  # noqa: BLE001 - activity must never fail the capture
            return
        self._last_record = dict(record)
        self._last_action = (phase, action)

    def publish(self, record: dict) -> None:
        """Forward one partial record produced by a nested emitter, guarded.

        The capture backend calls this through its own emitter while the walk,
        the per-instance reads and the mesh batch run; a forwarded record also
        becomes the keepalive's last observation.  A callback failure stays
        invisible to the capture.
        """

        if self._callback is None or not isinstance(record, dict):
            return
        try:
            self._callback(record)
        except Exception:  # noqa: BLE001 - activity must never fail the capture
            return
        self._last_record = dict(record)

    def refresh(self) -> None:
        """Re-emit the last accepted record so its freshness stays visible."""

        record = self._last_record
        if record is not None:
            self.publish(dict(record))

    def start_keepalive(self) -> None:
        """Refresh the last observation every ``keepalive_seconds`` until stopped."""

        if self._callback is None or not self._keepalive_seconds or self._keepalive_thread is not None:
            return
        stop = threading.Event()
        interval = float(self._keepalive_seconds)

        def loop() -> None:
            while not stop.wait(interval):
                self.refresh()

        self._keepalive_stop = stop
        self._keepalive_thread = threading.Thread(target=loop, name="solidworks-activity-keepalive", daemon=True)
        self._keepalive_thread.start()

    def stop_keepalive(self) -> None:
        stop, thread = self._keepalive_stop, self._keepalive_thread
        if stop is None or thread is None:
            return
        stop.set()
        thread.join(timeout=2.0)
        self._keepalive_stop = None
        self._keepalive_thread = None


#: SolidWorks COM mid-export transient: DISP_E_EXCEPTION ("unexpected server condition").
TRANSIENT_COM_RETRY_ATTEMPTS = 3
TRANSIENT_COM_RETRY_DELAYS = (0.2, 0.5)
#: Geometry-export topology reads can run against a CAD server that stays
#: unavailable under load for longer than the default budget. Keep the deeper
#: retry budget available to isolated native read diagnostics and the
#: facetless probe; ordinary call sites keep the 3/0.2/0.5 default.
FACE_READ_RETRY_ATTEMPTS = 8
FACE_READ_RETRY_DELAYS = (0.5, 1.0, 2.0, 3.0, 4.0, 4.0, 4.0)
# Unloading a full read-write capture session (61 documents) can lag; the
# settle window is bounded and only decides when a document is truly stuck.
CLOSE_ALL_SETTLE_ATTEMPTS = 30
CLOSE_ALL_SETTLE_DELAY_S = 0.5
# A facetless sheet face whose ITessellation probe exhausts the transient COM
# retries is still accountable when the face itself proves degeneracy: zero
# native triangle count, an empty display array, and a finite area at or below
# this bound.  The bound sits one order of magnitude above the largest
# degenerate face measured on the M3 PCB (1.871564249915375e-13 m2) and six
# orders of magnitude below 1 mm2, so no displayable face of this assembly can
# be mistaken for degenerate; the proof stays the zero count, the empty array
# and the measured area, never the failed probe.
FACETLESS_DEGENERATE_AREA_M2 = 1e-12
FACETLESS_EVIDENCE_API = "IBody2.GetTessellation/ITessellation.Tessellate/GetFaceFacets"
PERSISTENT_REFERENCE_DOMAIN = b"description-pipeline/native-identity/v1"


def _float32_xyz(point):
    """Return one finite point after the single conversion used by mesh output."""

    values = tuple(float(value) for value in point)
    if len(values) != 3 or not all(map(math.isfinite, values)):
        raise ValueError("point must contain three finite coordinates")
    return struct.unpack("<3f", struct.pack("<3f", *values))


def _binary64_xyz(point):
    """Return one finite native point without discarding binary64 precision."""

    values = tuple(float(value) for value in point)
    if len(values) != 3 or not all(map(math.isfinite, values)):
        raise ValueError("point must contain three finite coordinates")
    return values


def _canonical_triangle_key(points):
    """Canonicalize only cyclic rotations; opposite orientation stays distinct."""

    float32 = tuple(_float32_xyz(point) for point in points)
    if len(float32) != 3:
        raise ValueError("a triangle needs three points")
    return min(
        float32[offset:] + float32[:offset]
        for offset in range(3)
    )


def _typed_reference_sha256(kind, raw):
    """Domain-separate an opaque native persistent reference by entity kind."""

    payload = PERSISTENT_REFERENCE_DOMAIN + b"\x00" + kind.encode("ascii") + b"\x00" + raw
    return hashlib.sha256(payload).hexdigest()


class _NativeReferenceRegistry:
    """Capture-wide raw reference and scope-local identity audit."""

    def __init__(self):
        self.raw_owners = {
            "body": {},
            "face": {},
            "edge": {},
            "vertex": {},
        }
        self.identity_owners = {}

    def claim(
        self,
        *,
        kind: str,
        raw_sha256: str,
        typed_sha256: str,
        owner: dict,
        base: dict,
    ) -> None:
        recorded_owner = {
            **owner,
            "typed_sha256": typed_sha256,
        }
        previous = self.identity_owners.get(typed_sha256)
        if previous is not None:
            raise CadError(
                "cad_persistent_reference_alias",
                "a persistent identity is not unique",
                {**base, "kind": kind, "object": owner, "previous": previous},
            )
        self.identity_owners[typed_sha256] = recorded_owner
        self.raw_owners[kind].setdefault(raw_sha256, []).append(recorded_owner)

    def claim_scope(
        self,
        *,
        kind: str,
        raw_sha256: str,
        identity_sha256: str,
        owner: dict,
        base: dict,
    ) -> None:
        recorded_owner = {
            **owner,
            "typed_sha256": identity_sha256,
        }
        previous = self.identity_owners.get(identity_sha256)
        if previous is not None:
            if previous == recorded_owner:
                return
            raise CadError(
                "cad_scope_identity_alias",
                "a scope-local identity is not unique",
                {
                    **base,
                    "kind": kind,
                    "object": recorded_owner,
                    "previous": previous,
                },
            )
        self.identity_owners[identity_sha256] = recorded_owner
        self.raw_owners[kind].setdefault(raw_sha256, []).append(recorded_owner)

    def alias_audit(self) -> dict[str, tuple[str, ...]]:
        return {
            kind: tuple(
                sorted(
                    raw_sha256
                    for raw_sha256, owners in references.items()
                    if len(
                        {
                            owner["typed_sha256"]
                            for owner in owners
                        }
                    )
                    > 1
                )
            )
            for kind, references in self.raw_owners.items()
        }

    def cross_kind_aliases(self) -> tuple[dict, ...]:
        def owner_key(owner):
            return (
                str(owner.get("component", "")),
                int(owner.get("body_type", -1)),
                int(owner.get("body_index", -1)),
                str(owner["kind"]),
                int(owner.get("face_index", -1)),
                int(owner.get("edge_index", -1)),
                str(owner.get("endpoint", "")),
            )

        aliases = []
        for raw_sha256 in sorted(
            {
                raw_sha256
                for references in self.raw_owners.values()
                for raw_sha256 in references
            }
        ):
            owners = [
                owner
                for references in self.raw_owners.values()
                for owner in references.get(raw_sha256, [])
            ]
            if len({owner["kind"] for owner in owners}) > 1:
                aliases.append(
                    {
                        "raw_sha256": raw_sha256,
                        "owners": tuple(sorted(owners, key=owner_key)),
                    }
                )
        return tuple(aliases)


def _directed_fin_cycle(fin_vertex_ids):
    """Return the orientation-preserving vertex cycle of three directed fins."""

    pending = [tuple(vertex_ids) for vertex_ids in fin_vertex_ids]
    if len(pending) != 3 or any(
        len(pair) != 2 or pair[0] == pair[1] for pair in pending
    ):
        raise ValueError("a facet needs three directed fins")
    cycle = [pending[0][0], pending[0][1]]
    remaining = pending[1:]
    while remaining:
        matches = [pair for pair in remaining if pair[0] == cycle[-1]]
        if len(matches) != 1:
            raise ValueError("directed fins do not form one cycle")
        pair = matches.pop()
        remaining.remove(pair)
        cycle.append(pair[1])
    if cycle[-1] != cycle[0] or len(set(cycle[:3])) != 3:
        raise ValueError("directed fins do not form one triangle")
    return tuple(cycle[:3])


#: SolidWorks COM mid-session transients that are retried because pywin32 surfaces
#: both through ``com_error``: DISP_E_EXCEPTION 0x80010105 (unexpected server
#: condition) and RPC_S_SERVER_UNAVAILABLE 0x800706BA (RPC server unavailable).
TRANSIENT_COM_ERROR_CODES = ("-2147417851", "-2147023174")


def _is_transient_com_error(error: BaseException) -> bool:
    text = f"{type(error).__name__}: {error}"
    return "com_error" in text and any(code in text for code in TRANSIENT_COM_ERROR_CODES)


def _retry_transient_com(
    operation,
    *,
    attempts: int = TRANSIENT_COM_RETRY_ATTEMPTS,
    delays: tuple[float, ...] = TRANSIENT_COM_RETRY_DELAYS,
    sleep=None,
):
    """Bounded retries for the transient COM server exception only.

    Each attempt re-runs ``operation``, whose callers re-dereference the face or
    body first, so a stale COM proxy is never reused.  Any other exception, or
    the final attempt, propagates unchanged; the strict decision tree downstream
    is untouched.  ``attempts``, ``delays`` and ``sleep`` let a call site widen
    the budget (the geometry face reads do) without changing the shared default;
    ``sleep`` resolves to ``time.sleep`` at call time when not given.
    """

    if sleep is None:
        sleep = time.sleep
    for attempt in range(attempts):
        try:
            return operation()
        except Exception as error:  # noqa: BLE001 - only the transient signature is retried
            if attempt == attempts - 1 or not _is_transient_com_error(error):
                raise
            sleep(delays[min(attempt, len(delays) - 1)])
    raise AssertionError("unreachable")  # pragma: no cover


class SolidWorksBackend(CadBackend):
    name = "solidworks"

    def __init__(self, *, session_factory=None, cad_ownership_file=None):
        self._local = threading.local()
        self._session_factory = session_factory
        self._cad_ownership_file = cad_ownership_file
        self._sessions = {}
        self._sessions_lock = threading.Lock()
        self._owner_thread = None
        self._cancelled = threading.Event()
        self._capture_roots = []
        self._requested_configurations = {}
        #: ``None`` disables the pre-open gate; a set of normalized absolute
        #: source paths seals exactly which documents may be opened.
        self._sealed_documents: set[str] | None = None
        #: Session documents proven by the active occurrence walk.  A temporary
        #: path shape alone never grants a sealed-source-open exemption.
        self._session_document_paths: dict[str, tuple[str, object]] = {}
        self._session_artifact_roots: dict[object, str] = {}
        self._session_artifact_root_owners: dict[str, tuple[str, object]] = {}
        #: Pinned staged search scope applied to every owned session before any
        #: reference resolution happens (``None`` leaves machine search settings
        #: untouched); evidence is recorded per owned process.
        self._staged_search_root: str | None = None
        self._staged_search_applied: set = set()
        self._staged_search_evidence: list[dict] = []
        self._components = set()
        self._source_components = {}
        self._source_provenance = {}
        self._source_scope_overrides = {}
        self._source_documents = {}
        self._scene_source_root = None
        self._scene_document_key = None
        self.notes = {}
        self.source_files = {}
        #: ``GetSaveFlag`` per working-tree document, recorded as evidence.
        self.save_flags: dict[str, bool] = {}

    def _app_obj(self):
        with self._sessions_lock:
            current = threading.current_thread()
            if self._owner_thread is not None and self._owner_thread is not current:
                raise EnvironmentError_("cad_thread_mismatch", "CAD proxies must stay on their owning STA thread")
            self._owner_thread = current
            if self._cancelled.is_set():
                raise EnvironmentError_("cad_session_cancelled", "Capture was cancelled")
            role = getattr(self._local, "role", "copy" if "copy" in self._sessions else "source")
            session = self._sessions.get(role)
            source = self._sessions.get("source")
        if session is not None and session.closed:
            raise EnvironmentError_(
                "cad_session_retired", "The selected CAD session has been retired", session.identity()
            )
        if session is None:
            # The source has supplied its primitives and saved file bytes. Only
            # the collected copy needs live CAD now; retain the closed source's
            # identity, but never keep its process running or revive it.
            if role == "copy" and source is not None and not source.closed:
                try:
                    source.close()
                except BaseException:
                    self._cancelled.set()
                    raise
            from .isolation import CadSession

            factory = self._session_factory
            session = factory() if factory is not None else CadSession(ownership_file=self._cad_ownership_file)
            with self._sessions_lock:
                if self._cancelled.is_set():
                    session.close()
                    raise EnvironmentError_("cad_session_cancelled", "Capture was cancelled during CAD startup")
                self._sessions[role] = session
            # Register before a potentially blocking COM call so a watchdog can
            # close the owned Windows job without touching COM from its thread.
            try:
                app = session.connect(self._cancelled)
            except BaseException:
                session.close()
                raise
        else:
            app = session.current_application()
        if self._cancelled.is_set():
            raise EnvironmentError_("cad_session_cancelled", "Capture was cancelled")
        if self._staged_search_root is not None:
            key = session.identity().get("pid") if session is not None else id(app)
            if key not in self._staged_search_applied:
                evidence = pin_search_folders(app, self._staged_search_root)
                evidence["pid"] = key
                self._staged_search_applied.add(key)
                self._staged_search_evidence.append(evidence)
        return app

    def _role_for_path(self, path):
        normalized = normalize_document_path(os.path.abspath(path))
        if any(normalized.startswith(root + "\\") for root in self._capture_roots):
            return "copy"
        if session_artifact_path(path):
            # An engine-resolved session artifact (IC~~ import cache or VC~~
            # embedded component) belongs to the session whose walk produced
            # it, never to a path-derived role. During capture of a collected
            # copy the source session is retired, and routing the artifact to
            # "source" would look up (or worse, create) the wrong application.
            # Prefer the walk's current role; otherwise require exactly one
            # live session. Ambiguity refuses instead of guessing.
            role = getattr(self._local, "role", None)
            if role is not None and role in self._sessions and not self._sessions[role].closed:
                return role
            live = sorted(name for name, session in self._sessions.items() if not session.closed)
            if len(live) == 1:
                return live[0]
            raise CadError(
                "cad_materialization_session_ambiguous",
                "a session materialization has no unambiguous owning CAD session",
                {"path": str(path), "sessions": live},
            )
        return "source"

    def seal_documents(self, documents):
        """Seal the exact source documents this backend may open.

        The endpoint configures the verified selection here before any
        geometry or discovery work.  From then on every open of a source
        document - ``OpenDoc6`` through :meth:`open_document`,
        ``_ensure_document`` or discovery - must name one of these absolute
        paths; anything else raises ``document_not_sealed`` before COM is
        touched.  Collected copies under the capture roots stay exempt: freeze
        proves those with the collector, ``resolve_dependencies`` and
        ``inspect_copy``.

        This gates which documents may be opened; it is not OS file-access
        sandboxing.  It blocks backend opens only: once an open is permitted,
        SolidWorks itself reads the reference files it resolves, and the owned
        session provides no OS-level file-access isolation.  Pass absolute
        document paths and receive the sorted normalized sealed set as
        evidence.
        """

        sealed = set()
        for document in documents:
            text = os.fspath(document) if isinstance(document, os.PathLike) else document
            if not isinstance(text, str) or not text.strip():
                raise CadError(
                    "document_seal_invalid", "Sealed documents must be non-empty paths", {"document": repr(document)}
                )
            if not os.path.isabs(text):
                raise CadError("document_seal_invalid", "Sealed documents must be absolute paths", {"document": text})
            sealed.add(normalize_document_path(os.path.abspath(text)))
        self._sealed_documents = sealed
        return sorted(sealed)

    def sealed_documents(self):
        """The sealed source-document set, or ``None`` while the gate is disabled."""

        if self._sealed_documents is None:
            return None
        return sorted(self._sealed_documents)

    def configure_staged_search(self, package):
        """Pin the session search scope of every owned session to ``package``.

        The endpoint calls this before opening staged documents: relocated or
        authoring-absolute references may only resolve inside the provisional
        staged folder, and the search scope is the documented session
        ``SetSearchFolders`` configuration -- never machine File Locations or
        any persistent preference.  ``None`` disables the pin.  Returns the
        normalized pinned root, or ``None`` when the pin is disabled.
        """

        if package is None:
            self._staged_search_root = None
            self._staged_search_applied = set()
            return None
        if not isinstance(package, (str, os.PathLike)):
            raise CadError("cad_staged_search_invalid", "the staged search root must be a directory path")
        value = os.fspath(package)
        if not isinstance(value, str) or not value.strip():
            raise CadError("cad_staged_search_invalid", "the staged search root must be a directory path")
        root = str(Path(value).resolve())
        if root != self._staged_search_root:
            self._staged_search_root = root
            self._staged_search_applied = set()
            # Search evidence is append-only across root changes: sessions
            # created earlier keep their recorded pin (the collector re-pins
            # the copy session to the snapshot directory before it starts).
        return root

    def staged_search_evidence(self):
        """Recorded per-session search-scope evidence (before / pinned / after)."""

        return [dict(item) for item in self._staged_search_evidence]

    def _bind_session_document(self, role, session, path):
        if session is None or session.closed:
            raise CadError(
                "cad_materialization_session_unavailable",
                "the owning CAD session for a session artifact is not available",
                {"path": str(path), "role": role},
            )
        artifact_root = session_temp_root(path) or embedded_virtual_component_root(path)
        if not artifact_root:
            raise CadError(
                "cad_materialization_session_mismatch",
                "a session artifact has no canonical controlled temporary root",
                {"path": str(path), "role": role},
            )
        try:
            session_identity = session.identity()
        except Exception as error:  # noqa: BLE001 - unreadable ownership must fail before mutation
            raise CadError(
                "cad_materialization_session_mismatch",
                "the owning CAD session identity could not be read",
                {
                    "path": str(path),
                    "role": role,
                    "error": f"{type(error).__name__}: {error}"[:300],
                },
            ) from error
        session_pid = session_identity.get("pid") if isinstance(session_identity, dict) else None
        if type(session_pid) is not int or session_pid <= 0:
            raise CadError(
                "cad_materialization_session_mismatch",
                "the owning CAD session has no valid process id",
                {
                    "path": str(path),
                    "role": role,
                    "session_pid": session_pid,
                },
            )
        if session_root_pid(artifact_root) != session_pid:
            raise CadError(
                "cad_materialization_session_mismatch",
                "a session artifact root does not match the owning process id",
                {"path": str(path), "role": role, "session_pid": session_pid, "root": artifact_root},
            )
        recorded_root = self._session_artifact_roots.setdefault(session, artifact_root)
        if recorded_root != artifact_root:
            raise CadError(
                "cad_materialization_session_mismatch",
                "a session artifact does not belong to the controlled session temporary root",
                {"path": str(path), "role": role, "recorded_root": recorded_root, "root": artifact_root},
            )
        root_key = normalize_document_path(os.path.abspath(artifact_root))
        root_owner = self._session_artifact_root_owners.setdefault(root_key, (role, session))
        if root_owner != (role, session):
            raise CadError(
                "cad_materialization_session_mismatch",
                "one controlled session temporary root cannot be shared by multiple CAD sessions",
                {"path": str(path), "role": role, "root": artifact_root},
            )
        normalized = normalize_document_path(os.path.abspath(str(path)))
        recorded_owner = self._session_document_paths.get(normalized)
        if recorded_owner is not None and recorded_owner != (role, session):
            raise CadError(
                "cad_materialization_session_mismatch",
                "a session document cannot be rebound to another CAD session",
                {"path": str(path), "role": role},
            )
        self._session_document_paths[normalized] = (role, session)

    def _require_sealed(self, path):
        """Refuse an unsealed source open before any COM call is made."""

        if self._sealed_documents is None:
            return
        role = self._role_for_path(path)
        normalized = normalize_document_path(os.path.abspath(str(path)))
        session = self._sessions.get(role)
        if self._session_document_paths.get(normalized) == (role, session) and session is not None:
            return
        if role == "copy" and not session_artifact_path(path):
            return
        if normalized not in self._sealed_documents:
            raise CadError(
                "document_not_sealed",
                "The document is not in the sealed selection; refusing to open it",
                {"document": str(path)},
            )

    def _app_for_path(self, path):
        previous = getattr(self._local, "role", None)
        self._local.role = self._role_for_path(path)
        try:
            return self._app_obj()
        finally:
            if previous is None:
                del self._local.role
            else:
                self._local.role = previous

    @contextmanager
    def session(self):
        """A job owns its COM references and process handles until its final step."""
        if getattr(self._local, "depth", 0):
            yield self
            return
        initialized = co_initialize()
        self._local.depth = 1
        try:
            self.release()
            if self._cancelled.is_set():
                raise EnvironmentError_("cad_session_cancelled", "Capture was cancelled before CAD startup")
            yield self
        finally:
            pending = sys.exception()
            try:
                self.release()
            except Exception as error:
                if pending is None:
                    raise
                pending.add_note(f"CAD cleanup also failed: {error}")
            finally:
                self._cancelled.clear()
                self._local.depth = 0
                if initialized:
                    import pythoncom

                    pythoncom.CoUninitialize()

    def prepare_source(self, assembly, configuration):
        self._requested_configurations[normalize_document_path(assembly)] = str(configuration)

    def abort_owned_processes(self):
        self._cancelled.set()
        with self._sessions_lock:
            sessions = list(self._sessions.values())
        terminated, errors = [], []
        for session in sessions:
            if session.closed:
                continue
            try:
                session.terminate()
                terminated.append(session.identity())
            except Exception as error:
                errors.append({**session.identity(), "error": str(error)})
        return {"terminated": terminated, "errors": errors}

    def retire_session(self, role="copy"):
        """Close and remove one owned session without dropping capture policy.

        A collector may leave a document and its COM caches in a process-local
        state that is not suitable for the subsequent capture.  Retirement is
        an explicit process boundary: it closes the owned job, removes the role
        binding, and invalidates state that points at the old process.  Capture
        roots, requested configurations, sealed documents and the staged search
        root are policy, not session state, and deliberately remain intact for
        the next owned session.
        """

        if role not in {"source", "copy"}:
            raise EnvironmentError_(
                "cad_session_role_invalid",
                "an unknown CAD session role cannot be retired",
                {"role": str(role)},
            )
        with self._sessions_lock:
            session = self._sessions.get(role)
            if session is None:
                raise EnvironmentError_(
                    "cad_session_missing",
                    "the requested CAD session role is not registered",
                    {"role": str(role)},
                )
            before = session.identity()
        if not session.closed:
            # CadSession.close() publishes closed only after its owned process
            # is gone. A close failure therefore leaves the role registered and
            # must propagate instead of allowing a second process to start.
            session.close()
        with self._sessions_lock:
            if self._sessions.get(role) is not session:
                raise EnvironmentError_(
                    "cad_session_retirement_race",
                    "the selected CAD session changed while it was retiring",
                    {"role": str(role)},
                )
            del self._sessions[role]
            pid = before.get("pid")
            if isinstance(pid, int):
                self._staged_search_applied.discard(pid)
            # All of these contain COM proxies, session identities or caches
            # derived from the retired application. Rebuild them in the new
            # session; never carry them across the process boundary.
            self._components.clear()
            self._source_components.clear()
            self._source_documents.clear()
            self._session_document_paths.clear()
            self._session_artifact_roots.clear()
            self._session_artifact_root_owners.clear()
            self._scene_document_key = None
            for cache_name in ("_part_mass_properties", "_effective_instance_readings"):
                cache = getattr(self, cache_name, None)
                if isinstance(cache, dict):
                    cache.clear()
            self.notes = {}
            self.source_files = {}
        return {
            "role": role,
            "retired": True,
            "before": before,
            "after": session.identity(),
        }

    def release(self):
        if self._owner_thread is not None and self._owner_thread is not threading.current_thread():
            raise EnvironmentError_("cad_thread_mismatch", "Release CAD references on their owning STA thread")
        self._components.clear()
        self._source_components.clear()
        self._source_documents.clear()
        self._session_document_paths.clear()
        self._session_artifact_roots.clear()
        self._session_artifact_root_owners.clear()
        self._scene_document_key = None
        self._part_mass_properties = {}
        self._effective_instance_readings = {}
        self._capture_expected_documents = None
        self._capture_top_document = None
        self._capture_topology = None
        self._capture_rebuild_journal = []
        self._capture_required_configurations = {}
        self.save_flags.clear()
        with self._sessions_lock:
            sessions = list(self._sessions.items())
        errors = []
        closed_roles = []
        for role, session in sessions:
            if session.closed:
                closed_roles.append((role, session))
                continue
            try:
                session.close()
            except Exception as error:
                errors.append(str(error))
            else:
                closed_roles.append((role, session))
        with self._sessions_lock:
            for role, session in closed_roles:
                if self._sessions.get(role) is session:
                    del self._sessions[role]
            if not self._sessions:
                self._owner_thread = None
        if errors:
            raise EnvironmentError_("cad_process_cleanup_failed", "; ".join(errors))
        self._capture_roots.clear()
        self._requested_configurations.clear()
        self._capture_rebuild_journal = []

    def _active_document(self):
        doc = _modeldoc2(_member(self._app_obj(), "ActiveDoc"))
        if doc is None:
            raise EnvironmentError_("no_active_document", "SolidWorks has no active document")
        return _read_only_document(doc)

    def _document_handle_by_path(self, path):
        """Resolve an open document without changing its read-only state."""

        if not path:
            return self._active_document()
        doc = _modeldoc2(_member(self._app_for_path(path), "GetOpenDocumentByName", path))
        if doc is None or not document_paths_match(_member(doc, "GetPathName"), path):
            raise CadError(
                "document_not_open",
                "requested document is not open in the CAD session the adapter owns; "
                "the adapter reads the revision from disk in a session of its own and never uses yours",
                {"path": path},
            )
        return doc

    def _document_by_path(self, path):
        doc = self._document_handle_by_path(path)
        if not path:
            return doc
        return _read_only_document(doc)

    def health(self):
        app = self._app_obj()
        doc = _modeldoc2(_member(app, "ActiveDoc"))
        return {
            "ok": True,
            "backend": self.name,
            "sw_version": _member(app, "RevisionNumber"),
            "active_document": (_member(doc, "GetPathName") or _member(doc, "GetTitle")) if doc is not None else None,
        }

    def list_documents(self):
        documents = []
        for doc in _member(self._app_obj(), "GetDocuments") or ():
            document = _modeldoc2(doc)
            documents.append(_member(document, "GetPathName") or _member(document, "GetTitle"))
        return documents

    def open_document(self, path):
        # Silent + read-only in the owned application for this phase.
        doc, errors, warnings = self._open_document_raw(path)
        if doc is None or errors.value:
            raise self._open_failure(path, errors.value, warnings.value)
        return {
            "opened": _member(doc, "GetTitle"),
            "path": _member(doc, "GetPathName"),
            "read_only": True,
            "errors": errors.value,
            "warnings": warnings.value,
        }

    def _open_document_raw(self, path):
        """Open a document read-only in the owned application.

        Returns ``(doc, errors, warnings)`` and leaves CAD-reported open errors
        to the caller: :meth:`open_document` applies the strict gate, while the
        main-assembly open records them for the selected document.
        """

        self._require_sealed(path)
        import pythoncom

        wc = _win32()
        kind = {".sldasm": 2, ".sldprt": 1}.get(os.path.splitext(path)[1].lower())
        if kind is None:
            raise CadError("cad_document_type", "only SLDASM/SLDPRT are supported")
        errors = wc.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
        warnings = wc.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
        configuration = self._requested_configurations.get(normalize_document_path(path), "")
        doc = _member(self._app_for_path(path), "OpenDoc6", path, kind, 3, configuration, errors, warnings)
        return doc, errors, warnings

    def _unresolved_references(self, path, limit=32):
        """Best-effort unresolved dependency names and last-known paths.

        Read-only diagnostics for open failures: bounded, never raised, and
        ``None`` when the enumeration is unavailable so the original open error
        keeps its exact reporting.
        """

        try:
            raw = _member(self._app_for_path(path), "GetDocumentDependencies2", path, True, True, True)
        except Exception:  # noqa: BLE001 - diagnostics must never mask the open error
            return None
        try:
            items = list(raw or ())
            unresolved = []
            for index in range(0, len(items) - 2, 3):
                name = items[index]
                if not _is_text_name(name):
                    continue
                try:
                    exists = os.path.isfile(str(items[index + 1]))
                except Exception:  # noqa: BLE001
                    exists = False
                if exists:
                    continue
                unresolved.append({"name": str(name), "last_known_path": str(items[index + 1])})
                if len(unresolved) >= limit:
                    break
            return unresolved
        except Exception:  # noqa: BLE001 - malformed diagnostics cannot replace the CAD error
            return None

    def _open_failure(self, path, errors, warnings, exception=None):
        """Strict open failure; the detail carries unresolved references when readable."""

        detail = {"errors": errors, "warnings": warnings}
        if exception:
            detail["exception"] = exception
        unresolved = self._unresolved_references(path)
        if unresolved is not None:
            detail["unresolved_references"] = unresolved
        return CadError("cad_document_open_failed", path, detail)

    def close_document(self, name, confirm=False):
        if not confirm:
            raise CadError("confirm_required", "closing requires explicit confirmation")
        _member(self._app_obj(), "CloseDoc", name)
        return {"closed": name}

    def close_all_documents(self):
        """Close every open document in the owned application; fail closed on leftovers.

        Used by the pristine readout window: after this returns no document is
        open, so the reopened capture copy starts from a never-touched in-memory
        state.  The real capture opens the collected copy read-write and rebuilds
        and recalculates it, so its documents carry in-memory modifications;
        ``CloseAllDocuments(False)`` silently leaves such dirty documents open in
        a non-interactive session (native16 leftovers: 61 documents), while
        ``CloseAllDocuments(True)`` closes without saving - the collected copy is
        disposable, nothing after the window saves, and freeze re-hashes the
        post-window copy to prove no writes.  Probe v5's read-only session was
        clean, which is why False sufficed there.  Leftovers get a bounded settle
        window (unloading can lag), then ONE force-close attempt per remaining
        document via CloseDoc, and only then the capture fails closed.
        """

        app = self._app_obj()
        try:
            _member(app, "CloseAllDocuments", True)
        except Exception as error:  # noqa: BLE001 - a refused close is a capture failure
            raise CadError(
                "cad_close_failed", "CloseAllDocuments failed", {"error": " ".join(str(error).split())[:200]}
            ) from error
        remaining = self._settled_open_documents(app)
        if remaining:
            for title, path in remaining:
                try:
                    _member(app, "CloseDoc", title or path)
                except Exception:  # noqa: BLE001 - attempted once; a stuck document is reported below
                    continue
            remaining = self._settled_open_documents(app)
        if remaining:
            raise CadError(
                "cad_close_failed",
                "documents remain open after CloseAllDocuments",
                {"open": [path for _title, path in remaining][:20], "count": len(remaining)},
            )
        return {"closed": True}

    def _settled_open_documents(self, app):
        """Open documents as (title, path), polled until empty or the settle window expires."""

        remaining: list[tuple[str, str]] = []
        for attempt in range(CLOSE_ALL_SETTLE_ATTEMPTS):
            remaining = []
            for raw in list(_member(app, "GetDocuments") or []):
                document = _dynamic(raw)
                title = str(_member(document, "GetTitle") or "")
                path = str(_member(document, "GetPathName") or "")
                remaining.append((title, path or title or "?"))
            if not remaining:
                return []
            if attempt < CLOSE_ALL_SETTLE_ATTEMPTS - 1:
                time.sleep(CLOSE_ALL_SETTLE_DELAY_S)
        return remaining

    def _placement(self, component):
        transform = _member(component, "GetTotalTransform", False)
        if transform is None:
            raise CadError("cad_transform_failed", _member(component, "Name2"))
        return transform_from_solidworks(_member(transform, "ArrayData"))

    def _mass_properties_document(self, doc, require_material=True):
        if _member(doc, "GetType") != 1:
            raise CadError("cad_not_part", "mass reader requires a leaf part")
        document_path = str(_member(doc, "GetPathName"))
        document_title = _member(doc, "GetTitle")
        _manager, _configuration, configuration_name = _configuration_context(doc)
        bodies = _part_bodies(doc)
        if not bodies:
            raise CadError("cad_empty_model", "part has no solid bodies")
        # A missing explicit material is a warnable deficiency of the document, not a
        # capture failure: the measured CAD values are kept and the unverified
        # assignment travels verbatim beside them.  A strict (``cad``) capture still
        # refuses an *unreadable* assignment; documented-table mode records every
        # unreadable assignment instead.
        try:
            materials = _material_assignments_document(doc, bodies)
        except CadError as exc:
            if exc.code != "cad_material_provenance_missing" and require_material:
                raise
            materials = unverified_material_record(exc, configuration_name)
        mp = _member(_member(doc, "Extension"), "CreateMassProperty2")
        if mp is None:
            raise CadError("cad_empty_mass_property", "CreateMassProperty2 returned null")
        import pythoncom

        mp.UseSystemUnits = True
        mp.IncludeHiddenBodiesOrComponents = True
        # Calculation-object selection only; never clears the user's selection.
        mp.SelectedItems = _win32().VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_DISPATCH, tuple(bodies))
        _member(mp, "Recalculate")
        override = _member(mp, "GetOverrideOptions")
        overrides = {
            name: bool(_member(override, name))
            for name in ("OverrideMass", "OverrideCenterOfMass", "OverrideMomentsOfInertia")
        }
        if any(overrides.values()):
            raise CadError("cad_mass_override", "pure-CAD export refuses overrides", overrides)
        mass = float(_member(mp, "Mass"))
        com = tuple(map(float, _member(mp, "CenterOfMass")))
        values = tuple(map(float, _member(mp, "GetMomentOfInertia", 0)))
        if len(values) != 9:
            raise CadError("cad_mass_property_inertia_unsupported", "GetMomentOfInertia(0) must return 9 values")
        inertia, _ = _inertia_from_raw(values, document_title)
        if mass <= 0 or not math.isfinite(mass) or len(com) != 3 or not all(map(math.isfinite, com)):
            raise CadError("cad_mass_property_invalid", "mass/COM are not finite and positive")
        return {
            "mass": mass,
            "com": com,
            "inertia": inertia,
            "mode": "full",
            "reference": {
                "used_api": "IMassProperty2.GetMomentOfInertia(0)",
                "scope": "part_document",
                # The convention is proven for this API only (analytic fixture
                # v2, 2026-10-06); fallback arrays are never relabelled.
                "convention_basis": "analytic_fixture_v2_20261006",
                "reference_point": "center_of_mass",
                "axes": "part_document_axes",
                "use_system_units": True,
                # Recorded exactly as the API returned it.  The capture proves
                # the live product convention from its own sealed dual reads
                # and normalizes every recorded tensor to solidworks_standard
                # before sealing (inertia_convention, 2026-10-15); the analytic
                # fixture of 2026-10-06 stays the semantic anchor for what
                # "standard" means.  Consumers refuse any record that bypassed
                # the capture-side throat.
                "product_convention": "solidworks_standard",
                "volume_m3": float(_member(mp, "Volume")),
                "density_kg_m3": float(_member(mp, "Density")),
                "overrides": overrides,
                "body_count": len(bodies),
                "part_document": document_path,
                "configuration": materials["configuration"],
                "material_assignment": materials,
            },
        }

    def assembly_mass_properties(self, path):
        """Whole-assembly reading for the capture's mass-closure record.

        The leaf reader above answers "what does this part weigh"; this answers "what does the
        assembly document say the whole thing weighs", with the same settings and the same refusal
        of mass/COM/inertia overrides.  The capture records both readings; the v1 physics closure
        is a required blocking gate, so a missing or invalid whole-assembly reading fails the
        product gate instead of degrading to an advisory.

        Only ``IMassProperty2`` counts.  When ``CreateMassProperty2`` is unavailable the reader
        fails with an explicit error: an unqualified vector read is not a second implementation
        and cannot publish.
        """

        doc = self._document_by_path(path)
        if _member(doc, "GetType") != 2:
            raise CadError("cad_not_assembly", "assembly mass reader requires a saved SLDASM")
        document_path = str(_member(doc, "GetPathName"))
        document_title = _member(doc, "GetTitle")
        _manager, configuration, configuration_name = _configuration_context(doc)
        root = _component(_member(configuration, "GetRootComponent3", True))
        mp = _member(_member(doc, "Extension"), "CreateMassProperty2")
        if mp is None:
            raise CadError(
                "cad_mass_property_unavailable",
                "CreateMassProperty2 is required; no unqualified mass-only fallback exists",
            )
        if root is None:
            raise CadError("cad_empty_mass_property", "assembly mass property is unavailable")
        import pythoncom

        mp.UseSystemUnits = True
        mp.IncludeHiddenBodiesOrComponents = True
        # Calculation-object selection only; the root component is the whole assembly.
        mp.SelectedItems = _win32().VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_DISPATCH, (root,))
        _member(mp, "Recalculate")
        override = _member(mp, "GetOverrideOptions")
        overrides = {
            name: bool(_member(override, name))
            for name in ("OverrideMass", "OverrideCenterOfMass", "OverrideMomentsOfInertia")
        }
        if any(overrides.values()):
            raise CadError("cad_mass_override", "pure-CAD export refuses overrides", overrides)
        mass = float(_member(mp, "Mass"))
        com = tuple(map(float, _member(mp, "CenterOfMass")))
        values = tuple(map(float, _member(mp, "GetMomentOfInertia", 0)))
        if len(values) != 9:
            raise CadError("cad_mass_property_inertia_unsupported", "GetMomentOfInertia(0) must return 9 values")
        inertia, _ = _inertia_from_raw(values, document_title)
        if mass <= 0 or not math.isfinite(mass) or len(com) != 3 or not all(map(math.isfinite, com)):
            raise CadError("cad_mass_property_invalid", "mass/COM are not finite and positive")
        return {
            "mass": mass,
            "com": com,
            "inertia": inertia,
            "reference": {
                "used_api": "IMassProperty2.GetMomentOfInertia(0)",
                "scope": "assembly_document",
                "reference_point": "center_of_mass",
                "axes": "assembly_document_axes",
                "use_system_units": True,
                "product_convention": "solidworks_standard",
                "convention_basis": "same_api_selection_family_as_measured_group",
                "volume_m3": float(_member(mp, "Volume")),
                "density_kg_m3": float(_member(mp, "Density")),
                "overrides": overrides,
                "document": document_path,
                "configuration": configuration_name,
            },
        }

    def link_group_mass_properties(self, path, selections):
        """One-shot direct group readings for the rigid-body link selections.

        The capture calls this inside the pristine reopen window: after every
        other CAD-facing step it closes all documents, reopens the collected
        copy read-only, and takes these reads (plus one whole-assembly read) as
        the only mass activity of that window.  Any earlier component-instance
        mass read in the session shifts these COM/inertia values (probe v4);
        close+reopen restores the opened state bit-identically (probe v5).
        Recorded per-instance evidence and the closure ledger keep their walked
        values.

        ``selections`` maps each rigid body to the exact occurrence paths the
        producer composer chose (top-most effective providers plus uncovered
        leaves).  Every body is read twice with fresh ``CreateMassProperty2``
        objects in the same session; values are in assembly-document axes about
        the COM.  Component-level instance overrides are part of the CAD value
        and are read as-is, and the object-level flags are recorded verbatim in
        every read - a single-occurrence selection legitimately surfaces the
        instance's authored override there, so the group read never refuses on
        them (the whole-assembly and per-instance readers keep their refusals).
        Resolution is exact: an unresolved path, an empty selection or a
        malformed request aborts the whole call so the capture fails closed,
        never partially.
        """

        doc = self._document_by_path(path)
        if _member(doc, "GetType") != 2:
            raise CadError("cad_not_assembly", "link group mass reader requires a saved SLDASM")
        document_path = str(_member(doc, "GetPathName"))
        document_title = _member(doc, "GetTitle")
        _manager, configuration, configuration_name = _configuration_context(doc)
        root = _component(_member(configuration, "GetRootComponent3", True))
        if root is None:
            raise CadError("cad_empty_mass_property", "assembly has no root component")
        if not isinstance(selections, dict) or not selections:
            raise CadError("cad_empty_selection", "link group mass reader needs at least one body selection")
        requested: dict[str, list[str]] = {}
        wanted: set[str] = set()
        for body, names in selections.items():
            body_name = str(body)
            paths = [str(name) for name in (names or [])]
            if not paths:
                raise CadError("cad_empty_selection", "link group selection is empty", {"link": body_name})
            if len(set(paths)) != len(paths):
                raise CadError(
                    "cad_duplicate_selection",
                    "link group selection repeats an occurrence",
                    {"link": body_name},
                )
            requested[body_name] = sorted(paths)
            wanted.update(paths)
        found: dict[str, object] = {}
        stack: list[object] = list(reversed(list(_method(root, "GetChildren") or ())))
        while stack:
            component = _component(stack.pop())
            if _measured_component_state(component) == COMPONENT_SUPPRESSED:
                continue
            children = list(_method(component, "GetChildren") or ())
            stack.extend(reversed(children))
            name = str(_member(component, "Name2"))
            if name in wanted:
                found[name] = component
        import pythoncom

        def read_once(body_name: str, components: list[object]) -> dict:
            mp = _member(_member(doc, "Extension"), "CreateMassProperty2")
            if mp is None:
                raise CadError("cad_empty_mass_property", "CreateMassProperty2 returned null", {"link": body_name})
            mp.UseSystemUnits = True
            mp.IncludeHiddenBodiesOrComponents = True
            selection = _win32().VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_DISPATCH, tuple(components))
            mp.SelectedItems = selection
            _member(mp, "Recalculate")
            override = _member(mp, "GetOverrideOptions")
            overrides = {
                key: bool(_member(override, key))
                for key in ("OverrideMass", "OverrideCenterOfMass", "OverrideMomentsOfInertia")
            }
            # Instance-level overrides are part of the CAD value and are read
            # as-is; the flags travel verbatim in the read.  A single-occurrence
            # selection surfaces the instance's authored override as an
            # object-level flag in the pristine window (native17 l_ankle), so
            # the group read never refuses on them.  The whole-assembly and
            # per-instance readers keep their own refusals.
            mass = float(_member(mp, "Mass"))
            com = [float(value) for value in _member(mp, "CenterOfMass")]
            values = [float(value) for value in _member(mp, "GetMomentOfInertia", 0)]
            if len(values) != 9:
                raise CadError("cad_mass_property_inertia_unsupported", "GetMomentOfInertia(0) must return 9 values")
            inertia, _axis = _inertia_from_raw(tuple(values), document_title)
            if mass <= 0 or not math.isfinite(mass) or len(com) != 3 or not all(map(math.isfinite, com)):
                raise CadError("cad_mass_property_invalid", "mass/COM are not finite and positive", {"link": body_name})
            read = {
                "mass_kg": mass,
                "com_m": com,
                "inertia_tensor_about_com": [[float(value) for value in row] for row in inertia],
                "overrides": overrides,
            }
            try:
                origin = [float(value) for value in _member(mp, "GetMomentOfInertia", 1)]
            except Exception as error:  # noqa: BLE001 - the about-origin read is diagnostic only
                read["inertia_values_about_origin_api"] = {"error": " ".join(str(error).split())[:200]}
            else:
                read["inertia_values_about_origin_api"] = origin
            for key, member in (("volume_m3", "Volume"), ("density_kg_m3", "Density")):
                try:
                    value = float(_member(mp, member))
                except Exception:  # noqa: BLE001 - optional scalar, omitted as null when unreadable
                    read[key] = None
                else:
                    read[key] = value if math.isfinite(value) else None
            return read

        links: dict[str, dict] = {}
        for body_name, paths in requested.items():
            missing = sorted(set(paths) - set(found))
            if missing:
                raise CadError(
                    "cad_missing_component",
                    "link group selection paths are not component instances of this assembly",
                    {"link": body_name, "missing": missing},
                )
            components = [found[path] for path in paths]
            first = read_once(body_name, components)
            second = read_once(body_name, components)
            tensor_delta = max(
                abs(first["inertia_tensor_about_com"][row][column] - second["inertia_tensor_about_com"][row][column])
                for row in range(3)
                for column in range(3)
            )
            reproducibility = {
                "mass_delta_kg": abs(first["mass_kg"] - second["mass_kg"]),
                "com_delta_norm_m": math.dist(first["com_m"], second["com_m"]),
                "tensor_delta_max_abs": tensor_delta,
            }
            links[body_name] = {
                "selection": paths,
                "reads": [first, second],
                "reproducibility": reproducibility,
            }
        return {
            "assembly": document_path,
            "configuration": configuration_name,
            "used_api": "IMassProperty2 (SelectedItems = component set)",
            "selection_rule": "top_most_providers+uncovered_leaves",
            "links": links,
        }

    def assembly_component_mass_properties(self, path):
        """Per-instance mass in the *assembly context*, with the instance's override flags.

        The leaf reader answers "what does this part document weigh".  This answers "what mass does
        the assembly use for this component instance": ``SelectedItems = (instance,)`` includes
        component-level mass overrides that the part document does not carry, and
        ``GetOverrideOptions`` with the same selection reports which overrides are active.  Every
        instance is walked, not just the top-level children, so a nested override cannot hide
        behind a clean parent.  The capture records the two bases side by side and never
        distributes, scales or rewrites a value.

        Returns ``{"assembly", "configuration", "instances", "errors", "reference"}``.  An instance
        that cannot be read is reported in ``errors`` with its code and message instead of being
        dropped silently.
        """

        doc = self._document_by_path(path)
        if _member(doc, "GetType") != 2:
            raise CadError("cad_not_assembly", "component mass context requires a saved SLDASM")
        document_path = str(_member(doc, "GetPathName"))
        _manager, configuration, configuration_name = _configuration_context(doc)
        root = _component(_member(configuration, "GetRootComponent3", True))
        if root is None:
            raise CadError("cad_empty_mass_property", "assembly mass context has no root component")
        import pythoncom

        entries: list[dict] = []
        errors: list[dict] = []
        overridden_ancestors: set[str] = set()
        stack: list[tuple[object, str | None, int]] = [
            (component, None, 0) for component in reversed(list(_method(root, "GetChildren") or ()))
        ]
        while stack:
            raw, parent, depth = stack.pop()
            name = ""
            children: list = []
            try:
                component = _component(raw)
                raw_name = _member(component, "Name2")
                if raw_name is None or not str(raw_name):
                    raise CadError("cad_component_name_missing", "component instance has no Name2")
                name = str(raw_name)
                if _measured_component_state(component) == COMPONENT_SUPPRESSED:
                    continue
                children = list(_method(component, "GetChildren") or ())
                component_path = str(_method(component, "GetPathName"))
                try:
                    referenced_configuration = str(_member(component, "ReferencedConfiguration") or "")
                except Exception:  # noqa: BLE001 - optional context, never fatal
                    referenced_configuration = ""
            except Exception as error:  # noqa: BLE001 - one unreadable instance must not stop the walk
                errors.append(_instance_error(name, parent, depth, "hierarchy", error))
                continue
            stack.extend((child, name, depth + 1) for child in reversed(children))
            try:
                mp = _member(_member(doc, "Extension"), "CreateMassProperty2")
                if mp is None:
                    raise CadError("cad_empty_mass_property", "CreateMassProperty2 returned null")
                mp.UseSystemUnits = True
                mp.IncludeHiddenBodiesOrComponents = True
                # Keep the array alive until every read below has returned; a temporary released
                # right after the property put would leave the selection's lifetime to the binder.
                selection = _win32().VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_DISPATCH, (component,))
                mp.SelectedItems = selection
                _member(mp, "Recalculate")
                override = _member(mp, "GetOverrideOptions")
                mass = float(_member(mp, "Mass"))
                if not math.isfinite(mass) or mass <= 0.0:
                    raise CadError(
                        "cad_mass_property_invalid", "component mass is not finite and positive", {"name": name}
                    )
                volume = float(_member(mp, "Volume"))
                flags = {
                    key: bool(_member(override, key))
                    for key in ("OverrideMass", "OverrideCenterOfMass", "OverrideMomentsOfInertia")
                }
                document_type = "assembly" if children else "part"
                entry = {
                    "name": name,
                    "parent": parent,
                    "depth": depth,
                    "document": component_path,
                    "document_type": document_type,
                    "context_mass_kg": mass,
                    "context_volume_m3": volume if math.isfinite(volume) else None,
                    "overrides": flags,
                }
                has_override = any(flags.values())
                if has_override:
                    overridden_ancestors.add(name)
                part_reading = (getattr(self, "_part_mass_properties", None) or {}).get(name)
                differs_from_part = (
                    not has_override
                    and document_type == "part"
                    and isinstance(part_reading, dict)
                    and (part_reading.get("reference") or {}).get("scope") == "part_document"
                    and not any(name.startswith(ancestor + "/") for ancestor in overridden_ancestors)
                    and not math.isclose(mass, float(part_reading["mass"]), rel_tol=1e-9, abs_tol=1e-12)
                )
                if has_override or differs_from_part:
                    # Approved override support: one positively identified
                    # instance, full singleton tensor read from the SAME
                    # selection (no second traversal), cached for the
                    # canonical effective-instances record and the scene
                    # mirror.  A failed tensor read is recorded, never
                    # substituted.
                    effective = self._singleton_effective_reading(
                        mp,
                        name,
                        mass,
                        component_path,
                        document_path,
                        configuration_name,
                        referenced_configuration,
                        flags,
                        document_type,
                        measurement_basis=(
                            "cad_assembly_instance_measurement"
                            if differs_from_part
                            else "cad_authored_effective_instance"
                        ),
                        part_reading=part_reading if differs_from_part else None,
                    )
                    entry["effective"] = effective
                    if effective is None:
                        entry["effective_error"] = "cad_effective_instance_unreadable"
                    else:
                        cache = getattr(self, "_effective_instance_readings", None)
                        if cache is None:
                            cache = {}
                            self._effective_instance_readings = cache
                        cache[name] = effective
                entries.append(entry)
            except Exception as error:  # noqa: BLE001 - one unreadable instance is reported, not fatal
                errors.append(_instance_error(name, parent, depth, "mass_property", error))
        return {
            "assembly": document_path,
            "configuration": configuration_name,
            "instances": entries,
            "errors": errors,
            "reference": {
                "method": "IMassProperty2 (SelectedItems = component instance)",
                "override_api": "IMassProperty2.GetOverrideOptions (same selection)",
                "document": document_path,
                "configuration": configuration_name,
            },
        }

    def capture_axis_reference(self, reference):
        """Resolve a named reference axis or a native cylindrical face.

        ``reference`` is the authored joint ``axis_reference``
        (``{component, face_index, body_type}``).  The returned record carries
        the supported identity (component, selector, body type, face index or
        feature name) and the numeric line: a point on the axis, the unit
        direction and the radius, straight from ``ISurface.CylinderParams``.
        Independent verification can then test the authored joint axis for
        collinearity and origin alignment instead of trusting the datum alone.
        The native surface exposes no declared face-name member, so no
        advisory face name is recorded.
        """

        component = str(reference.get("component") or "")
        if reference.get("kind") == "reference_axis":
            name = reference.get("feature_name")
            if not _is_text_name(name) or "face_index" in reference:
                raise CadError("cad_axis_reference_invalid", "a reference axis needs an exact feature_name", reference)
            if not component:
                doc = self._captured_document(self._scene_document_key, phase="axis_reference")
                return _reference_axis_record(doc, name, component, reference)
            if component not in self._components:
                raise CadError("cad_missing_component", component, {"axis_reference": reference})
            with self._current_components("axis_reference"):
                key, _configuration_name = self._source_components[component]
                doc = self._captured_document(key, component, "axis_reference")
                return _reference_axis_record(doc, name, component, reference)
        if component not in self._components:
            raise CadError("cad_missing_component", component, {"axis_reference": reference})
        feature_name = reference.get("feature_name")
        face_index = reference.get("face_index")
        if not _is_text_name(feature_name) and (
            not isinstance(face_index, int) or isinstance(face_index, bool) or face_index < 0
        ):
            raise CadError(
                "cad_axis_reference_invalid",
                "the selector needs a named feature or a non-negative face_index",
                reference,
            )
        body_type = 1 if str(reference.get("body_type") or "solid") == "sheet" else 0
        with self._current_components("axis_reference") as current:
            holder = current[component]
            bodies = self._body_list(holder, body_type, component)
            if _is_text_name(feature_name):
                face = self._cylinder_face_by_feature(bodies, str(feature_name), component)
            else:
                faces = []
                for body in bodies:
                    faces.extend(_as_list(_member(body, "GetFaces")))
                if face_index >= len(faces):
                    raise CadError(
                        "cad_axis_reference_invalid",
                        "face_index is outside the component's faces",
                        {"component": component, "face_index": face_index, "faces": len(faces)},
                    )
                face = faces[face_index]
            surface = _member(face, "GetSurface")
            params = list(map(float, _member(surface, "CylinderParams") or ()))
            if len(params) != 7 or not all(map(math.isfinite, params)):
                raise CadError(
                    "cad_axis_reference_not_cylinder",
                    "the referenced face does not expose cylindrical geometry",
                    {"component": component, "face_index": face_index},
                )
            point, direction, radius = params[0:3], params[3:6], params[6]
            norm = math.sqrt(sum(value * value for value in direction))
            # Planar faces answer CylinderParams with garbage instead of raising, so
            # the geometry itself must prove it is a cylinder: unit axis, positive
            # radius.
            if abs(norm - 1.0) > 1e-6 or radius <= 0.0:
                raise CadError(
                    "cad_axis_reference_not_cylinder",
                    "the referenced face is not a cylinder with a unit axis and positive radius",
                    {"component": component, "face_index": face_index, "radius": radius, "axis_norm": norm},
                )
            record = {
                "component": component,
                "body_type": "sheet" if body_type == 1 else "solid",
                "selector": {key: value for key, value in reference.items() if key != "note"},
                "surface": "cylinder",
                # IComponent2 bodies answer in component/part-local coordinates and
                # the cylinder axis is an undirected line: the authored joint axis
                # supplies the positive direction.
                "coordinate_frame": "component_local",
                "direction_semantics": "undirected_axis_line",
                "axis_point_m": [float(value) for value in point],
                "axis_direction": [float(value) / norm for value in direction],
                "radius_m": float(radius),
                "used_api": ("IComponent2.GetBodies2/IBody2.GetFaces/IFace2.GetSurface/ISurface.CylinderParams"),
            }
            if isinstance(face_index, int) and not isinstance(face_index, bool):
                record["face_index"] = face_index
            persist = self._persist_reference(face)
            if persist is not None:
                record["persist_reference_b64"] = persist
            return record

    def _cylinder_face_by_feature(self, bodies, feature_name, component):
        """Resolve the named feature among this occurrence's actual body faces."""

        found = None
        for body in bodies:
            for candidate in _as_list(_member(body, "GetFaces")):
                feature = _member(candidate, "GetFeature")
                if feature is None or _member(feature, "Name") != feature_name:
                    continue
                params = list(map(float, _member(_member(candidate, "GetSurface"), "CylinderParams") or ()))
                if len(params) != 7 or not all(map(math.isfinite, params)):
                    continue
                norm = math.sqrt(sum(value * value for value in params[3:6]))
                if abs(norm - 1.0) <= 1e-6 and params[6] > 0.0:
                    if found is not None:
                        raise CadError(
                            "cad_axis_reference_ambiguous",
                            "the named feature carries more than one cylindrical face",
                            {"component": component, "feature_name": feature_name},
                        )
                    found = candidate
        if found is None:
            raise CadError(
                "cad_axis_reference_not_cylinder",
                "no cylindrical face found on the named feature",
                {"component": component, "feature_name": feature_name},
            )
        return found

    def _persist_reference(self, face):
        """Best-effort CAD persistent reference for the resolved entity."""

        try:
            import base64

            doc = self._captured_document(self._scene_document_key, phase="axis_reference")
            data = _member(_member(doc, "Extension"), "GetPersistReference3", face)
            if data is None:
                return None
            blob = bytes(int(value) & 0xFF for value in data)
            return base64.b64encode(blob).decode("ascii")
        except Exception:  # noqa: BLE001 - a missing persistent reference is not fatal
            return None

    def _component_override_flags(self, doc, component):
        """Effective override flags for one selected component instance."""

        import pythoncom

        mp = _member(_member(doc, "Extension"), "CreateMassProperty2")
        if mp is None:
            raise CadError("cad_empty_mass_property", "CreateMassProperty2 returned null")
        mp.UseSystemUnits = True
        mp.IncludeHiddenBodiesOrComponents = True
        selection = _win32().VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_DISPATCH, (component,))
        mp.SelectedItems = selection
        _member(mp, "Recalculate")
        override = _member(mp, "GetOverrideOptions")
        return {
            key: bool(_member(override, key))
            for key in ("OverrideMass", "OverrideCenterOfMass", "OverrideMomentsOfInertia")
        }

    def assembly_group_mass_properties(self, path, names, *, allow_overrides=False):
        """Mass properties for a selected group of component instances.

        Measured on the analytic fixture (2026-10-06, SolidWorks 34.0.0): a
        group selection answers in the SAME standard notation the analytic
        parallel-axis combination predicts (residual 6.5e-19 absolute), so the
        reading declares ``solidworks_standard``.  The reading is
        scope-qualified (``assembly_component_group``) so no historical part
        measurement is ever relabelled, and any effective instance override is
        refused instead of silently becoming the value.

        Returns ``{"assembly", "configuration", "group", "members", "mass",
        "com", "inertia", "reference"}``.
        """

        doc = self._document_by_path(path)
        if _member(doc, "GetType") != 2:
            raise CadError("cad_not_assembly", "group mass reader requires a saved SLDASM")
        document_path = str(_member(doc, "GetPathName"))
        document_title = _member(doc, "GetTitle")
        _manager, configuration, configuration_name = _configuration_context(doc)
        root = _component(_member(configuration, "GetRootComponent3", True))
        if root is None:
            raise CadError("cad_empty_mass_property", "assembly has no root component")
        wanted = {str(name) for name in names}
        if not wanted:
            raise CadError("cad_empty_selection", "group mass reader needs at least one component instance")
        selection: list[object] = []
        overrides: dict[str, dict[str, bool]] = {}
        stack: list[object] = list(reversed(list(_method(root, "GetChildren") or ())))
        while stack:
            component = _component(stack.pop())
            if _measured_component_state(component) == COMPONENT_SUPPRESSED:
                continue
            children = list(_method(component, "GetChildren") or ())
            stack.extend(reversed(children))
            name = str(_member(component, "Name2"))
            if name not in wanted:
                continue
            flags = self._component_override_flags(doc, component)
            if any(flags.values()) and not allow_overrides:
                raise CadError(
                    "cad_mass_override",
                    "pure-CAD export refuses component instance overrides",
                    {"component": name, "overrides": flags, "scope": "assembly_component_group"},
                )
            overrides[name] = flags
            selection.append(component)
        missing = sorted(wanted - set(overrides))
        if missing:
            raise CadError(
                "cad_missing_component",
                "group members are not component instances of this assembly",
                {"missing": missing, "group": sorted(wanted)},
            )
        if allow_overrides and len(selection) != 1:
            raise CadError(
                "cad_group_selection_not_singleton",
                "allow_overrides reads exactly one selected occurrence",
                {"selected": sorted(overrides), "requested": sorted(wanted)},
            )
        import pythoncom

        mp = _member(_member(doc, "Extension"), "CreateMassProperty2")
        if mp is None:
            raise CadError("cad_empty_mass_property", "CreateMassProperty2 returned null")
        mp.UseSystemUnits = True
        mp.IncludeHiddenBodiesOrComponents = True
        array = _win32().VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_DISPATCH, tuple(selection))
        mp.SelectedItems = array
        _member(mp, "Recalculate")
        mass = float(_member(mp, "Mass"))
        com = tuple(map(float, _member(mp, "CenterOfMass")))
        values = tuple(map(float, _member(mp, "GetMomentOfInertia", 0)))
        if len(values) != 9:
            raise CadError("cad_mass_property_inertia_unsupported", "GetMomentOfInertia(0) must return 9 values")
        inertia, _ = _inertia_from_raw(values, document_title)
        if mass <= 0 or not math.isfinite(mass) or len(com) != 3 or not all(map(math.isfinite, com)):
            raise CadError("cad_mass_property_invalid", "mass/COM are not finite and positive")
        result = {
            "assembly": document_path,
            "configuration": configuration_name,
            "group": sorted(wanted),
            "members": overrides,
            "mass": mass,
            "com": com,
            "inertia": inertia,
            "reference": {
                "used_api": "IMassProperty2.GetMomentOfInertia(0)",
                "scope": "assembly_component_group",
                "convention_basis": "analytic_fixture_v2_20261006",
                "product_convention": "solidworks_standard",
                "reference_point": "center_of_mass",
                "axes": "assembly_document_axes",
                "use_system_units": True,
                "overrides": overrides,
                "document": document_path,
                "configuration": configuration_name,
            },
        }
        if allow_overrides:
            singleton = overrides[sorted(overrides)[0]]
            effective = bool(any(singleton.values()))
            result["reference"].update(
                {
                    "selection_api": "IMassProperty2 (SelectedItems = component instance)",
                    "override_api": "IMassProperty2.GetOverrideOptions (same selection)",
                    "effective_instance": effective,
                    "measurement_basis": ("cad_authored_effective_instance" if effective else "cad_measured"),
                }
            )
            # Optional native scalars are preserved verbatim when readable and
            # omitted otherwise; nothing is inferred from them.
            for key, member in (("volume_m3", "Volume"), ("density_kg_m3", "Density")):
                try:
                    value = float(_member(mp, member))
                except Exception:  # noqa: BLE001 - an unreadable optional scalar is omitted, never inferred
                    continue
                if math.isfinite(value):
                    result["reference"][key] = value
        return result

    def _singleton_effective_reading(
        self,
        mp,
        name,
        mass,
        component_path,
        assembly_path,
        assembly_configuration,
        referenced,
        flags,
        document_type,
        *,
        measurement_basis="cad_authored_effective_instance",
        part_reading=None,
    ):
        """Full singleton tensor for one already-selected overridden instance.

        Reads COM/tensor/optional scalars from the mass-property object the
        caller created with exactly this instance selected: no second
        traversal, no second selection, no scaling and no substitution.  A
        failed tensor read returns ``None`` so the caller records the failure
        instead of inventing a value.
        """

        try:
            com = tuple(map(float, _member(mp, "CenterOfMass")))
            values = tuple(map(float, _member(mp, "GetMomentOfInertia", 0)))
            if len(values) != 9 or len(com) != 3 or not all(map(math.isfinite, com)):
                return None
            inertia, _ = _inertia_from_raw(values, name)
        except Exception:  # noqa: BLE001 - evidence-driven: the failure is recorded by the caller
            return None
        reference = {
            "used_api": "IMassProperty2.GetMomentOfInertia(0)",
            "selection_api": "IMassProperty2 (SelectedItems = component instance)",
            "override_api": "IMassProperty2.GetOverrideOptions (same selection)",
            "scope": "assembly_component_group",
            "convention_basis": "analytic_fixture_v2_20261006",
            "product_convention": "solidworks_standard",
            "reference_point": "center_of_mass",
            "axes": "assembly_document_axes",
            "use_system_units": True,
            "configuration": referenced,
            "part_document": component_path,
            "assembly_document": assembly_path,
            "assembly_configuration": assembly_configuration,
            "selected_components": [name],
            "effective_instance": True,
            "measurement_basis": measurement_basis,
            "overrides": dict(flags),
        }
        for key, member in (("volume_m3", "Volume"), ("density_kg_m3", "Density")):
            try:
                value = float(_member(mp, member))
            except Exception:  # noqa: BLE001
                continue
            if math.isfinite(value):
                reference[key] = value
        reading = {
            "role": "parent" if document_type == "assembly" else "leaf",
            "document_type": document_type,
            "mass": float(mass),
            "com": [float(value) for value in com],
            "inertia": [list(map(float, row)) for row in inertia],
            "reference": reference,
        }
        if part_reading is not None:
            part_reference = part_reading.get("reference") or {}
            diagnostic = {
                "mass_kg": part_reading["mass"],
                "center_of_mass_m": list(part_reading["com"]),
                "inertia_kg_m2": [list(row) for row in part_reading["inertia"]],
                "scope": part_reference.get("scope"),
                "used_api": part_reference.get("used_api"),
                "product_convention": part_reference.get("product_convention"),
            }
            for key in ("volume_m3", "density_kg_m3"):
                if key in part_reference:
                    diagnostic[key] = part_reference[key]
            reading["part_measurement"] = diagnostic
        return reading

    def effective_instance_readings(self):
        """Cached singleton readings for overridden instances (assembly context)."""

        cache = getattr(self, "_effective_instance_readings", None) or {}
        return {name: dict(value) for name, value in cache.items()}

    def _sheet_only_instance_fallback(self, occurrence, assembly_path, name, referenced, error, counts):
        """Effective CAD-authored instance measurement for a proven sheet-only occurrence.

        The part document of a surface-only occurrence cannot answer mass
        questions (``CreateMassProperty2`` returns no object), while the
        assembly instance carries the CAD-authored effective measurement,
        including any instance overrides.  The fallback is deliberately
        narrow: exactly one selected occurrence, exact boolean override flags
        with at least one true, and the reading keeps its own scope and
        measurement basis.  Nothing is rescaled, substituted or converted
        into part coordinates here; consumers convert from the raw assembly
        coordinates using the recorded placement.
        """

        reading = self.assembly_group_mass_properties(str(assembly_path), [name], allow_overrides=True)
        members = reading.get("members")
        flags = members.get(name) if isinstance(members, dict) else None
        expected_flags = ("OverrideMass", "OverrideCenterOfMass", "OverrideMomentsOfInertia")
        if (
            not isinstance(flags, dict)
            or set(flags) != set(expected_flags)
            or any(type(flags[key]) is not bool for key in expected_flags)
        ):
            raise CadError(
                "cad_effective_instance_flags_invalid",
                "effective instance measurement needs exact boolean override flags",
                {"component": name, "flags": flags},
            )
        if not any(flags.values()):
            raise CadError(
                "cad_effective_instance_flags_invalid",
                "effective instance measurement needs at least one active override",
                {"component": name, "flags": flags},
            )
        reference = dict(reading.get("reference") or {})
        if str(reference.get("used_api")) != "IMassProperty2.GetMomentOfInertia(0)":
            raise CadError(
                "cad_effective_instance_api_mismatch",
                "effective instance measurement must use the qualified tensor API",
                {"used_api": reference.get("used_api")},
            )
        row_reference = {
            "used_api": "IMassProperty2.GetMomentOfInertia(0)",
            "selection_api": str(reference.get("selection_api") or ""),
            "override_api": str(reference.get("override_api") or ""),
            "scope": "assembly_component_group",
            "convention_basis": str(reference.get("convention_basis") or "analytic_fixture_v2_20261006"),
            "product_convention": "solidworks_standard",
            "reference_point": "center_of_mass",
            "axes": "assembly_document_axes",
            "use_system_units": True,
            "configuration": str(referenced),
            "part_document": str(occurrence.path),
            "assembly_document": str(reading.get("assembly") or ""),
            "assembly_configuration": str(reading.get("configuration") or ""),
            "selected_components": [name],
            "fallback_reason": "cad_part_document_mass_unavailable",
            "fallback_error_code": str(error.code),
            "solid_bodies": int(counts.get("solid") or 0),
            "sheet_bodies": int(counts.get("sheet") or 0),
            "effective_instance": True,
            "overrides": {key: bool(flags[key]) for key in expected_flags},
        }
        for key in ("volume_m3", "density_kg_m3"):
            if key in reference:
                row_reference[key] = reference[key]
        return {
            "mass": float(reading["mass"]),
            "com": [float(value) for value in reading["com"]],
            "inertia": [[float(value) for value in row_values] for row_values in reading["inertia"]],
            "mode": "effective_instance",
            "state": "sheet_only",
            "measurement_basis": "cad_authored_effective_instance",
            "reference": row_reference,
        }

    def _capture_source_digest(self, path):
        try:
            return _hash(path)
        except OSError as error:
            raise CadError(
                "cad_capture_source_unreadable",
                "Collected capture source digest could not be read",
                {"path": path, "error": str(error)},
            ) from error

    def _capture_rebuild_state(
        self,
        path,
        *,
        document=None,
        phase="rebuild_state",
        include_assembly_controls=True,
    ):
        """Read one loaded dependency's complete rebuild state without changing it."""

        doc = document or self._document_handle_by_path(path)
        document, configuration = _capture_document_identity(doc, path, phase)
        state = _discovery_document_state(doc)
        state["path"] = document
        state["configurations"] = _configuration_rebuild_rows(doc)
        if include_assembly_controls:
            state["controls"] = _assembly_rebuild_controls(doc)
        if type(state.get("needs_rebuild2_value")) is not int or state["needs_rebuild2_value"] not in (0, 1, 2):
            raise CadError(
                "cad_rebuild_state_unreadable",
                "Assembly dependency has no valid native rebuild state",
                {"path": path, "state": state},
            )
        if type(state.get("configuration_needs_rebuild")) is not bool:
            raise CadError(
                "cad_rebuild_state_unreadable",
                "Assembly dependency has no valid configuration rebuild state",
                {"path": path, "state": state},
            )
        if state.get("configuration") != configuration:
            raise CadError(
                "cad_configuration_mismatch",
                "Assembly dependency configuration identity is inconsistent",
                {"path": path, "state": state},
            )
        return doc, state

    def _capture_loaded_documents(self):
        """Return every loaded capture-role document without inventing paths."""

        app = self._app_obj()
        documents = []
        identities = set()
        paths = set()
        for raw in _as_list(_member(app, "GetDocuments")):
            doc = _modeldoc2(_dynamic(raw))
            path = str(_member(doc, "GetPathName") or "")
            if not path:
                raise CadError(
                    "cad_capture_document_unbound",
                    "A loaded CAD document has no path identity",
                )
            document_type = _member(doc, "GetType")
            if type(document_type) is not int or document_type not in {1, 2}:
                raise CadError(
                    "cad_capture_document_unbound",
                    "A loaded CAD document has an unsupported type",
                    {"path": path, "document_type": document_type},
                )
            normalized = normalize_document_path(os.path.abspath(path))
            identity = _com_iunknown_pointer(doc)
            identity_key = (
                int(identity) if isinstance(identity, int) else repr(identity)
            )
            if identity_key in identities:
                raise CadError(
                    "cad_capture_document_identity",
                    "A loaded CAD document identity is duplicated",
                    {"path": path},
                )
            if normalized in paths:
                raise CadError(
                    "cad_capture_document_identity",
                    "A loaded CAD document path is duplicated",
                    {"path": path},
                )
            identities.add(identity_key)
            paths.add(normalized)
            documents.append(
                {
                    "doc": doc,
                    "identity": identity_key,
                    "normalized_path": normalized,
                    "path": path,
                    "document_type": document_type,
                }
            )
        if not documents:
            raise CadError(
                "cad_capture_document_unbound",
                "The capture session has no loaded CAD documents",
            )
        return sorted(documents, key=lambda row: row["normalized_path"])

    @staticmethod
    def _require_loaded_document_set(loaded_documents, expected_paths):
        loaded_paths = {row["normalized_path"] for row in loaded_documents}
        missing = sorted(expected_paths - loaded_paths)
        extras = sorted(loaded_paths - expected_paths)
        if missing or extras:
            raise CadError(
                "cad_capture_document_unbound",
                "Loaded CAD documents differ from the capture topology",
                {"missing": missing, "extra": extras},
            )

    @staticmethod
    def _capture_required_configuration_map(topology):
        required = {}
        for item in topology:
            if item.get("storage_scope") == "suppressed":
                continue
            required.setdefault(
                normalize_document_path(os.path.abspath(item["document_path"])),
                set(),
            ).add(item["referenced_configuration"])
        return {path: sorted(names) for path, names in required.items()}

    @staticmethod
    def _capture_configuration_state_ready(state, required, *, label):
        names = {
            item["name"]
            for item in state.get("configurations") or []
            if isinstance(item, dict)
        }
        missing = sorted(set(required) - names)
        pending = sorted(
            item["name"]
            for item in state.get("configurations") or []
            if isinstance(item, dict)
            and item.get("name") in required
            and item.get("needs_rebuild") is not False
        )
        if missing or pending:
            raise CadError(
                "cad_rebuild_incomplete",
                f"{label} has missing or pending required configurations",
                {
                    "path": state.get("path"),
                    "required": sorted(required),
                    "missing": missing,
                    "pending": pending,
                },
            )

    def _capture_assert_documents_ready(self, loaded_documents, *, phase):
        rows = []
        for loaded in loaded_documents:
            doc, state = self._capture_rebuild_state(
                loaded["path"],
                document=loaded["doc"],
                phase=phase,
                include_assembly_controls=loaded["document_type"] == 2,
            )
            del doc
            required = self._capture_required_configurations.get(loaded["normalized_path"], [])
            self._capture_configuration_state_ready(
                state,
                required,
                label=f"Capture read boundary {phase}",
            )
            if state.get("needs_rebuild2_value") != 0 or state.get("configuration_needs_rebuild") is not False:
                raise CadError(
                    "cad_rebuild_incomplete",
                    "Capture reads left a CAD document pending",
                    {"path": loaded["path"], "phase": phase, "state": state},
                )
            rows.append(
                {
                    "path": loaded["path"],
                    "document_type": loaded["document_type"],
                    "state": state,
                }
            )
        return rows

    @staticmethod
    def _capture_semantic_topology(topology):
        rows = []
        for row in topology:
            item = {
                key: row.get(key)
                for key in (
                    "instance",
                    "document_type",
                    "referenced_configuration",
                    "suppression_state",
                    "storage_scope",
                    "provenance_state",
                    "owner_document_path",
                    "owner_sha256",
                )
            }
            if row.get("storage_scope") != "session":
                item["normalized_path"] = row.get("normalized_path")
            rows.append(item)
        return sorted(
            rows,
            key=lambda row: tuple(
                "" if row.get(key) is None else str(row.get(key)).casefold()
                for key in (
                    "instance",
                    "normalized_path",
                    "document_type",
                    "referenced_configuration",
                    "suppression_state",
                    "storage_scope",
                    "provenance_state",
                    "owner_sha256",
                )
            ),
        )

    def _capture_session_evidence(self, path):
        role = self._role_for_path(path)
        session = self._sessions.get(role)
        try:
            identity = copy.deepcopy(session.identity()) if session is not None else None
            if (
                role != "copy"
                or not isinstance(identity, dict)
                or identity.get("state") != "active"
                or type(identity.get("pid")) is not int
                or identity["pid"] <= 0
            ):
                raise ValueError("capture has no active owned copy session")
            root = self._session_artifact_roots.get(session)
            if root is not None and session_root_pid(root) != identity["pid"]:
                raise ValueError("capture session root differs from its process identity")
        except Exception as error:
            raise CadError(
                "cad_capture_session_unbound",
                "Capture read boundary has no owned process identity",
                {"path": str(path), "error": str(error)[:300]},
            ) from error
        return {"role": role, "identity": identity, "temp_root": root}

    def verify_capture_documents_ready(self):
        """Fail closed unless every loaded document is clean at this read boundary."""

        expected_topology = getattr(self, "_capture_topology", None)
        top_path = getattr(self, "_capture_top_document", None)
        if not isinstance(expected_topology, list) or not top_path:
            raise CadError(
                "cad_capture_document_unbound",
                "Capture document authority was not initialized",
            )
        graph = self._capture_rebuild_graph(
            self._document_by_path(top_path),
            top_path,
        )
        self._capture_required_configurations = self._capture_required_configuration_map(
            graph["topology"]
        )
        if (
            self._capture_semantic_topology(graph["topology"])
            != expected_topology
        ):
            raise CadError(
                "cad_rebuild_topology_changed",
                "Capture read boundary changed the engineering topology",
                {
                    "before": expected_topology,
                    "after": self._capture_semantic_topology(graph["topology"]),
                },
            )
        rows = self._capture_assert_documents_ready(
            graph["loaded_documents"],
            phase="capture_final",
        )
        normalized_top = normalize_document_path(os.path.abspath(top_path))
        top = next(
            row
            for row in rows
            if normalize_document_path(os.path.abspath(row["path"]))
            == normalized_top
        )
        top_state = top["state"]
        if top_state.get("read_only") is not True:
            raise CadError(
                "cad_rebuild_incomplete",
                "Capture reads did not end with a read-only top assembly",
                {"path": top_path, "state": top_state},
            )
        return {
            "document_count": len(rows),
            "parts": sum(row["document_type"] == 1 for row in rows),
            "assemblies": sum(row["document_type"] == 2 for row in rows),
            "documents": rows,
            "topology": copy.deepcopy(graph["topology"]),
            "session": self._capture_session_evidence(top_path),
        }

    def _capture_rebuild_graph(self, doc, path):
        """Return unique assembly documents, dependencies, and exact live topology."""

        normalized_top = normalize_document_path(os.path.abspath(path))
        if not any(normalized_top.startswith(root + "\\") for root in self._capture_roots):
            raise CadError("cad_rebuild_scope", "Only a collected capture copy may be rebuilt")
        source_root = Path(path).resolve().parent
        _top_doc, top_state = self._capture_rebuild_state(path, document=doc, phase="before")
        try:
            _manager, top_configuration = _active_configuration_view(doc)
            top_configuration_name = str(_member(top_configuration, "Name") or "")
            root = _component(_member(top_configuration, "GetRootComponent3", True))
        except Exception as error:  # noqa: BLE001 - graph unreadable before mutation
            raise CadError(
                "cad_rebuild_state_unreadable",
                "Capture component topology could not be read",
                {"path": path, "error": f"{type(error).__name__}: {error}"[:300]},
            ) from error
        if root is None:
            raise CadError("cad_rebuild_state_unreadable", "Capture topology has no root component", {"path": path})

        assemblies = {
            normalized_top: {
                "depth": 0,
                "normalized_path": normalized_top,
                "path": path,
                "parents": set(),
                "referenced_configurations": {top_configuration_name},
                "instances": ["top"],
            }
        }
        part_documents = {}
        source_files = {normalized_top: {"path": path, "sha256": self._capture_source_digest(path)}}
        session_files = []
        topology = [
            {
                "instance": "top",
                "depth": 0,
                "document_path": path,
                "normalized_path": normalized_top,
                "document_type": 2,
                "referenced_configuration": top_configuration_name,
                "suppression_state": None,
                "storage_scope": "file",
                "provenance_state": None,
                "owner_document_path": None,
                "owner_sha256": None,
                "native_api_pid": None,
            }
        ]
        topology_by_path = {normalized_top: topology[0]}
        native_api_pid = None
        component_count = 0
        stack = [
            (item, 1, normalized_top)
            for item in reversed(_as_list(_method(root, "GetChildren")))
        ]
        while stack:
            raw, depth, parent_assembly = stack.pop()
            component_count += 1
            if component_count > 500 or depth > 40:
                raise CadError(
                    "cad_rebuild_state_unreadable",
                    "Capture component topology exceeds its bounded traversal",
                    {"path": path, "component_count": component_count, "depth": depth},
                )
            try:
                component = _component(raw)
                state = _component_state(component)
                name = str(_member(component, "Name2") or "")
                if state == COMPONENT_SUPPRESSED:
                    topology.append(
                        {
                            "instance": name,
                            "depth": depth,
                            "document_path": None,
                            "normalized_path": None,
                            "document_type": None,
                            "referenced_configuration": None,
                            "suppression_state": state,
                            "storage_scope": "suppressed",
                            "provenance_state": None,
                            "owner_document_path": None,
                            "owner_sha256": None,
                            "native_api_pid": None,
                        }
                    )
                    continue
                if state not in {2, 3}:
                    raise CadError(
                        "cad_rebuild_state_unreadable",
                        "Capture dependency is not fully loaded before rebuild",
                        {"instance": _member(component, "Name2"), "suppression_state": state},
                    )
                model = _component_document(component)
                if model is None:
                    raise CadError(
                        "cad_rebuild_state_unreadable",
                        "Capture dependency has no loaded model document",
                        {"instance": _member(component, "Name2")},
                    )
                document_path = str(_member(model, "GetPathName") or "")
                if not document_path:
                    raise CadError(
                        "cad_rebuild_state_unreadable",
                        "Capture dependency has no document path",
                        {"instance": _member(component, "Name2")},
                    )
                document_type = _member(model, "GetType")
                if type(document_type) is not int or document_type not in {1, 2}:
                    raise CadError(
                        "cad_document_type",
                        "Capture dependency has no supported native document type",
                        {"path": document_path, "document_type": document_type},
                    )
                referenced = str(_member(component, "ReferencedConfiguration") or "")
                normalized = normalize_document_path(os.path.abspath(document_path))
                in_capture = any(normalized.startswith(root_path + "\\") for root_path in self._capture_roots)
                provenance_state = None
                if in_capture:
                    storage_scope = "file"
                    source_files.setdefault(
                        normalized,
                        {"path": document_path, "sha256": self._capture_source_digest(document_path)},
                    )
                else:
                    source_document = _component_source_document(
                        component,
                        source_root,
                        native_path=document_path,
                    )
                    provenance = source_document.get("source_provenance") or {}
                    provenance_state = provenance.get("state")
                    if provenance_state not in {
                        "cached_materialization",
                        "embedded_virtual_component",
                    }:
                        raise CadError(
                            "cad_rebuild_scope",
                            "Capture dependency is neither a collected file nor a proven session document",
                            {
                                "path": document_path,
                                "instance": name,
                                "source_provenance": provenance,
                            },
                        )
                    storage_scope = "session"
                    role = self._role_for_path(document_path)
                    self._bind_session_document(
                        role,
                        self._sessions.get(role),
                        document_path,
                    )
                    session_files.append(document_path)
                owner_document_path = None
                owner_sha256 = None
                session_pid = None
                if storage_scope == "session":
                    parent_row = topology_by_path.get(parent_assembly)
                    if parent_row is not None and parent_row.get("storage_scope") == "session":
                        owner_document_path = parent_row["owner_document_path"]
                        owner_sha256 = parent_row["owner_sha256"]
                    else:
                        owner_document_path = parent_assembly
                        owner_sha256 = (
                            source_files[owner_document_path]["sha256"]
                            if owner_document_path in source_files
                            else self._capture_source_digest(owner_document_path)
                        )
                    if native_api_pid is None:
                        try:
                            owner_session = self._sessions.get(role)
                            identity = (
                                owner_session.identity()
                                if owner_session is not None
                                else None
                            )
                            native_api_pid = (
                                identity.get("pid")
                                if isinstance(identity, dict)
                                else None
                            )
                            if type(native_api_pid) is not int or native_api_pid <= 0:
                                raise ValueError("owned session identity has no positive pid")
                        except Exception as error:
                            raise CadError(
                                "cad_rebuild_state_unreadable",
                                "A session occurrence has no readable native process identity",
                                {
                                    "path": document_path,
                                    "error": f"{type(error).__name__}: {error}"[:300],
                                },
                            ) from error
                    session_pid = native_api_pid
                    artifact_root = (
                        session_temp_root(document_path)
                        or embedded_virtual_component_root(document_path)
                    )
                    root_pid = session_root_pid(artifact_root)
                    if root_pid != session_pid:
                        raise CadError(
                            "cad_rebuild_state_unreadable",
                            "A session occurrence path does not belong to its native process",
                            {
                                "path": document_path,
                                "path_pid": root_pid,
                                "native_api_pid": session_pid,
                            },
                        )
                topology.append(
                    {
                        "instance": name,
                        "depth": depth,
                        "document_path": document_path,
                        "normalized_path": normalized,
                        "document_type": document_type,
                        "referenced_configuration": referenced,
                        "suppression_state": state,
                        "storage_scope": storage_scope,
                        "provenance_state": provenance_state,
                        "owner_document_path": owner_document_path,
                        "owner_sha256": owner_sha256,
                        "native_api_pid": session_pid,
                    }
                )
                topology_by_path.setdefault(normalized, topology[-1])
                if document_type == 2:
                    row = assemblies.setdefault(
                        normalized,
                        {
                            "depth": depth,
                            "normalized_path": normalized,
                            "path": document_path,
                            "parents": set(),
                            "referenced_configurations": set(),
                            "instances": [],
                        },
                    )
                    row["depth"] = max(int(row["depth"]), depth)
                    row["parents"].add(parent_assembly)
                    if referenced:
                        row["referenced_configurations"].add(referenced)
                    if name:
                        row["instances"].append(name)
                elif document_type == 1:
                    row = part_documents.setdefault(
                        normalized,
                        {
                            "doc": model,
                            "path": document_path,
                            "instances": [],
                        },
                    )
                    if name:
                        row["instances"].append(name)
                child_parent = normalized if document_type == 2 else parent_assembly
                stack.extend(
                    (child, depth + 1, child_parent)
                    for child in reversed(_as_list(_method(component, "GetChildren")))
                )
            except CadError:
                raise
            except Exception as error:  # noqa: BLE001 - fail before any rebuild mutation
                raise CadError(
                    "cad_rebuild_state_unreadable",
                    "Capture dependency topology could not be read",
                    {"path": path, "error": f"{type(error).__name__}: {error}"[:300]},
                ) from error

        expected_documents = set(assemblies) | set(part_documents)
        loaded_documents = self._capture_loaded_documents()
        self._require_loaded_document_set(loaded_documents, expected_documents)
        loaded_by_path = {
            row["normalized_path"]: row for row in loaded_documents
        }
        states = []
        for normalized, row in assemblies.items():
            dependency_doc, state = (
                (doc, top_state)
                if normalized == normalized_top
                else self._capture_rebuild_state(
                    row["path"],
                    document=loaded_by_path[normalized]["doc"],
                    phase="before",
                )
            )
            configurations = row["referenced_configurations"]
            if not configurations or len(configurations) != 1:
                raise CadError(
                    "cad_rebuild_state_unreadable",
                    "Assembly dependency has no single referenced configuration",
                    {"path": row["path"], "configurations": sorted(configurations)},
                )
            referenced = next(iter(configurations))
            if normalized != normalized_top and state.get("configuration") != referenced:
                raise CadError(
                    "cad_configuration_mismatch",
                    "Assembly dependency active configuration differs from its reference",
                    {
                        "path": row["path"],
                        "referenced": referenced,
                        "active": state.get("configuration"),
                    },
                )
            row.update(
                {
                    "doc": dependency_doc if normalized != normalized_top else doc,
                    "initial_state": state,
                    "referenced_configuration": referenced,
                }
            )
            states.append(row)
        states.sort(key=lambda item: (-int(item["depth"]), normalize_document_path(item["path"])))
        part_states = []
        for normalized, row in part_documents.items():
            _part_doc, state = self._capture_rebuild_state(
                row["path"],
                document=loaded_by_path[normalized]["doc"],
                phase="before",
                include_assembly_controls=False,
            )
            part_states.append(
                {
                    "normalized_path": normalized,
                    "path": row["path"],
                    "instances": sorted(row["instances"]),
                    "initial_state": state,
                }
            )
        part_states.sort(key=lambda item: item["normalized_path"])
        by_path = {row["normalized_path"]: row for row in states}
        initial_pending_children = {
            row["normalized_path"]
            for row in states
            if row["normalized_path"] != normalized_top
            and row["initial_state"].get("needs_rebuild2_value") in (1, 2)
        }
        rebuild_candidates = set(initial_pending_children)
        pending = list(initial_pending_children)
        while pending:
            child = pending.pop()
            for parent in by_path[child]["parents"]:
                if parent == normalized_top or parent in rebuild_candidates:
                    continue
                rebuild_candidates.add(parent)
                pending.append(parent)
        loaded_rows = []
        state_by_path = {
            row["normalized_path"]: row["initial_state"] for row in states
        }
        state_by_path.update(
            {
                row["normalized_path"]: row["initial_state"]
                for row in part_states
            }
        )
        topology_rows = {
            row["normalized_path"]: row for row in topology if row["normalized_path"]
        }
        for loaded in loaded_documents:
            normalized = loaded["normalized_path"]
            topology_row = topology_rows[normalized]
            loaded_rows.append(
                {
                    "doc": loaded["doc"],
                    "path": loaded["path"],
                    "normalized_path": normalized,
                    "document_type": loaded["document_type"],
                    "instance": topology_row["instance"],
                    "state": state_by_path[normalized],
                }
            )
        return {
            "assemblies": states,
            "component_count": component_count,
            "initial_pending_children": sorted(initial_pending_children),
            "loaded_documents": loaded_rows,
            "normalized_top": normalized_top,
            "part_documents": part_states,
            "rebuild_candidates": sorted(rebuild_candidates),
            "session_files": sorted(set(session_files)),
            "source_files": source_files,
            "topology": sorted(topology, key=_capture_topology_sort_key),
        }

    @staticmethod
    def _capture_graph_top_row(graph):
        normalized_top = graph["normalized_top"]
        return next(
            row
            for row in graph["assemblies"]
            if row["normalized_path"] == normalized_top
        )

    def _capture_bind_graph(self, graph, path):
        self._capture_expected_documents = {
            row["normalized_path"] for row in graph["loaded_documents"]
        }
        self._capture_top_document = path
        self._capture_topology = self._capture_semantic_topology(
            graph["topology"]
        )
        self._capture_required_configurations = (
            self._capture_required_configuration_map(graph["topology"])
        )

    @classmethod
    def _capture_owner_identity(cls, path):
        """Normalize an owner by the owner's own controlled storage class."""

        if session_artifact_path(path):
            return cls._capture_session_path_signature(path)
        return normalize_document_path(os.path.abspath(path))

    @staticmethod
    def _capture_session_path_signature(path):
        text = str(path or "")
        root = session_temp_root(text)
        marker = "ic~~"
        if root is None:
            root = embedded_virtual_component_root(text)
            marker = "vc~~"
        if root is None or session_root_pid(root) is None:
            raise ValueError("a session path has no controlled owned root")
        module = posixpath if text.startswith("/") else ntpath
        normalized = module.normcase(module.normpath(text))
        normalized_root = module.normcase(module.normpath(root))
        relative = module.relpath(normalized, normalized_root)
        parts = re.split(r"[\\/]", relative)
        if (
            not parts
            or parts[0].casefold() != marker
            or any(part in {"", ".", ".."} for part in parts)
            or module.isabs(relative)
        ):
            raise ValueError("a session path escapes its controlled owned root")
        return "/".join(part.casefold() for part in parts)

    @classmethod
    def _capture_stable_topology(cls, rows):
        stable = []
        for row in rows:
            item = {
                "instance": row["instance"],
                "depth": row["depth"],
                "document_type": row["document_type"],
                "referenced_configuration": row["referenced_configuration"],
                "suppression_state": row["suppression_state"],
                "storage_scope": row["storage_scope"],
                "provenance_state": row["provenance_state"],
                "owner_sha256": row["owner_sha256"],
            }
            if row["storage_scope"] == "session":
                item["session_path"] = cls._capture_session_path_signature(
                    row["document_path"]
                )
            else:
                item["normalized_path"] = row["normalized_path"]
            if row["owner_document_path"] is not None:
                item["owner_identity"] = cls._capture_owner_identity(
                    row["owner_document_path"]
                )
            stable.append(item)
        return sorted(
            stable,
            key=lambda row: json.dumps(
                row,
                sort_keys=True,
                separators=(",", ":"),
            ),
        )

    @classmethod
    def _capture_session_file_signatures(cls, paths):
        return sorted(cls._capture_session_path_signature(path) for path in paths)

    @classmethod
    def _capture_loaded_document_signature(cls, graph):
        files = []
        sessions = []
        document_types = []
        for row in graph["loaded_documents"]:
            path = row["path"]
            if session_artifact_path(path):
                sessions.append(cls._capture_session_path_signature(path))
            else:
                files.append(normalize_document_path(os.path.abspath(path)))
            document_types.append(
                {
                    "path": (
                        cls._capture_session_path_signature(path)
                        if session_artifact_path(path)
                        else normalize_document_path(os.path.abspath(path))
                    ),
                    "document_type": row["document_type"],
                }
            )
        if (
            len(files) != len(set(files))
            or len(sessions) != len(set(sessions))
            or set(files) != set(graph["source_files"])
            or sorted(sessions)
            != cls._capture_session_file_signatures(graph["session_files"])
        ):
            raise ValueError(
                "loaded documents do not match the source and session inventories"
            )
        return {
            "file_paths": sorted(files),
            "session_paths": sorted(sessions),
            "document_types": sorted(
                document_types,
                key=lambda row: (row["path"], row["document_type"]),
            ),
        }

    def _rebuild_capture_document(self, row, *, phase, document=None):
        """Rebuild one pending assembly once and require an immediate clean state."""

        path = row["path"]
        before = row["state_before"]
        sequence = len(self._capture_rebuild_journal)
        journal = {
            "sequence": sequence,
            "kind": phase if phase in {"top", "top_recovery"} else "child",
            "phase": phase,
            "path": path,
            "depth": row["depth"],
            "state_before": copy.deepcopy(before),
        }
        self._capture_rebuild_journal.append(journal)
        detail_phase = "child" if phase == "child" else "rebuild"
        call_phase = "resolve_handle"
        elapsed = None
        try:
            dependency_doc = document or self._document_handle_by_path(path)
            extension = _member(dependency_doc, "Extension")
            call_phase = "rebuild"
            started = time.monotonic()
            result = _member(extension, "Rebuild", REBUILD_OPTIONS_FORCE_ALL)
            elapsed = time.monotonic() - started
        except Exception as error:  # noqa: BLE001 - raw COM failure is retained
            journal.update(
                {
                    "error_code": "cad_rebuild_failed",
                    "error_message": "Assembly rebuild raised a native error",
                    "error_phase": call_phase,
                    "error": f"{type(error).__name__}: {error}"[:300],
                    "elapsed_seconds": elapsed,
                }
            )
            raise CadError(
                "cad_rebuild_failed",
                "Assembly rebuild raised a native error",
                {
                    "path": path,
                    "depth": row["depth"],
                    "phase": detail_phase,
                    "error": f"{type(error).__name__}: {error}"[:300],
                    "state": before,
                    "state_before": before,
                    "elapsed_seconds": elapsed,
                },
            ) from error
        journal.update({"result": result, "elapsed_seconds": elapsed})
        if result is not True:
            journal.update(
                {
                    "result": result,
                    "error_code": "cad_rebuild_failed",
                    "error_message": "Assembly did not rebuild successfully",
                }
            )
            raise CadError(
                "cad_rebuild_failed",
                "Assembly did not rebuild successfully",
                {
                    "path": path,
                    "depth": row["depth"],
                    "phase": detail_phase,
                    "result": result,
                    "state": before,
                    "state_before": before,
                    "elapsed_seconds": elapsed,
                },
            )
        try:
            refreshed = (
                self._document_by_path(path)
                if detail_phase == "rebuild"
                else self._document_handle_by_path(path)
            )
        except CadError as error:
            journal.update(
                {
                    "error_code": error.code,
                    "error_message": error.message,
                    "error_phase": "refresh",
                    "error": str(error)[:300],
                    "elapsed_seconds": elapsed,
                }
            )
            raise
        except Exception as error:  # noqa: BLE001 - refresh failure keeps stable evidence
            journal.update(
                {
                    "result": result,
                    "error_code": "cad_capture_refresh_failed",
                    "error_message": (
                        "Rebuilt capture document could not be resolved"
                    ),
                    "error_phase": "refresh",
                    "error": f"{type(error).__name__}: {error}"[:300],
                    "elapsed_seconds": elapsed,
                }
            )
            raise CadError(
                "cad_capture_refresh_failed",
                "Rebuilt capture document could not be resolved",
                {
                    "path": path,
                    "phase": "refresh",
                    "error": f"{type(error).__name__}: {error}"[:300],
                    "state_before": before,
                    "elapsed_seconds": elapsed,
                },
            ) from error
        try:
            _, after = self._capture_rebuild_state(path, document=refreshed, phase="after")
        except CadError as error:
            journal.update(
                {
                    "error_code": error.code,
                    "error_message": error.message,
                    "error_phase": "after_state",
                    "error": str(error)[:300],
                    "elapsed_seconds": elapsed,
                }
            )
            raise
        if after.get("configuration") != before.get("configuration"):
            journal.update(
                {
                    "result": result,
                    "state_after": copy.deepcopy(after),
                    "error_code": "cad_configuration_mismatch",
                    "error_message": (
                        "Assembly rebuild changed the active configuration"
                    ),
                    "elapsed_seconds": elapsed,
                }
            )
            raise CadError(
                "cad_configuration_mismatch",
                "Assembly rebuild changed the active configuration",
                {
                    "path": path,
                    "depth": row["depth"],
                    "phase": detail_phase,
                    "before": before,
                    "after": after,
                    "elapsed_seconds": elapsed,
                },
            )
        if after.get("needs_rebuild2_value") != 0 or after.get("configuration_needs_rebuild") is not False:
            journal.update(
                {
                    "result": result,
                    "state_after": copy.deepcopy(after),
                    "error_code": "cad_rebuild_incomplete",
                    "error_message": (
                        "Assembly rebuild did not end in a fully rebuilt "
                        "configuration"
                    ),
                    "elapsed_seconds": elapsed,
                }
            )
            raise CadError(
                "cad_rebuild_incomplete",
                "Assembly rebuild did not end in a fully rebuilt configuration",
                {
                    "path": path,
                    "depth": row["depth"],
                    "phase": detail_phase,
                    "state_before": before,
                    "state_after": after,
                    "rebuild_result": result,
                    "state": after,
                    "elapsed_seconds": elapsed,
                },
            )
        journal.update(
            {
                "result": result,
                "state_after": copy.deepcopy(after),
                "elapsed_seconds": elapsed,
            }
        )
        row["call_sequence"] = sequence
        return refreshed, after, {
            "phase": phase,
            "path": path,
            "depth": row["depth"],
            "instances": list(row["instances"]),
            "elapsed_seconds": elapsed,
            "result": result,
            "call_sequence": sequence,
            "state_before": before,
            "state_after": after,
        }

    def _capture_rebuild_pending_children(self, graph, *, document):
        """Rebuild one live dirty-ancestor pass and refresh the top handle."""

        top = self._capture_graph_top_row(graph)
        child_rebuilds = []
        rebuild_candidates = set(graph["rebuild_candidates"])
        for row in graph["assemblies"]:
            # A sibling rebuild may dirty unrelated top-level assemblies. Those
            # are outside the initial dirty-ancestor closure and belong to the
            # bounded top pass rather than a repeatable child cascade.
            if (
                row is top
                or row["normalized_path"] not in rebuild_candidates
            ):
                continue
            child_doc, state_before = self._capture_rebuild_state(
                row["path"],
                phase="before_child",
            )
            row["state_before"] = state_before
            self._capture_rebuild_journal.append(
                {
                    "sequence": len(self._capture_rebuild_journal),
                    "kind": "candidate",
                    "path": row["path"],
                    "depth": row["depth"],
                    "required_configurations": list(
                        self._capture_required_configurations[
                            row["normalized_path"]
                        ]
                    ),
                    "state": copy.deepcopy(state_before),
                    "decision": (
                        "rebuild"
                        if state_before["needs_rebuild2_value"] in (1, 2)
                        else "skip_clean"
                    ),
                }
            )
            if state_before["needs_rebuild2_value"] not in (1, 2):
                continue
            _, after, evidence = self._rebuild_capture_document(
                row,
                phase="child",
                document=child_doc,
            )
            row["state_after"] = after
            child_rebuilds.append(evidence)

        if child_rebuilds:
            document, state_after_children = self._capture_rebuild_state(
                top["path"],
                phase="after_children",
            )
        else:
            _, state_after_children = self._capture_rebuild_state(
                top["path"],
                document=document,
                phase="after_children",
            )
        return document, state_after_children, child_rebuilds

    def _capture_rebuild_attempt_evidence(self, graph, session):
        """Freeze one session's graph, source bytes, and initial live states."""

        return {
            "session": copy.deepcopy(session),
            "source_files": copy.deepcopy(graph["source_files"]),
            "topology": copy.deepcopy(graph["topology"]),
            "loaded_documents": self._capture_loaded_document_signature(graph),
            "assembly_states": [
                {
                    "depth": row["depth"],
                    "path": row["path"],
                    "instances": list(row["instances"]),
                    "state": copy.deepcopy(row["initial_state"]),
                }
                for row in graph["assemblies"]
            ],
            "initial_pending": list(graph["initial_pending_children"]),
            "rebuild_candidates": list(graph["rebuild_candidates"]),
        }

    @staticmethod
    def _capture_session_pid(identity, label):
        if (
            not isinstance(identity, dict)
            or identity.get("state") != "active"
            or type(identity.get("pid")) is not int
            or identity["pid"] <= 0
        ):
            raise ValueError(f"{label} has no active owned process identity")
        return identity["pid"]

    def _capture_recover_top_rebuild(
        self,
        *,
        graph,
        path,
        child_rebuilds,
        failure,
    ):
        """Repeat one bounded live rebuild pass in a proven fresh session."""

        detail = failure.detail if isinstance(failure.detail, dict) else {}
        top = self._capture_graph_top_row(graph)
        failed_state = detail.get("state_after")
        recoverable = (
            failure.code == "cad_rebuild_incomplete"
            and detail.get("phase") == "rebuild"
            and detail.get("depth") == 0
            and detail.get("rebuild_result") is True
            and isinstance(detail.get("path"), str)
            and normalize_document_path(os.path.abspath(detail["path"]))
            == graph["normalized_top"]
            and isinstance(failed_state, dict)
            and failed_state.get("needs_rebuild2_value") in (1, 2)
            and failed_state.get("configuration_needs_rebuild") is False
            and bool(child_rebuilds)
        )
        if not recoverable:
            raise failure

        first_journal = copy.deepcopy(self._capture_rebuild_journal)
        old_session = self._capture_session_evidence(path)
        old_pid = self._capture_session_pid(
            old_session["identity"],
            "retired capture session",
        )
        first_attempt = self._capture_rebuild_attempt_evidence(
            graph,
            old_session,
        )
        first_attempt["rebuild_calls"] = first_journal

        recovery = {
            "schema_version": (
                "solidworks-to-urdf.rebuild-session-recovery/v3"
            ),
            "first_attempt": first_attempt,
            "fresh_attempt": None,
            "retirement": None,
            "reopen": None,
            "fresh_session": None,
        }

        try:
            retirement = self.retire_session("copy")
            recovery["retirement"] = retirement
            before = retirement.get("before") if isinstance(retirement, dict) else None
            after = retirement.get("after") if isinstance(retirement, dict) else None
            if (
                retirement.get("retired") is not True
                or retirement.get("role") != "copy"
                or before != old_session["identity"]
                or not isinstance(after, dict)
                or after.get("state") != "closed"
                or after.get("pid") != old_pid
            ):
                raise ValueError(
                    "the prior owned capture process is not proven retired"
                )

            opened = self.open_document(path)
            recovery["reopen"] = copy.deepcopy(opened)
            fresh_session = self._capture_session_evidence(path)
            recovery["fresh_session"] = fresh_session
            fresh_pid = self._capture_session_pid(
                fresh_session["identity"],
                "fresh capture session",
            )
            if fresh_pid == old_pid:
                raise ValueError(
                    "the fresh capture session reused the retired process"
                )
            recovery["fresh_attempt"] = {
                "session": copy.deepcopy(fresh_session),
                "phase": "graph",
                "loaded_inventory": None,
                "error": None,
            }
            fresh_document = self._document_handle_by_path(path)
            try:
                recovery["fresh_attempt"]["loaded_inventory"] = [
                    {
                        "path": row["path"],
                        "document_type": row["document_type"],
                    }
                    for row in self._capture_loaded_documents()
                ]
                fresh_graph = self._capture_rebuild_graph(fresh_document, path)
            except Exception as error:
                recovery["fresh_attempt"]["error"] = {
                    "type": type(error).__name__,
                    "code": str(getattr(error, "code", "") or ""),
                    "message": str(
                        getattr(error, "message", "") or str(error)
                    )[:500],
                    "detail": copy.deepcopy(
                        getattr(error, "detail", None)
                    ),
                }
                raise
            fresh_session = self._capture_session_evidence(path)
            recovery["fresh_session"] = fresh_session
            if fresh_session["identity"]["pid"] != fresh_pid:
                raise ValueError(
                    "the fresh capture session changed its process identity"
                )
            if (
                fresh_graph["session_files"]
                and fresh_session["temp_root"] is None
            ):
                raise ValueError(
                    "the fresh capture session has no dependency artifact root"
                )
            fresh_top = self._capture_graph_top_row(fresh_graph)
            fresh_state = fresh_top["initial_state"]
            recovery["fresh_attempt"] = (
                self._capture_rebuild_attempt_evidence(
                    fresh_graph,
                    fresh_session,
                )
            )
            source_unchanged = fresh_graph["source_files"] == graph["source_files"]
            topology_unchanged = (
                self._capture_stable_topology(fresh_graph["topology"])
                == self._capture_stable_topology(graph["topology"])
            )
            session_files_unchanged = (
                self._capture_session_file_signatures(
                    fresh_graph["session_files"]
                )
                == self._capture_session_file_signatures(
                    graph["session_files"]
                )
            )
            loaded_documents_unchanged = (
                self._capture_loaded_document_signature(fresh_graph)
                == self._capture_loaded_document_signature(graph)
            )
            configuration_unchanged = (
                fresh_state.get("configuration")
                == top["initial_state"].get("configuration")
            )
            invariants = {
                "read_only": fresh_state.get("read_only") is True,
                "source_unchanged": source_unchanged,
                "topology_unchanged": topology_unchanged,
                "session_files_unchanged": session_files_unchanged,
                "loaded_documents_unchanged": loaded_documents_unchanged,
                "configuration_unchanged": configuration_unchanged,
                "component_count_unchanged": (
                    fresh_graph["component_count"] == graph["component_count"]
                ),
            }
            violations = sorted(
                name for name, passed in invariants.items() if not passed
            )
            if violations:
                raise CadError(
                    "cad_rebuild_recovery_invariant",
                    "The fresh rebuild session changed a required invariant",
                    {
                        "path": path,
                        "violations": violations,
                        "first_session": old_session["identity"],
                        "fresh_session": fresh_session["identity"],
                    },
                )

            self._capture_bind_graph(fresh_graph, path)
            self._capture_rebuild_journal = [
                {
                    "kind": "recovery_initial",
                    "initial_pending": list(
                        fresh_graph["initial_pending_children"]
                    ),
                    "rebuild_candidates": list(
                        fresh_graph["rebuild_candidates"]
                    ),
                }
            ]
            (
                fresh_document,
                fresh_state,
                fresh_child_rebuilds,
            ) = self._capture_rebuild_pending_children(
                fresh_graph,
                document=fresh_document,
            )

            fresh_top_row = {
                **fresh_top,
                "state_before": copy.deepcopy(fresh_state),
            }
            fresh_top_rebuild = None
            if fresh_state.get("needs_rebuild2_value") in (1, 2):
                fresh_document, _, fresh_top_rebuild = (
                    self._rebuild_capture_document(
                        fresh_top_row,
                        phase="top_recovery",
                        document=fresh_document,
                    )
                )
            elif (
                fresh_state.get("needs_rebuild2_value") == 0
                and fresh_state.get("configuration_needs_rebuild") is False
            ):
                fresh_document = self._document_by_path(path)
            else:
                raise ValueError(
                    "the fresh capture session has an invalid top rebuild state"
                )
        except CadError as error:
            error_detail = (
                copy.deepcopy(error.detail)
                if isinstance(error.detail, dict)
                else {}
            )
            if isinstance(error.detail, dict):
                error.detail = {
                    **error_detail,
                    "session_recovery": recovery,
                }
            raise
        except Exception as error:  # noqa: BLE001 - recovery must stay fail-closed
            raise CadError(
                "cad_rebuild_recovery_failed",
                "The bounded fresh-session rebuild recovery failed",
                {
                    "path": path,
                    "error": f"{type(error).__name__}: {error}"[:500],
                    "session_recovery": recovery,
                },
            ) from error

        return (
            fresh_document,
            fresh_graph,
            recovery,
            fresh_top_rebuild,
            fresh_child_rebuilds,
            fresh_state,
        )

    def _rebuild_capture_copy(self, doc, path):
        self._capture_rebuild_journal = []
        graph = self._capture_rebuild_graph(doc, path)
        self._capture_rebuild_journal.append(
            {
                "kind": "initial",
                "initial_pending": list(graph["initial_pending_children"]),
                "rebuild_candidates": list(graph["rebuild_candidates"]),
            }
        )
        self._capture_bind_graph(graph, path)
        assemblies = graph["assemblies"]
        top = self._capture_graph_top_row(graph)
        document = top["initial_state"]["path"]
        configuration = top["initial_state"]["configuration"]
        state_before = top["initial_state"]
        if state_before.get("read_only") is not True:
            raise CadError(
                "cad_capture_not_read_only",
                "Collected capture copy must be explicitly read-only",
                {"path": path, "state": state_before},
            )
        source_sha256_before = graph["source_files"][graph["normalized_top"]]["sha256"]

        doc, state_after_children, child_rebuilds = (
            self._capture_rebuild_pending_children(
                graph,
                document=doc,
            )
        )
        top_row = {**top, "state_before": state_after_children}
        top_rebuild = None
        recovery = None
        if top_row["state_before"]["needs_rebuild2_value"] in (1, 2):
            try:
                doc, _top_final_state, top_rebuild = (
                    self._rebuild_capture_document(
                        top_row,
                        phase="top",
                        document=doc,
                    )
                )
            except CadError as error:
                (
                    doc,
                    graph,
                    recovery,
                    top_rebuild,
                    child_rebuilds,
                    state_after_children,
                ) = self._capture_recover_top_rebuild(
                    graph=graph,
                    path=path,
                    child_rebuilds=child_rebuilds,
                    failure=error,
                )
                assemblies = graph["assemblies"]
                top = self._capture_graph_top_row(graph)
                document = top["initial_state"]["path"]
                configuration = top["initial_state"]["configuration"]
                state_before = copy.deepcopy(top["initial_state"])
                source_sha256_before = graph["source_files"][
                    graph["normalized_top"]
                ]["sha256"]
        else:
            doc, _top_final_state = self._capture_rebuild_state(
                path,
                document=doc,
                phase="after_children",
            )

        final_graph = self._capture_rebuild_graph(doc, path)
        self._require_loaded_document_set(
            final_graph["loaded_documents"],
            self._capture_expected_documents,
        )
        self._capture_assert_documents_ready(
            final_graph["loaded_documents"],
            phase="after_rebuild",
        )
        try:
            expected_loaded_documents = (
                self._capture_loaded_document_signature(graph)
            )
            final_loaded_documents = self._capture_loaded_document_signature(
                final_graph
            )
        except ValueError as error:
            raise CadError(
                "cad_rebuild_topology_changed",
                "Capture rebuild changed a loaded document identity",
                {"path": path, "error": str(error)[:300]},
            ) from error
        if final_loaded_documents != expected_loaded_documents:
            raise CadError(
                "cad_rebuild_topology_changed",
                "Capture rebuild changed the loaded document set",
                {
                    "path": path,
                    "before": expected_loaded_documents,
                    "after": final_loaded_documents,
                },
            )
        final_topology = final_graph["topology"]
        expected_topology = graph["topology"]
        if recovery is not None:
            final_topology = self._capture_stable_topology(final_topology)
            expected_topology = self._capture_stable_topology(
                expected_topology
            )
        if final_topology != expected_topology:
            raise CadError(
                "cad_rebuild_topology_changed",
                "Capture rebuild changed the active dependency topology",
                {
                    "path": path,
                    "before": graph["topology"],
                    "after": final_graph["topology"],
                },
            )
        if final_graph["source_files"] != graph["source_files"]:
            raise CadError(
                "cad_capture_source_changed",
                "Capture preparation changed collected source bytes",
                {
                    "path": path,
                    "before": graph["source_files"],
                    "after": final_graph["source_files"],
                },
            )
        final_session_files = final_graph["session_files"]
        expected_session_files = graph["session_files"]
        if recovery is not None:
            final_session_files = self._capture_session_file_signatures(
                final_session_files
            )
            expected_session_files = self._capture_session_file_signatures(
                expected_session_files
            )
        if final_session_files != expected_session_files:
            raise CadError(
                "cad_rebuild_topology_changed",
                "Capture rebuild changed the session dependency set",
                {
                    "path": path,
                    "before": graph["session_files"],
                    "after": final_graph["session_files"],
                },
            )

        final_assemblies = final_graph["assemblies"]
        final_by_key = {
            normalize_document_path(os.path.abspath(row["path"])): row
            for row in final_assemblies
        }
        final_states = []
        for row in assemblies:
            key = normalize_document_path(os.path.abspath(row["path"]))
            final_row = final_by_key.get(key)
            if final_row is None:
                raise CadError(
                    "cad_rebuild_topology_changed",
                    "Capture rebuild removed an assembly document",
                    {"path": row["path"], "normalized_path": key},
                )
            state = final_row["initial_state"]
            expected_configuration = row["initial_state"]["configuration"]
            if state.get("configuration") != expected_configuration:
                raise CadError(
                    "cad_configuration_mismatch",
                    "Assembly rebuild changed a dependency configuration",
                    {
                        "path": row["path"],
                        "expected": expected_configuration,
                        "actual": state.get("configuration"),
                    },
                )
            required = self._capture_required_configurations.get(key, [])
            self._capture_configuration_state_ready(
                state,
                required,
                label="Capture preparation assembly",
            )
            if state.get("needs_rebuild2_value") != 0 or state.get("configuration_needs_rebuild") is not False:
                raise CadError(
                    "cad_rebuild_incomplete",
                    "Capture preparation left an assembly dependency pending",
                    {"path": row["path"], "depth": row["depth"], "state": state},
                )
            final_states.append(
                {
                    "depth": final_row["depth"],
                    "path": final_row["path"],
                    "instances": list(final_row["instances"]),
                    "state": state,
                }
            )
        final_part_states = []
        for part in final_graph["part_documents"]:
            state = part["initial_state"]
            key = normalize_document_path(os.path.abspath(part["path"]))
            required = self._capture_required_configurations.get(key, [])
            self._capture_configuration_state_ready(
                state,
                required,
                label="Capture preparation part",
            )
            if state.get("needs_rebuild2_value") != 0 or state.get("configuration_needs_rebuild") is not False:
                raise CadError(
                    "cad_rebuild_incomplete",
                    "Capture preparation left a part dependency pending",
                    {"path": part["path"], "state": state},
                )
            final_part_states.append(
                {
                    "path": part["path"],
                    "instances": list(part["instances"]),
                    "state": state,
                }
            )

        top_state = final_by_key[graph["normalized_top"]]["initial_state"]
        if top_state.get("read_only") is not True:
            raise CadError(
                "cad_rebuild_incomplete",
                "Capture preparation did not end in a read-only top assembly",
                {"path": path, "state": top_state},
            )
        source_files_after = final_graph["source_files"]
        source_sha256_after = source_files_after[graph["normalized_top"]]["sha256"]

        rebuilt = bool(
            recovery is not None
            or child_rebuilds
            or top_rebuild is not None
        )
        return doc, {
            "schema_version": "solidworks-to-urdf.capture-preparation/v3",
            "used_api": (
                "IModelDocExtension.NeedsRebuild2/"
                "IConfiguration.NeedsRebuild/"
                "IModelDocExtension.Rebuild(swForceRebuildAll=2)"
                if rebuilt
                else "IModelDocExtension.NeedsRebuild2/IConfiguration.NeedsRebuild"
            ),
            "scope": "collected_copy_in_memory",
            "document": document,
            "configuration": configuration,
            "read_only": True,
            "saved_to_disk": False,
            "rebuild_performed": rebuilt,
            "component_count": graph["component_count"],
            "assembly_document_count": len(final_assemblies),
            "child_rebuilds": child_rebuilds,
            "top_rebuild": top_rebuild,
            "rebuild_calls": copy.deepcopy(self._capture_rebuild_journal),
            "session": self._capture_session_evidence(path),
            "needs_rebuild2_before": state_before.get("needs_rebuild2_value"),
            "needs_rebuild2_after": top_state.get("needs_rebuild2_value"),
            "configuration_needs_rebuild_before": state_before.get("configuration_needs_rebuild"),
            "configuration_needs_rebuild_after": top_state.get("configuration_needs_rebuild"),
            "source_sha256_before": source_sha256_before,
            "source_sha256_after": source_sha256_after,
            "save_flag_before": state_before.get("save_flag"),
            "save_flag_after": top_state.get("save_flag"),
            "state_before": state_before,
            "state_after_children": state_after_children,
            "state_after": top_state,
            "assembly_states_before": [
                {
                    "depth": row["depth"],
                    "path": row["path"],
                    "instances": list(row["instances"]),
                    "state": row["initial_state"],
                }
                for row in sorted(assemblies, key=lambda item: int(item["depth"]))
            ],
            "assembly_states_after": final_states,
            "part_states_after": final_part_states,
            "source_files_before": graph["source_files"],
            "source_files_after": source_files_after,
            "session_dependency_paths": graph["session_files"],
            "topology_before": graph["topology"],
            "topology_after": final_graph["topology"],
            "loaded_documents_before": self._capture_loaded_document_signature(
                graph
            ),
            "loaded_documents_after": self._capture_loaded_document_signature(
                final_graph
            ),
            "session_recovery": copy.deepcopy(recovery),
        }

    def _leaf_mass_failure(self, name, occurrence, referenced, error, extra=None):
        """Name the occurrence behind a leaf mass-read failure.

        A failed leaf mass read must never be anonymous: the phase-one
        traversal already recorded the occurrence identity and its native
        solid/sheet counts, and both travel beside the original code, message
        and detail.  Nothing is retried, skipped or re-probed -- the capture
        still fails exactly as before, only with its context retained.
        """

        detail: dict = {}
        if isinstance(error.detail, dict):
            detail.update(error.detail)
        elif error.detail is not None:
            detail["detail"] = error.detail
        detail.update(
            {
                "component": name,
                "document": occurrence.path,
                "referenced_configuration": referenced,
                "bodies": dict(self.notes.get("bodies:" + name) or {}),
                "phase": "leaf_mass_read",
            }
        )
        if extra:
            detail.update(extra)
        return CadError(error.code, error.message, detail)

    def collect_scene(self, doc_path, coordinate_systems, on_activity=None, require_material=True):
        activity = _ActivityEmitter(on_activity)
        source_root = Path(doc_path).resolve().parent

        def note(action: str, **fields) -> None:
            activity.emit(action, phase="capture", **fields)

        note("prepare_capture", current_object=_activity_object(doc_path), force=True)
        doc = self._document_by_path(doc_path)
        if _member(doc, "GetType") != 2:
            raise CadError("cad_not_assembly", "export requires a saved SLDASM")
        try:
            doc, preparation = self._rebuild_capture_copy(doc, doc_path)
        except CadError as error:
            detail = copy.deepcopy(error.detail) if isinstance(error.detail, dict) else {}
            detail.setdefault("rebuild_journal", copy.deepcopy(self._capture_rebuild_journal))
            error.detail = detail
            raise
        try:
            native_api_pid = int(_method(self._app_obj(), "GetProcessID"))
        except Exception:  # noqa: BLE001 - unresolved PID keeps source scope incomplete
            native_api_pid = None
        self._record_save_flag(doc, doc_path)
        self._components = set()
        self._source_components = {}
        self._source_provenance = {}
        self._source_scope_overrides = {}
        self._source_documents = {}
        self._scene_source_root = source_root
        self._session_document_paths = {}
        self._session_artifact_roots = {}
        self._session_artifact_root_owners = {}
        self.notes = {"capture_preparation": preparation}
        self.source_files = {doc_path: _hash(doc_path)}
        _manager, config = _active_configuration_view(doc)
        self._scene_document_key = self._record_source_document(doc_path, str(_member(config, "Name")))
        root = _component(_member(config, "GetRootComponent3", True))
        stack = [
            (component, doc_path)
            for component in list(_method(root, "GetChildren") or ())
        ]
        borrowed_components = []
        occurrences = []
        components, properties = [], {}
        requested_datums = set(coordinate_systems)
        transforms, datum_owners = {}, {}

        def record_datums(document, owner, placement):
            for datum in _coordinate_system_features(document):
                if datum not in requested_datums:
                    continue
                if datum in transforms:
                    raise CadError(
                        "cad_coordinate_system_ambiguous",
                        "the requested coordinate system belongs to several native occurrences",
                        {"datum": datum, "owners": [datum_owners[datum], owner]},
                    )
                local = self._coordinate_system_transform(document, datum)
                if placement is None:
                    matrix = local
                else:
                    composed = self._multiply_frames(
                        [placement[0:4], placement[4:8], placement[8:12], placement[12:16]],
                        [local[0:4], local[4:8], local[8:12], local[12:16]],
                    )
                    matrix = tuple(value for row in composed for value in row)
                transforms[datum] = matrix
                datum_owners[datum] = owner
                self.notes["coordinate_system_owner:" + datum] = {
                    "component": owner,
                    "configuration": _active_configuration(document),
                    "document": _member(document, "GetPathName"),
                }

        record_datums(doc, "", None)
        while stack:
            raw_component, owner_path = stack.pop()
            comp = _component(raw_component)
            if _measured_component_state(comp) == COMPONENT_SUPPRESSED:
                continue
            borrowed_components.append(comp)
            name = str(_member(comp, "Name2"))
            note("walk", current_object=_activity_object(name))
            part = _component_document(comp)
            if part is None:
                raise CadError("cad_component_unresolved", name)
            # SetReadOnlyState can change native state. Defer it until the
            # occurrence tree has been captured as primitives and released;
            # phase-two document acquisition enforces read-only before reads.
            path = _method(comp, "GetPathName")
            if not path or not os.path.isfile(path):
                # Without a file on disk there is no revision to hash or copy, so the
                # snapshot could not name what it read.
                raise CadError("cad_component_not_on_disk", name, {"component": name, "path": path})
            source_document = _component_source_document(comp, source_root, native_path=path)
            provenance = source_document.get("source_provenance")
            if provenance is not None:
                owner_relative = _relative_document(
                    owner_path,
                    source_root,
                )
                provenance["owner"] = (
                    owner_relative
                    or normalize_document_path(str(owner_path))
                )
                provenance["owner_sha256"] = (
                    _hash(str(owner_path))
                    if os.path.isfile(owner_path)
                    else None
                )
                provenance["occurrence"] = name
                provenance["native_api_pid"] = native_api_pid
                self._source_provenance[name] = provenance
            session_document = (
                isinstance(provenance, dict)
                and provenance.get("state")
                in {"cached_materialization", "embedded_virtual_component"}
            )
            if provenance is not None and not session_document:
                raise CadError(
                    "cad_component_source_unread",
                    "a component did not resolve to a delivered file or an admitted session document",
                    {"component": name, "path": path, "source_provenance": provenance},
                )
            self._record_save_flag(part, path)
            referenced = _member(comp, "ReferencedConfiguration")
            # Configuration changes can invalidate borrowed occurrence interfaces.
            # Finish the assembly traversal using primitives before any selection.
            children = list(_method(comp, "GetChildren") or ())
            placement = self._placement(comp)
            if children:
                if _member(part, "GetType") != 2:
                    raise CadError("cad_component_type", name)
                document_type, fixed = "assembly", False
                stack.extend(
                    (child, path) for child in children
                )
            else:
                if _member(part, "GetType") != 1:
                    raise CadError("cad_empty_subassembly", name)
                document_type, fixed = "part", bool(_member(comp, "IsFixed"))
                self.notes["bodies:" + name] = {
                    "solid": self._body_count(comp, 0, name),
                    "sheet": self._body_count(comp, 1, name),
                }
            document_key = self._record_source_document(
                path,
                _active_configuration(part),
                session_document=session_document,
            )
            self._source_components[name] = (document_key, str(referenced))
            if not session_document and path not in self.source_files:
                self.source_files[path] = _hash(path)
            occurrences.append((RawComponent(name, path, placement, fixed, document_type), referenced))

        borrowed_components.clear()
        raw_component = owner_path = comp = children = part = root = stack = None
        note("walk", completed=len(occurrences), total=len(occurrences), force=True)
        for index, (occurrence, referenced) in enumerate(occurrences, start=1):
            name, path = occurrence.name, occurrence.path
            note(
                "instance_reads",
                current_object=_activity_object(name),
                completed=index - 1,
                total=len(occurrences),
            )
            with _temporary_configuration(
                lambda document_path=path: self._document_by_path(document_path), referenced, name
            ) as (part, _previous):
                record_datums(part, name, occurrence.transform)
                # Occurrence references are independent of the one active state
                # of a shared document; every temporary selection is restored.
                if occurrence.document_type == "assembly":
                    continue
                components.append(occurrence)
                self._components.add(name)
                try:
                    properties[name] = self._mass_properties_document(part, require_material)
                except CadError as error:
                    counts = dict(self.notes.get("bodies:" + name) or {})
                    fallback_row = None
                    if (
                        error.code == "cad_empty_model"
                        and int(counts.get("solid") or 0) == 0
                        and int(counts.get("sheet") or 0) > 0
                    ):
                        try:
                            fallback_row = self._sheet_only_instance_fallback(
                                occurrence, doc_path, name, referenced, error, counts
                            )
                        except Exception as fallback_error:  # noqa: BLE001
                            raise self._leaf_mass_failure(
                                name,
                                occurrence,
                                referenced,
                                error,
                                extra={
                                    "fallback_error": str(
                                        getattr(fallback_error, "code", "") or type(fallback_error).__name__
                                    ),
                                    "fallback_message": " ".join(str(fallback_error).split())[:200],
                                },
                            ) from fallback_error
                    if fallback_row is None:
                        raise self._leaf_mass_failure(name, occurrence, referenced, error) from error
                    properties[name] = fallback_row
                    self.notes["effective_instance:" + name] = "cad_authored_effective_instance"
                properties[name]["reference"]["configuration"] = referenced
                self.notes["mass_property:" + name] = properties[name]["reference"]["used_api"]
                note(
                    "instance_reads",
                    current_object=_activity_object(name),
                    completed=index,
                    total=len(occurrences),
                )
        if not components:
            raise CadError("cad_empty_model", "assembly has no resolved solid parts")
        missing_datums = sorted(requested_datums - transforms.keys())
        if missing_datums:
            raise CadError(
                "cad_missing_coordinate_system", "requested native coordinate systems were not found", missing_datums
            )
        note("instance_reads", completed=len(occurrences), total=len(occurrences), force=True)
        self._part_mass_properties = dict(properties)
        return RawScene(doc_path, components, transforms, properties, dict(self.notes))

    def _coordinate_system_transform(self, doc, name):
        tf = _member(_member(doc, "Extension"), "GetCoordinateSystemTransformByName", name)
        if tf is None:
            raise CadError("cad_missing_coordinate_system", name)
        self.notes["coordinate_system:" + name] = "IModelDocExtension.GetCoordinateSystemTransformByName"
        return transform_from_solidworks(_member(tf, "ArrayData"))

    def _body_list(self, holder, body_type, component):
        """Read bodies from the captured IComponent2 occurrence."""
        try:
            bodies = _as_list(_member(holder, "GetBodies2", body_type))
        except Exception as error:
            raise CadError(
                "cad_component_bodies_unreadable",
                "the component occurrence's bodies could not be read",
                {"component": component, "body_type": body_type, "error": str(error)},
            ) from error
        self.notes[f"bodies_api:{component}:{body_type}"] = "IComponent2.GetBodies2(type)"
        return bodies

    def _body_count(self, holder, body_type, component):
        return len(self._body_list(holder, body_type, component))

    def _capture_body_tessellation(
        self,
        body,
        extension,
        *,
        component,
        body_type,
        body_index,
        reference_registry=None,
        read_end_params=None,
        scope_context=None,
        source_scope=None,
        scope_witnesses=None,
        body_name=None,
        body_reference=None,
        body_faces=None,
        body_edges=None,
        body_owner_prebound=False,
        body_owner_pointer=None,
    ):
        """Capture one body's finite topology and orientation evidence.

        The body tessellation is requested and evaluated exactly once. Native
        edge sets partition finite-list fins: a fin absent from every edge set
        is an internal tessellation fin and keeps a null edge reference. No
        ``GetFinEdge`` or other per-internal-fin identity call is made. Native
        edge U domains, fin endpoint U values, cofins and edge-face senses are
        captured without defaults so final topology cannot pass on partial data.
        """

        base = {
            "component": component,
            "body_type": body_type,
            "body_index": body_index,
        }
        owner_scope = dict(scope_context or {})
        body_scope = dict(source_scope or {})
        witness_cache = None

        def get_witness_rows():
            nonlocal witness_cache
            if witness_cache is None:
                witness_cache = (
                    scope_witnesses()
                    if callable(scope_witnesses)
                    else scope_witnesses
                )
            return witness_cache or {}
        deep_retry_apis = {
            "ITessellation.GetFaceFacets",
            "ITessellation.GetFacetFins",
            "ITessellation.GetFinVertices",
            "ITessellation.GetVertexPoint",
        }

        def call(api, operation, **where):
            try:
                if api in deep_retry_apis:
                    return _retry_transient_com(
                        operation,
                        attempts=FACE_READ_RETRY_ATTEMPTS,
                        delays=FACE_READ_RETRY_DELAYS,
                    )
                return operation()
            except Exception as error:  # noqa: BLE001 - native failures need stable context
                raise CadError(
                    "cad_native_tessellation_unreadable",
                    "the body tessellation graph could not be captured",
                    {**base, **where, "api": api, "error": str(error)[:300]},
                ) from error

        registry = reference_registry or _NativeReferenceRegistry()

        def persistent(
            value,
            kind,
            *,
            audit=True,
            **where,
        ):
            try:
                raw = bytes(
                    int(item) & 0xFF
                    for item in _member(extension, "GetPersistReference3", value)
                )
            except Exception as error:  # noqa: BLE001
                raise CadError(
                    "cad_persistent_reference_unreadable",
                    "a native topology object has no persistent reference",
                    {**base, **where, "kind": kind, "error": str(error)[:300]},
                ) from error
            if not raw:
                raise CadError(
                    "cad_persistent_reference_unreadable",
                    "a native topology object has an empty persistent reference",
                    {**base, **where, "kind": kind},
                )
            raw_sha256 = hashlib.sha256(raw).hexdigest()
            typed_sha256 = _typed_reference_sha256(kind, raw)
            owner = {"kind": kind, **base, **where}
            if audit:
                registry.claim(
                    kind=kind,
                    raw_sha256=raw_sha256,
                    typed_sha256=typed_sha256,
                    owner=owner,
                    base=base,
                )
            return {
                "raw_sha256": raw_sha256,
                "typed_sha256": typed_sha256,
                "raw": raw,
            }

        body = _dynamic(body)
        if body_name is None:
            body_name = call("IBody2.Name", lambda: _member(body, "Name"))
        if not isinstance(body_name, str) or not body_name.strip():
            raise CadError(
                "cad_native_tessellation_unreadable",
                "the captured body has no stable name",
                {**base, "api": "IBody2.Name"},
            )
        if body_reference is None:
            body_reference = persistent(body, "body")
        else:
            registry.claim(
                kind="body",
                raw_sha256=body_reference["raw_sha256"],
                typed_sha256=body_reference["typed_sha256"],
                owner={"kind": "body", **base},
                base=base,
            )
        if body_faces is None:
            faces = call(
                "IBody2.GetFaces",
                lambda: _as_list(_member(body, "GetFaces")),
            )
        else:
            faces = list(body_faces)
        if body_edges is None:
            edges = call(
                "IBody2.GetEdges",
                lambda: _as_list(_member(body, "GetEdges")),
            )
        else:
            edges = list(body_edges)
        face_rows = []
        for face_index, raw_face in enumerate(faces):
            face = _interface(_dynamic(raw_face), IFACE2_IID)
            reference = persistent(
                face,
                "face",
                audit=False,
                face_index=face_index,
            )
            face_rows.append(
                {
                    "index": face_index,
                    "object": face,
                    "reference": reference,
                    "object_pointer": _com_object_pointer(face),
                }
            )
        edge_rows = []
        for edge_index, raw_edge in enumerate(edges):
            edge = _interface(_dynamic(raw_edge), IEDGE_IID)
            reference = persistent(
                edge,
                "edge",
                audit=False,
                edge_index=edge_index,
            )
            edge_rows.append(
                {
                    "index": edge_index,
                    "object": edge,
                    "reference": reference,
                }
            )

        for kind, interface_name, rows in (
            ("face", "IFace2", face_rows),
            ("edge", "IEdge", edge_rows),
        ):
            for row in rows:
                owner = call(
                    f"{interface_name}.GetBody",
                    lambda value=row["object"]: _interface(
                        _dynamic(_method(value, "GetBody")),
                        IBODY2_IID,
                    ),
                    **{f"{kind}_index": row["index"]},
                )
                if owner is None:
                    raise CadError(
                        "cad_native_tessellation_unreadable",
                        f"{kind} has no owning IBody2",
                        {**base, f"{kind}_index": row["index"]},
                    )
                owner_pointer = _com_iunknown_pointer(owner)
                owner_matches_name = (
                    call(
                        f"{interface_name}.GetBody.Name",
                        lambda value=owner: _member(value, "Name"),
                        **{f"{kind}_index": row["index"]},
                    )
                    == body_name
                )
                if body_owner_prebound:
                    owner_reference = body_reference
                    owner_matches = (
                        owner_matches_name
                        and owner_pointer == body_owner_pointer
                    )
                else:
                    owner_reference = persistent(
                        owner,
                        "body",
                        audit=False,
                        owner_of=kind,
                        **{f"{kind}_index": row["index"]},
                    )
                    owner_matches = (
                        owner_matches_name
                        and
                        owner_reference["raw_sha256"]
                        == body_reference["raw_sha256"]
                    )
                if not owner_matches:
                    raise CadError(
                        "cad_native_tessellation_unreadable",
                        f"{kind} belongs to a body other than the captured body",
                        {
                            **base,
                            f"{kind}_index": row["index"],
                            "body_reference_sha256": body_reference[
                                "typed_sha256"
                            ],
                            "owner_reference_sha256": owner_reference[
                                "typed_sha256"
                            ],
                            "body_owner_pointer": body_owner_pointer,
                            "owner_pointer": owner_pointer,
                        },
                    )
                row["owner_body_reference"] = owner_reference

        for edge_row in edge_rows:
            endpoints = {}
            for name, point_api in (
                ("start", "GetStartVertex"),
                ("end", "GetEndVertex"),
            ):
                vertex = None
                vertex_error = None
                try:
                    vertex = _interface(
                        _dynamic(_method(edge_row["object"], point_api)),
                        IVERTEX_IID,
                    )
                except Exception as error:  # noqa: BLE001
                    vertex_error = (
                        f"{type(error).__name__}: {error}"[:300]
                    )
                if vertex is None:
                    endpoints[name] = {
                        "state": (
                            "read_failed"
                            if vertex_error is not None
                            else "absent"
                        ),
                        "reference": None,
                        "point_m": None,
                        "vertex_error": vertex_error
                        or "endpoint interface unavailable",
                        "point_error": "vertex_absent",
                        "reference_error": None,
                        "object_pointer": None,
                    }
                    continue
                point = None
                point_error = None
                try:
                    point = _binary64_xyz(
                        _as_list(_method(vertex, "GetPoint"))
                    )
                except Exception as error:  # noqa: BLE001
                    point_error = (
                        f"{type(error).__name__}: {error}"[:300]
                    )
                reference = None
                reference_error = None
                try:
                    reference = persistent(
                        vertex,
                        "vertex",
                    audit=False,
                        edge_index=edge_row["index"],
                        endpoint=name,
                    )
                except Exception as error:  # noqa: BLE001
                    reference_error = (
                        f"{type(error).__name__}: {error}"[:300]
                    )
                endpoints[name] = {
                    "state": (
                        "present"
                        if point is not None and reference is not None
                        else "read_failed"
                    ),
                    "reference": reference,
                    "point_m": point,
                    "vertex_error": vertex_error,
                    "point_error": point_error,
                    "reference_error": reference_error,
                    "object_pointer": (
                        _com_object_pointer(vertex)
                        if vertex is not None
                        else None
                    ),
                }
            edge_row["endpoints"] = endpoints
            adjacent = call(
                "IEdge.GetTwoAdjacentFaces2",
                lambda edge=edge_row["object"]: _as_list(
                    _method(edge, "GetTwoAdjacentFaces2")
                ),
                edge_index=edge_row["index"],
            )
            adjacent_faces = []
            for face in adjacent:
                if face is None:
                    continue
                adjacent_face = _interface(_dynamic(face), IFACE2_IID)
                adjacent_pointer = _com_object_pointer(adjacent_face)
                matches = [
                    row["index"]
                    for row in face_rows
                    if row["object_pointer"] == adjacent_pointer
                ]
                if len(matches) != 1:
                    raise CadError(
                        "cad_native_tessellation_unreadable",
                        "a native edge names a face outside the captured body",
                        {
                            **base,
                            "edge_index": edge_row["index"],
                            "face_matches": tuple(matches),
                        },
                    )
                adjacent_faces.append(matches[0])
            if not adjacent_faces or len(adjacent_faces) != len(set(adjacent_faces)):
                raise CadError(
                    "cad_native_tessellation_unreadable",
                    "a native edge has no unique face incidence",
                    {**base, "edge_index": edge_row["index"]},
                )
            edge_row["face_indices"] = tuple(sorted(adjacent_faces))
            curve = call(
                "IEdge.GetCurve",
                lambda edge=edge_row["object"]: _interface(
                    _dynamic(_method(edge, "GetCurve")),
                    ICURVE_IID,
                ),
                edge_index=edge_row["index"],
            )
            if curve is None:
                raise CadError(
                    "cad_native_tessellation_unreadable",
                    "a native edge has no underlying curve",
                    {**base, "edge_index": edge_row["index"]},
                )
            curve_params = call(
                "IEdge.GetCurveParams3",
                lambda edge=edge_row["object"]: _interface(
                    _dynamic(_method(edge, "GetCurveParams3")),
                    ICURVEPARAMDATA_IID,
                ),
                edge_index=edge_row["index"],
            )
            if curve_params is None:
                raise CadError(
                    "cad_native_tessellation_unreadable",
                    "a native edge has no curve parameter data",
                    {**base, "edge_index": edge_row["index"]},
                )
            umin = call(
                "ICurveParamData.UMinValue",
                lambda value=curve_params: _member(value, "UMinValue"),
                edge_index=edge_row["index"],
            )
            umax = call(
                "ICurveParamData.UMaxValue",
                lambda value=curve_params: _member(value, "UMaxValue"),
                edge_index=edge_row["index"],
            )
            if (
                type(umin) not in (int, float)
                or type(umax) not in (int, float)
                or not math.isfinite(umin)
                or not math.isfinite(umax)
                or umin >= umax
            ):
                raise CadError(
                    "cad_native_tessellation_unreadable",
                    "a native edge has an invalid U domain",
                    {
                        **base,
                        "edge_index": edge_row["index"],
                        "u_min": umin,
                        "u_max": umax,
                    },
                )
            curve_start = call(
                "ICurveParamData.StartPoint",
                lambda value=curve_params: _binary64_xyz(
                    _as_list(_member(value, "StartPoint"))
                ),
                edge_index=edge_row["index"],
            )
            curve_end = call(
                "ICurveParamData.EndPoint",
                lambda value=curve_params: _binary64_xyz(
                    _as_list(_member(value, "EndPoint"))
                ),
                edge_index=edge_row["index"],
            )
            curve_sense = call(
                "ICurveParamData.Sense",
                lambda value=curve_params: _member(value, "Sense"),
                edge_index=edge_row["index"],
            )
            curve_type = call(
                "ICurveParamData.CurveType",
                lambda value=curve_params: _member(value, "CurveType"),
                edge_index=edge_row["index"],
            )
            if type(curve_sense) is not bool or type(curve_type) is not int:
                raise CadError(
                    "cad_native_tessellation_unreadable",
                    "a native edge curve has no strict boolean sense or integer type",
                    {
                        **base,
                        "edge_index": edge_row["index"],
                        "sense": repr(curve_sense)[:60],
                        "curve_type": repr(curve_type)[:60],
                    },
                )
            parameter_endpoints = native_curve_parameter_endpoints(
                curve_start,
                curve_end,
            )
            edge_row["curve"] = {
                "u_min": float(umin),
                "u_max": float(umax),
                "start_point_m": list(curve_start),
                "end_point_m": list(curve_end),
                "sense": curve_sense,
                "curve_type": curve_type,
            }
            end_params = (
                read_end_params(curve)
                if read_end_params is not None
                else _read_curve_end_params(curve)
            )
            if (
                not isinstance(end_params, dict)
                or set(end_params) != _CURVE_END_PARAMS_FIELDS
            ):
                raise CadError(
                    "cad_native_tessellation_unreadable",
                    "a native edge curve-end record is incomplete or unknown",
                    {
                        **base,
                        "edge_index": edge_row["index"],
                        "curve_end_params": end_params,
                    },
                )
            edge_row["curve_end_params"] = end_params
            try:
                curve_domain_transform = native_curve_domain_transform(
                    end_params["start"],
                    end_params["end"],
                    umin,
                    umax,
                    curve_sense,
                    end_params["is_periodic"],
                )
            except ValueError as error:
                curve_domain_transform = {
                    "rule_version": NATIVE_CURVE_DOMAIN_RULE,
                    "underlying_start": end_params["start"],
                    "underlying_end": end_params["end"],
                    "trimmed_start": float(umin),
                    "trimmed_end": float(umax),
                    "sense": curve_sense,
                    "direction": 1 if curve_sense else -1,
                    "is_periodic": end_params["is_periodic"],
                    "positive_start": (
                        float(umin) if curve_sense else -float(umax)
                    ),
                    "positive_end": (
                        float(umax) if curve_sense else -float(umin)
                    ),
                    "period": None,
                    "shift_periods": 0,
                    "lifted_start": None,
                    "lifted_end": None,
                    "seam_wrapped": False,
                    "segments": [],
                    "status": "unresolved",
                    "error": str(error),
                }
            edge_row["curve_domain_transform"] = curve_domain_transform
            curve_evaluations = {}
            evaluated_points = {}
            for name, u_parameter in (
                ("start", umin),
                ("end", umax),
            ):
                evaluation = {
                    "u": float(u_parameter),
                    "point_roundtrip_rule": NATIVE_CURVE_ENDPOINT_OPERATION_RULE,
                    "payload": None,
                    "packed_status": None,
                    "error": None,
                    "point_tolerance_m": None,
                    "point_error_m": None,
                }
                try:
                    raw_evaluation = _as_list(
                        _method(edge_row["object"], "Evaluate2", u_parameter, 1)
                    )
                    evaluate_evidence = native_evaluate2_evidence(
                        raw_evaluation
                    )
                    if evaluate_evidence is None:
                        raise ValueError(
                            "Evaluate2 shape or point payload is invalid"
                        )
                    if not evaluate_evidence["packed_status"][
                        "parsed_success"
                    ]:
                        raise ValueError(
                            "Evaluate2 packed success bit is zero"
                        )
                    evaluated_point = _binary64_xyz(
                        evaluate_evidence["payload"][:3]
                    )
                    evaluated_points[name] = evaluated_point
                    evaluation.update(
                        {
                            "payload": evaluate_evidence["payload"],
                            "packed_status": evaluate_evidence[
                                "packed_status"
                            ],
                        }
                    )
                except Exception as error:  # noqa: BLE001
                    evaluation["error"] = (
                        f"{type(error).__name__}: {error}"[:300]
                    )
                curve_evaluations[name] = evaluation
            if len(evaluated_points) == 2:
                for index, (name, expected_point) in enumerate(
                    zip(
                        ("start", "end"),
                        parameter_endpoints,
                        strict=True,
                    )
                ):
                    evaluation = curve_evaluations[name]
                    opposite_point = parameter_endpoints[1 - index]
                    tolerance = native_curve_endpoint_operation_tolerance(
                        expected_point,
                        evaluated_points[name],
                        opposite_point,
                    )
                    error_m = native_point_error(
                        expected_point,
                        evaluated_points[name],
                    )
                    evaluation.update(
                        {
                            "point_tolerance_m": tolerance,
                            "point_error_m": error_m,
                        }
                    )
                    if error_m > tolerance:
                        evaluation["error"] = (
                            "ValueError: Evaluate2 endpoint differs from "
                            "curve endpoint"
                        )
            edge_row["curve_endpoint_evaluations"] = curve_evaluations
            edge_row["topology_endpoint_assignment"] = None
            endpoint_states = {
                name: endpoints[name]["state"] for name in ("start", "end")
            }
            both_absent = all(state == "absent" for state in endpoint_states.values())
            any_read_failed = any(
                state == "read_failed" for state in endpoint_states.values()
            )
            endpoint_evaluations_valid = all(
                curve_evaluations[name]["error"] is None
                for name in ("start", "end")
            )
            endpoint_points_derived_from_curve = False
            if both_absent:
                evaluated_points = [
                    curve_evaluations[name]["payload"][:3]
                    for name in ("start", "end")
                    if curve_evaluations[name]["payload"] is not None
                ]
                closure_tolerance = (
                    native_point_roundtrip_tolerance(*evaluated_points)
                    if len(evaluated_points) == 2
                    else 0.0
                )
                closure_error = (
                    max(
                        abs(evaluated_points[0][index] - evaluated_points[1][index])
                        for index in range(3)
                    )
                    if len(evaluated_points) == 2
                    else float("inf")
                )
                endpoint_points_derived_from_curve = (
                    end_params["success"] is True
                    and end_params["is_closed"] is True
                    and endpoint_evaluations_valid
                    and closure_error <= closure_tolerance
                    and curve_domain_transform["status"]
                    in {
                        "contained",
                        "contained_seam_wrapped",
                        "edge_local_nonperiodic",
                    }
                )
                if not endpoint_points_derived_from_curve:
                    raise CadError(
                        "cad_native_tessellation_unreadable",
                        "both absent endpoints do not have a closed curve proof",
                        {
                            **base,
                            "edge_index": edge_row["index"],
                            "curve_end_params": end_params,
                            "curve_endpoint_evaluations": curve_evaluations,
                            "closure_error_m": closure_error,
                            "closure_tolerance_m": closure_tolerance,
                        },
                    )
            elif any_read_failed or endpoint_states["start"] != endpoint_states["end"]:
                raise CadError(
                    "cad_native_tessellation_unreadable",
                    "edge endpoint state is inconsistent or unreadable",
                    {
                        **base,
                        "edge_index": edge_row["index"],
                        "endpoint_states": endpoint_states,
                        "endpoint_errors": {
                            name: {
                                key: endpoints[name][key]
                                for key in (
                                    "vertex_error",
                                    "point_error",
                                    "reference_error",
                                )
                            }
                            for name in ("start", "end")
                        },
                    },
                )
            if set(endpoint_states.values()) == {"present"}:
                topology_assignment = native_curve_topology_endpoint_set_assignment(
                    parameter_endpoints[0],
                    parameter_endpoints[1],
                    endpoints["start"]["point_m"],
                    endpoints["end"]["point_m"],
                )
                edge_row["topology_endpoint_assignment"] = topology_assignment
                if topology_assignment["accepted"] is not True:
                    raise CadError(
                        "cad_native_tessellation_unreadable",
                        "edge topology endpoints do not form a complete curve endpoint set",
                        {
                            **base,
                            "edge_index": edge_row["index"],
                            "curve_sense": curve_sense,
                            "topology_endpoint_assignment": topology_assignment,
                        },
                    )
            if not both_absent and (
                not endpoint_evaluations_valid
                or curve_domain_transform["status"]
                not in {
                    "contained",
                    "contained_seam_wrapped",
                    "edge_local_nonperiodic",
                }
            ):
                raise CadError(
                    "cad_native_tessellation_unreadable",
                    "edge endpoints lack complete curve-domain evidence",
                    {
                        **base,
                        "edge_index": edge_row["index"],
                        "curve_end_params": end_params,
                        "curve_endpoint_evaluations": curve_evaluations,
                        "endpoint_errors": {
                            name: {
                                key: endpoints[name][key]
                                for key in (
                                    "vertex_error",
                                    "point_error",
                                    "reference_error",
                                )
                            }
                            for name in ("start", "end")
                        },
                    },
                )
            edge_row["endpoint_points_derived_from_curve"] = (
                endpoint_points_derived_from_curve
            )
            face_senses = []
            for face_index in edge_row["face_indices"]:
                face = face_rows[face_index]["object"]
                senses = {}
                for method in ("EdgeInFaceSense", "IEdgeInFaceSense2"):
                    try:
                        value = _method(edge_row["object"], method, face)
                    except Exception as error:  # noqa: BLE001
                        senses[method] = {
                            "value": None,
                            "error": f"{type(error).__name__}: {error}"[:300],
                        }
                    else:
                        senses[method] = (
                            {"value": value, "error": None}
                            if type(value) is bool
                            else {
                                "value": repr(value)[:60],
                                "error": "not_bool",
                            }
                        )
                face_senses.append(
                    {
                        "face_index": face_index,
                        **senses,
                    }
                )
            edge_row["face_senses"] = face_senses

        tessellation_request = None

        def get_tessellation():
            nonlocal tessellation_request
            typed_faces = tuple(
                _interface(_dynamic(face), IFACE2_IID) for face in faces
            )
            face_references = tuple(
                row["reference"]["typed_sha256"] for row in face_rows
            )
            face_list, tessellation_request = _whole_body_face_list(
                typed_faces,
                face_references,
            )
            return _member(body, "GetTessellation", face_list)

        raw_tessellation = call("IBody2.GetTessellation", get_tessellation)
        if raw_tessellation is None:
            raise CadError(
                "cad_native_tessellation_unreadable",
                "IBody2.GetTessellation returned no tessellation object",
                {**base, "api": "IBody2.GetTessellation"},
            )
        tessellation = _interface(_dynamic(raw_tessellation), ITESSELLATION_IID)
        tessellation_properties = {}
        for name, expected, evidence_name in (
            ("MatchType", 0, "match_type"),
            ("NeedFaceFacetMap", True, "need_face_facet_map"),
            ("NeedEdgeFinMap", True, "need_edge_fin_map"),
            ("NeedVertexParams", True, "need_vertex_params"),
        ):
            call(
                f"ITessellation.{name}",
                lambda name=name, expected=expected: setattr(
                    tessellation, name, expected
                ),
            )
            observed = call(
                f"ITessellation.{name}",
                lambda name=name: _member(tessellation, name),
            )
            if type(observed) is not type(expected) or observed != expected:
                raise CadError(
                    "cad_native_tessellation_unreadable",
                    "a required tessellation property did not preserve its requested value",
                    {**base, "api": f"ITessellation.{name}", "returned": repr(observed)[:60]},
                )
            tessellation_properties[evidence_name] = observed
        tessellate_result = call(
            "ITessellation.Tessellate",
            lambda: _method(tessellation, "Tessellate"),
        )
        if type(tessellate_result) is not bool:
            raise CadError(
                "cad_native_tessellation_unreadable",
                "ITessellation.Tessellate did not return a boolean",
                {**base, "api": "ITessellation.Tessellate", "returned": repr(tessellate_result)[:60]},
            )
        native_totals = {}
        for api, name in (
            ("GetFacetCount", "facets"),
            ("GetFinCount", "fins"),
            ("GetVertexCount", "vertices"),
        ):
            value = call(
                f"ITessellation.{api}",
                lambda api=api: _method(tessellation, api),
            )
            if type(value) is not int or value < 0:
                raise CadError(
                    "cad_native_tessellation_unreadable",
                    "a native tessellation count is not a non-negative integer",
                    {**base, "api": f"ITessellation.{api}", "returned": repr(value)[:60]},
                )
            native_totals[name] = value

        facets = {}
        fins_by_id = {}
        for face_row in face_rows:
            face_index = face_row["index"]
            facet_ids = call(
                "ITessellation.GetFaceFacets",
                lambda face=face_row["object"]: _as_list(
                    _member(tessellation, "GetFaceFacets", face)
                ),
                face_index=face_index,
            )
            normalized_facets = []
            for raw_facet_id in facet_ids:
                if type(raw_facet_id) is not int or raw_facet_id < 0:
                    raise CadError(
                        "cad_native_tessellation_unreadable",
                        "GetFaceFacets returned an invalid facet ID",
                        {**base, "face_index": face_index, "facet_id": repr(raw_facet_id)[:60]},
                    )
                if raw_facet_id in facets:
                    raise CadError(
                        "cad_native_tessellation_unreadable",
                        "a facet ID belongs to multiple faces",
                        {**base, "face_index": face_index, "facet_id": raw_facet_id},
                    )
                raw_fins = call(
                    "ITessellation.GetFacetFins",
                    lambda facet=raw_facet_id: _as_list(
                        _member(tessellation, "GetFacetFins", facet)
                    ),
                    face_index=face_index,
                    facet_id=raw_facet_id,
                )
                if (
                    len(raw_fins) != 3
                    or any(type(fin) is not int or fin < 0 for fin in raw_fins)
                    or len(set(raw_fins)) != 3
                ):
                    raise CadError(
                        "cad_native_tessellation_unreadable",
                        "a facet does not expose three distinct fin IDs",
                        {**base, "face_index": face_index, "facet_id": raw_facet_id},
                    )
                for fin_id in raw_fins:
                    if fin_id in fins_by_id:
                        raise CadError(
                            "cad_native_tessellation_unreadable",
                            "a fin belongs to multiple facets",
                            {**base, "facet_id": raw_facet_id, "fin_id": fin_id},
                        )
                    fins_by_id[fin_id] = {"facet_id": raw_facet_id}
                facets[raw_facet_id] = {
                    "facet_id": raw_facet_id,
                    "face_index": face_index,
                    "fin_ids": tuple(raw_fins),
                }
                normalized_facets.append(raw_facet_id)
            face_row["facet_ids"] = tuple(normalized_facets)

        face_raw_references = {
            row["reference"]["raw_sha256"]
            for row in face_rows
        }
        face_aliased = (
            len(face_rows) > 1
            and len(face_raw_references) < len(face_rows)
        )
        if face_aliased and len(face_raw_references) != 1:
            raise CadError(
                "cad_persistent_reference_alias",
                "native face references mix aliased and distinct values",
                {
                    **base,
                    "reference_sha256": sorted(face_raw_references),
                },
            )
        if face_aliased:
            _require_scope_owner(owner_scope, base)
        for face_row in face_rows:
            if not face_aliased:
                registry.claim(
                    kind="face",
                    raw_sha256=face_row["reference"]["raw_sha256"],
                    typed_sha256=face_row["reference"]["typed_sha256"],
                    owner={
                        "kind": "face",
                        **base,
                        "face_index": face_row["index"],
                    },
                    base=base,
                )
                continue
            geometry = call(
                "IFace2.GetArea/GetTessTriangles",
                lambda value=face_row["object"]: _face_scope_geometry(value),
                face_index=face_row["index"],
            )
            topology_key = {
                "rule": NATIVE_FACE_IDENTITY_KEY_RULE,
                "area_binary64": geometry["area_binary64"],
                "bbox_binary64": list(geometry["bbox_binary64"]),
            }
            witness = (
                get_witness_rows().get("face", {}).get(
                    face_row["index"]
                )
            )
            if (
                not isinstance(witness, dict)
                or set(witness)
                != {
                    "part_document_sha256",
                    "part_body_reference_sha256",
                    "face_index",
                    "persistent_reference_sha256",
                }
            ):
                raise CadError(
                    "cad_scope_witness_unavailable",
                    "an aliased native face has no frozen part witness",
                    {**base, "face_index": face_row["index"]},
                )
            identity = {
                "rule_version": NATIVE_SCOPE_IDENTITY_RULE,
                "entity_kind": "face",
                "identity_kind": "mapped_scope_local",
                "persistent": False,
                "owner_scope_key": _scope_owner_key(
                    owner_scope,
                    body_type=body_type,
                    body_index=body_index,
                    body_name=body_name,
                ),
                "source_document_sha256": owner_scope[
                    "source_document_sha256"
                ],
                "referenced_configuration": owner_scope[
                    "referenced_configuration"
                ],
                "occurrence_scope": owner_scope["occurrence"],
                "body_type": body_type,
                "body_index": body_index,
                "body_name": body_name,
                "native_face_index": face_row["index"],
                "area_binary64_hex": geometry["area_binary64"],
                "tessellation_bbox_min_binary64_hex": list(
                    geometry["bbox_binary64"][:3]
                ),
                "tessellation_bbox_max_binary64_hex": list(
                    geometry["bbox_binary64"][3:]
                ),
                "tessellation_coordinate_sha256_float64_le": geometry[
                    "coordinate_sha256_float64_le"
                ],
                "topology_signature_sha256": native_scope_identity_sha256(
                    {
                        **topology_key,
                        "facet_ids": sorted(face_row["facet_ids"]),
                    }
                ),
                "raw_component_reference_sha256": face_row["reference"][
                    "raw_sha256"
                ],
                "raw_component_reference_kind": "body_scope_alias",
                "mapping_source_part_document_sha256": witness[
                    "part_document_sha256"
                ],
                "mapping_source_body_reference_sha256": witness[
                    "part_body_reference_sha256"
                ],
                "mapping_source_face_index": witness["face_index"],
                "mapped_source_face_persistent_reference_sha256": witness[
                    "persistent_reference_sha256"
                ],
                "mapping_method": NATIVE_SCOPE_MAPPING_METHOD,
                "source_authority": NATIVE_SCOPE_SOURCE_AUTHORITY,
                "warnings": [
                    NATIVE_COMPONENT_REFERENCE_ALIASED,
                    NATIVE_PART_WITNESS_DERIVED,
                    NATIVE_SCOPE_LOCAL_IDENTITY_DERIVED,
                    NATIVE_TOPOLOGY_IDENTITY_DERIVED,
                ],
            }
            identity_sha256 = native_scope_identity_sha256(identity)
            face_row["identity"] = identity
            face_row["reference"]["typed_sha256"] = identity_sha256
            registry.claim_scope(
                kind="face",
                raw_sha256=face_row["reference"]["raw_sha256"],
                identity_sha256=identity_sha256,
                owner={
                    "kind": "face",
                    **base,
                    "face_index": face_row["index"],
                },
                base=base,
            )
        if tessellation_request is not None and face_rows:
            tessellation_request["face_list"][
                "first_face_reference_sha256"
            ] = face_rows[0]["reference"]["typed_sha256"]
            tessellation_request["face_list"][
                "last_face_reference_sha256"
            ] = face_rows[-1]["reference"]["typed_sha256"]

        fin_ids_by_edge_index = {}
        for edge_row in edge_rows:
            edge_index = edge_row["index"]
            edge_fins = call(
                "ITessellation.GetEdgeFins",
                lambda edge=edge_row["object"]: _as_list(
                    _member(tessellation, "GetEdgeFins", edge)
                ),
                edge_index=edge_index,
            )
            normalized = []
            seen = set()
            for fin_id in edge_fins:
                if type(fin_id) is not int or fin_id < 0:
                    raise CadError(
                        "cad_native_tessellation_unreadable",
                        "GetEdgeFins returned an invalid fin ID",
                        {**base, "edge_index": edge_index, "fin_id": repr(fin_id)[:60]},
                    )
                if fin_id not in fins_by_id:
                    raise CadError(
                        "cad_native_tessellation_unreadable",
                        "an edge returned a fin outside the captured facets",
                        {**base, "edge_index": edge_index, "fin_id": fin_id},
                    )
                if fin_id in seen:
                    raise CadError(
                        "cad_native_tessellation_unreadable",
                        "a native edge repeats a fin ID",
                        {**base, "edge_index": edge_index, "fin_id": fin_id},
                    )
                seen.add(fin_id)
                normalized.append(fin_id)
            normalized.sort()
            edge_row["fin_ids"] = tuple(normalized)
            fin_ids_by_edge_index[edge_index] = tuple(normalized)
            incidence = len(edge_row["face_indices"])
            if incidence not in (1, 2):
                raise CadError(
                    "cad_native_tessellation_unreadable",
                    "a native edge has more than two adjacent faces",
                    {
                        **base,
                        "edge_index": edge_index,
                        "face_indices": tuple(edge_row["face_indices"]),
                    },
                )
            if incidence == 1 and body_type != 1:
                raise CadError(
                    "cad_native_tessellation_unreadable",
                    "an open native edge is valid only on a sheet body",
                    {
                        **base,
                        "edge_index": edge_index,
                    },
                )
            if not normalized:
                raise CadError(
                    "cad_native_tessellation_unreadable",
                    "a native edge did not return any fins",
                    {
                        **base,
                        "edge_index": edge_index,
                    },
                )
        try:
            fin_to_edge_row = _fin_edge_row_map(
                edge_rows,
                fin_ids_by_edge_index,
            )
        except ValueError as error:
            raise CadError(
                "cad_native_tessellation_unreadable",
                str(error),
                {**base},
            ) from error

        fin_vertices = {}
        vertex_points_binary64 = {}
        vertex_points_float32 = {}
        for fin_id in sorted(fins_by_id):
            vertex_ids = call(
                "ITessellation.GetFinVertices",
                lambda fin=fin_id: _as_list(
                    _member(tessellation, "GetFinVertices", fin)
                ),
                fin_id=fin_id,
            )
            if (
                len(vertex_ids) != 2
                or any(type(vertex) is not int or vertex < 0 for vertex in vertex_ids)
                or len(set(vertex_ids)) != 2
            ):
                raise CadError(
                    "cad_native_tessellation_unreadable",
                    "a fin does not expose two distinct vertex IDs",
                    {**base, "fin_id": fin_id},
                )
            fin_vertices[fin_id] = tuple(vertex_ids)
            for vertex_id in vertex_ids:
                if vertex_id in vertex_points_binary64:
                    continue
                point = call(
                    "ITessellation.GetVertexPoint",
                    lambda vertex=vertex_id: _as_list(
                        _member(tessellation, "GetVertexPoint", vertex)
                    ),
                    vertex_id=vertex_id,
                )
                try:
                    vertex_points_binary64[vertex_id] = _binary64_xyz(point)
                    vertex_points_float32[vertex_id] = _float32_xyz(point)
                except (TypeError, ValueError, OverflowError) as error:
                    raise CadError(
                        "cad_native_tessellation_unreadable",
                        "a tessellation vertex has no finite point",
                        {**base, "vertex_id": vertex_id, "error": str(error)},
                    ) from error

        observed_totals = {
            "facets": len(facets),
            "fins": len(fins_by_id),
            "vertices": len(vertex_points_binary64),
        }
        if observed_totals != native_totals:
            raise CadError(
                "cad_native_tessellation_unreadable",
                "captured tessellation totals differ from native counts",
                {
                    **base,
                    "native": native_totals,
                    "observed": observed_totals,
                },
            )

        for edge_row in edge_rows:
            edge_row["fin_ids"] = tuple(edge_row["fin_ids"])
        edge_raw_references = {
            row["reference"]["raw_sha256"]
            for row in edge_rows
        }
        edge_aliased = (
            len(edge_rows) > 1
            and len(edge_raw_references) < len(edge_rows)
        )
        if edge_aliased and len(edge_raw_references) != 1:
            raise CadError(
                "cad_persistent_reference_alias",
                "native edge references mix aliased and distinct values",
                {
                    **base,
                    "reference_sha256": sorted(edge_raw_references),
                },
            )
        if edge_aliased:
            _require_scope_owner(owner_scope, base)

        mapped_edge_witnesses = {}
        vertex_observations = []
        for edge_row in edge_rows:
            for order, endpoint_name in enumerate(("start", "end")):
                endpoint = edge_row["endpoints"][endpoint_name]
                if endpoint["state"] != "present":
                    continue
                vertex_observations.append(
                    (
                        endpoint["point_m"],
                        endpoint["reference"]["raw_sha256"],
                        endpoint["reference"]["typed_sha256"],
                        edge_row["index"],
                        order,
                        endpoint["object_pointer"],
                    )
                )
        try:
            vertex_groups = _group_vertex_observations(vertex_observations)
        except ValueError as error:
            raise CadError(
                "cad_native_tessellation_unreadable",
                str(error),
                {**base},
            ) from error
        vertices_by_point = {}
        for group in vertex_groups:
            point_bits = group["point_bits"]
            vertices_by_point.setdefault(point_bits, []).append(group)
        ordered_vertices = sorted(
            (
                (
                    point_bits,
                    occurrence,
                    group,
                )
                for point_bits, groups in vertices_by_point.items()
                for occurrence, group in enumerate(groups)
            ),
            key=lambda row: (row[0], row[1]),
        )
        vertex_raw_references = {
            group["raw_reference_sha256"]
            for _, _, group in ordered_vertices
        }
        vertex_aliased = (
            len(ordered_vertices) > 1
            and len(vertex_raw_references) < len(ordered_vertices)
        )
        if vertex_aliased and len(vertex_raw_references) != 1:
            raise CadError(
                "cad_persistent_reference_alias",
                "native vertex references mix aliased and distinct values",
                {**base, "reference_sha256": sorted(vertex_raw_references)},
            )
        precomputed_vertex_identity_rows = []
        if vertex_aliased:
            _require_scope_owner(owner_scope, base)
            vertex_witnesses = get_witness_rows().get("vertex", {})
            edge_witnesses = get_witness_rows().get("edge", {})
            mapped_edge_witnesses = edge_witnesses
            for vertex_index, (
                point_bits,
                coincident_ordinal,
                group,
            ) in enumerate(ordered_vertices):
                raw_sha256 = group["raw_reference_sha256"]
                witness = vertex_witnesses.get(vertex_index)
                if (
                    not isinstance(witness, dict)
                    or set(witness)
                    != {
                        "part_document_sha256",
                        "part_body_reference_sha256",
                        "vertex_index",
                        "persistent_reference_sha256",
                    }
                ):
                    raise CadError(
                        "cad_scope_witness_unavailable",
                        "an aliased native vertex has no frozen part witness",
                        {**base, "vertex_index": vertex_index},
                    )
                incident_component_edges = sorted({
                    edge_index
                    for edge_index, _ in group["occurrences"]
                })
                for edge_index in incident_component_edges:
                    edge_witness = edge_witnesses.get(edge_index)
                    if (
                        not isinstance(edge_witness, dict)
                        or set(edge_witness)
                        != {
                            "part_document_sha256",
                            "part_body_reference_sha256",
                            "edge_index",
                            "persistent_reference_sha256",
                        }
                    ):
                        raise CadError(
                            "cad_scope_witness_unavailable",
                            "an aliased native vertex has no mapped edge witness",
                            {
                                **base,
                                "vertex_index": vertex_index,
                                "edge_index": edge_index,
                            },
                        )
                incident_part_edges = sorted(
                    edge_witnesses[edge_index]["edge_index"]
                    for edge_index in incident_component_edges
                )
                incident_part_edge_references = sorted(
                    edge_witnesses[edge_index][
                        "persistent_reference_sha256"
                    ]
                    for edge_index in incident_component_edges
                )
                topology_key = {
                    "point_binary64": list(point_bits),
                    "coincident_occurrence_index": coincident_ordinal,
                    "incident_mapped_edge_indices": incident_part_edges,
                    "incident_mapped_edge_reference_sha256":
                        incident_part_edge_references,
                }
                identity = {
                    "rule_version": NATIVE_SCOPE_IDENTITY_RULE,
                    "entity_kind": "vertex",
                    "identity_kind": "mapped_scope_local",
                    "persistent": False,
                    "owner_scope_key": _scope_owner_key(
                        owner_scope,
                        body_type=body_type,
                        body_index=body_index,
                        body_name=body_name,
                    ),
                    "source_document_sha256": owner_scope[
                        "source_document_sha256"
                    ],
                    "native_vertex_index": vertex_index,
                    "point_binary64_hex": list(point_bits),
                    "coincident_occurrence_index": coincident_ordinal,
                    "incident_mapped_edge_indices": incident_part_edges,
                    "incident_mapped_edge_reference_sha256":
                        incident_part_edge_references,
                    "topology_signature_sha256":
                        native_scope_identity_sha256(topology_key),
                    "mapping_source_part_document_sha256": witness[
                        "part_document_sha256"
                    ],
                    "mapping_source_body_reference_sha256": witness[
                        "part_body_reference_sha256"
                    ],
                    "mapping_source_vertex_index": witness["vertex_index"],
                    "mapped_source_vertex_persistent_reference_sha256":
                        witness["persistent_reference_sha256"],
                    "mapping_method": NATIVE_SCOPE_VERTEX_MAPPING_METHOD,
                    "source_authority": NATIVE_SCOPE_SOURCE_AUTHORITY,
                    "warnings": [
                        NATIVE_COMPONENT_REFERENCE_ALIASED,
                        NATIVE_PART_WITNESS_DERIVED,
                        NATIVE_SCOPE_LOCAL_IDENTITY_DERIVED,
                        NATIVE_TOPOLOGY_IDENTITY_DERIVED,
                    ],
                }
                identity_sha256 = native_scope_identity_sha256(identity)
                precomputed_vertex_identity_rows.append(
                    {
                        "vertex_index": vertex_index,
                        "point_m": list(group["point_m"]),
                        "identity_mode": NATIVE_SCOPE_IDENTITY_RULE,
                        "native_reference_sha256": raw_sha256,
                        "identity": identity,
                        "identity_sha256": identity_sha256,
                    }
                )
                registry.claim_scope(
                    kind="vertex",
                    raw_sha256=raw_sha256,
                    identity_sha256=identity_sha256,
                    owner={
                        "kind": "vertex",
                        **base,
                        "vertex_index": vertex_index,
                    },
                    base=base,
                )
                for edge_index, endpoint_order in group["occurrences"]:
                    endpoint_name = ("start", "end")[endpoint_order]
                    endpoint = edge_rows[edge_index]["endpoints"][
                        endpoint_name
                    ]
                    endpoint["reference"]["typed_sha256"] = identity_sha256
                    endpoint["identity"] = identity

        edge_topology_keys = {}
        for edge_row in edge_rows:
            legacy_identity = edge_row["reference"]["typed_sha256"]
            if not edge_aliased:
                registry.claim(
                    kind="edge",
                    raw_sha256=edge_row["reference"]["raw_sha256"],
                    typed_sha256=legacy_identity,
                    owner={
                        "kind": "edge",
                        **base,
                        "edge_index": edge_row["index"],
                    },
                    base=base,
                )
                continue
            endpoints = edge_row["endpoints"]
            endpoint_states = {
                endpoints[name]["state"] for name in ("start", "end")
            }
            if endpoint_states not in ({"present"}, {"absent"}):
                raise CadError(
                    "cad_native_tessellation_unreadable",
                    "native edge endpoint states differ",
                    {**base, "edge_index": edge_row["index"]},
                )
            face_witnesses = get_witness_rows().get("face", {})
            adjacent_face_ids = sorted(
                face_witnesses[face_index]["face_index"]
                for face_index in edge_row["face_indices"]
            )
            curve = edge_row["curve"]
            topology_endpoints = native_curve_parameter_endpoints(
                curve["start_point_m"],
                curve["end_point_m"],
            )
            identity_endpoints = {
                name: (
                    endpoints[name]["point_m"]
                    if endpoints[name]["state"] == "present"
                    else topology_endpoints[index]
                )
                for index, name in enumerate(("start", "end"))
            }

            def point_bits(point):
                return (
                    None
                    if point is None
                    else [_binary64_bits(value) for value in point]
                )

            topology_key = {
                "start_point_binary64": point_bits(
                    endpoints["start"]["point_m"]
                ),
                "end_point_binary64": point_bits(
                    endpoints["end"]["point_m"]
                ),
                "curve_u_min_binary64": _binary64_bits(curve["u_min"]),
                "curve_u_max_binary64": _binary64_bits(curve["u_max"]),
                "curve_start_point_binary64": [
                    _binary64_bits(value)
                    for value in curve["start_point_m"]
                ],
                "curve_end_point_binary64": [
                    _binary64_bits(value)
                    for value in curve["end_point_m"]
                ],
                "curve_sense": curve["sense"],
                "curve_type": curve["curve_type"],
                "curve_closed": edge_row["curve_end_params"]["is_closed"],
                "curve_periodic": edge_row["curve_end_params"][
                    "is_periodic"
                ],
                "adjacent_mapped_face_ids": adjacent_face_ids,
            }
            topology_sha256 = native_scope_identity_sha256(topology_key)
            previous = edge_topology_keys.get(topology_sha256)
            if previous is not None:
                raise CadError(
                    "cad_scope_identity_ambiguous",
                    "two native edges have the same scope-local topology key",
                    {
                        **base,
                        "edge_index": edge_row["index"],
                        "previous_edge_index": previous,
                        "topology_key_sha256": topology_sha256,
                    },
                )
            edge_topology_keys[topology_sha256] = edge_row["index"]
            witness = (
                get_witness_rows().get("edge", {}).get(
                    edge_row["index"]
                )
            )
            if (
                not isinstance(witness, dict)
                or set(witness)
                != {
                    "part_document_sha256",
                    "part_body_reference_sha256",
                    "edge_index",
                    "persistent_reference_sha256",
                }
            ):
                raise CadError(
                    "cad_scope_witness_unavailable",
                    "an aliased native edge has no frozen part witness",
                    {**base, "edge_index": edge_row["index"]},
                )
            identity = {
                "rule_version": NATIVE_SCOPE_IDENTITY_RULE,
                "entity_kind": "edge",
                "identity_kind": "mapped_scope_local",
                "persistent": False,
                "owner_scope_key": _scope_owner_key(
                    owner_scope,
                    body_type=body_type,
                    body_index=body_index,
                    body_name=body_name,
                ),
                "source_document_sha256": owner_scope[
                    "source_document_sha256"
                ],
                "native_edge_index": edge_row["index"],
                "start_point_binary64_hex": [
                    _binary64_bits(value)
                    for value in identity_endpoints["start"]
                ],
                "end_point_binary64_hex": [
                    _binary64_bits(value)
                    for value in identity_endpoints["end"]
                ],
                "start_vertex_reference_sha256": (
                    endpoints["start"]["reference"]["typed_sha256"]
                    if endpoints["start"]["reference"] is not None
                    else None
                ),
                "end_vertex_reference_sha256": (
                    endpoints["end"]["reference"]["typed_sha256"]
                    if endpoints["end"]["reference"] is not None
                    else None
                ),
                "curve_type": curve["curve_type"],
                "curve_sense": curve["sense"],
                "curve_domain_binary64_hex": [
                    _binary64_bits(curve["u_min"]),
                    _binary64_bits(curve["u_max"]),
                ],
                "curve_closed": edge_row["curve_end_params"]["is_closed"],
                "curve_periodic": edge_row["curve_end_params"][
                    "is_periodic"
                ],
                "adjacent_mapped_face_ids": adjacent_face_ids,
                "topology_signature_sha256": topology_sha256,
                "mapping_source_part_document_sha256": witness[
                    "part_document_sha256"
                ],
                "mapping_source_body_reference_sha256": witness[
                    "part_body_reference_sha256"
                ],
                "mapping_source_edge_index": witness["edge_index"],
                "mapping_source_edge_persistent_reference_sha256": witness[
                    "persistent_reference_sha256"
                ],
                "mapping_method": NATIVE_SCOPE_MAPPING_METHOD,
                "source_authority": NATIVE_SCOPE_SOURCE_AUTHORITY,
                "warnings": [
                    NATIVE_COMPONENT_REFERENCE_ALIASED,
                    NATIVE_PART_WITNESS_DERIVED,
                    NATIVE_SCOPE_LOCAL_IDENTITY_DERIVED,
                    NATIVE_TOPOLOGY_IDENTITY_DERIVED,
                ],
            }
            identity_sha256 = native_scope_identity_sha256(identity)
            edge_row["identity"] = identity
            edge_row["reference"]["typed_sha256"] = identity_sha256
            registry.claim_scope(
                kind="edge",
                raw_sha256=edge_row["reference"]["raw_sha256"],
                identity_sha256=identity_sha256,
                owner={
                    "kind": "edge",
                    **base,
                    "edge_index": edge_row["index"],
                },
                base=base,
            )
        vertex_identity_rows = list(precomputed_vertex_identity_rows)
        if not vertex_aliased:
            for vertex_index, (_, _, group) in enumerate(
                ordered_vertices
            ):
                typed_sha256 = group["typed_reference_sha256"]
                raw_sha256 = group["raw_reference_sha256"]
                vertex_identity_rows.append(
                    {
                        "vertex_index": vertex_index,
                        "point_m": list(group["point_m"]),
                        "identity_mode": NATIVE_PERSISTENT_IDENTITY_RULE,
                        "native_reference_sha256": raw_sha256,
                        "identity": None,
                        "identity_sha256": typed_sha256,
                    }
                )
                registry.claim(
                    kind="vertex",
                    raw_sha256=raw_sha256,
                    typed_sha256=typed_sha256,
                    owner={
                        "kind": "vertex",
                        **base,
                        "vertex_index": vertex_index,
                    },
                    base=base,
                )

        def fin_endpoint_u(
            edge,
            point,
            domain,
            *,
            fin_id,
            endpoint,
            edge_reference_sha256,
            diagnostic_context,
        ):
            common = {
                **base,
                **diagnostic_context,
                "fin_id": fin_id,
                "endpoint": endpoint,
                "edge_reference_sha256": edge_reference_sha256,
                "u_parameter_rule": NATIVE_U_CLOSEST_POINT_PARAMETER_RULE,
                "projection_rule": (
                    NATIVE_TESSELLATION_EDGE_PROJECTION_RULE
                ),
                "projection_metric": (
                    NATIVE_TESSELLATION_EDGE_PROJECTION_METRIC
                ),
                "projection_tolerance_m": (
                    NATIVE_TESSELLATION_EDGE_PROJECTION_TOLERANCE_M
                ),
                "curve_u_min": domain["u_min"],
                "curve_u_max": domain["u_max"],
                "curve_sense": domain["sense"],
                "curve_start_point_m": domain["start_point_m"],
                "curve_end_point_m": domain["end_point_m"],
            }
            closest = call(
                "IEdge.GetClosestPointOn",
                lambda edge=edge, point=point: _as_list(
                    _method(edge, "GetClosestPointOn", *point)
                ),
                fin_id=fin_id,
                endpoint=endpoint,
            )
            if (
                len(closest) != 5
                or any(
                    type(value) not in (int, float)
                    for value in closest[:4]
                )
                or not all(
                    math.isfinite(float(value))
                    for value in closest[:4]
                )
            ):
                raise CadError(
                    "cad_native_tessellation_unreadable",
                    "GetClosestPointOn returned malformed point/U evidence",
                    {
                        **common,
                        "get_closest_point_on_raw": repr(closest)[:500],
                    },
                )
            closest_point = _binary64_xyz(closest[0:3])
            closest_u = float(closest[3])
            closest_evidence = [*closest_point, closest_u]

            def point_error(left, right):
                return max(
                    abs(left[index] - right[index])
                    for index in range(3)
                )

            projection_error = point_error(point, closest_point)
            if not native_point_within_max_coordinate_bound(
                point,
                closest_point,
                NATIVE_TESSELLATION_EDGE_PROJECTION_TOLERANCE_M,
            ):
                raise CadError(
                    "cad_native_tessellation_unreadable",
                    "tessellation edge projection exceeds the 50 micrometre bound",
                    {
                        **common,
                        "source_point_m": list(point),
                        "closest_point_on_raw": closest_evidence,
                        "closest_point_m": list(closest_point),
                        "u": closest_u,
                        "projection_errors_m": {
                            "source_to_closest": projection_error,
                            "closest_to_evaluate": None,
                            "source_to_evaluate": None,
                        },
                        "rejected_predicates": ["source_to_closest"],
                    },
                )
            endpoint_u = closest_u
            u_parameter_rule = NATIVE_U_CLOSEST_POINT_PARAMETER_RULE
            if not native_u_in_closed_interval(
                endpoint_u,
                domain["u_min"],
                domain["u_max"],
            ):
                raise CadError(
                    "cad_native_tessellation_unreadable",
                    "GetClosestPointOn endpoint U is outside the edge domain",
                    {
                        **common,
                        "source_point_m": list(point),
                        "closest_point_on_raw": closest_evidence,
                        "closest_point_m": list(closest_point),
                        "u": endpoint_u,
                    },
                )
            evaluated_raw = call(
                "IEdge.Evaluate2",
                lambda edge=edge, endpoint_u=endpoint_u: _as_list(
                    _method(edge, "Evaluate2", endpoint_u, 1)
                ),
                fin_id=fin_id,
                endpoint=endpoint,
            )
            evaluate_evidence = native_evaluate2_evidence(evaluated_raw)
            if evaluate_evidence is None:
                raise CadError(
                    "cad_native_tessellation_unreadable",
                    "Evaluate2 shape or point payload is invalid",
                    {
                        **common,
                        "source_point_m": list(point),
                        "closest_point_on_raw": closest_evidence,
                        "u": endpoint_u,
                        "evaluate2_raw": list(evaluated_raw),
                    },
                )
            if not evaluate_evidence["packed_status"]["parsed_success"]:
                raise CadError(
                    "cad_native_tessellation_unreadable",
                    "Evaluate2 packed success bit is zero",
                    {
                        **common,
                        "source_point_m": list(point),
                        "closest_point_on_raw": closest_evidence,
                        "u": endpoint_u,
                        "evaluate2_raw": list(evaluated_raw),
                        "evaluate_packed_status": evaluate_evidence[
                            "packed_status"
                        ],
                    },
                )
            evaluated_point = _binary64_xyz(
                evaluate_evidence["payload"][:3]
            )
            projection_errors = {
                "source_to_closest": projection_error,
                "closest_to_evaluate": point_error(
                    closest_point,
                    evaluated_point,
                ),
                "source_to_evaluate": point_error(
                    point,
                    evaluated_point,
                ),
            }
            projection_pairs = {
                "source_to_closest": (point, closest_point),
                "closest_to_evaluate": (closest_point, evaluated_point),
                "source_to_evaluate": (point, evaluated_point),
            }
            rejected_predicates = [
                name
                for name, pair in projection_pairs.items()
                if not native_point_within_max_coordinate_bound(
                    pair[0],
                    pair[1],
                    NATIVE_TESSELLATION_EDGE_PROJECTION_TOLERANCE_M,
                )
            ]
            if rejected_predicates:
                raise CadError(
                    "cad_native_tessellation_unreadable",
                    "native closest-point projection exceeds the 50 micrometre bound",
                    {
                        **common,
                        "source_point_m": list(point),
                        "closest_point_on_raw": closest_evidence,
                        "closest_point_m": list(closest_point),
                        "u": endpoint_u,
                        "evaluate2_raw": list(evaluated_raw),
                        "evaluate_payload": evaluate_evidence["payload"],
                        "evaluate_packed_status": evaluate_evidence[
                            "packed_status"
                        ],
                        "projection_errors_m": projection_errors,
                        "rejected_predicates": rejected_predicates,
                    },
                )
            return {
                "u": endpoint_u,
                "u_parameter_rule": u_parameter_rule,
                "rule_version": NATIVE_U_IDENTITY_RULE,
                "projection_rule": NATIVE_TESSELLATION_EDGE_PROJECTION_RULE,
                "projection_metric": (
                    NATIVE_TESSELLATION_EDGE_PROJECTION_METRIC
                ),
                "projection_tolerance_m": (
                    NATIVE_TESSELLATION_EDGE_PROJECTION_TOLERANCE_M
                ),
                "source_point_m": list(point),
                "closest_point_on_raw": closest_evidence,
                "evaluate2_raw": list(evaluated_raw),
                "evaluate_payload": evaluate_evidence["payload"],
                "evaluate_packed_status": evaluate_evidence[
                    "packed_status"
                ],
                "projection_errors_m": projection_errors,
            }

        u_observation_cache = {}
        u_cache_hits = 0
        for fin_id, row in fins_by_id.items():
            row["vertex_ids"] = fin_vertices[fin_id]
            row["face_index"] = facets[row["facet_id"]]["face_index"]
            edge_row = fin_to_edge_row.get(fin_id)
            row["edge_reference_sha256"] = (
                edge_row["reference"]["typed_sha256"]
                if edge_row is not None
                else None
            )
            row["internal"] = edge_row is None
            row["classification"] = (
                "internal_only" if row["internal"] else "native_edge"
            )
            if row["internal"]:
                row.update(
                    {
                        "start_u": None,
                        "end_u": None,
                        "start_evidence": None,
                        "end_evidence": None,
                        "cofin_raw_id": None,
                        "cofin_id": None,
                    }
                )
            else:
                edge = edge_row
                domain = edge["curve"]
                cofin_id = call(
                    "ITessellation.GetFinCoFin",
                    lambda fin=fin_id: _method(tessellation, "GetFinCoFin", fin),
                    fin_id=fin_id,
                )
                if type(cofin_id) is not int or cofin_id < -1:
                    raise CadError(
                        "cad_native_tessellation_unreadable",
                        "GetFinCoFin did not return an integer boundary or cofin ID",
                        {
                            **base,
                            "fin_id": fin_id,
                            "raw_value": repr(cofin_id)[:60],
                            "raw_type": type(cofin_id).__name__,
                        },
                    )
                incidence = len(edge_row["face_indices"])
                if incidence == 1:
                    if cofin_id != -1:
                        raise CadError(
                            "cad_native_tessellation_unreadable",
                            "an open-edge fin did not report the boundary sentinel",
                            {
                                **base,
                                "edge_index": edge_row["index"],
                                "fin_id": fin_id,
                                "cofin_raw_id": cofin_id,
                            },
                        )
                    row["cofin_raw_id"] = -1
                    row["cofin_id"] = None
                elif incidence == 2:
                    if cofin_id < 0:
                        raise CadError(
                            "cad_native_tessellation_unreadable",
                            "a shared-edge fin did not report a cofin ID",
                            {
                                **base,
                                "edge_index": edge_row["index"],
                                "fin_id": fin_id,
                                "cofin_raw_id": cofin_id,
                            },
                        )
                    if cofin_id not in edge_row["fin_ids"]:
                        raise CadError(
                            "cad_native_tessellation_unreadable",
                            "a shared-edge cofin is outside the same edge",
                            {
                                **base,
                                "edge_index": edge_row["index"],
                                "fin_id": fin_id,
                                "cofin_id": cofin_id,
                                "fin_ids": tuple(edge_row["fin_ids"]),
                            },
                        )
                    row["cofin_raw_id"] = cofin_id
                    row["cofin_id"] = cofin_id
                else:
                    raise CadError(
                        "cad_native_tessellation_unreadable",
                        "a native edge has more than two adjacent faces",
                        {
                            **base,
                            "edge_index": edge_row["index"],
                            "face_indices": tuple(edge_row["face_indices"]),
                        },
                    )
                topology_assignment = edge["topology_endpoint_assignment"]
                endpoint_evidence = []
                for endpoint, vertex_id in zip(
                    ("start", "end"),
                    row["vertex_ids"],
                    strict=True,
                ):
                    point = vertex_points_binary64[vertex_id]
                    point_hash = hashlib.sha256(
                        struct.pack("<3d", *point)
                    ).hexdigest()
                    cache_key = (
                        row["edge_reference_sha256"],
                        vertex_id,
                        point_hash,
                    )
                    evidence = u_observation_cache.get(cache_key)
                    if evidence is None:
                        face_row = face_rows[row["face_index"]]
                        diagnostic_context = {
                            "body_reference_sha256": body_reference[
                                "typed_sha256"
                            ],
                            "native_body_reference_sha256": body_reference[
                                "raw_sha256"
                            ],
                            "face_index": row["face_index"],
                            "face_reference_sha256": face_row["reference"][
                                "typed_sha256"
                            ],
                            "native_face_reference_sha256": face_row[
                                "reference"
                            ]["raw_sha256"],
                            "edge_index": edge["index"],
                            "edge_endpoint_refs": [
                                (
                                    edge["endpoints"][name]["reference"][
                                        "typed_sha256"
                                    ]
                                    if edge["endpoints"][name]["reference"]
                                    is not None
                                    else None
                                )
                                for name in ("start", "end")
                            ],
                            "edge_start_point_m": edge["endpoints"]["start"][
                                "point_m"
                            ],
                            "edge_end_point_m": edge["endpoints"]["end"][
                                "point_m"
                            ],
                            "topology_endpoint_assignment": topology_assignment,
                            "fin_vertex_ids": row["vertex_ids"],
                            "cofin_raw_id": cofin_id,
                            "edge_fin_ids": edge["fin_ids"],
                            "source_scope": body_scope,
                        }
                        observed = fin_endpoint_u(
                            edge["object"],
                            point,
                            domain,
                            fin_id=fin_id,
                            endpoint=endpoint,
                            edge_reference_sha256=(
                                edge["reference"]["typed_sha256"]
                            ),
                            diagnostic_context=diagnostic_context,
                        )
                        u_observation_cache[cache_key] = copy.deepcopy(
                            observed
                        )
                        evidence = copy.deepcopy(observed)
                    else:
                        u_cache_hits += 1
                        evidence = copy.deepcopy(evidence)
                    if native_u_equal(evidence["u"], domain["u_min"]):
                        parameter_endpoint = "start"
                    elif native_u_equal(evidence["u"], domain["u_max"]):
                        parameter_endpoint = "end"
                    else:
                        parameter_endpoint = None
                    if parameter_endpoint is not None:
                        repeated = edge["curve_endpoint_evaluations"][
                            parameter_endpoint
                        ]
                        repeated_point = (
                            tuple(repeated["payload"][:3])
                            if repeated["payload"] is not None
                            else None
                        )
                        repeated_tolerance = (
                            native_point_roundtrip_tolerance(
                                repeated_point,
                                tuple(evidence["evaluate_payload"][:3]),
                            )
                            if repeated_point is not None
                            else None
                        )
                        if (
                            repeated["error"] is not None
                            or repeated_point is None
                            or repeated_tolerance is None
                            or not native_u_equal(
                                repeated["u"],
                                evidence["u"],
                            )
                            or native_point_error(
                                repeated_point,
                                evidence["evaluate_payload"][:3],
                            )
                            > repeated_tolerance
                        ):
                            raise CadError(
                                "cad_native_tessellation_unreadable",
                                "tolerant-edge Evaluate2 evidence is unstable at the boundary",
                                {
                                    **base,
                                    "fin_id": fin_id,
                                    "endpoint": endpoint,
                                    "edge_reference_sha256": (
                                        edge["reference"]["typed_sha256"]
                                    ),
                                    "u": evidence["u"],
                                },
                            )
                    endpoint_evidence.append(evidence)
                start_evidence, end_evidence = endpoint_evidence
                row["start_u"] = start_evidence["u"]
                row["end_u"] = end_evidence["u"]
                row["start_evidence"] = start_evidence
                row["end_evidence"] = end_evidence
        for edge_row in edge_rows:
            incidence = len(edge_row["face_indices"])
            relation_fins = {
                fin_id: {
                    **fins_by_id[fin_id],
                    "face_index": facets[fins_by_id[fin_id]["facet_id"]][
                        "face_index"
                    ],
                }
                for fin_id in edge_row["fin_ids"]
            }
            try:
                validate_native_edge_fin_relations(
                    component=component,
                    edge={
                        "body_type": body_type,
                        "face_indices": edge_row["face_indices"],
                        "fin_ids": edge_row["fin_ids"],
                        "face_senses": edge_row["face_senses"],
                    },
                    edge_fins=edge_row["fin_ids"],
                    fins_by_id=relation_fins,
                    vertices_by_id=vertex_points_binary64,
                )
            except NativeFinRelationError as error:
                raise CadError(
                    "cad_native_tessellation_unreadable",
                    error.message,
                    {
                        **base,
                        "edge_index": edge_row["index"],
                        **error.detail,
                    },
                ) from error
            if edge_row["fin_ids"]:
                intervals_by_face = {}
                for fin_id in edge_row["fin_ids"]:
                    fin = fins_by_id[fin_id]
                    face_index = facets[fin["facet_id"]]["face_index"]
                    intervals_by_face.setdefault(face_index, []).extend(
                        native_fin_interval(
                            fin["start_u"],
                            fin["end_u"],
                            edge_row["curve"]["u_min"],
                            edge_row["curve"]["u_max"],
                            is_periodic=edge_row["curve_end_params"][
                                "is_periodic"
                            ],
                            is_closed=edge_row["curve_end_params"][
                                "is_closed"
                            ],
                        )
                    )
                for face_index, intervals in intervals_by_face.items():
                    if not native_u_intervals_cover(
                        intervals,
                        edge_row["curve"]["u_min"],
                        edge_row["curve"]["u_max"],
                    ):
                        raise CadError(
                            "cad_native_tessellation_unreadable",
                            "one face's native edge fins do not cover its U domain",
                            {
                                **base,
                                "edge_index": edge_row["index"],
                                "face_index": face_index,
                                "intervals": intervals,
                            },
                        )
                for face_index in edge_row["face_indices"]:
                    if (
                        face_rows[face_index]["facet_ids"]
                        and face_index not in intervals_by_face
                    ):
                        raise CadError(
                            "cad_native_tessellation_unreadable",
                            "a tessellated native face has no edge fin U interval",
                            {
                                **base,
                                "edge_index": edge_row["index"],
                                "face_index": face_index,
                            },
                        )
        triangle_records = []
        for face_row in face_rows:
            face_index = face_row["index"]
            for facet_id in face_row["facet_ids"]:
                cycle = _directed_fin_cycle(
                    fins_by_id[fin]["vertex_ids"]
                    for fin in facets[facet_id]["fin_ids"]
                )
                native_points = tuple(
                    vertex_points_binary64[vertex] for vertex in cycle
                )
                output_points = tuple(
                    vertex_points_float32[vertex] for vertex in cycle
                )
                key = _canonical_triangle_key(native_points)
                triangle_record_index = len(triangle_records)
                triangle_records.append(
                    {
                        "triangle_record_index": triangle_record_index,
                        "face_index": face_index,
                        "facet_id": facet_id,
                        "native_binary64_points": native_points,
                        "output_float32_points": output_points,
                        "output_float32_key": key,
                    }
                )
                facets[facet_id].update(
                    {
                        "triangle_record_index": triangle_record_index,
                        "absolute_triangle_record_index": triangle_record_index,
                        "vertex_ids": cycle,
                        "native_binary64_points": native_points,
                        "output_float32_points": output_points,
                        "output_float32_key": key,
                    }
                )

        return {
            "body_type": body_type,
            "body_index": body_index,
            "body_name": body_name,
            "body_reference": body_reference,
            "faces": tuple(
                {
                    "index": row["index"],
                    "reference": row["reference"],
                    "owner_body_reference": row["owner_body_reference"],
                    "facet_ids": row["facet_ids"],
                    "identity": row.get("identity"),
                }
                for row in face_rows
            ),
            "edges": tuple(
                {
                    "index": row["index"],
                    "reference": row["reference"],
                    "owner_body_reference": row["owner_body_reference"],
                    "endpoints": row["endpoints"],
                    "face_indices": row["face_indices"],
                    "fin_ids": row["fin_ids"],
                    "identity": row.get("identity"),
                }
                for row in edge_rows
            ),
            "facets": facets,
            "fins": fins_by_id,
            "vertex_points_binary64": vertex_points_binary64,
            "vertex_points_float32": vertex_points_float32,
            "vertex_identity_rows": tuple(vertex_identity_rows),
            "triangle_records": tuple(triangle_records),
            "internal_fin_ids": tuple(
                fin_id for fin_id, row in fins_by_id.items() if row["internal"]
            ),
            "tessellate_result": tessellate_result,
            "counts": {
                "faces": len(face_rows),
                "edges": len(edge_rows),
                "facets": len(facets),
                "fins": len(fins_by_id),
                "vertices": len(vertex_points_binary64),
                "triangles": len(triangle_records),
                "internal_fins": sum(row["internal"] for row in fins_by_id.values()),
            },
            "native_totals": native_totals,
            "u_observation_stats": {
                "unique_observations": len(u_observation_cache),
                "cache_hits": u_cache_hits,
                "get_closest_point_on_calls": len(u_observation_cache),
                "evaluate2_calls": len(u_observation_cache),
            },
            "reference_alias_audit": dict(registry.alias_audit()),
            "cross_kind_raw_aliases": registry.cross_kind_aliases(),
            "itessellation_body": {
                "body_type": body_type,
                "body_index": body_index,
                "body_reference_sha256": body_reference["typed_sha256"],
                "scope": body_scope,
                "tessellate_result": tessellate_result,
                "request": tessellation_request,
                "properties": tessellation_properties,
                "facet_count": native_totals["facets"],
                "fin_count": native_totals["fins"],
                "vertex_count": native_totals["vertices"],
                "faces": [
                    {
                        "face_index": row["index"],
                        "facet_ids": sorted(row["facet_ids"]),
                        "face_identity_sha256": row["reference"][
                            "typed_sha256"
                        ],
                    }
                    for row in face_rows
                ],
                "facets": [
                    {
                        "facet_id": facet_id,
                        "face_index": facet["face_index"],
                        "fin_ids": sorted(facet["fin_ids"]),
                        "vertex_ids": sorted(facet["vertex_ids"]),
                        "triangle_record_index": facet["triangle_record_index"],
                    }
                    for facet_id, facet in sorted(facets.items())
                ],
                "fins": [
                    {
                        "fin_id": fin_id,
                        "facet_id": fin["facet_id"],
                        "face_index": facets[fin["facet_id"]]["face_index"],
                        "vertex_ids": list(fin["vertex_ids"]),
                        "edge_reference_sha256": fin["edge_reference_sha256"],
                        "classification": fin["classification"],
                        "start_u": fin["start_u"],
                        "end_u": fin["end_u"],
                        "start_evidence": fin["start_evidence"],
                        "end_evidence": fin["end_evidence"],
                        "cofin_raw_id": fin["cofin_raw_id"],
                        "cofin_id": fin["cofin_id"],
                    }
                    for fin_id, fin in sorted(fins_by_id.items())
                ],
            },
            "native_edge_rows": tuple(
                {
                    "body_type": body_type,
                    "body_index": body_index,
                    "edge_reference_sha256": row["reference"]["typed_sha256"],
                    "native_edge_reference_sha256": row["reference"][
                        "raw_sha256"
                    ],
                    "edge_identity_mode": (
                        NATIVE_SCOPE_IDENTITY_RULE
                        if row.get("identity") is not None
                        else NATIVE_PERSISTENT_IDENTITY_RULE
                    ),
                    "edge_identity": row.get("identity"),
                    "mapping_source_edge_index": (
                        mapped_edge_witnesses.get(
                            row["index"],
                            {},
                        ).get("edge_index")
                    ),
                    "mapping_source_edge_persistent_reference_sha256": (
                        mapped_edge_witnesses.get(
                            row["index"],
                            {},
                        ).get(
                            "persistent_reference_sha256"
                        )
                    ),
                    "start_vertex_reference_sha256": (
                        row["endpoints"]["start"]["reference"]["typed_sha256"]
                        if row["endpoints"]["start"]["reference"] is not None
                        else None
                    ),
                    "end_vertex_reference_sha256": (
                        row["endpoints"]["end"]["reference"]["typed_sha256"]
                        if row["endpoints"]["end"]["reference"] is not None
                        else None
                    ),
                    "start_point_m": (
                        list(row["endpoints"]["start"]["point_m"])
                        if row["endpoints"]["start"]["point_m"] is not None
                        else None
                    ),
                    "end_point_m": (
                        list(row["endpoints"]["end"]["point_m"])
                        if row["endpoints"]["end"]["point_m"] is not None
                        else None
                    ),
                    "start_vertex_error": row["endpoints"]["start"][
                        "vertex_error"
                    ],
                    "end_vertex_error": row["endpoints"]["end"][
                        "vertex_error"
                    ],
                    "start_point_error": row["endpoints"]["start"][
                        "point_error"
                    ],
                    "end_point_error": row["endpoints"]["end"][
                        "point_error"
                    ],
                    "start_vertex_state": row["endpoints"]["start"]["state"],
                    "end_vertex_state": row["endpoints"]["end"]["state"],
                    "start_reference_error": row["endpoints"]["start"][
                        "reference_error"
                    ],
                    "end_reference_error": row["endpoints"]["end"][
                        "reference_error"
                    ],
                    "face_indices": list(row["face_indices"]),
                    "fin_ids": sorted(row["fin_ids"]),
                    "u_min": row["curve"]["u_min"],
                    "u_max": row["curve"]["u_max"],
                    "curve_start_point_m": row["curve"]["start_point_m"],
                    "curve_end_point_m": row["curve"]["end_point_m"],
                    "curve_sense": row["curve"]["sense"],
                    "curve_type": row["curve"]["curve_type"],
                    "curve_closed": row["curve_end_params"]["is_closed"],
                    "curve_periodic": row["curve_end_params"]["is_periodic"],
                    "curve_end_params": row["curve_end_params"],
                    "curve_domain_transform": row[
                        "curve_domain_transform"
                    ],
                    "curve_endpoint_evaluations": row[
                        "curve_endpoint_evaluations"
                    ],
                    "topology_endpoint_assignment": row[
                        "topology_endpoint_assignment"
                    ],
                    "endpoint_points_derived_from_curve": row[
                        "endpoint_points_derived_from_curve"
                    ],
                    "face_senses": row["face_senses"],
                }
                for row in edge_rows
            ),
            "vertex_rows": tuple(
                {
                    "body_type": body_type,
                    "body_index": body_index,
                    "vertex_id": vertex_id,
                    "point_m": list(vertex_points_binary64[vertex_id]),
                }
                for vertex_id in sorted(vertex_points_binary64)
            ),
            "solidworks_vertex_identities": tuple(
                {
                    "body_type": body_type,
                    "body_index": body_index,
                    **row,
                }
                for row in vertex_identity_rows
            ),
        }

    def _scope_witnesses_for_body(
        self,
        component,
        body_type,
        body_index,
        body,
        faces,
        *,
        occurrence_extension,
    ):
        """Return frozen-part witness rows for aliased native entities."""

        override = getattr(self, "_scope_witnesses_override", None)
        if override is not None:
            return override(
                component,
                body_type,
                body_index,
                body,
                faces,
            )
        document_key = self._source_components[component][0]
        source_path = self._source_documents[document_key][0]
        component_doc = self._captured_document(
            document_key,
            component,
            "scope_witness",
        )

        source_sha256_before = _hash(source_path)
        referenced_configuration = self._source_components[component][1]
        body_name = str(_member(body, "Name") or "")
        try:
            with _temporary_configuration(
                lambda: component_doc,
                referenced_configuration,
                component,
            ) as (part_doc, _previous):
                part_extension = _member(part_doc, "Extension")
                part_bodies = [
                    candidate
                    for candidate in _part_bodies(part_doc, body_type)
                    if str(_member(candidate, "Name") or "") == body_name
                ]
                if len(part_bodies) != 1:
                    raise ValueError(
                        "the frozen part body is missing or ambiguous"
                    )
                part_body = _dynamic(part_bodies[0])
                part_body_reference_sha256 = (
                    _persistent_reference_sha256(
                        part_extension,
                        part_body,
                    )
                )
                part_faces = _as_list(_member(part_body, "GetFaces"))
                if len(part_faces) != len(faces):
                    raise ValueError(
                        "the frozen part face count differs"
                    )

                component_face_rows = _scope_face_rows(
                    faces,
                    occurrence_extension,
                )
                part_face_rows = _scope_face_rows(
                    part_faces,
                    part_extension,
                )
                face_map = _scope_bijection(
                    component_face_rows,
                    part_face_rows,
                )
                face_index_map = {
                    row["mapping_index"]: face_map[row["mapping_key"]]
                    for row in component_face_rows
                }

                component_edge_rows = _scope_edge_rows(
                    body,
                    occurrence_extension,
                    component_face_rows,
                )
                part_edge_rows = _scope_edge_rows(
                    part_body,
                    part_extension,
                    part_face_rows,
                )
                for row in component_edge_rows:
                    row["mapping_index"] = row["edge_index"]
                    row["mapping_key"] = _scope_edge_mapping_key(
                        row,
                        sorted(
                            face_index_map[face_index]
                            for face_index in row["face_indices"]
                        ),
                    )
                for row in part_edge_rows:
                    row["mapping_index"] = row["edge_index"]
                    row["mapping_key"] = _scope_edge_mapping_key(
                        row,
                        row["face_indices"],
                    )
                edge_map = _scope_bijection(
                    component_edge_rows,
                    part_edge_rows,
                )
                edge_index_map = {
                    row["mapping_index"]: edge_map[row["mapping_key"]]
                    for row in component_edge_rows
                }
                component_vertex_rows = _scope_vertex_rows(
                    component_edge_rows,
                    edge_index_map,
                )
                part_vertex_rows = _scope_vertex_rows(part_edge_rows)
                vertex_map = _scope_bijection(
                    component_vertex_rows,
                    part_vertex_rows,
                )
                vertex_index_map = {
                    row["mapping_index"]: vertex_map[row["mapping_key"]]
                    for row in component_vertex_rows
                }

                if (
                    len({
                        row["reference_sha256"]
                        for row in part_face_rows
                    })
                    != len(part_face_rows)
                    or len({
                        row["reference_sha256"]
                        for row in part_edge_rows
                    })
                    != len(part_edge_rows)
                    or len({
                        row["reference_sha256"]
                        for row in part_vertex_rows
                    })
                    != len(part_vertex_rows)
                ):
                    raise ValueError(
                        "the frozen part witness references are not unique"
                    )
        except Exception as error:
            raise CadError(
                "cad_scope_witness_mapping_invalid",
                "the frozen part witness could not be bound one-to-one",
                {
                    "component": component,
                    "body_type": body_type,
                    "body_index": body_index,
                    "error": str(error)[:300],
                },
            ) from error
        source_sha256_after = _hash(source_path)
        if source_sha256_before != source_sha256_after:
            raise CadError(
                "cad_source_changed",
                "the frozen part changed during scope witness capture",
                {
                    "component": component,
                    "source_path": source_path,
                },
            )

        witnesses = {
            "face": {
                component_index: {
                    "part_document_sha256": source_sha256_after,
                    "part_body_reference_sha256":
                        part_body_reference_sha256,
                    "face_index": part_index,
                    "persistent_reference_sha256": part_face_rows[
                        part_index
                    ]["reference_sha256"],
                }
                for component_index, part_index in face_index_map.items()
            },
            "edge": {
                component_index: {
                    "part_document_sha256": source_sha256_after,
                    "part_body_reference_sha256":
                        part_body_reference_sha256,
                    "edge_index": part_index,
                    "persistent_reference_sha256": part_edge_rows[
                        part_index
                    ]["reference_sha256"],
                }
                for component_index, part_index in edge_index_map.items()
            },
            "vertex": {
                component_index: {
                    "part_document_sha256": source_sha256_after,
                    "part_body_reference_sha256":
                        part_body_reference_sha256,
                    "vertex_index": part_index,
                    "persistent_reference_sha256": part_vertex_rows[
                        part_index
                    ]["reference_sha256"],
                }
                for component_index, part_index in vertex_index_map.items()
            },
        }
        self.notes[
            f"scope_witness:{component}:{body_type}:{body_index}"
        ] = {
            "source_document": normalize_document_path(
                os.path.abspath(source_path)
            ),
            "source_document_sha256": source_sha256_after,
            "referenced_configuration": referenced_configuration,
            "component_body_name": body_name,
            "face_count": len(face_index_map),
            "edge_count": len(edge_index_map),
            "vertex_count": len(vertex_index_map),
            "status": "bidirectional_one_to_one",
            "document_ownership": "existing_owned_session_no_close",
        }
        return witnesses

    def _body_face_triangles(
        self,
        bodies,
        body_type,
        component,
        reference_registry=None,
        *,
        body_holder=None,
    ):
        """Capture the production v3 tessellation stream from ITessellation."""

        registry = reference_registry or _NativeReferenceRegistry()
        values: list[float] = []
        evidence = {
            "schema_version": NATIVE_TESSELLATION_SCHEMA,
            "faces": 0,
            "face_spans": [],
            "native_edges": [],
            "no_facets_faces": [],
            "reference_alias_audit": {
                "body": [],
                "face": [],
                "edge": [],
                "vertex": [],
            },
            "cross_kind_raw_aliases": [],
            "itessellation_bodies": [],
            "itessellation_vertices": [],
            "solidworks_vertex_identities": [],
            "u_observation_stats": {
                "unique_observations": 0,
                "cache_hits": 0,
                "get_closest_point_on_calls": 0,
                "evaluate2_calls": 0,
            },
        }
        completed_faces = 0
        source_document_key = self._source_components[component][0]
        occurrence_document = self._captured_document(
            self._scene_document_key,
            component,
            "geometry",
        )
        extension = _member(occurrence_document, "Extension")
        source_entry = self._source_documents.get(source_document_key)
        scope_context = None
        if source_entry is not None:
            source_path = source_entry[0]
            normalized_source_path = normalize_document_path(
                os.path.abspath(source_path)
            )
            source_file_sha256 = (
                _hash(source_path)
                if os.path.isfile(source_path)
                else None
            )
            source_identity_sha256 = (
                source_file_sha256
                if source_file_sha256 is not None
                else hashlib.sha256(
                    normalized_source_path.encode("utf-8")
                ).hexdigest()
            )
            scope_context = {
                "occurrence": component,
                "source_document_path": normalized_source_path,
                "source_document_sha256": source_file_sha256,
                "source_document_identity_sha256": source_identity_sha256,
                "source_document_identity_kind": (
                    "file_sha256"
                    if source_file_sha256 is not None
                    else "virtual_path_sha256"
                ),
                "active_configuration": self._source_components[
                    component
                ][1],
                "referenced_configuration": self._source_components[
                    component
                ][1],
            }
        def read_body_rows(current_bodies):
            rows = []
            for body_index, raw_body in enumerate(current_bodies):
                body = _dynamic(raw_body)
                context = {
                    "component": component,
                    "body_type": body_type,
                    "body_index": body_index,
                }
                try:
                    body_name = _member(body, "Name")
                    raw_reference = _persistent_reference_bytes(
                        extension,
                        body,
                    )
                except Exception as error:  # noqa: BLE001
                    raise CadError(
                        "cad_persistent_reference_unavailable",
                        "a native body has no readable persistent identity",
                        {**context, "error": str(error)[:300]},
                    ) from error
                if (
                    not isinstance(body_name, str)
                    or not body_name.strip()
                ):
                    raise CadError(
                        "cad_body_identity_unreadable",
                        "a native body has no stable name",
                        context,
                    )
                if not raw_reference:
                    raise CadError(
                        "cad_persistent_reference_unavailable",
                        "a native body has an empty persistent identity",
                        context,
                    )
                try:
                    faces = _as_list(_member(body, "GetFaces"))
                except Exception as error:  # noqa: BLE001
                    raise CadError(
                        "cad_body_faces_unreadable",
                        "the occurrence body's faces could not be read",
                        {
                            **context,
                            "api": "IBody2.GetFaces",
                            "error": str(error),
                        },
                    ) from error
                if not faces:
                    raise CadError(
                        "cad_mesh_export_failed",
                        "a native body carries no faces",
                        {**context, "api": "IBody2.GetFaces"},
                    )
                try:
                    edges = _as_list(_member(body, "GetEdges"))
                    owner_pointers = set()
                    owner_names = set()
                    owner_references = set()
                    for kind, interface_id, values in (
                        ("face", IFACE2_IID, faces),
                        ("edge", IEDGE_IID, edges),
                    ):
                        for raw_value in values:
                            value = _interface(
                                _dynamic(raw_value),
                                interface_id,
                            )
                            owner = _interface(
                                _dynamic(_method(value, "GetBody")),
                                IBODY2_IID,
                            )
                            if owner is None:
                                raise ValueError(
                                    f"a {kind} has no owning IBody2"
                                )
                            owner_pointers.add(
                                _com_iunknown_pointer(owner)
                            )
                            owner_names.add(_member(owner, "Name"))
                            owner_references.add(
                                _persistent_reference_bytes(
                                    extension,
                                    owner,
                                )
                            )
                    if len(owner_pointers) != 1:
                        raise ValueError(
                            "the body faces and edges report several owning IBody2 objects"
                        )
                    if owner_names != {body_name}:
                        raise ValueError(
                            "the body faces and edges report another owning body name"
                        )
                    if owner_references != {raw_reference}:
                        raise ValueError(
                            "the body faces and edges report another owning persistent reference"
                        )
                    body_pointer = _com_iunknown_pointer(body)
                    if (
                        owner_pointers != {body_pointer}
                    ):
                        raise ValueError(
                            "the body faces and edges report another owning COM object"
                        )
                except Exception as error:  # noqa: BLE001
                    raise CadError(
                        "cad_native_tessellation_unreadable",
                        "the native body face and edge ownership is not stable",
                        {
                            **context,
                            "error": str(error)[:300],
                        },
                    ) from error
                rows.append(
                    {
                        "body": body,
                        "body_index": body_index,
                        "body_name": body_name,
                        "object_pointer": _com_iunknown_pointer(body),
                        "owner_pointer": body_pointer,
                        "reference": {
                            "raw": raw_reference,
                            "raw_sha256": hashlib.sha256(
                                raw_reference
                            ).hexdigest(),
                            "typed_sha256": _typed_reference_sha256(
                                "body",
                                raw_reference,
                            ),
                        },
                        "faces": faces,
                        "edges": edges,
                    }
                )
            return rows

        body_rows = read_body_rows(bodies)
        replay_bodies = (
            list(bodies)
            if body_holder is None
            else self._body_list(body_holder, body_type, component)
        )
        if len(replay_bodies) != len(bodies):
            raise CadError(
                "cad_body_identity_changed",
                "the complete native body identity set changed size",
                {
                    "component": component,
                    "body_type": body_type,
                    "first_body_count": len(bodies),
                    "replay_body_count": len(replay_bodies),
                },
            )
        replay_rows = read_body_rows(replay_bodies)
        for row, replay in zip(body_rows, replay_rows, strict=True):
            if (
                row["body_index"] != replay["body_index"]
                or row["body_name"] != replay["body_name"]
                or row["object_pointer"] != replay["object_pointer"]
                or row["owner_pointer"] != replay["owner_pointer"]
                or len(row["faces"]) != len(replay["faces"])
                or len(row["edges"]) != len(replay["edges"])
                or row["reference"]["raw_sha256"]
                != replay["reference"]["raw_sha256"]
                or row["reference"]["typed_sha256"]
                != replay["reference"]["typed_sha256"]
            ):
                raise CadError(
                    "cad_body_identity_changed",
                    "the complete native body identity set is not stable",
                    {
                        "component": component,
                        "body_type": body_type,
                        "body_index": row["body_index"],
                        "body_name": row["body_name"],
                        "first_reference_sha256": row["reference"][
                            "raw_sha256"
                        ],
                        "replay_reference_sha256": replay["reference"][
                            "raw_sha256"
                        ],
                    },
                )
        raw_body_references = [
            row["reference"]["raw_sha256"] for row in body_rows
        ]
        if len(set(raw_body_references)) != len(raw_body_references):
            raise CadError(
                "cad_persistent_reference_alias",
                "a persistent body identity is not unique",
                {
                    "component": component,
                    "body_type": body_type,
                    "reference_sha256": raw_body_references,
                },
            )

        for row in body_rows:
            body = row["body"]
            body_index = row["body_index"]
            body_name = row["body_name"]
            faces = row["faces"]
            context = {
                "component": component,
                "body_type": body_type,
                "body_index": body_index,
                "completed_faces": completed_faces,
            }

            def scope_witnesses(
                component=component,
                body_type=body_type,
                body_index=body_index,
                body=body,
                faces=faces,
            ):
                return self._scope_witnesses_for_body(
                    component,
                    body_type,
                    body_index,
                    body,
                    faces,
                    occurrence_extension=extension,
                )

            graph = self._capture_body_tessellation(
                body,
                extension,
                component=component,
                body_type=body_type,
                body_index=body_index,
                reference_registry=registry,
                scope_context=scope_context,
                source_scope=self._component_source_scope(component),
                scope_witnesses=scope_witnesses,
                body_name=body_name,
                body_reference={
                    **row["reference"],
                },
                body_faces=faces,
                body_edges=row["edges"],
                body_owner_prebound=True,
                body_owner_pointer=row["owner_pointer"],
            )
            for key, value in graph["u_observation_stats"].items():
                evidence["u_observation_stats"][key] += value
            body_triangle_start = len(values) // 9
            triangle_records = graph["triangle_records"]
            records_by_face = {}
            for record in triangle_records:
                records_by_face.setdefault(record["face_index"], []).append(record)
            facet_ids_by_face = {
                row["face_index"]: row["facet_ids"]
                for row in graph["itessellation_body"]["faces"]
            }
            for face_row in graph["faces"]:
                face_index = face_row["index"]
                records = records_by_face.get(face_index, [])
                records.sort(key=lambda record: record["triangle_record_index"])
                face_values = [
                    coordinate
                    for record in records
                    for point in record["native_binary64_points"]
                    for coordinate in point
                ]
                face_facets = facet_ids_by_face[face_index]
                if not face_values:
                    face = _dynamic(faces[face_index])
                    try:
                        native_count = _member(face, "GetTessTriangleCount")
                        area = _member(face, "GetArea")
                    except Exception:  # noqa: BLE001
                        native_count = area = None
                    if (
                        body_type != 1
                        or face_facets
                        or type(native_count) is not int
                        or native_count != 0
                        or type(area) not in (int, float)
                        or not math.isfinite(area)
                        or area < 0.0
                    ):
                        raise CadError(
                            "cad_mesh_export_failed",
                            "invalid face tessellation",
                            {
                                **context,
                                "face_index": face_index,
                                "triangle_count": native_count,
                                "area_m2": area,
                                "facet_count": len(face_facets),
                                "classification": "empty",
                            },
                        )
                    evidence["no_facets_faces"].append(
                        {
                            "body_type": body_type,
                            "body_index": body_index,
                            "face_index": face_index,
                            "triangle_count": 0,
                            "facet_count": 0,
                            "area_m2": float(area),
                            "tessellate_result": graph["tessellate_result"],
                            "used_api": FACETLESS_EVIDENCE_API,
                        }
                    )
                edge_references = sorted(
                    edge["edge_reference_sha256"]
                    for edge in graph["native_edge_rows"]
                    if face_index in edge["face_indices"]
                )
                evidence["face_spans"].append(
                    {
                        "body_type": body_type,
                        "body_index": body_index,
                        "body_name": graph["body_name"],
                        "body_reference_sha256": graph["body_reference"][
                            "typed_sha256"
                        ],
                        "native_body_reference_sha256": graph[
                            "body_reference"
                        ]["raw_sha256"],
                        "face_index": face_index,
                        "face_reference_sha256": face_row["reference"][
                            "typed_sha256"
                        ],
                        "native_face_reference_sha256": face_row["reference"][
                            "raw_sha256"
                        ],
                        "face_identity_mode": (
                            NATIVE_SCOPE_IDENTITY_RULE
                            if face_row.get("identity") is not None
                            else NATIVE_PERSISTENT_IDENTITY_RULE
                        ),
                        "face_identity": face_row.get("identity"),
                        "face_owner_id": None,
                        "edge_references": edge_references,
                        "triangle_start": len(values) // 9,
                        "triangle_count": len(records),
                        "triangle_records_sha256": (
                            None
                            if records
                            else hashlib.sha256(b"").hexdigest()
                        ),
                    }
                )
                values.extend(face_values)
                completed_faces += 1
                evidence["faces"] += 1
            evidence["native_edges"].extend(graph["native_edge_rows"])
            itessellation_body = graph["itessellation_body"]
            for facet in itessellation_body["facets"]:
                facet["triangle_record_index"] += body_triangle_start
            evidence["itessellation_bodies"].append(itessellation_body)
            evidence["itessellation_vertices"].extend(graph["vertex_rows"])
            evidence["solidworks_vertex_identities"].extend(
                graph["solidworks_vertex_identities"]
            )
        evidence["native_edges"].sort(
            key=lambda row: (
                row["body_type"],
                row["body_index"],
                row["edge_reference_sha256"],
            )
        )
        evidence["itessellation_bodies"].sort(
            key=lambda row: (row["body_type"], row["body_index"])
        )
        evidence["itessellation_vertices"].sort(
            key=lambda row: (
                row["body_type"],
                row["body_index"],
                row["vertex_id"],
            )
        )
        evidence["reference_alias_audit"] = {
            kind: list(references)
            for kind, references in registry.alias_audit().items()
        }
        evidence["cross_kind_raw_aliases"] = list(
            registry.cross_kind_aliases()
        )
        return values, evidence

    def export_component_meshes(self, destinations, on_activity=None):
        """Keep one live assembly traversal through the entire geometry batch.

        IComponent2 bodies follow the occurrence's referenced configuration;
        the shared part document may have another configuration active.
        """
        activity = _ActivityEmitter(on_activity)

        def note(action: str, **fields) -> None:
            activity.emit(action, phase="capture", **fields)

        missing = sorted(set(destinations) - self._components)
        if missing:
            raise CadError("cad_missing_component", missing[0])
        if len({os.path.normcase(os.path.abspath(path)) for path in destinations.values()}) != len(destinations):
            raise CadError("cad_mesh_export_failed", "each occurrence needs a distinct mesh destination")
        with self._current_components("geometry") as current:
            entries = {}
            total = len(destinations)
            for index, component in enumerate(sorted(destinations), start=1):
                note(
                    "mesh_export",
                    current_object=_activity_object(component),
                    completed=index - 1,
                    total=total,
                )
                dest_path = destinations[component]
                try:
                    entries[component] = self._write_component_mesh(component, dest_path, current[component])
                except CadError as error:
                    detail = error.detail if isinstance(error.detail, dict) else {"native_detail": error.detail}
                    error.detail = {**detail, "component": component, "completed_components": list(entries)}
                    raise
                note(
                    "mesh_export",
                    current_object=_activity_object(component),
                    completed=index,
                    total=total,
                )
            # The 0.25 s repeat throttle may drop the last per-component notes on a
            # fast batch; one forced final note keeps the accepted activity at n/N.
            note("mesh_export", completed=total, total=total, force=True)
            return entries

    def _component_source_scope(self, component):
        """Bind the occurrence's exact source identity and virtual owner path."""

        override = self._source_scope_overrides.get(component)
        if override is not None:
            return dict(override)
        configured = self._source_components.get(component)
        if configured is None:
            return None
        source_entry = self._source_documents.get(configured[0])
        if source_entry is None:
            return None
        source_path = normalize_document_path(
            os.path.abspath(source_entry[0])
        )
        provenance = self._source_provenance.get(component) or {}
        state = provenance.get("state")
        if (
            state == "embedded_virtual_component"
            and provenance.get("is_virtual") is True
            and provenance.get("kind")
            == "native_embedded_virtual_component"
            and embedded_virtual_component_path(source_path)
        ):
            source_sha256 = None
            identity_sha256 = hashlib.sha256(
                source_path.encode("utf-8")
            ).hexdigest()
            identity_kind = "virtual_path_sha256"
            source_copy_path = None
            virtual_owner = provenance.get("owner")
            virtual_owner_sha256 = provenance.get("owner_sha256")
            virtual_session_document_path = provenance.get(
                "session_document_path"
            )
            virtual_session_temp_root = provenance.get(
                "session_temp_root"
            )
            virtual_native_api_pid = provenance.get("native_api_pid")
        elif state is None:
            if not os.path.isfile(source_entry[0]):
                return None
            if (
                embedded_virtual_component_marker(source_path)
                or session_materialization_path(source_path)
            ):
                return None
            source_sha256 = _hash(source_entry[0])
            identity_sha256 = source_sha256
            identity_kind = "file_sha256"
            relative_source = (
                _relative_document(
                    source_entry[0],
                    self._scene_source_root,
                )
                if self._scene_source_root is not None
                else Path(source_entry[0]).name
            )
            if not relative_source:
                return None
            source_copy_path = f"source/{relative_source}"
            virtual_owner = None
            virtual_owner_sha256 = None
            virtual_session_document_path = None
            virtual_session_temp_root = None
            virtual_native_api_pid = None
        else:
            return None
        return {
            "rule_version": NATIVE_SOURCE_SCOPE_RULE,
            "component": component,
            "source_document_path": source_path,
            "source_copy_path": source_copy_path,
            "source_document_sha256": source_sha256,
            "source_copy_sha256": source_sha256,
            "source_document_identity_sha256": identity_sha256,
            "source_document_identity_kind": identity_kind,
            "referenced_configuration": configured[1],
            "occurrence_scope": component,
            "virtual_owner": virtual_owner,
            "virtual_owner_sha256": virtual_owner_sha256,
            "virtual_session_document_path": (
                normalize_document_path(str(virtual_session_document_path))
                if virtual_session_document_path
                else None
            ),
            "virtual_session_temp_root": virtual_session_temp_root,
            "virtual_native_api_pid": virtual_native_api_pid,
        }

    def source_scopes(self, components):
        """Return the exact immutable source scope for each requested occurrence."""

        scopes = {}
        for component in sorted(components):
            scope = self._component_source_scope(component)
            if scope is None:
                raise CadError(
                    "cad_source_scope_missing",
                    "a component has no exact source scope",
                    {"component": component},
                )
            scopes[component] = scope
        return scopes

    def _write_component_mesh(self, component, dest_path, holder):
        """Write the occurrence's solids and sheets within its owning geometry phase."""
        sources: list[str] = []
        values: list[float] = []
        body_counts = {"solid": 0, "sheet": 0}
        reference_registry = _NativeReferenceRegistry()
        tessellation = {
            "schema_version": NATIVE_TESSELLATION_SCHEMA,
            "faces": 0,
            "face_spans": [],
            "native_edges": [],
            "no_facets_faces": [],
            "itessellation": {
                "interface_iid": ITESSELLATION_IID,
                "u_rule_version": NATIVE_U_IDENTITY_RULE,
                "u_resolution_rule": NATIVE_U_RESOLUTION_RULE,
                "point_roundtrip_rule": NATIVE_POINT_ROUNDTRIP_RULE,
                "properties": {
                    "match_type": 0,
                    "need_face_facet_map": True,
                    "need_edge_fin_map": True,
                    "need_vertex_params": True,
                },
                "identity_rule": NATIVE_IDENTITY_POLICY_RULE,
                "bodies": [],
                "vertices": [],
                "solidworks_vertex_identities": [],
                "reference_alias_audit": {
                    "body": [],
                    "face": [],
                    "edge": [],
                    "vertex": [],
                },
                "cross_kind_raw_aliases": [],
                "u_observation_stats": {
                    "unique_observations": 0,
                    "cache_hits": 0,
                    "get_closest_point_on_calls": 0,
                    "evaluate2_calls": 0,
                },
            },
        }
        for body_type, kind, source in (
            (0, "solid", "solid_itessellation"),
            (1, "sheet", "sheet_itessellation"),
        ):
            bodies = self._body_list(holder, body_type, component)
            body_counts[kind] = len(bodies)
            if bodies:
                body_values, body_evidence = self._body_face_triangles(
                    bodies,
                    body_type,
                    component,
                    reference_registry,
                    body_holder=holder,
                )
                triangle_offset = len(values) // 9
                values.extend(body_values)
                sources.append(source)
                tessellation["faces"] += body_evidence["faces"]
                tessellation["native_edges"].extend(body_evidence["native_edges"])
                for body_record in body_evidence["itessellation_bodies"]:
                    for facet in body_record["facets"]:
                        facet["triangle_record_index"] += triangle_offset
                tessellation["itessellation"]["bodies"].extend(
                    body_evidence["itessellation_bodies"]
                )
                tessellation["itessellation"]["vertices"].extend(
                    body_evidence["itessellation_vertices"]
                )
                tessellation["itessellation"][
                    "solidworks_vertex_identities"
                ].extend(
                    body_evidence["solidworks_vertex_identities"]
                )
                for key, value in body_evidence["u_observation_stats"].items():
                    tessellation["itessellation"]["u_observation_stats"][key] += value
                for span in body_evidence["face_spans"]:
                    tessellation["face_spans"].append(
                        {
                            **span,
                            "triangle_start": span["triangle_start"] + triangle_offset,
                        }
                    )
                tessellation["no_facets_faces"].extend(body_evidence["no_facets_faces"])
        tessellation["itessellation"]["reference_alias_audit"] = {
            kind: list(references)
            for kind, references in reference_registry.alias_audit().items()
        }
        tessellation["itessellation"]["cross_kind_raw_aliases"] = list(
            reference_registry.cross_kind_aliases()
        )
        if len(tessellation["face_spans"]) > 0xFFFF:
            raise CadError(
                "cad_mesh_export_failed",
                "the component carries more face owners than a binary STL attribute can bind",
                {"component": component, "faces": len(tessellation["face_spans"])},
            )
        triangle_owners: list[int] = []
        for owner_id, span in enumerate(tessellation["face_spans"], start=1):
            span["face_owner_id"] = owner_id
            triangle_owners.extend([owner_id] * span["triangle_count"])
        if len(triangle_owners) != len(values) // 9:
            raise CadError(
                "cad_mesh_export_failed",
                "native face owners do not cover the complete triangle stream",
                {"component": component},
            )
        if not values:
            raise CadError(
                "cad_mesh_export_failed",
                "invalid tessellation",
                {
                    "component": component,
                    "solid_bodies": body_counts["solid"],
                    "sheet_bodies": body_counts["sheet"],
                },
            )
        if tessellation["no_facets_faces"]:
            self.notes["mesh_tessellation:" + component] = {
                "faces": tessellation["faces"],
                "no_facets_faces": tessellation["no_facets_faces"][:20],
            }
        # Native ITessellation facets, in metres, independent of global STL
        # preferences. No claim of machining-grade surface approximation.
        with open(dest_path, "xb") as handle:
            handle.write(b"SolidWorks ITessellation facets, metres".ljust(80, b" "))
            handle.write(struct.pack("<I", len(values) // 9))
            for triangle_index, offset in enumerate(range(0, len(values), 9)):
                xyz = values[offset : offset + 9]
                a, b, c = xyz[:3], xyz[3:6], xyz[6:9]
                u, v = [b[i] - a[i] for i in range(3)], [c[i] - a[i] for i in range(3)]
                n = (u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2], u[0] * v[1] - u[1] * v[0])
                size = math.sqrt(sum(t * t for t in n))
                if size == 0:
                    raise CadError("cad_mesh_degenerate", component)
                handle.write(
                    struct.pack(
                        "<12fH",
                        *(t / size for t in n),
                        *xyz,
                        triangle_owners[triangle_index],
                    )
                )
        self._fill_face_span_digests(dest_path, tessellation["face_spans"])
        # The count already written into the header is the writer's own
        # tessellation count; no reader pass is needed (the Linux verifier
        # parses the delivered bytes and owns all mesh quality checks).
        triangles = len(values) // 9
        api = (
            "ITessellation.Tessellate/GetFaceFacets/GetFacetFins/"
            "GetFinVertices/GetVertexPoint"
        )
        self.notes["mesh:" + component] = api
        return {
            "component": component,
            "written": dest_path,
            "used_api": api,
            "triangles": triangles,
            "units": "m",
            "representation": "CAD_native_itessellation",
            "tessellation_sources": sources,
            "bodies": body_counts,
            "source_scope": self._component_source_scope(component),
            "tessellation": {
                "schema_version": tessellation["schema_version"],
                "faces": tessellation["faces"],
                "face_spans": tessellation["face_spans"],
                "native_edges": tessellation["native_edges"],
                "no_facets_faces": tessellation["no_facets_faces"],
                "itessellation": tessellation["itessellation"],
            },
        }

    def _fill_face_span_digests(self, path, spans: list[dict]) -> None:
        with open(path, "rb") as handle:
            for span in spans:
                start = int(span["triangle_start"])
                count = int(span["triangle_count"])
                handle.seek(84 + 50 * start)
                payload = handle.read(50 * count)
                if len(payload) != 50 * count:
                    raise CadError(
                        "cad_mesh_export_failed",
                        "a face span escapes the native STL record stream",
                        {
                            "path": str(path),
                            "body_type": span["body_type"],
                            "body_index": span["body_index"],
                            "face_index": span["face_index"],
                            "triangle_start": start,
                            "triangle_count": count,
                        },
                    )
                span["triangle_records_sha256"] = hashlib.sha256(payload).hexdigest()

    def _record_source_document(self, path, configuration, *, session_document=False):
        role = self._role_for_path(path)
        key = (role, normalize_document_path(os.path.abspath(path)))
        session = self._sessions.get(role)
        if session is None or session.closed:
            if session_document:
                raise CadError(
                    "cad_materialization_session_unavailable",
                    "the owning CAD session for a session artifact is not available",
                    {"path": str(path), "role": role},
                )
            raise CadError(
                "cad_session_unavailable",
                "the CAD session for a captured document is not available",
                {"path": str(path), "role": role},
            )
        if session_document:
            self._bind_session_document(role, session, path)
        previous = self._source_documents.setdefault(key, (path, role, configuration, session))
        if previous[2] != configuration or previous[3] is not session:
            raise CadError("cad_source_changed", "a shared document's captured state changed", {"path": path})
        return key

    def _captured_document(self, key, component="", phase="verify_sources"):
        """Resolve a recorded path in its original live session, without reopening."""
        if key not in self._source_documents:
            raise CadError(
                "cad_source_state_unreadable",
                "no captured document identity is available",
                {"component": component, "phase": phase},
            )
        path, role, expected, session = self._source_documents[key]
        try:
            if (
                self._owner_thread is not threading.current_thread()
                or self._role_for_path(path) != role
                or self._sessions.get(role) is not session
                or session.app is None
                or not session.process.alive()
                or self._cancelled.is_set()
            ):
                raise ValueError("the original owned capture session is unavailable")
            doc = self._document_by_path(path)
            configuration = _active_configuration(doc)
            if not _is_text_name(configuration):
                raise ValueError("the document has no readable active configuration")
        except Exception as error:
            raise CadError(
                "cad_source_state_unreadable",
                "native source document could not be read",
                {"path": path, "component": component, "phase": phase, "error": str(error)},
            ) from error
        if configuration != expected:
            raise CadError(
                "cad_source_changed",
                "document configuration changed during export",
                {"path": path, "before": {"configuration": expected}, "after": {"configuration": configuration}},
            )
        return doc

    @contextmanager
    def _current_components(self, phase):
        """Keep native parent interfaces alive until the complete read phase finishes."""
        doc = self._captured_document(self._scene_document_key, phase=phase)
        path = self._source_documents[self._scene_document_key][0]
        name = ""
        current = {}
        try:
            _manager, config = _active_configuration_view(doc)
            root = _component(_member(config, "GetRootComponent3", True))
            if root is None:
                raise ValueError("the assembly has no readable root component")
            stack = list(_method(root, "GetChildren") or ())
            while stack:
                name = ""
                comp = _component(stack.pop())
                name = _member(comp, "Name2")
                if not _is_text_name(name):
                    raise ValueError("the occurrence has no readable full name")
                state = _component_state(comp)
                if state == COMPONENT_SUPPRESSED:
                    continue
                if phase == "geometry" and state in (COMPONENT_LIGHTWEIGHT, COMPONENT_FULLY_LIGHTWEIGHT):
                    # Geometry capture needs loaded bodies; a lightweight
                    # occurrence must be refused instead of exporting nothing.
                    raise CadError(
                        "cad_component_lightweight_unresolved",
                        "a lightweight occurrence cannot be measured; resolve it before capture",
                        {"component": name, "state": state, "phase": phase},
                    )
                if name in current or name not in self._source_components:
                    raise CadError(
                        "cad_source_changed", "the active occurrence identities changed", {"component": name}
                    )
                document_key, referenced = self._source_components[name]
                expected_path = self._source_documents[document_key][0]
                occurrence = {
                    "path": _method(comp, "GetPathName"),
                    "referenced_configuration": _member(comp, "ReferencedConfiguration"),
                }
                if not all(_is_text_name(value) for value in occurrence.values()):
                    raise ValueError("the occurrence path or referenced configuration is unreadable")
                if (
                    not document_paths_match(occurrence["path"], expected_path)
                    or occurrence["referenced_configuration"] != referenced
                ):
                    raise CadError(
                        "cad_source_changed",
                        name,
                        {
                            "path": expected_path,
                            "before": {"referenced_configuration": referenced},
                            "after": occurrence,
                        },
                    )
                current[name] = comp
                stack.extend(_method(comp, "GetChildren") or ())
        except Exception as error:
            if isinstance(error, CadError) and error.code in {
                "cad_source_changed",
                "cad_component_internal_id_mismatch",
            }:
                raise
            raise CadError(
                "cad_source_state_unreadable",
                "native occurrence state could not be read",
                {"path": path, "component": name, "phase": phase, "error": str(error)},
            ) from error
        missing = sorted(self._source_components.keys() - current.keys())
        if missing:
            raise CadError("cad_source_changed", "active occurrences disappeared", {"missing": missing, "path": path})
        yield current

    def verify_sources_unchanged(self):
        if not self._source_documents:
            raise CadError("cad_source_state_unreadable", "no captured document identities are available")
        for key in self._source_documents:
            component = next((name for name, (doc_key, _) in self._source_components.items() if doc_key == key), "")
            self._captured_document(key, component)
        with self._current_components("verify_sources"):
            pass
        for path, digest in self.source_files.items():
            if normalize_document_path(os.path.abspath(path)) in self._session_document_paths:
                continue
            if _hash(path) != digest:
                raise CadError("cad_source_changed", path)
        # Source identity stays hashes only: the save flag is an observation and lives
        # in the manifest's raw/ and evidence/ files, not in this binding.
        return {path: {"sha256": digest} for path, digest in self.source_files.items()}

    # -- freeze support ---------------------------------------------------

    def environment(self):
        """Build and licence facts a snapshot must record to be reproducible."""

        app = self._app_obj()
        facts = {
            "revision": _member(app, "RevisionNumber"),
            "input_mode": "saved_documents",
            "sessions": {role: session.identity() for role, session in self._sessions.items()},
        }
        for key, member in (("build", "GetBuildNumbers"), ("license", "GetCurrentLicenseType")):
            try:
                value = _member(app, member)
            except Exception as exc:  # noqa: BLE001 - optional API surface
                facts[key] = None
                facts.setdefault("unavailable", {})[member] = str(exc)
                continue
            valid = (type(value) is str and bool(value)) if key == "build" else type(value) is int
            if not valid:
                raise CadError(
                    "cad_environment_value_invalid",
                    f"{member} returned an unsupported type",
                    {"member": member, "type": type(value).__name__, "value": repr(value)},
                )
            facts[key] = value
        try:
            visible = _member(app, "Visible")
        except Exception:  # noqa: BLE001 - unreadable visibility remains an explicit failure placeholder
            facts["visible"] = None
        else:
            if type(visible) is not bool:
                raise CadError(
                    "cad_environment_value_invalid",
                    "Visible returned an unsupported type",
                    {"member": "Visible", "type": type(visible).__name__, "value": repr(visible)},
                )
            facts["visible"] = visible
        return facts

    def license_type(self):
        try:
            return int(_member(self._app_obj(), "GetCurrentLicenseType"))
        except Exception:  # noqa: BLE001 - reported as "unknown" instead of failing the probe
            return None

    def _ensure_document(self, path):
        """Return an open document, opening it read-only when needed."""

        if not path:
            return self._active_document()
        try:
            return self._document_by_path(path)
        except CadError as exc:
            if exc.code != "document_not_open":
                raise
        self.open_document(path)
        return self._document_by_path(path)

    def list_configurations(self, path):
        doc = self._ensure_document(path)
        names = []
        for item in _as_list(_member(doc, "GetConfigurationNames")):
            name = item if isinstance(item, str) else _member(_dynamic(item), "Name")
            if name:
                names.append(str(name))
        return names

    def _record_save_flag(self, doc, path):
        """Record what SolidWorks says about saving; never decide a capture by it.

        ``IModelDoc2::GetSaveFlag`` answers "would SolidWorks prompt me to save this
        document?".  The API reference says many operations set it and that a
        document created by an older SOLIDWORKS release starts with it set, so it is
        not evidence about anybody's edits.  It cannot be evidence about the
        operator's session either: this adapter opens the revision in an application
        it owns, copies the bytes and hashes them, so the flag only describes
        bookkeeping inside that owned session.  Keep it for the snapshot and let the
        caller show it, but never refuse a revision because of it.
        """

        document = _member(doc, "GetPathName") or path
        self.save_flags[normalize_document_path(str(document))] = bool(_member(doc, "GetSaveFlag"))

    def save_flag_documents(self):
        """Working-tree documents SolidWorks reported as needing a save."""

        return sorted(path for path, flagged in self.save_flags.items() if flagged)

    def document_state(self, path):
        """Saved state plus configuration facts; never saves anything itself.

        ``saved`` is SolidWorks' own answer (``not GetSaveFlag``): record it, but do
        not read it as "the operator has unsaved edits" - see _record_save_flag.
        """

        doc = self._ensure_document(path)
        _manager, active = _active_configuration_view(doc)
        save_flag = _member(doc, "GetSaveFlag")
        if type(save_flag) is not bool:
            raise CadError(
                "cad_save_flag_invalid",
                "GetSaveFlag returned an unsupported type",
                {"member": "GetSaveFlag", "type": type(save_flag).__name__, "value": repr(save_flag)},
            )
        state = {
            "path": _member(doc, "GetPathName"),
            "title": _member(doc, "GetTitle"),
            "save_flag": save_flag,
            "saved": not save_flag,
            "active_configuration": _member(active, "Name"),
            "configurations": self.list_configurations(path),
        }
        try:
            state["read_only"] = bool(_member(doc, "IsOpenedReadOnly"))
        except Exception:  # noqa: BLE001 - unreadable read-only state stays unknown
            state["read_only"] = None
        # The installed SDK declares no document lightweight property; record an
        # explicit unsupported diagnostic instead of probing undeclared spellings.
        state["lightweight"] = None
        state["unsupported"] = ["lightweight"]
        return state

    def list_dependencies(self, path):
        """Files this document references, as SolidWorks currently resolves them."""

        doc = self._ensure_document(path)
        document_path = _member(doc, "GetPathName") or path
        raw = _member(doc, "GetDependencies2", True, True, False)
        unique = normalise_dependency_entries(raw, document_path)
        return [
            {"path": entry, "exists": os.path.isfile(entry), "type": os.path.splitext(entry)[1].lower()}
            for entry in unique
        ]

    def resolve_dependencies(self, path):
        """Where the references of ``path`` actually resolve, after opening it.

        Used to prove a collected copy resolves inside the snapshot: the paths
        returned here are what SolidWorks would load, not what the copy intended.
        """

        return self.list_dependencies(path)

    def collect_dependencies(
        self,
        path,
        destination_dir,
        *,
        skip_missing=(),
        excluded_paths=(),
        required_names=None,
        admitted_roots=(),
        closure_documents=None,
        required_paths=(),
    ):
        """Copy saved native files and rewrite references in a second application.

        ReplaceReferencedDocument requires unopened copies with the original
        internal IDs. Copy bytes, retire the source, rewrite every direct
        reference in a fresh application, then independently inspect the copy.
        ``skip_missing`` lists absolute paths whose absence is already recorded
        (suppressed placements): they are reported instead of failing the
        collection, and they never enter the copied graph.  ``excluded_paths``
        contains exact admitted session-document paths and stale author source
        links; they are likewise reported and never copied or rewritten.

        ``required_names`` (the sealed closure's selected canonical document
        names), ``admitted_roots`` and ``closure_documents`` bind
        missing-reference policy to the sealed acquisition: a stored reference
        that is missing on disk is admissible stale metadata only when it is
        not a selected document, the scanning document has closure evidence,
        and the verbatim stored location is recorded in that document's
        ``stored_references`` without a resolved edge for it.  Exact
        containment and verbatim strings only -- never a basename.  Everything
        else still refuses ``dependency_missing``.

        ``required_paths`` are exact proven source paths (the freeze stage's
        ``original_files``): any required path the saved-reference walk did not
        reach -- e.g. a retained native import source leaf -- is copied
        byte-for-byte as a leaf under the mirrored layout without creating any
        synthetic native graph edge.  Exact paths only; a missing or
        out-of-root required path refuses.
        """
        source_path = str(Path(path).resolve())
        source_app = self._app_for_path(source_path)
        skipped_normalized = {os.path.normcase(os.path.abspath(str(item))) for item in skip_missing}
        excluded_normalized = {os.path.normcase(os.path.abspath(str(item))) for item in excluded_paths}
        required_folded = (
            {str(name).replace("\\", "/").casefold(): str(name) for name in required_names}
            if required_names is not None
            else None
        )
        roots_normalized = [os.path.normcase(os.path.abspath(str(item))) for item in admitted_roots]
        closure_docs_folded: dict[str, dict] = {}
        if isinstance(closure_documents, dict):
            for name, evidence in closure_documents.items():
                if not isinstance(evidence, dict):
                    continue
                closure_docs_folded[str(name).casefold()] = {
                    "stored_locations": [
                        str(item) for item in evidence.get("stored_locations") or [] if isinstance(item, str)
                    ],
                    "resolved_targets": {
                        str(item).casefold() for item in evidence.get("resolved_targets") or [] if isinstance(item, str)
                    },
                    "resolved_paths": {
                        os.path.normcase(os.path.abspath(str(item)))
                        for item in evidence.get("resolved_paths") or []
                        if isinstance(item, str) and item
                    },
                }
        graph_skipped: list[str] = []
        graph_excluded: list[str] = []
        graph_stale: list[dict] = []
        graph = {}
        pending = [source_path]
        while pending:
            document = pending.pop()
            if document in graph:
                continue
            if not Path(document).is_file():
                raise CadError("dependency_missing", document)
            raw = list(_method(source_app, "GetDocumentDependencies2", document, False, True, False) or ())
            if len(raw) % 2:
                raise CadError("dependency_list_invalid", document, {"entries": raw})
            references = []
            for value in raw[1::2]:
                if not isinstance(value, str) or not value:
                    raise CadError("dependency_list_invalid", document)
                reference = str(Path(resolve_dependency_path(value, document)).resolve())
                if os.path.normcase(os.path.abspath(reference)) in excluded_normalized:
                    graph_excluded.append(reference)
                    continue
                if required_folded is not None:
                    normalized_ref = os.path.normcase(os.path.abspath(reference))
                    contained = next(
                        (root for root in roots_normalized if normalized_ref.startswith(root + os.sep)),
                        None,
                    )
                    artifact_canonical = (
                        os.path.relpath(normalized_ref, contained).replace("\\", "/") if contained is not None else None
                    )
                    selected = artifact_canonical is not None and artifact_canonical.casefold() in required_folded
                    document_normalized = os.path.normcase(os.path.abspath(document))
                    document_root = next(
                        (root for root in roots_normalized if document_normalized.startswith(root + os.sep)),
                        None,
                    )
                    document_canonical = (
                        os.path.relpath(document_normalized, document_root).replace("\\", "/")
                        if document_root is not None
                        else None
                    )
                    evidence = (
                        closure_docs_folded.get(document_canonical.casefold())
                        if isinstance(document_canonical, str)
                        else None
                    )
                    if not selected and evidence is not None:
                        recorded = value in evidence["stored_locations"]
                        resolved_hit = (
                            artifact_canonical is not None
                            and artifact_canonical.casefold() in evidence["resolved_targets"]
                        ) or os.path.normcase(os.path.abspath(reference)) in evidence["resolved_paths"]
                        if recorded and not resolved_hit:
                            # Evidence-covered UNUSED stored reference: the
                            # closure recorded exactly this verbatim location
                            # for this document, resolved no active edge to it
                            # and did not select it.  It is never ingested,
                            # copied or rewritten -- whether or not the stale
                            # artifact still exists out of closure.
                            graph_stale.append(
                                {
                                    "document": document_canonical or str(document),
                                    "location": value,
                                    "resolved_path": reference,
                                    "exists": os.path.isfile(reference),
                                }
                            )
                            continue
                if not Path(reference).is_file():
                    if os.path.normcase(os.path.abspath(reference)) in skipped_normalized:
                        graph_skipped.append(reference)
                        continue
                    raise CadError("dependency_missing", reference, {"document": document})
                references.append(reference)
            graph[document] = sorted(set(references))
            pending.extend(graph[document])

        destination = Path(destination_dir).resolve()
        if destination.exists() and any(destination.iterdir()):
            raise CadError("dependency_destination_not_empty", str(destination))
        destination.mkdir(parents=True, exist_ok=True)
        seeded_leaves: list[str] = []
        # Preserve filenames and relative layout, including dependencies outside
        # the assembly directory. Distinct volumes get separate directories.
        volumes: dict[str, list[str]] = {}
        for document in graph:
            volumes.setdefault(Path(document).anchor, []).append(document)
        mapping = {}
        for anchor, documents in volumes.items():
            common = Path(os.path.commonpath([str(Path(item).parent) for item in documents]))
            base = destination
            if len(volumes) > 1:
                base /= "volume-" + hashlib.sha256(anchor.encode()).hexdigest()[:16]
            for document in documents:
                copied = base / Path(document).relative_to(common)
                if copied == Path(document):
                    raise CadError("dependency_destination_conflict", document)
                copied.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(document, copied)
                mapping[document] = str(copied)

        # Seed the exact proven required paths the saved-reference walk cannot
        # reach (a retained native import source materializes as a session temp
        # document, so the closed-document raw query never exposes it).  The
        # leaf is copied byte-for-byte under the mirrored layout; it never
        # creates a synthetic native graph edge and is never rewritten.
        covered_normalized = {os.path.normcase(os.path.abspath(str(item))) for item in mapping}
        for required in required_paths:
            normalized_required = os.path.normcase(os.path.abspath(str(required)))
            if normalized_required in covered_normalized:
                continue
            required_file = Path(str(required))
            if not required_file.is_file():
                raise CadError("dependency_missing", str(required), {"required": True})
            containing = next(
                (root for root in roots_normalized if normalized_required.startswith(root + os.sep)),
                None,
            )
            if containing is None:
                raise CadError(
                    "dependency_missing", str(required), {"required": True, "reason": "outside admitted roots"}
                )
            mirrored = destination / Path(os.path.relpath(required_file, containing))
            mirrored.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(required_file, mirrored)
            mapping[str(required_file)] = str(mirrored)
            seeded_leaves.append(str(required_file))
            covered_normalized.add(normalized_required)

        top = mapping[source_path]
        self._capture_roots.append(normalize_document_path(str(destination)))
        self._requested_configurations[normalize_document_path(top)] = self._requested_configurations.get(
            normalize_document_path(source_path), ""
        )
        del source_app
        if self._staged_search_root is not None:
            # The copy must resolve inside the snapshot it now lives in, so its
            # session search scope is re-pinned to the snapshot directory; the
            # source session keeps the pin it received at creation.
            self.configure_staged_search(destination)
        copy_app = self._app_for_path(top)
        replaced = 0
        for document, references in graph.items():
            for reference in references:
                if not _method(copy_app, "ReplaceReferencedDocument", mapping[document], reference, mapping[reference]):
                    raise CadError(
                        "dependency_rewrite_failed", document, {"reference": reference, "copy": mapping[document]}
                    )
                replaced += 1
        return {
            "method": "native_reference_copy",
            "source_document": source_path,
            "top_level": top,
            "destination": str(destination),
            "files": sorted(mapping.values()),
            "mapping": mapping,
            "reference_edges": replaced,
            "skipped_missing": sorted(graph_skipped),
            "skipped_excluded": sorted(graph_excluded),
            "seeded_required_leaves": sorted(seeded_leaves),
            "skipped_stale": sorted(
                graph_stale, key=lambda item: (str(item.get("document")), str(item.get("location")))
            ),
            "configured": ["GetDocumentDependencies2", "ReplaceReferencedDocument"],
        }

    def closure_readings(self, stage_root, main_assembly, index):
        """Raw native readings for the staged closure walk (mechanical only).

        Pins the session search scope to ``stage_root``, seals the staged
        inventory, opens the staged main read-only and records every
        occurrence's identity, referenced configuration, raw suppression
        state, resolved document path and virtual/import readings, plus
        pre-open query-A stored references over every staged native document.
        The session search scope is restored before return and both the
        restoration outcome and the per-session pinning evidence are recorded.
        State 5 and unreadable/invalid states are recorded, not raised:
        admission policy (``native_selection``) refuses with the exact
        instance.  Nothing is saved, moved or exported.
        """

        stage = Path(stage_root).resolve()
        names = sorted(str(name) for name in index)
        self.configure_staged_search(stage)
        sealed = self.seal_documents([str(stage / name) for name in names])
        readings: dict = {"stage_root": str(stage), "main_assembly": str(main_assembly), "sealed": sealed}
        session_info: dict = {}
        pinned: dict = {}
        restored_folders = None
        restored_raw = None
        restore_error = None
        with self.session():
            app = self._app_obj()
            session_info = {
                "native_api_pid": int(_method(app, "GetProcessID")),
                "solidworks_revision": str(_member(app, "RevisionNumber")),
            }
            evidence = self.staged_search_evidence()
            pinned = dict(evidence[-1]) if evidence else {}
            try:
                # Query A over every staged native document BEFORE any document is
                # opened: once an assembly is open, a false-Searchflag query reports
                # the in-session resolution rather than the saved author references.
                # The pre-open result is the verbatim stored-reference evidence.
                stored = {}
                for name in names:
                    if not name.lower().endswith((".sldasm", ".sldprt")):
                        continue
                    try:
                        entries = closed_document_dependencies(app, str(stage / name))
                        stored[name] = [
                            {"name": item.get("name"), "location": item.get("location")} for item in entries
                        ]
                    except Exception as error:  # noqa: BLE001 - recorded; closure policy refuses elsewhere
                        stored[name] = {"error": f"{type(error).__name__}: {error}"[:200]}
                readings["stored_references"] = stored
                readings["stored_references_phase"] = "preopen"
                readings["stored_references_count"] = len(stored)
                readings["documents_before"] = int(_member(app, "GetDocumentCount"))
                entry = self._probe_document(str(stage / main_assembly))
                readings["open"] = {
                    "already_open": entry.get("already_open"),
                    "open_exception": entry.get("open_exception"),
                    "errors": entry.get("errors"),
                    "warnings": entry.get("warnings"),
                }
                doc = entry.get("doc")
                if doc is None:
                    raise CadError(
                        "cad_staged_main_unopenable",
                        "the staged main assembly could not be opened read-only in the owned session",
                        {"main_assembly": str(main_assembly), "open": readings["open"]},
                    )
                document_state_before = _discovery_document_state(doc)
                _manager, configuration = _active_configuration_view(doc)
                # CU pre-verification input: the main document's declared delivery
                # identity (dp.*), read-only with the primitive configuration name.
                # An unreadable declaration set is recorded, never raised; the
                # admission policy (native_selection) refuses it fail-closed.
                try:
                    _context_manager, _context_configuration, configuration_name = _configuration_context(doc)
                    readings["configuration_name_error"] = None
                except CadError as error:
                    configuration_name = ""
                    readings["configuration_name_error"] = f"{type(error).__name__}: {error}"[:200]
                try:
                    identity_properties = _custom_properties(doc, configuration_name)
                except Exception as error:  # noqa: BLE001 - recorded; admission refuses unreadable declarations
                    identity_properties = None
                    readings["identity_properties_error"] = f"{type(error).__name__}: {error}"[:200]
                else:
                    readings["identity_properties_error"] = None
                readings["identity_properties"] = identity_properties
                # Delivery-configuration evidence in the same shape as the native
                # discovery record: the actual saved configuration name plus the
                # before/after saved-state observations of the opened document.
                # Persisted as read - unknowns and read errors stay explicit.
                # The "after" snapshot is taken when the native walk below has
                # completed and before the document is closed.
                readings["saved_configuration"] = str(configuration_name or "") or None
                root = _component(_member(configuration, "GetRootComponent3", True))
                occurrences = []
                stack = [
                    (child, "", 0, str(main_assembly), None, str(main_assembly))
                    for child in reversed(_as_list(_method(root, "GetChildren")))
                ]
                while stack:
                    item, context, depth, defining, anchor, native_owner = stack.pop()
                    if depth > 40:
                        raise CadError("cad_assembly_too_deep", str(stage / main_assembly), {"depth": depth})
                    component = _component(item)
                    name = str(_member(component, "Name2") or _member(component, "Name") or "")
                    instance = name if not context or name.startswith(context + "/") else f"{context}/{name}"
                    record = {
                        "instance": instance,
                        "name": name,
                        "depth": depth,
                        "defining": defining,
                        "defining_anchor": anchor,
                        "native_owner": native_owner,
                    }
                    referenced = _member(component, "ReferencedConfiguration")
                    record["configuration"] = str(referenced) if referenced is not None else None
                    try:
                        state = _method(component, "GetSuppression2")
                        record["suppression_state"] = int(state) if type(state) is int else None
                        if record["suppression_state"] is None:
                            record["state_error"] = f"non-integer reading: {state!r}"[:200]
                    except Exception as error:  # noqa: BLE001 - recorded for the policy refusal
                        record["suppression_state"] = None
                        record["state_error"] = f"{type(error).__name__}: {error}"[:200]
                    try:
                        record["resolved_path"] = str(_method(component, "GetPathName") or "")
                    except Exception as error:  # noqa: BLE001 - recorded for the policy refusal
                        record["resolved_path"] = ""
                        record["path_error"] = f"{type(error).__name__}: {error}"[:200]
                    virtual = None
                    try:
                        reading = _member(component, "IsVirtual")
                        virtual = reading if type(reading) is bool else None
                    except Exception:  # noqa: BLE001 - an unreadable virtual flag stays unknown
                        virtual = None
                    record["is_virtual"] = virtual
                    resolved = record["resolved_path"]
                    relative = _stage_relative(stage, resolved) if resolved else None
                    record["resolved_relative"] = relative
                    materialized = session_materialization_path(resolved) or (
                        virtual is True and embedded_virtual_component_path(resolved)
                    )
                    anchor_for_children = instance if materialized else anchor
                    child_owner = native_owner
                    if not materialized and relative:
                        child_owner = _index_spelling(index, relative) or native_owner
                    if (
                        (not resolved)
                        or relative is None
                        or virtual is True
                        or session_artifact_path(resolved)
                    ):
                        imported, import_error = _imported_path_reading(component)
                        record["imported_path"] = imported
                        if import_error is not None:
                            record["import_error"] = import_error
                    occurrences.append(record)
                    if record["suppression_state"] == COMPONENT_SUPPRESSED:
                        continue
                    child_defining = relative or resolved or defining
                    for child in reversed(_as_list(_method(component, "GetChildren"))):
                        stack.append((child, instance, depth + 1, child_defining, anchor_for_children, child_owner))
                readings["occurrences"] = occurrences
                readings["documents_after"] = int(_member(app, "GetDocumentCount"))
                readings["document_state"] = {
                    "before": document_state_before,
                    "after": _discovery_document_state(doc),
                    "used_api": (
                        "IModelDoc2.GetSaveFlag/IModelDocExtension.NeedsRebuild2/"
                        "IConfiguration.Name/IConfiguration.NeedsRebuild"
                    ),
                }
            finally:
                try:
                    _member(app, "SetSearchFolders", SEARCH_FOLDER_DOCUMENT_TYPE, ";".join(pinned.get("before") or []))
                    restored = read_search_folders(app, SEARCH_FOLDER_DOCUMENT_TYPE)
                    restored_folders = restored["folders"]
                    restored_raw = restored["raw"]
                except Exception as error:  # noqa: BLE001 - recorded, never masked
                    restore_error = f"{type(error).__name__}: {error}"[:300]
        before_folders = list(pinned.get("before") or [])
        restored_ok = restored_folders is not None and sorted(
            str(item).casefold() for item in restored_folders
        ) == sorted(str(item).casefold() for item in before_folders)
        readings["search"] = {
            "folder_type": pinned.get("folder_type", SEARCH_FOLDER_DOCUMENT_TYPE),
            "apis": ["ISldWorks.GetSearchFolders", "ISldWorks.SetSearchFolders"],
            "before": before_folders,
            "before_raw": pinned.get("before_raw"),
            "pinned": pinned.get("pinned", [str(stage)]),
            "observed_after_pin": pinned.get("observed_after_pin", []),
            "after_raw": pinned.get("after_raw"),
            "set_return": pinned.get("set_return"),
            "restored": restored_ok,
            "restored_raw": restored_raw,
            "restore_error": restore_error,
            "evidence": [dict(item) for item in self.staged_search_evidence()],
        }
        readings["session"] = {**session_info, "sealed": True, "closed": True}
        return readings

    def inspect_copy(self, assembly_path):
        """Open a document read-only and report every component *instance*.

        This is the evidence that a collected copy is the same assembly as the
        source: the active configuration of the top level, and for every instance
        its identity, the document it actually resolved to, its suppression state
        and the configuration of the referenced document that it uses.

        The walk is the one ``collect_scene`` already uses - the component tree of
        the active configuration, reached through ``GetRootComponent3`` and
        ``IComponent2::GetChildren`` - because recursing through model documents
        would lose the referenced-configuration context of each placement.

        Identity is the assembly instance path carried by ``Name2``
        (``module-1/part-1``), stitched with the parent context when a build
        reports the leaf alone.  ``IComponent2::GetPathName`` is the *document*
        path, not an instance path: it repeats for every placement of the same
        part, so it cannot key instances.
        """

        opened = self.open_document(assembly_path)
        doc = self._document_by_path(assembly_path)
        report_role = self._role_for_path(assembly_path)
        report_session = self._sessions.get(report_role)
        document_path = str(_member(doc, "GetPathName") or assembly_path)
        _manager, configuration = _active_configuration_view(doc)
        root = _component(_member(configuration, "GetRootComponent3", True))
        instances = []
        read_only_paths = set()

        stack = [(child, "", 0, document_path, None) for child in reversed(_as_list(_method(root, "GetChildren")))]
        while stack:
            item, context, depth, defining, anchor = stack.pop()
            if depth > 40:
                raise CadError("cad_assembly_too_deep", assembly_path, {"depth": depth})
            component = _component(item)
            name = str(_member(component, "Name2") or _member(component, "Name") or "")
            instance = name if not context or name.startswith(context + "/") else f"{context}/{name}"
            model = _component_document(component)
            document = _member(model, "GetPathName") if model is not None else None
            if not document:
                document = _method(component, "GetPathName")
            if model is not None and document:
                read_only_paths.add(str(document))
            referenced = _member(component, "ReferencedConfiguration")
            state = _component_state(component)
            suppressed = state == COMPONENT_SUPPRESSED
            virtual = None
            try:
                reading = _member(component, "IsVirtual")
                virtual = reading if type(reading) is bool else None
            except Exception:  # noqa: BLE001 - an unreadable virtual flag stays unknown
                virtual = None
            imported_path, imported_error = (
                _imported_path_reading(component)
                if (not document) or virtual is True or session_materialization_path(document)
                else (None, None)
            )
            ic_materialization = session_materialization_path(str(document))
            vc_materialization = embedded_virtual_component_path(str(document))
            if session_document_marker(str(document)) and not (ic_materialization or vc_materialization):
                raise CadError(
                    "cad_materialization_session_mismatch",
                    "a session document does not use one canonical controlled temporary-root layout",
                    {"instance": instance, "document": str(document)},
                )
            if virtual is True and not vc_materialization:
                raise CadError(
                    "cad_materialization_session_mismatch",
                    "a virtual occurrence does not resolve to a canonical VC~~ session document",
                    {"instance": instance, "document": str(document)},
                )
            if ic_materialization and virtual is not False:
                raise CadError(
                    "cad_materialization_session_mismatch",
                    "an IC~~ materialization requires an explicit False IsVirtual reading",
                    {"instance": instance, "document": str(document), "is_virtual": virtual},
                )
            materialized = ic_materialization or vc_materialization
            if materialized:
                self._bind_session_document(report_role, report_session, str(document))
            anchor_for_children = instance if materialized else anchor
            instances.append(
                {
                    "instance": instance,
                    "name": name,
                    "document": str(document) if document else None,
                    "document_name": ntpath.basename(str(document)).lower() if document else None,
                    "configuration": str(referenced) if referenced is not None else None,
                    "virtual": virtual,
                    "imported_path": imported_path,
                    "imported_error": imported_error,
                    "defining": defining,
                    "defining_anchor": anchor,
                    "suppressed": suppressed,
                    "suppression_state": state,
                    "depth": depth,
                }
            )
            if suppressed:
                # a suppressed placement loads no model, so its children are not
                # part of the resolved instance set of either side
                continue
            for child in reversed(_as_list(_method(component, "GetChildren"))):
                child_defining = str(document) if document else defining
                stack.append((child, instance, depth + 1, child_defining, anchor_for_children))

        # Finish borrowing occurrence interfaces before any native state change.
        component = item = child = model = root = configuration = doc = stack = None
        for path in sorted(read_only_paths):
            self._document_by_path(path)  # Reacquire and enforce read-only.

        return {
            "document": str(opened.get("path") or document_path),
            "configuration": _active_configuration(self._document_by_path(assembly_path)),
            "components": len(instances),
            # A suppressed instance is not evidence of an escape: it is recorded
            # separately and still has to match between source and copy.
            "unresolved": [
                entry["instance"] for entry in instances if not entry["document"] and not entry["suppressed"]
            ],
            "suppressed": [entry["instance"] for entry in instances if entry["suppressed"]],
            "instances": instances,
        }

    def _probe_document(self, path):
        """Open the selected assembly without strict gating.

        Documents already open in the owned session are reused.  Otherwise the
        read-only open is attempted and its CAD-reported errors are recorded
        instead of raised; the selected document is validated strictly before
        anything downstream reads it.  Only the exact vendor integer status is
        recorded: an unreadable VARIANT leaves the counts unset and blocks the
        selected root instead of coercing to zero.
        """

        try:
            doc = self._document_by_path(path)
        except CadError as exc:
            if exc.code != "document_not_open":
                raise
        else:
            return {"doc": doc, "errors": 0, "warnings": 0, "open_exception": None, "already_open": True}
        try:
            doc, errors, warnings = self._open_document_raw(path)
        except Exception as exc:  # noqa: BLE001 - a per-open failure is recorded, not fatal
            return {
                "doc": None,
                "errors": None,
                "warnings": None,
                "open_exception": f"{type(exc).__name__}: {exc}"[:300],
                "already_open": False,
            }
        if type(errors.value) is int and type(warnings.value) is int:
            counts = (errors.value, warnings.value)
        else:
            # An unreadable status VARIANT is not an observed zero; the
            # selected root is blocked later instead of coerced.
            counts = (None, None)
        return {
            "doc": doc,
            "errors": counts[0],
            "warnings": counts[1],
            "open_exception": None,
            "already_open": False,
        }

    def _main_entry(self, path):
        """The selected assembly opened read-only; per-open failures are recorded."""

        entry = self._probe_document(str(path))
        entry["path"] = path
        return entry

    def discover_native(self, frozen_source: Path, settings: dict, *, on_activity=None) -> dict:
        """Read the raw native record for CAD-only discovery (owned session).

        Opens the immutable engineering directory read-only inside this
        process's owned session and records the primitives discovery needs:
        identity properties, the component graph with occurrence transforms,
        mate features with component-frame entity geometry, coordinate systems
        (assembly and component scope), materials/masses and per-document
        hashes.  Every read fails closed; nothing is defaulted.  The delivered
        assembly is named by ``settings["main_assembly"]``; without one, a
        frozen directory holding exactly one ``.SLDASM`` is accepted as the
        sole candidate, while zero or several assemblies fail with an
        actionable selection error before any CAD session starts.  Exactly the
        selected frozen entry is opened read-only and only it gates the run.

        Mate entities that lie on the owning assembly's own reference geometry
        (assembly planes/axes) are recorded with ``component`` equal to that
        assembly's scope — ``""`` for the frozen root, or the sub-assembly
        occurrence path for a nested frame — and ``assembly_frame: true``.
        They bind to the assembly frame, never to a component occurrence name.
        The entity is identified as the assembly root against the object read
        from ``GetRootComponent3`` under the assembly's current configuration
        — exact normalized full document path equality across the walked
        assembly, the root object and the reference, equal reference/root
        ``Name2``, and ``IsRoot`` reading as exactly the boolean True (a
        readable False, a non-boolean value and an unreadable reading all fail
        closed) — never from the reference name alone.
        An unproven same-document reference keeps failing closed with
        ``cad_mate_scope_ambiguous``; containment chains that repeat a document
        and occurrences holding the owning assembly's own document raise
        ``cad_reference_cycle`` instead of being traversed or bound.
        Every mate entity also carries ``mate_entity_reference`` — the
        documented ``IMateEntity2`` readings ``ReferenceType`` /
        ``ReferenceType2`` as provenance plus ``EntityParams``
        (``[point xyz, vector ijk, radius1, radius2]`` in the owning
        assembly's model space), decoded only for the documented
        point/line/plane/cylinder/cone kinds — never conflating geometric
        classes — and re-expressed in the referenced component's local frame
        (frame entities keep their owning-assembly parameters).  A missing or
        malformed reading is recorded explicitly with the decoded geometry
        withheld.
        The block is additive evidence: the existing plane/cylinder/circle/point
        keys and every derivation consumer are unchanged by it, so recorded
        geometry gaps are not resolved until a consumer update is reviewed.
        Every component occurrence additionally carries ``solver_observation``:
        the configuration-bound ``IComponent2.Solving`` (family
        ``swComponentSolvingOption_e``) and ``IComponent2.GetConstrainedStatus``
        (family ``swConstrainedStatus_e``) readings, each with its method
        provenance, plus the defining document, its configuration and the
        occurrence scope the reading was taken in — distinct from the
        occurrence's referenced child configuration and from ``IsFixed``.
        These readings are diagnostics for unproven bodies: they change no
        grouping, mass, axis, limit or gate derivation, and an unavailable or
        unreadable reading is preserved explicitly (``value: None`` plus
        ``error``) without failing the capture.
        """

        source_root = Path(frozen_source).resolve()
        activity = _ActivityEmitter(on_activity)
        selection = settings.get("main_assembly") if isinstance(settings, dict) else None
        if selection is not None and (not isinstance(selection, str) or not selection.strip()):
            raise CadError(
                "native_discovery_selection_invalid",
                "the selected main assembly is not a canonical relative path",
                {"main_assembly": repr(selection)},
            )
        selected_path = None
        selection_mode = None
        if selection is not None:
            from ...io import artifact_path_parts, confined

            try:
                parts = artifact_path_parts(selection)
            except Exception as error:  # noqa: BLE001 - reported as the structured selection failure
                raise CadError(
                    "native_discovery_selection_invalid",
                    "the selected main assembly is not a portable relative path",
                    {"main_assembly": selection, "error": str(error)},
                ) from error
            if (
                not parts
                or "/".join(parts) != selection
                or "\\" in selection
                or not selection.casefold().endswith(".sldasm")
                or Path(selection).name.startswith("~$")
            ):
                raise CadError(
                    "native_discovery_selection_invalid",
                    "the selected main assembly is not a canonical .SLDASM path inside the handoff",
                    {"main_assembly": selection},
                )
            try:
                selected_path = Path(confined(source_root, selection, exists=False))
            except Exception as error:  # noqa: BLE001 - reported as the structured selection failure
                raise CadError(
                    "native_discovery_selection_invalid",
                    "the selected main assembly is not contained in the frozen engineering directory",
                    {"main_assembly": selection, "error": str(error)},
                ) from error
            if not selected_path.is_file():
                raise CadError(
                    "native_discovery_selection_missing",
                    "the selected main assembly is not present in the frozen engineering directory",
                    {"main_assembly": selection},
                )
            selection_mode = "explicit"
        else:
            candidates = sorted(
                path
                for path in source_root.rglob("*")
                if path.suffix.lower() == ".sldasm" and not path.name.startswith("~$") and path.is_file()
            )
            if not candidates:
                raise CadError(
                    "native_discovery_assembly_missing", "no SolidWorks assembly in the engineering directory"
                )
            if len(candidates) > 1:
                raise CadError(
                    "native_discovery_main_assembly_ambiguous",
                    "several assemblies are present; select the delivered main assembly before the CAD session",
                    {"candidates": [path.relative_to(source_root).as_posix() for path in candidates]},
                )
            selected_path = candidates[0]
            selection_mode = "sole_candidate"
        activity.emit("session_start", force=True)
        with self.session():
            native_api_pid = int(_method(self._app_obj(), "GetProcessID"))
            # Exactly the selected frozen entry is opened; zero or several
            # assemblies already failed before this session started.
            activity.emit("open_document", current_object=_activity_object(selected_path, source_root), force=True)
            main_entry = self._main_entry(selected_path)
            main_entry["selection_mode"] = selection_mode
            main_path = main_entry["path"]
            errors = main_entry.get("errors")
            warnings = main_entry.get("warnings")
            if main_entry.get("already_open"):
                # A document already open in the owned session may have been
                # loaded as a transitive dependency of another document; only a
                # fresh exact-root open reports this document's own status.
                try:
                    doc, raw_errors, raw_warnings = self._open_document_raw(str(main_path))
                except Exception as exc:  # noqa: BLE001 - reported as the strict open failure
                    raise self._open_failure(
                        str(main_path), None, None, exception=f"{type(exc).__name__}: {exc}"[:300]
                    ) from exc
                if doc is None:
                    raise self._open_failure(
                        str(main_path), None, None, exception="exact-root open returned no document"
                    )
                if type(raw_errors.value) is not int or type(raw_warnings.value) is not int:
                    raise self._open_failure(
                        str(main_path), None, None, exception="exact-root open status is not an integer"
                    )
                errors, warnings = raw_errors.value, raw_warnings.value
                main_entry = {**main_entry, "doc": doc, "errors": errors, "warnings": warnings}
            if main_entry.get("doc") is None or type(errors) is not int or errors != 0:
                raise self._open_failure(
                    str(main_path),
                    errors if type(errors) is int else None,
                    warnings if type(warnings) is int else None,
                    exception=main_entry.get("open_exception"),
                )
            doc = main_entry["doc"]
            notes: list[str] = []
            activity.emit("read_document_state", current_object=_activity_object(main_path, source_root), force=True)
            document_state_before = _discovery_document_state(doc)
            _manager, active = _active_configuration_view(doc)
            configuration = str(_member(active, "Name") or "")
            if not configuration:
                raise CadError("cad_configuration_missing", "the saved main assembly configuration is unreadable")
            # Capture native persistent identities in the initial saved root
            # context. Reading shared import documents later can make
            # GetImportedPath expose the stored source path instead of the
            # path SolidWorks resolved when this exact root was opened.
            exporter_block = _urdf_exporter_attribute(self._app_obj(), doc, source_root, configuration)
            activity.emit("read_properties", current_object=_activity_object(main_path, source_root), force=True)
            identity_properties = _custom_properties(doc, configuration)
            doc = active = None
            components = []
            by_component = {}
            by_document: dict[str, str] = {}
            owner_hashes: dict[str, str] = {}
            masses = []
            mates: list[dict] = []
            components_total = 0
            components_done = 0
            mates_total = 0
            mates_done = 0
            stack = [
                (
                    str(main_path),
                    "",
                    configuration,
                    [[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0], [0.0, 0.0, 1.0, 0.0], [0.0, 0.0, 0.0, 1.0]],
                    (normalize_document_path(str(main_path)),),
                    None,
                    str(main_path),
                )
            ]
            while stack:
                (
                    assembly_path,
                    prefix,
                    referenced_configuration,
                    parent_matrix,
                    ancestor_paths,
                    anchor,
                    owner_path,
                ) = stack.pop()
                activity.emit("open_document", current_object=_activity_object(assembly_path, source_root))
                assembly = self._document_by_path(assembly_path)
                activity.emit(
                    "select_configuration",
                    current_object=_activity_object(assembly_path, source_root),
                    params={"configuration": str(referenced_configuration or "")},
                )
                _select_configuration(assembly, referenced_configuration, prefix or "assembly")
                _manager, active_config = _active_configuration_view(assembly)
                root = _component(_member(active_config, "GetRootComponent3", True))
                root_children = list(_method(root, "GetChildren") or [])
                components_total += len(root_children)
                for raw in root_children:
                    component = _component(raw)
                    name = str(_member(component, "Name2") or "")
                    if not name:
                        components_done += 1
                        continue
                    # The scoped instance identity must be byte-identical to the
                    # selection walk's (and inspect_copy's) stitching rule so the
                    # closure carrier's occurrence strings compare exactly.
                    path_name = name if not prefix or name.startswith(prefix + "/") else f"{prefix}/{name}"
                    if path_name in by_component:
                        raise CadError(
                            "cad_component_identity_ambiguous",
                            "two native occurrences have the same scoped identity",
                            {"component": path_name, "configuration": referenced_configuration},
                        )
                    activity.emit(
                        "read_components",
                        current_object=_activity_object(path_name),
                        completed=components_done,
                        total=components_total,
                    )
                    # Read the documented suppression state before any path, model or
                    # mass traversal: a mismatched internal-ID binding must refuse the
                    # capture, not be silently traversed as a resolved occurrence.
                    state = _component_state(component)
                    document_path = _method(component, "GetPathName")
                    source_document = _component_source_document(component, source_root)
                    # The native materialization identity is recorded inside
                    # source_provenance. Reading it from the outer record would
                    # make cached descendants lose their defining anchor.
                    source_provenance = source_document.get("source_provenance") or {}
                    materialization_path = source_provenance.get("materialization_path")
                    materialized = bool(materialization_path) and (
                        session_materialization_path(materialization_path)
                        or (
                            source_provenance.get("state") == "embedded_virtual_component"
                            and embedded_virtual_component_path(materialization_path)
                        )
                    )
                    anchor_for_children = path_name if materialized else anchor
                    # Ultimate native owner: the nearest ancestor delivered
                    # document that owns this occurrence's subtree; nested
                    # materialization descendants inherit it unchanged.
                    child_owner_path = owner_path
                    if not materialized:
                        child_relative = _relative_document(document_path, source_root)
                        if child_relative:
                            child_owner_path = str(Path(source_root) / child_relative)
                    provenance = source_document.get("source_provenance")
                    if provenance is not None:
                        provenance["native_api_pid"] = native_api_pid
                        owner_relative = _relative_document(owner_path, source_root)
                        if not owner_relative:
                            owner_relative = normalize_document_path(str(owner_path))
                        try:
                            if owner_path not in owner_hashes:
                                owner_hashes[owner_path] = _hash(str(owner_path))
                            provenance["owner_sha256"] = owner_hashes[owner_path]
                        except Exception as error:  # noqa: BLE001 - an unreadable owner hash stays explicit
                            provenance["owner_sha256"] = None
                            provenance["owner_sha256_error"] = f"{type(error).__name__}: {error}"[:200]
                        provenance["occurrence"] = path_name
                        provenance["owner"] = owner_relative
                        provenance["defining_anchor"] = anchor
                        self._source_provenance[path_name] = provenance
                    local_transform = []
                    # Mechanical placement is the fixed False form only; the True
                    # form includes the presentation transform and must never
                    # become an alternate mechanical reading.
                    transform_error = "GetTotalTransform(False) returned no transform data"
                    try:
                        holder = _member(component, "GetTotalTransform", False)
                        local_transform = [
                            float(value) for value in transform_from_solidworks(_member(holder, "ArrayData"))
                        ]
                    except Exception as error:  # noqa: BLE001 - an unreadable transform must block, not guess
                        transform_error = f"GetTotalTransform(False): {error}"
                    else:
                        if local_transform:
                            transform_error = ""
                    if not local_transform:
                        notes.append(f"transform:{path_name}:{transform_error}")
                    transform = []
                    if local_transform:
                        local_matrix = [
                            local_transform[0:4],
                            local_transform[4:8],
                            local_transform[8:12],
                            local_transform[12:16],
                        ]
                        global_matrix = self._multiply_frames(parent_matrix, local_matrix)
                        transform = [value for row in global_matrix for value in row]
                    entry = {
                        "name2": path_name,
                        "instance_id": path_name,
                        "document": source_document["document"]
                        or (normalize_document_path(str(document_path)) if _is_text_name(document_path) else ""),
                        "configuration": str(_member(component, "ReferencedConfiguration") or ""),
                        "fixed": bool(_member(component, "IsFixed")),
                        "suppressed": state == COMPONENT_SUPPRESSED,
                        "suppression_state": state,
                        "lightweight": state in (1, 4),
                        "transform": transform,
                    }
                    if "source_provenance" in source_document:
                        entry["source_provenance"] = source_document["source_provenance"]
                    entry["defining_anchor"] = anchor
                    entry["solver_observation"] = _solver_observation(
                        component,
                        assembly_path,
                        source_root,
                        configuration=referenced_configuration,
                        scope=prefix,
                    )
                    components.append(entry)
                    # Keep primitives beyond this traversal scope. Later
                    # configuration changes must not reuse borrowed occurrences.
                    by_component[path_name] = (str(document_path), _method(component, "GetModelDoc2") is not None)
                    by_document[path_name] = entry["document"]
                    try:
                        mass_property = _member(_member(assembly, "Extension"), "CreateMassProperty2")
                        if mass_property is not None and entry["lightweight"]:
                            # Informational per-component mass: a lightweight
                            # occurrence is not measured and is never silently
                            # reported as if it had no mass.
                            notes.append(f"mass:{path_name}:lightweight_state_{entry['suppression_state']}_unmeasured")
                        if (
                            mass_property is not None
                            and not entry["suppressed"]
                            and not entry["lightweight"]
                            and not entry["document"].lower().endswith(".sldasm")
                        ):
                            mass_property.UseSystemUnits = True
                            mass_property.IncludeHiddenBodiesOrComponents = True
                            import pythoncom

                            mass_property.SelectedItems = _win32().VARIANT(
                                pythoncom.VT_ARRAY | pythoncom.VT_DISPATCH, (component,)
                            )
                            _member(mass_property, "Recalculate")
                            mass = float(_member(mass_property, "Mass"))
                            volume = float(_member(mass_property, "Volume"))
                            if math.isfinite(mass) and mass > 0:
                                masses.append(
                                    {
                                        "component": path_name,
                                        "mass_kg": mass,
                                        "volume_m3": volume if math.isfinite(volume) else None,
                                        "material": None,
                                    }
                                )
                    except Exception as error:  # noqa: BLE001 - masses are informational here
                        notes.append(f"mass:{path_name}:{error}")
                    children = list(_method(component, "GetChildren") or [])
                    if children:
                        if not transform:
                            # An unreadable occurrence transform leaves no frame
                            # for its descendants: composing with an empty parent
                            # would fabricate placement.  The subtree is skipped,
                            # the skip is recorded, and the occurrence's own
                            # empty transform stays a blocking native finding
                            # downstream.
                            notes.append(f"transform:{path_name}:subtree_skipped_unreadable_parent")
                        part = _method(component, "GetModelDoc2") if transform else None
                        if part is not None:
                            referenced = str(_member(component, "ReferencedConfiguration") or "")
                            # A containment chain that repeats a document (an assembly
                            # holding itself, directly or through its ancestors) is a
                            # reference cycle: recursing would never converge and the
                            # occurrence has no resolvable identity.
                            document_key = normalize_document_path(str(document_path))
                            if document_key and document_key in ancestor_paths:
                                raise CadError(
                                    "cad_reference_cycle",
                                    "an assembly occurrence repeats a document in its own ancestor chain",
                                    {"component": path_name, "document": entry["document"]},
                                )
                            stack.append(
                                (
                                    str(document_path),
                                    path_name,
                                    referenced,
                                    [
                                        transform[0:4],
                                        transform[4:8],
                                        transform[8:12],
                                        transform[12:16],
                                    ],
                                    (*ancestor_paths, document_key),
                                    anchor_for_children,
                                    child_owner_path,
                                )
                            )
                    components_done += 1
                mate_list = list(_mate_features(assembly))
                mates_total += len(mate_list)
                for feature, specific, entity_count in mate_list:
                    name = str(_member(feature, "Name") or "")
                    activity.emit(
                        "read_mates",
                        current_object=_activity_object(name),
                        completed=mates_done,
                        total=mates_total,
                    )
                    try:
                        type_index = _method(specific, "Type")
                        if type(type_index) is not int:
                            raise ValueError("mate type is not a native integer")
                    except Exception as error:  # noqa: BLE001
                        raise CadError(
                            "cad_mate_unreadable", "mate type could not be read", {"mate": name, "error": str(error)}
                        ) from error
                    mate_type = _SW_MATE_TYPES.get(type_index, f"unknown-{type_index}")
                    try:
                        suppressed = _method(feature, "IsSuppressed")
                        if type(suppressed) is not bool:
                            raise ValueError("mate suppression state is not a native boolean")
                    except Exception as error:  # noqa: BLE001
                        raise CadError(
                            "cad_mate_unreadable",
                            "mate suppression state could not be read",
                            {"mate": name, "error": str(error)},
                        ) from error
                    try:
                        error_code = _method(feature, "GetErrorCode")
                        if type(error_code) is not int:
                            raise ValueError("mate solve state is not a native integer")
                    except Exception as error:  # noqa: BLE001
                        raise CadError(
                            "cad_mate_unreadable",
                            "mate solve state could not be read; the constraint cannot be trusted",
                            {"mate": name, "error": str(error)},
                        ) from error
                    entities = []
                    for entity_index in range(entity_count):
                        if suppressed:
                            continue
                        try:
                            entity = _dynamic(_method(specific, "MateEntity", entity_index))
                            reference = _component(_member(entity, "ReferenceComponent"))
                            reference_full = str(_method(reference, "GetPathName") or "")
                            reference_name = str(_member(reference, "Name2") or "")
                            reference_configuration = str(_member(reference, "ReferencedConfiguration") or "")
                            reference_path = _relative_document(reference_full, source_root)
                            # An entity may lie on the owning assembly's own reference
                            # geometry (assembly planes/axes): it has no component
                            # occurrence identity and belongs to that assembly's frame.
                            # The root object from this assembly's current configuration
                            # is the authority; a document match alone is not proof, and
                            # the identity must be exact (full path, Name2, IsRoot true).
                            assembly_frame = _matches_assembly_root(
                                reference, root, reference_full, reference_name, assembly_path
                            )
                            # EXEMPT (untyped multi-type return): IMateEntity2.Reference
                            # is a VT_DISPATCH spanning multiple native geometry kinds
                            # with no single declared view, so the generic dispatch
                            # stays and the unambiguous GetFeature/GetSurface/GetCurve
                            # returns are bound to their own published interfaces.
                            target = _member(entity, "Reference")
                        except Exception as error:  # noqa: BLE001
                            raise CadError(
                                "cad_mate_unreadable",
                                "a mate entity could not be read",
                                {"mate": name, "error": str(error)},
                            ) from error
                        entities.append(
                            {
                                "entity_index": entity_index,
                                "reference_name": reference_name,
                                "reference_document": reference_path,
                                "reference_configuration": reference_configuration,
                                "assembly_frame": bool(assembly_frame),
                                "feature": _feature_name(target),
                                "face_index": None,
                                "mate_entity_reference": _mate_entity_reference(entity),
                                **_plane_or_cylinder(target),
                            }
                        )
                    limits = None
                    is_advanced_mate = None
                    is_advanced_mate_error = None
                    is_advanced_mate_api = None
                    variation_lower = None
                    variation_upper = None
                    if suppressed is False and error_code == 0:
                        try:
                            variation_lower = float(_member(specific, "MinimumVariation"))
                            variation_upper = float(_member(specific, "MaximumVariation"))
                        except Exception as error:  # noqa: BLE001
                            if type_index in {5, 6}:
                                raise CadError(
                                    "cad_mate_limits_unreadable",
                                    "mate travel variation could not be read",
                                    {"mate": name, "type": mate_type, "error": str(error)},
                                ) from error
                    if variation_lower is not None and variation_upper is not None and (
                        not math.isfinite(variation_lower)
                        or not math.isfinite(variation_upper)
                        or variation_upper < variation_lower
                    ):
                        raise CadError(
                            "cad_mate_limits_invalid",
                            "mate travel variation is not finite and ordered",
                            {"mate": name, "lower": variation_lower, "upper": variation_upper},
                        )
                    if type_index not in {5, 6} and (
                        variation_lower not in {None, 0.0} or variation_upper not in {None, 0.0}
                    ):
                        raise CadError(
                            "cad_mate_limits_invalid",
                            "a bounded range on an unsupported mate type cannot be observed",
                            {"mate": name, "type": mate_type},
                        )
                    native_limit = None
                    if suppressed is False and type_index in {5, 6} and error_code == 0:
                        interface_prefix = "IAngle" if type_index == 6 else "IDistance"
                        unit = "rad" if type_index == 6 else "m"
                        names = (
                            ("Angle", "MinimumAngle", "MaximumAngle")
                            if type_index == 6
                            else ("Distance", "MinimumDistance", "MaximumDistance")
                        )
                        try:
                            definition = _method(feature, "GetDefinition")
                            if definition is None:
                                is_advanced_mate_error = "mate definition is null"
                            else:
                                iid = IANGLEMATEFEATUREDATA_IID if type_index == 6 else IDISTANCEMATEFEATUREDATA_IID
                                bound = _interface(definition, iid)
                                value = _member(bound, "IsAdvancedMate")
                                if type(value) is bool:
                                    is_advanced_mate = value
                                    is_advanced_mate_api = f"{interface_prefix}MateFeatureData.IsAdvancedMate"
                                else:
                                    is_advanced_mate_error = f"unexpected value: {value!r}"
                                if is_advanced_mate is True:
                                    values = {}
                                    for key in names:
                                        raw_value = _member(bound, key)
                                        parsed = float(raw_value)
                                        if not math.isfinite(parsed):
                                            raise ValueError(f"{key} is not finite")
                                        values[key] = parsed
                                    values["MinimumVariation"] = variation_lower
                                    values["MaximumVariation"] = variation_upper
                                    nominal, minimum, maximum = (values[key] for key in names)
                                    lower = nominal + values["MinimumVariation"]
                                    upper = nominal + values["MaximumVariation"]
                                    if (
                                        abs(lower - minimum) > 1e-12
                                        or abs(upper - maximum) > 1e-12
                                        or upper < lower
                                    ):
                                        raise ValueError("native variation and absolute limits disagree")
                                    limits = {"lower": minimum, "upper": maximum, "unit": unit}
                                    mate_type = "limitdistance" if type_index == 5 else "limitangle"
                                    native_limit = {
                                        "type_index": type_index,
                                        "definition_interface": f"{interface_prefix}MateFeatureData",
                                        "definition_interface_iid": iid,
                                        "nominal_dimension": {
                                            "value": nominal,
                                            "api": f"{interface_prefix}MateFeatureData.{names[0]}",
                                            "unit": unit,
                                        },
                                        "minimum_variation": {
                                            "value": values["MinimumVariation"],
                                            "api": "IMate2.MinimumVariation",
                                            "unit": unit,
                                        },
                                        "maximum_variation": {
                                            "value": values["MaximumVariation"],
                                            "api": "IMate2.MaximumVariation",
                                            "unit": unit,
                                        },
                                        "minimum_limit": {
                                            "value": minimum,
                                            "api": f"{interface_prefix}MateFeatureData.{names[1]}",
                                            "unit": unit,
                                        },
                                        "maximum_limit": {
                                            "value": maximum,
                                            "api": f"{interface_prefix}MateFeatureData.{names[2]}",
                                            "unit": unit,
                                        },
                                    }
                        except Exception as error:  # noqa: BLE001
                            is_advanced_mate_error = f"{type(error).__name__}: {error}"[:240]
                    mate_record = {
                        "name": name,
                        "type": mate_type,
                        "suppressed": suppressed,
                        "limits": limits,
                        "is_advanced_mate": is_advanced_mate,
                        "is_advanced_mate_error": is_advanced_mate_error,
                        "is_advanced_mate_api": is_advanced_mate_api,
                        "native_limit": native_limit,
                        "entity_count": entity_count,
                        "entities": entities,
                        "error_code": error_code,
                        "scope": prefix or "",
                        "configuration": referenced_configuration,
                    }
                    if mate_type == "concentric":
                        # Documented native rotation lock (IConcentricMateFeatureData
                        # .LockRotation, SW2019+; typelib IID {D9E1EBDD-...}): the
                        # flag removes ONE rotational degree of freedom; a locked
                        # concentric is not by itself rigid because axial
                        # translation remains until another mate constrains it.
                        # The value is read from CAD through the same GetDefinition
                        # path the earlier manual native scripts used; it is never
                        # inferred from any reference output.
                        lock_rotation = None
                        lock_rotation_api = None
                        try:
                            definition = _method(feature, "GetDefinition")
                            if definition is not None:
                                bound = _interface(definition, ICONCENTRICMATEFEATUREDATA_IID)
                                raw_lock = _member(bound, "LockRotation")
                                if isinstance(raw_lock, bool):
                                    lock_rotation = raw_lock
                                    lock_rotation_api = "IConcentricMateFeatureData.LockRotation"
                                    notes.append(
                                        f"mate_lock_rotation:{name}:read:IConcentricMateFeatureData.LockRotation"
                                    )
                                else:
                                    notes.append(f"mate_lock_rotation:{name}:unreadable:{repr(raw_lock)[:60]}")
                            else:
                                notes.append(f"mate_lock_rotation:{name}:no_definition")
                        except Exception as error:  # noqa: BLE001 - recorded, never guessed
                            notes.append(f"mate_lock_rotation:{name}:{type(error).__name__}: {str(error)[:160]}")
                        mate_record["lock_rotation"] = lock_rotation
                        if lock_rotation_api is not None:
                            mate_record["lock_rotation_api"] = lock_rotation_api
                    mates.append(mate_record)
                    mates_done += 1
                # The next assembly selection may invalidate this entire borrowed
                # tree. Pending assemblies and occurrence records contain paths
                # and primitives only; release all native traversal handles now.
                component = raw = root = active_config = part = children = holder = mass_property = None
                root_children = mate_list = None
                entity = reference = target = feature = specific = assembly = None
            activity.emit("read_components", completed=components_done, total=components_total, force=True)
            activity.emit("read_mates", completed=mates_done, total=mates_total, force=True)
            # Top-level mates can name descendants that are visited later.
            # Resolve the full scoped occurrence; leaf-name matching loses
            # identity when the same part is inserted more than once.
            entity_frames = {str(entry.get("name2")): _frame_matrix(entry.get("transform")) for entry in components}
            for mate in mates:
                for entity in mate["entities"]:
                    reference_name = entity.pop("reference_name")
                    reference_document = entity.pop("reference_document")
                    assembly_frame = bool(entity.pop("assembly_frame", False))
                    scope = mate["scope"]
                    if assembly_frame:
                        # The entity is the owning assembly's own frame.  Bind it to that
                        # assembly scope ("" is the frozen root, i.e. the global frame);
                        # a nested sub-assembly must still be a recorded occurrence.
                        if scope and (
                            scope not in by_component
                            or (reference_document is not None and by_document[scope] != reference_document)
                        ):
                            raise CadError(
                                "cad_mate_scope_ambiguous",
                                "a mate entity does not resolve to its exact scoped occurrence",
                                {"mate": mate["name"], "component": reference_name, "scope": scope},
                            )
                        entity["component"] = scope
                        entity["assembly_frame"] = True
                        entity["mate_entity_reference"] = _localize_mate_entity_reference(
                            entity.get("mate_entity_reference"), scope, scope, entity_frames
                        )
                        continue
                    scoped_name = (
                        f"{scope}/{reference_name}"
                        if scope and not reference_name.startswith(scope + "/")
                        else reference_name
                    )
                    if scoped_name not in by_component or (
                        reference_document is not None and by_document[scoped_name] != reference_document
                    ):
                        raise CadError(
                            "cad_mate_scope_ambiguous",
                            "a mate entity does not resolve to its exact scoped occurrence",
                            {"mate": mate["name"], "component": reference_name, "scope": scope},
                        )
                    owner_entry = by_component.get(scope) if scope else None
                    owner_document = owner_entry[0] if owner_entry is not None else str(main_path)
                    if normalize_document_path(by_component[scoped_name][0]) == normalize_document_path(owner_document):
                        raise CadError(
                            "cad_reference_cycle",
                            "a mate entity resolves to an occurrence holding the owning assembly's own document",
                            {"mate": mate["name"], "component": reference_name, "scope": scope},
                        )
                    entity["component"] = scoped_name
                    entity["mate_entity_reference"] = _localize_mate_entity_reference(
                        entity.get("mate_entity_reference"), scoped_name, scope, entity_frames
                    )
            datums = []
            activity.emit("read_datums", current_object=_activity_object(main_path, source_root), force=True)
            doc = self._document_by_path(str(main_path))
            _select_configuration(doc, configuration, "assembly")
            for name in _coordinate_system_features(doc):
                matrix = [float(value) for value in self._coordinate_system_transform(doc, name)]
                datums.append(
                    {
                        "name": name,
                        "owner": "",
                        "array": matrix,
                        "configuration": configuration,
                        "references": _coordinate_system_references(
                            doc,
                            name,
                            source_root=source_root,
                            document=_relative_document(main_path, source_root),
                            configuration=configuration,
                            scope="",
                        ),
                    }
                )
            reference_axes = []
            for feature in _features_of_type(doc, {"RefAxis"}):
                suppressed = _method(feature, "IsSuppressed")
                if type(suppressed) is not bool:
                    raise CadError("cad_axis_reference_unreadable", "reference axis suppression state is unreadable")
                if not suppressed:
                    reference_axes.append(_reference_axis_record(doc, str(_member(feature, "Name"))))
            for entry in components:
                source = by_component.get(entry["name2"])
                if source is None or not source[1] or entry["suppressed"]:
                    continue
                if not entry["transform"]:
                    continue
                activity.emit("read_datums", current_object=_activity_object(entry["name2"]))
                document = self._document_by_path(source[0])
                _select_configuration(document, entry["configuration"], entry["name2"])
                for name in _coordinate_system_features(document):
                    values = [float(value) for value in self._coordinate_system_transform(document, name)]
                    local = [values[0:4], values[4:8], values[8:12], values[12:16]]
                    component_matrix = [
                        entry["transform"][0:4],
                        entry["transform"][4:8],
                        entry["transform"][8:12],
                        entry["transform"][12:16],
                    ]
                    composed = self._multiply_frames(component_matrix, local)
                    datums.append(
                        {
                            "name": name,
                            "owner": entry["name2"],
                            "array": [value for row in composed for value in row],
                            "configuration": entry["configuration"],
                            "references": _coordinate_system_references(
                                document,
                                name,
                                source_root=source_root,
                                document=_relative_document(source[0], source_root),
                                configuration=entry["configuration"],
                                scope=str(entry["name2"]),
                            ),
                        }
                    )
            property_buckets = {"document": identity_properties, "components": {}, "mates": {}}
            for entry in components:
                source = by_component.get(entry["name2"])
                if source is None or not source[1] or entry["suppressed"]:
                    continue
                activity.emit("read_properties", current_object=_activity_object(entry["name2"]))
                document = self._document_by_path(source[0])
                _select_configuration(document, entry["configuration"], entry["name2"])
                values = _custom_properties(document, entry["configuration"] or None)
                if values:
                    property_buckets["components"][entry["name2"]] = values
            files = {}
            hash_plan = []
            planned = set()
            for path in (main_path, *[Path(item[0]) for item in by_component.values()]):
                relative = _relative_document(path, source_root)
                if relative and relative not in planned:
                    planned.add(relative)
                    hash_plan.append((path, relative))
            for entry in components:
                provenance = entry.get("source_provenance", {})
                relative = provenance.get("document") if provenance.get("state") == "resolved" else None
                if relative and relative not in planned:
                    planned.add(relative)
                    hash_plan.append((source_root / relative, relative))
                elif provenance.get("state") == "cached_materialization" and provenance.get(
                    "author_imported_in_source_root"
                ):
                    # A validated in-stage author source of a cached
                    # materialization is an acquired CAD document: hash the
                    # staged bytes that the sealed selection retains as
                    # bound_source, so the native file inventory carries the
                    # same file.  Strictly confined to the source root;
                    # external author metadata never enters the file map.
                    bound_relative = provenance.get("author_imported_relative")
                    if isinstance(bound_relative, str) and bound_relative:
                        staged = source_root / bound_relative
                        if _relative_document(staged, source_root) != bound_relative:
                            raise CadError(
                                "cad_import_source_unavailable",
                                "the validated in-stage import source is missing from the acquired folder",
                                {"path": bound_relative},
                            )
                        if bound_relative not in planned:
                            planned.add(bound_relative)
                            hash_plan.append((staged, bound_relative))
            for index, (path, relative) in enumerate(hash_plan):
                activity.emit(
                    "hash_sources",
                    current_object=_activity_object(path, source_root) or relative,
                    completed=index,
                    total=len(hash_plan),
                )
                files[relative] = _hash(str(Path(source_root) / relative))
            activity.emit("hash_sources", completed=len(hash_plan), total=len(hash_plan), force=True)
            activity.emit(
                "build_record",
                force=True,
                params={"components": len(components), "mates": len(mates), "datums": len(datums)},
            )
            return {
                "schema_version": NATIVE_DISCOVERY_SCHEMA,
                "contract": "native-discovery/v1",
                "namespace": "dp",
                "generator": "solidworks-native-reader",
                "solidworks": dict(self.environment()),
                "saved_configuration": configuration,
                "document_state": {
                    "before": document_state_before,
                    "after": _discovery_document_state(self._document_by_path(str(main_path))),
                    "used_api": (
                        "IModelDoc2.GetSaveFlag/IModelDocExtension.NeedsRebuild2/"
                        "IConfiguration.Name/IConfiguration.NeedsRebuild"
                    ),
                },
                "identity": {
                    "hardware_id": identity_properties.get("dp.hardware_id"),
                    "revision": identity_properties.get("dp.revision"),
                    "parent_revision": identity_properties.get("dp.parent_revision"),
                    "owner": identity_properties.get("dp.owner"),
                    "change_summary": identity_properties.get("dp.change_summary"),
                    "control": {
                        "system": identity_properties.get("dp.control.system"),
                        "reference": identity_properties.get("dp.control.reference"),
                    },
                    "delivery_configuration": identity_properties.get("dp.delivery_configuration"),
                    "main_assembly": _relative_document(main_path, source_root),
                    "robot_name": identity_properties.get("dp.robot_name"),
                },
                "components": components,
                "mates": mates,
                "datums": datums,
                "reference_axes": reference_axes,
                "exporter": exporter_block,
                "masses": masses,
                "properties": property_buckets,
                "files": files,
                "selection": {
                    "mode": main_entry.get("selection_mode"),
                    "main_assembly": _relative_document(main_path, source_root),
                },
                "notes": notes,
            }

    @staticmethod
    def _multiply_frames(left, right):
        """Column-vector 4x4 product; ``self._coordinate_system_transform`` returns rows."""

        return [[sum(left[row][k] * right[k][column] for k in range(4)) for column in range(4)] for row in range(4)]

    def selftest(self, test_cs=None, export_mesh=None):
        points = {}

        def probe(name, fn):
            try:
                result = fn()
                points[name] = {"ok": True, "detail": result}
                return result
            except Exception as exc:
                points[name] = {"ok": False, "detail": str(exc), "code": getattr(exc, "code", type(exc).__name__)}
                return None

        probe("attach", lambda: self.health())
        doc = None
        try:
            doc = self._active_document()
            points["active_document"] = {"ok": True, "detail": _member(doc, "GetPathName") or _member(doc, "GetTitle")}
        except Exception as exc:
            points["active_document"] = {"ok": False, "detail": str(exc)}
        if doc is not None:
            if _member(doc, "GetType") == 2:
                scene = probe(
                    "assembly_export_inputs",
                    lambda: self.collect_scene(_member(doc, "GetPathName"), [test_cs] if test_cs else []),
                )
                if scene is not None:
                    points["assembly_export_inputs"]["detail"] = {
                        "components": len(scene.components),
                        "mass_kg": sum(v["mass"] for v in scene.mass_properties.values()),
                    }
            else:
                probe("part_mass_inertia", lambda: self._mass_properties_document(doc))
                if test_cs:
                    probe("coordinate_system", lambda: self._coordinate_system_transform(doc, test_cs))
            if export_mesh:
                # Do not disguise file writes behind an HTTP GET.
                points["export_mesh"] = {"ok": False, "detail": "use an explicit export job for mesh output"}
        return {"ok": bool(points) and all(p["ok"] for p in points.values()), "backend": self.name, "points": points}
