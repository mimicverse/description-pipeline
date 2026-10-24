# Runtime effect port

## Purpose

`StageEffectPort` is the minimum common capability injected into every stage handler. It is
bound internally to one `StageCommand`, one current `ExecutionClaim`/fence, one claim-owned
workspace, the real `ArtifactStore` and the platform `FactStore`. A handler cannot choose another
command, claim, fence or effect-id list.

The engine creates the port with:

```python
effects = services.effect_port(command, claim, workspace)
execution = handler.execute(command, claim, workspace, effects=effects)
```

The same port is supplied on candidate validation. No handler reports `required_effect_ids`;
the service reads the complete effect ledger from the platform store.

## Methods

```python
prepare_effect(
    effect_key, effect_kind, target, expected_state, outbound_root
) -> EffectIntent

authorize_dispatch(effect_id, source) -> EffectFact

record_response(effect_id, source, response_root) -> EffectFact
record_timeout(effect_id, source, detail_root=None) -> EffectFact
record_observation(effect_id, source, state_class, observation_root) -> EffectFact
reconcile_effect(effect_id, source, state_class, evidence_root) -> EffectFact

effect_state(effect_id) -> EffectState
effect_settlement(effect_id) -> EffectSettlement
effect_settlement_for_key(effect_key) -> EffectSettlement | None
```

`prepare_effect` seals the complete outbound artifact and persists immutable target plus expected
state before any call. `authorize_dispatch` performs a short database transaction that verifies
the open claim/fence and changes only `prepared` to the fenced dispatch authorization fact. A
second dispatch is refused while the original effect is unknown; the handler must observe or
reconcile the same effect first.

`effect_settlement` returns the projected state and the decisive append-only fact. When the
state is `satisfied`, its `payload_artifact` is the exact sealed observation/native artifact
already bound to that effect. Discover/Capture may independently admit that artifact and
continue; they must not send the native request again. An `unknown` settlement carries no
successful payload and permits only observation/reconciliation of the same effect.

`effect_settlement_for_key` is the restart lookup: it is scoped to the bound command and returns
the same settlement, or `None` only when that command has no intent under the effect key. This is
the required query-before-send path when a process no longer has the prior in-memory effect id.

Handler control flow:

```text
existing = effect_settlement_for_key(effect_key)
None       -> prepare_effect, authorize_dispatch, perform one external call
prepared   -> authorize_dispatch, perform one external call
unknown    -> query/reconcile the same effect; external call count remains zero
satisfied  -> independently admit/use settlement.payload_artifact; external call count is zero
definitely_failed -> consume the stored failure fact; external call count is zero
conflict   -> refuse without overwrite or another ledger
```

The actual native, Git or HTTP call occurs after `authorize_dispatch` commits. Response, timeout
and remote observations are appended under the same effect identity. Current absence is not
terminal until reconciliation proves that an earlier request can no longer arrive.

## Workspace retention

The stage workspace is cleanup-eligible only after a completed StageResult and its independent
record artifact are sealed and reverified, all command effects are satisfied, the result proves
resource termination, and the unique decision has committed.

A completed `StageExecution` therefore requires `resources_settled=True`. A failed or cancelled
execution carries a `StageFailure` with original error identity, message/detail and evidence
references; its reason code is not a substitute for the error.

Unhandled errors, unknown native/resource state, definite failures, diagnostic or result seal
failures, and a lost decision-commit response retain the claim-owned workspace. Local
`failure.json` preserves an unhandled error without becoming business authority. A minimal
FailureEnvelope records original error identity/message/detail, sealed evidence refs and actual
retained locators; diagnostic failure is an additional unavailable reason and never replaces
the original domain failure.

## Identity fields

- `ExecutionClaim.owner_sha256`: one concrete process/entry identity; never the tool release.
- `ExecutionClaim.executor_sha256`: actual tool/distribution identity.
- `StageCommand.expected_executor_sha256`: required tool identity; claim acquisition refuses drift.
- `StageResult.actual_executor_sha256`: executor tool identity, never process owner.
- `ArtifactRef.producer_tool_sha256`: tool identity.

## Required tests

1. An unknown pre-existing effect refuses dispatch before the native callback runs; callback count
   is zero.
2. A prepared effect receives one fenced authorization and a duplicate owner entry cannot execute.
3. A stale fence cannot authorize or append a response.
4. An old `known` fact followed by a later dispatch projects `unknown`.
5. `absent` observation alone remains unknown after a send; only `reconciled absent` is terminal.
6. Git and PR effects are independently prepared and both must be satisfied for Publish.

## Current implementation points

- `pipeline/effects.py`: canonical `EffectIntent`, `EffectFact`, projection and
  `StageEffectPort`.
- `pipeline/services.py`: `BoundStageEffectPort`, claim/tool separation, real ArtifactStore
  sealing and FactStore calls.
- `pipeline/engine.py`: handler injection, pre-send capability, first-seal validation,
  complete-ledger check and minimal failure fallback.
- `platform/facts.py`: transactional authorization, append-only facts, exact command ledger
  validation and failure-envelope commit.
- `tests/v1/test_runtime_candidate_postgres.py`: throwaway PostgreSQL concurrency and
  composition proof.

## 技术派发的持久化边界

Portal 的 Airflow 派发使用独立的 `DispatchCommand`，与 run、Freeze command、固定的
`dag_id`/`dag_run_id`、幂等键和派发执行器身份绑定。`DispatchConf` 只保存 `run_id` 与
`freeze_command_id`，作为独立的 `airflow_dispatch_conf` artifact 封存；凭据留在受控
adapter 中，不进入 conf、command、数据库或本模块工作区。

`FactStore.admit_run` 在一个短事务内写入 `RunAdmission`、Freeze `StageCommand`、dispatch
command、conf artifact 和唯一的 `airflow_dispatch` `EffectIntent`。schema 同时保证每个 run
最多有一个 dispatch command。dispatch 复用 command、execution claim 和 effect 事实，
没有第七个业务阶段，也不创建 `StageDecision`。

`TechnicalDispatchService` 只有在 effect 为 `prepared` 时取得 dispatch command 自己的
claim。它不复用 Freeze claim。触发器响应、异常和后续观察都写入同一 effect journal。
已满足的 effect 不访问外部服务；丢响应或后续不可观察时只查询原
`dag_id`/`dag_run_id`，不会重新 trigger、换 run id 或读取 XCom 推断结果。

`tests/v1/test_runtime_candidate_postgres.py` 使用监听 `127.0.0.1` 的本地 Airflow stub，
覆盖 admission 原子绑定、伪造 Freeze command、错误 conf/原件绑定、独立 claim、丢响应观察
与 unknown replay 零重发。该 stub 不连接真实 Airflow。

## Admission 与后续命令绑定

`AdmissionSpec` 是 principal、purpose、request、target、originals、rules、configuration、
六个 stage executor 身份和命令身份的唯一纯事实。每个 builder 只从
`StageExecutors.for_stage(stage)` 取得自己的 executor；Windows native closure 由独立的
`native_tool_source_sha256` 表达，Linux Capture executor 与它不要求相同。完整
`native_tool` 记录的摘要另作为 `native_tool_record_sha256` 保留，不能拿它与源码身份
互换。

`build_freeze_command`、`build_run_admission` 及
`FactStore.admit_run` 共同验证 Freeze command 是该 spec 的确定性投影；错绑原件、规则、
配置或管理字段会在任何数据库写入前拒绝。

Freeze 完成后，`FactStore.create_command` 同时要求持久化 `RunAdmission`、已验证的
immediate `VerifiedPredecessor`、按阶段所需的 supporting predecessors，以及 Capture
需要的 `VerifiedDiscoverResult`、Publish 需要的 `VerifiedVerifyResult` 与
`VerifiedPublishIntent`。它在 run 锁内逐个核对所有 predecessor command、
decision、StageResult artifact 和业务输出 artifact，再由同一 spec 重建并验证目标命令。

当前纯 builder/validator 覆盖 Freeze、Discover、Capture、Generate、Verify 和 Publish。Capture
从已验证 Discover 结果读取 prepared/handoff/upload/native 身份；Generate 只接受 Capture；
Verify 同时核对 Generate 和 Capture lineage。Publish 只接受封存 Verify artifact 中的
certificate、已验证明细的 Generate/Capture pair、已封存 RepositorySnapshot 与确定性的
commit 元数据；builder 不读取当前 Git、scratch 或未封存配置。

该路径不会接受调用方自报的 principal、purpose、source、configuration 或 executor
字段；任一前驱或 support 事实与已注册事实不同，都在 command 写入前拒绝。

本候选只完成共享运行时与 PostgreSQL 组合验证。Portal/handler 的最终接线由主架构
统一组合；本轮未连接真实 Airflow、Portal、现场、Windows 或 CAD，也不构成任何部署或
Capture 阶段通过声明。

## 单阶段 diagnostic replay

诊断入口使用显式的 `DiagnosticAdmissionSpec`：

```text
DiagnosticAdmissionSpec:
  schema_version, run_id, request_id
  target_stage
  request
  input_artifacts
  expected_executor_sha256
  config_snapshot_sha256, contract_sha256, policy_sha256
  created_at
```

`build_diagnostic_command` 从有序 `ArtifactRef` closure 生成唯一的
`DiagnosticReplayScope`，并创建 `predecessor_decision_id=None` 的单个 target-stage
command。`FactStore.admit_diagnostic` 只写该 run 和这一条 command，不写任何上游
`StageDecision`。同一 target 的重复接纳返回相同事实。

`DiagnosticReplayScope` 只包含 `target_stage` 和完整输入 closure 摘要，并条件性绑定在
`StageCommand`、`StageResult` 和 `FailureEnvelope` 中。formal 记录不写该字段，因此现有
v1 序列化形状不变。`StageQualification` 同时绑定 admission request digest、command
digest、result/failure record digest 和 diagnostic scope。formal 消费者必须用同一
Admission、command 与封存结果调用 `require_formal_qualification` 重算，不能只信任
`formal_eligible` 布尔；删除 scope、替换 record 或换用另一 admission 都会失败。
`VerifiedPredecessor` 另拒绝所有 diagnostic command 或 result。

入口适配步骤：

1. 通过 `DiagnosticRuntimeConfig(database_dsn=..., artifact_root=..., database_options=...)`
   显式打开私有 `DiagnosticStores`，不存在生产环境变量回退。
2. 用 `ArtifactStore.import_object(source_store, ref)` 导入封存对象。该操作验证源
   manifest/member closure，逐字节复制原 object，保留 creator producer、tool、摘要和
   artifact id；不调用 `seal_tree`。
3. 构造 `DiagnosticAdmissionSpec`，调用 `build_diagnostic_command` 和
   `FactStore.admit_diagnostic`。
4. 用现有 `CanonicalStageServices` 与 `execute_stage` 注入单一 handler 和现有 effect
   port。不得根据 scope 开放新的远端目标；native/Publish 仍走原 claim、effect
   authorization 和资源唯一规则。

入口 actor 的最小调用形状：

```python
config = DiagnosticRuntimeConfig(
    database_dsn=explicit_private_dsn,
    artifact_root=explicit_private_artifact_root,
    database_options=explicit_private_database_options,
)
stores = open_diagnostic_stores(config)
imported = stores.artifacts.import_object(source_artifact_store, sealed_input_ref)
spec = DiagnosticAdmissionSpec(
    schema_version="description-pipeline.diagnostic-admission-spec/v1",
    run_id=diagnostic_run_id,
    request_id=diagnostic_request_id,
    target_stage=target_stage,
    request=complete_stage_request,
    input_artifacts=(imported,),
    expected_executor_sha256=actual_handler_sha256,
    config_snapshot_sha256=config_sha256,
    contract_sha256=contract_sha256,
    policy_sha256=policy_sha256,
    created_at=created_at,
)
command = build_diagnostic_command(spec)
stores.facts.admit_diagnostic(spec, command)
services = CanonicalStageServices(
    facts=stores.facts,
    artifacts=stores.artifacts,
    work_root=private_work_root,
    claim_policy=ClaimPolicy(
        owner_sha256=concrete_process_sha256,
        executor_sha256=actual_handler_sha256,
        lease=timedelta(minutes=5),
    ),
    clock=clock,
)
result = execute_stage(command_id=command.command_id, handler=single_handler, services=services)
```

formal predecessor 消费不得直接检查 `formal_eligible` 字段；必须取得同一
`RunAdmission`、command 和 sealed result/failure，再调用
`require_formal_qualification` 重算完整绑定。

真实 domain handler 当前仍有待 owner 修正的输入绑定。最小 predicate 只允许
diagnostic scope 改变“前任资格来源”，不得放松 request、artifact kind、identity 或
effect 字段：

- `pipeline/freeze.py`：`FreezeHandler.execute` 未接收 engine 注入的 `effects`；line
  345 把 process `claim.owner_sha256` 与 tool `expected_executor_sha256` 比较。正确条件
  始终是 `claim.command_id == command.command_id` 与
  `claim.executor_sha256 == command.expected_executor_sha256`。
- `pipeline/discover.py` line 200 强制 Freeze artifact 的
  `producer_run_id == command.run_id`。diagnostic 分支只可允许该 producer 是历史身份，
  仍须精确匹配 request 的 `freeze_artifact_id`、artifact kind、set/digest 和 scope
  input closure。
- `pipeline/discover.py` line 385 强制非空 predecessor；line 387 混用 owner/executor。
  diagnostic 分支只可在 `command.diagnostic_scope.target_stage == "discover"` 时允许
  `predecessor_decision_id is None`，其余 Freeze request/input 校验保持原样。
- `pipeline/stages/generate.py` line 126 强制非空 predecessor。同样的单一 predicate 可
  允许 diagnostic Generate，Capture artifact binding、request schema 和 output contract
  不得放宽。

主分支另报的 Publish `execute` 非空 predecessor 检查与 Verify 临时命令也要使用同一
predicate；这两个 handler 不存在于当前公共层检出，公共层没有自行复制或绕过。
diagnostic scope 不改变任何 effect authorization、资源排他或受控目标检查。

诊断公共层不绕过这些检查。当前证据使用同一 engine/effect port 下、只读完整导入 closure
的本地 fixture handler；真实领域 handler 须由 owner 按 diagnostic scope 和真实输入语义
修正后接入。raw legacy 迁移仍应是外部显式工具，不进入正常 runtime 分支。

## Effect 只读对账入口

`EffectReconciliationService` 是无 claim 的应用入口，只接受原 command id、原 effect key
和只读
`EffectReconciliationAdapter.observe(command, intent, outbound_root, workspace_root)`。
它加载原 effect intent；`EffectSettlement.outbound_artifact` 由 `FactStore` 从既有 intent
直接解析，服务验证并物化这一个固定封存 bundle，不保留第二份 outbound 缓存。

Adapter 返回 `EffectReconciliationEvidence`，其中必须有完整 `payload_root`，或已有且
验证通过的 `payload_artifact`，并可选独立 metadata。Publish 返回包含
`observation.json` 和 `publication-plan.json` 的真实目录，Capture 返回
`execution.json`、`native-tool.json` 和 `native-evidence.zip` 的真实目录。Service 不再
生成 `reconciliation.json` wrapper；它逐字节封存该 payload，另封存
`effect_reconciliation_metadata`，`EffectFact.payload_artifact_id` 直接指向领域证据。
handler 从 settlement 物化后可直接按原有布局复用，无需重跑算法。

领域 adapter 的最小形状：

```python
class PublishObserver:
    def observe(self, *, command, intent, outbound_root, workspace_root):
        response_root = workspace_root / "publish-evidence"
        response_root.mkdir()
        # GET the fixed original operation here; never POST/authorize/send.
        (response_root / "observation.json").write_bytes(observation_bytes)
        (response_root / "publication-plan.json").write_bytes(
            (outbound_root / "publication-plan.json").read_bytes()
        )
        return EffectReconciliationEvidence(
            fact_type="reconciled",
            state_class="known",
            payload_root=response_root,
            metadata={"http_status": 200, "fixed_operation_id": intent.effect_id},
        )
```

已有完整 payload 时改为返回
`EffectReconciliationEvidence(..., payload_artifact=verified_ref)`；`payload_root` 与
`payload_artifact` 必须且只能提供一个。Windows/Capture observer 使用同一形状，只把
`payload_root` 内容设为 `execution.json`、`native-tool.json` 和
`native-evidence.zip`。adapter 只收到 GET/材料化权限，不获得 effect authorize、send 或
事实写入端口。

```python
reconciler = EffectReconciliationService(
    facts=facts,
    artifacts=artifacts,
    work_root=private_reconcile_work_root,
    observer_tool_sha256=observer_tool_sha256,
    clock=clock,
)
result = reconciler.reconcile(
    command_id=command_id,
    effect_key=effect_key,
    adapter=read_only_domain_adapter,
)
# Only after a known/absent reconciliation settles the exact effect may the
# original execute_stage acquire its next claim.
```

`observation absent` 只记录当前不可见，仍保持 `unknown`；只有
`reconciled absent` 可终结为 `definitely_failed`。`reconciled unobservable` 仍是
`unknown`。lease 到期从不是终态，也不授权发送。append 事务会在写入前重新投影原 effect；
若已被另一只读路径解析则拒绝第二份终态，保持 append-only 单权威。

adapter、payload 校验、seal 或 fact append 任一失败时，service 保留完整
service-owned workspace 并写 `reconciliation-failure.json`；
`EffectReconciliationFailure.retained_workspace` 指向原始下载证据。只有 payload、
metadata 和 fact 全部持久化后才清理 workspace。迟到的旧 claim response/timeout 仍可追加
作为原始证据，但不会再覆盖已由 exact observation/reconciliation 建立的终态。
