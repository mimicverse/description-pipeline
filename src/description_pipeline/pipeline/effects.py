"""Append-only external effect facts and their derived state."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Protocol

from ..io import canonical, digest
from .contracts import ArtifactRef, EffectKind, NativeTargetIdentity, ProtocolError

_HEX64 = re.compile(r"\A[0-9a-f]{64}\Z")
_SAFE_ID = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_FACT_TYPES = frozenset(
    {
        "dispatch_authorized",
        "resume_authorized",
        "response",
        "timeout",
        "observation",
        "reconciled",
        "conflict",
    }
)
_OBSERVATION_STATES = frozenset({"known", "absent", "unobservable"})
_GIT_OID = re.compile(r"\A[0-9a-f]{40}\Z")
_REPOSITORY = re.compile(r"\A[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")


class EffectState(StrEnum):
    PREPARED = "prepared"
    UNKNOWN = "unknown"
    SATISFIED = "satisfied"
    DEFINITELY_FAILED = "definitely_failed"
    CONFLICT = "conflict"


class EffectResumePhase(StrEnum):
    INPUT_TRANSFER_PENDING = "input_transfer_pending"


def native_transfer_target_digest(target: Mapping[str, Any]) -> str:
    identity = {
        key: _json_value(value)
        for key, value in target.items()
        if key != "target_binding_digest"
    }
    return digest(identity)


def require_native_transfer_target(target: Mapping[str, Any]) -> None:
    """Require a self-bound native target while excluding transport locators."""

    if not isinstance(target, Mapping):
        raise ProtocolError("effect.target_invalid", {"where": "native_transfer"})
    identity = NativeTargetIdentity.from_record(target.get("native_target_identity"))
    if target.get("native_target_id") != identity.target_id:
        raise ProtocolError(
            "effect.target_invalid",
            {"where": "native_transfer.native_target_id"},
        )
    binding = target.get("target_binding_digest")
    if not isinstance(binding, str) or _HEX64.fullmatch(binding) is None:
        raise ProtocolError(
            "effect.target_invalid",
            {"where": "native_transfer.target_binding_digest"},
        )
    if native_transfer_target_digest(target) != binding:
        raise ProtocolError(
            "effect.target_digest_mismatch",
            {"where": "native_transfer.target_binding_digest"},
        )


@dataclass(frozen=True, slots=True)
class EffectSettlement:
    """The projected state plus the decisive fact and immutable outbound."""

    effect_id: str
    state: EffectState
    decisive_fact: EffectFact | None
    payload_artifact: ArtifactRef | None = None
    outbound_artifact: ArtifactRef | None = None
    resume_phase: EffectResumePhase | None = None


class StageEffectPort(Protocol):
    """The command/claim-bound capability injected into a stage handler.

    The port, not the handler, binds command identity, claim/fence, artifact
    sealing and persistence. A handler cannot append a self-declared effect-id
    list or send an effect before ``authorize_dispatch`` succeeds.
    """

    def prepare_effect(
        self,
        *,
        effect_key: str,
        effect_kind: EffectKind,
        target: Mapping[str, Any],
        expected_state: Mapping[str, Any],
        outbound_root: str | Path,
    ) -> EffectIntent: ...

    def authorize_dispatch(self, effect_id: str, *, source: str) -> EffectFact: ...

    def authorize_resume(self, effect_id: str, *, source: str) -> EffectFact: ...

    def record_response(
        self,
        effect_id: str,
        *,
        source: str,
        response_root: str | Path,
    ) -> EffectFact: ...

    def record_timeout(
        self,
        effect_id: str,
        *,
        source: str,
        detail_root: str | Path | None = None,
    ) -> EffectFact: ...

    def record_observation(
        self,
        effect_id: str,
        *,
        source: str,
        state_class: str,
        observation_root: str | Path,
    ) -> EffectFact: ...

    def reconcile_effect(
        self,
        effect_id: str,
        *,
        source: str,
        state_class: str,
        evidence_root: str | Path,
    ) -> EffectFact: ...

    def effect_state(self, effect_id: str) -> EffectState: ...

    def effect_settlement(self, effect_id: str) -> EffectSettlement: ...

    def effect_settlement_for_key(self, effect_key: str) -> EffectSettlement | None: ...


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


def _identifier(value: object, where: str) -> str:
    if not isinstance(value, str) or _SAFE_ID.fullmatch(value) is None:
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


def _exact_keys(value: Mapping[str, Any], keys: set[str], where: str) -> None:
    if set(value) != keys:
        raise ProtocolError(
            "effect.expected_state_invalid",
            {"where": where, "missing": sorted(keys - set(value)), "unknown": sorted(set(value) - keys)},
        )


def _git_oid(value: object, where: str) -> str:
    if not isinstance(value, str) or _GIT_OID.fullmatch(value) is None:
        raise ProtocolError("effect.expected_state_invalid", {"where": where})
    return value


def _git_ref(value: object, where: str) -> str:
    text = _text(value, where)
    if text.startswith("-") or ".." in text or "@{" in text or text.endswith(("/", ".", ".lock")):
        raise ProtocolError("effect.expected_state_invalid", {"where": where})
    return text


def _validate_typed_state(
    effect_kind: str,
    target: Mapping[str, Any],
    expected_state: Mapping[str, Any],
) -> None:
    if effect_kind == "git_ref_update":
        expected_keys = {
            "schema_version",
            "repository",
            "base_ref",
            "expected_base_oid",
            "branch_ref",
            "expected_head_oid",
            "candidate_commit",
            "candidate_tree",
            "subject_sha256",
        }
        _exact_keys(expected_state, expected_keys, "git_ref_update.expected_state")
        _exact_keys(target, {"repository", "branch_ref"}, "git_ref_update.target")
        if expected_state["schema_version"] != "solidworks-to-urdf.git-ref-update/v1":
            raise ProtocolError("effect.expected_state_invalid", {"where": "schema_version"})
        repository = _text(expected_state["repository"], "expected_state.repository")
        if _REPOSITORY.fullmatch(repository) is None:
            raise ProtocolError("effect.expected_state_invalid", {"where": "expected_state.repository"})
        _git_ref(expected_state["base_ref"], "expected_state.base_ref")
        _git_ref(expected_state["branch_ref"], "expected_state.branch_ref")
        for key in ("expected_base_oid", "candidate_commit", "candidate_tree"):
            _git_oid(expected_state[key], f"expected_state.{key}")
        if expected_state["expected_head_oid"] is not None:
            _git_oid(expected_state["expected_head_oid"], "expected_state.expected_head_oid")
        _digest(expected_state["subject_sha256"], "expected_state.subject_sha256")
        if target["repository"] != expected_state["repository"] or target["branch_ref"] != expected_state["branch_ref"]:
            raise ProtocolError("effect.expected_state_invalid", {"where": "git_ref_update.target"})
    elif effect_kind == "pr_upsert":
        expected_keys = {
            "schema_version",
            "repository",
            "base_ref",
            "head_ref",
            "candidate_commit",
            "subject_sha256",
            "title_digest",
            "body_digest",
            "expected_pr",
        }
        _exact_keys(expected_state, expected_keys, "pr_upsert.expected_state")
        _exact_keys(target, {"repository", "base_ref", "head_ref"}, "pr_upsert.target")
        if expected_state["schema_version"] != "solidworks-to-urdf.pr-upsert/v1":
            raise ProtocolError("effect.expected_state_invalid", {"where": "schema_version"})
        repository = _text(expected_state["repository"], "expected_state.repository")
        if _REPOSITORY.fullmatch(repository) is None:
            raise ProtocolError("effect.expected_state_invalid", {"where": "expected_state.repository"})
        _git_ref(expected_state["base_ref"], "expected_state.base_ref")
        _git_ref(expected_state["head_ref"], "expected_state.head_ref")
        _git_oid(expected_state["candidate_commit"], "expected_state.candidate_commit")
        for key in ("subject_sha256", "title_digest", "body_digest"):
            _digest(expected_state[key], f"expected_state.{key}")
        expected_pr = expected_state["expected_pr"]
        if expected_pr is not None:
            if not isinstance(expected_pr, Mapping):
                raise ProtocolError("effect.expected_state_invalid", {"where": "expected_pr"})
            _exact_keys(expected_pr, {"number", "url", "head_oid"}, "expected_pr")
            if type(expected_pr["number"]) is not int or expected_pr["number"] <= 0:
                raise ProtocolError("effect.expected_state_invalid", {"where": "expected_pr.number"})
            _text(expected_pr["url"], "expected_pr.url")
            if expected_pr["head_oid"] != expected_state["candidate_commit"]:
                raise ProtocolError("effect.expected_state_invalid", {"where": "expected_pr.head_oid"})
        if (
            target["repository"] != expected_state["repository"]
            or target["base_ref"] != expected_state["base_ref"]
            or target["head_ref"] != expected_state["head_ref"]
        ):
            raise ProtocolError("effect.expected_state_invalid", {"where": "pr_upsert.target"})


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
        _identifier(self.effect_id, "effect_id")
        _identifier(self.command_id, "command_id")
        _text(self.effect_key, "effect_key")
        if self.effect_kind not in {"airflow_dispatch", "native_operation", "git_ref_update", "pr_upsert"}:
            raise ProtocolError("effect.invalid", {"where": "effect_kind", "value": self.effect_kind})
        object.__setattr__(self, "target", _mapping(self.target, "target"))
        object.__setattr__(self, "expected_state", _mapping(self.expected_state, "expected_state"))
        _validate_typed_state(self.effect_kind, self.target, self.expected_state)
        _digest(self.expected_state_digest, "expected_state_digest")
        if digest(_json_value(self.expected_state)) != self.expected_state_digest:
            raise ProtocolError("effect.expected_state_digest_mismatch", {"effect_id": self.effect_id})
        _identifier(self.outbound_artifact_id, "outbound_artifact_id")
        _digest(self.outbound_digest, "outbound_digest")
        _utc(self.created_at, "created_at")

    def identity_record(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "effect_id": self.effect_id,
            "effect_key": self.effect_key,
            "effect_kind": self.effect_kind,
            "expected_state": _json_value(self.expected_state),
            "expected_state_digest": self.expected_state_digest,
            "outbound_artifact_id": self.outbound_artifact_id,
            "outbound_digest": self.outbound_digest,
            "target": _json_value(self.target),
        }

    @property
    def identity_digest(self) -> str:
        return digest(self.identity_record())


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
    metadata_artifact_id: str | None = None
    resume_phase: EffectResumePhase | None = None

    def __post_init__(self) -> None:
        _identifier(self.fact_id, "fact_id")
        _identifier(self.effect_id, "effect_id")
        if type(self.seq) is not int or self.seq <= 0:
            raise ProtocolError("effect.invalid", {"where": "seq"})
        if self.claim_id is not None:
            _identifier(self.claim_id, "claim_id")
        if self.fence is not None and (type(self.fence) is not int or self.fence <= 0):
            raise ProtocolError("effect.invalid", {"where": "fence"})
        if (self.claim_id is None) != (self.fence is None):
            raise ProtocolError("effect.invalid", {"reason": "claim-fence-pair"})
        if self.fact_type not in _FACT_TYPES:
            raise ProtocolError("effect.invalid", {"where": "fact_type", "value": self.fact_type})
        if (
            self.fact_type in {"dispatch_authorized", "resume_authorized", "response", "timeout"}
            and self.claim_id is None
        ):
            raise ProtocolError("effect.invalid", {"where": "claim_id", "reason": "send-proof-required"})
        _text(self.source, "source")
        if self.state_class is not None and self.state_class not in _OBSERVATION_STATES:
            raise ProtocolError("effect.invalid", {"where": "state_class", "value": self.state_class})
        resolution = self.fact_type in {"observation", "reconciled"}
        if resolution and self.state_class is None:
            raise ProtocolError("effect.invalid", {"where": "state_class", "reason": "resolution-required"})
        if not resolution and self.state_class is not None:
            raise ProtocolError("effect.invalid", {"where": "state_class", "reason": "resolution-only"})
        if self.resume_phase is not None:
            if not isinstance(self.resume_phase, EffectResumePhase):
                raise ProtocolError("effect.invalid", {"where": "resume_phase"})
            if self.fact_type != "observation" or self.state_class != "unobservable":
                raise ProtocolError(
                    "effect.invalid",
                    {"where": "resume_phase", "reason": "nonterminal-observation-required"},
                )
        if self.payload_artifact_id is not None:
            _identifier(self.payload_artifact_id, "payload_artifact_id")
        if self.metadata_artifact_id is not None:
            _identifier(self.metadata_artifact_id, "metadata_artifact_id")
        if self.fact_type in {"observation", "reconciled"} and self.payload_artifact_id is None:
            raise ProtocolError("effect.invalid", {"where": "payload_artifact_id", "reason": "resolution-required"})
        _digest(self.payload_digest, "payload_digest")
        _utc(self.observed_at, "observed_at")


def project_effect_settlement(intent: EffectIntent, facts: tuple[EffectFact, ...]) -> EffectSettlement:
    """Derive one effect state and retain the fact that established it.

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
    state = EffectState.PREPARED
    decisive: EffectFact | None = None
    terminal = False
    resume_phase = None
    for fact in ordered:
        if state is EffectState.CONFLICT:
            if fact.fact_type == "conflict":
                decisive = fact
                continue
            if (
                fact.fact_type == "reconciled"
                and fact.metadata_artifact_id is not None
                and fact.state_class in {"known", "absent"}
            ):
                state = (
                    EffectState.SATISFIED
                    if fact.state_class == "known"
                    else EffectState.DEFINITELY_FAILED
                )
                decisive = fact
                terminal = True
                resume_phase = None
                continue
            # Once a contradiction is recorded, an ordinary observation or
            # late send fact remains audit evidence. Only an exact reconciliation
            # carrying its independent metadata closure can establish a new state.
            continue
        if fact.fact_type == "dispatch_authorized":
            # Every later send attempt is potentially in flight until a newer
            # exact observation or reconciliation settles it.
            state = EffectState.UNKNOWN
            decisive = fact
            terminal = False
            resume_phase = None
            continue
        if fact.fact_type == "resume_authorized":
            state = EffectState.UNKNOWN
            decisive = fact
            terminal = False
            continue
        if fact.fact_type in {"response", "timeout"}:
            # A delayed technical event from an already settled attempt remains
            # append-only evidence, but it cannot downgrade a newer terminal
            # observation for that attempt.
            if terminal:
                continue
            state = EffectState.UNKNOWN
            decisive = fact
            continue
        if fact.fact_type == "conflict":
            state = EffectState.CONFLICT
            decisive = fact
            terminal = True
            resume_phase = None
            continue
        if fact.fact_type == "observation":
            if terminal:
                # A late observation from an old execution remains audit evidence,
                # but it cannot reinterpret an already terminal external effect.
                continue
            if fact.state_class in {"known", "unobservable"}:
                state = EffectState.SATISFIED if fact.state_class == "known" else EffectState.UNKNOWN
            elif fact.state_class == "absent" and state is not EffectState.PREPARED:
                # A current target being absent does not prove that an earlier
                # request cannot still arrive. Only reconciliation can make the
                # absence terminal.
                state = EffectState.UNKNOWN
            decisive = fact
            terminal = state is EffectState.SATISFIED
            resume_phase = None if terminal else fact.resume_phase
            continue
        if fact.fact_type == "reconciled":
            if terminal:
                # Reconciliation establishes a terminal outcome once. Later
                # evidence must use the explicit conflict path if it contradicts it.
                continue
            if fact.state_class == "known":
                state = EffectState.SATISFIED
            elif fact.state_class == "absent":
                state = EffectState.DEFINITELY_FAILED
            else:
                state = EffectState.UNKNOWN
            decisive = fact
            terminal = state in {EffectState.SATISFIED, EffectState.DEFINITELY_FAILED}
            resume_phase = None if terminal else fact.resume_phase
            continue
        raise ProtocolError("effect.invalid", {"where": "fact_type", "value": fact.fact_type})
    return EffectSettlement(
        effect_id=intent.effect_id,
        state=state,
        decisive_fact=decisive,
        payload_artifact=None,
        resume_phase=resume_phase,
    )


def project_effect_state(intent: EffectIntent, facts: tuple[EffectFact, ...]) -> EffectState:
    """Derive one effect state from its immutable intent and append-only facts."""

    return project_effect_settlement(intent, facts).state
