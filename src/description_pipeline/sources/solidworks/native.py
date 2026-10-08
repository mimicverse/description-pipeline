"""Explicit, fail-closed SolidWorks COM access.

Each capture owns separate source and copy applications. Documents open read-only
from saved files; the user's application is never used as a collection server.
Failures raise stable error codes and only owned process trees are reclaimed.
"""

from __future__ import annotations

import hashlib
import math
import ntpath
import os
import posixpath
import re
import shutil
import struct
import sys
import threading
from contextlib import contextmanager
from pathlib import Path

from ...geometry.stl import StlError
from ...geometry.stl import read as read_stl
from .errors import CadError, EnvironmentError_
from .protocol import CadBackend, RawComponent, RawScene

# Published IComponent2 dual-interface IID in the SolidWorks type library.
ICOMPONENT2_IID = "{655D6F2A-5441-45D1-8CBA-D35FB26988E4}"


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


def _dynamic(value):
    # A COM interface can be callable through DISPID_VALUE. It is not a method.
    if hasattr(value, "_oleobj_"):
        import win32com.client.dynamic

        return win32com.client.dynamic.DumbDispatch(value._oleobj_)
    return value


def _component(value):
    """Bind a native occurrence to its published component interface."""
    if not hasattr(value, "_oleobj_"):
        return value
    import pythoncom
    import pywintypes
    import win32com.client.dynamic

    dispatch = value._oleobj_.QueryInterface(pywintypes.IID(ICOMPONENT2_IID), pythoncom.IID_IDispatch)
    return win32com.client.dynamic.DumbDispatch(dispatch)


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
    except AttributeError as error:  # 稳定的错误码，避免上层看到裸 AttributeError
        raise CadError("cad_member_missing", name) from error
    if hasattr(value, "_oleobj_"):
        if args:
            raise CadError("cad_member_not_callable", name)
        return _dynamic(value)
    if callable(value):
        return _dynamic(value(*args))
    if args:
        raise CadError("cad_member_not_callable", name)
    return value


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
    except AttributeError as error:  # 稳定的错误码，避免上层看到裸 AttributeError
        raise CadError("cad_member_missing", name) from error
    if hasattr(value, "_oleobj_"):
        raise CadError("cad_member_not_callable", name)
    if callable(value):
        return _dynamic(value(*args))
    raise CadError("cad_member_not_callable", name)


def _active_configuration(doc):
    """Name of the configuration a document currently has active."""

    manager = _member(doc, "ConfigurationManager")
    if manager is None:
        return None
    active = _member(manager, "ActiveConfiguration")
    name = _member(active, "Name") if active is not None else None
    return str(name) if name else None


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
            restoration = error if isinstance(error, CadError) else CadError(
                "cad_configuration_unreadable",
                "the document could not be restored to its prior configuration",
                {"component": occurrence, "configuration": previous, "error": str(error)},
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


def normalize_document_path(path):
    return (path or "").replace("/", "\\").strip().lower()


def document_paths_match(active, requested):
    a, b = normalize_document_path(active), normalize_document_path(requested)
    return bool(a and b and (a == b or ("\\" not in b and a.rsplit("\\", 1)[-1] == b)))


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
    """Require an explicit material for every included solid, without editing CAD.

    A positive Density is insufficient: an unassigned imported solid can still
    have a default density. Body material takes precedence over part material.
    This verifies assignment coverage, not correctness against real hardware.
    """
    try:
        active = _member(_member(doc, "ConfigurationManager"), "ActiveConfiguration")
        configuration = str(_member(active, "Name"))
        configurations = list(_member(doc, "GetConfigurationNames") or ())
        if not configuration or configuration not in configurations:
            raise ValueError("Active configuration is not in the document")
        # The API documents an empty argument for a sole Default configuration.
        query = "" if configurations == ["Default"] else configuration
        part = _read_material(doc, "GetMaterialPropertyName2", query)
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
    """The material evidence kept when a documented-table capture cannot verify a part.

    The CAD error carries the full part/body assignment in its detail; reducing the fallback to a
    code and a message would throw away what the document actually held.  Every detail key the
    reader produced travels with the record instead.
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


def _optional_bool(obj, *names):
    """A bool member that may not exist on every SolidWorks build."""

    for name in names:
        try:
            return bool(_member(obj, name))
        except Exception:  # noqa: BLE001
            continue
    return None


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
                raise CadError(
                    "cad_mate_unreadable", "mate sub-feature walk failed", {"error": str(error)}
                ) from error
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


class SolidWorksBackend(CadBackend):
    name = "solidworks"

    def __init__(self, *, session_factory=None):
        self._local = threading.local()
        self._session_factory = session_factory
        self._sessions = {}
        self._sessions_lock = threading.Lock()
        self._owner_thread = None
        self._cancelled = threading.Event()
        self._capture_roots = []
        self._requested_configurations = {}
        self._components = set()
        self._source_components = {}
        self._source_documents = {}
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

            session = (self._session_factory or CadSession)()
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
        return app

    def _role_for_path(self, path):
        normalized = normalize_document_path(os.path.abspath(path))
        return "copy" if any(normalized.startswith(root + "\\") for root in self._capture_roots) else "source"

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

    def release(self):
        if self._owner_thread is not None and self._owner_thread is not threading.current_thread():
            raise EnvironmentError_("cad_thread_mismatch", "Release CAD references on their owning STA thread")
        self._components.clear()
        self._source_components.clear()
        self._source_documents.clear()
        self._scene_document_key = None
        self.save_flags.clear()
        with self._sessions_lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
            self._owner_thread = None
        errors = []
        for session in sessions:
            if session.closed:
                continue
            try:
                session.close()
            except Exception as error:
                errors.append(str(error))
        self._capture_roots.clear()
        self._requested_configurations.clear()
        if errors:
            raise EnvironmentError_("cad_process_cleanup_failed", "; ".join(errors))

    def _active_document(self):
        doc = _member(self._app_obj(), "ActiveDoc")
        if doc is None:
            raise EnvironmentError_("no_active_document", "SolidWorks has no active document")
        return _read_only_document(doc)

    def _document_by_path(self, path):
        if not path:
            return self._active_document()
        doc = _member(self._app_for_path(path), "GetOpenDocumentByName", path)
        if doc is None or not document_paths_match(_member(doc, "GetPathName"), path):
            raise CadError(
                "document_not_open",
                "requested document is not open in the CAD session the adapter owns; "
                "the adapter reads the revision from disk in a session of its own and never uses yours",
                {"path": path},
            )
        return _read_only_document(doc)

    def health(self):
        app = self._app_obj()
        doc = _member(app, "ActiveDoc")
        return {
            "ok": True,
            "backend": self.name,
            "sw_version": _member(app, "RevisionNumber"),
            "active_document": (_member(doc, "GetPathName") or _member(doc, "GetTitle")) if doc is not None else None,
        }

    def list_documents(self):
        return [
            _member(_dynamic(doc), "GetPathName") or _member(_dynamic(doc), "GetTitle")
            for doc in (_member(self._app_obj(), "GetDocuments") or ())
        ]

    def open_document(self, path):
        # Silent + read-only in the owned application for this phase.
        import pythoncom

        wc = _win32()
        kind = {".sldasm": 2, ".sldprt": 1}.get(os.path.splitext(path)[1].lower())
        if kind is None:
            raise CadError("cad_document_type", "only SLDASM/SLDPRT are supported")
        errors = wc.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
        warnings = wc.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
        configuration = self._requested_configurations.get(normalize_document_path(path), "")
        doc = _member(self._app_for_path(path), "OpenDoc6", path, kind, 3, configuration, errors, warnings)
        if doc is None or errors.value:
            raise CadError("cad_document_open_failed", path, {"errors": errors.value, "warnings": warnings.value})
        return {
            "opened": _member(doc, "GetTitle"),
            "path": _member(doc, "GetPathName"),
            "read_only": True,
            "errors": errors.value,
            "warnings": warnings.value,
        }

    def close_document(self, name, confirm=False):
        if not confirm:
            raise CadError("confirm_required", "closing requires explicit confirmation")
        _member(self._app_obj(), "CloseDoc", name)
        return {"closed": name}

    def _placement(self, component):
        transform = _member(component, "GetTotalTransform", False)
        if transform is None:
            raise CadError("cad_transform_failed", _member(component, "Name2"))
        return transform_from_solidworks(_member(transform, "ArrayData"))

    def _mass_properties_document(self, doc, require_material=True):
        if _member(doc, "GetType") != 1:
            raise CadError("cad_not_part", "mass reader requires a leaf part")
        bodies = _member(doc, "GetBodies2", 0, False) or ()
        if not bodies:
            raise CadError("cad_empty_model", "part has no solid bodies")
        if require_material:
            materials = _material_assignments_document(doc, bodies)
        else:
            # Documented mass table: CAD materials are optional, but whatever the
            # document does contain is still recorded verbatim as evidence.
            try:
                materials = _material_assignments_document(doc, bodies)
            except CadError as exc:
                active = _member(_member(doc, "ConfigurationManager"), "ActiveConfiguration")
                materials = unverified_material_record(exc, str(_member(active, "Name")))
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
        inertia, _ = _inertia_from_raw(values, _member(doc, "GetTitle"))
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
                # Measured on the analytic fixture 2026-10-06 (SolidWorks
                # 34.0.0): GetMomentOfInertia(0) equals the analytic standard
                # tensor to 4e-20 for a rotated box, so the part document is
                # read as-is; solidworks_standard is the only supported
                # product convention.
                "product_convention": "solidworks_standard",
                "volume_m3": float(_member(mp, "Volume")),
                "density_kg_m3": float(_member(mp, "Density")),
                "overrides": overrides,
                "body_count": len(bodies),
                "part_document": str(_member(doc, "GetPathName")),
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
        configuration = _member(_member(doc, "ConfigurationManager"), "ActiveConfiguration")
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
        inertia, _ = _inertia_from_raw(values, _member(doc, "GetTitle"))
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
                "document": str(_member(doc, "GetPathName")),
                "configuration": str(_member(configuration, "Name")),
            },
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
        configuration = _member(_member(doc, "ConfigurationManager"), "ActiveConfiguration")
        root = _component(_member(configuration, "GetRootComponent3", True))
        if root is None:
            raise CadError("cad_empty_mass_property", "assembly mass context has no root component")
        import pythoncom

        entries: list[dict] = []
        errors: list[dict] = []
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
                if _member(component, "IsSuppressed"):
                    continue
                children = list(_method(component, "GetChildren") or ())
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
                entries.append(
                    {
                        "name": name,
                        "parent": parent,
                        "depth": depth,
                        "document": str(_method(component, "GetPathName")),
                        "document_type": "assembly" if children else "part",
                        "context_mass_kg": mass,
                        "context_volume_m3": volume if math.isfinite(volume) else None,
                        "overrides": {
                            key: bool(_member(override, key))
                            for key in ("OverrideMass", "OverrideCenterOfMass", "OverrideMomentsOfInertia")
                        },
                    }
                )
            except Exception as error:  # noqa: BLE001 - one unreadable instance is reported, not fatal
                errors.append(_instance_error(name, parent, depth, "mass_property", error))
        return {
            "assembly": str(_member(doc, "GetPathName")),
            "configuration": str(_member(configuration, "Name")),
            "instances": entries,
            "errors": errors,
            "reference": {
                "method": "IMassProperty2 (SelectedItems = component instance)",
                "override_api": "IMassProperty2.GetOverrideOptions (same selection)",
                "document": str(_member(doc, "GetPathName")),
                "configuration": str(_member(configuration, "Name")),
            },
        }

    def capture_axis_reference(self, reference):
        """Resolve a structured native face reference to its cylindrical axis.

        ``reference`` is the authored joint ``axis_reference``
        (``{component, face_index, body_type}``).  The returned record carries
        the identity (component, face index, face name) and the numeric line:
        a point on the axis, the unit direction and the radius, straight from
        ``ISurface.CylinderParams``.  Independent verification can then test
        the authored joint axis for collinearity and origin alignment instead of
        trusting the datum alone.
        """

        component = str(reference.get("component") or "")
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
            face_name = ""
            try:
                face_name = str(_member(face, "Name") or "")
            except CadError:
                face_name = ""
            record = {
                "component": component,
                "body_type": "sheet" if body_type == 1 else "solid",
                "selector": {key: value for key, value in reference.items() if key != "note"},
                "face_name": face_name,
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

    def assembly_group_mass_properties(self, path, names):
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
        configuration = _member(_member(doc, "ConfigurationManager"), "ActiveConfiguration")
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
            if _member(component, "IsSuppressed"):
                continue
            children = list(_method(component, "GetChildren") or ())
            stack.extend(reversed(children))
            name = str(_member(component, "Name2"))
            if name not in wanted:
                continue
            flags = self._component_override_flags(doc, component)
            if any(flags.values()):
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
        inertia, _ = _inertia_from_raw(values, _member(doc, "GetTitle"))
        if mass <= 0 or not math.isfinite(mass) or len(com) != 3 or not all(map(math.isfinite, com)):
            raise CadError("cad_mass_property_invalid", "mass/COM are not finite and positive")
        return {
            "assembly": str(_member(doc, "GetPathName")),
            "configuration": str(_member(configuration, "Name")),
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
                "document": str(_member(doc, "GetPathName")),
                "configuration": str(_member(configuration, "Name")),
            },
        }

    def _rebuild_capture_copy(self, doc, path):
        normalized = normalize_document_path(os.path.abspath(path))
        if not any(normalized.startswith(root + "\\") for root in self._capture_roots):
            raise CadError("cad_rebuild_scope", "Only a collected capture copy may be rebuilt")
        document = str(_member(doc, "GetPathName") or "")
        if not document or not document_paths_match(document, path):
            raise CadError(
                "cad_document_identity",
                "Collected capture document has no matching path identity",
                {"path": path, "document": document},
            )
        configuration = _active_configuration(doc)
        before = bool(_member(doc, "GetSaveFlag"))
        # Reopened assemblies can have resolved solid components but an empty
        # mass cache. Rebuild the owned read-only copy before ANY measurements,
        # not just a failed mass reading. Never save the rebuilt document.
        if not _member(doc, "ForceRebuild3", False):
            raise CadError("cad_rebuild_failed", "Collected assembly did not rebuild successfully", {"path": path})
        # Rebuild can replace model-document handles. Resolve the same open
        # document in its owned session before reading the rebuilt state.
        doc = self._document_by_path(path)
        if _active_configuration(doc) != configuration:
            raise CadError("cad_configuration_mismatch", "Capture rebuild changed the selected configuration")
        return {
            "used_api": "IModelDoc2.ForceRebuild3(False)",
            "scope": "collected_copy_in_memory",
            "document": document,
            "configuration": configuration,
            "read_only": bool(_member(doc, "IsOpenedReadOnly")),
            "saved_to_disk": False,
            "save_flag_before": before,
            "save_flag_after": bool(_member(doc, "GetSaveFlag")),
        }

    def collect_scene(self, doc_path, coordinate_systems, progress=None, require_material=True):
        doc = self._document_by_path(doc_path)
        if _member(doc, "GetType") != 2:
            raise CadError("cad_not_assembly", "export requires a saved SLDASM")
        preparation = self._rebuild_capture_copy(doc, doc_path)
        doc = self._document_by_path(str(preparation["document"]))
        if _active_configuration(doc) != preparation["configuration"]:
            raise CadError("cad_configuration_mismatch", "Capture rebuild changed the selected configuration")
        self._record_save_flag(doc, doc_path)
        self._components = set()
        self._source_components = {}
        self._source_documents = {}
        self.notes = {"capture_preparation": preparation}
        self.source_files = {doc_path: _hash(doc_path)}
        manager = _member(doc, "ConfigurationManager")
        config = _member(manager, "ActiveConfiguration")
        self._scene_document_key = self._record_source_document(doc_path, str(_member(config, "Name")))
        root = _component(_member(config, "GetRootComponent3", True))
        stack = list(_method(root, "GetChildren") or ())
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
            comp = _component(stack.pop())
            if _member(comp, "IsSuppressed"):
                continue
            borrowed_components.append(comp)
            name = str(_member(comp, "Name2"))
            part = _method(comp, "GetModelDoc2")
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
                stack.extend(children)
            else:
                if _member(part, "GetType") != 1:
                    raise CadError("cad_empty_subassembly", name)
                document_type, fixed = "part", bool(_member(comp, "IsFixed"))
                self.notes["bodies:" + name] = {
                    "solid": self._body_count(comp, 0, name),
                    "sheet": self._body_count(comp, 1, name),
                }
            document_key = self._record_source_document(path, _active_configuration(part))
            self._source_components[name] = (document_key, str(referenced))
            if path not in self.source_files:
                self.source_files[path] = _hash(path)
            occurrences.append((RawComponent(name, path, placement, fixed, document_type), referenced))

        borrowed_components.clear()
        comp = children = part = root = stack = None
        for occurrence, referenced in occurrences:
            name, path = occurrence.name, occurrence.path
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
                properties[name] = self._mass_properties_document(part, require_material)
                properties[name]["reference"]["configuration"] = referenced
                self.notes["mass_property:" + name] = properties[name]["reference"]["used_api"]
        if not components:
            raise CadError("cad_empty_model", "assembly has no resolved solid parts")
        missing_datums = sorted(requested_datums - transforms.keys())
        if missing_datums:
            raise CadError(
                "cad_missing_coordinate_system", "requested native coordinate systems were not found", missing_datums
            )
        if progress:
            progress(f"read {len(components)} leaf components; saved CAD sources hashed")
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

    def _body_face_triangles(self, bodies, body_type, component):
        """Component-local display triangles of every face of every requested body."""

        values: list[float] = []
        completed_faces = 0
        for index, body in enumerate(bodies):
            body_values: list[float] = []
            context = {
                "component": component,
                "body_type": body_type,
                "body_index": index,
                "completed_bodies": index,
                "completed_faces": completed_faces,
            }
            try:
                faces = _as_list(_member(body, "GetFaces"))
            except Exception as error:
                raise CadError(
                    "cad_body_faces_unreadable",
                    "the occurrence body's faces could not be read",
                    {**context, "api": "IBody2.GetFaces", "error": str(error)},
                ) from error
            for face_index, face in enumerate(faces):
                try:
                    face_values = list(map(float, _member(face, "GetTessTriangles", True) or ()))
                except Exception as error:
                    raise CadError(
                        "cad_face_tessellation_unreadable",
                        "the occurrence face's display triangles could not be read",
                        {
                            **context,
                            "face_index": face_index,
                            "completed_faces": completed_faces,
                            "api": "IFace2.GetTessTriangles(True)",
                            "error": str(error),
                        },
                    ) from error
                if not face_values or len(face_values) % 9 or not all(map(math.isfinite, face_values)):
                    raise CadError(
                        "cad_mesh_export_failed",
                        "invalid face tessellation",
                        {
                            "component": component,
                            "body_type": body_type,
                            "body_index": index,
                            "face_index": face_index,
                        },
                    )
                body_values.extend(face_values)
                completed_faces += 1
            if not body_values:
                raise CadError(
                    "cad_mesh_export_failed",
                    "a body carries no display tessellation",
                    {"component": component, "body_type": body_type, "body_index": index},
                )
            values.extend(body_values)
        return values

    def export_component_meshes(self, destinations, progress=None):
        """Keep one live assembly traversal through the entire geometry batch.

        IComponent2 bodies follow the occurrence's referenced configuration;
        the shared part document may have another configuration active.
        """
        missing = sorted(set(destinations) - self._components)
        if missing:
            raise CadError("cad_missing_component", missing[0])
        if len({os.path.normcase(os.path.abspath(path)) for path in destinations.values()}) != len(destinations):
            raise CadError("cad_mesh_export_failed", "each occurrence needs a distinct mesh destination")
        with self._current_components("geometry") as current:
            entries = {}
            for component in sorted(destinations):
                dest_path = destinations[component]
                try:
                    entries[component] = self._write_component_mesh(component, dest_path, current[component])
                except CadError as error:
                    detail = error.detail if isinstance(error.detail, dict) else {"native_detail": error.detail}
                    error.detail = {**detail, "component": component, "completed_components": list(entries)}
                    raise
            return entries

    def _write_component_mesh(self, component, dest_path, holder):
        """Write the occurrence's solids and sheets within its owning geometry phase."""
        sources: list[str] = []
        values: list[float] = []
        body_counts = {"solid": 0, "sheet": 0}
        for body_type, kind, source in (
            (0, "solid", "solid_body_faces"),
            (1, "sheet", "sheet_body_faces"),
        ):
            bodies = self._body_list(holder, body_type, component)
            body_counts[kind] = len(bodies)
            if bodies:
                values.extend(self._body_face_triangles(bodies, body_type, component))
                sources.append(source)
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
        # Native display tessellation, in metres, independent of global STL
        # preferences. No claim of machining-grade surface approximation.
        with open(dest_path, "xb") as handle:
            handle.write(b"SolidWorks GetTessTriangles(True), metres".ljust(80, b" "))
            handle.write(struct.pack("<I", len(values) // 9))
            for offset in range(0, len(values), 9):
                xyz = values[offset : offset + 9]
                a, b, c = xyz[:3], xyz[3:6], xyz[6:9]
                u, v = [b[i] - a[i] for i in range(3)], [c[i] - a[i] for i in range(3)]
                n = (u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2], u[0] * v[1] - u[1] * v[0])
                size = math.sqrt(sum(t * t for t in n))
                if size == 0:
                    raise CadError("cad_mesh_degenerate", component)
                handle.write(struct.pack("<12fH", *(t / size for t in n), *xyz, 0))
        try:
            triangles = read_stl(dest_path).triangles
        except (StlError, OSError) as exc:
            # SolidWorks wrote the file; a reader failure here is a CAD export
            # problem and must surface as one instead of a bare traceback.
            raise CadError("cad_mesh_invalid", str(exc), {"component": component}) from exc
        api = " + ".join(
            {
                "solid_body_faces": ("IComponent2.GetBodies2(0)/IBody2.GetFaces/IFace2.GetTessTriangles(True)"),
                "sheet_body_faces": ("IComponent2.GetBodies2(1)/IBody2.GetFaces/IFace2.GetTessTriangles(True)"),
            }[source]
            for source in sources
        )
        self.notes["mesh:" + component] = api
        return {
            "component": component,
            "written": dest_path,
            "used_api": api,
            "triangles": triangles,
            "units": "m",
            "representation": "CAD_display_tessellation",
            "tessellation_sources": sources,
            "bodies": body_counts,
        }

    def _record_source_document(self, path, configuration):
        role = self._role_for_path(path)
        key = (role, normalize_document_path(os.path.abspath(path)))
        session = self._sessions[role]
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
            manager = _member(doc, "ConfigurationManager")
            config = _member(manager, "ActiveConfiguration")
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
                suppressed = _member(comp, "IsSuppressed")
                if not isinstance(suppressed, bool):
                    raise ValueError("the occurrence has no readable suppression state")
                if suppressed:
                    continue
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
            if isinstance(error, CadError) and error.code == "cad_source_changed":
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
                facts[key] = _member(app, member)
            except Exception as exc:  # noqa: BLE001 - optional API surface
                facts[key] = None
                facts.setdefault("unavailable", {})[member] = str(exc)
        try:
            facts["visible"] = bool(_member(app, "Visible"))
        except Exception:  # noqa: BLE001
            facts["visible"] = None
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
        active = _member(_member(doc, "ConfigurationManager"), "ActiveConfiguration")
        state = {
            "path": _member(doc, "GetPathName"),
            "title": _member(doc, "GetTitle"),
            "saved": not bool(_member(doc, "GetSaveFlag")),
            "active_configuration": _member(active, "Name"),
            "configurations": self.list_configurations(path),
        }
        for key, member in (("read_only", "IsOpenedReadOnly"), ("lightweight", "IsLightWeight")):
            try:
                state[key] = bool(_member(doc, member))
            except Exception:  # noqa: BLE001 - older builds may not expose it
                state[key] = None
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

    def collect_dependencies(self, path, destination_dir):
        """Copy saved native files and rewrite references in a second application.

        ReplaceReferencedDocument requires unopened copies with the original
        internal IDs. Copy bytes, retire the source, rewrite every direct
        reference in a fresh application, then independently inspect the copy.
        """
        source_path = str(Path(path).resolve())
        source_app = self._app_for_path(source_path)
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
                if not Path(reference).is_file():
                    raise CadError("dependency_missing", reference, {"document": document})
                references.append(reference)
            graph[document] = sorted(set(references))
            pending.extend(graph[document])

        destination = Path(destination_dir).resolve()
        if destination.exists() and any(destination.iterdir()):
            raise CadError("dependency_destination_not_empty", str(destination))
        destination.mkdir(parents=True, exist_ok=True)
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

        top = mapping[source_path]
        self._capture_roots.append(normalize_document_path(str(destination)))
        self._requested_configurations[normalize_document_path(top)] = self._requested_configurations.get(
            normalize_document_path(source_path), ""
        )
        del source_app
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
            "configured": ["GetDocumentDependencies2", "ReplaceReferencedDocument"],
        }

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
        document_path = str(_member(doc, "GetPathName") or assembly_path)
        configuration = _member(_member(doc, "ConfigurationManager"), "ActiveConfiguration")
        root = _component(_member(configuration, "GetRootComponent3", True))
        instances = []
        read_only_paths = set()

        stack = [(child, "", 0) for child in reversed(_as_list(_method(root, "GetChildren")))]
        while stack:
            item, context, depth = stack.pop()
            if depth > 40:
                raise CadError("cad_assembly_too_deep", assembly_path, {"depth": depth})
            component = _component(item)
            name = str(_member(component, "Name2") or _member(component, "Name") or "")
            instance = name if not context or name.startswith(context + "/") else f"{context}/{name}"
            model = _method(component, "GetModelDoc2")
            document = _member(model, "GetPathName") if model is not None else None
            if not document:
                document = _method(component, "GetPathName")
            if model is not None and document:
                read_only_paths.add(str(document))
            referenced = _member(component, "ReferencedConfiguration")
            suppressed = bool(_member(component, "IsSuppressed"))
            instances.append(
                {
                    "instance": instance,
                    "name": name,
                    "document": str(document) if document else None,
                    "document_name": ntpath.basename(str(document)).lower() if document else None,
                    "configuration": str(referenced) if referenced is not None else None,
                    "suppressed": suppressed,
                    "depth": depth,
                }
            )
            if suppressed:
                # a suppressed placement loads no model, so its children are not
                # part of the resolved instance set of either side
                continue
            for child in reversed(_as_list(_method(component, "GetChildren"))):
                stack.append((child, instance, depth + 1))

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

    def discover_native(self, frozen_source: Path, settings: dict) -> dict:
        """Read the raw native record for CAD-only discovery (owned session).

        Opens the immutable engineering directory read-only inside this
        process's owned session and records the primitives discovery needs:
        identity properties, the component graph with occurrence transforms,
        mate features with component-frame entity geometry, coordinate systems
        (assembly and component scope), materials/masses and per-document
        hashes.  Every read fails closed; nothing is defaulted.
        """

        source_root = Path(frozen_source).resolve()
        candidates = sorted(
            path
            for path in source_root.rglob("*")
            if path.suffix.lower() == ".sldasm" and not path.name.startswith("~$") and path.is_file()
        )
        if not candidates:
            raise CadError("native_discovery_assembly_missing", "no SolidWorks assembly in the engineering directory")
        with self.session():
            assemblies = []
            for candidate in candidates:
                doc = self._ensure_document(str(candidate))
                assemblies.append((candidate, doc))
            main = None
            if len(assemblies) == 1:
                main = assemblies[0]
            else:
                referenced = set()
                for _candidate, doc in assemblies:
                    for component in _as_list(_member(doc, "GetComponents", False)) or []:
                        path = _method(_component(component), "GetPathName")
                        if _is_text_name(path):
                            referenced.add(normalize_document_path(str(path)))
                roots = [item for item in assemblies if normalize_document_path(str(item[0])) not in referenced]
                if len(roots) == 1:
                    main = roots[0]
            if main is None:
                raise CadError(
                    "native_discovery_main_assembly_ambiguous",
                    "several assemblies could be the delivered model; keep one top-level assembly",
                    {"candidates": [str(item[0]) for item in assemblies]},
                )
            main_path, doc = main
            notes: list[str] = []
            try:
                rebuilt = bool(_member(doc, "ForceRebuild3", False))
            except Exception as error:  # noqa: BLE001
                raise CadError(
                    "cad_rebuild_failed", "the assembly could not be rebuilt before capture", {"error": str(error)}
                ) from error
            if not rebuilt:
                raise CadError("cad_rebuild_failed", "ForceRebuild3 reported failure; the saved state is stale")
            active = _member(_member(doc, "ConfigurationManager"), "ActiveConfiguration")
            configuration = str(_member(active, "Name") or "")
            identity_properties = _custom_properties(doc, configuration)
            assemblies.clear()
            main = doc = active = None
            components = []
            by_component = {}
            by_document: dict[str, str] = {}
            masses = []
            mates: list[dict] = []
            stack = [
                (
                    str(main_path),
                    "",
                    configuration,
                    [[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0], [0.0, 0.0, 1.0, 0.0], [0.0, 0.0, 0.0, 1.0]],
                )
            ]
            while stack:
                assembly_path, prefix, referenced_configuration, parent_matrix = stack.pop()
                assembly = self._document_by_path(assembly_path)
                _select_configuration(assembly, referenced_configuration, prefix or "assembly")
                active_config = _member(_member(assembly, "ConfigurationManager"), "ActiveConfiguration")
                root = _component(_member(active_config, "GetRootComponent3", True))
                for raw in list(_method(root, "GetChildren") or []):
                    component = _component(raw)
                    name = str(_member(component, "Name2") or "")
                    if not name:
                        continue
                    path_name = f"{prefix}/{name}" if prefix else name
                    if path_name in by_component:
                        raise CadError(
                            "cad_component_identity_ambiguous",
                            "two native occurrences have the same scoped identity",
                            {"component": path_name, "configuration": referenced_configuration},
                        )
                    document_path = _method(component, "GetPathName")
                    relative = _relative_document(document_path, source_root) if _is_text_name(document_path) else None
                    local_transform = []
                    transform_error = "no transform API answered"
                    for probe in (("GetTotalTransform", False), ("GetTotalTransform", True)):
                        try:
                            holder = _member(component, *probe)
                            local_transform = [
                                float(value) for value in transform_from_solidworks(_member(holder, "ArrayData"))
                            ]
                            break
                        except Exception as error:  # noqa: BLE001 - an unreadable transform must block, not guess
                            transform_error = f"{probe[0]}({probe[1]}): {error}"
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
                        "document": relative
                        or (normalize_document_path(str(document_path)) if _is_text_name(document_path) else ""),
                        "configuration": str(_member(component, "ReferencedConfiguration") or ""),
                        "fixed": bool(_member(component, "IsFixed")),
                        "suppressed": bool(_member(component, "IsSuppressed")),
                        "lightweight": _optional_bool(component, "IsLightweight", "IsLightWeight"),
                        "transform": transform,
                    }
                    components.append(entry)
                    # Keep primitives beyond this traversal scope. Later
                    # configuration changes must not reuse borrowed occurrences.
                    by_component[path_name] = (str(document_path), _method(component, "GetModelDoc2") is not None)
                    by_document[path_name] = entry["document"]
                    try:
                        mass_property = _member(_member(assembly, "Extension"), "CreateMassProperty2")
                        if (
                            mass_property is not None
                            and not entry["suppressed"]
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
                        part = _method(component, "GetModelDoc2")
                        if part is not None:
                            referenced = str(_member(component, "ReferencedConfiguration") or "")
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
                                )
                            )
                for feature, specific, entity_count in _mate_features(assembly):
                    name = str(_member(feature, "Name") or "")
                    try:
                        type_index = _method(specific, "Type")
                        if type(type_index) is not int:
                            raise ValueError("mate type is not a native integer")
                    except Exception as error:  # noqa: BLE001
                        raise CadError(
                            "cad_mate_unreadable", "mate type could not be read", {"mate": name, "error": str(error)}
                        ) from error
                    mate_type = _SW_MATE_TYPES.get(type_index, f"unknown-{type_index}")
                    entities = []
                    for entity_index in range(entity_count):
                        try:
                            entity = _dynamic(_method(specific, "MateEntity", entity_index))
                            reference = _component(_member(entity, "ReferenceComponent"))
                            reference_name = str(_member(reference, "Name2") or "")
                            reference_path = _relative_document(_method(reference, "GetPathName"), source_root)
                        except Exception as error:  # noqa: BLE001
                            raise CadError(
                                "cad_mate_unreadable",
                                "a mate entity could not be read",
                                {"mate": name, "error": str(error)},
                            ) from error
                        target = _member(entity, "Reference")
                        entities.append(
                            {
                                "reference_name": reference_name,
                                "reference_document": reference_path,
                                "feature": _feature_name(target),
                                "face_index": None,
                                **_plane_or_cylinder(target),
                            }
                        )
                    limits = None
                    try:
                        lower = float(_method(specific, "MinimumVariation"))
                        upper = float(_method(specific, "MaximumVariation"))
                    except Exception as error:  # noqa: BLE001
                        raise CadError(
                            "cad_mate_limits_unreadable",
                            "mate travel variation could not be read",
                            {"mate": name, "type": mate_type, "error": str(error)},
                        ) from error
                    if not (math.isfinite(lower) and math.isfinite(upper)):
                        raise CadError("cad_mate_limits_invalid", "mate travel variation is not finite", {"mate": name})
                    if upper < lower:
                        raise CadError(
                            "cad_mate_limits_invalid",
                            "mate travel range is inverted",
                            {"mate": name, "lower": lower, "upper": upper},
                        )
                    if upper > lower + 1e-12:
                        unit = "m" if type_index == 5 else "rad" if type_index == 6 else None
                        if unit is None:
                            raise CadError(
                                "cad_mate_limits_invalid",
                                "a bounded range on an unsupported mate type cannot be observed",
                                {"mate": name, "type": mate_type},
                            )
                        limits = {"lower": lower, "upper": upper, "unit": unit}
                        mate_type = "limitdistance" if type_index == 5 else "limitangle"
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
                    mates.append(
                        {
                            "name": name,
                            "type": mate_type,
                            "suppressed": suppressed,
                            "limits": limits,
                            "entities": entities,
                            "error_code": error_code,
                            "scope": prefix or "",
                            "configuration": referenced_configuration,
                        }
                    )
                # The next assembly selection may invalidate this entire borrowed
                # tree. Pending assemblies and occurrence records contain paths
                # and primitives only; release all native traversal handles now.
                component = raw = root = active_config = part = children = holder = mass_property = None
                entity = reference = target = feature = specific = assembly = None
            # Top-level mates can name descendants that are visited later.
            # Resolve the full scoped occurrence; leaf-name matching loses
            # identity when the same part is inserted more than once.
            for mate in mates:
                for entity in mate["entities"]:
                    reference_name = entity.pop("reference_name")
                    reference_document = entity.pop("reference_document")
                    scope = mate["scope"]
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
                    entity["component"] = scoped_name
            datums = []
            doc = self._document_by_path(str(main_path))
            _select_configuration(doc, configuration, "assembly")
            for name in _coordinate_system_features(doc):
                matrix = [float(value) for value in self._coordinate_system_transform(doc, name)]
                datums.append({"name": name, "owner": "", "array": matrix, "configuration": configuration})
            for entry in components:
                source = by_component.get(entry["name2"])
                if source is None or not source[1] or entry["suppressed"]:
                    continue
                if not entry["transform"]:
                    continue
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
                        }
                    )
            property_buckets = {"document": identity_properties, "components": {}, "mates": {}}
            for entry in components:
                source = by_component.get(entry["name2"])
                if source is None or not source[1] or entry["suppressed"]:
                    continue
                document = self._document_by_path(source[0])
                _select_configuration(document, entry["configuration"], entry["name2"])
                values = _custom_properties(document, entry["configuration"] or None)
                if values:
                    property_buckets["components"][entry["name2"]] = values
            files = {}
            for path in (main_path, *[Path(item[0]) for item in by_component.values()]):
                relative = _relative_document(path, source_root)
                if relative and relative not in files:
                    files[relative] = _hash(str(Path(source_root) / relative))
            return {
                "schema_version": "solidworks-to-urdf.native-discovery/v1",
                "contract": "native-discovery/v1",
                "namespace": "dp",
                "generator": "solidworks-native-reader",
                "solidworks": dict(self.environment()),
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
                "masses": masses,
                "properties": property_buckets,
                "files": files,
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
