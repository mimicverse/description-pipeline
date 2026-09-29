"""来源层稳定错误码：每个失败都指向具体对象，便于上层区分"采集失败"与"模型不合格"。"""

from __future__ import annotations

from typing import Any


class OnshapeSourceError(RuntimeError):
    """带稳定 ``code`` 的来源层异常；``detail`` 只放可公开的对象信息。"""

    def __init__(self, code: str, message: str, detail: dict[str, Any] | None = None) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.detail = dict(detail or {})


CREDENTIALS_MISSING = "onshape_credentials_missing"
REFERENCE_INVALID = "onshape_reference_invalid"
SOURCE_CONFIG_INVALID = "onshape_source_config_invalid"
DEFINITION_INVALID = "onshape_definition_invalid"
IDENTITY_COLLISION = "onshape_identity_collision"
ATTRIBUTION_MISMATCH = "onshape_attribution_mismatch"
EVIDENCE_MISSING = "onshape_evidence_missing"
API_ERROR = "onshape_api_error"
API_UNAVAILABLE = "onshape_api_unavailable"
CACHE_MISS = "onshape_cache_miss"
CACHE_IDENTITY_MISMATCH = "onshape_cache_identity_mismatch"
FOREIGN_DOCUMENT = "onshape_foreign_document"
DEPENDENCY_INCOMPLETE = "onshape_dependency_incomplete"
REVISION_MISMATCH = "onshape_revision_mismatch"
SNAPSHOT_INCOMPLETE = "onshape_snapshot_incomplete"
SNAPSHOT_TAMPERED = "onshape_snapshot_tampered"
SCENE_INVALID = "onshape_scene_invalid"
GEOMETRY_UNRESOLVED = "onshape_geometry_unresolved"
SHARED_HELPER_MISSING = "pipeline_shared_helper_missing"
