# 七阶段独立重跑核心实现

本报告由隔离实现者编写，仅说明实现、复现方法和证据范围，不是独立审查签核。
工作检出为 `/home/andy/Work/description-stage-reruns`，基线为
`f0b1d67a2373e15e4d402603e8ba31f3fb294b85`。公共 Portal、界面、runtime、SQL、配置与权威文档由根另行集成。

## 唯一执行边界

七个阶段均通过 `ProductionStageRerunRequest` / `load_production_stage_rerun` 接纳。
`from_stage` 是所选阶段；未提供 `through_stage` 时仅执行该阶段，可显式选择后续终点。
普通首次上传显式封存 Freeze 至 Publish。范围封存在唯一 admission request 内，参与原有摘要，
不增加 SQL 列、表、调度状态副本或执行模型。已存在业务记录缺少该字段时保留原字节与原摘要，
原范围仍为 Publish；这不加载或执行旧发行包。

仅 Freeze 起点可从封存原件中选择另一装配、从当前受控配置选择另一目标。
其他起点继承装配、目标和用途；仅 MJCF 起点可修改 simulation。
用户变更说明先经既有唯一函数规范为 LF，再封存；不回填旧 Capture 的原生说明。

## 模块职责

- `stages.py` 与 `pipeline/contracts.py` 负责范围、状态与资格派生；范围外旧结果不能变成当前交付。
- `pipeline/admission.py` 负责同一组纯 command builder、直接来源契约和真实 producer 前缀；不制造当前运行的复用 decision。
- `pipeline/application.py` 负责必要前缀解析、封存事实重验、只读选择建议及共同执行引擎组合。
- `pipeline/ingestion.py` 负责封存本次请求与实际所需环境能力，只有 Discover/Capture 范围读取新的 native requirement/target。
- `platform/facts.py` 复用原事务、命令、claim、effect 和 decision；来源归属按 admission ancestry 解析，准入不再依赖父运行整体终态。
- `platform/queries.py` 与 `run_ownership.py` 派生 executed/reused/out_of_scope 及真实来源身份，沿用同一封存事实。
- Airflow DAG 与 CLI 仅尊重已封存范围；独立入口不绕过领域资格或 claim/effect 边界。

准入仅消费必要的 completed、qualified 前缀。父运行无关后缀未结束不阻止离线重算。
涉及 native 或 Publish 的新范围，另沿来源链核对应外部命令的 pending、UNKNOWN 与 CONFLICT；
未知外部结果不能通过新 run 绕过；同一 helper 也在原有 canonical claim 事务中保护所有正式 native/Publish 执行入口。既有 admission 的事实回读不套新的动态执行门，已完成命令重试只回读原 decision。实际 CAD 仍受原 canonical native 资源机制控制。

## 来源与本次环境

非 Freeze 起点保留来源的工程 configuration / platform_config_sha256，
这两个字段不伪称本次部署配置。所选范围使用本次适用 contract、policy 和实际工具。
历史前缀依旧 admission、command、ArtifactRef、receipt 和实际 producer 原值重验。
当前平台配置只为实际所需 native target 与新的 Publish target/base/head/actor 提供执行信息。
TLS/路由、Git base、辅助配置或安装 generation 变化不能推断 CAD 原件已变化。

Verify 可用旧 Capture/Generate 执行当前策略并形成新证书。
Generate receipt 的原 contract 绑定由来源重验核对；新 Verify 按受支持输出 schema、精确输入和当前策略验证。
Capture 准入复用原相邻输入解析器；Generate 准入只读取当前支持的 Capture 原始合同，不转换模型。Verify/MJCF/Publish 共同使用实际 typed 输入或证书校验；不相容时定位应重算的来源阶段，不维护旧 hash 白名单。

本轮发现历史 MJCF 证明重验误用当前工具身份：`binding_for_projection` 原来无条件注入当前 tool/engine。
修复允许验证者显式传入已核证书中的封存身份，先核旧 tool.source_sha256 与旧 MJCF command executor，
再重算全部绑定文件。实际新 MJCF 的 execute 和 candidate validation 仍核本次真实工具与引擎。
此处是需独立审查的工程契约差量；没有改动网格算法，不替代 A 已固定的几何证据。

## 多跳并发锁序闭环

最终并发检查实际复现 A 完整运行 → B 从 Generate 延续至 Publish（已完成至 MJCF）→ C Discover 的死锁。
B Publish 与 C Discover 的外部工作互不重叠且应可取得各自 claim；旧来源归属解析却先锁 B→A，
另一命令链先锁 A→B。受控隔离 PostgreSQL 报 `DeadlockDetected`。
原始错误、复现脚本和该轮源码摘要分别保存在 `LOCK-DEADLOCK-FAILURE.log`、
`LOCK-DEADLOCK-REPRODUCER.py` 与 `PRE-LOCK-SOURCE-BYTES.json`。
原失败 FactStore 源码逐字节归档于 `PRE-LOCK-FACTS.py.txt.gz`，解压 SHA 已核对该轮记录。
复现脚本的屏障只针对该旧锁顺序，不能将其在修复后屏障失配解读为修复失败。

根因是 metadata 来源解析同时承担了锁操作，现有单链测试没有并发覆盖中间来源 owner 与真实 producer 分离的情况。
修复使 owner 解析只读取不可变 admission，所有需要来源锁的准入、命令链和活动检查，
共同从原 admission ancestry 按根至叶取得现有运行锁；不增加新锁种类、状态模型或重试绕过。
新增实际并发的 Publish/Discover 两 claim 测试，核两个权限均成功且首次取得的锁符合来源拓扑顺序。
原 claim/effect/fence 与 UNKNOWN 规则继续保留。

## 独立发现的恢复前缀闭环

独立复核 CORE-RERUN-02 在真实隔离 PG/CAS 中保全后临时移走父 Publish output：
Generate-only 子 admission 与必要前缀仍可读取，但恢复授权返回空；恢复无关产物后又可授权。
根因是 `recorded_canonical_projection` 在正确的子前缀投影之后，又对父运行完整范围重验。
本轮将第二次读取限定到所选阶段之前的同一 `_qualified_stages` 范围，继续使用原投影与 typed source parser，
不增加 projection 或身份副本，也不改 native/Publish 的相关外部结果门。

原错误及逐值证明保存在 `PREFIX-RECOVERY-FAILURE.log/json`；失败读取器字节归档于
`PRE-PREFIX-RUN-OWNERSHIP.py.txt.gz`。新增真实反例回归同时证明：无关 Publish 产物缺失仍可恢复，
必要 Capture 产物缺失仍拒绝，原件恢复后可恢复。最后这个小差量仅复验原 01/02 和 ownership 影响范围，
15 项通过；此前固定源码下 178 项核心、5 项真实进程与并发组合证据继承，逐文件未变范围见 `VALIDATION.json`。

## 删除与复杂性

删除 Freeze/MJCF 两套专用 request、loader、factory 方法及旧 Freeze 原件读取入口，
由同一范围请求和来源解析替代。删除只围绕旧专用入口的重复 fixtures；真实七阶段、链式来源、
幂等/篡改、取消、失败与 UNKNOWN 测试覆盖共同机制，不为旧 fixture 留兼容 API。

新增辅助逻辑仅承担三项现有类型无法表达的职责：真实 producer ancestry 解析、
只读 typed 输入资格及相关外部工作门。没有新增持久状态权威或跨代执行 fallback。
具体 `rg` 命令、状态和输出保存在同目录 `DELETION-EVIDENCE.json`。

## 实际验证与限制

最终测试命令、日志摘要和固定字节记录见同目录 `VALIDATION.json`。
动态测试使用真实隔离 PostgreSQL、ArtifactStore、canonical services/engine/handlers、
独立 Python 进程、本地 bare Git 和文件 PR fixture。
native 是受控 Archive/QualifiedFixtureBackend；它们不是真 CAD 或真实远程 PR。
本报告不声称正式 GUI 七阶段 E2E、部署、交付 PR、现场资源清场或发布完成。

公共历史 Portal/supervised fixtures 仍由根迁移。实现者不修改根专属权威文档，
不合 main、不部署、不触发生产 CAD，也不签核自己的补丁。
