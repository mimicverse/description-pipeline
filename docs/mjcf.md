# URDF → MJCF

本模块消费已经交付的 URDF 与本地网格，生成可携带、可复现、经过独立检查的 MuJoCo 机器人和可选基础场景。它不改变 SolidWorks → URDF 的六阶段契约，不修改上游证据或赋予训练、控制与实机资格。

## 架构与唯一真值

```text
URDF + 引用的 STL ─┐
                  ├─ 冻结输入 → 精确 XML 投影 → MuJoCo 编译
仿真配置 JSON ────┘                              ↓
                         独立读取原 URDF/配置 ← 实际 MJCF 重载
                                                ↓
                                  模型、场景、来源清单、检查报告
```

URDF 是拓扑、关节原点/有向轴/范围、质量、COM、完整惯量和原始几何的唯一来源；上游 CAD 及其证据链仍是这些工程事实的最终来源。本模块不读取另一份规范化机器人 JSON。仿真配置只拥有基座模式、碰撞用途、接触参数、场景和附加标记坐标，不得覆盖机械属性。MJCF 和报告均为可删除重建的派生物。

| 模块 | 唯一职责 |
|---|---|
| `mjcf_contract.py` | 仿真配置、封存清单与数值检查常量；无机械推导 |
| `mjcf.py` | 冻结引用闭包、从 URDF 直接投影 XML、调用完整检查、提交新输出目录 |
| `verification/mjcf.py` | 独立解析冻结 URDF，重载实际 MJCF，数值、配置、资产及来源检查 |
| 现有 `cli.py` | `description mjcf build/check` 的参数与结果呈现 |

不新增通用机器人类、第二份完整模型、插件注册器、任务数据库、调度器或发布器。URDF 树只在一次调用的内存中解析。共用现有规范数值格式、摘要、路径边界和 MuJoCo 版本锁。

直接投影 XML 是必要的有限复杂度：锁定 MuJoCo 的原生 XML 导出会把部分数值缩减至约 6 位有效数字（实测 `MjSpec.to_xml()` 相对误差 7.1e-8）；质量、惯量和坐标保存重载后不能维持本模块的精度要求。投影使用现有可往返 binary64 的数值格式。

三条投影规则由在锁定 MuJoCo 上的实测确定，均有回归锁定：

- 根元素带 `<compiler angle="radian" inertiafromgeom="false" balanceinertia="false"/>`。MJCF 角度默认是度、URDF 是弧度，缺省会让限位与旋转静默错 57.3 倍；`inertiafromgeom` 缺省为 `auto` 会按几何体积臆造质量，与"不推定密度"冲突；`balanceinertia` 保持关闭，避免静默改写交付的物理量。
- URDF `rpy` 一律转成显式 `quat`（`Rz·Ry·Rx`）。MuJoCo 的 `euler` 与 `mju_euler2Quat` 的约定与 URDF 不同（同角序列下相差 8.8°），而 MuJoCo 自己的 URDF 导入器与 `Rz·Ry·Rx` 完全一致。
- 惯量写为 `diaginertia` 加 `quat`：投影侧用自身双精度对称特征分解，把 link 系完整张量无损重参数化到主轴系。不丢弃非对角项、不改质量、不修正物理量。若改写 `fullinertia`，MuJoCo 会用 `mju_eig3` 再分解，特征向量精度约 2.4e-8，编译模型还原后相对误差可达 1.2e-6，与 `INERTIA_REL` 不相容；自行分解后同一门残差约 1e-15。二进制 STL 的 `mesh_vert` 是 float32 且已按 MuJoCo 规则居中，世界顶点比较必须补偿 `mesh_pos`，并采用 `MESH_ABS`/`MESH_REL`。

MuJoCo 负责物理编译，独立检查使用编译后模型还原张量。

## 使用与配置

使用仓库锁定的 Linux / CPython 3.12 / MuJoCo 环境，沿用开发指南的安装方式。

```sh
description mjcf build /path/to/source --urdf urdf/robot.urdf \
  --config /path/to/simulation.json --output /path/to/new-mjcf
description mjcf check /path/to/new-mjcf
```

输入根目录必须包含 URDF 与它引用的全部网格；`--urdf` 是根目录内的相对路径。允许 URDF 通过 `../meshes/` 引用根内资源。拒绝绝对网格路径、远端/`package://` URI、符号链接、路径逃逸、重复或大小写冲突路径。源文件在冻结前后核对摘要，不改写源目录。

配置只支持如下字段；未知键、重复 JSON 键、非有限数字和未知枚举均拒绝。所有影响物理行为的配置都显式提供，不按机器人名称套用默认参数。

```json
{
  "schema_version": 1,
  "base": "floating",
  "collision": {
    "source": "visual_convex",
    "self_collision": true,
    "friction": [0.8, 0.005, 0.0001]
  },
  "simulation": {
    "timestep": 0.001,
    "gravity": [0, 0, -9.81],
    "integrator": "implicitfast"
  },
  "sites": [],
  "floor": {"height": 0, "friction": [0.8, 0.005, 0.0001]}
}
```

`base` 为 `fixed` 或 `floating`；浮动模式只给根刚体添加自由关节，保留根惯量。机器人零位与 URDF 根坐标一致，不因添加地面自动抬升或修改零姿态。

碰撞来源为 `urdf`、`visual_convex` 或 `none`。`urdf` 只使用原始 collision；`visual_convex` 明确授权将每个 visual 作为独立接触形状，网格由 MuJoCo 使用其凸包；`none` 禁用机器人接触。视觉几何始终不参与接触或惯量推定。不存在自动合并、简化、补碰撞体或重新估计惯量。报告列出实际碰撞数量与来源；无碰撞不能获得接触建模资格。

`self_collision: false` 通过接触位掩码禁用机器人内部接触，仍允许地面接触；`true` 使用 MuJoCo 的标准自碰撞规则，包括其相邻刚体过滤。摩擦依次是滑动、扭转和滚动系数，必须非负，接触维数固定为 3。凸包不是原始凹表面的精确接触表示；使用者需独立验证其对目标任务的适用性。

`sites` 为显式标记列表，每项为 `{"name":"left_sole","link":"left_foot_link","xyz":[0,0,0],"rpy":[0,0,0]}`。位置和姿态相对于指定 URDF link，米/弧度；名称必须唯一，link 必须存在。可表达倾斜足底坐标系，但不按左右脚名称猜测支撑面，不将网格启发式结果冒充作者坐标。site 只是坐标标记，不是传感器。

`floor` 可省略，省略时只交付机器人模型；提供时额外生成 `scene.xml`，包含机器人、指定高度的水平地面和一盏灯。地面高度属于场景配置，不改动机器人。`sites` 可省略，等价于空列表。除此之外，示例中所有字段均必填。没有驱动器、控制器、armature、训练奖励或传感器的隐式生成。

## 支持边界

| 输入 | 行为 |
|---|---|
| 单一根、连通、无环 URDF 树 | 保留全部 link 与关节身份；固定 link 不融合 |
| fixed / revolute / continuous / prismatic | 分别映射固定连接 / 有限 hinge / 无限 hinge / 有限 slide |
| 非零有向轴、有限且严格递增的有界范围 | 保留轴向、原点、上下限；拒绝零轴、非单位轴和无效范围 |
| inertial 的任意旋转和非对角惯量 | COM 保留，完整张量旋转到 link 坐标后写入 `fullinertia`；禁止调质量、对角化丢项或自动修复 |
| 正质量与满足物理条件的完整惯量 | 可动刚体必须具备；无惯量的固定坐标 link 保持为无质量 frame，不推定密度 |
| visual/collision 的 box、sphere、cylinder、二进制 STL | 保留坐标、尺度、颜色；单位为 SI；STL 保留原始字节与引用关系 |
| URDF damping / friction | 映射关节 damping / frictionloss；不存在时采用 URDF 的零值语义 |
| effort / velocity | 保留在冻结 URDF 与报告中；没有 actuator，不声称实施速度或驱动力限幅；零值与缺失分别报告 |
| mimic、闭环、多自由度、transmission、插件、外部实体、未知扩展 | 拒绝并定位，不静默忽略或近似为固定连接 |

相同网格的多次实例化保留各实例变换，文件只封存一次。MuJoCo 的网格重心/主轴预处理不改变实例的实际世界几何；检查以补偿后的世界顶点比较。超过锁定消费者所支持限制、网格非有限/空/损坏、负尺度、非物理惯量、未知材质引用都明确失败。

## 交付与可复现性

```text
input/<原根内路径>          # 原始 URDF 及实际引用的网格字节，仅一份
simulation.json            # 原始仿真配置字节
robot.xml                  # 可独立加载的机器人
scene.xml                  # 仅在指定 floor 时生成
manifest.json              # 输入身份、输出文件清单、工具与锁定引擎身份
reports/quality.json       # 独立重算的确定性报告
```

清单覆盖以上除自身和报告外的全部文件；报告绑定清单主体，不形成循环摘要。路径、长度、SHA-256 与实际文件集合必须一致，额外文件也失败。来源只以根内相对路径表示；时间、随机 ID、主机名、临时路径与运行日志不进入确定性产物。两处不同绝对目录、相同输入/配置/工具/引擎必须生成逐字节相同的交付。

只允许生成到新目录，禁止覆盖既有输出或写回源目录。先在独立临时目录完成编译及全量独立检查，再提交结果；所有退出路径清理本次临时文件。检查命令只读，检查前后核对主体，独立重算报告并与已保存报告比较。文件、工具、配置或引擎改变后需重新构建。

## 独立验收

验证器不导入转换器，不读取其内存模型或把两个派生文件互相一致当作真值。它从冻结的原始 URDF、网格和配置重建预期；在新进程中使用锁定 MuJoCo 加载实际 `robot.xml` 和可选 `scene.xml`。每个失败包含检查名与对象，不以成功打开文件代替物理验证。

| 检查 | 必需内容 |
|---|---|
| 身份与闭包 | 清单、输入、配置、XML、实际网格、引擎版本与报告绑定；未登记文件拒绝 |
| 拓扑与自由度 | link/joint 集合、父子关系、固定 frame、关节类型；浮动基座恰增加 7 个位置量和 6 个速度量 |
| 运动学 | 零位和确定的非零单关节/组合样本；独立 URDF FK 对比实际 MuJoCo link 世界位姿与有向关节轴 |
| 惯性 | 逐 link 质量、COM 和由主轴还原的完整张量；整机质量、COM、平行轴合成张量；编码必须让还原残差落在 `INERTIA_REL`/`INERTIA_ABS` 内，不得依赖 MuJoCo 对 `fullinertia` 的再分解 |
| 几何 | primitive 尺寸、实例变换、STL 顶点尺度和编译网格变换补偿；视觉/碰撞角色及数量 |
| 关节与配置 | 上下限与 `quat` 逐位等于 URDF 弧度值、`compiler/@angle` 为 `radian`、连续关节无限位、damping/frictionloss、无隐式 actuator、基座、site 与接触位掩码 |
| 场景 | include 的实际机器人、地面高度/法向/摩擦、照明、求解器参数；无自动零位漂移 |
| 运行 | 零状态 forward 与短时零驱动步进无 NaN/Inf；不以短时稳定替代站立、控制或训练资格 |
| 可复现性 | 不同绝对路径的双构建字节相同；保存/重新加载后仍符合全部数值门 |

纯编码/姿态转换比较采用位置、旋转、COM 绝对 `1e-10`，质量与完整张量相对 `1e-10` 加绝对 `1e-12`；STL 为 float32，世界顶点比较采用绝对 `1e-6 m` 加相对 `1e-6`。这些是表示转换精度，不取代 CAD 或物理实测容差。数值常量在 `mjcf_contract.py` 只定义一次，报告记录实际最大残差。

回归必须包含非对角惯量、旋转惯性坐标、非轴对齐有向轴、非对称有界关节、连续/直线/固定关节、固定根与浮动根、重复网格实例、非单位网格尺度、无碰撞输入和显式碰撞来源。负例在重新计算合法清单后故意改错惯量、轴向、父子关系、碰撞或场景，证明独立语义门能拒绝，不能只测摘要失败。

真实机器人在私有工作目录验收，记录源提交/输入哈希、配置、命令、工具身份、逐门结果与限制；不把真实 CAD/硬件数据写进工具仓库。M3 的缺失碰撞、零 effort/velocity 等源事实必须如实保留，不能宣称可驱动或可站立。公共 CI 运行同一完整检查流程的中性合成夹具。

## 精简验收

本模块只保留一个生成入口和一个完整只读检查入口。`rg -n 'generate_mjcf|MjModel|MjSpec|fullinertia|robot\.json' src/description_pipeline/mjcf* src/description_pipeline/verification/mjcf.py` 应能证明：无旧脚本复制、无完整第二模型、无原生低精度 XML 导出链。参考脚本属于另一个仓库，不能声称本 PR 已删除它；本仓库不存在旧 MJCF 实现则删除清单明确记为“不适用”，以实测搜索结果作证。

新增复杂度只为四件事服务：无损物理投影、显式场景来源、可携带引用闭包、独立数值验收。任何额外框架或持久状态必须先证明必要性。
