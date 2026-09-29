# Onshape 旧工具迁移

生产采集与构建统一使用 [Onshape 来源适配器](sources/onshape.md)和[运行手册](pipeline.md)。
旧 `export` 命令和 `engine.run` 已停用，不再安装或调用 `onshape-to-robot`。

`tools/onshape_to_urdf.py` 保留 `check`、`fetch`、`fetch-geometry`、`verify`，用于读取既有资料和历史回归。
这些检查不授予当前模型发布资格。旧缓存可作为回放输入；缺失的逐请求修订证据不能由回放补齐。

[迁移映射](../tools/onshape_export/MIGRATION.md)
