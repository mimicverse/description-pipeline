"""Shared application protocol for the six engineering stages."""

from .contracts import (
    ArtifactRef,
    ExecutionClaim,
    FailureEnvelope,
    RunAdmission,
    StageCandidate,
    StageCommand,
    StageDecision,
    StageExecution,
    StageFailure,
    StageResult,
    RunStatus,
    StageStatus,
    effect_is_settled,
    project_run_status,
    project_stage_status,
)
from .effects import (
    EffectFact,
    EffectIntent,
    EffectSettlement,
    EffectState,
    StageEffectPort,
    project_effect_settlement,
    project_effect_state,
)
from .engine import StageHandler, StageServices, StageWorkspace, execute_stage
from .discover import DiscoverDomainAdapter, DiscoverHandler
from .freeze import FreezeHandler, FreezeRefusal

__all__ = [
    "ArtifactRef",
    "DiscoverDomainAdapter",
    "DiscoverHandler",
    "EffectFact",
    "EffectIntent",
    "EffectSettlement",
    "EffectState",
    "ExecutionClaim",
    "FailureEnvelope",
    "FreezeHandler",
    "FreezeRefusal",
    "RunAdmission",
    "RunStatus",
    "StageCandidate",
    "StageCommand",
    "StageDecision",
    "StageEffectPort",
    "StageExecution",
    "StageFailure",
    "StageHandler",
    "StageResult",
    "StageServices",
    "StageStatus",
    "StageWorkspace",
    "effect_is_settled",
    "execute_stage",
    "project_effect_settlement",
    "project_effect_state",
    "project_run_status",
    "project_stage_status",
]
