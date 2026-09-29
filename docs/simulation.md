# 仿真应用验收

[English](simulation.en.md) · 中文

`description model accept` 在实际 MuJoCo 中执行站姿保持和单关节正弦跟踪，
输出带摘要的验收记录与逐时刻遥测。它只验收声明的仿真工况，不授予训练或实机资格。

## 准备模型

先在模型工作区完成物理参数、碰撞体、接触参数、执行器和控制映射定义。
profile 可用 `validation_poses` 指定接触检查姿态，第一个姿态用于 reset；未声明时检查全部采样姿态。
完整关节范围的 FK 与动力学采样仍独立执行。

在 `config/simulation-acceptance.json` 声明实验，示例如下。示例名称和参数须按受测模型替换：

```json
{
  "schema": "description.simulation-acceptance/v1",
  "purpose": "simulation",
  "model": {"mjcf": "mjcf/scene.xml"},
  "timestep_s": 0.002,
  "control_period_s": 0.002,
  "initial_state": {"joints": {"joint": 0.0}},
  "pd": {"default": {"kp": 3.0, "kd": 0.05}, "joints": {}},
  "torque_limits": {"default": 0.3, "joints": {}},
  "tests": [{
    "id": "track-joint",
    "kind": "joint_sine",
    "joint": "joint",
    "amplitude_rad": 0.08,
    "frequency_hz": 0.4,
    "duration_s": 5.0,
    "thresholds": {
      "tracking_rmse_rad": 0.04,
      "max_tracking_error_rad": 0.08,
      "max_torque_nm": 0.3,
      "max_saturation_steps_ratio": 0.01,
      "penetration_tol_m": 0.001
    }
  }]
}
```

`initial_state.joints` 必须覆盖全部受控关节。浮动模型还需 `base.position` 和 `base.quaternion`（wxyz）。
当前消费者支持每轴一个 `motor`、`gear=1` 的转动关节；PD 参数与限幅使用 SI 单位。
控制周期必须是物理步长的整数倍。限幅须落在模型全部有效控制/力矩范围内。
这些实验参数不是厂家持续额定值，也不代替舵机辨识。

`hold` 测试可用 `support` 声明地面接触比例和最低基座高度；浮动测试可用 `max_base_tilt_deg` 限制倾倒。
正弦跟踪按被驱动关节评判，保持测试按最差关节评判；同时报告各关节误差与力矩饱和比例。
测试时长和阈值须在验收前固定，跟踪容限应能检出目标关节不运动。

`max_torque_nm` 检查限幅后实际施加的力矩；`max_saturation_steps_ratio` 限制任一关节发生
饱和的控制步占比（0–1），用于检出限幅掩盖的力矩需求。报告中的 `max_required_torque_nm` 仅供分析。

profile 的 `acceptance_suites` 与测试 ID 一一对应；`consumer_environment` 固定目标平台上的实际软件版本，
可声明 `mujoco`、`numpy`、`python`、`platform`、`controller`，其中控制器为 `pd-torque/v1`。

Windows 与 Linux 的 Python 或系统版本不同，就分别使用各自的锁定环境运行并记录验收；不能把一个平台的记录复制到另一个平台。

## 日常更新

完成上述定义后，运行 `description model update --root MODEL --profile simulation`。
命令完成采集、构建、实验、证据写入、重验和 PR 提交；只改定义时加 `--reuse-source`。
Windows 的 `submit-host.json` 设置 `profile: simulation` 后，`submit.ps1` 执行同一流程。
只有 `consumer.application` 待验收时才运行实验；其他构建失败先修复，实验失败不提交，也不覆盖已有证据。

## 单独运行与诊断

需要独立执行实验或查看中间候选时，先使用模型工具锁对应的工具构建 simulation 候选：

```sh
description build --root MODEL --profile simulation --report RESULT/build.json
```

首次构建因缺少应用验收记录而返回失败。仅当 `blockers` 为 `consumer.application` 时，
才对报告 `diagnostic_path` 指向的完整候选运行应用测试；其他失败先修复输入。

```sh
description model accept \
  --root CANDIDATE --profile simulation --out RESULT/acceptance
```

运行器先检查 MJCF 引用闭包，再独立重验候选，只加载已验算的 `mjcf/scene.xml`。
每个物理步检查状态、警告、关节范围和穿透；测试还检查跟踪、实际力矩及支撑条件。
结果写入全新目录，失败保留独立诊断和已采集遥测。输入模型保持只读。
遥测文件及摘要见 `acceptance.json`；压缩的 JSONL 可用 `gzip -dc` 读取。

## 接入验收结果

测试全部通过后，将生成的记录和遥测放回作者工作区，再重建：

```sh
python -c "import shutil; shutil.copytree('RESULT/acceptance/docs/acceptance', 'MODEL/docs/acceptance', dirs_exist_ok=True)"
description build --root MODEL --profile simulation
description check --root MODEL --profile simulation
```

这组命令在 Windows 和 Linux 相同。`MODEL` 是作者工作区，`CANDIDATE` 是首次构建报告中的
`diagnostic_path`；不要把结果写入只读候选。失败实验保留诊断，修正定义或实验条件后重新开始。

本地记录携带 `attestation.kind: local_replay`，它只选择验证方式，不构成可信声明。
每次 build、check 和发布验证都会重新执行完整实验，核对固定工具、输入身份、用途、实际环境、
测试条件、阈值、测量值及遥测摘要。记录必须引用 `config/simulation-acceptance.json`；
不能用同名测试替换成另一份实验配置。原始运行的时间、路径和 oracle 是诊断信息，当前资格由本次验证决定。

重放要求结果及压缩遥测逐字节一致。受测工具和运行环境以工具锁为准；不同系统、Python 或压缩库
可能产生差异，须在目标环境重新锁定、构建和验收，不能放宽比较或沿用旧记录。
每次验收最多 256 个实验、总计 100 万物理步和 25 万遥测行；超限时调整实验设计。

通过后提交模型 PR，合入 feature，再运行 `description model promote --profile simulation`。
发布仍从远端重新取得精确候选并独立验证。仿真通过仅覆盖声明工况，不证明实物参数已校准，
也不授予训练或实机资格；这些用途仍需各自的独立证据。

## 可选托管验收

GitHub Actions 当前停用，不参与仿真资格。若重新启用，`simulation-acceptance.yml` 对精确模型 SHA
运行相同实验；兼容脚本 `tools/run_simulation_acceptance.py --external` 生成外部凭证材料。

将成功工件中的记录与日志放入 `docs/acceptance/`，在 `simulation.json` 增加
`attestation.repository`、`run_id`、`artifact_id`。公共工具核对已登记的执行方、运行身份、工件和
日志摘要，再对同一 subject 重建。外部记录与本地重放走各自的验证路径；自行填写 `passed` 均无效。
