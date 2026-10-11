"""Assembly-rooted file selection, independent of transport and the CAD runtime.

The upload index is metadata, not permission to read every indexed file. Only
the main assembly and references observed from already selected documents may
be requested. Native adapters supply those observations; this module neither
searches the filesystem nor guesses a stored reference's relocated path.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping

from ...io import PipelineError, artifact_path_parts, digest

SELECTION_SCHEMA = "solidworks-to-urdf.selection/v1"
SELECTION_SCOPE = "native_staged_folder_closure"
MAX_SELECTION_FILES = 4096
_SHA = re.compile(r"[0-9a-f]{64}\Z")


class SelectionError(PipelineError):
    """A source-selection refusal with stable, retained diagnostic fields."""

    def __init__(self, code: str, message: str, **detail):
        super().__init__(message)
        self.code = code
        self.detail = detail


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


def _within_stage(stage_root: str, resolved_path: str, canonical: str) -> bool:
    """True when the engine-resolved path is exactly the staged canonical target."""

    root = str(stage_root).replace("\\", "/").rstrip("/")
    value = str(resolved_path).replace("\\", "/").rstrip("/")
    if (
        not root
        or len(value) <= len(root)
        or value[len(root)] != "/"
        or value[: len(root)].casefold() != root.casefold()
    ):
        return False
    return value[len(root) + 1 :].casefold() == canonical.casefold()


def validate_file_index(files: Mapping[str, str]) -> dict[str, str]:
    """Validate metadata without reading or authorizing any candidate file."""

    if not isinstance(files, Mapping) or not 0 < len(files) <= MAX_SELECTION_FILES:
        raise SelectionError("selection_inventory_invalid", "Selection inventory is empty or exceeds its limit")
    result: dict[str, str] = {}
    aliases: dict[str, str] = {}
    directories: dict[str, str] = {}
    for name, checksum in files.items():
        name = _path(name)
        if not isinstance(checksum, str) or _SHA.fullmatch(checksum) is None:
            raise SelectionError("selection_digest_invalid", "File identity requires a SHA-256 digest", path=name)
        folded = name.casefold()
        if folded in aliases:
            raise SelectionError(
                "selection_path_collision", "Selection paths differ only by case", paths=[aliases[folded], name]
            )
        aliases[folded] = name
        parts = name.split("/")
        for index in range(1, len(parts)):
            parent = "/".join(parts[:index])
            prior = directories.setdefault(parent.casefold(), parent)
            if prior != parent:
                raise SelectionError(
                    "selection_path_collision", "Selection directories differ only by case", paths=[prior, parent]
                )
        result[name] = checksum
    for folded, name in aliases.items():
        parts = folded.split("/")
        if any("/".join(parts[:index]) in aliases for index in range(1, len(parts))):
            raise SelectionError("selection_path_collision", "A selection file is also a parent directory", path=name)
    return dict(sorted(result.items()))


def verify_selection(
    manifest: dict, upload_files: Mapping[str, str], selected_files: Mapping[str, str], *, reader_sha256: str
) -> dict:
    """Recompute graph membership and require exact transferred file identities."""

    if not isinstance(manifest, dict):
        raise SelectionError("selection_manifest_invalid", "Selection manifest must be an object")
    if manifest.get("scope") != SELECTION_SCOPE:
        raise SelectionError("selection_manifest_invalid", "Selection manifest uses another scope")
    expected = seal_closure_selection(
        upload_files,
        manifest.get("main_assembly"),
        manifest.get("closure") or {},
        reader_sha256=reader_sha256,
    )
    if validate_file_index(selected_files) != expected["files"]:
        raise SelectionError("selection_inventory_changed", "Transferred files differ from the assembly selection")
    if manifest != expected:
        raise SelectionError("selection_manifest_changed", "Selection manifest differs from its recorded graph")
    return expected


def reachable_closure(graph: Mapping[str, dict], main_assembly: str) -> set[str]:
    """Canonical upload-index names reachable from the main over resolved graph edges."""

    reachable: set[str] = set()
    pending = [_path(main_assembly)]
    while pending:
        name = pending.pop()
        if name in reachable:
            continue
        reachable.add(name)
        record = graph.get(name)
        if not isinstance(record, Mapping):
            continue
        for reference in record.get("references") or ():
            if isinstance(reference, str) and reference not in reachable:
                pending.append(reference)
    return reachable


def seal_closure_selection(
    upload_files: Mapping[str, str],
    main_assembly: str,
    closure: Mapping[str, object],
    *,
    reader_sha256: str,
) -> dict:
    """Seal one native closure evidence block into the shared selection manifest.

    The graph is the reachability proof: keys and references are canonical upload-index
    names whose hashes equal the index, the selected set is everything reachable from the
    main over resolved edges, and every other index entry is reported as excluded.  Stored
    author references and further native evidence stay verbatim under ``closure``.
    """

    if not isinstance(reader_sha256, str) or _SHA.fullmatch(reader_sha256) is None:
        raise SelectionError("selection_reader_invalid", "Selection requires a pinned native reader identity")
    files = validate_file_index(upload_files)
    main = _path(main_assembly)
    if not main.lower().endswith(".sldasm") or main not in files:
        raise SelectionError("selection_main_invalid", "Choose an indexed SolidWorks assembly", path=main)
    if not isinstance(closure, Mapping):
        raise SelectionError("selection_closure_invalid", "Closure evidence must be an object")
    evidence = dict(closure)
    declared_main = evidence.get("main_assembly")
    if declared_main is not None and str(declared_main).replace("\\", "/") != main:
        raise SelectionError(
            "selection_closure_invalid", "Closure evidence names another main assembly", path=str(declared_main)
        )
    graph = evidence.get("graph")
    if not isinstance(graph, Mapping) or not graph:
        raise SelectionError("selection_closure_invalid", "Closure evidence carries no resolved graph")
    stage_root = evidence.get("stage_root")
    if not isinstance(stage_root, str) or not stage_root.strip():
        raise SelectionError("selection_closure_invalid", "Closure evidence must name its stage root")
    suppressed = evidence.get("suppressed", [])
    if not isinstance(suppressed, list):
        raise SelectionError("selection_closure_invalid", "Suppressed placements must be a verbatim evidence list")
    for item in suppressed:
        if (
            not isinstance(item, Mapping)
            or item.get("suppression_state") != 0
            or not isinstance(item.get("document"), str)
            or not item.get("document")
            or (item.get("instance") is not None and not isinstance(item.get("instance"), str))
            or not isinstance(item.get("resolved_path"), str)
            or not item.get("resolved_path").strip()
            or item.get("justification") != "authored_suppression_clean_open"
        ):
            raise SelectionError(
                "selection_closure_invalid",
                "A suppressed placement record is incomplete; only placements justified by a clean "
                "native open are omitted from the closure and recorded verbatim",
            )
    if evidence.get("upload_sha256") != digest(files):
        raise SelectionError("selection_closure_invalid", "Closure evidence carries another upload digest")
    if evidence.get("reader_sha256") != reader_sha256:
        raise SelectionError("selection_closure_invalid", "Closure evidence carries another reader identity")
    search_configuration = evidence.get("search_configuration")
    folder_type = search_configuration.get("folder_type") if isinstance(search_configuration, Mapping) else None
    if (
        not isinstance(search_configuration, Mapping)
        or type(folder_type) is not int
        or folder_type != 0
        or not isinstance(search_configuration.get("apis"), list)
        or "ISldWorks.GetSearchFolders" not in search_configuration["apis"]
        or "ISldWorks.SetSearchFolders" not in search_configuration["apis"]
        or not isinstance(search_configuration.get("before"), list)
        or search_configuration.get("pinned") != [stage_root]
        or search_configuration.get("observed_after_pin") != [stage_root]
        or search_configuration.get("restored") is not True
        or search_configuration.get("restore_error") is not None
    ):
        raise SelectionError(
            "selection_closure_invalid",
            "Closure evidence must pin and restore the native search configuration",
        )
    open_status = evidence.get("open_status")
    if (
        not isinstance(open_status, Mapping)
        or type(open_status.get("errors")) is not int
        or type(open_status.get("warnings")) is not int
        or open_status["errors"] < 0
        or open_status["warnings"] < 0
    ):
        raise SelectionError("selection_closure_invalid", "Closure evidence carries no usable open status")
    session = evidence.get("session")
    if not isinstance(session, Mapping) or session.get("sealed") is not True or session.get("closed") is not True:
        raise SelectionError("selection_closure_invalid", "Closure evidence must come from a sealed, closed session")
    checked: dict[str, dict] = {}
    for name, record in graph.items():
        name = _path(name)
        if name not in files:
            raise SelectionError(
                "selection_closure_invalid", "The resolved graph names a document outside the upload index", path=name
            )
        if not isinstance(record, Mapping) or record.get("sha256") != files[name]:
            raise SelectionError(
                "selection_closure_invalid", "A resolved graph entry differs from the staged upload index", path=name
            )
        references = record.get("references")
        if not isinstance(references, list):
            raise SelectionError(
                "selection_closure_invalid", "A resolved graph entry carries no reference list", path=name
            )
        edges: list[str] = []
        for reference in references:
            reference = _path(reference)
            if reference not in files:
                raise SelectionError(
                    "selection_closure_invalid",
                    "A resolved reference is not part of the upload index",
                    path=reference,
                )
            if reference in edges:
                raise SelectionError("selection_closure_invalid", "Repeated resolved reference edge", path=reference)
            edges.append(reference)
        checked[name] = {"sha256": files[name], "references": sorted(edges)}

    def canonical_target(value: object) -> str:
        normalized = str(value or "").replace("\\", "/")
        if not normalized.strip():
            raise SelectionError("selection_closure_invalid", "A resolved native edge carries no resolved name")
        if normalized in files:
            return normalized
        folded = {candidate.casefold(): candidate for candidate in files}
        match = folded.get(normalized.casefold())
        if match is None:
            raise SelectionError(
                "selection_closure_invalid",
                "A resolved native edge targets a file outside the upload index",
                path=normalized,
            )
        return match

    documents = evidence.get("documents")
    if not isinstance(documents, Mapping) or not documents:
        raise SelectionError("selection_closure_invalid", "Closure evidence carries no document evidence map")
    folded_graph = {name.casefold(): name for name in checked}
    evidence_map: dict[str, Mapping] = {}
    for name, entry in documents.items():
        canonical = folded_graph.get(str(name).replace("\\", "/").casefold())
        if canonical is None or not isinstance(entry, Mapping):
            raise SelectionError(
                "selection_closure_invalid", "Document evidence does not match the resolved graph", path=str(name)
            )
        if entry.get("staged_sha256") != files[canonical]:
            raise SelectionError(
                "selection_closure_invalid", "Document evidence does not match the staged bytes", path=canonical
            )
        evidence_map[canonical] = entry
    if set(evidence_map) != set(checked):
        raise SelectionError("selection_closure_invalid", "Document evidence keys differ from the resolved graph keys")
    imports = evidence.get("imports", [])
    if not isinstance(imports, list) or any(not isinstance(item, Mapping) for item in imports):
        raise SelectionError("selection_closure_invalid", "Imported-source evidence must be a verbatim evidence list")
    for item in imports:
        if (
            item.get("state") != "cached_materialization"
            or item.get("binding") != "author_metadata_only"
            or not isinstance(item.get("materialization"), str)
            or not item.get("materialization").strip()
        ):
            raise SelectionError(
                "selection_closure_invalid",
                "An imported occurrence must be recorded as an unbound cached materialization; "
                "author import metadata never binds a staged source",
            )
    warnings = evidence.get("warnings")
    if warnings is not None and (
        not isinstance(warnings, list) or any(not isinstance(item, Mapping) for item in warnings)
    ):
        raise SelectionError("selection_closure_invalid", "Closure warnings must be a verbatim evidence list")
    for name, entry in evidence_map.items():
        edges_raw = entry.get("resolved_edges", [])
        if not isinstance(edges_raw, list) or any(not isinstance(item, Mapping) for item in edges_raw):
            raise SelectionError(
                "selection_closure_invalid", "Resolved native edges must be a verbatim evidence list", path=name
            )
        targets: set[str] = set()
        for item in edges_raw:
            if item.get("kind") not in {"native_document", "native_imported_document"}:
                raise SelectionError(
                    "selection_closure_invalid", "A resolved native edge carries an unknown kind", path=name
                )
            if not isinstance(item.get("instance"), str) or not item["instance"].strip():
                raise SelectionError(
                    "selection_closure_invalid", "A resolved native edge carries no instance identity", path=name
                )
            state = item.get("suppression_state")
            if type(state) is not int or state not in {1, 2, 3, 4}:
                raise SelectionError(
                    "selection_closure_invalid",
                    "A resolved native edge carries an undocumented suppression state; "
                    "suppressed placements never contribute edges",
                    path=name,
                )
            lightweight = item.get("lightweight")
            if type(lightweight) is not bool or lightweight != (state in {1, 4}):
                raise SelectionError(
                    "selection_closure_invalid",
                    "A resolved native edge is not coherent about its lightweight state",
                    path=name,
                )
            target = canonical_target(item.get("resolved"))
            resolved_path = item.get("resolved_path")
            if not isinstance(resolved_path, str) or not _within_stage(stage_root, resolved_path, target):
                raise SelectionError(
                    "selection_closure_invalid",
                    "A resolved native edge does not point at the staged canonical target",
                    path=name,
                )
            targets.add(target)
        if set(checked[name]["references"]) != targets:
            raise SelectionError(
                "selection_closure_incoherent",
                "The resolved graph references must equal the resolved native edges",
                path=name,
            )
    reachable = reachable_closure(checked, main)
    missing = sorted(name for name in reachable if name not in checked)
    if missing:
        raise SelectionError(
            "selection_closure_incomplete", "A reachable document has no resolved observation", paths=missing
        )
    selected = {name: files[name] for name in sorted(reachable)}
    manifest = {
        "schema_version": SELECTION_SCHEMA,
        "scope": SELECTION_SCOPE,
        "reader_sha256": reader_sha256,
        "upload_sha256": digest(files),
        "main_assembly": main,
        "files": selected,
        "handoff_sha256": digest(selected),
        "closure": {**evidence, "main_assembly": main, "graph": checked},
        "excluded_files": {name: value for name, value in files.items() if name not in selected},
    }
    return {**manifest, "selection_sha256": digest(manifest)}
