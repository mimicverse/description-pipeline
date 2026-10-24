"""Passive asynchronous wake-up trigger for one canonical pending effect."""

from __future__ import annotations

import asyncio
import os
import tempfile
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from airflow.triggers.base import BaseTrigger, TriggerEvent

from ..artifacts import ArtifactStore
from ..io import digest
from ..platform.facts import FactStore

if TYPE_CHECKING:
    from ..pipeline.application import EffectWakeupHint
    from ..pipeline.contracts import StageEffectWait
    from .native_operation_client import (
        NativeOperationStatusExpectation,
        SealedNativeOperationRequest,
    )


EffectWaitProbe = Callable[["StageEffectWait"], Awaitable["EffectWakeupHint"]]


def _minimum_poll_interval(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("poll_interval must be a number")
    interval = float(value)
    if not 0.1 <= interval <= 300:
        raise ValueError("poll_interval must be between 0.1 and 300 seconds")
    return interval


def _native_status_expectation(
    identity: SealedNativeOperationRequest,
) -> NativeOperationStatusExpectation:
    """Require every immutable field before issuing one status request."""

    from .native_operation_client import NativeOperationStatusExpectation

    input_archive_sha256 = getattr(identity, "input_archive_sha256", None)
    payload_sha256 = getattr(identity, "payload_sha256", None)
    if input_archive_sha256 is None:
        raise ValueError(
            "The sealed native operation request has no input archive identity"
        )
    if payload_sha256 is None:
        raise ValueError(
            "The sealed native operation request has no payload identity"
        )
    return NativeOperationStatusExpectation(
        operation_id=getattr(identity, "operation_id", None),
        kind=getattr(identity, "kind", None),
        request_sha256=getattr(identity, "request_sha256", None),
        native_tool_requirement_sha256=getattr(
            identity,
            "native_tool_requirement_sha256",
            None,
        ),
        input_archive_sha256=input_archive_sha256,
        payload_sha256=payload_sha256,
    )


def _native_probe_from_environment(wait: StageEffectWait) -> tuple[Any, Any]:
    """Load only the immutable identity needed for one status GET."""

    from ..pipeline.contracts import StagePending
    from .native_operation_client import (
        read_sealed_native_operation_request,
    )
    from ..pipeline.application import _native_client_from_environment

    artifacts = ArtifactStore.from_environment(os.environ)
    facts = FactStore.from_environment(os.environ, result_reader=artifacts)
    command = facts.load_command(wait.command_id)
    facts.verify_pending_effect(command, StagePending(wait))
    intent = facts.effect_intent(
        wait.effect_id,
        expected_command_id=wait.command_id,
    )
    if intent.effect_kind != "native_operation":
        raise ValueError("effect trigger supports only native_operation")
    settlement = facts.effect_settlement(
        wait.effect_id,
        expected_command_id=wait.command_id,
    )
    outbound = settlement.outbound_artifact
    if outbound is None:
        raise ValueError("deferred native effect has no sealed outbound artifact")
    artifacts.verify(outbound)
    with tempfile.TemporaryDirectory(
        prefix="description-airflow-effect-probe-"
    ) as directory:
        outbound_root = artifacts.materialize(
            outbound,
            Path(directory) / "outbound",
        )
        identity = read_sealed_native_operation_request(
            command,
            intent,
            outbound_root,
        )
    expected = _native_status_expectation(identity)
    return _native_client_from_environment(os.environ), expected


def _status_changed(view: object | None) -> bool:
    if view is None:
        return False
    state = getattr(view, "state", None)
    resource_terminated = getattr(view, "resource_terminated", False)
    return resource_terminated is True and state in {
        "satisfied",
        "failed",
        "cancelled",
        "unknown",
    }


async def _prepare_native_effect_probe(
    wait: StageEffectWait,
) -> EffectWaitProbe:
    from ..pipeline.application import EffectWakeupHint

    client, expected = await asyncio.to_thread(
        _native_probe_from_environment,
        wait,
    )
    wait_digest = digest(wait.to_record())

    async def probe(value: StageEffectWait) -> EffectWakeupHint:
        if value != wait:
            raise ValueError("effect status probe names a different wait")
        view = await client.probe_operation_status_async(expected)
        return EffectWakeupHint(
            wait_digest=wait_digest,
            observed_at=datetime.now(UTC),
            changed=_status_changed(view),
        )

    return probe


async def probe_effect_wait(wait: StageEffectWait) -> EffectWakeupHint:
    """Perform one read-only native status probe for a canonical wait."""

    probe = await _prepare_native_effect_probe(wait)
    return await probe(wait)


class EffectPollTrigger(BaseTrigger):
    """Poll one exact operation without acquiring or changing platform authority."""

    def __init__(
        self,
        *,
        wait: Mapping[str, object],
        poll_interval: float = 5.0,
        probe: EffectWaitProbe | None = None,
    ) -> None:
        super().__init__()
        from ..pipeline.contracts import StageEffectWait

        self.wait = StageEffectWait.from_record(wait).to_record()
        self.poll_interval = _minimum_poll_interval(poll_interval)
        self._probe = probe

    def serialize(self) -> tuple[str, dict[str, Any]]:
        return (
            "description_pipeline.orchestration.airflow_effect_trigger."
            "EffectPollTrigger",
            {
                "wait": dict(self.wait),
                "poll_interval": self.poll_interval,
            },
        )

    async def run(self) -> AsyncIterator[TriggerEvent]:
        from ..pipeline.contracts import StageEffectWait

        wait = StageEffectWait.from_record(self.wait)
        expected_digest = digest(wait.to_record())
        probe = (
            await _prepare_native_effect_probe(wait)
            if self._probe is None
            else self._probe
        )
        while True:
            hint = await probe(wait)
            if hint.wait_digest != expected_digest:
                raise ValueError("effect status probe returned a different wait identity")
            if hint.changed:
                yield TriggerEvent({"wait": wait.to_record()})
                return
            await asyncio.sleep(self.poll_interval)


__all__ = [
    "EffectPollTrigger",
    "EffectWaitProbe",
    "probe_effect_wait",
]
