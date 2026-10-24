"""Shared application protocol for the six engineering stages."""

from .contracts import (
    ArtifactRef,
    ExecutionClaim,
    RunAdmission,
    StageCandidate,
    StageCommand,
    StageDecision,
    StageExecution,
    StageResult,
    RunStatus,
    StageStatus,
    effect_is_settled,
    project_run_status,
    project_stage_status,
)
from .effects import EffectFact, EffectIntent, EffectState, project_effect_state
from .engine import StageHandler, StageServices, StageWorkspace, execute_stage
from .discover import DiscoverHandler, DiscoverNativePort
from .freeze import FreezeHandler, FreezeRefusal

__all__ = [
    "ArtifactRef",
    "DiscoverHandler",
    "DiscoverNativePort",
    "EffectFact",
    "EffectIntent",
    "EffectState",
    "ExecutionClaim",
    "FreezeHandler",
    "FreezeRefusal",
    "RunAdmission",
    "RunStatus",
    "StageCandidate",
    "StageCommand",
    "StageDecision",
    "StageExecution",
    "StageHandler",
    "StageResult",
    "StageServices",
    "StageStatus",
    "StageWorkspace",
    "effect_is_settled",
    "execute_stage",
    "project_effect_state",
    "project_run_status",
    "project_stage_status",
]
