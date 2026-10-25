"""Canonical Discover handler and one-transfer Windows protocol regressions."""

from __future__ import annotations

import copy
import json
import shutil
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from description_pipeline.artifacts import ArtifactStore
from description_pipeline.delivery import PIPELINE_ID
from description_pipeline.interface_records import config_snapshot_digest
from description_pipeline.io import (
    digest,
    file_digest,
    read_data,
    write_json,
)
from description_pipeline.orchestration.airflow_effect_trigger import (
    _EffectEvidence,
    _native_expectation,
)
from description_pipeline.orchestration.native_operation_runtime import (
    DISCOVER_KIND,
)
from description_pipeline.orchestration.discover_protocol import (
    ArchiveDiscoverAdapter,
    admit_observation_bundle,
    seal_observation_bundle,
)
from description_pipeline.orchestration.native_operation_client import (
    native_discover_operation_id,
)
import description_pipeline.pipeline.discover as discover_module
from description_pipeline.pipeline.contracts import (
    ArtifactRef,
    DiagnosticReplayScope,
    ExecutionClaim,
    NativeTargetIdentity,
    StageCandidate,
    StageCommand,
    StageDeferral,
    StageDecision,
    StageEffectWait,
    StagePending,
    StageResult,
    StageFailure,
)
from description_pipeline.pipeline.contracts import ProtocolError
from description_pipeline.pipeline.discover import (
    CONFIGURATION_KIND,
    DISCOVER_REQUEST_SCHEMA,
    FREEZE_RESULT_KIND,
    RULESET_KIND,
    SOURCE_SET_KIND,
    DiscoverHandler,
)
from description_pipeline.pipeline.engine import StageWorkspace, execute_stage
from description_pipeline.pipeline.freeze import RulesetCatalog, RulesetRoot
from description_pipeline.pipeline.effects import EffectSettlement, EffectState
from description_pipeline.pipeline.effects import EffectResumePhase
from description_pipeline.schemas import (
    CONFIG_SNAPSHOT_SCHEMA,
    NATIVE_DISCOVER_EFFECT_SCHEMA,
    NATIVE_DISCOVER_REQUEST_SCHEMA,
    PLATFORM_CONFIG_SCHEMA,
    STAGE_EFFECT_WAIT_SCHEMA,
    STAGE_RESULT_SCHEMA,
    TOOL_SCHEMA,
)
from description_pipeline.sources.solidworks.discover_stage import (
    NativeDiscoverRefusal,
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
    "schema_version": TOOL_SCHEMA,
    "pipeline_id": "fixture",
    "source_sha256": TOOL_SHA256,
    "runtime": {"role": "native"},
}
NATIVE_TOOL_RECORD_SHA256 = digest(NATIVE_TOOL)
NATIVE_TOOL_REQUIREMENT_ARTIFACT_ID = "native-tool-requirement-fixture"
NATIVE_TARGET = NativeTargetIdentity.create(
    install_id="win-" + "1" * 32,
    pipeline_id=PIPELINE_ID,
)


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
    def __init__(self, *, transform=None, refusal=None, reader_sha256=TOOL_SHA256) -> None:
        self.transform = transform
        self.refusal = refusal
        self.reader_sha256 = reader_sha256
        self.calls = 0

    def observe(self, stage, main_assembly, upload_files):
        self.calls += 1
        if self.refusal is not None:
            raise self.refusal
        evidence = closure_evidence(
            upload_files,
            main_assembly,
            edges=SELECTED_EDGES,
            reader_sha256=self.reader_sha256,
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


class EffectLedger:
    """Small in-memory double for the common effect-port semantics."""

    def __init__(self, artifacts: ArtifactStore) -> None:
        self.artifacts = artifacts
        self.by_key: dict[str, dict] = {}
        self.authorizations = 0
        self.resume_authorizations = 0
        self.timeouts = 0

    def bind(self, command, claim, workspace) -> _MemoryEffectPort:
        return _MemoryEffectPort(self, command, claim, workspace)

    def preload_unknown(self, effect_key: str) -> None:
        effect_id = "effect-" + digest({"key": effect_key})
        self.by_key[effect_key] = {
            "effect_id": effect_id,
            "state": EffectState.UNKNOWN,
            "outbound_sha256": None,
            "payload": None,
        }

    def states_for_command(self, command_id: str) -> dict[str, EffectState]:
        return {
            value["effect_id"]: value["state"]
            for value in self.by_key.values()
            if value.get("command_id") == command_id
        }


class _MemoryEffectPort:
    def __init__(self, ledger: EffectLedger, command, claim, workspace) -> None:
        self.ledger = ledger
        self.command = command
        self.claim = claim
        self.workspace = workspace

    def prepare_effect(self, *, effect_key, effect_kind, target, expected_state, outbound_root):
        outbound = self.ledger.artifacts.seal_tree(
            outbound_root,
            kind="effect_payload",
            producer_run_id=self.command.run_id,
            producer_stage=self.command.stage,
            producer_tool_sha256=self.claim.executor_sha256,
        )
        existing = self.ledger.by_key.get(effect_key)
        if existing is not None:
            if existing["outbound_sha256"] != outbound.set_sha256:
                raise ProtocolError("effect.identity_conflict", {"effect_key": effect_key})
            return SimpleNamespace(effect_id=existing["effect_id"])
        effect_id = "effect-" + digest(
            {
                "command_id": self.command.command_id,
                "effect_key": effect_key,
                "outbound": outbound.set_sha256,
            }
        )
        target_value = dict(target)
        expected_value = dict(expected_state)
        expected_state_digest = digest(expected_value)
        self.ledger.by_key[effect_key] = {
            "effect_id": effect_id,
            "command_id": self.command.command_id,
            "effect_kind": effect_kind,
            "expected_state": expected_value,
            "expected_state_digest": expected_state_digest,
            "intent_identity_digest": digest(
                {
                    "command_id": self.command.command_id,
                    "effect_id": effect_id,
                    "effect_key": effect_key,
                    "effect_kind": effect_kind,
                    "expected_state": expected_value,
                    "expected_state_digest": expected_state_digest,
                    "outbound_artifact_id": outbound.artifact_id,
                    "outbound_digest": outbound.set_sha256,
                    "target": target_value,
                }
            ),
            "outbound": outbound,
            "state": EffectState.PREPARED,
            "outbound_sha256": outbound.set_sha256,
            "payload": None,
            "resume_phase": None,
            "target": target_value,
        }
        return SimpleNamespace(effect_id=effect_id)

    def authorize_dispatch(self, effect_id, *, source):
        entry = self._entry(effect_id)
        if entry["state"] is not EffectState.PREPARED:
            raise ProtocolError("effect.dispatch_refused", {"state": entry["state"].value})
        entry["state"] = EffectState.UNKNOWN
        self.ledger.authorizations += 1
        return SimpleNamespace(effect_id=effect_id, fact_type="dispatch_authorized")

    def authorize_resume(self, effect_id, *, source):
        entry = self._entry(effect_id)
        if entry["state"] is not EffectState.UNKNOWN:
            raise ProtocolError(
                "effect.dispatch_refused",
                {"state": entry["state"].value},
            )
        self.ledger.resume_authorizations += 1
        return SimpleNamespace(effect_id=effect_id, fact_type="resume_authorized")

    def record_response(self, effect_id, *, source, response_root):
        entry = self._entry(effect_id)
        if entry["state"] is not EffectState.UNKNOWN:
            raise ProtocolError("effect.response_refused", {"state": entry["state"].value})
        return SimpleNamespace(effect_id=effect_id, fact_type="response")

    def record_timeout(self, effect_id, *, source, detail_root=None):
        entry = self._entry(effect_id)
        entry["state"] = EffectState.UNKNOWN
        self.ledger.timeouts += 1
        return SimpleNamespace(effect_id=effect_id, fact_type="timeout")

    def record_observation(self, effect_id, *, source, state_class, observation_root):
        entry = self._entry(effect_id)
        payload = self.ledger.artifacts.seal_tree(
            observation_root,
            kind="effect_observation",
            producer_run_id=self.command.run_id,
            producer_stage=self.command.stage,
            producer_tool_sha256=self.claim.executor_sha256,
        )
        entry["payload"] = payload
        entry["state"] = EffectState.SATISFIED if state_class == "known" else EffectState.UNKNOWN
        return SimpleNamespace(effect_id=effect_id, fact_type="observation")

    def reconcile_effect(self, effect_id, *, source, state_class, evidence_root):
        entry = self._entry(effect_id)
        payload = self.ledger.artifacts.seal_tree(
            evidence_root,
            kind="effect_reconciliation",
            producer_run_id=self.command.run_id,
            producer_stage=self.command.stage,
            producer_tool_sha256=self.claim.executor_sha256,
        )
        entry["payload"] = payload
        entry["state"] = (
            EffectState.SATISFIED
            if state_class == "known"
            else EffectState.DEFINITELY_FAILED
            if state_class == "absent"
            else EffectState.UNKNOWN
        )
        return SimpleNamespace(effect_id=effect_id, fact_type="reconciled")

    def effect_state(self, effect_id):
        return self._entry(effect_id)["state"]

    def effect_settlement(self, effect_id):
        entry = self._entry(effect_id)
        return EffectSettlement(
            effect_id=effect_id,
            state=entry["state"],
            decisive_fact=None,
            payload_artifact=entry["payload"],
            outbound_artifact=entry.get("outbound"),
            resume_phase=entry.get("resume_phase"),
        )

    def effect_settlement_for_key(self, effect_key):
        entry = self.ledger.by_key.get(effect_key)
        if entry is None:
            return None
        return self.effect_settlement(entry["effect_id"])

    def _entry(self, effect_id):
        for entry in self.ledger.by_key.values():
            if entry["effect_id"] == effect_id:
                return entry
        raise AssertionError(effect_id)


class DiscoverHarness:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.store = ArtifactStore(root / "cas")
        self.effects = EffectLedger(self.store)
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
        rules_root = self.rules / "root-000"
        (rules_root / "joints").mkdir(parents=True)
        (rules_root / "budget.json").write_text(
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
        (rules_root / "joints/arm.json").write_text(
            json.dumps(
                {
                    "limits": {"lower": -1.5, "upper": 1.5},
                    "drive": {"effort": 6.0, "velocity": 2.0},
                }
            ),
            encoding="utf-8",
        )
        members = {
            path.relative_to(rules_root).as_posix(): file_digest(path)
            for path in sorted(rules_root.rglob("*"))
            if path.is_file()
        }
        roots = (RulesetRoot(path="root-000", members=members),)
        catalog = RulesetCatalog(
            version="records-" + digest([root.to_record() for root in roots]),
            roots=roots,
            records={f"root-000/{path}": checksum for path, checksum in members.items()},
        )
        self.ruleset_version = catalog.version
        write_json(self.rules / "catalog.json", catalog.to_record())
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
                    "store_root": str(self.state / "runs"),
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
            "ruleset_version": self.ruleset_version,
            "ruleset_catalog_sha256": self.rules_ref.set_sha256,
            "platform_config_sha256": self.platform_config_sha256,
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
            "expected_native_tool_record_sha256": NATIVE_TOOL_RECORD_SHA256,
            "native_tool_requirement_artifact_id": (
                NATIVE_TOOL_REQUIREMENT_ARTIFACT_ID
            ),
            "native_target_identity": NATIVE_TARGET.to_record(),
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
            config_snapshot_sha256=self.platform_config_sha256,
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
            owner_sha256=digest({"process": f"discover-{fence}"}),
            executor_sha256=command.expected_executor_sha256,
            lane_key="native",
            expires_at=datetime(2026, 10, 25, tzinfo=UTC) + timedelta(minutes=5),
        )

    def native_request(self, command, *, request=None) -> NativeDiscoverRequest:
        value = self.request() if request is None else request
        return NativeDiscoverRequest(
            schema_version=NATIVE_DISCOVER_REQUEST_SCHEMA,
            operation_id=native_discover_operation_id(
                run_id=command.run_id,
                command_id=command.command_id,
                request_digest=command.request_digest,
            ),
            command_id=command.command_id,
            run_id=command.run_id,
            request_digest=command.request_digest,
            source_files=value["source_files"],
            source_inventory_sha256=value["source_inventory_sha256"],
            main_assembly=value["main_assembly"],
            purpose=value["purpose"],
            expected_native_tool_sha256=value["expected_native_tool_sha256"],
            expected_native_tool_record_sha256=value[
                "expected_native_tool_record_sha256"
            ],
            native_tool_requirement_artifact_id=value[
                "native_tool_requirement_artifact_id"
            ],
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

    def effect_port(self, command, *, fence=1):
        claim = self.claim(command, fence=fence)
        return claim, self.effects.bind(command, claim, self.workspace())

    def port(self, *, reader=None, backend=None, native_tool=None) -> ArchiveDiscoverAdapter:
        backend = backend or CountingBackend(copy.deepcopy(self.record))
        return ArchiveDiscoverAdapter(
            artifacts=self.store,
            native_tool=native_tool or NATIVE_TOOL,
            selection_reader_factory=lambda: reader or ClosureReader(),
            backend_factory=lambda _root, _files: backend,
        )


class DiscoverDomainTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="discover-handler-")
        self.addCleanup(self.temporary.cleanup)
        self.harness = DiscoverHarness(Path(self.temporary.name))
        self.command = self.harness.command()

    def _resume_wait(self) -> StageEffectWait:
        effect_key = f"native-discover:{self.command.command_id}"
        entry = self.harness.effects.by_key[effect_key]
        outbound = entry["outbound"]
        return StageEffectWait(
            schema_version=STAGE_EFFECT_WAIT_SCHEMA,
            run_id=self.command.run_id,
            stage="discover",
            command_id=self.command.command_id,
            request_digest=self.command.request_digest,
            effect_id=entry["effect_id"],
            effect_key=effect_key,
            effect_kind="native_operation",
            intent_identity_digest=entry["intent_identity_digest"],
            outbound_artifact_id=outbound.artifact_id,
            outbound_digest=outbound.set_sha256,
            expected_state_digest=entry["expected_state_digest"],
        )

    def test_complete_sourceset_transfers_once_and_ends_at_discover_result(self) -> None:
        backend = CountingBackend(copy.deepcopy(self.harness.record))
        port = self.harness.port(backend=backend)
        handler = DiscoverHandler(port)
        claim, effects = self.harness.effect_port(self.command)
        execution = handler.execute(
            self.command,
            claim,
            self.harness.workspace(),
            effects=effects,
        )

        self.assertEqual(execution.outcome, "completed")
        self.assertEqual(len(port.invocations), 1)
        self.assertEqual(self.harness.effects.authorizations, 1)
        self.assertEqual(backend.calls, 1)
        root = Path(execution.candidate_root)
        self.assertTrue((root / "prepared/discovery/native-discovery.json").is_file())
        self.assertTrue((root / "native-observation/observation.json").is_file())
        result = read_data(root / "result.json")
        self.assertTrue(result["passed"])
        self.assertIn("spare/base.SLDPRT", result["selection"]["files"])
        self.assertNotIn("unused/base.SLDPRT", result["selection"]["files"])

    def test_nonterminal_native_submission_returns_one_effect_deferral(self) -> None:
        backend = CountingBackend(copy.deepcopy(self.harness.record))
        adapter = self.harness.port(backend=backend)
        claim, effects = self.harness.effect_port(self.command)
        with patch.object(adapter, "submit", return_value=None) as submit:
            execution = DiscoverHandler(adapter).execute(
                self.command,
                claim,
                self.harness.workspace(),
                effects=effects,
            )

        self.assertIsInstance(execution, StageDeferral)
        effect_key = f"native-discover:{self.command.command_id}"
        entry = self.harness.effects.by_key[effect_key]
        self.assertEqual(execution.effect_id, entry["effect_id"])
        self.assertIs(entry["state"], EffectState.UNKNOWN)
        self.assertIsNotNone(entry["outbound"])
        submit.assert_called_once()
        self.assertEqual(backend.calls, 0)
        self.assertEqual(self.harness.effects.authorizations, 1)

    def test_transfer_phase_uses_only_the_narrow_resume_transfer_path(self) -> None:
        adapter = self.harness.port()
        claim, effects = self.harness.effect_port(self.command, fence=1)
        with patch.object(adapter, "submit", return_value=None):
            first = DiscoverHandler(adapter).execute(
                self.command,
                claim,
                self.harness.workspace(),
                effects=effects,
            )
        self.assertIsInstance(first, StageDeferral)
        effect_key = f"native-discover:{self.command.command_id}"
        entry = self.harness.effects.by_key[effect_key]
        entry["state"] = EffectState.UNKNOWN
        entry["resume_phase"] = EffectResumePhase.INPUT_TRANSFER_PENDING
        outbound_before = entry["outbound"]
        wait = self._resume_wait()
        resume_claim, _resume_effects = self.harness.effect_port(
            self.command,
            fence=2,
        )
        resume_claim = replace(
            resume_claim,
            scope="transfer_resume",
            effect_scope_id=entry["effect_id"],
        )
        resume_effects = self.harness.effects.bind(
            self.command,
            resume_claim,
            self.harness.workspace(),
        )

        with (
            patch.object(
                adapter,
                "resume_transfer",
                return_value=None,
            ) as resume_transfer,
            patch.object(
                adapter,
                "submit",
                side_effect=AssertionError(
                    "transfer resume must not use initial submission"
                ),
            ),
        ):
            resumed = DiscoverHandler(adapter).resume_transfer(
                self.command,
                resume_claim,
                self.harness.workspace(),
                effects=resume_effects,
                resume=wait,
            )

        self.assertIsInstance(resumed, StageDeferral)
        self.assertEqual(resumed.effect_id, entry["effect_id"])
        resume_transfer.assert_called_once()
        self.assertEqual(self.harness.effects.resume_authorizations, 1)
        self.assertEqual(entry["outbound"], outbound_before)

    def test_resume_reuses_satisfied_observation_without_resend(self) -> None:
        adapter = self.harness.port()
        handler = DiscoverHandler(adapter)
        first_claim, first_effects = self.harness.effect_port(
            self.command,
            fence=1,
        )
        first = handler.execute(
            self.command,
            first_claim,
            self.harness.workspace(),
            effects=first_effects,
        )
        self.assertEqual(first.outcome, "completed")
        wait = self._resume_wait()
        second_claim, second_effects = self.harness.effect_port(
            self.command,
            fence=2,
        )

        resumed = handler.resume_stage(
            self.command,
            second_claim,
            self.harness.workspace(),
            effects=second_effects,
            resume=wait,
        )

        self.assertEqual(resumed.outcome, "completed")
        self.assertEqual(len(adapter.invocations), 1)
        self.assertEqual(self.harness.effects.authorizations, 1)

    def test_resume_rejects_a_foreign_wait_before_native_resend(self) -> None:
        adapter = self.harness.port()
        handler = DiscoverHandler(adapter)
        first_claim, first_effects = self.harness.effect_port(
            self.command,
            fence=1,
        )
        handler.execute(
            self.command,
            first_claim,
            self.harness.workspace(),
            effects=first_effects,
        )
        wait = replace(
            self._resume_wait(),
            expected_state_digest=digest({"foreign": "wait"}),
        )
        second_claim, second_effects = self.harness.effect_port(
            self.command,
            fence=2,
        )

        with self.assertRaises(ProtocolError) as raised:
            handler.resume_stage(
                self.command,
                second_claim,
                self.harness.workspace(),
                effects=second_effects,
                resume=wait,
            )

        self.assertEqual(
            raised.exception.code,
            "discover.resume_identity_mismatch",
        )
        self.assertEqual(len(adapter.invocations), 1)

    def test_real_discover_handler_runs_as_one_diagnostic_stage(self) -> None:
        scope = DiagnosticReplayScope.from_inputs(
            "discover",
            self.command.input_artifacts,
        )
        command = StageCommand.create(
            command_id="diagnostic-discover",
            run_id="diagnostic-discover-run",
            stage="discover",
            idempotency_key="diagnostic-discover/run",
            request=self.command.to_record()["request"],
            input_artifacts=self.command.input_artifacts,
            config_snapshot_sha256=self.command.config_snapshot_sha256,
            contract_sha256=self.command.contract_sha256,
            policy_sha256=self.command.policy_sha256,
            expected_executor_sha256=self.command.expected_executor_sha256,
            predecessor_decision_id=None,
            diagnostic_scope=scope,
        )
        claim, effects = self.harness.effect_port(command)
        execution = DiscoverHandler(self.harness.port()).execute(
            command,
            claim,
            self.harness.workspace(),
            effects=effects,
        )
        self.assertEqual(execution.outcome, "completed")
        self.assertEqual(command.diagnostic_scope, scope)

    def test_formal_discover_requires_a_freeze_predecessor(self) -> None:
        command = StageCommand.create(
            command_id="discover-without-predecessor",
            run_id="discover-without-predecessor-run",
            stage="discover",
            idempotency_key="discover-without-predecessor/run",
            request=self.command.to_record()["request"],
            input_artifacts=self.command.input_artifacts,
            config_snapshot_sha256=self.command.config_snapshot_sha256,
            contract_sha256=self.command.contract_sha256,
            policy_sha256=self.command.policy_sha256,
            expected_executor_sha256=self.command.expected_executor_sha256,
            predecessor_decision_id=None,
        )
        port = self.harness.port()
        claim, effects = self.harness.effect_port(command)
        with self.assertRaises(StageFailure) as raised:
            DiscoverHandler(port).execute(
                command,
                claim,
                self.harness.workspace(),
                effects=effects,
            )
        self.assertEqual(
            raised.exception.reason_code,
            "discover.predecessor_invalid",
        )
        self.assertFalse(port.invocations)

    def test_sealed_observation_round_trips_as_one_return_bundle(self) -> None:
        claim, effects = self.harness.effect_port(self.command)
        execution = DiscoverHandler(self.harness.port()).execute(
            self.command,
            claim,
            self.harness.workspace(),
            effects=effects,
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

    def test_satisfied_effect_reuses_the_same_sealed_payload_without_resend(self) -> None:
        adapter = self.harness.port()
        handler = DiscoverHandler(adapter)
        first_claim, first_effects = self.harness.effect_port(self.command, fence=1)
        first = handler.execute(
            self.command,
            first_claim,
            self.harness.workspace(),
            effects=first_effects,
        )
        second_claim, second_effects = self.harness.effect_port(self.command, fence=2)
        second = handler.execute(
            self.command,
            second_claim,
            self.harness.workspace(),
            effects=second_effects,
        )
        self.assertEqual(first.outcome, "completed")
        self.assertEqual(second.outcome, "completed")
        self.assertEqual(len(adapter.invocations), 1)
        self.assertEqual(self.harness.effects.authorizations, 1)

    def test_prepared_effect_receives_one_dispatch_authorization(self) -> None:
        request = self.harness.native_request(self.command)
        prepare_claim, prepare_effects = self.harness.effect_port(self.command, fence=1)
        outbound, archive_sha256 = DiscoverHandler._write_outbound(
            self.harness.workspace(),
            prepare_claim,
            request,
            self.harness.source,
        )
        prepare_effects.prepare_effect(
            effect_key=f"native-discover:{self.command.command_id}",
            effect_kind="native_operation",
            target={
                "operation_id": request.operation_id,
                "main_assembly": MAIN,
                "native_tool_sha256": TOOL_SHA256,
            },
            expected_state={
                "schema_version": NATIVE_DISCOVER_EFFECT_SCHEMA,
                "operation_id": request.operation_id,
                "request_digest": self.command.request_digest,
                "source_inventory_sha256": digest(self.harness.source_files),
                "source_archive_sha256": archive_sha256,
                "native_target_id": NATIVE_TARGET.target_id,
                "native_tool_sha256": TOOL_SHA256,
                "native_tool_record_sha256": NATIVE_TOOL_RECORD_SHA256,
                "native_tool_requirement_artifact_id": (
                    NATIVE_TOOL_REQUIREMENT_ARTIFACT_ID
                ),
            },
            outbound_root=outbound,
        )
        entry = self.harness.effects.by_key[
            f"native-discover:{self.command.command_id}"
        ]
        settlement = prepare_effects.effect_settlement(entry["effect_id"])
        evidence = _EffectEvidence(
            artifacts=self.harness.store,
            command=self.command,
            intent=SimpleNamespace(
                effect_kind="native_operation",
                expected_state=entry["expected_state"],
            ),
            settlement=settlement,
        )
        with patch.object(
            self.harness.store,
            "materialize",
            side_effect=AssertionError("trigger must not materialize"),
        ):
            kind, expected = _native_expectation(evidence)
        self.assertEqual(kind, DISCOVER_KIND)
        self.assertEqual(expected.target_id, NATIVE_TARGET.target_id)
        execute_claim, execute_effects = self.harness.effect_port(self.command, fence=2)
        adapter = self.harness.port()
        execution = DiscoverHandler(adapter).execute(
            self.command,
            execute_claim,
            self.harness.workspace(),
            effects=execute_effects,
        )
        self.assertEqual(execution.outcome, "completed")
        self.assertEqual(self.harness.effects.authorizations, 1)
        self.assertEqual(len(adapter.invocations), 1)

    def test_trigger_rejects_noncanonical_discover_metadata_encodings(
        self,
    ) -> None:
        request = self.harness.native_request(self.command)
        prepare_claim, prepare_effects = self.harness.effect_port(
            self.command,
            fence=1,
        )
        outbound, archive_sha256 = DiscoverHandler._write_outbound(
            self.harness.workspace(),
            prepare_claim,
            request,
            self.harness.source,
        )
        expected_state = {
            "schema_version": NATIVE_DISCOVER_EFFECT_SCHEMA,
            "operation_id": request.operation_id,
            "request_digest": self.command.request_digest,
            "source_inventory_sha256": request.source_inventory_sha256,
            "source_archive_sha256": archive_sha256,
            "native_target_id": NATIVE_TARGET.target_id,
            "native_tool_sha256": TOOL_SHA256,
            "native_tool_record_sha256": NATIVE_TOOL_RECORD_SHA256,
            "native_tool_requirement_artifact_id": (
                NATIVE_TOOL_REQUIREMENT_ARTIFACT_ID
            ),
        }
        effect_key = f"native-discover:{self.command.command_id}"
        prepared = prepare_effects.prepare_effect(
            effect_key=effect_key,
            effect_kind="native_operation",
            target={
                "operation_id": self.command.command_id,
                "main_assembly": MAIN,
                "native_tool_sha256": TOOL_SHA256,
            },
            expected_state=expected_state,
            outbound_root=outbound,
        )
        settlement = prepare_effects.effect_settlement(prepared.effect_id)
        entry = self.harness.effects.by_key[effect_key]

        def evidence_for_variant(
            index: int,
            filename: str,
            transform,
        ) -> _EffectEvidence:
            variant = self.harness.root / f"discover-encoding-{index}"
            shutil.copytree(outbound, variant)
            path = variant / filename
            path.write_bytes(transform(path.read_bytes()))
            artifact = self.harness.store.seal_tree(
                variant,
                kind="effect_payload",
                producer_run_id=self.command.run_id,
                producer_stage=self.command.stage,
                producer_tool_sha256=prepare_claim.executor_sha256,
            )
            return _EffectEvidence(
                artifacts=self.harness.store,
                command=self.command,
                intent=SimpleNamespace(
                    effect_kind="native_operation",
                    expected_state=entry["expected_state"],
                ),
                settlement=replace(
                    settlement,
                    outbound_artifact=artifact,
                ),
            )

        def reordered(data: bytes) -> bytes:
            record = json.loads(data)
            return (
                json.dumps(
                    dict(reversed(list(record.items()))),
                    ensure_ascii=False,
                    allow_nan=False,
                    sort_keys=False,
                    separators=(",", ":"),
                ).encode("utf-8")
                + b"\n"
            )

        variants = (
            ("request.json", lambda data: data[:-1]),
            ("request.json", lambda data: data + b"\n"),
            ("source.json", lambda data: data[:-1]),
            ("source.json", lambda data: b" " + data),
            ("request.json", reordered),
        )
        for index, (filename, transform) in enumerate(variants):
            with (
                self.subTest(filename=filename, index=index),
                self.assertRaises(ValueError),
            ):
                _native_expectation(
                    evidence_for_variant(index, filename, transform)
                )

    def test_package_outside_reference_is_refused_before_native_record_read(self) -> None:
        def outside(evidence):
            evidence["documents"][MAIN]["resolved_edges"][0]["resolved_path"] = "C:\\outside\\cad\\base.SLDPRT"
            return evidence

        reader = ClosureReader(transform=outside)
        backend = CountingBackend(copy.deepcopy(self.harness.record))
        claim, effects = self.harness.effect_port(self.command)
        with self.assertRaises(StageFailure) as raised:
            DiscoverHandler(self.harness.port(reader=reader, backend=backend)).execute(
                self.command,
                claim,
                self.harness.workspace(),
                effects=effects,
            )
        self.assertIn(raised.exception.reason_code, {"selection_closure_invalid", "discover.native_refused"})
        self.assertIsInstance(raised.exception.__cause__, NativeDiscoverRefusal)
        self.assertEqual(reader.calls, 1)
        self.assertEqual(backend.calls, 0)

    def test_failure_evidence_write_cannot_replace_the_domain_failure(self) -> None:
        def outside(evidence):
            evidence["documents"][MAIN]["resolved_edges"][0]["resolved_path"] = "C:\\outside\\cad\\base.SLDPRT"
            return evidence

        original_write = discover_module._write_canonical

        def fail_native_evidence(path, value):
            if path.name == "native-failure-evidence.json":
                raise OSError("evidence write unavailable")
            return original_write(path, value)

        claim, effects = self.harness.effect_port(self.command)
        with (
            patch.object(discover_module, "_write_canonical", side_effect=fail_native_evidence),
            self.assertRaises(StageFailure) as raised,
        ):
            DiscoverHandler(self.harness.port(reader=ClosureReader(transform=outside))).execute(
                self.command,
                claim,
                self.harness.workspace(),
                effects=effects,
            )
        self.assertIn(raised.exception.reason_code, {"selection_closure_invalid", "discover.native_refused"})
        self.assertEqual(raised.exception.unavailable_reason, "native_failure_evidence_unavailable")
        self.assertIsInstance(raised.exception.__cause__, NativeDiscoverRefusal)

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

        claim, effects = self.harness.effect_port(self.command)
        execution = DiscoverHandler(self.harness.port(reader=ClosureReader(transform=add_virtual))).execute(
            self.command,
            claim,
            self.harness.workspace(),
            effects=effects,
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
        claim, effects = self.harness.effect_port(self.command)
        with self.assertRaises(StageFailure) as raised:
            DiscoverHandler(self.harness.port(backend=CountingBackend(payload))).execute(
                self.command,
                claim,
                self.harness.workspace(),
                effects=effects,
            )
        self.assertEqual(raised.exception.reason_code, "discover.derivation_invalid")

    def test_no_capture_or_downstream_stage_is_invoked(self) -> None:
        with patch(
            "description_pipeline.pipeline.capture._capture_native_transaction",
            side_effect=AssertionError("Capture must not run from Discover"),
        ) as capture:
            claim, effects = self.harness.effect_port(self.command)
            execution = DiscoverHandler(self.harness.port()).execute(
                self.command,
                claim,
                self.harness.workspace(),
                effects=effects,
            )
        self.assertEqual(execution.outcome, "completed")
        capture.assert_not_called()


class _Services:
    def __init__(self, root: Path, harness: DiscoverHarness, port: ArchiveDiscoverAdapter) -> None:
        self.root = root
        self.harness = harness
        self.port = port
        self.commands: dict[str, StageCommand] = {}
        self.keys: dict[str, str] = {}
        self.candidate = None
        self.decision = None
        self.pending = None
        self.fail_commit_once = False
        self.materializations = 0

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

    def cancellation_for(self, command):
        return None

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
        output_artifact = ArtifactRef(
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
            schema_version=STAGE_RESULT_SCHEMA,
            command_id=command.command_id,
            run_id=command.run_id,
            stage="discover",
            request_digest=command.request_digest,
            outcome=execution.outcome,
            output_artifact=output_artifact,
            actual_executor_sha256=command.expected_executor_sha256,
            checks_sha256=execution.checks_sha256,
            resources_settled=execution.resources_settled,
            required_effect_ids=tuple(self.harness.effects.states_for_command(command.command_id)),
            reason_code=execution.reason_code,
        )
        record_id = digest({"result_record": result.digest})
        result_record_artifact = ArtifactRef(
            artifact_id=record_id,
            kind="stage_result",
            locator=f"cas://artifact/{record_id}",
            manifest_sha256=digest({"manifest": record_id}),
            set_sha256=digest({"record": record_id}),
            producer_run_id=command.run_id,
            producer_stage="discover",
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
        return self.harness.effects.states_for_command(command_id)

    def pending_effect_for_command(self, command_id):
        if (
            self.pending is not None
            and self.pending.wait.command_id == command_id
        ):
            return self.pending
        return None

    def verify_pending_effect(self, command, pending):
        if (
            self.pending != pending
            or pending.wait.command_id != command.command_id
            or pending.wait.request_digest != command.request_digest
        ):
            raise ProtocolError(
                "discover.pending_mismatch",
                {"command_id": command.command_id},
            )
        return pending

    def yield_claim_for_effect(self, command, claim, effect_id):
        entry = next(
            value
            for value in self.harness.effects.by_key.values()
            if value["effect_id"] == effect_id
        )
        outbound = entry["outbound"]
        self.pending = StagePending(
            StageEffectWait(
                schema_version=STAGE_EFFECT_WAIT_SCHEMA,
                run_id=command.run_id,
                stage=command.stage,
                command_id=command.command_id,
                request_digest=command.request_digest,
                effect_id=effect_id,
                effect_key=next(
                    key
                    for key, value in self.harness.effects.by_key.items()
                    if value["effect_id"] == effect_id
                ),
                effect_kind="native_operation",
                intent_identity_digest=entry["intent_identity_digest"],
                outbound_artifact_id=outbound.artifact_id,
                outbound_digest=outbound.set_sha256,
                expected_state_digest=entry["expected_state_digest"],
            )
        )
        return self.pending

    def effect_port(self, command, claim, workspace):
        return self.harness.effects.bind(command, claim, workspace)

    def commit_failure(self, command, claim, failure):
        raise AssertionError("Discover success tests do not commit failures")

    def discard_workspace(self, workspace):
        return None

    def commit_decision(self, command, claim, candidate):
        if self.fail_commit_once:
            self.fail_commit_once = False
            raise RuntimeError("crash after Discover seal and before DB confirmation")
        self.decision = StageDecision(
            decision_id="discover-decision",
            run_id=command.run_id,
            stage="discover",
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
        self.pending = None
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
        self.assertEqual(len(self.port.invocations), 1)

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
        self.assertEqual(len(self.port.invocations), 1)
        shutil.rmtree(self.harness.source)
        recovered = execute_stage(command_id=self.command.command_id, handler=self.handler, services=self.services)
        self.assertEqual(recovered.outcome, "completed")
        self.assertEqual(len(self.port.invocations), 1)

    def test_engine_yields_and_resumes_without_native_resend(self) -> None:
        original_submit = self.port.submit
        with patch.object(self.port, "submit", return_value=None) as submit:
            pending = execute_stage(
                command_id=self.command.command_id,
                handler=self.handler,
                services=self.services,
            )
        self.assertIsInstance(pending, StagePending)
        submit.assert_called_once()
        effect_id = pending.wait.effect_id
        entry = next(
            value
            for value in self.harness.effects.by_key.values()
            if value["effect_id"] == effect_id
        )
        self.assertIs(entry["state"], EffectState.UNKNOWN)

        archive = (
            self.root
            / "engine-workspace-1"
            / "discover-outbound-1"
            / "source-set.zip"
        )
        observation = original_submit(
            archive,
            self.harness.native_request(self.command),
            NATIVE_TARGET,
            self.root / "canonical-observation",
        )
        self.assertIsNotNone(observation)
        claim = self.harness.claim(self.command, fence=1)
        observer_effects = self.harness.effects.bind(
            self.command,
            claim,
            self.harness.workspace(),
        )
        observer_effects.reconcile_effect(
            effect_id,
            source="native_observer",
            state_class="known",
            evidence_root=observation.root,
        )
        self.assertIs(entry["state"], EffectState.SATISFIED)

        resumed = execute_stage(
            command_id=self.command.command_id,
            handler=self.handler,
            services=self.services,
        )

        self.assertEqual(resumed.outcome, "completed")
        self.assertEqual(len(self.port.invocations), 1)
        self.assertIsNone(self.services.pending)

    def test_unknown_effect_is_refused_before_native_call(self) -> None:
        self.harness.effects.preload_unknown(f"native-discover:{self.command.command_id}")
        claim, effects = self.harness.effect_port(self.command)
        with self.assertRaises(ProtocolError) as raised:
            DiscoverHandler(self.port).execute(
                self.command,
                claim,
                self.harness.workspace(),
                effects=effects,
            )
        self.assertEqual(raised.exception.code, "effect.unresolved")
        self.assertEqual(self.harness.effects.authorizations, 0)
        self.assertEqual(len(self.port.invocations), 0)


if __name__ == "__main__":
    unittest.main()
