# 旧导出工具 → description_pipeline 迁移

> 状态：**已迁移**。旧引擎写通道（`export` 子命令 + `engine.run`）现在明确拒绝执行，
> 主链只有一套写实现：`description_pipeline`。

## 命令映射

| 旧入口 | 新入口 | 说明 |
|---|---|---|
| `check` | `description check --root <模型仓库>` / 保留旧命令 | 只读诊断；旧命令仍可读 API 与缓存 |
| `fetch`、`fetch-geometry` | 保留 | 只读 API 抓取，缓存布局（`json/` + `bytes/`）与来源包完全兼容 |
| `export` | `description source freeze` → `description build` | 冻结快照 + 规范化 + 公共后端写出 URDF/MJCF |
| `verify`（导出后核验） | `description check` | 重新执行来源、物理语义、消费者及完整性交验；旧 `OSV###`/`OSX###` 与 `URDF###` 并非等价映射，不能沿用旧资格 |
| `engine.run` / 离线引擎通道 | `description_pipeline.sources.onshape.normalize_scene` | 不再 patch 第三方模块全局状态，也不再改 `sys.argv` |

## 行为契约

* `tools/onshape_to_urdf.py export ...` → 退出码 **3**（`EXIT_DEPRECATED`），
  不发起 API 调用、不导入引擎、不创建输出目录。
* `onshape_export.engine.run(...)` → 抛 `EngineDeprecated`（`EngineError` 子类），
  信息里给出 `description source freeze` / `description build` / 来源包模块路径。
* 保留模块：`api`、`url`、`assembly`、`cache`、`checks`、`verify`、`layout`、`geometry`、
  `densities`、`contacts`、`offline`——用于读取与历史回归（`tests/test_onshape_*.py`）。
  其中 `linalg` 与 `geometry` 只是转发：实现只在公共包
  `src/description_pipeline/sources/onshape/` 里，改一处即可（此前两份逐字节相同的拷贝已删除）。
* 保留独立历史回归以检测语义退化；完成模型迁移后再评估旧格式读取入口是否仍有使用方。

## 缓存兼容

旧缓存目录（`json/assembly_*.json`、`bytes/stl_<safe(partId)>.stl`）可以直接作为来源包的
`config.source.cache` 使用，离线重放不需要重新抓取；`source.json` 里的 `capture` 记录会被
来源包读入证据链（缺失时按 `imported` 记录并给出 gap）。
