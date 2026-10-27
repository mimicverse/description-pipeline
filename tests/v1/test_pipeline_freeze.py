"""Freeze handler and recovery regressions over the shared stage protocol."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from description_pipeline.interface_records import config_snapshot_digest
from description_pipeline.io import digest, file_digest, read_data, write_json
from description_pipeline.pipeline.contracts import (
    ArtifactRef,
    DiagnosticReplayScope,
    ExecutionClaim,
    NativeTargetIdentity,
    StageCandidate,
    StageCommand,
    StageDecision,
    StageResult,
    StageFailure,
)
from description_pipeline.pipeline.contracts import ProtocolError
from description_pipeline.pipeline.engine import StageWorkspace, execute_stage
from description_pipeline.pipeline.freeze import (
    CONFIGURATION_KIND,
    FREEZE_REQUEST_SCHEMA,
    FreezeRefusal,
    ORIGINALS_KIND,
    RULESET_KIND,
    FreezeHandler,
    RulesetCatalog,
    RulesetRoot,
)
from description_pipeline.schemas import (
    CONFIG_SNAPSHOT_SCHEMA,
    PLATFORM_CONFIG_SCHEMA,
    STAGE_RESULT_SCHEMA,
)


def _members(root: Path) -> list[dict]:
    return [
        {"bytes": path.stat().st_size, "path": path.relative_to(root).as_posix(), "sha256": file_digest(path)}
        for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix())
        if path.is_file()
    ]


def _artifact(kind: str, root: Path) -> ArtifactRef:
    members = _members(root)
    artifact_id = digest({"kind": kind, "set_sha256": digest({"members": members})})
    return ArtifactRef(
        artifact_id=artifact_id,
        kind=kind,
        locator=f"cas://artifact/{artifact_id}",
        manifest_sha256=digest({"manifest": artifact_id}),
        set_sha256=digest({"members": members}),
        producer_run_id=None,
        producer_stage=None,
        producer_tool_sha256=digest({"producer": kind}),
    )


class CountingFreezeHandler(FreezeHandler):
    def __init__(self) -> None:
        self.executions = 0
        self.validations = 0

    def execute(self, command, claim, workspace, *, effects):
        self.executions += 1
        return super().execute(command, claim, workspace, effects=effects)

    def validate_candidate(self, command, claim, candidate, *, effects):
        self.validations += 1
        return super().validate_candidate(command, claim, candidate, effects=effects)


class NoEffects:
    """Freeze declares no effects and must never query the capability."""

    def __getattr__(self, name):
        raise AssertionError(f"Freeze touched effect capability: {name}")


class FreezeHarness:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.originals = root / "originals"
        self.ruleset = root / "ruleset"
        self.configuration = root / "configuration"
        self.originals.mkdir()
        self.ruleset.mkdir()
        self.configuration.mkdir()
        (self.originals / "top.SLDASM").write_bytes(b"assembly-bytes")
        (self.originals / "parts").mkdir()
        (self.originals / "parts" / "part.SLDPRT").write_bytes(b"part-bytes")
        rules_root = self.ruleset / "root-000"
        rules_root.mkdir()
        (rules_root / "arm.json").write_text('{"value":1}\n', encoding="utf-8")
        members = {"arm.json": file_digest(rules_root / "arm.json")}
        roots = (RulesetRoot(path="root-000", members=members),)
        catalog = RulesetCatalog(
            version="records-" + digest([root.to_record() for root in roots]),
            roots=roots,
            records={"root-000/arm.json": members["arm.json"]},
        )
        self.ruleset_version = catalog.version
        write_json(self.ruleset / "catalog.json", catalog.to_record())
        self.state = root / "state"
        self.handoff = root / "handoff"
        self.artifact_root = root / "artifacts"
        self.workspace_root = root / "workspace"
        self.release = root / "release"
        self.records = root / "records"
        self.checkout = self.workspace_root / "robot"
        for path in (
            self.state,
            self.handoff,
            self.artifact_root,
            self.workspace_root,
            self.release,
            self.records,
            self.checkout,
        ):
            path.mkdir(exist_ok=True)
        self.token = root / "token"
        self.secret = root / "secret"
        self.certificate = root / "certificate.pem"
        self.private_key = root / "private-key.pem"
        self.dsn = root / "database-dsn"
        for path, value in (
            (self.token, "t" * 64),
            (self.secret, "s" * 64),
            (self.certificate, "certificate"),
            (self.private_key, "key"),
            (self.dsn, "postgresql://fixture"),
        ):
            path.write_text(value, encoding="utf-8")
        self.snapshot = {
            "schema_version": CONFIG_SNAPSHOT_SCHEMA,
            "install_id": "install-fixture",
            "generated_at": "2026-10-25T00:00:00Z",
            "resolved": {
                "schema_version": PLATFORM_CONFIG_SCHEMA,
                "operator": {
                    "origin": "https://operator.example.test",
                    "certificate_file": str(self.certificate),
                    "private_key_file": str(self.private_key),
                },
                "storage": {
                    "state_root": str(self.state),
                    "handoff_root": str(self.handoff),
                    "artifact_root": str(self.artifact_root),
                    "workspace_root": str(self.workspace_root),
                    "release_root": str(self.release),
                },
                "database": {"dsn_file": str(self.dsn)},
                "windows": {
                    "ssh_host": "windows-fixture",
                    "endpoint_port": 8765,
                    "token_file": str(self.token),
                    "tunnel_loopback_port": 18765,
                    "instance": {
                        "task_name": "description-native",
                        "package_root": "C:/payloads/packages",
                        "output_root": "C:/payloads/outputs",
                        "state_root": "C:/payloads/state",
                    },
                },
                "auth": {
                    "feishu_secret_file": str(self.secret),
                    "tenant_keys": ["tenant"],
                    "admin_open_ids": [],
                },
                "targets": {
                    "m3.0": {
                        "repository_slug": "mimicverse/robot",
                        "base": "main",
                    }
                },
                "specifications": {"record_roots": [str(self.records)]},
                "retention": {"unreferenced_evidence_days": None},
                "derived": {
                    "repositories": {
                        "mimicverse/robot": str(self.checkout),
                    },
                    "discovery": {"record_roots": [str(self.records)]},
                },
            },
            "sources": {"platform_json_sha256": "a" * 64},
        }
        write_json(self.configuration / "config-snapshot.json", self.snapshot)
        self.platform_config_sha256 = config_snapshot_digest(self.snapshot)
        self.originals_ref = _artifact(ORIGINALS_KIND, self.originals)
        self.ruleset_ref = _artifact(RULESET_KIND, self.ruleset)
        self.configuration_ref = _artifact(CONFIGURATION_KIND, self.configuration)
        self.upload_files = {
            path.relative_to(self.originals).as_posix(): file_digest(path)
            for path in sorted(self.originals.rglob("*"))
            if path.is_file()
        }
        self.native_target = NativeTargetIdentity.create(
            install_id="win-" + "a" * 32,
            pipeline_id="solidworks-to-urdf",
        )

    def request(self, **overrides):
        record = {
            "schema_version": FREEZE_REQUEST_SCHEMA,
            "request_id": "request-1",
            "principal": "cli_fixture:tenant:ou_fixture",
            "target_hardware": "m3.0",
            "purpose": "engineering",
            "change_summary": "Freeze the admitted revision",
            "main_assembly": "top.SLDASM",
            "ruleset_version": self.ruleset_version,
            "originals_artifact_id": self.originals_ref.artifact_id,
            "ruleset_artifact_id": self.ruleset_ref.artifact_id,
            "configuration_artifact_id": self.configuration_ref.artifact_id,
            "upload_inventory_sha256": digest(self.upload_files),
            "ruleset_catalog_sha256": self.ruleset_ref.set_sha256,
            "platform_config_sha256": self.platform_config_sha256,
            "native_target_identity": self.native_target.to_record(),
        }
        record.update(overrides)
        return record

    def command(self, *, command_id="freeze-command-1", request=None, idempotency_key="run-1/freeze"):
        return StageCommand.create(
            command_id=command_id,
            run_id="run-1",
            stage="freeze",
            idempotency_key=idempotency_key,
            request=self.request() if request is None else request,
            input_artifacts=(self.originals_ref, self.ruleset_ref, self.configuration_ref),
            config_snapshot_sha256=self.platform_config_sha256,
            contract_sha256=digest({"contract": 1}),
            policy_sha256=digest({"policy": 1}),
            expected_executor_sha256=digest({"executor": "freeze"}),
            predecessor_decision_id=None,
        )

    def claim(self, command, *, fence=1):
        return ExecutionClaim(
            claim_id=f"claim-{fence}",
            command_id=command.command_id,
            fence=fence,
            owner_sha256=digest({"process": f"freeze-{fence}"}),
            executor_sha256=command.expected_executor_sha256,
            lane_key=None,
            expires_at=datetime(2026, 10, 25, tzinfo=UTC) + timedelta(minutes=5),
        )

    def workspace(self, *, fence=1):
        return StageWorkspace(
            root=self.root,
            inputs={
                self.originals_ref.artifact_id: self.originals,
                self.ruleset_ref.artifact_id: self.ruleset,
                self.configuration_ref.artifact_id: self.configuration,
            },
        )


class FreezeDomainTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="freeze-handler-")
        self.addCleanup(self.temporary.cleanup)
        self.harness = FreezeHarness(Path(self.temporary.name))
        self.handler = FreezeHandler()

    def test_complete_command_and_artifact_closure_complete_once(self) -> None:
        command = self.harness.command()
        claim = self.harness.claim(command)
        self.assertNotEqual(claim.owner_sha256, claim.executor_sha256)
        execution = self.handler.execute(
            command,
            claim,
            self.harness.workspace(),
            effects=NoEffects(),
        )
        self.assertEqual(execution.outcome, "completed")
        root = Path(execution.candidate_root)
        self.assertEqual(
            {path.name for path in root.iterdir()},
            {
                "checks.json",
                "configuration.json",
                "inputs.json",
                "request.json",
                "ruleset.json",
                "stage-request.json",
            },
        )
        checks = read_data(root / "checks.json")
        self.assertEqual(execution.checks_sha256, digest(checks))
        self.assertEqual(
            [item["id"] for item in checks["checks"]],
            [
                "command.binding",
                "artifact.closure",
                "handoff.integrity",
                "main_assembly",
                "ruleset.catalog",
                "configuration.snapshot",
            ],
        )

    def test_empty_catalog_root_is_restored_inside_freeze(self) -> None:
        root = self.harness.ruleset / "root-000"
        shutil.rmtree(root)
        roots = (RulesetRoot(path="root-000", members={}),)
        catalog = RulesetCatalog(
            version="records-" + digest([item.to_record() for item in roots]),
            roots=roots,
            records={},
        )
        self.harness.ruleset_version = catalog.version
        write_json(
            self.harness.ruleset / "catalog.json",
            catalog.to_record(),
        )
        self.harness.ruleset_ref = _artifact(
            RULESET_KIND,
            self.harness.ruleset,
        )
        command = self.harness.command()
        execution = self.handler.execute(
            command,
            self.harness.claim(command),
            self.harness.workspace(),
            effects=NoEffects(),
        )
        self.assertEqual(execution.outcome, "completed")
        self.assertTrue(root.is_dir())
        self.assertEqual(list(root.iterdir()), [])

    def test_freeze_refuses_a_fabricated_predecessor(self) -> None:
        source = self.harness.command()
        command = StageCommand.create(
            command_id="freeze-with-predecessor",
            run_id=source.run_id,
            stage="freeze",
            idempotency_key="freeze-with-predecessor",
            request=source.to_record()["request"],
            input_artifacts=source.input_artifacts,
            config_snapshot_sha256=source.config_snapshot_sha256,
            contract_sha256=source.contract_sha256,
            policy_sha256=source.policy_sha256,
            expected_executor_sha256=source.expected_executor_sha256,
            predecessor_decision_id="fabricated-predecessor",
        )
        with self.assertRaises(StageFailure) as raised:
            self.handler.execute(
                command,
                self.harness.claim(command),
                self.harness.workspace(),
                effects=NoEffects(),
            )
        self.assertEqual(
            raised.exception.reason_code,
            "freeze.predecessor_invalid",
        )

    def test_wrong_request_artifact_binding_raises_stage_failure_with_cause(self) -> None:
        command = self.harness.command(request=self.harness.request(originals_artifact_id="another-artifact"))
        with self.assertRaises(StageFailure) as raised:
            self.handler.execute(
                command,
                self.harness.claim(command),
                self.harness.workspace(),
                effects=NoEffects(),
            )
        self.assertEqual(raised.exception.reason_code, "freeze.artifact_binding_mismatch")
        self.assertIsInstance(raised.exception.__cause__, FreezeRefusal)
        self.assertFalse((self.harness.root / "freeze-failure-1").exists())

    def test_wrong_original_content_binding_is_refused(self) -> None:
        command = self.harness.command(request=self.harness.request(upload_inventory_sha256=digest({"wrong": 1})))
        with self.assertRaises(StageFailure) as raised:
            self.handler.execute(
                command,
                self.harness.claim(command),
                self.harness.workspace(),
                effects=NoEffects(),
            )
        self.assertEqual(raised.exception.reason_code, "freeze.original_binding_mismatch")

    def test_ruleset_version_is_bound_inside_the_immutable_catalog(self) -> None:
        command = self.harness.command(request=self.harness.request(ruleset_version="another-version"))
        with self.assertRaises(StageFailure) as raised:
            self.handler.execute(
                command,
                self.harness.claim(command),
                self.harness.workspace(),
                effects=NoEffects(),
            )
        self.assertEqual(raised.exception.reason_code, "freeze.ruleset_invalid")

    def test_case_only_main_assembly_variant_is_refused(self) -> None:
        command = self.harness.command(request=self.harness.request(main_assembly="TOP.sldasm"))
        with self.assertRaises(StageFailure) as raised:
            self.handler.execute(
                command,
                self.harness.claim(command),
                self.harness.workspace(),
                effects=NoEffects(),
            )
        self.assertEqual(raised.exception.reason_code, "freeze.main_assembly_invalid")


class _Services:
    def __init__(self, root: Path, harness: FreezeHarness) -> None:
        self.root = root
        self.harness = harness
        self.commands: dict[str, StageCommand] = {}
        self.keys: dict[str, str] = {}
        self.candidate = None
        self.decision = None
        self.fail_commit_once = False
        self.materializations = 0
        self.seals = 0
        self.commits = 0

    def add(self, command: StageCommand) -> None:
        prior = self.keys.get(command.idempotency_key)
        if prior is not None and prior != command.request_digest:
            raise ProtocolError("command.idempotency_conflict", {"key": command.idempotency_key})
        self.keys[command.idempotency_key] = command.request_digest
        self.commands[command.command_id] = command

    def load_command(self, command_id):
        return self.commands[command_id]

    def decision_for(self, command):
        return self.decision

    def cancellation_for(self, command):
        del command
        return None

    @staticmethod
    def pending_effect_for_command(command_id):
        del command_id
        return None

    @staticmethod
    def verify_pending_effect(command, pending):
        del command
        return pending

    @staticmethod
    def yield_claim_for_effect(command, claim, effect_id):
        del command, claim, effect_id
        raise AssertionError("Freeze never defers an external effect")

    def load_result(self, decision):
        return self.candidate.result

    def verify_decision(self, decision, result):
        self.decision = decision

    def acquire_claim(self, command):
        fence = 1 if self.materializations == 0 else self.materializations + 1
        return self.harness.claim(command, fence=fence)

    def candidate_for(self, command, claim):
        return self.candidate

    def materialize(self, command, claim, *, candidate):
        self.materializations += 1
        root = self.root / f"workspace-{claim.fence}"
        root.mkdir()
        if candidate is not None:
            return StageWorkspace(root=root, inputs={})
        inputs = {}
        for artifact in command.input_artifacts:
            source = {
                self.harness.originals_ref.artifact_id: self.harness.originals,
                self.harness.ruleset_ref.artifact_id: self.harness.ruleset,
                self.harness.configuration_ref.artifact_id: self.harness.configuration,
            }[artifact.artifact_id]
            target = root / "inputs" / artifact.artifact_id
            shutil.copytree(source, target)
            inputs[artifact.artifact_id] = target
        return StageWorkspace(root=root, inputs=inputs)

    def seal_candidate(self, command, claim, execution):
        self.seals += 1
        members = _members(Path(execution.candidate_root))
        artifact_id = digest({"result": execution.checks_sha256, "members": members})
        output_artifact = ArtifactRef(
            artifact_id=artifact_id,
            kind="stage_output:freeze",
            locator=f"cas://artifact/{artifact_id}",
            manifest_sha256=digest({"manifest": artifact_id}),
            set_sha256=digest({"members": members}),
            producer_run_id=command.run_id,
            producer_stage="freeze",
            producer_tool_sha256=command.expected_executor_sha256,
        )
        result = StageResult(
            schema_version=STAGE_RESULT_SCHEMA,
            command_id=command.command_id,
            run_id=command.run_id,
            stage="freeze",
            request_digest=command.request_digest,
            outcome=execution.outcome,
            output_artifact=output_artifact,
            actual_executor_sha256=command.expected_executor_sha256,
            checks_sha256=execution.checks_sha256,
            resources_settled=execution.resources_settled,
            reason_code=execution.reason_code,
            diagnostic_scope=command.diagnostic_scope,
        )
        record_id = digest({"result_record": result.digest})
        result_record_artifact = ArtifactRef(
            artifact_id=record_id,
            kind="stage_result",
            locator=f"cas://artifact/{record_id}",
            manifest_sha256=digest({"manifest": record_id}),
            set_sha256=digest({"record": record_id}),
            producer_run_id=command.run_id,
            producer_stage="freeze",
            producer_tool_sha256=claim.executor_sha256,
        )
        self.candidate = StageCandidate(
            result=result,
            result_record_artifact=result_record_artifact,
            candidate_id=f"candidate-{result.digest}",
            claim_id=claim.claim_id,
            fence=claim.fence,
        )
        return self.candidate

    def verify_candidate(self, command, claim, candidate):
        return candidate

    def effect_states_for_command(self, command_id):
        return {}

    def effect_port(self, command, claim, workspace):
        return NoEffects()

    def commit_failure(self, command, claim, failure):
        raise AssertionError("Freeze success tests do not commit failures")

    def discard_workspace(self, workspace):
        return None

    def commit_decision(self, command, claim, candidate):
        self.commits += 1
        if self.fail_commit_once:
            self.fail_commit_once = False
            raise RuntimeError("simulated crash after seal and before DB confirmation")
        self.decision = StageDecision(
            decision_id=f"decision-{self.commits}",
            run_id=command.run_id,
            stage="freeze",
            command_id=command.command_id,
            result_record_artifact=candidate.result_record_artifact,
            result_digest=candidate.result.digest,
            actual_executor_sha256=candidate.result.actual_executor_sha256,
            outcome=candidate.result.outcome,
            original_producer_decision_id=None,
            reason_code=candidate.result.reason_code,
            failure_envelope=None,
            decided_at=datetime(2026, 10, 25, tzinfo=UTC),
        )
        return self.decision


class FreezeEngineRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="freeze-engine-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.harness = FreezeHarness(self.root)
        self.handler = CountingFreezeHandler()
        self.services = _Services(self.root, self.harness)
        self.command = self.harness.command()
        self.services.add(self.command)

    def test_same_key_and_request_reuses_the_one_decision(self) -> None:
        first = execute_stage(command_id=self.command.command_id, handler=self.handler, services=self.services)
        second = execute_stage(command_id=self.command.command_id, handler=self.handler, services=self.services)
        self.assertEqual(first, second)
        self.assertEqual(self.handler.executions, 1)
        self.assertEqual(self.services.seals, 1)
        self.assertEqual(self.services.commits, 1)

    def test_real_freeze_handler_runs_as_one_diagnostic_stage(self) -> None:
        scope = DiagnosticReplayScope.from_inputs(
            "freeze",
            self.command.input_artifacts,
        )
        command = StageCommand.create(
            command_id="diagnostic-freeze",
            run_id="diagnostic-freeze-run",
            stage="freeze",
            idempotency_key="diagnostic-freeze/run",
            request=self.command.to_record()["request"],
            input_artifacts=self.command.input_artifacts,
            config_snapshot_sha256=self.command.config_snapshot_sha256,
            contract_sha256=self.command.contract_sha256,
            policy_sha256=self.command.policy_sha256,
            expected_executor_sha256=self.command.expected_executor_sha256,
            predecessor_decision_id=None,
            diagnostic_scope=scope,
        )
        self.services.add(command)
        result = execute_stage(
            command_id=command.command_id,
            handler=self.handler,
            services=self.services,
        )
        self.assertIsInstance(result, StageResult)
        self.assertEqual(result.outcome, "completed")
        self.assertEqual(result.diagnostic_scope, scope)
        self.assertEqual(self.handler.executions, 1)

    def test_same_key_with_another_request_is_refused(self) -> None:
        alternate = self.harness.command(
            command_id="freeze-command-2",
            request=self.harness.request(change_summary="Another accepted meaning"),
            idempotency_key=self.command.idempotency_key,
        )
        with self.assertRaises(ProtocolError) as raised:
            self.services.add(alternate)
        self.assertEqual(raised.exception.code, "command.idempotency_conflict")

    def test_seal_before_confirm_recovers_without_scratch_or_duplicate_checks(self) -> None:
        self.services.fail_commit_once = True
        with self.assertRaisesRegex(RuntimeError, "after seal"):
            execute_stage(command_id=self.command.command_id, handler=self.handler, services=self.services)
        self.assertEqual(self.handler.executions, 1)
        self.assertEqual(self.services.seals, 1)
        self.assertIsNotNone(self.services.candidate)

        shutil.rmtree(self.harness.originals)
        recovered = execute_stage(command_id=self.command.command_id, handler=self.handler, services=self.services)

        self.assertEqual(recovered.outcome, "completed")
        self.assertEqual(self.handler.executions, 1)
        self.assertEqual(self.handler.validations, 2)
        self.assertEqual(self.services.seals, 1)
        self.assertEqual(self.services.commits, 2)


if __name__ == "__main__":
    unittest.main()
