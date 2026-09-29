"""旧第三方引擎通道：**已弃用**，不再执行任何导出。

主链的出模型路径现在是 ``description_pipeline``：``description source freeze`` 冻结来源快照，
``description build`` 调用来源包的 ``normalize_scene`` 与公共后端写出 URDF/MJCF。
这里只保留历史复现所需的配置写法与一个明确的拒绝入口，避免仓库里长期并行两套写实现。
"""

from __future__ import annotations

import json
from pathlib import Path

from .url import DocumentRef

ENGINE_PACKAGE = "onshape-to-robot"
DEPRECATED_MESSAGE = (
    "旧引擎导出通道已弃用：请改用 description_pipeline（description source freeze → "
    "description build，来源包 description_pipeline.sources.onshape.normalize_scene）；"
    "本入口不再调用 onshape-to-robot，也不写任何模型文件"
)

# 引擎自己的格式名与仓库目录名不同：仓库用 mjcf/，引擎叫 mujoco。
ENGINE_FORMATS = {"urdf": "urdf", "mjcf": "mujoco"}


class EngineError(RuntimeError):
    pass


class EngineDeprecated(EngineError):
    """旧引擎写通道被拒绝时抛出的稳定错误类型。"""


def write_config(
    robot_dir: Path,
    ref: DocumentRef,
    *,
    output_format: str,
    assembly_name: str | None = None,
    color: tuple[float, float, float, float] = (0.72, 0.72, 0.74, 1.0),
    ignore: dict[str, str] | None = None,
    configuration: str = "default",
    joint_properties: dict | None = None,
) -> Path:
    """写历史引擎配置，仅供复现旧产物；新流程不读这个文件。"""

    config: dict = {
        "url": ref.url(),
        "output_format": output_format,
        "color": list(color),
        "configuration": configuration,
    }
    if assembly_name:
        config["assembly_name"] = assembly_name
    if ignore:
        config["ignore"] = ignore
    if joint_properties:
        # 透传给引擎：关节属性（damping/armature/frictionloss…）与执行器类型
        config["joint_properties"] = joint_properties
    path = Path(robot_dir) / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    return path


def run(
    robot_dir: Path,
    *,
    offline_cache: Path | None = None,
    auto_dof: bool = False,
    density_overrides: dict | None = None,
) -> None:
    """明确的弃用入口：不导入引擎、不改 ``sys.argv``、不写文件。"""

    del robot_dir, offline_cache, auto_dof, density_overrides
    raise EngineDeprecated(DEPRECATED_MESSAGE)
