"""Application-service routing tests using the common engine and real artifact storage."""

from __future__ import annotations

import io
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta
from pathlib import Path

from description_pipeline import cli
from description_pipeline.artifacts import ArtifactStore
from description_pipeline.io import PipelineError, digest, write_json
from description_pipeline.pipeline.application import (
    STAGES,
    ApplicationError,
    ApplicationPortMissing,
    StageApplication,
)
from description_pipeline.pipeline.contracts import (
    ExecutionClaim,
    StageCandidate,
    StageCommand,
    StageDecision,
    StageExecution,
    StageResult,
)
from description_pipeline.pipeline.engine import StageWorkspace
from description_pipeline.pipeline.verify import NoExternalEffects
from tests.v1 import test_pipeline_admission as admission_tests


class _CompletingHandler:
    def __init__(self) -> None:
        self.executions = 0

    def execute(self, command, claim, workspace: StageWorkspace, *, effects):
        del effects
        self.executions += 1
        output = workspace.root / f"output-{claim.fence}"
        output.mkdir()
        (output / "stage.txt").write_text(command.stage, encoding="utf-8")
        if command.stage == "discover":
            source_files = dict(command.request["source_files"])
            artifacts = {item.kind: item for item in command.input_artifacts}
            write_json(
                output / "result.json",
                {
                    "schema_version": "description-pipeline.discover-result/v1",
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
                    "native_tool": {"executor": "native"},
                    "source_set_artifact": artifacts["freeze_originals"].to_record(),
                    "ruleset_artifact": artifacts["freeze_ruleset"].to_record(),
                    "configuration_artifact": artifacts["platform_config"].to_record(),
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
        self.decisions: dict[tuple[str, str], StageDecision] = {}
        self.results: dict[str, StageResult] = {}
        self.candidates: dict[str, StageCandidate] = {}
        self.fence = 0

    def load_admission(self, run_id: str):
        return self.admission if self.admission is not None and self.admission.run_id == run_id else None

    def admit_run(self, admission, command: StageCommand):
        self.admission = admission
        self.commands[command.command_id] = command
        return command

    def load_command(self, command_id: str):
        try:
            return self.commands[command_id]
        except KeyError as error:
            raise PipelineError(f"unknown command: {command_id}") from error

    def create_command(self, command: StageCommand):
        if command.predecessor_decision_id is None:
            raise PipelineError("successor command has no predecessor")
        expected = STAGES[STAGES.index(command.stage) - 1]
        predecessor = self.decisions.get((command.run_id, expected))
        if predecessor is None or predecessor.decision_id != command.predecessor_decision_id:
            raise PipelineError("successor predecessor is unavailable")
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
            schema_version="description-pipeline.stage-result/v1",
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
        self.discover = _CompletingHandler()
        self.application = StageApplication(
            services=self.backend,
            facts=self.backend,
            artifacts=self.harness.store,
            handlers={"freeze": self.freeze, "discover": self.discover},
            builders={},
        )

    def test_admit_then_run_stage_uses_one_admission_and_real_engine(self):
        admitted = self.application.admit(self.harness.spec)
        freeze = self.application.run_stage("run-1", "freeze")
        self.assertIsInstance(freeze, StageResult)
        self.assertEqual(freeze.outcome, "completed")
        self.assertEqual(self.freeze.executions, 1)
        again = self.application.execute_registered_command(admitted.command_id)
        self.assertEqual(again, freeze)
        self.assertEqual(self.freeze.executions, 1)

        discover = self.application.run_stage("run-1", "discover")
        self.assertIsInstance(discover, StageResult)
        self.assertEqual(discover.outcome, "completed")
        self.assertEqual(self.discover.executions, 1)
        self.assertEqual(set(self.backend.commands), {
            admitted.command_id,
            discover.command_id,
        })

    def test_run_reports_exact_missing_capability_without_fallback(self):
        self.application.admit(self.harness.spec)
        with self.assertRaises(ApplicationPortMissing) as caught:
            self.application.execute_run("run-1", through="capture")
        self.assertEqual(caught.exception.detail["stage"], "capture")
        self.assertIn("capture", self.application.missing_capabilities()["handlers"])
        self.assertEqual(self.application.missing_capabilities()["builders"], [])

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

    def test_replay_refuses_until_common_command_scope_is_available(self):
        command = self.application.admit(self.harness.spec)
        with self.assertRaisesRegex(ApplicationError, "diagnostic qualification"):
            self.application.replay(command.command_id)

    def test_cli_dispatches_to_the_same_application(self):
        command = self.application.admit(self.harness.spec)
        with redirect_stdout(io.StringIO()):
            code = cli.main(
                ["stage", command.command_id],
                application_factory=lambda: self.application,
            )
        self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
