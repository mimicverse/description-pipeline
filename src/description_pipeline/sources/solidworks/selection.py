"""Assembly-rooted file selection, independent of transport and the CAD runtime.

The upload index is metadata, not permission to read every indexed file. Only
the main assembly and references observed from already selected documents may
be requested. Native adapters supply those observations; this module neither
searches the filesystem nor guesses a stored reference's relocated path.
"""

from __future__ import annotations

import ntpath
import re
import unicodedata
from collections.abc import Mapping

from ...io import PipelineError, artifact_path_parts, digest
from .session_artifacts import (
    SESSION_DOCUMENT_API,
    embedded_virtual_component_under_root,
    has_session_document_marker,
    session_document_marker,
    session_materialization_under_root,
    session_root_pid,
)

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
    import_targets: dict[str, set[str]] = {}
    session_occurrences: set[tuple[str, str]] = set()
    session_documents_by_instance: dict[str, set[str]] = {}
    session_owner_by_instance: dict[str, str] = {}
    session_slot_parents: dict[str, tuple[str, str]] = {}
    for item in imports:
        state = item.get("state")
        document_label = str(item.get("document") or "")
        if state == "cached_materialization":
            # The raw document label of a cached materialization stays verbatim
            # evidence (a session temp path for nested import descendants); its
            # graph membership is resolved from the validated ultimate owner,
            # falling back to the document label for carriers that spell the
            # owner there (both must name a resolved-graph key below).
            candidate = str(item.get("owner") or item.get("document") or "")
            owner = folded_graph.get(candidate.replace("\\", "/").casefold())
        else:
            owner = folded_graph.get(document_label.replace("\\", "/").casefold())
        if owner is None or owner not in evidence_map:
            raise SelectionError(
                "selection_closure_invalid",
                "An imported source names a document outside the resolved graph",
                path=document_label or str(item.get("owner")),
            )
        if state == "bound":
            target = canonical_target(item.get("resolved"))
            recorded_sha = item.get("sha256")
            if recorded_sha is not None and recorded_sha != files[target]:
                raise SelectionError(
                    "selection_closure_invalid", "A bound import differs from the staged bytes", path=target
                )
            import_targets.setdefault(owner, set()).add(target)
        elif state == "cached_materialization":
            if item.get("binding") != "author_metadata_only":
                raise SelectionError(
                    "selection_closure_invalid",
                    "A cached materialization is author metadata only; it never binds a staged source",
                )
            materialization = item.get("materialization")
            if not isinstance(materialization, str) or not materialization.strip():
                raise SelectionError(
                    "selection_closure_invalid", "A cached materialization records no materialization path"
                )
            if not isinstance(item.get("instance"), str) or not item["instance"].strip():
                raise SelectionError(
                    "selection_closure_invalid", "A cached materialization records no occurrence identity"
                )
            owner_sha = item.get("owner_sha256")
            if not isinstance(owner_sha, str) or owner_sha != files[owner]:
                raise SelectionError(
                    "selection_closure_invalid", "A cached materialization owner differs from the staged bytes"
                )
            native_api_pid = item.get("native_api_pid")
            if (
                type(native_api_pid) is not int
                or native_api_pid <= 0
                or session_root_pid(item.get("session_temp_root")) != native_api_pid
                or not session_materialization_under_root(
                    item.get("materialization"), item.get("session_temp_root")
                )
            ):
                raise SelectionError(
                    "selection_closure_invalid",
                    "A cached materialization is not owned by its recorded native session",
                )
            anchor = item.get("owner_anchor")
            if anchor is not None and (
                not isinstance(anchor, str)
                or not anchor.strip()
                or not item["instance"].startswith(anchor + "/")
            ):
                raise SelectionError(
                    "selection_closure_invalid",
                    "A cached materialization anchor does not corroborate its occurrence",
                )
            key = (owner.casefold(), item["instance"])
            if key in session_occurrences:
                raise SelectionError(
                    "selection_closure_invalid",
                    "The same session document occurrence is recorded more than once",
                    instance=item["instance"],
                )
            session_occurrences.add(key)
            session_documents_by_instance.setdefault(item["instance"], set()).add(
                ntpath.normcase(str(item.get("materialization")))
            )
            if item["instance"] in session_owner_by_instance and session_owner_by_instance[
                item["instance"]
            ] != owner:
                raise SelectionError(
                    "selection_closure_invalid",
                    "One session occurrence names multiple owners",
                    instance=item["instance"],
                )
            session_owner_by_instance[item["instance"]] = owner
            if anchor is not None:
                session_slot_parents[item["instance"]] = (
                    anchor,
                    ntpath.normcase(document_label),
                )
            if len(session_documents_by_instance[item["instance"]]) != 1:
                raise SelectionError(
                    "selection_closure_invalid",
                    "One occurrence names multiple session documents",
                    instance=item["instance"],
                )
        else:
            raise SelectionError(
                "selection_closure_invalid",
                "An imported source is neither bound nor a recorded cached materialization",
            )
    virtual_components = evidence.get("virtual_components", [])
    if not isinstance(virtual_components, list) or any(
        not isinstance(item, Mapping) for item in virtual_components
    ):
        raise SelectionError(
            "selection_closure_invalid", "Embedded virtual components must be a verbatim evidence list"
        )
    virtual_owners: set[str] = set()
    for item in virtual_components:
        if (
            item.get("state") != "embedded_virtual_component"
            or item.get("binding") != "owner_embedded_geometry"
            or item.get("kind") != "native_embedded_virtual_component"
            or item.get("used_api") != SESSION_DOCUMENT_API
            or item.get("is_virtual") is not True
            or "author_imported_path" not in item
            or item["author_imported_path"] is not None
            or "author_imported_error" not in item
            or item["author_imported_error"] is not None
        ):
            raise SelectionError(
                "selection_closure_invalid",
                "An embedded virtual component must be owner-bound and cannot carry import metadata",
            )
        owner_label = str(item.get("owner") or "")
        owner = folded_graph.get(owner_label.replace("\\", "/").casefold())
        if owner is None or owner not in evidence_map:
            raise SelectionError(
                "selection_closure_invalid",
                "An embedded virtual component names an owner outside the resolved graph",
                path=owner_label,
            )
        document_label = item.get("document")
        staged_label = (
            folded_graph.get(document_label.replace("\\", "/").casefold())
            if isinstance(document_label, str) and document_label
            else None
        )
        if staged_label is not None and staged_label != owner:
            raise SelectionError(
                "selection_closure_invalid",
                "An embedded virtual component document label names another staged owner",
                path=str(document_label),
            )
        instance = item.get("instance")
        configuration = item.get("configuration")
        if (
            not isinstance(instance, str)
            or not instance.strip()
            or not isinstance(configuration, str)
            or not configuration.strip()
        ):
            raise SelectionError(
                "selection_closure_invalid",
                "An embedded virtual component must record its occurrence and configuration",
            )
        anchor = item.get("owner_anchor")
        if anchor is not None and (
            not isinstance(anchor, str) or not anchor.strip() or not instance.startswith(anchor + "/")
        ):
            raise SelectionError(
                "selection_closure_invalid",
                "An embedded virtual component anchor does not corroborate its occurrence",
            )
        owner_sha = item.get("owner_sha256")
        if not isinstance(owner_sha, str) or owner_sha != files[owner]:
            raise SelectionError(
                "selection_closure_invalid",
                "An embedded virtual component owner differs from the staged bytes",
            )
        session_document = item.get("session_document_path")
        session_root = item.get("session_temp_root")
        native_api_pid = item.get("native_api_pid")
        if (
            type(native_api_pid) is not int
            or native_api_pid <= 0
            or session_root_pid(session_root) != native_api_pid
            or not embedded_virtual_component_under_root(session_document, session_root)
        ):
            raise SelectionError(
                "selection_closure_invalid",
                "An embedded virtual component is not owned by its recorded native session",
                path=str(session_document),
            )
        key = (owner.casefold(), instance)
        if key in session_occurrences:
            raise SelectionError(
                "selection_closure_invalid",
                "The same session document occurrence is recorded more than once",
                instance=instance,
            )
        session_occurrences.add(key)
        session_documents_by_instance.setdefault(instance, set()).add(
            ntpath.normcase(str(session_document))
        )
        if instance in session_owner_by_instance and session_owner_by_instance[instance] != owner:
            raise SelectionError(
                "selection_closure_invalid",
                "One session occurrence names multiple owners",
                instance=instance,
            )
        session_owner_by_instance[instance] = owner
        if anchor is not None:
            session_slot_parents[instance] = (
                anchor,
                ntpath.normcase(str(document_label)),
            )
        if len(session_documents_by_instance[instance]) != 1:
            raise SelectionError(
                "selection_closure_invalid",
                "One occurrence names multiple session documents",
                instance=instance,
            )
        virtual_owners.add(owner)
    warnings = evidence.get("warnings")
    if warnings is not None and (
        not isinstance(warnings, list) or any(not isinstance(item, Mapping) for item in warnings)
    ):
        raise SelectionError("selection_closure_invalid", "Closure warnings must be a verbatim evidence list")
    native_edge_instances: dict[str, tuple[str, str | None]] = {}
    anchored_edges: dict[str, list[tuple[str, str, str]]] = {}
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
            anchor = item.get("owner_anchor")
            edge_identity = (name.casefold(), anchor)
            if item["instance"] in native_edge_instances:
                raise SelectionError(
                    "selection_closure_invalid",
                    "One native occurrence is recorded under multiple resolved edges",
                    instance=item["instance"],
                )
            native_edge_instances[item["instance"]] = edge_identity
            lightweight = item.get("lightweight")
            if type(lightweight) is not bool or lightweight != (state in {1, 4}):
                raise SelectionError(
                    "selection_closure_invalid",
                    "A resolved native edge is not coherent about its lightweight state",
                    path=name,
                )
            defining_session_document = item.get("defining_session_document")
            if anchor is None:
                if defining_session_document is not None:
                    raise SelectionError(
                        "selection_closure_invalid",
                        "A native edge carries a session document without an owner anchor",
                        path=name,
                    )
            elif not (
                item.get("kind") == "native_document"
                and isinstance(anchor, str)
                and anchor.strip()
                and item["instance"].startswith(anchor + "/")
                and isinstance(defining_session_document, str)
                and defining_session_document
                and (
                    session_document_marker(defining_session_document)
                    if has_session_document_marker(defining_session_document)
                    else True
                )
            ):
                raise SelectionError(
                    "selection_closure_invalid",
                    "A nested native edge anchor lacks its session document or occurrence prefix",
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
            if anchor is not None:
                anchored_edges.setdefault(anchor, []).append(
                    (name.casefold(), target.casefold(), defining_session_document)
                )
        if set(checked[name]["references"]) != targets | import_targets.get(name, set()):
            raise SelectionError(
                "selection_closure_incoherent",
                "The resolved graph references must equal the resolved native edges plus bound imported sources",
                path=name,
            )
    reachable_by_anchor: dict[str, set[str]] = {}
    anchor_roots = set(anchored_edges) | {
        anchor for anchor, _document in session_slot_parents.values()
    }
    for anchor in anchor_roots:
        edges = anchored_edges.get(anchor, [])
        parent_documents = session_documents_by_instance.get(anchor)
        parent_owner = session_owner_by_instance.get(anchor)
        if not parent_documents or parent_owner is None:
            raise SelectionError(
                "selection_closure_invalid",
                "A nested native edge names no admitted session parent occurrence",
                instance=anchor,
            )
        reachable = {parent_owner.casefold()}
        changed = True
        while changed:
            changed = False
            for owner, target, _defining in edges:
                if owner in reachable and target not in reachable:
                    reachable.add(target)
                    changed = True
        for owner, _target, defining in edges:
            if owner not in reachable:
                raise SelectionError(
                    "selection_closure_invalid",
                    "A nested native edge is not connected to its session parent",
                    instance=anchor,
                    path=owner,
                )
            if has_session_document_marker(defining):
                if ntpath.normcase(defining) not in parent_documents:
                    raise SelectionError(
                        "selection_closure_invalid",
                        "A nested native edge does not use its parent session document",
                        instance=anchor,
                    )
            elif folded_graph.get(defining.replace("\\", "/").casefold()) != owner:
                raise SelectionError(
                    "selection_closure_invalid",
                    "A nested native edge uses a defining document outside its resolved owner chain",
                    instance=anchor,
                    path=owner,
                )
        reachable_by_anchor[anchor] = reachable
    for instance, (anchor, parent_document) in session_slot_parents.items():
        owner = session_owner_by_instance.get(instance)
        if owner is None or owner.casefold() not in reachable_by_anchor.get(anchor, set()):
            raise SelectionError(
                "selection_closure_invalid",
                "A session slot anchor does not resolve through the admitted owner chain",
                instance=instance,
            )
        if has_session_document_marker(parent_document):
            if parent_document not in session_documents_by_instance.get(anchor, set()):
                raise SelectionError(
                    "selection_closure_invalid",
                    "A nested session slot does not name its admitted parent session document",
                    instance=instance,
                )
        elif folded_graph.get(parent_document.replace("\\", "/").casefold()) != owner:
            raise SelectionError(
                "selection_closure_invalid",
                "A nested session slot defining document does not match its ultimate owner",
                instance=instance,
            )
    reachable = reachable_closure(checked, main)
    missing = sorted(name for name in reachable if name not in checked)
    if missing:
        raise SelectionError(
            "selection_closure_incomplete", "A reachable document has no resolved observation", paths=missing
        )
    unreachable_virtual_owners = sorted(virtual_owners - reachable)
    if unreachable_virtual_owners:
        raise SelectionError(
            "selection_closure_incomplete",
            "An embedded virtual component owner is not reachable from the selected main",
            paths=unreachable_virtual_owners,
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
