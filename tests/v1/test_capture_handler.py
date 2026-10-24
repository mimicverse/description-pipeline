"""Capture domain handler: producer/executor split and native admission boundaries."""

from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock, patch

from description_pipeline.artifacts import ArtifactStore
from description_pipeline.io import digest, file_digest, read_data
from description_pipeline.pipeline import (
    ArtifactRef,
    CaptureHandler,
    DiscoverHandler,
    ExecutionClaim,
    FreezeHandler,
    StageCommand,
    StageWorkspace,
)
from description_pipeline.pipeline.capture import CaptureUnresolved, NativeCaptureResult
from description_pipeline.pipeline.discover import (
    CONFIGURATION_KIND,
    FREEZE_RESULT_KIND,
    RULESET_KIND,
    SOURCE_SET_KIND,
)
from description_pipeline.pipeline.freeze import FREEZE_REQUEST_SCHEMA
from description_pipeline.orchestration.stage_transfer import admit_capture
from description_pipeline.sources.solidworks.discovery import PLAN_FILE

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


class _Effects:
    def __init__(self) -> None:
        self.authorized = []
        self.observed = []

    def authorize(self, **request):
        self.authorized.append(request)
        return request["effect_key"]

    def observe(self, **observation):
        self.observed.append(observation)


class CaptureHandlerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="capture-handler-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = ArtifactStore(self.root / "artifact-store")
        self.stage_fixture = DiscoverHarness(self.root / "discover-source")
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
        freeze_request = {
            "schema_version": FREEZE_REQUEST_SCHEMA,
            "request_id": "request-capture-boundary",
            "principal": "fixture:principal",
            "target_hardware": "nd_fixture",
            "purpose": "engineering",
            "change_summary": "Prove adjacent Freeze Discover Capture contracts",
            "main_assembly": MAIN,
            "ruleset_version": "rules-2026-10-25",
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
        freeze_execution = FreezeHandler().execute(freeze_command, freeze_claim, freeze_workspace)
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
                reader=ClosureReader(transform=_capture_component_closure),
                backend=CountingBackend(copy.deepcopy(self.stage_fixture.record))
            )
        ).execute(discover_command, discover_claim, discover_workspace)
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
        self.capture_tool = build_capture_tool()
        self.command = self._command()
        self.claim = ExecutionClaim(
            claim_id="claim-capture",
            command_id=self.command.command_id,
            fence=1,
            owner_sha256=self.command.expected_executor_sha256,
            lane_key="native",
            expires_at=datetime(2026, 10, 25, tzinfo=UTC),
        )

    @staticmethod
    def _claim(command: StageCommand, claim_id: str, *, fence: int) -> ExecutionClaim:
        return ExecutionClaim(
            claim_id=claim_id,
            command_id=command.command_id,
            fence=fence,
            owner_sha256=command.expected_executor_sha256,
            lane_key="native",
            expires_at=datetime(2026, 10, 25, tzinfo=UTC),
        )

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
        request: dict | None = None,
    ) -> StageCommand:
        return StageCommand.create(
            command_id="command-capture",
            run_id=CHILD,
            stage="capture",
            idempotency_key=f"{CHILD}/capture",
            request=request
            or {
                "effect_id": "effect-capture",
                "handoff_sha256": self.handoff_sha256,
                "main_assembly": MAIN,
                "native_tool_sha256": digest(self.capture_tool),
                "prepared_sha256": digest(self.prepared_files),
                "upload_sha256": self.upload_sha256,
            },
            input_artifacts=(self.freeze_ref, discover or self.discover_ref),
            config_snapshot_sha256=digest({"config": "capture"}),
            contract_sha256=digest({"contract": 1}),
            policy_sha256=digest({"policy": "capture"}),
            expected_executor_sha256=digest({"executor": "native"}),
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

    def workspace(self, *, discover_run_id: str = PARENT) -> StageWorkspace:
        discover = self._discover(producer_run_id=discover_run_id)
        root = self.root / f"workspace-{discover_run_id}"
        return self._materialized_workspace(root, (self.freeze_ref, discover))

    def handler(
        self,
        *,
        native_operation=None,
        tool=None,
        effects: _Effects | None = None,
    ) -> CaptureHandler:
        effects = effects or _Effects()
        return CaptureHandler(
            backend_factory=lambda package: fixture_backend(package, MAIN),
            native_tool_factory=lambda: tool or self.capture_tool,
            native_operation=native_operation,
            effect_authorizer=effects.authorize,
            effect_observer=effects.observe,
        )

    def test_real_freeze_discover_artifact_store_then_capture_keeps_identities_separate(self) -> None:
        discovery_before = (self.package / "discovery/native-discovery.json").read_bytes()
        effects = _Effects()
        with patch("description_pipeline.runtime.tool_record", return_value=self.capture_tool):
            execution = self.handler(effects=effects).execute(self.command, self.claim, self.workspace())

        if execution.outcome != "completed":
            failure = Path(execution.candidate_root) / "failure.json"
            self.fail(read_data(failure) if failure.is_file() else execution.reason_code)
        self.assertEqual(execution.outcome, "completed")
        admitted = Path(execution.candidate_root)
        manifest = json.loads((admitted / "transfer-manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["schema_version"], "solidworks-to-urdf.capture-transfer/v2")
        self.assertEqual(manifest["run_id"], CHILD)
        self.assertEqual(manifest["native_stage_scope"], ["capture"])
        self.assertEqual(manifest["execution"]["effect_id"], "effect-capture")
        self.assertNotEqual(
            manifest["execution"]["owner_sha256"],
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
        authorization = effects.authorized[0]
        self.assertEqual(authorization["effect_key"], "effect-capture")
        self.assertEqual(authorization["outbound_digest"], digest(authorization["outbound"]))
        self.assertEqual(
            authorization["expected_state_digest"],
            digest(authorization["expected_state"]),
        )
        self.assertEqual(effects.observed[0]["state_class"], "known")

    def test_producer_identity_is_rejected_before_native_operation(self) -> None:
        operation = Mock()
        discover = self._discover(producer_run_id="a0000000-0000-4000-8000-000000000099")
        command = self._command(discover=discover)
        with self.assertRaisesRegex(Exception, "producer"):
            self.handler(native_operation=operation).execute(
                command,
                self.claim,
                self.workspace(),
            )
        operation.assert_not_called()

    def test_missing_effect_gateway_fails_before_native_operation(self) -> None:
        operation = Mock()
        handler = CaptureHandler(
            backend_factory=lambda package: fixture_backend(package, MAIN),
            native_tool_factory=lambda: self.capture_tool,
            native_operation=operation,
        )
        with self.assertRaisesRegex(Exception, "effect_gateway_required"):
            handler.execute(self.command, self.claim, self.workspace())
        operation.assert_not_called()

    def test_native_tool_mismatch_is_rejected_before_effect_or_native_operation(self) -> None:
        operation = Mock()
        authorizer = Mock()
        command = self._command(
            request={
                **self.command.to_record()["request"],
                "native_tool_sha256": "f" * 64,
            }
        )
        handler = CaptureHandler(
            backend_factory=lambda package: fixture_backend(package, MAIN),
            native_tool_factory=lambda: self.capture_tool,
            native_operation=operation,
            effect_authorizer=authorizer,
            effect_observer=Mock(),
        )
        with self.assertRaisesRegex(Exception, "native_tool_mismatch"):
            handler.execute(command, self.claim, self.workspace())
        operation.assert_not_called()
        authorizer.assert_not_called()

    def test_unsettled_prior_effect_is_queried_before_native_operation(self) -> None:
        operation = Mock()
        observer = Mock()

        def unresolved(**request):
            raise CaptureUnresolved("effect.unknown")

        handler = CaptureHandler(
            backend_factory=lambda package: fixture_backend(package, MAIN),
            native_tool_factory=lambda: self.capture_tool,
            native_operation=operation,
            effect_authorizer=unresolved,
            effect_observer=observer,
        )
        with self.assertRaisesRegex(CaptureUnresolved, "effect.unknown"):
            handler.execute(self.command, self.claim, self.workspace())
        operation.assert_not_called()
        observer.assert_not_called()

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
            (self.freeze_ref, incomplete_ref),
        )
        operation = Mock()
        with self.assertRaisesRegex(Exception, "complete|outside"):
            self.handler(native_operation=operation).execute(
                command,
                self.claim,
                workspace,
            )
        operation.assert_not_called()

    def test_unknown_native_effect_never_becomes_a_terminal_stage_result(self) -> None:
        effects = _Effects()
        operation = Mock(
            return_value=NativeCaptureResult(
                "unknown",
                None,
                None,
                {"state": "unknown"},
                "cad_process_cleanup_failed",
            )
        )
        with self.assertRaises(CaptureUnresolved):
            self.handler(native_operation=operation, effects=effects).execute(
                self.command,
                self.claim,
                self.workspace(),
            )
        self.assertEqual(operation.call_count, 1)
        self.assertEqual(effects.authorized[0]["effect_key"], "effect-capture")
        self.assertEqual(effects.observed[0]["state_class"], "unobservable")

    def test_admission_rejects_tampered_upstream_identity_without_installing(self) -> None:
        workspace = self.workspace()
        with patch("description_pipeline.runtime.tool_record", return_value=self.capture_tool):
            execution = self.handler().execute(self.command, self.claim, workspace)
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
            "effect_id": "effect-capture",
            "fence": self.claim.fence,
            "native_tool_sha256": digest(self.capture_tool),
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


if __name__ == "__main__":
    unittest.main()
