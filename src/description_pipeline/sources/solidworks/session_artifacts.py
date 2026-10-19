"""Pure classification of SolidWorks session-owned temporary documents.

SolidWorks uses two distinct temporary-document classes during a read-only
session walk.  ``IC~~`` is a 3D Interconnect cache materialization, while
``VC~~`` represents an embedded virtual component created from geometry stored
inside its owning assembly.  Neither file is an upload source.  These helpers
only prove the exact marker and temporary-root shape; owner, occurrence,
configuration, and API provenance are bound independently by the sealed
carrier and the native reader.
"""

from __future__ import annotations

import ntpath
import posixpath
import re
import tempfile

_IC_MARKER = "ic~~"
_VC_MARKER = "vc~~"
_SESSION_ROOT = re.compile(r"swx[0-9]+\Z", re.IGNORECASE)
SESSION_DOCUMENT_API = "IComponent2.GetPathName/IComponent2.GetImportedPath/IComponent2.IsVirtual"


def _path_parts(value: object) -> tuple[str, object, str, list[str]] | None:
    text = str(value or "")
    if not text.strip():
        return None
    module = ntpath if (re.match(r"^[A-Za-z]:", text) or text.startswith("\\\\") or "\\" in text) else posixpath
    separator = "\\" if module is ntpath else "/"
    try:
        normalized = module.normpath(text)
    except (TypeError, ValueError):
        return None
    parts = normalized.split(separator)
    return normalized, module, separator, parts


def _marked_path(value: object, marker: str) -> tuple[str, object, str, list[str], int] | None:
    parsed = _path_parts(value)
    if parsed is None:
        return None
    normalized, module, separator, parts = parsed
    positions = [index for index, part in enumerate(parts) if part.casefold() == marker]
    session_markers = [
        index for index, part in enumerate(parts) if part.casefold() in {_IC_MARKER, _VC_MARKER}
    ]
    if len(positions) != 1 or len(session_markers) != 1:
        return None
    position = positions[0]
    if position == len(parts) - 1:
        return None
    return normalized, module, separator, parts, position


def _marked_root(value: object, marker: str) -> str | None:
    marked = _marked_path(value, marker)
    if marked is None:
        return None
    _normalized, _module, separator, parts, position = marked
    return separator.join(parts[:position]).rstrip("\\/")


def _under_system_temp(value: str, module: object) -> bool:
    try:
        candidate = module.normcase(module.normpath(str(value)))
        root = module.normcase(module.normpath(str(tempfile.gettempdir())))
    except (TypeError, ValueError):
        return False
    separator = "\\" if module is ntpath else "/"
    return candidate == root or candidate.startswith(root.rstrip("\\/") + separator)


def _owned_session_root(value: object, marker: str) -> str | None:
    root = _marked_root(value, marker)
    if root is None:
        return None
    parsed = _path_parts(root)
    if parsed is None:
        return None
    _normalized, module, _separator, parts = parsed
    if not _under_system_temp(root, module):
        return None
    if not parts or _SESSION_ROOT.fullmatch(parts[-1]) is None:
        return None
    return root


def import_materialization_path(value: object) -> bool:
    """True for a 3D Interconnect ``IC~~`` path component."""

    return _marked_path(value, _IC_MARKER) is not None


def session_materialization_path(value: object) -> bool:
    """True only for ``IC~~`` under the system temporary directory."""

    return _owned_session_root(value, _IC_MARKER) is not None


def session_temp_root(value: object) -> str | None:
    """The owned ``swxNNNN`` temporary root containing an ``IC~~`` document."""

    return _owned_session_root(value, _IC_MARKER)


def embedded_virtual_component_path(value: object) -> bool:
    """True only for a ``VC~~`` document in an owned session temporary root."""

    return _owned_session_root(value, _VC_MARKER) is not None


def embedded_virtual_component_marker(value: object) -> bool:
    """True for an exact ``VC~~`` path component, regardless of machine root."""

    return _marked_path(value, _VC_MARKER) is not None


def embedded_virtual_component_root(value: object) -> str | None:
    """The owned ``swxNNNN`` temporary root containing a ``VC~~`` document."""

    return _owned_session_root(value, _VC_MARKER)


def _session_document_under_root(value: object, root: object, marker: str) -> bool:
    """True when ``value`` carries ``marker`` exactly below ``root``.

    The recorded root must itself have the SolidWorks ``swxNNNN`` session shape.
    This variant is used to verify sealed evidence without depending on the
    verifier machine's temporary directory.
    """

    if not isinstance(root, str) or not root.strip():
        return False
    parsed_root = _path_parts(root)
    if parsed_root is None:
        return False
    normalized_root, module, _separator, parts = parsed_root
    if not module.isabs(normalized_root) or not parts or _SESSION_ROOT.fullmatch(parts[-1]) is None:
        return False
    marked = _marked_path(value, marker)
    if marked is None:
        return False
    normalized, value_module, _separator, _parts, _position = marked
    if value_module is not module or not module.isabs(normalized):
        return False
    try:
        relative = module.relpath(module.normcase(normalized), module.normcase(normalized_root))
    except (TypeError, ValueError):
        return False
    separator = "\\" if module is ntpath else "/"
    if relative == "." or relative == ".." or relative.startswith(".." + separator) or module.isabs(relative):
        return False
    parts = relative.split(separator)
    return bool(parts) and parts[0].casefold() == marker


def session_materialization_under_root(value: object, root: object) -> bool:
    """True when ``value`` is an ``IC~~`` document exactly below ``root``."""

    return _session_document_under_root(value, root, _IC_MARKER)


def embedded_virtual_component_under_root(value: object, root: object) -> bool:
    """True when ``value`` is a ``VC~~`` document exactly below ``root``."""

    return _session_document_under_root(value, root, _VC_MARKER)


def session_artifact_path(value: object) -> bool:
    """True for either independently recognized session-owned document class."""

    return session_materialization_path(value) or embedded_virtual_component_path(value)


def session_document_marker(value: object) -> bool:
    """True for exactly one ``IC~~`` or ``VC~~`` path component."""

    return _marked_path(value, _IC_MARKER) is not None or _marked_path(value, _VC_MARKER) is not None


def has_session_document_marker(value: object) -> bool:
    """True when any ``IC~~`` or ``VC~~`` path component is present."""

    parsed = _path_parts(value)
    if parsed is None:
        return False
    _normalized, _module, _separator, parts = parsed
    return any(part.casefold() in {_IC_MARKER, _VC_MARKER} for part in parts)
