"""Onshape → URDF/MJCF 导出工具（**写通道已弃用**，只保留只读与历史回归）。

出模型请走 `description_pipeline`：`description source freeze`（冻结来源快照）→
`description build`（来源包 `normalize_scene` + 公共后端写出 URDF/MJCF）。
本包的 `export` 子命令与 ``engine.run`` 现在明确拒绝执行，不再调用
`onshape-to-robot`，也不再写任何模型文件；`check` / `fetch` / `fetch-geometry` /
`verify` 仍可用于读取 API、抓取缓存与复现历史产物。

模块划分：

* ``url``      —— Onshape 引用（文档/工作区/版本/元素）解析
* ``api``      —— HMAC 签名 REST 客户端 + 本地响应缓存
* ``assembly`` —— 装配体数据模型：实例、mate、关节树
* ``checks``   —— 导出前诊断（未解算 mate、重复 mate、树结构、材质、限位）
* ``engine``   —— 旧引擎通道：只保留配置写法与明确的弃用拒绝
* ``layout``   —— 归一化为本仓库固定入口（urdf/ + meshes/ + mjcf/）
* ``verify``   —— 导出后独立核验（URDF/MJCF 结构、质量、限位、mesh 放置）
"""

SCHEMA_VERSION = "mimicverse.onshape_export/v1"

__all__ = ["SCHEMA_VERSION"]
