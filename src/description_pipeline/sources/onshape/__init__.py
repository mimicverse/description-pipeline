"""Onshape 来源适配器：采集/冻结、离线重放与按显式定义的语义规范化。

from description_pipeline.sources.onshape import freeze, load_scene
manifest = freeze(config, destination)      # 采集并冻结快照
scene = load_scene(snapshot_root)           # 校验并重放
model = normalize_scene(scene, definition, snapshot_root)   # 按定义分组并闭合运动链
"""

from .client import OnshapeClient, load_credentials
from .definition import RobotDefinition, parse_robot_definition
from .errors import OnshapeSourceError
from .freeze import __version__, freeze, load_scene
from .normalize import normalize_scene
from .reference import DocumentRef, parse_reference
from .verify import verify_normalization

__all__ = [
    "DocumentRef",
    "OnshapeClient",
    "OnshapeSourceError",
    "RobotDefinition",
    "__version__",
    "freeze",
    "load_credentials",
    "load_scene",
    "normalize_scene",
    "parse_reference",
    "parse_robot_definition",
    "verify_normalization",
]
