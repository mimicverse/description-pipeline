"""Application-service routing tests using the common engine and real artifact storage."""

from __future__ import annotations

import io
import json
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from description_pipeline import cli
from description_pipeline.artifacts import ArtifactStore
from description_pipeline.io import PipelineError, digest, write_json
from description_pipeline.pipeline.admission import build_run_admission
from description_pipeline.pipeline.application import (
    STAGES,
    AirflowTaskBinding,
    ApplicationError,
    ApplicationPortMissing,
    StageApplication,
    application_from_environment,
)
from description_pipeline.pipeline.stages.publish import PublishHandler
from description_pipeline.schemas import RUN_ADMISSION_SCHEMA
from description_pipeline.pipeline.contracts import (
    DiagnosticReplayScope,
    ExecutionClaim,
    StageCandidate,
    StageCommand,
    StageDecision,
    StageExecution,
    StageResult,
)
from description_pipeline.pipeline.engine import StageWorkspace
from description_pipeline.pipeline.reconciliation import EffectReconciliationService
from description_pipeline.pipeline.services import ClaimPolicy
from description_pipeline.pipeline.verify import NoExternalEffects
from description_pipeline.orchestration.native_operation_client import (
    NativeOperationEffectObserver,
)
from description_pipeline.schemas import (
    DISCOVER_RESULT_SCHEMA,
    STAGE_RESULT_SCHEMA,
    VERIFICATION_CERTIFICATE_SCHEMA,
    VERIFICATION_EXECUTION_SCHEMA,
    VERIFY_REQUEST_SCHEMA,
)
from tests.v1 import test_pipeline_admission as admission_tests


class _CompletingHandler:
    def __init__(self, native_tool=None) -> None:
        self.executions = 0
        self.native_tool = native_tool

    def execute(self, command, claim, workspace: StageWorkspace, *, effects):
        del effects
        self.executions += 1
        output = workspace.root / f"output-{claim.fence}"
        output.mkdir()
        if command.stage != "verify":
            (output / "stage.txt").write_text(command.stage, encoding="utf-8")
        artifacts = {item.kind: item for item in command.input_artifacts}
        if command.stage == "discover":
            source_files = dict(command.request["source_files"])
            write_json(
                output / "result.json",
                {
                    "schema_version": DISCOVER_RESULT_SCHEMA,
                    "command_id": command.command_id,
                    "run_id": command.run_id,
                    "request_digest": command.request_digest,
                    "selection": {
                        "files": source_files,
                        "handoff_sha256": digest(source_files),
                        "upload_sha256": command.request["source_inventory_sha256"],
                        "main_assembly": command.request["main_assembly"],
                    },
                    "prepared_sha256": digest({"prepared": command.command_id}),
                    "native_observation_sha256": digest({"observation": command.command_id}),
                    "passed": True,
                    "native_tool": dict(self.native_tool),
                    "source_set_artifact": artifacts["freeze_originals"].to_record(),
                    "ruleset_artifact": artifacts["freeze_ruleset"].to_record(),
                    "configuration_artifact": artifacts["platform_config"].to_record(),
                },
            )
        elif command.stage == "verify":
            generate = artifacts["stage_output:generate"]
            capture = artifacts["stage_output:capture"]
            verifier = {
                "executor": "verify",
                "source_sha256": command.expected_executor_sha256,
            }
            verifier_sha256 = verifier["source_sha256"]
            request = {
                "capture_artifact": capture.to_record(),
                "config_snapshot_sha256": command.config_snapshot_sha256,
                "contract_sha256": command.contract_sha256,
                "expected_verifier_sha256": command.expected_executor_sha256,
                "generate_artifact": generate.to_record(),
                "output_contract": "solidworks-to-urdf.verification-artifact/v1",
                "policy_sha256": command.policy_sha256,
                "purpose": command.request["purpose"],
                "schema_version": VERIFY_REQUEST_SCHEMA,
            }
            certificate = {
                "actual_verifier": verifier,
                "actual_verifier_sha256": verifier_sha256,
                "capture_artifact": capture.to_record(),
                "config_snapshot_sha256": command.config_snapshot_sha256,
                "contract_sha256": command.contract_sha256,
                "decision": "PASS",
                "generate_artifact": generate.to_record(),
                "passed": True,
                "policy_sha256": command.policy_sha256,
                "purpose": command.request["purpose"],
                "request": request,
                "request_sha256": digest(request),
                "schema_version": VERIFICATION_CERTIFICATE_SCHEMA,
                "subject_sha256": generate.set_sha256,
            }
            certificate["verification_key"] = digest(certificate)
            write_json(output / "verification-certificate.json", certificate)
            write_json(
                output / "verification-execution.json",
                {
                    "certificate_sha256": digest(certificate),
                    "schema_version": VERIFICATION_EXECUTION_SCHEMA,
                },
            )
        return StageExecution(
            outcome="completed",
            candidate_root=str(output),
            checks_sha256=digest({"stage": command.stage}),
            resources_settled=True,
        )

    def validate_candidate(self, command, claim, candidate, *, effects):
        del command, claim, effects
        return candidate


class _MemoryBackend:
    """In-memory fact/effect ledger with real ArtifactStore materialization and sealing."""

    def __init__(self, artifacts: ArtifactStore, root: Path) -> None:
        self.artifacts = artifacts
        self.root = root
        self.admission = None
        self.commands: dict[str, StageCommand] = {}
        self.dispatch_commands = {}
        self.decisions: dict[tuple[str, str], StageDecision] = {}
        self.results: dict[str, StageResult] = {}
        self.candidates: dict[str, StageCandidate] = {}
        self.fence = 0

    def load_admission(self, run_id: str):
        return self.admission if self.admission is not None and self.admission.run_id == run_id else None

    def admit_run(self, admission, command: StageCommand, dispatch_command, dispatch_outbound_artifact):
        self.admission = admission
        self.commands[command.command_id] = command
        self.dispatch_commands[dispatch_command.command_id] = dispatch_command
        if (
            dispatch_command.conf_artifact != dispatch_outbound_artifact
            or dispatch_command.freeze_command_id != command.command_id
            or dispatch_command.run_id != command.run_id
        ):
            raise PipelineError("dispatch identity conflict")
        conf = self.artifacts.read_dispatch_conf(dispatch_outbound_artifact)
        if conf.run_id != command.run_id or conf.freeze_command_id != command.command_id:
            raise PipelineError("dispatch conf identity conflict")
        return command, dispatch_command, None

    def load_command(self, command_id: str):
        try:
            return self.commands[command_id]
        except KeyError as error:
            raise PipelineError(f"unknown command: {command_id}") from error

    def load_dispatch_command(self, command_id: str):
        try:
            return self.dispatch_commands[command_id]
        except KeyError as error:
            raise PipelineError(f"unknown dispatch command: {command_id}") from error

    def create_command(
        self,
        command: StageCommand,
        *,
        admission,
        predecessor,
        supporting_predecessors=(),
        discover_result=None,
        verify_result=None,
        publish_intent=None,
    ):
        del discover_result, verify_result, publish_intent
        if admission != self.admission or command.predecessor_decision_id is None:
            raise PipelineError("successor command has no predecessor")
        stored_predecessor = self.decisions.get((command.run_id, predecessor.stage))
        if (
            stored_predecessor is None
            or stored_predecessor != predecessor.decision
            or stored_predecessor.decision_id != command.predecessor_decision_id
        ):
            raise PipelineError("successor predecessor is unavailable")
        if any(
            self.decisions.get((command.run_id, item.stage)) != item.decision
            for item in supporting_predecessors
        ):
            raise PipelineError("supporting predecessor is unavailable")
        existing = self.commands.get(command.command_id)
        if existing is not None and existing != command:
            raise PipelineError("command identity conflict")
        self.commands[command.command_id] = command
        return command

    def decision_for(self, command: StageCommand):
        decision = self.decisions.get((command.run_id, command.stage))
        return decision if decision is not None and decision.command_id == command.command_id else None

    def load_result(self, decision: StageDecision):
        return self.results[decision.result_digest]

    def verify_decision(self, decision: StageDecision, result: StageResult):
        if self.decisions.get((decision.run_id, decision.stage)) != decision or result.digest != decision.result_digest:
            raise PipelineError("decision is not bound")

    def acquire_claim(self, command: StageCommand):
        self.fence += 1
        return ExecutionClaim(
            claim_id=f"claim-{self.fence}",
            command_id=command.command_id,
            fence=self.fence,
            owner_sha256=digest({"owner": self.fence}),
            executor_sha256=command.expected_executor_sha256,
            lane_key=None,
            expires_at=datetime(2026, 10, 25, tzinfo=UTC) + timedelta(hours=1),
        )

    def candidate_for(self, command: StageCommand, claim: ExecutionClaim):
        del claim
        return self.candidates.get(command.command_id)

    def materialize(self, command: StageCommand, claim: ExecutionClaim, *, candidate):
        del candidate
        root = self.root / f"workspace-{claim.fence}"
        root.mkdir()
        inputs = root / "inputs"
        inputs.mkdir()
        materialized = {
            artifact.artifact_id: self.artifacts.materialize(
                artifact,
                inputs / artifact.artifact_id,
            )
            for artifact in command.input_artifacts
        }
        return StageWorkspace(root=root, inputs=materialized)

    def seal_candidate(self, command: StageCommand, claim: ExecutionClaim, execution: StageExecution):
        output = self.artifacts.seal_tree(
            execution.candidate_root,
            kind=f"stage_output:{command.stage}",
            producer_run_id=command.run_id,
            producer_stage=command.stage,
            producer_tool_sha256=claim.executor_sha256,
        )
        result = StageResult(
            schema_version=STAGE_RESULT_SCHEMA,
            command_id=command.command_id,
            run_id=command.run_id,
            stage=command.stage,
            request_digest=command.request_digest,
            outcome=execution.outcome,
            output_artifact=output,
            actual_executor_sha256=claim.executor_sha256,
            checks_sha256=execution.checks_sha256,
            resources_settled=execution.resources_settled,
            reason_code=execution.reason_code,
            diagnostic_scope=command.diagnostic_scope,
        )
        record = self.artifacts.seal_stage_result(result)
        candidate = StageCandidate(
            result=result,
            result_record_artifact=record,
            candidate_id=f"candidate-{result.digest}",
            claim_id=claim.claim_id,
            fence=claim.fence,
        )
        self.candidates[command.command_id] = candidate
        return candidate

    def verify_candidate(self, command: StageCommand, claim: ExecutionClaim, candidate: StageCandidate):
        del command, claim
        self.artifacts.verify(candidate.result.output_artifact)
        self.artifacts.verify(candidate.result_record_artifact)
        return candidate

    def effect_states_for_command(self, command_id: str):
        del command_id
        return {}

    def cancellation_for(self, command: StageCommand):
        del command
        return None

    def effect_port(self, command: StageCommand, claim: ExecutionClaim, workspace: StageWorkspace):
        del command, claim, workspace
        return NoExternalEffects()

    def commit_decision(self, command: StageCommand, claim: ExecutionClaim, candidate: StageCandidate):
        decision = StageDecision(
            decision_id=f"decision-{command.command_id}",
            run_id=command.run_id,
            stage=command.stage,
            command_id=command.command_id,
            result_record_artifact=candidate.result_record_artifact,
            result_digest=candidate.result.digest,
            actual_executor_sha256=claim.executor_sha256,
            outcome=candidate.result.outcome,
            original_producer_decision_id=None,
            reason_code=candidate.result.reason_code,
            failure_envelope=None,
            decided_at=datetime(2026, 10, 25, tzinfo=UTC),
        )
        self.decisions[(command.run_id, command.stage)] = decision
        self.results[candidate.result.digest] = candidate.result
        return decision

    def commit_failure(self, *args, **kwargs):
        raise AssertionError("failure is not expected")

    def discard_workspace(self, workspace: StageWorkspace):
        shutil.rmtree(workspace.root, ignore_errors=True)


class StageApplicationTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory(prefix="stage-application-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.harness = admission_tests.AdmissionHarness(self.root)
        self.backend = _MemoryBackend(self.harness.store, self.root / "work")
        self.backend.root.mkdir()
        self.freeze = _CompletingHandler()
        self.discover = _CompletingHandler(self.harness.native_tool)
        self.application = StageApplication(
            services=self.backend,
            facts=self.backend,
            artifacts=self.harness.store,
            handlers={"freeze": self.freeze, "discover": self.discover},
            builders={},
        )

    def test_admit_then_run_stage_uses_one_admission_and_real_engine(self):
        admitted = self.application.admit(self.harness.spec)
        self.assertEqual(admitted.admission, build_run_admission(self.harness.spec))
        self.assertEqual(admitted.dispatch_command.freeze_command_id, admitted.freeze_command.command_id)
        self.assertEqual(
            admitted.dispatch_command.expected_executor_sha256,
            self.harness.spec.dispatch_executor_sha256,
        )
        self.assertEqual(admitted.dispatch_command.dag_id, "solidworks_to_urdf")
        self.assertEqual(admitted.dispatch_command.dag_run_id, self.harness.spec.request_id)
        self.assertEqual(
            admitted.to_record()["admission"],
            {
                "created_at": self.harness.spec.created_at.isoformat(),
                "from_stage": self.harness.spec.from_stage,
                "parent_run_id": self.harness.spec.parent_run_id,
                "request": self.harness.spec.to_record(),
                "request_digest": admitted.admission.request_digest,
                "request_id": self.harness.spec.request_id,
                "run_id": self.harness.spec.run_id,
                "schema_version": RUN_ADMISSION_SCHEMA,
            },
        )
        self.assertEqual(
            self.harness.store.read_dispatch_conf(admitted.dispatch_command.conf_artifact).freeze_command_id,
            admitted.freeze_command.command_id,
        )
        self.assertEqual(
            self.backend.dispatch_commands[admitted.dispatch_command.command_id],
            admitted.dispatch_command,
        )
        freeze = self.application.run_stage("run-1", "freeze")
        self.assertIsInstance(freeze, StageResult)
        self.assertEqual(freeze.outcome, "completed")
        self.assertEqual(self.freeze.executions, 1)
        again = self.application.execute_registered_command(admitted.freeze_command.command_id)
        self.assertEqual(again, freeze)
        self.assertEqual(self.application.run_stage("run-1", "freeze"), freeze)
        self.assertEqual(self.freeze.executions, 1)

        discover = self.application.run_stage("run-1", "discover")
        self.assertIsInstance(discover, StageResult)
        self.assertEqual(discover.outcome, "completed")
        self.assertEqual(self.discover.executions, 1)
        self.assertEqual(set(self.backend.commands), {
            admitted.freeze_command.command_id,
            discover.command_id,
        })

    def test_linked_freeze_origins_require_the_parent_committed_closure(self):
        self.application.admit(self.harness.spec)
        with self.assertRaisesRegex(
            ApplicationError,
            "no completed Freeze decision",
        ):
            self.application.load_verified_freeze_origins("run-1")

    def test_airflow_binding_must_match_the_admitted_dispatch_and_stage(self):
        admitted = self.application.admit(self.harness.spec)
        self.backend.claim_policy = ClaimPolicy(
            owner_sha256="a" * 64,
            executor_sha256=self.harness.spec.executors.freeze,
            lease=timedelta(minutes=5),
            dag_id=admitted.dispatch_command.dag_id,
            dag_run_id="another-run",
            task_id="freeze",
            map_index=-1,
        )
        with self.assertRaisesRegex(ApplicationError, "admitted dispatch"):
            self.application.run_stage(self.harness.spec.run_id, "freeze")
        self.assertEqual(self.freeze.executions, 0)

        self.backend.claim_policy = replace(
            self.backend.claim_policy,
            dag_run_id=admitted.dispatch_command.dag_run_id,
            task_id="discover",
        )
        with self.assertRaisesRegex(ApplicationError, "requested stage"):
            self.application.run_stage(self.harness.spec.run_id, "freeze")
        self.assertEqual(self.freeze.executions, 0)

    def test_execute_run_resumes_after_a_completed_prefix(self):
        self.application.admit(self.harness.spec)
        freeze = self.application.run_stage("run-1", "freeze")
        outcome = self.application.execute_run("run-1", through="discover")
        self.assertEqual(outcome.outcome, "completed")
        self.assertEqual(outcome.stage, "discover")
        self.assertEqual(freeze.stage, "freeze")
        self.assertEqual(self.freeze.executions, 1)
        self.assertEqual(self.discover.executions, 1)

    def test_run_reports_exact_missing_capability_without_fallback(self):
        self.application.admit(self.harness.spec)
        with self.assertRaises(ApplicationPortMissing) as caught:
            self.application.execute_run("run-1", through="capture")
        self.assertEqual(caught.exception.detail["stage"], "capture")
        self.assertIn("capture", self.application.missing_capabilities()["handlers"])
        self.assertEqual(self.application.missing_capabilities()["builders"], [])
        self.assertEqual(
            self.application.missing_capabilities()["reconciliation"],
            ["native"],
        )

    def test_verified_discover_result_projects_capture_through_the_same_engine(self):
        capture = _CompletingHandler()
        application = StageApplication(
            services=self.backend,
            facts=self.backend,
            artifacts=self.harness.store,
            handlers={
                "freeze": self.freeze,
                "discover": self.discover,
                "capture": capture,
            },
            builders={},
        )
        application.admit(self.harness.spec)
        outcome = application.execute_run("run-1", through="capture")
        self.assertEqual(outcome.outcome, "completed")
        self.assertEqual(outcome.stage, "capture")
        self.assertEqual(capture.executions, 1)

    def test_execute_run_routes_all_six_stages_through_the_same_admission(self):
        handlers = {
            stage: _CompletingHandler(self.harness.native_tool if stage == "discover" else None)
            for stage in STAGES
        }
        application = StageApplication(
            services=self.backend,
            facts=self.backend,
            artifacts=self.harness.store,
            handlers=handlers,
            builders={},
        )
        application.admit(self.harness.spec)
        outcome = application.execute_run("run-1")
        self.assertEqual(outcome.outcome, "completed")
        self.assertEqual(outcome.stage, "publish")
        self.assertEqual(
            {stage: handler.executions for stage, handler in handlers.items()},
            dict.fromkeys(STAGES, 1),
        )

    def test_replay_refuses_until_common_command_scope_is_available(self):
        admitted = self.application.admit(self.harness.spec)
        with self.assertRaisesRegex(ApplicationError, "diagnostic qualification"):
            self.application.replay(admitted.freeze_command.command_id)

    def test_replay_executes_the_registered_diagnostic_command_once(self):
        formal = self.application.admit(self.harness.spec).freeze_command
        scope = DiagnosticReplayScope.from_inputs("freeze", formal.input_artifacts)
        diagnostic = StageCommand.create(
            command_id="diagnostic:freeze",
            run_id="diagnostic-run",
            stage="freeze",
            idempotency_key="diagnostic-run/freeze",
            request=formal.request,
            input_artifacts=formal.input_artifacts,
            config_snapshot_sha256=formal.config_snapshot_sha256,
            contract_sha256=formal.contract_sha256,
            policy_sha256=formal.policy_sha256,
            expected_executor_sha256=formal.expected_executor_sha256,
            predecessor_decision_id=None,
            diagnostic_scope=scope,
        )
        self.backend.commands[diagnostic.command_id] = diagnostic
        first = self.application.replay(diagnostic.command_id)
        second = self.application.replay(diagnostic.command_id)
        self.assertEqual(first, second)
        self.assertEqual(first.diagnostic_scope, scope)
        self.assertEqual(self.freeze.executions, 1)

    def test_admit_rejects_repository_metadata_detached_from_sealed_bytes(self):
        spec = self.harness.spec
        detached_snapshot = replace(
            spec.publish_intent.repository,
            expected_base_oid="2" * 40,
        )
        with self.assertRaisesRegex(ApplicationError, "Repository snapshot metadata differs"):
            self.application.admit(
                replace(
                    spec,
                    publish_intent=replace(spec.publish_intent, repository=detached_snapshot),
                )
            )

    def test_cli_dispatches_to_the_same_application(self):
        admitted = self.application.admit(self.harness.spec)
        with redirect_stdout(io.StringIO()):
            code = cli.main(
                ["stage", admitted.freeze_command.command_id],
                application_factory=lambda: self.application,
            )
        self.assertEqual(code, 0)

    def test_cli_admit_reports_both_atomic_commands(self):
        spec_path = self.root / "admission.json"
        write_json(spec_path, self.harness.spec.to_record())
        output = io.StringIO()
        with redirect_stdout(output):
            code = cli.main(
                ["admit", str(spec_path)],
                application_factory=lambda: self.application,
            )
        payload = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(payload["outcome"], "admitted")
        self.assertEqual(payload["admission"]["run_id"], self.harness.spec.run_id)
        self.assertIn("freeze_command", payload)
        self.assertIn("dispatch_command", payload)
        self.assertEqual(
            payload["dispatch_command"]["freeze_command_id"],
            payload["freeze_command"]["command_id"],
        )

    def test_environment_composition_uses_the_portable_source_identity(self):
        executor = "a" * 64
        token = self.root / "endpoint.token"
        token.write_text("t" * 40, encoding="utf-8")
        token.chmod(0o600)
        environment = {
            "PLATFORM_ARTIFACT_ROOT": str(self.root / "factory-cas"),
            "PLATFORM_SERVICE_DSN": "postgresql://unused/unused",
            "PIPELINE_WORK_ROOT": str(self.root / "factory-work"),
            "SOLIDWORKS_ENDPOINT_URL": "http://127.0.0.1:18765",
            "ENDPOINT_TOKEN_FILE": str(token),
        }
        with patch(
            "description_pipeline.pipeline.application.tool_record",
            return_value={"source_sha256": executor},
        ):
            application = application_from_environment(environment)
        self.assertEqual(application.services.claim_policy.executor_sha256, executor)
        self.assertIn("capture", application.handlers)
        self.assertIn("discover", application.handlers)
        self.assertIsInstance(application.handlers["publish"], PublishHandler)
        self.assertIsInstance(application.reconciler, EffectReconciliationService)
        self.assertIsInstance(
            application.reconciliation_adapter,
            NativeOperationEffectObserver,
        )
        self.assertEqual(application.missing_capabilities()["reconciliation"], [])

    def test_environment_composition_carries_the_explicit_airflow_task_binding(self):
        executor = "a" * 64
        token = self.root / "bound-endpoint.token"
        token.write_text("t" * 40, encoding="utf-8")
        token.chmod(0o600)
        environment = {
            "PLATFORM_ARTIFACT_ROOT": str(self.root / "bound-cas"),
            "PLATFORM_SERVICE_DSN": "postgresql://unused/unused",
            "PIPELINE_WORK_ROOT": str(self.root / "bound-work"),
            "SOLIDWORKS_ENDPOINT_URL": "http://127.0.0.1:18765",
            "ENDPOINT_TOKEN_FILE": str(token),
        }
        binding = AirflowTaskBinding(
            dag_id="solidworks_to_urdf",
            dag_run_id="portal-bound-run",
            task_id="freeze",
            map_index=-1,
        )
        with patch(
            "description_pipeline.pipeline.application.tool_record",
            return_value={"source_sha256": executor},
        ):
            application = application_from_environment(
                environment,
                airflow_task_binding=binding,
            )
        policy = application.services.claim_policy
        self.assertEqual(policy.dag_id, binding.dag_id)
        self.assertEqual(policy.dag_run_id, binding.dag_run_id)
        self.assertEqual(policy.task_id, binding.task_id)
        self.assertEqual(policy.map_index, binding.map_index)

    def test_airflow_task_binding_rejects_an_invalid_map_index(self):
        for value in (None, -2, 0.0, True):
            with self.subTest(value=value), self.assertRaisesRegex(
                ApplicationError,
                "map_index",
            ):
                AirflowTaskBinding(
                    dag_id="solidworks_to_urdf",
                    dag_run_id="portal-bound-run",
                    task_id="freeze",
                    map_index=value,
                )


if __name__ == "__main__":
    unittest.main()
