# Onshape 旧导出器参考（历史归档）

> 以下保留旧接口、规则和当时记录，不能作为当前安装、导出或验收指南。
> 生产流程见[当前来源说明](../sources/onshape.md)；旧原生导出和部署入口已停用。

---

# Onshape → URDF/MJCF 导出工具

> **写通道已弃用（2026-09-20）**：`export` 子命令拒绝执行（退出码 `3`），
> `engine.run` 抛 `EngineDeprecated`，本工具不再调用 `onshape-to-robot`，也不再写模型文件。
> 出模型请用 `description source freeze` → `description build`；来源语义规范化在
> `description_pipeline.sources.onshape.normalize_scene`。迁移映射见
> [tools/onshape_export/MIGRATION.md](../../tools/onshape_export/MIGRATION.md)；
> 保留的 `check` / `fetch` / `fetch-geometry` / `verify` 只用于 API 读取、缓存抓取与历史回归。

`tools/onshape_to_urdf.py` 把任意 Onshape 装配体导出成本仓库的固定入口
（`urdf/robot.urdf`、`meshes/`、`mjcf/robot.xml`、`mjcf/scene.xml`），
并在导出前后各做一轮独立检查。工具本身只用标准库；实际转换调用引擎
`onshape-to-robot`（与本仓库的历史模型同源）。

目标是"同一份文档在任何机器上导出同一份模型，且失败可解释"：

| 阶段 | 命令 | 产出 |
|---|---|---|
| 导出前诊断 | `check` | `onshape/checks.json`，规则编号 `OSX###` |
| 转换 | `export` | `urdf/`、`mjcf/`、`meshes/`、`onshape/` |
| 导出后核验 | `verify` | `onshape/verification.json`，规则编号 `OSV###` |

## 前置条件

- Python 3.12+；`check`/`verify` 零第三方依赖。
- 凭据（两种任一）：环境变量 `ONSHAPE_ACCESS_KEY` / `ONSHAPE_SECRET_KEY`
  （可选 `ONSHAPE_API`、`ONSHAPE_SECRET_BEARER`），或 `~/.onshape_api_keys.json`：

  ```json
  { "https://cad.onshape.com": { "access_key": "...", "secret_key": "..." } }
  ```

- 只有 `export` 需要引擎：`pip install onshape-to-robot==1.8.3`
  （本项目在该版本上验收；其它版本需重新跑 `verify` 并记录）。

## 快速开始

```bash
# 1) 导出前诊断（不写模型）
python tools/onshape_to_urdf.py check \
  --url https://cad.onshape.com/documents/<did>/w/<wid>/e/<eid> --mass

# 2) 导出（URDF + MJCF），任何 error 都会阻止导出
python tools/onshape_to_urdf.py export \
  --url https://cad.onshape.com/documents/<did>/w/<wid>/e/<eid> \
  --out . --format both --ignore "part 1"

# 3) 只核验已有目录（可给出权威模型做逐关节比对）
python tools/onshape_to_urdf.py verify --out . --reference path/to/reference/robot.urdf
```

导出后的目录：

```text
urdf/robot.urdf          # 模型名固定为 robot，网格引用为 ../meshes/<file>
meshes/*.stl             # 与 URDF/MJCF 共用；布局归一化时按内容哈希去重
mjcf/robot.xml scene.xml # MJCF 通过 meshdir="../meshes" 引用同一批网格
onshape/
  source.json            # 文档/工作区/元素、时间戳、缓存来源
  assembly.json          # 导出时的装配体响应快照（核验比对用）
  assembly_features.json # mate 限位等特征
  mate_values.json       # 当前关节值（限位偏移依据）
  checks.json            # 导出前诊断结果
  verification.json      # 导出后核验结果
  layout.json            # 网格名单与 SHA-256
  engine/<format>/config.json  # 传给引擎的原始配置（可复现）
```

## 命令与退出码

| 命令 | 说明 | 退出码 |
|---|---|---|
| `check` | 导出前诊断；`--mass` 追加材质/质量检查 | 0 通过；1 有 error；2 用法/凭据/网络问题 |
| `fetch` | 把装配体/特征/mate 值/质量属性抓进 `--cache` | 同上 |
| `fetch-geometry` | 抓零件工作室 GLTF 并拆成 per-part STL 写入缓存 | 0 全部匹配；1 有未匹配零件 |
| `export` | 诊断 → 引擎转换 → 布局归一 → 核验 | 0 通过；1 有 error（`--force` 可越过诊断） |
| `verify` | 只做导出后核验，可加 `--reference` 权威模型 | 0 通过；1 有 error |

`--json` 输出机器可读报告；`--report PATH` 额外落盘。默认不写任何文件。

## 参数总览

`--stack` 是全局参数（Onshape 站点，默认生产环境）。与文档来源相关的参数每个
子命令都有：

| 参数 | 作用 |
|---|---|
| `--url` | 文档 URL，可带 `/w/<wid>` 或 `/v/<vid>`，以及 `/e/<eid>` |
| `--document-id` `--workspace-id` `--version-id` `--element-id` | 逐个指定 ID；与 `--url` 二选一，同时给出时以显式 ID 为准（`--workspace-id` 与 `--version-id` 互斥） |
| `--cache DIR` | 读写 API 响应；`fetch` 写入，其余命令读取。配合 `--offline` 可完全离线 |
| `--offline` | 只读缓存，不发任何网络请求 |
| `--json` | 只输出 JSON（默认打印摘要） |
| `--report PATH` | 额外把报告写到指定路径（`check`/`export`/`verify` 可用） |

各命令独有参数：

| 命令 | 参数 | 作用 |
|---|---|---|
| `check` | `--mass` | 追加材质/质量检查（会多发一次质量属性请求） |
| `check` `export` | `--assembly-name` | 文档里有多个装配体时按名字选择 |
| `check` `export` | `--allow-unlimited` | 允许没有限位的转动/平动关节（默认告警） |
| `check` `export` | `--auto-dof` | 按 mate 类型自动判定关节/固定，见「任意命名的文档」一节 |
| `fetch-geometry` | `--tolerance` | 拆网格时"体积 + 质心"匹配阈值，默认 `0.05`（5%）；超阈值即报未匹配 |
| `export` | `--out DIR` | 输出根目录（写 `urdf/`、`meshes/`、`mjcf/`、`onshape/`） |
| `export` | `--format` | `urdf` / `mjcf` / `both`（默认 `both`） |
| `export` | `--ignore PATTERN` | 忽略的零件，可重复（如 `--ignore "part 1"`） |
| `export` | `--color R G B A` | MJCF 材质颜色，默认 `0.72 0.72 0.74 1.0`；显式给出可跳过逐零件 metadata 请求 |
| `export` | `--density-map FILE` | 分类等效密度覆盖，见「材料密度覆盖」一节 |
| `export` | `--joint-properties FILE` | 关节/执行器动力学属性，见「关节与执行器属性」一节 |
| `export` | `--contact-exclude FILE` | 接触排除表，见「接触排除」一节 |
| `export` | `--force` | 诊断有 error 时仍继续导出（默认中止） |
| `export` `verify` | `--mujoco` | 用 MuJoCo 真加载 `scene.xml` 比对质量，并统计零位自接触 |
| `export` `verify` | `--reference FILE` | 权威 URDF：逐关节比对位置/轴向/可达区间/质量 |
| `export` `verify` | `--reference-mjcf FILE` | 权威 MJCF：比对执行器类型与关节阻尼/摩擦/转子惯量 |
| `verify` | `--out DIR` | 要核验的导出目录 |
| `verify` | `--source FILE` | 源装配体 JSON（默认 `<out>/onshape/assembly.json`）；缺省时退化为纯模型核验 |

## 导出前诊断（导出失败与静默错误的常见根因）

| 规则 | 级别 | 含义与处理 |
|---|---|---|
| `OSX001` | error | 没有任何 `dof_*` mate：导出不会产生关节。先在 Onshape 建关节。 |
| `OSX002` | error | mate 未解算（Onshape UI 提示 *cannot resolve mate connectors*）。典型原因：`mateConnectorsQuery` 带了 occurrence `path`；该 mate 不会产生任何关节，必须删掉重建为"只给连接器 id、`path` 留空"。 |
| `OSX003` | error | 同名 mate 重复：会产生重复关节并把图变成非树。删掉多余副本。 |
| `OSX004` | error | `dof_*` 图不是单树（有环或多棵子树）：导出会出现重复关节或多个根节点。 |
| `OSX004b` | error | 存在与主树断开的孤立关节子树（子图本身是树，但整体不连通）：导出会得到多个根节点。 |
| `OSX005` | warning | 有实例不参与任何关节，会作为额外根节点出现。 |
| `OSX006` | error | 多个 `frame_*` 复用同一个孤儿实例：引擎要求每个参考系有独立孤儿体，否则报 *should mate an orphan body*。 |
| `OSX007` | warning | 转动/平动关节没有启用限位。确认是有意为之（`--allow-unlimited`）。 |
| `OSX008` | info | URDF 根节点将是装配体**第一个实例**（引擎行为，不是文档里的固定零件）。 |
| `OSX009` | warning | 有零件没有质量：STEP 导入不带材质，导出质量会为 0；先在 Onshape 赋材质。 |
| `OSX010` | error/info | 有转动/平动 mate 未按 `dof_` 命名：默认导出会把它当固定连接（error）；加 `--auto-dof` 后按类型导入（info）。 |
| `OSX010b` | info | 未命名约定的 mate 都是固定类型，会按固定连接导入，无需处理。 |
| `OSX011` | warning | `--auto-dof` 自动改名与既有 mate 重名，已顺延为 `dof_<name>_2` 等。 |

## 导出后核验

| 规则 | 级别 | 检查内容 |
|---|---|---|
| `OSV002`~`OSV002d` | error | 关节树：单根、连通、无重复子节点、引用存在。 |
| `OSV002e` | error | 关节类型非法（如 `type="motor"`）——常见原因是把执行器的 `type` 写进了 `joint_properties`。 |
| `OSV003`~`OSV004c` | error | 网格引用必须是 `../meshes/<file>`、文件存在非空、STL 有效、尺寸合理（最大边 ≤ 5 m，防单位错误）。 |
| `OSV004d` | warning | STL 里的零面积三角面数量（碰撞/视觉网格质量）。 |
| `OSV005`~`OSV005c` | error/warning | 质量非正、整机零质量、有网格但无惯性。 |
| `OSV006`~`OSV006d` | error/warning | 限位缺失、上下界颠倒、超过 2π。 |
| `OSV007`~`OSV010` | error/warning | MJCF 是否存在、`meshdir` 正确、URDF↔MJCF 的质量与限位逐项一致。 |
| `OSV011`~`OSV012` | error/info | 与**源装配体**比对：关节世界位置、轴向共线；正方向与 mate 第一连接器相反时给 info。 |
| `OSV013`~`OSV014` | warning/error | 可选 `--mujoco`：真加载 `scene.xml` 再比对质量。 |
| `OSV020`~`OSV025` | error/warning/info | 与 `--reference` **权威模型**比对：连杆质心映射、关节世界帧、轴向、**可达区间**、质量。 |
| `OSV030`~`OSV031` | error | 惯量张量必须对称正定、满足主惯量三角不等式；有质量不得零惯量。无质量参考系 link（引擎用 1e-9 kg 占位）不受此约束。 |
| `OSV032`~`OSV033` | warning/info | 与参考比对**单位质量惯量** I/m（量纲 m²）：把密度差异剔除后看几何/质量分布是否一致，按 Frobenius 范数给相对差。 |
| `OSV034`~`OSV034b` | error/warning | effort/velocity 与参考一致性核对；若只是引擎默认值（10/10）会明确标注"非伺服真实限制"。 |
| `OSV035` | warning/info | `--mujoco` 时在零位姿态真加载 `mjcf/robot.xml`，统计自接触刚体对（不含地面），供人工确认是否为设计限位。 |
| `OSV036` | info | 列出 MJCF 里声明的接触排除（`--contact-exclude`），并提示 URDF 与其他消费者不继承该设置。 |
| `OSV040`~`OSV041` | error/info | 与参考 MJCF 比对执行器类型、关节阻尼/摩擦/转子惯量（用 MuJoCo 解析默认 class 继承后的有效值）。 |

`OSV023`（可达区间）把父子角色与轴向符号一起换算后再比，因此能识别
"轴反向 + 非对称限位"这类只有物理语义才暴露的问题——这正是本项目在真实
Microban 文档上踩到并修复过的缺陷。

`verification.json` 的 `reference_comparison` 会记录**逐连杆映射**（世界质心距离）
与**逐关节明细**：父子角色是否互换、轴向符号、位置误差、轴向误差、换算后的可达区间
与判定（`ok` / `position_mismatch` / `axis_mismatch` / `range_mismatch`）。
因此"为什么判定通过"可以被逐条审计，而不只是一个结论。

## 必须知道的引擎行为

1. **根节点 = 装配体第一个实例。** 与文档里"固定"的零件无关；若第一个实例不是
   期望的躯干，导出的父子方向会与直觉不同（`OSX008` 会提示）。
2. **关节正方向由 mate 连接器顺序与树的父子方向共同决定。** 当引擎把某个关节的
   轴反向时，限位数值照抄不变；若该关节限位非对称，可达区间会被镜像。
   处理：用 `--reference` 与权威模型比对（`OSV023`），或在 Onshape 里交换该 mate
   的连接器顺序后重导。
3. **未解算 mate 会被静默跳过**，导致关节数变少；`OSX002` 会拦下。
4. **`frame_*` 参考系需要各自的孤儿体**（见 `OSX006`）。
5. **STEP 导入不带材质**：质量与惯量会退化为 0，动力学不可用（`OSX009`）。
   赋材质用 metadata API 写 `MATERIAL_PROPERTY_ID` 对应的材料（含 DENS），
   或直接在 Onshape 界面里批量赋材质。
6. **MJCF 的网格声明顺序随 `PYTHONHASHSEED` 变化**（引擎按 set 遍历用到的网格）。
   布局归一化会排序 `<asset>` 里的 `<mesh>`，因此同一版本引擎 + 同一缓存可以
   **字节复现**导出结果；否则同一份数据每次导出的 `robot.xml` 只有顺序不同。

## 覆盖范围：什么能验证，什么必须外部提供

工具只能使用"CAD/文档里真实存在的数据"。哪些信息在源文件里、哪些不在，
决定了它能验证到什么程度：

| 信息 | 是否在 CAD/导出里 | 工具的处理 |
|---|---|---|
| 关节树、命名、类型 | ✅ Onshape mate | `OSX001`~`OSX006`、URDF 结构检查 |
| 关节帧（位置/轴向）、限位 | ✅ mate + connectors | 与源 mate 比对（`OSV011`~`OSV012`）、与参考模型比对（`OSV021`~`OSV023`） |
| 几何（网格） | ✅ 零件工作室 | 网格引用/尺寸/退化面（`OSV003`~`OSV004d`） |
| 质量、惯量 | ⚠️ 需要材质；导入的 STEP 不带材质 | 缺材质时 `OSX009` 告警；惯量做正定/三角不等式检查与 I/m 比对（`OSV030`~`OSV033`） |
| 碰撞是否合理、自碰撞策略 | ❌ 设计意图，不在文件里 | 零位自接触统计供人工确认（`OSV035`）；已知的设计贴合/干涉用 `--contact-exclude` 写成 MJCF 接触排除（`OSV036`）；退化面统计（`OSV004d`） |
| effort/velocity | ❌ 伺服规格，不在 CAD 里 | 与参考一致性核对并标注是否为引擎默认值（`OSV034`） |
| 驱动极性、关节零位 | ❌ 电气/装配信息 | 由控制器仓库或实测提供（见模型分支的控制侧约定），本工具不做假设 |
| 逐零件材质 | ❌ STEP/URDF/MJCF/官方仓库都没有 | 记录为已知差异；可用等效密度近似，但必须标注来源与假设 |

因此"工具通过"的含义是：**与给定参考模型在几何与关节语义上等价、结构自洽**；
"伺服真实限制、碰撞策略、驱动极性/零位"属于模型之外的信息，必须在模型分支的
质量记录里显式填写来源（数据手册、控制器代码或实测），否则保持阻塞状态。

## 任意命名的文档：`--auto-dof`

引擎认识三类 mate 前缀：`dof_*`（关节）、`fix_*`（固定合并）、`frame_*`（参考系），
以及 `closing_*`（闭环）。不是所有 Onshape 文档都按这套命名，因此：

```bash
python tools/onshape_to_urdf.py check  --url <doc-url>              # 有未命名关节 → OSX010 error
python tools/onshape_to_urdf.py export --url <doc-url> --out . --auto-dof
```

`--auto-dof` 的判定规则（不猜名字，只看 mate 类型）：

* `REVOLUTE` / `CYLINDRICAL` / `SLIDER` / `BALL` → 作为关节，改名为 `dof_<原名>`
* 其它（含 `FASTENED`）→ 作为固定连接，改名为 `fix_<原名>`
* 改名与既有名称冲突时顺延为 `_2`、`_3` 并在报告里给 `OSX011` 告警

因为需要改写引擎看到的数据，`--auto-dof` 的导出走**缓存通道**：工具会先把装配体/
特征/mate 值/质量属性与零件网格抓进缓存（`--cache`，未指定时落在
`<out>/onshape/cache/`），再喂给引擎。这条通道同时更省配额：质量属性按工作室批量取
（2 次而不是逐零件 34 次），网格优先用零件工作室 GLTF 拆分（1 次/工作室），
缺失的零件自动回退到逐零件 STL。

建议：能改名就改名（`dof_` 前缀是引擎的稳定契约）；`--auto-dof` 是为"拿来就能跑"
准备的兜底路径，改名后请重新跑 `check` 确认 `OSX010` 消失。

## 材料密度覆盖：`--density-map`

CAD 几何是真的，材质常常缺失（STEP 导入）或需要按类别近似（打印件按 15% 填充的
等效密度、钢垫片按钢）。把结论写成 JSON 随模型提交，导出即可复现：

```json
{
  "source": {"derivation": "ρ_print = (818.910 g 官方整机 − 475.09 g 非打印件) / 359.14 cm³"},
  "parts": {"JFT": 7850},
  "names": {"steel_shim": 7850, "shoulder__shoulder": 957}
}
```

```bash
python tools/onshape_to_urdf.py export --url <doc-url> --out . \
  --density-map onshape/density_map.json
```

规则：

* 键可以是 `partId`（`parts`）或零件名（`names`，匹配实例名去掉 `<n>` 的部分，
  也接受 `xyz__xyz` 的短名）；两者都命中时 `partId` 优先。
* 覆盖只改**质量属性**：质量 = 体积 × 新密度，惯量按密度线性缩放，质心不变；
  源文档与几何不受影响。
* 覆盖未命中任何零件、或密度超出 10–25000 kg/m³ 时直接报错，避免静默用错材料。
* 走缓存通道（同 `--auto-dof`），导出目录会留下 `onshape/materials.json` 记录
  每个零件的 `mass_before/kg → mass_after/kg`，便于审计。

Microban 实测效果：打印件 957 kg/m³（由官方整机质量反推，实体 PLA 的 77%）、
钢垫片 7850、POM 1410 后，整机质量从 0.916920 kg 收敛到 **0.818978 kg**
（官方 0.818910 kg，差 0.07 g），逐连杆超差 link 从 19 个降到 10 个；剩余差异
来自导入件的产品结构分组（例如 hip 支架缺失），不是密度问题。

## 关节与执行器属性：`--joint-properties`

引擎默认写 `<joint frictionloss="0.1" armature="0.005">` 与 `<position kp="50">`；
若真值是力矩电机（如 Microban 的 XL330），动力学语义会偏。用属性文件对齐：

```json
{
  "source": {"reference": "官方 MJCF 的 xl330 class"},
  "joint_properties": {
    "default": {"damping": 0.041, "frictionloss": 0.013, "armature": 0.0018, "type": "motor"}
  }
}
```

```bash
python tools/onshape_to_urdf.py export --url <doc-url> --out . \
  --density-map onshape/density_map.json \
  --joint-properties onshape/joint_properties.json \
  --reference <官方 robot.urdf> --mujoco
```

注意：引擎的 URDF 与 MJCF **共用** `joint_properties`，但键含义不同——`type` 在 URDF 里
是关节类型、在 MJCF 里是执行器类型。工具会按输出格式过滤（URDF 只保留
`type`/`max_effort`/`max_velocity`，MJCF 只保留执行器与关节动力学键），并新增
`OSV002e` 拦住"执行器类型写进 URDF"这类事故。

## 接触排除：`--contact-exclude`

引擎给每个零件生成网格碰撞体，而装配在一起的零件常按设计互相干涉
（轴承压在孔里、螺钉穿过安装柱）。这些干涉在零位就产生自接触，会把静置姿态
顶偏，甚至顶到超出执行器力矩上限。官方 MJCF 的做法是手写 primitive 碰撞体；
本工具不改碰撞几何，而是把"设计上贴合/干涉"的刚体对写成 MJCF 原生排除：

```json
{
  "source": {"reference": "官方 MJCF 零位自接触 0 处；本导出用零件网格做碰撞体"},
  "excludes": [["foot__1", "tibia"], ["femur", "hip__1"]]
}
```

```bash
python tools/onshape_to_urdf.py export --url <doc-url> --out . \
  --contact-exclude onshape/contact_excludes.json --mujoco
```

- 刚体名写错会在导出开始前报错，并列出模型里可用的名字；同名/重复项也会被拒绝。
- 排除只作用于 MJCF（写进 `<contact><exclude>`，位于 `</worldbody>` 之后）；
  URDF 没有对应机制，其他消费者要自行复制这份名单，`OSV036` 会提示这条差异。
- Microban 实测效果：零位自接触 14 处 → 0 处，PD 保持零位 1 s 的髋俯仰下垂
  8.59° → 0.08°，基座水平漂移 3.6 mm → 0.6 mm，峰值力矩 0.64 N·m（饱和）
  → 0.073 N·m。

哪些刚体对属于"设计贴合"只能由设计意图判断（工具无法从 CAD 反推），因此
这份名单是显式输入，并且必须写清来源与理由；名单之外的零位自接触仍由
`OSV035` 报出。

## 配额耗尽时的离线流程

Onshape 年度 API 配额按**用户**计，换 key 无效；官方文档说明浏览器会话与
API Explorer 的调用不计入配额。因此数据可以先取回本地，再离线导出：

```bash
python tools/onshape_to_urdf.py fetch          --url <doc-url> --cache cache/
python tools/onshape_to_urdf.py fetch-geometry --url <doc-url> --cache cache/
python tools/onshape_to_urdf.py export --cache cache/ --offline --out . --ignore "part 1"
```

缓存键固定为 `<cache>/json/{assembly,assembly_features,mate_values,mass_properties}_<id>.json`
与 `<cache>/bytes/stl_<partId>.stl`，`<cache>/source.json` 记录文档引用；
无论用 API key 还是浏览器会话采集，只要按此布局落盘即可被 `--offline` 使用。
`fetch-geometry` 用零件工作室 GLTF（同源 JSON）拆网格，并按
"体积 + 质心"双重匹配回 `partId`，避免 STL 导出端点的跨域/重定向问题。

## 验收标准

一个分支上的模型要被认为"可交付"，至少满足：

1. `check` 0 error（warning 需在 PR/报告中逐条说明）。
2. `export` 成功且 `verify` 0 error。
3. 有权威模型时，`verify --reference` 无 `OSV021`/`OSV022`/`OSV023`。
4. `onshape/` 下的来源、配置、哈希与报告随模型一起提交；质量差异与未验证范围
   写在模型的 README/质量记录里，而不是靠口头约定。

工具通过不代表机械、驱动或安全验收；那些仍按
[工程规范](../engineering_standard.md) 由人工签核。

## 维护者指引

`tools/onshape_export/` 的模块职责：

| 模块 | 职责 |
|---|---|
| `api.py` | Onshape REST 客户端：HMAC 签名、重试、配额错误识别 |
| `cache.py` `offline.py` | 缓存读写；离线导出时替换传输层，转换逻辑不变 |
| `url.py` | 文档引用解析（URL 或显式 ID）与缓存键 |
| `assembly.py` | 装配体响应 → 实例 / mate / 关节树模型 |
| `checks.py` | 导出前诊断 `OSX###` |
| `engine.py` | 唯一接触第三方引擎的地方（写 `config.json`、调用 `onshape-to-robot`） |
| `geometry.py` `stl.py` | GLTF 拆 per-part STL、STL 校验与 SHA-256 |
| `densities.py` `contacts.py` | 两条覆盖通道：分类等效密度、接触排除 |
| `layout.py` | 引擎输出归一（固定入口、网格引用、排序去随机化） |
| `linalg.py` | 向量/惯量小工具 |
| `verify.py` | 导出后独立核验 `OSV###`（含与源装配体、权威模型的比对） |
| `cli.py` | 子命令、退出码、报告落盘 |

新增规则或参数的固定动作（缺文档会被 `tests/test_onshape_docs.py` 拦下）：

1. 代码里给稳定编号/名字——规则用 `OSX###` / `OSV###`（子编号加小写字母后缀），
   参数用 `--kebab-case`；
2. 在本文的规则表或参数总览里补条目（区间写法如 `OSV020`~`OSV025` 可覆盖子编号）；
3. 补一个能构造出该结论的测试，并跑 `python -m unittest discover -s tests`。

版本与取证约定：引擎版本 pin 写在前置条件里；每次导出把生效的输入
（`density_map.json` / `joint_properties.json` / `contact_excludes.json`）与产物
哈希（`layout.json`、`verification.json`）一起提交；模型分支再把引擎版本与工具
PR 记进 `docs/quality.json`。换引擎版本必须重跑 `verify --reference` 并记录差异。
