"""Subprocess fixture for Publish UNKNOWN send and claim-free resume."""

from __future__ import annotations

import argparse
import json
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import psycopg2

from description_pipeline.artifacts import ArtifactStore
from description_pipeline.pipeline import (
    EffectReconciliationService,
    EffectState,
    execute_stage,
)
from description_pipeline.pipeline.services import CanonicalStageServices, ClaimPolicy
from description_pipeline.pipeline.stages import publish as publish_stage
from description_pipeline.pipeline.stages.publish import (
    PublishGitReconciliationAdapter,
    PublishHandler,
)
from description_pipeline.platform.facts import FactStore
from description_pipeline.repository.publish import (
    PublicationRepository,
    PullRequestObservation,
)


class _ProcessCrash(RuntimeError):
    pass


class _FilePullRequests:
    def __init__(self, path: Path) -> None:
        self.path = path

    def _rows(self) -> list[PullRequestObservation]:
        if not self.path.exists():
            return []
        return [PullRequestObservation(**row) for row in json.loads(self.path.read_text(encoding="utf-8"))]

    def _write(self, rows: list[PullRequestObservation]) -> None:
        self.path.write_text(
            json.dumps([row.to_record() for row in rows], sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def observe(self, *, repository, base_ref, head_ref, candidate_commit):
        return tuple(
            row
            for row in self._rows()
            if row.base_ref == base_ref
            and row.head_ref == head_ref
            and row.head_oid == candidate_commit
        )

    def create(self, *, repository, base_ref, head_ref, candidate_commit, title, body):
        rows = self._rows()
        row = PullRequestObservation(
            number=len(rows) + 1,
            url=f"https://github.com/{repository}/pull/{len(rows) + 1}",
            state="OPEN",
            base_ref=base_ref,
            head_ref=head_ref,
            head_oid=candidate_commit,
            title=title,
            body=body,
        )
        self._write([*rows, row])
        return row

    def update(self, *, repository, number, candidate_commit, title, body):
        rows = self._rows()
        index = next(index for index, row in enumerate(rows) if row.number == number)
        rows[index] = replace(
            rows[index],
            head_oid=candidate_commit,
            title=title,
            body=body,
        )
        self._write(rows)
        return rows[index]


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
        work_root=Path(args.work_root) / f"send-{args.mode}",
        owner_sha256=args.owner_sha256,
        executor_sha256=args.executor_sha256,
        now=now,
    )
    command = facts.load_command(args.command_id)
    handler = PublishHandler(artifacts, pull_requests=_FilePullRequests(Path(args.pr_state)))
    failure = None
    if args.mode == "send":
        failure = patch.object(
            publish_stage,
            "_observe_git_exact",
            side_effect=_ProcessCrash("process stopped after git push"),
        )
        expected = _ProcessCrash
    else:
        failure = patch.object(
            PublicationRepository,
            "push",
            side_effect=RuntimeError("process stopped before remote write"),
        )
        expected = publish_stage.PublishIncomplete
    try:
        with failure:
            execute_stage(command_id=command.command_id, handler=handler, services=services)
    except expected:
        return 23
    raise AssertionError("The send process was expected to stop with an unresolved effect")


def _resume(args) -> int:
    artifacts = ArtifactStore(args.artifact_root)
    facts = FactStore(lambda: _connect(args.database_uri, args.schema), artifacts)
    now = datetime.fromisoformat(args.now)
    reconciliation = EffectReconciliationService(
        facts=facts,
        artifacts=artifacts,
        work_root=Path(args.work_root) / "reconcile",
        observer_tool_sha256=args.observer_tool_sha256,
        clock=lambda: now,
    )
    reconciled = reconciliation.reconcile(
        command_id=args.command_id,
        effect_key=publish_stage.GIT_EFFECT_KEY,
        adapter=PublishGitReconciliationAdapter(),
    )
    if reconciled.settlement.state is not EffectState.SATISFIED:
        return 25
    services = _services(
        facts=facts,
        artifacts=artifacts,
        work_root=Path(args.work_root) / "resume",
        owner_sha256=args.owner_sha256,
        executor_sha256=args.executor_sha256,
        now=now,
    )
    command = facts.load_command(args.command_id)
    handler = PublishHandler(artifacts, pull_requests=_FilePullRequests(Path(args.pr_state)))
    execute_stage(command_id=command.command_id, handler=handler, services=services)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("send", "send-absent", "resume"), required=True)
    parser.add_argument("--database-uri", required=True)
    parser.add_argument("--schema", required=True)
    parser.add_argument("--artifact-root", required=True)
    parser.add_argument("--work-root", required=True)
    parser.add_argument("--command-id", required=True)
    parser.add_argument("--owner-sha256", required=True)
    parser.add_argument("--executor-sha256", required=True)
    parser.add_argument("--now", required=True)
    parser.add_argument("--pr-state", required=True)
    parser.add_argument("--observer-tool-sha256")
    args = parser.parse_args()
    if args.mode in {"send", "send-absent"}:
        return _send(args)
    if args.observer_tool_sha256 is None:
        parser.error("--observer-tool-sha256 is required for resume")
    return _resume(args)


if __name__ == "__main__":
    raise SystemExit(main())
