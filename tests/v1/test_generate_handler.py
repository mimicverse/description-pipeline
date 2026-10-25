"""Generate handler contract over immutable capture artifacts."""

from __future__ import annotations

import copy
import shutil
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, UTC
from pathlib import Path
from unittest.mock import patch

from description_pipeline.artifacts import ArtifactStore
from description_pipeline.io import PipelineError, digest, inventory, read_data, write_json
from description_pipeline.model import EngineeringModel, emit
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
from description_pipeline.sources.solidworks.naming import visual_name
from description_pipeline.sources.snapshot import write_manifest

from tests.sources import test_solidworks_mass_authority as authority_fixtures
from description_pipeline.pipeline.stages import generate as generate_stage


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
                "description_pipeline.sources.solidworks.scene.load_scene",
                side_effect=AssertionError("transitional load_scene must not run"),
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

    def test_formal_generate_requires_a_predecessor_before_output(self) -> None:
        workspace, claim = self._workspace("generate-no-predecessor", 1)
        command = StageCommand.create(
            command_id="generate-without-predecessor",
            run_id="generate-without-predecessor-run",
            stage="generate",
            idempotency_key="generate-without-predecessor/run",
            request=self.command.to_record()["request"],
            input_artifacts=self.command.input_artifacts,
            config_snapshot_sha256=self.command.config_snapshot_sha256,
            contract_sha256=self.command.contract_sha256,
            policy_sha256=self.command.policy_sha256,
            expected_executor_sha256=self.command.expected_executor_sha256,
            predecessor_decision_id=None,
        )
        claim = replace(
            claim,
            claim_id="generate-no-predecessor",
            command_id=command.command_id,
        )
        with self.assertRaisesRegex(PipelineError, "predecessor_missing"):
            self.handler.execute(
                command,
                claim,
                workspace,
                effects=object(),
            )
        self.assertFalse((workspace.root / "output").exists())


class GenerateVerifyClosureTests(unittest.TestCase):
    def setUp(self) -> None:
        from tests.v1.test_pipeline_verify import PipelineVerifyHandlerTests

        self.fixture = PipelineVerifyHandlerTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.view_index = 0

    def _evaluate_details(self, generate_ref=None) -> tuple[dict, object]:
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
        return evaluate_bundle(root), generate

    def _evaluate(self, generate_ref=None) -> dict:
        return self._evaluate_details(generate_ref)[0]

    def _reseal_with_emitter(self, name: str, mutation) -> tuple[object, EngineeringModel, dict]:
        from description_pipeline.sources.snapshot import verify_snapshot
        from description_pipeline.sources.solidworks.captured import derive_engineering_input

        original_model = read_data(self.fixture.generate_source / "model/robot.json")
        source = self.fixture.root / f"mutated-generate-{name}"
        source.mkdir()
        payload = copy.deepcopy(original_model)
        mutation(payload)
        manifest = verify_snapshot(self.fixture.capture_root / "evidence")
        payload["semantic_identity"] = derive_engineering_input(
            self.fixture.capture_root / "evidence",
            manifest,
        )["semantic_identity"]
        model = EngineeringModel.from_dict(payload)
        emission = emit(
            model,
            evidence_root=self.fixture.capture_root / "evidence",
            destination=source,
        )
        shutil.copyfile(self.fixture.generate_source / "tool.json", source / "tool.json")
        receipt = read_data(self.fixture.generate_source / GENERATE_RECEIPT)
        receipt.update(
            {
                "semantic_identity": model.to_dict()["semantic_identity"],
                "model_sha256": emission.model_sha256,
                "urdf_sha256": emission.urdf_sha256,
                "mesh_files": list(emission.mesh_files),
            }
        )
        write_json(source / GENERATE_RECEIPT, receipt)
        reference = self.fixture.store.seal_tree(
            source,
            kind="stage_output:generate",
            producer_run_id=self.fixture.generator_command.run_id,
            producer_stage="generate",
            producer_tool_sha256=self.fixture.generate_executor,
        )
        return reference, model, receipt

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

    def test_producer_consistent_semantic_mutations_fail_independent_source_gates(self) -> None:
        def warning_objects(model: dict, code: str) -> list[dict]:
            warning = next(item for item in model["provenance"]["value_warnings"] if item["code"] == code)
            return warning["objects"]

        def material_color(model: dict) -> None:
            visual = model["links"][0]["visuals"][0]
            visual["rgba"] = [1.0, 0.0, 0.0, 1.0]
            for code in ("display.material_fallback", "display.constant"):
                for row in warning_objects(model, code):
                    if row["object"] == visual["name"]:
                        row["actual"]["rgba"] = [1.0, 0.0, 0.0, 1.0]

        def mass_inertia(model: dict) -> None:
            inertial = model["links"][0]["inertial"]
            inertial["mass"] *= 1.5
            inertial["inertia"][0] *= 2.0

        def joint_axis(model: dict) -> None:
            joint = model["joints"][0]
            joint["axis"] = [1.0, 0.0, 0.0]
            joint["provenance"]["axis.delivered_local_axis"] = [1.0, 0.0, 0.0]
            for row in warning_objects(model, "direction.semantics_unresolved"):
                if row["object"] == joint["name"]:
                    row["actual"] = [1.0, 0.0, 0.0]

        def object_binding(model: dict) -> None:
            visual = model["links"][0]["visuals"][0]
            previous = visual["name"]
            native_component = "base-1"
            current = visual_name(native_component, link="arm_link")
            visual["provenance"]["native_component"] = native_component
            visual["name"] = current
            for code in (
                "naming.derived_visual",
                "naming.content_addressed_mesh",
                "display.material_fallback",
                "display.constant",
            ):
                for row in warning_objects(model, code):
                    if row["object"] != previous:
                        continue
                    row["object"] = current
                    if code == "naming.derived_visual":
                        row["original"] = native_component
                        row["actual"] = current

        def missing_warning(model: dict) -> None:
            model["provenance"]["value_warnings"] = [
                item
                for item in model["provenance"]["value_warnings"]
                if item["code"] != "display.material_fallback"
            ]

        cases = (
            (
                "material-color",
                material_color,
                {"provenance.value_warnings"},
            ),
            (
                "mass-inertia",
                mass_inertia,
                {
                    "physics.mass_closure_equality",
                    "physics.independent",
                    "physics.expected_mass",
                },
            ),
            (
                "joint-axis",
                joint_axis,
                {"joints.shoulder_pitch_joint"},
            ),
            (
                "object-binding",
                object_binding,
                {"provenance.value_warnings"},
            ),
            (
                "missing-warning",
                missing_warning,
                {"provenance.value_warnings"},
            ),
        )
        capture_before = digest(inventory(self.fixture.capture_root))
        original_model = EngineeringModel.from_dict(
            read_data(self.fixture.generate_source / "model/robot.json")
        )
        for name, mutation, expected_failures in cases:
            with self.subTest(name=name):
                reference, model, receipt = self._reseal_with_emitter(name, mutation)
                report, generate = self._evaluate_details(reference)
                checks = {item["id"]: item for item in report["checks"]}
                self.assertIs(report["passed"], False)
                self.assertIs(checks["model.schema"]["passed"], True)
                self.assertIs(checks["urdf.syntax_names"]["passed"], True)
                if name == "mass-inertia":
                    self.assertIs(checks["inertia.arm_link"]["passed"], True)
                for identifier in expected_failures:
                    self.assertIs(checks[identifier]["passed"], False)
                self.assertEqual(
                    {
                        item["id"]
                        for item in report["checks"]
                        if item["passed"] is not True
                    },
                    expected_failures,
                )
                self.assertEqual(
                    model.to_dict()["semantic_identity"],
                    original_model.to_dict()["semantic_identity"],
                )
                self.assertNotEqual(model.semantic_sha256, original_model.semantic_sha256)
                self.assertNotEqual(reference.artifact_id, self.fixture.generate_ref.artifact_id)
                self.assertNotEqual(reference.set_sha256, self.fixture.generate_ref.set_sha256)
                members = {item["path"]: item["sha256"] for item in generate.manifest["members"]}
                self.assertEqual(members["model/robot.json"], receipt["model_sha256"])
                self.assertEqual(members["urdf/robot.urdf"], receipt["urdf_sha256"])
                if name == "missing-warning":
                    urdf = (generate.root / "urdf/robot.urdf").read_text(encoding="utf-8")
                    self.assertNotIn("WARNING display.material_fallback:", urdf)
                self.assertEqual(digest(inventory(self.fixture.capture_root)), capture_before)

    def test_shared_aligned_status_cannot_suppress_unresolved_warning(self) -> None:
        from description_pipeline.sources.solidworks import (
            discovery as discovery_module,
        )
        from description_pipeline.verification import (
            native_declarations,
            native_discovery,
        )

        baseline_model = read_data(
            self.fixture.generate_root / "model/robot.json"
        )
        baseline_joint = baseline_model["joints"][0]
        original_status = baseline_joint["provenance"]["axis.direction_relation"]
        self.assertEqual(original_status, "unverified")
        baseline = self._evaluate()
        self.assertTrue(baseline["passed"])
        capture_before = digest(inventory(self.fixture.capture_root))

        original = discovery_module.canonicalize_axis

        def aligned_status(*args, **kwargs):
            decision = original(*args, **kwargs)
            decision["status"] = "local_class_aligned"
            return decision

        workspace = StageWorkspace(
            root=self.fixture.root / "aligned-status-generate",
            inputs={self.fixture.capture_ref.artifact_id: self.fixture.capture_root},
        )
        claim = self.fixture._claim(
            self.fixture.generator_command,
            owner=digest({"process": "aligned-status-mutation"}),
            executor=self.fixture.generate_executor,
        )
        with (
            patch.object(
                discovery_module,
                "canonicalize_axis",
                side_effect=aligned_status,
            ),
            patch.object(
                native_discovery,
                "canonicalize_axis",
                side_effect=aligned_status,
            ),
            patch.object(
                native_declarations,
                "canonicalize_axis",
                side_effect=aligned_status,
            ),
        ):
            generated = GenerateHandler(self.fixture.store).execute(
                self.fixture.generator_command,
                claim,
                workspace,
                effects=object(),
            )
            source = Path(generated.candidate_root)
            reference = self.fixture.store.seal_tree(
                source,
                kind="stage_output:generate",
                producer_run_id=self.fixture.generator_command.run_id,
                producer_stage="generate",
                producer_tool_sha256=self.fixture.generate_executor,
            )
            report, _generate = self._evaluate_details(reference)

        poisoned_model = read_data(source / "model/robot.json")
        joint = poisoned_model["joints"][0]
        self.assertEqual(
            joint["provenance"]["axis.direction_relation"],
            "local_class_aligned",
        )
        for field in ("axis", "xyz", "rpy", "limits"):
            self.assertEqual(joint[field], baseline_joint[field])
        self.assertEqual(
            digest(inventory(self.fixture.capture_root)),
            capture_before,
        )
        direction_warnings = [
            item
            for item in poisoned_model["provenance"]["value_warnings"]
            if item["code"] == "direction.semantics_unresolved"
        ]
        self.assertEqual(len(direction_warnings), 1)
        direction_warning = direction_warnings[0]
        self.assertEqual(direction_warning["objects"][0]["object"], joint["name"])
        self.assertTrue(report["passed"], [item for item in report["checks"] if not item["passed"]])
        qualification = next(
            item
            for item in report["qualifications"]
            if item["fact_id"] == "machine_qualification"
        )
        self.assertEqual(qualification["status"], "unqualified")
        baseline_qualification = next(
            item
            for item in baseline["qualifications"]
            if item["fact_id"] == "machine_qualification"
        )
        self.assertEqual(
            qualification["status"],
            baseline_qualification["status"],
        )


if __name__ == "__main__":
    unittest.main()
