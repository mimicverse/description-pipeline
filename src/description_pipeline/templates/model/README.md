# Robot model: $hardware

维护 `config/robot.yaml` 中的机器人定义、`config/profiles/` 中的用途配置，以及 `sources/` 中的来源证据。

依次执行 `description source freeze`、`description build`、`description check`；检查通过后提交 PR。合入 feature 后，用 `description model promote` 从远端重新取得并验收精确候选，再发布。每条命令用 `--root` 指定工作区。

完成首次建模后，在本目录运行 `description model update` 可一次完成采集、构建和 PR 更新。
Linux 采集 SolidWorks 时加 `--worker-host SSH_ALIAS`；只改定义或证据时加 `--reuse-source`，校验并复用已有快照。

模型入口为 `urdf/robot.urdf`、`mjcf/robot.xml` 和 `mjcf/scene.xml`。使用端固定提交 SHA，并核对报告允许的用途与环境。

初始工作区只有定义，尚无可使用的模型。修正输入或工具后重建，工程决策记录在 `docs/decisions.md`。
