"""Native Discover domain records and sealed observation evidence."""

from __future__ import annotations

import copy
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

from ...io import PipelineError, canonical, confined, digest, file_digest, read_data
from ...interface_records import REQUEST_PURPOSES
from .discovery import DISCOVERY_SCHEMA, DiscoverySettings, read_native_record
from .revision import package_inventory
from .selection import selection_bindings, validate_file_index

NATIVE_REQUEST_SCHEMA = "description-pipeline.native-discover-request/v1"
NATIVE_OBSERVATION_SCHEMA = "description-pipeline.native-observation/v1"
OBSERVATION_MANIFEST = "observation.json"
OBSERVATION_MEMBERS = frozenset({"request.json", "source.json", "selection.json", "record.json", "tool.json"})
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_REQUEST_FIELDS = frozenset(
    {
        "schema_version",
        "operation_id",
        "command_id",
        "run_id",
        "request_digest",
        "source_files",
        "source_inventory_sha256",
        "main_assembly",
        "purpose",
        "expected_native_tool_sha256",
    }
)
_OBSERVATION_FIELDS = frozenset(
    {
        "schema_version",
        "request_sha256",
        "source_archive_sha256",
        "source_files",
        "source_inventory_sha256",
        "main_assembly",
        "selection_sha256",
        "selected_files_sha256",
        "native_tool",
        "native_tool_sha256",
        "record_sha256",
        "record_bytes",
        "files",
    }
)


class NativeDiscoverRefusal(PipelineError):
    """A deterministic native refusal retaining stable diagnostics."""

    def __init__(self, code: str, message: str, detail: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = dict(detail or {})


def _require(condition: bool, code: str, message: str, detail: Mapping[str, Any] | None = None) -> None:
    if not condition:
        raise NativeDiscoverRefusal(code, message, detail)


def _sha(value: object, field: str) -> str:
    _require(
        isinstance(value, str) and _SHA256.fullmatch(value) is not None,
        "discover.sha_invalid",
        f"{field} is invalid",
        {"field": field},
    )
    return str(value)


def _write_canonical(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical(value) + b"\n")


@dataclass(frozen=True, slots=True)
class NativeDiscoverRequest:
    """The non-secret native-side input of one Discover operation."""

    schema_version: str
    operation_id: str
    command_id: str
    run_id: str
    request_digest: str
    source_files: Mapping[str, str]
    source_inventory_sha256: str
    main_assembly: str
    purpose: str
    expected_native_tool_sha256: str

    def __post_init__(self) -> None:
        if self.schema_version != NATIVE_REQUEST_SCHEMA:
            raise NativeDiscoverRefusal("discover.native_request_invalid", "Native request schema is unsupported")
        for field in ("operation_id", "command_id", "run_id", "main_assembly"):
            value = getattr(self, field)
            _require(
                isinstance(value, str) and bool(value) and "\x00" not in value,
                "discover.native_request_invalid",
                f"{field} is invalid",
                {"field": field},
            )
        _sha(self.request_digest, "request_digest")
        files = validate_file_index(self.source_files)
        object.__setattr__(self, "source_files", MappingProxyType(files))
        _sha(self.source_inventory_sha256, "source_inventory_sha256")
        _require(
            digest(files) == self.source_inventory_sha256,
            "discover.source_inventory_mismatch",
            "Native source inventory differs from its request digest",
        )
        _require(
            self.main_assembly in files, "discover.main_assembly_invalid", "Main assembly is absent from the source set"
        )
        _require(self.purpose in REQUEST_PURPOSES, "discover.native_request_invalid", "Purpose is unsupported")
        _sha(self.expected_native_tool_sha256, "expected_native_tool_sha256")

    def to_record(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "operation_id": self.operation_id,
            "command_id": self.command_id,
            "run_id": self.run_id,
            "request_digest": self.request_digest,
            "source_files": dict(self.source_files),
            "source_inventory_sha256": self.source_inventory_sha256,
            "main_assembly": self.main_assembly,
            "purpose": self.purpose,
            "expected_native_tool_sha256": self.expected_native_tool_sha256,
        }

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> NativeDiscoverRequest:
        _require(isinstance(record, Mapping), "discover.native_request_invalid", "Native request must be an object")
        _require(set(record) == _REQUEST_FIELDS, "discover.native_request_invalid", "Native request fields are invalid")
        return cls(**dict(record))


@dataclass(frozen=True, slots=True)
class NativeObservation:
    """One sealed native observation with its selection and actual tool identity."""

    root: Path
    manifest: Mapping[str, Any]
    record: Mapping[str, Any]
    selection: Mapping[str, Any]
    native_tool: Mapping[str, Any]
    resources_settled: bool = False

    def __post_init__(self) -> None:
        if type(self.resources_settled) is not bool:
            raise NativeDiscoverRefusal(
                "discover.observation_invalid",
                "Native observation resource settlement must be Boolean",
            )

    @property
    def observation_sha256(self) -> str:
        return file_digest(self.root / OBSERVATION_MANIFEST)


def _validated_tool(native_tool: object, expected_sha256: str) -> dict:
    _require(
        isinstance(native_tool, Mapping) and bool(native_tool),
        "discover.native_tool_invalid",
        "Native tool record is invalid",
    )
    tool = dict(native_tool)
    _require(
        tool.get("source_sha256") == expected_sha256,
        "discover.native_tool_mismatch",
        "Native tool source identity differs from the expected tool",
    )
    return tool


def observe_native_record(
    selected_source: Path,
    output: Path,
    request: NativeDiscoverRequest,
    *,
    selection: Mapping[str, Any],
    native_tool: Mapping[str, Any],
    source_archive_sha256: str,
    backend=None,
    on_activity=None,
) -> NativeObservation:
    """Read one selected staged source and seal all native observations.

    This function runs on the native host. It performs no rule derivation and
    starts no Capture operation.
    """

    selected_source = Path(selected_source)
    output = Path(output)
    _require(output.exists() is False, "discover.observation_exists", "Observation destination already exists")
    bindings = selection_bindings(selection)
    _require(
        bindings["upload_sha256"] == request.source_inventory_sha256,
        "discover.source_selection_mismatch",
        "Sealed selection was computed from another full SourceSet",
    )
    _require(
        package_inventory(selected_source) == bindings["files"],
        "discover.source_selection_mismatch",
        "Selected staged files differ from the sealed selection",
    )
    tool = _validated_tool(native_tool, request.expected_native_tool_sha256)
    settings = DiscoverySettings(main_assembly=request.main_assembly)
    record = read_native_record(selected_source, settings, backend=backend, on_activity=on_activity)
    record = copy.deepcopy(record)
    record["source_selection"] = copy.deepcopy(bindings["selection"])
    _require(
        isinstance(record.get("files"), Mapping) and dict(record["files"]) == dict(bindings["files"]),
        "discover.native_record_mismatch",
        "Native observation did not read exactly the sealed selection",
    )

    output.mkdir(parents=True)
    request_record = request.to_record()
    source_record = {
        "schema_version": "description-pipeline.native-source-set/v1",
        "source_files": dict(request.source_files),
        "source_inventory_sha256": request.source_inventory_sha256,
    }
    _write_canonical(output / "request.json", request_record)
    _write_canonical(output / "source.json", source_record)
    _write_canonical(output / "selection.json", bindings["selection"])
    _write_canonical(output / "record.json", record)
    _write_canonical(output / "tool.json", tool)
    record_bytes = canonical(record)
    members = [
        {"path": path, "bytes": (output / path).stat().st_size, "sha256": file_digest(output / path)}
        for path in sorted(OBSERVATION_MEMBERS)
    ]
    manifest = {
        "schema_version": NATIVE_OBSERVATION_SCHEMA,
        "request_sha256": digest(request_record),
        "source_archive_sha256": _sha(source_archive_sha256, "source_archive_sha256"),
        "source_files": dict(request.source_files),
        "source_inventory_sha256": request.source_inventory_sha256,
        "main_assembly": request.main_assembly,
        "selection_sha256": bindings["selection_sha256"],
        "selected_files_sha256": bindings["handoff_sha256"],
        "native_tool": tool,
        "native_tool_sha256": digest(tool),
        "record_sha256": digest(record),
        "record_bytes": len(record_bytes),
        "files": {item["path"]: item["sha256"] for item in members},
    }
    _write_canonical(output / OBSERVATION_MANIFEST, manifest)
    return admit_native_observation(
        output,
        request=request,
        resources_settled=True,
    )


def admit_native_observation(
    observation_root: Path,
    *,
    request: NativeDiscoverRequest,
    artifact_ref=None,
    resources_settled: bool = False,
) -> NativeObservation:
    """Recompute and admit one Windows-sealed native observation."""

    root = Path(observation_root)
    _require(
        root.is_dir() and not root.is_symlink(),
        "discover.observation_invalid",
        "Observation root is not a real directory",
    )
    manifest_path = root / OBSERVATION_MANIFEST
    _require(
        manifest_path.is_file() and not manifest_path.is_symlink(),
        "discover.observation_invalid",
        "Observation manifest is missing",
    )
    manifest = read_data(manifest_path)
    _require(
        isinstance(manifest, dict) and set(manifest) == _OBSERVATION_FIELDS,
        "discover.observation_invalid",
        "Observation manifest fields are invalid",
    )
    _require(
        manifest["schema_version"] == NATIVE_OBSERVATION_SCHEMA,
        "discover.observation_invalid",
        "Observation schema is unsupported",
    )
    _require(
        manifest["request_sha256"] == digest(request.to_record()),
        "discover.observation_mismatch",
        "Observation belongs to another request",
    )
    _require(
        dict(manifest["source_files"]) == dict(request.source_files),
        "discover.observation_mismatch",
        "Observation carries another SourceSet",
    )
    _sha(manifest["source_archive_sha256"], "source_archive_sha256")
    _sha(manifest["request_sha256"], "request_sha256")
    _sha(manifest["source_inventory_sha256"], "source_inventory_sha256")
    _sha(manifest["selection_sha256"], "selection_sha256")
    _sha(manifest["selected_files_sha256"], "selected_files_sha256")
    _sha(manifest["native_tool_sha256"], "native_tool_sha256")
    _sha(manifest["record_sha256"], "record_sha256")
    files = manifest["files"]
    _require(
        isinstance(files, dict) and set(files) == OBSERVATION_MEMBERS,
        "discover.observation_invalid",
        "Observation member set is invalid",
    )
    for name, checksum in files.items():
        path = confined(root, name)
        _require(
            path.is_file() and not path.is_symlink(),
            "discover.observation_invalid",
            "Observation member is missing",
            {"path": name},
        )
        _require(
            file_digest(path) == checksum,
            "discover.observation_invalid",
            "Observation member differs from its digest",
            {"path": name},
        )
    actual = {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()}
    cas_manifest = root / "manifest.json"
    if cas_manifest.is_file() and not cas_manifest.is_symlink():
        try:
            cas_record = read_data(cas_manifest)
        except (PipelineError, OSError, ValueError) as error:
            raise NativeDiscoverRefusal(
                "discover.observation_invalid", "Observation CAS manifest is unreadable"
            ) from error
        if (
            isinstance(cas_record, dict)
            and cas_record.get("schema_version") == "description-pipeline.artifact-manifest/v1"
        ):
            if artifact_ref is not None and cas_record.get("artifact_id") != artifact_ref.artifact_id:
                raise NativeDiscoverRefusal(
                    "discover.observation_mismatch",
                    "Observation CAS manifest names another artifact",
                )
            actual.discard("manifest.json")
    _require(
        actual == OBSERVATION_MEMBERS | {OBSERVATION_MANIFEST},
        "discover.observation_invalid",
        "Observation contains an unregistered member",
    )
    request_record = read_data(root / "request.json")
    _require(
        request_record == request.to_record(),
        "discover.observation_mismatch",
        "Observation request differs from its canonical input",
    )
    source = read_data(root / "source.json")
    _require(
        source.get("source_files") == dict(request.source_files)
        and source.get("source_inventory_sha256") == request.source_inventory_sha256,
        "discover.observation_mismatch",
        "Observation source inventory is inconsistent",
    )
    selection = read_data(root / "selection.json")
    bindings = selection_bindings(selection)
    _require(
        bindings["selection_sha256"] == manifest["selection_sha256"],
        "discover.observation_mismatch",
        "Selection digest is inconsistent",
    )
    _require(
        bindings["handoff_sha256"] == manifest["selected_files_sha256"],
        "discover.observation_mismatch",
        "Selected files digest is inconsistent",
    )
    _require(
        bindings["upload_sha256"] == request.source_inventory_sha256,
        "discover.observation_mismatch",
        "Selection was computed from another full SourceSet",
    )
    tool = read_data(root / "tool.json")
    _validated_tool(tool, request.expected_native_tool_sha256)
    _require(
        manifest["native_tool"] == tool and manifest["native_tool_sha256"] == digest(tool),
        "discover.observation_mismatch",
        "Native tool identity is inconsistent",
    )
    record = read_data(root / "record.json")
    _require(
        isinstance(record, dict) and record.get("schema_version") == DISCOVERY_SCHEMA,
        "discover.observation_invalid",
        "Native record schema is invalid",
    )
    _require(
        record.get("source_selection") == selection,
        "discover.observation_mismatch",
        "Native record carries another selection",
    )
    _require(
        dict(record.get("files") or {}) == dict(bindings["files"]),
        "discover.observation_mismatch",
        "Native record files differ from the selection",
    )
    _require(
        manifest["record_sha256"] == digest(record),
        "discover.observation_mismatch",
        "Native record digest differs from its observation",
    )
    _require(
        manifest["record_bytes"] == len(canonical(record)),
        "discover.observation_mismatch",
        "Native record size differs from its observation",
    )
    return NativeObservation(
        root=root,
        manifest=MappingProxyType(copy.deepcopy(manifest)),
        record=MappingProxyType(copy.deepcopy(record)),
        selection=MappingProxyType(copy.deepcopy(selection)),
        native_tool=MappingProxyType(copy.deepcopy(tool)),
        resources_settled=resources_settled,
    )


__all__ = [
    "NATIVE_OBSERVATION_SCHEMA",
    "NATIVE_REQUEST_SCHEMA",
    "NativeDiscoverRefusal",
    "NativeDiscoverRequest",
    "NativeObservation",
    "admit_native_observation",
    "observe_native_record",
]
