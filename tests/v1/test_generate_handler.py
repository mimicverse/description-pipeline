"""Generate handler contract over immutable capture artifacts."""

from __future__ import annotations

import copy
import re
import shutil
import tempfile
import unittest
from dataclasses import replace
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
from description_pipeline.runtime import tool_record
from description_pipeline.schemas import STAGE_RESULT_SCHEMA
from description_pipeline.pipeline.stages.generate import (
    CAPTURE_ARTIFACT_KIND,
    GENERATE_OUTPUT_CONTRACT,
    GENERATE_RECEIPT,
    GENERATE_REQUEST_SCHEMA,
    GenerateHandler,
)
from description_pipeline.sources.solidworks.generation_warnings import generation_value_warnings
from description_pipeline.sources.snapshot import write_manifest

from tests.sources import test_solidworks_mass_authority as authority_fixtures
from description_pipeline.pipeline.stages import generate as generate_stage
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
        self.tool = tool_record(role="portable")
        self.executor = self.tool["source_sha256"]
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
            expected_executor_sha256=self.executor,
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
            executor_sha256=self.executor,
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
            first = self.handler.execute(self.command, first_claim, first_workspace, effects=object())
        first_files = inventory(Path(first.candidate_root))
        shutil.rmtree(first_workspace.root)

        second_workspace, second_claim = self._workspace("attempt-b", 2)
        second = self.handler.execute(self.command, second_claim, second_workspace, effects=object())
        second_files = inventory(Path(second.candidate_root))

        self.assertEqual(first.outcome, "completed")
        self.assertEqual(first.checks_sha256, second.checks_sha256)
        self.assertEqual(first_files, second_files)
        self.assertNotIn(str(first_workspace.root), repr(first_files))
        self.assertNotIn(str(second_workspace.root), repr(second_files))

    def _candidate(self, output_artifact, *, checks_sha256: str) -> StageCandidate:
        result = StageResult(
            schema_version=STAGE_RESULT_SCHEMA,
            command_id=self.command.command_id,
            run_id=self.command.run_id,
            stage="generate",
            request_digest=self.command.request_digest,
            outcome="completed",
            output_artifact=output_artifact,
            actual_executor_sha256=self.executor,
            checks_sha256=checks_sha256,
            resources_settled=True,
        )
        return StageCandidate(result=result)

    def test_candidate_validation_is_structural_and_does_not_regenerate(self) -> None:
        workspace, claim = self._workspace("attempt", 1)
        with (
            patch.object(generate_stage, "_compile_capture", wraps=generate_stage._compile_capture) as compile_call,
            patch.object(generate_stage, "emit", wraps=generate_stage.emit) as emit_call,
        ):
            execution = self.handler.execute(self.command, claim, workspace, effects=object())
            candidate_artifact = self.store.seal_tree(
                execution.candidate_root,
                kind="stage_output:generate",
                producer_run_id=self.command.run_id,
                producer_stage="generate",
                producer_tool_sha256=self.executor,
            )
            candidate = self._candidate(candidate_artifact, checks_sha256=execution.checks_sha256)
            self.handler.validate_candidate(self.command, claim, candidate, effects=object())
        self.assertEqual(compile_call.call_count, 1)
        self.assertEqual(emit_call.call_count, 1)

    def test_receipt_records_actual_and_required_tool_identity_separately(self) -> None:
        workspace, claim = self._workspace("attempt", 1)
        execution = self.handler.execute(self.command, claim, workspace, effects=object())
        tool = read_data(Path(execution.candidate_root) / "tool.json")
        receipt = read_data(Path(execution.candidate_root) / "reports/generate.json")
        self.assertEqual(receipt["required_executor_sha256"], self.command.expected_executor_sha256)
        self.assertEqual(receipt["actual_executor_sha256"], tool["source_sha256"])
        self.assertEqual(tool, self.tool)

    def test_current_tool_must_match_command_and_claim(self) -> None:
        workspace, claim = self._workspace("attempt", 1)
        drifted = replace(claim, executor_sha256=digest({"tool": "different"}))
        with self.assertRaisesRegex(ProtocolError, "generate.executor_mismatch"):
            self.handler.execute(self.command, drifted, workspace, effects=object())
        self.assertFalse((workspace.root / "output").exists())

    def test_mutated_output_is_rejected_without_generation(self) -> None:
        workspace, claim = self._workspace("attempt", 1)
        execution = self.handler.execute(self.command, claim, workspace, effects=object())
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
        candidate = self._candidate(candidate_artifact, checks_sha256=execution.checks_sha256)
        with (
            patch.object(generate_stage, "_compile_capture") as compile_call,
            self.assertRaisesRegex(ProtocolError, "generate.receipt_urdf"),
        ):
            self.handler.validate_candidate(self.command, claim, candidate, effects=object())
        compile_call.assert_not_called()

    def test_mutated_model_is_rejected_without_generation(self) -> None:
        workspace, claim = self._workspace("attempt", 1)
        execution = self.handler.execute(self.command, claim, workspace, effects=object())
        candidate_root = workspace.root / "candidate-source"
        shutil.copytree(execution.candidate_root, candidate_root)
        model_path = candidate_root / "model/robot.json"
        model = read_data(model_path)
        model["name"] = "mutated_robot"
        write_json(model_path, model)
        candidate_artifact = self.store.seal_tree(
            candidate_root,
            kind="stage_output:generate",
            producer_run_id=self.command.run_id,
            producer_stage="generate",
            producer_tool_sha256=self.executor,
        )
        candidate = self._candidate(candidate_artifact, checks_sha256=execution.checks_sha256)
        with (
            patch.object(generate_stage, "_compile_capture") as compile_call,
            self.assertRaisesRegex(ProtocolError, "generate.receipt_model"),
        ):
            self.handler.validate_candidate(self.command, claim, candidate, effects=object())
        compile_call.assert_not_called()

    def test_input_artifact_mutation_is_rejected(self) -> None:
        workspace, claim = self._workspace("attempt", 1)
        execution = self.handler.execute(self.command, claim, workspace, effects=object())
        candidate_artifact = self.store.seal_tree(
            execution.candidate_root,
            kind="stage_output:generate",
            producer_run_id=self.command.run_id,
            producer_stage="generate",
            producer_tool_sha256=self.executor,
        )
        capture_object = self.store.objects / self.capture_ref.artifact_id
        request_path = capture_object / "input/request.json"
        request_path.write_bytes(request_path.read_bytes() + b" ")
        candidate = self._candidate(candidate_artifact, checks_sha256=execution.checks_sha256)
        with self.assertRaises(PipelineError):
            self.handler.validate_candidate(self.command, claim, candidate, effects=object())

    def test_missing_capture_input_fails_before_output(self) -> None:
        workspace, claim = self._workspace("attempt", 1)
        (workspace.inputs[self.capture_ref.artifact_id] / "input/request.json").unlink()
        with self.assertRaises(PipelineError):
            self.handler.execute(self.command, claim, workspace, effects=object())
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
        self.assertEqual(actual[0]["objects"][0]["fin_endpoint"], "end")
        self.assertEqual(
            actual[0]["objects"][0]["topology_endpoint_set"],
            source["topology_endpoint_set"],
        )

    def test_fallback_without_v5_identity_does_not_mint_a_warning(self) -> None:
        source = warning_fixtures.tolerant_warning()
        geometry = [
            {
                "component": "part-1",
                "tessellation": {
                    "native_edges": [],
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
                                        "start_evidence": {
                                            "tolerant_edge_evidence": None,
                                            "topology_endpoint_set_accepted": False,
                                        },
                                        "end_evidence": {
                                            "tolerant_edge_evidence": None,
                                            "topology_endpoint_set_accepted": False,
                                        },
                                        "open_edge_boundary_identity": None,
                                    }
                                ],
                            }
                        ]
                    },
                },
            }
        ]
        with tempfile.TemporaryDirectory(prefix="generate-fallback-warning-") as temporary:
            root = Path(temporary)
            write_json(root / "evidence/raw/geometry.json", geometry)
            write_json(root / "evidence/raw/source_scopes.json", {"part-1": source["source_scope"]})
            self.assertEqual(generation_value_warnings(root), [])


class GenerateVerifyClosureTests(unittest.TestCase):
    def setUp(self) -> None:
        from tests.v1.test_pipeline_verify import PipelineVerifyHandlerTests

        self.fixture = PipelineVerifyHandlerTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.view_index = 0

    def _evaluate(self, generate_ref=None) -> dict:
        from description_pipeline.interface_records import check_policy_digest
        from description_pipeline.pipeline.verify import _evaluation_view, _verify_artifact_tree
        from description_pipeline.stages import CONTRACT_SHA256
        from description_pipeline.verification.solidworks_urdf import evaluate_bundle

        generate_ref = generate_ref or self.fixture.generate_ref
        self.view_index += 1
        root = self.fixture.root / f"independent-view-{self.view_index}"
        generate_root = (
            self.fixture.generate_root
            if generate_ref == self.fixture.generate_ref
            else self.fixture.store.materialize(
                generate_ref,
                self.fixture.root / f"independent-generate-{self.view_index}",
            )
        )
        generate = _verify_artifact_tree(generate_root, generate_ref)
        capture = _verify_artifact_tree(self.fixture.capture_root, self.fixture.capture_ref)
        _evaluation_view(
            root,
            generate=generate,
            capture=capture,
            contract_sha256=CONTRACT_SHA256,
            policy_sha256=check_policy_digest(),
        )
        return evaluate_bundle(root)

    def _reseal(self, name: str, mutation) -> object:
        from description_pipeline.io import file_digest

        source = self.fixture.root / f"mutated-generate-{name}"
        shutil.copytree(self.fixture.generate_source, source)
        urdf = source / "urdf/robot.urdf"
        model_path = source / "model/robot.json"
        before_model = model_path.read_bytes()
        before_urdf = urdf.read_bytes()
        mutation(model_path=model_path, urdf=urdf)
        receipt_path = source / GENERATE_RECEIPT
        receipt = read_data(receipt_path)
        if model_path.read_bytes() != before_model:
            model = read_data(model_path)
            receipt["model_sha256"] = file_digest(model_path)
            receipt["semantic_identity"] = model.get("semantic_identity")
        if urdf.read_bytes() != before_urdf:
            receipt["urdf_sha256"] = file_digest(urdf)
        write_json(receipt_path, receipt)
        return self.fixture.store.seal_tree(
            source,
            kind="stage_output:generate",
            producer_run_id=self.fixture.generator_command.run_id,
            producer_stage="generate",
            producer_tool_sha256=self.fixture.generate_executor,
        )

    def test_generate_pair_closes_to_independent_verify_with_per_field_warnings(self) -> None:
        report = self._evaluate()
        self.assertIs(report["passed"], True, [check for check in report["checks"] if not check["passed"]])
        model = read_data(self.fixture.generate_root / "model/robot.json")
        warnings = model["provenance"]["value_warnings"]
        by_code = {item["code"]: item for item in warnings}
        for code in (
            "display.material_fallback",
            "display.constant",
            "geometry.derived_from_native",
            "physics.derived_from_native",
            "metadata.non_native",
        ):
            self.assertIn(code, by_code)
            self.assertTrue(by_code[code]["objects"])
            self.assertTrue(all(item.get("object") for item in by_code[code]["objects"]))
            self.assertTrue(all(item.get("fields") or item.get("field") for item in by_code[code]["objects"]))

    def test_resealed_generate_semantic_mutations_are_rejected(self) -> None:
        cases = (
            (
                "material-color",
                lambda model_path, urdf: urdf.write_text(
                    re.sub(
                        r'rgba="[^"]+"',
                        'rgba="1 0 0 1"',
                        urdf.read_text(encoding="utf-8"),
                        count=1,
                    ),
                    encoding="utf-8",
                ),
                "urdf.syntax_names",
            ),
            (
                "object-binding",
                lambda model_path, urdf: urdf.write_text(
                    re.sub(
                        r'visual name="[^"]+"',
                        'visual name="foreign_visual"',
                        urdf.read_text(encoding="utf-8"),
                        count=1,
                    ),
                    encoding="utf-8",
                ),
                "urdf.syntax_names",
            ),
            (
                "inertia",
                lambda model_path, urdf: urdf.write_text(
                    re.sub(
                        r'ixx="[^"]+"',
                        'ixx="9.0"',
                        urdf.read_text(encoding="utf-8"),
                        count=1,
                    ),
                    encoding="utf-8",
                ),
                "inertia.arm_link",
            ),
            (
                "axis-transform",
                lambda model_path, urdf: urdf.write_text(
                    re.sub(
                        r'<axis xyz="[^"]+"',
                        '<axis xyz="0 1 0"',
                        urdf.read_text(encoding="utf-8"),
                        count=1,
                    ),
                    encoding="utf-8",
                ),
                "urdf.syntax_names",
            ),
            (
                "missing-warning",
                lambda model_path, urdf: write_json(
                    model_path,
                    {
                        **read_data(model_path),
                        "provenance": {
                            **read_data(model_path)["provenance"],
                            "value_warnings": [
                                item
                                for item in read_data(model_path)["provenance"]["value_warnings"]
                                if item["code"] != "display.material_fallback"
                            ],
                        },
                    },
                ),
                "provenance.value_warnings",
            ),
        )
        for name, mutation, expected_gate in cases:
            with self.subTest(name=name):
                reference = self._reseal(name, mutation)
                report = self._evaluate(reference)
                self.assertIs(report["passed"], False)
                failed = {check["id"] for check in report["checks"] if check["passed"] is not True}
                self.assertIn(expected_gate, failed)


if __name__ == "__main__":
    unittest.main()
