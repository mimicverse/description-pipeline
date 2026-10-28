"""Pure classification of SolidWorks session-owned temporary documents.

SolidWorks uses two distinct temporary-document classes during a read-only
session walk.  ``IC~~`` is a 3D Interconnect cache materialization, while
``VC~~`` represents an embedded virtual component created from geometry stored
inside its owning assembly. Neither file is an upload source. These helpers
validate document class, stable owner identity and the current session envelope.
They do not open CAD or derive engineering values; the sealed carrier and native
reader supply the evidence.
"""

from __future__ import annotations

import hashlib
import ntpath
import posixpath
import re
import tempfile
from collections.abc import Mapping
from pathlib import Path

from ...io import digest

_IC_MARKER = "ic~~"
_VC_MARKER = "vc~~"
_SESSION_ROOT = re.compile(r"swx[0-9]+\Z", re.IGNORECASE)
_SESSION_ROOT_PID = re.compile(r"swx([0-9]+)\Z", re.IGNORECASE)
SESSION_DOCUMENT_API = "IComponent2.GetPathName/IComponent2.GetImportedPath/IComponent2.IsVirtual"
_SOURCE_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
SESSION_SOURCE_IDENTITY_FIELDS = frozenset(
    {
        "kind", "state", "owner", "owner_sha256", "occurrence", "defining_anchor",
        "configuration", "is_virtual", "used_api",
    }
)


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


def session_root_pid(value: object) -> int | None:
    """The positive process id encoded by an owned ``swxNNNN`` root."""

    if not isinstance(value, str) or not value.strip():
        return None
    parsed = _path_parts(value)
    if parsed is None:
        return None
    _normalized, _module, _separator, parts = parsed
    if not parts:
        return None
    match = _SESSION_ROOT_PID.fullmatch(parts[-1])
    if match is None:
        return None
    pid = int(match.group(1))
    return pid if pid > 0 else None


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


def _session_identity(
    *,
    owner: object,
    owner_sha256: object,
    occurrence: object,
    defining_anchor: object,
    configuration: object,
    native_files: Mapping[str, str] | None = None,
) -> dict:
    """Validate the stable portion shared by Discover and a later Capture session."""

    from .selection import selection_path

    if not isinstance(owner, str) or not owner.strip():
        raise ValueError("a cached materialization has no ultimate owner")
    canonical_owner = selection_path(owner.replace("\\", "/"))
    if native_files is not None:
        matches = [
            name
            for name in native_files
            if selection_path(name.replace("\\", "/")).casefold()
            == canonical_owner.casefold()
        ]
        if len(matches) != 1:
            raise ValueError("a cached materialization owner is not unique in the frozen inventory")
        canonical_owner = matches[0]
        if native_files[canonical_owner] != owner_sha256:
            raise ValueError("a cached materialization owner bytes differ from the frozen inventory")
    if (
        not isinstance(owner_sha256, str)
        or _SOURCE_SHA256.fullmatch(owner_sha256) is None
        or not isinstance(occurrence, str)
        or not occurrence.strip()
        or (defining_anchor is not None and (
            not isinstance(defining_anchor, str) or not defining_anchor.strip()
        ))
        or not isinstance(configuration, str)
        or not configuration.strip()
    ):
        raise ValueError("a cached materialization stable identity is incomplete")
    return {
        "kind": "native_import_materialization",
        "state": "cached_materialization",
        "owner": canonical_owner,
        "owner_sha256": owner_sha256,
        "occurrence": occurrence,
        "defining_anchor": defining_anchor,
        "configuration": configuration,
        "is_virtual": False,
        "used_api": SESSION_DOCUMENT_API,
    }


def session_source_identity(
    component: Mapping,
    *,
    native_files: Mapping[str, str] | None = None,
) -> dict:
    """Return one validated canonical cached-materialization identity."""

    provenance = component.get("source_provenance")
    if not isinstance(provenance, Mapping):
        raise ValueError("a cached materialization has no source provenance")
    if (
        provenance.get("state") != "cached_materialization"
        or provenance.get("kind") != "native_import_materialization"
        or provenance.get("is_virtual") is not False
        or provenance.get("used_api") != SESSION_DOCUMENT_API
    ):
        raise ValueError("the component is not an API-proven cached materialization")
    instance_id = component.get("instance_id")
    name2 = component.get("name2")
    if isinstance(instance_id, str) and isinstance(name2, str) and instance_id != name2:
        raise ValueError("a cached materialization carries conflicting occurrence identities")
    occurrence = provenance.get("occurrence")
    if (
        not isinstance(occurrence, str)
        or not occurrence.strip()
        or (
            isinstance(instance_id, str)
            and instance_id
            and instance_id != occurrence
        )
        or (
            isinstance(name2, str)
            and name2
            and name2 != occurrence
        )
    ):
        raise ValueError("a cached materialization occurrence is not complete")
    return _session_identity(
        owner=provenance.get("owner"),
        owner_sha256=provenance.get("owner_sha256"),
        occurrence=occurrence,
        defining_anchor=provenance.get("defining_anchor"),
        configuration=component.get("configuration"),
        native_files=native_files,
    )


def session_source_identity_sha256(identity: Mapping) -> str:
    """Hash one exact stable identity record."""

    if not isinstance(identity, Mapping) or set(identity) != SESSION_SOURCE_IDENTITY_FIELDS:
        raise ValueError("a cached materialization identity is malformed")
    return digest(dict(identity))


def sealed_session_source(
    component: Mapping,
    slot: Mapping,
    *,
    native_files: Mapping[str, str] | None = None,
) -> dict:
    """Bind one sealed cached slot to the component identity it claims."""

    identity = session_source_identity(component, native_files=native_files)
    slot_identity = _session_identity(
        owner=slot.get("owner"),
        owner_sha256=slot.get("owner_sha256"),
        occurrence=slot.get("occurrence", slot.get("instance")),
        defining_anchor=slot.get("owner_anchor"),
        configuration=slot.get("configuration"),
        native_files=native_files,
    )
    if (
        slot.get("state") != "cached_materialization"
        or slot.get("kind") != "native_import_materialization"
        or slot.get("binding") != "author_metadata_only"
    ):
        raise ValueError("a sealed cached slot has an invalid class")
    if slot.get("is_virtual") is not False or slot.get("used_api") != SESSION_DOCUMENT_API:
        raise ValueError("a sealed cached slot has invalid session API evidence")
    expected = session_source_identity_sha256(identity)
    if slot_identity != identity or slot.get("session_source_sha256") != expected:
        raise ValueError("a sealed cached slot differs from the component identity")
    return identity


def current_session_source_matches(
    provenance: Mapping,
    expected_sha256: str,
    session_pid: object,
    native_files: Mapping[str, str],
) -> bool:
    """Recompute a cached identity and its current owned-session envelope."""

    if not isinstance(provenance, Mapping) or not isinstance(native_files, Mapping):
        return False
    if (
        provenance.get("state") != "cached_materialization"
        or provenance.get("kind") != "native_import_materialization"
        or provenance.get("is_virtual") is not False
        or provenance.get("used_api") != SESSION_DOCUMENT_API
    ):
        return False
    try:
        identity = _session_identity(
            owner=provenance.get("owner"),
            owner_sha256=provenance.get("owner_sha256"),
            occurrence=provenance.get("occurrence"),
            defining_anchor=provenance.get("defining_anchor"),
            configuration=provenance.get("configuration"),
            native_files=native_files,
        )
    except ValueError:
        return False
    if (
        not isinstance(expected_sha256, str)
        or _SOURCE_SHA256.fullmatch(expected_sha256) is None
        or session_source_identity_sha256(identity) != expected_sha256
        or type(session_pid) is not int
        or session_pid <= 0
        or provenance.get("native_api_pid") != session_pid
    ):
        return False
    author_path = provenance.get("author_imported_path")
    author_error = provenance.get("author_imported_error")
    author_exists = provenance.get("author_imported_exists")
    relative = provenance.get("author_imported_relative")
    if (
        not {
            "author_imported_path", "author_imported_error", "author_imported_exists", "author_imported_relative"
        } <= provenance.keys()
        or (author_error is not None and (not isinstance(author_error, str) or not author_error.strip()))
        or (author_path is None and (author_exists is not None or relative is not None))
        or (author_path is not None and (
            not isinstance(author_path, str) or not author_path.strip()
            or not ntpath.isabs(author_path) or author_error is not None
            or type(author_exists) is not bool
        ))
    ):
        return False
    if author_path is not None and author_error is None:
        source_root = provenance.get("source_root")
        if provenance.get("author_imported_in_source_root") is True:
            if (
                author_exists is not True
                or not isinstance(relative, str)
                or not relative
                or not isinstance(source_root, str)
                or not source_root
                or relative not in native_files
                or ntpath.normcase(ntpath.normpath(author_path))
                != ntpath.normcase(ntpath.normpath(ntpath.join(source_root, relative)))
            ):
                return False
            try:
                if hashlib.sha256(
                    (Path(source_root) / relative).read_bytes()
                ).hexdigest() != native_files[relative]:
                    return False
            except OSError:
                return False
        elif author_exists is not False or relative is not None:
            return False
    path = provenance.get("session_document_path") or provenance.get("materialization_path")
    root = provenance.get("session_temp_root")
    return (
        isinstance(path, str)
        and isinstance(root, str)
        and (
            provenance.get("materialization_path") is None
            or provenance.get("materialization_path") == path
        )
        and session_materialization_under_root(path, root)
        and session_root_pid(root) == session_pid
    )
