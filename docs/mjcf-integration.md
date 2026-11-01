# URDF pipeline 集成 MJCF：当前设计与验收边界

更新：2026-11-01。本文是 MJCF 集成提案与开发记录；第二轮执行方案见 [mjcf-refactor-plan.md](mjcf-refactor-plan.md)。用户已授权按 Goal 在独立 worktree 执行。产品实现和开发验收不等于正式发行，权威文档同步及正式发布门见文末。

## 需求与阶段边界

完整成功同时交付 URDF/MJCF。MJCF 是独立阶段，能够从全新 CLI/Airflow 进程单独启动，并复用已验证 URDF 单独重跑；不重新操作 CAD 或生成 URDF。转换、碰撞代理和独立验证属于同一步，失败阻止组合发布。

```text
Freeze -> Discover -> Capture -> Generate -> Verify -> MJCF -> Publish
```

| 模块 | 唯一职责 |
|---|---|
| Freeze / Discover / Capture | 既有原生输入、发现与采集；不为 MJCF 增加 CAD 读取 |
| Generate / Verify | 唯一 EngineeringModel、URDF 与网格；从 raw 独立验证实际 URDF |
| `mjcf.py` | 从实际 URDF、组件身份及策略投影碰撞代理和 MJCF，不拥有状态 |
| `verification/mjcf.py` | 从实际输入和输出另行复算、加载引擎并检查，不借生成器内存答案 |
| MJCF handler | 有资源上限的投影与独立检查进程、候选、证书与失败保全 |
| Publish | 重验两个证书和组合字节，交付同一个受控 Git 提交及 PR |
| StageApplication / engine / FactStore | 接纳、阶段命令、执行权、不可变决定、取消及 UNKNOWN 对账 |
| Portal / Airflow | 渲染共同查询与调度实际范围，不拥有第二业务状态机 |

不增加转换产品、任务表、状态文件、可变模块 registry、机器人模型或配置副本。正式运行、诊断资格及外部工程/硬件批准保持分离。

## 原始参考项目及采用依据

直接审查并运行的参考为 `mimicverse/description` 的 `work/mg-oct-14-train@3f07af6f0a1cc2dde6c3b2857b381113538c87cd`，本地只读副本 `.dev/reference-description`。历史 pipeline 迁移代码与 PR #55 只补充背景，不代替原始参考。

| 参考位置 | 已确认实现 | 接入决定 |
|---|---|---|
| `tools/generate_mjcf.py:95` | 从当前 URDF 直接生成，读取原始网格；不重放 CAD 导出 | 继承输入边界，交给现有 stage engine 负责新输出和不可变封存 |
| `tools/generate_mjcf.py:150` | 完整惯量旋转后用 NumPy 主轴分解，明确禁止几何推定惯量 | 继承物理表达；用独立张量还原核验当前锁定引擎 |
| `tools/generate_mjcf.py:171` | 保留每个 CAD visual 零件的 collider，脚部鞋与附件也分别保留 | 以按原生组件分开的凸包作为基线；必须处理当前 URDF 的材质分区，不能假定一个 visual 等于一个零件 |
| `tools/generate_mjcf.py:70` | 从世界坐标的原始三角形找整脚下方的支撑面，生成 site 与方向 | 迁移几何支撑面能力；移除脚部名称硬编码，明确中心规则和来源 warning，独立复算 |
| `tools/generate_mjcf.py:200` | `robot.xml` 与 `scene.xml` 分开，地面取支撑几何最低世界高度 | 继承分离与贴地语义，不抬机器人、不改零位；支撑 link 使用明确绑定 |
| `tools/verify_mjcf.py` | 独立 FK、惯量、collider、site、限位恢复力、地面接触和被动步进 | 迁移有效验收思路；生产门使用显式失败，不沿用可被优化模式关闭的 assert |
| `tools/test_mjcf.py` | 重放、输入限位传播，以及轴/惯量/collision/site 变异检测 | 复用这些反例的工程目的，并改为通用夹具，不带入固定模型值或名称 |
| `docs/mjcf-generation-guide.md` | 原生 viewer；碰撞预览启用真实凸包显示，同相机对照 visual/collision | 继承显示语义；浏览器实现借用固定版本官方 WASM，不能把原始凹表面冒充碰撞凸包 |

参考实现未使用 CoACD。新增凸分解必须先与参考的组件凸包基线比较，并以确有必要的接触保真或成本收益证明其适用范围；不默认对所有网格执行复杂分解。

参考还包含仅支持特定根名、全部 revolute、两只固定名称脚部、每 link 惯量和 binary STL 的限制，并直接写既有输出。借鉴算法与验收语义，不能照搬这些限制、写入方式或模型参数。参考预览图片也有更新滞后的明确记录；新预览必须绑定当前实际交付字节。

## 输入、产物与摘要

机械值读取 URDF；`model/robot.json` 仅提供 visual 到原生组件的来源身份，不能覆盖几何、位置或物性。命令绑定精确 Generate/Capture/Verify artifacts、通过证书、仿真策略、实际工具身份、用途及来源闭包。资源须在受控闭包内，拒绝缺失、包外、错绑和字节漂移。

```text
mjcf/robot.xml
mjcf/simulation.json
mjcf/collision/*.obj
mjcf/scene.xml                   # present only when requested by the sealed policy
reports/mjcf-certificate.json
```

MJCF artifact 只封存新增文件，visual 资源引用最终交付的 `../meshes/`；运行时物化受控输入视图。最终交付自包含，沿用原 input/evidence/model/URDF/meshes/tool，并增加 MJCF 资源。原 URDF、网格及原始终态不改写。

三个摘要域分别是原 URDF Verify subject、MJCF 转换 subject 和组合 publication subject。原 Verify 不扩张为整个双格式交付的证书；Publish 重验两证书、精确来源及实际复制/提交字节，最终只有一份交付 manifest。warning 在模型、证书、预览及 PR 使用同一已核验库存。

## 碰撞代理与仿真策略

策略权威在 `mjcf_contract.py`，当前 config schema 2。默认 `auto` 优先保留 URDF collision；没有 collision 时按明确来源从 visual 派生凸代理。同一原生组件的材质分区先在 link 坐标中重组，不按颜色或 mesh ID 决定物理分区，不跨运动关节合并。

简单基本体保留；mesh 使用 MuJoCo 3.13.0 的受控凸包，默认256顶点、相对源组件凸包 `0.00025 m` 独立误差门。该门不是凹 CAD 表面等价或硬件接触资格。参考八个超过256顶点的凸包中，完整顶点距离最大约 `0.000105521 m`，构成当前预算的样本依据；每次转换仍需独立核验，不能自动放宽。

每个分量独立成为 asset/geom。共享 mesh 的每个实例均核验变换；不会按资产去重而漏掉第二实例。代理不能推定质量、COM 或惯量。算法、预算、接触参数、base、scene、site 和展示常量均明确标为策略派生。自碰撞依据引擎规则和显式策略，不因零位相交自动删除碰撞对。

CoACD 1.0.14 已在中性凹槽样本评估：组件凸包会封槽，三个凸分量保留已测接触探针；更细阈值产生278分量。它尚未进入默认生产路径；确有凹槽/间隙保真需求时应以接触及成本证据选择，不能声称现有凸包已覆盖凹形保真。

投影与独立检查分别在受限子进程运行，具备CPU、内存、时限和实际退出状态检查；超时、错误退出、退化几何、误差超标或封存失败保留原件及原因。不能用有效 JSON、预算耗尽或局部抽样冒充成功或完整最大误差界限。

## 独立验证与预览

独立证书通过需完整19项检查，覆盖输入/工具/策略闭包、引擎加载、拓扑与自由度、零位及非零 FK、有向轴、逐 link/整体质量和完整惯量、几何及重复实例、碰撞代理、限位/恢复力、配置/场景、支撑接触、短时步进、warning 和结束后字节稳定性。`engine.process` 仅为失败证据，不能重签成通过证书。

惯量主轴表达由独立还原张量检查；限位恢复力在源界外各偏移0.02 rad或m实测。接触探针在私有 MjData 中移动地面实际位置后调用碰撞计算，不用修改静态 geom_pos 冒充实际接触变化。短时稳定、加载或 PR 不建立控制、训练、站立及硬件资格。

门户从已登记证书与实际 artifact 构造预览，每次文件请求重验授权与SHA，浏览器再次核摘要。URDF 与 MJCF 共用入口/renderer；切换先释放旧模型、监听和 Worker。MJCF 使用固定官方 MuJoCo 3.13.0 WASM，在专用 Worker 中加载实际交付 XML/资源，支持 visual/collision/overlay、link 选择、frames/COM/contact、关节调姿、播放/暂停、单步和复位。碰撞显示来自引擎接触代理，交互不改交付。

默认显示运行结果、阶段摘要与预览；原始证据、warning 来源及高级策略折叠。MJCF 失败阻止完整成功，已验证 URDF 仍可查看。没有通过证书的产物不能展示通过资格；下载 robot/scene XML 经同一授权与字节门，完整资源随同一仓库交付。

## 单独启动与单独重跑

| 请求 | 行为 |
|---|---|
| 新 CAD 输入或原机械语义改变 | 正常 Freeze 全流程 |
| 相同幂等键重试 | 返回/恢复原请求，不创建第二运行或 effect |
| 有意重算 MJCF、修改仿真策略 | 新不可变 `from_stage=mjcf` 后继 |
| 原运行 MJCF/Publish 失败、上游仍有效 | 复用原始已验证前缀，不继承失败下游 |
| 未验证、来源冲突、缺失字节、诊断来源、effect 未决 | 明确拒绝 |
| 已有终态 command 再提交 | 复核返回原决定，不删旧决定强制重算 |

后继始终直接引用真正持有 Verify 决定的原始运行，不形成 MJCF 后继链。保留所有者、用途、规则、配置、来源与上游 producer/executor，策略覆盖封存为新请求。接纳、claim、candidate 提交和 Publish 分别重验来源及结算。

`execution_stage_ids` 从不可变 admission 推导范围。共同查询给出 `execution_scope`；复用上游为 `state=reused / in_scope=false / command=null / decision=null`，真实来源在 `source/reuse`。Portal 仅渲染，不将源 command/decision 冒充本次事实。

既有 `description stage COMMAND_ID` 及 Airflow 工厂先读取登记目标，只装配所需端口。Generate/Verify/MJCF 无 CAD endpoint/token 可启动；Discover/Capture 仍要求真实原生端口；Publish 才加载发布能力。能力选择只在进程内，不保存第二份计划。

DAG 保持七任务；仅 MJCF 使用 `none_failed`，其他为 `all_success`。MJCF-only 的 Freeze 在共同范围核验后 skip，四个后续上游由调度器 skip，实际执行 MJCF/Publish。误标 skipped 不能绕过共同引擎的真实前驱资格。

## 取消、失败与唯一状态

执行中取消在原 claim/fence 内收敛。FactStore 在提交事务内发现取消，以类型化 `StageCancellationRequested` 交回共同 engine；engine 保全候选/StageResult artifact、原始失败及保留路径，写入一次 cancelled 决定并关闭原 claim。通知不持久化，不另抢执行权。完成后的取消不改成功，重复执行返回原终态。

UNKNOWN/CONFLICT 必须先按原 effect 对账；取消不能掩盖外部结果或宣称子进程已停止。跨运行锁统一先取来源到当前 run，再取根到叶 command、effect及claim，避免来源读取与后继提交反向等待。原死锁、取消失败和夹具失败证据均保留，不改失败运行强制重跑。

## 隔离环境与证据

开发 worktree `/home/andy/Work/description-mjcf-dev`，分支 `feat/mjcf-dev-integration`，入口 `http://127.0.0.1:8878/_development`。数据库、Airflow metadata、CAS、work root、cookie、依赖、日志和进程均独立。开发身份与离线CAD夹具只在忽略的 `.dev/dev_runtime.py`，不进入发行包；Git为本地仓库，PR传输受控，不触达真实目标。

本轮关键证据保存在 `.dev/refactor-20261101/`：

| 证据 | 证明范围 |
|---|---|
| `R1-PROBE-RESULTS.json` / `R1-REGRESSION.log` | 真实转换取消闭环、原fence、失败与相邻边界 |
| `R2-PROBE-RESULTS.json` / `R2-CAPABILITY-AFTER.log` | 无CAD环境的新进程默认CLI实际完成，不是注入完整应用 |
| `R3-COMMON-SCOPE-AFTER.log` | 共同范围、来源、Portal及七阶段组合 |
| `R4-AIRFLOW-RESULT.json` / `R4-ACTUAL-SCHEDULING-REGRESSION.log` | 实际Airflow默认工厂：五skipped、两success、两个新决定 |
| `DEVELOPMENT-LAST.json` / `DEVELOPMENT-RERUN-PROJECTION.json` | 浏览器提交及真实本地DAG.test，实际装配空/MJCF/Publish，无CAD环境 |
| `R5-DELETION-AUDIT.json` | 门户重复范围与Airflow伪成功分支已删除，真实共同调用 |
| `AUTHORITY-PROPOSAL-CHECK.log` | 权威文档和契约断言的隔离建议副本通过，未修改原文档 |

参考完整模型的19项检查、40个输入SHA不变、惯量/支撑/接触/限位及原生/WASM对照记录继承 `.dev/m1/`、`.dev/m2/` 中未变化且适用的证据。完整回归、最终开发截图和包字节核验以本轮执行方案的最终记录为准，不把继承结果称为最终全套通过。

## 待集成与正式发布门

[Draft PR #184](https://github.com/mimicverse/description-pipeline/pull/184) 保持Draft。`docs/design.md`、`docs/contracts.md`、`docs/deployment.md` 依项目规则只由总负责人维护；[精确建议补丁](../reviews/mjcf-refactor-20261101/authority-sync.patch) 同时包含三文档及原契约测试的七阶段/范围差量，当前工作文件不代改、不skip其失败门。

独立固定head审查、负责人文档同步、必需CI、唯一发布者match-head合main、干净main完整发行、双端实际身份、正式CAD GUI七阶段、独立Verify、双格式预览/PR及清场仍须按原权限执行。离线夹具、CLI、开发门户和本地DAG.test不能替代正式调度器服务或CAD E2E。本轮不操作共享检出、CAD、生产、main或正式发行。
