"""Capture domain handler: producer/executor split and native admission boundaries."""

from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock, patch

from description_pipeline.artifacts import ArtifactStore
from description_pipeline.io import canonical, digest, file_digest, read_data
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
    StageCommand,
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
    EffectSettlement,
    EffectState,
    project_effect_settlement,
)
from description_pipeline.pipeline.freeze import FREEZE_REQUEST_SCHEMA
from description_pipeline.pipeline.discover import DiscoverHandler
from description_pipeline.pipeline.engine import StageWorkspace
from description_pipeline.pipeline.freeze import FreezeHandler
from description_pipeline.orchestration.stage_transfer import admit_capture
from description_pipeline.sources.solidworks.discovery import PLAN_FILE
from description_pipeline.schemas import NATIVE_TOOL_REQUIREMENT_KIND

from .split_capture_fixture import fixture_backend
from .test_pipeline_discover import (
    ClosureReader,
    CountingBackend,
    DiscoverHarness,
)
from .test_split_roundtrip import capture_record
from .test_stage_transfer import native_tool as build_capture_tool


PARENT = "a0000000-0000-4000-8000-000000000001"
CHILD = "a0000000-0000-4000-8000-000000000002"
MAIN = "cad/robot.SLDASM"


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
        return intent

    def _append(self, effect_id, fact_type, *, source, payload=None, state_class=None):
        facts = self.ledger.facts[effect_id]
        claim_bound = fact_type in {"dispatch_authorized", "response", "timeout"}
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

    def record_observation(self, effect_id, *, source, state_class, observation_root):
        payload = self._payload(observation_root, "effect_observation")
        self.observed.append((state_class, effect_id))
        return self._append(
            effect_id,
            "observation",
            source=source,
            payload=payload,
            state_class=state_class,
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
            "platform_config_sha256": self.configuration_ref.set_sha256,
        }
        freeze_command = StageCommand.create(
            command_id="freeze-command",
            run_id=PARENT,
            stage="freeze",
            idempotency_key=f"{PARENT}/freeze",
            request=freeze_request,
            input_artifacts=(self.source_ref, self.ruleset_ref, self.configuration_ref),
            config_snapshot_sha256=self.configuration_ref.set_sha256,
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
            platform_config_sha256=self.configuration_ref.set_sha256,
            expected_native_tool_sha256=self.expected_native_tool_sha256,
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
            config_snapshot_sha256=self.configuration_ref.set_sha256,
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
        outbound = workspace.root / "prior-effect"
        outbound.mkdir()
        (outbound / "request.txt").write_text("capture\n", encoding="utf-8")
        intent = effects.prepare_effect(
            effect_key="capture-native",
            effect_kind="native_operation",
            target={"stage": "capture"},
            expected_state={"stage": "capture"},
            outbound_root=outbound,
        )
        effects.authorize_dispatch(intent.effect_id, source="prior-owner")
        with self.assertRaisesRegex(CaptureUnresolved, "effect_unknown"):
            self.handler(native_operation=operation).execute(
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
