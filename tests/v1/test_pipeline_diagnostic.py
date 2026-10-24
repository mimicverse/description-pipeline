"""Diagnostic admission, scope and qualification remain outside formal lineage."""

from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from description_pipeline.io import digest
from description_pipeline.pipeline.admission import VerifiedPredecessor
from description_pipeline.pipeline.contracts import (
    ArtifactRef,
    DiagnosticReplayScope,
    FailureEnvelope,
    RunAdmission,
    StageCommand,
    StageDecision,
    StageQualification,
    StageResult,
    qualify_stage,
    require_formal_qualification,
)
from description_pipeline.pipeline.diagnostic import (
    DiagnosticAdmissionSpec,
    build_diagnostic_command,
    build_diagnostic_run_admission,
    validate_diagnostic_command,
)
from description_pipeline.pipeline.contracts import ProtocolError
from description_pipeline.platform.diagnostic import DiagnosticRuntimeConfig, open_diagnostic_stores
from description_pipeline.schemas import (
    DIAGNOSTIC_ADMISSION_SCHEMA,
    FAILURE_ENVELOPE_SCHEMA,
    STAGE_QUALIFICATION_SCHEMA,
    STAGE_RESULT_SCHEMA,
)


def artifact(name: str, *, run_id: str | None, stage=None, tool: str | None = None) -> ArtifactRef:
    return ArtifactRef(
        artifact_id=name,
        kind=f"stage_output:{stage}" if stage is not None else "fixture_input",
        locator=f"cas://artifact/{name}",
        manifest_sha256=digest({"manifest": name}),
        set_sha256=digest({"set": name}),
        producer_run_id=run_id,
        producer_stage=stage,
        producer_tool_sha256=tool or digest({"tool": name}),
    )


class DiagnosticProtocolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="diagnostic-protocol-")
        self.addCleanup(self.temporary.cleanup)
        self.now = datetime(2026, 10, 25, 10, 0, tzinfo=UTC)
        self.historical = artifact(
            "historical-capture",
            run_id="historical-run",
            stage="capture",
            tool=digest({"tool": "historical"}),
        )
        self.spec = DiagnosticAdmissionSpec(
            schema_version=DIAGNOSTIC_ADMISSION_SCHEMA,
            run_id="diagnostic-run",
            request_id="diagnostic-request",
            target_stage="capture",
            request={"schema_version": "diagnostic-fixture/v1", "mode": "read-only"},
            input_artifacts=(self.historical,),
            expected_executor_sha256=digest({"tool": "diagnostic-handler"}),
            config_snapshot_sha256=digest({"config": 1}),
            contract_sha256=digest({"contract": 1}),
            policy_sha256=digest({"policy": 1}),
            created_at=self.now,
        )
        self.command = build_diagnostic_command(self.spec)

    def diagnostic_result(self, command: StageCommand, *, scope: DiagnosticReplayScope | None = None) -> StageResult:
        output = artifact(
            "diagnostic-output",
            run_id=command.run_id,
            stage="capture",
            tool=command.expected_executor_sha256,
        )
        return StageResult(
            schema_version=STAGE_RESULT_SCHEMA,
            command_id=command.command_id,
            run_id=command.run_id,
            stage="capture",
            request_digest=command.request_digest,
            outcome="completed",
            output_artifact=output,
            actual_executor_sha256=command.expected_executor_sha256,
            checks_sha256=digest({"checks": 1}),
            resources_settled=True,
            diagnostic_scope=command.diagnostic_scope if scope is None else scope,
        )

    def test_diagnostic_spec_builds_one_predecessorless_scoped_command(self) -> None:
        admission = build_diagnostic_run_admission(self.spec)
        self.assertEqual(self.command.predecessor_decision_id, None)
        self.assertEqual(self.command.stage, "capture")
        self.assertEqual(self.command.input_artifacts, (self.historical,))
        self.assertIsNotNone(self.command.diagnostic_scope)
        self.assertEqual(self.command.diagnostic_scope.target_stage, "capture")
        self.assertEqual(
            self.command.diagnostic_scope.input_closure_sha256,
            digest([self.historical.to_record()]),
        )
        self.assertEqual(validate_diagnostic_command(self.spec, admission, self.command), self.command)
        self.assertEqual(DiagnosticAdmissionSpec.from_record(self.spec.to_record()), self.spec)
        self.assertEqual(StageCommand.from_record(self.command.to_record()), self.command)

    def test_diagnostic_scope_binds_result_failure_and_qualification(self) -> None:
        admission = build_diagnostic_run_admission(self.spec)
        result = self.diagnostic_result(self.command)
        self.assertEqual(StageResult.from_record(result.to_record()), result)
        qualification = qualify_stage(admission, self.command, result=result)
        self.assertEqual(qualification.scope_kind, "diagnostic")
        self.assertFalse(qualification.formal_eligible)
        self.assertEqual(StageQualification.from_record(qualification.to_record()), qualification)

        failure = FailureEnvelope(
            schema_version=FAILURE_ENVELOPE_SCHEMA,
            command_id=self.command.command_id,
            run_id=self.command.run_id,
            stage="capture",
            request_digest=self.command.request_digest,
            outcome="failed",
            actual_executor_sha256=self.command.expected_executor_sha256,
            reason_code="fixture.failed",
            error_type="FixtureFailure",
            error_message="fixture failed",
            error_detail=None,
            effect_states=(),
            evidence_artifacts=(),
            retained_locations=(),
            unavailable_reason=None,
            occurred_at=self.now,
            diagnostic_scope=self.command.diagnostic_scope,
        )
        self.assertEqual(FailureEnvelope.from_record(failure.to_record()), failure)
        failed_qualification = qualify_stage(admission, self.command, failure=failure)
        self.assertEqual(failed_qualification.scope_kind, "diagnostic")
        self.assertFalse(failed_qualification.formal_eligible)

    def test_formal_qualification_is_derived_only_without_diagnostic_scope(self) -> None:
        formal = StageCommand.create(
            command_id="formal-command",
            run_id="formal-run",
            stage="capture",
            idempotency_key="formal-run/capture",
            request={"schema_version": "fixture/v1"},
            input_artifacts=(self.historical,),
            config_snapshot_sha256=self.spec.config_snapshot_sha256,
            contract_sha256=self.spec.contract_sha256,
            policy_sha256=self.spec.policy_sha256,
            expected_executor_sha256=self.spec.expected_executor_sha256,
            predecessor_decision_id="formal-discover",
        )
        result = self.diagnostic_result(formal, scope=None)
        admission = RunAdmission.create(
            run_id="formal-run",
            request_id="formal-request",
            parent_run_id=None,
            from_stage="capture",
            request={"schema_version": "formal-fixture/v1"},
            created_at=self.now,
        )
        qualification = qualify_stage(admission, formal, result=result)
        self.assertEqual(qualification.scope_kind, "formal")
        self.assertTrue(qualification.formal_eligible)
        self.assertEqual(
            require_formal_qualification(qualification, admission, formal, result=result),
            qualification,
        )
        self.assertNotIn("diagnostic_scope", formal.to_record())
        self.assertNotIn("diagnostic_scope", result.to_record())

    def test_formal_consumer_cannot_strip_diagnostic_scope_or_rebind_record(self) -> None:
        admission = build_diagnostic_run_admission(self.spec)
        result = self.diagnostic_result(self.command)
        diagnostic = qualify_stage(admission, self.command, result=result)
        forged = StageQualification(
            schema_version=STAGE_QUALIFICATION_SCHEMA,
            command_id=diagnostic.command_id,
            run_id=diagnostic.run_id,
            stage=diagnostic.stage,
            admission_request_digest=diagnostic.admission_request_digest,
            request_digest=diagnostic.request_digest,
            command_digest=diagnostic.command_digest,
            record_digest=diagnostic.record_digest,
            outcome=diagnostic.outcome,
            diagnostic_scope=None,
        )
        self.assertTrue(forged.formal_eligible)
        self.assertEqual(StageQualification.from_record(forged.to_record()), forged)
        with self.assertRaisesRegex(ProtocolError, "qualification.formal_ineligible"):
            require_formal_qualification(forged, admission, self.command, result=result)

        stripped_result = StageResult(
            schema_version=result.schema_version,
            command_id=result.command_id,
            run_id=result.run_id,
            stage=result.stage,
            request_digest=result.request_digest,
            outcome=result.outcome,
            output_artifact=result.output_artifact,
            actual_executor_sha256=result.actual_executor_sha256,
            checks_sha256=result.checks_sha256,
            resources_settled=result.resources_settled,
            required_effect_ids=result.required_effect_ids,
            reason_code=result.reason_code,
            diagnostic_scope=None,
        )
        with self.assertRaises(ProtocolError):
            require_formal_qualification(diagnostic, admission, self.command, result=stripped_result)

    def test_diagnostic_record_cannot_register_as_formal_predecessor(self) -> None:
        result = self.diagnostic_result(self.command)
        record = artifact(
            "diagnostic-result-record",
            run_id=self.command.run_id,
            stage="capture",
            tool=self.command.expected_executor_sha256,
        )
        decision = StageDecision(
            decision_id="diagnostic-decision",
            run_id=self.command.run_id,
            stage="capture",
            command_id=self.command.command_id,
            result_record_artifact=record.__class__(
                artifact_id=record.artifact_id,
                kind="stage_result",
                locator=record.locator,
                manifest_sha256=record.manifest_sha256,
                set_sha256=record.set_sha256,
                producer_run_id=record.producer_run_id,
                producer_stage=record.producer_stage,
                producer_tool_sha256=record.producer_tool_sha256,
            ),
            result_digest=result.digest,
            actual_executor_sha256=result.actual_executor_sha256,
            outcome="completed",
            original_producer_decision_id=None,
            reason_code=None,
            failure_envelope=None,
            decided_at=self.now,
        )
        with self.assertRaises(ProtocolError):
            VerifiedPredecessor(
                stage="capture",
                command=self.command,
                result=result,
                decision=decision,
            )

    def test_wrong_input_closure_or_predecessor_is_rejected(self) -> None:
        other = artifact("other", run_id="historical-run", stage="capture")
        with self.assertRaises(ProtocolError):
            StageCommand.create(
                command_id="wrong-scope",
                run_id=self.spec.run_id,
                stage="capture",
                idempotency_key="diagnostic-run/wrong",
                request={"schema_version": "diagnostic-fixture/v1"},
                input_artifacts=(other,),
                config_snapshot_sha256=self.spec.config_snapshot_sha256,
                contract_sha256=self.spec.contract_sha256,
                policy_sha256=self.spec.policy_sha256,
                expected_executor_sha256=self.spec.expected_executor_sha256,
                predecessor_decision_id=None,
                diagnostic_scope=self.command.diagnostic_scope,
            )
        with self.assertRaises(ProtocolError):
            StageCommand.create(
                command_id="with-predecessor",
                run_id=self.spec.run_id,
                stage="capture",
                idempotency_key="diagnostic-run/with-predecessor",
                request={"schema_version": "diagnostic-fixture/v1"},
                input_artifacts=(self.historical,),
                config_snapshot_sha256=self.spec.config_snapshot_sha256,
                contract_sha256=self.spec.contract_sha256,
                policy_sha256=self.spec.policy_sha256,
                expected_executor_sha256=self.spec.expected_executor_sha256,
                predecessor_decision_id="fabricated-predecessor",
                diagnostic_scope=self.command.diagnostic_scope,
            )

    def test_private_runtime_config_has_no_production_environment_default(self) -> None:
        with self.assertRaisesRegex(Exception, "explicitly"):
            DiagnosticRuntimeConfig(database_dsn="", artifact_root=Path(self.temporary.name) / "cas")
        with self.assertRaisesRegex(Exception, "absolute"):
            DiagnosticRuntimeConfig(database_dsn="postgresql://private/test", artifact_root=Path("relative"))
        self.assertTrue(callable(open_diagnostic_stores))


if __name__ == "__main__":
    unittest.main()
