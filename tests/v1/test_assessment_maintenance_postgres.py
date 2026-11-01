"""Actual PostgreSQL maintenance gates and snapshot-bound assessment recovery."""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from description_pipeline.artifacts import ArtifactStore
from description_pipeline.dynamics_assessment.contracts import AssessmentRequest, MemberRef
from description_pipeline.dynamics_assessment.queries import read_lifecycle_rows, read_recovery_artifacts
from description_pipeline.dynamics_assessment.store import AssessmentStore
from description_pipeline.io import digest
from description_pipeline.orchestration import lifecycle_state
from tests.v1.test_dynamics_assessment_contracts import AssessmentCase, request_case
from tests.v1.test_dynamics_assessment_postgres import postgres_server, register, result

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "deploy/operator"))
import recovery  # noqa: E402


class AssessmentMaintenancePostgresTests(AssessmentCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.server = cls.enterClassContext(postgres_server())

    def _new_database(self):
        import psycopg2

        name = "assessment_maintenance_" + uuid.uuid4().hex
        with contextlib.closing(psycopg2.connect(self.server.get_uri())) as connection:
            connection.autocommit = True
            with connection.cursor() as cursor:
                cursor.execute('CREATE DATABASE "' + name + '"')

        def cleanup():
            with contextlib.closing(psycopg2.connect(self.server.get_uri())) as connection:
                connection.autocommit = True
                with connection.cursor() as cursor:
                    cursor.execute('DROP DATABASE "' + name + '" WITH (FORCE)')

        self.addCleanup(cleanup)
        return name

    def connect(self, database=None):
        import psycopg2

        return psycopg2.connect(
            self.server.get_uri(database or self.database), options="-c search_path=platform,airflow,pg_catalog"
        )

    def setUp(self):
        super().setUp()
        self.database = self._new_database()
        with self.connect() as connection, connection.cursor() as cursor:
            cursor.execute("CREATE SCHEMA platform; CREATE SCHEMA airflow")
            cursor.execute((ROOT / "deploy/platform/schema.sql").read_text().replace(':"platform_schema"', '"platform"'))
            cursor.execute(
                "CREATE TABLE airflow.dag_run (dag_id text,run_id text,state text,start_date timestamptz,"
                "end_date timestamptz,PRIMARY KEY(dag_id,run_id));"
                "CREATE TABLE airflow.task_instance (dag_id text,run_id text,task_id text,map_index integer,"
                "state text,start_date timestamptz,end_date timestamptz,PRIMARY KEY(dag_id,run_id,task_id,map_index))"
            )
            cursor.execute(
                (ROOT / "deploy/platform/views.sql").read_text()
                .replace(':"platform_schema"', '"platform"').replace(':"airflow_schema"', '"airflow"')
            )
            cursor.execute("UPDATE admission SET state='open' WHERE id=1")
            cursor.execute(
                "INSERT INTO run_admission(run_id,request_id,request,request_digest) "
                "VALUES('source-run','source-dag-run',%s::jsonb,%s)",
                (json.dumps({"through_stage": "freeze"}), digest({"through_stage": "freeze"})),
            )
            cursor.execute(
                "INSERT INTO command(command_id,run_id,kind,stage,idempotency_key,request,request_digest,"
                "config_snapshot_sha256,contract_sha256,policy_sha256,expected_executor_sha256) "
                "VALUES('source-command','source-run','stage','freeze','source-key','{}',%s,%s,%s,%s,%s)",
                (digest({}),) * 5,
            )
            cursor.execute(
                "INSERT INTO stage_decision(decision_id,run_id,stage,command_id,outcome,actual_executor_sha256,"
                "reason_code,failure_envelope,failure_digest,decided_at) VALUES"
                "('source-decision','source-run','freeze','source-command','failed',%s,'fixture','{}',%s,now())",
                (digest({}), digest({})),
            )
        artifacts, self.request = request_case(self.root)
        self.store = AssessmentStore(self.connect, artifacts)
        self.store.platform.register_artifact(MemberRef.from_record(self.request.source.record["urdf"]).artifact)

    def _attempt(self, request=None):
        request = request or self.request
        return self.store.new_attempt(register(self.store, request, self.root))

    def _scheduling(self, attempt, state="running"):
        with self.connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO airflow.dag_run(dag_id,run_id,state,start_date) VALUES(%s,%s,%s,now())",
                ("upper_body_dynamics", attempt["dag_run_id"], state),
            )
            cursor.execute(
                "INSERT INTO airflow.task_instance(dag_id,run_id,task_id,map_index,state,start_date) "
                "VALUES(%s,%s,'assess',-1,%s,now())",
                ("upper_body_dynamics", attempt["dag_run_id"], state),
            )

    def _snapshot(self):
        with patch.dict(os.environ, {"PLATFORM_OPERATOR_DSN": self.connect_dsn()}):
            return lifecycle_state.snapshot()

    def connect_dsn(self):
        with contextlib.closing(self.connect()) as connection:
            return connection.dsn

    def _probe(self):
        with self.connect() as connection:
            connection.set_session(isolation_level="REPEATABLE READ", readonly=True)
            return recovery._maintenance_probe(connection)

    def _close(self):
        with self.connect() as connection, connection.cursor() as cursor:
            cursor.execute("UPDATE admission SET state='closed' WHERE id=1")

    def test_cancelled_queued_attempt_retains_exact_running_scheduler_debt(self):
        attempt = self._attempt()
        self.store.begin_dispatch(attempt["attempt_id"])
        self.store.dispatched(attempt["attempt_id"])
        self._scheduling(attempt)
        with self.connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO airflow.dag_run(dag_id,run_id,state) VALUES('foreign',%s,'running')",
                (attempt["dag_run_id"],),
            )
        cancelled = self.store.cancel(attempt["attempt_id"])
        self.assertTrue(cancelled["resources_settled"])
        self._close()
        self.assertEqual(len(self._snapshot()["in_flight"]), 2)
        self.assertEqual(self._probe()["unsettled_count"], 2)
        with self.connect() as connection, connection.cursor() as cursor:
            cursor.execute("UPDATE airflow.dag_run SET state='success' WHERE dag_id='upper_body_dynamics'")
            cursor.execute("UPDATE airflow.task_instance SET state='success'")
        self.assertTrue(self._snapshot()["drained"])
        recovery._require_quiescent(self._probe(), "actual_pg_after_scheduler_settlement")

    def test_registered_cancel_without_dispatch_has_no_phantom_scheduler_debt(self):
        attempt = self._attempt()
        self.store.cancel(attempt["attempt_id"])
        self._close()
        self.assertTrue(self._snapshot()["drained"])
        recovery._require_quiescent(self._probe(), "actual_pg_never_dispatched")

    def test_expired_lease_and_unknown_block_the_same_live_consumers(self):
        attempt = self._attempt()
        self.store.begin_dispatch(attempt["attempt_id"])
        self._scheduling(attempt)
        owner = digest({"owner": self.id()})
        self.store.claim(attempt["attempt_id"], owner, controller={"fixture": "controller"})
        with self.connect() as connection, connection.cursor() as cursor:
            cursor.execute("UPDATE assessment_attempt SET lease_until=now()-interval '1 second'")
        self._close()
        rows = self._snapshot()["unresolved"]
        self.assertEqual(rows[0]["state"], "unknown")
        self.assertEqual(rows[0]["detail"]["reason"], "owner_lease_expired")
        self.assertFalse(rows[0]["resources_settled"])
        with self.assertRaises(recovery.RecoveryError):
            recovery._require_quiescent(self._probe(), "actual_pg_expired_lease")
        self.store.unknown(attempt["attempt_id"], {"phase": "reconciliation"}, owner=owner)
        with patch.dict(os.environ, {"PLATFORM_OPERATOR_DSN": self.connect_dsn()}), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(lifecycle_state.main(["report", "--require-no-unresolved"]), 1)
        with self.connect() as connection:
            self.assertEqual(read_lifecycle_rows(connection)[0]["state"], "unknown")

    def test_previously_accepted_missing_dagrun_refuses_settlement(self):
        attempt = self._attempt()
        self.store.begin_dispatch(attempt["attempt_id"])
        self.store.dispatched(attempt["attempt_id"])
        self.store.cancel(attempt["attempt_id"])
        self._close()
        self.assertEqual(len(self._snapshot()["unknown"]), 1)
        with self.assertRaises(recovery.RecoveryError):
            recovery._require_quiescent(self._probe(), "actual_pg_missing_accepted_dagrun")

    def test_autocommit_probe_keeps_one_snapshot_across_concurrent_settlement(self):
        attempt = self._attempt()
        self._close()

        def settle_between_reads(connection):
            self.store.cancel(attempt["attempt_id"])
            return read_lifecycle_rows(connection)

        with contextlib.closing(self.connect()) as connection:
            connection.autocommit = True
            with patch.object(recovery, "read_assessment_lifecycle_rows", side_effect=settle_between_reads):
                before = recovery._maintenance_probe(connection)
            self.assertEqual(before["unsettled_count"], 1)
            self.assertEqual(before["unsettled"][0]["state"], "registered")
            self.assertTrue(recovery._maintenance_probe(connection)["settled"])

    def test_existing_read_committed_transaction_cannot_supply_a_probe_snapshot(self):
        with self.connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            with self.assertRaisesRegex(recovery.RecoveryError, "consistent caller snapshot"):
                recovery._maintenance_probe(connection)

    def test_existing_repeatable_read_snapshot_remains_owned_by_the_caller(self):
        attempt = self._attempt()
        self._close()
        with self.connect() as connection, connection.cursor() as cursor:
            connection.set_session(isolation_level="REPEATABLE READ", readonly=True)
            cursor.execute("SELECT state FROM admission WHERE id=1")
            self.store.cancel(attempt["attempt_id"])
            probe = recovery._maintenance_probe(connection)
            self.assertEqual(probe["unsettled_count"], 1)
            cursor.execute("SELECT state FROM assessment_attempt")
            self.assertEqual(cursor.fetchone(), ("registered",))
        self.assertTrue(self._probe()["settled"])

    def _bundle_request(self, *, register_bundle=True):
        folder = self.root / "unique-bundle"
        folder.mkdir()
        (folder / "execution.bin").write_bytes(b"unique assessment execution input\x00")
        bundle = self.store.artifacts.seal_tree(
            folder, kind="dynamics_execution_bundle", producer_run_id="source-run", producer_stage=None,
            producer_tool_sha256=self.request.record["executor_sha256"],
        )
        if register_bundle:
            self.store.platform.register_artifact(bundle)
        return AssessmentRequest({**self.request.to_record(), "bundle": bundle.to_record()})

    def test_unique_bundle_survives_exported_snapshot_dump_and_empty_target_restore(self):
        from pgserver._commands import POSTGRES_BIN_PATH

        record = self.request.to_record()
        source = record["source"]
        members = [(source, "urdf"), (source, "mjcf")]
        members.extend((source["certificates"], key) for key in ("urdf", "mjcf"))
        for index, (container, key) in enumerate(members):
            original = MemberRef.from_record(container[key])
            folder = self.root / ("source-member-" + str(index))
            folder.mkdir()
            (folder / original.path).write_bytes(original.read(self.store.artifacts))
            artifact = self.store.artifacts.seal_tree(
                folder, kind="assessment_source_fixture", producer_run_id="source-run", producer_stage=None,
                producer_tool_sha256=self.request.record["executor_sha256"],
            )
            self.store.platform.register_artifact(artifact)
            container[key] = MemberRef(artifact, original.path, original.bytes, original.sha256).to_record()
        self.request = AssessmentRequest(record)
        request = self._bundle_request()
        attempt = self._attempt(request)
        self.store.begin_dispatch(attempt["attempt_id"])
        owner = digest({"owner": "recovery"})
        self.store.claim(attempt["attempt_id"], owner, controller={"fixture": "controller"})
        self.store.finish(
            attempt["attempt_id"], owner, state="failed",
            artifact=result(self.store, request, self.root, attempt, "failed"), detail={},
        )
        self._scheduling(attempt, state="failed")
        self._close()
        recovery._require_quiescent(self._probe(), "actual_pg_before_snapshot_dump")
        archive = self.root / "database.dump"
        captured = self.root / "captured-artifacts"
        with self.connect() as snapshot:
            snapshot_id = recovery._begin_exported_snapshot(snapshot)
            root = recovery._recovery_root(snapshot, self.store.artifacts.root)
            self.assertEqual(len(root["artifacts"]), 7)
            self.assertIn(request.bundle.to_record(), root["artifacts"])
            selection = recovery.checkpoint_artifact_selection(root)
            recovery.copy_selected_root(self.store.artifacts.root, captured, selection)
            index = recovery.checkpoint_index(root, captured)
            subprocess.run(
                [str(POSTGRES_BIN_PATH / "pg_dump"), "--dbname", self.server.get_uri(self.database),
                 "--format=custom", "--snapshot=" + snapshot_id, "--file", str(archive)],
                check=True, capture_output=True, timeout=30,
            )
        target = self._new_database()
        subprocess.run(
            [str(POSTGRES_BIN_PATH / "pg_restore"), "--exit-on-error", "--dbname", self.server.get_uri(target), str(archive)],
            check=True, capture_output=True, timeout=30,
        )
        recovery.verify_checkpoint_index(index, captured)
        with self.connect(target) as connection:
            restored = recovery._recovery_root(connection, captured)
        self.assertEqual(restored, root)
        self.assertEqual(recovery._digest_document(restored), recovery._digest_document(root))
        restored_artifacts = ArtifactStore(captured)
        restored_store = AssessmentStore(lambda: self.connect(target), restored_artifacts)
        self.assertEqual(restored_store.request(attempt["request_id"]).sha256, request.sha256)
        self.assertEqual(
            restored_artifacts.read_metadata_member(request.bundle, "execution.bin", max_bytes=1024)[0],
            b"unique assessment execution input\x00",
        )

    def test_missing_nested_registry_reference_refuses_backup(self):
        request = self._bundle_request(register_bundle=False)
        self._attempt(request)
        with self.connect() as connection, self.assertRaisesRegex(ValueError, "absent from the artifact registry"):
            read_recovery_artifacts(connection, self.store.artifacts.root)

    def test_changed_complete_nested_reference_refuses_backup(self):
        request = self._bundle_request()
        record = request.to_record()
        record["bundle"]["producer_tool_sha256"] = digest({"changed": True})
        self._attempt(AssessmentRequest(record))
        with self.connect() as connection, self.assertRaisesRegex(ValueError, "complete registered ArtifactRef"):
            read_recovery_artifacts(connection, self.store.artifacts.root)


if __name__ == "__main__":
    unittest.main()
