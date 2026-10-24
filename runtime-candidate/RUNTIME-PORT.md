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

## Deferred Portal dispatch decision

Portal admission and Airflow dispatch must not consume the Freeze stage execution claim. The
candidate direction is a separate technical dispatch command linked in the same admission
transaction to the immutable RunAdmission and Freeze command. It reuses command, claim and
effect facts but creates neither a seventh business stage nor a StageDecision.

Its complete non-secret target/payload and outbound artifact must be durable with admission.
A lost trigger response is reconciled through the original dispatch effect; neither a new DAG
run identity nor a retry is inferred from timeout, scratch state or XCom. This model remains a
follow-up integration item and is not implemented in the current P0/port batch.

Admission semantic binding also remains open. The current `FactStore.admit_run` verifies only
run/stage shape and does not yet prove that the Freeze command is the deterministic projection
of the admission principal, request id, purpose, target, originals, rules and configuration.
The Freeze/Discover typed `AdmissionSpec` and pure command builder/validator are pending shared
integration. No `admission semantic binding closed` claim is made by this candidate.
