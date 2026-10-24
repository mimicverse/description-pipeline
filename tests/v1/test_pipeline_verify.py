"""VerifyHandler tests over real GenerateHandler and ArtifactStore outputs."""

from __future__ import annotations

import copy
import shutil
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from description_pipeline.artifacts import ArtifactStore
from description_pipeline.interface_records import check_policy_digest
from description_pipeline.io import PipelineError, digest, file_digest, read_data, write_json
from description_pipeline.pipeline import verify as pipeline_verify
from description_pipeline.pipeline.contracts import (
    ExecutionClaim,
    StageCandidate,
    StageCommand,
    StageResult,
)
from description_pipeline.pipeline.engine import StageWorkspace
from description_pipeline.pipeline.stages.generate import (
    GENERATE_OUTPUT_CONTRACT,
    GENERATE_REQUEST_SCHEMA,
    GENERATE_RECEIPT,
    GenerateHandler,
)
from description_pipeline.pipeline.verify import (
    NoExternalEffects,
    CERTIFICATE_FILENAME,
    EXECUTION_RECEIPT_FILENAME,
    OUTPUT_CONTRACT,
    REQUEST_SCHEMA,
    VerifyHandler,
    replay_verification,
    validate_verification_certificate,
)
from description_pipeline.runtime import tool_record
from description_pipeline.schemas import STAGE_RESULT_SCHEMA
from description_pipeline.stages import CONTRACT_SHA256
from tests.v1 import test_hermetic_six_stage as hermetic


class PipelineVerifyHandlerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = hermetic.HermeticSixStageTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.directory = tempfile.TemporaryDirectory(prefix="verify-generate-pair-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.store = ArtifactStore(self.root / "cas")
        capture_source, _native = self.fixture._prepare_and_capture()
        capture_executor = digest({"executor": "capture-tool"})
        self.capture_ref = self.store.seal_tree(
            capture_source,
            kind="stage_output:capture",
            producer_run_id="capture-run",
            producer_stage="capture",
            producer_tool_sha256=capture_executor,
        )
        self.capture_root = self.store.materialize(self.capture_ref, self.root / "capture")
        self.frozen_request = read_data(self.capture_root / "input/request.json")
        self.verify_attempt = 0
        self.generate_executor = tool_record(role="portable")["source_sha256"]
        self.generator_command = StageCommand.create(
            command_id="generate-command",
            run_id="generate-run",
            stage="generate",
            idempotency_key="generate-command",
            request={
                "schema_version": GENERATE_REQUEST_SCHEMA,
                "capture_artifact_id": self.capture_ref.artifact_id,
                "output_contract": GENERATE_OUTPUT_CONTRACT,
            },
            input_artifacts=(self.capture_ref,),
            config_snapshot_sha256=str(self.frozen_request["platform_config_digest"]),
            contract_sha256=CONTRACT_SHA256,
            policy_sha256=check_policy_digest(),
            expected_executor_sha256=self.generate_executor,
            predecessor_decision_id="capture-decision",
        )
        with patch(
            "description_pipeline.steps.generate_model",
            side_effect=AssertionError("legacy Generate path must not run"),
        ):
            generated = GenerateHandler(self.store).execute(
                self.generator_command,
                self._claim(
                    self.generator_command,
                    owner=digest({"process": "generate"}),
                    executor=self.generate_executor,
                ),
                StageWorkspace(
                    root=self.root / "generate-attempt",
                    inputs={self.capture_ref.artifact_id: self.capture_root},
                ),
                effects=NoExternalEffects(),
            )
        self.generate_source = Path(generated.candidate_root)
        self.generate_ref = self.store.seal_tree(
            self.generate_source,
            kind="stage_output:generate",
            producer_run_id=self.generator_command.run_id,
            producer_stage="generate",
            producer_tool_sha256=self.generate_executor,
        )
        self.generate_root = self.store.materialize(self.generate_ref, self.root / "generate")

    @staticmethod
    def _claim(
        command: StageCommand,
        *,
        owner: str,
        executor: str,
        fence: int = 1,
    ) -> ExecutionClaim:
        return ExecutionClaim(
            claim_id=f"claim-{command.command_id}-{fence}",
            command_id=command.command_id,
            fence=fence,
            owner_sha256=owner,
            executor_sha256=executor,
            lane_key=None,
            expires_at=datetime(2026, 10, 25, tzinfo=UTC) + timedelta(hours=1),
        )

    def _verify_request(
        self,
        generate_ref=None,
    ) -> tuple[StageCommand, ExecutionClaim, StageWorkspace]:
        generate_ref = generate_ref or self.generate_ref
        verifier = hermetic._portable_tool()
        verifier_sha256 = digest(verifier)
        command = StageCommand.create(
            command_id="verify-command",
            run_id="verify-run",
            stage="verify",
            idempotency_key="verify-command",
            request={
                "capture_artifact_id": self.capture_ref.artifact_id,
                "generate_artifact_id": generate_ref.artifact_id,
                "output_contract": OUTPUT_CONTRACT,
                "purpose": self.frozen_request["purpose"],
                "schema_version": REQUEST_SCHEMA,
            },
            input_artifacts=(generate_ref, self.capture_ref),
            config_snapshot_sha256=str(self.frozen_request["platform_config_digest"]),
            contract_sha256=CONTRACT_SHA256,
            policy_sha256=check_policy_digest(),
            expected_executor_sha256=verifier_sha256,
            predecessor_decision_id="generate-decision",
        )
        claim = self._claim(
            command,
            owner=digest({"process": "verify"}),
            executor=verifier_sha256,
        )
        self.verify_attempt += 1
        if generate_ref == self.generate_ref:
            generate_root = self.generate_root
        else:
            variant = self.root / f"variant-generate-{generate_ref.artifact_id[:8]}"
            generate_root = variant if variant.exists() else self.store.materialize(generate_ref, variant)
        workspace = StageWorkspace(
            root=self.root / f"verify-{generate_ref.artifact_id[:8]}-{self.verify_attempt}",
            inputs={
                generate_ref.artifact_id: generate_root,
                self.capture_ref.artifact_id: self.capture_root,
            },
        )
        return command, claim, workspace

    def _run_verify(self, generate_ref=None):
        command, claim, workspace = self._verify_request(generate_ref)
        with (
            patch(
                "description_pipeline.steps.verify_delivery",
                side_effect=AssertionError("legacy Verify path must not run"),
            ),
        ):
            execution = VerifyHandler(
                identity_provider=hermetic._portable_tool,
                clock=lambda: datetime(2026, 10, 25, tzinfo=UTC),
            ).execute(command, claim, workspace, effects=NoExternalEffects())
        output = Path(execution.candidate_root)
        return (
            command,
            execution,
            read_data(output / CERTIFICATE_FILENAME),
            read_data(output / EXECUTION_RECEIPT_FILENAME),
            (output / CERTIFICATE_FILENAME).read_bytes(),
            command.input_artifacts[0],
            claim,
        )

    def _seal_mutated_generate(self, name: str, mutate) -> object:
        source = self.root / f"generate-source-{name}"
        shutil.copytree(self.generate_source, source)
        urdf = source / "urdf/robot.urdf"
        urdf.write_text(mutate(urdf.read_text(encoding="utf-8")), encoding="utf-8")
        receipt = read_data(source / GENERATE_RECEIPT)
        receipt["urdf_sha256"] = file_digest(urdf)
        write_json(source / GENERATE_RECEIPT, receipt)
        return self.store.seal_tree(
            source,
            kind="stage_output:generate",
            producer_run_id=self.generator_command.run_id,
            producer_stage="generate",
            producer_tool_sha256=self.generate_executor,
        )

    def test_real_generate_and_capture_pair_produces_deterministic_certificate(self) -> None:
        first = self._run_verify()
        second = self._run_verify()
        first_execution, first_certificate, first_receipt = first[1], first[2], first[3]
        second_certificate, second_receipt = second[2], second[3]
        self.assertEqual(first_execution.outcome, "completed")
        self.assertFalse(hasattr(first_execution, "required_effect_ids"))
        self.assertEqual(first_certificate["actual_verifier_sha256"], first[6].executor_sha256)
        self.assertNotEqual(first_certificate["actual_verifier_sha256"], first[6].owner_sha256)
        self.assertEqual(first_certificate, second_certificate)
        self.assertEqual(first[4], second[4])
        self.assertNotEqual(first_receipt["execution_id"], second_receipt["execution_id"])
        output = Path(first_execution.candidate_root)
        self.assertEqual(
            {path.name for path in output.iterdir()},
            {CERTIFICATE_FILENAME, EXECUTION_RECEIPT_FILENAME},
        )
        self.assertFalse((output / "model").exists())
        self.assertFalse((self.generate_root / "input").exists())
        self.assertFalse((self.generate_root / "evidence").exists())

    def test_validate_candidate_only_checks_runtime_identity(self) -> None:
        command, execution, certificate, _receipt, _bytes, _generate_ref, claim = self._run_verify()
        output_ref = self.store.seal_tree(
            execution.candidate_root,
            kind="stage_output:verify",
            producer_run_id=command.run_id,
            producer_stage="verify",
            producer_tool_sha256=claim.executor_sha256,
        )
        result = StageResult(
            schema_version=STAGE_RESULT_SCHEMA,
            command_id=command.command_id,
            run_id=command.run_id,
            stage="verify",
            request_digest=command.request_digest,
            outcome="completed",
            output_artifact=output_ref,
            actual_executor_sha256=claim.executor_sha256,
            checks_sha256=digest(certificate),
            resources_settled=True,
        )
        candidate = StageCandidate(result=result)
        with patch.object(
            pipeline_verify,
            "evaluate_bundle",
            side_effect=AssertionError("candidate validation must not replay mechanics"),
        ):
            validated = VerifyHandler(identity_provider=hermetic._portable_tool).validate_candidate(
                command,
                claim,
                candidate,
                effects=NoExternalEffects(),
            )
        self.assertIs(validated, candidate)

    def test_certificate_consumer_rechecks_pair_without_mechanical_replay(self) -> None:
        command, execution, certificate, _receipt, _bytes, generate_ref, _claim = self._run_verify()
        with (
            patch.object(pipeline_verify, "evaluate_bundle", side_effect=AssertionError("mechanical replay")),
            patch.object(
                pipeline_verify,
                "require_qualified_report",
                side_effect=AssertionError("mechanical replay"),
            ),
        ):
            validated = validate_verification_certificate(
                Path(execution.candidate_root),
                generate_root=self.generate_root,
                capture_root=self.capture_root,
                generate_artifact=generate_ref,
                capture_artifact=command.input_artifacts[1],
            )
        self.assertEqual(validated, certificate)

    def test_wrong_generate_receipt_capture_ref_is_rejected_before_evaluation(self) -> None:
        source = self.root / "generate-wrong-capture-ref"
        shutil.copytree(self.generate_source, source)
        receipt = read_data(source / GENERATE_RECEIPT)
        receipt["capture_artifact"]["artifact_id"] = "f" * 64
        write_json(source / GENERATE_RECEIPT, receipt)
        wrong_ref = self.store.seal_tree(
            source,
            kind="stage_output:generate",
            producer_run_id=self.generator_command.run_id,
            producer_stage="generate",
            producer_tool_sha256=self.generate_executor,
        )
        command, claim, workspace = self._verify_request(wrong_ref)
        with (
            patch.object(
                pipeline_verify,
                "evaluate_bundle",
                side_effect=AssertionError("evaluation must not start"),
            ),
            self.assertRaisesRegex(PipelineError, "different Capture artifact"),
        ):
            VerifyHandler(identity_provider=hermetic._portable_tool).execute(
                command,
                claim,
                workspace,
                effects=NoExternalEffects(),
            )

    def test_original_xml_mutations_fail_and_certificate_is_deterministic(self) -> None:
        def material_missing(payload: str) -> str:
            start = payload.index("<material")
            end = payload.index("</material>") + len("</material>")
            return payload[:start] + payload[end:]

        def visual_invalid_name(payload: str) -> str:
            return payload.replace('visual name="', 'visual name="BAD-', 1)

        def material_wrong_color(payload: str) -> str:
            return payload.replace('rgba="0.7 0.7 0.7 1.0"', 'rgba="1 0 0 1"', 1)

        for name, mutate in (
            ("material-missing", material_missing),
            ("visual-invalid-name", visual_invalid_name),
            ("material-wrong-color", material_wrong_color),
        ):
            generate_ref = self._seal_mutated_generate(name, mutate)
            _command, execution, certificate, _receipt, _bytes, _ref, _claim = self._run_verify(generate_ref)
            self.assertEqual(execution.outcome, "failed")
            self.assertEqual(certificate["decision"], "FAIL")
            failed = {row["id"] for row in certificate["checks"] if row["passed"] is not True}
            self.assertIn("urdf.syntax_names", failed)

        first_ref = self._seal_mutated_generate("deterministic-a", material_missing)
        first = self._run_verify(first_ref)
        second_ref = self._seal_mutated_generate("deterministic-b", material_missing)
        second = self._run_verify(second_ref)
        self.assertEqual(first_ref.artifact_id, second_ref.artifact_id)
        self.assertEqual(first[2], second[2])
        self.assertEqual(first[4], second[4])
        self.assertNotEqual(first[3]["execution_id"], second[3]["execution_id"])

    def test_consumer_rejects_closure_mutation_and_tampered_certificate(self) -> None:
        command, execution, certificate, _receipt, _bytes, generate_ref, _claim = self._run_verify()
        changed = self.root / "generate-changed"
        shutil.copytree(self.generate_root, changed)
        urdf = changed / "urdf/robot.urdf"
        urdf.write_bytes(urdf.read_bytes() + b"\n")
        with self.assertRaises(PipelineError):
            validate_verification_certificate(
                Path(execution.candidate_root),
                generate_root=changed,
                capture_root=self.capture_root,
                generate_artifact=generate_ref,
                capture_artifact=command.input_artifacts[1],
            )

        certificate_path = Path(execution.candidate_root) / CERTIFICATE_FILENAME
        signed = read_data(certificate_path)
        tampered = copy.deepcopy(signed)
        tampered["checks"][0]["passed"] = False
        write_json(certificate_path, tampered)
        with self.assertRaisesRegex(PipelineError, "proof digest differs"):
            validate_verification_certificate(
                Path(execution.candidate_root),
                generate_root=self.generate_root,
                capture_root=self.capture_root,
                generate_artifact=generate_ref,
                capture_artifact=command.input_artifacts[1],
            )
        write_json(certificate_path, certificate)

    def test_explicit_replay_executes_handler_and_matches_saved_certificate(self) -> None:
        command, _execution, certificate, _receipt, _bytes, generate_ref, _claim = self._run_verify()
        with patch.object(
            pipeline_verify,
            "evaluate_bundle",
            wraps=pipeline_verify.evaluate_bundle,
        ) as evaluator:
            replayed = replay_verification(
                generate_root=self.generate_root,
                capture_root=self.capture_root,
                generate_artifact=generate_ref,
                capture_artifact=command.input_artifacts[1],
                identity_provider=hermetic._portable_tool,
                clock=lambda: datetime(2026, 10, 25, tzinfo=UTC),
            )
        self.assertEqual(replayed, certificate)
        self.assertGreaterEqual(evaluator.call_count, 1)
        self.assertFalse(hasattr(pipeline_verify, "publish"))


if __name__ == "__main__":
    unittest.main()
