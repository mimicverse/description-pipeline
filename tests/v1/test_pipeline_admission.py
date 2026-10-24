"""Pure AdmissionSpec to stage-command projection and anti-confusion tests."""

from __future__ import annotations

import tempfile
import unittest
import shutil
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

from description_pipeline.artifacts import ArtifactStore
from description_pipeline.io import digest, write_json
from description_pipeline.pipeline import (
    AdmissionSpec,
    ArtifactRef,
    StageCommand,
    StageDecision,
    StageResult,
    StageExecutors,
    VerifiedArtifact,
    VerifiedDiscoverResult,
    VerifiedPredecessor,
    VerifiedRuleset,
    build_capture_command,
    build_discover_command,
    build_freeze_command,
    build_generate_command,
    build_publish_command,
    build_run_admission,
    build_verify_command,
    validate_capture_command,
    validate_discover_command,
    validate_freeze_command,
    validate_generate_command,
    validate_run_admission,
    validate_stage_command,
    validate_verify_command,
    validate_store_command,
)
from description_pipeline.pipeline.admission import ADMISSION_SPEC_SCHEMA
from description_pipeline.pipeline.contracts import ProtocolError
from description_pipeline.pipeline.discover import (
    CONFIGURATION_KIND,
    DISCOVER_REQUEST_SCHEMA,
    RULESET_KIND,
    SOURCE_SET_KIND,
)
from description_pipeline.pipeline.freeze import FREEZE_REQUEST_SCHEMA, RULESET_CATALOG_SCHEMA


def _plain(value):
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    return value


def _request(command: StageCommand) -> dict:
    return _plain(command.request)


class AdmissionHarness:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.store = ArtifactStore(root / "cas")
        self.originals_ref = self._seal_source(
            SOURCE_SET_KIND,
            root / "source",
            {
                "cad/robot.SLDASM": b"assembly",
                "cad/base.SLDPRT": b"base",
                "parts/arm.SLDPRT": b"arm",
            },
        )
        self.ruleset_ref = self._seal_source(
            RULESET_KIND,
            root / "rules",
            {
                "catalog.json": (
                    f'{{"schema_version":"{RULESET_CATALOG_SCHEMA}","version":"rules-2026-10-25","records":{{}}}}\n'
                ).encode(),
                "joints/arm.json": b'{"limits":{"lower":-1.0,"upper":1.0}}\n',
            },
        )
        self.configuration_ref = self._seal_source(
            CONFIGURATION_KIND,
            root / "configuration",
            {"config.json": b'{"snapshot":"config-1"}\n'},
        )
        self.originals = self._verified(self.originals_ref)
        self.ruleset = self._verified(self.ruleset_ref)
        self.configuration = self._verified(self.configuration_ref)
        self.verified_ruleset = VerifiedRuleset.from_catalog_bytes(
            self.ruleset,
            (root / "rules/catalog.json").read_bytes(),
        )
        self.spec = AdmissionSpec(
            schema_version=ADMISSION_SPEC_SCHEMA,
            run_id="run-1",
            request_id="request-1",
            parent_run_id=None,
            from_stage="freeze",
            principal="cli_fixture:tenant:ou_fixture",
            target_hardware="m3.0",
            purpose="engineering",
            change_summary="Admitted revision",
            main_assembly="cad/robot.SLDASM",
            originals=self.originals,
            ruleset=self.verified_ruleset,
            configuration=self.configuration,
            executors=StageExecutors(
                freeze=digest({"executor": "freeze"}),
                discover=digest({"executor": "discover"}),
                capture=digest({"executor": "capture-linux"}),
                generate=digest({"executor": "generate"}),
                verify=digest({"executor": "verify"}),
                publish=digest({"executor": "publish"}),
            ),
            native_tool_source_sha256=digest({"executor": "native-source"}),
            contract_sha256=digest({"contract": 1}),
            policy_sha256=digest({"policy": 1}),
            created_at=datetime(2026, 10, 25, 9, 30, tzinfo=UTC),
        )

    def _seal_source(self, kind: str, root: Path, files: dict[str, bytes]) -> ArtifactRef:
        for name, payload in files.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
        return self.store.seal_tree(
            root,
            kind=kind,
            producer_run_id=None,
            producer_stage=None,
            producer_tool_sha256=digest({"producer": kind}),
        )

    def _verified(self, artifact: ArtifactRef) -> VerifiedArtifact:
        return VerifiedArtifact.from_store_record(artifact, self.store.verify(artifact))

    def alternate(self, kind: str, name: str) -> ArtifactRef:
        root = self.root / "alternate" / name
        root.mkdir(parents=True)
        (root / "different.txt").write_text(name, encoding="utf-8")
        return self.store.seal_tree(
            root,
            kind=kind,
            producer_run_id=None,
            producer_stage=None,
            producer_tool_sha256=digest({"producer": kind, "name": name}),
        )

    @staticmethod
    def manual(
        command: StageCommand,
        *,
        request=None,
        run_id=None,
        stage=None,
        input_artifacts=None,
        config_snapshot_sha256=None,
        expected_executor_sha256=...,
        predecessor_decision_id=...,
    ) -> StageCommand:
        return StageCommand.create(
            command_id=command.command_id,
            run_id=command.run_id if run_id is None else run_id,
            stage=command.stage if stage is None else stage,
            idempotency_key=command.idempotency_key,
            request=_request(command) if request is None else _plain(request),
            input_artifacts=command.input_artifacts if input_artifacts is None else input_artifacts,
            config_snapshot_sha256=(
                command.config_snapshot_sha256 if config_snapshot_sha256 is None else config_snapshot_sha256
            ),
            contract_sha256=command.contract_sha256,
            policy_sha256=command.policy_sha256,
            expected_executor_sha256=(
                command.expected_executor_sha256 if expected_executor_sha256 is ... else expected_executor_sha256
            ),
            predecessor_decision_id=(
                command.predecessor_decision_id if predecessor_decision_id is ... else predecessor_decision_id
            ),
        )

    def freeze_result(self, freeze_command: StageCommand) -> VerifiedPredecessor:
        return self.predecessor("freeze", freeze_command, {"stage": "freeze"})

    def predecessor(self, stage: str, command: StageCommand, payload: dict) -> VerifiedPredecessor:
        output = self.root / f"{stage}-result-{command.command_id.replace(':', '-')}"
        output.mkdir()
        write_json(output / "result.json", payload)
        result_artifact = self.store.seal_tree(
            output,
            kind=f"stage_output:{stage}",
            producer_run_id=self.spec.run_id,
            producer_stage=stage,
            producer_tool_sha256=self.spec.executors.for_stage(stage),
        )
        result = StageResult(
            schema_version="description-pipeline.stage-result/v1",
            command_id=command.command_id,
            run_id=self.spec.run_id,
            stage=stage,
            request_digest=command.request_digest,
            outcome="completed",
            output_artifact=result_artifact,
            actual_executor_sha256=self.spec.executors.for_stage(stage),
            checks_sha256=digest({"checks": stage}),
            resources_settled=True,
        )
        result_record_artifact = self.store.seal_stage_result(result)
        decision = StageDecision(
            decision_id=f"{stage}-decision",
            run_id=self.spec.run_id,
            stage=stage,
            command_id=command.command_id,
            result_record_artifact=result_record_artifact,
            result_digest=result.digest,
            actual_executor_sha256=self.spec.executors.for_stage(stage),
            outcome="completed",
            original_producer_decision_id=None,
            reason_code=None,
            failure_envelope=None,
            decided_at=datetime(2026, 10, 25, 9, 31, tzinfo=UTC),
        )
        return VerifiedPredecessor(
            stage=stage,
            command=command,
            result=result,
            decision=decision,
        )


class AdmissionProjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="admission-projection-")
        self.addCleanup(self.temporary.cleanup)
        self.harness = AdmissionHarness(Path(self.temporary.name))
        self.command = build_freeze_command(self.harness.spec)

    def test_same_admission_and_verified_bytes_build_one_stable_freeze_command(self) -> None:
        first = build_freeze_command(self.harness.spec)
        second = build_freeze_command(self.harness.spec)
        self.assertEqual(first, second)
        self.assertEqual(first.stage, "freeze")
        self.assertEqual(first.run_id, self.harness.spec.run_id)
        self.assertIsNone(first.predecessor_decision_id)
        self.assertEqual(first.request["schema_version"], FREEZE_REQUEST_SCHEMA)
        self.assertEqual(first.request["request_id"], self.harness.spec.request_id)
        self.assertEqual(first.request["originals_artifact_id"], self.harness.originals_ref.artifact_id)
        self.assertEqual(
            first.input_artifacts,
            (
                self.harness.originals_ref,
                self.harness.ruleset_ref,
                self.harness.configuration_ref,
            ),
        )
        self.assertEqual(validate_freeze_command(self.harness.spec, first), first)

    def test_admission_spec_round_trips_without_losing_identity(self) -> None:
        restored = AdmissionSpec.from_record(self.harness.spec.to_record())
        self.assertEqual(restored, self.harness.spec)
        self.assertEqual(restored.admission_sha256, self.harness.spec.admission_sha256)
        self.assertEqual(build_freeze_command(restored), self.command)

    def test_run_admission_frozen_request_rebuilds_the_same_spec(self) -> None:
        restored = AdmissionSpec.from_record(build_run_admission(self.harness.spec).request)
        self.assertEqual(restored, self.harness.spec)

    def test_wrong_administrative_fields_are_each_rejected(self) -> None:
        cases = {
            "principal": "forged-principal",
            "purpose": "inspection_preview",
            "target_hardware": "m4.0",
            "request_id": "another-request",
        }
        for field, value in cases.items():
            with self.subTest(field=field):
                forged = AdmissionHarness.manual(
                    self.command,
                    request={**_request(self.command), field: value},
                )
                with self.assertRaises(ProtocolError) as raised:
                    validate_freeze_command(self.harness.spec, forged)
                self.assertEqual(raised.exception.code, "admission.command_mismatch")
                self.assertIn(f"request.{field}", raised.exception.detail["fields"])

    def test_wrong_originals_rules_or_configuration_artifact_is_rejected(self) -> None:
        alternate_originals = self.harness.alternate(SOURCE_SET_KIND, "originals")
        alternate_rules = self.harness.alternate(RULESET_KIND, "rules")
        alternate_configuration = self.harness.alternate(CONFIGURATION_KIND, "configuration")
        for index, artifact in enumerate((alternate_originals, alternate_rules, alternate_configuration)):
            with self.subTest(index=index):
                inputs = list(self.command.input_artifacts)
                inputs[index] = artifact
                forged = AdmissionHarness.manual(self.command, input_artifacts=tuple(inputs))
                with self.assertRaises(ProtocolError) as raised:
                    validate_freeze_command(self.harness.spec, forged)
                self.assertIn("input_artifacts", raised.exception.detail["fields"])

    def test_wrong_run_identity_or_configuration_snapshot_is_rejected(self) -> None:
        for forged in (
            AdmissionHarness.manual(self.command, run_id="run-2"),
            AdmissionHarness.manual(self.command, config_snapshot_sha256=digest({"config": "other"})),
        ):
            with self.assertRaises(ProtocolError) as raised:
                validate_freeze_command(self.harness.spec, forged)
            self.assertEqual(raised.exception.code, "admission.command_mismatch")

    def test_store_validation_hook_dispatches_freeze_without_io(self) -> None:
        admission = build_run_admission(self.harness.spec)
        self.assertEqual(
            validate_store_command(self.harness.spec, admission, self.command),
            self.command,
        )
        self.assertEqual(validate_run_admission(self.harness.spec, admission), admission)

    def test_run_admission_cannot_carry_another_principal_or_closure(self) -> None:
        canonical = build_run_admission(self.harness.spec)
        changed_request = _plain(canonical.request)
        changed_request["principal"] = "forged-principal"
        changed_request["originals"]["artifact"]["artifact_id"] = "other-originals"
        forged = canonical.__class__.create(
            run_id=canonical.run_id,
            request_id=canonical.request_id,
            parent_run_id=canonical.parent_run_id,
            from_stage=canonical.from_stage,
            request=changed_request,
            created_at=canonical.created_at,
        )
        with self.assertRaises(ProtocolError) as raised:
            validate_store_command(self.harness.spec, forged, self.command)
        self.assertEqual(raised.exception.code, "admission.run_mismatch")

    def test_builder_reads_no_blob_store_after_admission_is_verified(self) -> None:
        expected = build_freeze_command(self.harness.spec)
        shutil.rmtree(self.harness.root / "cas")
        self.assertEqual(build_freeze_command(self.harness.spec), expected)
        self.assertEqual(validate_freeze_command(self.harness.spec, expected), expected)

    def test_verified_artifact_rejects_tampered_member_record(self) -> None:
        record = self.harness.store.verify(self.harness.originals_ref)
        record["members"][0]["sha256"] = "0" * 64
        with self.assertRaises(ProtocolError):
            VerifiedArtifact.from_store_record(self.harness.originals_ref, record)

    def test_ruleset_version_cannot_be_detached_from_catalog_bytes(self) -> None:
        with self.assertRaises(ProtocolError) as raised:
            VerifiedRuleset.from_catalog_bytes(
                self.harness.ruleset,
                b'{"schema_version":"different"}\n',
            )
        self.assertEqual(raised.exception.code, "admission.ruleset_invalid")

    def test_discover_projects_only_admission_and_verified_freeze_result(self) -> None:
        predecessor = self.harness.freeze_result(self.command)
        discover = build_discover_command(self.harness.spec, predecessor)
        self.assertEqual(discover.stage, "discover")
        self.assertEqual(discover.predecessor_decision_id, "freeze-decision")
        self.assertEqual(discover.request["schema_version"], DISCOVER_REQUEST_SCHEMA)
        self.assertEqual(discover.request["freeze_artifact_id"], predecessor.result.output_artifact.artifact_id)
        self.assertEqual(discover.request["source_inventory_sha256"], self.harness.originals.inventory_sha256)
        self.assertEqual(discover.request["ruleset_version"], self.harness.spec.ruleset.version)
        self.assertEqual(discover.request["submission_identity"]["owner"], self.harness.spec.principal)
        self.assertEqual(discover.request["submission_identity"]["hardware_id"], self.harness.spec.target_hardware)
        self.assertEqual(discover.input_artifacts[0], predecessor.result.output_artifact)
        self.assertEqual(validate_discover_command(self.harness.spec, predecessor, discover), discover)
        self.assertEqual(
            validate_stage_command(self.harness.spec, discover, predecessor=predecessor),
            discover,
        )
        self.assertEqual(
            validate_store_command(
                self.harness.spec,
                build_run_admission(self.harness.spec),
                discover,
                predecessor=predecessor,
            ),
            discover,
        )

    def test_wrong_discover_request_field_is_rejected(self) -> None:
        predecessor = self.harness.freeze_result(self.command)
        discover = build_discover_command(self.harness.spec, predecessor)
        forged = AdmissionHarness.manual(
            discover,
            request={**_request(discover), "purpose": "inspection_preview"},
        )
        with self.assertRaises(ProtocolError) as raised:
            validate_discover_command(self.harness.spec, predecessor, forged)
        self.assertIn("request.purpose", raised.exception.detail["fields"])

    def test_capture_generate_and_verify_project_from_verified_lineage(self) -> None:
        freeze = self.harness.freeze_result(self.command)
        discover_command = build_discover_command(self.harness.spec, freeze)
        discover_payload = {
            "schema_version": "description-pipeline.discover-result/v1",
            "command_id": discover_command.command_id,
            "run_id": self.harness.spec.run_id,
            "request_digest": discover_command.request_digest,
            "selection": {
                "files": {"cad/robot.SLDASM": self.harness.originals.inventory["cad/robot.SLDASM"]},
                "handoff_sha256": digest({"selected": 1}),
                "upload_sha256": self.harness.originals.inventory_sha256,
                "main_assembly": self.harness.spec.main_assembly,
            },
            "prepared_sha256": digest({"prepared": 1}),
            "native_observation_sha256": digest({"observation": 1}),
            "source_set_artifact": self.harness.originals_ref.to_record(),
            "ruleset_artifact": self.harness.ruleset_ref.to_record(),
            "configuration_artifact": self.harness.configuration_ref.to_record(),
            "native_tool": {
                "executor": "native",
                "source_sha256": self.harness.spec.native_tool_source_sha256,
            },
            "passed": True,
        }
        discover_payload["selection"]["handoff_sha256"] = digest(discover_payload["selection"]["files"])
        discover = self.harness.predecessor("discover", discover_command, discover_payload)
        discover_result = VerifiedDiscoverResult.from_record(discover, discover_payload)
        self.assertEqual(
            discover_result.native_tool_source_sha256,
            self.harness.spec.native_tool_source_sha256,
        )
        self.assertEqual(
            discover_result.native_tool_record_sha256,
            digest(discover_payload["native_tool"]),
        )
        self.assertNotEqual(
            discover_result.native_tool_source_sha256,
            discover_result.native_tool_record_sha256,
        )
        capture_command = build_capture_command(
            self.harness.spec,
            freeze,
            discover,
            discover_result,
        )
        capture = self.harness.predecessor("capture", capture_command, {"stage": "capture"})
        generate_command = build_generate_command(self.harness.spec, capture)
        generate = self.harness.predecessor("generate", generate_command, {"stage": "generate"})
        verify_command = build_verify_command(self.harness.spec, generate, capture)

        self.assertEqual(
            {
                command.stage: command.expected_executor_sha256
                for command in (
                    self.command,
                    discover_command,
                    capture_command,
                    generate_command,
                    verify_command,
                )
            },
            {
                stage: self.harness.spec.executors.for_stage(stage)
                for stage in ("freeze", "discover", "capture", "generate", "verify")
            },
        )
        self.assertEqual(capture_command.stage, "capture")
        self.assertEqual(
            set(capture_command.request),
            {
                "effect_key",
                "handoff_sha256",
                "main_assembly",
                "native_tool_sha256",
                "prepared_sha256",
                "upload_sha256",
            },
        )
        self.assertEqual(capture_command.predecessor_decision_id, discover.decision.decision_id)
        self.assertEqual(
            capture_command.input_artifacts,
            (
                freeze.result.output_artifact,
                discover.result.output_artifact,
            ),
        )
        self.assertEqual(capture_command.request["prepared_sha256"], discover_result.prepared_sha256)
        self.assertEqual(
            capture_command.request["native_tool_sha256"],
            discover_result.native_tool_source_sha256,
        )
        self.assertNotEqual(
            capture_command.expected_executor_sha256,
            capture_command.request["native_tool_sha256"],
        )
        self.assertEqual(
            generate_command.request,
            {
                "schema_version": "description-pipeline.generate-request/v1",
                "capture_artifact_id": capture.result.output_artifact.artifact_id,
                "output_contract": "solidworks-to-urdf.generated-artifact/v1",
            },
        )
        self.assertEqual(
            set(verify_command.request),
            {
                "capture_artifact_id",
                "generate_artifact_id",
                "output_contract",
                "purpose",
                "schema_version",
            },
        )
        self.assertEqual(verify_command.request["generate_artifact_id"], generate.result.output_artifact.artifact_id)
        self.assertEqual(verify_command.request["capture_artifact_id"], capture.result.output_artifact.artifact_id)
        self.assertEqual(verify_command.request["purpose"], self.harness.spec.purpose)
        self.assertEqual(
            validate_capture_command(
                self.harness.spec,
                freeze,
                discover,
                discover_result,
                capture_command,
            ),
            capture_command,
        )
        self.assertEqual(validate_generate_command(self.harness.spec, capture, generate_command), generate_command)
        self.assertEqual(
            validate_verify_command(self.harness.spec, generate, capture, verify_command),
            verify_command,
        )
        self.assertEqual(
            validate_stage_command(
                self.harness.spec,
                capture_command,
                predecessor=discover,
                supporting_predecessors=(freeze,),
                discover_result=discover_result,
            ),
            capture_command,
        )
        self.assertEqual(
            validate_stage_command(
                self.harness.spec,
                generate_command,
                predecessor=capture,
            ),
            generate_command,
        )
        self.assertEqual(
            validate_stage_command(
                self.harness.spec,
                verify_command,
                predecessor=generate,
                supporting_predecessors=(capture,),
            ),
            verify_command,
        )
        admission = build_run_admission(self.harness.spec)
        self.assertEqual(
            validate_store_command(
                self.harness.spec,
                admission,
                capture_command,
                predecessor=discover,
                supporting_predecessors=(freeze,),
                discover_result=discover_result,
            ),
            capture_command,
        )
        self.assertEqual(
            validate_store_command(
                self.harness.spec,
                admission,
                generate_command,
                predecessor=capture,
            ),
            generate_command,
        )
        self.assertEqual(
            validate_store_command(
                self.harness.spec,
                admission,
                verify_command,
                predecessor=generate,
                supporting_predecessors=(capture,),
            ),
            verify_command,
        )
        wrong_generate = AdmissionHarness.manual(
            generate_command,
            request={**_request(generate_command), "capture_artifact_id": "another-capture"},
        )
        with self.assertRaises(ProtocolError) as generate_error:
            validate_generate_command(self.harness.spec, capture, wrong_generate)
        self.assertIn("request.capture_artifact_id", generate_error.exception.detail["fields"])
        wrong_verify = AdmissionHarness.manual(
            verify_command,
            request={**_request(verify_command), "capture_artifact_id": "another-capture"},
        )
        with self.assertRaises(ProtocolError) as verify_error:
            validate_verify_command(self.harness.spec, generate, capture, wrong_verify)
        self.assertIn("request.capture_artifact_id", verify_error.exception.detail["fields"])

        wrong_native_record = {
            **discover_payload,
            "native_tool": {
                "executor": "different-native",
                "source_sha256": digest({"executor": "different-native"}),
            },
        }
        wrong_native_result = VerifiedDiscoverResult.from_record(discover, wrong_native_record)
        sends: list[str] = []
        with self.assertRaises(ProtocolError):
            build_capture_command(self.harness.spec, freeze, discover, wrong_native_result)
            sends.append("native")
        self.assertEqual(sends, [])

        wrong_native_request = AdmissionHarness.manual(
            capture_command,
            request={
                **_request(capture_command),
                "native_tool_sha256": digest({"executor": "wrong-native-requirement"}),
            },
        )
        with self.assertRaises(ProtocolError):
            validate_capture_command(
                self.harness.spec,
                freeze,
                discover,
                discover_result,
                wrong_native_request,
            )
            sends.append("native")
        self.assertEqual(sends, [])

        wrong_executor = AdmissionHarness.manual(
            capture_command,
            expected_executor_sha256=digest({"executor": "wrong-linux-capture"}),
        )
        with self.assertRaises(ProtocolError):
            validate_capture_command(
                self.harness.spec,
                freeze,
                discover,
                discover_result,
                wrong_executor,
            )
            sends.append("native")
        self.assertEqual(sends, [])

    def test_wrong_capture_prepared_digest_is_rejected(self) -> None:
        freeze = self.harness.freeze_result(self.command)
        discover_command = build_discover_command(self.harness.spec, freeze)
        discover_payload = {
            "schema_version": "description-pipeline.discover-result/v1",
            "command_id": discover_command.command_id,
            "run_id": self.harness.spec.run_id,
            "request_digest": discover_command.request_digest,
            "selection": {
                "files": {"cad/robot.SLDASM": self.harness.originals.inventory["cad/robot.SLDASM"]},
                "upload_sha256": self.harness.originals.inventory_sha256,
                "main_assembly": self.harness.spec.main_assembly,
            },
            "prepared_sha256": digest({"prepared": 1}),
            "native_observation_sha256": digest({"observation": 1}),
            "source_set_artifact": self.harness.originals_ref.to_record(),
            "ruleset_artifact": self.harness.ruleset_ref.to_record(),
            "configuration_artifact": self.harness.configuration_ref.to_record(),
            "native_tool": {
                "executor": "native",
                "source_sha256": self.harness.spec.native_tool_source_sha256,
            },
            "passed": True,
        }
        discover_payload["selection"]["handoff_sha256"] = digest(discover_payload["selection"]["files"])
        discover = self.harness.predecessor("discover", discover_command, discover_payload)
        discover_result = VerifiedDiscoverResult.from_record(discover, discover_payload)
        capture = build_capture_command(self.harness.spec, freeze, discover, discover_result)
        forged = AdmissionHarness.manual(
            capture,
            request={**_request(capture), "prepared_sha256": digest({"another": 1})},
        )
        with self.assertRaises(ProtocolError) as raised:
            validate_capture_command(self.harness.spec, freeze, discover, discover_result, forged)
        self.assertIn("request.prepared_sha256", raised.exception.detail["fields"])

    def test_publish_boundary_is_explicitly_pending(self) -> None:
        freeze = self.harness.freeze_result(self.command)
        discover_command = build_discover_command(self.harness.spec, freeze)
        discover_payload = {
            "schema_version": "description-pipeline.discover-result/v1",
            "command_id": discover_command.command_id,
            "run_id": self.harness.spec.run_id,
            "request_digest": discover_command.request_digest,
            "selection": {
                "files": {"cad/robot.SLDASM": self.harness.originals.inventory["cad/robot.SLDASM"]},
                "upload_sha256": self.harness.originals.inventory_sha256,
                "main_assembly": self.harness.spec.main_assembly,
            },
            "prepared_sha256": digest({"prepared": 1}),
            "native_observation_sha256": digest({"observation": 1}),
            "source_set_artifact": self.harness.originals_ref.to_record(),
            "ruleset_artifact": self.harness.ruleset_ref.to_record(),
            "configuration_artifact": self.harness.configuration_ref.to_record(),
            "native_tool": {
                "executor": "native",
                "source_sha256": self.harness.spec.native_tool_source_sha256,
            },
            "passed": True,
        }
        discover_payload["selection"]["handoff_sha256"] = digest(discover_payload["selection"]["files"])
        discover = self.harness.predecessor("discover", discover_command, discover_payload)
        discover_result = VerifiedDiscoverResult.from_record(discover, discover_payload)
        capture_command = build_capture_command(self.harness.spec, freeze, discover, discover_result)
        capture = self.harness.predecessor("capture", capture_command, {"stage": "capture"})
        generate_command = build_generate_command(self.harness.spec, capture)
        generate = self.harness.predecessor("generate", generate_command, {"stage": "generate"})
        verify = self.harness.predecessor(
            "verify",
            build_verify_command(self.harness.spec, generate, capture),
            {"stage": "verify"},
        )
        with self.assertRaises(ProtocolError) as raised:
            build_publish_command(self.harness.spec, verify)
        self.assertEqual(raised.exception.code, "admission.publish_contract_pending")

    def test_invalid_predecessor_fact_cannot_project_discover(self) -> None:
        predecessor = self.harness.freeze_result(self.command)
        wrong = StageDecision(
            decision_id=predecessor.decision.decision_id,
            run_id=predecessor.decision.run_id,
            stage="freeze",
            command_id=predecessor.decision.command_id,
            result_record_artifact=predecessor.decision.result_record_artifact,
            result_digest=digest({"wrong": "result"}),
            actual_executor_sha256=predecessor.decision.actual_executor_sha256,
            outcome="completed",
            original_producer_decision_id=None,
            reason_code=None,
            failure_envelope=None,
            decided_at=predecessor.decision.decided_at,
        )
        with self.assertRaises(ProtocolError):
            VerifiedPredecessor(
                stage="freeze",
                command=predecessor.command,
                result=predecessor.result,
                decision=wrong,
            )

    def test_unsupported_stage_is_explicit(self) -> None:
        publish = StageCommand.create(
            command_id="publish-command",
            run_id="run-1",
            stage="publish",
            idempotency_key="run-1/publish",
            request={},
            input_artifacts=(),
            config_snapshot_sha256=self.harness.configuration_ref.set_sha256,
            contract_sha256=self.harness.spec.contract_sha256,
            policy_sha256=self.harness.spec.policy_sha256,
            expected_executor_sha256=self.harness.spec.executors.publish,
            predecessor_decision_id="verify-decision",
        )
        with self.assertRaises(ProtocolError) as raised:
            validate_stage_command(self.harness.spec, publish)
        self.assertEqual(raised.exception.code, "admission.stage_not_supported")


if __name__ == "__main__":
    unittest.main()
