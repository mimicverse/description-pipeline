"""Generate handler contract over immutable capture artifacts."""

from __future__ import annotations

import copy
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, UTC
from pathlib import Path
from unittest.mock import patch

from description_pipeline.artifacts import ArtifactStore
from description_pipeline.geometry.native_u import native_tolerant_edge_value_warning
from description_pipeline.io import PipelineError, digest, inventory, read_data, write_json
from description_pipeline.pipeline.contracts import (
    ExecutionClaim,
    ProtocolError,
    StageCandidate,
    StageCommand,
    StageResult,
)
from description_pipeline.pipeline.engine import StageWorkspace
from description_pipeline.pipeline.stages.generate import (
    CAPTURE_ARTIFACT_KIND,
    GENERATE_OUTPUT_CONTRACT,
    GENERATE_REQUEST_SCHEMA,
    GenerateHandler,
)
from description_pipeline.sources.solidworks.generation_warnings import generation_value_warnings
from description_pipeline.sources.snapshot import write_manifest

from tests.sources import test_solidworks_mass_authority as authority_fixtures
from tests.v1 import test_native_scope_warning_projection as warning_fixtures


class GenerateHandlerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="generate-handler-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.authority = authority_fixtures.CadMassAuthorityTests(methodName="runTest")
        self.authority.setUp()
        self.addCleanup(self.authority.tearDown)
        self.capture = self.authority._freeze("capture")
        authority_fixtures._compose_scene_inputs(self.capture, self.authority.tmp / "cad")
        artifact_source = self.root / "capture-artifact"
        evidence = artifact_source / "evidence"
        evidence.mkdir(parents=True)
        shutil.copytree(self.capture / "input", artifact_source / "input")
        shutil.copytree(self.capture / "raw", evidence / "raw")
        shutil.copytree(self.capture / "source", evidence / "source")
        shutil.copytree(self.capture / "evidence", evidence / "evidence")
        shutil.copyfile(self.capture / "evidence" / "discovery.json", evidence / "discovery.json")
        shutil.copytree(self.capture / "reports", artifact_source / "reports")
        shutil.copyfile(self.capture / "native-runtime.json", evidence / "native-runtime.json")
        source_manifest = read_data(self.capture / "manifest.json")
        write_manifest(
            evidence,
            kind=source_manifest["kind"],
            identity=source_manifest["identity"],
            evidence_class=source_manifest["evidence_class"],
        )
        self.store = ArtifactStore(self.root / "cas")
        capture_tool = digest({"tool": "capture"})
        self.capture_ref = self.store.seal_tree(
            artifact_source,
            kind=CAPTURE_ARTIFACT_KIND,
            producer_run_id="capture-run",
            producer_stage="capture",
            producer_tool_sha256=capture_tool,
        )
        self.command = StageCommand.create(
            command_id="generate-command",
            run_id="generate-run",
            stage="generate",
            idempotency_key="generate-run/generate",
            request={
                "schema_version": GENERATE_REQUEST_SCHEMA,
                "capture_artifact_id": self.capture_ref.artifact_id,
                "output_contract": GENERATE_OUTPUT_CONTRACT,
            },
            input_artifacts=(self.capture_ref,),
            config_snapshot_sha256=digest({"config": 1}),
            contract_sha256=digest({"rules": 1}),
            policy_sha256=digest({"policy": 1}),
            expected_executor_sha256=digest({"executor": "generate-tool"}),
            predecessor_decision_id="capture-decision",
        )
        self.handler = GenerateHandler(self.store)

    def _workspace(self, name: str, fence: int):
        root = self.root / name
        root.mkdir()
        source = self.store.materialize(self.capture_ref, root / "capture")
        claim = ExecutionClaim(
            claim_id=f"claim-{fence}",
            command_id=self.command.command_id,
            fence=fence,
            owner_sha256=digest({"process": fence}),
            lane_key=None,
            expires_at=datetime(2026, 10, 25, tzinfo=UTC) + timedelta(minutes=5),
        )
        return StageWorkspace(root=root, inputs={self.capture_ref.artifact_id: source}), claim

    def test_generate_is_work_root_independent_and_survives_scratch_loss(self) -> None:
        first_workspace, first_claim = self._workspace("attempt-a", 1)
        with (
            patch(
                "description_pipeline.steps.generate_model",
                side_effect=AssertionError("legacy Generate entry must not run"),
            ),
            patch(
                "description_pipeline.steps.verify_delivery",
                side_effect=AssertionError("Generate must not run Verify"),
            ),
            patch(
                "description_pipeline.solidworks.verify",
                side_effect=AssertionError("Generate must not run the legacy Verify stage"),
            ),
            patch(
                "description_pipeline.sources.solidworks.scene.load_scene",
                side_effect=AssertionError("transitional load_scene must not run"),
            ),
            patch(
                "description_pipeline.verification.native_mesh_observations.native_geometry_value_warnings",
                side_effect=AssertionError("verification helper must not produce model warnings"),
            ),
        ):
            first = self.handler.execute(self.command, first_claim, first_workspace)
        first_files = inventory(Path(first.candidate_root))
        shutil.rmtree(first_workspace.root)

        second_workspace, second_claim = self._workspace("attempt-b", 2)
        second = self.handler.execute(self.command, second_claim, second_workspace)
        second_files = inventory(Path(second.candidate_root))

        self.assertEqual(first.outcome, "completed")
        self.assertEqual(first.checks_sha256, second.checks_sha256)
        self.assertEqual(first_files, second_files)
        self.assertNotIn(str(first_workspace.root), repr(first_files))
        self.assertNotIn(str(second_workspace.root), repr(second_files))

    def test_mutated_candidate_is_rejected_by_deterministic_replay(self) -> None:
        workspace, claim = self._workspace("attempt", 1)
        execution = self.handler.execute(self.command, claim, workspace)
        candidate_root = workspace.root / "candidate-source"
        shutil.copytree(execution.candidate_root, candidate_root)
        urdf = candidate_root / "urdf/robot.urdf"
        urdf.write_bytes(urdf.read_bytes().replace(b"<robot", b"<robot_alt", 1))
        candidate_artifact = self.store.seal_tree(
            candidate_root,
            kind="stage_output:generate",
            producer_run_id=self.command.run_id,
            producer_stage="generate",
            producer_tool_sha256=self.command.expected_executor_sha256,
        )
        result = StageResult(
            schema_version="description-pipeline.stage-result/v1",
            command_id=self.command.command_id,
            run_id=self.command.run_id,
            stage="generate",
            request_digest=self.command.request_digest,
            outcome="completed",
            result_artifact=candidate_artifact,
            actual_executor_sha256=self.command.expected_executor_sha256,
            checks_sha256=execution.checks_sha256,
        )
        with self.assertRaisesRegex(ProtocolError, "generate.candidate_mismatch"):
            self.handler.validate_candidate(self.command, claim, StageCandidate(result=result))

    def test_missing_capture_input_fails_before_output(self) -> None:
        workspace, claim = self._workspace("attempt", 1)
        (workspace.inputs[self.capture_ref.artifact_id] / "input/request.json").unlink()
        with self.assertRaises(PipelineError):
            self.handler.execute(self.command, claim, workspace)
        self.assertFalse((workspace.root / "output").exists())


class ProductionWarningTests(unittest.TestCase):
    def test_production_projection_reads_native_records_independently(self) -> None:
        source = warning_fixtures.tolerant_warning()
        scope = source["source_scope"]
        tolerant = {
            key: copy.deepcopy(source[key])
            for key in (
                "parameter_boundary",
                "point_roundtrip_rule",
                "evaluate_rule_version",
                "evaluate_metric",
                "evaluate_point_error_m",
                "point_tolerance_m",
                "projection_metric",
                "projection_to_curve_l2_error_m",
                "eligible_gap_m",
                "gap_used",
                "composed_projection_tolerance_m",
            )
        }
        geometry = [
            {
                "component": "part-1",
                "tessellation": {
                    "native_edges": [
                        {
                            "body_type": 1,
                            "body_index": 0,
                            "edge_reference_sha256": source["edge_reference_sha256"],
                            "edge_tolerance": {"out_variant_type": source["edge_tolerance_out_variant_type"]},
                            "topology_endpoint_assignment": source["topology_endpoint_set"],
                        }
                    ],
                    "itessellation": {
                        "bodies": [
                            {
                                "body_type": 1,
                                "body_index": 0,
                                "fins": [
                                    {
                                        "classification": "native_edge",
                                        "fin_id": source["fin_id"],
                                        "edge_reference_sha256": source["edge_reference_sha256"],
                                        "start_evidence": {"tolerant_edge_evidence": None},
                                        "end_evidence": {"tolerant_edge_evidence": tolerant},
                                        "open_edge_boundary_identity": {
                                            "identity_chain_sha256": source[
                                                "open_edge_boundary_identity_sha256"
                                            ]
                                        },
                                    }
                                ],
                            }
                        ]
                    },
                },
            }
        ]
        with tempfile.TemporaryDirectory(prefix="generate-warning-") as temporary:
            root = Path(temporary)
            write_json(root / "evidence/raw/geometry.json", geometry)
            write_json(root / "evidence/raw/source_scopes.json", {"part-1": scope})
            actual = generation_value_warnings(root)
        expected = native_tolerant_edge_value_warning([source])
        self.assertEqual(actual, expected)


if __name__ == "__main__":
    unittest.main()
