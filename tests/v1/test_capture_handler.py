"""Capture domain handler: producer/executor split and native admission boundaries."""

from __future__ import annotations

import copy
import hashlib
import io
import json
import shutil
import tempfile
import unittest
import zipfile
from collections.abc import Mapping
from contextlib import nullcontext
from dataclasses import replace
from datetime import UTC, datetime
from email.message import Message
from pathlib import Path
from unittest.mock import Mock, patch
from urllib import error as urlerror

from description_pipeline.artifacts import ArtifactStore
from description_pipeline.delivery import PIPELINE_ID
from description_pipeline.io import (
    canonical,
    digest,
    file_digest,
    read_data,
    write_json,
)
import description_pipeline.pipeline.capture as capture_module
from description_pipeline.pipeline.capture import (
    CaptureHandler,
    CaptureUnresolved,
    NativeCaptureResult,
)
from description_pipeline.pipeline.contracts import (
    ArtifactRef,
    DiagnosticReplayScope,
    ExecutionClaim,
    NativeTargetIdentity,
    StageCommand,
    StageEffectWait,
    StagePending,
)
from description_pipeline.pipeline.discover import (
    CONFIGURATION_KIND,
    FREEZE_RESULT_KIND,
    RULESET_KIND,
    SOURCE_SET_KIND,
)
from description_pipeline.pipeline.effects import (
    EffectFact,
    EffectIntent,
    EffectResumePhase,
    EffectSettlement,
    EffectState,
    project_effect_settlement,
)
from description_pipeline.pipeline.freeze import FREEZE_REQUEST_SCHEMA
from description_pipeline.pipeline.discover import DiscoverHandler
from description_pipeline.pipeline.engine import (
    StageWorkspace,
    execute_stage,
)
from description_pipeline.pipeline.freeze import FreezeHandler
from description_pipeline.pipeline.reconciliation import (
    EffectReconciliationResult,
)
from description_pipeline.orchestration.native_operation_client import (
    ArtifactDownload,
    NativeOperationClient,
    NativeOperationCapturePort,
    NativeOperationClientConflict,
    NativeOperationEffectObserver,
    NativeOperationEndpoint,
    native_capture_operation_id,
)
from description_pipeline.orchestration.native_operation_runtime import (
    CAPTURE_KIND,
    CAPTURE_OPERATION_SCHEMA,
    CaptureOperationRequest,
    NativeArtifactView,
    NativeOperationView,
    OPERATION_VIEW_SCHEMA,
)
from description_pipeline.orchestration.airflow_effect_trigger import (
    _EffectEvidence,
    _native_expectation,
)
from description_pipeline.orchestration.stage_transfer import (
    CAPTURE_ARCHIVE,
    admit_capture,
)
from description_pipeline.sources.solidworks.discovery import PLAN_FILE
from description_pipeline.schemas import (
    NATIVE_TOOL_REQUIREMENT_KIND,
    STAGE_EFFECT_WAIT_SCHEMA,
)

from .split_capture_fixture import fixture_backend
from .test_pipeline_discover import (
    ClosureReader,
    CountingBackend,
    DiscoverHarness,
)
from .native_fixture_support import capture_record
from .test_stage_transfer import native_tool as build_capture_tool


PARENT = "a0000000-0000-4000-8000-000000000001"
CHILD = "a0000000-0000-4000-8000-000000000002"
MAIN = "cad/robot.SLDASM"
CAPTURE_TARGET = NativeTargetIdentity.create(
    install_id="win-" + "2" * 32,
    pipeline_id=PIPELINE_ID,
)


def _capture_component_closure(evidence: dict) -> dict:
    """Bind selected dependency edges to the occurrence names Capture reads."""

    root = evidence["documents"][MAIN]
    retained = []
    instances = {
        "cad/base.SLDPRT": "base-1",
        "cad/arm.SLDPRT": "arm-1",
    }
    for edge in root["resolved_edges"]:
        instance = instances.get(edge["resolved"])
        if instance is None:
            continue
        retained.append({**edge, "instance": instance})
    root["resolved_edges"] = retained
    evidence["graph"][MAIN]["references"] = ["cad/base.SLDPRT", "cad/arm.SLDPRT"]
    evidence["documents"].pop("spare/base.SLDPRT", None)
    evidence["graph"].pop("spare/base.SLDPRT", None)
    return evidence


class _EffectLedger:
    def __init__(self, store: ArtifactStore) -> None:
        self.store = store
        self.intents: dict[str, EffectIntent] = {}
        self.facts: dict[str, list[EffectFact]] = {}
        self.payloads: dict[str, ArtifactRef] = {}
        self.outbounds: dict[str, ArtifactRef] = {}


class _Effects:
    def __init__(
        self,
        ledger: _EffectLedger,
        command: StageCommand,
        claim: ExecutionClaim,
    ) -> None:
        self.ledger = ledger
        self.command = command
        self.claim = claim
        self.authorized = []
        self.observed = []

    def prepare_effect(
        self,
        *,
        effect_key,
        effect_kind,
        target,
        expected_state,
        outbound_root,
    ):
        outbound = self.ledger.store.seal_tree(
            outbound_root,
            kind="effect_payload",
            producer_run_id=self.command.run_id,
            producer_stage=self.command.stage,
            producer_tool_sha256=self.claim.executor_sha256,
        )
        identity = {
            "command_id": self.command.command_id,
            "effect_key": effect_key,
            "effect_kind": effect_kind,
            "expected_state_digest": digest(dict(expected_state)),
            "outbound_artifact_id": outbound.artifact_id,
            "target": dict(target),
        }
        intent = EffectIntent(
            effect_id="effect-" + digest(identity)[:32],
            command_id=self.command.command_id,
            effect_key=effect_key,
            effect_kind=effect_kind,
            target=dict(target),
            expected_state=dict(expected_state),
            expected_state_digest=digest(dict(expected_state)),
            outbound_artifact_id=outbound.artifact_id,
            outbound_digest=outbound.set_sha256,
            created_at=datetime.now(UTC),
        )
        self.ledger.intents[intent.effect_id] = intent
        self.ledger.facts.setdefault(intent.effect_id, [])
        self.ledger.outbounds[intent.effect_id] = outbound
        return intent

    def _append(
        self,
        effect_id,
        fact_type,
        *,
        source,
        payload=None,
        state_class=None,
        resume_phase=None,
    ):
        facts = self.ledger.facts[effect_id]
        claim_bound = fact_type in {
            "dispatch_authorized",
            "resume_authorized",
            "response",
            "timeout",
        }
        fact = EffectFact(
            fact_id=f"fact-{effect_id}-{len(facts) + 1}",
            effect_id=effect_id,
            seq=len(facts) + 1,
            claim_id=self.claim.claim_id if claim_bound else None,
            fence=self.claim.fence if claim_bound else None,
            fact_type=fact_type,
            source=source,
            state_class=state_class,
            payload_artifact_id=payload.artifact_id if payload is not None else None,
            payload_digest=payload.set_sha256 if payload is not None else digest({"fact": fact_type}),
            observed_at=datetime.now(UTC),
            resume_phase=resume_phase,
        )
        facts.append(fact)
        if payload is not None:
            self.ledger.payloads[payload.artifact_id] = payload
        return fact

    def authorize_dispatch(self, effect_id, *, source):
        intent = self.ledger.intents[effect_id]
        if self.effect_state(effect_id) is not EffectState.PREPARED:
            raise RuntimeError("capture effect is not prepared")
        self.authorized.append((intent.effect_key, self.claim.claim_id, self.claim.fence))
        return self._append(effect_id, "dispatch_authorized", source=source)

    def authorize_resume(self, effect_id, *, source):
        intent = self.ledger.intents[effect_id]
        self.authorized.append((intent.effect_key, self.claim.claim_id, self.claim.fence))
        return self._append(effect_id, "resume_authorized", source=source)

    def _payload(self, root, kind):
        return self.ledger.store.seal_tree(
            root,
            kind=kind,
            producer_run_id=self.command.run_id,
            producer_stage=self.command.stage,
            producer_tool_sha256=self.claim.executor_sha256,
        )

    def record_response(self, effect_id, *, source, response_root):
        payload = self._payload(response_root, "effect_response")
        self.observed.append(("response", effect_id))
        return self._append(effect_id, "response", source=source, payload=payload)

    def record_timeout(self, effect_id, *, source, detail_root=None):
        payload = self._payload(detail_root, "effect_timeout") if detail_root is not None else None
        self.observed.append(("timeout", effect_id))
        return self._append(effect_id, "timeout", source=source, payload=payload)

    def record_observation(
        self,
        effect_id,
        *,
        source,
        state_class,
        observation_root,
        resume_phase=None,
    ):
        payload = self._payload(observation_root, "effect_observation")
        self.observed.append((state_class, effect_id))
        return self._append(
            effect_id,
            "observation",
            source=source,
            payload=payload,
            state_class=state_class,
            resume_phase=resume_phase,
        )

    def reconcile_effect(self, effect_id, *, source, state_class, evidence_root):
        payload = self._payload(evidence_root, "effect_reconciliation")
        self.observed.append((f"reconciled:{state_class}", effect_id))
        return self._append(
            effect_id,
            "reconciled",
            source=source,
            payload=payload,
            state_class=state_class,
        )

    def _settlement(self, effect_id):
        settlement = project_effect_settlement(
            self.ledger.intents[effect_id],
            tuple(self.ledger.facts[effect_id]),
        )
        payload = None
        if settlement.decisive_fact is not None and settlement.decisive_fact.payload_artifact_id is not None:
            payload = self.ledger.payloads[settlement.decisive_fact.payload_artifact_id]
        return EffectSettlement(
            effect_id=settlement.effect_id,
            state=settlement.state,
            decisive_fact=settlement.decisive_fact,
            payload_artifact=payload,
            outbound_artifact=self.ledger.outbounds[effect_id],
            resume_phase=settlement.resume_phase,
        )

    def effect_state(self, effect_id):
        return self._settlement(effect_id).state

    def effect_settlement(self, effect_id):
        return self._settlement(effect_id)

    def effect_settlement_for_key(self, effect_key):
        for effect_id, intent in self.ledger.intents.items():
            if intent.command_id == self.command.command_id and intent.effect_key == effect_key:
                return self._settlement(effect_id)
        return None


class _TransferServices:
    """Minimal engine services for the restricted Capture continuation path."""

    def __init__(
        self,
        *,
        command: StageCommand,
        claim: ExecutionClaim,
        workspace: StageWorkspace,
        pending: StagePending,
        effects: _Effects,
    ) -> None:
        self.command = command
        self.claim = claim
        self.workspace = workspace
        self.pending = pending
        self.effects = effects
        self.discarded = False

    def load_command(self, command_id):
        if command_id != self.command.command_id:
            raise AssertionError("engine loaded another command")
        return self.command

    def decision_for(self, command):
        del command
        return None

    def cancellation_for(self, command):
        del command
        return None

    def verify_pending_effect(self, command, pending):
        if command != self.command or pending != self.pending:
            raise AssertionError("engine used a foreign pending effect")
        return pending

    def acquire_transfer_resume_claim(self, command, effect_id):
        if (
            command != self.command
            or effect_id != self.pending.wait.effect_id
        ):
            raise AssertionError("engine acquired the wrong transfer claim")
        return self.claim

    def acquire_claim(self, command):
        del command
        raise AssertionError("the transfer path must not acquire a general claim")

    def materialize(self, command, claim, *, candidate):
        if (
            command != self.command
            or claim != self.claim
            or candidate is not None
        ):
            raise AssertionError("engine materialized the wrong transfer scope")
        return self.workspace

    def transfer_resume_gate(self, command, claim):
        if command != self.command or claim != self.claim:
            raise AssertionError("engine entered the wrong maintenance gate")
        return nullcontext()

    def transfer_effect_port(self, command, claim, workspace):
        if (
            command != self.command
            or claim != self.claim
            or workspace != self.workspace
        ):
            raise AssertionError("engine issued the wrong restricted port")
        return self.effects

    def yield_claim_for_effect(self, command, claim, effect_id):
        if (
            command != self.command
            or claim != self.claim
            or effect_id != self.pending.wait.effect_id
        ):
            raise AssertionError("engine yielded the wrong effect")
        return self.pending

    def discard_workspace(self, workspace):
        if workspace != self.workspace:
            raise AssertionError("engine discarded another workspace")
        self.discarded = True


class _TransferReconciler:
    def __init__(self, effects: _Effects, pending: StagePending) -> None:
        self.effects = effects
        self.pending = pending

    def reconcile_effect(self, *, command_id, effect_key):
        settlement = self.effects.effect_settlement(
            self.pending.wait.effect_id
        )
        return EffectReconciliationResult(
            command_id=command_id,
            effect_id=settlement.effect_id,
            effect_key=effect_key,
            fact=None,
            settlement=settlement,
            resume_phase=settlement.resume_phase,
        )


class CaptureHandlerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="capture-handler-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = ArtifactStore(self.root / "artifact-store")
        self.effect_ledger = _EffectLedger(self.store)
        self.stage_fixture = DiscoverHarness(self.root / "discover-source")
        self.stage_fixture.store = self.store
        self.stage_fixture.record = capture_record()
        self.stage_fixture.record["files"] = {
            name: self.stage_fixture.source_files[name]
            for name in (MAIN, "cad/base.SLDPRT", "cad/arm.SLDPRT")
        }
        self.source_ref = self.store.seal_tree(
            self.stage_fixture.source,
            kind=SOURCE_SET_KIND,
            producer_run_id=None,
            producer_stage=None,
            producer_tool_sha256=digest({"producer": SOURCE_SET_KIND}),
        )
        self.ruleset_ref = self.store.seal_tree(
            self.stage_fixture.rules,
            kind=RULESET_KIND,
            producer_run_id=None,
            producer_stage=None,
            producer_tool_sha256=digest({"producer": RULESET_KIND}),
        )
        self.configuration_ref = self.store.seal_tree(
            self.stage_fixture.configuration,
            kind=CONFIGURATION_KIND,
            producer_run_id=None,
            producer_stage=None,
            producer_tool_sha256=digest({"producer": CONFIGURATION_KIND}),
        )
        self.capture_tool = build_capture_tool()
        self.current_native_tool = copy.deepcopy(self.capture_tool)
        self.expected_native_tool_sha256 = self.capture_tool["source_sha256"]
        self.native_tool_sha256 = digest(self.capture_tool)
        self.native_tool_requirement_ref = self._seal_native_requirement(
            self.current_native_tool,
            "native-tool-requirement",
        )
        freeze_request = {
            "schema_version": FREEZE_REQUEST_SCHEMA,
            "request_id": "request-capture-boundary",
            "principal": "fixture:principal",
            "target_hardware": "nd_fixture",
            "purpose": "engineering",
            "change_summary": "Prove adjacent Freeze Discover Capture contracts",
            "main_assembly": MAIN,
            "ruleset_version": self.stage_fixture.ruleset_version,
            "originals_artifact_id": self.source_ref.artifact_id,
            "ruleset_artifact_id": self.ruleset_ref.artifact_id,
            "configuration_artifact_id": self.configuration_ref.artifact_id,
            "upload_inventory_sha256": digest(self.stage_fixture.source_files),
            "ruleset_catalog_sha256": self.ruleset_ref.set_sha256,
            "platform_config_sha256": self.stage_fixture.platform_config_sha256,
            "native_target_identity": CAPTURE_TARGET.to_record(),
        }
        freeze_command = StageCommand.create(
            command_id="freeze-command",
            run_id=PARENT,
            stage="freeze",
            idempotency_key=f"{PARENT}/freeze",
            request=freeze_request,
            input_artifacts=(self.source_ref, self.ruleset_ref, self.configuration_ref),
            config_snapshot_sha256=self.stage_fixture.platform_config_sha256,
            contract_sha256=digest({"contract": "freeze"}),
            policy_sha256=digest({"policy": "freeze"}),
            expected_executor_sha256=digest({"executor": "freeze"}),
            predecessor_decision_id=None,
        )
        freeze_claim = self._claim(freeze_command, "claim-freeze", fence=1)
        freeze_workspace = self._materialized_workspace(
            self.root / "freeze-workspace",
            (self.source_ref, self.ruleset_ref, self.configuration_ref),
        )
        freeze_execution = FreezeHandler().execute(
            freeze_command,
            freeze_claim,
            freeze_workspace,
            effects=self.effects(freeze_command, freeze_claim),
        )
        self.assertEqual(freeze_execution.outcome, "completed")
        self.freeze_ref = self.store.seal_tree(
            freeze_execution.candidate_root,
            kind=FREEZE_RESULT_KIND,
            producer_run_id=PARENT,
            producer_stage="freeze",
            producer_tool_sha256=freeze_command.expected_executor_sha256,
        )

        discover_request = self.stage_fixture.request(
            freeze_artifact_id=self.freeze_ref.artifact_id,
            source_set_artifact_id=self.source_ref.artifact_id,
            ruleset_artifact_id=self.ruleset_ref.artifact_id,
            configuration_artifact_id=self.configuration_ref.artifact_id,
            ruleset_catalog_sha256=self.ruleset_ref.set_sha256,
            platform_config_sha256=self.stage_fixture.platform_config_sha256,
            expected_native_tool_sha256=self.expected_native_tool_sha256,
            expected_native_tool_record_sha256=(
                self.native_tool_sha256
            ),
            native_tool_requirement_artifact_id=(
                self.native_tool_requirement_ref.artifact_id
            ),
        )
        discover_command = StageCommand.create(
            command_id="discover-command",
            run_id=PARENT,
            stage="discover",
            idempotency_key=f"{PARENT}/discover",
            request=discover_request,
            input_artifacts=(
                self.freeze_ref,
                self.source_ref,
                self.ruleset_ref,
                self.configuration_ref,
            ),
            config_snapshot_sha256=self.stage_fixture.platform_config_sha256,
            contract_sha256=digest({"contract": "discover"}),
            policy_sha256=digest({"policy": "discover"}),
            expected_executor_sha256=digest({"executor": "discover"}),
            predecessor_decision_id="freeze-decision",
        )
        discover_claim = self._claim(discover_command, "claim-discover", fence=1)
        discover_workspace = self._materialized_workspace(
            self.root / "discover-workspace",
            (
                self.freeze_ref,
                self.source_ref,
                self.ruleset_ref,
                self.configuration_ref,
            ),
        )
        discover_execution = DiscoverHandler(
            self.stage_fixture.port(
                reader=ClosureReader(
                    transform=_capture_component_closure,
                    reader_sha256=self.expected_native_tool_sha256,
                ),
                backend=CountingBackend(copy.deepcopy(self.stage_fixture.record)),
                native_tool=self.capture_tool,
            )
        ).execute(
            discover_command,
            discover_claim,
            discover_workspace,
            effects=self.effects(discover_command, discover_claim),
        )
        if discover_execution.outcome != "completed":
            self.fail(read_data(Path(discover_execution.candidate_root) / "failure.json"))
        self.assertEqual(discover_execution.outcome, "completed", discover_execution.reason_code)
        self.discover_output = Path(discover_execution.candidate_root)
        self.discover_result = read_data(self.discover_output / "result.json")
        self.discover_ref = self.store.seal_tree(
            self.discover_output,
            kind="stage_output:discover",
            producer_run_id=PARENT,
            producer_stage="discover",
            producer_tool_sha256=discover_command.expected_executor_sha256,
        )
        self.package = self.discover_output / "prepared"
        self.prepared_files = {
            path.relative_to(self.package).as_posix(): file_digest(path)
            for path in sorted(self.package.rglob("*"))
            if path.is_file()
        }
        self.handoff_sha256 = digest(self.discover_result["selection"]["files"])
        self.upload_sha256 = digest(self.stage_fixture.source_files)
        self.stage_executor = digest({"executor": "linux-capture-orchestrator"})
        self.command = self._command()
        self.claim = ExecutionClaim(
            claim_id="claim-capture",
            command_id=self.command.command_id,
            fence=1,
            owner_sha256=digest({"owner": "capture-process"}),
            executor_sha256=self.command.expected_executor_sha256,
            lane_key="native",
            expires_at=datetime(2026, 10, 25, tzinfo=UTC),
        )

    @staticmethod
    def _claim(command: StageCommand, claim_id: str, *, fence: int) -> ExecutionClaim:
        return ExecutionClaim(
            claim_id=claim_id,
            command_id=command.command_id,
            fence=fence,
            owner_sha256=digest({"owner": claim_id}),
            executor_sha256=command.expected_executor_sha256,
            lane_key="native",
            expires_at=datetime(2026, 10, 25, tzinfo=UTC),
        )

    def _seal_native_requirement(
        self,
        tool: Mapping[str, object],
        name: str,
    ) -> ArtifactRef:
        root = self.root / f"{name}-source"
        root.mkdir()
        (root / "tool.json").write_bytes(canonical(dict(tool)))
        return self.store.seal_tree(
            root,
            kind=NATIVE_TOOL_REQUIREMENT_KIND,
            producer_run_id=None,
            producer_stage=None,
            producer_tool_sha256=digest({"producer": name}),
        )

    def _native_tool_variant(self, name: str) -> dict:
        tool = copy.deepcopy(self.capture_tool)
        tool["runtime"]["execution_variant"] = name
        return tool

    def _materialized_workspace(
        self,
        root: Path,
        artifacts: tuple[ArtifactRef, ...],
    ) -> StageWorkspace:
        root.mkdir()
        inputs = {}
        for artifact_value in artifacts:
            inputs[artifact_value.artifact_id] = self.store.materialize(
                artifact_value,
                root / "inputs" / artifact_value.artifact_id,
            )
        return StageWorkspace(root=root, inputs=inputs)

    def _command(
        self,
        *,
        discover: ArtifactRef | None = None,
        requirement: ArtifactRef | None = None,
        request: dict | None = None,
        expected_executor: str | None = None,
    ) -> StageCommand:
        requirement_ref = requirement or self.native_tool_requirement_ref
        return StageCommand.create(
            command_id="command-capture",
            run_id=CHILD,
            stage="capture",
            idempotency_key=f"{CHILD}/capture",
            request=request
            or {
                "effect_key": "capture-native",
                "handoff_sha256": self.handoff_sha256,
                "main_assembly": MAIN,
                "native_target_identity": CAPTURE_TARGET.to_record(),
                "native_tool_source_sha256": self.expected_native_tool_sha256,
                "native_tool_record_sha256": self.native_tool_sha256,
                "native_tool_requirement_artifact_id": (
                    requirement_ref.artifact_id
                ),
                "prepared_sha256": digest(self.prepared_files),
                "upload_sha256": self.upload_sha256,
            },
            input_artifacts=(
                self.freeze_ref,
                discover or self.discover_ref,
                requirement_ref,
            ),
            config_snapshot_sha256=digest({"config": "capture"}),
            contract_sha256=digest({"contract": 1}),
            policy_sha256=digest({"policy": "capture"}),
            expected_executor_sha256=expected_executor or self.stage_executor,
            predecessor_decision_id="discover-decision",
        )

    def _discover(self, *, producer_run_id: str = PARENT) -> ArtifactRef:
        discover = self.discover_ref
        if producer_run_id != PARENT:
            discover = ArtifactRef(
                artifact_id=self.discover_ref.artifact_id,
                kind=self.discover_ref.kind,
                locator=self.discover_ref.locator,
                manifest_sha256=self.discover_ref.manifest_sha256,
                set_sha256=self.discover_ref.set_sha256,
                producer_run_id=producer_run_id,
                producer_stage="discover",
                producer_tool_sha256=self.discover_ref.producer_tool_sha256,
            )
        return discover

    def _diagnostic_command(
        self,
        current_tool: Mapping[str, object],
        *,
        expected_executor: str | None = None,
        requirement_tool: Mapping[str, object] | None = None,
    ) -> tuple[StageCommand, ArtifactRef]:
        requirement = self._seal_native_requirement(
            requirement_tool or current_tool,
            "diagnostic-native-tool-requirement",
        )
        input_artifacts = (
            self.freeze_ref,
            self.discover_ref,
            requirement,
        )
        command = StageCommand.create(
            command_id="command-capture-diagnostic",
            run_id=CHILD,
            stage="capture",
            idempotency_key=f"{CHILD}/diagnostic/capture",
            request={
                "effect_key": "capture-native-diagnostic",
                "handoff_sha256": self.handoff_sha256,
                "main_assembly": MAIN,
                "native_target_identity": CAPTURE_TARGET.to_record(),
                "native_tool_source_sha256": current_tool["source_sha256"],
                "native_tool_record_sha256": digest(current_tool),
                "native_tool_requirement_artifact_id": requirement.artifact_id,
                "prepared_sha256": digest(self.prepared_files),
                "upload_sha256": self.upload_sha256,
            },
            input_artifacts=input_artifacts,
            config_snapshot_sha256=digest({"config": "capture-diagnostic"}),
            contract_sha256=digest({"contract": "capture-diagnostic"}),
            policy_sha256=digest({"policy": "capture-diagnostic"}),
            expected_executor_sha256=expected_executor or self.stage_executor,
            predecessor_decision_id=None,
            diagnostic_scope=DiagnosticReplayScope.from_inputs(
                "capture",
                input_artifacts,
            ),
        )
        return command, requirement

    def workspace(
        self,
        *,
        discover_run_id: str = PARENT,
        name: str | None = None,
    ) -> StageWorkspace:
        discover = self._discover(producer_run_id=discover_run_id)
        root = self.root / (name or f"workspace-{discover_run_id}")
        return self._materialized_workspace(
            root,
            (
                self.freeze_ref,
                discover,
                self.native_tool_requirement_ref,
            ),
        )

    def handler(
        self,
        *,
        native_operation=None,
        tool=None,
    ) -> CaptureHandler:
        return CaptureHandler(
            artifact_store=self.store,
            backend_factory=lambda package: fixture_backend(package, MAIN),
            native_tool_factory=lambda: (
                self.current_native_tool if tool is None else tool
            ),
            native_operation=native_operation,
        )

    def effects(
        self,
        command: StageCommand | None = None,
        claim: ExecutionClaim | None = None,
    ) -> _Effects:
        return _Effects(
            self.effect_ledger,
            command or self.command,
            claim or self.claim,
        )

    def _pending_transfer(
        self,
        *,
        name: str,
    ) -> tuple[
        StageWorkspace,
        _Effects,
        ExecutionClaim,
        EffectIntent,
        StageEffectWait,
    ]:
        workspace = self.workspace(name=name)
        effects = self.effects()

        def unavailable(*_args, **_kwargs):
            raise RuntimeError("native response was lost")

        with self.assertRaisesRegex(RuntimeError, "response was lost"):
            self.handler(native_operation=unavailable).execute(
                self.command,
                self.claim,
                workspace,
                effects=effects,
            )
        intent = next(
            item
            for item in self.effect_ledger.intents.values()
            if item.command_id == self.command.command_id
        )
        pending_root = workspace.root / "initial-transfer-pending"
        pending_root.mkdir()
        write_json(
            pending_root / "operation.json",
            {
                "operation_id": intent.expected_state["operation_id"],
                "state": "accepted",
            },
        )
        effects.record_observation(
            intent.effect_id,
            source="observer",
            state_class="unobservable",
            observation_root=pending_root,
            resume_phase=EffectResumePhase.INPUT_TRANSFER_PENDING,
        )
        claim = replace(
            self.claim,
            claim_id="claim-capture-transfer",
            fence=2,
            scope="transfer_resume",
            effect_scope_id=intent.effect_id,
        )
        wait = StageEffectWait(
            schema_version=STAGE_EFFECT_WAIT_SCHEMA,
            run_id=self.command.run_id,
            stage="capture",
            command_id=self.command.command_id,
            request_digest=self.command.request_digest,
            effect_id=intent.effect_id,
            effect_key=intent.effect_key,
            effect_kind="native_operation",
            intent_identity_digest=intent.identity_digest,
            outbound_artifact_id=intent.outbound_artifact_id,
            outbound_digest=intent.outbound_digest,
            expected_state_digest=intent.expected_state_digest,
        )
        return workspace, effects, claim, intent, wait

    def _observer_client(
        self,
        *,
        archive: Path,
        outbound_root: Path,
    ) -> tuple[NativeOperationClient, str]:
        outbound = read_data(outbound_root / "outbound.json")
        input_archive = outbound_root / capture_module.CAPTURE_INPUT_ARCHIVE
        self.assertTrue(input_archive.is_file())
        request = CaptureOperationRequest.from_record(
            {
                "schema_version": CAPTURE_OPERATION_SCHEMA,
                "command_id": self.command.command_id,
                "run_id": self.command.run_id,
                "request_digest": self.command.request_digest,
                "effect_key": "capture-native",
                "handoff_sha256": outbound["handoff_sha256"],
                "upload_sha256": outbound["upload_sha256"],
                "prepared_sha256": outbound["prepared_sha256"],
                "main_assembly": outbound["main_assembly"],
                "package_files": outbound["prepared_files"],
                "upstream_artifacts": outbound["upstream_artifacts"],
                "execution": outbound["execution"],
                "native_tool_source_sha256": (
                    outbound["native_tool_source_sha256"]
                ),
                "request_evidence": outbound["request_evidence"],
            }
        )
        operation_id = native_capture_operation_id(
            run_id=self.command.run_id,
            command_id=self.command.command_id,
            effect_key="capture-native",
        )
        artifact = NativeArtifactView(
            name=CAPTURE_ARCHIVE,
            bytes=archive.stat().st_size,
            sha256=file_digest(archive),
            media_type="application/zip",
        )
        now = datetime.now(UTC).isoformat()
        native_owner_sha256 = digest(
            {"native_operation_claim": operation_id}
        )
        native_executor_sha256 = digest(self.current_native_tool)
        view = NativeOperationView(
            schema_version=OPERATION_VIEW_SCHEMA,
            operation_id=operation_id,
            kind=CAPTURE_KIND,
            target_id=CAPTURE_TARGET.target_id,
            payload_sha256=outbound["payload_sha256"],
            state="satisfied",
            request_sha256=digest(request.to_record()),
            input_archive_sha256=(
                outbound["input_archive_sha256"]
            ),
            native_tool_requirement_sha256=digest(
                self.current_native_tool
            ),
            actual_native_tool_sha256=native_executor_sha256,
            owner_sha256=native_owner_sha256,
            executor_sha256=native_executor_sha256,
            resource_terminated=True,
            artifact=artifact,
            failure=None,
            created_at=now,
            started_at=now,
            finished_at=now,
        )
        client = NativeOperationClient(
            NativeOperationEndpoint("http://127.0.0.1:1", "t" * 32),
            target_id=CAPTURE_TARGET.target_id,
        )
        client.get_operation = Mock(return_value=view)

        def download(_operation_id, destination):
            target = Path(destination)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(archive, target)
            return ArtifactDownload(
                operation_id=operation_id,
                path=target,
                name=CAPTURE_ARCHIVE,
                bytes=target.stat().st_size,
                sha256=file_digest(target),
                media_type="application/zip",
            )

        client.download_artifact = Mock(side_effect=download)
        client.create_operation = Mock(
            side_effect=AssertionError("observer must not create")
        )
        client.upload_input = Mock(
            side_effect=AssertionError("observer must not upload")
        )
        client.cancel_operation = Mock(
            side_effect=AssertionError("observer must not cancel")
        )
        return client, operation_id

    @staticmethod
    def _rewrite_capture_archive(
        source: Path,
        destination: Path,
        *,
        remove: set[str] | None = None,
        replace_manifest: dict | None = None,
        add: tuple[tuple[str, bytes], ...] = (),
    ) -> None:
        remove = remove or set()
        with zipfile.ZipFile(source) as archive:
            entries = [
                (info.filename, archive.read(info))
                for info in archive.infolist()
            ]
        with zipfile.ZipFile(
            destination,
            "w",
            zipfile.ZIP_DEFLATED,
        ) as archive:
            for name, payload in entries:
                if name in remove:
                    continue
                if (
                    name == "transfer-manifest.json"
                    and replace_manifest is not None
                ):
                    payload = canonical(replace_manifest)
                archive.writestr(zipfile.ZipInfo(name), payload)
            for name, payload in add:
                archive.writestr(zipfile.ZipInfo(name), payload)

    def test_real_freeze_discover_artifact_store_then_capture_keeps_identities_separate(self) -> None:
        discovery_before = (self.package / "discovery/native-discovery.json").read_bytes()
        effects = self.effects()
        with patch("description_pipeline.runtime.tool_record", return_value=self.capture_tool):
            execution = self.handler().execute(
                self.command,
                self.claim,
                self.workspace(),
                effects=effects,
            )

        if execution.outcome != "completed":
            failure = Path(execution.candidate_root) / "failure.json"
            self.fail(read_data(failure) if failure.is_file() else execution.reason_code)
        self.assertEqual(execution.outcome, "completed")
        admitted = Path(execution.candidate_root)
        manifest = json.loads((admitted / "transfer-manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["schema_version"], "solidworks-to-urdf.capture-transfer/v3")
        self.assertEqual(manifest["run_id"], CHILD)
        self.assertEqual(manifest["native_stage_scope"], ["capture"])
        self.assertEqual(manifest["execution"]["effect_key"], "capture-native")
        self.assertEqual(manifest["execution"]["executor_sha256"], self.stage_executor)
        self.assertEqual(manifest["execution"]["native_tool_sha256"], self.native_tool_sha256)
        self.assertEqual(manifest["native_tool_sha256"], self.native_tool_sha256)
        self.assertNotEqual(
            manifest["execution"]["owner_sha256"],
            manifest["execution"]["executor_sha256"],
        )
        self.assertNotEqual(
            manifest["execution"]["executor_sha256"],
            manifest["execution"]["native_tool_sha256"],
        )
        self.assertEqual(
            {
                row["producer_stage"]: row["producer_run_id"]
                for row in manifest["upstream_artifacts"]
            },
            {"discover": PARENT, "freeze": PARENT},
        )
        self.assertEqual((self.package / "discovery/native-discovery.json").read_bytes(), discovery_before)
        self.assertEqual(effects.authorized[0][0], "capture-native")
        self.assertEqual(effects.observed[0][0], "response")
        self.assertEqual(effects.observed[1][0], "known")

    def test_satisfied_effect_reuses_sealed_payload_without_native_call(self) -> None:
        first_effects = self.effects()
        with patch("description_pipeline.runtime.tool_record", return_value=self.capture_tool):
            first = self.handler().execute(
                self.command,
                self.claim,
                self.workspace(),
                effects=first_effects,
            )
        self.assertEqual(first.outcome, "completed")

        replay_claim = self._claim(self.command, "claim-capture-replay", fence=2)
        replay_effects = self.effects(self.command, replay_claim)
        operation = Mock(side_effect=AssertionError("satisfied effects must not resend"))
        with patch("description_pipeline.runtime.tool_record", return_value=self.capture_tool):
            replay = self.handler(native_operation=operation).execute(
                self.command,
                replay_claim,
                self.workspace(name="workspace-replay"),
                effects=replay_effects,
            )
        self.assertEqual(replay.outcome, "completed")
        operation.assert_not_called()

    def test_effect_outbound_recomputes_after_prepared_inputs_are_removed(self) -> None:
        workspace = self.workspace()
        effects = self.effects()
        with patch(
            "description_pipeline.runtime.tool_record",
            return_value=self.capture_tool,
        ):
            execution = self.handler().execute(
                self.command,
                self.claim,
                workspace,
                effects=effects,
            )
        self.assertEqual(execution.outcome, "completed")
        outbound_path = workspace.root / "capture-effect-outbound/outbound.json"
        outbound = read_data(outbound_path)
        input_archive = (
            outbound_path.parent
            / capture_module.CAPTURE_INPUT_ARCHIVE
        )
        self.assertEqual(
            outbound["prepared_files"],
            self.prepared_files,
        )
        self.assertEqual(
            outbound["handoff_sha256"],
            self.handoff_sha256,
        )
        self.assertEqual(outbound["upload_sha256"], self.upload_sha256)
        self.assertEqual(
            outbound["native_tool_source_sha256"],
            self.expected_native_tool_sha256,
        )
        self.assertEqual(
            outbound["native_tool_record_sha256"],
            self.native_tool_sha256,
        )
        self.assertEqual(
            outbound["input_archive_bytes"],
            input_archive.stat().st_size,
        )
        self.assertEqual(
            outbound["input_archive_sha256"],
            file_digest(input_archive),
        )
        self.assertEqual(
            outbound["operation_id"],
            native_capture_operation_id(
                run_id=self.command.run_id,
                command_id=self.command.command_id,
                effect_key="capture-native",
            ),
        )
        self.assertRegex(outbound["payload_sha256"], r"\A[0-9a-f]{64}\Z")
        for root in workspace.inputs.values():
            shutil.rmtree(root)
        self.assertEqual(
            capture_module.validate_capture_effect_outbound(
                outbound_path.parent,
                self.command,
            ),
            outbound,
        )

    def test_transfer_resume_uses_only_the_sealed_archive_and_yields(self) -> None:
        workspace, _effects, claim, intent, wait = self._pending_transfer(
            name="capture-transfer-success"
        )
        resume_port = Mock()
        resume_port.resume_input_transfer.return_value = NativeOperationView(
            schema_version=OPERATION_VIEW_SCHEMA,
            operation_id=intent.expected_state["operation_id"],
            kind=CAPTURE_KIND,
            target_id=CAPTURE_TARGET.target_id,
            payload_sha256=intent.expected_state["payload_sha256"],
            state="accepted",
            request_sha256="e" * 64,
            input_archive_sha256=intent.expected_state[
                "input_archive_sha256"
            ],
            native_tool_requirement_sha256=self.native_tool_sha256,
            actual_native_tool_sha256=None,
            owner_sha256=None,
            executor_sha256=None,
            resource_terminated=False,
            artifact=None,
            failure=None,
            created_at=datetime.now(UTC).isoformat(),
            started_at=None,
            finished_at=None,
        )
        transfer_effects = _Effects(self.effect_ledger, self.command, claim)
        result = self.handler(native_operation=resume_port).resume_transfer(
            self.command,
            claim,
            workspace,
            effects=transfer_effects,
            resume=wait,
        )

        self.assertEqual(result.effect_id, intent.effect_id)
        resume_port.assert_not_called()
        resume_port.resume_input_transfer.assert_called_once()
        dispatch = resume_port.resume_input_transfer.call_args.args[0]
        self.assertEqual(
            file_digest(dispatch.input_archive),
            intent.expected_state["input_archive_sha256"],
        )
        self.assertEqual(
            resume_port.resume_input_transfer.call_args.kwargs[
                "native_tool"
            ],
            self.current_native_tool,
        )
        self.assertEqual(
            transfer_effects.authorized,
            [(intent.effect_key, claim.claim_id, claim.fence)],
        )
        self.assertIn(("response", intent.effect_id), transfer_effects.observed)
        settlement = transfer_effects.effect_settlement(intent.effect_id)
        self.assertEqual(settlement.state, EffectState.UNKNOWN)
        self.assertEqual(
            settlement.resume_phase,
            EffectResumePhase.INPUT_TRANSFER_PENDING,
        )

    def test_transfer_resume_without_observation_yields_without_create(
        self,
    ) -> None:
        workspace, _effects, claim, intent, wait = self._pending_transfer(
            name="capture-transfer-unobservable"
        )
        resume_port = Mock()
        resume_port.resume_input_transfer.return_value = None
        transfer_effects = _Effects(self.effect_ledger, self.command, claim)
        result = self.handler(native_operation=resume_port).resume_transfer(
            self.command,
            claim,
            workspace,
            effects=transfer_effects,
            resume=wait,
        )

        self.assertEqual(result.effect_id, intent.effect_id)
        resume_port.assert_not_called()
        resume_port.resume_input_transfer.assert_called_once()
        self.assertIn(("timeout", intent.effect_id), transfer_effects.observed)
        settlement = transfer_effects.effect_settlement(intent.effect_id)
        self.assertEqual(settlement.state, EffectState.UNKNOWN)
        self.assertEqual(
            settlement.resume_phase,
            EffectResumePhase.INPUT_TRANSFER_PENDING,
        )

    def test_engine_routes_capture_transfer_resume_through_the_narrow_port(
        self,
    ) -> None:
        workspace, _effects, claim, intent, wait = self._pending_transfer(
            name="capture-transfer-engine"
        )
        pending = StagePending(wait)
        resume_port = Mock()
        resume_port.resume_input_transfer.return_value = NativeOperationView(
            schema_version=OPERATION_VIEW_SCHEMA,
            operation_id=intent.expected_state["operation_id"],
            kind=CAPTURE_KIND,
            target_id=CAPTURE_TARGET.target_id,
            payload_sha256=intent.expected_state["payload_sha256"],
            state="accepted",
            request_sha256="e" * 64,
            input_archive_sha256=intent.expected_state[
                "input_archive_sha256"
            ],
            native_tool_requirement_sha256=self.native_tool_sha256,
            actual_native_tool_sha256=None,
            owner_sha256=None,
            executor_sha256=None,
            resource_terminated=False,
            artifact=None,
            failure=None,
            created_at=datetime.now(UTC).isoformat(),
            started_at=None,
            finished_at=None,
        )
        transfer_effects = _Effects(self.effect_ledger, self.command, claim)
        services = _TransferServices(
            command=self.command,
            claim=claim,
            workspace=workspace,
            pending=pending,
            effects=transfer_effects,
        )
        result = execute_stage(
            command_id=self.command.command_id,
            handler=self.handler(native_operation=resume_port),
            services=services,
            resume=wait,
            resume_reconciler=_TransferReconciler(
                transfer_effects,
                pending,
            ),
        )

        self.assertEqual(result, pending)
        resume_port.assert_not_called()
        resume_port.resume_input_transfer.assert_called_once()
        self.assertTrue(services.discarded)
        settlement = transfer_effects.effect_settlement(intent.effect_id)
        self.assertEqual(settlement.state, EffectState.UNKNOWN)

    def test_effect_outbound_rejects_native_target_drift(self) -> None:
        workspace = self.workspace(name="capture-target-drift")
        effects = self.effects()
        request = capture_module.CaptureRequest.from_command(self.command)
        inputs = capture_module.CaptureInputs.resolve(
            self.command,
            workspace,
            request,
        )
        self.handler()._effect_state(
            self.command,
            self.claim,
            workspace,
            request,
            inputs,
            self.current_native_tool,
            effects,
        )
        outbound_root = workspace.root / "capture-effect-outbound"
        outbound = read_data(outbound_root / "outbound.json")
        outbound["native_target_id"] = "f" * 64
        write_json(outbound_root / "outbound.json", outbound)

        with self.assertRaisesRegex(
            capture_module.ProtocolError,
            "capture.effect_outbound",
        ):
            capture_module.validate_capture_effect_outbound(
                outbound_root,
                self.command,
            )

    def test_trigger_reads_capture_identity_without_materializing_archive(self) -> None:
        effects = self.effects()
        operation = Mock(side_effect=RuntimeError("transport stopped"))
        with self.assertRaisesRegex(RuntimeError, "transport stopped"):
            self.handler(native_operation=operation).execute(
                self.command,
                self.claim,
                self.workspace(name="capture-trigger-small-identity"),
                effects=effects,
            )

        intent = next(
            item
            for item in self.effect_ledger.intents.values()
            if item.command_id == self.command.command_id
        )
        settlement = effects.effect_settlement(intent.effect_id)
        evidence = _EffectEvidence(
            artifacts=self.store,
            command=self.command,
            intent=intent,
            settlement=settlement,
        )
        with patch.object(
            self.store,
            "materialize",
            side_effect=AssertionError("trigger must not materialize"),
        ):
            kind, expected = _native_expectation(evidence)

        self.assertEqual(kind, CAPTURE_KIND)
        self.assertEqual(
            expected.operation_id,
            intent.expected_state["operation_id"],
        )
        self.assertEqual(
            expected.input_archive_sha256,
            intent.expected_state["input_archive_sha256"],
        )
        self.assertEqual(
            expected.payload_sha256,
            intent.expected_state["payload_sha256"],
        )

    def test_effect_outbound_rejects_changed_exact_input_archive(self) -> None:
        workspace = self.workspace(name="capture-outbound-archive")
        effects = self.effects()
        with patch(
            "description_pipeline.runtime.tool_record",
            return_value=self.capture_tool,
        ):
            execution = self.handler().execute(
                self.command,
                self.claim,
                workspace,
                effects=effects,
            )
        self.assertEqual(execution.outcome, "completed")
        settlement = effects.effect_settlement_for_key("capture-native")
        self.assertIsNotNone(settlement)
        self.assertIsNotNone(settlement.outbound_artifact)
        recovered = self.store.materialize(
            settlement.outbound_artifact,
            self.root / "recovered-effect-outbound",
        )
        archive = recovered / capture_module.CAPTURE_INPUT_ARCHIVE
        archive.write_bytes(archive.read_bytes() + b"tampered")
        with self.assertRaisesRegex(Exception, "effect_outbound"):
            capture_module.validate_capture_effect_outbound(
                recovered,
                self.command,
            )

    def test_effect_reuse_rejects_a_different_current_outbound_closure(self) -> None:
        workspace = self.workspace()
        effects = self.effects()
        with patch(
            "description_pipeline.runtime.tool_record",
            return_value=self.capture_tool,
        ):
            execution = self.handler().execute(
                self.command,
                self.claim,
                workspace,
                effects=effects,
            )
        self.assertEqual(execution.outcome, "completed")
        request = capture_module.CaptureRequest.from_command(
            self.command
        )
        inputs = capture_module.CaptureInputs.resolve(
            self.command,
            workspace,
            request,
        )
        changed = replace(inputs, object_names=("changed-object",))
        with self.assertRaisesRegex(
            Exception,
            "effect_identity_conflict",
        ):
            self.handler()._effect_state(
                self.command,
                self.claim,
                workspace,
                request,
                changed,
                self.capture_tool,
                effects,
            )

    def test_native_effect_observer_returns_capture_payload_without_posting(self) -> None:
        effects = self.effects()
        workspace = self.workspace()
        with patch(
            "description_pipeline.runtime.tool_record",
            return_value=self.current_native_tool,
        ):
            completed = self.handler().execute(
                self.command,
                self.claim,
                workspace,
                effects=effects,
            )
        self.assertEqual(completed.outcome, "completed")
        payload = workspace.root / "capture-effect-payload"
        archive = payload / "native-evidence.zip"
        outbound_root = workspace.root / "capture-effect-outbound"
        outbound = read_data(outbound_root / "outbound.json")
        intent = next(
            item
            for item in self.effect_ledger.intents.values()
            if item.command_id == self.command.command_id
        )
        client, operation_id = self._observer_client(
            archive=archive,
            outbound_root=outbound_root,
        )
        view = client.get_operation.return_value
        native_owner = digest(
            {"native_operation_claim": operation_id}
        )
        native_executor = digest(self.current_native_tool)
        self.assertEqual(view.payload_sha256, outbound["payload_sha256"])
        self.assertEqual(
            view.input_archive_sha256,
            outbound["input_archive_sha256"],
        )
        self.assertNotEqual(
            native_owner,
            outbound["execution"]["owner_sha256"],
        )
        self.assertNotEqual(
            native_executor,
            outbound["execution"]["executor_sha256"],
        )
        observer_root = self.root / "capture-observer-work"
        observer_root.mkdir()
        evidence = NativeOperationEffectObserver(client).observe(
            command=self.command,
            intent=intent,
            outbound_root=outbound_root,
            workspace_root=observer_root,
        )
        self.assertEqual(evidence.fact_type, "reconciled")
        self.assertEqual(evidence.state_class, "known")
        self.assertEqual(
            {path.name for path in evidence.payload_root.iterdir()},
            {"execution.json", "native-evidence.zip", "native-tool.json"},
        )
        self.assertEqual(
            evidence.metadata["request_sha256"],
            view.request_sha256,
        )
        identity = evidence.metadata["identity"]
        self.assertEqual(identity["platform_origin"]["claim_id"], self.claim.claim_id)
        self.assertEqual(identity["platform_origin"]["fence"], self.claim.fence)
        self.assertEqual(
            identity["platform_origin"]["owner_sha256"],
            outbound["execution"]["owner_sha256"],
        )
        self.assertEqual(
            identity["platform_origin"]["executor_sha256"],
            self.stage_executor,
        )
        self.assertEqual(
            identity["native_runtime"]["owner_sha256"],
            native_owner,
        )
        self.assertEqual(
            identity["native_runtime"]["owner_binding"],
            "native_claim_projection",
        )
        self.assertEqual(
            identity["native_runtime"]["actual_native_tool_sha256"],
            native_executor,
        )
        self.assertEqual(
            identity["native_runtime"]["executor_sha256"],
            native_executor,
        )
        self.assertEqual(
            identity["native_runtime"]["executor_binding"],
            "tool_observed_and_archive",
        )
        self.assertEqual(client.get_operation.call_count, 2)
        client.create_operation.assert_not_called()
        client.upload_input.assert_not_called()
        client.cancel_operation.assert_not_called()

    def test_native_effect_observer_marks_missing_claimed_owner_unestablished(
        self,
    ) -> None:
        effects = self.effects()
        workspace = self.workspace(name="workspace-missing-native-owner")
        with patch(
            "description_pipeline.runtime.tool_record",
            return_value=self.current_native_tool,
        ):
            completed = self.handler().execute(
                self.command,
                self.claim,
                workspace,
                effects=effects,
            )
        self.assertEqual(completed.outcome, "completed")
        archive = workspace.root / "capture-effect-payload/native-evidence.zip"
        outbound_root = workspace.root / "capture-effect-outbound"
        intent = next(
            item
            for item in self.effect_ledger.intents.values()
            if item.command_id == self.command.command_id
        )
        client, _operation_id = self._observer_client(
            archive=archive,
            outbound_root=outbound_root,
        )
        observed = client.get_operation.return_value
        client.get_operation = Mock(
            return_value=replace(observed, owner_sha256=None)
        )
        observer_root = self.root / "capture-observer-missing-native-owner"
        observer_root.mkdir()

        evidence = NativeOperationEffectObserver(client).observe(
            command=self.command,
            intent=intent,
            outbound_root=outbound_root,
            workspace_root=observer_root,
        )

        self.assertEqual(evidence.fact_type, "reconciled")
        self.assertEqual(evidence.state_class, "known")
        identity = evidence.metadata["identity"]
        self.assertIsNone(identity["native_runtime"]["owner_sha256"])
        self.assertEqual(
            identity["native_runtime"]["owner_binding"],
            "unestablished",
        )
        self.assertEqual(
            identity["platform_origin"]["owner_sha256"],
            self.claim.owner_sha256,
        )

    def test_native_effect_observer_rejects_executor_not_actual_native_tool(self) -> None:
        effects = self.effects()
        workspace = self.workspace(name="workspace-foreign-native-executor")
        with patch(
            "description_pipeline.runtime.tool_record",
            return_value=self.current_native_tool,
        ):
            completed = self.handler().execute(
                self.command,
                self.claim,
                workspace,
                effects=effects,
            )
        self.assertEqual(completed.outcome, "completed")
        payload = workspace.root / "capture-effect-payload"
        archive = payload / "native-evidence.zip"
        outbound_root = workspace.root / "capture-effect-outbound"
        intent = next(
            item
            for item in self.effect_ledger.intents.values()
            if item.command_id == self.command.command_id
        )
        client, _operation_id = self._observer_client(
            archive=archive,
            outbound_root=outbound_root,
        )
        view = client.get_operation.return_value
        self.assertIsNotNone(view.actual_native_tool_sha256)
        client.get_operation = Mock(
            return_value=replace(
                view,
                executor_sha256=self.stage_executor,
            )
        )
        client.download_artifact = Mock(
            side_effect=AssertionError(
                "executor mismatch must be rejected before artifact download"
            )
        )
        observer_root = self.root / "capture-observer-foreign-executor"
        observer_root.mkdir()

        with self.assertRaises(NativeOperationClientConflict) as raised:
            NativeOperationEffectObserver(client).observe(
                command=self.command,
                intent=intent,
                outbound_root=outbound_root,
                workspace_root=observer_root,
            )

        self.assertEqual(raised.exception.code, "native_executor_mismatch")
        client.download_artifact.assert_not_called()
        self.assertFalse(
            any(observer_root.glob("admitted-capture-*"))
        )

    def test_native_effect_observer_rejects_foreign_operation_envelope(self) -> None:
        effects = self.effects()
        workspace = self.workspace(name="workspace-foreign-operation-envelope")
        with patch(
            "description_pipeline.runtime.tool_record",
            return_value=self.current_native_tool,
        ):
            completed = self.handler().execute(
                self.command,
                self.claim,
                workspace,
                effects=effects,
            )
        self.assertEqual(completed.outcome, "completed")
        archive = workspace.root / "capture-effect-payload/native-evidence.zip"
        outbound_root = workspace.root / "capture-effect-outbound"
        intent = next(
            item
            for item in self.effect_ledger.intents.values()
            if item.command_id == self.command.command_id
        )

        for field in ("payload_sha256", "input_archive_sha256"):
            with self.subTest(field=field):
                client, _operation_id = self._observer_client(
                    archive=archive,
                    outbound_root=outbound_root,
                )
                observed = client.get_operation.return_value
                client.get_operation = Mock(
                    return_value=replace(
                        observed,
                        **{field: digest({"foreign": field})},
                    )
                )
                client.download_artifact = Mock(
                    side_effect=AssertionError(
                        "foreign operation envelope must be rejected before download"
                    )
                )
                observer_root = self.root / f"capture-observer-{field}"
                observer_root.mkdir()

                with self.assertRaises(NativeOperationClientConflict) as raised:
                    NativeOperationEffectObserver(client).observe(
                        command=self.command,
                        intent=intent,
                        outbound_root=outbound_root,
                        workspace_root=observer_root,
                    )

                self.assertEqual(
                    raised.exception.code,
                    "native_operation_conflict",
                )
                client.download_artifact.assert_not_called()

    def test_native_effect_observer_rejects_missing_request_evidence(self) -> None:
        effects = self.effects()
        workspace = self.workspace()
        with patch(
            "description_pipeline.runtime.tool_record",
            return_value=self.current_native_tool,
        ):
            completed = self.handler().execute(
                self.command,
                self.claim,
                workspace,
                effects=effects,
            )
        self.assertEqual(completed.outcome, "completed")
        outbound_root = workspace.root / "capture-effect-outbound"
        outbound = read_data(outbound_root / "outbound.json")
        outbound.pop("request_evidence")
        outbound_root.joinpath("outbound.json").write_bytes(
            canonical(outbound)
        )
        intent = next(
            item
            for item in self.effect_ledger.intents.values()
            if item.command_id == self.command.command_id
        )
        observer_root = self.root / "capture-observer-missing-evidence"
        observer_root.mkdir()
        client = NativeOperationClient(
            NativeOperationEndpoint("http://127.0.0.1:1", "t" * 32),
            target_id=CAPTURE_TARGET.target_id,
        )
        with self.assertRaisesRegex(Exception, "effect_outbound"):
            NativeOperationEffectObserver(client).observe(
                command=self.command,
                intent=intent,
                outbound_root=outbound_root,
                workspace_root=observer_root,
            )

    def test_prepared_effect_retry_sends_the_original_sealed_request_and_archive(
        self,
    ) -> None:
        first_workspace = self.workspace(name="capture-prepared-origin")
        first_effects = self.effects()
        request = capture_module.CaptureRequest.from_command(self.command)
        inputs = capture_module.CaptureInputs.resolve(
            self.command,
            first_workspace,
            request,
        )
        effect_id, first_settlement = self.handler()._effect_state(
            self.command,
            self.claim,
            first_workspace,
            request,
            inputs,
            self.current_native_tool,
            first_effects,
        )
        self.assertIsNone(first_settlement)
        # Simulate the process crash after prepare_effect sealed the outbound
        # artifact but before the original claim authorized dispatch.
        first_effects.ledger.facts[effect_id] = []
        first_effects.authorized.clear()
        prepared = first_effects.effect_settlement_for_key("capture-native")
        self.assertEqual(prepared.state, EffectState.PREPARED)
        self.assertIsNotNone(prepared.outbound_artifact)
        sealed_root = self.store.materialize(
            prepared.outbound_artifact,
            self.root / "prepared-effect-sealed",
        )
        sealed_outbound = read_data(sealed_root / "outbound.json")
        sealed_archive = (
            sealed_root / capture_module.CAPTURE_INPUT_ARCHIVE
        )
        self.assertTrue(sealed_archive.is_file())

        operation_id = native_capture_operation_id(
            run_id=self.command.run_id,
            command_id=self.command.command_id,
            effect_key="capture-native",
        )
        created: list[dict] = []
        uploaded: list[bytes] = []
        now = datetime.now(UTC).isoformat()

        class Response:
            def __init__(self, body: dict) -> None:
                self.headers = Message()
                self.headers["Content-Type"] = (
                    "application/json; charset=utf-8"
                )
                self.headers["X-Native-Target-ID"] = (
                    CAPTURE_TARGET.target_id
                )
                self._body = io.BytesIO(
                    (json.dumps(body, sort_keys=True) + "\n").encode()
                )

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                self._body.close()
                return False

            def read(self, limit: int = -1):
                return self._body.read(limit)

        def opener(request, *, timeout):
            del timeout
            method = request.get_method()
            path = request.full_url
            if method == "GET" and path.endswith("/target"):
                return Response(CAPTURE_TARGET.to_record())
            if method == "GET" and path.endswith(
                f"/operations/{operation_id}"
            ):
                raise urlerror.HTTPError(
                    request.full_url,
                    404,
                    "Not Found",
                    Message(),
                    io.BytesIO(b'{"error":"missing"}\n'),
                )
            if method == "POST" and path.endswith("/operations"):
                envelope = json.loads(request.data.decode("utf-8"))
                created.append(copy.deepcopy(envelope))
                return Response(
                    {
                        "schema_version": OPERATION_VIEW_SCHEMA,
                        "operation_id": envelope["operation_id"],
                        "kind": envelope["kind"],
                        "target_id": CAPTURE_TARGET.target_id,
                        "payload_sha256": envelope["payload_sha256"],
                        "state": "accepted",
                        "request_sha256": envelope["request_sha256"],
                        "input_archive_sha256": envelope["input"][
                            "archive_sha256"
                        ],
                        "native_tool_requirement_sha256": envelope[
                            "native_tool"
                        ]["requirement_sha256"],
                        "actual_native_tool_sha256": None,
                        "owner_sha256": None,
                        "executor_sha256": None,
                        "resource_terminated": False,
                        "artifact": None,
                        "failure": None,
                        "created_at": now,
                        "started_at": None,
                        "finished_at": None,
                    }
                )
            if method == "POST" and path.endswith(
                f"/operations/{operation_id}/input"
            ):
                uploaded.append(request.data.read())
                envelope = created[0]
                return Response(
                    {
                        "schema_version": OPERATION_VIEW_SCHEMA,
                        "operation_id": operation_id,
                        "kind": CAPTURE_KIND,
                        "target_id": CAPTURE_TARGET.target_id,
                        "payload_sha256": envelope["payload_sha256"],
                        "state": "unknown",
                        "request_sha256": envelope["request_sha256"],
                        "input_archive_sha256": envelope["input"][
                            "archive_sha256"
                        ],
                        "native_tool_requirement_sha256": envelope[
                            "native_tool"
                        ]["requirement_sha256"],
                        "actual_native_tool_sha256": digest(
                            self.current_native_tool
                        ),
                        "owner_sha256": digest(
                            {
                                "native_operation_claim": operation_id,
                            }
                        ),
                        "executor_sha256": digest(
                            self.current_native_tool
                        ),
                        "resource_terminated": True,
                        "artifact": None,
                        "failure": None,
                        "created_at": now,
                        "started_at": now,
                        "finished_at": now,
                    }
                )
            raise AssertionError(
                f"unexpected native request: {method} {path}"
            )

        client = NativeOperationClient(
            NativeOperationEndpoint(
                "http://127.0.0.1:1",
                "t" * 32,
            ),
            target_id=CAPTURE_TARGET.target_id,
            opener=opener,
        )
        replay_claim = self._claim(
            self.command,
            "claim-capture-after-prepared-crash",
            fence=2,
        )
        remote_handler = CaptureHandler(
            artifact_store=self.store,
            native_operation=NativeOperationCapturePort(client),
            native_tool_factory=lambda: self.current_native_tool,
        )
        with self.assertRaises(CaptureUnresolved):
            remote_handler.execute(
                self.command,
                replay_claim,
                self.workspace(name="capture-prepared-retry"),
                effects=first_effects,
            )

        self.assertEqual(len(created), 1)
        self.assertEqual(len(uploaded), 1)
        self.assertEqual(
            created[0]["request"]["execution"],
            sealed_outbound["execution"],
        )
        self.assertEqual(
            created[0]["payload_sha256"],
            sealed_outbound["payload_sha256"],
        )
        self.assertEqual(
            created[0]["input"]["archive_sha256"],
            sealed_outbound["input_archive_sha256"],
        )
        self.assertEqual(
            created[0]["input"]["archive_bytes"],
            sealed_outbound["input_archive_bytes"],
        )
        self.assertEqual(uploaded[0], sealed_archive.read_bytes())

    def test_pending_capture_reconciles_producer_archive_then_replays_exact_outbound(
        self,
    ) -> None:
        workspace = self.workspace(name="capture-pending-workspace")
        effects = self.effects()
        recovered_archive = self.root / "recovered-producer-capture.zip"
        native_calls = 0

        def lose_response(inputs, output, **kwargs):
            nonlocal native_calls
            native_calls += 1
            completed = capture_module._invoke_native_operation(
                inputs,
                output,
                **kwargs,
            )
            self.assertEqual(
                completed.outcome,
                "completed",
                completed.receipt,
            )
            self.assertIsNotNone(completed.archive)
            shutil.copyfile(completed.archive, recovered_archive)
            return NativeCaptureResult(
                "unknown",
                None,
                None,
                completed.receipt,
                False,
                "capture.response_lost",
            )

        with (
            patch(
                "description_pipeline.runtime.tool_record",
                return_value=self.current_native_tool,
            ),
            self.assertRaises(CaptureUnresolved),
        ):
            self.handler(native_operation=lose_response).execute(
                self.command,
                self.claim,
                workspace,
                effects=effects,
            )
        self.assertEqual(native_calls, 1)
        settlement = effects.effect_settlement_for_key("capture-native")
        self.assertIsNotNone(settlement)
        self.assertEqual(settlement.state, EffectState.UNKNOWN)
        self.assertIsNotNone(settlement.outbound_artifact)
        self.assertTrue(recovered_archive.is_file())

        outbound_root = self.root / "pending-recovery-outbound"
        self.store.materialize(
            settlement.outbound_artifact,
            outbound_root,
        )
        outbound_bytes = (outbound_root / "outbound.json").read_bytes()
        shutil.rmtree(workspace.root)
        intent = self.effect_ledger.intents[settlement.effect_id]
        client, operation_id = self._observer_client(
            archive=recovered_archive,
            outbound_root=outbound_root,
        )
        observer_root = self.root / "pending-capture-observer"
        observer_root.mkdir()
        evidence = NativeOperationEffectObserver(client).observe(
            command=self.command,
            intent=intent,
            outbound_root=outbound_root,
            workspace_root=observer_root,
        )
        self.assertEqual(evidence.fact_type, "reconciled")
        self.assertEqual(evidence.state_class, "known")
        admitted = observer_root / f"admitted-capture-{operation_id}"
        self.assertTrue(admitted.is_dir())
        self.assertEqual(
            read_data(admitted / "transfer-manifest.json")["run_id"],
            self.command.run_id,
        )
        effects.reconcile_effect(
            settlement.effect_id,
            source="native-observer",
            state_class=evidence.state_class,
            evidence_root=evidence.payload_root,
        )
        self.assertEqual(
            effects.effect_settlement_for_key("capture-native").state,
            EffectState.SATISFIED,
        )

        replay_claim = self._claim(
            self.command,
            "claim-capture-recovery-replay",
            fence=2,
        )
        unexpected = Mock(
            side_effect=AssertionError(
                "recovered Capture must not call the native operation"
            )
        )
        with patch(
            "description_pipeline.runtime.tool_record",
            return_value=self.current_native_tool,
        ):
            replayed = self.handler(
                native_operation=unexpected
            ).execute(
                self.command,
                replay_claim,
                self.workspace(
                    name="capture-pending-replay-workspace"
                ),
                effects=effects,
            )
        self.assertEqual(replayed.outcome, "completed")
        unexpected.assert_not_called()
        self.assertEqual(native_calls, 1)
        self.assertEqual(
            (outbound_root / "outbound.json").read_bytes(),
            outbound_bytes,
        )
        recovered = effects.effect_settlement_for_key("capture-native")
        self.assertEqual(
            recovered.outbound_artifact,
            settlement.outbound_artifact,
        )
        self.store.verify(recovered.outbound_artifact)

    def test_capture_observer_rejects_malformed_or_foreign_producer_archive(
        self,
    ) -> None:
        workspace = self.workspace(name="capture-producer-workspace")
        effects = self.effects()
        with patch(
            "description_pipeline.runtime.tool_record",
            return_value=self.current_native_tool,
        ):
            completed = self.handler().execute(
                self.command,
                self.claim,
                workspace,
                effects=effects,
            )
        self.assertEqual(completed.outcome, "completed")
        producer_archive = (
            workspace.root
            / "capture-native"
            / CAPTURE_ARCHIVE
        )
        outbound_root = workspace.root / "capture-effect-outbound"
        settlement = effects.effect_settlement_for_key("capture-native")
        self.assertIsNotNone(settlement)
        intent = self.effect_ledger.intents[settlement.effect_id]

        missing = self.root / "capture-missing-member.zip"
        self._rewrite_capture_archive(
            producer_archive,
            missing,
            remove={"reports/native-tool.json"},
        )
        duplicate = self.root / "capture-duplicate-member.zip"
        self._rewrite_capture_archive(
            producer_archive,
            duplicate,
            add=(("Reports/Native-Tool.json", b"{}"),),
        )
        with zipfile.ZipFile(producer_archive) as archive:
            foreign_manifest = json.loads(
                archive.read("transfer-manifest.json")
            )
        foreign_manifest["execution"]["owner_sha256"] = "e" * 64
        foreign = self.root / "capture-foreign-execution.zip"
        self._rewrite_capture_archive(
            producer_archive,
            foreign,
            replace_manifest=foreign_manifest,
        )

        cases = (
            (
                "missing",
                missing,
                "provenance is incomplete",
            ),
            (
                "duplicate",
                duplicate,
                "Duplicate transfer member",
            ),
            (
                "foreign",
                foreign,
                "another execution identity",
            ),
        )
        for label, archive, expected in cases:
            with self.subTest(case=label):
                observer_root = (
                    self.root / f"capture-observer-{label}"
                )
                observer_root.mkdir()
                client, _operation_id = self._observer_client(
                    archive=archive,
                    outbound_root=outbound_root,
                )
                with self.assertRaisesRegex(Exception, expected):
                    NativeOperationEffectObserver(client).observe(
                        command=self.command,
                        intent=intent,
                        outbound_root=outbound_root,
                        workspace_root=observer_root,
                    )
                self.assertEqual(
                    list(observer_root.glob("admitted-capture-*")),
                    [],
                )

    def test_producer_identity_is_rejected_before_native_operation(self) -> None:
        operation = Mock()
        discover = self._discover(producer_run_id="a0000000-0000-4000-8000-000000000099")
        command = self._command(discover=discover)
        with self.assertRaisesRegex(Exception, "producer"):
            self.handler(native_operation=operation).execute(
                command,
                self.claim,
                self.workspace(),
                effects=self.effects(command),
            )
        operation.assert_not_called()

    def test_native_tool_mismatch_is_rejected_before_effect_or_native_operation(self) -> None:
        operation = Mock()
        drifted_tool = {**self.capture_tool, "source_sha256": "e" * 64}
        effects = self.effects()
        handler = CaptureHandler(
            artifact_store=self.store,
            backend_factory=lambda package: fixture_backend(package, MAIN),
            native_tool_factory=lambda: drifted_tool,
            native_operation=operation,
        )
        with self.assertRaisesRegex(Exception, "native_tool_mismatch"):
            handler.execute(self.command, self.claim, self.workspace(), effects=effects)
        operation.assert_not_called()
        self.assertFalse(
            any(intent.command_id == self.command.command_id for intent in self.effect_ledger.intents.values())
        )

    def test_diagnostic_capture_keeps_historical_current_and_executor_tools_separate(self) -> None:
        current_tool = self._native_tool_variant("current")
        command, requirement = self._diagnostic_command(current_tool)
        claim = self._claim(command, "claim-capture-diagnostic", fence=1)
        self.assertNotEqual(digest(self.capture_tool), digest(current_tool))
        self.assertNotEqual(digest(current_tool), self.stage_executor)
        self.assertNotEqual(self.stage_executor, self.native_tool_sha256)

        with patch(
            "description_pipeline.runtime.tool_record",
            return_value=current_tool,
        ):
            execution = self.handler(tool=current_tool).execute(
                command,
                claim,
                self._materialized_workspace(
                    self.root / "workspace-capture-diagnostic",
                    (self.freeze_ref, self.discover_ref, requirement),
                ),
                effects=self.effects(command, claim),
            )

        self.assertEqual(execution.outcome, "completed")
        admitted = Path(execution.candidate_root)
        manifest = read_data(admitted / "transfer-manifest.json")
        self.assertEqual(manifest["native_tool"], current_tool)
        self.assertEqual(manifest["native_tool_sha256"], digest(current_tool))
        self.assertEqual(
            manifest["execution"]["native_tool_sha256"],
            digest(current_tool),
        )
        self.assertEqual(
            manifest["execution"]["executor_sha256"],
            self.stage_executor,
        )
        self.assertNotEqual(
            manifest["execution"]["native_tool_sha256"],
            self.native_tool_sha256,
        )

    def test_diagnostic_capture_rejects_current_tool_bound_to_history(self) -> None:
        current_tool = self._native_tool_variant("current")
        command, requirement = self._diagnostic_command(self.capture_tool)
        claim = self._claim(command, "claim-diagnostic-history-binding", fence=1)
        operation = Mock()
        effects = self.effects(command, claim)

        with self.assertRaisesRegex(Exception, "native_tool_mismatch"):
            self.handler(tool=current_tool, native_operation=operation).execute(
                command,
                claim,
                self._materialized_workspace(
                    self.root / "workspace-diagnostic-history-binding",
                    (self.freeze_ref, self.discover_ref, requirement),
                ),
                effects=effects,
            )

        operation.assert_not_called()
        self.assertFalse(
            any(
                intent.command_id == command.command_id
                for intent in self.effect_ledger.intents.values()
            )
        )

    def test_diagnostic_capture_rejects_a_drifted_current_windows_tool(self) -> None:
        current_tool = self._native_tool_variant("current")
        drifted_tool = self._native_tool_variant("drifted")
        command, requirement = self._diagnostic_command(current_tool)
        claim = self._claim(command, "claim-diagnostic-current-tool", fence=1)
        operation = Mock()
        effects = self.effects(command, claim)

        with self.assertRaisesRegex(Exception, "native_tool_mismatch"):
            self.handler(tool=drifted_tool, native_operation=operation).execute(
                command,
                claim,
                self._materialized_workspace(
                    self.root / "workspace-diagnostic-current-tool",
                    (self.freeze_ref, self.discover_ref, requirement),
                ),
                effects=effects,
            )

        operation.assert_not_called()
        self.assertFalse(
            any(
                intent.command_id == command.command_id
                for intent in self.effect_ledger.intents.values()
            )
        )

    def test_diagnostic_capture_rejects_a_different_linux_executor(self) -> None:
        current_tool = self._native_tool_variant("current")
        command, requirement = self._diagnostic_command(current_tool)
        claim = ExecutionClaim(
            claim_id="claim-diagnostic-executor",
            command_id=command.command_id,
            fence=1,
            owner_sha256=digest({"owner": "diagnostic-executor"}),
            executor_sha256=digest({"executor": "different-linux-capture"}),
            lane_key="native",
            expires_at=datetime(2026, 10, 25, tzinfo=UTC),
        )
        operation = Mock()
        effects = self.effects(command, claim)

        with self.assertRaisesRegex(Exception, "claim_mismatch"):
            self.handler(tool=current_tool, native_operation=operation).execute(
                command,
                claim,
                self._materialized_workspace(
                    self.root / "workspace-diagnostic-executor",
                    (self.freeze_ref, self.discover_ref, requirement),
                ),
                effects=effects,
            )

        operation.assert_not_called()
        self.assertFalse(
            any(
                intent.command_id == command.command_id
                for intent in self.effect_ledger.intents.values()
            )
        )

    def test_diagnostic_capture_rejects_wrong_current_requirement_bytes(self) -> None:
        current_tool = self._native_tool_variant("current")
        command, requirement = self._diagnostic_command(
            current_tool,
            requirement_tool=self.capture_tool,
        )
        claim = self._claim(command, "claim-diagnostic-requirement-bytes", fence=1)
        operation = Mock()
        effects = self.effects(command, claim)

        with self.assertRaisesRegex(Exception, "native_requirement_artifact"):
            self.handler(tool=current_tool, native_operation=operation).execute(
                command,
                claim,
                self._materialized_workspace(
                    self.root / "workspace-diagnostic-requirement-bytes",
                    (self.freeze_ref, self.discover_ref, requirement),
                ),
                effects=effects,
            )

        operation.assert_not_called()
        self.assertFalse(
            any(
                intent.command_id == command.command_id
                for intent in self.effect_ledger.intents.values()
            )
        )

    def test_capture_rejects_tampered_historical_discover_bytes(self) -> None:
        current_tool = self._native_tool_variant("current")
        command, requirement = self._diagnostic_command(current_tool)
        claim = self._claim(command, "claim-diagnostic-historical-bytes", fence=1)
        workspace = self._materialized_workspace(
            self.root / "workspace-diagnostic-historical-bytes",
            (self.freeze_ref, self.discover_ref, requirement),
        )
        result_path = workspace.inputs[self.discover_ref.artifact_id] / "result.json"
        result = read_data(result_path)
        result["native_tool"]["runtime"]["execution_variant"] = "tampered"
        result_path.write_text(json.dumps(result), encoding="utf-8")
        operation = Mock()
        effects = self.effects(command, claim)

        with self.assertRaisesRegex(Exception, "artifact_changed|native_history"):
            self.handler(tool=current_tool, native_operation=operation).execute(
                command,
                claim,
                workspace,
                effects=effects,
            )

        operation.assert_not_called()
        self.assertFalse(
            any(
                intent.command_id == command.command_id
                for intent in self.effect_ledger.intents.values()
            )
        )

    def test_formal_capture_does_not_relax_distinct_current_requirement(self) -> None:
        current_tool = self._native_tool_variant("current")
        requirement = self._seal_native_requirement(
            current_tool,
            "formal-native-tool-requirement",
        )
        request = {
            **self.command.to_record()["request"],
            "native_tool_source_sha256": current_tool["source_sha256"],
            "native_tool_record_sha256": digest(current_tool),
            "native_tool_requirement_artifact_id": requirement.artifact_id,
        }
        command = self._command(requirement=requirement, request=request)
        claim = self._claim(command, "claim-formal-distinct-requirement", fence=1)
        operation = Mock()
        effects = self.effects(command, claim)

        with self.assertRaisesRegex(Exception, "native_requirement_binding"):
            self.handler(tool=current_tool, native_operation=operation).execute(
                command,
                claim,
                self._materialized_workspace(
                    self.root / "workspace-formal-distinct-requirement",
                    (self.freeze_ref, self.discover_ref, requirement),
                ),
                effects=effects,
            )

        operation.assert_not_called()
        self.assertFalse(
            any(
                intent.command_id == command.command_id
                for intent in self.effect_ledger.intents.values()
            )
        )

    def test_capture_rejects_an_incomplete_requirement_input_closure(self) -> None:
        current_tool = self._native_tool_variant("current")
        requirement = self._seal_native_requirement(
            current_tool,
            "unreferenced-native-tool-requirement",
        )
        input_artifacts = (self.freeze_ref, self.discover_ref)
        request = {
            **self.command.to_record()["request"],
            "native_tool_source_sha256": current_tool["source_sha256"],
            "native_tool_record_sha256": digest(current_tool),
            "native_tool_requirement_artifact_id": requirement.artifact_id,
        }
        command = StageCommand.create(
            command_id="command-capture-missing-requirement",
            run_id=CHILD,
            stage="capture",
            idempotency_key=f"{CHILD}/missing-requirement",
            request=request,
            input_artifacts=input_artifacts,
            config_snapshot_sha256=digest({"config": "missing-requirement"}),
            contract_sha256=digest({"contract": "missing-requirement"}),
            policy_sha256=digest({"policy": "missing-requirement"}),
            expected_executor_sha256=self.stage_executor,
            predecessor_decision_id=None,
            diagnostic_scope=DiagnosticReplayScope.from_inputs(
                "capture",
                input_artifacts,
            ),
        )
        claim = self._claim(command, "claim-missing-requirement", fence=1)
        operation = Mock()
        effects = self.effects(command, claim)

        with self.assertRaisesRegex(Exception, "native_requirement_artifact"):
            self.handler(tool=current_tool, native_operation=operation).execute(
                command,
                claim,
                self._materialized_workspace(
                    self.root / "workspace-missing-requirement",
                    input_artifacts,
                ),
                effects=effects,
            )

        operation.assert_not_called()
        self.assertFalse(
            any(
                intent.command_id == command.command_id
                for intent in self.effect_ledger.intents.values()
            )
        )

    def test_capture_requires_an_explicit_current_tool_provider(self) -> None:
        operation = Mock()
        handler = CaptureHandler(
            artifact_store=self.store,
            backend_factory=lambda package: fixture_backend(package, MAIN),
            native_operation=operation,
        )
        effects = self.effects()

        with self.assertRaisesRegex(Exception, "native_tool_provider_missing"):
            handler.execute(
                self.command,
                self.claim,
                self.workspace(),
                effects=effects,
            )

        operation.assert_not_called()
        self.assertFalse(
            any(
                intent.command_id == self.command.command_id
                for intent in self.effect_ledger.intents.values()
            )
        )

    def test_formal_capture_requires_a_predecessor_before_native_work(self) -> None:
        command = StageCommand.create(
            command_id="capture-without-predecessor",
            run_id=CHILD,
            stage="capture",
            idempotency_key="capture-without-predecessor",
            request=self.command.to_record()["request"],
            input_artifacts=self.command.input_artifacts,
            config_snapshot_sha256=self.command.config_snapshot_sha256,
            contract_sha256=self.command.contract_sha256,
            policy_sha256=self.command.policy_sha256,
            expected_executor_sha256=self.command.expected_executor_sha256,
            predecessor_decision_id=None,
        )
        claim = self._claim(command, "claim-capture-no-predecessor", fence=1)
        operation = Mock(side_effect=AssertionError("Capture must not run"))
        with self.assertRaisesRegex(Exception, "predecessor_missing"):
            self.handler(native_operation=operation).execute(
                command,
                claim,
                self.workspace(),
                effects=self.effects(command, claim),
            )
        operation.assert_not_called()

    def test_capture_cannot_rebind_the_discover_native_requirement(self) -> None:
        operation = Mock()
        request = {
            **self.command.to_record()["request"],
            "native_tool_record_sha256": "d" * 64,
        }
        command = self._command(request=request)
        claim = self._claim(command, "claim-native-rebind", fence=1)
        effects = self.effects(command, claim)
        with self.assertRaisesRegex(Exception, "native_requirement"):
            self.handler(native_operation=operation).execute(
                command,
                claim,
                self.workspace(name="workspace-native-rebind"),
                effects=effects,
            )
        operation.assert_not_called()
        self.assertFalse(
            any(intent.command_id == command.command_id for intent in self.effect_ledger.intents.values())
        )

    def test_capture_cannot_rebind_the_discover_native_source(self) -> None:
        operation = Mock()
        request = {
            **self.command.to_record()["request"],
            "native_tool_source_sha256": "d" * 64,
        }
        command = self._command(request=request)
        claim = self._claim(command, "claim-native-source-rebind", fence=1)
        effects = self.effects(command, claim)
        with self.assertRaisesRegex(Exception, "native_requirement"):
            self.handler(native_operation=operation).execute(
                command,
                claim,
                self.workspace(name="workspace-native-source-rebind"),
                effects=effects,
            )
        operation.assert_not_called()
        self.assertFalse(
            any(intent.command_id == command.command_id for intent in self.effect_ledger.intents.values())
        )

    def test_executor_mismatch_is_rejected_before_effect_or_native_operation(self) -> None:
        operation = Mock()
        claim = ExecutionClaim(
            claim_id="claim-executor-mismatch",
            command_id=self.command.command_id,
            fence=1,
            owner_sha256=digest({"owner": "capture-executor-mismatch"}),
            executor_sha256="f" * 64,
            lane_key="native",
            expires_at=datetime(2026, 10, 25, tzinfo=UTC),
        )
        effects = self.effects(self.command, claim)
        with self.assertRaisesRegex(Exception, "claim_mismatch"):
            self.handler(native_operation=operation).execute(
                self.command,
                claim,
                self.workspace(),
                effects=effects,
            )
        operation.assert_not_called()
        self.assertFalse(
            any(intent.command_id == self.command.command_id for intent in self.effect_ledger.intents.values())
        )

    def test_unsettled_prior_effect_is_queried_before_native_operation(self) -> None:
        operation = Mock()
        workspace = self.workspace()
        effects = self.effects()
        handler = self.handler(native_operation=operation)
        request = capture_module.CaptureRequest.from_command(
            self.command
        )
        inputs = capture_module.CaptureInputs.resolve(
            self.command,
            workspace,
            request,
        )
        effect_id, _settlement = handler._effect_state(
            self.command,
            self.claim,
            workspace,
            request,
            inputs,
            self.capture_tool,
            effects,
        )
        effects.record_timeout(
            effect_id,
            source="prior-owner",
        )
        with self.assertRaisesRegex(CaptureUnresolved, "effect_unknown"):
            handler.execute(
                self.command,
                self.claim,
                workspace,
                effects=effects,
            )
        operation.assert_not_called()

    def test_plan_must_cover_the_complete_active_object_set_before_native_operation(self) -> None:
        discover_root = self.store.materialize(
            self.discover_ref,
            self.root / "incomplete-discover-artifact",
        )
        (discover_root / "manifest.json").unlink()
        package = discover_root / "prepared"
        plan_path = package / PLAN_FILE
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        plan["objects"].pop()
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        prepared = {
            path.relative_to(package).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in package.rglob("*")
            if path.is_file()
        }
        result_path = discover_root / "result.json"
        result = read_data(result_path)
        result["prepared_sha256"] = digest(prepared)
        result["prepared_files"] = prepared
        result_path.write_text(json.dumps(result), encoding="utf-8")
        incomplete_ref = self.store.seal_tree(
            discover_root,
            kind="stage_output:discover",
            producer_run_id=PARENT,
            producer_stage="discover",
            producer_tool_sha256=self.discover_ref.producer_tool_sha256,
        )
        command = self._command(
            discover=incomplete_ref,
            request={**self.command.to_record()["request"], "prepared_sha256": digest(prepared)}
        )
        workspace = self._materialized_workspace(
            self.root / "incomplete-workspace",
            (
                self.freeze_ref,
                incomplete_ref,
                self.native_tool_requirement_ref,
            ),
        )
        operation = Mock()
        with self.assertRaisesRegex(Exception, "complete|outside"):
            self.handler(native_operation=operation).execute(
                command,
                self.claim,
                workspace,
                effects=self.effects(command),
            )
        operation.assert_not_called()

    def test_unknown_native_effect_never_becomes_a_terminal_stage_result(self) -> None:
        effects = self.effects()
        operation = Mock(
            return_value=NativeCaptureResult(
                "unknown",
                None,
                None,
                {"state": "unknown"},
                False,
                "cad_process_cleanup_failed",
            )
        )
        with self.assertRaises(CaptureUnresolved):
            self.handler(native_operation=operation).execute(
                self.command,
                self.claim,
                self.workspace(),
                effects=effects,
            )
        self.assertEqual(operation.call_count, 1)
        self.assertEqual(effects.authorized[0][0], "capture-native")
        self.assertEqual(effects.observed[0][0], "timeout")
        self.assertEqual(effects.observed[1][0], "unobservable")

    def test_native_failure_without_resource_settlement_stays_unknown(self) -> None:
        workspace = self.workspace()
        failure_root = workspace.root / "native-failed"
        failure_root.mkdir()
        (failure_root / "raw.txt").write_text("native failure bytes\n", encoding="utf-8")
        operation = Mock(
            return_value=NativeCaptureResult(
                "failed",
                failure_root,
                None,
                {"state": "failed"},
                False,
                "cad_process_cleanup_failed",
            )
        )
        effects = self.effects()
        with self.assertRaisesRegex(CaptureUnresolved, "cad_process_cleanup_failed"):
            self.handler(native_operation=operation).execute(
                self.command,
                self.claim,
                workspace,
                effects=effects,
            )
        self.assertEqual(
            [kind for kind, _effect_id in effects.observed],
            ["response", "unobservable"],
        )
        self.assertEqual(
            effects.effect_settlement_for_key("capture-native").state,
            EffectState.UNKNOWN,
        )
        self.assertTrue((failure_root / "raw.txt").is_file())

    def test_native_failure_with_resource_proof_is_terminal_and_reconciled(self) -> None:
        workspace = self.workspace()
        failure_root = workspace.root / "native-definitely-failed"
        failure_root.mkdir()
        (failure_root / "raw.txt").write_text("definitive failure\n", encoding="utf-8")
        operation = Mock(
            return_value=NativeCaptureResult(
                "failed",
                failure_root,
                None,
                {"state": "failed"},
                True,
                "cad_domain_refused",
            )
        )
        effects = self.effects()
        execution = self.handler(native_operation=operation).execute(
            self.command,
            self.claim,
            workspace,
            effects=effects,
        )
        self.assertEqual(execution.outcome, "failed")
        self.assertTrue(execution.resources_settled)
        self.assertEqual(
            [kind for kind, _effect_id in effects.observed],
            ["response", "reconciled:absent"],
        )
        self.assertEqual(
            effects.effect_settlement_for_key("capture-native").state,
            EffectState.DEFINITELY_FAILED,
        )

    def test_admission_rejects_tampered_upstream_identity_without_installing(self) -> None:
        workspace = self.workspace()
        with patch("description_pipeline.runtime.tool_record", return_value=self.capture_tool):
            execution = self.handler().execute(
                self.command,
                self.claim,
                workspace,
                effects=self.effects(),
            )
        self.assertEqual(execution.outcome, "completed")
        archive = workspace.root / "capture-native" / "native-evidence.zip"
        changed = {
            **self.discover_ref.to_record(),
            "producer_run_id": "a0000000-0000-4000-8000-000000000099",
        }
        expected_upstream = (
            self.freeze_ref.to_record(),
            changed,
        )
        destination = self.root / "rejected-admission"
        execution_record = {
            "claim_id": self.claim.claim_id,
            "command_id": self.command.command_id,
            "effect_key": "capture-native",
            "executor_sha256": self.stage_executor,
            "fence": self.claim.fence,
            "native_tool_sha256": self.native_tool_sha256,
            "owner_sha256": self.claim.owner_sha256,
            "run_id": self.command.run_id,
        }
        with self.assertRaisesRegex(Exception, "upstream"):
            admit_capture(
                archive,
                destination,
                expected_run_id=self.command.run_id,
                expected_handoff_sha256=self.handoff_sha256,
                expected_upload_sha256=self.upload_sha256,
                expected_main_assembly=MAIN,
                expected_native_tool=self.capture_tool,
                expected_prepared_dir=str(self.package),
                expected_prepared_files=self.prepared_files,
                expected_upstream_artifacts=expected_upstream,
                expected_execution=execution_record,
            )
        self.assertFalse(destination.exists())

    def test_admission_rejects_native_execution_identity_drift(self) -> None:
        workspace = self.workspace()
        with patch("description_pipeline.runtime.tool_record", return_value=self.capture_tool):
            execution = self.handler().execute(
                self.command,
                self.claim,
                workspace,
                effects=self.effects(),
            )
        self.assertEqual(execution.outcome, "completed")
        archive = workspace.root / "capture-native" / "native-evidence.zip"
        execution_record = {
            "claim_id": self.claim.claim_id,
            "command_id": self.command.command_id,
            "effect_key": "capture-native",
            "executor_sha256": self.stage_executor,
            "fence": self.claim.fence,
            "native_tool_sha256": "e" * 64,
            "owner_sha256": self.claim.owner_sha256,
            "run_id": self.command.run_id,
        }
        destination = self.root / "rejected-native-execution"
        with self.assertRaisesRegex(Exception, "execution identity"):
            admit_capture(
                archive,
                destination,
                expected_run_id=self.command.run_id,
                expected_handoff_sha256=self.handoff_sha256,
                expected_upload_sha256=self.upload_sha256,
                expected_main_assembly=MAIN,
                expected_native_tool=self.capture_tool,
                expected_prepared_dir=None,
                expected_prepared_files=self.prepared_files,
                expected_upstream_artifacts=(self.freeze_ref.to_record(), self.discover_ref.to_record()),
                expected_execution=execution_record,
            )
        self.assertFalse(destination.exists())


class CaptureTransactionPreservationTests(unittest.TestCase):
    def test_failure_evidence_failure_keeps_the_untouched_native_staging_root(self) -> None:
        with tempfile.TemporaryDirectory(prefix="capture-preservation-") as temporary:
            root = Path(temporary)
            package = root / "package"
            package.mkdir()
            output = root / "output"

            def fail_evidence(_package, staging, **_kwargs):
                staging.mkdir(parents=True, exist_ok=True)
                (staging / "raw-native.bin").write_bytes(b"original native bytes")
                raise RuntimeError("native reader failed")

            with (
                patch(
                    "description_pipeline.pipeline.capture.capture_native_package",
                    side_effect=fail_evidence,
                ),
                patch(
                    "description_pipeline.pipeline.capture._keep_capture_diagnostic",
                    side_effect=OSError("diagnostic install failed"),
                ),
                self.assertRaisesRegex(OSError, "diagnostic install failed"),
            ):
                capture_module._capture_native_transaction(
                    package,
                    output,
                    run_id=CHILD,
                    handoff_sha256=digest({"handoff": 1}),
                    main_assembly=MAIN,
                )

            retained = list(root.glob(".output-capture-*"))
            self.assertEqual(len(retained), 1)
            self.assertEqual(
                (retained[0] / "raw-native.bin").read_bytes(),
                b"original native bytes",
            )


if __name__ == "__main__":
    unittest.main()
