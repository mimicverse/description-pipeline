"""Two-leg discovery transfer: neutral roundtrip plus tamper and boundary refusals.

No native module, CAD session or Windows host is involved: the raw record comes
from the neutral discovery fixture and every admission stays local.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import unittest
import zipfile
from dataclasses import replace
from pathlib import Path
from unittest import mock

from description_pipeline.io import PipelineError, canonical, digest, inventory
from description_pipeline.orchestration import discovery_transfer as transfer
from description_pipeline.sources.solidworks.discovery import DiscoverySettings, derive_native_package
from description_pipeline.sources.solidworks.selection import seal_selection

from tests.sources.test_solidworks_discovery import record

SOURCE_FILES = ("cad/robot.SLDASM", "cad/base.SLDPRT", "cad/arm.SLDPRT")
MAIN_ASSEMBLY = "cad/robot.SLDASM"
RUN_ID = "portal-20261011T000000-abcdef12"
TOOL = {
    "schema_version": "solidworks-to-urdf.tool/v1",
    "pipeline_id": "test",
    "source_sha256": "ab" * 32,
}


class DiscoveryTransferTests(unittest.TestCase):
    """Seal, admit, derive and stage the two legs against the neutral fixture."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="sw-transfer-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.serial = 0
        self.source, self.records = self._scene()
        self.payload = record()
        self.selection = self._selection()
        self.tool = dict(TOOL)

    # -- fixtures ----------------------------------------------------------

    def _path(self, name: str) -> Path:
        self.serial += 1
        return self.tmp / f"{self.serial:02d}-{name}"

    def _scene(self) -> tuple[Path, Path]:
        root = self.tmp / "native"
        for name in SOURCE_FILES:
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(f"placeholder {name}\n".encode())
        records = self.tmp / "records"
        joints = records / "joints" / "arm.json"
        joints.parent.mkdir(parents=True, exist_ok=True)
        joints.write_text(
            json.dumps({"limits": {"lower": -1.5, "upper": 1.5}, "drive": {"effort": 6.0, "velocity": 2.0}}),
            encoding="utf-8",
        )
        (records / "budget.json").write_text(
            json.dumps({"robot": {"expected_mass_kg": [0.28, 0.32], "expected_extent_m": [0.35, 0.5]}}),
            encoding="utf-8",
        )
        return root, records

    def _selection(self, reader: str = "ab" * 32, *, decoys: bool = True) -> dict:
        files = inventory(self.source)
        upload = {**files}
        if decoys:
            upload.update({"notes.txt": "cd" * 32, "cad/spare.SLDPRT": "ef" * 32})
        observations = {
            MAIN_ASSEMBLY: {
                "sha256": files[MAIN_ASSEMBLY],
                "references": ["cad/base.SLDPRT", "cad/arm.SLDPRT"],
            },
            "cad/base.SLDPRT": {"sha256": files["cad/base.SLDPRT"], "references": []},
            "cad/arm.SLDPRT": {"sha256": files["cad/arm.SLDPRT"], "references": []},
        }
        return seal_selection(upload, MAIN_ASSEMBLY, observations, selected_files=files, reader_sha256=reader)

    def _settings(self) -> DiscoverySettings:
        return DiscoverySettings(record_roots=(self.records,))

    def _prepared(self):
        return derive_native_package(
            self.source, self._path("prepared"), RUN_ID, self.payload, settings=self._settings()
        )

    def _seal_record_bundle(self, payload: dict | None = None, **overrides) -> dict:
        kwargs = {"run_id": RUN_ID, "selection": self.selection, "native_tool": self.tool}
        kwargs.update(overrides)
        return transfer.seal_raw_record_bundle(
            self.payload if payload is None else payload, self._path("raw-record.zip"), **kwargs
        )

    def _seal_capture_request(self, prepared, **overrides) -> dict:
        kwargs = {"record": self.payload, "selection": self.selection, "native_tool": self.tool}
        kwargs.update(overrides)
        return transfer.seal_capture_request(prepared, self._path("capture-request.zip"), **kwargs)

    def _build_zip(self, items: dict[str, bytes], manifest: dict | None = None) -> Path:
        target = self._path("bundle.zip")
        data = dict(items)
        if manifest is not None:
            data[transfer.MANIFEST_MEMBER] = canonical(manifest)
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as sink:
            for name, payload in data.items():
                sink.writestr(name, payload)
        return target

    def _rewrite_zip(
        self,
        archive: Path,
        *,
        replacements: dict[str, bytes] | None = None,
        drop: tuple[str, ...] = (),
        extra: dict[str, bytes] | None = None,
    ) -> Path:
        with zipfile.ZipFile(archive) as source:
            items = {info.filename: source.read(info) for info in source.infolist()}
        for name, data in (replacements or {}).items():
            items[name] = data
        for name in drop:
            items.pop(name, None)
        for name, data in (extra or {}).items():
            items[name] = data
        return self._build_zip(items)

    # -- the positive two-leg roundtrip ------------------------------------

    def test_two_leg_roundtrip_matches_the_neutral_preparation(self) -> None:
        sealed = self._seal_record_bundle()
        archive = Path(sealed["artifact"]["path"])
        self.assertEqual(sealed["artifact"]["name"], transfer.RAW_RECORD_ARCHIVE)
        self.assertEqual(sealed["artifact"]["sha256"], hashlib.sha256(archive.read_bytes()).hexdigest())
        manifest = sealed["manifest"]
        self.assertEqual(
            set(manifest),
            {
                "schema_version",
                "run_id",
                "main_assembly",
                "main_assembly_sha256",
                "selection",
                "selection_sha256",
                "upload_sha256",
                "handoff_sha256",
                "record_sha256",
                "record_bytes",
                "native_tool",
                "native_tool_sha256",
                "files",
                "file_count",
                "total_bytes",
            },
        )
        digests = {manifest["selection_sha256"], manifest["upload_sha256"], manifest["handoff_sha256"]}
        self.assertEqual(len(digests), 3)
        admission = transfer.admit_raw_record_bundle(
            archive,
            expected_run_id=RUN_ID,
            expected_selection=self.selection,
            expected_native_tool=self.tool,
            expected_archive_sha256=sealed["artifact"]["sha256"],
        )
        self.assertEqual(admission.record, self.payload)
        self.assertEqual(admission.manifest, manifest)

        reference = derive_native_package(
            self.source, self._path("reference"), RUN_ID, self.payload, settings=self._settings()
        )
        with mock.patch(
            "description_pipeline.runtime.native_readiness",
            side_effect=AssertionError("the transfer must not consult the native runtime"),
        ):
            prepared = transfer.derive_from_raw_bundle(
                archive,
                self.source,
                self._path("linux-prepared"),
                expected_run_id=RUN_ID,
                expected_selection=self.selection,
                expected_native_tool=self.tool,
                settings=self._settings(),
            )
        self.assertTrue(prepared.passed)
        self.assertEqual(prepared.handoff_sha256, self.selection["handoff_sha256"])
        self.assertEqual(prepared.prepared_sha256, reference.prepared_sha256)
        self.assertEqual(inventory(prepared.package), inventory(reference.package))
        self.assertEqual(
            (prepared.package / "robot.yaml").read_bytes(),
            (reference.package / "robot.yaml").read_bytes(),
        )

        request = self._seal_capture_request(prepared)
        request_archive = Path(request["artifact"]["path"])
        with zipfile.ZipFile(request_archive) as source:
            names = set(source.namelist())
        self.assertEqual(names, set(request["manifest"]["files"]) | {transfer.MANIFEST_MEMBER})
        self.assertEqual(
            set(request["manifest"]["files"]),
            set(inventory(prepared.package)) - set(self.selection["files"]),
        )
        self.assertIn("robot.yaml", request["manifest"]["files"])
        for name in request["manifest"]["files"]:
            self.assertFalse(name.lower().endswith((".sldasm", ".sldprt", ".step", ".stp")))
            self.assertNotIn(name, self.selection["files"])

        destination = self._path("windows-prepared")
        for name in SOURCE_FILES:
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(self.source / name, target)
        rebuilt = transfer.stage_capture_request(
            request_archive,
            destination,
            expected_run_id=RUN_ID,
            expected_selection=self.selection,
            expected_record=self.payload,
            expected_native_tool=self.tool,
            expected_prepared_sha256=prepared.prepared_sha256,
        )
        self.assertTrue(rebuilt.passed)
        self.assertEqual(rebuilt.run_id, RUN_ID)
        self.assertEqual(rebuilt.handoff_sha256, self.selection["handoff_sha256"])
        self.assertEqual(rebuilt.prepared_sha256, prepared.prepared_sha256)
        self.assertEqual(rebuilt.discovery_sha256, prepared.discovery_sha256)
        self.assertEqual(inventory(rebuilt.package), inventory(prepared.package))
        self.assertEqual(digest(inventory(destination)), prepared.prepared_sha256)

    def test_full_closure_selection_without_decoys_roundtrips(self) -> None:
        selection = self._selection(decoys=False)
        self.assertEqual(selection["upload_sha256"], selection["handoff_sha256"])
        sealed = transfer.seal_raw_record_bundle(
            self.payload, self._path("raw-record.zip"), run_id=RUN_ID, selection=selection, native_tool=self.tool
        )
        admission = transfer.admit_raw_record_bundle(
            Path(sealed["artifact"]["path"]),
            expected_run_id=RUN_ID,
            expected_selection=selection,
            expected_native_tool=self.tool,
        )
        self.assertEqual(admission.record, self.payload)
        prepared = transfer.derive_from_raw_bundle(
            Path(sealed["artifact"]["path"]),
            self.source,
            self._path("prepared"),
            expected_run_id=RUN_ID,
            expected_selection=selection,
            expected_native_tool=self.tool,
            settings=self._settings(),
        )
        self.assertTrue(prepared.passed)
        request = self._seal_capture_request(prepared, selection=selection)
        destination = self._path("windows-prepared")
        for name in SOURCE_FILES:
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(self.source / name, target)
        rebuilt = transfer.stage_capture_request(
            Path(request["artifact"]["path"]),
            destination,
            expected_run_id=RUN_ID,
            expected_selection=selection,
            expected_record=self.payload,
            expected_native_tool=self.tool,
            expected_prepared_sha256=prepared.prepared_sha256,
        )
        self.assertEqual(digest(inventory(destination)), prepared.prepared_sha256)
        self.assertEqual(rebuilt.prepared_sha256, prepared.prepared_sha256)
        self.assertEqual(rebuilt.handoff_sha256, selection["handoff_sha256"])

    def test_stage_binds_the_embedded_record_not_the_manifest_claim(self) -> None:
        prepared = self._prepared()
        modified = self._path("modified-package")
        shutil.copytree(prepared.package, modified)
        discovery_path = modified / "discovery/native-discovery.json"
        payload = json.loads(discovery_path.read_bytes())
        payload["raw"]["identity"]["change_summary"] = "tampered"
        discovery_path.write_bytes(canonical(payload))
        polluted_identity = digest(inventory(modified))
        sealed = self._seal_capture_request(replace(prepared, package=modified, prepared_sha256=polluted_identity))
        destination = self._path("windows-prepared")
        for name in SOURCE_FILES:
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(self.source / name, target)
        with self.assertRaisesRegex(PipelineError, "another raw record"):
            transfer.stage_capture_request(
                Path(sealed["artifact"]["path"]),
                destination,
                expected_run_id=RUN_ID,
                expected_selection=self.selection,
                expected_record=self.payload,
                expected_native_tool=self.tool,
                expected_prepared_sha256=polluted_identity,
            )
        for name in sealed["manifest"]["files"]:
            self.assertFalse((destination / name).exists())

    def test_failed_staging_is_transactional(self) -> None:
        prepared = self._prepared()
        sealed = self._seal_capture_request(prepared)
        destination = self._path("windows-prepared")
        for name in SOURCE_FILES:
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(self.source / name, target)
        (destination / "unexpected.bin").write_bytes(b"x")
        with self.assertRaisesRegex(PipelineError, "derived prepared identity"):
            transfer.stage_capture_request(
                Path(sealed["artifact"]["path"]),
                destination,
                expected_run_id=RUN_ID,
                expected_selection=self.selection,
                expected_record=self.payload,
                expected_native_tool=self.tool,
                expected_prepared_sha256=prepared.prepared_sha256,
            )
        for name in sealed["manifest"]["files"]:
            self.assertFalse((destination / name).exists())

    def test_consistently_resealed_definition_cannot_hide_blocking_findings(self) -> None:
        prepared = self._prepared()
        modified = self._path("blocked-package")
        shutil.copytree(prepared.package, modified)
        discovery_path = modified / transfer.DISCOVERY_FILE
        payload = json.loads(discovery_path.read_bytes())
        payload["findings"].append({"code": "discovery.axis_unresolved", "blocking": True})
        discovery_path.write_bytes(canonical(payload))
        findings_path = modified / transfer.FINDINGS_FILE
        findings_document = json.loads(findings_path.read_bytes())
        findings_document["findings"] = payload["findings"]
        findings_path.write_bytes(canonical(findings_document))
        polluted_identity = digest(inventory(modified))
        sealed = self._seal_capture_request(replace(prepared, package=modified, prepared_sha256=polluted_identity))
        destination = self._path("windows-prepared")
        shutil.copytree(self.source, destination)
        with self.assertRaisesRegex(PipelineError, "blocking findings"):
            transfer.stage_capture_request(
                Path(sealed["artifact"]["path"]),
                destination,
                expected_run_id=RUN_ID,
                expected_selection=self.selection,
                expected_record=self.payload,
                expected_native_tool=self.tool,
                expected_prepared_sha256=polluted_identity,
            )
        self.assertEqual(inventory(destination), inventory(self.source))

    def test_resealed_record_with_invalid_suppression_state_is_refused(self) -> None:
        """The semantic check fires on a validly resealed bundle, not a digest mismatch."""

        cases = (
            ("state5", lambda payload: payload["components"][0].update(suppression_state=5)),
            ("missing", lambda payload: payload["components"][0].pop("suppression_state")),
            ("boolean", lambda payload: payload["components"][0].update(suppression_state=True)),
            ("incoherent", lambda payload: payload["components"][0].update(suppressed=True)),
        )
        for label, mutate in cases:
            with self.subTest(case=label):
                payload = json.loads(json.dumps(self.payload))
                mutate(payload)
                sealed = self._seal_record_bundle(payload=payload)
                prepared = transfer.derive_from_raw_bundle(
                    Path(sealed["artifact"]["path"]),
                    self.source,
                    self._path("prepared"),
                    expected_run_id=RUN_ID,
                    expected_selection=self.selection,
                    expected_native_tool=self.tool,
                    settings=self._settings(),
                )
                self.assertFalse(prepared.passed)
                codes = {finding["code"] for finding in prepared.findings}
                self.assertTrue(
                    codes & {"discovery.suppression_state_invalid", "discovery.suppression_state_incoherent"},
                    codes,
                )

    # -- leg 1 refusals ----------------------------------------------------

    def test_raw_record_refusals(self) -> None:
        sealed = self._seal_record_bundle()
        archive = Path(sealed["artifact"]["path"])
        good = {
            "expected_run_id": RUN_ID,
            "expected_selection": self.selection,
            "expected_native_tool": self.tool,
        }
        changed_record = json.loads(json.dumps(self.payload))
        changed_record["namespace"] = "other"
        tampered = self._rewrite_zip(archive, replacements={transfer.RECORD_MEMBER: canonical(changed_record)})
        with self.assertRaisesRegex(PipelineError, "record_sha256"):
            transfer.admit_raw_record_bundle(tampered, **good)
        broken = self._rewrite_zip(archive, replacements={transfer.RECORD_MEMBER: b"not json"})
        with self.assertRaisesRegex(PipelineError, "not readable JSON"):
            transfer.admit_raw_record_bundle(broken, **good)
        extra = self._rewrite_zip(archive, extra={"extra.txt": b"x"})
        with self.assertRaisesRegex(PipelineError, "member set is not exactly"):
            transfer.admit_raw_record_bundle(extra, **good)
        missing = self._rewrite_zip(archive, drop=(transfer.MANIFEST_MEMBER,))
        with self.assertRaisesRegex(PipelineError, "member set is not exactly"):
            transfer.admit_raw_record_bundle(missing, **good)
        traversal = self._rewrite_zip(archive, extra={"../evil.txt": b"x"})
        with self.assertRaisesRegex(PipelineError, "Escaping artifact path"):
            transfer.admit_raw_record_bundle(traversal, **good)
        other_run = self._rewrite_zip(
            archive,
            replacements={transfer.MANIFEST_MEMBER: canonical({**sealed["manifest"], "run_id": "other"})},
        )
        with self.assertRaisesRegex(PipelineError, "another run"):
            transfer.admit_raw_record_bundle(other_run, **good)
        with self.assertRaisesRegex(PipelineError, "another selection manifest"):
            transfer.admit_raw_record_bundle(archive, **{**good, "expected_selection": self._selection("12" * 32)})
        with self.assertRaisesRegex(PipelineError, "native tool record"):
            transfer.admit_raw_record_bundle(
                archive, **{**good, "expected_native_tool": {**self.tool, "version": "other"}}
            )
        with self.assertRaisesRegex(PipelineError, "transferred digest"):
            transfer.admit_raw_record_bundle(archive, **good, expected_archive_sha256="00" * 32)

    def test_seal_and_admit_enforce_the_record_size_limit(self) -> None:
        with (
            mock.patch.object(transfer, "MAX_RECORD_BYTES", 64),
            self.assertRaisesRegex(PipelineError, "record transfer limit"),
        ):
            self._seal_record_bundle()
        sealed = self._seal_record_bundle()
        with (
            mock.patch.object(transfer, "MAX_RECORD_BYTES", 64),
            self.assertRaisesRegex(PipelineError, "size limit"),
        ):
            transfer.admit_raw_record_bundle(
                Path(sealed["artifact"]["path"]),
                expected_run_id=RUN_ID,
                expected_selection=self.selection,
                expected_native_tool=self.tool,
            )

    def test_seal_binds_the_record_to_the_selection(self) -> None:
        with self.assertRaises(PipelineError):
            self._seal_record_bundle(payload={**self.payload, "files": {}})
        changed = json.loads(json.dumps(self.payload))
        changed["files"]["cad/unknown.SLDPRT"] = "ee" * 32
        with self.assertRaisesRegex(PipelineError, "not part of the selected staged bytes"):
            self._seal_record_bundle(payload=changed)

    def test_selection_manifest_is_verified_with_the_shared_verifier(self) -> None:
        with self.assertRaises(PipelineError):
            self._seal_record_bundle(selection={**self.selection, "excluded_files": {}})
        with self.assertRaises(PipelineError):
            self._seal_record_bundle(selection={**self.selection, "selection_sha256": "00" * 32})

    def test_derive_requires_the_frozen_source_to_be_the_selected_stage(self) -> None:
        sealed = self._seal_record_bundle()
        source = self._path("staged")
        shutil.copytree(self.source, source)
        (source / "cad/extra.SLDPRT").write_bytes(b"extra\n")
        with self.assertRaisesRegex(PipelineError, "Frozen source differs"):
            transfer.derive_from_raw_bundle(
                Path(sealed["artifact"]["path"]),
                source,
                self._path("derived"),
                expected_run_id=RUN_ID,
                expected_selection=self.selection,
                expected_native_tool=self.tool,
                settings=self._settings(),
            )

    # -- leg 2 refusals ----------------------------------------------------

    def test_capture_request_refusals(self) -> None:
        prepared = self._prepared()
        sealed = self._seal_capture_request(prepared)
        archive = Path(sealed["artifact"]["path"])
        good = {
            "expected_run_id": RUN_ID,
            "expected_selection": self.selection,
            "expected_record": self.payload,
            "expected_native_tool": self.tool,
            "expected_prepared_sha256": prepared.prepared_sha256,
        }
        tampered = self._rewrite_zip(archive, replacements={"robot.yaml": b"changed: true\n"})
        with self.assertRaisesRegex(PipelineError, "differs from its digest"):
            transfer.stage_capture_request(tampered, shutil.copytree(self.source, self._path("stage")), **good)
        with zipfile.ZipFile(archive) as source:
            items = {info.filename: source.read(info) for info in source.infolist()}
        polluted = {**items, "cad/robot.SLDASM": b"x"}
        polluted_manifest = dict(sealed["manifest"])
        polluted_manifest["files"] = {
            **polluted_manifest["files"],
            "cad/robot.SLDASM": hashlib.sha256(b"x").hexdigest(),
        }
        polluted_manifest["file_count"] = len(polluted_manifest["files"])
        polluted_manifest["total_bytes"] = sum(len(polluted[name]) for name in polluted_manifest["files"])
        with self.assertRaisesRegex(PipelineError, "unexpected generated member"):
            transfer.stage_capture_request(
                self._build_zip(polluted, polluted_manifest), shutil.copytree(self.source, self._path("stage")), **good
            )
        dropped = self._rewrite_zip(archive, drop=("robot.yaml",))
        with self.assertRaisesRegex(PipelineError, "member set differs from its manifest"):
            transfer.stage_capture_request(dropped, shutil.copytree(self.source, self._path("stage")), **good)
        without = {name: data for name, data in items.items() if name != "robot.yaml"}
        manifest = dict(sealed["manifest"])
        manifest["files"] = {name: value for name, value in manifest["files"].items() if name != "robot.yaml"}
        manifest["file_count"] = len(manifest["files"])
        manifest["total_bytes"] = sum(len(without[name]) for name in manifest["files"])
        required_missing = self._build_zip(without, manifest)
        with self.assertRaisesRegex(PipelineError, "lacks required generated members"):
            transfer.stage_capture_request(required_missing, shutil.copytree(self.source, self._path("stage")), **good)
        other_prepared = self._rewrite_zip(
            archive,
            replacements={transfer.MANIFEST_MEMBER: canonical({**sealed["manifest"], "prepared_sha256": "00" * 32})},
        )
        with self.assertRaisesRegex(PipelineError, "prepared identity"):
            transfer.stage_capture_request(other_prepared, shutil.copytree(self.source, self._path("stage")), **good)
        changed_record = json.loads(json.dumps(self.payload))
        changed_record["identity"]["change_summary"] = "changed"
        with self.assertRaisesRegex(PipelineError, "another raw record"):
            transfer.stage_capture_request(
                archive,
                shutil.copytree(self.source, self._path("stage")),
                **{**good, "expected_record": changed_record},
            )
        with self.assertRaisesRegex(PipelineError, "other selection digests"):
            transfer.stage_capture_request(
                archive,
                shutil.copytree(self.source, self._path("stage")),
                **{**good, "expected_selection": self._selection("12" * 32)},
            )
        with self.assertRaisesRegex(PipelineError, "transferred digest"):
            transfer.stage_capture_request(
                archive, shutil.copytree(self.source, self._path("stage")), **good, expected_archive_sha256="00" * 32
            )

    def test_capture_request_refuses_a_failed_derivation(self) -> None:
        prepared = self._prepared()
        refused = replace(prepared, passed=False)
        with self.assertRaisesRegex(PipelineError, "did not pass"):
            self._seal_capture_request(refused)
        partial = self._path("partial-package")
        shutil.copytree(prepared.package, partial)
        (partial / "robot.yaml").unlink()
        without_robot = replace(prepared, package=partial, prepared_sha256=digest(inventory(partial)))
        with self.assertRaisesRegex(PipelineError, "lacks required generated members"):
            self._seal_capture_request(without_robot)

    def test_capture_request_enforces_the_size_limit(self) -> None:
        prepared = self._prepared()
        with (
            mock.patch.object(transfer, "MAX_REQUEST_BYTES", 16),
            self.assertRaisesRegex(PipelineError, "transfer size limit"),
        ):
            self._seal_capture_request(prepared)


if __name__ == "__main__":
    unittest.main()
