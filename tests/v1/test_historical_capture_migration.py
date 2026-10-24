"""Historical Capture input migration stays diagnostic and byte exact."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

from description_pipeline.artifacts import ArtifactStore
from description_pipeline.io import digest, file_digest, read_data, write_json
from description_pipeline.pipeline.contracts import ArtifactRef
from tests.sources.selection_support import sealed_selection

ROOT = Path(__file__).resolve().parents[2]
MIGRATION_TOOL = ROOT / "tools" / "migrate_historical_capture_input.py"
RUN_ID = "preflight-fixture-0123456789abcdef"
MAIN = "robot.SLDASM"
SOURCE_SHA256 = digest({"migration-source": 1})


def _load_tool():
    spec = importlib.util.spec_from_file_location(
        "historical_capture_migration",
        MIGRATION_TOOL,
    )
    if spec is None or spec.loader is None:
        raise AssertionError("Historical migration tool cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


HISTORICAL = _load_tool()


class HistoricalCaptureMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="historical-capture-migration-"
        )
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.prepared = self.root / "prepared"
        self.prepared.mkdir()
        (self.prepared / "discovery").mkdir()
        (self.prepared / MAIN).write_bytes(b"legacy assembly\n")
        (self.prepared / "part.SLDPRT").write_bytes(b"legacy part\n")
        selected = {
            name: file_digest(self.prepared / name)
            for name in (MAIN, "part.SLDPRT")
        }
        upload = digest(selected)
        selected_digest = digest(selected)
        selection = sealed_selection(
            selected,
            MAIN,
            reader_sha256="a" * 64,
        )
        source = {
            "identity": {
                "hardware_id": "m3.0",
                "revision": None,
                "main_assembly": MAIN,
                "handoff_digest": upload,
            }
        }
        raw_record = {
            "run_id": RUN_ID,
            "handoff_sha256": selected_digest,
            "files": selected,
            "source_selection": selection,
            "components": [
                {
                    "name2": "part-1",
                    "document": "part.SLDPRT",
                    "configuration": "Default",
                    "suppressed": False,
                    "suppression_state": 2,
                }
            ],
            "saved_configuration": "Default",
        }
        discovery = {
            "run_id": RUN_ID,
            "handoff_sha256": selected_digest,
            "native_files": selected,
            "raw": raw_record,
            "derivation": {"main_assembly": MAIN},
            "identity": {
                "hardware_id": "m3.0",
                "revision": None,
            },
        }
        resolution = {
            "run_id": RUN_ID,
            "handoff_sha256": selected_digest,
            "hardware_id": "m3.0",
            "revision": None,
        }
        plan = {
            "schema_version": HISTORICAL.PLAN_SCHEMA,
            "run_id": RUN_ID,
            "handoff_sha256": selected_digest,
            "discovery_sha256": digest(discovery),
            "main_assembly": MAIN,
            "identity": {
                "hardware_id": "m3.0",
                "revision": None,
            },
            "selection": selection,
            "objects": [
                {
                    "name": "part-1",
                    "document": "part.SLDPRT",
                    "state": 2,
                }
            ],
            "groups": [{"name": "body", "components": ["part-1"]}],
            "axis_requests": [],
            "datums": [],
            "evidence_requests": [
                "evidence/capture.json",
                "evidence/geometry/",
                "evidence/native-runtime.json",
            ],
            "configuration": "Default",
        }
        write_json(self.prepared / "source.json", source)
        write_json(
            self.prepared / "discovery/native-discovery.json",
            discovery,
        )
        write_json(
            self.prepared / "discovery/identity-resolution.json",
            resolution,
        )
        write_json(self.prepared / "discovery/findings.json", {"findings": []})
        write_json(self.prepared / "capture-plan.json", plan)
        self.native_record = self.root / "native-record.json"
        write_json(
            self.native_record,
            {
                "generator": "solidworks-native-reader",
                "files": selected,
                "source_selection": selection,
            },
        )
        self.source_request = self.root / "source-request.json"
        write_json(
            self.source_request,
            {
                "target_hardware": "m3.0",
                "main_assembly": MAIN,
                "handoff_digest": upload,
            },
        )
        self.capture_request = self.root / "capture-request.zip"
        archive_files = {
            "capture-plan.json": (self.prepared / "capture-plan.json").read_bytes(),
            "source.json": (self.prepared / "source.json").read_bytes(),
            "discovery/native-discovery.json": (
                self.prepared / "discovery/native-discovery.json"
            ).read_bytes(),
            "discovery/identity-resolution.json": (
                self.prepared / "discovery/identity-resolution.json"
            ).read_bytes(),
            "discovery/findings.json": (
                self.prepared / "discovery/findings.json"
            ).read_bytes(),
        }
        manifest = {
            "run_id": RUN_ID,
            "handoff_sha256": selected_digest,
            "main_assembly": MAIN,
            "upload_sha256": upload,
            "prepared_sha256": digest(
                {
                    path.relative_to(self.prepared).as_posix(): file_digest(path)
                    for path in self.prepared.rglob("*")
                    if path.is_file()
                }
            ),
            "files": {
                name: hashlib.sha256(payload).hexdigest()
                for name, payload in archive_files.items()
            },
            "native_tool": {"source_sha256": "a" * 64},
        }
        manifest["native_tool_sha256"] = digest(manifest["native_tool"])
        with zipfile.ZipFile(
            self.capture_request,
            "w",
            compression=zipfile.ZIP_DEFLATED,
        ) as archive:
            archive.writestr(
                "manifest.json",
                json.dumps(manifest, sort_keys=True, separators=(",", ":")),
            )
            for name, payload in archive_files.items():
                archive.writestr(name, payload)
        self.artifact_store = self.root / "artifacts"
        self.work_root = self.root / "work"
        self.work_root.mkdir()
        self.tool_record = {
            "schema_version": "solidworks-to-urdf.tool/v1",
            "source_sha256": SOURCE_SHA256,
            "runtime": {"role": "portable"},
        }

    def _migrate(self, **overrides):
        arguments = {
            "prepared": self.prepared,
            "native_record": self.native_record,
            "source_request": self.source_request,
            "capture_request": self.capture_request,
        }
        arguments.update(overrides)
        return HISTORICAL.migrate_historical_capture_input(
            HISTORICAL.MigrationInputs(**arguments),
            artifact_store=self.artifact_store,
            work_root=self.work_root,
            migration_tool=self.tool_record,
        )

    def test_complete_legacy_closure_becomes_an_isolated_byte_exact_artifact(
        self,
    ) -> None:
        receipt = self._migrate()
        artifact = receipt["artifact"]
        self.assertEqual(
            artifact["kind"],
            HISTORICAL.MIGRATION_KIND,
        )
        self.assertEqual(
            artifact["producer_tool_sha256"],
            SOURCE_SHA256,
        )
        store = ArtifactStore(self.artifact_store)
        reference = ArtifactRef.from_record(artifact)
        record = store.verify(reference)
        self.assertEqual(record["set_sha256"], artifact["set_sha256"])
        root = store.materialize(
            reference,
            self.root / "materialized",
        )
        migration = read_data(root / "migration.json")
        self.assertTrue(migration["diagnostic_only"])
        self.assertFalse(migration["execution_authorized"])
        self.assertFalse(migration["formal_checkpoint_eligible"])
        self.assertEqual(migration["source_label"], "preflight-fixture")
        self.assertEqual(
            migration["original_producer"]["reader_sha256"],
            "a" * 64,
        )
        self.assertIsNone(
            migration["original_producer"]["generator_version"]
        )
        self.assertEqual(
            migration["migration_tool"]["source_sha256"],
            SOURCE_SHA256,
        )
        for name in (MAIN, "part.SLDPRT", "source.json", "capture-plan.json"):
            self.assertEqual(
                (root / "legacy/prepared" / name).read_bytes(),
                (self.prepared / name).read_bytes(),
            )

    def test_plan_snapshot_drift_is_refused_before_sealing(self) -> None:
        path = self.prepared / "discovery/native-discovery.json"
        record = read_data(path)
        record["handoff_sha256"] = digest({"different": "snapshot"})
        write_json(path, record)
        with self.assertRaisesRegex(
            HISTORICAL.HistoricalMigrationError,
            "plan_snapshot_mismatch",
        ):
            self._migrate()

    def test_prepared_drift_is_refused_before_sealing(self) -> None:
        (self.prepared / "part.SLDPRT").write_bytes(b"changed after plan\n")
        with self.assertRaisesRegex(
            HISTORICAL.HistoricalMigrationError,
            "plan_prepared_mismatch",
        ):
            self._migrate()

    def test_source_label_cannot_be_upgraded_by_an_argument(self) -> None:
        with self.assertRaisesRegex(
            HISTORICAL.HistoricalMigrationError,
            "source_label_mismatch",
        ):
            HISTORICAL.migrate_historical_capture_input(
                HISTORICAL.MigrationInputs(
                    prepared=self.prepared,
                    native_record=self.native_record,
                    source_request=self.source_request,
                    capture_request=self.capture_request,
                ),
                artifact_store=self.artifact_store,
                work_root=self.work_root,
                migration_tool=self.tool_record,
                expected_run_id="fa1-" + "0" * 32,
            )


if __name__ == "__main__":
    unittest.main()
