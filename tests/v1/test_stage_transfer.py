"""Transfer seal/admit: complete roundtrip plus tamper rejection; no Windows required."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from packaging.utils import canonicalize_name

from description_pipeline import __version__ as PIPELINE_VERSION
from description_pipeline.build.archive import write_zip
from description_pipeline.delivery import PIPELINE_ID
from description_pipeline.io import PipelineError, canonical, digest, file_digest, write_json
from description_pipeline.orchestration import stage_transfer as transfer
from description_pipeline.runtime import RUNTIME_VERSIONS, required_packages
from description_pipeline.sources.snapshot import write_manifest
from description_pipeline.stages import stage_view as rendered_stage_view

from . import protocol_support

RUN_ID = "20261010T000000-transfer"
HANDOFF = "a" * 64
MAIN_ASSEMBLY = "robot.SLDASM"


def native_tool() -> dict:
    return {
        "schema_version": "solidworks-to-urdf.tool/v1",
        "pipeline_id": PIPELINE_ID,
        "version": PIPELINE_VERSION,
        "source_sha256": "f" * 64,
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


def capture_root(base: Path, *, run_id: str = RUN_ID, handoff: str = HANDOFF, main: str = MAIN_ASSEMBLY) -> Path:
    root = Path(base) / "capture"
    (root / "input").mkdir(parents=True)
    write_json(root / "input/robot.yaml", {"hardware_id": "robot"})
    (root / "input" / main).write_bytes(b"CAD-BYTES")
    write_json(
        root / "input/discovery/native-discovery.json",
        {
            "identity": {"main_assembly": main},
            "handoff_sha256": handoff,
            "native_files": {main: file_digest(root / "input" / main)},
        },
    )
    package_files = {
        name: file_digest(root / "input" / name) for name in ("robot.yaml", main, "discovery/native-discovery.json")
    }
    write_json(
        root / "reports/input.json",
        {
            "passed": True,
            "input_receipt": {
                "inventory": [{"path": name, "sha256": sha} for name, sha in sorted(package_files.items())]
            },
            "cad_revision": {"revision": "r1"},
            "package_files": package_files,
        },
    )
    evidence = root / "evidence"
    (evidence / "source").mkdir(parents=True)
    (evidence / "source/base.SLDPRT").write_bytes(b"PART-BYTES")
    write_json(evidence / "scene.json", {"components": [], "mates": []})
    (evidence / "evidence").mkdir()
    identity = {"provider": "solidworks", "assembly": str(root / "input" / main), "dependency_digest": "c" * 64}
    write_json(
        evidence / "evidence/collection.json",
        {
            "identity": identity,
            "capture": {
                "originals_unchanged": {
                    "files_checked": 2,
                    "states_recorded": 2,
                    "state_scope": "initial_source_observation",
                },
                "source_hashes": {main.lower(): {"sha256": "d" * 64}},
            },
        },
    )
    write_manifest(
        evidence,
        kind="solidworks",
        identity=identity,
        evidence_class="cad",
    )
    write_json(root / "reports/native-tool.json", native_tool())
    write_json(root / "reports/native-stages.json", stage_view(run_id=run_id, handoff=handoff))
    return root


def seal(root: Path, archive: Path, **overrides) -> dict:
    arguments = {
        "run_id": RUN_ID,
        "handoff_sha256": HANDOFF,
        "main_assembly": MAIN_ASSEMBLY,
        "native_tool": native_tool(),
    }
    arguments.update(overrides)
    return transfer.seal_capture(root, archive, **arguments)


def admit(archive: Path, destination: Path, **overrides) -> dict:
    arguments = {
        "expected_run_id": RUN_ID,
        "expected_handoff_sha256": HANDOFF,
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
        self.assertEqual(manifest["schema_version"], transfer.TRANSFER_SCHEMA)
        self.assertEqual(manifest["run_id"], RUN_ID)
        self.assertEqual(manifest["file_count"], len(manifest["files"]))
        self.assertEqual(manifest["native_stage_scope"], list(transfer.NATIVE_STAGE_SCOPE))
        self.assertEqual(manifest["main_assembly_sha256"], manifest["files"]["input/" + MAIN_ASSEMBLY])

        destination = self.base / "admitted"
        admitted = admit(self.archive, destination)
        self.assertEqual(admitted, manifest)
        installed = {path.relative_to(destination).as_posix() for path in destination.rglob("*") if path.is_file()}
        self.assertEqual(installed, set(manifest["files"]) | {transfer.CAPTURE_MANIFEST})
        self.assertEqual(
            file_digest(destination / "reports/native-tool.json"),
            manifest["files"]["reports/native-tool.json"],
        )
        with zipfile.ZipFile(self.archive) as archive:
            sealed = archive.read(transfer.CAPTURE_MANIFEST)
        self.assertEqual((destination / transfer.CAPTURE_MANIFEST).read_bytes(), sealed)

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
                with self.assertRaisesRegex(PipelineError, "inventory differs"):
                    admit(broken, self.base / f"out-{label}")

    def test_edited_or_replaced_member_bytes_are_refused(self) -> None:
        seal(self.root, self.archive)
        with zipfile.ZipFile(self.archive) as archive:
            original = archive.read("reports/input.json")
        broken = self.base / "edited.zip"
        # Same length so the size invariant still holds and the content hash is what rejects it.
        raw_copy(self.archive, broken, replace={"reports/input.json": b"X" * len(original)})
        with self.assertRaisesRegex(PipelineError, "hash differs"):
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
        with self.assertRaisesRegex(PipelineError, "CAD bytes were unchanged"):
            admit(broken, self.base / "out-evidence")

    def test_self_consistent_input_report_tamper_is_refused(self) -> None:
        seal(self.root, self.archive)
        with zipfile.ZipFile(self.archive) as source:
            report = json.loads(source.read("reports/input.json"))
        report["passed"] = False
        broken = self.base / "input.zip"
        repack(self.archive, broken, {"reports/input.json": canonical(report)})
        with self.assertRaisesRegex(PipelineError, "Input report did not pass"):
            admit(broken, self.base / "out-input")

    def test_self_consistent_main_assembly_hash_tamper_is_refused(self) -> None:
        seal(self.root, self.archive)
        replacement = b"OTHER-BYTES"
        with zipfile.ZipFile(self.archive) as source:
            report = json.loads(source.read("reports/input.json"))
        report["package_files"][MAIN_ASSEMBLY] = hashlib.sha256(replacement).hexdigest()
        broken = self.base / "main.zip"
        repack(
            self.archive,
            broken,
            {
                "input/" + MAIN_ASSEMBLY: replacement,
                "reports/input.json": canonical(report),
            },
        )
        # The archive is fully self-consistent; only the native discovery inventory still names
        # the original bytes, so the main-assembly binding must refuse it.
        with self.assertRaisesRegex(PipelineError, "native file inventory"):
            admit(self.base / "main.zip", self.base / "out-main")

    def test_native_discovery_binding_is_enforced(self) -> None:
        seal(self.root, self.archive)
        with zipfile.ZipFile(self.archive) as source:
            record = json.loads(source.read("input/discovery/native-discovery.json"))
            report = json.loads(source.read("reports/input.json"))

        renamed = {**record, "identity": {**record["identity"], "main_assembly": "other.SLDASM"}}
        renamed_bytes = canonical(renamed)
        report["package_files"]["discovery/native-discovery.json"] = hashlib.sha256(renamed_bytes).hexdigest()
        broken = self.base / "discovery-name.zip"
        repack(
            self.archive,
            broken,
            {
                "input/discovery/native-discovery.json": renamed_bytes,
                "reports/input.json": canonical(report),
            },
        )
        with self.assertRaisesRegex(PipelineError, "opened another main assembly"):
            admit(broken, self.base / "out-discovery-name")

        rebased = {**record, "handoff_sha256": "b" * 64}
        rebased_bytes = canonical(rebased)
        report["package_files"]["discovery/native-discovery.json"] = hashlib.sha256(rebased_bytes).hexdigest()
        broken = self.base / "discovery-handoff.zip"
        repack(
            self.archive,
            broken,
            {
                "input/discovery/native-discovery.json": rebased_bytes,
                "reports/input.json": canonical(report),
            },
        )
        with self.assertRaisesRegex(PipelineError, "bound to another handoff"):
            admit(broken, self.base / "out-discovery-handoff")

    def test_structural_zip_tamper_is_refused(self) -> None:
        seal(self.root, self.archive)
        duplicate = zipfile.ZipInfo("Reports/Input.json")
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
        with mock.patch.object(transfer, "MAX_TRANSFER_FILES", 1), self.assertRaisesRegex(PipelineError, "file limit"):
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

        input_report = json.loads((self.root / "reports/input.json").read_text(encoding="utf-8"))
        input_report["passed"] = False
        cases.append(("input-failed", input_report, "reports/input.json", "Input report did not pass"))

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
        (self.root / "input" / MAIN_ASSEMBLY).write_bytes(b"CHANGED")
        with self.assertRaisesRegex(PipelineError, "Archived input differs"):
            seal(self.root, self.archive)

    def test_seal_rejects_missing_reports(self) -> None:
        (self.root / "reports/native-tool.json").unlink()
        with self.assertRaisesRegex(PipelineError, "lacks required report"):
            seal(self.root, self.archive)

    def test_admission_refuses_foreign_archive_layout(self) -> None:
        with self.assertRaisesRegex(PipelineError, "missing"):
            admit(self.base / "absent.zip", self.base / "out-absent")

    def test_live_receipts_are_ignored_at_seal_and_never_transferred(self) -> None:
        write_json(self.root / "reports/stages.json", stage_view())
        write_json(self.root / "reports/run.json", {"run_id": RUN_ID})
        manifest = seal(self.root, self.archive)
        self.assertNotIn("reports/stages.json", manifest["files"])
        self.assertNotIn("reports/run.json", manifest["files"])
        self.assertIn("reports/native-stages.json", manifest["files"])

        broken = self.base / "live.zip"
        repack(self.archive, broken, {"reports/run.json": b"{}"})
        with self.assertRaisesRegex(PipelineError, "outside the capture payload"):
            admit(broken, self.base / "out-live")

    def test_streaming_large_member_roundtrips(self) -> None:
        payload = b"x" * (4 * 1024 * 1024)
        (self.root / "input/big.bin").write_bytes(payload)
        report = json.loads((self.root / "reports/input.json").read_text(encoding="utf-8"))
        report["package_files"]["big.bin"] = hashlib.sha256(payload).hexdigest()
        write_json(self.root / "reports/input.json", report)

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
                {"runtime": {**native_tool()["runtime"], "packages": {"numpy": "0.0.1"}}},
                "pin numpy",
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
                        "mass": support.mass_payload(1.0, (0.0, 0.0, 0.0)),
                    },
                    {
                        "name": "arm-1",
                        "transform": support.placement((0.0, 0.0, 0.2)),
                        "mass": support.mass_payload(0.5, (0.0, 0.0, 0.05)),
                    },
                ],
                dependencies=[work / "cad/base.SLDPRT", work / "cad/arm.SLDPRT"],
            )
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
            freeze(config, snapshot, backend=backend, worker_version="test-worker")

            capture = self.base / "real-capture"
            (capture / "input").mkdir(parents=True)
            write_json(capture / "input/robot.yaml", {"hardware_id": "robot"})
            shutil.copyfile(assembly, capture / "input" / assembly.name)
            shutil.copytree(snapshot, capture / "evidence")
            native_files = {
                name: file_digest(work / "cad" / name) for name in ("robot.SLDASM", "base.SLDPRT", "arm.SLDPRT")
            }
            write_json(
                capture / "input/discovery/native-discovery.json",
                {
                    "identity": {"main_assembly": assembly.name},
                    "handoff_sha256": HANDOFF,
                    "native_files": native_files,
                },
            )
            package_files = {
                name: file_digest(capture / "input" / name)
                for name in ("robot.yaml", assembly.name, "discovery/native-discovery.json")
            }
            write_json(
                capture / "reports/input.json",
                {
                    "passed": True,
                    "input_receipt": {
                        "inventory": [{"path": name, "sha256": sha} for name, sha in sorted(package_files.items())]
                    },
                    "cad_revision": {"revision": "r1"},
                    "package_files": package_files,
                },
            )
            write_json(capture / "reports/native-tool.json", native_tool())
            write_json(capture / "reports/native-stages.json", stage_view())

            archive = self.base / "real.zip"
            manifest = seal(capture, archive, main_assembly=assembly.name)
            self.assertIn("evidence/evidence/collection.json", manifest["files"])
            destination = self.base / "real-admitted"
            admit(archive, destination, expected_main_assembly=assembly.name)
            self.assertTrue((destination / "evidence/manifest.json").is_file())
            self.assertTrue((destination / transfer.CAPTURE_MANIFEST).is_file())
        finally:
            support.cleanup(work)

    def test_publisher_copies_the_native_provenance_files(self) -> None:
        from description_pipeline.repository.urdf_pr import _copy_governed

        bundle = self.base / "publish-bundle"
        for name in ("input", "evidence", "model", "urdf", "meshes"):
            (bundle / name).mkdir(parents=True)
        (bundle / "README.md").write_text("delivery\n", encoding="utf-8")
        write_json(bundle / "reports/input.json", {"passed": True})
        write_json(bundle / "reports/tool.json", {"role": "portable"})
        write_json(bundle / "reports/quality.json", {"passed": True})
        write_json(bundle / "reports/native-tool.json", native_tool())
        write_json(bundle / "reports/native-stages.json", stage_view())
        write_json(bundle / "transfer-manifest.json", {"schema_version": transfer.TRANSFER_SCHEMA})

        worktree = self.base / "publish-worktree"
        _copy_governed(bundle, worktree)
        self.assertTrue((worktree / "reports/native-tool.json").is_file())
        self.assertTrue((worktree / "reports/native-stages.json").is_file())
        self.assertTrue((worktree / transfer.CAPTURE_MANIFEST).is_file())

        neutral = self.base / "neutral-bundle"
        for name in ("input", "evidence", "model", "urdf", "meshes"):
            (neutral / name).mkdir(parents=True)
        (neutral / "README.md").write_text("delivery\n", encoding="utf-8")
        write_json(neutral / "reports/input.json", {"passed": True})
        write_json(neutral / "reports/tool.json", {"role": "portable"})
        neutral_worktree = self.base / "neutral-worktree"
        _copy_governed(neutral, neutral_worktree)
        self.assertFalse((neutral_worktree / "reports/native-tool.json").exists())
        self.assertFalse((neutral_worktree / transfer.CAPTURE_MANIFEST).exists())

    def test_subject_includes_transfer_provenance_only_as_a_complete_triplet(self) -> None:
        from description_pipeline.delivery import subject_digest, subject_inventory

        bundle = self.base / "subject-bundle"
        for name in ("input", "evidence", "model", "urdf", "meshes"):
            (bundle / name).mkdir(parents=True)
        (bundle / "README.md").write_text("delivery\n", encoding="utf-8")
        write_json(bundle / "reports/input.json", {"passed": True})
        write_json(bundle / "reports/tool.json", {"role": "portable"})

        neutral = subject_inventory(bundle)  # no transfer files: neutral fixtures stay valid
        self.assertNotIn(transfer.CAPTURE_MANIFEST, neutral)

        write_json(bundle / "reports/native-tool.json", native_tool())
        with self.assertRaisesRegex(PipelineError, "provenance is incomplete"):
            subject_inventory(bundle)

        write_json(bundle / "reports/native-stages.json", stage_view())
        write_json(bundle / transfer.CAPTURE_MANIFEST, {"schema_version": transfer.TRANSFER_SCHEMA})
        complete = subject_inventory(bundle)
        for name in ("reports/native-tool.json", "reports/native-stages.json", transfer.CAPTURE_MANIFEST):
            self.assertIn(name, complete)
        self.assertNotEqual(subject_digest(bundle), digest(neutral))

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
        self.assertEqual(transfer.verify_transfer(clean)["main_assembly"], MAIN_ASSEMBLY)

        neutral = self.base / "gate-neutral"
        (neutral / "input").mkdir(parents=True)
        (neutral / "evidence").mkdir()
        write_json(neutral / "reports/tool.json", {"role": "portable"})
        self.assertIsNone(transfer.verify_transfer(neutral))

        staged = bundle("gate-stages")
        view = stage_view()
        next(stage for stage in view["stages"] if stage["id"] == "capture")["checks_passed"] = 0
        reseal(staged, {"reports/native-stages.json": canonical(view)})
        with self.assertRaisesRegex(PipelineError, "receipts are incomplete"):
            transfer.verify_transfer(staged)

        retooled = bundle("gate-tool")
        bumped = {**native_tool(), "version": "9.9.9"}
        reseal(retooled, {"reports/native-tool.json": canonical(bumped)})
        with self.assertRaisesRegex(PipelineError, "another release"):
            transfer.verify_transfer(retooled)

        remade = bundle("gate-main")
        replacement = b"OTHER-BYTES"
        report = json.loads((remade / "reports/input.json").read_text(encoding="utf-8"))
        report["package_files"][MAIN_ASSEMBLY] = hashlib.sha256(replacement).hexdigest()
        reseal(
            remade,
            {
                "input/" + MAIN_ASSEMBLY: replacement,
                "reports/input.json": canonical(report),
            },
        )
        with self.assertRaisesRegex(PipelineError, "native file inventory"):
            transfer.verify_transfer(remade)

        removed = bundle("gate-removed")
        (removed / "reports/native-stages.json").unlink()
        with self.assertRaises(PipelineError):
            transfer.verify_transfer(removed)

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
        cases["incomplete-events"] = (missing_stage, "rebuilt from their own events")

        malformed = stage_view()
        malformed["events"] = [
            *malformed["events"],
            {
                "at": "2026-10-10T00:00:00+00:00",
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
        with self.assertRaisesRegex(PipelineError, "rebuilt from their own events"):
            seal(root, self.base / "fabricated.zip")

    def test_windows_evidence_paths_transport_onto_linux(self) -> None:
        """The real identity.assembly is a Windows absolute path; binding must not parse it."""

        root = capture_root(self.base / "winpath")
        windows = r"C:\native-discovery-20261007\gap-config-runs\prepared\robot.SLDASM"
        evidence = root / "evidence"
        collection_path = evidence / "evidence/collection.json"
        collection = json.loads(collection_path.read_text(encoding="utf-8"))
        collection["identity"]["assembly"] = windows
        payload = canonical(collection)
        collection_path.write_bytes(payload)
        manifest_path = evidence / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["identity"]["assembly"] = windows
        manifest["files"]["evidence/collection.json"] = hashlib.sha256(payload).hexdigest()
        manifest_path.write_bytes(canonical(manifest))

        archive = self.base / "winpath.zip"
        seal(root, archive)
        destination = self.base / "winpath-out"
        admit(archive, destination)
        self.assertEqual(transfer.verify_transfer(destination)["main_assembly"], MAIN_ASSEMBLY)

    def test_git_attributes_protect_the_transfer_sidecar(self) -> None:
        from description_pipeline.repository.urdf_pr import GIT_ATTRIBUTES

        self.assertIn(b"/transfer-manifest.json -text -filter -working-tree-encoding\n", GIT_ATTRIBUTES)
        if shutil.which("git") is None:
            self.skipTest("git is unavailable")
        repo = self.base / "attr-repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo), "config", "core.autocrlf", "true"], check=True, capture_output=True)
        (repo / ".gitattributes").write_bytes(GIT_ATTRIBUTES)
        (repo / "transfer-manifest.json").write_bytes(b"{}\n")
        (repo / "reports").mkdir()
        (repo / "reports/native-tool.json").write_bytes(b"{}\n")
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
                "transfer-manifest.json",
                "reports/native-tool.json",
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
