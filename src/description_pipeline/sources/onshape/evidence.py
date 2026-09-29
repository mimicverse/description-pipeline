"""Bind exclusion evidence to exact source identities, never substring matches."""

from pathlib import Path, PurePosixPath

from ...io import PipelineError, confined, read_data
from .collector import safe_name

NOT_CAPTURE_LAYER = "evidence_not_capture_layer"
NOT_BOUND = "evidence_not_bound_to_entity"
UNREADABLE = "evidence_file_unreadable"

MESSAGES = {
    NOT_CAPTURE_LAYER: "排除项证据必须来自 raw/ 或对应的 geometry/parts/ 文件",
    NOT_BOUND: "排除项证据未绑定该实例或该零件工作室中的零件",
    UNREADABLE: "排除项证据无法读取",
}


def _mentions(value, entity: str, part_id: str, element_id: str) -> bool:
    if isinstance(value, dict):
        if part_id and element_id and (value.get("partId"), value.get("elementId")) == (part_id, element_id):
            return True
        return any(key == entity or _mentions(child, entity, part_id, element_id) for key, child in value.items())
    if isinstance(value, list):
        return value == entity.split("/") or any(_mentions(child, entity, part_id, element_id) for child in value)
    return isinstance(value, str) and value == entity


def evidence_problems(root: Path, relative: str, entity: str, part_id: str, element_id: str) -> list[str]:
    path = PurePosixPath(relative)
    if path.parts and path.parts[0] == "geometry":
        return [] if part_id and relative == f"geometry/parts/{safe_name(part_id)}.stl" else [NOT_BOUND]
    if not path.parts or path.parts[0] != "raw" or path.suffix != ".json":
        return [NOT_CAPTURE_LAYER]
    try:
        payload = read_data(confined(root, relative))
    except (OSError, PipelineError):
        return [UNREADABLE]
    if (
        part_id
        and element_id
        and relative == f"raw/mass_properties_{element_id}.json"
        and isinstance(payload, dict)
        and part_id in (payload.get("bodies") or {})
    ):
        return []
    return [] if _mentions(payload, entity, part_id, element_id) else [NOT_BOUND]


def messages(problems: list[str]) -> str:
    return "；".join(MESSAGES.get(code, code) for code in problems)
