"""Append-only external effect facts and their derived state."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from ..io import canonical, digest
from .contracts import EffectKind, ProtocolError

_HEX64 = re.compile(r"\A[0-9a-f]{64}\Z")
_FACT_TYPES = frozenset(
    {"dispatch_authorized", "response", "timeout", "observation", "reconciled", "conflict"}
)
_OBSERVATION_STATES = frozenset({"known", "absent", "unobservable"})


class EffectState(StrEnum):
    PREPARED = "prepared"
    UNKNOWN = "unknown"
    SATISFIED = "satisfied"
    DEFINITELY_FAILED = "definitely_failed"
    CONFLICT = "conflict"


def _mapping(value: Mapping[str, Any], where: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ProtocolError("effect.invalid", {"where": where})
    result = dict(value)
    try:
        canonical(result)
    except (TypeError, ValueError) as error:
        raise ProtocolError("effect.invalid", {"where": where, "reason": str(error)}) from error
    def freeze(item: Any) -> Any:
        if isinstance(item, Mapping):
            return MappingProxyType({key: freeze(child) for key, child in item.items()})
        if isinstance(item, (list, tuple)):
            return tuple(freeze(child) for child in item)
        return item

    return freeze(result)


def _json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    return value


def _text(value: object, where: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ProtocolError("effect.invalid", {"where": where})
    return value


def _digest(value: object, where: str) -> str:
    if not isinstance(value, str) or _HEX64.fullmatch(value) is None:
        raise ProtocolError("effect.invalid", {"where": where})
    return value


def _utc(value: object, where: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ProtocolError("effect.invalid", {"where": where})
    return value


@dataclass(frozen=True, slots=True)
class EffectIntent:
    """One immutable external effect prepared before it is sent."""

    effect_id: str
    command_id: str
    effect_key: str
    effect_kind: EffectKind
    target: Mapping[str, Any]
    expected_state: Mapping[str, Any]
    expected_state_digest: str
    outbound_artifact_id: str
    outbound_digest: str
    created_at: datetime

    def __post_init__(self) -> None:
        _text(self.effect_id, "effect_id")
        _text(self.command_id, "command_id")
        _text(self.effect_key, "effect_key")
        if self.effect_kind not in {"airflow_dispatch", "native_operation", "git_ref_update", "pr_upsert"}:
            raise ProtocolError("effect.invalid", {"where": "effect_kind", "value": self.effect_kind})
        object.__setattr__(self, "target", _mapping(self.target, "target"))
        object.__setattr__(self, "expected_state", _mapping(self.expected_state, "expected_state"))
        _digest(self.expected_state_digest, "expected_state_digest")
        if digest(_json_value(self.expected_state)) != self.expected_state_digest:
            raise ProtocolError("effect.expected_state_digest_mismatch", {"effect_id": self.effect_id})
        _text(self.outbound_artifact_id, "outbound_artifact_id")
        _digest(self.outbound_digest, "outbound_digest")
        _utc(self.created_at, "created_at")


@dataclass(frozen=True, slots=True)
class EffectFact:
    """One append-only observation about an effect."""

    fact_id: str
    effect_id: str
    seq: int
    claim_id: str | None
    fence: int | None
    fact_type: str
    source: str
    state_class: str | None
    payload_artifact_id: str | None
    payload_digest: str
    observed_at: datetime

    def __post_init__(self) -> None:
        _text(self.fact_id, "fact_id")
        _text(self.effect_id, "effect_id")
        if type(self.seq) is not int or self.seq <= 0:
            raise ProtocolError("effect.invalid", {"where": "seq"})
        if self.claim_id is not None:
            _text(self.claim_id, "claim_id")
        if self.fence is not None and (type(self.fence) is not int or self.fence <= 0):
            raise ProtocolError("effect.invalid", {"where": "fence"})
        if self.fact_type not in _FACT_TYPES:
            raise ProtocolError("effect.invalid", {"where": "fact_type", "value": self.fact_type})
        _text(self.source, "source")
        if self.state_class is not None and self.state_class not in _OBSERVATION_STATES:
            raise ProtocolError("effect.invalid", {"where": "state_class", "value": self.state_class})
        resolution = self.fact_type in {"response", "observation", "reconciled"}
        if resolution and self.state_class is None:
            raise ProtocolError("effect.invalid", {"where": "state_class", "reason": "resolution-required"})
        if not resolution and self.state_class is not None:
            raise ProtocolError("effect.invalid", {"where": "state_class", "reason": "resolution-only"})
        if self.payload_artifact_id is not None:
            _text(self.payload_artifact_id, "payload_artifact_id")
        _digest(self.payload_digest, "payload_digest")
        _utc(self.observed_at, "observed_at")


def project_effect_state(intent: EffectIntent, facts: tuple[EffectFact, ...]) -> EffectState:
    """Derive one effect state from its immutable intent and append-only facts.

    A retry cannot be inferred from a missing response or elapsed lease. The
    only safe result is ``UNKNOWN`` after any send authorization until an exact
    observation or reconcile fact settles it.
    """

    if not isinstance(intent, EffectIntent):
        raise ProtocolError("effect.invalid", {"where": "intent"})
    if not isinstance(facts, tuple) or not all(isinstance(fact, EffectFact) for fact in facts):
        raise ProtocolError("effect.invalid", {"where": "facts"})
    if any(fact.effect_id != intent.effect_id for fact in facts):
        raise ProtocolError("effect.fact_identity_mismatch", {"effect_id": intent.effect_id})
    sequences = [fact.seq for fact in facts]
    if sequences != list(range(1, len(facts) + 1)):
        raise ProtocolError("effect.fact_sequence_invalid", {"effect_id": intent.effect_id})
    ordered = tuple(sorted(facts, key=lambda fact: fact.seq))
    if any(fact.fact_type == "conflict" for fact in ordered):
        return EffectState.CONFLICT

    for fact in reversed(ordered):
        if fact.fact_type not in {"response", "observation", "reconciled"}:
            continue
        if fact.state_class == "unobservable":
            return EffectState.UNKNOWN
        if fact.state_class == "known":
            return EffectState.SATISFIED
        if fact.state_class == "absent":
            return EffectState.DEFINITELY_FAILED

    if any(fact.fact_type in {"dispatch_authorized", "timeout"} for fact in ordered):
        return EffectState.UNKNOWN
    return EffectState.PREPARED
