"""Publish handler tests over local Git and a controlled pull-request client."""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import shutil
import subprocess
import sys
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen

from description_pipeline.artifacts import ArtifactStore
from description_pipeline import mjcf_contract
from description_pipeline.io import PipelineError, digest, file_digest, inventory, read_data, write_json
from description_pipeline.interface_records import check_policy_digest
from description_pipeline.pipeline.contracts import (
    ExecutionClaim,
    ProtocolError,
    StageCandidate,
    StageCommand,
    StageDeferral,
    StageEffectWait,
    StageFailure,
    StageResult,
)
from description_pipeline.pipeline.effects import EffectIntent, EffectSettlement, EffectState
from description_pipeline.pipeline.engine import StageWorkspace
from description_pipeline.pipeline.stages import publish as publish_stage
from description_pipeline.pipeline.stages.mjcf import MJCFHandler
from description_pipeline.pipeline.verify import NoExternalEffects
from description_pipeline.pipeline.verify_contract import validate_certificate_diagnostics
from description_pipeline.pipeline.stages.publish import (
    PUBLISH_OUTPUT_CONTRACT,
    PUBLISH_REQUEST_SCHEMA,
    VERIFY_ARTIFACT_KIND,
    PublishHandler,
    PublishGitStatusProbe,
    PublishPullRequestStatusProbe,
    validate_publication_receipt,
)
from description_pipeline.repository.publish import (
    GitHubCliPullRequestClient,
    PublicationRepository,
    PullRequestObservation,
    seal_repository_snapshot,
)
from description_pipeline.runtime import tool_record
from description_pipeline.schemas import (
    STAGE_EFFECT_WAIT_SCHEMA,
    STAGE_RESULT_SCHEMA,
)
from description_pipeline.stages import CONTRACT_SHA256
from tests.v1 import test_pipeline_verify as pair_fixtures

POLICY_SHA = check_policy_digest()
CONTRACT_SHA = CONTRACT_SHA256
REVIEW_BRANCH = "work/description/robot/0123456789abcdef01234567"
OTHER_REVIEW_BRANCH = (
    "work/description/robot2/89abcdef0123456789abcdef"
)


def git(repo: Path | None, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    command = ["git", *args] if repo is None else ["git", "-C", str(repo), *args]
    return subprocess.run(command, check=check, capture_output=True, text=True, encoding="utf-8")


def tree_digest(files: dict[str, str]) -> str:
    return digest(files)


class _MemoryEffects:
    def __init__(self, artifacts: ArtifactStore, *, command_id: str, executor: str) -> None:
        self.artifacts = artifacts
        self.command_id = command_id
        self.executor = executor
        self.intents: dict[str, EffectIntent] = {}
        self.by_key: dict[str, str] = {}
        self.states: dict[str, EffectState] = {}
        self.payloads: dict[str, object] = {}
        self.outbound: dict[str, object] = {}
        self.authorizations = 0

    def prepare_effect(self, *, effect_key, effect_kind, target, expected_state, outbound_root):
        outbound = self.artifacts.seal_tree(
            outbound_root,
            kind="effect_payload",
            producer_run_id="publish-run",
            producer_stage="publish",
            producer_tool_sha256=self.executor,
        )
        expected = dict(expected_state)
        identity = {
            "command_id": self.command_id,
            "effect_key": effect_key,
            "effect_kind": effect_kind,
            "expected_state_digest": digest(expected),
            "outbound_artifact_id": outbound.artifact_id,
            "target": dict(target),
        }
        intent = EffectIntent(
            effect_id="effect-" + digest(identity),
            command_id=self.command_id,
            effect_key=effect_key,
            effect_kind=effect_kind,
            target=target,
            expected_state=expected,
            expected_state_digest=digest(expected),
            outbound_artifact_id=outbound.artifact_id,
            outbound_digest=outbound.set_sha256,
            created_at=datetime(2026, 10, 25, tzinfo=UTC),
        )
        self.intents[intent.effect_id] = intent
        self.outbound[intent.effect_id] = outbound
        self.by_key[effect_key] = intent.effect_id
        self.states[intent.effect_id] = EffectState.PREPARED
        return intent

    def authorize_dispatch(self, effect_id, *, source):
        self.states[effect_id] = EffectState.UNKNOWN
        self.authorizations += 1
        return SimpleNamespace(effect_id=effect_id, kind="dispatch_authorized", source=source)

    def record_response(self, effect_id, *, source, response_root):
        self.payloads[effect_id] = self.artifacts.seal_tree(
            response_root,
            kind="effect_response",
            producer_run_id="publish-run",
            producer_stage="publish",
            producer_tool_sha256=self.executor,
        )
        return SimpleNamespace(effect_id=effect_id, kind="response", source=source)

    def record_timeout(self, effect_id, *, source, detail_root=None):
        self.states[effect_id] = EffectState.UNKNOWN
        return SimpleNamespace(effect_id=effect_id, kind="timeout", source=source)

    def record_observation(self, effect_id, *, source, state_class, observation_root):
        self.payloads[effect_id] = self.artifacts.seal_tree(
            observation_root,
            kind="effect_observation",
            producer_run_id=None,
            producer_stage=None,
            producer_tool_sha256=self.executor,
        )
        self.states[effect_id] = EffectState.SATISFIED if state_class == "known" else EffectState.UNKNOWN
        return SimpleNamespace(effect_id=effect_id, kind="observation", source=source)

    def reconcile_effect(self, effect_id, *, source, state_class, evidence_root):
        return self.record_observation(
            effect_id,
            source=source,
            state_class=state_class,
            observation_root=evidence_root,
        )

    def effect_state(self, effect_id):
        return self.states[effect_id]

    def effect_settlement(self, effect_id):
        return EffectSettlement(
            effect_id=effect_id,
            state=self.states[effect_id],
            decisive_fact=None,
            payload_artifact=self.payloads.get(effect_id),
            outbound_artifact=self.outbound[effect_id],
        )

    def effect_settlement_for_key(self, effect_key):
        effect_id = self.by_key.get(effect_key)
        return None if effect_id is None else self.effect_settlement(effect_id)

    def force_unknown(self, effect_key):
        effect_id = self.by_key[effect_key]
        self.states[effect_id] = EffectState.UNKNOWN


class _PullRequests:
    def __init__(self) -> None:
        self.rows: list[PullRequestObservation] = []
        self.observe_calls = 0
        self.create_calls = 0
        self.update_calls = 0
        self.lose_next_response = False
        self.force_multiple = False

    def observe(self, *, repository, base_ref, head_ref, candidate_commit):
        self.observe_calls += 1
        if self.force_multiple:
            return tuple(
                PullRequestObservation(
                    number=number,
                    url=f"https://example.test/{repository}/pull/{number}",
                    state="OPEN",
                    base_ref=base_ref,
                    head_ref=head_ref,
                    head_oid=candidate_commit,
                    title="unexpected",
                    body="unexpected",
                )
                for number in (1, 2)
            )
        rows = [
            row
            for row in self.rows
            if row.base_ref == base_ref and row.head_ref == head_ref and row.head_oid == candidate_commit
        ]
        return tuple(rows)

    def create(self, *, repository, base_ref, head_ref, candidate_commit, title, body):
        self.create_calls += 1
        number = len(self.rows) + 1
        row = PullRequestObservation(
            number=number,
            url=f"https://example.test/{repository}/pull/{number}",
            state="OPEN",
            base_ref=base_ref,
            head_ref=head_ref,
            head_oid=candidate_commit,
            title=title,
            body=body,
        )
        self.rows.append(row)
        if self.lose_next_response:
            self.lose_next_response = False
            raise RuntimeError("response lost")
        return row

    def update(self, *, repository, number, candidate_commit, title, body):
        self.update_calls += 1
        index = next(index for index, row in enumerate(self.rows) if row.number == number)
        row = replace(
            self.rows[index],
            state="OPEN",
            base_ref=self.rows[index].base_ref,
            head_ref=self.rows[index].head_ref,
            head_oid=candidate_commit,
            title=title,
            body=body,
        )
        self.rows[index] = row
        return row


class _PullRequestHttpService:
    def __init__(self) -> None:
        self.rows: list[dict] = []
        self.lock = threading.Lock()
        service = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                return

            def _json(self, status: int, payload) -> None:
                body = json.dumps(payload, sort_keys=True).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                parsed = urlparse(self.path)
                query = parse_qs(parsed.query)
                if parsed.path != "/pulls":
                    self._json(404, {"error": "not_found"})
                    return
                with service.lock:
                    rows = [
                        row
                        for row in service.rows
                        if row["base_ref"] == query.get("base", [""])[0]
                        and row["head_ref"] == query.get("head", [""])[0]
                        and row["head_oid"] == query.get("commit", [""])[0]
                    ]
                self._json(200, rows)

            def do_POST(self):
                if self.path != "/pulls":
                    self._json(404, {"error": "not_found"})
                    return
                length = int(self.headers.get("Content-Length", "0"))
                request = json.loads(self.rfile.read(length))
                with service.lock:
                    number = len(service.rows) + 1
                    row = {
                        **request,
                        "number": number,
                        "url": f"http://127.0.0.1/pull/{number}",
                        "state": "OPEN",
                    }
                    service.rows.append(row)
                self._json(201, row)

            def do_PATCH(self):
                if not self.path.startswith("/pulls/"):
                    self._json(404, {"error": "not_found"})
                    return
                number = int(self.path.rsplit("/", 1)[-1])
                length = int(self.headers.get("Content-Length", "0"))
                request = json.loads(self.rfile.read(length))
                with service.lock:
                    row = next(item for item in service.rows if item["number"] == number)
                    row.update(request)
                self._json(200, row)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"

    def close(self) -> None:
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()


class _HttpPullRequests:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url

    def _call(self, method: str, path: str, payload: dict | None = None):
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = Request(
            self.base_url + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        with urlopen(request, timeout=5) as response:
            return json.loads(response.read())

    def observe(self, *, repository, base_ref, head_ref, candidate_commit):
        rows = self._call(
            "GET",
            "/pulls?" + urlencode({"base": base_ref, "head": head_ref, "commit": candidate_commit}),
        )
        return tuple(
            PullRequestObservation(
                number=row["number"],
                url=row["url"],
                state=row["state"],
                base_ref=row["base_ref"],
                head_ref=row["head_ref"],
                head_oid=row["head_oid"],
                title=row["title"],
                body=row["body"],
            )
            for row in rows
        )

    def create(self, *, repository, base_ref, head_ref, candidate_commit, title, body):
        row = self._call(
            "POST",
            "/pulls",
            {
                "repository": repository,
                "base_ref": base_ref,
                "head_ref": head_ref,
                "head_oid": candidate_commit,
                "title": title,
                "body": body,
            },
        )
        return PullRequestObservation(
            number=row["number"],
            url=row["url"],
            state=row["state"],
            base_ref=row["base_ref"],
            head_ref=row["head_ref"],
            head_oid=row["head_oid"],
            title=row["title"],
            body=row["body"],
        )

    def update(self, *, repository, number, candidate_commit, title, body):
        row = self._call(
            "PATCH",
            f"/pulls/{number}",
            {"head_oid": candidate_commit, "title": title, "body": body},
        )
        return PullRequestObservation(
            number=row["number"],
            url=row["url"],
            state=row["state"],
            base_ref=row["base_ref"],
            head_ref=row["head_ref"],
            head_oid=row["head_oid"],
            title=row["title"],
            body=row["body"],
        )


class PublishFixture:
    def __init__(self, *, diagnostics: list[dict] | None = None) -> None:
        self.pair = pair_fixtures.PipelineVerifyHandlerTests(methodName="runTest")
        self.pair.setUp()
        self.root = self.pair.root
        self.store = self.pair.store
        self.tool = tool_record(role="portable")
        self.executor = self.tool["source_sha256"]
        self._make_repository()
        capture_root, generate_root = self.pair.capture_root, self.pair.generate_root
        self.capture_ref, self.generate_ref = self.pair.capture_ref, self.pair.generate_ref
        verify_root, self.certificate = self._make_verify(capture_root, generate_root, diagnostics=diagnostics)
        self.verify_ref = self.store.seal_tree(
            verify_root,
            kind=VERIFY_ARTIFACT_KIND,
            producer_run_id="verify-run",
            producer_stage="verify",
            producer_tool_sha256=self.certificate["actual_verifier_sha256"],
        )
        self._make_mjcf(verify_root)
        if self.deferred_diagnostics is not None:
            self._write_certificate_diagnostics(verify_root, self.deferred_diagnostics)
            self.verify_ref = self.store.seal_tree(
                verify_root, kind=VERIFY_ARTIFACT_KIND, producer_run_id="verify-run", producer_stage="verify",
                producer_tool_sha256=self.certificate["actual_verifier_sha256"],
            )
        snapshot_root = self.root / "repository-snapshot"
        self.snapshot = seal_repository_snapshot(
            self.repo,
            snapshot_root,
            repository_slug="example/robot",
            origin_url=str(self.remote),
            base_ref="main",
            base_oid=self.base_oid,
            branch_ref=REVIEW_BRANCH,
            head_oid=None,
        )
        self.snapshot_ref = self.store.seal_tree(
            snapshot_root,
            kind=publish_stage.REPOSITORY_SNAPSHOT_KIND,
            producer_run_id=None,
            producer_stage=None,
            producer_tool_sha256=digest({"tool": "snapshot-builder"}),
        )
        self.command = StageCommand.create(
            command_id="publish-command",
            run_id="publish-run",
            stage="publish",
            idempotency_key="publish-run/publish",
            request={
                "schema_version": PUBLISH_REQUEST_SCHEMA,
                "output_contract": PUBLISH_OUTPUT_CONTRACT,
                "generate_artifact_id": self.generate_ref.artifact_id,
                "capture_artifact_id": self.capture_ref.artifact_id,
                "verify_artifact_id": self.verify_ref.artifact_id,
                "mjcf_artifact_id": self.mjcf_ref.artifact_id,
                "mjcf_command_id": self.mjcf_command.command_id,
                "mjcf_request_digest": self.mjcf_command.request_digest,
                "mjcf_certificate_sha256": self.mjcf_checks_sha256,
                "repository_snapshot_artifact_id": self.snapshot_ref.artifact_id,
                "verified_subject_sha256": self.generate_ref.set_sha256,
                "repository_slug": "example/robot",
                "origin_url": str(self.remote),
                "base_ref": "main",
                "expected_base_oid": self.base_oid,
                "branch_ref": REVIEW_BRANCH,
                "expected_head_oid": None,
                "commit": {
                    "author_name": "Publish Bot",
                    "author_email": "publish@example.test",
                    "authored_at": "2026-10-25T00:00:00+00:00",
                    "committer_name": "Publish Bot",
                    "committer_email": "publish@example.test",
                    "committed_at": "2026-10-25T00:00:00+00:00",
                    "subject": "feat(robot): publish URDF",
                    "body": "verified offline publication fixture",
                },
            },
            input_artifacts=(self.capture_ref, self.generate_ref, self.verify_ref, self.snapshot_ref, self.mjcf_ref),
            config_snapshot_sha256=self.certificate["config_snapshot_sha256"],
            contract_sha256=CONTRACT_SHA,
            policy_sha256=POLICY_SHA,
            expected_executor_sha256=self.executor,
            predecessor_decision_id="mjcf-decision",
        )
        self.claim = ExecutionClaim(
            claim_id="publish-claim",
            command_id=self.command.command_id,
            fence=1,
            owner_sha256=digest({"process": "publish"}),
            executor_sha256=self.executor,
            lane_key="example/robot:robot:engineering",
            expires_at=datetime(2026, 10, 25, tzinfo=UTC) + timedelta(hours=1),
        )
        self.workspace = StageWorkspace(
            root=self.root / "workspace",
            inputs={
                self.capture_ref.artifact_id: self.store.materialize(
                    self.capture_ref, self.root / "workspace/inputs/capture"
                ),
                self.generate_ref.artifact_id: self.store.materialize(
                    self.generate_ref, self.root / "workspace/inputs/generate"
                ),
                self.verify_ref.artifact_id: self.store.materialize(
                    self.verify_ref, self.root / "workspace/inputs/verify"
                ),
                self.mjcf_ref.artifact_id: self.store.materialize(self.mjcf_ref, self.root / "workspace/inputs/mjcf"),
                self.snapshot_ref.artifact_id: self.store.materialize(
                    self.snapshot_ref, self.root / "workspace/inputs/repository-snapshot"
                ),
            },
        )
        self.effects = _MemoryEffects(self.store, command_id=self.command.command_id, executor=self.executor)
        self.pr = _PullRequests()
        self.handler = PublishHandler(self.store, pull_requests=self.pr)

    def _make_repository(self) -> None:
        self.remote = self.root / "example" / "robot.git"
        self.remote.parent.mkdir()
        git(None, "init", "--bare", "-b", "main", str(self.remote))
        seed = self.root / "seed"
        seed.mkdir()
        git(None, "init", "-b", "main", str(seed))
        git(seed, "config", "user.name", "Seed")
        git(seed, "config", "user.email", "seed@example.test")
        (seed / "README.md").write_text("base\n", encoding="utf-8")
        git(seed, "add", "README.md")
        git(seed, "commit", "-m", "base")
        git(seed, "remote", "add", "origin", str(self.remote))
        git(seed, "push", "-u", "origin", "main")
        self.base_oid = git(seed, "rev-parse", "HEAD").stdout.strip()
        self.repo = self.root / "repo"
        git(None, "clone", str(self.remote), str(self.repo))
        git(self.repo, "config", "user.name", "Seed")
        git(self.repo, "config", "user.email", "seed@example.test")

    def _make_verify(
        self, capture_root: Path, generate_root: Path, *, diagnostics: list[dict] | None = None
    ) -> tuple[Path, dict]:
        self.deferred_diagnostics = None
        _command, execution, certificate, *_ = self.pair._run_verify()
        assert execution.outcome == "completed", execution
        root = Path(execution.candidate_root)
        self.certificate = certificate
        if diagnostics is not None:
            try:
                validate_certificate_diagnostics({
                    **certificate, "diagnostics": [*certificate["diagnostics"], *diagnostics],
                })
            except PipelineError:
                self.deferred_diagnostics = diagnostics
            else:
                self._write_certificate_diagnostics(root, diagnostics)
        return root, self.certificate

    def _write_certificate_diagnostics(self, root, diagnostics):
        self.certificate["diagnostics"].extend(diagnostics)
        self.certificate.pop("verification_key")
        self.certificate["verification_key"] = digest(self.certificate)
        write_json(root / "verification-certificate.json", self.certificate)
        receipt = read_data(root / "verification-execution.json")
        receipt["certificate_sha256"] = digest(self.certificate)
        write_json(root / "verification-execution.json", receipt)

    def _make_mjcf(self, verify_root):
        command = StageCommand.create(
            command_id="mjcf-command", run_id="publish-run", stage="mjcf", idempotency_key="publish-run/mjcf",
            request={
                "schema_version": mjcf_contract.STAGE_REQUEST_SCHEMA, "output_contract": mjcf_contract.OUTPUT_CONTRACT,
                "generate_artifact_id": self.generate_ref.artifact_id,
                "capture_artifact_id": self.capture_ref.artifact_id, "verify_artifact_id": self.verify_ref.artifact_id,
                "verify_certificate_sha256": file_digest(verify_root / "verification-certificate.json"),
                "simulation": "{}\n",
            }, input_artifacts=(self.generate_ref, self.capture_ref, self.verify_ref),
            config_snapshot_sha256=self.certificate["config_snapshot_sha256"],
            contract_sha256=CONTRACT_SHA, policy_sha256=POLICY_SHA,
            expected_executor_sha256=self.executor, predecessor_decision_id="verify-decision",
        )
        claim = self.pair._claim(command, owner=digest({"worker": "mjcf"}), executor=self.executor)
        execution = MJCFHandler(self.store).execute(command, claim, StageWorkspace(
            root=self.root / "mjcf-attempt", inputs={self.generate_ref.artifact_id:self.pair.generate_root,
            self.capture_ref.artifact_id:self.pair.capture_root, self.verify_ref.artifact_id:verify_root},
        ), effects=NoExternalEffects())
        assert execution.outcome == "completed", execution
        self.mjcf_command, self.mjcf_checks_sha256 = command, execution.checks_sha256
        self.mjcf_ref = self.store.seal_tree(
            Path(execution.candidate_root), kind=mjcf_contract.ARTIFACT_KIND, producer_run_id=command.run_id,
            producer_stage="mjcf", producer_tool_sha256=self.executor,
        )

    def close(self) -> None:
        self.pair.doCleanups()


class PublishHandlerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = PublishFixture()
        self.addCleanup(self.fixture.close)

    def _run(
        self,
        *,
        command=None,
        workspace=None,
        claim=None,
        effects=None,
        handler=None,
        resume=None,
    ):
        selected = handler or self.fixture.handler
        arguments = (
            command or self.fixture.command,
            claim or self.fixture.claim,
            workspace or self.fixture.workspace,
        )
        selected_effects = effects or self.fixture.effects
        if resume is None:
            return selected.execute(
                *arguments,
                effects=selected_effects,
            )
        return selected.resume_stage(
            *arguments,
            effects=selected_effects,
            resume=resume,
        )

    def _effect_wait(self, effect_key: str) -> StageEffectWait:
        effect_id = self.fixture.effects.by_key[effect_key]
        intent = self.fixture.effects.intents[effect_id]
        settlement = self.fixture.effects.effect_settlement_for_key(effect_key)
        self.assertIsNotNone(settlement)
        self.assertIsNotNone(settlement.outbound_artifact)
        return StageEffectWait(
            schema_version=STAGE_EFFECT_WAIT_SCHEMA,
            run_id=self.fixture.command.run_id,
            stage=self.fixture.command.stage,
            command_id=self.fixture.command.command_id,
            request_digest=self.fixture.command.request_digest,
            effect_id=effect_id,
            effect_key=effect_key,
            effect_kind=intent.effect_kind,
            intent_identity_digest=intent.identity_digest,
            outbound_artifact_id=intent.outbound_artifact_id,
            outbound_digest=intent.outbound_digest,
            expected_state_digest=intent.expected_state_digest,
        )

    def _probe_source(self, effect_key: str, name: str):
        wait = self._effect_wait(effect_key)
        settlement = self.fixture.effects.effect_settlement_for_key(effect_key)
        self.assertIsNotNone(settlement)
        self.assertIsNotNone(settlement.outbound_artifact)
        root = self.fixture.store.materialize(
            settlement.outbound_artifact,
            self.fixture.root / "probe-sources" / name,
        )
        return wait, settlement.outbound_artifact, Path(root)

    def _pr_recovery_inputs(self):
        first = self._run()
        effect_id = self.fixture.effects.by_key[publish_stage.PR_EFFECT_KEY]
        intent = self.fixture.effects.intents[effect_id]
        settlement = self.fixture.effects.effect_settlement_for_key(
            publish_stage.PR_EFFECT_KEY
        )
        self.assertIsNotNone(settlement)
        self.assertIsNotNone(settlement.outbound_artifact)
        outbound = self.fixture.store.materialize(
            settlement.outbound_artifact,
            self.fixture.root / "pr-recovery-outbound",
        )
        self.fixture.effects.force_unknown(publish_stage.PR_EFFECT_KEY)
        return first, effect_id, intent, Path(outbound)

    def _command_with_target(self, *, base_ref: str, branch_ref: str) -> StageCommand:
        request = self.fixture.command.to_record()["request"]
        request.update({"base_ref": base_ref, "branch_ref": branch_ref})
        command = self.fixture.command
        return StageCommand.create(
            command_id=command.command_id,
            run_id=command.run_id,
            stage=command.stage,
            idempotency_key=command.idempotency_key,
            request=request,
            input_artifacts=command.input_artifacts,
            config_snapshot_sha256=command.config_snapshot_sha256,
            contract_sha256=command.contract_sha256,
            policy_sha256=command.policy_sha256,
            expected_executor_sha256=command.expected_executor_sha256,
            predecessor_decision_id=command.predecessor_decision_id,
        )

    def test_publish_prepares_commits_and_satisfies_two_effects(self) -> None:
        self.assertNotIn("repository_path", self.fixture.command.request)
        self.assertNotIn("expected_publication_subject_sha256", self.fixture.command.request)
        execution = self._run()
        self.assertEqual(execution.outcome, "completed")
        self.assertTrue(execution.resources_settled)
        self.assertEqual(self.fixture.effects.authorizations, 2)
        self.assertTrue(all(state is EffectState.SATISFIED for state in self.fixture.effects.states.values()))
        branch_ref = self.fixture.command.request["branch_ref"]
        branch = git(self.fixture.repo, "ls-remote", "origin", f"refs/heads/{branch_ref}")
        commit = branch.stdout.split()[0]
        receipt = read_data(Path(execution.candidate_root) / "publication-receipt.json")
        self.assertEqual(receipt["candidate_commit"], commit)
        self.assertEqual(len(self.fixture.pr.rows), 1)
        self.assertEqual(receipt["pr_effect"]["observation"]["number"], self.fixture.pr.rows[0].number)
        git_effect_id = self.fixture.effects.by_key[publish_stage.GIT_EFFECT_KEY]
        outbound_id = self.fixture.effects.intents[git_effect_id].outbound_artifact_id
        bundle = self.fixture.store.objects / outbound_id / "candidate.bundle"
        plan_path = self.fixture.store.objects / outbound_id / "publication-plan.json"
        repo = self.fixture.workspace.root / "repository"
        self.assertEqual(git(repo, "bundle", "verify", str(bundle)).returncode, 0)
        self.assertEqual(file_digest(plan_path), receipt["publication_plan_sha256"])
        self.assertEqual(
            git(repo, "rev-parse", f"{commit}^{{tree}}").stdout.strip(),
            receipt["candidate_tree"],
        )
        metadata = git(
            repo,
            "show",
            "-s",
            "--format=%an%n%ae%n%aI%n%cn%n%ce%n%cI",
            commit,
        ).stdout.splitlines()
        self.assertEqual(metadata[:2], ["Publish Bot", "publish@example.test"])
        self.assertEqual(metadata[3:5], ["Publish Bot", "publish@example.test"])
        expected_time = datetime(2026, 10, 25, tzinfo=UTC)
        self.assertEqual(datetime.fromisoformat(metadata[2].replace("Z", "+00:00")), expected_time)
        self.assertEqual(datetime.fromisoformat(metadata[5].replace("Z", "+00:00")), expected_time)

    def test_publish_uses_controlled_loopback_http_pull_request_service(self) -> None:
        service = _PullRequestHttpService()
        self.addCleanup(service.close)
        handler = PublishHandler(
            self.fixture.store,
            pull_requests=_HttpPullRequests(service.base_url),
        )
        execution = self._run(handler=handler)
        self.assertEqual(execution.outcome, "completed")
        self.assertEqual(len(service.rows), 1)
        receipt = read_data(Path(execution.candidate_root) / "publication-receipt.json")
        self.assertEqual(service.rows[0]["head_oid"], receipt["candidate_commit"])

    def test_publish_rejects_non_pr_and_protected_targets_before_external_effects(self) -> None:
        remote_before = git(
            None,
            "--git-dir",
            str(self.fixture.remote),
            "for-each-ref",
            "--format=%(refname) %(objectname)",
        ).stdout
        cases = (
            ("main", "main", "same-as-base"),
            ("release", "main", "protected-main"),
            ("main", "refs/heads/main", "protected-main"),
            ("main", "feature/robot", "not-review-branch"),
            (
                REVIEW_BRANCH,
                REVIEW_BRANCH,
                "same-as-base",
            ),
        )
        with patch.object(PublicationRepository, "push", side_effect=AssertionError("must not push")):
            for base_ref, branch_ref, reason in cases:
                with self.subTest(base_ref=base_ref, branch_ref=branch_ref):
                    with self.assertRaises(PipelineError) as raised:
                        self._run(
                            command=self._command_with_target(
                                base_ref=base_ref,
                                branch_ref=branch_ref,
                            )
                        )
                    self.assertEqual(raised.exception.code, "publish.target_forbidden")
                    self.assertEqual(raised.exception.detail["reason"], reason)
        remote_after = git(
            None,
            "--git-dir",
            str(self.fixture.remote),
            "for-each-ref",
            "--format=%(refname) %(objectname)",
        ).stdout
        self.assertEqual(remote_after, remote_before)
        self.assertEqual(self.fixture.effects.intents, {})
        self.assertEqual(self.fixture.effects.authorizations, 0)
        self.assertEqual(self.fixture.pr.observe_calls, 0)
        self.assertEqual(self.fixture.pr.create_calls, 0)
        self.assertEqual(self.fixture.pr.update_calls, 0)

    def test_repository_prepare_and_push_revalidate_target_before_git(self) -> None:
        repository = PublicationRepository(self.fixture.repo, expected_origin=str(self.fixture.remote))
        candidate = SimpleNamespace(worktree=self.fixture.repo)
        with patch(
            "description_pipeline.repository.publish._run",
            side_effect=AssertionError("must not run Git"),
        ):
            with self.assertRaises(PipelineError) as prepared:
                repository.candidate(
                    publication_root=self.fixture.root / "publication",
                    base_ref="main",
                    expected_base_oid=self.fixture.base_oid,
                    branch_ref="main",
                    expected_head_oid=None,
                    commit_spec=None,
                    staging_root=self.fixture.root / "candidate",
                )
            with self.assertRaises(PipelineError) as pushed:
                repository.push(
                    candidate,
                    base_ref="main",
                    branch_ref="main",
                    expected_head_oid=None,
                )
        self.assertEqual(prepared.exception.code, "publish.target_forbidden")
        self.assertEqual(pushed.exception.code, "publish.target_forbidden")

    def test_repository_validation_rejects_split_fetch_and_push_targets(self) -> None:
        second_remote = self.fixture.root / "example" / "other.git"
        second_remote.parent.mkdir(exist_ok=True)
        git(None, "init", "--bare", "-b", "main", str(second_remote))
        git(
            self.fixture.repo,
            "remote",
            "set-url",
            "--push",
            "origin",
            str(second_remote),
        )
        repository = PublicationRepository(
            self.fixture.repo,
            expected_origin=str(self.fixture.remote),
        )
        with self.assertRaises(PipelineError) as raised:
            repository.validate()
        self.assertEqual(
            raised.exception.code,
            "publish.remote_target_mismatch",
        )

    def test_push_revalidates_the_remote_target_before_git(self) -> None:
        repository = PublicationRepository(
            self.fixture.repo,
            expected_origin=str(self.fixture.remote),
        )
        worktree = self.fixture.root / "linked-push-worktree"
        git(
            self.fixture.repo,
            "config",
            "extensions.worktreeConfig",
            "true",
        )
        git(
            self.fixture.repo,
            "worktree",
            "add",
            "--detach",
            str(worktree),
            "HEAD",
        )
        second_remote = self.fixture.root / "example" / "changed.git"
        second_remote.parent.mkdir(exist_ok=True)
        git(None, "init", "--bare", "-b", "main", str(second_remote))
        git(
            worktree,
            "config",
            "--worktree",
            "remote.origin.pushurl",
            str(second_remote),
        )
        repository.validate()
        with self.assertRaises(PipelineError) as raised:
            repository.push(
                SimpleNamespace(worktree=worktree),
                base_ref="main",
                branch_ref=REVIEW_BRANCH,
                expected_head_oid=None,
            )
        self.assertEqual(raised.exception.code, "publish.remote_target_mismatch")
        self.assertEqual(
            git(
                None,
                "--git-dir",
                str(second_remote),
                "for-each-ref",
                "--format=%(refname)",
            ).stdout,
            "",
        )

    def test_repository_validation_rejects_multiple_push_targets(self) -> None:
        for name in ("other-one", "other-two"):
            remote = self.fixture.root / "example" / f"{name}.git"
            remote.parent.mkdir(exist_ok=True)
            git(None, "init", "--bare", "-b", "main", str(remote))
            git(
                self.fixture.repo,
                "remote",
                "set-url",
                "--add",
                "--push",
                "origin",
                str(remote),
            )
        repository = PublicationRepository(
            self.fixture.repo,
            expected_origin=str(self.fixture.remote),
        )
        with self.assertRaises(PipelineError) as raised:
            repository.validate()
        self.assertEqual(
            raised.exception.code,
            "publish.remote_target_mismatch",
        )

    def test_repository_validation_rejects_insteadof_rewrites(self) -> None:
        replacement = self.fixture.root / "rewritten.git"
        git(None, "init", "--bare", "-b", "main", str(replacement))
        git(
            self.fixture.repo,
            "config",
            f"url.{replacement}.insteadOf",
            str(self.fixture.remote),
        )
        repository = PublicationRepository(
            self.fixture.repo,
            expected_origin=str(self.fixture.remote),
        )
        with self.assertRaises(PipelineError) as raised:
            repository.validate()
        self.assertEqual(raised.exception.code, "publish.remote_rewrite")

    def test_symlinked_repository_root_is_refused_before_resolve(self) -> None:
        linked = self.fixture.root / "linked-repository"
        try:
            linked.symlink_to(self.fixture.repo, target_is_directory=True)
        except OSError as error:
            self.skipTest(f"The host cannot create a symbolic link: {error}")
        with self.assertRaises(PipelineError) as raised:
            PublicationRepository(
                linked,
                expected_origin=str(self.fixture.remote),
            )
        self.assertEqual(raised.exception.code, "publish.repository_missing")

    def test_github_cli_client_always_targets_github_com(self) -> None:
        client = GitHubCliPullRequestClient(self.fixture.repo)
        completed = SimpleNamespace(stdout=b"[]", returncode=0)
        with (
            patch.dict(os.environ, {"GH_HOST": "enterprise.invalid"}),
            patch(
                "description_pipeline.repository.publish.subprocess.run",
                return_value=completed,
            ) as run,
        ):
            self.assertEqual(
                client.observe(
                    repository="mimicverse/robot",
                    base_ref="main",
                    head_ref="description/run-1",
                    candidate_commit="a" * 40,
                ),
                (),
            )
        self.assertEqual(
            run.call_args.args[0][:5],
            ["gh", "pr", "list", "--repo", "mimicverse/robot"],
        )
        self.assertEqual(run.call_args.kwargs["env"]["GH_HOST"], "github.com")

    def test_default_pull_request_client_is_scoped_to_each_claim_workspace(self) -> None:
        bin_root = self.fixture.root / "fake-bin"
        bin_root.mkdir()
        gh = bin_root / "gh"
        gh.write_text(
            """#!/usr/bin/python3
import json
import os
import subprocess
import sys
from pathlib import Path

with Path(os.environ["GH_TEST_LOG"]).open("a", encoding="utf-8") as stream:
    stream.write(str(Path.cwd()) + "\\n")

args = sys.argv[1:]
if args[:2] == ["pr", "list"]:
    state = Path(os.environ["GH_TEST_STATE"])
    if state.exists():
        print(json.dumps([json.loads(state.read_text(encoding="utf-8"))]))
    else:
        print("[]")
elif args[:2] == ["pr", "create"]:
    base = args[args.index("--base") + 1]
    head = args[args.index("--head") + 1]
    title = args[args.index("--title") + 1]
    body = args[args.index("--body") + 1]
    listing = subprocess.check_output(
        ["git", "ls-remote", "origin", f"refs/heads/{head}"],
        text=True,
    ).strip()
    commit = listing.split()[0]
    row = {
        "baseRefName": base,
        "body": body,
        "headRefName": head,
        "headRefOid": commit,
        "number": 1,
        "state": "OPEN",
        "title": title,
        "url": "https://github.com/example/robot/pull/1",
    }
    Path(os.environ["GH_TEST_STATE"]).write_text(json.dumps(row), encoding="utf-8")
    print(row["url"])
elif args[:2] == ["pr", "view"]:
    print(Path(os.environ["GH_TEST_STATE"]).read_text(encoding="utf-8"))
else:
    raise SystemExit(f"unexpected gh invocation: {args!r}")
""",
            encoding="utf-8",
        )
        gh.chmod(0o755)
        log_path = self.fixture.root / "gh-cwd.log"
        state_path = self.fixture.root / "gh-state.json"
        handler = PublishHandler(self.fixture.store)
        first_root = self.fixture.workspace.root
        with patch.dict(
            os.environ,
            {
                "GH_TEST_LOG": str(log_path),
                "GH_TEST_STATE": str(state_path),
                "PATH": str(bin_root) + os.pathsep + os.environ["PATH"],
            },
        ):
            first = handler.execute(
                self.fixture.command,
                self.fixture.claim,
                self.fixture.workspace,
                effects=self.fixture.effects,
            )
            first_calls = log_path.read_text(encoding="utf-8").splitlines()
            shutil.rmtree(first_root)
            state_path.unlink()

            second_branch = OTHER_REVIEW_BRANCH
            snapshot_root = self.fixture.root / "repository-snapshot-2"
            seal_repository_snapshot(
                self.fixture.repo,
                snapshot_root,
                repository_slug="example/robot",
                origin_url=str(self.fixture.remote),
                base_ref="main",
                base_oid=self.fixture.base_oid,
                branch_ref=second_branch,
                head_oid=None,
            )
            snapshot_ref = self.fixture.store.seal_tree(
                snapshot_root,
                kind=publish_stage.REPOSITORY_SNAPSHOT_KIND,
                producer_run_id=None,
                producer_stage=None,
                producer_tool_sha256=digest({"tool": "snapshot-builder-2"}),
            )
            request = self.fixture.command.to_record()["request"]
            request.update(
                {
                    "branch_ref": second_branch,
                    "repository_snapshot_artifact_id": snapshot_ref.artifact_id,
                }
            )
            command = StageCommand.create(
                command_id="publish-command-2",
                run_id=self.fixture.command.run_id,
                stage="publish",
                idempotency_key="publish-run/publish-2",
                request=request,
                input_artifacts=(
                    self.fixture.capture_ref,
                    self.fixture.generate_ref,
                    self.fixture.verify_ref,
                    snapshot_ref,
                ),
                config_snapshot_sha256=self.fixture.command.config_snapshot_sha256,
                contract_sha256=self.fixture.command.contract_sha256,
                policy_sha256=self.fixture.command.policy_sha256,
                expected_executor_sha256=self.fixture.executor,
                predecessor_decision_id="verify-decision",
            )
            claim = replace(
                self.fixture.claim,
                claim_id="publish-claim-2",
                command_id=command.command_id,
                fence=2,
                owner_sha256=digest({"process": "publish-2"}),
                lane_key="example/robot:robot2:engineering",
            )
            second_root = self.fixture.root / "second-workspace"
            workspace = StageWorkspace(
                root=second_root,
                inputs={
                    ref.artifact_id: self.fixture.store.materialize(
                        ref,
                        second_root / "inputs" / ref.artifact_id,
                    )
                    for ref in command.input_artifacts
                },
            )
            effects = _MemoryEffects(
                self.fixture.store,
                command_id=command.command_id,
                executor=self.fixture.executor,
            )
            second = handler.execute(command, claim, workspace, effects=effects)
            all_calls = log_path.read_text(encoding="utf-8").splitlines()

        self.assertEqual(first.outcome, "completed")
        self.assertEqual(second.outcome, "completed")
        self.assertFalse(first_root.exists())
        self.assertTrue(all(Path(path).is_relative_to(first_root) for path in first_calls))
        second_calls = all_calls[len(first_calls) :]
        self.assertTrue(second_calls)
        self.assertTrue(all(Path(path).is_relative_to(second_root) for path in second_calls))
        self.assertFalse(any(Path(path).is_relative_to(first_root) for path in second_calls))
        self.assertIsNone(handler.pull_requests)
        self.assertEqual(effects.authorizations, 2)
        self.assertTrue(all(state is EffectState.SATISFIED for state in effects.states.values()))

    def test_concurrent_claims_use_distinct_default_pull_request_client_cwds(self) -> None:
        bin_root = self.fixture.root / "concurrent-fake-bin"
        bin_root.mkdir()
        gh = bin_root / "gh"
        gh.write_text(
            """#!/usr/bin/python3
import fcntl
import json
import os
import subprocess
import sys
import time
from pathlib import Path

args = sys.argv[1:]
repository = args[args.index("--repo") + 1]
state_root = Path(os.environ["GH_TEST_STATE_DIR"])
state_root.mkdir(parents=True, exist_ok=True)
with Path(os.environ["GH_TEST_LOG"]).open("a", encoding="utf-8") as stream:
    fcntl.flock(stream, fcntl.LOCK_EX)
    stream.write(repository + "\\t" + str(Path.cwd()) + "\\n")
if args[:2] == ["pr", "list"]:
    marker = state_root / (repository.replace("/", "__") + ".marker")
    marker.write_text("ready\\n", encoding="utf-8")
    deadline = time.monotonic() + 10
    while len(list(state_root.glob("*.marker"))) < 2:
        if time.monotonic() >= deadline:
            raise SystemExit("concurrent gh barrier timed out")
        time.sleep(0.02)
    state = state_root / (repository.replace("/", "__") + ".json")
    print(json.dumps([json.loads(state.read_text(encoding="utf-8"))]) if state.exists() else "[]")
elif args[:2] == ["pr", "create"]:
    base = args[args.index("--base") + 1]
    head = args[args.index("--head") + 1]
    title = args[args.index("--title") + 1]
    body = args[args.index("--body") + 1]
    listing = subprocess.check_output(
        ["git", "ls-remote", "origin", f"refs/heads/{head}"],
        text=True,
    ).strip()
    commit = listing.split()[0]
    row = {
        "baseRefName": base,
        "body": body,
        "headRefName": head,
        "headRefOid": commit,
        "number": 1,
        "state": "OPEN",
        "title": title,
        "url": f"https://github.com/{repository}/pull/1",
    }
    (state_root / (repository.replace("/", "__") + ".json")).write_text(
        json.dumps(row),
        encoding="utf-8",
    )
    print(row["url"])
elif args[:2] == ["pr", "view"]:
    state = state_root / (repository.replace("/", "__") + ".json")
    print(state.read_text(encoding="utf-8"))
else:
    raise SystemExit(f"unexpected gh invocation: {args!r}")
""",
            encoding="utf-8",
        )
        gh.chmod(0o755)
        log_path = self.fixture.root / "concurrent-gh-cwd.log"
        state_root = self.fixture.root / "concurrent-gh-state"

        second_remote = self.fixture.root / "remote-2.git"
        second_seed = self.fixture.root / "seed-2"
        second_repo = self.fixture.root / "repo-2"
        git(None, "init", "--bare", "-b", "main", str(second_remote))
        second_seed.mkdir()
        git(None, "init", "-b", "main", str(second_seed))
        git(second_seed, "config", "user.name", "Seed")
        git(second_seed, "config", "user.email", "seed@example.test")
        (second_seed / "README.md").write_text("second base\n", encoding="utf-8")
        git(second_seed, "add", "README.md")
        git(second_seed, "commit", "-m", "second base")
        git(second_seed, "remote", "add", "origin", str(second_remote))
        git(second_seed, "push", "-u", "origin", "main")
        second_base_oid = git(second_seed, "rev-parse", "HEAD").stdout.strip()
        git(None, "clone", str(second_remote), str(second_repo))
        git(second_repo, "config", "user.name", "Seed")
        git(second_repo, "config", "user.email", "seed@example.test")
        second_branch = OTHER_REVIEW_BRANCH
        snapshot_root = self.fixture.root / "concurrent-repository-snapshot-2"
        seal_repository_snapshot(
            second_repo,
            snapshot_root,
            repository_slug="example/robot2",
            origin_url=str(second_remote),
            base_ref="main",
            base_oid=second_base_oid,
            branch_ref=second_branch,
            head_oid=None,
        )
        snapshot_ref = self.fixture.store.seal_tree(
            snapshot_root,
            kind=publish_stage.REPOSITORY_SNAPSHOT_KIND,
            producer_run_id=None,
            producer_stage=None,
            producer_tool_sha256=digest({"tool": "snapshot-builder-2"}),
        )
        request = self.fixture.command.to_record()["request"]
        request.update(
            {
                "branch_ref": second_branch,
                "expected_base_oid": second_base_oid,
                "origin_url": str(second_remote),
                "repository_slug": "example/robot2",
                "repository_snapshot_artifact_id": snapshot_ref.artifact_id,
            }
        )
        second_command = StageCommand.create(
            command_id="publish-command-2",
            run_id=self.fixture.command.run_id,
            stage="publish",
            idempotency_key="publish-run/publish-2",
            request=request,
            input_artifacts=(
                self.fixture.capture_ref,
                self.fixture.generate_ref,
                self.fixture.verify_ref,
                snapshot_ref,
            ),
            config_snapshot_sha256=self.fixture.command.config_snapshot_sha256,
            contract_sha256=self.fixture.command.contract_sha256,
            policy_sha256=self.fixture.command.policy_sha256,
            expected_executor_sha256=self.fixture.executor,
            predecessor_decision_id="verify-decision",
        )
        second_claim = replace(
            self.fixture.claim,
            claim_id="publish-claim-2",
            command_id=second_command.command_id,
            fence=2,
            owner_sha256=digest({"process": "publish-2"}),
            lane_key="example/robot2:robot2:engineering",
        )
        second_workspace = StageWorkspace(
            root=self.fixture.root / "concurrent-workspace-2",
            inputs={
                ref.artifact_id: self.fixture.store.materialize(
                    ref,
                    self.fixture.root / "concurrent-workspace-2/inputs" / ref.artifact_id,
                )
                for ref in second_command.input_artifacts
            },
        )
        second_effects = _MemoryEffects(
            self.fixture.store,
            command_id=second_command.command_id,
            executor=self.fixture.executor,
        )
        handler = PublishHandler(self.fixture.store)
        with (
            patch.dict(
                os.environ,
                {
                    "GH_TEST_LOG": str(log_path),
                    "GH_TEST_STATE_DIR": str(state_root),
                    "PATH": str(bin_root) + os.pathsep + os.environ["PATH"],
                },
            ),
            ThreadPoolExecutor(max_workers=2) as executor,
        ):
            first_future = executor.submit(
                handler.execute,
                self.fixture.command,
                self.fixture.claim,
                self.fixture.workspace,
                effects=self.fixture.effects,
            )
            second_future = executor.submit(
                handler.execute,
                second_command,
                second_claim,
                second_workspace,
                effects=second_effects,
            )
            first = first_future.result(timeout=30)
            second = second_future.result(timeout=30)

        self.assertEqual(first.outcome, "completed")
        self.assertEqual(second.outcome, "completed")
        self.assertIsNone(handler.pull_requests)
        cwd_by_repository: dict[str, set[Path]] = {}
        for row in log_path.read_text(encoding="utf-8").splitlines():
            repository, cwd = row.split("\t", 1)
            cwd_by_repository.setdefault(repository, set()).add(Path(cwd))
        self.assertEqual(
            cwd_by_repository["example/robot"],
            {(self.fixture.workspace.root / "repository").resolve()},
        )
        self.assertEqual(
            cwd_by_repository["example/robot2"],
            {(second_workspace.root / "repository").resolve()},
        )
        self.assertEqual(
            git(
                None,
                "--git-dir",
                str(second_remote),
                "rev-parse",
                f"refs/heads/{second_branch}",
            ).stdout.strip(),
            read_data(Path(second.candidate_root) / "publication-receipt.json")["candidate_commit"],
        )
        self.assertEqual(second_effects.authorizations, 2)

    def test_second_run_reuses_satisfied_effects_without_external_calls(self) -> None:
        first = self._run()
        before = (self.fixture.pr.observe_calls, self.fixture.pr.create_calls, self.fixture.pr.update_calls)
        second_workspace = StageWorkspace(
            root=self.fixture.root / "second-workspace",
            inputs=self.fixture.workspace.inputs,
        )
        second_claim = replace(
            self.fixture.claim,
            claim_id="publish-claim-2",
            fence=2,
            owner_sha256=digest({"process": "publish-2"}),
        )
        with (
            patch.object(PublishHandler, "_prepare", side_effect=AssertionError("must not rebuild publication")),
            patch.object(PublicationRepository, "push", side_effect=AssertionError("must not resend")),
        ):
            second = self._run(workspace=second_workspace, claim=second_claim)
        after = (self.fixture.pr.observe_calls, self.fixture.pr.create_calls, self.fixture.pr.update_calls)
        self.assertEqual(first.checks_sha256, second.checks_sha256)
        self.assertEqual(before, after)

    def test_completed_effect_replay_rechecks_the_live_base(self) -> None:
        self._run()
        before = (dict(self.fixture.effects.intents), self.fixture.effects.authorizations)
        seed = self.fixture.root / "seed"
        (seed / "README.md").write_text("moved after effects completed\n", encoding="utf-8")
        git(seed, "add", "README.md")
        git(seed, "commit", "-m", "move base after publication")
        git(seed, "push", "origin", "main")
        second_workspace = StageWorkspace(
            root=self.fixture.root / "replay-workspace",
            inputs=self.fixture.workspace.inputs,
        )
        second_claim = replace(
            self.fixture.claim,
            claim_id="publish-claim-replay",
            fence=2,
            owner_sha256=digest({"process": "publish-replay"}),
        )
        with (
            patch.object(PublishHandler, "_prepare", side_effect=AssertionError("must not rebuild publication")),
            patch.object(PublicationRepository, "push", side_effect=AssertionError("must not resend")),
            patch.object(self.fixture.pr, "create", side_effect=AssertionError("must not recreate PR")),
            patch.object(self.fixture.pr, "update", side_effect=AssertionError("must not update PR")),
            self.assertRaises(StageFailure) as raised,
        ):
            self._run(workspace=second_workspace, claim=second_claim)
        self.assertEqual(raised.exception.outcome, "failed")
        self.assertEqual(raised.exception.reason_code, "publish.local_contract_failed")
        self.assertEqual(raised.exception.detail["exception"]["code"], "publish.base_moved")
        self.assertEqual((self.fixture.effects.intents, self.fixture.effects.authorizations), before)

    def test_pull_request_reconciliation_reuses_original_outbound_without_writes(self) -> None:
        first, effect_id, intent, outbound = self._pr_recovery_inputs()
        original = self.fixture.pr.rows[0]
        before = (
            self.fixture.pr.observe_calls,
            self.fixture.pr.create_calls,
            self.fixture.pr.update_calls,
        )
        workspace = self.fixture.root / "pr-reconcile-work"
        workspace.mkdir()
        evidence = publish_stage.PublishPullRequestReconciliationAdapter(
            self.fixture.pr
        ).observe(
            command=self.fixture.command,
            intent=intent,
            outbound_root=outbound,
            workspace_root=workspace,
        )
        after = (
            self.fixture.pr.observe_calls,
            self.fixture.pr.create_calls,
            self.fixture.pr.update_calls,
        )
        self.assertEqual(after, (before[0] + 1, before[1], before[2]))
        self.assertEqual((evidence.fact_type, evidence.state_class), ("reconciled", "known"))
        self.assertIsNotNone(evidence.payload_root)
        self.assertEqual(
            set(inventory(evidence.payload_root, exclude=("manifest.json",))),
            {"observation.json", "request.json"},
        )
        payload = read_data(evidence.payload_root / "observation.json")
        self.assertEqual(payload["action"], "reconciled")
        self.assertEqual(payload["pull_request"], original.to_record())

        self.fixture.effects.record_observation(
            effect_id,
            source="test.pr.reconcile",
            state_class="known",
            observation_root=evidence.payload_root,
        )
        second_workspace = StageWorkspace(
            root=self.fixture.root / "pr-reconcile-resume",
            inputs=self.fixture.workspace.inputs,
        )
        second_claim = replace(
            self.fixture.claim,
            claim_id="publish-claim-pr-reconcile",
            fence=2,
            owner_sha256=digest({"process": "publish-pr-reconcile"}),
        )
        before_resume = (
            self.fixture.pr.observe_calls,
            self.fixture.pr.create_calls,
            self.fixture.pr.update_calls,
        )
        with (
            patch.object(PublishHandler, "_prepare", side_effect=AssertionError("must not rebuild")),
            patch.object(PublicationRepository, "push", side_effect=AssertionError("must not resend")),
            patch.object(self.fixture.pr, "create", side_effect=AssertionError("must not create")),
            patch.object(self.fixture.pr, "update", side_effect=AssertionError("must not update")),
        ):
            second = self._run(workspace=second_workspace, claim=second_claim)
        self.assertEqual(first.checks_sha256, second.checks_sha256)
        self.assertEqual(
            (
                self.fixture.pr.observe_calls,
                self.fixture.pr.create_calls,
                self.fixture.pr.update_calls,
            ),
            before_resume,
        )

    def test_pull_request_reconciliation_keeps_inexact_results_nonterminal(self) -> None:
        _first, _effect_id, intent, outbound = self._pr_recovery_inputs()
        original = self.fixture.pr.rows[0]
        adapter = publish_stage.PublishPullRequestReconciliationAdapter(self.fixture.pr)

        cases = (
            ("absent", [], "absent"),
            (
                "conflict",
                [replace(original, title="different title")],
                "unobservable",
            ),
            (
                "ambiguous",
                [
                    original,
                    replace(
                        original,
                        number=2,
                        url="https://example.test/example/robot/pull/2",
                    ),
                ],
                "unobservable",
            ),
        )
        for name, rows, expected_state in cases:
            with self.subTest(name=name):
                self.fixture.pr.rows = list(rows)
                workspace = self.fixture.root / f"pr-reconcile-{name}"
                workspace.mkdir()
                evidence = adapter.observe(
                    command=self.fixture.command,
                    intent=intent,
                    outbound_root=outbound,
                    workspace_root=workspace,
                )
                self.assertEqual(
                    (evidence.fact_type, evidence.state_class),
                    ("observation", expected_state),
                )
                payload = read_data(evidence.payload_root / "observation.json")
                self.assertEqual(payload["action"], expected_state)
                self.assertEqual(payload["observed_pull_requests"], [row.to_record() for row in rows])
        self.assertEqual(self.fixture.pr.create_calls, 1)
        self.assertEqual(self.fixture.pr.update_calls, 0)

    def test_pull_request_reconciliation_binds_the_frozen_existing_pr(self) -> None:
        _first, _effect_id, intent, outbound = self._pr_recovery_inputs()
        original = self.fixture.pr.rows[0]
        request_path = outbound / "request.json"
        request = read_data(request_path)
        request["expected_pr"] = {
            "number": original.number,
            "url": original.url,
            "head_oid": original.head_oid,
        }
        write_json(request_path, request)
        expected_state = {
            name: request[name]
            for name in publish_stage._PR_EXPECTED_STATE_FIELDS
        }
        intent = replace(
            intent,
            expected_state=expected_state,
            expected_state_digest=digest(expected_state),
        )
        adapter = publish_stage.PublishPullRequestReconciliationAdapter(self.fixture.pr)

        workspace = self.fixture.root / "pr-reconcile-existing"
        workspace.mkdir()
        evidence = adapter.observe(
            command=self.fixture.command,
            intent=intent,
            outbound_root=outbound,
            workspace_root=workspace,
        )
        self.assertEqual((evidence.fact_type, evidence.state_class), ("reconciled", "known"))

        self.fixture.pr.rows = [
            replace(original, url="https://example.test/example/robot/pull/other")
        ]
        workspace = self.fixture.root / "pr-reconcile-existing-conflict"
        workspace.mkdir()
        evidence = adapter.observe(
            command=self.fixture.command,
            intent=intent,
            outbound_root=outbound,
            workspace_root=workspace,
        )
        self.assertEqual((evidence.fact_type, evidence.state_class), ("observation", "unobservable"))

    def test_pull_request_reconciliation_rejects_tampered_outbound_before_query(self) -> None:
        _first, _effect_id, intent, outbound = self._pr_recovery_inputs()
        request_path = outbound / "request.json"
        request = read_data(request_path)
        request["body"] += "tampered"
        write_json(request_path, request)
        before = self.fixture.pr.observe_calls
        with self.assertRaisesRegex(PipelineError, "pr_recovery_request"):
            publish_stage.PublishPullRequestReconciliationAdapter(self.fixture.pr).observe(
                command=self.fixture.command,
                intent=intent,
                outbound_root=outbound,
                workspace_root=self.fixture.root / "pr-reconcile-tampered-work",
            )
        self.assertEqual(self.fixture.pr.observe_calls, before)

    def test_sealed_snapshot_replays_after_original_checkout_is_deleted(self) -> None:
        first = self._run()
        first_receipt = read_data(Path(first.candidate_root) / "publication-receipt.json")
        shutil.rmtree(self.fixture.repo)
        second_root = self.fixture.root / "fresh-workspace"
        second_workspace = StageWorkspace(
            root=second_root,
            inputs={
                ref.artifact_id: self.fixture.store.materialize(
                    ref,
                    second_root / "inputs" / ref.artifact_id,
                )
                for ref in self.fixture.command.input_artifacts
            },
        )
        second_claim = replace(
            self.fixture.claim,
            claim_id="publish-claim-fresh",
            fence=2,
            owner_sha256=digest({"process": "publish-fresh"}),
        )
        with (
            patch.object(PublishHandler, "_prepare", side_effect=AssertionError("must not rebuild publication")),
            patch.object(PublicationRepository, "push", side_effect=AssertionError("must not resend")),
            patch.object(self.fixture.pr, "create", side_effect=AssertionError("must not recreate PR")),
            patch.object(self.fixture.pr, "update", side_effect=AssertionError("must not update PR")),
        ):
            second = self._run(workspace=second_workspace, claim=second_claim)
        second_receipt = read_data(Path(second.candidate_root) / "publication-receipt.json")
        self.assertEqual(first_receipt, second_receipt)
        self.assertEqual(first.checks_sha256, second.checks_sha256)

    def test_candidate_recovery_consumes_receipt_without_external_calls(self) -> None:
        execution = self._run()
        output_ref = self.fixture.store.seal_tree(
            execution.candidate_root,
            kind="stage_output:publish",
            producer_run_id=self.fixture.command.run_id,
            producer_stage="publish",
            producer_tool_sha256=self.fixture.executor,
        )
        result = StageResult(
            schema_version=STAGE_RESULT_SCHEMA,
            command_id=self.fixture.command.command_id,
            run_id=self.fixture.command.run_id,
            stage="publish",
            request_digest=self.fixture.command.request_digest,
            outcome="completed",
            output_artifact=output_ref,
            actual_executor_sha256=self.fixture.executor,
            checks_sha256=execution.checks_sha256,
            resources_settled=True,
        )
        before = (self.fixture.pr.observe_calls, self.fixture.pr.create_calls, self.fixture.pr.update_calls)
        with patch.object(PublicationRepository, "push", side_effect=AssertionError("must not resend")):
            validated = self.fixture.handler.validate_candidate(
                self.fixture.command,
                replace(self.fixture.claim, claim_id="publish-claim-2", fence=2),
                StageCandidate(result=result),
                effects=self.fixture.effects,
            )
        after = (self.fixture.pr.observe_calls, self.fixture.pr.create_calls, self.fixture.pr.update_calls)
        self.assertIs(validated.result, result)
        self.assertEqual(before, after)

    def test_base_drift_refuses_before_external_effect(self) -> None:
        seed = self.fixture.root / "seed"
        (seed / "README.md").write_text("moved\n", encoding="utf-8")
        git(seed, "add", "README.md")
        git(seed, "commit", "-m", "move base")
        git(seed, "push", "origin", "main")
        with self.assertRaises(StageFailure) as raised:
            self._run()
        self.assertEqual(raised.exception.outcome, "failed")
        self.assertEqual(raised.exception.reason_code, "publish.local_contract_failed")
        self.assertEqual(raised.exception.detail["exception"]["code"], "publish.base_moved")
        self.assertEqual(self.fixture.effects.authorizations, 0)
        self.assertEqual(self.fixture.effects.intents, {})
        branch_ref = self.fixture.command.request["branch_ref"]
        branch = git(self.fixture.repo, "ls-remote", "origin", f"refs/heads/{branch_ref}")
        self.assertEqual(branch.stdout, "")

    def test_missing_or_tampered_repository_snapshot_is_rejected(self) -> None:
        missing = StageWorkspace(
            root=self.fixture.root / "missing-snapshot",
            inputs={
                key: value
                for key, value in self.fixture.workspace.inputs.items()
                if key != self.fixture.snapshot_ref.artifact_id
            },
        )
        with self.assertRaisesRegex(PipelineError, "input_closure"):
            self._run(workspace=missing)
        snapshot_path = (
            self.fixture.store.objects
            / self.fixture.snapshot_ref.artifact_id
            / "repository.bundle"
        )
        snapshot_path.write_bytes(snapshot_path.read_bytes() + b"tamper")
        with self.assertRaises(StageFailure) as raised:
            self._run()
        self.assertEqual(raised.exception.reason_code, "publish.local_contract_failed")
        self.assertEqual(raised.exception.detail["exception"]["operation"], "publish.verify_inputs")
        self.assertEqual(self.fixture.effects.intents, {})
        self.assertEqual(self.fixture.effects.authorizations, 0)

    def test_head_drift_refuses_before_external_effect(self) -> None:
        branch_ref = self.fixture.command.request["branch_ref"]
        seed = self.fixture.root / "seed"
        git(seed, "checkout", "-b", branch_ref)
        (seed / "other.txt").write_text("existing head\n", encoding="utf-8")
        git(seed, "add", "other.txt")
        git(seed, "commit", "-m", "existing review head")
        git(seed, "push", "origin", branch_ref)
        with self.assertRaises(StageFailure) as raised:
            self._run()
        self.assertEqual(raised.exception.outcome, "failed")
        self.assertEqual(raised.exception.reason_code, "publish.local_contract_failed")
        self.assertEqual(raised.exception.detail["exception"]["code"], "publish.head_moved")
        self.assertEqual(self.fixture.effects.authorizations, 0)
        self.assertEqual(self.fixture.effects.intents, {})
        self.assertEqual(self.fixture.pr.create_calls, 0)

    def test_force_with_lease_preserves_a_racing_remote_head(self) -> None:
        branch_ref = self.fixture.command.request["branch_ref"]
        seed = self.fixture.root / "seed"

        def race_then_push(self, candidate, *, base_ref, branch_ref, expected_head_oid):
            git(seed, "checkout", "main")
            git(seed, "checkout", "-b", "racing")
            (seed / "race.txt").write_text("racing\n", encoding="utf-8")
            git(seed, "add", "race.txt")
            git(seed, "commit", "-m", "racing head")
            git(seed, "push", "origin", f"HEAD:{branch_ref}")
            original(
                self,
                candidate,
                base_ref=base_ref,
                branch_ref=branch_ref,
                expected_head_oid=expected_head_oid,
            )

        original = PublicationRepository.push
        with patch.object(PublicationRepository, "push", race_then_push):
            result = self._run()
        self.assertIsInstance(result, StageDeferral)
        git_effect_id = self.fixture.effects.by_key[publish_stage.GIT_EFFECT_KEY]
        self.assertEqual(result.effect_id, git_effect_id)
        self.assertIs(self.fixture.effects.states[git_effect_id], EffectState.UNKNOWN)
        self.assertEqual(self.fixture.pr.create_calls, 0)
        observed = git(self.fixture.repo, "ls-remote", "origin", f"refs/heads/{branch_ref}").stdout.split()[0]
        racing = git(seed, "rev-parse", "HEAD").stdout.strip()
        self.assertEqual(observed, racing)

    def test_commit_blob_mutation_is_rejected_before_push(self) -> None:
        original = publish_stage.materialize_repository_snapshot

        def materialize_with_hook(*args, **kwargs):
            result = original(*args, **kwargs)
            hook = Path(args[1]) / ".git/hooks/pre-commit"
            hook.write_text(
                "#!/bin/sh\nprintf tampered >> model/robot.json\ngit add model/robot.json\n",
                encoding="utf-8",
            )
            hook.chmod(0o755)
            return result

        with (
            patch.object(
                publish_stage,
                "materialize_repository_snapshot",
                materialize_with_hook,
            ),
            self.assertRaises(StageFailure) as raised,
        ):
            self._run()
        self.assertEqual(raised.exception.outcome, "failed")
        self.assertEqual(raised.exception.reason_code, "publish.local_contract_failed")
        self.assertEqual(raised.exception.detail["exception"]["code"], "publish.blob_mismatch")
        self.assertEqual(self.fixture.effects.intents, {})
        self.assertEqual(self.fixture.effects.authorizations, 0)
        self.assertEqual(self.fixture.pr.create_calls, 0)
        branch_ref = self.fixture.command.request["branch_ref"]
        branch = git(self.fixture.repo, "ls-remote", "origin", f"refs/heads/{branch_ref}")
        self.assertEqual(branch.stdout, "")

    def test_partial_push_remains_unknown_and_does_not_delete_or_rewrite(self) -> None:
        hook = self.fixture.remote / "hooks/pre-receive"
        hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        hook.chmod(0o755)
        result = self._run()
        self.assertIsInstance(result, StageDeferral)
        branch_ref = self.fixture.command.request["branch_ref"]
        branch = git(self.fixture.repo, "ls-remote", "origin", f"refs/heads/{branch_ref}")
        self.assertEqual(branch.stdout, "")
        git_effect_id = self.fixture.effects.by_key[publish_stage.GIT_EFFECT_KEY]
        self.assertEqual(result.effect_id, git_effect_id)
        self.assertIs(self.fixture.effects.states[git_effect_id], EffectState.UNKNOWN)
        self.assertEqual(self.fixture.pr.create_calls, 0)

    def test_unavailable_git_readback_returns_deferral(self) -> None:
        original_observe = PublicationRepository.observe
        push_attempted = False

        def lose_push(*args, **kwargs):
            nonlocal push_attempted
            del args, kwargs
            push_attempted = True
            raise RuntimeError("response lost")

        def observe_after_push_fails(*args, **kwargs):
            if push_attempted:
                raise RuntimeError("readback unavailable")
            return original_observe(*args, **kwargs)

        with (
            patch.object(
                PublicationRepository,
                "push",
                autospec=True,
                side_effect=lose_push,
            ),
            patch.object(
                PublicationRepository,
                "observe",
                autospec=True,
                side_effect=observe_after_push_fails,
            ),
        ):
            result = self._run()
        self.assertIsInstance(result, StageDeferral)
        git_effect_id = self.fixture.effects.by_key[publish_stage.GIT_EFFECT_KEY]
        self.assertEqual(result.effect_id, git_effect_id)
        self.assertIs(self.fixture.effects.states[git_effect_id], EffectState.UNKNOWN)
        self.assertEqual(self.fixture.pr.create_calls, 0)

    def test_multiple_exact_pull_requests_conflict_without_pr_write(self) -> None:
        self.fixture.pr.force_multiple = True
        with self.assertRaises(StageFailure) as raised:
            self._run()
        self.assertEqual(raised.exception.reason_code, "publish_pr_ambiguous")
        self.assertEqual(self.fixture.pr.create_calls, 0)
        self.assertEqual(self.fixture.pr.update_calls, 0)
        branch_ref = self.fixture.command.request["branch_ref"]
        branch = git(self.fixture.repo, "ls-remote", "origin", f"refs/heads/{branch_ref}")
        self.assertNotEqual(branch.stdout, "")

    def test_lost_git_response_reconciles_same_effect(self) -> None:
        original = PublicationRepository.push

        def push_then_lose(self, candidate, *, base_ref, branch_ref, expected_head_oid):
            original(
                self,
                candidate,
                base_ref=base_ref,
                branch_ref=branch_ref,
                expected_head_oid=expected_head_oid,
            )
            raise RuntimeError("response lost")

        with patch.object(PublicationRepository, "push", push_then_lose):
            execution = self._run()
        self.assertEqual(execution.outcome, "completed")
        git_effect = next(
            state for key, state in self.fixture.effects.states.items()
            if self.fixture.effects.intents[key].effect_key == publish_stage.GIT_EFFECT_KEY
        )
        self.assertIs(git_effect, EffectState.SATISFIED)

    def test_lost_pr_response_reconciles_same_effect(self) -> None:
        self.fixture.pr.lose_next_response = True
        execution = self._run()
        self.assertEqual(execution.outcome, "completed")
        self.assertEqual(len(self.fixture.pr.rows), 1)
        self.assertEqual(
            sum(
                1
                for intent in self.fixture.effects.intents.values()
                if intent.effect_key == publish_stage.PR_EFFECT_KEY
            ),
            1,
        )

    def test_unobserved_pr_effect_returns_deferral_without_resend(self) -> None:
        original_create = self.fixture.pr.create
        original_observe = self.fixture.pr.observe

        def create_then_lose(**kwargs):
            original_create(**kwargs)
            raise RuntimeError("response lost")

        def observe_after_write_fails(**kwargs):
            if self.fixture.pr.create_calls:
                raise RuntimeError("readback unavailable")
            return original_observe(**kwargs)

        with (
            patch.object(self.fixture.pr, "create", side_effect=create_then_lose),
            patch.object(self.fixture.pr, "observe", side_effect=observe_after_write_fails),
        ):
            result = self._run()

        self.assertIsInstance(result, StageDeferral)
        effect_id = self.fixture.effects.by_key[publish_stage.PR_EFFECT_KEY]
        self.assertEqual(result.effect_id, effect_id)
        self.assertIs(self.fixture.effects.states[effect_id], EffectState.UNKNOWN)
        self.assertEqual(self.fixture.pr.create_calls, 1)
        self.assertEqual(self.fixture.pr.update_calls, 0)

    def test_mismatched_pr_response_returns_deferral(self) -> None:
        request = self.fixture.command.request

        def create_mismatch(**kwargs):
            self.fixture.pr.create_calls += 1
            return PullRequestObservation(
                number=1,
                url="https://example.test/example/robot/pull/1",
                state="OPEN",
                base_ref=request["base_ref"],
                head_ref=request["branch_ref"],
                head_oid=kwargs["candidate_commit"],
                title="different",
                body="different",
            )

        with patch.object(self.fixture.pr, "create", side_effect=create_mismatch):
            result = self._run()

        self.assertIsInstance(result, StageDeferral)
        effect_id = self.fixture.effects.by_key[publish_stage.PR_EFFECT_KEY]
        self.assertEqual(result.effect_id, effect_id)
        self.assertIs(self.fixture.effects.states[effect_id], EffectState.UNKNOWN)
        self.assertEqual(self.fixture.pr.create_calls, 1)
        self.assertEqual(self.fixture.pr.update_calls, 0)

    def test_git_reconciliation_binds_warning_source_to_the_sealed_candidate(self) -> None:
        self._run()
        wait, _artifact, outbound = self._probe_source(publish_stage.GIT_EFFECT_KEY, "git-certificate-binding")
        plan_path = outbound / publish_stage.PUBLICATION_PLAN
        original = read_data(plan_path)
        request_path = outbound / "request.json"
        original_request = read_data(request_path)
        for field, changed, code in (
            ("verification_certificate_sha256", "f" * 64, "publish.git_recovery_certificate"),
            ("candidate_tree", "f" * 40, "publish.git_recovery_intent"),
        ):
            with self.subTest(field=field):
                write_json(plan_path, {**original, field: changed})
                write_json(
                    request_path,
                    {**original_request, field: changed} if field == "candidate_tree" else original_request,
                )
                workspace = self.fixture.root / f"git-certificate-binding-{field}"
                with (
                    patch.object(
                        PublicationRepository, "observe", side_effect=AssertionError("invalid source was queried")
                    ),
                    self.assertRaisesRegex(PipelineError, code),
                ):
                    publish_stage.PublishGitReconciliationAdapter().observe(
                        command=self.fixture.command,
                        intent=self.fixture.effects.intents[wait.effect_id],
                        outbound_root=outbound,
                        workspace_root=workspace,
                    )
                self.assertFalse((workspace / "publish-evidence").exists())
        write_json(plan_path, {**original, "pr_body_sha256": "f" * 64})
        write_json(request_path, original_request)
        request = publish_stage._request_contract(self.fixture.command)
        repository = publish_stage.clone_publication_repository(
            request.origin_url, self.fixture.root / "pr-text-consumer-repository",
        )
        with patch.object(
            publish_stage, "_publication_text", side_effect=AssertionError("Git reader must not derive PR content")
        ):
            sealed = publish_stage._read_sealed_git_outbound_root(
                repository=repository, request=request, outbound_root=outbound,
            )
        with self.assertRaisesRegex(PipelineError, "publish.git_recovery_pr_text"):
            publish_stage._sealed_publication_text(repository=repository, request=request, sealed=sealed)

    def test_resume_reuses_settled_effects_without_external_writes(self) -> None:
        first = self._run()
        self.assertEqual(first.outcome, "completed")
        before = (
            self.fixture.effects.authorizations,
            self.fixture.pr.observe_calls,
            self.fixture.pr.create_calls,
            self.fixture.pr.update_calls,
        )
        for index, effect_key in enumerate(
            (publish_stage.GIT_EFFECT_KEY, publish_stage.PR_EFFECT_KEY)
        ):
            workspace = StageWorkspace(
                root=self.fixture.root / f"resume-settled-{index}",
                inputs=self.fixture.workspace.inputs,
            )
            with (
                patch.object(
                    PublicationRepository,
                    "push",
                    side_effect=AssertionError("a resumed Git effect must not be sent"),
                ),
                patch.object(
                    self.fixture.pr,
                    "create",
                    side_effect=AssertionError("a resumed PR effect must not be sent"),
                ),
                patch.object(
                    self.fixture.pr,
                    "update",
                    side_effect=AssertionError("a resumed PR effect must not be sent"),
                ),
            ):
                resumed = self._run(
                    workspace=workspace,
                    resume=self._effect_wait(effect_key),
                )

            self.assertEqual(resumed.outcome, "completed")
            self.assertEqual(
                (
                    self.fixture.effects.authorizations,
                    self.fixture.pr.observe_calls,
                    self.fixture.pr.create_calls,
                    self.fixture.pr.update_calls,
                ),
                before,
            )

    def test_publish_handler_separates_normal_and_resume_protocols(self) -> None:
        execute_parameters = inspect.signature(
            PublishHandler.execute,
            eval_str=True,
        ).parameters
        resume_parameters = inspect.signature(
            PublishHandler.resume_stage,
            eval_str=True,
        ).parameters
        self.assertNotIn("resume", execute_parameters)
        self.assertIn("resume", resume_parameters)
        self.assertIs(
            resume_parameters["resume"].annotation,
            StageEffectWait,
        )

    def test_git_status_probe_is_read_only_and_hints_exact_refs_only(self) -> None:
        first = self._run()
        self.assertEqual(first.outcome, "completed")
        wait, artifact, root = self._probe_source(
            publish_stage.GIT_EFFECT_KEY,
            "git-probe",
        )
        before = inventory(root)
        intent = self.fixture.effects.intents[wait.effect_id]
        base_ref = intent.expected_state["base_ref"]
        branch_ref = intent.expected_state["branch_ref"]
        expected_base = intent.expected_state["expected_base_oid"]
        candidate = intent.expected_state["candidate_commit"]
        results = iter(
            (
                SimpleNamespace(
                    returncode=0,
                    stdout=(
                        f"{expected_base} refs/heads/{base_ref}\n"
                        f"{candidate} refs/heads/{branch_ref}\n"
                    ).encode(),
                    stderr=b"",
                ),
                SimpleNamespace(
                    returncode=0,
                    stdout=f"{'f' * 40} refs/heads/{base_ref}\n".encode(),
                    stderr=b"",
                ),
                SimpleNamespace(returncode=0, stdout=b"", stderr=b""),
                SimpleNamespace(returncode=1, stdout=b"", stderr=b"not found"),
            )
        )
        calls = []

        async def runner(argv, timeout):
            calls.append((argv, timeout))
            return next(results)

        probe = publish_stage.PublishGitStatusProbe.from_sealed_outbound(
            wait=wait,
            outbound=artifact,
            outbound_root=root,
            runner=runner,
        )
        exact = asyncio.run(probe.observe())
        base_drift = asyncio.run(probe.observe())
        empty = asyncio.run(probe.observe())
        unavailable = asyncio.run(probe.observe())

        self.assertIsInstance(probe, PublishGitStatusProbe)
        self.assertTrue(exact.changed)
        self.assertTrue(base_drift.changed)
        self.assertFalse(empty.changed)
        self.assertFalse(unavailable.changed)
        self.assertEqual(exact.wait_digest, wait.digest)
        self.assertTrue(all(call[0][:3] == ("git", "ls-remote", "--") for call in calls))
        self.assertTrue(all(call[1] == 15.0 for call in calls))
        self.assertEqual(inventory(root), before)

    def test_git_status_probe_runs_the_default_child_contract(self) -> None:
        first = self._run()
        self.assertEqual(first.outcome, "completed")
        wait, artifact, root = self._probe_source(
            publish_stage.GIT_EFFECT_KEY,
            "git-probe-default-runner",
        )
        probe = publish_stage.PublishGitStatusProbe.from_sealed_outbound(
            wait=wait,
            outbound=artifact,
            outbound_root=root,
        )
        hint = asyncio.run(probe.observe())
        self.assertTrue(hint.changed)
        self.assertEqual(hint.wait_digest, wait.digest)

    def test_pr_status_probe_uses_one_exact_read_only_hint(self) -> None:
        first = self._run()
        self.assertEqual(first.outcome, "completed")
        wait, artifact, root = self._probe_source(
            publish_stage.PR_EFFECT_KEY,
            "pr-probe",
        )
        before = inventory(root)
        request = read_data(root / "request.json")
        expected_pr = request["expected_pr"]
        exact_row = {
            "number": expected_pr["number"] if expected_pr is not None else 7,
            "url": expected_pr["url"] if expected_pr is not None else "https://example.test/pull/7",
            "state": "OPEN",
            "baseRefName": request["base_ref"],
            "headRefName": request["head_ref"],
            "headRefOid": request["candidate_commit"],
            "title": request["title"],
            "body": request["body"],
        }
        results = iter(
            (
                SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps([exact_row]).encode(),
                    stderr=b"",
                ),
                SimpleNamespace(returncode=0, stdout=b"[]", stderr=b""),
                SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps([{**exact_row, "title": "different"}]).encode(),
                    stderr=b"",
                ),
                SimpleNamespace(returncode=1, stdout=b"", stderr=b"not found"),
            )
        )
        calls = []

        async def runner(argv, timeout):
            calls.append((argv, timeout))
            return next(results)

        probe = publish_stage.PublishPullRequestStatusProbe.from_sealed_outbound(
            wait=wait,
            outbound=artifact,
            outbound_root=root,
            runner=runner,
        )
        exact = asyncio.run(probe.observe())
        empty = asyncio.run(probe.observe())
        wrong = asyncio.run(probe.observe())
        unavailable = asyncio.run(probe.observe())

        self.assertIsInstance(probe, PublishPullRequestStatusProbe)
        self.assertTrue(exact.changed)
        self.assertFalse(empty.changed)
        self.assertFalse(wrong.changed)
        self.assertFalse(unavailable.changed)
        self.assertEqual(exact.wait_digest, wait.digest)
        self.assertTrue(all(call[0][:3] == ("gh", "pr", "list") for call in calls))
        self.assertTrue(all(call[1] == 15.0 for call in calls))
        self.assertEqual(inventory(root), before)

    def test_publish_status_probe_rejects_tampered_wait(self) -> None:
        first = self._run()
        self.assertEqual(first.outcome, "completed")
        wait, artifact, root = self._probe_source(
            publish_stage.GIT_EFFECT_KEY,
            "git-probe-tampered",
        )
        with self.assertRaises(ProtocolError) as raised:
            publish_stage.PublishGitStatusProbe.from_sealed_outbound(
                wait=replace(wait, expected_state_digest="0" * 64),
                outbound=artifact,
                outbound_root=root,
            )
        self.assertEqual(raised.exception.code, "publish.resume_wait")

    def test_publish_status_probe_runner_has_no_stdin_or_unbounded_child(self) -> None:
        no_stdin = asyncio.run(
            publish_stage._run_read_only_command(
                (
                    sys.executable,
                    "-c",
                    "import sys; sys.stdout.write(sys.stdin.read())",
                ),
                2.0,
            )
        )
        self.assertEqual((no_stdin.returncode, no_stdin.stdout), (0, b""))
        timed_out = asyncio.run(
            publish_stage._run_read_only_command(
                (sys.executable, "-c", "import time; time.sleep(60)"),
                0.1,
            )
        )
        self.assertEqual(timed_out.returncode, -1)

    def test_unknown_pr_effect_requires_the_claimless_reconciler(self) -> None:
        self._run()
        self.fixture.effects.force_unknown(publish_stage.PR_EFFECT_KEY)
        workspace = StageWorkspace(
            root=self.fixture.root / "unknown-pr-workspace",
            inputs=self.fixture.workspace.inputs,
        )
        before = (
            self.fixture.pr.observe_calls,
            self.fixture.pr.create_calls,
            self.fixture.pr.update_calls,
        )
        result = self._run(workspace=workspace)
        self.assertIsInstance(result, StageDeferral)
        effect_id = self.fixture.effects.by_key[publish_stage.PR_EFFECT_KEY]
        self.assertEqual(result.effect_id, effect_id)
        self.assertEqual(
            (
                self.fixture.pr.observe_calls,
                self.fixture.pr.create_calls,
                self.fixture.pr.update_calls,
            ),
            before,
        )

    def test_unknown_effect_requires_claim_free_reconciliation_without_resend(self) -> None:
        self.fixture.effects.prepare_effect(
            effect_key=publish_stage.GIT_EFFECT_KEY,
            effect_kind="git_ref_update",
            target={
                "repository": "example/robot",
                "branch_ref": self.fixture.command.request["branch_ref"],
            },
            expected_state={
                "schema_version": "solidworks-to-urdf.git-ref-update/v1",
                "repository": "example/robot",
                "base_ref": "main",
                "expected_base_oid": "a" * 40,
                "branch_ref": self.fixture.command.request["branch_ref"],
                "expected_head_oid": None,
                "candidate_commit": "b" * 40,
                "candidate_tree": "c" * 40,
                "subject_sha256": "d" * 64,
            },
            outbound_root=self._root_with_file("unknown-effect"),
        )
        self.fixture.effects.force_unknown(publish_stage.GIT_EFFECT_KEY)
        with patch.object(
            PublicationRepository,
            "push",
            side_effect=AssertionError("must not resend"),
        ):
            result = self._run()
        self.assertIsInstance(result, StageDeferral)
        git_effect_id = self.fixture.effects.by_key[publish_stage.GIT_EFFECT_KEY]
        self.assertEqual(result.effect_id, git_effect_id)
        self.assertEqual(self.fixture.pr.observe_calls, 0)
        self.assertEqual(self.fixture.pr.create_calls, 0)
        self.assertEqual(self.fixture.pr.update_calls, 0)

    def test_resume_rejects_noncanonical_wait_identity(self) -> None:
        first = self._run()
        self.assertEqual(first.outcome, "completed")
        wait = self._effect_wait(publish_stage.PR_EFFECT_KEY)
        tampered = (
            replace(wait, run_id="other-run"),
            replace(wait, stage="verify"),
            replace(wait, command_id="other-command"),
            replace(wait, request_digest="0" * 64),
            replace(wait, effect_id="other-effect"),
            replace(wait, effect_key="publish.other"),
            replace(wait, effect_kind="git_ref_update"),
            replace(wait, intent_identity_digest="0" * 64),
            replace(wait, outbound_artifact_id="other-outbound"),
            replace(wait, outbound_digest="0" * 64),
            replace(wait, expected_state_digest="0" * 64),
        )
        with patch.object(
            PublicationRepository,
            "push",
            side_effect=AssertionError("a rejected wait must not be sent"),
        ):
            for index, invalid in enumerate(tampered):
                workspace = StageWorkspace(
                    root=self.fixture.root / f"resume-invalid-{index}",
                    inputs=self.fixture.workspace.inputs,
                )
                with self.subTest(index=index):
                    with self.assertRaises(ProtocolError) as raised:
                        self._run(workspace=workspace, resume=invalid)
                    self.assertEqual(raised.exception.code, "publish.resume_wait")

    def _root_with_file(self, name: str) -> Path:
        root = self.fixture.root / name
        root.mkdir()
        (root / "request.json").write_text("{}\n", encoding="utf-8")
        return root


class PublishReceiptTests(unittest.TestCase):
    def test_receipt_rejects_missing_required_effect_fields(self) -> None:
        fixture = PublishFixture()
        self.addCleanup(fixture.close)
        execution = fixture.handler.execute(fixture.command, fixture.claim, fixture.workspace, effects=fixture.effects)
        artifact = fixture.store.seal_tree(
            execution.candidate_root,
            kind="stage_output:publish",
            producer_run_id=fixture.command.run_id,
            producer_stage="publish",
            producer_tool_sha256=fixture.executor,
        )
        receipt = read_data(Path(execution.candidate_root) / "publication-receipt.json")
        receipt["pr_effect"].pop("effect_id")
        with self.assertRaisesRegex(PipelineError, "incomplete"):
            validate_publication_receipt(receipt, command=fixture.command, claim=fixture.claim, artifact=artifact)


class PublicationWarningProjectionTests(unittest.TestCase):
    def test_saved_state_warning_is_projected_into_the_pull_request(self) -> None:
        detail = {
            "classification": "continuable_missing_referenced_documents",
            "open_errors": 2,
            "open_warnings": 1048576,
            "configuration": "Default",
            "missing_references": {
                "state": "observed",
                "items": [{"name": "missing", "last_known_path": r"C:\missing.SLDPRT"}],
            },
            "actual_scope": {
                "graph_nodes": 3,
                "occurrence_count": 7,
                "selected_files": ["a", "b", "c"],
                "excluded_files": ["d"],
                "unresolved_occurrences": [{"instance": "missing-1"}],
            },
            "source_integrity_proven": False,
            "rebuilt": False,
            "mechanical_qualification_complete": False,
            "hardware_qualified": False,
        }
        text = publish_stage._publication_warning_text(
            {
                "diagnostics": [
                    {
                        "code": "source.saved_state_incomplete",
                        "blocking": False,
                        "message": "The assembly was exported from its saved state.",
                        "detail": detail,
                    }
                ]
            }
        )

        self.assertIn("source.saved_state_incomplete", text)
        recorded = json.loads(text.split("```json\n", 1)[1].split("\n```", 1)[0])
        self.assertEqual(recorded["detail"], detail)
        self.assertIn("reports/verification-certificate.json", text)
        self.assertNotIn("reports/quality.json", text)

    def test_malformed_certificate_diagnostics_cannot_be_published(self) -> None:
        for diagnostic in (
            {"blocking": False, "message": "Missing code"},
            "not an object", {"code": "unknown"},
            {"code": "unknown", "blocking": "false"},
            {"code": "blocking", "blocking": True},
        ):
            with self.subTest(diagnostic=diagnostic):
                fixture = PublishFixture(diagnostics=[diagnostic])
                self.addCleanup(fixture.close)
                with self.assertRaises(StageFailure) as raised:
                    fixture.handler.execute(
                        fixture.command, fixture.claim, fixture.workspace, effects=fixture.effects,
                    )
                self.assertEqual(raised.exception.reason_code, "publish.local_contract_failed")
                self.assertIn("diagnostic", raised.exception.detail["exception"]["message"])
                self.assertEqual(fixture.effects.intents, {})
                self.assertEqual(fixture.effects.authorizations, 0)
                self.assertEqual(fixture.pr.rows, [])

    def test_published_warning_links_resolve_to_the_bound_certificate(self) -> None:
        warning = {
            "code": "limits.default_policy", "blocking": False,
            "object": "joint:left", "detail": {"field": "limits", "hardware_qualified": False},
        }
        fixture = PublishFixture(diagnostics=[warning])
        self.addCleanup(fixture.close)
        result = fixture.handler.execute(
            fixture.command, fixture.claim, fixture.workspace, effects=fixture.effects,
        )
        self.assertEqual(result.outcome, "completed")
        body = fixture.pr.rows[0].body
        self.assertIn("reports/verification-certificate.json", body)
        self.assertNotIn("reports/quality.json", body)
        raw = git(
            None, "--git-dir", str(fixture.remote), "show",
            f"refs/heads/{fixture.command.request['branch_ref']}:reports/verification-certificate.json",
        ).stdout
        certificate = json.loads(raw)
        self.assertIn(warning, certificate["diagnostics"])
        self.assertIn('"hardware_qualified": false', body)

    def test_warning_categories_get_a_record_before_repeated_details(self) -> None:
        repeated = [
            {"code": "source.first", "blocking": False, "object": f"part-{index}",
             "detail": {"field": "identity", "source": "x" * 2000}}
            for index in range(100)
        ]
        last = {"code": "source.last", "blocking": False, "object": "last-part",
                "detail": {"field": "coverage", "qualified": False}}
        text = publish_stage._publication_warning_text({"diagnostics": [*repeated, last]})
        self.assertLess(len(text), 50000)
        self.assertIn("`source.first`: 100 records", text)
        self.assertIn("`source.last`: 1 records", text)
        self.assertIn('"object": "last-part"', text)
        self.assertIn('"qualified": false', text)
        self.assertIn("additional warning records", text)

    def test_all_warning_kinds_keep_context_and_long_summaries_are_explicit(self) -> None:
        warning = {
            "code": "limits.default_policy", "blocking": False,
            "object": "joint:left", "field": "limits",
            "detail": {"native_cad_ranges": False, "hardware_qualified": False, "original": [0, 0]},
        }
        text = publish_stage._publication_warning_text({"diagnostics": [warning]})
        recorded = json.loads(text.split("```json\n", 1)[1].split("\n```", 1)[0])
        self.assertEqual(recorded, {k: v for k, v in warning.items() if k != "code"})
        large = {**warning, "detail": {"objects": ["component-" + "x" * 100] * 1000}}
        text = publish_stage._publication_warning_text({"diagnostics": [warning, large]})
        self.assertLess(len(text), 50000)
        self.assertIn("1 additional warning records", text)
        self.assertIn("verification-certificate.json", text)



if __name__ == "__main__":
    unittest.main()
