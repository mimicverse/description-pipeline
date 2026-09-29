# SolidWorks 旧工具迁移

生产采集与构建统一使用 [Windows worker](sources/solidworks.md)和[运行手册](pipeline.md)。
旧原生 `run_export`、`/v1/export`、部署脚本及独立打包入口已停用。

`tools/solidworks_export` 仅保留历史包读取、配置校验和 synthetic 回归。原生 COM 层转发到公共包，
不再单独安装。历史 synthetic 导出用于检验变换、惯量和错误处理，不能作为 CAD 证据。

若机器上仍有旧 `swbridge` 登录任务，迁移时应由操作者停用该任务，再部署新 worker。
不要同时运行两套采集服务，也不要自动结束操作者的 SolidWorks 进程。

[迁移步骤](../tools/solidworks_export/MIGRATION.md)
