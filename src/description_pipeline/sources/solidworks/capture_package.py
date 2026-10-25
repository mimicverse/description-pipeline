"""Strict consumer for the one prepared Capture package."""

from __future__ import annotations

import hashlib
from pathlib import Path

from ...io import PipelineError, artifact_path_parts, digest, read_data
from .discovery import (
    DISCOVERY_FILE,
    PLAN_FILE,
    PLAN_SCHEMA,
    SOURCE_FILE,
    native_component_identity,
    sealed_embedded_virtual_document,
)
from .selection import selection_bindings

CAPTURE_EVIDENCE_REQUESTS = (
    "evidence/capture.json",
    "evidence/geometry/",
    "evidence/native-runtime.json",
)

_PLAN_KEYS = frozenset(
    {
        "schema_version",
        "run_id",
        "handoff_sha256",
        "discovery_sha256",
        "main_assembly",
        "configuration",
        "identity",
        "selection",
        "objects",
        "groups",
        "axis_requests",
        "datums",
        "evidence_requests",
    }
)


def _require_selector_state(item: dict, component: object) -> None:
    """Require the plan state to equal the native suppression integer."""

    component_state = (
        component.get("suppression_state")
        if isinstance(component, dict)
        else None
    )
    plan_state = item.get("state")
    if (
        component is None
        or type(component_state) is not int
        or component_state not in {0, 1, 2, 3, 4}
        or type(plan_state) is not int
        or plan_state != component_state
    ):
        raise PipelineError(
            "The sealed capture plan does not bind the native suppression state"
        )


def _require_supported_source_kind(component: object, name: str) -> None:
    """Reject the source class this capture runtime does not implement."""

    provenance = (
        component.get("source_provenance")
        if isinstance(component, dict)
        and isinstance(component.get("source_provenance"), dict)
        else {}
    )
    if (
        provenance.get("kind") == "native_import_materialization"
        or provenance.get("state") == "cached_materialization"
    ):
        error = PipelineError(
            f"Cached 3D Interconnect import is not an accepted capture input: {name}"
        )
        error.details = {
            "object": name,
            "reason": "cached_materialization",
            "state": provenance.get("state"),
            "kind": provenance.get("kind"),
            "used_api": provenance.get("used_api"),
            "is_virtual": provenance.get("is_virtual"),
        }
        raise error


def consume_sealed_definition(package: Path) -> tuple[dict, dict]:
    """Consume the sealed source identity and Capture plan from one package."""

    discovery_path = package / DISCOVERY_FILE
    source_path = package / SOURCE_FILE
    plan_path = package / PLAN_FILE
    if not discovery_path.is_file():
        raise PipelineError(
            "The prepared package carries no sealed discovery record"
        )
    if not source_path.is_file():
        raise PipelineError("The prepared package carries no sealed source identity")
    if not plan_path.is_file():
        raise PipelineError("The prepared package carries no sealed capture plan")
    source = read_data(source_path)
    if not isinstance(source, dict) or not source:
        raise PipelineError("The sealed source identity is not a usable record")
    plan = read_data(plan_path)
    if not isinstance(plan, dict) or plan.get("schema_version") != PLAN_SCHEMA:
        raise PipelineError("The sealed capture plan has an unexpected schema")
    if set(plan) != _PLAN_KEYS:
        raise PipelineError("The sealed capture plan carries an unknown field")
    payload = read_data(discovery_path)
    if not isinstance(payload, dict):
        raise PipelineError("The sealed discovery record is not a usable record")
    if plan.get("discovery_sha256") != hashlib.sha256(
        discovery_path.read_bytes()
    ).hexdigest():
        raise PipelineError(
            "The sealed capture plan does not bind the sealed discovery"
        )
    native_files = (
        payload.get("native_files")
        if isinstance(payload.get("native_files"), dict)
        else {}
    )
    expected_files = {
        str(key): str(value) for key, value in native_files.items()
    }
    if plan.get("handoff_sha256") != digest(expected_files):
        raise PipelineError(
            "The sealed capture plan does not bind the frozen inventory"
        )
    selection = plan.get("selection")
    files = selection.get("files") if isinstance(selection, dict) else None
    if (
        not isinstance(files, dict)
        or {str(key): str(value) for key, value in files.items()}
        != expected_files
    ):
        raise PipelineError(
            "The sealed capture plan selection differs from the frozen inventory"
        )
    derivation = (
        payload.get("derivation")
        if isinstance(payload.get("derivation"), dict)
        else {}
    )
    if plan.get("main_assembly") != derivation.get("main_assembly"):
        raise PipelineError(
            "The sealed capture plan names another project entry than the sealed discovery"
        )
    record = payload.get("raw") if isinstance(payload.get("raw"), dict) else {}
    raw_selection = record.get("source_selection")
    bindings = selection_bindings(raw_selection)
    if (
        bindings["files"] != expected_files
        or record.get("files") != bindings["files"]
        or plan.get("selection", {}).get("files") != bindings["files"]
        or bindings["main_assembly"] != plan.get("main_assembly")
    ):
        raise PipelineError(
            "The sealed source selection does not bind the complete frozen inventory"
        )
    saved = record.get("saved_configuration")
    if isinstance(saved, str) and saved and plan.get("configuration") != saved:
        raise PipelineError(
            "The sealed capture plan configuration disagrees with the recorded saved configuration"
        )
    if plan.get("run_id") != payload.get("run_id"):
        raise PipelineError(
            "The sealed capture plan names another run than the sealed discovery"
        )
    component_rows = [
        item
        for item in (record.get("components") or [])
        if isinstance(item, dict)
    ]
    component_names = [
        native_component_identity(item) for item in component_rows
    ]
    if len(set(component_names)) != len(component_names):
        raise PipelineError(
            "The sealed capture plan repeats a native object selector"
        )
    raw_documents = {
        name: str(item.get("document") or "").replace("\\", "/")
        for name, item in zip(component_names, component_rows, strict=True)
        if not item.get("suppressed")
    }
    raw_components = dict(
        zip(component_names, component_rows, strict=True)
    )
    registered = set(raw_documents)
    objects = plan.get("objects")
    if not isinstance(objects, list) or not objects:
        raise PipelineError(
            "The sealed capture plan carries no native object selectors"
        )
    names: list[str] = []
    for item in objects:
        if not isinstance(item, dict) or set(item) - {
            "name",
            "document",
            "state",
        }:
            raise PipelineError(
                "The sealed capture plan carries an unknown native selector field"
            )
        name = item.get("name")
        if not isinstance(name, str) or not name:
            raise PipelineError(
                "The sealed capture plan carries no usable native object selectors"
            )
        if name not in registered:
            raise PipelineError(
                "The sealed capture plan names instances outside the sealed record"
            )
        component = raw_components.get(name)
        _require_supported_source_kind(component, name)
        document = item.get("document")
        if document is None:
            virtual = isinstance(
                component, dict
            ) and sealed_embedded_virtual_document(
                component,
                name,
                expected_files,
            )
            if component is None or not virtual:
                raise PipelineError(
                    "The sealed capture plan omits a native selector document without complete session proof"
                )
            provenance = (
                component.get("source_provenance")
                if isinstance(component.get("source_provenance"), dict)
                else {}
            )
            closure = (record.get("source_selection") or {}).get("closure")
            virtuals = (
                closure.get("virtual_components")
                if isinstance(closure, dict)
                else None
            )
            matches = [
                row
                for row in (virtuals or [])
                if isinstance(row, dict)
                and (row.get("instance") or row.get("name")) == name
            ]
            if len(matches) != 1:
                raise PipelineError(
                    "The sealed closure carries no unique embedded-virtual occurrence"
                )
            entry = matches[0]
            root = entry.get("session_temp_root")
            path = entry.get("session_document_path")
            pid = entry.get("native_api_pid")
            root_name = (
                root.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
                if isinstance(root, str)
                else ""
            )
            if (
                entry.get("kind") != provenance.get("kind")
                or entry.get("state") != provenance.get("state")
                or entry.get("is_virtual") is not True
                or entry.get("occurrence", entry.get("instance")) != name
                or entry.get("owner") != provenance.get("owner")
                or entry.get("owner_sha256")
                != provenance.get("owner_sha256")
                or entry.get("configuration")
                != component.get("configuration")
                or entry.get("suppression_state")
                != component.get("suppression_state")
                or entry.get("used_api") != provenance.get("used_api")
                or entry.get("binding") != "owner_embedded_geometry"
                or "author_imported_path" not in entry
                or "author_imported_error" not in entry
                or "owner_anchor" not in entry
                or entry.get("author_imported_path")
                != provenance.get("author_imported_path")
                or entry.get("author_imported_error")
                != provenance.get("author_imported_error")
                or entry.get("owner_anchor")
                != provenance.get("defining_anchor")
                or type(entry.get("suppression_state")) is not int
                or not isinstance(root, str)
                or not isinstance(path, str)
                or type(pid) is not int
                or pid <= 0
                or not root_name.startswith("swx")
                or not root_name[3:].isdigit()
                or int(root_name[3:]) != pid
                or not path.replace("\\", "/")
                .casefold()
                .startswith(root.replace("\\", "/").casefold() + "/")
                or path.replace("\\", "/").casefold().count("/vc~~/") != 1
            ):
                raise PipelineError(
                    "The sealed closure does not bind the embedded-virtual occurrence"
                )
        if document is not None:
            if not isinstance(document, str) or not document:
                raise PipelineError(
                    "The sealed capture plan carries an unusable selector document"
                )
            try:
                artifact_path_parts(document)
            except PipelineError as error:
                raise PipelineError(
                    "The sealed capture plan carries an unsafe selector document"
                ) from error
            if not document.lower().endswith((".sldprt", ".sldasm")):
                raise PipelineError(
                    "The sealed capture plan carries a non-CAD selector document"
                )
            recorded_document = raw_documents.get(name, "")
            if recorded_document and recorded_document != document:
                raise PipelineError(
                    "The sealed capture plan binds another document to a native instance"
                )
        component = raw_components.get(name)
        _require_selector_state(item, component)
        names.append(name)
    if len(set(names)) != len(names):
        raise PipelineError(
            "The sealed capture plan repeats a native object selector"
        )
    if not registered or not set(names) <= registered:
        raise PipelineError(
            "The sealed capture plan names instances outside the sealed record"
        )
    datums = plan.get("datums")
    if (
        not isinstance(datums, list)
        or any(
            not isinstance(item, str) or not item for item in datums
        )
        or len(set(datums)) != len(datums)
    ):
        raise PipelineError(
            "The sealed capture plan carries unusable datums"
        )
    if plan.get("evidence_requests") != list(CAPTURE_EVIDENCE_REQUESTS):
        raise PipelineError(
            "The sealed capture plan requests unknown evidence items"
        )
    groups = plan.get("groups")
    if not isinstance(groups, list) or not groups:
        raise PipelineError(
            "The sealed capture plan carries no native link-group selections"
        )
    for item in groups:
        if (
            not isinstance(item, dict)
            or set(item) != {"name", "components"}
            or not isinstance(item["name"], str)
            or not item["name"]
            or not isinstance(item["components"], list)
            or any(
                not isinstance(member, str) or not member
                for member in item["components"]
            )
        ):
            raise PipelineError(
                "The sealed capture plan carries unusable native link-group selections"
            )
    axis_requests = plan.get("axis_requests")
    if not isinstance(axis_requests, list):
        raise PipelineError(
            "The sealed capture plan carries no native axis requests"
        )
    for item in axis_requests:
        if (
            not isinstance(item, dict)
            or set(item) != {"name", "axis_reference"}
            or not isinstance(item["name"], str)
            or not item["name"]
            or not isinstance(item["axis_reference"], dict)
        ):
            raise PipelineError(
                "The sealed capture plan carries unusable native axis requests"
            )
    return source, plan


__all__ = [
    "CAPTURE_EVIDENCE_REQUESTS",
    "consume_sealed_definition",
]
