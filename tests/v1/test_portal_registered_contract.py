"""Registered read errors and complete certificate gates on the portal boundary."""

from __future__ import annotations

import copy
import hashlib
import io
import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from description_pipeline.artifacts import ArtifactStore
from description_pipeline.io import PipelineError, digest, write_json
from description_pipeline.orchestration.airflow_client import native_run_id
from description_pipeline.orchestration.portal import PortalApp, PortalConfig, PortalError
from description_pipeline.pipeline.contracts import RunAdmission, StageCommand, StageResult
from description_pipeline.schemas import STAGE_RESULT_SCHEMA

from .portal_read_support import (
    RegisteredProjectionArtifacts, registered_certificate_fixture, registered_command_fixture,
)


class RegisteredReadErrorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="portal-registered-read-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = ArtifactStore(self.root / "cas")
        self.dag_run_id = "portal-read-contract"
        self.run_id = native_run_id(self.dag_run_id)
        now = datetime(2026, 10, 29, tzinfo=UTC)
        command = StageCommand.create(
            command_id="freeze-command",
            run_id=self.run_id,
            stage="freeze",
            idempotency_key="freeze-key",
            request={"run_id": self.run_id},
            input_artifacts=(),
            config_snapshot_sha256="c" * 64,
            contract_sha256="d" * 64,
            policy_sha256="e" * 64,
            expected_executor_sha256="f" * 64,
            predecessor_decision_id=None,
        )
        admission = RunAdmission.create(
            run_id=self.run_id,
            request_id=self.dag_run_id,
            parent_run_id=None,
            from_stage="freeze",
            request={"purpose": "engineering", "principal": "app:tenant:open"},
            created_at=now,
        )
        candidate = self.root / "candidate"
        candidate.mkdir()
        checks = {
            "command_id": command.command_id,
            "request_digest": command.request_digest,
            "checks": [{"id": "handoff.integrity", "state": "passed", "details": {}}],
        }
        write_json(candidate / "checks.json", checks)
        output = self.store.seal_tree(
            candidate,
            kind="stage_output:freeze",
            producer_run_id=self.run_id,
            producer_stage="freeze",
            producer_tool_sha256=command.expected_executor_sha256,
        )
        self.output = output
        result = StageResult(
            schema_version=STAGE_RESULT_SCHEMA,
            command_id=command.command_id,
            run_id=self.run_id,
            stage="freeze",
            request_digest=command.request_digest,
            outcome="completed",
            output_artifact=output,
            actual_executor_sha256=command.expected_executor_sha256,
            checks_sha256=digest(checks),
            resources_settled=True,
        )
        result_ref = self.store.seal_stage_result(result)
        self.rows = {
            "FROM run_admission": [
                (self.dag_run_id, None, "freeze", dict(admission.request), admission.request_digest, now)
            ],
            "FROM command\n": [
                (
                    command.command_id,
                    self.run_id,
                    "stage",
                    "freeze",
                    dict(command.request),
                    command.request_digest,
                    command.idempotency_key,
                    command.config_snapshot_sha256,
                    command.contract_sha256,
                    command.policy_sha256,
                    command.expected_executor_sha256,
                    None,
                )
            ],
            "FROM stage_decision": [
                (
                    "freeze-decision",
                    "freeze",
                    command.command_id,
                    "completed",
                    result_ref.artifact_id,
                    result.digest,
                    command.expected_executor_sha256,
                    None,
                    None,
                    now,
                )
            ],
            "FROM artifact\n": [
                tuple(
                    getattr(result_ref, field)
                    for field in (
                        "artifact_id",
                        "kind",
                        "locator",
                        "manifest_sha256",
                        "set_sha256",
                        "producer_run_id",
                        "producer_stage",
                        "producer_tool_sha256",
                    )
                )
            ],
        }
        outer = self

        class Cursor:
            def execute(self, statement, parameters):
                del parameters
                self.result = next((rows for clause, rows in outer.rows.items() if clause in statement), [])

            def fetchone(self):
                return self.result[0] if self.result else None

            def fetchall(self):
                return list(self.result)

            def close(self):
                pass

        self.enterContext(patch.dict("os.environ", {"PLATFORM_SERVICE_DSN": "local-read-fixture"}))
        self.enterContext(patch("psycopg2.connect", return_value=SimpleNamespace(cursor=Cursor, close=lambda: None)))
        airflow = SimpleNamespace(
            dag_run=lambda *_: {"dag_run_id": self.dag_run_id, "state": "running"},
            task_instances=lambda *_: [],
            profile=lambda *_: {
                "principal": "app:tenant:open",
                "name": "Reader",
                "app_id": "app",
                "tenant_key": "tenant",
                "open_id": "open",
                "role": "ADMIN",
            },
        )
        self.app = PortalApp(
            PortalConfig(
                airflow=airflow,
                artifact_store=self.store,
                run_control_store=SimpleNamespace(get_run_display_metadata=lambda *_: None),
            )
        )
        self.session = self.app.sessions.create("Reader", "token", principal="app:tenant:open")

    def request(self, suffix: str) -> tuple[int, dict]:
        statuses = []
        body = b"".join(
            self.app(
                {
                    "REQUEST_METHOD": "GET",
                    "PATH_INFO": f"/api/runs/{self.dag_run_id}{suffix}",
                    "HTTP_COOKIE": f"solidworks_portal_session={self.session.session_id}",
                    "wsgi.input": io.BytesIO(b""),
                },
                lambda status, _headers: statuses.append(int(status.split()[0])),
            )
        )
        return statuses[0], json.loads(body)

    def test_cas_corruption_is_not_a_missing_run_or_unstarted_stage(self) -> None:
        status, before = self.request("")
        self.assertEqual(status, 200)
        self.assertEqual(before["report"]["overall"]["stages_completed"], 1)
        (self.store.objects / self.output.artifact_id / "checks.json").write_bytes(b'{"changed":true}')
        for suffix in ("", "/preview", "/artifacts/checks.json"):
            with self.subTest(suffix=suffix):
                status, body = self.request(suffix)
                self.assertEqual(status, 502)
                self.assertIn("artifact bytes differ", body["error"])
                self.assertNotIn("report", body)

    def test_missing_admission_still_has_no_projection(self) -> None:
        self.rows["FROM run_admission"] = []
        self.assertIsNone(self.app._read_fact_projection(self.run_id))
        self.assertEqual(self.request("/artifacts/checks.json")[0], 404)


class RegisteredCertificateContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.artifacts = RegisteredProjectionArtifacts()
        self.app = PortalApp(PortalConfig(airflow=SimpleNamespace(), artifact_store=self.artifacts))
        self.capture_files = {"input/handoff.json": b"{}"}
        self.capture = self.artifacts.register(run_id="run", stage="capture", files=self.capture_files)
        self.generate_files = {
            "tool.json": b"{}",
            "urdf/robot.urdf": b'<robot name="fixture"/>',
            "reports/generate.json": json.dumps(
                {
                    "capture_artifact": self.capture,
                    "config_snapshot_sha256": "e" * 64,
                    "contract_sha256": "f" * 64,
                    "actual_executor_sha256": digest({"tool": "generate"}),
                    "required_executor_sha256": digest({"tool": "generate"}),
                }
            ).encode(),
        }
        self.generate = self.artifacts.register(run_id="run", stage="generate", files=self.generate_files)
        self.certificate = registered_certificate_fixture(
            generate=self.generate,
            capture=self.capture,
            generate_files=self.generate_files,
            capture_files=self.capture_files,
            report={"passed": True},
        )

    @staticmethod
    def members(ref: dict, files: dict[str, bytes]) -> dict:
        return {
            name: {"artifact": ref, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
            for name, data in files.items()
        }

    def projection(self, certificate: dict, *, contract="f" * 64, policy="1" * 64) -> dict:
        data = json.dumps(certificate).encode()
        files = {"verification-certificate.json": data}
        verify = self.artifacts.register(run_id="run", stage="verify", files=files)
        return {
            "run_id": "run",
            "state": "running",
            "purpose": "engineering",
            "stages": {
                "generate": {
                    "command": registered_command_fixture(
                        "run", "generate", request={"capture_artifact_id": self.capture["artifact_id"]},
                        inputs=[self.capture],
                    ),
                },
                "verify": {
                    "state": "completed",
                    "receipt": {"checks_sha256": digest(certificate)},
                    "command": registered_command_fixture(
                        "run", "verify", request={"purpose": "engineering"}, inputs=[self.generate, self.capture],
                        contract=contract, policy=policy,
                    ),
                    "formal_eligible": True,
                    "qualification": {"formal_eligible": True, "scope_kind": "formal", "outcome": "completed"},
                }
            },
            "by_stage": {
                "generate": self.members(self.generate, self.generate_files),
                "capture": self.members(self.capture, self.capture_files),
                "verify": self.members(verify, files),
            },
        }

    def test_complete_certificate_remains_previewable(self) -> None:
        preview = self.app._preview_from_projection("run", self.projection(self.certificate))
        self.assertTrue(preview["verified"])

    def test_new_verifier_contract_keeps_the_original_generate_receipt(self) -> None:
        contract, policy = digest({"new_verifier_contract": True}), digest({"new_verifier_policy": True})
        certificate = registered_certificate_fixture(
            generate=self.generate, capture=self.capture,
            generate_files=self.generate_files, capture_files=self.capture_files,
            report={"passed": True}, contract_sha256=contract, policy_sha256=policy,
        )
        projection = self.projection(certificate, contract=contract, policy=policy)
        preview = self.app._preview_from_projection("run", projection)
        self.assertTrue(preview["verified"])
        self.assertEqual(projection["stages"]["generate"]["command"]["contract_sha256"], "f" * 64)
        projection["stages"]["generate"]["command"] = registered_command_fixture(
            "run", "generate", request={"capture_artifact_id": self.capture["artifact_id"]},
            inputs=[self.capture], contract=contract,
        )
        with self.assertRaisesRegex(PipelineError, "Generate receipt differs"):
            self.app._preview_from_projection("run", projection)

    def test_registered_bad_schema_proof_and_failed_check_are_refused(self) -> None:
        for mutation, message in (
            ("schema", "unknown schema"),
            ("proof", "proof digest"),
            ("check", "non-passing check"),
        ):
            with self.subTest(mutation=mutation):
                certificate = copy.deepcopy(self.certificate)
                if mutation == "schema":
                    certificate["schema_version"] = "unknown/v999"
                elif mutation == "proof":
                    certificate["verification_key"] = "0" * 64
                else:
                    certificate["checks"][0].update(state="failed", passed=False)
                    certificate["verification_key"] = digest(
                        {key: value for key, value in certificate.items() if key != "verification_key"}
                    )
                projection = self.projection(certificate)
                for reader in (
                    lambda projection=projection: self.app._preview_from_projection("run", projection),
                    lambda projection=projection: self.app._fact_display_job(projection),
                ):
                    with self.assertRaisesRegex(PortalError, message) as caught:
                        reader()
                    self.assertEqual(caught.exception.status, 502)


if __name__ == "__main__":
    unittest.main()
