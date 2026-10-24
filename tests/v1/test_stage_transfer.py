"""Transfer seal/admit: complete roundtrip plus tamper rejection; no Windows required."""

from __future__ import annotations

import copy
import hashlib
import json
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
from description_pipeline.io import PipelineError, canonical, digest, file_digest, inventory, read_data, write_json
from description_pipeline.orchestration import stage_transfer as transfer
from description_pipeline.runtime import RUNTIME_VERSIONS, required_packages
from description_pipeline.schemas import SOURCE_RECORD_SCHEMA
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


def stage_view(
    *, run_id: str = RUN_ID, handoff: str = HANDOFF, scope=("freeze", "discover", "capture"), events=None
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
    main: str,
    native_files: dict[str, str],
    upload: str | None = None,
    upload_files: dict[str, str] | None = None,
) -> list[dict]:
    upload = upload or handoff
    upload_files = dict(upload_files or native_files)
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
    discovery_record = read_data(root / "evidence/discovery.json")
    discovery_identity = discovery_record.get("identity") or {}
    native_discovery_path = root / "input/discovery/native-discovery.json"
    input_inventory = {
        name: checksum
        for name, checksum in inventory(root / "input").items()
        if name not in {"handoff.json", "request.json"}
    }
    binding_files = {
        f"input/{name}": checksum
        for name, checksum in input_inventory.items()
    }
    prepared_files = dict(input_inventory)
    capture_plan = root / "evidence/capture-plan.json"
    if capture_plan.is_file():
        plan_sha256 = file_digest(capture_plan)
        binding_files["input/capture-plan.json"] = plan_sha256
        prepared_files["capture-plan.json"] = plan_sha256
    evidence_inventory = {
        name: checksum
        for name, checksum in inventory(root / "evidence").items()
        if name
        not in {
            "capture-plan.json",
            "capture.json",
            "discovery.json",
            "native-runtime.json",
        }
    }
    main_sha256 = native_files[main]
    return [
        event(
            0,
            "freeze",
            "running",
            check(
                "input",
                "handoff.admission",
                {"handoff_sha256": upload, "package": "fixture"},
            ),
        ),
        event(
            1,
            "freeze",
            "running",
            check(
                "output",
                "handoff.integrity",
                {
                    "files": {
                        f"handoff/{name}": checksum
                        for name, checksum in upload_files.items()
                    },
                    "handoff_sha256": upload,
                    "main_assembly": main,
                    "main_assembly_sha256": main_sha256,
                },
            ),
        ),
        event(2, "freeze", "completed"),
        event(
            3,
            "discover",
            "running",
            check(
                "input",
                "discovery.inputs",
                {
                    "files_sha256": handoff,
                    "handoff_sha256": handoff,
                    "main_assembly": main,
                    "record_roots": [],
                },
            ),
        ),
        event(
            4,
            "discover",
            "running",
            check(
                "output",
                "discovery.definition",
                {
                    "discovery_sha256": file_digest(native_discovery_path),
                    "findings": discovery_record.get("findings") or [],
                    "hardware_id": discovery_identity.get("hardware_id"),
                    "passed": True,
                    "revision": discovery_identity.get("revision"),
                },
            ),
        ),
        event(
            5,
            "discover",
            "running",
            check(
                "output",
                "discovery.binding",
                {
                    "base": "fixture",
                    "discovery_sha256": file_digest(native_discovery_path),
                    "files": binding_files,
                    "hardware_id": discovery_identity.get("hardware_id"),
                    "prepared_sha256": digest(prepared_files),
                    "repository_slug": "example/fixture",
                    "revision": discovery_identity.get("revision"),
                },
            ),
        ),
        event(6, "discover", "completed"),
        event(
            7,
            "capture",
            "running",
            check(
                "input",
                "input.valid",
                {
                    "cad_revision": handoff_record["cad_revision"],
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
            8,
            "capture",
            "running",
            check(
                "input",
                "runtime.ready",
                {
                    "role": "native",
                    "scope": "fixture",
                    "solidworks_executable": r"C:\Program Files\SOLIDWORKS Corp\SOLIDWORKS\sldworks.exe",
                },
            ),
        ),
        event(
            9,
            "capture",
            "running",
            check(
                "output",
                "capture.integrity",
                {
                    "files": {
                        f"evidence/{name}": checksum
                        for name, checksum in evidence_inventory.items()
                    },
                    "manifest_sha256": file_digest(root / "evidence/manifest.json"),
                },
            ),
        ),
        event(
            10,
            "capture",
            "running",
            check(
                "output",
                "capture.input_stability",
                {
                    "files": {
                        f"input/{Path(name).name}": checksum
                        for name, checksum in input_inventory.items()
                    }
                },
            ),
        ),
        event(11, "capture", "completed"),
    ]


def capture_root(
    base: Path,
    *,
    run_id: str = RUN_ID,
    handoff: str = HANDOFF,
    upload: str = HANDOFF,
    upload_files: dict[str, str] | None = None,
    main: str = MAIN_ASSEMBLY,
) -> Path:
    root = Path(base) / "capture"
    (root / "input" / "cad").mkdir(parents=True)
    (root / "input" / "cad" / main).write_bytes(MAIN_BYTES)
    native_files = {main: file_digest(root / "input" / "cad" / main)}
    upload_files = dict(upload_files or native_files)
    if digest(upload_files) != upload:
        raise AssertionError("upload fixture digest differs from its inventory")
    package_files = {"cad/" + main: native_files[main]}
    cad_revision = {
        "change_summary": "fixture",
        "control": None,
        "hardware_id": "fixture",
        "owner": None,
        "revision": "r1",
    }
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
        "schema_version": "solidworks-to-urdf.native-discovery/v1",
        "run_id": run_id,
        "identity": {
            "delivery_configuration": "Default",
            "hardware_id": "fixture",
            "main_assembly": main,
            "revision": "r1",
        },
        "handoff_sha256": handoff,
        "native_files": native_files,
        "files": [
            {"path": name, "sha256": checksum}
            for name, checksum in sorted(native_files.items())
        ],
        "native_inventory_sha256": digest(
            [
                {"path": name, "sha256": checksum}
                for name, checksum in sorted(native_files.items())
            ]
        ),
        "records": [],
        "findings": [],
        "robot_name": "robot",
    }
    write_json(root / "evidence/discovery.json", discovery)
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
            "cad_revision": cad_revision,
            "package_files": dict(sorted(package_files.items())),
            "main_assembly": main,
            "configuration": "Default",
        },
    )
    evidence = root / "evidence"
    (evidence / "raw").mkdir(parents=True)
    (evidence / "evidence").mkdir()
    write_json(evidence / "raw/scene_raw.json", {"components": [], "mates": []})
    dependency_closure = {
        "configuration": "Default",
        "copy_files": {"source/" + main: native_files[main]},
        "mapping": [
            {
                "copy": "source/" + main,
                "copy_sha256": native_files[main],
                "instance": "top_level",
                "source": str(root / "input/cad" / main),
                "source_sha256": native_files[main],
            }
        ],
        "original_files": {str(root / "input/cad" / main): native_files[main]},
        "top_level": str(root / "input/cad" / main),
    }
    write_json(evidence / "raw/dependency_closure.json", dependency_closure)
    material = {
        "assembly": str(root / "input/cad" / main),
        "configuration": "Default",
        "measured_source": "cad_api",
        "schema_version": "solidworks-to-urdf.material-provenance/v1",
        "warnings": [],
    }
    write_json(evidence / "raw/material_provenance.json", material)
    tool = native_tool()
    environment = {
        "captured_at": "2026-10-10T00:00:00Z",
        "platform": "Windows-11",
        "python": "3.12.10",
        "session_boundary": {},
        "solidworks": {
            "revision": "34.4.1",
            "sessions": {},
        },
        "toolchain": tool,
        "worker_version": tool["version"],
    }
    dependency_inputs = [(main.casefold(), native_files[main])]
    identity = {
        "provider": "solidworks",
        "assembly": str(root / "input" / "cad" / main),
        "document_id": str(root / "input" / "cad" / main),
        "configuration": "Default",
        "worker_version": tool["version"],
        "solidworks_revision": "34.4.1",
        "dependency_digest": digest(dependency_inputs),
    }
    collection = {
        "identity": identity,
        "environment": environment,
        "capture": {
            "collected_document": str(root / "input/cad" / main),
            "originals_unchanged": {
                "files_checked": 1,
                "states_recorded": 1,
                "state_scope": "initial_source_observation",
            },
            "source_hashes": {main.casefold(): {"sha256": native_files[main]}},
        },
    }
    write_json(evidence / "evidence/collection.json", collection)
    write_json(evidence / "evidence/environment.json", environment)
    # The capture stage declares evidence/capture.json, evidence/geometry/ and
    # evidence/native-runtime.json as native outputs; the synthetic tree carries
    # them so a sealed capture delivery covers its stage contract.
    write_json(
        evidence / "capture.json",
        {
            "collection": collection,
            "dependency_closure": dependency_closure,
            "material_provenance": material,
        },
    )
    write_json(evidence / "native-runtime.json", environment)
    (evidence / "geometry").mkdir()
    (evidence / "geometry/base.stl").write_bytes(b"STL-BYTES")
    write_manifest(
        evidence,
        kind="solidworks",
        identity=identity,
        evidence_class="cad",
    )
    write_json(root / "reports/native-tool.json", tool)
    events = _capture_events(
        root,
        handoff=handoff,
        upload=upload,
        upload_files=upload_files,
        main=main,
        native_files=native_files,
    )
    write_json(
        root / "reports/native-stages.json",
        stage_view(run_id=run_id, handoff=handoff, events=events),
    )
    # The native capture stamps the structural-team reports beside the sealed payload; they
    # must travel with the archive so the Linux side can always hand them to the team.
    write_json(
        root / "reports/structural-team.json",
        {
            "schema_version": "solidworks-to-urdf.structural-report/v1",
            "run_id": run_id,
            "state": "native_complete",
            "main_assembly": main,
            "summary": {"warnings": 0, "blockers": 0},
            "findings": [],
        },
    )
    (root / "reports/structural-team.md").write_text("# Structural team report\n\nNo findings.\n", encoding="utf-8")
    return root


def seal(root: Path, archive: Path, **overrides) -> dict:
    arguments = {
        "run_id": RUN_ID,
        "handoff_sha256": HANDOFF,
        "upload_sha256": HANDOFF,
        "main_assembly": MAIN_ASSEMBLY,
        "native_tool": native_tool(),
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
        self.assertEqual(manifest["schema_version"], native_transfer.TRANSFER_SCHEMA)
        self.assertEqual(manifest["run_id"], RUN_ID)
        self.assertEqual(manifest["file_count"], len(manifest["files"]))
        self.assertEqual(manifest["native_stage_scope"], list(native_transfer.NATIVE_STAGE_SCOPE))
        self.assertEqual(manifest["main_assembly_sha256"], manifest["files"]["input/cad/" + MAIN_ASSEMBLY])

    def test_archive_and_in_memory_receipts_use_the_same_manifest_verifier(self) -> None:
        seal(self.root, self.archive)
        with mock.patch.object(native_transfer, "verify_transfer", wraps=native_transfer.verify_transfer) as verify:
            admit(self.archive, self.base / "public-api")
        self.assertEqual(verify.call_count, 1)
        for label, mutation in (
            ("unknown", lambda manifest: {**manifest, "unexpected_field": True}),
            ("scope", lambda manifest: {**manifest, "native_stage_scope": ["capture"]}),
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
            self.assertIn(name, manifest["files"])
            self.assertEqual(file_digest(destination / name), file_digest(self.root / name))
        self.assertEqual(
            file_digest(destination / "reports/native-tool.json"),
            manifest["files"]["reports/native-tool.json"],
        )
        with zipfile.ZipFile(self.archive) as archive:
            sealed = archive.read(transfer.CAPTURE_MANIFEST)
        self.assertEqual((destination / transfer.CAPTURE_MANIFEST).read_bytes(), sealed)

    def test_full_upload_and_selected_native_handoff_are_bound_separately(self) -> None:
        upload_files = {**NATIVE_FILES, "excluded.SLDPRT": "e" * 64}
        upload_sha256 = digest(upload_files)
        root = capture_root(
            self.base / "separate-upload",
            upload=upload_sha256,
            upload_files=upload_files,
        )
        archive = self.base / "separate-upload.zip"
        manifest = seal(root, archive, upload_sha256=upload_sha256)
        self.assertEqual(manifest["upload_sha256"], upload_sha256)
        self.assertEqual(manifest["handoff_sha256"], HANDOFF)

        destination = self.base / "separate-upload-admitted"
        admit(archive, destination, expected_upload_sha256=upload_sha256)
        summary = native_transfer.verify_transfer(
            destination,
            expected_upload_sha256=upload_sha256,
        )
        self.assertEqual(summary["upload_sha256"], upload_sha256)
        self.assertEqual(summary["handoff_sha256"], HANDOFF)

    def test_regenerated_team_reports_are_tolerated_only_in_deliveries(self) -> None:
        manifest = seal(self.root, self.archive)
        destination = self.base / "admitted"
        admit(self.archive, destination)
        summary = native_transfer.verify_transfer(destination)
        self.assertEqual(summary["files"], manifest["file_count"])

        # The admitted capture stays strict when the sealed report bytes change ...
        (destination / "reports/structural-team.md").write_text("# regenerated\n", encoding="utf-8")
        (destination / "reports/structural-team.json").write_text('{"findings": []}\n', encoding="utf-8")
        with self.assertRaisesRegex(PipelineError, "differs from the transfer manifest"):
            native_transfer.verify_transfer(destination)
        # ... while a delivery may carry its own regenerated copy ...
        summary = native_transfer.verify_transfer(destination, regenerated=native_transfer.TEAM_REPORTS)
        self.assertEqual(summary["files"], manifest["file_count"])
        # ... or none at all before the report is stamped.
        (destination / "reports/structural-team.md").unlink()
        (destination / "reports/structural-team.json").unlink()
        summary = native_transfer.verify_transfer(destination, regenerated=native_transfer.TEAM_REPORTS)
        self.assertEqual(summary["files"], manifest["file_count"])
        with self.assertRaises(PipelineError):
            native_transfer.verify_transfer(destination)

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
            collection = json.loads(source.read("evidence/evidence/collection.json"))
        collection["capture"]["originals_unchanged"] = False
        payload = canonical(collection)
        with zipfile.ZipFile(self.archive) as source:
            evidence_manifest = json.loads(source.read("evidence/manifest.json"))
        evidence_manifest["files"]["evidence/collection.json"] = hashlib.sha256(payload).hexdigest()
        broken = self.base / "evidence.zip"
        repack(
            self.archive,
            broken,
            {"evidence/evidence/collection.json": payload, "evidence/manifest.json": canonical(evidence_manifest)},
        )
        with self.assertRaisesRegex(PipelineError, "Standalone capture collection"):
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
            record = json.loads(source.read("evidence/discovery.json"))

        renamed = {**record, "identity": {**record["identity"], "main_assembly": "other.SLDASM"}}
        broken = self.base / "discovery-name.zip"
        repack(
            self.archive,
            broken,
            {
                "evidence/discovery.json": canonical(renamed),
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
                "evidence/discovery.json": canonical(rebased),
            },
        )
        with self.assertRaisesRegex(PipelineError, "bound to another handoff"):
            admit(broken, self.base / "out-discovery-handoff")

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

        portable = {**native_tool(), "runtime": {"role": "portable"}}
        cases.append(("portable-tool", portable, "reports/native-tool.json", "runtime.role", portable))

        wide_scope = stage_view(scope=("freeze", "discover", "capture", "generate"))
        cases.append(("wide-scope", wide_scope, "reports/native-stages.json", "native execution scope"))

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
        events = _capture_events(
            self.root,
            handoff=HANDOFF,
            main=MAIN_ASSEMBLY,
            native_files={MAIN_ASSEMBLY: file_digest(self.root / "input/cad" / MAIN_ASSEMBLY)},
        )
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
                "pin",
                {
                    "runtime": {
                        **native_tool()["runtime"],
                        "packages": {required_packages("native")[0]: "0.0.1"},
                    }
                },
                f"pin {required_packages('native')[0]}",
            ),
            (
                "mujoco",
                {
                    "runtime": {
                        **native_tool()["runtime"],
                        "packages": {**native_tool()["runtime"]["packages"], "mujoco": "3.13.0"},
                    }
                },
                "MuJoCo",
            ),
        )
        for label, overrides, expected in cases:
            with self.subTest(case=label):
                root = capture_root(self.base / label)
                tool = {**native_tool(), **overrides}
                write_json(root / "reports/native-tool.json", tool)
                with self.assertRaisesRegex(PipelineError, expected):
                    seal(root, self.base / f"tool-{label}.zip", native_tool=tool)

    def test_neutral_freeze_capture_roundtrip(self) -> None:
        """The evidence identity is validated against a real freeze snapshot, not a hand shape."""

        from tests.sources import support

        from description_pipeline.sources.solidworks.freeze import freeze

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
                freeze(
                    support.capture_requests(config),
                    snapshot,
                    backend=backend,
                    worker_version=native_identity["version"],
                )

            capture = self.base / "real-capture"
            (capture / "input" / "cad").mkdir(parents=True)
            shutil.copyfile(assembly, capture / "input" / "cad" / assembly.name)
            for name in ("base.SLDPRT", "arm.SLDPRT"):
                shutil.copyfile(work / "cad" / name, capture / "input" / "cad" / name)
            shutil.copytree(snapshot, capture / "evidence")
            collection = read_data(capture / "evidence/evidence/collection.json")
            write_json(
                capture / "evidence/capture.json",
                {
                    "collection": collection,
                    "dependency_closure": read_data(
                        capture / "evidence/raw/dependency_closure.json"
                    ),
                    "material_provenance": read_data(
                        capture / "evidence/raw/material_provenance.json"
                    ),
                },
            )
            write_json(capture / "evidence/native-runtime.json", collection["environment"])
            native_files = {
                name: file_digest(work / "cad" / name) for name in ("robot.SLDASM", "base.SLDPRT", "arm.SLDPRT")
            }
            handoff_sha256 = digest(native_files)
            native_discovery = {
                "schema_version": "solidworks-to-urdf.native-discovery/v1",
                "run_id": RUN_ID,
                "identity": {
                    "delivery_configuration": "Default",
                    "hardware_id": "fixture",
                    "main_assembly": assembly.name,
                    "revision": "r1",
                },
                "handoff_sha256": handoff_sha256,
                "native_files": native_files,
                "files": [
                    {"path": name, "sha256": checksum}
                    for name, checksum in sorted(native_files.items())
                ],
                "native_inventory_sha256": digest(
                    [
                        {"path": name, "sha256": checksum}
                        for name, checksum in sorted(native_files.items())
                    ]
                ),
                "records": [],
                "findings": [],
                "robot_name": "robot",
            }
            write_json(capture / "evidence/discovery.json", native_discovery)
            (capture / "input" / "discovery").mkdir()
            write_json(
                capture / "input/discovery/native-discovery.json",
                native_discovery,
            )
            manifest = read_data(capture / "evidence" / "manifest.json")
            manifest["files"]["capture.json"] = file_digest(capture / "evidence/capture.json")
            manifest["files"]["discovery.json"] = file_digest(capture / "evidence" / "discovery.json")
            manifest["files"]["native-runtime.json"] = file_digest(
                capture / "evidence/native-runtime.json"
            )
            manifest["files"] = dict(sorted(manifest["files"].items()))
            write_json(capture / "evidence" / "manifest.json", manifest)
            package_files = {
                "cad/" + name: checksum
                for name, checksum in native_files.items()
            }
            write_json(
                capture / "input/handoff.json",
                {
                    "passed": True,
                    "input": {
                        "hardware_id": "fixture",
                        "source": {
                            "assembly": assembly.name,
                            "configuration": "Default",
                        },
                    },
                    "input_receipt": {"inventory": dict(sorted(native_files.items()))},
                    "cad_revision": {"hardware_id": "fixture", "revision": "r1"},
                    "package_files": dict(sorted(package_files.items())),
                    "main_assembly": assembly.name,
                    "configuration": "Default",
                },
            )
            source_identity = {
                "configuration": "Default",
                "handoff_digest": handoff_sha256,
                "hardware_id": "fixture",
                "kind": "source-revision",
                "main_assembly": assembly.name,
                "records": [],
            }
            write_json(
                capture / "input/source.json",
                {
                    "schema_version": SOURCE_RECORD_SCHEMA,
                    "source_revision_id": digest(source_identity),
                    "identity": source_identity,
                    "selection": {
                        "configuration": "Default",
                        "hardware_id": "fixture",
                        "hardware_source": "cad",
                        "main_assembly": assembly.name,
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
            package_files["source.json"] = file_digest(capture / "input/source.json")
            handoff_record = read_data(capture / "input/handoff.json")
            handoff_record["package_files"] = dict(sorted(package_files.items()))
            write_json(capture / "input/handoff.json", handoff_record)
            write_json(capture / "reports/native-tool.json", native_identity)
            events = _capture_events(
                capture,
                handoff=handoff_sha256,
                main=assembly.name,
                native_files=native_files,
            )
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
            self.assertIn("evidence/evidence/collection.json", manifest["files"])
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
        from description_pipeline.delivery import check_layout, subject_inventory

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
        write_json(bundle / transfer.CAPTURE_MANIFEST, {"schema_version": native_transfer.TRANSFER_SCHEMA})
        for name in ("reports/native-tool.json", "reports/native-stages.json", transfer.CAPTURE_MANIFEST):
            self.assertNotIn(name, subject)
        self.assertEqual(subject_inventory(bundle), subject)  # receipts never bind into the subject
        (bundle / "README.md").unlink()
        with self.assertRaisesRegex(PipelineError, "Unregistered delivery"):
            check_layout(bundle)

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
        self.assertEqual(native_transfer.verify_transfer(clean)["main_assembly"], MAIN_ASSEMBLY)

        neutral = self.base / "gate-neutral"
        (neutral / "input").mkdir(parents=True)
        (neutral / "evidence").mkdir()
        write_json(neutral / "reports/tool.json", {"role": "portable"})
        self.assertIsNone(native_transfer.verify_transfer(neutral))

        staged = bundle("gate-stages")
        view = stage_view()
        next(stage for stage in view["stages"] if stage["id"] == "capture")["checks_passed"] = 0
        reseal(staged, {"reports/native-stages.json": canonical(view)})
        with self.assertRaisesRegex(PipelineError, "receipts are incomplete"):
            native_transfer.verify_transfer(staged)

        retooled = bundle("gate-tool")
        bumped = {**native_tool(), "version": "9.9.9"}
        reseal(retooled, {"reports/native-tool.json": canonical(bumped)})
        with self.assertRaisesRegex(PipelineError, "another release"):
            native_transfer.verify_transfer(retooled)

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
            native_transfer.verify_transfer(remade)

        removed = bundle("gate-removed")
        (removed / "reports/native-stages.json").unlink()
        with self.assertRaises(PipelineError):
            native_transfer.verify_transfer(removed)

    def test_immutable_receipts_cannot_hide_corrupt_disk_copies(self) -> None:
        seal(self.root, self.archive)
        destination = self.base / "receipt-parity"
        admit(self.archive, destination)
        receipts = {
            "native_tool_bytes": (destination / "reports/native-tool.json").read_bytes(),
            "native_stages_bytes": (destination / "reports/native-stages.json").read_bytes(),
            "transfer_manifest_bytes": (destination / transfer.CAPTURE_MANIFEST).read_bytes(),
            "structural_team_md_bytes": (
                destination / "reports/structural-team.md"
            ).read_bytes(),
            "structural_team_json_bytes": (
                destination / "reports/structural-team.json"
            ).read_bytes(),
        }
        self.assertEqual(
            native_transfer.verify_transfer(destination, native_receipts=receipts)["main_assembly"],
            MAIN_ASSEMBLY,
        )
        (destination / "reports/native-tool.json").write_bytes(b'{"forged":true}\n')
        with self.assertRaisesRegex(PipelineError, "disk receipt differs"):
            native_transfer.verify_transfer(destination, native_receipts=receipts)

    def test_immutable_receipts_always_verify_the_complete_total_bytes(self) -> None:
        seal(self.root, self.archive)
        destination = self.base / "receipt-total"
        admit(self.archive, destination)
        receipts = {
            "native_tool_bytes": (destination / "reports/native-tool.json").read_bytes(),
            "native_stages_bytes": (destination / "reports/native-stages.json").read_bytes(),
            "structural_team_md_bytes": (
                destination / "reports/structural-team.md"
            ).read_bytes(),
            "structural_team_json_bytes": (
                destination / "reports/structural-team.json"
            ).read_bytes(),
        }
        manifest = read_data(destination / transfer.CAPTURE_MANIFEST)
        manifest["total_bytes"] += 1
        receipts["transfer_manifest_bytes"] = canonical(manifest)
        for name in native_transfer.TEAM_REPORTS:
            (destination / name).unlink()
        with self.assertRaisesRegex(PipelineError, "size differs"):
            native_transfer.verify_transfer(
                destination,
                regenerated=native_transfer.TEAM_REPORTS,
                native_receipts=receipts,
            )

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
        discovery_path = record_bound / "evidence/discovery.json"
        discovery = read_data(discovery_path)
        discovery["records"] = [
            {
                "reference": "records/forged.json#value",
                "package_file": f"records/forged/{MAIN_ASSEMBLY}",
                "source_sha256": NATIVE_FILES[MAIN_ASSEMBLY],
            }
        ]
        write_json(discovery_path, discovery)
        evidence_manifest = read_data(record_bound / "evidence/manifest.json")
        evidence_manifest["files"]["discovery.json"] = file_digest(discovery_path)
        write_json(record_bound / "evidence/manifest.json", evidence_manifest)
        events = _capture_events(
            record_bound,
            handoff=HANDOFF,
            main=MAIN_ASSEMBLY,
            native_files=NATIVE_FILES,
        )
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
            discovery_path = root / "evidence/discovery.json"
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
            evidence_manifest = read_data(root / "evidence/manifest.json")
            evidence_manifest["files"]["discovery.json"] = file_digest(discovery_path)
            write_json(root / "evidence/manifest.json", evidence_manifest)
            events = _capture_events(
                root,
                handoff=HANDOFF,
                main=MAIN_ASSEMBLY,
                native_files=NATIVE_FILES,
            )
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
            discovery_path = root / "evidence/discovery.json"
            discovery = read_data(discovery_path)
            mutate(discovery["records"][0])
            write_json(discovery_path, discovery)
            evidence_manifest = read_data(root / "evidence/manifest.json")
            evidence_manifest["files"]["discovery.json"] = file_digest(discovery_path)
            write_json(root / "evidence/manifest.json", evidence_manifest)
            events = _capture_events(
                root,
                handoff=HANDOFF,
                main=MAIN_ASSEMBLY,
                native_files=NATIVE_FILES,
            )
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

    def test_all_native_stage_check_details_are_bound(self) -> None:
        mutations = (
            (
                "handoff.admission",
                lambda details: details.update(handoff_sha256="0" * 64),
            ),
            (
                "discovery.definition",
                lambda details: details.update(discovery_sha256="0" * 64),
            ),
            (
                "discovery.binding",
                lambda details: details["files"].update(
                    {"input/forged.SLDPRT": "0" * 64}
                ),
            ),
            (
                "runtime.ready",
                lambda details: details.update(
                    solidworks_executable=r"C:\foreign\not-solidworks.exe"
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

    def test_discovery_binding_is_a_complete_prepared_to_archived_mapping(self) -> None:
        relative = f"input/cad/{MAIN_ASSEMBLY}"
        for label in ("omitted", "foreign-path"):
            with self.subTest(case=label):
                root = capture_root(self.base / f"binding-{label}")
                view = read_data(root / "reports/native-stages.json")
                event = next(
                    event
                    for event in view["events"]
                    if (event.get("check") or {}).get("id") == "discovery.binding"
                )
                files = dict(event["check"]["details"]["files"])
                checksum = files.pop(relative)
                if label == "foreign-path":
                    files[f"input/foreign/{MAIN_ASSEMBLY}"] = checksum
                event["check"]["details"]["files"] = files
                event["check"]["details"]["prepared_sha256"] = digest(
                    {
                        name.removeprefix("input/"): value
                        for name, value in files.items()
                    }
                )
                write_json(
                    root / "reports/native-stages.json",
                    stage_view(run_id=RUN_ID, handoff=HANDOFF, events=view["events"]),
                )
                with self.assertRaises(PipelineError):
                    seal(root, self.base / f"binding-{label}.zip")

    def test_unknown_discovery_input_is_not_a_derived_file(self) -> None:
        root = capture_root(self.base / "unknown-discovery-input")
        write_json(root / "input/discovery/evil.txt", {"forged": True})
        events = _capture_events(
            root,
            handoff=HANDOFF,
            main=MAIN_ASSEMBLY,
            native_files=NATIVE_FILES,
        )
        write_json(
            root / "reports/native-stages.json",
            stage_view(run_id=RUN_ID, handoff=HANDOFF, events=events),
        )
        with self.assertRaisesRegex(PipelineError, "unbound non-package file"):
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
        events = copy.deepcopy(read_data(root / "reports/native-stages.json")["events"])
        reordered = [
            event
            for stage in ("capture", "freeze", "discover")
            for event in events
            if event["stage"] == stage
        ]
        for index, event in enumerate(reordered):
            event["at"] = (started + timedelta(microseconds=index)).isoformat()
        write_json(
            root / "reports/native-stages.json",
            stage_view(run_id=RUN_ID, handoff=HANDOFF, events=reordered),
        )
        with self.assertRaisesRegex(PipelineError, "reordered, missing or interleaved"):
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
                discovery_path = root / "evidence/discovery.json"
                discovery = read_data(discovery_path)
                if added:
                    discovery["native_files"]["extra.SLDPRT"] = "0" * 64
                write_json(discovery_path, discovery)
                with self.assertRaisesRegex(PipelineError, "Native discovery inventory"):
                    seal(root, self.base / f"inventory-{label}.zip")

    def test_capture_identity_is_cross_bound_to_tool_runtime_and_sources(self) -> None:
        root = capture_root(self.base / "identity")
        collection_path = root / "evidence/evidence/collection.json"
        collection = read_data(collection_path)
        collection["identity"].update(
            document_id=r"C:\foreign\Other.SLDASM",
            worker_version="999.0.0",
            solidworks_revision="99.9.9",
            dependency_digest="f" * 64,
        )
        write_json(collection_path, collection)
        capture_record = read_data(root / "evidence/capture.json")
        capture_record["collection"] = collection
        write_json(root / "evidence/capture.json", capture_record)
        evidence_manifest = read_data(root / "evidence/manifest.json")
        evidence_manifest["identity"] = collection["identity"]
        evidence_manifest["files"]["evidence/collection.json"] = file_digest(collection_path)
        evidence_manifest["files"]["capture.json"] = file_digest(root / "evidence/capture.json")
        write_json(root / "evidence/manifest.json", evidence_manifest)
        events = _capture_events(
            root,
            handoff=HANDOFF,
            main=MAIN_ASSEMBLY,
            native_files=NATIVE_FILES,
        )
        write_json(
            root / "reports/native-stages.json",
            stage_view(run_id=RUN_ID, handoff=HANDOFF, events=events),
        )
        with self.assertRaisesRegex(PipelineError, "worker version differs"):
            seal(root, self.base / "identity.zip")

    def test_capture_identity_paths_bind_the_full_main_assembly_path(self) -> None:
        root = capture_root(self.base / "identity-paths")
        collection_path = root / "evidence/evidence/collection.json"
        collection = read_data(collection_path)
        foreign = r"C:\foreign\robot.SLDASM"
        collection["identity"].update(assembly=foreign, document_id=foreign)
        write_json(collection_path, collection)
        capture_record = read_data(root / "evidence/capture.json")
        capture_record["collection"] = collection
        write_json(root / "evidence/capture.json", capture_record)
        evidence_manifest = read_data(root / "evidence/manifest.json")
        evidence_manifest["identity"] = collection["identity"]
        evidence_manifest["files"]["evidence/collection.json"] = file_digest(collection_path)
        evidence_manifest["files"]["capture.json"] = file_digest(root / "evidence/capture.json")
        write_json(root / "evidence/manifest.json", evidence_manifest)
        with self.assertRaisesRegex(PipelineError, "source paths disagree"):
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
        cases["foreign-stage"] = (foreign, "first three stages")

        missing_stage = stage_view()
        missing_stage["events"] = [event for event in missing_stage["events"] if event.get("stage") != "capture"]
        cases["incomplete-events"] = (missing_stage, "reordered, missing or interleaved")

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
        with self.assertRaisesRegex(PipelineError, "reordered, missing or interleaved"):
            seal(root, self.base / "fabricated.zip")

    def test_windows_evidence_paths_transport_onto_linux(self) -> None:
        """The real identity.assembly is a Windows absolute path; binding must not parse it."""

        root = capture_root(self.base / "winpath")
        windows = r"C:\native-discovery-20261007\gap-config-runs\prepared\cad\robot.SLDASM"
        evidence = root / "evidence"
        collection_path = evidence / "evidence/collection.json"
        collection = json.loads(collection_path.read_text(encoding="utf-8"))
        collection["identity"].update(assembly=windows, document_id=windows)
        collection["capture"]["collected_document"] = windows
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
        material_path = evidence / "raw/material_provenance.json"
        material = read_data(material_path)
        material["assembly"] = windows
        write_json(material_path, material)
        payload = canonical(collection)
        collection_path.write_bytes(payload)
        capture_record = json.loads((evidence / "capture.json").read_text(encoding="utf-8"))
        capture_record["collection"] = collection
        capture_record["dependency_closure"] = closure
        capture_record["material_provenance"] = material
        write_json(evidence / "capture.json", capture_record)
        manifest_path = evidence / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["identity"].update(assembly=windows, document_id=windows)
        manifest["files"]["evidence/collection.json"] = hashlib.sha256(payload).hexdigest()
        manifest["files"]["raw/dependency_closure.json"] = file_digest(closure_path)
        manifest["files"]["raw/material_provenance.json"] = file_digest(material_path)
        manifest["files"]["capture.json"] = file_digest(evidence / "capture.json")
        manifest_path.write_bytes(canonical(manifest))
        events = _capture_events(
            root,
            handoff=HANDOFF,
            main=MAIN_ASSEMBLY,
            native_files=NATIVE_FILES,
        )
        write_json(
            root / "reports/native-stages.json",
            stage_view(run_id=RUN_ID, handoff=HANDOFF, events=events),
        )

        archive = self.base / "winpath.zip"
        seal(root, archive)
        destination = self.base / "winpath-out"
        admit(archive, destination)
        self.assertEqual(native_transfer.verify_transfer(destination)["main_assembly"], MAIN_ASSEMBLY)

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
