"""Publish one verified artifact pair through fenced Git and PR effects."""

from __future__ import annotations

import re
import shutil
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from ...artifacts import ArtifactStore
from ...delivery import subject_digest, write_delivery_manifest
from ...interface_records import CHECK_POLICY_SCHEMA, validate_qualification
from ...io import PipelineError, canonical, digest, file_digest, inventory, read_data, write_json
from ...repository.publish import (
    CommitSpec,
    GitHubCliPullRequestClient,
    PreparedCandidate,
    PublicationError,
    PublicationRepository,
    PullRequestClient,
    PullRequestObservation,
    materialize_repository_snapshot,
)
from ...runtime import tool_record
from ...stages import CONTRACT
from ..contracts import (
    ArtifactRef,
    ExecutionClaim,
    ProtocolError,
    StageCandidate,
    StageCommand,
    StageExecution,
    StageFailure,
)
from ..effects import EffectSettlement, EffectState, StageEffectPort
from ..engine import StageWorkspace

PUBLISH_REQUEST_SCHEMA = "description-pipeline.publish-request/v1"
PUBLISH_RECEIPT_SCHEMA = "description-pipeline.publication-receipt/v1"
PUBLISH_OUTPUT_CONTRACT = "solidworks-to-urdf.publication-artifact/v1"
GENERATE_ARTIFACT_KIND = "stage_output:generate"
CAPTURE_ARTIFACT_KIND = "stage_output:capture"
VERIFY_ARTIFACT_KIND = "stage_output:verify"
REPOSITORY_SNAPSHOT_KIND = "repository_snapshot"
GENERATE_RECEIPT = "reports/generate.json"
VERIFY_CERTIFICATE = "verification-certificate.json"
VERIFY_EXECUTION = "verification-execution.json"
PUBLICATION_RECEIPT = "publication-receipt.json"
PUBLICATION_PLAN = "publication-plan.json"
GIT_EFFECT_KEY = "publish.branch"
PR_EFFECT_KEY = "publish.pull-request"
_HEX40 = re.compile(r"\A[0-9a-f]{40}\Z")
_HEX64 = re.compile(r"\A[0-9a-f]{64}\Z")


class PublishIncomplete(PipelineError):
    """An existing external-effect result requires reconciliation."""


@dataclass(frozen=True, slots=True)
class PublishDomainRequest:
    generate_artifact: ArtifactRef
    capture_artifact: ArtifactRef
    verify_artifact: ArtifactRef
    repository_snapshot: ArtifactRef
    verified_subject_sha256: str
    repository_slug: str
    origin_url: str
    base_ref: str
    expected_base_oid: str
    branch_ref: str
    expected_head_oid: str | None
    commit: CommitSpec
    schema_version: str = PUBLISH_REQUEST_SCHEMA
    output_contract: str = PUBLISH_OUTPUT_CONTRACT

    def __post_init__(self) -> None:
        refs = (
            self.generate_artifact,
            self.capture_artifact,
            self.verify_artifact,
            self.repository_snapshot,
        )
        if any(not isinstance(item, ArtifactRef) for item in refs):
            raise ProtocolError("publish.input_ref", {})
        if (
            self.generate_artifact.kind != GENERATE_ARTIFACT_KIND
            or self.generate_artifact.producer_stage != "generate"
            or self.capture_artifact.kind != CAPTURE_ARTIFACT_KIND
            or self.capture_artifact.producer_stage != "capture"
            or self.verify_artifact.kind != VERIFY_ARTIFACT_KIND
            or self.verify_artifact.producer_stage != "verify"
            or self.repository_snapshot.kind != REPOSITORY_SNAPSHOT_KIND
        ):
            raise ProtocolError("publish.input_ref", {"reason": "kind-or-producer"})
        if len({item.artifact_id for item in refs}) != 4:
            raise ProtocolError("publish.input_ref", {"reason": "duplicate"})
        if _HEX64.fullmatch(self.verified_subject_sha256) is None:
            raise ProtocolError("publish.subject", {"field": "verified_subject_sha256"})
        if not isinstance(self.repository_slug, str) or "/" not in self.repository_slug:
            raise ProtocolError("publish.repository", {"value": self.repository_slug})
        if not isinstance(self.origin_url, str) or not self.origin_url:
            raise ProtocolError("publish.repository", {"value": self.origin_url})
        for name in ("base_ref", "branch_ref"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value or value.startswith("-") or ".." in value:
                raise ProtocolError("publish.ref", {"field": name, "value": value})
        if _HEX40.fullmatch(self.expected_base_oid) is None:
            raise ProtocolError("publish.base_oid", {"value": self.expected_base_oid})
        if self.expected_head_oid is not None and _HEX40.fullmatch(self.expected_head_oid) is None:
            raise ProtocolError("publish.head_oid", {"value": self.expected_head_oid})
        if self.schema_version != PUBLISH_REQUEST_SCHEMA or self.output_contract != PUBLISH_OUTPUT_CONTRACT:
            raise ProtocolError("publish.request_contract", {"command_id": "unknown"})

    def to_record(self) -> dict[str, Any]:
        return {
            "base_ref": self.base_ref,
            "branch_ref": self.branch_ref,
            "capture_artifact": self.capture_artifact.to_record(),
            "commit": {
                "author_email": self.commit.author_email,
                "author_name": self.commit.author_name,
                "authored_at": self.commit.authored_at,
                "body": self.commit.body,
                "committed_at": self.commit.committed_at,
                "committer_email": self.commit.committer_email,
                "committer_name": self.commit.committer_name,
                "subject": self.commit.subject,
            },
            "expected_base_oid": self.expected_base_oid,
            "expected_head_oid": self.expected_head_oid,
            "verified_subject_sha256": self.verified_subject_sha256,
            "generate_artifact": self.generate_artifact.to_record(),
            "origin_url": self.origin_url,
            "output_contract": self.output_contract,
            "repository_slug": self.repository_slug,
            "repository_snapshot_artifact": self.repository_snapshot.to_record(),
            "schema_version": self.schema_version,
            "verify_artifact": self.verify_artifact.to_record(),
        }


@dataclass(frozen=True, slots=True)
class _Inputs:
    request: PublishDomainRequest
    generate_root: Path
    capture_root: Path
    verify_root: Path
    certificate: dict[str, Any]
    execution_receipt: dict[str, Any]
    publication_root: Path
    publication_subject_sha256: str
    pr_title: str
    pr_body: str
    publisher_tool: dict[str, Any]
    actual_executor_sha256: str


@dataclass(frozen=True, slots=True)
class _PublishedCandidate:
    commit: str
    tree: str
    parents: tuple[str, ...]
    subject_sha256: str


def _request(command: StageCommand, workspace: StageWorkspace) -> PublishDomainRequest:
    request = dict(command.request)
    required = {
        "base_ref",
        "branch_ref",
        "capture_artifact_id",
        "commit",
        "expected_base_oid",
        "expected_head_oid",
        "verified_subject_sha256",
        "generate_artifact_id",
        "origin_url",
        "output_contract",
        "repository_slug",
        "repository_snapshot_artifact_id",
        "schema_version",
        "verify_artifact_id",
    }
    if set(request) != required:
        raise ProtocolError(
            "publish.request_shape",
            {"missing": sorted(required - set(request)), "unknown": sorted(set(request) - required)},
        )
    if request["schema_version"] != PUBLISH_REQUEST_SCHEMA or request["output_contract"] != PUBLISH_OUTPUT_CONTRACT:
        raise ProtocolError("publish.request_contract", {"command_id": command.command_id})
    by_id = {item.artifact_id: item for item in command.input_artifacts}
    if len(by_id) != 4 or len(command.input_artifacts) != 4:
        raise ProtocolError("publish.input_closure", {"command_id": command.command_id})
    try:
        generate = by_id[request["generate_artifact_id"]]
        capture = by_id[request["capture_artifact_id"]]
        verify = by_id[request["verify_artifact_id"]]
        repository_snapshot = by_id[request["repository_snapshot_artifact_id"]]
    except (KeyError, TypeError) as error:
        raise ProtocolError("publish.input_closure", {"command_id": command.command_id}) from error
    if set(workspace.inputs) != set(by_id):
        raise ProtocolError("publish.input_closure", {"command_id": command.command_id})
    commit = request["commit"]
    commit_fields = {
        "author_email",
        "author_name",
        "authored_at",
        "body",
        "committed_at",
        "committer_email",
        "committer_name",
        "subject",
    }
    if not isinstance(commit, Mapping) or set(commit) != commit_fields:
        raise ProtocolError("publish.commit_metadata", {"command_id": command.command_id})
    return PublishDomainRequest(
        generate_artifact=generate,
        capture_artifact=capture,
        verify_artifact=verify,
        repository_snapshot=repository_snapshot,
        verified_subject_sha256=str(request["verified_subject_sha256"]),
        repository_slug=str(request["repository_slug"]),
        origin_url=str(request["origin_url"]),
        base_ref=str(request["base_ref"]),
        expected_base_oid=str(request["expected_base_oid"]),
        branch_ref=str(request["branch_ref"]),
        expected_head_oid=request["expected_head_oid"],
        commit=CommitSpec(**dict(commit)),
    )


def _require_object(value: object, label: str) -> dict:
    if not isinstance(value, dict):
        raise PipelineError(f"{label} must be an object")
    return value


def _pair_subject(generate_root: Path, capture_root: Path) -> str:
    return digest(
        {
            **{"input/" + path: checksum for path, checksum in inventory(capture_root / "input").items()},
            **{"evidence/" + path: checksum for path, checksum in inventory(capture_root / "evidence").items()},
            **{"model/" + path: checksum for path, checksum in inventory(generate_root / "model").items()},
            **{"urdf/" + path: checksum for path, checksum in inventory(generate_root / "urdf").items()},
            **{"meshes/" + path: checksum for path, checksum in inventory(generate_root / "meshes").items()},
            "tool.json": file_digest(generate_root / "tool.json"),
        }
    )


def _validate_evidence_references(
    qualifications: list[dict],
    *,
    generate_root: Path,
    capture_root: Path,
) -> None:
    for item in qualifications:
        validate_qualification(item)
        for reference in item["evidence"]:
            relative = reference["record"]
            root = capture_root if relative.startswith(("input/", "evidence/")) else generate_root
            path = root / relative
            if not path.is_file() or file_digest(path) != reference["sha256"]:
                raise PipelineError("Publish qualification evidence differs from its artifact")


def _certificate(
    *,
    request: PublishDomainRequest,
    verify_root: Path,
    generate_root: Path,
    capture_root: Path,
    command: StageCommand,
) -> tuple[dict, dict]:
    certificate_path = verify_root / VERIFY_CERTIFICATE
    execution_path = verify_root / VERIFY_EXECUTION
    if set(inventory(verify_root, exclude=("manifest.json",))) != {VERIFY_CERTIFICATE, VERIFY_EXECUTION}:
        raise PipelineError("Verify artifact contains files outside its output contract")
    raw = certificate_path.read_bytes()
    certificate = _require_object(read_data(certificate_path), "verification certificate")
    if raw != canonical(certificate):
        raise PipelineError("Verification certificate is not canonically encoded")
    required = {
        "actual_verifier",
        "actual_verifier_sha256",
        "capture_artifact",
        "checks",
        "closure_sha256",
        "config_snapshot_sha256",
        "contract_sha256",
        "decision",
        "diagnostics",
        "evaluation_subject_sha256",
        "generate_artifact",
        "generate_receipt_sha256",
        "passed",
        "pipeline_id",
        "policy_sha256",
        "purpose",
        "qualifications",
        "request",
        "request_sha256",
        "required_checks",
        "schema_version",
        "subject_sha256",
        "subject_status",
        "verification_key",
    }
    if set(certificate) != required:
        raise PipelineError("Verification certificate fields are incomplete or unknown")
    proof = dict(certificate)
    verification_key = proof.pop("verification_key")
    if digest(proof) != verification_key:
        raise PipelineError("Verification certificate proof digest differs")
    verifier = _require_object(certificate["actual_verifier"], "actual verifier")
    if digest(verifier) != certificate["actual_verifier_sha256"]:
        raise PipelineError("Verification certificate verifier identity differs")
    if certificate["decision"] != "PASS" or certificate["passed"] is not True:
        raise PipelineError("Publish requires a passing verification certificate")
    checks = certificate["checks"]
    required_checks = certificate["required_checks"]
    if (
        not isinstance(checks, list)
        or not checks
        or not all(
            isinstance(row, dict)
            and row.get("state") == "passed"
            and row.get("passed") is True
            for row in checks
        )
        or not isinstance(required_checks, list)
        or {row.get("id") for row in checks} != set(required_checks)
    ):
        raise PipelineError("Verification certificate check inventory is incomplete")
    if (
        certificate["capture_artifact"] != request.capture_artifact.to_record()
        or certificate["generate_artifact"] != request.generate_artifact.to_record()
        or certificate["schema_version"] != "description-pipeline.verification-certificate/v1"
        or not isinstance(certificate["request"], dict)
        or digest(certificate["request"]) != certificate["request_sha256"]
        or certificate["subject_sha256"] != request.verified_subject_sha256
        or certificate["policy_sha256"] != command.policy_sha256
        or certificate["contract_sha256"] != command.contract_sha256
        or certificate["config_snapshot_sha256"] != command.config_snapshot_sha256
        or certificate["generate_receipt_sha256"] != file_digest(generate_root / GENERATE_RECEIPT)
        or certificate["closure_sha256"]
        != digest(
            {
                "capture_artifact": request.capture_artifact.to_record(),
                "generate_artifact": request.generate_artifact.to_record(),
            }
        )
        or certificate["evaluation_subject_sha256"] != _pair_subject(generate_root, capture_root)
    ):
        raise PipelineError("Verification certificate differs from the current Generate/Capture pair")
    qualifications = certificate["qualifications"]
    if not isinstance(qualifications, list):
        raise PipelineError("Verification certificate qualifications must be a list")
    _validate_evidence_references(qualifications, generate_root=generate_root, capture_root=capture_root)

    execution = _require_object(read_data(execution_path), "verification execution receipt")
    required_execution = {
        "actual_verifier",
        "actual_verifier_sha256",
        "certificate_sha256",
        "claim_id",
        "closure_sha256",
        "command_id",
        "fence",
        "finished_at",
        "raw_checks",
        "raw_diagnostics",
        "request",
        "request_sha256",
        "run_id",
        "schema_version",
        "stage",
        "started_at",
        "execution_id",
    }
    if (
        set(execution) != required_execution
        or execution["stage"] != "verify"
        or execution["certificate_sha256"] != digest(certificate)
        or execution["request"] != certificate["request"]
        or execution["request_sha256"] != certificate["request_sha256"]
        or execution["closure_sha256"] != certificate["closure_sha256"]
        or execution["actual_verifier"] != verifier
        or execution["actual_verifier_sha256"] != certificate["actual_verifier_sha256"]
    ):
        raise PipelineError("Verification execution receipt differs from its certificate")
    return certificate, execution


def _publication_root(
    *,
    request: PublishDomainRequest,
    command: StageCommand,
    generate_root: Path,
    capture_root: Path,
    verify_root: Path,
    destination: Path,
) -> tuple[Path, str, str, str]:
    destination.mkdir(parents=True)
    for name in ("input", "evidence"):
        shutil.copytree(capture_root / name, destination / name)
    for name in ("model", "urdf", "meshes"):
        shutil.copytree(generate_root / name, destination / name)
    shutil.copyfile(generate_root / "tool.json", destination / "tool.json")
    reports = destination / "reports"
    reports.mkdir()
    shutil.copyfile(verify_root / VERIFY_CERTIFICATE, reports / VERIFY_CERTIFICATE)
    shutil.copyfile(verify_root / VERIFY_EXECUTION, reports / VERIFY_EXECUTION)
    write_delivery_manifest(
        destination,
        contract={"schema_version": CONTRACT["schema_version"], "digest": command.contract_sha256},
        policy={"schema_version": CHECK_POLICY_SCHEMA, "digest": command.policy_sha256},
    )
    subject = subject_digest(destination)
    title = request.commit.subject
    body = (
        request.commit.body.rstrip()
        + "\n\n"
        + f"Verification-Certificate-SHA256: {file_digest(verify_root / VERIFY_CERTIFICATE)}\n"
        + f"Publication-Subject-SHA256: {subject}\n"
        + f"Repository: {request.repository_slug}\n"
    )
    return destination, subject, title, body


def _write_publication_plan(
    *,
    root: Path,
    inputs: _Inputs,
    candidate: PreparedCandidate,
) -> tuple[Path, dict]:
    plan = {
        "schema_version": "description-pipeline.publication-plan/v1",
        "capture_artifact": inputs.request.capture_artifact.to_record(),
        "generate_artifact": inputs.request.generate_artifact.to_record(),
        "verify_artifact": inputs.request.verify_artifact.to_record(),
        "repository_snapshot_artifact": inputs.request.repository_snapshot.to_record(),
        "repository": inputs.request.repository_slug,
        "origin_url": inputs.request.origin_url,
        "base_ref": inputs.request.base_ref,
        "expected_base_oid": inputs.request.expected_base_oid,
        "branch_ref": inputs.request.branch_ref,
        "expected_head_oid": inputs.request.expected_head_oid,
        "candidate_commit": candidate.commit,
        "candidate_tree": candidate.tree,
        "candidate_parents": list(candidate.parents),
        "publication_subject_sha256": inputs.publication_subject_sha256,
        "verification_certificate_sha256": digest(inputs.certificate),
        "pr_title_sha256": digest({"title": inputs.pr_title}),
        "pr_body_sha256": digest({"body": inputs.pr_body}),
    }
    path = root / PUBLICATION_PLAN
    write_json(path, plan)
    return path, plan


def _seal_effect_outbound(
    root: Path,
    payload: dict,
    *,
    bundle: Path | None = None,
    plan: Path | None = None,
) -> Path:
    root.mkdir(parents=True)
    write_json(root / "request.json", payload)
    if bundle is not None:
        shutil.copyfile(bundle, root / "candidate.bundle")
    if plan is not None:
        shutil.copyfile(plan, root / PUBLICATION_PLAN)
    return root


def _effect_observation(
    *,
    artifacts: ArtifactStore,
    settlement: EffectSettlement,
    root: Path,
) -> tuple[dict, Path]:
    if settlement.payload_artifact is None:
        raise PublicationError("publish.effect_observation", {"effect_id": settlement.effect_id})
    materialized = artifacts.materialize(settlement.payload_artifact, root / settlement.effect_id)
    observation = read_data(Path(materialized) / "observation.json")
    if not isinstance(observation, dict):
        raise PublicationError("publish.effect_observation", {"effect_id": settlement.effect_id})
    return observation, Path(materialized)


def _require_satisfied(effects: StageEffectPort, *, key: str, effect_id: str) -> None:
    settlement = effects.effect_settlement_for_key(key)
    if settlement is None or settlement.effect_id != effect_id or settlement.state is not EffectState.SATISFIED:
        raise PublishIncomplete("publish.effect_unsettled", {"effect_key": key, "effect_id": effect_id})


def _observe_git_exact(
    repository: PublicationRepository,
    request: PublishDomainRequest,
    candidate: PreparedCandidate,
) -> dict:
    observed_head = repository.observe(request.branch_ref)
    observed_base = repository.fetch_expected(request.base_ref, request.expected_base_oid) or request.expected_base_oid
    exact = observed_head == candidate.commit and observed_base == request.expected_base_oid
    return {
        "state": "known" if exact else "conflict",
        "base_oid": observed_base,
        "head_oid": observed_head,
        "candidate_commit": candidate.commit,
        "candidate_tree": candidate.tree,
        "candidate_parents": list(candidate.parents),
        "subject_sha256": candidate.subject_sha256,
    }


def _git_effect(
    *,
    effects: StageEffectPort,
    artifacts: ArtifactStore,
    repository: PublicationRepository,
    request: PublishDomainRequest,
    candidate: PreparedCandidate,
    workspace: StageWorkspace,
    settlement: EffectSettlement | None,
    plan_path: Path,
    plan_sha256: str,
) -> tuple[str, dict, bool]:
    if settlement is not None and settlement.state is EffectState.SATISFIED:
        observation, observation_root = _effect_observation(
            artifacts=artifacts,
            settlement=settlement,
            root=workspace.root / "effect-readback",
        )
        plan = read_data(observation_root / PUBLICATION_PLAN)
        if not isinstance(plan, dict) or file_digest(observation_root / PUBLICATION_PLAN) != observation.get(
            "publication_plan_sha256"
        ):
            raise PublicationError("publish.git_effect_plan")
        if observation.get("head_oid") != candidate.commit or observation.get("state") != "known":
            raise PublicationError("publish.git_effect_mismatch")
        return settlement.effect_id, observation, True
    if settlement is not None and settlement.state is EffectState.UNKNOWN:
        observed = repository.observe(request.branch_ref)
        if observed == candidate.commit:
            observation = {
                "state": "known",
                "base_oid": request.expected_base_oid,
                "head_oid": observed,
                "candidate_commit": candidate.commit,
                "candidate_tree": candidate.tree,
                "candidate_parents": list(candidate.parents),
                "subject_sha256": candidate.subject_sha256,
                "publication_plan_sha256": plan_sha256,
                "reconciled": True,
            }
            root = workspace.root / "effects/git-reconcile"
            root.mkdir(parents=True)
            write_json(root / "observation.json", observation)
            shutil.copyfile(plan_path, root / PUBLICATION_PLAN)
            effects.record_observation(
                settlement.effect_id,
                source="publish.git.reconcile",
                state_class="known",
                observation_root=root,
            )
            _require_satisfied(effects, key=GIT_EFFECT_KEY, effect_id=settlement.effect_id)
            return settlement.effect_id, observation, True
        raise PublishIncomplete("publish.git_effect_unknown", {"effect_id": settlement.effect_id})
    if settlement is not None and settlement.state is EffectState.CONFLICT:
        raise StageFailure("publish_git_effect_conflict", "The Git effect is conflicted")
    if settlement is not None and settlement.state is EffectState.DEFINITELY_FAILED:
        raise StageFailure("publish_git_effect_failed", "The Git effect is definitively failed")

    if settlement is None:
        expected_state = {
            "schema_version": "solidworks-to-urdf.git-ref-update/v1",
            "repository": request.repository_slug,
            "base_ref": request.base_ref,
            "expected_base_oid": request.expected_base_oid,
            "branch_ref": request.branch_ref,
            "expected_head_oid": request.expected_head_oid,
            "candidate_commit": candidate.commit,
            "candidate_tree": candidate.tree,
            "subject_sha256": candidate.subject_sha256,
        }
        outbound = _seal_effect_outbound(
            workspace.root / "effects/git-request",
            {**expected_state, "parents": list(candidate.parents)},
            bundle=candidate.bundle,
            plan=plan_path,
        )
        intent = effects.prepare_effect(
            effect_key=GIT_EFFECT_KEY,
            effect_kind="git_ref_update",
            target={"repository": request.repository_slug, "branch_ref": request.branch_ref},
            expected_state=expected_state,
            outbound_root=outbound,
        )
        effect_id = intent.effect_id
    else:
        if settlement.state is not EffectState.PREPARED:
            raise PublicationError("publish.git_effect_state", {"state": settlement.state.value})
        effect_id = settlement.effect_id

    effects.authorize_dispatch(effect_id, source="publish.git")
    try:
        repository.push(
            candidate,
            branch_ref=request.branch_ref,
            expected_head_oid=request.expected_head_oid,
        )
    except Exception as error:
        observed = repository.observe(request.branch_ref)
        if observed == candidate.commit:
            observation = {
                "state": "known",
                "base_oid": request.expected_base_oid,
                "head_oid": observed,
                "candidate_commit": candidate.commit,
                "candidate_tree": candidate.tree,
                "subject_sha256": candidate.subject_sha256,
                "publication_plan_sha256": plan_sha256,
                "response_lost": True,
            }
            root = workspace.root / "effects/git-observation"
            root.mkdir(parents=True)
            write_json(root / "observation.json", observation)
            shutil.copyfile(plan_path, root / PUBLICATION_PLAN)
            effects.record_observation(
                effect_id,
                source="publish.git",
                state_class="known",
                observation_root=root,
            )
            _require_satisfied(effects, key=GIT_EFFECT_KEY, effect_id=effect_id)
            return effect_id, observation, False
        detail = workspace.root / "effects/git-timeout"
        detail.mkdir(parents=True)
        write_json(detail / "timeout.json", {"error": type(error).__name__, "message": str(error)})
        effects.record_timeout(effect_id, source="publish.git", detail_root=detail)
        raise PublishIncomplete("publish.git_unknown", {"effect_id": effect_id}) from error

    observed = _observe_git_exact(repository, request, candidate)
    observed["publication_plan_sha256"] = plan_sha256
    response = workspace.root / "effects/git-response"
    response.mkdir(parents=True)
    write_json(response / "observation.json", observed)
    effects.record_response(effect_id, source="publish.git", response_root=response)
    observation_root = workspace.root / "effects/git-observation"
    observation_root.mkdir(parents=True)
    write_json(observation_root / "observation.json", observed)
    shutil.copyfile(plan_path, observation_root / PUBLICATION_PLAN)
    effects.record_observation(
        effect_id,
        source="publish.git",
        state_class="known" if observed["state"] == "known" else "unobservable",
        observation_root=observation_root,
    )
    _require_satisfied(effects, key=GIT_EFFECT_KEY, effect_id=effect_id)
    if observed["state"] != "known":
        raise StageFailure("publish_git_conflict", "Observed review branch does not match the fixed candidate")
    return effect_id, observed, False


def _expected_pr(
    client: PullRequestClient,
    request: PublishDomainRequest,
    candidate: PreparedCandidate | _PublishedCandidate,
) -> tuple[PullRequestObservation | None, tuple[PullRequestObservation, ...]]:
    observations = client.observe(
        repository=request.repository_slug,
        base_ref=request.base_ref,
        head_ref=request.branch_ref,
        candidate_commit=candidate.commit,
    )
    if len(observations) > 1:
        raise StageFailure("publish_pr_ambiguous", "More than one exact open pull request exists")
    return (observations[0] if observations else None), observations


def _require_pr_match(
    observation: PullRequestObservation,
    *,
    request: PublishDomainRequest,
    candidate: PreparedCandidate | _PublishedCandidate,
    title: str,
    body: str,
) -> None:
    if (
        observation.number <= 0
        or observation.state != "OPEN"
        or observation.base_ref != request.base_ref
        or observation.head_ref != request.branch_ref
        or observation.head_oid != candidate.commit
        or digest({"title": observation.title}) != digest({"title": title})
        or digest({"body": observation.body}) != digest({"body": body})
    ):
        raise StageFailure("publish_pr_conflict", "Pull request does not exactly match the fixed intent")


def _pr_effect(
    *,
    effects: StageEffectPort,
    artifacts: ArtifactStore,
    client: PullRequestClient,
    request: PublishDomainRequest,
    candidate: PreparedCandidate | _PublishedCandidate,
    workspace: StageWorkspace,
    title: str,
    body: str,
) -> tuple[str, PullRequestObservation, bool]:
    settlement = effects.effect_settlement_for_key(PR_EFFECT_KEY)
    if settlement is not None and settlement.state is EffectState.SATISFIED:
        observation, _observation_root = _effect_observation(
            artifacts=artifacts,
            settlement=settlement,
            root=workspace.root / "effect-readback",
        )
        pull_request = PullRequestObservation(**observation["pull_request"])
        _require_pr_match(
            pull_request,
            request=request,
            candidate=candidate,
            title=title,
            body=body,
        )
        return settlement.effect_id, pull_request, True
    if settlement is not None and settlement.state is EffectState.UNKNOWN:
        observations = client.observe(
            repository=request.repository_slug,
            base_ref=request.base_ref,
            head_ref=request.branch_ref,
            candidate_commit=candidate.commit,
        )
        if len(observations) == 1:
            _require_pr_match(
                observations[0],
                request=request,
                candidate=candidate,
                title=title,
                body=body,
            )
            payload = {"action": "reconciled", "pull_request": observations[0].to_record()}
            root = workspace.root / "effects/pr-reconcile"
            root.mkdir(parents=True)
            write_json(root / "observation.json", payload)
            effects.record_observation(
                settlement.effect_id,
                source="publish.pr.reconcile",
                state_class="known",
                observation_root=root,
            )
            _require_satisfied(effects, key=PR_EFFECT_KEY, effect_id=settlement.effect_id)
            return settlement.effect_id, observations[0], True
        raise PublishIncomplete("publish.pr_effect_unknown", {"effect_id": settlement.effect_id})
    if settlement is not None and settlement.state is EffectState.CONFLICT:
        raise StageFailure("publish_pr_effect_conflict", "The PR effect is conflicted")
    if settlement is not None and settlement.state is EffectState.DEFINITELY_FAILED:
        raise StageFailure("publish_pr_effect_failed", "The PR effect is definitively failed")

    existing, _observations = _expected_pr(client, request, candidate)
    title_sha256 = digest({"title": title})
    body_sha256 = digest({"body": body})
    if settlement is None:
        expected_pr = None
        if existing is not None:
            expected_pr = {"number": existing.number, "url": existing.url, "head_oid": existing.head_oid}
        expected_state = {
            "schema_version": "solidworks-to-urdf.pr-upsert/v1",
            "repository": request.repository_slug,
            "base_ref": request.base_ref,
            "head_ref": request.branch_ref,
            "candidate_commit": candidate.commit,
            "subject_sha256": candidate.subject_sha256,
            "title_digest": title_sha256,
            "body_digest": body_sha256,
            "expected_pr": expected_pr,
        }
        outbound = _seal_effect_outbound(
            workspace.root / "effects/pr-request",
            {
                **expected_state,
                "title": title,
                "body": body,
            },
        )
        intent = effects.prepare_effect(
            effect_key=PR_EFFECT_KEY,
            effect_kind="pr_upsert",
            target={
                "repository": request.repository_slug,
                "base_ref": request.base_ref,
                "head_ref": request.branch_ref,
            },
            expected_state=expected_state,
            outbound_root=outbound,
        )
        effect_id = intent.effect_id
    else:
        if settlement.state is not EffectState.PREPARED:
            raise PublicationError("publish.pr_effect_state", {"state": settlement.state.value})
        effect_id = settlement.effect_id
    effects.authorize_dispatch(effect_id, source="publish.pr")
    action = "created" if existing is None else "updated"
    try:
        if existing is None:
            observation = client.create(
                repository=request.repository_slug,
                base_ref=request.base_ref,
                head_ref=request.branch_ref,
                candidate_commit=candidate.commit,
                title=title,
                body=body,
            )
        else:
            observation = client.update(
                repository=request.repository_slug,
                number=existing.number,
                candidate_commit=candidate.commit,
                title=title,
                body=body,
            )
    except Exception as error:
        observed = client.observe(
            repository=request.repository_slug,
            base_ref=request.base_ref,
            head_ref=request.branch_ref,
            candidate_commit=candidate.commit,
        )
        if len(observed) == 1:
            observation = observed[0]
            action = "reconciled"
        else:
            detail = workspace.root / "effects/pr-timeout"
            detail.mkdir(parents=True)
            write_json(detail / "timeout.json", {"error": type(error).__name__, "message": str(error)})
            effects.record_timeout(effect_id, source="publish.pr", detail_root=detail)
            raise PublishIncomplete("publish.pr_unknown", {"effect_id": effect_id}) from error
    _require_pr_match(
        observation,
        request=request,
        candidate=candidate,
        title=title,
        body=body,
    )
    observed_again = client.observe(
        repository=request.repository_slug,
        base_ref=request.base_ref,
        head_ref=request.branch_ref,
        candidate_commit=candidate.commit,
    )
    if len(observed_again) != 1 or observed_again[0] != observation:
        raise StageFailure("publish_pr_ambiguous", "Pull request identity is not unique and exact")
    payload = {
        "action": action,
        "pull_request": observation.to_record(),
    }
    response = workspace.root / "effects/pr-response"
    response.mkdir(parents=True)
    write_json(response / "observation.json", payload)
    effects.record_response(effect_id, source="publish.pr", response_root=response)
    observation_root = workspace.root / "effects/pr-observation"
    observation_root.mkdir(parents=True)
    write_json(observation_root / "observation.json", payload)
    effects.record_observation(
        effect_id,
        source="publish.pr",
        state_class="known",
        observation_root=observation_root,
    )
    _require_satisfied(effects, key=PR_EFFECT_KEY, effect_id=effect_id)
    return effect_id, observation, False


class PublishHandler:
    """Publish one verified pair through separate Git and PR effects."""

    def __init__(
        self,
        artifacts: ArtifactStore,
        *,
        pull_requests: PullRequestClient | None = None,
    ) -> None:
        self.artifacts = artifacts
        self.pull_requests = pull_requests

    def _prepare(
        self,
        command: StageCommand,
        claim: ExecutionClaim,
        workspace: StageWorkspace,
    ) -> _Inputs:
        request = _request(command, workspace)
        for artifact in (
            request.generate_artifact,
            request.capture_artifact,
            request.verify_artifact,
            request.repository_snapshot,
        ):
            self.artifacts.verify(artifact)
        generate_root = Path(workspace.inputs[request.generate_artifact.artifact_id])
        capture_root = Path(workspace.inputs[request.capture_artifact.artifact_id])
        verify_root = Path(workspace.inputs[request.verify_artifact.artifact_id])
        certificate, execution = _certificate(
            request=request,
            verify_root=verify_root,
            generate_root=generate_root,
            capture_root=capture_root,
            command=command,
        )
        publication_root, subject, title, body = _publication_root(
            request=request,
            command=command,
            generate_root=generate_root,
            capture_root=capture_root,
            verify_root=verify_root,
            destination=workspace.root / "publication",
        )
        if claim.executor_sha256 != command.expected_executor_sha256:
            raise ProtocolError("publish.executor_mismatch", {"command_id": command.command_id})
        publisher_tool = tool_record(role="portable")
        actual_executor = publisher_tool.get("source_sha256")
        if actual_executor != claim.executor_sha256:
            raise ProtocolError(
                "publish.executor_mismatch",
                {"actual": actual_executor, "claimed": claim.executor_sha256},
            )
        if actual_executor != command.expected_executor_sha256:
            raise ProtocolError(
                "publish.executor_mismatch",
                {"actual": actual_executor, "expected": command.expected_executor_sha256},
            )
        return _Inputs(
            request=request,
            generate_root=generate_root,
            capture_root=capture_root,
            verify_root=verify_root,
            certificate=certificate,
            execution_receipt=execution,
            publication_root=publication_root,
            publication_subject_sha256=subject,
            pr_title=title,
            pr_body=body,
            publisher_tool=publisher_tool,
            actual_executor_sha256=actual_executor,
        )

    def execute(
        self,
        command: StageCommand,
        claim: ExecutionClaim,
        workspace: StageWorkspace,
        *,
        effects: StageEffectPort,
    ) -> StageExecution:
        if command.stage != "publish" or command.predecessor_decision_id is None:
            raise ProtocolError("publish.command", {"command_id": command.command_id})
        inputs = self._prepare(command, claim, workspace)
        repository_root = workspace.root / "repository"
        materialize_repository_snapshot(
            workspace.inputs[inputs.request.repository_snapshot.artifact_id],
            repository_root,
            expected_repository=inputs.request.repository_slug,
            expected_origin_url=inputs.request.origin_url,
            expected_base_ref=inputs.request.base_ref,
            expected_base_oid=inputs.request.expected_base_oid,
            expected_branch_ref=inputs.request.branch_ref,
            expected_head_oid=inputs.request.expected_head_oid,
        )
        if self.pull_requests is None:
            self.pull_requests = GitHubCliPullRequestClient(repository_root)
        repository = PublicationRepository(
            repository_root,
            expected_origin=inputs.request.origin_url,
        )
        git_settlement = effects.effect_settlement_for_key(GIT_EFFECT_KEY)
        candidate: PreparedCandidate | None = None
        if git_settlement is not None and git_settlement.state is EffectState.SATISFIED:
            git_observation, observation_root = _effect_observation(
                artifacts=self.artifacts,
                settlement=git_settlement,
                root=workspace.root / "effect-readback-git",
            )
            plan = _require_object(read_data(observation_root / PUBLICATION_PLAN), "publication plan")
            plan_sha256 = file_digest(observation_root / PUBLICATION_PLAN)
            try:
                candidate_facts = _PublishedCandidate(
                    commit=str(git_observation["candidate_commit"]),
                    tree=str(git_observation["candidate_tree"]),
                    parents=tuple(str(item) for item in git_observation["candidate_parents"]),
                    subject_sha256=str(git_observation["subject_sha256"]),
                )
            except (KeyError, TypeError) as error:
                raise PublicationError("publish.git_effect_observation") from error
            if (
                git_observation.get("state") != "known"
                or git_observation.get("head_oid") != candidate_facts.commit
                or candidate_facts.subject_sha256 != inputs.publication_subject_sha256
                or plan.get("candidate_commit") != candidate_facts.commit
                or plan.get("candidate_tree") != candidate_facts.tree
                or plan.get("candidate_parents") != list(candidate_facts.parents)
                or plan.get("publication_subject_sha256") != candidate_facts.subject_sha256
                or git_observation.get("publication_plan_sha256") != plan_sha256
            ):
                raise PublicationError("publish.git_effect_mismatch")
            git_effect_id = git_settlement.effect_id
            git_reused = True
        else:
            staging = workspace.root / "candidate"
            staging.mkdir()
            candidate = repository.candidate(
                publication_root=inputs.publication_root,
                base_ref=inputs.request.base_ref,
                expected_base_oid=inputs.request.expected_base_oid,
                branch_ref=inputs.request.branch_ref,
                expected_head_oid=inputs.request.expected_head_oid,
                commit_spec=replace(inputs.request.commit, body=inputs.pr_body),
                staging_root=staging,
            )
            candidate_facts = _PublishedCandidate(
                commit=candidate.commit,
                tree=candidate.tree,
                parents=candidate.parents,
                subject_sha256=candidate.subject_sha256,
            )
            plan_path, _plan = _write_publication_plan(
                root=workspace.root,
                inputs=inputs,
                candidate=candidate,
            )
            plan_sha256 = file_digest(plan_path)
            git_effect_id, git_observation, git_reused = _git_effect(
                effects=effects,
                artifacts=self.artifacts,
                repository=repository,
                request=inputs.request,
                candidate=candidate,
                workspace=workspace,
                settlement=git_settlement,
                plan_path=plan_path,
                plan_sha256=plan_sha256,
            )
        try:
            pr_effect_id, pr_observation, pr_reused = _pr_effect(
                effects=effects,
                artifacts=self.artifacts,
                client=self.pull_requests,
                request=inputs.request,
                candidate=candidate_facts,
                workspace=workspace,
                title=inputs.pr_title,
                body=inputs.pr_body,
            )
            if not (git_reused and pr_reused):
                if repository.observe(inputs.request.branch_ref) != candidate_facts.commit:
                    raise StageFailure("publish_remote_drift", "Published branch changed before stage completion")
                repository.fetch_expected(inputs.request.base_ref, inputs.request.expected_base_oid)
            receipt = {
                "schema_version": PUBLISH_RECEIPT_SCHEMA,
                "command_id": command.command_id,
                "run_id": command.run_id,
                "request_digest": command.request_digest,
                "repository": inputs.request.repository_slug,
                "origin_url": inputs.request.origin_url,
                "base_ref": inputs.request.base_ref,
                "expected_base_oid": inputs.request.expected_base_oid,
                "branch_ref": inputs.request.branch_ref,
                "expected_head_oid": inputs.request.expected_head_oid,
                "candidate_commit": candidate_facts.commit,
                "candidate_tree": candidate_facts.tree,
                "candidate_parents": list(candidate_facts.parents),
                "publication_subject_sha256": inputs.publication_subject_sha256,
                "verification_certificate_sha256": digest(inputs.certificate),
                "publication_plan_sha256": plan_sha256,
                "generate_artifact": inputs.request.generate_artifact.to_record(),
                "capture_artifact": inputs.request.capture_artifact.to_record(),
                "verify_artifact": inputs.request.verify_artifact.to_record(),
                "repository_snapshot_artifact": inputs.request.repository_snapshot.to_record(),
                "git_effect": {"effect_id": git_effect_id, "observation": git_observation},
                "pr_effect": {
                    "effect_id": pr_effect_id,
                    "observation": pr_observation.to_record(),
                },
                "pr_title_sha256": digest({"title": inputs.pr_title}),
                "pr_body_sha256": digest({"body": inputs.pr_body}),
                "publisher_tool": inputs.publisher_tool,
                "actual_executor_sha256": inputs.actual_executor_sha256,
                "required_executor_sha256": command.expected_executor_sha256,
            }
            output = workspace.root / "output"
            output.mkdir()
            write_json(output / PUBLICATION_RECEIPT, receipt)
            return StageExecution(
                outcome="completed",
                candidate_root=str(output),
                checks_sha256=digest(receipt),
                resources_settled=True,
            )
        finally:
            if candidate is not None:
                candidate.cleanup()

    def validate_candidate(
        self,
        command: StageCommand,
        claim: ExecutionClaim,
        candidate: StageCandidate,
        *,
        effects: StageEffectPort,
    ) -> StageCandidate:
        result = candidate.result
        if (
            command.stage != "publish"
            or claim.command_id != command.command_id
            or result.command_id != command.command_id
            or result.run_id != command.run_id
            or result.stage != "publish"
            or result.request_digest != command.request_digest
            or result.outcome != "completed"
            or result.resources_settled is not True
            or result.actual_executor_sha256 != claim.executor_sha256
            or result.output_artifact.kind != "stage_output:publish"
            or result.output_artifact.producer_run_id != command.run_id
            or result.output_artifact.producer_stage != "publish"
            or result.output_artifact.producer_tool_sha256 != claim.executor_sha256
        ):
            raise ProtocolError("publish.candidate_identity", {"command_id": command.command_id})
        self.artifacts.verify(result.output_artifact)
        with tempfile.TemporaryDirectory(prefix="publish-candidate-") as temporary:
            root = self.artifacts.materialize(
                result.output_artifact,
                Path(temporary) / "output",
            )
            if set(inventory(root, exclude=("manifest.json",))) != {PUBLICATION_RECEIPT}:
                raise PipelineError("Publish candidate output has an unknown file closure")
            receipt_path = root / PUBLICATION_RECEIPT
            raw = receipt_path.read_bytes()
            receipt = _require_object(read_data(receipt_path), "publication receipt")
            if raw != canonical(receipt):
                raise PipelineError("Publication receipt is not canonically encoded")
            validate_publication_receipt(receipt, command=command, claim=claim, artifact=result.output_artifact)
            if digest(receipt) != result.checks_sha256:
                raise ProtocolError("publish.candidate_checks", {"command_id": command.command_id})
            for key, recorded in (
                (GIT_EFFECT_KEY, receipt["git_effect"]["effect_id"]),
                (PR_EFFECT_KEY, receipt["pr_effect"]["effect_id"]),
            ):
                settlement = effects.effect_settlement_for_key(key)
                if (
                    settlement is None
                    or settlement.effect_id != recorded
                    or settlement.state is not EffectState.SATISFIED
                ):
                    raise ProtocolError("publish.candidate_effect", {"effect_key": key})
                if settlement.payload_artifact is None:
                    raise ProtocolError("publish.candidate_effect", {"effect_key": key, "reason": "payload"})
                payload_root = self.artifacts.materialize(
                    settlement.payload_artifact,
                    Path(temporary) / f"effect-{key}",
                )
                observation = read_data(payload_root / "observation.json")
                receipt_observation = (
                    receipt["git_effect"]["observation"]
                    if key == GIT_EFFECT_KEY
                    else receipt["pr_effect"]["observation"]
                )
                if key == GIT_EFFECT_KEY:
                    if observation != receipt_observation:
                        raise ProtocolError("publish.candidate_effect", {"effect_key": key, "reason": "payload"})
                elif observation.get("pull_request") != receipt_observation:
                    raise ProtocolError("publish.candidate_effect", {"effect_key": key, "reason": "payload"})
        return candidate


def validate_publication_receipt(
    receipt: dict[str, Any],
    *,
    command: StageCommand,
    claim: ExecutionClaim,
    artifact: ArtifactRef,
) -> None:
    """Validate the complete publication receipt at a downstream boundary."""

    actual_tool = tool_record(role="portable")
    required = {
        "actual_executor_sha256",
        "base_ref",
        "branch_ref",
        "candidate_commit",
        "candidate_parents",
        "candidate_tree",
        "capture_artifact",
        "command_id",
        "expected_base_oid",
        "expected_head_oid",
        "generate_artifact",
        "git_effect",
        "origin_url",
        "pr_effect",
        "pr_body_sha256",
        "pr_title_sha256",
        "repository_snapshot_artifact",
        "publication_plan_sha256",
        "publication_subject_sha256",
        "publisher_tool",
        "repository",
        "request_digest",
        "required_executor_sha256",
        "run_id",
        "schema_version",
        "verification_certificate_sha256",
        "verify_artifact",
    }
    if (
        set(receipt) != required
        or receipt["schema_version"] != PUBLISH_RECEIPT_SCHEMA
        or receipt["command_id"] != command.command_id
        or receipt["run_id"] != command.run_id
        or receipt["request_digest"] != command.request_digest
        or receipt["actual_executor_sha256"] != claim.executor_sha256
        or receipt["required_executor_sha256"] != command.expected_executor_sha256
        or not isinstance(receipt["publisher_tool"], dict)
        or receipt["publisher_tool"].get("source_sha256") != receipt["actual_executor_sha256"]
        or actual_tool != receipt["publisher_tool"]
        or _HEX40.fullmatch(str(receipt["candidate_commit"])) is None
        or _HEX40.fullmatch(str(receipt["candidate_tree"])) is None
        or not isinstance(receipt["candidate_parents"], list)
        or not receipt["candidate_parents"]
        or any(_HEX40.fullmatch(str(parent)) is None for parent in receipt["candidate_parents"])
        or _HEX64.fullmatch(str(receipt["publication_subject_sha256"])) is None
        or _HEX64.fullmatch(str(receipt["publication_plan_sha256"])) is None
        or _HEX64.fullmatch(str(receipt["verification_certificate_sha256"])) is None
        or _HEX64.fullmatch(str(receipt["pr_title_sha256"])) is None
        or _HEX64.fullmatch(str(receipt["pr_body_sha256"])) is None
        or receipt["generate_artifact"] not in [item.to_record() for item in command.input_artifacts]
        or receipt["capture_artifact"] not in [item.to_record() for item in command.input_artifacts]
        or receipt["verify_artifact"] not in [item.to_record() for item in command.input_artifacts]
        or receipt["repository_snapshot_artifact"] not in [item.to_record() for item in command.input_artifacts]
        or artifact.kind != "stage_output:publish"
        or artifact.producer_run_id != command.run_id
        or artifact.producer_stage != "publish"
        or artifact.producer_tool_sha256 != receipt["actual_executor_sha256"]
    ):
        raise PipelineError("Publication receipt is incomplete or bound to another command")
    git_effect = receipt["git_effect"]
    pr_effect = receipt["pr_effect"]
    if (
        not isinstance(git_effect, dict)
        or set(git_effect) != {"effect_id", "observation"}
        or not isinstance(git_effect["effect_id"], str)
        or not git_effect["effect_id"]
        or git_effect["observation"].get("head_oid") != receipt["candidate_commit"]
        or git_effect["observation"].get("candidate_tree") != receipt["candidate_tree"]
        or not isinstance(pr_effect, dict)
        or set(pr_effect) != {"effect_id", "observation"}
        or not isinstance(pr_effect["effect_id"], str)
        or not pr_effect["effect_id"]
    ):
        raise PipelineError("Publication effect receipts are incomplete")
    pr = pr_effect["observation"]
    if (
        not isinstance(pr, dict)
        or set(pr) != {"number", "url", "state", "base_ref", "head_ref", "head_oid", "title", "body"}
        or type(pr["number"]) is not int
        or pr["number"] <= 0
        or pr["state"] != "OPEN"
        or pr["base_ref"] != receipt["base_ref"]
        or pr["head_ref"] != receipt["branch_ref"]
        or pr["head_oid"] != receipt["candidate_commit"]
        or digest({"title": pr["title"]}) != receipt["pr_title_sha256"]
        or digest({"body": pr["body"]}) != receipt["pr_body_sha256"]
    ):
        raise PipelineError("Publication pull request receipt is incomplete")
