# 机械运动学验收

[English](mechanical-acceptance.en.md) · 中文

本流程将实际 URDF、MuJoCo 机器人和场景与独立采集的参考比较。通过只表示**相对于该参考的运动学合格**，
不代表物理标定、执行器性能、接触动力学、训练或实机安全合格。

## 1. 完成机械定义

在 `config/robot.yaml` 中确定刚体划分、关节身份、有符号轴线、零位、位置限制与耦合。
在 `interfaces.mechanical_drives` 中按**规范关节名称**声明每个可动关节的机械驱动，不使用来源 id：

```yaml
interfaces:
  mechanical_drives:
    shoulder_joint:
      kind: active
      id: shoulder-motor-1
      stator: ["assembly/motor-case-1"]
      rotor: ["assembly/output-shaft-1"]
    passive_joint:
      kind: passive
```

列表使用冻结 CAD 的精确实例标识。定子属于关节父刚体，转子属于子刚体；驱动身份唯一，两个实例集合互斥。
这是机械身份与安装关系，不提供仿真执行器、额定力矩或控制参数。这些参数仍由各自输入定义。
映射派生到 `model/robot.json`，验证时从作者输入重新推导。

在 `config/profiles/kinematics.json` 的 `acceptance_suites` 中声明一个机械验收套件，
在 `consumer_environment` 中固定目标环境及实际 MuJoCo 版本，并选择适合机构的误差容限。
本流程支持固定、旋转、连续旋转、平移与 mimic 关节；后端不支持的闭环约束阻断生成。

## 2. 获取并批准独立参考

设计负责人记录批准的机械映射、零位和独立采集的留出运动观测，在参考的 `conditions` 及配套工程证据中
保留采集方式、具名 CAD 坐标系及转换、采集人、日期、CAD 配置和批准依据。

参考存放在**所有模型工作区之外**的已批准证据库，随评审提供 SHA-256。操作者显式选择文件，候选不能自行指定验收权威。
参考的来源和批准由操作者负责；软件验证与所选字节的一致性，不能证明文件确实独立采集或获批。
将生成模型的变换复制到外部文件不构成独立证据。参与过模型拟合的观测不能再次作为验收数据，必须重新取得留出观测。

UTF-8 JSON 的 `schema_version` 为 `description.mechanical-reference/v1`，字段如下：

| 字段 | 必需内容 |
|---|---|
| `hardware_id`、`source_manifest_digest` | 精确硬件标识及 `sources/source.lock.json` 的 `manifest_digest`。 |
| `suite`、`environment` | profile 唯一的验收套件及完整 `consumer_environment`。 |
| `evidence_class`、`data_role`、`used_for_fitting` | 与快照一致的 `cad` 或 `fixture`；`validation`；`false`。 |
| `conditions` | 非空的采集与批准依据，包括从具名 CAD 坐标系独立转换的记录。 |
| `conventions` | `{"units":"SI","joint_origin":"parent_link","joint_axis":"joint","poses":"world"}`。 |
| `tolerances` | 有限正数 `position_m`、`rotation_rad`；比较使用参考与 profile 中更严格的容限。 |
| `ownership` | 每个 URDF 刚体名称对应其完整 CAD 实例集合；派生参考坐标系的集合为空。 |
| `joints` | 每个 URDF 关节，含派生坐标系的固定关节，声明 `type`、`parent`、`child`、`origin`、`axis`、`limits`、`mimic`。 |
| `drives` | 每个可动关节对应批准的主动或被动机械驱动定义。 |
| `constraints` | 完整批准约束；当前支持的后端要求为空。 |
| `zero_pose`、`poses` | 零位观测名称及 2–512 个名称唯一、覆盖全部关节与刚体的观测。 |

`origin` 是零位时从父 link 到关节及子 link 坐标系的 4×4 变换。
`axis` 是**关节坐标系下的有符号单位向量**，固定关节为 `null`。
`limits` 包含 `lower`、`upper`，固定与连续旋转关节为 `null`。
`mimic` 为 `null`，或包含 `joint`、`multiplier`、`offset`。
设计负责人须将具名 CAD 坐标系中的轴线独立转换到这些坐标系，流水线不推断转换。

每个姿态包含 `name`、`joints`、`base`、`links`。关节值使用弧度或米；`base` 和 `links` 中
每个刚体均为 4×4 世界变换。固定根模型以根 link 坐标系为世界坐标系，`base` 为单位矩阵。
零位中所有独立关节为零，从属关节遵循批准耦合。
每个独立关节的留出运动跨度须大于 `max(10 × 有效容限, 1e-6)`。结论仅覆盖实测样本和条件，不保证连续工作空间。
重复键、非有限数值、覆盖不全和复用拟合文件均被拒绝。参考不超过 32 MiB 和 100,000 个姿态／刚体观测。

## 3. 构建、验收与提交

冻结已保存 CAD 后，在模型锁定环境中完成全流程。只修改定义或证据时使用 `--reuse-source`：

```sh
description model update --root MODEL --profile kinematics --reuse-source \
  --mechanical-reference /approved/mechanism.json --message "Update mechanical definition"
```

命令先构建候选；仅缺应用验收时，对保留候选运行重放，复制成功记录，重新构建后提交 PR。
其他失败阻断流程。失败观测和执行诊断保存在 `build/`，不替换现有验收证据或交付产物。

Linux 的 `submit.sh` 原样转发选项。Windows 使用
`submit.ps1 -MechanicalReference C:\approved\mechanism.json`，或在 `submit-host.json` 中配置
`mechanical_reference`。可选远程构建时，参考路径属于**构建主机**，脚本不传输参考。
SolidWorks 原生采集仍需 Windows，冻结快照可在任一平台构建与验收。

需要在提交前单独检查验收时，先构建；若只缺应用验收，以报告的 `diagnostic_path` 作为 `CANDIDATE`：

```sh
description build --root MODEL --profile kinematics
description model accept --root CANDIDATE --profile kinematics \
  --mechanical-reference /approved/mechanism.json --out /results/new-run
```

`--out` 必须不存在且位于 `CANDIDATE` 外。检查 `acceptance.json` 和
`docs/acceptance/mechanical-observations.json`。成功后将结果的 `docs/acceptance/` 文件复制到
模型工作区的 `docs/acceptance/`，带 `--mechanical-reference` 重建。用 `description model submit` 提交已合格模型时，也传入该选项。

## 4. 验证、发布与消费

每个消费者重新显式选择已批准参考，记录本身不能使交付合格：

```sh
description check --root MODEL --profile kinematics \
  --mechanical-reference /consumer/approved/mechanism.json
description model validate --root REPOSITORY --candidate FULL_SHA --profile kinematics --remote \
  --mechanical-reference /consumer/approved/mechanism.json
description model promote --root REPOSITORY --hardware HARDWARE --candidate FULL_SHA --profile kinematics \
  --mechanical-reference /consumer/approved/mechanism.json
```

验证获取精确提交并重放观测。晋级另要求原生 CAD 证据、已接受的固定工具提交及正常评审；
检查计划后再加 `--apply`。计划绑定参考摘要，发布前再次验证。运动学发布仍只具有该用途资格，
其他用途必须各自验收；无需 GitHub Actions。

重放绑定输入身份、profile、环境、工具、运行时、参考摘要与观测文件。source lock、bundle manifest
与质量报告携带工作流 `pipeline_id`，校验会将其与定义和冻结来源比对；单次执行的 `run_id` 只属于执行本身，
绝不进入 subject、manifest、质量报告或验收记录。
未选择参考为 `not_run`；字节改变或观测不一致则失败。原生运行时被阻止意味着未执行，不授予资格。
修正输入或取得获准运行时后，重新构建与验收，不能通过修改生成文件或放宽参考来获取通过。
