"""Real Git and process evidence for pre-dispatch rejection and uncertain Publish results."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from description_pipeline.io import file_digest, process_error_record, read_data
from description_pipeline.pipeline.contracts import StageDeferral, StageFailure
from description_pipeline.pipeline.contracts import FailureEnvelope
from description_pipeline.pipeline.engine import StageWorkspace, execute_stage
from description_pipeline.io import digest
from description_pipeline.pipeline.stages import publish as stage
from description_pipeline.pipeline.reconciliation import EffectReconciliationService
from description_pipeline.pipeline.effects import EffectState
from description_pipeline.platform.queries import read_run_projection
from description_pipeline.repository import publish as repository_module
from description_pipeline.repository.publish import (
    GITHUB_MAX_BLOB_BYTES,
    PublicationError,
    PublicationRepository,
)

from tests.v1.test_pipeline_publish import PublishFixture, git
from tests.v1.publish_recovery_child import _services
from tests.v1 import test_pipeline_publish_recovery_postgres as recovery_tests


class ProcessDiagnosticTests(unittest.TestCase):
    def test_structured_details_and_environment_credentials_are_redacted(self) -> None:
        error = PublicationError("publish.sensitive_detail")
        error.detail = {
            "authorization": "Bearer private-auth-value",
            "nested": [{"credential": "private-cli-value", "passwd": "private-password-value"}],
            "message": "private-env-credential",
            "reason": "remote rejection",
        }
        with patch.dict(os.environ, {"CLI_CREDENTIAL": "private-env-credential"}):
            record = process_error_record(error, operation="publish.git")
        self.assertNotIn("private-", json.dumps(record))
        self.assertEqual(record["detail"]["authorization"], "[REDACTED]")
        self.assertEqual(record["detail"]["nested"][0]["credential"], "[REDACTED]")
        self.assertEqual(record["detail"]["reason"], "remote rejection")

    def test_real_nonzero_process_output_and_cause_are_retained_without_secrets(self) -> None:
        secret = "private-process-secret-4930"
        with patch.dict(os.environ, {"GH_TOKEN": secret}):
            try:
                subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        "import sys; print(sys.argv[1]); "
                        "sys.stderr.write('remote: rejected\\nAuthorization: Bearer '+sys.argv[1]); sys.exit(7)",
                        secret,
                    ],
                    check=True,
                    capture_output=True,
                )
            except subprocess.CalledProcessError as original:
                error = PublicationError("publish.original_error")
                error.__cause__ = original
            record = process_error_record(error, operation="publish.git")
        serialized = json.dumps(record)
        self.assertNotIn(secret, serialized)
        self.assertEqual(record["causes"][0]["returncode"], 7)
        self.assertIn("remote: rejected", record["causes"][0]["stderr"]["text"])
        self.assertIn("[REDACTED]", record["causes"][0]["stdout"]["text"])

    def test_real_timeout_preserves_partial_output(self) -> None:
        with self.assertRaises(subprocess.TimeoutExpired) as raised:
            subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import sys,time; print('partial', flush=True); "
                    "sys.stderr.write('partial error\\n'); sys.stderr.flush(); time.sleep(2)",
                ],
                capture_output=True,
                timeout=0.2,
                check=True,
            )
        record = process_error_record(raised.exception, operation="publish.pr")
        self.assertTrue(record["timed_out"])
        self.assertIsNone(record["returncode"])
        self.assertIn("partial", record["stdout"]["text"])
        self.assertIn("partial error", record["stderr"]["text"])

    def test_output_limits_invalid_utf8_and_credential_forms_are_explicit(self) -> None:
        error = subprocess.CalledProcessError(
            1,
            ["gh", "--body", "never serialize argv"],
            output=b"\xff" + b"x" * 140000 + b" tail",
            stderr=b"https://user:password@example.test failure password=hidden github_pat_privatevalue",
        )
        record = process_error_record(error, operation="publish.pr")
        self.assertTrue(record["stdout"]["truncated"])
        self.assertEqual(record["stdout"]["original_bytes"], 140006)
        self.assertIn("tail", record["stdout"]["text"])
        self.assertNotIn("user:password", json.dumps(record))
        self.assertNotIn("hidden", json.dumps(record))
        self.assertNotIn("github_pat_privatevalue", json.dumps(record))
        self.assertNotIn("never serialize argv", json.dumps(record))


class ActualGitBlobTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="publish-blob-gate-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        git(None, "init", "-b", "main", str(self.root))
        git(self.root, "config", "user.name", "Blob Test")
        git(self.root, "config", "user.email", "blob@example.test")
        git(self.root, "remote", "add", "origin", "https://github.com/example/robot.git")
        (self.root / "README").write_text("base\n")
        self.base = self.commit()
        self.repository = PublicationRepository(self.root, expected_origin="https://github.com/example/robot.git")

    def commit(self) -> str:
        git(self.root, "add", "--all")
        git(self.root, "commit", "-m", "fixture")
        return git(self.root, "rev-parse", "HEAD").stdout.strip()

    def test_real_blob_boundary_and_deleted_candidate_history(self) -> None:
        path = self.root / "large evidence.bin"
        with path.open("wb") as stream:
            stream.truncate(GITHUB_MAX_BLOB_BYTES)
        exact = self.commit()
        self.repository.validate_candidate_objects(commit=exact, expected_base_oid=self.base)
        with path.open("ab") as stream:
            stream.write(b"x")
        self.commit()
        path.unlink()
        deleted = self.commit()
        with self.assertRaises(PublicationError) as raised:
            self.repository.validate_candidate_objects(commit=deleted, expected_base_oid=self.base)
        self.assertEqual(raised.exception.code, "publish.github_blob_too_large")
        objects = raised.exception.detail["objects"]
        self.assertEqual(len(objects), 1)
        self.assertEqual(objects[0]["bytes"], GITHUB_MAX_BLOB_BYTES + 1)
        self.assertEqual(objects[0]["paths"][0]["path"], "large evidence.bin")

    def test_base_and_unrelated_refs_are_excluded_from_the_upload_closure(self) -> None:
        with (self.root / "old-large.bin").open("wb") as stream:
            stream.truncate(GITHUB_MAX_BLOB_BYTES + 1)
        oversized_base = self.commit()
        git(self.root, "branch", "unrelated-large")
        (self.root / "README").write_text("candidate\n")
        candidate = self.commit()
        self.repository.validate_candidate_objects(commit=candidate, expected_base_oid=oversized_base)


class GitHubCommitAuthorityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = ActualGitBlobTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def observe(self, commit_status=422, *, repository_status=200, permissions=True, commit_payload=None):
        original = subprocess.run
        repository = self.fixture.repository

        def run(command, **kwargs):
            if command[0] != "gh":
                return original(command, **kwargs)
            is_commit = "/commits/" in command[-1]
            status = commit_status if is_commit else repository_status
            value = (
                (
                    commit_payload
                    or (
                        {"message": f"No commit found for SHA: {self.fixture.base}"}
                        if status == 422
                        else {"sha": self.fixture.base}
                    )
                )
                if is_commit
                else {"id": 42, "full_name": "example/robot", "permissions": {"pull": permissions}}
            )
            stdout = f"HTTP/2.0 {status} Result\r\nContent-Type: application/json\r\n\r\n{json.dumps(value)}".encode()
            return subprocess.CompletedProcess(command, 0 if status == 200 else 1, stdout=stdout, stderr=b"")

        with patch.object(repository_module.subprocess, "run", side_effect=run):
            return repository.observe_github_commit(repository_slug="example/robot", commit=self.fixture.base)

    def test_commit_absence_requires_accessible_exact_repository(self) -> None:
        self.assertEqual(self.observe()["state"], "absent")
        self.assertEqual(self.observe(200)["state"], "known")
        for arguments in (
            {"repository_status": 404},
            {"permissions": False},
            {"commit_status": 503},
            {"commit_status": 404},
        ):
            with self.subTest(arguments=arguments), self.assertRaises(PublicationError):
                self.observe(**arguments)

    def test_wrong_commit_response_is_not_an_absence_proof(self) -> None:
        with self.assertRaises(PublicationError):
            self.observe(200, commit_payload={"sha": "0" * 40})


class PublishFailureBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = PublishFixture()
        self.addCleanup(self.fixture.close)
        self.diagnostics = []
        original = self.fixture.effects.record_timeout

        def retain(effect_id, *, source, detail_root=None):
            artifact = self.fixture.store.seal_tree(
                detail_root,
                kind="effect_timeout",
                producer_run_id="publish-run",
                producer_stage="publish",
                producer_tool_sha256=self.fixture.executor,
            )
            self.diagnostics.append((effect_id, artifact))
            return original(effect_id, source=source, detail_root=detail_root)

        self.fixture.effects.record_timeout = retain

    def run_publish(self, workspace=None):
        return self.fixture.handler.execute(
            self.fixture.command,
            self.fixture.claim,
            workspace or self.fixture.workspace,
            effects=self.fixture.effects,
        )

    def diagnostic_records(self):
        records = []
        for index, (_effect_id, artifact) in enumerate(self.diagnostics):
            root = self.fixture.store.materialize(artifact, self.fixture.root / f"diagnostic-{index}")
            records.append(read_data(root / "timeout.json"))
        return records

    def test_size_rejection_precedes_bundle_effect_and_dispatch(self) -> None:
        with (
            patch.object(repository_module, "GITHUB_MAX_BLOB_BYTES", 1),
            patch.object(PublicationRepository, "push") as push,
            self.assertRaises(StageFailure) as raised,
        ):
            self.run_publish()
        self.assertEqual(raised.exception.reason_code, "publish.local_contract_failed")
        self.assertTrue(raised.exception.detail["objects"])
        self.assertEqual(self.fixture.effects.intents, {})
        self.assertEqual(self.fixture.effects.authorizations, 0)
        self.assertEqual(list(self.fixture.workspace.root.rglob("candidate.bundle")), [])
        push.assert_not_called()
        self.assertEqual(self.fixture.pr.create_calls, 0)

    def test_lost_git_response_keeps_original_diagnostic_after_exact_success(self) -> None:
        original_push = PublicationRepository.push

        def lose_response(repository, candidate, **kwargs):
            original_push(repository, candidate, **kwargs)
            raise subprocess.CalledProcessError(1, ["git", "push"], output=b"accepted", stderr=b"response lost")

        with patch.object(PublicationRepository, "push", lose_response):
            result = self.run_publish()
        self.assertEqual(result.outcome, "completed")
        record = self.diagnostic_records()[0]["operation_error"]
        self.assertEqual(record["returncode"], 1)
        self.assertEqual(record["stderr"]["text"], "response lost")

    def test_successful_send_failed_readback_stays_unknown_without_resend(self) -> None:
        error = subprocess.CalledProcessError(9, ["git", "ls-remote"], stderr=b"readback failed")
        original = PublicationRepository.push
        sends = []

        def send(repository, candidate, **kwargs):
            sends.append(candidate.commit)
            return original(repository, candidate, **kwargs)

        with (
            patch.object(PublicationRepository, "push", send),
            patch.object(stage, "_observe_git_exact", side_effect=error),
        ):
            result = self.run_publish()
            outbound = self.fixture.effects.outbound[result.effect_id]
            retry_workspace = StageWorkspace(
                root=self.fixture.root / "second-workspace", inputs=self.fixture.workspace.inputs
            )
            repeated = self.run_publish(retry_workspace)
        self.assertIsInstance(result, StageDeferral)
        self.assertEqual(repeated.effect_id, result.effect_id)
        self.assertEqual(len(sends), 1)
        self.assertEqual(self.fixture.effects.authorizations, 1)
        self.assertEqual(self.fixture.effects.outbound[result.effect_id], outbound)
        self.assertEqual(self.diagnostic_records()[0]["observation_error"]["returncode"], 9)
        self.assertEqual(self.fixture.pr.create_calls, 0)

    def test_lost_pr_response_keeps_diagnostics_and_does_not_create_twice(self) -> None:
        original = self.fixture.pr.create

        def create(**kwargs):
            original(**kwargs)
            raise subprocess.TimeoutExpired(["gh", "pr", "create"], 30, output=b"created", stderr=b"response lost")

        with patch.object(self.fixture.pr, "create", side_effect=create):
            result = self.run_publish()
        self.assertEqual(result.outcome, "completed")
        self.assertEqual(self.fixture.pr.create_calls, 1)
        self.assertTrue(self.diagnostic_records()[0]["operation_error"]["timed_out"])

    def test_successful_pr_write_failed_second_readback_defers_with_evidence(self) -> None:
        original = self.fixture.pr.observe

        def observe(**kwargs):
            if self.fixture.pr.create_calls:
                raise subprocess.CalledProcessError(3, ["gh", "pr", "list"], stderr=b"query unavailable")
            return original(**kwargs)

        with patch.object(self.fixture.pr, "observe", side_effect=observe):
            result = self.run_publish()
        self.assertIsInstance(result, StageDeferral)
        self.assertEqual(self.fixture.pr.create_calls, 1)
        self.assertEqual(self.diagnostic_records()[0]["observation_error"]["returncode"], 3)
        self.assertIs(self.fixture.effects.states[result.effect_id], EffectState.UNKNOWN)
        response = self.fixture.store.materialize(
            self.fixture.effects.payloads[result.effect_id], self.fixture.root / "retained-pr-response",
        )
        recorded = read_data(response / "observation.json")
        self.assertEqual(recorded["action"], "created")
        self.assertEqual(recorded["pull_request"], self.fixture.pr.rows[0].to_record())
        self.assertIsInstance(self.run_publish(), StageDeferral)
        self.assertEqual(self.fixture.pr.create_calls, 1)

    def reconciliation_inputs(self):
        hook = self.fixture.remote / "hooks/pre-receive"
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)
        result = self.run_publish()
        intent = self.fixture.effects.intents[result.effect_id]
        artifact = self.fixture.effects.outbound[result.effect_id]
        outbound = self.fixture.store.materialize(artifact, self.fixture.root / "reconciliation-outbound")
        return intent, outbound

    def test_recomputed_intent_tampering_is_rejected_before_any_remote_probe(self) -> None:
        intent, outbound = self.reconciliation_inputs()
        for field in ("command_id", "candidate_commit", "candidate_tree", "repository"):
            with self.subTest(field=field):
                expected = dict(intent.expected_state)
                target = dict(intent.target)
                command_id = intent.command_id
                if field == "command_id":
                    command_id = "other-command"
                elif field == "repository":
                    expected[field] = target[field] = "other/repository"
                else:
                    expected[field] = "f" * 40
                forged = replace(
                    intent, command_id=command_id, expected_state=expected,
                    expected_state_digest=digest(expected), target=target,
                )
                with (
                    patch.object(stage, "clone_publication_repository", side_effect=AssertionError("unexpected probe")),
                    self.assertRaisesRegex(PublicationError, "publish.git_recovery_intent"),
                ):
                    stage.PublishGitReconciliationAdapter().observe(
                        command=self.fixture.command, intent=forged, outbound_root=outbound,
                        workspace_root=self.fixture.root / f"forged-intent-{field}",
                    )

    def test_only_proven_oversize_and_authoritative_commit_absence_can_settle_absent(self) -> None:
        intent, outbound = self.reconciliation_inputs()
        bundle_sha = file_digest(outbound / "candidate.bundle")
        adapter = stage.PublishGitReconciliationAdapter()
        ordinary = adapter.observe(
            command=self.fixture.command,
            intent=intent,
            outbound_root=outbound,
            workspace_root=self.fixture.root / "ordinary-observer",
        )
        self.assertEqual((ordinary.fact_type, ordinary.state_class), ("observation", "absent"))
        for index, authority in enumerate(("known", "absent")):
            candidate = intent.expected_state["candidate_commit"]
            commit_observation = {
                "state": authority,
                "candidate_commit": candidate,
                "repository": {"full_name": "example/robot", "id": 42, "pull": True},
                "resource": f"repos/example/robot/commits/{candidate}",
                "http_status": 422 if authority == "absent" else 200,
                "response": {"message": f"No commit found for SHA: {candidate}"}
                if authority == "absent"
                else {"sha": candidate},
            }
            with (
                patch.object(repository_module, "GITHUB_MAX_BLOB_BYTES", 1),
                patch.object(PublicationRepository, "observe_github_commit", return_value=commit_observation),
            ):
                observed = adapter.observe(
                    command=self.fixture.command,
                    intent=intent,
                    outbound_root=outbound,
                    workspace_root=self.fixture.root / f"observer-{index}",
                )
            self.assertEqual(observed.state_class, "absent" if authority == "absent" else "unobservable")
            self.assertEqual(observed.fact_type, "reconciled" if authority == "absent" else "observation")
        self.assertEqual(file_digest(outbound / "candidate.bundle"), bundle_sha)

    def test_oversize_probe_failure_never_claims_terminal_absence(self) -> None:
        intent, outbound = self.reconciliation_inputs()
        with (
            patch.object(repository_module, "GITHUB_MAX_BLOB_BYTES", 1),
            patch.object(
                PublicationRepository, "observe_github_commit", side_effect=subprocess.TimeoutExpired(["gh"], 30)
            ),
            self.assertRaises(subprocess.TimeoutExpired),
        ):
            stage.PublishGitReconciliationAdapter().observe(
                command=self.fixture.command,
                intent=intent,
                outbound_root=outbound,
                workspace_root=self.fixture.root / "failed-observer",
            )
        self.assertEqual(self.fixture.effects.authorizations, 1)

    def test_changed_remote_ref_prevents_oversize_absence_settlement(self) -> None:
        intent, outbound = self.reconciliation_inputs()
        adapter = stage.PublishGitReconciliationAdapter()
        with (
            patch.object(repository_module, "GITHUB_MAX_BLOB_BYTES", 1),
            patch.object(PublicationRepository, "observe", return_value="f" * 40),
            patch.object(PublicationRepository, "observe_github_commit") as authority,
        ):
            observed = adapter.observe(
                command=self.fixture.command,
                intent=intent,
                outbound_root=outbound,
                workspace_root=self.fixture.root / "changed-ref-observer",
            )
        self.assertEqual((observed.fact_type, observed.state_class), ("observation", "unobservable"))
        authority.assert_not_called()

    def test_base_drift_prevents_oversize_absence_settlement(self) -> None:
        intent, outbound = self.reconciliation_inputs()
        with (
            patch.object(repository_module, "GITHUB_MAX_BLOB_BYTES", 1),
            patch.object(PublicationRepository, "fetch_expected", side_effect=PublicationError("publish.base_moved")),
            patch.object(PublicationRepository, "observe_github_commit") as authority,
            self.assertRaises(PublicationError),
        ):
            stage.PublishGitReconciliationAdapter().observe(
                command=self.fixture.command,
                intent=intent,
                outbound_root=outbound,
                workspace_root=self.fixture.root / "changed-base-observer",
            )
        authority.assert_not_called()


class PublishFailurePostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        recovery_tests.PublishRecoveryPostgresTests.setUpClass()

    @classmethod
    def tearDownClass(cls) -> None:
        recovery_tests.PublishRecoveryPostgresTests.tearDownClass()

    def setUp(self) -> None:
        self.harness = recovery_tests.PublishRecoveryPostgresTests(
            "test_two_process_absence_stays_unknown_and_never_creates_pr"
        )
        self.harness.setUp()
        self.addCleanup(self.harness.doCleanups)

    def test_actual_local_gate_commits_failed_decision_without_dispatch(self) -> None:
        harness = self.harness
        command = harness._command("oversized-before-send")
        services = _services(
            facts=harness.facts,
            artifacts=harness.artifacts,
            work_root=harness.work_root,
            owner_sha256=digest({"process": "size-gate"}),
            executor_sha256=harness.fixture.executor,
            now=harness.now,
        )
        with (
            patch.object(repository_module, "GITHUB_MAX_BLOB_BYTES", 1),
            patch.object(PublicationRepository, "push") as push,
        ):
            outcome = execute_stage(
                command_id=command.command_id,
                handler=stage.PublishHandler(harness.artifacts, pull_requests=harness.fixture.pr),
                services=services,
            )
        self.assertIsInstance(outcome, FailureEnvelope)
        self.assertEqual(outcome.outcome, "failed")
        self.assertEqual(outcome.reason_code, "publish.local_contract_failed")
        self.assertTrue(outcome.error_detail["objects"])
        push.assert_not_called()
        with harness._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT outcome FROM stage_decision WHERE command_id=%s", (command.command_id,))
            self.assertEqual(cursor.fetchall(), [("failed",)])
            cursor.execute(
                "SELECT closure_code, closed_at IS NOT NULL FROM execution_claim WHERE command_id=%s",
                (command.command_id,),
            )
            self.assertEqual(cursor.fetchall(), [("definite_failure", True)])
            cursor.execute("SELECT count(*) FROM effect_intent WHERE command_id=%s", (command.command_id,))
            self.assertEqual(cursor.fetchone(), (0,))

    def test_real_local_process_failures_close_the_claim_and_project_failed_without_dispatch(self) -> None:
        harness = self.harness
        offline_remote = harness.fixture.remote.with_name("offline.git")
        empty_path = harness.fixture.root / "empty-path"
        empty_path.mkdir()
        seal_outbound = stage._seal_effect_outbound

        def collide_outbound(root, payload, **kwargs):
            root.parent.mkdir(parents=True, exist_ok=True)
            root.write_text("occupied\n")
            return seal_outbound(root, payload, **kwargs)

        for index, operation in enumerate(("publish.snapshot", "publish.git_prepare", "publish.outbound_prepare")):
            with self.subTest(operation=operation), ExitStack() as stack:
                command = harness._command(f"local-process-{index}")
                services = _services(
                    facts=harness.facts,
                    artifacts=harness.artifacts,
                    work_root=harness.work_root / str(index),
                    owner_sha256=digest({"process": f"local-process-{index}"}),
                    executor_sha256=harness.fixture.executor,
                    now=harness.now,
                )
                if operation == "publish.snapshot":
                    stack.enter_context(patch.dict(os.environ, {"PATH": str(empty_path)}))
                elif operation == "publish.git_prepare":
                    harness.fixture.remote.rename(offline_remote)
                    stack.callback(offline_remote.rename, harness.fixture.remote)
                else:
                    stack.enter_context(patch.object(stage, "_seal_effect_outbound", side_effect=collide_outbound))
                push = stack.enter_context(patch.object(PublicationRepository, "push"))
                outcome = execute_stage(
                    command_id=command.command_id,
                    handler=stage.PublishHandler(harness.artifacts, pull_requests=harness.fixture.pr),
                    services=services,
                )
                self.assertIsInstance(outcome, FailureEnvelope)
                self.assertEqual(outcome.reason_code, "publish.local_process_failed")
                self.assertEqual(outcome.error_detail["exception"]["operation"], operation)
                if operation == "publish.git_prepare":
                    self.assertEqual(outcome.error_detail["exception"]["returncode"], 128)
                    self.assertIn("stderr", outcome.error_detail["exception"])
                push.assert_not_called()
                self.assertEqual(harness.fixture.pr.create_calls, 0)
                with harness._connect() as connection, connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT closure_code, closed_at IS NOT NULL FROM execution_claim WHERE command_id=%s",
                        (command.command_id,),
                    )
                    self.assertEqual(cursor.fetchall(), [("definite_failure", True)])
                    cursor.execute("SELECT count(*) FROM effect_intent WHERE command_id=%s", (command.command_id,))
                    self.assertEqual(cursor.fetchone(), (0,))
                    projection = read_run_projection(connection, command.run_id, artifacts=harness.artifacts)
                self.assertEqual(projection["state"], "failed")
                self.assertEqual(projection["stages"][-1]["state"], "failed")

    def test_failed_pr_read_before_dispatch_preserves_the_satisfied_git_effect(self) -> None:
        harness = self.harness
        command = harness._command("pr-read-before-write")
        services = _services(
            facts=harness.facts, artifacts=harness.artifacts, work_root=harness.work_root,
            owner_sha256=digest({"process": "pr-read-before-write"}),
            executor_sha256=harness.fixture.executor, now=harness.now,
        )

        def fail_read(**kwargs):
            return subprocess.run(
                [sys.executable, "-c", "import sys; sys.stderr.write('query unavailable'); sys.exit(3)"],
                check=True, capture_output=True,
            )

        with patch.object(harness.fixture.pr, "observe", side_effect=fail_read):
            outcome = execute_stage(
                command_id=command.command_id,
                handler=stage.PublishHandler(harness.artifacts, pull_requests=harness.fixture.pr),
                services=services,
            )
        self.assertIsInstance(outcome, FailureEnvelope)
        self.assertEqual(outcome.error_detail["exception"]["operation"], "publish.pr_observe")
        self.assertEqual(outcome.error_detail["exception"]["returncode"], 3)
        self.assertIs(harness.facts.effect_settlement_for_key(command.command_id, stage.GIT_EFFECT_KEY).state,
                      EffectState.SATISFIED)
        self.assertIsNone(harness.facts.effect_settlement_for_key(command.command_id, stage.PR_EFFECT_KEY))
        self.assertEqual(harness.fixture.pr.create_calls, 0)
        self.assertEqual(harness.facts.decision_for(command).outcome, "failed")

    def test_failed_final_read_preserves_both_satisfied_effects_and_commits_failure(self) -> None:
        harness = self.harness
        command = harness._command("final-read-after-writes")
        services = _services(
            facts=harness.facts, artifacts=harness.artifacts, work_root=harness.work_root,
            owner_sha256=digest({"process": "final-read-after-writes"}),
            executor_sha256=harness.fixture.executor, now=harness.now,
        )
        observe = PublicationRepository.observe

        def fail_final_read(repository, ref):
            if harness.fixture.pr.create_calls:
                subprocess.run(
                    [sys.executable, "-c", "import sys; sys.stderr.write('read timed out'); sys.exit(9)"],
                    check=True, capture_output=True,
                )
            return observe(repository, ref)

        with patch.object(PublicationRepository, "observe", new=fail_final_read):
            outcome = execute_stage(
                command_id=command.command_id,
                handler=stage.PublishHandler(harness.artifacts, pull_requests=harness.fixture.pr),
                services=services,
            )
        self.assertIsInstance(outcome, FailureEnvelope)
        self.assertEqual(outcome.error_detail["exception"]["operation"], "publish.final_readback")
        self.assertEqual(outcome.error_detail["exception"]["returncode"], 9)
        for key in (stage.GIT_EFFECT_KEY, stage.PR_EFFECT_KEY):
            self.assertIs(harness.facts.effect_settlement_for_key(command.command_id, key).state,
                          EffectState.SATISFIED)
        self.assertEqual(harness.fixture.pr.create_calls, 1)
        self.assertEqual(harness.facts.decision_for(command).outcome, "failed")

    def test_constraint_reconciliation_is_bound_and_closes_failed_original_stage_without_resend(self) -> None:
        harness = self.harness
        command = harness._command("constraint-absence")
        sent = harness._run_child(
            "--mode",
            "send-absent",
            *harness._common_args(command),
            "--owner-sha256",
            digest({"process": "first-sender"}),
            "--now",
            harness.now.isoformat(),
        )
        self.assertEqual(sent.returncode, 23, sent.stderr)
        before = harness.facts.effect_settlement_for_key(command.command_id, stage.GIT_EFFECT_KEY)
        self.assertIs(before.state, EffectState.UNKNOWN)
        outbound_id = before.outbound_artifact.artifact_id
        outbound_sha = before.outbound_artifact.set_sha256
        later = harness.now + timedelta(minutes=10)
        reconciler = EffectReconciliationService(
            facts=harness.facts,
            artifacts=harness.artifacts,
            work_root=harness.work_root / "reconcile",
            observer_tool_sha256=harness.observer_tool,
            clock=lambda: later,
        )
        candidate = harness.facts.effect_intent_for_key(command.command_id, stage.GIT_EFFECT_KEY).expected_state[
            "candidate_commit"
        ]
        authority = {
            "state": "absent",
            "candidate_commit": candidate,
            "repository": {"full_name": "example/robot", "id": 42, "pull": True},
            "resource": f"repos/example/robot/commits/{candidate}",
            "http_status": 422,
            "response": {"message": f"No commit found for SHA: {candidate}"},
        }
        with (
            patch.object(repository_module, "GITHUB_MAX_BLOB_BYTES", 1),
            patch.object(PublicationRepository, "observe_github_commit", return_value=authority),
            patch.object(PublicationRepository, "push") as push,
        ):
            result = reconciler.reconcile(
                command_id=command.command_id,
                effect_key=stage.GIT_EFFECT_KEY,
                adapter=stage.PublishGitReconciliationAdapter(),
            )
            self.assertIs(result.settlement.state, EffectState.DEFINITELY_FAILED)
            self.assertEqual(result.fact.fact_type, "reconciled")
            after = harness.facts.effect_settlement_for_key(command.command_id, stage.GIT_EFFECT_KEY)
            self.assertEqual(
                (after.outbound_artifact.artifact_id, after.outbound_artifact.set_sha256), (outbound_id, outbound_sha)
            )
            payload = harness.artifacts.materialize(after.payload_artifact, harness.fixture.root / "constraint-payload")
            self.assertTrue(read_data(payload / "github-blob-constraint.json")["objects"])
            self.assertEqual(
                read_data(payload / "observation.json")["absence_basis"],
                "github_unacceptable_new_git_blobs_and_commit_absent",
            )
            services = _services(
                facts=harness.facts,
                artifacts=harness.artifacts,
                work_root=harness.work_root / "resume",
                owner_sha256=digest({"process": "later-executor"}),
                executor_sha256=harness.fixture.executor,
                now=later,
            )
            outcome = execute_stage(
                command_id=command.command_id,
                handler=stage.PublishHandler(harness.artifacts, pull_requests=harness.fixture.pr),
                services=services,
            )
        push.assert_not_called()
        self.assertIsInstance(outcome, FailureEnvelope)
        self.assertEqual(outcome.outcome, "failed")
        with harness._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM effect_fact WHERE effect_id=%s AND fact_type='dispatch_authorized'",
                (before.effect_id,),
            )
            self.assertEqual(cursor.fetchone(), (1,))

    def test_known_preparation_domain_errors_share_the_failure_commit_boundary(self) -> None:
        harness = self.harness
        boundaries = (
            (PublicationRepository, "candidate", "publish.head_moved"),
            (stage, "materialize_repository_snapshot", "publish.snapshot_binding"),
            (stage.PublishHandler, "_prepare", "publish.subject"),
        )
        for index, (owner, name, code) in enumerate(boundaries):
            with self.subTest(boundary=name):
                command = harness._command(f"known-domain-{index}")
                services = _services(
                    facts=harness.facts,
                    artifacts=harness.artifacts,
                    work_root=harness.work_root / str(index),
                    owner_sha256=digest({"process": f"domain-{index}"}),
                    executor_sha256=harness.fixture.executor,
                    now=harness.now,
                )
                with patch.object(owner, name, side_effect=PublicationError(code, {"reason": "fixture"})):
                    outcome = execute_stage(
                        command_id=command.command_id,
                        handler=stage.PublishHandler(harness.artifacts, pull_requests=harness.fixture.pr),
                        services=services,
                    )
                self.assertIsInstance(outcome, FailureEnvelope)
                self.assertEqual(outcome.reason_code, "publish.local_contract_failed")
                self.assertEqual(outcome.error_detail["exception"]["code"], code)
                with harness._connect() as connection, connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT closure_code, closed_at IS NOT NULL FROM execution_claim WHERE command_id=%s",
                        (command.command_id,),
                    )
                    self.assertEqual(cursor.fetchall(), [("definite_failure", True)])
                    cursor.execute("SELECT count(*) FROM effect_intent WHERE command_id=%s", (command.command_id,))
                    self.assertEqual(cursor.fetchone(), (0,))


if __name__ == "__main__":
    unittest.main()
