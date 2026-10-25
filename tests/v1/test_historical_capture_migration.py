"""Historical Capture input migration stays diagnostic and byte exact."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import tempfile
import unittest
import uuid
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

try:
    import pgserver
    import psycopg2
except ImportError:
    pgserver = None
    psycopg2 = None

from description_pipeline.artifacts import ArtifactStore
from description_pipeline.delivery import PIPELINE_ID
from description_pipeline.io import digest, file_digest, read_data, write_json
from description_pipeline.orchestration.native_operation_client import (
    capture_handler_from_native_operation,
    native_capture_operation_id,
    observe_native_operation,
)
from description_pipeline.orchestration.native_operation_runtime import (
    CAPTURE_KIND,
    NativeOperationFailure,
    NativeOperationRuntime,
)
from description_pipeline.pipeline.application import StageApplication
from description_pipeline.pipeline.capture import (
    CaptureInputs,
    CaptureRequest,
)
from description_pipeline.pipeline.contracts import (
    ArtifactRef,
    NativeTargetIdentity,
)
from description_pipeline.pipeline.diagnostic import (
    DiagnosticAdmissionSpec,
    build_diagnostic_command,
)
from description_pipeline.pipeline.engine import StageWorkspace
from description_pipeline.pipeline.services import (
    CanonicalStageServices,
    ClaimPolicy,
)
from description_pipeline.platform.facts import FactStore, RunControlStore
from description_pipeline.schemas import TOOL_SCHEMA
from tests.sources.selection_support import sealed_selection
from tests.v1.test_native_operations import _Server

ROOT = Path(__file__).resolve().parents[2]
MIGRATION_TOOL = ROOT / "tools" / "migrate_historical_capture_input.py"
PLATFORM_SCHEMA = ROOT / "deploy" / "platform" / "schema.sql"
PG_AVAILABLE = pgserver is not None and psycopg2 is not None
RUN_ID = "preflight-fixture-0123456789abcdef"
MAIN = "robot.SLDASM"
SOURCE_SHA256 = digest({"migration-source": 1})
TEST_INSTALL_ID = "win-" + "7" * 32


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
        install_id = mock.patch.object(
            NativeOperationRuntime,
            "_install_id",
            return_value=TEST_INSTALL_ID,
        )
        install_id.start()
        self.addCleanup(install_id.stop)
        self.native_target_identity = NativeTargetIdentity.create(
            install_id=TEST_INSTALL_ID,
            pipeline_id=PIPELINE_ID,
        )
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
                "schema_version": "solidworks-to-urdf.request/v2",
                "request_id": "request-fixture",
                "purpose": "inspection_preview",
                "change_summary": "Migrate a historical closure",
                "target_hardware": "m3.0",
                "main_assembly": MAIN,
                "handoff_digest": upload,
                "platform_config_digest": digest({"config": 1}),
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
            "schema_version": TOOL_SCHEMA,
            "source_sha256": SOURCE_SHA256,
            "runtime": {"role": "portable"},
        }
        self.native_tool_record = {
            "schema_version": TOOL_SCHEMA,
            "source_sha256": digest({"native-source": 1}),
            "runtime": {"role": "native"},
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
            self.native_target_identity,
            artifact_store=self.artifact_store,
            work_root=self.work_root,
            migration_tool=self.tool_record,
            native_tool=self.native_tool_record,
            diagnostic_created_at=datetime(
                2026,
                10,
                25,
                tzinfo=UTC,
            ),
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
        self.assertEqual(
            migration["current_native_target"],
            self.native_target_identity.to_record(),
        )
        for name in (MAIN, "part.SLDPRT", "source.json", "capture-plan.json"):
            self.assertEqual(
                (root / "legacy/prepared" / name).read_bytes(),
                (self.prepared / name).read_bytes(),
            )
        admission = DiagnosticAdmissionSpec.from_record(
            receipt["diagnostic_admission"]
        )
        command = build_diagnostic_command(admission)
        self.assertEqual(
            command.to_record(),
            receipt["diagnostic_command"],
        )
        self.assertEqual(
            command.request["native_target_identity"],
            self.native_target_identity.to_record(),
        )
        workspace = StageWorkspace(
            root=self.root / "diagnostic-workspace",
            inputs={
                artifact.artifact_id: store.materialize(
                    artifact,
                    self.root
                    / "diagnostic-workspace-inputs"
                    / artifact.artifact_id,
                )
                for artifact in admission.input_artifacts
            },
        )
        resolved = CaptureInputs.resolve(
            command,
            workspace,
            CaptureRequest.from_command(command),
        )
        self.assertEqual(
            resolved.historical_native_tool["source_sha256"],
            "a" * 64,
        )
        self.assertEqual(
            resolved.current_native_tool["source_sha256"],
            self.native_tool_record["source_sha256"],
        )
        self.assertEqual(
            resolved.plan["run_id"],
            RUN_ID,
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

    def test_plan_identity_drift_is_refused_before_sealing(self) -> None:
        path = self.prepared / "capture-plan.json"
        plan = read_data(path)
        plan["identity"] = {
            "hardware_id": "other-hardware",
            "revision": None,
        }
        write_json(path, plan)
        with self.assertRaisesRegex(
            HISTORICAL.HistoricalMigrationError,
            "identity_mismatch",
        ):
            self._migrate()

    def test_prepared_drift_is_refused_before_sealing(self) -> None:
        (self.prepared / "part.SLDPRT").write_bytes(b"changed after plan\n")
        with self.assertRaisesRegex(
            HISTORICAL.HistoricalMigrationError,
            "plan_prepared_mismatch",
        ):
            self._migrate()

    def test_missing_native_producer_evidence_is_refused(self) -> None:
        record = read_data(self.native_record)
        record.pop("source_selection")
        write_json(self.native_record, record)
        with self.assertRaisesRegex(
            HISTORICAL.HistoricalMigrationError,
            "producer_binding",
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
                self.native_target_identity,
                artifact_store=self.artifact_store,
                work_root=self.work_root,
                migration_tool=self.tool_record,
                native_tool=self.native_tool_record,
                diagnostic_created_at=datetime(
                    2026,
                    10,
                    25,
                    tzinfo=UTC,
                ),
                expected_run_id="fa1-" + "0" * 32,
            )

    @unittest.skipUnless(PG_AVAILABLE, "pgserver/psycopg2 are not installed")
    def test_migrated_closure_reaches_capture_through_the_common_engine(
        self,
    ) -> None:
        receipt = self._migrate()
        spec = DiagnosticAdmissionSpec.from_record(
            receipt["diagnostic_admission"]
        )
        command = build_diagnostic_command(spec)
        server_root = tempfile.TemporaryDirectory(
            prefix="historical-capture-postgres-"
        )
        self.addCleanup(server_root.cleanup)
        database = pgserver.get_server(
            Path(server_root.name) / "pgdata",
            cleanup_mode="delete",
        )
        self.addCleanup(database.cleanup)
        schema = "historical_capture_" + uuid.uuid4().hex
        schema_sql = PLATFORM_SCHEMA.read_text(encoding="utf-8").replace(
            ':"platform_schema"',
            f'"{schema}"',
        )
        with (
            psycopg2.connect(database.get_uri()) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(f'CREATE SCHEMA "{schema}"')
            cursor.execute(schema_sql)
            cursor.execute(
                f'UPDATE "{schema}".admission SET state = %s WHERE id = 1',
                ("open",),
            )

        def connect():
            return psycopg2.connect(
                database.get_uri(),
                options=f"-c search_path={schema},public",
            )

        store = ArtifactStore(self.artifact_store)
        facts = FactStore(connect, store)
        facts.admit_diagnostic(spec, command)
        observed: dict[str, object] = {}

        def controlled_native_boundary(
            request,
            source_archive,
            workspace,
            native_tool,
        ):
            observed.update(
                {
                    "request": dict(request),
                    "archive_sha256": file_digest(source_archive),
                    "archive_bytes": source_archive.stat().st_size,
                    "workspace": str(workspace),
                    "native_tool": dict(native_tool),
                }
            )
            raise NativeOperationFailure(
                "historical_capture.offline_boundary",
                "The controlled loopback boundary intentionally stops before CAD",
                resource_terminated=True,
                detail={
                    "diagnostic_only": True,
                    "formal_checkpoint_eligible": False,
                },
            )

        runtime = NativeOperationRuntime(
            state_root=self.root / "native-state",
            native_tool_factory=lambda: dict(
                self.native_tool_record
            ),
            executors={CAPTURE_KIND: controlled_native_boundary},
        )
        self.addCleanup(runtime.close)
        server = _Server(runtime)
        self.addCleanup(server.close)
        client = server.client()
        handler = capture_handler_from_native_operation(
            client,
            artifact_store=store,
            timeout=5,
            poll_interval=0.01,
        )
        services = CanonicalStageServices(
            facts=facts,
            artifacts=store,
            work_root=self.root / "engine-work",
            claim_policy=ClaimPolicy(
                owner_sha256=digest({"owner": "historical-capture"}),
                executor_sha256=spec.expected_executor_sha256,
                lease=timedelta(minutes=5),
            ),
            clock=lambda: spec.created_at,
        )
        application = StageApplication(
            services=services,
            facts=facts,
            artifacts=store,
            handlers={"capture": handler},
            builders={},
            clock=lambda: spec.created_at,
        )
        outcome = application.execute_registered_command(command.command_id)
        self.assertEqual(outcome.outcome, "failed")
        self.assertEqual(
            outcome.reason_code,
            "historical_capture.offline_boundary",
        )
        self.assertEqual(
            RunControlStore(connect).get_admitted_run(spec.run_id).state,
            "failed",
        )
        with (
            connect() as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(
                "SELECT outcome FROM stage_decision "
                "WHERE run_id = %s AND stage = 'capture'",
                (spec.run_id,),
            )
            self.assertEqual(cursor.fetchall(), [("failed",)])
        request = observed["request"]
        self.assertEqual(
            len(request["package_files"]),
            len(self.prepared_files()),
        )
        self.assertEqual(
            request["native_tool_source_sha256"],
            self.native_tool_record["source_sha256"],
        )
        self.assertEqual(
            observed["native_tool"],
            self.native_tool_record,
        )
        operation_id = native_capture_operation_id(
            run_id=spec.run_id,
            command_id=command.command_id,
            effect_key="capture-native",
        )
        view = observe_native_operation(client, operation_id)
        self.assertIsNotNone(view)
        self.assertEqual(view.state, "failed")
        self.assertEqual(view.input_archive_sha256, observed["archive_sha256"])
        self.assertEqual(
            view.actual_native_tool_sha256,
            digest(self.native_tool_record),
        )

    def prepared_files(self) -> dict[str, str]:
        return {
            path.relative_to(self.prepared).as_posix(): file_digest(path)
            for path in self.prepared.rglob("*")
            if path.is_file()
        }


if __name__ == "__main__":
    unittest.main()
