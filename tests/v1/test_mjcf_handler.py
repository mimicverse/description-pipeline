"""Real Generate/Verify artifacts consumed by the isolated MJCF stage."""

from __future__ import annotations

import unittest
import copy
import tempfile
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

try:
    import pgserver
    import psycopg2
except ImportError:
    pgserver = psycopg2 = None

from description_pipeline import mjcf_contract as contract
from description_pipeline.io import PipelineError, canonical, digest, file_digest, inventory, read_data
from description_pipeline.pipeline.contracts import StageCandidate, StageCommand, StageResult
from description_pipeline.pipeline.engine import StageWorkspace, execute_stage
from description_pipeline.pipeline.diagnostic import DiagnosticAdmissionSpec, build_diagnostic_command
from description_pipeline.pipeline.services import CanonicalStageServices, ClaimPolicy
from description_pipeline.platform.facts import FactStore
from description_pipeline.pipeline.stages.mjcf import MJCFHandler, validate_certificate, certificate_from_bytes
from description_pipeline.pipeline.verify import NoExternalEffects
from description_pipeline.schemas import STAGE_RESULT_SCHEMA, DIAGNOSTIC_ADMISSION_SCHEMA
from tests.v1 import test_pipeline_verify as pair_fixtures


class MJCFHandlerTests(unittest.TestCase):
    def setUp(self):
        self.pair = pair_fixtures.PipelineVerifyHandlerTests(methodName="runTest")
        self.pair.setUp()
        self.addCleanup(self.pair.doCleanups)
        source_command, execution, certificate, *_ = self.pair._run_verify()
        self.assertEqual("completed", execution.outcome)
        self.source_command = source_command
        self.store = self.pair.store
        self.verify_ref = self.store.seal_tree(
            Path(execution.candidate_root),
            kind="stage_output:verify",
            producer_stage="verify",
            producer_run_id=source_command.run_id,
            producer_tool_sha256=certificate["actual_verifier_sha256"],
        )
        self.roots = {
            "generate": self.pair.generate_root,
            "capture": self.pair.capture_root,
            "verify": self.store.materialize(self.verify_ref, self.pair.root / "verified"),
        }
        self.handler = MJCFHandler(self.store)

    def request(self, name="mjcf-first", simulation="{}\n"):
        source = self.source_command
        command = StageCommand.create(
            command_id=name,
            run_id=name,
            stage="mjcf",
            idempotency_key=name,
            request={
                "schema_version": contract.STAGE_REQUEST_SCHEMA,
                "output_contract": contract.OUTPUT_CONTRACT,
                "generate_artifact_id": self.pair.generate_ref.artifact_id,
                "capture_artifact_id": self.pair.capture_ref.artifact_id,
                "verify_artifact_id": self.verify_ref.artifact_id,
                "verify_certificate_sha256": file_digest(self.roots["verify"] / "verification-certificate.json"),
                "simulation": simulation,
            },
            input_artifacts=(self.pair.generate_ref, self.pair.capture_ref, self.verify_ref),
            config_snapshot_sha256=source.config_snapshot_sha256,
            contract_sha256=source.contract_sha256,
            policy_sha256=source.policy_sha256,
            expected_executor_sha256=contract.tool_identity()["source_sha256"],
            predecessor_decision_id="verified-source-decision",
        )
        claim = self.pair._claim(command, owner=digest({"process": name}), executor=command.expected_executor_sha256)
        workspace = StageWorkspace(
            root=self.pair.root / name,
            inputs={
                ref.artifact_id: self.roots[stage]
                for stage, ref in (
                    ("generate", self.pair.generate_ref),
                    ("capture", self.pair.capture_ref),
                    ("verify", self.verify_ref),
                )
            },
        )
        return command, claim, workspace

    def test_real_stage_preserves_upstream_and_seals_only_mjcf_and_certificate(self):
        before = {name: inventory(root) for name, root in self.roots.items()}
        command, claim, workspace = self.request()
        execution = self.handler.execute(command, claim, workspace, effects=NoExternalEffects())
        self.assertEqual("completed", execution.outcome)
        output = Path(execution.candidate_root)
        self.assertTrue(all(path.startswith("mjcf/") or path == contract.REPORT_PATH for path in inventory(output)))
        certificate = validate_certificate(output, command, source_roots=self.roots)
        self.assertEqual(execution.checks_sha256, digest(certificate))
        ref = self.store.seal_tree(
            output,
            kind=contract.ARTIFACT_KIND,
            producer_run_id=command.run_id,
            producer_stage="mjcf",
            producer_tool_sha256=command.expected_executor_sha256,
        )
        candidate = StageCandidate(
            result=StageResult(
                schema_version=STAGE_RESULT_SCHEMA,
                command_id=command.command_id,
                run_id=command.run_id,
                stage="mjcf",
                request_digest=command.request_digest,
                outcome="completed",
                output_artifact=ref,
                actual_executor_sha256=command.expected_executor_sha256,
                checks_sha256=execution.checks_sha256,
                resources_settled=True,
            )
        )
        self.assertEqual(
            candidate, self.handler.validate_candidate(command, claim, candidate, effects=NoExternalEffects())
        )
        self.assertEqual(before, {name: inventory(root) for name, root in self.roots.items()})
        second_command, second_claim, second_workspace = self.request("mjcf-second")
        second = self.handler.execute(second_command, second_claim, second_workspace, effects=NoExternalEffects())
        self.assertEqual(inventory(output / "mjcf"), inventory(Path(second.candidate_root) / "mjcf"))
        self.assertNotEqual(command.request_digest, second_command.request_digest)
        path = output / contract.ROBOT_NAME
        path.write_bytes(path.read_bytes() + b"\n")
        with self.assertRaisesRegex(PipelineError, "differs from its certificate"):
            validate_certificate(output, command, source_roots=self.roots)

    def test_engine_only_report_cannot_claim_passing_qualification(self):
        command, claim, workspace = self.request("mjcf-engine-only")
        execution = self.handler.execute(command, claim, workspace, effects=NoExternalEffects())
        self.assertEqual("completed", execution.outcome)
        certificate = read_data(Path(execution.candidate_root) / contract.REPORT_PATH)
        forged = copy.deepcopy(certificate)
        forged["report"]["checks"] = [{"id": "engine.process", "state": "passed", "passed": True, "details": {}}]
        forged["certificate_key"] = digest({key: value for key, value in forged.items() if key != "certificate_key"})
        for require_pass in (True, False):
            with self.subTest(require_pass=require_pass), self.assertRaisesRegex(PipelineError, "every required check"):
                certificate_from_bytes(canonical(forged), command, require_pass=require_pass)
        forged["report"]["passed"] = False
        forged["report"]["checks"][0].update(passed=False, state="failed")
        forged["certificate_key"] = digest({key: value for key, value in forged.items() if key != "certificate_key"})
        self.assertFalse(certificate_from_bytes(canonical(forged), command, require_pass=False)["report"]["passed"])

    def test_failed_collision_approximation_retains_candidate_and_independent_report(self):
        command, claim, workspace = self.request(
            simulation=('{"collision":{"source":"visual_convex","max_vertices":4,"max_error_m":1e-12}}')
        )
        execution = self.handler.execute(command, claim, workspace, effects=NoExternalEffects())
        self.assertEqual("failed", execution.outcome)
        self.assertEqual("mjcf.verification_failed", execution.failure.reason_code)
        output = Path(execution.candidate_root)
        report = read_data(output / contract.REPORT_PATH)["report"]
        self.assertFalse(report["passed"])
        self.assertTrue((output / contract.ROBOT_NAME).is_file())
        self.assertIn("collision.hulls", {row["id"] for row in report["checks"] if not row["passed"]})


@unittest.skipUnless(pgserver is not None and psycopg2 is not None, "PostgreSQL test dependencies are required")
class MJCFEnginePostgresTests(unittest.TestCase):
    def test_real_handler_success_failure_and_replay_use_one_engine(self):
        fixture = MJCFHandlerTests(methodName="runTest")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        with tempfile.TemporaryDirectory(prefix="mjcf-engine-postgres-") as directory:
            server = pgserver.get_server(Path(directory) / "database", cleanup_mode="delete")
            self.addCleanup(server.cleanup)
            schema = "mjcf_" + uuid.uuid4().hex
            sql = (Path(__file__).resolve().parents[2] / "deploy/platform/schema.sql").read_text()
            with psycopg2.connect(server.get_uri()) as connection, connection.cursor() as cursor:
                cursor.execute(f'CREATE SCHEMA "{schema}"')
                cursor.execute(sql.replace(':"platform_schema"', f'"{schema}"'))
                cursor.execute(f'UPDATE "{schema}".admission SET state = %s WHERE id = 1', ("open",))

            def connect():
                return psycopg2.connect(server.get_uri(), options=f"-c search_path={schema},public")

            facts = FactStore(connect, fixture.store)
            before = {name: inventory(root) for name, root in fixture.roots.items()}
            for name, simulation in (
                ("pass", "{}\n"),
                ("fail", '{"collision":{"source":"visual_convex","max_vertices":4,"max_error_m":1e-12}}'),
            ):
                base, _claim, _workspace = fixture.request("engine-" + name, simulation)
                now = datetime(2026, 11, 1, tzinfo=UTC)
                spec = DiagnosticAdmissionSpec(
                    schema_version=DIAGNOSTIC_ADMISSION_SCHEMA,
                    run_id=base.run_id,
                    request_id=base.run_id,
                    target_stage="mjcf",
                    request=base.to_record()["request"],
                    input_artifacts=base.input_artifacts,
                    expected_executor_sha256=base.expected_executor_sha256,
                    config_snapshot_sha256=base.config_snapshot_sha256,
                    contract_sha256=base.contract_sha256,
                    policy_sha256=base.policy_sha256,
                    created_at=now,
                )
                command = build_diagnostic_command(spec)
                facts.admit_diagnostic(spec, command)
                services = CanonicalStageServices(
                    facts=facts,
                    artifacts=fixture.store,
                    work_root=fixture.pair.root / ("runtime-" + name),
                    claim_policy=ClaimPolicy(
                        owner_sha256=digest({"worker": name}),
                        executor_sha256=base.expected_executor_sha256,
                        lease=timedelta(minutes=5),
                        claim_id_factory=lambda: "mjcf-claim-" + uuid.uuid4().hex,
                    ),
                    clock=lambda instant=now: instant,
                )
                result = execute_stage(command_id=command.command_id, handler=fixture.handler, services=services)
                self.assertEqual({"pass": "completed", "fail": "failed"}[name], result.outcome)
                repeated = execute_stage(command_id=command.command_id, handler=fixture.handler, services=services)
                self.assertEqual(result, repeated)
                with connect() as connection, connection.cursor() as cursor:
                    cursor.execute("SELECT count(*) FROM stage_decision WHERE run_id = %s", (command.run_id,))
                    self.assertEqual((1,), cursor.fetchone())
                    cursor.execute(
                        "SELECT count(*) FROM effect_intent JOIN command USING(command_id) WHERE command.run_id = %s",
                        (command.run_id,),
                    )
                    self.assertEqual((0,), cursor.fetchone())
                if name in {"pass", "fail"}:
                    root = fixture.store.materialize(result.output_artifact, fixture.pair.root / ("sealed-" + name))
                    report = read_data(root / contract.REPORT_PATH)["report"]
                    self.assertEqual(name == "pass", report["passed"])
            self.assertEqual(before, {name: inventory(root) for name, root in fixture.roots.items()})


if __name__ == "__main__":
    unittest.main()
