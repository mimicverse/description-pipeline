"""Canonical Discover handler and one-transfer Windows protocol regressions."""

from __future__ import annotations

import copy
import json
import shutil
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from description_pipeline.io import digest, file_digest, read_data, write_json
from description_pipeline.orchestration.discover_protocol import (
    LocalArchiveDiscoverPort,
    admit_observation_bundle,
    seal_observation_bundle,
)
from description_pipeline.pipeline import (
    ArtifactRef,
    ExecutionClaim,
    StageCandidate,
    StageCommand,
    StageDecision,
    StageResult,
    StageWorkspace,
    execute_stage,
)
from description_pipeline.pipeline.contracts import ProtocolError
from description_pipeline.pipeline.discover import (
    CONFIGURATION_KIND,
    DISCOVER_REQUEST_SCHEMA,
    FREEZE_RESULT_KIND,
    RULESET_CATALOG_SCHEMA,
    RULESET_KIND,
    SOURCE_SET_KIND,
    DiscoverHandler,
)
from description_pipeline.pipeline.effects import EffectState
from description_pipeline.sources.solidworks.discover_stage import (
    NativeDiscoverRequest,
    admit_native_observation,
)
from tests.sources.selection_support import closure_evidence
from tests.sources.test_solidworks_discovery import FakeBackend, record as native_record

MAIN = "cad/robot.SLDASM"
SELECTED_EDGES = {
    MAIN: ["cad/base.SLDPRT", "cad/arm.SLDPRT", "spare/base.SLDPRT"],
    "cad/base.SLDPRT": [],
    "cad/arm.SLDPRT": [],
    "spare/base.SLDPRT": [],
}
TOOL_SHA256 = "ab" * 32
NATIVE_TOOL = {
    "schema_version": "solidworks-to-urdf.tool/v1",
    "pipeline_id": "fixture",
    "source_sha256": TOOL_SHA256,
    "runtime": {"role": "native"},
}


def _members(root: Path) -> list[dict]:
    return [
        {
            "bytes": path.stat().st_size,
            "path": path.relative_to(root).as_posix(),
            "sha256": file_digest(path),
        }
        for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix())
        if path.is_file()
    ]


def _artifact(kind: str, root: Path, *, run_id: str | None = None, stage: str | None = None) -> ArtifactRef:
    members = _members(root)
    set_sha256 = digest({"members": members})
    artifact_id = digest({"kind": kind, "set_sha256": set_sha256})
    return ArtifactRef(
        artifact_id=artifact_id,
        kind=kind,
        locator=f"cas://artifact/{artifact_id}",
        manifest_sha256=digest({"manifest": artifact_id}),
        set_sha256=set_sha256,
        producer_run_id=run_id,
        producer_stage=stage,
        producer_tool_sha256=digest({"producer": kind}),
    )


class ClosureReader:
    def __init__(self, *, transform=None, refusal=None) -> None:
        self.transform = transform
        self.refusal = refusal
        self.calls = 0

    def observe(self, stage, main_assembly, upload_files):
        self.calls += 1
        if self.refusal is not None:
            raise self.refusal
        evidence = closure_evidence(
            upload_files,
            main_assembly,
            edges=SELECTED_EDGES,
            reader_sha256=TOOL_SHA256,
            stage_root=str(stage),
        )
        if self.transform is not None:
            evidence = self.transform(evidence)
        return evidence


class CountingBackend(FakeBackend):
    def __init__(self, payload: dict) -> None:
        super().__init__(payload)
        self.calls = 0

    def discover_native(self, frozen_source, settings, *, on_activity=None):
        self.calls += 1
        return super().discover_native(frozen_source, settings, on_activity=on_activity)


class DiscoverHarness:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.source = root / "artifacts/source"
        self.rules = root / "artifacts/rules"
        self.configuration = root / "artifacts/configuration"
        self.freeze = root / "artifacts/freeze"
        for directory in (self.source, self.rules, self.configuration, self.freeze):
            directory.mkdir(parents=True)
        self.source_bytes = {
            MAIN: b"placeholder cad/robot.SLDASM\n",
            "cad/base.SLDPRT": b"placeholder cad/base.SLDPRT\n",
            "cad/arm.SLDPRT": b"placeholder cad/arm.SLDPRT\n",
            "spare/base.SLDPRT": b"selected same-basename part\n",
            "unused/base.SLDPRT": b"excluded same-basename part\n",
        }
        for name, payload in self.source_bytes.items():
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
        (self.rules / "catalog.json").write_text(
            json.dumps(
                {
                    "schema_version": RULESET_CATALOG_SCHEMA,
                    "version": "rules-2026-10-25",
                    "records": {
                        "budget": "budget.json",
                        "joint": "joints/arm.json",
                    },
                }
            ),
            encoding="utf-8",
        )
        (self.rules / "joints").mkdir()
        (self.rules / "budget.json").write_text(
            json.dumps(
                {
                    "robot": {
                        "expected_mass_kg": [0.28, 0.32],
                        "expected_extent_m": [0.35, 0.5],
                    }
                }
            ),
            encoding="utf-8",
        )
        (self.rules / "joints/arm.json").write_text(
            json.dumps(
                {
                    "limits": {"lower": -1.5, "upper": 1.5},
                    "drive": {"effort": 6.0, "velocity": 2.0},
                }
            ),
            encoding="utf-8",
        )
        write_json(self.configuration / "config.json", {"snapshot": "configuration-1"})
        write_json(self.freeze / "result.json", {"stage": "freeze", "passed": True})
        self.source_files = {name: file_digest(self.source / name) for name in self.source_bytes}
        self.source_ref = _artifact(SOURCE_SET_KIND, self.source)
        self.rules_ref = _artifact(RULESET_KIND, self.rules)
        self.configuration_ref = _artifact(CONFIGURATION_KIND, self.configuration)
        self.freeze_ref = _artifact(FREEZE_RESULT_KIND, self.freeze, run_id="run-discover", stage="freeze")
        self.record = native_record()
        self.record["files"] = {
            name: self.source_files[name] for name in (MAIN, "cad/base.SLDPRT", "cad/arm.SLDPRT", "spare/base.SLDPRT")
        }

    def request(self, **overrides):
        record = {
            "schema_version": DISCOVER_REQUEST_SCHEMA,
            "freeze_artifact_id": self.freeze_ref.artifact_id,
            "source_set_artifact_id": self.source_ref.artifact_id,
            "ruleset_artifact_id": self.rules_ref.artifact_id,
            "configuration_artifact_id": self.configuration_ref.artifact_id,
            "source_files": dict(self.source_files),
            "source_inventory_sha256": digest(self.source_files),
            "main_assembly": MAIN,
            "purpose": "engineering",
            "ruleset_version": "rules-2026-10-25",
            "ruleset_catalog_sha256": self.rules_ref.set_sha256,
            "platform_config_sha256": self.configuration_ref.set_sha256,
            "submission_identity": {
                "hardware_id": None,
                "owner": None,
                "change_summary": None,
                "parent_revision": None,
                "control_reference": None,
                "as_of": None,
                "inventory_digest": None,
            },
            "expected_native_tool_sha256": TOOL_SHA256,
        }
        record.update(overrides)
        return record

    def command(self, *, request=None, command_id="discover-command-1", key=None):
        return StageCommand.create(
            command_id=command_id,
            run_id="run-discover",
            stage="discover",
            idempotency_key=key or "run-discover/discover",
            request=self.request() if request is None else request,
            input_artifacts=(
                self.freeze_ref,
                self.source_ref,
                self.rules_ref,
                self.configuration_ref,
            ),
            config_snapshot_sha256=self.configuration_ref.set_sha256,
            contract_sha256=digest({"contract": "discover"}),
            policy_sha256=digest({"policy": "discover"}),
            expected_executor_sha256=digest({"executor": "discover"}),
            predecessor_decision_id="freeze-decision",
        )

    def claim(self, command, *, fence=1):
        return ExecutionClaim(
            claim_id=f"claim-{fence}",
            command_id=command.command_id,
            fence=fence,
            owner_sha256=command.expected_executor_sha256,
            lane_key="native",
            expires_at=datetime(2026, 10, 25, tzinfo=UTC) + timedelta(minutes=5),
        )

    def native_request(self, command, *, request=None) -> NativeDiscoverRequest:
        value = self.request() if request is None else request
        return NativeDiscoverRequest(
            schema_version="description-pipeline.native-discover-request/v1",
            operation_id=command.command_id,
            command_id=command.command_id,
            run_id=command.run_id,
            request_digest=command.request_digest,
            source_files=value["source_files"],
            source_inventory_sha256=value["source_inventory_sha256"],
            main_assembly=value["main_assembly"],
            purpose=value["purpose"],
            expected_native_tool_sha256=value["expected_native_tool_sha256"],
        )

    def workspace(self):
        return StageWorkspace(
            root=self.root,
            inputs={
                self.freeze_ref.artifact_id: self.freeze,
                self.source_ref.artifact_id: self.source,
                self.rules_ref.artifact_id: self.rules,
                self.configuration_ref.artifact_id: self.configuration,
            },
        )

    def port(self, *, reader=None, backend=None) -> LocalArchiveDiscoverPort:
        backend = backend or CountingBackend(copy.deepcopy(self.record))
        return LocalArchiveDiscoverPort(
            native_tool=NATIVE_TOOL,
            selection_reader_factory=lambda: reader or ClosureReader(),
            backend_factory=lambda _root, _files: backend,
        )


class DiscoverDomainTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="discover-handler-")
        self.addCleanup(self.temporary.cleanup)
        self.harness = DiscoverHarness(Path(self.temporary.name))
        self.command = self.harness.command()

    def test_complete_sourceset_transfers_once_and_ends_at_discover_result(self) -> None:
        backend = CountingBackend(copy.deepcopy(self.harness.record))
        port = self.harness.port(backend=backend)
        handler = DiscoverHandler(port)
        execution = handler.execute(self.command, self.harness.claim(self.command), self.harness.workspace())

        self.assertEqual(execution.outcome, "completed")
        self.assertEqual(execution.required_effect_ids, ("native-discover:discover-command-1",))
        self.assertEqual(len(port.transfers), 1)
        self.assertEqual(backend.calls, 1)
        root = Path(execution.candidate_root)
        self.assertTrue((root / "prepared/discovery/native-discovery.json").is_file())
        self.assertTrue((root / "native-observation/observation.json").is_file())
        result = read_data(root / "result.json")
        self.assertTrue(result["passed"])
        self.assertIn("spare/base.SLDPRT", result["selection"]["files"])
        self.assertNotIn("unused/base.SLDPRT", result["selection"]["files"])

    def test_sealed_observation_round_trips_as_one_return_bundle(self) -> None:
        execution = DiscoverHandler(self.harness.port()).execute(
            self.command,
            self.harness.claim(self.command),
            self.harness.workspace(),
        )
        observation_root = Path(execution.candidate_root) / "native-observation"
        request = self.harness.native_request(self.command)
        observation = admit_native_observation(observation_root, request=request)
        bundle = seal_observation_bundle(observation, self.harness.root / "return/native-observation.zip")
        admitted = admit_observation_bundle(
            bundle,
            self.harness.root / "return/admitted",
            request=request,
        )
        self.assertEqual(admitted.manifest, observation.manifest)
        self.assertEqual(admitted.record, observation.record)

    def test_package_outside_reference_is_refused_before_native_record_read(self) -> None:
        def outside(evidence):
            evidence["documents"][MAIN]["resolved_edges"][0]["resolved_path"] = "C:\\outside\\cad\\base.SLDPRT"
            return evidence

        reader = ClosureReader(transform=outside)
        backend = CountingBackend(copy.deepcopy(self.harness.record))
        execution = DiscoverHandler(self.harness.port(reader=reader, backend=backend)).execute(
            self.command,
            self.harness.claim(self.command),
            self.harness.workspace(),
        )
        self.assertEqual(execution.outcome, "failed")
        self.assertIn(execution.reason_code, {"selection_closure_invalid", "discover.native_refused"})
        self.assertEqual(reader.calls, 1)
        self.assertEqual(backend.calls, 0)

    def test_embedded_virtual_component_evidence_survives_the_single_transfer(self) -> None:
        def add_virtual(evidence):
            session_root = evidence["stage_root"].rstrip("/\\") + "/swx21040"
            slot = {
                "document": MAIN,
                "owner": MAIN,
                "owner_anchor": None,
                "owner_sha256": self.harness.source_files[MAIN],
                "instance": "waist-1/Copy of virtual part^waist-1",
                "state": "embedded_virtual_component",
                "binding": "owner_embedded_geometry",
                "kind": "native_embedded_virtual_component",
                "session_document_path": session_root + "/VC~~/waist/Copy of virtual part.SLDPRT",
                "session_temp_root": session_root,
                "native_api_pid": 21040,
                "configuration": "Default",
                "suppression_state": 2,
                "is_virtual": True,
                "used_api": "IComponent2.GetPathName/IComponent2.GetImportedPath/IComponent2.IsVirtual",
                "author_imported_path": None,
                "author_imported_error": None,
            }
            evidence["virtual_components"].append(slot)
            return evidence

        execution = DiscoverHandler(self.harness.port(reader=ClosureReader(transform=add_virtual))).execute(
            self.command, self.harness.claim(self.command), self.harness.workspace()
        )
        self.assertEqual(execution.outcome, "completed")
        result = read_data(Path(execution.candidate_root) / "result.json")
        self.assertEqual(
            result["selection"]["closure"]["virtual_components"][0]["state"],
            "embedded_virtual_component",
        )

    def test_saved_and_declared_configuration_mismatch_is_refused(self) -> None:
        payload = copy.deepcopy(self.harness.record)
        payload["saved_configuration"] = "Other"
        execution = DiscoverHandler(self.harness.port(backend=CountingBackend(payload))).execute(
            self.command, self.harness.claim(self.command), self.harness.workspace()
        )
        self.assertEqual(execution.outcome, "failed")
        self.assertEqual(execution.reason_code, "discover.derivation_invalid")

    def test_no_capture_or_downstream_stage_is_invoked(self) -> None:
        with patch(
            "description_pipeline.solidworks.capture",
            side_effect=AssertionError("Capture must not run from Discover"),
        ) as capture:
            execution = DiscoverHandler(self.harness.port()).execute(
                self.command,
                self.harness.claim(self.command),
                self.harness.workspace(),
            )
        self.assertEqual(execution.outcome, "completed")
        capture.assert_not_called()


class _BlockedEffectPort:
    def __init__(self) -> None:
        self.authorization_checks = 0
        self.native_calls = 0

    def discover(self, request, source_root, workspace):
        self.authorization_checks += 1
        raise RuntimeError("effect is unknown; reconcile before resend")


class _Services:
    def __init__(self, root: Path, harness: DiscoverHarness, port: LocalArchiveDiscoverPort) -> None:
        self.root = root
        self.harness = harness
        self.port = port
        self.commands: dict[str, StageCommand] = {}
        self.keys: dict[str, str] = {}
        self.candidate = None
        self.decision = None
        self.fail_commit_once = False
        self.materializations = 0
        self.effects = {"native-discover:discover-command-1": EffectState.SATISFIED}

    def add(self, command):
        prior = self.keys.get(command.idempotency_key)
        if prior is not None and prior != command.request_digest:
            raise ProtocolError("command.idempotency_conflict", {"key": command.idempotency_key})
        self.keys[command.idempotency_key] = command.request_digest
        self.commands[command.command_id] = command

    def load_command(self, command_id):
        return self.commands[command_id]

    def decision_for(self, command):
        return self.decision

    def load_result(self, decision):
        return self.candidate.result

    def verify_decision(self, decision, result):
        self.decision = decision

    def acquire_claim(self, command):
        return self.harness.claim(command, fence=self.materializations + 1)

    def candidate_for(self, command, claim):
        return self.candidate

    def materialize(self, command, claim, *, candidate):
        self.materializations += 1
        root = self.root / f"engine-workspace-{claim.fence}"
        root.mkdir()
        inputs = {}
        if candidate is None:
            sources = {
                self.harness.freeze_ref.artifact_id: self.harness.freeze,
                self.harness.source_ref.artifact_id: self.harness.source,
                self.harness.rules_ref.artifact_id: self.harness.rules,
                self.harness.configuration_ref.artifact_id: self.harness.configuration,
            }
            for artifact in command.input_artifacts:
                target = root / "inputs" / artifact.artifact_id
                shutil.copytree(sources[artifact.artifact_id], target)
                inputs[artifact.artifact_id] = target
        return StageWorkspace(root=root, inputs=inputs)

    def seal_candidate(self, command, claim, execution):
        members = _members(Path(execution.candidate_root))
        artifact_id = digest({"result": execution.checks_sha256, "members": members})
        artifact = ArtifactRef(
            artifact_id=artifact_id,
            kind="stage_output:discover",
            locator=f"cas://artifact/{artifact_id}",
            manifest_sha256=digest({"manifest": artifact_id}),
            set_sha256=digest({"members": members}),
            producer_run_id=command.run_id,
            producer_stage="discover",
            producer_tool_sha256=command.expected_executor_sha256,
        )
        result = StageResult(
            schema_version="description-pipeline.stage-result/v1",
            command_id=command.command_id,
            run_id=command.run_id,
            stage="discover",
            request_digest=command.request_digest,
            outcome=execution.outcome,
            result_artifact=artifact,
            actual_executor_sha256=command.expected_executor_sha256,
            checks_sha256=execution.checks_sha256,
            required_effect_ids=execution.required_effect_ids,
            reason_code=execution.reason_code,
        )
        self.candidate = StageCandidate(result=result)
        return self.candidate

    def verify_candidate(self, command, claim, candidate):
        return candidate

    def effect_state(self, effect_id):
        return self.effects[effect_id]

    def commit_decision(self, command, claim, candidate):
        if self.fail_commit_once:
            self.fail_commit_once = False
            raise RuntimeError("crash after Discover seal and before DB confirmation")
        self.decision = StageDecision(
            decision_id="discover-decision",
            run_id=command.run_id,
            stage="discover",
            command_id=command.command_id,
            result_artifact=candidate.result.result_artifact,
            result_digest=candidate.result.digest,
            actual_executor_sha256=candidate.result.actual_executor_sha256,
            outcome=candidate.result.outcome,
            original_producer_decision_id=None,
            reason_code=candidate.result.reason_code,
            decided_at=datetime(2026, 10, 25, tzinfo=UTC),
        )
        return self.decision


class DiscoverEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="discover-engine-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.harness = DiscoverHarness(self.root)
        self.port = self.harness.port()
        self.handler = DiscoverHandler(self.port)
        self.command = self.harness.command()
        self.services = _Services(self.root, self.harness, self.port)
        self.services.add(self.command)

    def test_same_key_and_request_reuses_one_decision_and_one_transfer(self) -> None:
        first = execute_stage(command_id=self.command.command_id, handler=self.handler, services=self.services)
        second = execute_stage(command_id=self.command.command_id, handler=self.handler, services=self.services)
        self.assertEqual(first, second)
        self.assertEqual(len(self.port.transfers), 1)

    def test_same_key_with_changed_rules_is_conflict(self) -> None:
        alternate = self.harness.command(
            command_id="discover-command-2",
            request=self.harness.request(ruleset_catalog_sha256=digest({"changed": 1})),
            key=self.command.idempotency_key,
        )
        with self.assertRaises(ProtocolError) as raised:
            self.services.add(alternate)
        self.assertEqual(raised.exception.code, "command.idempotency_conflict")

    def test_seal_before_confirm_recovers_without_second_native_observation(self) -> None:
        self.services.fail_commit_once = True
        with self.assertRaisesRegex(RuntimeError, "after Discover seal"):
            execute_stage(command_id=self.command.command_id, handler=self.handler, services=self.services)
        self.assertEqual(len(self.port.transfers), 1)
        shutil.rmtree(self.harness.source)
        recovered = execute_stage(command_id=self.command.command_id, handler=self.handler, services=self.services)
        self.assertEqual(recovered.outcome, "completed")
        self.assertEqual(len(self.port.transfers), 1)

    def test_unknown_effect_is_refused_before_native_call(self) -> None:
        blocked = _BlockedEffectPort()
        handler = DiscoverHandler(blocked)
        with self.assertRaisesRegex(RuntimeError, "effect is unknown"):
            handler.execute(self.command, self.harness.claim(self.command), self.harness.workspace())
        self.assertEqual(blocked.authorization_checks, 1)
        self.assertEqual(blocked.native_calls, 0)


if __name__ == "__main__":
    unittest.main()
