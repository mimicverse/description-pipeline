"""Native observations for the staged selection graph, without opening documents.

The endpoint stages an admitted subset of the upload index and then asks this
adapter for the closed-document references of exactly the pending batch.  Every
query is the documented unopened, no-search form
``GetDocumentDependencies2(path, False, False, False)`` (see
:func:`native.closed_document_dependencies`) and it runs only on staged bytes
whose recorded identity matches the upload index.

What the adapter refuses before any geometry is opened:

* stored locations that cannot be proved inside the stage namespace
  (``outside_stage``; the stored name and location stay verbatim in the
  diagnostics, and a same-named decoy inside the stage is never substituted),
* 3D Interconnect / temp import artifacts (``import_unsupported``; a stored
  ``...\\IC~~\\X.step.SLDAPRT`` temp path is never reinterpreted as the
  delivered ``X.step``),
* in-namespace references whose suffix is not a native document
  (``unsupported_type``; an unsupported resource is never an empty leaf by
  suffix),
* in-namespace references the upload index does not authorize
  (``not_in_upload_index``),
* nonportable relative spellings (``nonportable``),
* staged bytes that do not match their recorded identity
  (``selection_source_changed``).

The stage namespace is content, not permission: only the pending names are
read, no other file is scanned, no search rule runs, and no basename fallback
exists.  This is a metadata gate, not an OS sandbox - once an open is
permitted, SolidWorks itself resolves references wherever its own search
leads, and the session provides no file-access isolation.  The companion
pre-open guard (:meth:`native.SolidWorksBackend.seal_documents`) is what
restricts which documents may be opened afterwards.
"""

from __future__ import annotations

import hashlib
import ntpath
import os
import posixpath
import re
import threading
from collections.abc import Mapping
from pathlib import Path

from ...io import PipelineError, _filesystem_path, artifact_path_parts, confined
from .handoff import MAX_HANDOFF_BYTES, _directory
from .selection import MAX_SELECTION_FILES, SelectionError, validate_file_index

__all__ = ["MAX_SELECTION_FILES", "NativeSelectionReader"]

_READ_CHUNK = 1 << 20
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_DRIVE = re.compile(r"^[A-Za-z]:")
_NATIVE_SUFFIXES = (".sldprt", ".sldasm")
_IMPORT_SUFFIXES = (".step", ".stp", ".iges", ".igs", ".x_t", ".x_b", ".sat", ".sab")
_DOUBLED_IMPORT_SUFFIX = re.compile(r"\.(step|stp|iges|igs|x_t|x_b|sat|sab)\.(sldprt|sldasm)$", re.IGNORECASE)


def _module_for(*values: str):
    """The path flavour of these values: ``ntpath`` for Windows spellings."""

    for value in values:
        text = str(value or "")
        if _DRIVE.match(text) or text.startswith("\\\\") or "\\" in text:
            return ntpath
    return posixpath


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


def _staged_digest(root: Path, name: str) -> str | None:
    """The staged digest of ``name``, or None when the bytes are not staged."""

    try:
        path = confined(root, name, exists=False)
    except PipelineError as error:
        raise SelectionError("selection_stage_invalid", str(error), path=name) from error
    if not path.is_file():
        return None
    return _staged_sha256(path, name)


def _document_path(root: Path, name: str) -> str:
    module = _module_for(str(root))
    return module.join(module.normpath(str(root)), *name.split("/"))


def _pending_name(value, files: Mapping[str, str]) -> str:
    """Exactly the canonical upload-index name; nothing is normalized or guessed."""

    if not isinstance(value, str) or value not in files:
        raise SelectionError(
            "selection_request_unknown",
            "The pending document is not exactly an upload index name",
            path=repr(value),
        )
    return value


def _import_artifact(name: str, location: str) -> bool:
    """True when the stored reference is a 3D Interconnect / temp import."""

    path_text = location.replace("\\", "/").casefold()
    parts = [part for part in path_text.split("/") if part]
    if any(part == "ic~~" for part in parts):
        return True
    if parts and _DOUBLED_IMPORT_SUFFIX.search(parts[-1]):
        return True
    return name.casefold().endswith(_IMPORT_SUFFIXES)


def _canonical_reference(module, stage_root: str, document_path: str, location: str) -> str | None:
    """Upload-relative name for a stored location, or None when it leaves the stage."""

    if module.isabs(location) or _DRIVE.match(location) or location.startswith("\\\\"):
        candidate = location
    else:
        from .native import resolve_dependency_path

        candidate = resolve_dependency_path(location, document_path)
    root = module.normpath(stage_root)
    target = module.normpath(candidate)
    if module is ntpath:
        key_root, key_target = root.casefold(), target.casefold()
    else:
        key_root, key_target = root, target
    prefix = key_root.rstrip("\\/") + ("\\" if module is ntpath else "/")
    if not key_target.startswith(prefix):
        return None
    relative = target[len(root) :].lstrip("\\/")
    return relative.replace("\\", "/")


def _index_name(files: Mapping[str, str], canonical: str) -> str | None:
    """The index spelling of an in-namespace reference.

    SolidWorks stores Windows paths with the case it saw, while the delivered
    name is authoritative.  ``validate_file_index`` already refuses two names
    that differ only by case, so a case-insensitive match is unique - this is
    deliberately narrow and applies to stored references only, never to
    pending names or to any basename.
    """

    if canonical in files:
        return canonical
    folded = canonical.casefold()
    matches = [name for name in files if name.casefold() == folded]
    if len(matches) == 1:
        return matches[0]
    return None


def _classify_reference(entry, document: str, document_path: str, stage_root: str, files: Mapping[str, str]):
    """Either the canonical reference name or one structured diagnostic."""

    if not isinstance(entry, Mapping):
        return {"document": document, "problem": "invalid_entry", "name": repr(entry), "location": None}
    name = entry.get("name")
    location = entry.get("location")
    evidence = {"document": document, "name": name, "location": location}
    if not isinstance(name, str) or not name.strip() or not isinstance(location, str) or not location.strip():
        return {**evidence, "problem": "invalid_entry"}
    if _import_artifact(name, location):
        return {**evidence, "problem": "import_unsupported"}
    module = _module_for(stage_root, document_path, location)
    canonical = _canonical_reference(module, stage_root, document_path, location)
    if canonical is None:
        return {**evidence, "problem": "outside_stage"}
    try:
        artifact_path_parts(canonical)
    except PipelineError:
        return {**evidence, "problem": "nonportable"}
    if posixpath.splitext(canonical)[1].lower() not in _NATIVE_SUFFIXES:
        return {**evidence, "problem": "unsupported_type"}
    resolved = _index_name(files, canonical)
    if resolved is None:
        return {**evidence, "problem": "not_in_upload_index"}
    return resolved


class NativeSelectionReader:
    """Owned serial reader over one endpoint CAD session.

    ``with NativeSelectionReader() as reader:`` acquires nothing until the
    first real query; the owned SolidWorks session is created on that first
    query and closed on exit (also when the body raised).  Inject
    ``application`` (a COM application object or a zero-argument callable that
    returns one) to use a caller-owned session or a fake backend in Linux
    tests; an injected application or session is never closed here.

    All ``observe`` calls must run serially on the thread that entered the
    context - there is no global session and no cross-thread COM assumption.
    """

    def __init__(self, *, application=None, session=None, max_documents: int = MAX_SELECTION_FILES):
        self._injected = application
        self._session = session
        self._owns_session = application is None and session is None
        self._thread: int | None = None
        self._closed = True
        self._cancelled = threading.Event()
        if not isinstance(max_documents, int) or isinstance(max_documents, bool) or max_documents <= 0:
            raise SelectionError("selection_reader_invalid", "max_documents must be a positive integer")
        self._max_documents = max_documents

    def __enter__(self):
        if self._thread is not None and not self._closed:
            raise SelectionError("selection_reader_busy", "The reader context is already active")
        self._thread = threading.get_ident()
        self._closed = False
        self._cancelled = threading.Event()
        return self

    def __exit__(self, *exc_info):
        self._cancelled.set()
        session = self._session if self._owns_session else None
        self._session = None
        self._closed = True
        if session is not None:
            session.close()
        return False

    def _check_open(self) -> None:
        if self._closed or self._thread is None:
            raise SelectionError("selection_reader_closed", "Observe calls must run inside the reader context")
        if threading.get_ident() != self._thread:
            raise SelectionError("selection_reader_thread", "Observe calls must stay on the reader's owning thread")

    def _application(self):
        if self._injected is not None:
            application = self._injected
            if callable(application) and not hasattr(application, "_oleobj_"):
                return application()
            return application
        session = self._session
        if session is None:
            from .isolation import CadSession  # imported only when the endpoint really owns CAD

            session = self._session = CadSession()
        if getattr(session, "app", None) is None:
            return session.connect(self._cancelled)
        return session.current_application()

    def _read(self, document_path: str):
        from .native import closed_document_dependencies  # pure module; COM stays in the app object

        return closed_document_dependencies(self._application(), document_path, max_documents=self._max_documents)

    def observe(self, stage, pending, upload_files) -> dict[str, dict]:
        """Closed-document references of exactly the pending staged documents.

        Returns observations ``{name: {"sha256": ..., "references": [...]}}``
        covering exactly the pending names, in the upload index spelling.
        Any refused edge raises :class:`SelectionError` with the verbatim
        stored name and location before any document is opened.
        """

        self._check_open()
        files = validate_file_index(upload_files)
        if not isinstance(pending, Mapping):
            raise SelectionError("selection_request_invalid", "Pending documents must be a name-to-digest mapping")
        asked: dict[str, str] = {}
        for value, digest in pending.items():
            name = _pending_name(value, files)
            if not isinstance(digest, str) or _SHA.fullmatch(digest) is None:
                raise SelectionError("selection_request_invalid", "Pending entries must be SHA-256 digests", path=name)
            if name in asked and asked[name] != digest:
                raise SelectionError(
                    "selection_request_invalid", "Pending entries repeat a document with different digests", path=name
                )
            asked[name] = digest
        if len(asked) > MAX_SELECTION_FILES:
            raise SelectionError("selection_request_invalid", "The pending batch exceeds the selection cap")

        root = _stage_root(stage)
        document_paths: dict[str, str] = {}
        staged: dict[str, str] = {}
        for name, digest in asked.items():
            if digest != files[name]:
                raise SelectionError(
                    "selection_source_changed", "Pending identity differs from the upload index", path=name
                )
            if posixpath.splitext(name)[1].lower() not in _NATIVE_SUFFIXES:
                raise SelectionError(
                    "selection_request_unsupported", "Only native SolidWorks documents can be observed", path=name
                )
            actual = _staged_digest(root, name)
            if actual is None:
                raise SelectionError("selection_document_not_staged", "The pending document is not staged", path=name)
            if actual != files[name]:
                raise SelectionError("selection_source_changed", "Staged bytes differ from the upload index", path=name)
            staged[name] = actual
            document_paths[name] = _document_path(root, name)

        observations: dict[str, dict] = {}
        problems: list[dict] = []
        for name in asked:
            entries = self._read(document_paths[name]) or ()
            # The bytes must still be the verified ones before any observation
            # is emitted; a document that changed while SolidWorks read it
            # cannot enter the sealed selection.
            if _staged_digest(root, name) != staged[name]:
                raise SelectionError(
                    "selection_source_changed", "Staged bytes changed during the native query", path=name
                )
            references: set[str] = set()
            for entry in entries:
                outcome = _classify_reference(entry, name, document_paths[name], str(root), files)
                if isinstance(outcome, str):
                    references.add(outcome)
                else:
                    problems.append(outcome)
            observations[name] = {"sha256": files[name], "references": sorted(references)}
        if problems:
            problems.sort(
                key=lambda item: (str(item.get("document")), str(item.get("name")), str(item.get("location")))
            )
            raise SelectionError(
                "selection_dependencies_refused",
                "Stored references cannot be admitted into the staged selection",
                stage_root=str(root),
                problems=problems,
                count=len(problems),
            )
        return dict(sorted(observations.items()))
