"""Separate the three states people confuse: installed, running, collectable.

A worker can be installed and still be unreachable, be reachable while no
SolidWorks session exists, or have a session that cannot be collected from (no
licence, modal dialog, unsaved edits, wrong desktop session).  Each of those is
reported on its own with the evidence behind it.
"""

from __future__ import annotations

import ctypes
import os
import platform
import shutil
import sys
from contextlib import nullcontext
from typing import Any

from .errors import BridgeError, CadError


def _session_id() -> int | None:
    if os.name != "nt":
        return None
    try:
        windll = getattr(ctypes, "windll", None)
        if windll is None:
            return None
        pid = os.getpid()
        session = ctypes.c_ulong()
        windll.kernel32.ProcessIdToSessionId(pid, ctypes.byref(session))
        return int(session.value)
    except Exception:  # noqa: BLE001 - diagnostics must not fail the doctor run
        return None


def _active_console_session() -> int | None:
    if os.name != "nt":
        return None
    try:
        windll = getattr(ctypes, "windll", None)
        if windll is None:
            return None
        value = windll.kernel32.WTSGetActiveConsoleSessionId()
        return None if value in (0xFFFFFFFF, None) else int(value)
    except Exception:  # noqa: BLE001
        return None


def probe_host() -> dict[str, Any]:
    """Everything that does not need SolidWorks at all."""

    work_root = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    usage = shutil.disk_usage(work_root)
    session = _session_id()
    console = _active_console_session()
    return {
        "name": "host",
        "status": "passed",
        "platform": platform.platform(),
        "windows": os.name == "nt",
        "python": sys.version.split()[0],
        "python_arch": platform.machine(),
        "executable": sys.executable,
        "session_id": session,
        "active_console_session": console,
        "interactive_session": None if session is None or console is None else session == console,
        "disk_free_mb": int(usage.free / (1024 * 1024)),
    }


REQUIRED_MODULES = ("yaml", "jsonschema", "numpy")


def probe_dependencies() -> dict[str, Any]:
    """The worker needs the pipeline core and, on Windows, pywin32."""

    import importlib.util

    modules = list(REQUIRED_MODULES)
    if os.name == "nt":
        modules.append("win32com")
    missing = [name for name in modules if importlib.util.find_spec(name) is None]
    version = sys.version_info
    supported = (version.major, version.minor) >= (3, 12)
    return {
        "name": "dependencies",
        "status": "passed" if supported and not missing else "failed",
        "python": sys.version.split()[0],
        "python_supported": supported,
        "required": modules,
        "missing": missing,
    }


def probe_worker() -> dict[str, Any]:
    """Is *this* process alive and answering - not whether CAD works."""

    return {
        "name": "worker",
        "status": "passed",
        "pid": os.getpid(),
        "python": sys.version.split()[0],
    }


def probe_solidworks(backend: Any) -> dict[str, Any]:
    """Can we start an owned SolidWorks application and read documents?"""

    try:
        health = backend.health()
    except Exception as exc:
        return {
            "name": "solidworks",
            "status": "unavailable",
            "reason": exc.code if isinstance(exc, BridgeError) else "cad_api_error",
            "message": str(exc),
            "hint": "run the worker in the logged-in desktop session with a valid SolidWorks installation and licence",
        }
    documents = []
    try:
        documents = list(backend.list_documents())
    except Exception:
        documents = []
    license_type = None
    getter = getattr(backend, "license_type", None)
    if callable(getter):
        try:
            license_type = getter()
        except Exception:
            license_type = None
    return {
        "name": "solidworks",
        "status": "passed",
        "revision": health.get("sw_version"),
        "active_document": health.get("active_document"),
        "open_documents": len(documents),
        "license_type": license_type,
    }


def probe_collection(backend: Any, assembly: str, configuration: str | None = None) -> dict[str, Any]:
    """Read-only walk of the requested assembly: the only proof CAD is collectable."""

    try:
        opened = backend.open_document(assembly)
        if configuration:
            state_reader = getattr(backend, "document_state", None)
            state = state_reader(assembly) if callable(state_reader) else {}
            if state.get("active_configuration") != configuration:
                raise CadError(
                    "cad_configuration_not_active",
                    "SolidWorks did not open the requested configuration",
                    {"requested": configuration, "active": state.get("active_configuration")},
                )
        scene = backend.collect_scene(assembly, [], require_material=False)
    except Exception as exc:
        return {
            "name": "collection",
            "status": "failed",
            "reason": exc.code if isinstance(exc, BridgeError) else "cad_api_error",
            "message": str(exc),
            "detail": exc.detail if isinstance(exc, BridgeError) else {"type": type(exc).__name__},
        }
    configurations = []
    reader = getattr(backend, "list_configurations", None)
    if callable(reader):
        try:
            configurations = list(reader(assembly))
        except Exception:
            configurations = []
    result = {
        "name": "collection",
        "status": "passed",
        "assembly": opened.get("path") or assembly,
        "configuration": configuration,
        "configurations": configurations,
        "components": len(scene.components),
        "mass_properties": len(scene.mass_properties),
    }
    advisories = _save_flag_advisories(backend)
    if advisories:
        result["advisories"] = advisories
    return result


def _save_flag_advisories(backend: Any) -> list[dict[str, Any]]:
    """Documents SolidWorks would prompt to save, reported but never blocking.

    ``GetSaveFlag`` is SolidWorks' own answer about saving a document in the session
    the adapter owns - not evidence about the operator's edits (see
    ``SolidWorksBackend._record_save_flag``) - so it never changes collectability.  It
    is shown, because the one case where it may matter to the operator is an edit of
    their own that they have not saved yet.
    """

    reader = getattr(backend, "save_flag_documents", None)
    if not callable(reader):
        return []
    try:
        documents = [str(item) for item in (reader() or [])]
    except Exception:  # noqa: BLE001 - an advisory must never fail the diagnosis
        return []
    if not documents:
        return []
    return [
        {
            "code": "cad_save_flag_set",
            "message": (
                "SolidWorks reports unsaved changes for a document opened read-only in the session the "
                "adapter owns; the capture names the bytes on disk, so save in SolidWorks and capture "
                "again if you meant to include newer edits"
            ),
            "documents": documents[:20],
            "count": len(documents),
        }
    ]


def diagnose(backend: Any = None, assembly: str | None = None, configuration: str | None = None) -> dict[str, Any]:
    """Report installation, connectivity and collectability separately."""

    checks = [probe_host(), probe_worker(), probe_dependencies()]
    if backend is not None:
        checks.append(probe_solidworks(backend))
        if assembly and checks[-1]["status"] == "passed":
            checks.append(probe_collection(backend, assembly, configuration))
        elif assembly:
            checks.append(
                {
                    "name": "collection",
                    "status": "not_run",
                    "reason": "solidworks_unavailable",
                    "message": "collection was not attempted because SolidWorks could not be reached",
                }
            )
    by_name = {check["name"]: check for check in checks}
    installed = by_name["host"]["status"] == "passed" and by_name["dependencies"]["status"] == "passed"
    worker_alive = by_name["worker"]["status"] == "passed"
    solidworks = by_name.get("solidworks", {})
    collection = by_name.get("collection", {})
    return {
        "schema_version": "description-pipeline.solidworks-doctor/v1",
        "installed": installed,
        "worker_alive": worker_alive,
        "solidworks_reachable": solidworks.get("status") == "passed",
        "cad_collectable": collection.get("status") == "passed",
        "advisories": [note for check in checks for note in (check.get("advisories") or [])],
        "checks": checks,
    }


def diagnose_owned(backend: Any, assembly: str | None, configuration: str | None) -> dict[str, Any]:
    """All diagnostic entry points use the same per-operation CAD lifecycle."""
    session = getattr(backend, "session", None)
    with session() if callable(session) else nullcontext():
        prepare = getattr(backend, "prepare_source", None)
        if assembly and configuration and callable(prepare):
            prepare(assembly, configuration)
        return diagnose(backend, assembly, configuration)
