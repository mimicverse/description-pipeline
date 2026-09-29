"""公共契约门禁：场景结构用共享 schema 校验，物理合理性留给规范化之后的层。"""

from __future__ import annotations

import json

from .errors import SCENE_INVALID, SHARED_HELPER_MISSING, OnshapeSourceError


def validate_scene(scene: dict) -> None:
    """用 ``description_pipeline.model/schema.json`` 校验场景结构。"""

    try:
        from importlib.resources import files

        import jsonschema

        schema_path = files("description_pipeline.model").joinpath("schema.json")
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
    except (ModuleNotFoundError, FileNotFoundError) as error:  # pragma: no cover - 集成后才有
        raise OnshapeSourceError(
            SHARED_HELPER_MISSING,
            "缺少公共模型 schema：description_pipeline/model/schema.json",
            {"expected": "description.scene/v1"},
        ) from error
    try:
        jsonschema.Draft202012Validator(schema).validate(scene)
    except jsonschema.ValidationError as error:
        raise OnshapeSourceError(
            SCENE_INVALID,
            f"场景不符合公共 schema：{list(error.absolute_path)}",
            {"message": error.message},
        ) from error
