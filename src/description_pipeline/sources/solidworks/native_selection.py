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
  resolved document (and, for imports, the materialization/author-metadata
  readings) is recorded,
* admission is the native closure: every resolved document must lie under the
  staged root and be a staged upload-index file with matching sha256; any
  out-of-stage, unresolved or unbound-import edge refuses the run with the
  verbatim stored/resolved paths,
* the CU pre-verification (W6, the W5 probe-v4 check set) reads the main
  document's declared delivery identity (``dp.*``) in the same read-only pass
  and refuses when a required declaration is absent or the property set is
  unreadable: submitted values never replace CAD-authored declarations, and
  no definition exchange or native capture runs on such a package.  The
  sealed selection record carries the ``preverify`` block as its receipt.

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
from .native import session_materialization_path, session_temp_root
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

#: CU pre-verification (W6): the main document's declared delivery identity.
#: The list mirrors the W5 probe-v4 check set; admission refuses fail-closed
#: when a required declaration is absent (or the property set is unreadable),
#: so no native capture runs on a package that only carries submitted values.
REQUIRED_DECLARATIONS = (
    "dp.hardware_id",
    "dp.revision",
    "dp.owner",
    "dp.change_summary",
    "dp.control.system",
    "dp.control.reference",
    "dp.delivery_configuration",
)
OPTIONAL_DECLARATIONS = ("dp.robot_name", "dp.design_budget_record")


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
    open_status = readings.get("open")
    open_status = dict(open_status) if isinstance(open_status, Mapping) else {}
    open_errors = open_status.get("errors")
    if type(open_errors) is not int:
        raise SelectionError(
            "selection_open_status_unreadable",
            "the staged main open status is not an integer; a complete closure cannot be proven",
            stage_root=str(stage_root),
            problems=[
                _problem(
                    "selection_open_status_unreadable",
                    "the staged main open status is not an integer",
                    document=main,
                    open_status=open_status,
                )
            ],
            open_status=open_status,
        )
    if open_errors != 0:
        # A nonzero CAD open status means references were not all resolved as
        # saved; missing references can auto-suppress components (M3 original:
        # two formerly active HD(2) assemblies and their 34 children), so a
        # suppressed placement can never be assumed to be author intent here
        # and a complete closure cannot be claimed.
        raise SelectionError(
            "selection_open_errors",
            "the staged main opened with CAD-reported errors; missing references may "
            "have auto-suppressed occurrences, so a complete closure cannot be claimed",
            stage_root=str(stage_root),
            problems=[
                _problem(
                    "selection_open_errors",
                    "the staged main opened with CAD-reported errors",
                    document=main,
                    open_errors=open_errors,
                    open_warnings=open_status.get("warnings"),
                )
            ],
            open_status=open_status,
        )
    declarations = readings.get("identity_properties")
    declarations = dict(declarations) if isinstance(declarations, Mapping) else {}
    declarations_error = readings.get("identity_properties_error")
    missing_declarations = [
        key
        for key in REQUIRED_DECLARATIONS
        if not (isinstance(declarations.get(key), str) and declarations[key].strip())
    ]
    preverify = {
        "schema": "m303-probe-v4/selection",
        "open_errors": open_errors,
        "open_warnings": open_status.get("warnings"),
        "declarations": {key: declarations.get(key) for key in (*REQUIRED_DECLARATIONS, *OPTIONAL_DECLARATIONS)},
        "declarations_missing": missing_declarations,
        "declarations_error": declarations_error,
        "verdict": "attention" if (missing_declarations or declarations_error) else "pass",
    }
    if missing_declarations or declarations_error:
        problems.append(
            _problem(
                "selection_declarations_missing",
                "the staged main carries no complete declared delivery identity (dp.*); "
                "submitted values never replace CAD-authored declarations",
                document=main,
                missing=missing_declarations,
                declarations_error=declarations_error,
                declarations=preverify["declarations"],
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
                problems.append(
                    _problem(
                        "selection_suppressed_unresolved",
                        "a suppressed occurrence has no resolvable document even though the main opened cleanly",
                        document=defining_name or raw_defining,
                        instance=instance,
                    )
                )
                continue
            suppressed.append(
                {
                    "document": defining_name or raw_defining,
                    "owner_anchor": raw_anchor,
                    "instance": instance,
                    "name": record.get("name"),
                    "suppression_state": 0,
                    "resolved_path": resolved,
                    "justification": "authored_suppression_clean_open",
                }
            )
            continue
        virtual = record.get("is_virtual")
        imported = record.get("imported_path")
        imported = imported if isinstance(imported, str) and imported.strip() else None
        lightweight = state in (_COMPONENT_LIGHTWEIGHT, _COMPONENT_FULLY_LIGHTWEIGHT)
        cached_defining = bool(raw_defining) and cached_key(raw_defining) in cached_materializations
        structural = bool(resolved) and session_materialization_path(resolved)
        importish = cached_defining or structural or (not resolved and imported is not None)
        if importish:
            label = defining_name or raw_defining
            if resolved and (structural or cached_defining):
                # A materialized imported occurrence: geometry comes from the
                # session materialization; GetImportedPath is author-path
                # metadata, never an in-package binding.  Recorded as a warning
                # (no graph edge); the absence test decides cache provenance.
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
                        "owner_sha256": owner_sha256,
                        "bound_source": bound_source,
                        "defining_materialization": raw_defining if cached_defining else None,
                        "instance": instance,
                        "name": record.get("name"),
                        "author_imported_path": imported,
                        "author_metadata_in_stage": author_in_stage,
                        "is_virtual": virtual if type(virtual) is bool else None,
                        "materialization": resolved,
                        "session_temp_root": session_temp_root(resolved),
                        "configuration": record.get("configuration"),
                        "suppression_state": state,
                        "state": "cached_materialization",
                        "binding": "author_metadata_only",
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
                    document=label,
                    instance=instance,
                    resolved=resolved,
                    imported_path=imported,
                    note="a present external file is never downgraded to a cached import",
                )
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
    preverify["closure_ok"] = True
    return {
        "schema_version": CLOSURE_SCHEMA,
        "stage_root": str(stage_root),
        "main_assembly": main,
        "upload_sha256": digest(validate_file_index(files)),
        "reader_sha256": reader_sha256,
        "stored_references_phase": readings.get("stored_references_phase"),
        "search_configuration": readings.get("search") or {},
        "open_status": readings.get("open") or {},
        "saved_configuration": readings.get("saved_configuration"),
        "document_state": readings.get("document_state"),
        "preverify": preverify,
        "graph": graph_out,
        "documents": documents_out,
        "imports": sorted(imports, key=lambda item: (str(item.get("document")), str(item.get("instance")))),
        "warnings": sorted(
            warnings, key=lambda item: (str(item.get("code")), str(item.get("document")), str(item.get("instance")))
        ),
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
    ``cad_ownership_file`` pinpoints the default backend's CAD ownership
    ledger, so an endpoint-side reader shares the endpoint's single
    state-root ledger instead of the library default.  Admission is
    fail-closed; see the module docstring for the rules.
    """

    def __init__(self, *, walker=None, backend=None, reader_sha256=None, cad_ownership_file=None):
        if walker is not None and backend is not None:
            raise SelectionError("selection_reader_invalid", "Inject either a walker or a backend, not both")
        if cad_ownership_file is not None and (walker is not None or backend is not None):
            raise SelectionError(
                "selection_reader_invalid", "A CAD ownership file applies only to the default backend"
            )
        if reader_sha256 is not None and (
            not isinstance(reader_sha256, str) or _SHA.fullmatch(reader_sha256) is None
        ):
            raise SelectionError("selection_reader_invalid", "reader_sha256 must be a SHA-256 digest")
        if walker is None:
            if backend is None:
                from .native import SolidWorksBackend

                backend = SolidWorksBackend(
                    cad_ownership_file=None if cad_ownership_file is None else Path(cad_ownership_file)
                )
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
