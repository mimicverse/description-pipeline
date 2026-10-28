"""Shared fully valid native-closure evidence for selection seal and transport tests.

One complete, contract-shaped closure payload (mandatory stage root, pinned and restored
search configuration, sealed and closed session, canonical graph and document evidence)
so endpoint, transport and verifier tests import a single source of truth.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from description_pipeline.io import digest
from description_pipeline.sources.solidworks.selection import seal_closure_selection

READER_SHA256 = "ab" * 32
STAGE_ROOT = "C:\\stage"
DEFAULT_MAIN = "cad/robot.SLDASM"


def closure_evidence(
    upload_files: Mapping[str, str],
    main_assembly: str = DEFAULT_MAIN,
    *,
    edges: Mapping[str, Sequence[str]] | None = None,
    reader_sha256: str = READER_SHA256,
    stage_root: str = STAGE_ROOT,
    suppressed: Sequence[Mapping[str, Any]] = (),
    imports: Sequence[Mapping[str, Any]] = (),
    virtual_components: Sequence[Mapping[str, Any]] = (),
    search_configuration: Mapping[str, Any] | None = None,
    open_status: Mapping[str, Any] | None = None,
    session: Mapping[str, Any] | None = None,
) -> dict:
    """One fully valid native closure payload for the locked selection contract."""

    files = dict(upload_files)
    if edges is None:
        others = sorted(name for name in files if name != main_assembly)
        edges = {main_assembly: others, **{name: [] for name in others}}
    separator = "\\" if "\\" in stage_root else "/"
    graph = {name: {"sha256": files[name], "references": list(refs)} for name, refs in edges.items()}
    documents = {
        name: {
            "staged_sha256": files[name],
            "stored_references": [],
            "resolved_edges": [
                {
                    "name": reference,
                    "instance": reference,
                    "kind": "native_document",
                    "resolved": reference.replace("/", separator),
                    "resolved_path": stage_root.rstrip("/\\") + separator + reference.replace("/", separator),
                    "configuration": "Default",
                    "suppression_state": 2,
                    "lightweight": False,
                }
                for reference in refs
            ],
        }
        for name, refs in edges.items()
    }
    return {
        "schema_version": "solidworks-to-urdf.native-closure/v1",
        "stage_root": stage_root,
        "main_assembly": main_assembly,
        "upload_sha256": digest(files),
        "reader_sha256": reader_sha256,
        "search_configuration": dict(
            search_configuration
            or {
                "apis": ["ISldWorks.GetSearchFolders", "ISldWorks.SetSearchFolders"],
                "folder_type": 0,
                "before": [],
                "pinned": [stage_root],
                "observed_after_pin": [stage_root],
                "set_return": "True",
                "restored": True,
                "restore_error": None,
            }
        ),
        "open_status": dict(open_status or {"errors": 0, "warnings": 0}),
        "graph": graph,
        "documents": documents,
        "suppressed": [dict(item) for item in suppressed],
        "imports": [dict(item) for item in imports],
        "virtual_components": [dict(item) for item in virtual_components],
        "session": dict(session or {"sealed": True, "closed": True}),
    }


def sealed_selection(
    upload_files: Mapping[str, str],
    main_assembly: str = DEFAULT_MAIN,
    **kwargs: Any,
) -> dict:
    """Seal the shared closure evidence exactly as the transport seals it."""

    reader_sha256 = str(kwargs.get("reader_sha256") or READER_SHA256)
    kwargs["reader_sha256"] = reader_sha256
    evidence = closure_evidence(upload_files, main_assembly, **kwargs)
    return seal_closure_selection(upload_files, main_assembly, evidence, reader_sha256=reader_sha256)


def observation_from_closure_fixture(record: dict, closure: dict) -> dict:
    """Give one test backend the same primitive carrier as native Discover."""

    import copy

    record = copy.deepcopy(record)
    rows = []
    edges = [
        (owner, edge)
        for owner, document in closure["documents"].items()
        for edge in document.get("resolved_edges", [])
    ]
    for row in record["components"]:
        match = next(((owner, edge) for owner, edge in edges if edge["resolved"] == row["document"]), None)
        if match is None:
            continue
        owner, edge = match
        row["native_reference"] = {
            "defining": owner,
            "native_owner": owner,
            "defining_anchor": None,
            "resolved_path": edge["resolved_path"],
            "is_virtual": False,
        }
        rows.append(row)
    for slot in closure.get("virtual_components", []):
        rows.append({
            "instance_id": slot["instance"],
            "name2": slot["instance"],
            "document": slot["session_document_path"],
            "configuration": slot["configuration"],
            "suppression_state": slot["suppression_state"],
            "native_reference": {
                "defining": slot["document"],
                "native_owner": slot["owner"],
                "defining_anchor": slot["owner_anchor"],
                "resolved_path": slot["session_document_path"],
                "is_virtual": True,
                "imported_path": None,
            },
        })
    record["components"] = rows
    record["unresolved_components"] = []
    record["open_status"] = closure["open_status"]
    record["search"] = closure["search_configuration"]
    record["session"] = {"native_api_pid": 21040, **closure["session"]}
    record["stored_references_phase"] = "preopen"
    record["stored_references"] = {
        name: document["stored_references"] for name, document in closure["documents"].items()
    }
    return record
