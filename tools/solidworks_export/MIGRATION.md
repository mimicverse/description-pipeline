# 迁移到公共流水线

真实 SolidWorks 的旧 `run_export` / `/v1/export` 写通道已拒绝执行。
原生 COM 和错误类型已变为公共包的薄转发，不再维护第二份原生取数实现。
旧数据读取、package check 和 synthetic backend 保留用于历史回归；它们不是生产发布入口。

生产路径统一为：

1. 在 Windows 交互式会话部署 [worker](../../src/description_pipeline/sources/solidworks/deploy/worker.ps1)。
2. 配置 `config/robot.yaml` 的 `source.provider: solidworks` 和来源/分组/关节依据。
3. Linux 通过回环 SSH 隧道的 `source.worker_url` 执行 `description source freeze --root MODEL`。
4. 执行 `description build`、`description check` 和统一 Git 提交/晋级流程。

命令和字段见 [当前来源说明](../../docs/sources/solidworks.md)，数据合同见
[工程契约](../../docs/pipeline.md)。Windows 需要 Python 3.12 和固定 worker 依赖；旧包的 Python 3.8 承诺不适用于新版本。
