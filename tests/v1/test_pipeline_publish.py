"""Publish handler tests over local Git and a controlled pull-request client."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import threading
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen

from description_pipeline.artifacts import ArtifactStore
from description_pipeline.io import PipelineError, digest, file_digest, inventory, read_data, write_json
from description_pipeline.interface_records import check_policy_digest
from description_pipeline.pipeline.contracts import (
    ExecutionClaim,
    StageCandidate,
    StageCommand,
    StageFailure,
    StageResult,
)
from description_pipeline.pipeline.effects import EffectIntent, EffectSettlement, EffectState
from description_pipeline.pipeline.engine import StageWorkspace
from description_pipeline.pipeline.stages import publish as publish_stage
from description_pipeline.pipeline.stages.publish import (
    CAPTURE_ARTIFACT_KIND,
    GENERATE_ARTIFACT_KIND,
    PUBLISH_OUTPUT_CONTRACT,
    PUBLISH_REQUEST_SCHEMA,
    VERIFY_ARTIFACT_KIND,
    PublishHandler,
    validate_publication_receipt,
)
from description_pipeline.repository.publish import (
    PublicationRepository,
    PullRequestObservation,
    seal_repository_snapshot,
)
from description_pipeline.runtime import tool_record
from description_pipeline.schemas import ENGINEERING_MODEL_SCHEMA
from description_pipeline.stages import CONTRACT_SHA256

VERIFY_IDENTITY = {
    "role": "verify-fixture",
    "version": "1",
    "source_sha256": "a" * 64,
}
VERIFY_SHA256 = VERIFY_IDENTITY["source_sha256"]
POLICY_SHA = check_policy_digest()
CONTRACT_SHA = CONTRACT_SHA256
CONFIG_SHA = digest({"config": "fixture"})


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
    def __init__(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="publish-fixture-")
        self.root = Path(self.temporary.name)
        self.store = ArtifactStore(self.root / "cas")
        self.tool = tool_record(role="portable")
        self.executor = self.tool["source_sha256"]
        self._make_repository()
        capture_root = self.root / "capture"
        write_json(capture_root / "input/request.json", {"purpose": "engineering"})
        write_json(capture_root / "input/source.json", {"selection": {"hardware_id": "robot"}})
        write_json(capture_root / "evidence/raw/proof.json", {"proof": 1})
        self.capture_ref = self.store.seal_tree(
            capture_root,
            kind=CAPTURE_ARTIFACT_KIND,
            producer_run_id="capture-run",
            producer_stage="capture",
            producer_tool_sha256=digest({"tool": "capture"}),
        )
        generate_root = self.root / "generate"
        write_json(
            generate_root / "model/robot.json",
            {
                "schema_version": ENGINEERING_MODEL_SCHEMA,
                "name": "robot",
            },
        )
        (generate_root / "urdf").mkdir(parents=True)
        (generate_root / "urdf/robot.urdf").write_text('<robot name="robot"/>\n', encoding="utf-8")
        (generate_root / "meshes").mkdir()
        (generate_root / "meshes/part.stl").write_bytes(b"solid fixture\n")
        write_json(generate_root / "reports/generate.json", {"generated": True})
        write_json(generate_root / "tool.json", self.tool)
        self.generate_ref = self.store.seal_tree(
            generate_root,
            kind=GENERATE_ARTIFACT_KIND,
            producer_run_id="generate-run",
            producer_stage="generate",
            producer_tool_sha256=self.executor,
        )
        verify_root, self.certificate = self._make_verify(capture_root, generate_root)
        self.verify_ref = self.store.seal_tree(
            verify_root,
            kind=VERIFY_ARTIFACT_KIND,
            producer_run_id="verify-run",
            producer_stage="verify",
            producer_tool_sha256=VERIFY_SHA256,
        )
        snapshot_root = self.root / "repository-snapshot"
        self.snapshot = seal_repository_snapshot(
            self.repo,
            snapshot_root,
            repository_slug="example/robot",
            origin_url=str(self.remote),
            base_ref="main",
            base_oid=self.base_oid,
            branch_ref="work/solidworks/robot/engineering",
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
                "repository_snapshot_artifact_id": self.snapshot_ref.artifact_id,
                "verified_subject_sha256": self.generate_ref.set_sha256,
                "repository_slug": "example/robot",
                "origin_url": str(self.remote),
                "base_ref": "main",
                "expected_base_oid": self.base_oid,
                "branch_ref": "work/solidworks/robot/engineering",
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
            input_artifacts=(self.capture_ref, self.generate_ref, self.verify_ref, self.snapshot_ref),
            config_snapshot_sha256=CONFIG_SHA,
            contract_sha256=CONTRACT_SHA,
            policy_sha256=POLICY_SHA,
            expected_executor_sha256=self.executor,
            predecessor_decision_id="verify-decision",
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
                self.snapshot_ref.artifact_id: self.store.materialize(
                    self.snapshot_ref, self.root / "workspace/inputs/repository-snapshot"
                ),
            },
        )
        self.effects = _MemoryEffects(self.store, command_id=self.command.command_id, executor=self.executor)
        self.pr = _PullRequests()
        self.handler = PublishHandler(self.store, pull_requests=self.pr)

    def _make_repository(self) -> None:
        self.remote = self.root / "remote.git"
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

    def _make_verify(self, capture_root: Path, generate_root: Path) -> tuple[Path, dict]:
        pair_subject = tree_digest(
            {
                **{"input/" + path: checksum for path, checksum in inventory(capture_root / "input").items()},
                **{"evidence/" + path: checksum for path, checksum in inventory(capture_root / "evidence").items()},
                **{"model/" + path: checksum for path, checksum in inventory(generate_root / "model").items()},
                **{"urdf/" + path: checksum for path, checksum in inventory(generate_root / "urdf").items()},
                **{"meshes/" + path: checksum for path, checksum in inventory(generate_root / "meshes").items()},
                "tool.json": file_digest(generate_root / "tool.json"),
            }
        )
        verify_request = {
            "capture_artifact": self.capture_ref.to_record(),
            "generate_artifact": self.generate_ref.to_record(),
            "expected_verifier_sha256": VERIFY_SHA256,
            "contract_sha256": CONTRACT_SHA,
            "policy_sha256": POLICY_SHA,
            "config_snapshot_sha256": CONFIG_SHA,
            "purpose": "engineering",
            "schema_version": "description-pipeline.verify-request/v1",
            "output_contract": "solidworks-to-urdf.verification-artifact/v1",
        }
        proof = {
            "actual_verifier": VERIFY_IDENTITY,
            "actual_verifier_sha256": VERIFY_SHA256,
            "capture_artifact": self.capture_ref.to_record(),
            "checks": [{"id": "fixture", "state": "passed", "passed": True}],
            "closure_sha256": digest(
                {
                    "capture_artifact": self.capture_ref.to_record(),
                    "generate_artifact": self.generate_ref.to_record(),
                }
            ),
            "config_snapshot_sha256": CONFIG_SHA,
            "contract_sha256": CONTRACT_SHA,
            "decision": "PASS",
            "diagnostics": [],
            "evaluation_subject_sha256": pair_subject,
            "generate_artifact": self.generate_ref.to_record(),
            "generate_receipt_sha256": file_digest(generate_root / "reports/generate.json"),
            "passed": True,
            "pipeline_id": "solidworks-to-urdf",
            "policy_sha256": POLICY_SHA,
            "purpose": "engineering",
            "qualifications": [],
            "request": verify_request,
            "request_sha256": digest(verify_request),
            "required_checks": ["fixture"],
            "schema_version": "description-pipeline.verification-certificate/v1",
            "subject_sha256": self.generate_ref.set_sha256,
            "subject_status": "bound",
        }
        certificate = {**proof, "verification_key": digest(proof)}
        verify_root = self.root / "verify"
        write_json(verify_root / "verification-certificate.json", certificate)
        write_json(
            verify_root / "verification-execution.json",
            {
                "actual_verifier": VERIFY_IDENTITY,
                "actual_verifier_sha256": VERIFY_SHA256,
                "certificate_sha256": digest(certificate),
                "claim_id": "verify-claim",
                "closure_sha256": proof["closure_sha256"],
                "command_id": "verify-command",
                "fence": 1,
                "finished_at": "2026-10-25T00:00:01+00:00",
                "raw_checks": [],
                "raw_diagnostics": [],
                "request": verify_request,
                "request_sha256": digest(verify_request),
                "run_id": "verify-run",
                "schema_version": "description-pipeline.verification-execution/v1",
                "stage": "verify",
                "started_at": "2026-10-25T00:00:00+00:00",
                "execution_id": "verify-execution",
            },
        )
        return verify_root, certificate

    def close(self) -> None:
        self.temporary.cleanup()


class PublishHandlerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = PublishFixture()
        self.addCleanup(self.fixture.close)

    def _run(self, *, workspace=None, claim=None, effects=None, handler=None):
        return (handler or self.fixture.handler).execute(
            self.fixture.command,
            claim or self.fixture.claim,
            workspace or self.fixture.workspace,
            effects=effects or self.fixture.effects,
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
        with patch.object(PublicationRepository, "push", side_effect=AssertionError("must not resend")):
            second = self._run(workspace=second_workspace, claim=second_claim)
        after = (self.fixture.pr.observe_calls, self.fixture.pr.create_calls, self.fixture.pr.update_calls)
        self.assertEqual(first.checks_sha256, second.checks_sha256)
        self.assertEqual(before, after)

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
            schema_version="description-pipeline.stage-result/v1",
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
        with self.assertRaisesRegex(PipelineError, "publish.base_moved"):
            self._run()
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
        with self.assertRaises(PipelineError):
            self._run()

    def test_head_drift_refuses_before_external_effect(self) -> None:
        branch_ref = self.fixture.command.request["branch_ref"]
        seed = self.fixture.root / "seed"
        git(seed, "checkout", "-b", branch_ref)
        (seed / "other.txt").write_text("existing head\n", encoding="utf-8")
        git(seed, "add", "other.txt")
        git(seed, "commit", "-m", "existing review head")
        git(seed, "push", "origin", branch_ref)
        with self.assertRaisesRegex(PipelineError, "publish.head_moved"):
            self._run()
        self.assertEqual(self.fixture.effects.intents, {})
        self.assertEqual(self.fixture.pr.create_calls, 0)

    def test_force_with_lease_preserves_a_racing_remote_head(self) -> None:
        branch_ref = self.fixture.command.request["branch_ref"]
        seed = self.fixture.root / "seed"

        def race_then_push(self, candidate, *, branch_ref, expected_head_oid):
            git(seed, "checkout", "main")
            git(seed, "checkout", "-b", "racing")
            (seed / "race.txt").write_text("racing\n", encoding="utf-8")
            git(seed, "add", "race.txt")
            git(seed, "commit", "-m", "racing head")
            git(seed, "push", "origin", f"HEAD:{branch_ref}")
            original(self, candidate, branch_ref=branch_ref, expected_head_oid=expected_head_oid)

        original = PublicationRepository.push
        with (
            patch.object(PublicationRepository, "push", race_then_push),
            self.assertRaises(publish_stage.PublishIncomplete),
        ):
            self._run()
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
            self.assertRaisesRegex(PipelineError, "publish.blob_mismatch"),
        ):
            self._run()
        branch_ref = self.fixture.command.request["branch_ref"]
        branch = git(self.fixture.repo, "ls-remote", "origin", f"refs/heads/{branch_ref}")
        self.assertEqual(branch.stdout, "")

    def test_partial_push_remains_unknown_and_does_not_delete_or_rewrite(self) -> None:
        hook = self.fixture.remote / "hooks/pre-receive"
        hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        hook.chmod(0o755)
        with self.assertRaises(publish_stage.PublishIncomplete):
            self._run()
        branch_ref = self.fixture.command.request["branch_ref"]
        branch = git(self.fixture.repo, "ls-remote", "origin", f"refs/heads/{branch_ref}")
        self.assertEqual(branch.stdout, "")
        git_effect_id = self.fixture.effects.by_key[publish_stage.GIT_EFFECT_KEY]
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

        def push_then_lose(self, candidate, *, branch_ref, expected_head_oid):
            original(self, candidate, branch_ref=branch_ref, expected_head_oid=expected_head_oid)
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

    def test_unknown_effect_never_resends_git(self) -> None:
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
        with (
            patch.object(PublicationRepository, "push", side_effect=AssertionError("must not resend")),
            self.assertRaises(publish_stage.PublishIncomplete),
        ):
            self._run()

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


if __name__ == "__main__":
    unittest.main()
