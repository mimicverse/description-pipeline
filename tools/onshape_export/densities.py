"""材料密度覆盖：把"分类等效密度"这种外部结论写进导出，而不改源文档数据。

为什么需要：CAD 几何是真的，但材质常常缺失（STEP 导入）或需要按类别近似
（例如打印件按 PLA + 15% 填充的等效密度、钢垫片按钢）。把结论记成一个 JSON 文件、
随模型一起提交，导出即可复现，来源与推导过程可审计；源文档保持原样。

文件格式（两种键都支持，partId 优先）::

    {
      "PLA 15% infill (equivalent)": {"density_kg_m3": 957, "note": "由官方整机质量反推"},
      "parts": {"KFTB": 957, "JFT": 7850},
      "names": {"steel_shim": 7850, "pom_shim": 1410}
    }
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path


class DensityError(ValueError):
    pass


DOC_KEYS = {"source", "note", "notes", "description", "derivation", "date", "license"}


def load_overrides(path: Path) -> tuple[dict[str, float], dict[str, float], dict]:
    """返回 ``(partId 覆盖, 零件名覆盖, 原始文档)``。"""

    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    by_part = {str(key): float(value) for key, value in (payload.get("parts") or {}).items()}
    by_name = {str(key): float(value) for key, value in (payload.get("names") or {}).items()}
    for key, value in payload.items():
        if key in {"parts", "names"} or key in DOC_KEYS:
            continue
        if isinstance(value, dict):
            pass  # 只是文档性的分组说明，密度仍需写在 parts/names 里
        else:
            raise DensityError(f"未知条目：{key}（密度请写在 parts / names 下）")
    for density in list(by_part.values()) + list(by_name.values()):
        if not 10.0 <= density <= 25000.0:
            raise DensityError(f"密度超出合理范围：{density} kg/m³")
    return by_part, by_name, payload


def rescale_body(body: dict, density: float) -> dict:
    """把一个零件的质量属性按新密度缩放（质量 = 体积×密度；惯量与密度成正比）。"""

    volume = float((body.get("volume") or [0])[0])
    if volume <= 0:
        raise DensityError("没有体积的零件不能按密度覆盖")
    mass = float((body.get("mass") or [0])[0])
    old_density = mass / volume if mass > 0 else 0.0
    factor = density / old_density if old_density > 0 else None
    if factor is None:
        raise DensityError("零件没有原始质量，无法推断缩放系数；请先在文档里赋材质")
    scaled = json.loads(json.dumps(body))
    if isinstance(scaled.get("mass"), list):
        scaled["mass"] = [value * factor for value in scaled["mass"]]
    if isinstance(scaled.get("inertia"), list):
        scaled["inertia"] = [value * factor for value in scaled["inertia"]]
    if isinstance(scaled.get("principalInertia"), list):
        scaled["principalInertia"] = [value * factor for value in scaled["principalInertia"]]
    scaled["hasMass"] = True
    scaled["densityKgM3"] = density
    return scaled


def apply_overrides(
    bodies_by_element: dict[str, dict],
    by_part: dict[str, float],
    by_name: dict[str, float],
    names: dict[str, str] | None = None,
) -> tuple[dict[str, dict], list[dict]]:
    """按 partId 或零件名把覆盖密度应用到每个工作室的逐体质量属性上。"""

    names = names or {}
    applied: list[dict] = []
    result: dict[str, dict] = {}
    hit: set[str] = set()
    matched_names: Counter[str] = Counter()
    for element_id, bodies in bodies_by_element.items():
        rewritten = {}
        for part_id, body in bodies.items():
            density = by_part.get(part_id)
            source = "partId"
            if density is None:
                base = names.get(part_id, "")
                for candidate in (base, base.split("__")[0]):
                    if candidate and candidate in by_name:
                        density = by_name[candidate]
                        matched_names[candidate] += 1
                        source = f"name:{candidate}"
                        break
            if density is None or not body.get("volume"):
                rewritten[part_id] = body
                continue
            rewritten[part_id] = rescale_body(body, density)
            hit.add(part_id)
            applied.append(
                {
                    "part_id": part_id,
                    "name": names.get(part_id, ""),
                    "density_kg_m3": density,
                    "source": source,
                    "mass_before_kg": float((body.get("mass") or [0])[0]),
                    "mass_after_kg": float(rewritten[part_id]["mass"][0]),
                }
            )
        result[element_id] = rewritten
    for part_id in by_part:
        if part_id not in hit:
            raise DensityError(f"覆盖里的 partId {part_id} 不在装配体中")
    for name in by_name:
        if matched_names[name] == 0:
            raise DensityError(f"覆盖里的零件名 {name} 不在装配体中")
    return result, applied


def part_names_from_assembly(assembly: dict) -> dict[str, str]:
    """从装配体响应里取 partId → 零件名（去掉实例的 ``<n>`` 后缀）。"""

    names: dict[str, str] = {}
    entries = list(assembly.get("rootAssembly", {}).get("instances", []) or [])
    for sub in assembly.get("subAssemblies", []) or []:
        entries += list(sub.get("instances", []) or [])
    for item in entries:
        part_id = item.get("partId")
        if part_id and part_id not in names:
            names[part_id] = "<".join(item.get("name", "").split("<")[:-1]).strip()
    return names
