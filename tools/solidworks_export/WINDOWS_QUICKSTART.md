# Windows 接入入口已迁移

使用公共工具包中的 [worker 部署流程](../../docs/sources/solidworks.md)。
旧 `deploy_windows.ps1` 和 `build_bundle.py` 明确拒绝执行，避免安装无法导出原生模型的旧服务。

迁移前停用旧 `swbridge` 登录任务。新 worker 使用 CPython 3.12、固定依赖和已登录桌面会话。
