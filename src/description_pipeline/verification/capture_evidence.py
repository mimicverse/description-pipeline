"""Standard-library readers and independent validation for frozen capture evidence."""

from __future__ import annotations

import ntpath
import re
from pathlib import Path

from ..delivery import read_source_record
from ..geometry.native_scope import native_source_scope_is_valid
from ..io import PipelineError, confined, file_digest, read_data
from ..sources.solidworks.session_artifacts import (
    embedded_virtual_component_under_root,
    session_root_pid,
)


def is_container(component: dict | None) -> bool:
    """An assembly-document occurrence is a container, never a physical member."""

    if not isinstance(component, dict):
        return False
    return str(component.get("document") or "").strip().lower().endswith(".sldasm")


# Delivery evidence reads (IF-13 tree): one owner for the frozen block shapes.

CAPTURE = "evidence/capture.json"
DISCOVERY = "evidence/discovery.json"
RUNTIME = "evidence/native-runtime.json"
HANDOFF = "input/handoff.json"
INERTIA_CONVENTION = "evidence/raw/inertia_convention.json"


def _delivery_root(root: Path) -> Path:
    root = Path(root).resolve()
    return root.parent if root.name == "evidence" else root


def capture(root) -> dict:
    record = read_data(confined(_delivery_root(root), CAPTURE))
    if not isinstance(record, dict):
        raise PipelineError("Capture evidence is not an object")
    return record


def discovery(root) -> dict:
    record = read_data(confined(_delivery_root(root), DISCOVERY))
    if not isinstance(record, dict):
        raise PipelineError("Discovery evidence is not an object")
    return record


def runtime(root) -> dict:
    record = read_data(confined(_delivery_root(root), RUNTIME))
    if not isinstance(record, dict):
        raise PipelineError("Native runtime evidence is not an object")
    return record


def handoff(root) -> dict:
    record = read_data(confined(_delivery_root(root), HANDOFF))
    if not isinstance(record, dict):
        raise PipelineError("Frozen handoff record is not an object")
    return record


def source(root) -> dict:
    return read_source_record(_delivery_root(root))


def block(root, name: str):
    """A same-name verbatim capture block (the frozen payloads)."""

    record = capture(root)
    if name not in record:
        raise PipelineError(f"Capture evidence block is missing: {name}")
    return record[name]


def scene(root) -> dict:
    return block(root, "scene")


def mass_closure(root) -> dict:
    return block(root, "mass_closure")


def material_provenance(root) -> dict:
    return block(root, "material_provenance")


def dependency_closure(root) -> dict:
    return block(root, "dependency_closure")


def link_group_mass_properties(root) -> dict:
    return block(root, "link_group_mass_properties")


def collection(root) -> dict:
    """The verbatim collection block (kind/evidence_class/identity/limits/environment)."""

    return block(root, "collection")


def effective_instances(root) -> dict:
    """The verbatim effective-instance record (schema/warnings/readings keys intact)."""

    return block(root, "effective_instances")


def native_capture_facts(root) -> dict:
    """Read the one frozen raw native fact member."""

    delivery = _delivery_root(root)
    path = confined(
        delivery,
        "evidence/raw/native_capture_facts.json",
    )
    record = read_data(path)
    if not isinstance(record, dict):
        raise PipelineError("Sealed native capture facts are not an object")
    return record


def inertia_convention(root) -> dict:
    """The sealed product-of-inertia decision applied after projection."""

    record = read_data(confined(_delivery_root(root), INERTIA_CONVENTION))
    if not isinstance(record, dict):
        raise PipelineError("Inertia convention evidence is not an object")
    return record


def geometry(root):
    return block(root, "geometry")


def _offline_virtual_component_root(value: object) -> str | None:
    """Recover one ``swxNNN`` root from sealed Windows or POSIX evidence."""

    if not isinstance(value, str) or not value.strip():
        return None
    text = value.replace("\\", "/")
    folded = text.casefold()
    marker = "/vc~~/"
    if folded.count(marker) != 1:
        return None
    position = folded.index(marker)
    prefix = text[:position]
    root = prefix.rsplit("/", 1)[-1]
    if re.fullmatch(r"swx[0-9]+", root, re.IGNORECASE) is None:
        return None
    return text[: position].replace("/", "\\") if "\\" in value else prefix


def _canonical_evidence_path(value: object) -> str | None:
    """Canonicalize one recorded path without host-dependent casing."""

    if not isinstance(value, str) or not value.strip():
        return None
    normalized = ntpath.normpath(value.replace("/", "\\")).replace(
        "\\",
        "/",
    )
    if normalized in {"", ".", ".."} or normalized.startswith("../"):
        return None
    return normalized.casefold()


def source_scopes(root) -> dict:
    """Read source scopes and cross-check them against finite sealed anchors."""

    try:
        path = confined(
            _delivery_root(root),
            "evidence/raw/source_scopes.json",
        )
    except PipelineError:
        return {}
    if not path.is_file():
        return {}
    record = read_data(path)
    if not isinstance(record, dict):
        raise PipelineError("Source-scope evidence is not an object")
    discovery_record = discovery(root)
    discovery_raw = (
        discovery_record.get("raw")
        if isinstance(discovery_record.get("raw"), dict)
        else {}
    )
    discovery_components = discovery_raw.get("components")
    if not isinstance(discovery_components, list):
        raise PipelineError("Discovery source components are unavailable")
    discovery_by_name = {}
    for row in discovery_components:
        if not isinstance(row, dict):
            raise PipelineError("Discovery source component is malformed")
        name = row.get("instance_id") or row.get("name2")
        if not isinstance(name, str) or not name:
            raise PipelineError("Discovery source component has no identity")
        if name in discovery_by_name:
            raise PipelineError(
                f"Discovery source component is duplicated: {name}"
            )
        discovery_by_name[name] = row
    scene_path = confined(
        _delivery_root(root),
        "evidence/raw/scene_raw.json",
    )
    scene_record = read_data(scene_path)
    scene_components = scene_record.get("components")
    if not isinstance(scene_components, list):
        raise PipelineError("Scene source components are unavailable")
    scene_by_name = {}
    for row in scene_components:
        if not isinstance(row, dict):
            raise PipelineError("Scene source component is malformed")
        name = row.get("name")
        if not isinstance(name, str) or not name:
            raise PipelineError("Scene source component has no identity")
        if name in scene_by_name:
            raise PipelineError(
                f"Scene source component is duplicated: {name}"
            )
        scene_by_name[name] = row
    geometry_names = set()
    try:
        geometry_path = confined(
            _delivery_root(root),
            "evidence/raw/geometry.json",
        )
    except PipelineError:
        geometry_path = None
    if geometry_path is not None:
        geometry_rows = read_data(geometry_path)
        if not isinstance(geometry_rows, list):
            raise PipelineError("Geometry component inventory is malformed")
        for row in geometry_rows:
            if (
                not isinstance(row, dict)
                or not isinstance(row.get("component"), str)
                or not row["component"]
            ):
                raise PipelineError("Geometry component identity is malformed")
            if row["component"] in geometry_names:
                raise PipelineError(
                    f"Geometry component is duplicated: {row['component']}"
                )
            geometry_names.add(row["component"])
    scope_names = set(record)
    scene_names = set(scene_by_name)
    discovery_exported_names = {
        name
        for name, row in discovery_by_name.items()
        if not is_container(row) and row.get("suppressed") is not True
    }
    if (
        scope_names != scene_names
        or discovery_exported_names != scene_names
        or (geometry_names and scope_names != geometry_names)
    ):
        raise PipelineError(
            "Source scopes do not cover the native component inventories"
        )
    capture_record = capture(root)
    capture_components = capture_record.get("components")
    capture_top_by_name = {}
    capture_scene_by_name = {}

    def capture_rows_by_name(rows, label):
        result = {}
        for row in rows:
            if not isinstance(row, dict):
                raise PipelineError(f"{label} component is malformed")
            component_name = (
                row.get("instance_id")
                or row.get("name2")
                or row.get("name")
            )
            if not isinstance(component_name, str) or not component_name:
                raise PipelineError(f"{label} component has no identity")
            if component_name in result:
                raise PipelineError(
                    f"{label} component is duplicated: {component_name}"
                )
            result[component_name] = row
        return result

    if type(capture_components) is int:
        if capture_components != len(scope_names):
            raise PipelineError(
                "Capture component count differs from source scopes"
            )
    elif isinstance(capture_components, list):
        capture_top_by_name = capture_rows_by_name(
            capture_components,
            "Capture",
        )
        capture_names = set(capture_top_by_name)
        if capture_names != scope_names:
            raise PipelineError(
                "Capture component identities differ from source scopes"
            )
    capture_scene = capture_record.get("scene")
    if isinstance(capture_scene, dict):
        capture_scene_components = capture_scene.get("components")
        if capture_scene_components is not None:
            if not isinstance(capture_scene_components, list):
                raise PipelineError(
                    "Capture scene components are malformed"
                )
            capture_scene_by_name = capture_rows_by_name(
                capture_scene_components,
                "Capture scene",
            )
            if set(capture_scene_by_name) != scope_names:
                raise PipelineError(
                    "Capture scene component identities differ from source scopes"
                )
    source_record = source(root)
    if not isinstance(source_record, dict):
        raise PipelineError("Source record is malformed")
    selection = source_record.get("selection")
    if (
        not isinstance(selection, dict)
        or not isinstance(selection.get("configuration"), str)
        or not selection["configuration"]
    ):
        raise PipelineError("Source record has no requested configuration")
    files = discovery_record.get("files")
    if not isinstance(files, list):
        raise PipelineError("Discovery source files are unavailable")

    def unique_path_index(entries, label):
        result = {}
        for raw_path, checksum in entries:
            if (
                not isinstance(raw_path, str)
                or not raw_path
                or not isinstance(checksum, str)
            ):
                raise PipelineError(f"{label} path index is malformed")
            key = ntpath.normpath(raw_path).replace("\\", "/").casefold()
            if key in result:
                raise PipelineError(f"{label} path index is duplicated: {key}")
            result[key] = checksum
        return result

    file_rows = []
    for row in files:
        if not isinstance(row, dict):
            raise PipelineError("Discovery source file is malformed")
        file_rows.append((row.get("path"), row.get("sha256")))
    discovery_files_case = unique_path_index(
        file_rows,
        "Discovery source files",
    )
    source_files = discovery_raw.get("files")
    if not isinstance(source_files, dict):
        raise PipelineError("Discovery source file hashes are unavailable")
    source_files_case = unique_path_index(
        source_files.items(),
        "Discovery source files",
    )
    dependency_path = confined(
        _delivery_root(root),
        "evidence/raw/dependency_closure.json",
    )
    dependency = read_data(dependency_path)
    if not isinstance(dependency, dict):
        raise PipelineError("Dependency closure is malformed")
    original_files = dependency.get("original_files")
    copy_files = dependency.get("copy_files")
    original_files_case = (
        unique_path_index(
            original_files.items(),
            "Dependency original files",
        )
        if isinstance(original_files, dict)
        else {}
    )
    copy_files_case = (
        unique_path_index(
            copy_files.items(),
            "Dependency copy files",
        )
        if isinstance(copy_files, dict)
        else {}
    )
    delivery_root = _delivery_root(root)
    frozen_source_digests = {}
    for name, scope in record.items():
        if not native_source_scope_is_valid(scope, name):
            raise PipelineError(
                f"Source scope is malformed or unbound: {name}"
            )
        discovery_row = discovery_by_name[name]
        scene_row = scene_by_name[name]
        configuration = discovery_row.get("configuration")
        if (
            not isinstance(configuration, str)
            or configuration != scope["referenced_configuration"]
        ):
            raise PipelineError(
                f"Source configuration differs from discovery: {name}"
            )
        for label, capture_table in (
            ("capture", capture_top_by_name),
            ("capture scene", capture_scene_by_name),
        ):
            capture_row = capture_table.get(name)
            if (
                isinstance(capture_row, dict)
                and "configuration" in capture_row
                and capture_row["configuration"]
                != scope["referenced_configuration"]
            ):
                raise PipelineError(
                    f"Source configuration differs from {label}: {name}"
                )
        capture_mass = capture_record.get("mass_properties")
        capture_mass_row = (
            capture_mass.get(name)
            if isinstance(capture_mass, dict)
            else None
        )
        capture_mass_reference = (
            capture_mass_row.get("reference")
            if isinstance(capture_mass_row, dict)
            else None
        )
        if (
            isinstance(capture_mass_reference, dict)
            and capture_mass_reference.get("configuration")
            != scope["referenced_configuration"]
        ):
            raise PipelineError(
                f"Source configuration differs from capture mass: {name}"
            )
        if (
            "configuration" in scene_row
            and scene_row["configuration"]
            != scope["referenced_configuration"]
        ):
            raise PipelineError(
                f"Source configuration differs from scene: {name}"
            )
        scene_mass = scene_record.get("mass_properties")
        mass_row = (
            scene_mass.get(name)
            if isinstance(scene_mass, dict)
            else None
        )
        mass_reference = (
            mass_row.get("reference")
            if isinstance(mass_row, dict)
            else None
        )
        if (
            isinstance(mass_reference, dict)
            and mass_reference.get("configuration")
            != scope["referenced_configuration"]
        ):
            raise PipelineError(
                f"Source configuration differs from scene mass: {name}"
            )
        source_path = scope["source_document_path"]
        scene_path = scene_row.get("path")
        if (
            not isinstance(scene_path, str)
            or ntpath.normcase(ntpath.normpath(scene_path))
            != ntpath.normcase(ntpath.normpath(source_path))
        ):
            raise PipelineError(
                f"Source path differs from scene: {name}"
            )
        if scope["source_document_identity_kind"] == "file_sha256":
            document = discovery_row.get("document")
            if not isinstance(document, str) or not document:
                raise PipelineError(
                    f"File source document is missing: {name}"
                )
            document_path = ntpath.normpath(document).replace("\\", "/")
            document_key = document_path.casefold()
            expected_source_sha = discovery_files_case.get(document_key)
            raw_source_sha = source_files_case.get(document_key)
            mapping_copy = _canonical_evidence_path(
                scope["source_copy_path"]
            )
            mapping_copy_key = (
                mapping_copy.casefold() if mapping_copy is not None else None
            )
            mapping_rows = [
                row
                for row in dependency.get("mapping") or []
                if isinstance(row, dict)
                and ntpath.normpath(str(row.get("copy") or ""))
                .replace("\\", "/")
                .casefold()
                == (mapping_copy_key or "")
            ]
            mapping_source = (
                mapping_rows[0].get("source")
                if len(mapping_rows) == 1
                else None
            )
            original_sha = (
                original_files_case.get(
                    ntpath.normpath(str(mapping_source))
                    .replace("\\", "/")
                    .casefold()
                )
                if isinstance(mapping_source, str)
                else None
            )
            copy_sha = (
                copy_files_case.get(mapping_copy_key)
                if mapping_copy_key is not None
                else None
            )
            normalized_source_path = (
                ntpath.normpath(source_path).replace("\\", "/").casefold()
            )
            normalized_mapping_source = (
                ntpath.normpath(str(mapping_source))
                .replace("\\", "/")
                .casefold()
                if isinstance(mapping_source, str)
                else ""
            )
            if (
                expected_source_sha is None
                or expected_source_sha != scope["source_document_sha256"]
                or raw_source_sha is None
                or raw_source_sha != scope["source_document_sha256"]
                or mapping_copy_key is None
                or len(mapping_rows) != 1
                or not isinstance(mapping_source, str)
                or original_sha != scope["source_document_sha256"]
                or mapping_rows[0].get("copy_sha256")
                != scope["source_copy_sha256"]
                or copy_sha != scope["source_copy_sha256"]
                or ntpath.basename(mapping_source).casefold()
                != ntpath.basename(document).casefold()
                or not normalized_mapping_source.endswith(
                    "/" + document_key
                )
                or not normalized_source_path.endswith(
                    "/" + mapping_copy_key
                )
                or ntpath.basename(source_path).casefold()
                != ntpath.basename(document).casefold()
            ):
                raise PipelineError(
                    f"File source identity differs from discovery: {name}"
                )
            frozen_source = confined(
                delivery_root,
                f"evidence/{scope['source_copy_path']}",
            )
            frozen_source_key = str(frozen_source.resolve())
            if not frozen_source.is_file():
                raise PipelineError(
                    f"Frozen source bytes differ from the source scope: {name}"
                )
            if frozen_source_key not in frozen_source_digests:
                frozen_source_digests[frozen_source_key] = file_digest(
                    frozen_source
                )
            if (
                frozen_source_digests[frozen_source_key]
                != scope["source_copy_sha256"]
            ):
                raise PipelineError(
                    f"Frozen source bytes differ from the source scope: {name}"
                )
        else:
            provenance = discovery_row.get("source_provenance")
            if not isinstance(provenance, dict):
                raise PipelineError(
                    f"Virtual source provenance is missing: {name}"
                )
            normalized_scope_path = _canonical_evidence_path(source_path)
            normalized_scope_root = _canonical_evidence_path(
                scope["virtual_session_temp_root"]
            )
            normalized_session_path = _canonical_evidence_path(
                provenance.get("session_document_path")
            )
            normalized_session_root = _canonical_evidence_path(
                provenance.get("session_temp_root")
            )
            scope_source_root = _offline_virtual_component_root(source_path)
            canonical_scope_source_root = _canonical_evidence_path(
                scope_source_root
            )
            scope_relative = (
                _canonical_evidence_path(
                    ntpath.relpath(
                        normalized_scope_path,
                        canonical_scope_source_root,
                    )
                )
                if (
                    normalized_scope_path is not None
                    and canonical_scope_source_root is not None
                )
                else None
            )
            provenance_relative = (
                _canonical_evidence_path(
                    ntpath.relpath(
                        normalized_session_path,
                        normalized_session_root,
                    )
                )
                if (
                    normalized_session_path is not None
                    and normalized_session_root is not None
                )
                else None
            )
            owner = provenance.get("owner")
            owner_file_sha = source_files_case.get(
                ntpath.normpath(str(owner))
                .replace("\\", "/")
                .casefold()
            )
            if (
                not embedded_virtual_component_under_root(
                    source_path,
                    scope_source_root,
                )
                or provenance.get("is_virtual") is not True
                or provenance.get("state")
                != "embedded_virtual_component"
                or provenance.get("kind")
                != "native_embedded_virtual_component"
                or provenance.get("owner") != scope["virtual_owner"]
                or provenance.get("owner_sha256")
                != scope["virtual_owner_sha256"]
                or owner_file_sha != scope["virtual_owner_sha256"]
                or provenance.get("occurrence") != name
                or scope_source_root is None
                or canonical_scope_source_root != normalized_scope_root
                or session_root_pid(scope["virtual_session_temp_root"])
                != scope["virtual_native_api_pid"]
                or session_root_pid(provenance.get("session_temp_root"))
                != provenance.get("native_api_pid")
                or scope_relative != provenance_relative
                or not embedded_virtual_component_under_root(
                    provenance.get("session_document_path"),
                    provenance.get("session_temp_root"),
                )
            ):
                raise PipelineError(
                    f"Virtual source owner/path/root differs: {name}"
                )
    return record


def components(root) -> list:
    record = capture(root)
    rows = record.get("components")
    if not isinstance(rows, list):
        raise PipelineError("Capture evidence carries no component readings")
    return rows


def mass_properties(root) -> dict:
    """Component-name to mass-property reading, as carried by the capture rows."""

    rows = components(root)
    readings = {str(row.get("name")): row.get("mass_properties") for row in rows}
    if not readings or any(value is None for value in readings.values()):
        raise PipelineError("Component mass-property readings are missing")
    return readings


def datums(root) -> dict:
    """Datum name to 4x4 row-major matrix, as carried by the capture rows."""

    record = capture(root)
    rows = record.get("datums")
    if not isinstance(rows, list):
        raise PipelineError("Capture evidence carries no datum records")
    return {str(row["name"]): row["matrix"] for row in rows}


def axes(root) -> list:
    """Axis re-read records (verbatim entries, plus ``kinds`` for the rebuild)."""

    record = capture(root)
    rows = record.get("axes")
    if not isinstance(rows, list):
        raise PipelineError("Capture evidence carries no axis references")
    return rows
