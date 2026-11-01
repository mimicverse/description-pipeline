# MJCF 第二轮重构与优化方案

日期：2026-11-01。起点：`7a2d6b2cf0ad53c442e1206f2a95a95060d0c0d9`。用户已授权制定方案后进入 Goal 执行。本方案补充 `mjcf-integration.md`，以本轮复审的真实反例为验收起点。

## 目标与边界

完整成功继续同时交付 URDF/MJCF；MJCF 可引用原始已验证输入单独重跑，且现有公共 CLI 与 Airflow 阶段可独立启动。所有终态由既有 FactStore 决定，原始失败与来源字节不可变。只在 `/home/andy/Work/description-mjcf-dev`、`feat/mjcf-dev-integration` 与隔离开发资源执行；8878 仅按身份重启自有实例。生产服务、共享检出、CAD、main 合并及正式发行不在本轮执行范围。

不增加独立转换产品、任务表、状态文件、机器人模型或配置副本。原 URDF、碰撞策略、独立验证及双格式证书边界保留。代码、测试、日志和界面使用英文，方案与审查记录使用中文。

## R1：在共同引擎收敛取消终态

FactStore 保留提交时的取消门，并以明确的类型化结果通知共同引擎；不按错误字符串判断。已执行的转换或验证结束、实际资源已结算后，由原 claim/fence 保全候选及失败上下文，提交一次 cancelled 决定并关闭原执行权。执行前与执行中取消共用取消记录与终态生成逻辑，消除另行抢占 claim 的失败路径。

同时覆盖成功候选、失败候选和无法封存的失败结果。取消与提交之间的竞态仍由原数据库锁串行化；已完成决定不改写。UNKNOWN/冲突效果先对账，不能借取消隐藏外部结果或宣称运行中的子进程已停止。排查 Generate、Verify、Publish 等共同引擎消费者，修复落在共同职责处。

验收：原活动租期反例得到 cancelled，claim 关闭，重复执行返回原决定，候选可追溯；取消晚于完成返回原成功；UNKNOWN 不能终态取消；错误 fence 与其他执行者不能关闭当前 claim。真实转换/验证与 PostgreSQL 分支必须运行。

## R2：按实际阶段装配启动能力

继续使用原 CLI、StageApplication、StageTaskOperator 与共同引擎。启动先读取已登记 command/admission，按实际目标阶段或本次执行范围装配处理器及必要端口。MJCF、Generate、Verify 的本地执行不加载 CAD endpoint/token 或发布客户端；Publish 只要求发布能力。Discover/Capture 保留实际原生端口与身份门。

将当前混合的“接纳新输入的现场身份核验”和“读取既有封存来源”拆回各自职责。封存输入、命令、来源身份、executor 与资格仍必须完整绑定；不以跳过资格换取独立启动。能力选择是临时组合，不另存可变模块 registry 或第二份运行计划。同步 CLI、Airflow、对账与取消入口，删除无条件完整装配和非必要目录初始化。

验收：全新进程用现有 CLI 单跑合法 MJCF 命令，无 CAD endpoint/token、无可用原生端口，实际通过独立验证；不装配上游或发布处理器。真实原生阶段缺必要端口仍明确失败。不能仅以注入完整 application 的测试宣称默认启动通过。

## R3：统一执行范围与复用来源

从唯一不可变 admission.from_stage 与原始来源引用推导本次执行阶段；全流程七阶段，MJCF successor 为 MJCF/Publish。共同投影给出本次范围与复用引用，CLI、门户、查询和调度共用。复用事实与来源决定有明确所有者，不把源运行的 command/decision 伪装成新运行自己的决定。

删除门户对 in_scope 的二次补判与独立范围规则。保留查看原始来源证据的能力，门户只渲染已核验的来源引用和共同范围。无本次执行的上游不能显示 waiting/blocked 而又属于本次执行范围，也不能获得本次业务成功。

验收：共同查询与门户的实际执行范围均为 MJCF/Publish；上游无新命令、claim、决定或外部 effect；完整正常运行仍含七阶段；非法来源、来源未决、后继链和诊断来源继续拒绝。

## R4：让 Airflow 跳过复用上游

保持一个 DAG 和七个工程阶段。调度依据共同范围，使复用上游为 skipped，实际业务处理仅执行 MJCF/Publish；不再逐个运行五次完整应用和来源资格重建，也不把 reused 包装成五次 success。优先利用现有阶段适配器的早期范围判断和 Airflow trigger rule，不新增控制 DAG、持久化调度状态或纯转发层。

正常全流程的前驱成功门仍生效；即便外部误标 skipped，StageApplication/FactStore 仍要求实际前驱决定，不能由调度状态授予资格。所有实际运行阶段继续在 claim、提交与 Publish 边界重验必要来源。

验收：真实隔离 Airflow 本地组合显示上游五任务 skipped，MJCF/Publish success，数据库仅两个新阶段决定；native provider 与上游 handler 设为调用即失败仍不触发。失败/取消/未决不会误放行 Publish。

## R5：删减核验、固定候选与开发部署

逐项列出删除的重复范围解释、无条件能力装配、未使用入口和测试夹具补丁，附可执行 rg 命令及实际调用证据。只新增能覆盖原失败和相邻边界的必要测试；继承未变化且适用的几何、参考与浏览器证据。固定实现后运行适用回归、静态检查、包资产字节核验、冷启动 CLI、真实 Airflow 与开发浏览器验证，并更新 Draft PR #184。

项目规则将 `docs/design.md`、`docs/contracts.md`、`docs/deployment.md` 保留给总负责人。本轮提供准确的待同步差量和验收证据，不代写这三份文档，不删除或跳过现有文档契约失败门。该外部集成门、独立固定候选审查和正式发行仍明确未完成；开发 Goal 的完成条件是 R1–R4 原失败闭环及 R5 可评审开发交付，不能表述为正式上线或全部发布验收通过。

## 执行次序与记录

依次执行 R1 → R2 → R3 → R4 → R5。每项先保全原失败，形成实现和针对性验证，再进入依赖它的下一项。发现新的同源问题须追踪全部消费者；不为进度截止降低质量，也不重复没有变化的全套检查。证据保存在本 worktree 的 `.dev/refactor-20261101/`，前轮复审原件保留在 `.dev/review-20261101/`。

当前状态：前轮R1–R5开发交付已完成；用户追加的权威文档/独立审查/CI收口正在执行，三项实际发现修复后需在固定新集成提交完成验证及差量独立复核，PR保持Draft。

## 已完成的实现与删减

| 项目 | 结果与必要性 | 实际调用/验证 |
|---|---|---|
| R1 | FactStore 类型化取消通知；共同 engine 使用原 claim/fence 提交 cancelled，保全候选及原失败。通知只是进程内控制流，不增加持久状态 | 15项取消回归、44项共同回归；真实MJCF活动租期反例收敛并可重复读取 |
| R2 | 原 CLI/Airflow 按登记目标临时组合所需 handler；只有 Discover/Capture 加载CAD，只有Publish加载Git/PR | 新进程默认CLI在无CAD环境完成MJCF；25项能力回归通过 |
| R3 | `execution_stage_ids` 唯一推导范围；共同查询核验来源并给出source/reuse，当前上游command/decision为空 | 66项共同范围、Portal、状态及真实七阶段组合通过 |
| R4 | 原DAG七任务，MJCF使用none_failed，复用前缀由实际调度器skip；删除ReusedStage伪装success分支 | 默认工厂实际Airflow五skipped、两success；32项适配及12项实际调度相关回归通过 |

新增复杂性分别用于关闭原取消竞态、解除冷启动的无关端口依赖、表达跨运行来源所有者及兑现实际调度范围；没有新增运行表、调度状态、机器人模型或配置副本。转换、独立验证、FactStore和已有端口继续各负其责。隔离Airflow驱动属于测试，不是新生产入口。

删除核验的原始命令/返回见 `.dev/refactor-20261101/R5-DELETION-AUDIT.json`：

```bash
rg -n 'reused_from|_mjcf_input_projection' src/description_pipeline/orchestration/portal.py
rg -n 'ReusedStage' src/description_pipeline/orchestration/airflow_stage_operator.py
rg -n 'execution_stage_ids' src/description_pipeline/stages.py src/description_pipeline/pipeline/contracts.py src/description_pipeline/pipeline/application.py src/description_pipeline/platform/queries.py
rg -n 'StageCancellationRequested|_commit_owned_cancellation' src/description_pipeline/pipeline/contracts.py src/description_pipeline/pipeline/engine.py src/description_pipeline/platform/facts.py
```

前两条exit 1且无匹配，证明旧重复逻辑已删除；后两条分别11处实际定义/调用，不以文档宣称代替检查。Portal保留的资源清单辅助函数只处理文件闭包，不判断执行范围。开发启动器也删除共享claim_policy手工替换，实际装配记录为Freeze空handler、MJCF单handler、Publish单handler；与生产默认工厂相同。

## 权威文档待同步差量

建议补丁为 [authority-sync.patch](../reviews/mjcf-refactor-20261101/authority-sync.patch)，包含三份权威文档及 `tests/v1/test_stages.py` 原断言的准确七阶段、MJCF输入输出/QC、受限后继、范围、默认装配、取消和资产验收差量。`git apply --check`通过，隔离副本的原文档契约检查通过。补丁尚未应用到当前文档/测试；它是交给负责人的可审阅差量，不授予批准、合并或正式发行资格。

## 完整回归与结果展示修复

`f129c3de` 的标准完整入口实际运行2824项：2812通过、10因平台/隔离条件跳过、1文档契约失败、1门户旧测试错误，原始日志为 `.dev/refactor-20261101/FULL-REGRESSION.log`。不改写该结果为全绿。

门户旧测试仍要求从父artifact推断reuse，违反本轮共同查询的唯一职责。已将测试改为断言“无共同reuse则不推断、有共同reuse则原样展示”，保留foreign producer拒绝；真实来源完整性继续由PostgreSQL/真实门户回归覆盖，不恢复被删除的门户第二判定。

最终浏览器/API审查又发现成功报告把五个复用来源算作本次完成，显示7/7，实际共同范围为MJCF/Publish。根因是 `report_view.build_report` 的显示计数遍历所有来源证据，原测试仅核通过状态及逐阶段reuse，没有核总数。原件为 `HEADLINE-BEFORE-RESULT.json` 和 `DEVELOPMENT-DOM-BEFORE-HEADLINE.txt`；同一真实门户测试新增本次2/2断言，在原代码稳定失败 `REPORT-SCOPE-REGRESSION-BEFORE.log`。修复使用已有in_scope做展示计数，成功摘要简化为 `Completed; independent verification passed`；不新增状态或范围规则。49项报告、Portal投影和真实PostgreSQL门户回归通过 `REPORT-SCOPE-REGRESSION-AFTER.log`，包含完整七阶段计数、失败及来源拒绝等相邻消费者。

产品固定提交 `04931f18` 通过Ruff、diff检查和建议补丁可应用性检查。开发wheel的147个源码/JS/WASM/许可证/契约资源与当前源码逐文件一致，RECORD逐项验证，记录在 `WHEEL-IDENTITY.json`。源投影、独立检查与viewer五个核心文件及参考40输入保持原字节，见 `REFERENCE-INHERITANCE.json`，因此继承已有19项参考模型检查，不无意义重复全套几何基准。

上述完整入口与49项差量验证应分别引用；最终head的CI按PR实际结果记录，不能把继承证据冒称最终全套全绿。三份权威文档及其测试保持原样，当前唯一已知未关闭的测试门是六/七阶段文档契约。

## 最终开发验收

最终浏览器运行 `portal-mjcf-50428384-2746-4f67-ac71-05f08e2bcd00` 在产品提交 `04931f18` 完成，实际默认装配为空/MJCF/Publish；上游五task skipped，MJCF/Publish success，数据库仅新增两阶段决定。真实API回读为本次2/2，源五阶段保持Reused。最终候选再次实测碰撞视图、0.4rad调姿、1ms步进、overlay与复位；播放/暂停已在同源码viewer的前次本轮运行实际验证，未变化证据继承。截图为 `.dev/refactor-20261101/FINAL-DEVELOPMENT-MJCF.png`，明确为两body离线开发夹具，不冒称用户CAD模型。

[证据摘要](../reviews/mjcf-refactor-20261101/DEVELOPMENT-EVIDENCE.json) 绑定产品提交、真实运行、完整/差量回归范围、wheel、原始日志及页面SHA。本轮开发构建只用于核字节，正式发行仍须干净main完整构建。最后的文档和证据提交不改变上述产品源码，最终wheel会再核对同一字节。

开发实例为自有8878/PID707888；隔离检查证明生产8791/PID1766、8971/PID1796、8793/PID2734不变，三份权威文档及原契约测试原字节不变。旧实例和数据库已按身份核对、无活动任务、保全CAS/metadata/SQL后停止；不删除失败证据或用户输入。不要将该隔离交付视为生产上线。

## 用户追加的合并前收口（2026-11-01）

负责人实际回读原始用户消息并形成 `/home/andy/Work/reviews/mjcf-root-system-review-20261101/ROOT-DESIGN-DECISION.json`：确认完整成功必须双格式，允许受限MJCF新后继直接复用原始已结算Verify前缀。原工程request/URDF证书字节不改；本次转换request/summary/simulation/Publish intent是分别封存的现有事实。根亲写的d907e149、7a265112已重放，三权威文档和配套断言同步，不机械采用前轮提案。

三条固定字节独立审查的结论为CHANGES_REQUIRED，原件保存在根的mjcf-root-system-review-20261101目录，不能用过程测试代替最终ALLOW。统一修复经ROOT-FIX-CLOSURE-ACTIVATION.json激活本worktree唯一写者，并保留生产/CAD/main/新平台run禁区。

| 发现 | 根因与原验证缺口 | 修复与必要验证 |
|---|---|---|
| CLI admit加载无关原生能力 | 默认适配器先装完整运行环境再解析spec；此前新进程只测已登记stage，后继接纳使用注入完整应用 | 先解析AdmissionSpec，接纳只组合事实/产物能力，初始target绑定保留；真实新进程无native配置/token/snapshot完成新的合格后继接纳、幂等读取及MJCF阶段，坏来源拒绝且不登记 |
| 大网格支撑配置内存失败 | `_minimum_support`构造全部顶点×全部法向矩阵，原参考需2.36GiB超过既有2GiB；旧参考脚本用无界入口，小夹具才测隔离入口 | 在同一函数对全部数据有界分块归约，不放宽资源/误差、不删supports；分块边界与34项投影/handler回归，并在原40输入、原SUPPORT-CONFIG实际project_isolated+19项检查闭环 |
| 通用checker漏geom所属body | 零位几何和名称核对不足以证明运动归属；未命名相同surface可在零位等价错挂 | 共同instance门核实际geom_bodyid与源link，visual/collision及primitive/mesh四组合都在geometry.instances拒绝；证明零位等价但0.4rad运动偏差，原URDF字节不变。现正式原生链唯一名称不变量仍保留，不夸大可达范围 |

旧49d源码上的新增工程反例实际5项失败，原CLI接纳新进程因native配置缺失失败，日志均保留在 `.dev/closeout-20261101/`。首轮新测试有碰撞geom命名、target夹具错误，已保全并纠正；不把测试夹具错误算作产品发现。修复覆盖共同装配入口和共同实例门，不为模型/数量/路径特判，也不新增状态或执行计划。

开发分支已合入精确main3da83503，保留R01测试组合辅助迁出、R08原生读取错误门及元数据类型保真；共享main未写。最终固定集成提交需跑受影响的真实组合、原参考有界重放、完整适用CI、包字节检查并交根亲审和独立差量复核。最终结果只在该固定头记录；本文不预先宣称这些门已通过。GUI布局由根的独立description-gui-clarity工作树负责，本轮不改static/index.html/style.css/app.js或viewer布局。
