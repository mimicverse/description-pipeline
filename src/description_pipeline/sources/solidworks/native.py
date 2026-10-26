"""Explicit, fail-closed SolidWorks COM access for direct native facts.

Documents open read-only from prepared files. Geometry is read directly from
each actual occurrence's faces; no rebuild, document copy or in-session topology
derivation is performed. Failures retain stable codes and only owned process
trees are reclaimed.
"""

from __future__ import annotations

import copy
import hashlib
import math
import ntpath
import os
import posixpath
import re
import sys
import threading
import time
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path

from ...geometry.native_scope import NATIVE_SOURCE_SCOPE_RULE
from ...geometry.native_transform import transform_from_solidworks as _shared_transform
from ...schemas import NATIVE_CAPTURE_FACTS_KEYS, NATIVE_DISCOVERY_SCHEMA
from .errors import CadError, EnvironmentError_
from .protocol import (
    CadBackend,
    Matrix,
    RawComponent,
    RawScene,
)
from .session_artifacts import (
    SESSION_DOCUMENT_API,
    embedded_virtual_component_marker,
    embedded_virtual_component_path,
    embedded_virtual_component_root,
    session_artifact_path,
    session_materialization_path,
    session_root_pid,
    session_temp_root,
)

# Published dual-interface IIDs in the SolidWorks type library.
ICOMPONENT2_IID = "{655D6F2A-5441-45D1-8CBA-D35FB26988E4}"
IMODELDOC2_IID = "{B90793FB-EF3D-4B80-A5C4-99959CDB6CEB}"
IPARTDOC_IID = "{83A33D32-27C5-11CE-BFD4-00400513BB57}"
ICONFIGURATIONMANAGER_IID = "{8DB64337-F36E-47CC-BBBC-B4B979D31505}"
ICONFIGURATION_IID = "{83A33D98-27C5-11CE-BFD4-00400513BB57}"
IMODELDOCEXTENSION_IID = "{99F4D4AF-F268-4EE1-8C55-041F7BECF879}"
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
ISURFACE_IID = "{83A33D40-27C5-11CE-BFD4-00400513BB57}"
ICURVE_IID = "{83A33D44-27C5-11CE-BFD4-00400513BB57}"
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
    "GetTotalTransform": IMATHTRANSFORM_IID,
    "OpenDoc6": IMODELDOC2_IID,
}

# Members whose declared return is an array of one published interface.
RETURN_ARRAY_VIEWS = {
    "GetBodies2": IBODY2_IID,
    "GetFaces": IFACE2_IID,
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


def _component(value):
    """Bind a native occurrence to its published component interface."""
    return _interface(value, ICOMPONENT2_IID)


def _modeldoc2(value):
    """Bind a native document dispatch to the common IModelDoc2 interface."""
    return _interface(value, IMODELDOC2_IID)


def _partdoc(value):
    """Bind a part document dispatch to the IPartDoc domain interface."""
    return _interface(value, IPARTDOC_IID)


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
    # OpenDoc6's read-only option does not propagate to loaded references.
    state = _member(doc, "IsOpenedReadOnly")
    if state is False:
        _member(doc, "SetReadOnlyState", True)
        state = _member(doc, "IsOpenedReadOnly")
    if state is not True:
        raise CadError(
            "cad_read_only_failed",
            "The capture document is not verifiably read-only",
            {"path": _member(doc, "GetPathName"), "read_only": _raw_scalar(state)},
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


def _active_configuration(doc):
    """Name of the configuration a document currently has active."""

    _manager, active = _active_configuration_view(doc)
    name = _member(active, "Name") if active is not None else None
    return name if _is_text_name(name) else None


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
            state[name] = _raw_scalar(value)
        except Exception as error:  # noqa: BLE001 - unread state is explicit, never assumed clean
            state[name] = None
            state[name + "_error"] = f"{type(error).__name__}: {error}"[:300]
        else:
            if type(value) is not expected:
                state[name + "_error"] = (
                    f"unexpected native value {value!r} with type {type(value).__name__}"
                )
    try:
        _manager, active = _active_configuration_view(doc)
    except Exception as error:  # noqa: BLE001
        state["configuration"] = None
        state["configuration_error"] = f"{type(error).__name__}: {error}"[:300]
        state["configuration_needs_rebuild"] = None
        state["configuration_needs_rebuild_error"] = f"{type(error).__name__}: {error}"[:300]
    else:
        try:
            configuration = _member(active, "Name")
        except Exception as error:  # noqa: BLE001
            state["configuration"] = None
            state["configuration_error"] = f"{type(error).__name__}: {error}"[:300]
        else:
            state["configuration"] = _raw_scalar(configuration)
            if not isinstance(configuration, str) or not configuration.strip():
                state["configuration_error"] = (
                    f"unexpected native value {configuration!r} with type {type(configuration).__name__}"
                )
        try:
            value = _member(active, "NeedsRebuild")
        except Exception as error:  # noqa: BLE001
            state["configuration_needs_rebuild"] = None
            state["configuration_needs_rebuild_error"] = f"{type(error).__name__}: {error}"[:300]
        else:
            state["configuration_needs_rebuild"] = _raw_scalar(value)
            if type(value) is not bool:
                state["configuration_needs_rebuild_error"] = (
                    f"unexpected native value {value!r} with type {type(value).__name__}"
                )
    return state


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
    if not isinstance(values, (list, tuple)) or any(
        type(value) not in (int, float) for value in values
    ):
        raise CadError(
            "cad_mass_property_inertia_type",
            component,
            {"value_type": type(values).__name__},
        )
    data = [float(value) for value in values]
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
    """Adapt the shared pure conversion to the backend's stable error contract."""

    if not isinstance(values, (list, tuple)) or any(
        type(value) not in (int, float) for value in values
    ):
        raise CadError(
            "cad_transform_invalid",
            "expected a raw array of finite non-boolean numbers",
            {"value": _raw_scalar(values)},
        )
    try:
        return tuple(_shared_transform(values))
    except (TypeError, ValueError) as error:
        message = str(error)
        if "scaled" in message:
            code = "cad_transform_scaled"
        elif "reflection" in message:
            code = "cad_transform_reflection"
        else:
            code = "cad_transform_invalid"
        raise CadError(code, message, values) from error


def _raw_mass_fact(reading):
    """Keep getter values and identity while dropping projected tensors."""

    native_raw = reading.get("native_raw")
    if not isinstance(native_raw, dict):
        raise CadError("cad_mass_property_unavailable", "a mass reading has no native getter values")
    result = {
        "native_raw": _json_raw_value(native_raw),
        "reference": _json_raw_value(reading.get("reference") or {}),
        "read_errors": _json_raw_value(reading.get("read_errors") or {}),
    }
    for key in ("mode", "role", "state", "measurement_basis", "document_type"):
        if reading.get(key) is not None:
            result[key] = reading[key]
    if reading.get("part_measurement_raw") is not None:
        result["part_measurement_raw"] = _json_raw_value(reading["part_measurement_raw"])
    return result


def _raw_scalar(value):
    if isinstance(value, float) and not math.isfinite(value):
        return {
            "type": "float",
            "repr": repr(value),
            "nonfinite": True,
        }
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    return {
        "type": type(value).__name__,
        "repr": repr(value)[:300],
    }


def _json_raw_value(value):
    if isinstance(value, dict):
        return {str(key): _json_raw_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_raw_value(item) for item in value]
    return _raw_scalar(value)


def _appearance_values_are_valid(value: object) -> bool:
    """Whether one native material-property reading is the documented 9-vector."""

    return (
        isinstance(value, list)
        and len(value) == 9
        and all(
            type(item) in (int, float) and math.isfinite(float(item))
            for item in value
        )
    )


def _appearance_read_error(code: str, message: str, value: object) -> dict:
    return {
        "code": code,
        "message": message,
        "detail": {"value": copy.deepcopy(value)},
    }


def _appearance_value(owner, name: str, *, method: bool = False, args=()):
    """Read one auxiliary native value without converting an error into null."""

    try:
        value = _method(owner, name, *args) if method else _member(owner, name)
    except Exception as error:  # noqa: BLE001 - caller records the field failure
        return None, _native_error_record(error)
    return _json_raw_value(value), None


def _appearance_persistent_reference(extension, entity):
    if extension is None:
        return None, {
            "code": "cad_appearance_owner_unavailable",
            "message": "the apparent owner document extension is unavailable",
            "detail": {},
        }
    try:
        raw = _member(extension, "GetPersistReference3", entity)
    except Exception as error:  # noqa: BLE001 - identity failure is field-local
        return None, _native_error_record(error)
    if raw is None:
        return None, _appearance_read_error(
            "cad_appearance_persistent_reference_missing",
            "GetPersistReference3 returned null",
            None,
        )
    if isinstance(raw, memoryview):
        raw = raw.tobytes()
    if isinstance(raw, (bytes, bytearray)):
        value = list(raw)
    elif isinstance(raw, (list, tuple)):
        value = [_json_raw_value(item) for item in raw]
    else:
        value = _json_raw_value(raw)
    if (
        not isinstance(value, list)
        or not value
        or any(type(item) is not int or not -128 <= item <= 255 for item in value)
    ):
        return value, _appearance_read_error(
            "cad_appearance_persistent_reference_invalid",
            "GetPersistReference3 did not return a nonempty byte array",
            value,
        )
    return value, None


def _appearance_reference_key(value):
    if not isinstance(value, list) or not value:
        return None
    if any(type(item) is not int for item in value):
        return None
    return tuple(item & 0xFF for item in value)


def _appearance_feature_identity(face, extension):
    errors = {}
    try:
        feature = _member(face, "GetFeature")
    except Exception as error:  # noqa: BLE001 - identity remains field-local
        return {
            "name": None,
            "type_name": None,
            "persistent_reference": None,
            "read_errors": {"feature": _native_error_record(error)},
        }, None
    if feature is None:
        return {
            "name": None,
            "type_name": None,
            "persistent_reference": None,
            "read_errors": {},
        }, None
    name, name_error = _appearance_value(feature, "Name")
    type_name, type_error = _appearance_value(feature, "GetTypeName2", method=True)
    reference, reference_error = _appearance_persistent_reference(
        extension,
        feature,
    )
    if name_error is not None:
        errors["name"] = name_error
    if type_error is not None:
        errors["type_name"] = type_error
    if reference_error is not None:
        errors["persistent_reference"] = reference_error
    return {
        "name": name,
        "type_name": type_name,
        "persistent_reference": reference,
        "read_errors": errors,
    }, feature


def _appearance_render_material(material, index: int, extension) -> dict:
    errors = {}
    linked_display_states, error = _appearance_value(
        material,
        "GetLinkedDisplayStates",
        method=True,
    )
    if error is not None:
        errors["linked_display_states"] = error
    try:
        raw_entities = _method(material, "GetEntities")
    except Exception as error:  # noqa: BLE001 - retain entity read failure
        raw_entities = None
        entities = None
        entity_objects = []
        entities_state = "error"
        errors["entities"] = _native_error_record(error)
    else:
        entities = _json_raw_value(raw_entities)
        entity_objects = _as_list(raw_entities)
        entities_state = (
            "null_result"
            if raw_entities is None
            else "empty"
            if not entity_objects
            else "observed"
        )
    entity_records = []
    for entity_index, entity in enumerate(entity_objects):
        reference, reference_error = _appearance_persistent_reference(
            extension,
            entity,
        )
        record_errors = {}
        if reference_error is not None:
            record_errors["persistent_reference"] = reference_error
        entity_records.append(
            {
                "entity_index": entity_index,
                "native_object_type": type(entity).__name__,
                "persistent_reference": reference,
                "read_errors": record_errors,
            }
        )
    fields = {}
    for key, name in (
        ("primary_color", "PrimaryColor"),
        ("transparency", "Transparency"),
        ("color_form", "ColorForm"),
        ("texture_filename", "TextureFilename"),
        ("surface_finish_file", "SurfaceFinishFile"),
    ):
        value, error = _appearance_value(material, name)
        fields[key] = value
        if error is not None:
            errors[key] = error
    return {
        "material_index": index,
        "linked_display_states": linked_display_states,
        "entities": entities,
        "entities_state": entities_state,
        "entity_records": entity_records,
        "primary_color": fields["primary_color"],
        "transparency": fields["transparency"],
        "color_form": fields["color_form"],
        "texture_filename": fields["texture_filename"],
        "surface_finish_file": fields["surface_finish_file"],
        "read_errors": errors,
    }


def _appearance_display_state(holder):
    try:
        raw = _member(holder, "ReferencedDisplayState2")
    except Exception as error:  # noqa: BLE001 - retain a field-local read failure
        return None, _native_error_record(error)
    value = _json_raw_value(raw)
    if not _is_text_name(raw):
        return value, _appearance_read_error(
            "cad_appearance_display_state_invalid",
            "ReferencedDisplayState2 did not return a nonempty string",
            value,
        )
    return value, None


def _appearance_document_display_state(document):
    try:
        _manager, active = _active_configuration_view(document)
        if active is None:
            raise ValueError("the owner document has no active configuration")
        configuration = str(_member(active, "Name"))
    except Exception as error:  # noqa: BLE001 - active owner failure
        return None, None, _native_error_record(error), None, "error"
    try:
        raw_states = _method(active, "GetDisplayStates")
    except Exception as error:  # noqa: BLE001 - display-state field failure
        return (
            configuration,
            None,
            _native_error_record(error),
            None,
            "error",
        )
    states_raw = _json_raw_value(raw_states)
    state_error = None
    if raw_states is None:
        states = []
        states_state = "null_result"
    elif isinstance(raw_states, (list, tuple)):
        states = _as_list(raw_states)
        states_state = "empty" if not states else "observed"
    else:
        states = []
        states_state = "error"
        state_error = _appearance_read_error(
            "cad_appearance_display_states_invalid",
            "GetDisplayStates did not return null or an array",
            states_raw,
        )
    display_state = states[0] if states else None
    if not _is_text_name(configuration):
        state_error = _appearance_read_error(
            "cad_appearance_active_configuration_invalid",
            "the owner document active configuration is unreadable",
            _json_raw_value(configuration),
        )
    elif state_error is None and not _is_text_name(display_state):
        state_error = _appearance_read_error(
            "cad_appearance_display_state_missing",
            "GetDisplayStates returned no active display-state name",
            states_raw,
        )
    return (
        configuration,
        _json_raw_value(display_state),
        state_error,
        states_raw,
        states_state,
    )


def _state_render_materials_raw(
    document,
    *,
    configuration,
    display_state,
) -> tuple[dict, dict | None]:
    """Read one exact document/configuration/display-state owner result."""

    record = {
        "api": "IModelDocExtension.GetRenderMaterials2",
        "display_state_option": 3,
        "display_state": display_state,
        "native_render_material_count": None,
        "native_render_material_count_error": None,
        "call_state": "error",
        "materials": [],
        "read_errors": {},
    }
    if configuration is not None:
        record["configuration"] = _json_raw_value(configuration)
    if document is None:
        record["read_errors"]["document"] = {
            "code": "cad_appearance_owner_unavailable",
            "message": "the apparent owner document is unavailable",
            "detail": {},
        }
        return record, None
    if not _is_text_name(display_state):
        record["call_state"] = "state_unavailable"
        return record, None
    try:
        import pythoncom

        extension = _member(document, "Extension")
        try:
            native_count = _method(
                extension,
                "GetRenderMaterialsCount",
            )
        except Exception as error:  # noqa: BLE001 - count stays field-local
            count_error = _native_error_record(error)
        else:
            if type(native_count) is int and native_count >= 0:
                record["native_render_material_count"] = native_count
                count_error = None
            else:
                count_error = _appearance_read_error(
                    "cad_appearance_render_material_count_invalid",
                    "GetRenderMaterialsCount did not return a nonnegative integer",
                    _json_raw_value(native_count),
                )
        if count_error is not None:
            record["native_render_material_count_error"] = count_error
            record["read_errors"]["native_render_material_count"] = copy.deepcopy(
                count_error
            )
        display_state_names = _win32().VARIANT(
            pythoncom.VT_ARRAY | pythoncom.VT_BSTR,
            (display_state,),
        )
        raw_materials = _member(
            extension,
            "GetRenderMaterials2",
            3,
            display_state_names,
        )
    except Exception as error:  # noqa: BLE001 - appearance remains field-local
        record["read_errors"]["materials"] = _native_error_record(error)
        return record, None
    materials = _as_list(raw_materials)
    if raw_materials is None:
        record["call_state"] = "null_result"
        record["materials"] = None
    elif not materials:
        record["call_state"] = "empty"
    else:
        record["call_state"] = "observed"
        record["materials"] = [
            _appearance_render_material(material, index, extension)
            for index, material in enumerate(materials)
        ]
    return record, extension


def _appearance_texture_raw(owner, configuration, read_errors):
    """Read one documented texture pointer without making it a primary gate."""

    try:
        texture = _member(owner, "GetTexture", configuration)
    except Exception as error:  # noqa: BLE001 - appearance is auxiliary evidence
        read_errors["texture"] = _native_error_record(error)
        return {
            "configuration": configuration,
            "state": "unreadable",
            "material_name": None,
        }
    if texture is None:
        return {
            "configuration": configuration,
            "state": "absent",
            "material_name": None,
        }
    try:
        raw_name = _member(texture, "MaterialName")
    except Exception as error:  # noqa: BLE001 - preserve the applied texture
        read_errors["texture.material_name"] = _native_error_record(error)
        return {
            "configuration": configuration,
            "state": "present",
            "material_name": None,
        }
    if not isinstance(raw_name, str):
        read_errors["texture.material_name"] = _appearance_read_error(
            "cad_appearance_texture_name_invalid",
            "ITexture.MaterialName did not return text",
            _json_raw_value(raw_name),
        )
        return {
            "configuration": configuration,
            "state": "present",
            "material_name": None,
        }
    return {
        "configuration": configuration,
        "state": "present",
        "material_name": raw_name,
    }


_DEFAULT_APPEARANCE_OWNER = object()


def _appearance_raw(
    owner,
    *,
    configuration,
    material_member: str,
    material_args=(),
    material_context: dict,
    used_api: str,
    presence_owner=_DEFAULT_APPEARANCE_OWNER,
    presence_api: str | None = None,
    texture_owner=_DEFAULT_APPEARANCE_OWNER,
):
    """Record one exact appearance owner without coercing absence or failure."""

    presence_owner = (
        owner
        if presence_owner is _DEFAULT_APPEARANCE_OWNER
        else presence_owner
    )
    texture_owner = (
        owner
        if texture_owner is _DEFAULT_APPEARANCE_OWNER
        else texture_owner
    )
    same_owner_presence = presence_owner is owner
    read_errors = {}
    material_read_failed = False
    try:
        material_values = _json_raw_value(
            _member(owner, material_member, *material_args)
        )
    except Exception as error:  # noqa: BLE001 - appearance is auxiliary evidence
        material_read_failed = True
        material_values = None
        read_errors["material_property_values"] = _native_error_record(error)
    else:
        if material_values is not None and not _appearance_values_are_valid(material_values):
            read_errors["material_property_values"] = _appearance_read_error(
                "cad_appearance_values_invalid",
                f"{material_member} did not return nine finite numeric values",
                material_values,
            )
    try:
        if presence_owner is None:
            raise AttributeError("the diagnostic appearance owner is unavailable")
        raw_has_values = _method(
            presence_owner,
            "HasMaterialPropertyValues",
        )
    except Exception as error:  # noqa: BLE001 - appearance is auxiliary evidence
        has_values = None
        read_errors["has_material_property_values"] = _native_error_record(error)
    else:
        has_values = raw_has_values if type(raw_has_values) is bool else None
        if has_values is None:
            read_errors["has_material_property_values"] = _appearance_read_error(
                "cad_appearance_presence_invalid",
                "HasMaterialPropertyValues did not return bool",
                _json_raw_value(raw_has_values),
            )
    if (
        material_values is None
        and not material_read_failed
        and same_owner_presence
        and has_values is True
    ):
        read_errors["material_property_values"] = _appearance_read_error(
            "cad_appearance_values_unresolved",
            f"{material_member} returned null while the same owner reports material property values",
            None,
        )
    return {
        "used_api": used_api,
        "material_call": material_context,
        "presence_call": {
            "arguments": [],
            "configuration_semantics": "native_owner_context",
            "same_owner": same_owner_presence,
            **({"owner_api": presence_api} if presence_api else {}),
        },
        "material_property_values": material_values,
        "has_material_property_values": has_values,
        "texture": _appearance_texture_raw(
            texture_owner,
            configuration,
            read_errors,
        ),
        "read_errors": read_errors,
    }


def _unavailable_appearance_raw(
    *,
    configuration,
    material_context: dict,
    used_api: str,
    error: object,
) -> dict:
    return {
        "used_api": used_api,
        "material_call": material_context,
        "presence_call": {"arguments": [], "configuration_semantics": "native_owner_context"},
        "material_property_values": None,
        "has_material_property_values": None,
        "texture": {
            "configuration": configuration,
            "state": "unreadable",
            "material_name": None,
        },
        "read_errors": {"owner": copy.deepcopy(error)},
    }


def _optional_mass_scalar(mass_property, member, read_errors):
    key = {
        "Volume": "volume_m3",
        "Density": "density_kg_m3",
    }.get(member, member)
    try:
        return _json_raw_value(_member(mass_property, member))
    except Exception as error:  # noqa: BLE001 - auxiliary getter cannot hide primary mass
        read_errors[key] = _native_error_record(error)
        return None


def _read_mass_primary(mass_property, read_errors):
    values = {}
    for key, member, args in (
        ("mass_kg", "Mass", ()),
        ("center_of_mass_m", "CenterOfMass", ()),
        ("inertia_values", "GetMomentOfInertia", (0,)),
    ):
        try:
            value = _member(mass_property, member, *args)
        except Exception as error:  # noqa: BLE001 - each primary getter stands alone
            read_errors[key] = _native_error_record(error)
        else:
            values[key] = _json_raw_value(value)
    return values


def _raw_component_mass_fact(reading):
    result = copy.deepcopy(reading)
    for row in result.get("instances") or []:
        if isinstance(row, dict) and isinstance(row.get("effective"), dict):
            row["effective"] = _raw_mass_fact(row["effective"])
    return _json_raw_value(result)




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


def _capture_reference_axis_raw(doc, name, component="", selector=None):
    """Read one exact RefAxis feature and its endpoints without derived math."""

    detail = {"component": component, "feature_name": name}
    record = {
        "kind": "reference_axis",
        "component": component,
        "selector": dict(selector or {"kind": "reference_axis", **detail}),
        "coordinate_frame": "component_local" if component else "assembly",
        "feature_name": name,
        "suppressed_raw": None,
        "feature_error_code_raw": None,
        "endpoints_raw": None,
        "read_errors": {},
        "used_api": "IFeature.GetSpecificFeature2/IRefAxis.GetRefAxisParams",
    }
    try:
        matching = [
            feature
            for feature in _features_of_type(doc, {"RefAxis"})
            if _member(feature, "Name") == name
        ]
        if len(matching) != 1:
            raise ValueError(f"expected one exact RefAxis feature, found {len(matching)}")
    except Exception as error:  # noqa: BLE001 - raw capture retains the failure
        record["read_errors"]["feature_selection"] = _native_error_record(error)
        return record
    feature = matching[0]
    try:
        record["suppressed_raw"] = _raw_scalar(_method(feature, "IsSuppressed"))
    except Exception as error:  # noqa: BLE001
        record["read_errors"]["suppressed"] = _native_error_record(error)
    try:
        record["feature_error_code_raw"] = _raw_scalar(_method(feature, "GetErrorCode"))
    except Exception as error:  # noqa: BLE001
        record["read_errors"]["feature_error_code"] = _native_error_record(error)
    try:
        axis = _specific_feature_object(feature)
        record["endpoints_raw"] = _json_raw_value(_method(axis, "GetRefAxisParams"))
    except Exception as error:  # noqa: BLE001 - raw capture retains the failure
        record["read_errors"]["endpoints"] = _native_error_record(error)
    return record


def _project_reference_axis(raw):
    if not isinstance(raw, dict) or raw.get("kind") != "reference_axis":
        raise CadError("cad_axis_reference_unreadable", "raw reference-axis facts are malformed")
    read_errors = raw.get("read_errors")
    if not isinstance(read_errors, dict) or read_errors:
        raise CadError(
            "cad_axis_reference_unreadable",
            "raw reference-axis read failed",
            {"feature_name": raw.get("feature_name"), "errors": copy.deepcopy(read_errors)},
        )
    if raw.get("suppressed_raw") is not False:
        raise CadError(
            "cad_axis_reference_unreadable",
            "reference axis is suppressed or its suppression state is not false",
            {"feature_name": raw.get("feature_name"), "suppressed": raw.get("suppressed_raw")},
        )
    if type(raw.get("feature_error_code_raw")) is not int or raw["feature_error_code_raw"] != 0:
        raise CadError(
            "cad_axis_reference_unreadable",
            "reference axis has a nonzero or unreadable feature error",
            {"feature_name": raw.get("feature_name"), "error_code": raw.get("feature_error_code_raw")},
        )
    raw_endpoints = raw.get("endpoints_raw")
    if not isinstance(raw_endpoints, (list, tuple)) or any(type(value) not in (int, float) for value in raw_endpoints):
        raise CadError("cad_axis_reference_unreadable", "GetRefAxisParams did not return native numeric endpoints")
    try:
        endpoints = [float(value) for value in raw_endpoints]
    except Exception as error:  # noqa: BLE001
        raise CadError(
            "cad_axis_reference_unreadable",
            "GetRefAxisParams did not return numeric endpoints",
            {"feature_name": raw.get("feature_name"), "raw_endpoints": copy.deepcopy(raw_endpoints)},
        ) from error
    if len(endpoints) != 6 or not all(math.isfinite(value) for value in endpoints):
        raise CadError(
            "cad_axis_reference_unreadable",
            "GetRefAxisParams must return six finite endpoint coordinates",
            {"feature_name": raw.get("feature_name"), "raw_endpoints": copy.deepcopy(raw_endpoints)},
        )
    vector = [endpoints[index + 3] - endpoints[index] for index in range(3)]
    norm = math.hypot(*vector)
    if norm <= 0 or not math.isfinite(norm):
        raise CadError(
            "cad_axis_reference_unreadable",
            "reference-axis endpoints do not define a finite nonzero line",
            {"feature_name": raw.get("feature_name"), "raw_endpoints": copy.deepcopy(raw_endpoints)},
        )
    return {
        "kind": "reference_axis",
        "component": str(raw.get("component") or ""),
        "selector": copy.deepcopy(raw.get("selector") or {}),
        "coordinate_frame": str(raw.get("coordinate_frame") or ""),
        "direction_semantics": "undirected_axis_line",
        "raw_endpoints_m": endpoints,
        "axis_point_m": endpoints[:3],
        "axis_direction": [value / norm for value in vector],
        "used_api": str(raw.get("used_api") or ""),
    }


def _reference_axis_record(doc, name, component="", selector=None):
    """Keep the existing Discover projection over the raw RefAxis getter."""

    return _project_reference_axis(
        _capture_reference_axis_raw(doc, name, component, selector)
    )


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
        except Exception:  # noqa: BLE001 - preserve unavailable process provenance
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
#: open/close, a whole-assembly read, a heavy mesh) cannot leave the portal's
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


# Unloading a large read-only source session can lag; the settle window is
# bounded and only decides when a document is truly stuck.


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
        self._component_transforms = {}
        self._source_components = {}
        self._source_provenance = {}
        self._source_scope_overrides = {}
        self._source_documents = {}
        self._scene_source_root = None
        self._scene_document_key = None
        self._prepared_capture_root = None
        self.notes = {}
        #: ``GetSaveFlag`` per working-tree document, recorded as evidence.
        self.save_flags: dict[str, object] = {}
        self.save_flag_errors: dict[str, dict] = {}

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
        if session_artifact_path(path):
            # An engine-resolved session artifact (IC~~ import cache or VC~~
            # embedded component) belongs to the session whose walk produced
            # it, never to a path-derived role. Routing an artifact by its
            # temporary path can select the wrong application.
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
        prepared_root = self._prepared_capture_root
        if prepared_root is not None:
            try:
                Path(path).resolve().relative_to(prepared_root)
            except (OSError, ValueError):
                pass
            else:
                return "copy"
        return "source"

    def seal_documents(self, documents):
        """Seal the exact source documents this backend may open.

        The endpoint configures the verified selection here before any
        geometry or discovery work.  From then on every open of a source
        document - ``OpenDoc6`` through :meth:`open_document`,
        ``_ensure_document`` or discovery - must name one of these absolute
        paths; anything else raises ``document_not_sealed`` before COM is
        touched. Prepared copies under the controlled capture root are bound
        to the sealed inventory and rehashed after the owned session closes.

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

    def prepare_prepared_capture(self, source_root, assembly, configuration):
        """Bind prepared bytes without starting CAD or reading CAD state."""

        if not isinstance(configuration, str) or not configuration.strip():
            raise CadError(
                "cad_prepared_configuration_invalid",
                "prepared Capture needs one non-empty configuration name",
                {"configuration": configuration},
            )
        try:
            root = Path(source_root).resolve(strict=True)
            document = Path(assembly).resolve(strict=True)
        except (OSError, TypeError, ValueError) as error:
            raise CadError(
                "cad_prepared_source_invalid",
                "prepared Capture root and assembly must be existing paths",
                {"source_root": str(source_root), "assembly": str(assembly)},
            ) from error
        if not root.is_dir() or not document.is_file():
            raise CadError(
                "cad_prepared_source_invalid",
                "prepared Capture root must be a directory and the assembly a file",
                {"source_root": str(root), "assembly": str(document)},
            )
        if document.suffix.lower() != ".sldasm":
            raise CadError(
                "cad_not_assembly",
                "prepared Capture requires one SLDASM top assembly",
                {"assembly": str(document)},
            )
        try:
            document.relative_to(root)
        except ValueError as error:
            raise CadError(
                "cad_prepared_source_invalid",
                "the prepared top assembly must be inside its controlled root",
                {"source_root": str(root), "assembly": str(document)},
            ) from error
        with self._sessions_lock:
            if self._sessions:
                raise CadError(
                    "cad_prepared_policy_active_session",
                    "prepared Capture policy cannot change while a CAD session is live",
                    {"sessions": sorted(self._sessions)},
                )
        documents = sorted(
            str(path)
            for path in root.rglob("*")
            if path.is_file() and path.suffix.lower() in {".sldasm", ".sldprt"}
        )
        if str(document) not in documents:
            raise CadError(
                "cad_prepared_source_invalid",
                "the prepared top assembly is not part of the prepared document inventory",
                {"assembly": str(document)},
            )
        self._prepared_capture_root = str(root)
        search_root = self.configure_staged_search(str(root))
        self.prepare_source(str(document), configuration)
        sealed = self.seal_documents(documents)
        return {
            "mode": "prepared_read_only_copy",
            "source_root": str(root),
            "assembly": str(document),
            "requested_configuration": configuration,
            "session_role": "copy",
            "search_root": search_root,
            "sealed_documents": sealed,
            "com_started": False,
        }

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


    def release(self):
        if self._owner_thread is not None and self._owner_thread is not threading.current_thread():
            raise EnvironmentError_("cad_thread_mismatch", "Release CAD references on their owning STA thread")
        self._components.clear()
        self._component_transforms.clear()
        self._source_components.clear()
        self._source_documents.clear()
        self._session_document_paths.clear()
        self._session_artifact_roots.clear()
        self._session_artifact_root_owners.clear()
        self._scene_document_key = None
        self._prepared_capture_root = None
        self._part_mass_properties = {}
        self.save_flags.clear()
        self.save_flag_errors.clear()
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
        self._requested_configurations.clear()

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
        active_document = None
        active_document_error = None
        if doc is not None:
            try:
                active_document = _member(doc, "GetPathName")
            except Exception as error:  # noqa: BLE001 - title/path are diagnostic here
                active_document_error = _native_error_record(error)
            if not isinstance(active_document, str) or not active_document:
                try:
                    active_document = _member(doc, "GetTitle")
                except Exception as error:  # noqa: BLE001
                    active_document_error = _native_error_record(error)
        facts = {
            "ok": True,
            "backend": self.name,
            "sw_version": _member(app, "RevisionNumber"),
            "active_document": active_document,
        }
        if active_document_error is not None:
            facts["active_document_error"] = active_document_error
        return facts

    def list_documents(self):
        documents = []
        for doc in _member(self._app_obj(), "GetDocuments") or ():
            document = _modeldoc2(doc)
            try:
                path = _member(document, "GetPathName")
            except Exception as error:  # noqa: BLE001
                self.notes[f"list_documents_error:{len(documents)}"] = _native_error_record(error)
                continue
            if isinstance(path, str) and path:
                documents.append(path)
                continue
            try:
                title = _member(document, "GetTitle")
            except Exception as error:  # noqa: BLE001
                self.notes[f"list_documents_error:{len(documents)}"] = _native_error_record(error)
                continue
            if isinstance(title, str):
                documents.append(title)
        return documents

    def open_document(self, path):
        # Silent + read-only in the owned application for this phase.
        doc, errors, warnings = self._open_document_raw(path)
        if doc is None or errors.value:
            raise self._open_failure(path, errors.value, warnings.value)
        try:
            title = _member(doc, "GetTitle")
        except Exception as error:  # noqa: BLE001 - title is diagnostic only
            title = None
            title_error = _native_error_record(error)
        else:
            title_error = None
        result = {
            "opened": title,
            "path": _member(doc, "GetPathName"),
            "read_only": _raw_scalar(_member(doc, "IsOpenedReadOnly")),
            "errors": errors.value,
            "warnings": warnings.value,
        }
        if title_error is not None:
            result["title_error"] = title_error
        return result

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

    def _placement(self, component):
        holder = _member(component, "GetTotalTransform", False)
        if holder is None:
            raise CadError("cad_transform_failed", _member(component, "Name2"))
        raw = _member(holder, "ArrayData")
        if not isinstance(raw, (list, tuple)):
            raise CadError(
                "cad_transform_invalid",
                "GetTotalTransform(False).ArrayData did not return an array",
                {"component": _member(component, "Name2"), "value": _raw_scalar(raw)},
            )
        return _json_raw_value(list(raw))

    def _mass_properties_document(self, doc, require_material=True):
        if _member(doc, "GetType") != 1:
            raise CadError("cad_not_part", "mass reader requires a leaf part")
        document_path = str(_member(doc, "GetPathName"))
        _manager, _configuration, configuration_name = _configuration_context(doc)
        bodies = _part_bodies(doc)
        if not bodies:
            raise CadError("cad_empty_model", "part has no solid bodies")
        read_errors = {}
        try:
            materials = _material_assignments_document(doc, bodies)
        except Exception as exc:  # noqa: BLE001 - material is auxiliary to mass values
            materials = unverified_material_record(exc, configuration_name)
            read_errors["material"] = _native_error_record(exc)
        mp = _member(_member(doc, "Extension"), "CreateMassProperty2")
        if mp is None:
            raise CadError("cad_empty_mass_property", "CreateMassProperty2 returned null")
        import pythoncom

        mp.UseSystemUnits = True
        mp.IncludeHiddenBodiesOrComponents = True
        # Calculation-object selection only; never clears the user's selection.
        mp.SelectedItems = _win32().VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_DISPATCH, tuple(bodies))
        _member(mp, "Recalculate")
        overrides = {}
        try:
            override = _member(mp, "GetOverrideOptions")
            overrides = {
                name: _member(override, name)
                for name in ("OverrideMass", "OverrideCenterOfMass", "OverrideMomentsOfInertia")
            }
        except Exception as error:  # noqa: BLE001 - flags are auxiliary to mass values
            read_errors["overrides"] = _native_error_record(error)
        primary = _read_mass_primary(mp, read_errors)
        volume = _optional_mass_scalar(mp, "Volume", read_errors)
        density = _optional_mass_scalar(mp, "Density", read_errors)
        return {
            "mass": primary.get("mass_kg"),
            "com": primary.get("center_of_mass_m"),
            "native_raw": {
                **primary,
                "volume_m3": volume,
                "density_kg_m3": density,
                "overrides": overrides,
            },
            "read_errors": read_errors,
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
                "volume_m3": volume,
                "density_kg_m3": density,
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
        read_errors = {}
        overrides = {}
        try:
            override = _member(mp, "GetOverrideOptions")
            overrides = {
                name: _member(override, name)
                for name in ("OverrideMass", "OverrideCenterOfMass", "OverrideMomentsOfInertia")
            }
        except Exception as error:  # noqa: BLE001 - flags are auxiliary to mass values
            read_errors["overrides"] = _native_error_record(error)
        primary = _read_mass_primary(mp, read_errors)
        volume = _optional_mass_scalar(mp, "Volume", read_errors)
        density = _optional_mass_scalar(mp, "Density", read_errors)
        return {
            "mass": primary.get("mass_kg"),
            "com": primary.get("center_of_mass_m"),
            "native_raw": {
                **primary,
                "volume_m3": volume,
                "density_kg_m3": density,
                "overrides": overrides,
            },
            "read_errors": read_errors,
            "reference": {
                "used_api": "IMassProperty2.GetMomentOfInertia(0)",
                "scope": "assembly_document",
                "reference_point": "center_of_mass",
                "axes": "assembly_document_axes",
                "use_system_units": True,
                "product_convention": "solidworks_standard",
                "convention_basis": "same_api_selection_family_as_measured_group",
                "volume_m3": volume,
                "density_kg_m3": density,
                "overrides": overrides,
                "document": document_path,
                "configuration": configuration_name,
            },
        }

    def link_group_mass_properties(self, path, selections):
        """One-shot direct group readings for the rigid-body link selections.

        The prepared Capture calls this in its one read-only session after the
        scene and component-context reads.  The returned raw getter values stay
        on the sealed facts; the portable projection performs tensor grouping
        and reproducibility math after release.

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
            read_errors = {}
            overrides = {}
            try:
                override = _member(mp, "GetOverrideOptions")
                overrides = {
                    key: _member(override, key)
                    for key in ("OverrideMass", "OverrideCenterOfMass", "OverrideMomentsOfInertia")
                }
            except Exception as error:  # noqa: BLE001 - flags are auxiliary to mass values
                read_errors["overrides"] = _native_error_record(error)
            # Instance-level overrides are part of the CAD value and are read
            # as-is; the flags travel verbatim in the read.  A single-occurrence
            # selection surfaces the instance's authored override as an
            # object-level flag, so the group read never refuses on them.  The
            # whole-assembly and per-instance readers keep their own refusals.
            primary = _read_mass_primary(mp, read_errors)
            read = {
                "mass_kg": primary.get("mass_kg"),
                "com_m": primary.get("center_of_mass_m"),
                "inertia_values_raw": primary.get("inertia_values"),
                "overrides": _json_raw_value(overrides),
                "read_errors": read_errors,
            }
            try:
                read["inertia_values_about_origin_api"] = _json_raw_value(
                    _member(mp, "GetMomentOfInertia", 1)
                )
            except Exception as error:  # noqa: BLE001 - origin sample stands alone
                read_errors["inertia_values_about_origin_api"] = _native_error_record(
                    error
                )
                read["inertia_values_about_origin_api"] = copy.deepcopy(
                    read_errors["inertia_values_about_origin_api"]
                )
            read["volume_m3"] = _raw_scalar(
                _optional_mass_scalar(mp, "Volume", read_errors)
            )
            read["density_kg_m3"] = _raw_scalar(
                _optional_mass_scalar(mp, "Density", read_errors)
            )
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
            links[body_name] = {
                "selection": paths,
                "reads": [first, second],
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
                read_errors = {}
                primary = _read_mass_primary(mp, read_errors)
                flags = {}
                try:
                    override = _member(mp, "GetOverrideOptions")
                    flags = {
                        key: _member(override, key)
                        for key in ("OverrideMass", "OverrideCenterOfMass", "OverrideMomentsOfInertia")
                    }
                except Exception as error:  # noqa: BLE001 - flags are auxiliary to context mass
                    read_errors["overrides"] = _native_error_record(error)
                volume = _optional_mass_scalar(mp, "Volume", read_errors)
                density = _optional_mass_scalar(mp, "Density", read_errors)
                document_type = "assembly" if children else "part"
                entry = {
                    "name": name,
                    "parent": parent,
                    "depth": depth,
                    "document": component_path,
                    "document_type": document_type,
                    "context_mass_kg": primary.get("mass_kg"),
                    "context_volume_m3": volume,
                    "context_density_kg_m3": density,
                    "overrides": flags,
                    "read_errors": read_errors,
                    "primary_raw": primary,
                }
                has_override = any(value is True for value in flags.values())
                if has_override:
                    overridden_ancestors.add(name)
                part_reading = (getattr(self, "_part_mass_properties", None) or {}).get(name)
                mass_value = primary.get("mass_kg")
                part_mass_value = (
                    (part_reading.get("native_raw") or {}).get("mass_kg")
                    if isinstance(part_reading, dict)
                    else None
                )
                differs_from_part = (
                    not has_override
                    and document_type == "part"
                    and isinstance(part_reading, dict)
                    and (part_reading.get("reference") or {}).get("scope") == "part_document"
                    and not any(name.startswith(ancestor + "/") for ancestor in overridden_ancestors)
                    and type(mass_value) in (int, float)
                    and type(part_mass_value) in (int, float)
                    and not math.isclose(mass_value, part_mass_value, rel_tol=1e-9, abs_tol=1e-12)
                )
                if has_override or differs_from_part:
                    # Approved override support: one positively identified
                    # instance, full singleton tensor read from the SAME
                    # selection (no second traversal), cached for the
                    # canonical effective-instances record and the scene
                    # mirror.  A failed tensor read is recorded, never
                    # substituted.
                    effective = self._singleton_effective_reading(
                        name,
                        primary,
                        component_path,
                        document_path,
                        configuration_name,
                        referenced_configuration,
                        flags,
                        document_type,
                        volume_raw=volume,
                        density_raw=density,
                        read_errors=read_errors,
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
        """Read raw RefAxis endpoints or candidate cylindrical-face getter values."""

        if not isinstance(reference, dict):
            return {
                "kind": "invalid",
                "component": None,
                "selector": _raw_scalar(reference),
                "coordinate_frame": None,
                "read_errors": {
                    "selector": {
                        "code": "cad_axis_reference_invalid",
                        "message": "axis reference is not an object",
                        "detail": {"raw_value": _raw_scalar(reference)},
                    }
                },
                "used_api": None,
            }
        component = reference.get("component")
        common = {
            "component": component,
            "selector": copy.deepcopy(reference),
            "coordinate_frame": "component_local",
            "read_errors": {},
            "used_api": (
                "IComponent2.GetBodies2/IBody2.GetFaces/IFace2.GetSurface/"
                "ISurface.IsCylinder/ISurface.CylinderParams"
            ),
        }
        if reference.get("kind") == "reference_axis":
            name = reference.get("feature_name")
            raw = {
                **common,
                "kind": "reference_axis",
                "coordinate_frame": "component_local" if component else "assembly",
                "feature_name": name,
                "suppressed_raw": None,
                "feature_error_code_raw": None,
                "endpoints_raw": None,
                "used_api": "IFeature.GetSpecificFeature2/IRefAxis.GetRefAxisParams",
            }
            if not _is_text_name(name) or "face_index" in reference:
                raw["read_errors"]["selector"] = {
                    "code": "cad_axis_reference_invalid",
                    "message": "a reference axis needs an exact feature_name",
                    "detail": copy.deepcopy(reference),
                }
                return raw
            try:
                if component:
                    if not isinstance(component, str) or component not in self._components:
                        raise CadError("cad_missing_component", str(component))
                    with self._current_components("axis_reference"):
                        key, _configuration_name = self._source_components[component]
                        doc = self._captured_document(key, component, "axis_reference")
                else:
                    doc = self._captured_document(self._scene_document_key, phase="axis_reference")
                return {
                    **raw,
                    **_capture_reference_axis_raw(doc, name, str(component or ""), reference),
                }
            except Exception as error:  # noqa: BLE001 - raw failure is projected offline
                raw["read_errors"]["document"] = _native_error_record(error)
                return raw
        raw = {
            **common,
            "kind": "cylinder",
            "body_type": _raw_scalar(reference.get("body_type")),
            "body_type_native": None,
            "feature_name": _raw_scalar(reference.get("feature_name")),
            "face_index": _raw_scalar(reference.get("face_index")),
            "face_candidates": [],
        }
        if not isinstance(component, str) or not component:
            raw["read_errors"]["component"] = {
                "code": "cad_missing_component",
                "message": "cylinder selector has no component",
                "detail": copy.deepcopy(reference),
            }
            return raw
        if component not in self._components:
            raw["read_errors"]["component"] = {
                "code": "cad_missing_component",
                "message": component,
                "detail": copy.deepcopy(reference),
            }
            return raw
        body_type_name = reference.get("body_type", "solid")
        if not isinstance(body_type_name, str) or body_type_name not in {"solid", "sheet"}:
            raw["read_errors"]["body_type"] = {
                "code": "cad_axis_reference_invalid",
                "message": "body_type must be solid or sheet",
                "detail": {"value": _raw_scalar(body_type_name)},
            }
            return raw
        raw["body_type_native"] = body_type_name
        feature_name = reference.get("feature_name")
        face_index = reference.get("face_index")
        named = _is_text_name(feature_name)
        if not named and (
            not isinstance(face_index, int)
            or isinstance(face_index, bool)
            or face_index < 0
        ):
            raw["read_errors"]["selector"] = {
                "code": "cad_axis_reference_invalid",
                "message": "the selector needs a named feature or a non-negative face_index",
                "detail": copy.deepcopy(reference),
            }
            return raw
        body_type = 0 if body_type_name == "solid" else 1
        try:
            with self._current_components("axis_reference") as current:
                holder = current[component]
                bodies = self._body_list(holder, body_type, component)
                readable_faces = 0
                for body_index, raw_body in enumerate(bodies):
                    try:
                        body = _interface(_dynamic(raw_body), IBODY2_IID)
                        faces = _as_list(_member(body, "GetFaces"))
                    except Exception as error:  # noqa: BLE001 - retain body/face read failure
                        raw["read_errors"][f"body:{body_index}"] = _native_error_record(error)
                        continue
                    for local_face_index, face in enumerate(faces):
                        current_feature_name = None
                        if named:
                            try:
                                feature = _member(face, "GetFeature")
                                current_feature_name = _member(feature, "Name")
                            except Exception as error:  # noqa: BLE001 - retain feature read failure
                                raw["read_errors"][
                                    f"face:{body_index}:{local_face_index}:feature"
                                ] = _native_error_record(error)
                                continue
                            if current_feature_name != feature_name:
                                continue
                        elif readable_faces != face_index:
                            readable_faces += 1
                            continue
                        candidate = {
                            "body_index": body_index,
                            "face_index": readable_faces,
                            "body_face_index": local_face_index,
                            "feature_name": current_feature_name,
                            "is_cylinder": None,
                            "cylinder_params_raw": None,
                            "persist_reference_b64": self._persist_reference(face),
                            "read_errors": {},
                        }
                        try:
                            surface = _member(face, "GetSurface")
                        except Exception as error:  # noqa: BLE001 - retain face read failure
                            candidate["read_errors"]["surface"] = _native_error_record(error)
                            surface = None
                        if surface is not None:
                            try:
                                candidate["is_cylinder"] = _raw_scalar(
                                    _member(surface, "IsCylinder")
                                )
                            except Exception as error:  # noqa: BLE001 - retain type read failure
                                candidate["read_errors"]["is_cylinder"] = _native_error_record(error)
                            try:
                                candidate["cylinder_params_raw"] = [
                                    _raw_scalar(value)
                                    for value in _as_list(_member(surface, "CylinderParams"))
                                ]
                            except Exception as error:  # noqa: BLE001 - retain params read failure
                                candidate["read_errors"]["cylinder_params"] = _native_error_record(error)
                        raw["face_candidates"].append(candidate)
                        readable_faces += 1
                        if not named:
                            break
                    if not named and raw["face_candidates"]:
                        break
                if not named and not raw["face_candidates"]:
                    raw["read_errors"]["face_index"] = {
                        "code": "cad_axis_reference_invalid",
                        "message": "face_index is outside the component's faces",
                        "detail": {"face_index": face_index, "readable_faces": readable_faces},
                    }
        except Exception as error:  # noqa: BLE001 - raw failure is projected offline
            raw["read_errors"]["document_or_bodies"] = _native_error_record(error)
        return raw

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

    def assembly_group_mass_properties(self, path, names, *, allow_overrides=False):
        """Mass properties for a selected group of component instances.

        Measured on the analytic fixture (2026-10-06, SolidWorks 34.0.0): a
        group selection answers in the SAME standard notation the analytic
        parallel-axis combination predicts (residual 6.5e-19 absolute), so the
        reading declares ``solidworks_standard``.  The reading is
        scope-qualified (``assembly_component_group``) so no historical part
        measurement is ever relabelled, and any effective instance override is
        refused instead of silently becoming the value.

        Returns the selected native getter arrays and their identity binding;
        physical tensor projection happens after the CAD session closes.
        """

        doc = self._document_by_path(path)
        if _member(doc, "GetType") != 2:
            raise CadError("cad_not_assembly", "group mass reader requires a saved SLDASM")
        document_path = str(_member(doc, "GetPathName"))
        _manager, configuration, configuration_name = _configuration_context(doc)
        root = _component(_member(configuration, "GetRootComponent3", True))
        if root is None:
            raise CadError("cad_empty_mass_property", "assembly has no root component")
        wanted = {str(name) for name in names}
        if not wanted:
            raise CadError("cad_empty_selection", "group mass reader needs at least one component instance")
        selection: list[object] = []
        selected_names: list[str] = []
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
            selected_names.append(name)
            selection.append(component)
        missing = sorted(wanted - set(selected_names))
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
                {"selected": sorted(selected_names), "requested": sorted(wanted)},
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
        read_errors = {}
        overrides = {}
        try:
            override = _member(mp, "GetOverrideOptions")
            overrides = {
                key: _member(override, key)
                for key in ("OverrideMass", "OverrideCenterOfMass", "OverrideMomentsOfInertia")
            }
        except Exception as error:  # noqa: BLE001 - flags are auxiliary to group mass values
            read_errors["overrides"] = _native_error_record(error)
        primary = _read_mass_primary(mp, read_errors)
        members = {selected_names[0]: dict(overrides)} if len(selected_names) == 1 else {}
        result = {
            "assembly": document_path,
            "configuration": configuration_name,
            "group": sorted(wanted),
            "members": members,
            "native_raw": {
                **primary,
                "overrides": dict(overrides),
            },
            "read_errors": read_errors,
            "reference": {
                "used_api": "IMassProperty2.GetMomentOfInertia(0)",
                "scope": "assembly_component_group",
                "convention_basis": "analytic_fixture_v2_20261006",
                "product_convention": "solidworks_standard",
                "reference_point": "center_of_mass",
                "axes": "assembly_document_axes",
                "use_system_units": True,
                "overrides": dict(overrides),
                "document": document_path,
                "configuration": configuration_name,
            },
        }
        if allow_overrides:
            effective = any(value is True for value in overrides.values())
            result["reference"].update(
                {
                    "selection_api": "IMassProperty2 (SelectedItems = component instance)",
                    "override_api": "IMassProperty2.GetOverrideOptions (same selection)",
                    "effective_instance": effective,
                    "measurement_basis": ("cad_authored_effective_instance" if effective else "cad_measured"),
                }
            )
            for key, member in (("volume_m3", "Volume"), ("density_kg_m3", "Density")):
                value = _optional_mass_scalar(mp, member, read_errors)
                if value is not None:
                    result["reference"][key] = value
        return result

    def _singleton_effective_reading(
        self,
        name,
        primary,
        component_path,
        assembly_path,
        assembly_configuration,
        referenced,
        flags,
        document_type,
        *,
        volume_raw=None,
        density_raw=None,
        read_errors=None,
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
        if volume_raw is not None:
            reference["volume_m3"] = volume_raw
        if density_raw is not None:
            reference["density_kg_m3"] = density_raw
        reading = {
            "role": "parent" if document_type == "assembly" else "leaf",
            "document_type": document_type,
            "native_raw": {
                **copy.deepcopy(primary),
                "volume_m3": volume_raw,
                "density_kg_m3": density_raw,
                "overrides": dict(flags),
            },
            "reference": reference,
            "read_errors": copy.deepcopy(read_errors or {}),
        }
        if part_reading is not None:
            reading["part_measurement_raw"] = _raw_mass_fact(part_reading)
        return reading


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

        occurrence_path = (
            occurrence.get("path")
            if isinstance(occurrence, dict)
            else getattr(occurrence, "path", "")
        )
        reading = self.assembly_group_mass_properties(str(assembly_path), [name], allow_overrides=True)
        members = reading.get("members")
        raw_flags = members.get(name) if isinstance(members, dict) else {}
        flags = raw_flags if isinstance(raw_flags, dict) else {}
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
            "part_document": str(occurrence_path),
            "assembly_document": str(reading.get("assembly") or ""),
            "assembly_configuration": str(reading.get("configuration") or ""),
            "selected_components": [name],
            "fallback_reason": "cad_part_document_mass_unavailable",
            "fallback_error_code": str(error.code),
            "solid_bodies": int(counts.get("solid") or 0),
            "sheet_bodies": int(counts.get("sheet") or 0),
            "effective_instance": True,
            "overrides": dict(flags),
        }
        for key in ("volume_m3", "density_kg_m3"):
            if key in reference:
                row_reference[key] = reference[key]
        return {
            "native_raw": copy.deepcopy(reading.get("native_raw") or {}),
            "mode": "effective_instance",
            "state": "sheet_only",
            "measurement_basis": "cad_authored_effective_instance",
            "reference": row_reference,
            "read_errors": copy.deepcopy(reading.get("read_errors") or {}),
        }


    def collect_prepared_scene(
        self,
        doc_path,
        coordinate_systems,
        groups,
        on_activity=None,
        require_material=True,
    ):
        facts = {}
        try:
            activity = _ActivityEmitter(on_activity)
            if self._prepared_capture_root is None:
                raise CadError(
                    "cad_prepared_policy_missing",
                    "collect_prepared_scene requires prepare_prepared_capture policy",
                    {"document": str(doc_path)},
                )
            source_root = Path(self._prepared_capture_root)
            try:
                Path(doc_path).resolve().relative_to(source_root)
            except (OSError, ValueError) as error:
                raise CadError(
                    "cad_prepared_document_outside_root",
                    "the prepared document is outside its controlled root",
                    {"source_root": str(source_root), "document": str(doc_path)},
                ) from error

            def note(action: str, **fields) -> None:
                activity.emit(action, phase="capture", **fields)

            note("prepare_capture", current_object=_activity_object(doc_path), force=True)
            doc = self._document_by_path(doc_path)
            if _member(doc, "GetType") != 2:
                raise CadError("cad_not_assembly", "export requires a saved SLDASM")
            document, configuration = _capture_document_identity(
                doc,
                doc_path,
                "source",
            )
            requested_configuration = self._requested_configurations.get(
                normalize_document_path(str(doc_path)),
                "",
            )
            if requested_configuration and configuration != requested_configuration:
                raise CadError(
                    "cad_capture_configuration_mismatch",
                    "the prepared assembly did not open in its requested configuration",
                    {
                        "path": document,
                        "requested": requested_configuration,
                        "observed": configuration,
                    },
                )
            raw_state = _discovery_document_state(doc)
            if raw_state.get("read_only") is not True:
                raise CadError(
                    "cad_capture_not_read_only",
                    "the native source document is not read-only",
                    {"path": document, "state": raw_state},
                )
            source_sha256 = _hash(document)
            try:
                native_api_pid = int(_method(self._app_obj(), "GetProcessID"))
            except Exception:  # noqa: BLE001 - unresolved PID keeps source scope incomplete
                native_api_pid = None
            self._record_save_flag(doc, doc_path)
            self._components = set()
            self._component_transforms = {}
            self._source_components = {}
            self._source_provenance = {}
            self._source_scope_overrides = {}
            self._source_documents = {}
            self._scene_source_root = source_root
            self._session_document_paths = {}
            self._session_artifact_roots = {}
            self._session_artifact_root_owners = {}
            self.notes = {}
            facts = {
                "scene_raw": {
                    "document": str(document),
                    "configuration": configuration,
                    "source_sha256": source_sha256,
                    "document_state": raw_state,
                    "occurrences": None,
                    "components": None,
                    "coordinate_systems": None,
                },
                "part_mass_raw": {},
                "assembly_mass_raw": None,
                "component_mass_raw": None,
                "link_group_mass_raw": None,
                "source_scopes": None,
                "notes": self.notes,
            }
            _manager, config = _active_configuration_view(doc)
            self._scene_document_key = self._record_source_document(doc_path, str(_member(config, "Name")))
            root = _component(_member(config, "GetRootComponent3", True))
            stack = [
                (component, doc_path)
                for component in list(_method(root, "GetChildren") or ())
            ]
            borrowed_components = []
            occurrences = []
            leaf_components = []
            properties = {}
            part_mass_raw = {}
            requested_datums = set(coordinate_systems)
            coordinate_system_rows: dict[str, dict] = {}
            found_datums = set()
            facts["scene_raw"].update(
                {
                    "occurrences": occurrences,
                    "components": leaf_components,
                    "coordinate_systems": coordinate_system_rows,
                }
            )
            facts["part_mass_raw"] = part_mass_raw

            def record_datums(document, owner, placement):
                for datum in _coordinate_system_features(document):
                    qualified = datum if not owner else f"{owner}::{datum}"
                    if datum in requested_datums:
                        key = datum
                    elif qualified in requested_datums:
                        key = qualified
                    else:
                        continue
                    raw_values = None
                    array_error = None
                    try:
                        holder = _member(
                            _member(document, "Extension"),
                            "GetCoordinateSystemTransformByName",
                            datum,
                        )
                        if holder is None:
                            raise CadError(
                                "cad_missing_coordinate_system", "native coordinate-system transform is null"
                            )
                        raw_values = _json_raw_value(_member(holder, "ArrayData"))
                    except Exception as error:
                        array_error = _native_error_record(error)
                    record = {
                        "key": key,
                        "name": datum,
                        "owner": owner,
                        "configuration": _active_configuration(document),
                        "document": str(_member(document, "GetPathName")),
                        "array_data": raw_values,
                        "read_errors": (
                            {"array_data": array_error}
                            if array_error is not None
                            else {}
                        ),
                        "occurrence_transform_raw": (
                            copy.deepcopy(placement)
                            if placement is not None
                            else None
                        ),
                    }
                    if key in coordinate_system_rows:
                        raise CadError(
                            "cad_coordinate_system_ambiguous",
                            "the requested coordinate system is repeated",
                            {"datum": datum, "owner": owner, "key": key},
                        )
                    coordinate_system_rows[key] = record
                    found_datums.add(datum)

            record_datums(doc, "", None)
            while stack:
                raw_component, owner_path = stack.pop()
                comp = _component(raw_component)
                if _measured_component_state(comp) == COMPONENT_SUPPRESSED:
                    continue
                borrowed_components.append(comp)
                raw_name = _member(comp, "Name2")
                if not isinstance(raw_name, str) or not raw_name.strip():
                    raise CadError(
                        "cad_component_identity_unreadable",
                        "a component has no readable Name2",
                        {"raw_name": repr(raw_name)},
                    )
                name = raw_name
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
                if not isinstance(referenced, str) or not referenced.strip():
                    raise CadError(
                        "cad_component_configuration_unreadable",
                        "a component has no readable ReferencedConfiguration",
                        {"component": name, "raw_value": repr(referenced)},
                    )
                # Configuration changes can invalidate borrowed occurrence interfaces.
                # Finish the assembly traversal using primitives before any selection.
                children = list(_method(comp, "GetChildren") or ())
                try:
                    placement = self._placement(comp)
                except CadError as error:
                    placement = None
                    self.notes["transform_error:" + name] = {
                        "code": error.code,
                        "message": error.message,
                        "detail": error.detail,
                    }
                if children:
                    if _member(part, "GetType") != 2:
                        raise CadError("cad_component_type", name)
                    document_type = "assembly"
                    stack.extend(
                        (child, path) for child in children
                    )
                else:
                    if _member(part, "GetType") != 1:
                        raise CadError("cad_empty_subassembly", name)
                    document_type = "part"
                    body_counts = {"solid": None, "sheet": None}
                    body_count_errors = {}
                    for body_type, kind in ((0, "solid"), (1, "sheet")):
                        try:
                            body_counts[kind] = self._body_count(comp, body_type, name)
                        except CadError as error:
                            body_count_errors[kind] = _native_error_record(error)
                    self.notes["bodies:" + name] = {
                        **body_counts,
                        "errors": body_count_errors,
                    }
                fixed = None
                fixed_error = None
                try:
                    fixed = _member(comp, "IsFixed")
                except Exception as error:  # noqa: BLE001 - preserve the getter failure
                    fixed_error = _native_error_record(error)
                else:
                    if type(fixed) is not bool:
                        fixed_error = {
                            "code": "cad_component_fixed_invalid",
                            "message": "IComponent2.IsFixed did not return bool",
                            "detail": {"value_type": type(fixed).__name__},
                        }
                document_key = self._record_source_document(
                    path,
                    _active_configuration(part),
                    session_document=session_document,
                )
                self._source_components[name] = (document_key, str(referenced))
                self._component_transforms[name] = copy.deepcopy(placement)
                occurrences.append(
                    {
                        "name": name,
                        "path": path,
                        "transform_raw": copy.deepcopy(placement),
                        "referenced_configuration": str(referenced),
                        "is_fixed": fixed,
                        "is_fixed_error": fixed_error,
                        "document_type": document_type,
                    }
                )

            borrowed_components.clear()
            raw_component = owner_path = comp = children = part = root = stack = None
            note("walk", completed=len(occurrences), total=len(occurrences), force=True)
            for index, occurrence in enumerate(occurrences, start=1):
                name, path = occurrence["name"], occurrence["path"]
                referenced = occurrence["referenced_configuration"]
                note(
                    "instance_reads",
                    current_object=_activity_object(name),
                    completed=index - 1,
                    total=len(occurrences),
                )
                with _temporary_configuration(
                    lambda document_path=path: self._document_by_path(document_path), referenced, name
                ) as (part, _previous):
                    record_datums(part, name, occurrence["transform_raw"])
                    # Occurrence references are independent of the one active state
                    # of a shared document; every temporary selection is restored.
                    if occurrence["document_type"] == "assembly":
                        continue
                    leaf_components.append(occurrence)
                    self._components.add(name)
                    try:
                        properties[name] = self._mass_properties_document(part, require_material)
                    except CadError as error:
                        counts = dict(self.notes.get("bodies:" + name) or {})
                        original_detail = error.detail
                        context = (
                            original_detail if isinstance(original_detail, dict) else {"native_detail": original_detail}
                        )
                        error.detail = {
                            **context,
                            "component": name,
                            "document": path,
                            "referenced_configuration": referenced,
                            "bodies": counts,
                            "phase": "leaf_mass_read",
                        }
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
                                self.notes["mass_error:" + name] = {
                                    "code": error.code,
                                    "message": error.message,
                                    "detail": error.detail,
                                    "fallback_error": (
                                        f"{type(fallback_error).__name__}: "
                                        f"{fallback_error}"
                                    )[:300],
                                }
                        if fallback_row is None:
                            self.notes.setdefault(
                                "mass_error:" + name,
                                {
                                    "code": error.code,
                                    "message": error.message,
                                    "detail": error.detail,
                                },
                            )
                        else:
                            properties[name] = fallback_row
                            self.notes["effective_instance:" + name] = (
                                "cad_authored_effective_instance"
                            )
                    if name in properties:
                        properties[name]["reference"]["configuration"] = referenced
                        part_mass_raw[name] = _raw_mass_fact(properties[name])
                        self.notes["mass_property:" + name] = (
                            properties[name]["reference"]["used_api"]
                        )
                    note(
                        "instance_reads",
                        current_object=_activity_object(name),
                        completed=index,
                        total=len(occurrences),
                    )
            if not leaf_components:
                raise CadError("cad_empty_model", "assembly has no resolved solid parts")
            missing_datums = sorted(requested_datums - found_datums)
            if missing_datums:
                self.notes["missing_coordinate_systems"] = missing_datums
            note("instance_reads", completed=len(occurrences), total=len(occurrences), force=True)
            self._part_mass_properties = dict(properties)
            assembly_mass_raw = None
            try:
                assembly_mass_raw = self.assembly_mass_properties(doc_path)
            except Exception as error:  # noqa: BLE001 - retained as native read failure
                self.notes["assembly_mass_error"] = _native_error_record(error)
            else:
                facts["assembly_mass_raw"] = _raw_mass_fact(
                    assembly_mass_raw
                )
            component_mass_raw = None
            try:
                component_mass_raw = self.assembly_component_mass_properties(doc_path)
            except Exception as error:  # noqa: BLE001 - retained as native read failure
                self.notes["component_mass_error"] = _native_error_record(error)
            else:
                facts["component_mass_raw"] = (
                    _raw_component_mass_fact(component_mass_raw)
                )
            component_names = [str(item["name"]) for item in leaf_components]
            selection = _compose_link_group_selections(
                groups,
                component_names,
                {
                    str(row["name"])
                    for row in (component_mass_raw or {}).get("instances", [])
                    if isinstance(row, dict) and isinstance(row.get("effective"), dict)
                }
                | {
                    name
                    for name, row in properties.items()
                    if isinstance(row, dict) and row.get("mode") == "effective_instance"
                },
            )
            link_group_mass_raw = self.link_group_mass_properties(
                doc_path,
                {name: entry["selection"] for name, entry in selection.items()},
            )
            facts["link_group_mass_raw"] = link_group_mass_raw
            source_scopes = self.source_scopes(component_names)
            facts["source_scopes"] = source_scopes
            self.notes["save_flags"] = copy.deepcopy(self.save_flags)
            self.notes["save_flag_errors"] = copy.deepcopy(self.save_flag_errors)
            return facts
        except Exception as error:
            if facts:
                snapshot = copy.deepcopy(facts)
                snapshot["notes"]["save_flags"] = copy.deepcopy(self.save_flags)
                snapshot["notes"]["save_flag_errors"] = copy.deepcopy(self.save_flag_errors)
                error.partial_native_facts = snapshot
            raise

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

    def _appearance_owner_rows(
        self,
        component,
        holder,
        current,
        leaf_document,
        leaf_display_state,
        leaf_display_state_error,
    ):
        """Relevant top, ancestor, and leaf document owners for one occurrence."""

        rows = []
        try:
            top_document = self._captured_document(
                self._scene_document_key,
                component=component,
                phase="appearance_owner",
            )
        except Exception as error:  # noqa: BLE001 - retained owner failure
            rows.append(
                {
                    "owner_scope": "top_assembly",
                    "document": None,
                    "document_path": None,
                    "configuration": None,
                    "display_state": None,
                    "display_state_error": _native_error_record(error),
                    "display_states": None,
                    "display_states_state": "error",
                    "owner_error": None,
                }
            )
        else:
            (
                top_configuration,
                top_state,
                top_state_error,
                top_display_states,
                top_display_states_state,
            ) = (
                _appearance_document_display_state(top_document)
            )
            top_path_error = None
            try:
                top_path = _member(top_document, "GetPathName")
            except Exception as error:  # noqa: BLE001 - path stays field-local
                top_path = None
                top_path_error = _native_error_record(error)
            rows.append(
                {
                    "owner_scope": "top_assembly",
                    "document": top_document,
                    "document_path": top_path,
                    "configuration": top_configuration,
                    "display_state": top_state,
                    "display_state_error": top_state_error,
                    "display_states": top_display_states,
                    "display_states_state": top_display_states_state,
                    "owner_error": top_path_error,
                }
            )

        ancestors = []
        parts = component.split("/")
        for end in range(1, len(parts)):
            name = "/".join(parts[:end])
            ancestor = current.get(name)
            if ancestor is None:
                continue
            ancestors.append(name)
            try:
                ancestor_document = _component_document(ancestor)
                configuration = _member(
                    ancestor,
                    "ReferencedConfiguration",
                )
                display_state, state_error = _appearance_display_state(
                    ancestor
                )
            except Exception as error:  # noqa: BLE001 - owner stays field-local
                rows.append(
                    {
                        "owner_scope": "parent_assembly",
                        "document": None,
                        "document_path": None,
                        "configuration": None,
                        "display_state": None,
                        "display_state_error": _native_error_record(error),
                        "display_states": None,
                        "display_states_state": "not_applicable",
                        "owner_error": None,
                    }
                )
                continue
            path_error = None
            try:
                document_path = _member(
                    ancestor_document,
                    "GetPathName",
                )
            except Exception as error:  # noqa: BLE001 - path stays field-local
                document_path = None
                path_error = _native_error_record(error)
            rows.append(
                {
                    "owner_scope": "parent_assembly",
                    "document": ancestor_document,
                    "document_path": document_path,
                    "configuration": configuration,
                    "display_state": display_state,
                    "display_state_error": state_error,
                    "display_states": None,
                    "display_states_state": "not_applicable",
                    "owner_error": path_error,
                }
            )

        try:
            leaf_path = _member(leaf_document, "GetPathName")
            leaf_path_error = None
        except Exception as error:  # noqa: BLE001 - path stays field-local
            leaf_path = None
            leaf_path_error = _native_error_record(error)
        rows.append(
            {
                "owner_scope": "component_document",
                "document": leaf_document,
                "document_path": leaf_path,
                "configuration": None,
                "display_state": leaf_display_state,
                "display_state_error": leaf_display_state_error,
                "display_states": None,
                "display_states_state": "not_applicable",
                "owner_error": leaf_path_error,
            }
        )
        return rows, ancestors

    @staticmethod
    def _appearance_identity_maps(
        extension,
        holders,
        bodies,
        faces,
        features,
    ):
        """Build exact persistent-reference maps for one occurrence context."""

        maps = {
            "occurrences": {},
            "bodies": {},
            "faces": {},
            "features": {},
        }
        mapping_errors = []

        def add(kind, entity, identity):
            reference, error = _appearance_persistent_reference(
                extension,
                entity,
            )
            if error is not None:
                mapping_errors.append(
                    {
                        "entity_kind": kind,
                        "identity": copy.deepcopy(identity),
                        "error": copy.deepcopy(error),
                    }
                )
                return
            key = _appearance_reference_key(reference)
            if key is not None:
                maps[kind].setdefault(key, []).append(identity)

        for name, component in holders:
            add("occurrences", component, {"component": name})
        for row in bodies:
            add(
                "bodies",
                row["body"],
                {
                    "component": row["component"],
                    "body_type": row["body_type"],
                    "body_index": row["body_index"],
                },
            )
        for row in faces:
            add(
                "faces",
                row["face"],
                {
                    "component": row["component"],
                    "body_type": row["body_type"],
                    "body_index": row["body_index"],
                    "face_index": row["face_index"],
                },
            )
        for row in features:
            add(
                "features",
                row["feature"],
                {
                    "component": row["component"],
                    "body_type": row["body_type"],
                    "body_index": row["body_index"],
                    "face_index": row["face_index"],
                    "name": row["identity"]["name"],
                    "type_name": row["identity"]["type_name"],
                    "persistent_reference": row["identity"][
                        "persistent_reference"
                    ],
                },
            )
        return maps, mapping_errors

    def _capture_state_render_materials(
        self,
        *,
        component,
        holder,
        current,
        leaf_document,
        source_configuration,
        leaf_display_state,
        leaf_display_state_error,
        bodies,
        faces,
        features,
        cache,
    ):
        owner_rows, ancestors = self._appearance_owner_rows(
            component,
            holder,
            current,
            leaf_document,
            leaf_display_state,
            leaf_display_state_error,
        )
        holder_rows = [(component, holder)]
        holder_rows.extend(
            (name, current[name])
            for name in ancestors
            if name in current
        )
        records = []
        for owner in owner_rows:
            try:
                document = owner["document"]
                configuration = owner["configuration"]
                display_state = owner["display_state"]
                state_error = owner["display_state_error"]
                owner_error = owner["owner_error"]
                owner_configuration = (
                    configuration
                    if owner["owner_scope"] != "component_document"
                    else source_configuration
                )
                record = {
                    "owner_document_path": owner["document_path"],
                    "owner_scope": owner["owner_scope"],
                    "api": "IModelDocExtension.GetRenderMaterials2",
                    "display_state_option": 3,
                    "native_render_material_count": None,
                    "native_render_material_count_error": None,
                    "configuration": _json_raw_value(owner_configuration),
                    "display_state": display_state,
                    "display_states": copy.deepcopy(
                        owner["display_states"]
                    ),
                    "display_states_state": owner["display_states_state"],
                    "call_state": "error",
                    "materials": [],
                    "mapping_errors": [],
                    "read_errors": {},
                }
                if owner_error is not None:
                    record["call_state"] = "error"
                    record["read_errors"]["owner"] = copy.deepcopy(
                        owner_error
                    )
                    records.append(record)
                    continue
                if state_error is not None:
                    record["call_state"] = "state_unavailable"
                    record["read_errors"]["display_state"] = copy.deepcopy(
                        state_error
                    )
                    records.append(record)
                    continue
                key = (
                    normalize_document_path(owner["document_path"]),
                    str(owner_configuration),
                    str(display_state),
                )
                if key not in cache:
                    cache[key] = _state_render_materials_raw(
                        document,
                        configuration=owner_configuration,
                        display_state=display_state,
                    )
                raw_record, extension = cache[key]
                record.update(copy.deepcopy(raw_record))
                if (
                    record["call_state"] == "observed"
                    and extension is not None
                ):
                    maps, mapping_errors = self._appearance_identity_maps(
                        extension,
                        holder_rows,
                        bodies,
                        faces,
                        features,
                    )
                    record["mapping_errors"] = mapping_errors
                    for material in record["materials"]:
                        for entity in material["entity_records"]:
                            entity["matched_identity"] = {
                                kind: copy.deepcopy(
                                    values.get(
                                        _appearance_reference_key(
                                            entity["persistent_reference"]
                                        ),
                                        [],
                                    )
                                )
                                for kind, values in maps.items()
                            }
                records.append(record)
            except Exception as error:  # noqa: BLE001 - owner-local failure
                records.append(
                    {
                        "owner_document_path": owner.get("document_path"),
                        "owner_scope": owner.get("owner_scope", "unresolved"),
                        "api": "IModelDocExtension.GetRenderMaterials2",
                        "display_state_option": 3,
                        "native_render_material_count": None,
                        "native_render_material_count_error": None,
                        "configuration": _json_raw_value(
                            owner.get("configuration")
                        ),
                        "display_state": owner.get("display_state"),
                        "display_states": copy.deepcopy(
                            owner.get("display_states")
                        ),
                        "display_states_state": owner.get(
                            "display_states_state"
                        ),
                        "call_state": "error",
                        "materials": [],
                        "mapping_errors": [],
                        "read_errors": {
                            "owner": _native_error_record(error),
                        },
                    }
                )
        return records

    def capture_prepared_geometry(self, source_scopes, on_activity=None):
        """Capture direct raw triangles for every occurrence in memory.

        IComponent2 bodies follow the occurrence's referenced configuration;
        the shared part document may have another configuration active.
        """
        activity = _ActivityEmitter(on_activity)

        def note(action: str, **fields) -> None:
            activity.emit(action, phase="capture", **fields)

        if not isinstance(source_scopes, Mapping):
            raise CadError(
                "cad_source_scope_missing",
                "prepared geometry requires a source scope for every component",
                {"returned": type(source_scopes).__name__},
            )
        missing = sorted(set(source_scopes) - self._components)
        if missing:
            raise CadError("cad_missing_component", missing[0])
        absent_scopes = sorted(self._components - set(source_scopes))
        if absent_scopes:
            raise CadError(
                "cad_source_scope_missing",
                "prepared geometry is missing an occurrence source scope",
                {"components": absent_scopes},
            )
        with self._current_components("geometry") as current:
            entries = {}
            render_material_cache = {}
            total = len(self._components)
            for index, component in enumerate(sorted(self._components), start=1):
                note(
                    "geometry_facts",
                    current_object=_activity_object(component),
                    completed=index - 1,
                    total=total,
                )
                try:
                    entries[component] = self._capture_component_geometry(
                        component,
                        current[component],
                        source_scopes[component],
                        current=current,
                        render_material_cache=render_material_cache,
                    )
                except Exception as error:  # noqa: BLE001 - continue the remaining objects
                    detail = getattr(error, "detail", None)
                    if not isinstance(detail, dict):
                        detail = {"native_detail": detail}
                    entries[component] = {
                        "component": component,
                        "triangles": 0,
                        "state": "error",
                        "error": {
                            "code": str(
                                getattr(error, "code", "")
                                or type(error).__name__
                            ),
                            "message": str(
                                getattr(error, "message", "") or error
                            )[:500],
                            "detail": detail,
                        },
                        "triangle_values": [],
                        "body_inventory": [],
                        "face_inventory": [],
                        "empty_faces": [],
                        "errors": [],
                        "bodies": {"solid": 0, "sheet": 0},
                        "source_scope": source_scopes[component],
                        "transform_raw": _json_raw_value(self._component_transforms.get(component)),
                        "appearance_raw": None,
                    }
                note(
                    "geometry_facts",
                    current_object=_activity_object(component),
                    completed=index,
                    total=total,
                )
            # The 0.25 s repeat throttle may drop the last per-component notes on a
            # fast batch; one forced final note keeps the accepted activity at n/N.
            note("geometry_facts", completed=total, total=total, force=True)
            return entries

    def _capture_component_geometry(
        self,
        component,
        holder,
        source_scope,
        *,
        current,
        render_material_cache,
    ):
        """Read direct face display triangles without writing or deriving."""

        if not isinstance(source_scope, dict):
            raise CadError(
                "cad_source_scope_missing",
                "a component has no exact source scope",
                {"component": component},
            )
        values: list[float] = []
        spans: list[dict[str, object]] = []
        body_inventory: list[dict[str, object]] = []
        body_entities: list[dict[str, object]] = []
        face_entities: list[dict[str, object]] = []
        feature_entities: list[dict[str, object]] = []
        empty_faces: list[dict[str, object]] = []
        errors: list[dict[str, object]] = []
        body_counts = {"solid": 0, "sheet": 0}
        face_count = 0
        occurrence_transform = self._component_transforms.get(component)
        used_api = (
            "IComponent2.GetBodies2/IBody2.GetFaces/"
            "IFace2.GetTessTriangles(True)"
        )
        configuration = source_scope.get("referenced_configuration")
        requested_context = {
            "configuration_semantics": "requested_configuration",
            "configuration_option": 3,
            "configuration_names": [configuration],
        }
        document = None
        document_extension = None
        document_error = None
        try:
            document_key = self._source_components[component][0]
            document = self._captured_document(
                document_key,
                component,
                "appearance",
            )
        except Exception as error:  # noqa: BLE001 - appearance is auxiliary evidence
            document_error = _native_error_record(error)
        native_active_configuration = None
        native_active_configuration_error = None
        if document is not None:
            try:
                native_active_configuration = _active_configuration(document)
            except Exception as error:  # noqa: BLE001 - body context stays unreadable
                native_active_configuration_error = _native_error_record(error)
            else:
                if not _is_text_name(native_active_configuration):
                    native_active_configuration_error = _appearance_read_error(
                        "cad_appearance_active_configuration_invalid",
                        "the native active configuration is unreadable",
                        _json_raw_value(native_active_configuration),
                    )
        document_api = (
            "IModelDoc2.MaterialPropertyValues"
            "/IModelDocExtension.HasMaterialPropertyValues"
            "/IModelDocExtension.GetTexture/ITexture.MaterialName"
        )
        if document is None:
            document_appearance = _unavailable_appearance_raw(
                configuration=native_active_configuration,
                material_context={
                    "configuration_semantics": "active_configuration",
                    "active_configuration": native_active_configuration,
                },
                used_api=document_api,
                error=document_error,
            )
        else:
            extension_error = None
            try:
                document_extension = _member(document, "Extension")
            except Exception as error:  # noqa: BLE001 - diagnostic owner only
                extension_error = _native_error_record(error)
            try:
                document_appearance = _appearance_raw(
                    document,
                    configuration=native_active_configuration,
                    material_member="MaterialPropertyValues",
                    material_context={
                        "configuration_semantics": "active_configuration",
                        "active_configuration": native_active_configuration,
                    },
                    used_api=document_api,
                    presence_owner=document_extension,
                    presence_api="IModelDocExtension.HasMaterialPropertyValues",
                    texture_owner=document_extension,
                )
            except Exception as error:  # noqa: BLE001 - appearance is auxiliary
                document_appearance = _unavailable_appearance_raw(
                    configuration=native_active_configuration,
                    material_context={
                        "configuration_semantics": "active_configuration",
                        "active_configuration": native_active_configuration,
                    },
                    used_api=document_api,
                    error=_native_error_record(error),
                )
            else:
                if extension_error is not None:
                    document_appearance["read_errors"][
                        "has_material_property_values"
                    ] = copy.deepcopy(extension_error)
                    document_appearance["read_errors"]["texture"] = (
                        copy.deepcopy(extension_error)
                    )
        occurrence_api = (
            "IComponent2.GetMaterialPropertyValues2(swSpecifyConfiguration)"
            "/IComponent2.HasMaterialPropertyValues"
            "/IComponent2.GetTexture/ITexture.MaterialName"
        )
        try:
            occurrence_appearance = _appearance_raw(
                holder,
                configuration=configuration,
                material_member="GetMaterialPropertyValues2",
                material_args=(3, (configuration,)),
                material_context=requested_context,
                used_api=occurrence_api,
            )
        except Exception as error:  # noqa: BLE001 - appearance is auxiliary evidence
            occurrence_appearance = _unavailable_appearance_raw(
                configuration=configuration,
                material_context=requested_context,
                used_api=occurrence_api,
                error=_native_error_record(error),
            )
        native_referenced_display_state, display_state_error = (
            _appearance_display_state(holder)
        )
        appearance_raw = {
            "component": component,
            "source_document_path": source_scope.get("source_document_path"),
            "referenced_configuration": configuration,
            "native_active_configuration": native_active_configuration,
            "native_active_configuration_error": native_active_configuration_error,
            "native_referenced_display_state": native_referenced_display_state,
            "native_referenced_display_state_error": display_state_error,
            "document": document_appearance,
            "occurrence": occurrence_appearance,
            "bodies": [],
            "state_render_materials": [],
        }
        for body_type, kind in ((0, "solid"), (1, "sheet")):
            try:
                bodies = self._body_list(holder, body_type, component)
            except CadError as error:
                errors.append(
                    {
                        "body_type": body_type,
                        "body_index": None,
                        "face_index": None,
                        "api": "IComponent2.GetBodies2",
                        "code": error.code,
                        "error": error.message,
                        "detail": error.detail,
                    }
                )
                continue
            body_counts[kind] = len(bodies)
            for body_index, raw_body in enumerate(bodies):
                try:
                    body = _interface(_dynamic(raw_body), IBODY2_IID)
                except Exception as error:  # noqa: BLE001
                    errors.append(
                        {
                            "body_type": body_type,
                            "body_index": body_index,
                            "face_index": None,
                            "api": "IComponent2.GetBodies2 binding",
                            "error": f"{type(error).__name__}: {error}"[:300],
                        }
                    )
                    continue
                body_name_error = None
                try:
                    raw_body_name = _member(body, "Name")
                    body_name = raw_body_name if isinstance(raw_body_name, str) else None
                    if not isinstance(raw_body_name, str):
                        body_name_error = {
                            "api": "IBody2.Name",
                            "error": f"IBody2.Name returned {type(raw_body_name).__name__}",
                        }
                except Exception as error:  # noqa: BLE001
                    body_name = None
                    body_name_error = {
                        "api": "IBody2.Name",
                        "error": f"{type(error).__name__}: {error}"[:300],
                    }
                if body_name_error is not None:
                    self.notes[
                        f"body_name_read_error:{component}:{body_type}/{body_index}"
                    ] = copy.deepcopy(body_name_error)
                # IBody2.MaterialPropertyValues2 is the related-body owner path
                # through IPartDoc.EnumRelatedBodies2; GetBodies2 here remains
                # the geometry traversal and cannot be relabeled as that owner.
                body_appearance_record = {
                    "component": component,
                    "enumeration_api": "IComponent2.GetBodies2",
                    "body_type": body_type,
                    "body_index": body_index,
                    "body_name": body_name,
                    "faces": [],
                }
                appearance_raw["bodies"].append(body_appearance_record)
                body_entities.append(
                    {
                        "component": component,
                        "body_type": body_type,
                        "body_index": body_index,
                        "body": body,
                    }
                )
                try:
                    faces = _as_list(_member(body, "GetFaces"))
                except Exception as error:  # noqa: BLE001
                    errors.append(
                        {
                            "body_type": body_type,
                            "body_index": body_index,
                            "face_index": None,
                            "api": "IBody2.GetFaces",
                            "error": f"{type(error).__name__}: {error}"[:300],
                        }
                    )
                    continue
                body_inventory.append(
                    {
                        "body_type": body_type,
                        "body_index": body_index,
                        "body_name": body_name,
                        "face_count": len(faces),
                    }
                )
                for face_index, raw_face in enumerate(faces):
                    face_count += 1
                    triangle_start = len(values) // 9
                    try:
                        face = _interface(_dynamic(raw_face), IFACE2_IID)
                    except Exception as error:  # noqa: BLE001
                        errors.append(
                            {
                                "body_type": body_type,
                                "body_index": body_index,
                                "body_name": body_name,
                                "face_index": face_index,
                                "api": "IFace2 binding",
                                "error": (
                                    f"{type(error).__name__}: {error}"
                                )[:300],
                            }
                        )
                        continue
                    face_api = (
                        "IFace2.MaterialPropertyValues"
                        "/IFace2.HasMaterialPropertyValues"
                        "/IFace2.GetTexture/ITexture.MaterialName"
                    )
                    try:
                        if native_active_configuration != configuration:
                            raise CadError(
                                "cad_appearance_active_configuration_mismatch",
                                "the face active configuration differs from the occurrence referenced configuration",
                                {
                                    "component": component,
                                    "referenced_configuration": configuration,
                                    "native_active_configuration": native_active_configuration,
                                },
                            )
                        face_appearance = _appearance_raw(
                            face,
                            configuration=native_active_configuration,
                            material_member="MaterialPropertyValues",
                            material_context={
                                "configuration_semantics": "active_configuration",
                                "active_configuration": native_active_configuration,
                            },
                            used_api=face_api,
                        )
                    except Exception as error:  # noqa: BLE001 - appearance is auxiliary
                        face_appearance = _unavailable_appearance_raw(
                            configuration=native_active_configuration,
                            material_context={
                                "configuration_semantics": "active_configuration",
                                "active_configuration": native_active_configuration,
                            },
                            used_api=face_api,
                            error=_native_error_record(error),
                        )
                    feature_identity, feature = _appearance_feature_identity(
                        face,
                        document_extension,
                    )
                    face_reference, face_reference_error = (
                        _appearance_persistent_reference(
                            document_extension,
                            face,
                        )
                    )
                    native_identity_errors = {}
                    if face_reference_error is not None:
                        native_identity_errors[
                            "persistent_reference"
                        ] = face_reference_error
                    face_appearance["native_identity"] = {
                        "owner_document_path": source_scope.get(
                            "source_document_path"
                        ),
                        "owner_scope": "component_document",
                        "persistent_reference": face_reference,
                        "feature": feature_identity,
                        "read_errors": native_identity_errors,
                    }
                    face_entities.append(
                        {
                            "component": component,
                            "body_type": body_type,
                            "body_index": body_index,
                            "face_index": face_index,
                            "face": face,
                        }
                    )
                    if feature is not None:
                        feature_entities.append(
                            {
                                "component": component,
                                "body_type": body_type,
                                "body_index": body_index,
                                "face_index": face_index,
                                "feature": feature,
                                "identity": feature_identity,
                            }
                        )
                    body_appearance_record["faces"].append(
                        {
                            "face_index": face_index,
                            "appearance": face_appearance,
                        }
                    )
                    try:
                        raw_values = _member(
                            face,
                            "GetTessTriangles",
                            True,
                        )
                        if raw_values is None:
                            raise ValueError(
                                "GetTessTriangles returned null"
                            )
                        if not isinstance(raw_values, (list, tuple)):
                            raise TypeError("GetTessTriangles did not return an array")
                        face_values = [_json_raw_value(value) for value in raw_values]
                    except Exception as error:  # noqa: BLE001
                        errors.append(
                            {
                                "body_type": body_type,
                                "body_index": body_index,
                                "body_name": body_name,
                                "face_index": face_index,
                                "api": "IFace2.GetTessTriangles(True)",
                                "error": (
                                    f"{type(error).__name__}: {error}"
                                )[:300],
                            }
                        )
                        continue
                    if not face_values:
                        empty_faces.append(
                            {
                                "body_type": body_type,
                                "body_index": body_index,
                                "body_name": body_name,
                                "face_index": face_index,
                            }
                        )
                        continue
                    count = len(face_values) // 9
                    values.extend(face_values)
                    spans.append(
                        {
                            "body_type": body_type,
                            "body_index": body_index,
                            "face_index": face_index,
                            "triangle_start": triangle_start,
                            "triangle_count": count,
                        }
                    )

        try:
            appearance_raw["state_render_materials"] = (
                self._capture_state_render_materials(
                    component=component,
                    holder=holder,
                    current=current,
                    leaf_document=document,
                    source_configuration=configuration,
                    leaf_display_state=native_referenced_display_state,
                    leaf_display_state_error=display_state_error,
                    bodies=body_entities,
                    faces=face_entities,
                    features=feature_entities,
                    cache=render_material_cache,
                )
            )
        except Exception as error:  # noqa: BLE001 - appearance is auxiliary
            appearance_raw["state_render_materials"].append(
                {
                    "owner_document_path": None,
                    "owner_scope": "unresolved",
                    "api": "IModelDocExtension.GetRenderMaterials2",
                    "display_state_option": 3,
                    "native_render_material_count": None,
                    "native_render_material_count_error": None,
                    "configuration": None,
                    "display_state": native_referenced_display_state,
                    "call_state": "error",
                    "materials": [],
                    "mapping_errors": [],
                    "read_errors": {
                        "owner_resolution": _native_error_record(error),
                    },
                }
            )
        state = (
            "observed"
            if not errors and not empty_faces and values
            else "empty"
            if not values and not errors
            else "partial"
            if values
            else "failed"
        )
        triangles = len(values) // 9
        self.notes["mesh:" + component] = used_api
        return {
            "component": component,
            "used_api": used_api,
            "triangles": triangles,
            "triangle_values": values,
            "body_inventory": body_inventory,
            "face_inventory": spans,
            "empty_faces": empty_faces,
            "errors": errors,
            "faces": face_count,
            "units": "m",
            "state": state,
            "representation": "cad_face_display_triangles_raw",
            "source_scope": source_scope,
            "transform_raw": _json_raw_value(occurrence_transform),
            "bodies": body_counts,
            "appearance_raw": appearance_raw,
        }

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
            facts[key] = _raw_scalar(value)
            if not valid:
                facts.setdefault("unavailable", {})[member] = (
                    f"Unsupported native type: {type(value).__name__}"
                )
        try:
            visible = _member(app, "Visible")
        except Exception as error:
            facts["visible"] = None
            facts.setdefault("unavailable", {})["Visible"] = str(error)
        else:
            facts["visible"] = _raw_scalar(visible)
            if type(visible) is not bool:
                facts.setdefault("unavailable", {})["Visible"] = (
                    f"Unsupported native type: {type(visible).__name__}"
                )
        return facts


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


    def _record_save_flag(self, doc, path):
        """Record what SolidWorks says about saving; never decide a capture by it.

        ``IModelDoc2::GetSaveFlag`` answers "would SolidWorks prompt me to save this
        document?".  The API reference says many operations set it and that a
        document created by an older SOLIDWORKS release starts with it set, so it is
        not evidence about anybody's edits.  It cannot be evidence about the
        operator's session either: this adapter opens the revision in an
        application it owns and hashes the source bytes, so the flag only
        describes bookkeeping inside that owned session. Keep it for the
        snapshot and let the caller show it, but never refuse a revision because
        of it.
        """

        try:
            document = _member(doc, "GetPathName")
        except Exception:  # noqa: BLE001 - path fallback is supplied by the caller
            document = None
        key = normalize_document_path(os.path.abspath(str(document or path)))
        try:
            value = _method(doc, "GetSaveFlag")
        except Exception as error:  # noqa: BLE001 - diagnostic flag never blocks primary reads
            self.save_flags[key] = None
            self.save_flag_errors[key] = _native_error_record(error)
        else:
            self.save_flags[key] = _raw_scalar(value)
            if type(value) is not bool:
                self.save_flag_errors[key] = {
                    "code": "cad_save_flag_invalid",
                    "message": "GetSaveFlag did not return bool",
                    "detail": {"value_type": type(value).__name__, "value": repr(value)},
                }


    def document_state(self, path):
        """Saved state plus configuration facts; never saves anything itself.

        ``saved`` is SolidWorks' own answer (``not GetSaveFlag``): record it, but do
        not read it as "the operator has unsaved edits" - see _record_save_flag.
        """

        doc = self._ensure_document(path)
        document_path = _member(doc, "GetPathName")
        if not isinstance(document_path, str) or not document_path.strip():
            raise CadError(
                "cad_document_identity",
                "document_state has no readable native document path",
                {"raw_path": repr(document_path)},
            )
        try:
            _manager, active = _active_configuration_view(doc)
            configuration = _member(active, "Name")
        except Exception as error:  # noqa: BLE001 - real configuration identity is required
            raise CadError(
                "cad_configuration_unreadable",
                "document_state cannot bind the active native configuration",
                {"path": document_path, "error": str(error)},
            ) from error
        if not isinstance(configuration, str) or not configuration.strip():
            raise CadError(
                "cad_configuration_unreadable",
                "document_state has no readable active configuration",
                {"path": document_path, "raw_configuration": repr(configuration)},
            )
        state = _discovery_document_state(doc)
        state["path"] = document_path
        state["active_configuration"] = configuration
        save_flag = state.get("save_flag")
        state["saved"] = not save_flag if type(save_flag) is bool else None
        try:
            title = _member(doc, "GetTitle")
        except Exception as error:  # noqa: BLE001 - title is diagnostic only
            state["title"] = None
            state["title_error"] = _native_error_record(error)
        else:
            state["title"] = _raw_scalar(title)
            if not isinstance(title, str):
                state["title_error"] = {
                    "code": "cad_title_invalid",
                    "message": "GetTitle did not return string",
                    "detail": {"value_type": type(title).__name__, "value": repr(title)},
                }
        try:
            configurations = _as_list(_member(doc, "GetConfigurationNames"))
        except Exception as error:  # noqa: BLE001 - configuration names are diagnostic only
            state["configurations"] = None
            state["configurations_error"] = _native_error_record(error)
        else:
            state["configurations"] = [_raw_scalar(name) for name in configurations]
            if any(not isinstance(name, str) for name in configurations):
                state["configurations_error"] = {
                    "code": "cad_configuration_names_invalid",
                    "message": "GetConfigurationNames returned non-string entries",
                    "detail": {"raw_values": [_raw_scalar(value) for value in configurations]},
                }
        # The installed SDK declares no document lightweight property; record an
        # explicit unsupported diagnostic instead of probing undeclared spellings.
        state["lightweight"] = None
        state["unsupported"] = ["lightweight"]
        return state




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
                    # selection walk's stitching rule so the
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
                        global_matrix = _compose_frames(parent_matrix, local_matrix)
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
                    composed = _compose_frames(component_matrix, local)
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




def _frame_from_raw(values: Sequence[float], *, field: str) -> Matrix:
    try:
        return tuple(transform_from_solidworks(values))  # type: ignore[return-value]
    except CadError as error:
        detail = error.detail if isinstance(error.detail, dict) else {"detail": error.detail}
        raise CadError(error.code, error.message, {**detail, "field": field}) from error




def _native_error_record(error):
    detail = getattr(error, "detail", None)
    return {
        "code": str(getattr(error, "code", "") or type(error).__name__),
        "message": str(getattr(error, "message", "") or error)[:500],
        "detail": copy.deepcopy(detail) if isinstance(detail, (dict, list)) else detail,
    }


def _compose_link_group_selections(groups, leaves, provider_names):
    if not isinstance(groups, list) or not groups:
        raise CadError("link_group_body_missing", "native groups must be a non-empty list")
    leaf_rows = [str(name) for name in leaves]
    if len(set(leaf_rows)) != len(leaf_rows):
        raise CadError("link_group_occurrence_duplicate", "the captured scene repeats an occurrence")
    body_of = {}
    for group in groups:
        if not isinstance(group, dict):
            raise CadError("link_group_body_unnamed", "a native group is not an object")
        body_name = str(group.get("name") or group.get("id") or "")
        if not body_name:
            raise CadError("link_group_body_unnamed", "every native group needs a name")
        members = [str(item) for item in (group.get("components") or [])]
        if not members:
            raise CadError("link_group_empty_selection", "a native group has no components", {"body": body_name})
        for member in members:
            if member not in leaf_rows:
                raise CadError(
                    "link_group_member_unknown",
                    "a native group names an unknown occurrence",
                    {"body": body_name, "component": member},
                )
            if member in body_of:
                raise CadError(
                    "link_group_member_duplicate",
                    "an occurrence belongs to several native groups",
                    {"component": member, "bodies": [body_of[member], body_name]},
                )
            body_of[member] = body_name
    providers = {}
    for provider in sorted({str(name) for name in provider_names}):
        subtree = {leaf for leaf in leaf_rows if leaf == provider or leaf.startswith(provider + "/")}
        if not subtree:
            raise CadError("link_group_provider_unknown", "an effective instance is not a captured occurrence")
        owners = {body_of.get(leaf) for leaf in subtree}
        named = {owner for owner in owners if owner}
        if len(named) > 1:
            raise CadError(
                "link_group_provider_spans_bodies",
                "an effective instance spans several native groups",
                {"provider": provider, "bodies": sorted(named)},
            )
        if named and len(owners) != 1:
            raise CadError(
                "link_group_provider_partial",
                "an effective instance is only partially assigned",
                {"provider": provider, "body": sorted(named)[0]},
            )
        if named:
            providers[provider] = subtree
    selections = {}
    for group in groups:
        body_name = str(group.get("name") or group.get("id"))
        members = {str(item) for item in (group.get("components") or [])}
        inner = [provider for provider, subtree in providers.items() if subtree <= members]
        top_most = [
            provider
            for provider in inner
            if not any(other != provider and provider.startswith(other + "/") for other in inner)
        ]
        covered = set()
        for provider in top_most:
            covered |= providers[provider]
        uncovered = sorted(members - covered)
        selection = sorted([*top_most, *uncovered])
        if not selection:
            raise CadError("link_group_empty_selection", "a native group has no selectable occurrence")
        selections[body_name] = {
            "selection": selection,
            "providers": sorted(top_most),
            "uncovered_leaves": uncovered,
        }
    return selections


def _project_scene(raw_scene):
    if not isinstance(raw_scene, dict):
        raise CadError("cad_prepared_scene_facts_invalid", "scene_raw is not an object")
    projected_by_name = {}
    occurrences = raw_scene.get("occurrences")
    components = raw_scene.get("components")
    if not isinstance(occurrences, list) or not isinstance(components, list):
        raise CadError("cad_prepared_scene_facts_invalid", "scene_raw occurrences are missing")
    for occurrence in occurrences:
        if not isinstance(occurrence, dict) or not isinstance(occurrence.get("name"), str):
            raise CadError("cad_prepared_scene_facts_invalid", "a raw occurrence has no identity")
        projected_by_name[occurrence["name"]] = _frame_from_raw(
            occurrence.get("transform_raw"),
            field=f"occurrences[{occurrence['name']}].transform_raw",
        )
    projected_components = []
    for occurrence in components:
        name = occurrence.get("name") if isinstance(occurrence, dict) else None
        if name not in projected_by_name:
            raise CadError("cad_prepared_scene_facts_invalid", "a component has no raw occurrence")
        is_fixed = occurrence.get("is_fixed")
        if type(is_fixed) is not bool:
            raise CadError(
                "cad_component_fixed_invalid",
                "a raw component has no native boolean IsFixed value",
                {
                    "component": name,
                    "value": repr(is_fixed),
                    "error": copy.deepcopy(occurrence.get("is_fixed_error")),
                },
            )
        projected_components.append(
            RawComponent(
                name=name,
                path=str(occurrence.get("path") or ""),
                transform=projected_by_name[name],
                is_fixed=is_fixed,
                document_type=str(occurrence.get("document_type") or ""),
            )
        )
    coordinate_systems = {}
    raw_datums = raw_scene.get("coordinate_systems")
    if not isinstance(raw_datums, dict):
        raise CadError("cad_prepared_scene_facts_invalid", "scene_raw coordinate systems are missing")
    for key, datum in raw_datums.items():
        if not isinstance(datum, dict):
            raise CadError("cad_prepared_scene_facts_invalid", "a raw coordinate system is malformed")
        datum_errors = datum.get("read_errors")
        if not isinstance(datum_errors, dict):
            raise CadError("cad_prepared_scene_facts_invalid", "a raw coordinate system has no error map")
        if datum_errors:
            first_error = next(iter(datum_errors.values()), {})
            raise CadError(
                str(first_error.get("code") or "cad_coordinate_system_invalid"),
                str(first_error.get("message") or "raw coordinate-system data is invalid"),
                {
                    "coordinate_system": key,
                    "array_data": copy.deepcopy(datum.get("array_data")),
                    "error": copy.deepcopy(first_error),
                },
            )
        local = _frame_from_raw(datum.get("array_data"), field=f"coordinate_systems[{key}].array_data")
        owner = str(datum.get("owner") or "")
        if owner:
            placement_raw = datum.get("occurrence_transform_raw")
            if not isinstance(placement_raw, list) or len(placement_raw) != 16:
                raise CadError(
                    "cad_coordinate_system_owner_unbound",
                    "a component coordinate system has no raw occurrence placement",
                    {"coordinate_system": key, "component": owner},
                )
            placement = _frame_from_raw(
                placement_raw,
                field=f"coordinate_systems[{key}].occurrence_transform_raw",
            )
            composed = _compose_frames(
                [placement[offset : offset + 4] for offset in range(0, 16, 4)],
                [local[offset : offset + 4] for offset in range(0, 16, 4)],
            )
            local = tuple(value for row in composed for value in row)
        coordinate_systems[str(key)] = local
    return RawScene(
        document=str(raw_scene.get("document") or ""),
        components=projected_components,
        coordinate_systems=coordinate_systems,
        mass_properties={},
        notes=copy.deepcopy(raw_scene.get("notes") or {}),
    )


def _project_real(value, *, component, field):
    if type(value) not in (int, float) or not math.isfinite(float(value)):
        raise CadError(
            f"cad_mass_property_{field}_invalid",
            f"{field} must be a finite non-boolean number",
            {"component": component, "value": _raw_scalar(value)},
        )
    return float(value)


def _project_mass_reading(raw):
    if not isinstance(raw, dict):
        raise CadError("cad_mass_projection_invalid", "a raw mass reading is not an object")
    read_errors = raw.get("read_errors")
    if not isinstance(read_errors, dict):
        raise CadError("cad_mass_projection_invalid", "a raw mass reading has no read-error map")
    for field, error in read_errors.items():
        if not isinstance(error, dict):
            raise CadError("cad_mass_projection_invalid", "a raw mass read error is malformed", {"field": field})
        if field in {"volume_m3", "density_kg_m3"}:
            continue
        if field == "material" and error.get("code") == "cad_material_provenance_missing":
            continue
        raise CadError(
            str(error.get("code") or "cad_mass_read_failed"),
            str(error.get("message") or f"native {field} read failed"),
            copy.deepcopy(error.get("detail")),
        )
    native_raw = raw.get("native_raw")
    if not isinstance(native_raw, dict):
        raise CadError("cad_mass_projection_invalid", "a raw mass reading has no native getter values")
    overrides = native_raw.get("overrides")
    if (
        not isinstance(overrides, dict)
        or set(overrides) != {"OverrideMass", "OverrideCenterOfMass", "OverrideMomentsOfInertia"}
        or any(type(value) is not bool for value in overrides.values())
    ):
        raise CadError("cad_mass_override_invalid", "native mass override flags are malformed", overrides)
    if raw.get("mode") != "effective_instance" and any(overrides.values()):
        raise CadError("cad_mass_override", "pure-CAD export refuses overrides", overrides)
    if (
        raw.get("mode") == "effective_instance"
        and raw.get("state") == "sheet_only"
        and not (isinstance(overrides, dict) and any(value is True for value in overrides.values()))
    ):
        raise CadError(
            "cad_effective_instance_flags_invalid",
            "a sheet-only effective instance needs an active override",
            {"overrides": overrides},
        )
    component = str(
        (raw.get("reference") or {}).get("part_document")
        or (raw.get("reference") or {}).get("document")
        or "mass"
    )
    mass = _project_real(native_raw.get("mass_kg"), component=component, field="mass")
    com_raw = native_raw.get("center_of_mass_m")
    if not isinstance(com_raw, list) or len(com_raw) != 3:
        raise CadError(
            "cad_mass_property_center_invalid",
            "center_of_mass_m must be a three-value array",
            {"component": component, "value": _raw_scalar(com_raw)},
        )
    com = [_project_real(value, component=component, field="center") for value in com_raw]
    if mass <= 0.0:
        raise CadError("cad_mass_projection_invalid", "a raw mass or center is invalid")
    inertia, _axis = _inertia_from_raw(native_raw.get("inertia_values"), component)
    reference = copy.deepcopy(raw.get("reference") or {})
    for key, source_key in (("volume_m3", "volume_m3"), ("density_kg_m3", "density_kg_m3")):
        value = native_raw.get(source_key)
        reference[key] = (
            _project_real(value, component=component, field=key) if value is not None else None
        )
    projected = {
        "mass": mass,
        "com": com,
        "inertia": inertia,
        "reference": reference,
    }
    for key in ("mode", "role", "state", "measurement_basis", "document_type"):
        if raw.get(key) is not None:
            projected[key] = raw[key]
    return projected


def _project_effective_reading(raw):
    projected = copy.deepcopy(raw)
    native_raw = projected.pop("native_raw", None)
    part_measurement_raw = projected.pop("part_measurement_raw", None)
    if not isinstance(native_raw, dict):
        return projected
    read_errors = projected.get("read_errors")
    if not isinstance(read_errors, dict):
        raise CadError("cad_mass_projection_invalid", "an effective-instance reading has no error map")
    override_error = read_errors.get("overrides")
    if isinstance(override_error, dict):
        raise CadError(
            str(override_error.get("code") or "cad_mass_override_unreadable"),
            str(override_error.get("message") or "native mass override flags are unreadable"),
            copy.deepcopy(override_error.get("detail")),
        )
    projected.pop("read_errors", None)
    overrides = native_raw.get("overrides")
    if (
        not isinstance(overrides, dict)
        or set(overrides) != {"OverrideMass", "OverrideCenterOfMass", "OverrideMomentsOfInertia"}
        or any(type(value) is not bool for value in overrides.values())
    ):
        raise CadError("cad_mass_override_invalid", "native mass override flags are malformed", overrides)
    reference = projected.get("reference")
    if (
        isinstance(reference, dict)
        and reference.get("measurement_basis") == "cad_authored_effective_instance"
        and not any(overrides.values())
    ):
        raise CadError(
            "cad_effective_instance_flags_invalid",
            "an authored effective instance needs an active override",
            {"overrides": overrides},
        )
    selected_components = reference.get("selected_components") if isinstance(reference, dict) else None
    component = str(
        selected_components[0]
        if isinstance(selected_components, list) and selected_components
        else "effective_instance"
    )
    mass = _project_real(native_raw.get("mass_kg"), component=component, field="mass")
    com_raw = native_raw.get("center_of_mass_m")
    if not isinstance(com_raw, list) or len(com_raw) != 3:
        raise CadError(
            "cad_mass_property_center_invalid",
            "center_of_mass_m must be a three-value array",
            {"component": component, "value": _raw_scalar(com_raw)},
        )
    com = [_project_real(value, component=component, field="center") for value in com_raw]
    if mass <= 0.0:
        raise CadError("cad_mass_projection_invalid", "an effective-instance mass or center is invalid")
    inertia, _axis = _inertia_from_raw(native_raw.get("inertia_values"), component)
    projected["mass"] = mass
    projected["com"] = com
    projected["inertia"] = [list(row) for row in inertia]
    if isinstance(part_measurement_raw, dict):
        part = _project_mass_reading(part_measurement_raw)
        reference = part.get("reference") or {}
        projected["part_measurement"] = {
            "mass_kg": part["mass"],
            "center_of_mass_m": list(part["com"]),
            "inertia_kg_m2": [list(row) for row in part["inertia"]],
            "scope": reference.get("scope"),
            "used_api": reference.get("used_api"),
            "product_convention": reference.get("product_convention"),
            **{
                key: reference[key]
                for key in ("volume_m3", "density_kg_m3")
                if key in reference
            },
        }
    return projected


def _project_component_mass(raw):
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise CadError("cad_component_mass_projection_invalid", "raw component mass facts are not an object")
    projected = copy.deepcopy(raw)
    for row in projected.get("instances") or []:
        if not isinstance(row, dict):
            raise CadError("cad_component_mass_projection_invalid", "a raw component mass row is malformed")
        name = str(row.get("name") or "component")
        row["context_mass_kg"] = _project_real(row.get("context_mass_kg"), component=name, field="mass")
        volume = row.get("context_volume_m3")
        if volume is not None:
            row["context_volume_m3"] = _project_real(volume, component=name, field="volume")
        overrides = row.get("overrides")
        if (
            not isinstance(overrides, dict)
            or set(overrides) != {"OverrideMass", "OverrideCenterOfMass", "OverrideMomentsOfInertia"}
            or any(type(value) is not bool for value in overrides.values())
        ):
            raise CadError(
                "cad_component_mass_projection_invalid",
                "a raw component override row is malformed",
                {"component": name},
            )
        if isinstance(row.get("effective"), dict):
            row["effective"] = _project_effective_reading(row["effective"])
    return projected


def _project_link_group_mass(raw):
    if not isinstance(raw, dict) or not isinstance(raw.get("links"), dict):
        raise CadError("cad_link_group_projection_invalid", "raw link-group mass facts are missing")
    links = {}
    for body_name, body in raw["links"].items():
        if not isinstance(body, dict) or not isinstance(body.get("reads"), list) or len(body["reads"]) != 2:
            raise CadError(
                "cad_link_group_projection_invalid",
                "a raw link-group reading does not contain two reads",
                {"body": body_name},
            )
        reads = []
        essential_errors = (
            "mass_kg",
            "center_of_mass_m",
            "inertia_values",
            "overrides",
        )
        for read_index, read in enumerate(body["reads"]):
            if not isinstance(read, dict):
                raise CadError("cad_link_group_projection_invalid", "a raw link-group read is not an object")
            read_errors = read.get("read_errors")
            if not isinstance(read_errors, dict):
                raise CadError("cad_link_group_projection_invalid", "a raw link-group read has no error map")
            for field in essential_errors:
                native_error = read_errors.get(field)
                if native_error is None:
                    continue
                if not isinstance(native_error, dict):
                    raise CadError(
                        "cad_link_group_projection_invalid",
                        "a raw link-group field error is malformed",
                        {"body": body_name, "read": read_index, "field": field},
                    )
                native_detail = native_error.get("detail")
                location = {
                    "body": body_name,
                    "read": read_index,
                    "field": field,
                }
                if isinstance(native_detail, dict):
                    detail = {**copy.deepcopy(native_detail), **location}
                else:
                    detail = {
                        "native_detail": copy.deepcopy(native_detail),
                        **location,
                    }
                raise CadError(
                    str(native_error.get("code") or "cad_mass_read_failed"),
                    str(native_error.get("message") or f"native {field} read failed"),
                    detail,
                )
            overrides = read.get("overrides")
            if (
                not isinstance(overrides, dict)
                or set(overrides) != {"OverrideMass", "OverrideCenterOfMass", "OverrideMomentsOfInertia"}
                or any(type(value) is not bool for value in overrides.values())
            ):
                raise CadError("cad_mass_override_invalid", "native mass override flags are malformed", overrides)
            mass = _project_real(read.get("mass_kg"), component=str(body_name), field="mass")
            com_raw = read.get("com_m")
            if not isinstance(com_raw, list) or len(com_raw) != 3:
                raise CadError(
                    "cad_mass_property_center_invalid",
                    "a raw link-group COM must be a three-value array",
                    {"body": body_name, "value": _raw_scalar(com_raw)},
                )
            com = [
                _project_real(value, component=str(body_name), field="center")
                for value in com_raw
            ]
            if mass <= 0.0:
                raise CadError("cad_link_group_projection_invalid", "a raw link-group mass or center is invalid")
            inertia, _axis = _inertia_from_raw(read.get("inertia_values_raw"), str(body_name))
            origin_raw = read.get("inertia_values_about_origin_api")
            projected_read = {
                "mass_kg": mass,
                "com_m": com,
                "inertia_tensor_about_com": [list(row) for row in inertia],
                "overrides": dict(overrides),
                "inertia_values_about_origin_api": copy.deepcopy(origin_raw),
            }
            for key in ("volume_m3", "density_kg_m3"):
                if key in read:
                    projected_read[key] = read[key]
            reads.append(projected_read)
        tensor_delta = max(
            abs(reads[0]["inertia_tensor_about_com"][row][column] - reads[1]["inertia_tensor_about_com"][row][column])
            for row in range(3)
            for column in range(3)
        )
        links[body_name] = {
            "selection": [str(item) for item in body.get("selection") or []],
            "reads": reads,
            "reproducibility": {
                "mass_delta_kg": abs(reads[0]["mass_kg"] - reads[1]["mass_kg"]),
                "com_delta_norm_m": math.dist(reads[0]["com_m"], reads[1]["com_m"]),
                "tensor_delta_max_abs": tensor_delta,
            },
        }
    return {
        "assembly": str(raw.get("assembly") or ""),
        "configuration": str(raw.get("configuration") or ""),
        "used_api": str(raw.get("used_api") or ""),
        "selection_rule": str(raw.get("selection_rule") or ""),
        "links": links,
    }


def _project_cylinder_axis(raw):
    read_errors = raw.get("read_errors")
    if not isinstance(read_errors, dict):
        raise CadError("cad_axis_reference_invalid", "raw cylinder axis has no error map")
    if read_errors:
        raise CadError(
            "cad_axis_reference_unreadable",
            "raw cylinder-axis selection is incomplete",
            {"component": raw.get("component"), "errors": copy.deepcopy(read_errors)},
        )
    candidates = raw.get("face_candidates")
    if not isinstance(candidates, list):
        raise CadError("cad_axis_reference_invalid", "raw cylinder axis has no face candidates")
    cylindrical = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            raise CadError("cad_axis_reference_invalid", "a raw axis candidate is malformed")
        errors = candidate.get("read_errors")
        if not isinstance(errors, dict):
            raise CadError("cad_axis_reference_invalid", "a raw axis candidate has no error map")
        if "is_cylinder" in errors:
            raise CadError(
                "cad_axis_reference_unreadable",
                "ISurface.IsCylinder could not be read",
                copy.deepcopy(errors["is_cylinder"]),
            )
        is_cylinder = candidate.get("is_cylinder")
        if type(is_cylinder) is not bool:
            raise CadError(
                "cad_axis_reference_invalid",
                "ISurface.IsCylinder did not return bool",
                {"value": repr(is_cylinder)},
            )
        if not is_cylinder:
            continue
        if "surface" in errors or "cylinder_params" in errors:
            raise CadError(
                "cad_axis_reference_unreadable",
                "a native cylindrical face did not expose CylinderParams",
                copy.deepcopy(errors),
            )
        cylindrical.append(candidate)
    if len(cylindrical) != 1:
        raise CadError(
            "cad_axis_reference_ambiguous" if cylindrical else "cad_axis_reference_not_cylinder",
            "the selector does not identify exactly one native cylindrical face",
            {
                "component": raw.get("component"),
                "feature_name": raw.get("feature_name"),
                "face_index": raw.get("face_index"),
                "cylindrical_face_count": len(cylindrical),
            },
        )
    candidate = cylindrical[0]
    raw_params = candidate.get("cylinder_params_raw")
    if not isinstance(raw_params, (list, tuple)) or any(type(value) not in (int, float) for value in raw_params):
        raise CadError("cad_axis_reference_not_cylinder", "CylinderParams did not return native numeric values")
    try:
        params = [float(value) for value in raw_params]
    except Exception as error:  # noqa: BLE001
        raise CadError(
            "cad_axis_reference_not_cylinder",
            "CylinderParams is not a numeric array",
            {"raw_values": copy.deepcopy(raw_params)},
        ) from error
    if len(params) != 7 or not all(map(math.isfinite, params)):
        raise CadError(
            "cad_axis_reference_not_cylinder",
            "CylinderParams must contain seven finite values",
            {"raw_values": copy.deepcopy(raw_params)},
        )
    point, direction, radius = params[0:3], params[3:6], params[6]
    norm = math.hypot(*direction)
    if abs(norm - 1.0) > 1e-6 or radius <= 0.0:
        raise CadError(
            "cad_axis_reference_not_cylinder",
            "native cylinder parameters require a unit axis and positive radius",
            {
                "component": raw.get("component"),
                "face_index": candidate.get("face_index"),
                "radius": radius,
                "axis_norm": norm,
            },
        )
    result = {
        "kind": "cylinder",
        "component": str(raw.get("component") or ""),
        "body_type": str(raw.get("body_type_native")),
        "selector": copy.deepcopy(raw.get("selector") or {}),
        "surface": "cylinder",
        "coordinate_frame": "component_local",
        "direction_semantics": "undirected_axis_line",
        "body_index": candidate.get("body_index"),
        "face_index": candidate.get("face_index"),
        "cylinder_params_raw": params,
        "is_cylinder_raw": candidate["is_cylinder"],
        "axis_point_m": point,
        "axis_direction": [value / norm for value in direction],
        "radius_m": radius,
        "used_api": str(raw.get("used_api") or ""),
    }
    if candidate.get("feature_name") is not None:
        result["feature_name"] = candidate["feature_name"]
    if candidate.get("persist_reference_b64") is not None:
        result["persist_reference_b64"] = candidate["persist_reference_b64"]
    return result


def project_axis_references(raw_rows):
    """Project captured axis raw values after the owned CAD session closes."""

    if not isinstance(raw_rows, list):
        raise CadError("cad_axis_reference_invalid", "raw axis rows are not a list")
    projected = []
    for raw in raw_rows:
        if not isinstance(raw, dict) or not isinstance(raw.get("joint"), str) or not raw["joint"]:
            raise CadError("cad_axis_reference_invalid", "a raw axis row has no joint identity")
        kind = raw.get("kind")
        if kind == "reference_axis":
            result = _project_reference_axis(raw)
        elif kind == "cylinder":
            result = _project_cylinder_axis(raw)
        else:
            raise CadError("cad_axis_reference_invalid", "a raw axis row has no supported kind")
        projected.append({"joint": raw["joint"], **result})
    return projected


def project_prepared_capture(raw_facts) -> dict:
    """Project one sealed native fact set after the owned session closes."""

    if not isinstance(raw_facts, dict) or set(raw_facts) != NATIVE_CAPTURE_FACTS_KEYS:
        raise CadError(
            "cad_prepared_capture_facts",
            "raw Capture facts have an unknown shape",
            {
                "expected": sorted(NATIVE_CAPTURE_FACTS_KEYS),
                "received": sorted(raw_facts) if isinstance(raw_facts, dict) else None,
            },
        )
    scene = _project_scene(raw_facts["scene_raw"])
    part_mass_raw = raw_facts["part_mass_raw"]
    if not isinstance(part_mass_raw, dict):
        raise CadError("cad_prepared_capture_facts", "part_mass_raw is not an object")
    notes = raw_facts["notes"] if isinstance(raw_facts["notes"], dict) else {}
    for component in scene.components:
        if component.name in part_mass_raw:
            continue
        error = notes.get("mass_error:" + component.name)
        if isinstance(error, dict):
            raise CadError(
                str(error.get("code") or "cad_mass_property_unavailable"),
                str(error.get("message") or "part mass getter failed"),
                copy.deepcopy(error.get("detail")),
            )
        raise CadError(
            "cad_prepared_capture_facts",
            "part mass getter produced neither raw facts nor an error",
            {"component": component.name},
        )
    scene.mass_properties = {
        name: _project_mass_reading(row)
        for name, row in part_mass_raw.items()
    }
    scene.notes = copy.deepcopy(raw_facts["notes"])
    assembly_mass_raw = raw_facts["assembly_mass_raw"]
    if assembly_mass_raw is None:
        error = notes.get("assembly_mass_error")
        if isinstance(error, dict):
            raise CadError(
                str(error.get("code") or "cad_mass_property_unavailable"),
                str(error.get("message") or "assembly mass getter failed"),
                copy.deepcopy(error.get("detail")),
            )
        raise CadError(
            "cad_prepared_capture_facts",
            "assembly mass getter produced neither raw facts nor an error",
        )
    if not isinstance(assembly_mass_raw, dict):
        raise CadError("cad_prepared_capture_facts", "assembly_mass_raw is not an object")
    component_mass_raw = raw_facts["component_mass_raw"]
    if component_mass_raw is None:
        error = notes.get("component_mass_error")
        if isinstance(error, dict):
            raise CadError(
                str(error.get("code") or "cad_mass_property_unavailable"),
                str(error.get("message") or "component mass getter failed"),
                copy.deepcopy(error.get("detail")),
            )
        raise CadError(
            "cad_prepared_capture_facts",
            "component mass getter produced neither raw facts nor an error",
        )
    if not isinstance(component_mass_raw, dict):
        raise CadError("cad_prepared_capture_facts", "component_mass_raw is not an object")
    component_mass = copy.deepcopy(component_mass_raw)
    effective_instances = {}
    effective_error = None
    if isinstance(notes.get("component_mass_error"), dict):
        effective_error = copy.deepcopy(notes["component_mass_error"])
    if isinstance(component_mass, dict):
        for row in component_mass.get("instances") or []:
            if not isinstance(row, dict):
                continue
            name = str(row.get("name") or "")
            effective = row.get("effective")
            if name and isinstance(effective, dict):
                effective_instances[name] = _project_effective_reading(effective)
            read_errors = row.get("read_errors")
            if name and (row.get("effective_error") is not None or read_errors):
                first_error = next(iter(read_errors.values()), {}) if isinstance(read_errors, dict) else {}
                effective_error = effective_error or {
                    "reason": str(
                        row.get("effective_error")
                        or (first_error.get("code") if isinstance(first_error, dict) else None)
                        or "cad_component_mass_read_error"
                    ),
                    "message": f"effective instance {name} was not readable",
                }
        if component_mass.get("errors"):
            effective_error = effective_error or {
                "reason": "cad_component_mass_context_partial",
                "message": "component mass context retained native read errors",
            }
    for name, row in part_mass_raw.items():
        if isinstance(row, dict) and row.get("mode") == "effective_instance":
            effective_instances[str(name)] = _project_effective_reading(row)
    component_mass = _project_component_mass(component_mass_raw)
    assembly_mass = _project_mass_reading(assembly_mass_raw)
    return {
        "scene": scene,
        "assembly_mass": assembly_mass,
        "component_mass": component_mass,
        "effective_instances": effective_instances,
        "effective_instances_error": effective_error,
        "link_group_mass_properties": _project_link_group_mass(raw_facts["link_group_mass_raw"]),
    }


def _raw_geometry_record(
    component: str,
    facts: object,
) -> tuple[list[float], list[dict], list[dict], dict, str, list[float]]:
    if not isinstance(facts, Mapping):
        raise CadError(
            "cad_mesh_export_failed",
            "prepared geometry facts are not a mapping",
            {"component": component, "returned": type(facts).__name__},
        )
    if facts.get("component") != component:
        raise CadError(
            "cad_mesh_export_failed",
            "prepared geometry facts name another component",
            {"component": component, "reported": facts.get("component")},
        )
    if facts.get("state") != "observed" or facts.get("errors") or facts.get("empty_faces"):
        raise CadError(
            "cad_native_tessellation_unreadable",
            "prepared geometry is incomplete",
            {
                "component": component,
                "state": facts.get("state"),
                "errors": copy.deepcopy(facts.get("errors") or []),
                "empty_faces": copy.deepcopy(facts.get("empty_faces") or []),
            },
        )
    triangle_values = facts.get("triangle_values")
    body_inventory = facts.get("body_inventory")
    face_inventory = facts.get("face_inventory")
    body_counts = facts.get("bodies")
    transform_raw = facts.get("transform_raw")
    used_api = facts.get("used_api")
    source_scope = facts.get("source_scope")
    if (
        not isinstance(triangle_values, list)
        or not triangle_values
        or len(triangle_values) % 9
        or any(
            type(value) not in (int, float)
            or not math.isfinite(float(value))
            for value in triangle_values
        )
        or not isinstance(body_inventory, list)
        or not isinstance(face_inventory, list)
        or not isinstance(body_counts, dict)
        or set(body_counts) != {"solid", "sheet"}
        or not isinstance(transform_raw, list)
        or len(transform_raw) != 16
        or any(
            type(value) not in (int, float)
            or not math.isfinite(float(value))
            for value in transform_raw
        )
        or not isinstance(used_api, str)
        or not used_api
        or not isinstance(source_scope, dict)
    ):
        raise CadError(
            "cad_mesh_export_failed",
            "prepared geometry facts are malformed",
            {"component": component},
        )
    return (
        [float(value) for value in triangle_values],
        copy.deepcopy(body_inventory),
        copy.deepcopy(face_inventory),
        dict(body_counts),
        used_api,
        [float(value) for value in transform_raw],
    )


def _publish_canonical_artifact(temporary: Path, target: Path, digest: str) -> None:
    if target.is_file():
        if _hash(target) != digest:
            raise CadError(
                "cad_mesh_digest_collision",
                "two different geometry payloads resolved to one SHA-256 path",
                {"temporary": str(temporary), "target": str(target)},
            )
        temporary.unlink()
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    os.replace(temporary, target)


def materialize_native_triangles(
    raw_geometry: Mapping[str, Mapping],
    destinations: Mapping[str, str],
) -> dict[str, dict]:
    """Write raw native triangles and materialize STL after CAD release."""

    from ...geometry.native_tessellation import (
        build_native_tessellation,
        materialize_native_stl,
        native_stl_relative_path,
        native_triangle_relative_path,
        parse_native_tessellation,
        write_native_triangle_stream,
    )

    if not isinstance(raw_geometry, Mapping) or not isinstance(destinations, Mapping):
        raise CadError(
            "cad_mesh_export_failed",
            "geometry serialization requires component mappings",
            {
                "raw_geometry": type(raw_geometry).__name__,
                "destinations": type(destinations).__name__,
            },
        )
    if set(raw_geometry) != set(destinations):
        raise CadError(
            "cad_mesh_export_failed",
            "geometry serialization has a different occurrence set",
            {
                "raw_geometry": sorted(raw_geometry),
                "destinations": sorted(destinations),
            },
        )
    serialized = {}
    for component in sorted(raw_geometry):
        (
            triangle_values,
            body_inventory,
            face_inventory,
            body_counts,
            used_api,
            transform_raw,
        ) = _raw_geometry_record(component, raw_geometry[component])
        source_scope = raw_geometry[component].get("source_scope")
        triangles = len(triangle_values) // 9
        requested = Path(destinations[component])
        evidence_root = (
            requested.parent.parent
            if requested.parent.name == "geometry"
            else requested.parent
        )
        raw_directory = evidence_root / "raw" / "triangles"
        stl_directory = evidence_root / "geometry"
        raw_temporary = raw_directory / f".{requested.stem}.partial.triangles"
        stl_temporary = stl_directory / f".{requested.stem}.partial.stl"
        if raw_temporary == stl_temporary:
            raise CadError(
                "cad_mesh_export_failed",
                "native triangle and STL destinations must differ",
                {"component": component, "path": str(requested)},
            )
        try:
            write_native_triangle_stream(raw_temporary, triangle_values)
            tessellation = build_native_tessellation(
                component,
                body_inventory,
                face_inventory,
                source_scope=source_scope,
            )
            spans = parse_native_tessellation(
                component,
                tessellation,
                body_counts,
                triangles,
                source_scope=source_scope,
                warnings=[],
            )
            materialize_native_stl(raw_temporary, stl_temporary, spans)
            raw_sha = _hash(raw_temporary)
            stl_sha = _hash(stl_temporary)
            raw_relative = native_triangle_relative_path(raw_sha)
            stl_relative = native_stl_relative_path(stl_sha)
            raw_path = evidence_root / raw_relative
            stl_path = evidence_root / stl_relative
            _publish_canonical_artifact(raw_temporary, raw_path, raw_sha)
            _publish_canonical_artifact(stl_temporary, stl_path, stl_sha)
        except CadError:
            raise
        except Exception as error:  # noqa: BLE001 - preserve the failing component
            raise CadError(
                "cad_mesh_serialization_failed",
                "native triangle serialization failed after CAD release",
                {
                    "component": component,
                    "raw_path": str(raw_temporary),
                    "stl_path": str(stl_temporary),
                    "error": f"{type(error).__name__}: {error}"[:500],
                },
            ) from error
        serialized[component] = {
            "component": component,
            "raw_path": raw_relative,
            "raw_sha": raw_sha,
            "path": stl_relative,
            "sha": stl_sha,
            "triangles": triangles,
            "bodies": body_counts,
            "used_api": used_api,
            "tessellation": tessellation,
            "transform_raw": transform_raw,
        }
    return serialized
