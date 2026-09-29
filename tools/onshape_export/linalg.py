"""历史兼容名：实现只在公共包里（``description_pipeline.sources.onshape.linalg``）。

旧包曾经自留一份拷贝（与公共包逐字节相同，改一处就要同步改两处）；现在只做转发。
见同目录 ``MIGRATION.md``。
"""

from description_pipeline.sources.onshape.linalg import (
    Matrix,
    Vector,
    angle_deg,
    axis,
    distance,
    from_axes,
    from_flat,
    identity,
    matmul,
    rotation_difference_deg,
    rotation_rpy,
    translation,
)

__all__ = [
    "Matrix",
    "Vector",
    "angle_deg",
    "axis",
    "distance",
    "from_axes",
    "from_flat",
    "identity",
    "matmul",
    "rotation_difference_deg",
    "rotation_rpy",
    "translation",
]
