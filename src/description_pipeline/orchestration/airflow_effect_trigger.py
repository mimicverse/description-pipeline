"""Passive asynchronous wake-up trigger for one canonical pending effect."""

from __future__ import annotations

import asyncio
import os
from contextlib import suppress
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from airflow.triggers.base import BaseTrigger, TriggerEvent

from ..artifacts import ArtifactStore
from ..io import canonical, digest, parse_data
from ..pipeline.contracts import (
    StageCommand,
    StageEffectWait,
    StagePending,
)
from ..pipeline.effects import EffectIntent, EffectSettlement
from ..platform.facts import FactStore

if TYPE_CHECKING:
    from ..pipeline.application import EffectWakeupHint
    from .native_operation_client import NativeOperationStatusExpectation


EffectWaitProbe = Callable[[StageEffectWait], Awaitable["EffectWakeupHint"]]


@dataclass(frozen=True, slots=True)
class _EffectEvidence:
    artifacts: ArtifactStore
    command: StageCommand
    intent: EffectIntent
    settlement: EffectSettlement
    facts: FactStore | None = None


def _minimum_poll_interval(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("poll_interval must be a number")
    interval = float(value)
    if not 0.1 <= interval <= 300:
        raise ValueError("poll_interval must be between 0.1 and 300 seconds")
    return interval


def _declared_member(manifest: Mapping[str, Any], member: str) -> dict[str, Any]:
    matches = [item for item in manifest["members"] if item["path"] == member]
    if len(matches) != 1:
        raise ValueError(f"The pending outbound artifact lacks one {member}")
    return matches[0]


def _load_effect_evidence(wait: StageEffectWait) -> _EffectEvidence:
    """Load only canonical facts and the small outbound identity."""

    artifacts = ArtifactStore.from_environment(os.environ)
    facts = FactStore.from_environment(os.environ, result_reader=artifacts)
    command = facts.load_command(wait.command_id)
    facts.verify_pending_effect(command, StagePending(wait))
    intent = facts.effect_intent(
        wait.effect_id,
        expected_command_id=wait.command_id,
    )
    settlement = facts.effect_settlement(
        wait.effect_id,
        expected_command_id=wait.command_id,
    )
    outbound = settlement.outbound_artifact
    if (
        intent.effect_key != wait.effect_key
        or intent.effect_kind != wait.effect_kind
        or intent.identity_digest != wait.intent_identity_digest
        or intent.expected_state_digest != wait.expected_state_digest
        or outbound is None
        or outbound.artifact_id != wait.outbound_artifact_id
        or outbound.set_sha256 != wait.outbound_digest
    ):
        raise ValueError("The pending effect facts differ from their wait")
    return _EffectEvidence(
        facts=facts,
        artifacts=artifacts,
        command=command,
        intent=intent,
        settlement=settlement,
    )


def _expectation_from_operation(
    operation: Mapping[str, Any],
) -> NativeOperationStatusExpectation:
    from .native_operation_client import NativeOperationStatusExpectation

    return NativeOperationStatusExpectation(
        operation_id=operation["operation_id"],
        kind=operation["kind"],
        target_id=operation["target_id"],
        request_sha256=operation["request_sha256"],
        native_tool_requirement_sha256=operation["native_tool"][
            "requirement_sha256"
        ],
        input_archive_sha256=operation["input"]["archive_sha256"],
        payload_sha256=operation["payload_sha256"],
    )


def _native_expectation(
    evidence: _EffectEvidence,
) -> tuple[str, NativeOperationStatusExpectation]:
    from ..schemas import (
        NATIVE_CAPTURE_EFFECT_SCHEMA,
        NATIVE_DISCOVER_EFFECT_SCHEMA,
    )
    from ..pipeline.contracts import NativeTargetIdentity
    from .native_operation_client import MAX_OPERATION_JSON_BYTES
    from .native_operation_runtime import (
        CAPTURE_KIND,
        DISCOVER_KIND,
        NativeOperationError,
        build_operation_record,
    )
    from ..pipeline.capture import (
        CAPTURE_INPUT_ARCHIVE,
        CaptureRequest,
        _CAPTURE_OUTBOUND_FIELDS,
        _capture_operation_envelope,
    )
    from ..orchestration.discover_protocol import SOURCE_ARCHIVE
    from ..sources.solidworks.discover_stage import (
        NATIVE_OBSERVATION_SCHEMA,
        NativeDiscoverRequest,
    )

    outbound = evidence.settlement.outbound_artifact
    if outbound is None:
        raise ValueError("The pending native effect has no outbound artifact")
    command = evidence.command
    if command.stage == "capture":
        raw, outbound_member, manifest = evidence.artifacts.read_metadata_member(
            outbound,
            "outbound.json",
            max_bytes=MAX_OPERATION_JSON_BYTES,
        )
        record = parse_data(raw, Path("outbound.json"))
        if (
            not isinstance(record, dict)
            or set(record) != _CAPTURE_OUTBOUND_FIELDS
            or raw != canonical(record)
        ):
            raise ValueError("The Capture pending outbound metadata is invalid")
        archive_member = _declared_member(manifest, CAPTURE_INPUT_ARCHIVE)
        if (
            record.get("input_archive_sha256") != archive_member.get("sha256")
            or record.get("input_archive_bytes") != archive_member.get("bytes")
        ):
            raise ValueError("The Capture pending archive identity is inconsistent")
        request = CaptureRequest.from_command(command)
        expected_state = {
            "command_id": command.command_id,
            "effect_key": request.effect_key,
            "handoff_sha256": request.handoff_sha256,
            "input_archive_bytes": archive_member["bytes"],
            "input_archive_sha256": archive_member["sha256"],
            "main_assembly": request.main_assembly,
            "native_target_id": request.native_target_identity["target_id"],
            "native_tool_record_sha256": request.native_tool_record_sha256,
            "native_tool_source_sha256": request.native_tool_source_sha256,
            "object_set_sha256": record.get("object_set_sha256"),
            "operation_id": record.get("operation_id"),
            "outbound_sha256": digest(
                {"members": [outbound_member, archive_member]}
            ),
            "payload_sha256": record.get("payload_sha256"),
            "prepared_sha256": request.prepared_sha256,
            "request_digest": command.request_digest,
            "schema_version": NATIVE_CAPTURE_EFFECT_SCHEMA,
            "upload_sha256": request.upload_sha256,
        }
        if (
            record.get("schema_version") != NATIVE_CAPTURE_EFFECT_SCHEMA
            or record.get("command_id") != command.command_id
            or record.get("run_id") != command.run_id
            or record.get("request_digest") != command.request_digest
            or record.get("effect_key") != request.effect_key
            or expected_state != dict(evidence.intent.expected_state)
        ):
            raise ValueError("The Capture pending outbound differs from its intent")
        operation = _capture_operation_envelope(record)
        if (
            operation["operation_id"] != record.get("operation_id")
            or operation["payload_sha256"] != record.get("payload_sha256")
        ):
            raise ValueError("The Capture pending operation identity is invalid")
        return CAPTURE_KIND, _expectation_from_operation(operation)

    if command.stage == "discover":
        request_raw, _, manifest = evidence.artifacts.read_metadata_member(
            outbound,
            "request.json",
            max_bytes=MAX_OPERATION_JSON_BYTES,
        )
        request = NativeDiscoverRequest.from_record(
            parse_data(request_raw, Path("request.json"))
        )
        if request_raw != canonical(request.to_record()) + b"\n":
            raise ValueError("The Discover pending request is not canonical")
        target = NativeTargetIdentity.from_record(
            command.request.get("native_target_identity")
        )
        source_raw, _, _ = evidence.artifacts.read_metadata_member(
            outbound,
            "source.json",
            max_bytes=MAX_OPERATION_JSON_BYTES,
        )
        source = parse_data(source_raw, Path("source.json"))
        archive_member = _declared_member(manifest, SOURCE_ARCHIVE)
        if (
            source
            != {
                "source_files": dict(request.source_files),
                "source_inventory_sha256": request.source_inventory_sha256,
            }
            or source_raw != canonical(source) + b"\n"
            or request.command_id != command.command_id
            or request.run_id != command.run_id
            or request.request_digest != command.request_digest
            or request.expected_native_tool_sha256
            != command.request.get("expected_native_tool_sha256")
            or request.expected_native_tool_record_sha256
            != command.request.get("expected_native_tool_record_sha256")
            or request.native_tool_requirement_artifact_id
            != command.request.get("native_tool_requirement_artifact_id")
        ):
            raise ValueError("The Discover pending outbound differs from its command")
        expected_state = {
            "schema_version": NATIVE_DISCOVER_EFFECT_SCHEMA,
            "operation_id": request.operation_id,
            "request_digest": command.request_digest,
            "source_inventory_sha256": request.source_inventory_sha256,
            "source_archive_sha256": archive_member["sha256"],
            "native_target_id": target.target_id,
            "native_tool_sha256": request.expected_native_tool_sha256,
            "native_tool_record_sha256": request.expected_native_tool_record_sha256,
            "native_tool_requirement_artifact_id": (
                request.native_tool_requirement_artifact_id
            ),
        }
        if expected_state != dict(evidence.intent.expected_state):
            raise ValueError("The Discover pending outbound differs from its intent")
        try:
            operation = build_operation_record(
                operation_id=request.operation_id,
                kind=DISCOVER_KIND,
                request=request.to_record(),
                input_archive_sha256=archive_member["sha256"],
                input_archive_bytes=archive_member["bytes"],
                native_tool_requirement_sha256=(
                    request.expected_native_tool_record_sha256
                ),
                output_contract=NATIVE_OBSERVATION_SCHEMA,
                target_id=target.target_id,
            )
        except NativeOperationError as error:
            raise ValueError("The Discover pending operation is invalid") from error
        return DISCOVER_KIND, _expectation_from_operation(operation)

    raise ValueError("native_operation is valid only for Discover or Capture")


def _native_probe_from_environment(
    wait: StageEffectWait,
) -> tuple[Any, NativeOperationStatusExpectation, FactStore | None]:
    """Load one small immutable native identity without materializing its archive."""

    from ..pipeline.application import _native_client_from_environment

    evidence = _load_effect_evidence(wait)
    if evidence.intent.effect_kind != "native_operation":
        raise ValueError("The pending effect is not a native operation")
    _kind, expected = _native_expectation(evidence)
    return (
        _native_client_from_environment(
            os.environ,
            target_id=expected.target_id,
        ),
        expected,
        evidence.facts,
    )


def _native_wakeable(view: object | None) -> bool:
    if view is None:
        return False
    state = getattr(view, "state", None)
    if state == "accepted":
        return True
    return getattr(view, "resource_terminated", False) is True and state in {
        "satisfied",
        "failed",
        "cancelled",
        "unknown",
    }


def _native_observation_retryable(error: Exception) -> bool:
    from .native_operation_client import (
        NativeOperationClientConflict,
        NativeOperationClientError,
        NativeOperationNotFound,
        NativeOperationProtocolError,
    )

    if getattr(error, "status", None) in {401, 403}:
        return False
    if isinstance(error, NativeOperationNotFound):
        return True
    if not isinstance(error, NativeOperationClientError):
        return False
    if isinstance(error, NativeOperationClientConflict):
        return error.code == "native_target_mismatch"
    if isinstance(error, NativeOperationProtocolError):
        return True
    return error.status is None or (
        isinstance(error.status, int) and error.status >= 500
    )


def _wakeup(wait: StageEffectWait, changed: bool) -> EffectWakeupHint:
    from ..pipeline.application import EffectWakeupHint

    return EffectWakeupHint(
        wait_digest=wait.digest,
        observed_at=datetime.now(UTC),
        changed=changed,
    )


async def _prepare_native_effect_probe(
    wait: StageEffectWait,
) -> EffectWaitProbe:
    from .native_operation_client import NativeOperationClientError

    client, expected, facts = await asyncio.to_thread(
        _native_probe_from_environment,
        wait,
    )

    async def probe(value: StageEffectWait) -> EffectWakeupHint:
        if value != wait:
            raise ValueError("effect status probe names a different wait")
        try:
            view = await asyncio.to_thread(
                client.get_operation,
                expected.operation_id,
            )
        except NativeOperationClientError as error:
            if _native_observation_retryable(error):
                return _wakeup(wait, False)
            raise
        if view is not None:
            expected.require(view)
            if facts is not None and getattr(view, "activity", None) is not None:
                with suppress(Exception):
                    facts.record_run_activity(
                        wait.run_id,
                        view.operation_id,
                        view.activity,
                        terminal=view.state
                        in {"satisfied", "failed", "cancelled", "unknown"},
                    )
        return _wakeup(wait, _native_wakeable(view))

    return probe


async def _prepare_publish_effect_probe(
    wait: StageEffectWait,
) -> EffectWaitProbe:
    from ..pipeline.stages.publish import (
        PUBLICATION_PLAN,
        PublishGitStatusProbe,
        PublishPullRequestStatusProbe,
    )
    from .native_operation_client import MAX_OPERATION_JSON_BYTES

    evidence = await asyncio.to_thread(_load_effect_evidence, wait)
    outbound = evidence.settlement.outbound_artifact
    if outbound is None:
        raise ValueError("The pending Publish effect has no outbound artifact")
    evidence.artifacts.read_metadata_member(
        outbound,
        "request.json",
        max_bytes=MAX_OPERATION_JSON_BYTES,
    )
    if wait.effect_kind == "git_ref_update":
        evidence.artifacts.read_metadata_member(
            outbound,
            PUBLICATION_PLAN,
            max_bytes=MAX_OPERATION_JSON_BYTES,
        )
    outbound_root = evidence.artifacts.objects / outbound.artifact_id
    if wait.effect_kind == "git_ref_update":
        status_probe = await asyncio.to_thread(
            PublishGitStatusProbe.from_sealed_outbound,
            wait=wait,
            outbound=outbound,
            outbound_root=outbound_root,
        )
    elif wait.effect_kind == "pr_upsert":
        status_probe = await asyncio.to_thread(
            PublishPullRequestStatusProbe.from_sealed_outbound,
            wait=wait,
            outbound=outbound,
            outbound_root=outbound_root,
        )
    else:
        raise ValueError("The pending effect is not a Publish operation")

    async def probe(value: StageEffectWait) -> EffectWakeupHint:
        if value != wait:
            raise ValueError("effect status probe names a different wait")
        hint = await status_probe.observe()
        if hint.wait_digest != wait.digest:
            raise ValueError("Publish status probe returned another wait")
        return _wakeup(wait, hint.changed)

    return probe


async def _prepare_effect_probe(wait: StageEffectWait) -> EffectWaitProbe:
    if wait.effect_kind == "native_operation":
        return await _prepare_native_effect_probe(wait)
    if wait.effect_kind in {"git_ref_update", "pr_upsert"}:
        return await _prepare_publish_effect_probe(wait)
    raise ValueError(
        "The pending effect kind has no read-only wake-up observer"
    )


async def probe_effect_wait(wait: StageEffectWait) -> EffectWakeupHint:
    """Perform one read-only effect observation for a canonical wait."""

    probe = await _prepare_effect_probe(wait)
    return await probe(wait)


class EffectPollTrigger(BaseTrigger):
    """Observe one exact effect without acquiring or changing platform authority."""

    def __init__(
        self,
        *,
        wait: Mapping[str, object],
        poll_interval: float = 5.0,
        probe: EffectWaitProbe | None = None,
    ) -> None:
        super().__init__()
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
        wait = StageEffectWait.from_record(self.wait)
        expected_digest = wait.digest
        probe = (
            await _prepare_effect_probe(wait)
            if self._probe is None
            else self._probe
        )
        while True:
            hint = await probe(wait)
            if hint.wait_digest != expected_digest:
                raise ValueError(
                    "effect status probe returned a different wait identity"
                )
            if hint.changed:
                yield TriggerEvent({"wait": wait.to_record()})
                return
            await asyncio.sleep(self.poll_interval)


__all__ = [
    "EffectPollTrigger",
    "EffectWaitProbe",
    "probe_effect_wait",
]
