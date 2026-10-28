"""Pure closure admission projected from the one native Discover observation.

The native reader owns pre-open references, exact main opening, occurrence
readings, search restoration and release. This module never starts CAD. It
maps those same primitives to the sealed selection using exact staged paths;
unknown bindings and unreadable required facts refuse rather than substitute.
"""
from __future__ import annotations

import hashlib
import ntpath
import os
import re
from collections.abc import Mapping
from pathlib import Path

from ...io import PipelineError, _filesystem_path, digest
from .handoff import MAX_HANDOFF_BYTES, _directory
from .session_artifacts import (
    SESSION_DOCUMENT_API,
    SESSION_SOURCE_IDENTITY_FIELDS,
    embedded_virtual_component_path,
    embedded_virtual_component_root,
    has_session_document_marker,
    session_artifact_path,
    session_materialization_under_root,
    session_materialization_path,
    session_root_pid,
    session_temp_root,
)
from .saved_state import normalize_open_status
from .selection import MAX_SELECTION_FILES, SelectionError, selection_path, validate_file_index

__all__ = ["MAX_SELECTION_FILES", "project_native_closure"]

CLOSURE_SCHEMA = "solidworks-to-urdf.native-closure/v1"

_READ_CHUNK = 1 << 20
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_COMPONENT_SUPPRESSED = 0
_COMPONENT_LIGHTWEIGHT = 1
_COMPONENT_FULLY_LIGHTWEIGHT = 4
_COMPONENT_INTERNAL_ID_MISMATCH = 5
_COMPONENT_STATES = (0, 1, 2, 3, 4)

#: CU pre-verification (W6): the main document's declared delivery identity.
#: Frozen identity contract: the property set must be readable - an unreadable
#: set is unknown, never missing, and refuses the run.  Per field: an absent
#: ``dp.hardware_id`` adopts the configured target downstream (marked
#: ``platform_configuration``); an absent ``dp.delivery_configuration`` keeps
#: the natively saved configuration and its source while a present value is
#: carried for the discovery-side saved/actual consistency check; absence of
#: revision, owner, control or change summary is a review-displayed gap that
#: never borrows a submitted value.  Only an unreadable set and an unusable
#: present configuration refuse here.
IDENTITY_DECLARATIONS = (
    "dp.hardware_id",
    "dp.revision",
    "dp.owner",
    "dp.change_summary",
    "dp.control.system",
    "dp.control.reference",
    "dp.delivery_configuration",
)
OPTIONAL_DECLARATIONS = ("dp.robot_name", "dp.design_budget_record")


def _staged_sha256(path: Path, name: str) -> str:
    """Hash staged bytes bounded by their recorded size and the handoff limit."""

    try:
        size = path.stat().st_size
    except OSError as error:
        raise SelectionError(
            "selection_source_changed", "Staged document could not be read", path=name, error=str(error)
        ) from error
    if size > MAX_HANDOFF_BYTES:
        raise SelectionError(
            "selection_document_too_large",
            "Staged document exceeds the handoff input limit",
            path=name,
            size=size,
            limit=MAX_HANDOFF_BYTES,
        )
    hasher = hashlib.sha256()
    read = 0
    try:
        with open(_filesystem_path(path), "rb") as stream:
            for block in iter(lambda: stream.read(_READ_CHUNK), b""):
                read += len(block)
                if read > size:
                    raise SelectionError("selection_source_changed", "Staged document changed while hashing", path=name)
                hasher.update(block)
    except OSError as error:
        raise SelectionError(
            "selection_source_changed", "Staged document could not be read", path=name, error=str(error)
        ) from error
    return hasher.hexdigest()


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
        or _SHA.fullmatch(owner_sha256) is None
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
        or _SHA.fullmatch(expected_sha256) is None
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


def _stage_root(stage) -> Path:
    """The real staged tree, validated by the handoff directory rules."""

    if not isinstance(stage, (str, os.PathLike)):
        raise SelectionError("selection_stage_invalid", "The stage must be a directory path", stage=repr(stage))
    value = os.fspath(stage)
    if not isinstance(value, str) or not value.strip():
        raise SelectionError("selection_stage_invalid", "The stage must be a directory path", stage=repr(stage))
    try:
        return _directory(Path(value))
    except PipelineError as error:
        raise SelectionError("selection_stage_invalid", str(error), stage=value) from error


def _index_name(files: Mapping[str, str], canonical: str) -> str | None:
    """The index spelling of an exactly staged relative path.

    ``validate_file_index`` already refuses two names differing only by case,
    so a case-insensitive match of the full staged-relative path is unique -
    this is deliberately narrow and never applies to a bare basename.
    """

    if canonical in files:
        return canonical
    folded = canonical.casefold()
    matches = [name for name in files if name.casefold() == folded]
    if len(matches) == 1:
        return matches[0]
    return None


def _under_stage(stage_root: Path, value) -> str | None:
    """Portable staged-relative path of an engine-reported path, or ``None``."""

    if not isinstance(value, str) or not value.strip():
        return None
    try:
        relative = Path(value).resolve().relative_to(stage_root)
    except (OSError, ValueError):
        return None
    return relative.as_posix()


def _indexed(stage_root: Path, value, files: Mapping[str, str]) -> str | None:
    """Upload-index name for an engine-reported path, by exact staged location."""

    relative = _under_stage(stage_root, value)
    if relative is None:
        return None
    return _index_name(files, relative)


def _problem(code: str, message: str, *, document=None, instance=None, stored_location=None, resolved=None, **extra):
    """One structured refusal item for the structural report (mandatory keys)."""

    item = {
        "code": code,
        "message": message,
        "document": document,
        "instance": instance,
        "stored_location": stored_location,
        "resolved": resolved,
    }
    for key, value in extra.items():
        if value is not None:
            item[key] = value
    return item


def _closure_payload(stage_root: Path, main: str, files: Mapping[str, str], readings: Mapping, reader_sha256):
    """Map raw closure readings onto the agreed evidence payload, fail-closed."""

    occurrences = readings.get("occurrences")
    if not isinstance(occurrences, list):
        raise SelectionError("selection_reader_result_invalid", "The closure walk returned no occurrence list")
    stored_references = readings.get("stored_references")
    stored_references = stored_references if isinstance(stored_references, Mapping) else {}
    graph: dict[str, dict] = {}
    documents: dict[str, dict] = {}
    imports: list[dict] = []
    virtual_components: list[dict] = []
    suppressed: list[dict] = []
    unresolved: list[dict] = []
    warnings: list[dict] = []
    problems: list[dict] = []
    hashes: dict[str, str] = {}
    cached_materializations: set[str] = set()

    def cached_key(value):
        text = str(value or "").strip()
        return os.path.normcase(os.path.abspath(text)) if text else None

    def staged_digest(name: str) -> str:
        if name not in hashes:
            hashes[name] = _staged_sha256(stage_root / name, name)
        return hashes[name]

    def graph_entry(name: str) -> dict:
        entry = graph.get(name)
        if entry is None:
            entry = graph[name] = {"sha256": files[name], "references": set()}
        return entry

    def document_entry(name: str) -> dict:
        entry = documents.get(name)
        if entry is None:
            entry = documents[name] = {
                "staged_sha256": None,
                "stored_references": stored_references.get(name, []),
                "resolved_edges": [],
            }
        return entry

    def staged_ok(name: str) -> bool:
        actual = staged_digest(name)
        if actual != files[name]:
            problems.append(
                _problem(
                    "selection_source_changed",
                    "staged bytes differ from the upload index",
                    document=name,
                    resolved=str(stage_root / name),
                )
            )
            return False
        return True

    graph_entry(main)
    document_entry(main)
    try:
        open_status = normalize_open_status(readings.get("open"))
    except ValueError as error:
        raise SelectionError(
            "selection_open_status_unreadable",
            "the staged main open status is not a usable integer pair",
            stage_root=str(stage_root),
            problems=[
                _problem(
                    "selection_open_status_unreadable",
                    "the staged main open status is not a usable integer pair",
                    document=main,
                    open_status=readings.get("open"),
                    error=str(error),
                )
            ],
            open_status=readings.get("open"),
        ) from error
    open_errors = open_status["errors"]
    open_warnings = open_status["warnings"]
    saved_state_incomplete = (open_status["errors"] != 0 or open_status["warnings"] != 0)
    missing_reference_open = open_errors == 2
    if not open_status["continuable"]:
        raise SelectionError(
            "selection_open_errors",
            "the staged main opened with unsupported CAD error bits; only the known "
            "missing-reference error can continue from readable saved state",
            stage_root=str(stage_root),
            problems=[
                _problem(
                    "selection_open_errors",
                    "the staged main opened with CAD-reported errors outside the continuable policy",
                    document=main,
                    open_errors=open_errors,
                    open_warnings=open_warnings,
                    open_error_bits=open_status["error_bits"],
                )
            ],
            open_status=open_status,
        )
    declarations = readings.get("identity_properties")
    declarations = dict(declarations) if isinstance(declarations, Mapping) else {}
    declarations_error = readings.get("identity_properties_error")
    session_evidence = readings.get("session")
    native_api_pid = session_evidence.get("native_api_pid") if isinstance(session_evidence, Mapping) else None
    if type(native_api_pid) is not int or native_api_pid <= 0:
        native_api_pid = None
    missing_declarations = [
        key
        for key in IDENTITY_DECLARATIONS
        if not (isinstance(declarations.get(key), str) and declarations[key].strip())
    ]
    configuration_value = declarations.get("dp.delivery_configuration")
    configuration_declared = configuration_value.strip() if isinstance(configuration_value, str) else None
    configuration_invalid = configuration_value is not None and not configuration_declared
    preverify = {
        "schema": "m303-probe-v4/selection",
        "open_errors": open_errors,
        "open_warnings": open_warnings,
        "open_classification": open_status["classification"],
        "declarations": {key: declarations.get(key) for key in (*IDENTITY_DECLARATIONS, *OPTIONAL_DECLARATIONS)},
        "declarations_missing": missing_declarations,
        "declarations_error": declarations_error,
        "delivery_configuration": {
            "declared": configuration_declared,
            "source": "cad" if configuration_declared else "native_saved_configuration",
            "verbatim": configuration_value,
        },
        "verdict": (
            "attention"
            if (saved_state_incomplete or declarations_error or configuration_invalid)
            else "pass"
        ),
    }
    if declarations_error:
        problems.append(
            _problem(
                "selection_identity_unreadable",
                "the staged main's declared delivery identity (dp.*) is unreadable; an unread "
                "identity is unknown, never missing, and submitted values never replace it",
                document=main,
                declarations_error=declarations_error,
                declarations=preverify["declarations"],
            )
        )
    if configuration_invalid:
        problems.append(
            _problem(
                "selection_configuration_invalid",
                "the declared delivery configuration is present but not usable text; the natively "
                "saved configuration is never replaced by a default or a submitted value",
                document=main,
                configuration=configuration_value,
            )
        )
    for record in occurrences:
        if not isinstance(record, Mapping):
            problems.append(_problem("selection_reader_result_invalid", "an occurrence record is not a mapping"))
            continue
        instance = record.get("instance") if isinstance(record.get("instance"), str) else record.get("name")
        defining = record.get("defining")
        raw_defining = defining if isinstance(defining, str) and defining else None
        defining_name = raw_defining if raw_defining in files else None
        anchor = record.get("defining_anchor")
        raw_anchor = anchor if isinstance(anchor, str) and anchor else None
        resolved = record.get("resolved_path")
        resolved = resolved if isinstance(resolved, str) else ""
        virtual = record.get("is_virtual")
        imported_reading = record.get("imported_path")
        imported = imported_reading if isinstance(imported_reading, str) and imported_reading.strip() else None
        invalid_import_reading = imported_reading is not None and not isinstance(imported_reading, str)
        state = record.get("suppression_state")
        if type(state) is not int or state not in _COMPONENT_STATES:
            if state == _COMPONENT_INTERNAL_ID_MISMATCH:
                code = "cad_component_internal_id_mismatch"
                message = "component is bound to a document with a different internal ID"
            elif type(state) is not int:
                code = "cad_component_state_unreadable"
                message = "component suppression state could not be read"
            else:
                code = "cad_component_state_unsupported"
                message = "component suppression state is not a documented value"
            problems.append(
                _problem(
                    code,
                    message,
                    document=defining_name or raw_defining,
                    instance=instance,
                    resolved=resolved or None,
                    suppression_state=state if type(state) is int else None,
                    error=record.get("state_error"),
                )
            )
            continue
        if state == _COMPONENT_SUPPRESSED:
            if not resolved:
                if missing_reference_open and not record.get("path_error") and virtual is False:
                    unresolved.append(
                        {
                            "instance": instance,
                            "document": defining_name or raw_defining,
                            "configuration": record.get("configuration"),
                            "suppression_state": state,
                            "resolved_path": "",
                            "path_error": record.get("path_error"),
                            "state": "missing_reference",
                        }
                    )
                    continue
                problems.append(
                    _problem(
                        "selection_suppressed_unresolved",
                        "a suppressed occurrence has no resolvable document and the open "
                        "status does not prove a continuable missing reference",
                        document=defining_name or raw_defining,
                        instance=instance,
                    )
                )
                continue
            if virtual is not False:
                problems.append(
                    _problem(
                        "selection_virtual_state_unproven",
                        "a suppressed delivered occurrence requires an explicit False IsVirtual reading",
                        document=defining_name or raw_defining,
                        instance=instance,
                        resolved=resolved or None,
                        is_virtual=virtual if type(virtual) is bool else None,
                    )
                )
                continue
            if has_session_document_marker(resolved) and not session_materialization_path(resolved):
                problems.append(
                    _problem(
                        "selection_suppressed_session_document_unproven",
                        "a suppressed occurrence cannot carry a VC~~ or mixed session document",
                        document=defining_name or raw_defining,
                        instance=instance,
                        resolved=resolved or None,
                    )
                )
                continue
        if state == _COMPONENT_SUPPRESSED:
            suppressed.append(
                {
                    "document": defining_name or raw_defining,
                    "owner_anchor": raw_anchor,
                    "instance": instance,
                    "name": record.get("name"),
                    "suppression_state": 0,
                    "resolved_path": resolved,
                    "justification": (
                        "suppression_with_incomplete_saved_state"
                        if saved_state_incomplete
                        else "authored_suppression_clean_open"
                    ),
                }
            )
            continue
        lightweight = state in (_COMPONENT_LIGHTWEIGHT, _COMPONENT_FULLY_LIGHTWEIGHT)
        cached_defining = bool(raw_defining) and cached_key(raw_defining) in cached_materializations
        structural = bool(resolved) and session_materialization_path(resolved)
        virtual_document = bool(resolved) and embedded_virtual_component_path(resolved)
        importish = (
            cached_defining
            or structural
            or virtual_document
            or virtual is True
            or (not resolved and imported is not None)
        )
        if importish:
            label = defining_name or raw_defining
            if virtual is True:
                conflict = structural or imported is not None
                if conflict:
                    problems.append(
                        _problem(
                            "selection_virtual_component_import_conflict",
                            "a virtual component is also represented as a 3D Interconnect import; "
                            "the two native geometry classes cannot be conflated",
                            document=label,
                            instance=instance,
                            resolved=resolved or None,
                            imported_path=imported,
                        )
                    )
                    continue
                owner = record.get("native_owner")
                owner = owner if isinstance(owner, str) and owner in files else None
                if owner is None and raw_defining in files:
                    owner = raw_defining
                if owner is None:
                    problems.append(
                        _problem(
                            "selection_virtual_component_unproven",
                            "an embedded virtual component names no ultimate owner in the staged inventory",
                            document=raw_defining,
                            instance=instance,
                            resolved=resolved or None,
                            owner=record.get("native_owner"),
                        )
                    )
                    continue
                if raw_defining in files and owner.casefold() != raw_defining.casefold():
                    problems.append(
                        _problem(
                            "selection_virtual_component_unproven",
                            "the virtual component owner differs from its defining staged document",
                            document=raw_defining,
                            instance=instance,
                            resolved=resolved or None,
                            owner=owner,
                        )
                    )
                    continue
                session_root = embedded_virtual_component_root(resolved)
                configuration = record.get("configuration")
                if (
                    not virtual_document
                    or session_root is None
                    or native_api_pid is None
                    or session_root_pid(session_root) != native_api_pid
                    or not isinstance(instance, str)
                    or not instance.strip()
                    or not isinstance(configuration, str)
                    or not configuration.strip()
                    or record.get("import_error") is not None
                    or invalid_import_reading
                ):
                    problems.append(
                        _problem(
                            "selection_virtual_component_unproven",
                            "IsVirtual=True requires an exact VC~~ document under the current "
                            "controlled session temp root plus an occurrence and configuration",
                            document=label,
                            instance=instance,
                            resolved=resolved or None,
                            session_temp_root=session_root,
                            native_api_pid=native_api_pid,
                            configuration=configuration,
                            imported_path_error=record.get("import_error"),
                            imported_path_reading=imported_reading,
                        )
                    )
                    continue
                if raw_anchor is not None and not instance.startswith(raw_anchor + "/"):
                    problems.append(
                        _problem(
                            "selection_virtual_component_unproven",
                            "the virtual component defining anchor does not corroborate its occurrence",
                            document=label,
                            instance=instance,
                            owner_anchor=raw_anchor,
                        )
                    )
                    continue
                graph_entry(owner)
                document_entry(owner)
                virtual_components.append(
                    {
                        "document": label,
                        "owner": owner,
                        "owner_anchor": raw_anchor,
                        "defining_anchor": raw_anchor,
                        "owner_sha256": files[owner],
                        "instance": instance,
                        "name": record.get("name"),
                        "session_document_path": resolved,
                        "session_temp_root": session_root,
                        "native_api_pid": native_api_pid,
                        "configuration": configuration,
                        "suppression_state": state,
                        "is_virtual": True,
                        "state": "embedded_virtual_component",
                        "binding": "owner_embedded_geometry",
                        "kind": "native_embedded_virtual_component",
                        "author_imported_path": None,
                        "author_imported_error": None,
                        "used_api": SESSION_DOCUMENT_API,
                    }
                )
                warnings.append(
                    {
                        "code": "embedded_virtual_component_geometry",
                        "message": (
                            "an embedded virtual component supplies geometry from the owner "
                            "assembly's in-memory definition; its VC~~ session document is not "
                            "an upload source"
                        ),
                        "document": label,
                        "owner": owner,
                        "owner_anchor": raw_anchor,
                        "owner_sha256": files[owner],
                        "instance": instance,
                        "session_document_path": resolved,
                        "session_temp_root": session_root,
                        "requires_owner_binding": True,
                        "used_api": SESSION_DOCUMENT_API,
                    }
                )
                continue
            if virtual_document:
                problems.append(
                    _problem(
                        "selection_virtual_component_unproven",
                        "a VC~~ document requires an explicit True IsVirtual reading",
                        document=label,
                        instance=instance,
                        resolved=resolved,
                        is_virtual=virtual if type(virtual) is bool else None,
                    )
                )
                continue
            if structural and virtual is not False:
                problems.append(
                    _problem(
                        "selection_cached_materialization_unproven",
                        "an IC~~ materialization requires an explicit False IsVirtual reading",
                        document=label,
                        instance=instance,
                        resolved=resolved,
                        is_virtual=virtual if type(virtual) is bool else None,
                    )
                )
                continue
            if resolved and (structural or cached_defining):
                # A materialized imported occurrence: geometry comes from the
                # session materialization; GetImportedPath is author-path
                # metadata, never an in-package binding.  Recorded as a warning
                # (no graph edge); the absence test decides cache provenance.
                cached_root = session_temp_root(resolved)
                if native_api_pid is None or session_root_pid(cached_root) != native_api_pid:
                    problems.append(
                        _problem(
                            "selection_cached_materialization_unproven",
                            "an IC~~ materialization is not owned by the current native session",
                            document=label,
                            instance=instance,
                            resolved=resolved,
                            session_temp_root=cached_root,
                            native_api_pid=native_api_pid,
                        )
                    )
                    continue
                cached_materializations.add(cached_key(resolved))
                author_in_stage = _indexed(stage_root, imported, files) is not None if imported else None
                owner = record.get("native_owner")
                owner = owner if isinstance(owner, str) and owner in files else (defining_name or raw_defining)
                owner_sha256 = files.get(owner) if isinstance(owner, str) and owner in files else None
                # An author import path that resolves to a staged index file is
                # the actual delivered source: retain it as a bound graph edge
                # so the sealed selection keeps it in the frozen folder (the
                # collector needs it and pruning would otherwise lose it).
                bound_source = None
                if imported:
                    candidate = _indexed(stage_root, imported, files)
                    if candidate is not None and staged_ok(candidate):
                        bound_source = candidate
                if bound_source is not None:
                    # The bound edge hangs off the validated ultimate owner (a
                    # resolved-graph key); a nested descendant's raw defining is
                    # a temp materialization and is never used as a graph key.
                    # Without a validated ultimate owner nothing is mutated:
                    # the run refuses with the structured problem protocol.
                    bound_owner = owner if isinstance(owner, str) and owner in files else None
                    if bound_owner is None:
                        problems.append(
                            _problem(
                                "selection_closure_invalid",
                                "a cached materialization retains an in-stage bound source "
                                "without a validated ultimate owner",
                                document=label,
                                instance=instance,
                                resolved=bound_source,
                                **({"owner": owner} if isinstance(owner, str) and owner else {}),
                            )
                        )
                    else:
                        graph_entry(bound_owner)["references"].add(bound_source)
                        graph_entry(bound_source)
                        document_entry(bound_source)
                        document_entry(bound_owner)["resolved_edges"].append(
                            {
                                "name": record.get("name"),
                                "instance": instance,
                                "kind": "native_imported_document",
                                "resolved": bound_source,
                                "resolved_path": imported,
                                "configuration": record.get("configuration"),
                                "suppression_state": state,
                                "lightweight": lightweight,
                                "evidence": "in-stage author import source retained",
                            }
                        )
                cached_identity_sha256 = None
                if isinstance(owner, str) and owner in files and isinstance(owner_sha256, str):
                    try:
                        cached_identity_sha256 = session_source_identity_sha256(
                            _session_identity(
                                owner=owner,
                                owner_sha256=owner_sha256,
                                occurrence=instance,
                                defining_anchor=raw_anchor,
                                configuration=record.get("configuration"),
                                native_files=files,
                            )
                        )
                    except (ValueError, SelectionError):
                        cached_identity_sha256 = None
                imports.append(
                    {
                        # Verbatim raw evidence: the defining label (a session
                        # materialization path for nested import descendants).
                        # Graph membership is proven separately by `owner`
                        # (ultimate delivered owner) + owner_sha256, which the
                        # seal resolves and corroborates; the raw label is never
                        # used as a graph key.
                        "document": label,
                        "owner": owner,
                        "owner_anchor": raw_anchor,
                        "defining_anchor": raw_anchor,
                        "owner_sha256": owner_sha256,
                        "bound_source": bound_source,
                        "defining_materialization": raw_defining if cached_defining else None,
                        "instance": instance,
                        "name": record.get("name"),
                        "author_imported_path": imported,
                        "author_imported_error": record.get("import_error"),
                        "author_imported_relative": bound_source,
                        "author_imported_exists": record.get("import_path_exists"),
                        "author_metadata_in_stage": author_in_stage,
                        "is_virtual": virtual if type(virtual) is bool else None,
                        "materialization": resolved,
                        "session_temp_root": cached_root,
                        "native_api_pid": native_api_pid,
                        "occurrence": instance,
                        "configuration": record.get("configuration"),
                        "suppression_state": state,
                        "state": "cached_materialization",
                        "kind": "native_import_materialization",
                        "binding": "author_metadata_only",
                        "used_api": SESSION_DOCUMENT_API,
                        **(
                            {"session_source_sha256": cached_identity_sha256}
                            if cached_identity_sha256 is not None
                            else {}
                        ),
                    }
                )
                warnings.append(
                    {
                        "code": "import_cached_materialization",
                        "message": (
                            "an imported occurrence contributes geometry from a session materialization; "
                            "the author import path is metadata, not a proven in-package binding"
                        ),
                        "document": label,
                        "owner": owner,
                        "owner_anchor": raw_anchor,
                        "owner_sha256": owner_sha256,
                        "instance": instance,
                        "materialization": resolved,
                        "session_temp_root": session_temp_root(resolved),
                        "author_imported_path": imported,
                        "author_imported_error": record.get("import_error"),
                        "author_imported_relative": record.get("author_imported_relative"),
                        "author_imported_exists": record.get("import_path_exists"),
                        "author_metadata_in_stage": author_in_stage,
                        "needs_absence_test": True,
                    }
                )
                continue
            if not resolved:
                problems.append(
                    _problem(
                        "selection_import_unresolved",
                        "an imported occurrence has no materialization and no staged source",
                        document=label,
                        instance=instance,
                        imported_path=imported,
                        defining_materialization=raw_defining if cached_defining else None,
                    )
                )
                continue
            problems.append(
                _problem(
                    "selection_resolved_outside_stage",
                    "an out-of-stage resolved document is not a session materialization",
                    document=defining_name or raw_defining,
                    instance=instance,
                    resolved=resolved,
                    imported_path=imported,
                    note="a present external file is never downgraded to a cached import",
                )
            )
            continue
        if virtual is not False:
            problems.append(
                _problem(
                    "selection_virtual_state_unproven",
                    "a delivered occurrence requires an explicit False IsVirtual reading",
                    document=defining_name or raw_defining,
                    instance=instance,
                    resolved=resolved or None,
                    is_virtual=virtual if type(virtual) is bool else None,
                )
            )
            continue
        graph_owner = defining_name
        nested_anchor = None
        if graph_owner is None and raw_anchor and raw_defining and session_artifact_path(raw_defining):
            native_owner = record.get("native_owner")
            if (
                isinstance(native_owner, str)
                and native_owner in files
                and instance.startswith(raw_anchor + "/")
            ):
                graph_owner = native_owner
                nested_anchor = raw_anchor
        if graph_owner is None:
            problems.append(
                _problem(
                    "selection_resolved_outside_stage",
                    "an occurrence is contained by a document outside the staged inventory",
                    document=raw_defining,
                    instance=instance,
                    resolved=resolved or None,
                )
            )
            continue
        graph_entry(graph_owner)
        document_entry(graph_owner)
        if not resolved:
            problems.append(
                _problem(
                    "selection_dependency_unresolved",
                    "a non-suppressed occurrence has no resolved document",
                    document=defining_name,
                    instance=instance,
                )
            )
            continue
        name = _indexed(stage_root, resolved, files)
        if name is None:
            if _under_stage(stage_root, resolved) is None:
                code = "selection_resolved_outside_stage"
                message = "a reference resolves outside the staged root"
            else:
                code = "selection_resolved_not_in_inventory"
                message = "a reference resolves under the stage but is not a staged index file"
            problems.append(
                _problem(code, message, document=defining_name, instance=instance, resolved=resolved or None)
            )
            continue
        if not staged_ok(name):
            continue
        graph_entry(graph_owner)["references"].add(name)
        graph_entry(name)
        document_entry(name)
        document_entry(graph_owner)["resolved_edges"].append(
            {
                "name": record.get("name"),
                "instance": instance,
                "kind": "native_document",
                "resolved": name,
                "resolved_path": resolved,
                "configuration": record.get("configuration"),
                "suppression_state": state,
                "lightweight": lightweight,
                "owner_anchor": nested_anchor,
                "defining_session_document": raw_defining if nested_anchor else None,
            }
        )
    if problems:
        problems.sort(
            key=lambda item: (
                str(item.get("code")),
                str(item.get("document")),
                str(item.get("instance")),
                str(item.get("resolved")),
            )
        )
        primary = problems[0]
        raise SelectionError(
            primary["code"], primary["message"], stage_root=str(stage_root), problems=problems, count=len(problems)
        )
    graph_out: dict[str, dict] = {}
    documents_out: dict[str, dict] = {}
    for name in sorted(graph):
        actual = staged_digest(name)
        if actual != files[name]:
            raise SelectionError(
                "selection_source_changed",
                "staged bytes differ from the upload index",
                stage_root=str(stage_root),
                problems=[
                    _problem(
                        "selection_source_changed",
                        "staged bytes differ from the upload index",
                        document=name,
                        resolved=str(stage_root / name),
                    )
                ],
            )
        graph_out[name] = {"sha256": files[name], "references": sorted(graph[name]["references"])}
        entry = documents.get(name) or {"stored_references": stored_references.get(name, []), "resolved_edges": []}
        entry["staged_sha256"] = actual
        entry["resolved_edges"] = sorted(entry["resolved_edges"], key=lambda item: str(item.get("instance")))
        documents_out[name] = entry
    excluded = sorted(set(files) - set(graph))
    if saved_state_incomplete:
        occurrences_out = []
        for record in occurrences:
            if not isinstance(record, Mapping):
                continue
            occurrences_out.append(
                {
                    "instance": record.get("instance") or record.get("name"),
                    "defining": record.get("defining"),
                    "configuration": record.get("configuration"),
                    "suppression_state": record.get("suppression_state"),
                    "resolved_path": record.get("resolved_path"),
                }
            )
        occurrences_out.sort(
            key=lambda item: (
                str(item.get("instance")),
                str(item.get("resolved_path")),
                str(item.get("configuration")),
            )
        )
    preverify["closure_ok"] = True
    return {
        "schema_version": CLOSURE_SCHEMA,
        "stage_root": str(stage_root),
        "main_assembly": main,
        "upload_sha256": digest(validate_file_index(files)),
        "reader_sha256": reader_sha256,
        "stored_references_phase": readings.get("stored_references_phase"),
        "search_configuration": readings.get("search") or {},
        "open_status": open_status,
        **({"observed_occurrences": occurrences_out} if saved_state_incomplete else {}),
        "saved_configuration": readings.get("saved_configuration"),
        "document_state": readings.get("document_state"),
        "preverify": preverify,
        "graph": graph_out,
        "documents": documents_out,
        "imports": sorted(imports, key=lambda item: (str(item.get("document")), str(item.get("instance")))),
        "virtual_components": sorted(
            virtual_components, key=lambda item: (str(item.get("owner")), str(item.get("instance")))
        ),
        "warnings": sorted(
            warnings, key=lambda item: (str(item.get("code")), str(item.get("document")), str(item.get("instance")))
        ),
        "suppressed": sorted(
            suppressed, key=lambda item: (str(item.get("document")), str(item.get("instance")))
        ),
        "unresolved": sorted(
            unresolved,
            key=lambda item: (
                str(item.get("instance")),
                str(item.get("document")),
                str(item.get("resolved_path")),
            ),
        ),
        "excluded": excluded,
        "session": readings.get("session") or {},
    }


def project_native_closure(stage, main_assembly, upload_files, record, *, reader_sha256):
    """Admit the released observation without another native walk or open."""

    stage_root = _stage_root(stage)
    files = validate_file_index(upload_files)
    main = selection_path(main_assembly)
    if not main.lower().endswith(".sldasm") or main not in files:
        raise SelectionError("selection_main_invalid", "Choose an indexed SolidWorks assembly", path=main)
    if not isinstance(reader_sha256, str) or _SHA.fullmatch(reader_sha256) is None:
        raise SelectionError("selection_reader_invalid", "reader_sha256 must be a SHA-256 digest")
    if not isinstance(record, Mapping):
        raise SelectionError("selection_reader_result_invalid", "Native Discover returned no observation")
    components = record.get("components")
    unresolved = record.get("unresolved_components")
    if not isinstance(components, list) or not isinstance(unresolved, list):
        raise SelectionError("selection_reader_result_invalid", "Native occurrence observations are missing")
    occurrences = []
    for row in [*components, *unresolved]:
        reference = row.get("native_reference") if isinstance(row, Mapping) else None
        if not isinstance(reference, Mapping):
            raise SelectionError("selection_reader_result_invalid", "A native occurrence has no source reading")
        instance = row.get("instance_id") or row.get("name2") or row.get("instance")
        occurrences.append({
            **reference,
            "instance": instance,
            "name": instance,
            "configuration": row.get("configuration"),
            "suppression_state": row.get("suppression_state"),
        })
    properties = record.get("properties")
    if not isinstance(properties, Mapping) or not isinstance(properties.get("document"), Mapping):
        raise SelectionError("selection_identity_unreadable", "The native document property set is unreadable")
    readings = {
        "occurrences": occurrences,
        "open": record.get("open_status"),
        "identity_properties": properties["document"],
        "saved_configuration": record.get("saved_configuration"),
        "document_state": record.get("document_state"),
        "stored_references": record.get("stored_references"),
        "stored_references_phase": record.get("stored_references_phase"),
        "search": record.get("search"),
        "session": record.get("session"),
    }
    return _closure_payload(stage_root, main, files, readings, reader_sha256)
