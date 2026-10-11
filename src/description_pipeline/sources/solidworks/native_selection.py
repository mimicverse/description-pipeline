"""Native closure admission for the provisionally staged upload inventory.

The whole uploaded folder is staged first (per-file hash-verified by the
endpoint); this reader then performs **one** closure walk over the staged
bytes in the backend's owned session:

* the session search scope is pinned to the staged root
  (``SolidWorksBackend.configure_staged_search`` /
  ``ISldWorks.SetSearchFolders``) before any document is opened,
* closed no-search ``GetDocumentDependencies2(path, False, False, False)``
  stored references are recorded verbatim **before any document is opened**
  (query A over every staged native document; once an assembly is open, a
  false-Searchflag query reports the in-session resolution, not the saved
  author references) and are evidence only, never admission,
* the sealed staged main is opened read-only and every occurrence's actual
  resolved document (and ``GetImportedPath`` binding for imports) is recorded,
* admission is the native closure: every resolved document must lie under the
  staged root and be a staged upload-index file with matching sha256; any
  out-of-stage, unresolved or unbound-import edge refuses the run with the
  verbatim stored/resolved paths.

No basename or suffix substitution exists anywhere in this module: engine
reported paths are mapped to upload-index names by their exact staged
relative location.  A suppressed placement (state 0) loads nothing: it is
recorded as evidence and omitted from the closure graph.  A lightweight
placement (state 1/4) stays graph-admissible and is flagged; measurement paths
refuse it elsewhere.  State 5 (internal-ID mismatch) and unreadable or
undocumented states refuse the run with the exact instance.

The return contract is documented in
``reviews/incident094339/native-closure-reader-contract-20261011.md`` (agreed
with worker B, 2026-10-11).
"""
from __future__ import annotations

import hashlib
import os
import re
import unicodedata
from collections.abc import Mapping
from pathlib import Path

from ...io import PipelineError, _filesystem_path, artifact_path_parts, digest
from .handoff import MAX_HANDOFF_BYTES, _directory
from .selection import MAX_SELECTION_FILES, SelectionError, validate_file_index

__all__ = ["MAX_SELECTION_FILES", "NativeSelectionReader"]

CLOSURE_SCHEMA = "solidworks-to-urdf.native-closure/v1"

_READ_CHUNK = 1 << 20
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_COMPONENT_SUPPRESSED = 0
_COMPONENT_LIGHTWEIGHT = 1
_COMPONENT_FULLY_LIGHTWEIGHT = 4
_COMPONENT_INTERNAL_ID_MISMATCH = 5
_COMPONENT_STATES = (0, 1, 2, 3, 4)


def _path(value: object) -> str:
    if not isinstance(value, str) or unicodedata.normalize("NFC", value) != value:
        raise SelectionError("selection_path_invalid", "Selection paths must be canonical text", path=value)
    try:
        parts = artifact_path_parts(value)
    except PipelineError as error:
        raise SelectionError("selection_path_invalid", str(error), path=value) from error
    if not parts or "/".join(parts) != value or any(part.startswith("~$") for part in parts):
        raise SelectionError("selection_path_invalid", "Selection paths must be portable file names", path=value)
    return value


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
    suppressed: list[dict] = []
    problems: list[dict] = []
    hashes: dict[str, str] = {}

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
    for record in occurrences:
        if not isinstance(record, Mapping):
            problems.append(_problem("selection_reader_result_invalid", "an occurrence record is not a mapping"))
            continue
        instance = record.get("instance") if isinstance(record.get("instance"), str) else record.get("name")
        defining = record.get("defining")
        raw_defining = defining if isinstance(defining, str) and defining else None
        defining_name = raw_defining if raw_defining in files else None
        resolved = record.get("resolved_path")
        resolved = resolved if isinstance(resolved, str) else ""
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
            suppressed.append(
                {
                    "document": defining_name or raw_defining,
                    "instance": instance,
                    "name": record.get("name"),
                    "suppression_state": 0,
                    "resolved_path": resolved or None,
                }
            )
            continue
        if defining_name is None:
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
        graph_entry(defining_name)
        document_entry(defining_name)
        virtual = record.get("is_virtual")
        imported = record.get("imported_path")
        imported = imported if isinstance(imported, str) and imported.strip() else None
        lightweight = state in (_COMPONENT_LIGHTWEIGHT, _COMPONENT_FULLY_LIGHTWEIGHT)
        if not resolved and imported is None and virtual is not True:
            problems.append(
                _problem(
                    "selection_dependency_unresolved",
                    "a non-suppressed occurrence has no resolved document",
                    document=defining_name,
                    instance=instance,
                )
            )
            continue
        if (not resolved) or virtual is True or "IC~~" in resolved:
            bound = _indexed(stage_root, imported, files) if imported else None
            if bound is not None and staged_ok(bound):
                graph_entry(defining_name)["references"].add(bound)
                graph_entry(bound)
                document_entry(bound)
                document_entry(defining_name)["resolved_edges"].append(
                    {
                        "name": record.get("name"),
                        "instance": instance,
                        "kind": "native_imported_document",
                        "resolved": bound,
                        "resolved_path": resolved or None,
                        "imported_path": imported,
                        "is_virtual": virtual if type(virtual) is bool else None,
                        "configuration": record.get("configuration"),
                        "suppression_state": state,
                        "lightweight": lightweight,
                    }
                )
                imports.append(
                    {
                        "document": defining_name,
                        "instance": instance,
                        "name": record.get("name"),
                        "imported_path": imported,
                        "is_virtual": virtual if type(virtual) is bool else None,
                        "resolved": bound,
                        "resolved_path": resolved or None,
                        "sha256": files[bound],
                        "state": "bound",
                    }
                )
                continue
            problems.append(
                _problem(
                    "selection_import_unsupported",
                    "an imported occurrence is not bound to a staged source file",
                    document=defining_name,
                    instance=instance,
                    resolved=resolved or None,
                    imported_path=imported,
                    temp_document=resolved if (resolved and ("IC~~" in resolved or virtual is True)) else None,
                    needed_evidence="engine-reported GetImportedPath inside the staged root with matching sha256",
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
        graph_entry(defining_name)["references"].add(name)
        graph_entry(name)
        document_entry(name)
        document_entry(defining_name)["resolved_edges"].append(
            {
                "name": record.get("name"),
                "instance": instance,
                "kind": "native_document",
                "resolved": name,
                "resolved_path": resolved,
                "configuration": record.get("configuration"),
                "suppression_state": state,
                "lightweight": lightweight,
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
    return {
        "schema_version": CLOSURE_SCHEMA,
        "stage_root": str(stage_root),
        "main_assembly": main,
        "upload_sha256": digest(validate_file_index(files)),
        "reader_sha256": reader_sha256,
        "stored_references_phase": readings.get("stored_references_phase"),
        "search_configuration": readings.get("search") or {},
        "open_status": readings.get("open") or {},
        "graph": graph_out,
        "documents": documents_out,
        "imports": sorted(imports, key=lambda item: (str(item.get("document")), str(item.get("instance")))),
        "suppressed": sorted(
            suppressed, key=lambda item: (str(item.get("document")), str(item.get("instance")))
        ),
        "excluded": excluded,
        "session": readings.get("session") or {},
        "sealed_count": len(readings.get("sealed") or []),
        "documents_before": readings.get("documents_before"),
        "documents_after": readings.get("documents_after"),
    }


class NativeSelectionReader:
    """One-shot closure reader over the provisionally staged upload inventory.

    ``observe(stage, main_assembly, upload_files)`` runs exactly one closure
    walk in the backend's owned session and returns the evidence payload; the
    session is closed before it returns.  Inject ``walker`` in tests (a
    callable ``(stage_root, main, files) -> readings``); inject ``backend`` to
    reuse a configured ``SolidWorksBackend`` (its staged search pin applies).
    Admission is fail-closed; see the module docstring for the rules.
    """

    def __init__(self, *, walker=None, backend=None, reader_sha256=None):
        if walker is not None and backend is not None:
            raise SelectionError("selection_reader_invalid", "Inject either a walker or a backend, not both")
        if reader_sha256 is not None and (
            not isinstance(reader_sha256, str) or _SHA.fullmatch(reader_sha256) is None
        ):
            raise SelectionError("selection_reader_invalid", "reader_sha256 must be a SHA-256 digest")
        if walker is None:
            if backend is None:
                from .native import SolidWorksBackend

                backend = SolidWorksBackend()
            walker = backend.closure_readings
        if not callable(walker):
            raise SelectionError("selection_reader_invalid", "The closure walker must be callable")
        self._walker = walker
        self._reader_sha256 = reader_sha256

    def observe(self, stage, main_assembly, upload_files):
        """One native closure walk; the evidence payload or a fail-closed refusal."""

        stage_root = _stage_root(stage)
        files = validate_file_index(upload_files)
        main = _path(main_assembly)
        if not main.lower().endswith(".sldasm") or main not in files:
            raise SelectionError("selection_main_invalid", "Choose an indexed SolidWorks assembly", path=main)
        readings = self._walker(stage_root, main, files)
        if not isinstance(readings, Mapping):
            raise SelectionError("selection_reader_result_invalid", "The closure walk returned no readings")
        return _closure_payload(stage_root, main, files, readings, self._reader_sha256)
