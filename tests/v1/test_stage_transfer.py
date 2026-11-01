"""Transfer seal/admit: complete roundtrip plus tamper rejection; no Windows required."""

from __future__ import annotations

import copy
import hashlib
import json
import ntpath
import shutil
import subprocess
import tempfile
import unittest
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

from packaging.utils import canonicalize_name

from description_pipeline import __version__ as PIPELINE_VERSION
from description_pipeline.build.archive import write_zip
from description_pipeline.delivery import PIPELINE_ID
from description_pipeline.geometry.native_scope import (
    native_scope_identity_sha256,
)
from description_pipeline.geometry.native_tessellation import (
    native_triangle_relative_path,
    write_native_triangle_stream,
)
from description_pipeline.io import (
    PipelineError, canonical, digest, file_digest, inventory, parse_data, read_data, write_json,
)
from description_pipeline.orchestration import stage_transfer as transfer
from description_pipeline.pipeline.contracts import ArtifactRef
from description_pipeline.request_evidence import build_request_evidence
from description_pipeline.runtime import RUNTIME_VERSIONS, required_packages
from description_pipeline.schemas import (
    CAPTURE_EVIDENCE_REQUESTS,
    NATIVE_CAPTURE_OBSERVATION_SCHEMA,
    SOURCE_RECORD_SCHEMA,
)
from description_pipeline import native_transfer
from description_pipeline.interface_records import record_archive_path
from description_pipeline.sources.solidworks.capture_transaction import (
    _archive_input,
)
from description_pipeline.sources.snapshot import write_manifest
from description_pipeline.stages import stage_view as rendered_stage_view

from . import protocol_support

RUN_ID = "20261010-0000-4000-8000-000000000001"
MAIN_ASSEMBLY = "robot.SLDASM"
MAIN_BYTES = b"CAD-BYTES"
NATIVE_FILES = {
    MAIN_ASSEMBLY: hashlib.sha256(MAIN_BYTES).hexdigest(),
}
HANDOFF = digest(NATIVE_FILES)


def native_tool() -> dict:
    return {
        "schema_version": "solidworks-to-urdf.tool/v1",
        "pipeline_id": PIPELINE_ID,
        "version": PIPELINE_VERSION,
        "source_sha256": "f" * 64,
        "release": None,
        "runtime": {
            "role": "native",
            "system": "Windows",
            "python": "3.12.10",
            "machine": "AMD64",
            "packages": {canonicalize_name(name): RUNTIME_VERSIONS[name] for name in required_packages("native")},
        },
    }


def upstream_artifacts() -> list[dict]:
    return [
        ArtifactRef(
            artifact_id=f"{stage}-artifact",
            kind=f"stage_output:{stage}",
            locator=f"cas://artifact/{stage}-artifact",
            manifest_sha256=character * 64,
            set_sha256=character * 64,
            producer_run_id=RUN_ID,
            producer_stage=stage,
            producer_tool_sha256="f" * 64,
        ).to_record()
        for stage, character in (("freeze", "a"), ("discover", "b"))
    ]


def execution() -> dict:
    return {
        "claim_id": "capture-claim",
        "command_id": "capture-command",
        "effect_key": "capture-effect",
        "executor_sha256": "c" * 64,
        "fence": 1,
        "native_tool_sha256": digest(native_tool()),
        "owner_sha256": "d" * 64,
        "run_id": RUN_ID,
    }


def _verify_transfer(path: Path, **kwargs) -> dict | None:
    kwargs.setdefault("expected_upstream_artifacts", upstream_artifacts())
    kwargs.setdefault("expected_execution", execution())
    return native_transfer.verify_transfer(path, **kwargs)


def stage_view(
    *, run_id: str = RUN_ID, handoff: str = HANDOFF, scope=("capture",), events=None
) -> dict:
    """The real rendered receipt (from synthetic protocol events) carrying its raw events list."""

    if events is None:
        events = protocol_support.protocol_events(stages=tuple(scope))
    view = rendered_stage_view(
        {
            "run_id": run_id,
            "request": {"handoff_sha256": handoff},
            "result": {"execution_scope": list(scope)},
            "events": events,
        }
    )
    view["events"] = events
    return view


def _capture_events(
    root: Path,
    *,
    handoff: str,
) -> list[dict]:
    started = datetime(2026, 10, 10, tzinfo=UTC)

    def event(offset: int, stage: str, state: str, check=None):
        row = {
            "at": (started + timedelta(microseconds=offset)).isoformat(),
            "stage": stage,
            "state": state,
        }
        if check is not None:
            row["check"] = check
        return row

    def check(boundary: str, identifier: str, details: dict) -> dict:
        return {
            "boundary": boundary,
            "details": details,
            "id": identifier,
            "state": "passed",
        }

    handoff_record = read_data(root / "input/handoff.json")
    input_inventory = {
        name: checksum
        for name, checksum in inventory(root / "input").items()
        if name not in {"handoff.json", "request.json"}
    }
    evidence_manifest = read_data(root / "evidence/manifest.json")
    prepared_inputs = {
        f"input/{name.removeprefix('cad/')}": checksum
        for name, checksum in input_inventory.items()
    }
    prepared_inputs["input/capture-plan.json"] = file_digest(
        root / "input/capture-plan.json"
    )
    return [
        event(
            0,
            "capture",
            "running",
            check(
                "input",
                "input.valid",
                {
                    "files": {
                        f"input/{name}": checksum
                        for name, checksum in input_inventory.items()
                    },
                    "files_sha256": digest(handoff_record["package_files"]),
                    "handoff_sha256": handoff,
                    "passed": True,
                    "warnings": [],
                },
            ),
        ),
        event(
            1,
            "capture",
            "running",
            check(
                "output",
                "capture.integrity",
                {
                    "files": {
                        "evidence/manifest.json": file_digest(root / "evidence/manifest.json"),
                        **{
                            f"evidence/{name}": checksum
                            for name, checksum in evidence_manifest["files"].items()
                        },
                    },
                    "manifest_sha256": file_digest(root / "evidence/manifest.json"),
                },
            ),
        ),
        event(
            2,
            "capture",
            "running",
            check(
                "output",
                "capture.input_stability",
                {
                    "files": prepared_inputs,
                },
            ),
        ),
        event(3, "capture", "completed"),
    ]


def rewrite_native_evidence_receipts(root: Path, *, handoff: str) -> None:
    """Re-seal the evidence manifest and stage receipt after a fixture mutation."""

    manifest = read_data(root / "evidence/manifest.json")
    write_manifest(
        root / "evidence",
        kind=manifest["kind"],
        identity=manifest["identity"],
        evidence_class=manifest["evidence_class"],
    )
    write_json(
        root / "reports/native-stages.json",
        stage_view(
            handoff=handoff,
            events=_capture_events(root, handoff=handoff),
        ),
    )


def capture_root(
    base: Path,
    *,
    run_id: str = RUN_ID,
    handoff: str | None = None,
    upload: str | None = None,
    upload_files: dict[str, str] | None = None,
    main: str = MAIN_ASSEMBLY,
    extra_native_files: dict[str, bytes] | None = None,
) -> Path:
    root = Path(base) / "capture"
    (root / "input" / "cad").mkdir(parents=True)
    native_contents = {main: MAIN_BYTES, **(extra_native_files or {})}
    if main not in native_contents:
        raise AssertionError("native fixture lacks its main assembly")
    native_files = {}
    for relative, payload in sorted(native_contents.items()):
        target = root / "input" / "cad" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        native_files[relative] = file_digest(target)
    handoff = digest(native_files) if handoff is None else handoff
    upload_files = dict(upload_files or native_files)
    upload = digest(upload_files) if upload is None else upload
    if digest(upload_files) != upload:
        raise AssertionError("upload fixture digest differs from its inventory")
    package_files = {f"cad/{name}": checksum for name, checksum in native_files.items()}
    source_identity = {
        "configuration": "Default",
        "handoff_digest": upload,
        "hardware_id": "fixture",
        "kind": "source-revision",
        "main_assembly": main,
        "records": [],
    }
    write_json(
        root / "input/source.json",
        {
            "schema_version": SOURCE_RECORD_SCHEMA,
            "source_revision_id": digest(source_identity),
            "identity": source_identity,
            "selection": {
                "configuration": "Default",
                "hardware_id": "fixture",
                "hardware_source": "cad",
                "main_assembly": main,
            },
            "metadata": {
                "change_summary": "fixture",
                "change_summary_source": "cad",
                "control": None,
                "owner": None,
                "revision": "r1",
            },
        },
    )
    package_files["source.json"] = file_digest(root / "input/source.json")
    discovery = {
        "schema_version": "solidworks-to-urdf.native-discovery/v2",
        "run_id": run_id,
        "derivation": {
            "main_assembly": main,
            "configuration": "Default",
            "record_roots": [],
        },
        "identity": {
            "delivery_configuration": "Default",
            "hardware_id": "fixture",
            "main_assembly": main,
            "revision": "r1",
            "robot_name": "robot",
        },
        "handoff_sha256": handoff,
        "native_files": native_files,
        "records": [],
        "findings": [],
    }
    write_json(root / "input/discovery/native-discovery.json", discovery)
    write_json(
        root / "input/discovery/findings.json",
        {"schema_version": "solidworks-to-urdf.discovery-findings/v1", "findings": []},
    )
    package_files["discovery/native-discovery.json"] = file_digest(
        root / "input/discovery/native-discovery.json"
    )
    package_files["discovery/findings.json"] = file_digest(
        root / "input/discovery/findings.json"
    )
    write_json(
        root / "input/handoff.json",
        {
            "passed": True,
            "input": {
                "hardware_id": "fixture",
                "source": {
                    "assembly": main,
                    "configuration": "Default",
                },
            },
            "input_receipt": {"inventory": dict(sorted(native_files.items()))},
            "package_files": dict(sorted(package_files.items())),
            "main_assembly": main,
            "configuration": "Default",
        },
    )
    evidence = root / "evidence"
    (evidence / "raw").mkdir(parents=True)
    main_root = ntpath.dirname(main.replace("/", "\\"))
    source_inputs = {}
    for name, checksum in native_files.items():
        normalized = name.replace("/", "\\")
        relative = ntpath.relpath(normalized, main_root) if main_root else normalized
        source_inputs[relative.replace("\\", "/").casefold()] = checksum
    dependency_closure = {
        "configuration": "Default",
        "copy_files": {
            "source/" + name: checksum for name, checksum in native_files.items()
        },
        "mapping": [
            {
                "copy": "source/" + name,
                "copy_sha256": checksum,
                "instance": "top_level" if name == main else name,
                "source": str(root / "input/cad" / name),
                "source_sha256": checksum,
            }
            for name, checksum in native_files.items()
        ],
        "original_files": {
            str(root / "input/cad" / name): checksum
            for name, checksum in native_files.items()
        },
        "top_level": str(root / "input/cad" / main),
    }
    write_json(evidence / "raw/dependency_closure.json", dependency_closure)
    tool = native_tool()
    environment = {
        "captured_at": "2026-10-10T00:00:00Z",
        "platform": "Windows-11",
        "python": "3.12.10",
        "solidworks": {
            "revision": "34.4.1",
            "sessions": {},
        },
        "toolchain": tool,
        "worker_version": tool["version"],
    }
    dependency_inputs = sorted(source_inputs.items())
    identity = {
        "provider": "solidworks",
        "assembly": str(root / "input" / "cad" / main),
        "document_id": str(root / "input" / "cad" / main),
        "configuration": "Default",
        "worker_version": tool["version"],
        "solidworks_revision": "34.4.1",
        "dependency_digest": digest(dependency_inputs),
    }
    write_json(evidence / "native-runtime.json", environment)
    source_scope = {
        "source_document_path": str(root / "input/cad" / main),
        "source_document_identity_kind": "file_sha256",
        "source_document_sha256": native_files[main],
        "source_copy_path": "source/" + main,
        "source_copy_sha256": native_files[main],
        "referenced_configuration": "Default",
        "virtual_owner": None,
        "virtual_owner_sha256": None,
        "virtual_session_temp_root": None,
        "virtual_native_api_pid": None,
    }
    triangle_values = [
        0.0,
        0.0,
        0.0,
        1.0,
        0.0,
        0.0,
        0.0,
        1.0,
        0.0,
    ]
    stream_name = "raw/triangles/fixture.triangles"
    stream = evidence / stream_name
    triangle_count = write_native_triangle_stream(
        stream,
        triangle_values,
    )
    stream_sha256 = file_digest(stream)
    stream_relative = native_triangle_relative_path(stream_sha256)
    canonical_stream = evidence / stream_relative
    canonical_stream.parent.mkdir(parents=True, exist_ok=True)
    stream.replace(canonical_stream)
    transform_raw = [
        1.0,
        0.0,
        0.0,
        0.0,
        1.0,
        0.0,
        0.0,
        0.0,
        1.0,
        0.0,
        0.0,
        0.0,
        1.0,
        0.0,
        0.0,
        0.0,
    ]
    native_raw_mass = {
        "mass_kg": 1.0,
        "center_of_mass_m": [0.0, 0.0, 0.0],
        "inertia_values": [
            0.001,
            0.0,
            0.0,
            0.0,
            0.001,
            0.0,
            0.0,
            0.0,
            0.001,
        ],
        "overrides": {
            "OverrideMass": False,
            "OverrideCenterOfMass": False,
            "OverrideMomentsOfInertia": False,
        },
    }
    mass_read = {
        "native_raw": copy.deepcopy(native_raw_mass),
        "reference": {"scope": "part_document"},
        "read_errors": {},
    }
    link_read = {
        "mass_kg": 1.0,
        "com_m": [0.0, 0.0, 0.0],
        "inertia_values_raw": copy.deepcopy(
            native_raw_mass["inertia_values"]
        ),
        "inertia_values_about_origin_api": None,
        "overrides": copy.deepcopy(native_raw_mass["overrides"]),
        "read_errors": {},
    }
    native_facts = {
        "schema_version": NATIVE_CAPTURE_OBSERVATION_SCHEMA,
        "scene_raw": {
            "document": str(root / "input/cad" / main),
            "configuration": "Default",
            "occurrences": [
                {
                    "name": main,
                    "path": str(root / "input/cad" / main),
                    "transform_raw": transform_raw,
                    "referenced_configuration": "Default",
                    "suppression_state": 2,
                    "is_fixed": True,
                    "document_type": "assembly",
                }
            ],
            "components": [
                {
                    "name": main,
                    "path": str(root / "input/cad" / main),
                    "suppression_state": 2,
                    "is_fixed": True,
                    "document_type": "assembly",
                }
            ],
            "coordinate_systems": {},
        },
        "part_mass_raw": {main: copy.deepcopy(mass_read)},
        "assembly_mass_raw": copy.deepcopy(mass_read),
        "component_mass_raw": {
            "assembly": str(root / "input/cad" / main),
            "configuration": "Default",
            "instances": [
                {
                    "name": main,
                    "parent": None,
                    "depth": 0,
                    "document": str(root / "input/cad" / main),
                    "document_type": "assembly",
                    "context_mass_kg": 1.0,
                    "context_volume_m3": None,
                    "overrides": copy.deepcopy(
                        native_raw_mass["overrides"]
                    ),
                    "read_errors": {},
                }
            ],
            "errors": [],
            "reference": {},
        },
        "link_group_mass_raw": {
            "assembly": str(root / "input/cad" / main),
            "configuration": "Default",
            "used_api": "IMassProperty2 (SelectedItems = component set)",
            "selection_rule": "top_most_providers+uncovered_leaves",
            "links": {
                "body": {
                    "selection": [main],
                    "reads": [
                        copy.deepcopy(link_read),
                        copy.deepcopy(link_read),
                    ],
                }
            },
        },
        "source_scopes": {main: source_scope},
        "geometry_raw": {
            main: {
                "stream_path": stream_relative,
                "stream_sha256": stream_sha256,
                "triangle_count": triangle_count,
                "body_inventory": [],
                "face_inventory": [],
                "body_counts": {"solid": 0, "sheet": 0},
                "used_api": "fixture",
                "source_scope_sha256": native_scope_identity_sha256(
                    source_scope
                ),
                "appearance_raw": None,
            }
        },
        "axis_references_raw": [],
        "document_state": {
            "before": {
                "path": str(root / "input/cad" / main),
                "active_configuration": "Default",
                "read_only": True,
                "save_flag": False,
            },
            "after": {
                "path": str(root / "input/cad" / main),
                "active_configuration": "Default",
                "read_only": True,
                "save_flag": False,
            },
        },
        "input_stability": {
            "originals_unchanged": {
                "files_checked": len(native_files),
            },
            "source_hashes": {
                name: {"sha256": checksum}
                for name, checksum in sorted(source_inputs.items())
            },
        },
        "notes": {},
    }
    write_json(evidence / "raw/native_capture_facts.json.gz", native_facts)
    write_json(
        root / "input/capture-plan.json",
        {
            "schema_version": "solidworks-to-urdf.capture-plan/v1",
            "run_id": run_id,
            "handoff_sha256": handoff,
            "discovery_sha256": file_digest(
                root / "input/discovery/native-discovery.json"
            ),
            "main_assembly": main,
            "configuration": "Default",
            "identity": {"hardware_id": "fixture", "revision": "r1"},
            "selection": {"files": native_files},
            "objects": [
                {
                    "name": main,
                    "document": main,
                    "state": 2,
                }
            ],
            "groups": [
                {"name": "body", "components": [main]}
            ],
            "axis_requests": [],
            "datums": [],
            "evidence_requests": list(CAPTURE_EVIDENCE_REQUESTS),
        },
    )
    handoff_record = read_data(root / "input/handoff.json")
    handoff_record["package_files"]["capture-plan.json"] = file_digest(
        root / "input/capture-plan.json"
    )
    write_json(root / "input/handoff.json", handoff_record)
    write_manifest(
        evidence,
        kind="solidworks",
        identity=identity,
        evidence_class="cad",
    )
    write_json(root / "reports/native-tool.json", tool)
    events = _capture_events(root, handoff=handoff)
    write_json(
        root / "reports/native-stages.json",
        stage_view(run_id=run_id, handoff=handoff, events=events),
    )
    return root


def seal(root: Path, archive: Path, **overrides) -> dict:
    arguments = {
        "run_id": RUN_ID,
        "handoff_sha256": HANDOFF,
        "upload_sha256": HANDOFF,
        "main_assembly": MAIN_ASSEMBLY,
        "native_tool": native_tool(),
        "upstream_artifacts": upstream_artifacts(),
        "execution": execution(),
    }
    arguments.update(overrides)
    return transfer.seal_capture(root, archive, **arguments)


def admit(archive: Path, destination: Path, **overrides) -> dict:
    arguments = {
        "expected_run_id": RUN_ID,
        "expected_handoff_sha256": HANDOFF,
        "expected_upload_sha256": HANDOFF,
        "expected_main_assembly": MAIN_ASSEMBLY,
        "expected_native_tool": native_tool(),
        "expected_upstream_artifacts": upstream_artifacts(),
        "expected_execution": execution(),
    }
    arguments.update(overrides)
    return transfer.admit_capture(archive, destination, **arguments)


def repack(archive: Path, destination: Path, mutate: dict[str, bytes | None], manifest_mutate=None) -> None:
    """Rebuild an archive with a self-consistent manifest after the given member mutations."""

    with zipfile.ZipFile(archive) as source:
        entries = {info.filename: source.read(info) for info in source.infolist()}
    manifest = json.loads(entries.pop(transfer.CAPTURE_MANIFEST))
    for name, payload in mutate.items():
        if payload is None:
            entries.pop(name, None)
        else:
            entries[name] = payload
    if manifest_mutate is not None:
        manifest = manifest_mutate(manifest)
    manifest["files"] = {name: hashlib.sha256(payload).hexdigest() for name, payload in entries.items()}
    manifest["file_count"] = len(entries)
    manifest["total_bytes"] = sum(len(payload) for payload in entries.values())
    entries[transfer.CAPTURE_MANIFEST] = canonical(manifest)
    write_zip(destination, sorted(entries.items()))


def raw_zip(source: Path, destination: Path, extra: list[tuple[zipfile.ZipInfo, bytes]]) -> None:
    """Copy an archive and append raw members with crafted metadata."""

    with zipfile.ZipFile(source) as archive:
        entries = [(info.filename, archive.read(info)) for info in archive.infolist()]
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in entries:
            archive.writestr(zipfile.ZipInfo(name), payload)
        for info, payload in extra:
            archive.writestr(info, payload)


def raw_copy(
    source: Path,
    destination: Path,
    *,
    remove: tuple[str, ...] = (),
    replace: dict[str, bytes] | None = None,
    add: dict[str, bytes] | None = None,
) -> None:
    """Copy an archive with the manifest bytes untouched, so member edits stay inconsistent."""

    replace = replace or {}
    add = add or {}
    with zipfile.ZipFile(source) as archive:
        entries = [(info.filename, archive.read(info)) for info in archive.infolist()]
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in entries:
            if name in remove:
                continue
            archive.writestr(zipfile.ZipInfo(name), replace.get(name, payload))
        for name, payload in add.items():
            archive.writestr(zipfile.ZipInfo(name), payload)


class StageTransferTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="stage-transfer-")
        self.base = Path(self._temporary.name)
        self.root = capture_root(self.base)
        self.archive = self.base / transfer.CAPTURE_ARCHIVE

    def tearDown(self) -> None:
        self._temporary.cleanup()

    # ------------------------------------------------------------------ roundtrip

    def test_complete_roundtrip_installs_the_closed_capture(self) -> None:
        manifest = seal(self.root, self.archive)
        self.assertEqual(
            manifest["schema_version"],
            native_transfer.CAPTURE_TRANSFER_SCHEMA,
        )
        self.assertEqual(manifest["run_id"], RUN_ID)
        self.assertEqual(manifest["file_count"], len(manifest["files"]))
        self.assertEqual(manifest["upstream_artifacts"], upstream_artifacts())
        self.assertEqual(manifest["execution"], execution())
        self.assertNotIn("native_stage_scope", manifest)
        self.assertEqual(manifest["main_assembly_sha256"], manifest["files"]["input/cad/" + MAIN_ASSEMBLY])

    def test_nested_duplicate_basenames_keep_complete_source_and_input_paths(self) -> None:
        root = capture_root(
            self.base / "nested-duplicate-basenames",
            main="cad/robot.SLDASM",
            extra_native_files={
                "cad/left/part.SLDPRT": b"LEFT-PART",
                "cad/right/part.SLDPRT": b"RIGHT-PART",
            },
        )
        native_files = read_data(
            root / "input/discovery/native-discovery.json"
        )["native_files"]
        handoff = digest(native_files)
        source_hashes = read_data(
            root / "evidence/raw/native_capture_facts.json.gz"
        )["input_stability"]["source_hashes"]
        self.assertEqual(
            set(source_hashes),
            {"robot.sldasm", "left/part.sldprt", "right/part.sldprt"},
        )

        producer_events = read_data(root / "reports/native-stages.json")["events"]
        producer_checks = {
            event["check"]["id"]: event["check"]["details"]
            for event in producer_events
            if isinstance(event.get("check"), dict)
        }
        self.assertEqual(
            set(producer_checks["capture.input_stability"]["files"]),
            {
                "input/cad/left/part.SLDPRT",
                "input/cad/right/part.SLDPRT",
                "input/cad/robot.SLDASM",
                "input/capture-plan.json",
                "input/discovery/findings.json",
                "input/discovery/native-discovery.json",
                "input/source.json",
            },
        )
        integrity_files = set(
            producer_checks["capture.integrity"]["files"]
        )
        self.assertTrue(
            {
                "evidence/manifest.json",
                "evidence/native-runtime.json",
                "evidence/raw/dependency_closure.json",
                "evidence/raw/native_capture_facts.json.gz",
            }
            <= integrity_files
        )
        self.assertTrue(
            any(
                name.startswith("evidence/raw/triangles/")
                for name in integrity_files
            )
        )
        self.assertFalse(
            integrity_files
            & {
                "evidence/capture-plan.json",
                "evidence/capture.json",
                "evidence/discovery.json",
                "evidence/geometry/base.stl",
            }
        )

        archive = self.base / "nested-duplicate-basenames.zip"
        seal(
            root,
            archive,
            handoff_sha256=handoff,
            upload_sha256=handoff,
            main_assembly="cad/robot.SLDASM",
        )
        destination = self.base / "nested-duplicate-basenames-out"
        admit(
            archive,
            destination,
            expected_handoff_sha256=handoff,
            expected_upload_sha256=handoff,
            expected_main_assembly="cad/robot.SLDASM",
        )
        admitted_events = read_data(destination / "reports/native-stages.json")["events"]
        self.assertEqual(admitted_events, producer_events)

    def test_input_stability_rejects_a_missing_capture_plan(self) -> None:
        missing = capture_root(self.base / "missing-capture-plan")
        (missing / "input/capture-plan.json").unlink()
        with self.assertRaisesRegex(PipelineError, "capture-plan.json"):
            seal(missing, self.base / "missing-capture-plan.zip")

    def _assert_input_stability_rejected(
        self,
        label: str,
        mutate,
        message: str,
    ) -> None:
        base = self.base / label
        root = capture_root(
            base,
            extra_native_files={"part.SLDPRT": b"PART"},
        )
        native_files = read_data(
            root / "input/discovery/native-discovery.json"
        )["native_files"]
        handoff = digest(native_files)
        valid = base / "valid.zip"
        seal(
            root,
            valid,
            handoff_sha256=handoff,
            upload_sha256=handoff,
        )
        facts_path = root / "evidence/raw/native_capture_facts.json.gz"
        facts = read_data(facts_path)
        mutate(facts["input_stability"])
        write_json(facts_path, facts)
        rewrite_native_evidence_receipts(root, handoff=handoff)

        with self.assertRaisesRegex(PipelineError, message):
            seal(
                root,
                base / "forged-root.zip",
                handoff_sha256=handoff,
                upload_sha256=handoff,
            )

        forged = base / "forged.zip"
        repack(
            valid,
            forged,
            {
                "evidence/raw/native_capture_facts.json.gz": facts_path.read_bytes(),
                "evidence/manifest.json": (
                    root / "evidence/manifest.json"
                ).read_bytes(),
                "reports/native-stages.json": (
                    root / "reports/native-stages.json"
                ).read_bytes(),
            },
        )
        with self.assertRaisesRegex(PipelineError, message):
            admit(
                forged,
                base / "admitted",
                expected_handoff_sha256=handoff,
                expected_upload_sha256=handoff,
            )

    def test_input_stability_count_matches_complete_source_inventory(self) -> None:
        base = self.base / "complete-source-inventory"
        root = capture_root(
            base,
            extra_native_files={"part.SLDPRT": b"PART"},
        )
        native_files = read_data(
            root / "input/discovery/native-discovery.json"
        )["native_files"]
        facts = read_data(
            root / "evidence/raw/native_capture_facts.json.gz"
        )
        self.assertEqual(len(native_files), 2)
        self.assertEqual(
            len(facts["input_stability"]["source_hashes"]),
            len(native_files),
        )
        self.assertEqual(
            facts["input_stability"]["originals_unchanged"],
            {"files_checked": len(native_files)},
        )
        handoff = digest(native_files)
        archive = base / "complete-source-inventory.zip"
        seal(
            root,
            archive,
            handoff_sha256=handoff,
            upload_sha256=handoff,
        )
        admit(
            archive,
            base / "admitted",
            expected_handoff_sha256=handoff,
            expected_upload_sha256=handoff,
        )

    def test_input_stability_rejects_forged_count_at_seal_and_admit(self) -> None:
        for label, forged, message in (
            ("underreported", 1, "files_checked.*complete native source inventory"),
            ("overreported", 3, "files_checked.*complete native source inventory"),
            ("boolean", True, "files_checked must be a positive integer"),
            ("string", "2", "files_checked must be a positive integer"),
            ("float", 2.0, "files_checked must be a positive integer"),
        ):
            with self.subTest(label=label):
                self._assert_input_stability_rejected(
                    f"count-{label}",
                    lambda stability, value=forged: stability[
                        "originals_unchanged"
                    ].__setitem__("files_checked", value),
                    message,
                )

    def test_input_stability_rejects_unknown_or_missing_fields(self) -> None:
        cases = (
            (
                "unknown-stability-field",
                lambda stability: stability.__setitem__("extra", True),
            ),
            (
                "unknown-unchanged-field",
                lambda stability: stability["originals_unchanged"].__setitem__(
                    "extra",
                    True,
                ),
            ),
            (
                "missing-source-hashes",
                lambda stability: stability.pop("source_hashes"),
            ),
            (
                "missing-files-checked",
                lambda stability: stability["originals_unchanged"].pop(
                    "files_checked"
                ),
            ),
        )
        for label, mutate in cases:
            with self.subTest(label=label):
                self._assert_input_stability_rejected(
                    label,
                    mutate,
                    "input stability fields are incomplete or unknown",
                )

    def test_transfer_requires_exact_upstream_and_execution_bindings(self) -> None:
        seal(self.root, self.archive)
        destination = self.base / "bound"
        admit(self.archive, destination)

        wrong_upstream = copy.deepcopy(upstream_artifacts())
        wrong_upstream[0]["producer_run_id"] = (
            "20261010-0000-4000-8000-000000000099"
        )
        with self.assertRaisesRegex(PipelineError, "different upstream"):
            native_transfer.verify_transfer(
                destination,
                expected_upstream_artifacts=wrong_upstream,
                expected_execution=execution(),
            )

        wrong_execution = execution()
        wrong_execution["owner_sha256"] = "e" * 64
        with self.assertRaisesRegex(PipelineError, "another execution"):
            native_transfer.verify_transfer(
                destination,
                expected_upstream_artifacts=upstream_artifacts(),
                expected_execution=wrong_execution,
            )

        wrong_kind = copy.deepcopy(upstream_artifacts())
        wrong_kind[0]["kind"] = "not-freeze"
        wrong_kind[1]["kind"] = "not-discover"
        with self.assertRaisesRegex(PipelineError, "invalid stage-output kinds"):
            seal(
                self.root,
                self.base / "wrong-upstream-kind.zip",
                upstream_artifacts=wrong_kind,
            )

    def test_archive_and_in_memory_receipts_use_the_same_manifest_verifier(self) -> None:
        seal(self.root, self.archive)
        with mock.patch.object(native_transfer, "verify_transfer", wraps=native_transfer.verify_transfer) as verify:
            admit(self.archive, self.base / "public-api")
        self.assertEqual(verify.call_count, 1)
        for label, mutation in (
            ("unknown", lambda manifest: {**manifest, "unexpected_field": True}),
            ("execution", lambda manifest: {**manifest, "execution": None}),
        ):
            with self.subTest(case=label):
                broken = self.base / f"manifest-{label}.zip"
                repack(self.archive, broken, {}, manifest_mutate=mutation)
                destination = self.base / f"reject-{label}"
                with self.assertRaises(PipelineError):
                    admit(broken, destination)
                self.assertFalse(destination.exists())
                with zipfile.ZipFile(broken) as archive:
                    receipts = {
                        "native_tool_bytes": archive.read("reports/native-tool.json"),
                        "native_stages_bytes": archive.read("reports/native-stages.json"),
                        "transfer_manifest_bytes": archive.read(transfer.CAPTURE_MANIFEST),
                    }
                with (
                    mock.patch.object(
                        native_transfer, "verify_transfer", wraps=native_transfer.verify_transfer,
                    ) as verify,
                    self.assertRaises(PipelineError),
                ):
                    native_transfer.validate_native_receipts(self.root, receipts)
                self.assertEqual(verify.call_count, 1)

    def test_complete_roundtrip_preserves_every_closed_member(self) -> None:
        manifest = seal(self.root, self.archive)

        destination = self.base / "admitted"
        admitted = admit(self.archive, destination)
        self.assertEqual(admitted, manifest)
        installed = {path.relative_to(destination).as_posix() for path in destination.rglob("*") if path.is_file()}
        self.assertEqual(installed, set(manifest["files"]) | {transfer.CAPTURE_MANIFEST})
        for name in ("reports/structural-team.md", "reports/structural-team.json"):
            self.assertNotIn(name, manifest["files"])
        self.assertEqual(
            file_digest(destination / "reports/native-tool.json"),
            manifest["files"]["reports/native-tool.json"],
        )
        with zipfile.ZipFile(self.archive) as archive:
            sealed = archive.read(transfer.CAPTURE_MANIFEST)
        self.assertEqual((destination / transfer.CAPTURE_MANIFEST).read_bytes(), sealed)

    def test_pruned_input_cannot_be_sealed_as_the_complete_upload(self) -> None:
        upload_files = {**NATIVE_FILES, "support.SLDPRT": "e" * 64}
        upload_sha256 = digest(upload_files)
        root = capture_root(
            self.base / "pruned-input", upload=upload_sha256, upload_files=upload_files,
        )
        with self.assertRaisesRegex(PipelineError, "complete frozen upload"):
            seal(root, self.base / "pruned-input.zip", upload_sha256=upload_sha256)

    def test_structural_team_reports_are_not_capture_payload(self) -> None:
        (self.root / "reports/structural-team.md").write_text(
            "# generated\n",
            encoding="utf-8",
        )
        (self.root / "reports/structural-team.json").write_text(
            '{"findings": []}\n',
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            PipelineError,
            "outside the transfer payload",
        ):
            seal(self.root, self.archive)

    def test_unknown_success_evidence_members_are_not_capture_payload(
        self,
    ) -> None:
        evidence = self.root / "evidence"
        write_json(evidence / "discovery.json", {"legacy": True})
        manifest = read_data(evidence / "manifest.json")
        write_manifest(
            evidence,
            kind=manifest["kind"],
            identity=manifest["identity"],
            evidence_class=manifest["evidence_class"],
        )
        with self.assertRaisesRegex(
            PipelineError,
            "outside the transfer payload",
        ):
            seal(self.root, self.archive)

    def test_noncanonical_triangle_evidence_name_is_not_capture_payload(
        self,
    ) -> None:
        evidence = self.root / "evidence"
        triangle = evidence / "raw/triangles/not-a-content-hash.triangles"
        triangle.parent.mkdir(parents=True, exist_ok=True)
        triangle.write_bytes(b"not canonical")
        manifest = read_data(evidence / "manifest.json")
        write_manifest(
            evidence,
            kind=manifest["kind"],
            identity=manifest["identity"],
            evidence_class=manifest["evidence_class"],
        )
        with self.assertRaisesRegex(
            PipelineError,
            "outside the transfer payload",
        ):
            seal(self.root, self.archive)

    def test_unreferenced_canonical_triangle_is_not_capture_payload(
        self,
    ) -> None:
        seal(self.root, self.archive)
        content = b"unreferenced triangle stream"
        checksum = hashlib.sha256(content).hexdigest()
        relative = f"raw/triangles/{checksum}.triangles"
        with zipfile.ZipFile(self.archive) as source:
            evidence_manifest = json.loads(
                source.read("evidence/manifest.json")
            )
        evidence_manifest["files"][relative] = checksum
        broken = self.base / "unreferenced-triangle.zip"
        repack(
            self.archive,
            broken,
            {
                "evidence/" + relative: content,
                "evidence/manifest.json": canonical(evidence_manifest),
            },
        )
        with self.assertRaisesRegex(
            PipelineError,
            "triangle inventory differs",
        ):
            admit(broken, self.base / "out-unreferenced-triangle")

    def test_sealing_is_deterministic(self) -> None:
        first = self.base / "first.zip"
        second = self.base / "second.zip"
        seal(self.root, first)
        seal(self.root, second)
        self.assertEqual(first.read_bytes(), second.read_bytes())

    def test_seal_ignores_its_own_outputs_and_rejects_stray_files(self) -> None:
        inside = self.root / transfer.CAPTURE_ARCHIVE
        manifest = seal(self.root, inside)
        self.assertTrue(inside.is_file())
        self.assertNotIn(transfer.CAPTURE_ARCHIVE, manifest["files"])

        with_sidecar = capture_root(self.base / "sidecar")
        write_json(with_sidecar / transfer.CAPTURE_MANIFEST, {"stale": True})
        manifest = seal(with_sidecar, self.base / "sidecar.zip")
        self.assertNotIn(transfer.CAPTURE_MANIFEST, manifest["files"])
        self.assertIn("reports/native-stages.json", manifest["files"])

        stray = capture_root(self.base / "stray")
        (stray / "stray.txt").write_bytes(b"stray")
        with self.assertRaisesRegex(PipelineError, "outside the transfer payload"):
            seal(stray, self.base / "stray.zip")

    def test_existing_archive_or_destination_is_refused(self) -> None:
        seal(self.root, self.archive)
        with self.assertRaises(PipelineError):
            seal(self.root, self.archive)
        destination = self.base / "admitted"
        destination.mkdir()
        with self.assertRaises(PipelineError):
            admit(self.archive, destination)

    # ------------------------------------------------------------------ archive tamper

    def test_missing_or_extra_members_are_refused(self) -> None:
        seal(self.root, self.archive)
        for label, arguments in (
            ("missing", {"remove": ("reports/native-stages.json",)}),
            ("extra", {"add": {"reports/extra.json": b"{}"}}),
        ):
            with self.subTest(case=label):
                broken = self.base / f"{label}.zip"
                raw_copy(self.archive, broken, **arguments)
                with self.assertRaisesRegex(PipelineError, "inventory differs|provenance is incomplete"):
                    admit(broken, self.base / f"out-{label}")

    def test_edited_or_replaced_member_bytes_are_refused(self) -> None:
        seal(self.root, self.archive)
        with zipfile.ZipFile(self.archive) as archive:
            original = archive.read("input/handoff.json")
        broken = self.base / "edited.zip"
        # Same length so the size invariant still holds and the content hash is what rejects it.
        raw_copy(self.archive, broken, replace={"input/handoff.json": b"X" * len(original)})
        with self.assertRaisesRegex(PipelineError, "differs from the transfer manifest"):
            admit(broken, self.base / "out-edited")

    def test_self_consistent_but_incomplete_stage_receipts_are_refused(self) -> None:
        seal(self.root, self.archive)
        view = stage_view()
        for stage in view["stages"]:
            if stage["id"] == "capture":
                stage["checks_passed"] = 0
        broken = self.base / "stages.zip"
        repack(self.archive, broken, {"reports/native-stages.json": canonical(view)})
        with self.assertRaisesRegex(PipelineError, "receipts are incomplete"):
            admit(broken, self.base / "out-stages")

    def test_self_consistent_stray_member_is_refused(self) -> None:
        seal(self.root, self.archive)
        broken = self.base / "stray-member.zip"
        repack(self.archive, broken, {"reports/extra.json": b"{}"})
        with self.assertRaisesRegex(PipelineError, "outside the capture payload"):
            admit(broken, self.base / "out-stray-member")

    def test_self_consistent_evidence_tamper_is_refused(self) -> None:
        seal(self.root, self.archive)
        with zipfile.ZipFile(self.archive) as source:
            facts = parse_data(
                source.read("evidence/raw/native_capture_facts.json.gz"),
                Path("evidence/raw/native_capture_facts.json.gz"),
            )
        facts["input_stability"]["originals_unchanged"] = False
        mutated_facts = self.base / "mutated-native-capture-facts.json.gz"
        write_json(mutated_facts, facts)
        payload = mutated_facts.read_bytes()
        with zipfile.ZipFile(self.archive) as source:
            evidence_manifest = json.loads(
                source.read("evidence/manifest.json")
            )
        evidence_manifest["files"]["raw/native_capture_facts.json.gz"] = hashlib.sha256(
            payload
        ).hexdigest()
        broken = self.base / "evidence.zip"
        repack(
            self.archive,
            broken,
            {
                "evidence/raw/native_capture_facts.json.gz": payload,
                "evidence/manifest.json": canonical(evidence_manifest),
            },
        )
        with self.assertRaisesRegex(PipelineError, "CAD bytes were unchanged"):
            admit(broken, self.base / "out-evidence")

    def test_self_consistent_input_report_tamper_is_refused(self) -> None:
        seal(self.root, self.archive)
        with zipfile.ZipFile(self.archive) as source:
            report = json.loads(source.read("input/handoff.json"))
        report["passed"] = False
        broken = self.base / "input.zip"
        repack(self.archive, broken, {"input/handoff.json": canonical(report)})
        with self.assertRaisesRegex(PipelineError, "Input report did not pass"):
            admit(broken, self.base / "out-input")

    def test_self_consistent_main_assembly_hash_tamper_is_refused(self) -> None:
        seal(self.root, self.archive)
        replacement = b"OTHER-BYTES"
        with zipfile.ZipFile(self.archive) as source:
            report = json.loads(source.read("input/handoff.json"))
        report["input_receipt"]["inventory"][MAIN_ASSEMBLY] = hashlib.sha256(replacement).hexdigest()
        broken = self.base / "main.zip"
        repack(
            self.archive,
            broken,
            {
                "input/cad/" + MAIN_ASSEMBLY: replacement,
                "input/handoff.json": canonical(report),
            },
        )
        # The archive is fully self-consistent; only the native discovery inventory still names
        # the original bytes, so the main-assembly binding must refuse it.
        with self.assertRaisesRegex(PipelineError, "Native discovery inventory"):
            admit(broken, self.base / "out-main")

    def test_native_discovery_binding_is_enforced(self) -> None:
        seal(self.root, self.archive)
        with zipfile.ZipFile(self.archive) as source:
            record = json.loads(
                source.read("input/discovery/native-discovery.json")
            )
            plan = json.loads(source.read("input/capture-plan.json"))

        renamed = {**record, "identity": {**record["identity"], "main_assembly": "other.SLDASM"}}
        broken = self.base / "discovery-name.zip"
        repack(
            self.archive,
            broken,
            {
                "input/discovery/native-discovery.json": canonical(renamed),
            },
        )
        with self.assertRaisesRegex(PipelineError, "opened another main assembly"):
            admit(broken, self.base / "out-discovery-name")

        rebased = {**record, "handoff_sha256": "b" * 64}
        broken = self.base / "discovery-handoff.zip"
        repack(
            self.archive,
            broken,
            {
                "input/discovery/native-discovery.json": canonical(rebased),
            },
        )
        with self.assertRaisesRegex(PipelineError, "bound to another handoff"):
            admit(broken, self.base / "out-discovery-handoff")

        foreign_plan = copy.deepcopy(plan)
        foreign_plan["identity"]["hardware_id"] = "foreign-hardware"
        broken = self.base / "plan-identity.zip"
        repack(
            self.archive,
            broken,
            {
                "input/capture-plan.json": canonical(foreign_plan),
            },
        )
        with self.assertRaisesRegex(PipelineError, "plan identity differs"):
            admit(broken, self.base / "out-plan-identity")

    def test_structural_zip_tamper_is_refused(self) -> None:
        seal(self.root, self.archive)
        duplicate = zipfile.ZipInfo("Input/Handoff.json")
        duplicate.create_system = 3
        duplicate.external_attr = 0o644 << 16
        link = zipfile.ZipInfo("reports/link.json")
        link.create_system = 3
        link.external_attr = 0o120777 << 16
        cases = (
            ("case-duplicate", duplicate, b"{}", "Duplicate transfer member"),
            ("symlink", link, b"{}", "not a regular file"),
            ("directory", zipfile.ZipInfo("reports/"), b"", "directory"),
        )
        for label, info, payload, expected in cases:
            with self.subTest(case=label):
                broken = self.base / f"{label}.zip"
                raw_zip(self.archive, broken, [(info, payload)])
                with self.assertRaisesRegex(PipelineError, expected):
                    admit(broken, self.base / f"out-{label}")

    def test_traversal_and_nonportable_member_names_are_refused(self) -> None:
        seal(self.root, self.archive)
        for label, name in (("traversal", "../escape.txt"), ("nonportable", "reports/aux<>.json")):
            with self.subTest(case=label):
                broken = self.base / f"{label}.zip"
                raw_zip(self.archive, broken, [(zipfile.ZipInfo(name), b"x")])
                with self.assertRaises(PipelineError):
                    admit(broken, self.base / f"out-{label}")

    def test_missing_manifest_is_refused(self) -> None:
        seal(self.root, self.archive)
        broken = self.base / "no-manifest.zip"
        raw_copy(self.archive, broken, remove=(transfer.CAPTURE_MANIFEST,))
        with self.assertRaisesRegex(PipelineError, "transfer-manifest"):
            admit(broken, self.base / "out-manifest")

    def test_size_and_file_limits_are_enforced(self) -> None:
        with (
            mock.patch.object(native_transfer, "MAX_TRANSFER_FILES", 1),
            self.assertRaisesRegex(PipelineError, "file limit"),
        ):
            seal(self.root, self.base / "limit.zip")
        seal(self.root, self.archive)
        with mock.patch.object(transfer, "MAX_TRANSFER_BYTES", 8), self.assertRaisesRegex(PipelineError, "size limit"):
            admit(self.archive, self.base / "out-limit")

    # ------------------------------------------------------------------ identity tamper

    def test_foreign_identity_is_refused(self) -> None:
        seal(self.root, self.archive)
        cases = (
            ("run", {"expected_run_id": "other-run"}, "another run"),
            ("handoff", {"expected_handoff_sha256": "b" * 64}, "another handoff"),
            ("assembly", {"expected_main_assembly": "other.SLDASM"}, "another main assembly"),
            (
                "tool",
                {"expected_native_tool": {**native_tool(), "source_sha256": "e" * 64}},
                "another native tool",
            ),
        )
        for label, overrides, expected in cases:
            with self.subTest(case=label), self.assertRaisesRegex(PipelineError, expected):
                admit(self.archive, self.base / f"out-{label}", **overrides)

    def test_seal_rejects_inconsistent_capture_roots(self) -> None:
        cases = []

        input_report = json.loads((self.root / "input/handoff.json").read_text(encoding="utf-8"))
        input_report["passed"] = False
        cases.append(("input-failed", input_report, "input/handoff.json", "Input report did not pass"))

        portable = {
            **native_tool(),
            "runtime": {**native_tool()["runtime"], "role": "portable"},
        }
        cases.append(("portable-tool", portable, "reports/native-tool.json", "runtime.role", portable))

        wide_scope = stage_view(scope=("capture", "generate"))
        cases.append(("wide-scope", wide_scope, "reports/native-stages.json", "capture execution scope"))

        failed_check = stage_view()
        failed_check["stages"][2]["output_qc"][0]["state"] = "failed"
        failed_check["stages"][2]["checks_passed"] = failed_check["stages"][2]["checks_total"] - 1
        cases.append(("failed-check", failed_check, "reports/native-stages.json", "failed or unrun"))

        for case in cases:
            label, payload, target, expected = case[:4]
            tool = case[4] if len(case) > 4 else None
            with self.subTest(case=label):
                root = capture_root(self.base / label)
                write_json(root / target, payload)
                with self.assertRaisesRegex(PipelineError, expected):
                    seal(root, self.base / f"{label}.zip", **({"native_tool": tool} if tool else {}))

    def test_seal_rejects_changed_archived_input(self) -> None:
        (self.root / "input" / "cad" / MAIN_ASSEMBLY).write_bytes(b"CHANGED")
        with self.assertRaisesRegex(PipelineError, "Archived input differs"):
            seal(self.root, self.archive)

    def test_seal_rejects_missing_reports(self) -> None:
        (self.root / "reports/native-tool.json").unlink()
        with self.assertRaisesRegex(PipelineError, "lacks required report"):
            seal(self.root, self.archive)

    def test_admission_refuses_foreign_archive_layout(self) -> None:
        with self.assertRaisesRegex(PipelineError, "missing"):
            admit(self.base / "absent.zip", self.base / "out-absent")

    def test_live_receipts_are_ignored_at_seal_and_stray_receipts_are_refused(self) -> None:
        write_json(self.root / "reports/stages.json", stage_view())
        manifest = seal(self.root, self.archive)
        self.assertNotIn("reports/stages.json", manifest["files"])
        self.assertIn("reports/native-stages.json", manifest["files"])

        # A stray legacy receipt is an unknown member now: the closed payload
        # inventory refuses it instead of silently ignoring it.
        write_json(self.root / "reports/run.json", {"run_id": RUN_ID})
        with self.assertRaisesRegex(PipelineError, "outside the transfer payload"):
            seal(self.root, self.base / "stray-receipt.zip")

        broken = self.base / "live.zip"
        repack(self.archive, broken, {"reports/run.json": b"{}"})
        with self.assertRaisesRegex(PipelineError, "outside the capture payload"):
            admit(broken, self.base / "out-live")

    def test_streaming_large_member_roundtrips(self) -> None:
        payload = b"x" * (4 * 1024 * 1024)
        (self.root / "input/big.bin").write_bytes(payload)
        report = json.loads((self.root / "input/handoff.json").read_text(encoding="utf-8"))
        report["package_files"]["big.bin"] = hashlib.sha256(payload).hexdigest()
        write_json(self.root / "input/handoff.json", report)
        events = _capture_events(self.root, handoff=HANDOFF)
        write_json(
            self.root / "reports/native-stages.json",
            stage_view(run_id=RUN_ID, handoff=HANDOFF, events=events),
        )

        manifest = seal(self.root, self.archive)
        self.assertEqual(manifest["files"]["input/big.bin"], hashlib.sha256(payload).hexdigest())
        destination = self.base / "out-big"
        admit(self.archive, destination)
        self.assertEqual(file_digest(destination / "input/big.bin"), hashlib.sha256(payload).hexdigest())

    def test_stage_receipts_must_match_the_contract_checks_exactly(self) -> None:
        seal(self.root, self.archive)
        view = stage_view()
        capture = next(stage for stage in view["stages"] if stage["id"] == "capture")
        capture["input_qc"][0] = {"id": "input.arbitrary", "state": "passed", "details": {}}
        broken = self.base / "wrong-checks.zip"
        repack(self.archive, broken, {"reports/native-stages.json": canonical(view)})
        with self.assertRaisesRegex(PipelineError, "differ from the release contract"):
            admit(broken, self.base / "out-wrong-checks")

    def test_native_tool_record_must_pin_its_release_and_runtime(self) -> None:
        cases = (
            ("schema", {"schema_version": "other/v1"}, "unknown schema"),
            ("release", {"version": "9.9.9"}, "another release"),
            ("system", {"runtime": {**native_tool()["runtime"], "system": "Linux"}}, "Windows host"),
            ("python", {"runtime": {**native_tool()["runtime"], "python": "3.11.9"}}, "Python 3.12"),
            (
                "python-decimal",
                {"runtime": {**native_tool()["runtime"], "python": "3.120"}},
                "Python 3.12",
            ),
            (
                "python-garbage",
                {"runtime": {**native_tool()["runtime"], "python": "3.12garbage"}},
                "Python 3.12",
            ),
            (
                "python-number",
                {"runtime": {**native_tool()["runtime"], "python": 3.12}},
                "Python 3.12",
            ),
            (
                "python-prerelease",
                {"runtime": {**native_tool()["runtime"], "python": "3.12.0rc1"}},
                "Python 3.12",
            ),
            (
                "pin",
                {
                    "runtime": {
                        **native_tool()["runtime"],
                        "packages": {required_packages("native")[0]: "0.0.1"},
                    }
                },
                "exactly the native package closure",
            ),
            (
                "mujoco",
                {
                    "runtime": {
                        **native_tool()["runtime"],
                        "packages": {**native_tool()["runtime"]["packages"], "mujoco": "3.13.0"},
                    }
                },
                "exactly the native package closure",
            ),
            (
                "runtime-extra",
                {
                    "runtime": {
                        **native_tool()["runtime"],
                        "execution_variant": "unexpected",
                    }
                },
                "exact runtime closure",
            ),
            (
                "package-extra",
                {
                    "runtime": {
                        **native_tool()["runtime"],
                        "packages": {
                            **native_tool()["runtime"]["packages"],
                            "unexpected-runtime": "1.0.0",
                        },
                    }
                },
                "exactly the native package closure",
            ),
            (
                "machine",
                {
                    "runtime": {
                        **native_tool()["runtime"],
                        "machine": "ARM64",
                    }
                },
                "Windows x86_64",
            ),
            (
                "tool-extra",
                {"unexpected": True},
                "exact fields",
            ),
            (
                "unknown-release",
                {"release": {"schema_version": "unexpected-release/v1"}},
                "release",
            ),
        )
        for label, overrides, expected in cases:
            with self.subTest(case=label):
                root = capture_root(self.base / label)
                tool = {**native_tool(), **overrides}
                write_json(root / "reports/native-tool.json", tool)
                with self.assertRaisesRegex(PipelineError, expected):
                    seal(root, self.base / f"tool-{label}.zip", native_tool=tool)

    def test_installed_native_tool_rejects_top_level_source_drift(self) -> None:
        with tempfile.TemporaryDirectory(prefix="installed-native-tool-") as directory:
            package = Path(directory)
            package_files = {"worker.py": "a" * 64}
            source_sha256 = digest(package_files)
            identity = {
                "schema_version": "solidworks-to-urdf.release/v1",
                "pipeline_id": PIPELINE_ID,
                "version": PIPELINE_VERSION,
                "source_commit": "b" * 40,
                "source_tree": "c" * 40,
                "source_sha256": source_sha256,
                "package_files": package_files,
                "contract": {},
                "support": {},
            }
            write_json(package / "tool-release.json", identity)
            tool = {**native_tool(), "source_sha256": source_sha256, "release": identity}
            with mock.patch.object(
                native_transfer,
                "__file__",
                str(package / "native_transfer.py"),
            ):
                native_transfer._validate_native_tool(tool)
                drifted = {**tool, "source_sha256": "e" * 64}
                with self.assertRaisesRegex(PipelineError, "release_invalid"):
                    native_transfer._validate_native_tool(drifted)

    def test_neutral_freeze_capture_roundtrip(self) -> None:
        """The evidence identity is validated against a real freeze snapshot, not a hand shape."""

        from tests.sources import support

        work = self.base / "freeze"
        try:
            assembly = support.make_cad_tree(work / "cad")
            backend = support.FixtureCadBackend(
                assembly,
                [
                    {
                        "name": "base-1",
                        "transform": support.placement((0.0, 0.0, 0.0)),
                        # Off-axis COM: the product signal proves the convention at capture time.
                        "mass": support.mass_payload(1.0, (0.01, 0.02, 0.0)),
                    },
                    {
                        "name": "arm-1",
                        "transform": support.placement((0.0, 0.0, 0.2)),
                        "mass": support.mass_payload(0.5, (0.0, 0.0, 0.05)),
                    },
                ],
                dependencies=[work / "cad/base.SLDPRT", work / "cad/arm.SLDPRT"],
            )
            backend.evidence_class = "cad"
            config = {
                "provider": "solidworks",
                "assembly": str(assembly),
                "configuration": "Default",
                "allowed_roots": [str(work / "cad")],
                "geometry": {"enabled": True, "format": "stl_binary"},
                "coordinate_systems": ["base_datum", "arm_datum", "imu_datum"],
                "bodies": [
                    {
                        "id": "base",
                        "name": "base_link",
                        "components": ["base-1"],
                        "frame": {"coordinate_system": "base_datum"},
                    },
                    {
                        "id": "arm",
                        "name": "arm_link",
                        "components": ["arm-1"],
                        "frame": {"coordinate_system": "arm_datum"},
                    },
                ],
                "joints": [
                    {
                        "id": "hinge",
                        "name": "hinge_joint",
                        "type": "revolute",
                        "parent": "base_link",
                        "child": "arm_link",
                        "axis": [0.0, 0.0, 1.0],
                        "limits": {"lower": -1.0, "upper": 1.0, "effort": 2.0, "velocity": 3.0},
                    }
                ],
                "frames": [{"id": "imu", "parent": "base_link", "coordinate_system": "imu_datum"}],
            }
            snapshot = work / "snapshot"
            # The fixture keeps its authoring carriers; the retired keys are
            # dropped at the call boundary and only groups/axis_requests reach
            # the product.
            native_identity = native_tool()
            with mock.patch(
                "description_pipeline.runtime.tool_record",
                return_value=native_identity,
            ):
                support.capture_prepared_fixture(
                    config,
                    snapshot,
                    backend=backend,
                    worker_version=native_identity["version"],
                )

            capture = capture_root(
                self.base / "real-capture",
                main=assembly.name,
            )
            shutil.copyfile(
                assembly,
                capture / "input/cad" / assembly.name,
            )
            for name in ("base.SLDPRT", "arm.SLDPRT"):
                shutil.copyfile(
                    work / "cad" / name,
                    capture / "input/cad" / name,
                )
            evidence = capture / "evidence"
            shutil.rmtree(evidence)
            shutil.copytree(snapshot, evidence)
            native_files = {
                name: file_digest(work / "cad" / name)
                for name in (
                    assembly.name,
                    "base.SLDPRT",
                    "arm.SLDPRT",
                )
            }
            handoff_sha256 = digest(native_files)
            facts = read_data(
                evidence / "raw/native_capture_facts.json.gz"
            )
            discovery_path = (
                capture / "input/discovery/native-discovery.json"
            )
            discovery = read_data(discovery_path)
            discovery["native_files"] = native_files
            discovery["handoff_sha256"] = handoff_sha256
            discovery["identity"].update(
                main_assembly=assembly.name,
                delivery_configuration="Default",
            )
            write_json(discovery_path, discovery)
            source_path = capture / "input/source.json"
            source = read_data(source_path)
            source["identity"].update(
                handoff_digest=handoff_sha256,
                main_assembly=assembly.name,
                configuration="Default",
            )
            source["selection"].update(
                main_assembly=assembly.name,
                configuration="Default",
            )
            source["source_revision_id"] = digest(source["identity"])
            write_json(source_path, source)
            link_groups = facts["link_group_mass_raw"]["links"]
            write_json(
                capture / "input/capture-plan.json",
                {
                    "schema_version": "solidworks-to-urdf.capture-plan/v1",
                    "run_id": RUN_ID,
                    "handoff_sha256": handoff_sha256,
                    "discovery_sha256": file_digest(discovery_path),
                    "main_assembly": assembly.name,
                    "configuration": "Default",
                    "identity": {
                        "hardware_id": "fixture",
                        "revision": "r1",
                    },
                    "selection": {"files": native_files},
                    "objects": [
                        {
                            "name": row["name"],
                            "document": Path(row["path"]).name,
                            "state": row["suppression_state"],
                        }
                        for row in facts["scene_raw"]["occurrences"]
                    ],
                    "groups": [
                        {
                            "name": name,
                            "components": list(entry["selection"]),
                        }
                        for name, entry in sorted(link_groups.items())
                    ],
                    "axis_requests": [],
                    "datums": sorted(
                        facts["scene_raw"]["coordinate_systems"]
                    ),
                    "evidence_requests": list(
                        CAPTURE_EVIDENCE_REQUESTS
                    ),
                },
            )
            package_files = {
                name: checksum
                for name, checksum in inventory(capture / "input").items()
                if name not in {"handoff.json", "request.json"}
            }
            handoff_record = read_data(capture / "input/handoff.json")
            handoff_record["input_receipt"]["inventory"] = dict(
                sorted(native_files.items())
            )
            handoff_record["package_files"] = dict(sorted(package_files.items()))
            write_json(capture / "input/handoff.json", handoff_record)
            write_json(capture / "reports/native-tool.json", native_identity)
            events = _capture_events(capture, handoff=handoff_sha256)
            write_json(
                capture / "reports/native-stages.json",
                stage_view(run_id=RUN_ID, handoff=handoff_sha256, events=events),
            )

            archive = self.base / "real.zip"
            manifest = seal(
                capture,
                archive,
                handoff_sha256=handoff_sha256,
                upload_sha256=handoff_sha256,
                main_assembly=assembly.name,
            )
            self.assertIn(
                "evidence/raw/native_capture_facts.json.gz",
                manifest["files"],
            )
            self.assertNotIn(
                "evidence/evidence/collection.json",
                manifest["files"],
            )
            destination = self.base / "real-admitted"
            admit(
                archive,
                destination,
                expected_handoff_sha256=handoff_sha256,
                expected_upload_sha256=handoff_sha256,
                expected_main_assembly=assembly.name,
            )
            self.assertTrue((destination / "evidence/manifest.json").is_file())
            self.assertTrue((destination / transfer.CAPTURE_MANIFEST).is_file())
        finally:
            support.cleanup(work)

    def test_transfer_receipts_never_enter_the_delivery_subject(self) -> None:
        from description_pipeline.delivery import subject_inventory

        bundle = self.base / "subject-bundle"
        for name in ("input", "evidence", "model", "urdf", "meshes"):
            (bundle / name).mkdir(parents=True)
        (bundle / "README.md").write_text("delivery\n", encoding="utf-8")
        write_json(bundle / "input/handoff.json", {"passed": True})
        write_json(bundle / "tool.json", {"role": "portable"})

        subject = subject_inventory(bundle)  # the target subject carries tool.json only
        self.assertIn("tool.json", subject)

        write_json(bundle / "reports/native-tool.json", native_tool())
        write_json(bundle / "reports/native-stages.json", stage_view())
        write_json(
            bundle / transfer.CAPTURE_MANIFEST,
            {"schema_version": native_transfer.CAPTURE_TRANSFER_SCHEMA},
        )
        for name in ("reports/native-tool.json", "reports/native-stages.json", transfer.CAPTURE_MANIFEST):
            self.assertNotIn(name, subject)
        self.assertEqual(subject_inventory(bundle), subject)  # receipts never bind into the subject

    def test_verify_transfer_gate_revalidates_resealed_deliveries(self) -> None:
        seal(self.root, self.archive)

        def bundle(name: str) -> Path:
            target = self.base / name
            admit(self.archive, target)
            return target

        def reseal(target: Path, mutate: dict[str, bytes]) -> None:
            manifest_path = target / transfer.CAPTURE_MANIFEST
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            for name, payload in mutate.items():
                path = target / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(payload)
                if name in manifest["files"]:
                    manifest["files"][name] = hashlib.sha256(payload).hexdigest()
            manifest["file_count"] = len(manifest["files"])
            manifest["total_bytes"] = sum((target / name).stat().st_size for name in manifest["files"])
            if "reports/native-tool.json" in mutate:
                manifest["native_tool"] = json.loads(mutate["reports/native-tool.json"])
                manifest["native_tool_sha256"] = digest(manifest["native_tool"])
            manifest_path.write_bytes(canonical(manifest))

        clean = bundle("gate-clean")
        self.assertEqual(_verify_transfer(clean)["main_assembly"], MAIN_ASSEMBLY)

        neutral = self.base / "gate-neutral"
        (neutral / "input").mkdir(parents=True)
        (neutral / "evidence").mkdir()
        write_json(neutral / "reports/tool.json", {"role": "portable"})
        self.assertIsNone(_verify_transfer(neutral))

        staged = bundle("gate-stages")
        view = stage_view()
        next(stage for stage in view["stages"] if stage["id"] == "capture")["checks_passed"] = 0
        reseal(staged, {"reports/native-stages.json": canonical(view)})
        with self.assertRaisesRegex(PipelineError, "receipts are incomplete"):
            _verify_transfer(staged)

        retooled = bundle("gate-tool")
        bumped = {**native_tool(), "version": "9.9.9"}
        reseal(retooled, {"reports/native-tool.json": canonical(bumped)})
        with self.assertRaisesRegex(PipelineError, "another release"):
            _verify_transfer(retooled)

        remade = bundle("gate-main")
        replacement = b"OTHER-BYTES"
        report = json.loads((remade / "input/handoff.json").read_text(encoding="utf-8"))
        report["input_receipt"]["inventory"][MAIN_ASSEMBLY] = hashlib.sha256(replacement).hexdigest()
        reseal(
            remade,
            {
                "input/cad/" + MAIN_ASSEMBLY: replacement,
                "input/handoff.json": canonical(report),
            },
        )
        with self.assertRaisesRegex(PipelineError, "Native discovery inventory"):
            _verify_transfer(remade)

        removed = bundle("gate-removed")
        (removed / "reports/native-stages.json").unlink()
        with self.assertRaises(PipelineError):
            _verify_transfer(removed)

    def test_immutable_receipts_cannot_hide_corrupt_disk_copies(self) -> None:
        seal(self.root, self.archive)
        destination = self.base / "receipt-parity"
        admit(self.archive, destination)
        receipts = {
            "native_tool_bytes": (destination / "reports/native-tool.json").read_bytes(),
            "native_stages_bytes": (destination / "reports/native-stages.json").read_bytes(),
            "transfer_manifest_bytes": (destination / transfer.CAPTURE_MANIFEST).read_bytes(),
        }
        self.assertEqual(
            _verify_transfer(
                destination,
                native_receipts=receipts,
            )["main_assembly"],
            MAIN_ASSEMBLY,
        )
        (destination / "reports/native-tool.json").write_bytes(b'{"forged":true}\n')
        with self.assertRaisesRegex(PipelineError, "disk receipt differs"):
            _verify_transfer(destination, native_receipts=receipts)

    def test_carried_request_is_consistent_with_or_without_a_native_member(self) -> None:
        request = build_request_evidence(
            purpose="engineering",
            request_id="portal-20261016T000000-abcdef01",
            target_hardware="fixture",
            change_summary="carried request consistency",
            main_assembly=MAIN_ASSEMBLY,
            handoff_digest=HANDOFF,
            platform_config_digest="e" * 64,
        )
        for label, native_member in (("native-member", True), ("carried-only", False)):
            with self.subTest(case=label):
                root = capture_root(self.base / label)
                if native_member:
                    write_json(root / "input/request.json", request)
                archive = self.base / f"{label}.zip"
                seal(root, archive)
                destination = self.base / f"{label}-admitted"
                admit(archive, destination)
                if not native_member:
                    write_json(destination / "input/request.json", request)
                receipts = {
                    "native_tool_bytes": (destination / "reports/native-tool.json").read_bytes(),
                    "native_stages_bytes": (destination / "reports/native-stages.json").read_bytes(),
                    "transfer_manifest_bytes": (destination / transfer.CAPTURE_MANIFEST).read_bytes(),
                }
                evidence = native_transfer.validate_native_receipts(destination, receipts)
                self.assertEqual(evidence["main_assembly"], MAIN_ASSEMBLY)

    def test_immutable_receipts_always_verify_the_complete_total_bytes(self) -> None:
        seal(self.root, self.archive)
        destination = self.base / "receipt-total"
        admit(self.archive, destination)
        receipts = {
            "native_tool_bytes": (destination / "reports/native-tool.json").read_bytes(),
            "native_stages_bytes": (destination / "reports/native-stages.json").read_bytes(),
        }
        manifest = read_data(destination / transfer.CAPTURE_MANIFEST)
        manifest["total_bytes"] += 1
        receipts["transfer_manifest_bytes"] = canonical(manifest)
        with self.assertRaisesRegex(PipelineError, "size differs"):
            _verify_transfer(destination, native_receipts=receipts)

    def test_source_record_is_required_and_cross_bound(self) -> None:
        missing = capture_root(self.base / "missing-source")
        (missing / "input/source.json").unlink()
        with self.assertRaisesRegex(PipelineError, "input/source.json"):
            seal(missing, self.base / "missing-source.zip")

        forged = capture_root(self.base / "foreign-source")
        source_path = forged / "input/source.json"
        source = read_data(source_path)
        source["identity"]["hardware_id"] = "foreign-hardware"
        source["selection"]["hardware_id"] = "foreign-hardware"
        source["source_revision_id"] = digest(source["identity"])
        write_json(source_path, source)
        with self.assertRaisesRegex(PipelineError, "Source record identity differs"):
            seal(forged, self.base / "foreign-source.zip")

        record_bound = capture_root(self.base / "record-source")
        source_path = record_bound / "input/source.json"
        source = read_data(source_path)
        source["identity"]["records"] = [
            {
                "reference": "records/forged.json#value",
                "file_sha256": NATIVE_FILES[MAIN_ASSEMBLY],
            }
        ]
        source["source_revision_id"] = digest(source["identity"])
        write_json(source_path, source)
        discovery_path = (
            record_bound / "input/discovery/native-discovery.json"
        )
        discovery = read_data(discovery_path)
        discovery["records"] = [
            {
                "reference": "records/forged.json#value",
                "package_file": f"records/forged/{MAIN_ASSEMBLY}",
                "source_sha256": NATIVE_FILES[MAIN_ASSEMBLY],
            }
        ]
        write_json(discovery_path, discovery)
        handoff = read_data(record_bound / "input/handoff.json")
        handoff["package_files"][
            "discovery/native-discovery.json"
        ] = file_digest(discovery_path)
        write_json(record_bound / "input/handoff.json", handoff)
        events = _capture_events(record_bound, handoff=HANDOFF)
        write_json(
            record_bound / "reports/native-stages.json",
            stage_view(run_id=RUN_ID, handoff=HANDOFF, events=events),
        )
        with self.assertRaises(PipelineError):
            seal(record_bound, self.base / "record-source.zip")

    def test_controlled_record_reference_binds_its_file_key_and_anchor(self) -> None:
        def record_root(label: str) -> Path:
            root = capture_root(self.base / f"record-binding-{label}")
            content = b'{"value": "bound"}\n'
            archive_name = "records/source.txt"
            target = root / "input" / archive_name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            checksum = hashlib.sha256(content).hexdigest()
            handoff = read_data(root / "input/handoff.json")
            handoff["package_files"][archive_name] = checksum
            write_json(root / "input/handoff.json", handoff)
            source_path = root / "input/source.json"
            source = read_data(source_path)
            source["identity"]["records"] = [
                {
                    "reference": "components/source.json#value",
                    "file_sha256": checksum,
                }
            ]
            source["source_revision_id"] = digest(source["identity"])
            write_json(source_path, source)
            handoff = read_data(root / "input/handoff.json")
            handoff["package_files"]["source.json"] = file_digest(source_path)
            write_json(root / "input/handoff.json", handoff)
            discovery_path = root / "input/discovery/native-discovery.json"
            discovery = read_data(discovery_path)
            discovery["records"] = [
                {
                    "reference": "components/source.json#value",
                    "file": "components/source.json",
                    "key": "value",
                    "package_file": archive_name,
                    "sha256": checksum,
                    "source_sha256": checksum,
                    "anchor": '"value"',
                }
            ]
            write_json(discovery_path, discovery)
            handoff = read_data(root / "input/handoff.json")
            handoff["package_files"][
                "discovery/native-discovery.json"
            ] = file_digest(discovery_path)
            write_json(root / "input/handoff.json", handoff)
            events = _capture_events(root, handoff=HANDOFF)
            write_json(
                root / "reports/native-stages.json",
                stage_view(run_id=RUN_ID, handoff=HANDOFF, events=events),
            )
            return root

        valid = record_root("valid")
        self.assertEqual(seal(valid, self.base / "record-binding-valid.zip")["run_id"], RUN_ID)

        for label, mutate, pattern in (
            ("file", lambda row: row.update(file="components/other.json"), "reference disagrees"),
            ("key", lambda row: row.update(key="other"), "reference disagrees"),
            ("anchor", lambda row: row.update(anchor='"missing"'), "anchor is not present"),
        ):
            root = record_root(label)
            discovery_path = root / "input/discovery/native-discovery.json"
            discovery = read_data(discovery_path)
            mutate(discovery["records"][0])
            write_json(discovery_path, discovery)
            handoff = read_data(root / "input/handoff.json")
            handoff["package_files"][
                "discovery/native-discovery.json"
            ] = file_digest(discovery_path)
            write_json(root / "input/handoff.json", handoff)
            events = _capture_events(root, handoff=HANDOFF)
            write_json(
                root / "reports/native-stages.json",
                stage_view(run_id=RUN_ID, handoff=HANDOFF, events=events),
            )
            with self.assertRaisesRegex(PipelineError, pattern):
                seal(root, self.base / f"record-binding-{label}.zip")

    def test_controlled_record_archive_path_is_canonical_and_never_overwrites(self) -> None:
        self.assertEqual(record_archive_path("records/record.txt"), "records/record.txt")
        for invalid in (
            "cad/robot.SLDASM",
            "../record.txt",
            "records/../record.txt",
            "records/records/record.txt",
            "records\\record.txt",
        ):
            with self.subTest(invalid=invalid), self.assertRaises(PipelineError):
                record_archive_path(invalid)

        package = self.base / "record-package"
        (package / "discovery").mkdir(parents=True)
        (package / "records").mkdir()
        (package / "records" / "record.txt").write_text("record\n", encoding="utf-8")
        write_json(
            package / "discovery/native-discovery.json",
            {
                "native_files": {},
                "records": [{"package_file": "records/record.txt"}],
            },
        )
        write_json(package / "discovery/findings.json", {"findings": []})
        staging = self.base / "record-staging"
        (staging / "input/records").mkdir(parents=True)
        (staging / "input/records/record.txt").write_text("existing\n", encoding="utf-8")
        with self.assertRaisesRegex(PipelineError, "overwrite"):
            _archive_input(package, staging)

    def test_input_valid_details_are_recomputed_from_archived_bytes(self) -> None:
        root = capture_root(self.base / "input-details")
        view = read_data(root / "reports/native-stages.json")
        invalid = next(
            event["check"]
            for event in view["events"]
            if (event.get("check") or {}).get("id") == "input.valid"
        )
        invalid["details"]["files"][f"input/cad/{MAIN_ASSEMBLY}"] = "0" * 64
        write_json(
            root / "reports/native-stages.json",
            stage_view(run_id=RUN_ID, handoff=HANDOFF, events=view["events"]),
        )
        with self.assertRaisesRegex(PipelineError, "input.valid"):
            seal(root, self.base / "input-details.zip")

    def test_capture_stage_check_details_are_bound(self) -> None:
        mutations = (
            (
                "input.valid",
                lambda details: details["files"].update(
                    {f"input/cad/{MAIN_ASSEMBLY}": "0" * 64}
                ),
            ),
            (
                "capture.integrity",
                lambda details: details.update(manifest_sha256="0" * 64),
            ),
            (
                "capture.input_stability",
                lambda details: details["files"].update(
                    {"input/forged.SLDPRT": "0" * 64}
                ),
            ),
        )
        for label, mutate in mutations:
            with self.subTest(check=label):
                root = capture_root(self.base / f"details-{label.replace('.', '-')}")
                view = read_data(root / "reports/native-stages.json")
                target = next(
                    event["check"]
                    for event in view["events"]
                    if (event.get("check") or {}).get("id") == label
                )
                mutate(target["details"])
                write_json(
                    root / "reports/native-stages.json",
                    stage_view(run_id=RUN_ID, handoff=HANDOFF, events=view["events"]),
                )
                with self.assertRaises(PipelineError):
                    seal(root, self.base / f"details-{label.replace('.', '-')}.zip")

    def test_unknown_discovery_input_is_not_a_derived_file(self) -> None:
        root = capture_root(self.base / "unknown-discovery-input")
        write_json(root / "input/discovery/evil.txt", {"forged": True})
        events = _capture_events(root, handoff=HANDOFF)
        write_json(
            root / "reports/native-stages.json",
            stage_view(run_id=RUN_ID, handoff=HANDOFF, events=events),
        )
        with self.assertRaisesRegex(PipelineError, "Frozen package file inventory differs"):
            seal(root, self.base / "unknown-discovery-input.zip")

    def test_receipt_rows_cannot_diverge_from_raw_events(self) -> None:
        root = capture_root(self.base / "row-details")
        view = read_data(root / "reports/native-stages.json")
        capture_stage = next(stage for stage in view["stages"] if stage["id"] == "capture")
        input_valid = next(
            item for item in capture_stage["input_qc"] if item["id"] == "input.valid"
        )
        input_valid["details"] = {
            "files": {"input/forged.SLDPRT": "0" * 64},
            "passed": True,
            "warnings": [],
        }
        write_json(root / "reports/native-stages.json", view)
        with self.assertRaisesRegex(PipelineError, "receipts differ"):
            seal(root, self.base / "row-details.zip")

    def test_stage_rows_must_remain_in_contract_order(self) -> None:
        root = capture_root(self.base / "row-order")
        view = read_data(root / "reports/native-stages.json")
        view["stages"].reverse()
        write_json(root / "reports/native-stages.json", view)
        with self.assertRaisesRegex(PipelineError, "contract order"):
            seal(root, self.base / "row-order.zip")

    def test_only_native_solidworks_cad_evidence_can_be_transferred(self) -> None:
        root = capture_root(self.base / "evidence-class")
        manifest_path = root / "evidence/manifest.json"
        manifest = read_data(manifest_path)
        manifest["kind"] = "fixture"
        manifest["evidence_class"] = "fixture"
        write_json(manifest_path, manifest)
        with self.assertRaisesRegex(PipelineError, "native SolidWorks CAD evidence"):
            seal(root, self.base / "evidence-class.zip")

    def test_stage_event_order_timestamp_and_completion_are_strict(self) -> None:
        started = datetime(2026, 10, 10, tzinfo=UTC)

        root = capture_root(self.base / "stage-order")
        foreign_view = stage_view()
        foreign_view["events"].append(
            {
                "at": (started + timedelta(seconds=1)).isoformat(),
                "stage": "freeze",
                "state": "completed",
            }
        )
        write_json(root / "reports/native-stages.json", foreign_view)
        with self.assertRaisesRegex(PipelineError, "out-of-scope stage"):
            seal(root, self.base / "stage-order.zip")

        root = capture_root(self.base / "stage-time")
        events = copy.deepcopy(read_data(root / "reports/native-stages.json")["events"])
        events[0]["at"], events[-1]["at"] = events[-1]["at"], events[0]["at"]
        write_json(
            root / "reports/native-stages.json",
            stage_view(run_id=RUN_ID, handoff=HANDOFF, events=events),
        )
        with self.assertRaisesRegex(PipelineError, "not monotonic"):
            seal(root, self.base / "stage-time.zip")

        root = capture_root(self.base / "stage-complete")
        events = copy.deepcopy(read_data(root / "reports/native-stages.json")["events"])
        capture_completed = next(
            event
            for event in events
            if event["stage"] == "capture" and event["state"] == "completed"
        )
        events.insert(-1, {**capture_completed, "at": events[-1]["at"]})
        for index, event in enumerate(events):
            event["at"] = (started + timedelta(microseconds=index)).isoformat()
        write_json(
            root / "reports/native-stages.json",
            stage_view(run_id=RUN_ID, handoff=HANDOFF, events=events),
        )
        with self.assertRaisesRegex(PipelineError, "exactly one completed"):
            seal(root, self.base / "stage-complete.zip")

    def test_native_inventory_rejects_missing_or_added_dependencies(self) -> None:
        for label, added in (("missing", False), ("added", True)):
            with self.subTest(label=label):
                root = capture_root(self.base / f"inventory-{label}")
                extra = root / "input/cad/extra.SLDPRT"
                extra.write_bytes(b"EXTRA")
                report_path = root / "input/handoff.json"
                report = read_data(report_path)
                report["input_receipt"]["inventory"]["extra.SLDPRT"] = file_digest(extra)
                report["package_files"]["cad/extra.SLDPRT"] = file_digest(extra)
                write_json(report_path, report)
                discovery_path = root / "input/discovery/native-discovery.json"
                discovery = read_data(discovery_path)
                if added:
                    discovery["native_files"]["extra.SLDPRT"] = "0" * 64
                write_json(discovery_path, discovery)
                report["package_files"][
                    "discovery/native-discovery.json"
                ] = file_digest(discovery_path)
                write_json(report_path, report)
                with self.assertRaisesRegex(PipelineError, "Native discovery inventory"):
                    seal(root, self.base / f"inventory-{label}.zip")

    def test_capture_identity_is_cross_bound_to_tool_runtime_and_sources(self) -> None:
        root = capture_root(self.base / "identity")
        manifest_path = root / "evidence/manifest.json"
        manifest = read_data(manifest_path)
        manifest["identity"].update(
            document_id=r"C:\foreign\Other.SLDASM",
            worker_version="999.0.0",
            solidworks_revision="99.9.9",
            dependency_digest="f" * 64,
        )
        write_json(manifest_path, manifest)
        events = _capture_events(root, handoff=HANDOFF)
        write_json(
            root / "reports/native-stages.json",
            stage_view(run_id=RUN_ID, handoff=HANDOFF, events=events),
        )
        with self.assertRaisesRegex(PipelineError, "worker version differs"):
            seal(root, self.base / "identity.zip")

    def test_capture_identity_paths_bind_the_full_main_assembly_path(self) -> None:
        root = capture_root(self.base / "identity-paths")
        manifest_path = root / "evidence/manifest.json"
        manifest = read_data(manifest_path)
        foreign = r"C:\foreign\robot.SLDASM"
        manifest["identity"].update(assembly=foreign, document_id=foreign)
        write_json(manifest_path, manifest)
        with self.assertRaisesRegex(
            PipelineError,
            "does not bind the selected source assembly",
        ):
            seal(root, self.base / "identity-paths.zip")

    def test_native_receipts_must_carry_events_that_rebuild_their_rows(self) -> None:
        cases = {}

        without_events = stage_view()
        without_events.pop("events")
        cases["no-events"] = (without_events, "carry the raw protocol events")

        foreign = stage_view()
        foreign["events"] = [
            *foreign["events"],
            {"at": "2026-10-10T00:00:00+00:00", "stage": "generate", "state": "completed"},
        ]
        cases["foreign-stage"] = (foreign, "out-of-scope stage")

        missing_stage = stage_view()
        missing_stage["events"] = [event for event in missing_stage["events"] if event.get("stage") != "capture"]
        cases["incomplete-events"] = (missing_stage, "carry the raw protocol events")

        malformed = stage_view()
        malformed["events"] = [
            *malformed["events"],
            {
                "at": "2030-10-11T00:00:00+00:00",
                "stage": "capture",
                "state": "running",
                "check": {"state": "maybe"},
            },
        ]
        cases["malformed-check"] = (malformed, "malformed")

        for label, (receipt, expected) in cases.items():
            with self.subTest(case=label):
                root = capture_root(self.base / label)
                write_json(root / "reports/native-stages.json", receipt)
                with self.assertRaisesRegex(PipelineError, expected):
                    seal(root, self.base / f"{label}.zip")

    def test_receipts_rebuilt_from_fabricated_rows_are_refused(self) -> None:
        # Stored rows claim success while the bound events never reported the checks.
        receipt = stage_view()
        receipt["events"] = [event for event in receipt["events"] if isinstance(event.get("check"), dict)][:1]
        root = capture_root(self.base / "fabricated")
        write_json(root / "reports/native-stages.json", receipt)
        with self.assertRaisesRegex(PipelineError, "exactly one completed"):
            seal(root, self.base / "fabricated.zip")

    def test_windows_evidence_paths_transport_onto_linux(self) -> None:
        """The real identity.assembly is a Windows absolute path; binding must not parse it."""

        root = capture_root(self.base / "winpath")
        windows = r"C:\native-discovery-20261007\gap-config-runs\prepared\cad\robot.SLDASM"
        evidence = root / "evidence"
        closure_path = evidence / "raw/dependency_closure.json"
        closure = read_data(closure_path)
        old_source = closure["top_level"]
        closure["top_level"] = windows
        closure["original_files"] = {
            windows: closure["original_files"].pop(old_source)
        }
        for row in closure["mapping"]:
            if row.get("source") == old_source:
                row["source"] = windows
        write_json(closure_path, closure)
        facts_path = evidence / "raw/native_capture_facts.json.gz"
        facts = read_data(facts_path)
        facts["scene_raw"]["document"] = windows
        for phase in ("before", "after"):
            facts["document_state"][phase]["path"] = windows
        write_json(facts_path, facts)
        manifest_path = evidence / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["identity"].update(assembly=windows, document_id=windows)
        manifest["files"]["raw/dependency_closure.json"] = file_digest(closure_path)
        manifest["files"]["raw/native_capture_facts.json.gz"] = file_digest(
            facts_path
        )
        manifest_path.write_bytes(canonical(manifest))
        events = _capture_events(root, handoff=HANDOFF)
        write_json(
            root / "reports/native-stages.json",
            stage_view(run_id=RUN_ID, handoff=HANDOFF, events=events),
        )

        archive = self.base / "winpath.zip"
        seal(root, archive)
        destination = self.base / "winpath-out"
        admit(archive, destination)
        self.assertEqual(
            _verify_transfer(destination)["main_assembly"],
            MAIN_ASSEMBLY,
        )

    def test_git_attributes_protect_the_delivered_bytes(self) -> None:
        from description_pipeline.repository.publish import GIT_ATTRIBUTES

        for line in (
            b"/manifest.json -text -filter -working-tree-encoding\n",
            b"/input/** -text -filter -working-tree-encoding\n",
            b"/reports/** -text -filter -working-tree-encoding\n",
            b"/tool.json -text -filter -working-tree-encoding\n",
        ):
            self.assertIn(line, GIT_ATTRIBUTES)
        # Transfer receipts stay in platform run storage; nothing protects them in the repo.
        self.assertNotIn(b"transfer-manifest", GIT_ATTRIBUTES)
        if shutil.which("git") is None:
            self.skipTest("git is unavailable")
        repo = self.base / "attr-repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo), "config", "core.autocrlf", "true"], check=True, capture_output=True)
        (repo / ".gitattributes").write_bytes(GIT_ATTRIBUTES)
        (repo / "tool.json").write_bytes(b"{}\n")
        (repo / "reports").mkdir()
        (repo / "reports/quality.json").write_bytes(b"{}\n")
        result = subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "check-attr",
                "text",
                "filter",
                "working-tree-encoding",
                "--",
                "tool.json",
                "reports/quality.json",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        lines = result.stdout.splitlines()
        self.assertEqual(len(lines), 6)
        for line in lines:
            self.assertTrue(line.endswith(": unset"), line)


if __name__ == "__main__":
    unittest.main()
