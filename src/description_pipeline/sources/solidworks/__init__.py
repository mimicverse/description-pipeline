"""SolidWorks source adapter.

Public entry points (the pipeline-wide adapter contract):

``freeze(config, destination)``
    Capture a complete, verified snapshot of a SolidWorks assembly.

``load_scene(snapshot_root)``
    Read a frozen snapshot back without SolidWorks and return its scene.

Both are also reachable through :data:`ADAPTER` so the pipeline can register the
source without importing private modules.
"""

from __future__ import annotations

from typing import Any

from .freeze import FREEZE_SCHEMA, SOURCE_KIND, freeze, validate_source_config
from .scene import SCENE_SCHEMA, load_scene, normalize_scene
from .verify import verify_normalization

ADAPTER: dict[str, Any] = {
    "kind": SOURCE_KIND,
    "freeze": freeze,
    "load_scene": load_scene,
    "normalize_scene": normalize_scene,
    "verify_normalization": verify_normalization,
    "validate_config": validate_source_config,
    "scene_schema": SCENE_SCHEMA,
    "freeze_schema": FREEZE_SCHEMA,
    "evidence_classes": ("cad", "fixture", "imported"),
}

__all__ = [
    "ADAPTER",
    "FREEZE_SCHEMA",
    "SCENE_SCHEMA",
    "SOURCE_KIND",
    "freeze",
    "load_scene",
    "normalize_scene",
    "validate_source_config",
    "verify_normalization",
]
