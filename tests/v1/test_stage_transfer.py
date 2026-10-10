"""Transfer seal/admit: complete roundtrip plus tamper rejection; no Windows required."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from description_pipeline.build.archive import write_zip
from description_pipeline.delivery import PIPELINE_ID
from description_pipeline.io import PipelineError, canonical, file_digest, write_json
from description_pipeline.orchestration import stage_transfer as transfer
from description_pipeline.sources.snapshot import write_manifest
from description_pipeline.stages import CONTRACT, CONTRACT_FILE_SHA256, CONTRACT_SHA256, STAGE_IDS, VIEW_SCHEMA

RUN_ID = "20261010T000000-transfer"
HANDOFF = "a" * 64
MAIN_ASSEMBLY = "robot.SLDASM"


def native_tool() -> dict:
    return {
        "name": "solidworks-native-reader",
        "version": "1.3.1",
        "runtime": {"role": "native", "python": "3.12.10", "packages": {"numpy": "2.5.3"}},
    }


def stage_view(*, run_id: str = RUN_ID, handoff: str = HANDOFF, scope=("freeze", "discover", "capture")) -> dict:
    definitions = {stage["id"]: stage for stage in CONTRACT["stages"]}
    stages = []
    for stage_id in STAGE_IDS:
        definition = definitions[stage_id]
        in_scope = stage_id in scope
        state = "passed" if in_scope else "not_run"
        input_qc = [{"id": item["id"], "state": state, "details": {}} for item in definition["input_qc"]]
        output_qc = [{"id": item["id"], "state": state, "details": {}} for item in definition["output_qc"]]
        stages.append(
            {
                "id": stage_id,
                "state": "completed" if in_scope else "not_run",
                "in_scope": in_scope,
                "checks_passed": len(input_qc) + len(output_qc) if in_scope else 0,
                "checks_total": len(input_qc) + len(output_qc),
                "input_qc": input_qc,
                "output_qc": output_qc,
            }
        )
    return {
        "schema_version": VIEW_SCHEMA,
        "pipeline_id": PIPELINE_ID,
        "contract_sha256": CONTRACT_SHA256,
        "contract_file_sha256": CONTRACT_FILE_SHA256,
        "run_id": run_id,
        "handoff_sha256": handoff,
        "execution_scope": list(scope),
        "stages": stages,
    }


def capture_root(base: Path, *, run_id: str = RUN_ID, handoff: str = HANDOFF, main: str = MAIN_ASSEMBLY) -> Path:
    root = Path(base) / "capture"
    (root / "input").mkdir(parents=True)
    write_json(root / "input/robot.yaml", {"hardware_id": "robot"})
    (root / "input" / main).write_bytes(b"CAD-BYTES")
    package_files = {
        "robot.yaml": file_digest(root / "input/robot.yaml"),
        main: file_digest(root / "input" / main),
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
    write_json(
        evidence / "collection.json",
        {
            "identity": {"provider": "solidworks", "assembly": main, "dependency_digest": "c" * 64},
            "capture": {"originals_unchanged": True, "source_hashes": {main: {"sha256": "d" * 64}}},
        },
    )
    write_manifest(
        evidence,
        kind="solidworks",
        identity={"provider": "solidworks", "assembly": main},
        evidence_class="cad",
    )
    write_json(root / "reports/native-tool.json", native_tool())
    write_json(root / "reports/stages.json", stage_view(run_id=run_id, handoff=handoff))
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
        self.assertEqual(installed, set(manifest["files"]))
        self.assertEqual(
            file_digest(destination / "reports/native-tool.json"),
            manifest["files"]["reports/native-tool.json"],
        )

    def test_sealing_is_deterministic(self) -> None:
        first = self.base / "first.zip"
        second = self.base / "second.zip"
        seal(self.root, first)
        seal(self.root, second)
        self.assertEqual(first.read_bytes(), second.read_bytes())

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
            ("missing", {"remove": ("reports/stages.json",)}),
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
        repack(self.archive, broken, {"reports/stages.json": canonical(view)})
        with self.assertRaisesRegex(PipelineError, "receipts are incomplete"):
            admit(broken, self.base / "out-stages")

    def test_self_consistent_evidence_tamper_is_refused(self) -> None:
        seal(self.root, self.archive)
        with zipfile.ZipFile(self.archive) as source:
            collection = json.loads(source.read("evidence/collection.json"))
        collection["capture"]["originals_unchanged"] = False
        payload = canonical(collection)
        with zipfile.ZipFile(self.archive) as source:
            evidence_manifest = json.loads(source.read("evidence/manifest.json"))
        evidence_manifest["files"]["collection.json"] = hashlib.sha256(payload).hexdigest()
        broken = self.base / "evidence.zip"
        repack(
            self.archive,
            broken,
            {"evidence/collection.json": payload, "evidence/manifest.json": canonical(evidence_manifest)},
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
        broken = self.base / "main.zip"
        repack(self.archive, broken, {"input/" + MAIN_ASSEMBLY: b"OTHER-BYTES"})
        # The report hashes were updated by repack, so the archive is self-consistent; the
        # report's package_files no longer matches the input inventory.
        with self.assertRaisesRegex(PipelineError, "Archived input differs"):
            admit(self.base / "main.zip", self.base / "out-main")

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
            ("tool", {"expected_native_tool": {**native_tool(), "version": "9.9.9"}}, "another native tool"),
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
        cases.append(("wide-scope", wide_scope, "reports/stages.json", "native execution scope"))

        failed_check = stage_view()
        failed_check["stages"][2]["output_qc"][0]["state"] = "failed"
        failed_check["stages"][2]["checks_passed"] = failed_check["stages"][2]["checks_total"] - 1
        cases.append(("failed-check", failed_check, "reports/stages.json", "failed or unrun"))

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


if __name__ == "__main__":
    unittest.main()
