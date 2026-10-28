"""Two-process PostgreSQL proof for Publish UNKNOWN Git reconciliation."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

try:
    import pgserver
    import psycopg2
except ImportError:
    pgserver = None
    psycopg2 = None

from description_pipeline.artifacts import ArtifactStore
from description_pipeline.io import digest, file_digest, inventory
from description_pipeline.pipeline.diagnostic import (
    DiagnosticAdmissionSpec,
    build_diagnostic_command,
)
from description_pipeline.pipeline.effects import EffectState
from description_pipeline.pipeline.stages import publish as publish_stage
from description_pipeline.platform.facts import FactConflict, FactStore
from description_pipeline.schemas import DIAGNOSTIC_ADMISSION_SCHEMA

from tests.v1.test_pipeline_publish import PublishFixture

PG_AVAILABLE = pgserver is not None and psycopg2 is not None
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PLATFORM_SCHEMA = REPOSITORY_ROOT / "deploy" / "platform" / "schema.sql"


@unittest.skipUnless(PG_AVAILABLE, "pgserver/psycopg2 are not installed")
class PublishRecoveryPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server_root = tempfile.TemporaryDirectory(prefix="publish-recovery-postgres-")
        cls.server = pgserver.get_server(
            Path(cls.server_root.name) / "pgdata",
            cleanup_mode="delete",
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.cleanup()
        cls.server_root.cleanup()

    def setUp(self) -> None:
        self.fixture = PublishFixture(diagnostics=[{
            "code": "input.cad_revision_absent",
            "blocking": False,
            "message": "the CAD revision label is absent; the raw value stays missing",
            "detail": {"hardware_id": "robot", "missing": ["revision"]},
        }])
        self.addCleanup(self.fixture.close)
        self.schema = "publish_recovery_" + uuid.uuid4().hex
        schema_sql = PLATFORM_SCHEMA.read_text(encoding="utf-8").replace(
            ':"platform_schema"',
            f'"{self.schema}"',
        )
        with psycopg2.connect(self.server.get_uri()) as connection, connection.cursor() as cursor:
            cursor.execute(f'CREATE SCHEMA "{self.schema}"')
            cursor.execute(schema_sql)
            cursor.execute(f'UPDATE "{self.schema}".admission SET state = %s WHERE id = 1', ("open",))
        self.artifacts = ArtifactStore(self.fixture.root / "recovery-cas")
        self.facts = FactStore(self._connect, self.artifacts)
        self.now = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
        self.observer_tool = digest({"tool": "publish-read-only-reconciler"})
        self.work_root = self.fixture.root / "recovery-work"
        self.pr_state = self.fixture.root / "pull-requests.json"

    def _connect(self):
        return psycopg2.connect(
            self.server.get_uri(),
            options=f"-c search_path={self.schema},public",
        )

    def _command(self, suffix: str):
        imported = tuple(
            self.artifacts.import_object(self.fixture.store, item)
            for item in self.fixture.command.input_artifacts
        )
        spec = DiagnosticAdmissionSpec(
            schema_version=DIAGNOSTIC_ADMISSION_SCHEMA,
            run_id=f"publish-recovery-{suffix}",
            request_id=f"publish-recovery-request-{suffix}",
            target_stage="publish",
            request=self.fixture.command.to_record()["request"],
            input_artifacts=imported,
            expected_executor_sha256=self.fixture.executor,
            config_snapshot_sha256=self.fixture.command.config_snapshot_sha256,
            contract_sha256=self.fixture.command.contract_sha256,
            policy_sha256=self.fixture.command.policy_sha256,
            created_at=self.now,
        )
        command = build_diagnostic_command(spec)
        self.facts.admit_diagnostic(spec, command)
        return command

    def _child_environment(self) -> dict[str, str]:
        environment = os.environ.copy()
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        environment["PYTHONPATH"] = os.pathsep.join(
            (
                str(REPOSITORY_ROOT / "src"),
                str(REPOSITORY_ROOT),
                environment.get("PYTHONPATH", ""),
            )
        )
        return environment

    def _run_child(self, *arguments: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-B", "-m", "tests.v1.publish_recovery_child", *arguments],
            cwd=REPOSITORY_ROOT,
            env=self._child_environment(),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    def _common_args(self, command) -> tuple[str, ...]:
        return (
            "--database-uri",
            self.server.get_uri(),
            "--schema",
            self.schema,
            "--artifact-root",
            str(self.artifacts.root),
            "--work-root",
            str(self.work_root),
            "--command-id",
            command.command_id,
            "--executor-sha256",
            self.fixture.executor,
            "--pr-state",
            str(self.pr_state),
        )

    def test_two_process_restart_recovers_original_outbound_without_resend(self) -> None:
        command = self._command("positive")
        old_owner = digest({"process": "old-publish-process"})
        new_owner = digest({"process": "new-publish-process"})
        common = self._common_args(command)
        sent = self._run_child(
            "--mode",
            "send",
            *common,
            "--owner-sha256",
            old_owner,
            "--now",
            self.now.isoformat(),
        )
        self.assertEqual(sent.returncode, 23, sent.stderr)
        branch_ref = command.request["branch_ref"]
        pushed = subprocess.run(
            ["git", "--git-dir", str(self.fixture.remote), "rev-parse", f"refs/heads/{branch_ref}"],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        ).stdout.strip()
        unknown = self.facts.effect_settlement_for_key(command.command_id, publish_stage.GIT_EFFECT_KEY)
        self.assertIs(unknown.state, EffectState.UNKNOWN)
        self.assertIsNotNone(unknown.outbound_artifact)
        self.artifacts.verify(unknown.outbound_artifact)
        outbound = self.artifacts.materialize(
            unknown.outbound_artifact,
            self.fixture.root / "original-outbound",
        )
        original_plan_sha256 = file_digest(Path(outbound) / publish_stage.PUBLICATION_PLAN)

        later = self.now + timedelta(minutes=10)
        with self.assertRaisesRegex(FactConflict, "unresolved"):
            self.facts.acquire_claim(
                command.command_id,
                claim_id="premature-recovery-claim",
                owner_sha256=new_owner,
                executor_sha256=self.fixture.executor,
                now=later,
                expires_at=later + timedelta(minutes=5),
            )
        resumed = self._run_child(
            "--mode",
            "resume",
            *common,
            "--owner-sha256",
            new_owner,
            "--now",
            later.isoformat(),
            "--observer-tool-sha256",
            self.observer_tool,
        )
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        settlement = self.facts.effect_settlement_for_key(command.command_id, publish_stage.GIT_EFFECT_KEY)
        self.assertIs(settlement.state, EffectState.SATISFIED)
        self.assertIsNotNone(settlement.payload_artifact)
        payload = self.artifacts.materialize(
            settlement.payload_artifact,
            self.fixture.root / "reconciled-payload",
        )
        self.assertEqual(
            set(inventory(payload, exclude=("manifest.json",))),
            {"observation.json", publish_stage.PUBLICATION_PLAN, "request.json"},
        )
        self.assertEqual(file_digest(Path(payload) / publish_stage.PUBLICATION_PLAN), original_plan_sha256)
        observation = json.loads((payload / "observation.json").read_text(encoding="utf-8"))
        self.assertEqual(observation["head_oid"], pushed)
        rows = json.loads(self.pr_state.read_text(encoding="utf-8"))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["head_oid"], pushed)
        original_plan = json.loads((outbound / publish_stage.PUBLICATION_PLAN).read_bytes())
        self.assertEqual(digest({"body": rows[0]["body"]}), original_plan["pr_body_sha256"])
        self.assertIn("`input.cad_revision_absent`", rows[0]["body"])
        self.assertEqual(rows[0]["body"].count("Verification warnings:"), 1)

    def test_two_process_absence_stays_unknown_and_never_creates_pr(self) -> None:
        command = self._command("absence")
        common = self._common_args(command)
        sent = self._run_child(
            "--mode",
            "send-absent",
            *common,
            "--owner-sha256",
            digest({"process": "old-absent-process"}),
            "--now",
            self.now.isoformat(),
        )
        self.assertEqual(sent.returncode, 23, sent.stderr)
        branch_ref = command.request["branch_ref"]
        self.assertEqual(
            subprocess.run(
                ["git", "--git-dir", str(self.fixture.remote), "for-each-ref", f"refs/heads/{branch_ref}"],
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
            ).stdout,
            "",
        )
        resumed = self._run_child(
            "--mode",
            "resume",
            *common,
            "--owner-sha256",
            digest({"process": "new-absent-process"}),
            "--now",
            (self.now + timedelta(minutes=10)).isoformat(),
            "--observer-tool-sha256",
            self.observer_tool,
        )
        self.assertEqual(resumed.returncode, 25, resumed.stderr)
        settlement = self.facts.effect_settlement_for_key(command.command_id, publish_stage.GIT_EFFECT_KEY)
        self.assertIs(settlement.state, EffectState.UNKNOWN)
        self.assertIsNone(
            self.facts.effect_settlement_for_key(
                command.command_id,
                publish_stage.PR_EFFECT_KEY,
            )
        )
        self.assertFalse(self.pr_state.exists())

    def test_two_process_restart_recovers_pr_effect_without_rewriting(self) -> None:
        command = self._command("pr-positive")
        common = self._common_args(command)
        sent = self._run_child(
            "--mode",
            "send-pr",
            *common,
            "--owner-sha256",
            digest({"process": "old-pr-publish-process"}),
            "--now",
            self.now.isoformat(),
        )
        self.assertEqual(sent.returncode, 23, sent.stderr)
        git_settlement = self.facts.effect_settlement_for_key(
            command.command_id,
            publish_stage.GIT_EFFECT_KEY,
        )
        unknown = self.facts.effect_settlement_for_key(
            command.command_id,
            publish_stage.PR_EFFECT_KEY,
        )
        self.assertIs(git_settlement.state, EffectState.SATISFIED)
        self.assertIs(unknown.state, EffectState.UNKNOWN)
        self.assertIsNotNone(unknown.outbound_artifact)
        self.artifacts.verify(unknown.outbound_artifact)
        outbound = self.artifacts.materialize(
            unknown.outbound_artifact,
            self.fixture.root / "original-pr-outbound",
        )
        original_request_sha256 = file_digest(Path(outbound) / "request.json")
        rows_before = json.loads(self.pr_state.read_text(encoding="utf-8"))
        self.assertEqual(len(rows_before), 1)

        later = self.now + timedelta(minutes=10)
        with self.assertRaisesRegex(FactConflict, "unresolved"):
            self.facts.acquire_claim(
                command.command_id,
                claim_id="premature-pr-recovery-claim",
                owner_sha256=digest({"process": "premature-pr-recovery"}),
                executor_sha256=self.fixture.executor,
                now=later,
                expires_at=later + timedelta(minutes=5),
            )
        resumed = self._run_child(
            "--mode",
            "resume-pr",
            *common,
            "--owner-sha256",
            digest({"process": "new-pr-publish-process"}),
            "--now",
            later.isoformat(),
            "--observer-tool-sha256",
            self.observer_tool,
        )
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        settlement = self.facts.effect_settlement_for_key(
            command.command_id,
            publish_stage.PR_EFFECT_KEY,
        )
        self.assertIs(settlement.state, EffectState.SATISFIED)
        self.assertIsNotNone(settlement.payload_artifact)
        payload = self.artifacts.materialize(
            settlement.payload_artifact,
            self.fixture.root / "reconciled-pr-payload",
        )
        self.assertEqual(
            set(inventory(payload, exclude=("manifest.json",))),
            {"observation.json", "request.json"},
        )
        self.assertEqual(file_digest(Path(payload) / "request.json"), original_request_sha256)
        observation = json.loads((payload / "observation.json").read_text(encoding="utf-8"))
        self.assertEqual(observation["action"], "reconciled")
        self.assertEqual(observation["pull_request"], rows_before[0])
        self.assertEqual(
            json.loads(self.pr_state.read_text(encoding="utf-8")),
            rows_before,
        )

    def test_two_process_pr_absence_stays_unknown_and_is_never_created(self) -> None:
        command = self._command("pr-absent")
        common = self._common_args(command)
        sent = self._run_child(
            "--mode",
            "send-pr-absent",
            *common,
            "--owner-sha256",
            digest({"process": "old-absent-pr-process"}),
            "--now",
            self.now.isoformat(),
        )
        self.assertEqual(sent.returncode, 23, sent.stderr)
        unknown = self.facts.effect_settlement_for_key(
            command.command_id,
            publish_stage.PR_EFFECT_KEY,
        )
        self.assertIs(unknown.state, EffectState.UNKNOWN)
        self.assertFalse(self.pr_state.exists())

        resumed = self._run_child(
            "--mode",
            "resume-pr",
            *common,
            "--owner-sha256",
            digest({"process": "new-absent-pr-process"}),
            "--now",
            (self.now + timedelta(minutes=10)).isoformat(),
            "--observer-tool-sha256",
            self.observer_tool,
        )
        self.assertEqual(resumed.returncode, 25, resumed.stderr)
        settlement = self.facts.effect_settlement_for_key(
            command.command_id,
            publish_stage.PR_EFFECT_KEY,
        )
        self.assertIs(settlement.state, EffectState.UNKNOWN)
        self.assertFalse(self.pr_state.exists())


if __name__ == "__main__":
    unittest.main()
