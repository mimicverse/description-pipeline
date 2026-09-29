"""历史兼容名：实现只在公共包里（``description_pipeline.sources.onshape.geometry``）。

拆 GLTF 的规则（顶点去重、体积/质心配对）改动只落一处，兼容入口不再持有实现。
见同目录 ``MIGRATION.md``。
"""

from description_pipeline.sources.onshape.geometry import GeometryError, load_gltf, split_gltf

__all__ = ["GeometryError", "load_gltf", "split_gltf"]
