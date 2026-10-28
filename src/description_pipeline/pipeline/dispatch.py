"""Technical Airflow dispatch without a seventh business stage."""

from __future__ import annotations

import shutil
import tempfile
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from ..artifacts.store import path_has_link_ancestor
from ..io import canonical, write_json
from .contracts import DispatchCommand, DispatchConf, ProtocolError
from .effects import EffectSettlement, EffectState

if TYPE_CHECKING:
    from ..artifacts import ArtifactStore
    from ..platform.facts import FactStore
    from .services import ClaimPolicy

_OBSERVATION_STATES = frozenset({"known", "absent", "unobservable"})


@dataclass(frozen=True, slots=True)
class DispatchObservation:
    """One raw result of querying the fixed Airflow DagRun identity."""

    state_class: str
    payload: Mapping[str, Any]

    def __post_init__(self) -> None:
        if self.state_class not in _OBSERVATION_STATES:
            raise ProtocolError("dispatch.observation_invalid", {"state_class": self.state_class})
        if not isinstance(self.payload, Mapping):
            raise ProtocolError("dispatch.observation_invalid", {"where": "payload"})
        try:
            canonical(dict(self.payload))
        except (TypeError, ValueError) as error:
            raise ProtocolError("dispatch.observation_invalid", {"where": "payload"}) from error


@dataclass(frozen=True, slots=True)
class DispatchResult:
    command_id: str
    effect_id: str
    state: EffectState
    observation: DispatchObservation | None


class DispatchAdapter(Protocol):
    """Credential-owning Airflow transport for one fixed dispatch command."""

    def trigger(self, command: DispatchCommand, conf: DispatchConf) -> Mapping[str, Any] | None: ...

    def observe(self, command: DispatchCommand, conf: DispatchConf) -> DispatchObservation: ...


class TechnicalDispatchService:
    """Drive one technical command through the common effect and claim facts."""

    def __init__(
        self,
        *,
        facts: FactStore,
        artifacts: ArtifactStore,
        work_root: str | Path,
        claim_policy: ClaimPolicy,
        clock: Callable[[], datetime],
        adapter: DispatchAdapter,
    ) -> None:
        self.facts = facts
        self.artifacts = artifacts
        root = Path(work_root)
        if path_has_link_ancestor(root):
            raise ProtocolError("dispatch.workspace_invalid", {"root": str(root)})
        root.mkdir(parents=True, exist_ok=True)
        self.work_root = root.resolve()
        self.claim_policy = claim_policy
        self.clock = clock
        self.adapter = adapter

    def dispatch(self, command_id: str) -> DispatchResult:
        command = self.facts.load_dispatch_command(command_id)
        conf = self.artifacts.read_dispatch_conf(command.conf_artifact)
        if conf.run_id != command.run_id or conf.freeze_command_id != command.freeze_command_id:
            raise ProtocolError("dispatch.conf_mismatch", {"command_id": command_id})
        settlement = self.facts.effect_settlement_for_key(command.command_id, command.effect_key)
        if settlement is None:
            raise ProtocolError("dispatch.effect_missing", {"command_id": command_id})

        if settlement.state is EffectState.PREPARED:
            claim = self._claim(command)
            settlement = self.facts.effect_settlement(
                settlement.effect_id,
                expected_command_id=command.command_id,
            )
            if settlement.state is not EffectState.PREPARED:
                return self._observe_existing(command, conf, settlement)
            self.facts.authorize_effect_dispatch(
                effect_id=settlement.effect_id,
                claim=claim,
                source="airflow-dispatch",
                payload_digest=command.request_digest,
                observed_at=self._now(),
                fact_id="fact-" + str(uuid.uuid4()),
            )
            try:
                response = self.adapter.trigger(command, conf)
            except Exception as error:
                payload = self._seal_json(
                    command,
                    {
                        "error_type": type(error).__name__,
                        "error_message": str(error)[:4096],
                    },
                    kind="airflow_dispatch_timeout",
                )
                self.facts.append_effect_fact(
                    effect_id=settlement.effect_id,
                    claim=claim,
                    fact_type="timeout",
                    source="airflow-dispatch",
                    state_class=None,
                    payload_artifact=payload,
                    payload_digest=payload.set_sha256,
                    observed_at=self._now(),
                    fact_id="fact-" + str(uuid.uuid4()),
                    expected_command_id=command.command_id,
                )
            else:
                if response is not None and not isinstance(response, Mapping):
                    raise ProtocolError("dispatch.response_invalid", {"command_id": command_id})
                payload = self._seal_json(
                    command,
                    {"response": dict(response or {})},
                    kind="airflow_dispatch_response",
                )
                self.facts.append_effect_fact(
                    effect_id=settlement.effect_id,
                    claim=claim,
                    fact_type="response",
                    source="airflow-dispatch",
                    state_class=None,
                    payload_artifact=payload,
                    payload_digest=payload.set_sha256,
                    observed_at=self._now(),
                    fact_id="fact-" + str(uuid.uuid4()),
                    expected_command_id=command.command_id,
                )
            return self._observe_existing(
                command,
                conf,
                self.facts.effect_settlement(
                    settlement.effect_id,
                    expected_command_id=command.command_id,
                ),
            )

        return self._observe_existing(command, conf, settlement)

    def _claim(self, command: DispatchCommand):
        if self.claim_policy.executor_sha256 != command.expected_executor_sha256:
            raise ProtocolError(
                "dispatch.executor_mismatch",
                {
                    "expected": command.expected_executor_sha256,
                    "actual": self.claim_policy.executor_sha256,
                },
            )
        now = self._now()
        return self.facts.acquire_claim(
            command.command_id,
            claim_id=self.claim_policy.claim_id_factory(),
            owner_sha256=self.claim_policy.owner_sha256,
            executor_sha256=self.claim_policy.executor_sha256,
            now=now,
            expires_at=now + self.claim_policy.lease,
            lane_key=self.claim_policy.lane_key,
            dag_id=command.dag_id,
            dag_run_id=command.dag_run_id,
            task_id=self.claim_policy.task_id,
            map_index=self.claim_policy.map_index,
        )

    def _observe_existing(
        self,
        command: DispatchCommand,
        conf: DispatchConf,
        settlement: EffectSettlement,
    ) -> DispatchResult:
        if settlement.state in {EffectState.SATISFIED, EffectState.DEFINITELY_FAILED}:
            self.facts.close_open_claim_for_command(
                command.command_id,
                closure_code=(
                    "completed"
                    if settlement.state is EffectState.SATISFIED
                    else "definite_failure"
                ),
                closed_at=self._now(),
            )
            return DispatchResult(command.command_id, settlement.effect_id, settlement.state, None)
        if settlement.state is EffectState.CONFLICT:
            return DispatchResult(command.command_id, settlement.effect_id, settlement.state, None)
        try:
            observation = self.adapter.observe(command, conf)
        except Exception as error:
            payload = self._seal_json(
                command,
                {"error_type": type(error).__name__, "error_message": str(error)[:4096]},
                kind="airflow_dispatch_observation",
            )
            self.facts.append_effect_fact(
                effect_id=settlement.effect_id,
                claim=None,
                fact_type="observation",
                source="airflow-dispatch",
                state_class="unobservable",
                payload_artifact=payload,
                payload_digest=payload.set_sha256,
                observed_at=self._now(),
                fact_id="fact-" + str(uuid.uuid4()),
                expected_command_id=command.command_id,
            )
            observation = DispatchObservation("unobservable", payload=payload.to_record())
        else:
            if observation.state_class == "known" and (
                observation.payload.get("dag_id") != command.dag_id
                or observation.payload.get("dag_run_id") != command.dag_run_id
            ):
                raise ProtocolError("dispatch.observation_mismatch", {"command_id": command.command_id})
            payload = self._seal_json(
                command,
                dict(observation.payload),
                kind="airflow_dispatch_observation",
            )
            self.facts.append_effect_fact(
                effect_id=settlement.effect_id,
                claim=None,
                fact_type="observation",
                source="airflow-dispatch",
                state_class=observation.state_class,
                payload_artifact=payload,
                payload_digest=payload.set_sha256,
                observed_at=self._now(),
                fact_id="fact-" + str(uuid.uuid4()),
                expected_command_id=command.command_id,
            )
        final = self.facts.effect_settlement(
            settlement.effect_id,
            expected_command_id=command.command_id,
        )
        if final.state in {EffectState.SATISFIED, EffectState.DEFINITELY_FAILED}:
            self.facts.close_open_claim_for_command(
                command.command_id,
                closure_code=(
                    "completed"
                    if final.state is EffectState.SATISFIED
                    else "definite_failure"
                ),
                closed_at=self._now(),
            )
        return DispatchResult(command.command_id, settlement.effect_id, final.state, observation)

    def _seal_json(
        self,
        command: DispatchCommand,
        payload: Mapping[str, Any],
        *,
        kind: str,
    ):
        root = Path(tempfile.mkdtemp(prefix="airflow-dispatch-", dir=self.work_root))
        try:
            write_json(root / "observation.json", dict(payload))
            executor = self.claim_policy.executor_sha256
            return self.artifacts.seal_tree(
                root,
                kind=kind,
                producer_run_id=command.run_id,
                producer_stage=None,
                producer_tool_sha256=executor,
            )
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def _now(self) -> datetime:
        value = self.clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise ProtocolError("clock.invalid", {"where": "technical_dispatch"})
        return value
