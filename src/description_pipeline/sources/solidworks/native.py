"""Explicit, fail-closed SolidWorks COM access (ported from tools/solidworks_export).

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


def _member(obj, name, *args):
    # Flag methods BEFORE getattr: late binding may otherwise invoke them as
    # zero-argument properties, even crashing an incorrectly invoked CAD API.
    if args and hasattr(obj, "_FlagAsMethod"):
        obj._FlagAsMethod(name)
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

    if hasattr(obj, "_FlagAsMethod"):
        obj._FlagAsMethod(name)
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


def _parallel_axis_terms(mass, com):
    """Rotational moments, in SolidWorks positive-product notation."""
    x, y, z = map(float, com)
    return (
        mass * (y * y + z * z),
        mass * (x * x + z * z),
        mass * (x * x + y * y),
        mass * x * y,
        mass * x * z,
        mass * y * z,
    )


def _inertia_from_raw(values, component, mass=None, com=None):
    """Parse full9; explicitly paired legacy six-value groups are checked.

    Live COM uses only GetMomentOfInertia(0)'s documented nine-value result.
    Array length never determines an unknown API's reference point or axes.
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
    if len(data) == 12:
        if mass is None or com is None or mass <= 0 or not math.isfinite(mass):
            raise CadError("cad_mass_property_inertia_ambiguous", component)
        shift = _parallel_axis_terms(mass, com)
        tolerance = 1e-10 * max(max(map(abs, data)), max(map(abs, shift)), 1e-30)
        first, second = data[:6], data[6:]
        candidates = [
            (a, label)
            for a, b, label in ((first, second, "com_first"), (second, first, "com_second"))
            if all(abs(o - c - s) <= tolerance for c, o, s in zip(a, b, shift, strict=True))
        ]
        if not candidates or (
            len(candidates) == 2 and any(abs(a - b) > tolerance for a, b in zip(first, second, strict=True))
        ):
            raise CadError("cad_mass_property_inertia_ambiguous", component)
        (xx, yy, zz, xy, xz, yz), label = candidates[0]
        return ((xx, xy, xz), (xy, yy, yz), (xz, yz, zz)), "six6:validated:" + label
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
        self._components = {}
        self._source_components = {}
        self._doc = None
        self.notes = {}
        self.source_files = {}
        #: ``GetSaveFlag`` per working-tree document, recorded as evidence.
        self.save_flags: dict[str, bool] = {}
        self._source_configuration = None

    def _app_obj(self):
        role = getattr(self._local, "role", "source")
        with self._sessions_lock:
            current = threading.current_thread()
            if self._owner_thread is not None and self._owner_thread is not current:
                raise EnvironmentError_("cad_thread_mismatch", "CAD proxies must stay on their owning STA thread")
            self._owner_thread = current
            session = self._sessions.get(role)
        if session is None:
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
                session.connect(self._cancelled)
            except BaseException:
                session.close()
                raise
        if self._cancelled.is_set():
            raise EnvironmentError_("cad_session_cancelled", "Capture was cancelled")
        return session.app

    def _app_for_path(self, path):
        previous = getattr(self._local, "role", "source")
        normalized = normalize_document_path(os.path.abspath(path))
        self._local.role = (
            "copy" if any(normalized.startswith(root + "\\") for root in self._capture_roots) else "source"
        )
        try:
            return self._app_obj()
        finally:
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
        self._doc = None
        self.save_flags.clear()
        with self._sessions_lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
            self._owner_thread = None
        errors = []
        for session in sessions:
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
                materials = {
                    "schema_version": "swbridge.material-assignment/v1",
                    "configuration": str(_member(active, "Name")),
                    "query_configuration": "",
                    "unverified_reason": exc.code,
                    "message": exc.message,
                }
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
            "reference": {
                "used_api": "IMassProperty2.GetMomentOfInertia(0)",
                "reference_point": "center_of_mass",
                "axes": "part_document_axes",
                "use_system_units": True,
                "product_convention": "solidworks_positive",
                "volume_m3": float(_member(mp, "Volume")),
                "density_kg_m3": float(_member(mp, "Density")),
                "overrides": overrides,
                "body_count": len(bodies),
                "part_document": str(_member(doc, "GetPathName")),
                "configuration": materials["configuration"],
                "material_assignment": materials,
            },
        }

    def collect_scene(self, doc_path, coordinate_systems, progress=None, require_material=True):
        doc = self._document_by_path(doc_path)
        if _member(doc, "GetType") != 2:
            raise CadError("cad_not_assembly", "export requires a saved SLDASM")
        self._record_save_flag(doc, doc_path)
        self._doc = doc
        self._components = {}
        self._source_components = {}
        self.notes = {}
        self.source_files = {doc_path: _hash(doc_path)}
        config = _member(_member(doc, "ConfigurationManager"), "ActiveConfiguration")
        self._source_configuration = str(_member(config, "Name"))
        root = _member(config, "GetRootComponent3", True)
        stack = list(_member(root, "GetChildren") or ())
        components, properties = [], {}
        while stack:
            comp = _dynamic(stack.pop())
            if _member(comp, "IsSuppressed"):
                continue
            name = str(_member(comp, "Name2"))
            children = list(_member(comp, "GetChildren") or ())
            part = _member(comp, "GetModelDoc2")
            if part is None:
                raise CadError("cad_component_unresolved", name)
            _read_only_document(part)
            path = _member(comp, "GetPathName")
            if not path or not os.path.isfile(path):
                # Without a file on disk there is no revision to hash or copy, so the
                # snapshot could not name what it read.
                raise CadError("cad_component_not_on_disk", name, {"component": name, "path": path})
            self._record_save_flag(part, path)
            active = _member(_member(part, "ConfigurationManager"), "ActiveConfiguration")
            referenced = _member(comp, "ReferencedConfiguration")
            if _member(active, "Name") != referenced:
                raise CadError(
                    "cad_configuration_mismatch", name, {"referenced": referenced, "active": _member(active, "Name")}
                )
            # Intermediate assemblies own placements/configurations too. Their
            # saved bytes and in-memory state are part of the source closure.
            self._source_components[name] = (comp, str(referenced))
            if path not in self.source_files:
                self.source_files[path] = _hash(path)
            if children:
                if _member(part, "GetType") != 2:
                    raise CadError("cad_component_type", name)
                stack.extend(children)
                continue
            if _member(part, "GetType") != 1:
                raise CadError("cad_empty_subassembly", name)
            components.append(RawComponent(name, path, self._placement(comp), bool(_member(comp, "IsFixed")), "part"))
            self._components[name] = comp
            properties[name] = self._mass_properties_document(part, require_material)
            properties[name]["reference"]["configuration"] = referenced
            self.notes["mass_property:" + name] = properties[name]["reference"]["used_api"]
        if not components:
            raise CadError("cad_empty_model", "assembly has no resolved solid parts")
        transforms = {name: self._coordinate_system_transform(doc, name) for name in coordinate_systems}
        if progress:
            progress(f"read {len(components)} leaf components; saved CAD sources hashed")
        return RawScene(doc_path, components, transforms, properties, dict(self.notes))

    def _coordinate_system_transform(self, doc, name):
        tf = _member(_member(doc, "Extension"), "GetCoordinateSystemTransformByName", name)
        if tf is None:
            raise CadError("cad_missing_coordinate_system", name)
        self.notes["coordinate_system:" + name] = "IModelDocExtension.GetCoordinateSystemTransformByName"
        return transform_from_solidworks(_member(tf, "ArrayData"))

    def export_component_mesh(self, component, dest_path, progress=None):
        if component not in self._components:
            raise CadError("cad_missing_component", component)
        doc = _member(self._components[component], "GetModelDoc2")
        values = list(map(float, _member(doc, "GetTessTriangles", True) or ()))
        if not values or len(values) % 9 or not all(map(math.isfinite, values)):
            raise CadError("cad_mesh_export_failed", "invalid tessellation", {"component": component})
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
        api = "IPartDoc.GetTessTriangles(True)"
        self.notes["mesh:" + component] = api
        return {
            "component": component,
            "written": dest_path,
            "used_api": api,
            "triangles": triangles,
            "units": "m",
            "representation": "CAD_display_tessellation",
        }

    def verify_sources_unchanged(self):
        active = _member(_member(self._doc, "ConfigurationManager"), "ActiveConfiguration")
        configuration = _member(active, "Name")
        if configuration != self._source_configuration:
            raise CadError(
                "cad_source_changed",
                "assembly changed in memory during export",
                {
                    "path": _member(self._doc, "GetPathName"),
                    "before": {"configuration": self._source_configuration},
                    "after": {"configuration": configuration},
                },
            )
        for name, (comp, referenced) in self._source_components.items():
            doc = _member(comp, "GetModelDoc2")
            if doc is None:
                raise CadError("cad_source_changed", name)
            active = _member(_member(doc, "ConfigurationManager"), "ActiveConfiguration")
            state = {
                "configuration": _member(active, "Name"),
                "referenced_configuration": _member(comp, "ReferencedConfiguration"),
            }
            expected = {"configuration": referenced, "referenced_configuration": referenced}
            if state != expected:
                raise CadError(
                    "cad_source_changed",
                    name,
                    {"path": _member(doc, "GetPathName"), "before": expected, "after": state},
                )
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
        internal IDs. Copy bytes first, rewrite every direct reference, then let
        freeze independently reopen and compare the entire assembly.
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
        root = _member(configuration, "GetRootComponent3", True)
        instances = []

        stack = [(child, "", 0) for child in reversed(_as_list(_member(root, "GetChildren")))]
        while stack:
            item, context, depth = stack.pop()
            if depth > 40:
                raise CadError("cad_assembly_too_deep", assembly_path, {"depth": depth})
            component = _dynamic(item)
            name = str(_member(component, "Name2") or _member(component, "Name") or "")
            instance = name if not context or name.startswith(context + "/") else f"{context}/{name}"
            model = _member(component, "GetModelDoc2")
            if model is not None:
                _read_only_document(model)
            document = _member(model, "GetPathName") if model is not None else None
            if not document:
                document = _member(component, "GetPathName")
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
            for child in reversed(_as_list(_member(component, "GetChildren"))):
                stack.append((child, instance, depth + 1))

        return {
            "document": str(opened.get("path") or document_path),
            "configuration": _active_configuration(doc),
            "components": len(instances),
            # A suppressed instance is not evidence of an escape: it is recorded
            # separately and still has to match between source and copy.
            "unresolved": [
                entry["instance"] for entry in instances if not entry["document"] and not entry["suppressed"]
            ],
            "suppressed": [entry["instance"] for entry in instances if entry["suppressed"]],
            "instances": instances,
        }

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
