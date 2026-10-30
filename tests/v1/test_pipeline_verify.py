"""VerifyHandler tests over real GenerateHandler and ArtifactStore outputs."""

from __future__ import annotations

import copy
import hashlib
import math
import shutil
import tempfile
import unittest
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree as ET

try:
    import pgserver
    import psycopg2
except ImportError:
    pgserver = None
    psycopg2 = None

from description_pipeline.artifacts import ArtifactStore
from description_pipeline.geometry import stl
from description_pipeline.interface_records import check_policy_digest
from description_pipeline.io import PipelineError, digest, file_digest, format_number, read_data, write_json
import description_pipeline.pipeline.verify as pipeline_verify
from description_pipeline.pipeline.contracts import (
    DiagnosticReplayScope,
    ExecutionClaim,
    FailureEnvelope,
    ProtocolError,
    StageCandidate,
    StageCommand,
    StageResult,
    StageFailure,
)
from description_pipeline.pipeline.diagnostic import (
    DiagnosticAdmissionSpec,
    build_diagnostic_command,
)
from description_pipeline.pipeline.engine import StageWorkspace, execute_stage
from description_pipeline.pipeline.services import CanonicalStageServices, ClaimPolicy
from description_pipeline.pipeline.stages.generate import (
    GENERATE_OUTPUT_CONTRACT,
    GENERATE_REQUEST_SCHEMA,
    GENERATE_RECEIPT,
    GenerateHandler,
)
from description_pipeline.pipeline.verify import (
    NoExternalEffects,
    CERTIFICATE_FILENAME,
    EXECUTION_RECEIPT_FILENAME,
    OUTPUT_CONTRACT,
    REQUEST_SCHEMA,
    VerifyHandler,
    replay_verification,
    validate_verification_certificate,
)
from description_pipeline.runtime import tool_record
from description_pipeline.schemas import DIAGNOSTIC_ADMISSION_SCHEMA, STAGE_RESULT_SCHEMA
from description_pipeline.sources.snapshot import write_manifest
from description_pipeline.stages import CONTRACT_SHA256
from description_pipeline.platform.facts import FactStore
from description_pipeline.verification.consumer import ConsumerError
from tests.v1 import verification_fixture as hermetic

PG_AVAILABLE = pgserver is not None and psycopg2 is not None
PLATFORM_SCHEMA = Path(__file__).resolve().parents[2] / "deploy" / "platform" / "schema.sql"
PART_FACE_OVERRIDE_FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "native-appearance-part-face-override.json"
)


def _appearance_entity_record(
    *,
    reference=(1, 2, 3),
    owner_scope="component_document",
    owner_document_path="C:/cad/arm.SLDPRT",
    native_interface_kinds=None,
):
    interface_kinds = (
        list(native_interface_kinds)
        if native_interface_kinds is not None
        else ["IFace2", "IEntity"]
    )
    return {
        "entity_index": 0,
        "native_object_type": "_CDispatch",
        "native_interface_kinds": interface_kinds,
        "persistent_reference": list(reference),
        "binding_context": {
            "owner_scope": owner_scope,
            "owner_document_path": owner_document_path,
            "component": "arm-1",
            "api": "IModelDocExtension.GetPersistReference3",
        },
        "native_identity": {
            "kind": "reference",
            "state": "observed",
            "proof_api": (
                "IModelDocExtension.GetObjectByPersistReference3"
            ),
            "status": 0,
            "owner_document_path": owner_document_path,
            "interface_kinds": interface_kinds,
            "read_errors": {},
        },
        "read_errors": {},
    }


def _appearance_index_record(
    *,
    reference,
    target,
    owner_scope="component_document",
    owner_document_path="C:/cad/arm.SLDPRT",
):
    target_kind = target.get("kind")
    if target_kind == "body":
        target = {
            "kind": "body",
            "component": target.get("component", "arm-1"),
            "faces": [
                {
                    "body_type": target.get("body_type"),
                    "body_index": target.get("body_index"),
                    "face_index": target.get("face_index", 0),
                }
            ],
            "occurrence_bodies": [
                [
                    target.get("body_type"),
                    target.get("body_index"),
                ]
            ],
            "persistent_reference_sha256": hashlib.sha256(
                bytes(item & 0xFF for item in reference)
            ).hexdigest(),
        }
    is_assembly_occurrence = target_kind == "occurrence"
    is_body = target_kind == "body"
    is_feature = target_kind == "feature"
    return {
        "persistent_reference": list(reference),
        "native_interface_kind": (
            "IEntity"
            if target.get("kind") == "face"
            else "IBody2"
            if target.get("kind") == "body"
            else None
        ),
        "read_errors": {},
        "conversion": (
            None
            if is_assembly_occurrence
            else {
                "component": target.get("component", "arm-1"),
                "input_context": {
                    "owner_scope": "assembly_occurrence",
                    "object_kind": "assembly_occurrence",
                    "source_api": (
                        "IComponent2.GetBodies3/IBody2.GetFaces"
                    ),
                    "traversal_api": (
                        "IConfiguration.GetRootComponent3/"
                        "IComponent2.GetChildren"
                    ),
                },
                "receiver": {
                    "owner_scope": "component_document",
                    "owner_document_path": owner_document_path,
                    "interface": "IModelDocExtension",
                    "object_source": "captured_component_source_document",
                },
                "api": (
                    "IFace2.GetBody+"
                    "IModelDocExtension.GetPersistReference3"
                    if is_body
                    else "IFace2.GetFeature+"
                    "IModelDocExtension.GetPersistReference3"
                    if is_feature
                    else "IModelDocExtension.GetCorrespondingEntity2"
                ),
                "output_interface_kind": (
                    "IBody2"
                    if is_body
                    else "IFeature"
                    if is_feature
                    else "IFace2"
                ),
                "output_domain": {
                    "proof_api": (
                        "IModelDocExtension.GetObjectByPersistReference3"
                    ),
                    "status": 0,
                    "interface_kind": (
                        "IBody2"
                        if is_body
                        else "IFeature"
                        if is_feature
                        else "IFace2"
                    ),
                    "expected_owner_document_path": owner_document_path,
                },
                "state": "observed",
                "read_errors": {},
            }
        ),
        "binding_context": {
            "owner_scope": owner_scope,
            "owner_document_path": owner_document_path,
            "component": "arm-1",
            "api": "IModelDocExtension.GetPersistReference3",
        },
        "target": target,
    }


def _attach_body_witnesses(raw, references=None):
    references = references or {}
    document_path = raw["source_document_path"]
    for body in raw["bodies"]:
        observations = []
        distinct_references = set()
        for face_position, face in enumerate(body["faces"]):
            face_key = (
                body["body_type"],
                body["body_index"],
                face["face_index"],
            )
            face_reference = [101, face_position]
            feature_reference = [201, face_position]
            face_row = _appearance_index_record(
                reference=face_reference,
                target={
                    "kind": "face",
                    "component": "arm-1",
                    "body_type": face_key[0],
                    "body_index": face_key[1],
                    "face_index": face_key[2],
                },
            )
            feature_row = _appearance_index_record(
                reference=feature_reference,
                target={
                    "kind": "feature",
                    "component": "arm-1",
                    "name": "Boss-Extrude1",
                    "type_name": "Extrusion",
                    "faces": [
                        {
                            "body_type": face_key[0],
                            "body_index": face_key[1],
                            "face_index": face_key[2],
                        }
                    ],
                },
            )
            face["appearance"]["native_identity"] = {
                "persistent_reference": face_reference,
                "conversion": face_row["conversion"],
                "read_errors": {},
                "feature": {
                    "name": "Boss-Extrude1",
                    "type_name": "Extrusion",
                    "persistent_reference": feature_reference,
                    "conversion": feature_row["conversion"],
                    "read_errors": {},
                },
            }
            reference = list(references.get(face_key, (1, 2, 3)))
            distinct_references.add(tuple(reference))
            observations.append(
                {
                    "face_index": face["face_index"],
                    "persistent_reference": reference,
                    "conversion": {
                        "component": raw["component"],
                        "input_context": {
                            "owner_scope": "assembly_occurrence",
                            "object_kind": "assembly_occurrence",
                            "source_api": (
                                "IComponent2.GetBodies3/IBody2.GetFaces"
                            ),
                            "traversal_api": (
                                "IConfiguration.GetRootComponent3/"
                                "IComponent2.GetChildren"
                            ),
                        },
                        "receiver": {
                            "owner_scope": "component_document",
                            "owner_document_path": document_path,
                            "interface": "IModelDocExtension",
                            "object_source": (
                                "captured_component_source_document"
                            ),
                        },
                        "api": (
                            "IFace2.GetBody+"
                            "IModelDocExtension.GetPersistReference3"
                        ),
                        "output_interface_kind": "IBody2",
                        "output_domain": {
                            "proof_api": (
                                "IModelDocExtension."
                                "GetObjectByPersistReference3"
                            ),
                            "status": 0,
                            "interface_kind": "IBody2",
                            "expected_owner_document_path": document_path,
                        },
                        "state": "observed",
                        "read_errors": {},
                    },
                    "read_errors": {},
                }
            )
        body["native_identity"] = {
            "owner_scope": "component_document",
            "owner_document_path": document_path,
            "state": "observed",
            "part_body_reference_count": len(distinct_references),
            "read_errors": {},
            "observation_errors": [],
            "face_body_observations": observations,
        }
    return raw


class PipelineVerifyHandlerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = hermetic.HermeticNativeFixture()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.directory = tempfile.TemporaryDirectory(prefix="verify-generate-pair-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.store = ArtifactStore(self.root / "cas")
        capture_source, _native = self.fixture._prepare_and_capture()
        capture_executor = digest({"executor": "capture-tool"})
        self.capture_ref = self.store.seal_tree(
            capture_source,
            kind="stage_output:capture",
            producer_run_id="capture-run",
            producer_stage="capture",
            producer_tool_sha256=capture_executor,
        )
        self.capture_root = self.store.materialize(self.capture_ref, self.root / "capture")
        self.frozen_request = read_data(self.capture_root / "input/request.json")
        self.verify_attempt = 0
        self.generate_executor = tool_record(role="portable")["source_sha256"]
        self.generator_command = StageCommand.create(
            command_id="generate-command",
            run_id="generate-run",
            stage="generate",
            idempotency_key="generate-command",
            request={
                "schema_version": GENERATE_REQUEST_SCHEMA,
                "capture_artifact_id": self.capture_ref.artifact_id,
                "output_contract": GENERATE_OUTPUT_CONTRACT,
            },
            input_artifacts=(self.capture_ref,),
            config_snapshot_sha256=str(self.frozen_request["platform_config_digest"]),
            contract_sha256=CONTRACT_SHA256,
            policy_sha256=check_policy_digest(),
            expected_executor_sha256=self.generate_executor,
            predecessor_decision_id="capture-decision",
        )
        generated = GenerateHandler(self.store).execute(
            self.generator_command,
            self._claim(
                self.generator_command,
                owner=digest({"process": "generate"}),
                executor=self.generate_executor,
            ),
            StageWorkspace(
                root=self.root / "generate-attempt",
                inputs={self.capture_ref.artifact_id: self.capture_root},
            ),
            effects=NoExternalEffects(),
        )
        self.generate_source = Path(generated.candidate_root)
        self.generate_ref = self.store.seal_tree(
            self.generate_source,
            kind="stage_output:generate",
            producer_run_id=self.generator_command.run_id,
            producer_stage="generate",
            producer_tool_sha256=self.generate_executor,
        )
        self.generate_root = self.store.materialize(self.generate_ref, self.root / "generate")

    @staticmethod
    def _claim(
        command: StageCommand,
        *,
        owner: str,
        executor: str,
        fence: int = 1,
    ) -> ExecutionClaim:
        return ExecutionClaim(
            claim_id=f"claim-{command.command_id}-{fence}",
            command_id=command.command_id,
            fence=fence,
            owner_sha256=owner,
            executor_sha256=executor,
            lane_key=None,
            expires_at=datetime(2026, 10, 25, tzinfo=UTC) + timedelta(hours=1),
        )

    def _verify_request(
        self,
        generate_ref=None,
        capture_ref=None,
        capture_root=None,
    ) -> tuple[StageCommand, ExecutionClaim, StageWorkspace]:
        generate_ref = generate_ref or self.generate_ref
        capture_ref = capture_ref or self.capture_ref
        capture_root = capture_root or self.capture_root
        verifier = hermetic._portable_tool()
        verifier_sha256 = verifier["source_sha256"]
        command = StageCommand.create(
            command_id="verify-command",
            run_id="verify-run",
            stage="verify",
            idempotency_key="verify-command",
            request={
                "capture_artifact_id": capture_ref.artifact_id,
                "generate_artifact_id": generate_ref.artifact_id,
                "output_contract": OUTPUT_CONTRACT,
                "purpose": self.frozen_request["purpose"],
                "schema_version": REQUEST_SCHEMA,
            },
            input_artifacts=(generate_ref, capture_ref),
            config_snapshot_sha256=str(self.frozen_request["platform_config_digest"]),
            contract_sha256=CONTRACT_SHA256,
            policy_sha256=check_policy_digest(),
            expected_executor_sha256=verifier_sha256,
            predecessor_decision_id="generate-decision",
        )
        claim = self._claim(
            command,
            owner=digest({"process": "verify"}),
            executor=verifier_sha256,
        )
        self.verify_attempt += 1
        if generate_ref == self.generate_ref:
            generate_root = self.generate_root
        else:
            variant = self.root / f"variant-generate-{generate_ref.artifact_id[:8]}"
            generate_root = variant if variant.exists() else self.store.materialize(generate_ref, variant)
        workspace = StageWorkspace(
            root=self.root / f"verify-{generate_ref.artifact_id[:8]}-{self.verify_attempt}",
            inputs={
                generate_ref.artifact_id: generate_root,
                capture_ref.artifact_id: capture_root,
            },
        )
        return command, claim, workspace

    def _run_verify(self, generate_ref=None):
        command, claim, workspace = self._verify_request(generate_ref)
        execution = VerifyHandler(
            identity_provider=hermetic._portable_tool,
            clock=lambda: datetime(2026, 10, 25, tzinfo=UTC),
        ).execute(command, claim, workspace, effects=NoExternalEffects())
        output = Path(execution.candidate_root)
        return (
            command,
            execution,
            read_data(output / CERTIFICATE_FILENAME),
            read_data(output / EXECUTION_RECEIPT_FILENAME),
            (output / CERTIFICATE_FILENAME).read_bytes(),
            command.input_artifacts[0],
            claim,
        )

    def _bundle_report(self, generate_ref):
        from description_pipeline.verification.solidworks_urdf import evaluate_bundle

        command, _claim, workspace = self._verify_request(generate_ref)
        request = pipeline_verify.VerifyDomainRequest(
            generate_artifact=command.input_artifacts[0],
            capture_artifact=command.input_artifacts[1],
            expected_verifier_sha256=command.expected_executor_sha256,
            contract_sha256=command.contract_sha256,
            policy_sha256=command.policy_sha256,
            config_snapshot_sha256=command.config_snapshot_sha256,
            purpose="engineering",
        )
        prepared = pipeline_verify._prepare(
            request,
            workspace=workspace,
            command=command,
            identity_provider=hermetic._portable_tool,
        )
        view = pipeline_verify._evaluation_view(
            workspace.root / "bundle-view",
            generate=prepared.generate,
            capture=prepared.capture,
            contract_sha256=command.contract_sha256,
            policy_sha256=command.policy_sha256,
        )
        return evaluate_bundle(view)

    def test_native_face_partition_reaches_independent_verify(self):
        result = self._run_verify()
        self.assertEqual(result[1].outcome, "completed")
        appearance = next(
            row
            for row in result[2]["checks"]
            if row["id"] == "appearance.native"
        )
        self.assertTrue(appearance["passed"])
        self.assertGreaterEqual(appearance["details"]["groups"], 4)

    def test_independent_appearance_rejects_an_unmapped_state_entity(self):
        from description_pipeline.verification.native_appearance import _resolved_faces

        raw = hermetic._fixture_appearance_raw(
            "arm-1",
            {
                "source_document_path": "C:/cad/arm.SLDPRT",
                "referenced_configuration": "Painted",
            },
            [
                {
                    "body_type": 0,
                    "body_index": 0,
                    "face_index": 0,
                }
            ],
        )
        owner = raw["state_render_materials"][-1]
        owner["call_state"] = "observed"
        owner["native_render_material_count"] = 1
        owner["mapping_errors"] = []
        owner["materials"] = [
            {
                "material_index": 0,
                "linked_display_states": ["Default_Display_State"],
                "entities": ["unbound-entity"],
                "entities_state": "observed",
                "entity_records": [
                    _appearance_entity_record(reference=(7, 8, 9))
                ],
                "primary_color": 0x00AABBCC,
                "transparency": 0.0,
                "color_form": 1,
                "texture_filename": "",
                "surface_finish_file": "",
                "read_errors": {},
            }
        ]
        with self.assertRaisesRegex(
            PipelineError,
            "has no native reference-index binding",
        ):
            _resolved_faces(
                raw,
                configuration="Painted",
                where="capture",
            )

    def test_independent_color_encodings_share_one_observed_rgba(self):
        from description_pipeline.verification.native_appearance import _resolved_faces

        for owner_red, should_pass in ((57 / 255.0, True), (58 / 255.0, False)):
            with self.subTest(owner_red=owner_red):
                raw = hermetic._fixture_appearance_raw(
                    "arm-1",
                    {
                        "source_document_path": "C:/cad/arm.SLDPRT",
                        "referenced_configuration": "Painted",
                    },
                    [{"body_type": 0, "body_index": 0, "face_index": 0}],
                )
                owner_channel = (
                    math.nextafter(owner_red, -math.inf)
                    if should_pass
                    else owner_red
                )
                face = raw["bodies"][0]["faces"][0]["appearance"]
                face["material_property_values"] = [
                    owner_channel,
                    owner_channel,
                    owner_channel,
                    1.0,
                    1.0,
                    0.5,
                    0.3125,
                    0.0,
                    0.0,
                ]
                owner = raw["state_render_materials"][-1]
                owner["call_state"] = "observed"
                owner["native_render_material_count"] = 1
                owner["mapping_errors"] = []
                owner["native_reference_index"] = []
                owner["materials"] = [
                    {
                        "material_index": 0,
                        "linked_display_states": ["Default_Display_State"],
                        "entities": ["face"],
                        "entities_state": "observed",
                        "entity_records": [
                            _appearance_entity_record(
                                reference=(1, 2, 3),
                                native_interface_kinds=["IFace2", "IEntity"],
                            )
                        ],
                        "primary_color": 0x393939,
                        "transparency": 0.0,
                        "color_form": 1,
                        "texture_filename": "",
                        "surface_finish_file": "",
                        "read_errors": {},
                    }
                ]
                if should_pass:
                    resolved = _resolved_faces(
                        raw,
                        configuration="Painted",
                        where="capture",
                    )
                    self.assertEqual(
                        resolved["faces"][(0, 0, 0)]["rgba"],
                        (
                            owner_channel,
                            owner_channel,
                            owner_channel,
                            1.0,
                        ),
                    )
                    self.assertEqual(
                        {
                            source["kind"]
                            for source in resolved["faces"][(0, 0, 0)]["source"]["owners"]
                        },
                        {"owner", "display_state_render_material"},
                    )
                else:
                    with self.assertRaisesRegex(
                        PipelineError, "conflicting appearances"
                    ):
                        _resolved_faces(
                            raw,
                            configuration="Painted",
                            where="capture",
                        )

    def test_independent_body_target_uses_its_exact_face_footprint(self):
        from description_pipeline.verification.native_appearance import _resolved_faces

        raw = hermetic._fixture_appearance_raw(
            "arm-1",
            {
                "source_document_path": "C:/cad/arm.SLDPRT",
                "referenced_configuration": "Painted",
            },
            [
                {"body_type": 0, "body_index": 0, "face_index": 0},
                {"body_type": 0, "body_index": 0, "face_index": 1},
            ],
        )
        _attach_body_witnesses(raw)
        owner = raw["state_render_materials"][-1]
        owner["call_state"] = "observed"
        owner["native_render_material_count"] = 1
        owner["mapping_errors"] = []
        owner["native_reference_index"] = []
        owner["materials"] = [
            {
                "material_index": 0,
                "linked_display_states": ["Default_Display_State"],
                "entities": ["body"],
                "entities_state": "observed",
                "entity_records": [
                    _appearance_entity_record(
                        reference=(1, 2, 3),
                        native_interface_kinds=["IBody2"],
                    )
                ],
                "primary_color": 0x0000FF00,
                "transparency": 0.0,
                "color_form": 1,
                "texture_filename": "",
                "surface_finish_file": "",
                "read_errors": {},
            }
        ]
        for base_owner in (raw["document"], raw["occurrence"]):
            base_owner["material_property_values"] = None
            base_owner["has_material_property_values"] = False
        for body in raw["bodies"]:
            body["appearance"]["material_property_values"] = None
            body["appearance"]["has_material_property_values"] = False
            for face in body["faces"]:
                face["appearance"]["material_property_values"] = None
                face["appearance"]["has_material_property_values"] = False

        resolved = _resolved_faces(
            raw,
            configuration="Painted",
            where="capture",
        )
        self.assertEqual(
            {row["rgba"] for row in resolved["faces"].values()},
            {(0.0, 1.0, 0.0, 1.0)},
        )
        self.assertEqual(
            {
                tuple(row["source"]["scopes"])
                for row in resolved["faces"].values()
            },
            {("body",)},
        )

        for label in ("wrong_component", "owner", "api", "interface"):
            with self.subTest(label=label):
                malformed_observation = copy.deepcopy(raw)
                body = malformed_observation["bodies"][0]
                conversion = body["native_identity"][
                    "face_body_observations"
                ][1]["conversion"]
                if label == "wrong_component":
                    body["component"] = "other-1"
                elif label == "owner":
                    conversion["receiver"][
                        "owner_document_path"
                    ] = "C:/cad/other.SLDPRT"
                elif label == "api":
                    conversion["api"] = "IModelDocExtension.GetCorresponding2"
                else:
                    conversion["output_interface_kind"] = "IFace2"
                with self.assertRaises(PipelineError) as malformed_caught:
                    _resolved_faces(
                        malformed_observation,
                        configuration="Painted",
                        where="capture",
                    )
                self.assertTrue(
                    "another component" in str(malformed_caught.exception)
                    or "incomplete or conflicting face-body witnesses"
                    in str(malformed_caught.exception)
                    or "native object relation evidence is unresolved"
                    in str(malformed_caught.exception)
                )

        duplicate_body = copy.deepcopy(raw)
        duplicate = copy.deepcopy(duplicate_body["bodies"][0])
        duplicate["native_identity"]["face_body_observations"][0][
            "persistent_reference"
        ] = [4, 5, 6]
        duplicate_body["bodies"].append(duplicate)
        with self.assertRaisesRegex(
            PipelineError,
            "repeats a body appearance owner",
        ):
            _resolved_faces(
                duplicate_body,
                configuration="Painted",
                where="capture",
            )

        no_reference_fragment = copy.deepcopy(raw)
        failed_body = copy.deepcopy(no_reference_fragment["bodies"][0])
        failed_body["body_index"] = 1
        failed_body["faces"] = [copy.deepcopy(failed_body["faces"][0])]
        failed_body["native_identity"] = {
            "owner_scope": "component_document",
            "owner_document_path": "C:/cad/arm.SLDPRT",
            "state": "error",
            "part_body_reference_count": 0,
            "read_errors": {
                "body_reference": {
                    "code": "cad_appearance_part_body_missing"
                }
            },
            "observation_errors": [
                {
                    "face_index": 0,
                    "read_errors": {
                        "body": {
                            "code": "cad_appearance_part_body_missing"
                        }
                    },
                }
            ],
            "face_body_observations": [
                {
                    "face_index": 0,
                    "persistent_reference": None,
                    "conversion": {
                        "state": "error",
                        "read_errors": {
                            "body": {
                                "code": "cad_appearance_part_body_missing"
                            }
                        },
                    },
                    "read_errors": {
                        "body": {
                            "code": "cad_appearance_part_body_missing"
                        }
                    },
                }
            ],
        }
        no_reference_fragment["bodies"].append(failed_body)
        with self.assertRaisesRegex(
            PipelineError,
            "native object relation evidence is unresolved",
        ):
            _resolved_faces(
                no_reference_fragment,
                configuration="Painted",
                where="capture",
            )

        wrong_interface = copy.deepcopy(raw)
        wrong_entity = wrong_interface["state_render_materials"][-1][
            "materials"
        ][0]["entity_records"][0]
        wrong_entity["native_interface_kinds"] = ["IFeature"]
        wrong_entity["native_identity"]["interface_kinds"] = ["IFeature"]
        with self.assertRaisesRegex(
            PipelineError,
            "public interfaces do not match",
        ):
            _resolved_faces(
                wrong_interface,
                configuration="Painted",
                where="capture",
            )

        cross_fragment = hermetic._fixture_appearance_raw(
            "arm-1",
            {
                "source_document_path": "C:/cad/arm.SLDPRT",
                "referenced_configuration": "Painted",
            },
            [
                {"body_type": 0, "body_index": 0, "face_index": 0},
                {"body_type": 0, "body_index": 0, "face_index": 1},
                {"body_type": 0, "body_index": 1, "face_index": 0},
            ],
        )
        _attach_body_witnesses(cross_fragment)
        conflicting_observation = copy.deepcopy(
            cross_fragment["bodies"][0]["native_identity"][
                "face_body_observations"
            ][0]
        )
        conflicting_observation["persistent_reference"] = [4, 5, 6]
        cross_fragment["bodies"][0]["native_identity"][
            "face_body_observations"
        ].append(conflicting_observation)
        cross_fragment["bodies"][0]["native_identity"][
            "part_body_reference_count"
        ] = 2
        cross_owner = cross_fragment["state_render_materials"][-1]
        cross_owner["call_state"] = "observed"
        cross_owner["native_render_material_count"] = 1
        cross_owner["mapping_errors"] = []
        cross_owner["native_reference_index"] = []
        cross_owner["materials"] = copy.deepcopy(owner["materials"])
        for base_owner in (
            cross_fragment["document"],
            cross_fragment["occurrence"],
        ):
            base_owner["material_property_values"] = None
            base_owner["has_material_property_values"] = False
        for body in cross_fragment["bodies"]:
            body["appearance"]["material_property_values"] = None
            body["appearance"]["has_material_property_values"] = False
            for face in body["faces"]:
                face["appearance"]["material_property_values"] = None
                face["appearance"]["has_material_property_values"] = False
        with self.assertRaisesRegex(
            PipelineError,
            "native object relation evidence is unresolved",
        ):
            _resolved_faces(
                cross_fragment,
                configuration="Painted",
                where="capture",
            )

    def test_independent_body_evidence_accepts_legal_shared_ownership(self):
        from description_pipeline.verification.native_appearance import _resolved_faces

        def attach_material(raw):
            owner = raw["state_render_materials"][-1]
            owner["call_state"] = "observed"
            owner["native_render_material_count"] = 1
            owner["mapping_errors"] = []
            owner["native_reference_index"] = []
            owner["materials"] = [
                {
                    "material_index": 0,
                    "linked_display_states": ["Default_Display_State"],
                    "entities": ["body"],
                    "entities_state": "observed",
                    "entity_records": [
                        _appearance_entity_record(
                            reference=(1, 2, 3),
                            native_interface_kinds=["IBody2"],
                        )
                    ],
                    "primary_color": 0x0000FF00,
                    "transparency": 0.0,
                    "color_form": 1,
                    "texture_filename": "",
                    "surface_finish_file": "",
                    "read_errors": {},
                }
            ]
            for base_owner in (raw["document"], raw["occurrence"]):
                base_owner["material_property_values"] = None
                base_owner["has_material_property_values"] = False
            for body in raw["bodies"]:
                body["appearance"]["material_property_values"] = None
                body["appearance"]["has_material_property_values"] = False
                for face in body["faces"]:
                    face["appearance"]["material_property_values"] = None
                    face["appearance"]["has_material_property_values"] = False

        multi_body = hermetic._fixture_appearance_raw(
            "arm-1",
            {
                "source_document_path": "C:/cad/arm.SLDPRT",
                "referenced_configuration": "Painted",
            },
            [
                {"body_type": 0, "body_index": 0, "face_index": 0},
                {"body_type": 0, "body_index": 0, "face_index": 1},
            ],
        )
        _attach_body_witnesses(
            multi_body,
            {
                (0, 0, 0): (1, 2, 3),
                (0, 0, 1): (4, 5, 6),
            },
        )
        attach_material(multi_body)
        multi_owner = multi_body["state_render_materials"][-1]
        second_material = copy.deepcopy(multi_owner["materials"][0])
        second_material["material_index"] = 1
        second_material["primary_color"] = 0x000000FF
        second_material["entity_records"][0][
            "persistent_reference"
        ] = [4, 5, 6]
        second_material["entity_records"][0]["native_identity"][
            "interface_kinds"
        ] = ["IBody2"]
        multi_owner["materials"].append(second_material)
        multi_owner["native_render_material_count"] = 2
        multi_resolved = _resolved_faces(
            multi_body,
            configuration="Painted",
            where="capture",
        )
        self.assertEqual(
            multi_resolved["faces"][(0, 0, 0)]["rgba"],
            (0.0, 1.0, 0.0, 1.0),
        )
        self.assertEqual(
            multi_resolved["faces"][(0, 0, 1)]["rgba"],
            (1.0, 0.0, 0.0, 1.0),
        )

        cross_fragment = hermetic._fixture_appearance_raw(
            "arm-1",
            {
                "source_document_path": "C:/cad/arm.SLDPRT",
                "referenced_configuration": "Painted",
            },
            [
                {"body_type": 0, "body_index": 0, "face_index": 0},
                {"body_type": 0, "body_index": 0, "face_index": 1},
                {"body_type": 0, "body_index": 1, "face_index": 0},
            ],
        )
        _attach_body_witnesses(cross_fragment)
        attach_material(cross_fragment)
        cross_resolved = _resolved_faces(
            cross_fragment,
            configuration="Painted",
            where="capture",
        )
        self.assertEqual(
            {
                tuple(row["source"]["scopes"])
                for row in cross_resolved["faces"].values()
            },
            {("body",)},
        )

    def test_independent_appearance_scopes_mapping_errors_to_participants(
        self,
    ):
        from description_pipeline.verification.native_appearance import _resolved_faces

        mapping_error = {
            "owner_scope": "component_document",
            "owner_document_path": "C:/cad/arm.SLDPRT",
            "component": "arm-1",
            "entity_kind": "faces",
            "target_kind": "face",
            "identity": {
                "component": "arm-1",
                "body_type": 0,
                "body_index": 0,
                "face_index": 0,
            },
            "persistent_reference": None,
            "native_interface_kind": "IEntity",
            "error": {"code": "cad_appearance_persistent_reference_invalid"},
        }
        unrelated_error = copy.deepcopy(mapping_error)
        unrelated_error["identity"]["face_index"] = 1
        empty = hermetic._fixture_appearance_raw(
            "arm-1",
            {
                "source_document_path": "C:/cad/arm.SLDPRT",
                "referenced_configuration": "Painted",
            },
            [
                {"body_type": 0, "body_index": 0, "face_index": 0},
                {"body_type": 0, "body_index": 0, "face_index": 1},
            ],
        )
        resolved = _resolved_faces(
            empty,
            configuration="Painted",
            where="capture",
        )
        self.assertIn((0, 0, 0), resolved["faces"])
        for field, value in (
            ("mapping_errors", [unrelated_error]),
            ("native_reference_index", [{"derived": "footprint"}]),
        ):
            with self.subTest(field=field):
                malformed = copy.deepcopy(empty)
                malformed["state_render_materials"][-1][field] = value
                with self.assertRaisesRegex(
                    PipelineError,
                    "carries a derived footprint index or mapping answer",
                ):
                    _resolved_faces(
                        malformed,
                        configuration="Painted",
                        where="capture",
                    )

        participating = copy.deepcopy(empty)
        for base_owner in (
            participating["document"],
            participating["occurrence"],
        ):
            base_owner["material_property_values"] = None
            base_owner["has_material_property_values"] = False
        for body in participating["bodies"]:
            body["appearance"]["material_property_values"] = None
            body["appearance"]["has_material_property_values"] = False
            for face in body["faces"]:
                if face["face_index"] == 0:
                    face["appearance"]["material_property_values"] = None
                    face["appearance"]["has_material_property_values"] = False
        owner = participating["state_render_materials"][-1]
        owner["call_state"] = "observed"
        owner["native_render_material_count"] = 1
        owner["native_reference_index"] = []
        owner["materials"] = [
            {
                "material_index": 0,
                "linked_display_states": ["Default_Display_State"],
                "entities": ["face"],
                "entities_state": "observed",
                "entity_records": [
                    _appearance_entity_record(
                        native_interface_kinds=["IFace2", "IEntity"]
                    )
                ],
                "primary_color": 0x000000FF,
                "transparency": 0.0,
                "color_form": 1,
                "texture_filename": "",
                "surface_finish_file": "",
                "read_errors": {},
            }
        ]
        resolved = _resolved_faces(
            participating,
            configuration="Painted",
            where="capture",
        )
        self.assertIn((0, 0, 0), resolved["faces"])

        unresolved = copy.deepcopy(participating)
        unresolved["bodies"][0]["faces"][0]["appearance"][
            "native_identity"
        ]["persistent_reference"] = [9, 9, 9]
        with self.assertRaisesRegex(
            PipelineError,
            "has no native reference-index binding",
        ):
            _resolved_faces(
                unresolved,
                configuration="Painted",
                where="capture",
            )

    def test_independent_appearance_binds_geometry_component_and_document(
        self,
    ):
        from description_pipeline.verification.native_appearance import _resolved_faces

        raw = hermetic._fixture_appearance_raw(
            "arm-1",
            {
                "source_document_path": "C:/cad/arm.SLDPRT",
                "referenced_configuration": "Painted",
            },
            [{"body_type": 0, "body_index": 0, "face_index": 0}],
        )
        _resolved_faces(
            raw,
            configuration="Painted",
            where="capture",
            expected_component="arm-1",
            expected_document_path="C:/cad/arm.SLDPRT",
        )
        for mutation, message in (
            (("component", "other-1"), "names another component"),
            (
                ("source_document_path", "C:/cad/other.SLDPRT"),
                "names another source document",
            ),
        ):
            malformed = copy.deepcopy(raw)
            malformed[mutation[0]] = mutation[1]
            with self.subTest(field=mutation[0]), self.assertRaisesRegex(
                PipelineError,
                message,
            ):
                _resolved_faces(
                    malformed,
                    configuration="Painted",
                    where="capture",
                    expected_component="arm-1",
                    expected_document_path="C:/cad/arm.SLDPRT",
                )

    def test_independent_appearance_rejects_ambiguous_face_aliases(self):
        from description_pipeline.verification.native_appearance import _resolved_faces

        raw = hermetic._fixture_appearance_raw(
            "arm-1",
            {
                "source_document_path": "C:/cad/arm.SLDPRT",
                "referenced_configuration": "Painted",
            },
            [
                {"body_type": 0, "body_index": 0, "face_index": 0},
                {"body_type": 0, "body_index": 0, "face_index": 1},
            ],
        )
        owner = raw["state_render_materials"][-1]
        owner["call_state"] = "observed"
        owner["native_render_material_count"] = 1
        owner["native_reference_index"] = []
        raw["bodies"][0]["faces"][1]["appearance"]["native_identity"][
            "persistent_reference"
        ] = [1, 2, 3]
        owner["materials"] = [
            {
                "material_index": 0,
                "linked_display_states": ["Default_Display_State"],
                "entities": ["ambiguous"],
                "entities_state": "observed",
                "entity_records": [
                    _appearance_entity_record(
                        native_interface_kinds=["IFace2", "IEntity"]
                    )
                ],
                "primary_color": 0x000000FF,
                "transparency": 0.0,
                "color_form": 1,
                "texture_filename": "",
                "surface_finish_file": "",
                "read_errors": {},
            }
        ]
        with self.assertRaisesRegex(
            PipelineError,
            "native reference aliases multiple targets",
        ):
            _resolved_faces(
                raw,
                configuration="Painted",
                where="capture",
            )

    def test_independent_appearance_verifies_reference_evidence(self):
        from description_pipeline.verification.native_appearance import _resolved_faces

        raw = hermetic._fixture_appearance_raw(
            "arm-1",
            {
                "source_document_path": "C:/cad/arm.SLDPRT",
                "referenced_configuration": "Painted",
            },
            [{"body_type": 0, "body_index": 0, "face_index": 0}],
        )
        owner = raw["state_render_materials"][-1]
        owner["call_state"] = "observed"
        owner["native_render_material_count"] = 1
        owner["native_reference_index"] = []
        owner["materials"] = [
            {
                "material_index": 0,
                "linked_display_states": ["Default_Display_State"],
                "entities": ["feature"],
                "entities_state": "observed",
                "entity_records": [
                    _appearance_entity_record()
                    | {"native_interface_kinds": ["IFace2", "IEntity"]}
                ],
                "primary_color": 0x000000FF,
                "transparency": 0.0,
                "color_form": 1,
                "texture_filename": "",
                "surface_finish_file": "",
                "read_errors": {},
            }
        ]
        for base_owner in (raw["document"], raw["occurrence"]):
            base_owner["material_property_values"] = None
            base_owner["has_material_property_values"] = False
        for body in raw["bodies"]:
            body["appearance"]["material_property_values"] = None
            body["appearance"]["has_material_property_values"] = False
            for face in body["faces"]:
                face["appearance"]["material_property_values"] = None
                face["appearance"]["has_material_property_values"] = False
        _resolved_faces(
            raw,
            configuration="Painted",
            where="capture",
        )
        for mutation in (
            lambda conversion: conversion["receiver"].update(
                owner_scope="top_assembly"
            ),
            lambda conversion: conversion.update(
                api="IComponent2.GetCorrespondingEntity"
            ),
            lambda conversion: conversion["output_domain"].update(
                proof_api="ISldWorks.IsSame",
                status=1,
            ),
            lambda conversion: conversion["output_domain"].update(
                status=False
            ),
        ):
            malformed = copy.deepcopy(raw)
            conversion = malformed["bodies"][0]["faces"][0]["appearance"][
                "native_identity"
            ]["conversion"]
            mutation(conversion)
            with self.assertRaisesRegex(
                PipelineError,
                "cad_appearance_render_conversion_unproven",
            ):
                _resolved_faces(
                    malformed,
                    configuration="Painted",
                    where="capture",
                )

        for failure, expected_message in (
            ("wrong_owner", "binding context is not its actual owner"),
            (
                "conversion_in_owner",
                "is not bound to its actual owner document",
            ),
            (
                "missing_owner_reference",
                "is not bound to its actual owner document",
            ),
        ):
            with self.subTest(failure=failure):
                malformed = copy.deepcopy(raw)
                entity = malformed["state_render_materials"][-1]["materials"][
                    0
                ]["entity_records"][0]
                if failure == "wrong_owner":
                    entity["binding_context"]["component"] = "other-1"
                elif failure == "conversion_in_owner":
                    entity["binding_context"]["api"] = (
                        "IComponent2.GetCorrespondingEntity"
                    )
                else:
                    entity["persistent_reference"] = None
                with self.assertRaisesRegex(
                    PipelineError,
                    expected_message,
                ):
                    _resolved_faces(
                        malformed,
                        configuration="Painted",
                        where="capture",
                    )

    def test_independent_appearance_conversion_is_target_specific(self):
        from description_pipeline.verification.native_appearance import (
            _validate_index_conversion,
        )

        base = _appearance_index_record(
            reference=(1, 2, 3),
            target={
                "kind": "face",
                "component": "arm-1",
                "body_type": 0,
                "body_index": 0,
                "face_index": 0,
            },
        )
        for target_kind, api, interface, source_api in (
            (
                "body",
                "IFace2.GetBody+"
                "IModelDocExtension.GetPersistReference3",
                "IBody2",
                "IComponent2.GetBodies3/IBody2.GetFaces",
            ),
            (
                "face",
                "IModelDocExtension.GetCorrespondingEntity2",
                "IFace2",
                "IComponent2.GetBodies3/IBody2.GetFaces",
            ),
            (
                "feature",
                "IFace2.GetFeature+"
                "IModelDocExtension.GetPersistReference3",
                "IFeature",
                "IComponent2.GetBodies3/IBody2.GetFaces",
            ),
        ):
            indexed = copy.deepcopy(base)
            target = indexed["target"]
            target["kind"] = target_kind
            if target_kind == "body":
                target["persistent_reference_sha256"] = hashlib.sha256(
                    bytes(
                        item & 0xFF
                        for item in indexed["persistent_reference"]
                    )
                ).hexdigest()
                target["faces"] = [
                    {
                        "body_type": target.pop("body_type"),
                        "body_index": target.pop("body_index"),
                        "face_index": target.pop("face_index", 0),
                    }
                ]
                target["occurrence_bodies"] = [[0, 0]]
            elif target_kind == "feature":
                target.update(
                    {
                        "name": "Boss-Extrude1",
                        "type_name": "Extrusion",
                        "persistent_reference": [4, 5, 6],
                        "faces": [
                            {
                                "body_type": 0,
                                "body_index": 0,
                                "face_index": 0,
                            }
                        ],
                    }
                )
                target.pop("face_index", None)
            indexed["conversion"].update(
                {
                    "api": api,
                    "output_interface_kind": interface,
                }
            )
            indexed["native_interface_kind"] = (
                "IBody2"
                if target_kind == "body"
                else "IEntity"
                if target_kind == "face"
                else None
            )
            indexed["conversion"]["output_domain"]["interface_kind"] = (
                interface
            )
            indexed["conversion"]["input_context"]["source_api"] = source_api
            _validate_index_conversion(
                indexed,
                owner_document_path="C:/cad/arm.SLDPRT",
                component="arm-1",
                path="capture",
            )
            indexed["conversion"]["api"] = (
                "IComponent2.GetCorrespondingEntity"
            )
            with self.assertRaisesRegex(
                PipelineError,
                "cad_appearance_render_conversion_unproven",
            ):
                _validate_index_conversion(
                    indexed,
                    owner_document_path="C:/cad/arm.SLDPRT",
                    component="arm-1",
                    path="capture",
                )

    def test_independent_appearance_merges_only_one_feature_footprint(self):
        from description_pipeline.verification.native_appearance import _resolved_faces

        for feature_names, should_pass in (
            (("Boss-Extrude1", "Boss-Extrude1"), True),
            (("Boss-Extrude1", "Cut-Extrude1"), False),
        ):
            with self.subTest(feature_names=feature_names):
                raw = hermetic._fixture_appearance_raw(
                    "arm-1",
                    {
                        "source_document_path": "C:/cad/arm.SLDPRT",
                        "referenced_configuration": "Painted",
                    },
                    [
                        {"body_type": 0, "body_index": 0, "face_index": 0},
                        {"body_type": 0, "body_index": 0, "face_index": 1},
                    ],
                )
                owner = raw["state_render_materials"][-1]
                owner["call_state"] = "observed"
                owner["native_render_material_count"] = 1
                owner["native_reference_index"] = []
                if not should_pass:
                    raw["bodies"][0]["faces"][1]["appearance"][
                        "native_identity"
                    ]["feature"]["name"] = feature_names[1]
                owner["materials"] = [
                    {
                        "material_index": 0,
                        "linked_display_states": ["Default_Display_State"],
                        "entities": ["feature"],
                        "entities_state": "observed",
                        "entity_records": [
                            _appearance_entity_record(
                                reference=(4, 5, 6),
                                native_interface_kinds=["IFeature"]
                            )
                        ],
                        "primary_color": 0x000000FF,
                        "transparency": 0.0,
                        "color_form": 1,
                        "texture_filename": "",
                        "surface_finish_file": "",
                        "read_errors": {},
                    }
                ]
                for base_owner in (
                    raw["document"],
                    raw["occurrence"],
                ):
                    base_owner["material_property_values"] = None
                    base_owner["has_material_property_values"] = False
                for body in raw["bodies"]:
                    body["appearance"]["material_property_values"] = None
                    body["appearance"]["has_material_property_values"] = False
                    for face in body["faces"]:
                        face["appearance"]["material_property_values"] = None
                        face["appearance"]["has_material_property_values"] = False
                if should_pass:
                    resolved = _resolved_faces(
                        raw,
                        configuration="Painted",
                        where="capture",
                    )
                    self.assertEqual(
                        {
                            face["rgba"]
                            for face in resolved["faces"].values()
                        },
                        {(1.0, 0.0, 0.0, 1.0)},
                    )
                    incomplete = copy.deepcopy(raw)
                    incomplete["bodies"][0]["faces"][0]["appearance"][
                        "native_identity"
                    ]["feature"]["read_errors"] = {
                        "name": {
                            "code": "cad_member_missing",
                            "message": "missing",
                            "detail": {},
                        }
                    }
                    with self.assertRaisesRegex(
                        PipelineError,
                        "native object relation evidence is unresolved",
                    ):
                        _resolved_faces(
                            incomplete,
                            configuration="Painted",
                            where="capture",
                        )
                    missing_feature_reference = copy.deepcopy(raw)
                    missing_feature_reference["bodies"][0]["faces"][0][
                        "appearance"
                    ]["native_identity"]["feature"][
                        "persistent_reference"
                    ] = None
                    missing_feature_reference["bodies"][0]["faces"][0][
                        "appearance"
                    ]["native_identity"]["feature"]["conversion"] = None
                    with self.assertRaisesRegex(
                        PipelineError,
                        "native object relation evidence is unresolved",
                    ):
                        _resolved_faces(
                            missing_feature_reference,
                            configuration="Painted",
                            where="capture",
                        )
                else:
                    with self.assertRaisesRegex(
                        PipelineError,
                        "native object relation evidence is unresolved",
                    ):
                        _resolved_faces(
                            raw,
                            configuration="Painted",
                            where="capture",
                        )

    def test_independent_document_material_precedes_legacy_texture(self):
        from description_pipeline.verification.native_appearance import _resolved_faces

        raw = hermetic._fixture_appearance_raw(
            "arm-1",
            {
                "source_document_path": "C:/cad/arm.SLDPRT",
                "referenced_configuration": "Painted",
            },
            [
                {"body_type": 0, "body_index": 0, "face_index": 0},
                {"body_type": 0, "body_index": 0, "face_index": 1},
            ],
        )
        owner = raw["state_render_materials"][-1]
        owner["call_state"] = "observed"
        owner["native_render_material_count"] = 1
        owner["mapping_errors"] = []
        owner["materials"] = [
            {
                "material_index": 0,
                "linked_display_states": ["Default_Display_State"],
                "entities": ["document"],
                "entities_state": "observed",
                "entity_records": [
                    {
                        "entity_index": 0,
                        "native_object_type": "IModelDoc2",
                        "native_interface_kinds": ["IModelDoc2"],
                        "persistent_reference": None,
                        "binding_context": {
                            "owner_scope": "component_document",
                            "owner_document_path": "C:/cad/arm.SLDPRT",
                            "component": "arm-1",
                            "api": (
                                "IModelDoc2.GetPathName"
                                "+ConfigurationManager.ActiveConfiguration.Name"
                            ),
                        },
                        "native_identity": {
                            "kind": "document",
                            "qi_iid": "{B90793FB-EF3D-4B80-A5C4-99959CDB6CEB}",
                            "document_path": "C:/cad/arm.SLDPRT",
                            "active_configuration": "Painted",
                            "state": "observed",
                            "read_errors": {},
                        },
                        "read_errors": {},
                    }
                ],
                "primary_color": 0x000000FF,
                "transparency": 0.0,
                "color_form": 1,
                "texture_filename": "",
                "surface_finish_file": "",
                "read_errors": {},
            }
        ]
        for base_owner in (raw["document"], raw["occurrence"]):
            base_owner["material_property_values"] = None
            base_owner["has_material_property_values"] = False
        for body in raw["bodies"]:
            body["appearance"]["material_property_values"] = None
            body["appearance"]["has_material_property_values"] = False
            for face in body["faces"]:
                face["appearance"]["material_property_values"] = None
                face["appearance"]["has_material_property_values"] = False
        raw["document"]["texture"] = {
            "configuration": "Painted",
            "state": "present",
            "material_name": "",
        }
        resolved = _resolved_faces(
            raw,
            configuration="Painted",
            where="capture",
        )
        self.assertEqual(
            {
                face["rgba"]
                for face in resolved["faces"].values()
            },
            {(1.0, 0.0, 0.0, 1.0)},
        )
        for field, value in (
            ("qi_iid", "{wrong-iid}"),
            ("binding_api", "IModelDocExtension.GetCorrespondingEntity2"),
        ):
            malformed = copy.deepcopy(raw)
            entity = malformed["state_render_materials"][-1]["materials"][
                0
            ]["entity_records"][0]
            if field == "qi_iid":
                entity["native_identity"]["qi_iid"] = value
            else:
                entity["binding_context"]["api"] = value
            with self.subTest(field=field), self.assertRaises(PipelineError):
                _resolved_faces(
                    malformed,
                    configuration="Painted",
                    where="capture",
                )

    def test_independent_appearance_applies_part_and_face_scope_priority(self):
        from description_pipeline.verification.native_appearance import _resolved_faces

        case = read_data(PART_FACE_OVERRIDE_FIXTURE)
        resolved = _resolved_faces(
            copy.deepcopy(case["raw"]),
            configuration="Painted",
            where="capture",
        )
        self.assertEqual(
            {
                face: row["rgba"]
                for face, row in resolved["faces"].items()
            },
            {
                (0, 0, 0): (0.0, 0.0, 1.0, 1.0),
                (0, 0, 1): (1.0, 0.0, 0.0, 1.0),
            },
        )
        self.assertEqual(
            {
                face: row["source"]["scopes"]
                for face, row in resolved["faces"].items()
            },
            {
                (0, 0, 0): ["face"],
                (0, 0, 1): ["document"],
            },
        )

    def test_independent_assembly_override_beats_part_and_face(self):
        from description_pipeline.verification.native_appearance import _resolved_faces

        case = read_data(PART_FACE_OVERRIDE_FIXTURE)
        raw = copy.deepcopy(case["raw"])
        top = {
            "owner_document_path": "C:/cad/robot.SLDASM",
            "owner_scope": "top_assembly",
            "api": "IModelDocExtension.GetRenderMaterials2",
            "display_state_option": 3,
            "configuration": "Top Configuration",
            "display_state": "Top State",
            "display_states": ["Top State"],
            "display_states_state": "observed",
            "native_render_material_count_api": (
                "IModelDocExtension.GetRenderMaterialsCount2"
            ),
            "native_render_material_count": 1,
            "native_render_material_count_error": None,
            "call_state": "observed",
            "materials": [
                {
                    "material_index": 0,
                    "linked_display_states": ["Top State"],
                    "entities": ["top-occurrence"],
                    "entities_state": "observed",
                    "entity_records": [
                        _appearance_entity_record(
                            reference=(4, 5, 6),
                            owner_scope="top_assembly",
                            owner_document_path="C:/cad/robot.SLDASM",
                            native_interface_kinds=["IComponent2"],
                        )
                    ],
                    "primary_color": 0x0000FF00,
                    "transparency": 0.0,
                    "color_form": 1,
                    "texture_filename": "",
                    "surface_finish_file": "",
                    "read_errors": {},
                }
            ],
            "mapping_errors": [],
            "native_reference_index": [
                _appearance_index_record(
                    reference=(4, 5, 6),
                    target={"kind": "occurrence", "component": "arm-1"},
                    owner_scope="top_assembly",
                    owner_document_path="C:/cad/robot.SLDASM",
                )
            ],
            "read_errors": {},
        }
        raw["state_render_materials"].insert(0, top)
        resolved = _resolved_faces(
            raw,
            configuration="Painted",
            where="capture",
        )
        self.assertEqual(
            {
                row["rgba"]
                for row in resolved["faces"].values()
            },
            {(0.0, 1.0, 0.0, 1.0)},
        )
        self.assertEqual(
            {
                tuple(row["source"]["scopes"])
                for row in resolved["faces"].values()
            },
            {("occurrence",)},
        )
        non_occurrence = copy.deepcopy(raw)
        entity = non_occurrence["state_render_materials"][0]["materials"][
            0
        ]["entity_records"][0]
        entity["persistent_reference"] = [7, 8, 9]
        entity["native_interface_kinds"] = ["IFace2", "IEntity"]
        entity["native_identity"] = {
            "kind": "reference",
            "state": "observed",
            "proof_api": "IModelDocExtension.GetObjectByPersistReference3",
            "status": 0,
            "owner_document_path": "C:/cad/arm.SLDPRT",
            "interface_kinds": ["IFace2", "IEntity"],
            "read_errors": {},
        }
        non_occurrence["state_render_materials"][0][
            "native_reference_index"
        ] = [
            _appearance_index_record(
                reference=(7, 8, 9),
                target={
                    "kind": "face",
                    "component": "arm-1",
                    "body_type": 0,
                    "body_index": 0,
                    "face_index": 0,
                },
                owner_scope="top_assembly",
                owner_document_path="C:/cad/robot.SLDASM",
            )
        ]
        with self.assertRaisesRegex(
            PipelineError,
            "assembly-owned material target is not an occurrence",
        ):
            _resolved_faces(
                non_occurrence,
                configuration="Painted",
                where="capture",
            )

    def test_independent_appearance_accepts_null_with_zero_native_count(self):
        from description_pipeline.verification.native_appearance import _resolved_faces

        raw = hermetic._fixture_appearance_raw(
            "arm-1",
            {
                "source_document_path": "C:/cad/arm.SLDPRT",
                "referenced_configuration": "Painted",
            },
            [
                {
                    "body_type": 0,
                    "body_index": 0,
                    "face_index": 0,
                }
            ],
        )
        owner = raw["state_render_materials"][-1]
        owner["call_state"] = "null_result"
        owner["materials"] = None
        resolved = _resolved_faces(
            raw,
            configuration="Painted",
            where="capture",
        )
        self.assertEqual(len(resolved["faces"]), 1)

    def test_independent_appearance_uses_the_material_owners_display_state(self):
        from description_pipeline.verification.native_appearance import _resolved_faces

        raw = hermetic._fixture_appearance_raw(
            "arm-1",
            {
                "source_document_path": "C:/cad/arm.SLDPRT",
                "referenced_configuration": "Painted",
            },
            [
                {
                    "body_type": 0,
                    "body_index": 0,
                    "face_index": 0,
                }
            ],
        )
        owner = raw["state_render_materials"][0]
        owner["call_state"] = "observed"
        owner["native_render_material_count"] = 1
        owner["native_reference_index"] = [
            _appearance_index_record(
                reference=(4, 5, 6),
                target={"kind": "occurrence", "component": "arm-1"},
                owner_scope="top_assembly",
                owner_document_path="fixture-top.SLDASM",
            )
        ]
        owner["materials"] = [
            {
                "material_index": 0,
                "linked_display_states": ["Top Display State"],
                "entities": ["top-face"],
                "entities_state": "observed",
                "entity_records": [
                    _appearance_entity_record(
                        reference=(4, 5, 6),
                        owner_scope="top_assembly",
                        owner_document_path="fixture-top.SLDASM",
                        native_interface_kinds=["IComponent2"],
                    )
                ],
                "primary_color": 0x00AABBCC,
                "transparency": 0.0,
                "color_form": 1,
                "texture_filename": "",
                "surface_finish_file": "",
                "read_errors": {},
            }
        ]
        resolved = _resolved_faces(
            raw,
            configuration="Painted",
            where="capture",
        )
        self.assertEqual(
            resolved["faces"][(0, 0, 0)]["rgba"],
            (0xCC / 255, 0xBB / 255, 0xAA / 255, 1.0),
        )

    def test_native_face_color_tamper_fails_independent_verify(self):
        source = self.root / "mutated-native-appearance"
        shutil.copytree(self.generate_source, source)
        model_path = source / "model/robot.json"
        model = read_data(model_path)
        visual = next(
            visual
            for link in model["links"]
            for visual in link["visuals"]
            if (visual.get("provenance") or {}).get("material_source")
            == "native_appearance"
        )
        original_rgba = list(visual["rgba"])
        visual["rgba"] = [
            (value + 0.125) % 1.0 for value in visual["rgba"][:3]
        ] + [visual["rgba"][3]]
        write_json(model_path, model)
        urdf_path = source / "urdf/robot.urdf"
        document = ET.parse(urdf_path)
        xml_visual = next(
            node
            for node in document.getroot().iter("visual")
            if node.get("name") == visual["name"]
        )
        xml_visual.find("material/color").set(
            "rgba",
            " ".join(format_number(value) for value in visual["rgba"]),
        )
        ET.indent(document)
        urdf_path.write_bytes(
            ET.tostring(document.getroot(), encoding="utf-8", xml_declaration=True)
            + b"\n"
        )
        receipt_path = source / GENERATE_RECEIPT
        receipt = read_data(receipt_path)
        receipt["model_sha256"] = file_digest(model_path)
        receipt["urdf_sha256"] = file_digest(urdf_path)
        write_json(receipt_path, receipt)
        mutated = self.store.seal_tree(
            source,
            kind="stage_output:generate",
            producer_run_id=self.generator_command.run_id,
            producer_stage="generate",
            producer_tool_sha256=self.generate_executor,
        )
        report = self._bundle_report(mutated)
        appearance = next(
            row for row in report["checks"] if row["id"] == "appearance.native"
        )
        self.assertFalse(appearance["passed"])
        self.assertIn("RGBA differs", appearance["details"]["error"])
        self.assertNotEqual(original_rgba, visual["rgba"])

    def test_native_all_false_appearance_fails_generate_handler(self):
        source = self.root / "native-all-false-capture"
        shutil.copytree(self.capture_root, source)
        evidence = source / "evidence"
        facts_path = evidence / "raw/native_capture_facts.json.gz"
        facts = read_data(facts_path)
        geometry = facts["geometry_raw"]
        for occurrence in facts["scene_raw"]["occurrences"]:
            component = str(occurrence["name"])
            scope = facts["source_scopes"][component]
            configuration = str(scope["referenced_configuration"])

            def owner(
                member,
                *,
                active=False,
                configuration=configuration,
            ):
                return {
                    "used_api": member,
                    "material_call": (
                        {
                            "configuration_semantics": "active_configuration",
                            "active_configuration": configuration,
                        }
                        if active
                        else {
                            "configuration_semantics": "requested_configuration",
                            "configuration_option": 3,
                            "configuration_names": [configuration],
                        }
                    ),
                    "presence_call": {
                        "arguments": [],
                        "configuration_semantics": "native_owner_context",
                    },
                    "material_property_values": [-1.0] * 9,
                    "has_material_property_values": False,
                    "texture": {
                        "configuration": configuration,
                        "state": "absent",
                        "material_name": None,
                    },
                    "read_errors": {},
                }

            body_rows = []
            for body in geometry[component]["body_inventory"]:
                body_rows.append(
                    {
                        "component": component,
                        "enumeration_api": "IComponent2.GetBodies3",
                        "body_type": body["body_type"],
                        "body_index": body["body_index"],
                        "body_name": body.get("body_name"),
                        "appearance": owner(
                            "IBody2.MaterialPropertyValues2",
                            active=True,
                        ),
                        "faces": [
                            {
                                "face_index": face["face_index"],
                                "appearance": owner(
                                    "IFace2.MaterialPropertyValues"
                                ),
                            }
                            for face in geometry[component][
                                "face_inventory"
                            ]
                            if face["body_type"] == body["body_type"]
                            and face["body_index"] == body["body_index"]
                        ],
                    }
                )
            geometry[component]["appearance_raw"] = {
                "native_active_configuration": configuration,
                "native_active_configuration_error": None,
                "native_referenced_display_state": "Display State 1",
                "native_referenced_display_state_error": None,
                "document": owner("IModelDoc2.MaterialPropertyValues", active=True),
                "occurrence": owner(
                    "IComponent2.GetMaterialPropertyValues2"
                ),
                "bodies": body_rows,
                "state_render_materials": [
                    {
                        "owner_document_path": scope["source_document_path"],
                        "owner_scope": "component_document",
                        "api": "IModelDocExtension.GetRenderMaterials2",
                        "display_state_option": 3,
                        "configuration": configuration,
                        "display_state": "Display State 1",
                        "display_states": None,
                        "display_states_state": "not_applicable",
                        "native_render_material_count_api": (
                            "IModelDocExtension.GetRenderMaterialsCount2"
                        ),
                        "native_render_material_count": 0,
                        "native_render_material_count_error": None,
                        "call_state": "empty",
                        "materials": [],
                        "mapping_errors": [],
                        "native_reference_index": [],
                        "read_errors": {},
                    }
                ],
            }
        write_json(facts_path, facts)
        manifest = read_data(evidence / "manifest.json")
        write_manifest(
            evidence,
            kind=manifest["kind"],
            identity=manifest["identity"],
            evidence_class="cad",
        )
        (source / "manifest.json").unlink()
        capture_ref = self.store.seal_tree(
            source,
            kind="stage_output:capture",
            producer_run_id="capture-run",
            producer_stage="capture",
            producer_tool_sha256=digest({"executor": "capture-tool"}),
        )
        capture_root = self.store.materialize(
            capture_ref,
            self.root / "native-all-false-materialized",
        )
        generate_command = StageCommand.create(
            command_id="generate-all-false",
            run_id="generate-all-false-run",
            stage="generate",
            idempotency_key="generate-all-false/run",
            request={
                "schema_version": GENERATE_REQUEST_SCHEMA,
                "capture_artifact_id": capture_ref.artifact_id,
                "output_contract": GENERATE_OUTPUT_CONTRACT,
            },
            input_artifacts=(capture_ref,),
            config_snapshot_sha256=self.generator_command.config_snapshot_sha256,
            contract_sha256=self.generator_command.contract_sha256,
            policy_sha256=self.generator_command.policy_sha256,
            expected_executor_sha256=self.generate_executor,
            predecessor_decision_id="capture-decision",
        )
        generate_claim = replace(
            self._claim(
                generate_command,
                owner=digest({"process": "all-false-generate"}),
                executor=self.generate_executor,
            ),
            command_id=generate_command.command_id,
        )
        with self.assertRaises(StageFailure) as caught:
            GenerateHandler(self.store).execute(
                generate_command,
                generate_claim,
                StageWorkspace(
                    root=self.root / "all-false-generate-attempt",
                    inputs={capture_ref.artifact_id: capture_root},
                ),
                effects=NoExternalEffects(),
            )
        self.assertEqual(
            caught.exception.reason_code,
            "generate.appearance_unrepresentable",
        )

    def _seal_mutated_generate(self, name: str, mutate) -> object:
        source = self.root / f"generate-source-{name}"
        shutil.copytree(self.generate_source, source)
        urdf = source / "urdf/robot.urdf"
        urdf.write_text(mutate(urdf.read_text(encoding="utf-8")), encoding="utf-8")
        receipt = read_data(source / GENERATE_RECEIPT)
        receipt["urdf_sha256"] = file_digest(urdf)
        write_json(source / GENERATE_RECEIPT, receipt)
        return self.store.seal_tree(
            source,
            kind="stage_output:generate",
            producer_run_id=self.generator_command.run_id,
            producer_stage="generate",
            producer_tool_sha256=self.generate_executor,
        )

    def test_real_generate_and_capture_pair_produces_deterministic_certificate(self) -> None:
        first = self._run_verify()
        second = self._run_verify()
        first_execution, first_certificate, first_receipt = first[1], first[2], first[3]
        second_certificate, second_receipt = second[2], second[3]
        self.assertEqual(first_execution.outcome, "completed")
        model = read_data(self.generate_source / "model/robot.json")
        appearance_links = [
            link
            for link in model["links"]
            if any(
                (visual.get("provenance") or {}).get("material_source")
                == "native_appearance"
                for visual in link["visuals"]
            )
        ]
        self.assertEqual(len(appearance_links), 2)
        for link in appearance_links:
            self.assertEqual(len(link["visuals"]), 2)
            self.assertEqual(
                {tuple(visual["rgba"]) for visual in link["visuals"]},
                {
                    (0.7, 0.7, 0.7, 1.0),
                    (0.15, 0.35, 0.75, 1.0),
                },
            )
            face_references = [
                reference
                for visual in link["visuals"]
                for reference in visual["face_references"]
            ]
            self.assertEqual(len(face_references), 6)
            self.assertEqual(len(set(face_references)), 6)
            for visual in link["visuals"]:
                delivered = self.generate_source / visual["mesh_name"]
                stats = stl.read_bytes(delivered.read_bytes())
                self.assertTrue(stats.binary)
                self.assertEqual(stats.triangles, 6)
        self.assertFalse(hasattr(first_execution, "required_effect_ids"))
        self.assertEqual(first_certificate["actual_verifier_sha256"], first[6].executor_sha256)
        self.assertNotEqual(first_certificate["actual_verifier_sha256"], first[6].owner_sha256)
        self.assertEqual(first_certificate["actual_verifier"], hermetic._portable_tool())
        self.assertEqual(first[3]["actual_verifier"], hermetic._portable_tool())
        self.assertEqual(first[3]["actual_verifier_sha256"], first[6].executor_sha256)
        object_diagnostics = [row for row in first_certificate["diagnostics"] if row.get("objects")]
        self.assertTrue(object_diagnostics)
        self.assertFalse(any(row.get("detail") == {"objects": row["objects"]} for row in object_diagnostics))
        self.assertEqual(first_certificate, second_certificate)
        self.assertEqual(first[4], second[4])
        self.assertNotEqual(first_receipt["execution_id"], second_receipt["execution_id"])
        output = Path(first_execution.candidate_root)
        self.assertEqual(
            {path.name for path in output.iterdir()},
            {CERTIFICATE_FILENAME, EXECUTION_RECEIPT_FILENAME},
        )
        self.assertFalse((output / "model").exists())
        self.assertFalse((self.generate_root / "input").exists())
        self.assertFalse((self.generate_root / "evidence").exists())

    def test_consumer_process_failure_is_kept_as_receipt_execution_detail(self) -> None:
        stderr = "native consumer failure evidence"
        failure = ConsumerError(
            "Consumer loading failed",
            returncode=17,
            stderr=stderr,
        )
        with patch(
            "description_pipeline.verification.solidworks_urdf.load_consumer",
            side_effect=failure,
        ):
            result = self._run_verify()

        execution = result[1]
        certificate = result[2]
        receipt = result[3]
        self.assertEqual(execution.outcome, "failed")
        self.assertEqual(certificate["decision"], "FAIL")
        consumer = next(
            row
            for row in certificate["checks"]
            if row["id"] == "consumer.urdf"
        )
        self.assertFalse(consumer["passed"])
        self.assertNotIn("returncode", consumer["details"])
        self.assertNotIn("stderr", consumer["details"])
        self.assertNotIn("raw_checks", receipt)
        self.assertNotIn("raw_diagnostics", receipt)
        self.assertEqual(
            receipt["execution_details"]["consumer.urdf"],
            {
                "returncode": 17,
                "stderr": stderr,
            },
        )

    def test_real_verify_handler_runs_as_one_diagnostic_stage(self) -> None:
        command, claim, workspace = self._verify_request()
        scope = DiagnosticReplayScope.from_inputs(
            "verify",
            command.input_artifacts,
        )
        diagnostic = StageCommand.create(
            command_id="diagnostic-verify",
            run_id="diagnostic-verify-run",
            stage="verify",
            idempotency_key="diagnostic-verify/run",
            request=command.to_record()["request"],
            input_artifacts=command.input_artifacts,
            config_snapshot_sha256=command.config_snapshot_sha256,
            contract_sha256=command.contract_sha256,
            policy_sha256=command.policy_sha256,
            expected_executor_sha256=command.expected_executor_sha256,
            predecessor_decision_id=None,
            diagnostic_scope=scope,
        )
        diagnostic_claim = replace(
            claim,
            claim_id="diagnostic-verify-claim",
            command_id=diagnostic.command_id,
        )
        execution = VerifyHandler(
            identity_provider=hermetic._portable_tool,
            clock=lambda: datetime(2026, 10, 25, tzinfo=UTC),
        ).execute(
            diagnostic,
            diagnostic_claim,
            workspace,
            effects=NoExternalEffects(),
        )
        self.assertEqual(execution.outcome, "completed")
        self.assertEqual(diagnostic.diagnostic_scope, scope)

    def test_formal_verify_requires_a_predecessor_before_evaluation(self) -> None:
        command, claim, workspace = self._verify_request()
        formal = StageCommand.create(
            command_id="verify-without-predecessor",
            run_id="verify-without-predecessor-run",
            stage="verify",
            idempotency_key="verify-without-predecessor/run",
            request=command.to_record()["request"],
            input_artifacts=command.input_artifacts,
            config_snapshot_sha256=command.config_snapshot_sha256,
            contract_sha256=command.contract_sha256,
            policy_sha256=command.policy_sha256,
            expected_executor_sha256=command.expected_executor_sha256,
            predecessor_decision_id=None,
        )
        formal_claim = replace(
            claim,
            claim_id="verify-no-predecessor",
            command_id=formal.command_id,
        )
        with self.assertRaisesRegex(PipelineError, "predecessor_missing"):
            VerifyHandler(
                identity_provider=hermetic._portable_tool,
            ).execute(
                formal,
                formal_claim,
                workspace,
                effects=NoExternalEffects(),
            )
        self.assertFalse((workspace.root / "output").exists())

    def test_identity_provider_record_drift_is_rejected_after_source_match(self) -> None:
        initial = hermetic._portable_tool()
        cases = (
            (
                "source",
                {**initial, "source_sha256": "0" * 64},
            ),
            (
                "runtime",
                {**initial, "release": {"schema_version": "drifted", "version": "9"}},
            ),
        )
        for name, final in cases:
            with self.subTest(name=name):
                command, claim, workspace = self._verify_request()
                records = iter((initial, final))
                with self.assertRaisesRegex(ProtocolError, "verify.executor_changed"):
                    VerifyHandler(identity_provider=lambda records=records: next(records)).execute(
                        command,
                        claim,
                        workspace,
                        effects=NoExternalEffects(),
                    )
                self.assertFalse((workspace.root / "output").exists())

    def test_nested_check_observation_reaches_certificate_preview_and_publication(self) -> None:
        from description_pipeline.orchestration.portal import _quality_warnings
        from description_pipeline.pipeline.stages.publish import _publication_warning_text
        from description_pipeline.pipeline.verify_contract import (
            certificate_check_details, validate_certificate_diagnostics,
        )

        observation = {
            "code": "cad.instance_observation", "blocking": False, "component": "arm-1",
            "qualification": {"hardware_qualified": False}, "measured": {"mass_kg": 1.0},
        }
        extensions = [
            {**observation, "detail": {"stderr": text, "stdout": "native observation"}}
            for text in ("first recorded value", "second recorded value")
        ]
        evaluate = pipeline_verify.evaluate_bundle

        def with_observation(*args, **kwargs):
            report = evaluate(*args, **kwargs)
            report["checks"][0]["details"]["warnings"] = [observation, *extensions, extensions[0]]
            return report

        with patch.object(pipeline_verify, "evaluate_bundle", side_effect=with_observation):
            certificate = self._run_verify()[2]
        validate_certificate_diagnostics(certificate)
        self.assertIn(observation, certificate["diagnostics"])
        recorded = [row for row in certificate["diagnostics"] if row["code"] == observation["code"]]
        self.assertEqual(recorded, [observation, *extensions])
        displayed = [row for row in _quality_warnings(certificate) if row["code"] == observation["code"]]
        self.assertEqual(len(displayed), len(recorded))
        for extension in extensions:
            self.assertIn(extension, certificate_check_details(certificate, certificate["checks"][0])["warnings"])
        body = _publication_warning_text(certificate)
        self.assertIn(f"Verification warnings ({len(certificate['diagnostics'])} recorded)", body)
        if observation["code"] not in body:
            self.assertIn("additional warning records", body)
            self.assertIn("reports/verification-certificate.json", body)
        duplicate = copy.deepcopy(certificate)
        duplicate["diagnostics"].append(copy.deepcopy(observation))
        with self.assertRaisesRegex(PipelineError, "duplicate"):
            validate_certificate_diagnostics(duplicate)
        certificate["diagnostics"].remove(observation)
        with self.assertRaisesRegex(PipelineError, "indices"):
            validate_certificate_diagnostics(certificate)
        for flag in (True, None, "false"):
            certificate["diagnostics"][certificate["checks"][0]["details"]["warning_indices"][0]]["blocking"] = flag
            with self.assertRaisesRegex(PipelineError, "diagnostic"):
                validate_certificate_diagnostics(certificate)

    def test_validate_candidate_only_checks_runtime_identity(self) -> None:
        command, execution, certificate, _receipt, _bytes, _generate_ref, claim = self._run_verify()
        output_ref = self.store.seal_tree(
            execution.candidate_root,
            kind="stage_output:verify",
            producer_run_id=command.run_id,
            producer_stage="verify",
            producer_tool_sha256=claim.executor_sha256,
        )
        result = StageResult(
            schema_version=STAGE_RESULT_SCHEMA,
            command_id=command.command_id,
            run_id=command.run_id,
            stage="verify",
            request_digest=command.request_digest,
            outcome="completed",
            output_artifact=output_ref,
            actual_executor_sha256=claim.executor_sha256,
            checks_sha256=digest(certificate),
            resources_settled=True,
        )
        candidate = StageCandidate(result=result)
        with patch.object(
            pipeline_verify,
            "evaluate_bundle",
            side_effect=AssertionError("candidate validation must not replay mechanics"),
        ):
            validated = VerifyHandler(identity_provider=hermetic._portable_tool).validate_candidate(
                command,
                claim,
                candidate,
                effects=NoExternalEffects(),
            )
        self.assertIs(validated, candidate)

    def test_certificate_consumer_rechecks_pair_without_mechanical_replay(self) -> None:
        command, execution, certificate, _receipt, _bytes, generate_ref, _claim = self._run_verify()
        with (
            patch.object(pipeline_verify, "evaluate_bundle", side_effect=AssertionError("mechanical replay")),
            patch.object(
                pipeline_verify,
                "require_qualified_report",
                side_effect=AssertionError("mechanical replay"),
            ),
        ):
            validated = validate_verification_certificate(
                Path(execution.candidate_root),
                generate_root=self.generate_root,
                capture_root=self.capture_root,
                generate_artifact=generate_ref,
                capture_artifact=command.input_artifacts[1],
            )
        self.assertEqual(validated, certificate)

    def test_wrong_generate_receipt_capture_ref_is_rejected_before_evaluation(self) -> None:
        source = self.root / "generate-wrong-capture-ref"
        shutil.copytree(self.generate_source, source)
        receipt = read_data(source / GENERATE_RECEIPT)
        receipt["capture_artifact"]["artifact_id"] = "f" * 64
        write_json(source / GENERATE_RECEIPT, receipt)
        wrong_ref = self.store.seal_tree(
            source,
            kind="stage_output:generate",
            producer_run_id=self.generator_command.run_id,
            producer_stage="generate",
            producer_tool_sha256=self.generate_executor,
        )
        command, claim, workspace = self._verify_request(wrong_ref)
        with (
            patch.object(
                pipeline_verify,
                "evaluate_bundle",
                side_effect=AssertionError("evaluation must not start"),
            ),
            self.assertRaisesRegex(StageFailure, "different Capture artifact"),
        ):
            VerifyHandler(identity_provider=hermetic._portable_tool).execute(
                command,
                claim,
                workspace,
                effects=NoExternalEffects(),
            )

    def test_original_xml_mutations_fail_and_certificate_is_deterministic(self) -> None:
        def mesh_path_invalid(payload: str) -> str:
            return payload.replace(
                'filename="../meshes/',
                'filename="../invalid/',
                1,
            )

        def visual_invalid_name(payload: str) -> str:
            return payload.replace('visual name="', 'visual name="BAD-', 1)

        def scale_invalid(payload: str) -> str:
            return payload.replace(
                'scale="1.0 1.0 1.0"',
                'scale="2.0 1.0 1.0"',
                1,
            )

        for name, mutate in (
            ("mesh-path-invalid", mesh_path_invalid),
            ("visual-invalid-name", visual_invalid_name),
            ("scale-invalid", scale_invalid),
        ):
            generate_ref = self._seal_mutated_generate(name, mutate)
            _command, execution, certificate, _receipt, _bytes, _ref, _claim = self._run_verify(generate_ref)
            self.assertEqual(execution.outcome, "failed")
            self.assertEqual(certificate["decision"], "FAIL")
            failed = {row["id"] for row in certificate["checks"] if row["passed"] is not True}
            self.assertIn("urdf.syntax_names", failed)

        first_ref = self._seal_mutated_generate("deterministic-a", mesh_path_invalid)
        first = self._run_verify(first_ref)
        second_ref = self._seal_mutated_generate("deterministic-b", mesh_path_invalid)
        second = self._run_verify(second_ref)
        self.assertEqual(first_ref.artifact_id, second_ref.artifact_id)
        self.assertEqual(first[2], second[2])
        self.assertEqual(first[4], second[4])
        self.assertNotEqual(first[3]["execution_id"], second[3]["execution_id"])

    def test_consumer_rejects_closure_mutation_and_tampered_certificate(self) -> None:
        command, execution, certificate, _receipt, _bytes, generate_ref, _claim = self._run_verify()
        changed = self.root / "generate-changed"
        shutil.copytree(self.generate_root, changed)
        urdf = changed / "urdf/robot.urdf"
        urdf.write_bytes(urdf.read_bytes() + b"\n")
        with self.assertRaises(PipelineError):
            validate_verification_certificate(
                Path(execution.candidate_root),
                generate_root=changed,
                capture_root=self.capture_root,
                generate_artifact=generate_ref,
                capture_artifact=command.input_artifacts[1],
            )

        certificate_path = Path(execution.candidate_root) / CERTIFICATE_FILENAME
        signed = read_data(certificate_path)
        tampered = copy.deepcopy(signed)
        tampered["checks"][0]["passed"] = False
        write_json(certificate_path, tampered)
        with self.assertRaisesRegex(PipelineError, "proof digest differs"):
            validate_verification_certificate(
                Path(execution.candidate_root),
                generate_root=self.generate_root,
                capture_root=self.capture_root,
                generate_artifact=generate_ref,
                capture_artifact=command.input_artifacts[1],
            )
        write_json(certificate_path, certificate)

    def test_explicit_replay_executes_handler_and_matches_saved_certificate(self) -> None:
        command, _execution, certificate, _receipt, _bytes, generate_ref, _claim = self._run_verify()
        with (
            patch.object(
                pipeline_verify,
                "evaluate_bundle",
                wraps=pipeline_verify.evaluate_bundle,
            ) as evaluator,
        ):
            replayed = replay_verification(
                generate_root=self.generate_root,
                capture_root=self.capture_root,
                generate_artifact=generate_ref,
                capture_artifact=command.input_artifacts[1],
                identity_provider=hermetic._portable_tool,
                clock=lambda: datetime(2026, 10, 25, tzinfo=UTC),
            )
        self.assertEqual(replayed, certificate)
        self.assertGreaterEqual(evaluator.call_count, 1)
        self.assertFalse(hasattr(pipeline_verify, "publish"))


@unittest.skipUnless(PG_AVAILABLE, "pgserver/psycopg2 are not installed")
class VerifyIdentityPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server_root = tempfile.TemporaryDirectory(prefix="verify-identity-postgres-")
        cls.server = pgserver.get_server(
            Path(cls.server_root.name) / "pgdata",
            cleanup_mode="delete",
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.cleanup()
        cls.server_root.cleanup()

    def setUp(self) -> None:
        self.fixture = PipelineVerifyHandlerTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.schema = "verify_identity_" + uuid.uuid4().hex
        schema_sql = PLATFORM_SCHEMA.read_text(encoding="utf-8").replace(
            ':"platform_schema"',
            f'"{self.schema}"',
        )
        with psycopg2.connect(self.server.get_uri()) as connection, connection.cursor() as cursor:
            cursor.execute(f'CREATE SCHEMA "{self.schema}"')
            cursor.execute(schema_sql)
            cursor.execute(f'UPDATE "{self.schema}".admission SET state = %s WHERE id = 1', ("open",))

        self.identity = hermetic._portable_tool()
        self.executor = self.identity["source_sha256"]
        base_command, _claim, _workspace = self.fixture._verify_request()
        spec = DiagnosticAdmissionSpec(
            schema_version=DIAGNOSTIC_ADMISSION_SCHEMA,
            run_id="verify-identity-run",
            request_id="verify-identity-request",
            target_stage="verify",
            request=base_command.to_record()["request"],
            input_artifacts=(self.fixture.generate_ref, self.fixture.capture_ref),
            expected_executor_sha256=self.executor,
            config_snapshot_sha256=base_command.config_snapshot_sha256,
            contract_sha256=base_command.contract_sha256,
            policy_sha256=base_command.policy_sha256,
            created_at=datetime(2026, 10, 25, tzinfo=UTC),
        )
        self.command = build_diagnostic_command(spec)
        self.facts = FactStore(self._connect, self.fixture.store)
        self.facts.admit_diagnostic(spec, self.command)
        self.services = CanonicalStageServices(
            facts=self.facts,
            artifacts=self.fixture.store,
            work_root=self.fixture.root / "verify-identity-work",
            claim_policy=ClaimPolicy(
                owner_sha256=digest({"process": uuid.uuid4().hex}),
                executor_sha256=self.executor,
                lease=timedelta(minutes=5),
                claim_id_factory=lambda: "verify-identity-" + uuid.uuid4().hex,
            ),
            clock=lambda: datetime(2026, 10, 25, tzinfo=UTC),
        )

    def _connect(self):
        return psycopg2.connect(
            self.server.get_uri(),
            options=f"-c search_path={self.schema},public",
        )

    def _execute(self, provider):
        return execute_stage(
            command_id=self.command.command_id,
            handler=VerifyHandler(
                identity_provider=provider,
                clock=lambda: datetime(2026, 10, 25, tzinfo=UTC),
            ),
            services=self.services,
        )

    def test_constant_provider_preserves_source_executor_and_full_record(self) -> None:
        result = self._execute(lambda: self.identity)
        self.assertIsInstance(result, StageResult)
        self.assertEqual(result.actual_executor_sha256, self.executor)
        output = self.fixture.store.materialize(
            result.output_artifact,
            self.fixture.root / "verify-pg-output",
        )
        certificate = read_data(output / CERTIFICATE_FILENAME)
        receipt = read_data(output / EXECUTION_RECEIPT_FILENAME)
        for value in (certificate, receipt):
            self.assertEqual(value["actual_verifier"], self.identity)
            self.assertEqual(value["actual_verifier_sha256"], self.executor)
        self.assertIsNotNone(self.facts.decision_for(self.command))

    def test_local_pair_validation_failure_commits_one_failure_and_closes_claim(self) -> None:
        with patch.object(
            pipeline_verify, "_generate_receipt", side_effect=PipelineError("Generate receipt names another pair"),
        ):
            failure = self._execute(lambda: self.identity)
        self.assertIsInstance(failure, FailureEnvelope)
        self.assertEqual(failure.reason_code, "verify.local_contract_failed")
        self.assertEqual(failure.error_message, "Generate receipt names another pair")
        self.assertEqual(failure.effect_states, ())
        self.assertEqual(self.facts.decision_for(self.command).outcome, "failed")
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT closed_at, closure_code FROM execution_claim WHERE command_id = %s",
                (self.command.command_id,),
            )
            [(closed_at, code)] = cursor.fetchall()
        self.assertIsNotNone(closed_at)
        self.assertEqual(code, "definite_failure")

    def test_unavailable_actual_identity_precedes_local_input_rejection(self) -> None:
        with (
            patch.object(
                pipeline_verify, "_generate_receipt", side_effect=PipelineError("Invalid local input"),
            ) as local,
            self.assertRaisesRegex(ProtocolError, "verify.executor_unavailable"),
        ):
            self._execute(dict)
        local.assert_not_called()
        self.assertIsNone(self.facts.decision_for(self.command))

    def test_source_drift_is_rejected_after_initial_source_match(self) -> None:
        changed = {**self.identity, "source_sha256": "0" * 64}
        records = iter((self.identity, changed))
        with self.assertRaisesRegex(ProtocolError, "verify.executor_changed"):
            self._execute(lambda: next(records))
        self.assertIsNone(self.facts.decision_for(self.command))

    def test_full_record_drift_with_same_source_is_rejected(self) -> None:
        changed = {
            **self.identity,
            "release": {"schema_version": "drifted-runtime/v1", "version": "9"},
        }
        records = iter((self.identity, changed))
        with self.assertRaisesRegex(ProtocolError, "verify.executor_changed"):
            self._execute(lambda: next(records))
        self.assertIsNone(self.facts.decision_for(self.command))


if __name__ == "__main__":
    unittest.main()
