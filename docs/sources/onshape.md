# Onshape 来源适配器（`description_pipeline.sources.onshape`）

[English](onshape.en.md) · 中文

把任意 Onshape 装配体冻结成**不可变来源快照**，并在任意机器上离线重放出同一份
`description.scene/v1`。适配器只负责"把 CAD 读准"：刚体分组、限位取舍、
effort/velocity、碰撞策略与材质假设都由机器人定义和规范化层决定，不在这里猜。

## 快速开始

```python
from pathlib import Path

from description_pipeline.sources.onshape import freeze, load_scene

manifest = freeze(
    {
        "url": "https://cad.onshape.com/documents/<did>/w/<wid>/e/<eid>",
        "cache": ".cache/onshape",  # 复用不可变响应；在线采集刷新工作区头
        "configuration": "default",
        "capture": {"evidence": "cad", "reason": "2026-09-17 真实 API 采集"},
    },
    Path("out/snapshot"),
)
scene = load_scene(Path("out/snapshot"))  # 校验清单与全部摘要后返回场景对象
```

把同一份配置改成 `{"cache": ".cache/onshape", "offline": True}`，即可在没有凭据、
没有网络的机器上重放出同一份场景；摘要不一致会直接失败（`onshape_snapshot_tampered`）。

## 配置项

`freeze` 的 `config` 就是 `config/robot.yaml` 里的 `source` 映射。
**键名是白名单**：出现未定义键直接报 `onshape_source_config_invalid`（列出已接受的键），
类型不符（`offline`/`include_geometry` 非布尔、`tolerance` 非有限正数、`elements` 非字符串列表等）
同样拒绝——拼错开关不允许静默改变采集行为。

| 键 | 必填 | 说明 |
|---|---|---|
| `url` | 二选一 | Onshape 文档 URL；`/w/<id>` 为工作区，`/v/<id>` 为版本 |
| `document_id` / `element_id` / `workspace_id` 或 `version_id` | 二选一 | 显式 ID，优先于 URL；工作区与版本互斥 |
| `stack` | 否 | 默认 `https://cad.onshape.com` |
| `configuration` | 否 | 装配配置，默认 `default`；会写进身份与清单 |
| `cache` | 否 | 缓存目录（`json/` + `bytes/`）；不可变响应优先复用，在线工作区头始终刷新 |
| `offline` | 否 | 离线：只读缓存，不发起任何请求 |
| `include_geometry` | 否 | 默认 `true`；`false` 时只要物理读数 |
| `tolerance` | 否 | glTF 拆分时零件匹配容差，默认 `0.05` |
| `elements` | 否 | 额外限定的零件工作室元素；离线缺目录时补采 |
| `capture` | 否 | 采集声明，见下节；覆盖时必须写 `reason` |
| `client` | 否 | 注入自定义传输层（测试或企业代理） |

凭据只从环境（`ONSHAPE_ACCESS_KEY` / `ONSHAPE_SECRET_KEY`，可选 `ONSHAPE_API`、
`ONSHAPE_SECRET_BEARER`）或 `~/.onshape_api_keys.json` 读取，任何输出都不含密钥。

## 快照布局

```text
manifest.json            公共清单：schema_version/kind/identity/evidence_class/scene/files
scene.json               description.scene/v1，来源语义（唯一的场景入口）
raw/assembly_*.json      原始 API 响应：装配树、features、matevalues、质量属性、glTF
geometry/parts/*.stl     每个零件的网格（文件名按缓存规则净化）
geometry/parts.json      网格读数：sha256、字节数、三角面、包围盒、体积、来源
```

`manifest.files` 覆盖除清单本身以外的全部文件摘要；多一个、少一个、改一个字节都会
被 `load_scene` 拒绝。清单 `identity` 记录来源锁（文档/工作区或版本/配置/microversion）、
依赖闭包（元素、子装配、零件工作室、零件）与采集设置。

## 证据等级

`identity.capture.mode` 描述取数通道，`evidence_class` 描述这批字节的来历，两者独立：

| 情形 | `capture.mode` | `evidence_class` | 说明 |
|---|---|---|---|
| 本次真的访问了 API | `live_api` | `cad` | 唯一能授予原生 CAD 出处的情形 |
| 重放真实采集缓存并声明 `capture.evidence=cad` | `cache_replay` | `cad` | 缓存是不可变的真实副本 |
| 仓库夹具并声明 `capture.evidence=fixture` | `cache_replay` | `fixture` | 结构测试用，不得当成 CAD 验收 |
| 缓存没有采集声明 | `cache_replay` | `imported` | 同时追加 `capture_provenance_missing` gap |

覆盖 `capture` 必须写 `reason`，否则 `freeze` 直接报 `onshape_snapshot_incomplete`：
证据来历是判断结论可信度的输入，不能默默改写。

## 场景映射规则

| 对象 | 规则 | provenance 字段 |
|---|---|---|
| `links[]` | 一个**叶子 Part 实例**一个 link；帧取零件工作室坐标系 | `source_entities`（实例路径）、`source_part`、`source_name`、`source_mass`、`source_geometry` |
| 质量 | `massproperties` 的标称值（每标量三段 `[标称, 下界, 上界]`，取第 1 段） | `source_mass` 指向原始响应位置 |
| 几何 | glTF 拆分出的网格；无 glTF 时回退旧缓存的 per-part STL | `geometry_source` = `gltf_split` / `cached_part_stl` |
| `joints[]` | mate → `revolute`/`prismatic`/`fixed`；父子取 `matedEntities` 顺序 | `parent_child_from`、`limits_from` |
| joint 位姿 | 第 0 端 mate 连接器在世界系下的原点/z 轴，换算到父 link 局部 | `axis_from` = `mate_connector_z` |
| `frames[]` | `frame_*` mate（mateConnector 定位）→ 命名坐标系 | `source_mate`、`source_mate_entities` |
| 命名 | `"femur (1) <1>"` → `femur_1`，重名加序号；原名保留 | `source_name` |

`scene.provenance` 用三档字段对账，避免把装配容器当成质量实体：link 用
`source_entities` 认领属于自己的物理零件，上层据此检查遗漏与重复，`entity_counts`
给出计数口径。方向、符号、刚体分组一律留给机器人定义覆盖。

| 字段 | 内容 |
|---|---|
| `provenance.expected_occurrences` | 全部 CAD 实例路径（含容器） |
| `provenance.expected_entities` | 只有物理零件实例，才是质量实体 |
| `provenance.non_physical` | `containers`（装配容器）与 `suppressed`（被抑制的零件实例）及依据；两者都不进模型但保留存在性 |

## 按定义规范化（`normalize_scene`）

来源场景是**装配语义**（一个叶子零件一个 link），不是机器人树。公共核心在
`Robot.from_dict` 之前调用来源包里的纯函数：

```python
from description_pipeline.sources.onshape import load_scene, normalize_scene

model = normalize_scene(load_scene(snapshot_root), author_definition, snapshot_root)
```

`author_definition` 就是 `config/robot.yaml` 整体（读它的 `robot` 映射），不是来源采集配置——
分组与关节语义来自作者定义，`source` 仍然只描述"数据从哪来"，因此这一步不需要再访问 API。

### 定义字段（`robot:`）

| 键 | 说明 |
|---|---|
| `schema` | 固定 `description.robot-definition/v1` |
| `root` | 运动树根 link 名 |
| `reference` | 参考模型与对账方法（自由字段，写清依据） |
| `links[]` | `name` + `members`（成员选择器：实体键或顶层实例键）+ `reference`（根 link 的坐标系基准）+ `source` |
| `joints[]` | `name`、`mate`（来源 mate 名或 ID，必须唯一解析）、`parent`/`child`、`type`、`axis.source`/`axis.sign`、`limits.source\|lower/upper`、`zero`、`source` |
| `frames[]` | `name`、`link`（生成的无质量 link）、`mate`、`parent`（固定关节父级） |
| `mass.overrides[]` | `part_ids`/`names` + `density_kg_m3` + `source`（质量假设，只按显式清单生效） |
| `non_physical[]` | `entities` + `basis` + 可选 `evidence`：原始 JSON 须绑定确切实例或同一工作室的零件，几何须是该零件对应的 STL；无实际 frame 消费方且无此类证据时拒绝排除 |
| `collision` / `effort_velocity` | 可选；不写即保持缺失，由用途规则阻断 |

解析是严格的：未知键、非法标识、重复 link、引用不存在的 link 直接报
`onshape_definition_invalid`。
`mate` 选择器同理：装配里同名（或名字与另一个 mate 的 ID 撞车）时无法唯一寻址，直接报
`onshape_definition_invalid` 并列出候选，不做"取第一个"的猜测——静默绑定会把关节连到错误零件上。
`robot.reference` 里的数字（对账误差、限位一致数）是**作者声明**，流水线不复算；机器可查的结论
一律来自下面的 `verify_normalization` 与公共质检层。含义不明确的 `tolerance_m` 已从定义移除，
写了会按未定义字段拒绝。

### 独立校验（`verify_normalization`）

```python
from description_pipeline.sources.onshape import verify_normalization

checks = verify_normalization(raw_scene, definition, snapshot_root, canonical)
# → [{id, version, status, expected, checked, missing, details}, ...]
```

四条检查全部**从原始读数现算**，不调用 `normalize` 的融合/定位实现：

| id | 检查 |
|---|---|
| `source.occurrences` | 从 `raw/assembly_*.json` 重推实例/零件/容器集合与计数，比对 `expected_occurrences`、`expected_entities`、`non_physical.containers`、`entity_counts` |
| `source.entities` | 原始物理零件 = 已覆盖（`source_entities`）+ 显式排除，且无重复、无交叉 |
| `source.exclusions` | 每条排除必须有真实 frame 消费方，或一份**采集层**（`raw/`、`geometry/`）内、确切绑定该实例或零件身份的证据文件；并给出排除质量/零件 ID/源几何；details 里有 `excluded_mass_total_kg` |
| `source.mass_conservation` | 用作者密度假设从 raw 重算世界系 Σm、Σm·c/Σm、Σ(R·I·Rᵀ + m·平行轴)，再用模型树 q=0 FK 把各 link 的惯量搬到同一世界系逐项比对（质量 1e-12 kg / 质心 1e-9 m / 惯量 1e-12+1e-6 相对） |

### 来源身份与修订锁定

`identity.capture_settings` 记录配置与导出设置；`source_semantics` 记录 SI 单位、张量顺序、
惯量参考点和变换方向。原始 JSON 按内容保存，STL 保留原始字节，逐请求记录摘要。
在线采集收尾再次读取工作区头；变化记为 `workspace_moved_during_capture`，不改写已固定的修订。

* **工作区引用**：先用 `/w/` 读一次根装配（探测请求），拿到 `rootAssembly.documentMicroversion` 后，
  之后**所有请求都改用 `/m/<microversion>`**；**版本引用**直接用 `/v/<version>`。因此链上每个字节
  都属于同一个不可变修订。
* **依赖身份**：每个实例必须与根**同文档、同配置、同微版本**，否则明确拒绝——
  `onshape_foreign_document`（linked document）、`onshape_revision_mismatch`（混修订或缺微版本）、
  `onshape_identity_collision`（未请求的配置）。缓存名是单文档单配置命名空间，不做静默重命名。
* **缓存请求绑定**：新写入的缓存会在 `json/_requests.json` 里记录每项的 `(path, query, sha256)`；
  读取时逐项核对，同一文件名对应不同请求或内容摘要不符都会报 `onshape_cache_identity_mismatch`。
* **旧缓存（legacy）**：`tools/onshape_export` 时代的缓存没有索引，仍可离线重放，但每一项都会
  记为 `unbound`，`capture.cache_binding = "legacy_unverified"`，`identity.revision_locked = false`，
  并追加 `cache_request_identity_unverified` 与 `revision_not_locked` gap——不伪称已锁定。
* **清单字段**：`identity.revision_locked` / `identity.revision_evidence`（微版本、探测请求、
  已钉住请求列表、元素级 microversion）与 `identity.request_bindings`（每项 path/query/摘要 + 汇总摘要）。
* **采集时刻**：`capture.at` 只表示"这批字节的采集时刻"，并附 `at_source`
  （`caller` / `cache_capture_record` / `cache_source_json` / `run` / `unrecorded`）——
  回放不会把运行时刻写成采集时刻，查不到就如实记 `null` + `unrecorded`。
* **原始记录与本次运行分开**：`identity.snapshot_origin.recorded_capture` 只报告原始记录里**确实有**的
  字段（mode、tool_version、at…；缺失就不出现，不补本次的值）；`identity.this_run` 单独记录本次
  的 `method`、当前 `tool_version`、实际运行时间 `at` + `at_source=run` 与请求验证情况。
  `capture` 里还分三层：`origin`（历史原记录，不变）、`declared`（调用方在 `config.capture` 里的
  显式选择/补充 + `reason`）、`effective`（两者合并的生效值，兼容字段用它，但绝不回写 origin）。
  `this_run.transport` 为 `api` / `cache_replay` / `mixed`：混合缓存与 API 时只有
  `network_requests` 列出的项算本次采集，`this_run.capture` 记录本次采集身份（mode/工具版本/时间）。
* **依赖闭包**：实例必须带 `elementId` 且类型为 `Part`/`Assembly`；缺失或未知一律报
  `onshape_dependency_incomplete`，否则整棵装配子树可能静默消失。

`partId` 只在**零件工作室内**唯一。质量属性读数在生成侧与 oracle 都按 `(element, partId)` 作用域读取，
跨工作室同名零件各用各的读数，不会互相覆盖。

但**几何与缓存文件名仍以 partId 为全局键**（`geometry/parts/<safe_name(partId)>.stl`、
缓存里的 `bytes/stl_<safe_name(partId)>.stl`），所以下列三种情况一律 fail closed
（`onshape_identity_collision`），不做静默 last-wins：

1. 同一个 `partId` 出现在两个零件工作室且两边都有几何；
2. 两个 `partId` 净化后同名（例如 `PART-B` 与 `PART/B` 都落到 `PART_B`）；
3. 同一个元素在同一次冻结里被多个配置引用（缓存文件名不含配置）。

缓存目录不记录配置，因此不同配置请使用不同的 `source.cache` 目录；快照的
`identity.configuration` 只声明本次冻结请求的配置。跨文档的同名元素同样不受保护，需要落到
各自的快照里分别冻结（完整的多文档命名方案不在本层范围）。
工作区修订变化后也应使用新缓存目录；已有不可变响应与新请求不符时会明确拒绝，不能覆盖旧身份。

### 规范化规则

* **分组**：定义里列出的成员合成一个刚体；没被覆盖的物理零件、被重复分配、或引用了不存在
  的实体都会报 `onshape_attribution_mismatch`。
* **融合守恒**：质量求和、质心按质量加权、惯量按 `Σ(R·I·Rᵀ + m·平行轴)` 合成完整张量，
  再旋转到 link 局部坐标系；密度假设按质量比等比缩放质量与惯量，形状与质心不变。
* **坐标系**：URDF 里子 link 在 q=0 的坐标系**就是**驱动它的关节坐标系，因此子 link 帧取
  mate 连接器在世界系下的位姿（原始证据可直接测），根 link 帧取定义里的参考实体。
* **网格放置**：每个成员网格按"成员实例 → link 帧"的相对刚体变换放置，与惯量使用同一套变换。
* **运动链**：唯一根、每个 link 最多一个父、无环、全连通在返回前强制检查（`onshape_definition_invalid`）。
* **不补默认值**：`effort`/`velocity`、`collisions` 保持缺失；每个关节的
  `provenance.effort_velocity = "not_defined"`，`conventions.collision_policy = {"policy": "undefined"}`。

当前资格边界见[验收状态](../validation.md)。
来源缺 effort/velocity 时，必须补充额定参数依据；不能填 0 或引擎默认值来通过 URDF 检查。

## 不猜不补：`provenance.gaps`

| `kind` | 含义 |
|---|---|
| `mass_reading_missing` | 质量属性里没有该零件的质量（`inertial` 留空） |
| `inertia_reading_missing` | 有质量但缺惯量或质心读数 |
| `geometry_missing` | 该零件没有可用网格（`visuals` 留空） |
| `geometry_invalid` | 网格字节未通过公共 STL 接受标准；字节仍作证据保留，但不成 visual |
| `gltf_unavailable` | glTF 端点不可用，已回退旧缓存的 per-part STL |
| `gltf` / `gltf_split` / `geometry_unmatched` | glTF 缺失、拆分失败或有零件未匹配 |
| `joint_limits_missing` | 可动 mate 在 features 里没有限位 |
| `joint_child_unresolved` | mate 另一端不是可解析的叶子零件 |
| `mate_parent_unresolved` | mate 第 1 端不在实例树里 |
| `mate_frame_unresolved` / `frame_unresolved` | mate 未解算，取不到坐标系 |
| `mate_type_unsupported` | `CYLINDRICAL`/`BALL`/`PLANAR`/`PARALLEL` 暂无对应关节语义 |
| （被抑制的实例不再记 gap） | 见 `provenance.non_physical.suppressed`：保留存在性、不进模型 |
| `capture_provenance_missing` | 缓存没有采集声明，只能记 `imported` |
| `unresolved_occurrences`（`provenance` 字段） | 实例路径在响应里找不到对应实例 |

## 错误码

| 码 | 触发条件 |
|---|---|
| `onshape_reference_invalid` | URL/显式 ID 不完整，或同时给出工作区与版本 |
| `onshape_credentials_missing` | 需要访问 API 但没有可用凭据 |
| `onshape_api_error` | HTTP 错误（`detail.status`；402 会给配额提示） |
| `onshape_api_unavailable` | 离线模式发起请求，或网络重试耗尽 |
| `onshape_cache_miss` | 缓存缺条目且没有可用的 client |
| `onshape_snapshot_incomplete` | 目标目录非空、缺少清单、来源类型不符、capture 声明非法 |
| `onshape_snapshot_tampered` | 清单与文件不一致（缺失、改写、多出文件） |
| `onshape_scene_invalid` | 派生场景不符合公共 `description.scene/v1` schema |
| `onshape_definition_invalid` | 机器人定义非法（未知键、坏标识、重复/悬空 link、运动树不成树、mate 选择器不唯一） |
| `onshape_dependency_incomplete` | 实例缺少 `elementId` 或类型未知，依赖闭包无法保证完整 |
| `onshape_attribution_mismatch` | 定义与快照对不上（漏覆盖、重复分配、未定义的 mate） |
| `onshape_evidence_missing` | 规范化缺少必要证据（原始响应、质量读数、体积）；排除项证据不在采集层或没绑定到该实体时同样报错 |
| `pipeline_shared_helper_missing` | 集成环境缺少公共 snapshot helper 或模型 schema |

## 真实性与边界

- 仓库夹具 `tests/fixtures/onshape/cache`（2026-09-17 抓取）只用于离线回归，重放时按
  `fixture` 记录；真实缓存（含 34 个零件网格）重放时按 `cad` 记录，两者不可互换。
- 适配器**不**校验物理合理性（惯量正定、运动树连通、限位区间），那是规范化与质检层的
  职责；这里只保证结构合规、数值来自来源、缺口可见。
- 不在来源里的量（effort/velocity、碰撞策略、材质密度、零位与驱动极性）不会补默认值，
  一律记 `not_in_source` 或出现在 `gaps`，由机器人定义显式提供。
- 迁移自旧工具的 `assembly.py` / `linalg.py` / `geometry.py` / `stl.py` 已并入本包，
  不再修改第三方模块全局状态，也不修改 `sys.argv`。

## 测试

```bash
python -m unittest discover -s tests -t tests -k sources.onshape
```

覆盖来源身份解析、缓存与取数通道、错误码映射、场景派生（命名/读数/缺口）、
公共 schema 门禁、清单篡改检测，以及仓库夹具的端点重放与可复现性；
端到端数据流见图 `tests/sources/onshape/test_replay.py`。
