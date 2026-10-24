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

`AdmissionSpec` 是 principal、purpose、request、target、originals、rules、configuration
和命令身份的唯一纯事实。`build_freeze_command`、`build_run_admission` 及
`FactStore.admit_run` 共同验证 Freeze command 是该 spec 的确定性投影；错绑原件、规则、
配置或管理字段会在任何数据库写入前拒绝。

Freeze 完成后，`FactStore.create_command` 同时要求持久化 `RunAdmission` 和已验证的
`VerifiedPredecessor`。它在 run 锁内重新核对 admission、Freeze command、decision、
StageResult artifact 和业务输出 artifact，再由同一 spec 重建并验证 Discover command。
该路径不会接受调用方自报的 principal、purpose、source 或 configuration 字段；不支持
AdmissionSpec 投影的阶段在写入前明确拒绝。

本候选只完成共享运行时与 PostgreSQL 组合验证。Portal/handler 的最终接线由主架构
统一组合；本轮未连接真实 Airflow、Portal、现场、Windows 或 CAD，也不构成任何部署或
Capture 阶段通过声明。
