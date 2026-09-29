# URDF 合同与严格质检

[English](urdf_standard.en.md) · 中文

当前模型使用 `description check --root MODEL --profile PROFILE`，统一执行来源、物理语义、消费者与交付完整性检查。
本页维护其中的 `URDF###` 规则编号，以及独立诊断工具 `tools/audit.py` 的参数。
独立 Audit 只检查模型文件，不能替代完整用途验收或授予发布资格。

```sh
description check --root /path/to/model --profile kinematics
# 单独诊断既有 URDF；报告放在 build/ 下，不改变交付内容
python tools/audit.py --root /path/to/model --policy strict --mujoco --report /path/to/model/build/urdf-audit.json
```

退出码：`0` 通过；`1` 有 error 或（strict 下）未豁免的 warning；`2` 用法/解析错误。

## 参数

| 参数 | 作用 |
|---|---|
| `--root DIR` | 要检查的工作区（默认当前目录）；工具在 main 时指向模型分支工作区 |
| `--urdf PATH` | 指定 URDF（默认 `<root>/urdf/robot.urdf`） |
| `--mjcf PATH` | 指定 MJCF（默认 `<root>/mjcf/robot.xml`，存在才做一致性检查） |
| `--joint-names PATH` | 结构关节台账（默认 `<root>/config/joint_names.yaml`） |
| `--waivers PATH` | 例外台账（默认 `<root>/config/urdf_quality.json`） |
| `--policy strict\|advisory` | `strict`（默认）：error 或未豁免 warning 都失败；`advisory`：只有 error 失败 |
| `--mujoco` | 额外做编译层比对（质量/质心/惯量/正运动学/多姿态自接触），需要安装 mujoco |
| `--today YYYY-MM-DD` | 判定例外是否过期的日期（默认今天，便于复现） |
| `--json` | 只输出 JSON 报告 |
| `--report PATH` | 把 JSON 报告写到该路径（独立诊断报告，建议放在 `build/`） |
| `--verify-report PATH` | 只校验已提交的报告：存在、通过、且与当前 URDF 字节一致（历史报告完整性检查）；配 `--json` 输出 `{ok, reason, report, summary}`，便于 CI 解析 |

## 适用范围

`description check` 根据用途和规范模型选择规则：工作区带 `config/joint_names.yaml` 时，关节清单
用**这份台账**（与 URDF 脱节会被 `URDF208` 判失败），不带时用模型自己声明的关节——交付布局要求
这份文件，独立 Audit 会对缺失报错。无质量参考系直接来自模型，网格允许已声明并经验证的缩放，
运动学用途不要求碰撞，左右对称须显式声明。
均匀密度声明由完整张量几何验算检查；`URDF310/311` 保留为独立 Audit 的历史诊断规则。
具体用途要求与消费者检查见[运行手册](pipeline.md)。

## `description check` 与交付审计

两个工具读同一批规则表，差异在**选取**上，而且是有意的：`check` 按某个用途给规范模型判定资格，严格
审计判定**交付文件**（加 `--mujoco` 才含编译层）。下表每一行都是"`check` 能过、审计仍可能拒绝"的地方，
提前知道比事后被拒好：

| 方面 | `description check` | `tools/audit.py --policy strict` |
|---|---|---|
| 规则选取 | 按用途与规范模型裁剪 | 全套规则，加 `--mujoco` 的编译层 |
| 碰撞几何 | 运动学用途不要求 | `URDF407` warning，严格策略下须豁免才过 |
| `left_*` / `right_*` 成对 | 仅在模型声明 `mirror_symmetry_required` 时检查 | 总是检查——`URDF601`–`URDF603` warning |
| 均匀密度 | 由完整张量几何验算替代 `URDF310`/`URDF311` | 保留 `URDF310`/`URDF311` |
| 编译层 | 流水线自己的 MuJoCo 消费者检查 | `URDF507`、`URDF510`、`URDF511` |

台账与网格缩放**不在**上表里：`config/joint_names.yaml` 缺失、读不出或与 URDF 脱节在两个工具里都是
`URDF208` error（`description model init` 会生成带空清单的模板，每个可动关节登记齐之前不会放行），
网格 `scale` 不是 1 在两个工具里都是 `URDF405` error——单位换算属于导出阶段。两个已发布的来源适配器
交付的都是单位缩放（SolidWorks 写 `scale="1 1 1"`，Onshape 根本不写 scale 属性），所以缩放的严格边界
只对手写网格有影响。

差异落在 **warning** 上时（`URDF407`、`URDF601`–`URDF603`），工作区级例外台账就是官方桥：写清编号、
对象、理由、责任人与日期，两个工具都会接受这份工作区——一次运行只对**自己评估过**的规则判"死例外"
（`URDF702`）。**error** 级结论不可豁免：`URDF208` 与 `URDF405` 都不能靠例外台账过关。

## 规则表

### 结构（URDF1xx）

| 规则 | 级别 | 含义 |
|---|---|---|
| `URDF101` | error | `<robot>` 缺少 name |
| `URDF102` | error | 没有任何 link（模板除外） |
| `URDF103` / `URDF104` | error | link / joint 名字重复 |
| `URDF105` | error | 关节引用了不存在的 parent/child link |
| `URDF106` | error | 关节图不是单根树（根 link 数量 ≠ 1） |
| `URDF107` | error | 有环、悬空子图，或 link 有多个父关节 |
| `URDF108` | error | 关节类型非法 |
| `URDF109` | error | 出现 `floating` / `planar` 关节（无法从 CAD 复核，也不是单轴驱动） |
| `URDF110` | error | 没有任何可动关节 |

### 关节语义（URDF2xx）

| 规则 | 级别 | 含义 |
|---|---|---|
| `URDF201` | error | 可动关节缺 effort/velocity；revolute/prismatic 还须有 lower/upper |
| `URDF202` | error | 限位非有限、上下界颠倒、effort/velocity 非正 |
| `URDF203` | error | revolute 角限位绝对值超过 2π（疑似把 deg 当 rad） |
| `URDF204` | warning | continuous 带无效的 lower/upper；effort/velocity 仍须保留 |
| `URDF205` | error | 可动关节缺 `<axis>` 或 axis 是零向量 |
| `URDF206` | error | axis 未单位化（误差 > 1e-3），动力学与限位语义会偏 |
| `URDF207` | error | origin/axis 含非有限值，或 origin 偏移超过 10 m |
| `URDF208` | error | `config/joint_names.yaml` 台账与 URDF 的可动关节不一致（缺登记、多登记、重复，或文件读不成台账） |

### 惯性（URDF3xx）

| 规则 | 级别 | 含义 |
|---|---|---|
| `URDF301` | error | 有网格的 link 缺 `<inertial>`（无质量参考系要写进例外台账） |
| `URDF302` | error | 质量非正；有几何的 link 质量必须为正 |
| `URDF303` | error | 质量/质心/惯量含非有限值 |
| `URDF304` | error | 惯量非正定或违反主惯量三角不等式 |
| `URDF305` | error | 回转半径超过几何上限（物理不可能：惯量或单位错误） |
| `URDF306` | warning | 回转半径接近几何上限（> 0.85×）或过小（< 0.02×） |
| `URDF307` | warning | 质心不在 link 几何包围盒内（±2 mm 容差） |
| `URDF308` | warning | 等效密度超出 100–20000 kg/m³ 数量级 |
| `URDF309` | info/warning | 无质量参考系（info）；有惯性却没有任何几何（warning，合法 primitive 也算几何） |
| `URDF310` | warning | 主惯量与**均匀密度网格惯量**差超过 ~33%（量级/张量/质量归属可疑） |
| `URDF311` | warning | 最大主惯量轴与网格主轴相差 > 20°（质量分布或坐标系可疑） |

几何上限的定义：几何包围盒的角点到质心的最大距离；任何落在该几何内的质量分布
都不可能超过这个回转半径，因此 `URDF305` 是硬约束，`URDF306` 是复核提示。
`URDF310`/`URDF311` 用同一份网格另算一个"均匀密度刚体"：它不等于真值（网格可能
含伺服壳体、外壳），但能抓出数量级错误、张量填错、主轴错位与质量归属错误。

### 几何（URDF4xx）

| 规则 | 级别 | 含义 |
|---|---|---|
| `URDF401` | error | mesh 用了绝对路径/`package://`/URL（本仓库只接受相对路径） |
| `URDF402` | error | 网格文件不存在 |
| `URDF403` | error | 网格无法解析（二进制/ASCII STL 都不是） |
| `URDF404` | error | 网格最大边长超出 1e-4–5 m |
| `URDF405` | error | mesh `scale` 不是 1（单位换算应在导出阶段完成） |
| `URDF406` | warning | 退化（零面积）三角面比例 > 1% |
| `URDF407` | warning | 有 visual 但没有 collision |
| `URDF408` | info | 多个网格文件内容完全相同（内容哈希） |
| `URDF409` | warning | 网格不是封闭实体（有向体积≈0），体积类检查不可用 |
| `URDF410` | info | 非闭合边统计（CAD 导出的 T 型接缝，仅记录） |
| `URDF411` | warning | 网格法线整体朝内（有向体积为负） |

### MJCF 一致性（URDF5xx，`mjcf/robot.xml` 存在时才检查）

| 规则 | 级别 | 含义 |
|---|---|---|
| `URDF502` | error | `meshdir` 不是相对路径 |
| `URDF503` | error | MJCF 引用了 URDF 没有的网格 |
| `URDF504` | error | MJCF body 在 URDF 里没有同名 link |
| `URDF505` | error | 同名 body 质量与 URDF link 不一致（> 1e-6 kg） |
| `URDF506` | error | 关节限位与 URDF 不一致（> 1e-5 rad） |
| `URDF507` | error | `--mujoco`：**编译后**质量与 URDF 不一致（覆盖密度推断/default 继承差异） |
| `URDF508` | error | `--mujoco`：编译后质心与 URDF 相差 > 1 mm |
| `URDF509` | error | `--mujoco`：编译后完整惯量张量（含方向）与 URDF 不一致 |
| `URDF510` | error | 要求 `--mujoco` 但缺少消费者/MJCF，或 MJCF 无法编译 |
| `URDF511` | error | `--mujoco`：编译后缺少可动关节，或 body 位移 > 0.1 mm / 姿态差 > 0.04°（含 prismatic） |
| `URDF512` | info | `--mujoco`：多姿态自接触统计（碰撞策略仍需机械/仿真签核，不作为门禁） |

`URDF511` 的两个阈值按存储精度校准：URDF 存 `rpy`、MJCF 存 `quat`，各自只保留约 6 位有效
数字，舍入本身就有约 0.001° 量级；真实的轴/原点错位在
度级以上，因此门禁取 0.1 mm 与 0.04°。位置对比在 5 个采样姿态上做，姿态对比同时覆盖位置
重合但朝向不同的情况。

### 左右镜像（URDF6xx，仅对 `left_*`/`right_*` 成对命名生效）

| 规则 | 级别 | 含义 |
|---|---|---|
| `URDF601` | warning | 左右 link 质量差 > 5% |
| `URDF602` | warning | 左右惯量 Frobenius 相对差 > 10% |
| `URDF603` | warning | 左右关节限位形状不同（宽度不同，或中心既不相同也不相反） |

左右轴向符号约定可以相反，因此限位比较的是**区间形状**（宽度与中心），不是逐字
相同的 `lower`/`upper`。

### 台账与例外（URDF7xx）

| 规则 | 级别 | 含义 |
|---|---|---|
| `URDF701` | error | 例外台账格式错误（非 JSON、缺字段、编号不合法、日期非 ISO） |
| `URDF702` | error | 死例外：例外没有命中任何结论（改了模型就要删掉；本次运行**没有评估**的规则——开关关闭或用途未选中——不算死例外） |
| `URDF704` | error | 试图豁免 error 级结论（不允许） |
| `URDF705` | error | 例外已过期（`review_after` 早于当天），复核后更新 |

## 例外台账：`config/urdf_quality.json`

```json
{
  "massless_links": ["imu_frame"],
  "waivers": [
    {
      "code": "URDF308",
      "subject": "battery",
      "reason": "电池网格是外壳，等效密度不适用；质量来自数据手册 45 g",
      "owner": "andy",
      "date": "2026-09-18",
      "review_after": "2027-03-18"
    }
  ]
}
```

纪律：**error 一律不可豁免**（要么修模型，要么改规则并写测试）；warning 必须逐条
写理由、责任人、日期，可选复核期限；模型改了导致例外不再命中就是 `URDF702` 错误；
过期是 `URDF705` 错误。因此台账不会腐烂，也不会变成静默放任。台账是工作区级文件，而一次运行只看
规则子集：为本次没有评估的规则写的例外不算死例外（例如不开 `--mujoco` 时的编译层规则，或运动学
用途下的 `URDF407`）——否则交付审计与 `description check` 会互相要求相反的东西。

## 验收边界

规则通过说明模型满足已执行的检查，不能证明 CAD 材料、执行器额定参数或实物标定正确。
来源修订、排除依据、动作与观测映射、碰撞工况及应用/HIL 证据由[完整检查链](pipeline.md)分别验证。

完整验收使用模型锁定的工具检查精确模型 SHA；发布时重新取得远端实际字节。
可选工作流 `model-validation.yml` 和 `validate.yml` 分别验证模型与工具。模型发布流程见[运行手册](pipeline.md#提交验收与发布)。

## 维护

新增规则必须同时做三件事：在 `src/description_pipeline/verification/urdf_quality/rules.py` 用稳定编号实现、
在本文补条目、在 `tests/test_urdf_quality.py` 里补一个能触发它的反例；
`test_every_rule_has_a_seeded_defect` 会在缺少反例时失败。

新 profile 的规则适用性、报告全内容绑定及应用验收合同见 [工程契约](pipeline.md)。
