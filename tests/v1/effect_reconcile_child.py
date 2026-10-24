"""Subprocess fixture for the effect reconciliation restart proof."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta
from pathlib import Path
from urllib import request as urlrequest

import psycopg2

from description_pipeline.artifacts import ArtifactStore
from description_pipeline.io import read_data
from description_pipeline.pipeline import (
    EffectReconciliationEvidence,
    EffectReconciliationService,
    EffectState,
    StageExecution,
    execute_stage,
)
from description_pipeline.pipeline.services import CanonicalStageServices, ClaimPolicy
from description_pipeline.platform.facts import FactStore


class _ResumeHandler:
    def __init__(self, artifacts: ArtifactStore) -> None:
        self.artifacts = artifacts

    def execute(self, command, claim, workspace, *, effects):
        settlement = effects.effect_settlement_for_key("native")
        if settlement is None or settlement.state is not EffectState.SATISFIED:
            raise AssertionError("The resumed handler requires a reconciled satisfied effect")
        if settlement.payload_artifact is None:
            raise AssertionError("The reconciled effect exposes no domain payload")
        payload = self.artifacts.materialize(
            settlement.payload_artifact,
            workspace.root / "reconciled-payload",
        )
        if not (payload / "observation.json").is_file() or not (payload / "publication-plan.json").is_file():
            raise AssertionError("The resumed handler did not receive the publication evidence closure")
        output = workspace.root / "output"
        output.mkdir()
        (output / "result.txt").write_text("resumed without resend\n", encoding="utf-8")
        return StageExecution(
            outcome="completed",
            candidate_root=str(output),
            checks_sha256="0" * 64,
            resources_settled=True,
        )

    def validate_candidate(self, command, claim, candidate, *, effects):
        return candidate


class _HttpObserver:
    def observe(self, *, command, intent, outbound_root, workspace_root):
        plan = read_data(outbound_root / "request.json")
        with urlrequest.urlopen(
            f"{plan['base_url']}/observe/{intent.effect_id}",
            timeout=3,
        ) as response:
            payload = json.loads(response.read())
        evidence = workspace_root / "publish-evidence"
        evidence.mkdir()
        (evidence / "observation.json").write_text(
            json.dumps(payload, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        (evidence / "publication-plan.json").write_bytes((outbound_root / "request.json").read_bytes())
        return EffectReconciliationEvidence(
            fact_type="reconciled",
            state_class="known",
            payload_root=evidence,
            metadata={"remote_state": payload["state"]},
        )


def _connect(database_uri: str, schema: str):
    return psycopg2.connect(
        database_uri,
        options=f"-c search_path={schema},public",
    )


def _services(
    *,
    facts: FactStore,
    artifacts: ArtifactStore,
    work_root: Path,
    owner_sha256: str,
    executor_sha256: str,
    now: datetime,
) -> CanonicalStageServices:
    return CanonicalStageServices(
        facts=facts,
        artifacts=artifacts,
        work_root=work_root,
        claim_policy=ClaimPolicy(
            owner_sha256=owner_sha256,
            executor_sha256=executor_sha256,
            lease=timedelta(minutes=5),
        ),
        clock=lambda: now,
    )


def _send(args) -> int:
    artifacts = ArtifactStore(args.artifact_root)
    facts = FactStore(lambda: _connect(args.database_uri, args.schema), artifacts)
    now = datetime.fromisoformat(args.now)
    services = _services(
        facts=facts,
        artifacts=artifacts,
        work_root=Path(args.work_root),
        owner_sha256=args.owner_sha256,
        executor_sha256=args.executor_sha256,
        now=now,
    )
    command = facts.load_command(args.command_id)
    claim = services.acquire_claim(command)
    workspace = services.materialize(command, claim, candidate=None)
    effects = services.effect_port(command, claim, workspace)
    outbound = workspace.root / "outbound"
    outbound.mkdir()
    plan = {"base_url": args.base_url, "command_id": command.command_id}
    (outbound / "request.json").write_text(json.dumps(plan, sort_keys=True) + "\n", encoding="utf-8")
    intent = effects.prepare_effect(
        effect_key="native",
        effect_kind="native_operation",
        target={"endpoint": args.base_url + "/send"},
        expected_state={"command_id": command.command_id, "state": "created"},
        outbound_root=outbound,
    )
    effects.authorize_dispatch(intent.effect_id, source="old-process")
    try:
        urlrequest.urlopen(
            urlrequest.Request(
                args.base_url + "/send",
                data=json.dumps({"command_id": command.command_id, "effect_id": intent.effect_id}).encode(),
                method="POST",
            ),
            timeout=3,
        ).read()
    except Exception:
        return 23
    raise AssertionError("The loopback server was expected to lose the response")


def _resume(args) -> int:
    artifacts = ArtifactStore(args.artifact_root)
    facts = FactStore(lambda: _connect(args.database_uri, args.schema), artifacts)
    now = datetime.fromisoformat(args.now)
    command = facts.load_command(args.command_id)
    reconciliation = EffectReconciliationService(
        facts=facts,
        artifacts=artifacts,
        work_root=Path(args.work_root) / "reconcile",
        observer_tool_sha256=args.observer_tool_sha256,
        clock=lambda: now,
    )
    result = reconciliation.reconcile(
        command_id=command.command_id,
        effect_key="native",
        adapter=_HttpObserver(),
    )
    if result.settlement.state is not EffectState.SATISFIED:
        raise AssertionError("The exact external observation did not satisfy the effect")
    services = _services(
        facts=facts,
        artifacts=artifacts,
        work_root=Path(args.work_root),
        owner_sha256=args.owner_sha256,
        executor_sha256=args.executor_sha256,
        now=now,
    )
    execute_stage(
        command_id=command.command_id,
        handler=_ResumeHandler(artifacts),
        services=services,
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("send", "resume"), required=True)
    parser.add_argument("--database-uri", required=True)
    parser.add_argument("--schema", required=True)
    parser.add_argument("--artifact-root", required=True)
    parser.add_argument("--work-root", required=True)
    parser.add_argument("--command-id", required=True)
    parser.add_argument("--owner-sha256", required=True)
    parser.add_argument("--executor-sha256", required=True)
    parser.add_argument("--now", required=True)
    parser.add_argument("--base-url")
    parser.add_argument("--observer-tool-sha256")
    args = parser.parse_args()
    if args.mode == "send":
        if args.base_url is None:
            parser.error("--base-url is required for send")
        return _send(args)
    if args.observer_tool_sha256 is None:
        parser.error("--observer-tool-sha256 is required for resume")
    return _resume(args)


if __name__ == "__main__":
    raise SystemExit(main())
