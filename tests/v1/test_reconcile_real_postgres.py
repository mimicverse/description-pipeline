"""Real PostgreSQL proof for canonical run control and effect reconciliation."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import unittest
import uuid
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

from description_pipeline.io import digest
from description_pipeline.orchestration.airflow_client import native_run_id
from description_pipeline.orchestration.portal import PortalApp
from description_pipeline.pipeline.admission import (
    build_freeze_command,
    build_run_admission,
)
from description_pipeline.pipeline.contracts import (
    DispatchCommand,
    DispatchConf,
    ExecutionClaim,
    RunStatus,
    StageExecution,
)
from description_pipeline.pipeline.effects import EffectState
from description_pipeline.pipeline.engine import execute_stage
from description_pipeline.pipeline.reconciliation import (
    EffectReconciliationEvidence,
    EffectReconciliationService,
)
from description_pipeline.pipeline.services import CanonicalStageServices, ClaimPolicy
from description_pipeline.platform.facts import FactStore
from description_pipeline.platform.queries import read_run_projection
from description_pipeline.schemas import AIRFLOW_DISPATCH_CONF_SCHEMA
from tests.v1.test_pipeline_admission import AdmissionHarness

try:
    import psycopg2
except ImportError:
    psycopg2 = None


DAG_ID = "solidworks_to_urdf"
REQUIRED = {
    "airflow_dsn": os.environ.get("DESCRIPTION_RECONCILE_AIRFLOW_DSN", "").strip(),
    "platform_dsn": os.environ.get("DESCRIPTION_RECONCILE_PLATFORM_DSN", "").strip(),
    "evidence": os.environ.get("DESCRIPTION_RECONCILE_EVIDENCE_DIR", "").strip(),
}


class _EffectHandler:
    def execute(self, command, claim, workspace, *, effects):
        outbound = workspace.root / "outbound"
        outbound.mkdir()
        (outbound / "request.json").write_text(
            '{"operation":"freeze"}\n',
            encoding="utf-8",
        )
        intent = effects.prepare_effect(
            effect_key="native",
            effect_kind="native_operation",
            target={"endpoint": "real-postgres-proof", "operation": command.command_id},
            expected_state={"operation": command.command_id, "state": "complete"},
            outbound_root=outbound,
        )
        effects.authorize_dispatch(intent.effect_id, source="proof")

        response = workspace.root / "response"
        response.mkdir()
        (response / "response.json").write_text('{"state":"complete"}\n', encoding="utf-8")
        effects.record_response(
            intent.effect_id,
            source="proof",
            response_root=response,
        )

        observation = workspace.root / "observation"
        observation.mkdir()
        (observation / "observation.json").write_text(
            '{"state":"complete"}\n',
            encoding="utf-8",
        )
        effects.record_observation(
            intent.effect_id,
            source="proof",
            state_class="known",
            observation_root=observation,
        )

        output = workspace.root / "output"
        output.mkdir()
        (output / "freeze.json").write_text('{"stage":"freeze"}\n', encoding="utf-8")
        return StageExecution(
            outcome="completed",
            candidate_root=str(output),
            checks_sha256=digest({"checks": "freeze"}),
            resources_settled=True,
        )

    def validate_candidate(self, command, claim, candidate, *, effects):
        del command, claim, effects
        return candidate


class _KnownEffectAdapter:
    def observe(self, *, command, intent, outbound_root, workspace_root):
        del command, outbound_root
        payload = workspace_root / "reconciliation-known"
        payload.mkdir()
        (payload / "observation.json").write_text(
            '{"effect":"present"}\n',
            encoding="utf-8",
        )
        return EffectReconciliationEvidence(
            fact_type="reconciled",
            state_class="known",
            payload_root=payload,
            metadata={"effect_id": intent.effect_id},
        )


@unittest.skipUnless(
    psycopg2 is not None and all(REQUIRED.values()),
    "DESCRIPTION_RECONCILE_* isolation inputs are not configured",
)
class CanonicalReconcileRealPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.airflow_home = tempfile.mkdtemp(prefix="reconcile-postgres-home-")
        cls.original_environ = dict(os.environ)
        cls.addClassCleanup(cls._cleanup)
        os.environ["AIRFLOW_HOME"] = cls.airflow_home
        os.environ["AIRFLOW__DATABASE__SQL_ALCHEMY_CONN"] = REQUIRED["airflow_dsn"]
        os.environ["AIRFLOW__CORE__LOAD_EXAMPLES"] = "false"
        evidence_parent = Path(REQUIRED["evidence"])
        if not evidence_parent.is_absolute():
            raise AssertionError("DESCRIPTION_RECONCILE_EVIDENCE_DIR must be absolute")
        evidence_parent.mkdir(parents=True, exist_ok=True)
        cls.evidence = evidence_parent / f"canonical-reconcile-{uuid.uuid4().hex[:12]}"
        cls.evidence.mkdir()

    @classmethod
    def _cleanup(cls) -> None:
        os.environ.clear()
        os.environ.update(cls.original_environ)
        shutil.rmtree(cls.airflow_home, ignore_errors=True)

    def setUp(self) -> None:
        from airflow.models.dagrun import DagRun
        from airflow.models.taskinstance import TaskInstance
        from airflow.settings import Session

        suffix = uuid.uuid4().hex
        self.dag_run_id = f"portal-{suffix}"
        self.run_id = native_run_id(self.dag_run_id)
        self.request_id = f"request-{suffix}"
        self.harness = AdmissionHarness(self.evidence / f"admission-{suffix}")
        self.spec = replace(
            self.harness.spec,
            run_id=self.run_id,
            request_id=self.request_id,
            parent_run_id=None,
            from_stage="freeze",
        )
        self.artifacts = self.harness.store
        self.facts = FactStore(self._connect, self.artifacts)
        self.facts.open_admission(reason="reconcile proof", actor="reconcile-test")

        self.admission = build_run_admission(self.spec)
        self.command = build_freeze_command(self.spec)
        conf = DispatchConf(
            schema_version=AIRFLOW_DISPATCH_CONF_SCHEMA,
            run_id=self.run_id,
            freeze_command_id=self.command.command_id,
        )
        conf_artifact = self.artifacts.seal_dispatch_conf(
            conf,
            producer_run_id=self.run_id,
            producer_tool_sha256=self.spec.dispatch_executor_sha256,
        )
        dispatch = DispatchCommand.create(
            command_id=f"dispatch-{suffix}",
            run_id=self.run_id,
            freeze_command_id=self.command.command_id,
            idempotency_key=f"{self.run_id}/dispatch",
            dag_id=DAG_ID,
            dag_run_id=self.dag_run_id,
            conf_artifact=conf_artifact,
            expected_executor_sha256=self.spec.dispatch_executor_sha256,
        )
        self.facts.admit_run(
            self.admission,
            self.command,
            dispatch,
            conf_artifact,
        )

        with Session() as session:
            session.add(
                DagRun(
                    dag_id=DAG_ID,
                    run_id=self.dag_run_id,
                    run_type="manual",
                    state="running",
                )
            )
            session.flush()
            session.execute(
                TaskInstance.__table__.insert().values(
                    dag_id=DAG_ID,
                    run_id=self.dag_run_id,
                    task_id="freeze",
                    map_index=-1,
                    state="running",
                    try_number=1,
                    pool="default_pool",
                    pool_slots=1,
                )
            )
            session.commit()

        owner = digest({"process": suffix})
        self.now = self.spec.created_at
        self.services = CanonicalStageServices(
            facts=self.facts,
            artifacts=self.artifacts,
            work_root=self.evidence / f"work-{suffix}",
            claim_policy=ClaimPolicy(
                owner_sha256=owner,
                executor_sha256=self.spec.executors.freeze,
                lease=timedelta(minutes=5),
                dag_id=DAG_ID,
                dag_run_id=self.dag_run_id,
                task_id="freeze",
                map_index=-1,
            ),
            clock=lambda: self.now,
        )
        self.result = execute_stage(
            command_id=self.command.command_id,
            handler=_EffectHandler(),
            services=self.services,
        )

    def _connect(self):
        return psycopg2.connect(REQUIRED["platform_dsn"])

    def _query(self, statement: str, parameters=()):
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(statement, parameters)
            return cursor.fetchall()

    def _portal_projection(self) -> dict:
        portal = PortalApp.__new__(PortalApp)
        portal.config = SimpleNamespace(
            run_projection_factory=lambda run_id: read_run_projection(
                self._connect(),
                run_id,
                artifacts=self.artifacts,
            )
        )
        projection = portal._read_projection(self.run_id)
        self.assertIsNotNone(projection)
        return projection

    def _portal_control(self) -> PortalApp:
        portal = PortalApp.__new__(PortalApp)
        portal._run_control_store = self.facts
        portal._platform_lock = threading.Lock()
        portal.config = SimpleNamespace(run_projection_factory=None)
        return portal

    def test_freeze_claim_persists_the_real_airflow_task_binding(self) -> None:
        self.assertEqual(self.result.outcome, "completed")
        claim = self._query(
            """
            SELECT dag_id, dag_run_id, task_id, map_index
            FROM execution_claim
            WHERE command_id = %s
            ORDER BY fence DESC
            LIMIT 1
            """,
            (self.command.command_id,),
        )
        self.assertEqual(
            claim,
            [(DAG_ID, self.dag_run_id, "freeze", -1)],
        )

    def test_completed_freeze_remains_incomplete_in_control_and_portal_reads(self) -> None:
        summary = self.facts.get_admitted_run(self.run_id)
        self.assertIsNotNone(summary)
        self.assertEqual(summary.state, RunStatus.INCOMPLETE.value)
        projection = self._portal_projection()
        self.assertIn(
            projection["run"]["state"],
            {RunStatus.INCOMPLETE.value, "running"},
        )
        self.assertFalse(projection["formal_eligible"])
        job = PortalApp.__new__(PortalApp)._fact_display_job(projection)
        self.assertIsNotNone(job)
        self.assertFalse(job["formal_eligible"])

    def test_conflict_requires_reconciliation_and_cannot_downgrade_afterward(self) -> None:
        effect_id = self.facts.effect_ids_for_command(self.command.command_id)[0]
        self.facts.append_effect_fact(
            effect_id=effect_id,
            claim=None,
            fact_type="conflict",
            source="independent-audit",
            state_class=None,
            payload_artifact=None,
            payload_digest=digest({"conflict": effect_id}),
            observed_at=self.now + timedelta(minutes=1),
            fact_id=f"conflict-{effect_id}",
            expected_command_id=self.command.command_id,
        )
        summary = self.facts.get_admitted_run(self.run_id)
        self.assertEqual(summary.state, RunStatus.UNRESOLVED.value)
        self.assertEqual(
            self.facts.effect_state(effect_id),
            EffectState.CONFLICT,
        )

        reconciler = EffectReconciliationService(
            facts=self.facts,
            artifacts=self.artifacts,
            work_root=self.evidence / f"reconcile-work-{uuid.uuid4().hex}",
            observer_tool_sha256=digest({"observer": "known"}),
            clock=lambda: self.now + timedelta(minutes=2),
        )
        reconciled = reconciler.reconcile(
            command_id=self.command.command_id,
            effect_key="native",
            adapter=_KnownEffectAdapter(),
        )
        self.assertEqual(reconciled.settlement.state, EffectState.SATISFIED)
        projection = self._portal_projection()
        projected_effect = next(
            effect
            for effect in projection["technical"]["effects"]
            if effect["effect_id"] == effect_id
        )
        self.assertEqual(projected_effect["state"], "satisfied")
        claim = self._query(
            """
            SELECT claim_id, fence, owner_sha256, executor_sha256,
                   lane_key, expires_at
            FROM execution_claim
            WHERE command_id = %s
            ORDER BY fence DESC
            LIMIT 1
            """,
            (self.command.command_id,),
        )[0]
        old_claim = ExecutionClaim(
            claim_id=claim[0],
            command_id=self.command.command_id,
            fence=claim[1],
            owner_sha256=claim[2],
            executor_sha256=claim[3],
            lane_key=claim[4],
            expires_at=claim[5],
        )
        self.facts.append_effect_fact(
            effect_id=effect_id,
            claim=old_claim,
            fact_type="response",
            source="late-old-fence",
            state_class=None,
            payload_artifact=None,
            payload_digest=digest({"late_response": effect_id}),
            observed_at=self.now + timedelta(minutes=3),
            fact_id=f"late-response-{effect_id}",
            expected_command_id=self.command.command_id,
        )
        self.assertEqual(
            self.facts.effect_state(effect_id),
            EffectState.SATISFIED,
        )
        summary = self.facts.get_admitted_run(self.run_id)
        self.assertEqual(summary.state, RunStatus.INCOMPLETE.value)

    def test_run_control_display_metadata_is_canonical_and_reversible(self) -> None:
        renamed = self.facts.rename_run(self.run_id, "real postgres run")
        self.assertEqual(renamed.display_name, "real postgres run")
        hidden = self.facts.hide_run(self.run_id, actor="operator")
        self.assertIsNotNone(hidden.hidden_at)
        repeated = self.facts.hide_run(self.run_id, actor="another")
        self.assertEqual(repeated.hidden_at, hidden.hidden_at)
        self.assertEqual(repeated.hidden_reason, "operator")
        restored = self.facts.restore_run(self.run_id)
        self.assertIsNone(restored.hidden_at)
        self.assertIsNone(restored.hidden_reason)
        self.assertEqual(
            self.facts.get_run_display_metadata(self.run_id),
            restored,
        )
        portal = self._portal_control()
        self.assertEqual(
            portal._metadata_overlay(self.dag_run_id)["title"],
            "real postgres run",
        )
        self.assertIn(self.run_id, portal._metadata_snapshot())

        report = {
            "scope": "isolated canonical PostgreSQL/Airflow metadata",
            "run_id": self.run_id,
            "claim_binding": {
                "dag_id": DAG_ID,
                "dag_run_id": self.dag_run_id,
                "task_id": "freeze",
                "map_index": -1,
            },
            "run_state": self.facts.get_admitted_run(self.run_id).state,
        }
        path = self.evidence / "canonical-reconcile-report.json"
        path.write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(f"canonical reconcile report: {path}")


if __name__ == "__main__":
    unittest.main()
