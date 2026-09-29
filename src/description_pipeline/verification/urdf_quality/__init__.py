"""URDF 合同质检：与来源工具链无关的严格检查（结构、关节、惯性、几何、台账）。

入口是 ``tools/audit.py``；规则编号 ``URDF###``，说明见 ``docs/urdf_standard.md``。
本包零第三方依赖，只读模型与仓库台账，不做修复。
"""

SCHEMA = "mimicverse.urdf_quality/v1"
