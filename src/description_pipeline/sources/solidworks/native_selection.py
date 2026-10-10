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


def _hash_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with open(_filesystem_path(path), "rb") as stream:
        for block in iter(lambda: stream.read(_READ_CHUNK), b""):
            hasher.update(block)
    return hasher.hexdigest()


def _stage_root(stage) -> str | None:
    if isinstance(stage, (str, os.PathLike)):
        return os.fspath(stage) or None
    if isinstance(stage, Mapping):
        return None
    for attribute in ("root", "stage_root", "path"):
        value = getattr(stage, attribute, None)
        if isinstance(value, (str, os.PathLike)) and os.fspath(value):
            return os.fspath(value)
    return None


def _stage_digest_probe(stage):
    """Callables/mappings the stage offers as the exact staged inventory."""

    if isinstance(stage, Mapping):
        return stage, None
    inventory = None
    for attribute in ("files", "inventory", "staged"):
        value = getattr(stage, attribute, None)
        if isinstance(value, Mapping):
            inventory = value
            break
    probe = None
    for attribute in ("sha256", "staged_sha256"):
        value = getattr(stage, attribute, None)
        if callable(value):
            probe = value
            break
    return inventory, probe


def _inventory_digest(inventory: Mapping, name: str) -> str | None:
    if name in inventory:
        value = inventory[name]
    else:
        folded = name.casefold()
        matches = [key for key in inventory if isinstance(key, str) and key.casefold() == folded]
        if len(matches) != 1:
            return None
        value = inventory[matches[0]]
    if not isinstance(value, str) or _SHA.fullmatch(value) is None:
        raise SelectionError("selection_stage_invalid", "Staged inventory entries must be SHA-256 digests", path=name)
    return value


def _staged_digest(stage, name: str) -> str | None:
    """The staged digest of ``name``, or None when the bytes are not staged.

    The staged bytes are authoritative: the stage root must be a readable
    directory on this host and the file's digest is hashed here.  Inventory
    mappings or ``sha256(name)`` probes the stage also offers are cross-checked
    only - they can never stand in for bytes that do not exist.
    """

    root = _stage_root(stage)
    if root is None:
        raise SelectionError("selection_stage_invalid", "The stage must expose its root directory", path=name)
    local_root = Path(root)
    if not local_root.is_dir():
        raise SelectionError(
            "selection_stage_invalid", "The stage root is not a readable directory", root=root, path=name
        )
    try:
        path = confined(local_root, name, exists=False)
    except PipelineError as error:
        raise SelectionError("selection_stage_invalid", str(error), path=name) from error
    if not path.is_file():
        return None
    actual = _hash_file(path)
    inventory, probe = _stage_digest_probe(stage)
    if inventory is not None:
        value = _inventory_digest(inventory, name)
        if value is not None and value != actual:
            raise SelectionError("selection_source_changed", "Stage inventory disagrees with the bytes", path=name)
    if probe is not None:
        try:
            value = probe(name)
        except Exception as error:  # noqa: BLE001 - a broken probe refuses, never guesses
            raise SelectionError("selection_stage_invalid", "The stage probe failed", path=name) from error
        if value is not None:
            if not isinstance(value, str) or _SHA.fullmatch(value) is None:
                raise SelectionError("selection_stage_invalid", "Staged entries must be SHA-256 digests", path=name)
            if value != actual:
                raise SelectionError("selection_source_changed", "Stage probe disagrees with the bytes", path=name)
    return actual


def _document_path(root: str | None, name: str) -> str:
    if root is None:
        return name
    module = _module_for(root)
    return module.join(module.normpath(root), *name.split("/"))


def _canonical_requested(value, files: Mapping[str, str]) -> str:
    text = os.fspath(value) if isinstance(value, os.PathLike) else value
    if not isinstance(text, str) or not text.strip():
        raise SelectionError("selection_request_invalid", "Requested documents must be names", path=repr(value))
    name = text.replace("\\", "/").lstrip("/")
    if name.startswith("./"):
        name = name[2:]
    if name in files:
        return name
    folded = name.casefold()
    matches = [candidate for candidate in files if candidate.casefold() == folded]
    if len(matches) == 1:
        return matches[0]
    raise SelectionError(
        "selection_request_unknown", "The requested document is not authorized by the upload index", path=name
    )


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
            raise SelectionError(
                "selection_reader_closed", "Observe calls must run inside the reader context"
            )
        if threading.get_ident() != self._thread:
            raise SelectionError(
                "selection_reader_thread", "Observe calls must stay on the reader's owning thread"
            )

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
            name = _canonical_requested(value, files)
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
        for name, digest in asked.items():
            if digest != files[name]:
                raise SelectionError(
                    "selection_source_changed", "Pending identity differs from the upload index", path=name
                )
            if posixpath.splitext(name)[1].lower() not in _NATIVE_SUFFIXES:
                raise SelectionError(
                    "selection_request_unsupported", "Only native SolidWorks documents can be observed", path=name
                )
            actual = _staged_digest(stage, name)
            if actual is None:
                raise SelectionError("selection_document_not_staged", "The pending document is not staged", path=name)
            if actual != files[name]:
                raise SelectionError("selection_source_changed", "Staged bytes differ from the upload index", path=name)
            document_paths[name] = _document_path(root, name)

        if root is None and asked:
            raise SelectionError(
                "selection_stage_invalid",
                "Observing documents requires the stage namespace root",
                paths=sorted(asked),
            )

        observations: dict[str, dict] = {}
        problems: list[dict] = []
        for name in asked:
            references: set[str] = set()
            for entry in self._read(document_paths[name]) or ():
                outcome = _classify_reference(entry, name, document_paths[name], root, files)
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
                stage_root=root,
                problems=problems,
                count=len(problems),
            )
        return dict(sorted(observations.items()))
