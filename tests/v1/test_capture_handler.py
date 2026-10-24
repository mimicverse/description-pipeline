"""Capture domain handler: producer/executor split and native admission boundaries."""

from __future__ import annotations

import json
import hashlib
import shutil
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock, patch

from description_pipeline.io import digest
from description_pipeline.pipeline import (
    ArtifactRef,
    CaptureHandler,
    ExecutionClaim,
    StageCommand,
    StageWorkspace,
)
from description_pipeline.pipeline.capture import CaptureUnresolved, NativeCaptureResult
from description_pipeline.orchestration.stage_transfer import admit_capture
from description_pipeline.sources.solidworks.discovery import PLAN_FILE

from .split_capture_fixture import build_fixture_capture, fixture_backend


PARENT = "a0000000-0000-4000-8000-000000000001"
CHILD = "a0000000-0000-4000-8000-000000000002"
MAIN = "cad/robot.SLDASM"


def artifact(identifier: str, stage: str, run_id: str) -> ArtifactRef:
    return ArtifactRef(
        artifact_id=identifier,
        kind=f"{stage}_result",
        locator=f"cas://{identifier}",
        manifest_sha256=digest({"manifest": identifier}),
        set_sha256=digest({"set": identifier}),
        producer_run_id=run_id,
        producer_stage=stage,
        producer_tool_sha256=digest({"tool": stage}),
    )


class CaptureHandlerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="capture-handler-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.fixture = build_fixture_capture(self.root / "fixture", run_id=PARENT)
        self.package = Path(self.fixture["prepared_dir"])
        self.freeze_ref = artifact("freeze-artifact", "freeze", PARENT)
        self.discover_ref = artifact("discover-artifact", "discover", PARENT)
        self.command = self._command()
        self.claim = ExecutionClaim(
            claim_id="claim-capture",
            command_id=self.command.command_id,
            fence=1,
            owner_sha256=self.command.expected_executor_sha256,
            lane_key="native",
            expires_at=datetime(2026, 10, 25, tzinfo=UTC),
        )

    def _command(
        self,
        *,
        discover: ArtifactRef | None = None,
        request: dict | None = None,
    ) -> StageCommand:
        return StageCommand.create(
            command_id="command-capture",
            run_id=CHILD,
            stage="capture",
            idempotency_key=f"{CHILD}/capture",
            request=request
            or {
                "effect_id": "effect-capture",
                "handoff_sha256": self.fixture["handoff_sha256"],
                "main_assembly": MAIN,
                "prepared_sha256": digest(self.fixture["prepared_files"]),
                "upload_sha256": self.fixture["handoff_sha256"],
            },
            input_artifacts=(self.freeze_ref, discover or self.discover_ref),
            config_snapshot_sha256=digest({"config": "capture"}),
            contract_sha256=digest({"contract": 1}),
            policy_sha256=digest({"policy": "capture"}),
            expected_executor_sha256=digest({"executor": "native"}),
            predecessor_decision_id="discover-decision",
        )

    def _discover(self, *, producer_run_id: str = PARENT) -> ArtifactRef:
        discover = self.discover_ref
        if producer_run_id != PARENT:
            discover = ArtifactRef(
                artifact_id=self.discover_ref.artifact_id,
                kind=self.discover_ref.kind,
                locator=self.discover_ref.locator,
                manifest_sha256=self.discover_ref.manifest_sha256,
                set_sha256=self.discover_ref.set_sha256,
                producer_run_id=producer_run_id,
                producer_stage="discover",
                producer_tool_sha256=self.discover_ref.producer_tool_sha256,
            )
        return discover

    def workspace(self, *, discover_run_id: str = PARENT) -> StageWorkspace:
        discover = self._discover(producer_run_id=discover_run_id)
        root = self.root / f"workspace-{discover_run_id}"
        root.mkdir()
        return StageWorkspace(
            root=root,
            inputs={
                self.freeze_ref.artifact_id: self.package / "cad",
                discover.artifact_id: self.package,
            },
        )

    def handler(self, *, native_operation=None, tool=None) -> CaptureHandler:
        return CaptureHandler(
            backend_factory=lambda package: fixture_backend(package, MAIN),
            native_tool_factory=lambda: tool or self.fixture["native_tool"],
            native_operation=native_operation,
        )

    def test_real_capture_seal_and_linux_admission_keep_producer_and_executor_separate(self) -> None:
        discovery_before = (self.package / "discovery/native-discovery.json").read_bytes()
        with patch("description_pipeline.runtime.tool_record", return_value=self.fixture["native_tool"]):
            execution = self.handler().execute(self.command, self.claim, self.workspace())

        self.assertEqual(execution.outcome, "completed")
        admitted = Path(execution.candidate_root)
        manifest = json.loads((admitted / "transfer-manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["schema_version"], "solidworks-to-urdf.capture-transfer/v2")
        self.assertEqual(manifest["run_id"], CHILD)
        self.assertEqual(manifest["native_stage_scope"], ["capture"])
        self.assertEqual(manifest["execution"]["effect_id"], "effect-capture")
        self.assertEqual(
            {
                row["producer_stage"]: row["producer_run_id"]
                for row in manifest["upstream_artifacts"]
            },
            {"discover": PARENT, "freeze": PARENT},
        )
        self.assertEqual((self.package / "discovery/native-discovery.json").read_bytes(), discovery_before)

    def test_producer_identity_is_rejected_before_native_operation(self) -> None:
        operation = Mock()
        discover = self._discover(producer_run_id="a0000000-0000-4000-8000-000000000099")
        command = self._command(discover=discover)
        with self.assertRaisesRegex(Exception, "producer"):
            self.handler(native_operation=operation).execute(
                command,
                self.claim,
                self.workspace(discover_run_id="a0000000-0000-4000-8000-000000000099"),
            )
        operation.assert_not_called()

    def test_plan_must_cover_the_complete_active_object_set_before_native_operation(self) -> None:
        package = self.root / "incomplete-plan"
        shutil.copytree(self.package, package)
        plan_path = package / PLAN_FILE
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        plan["objects"].pop()
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        prepared = {
            path.relative_to(package).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in package.rglob("*")
            if path.is_file()
        }
        command = self._command(
            request={**self.command.to_record()["request"], "prepared_sha256": digest(prepared)}
        )
        workspace_root = self.root / "incomplete-workspace"
        workspace_root.mkdir()
        operation = Mock()
        with self.assertRaisesRegex(Exception, "complete|outside"):
            self.handler(native_operation=operation).execute(
                command,
                self.claim,
                StageWorkspace(
                    root=workspace_root,
                    inputs={
                        self.freeze_ref.artifact_id: package / "cad",
                        self.discover_ref.artifact_id: package,
                    },
                ),
            )
        operation.assert_not_called()

    def test_unknown_native_effect_never_becomes_a_terminal_stage_result(self) -> None:
        operation = Mock(
            return_value=NativeCaptureResult(
                "unknown",
                None,
                None,
                {"state": "unknown"},
                "cad_process_cleanup_failed",
            )
        )
        with self.assertRaises(CaptureUnresolved):
            self.handler(native_operation=operation).execute(self.command, self.claim, self.workspace())
        self.assertEqual(operation.call_count, 1)

    def test_admission_rejects_tampered_upstream_identity_without_installing(self) -> None:
        workspace = self.workspace()
        with patch("description_pipeline.runtime.tool_record", return_value=self.fixture["native_tool"]):
            execution = self.handler().execute(self.command, self.claim, workspace)
        self.assertEqual(execution.outcome, "completed")
        archive = workspace.root / "capture-native" / "native-evidence.zip"
        changed = {
            **self.discover_ref.to_record(),
            "producer_run_id": "a0000000-0000-4000-8000-000000000099",
        }
        expected_upstream = (
            self.freeze_ref.to_record(),
            changed,
        )
        destination = self.root / "rejected-admission"
        execution_record = {
            "claim_id": self.claim.claim_id,
            "command_id": self.command.command_id,
            "effect_id": "effect-capture",
            "executor_sha256": self.claim.owner_sha256,
            "fence": self.claim.fence,
            "run_id": self.command.run_id,
        }
        with self.assertRaisesRegex(Exception, "upstream"):
            admit_capture(
                archive,
                destination,
                expected_run_id=self.command.run_id,
                expected_handoff_sha256=self.fixture["handoff_sha256"],
                expected_upload_sha256=self.fixture["handoff_sha256"],
                expected_main_assembly=MAIN,
                expected_native_tool=self.fixture["native_tool"],
                expected_prepared_dir=str(self.package),
                expected_prepared_files=self.fixture["prepared_files"],
                expected_upstream_artifacts=expected_upstream,
                expected_execution=execution_record,
            )
        self.assertFalse(destination.exists())


if __name__ == "__main__":
    unittest.main()
