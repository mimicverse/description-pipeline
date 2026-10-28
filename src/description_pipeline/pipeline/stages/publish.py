"""Publish one verified artifact pair through fenced Git and PR effects."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import signal
import shutil
import tempfile
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
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
    clone_publication_repository,
    materialize_repository_snapshot,
    validate_publication_target,
)
from ...runtime import tool_record
from ...schemas import (
    PUBLICATION_PLAN_SCHEMA,
    PUBLICATION_RECEIPT_SCHEMA,
    PUBLISH_REQUEST_SCHEMA,
    VERIFICATION_CERTIFICATE_SCHEMA,
)
from ...sources.solidworks.saved_state import SAVED_STATE_WARNING_CODE
from ...stages import CONTRACT
from ..contracts import (
    ArtifactRef,
    ExecutionClaim,
    ProtocolError,
    StageCandidate,
    StageCommand,
    StageDeferral,
    StageEffectWait,
    StageExecution,
    StageFailure,
)
from ..effects import EffectIntent, EffectSettlement, EffectState, StageEffectPort
from ..engine import StageWorkspace
from ..generate_contract import (
    CAPTURE_ARTIFACT_KIND,
    GENERATE_ARTIFACT_KIND,
    GENERATE_RECEIPT,
)
from ..publish_contract import PUBLISH_OUTPUT_CONTRACT, REPOSITORY_SNAPSHOT_KIND
from ..reconciliation import EffectReconciliationEvidence
from ..verify import current_verifier_sha256
from ..verify_contract import (
    CERTIFICATE_FIELDS,
    CERTIFICATE_FILENAME as VERIFY_CERTIFICATE,
    EXECUTION_RECEIPT_FILENAME as VERIFY_EXECUTION,
    VERIFY_ARTIFACT_KIND,
)

PUBLISH_RECEIPT_SCHEMA = PUBLICATION_RECEIPT_SCHEMA
PUBLICATION_RECEIPT = "publication-receipt.json"
PUBLICATION_PLAN = "publication-plan.json"
GIT_EFFECT_KEY = "publish.branch"
PR_EFFECT_KEY = "publish.pull-request"
_HEX40 = re.compile(r"\A[0-9a-f]{40}\Z")
_HEX64 = re.compile(r"\A[0-9a-f]{64}\Z")
_GIT_OUTBOUND_FILES = frozenset({"candidate.bundle", PUBLICATION_PLAN, "request.json"})
_GIT_EXPECTED_STATE_FIELDS = frozenset(
    {
        "schema_version",
        "repository",
        "base_ref",
        "expected_base_oid",
        "branch_ref",
        "expected_head_oid",
        "candidate_commit",
        "candidate_tree",
        "subject_sha256",
    }
)
_PUBLICATION_PLAN_FIELDS = frozenset(
    {
        "base_ref",
        "branch_ref",
        "candidate_commit",
        "candidate_parents",
        "candidate_tree",
        "capture_artifact",
        "expected_base_oid",
        "expected_head_oid",
        "generate_artifact",
        "origin_url",
        "pr_body_sha256",
        "pr_title_sha256",
        "publication_subject_sha256",
        "repository",
        "repository_snapshot_artifact",
        "schema_version",
        "verification_certificate_sha256",
        "verify_artifact",
    }
)
_PR_OUTBOUND_FIELDS = frozenset(
    {
        "base_ref",
        "body",
        "body_digest",
        "candidate_commit",
        "expected_pr",
        "head_ref",
        "repository",
        "schema_version",
        "subject_sha256",
        "title",
        "title_digest",
    }
)
_PR_EXPECTED_STATE_FIELDS = _PR_OUTBOUND_FIELDS - {"body", "title"}
_PR_TARGET_FIELDS = frozenset({"base_ref", "head_ref", "repository"})


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
        try:
            validate_publication_target(base_ref=self.base_ref, branch_ref=self.branch_ref)
        except PublicationError as error:
            raise ProtocolError(error.code, error.detail) from error
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


def _request_contract(command: StageCommand) -> PublishDomainRequest:
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


def _request(command: StageCommand, workspace: StageWorkspace) -> PublishDomainRequest:
    request = _request_contract(command)
    if set(workspace.inputs) != {item.artifact_id for item in command.input_artifacts}:
        raise ProtocolError("publish.input_closure", {"command_id": command.command_id})
    return request


def _require_object(value: object, label: str) -> dict:
    if not isinstance(value, dict):
        raise PipelineError(f"{label} must be an object")
    return value


def _executor_identity(
    command: StageCommand,
    claim: ExecutionClaim,
) -> tuple[dict[str, Any], str]:
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
    return publisher_tool, actual_executor


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
    required = CERTIFICATE_FIELDS
    if set(certificate) != required:
        raise PipelineError("Verification certificate fields are incomplete or unknown")
    proof = dict(certificate)
    verification_key = proof.pop("verification_key")
    if digest(proof) != verification_key:
        raise PipelineError("Verification certificate proof digest differs")
    verifier = _require_object(certificate["actual_verifier"], "actual verifier")
    verifier_sha256 = current_verifier_sha256(verifier)
    if (
        verifier_sha256 != certificate["actual_verifier_sha256"]
        or request.verify_artifact.producer_tool_sha256 != verifier_sha256
    ):
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
        or certificate["schema_version"] != VERIFICATION_CERTIFICATE_SCHEMA
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


def _publication_warning_text(certificate: Mapping[str, Any]) -> str:
    """Project verifier diagnostics into the PR without creating a second state."""

    warnings = [
        item
        for item in certificate.get("diagnostics") or []
        if isinstance(item, Mapping) and item.get("blocking") is False and item.get("code")
    ]
    if not warnings:
        return ""
    lines = ["", "Verification warnings:"]
    for item in warnings:
        code = str(item["code"])
        message = str(item.get("message") or "")
        lines.append(f"- `{code}`: {message}")
        detail = item.get("detail")
        if code != SAVED_STATE_WARNING_CODE or not isinstance(detail, Mapping):
            continue
        missing = detail.get("missing_references")
        missing = missing if isinstance(missing, Mapping) else {}
        scope = detail.get("actual_scope")
        scope = scope if isinstance(scope, Mapping) else {}
        lines.append(
            "  - Open status: "
            f"errors={detail.get('open_errors')}, warnings={detail.get('open_warnings')}, "
            f"classification={detail.get('classification')}, "
            f"configuration={detail.get('configuration')}"
        )
        lines.append(
            "  - Missing references: "
            f"state={missing.get('state')}, "
            f"items={json.dumps(missing.get('items') or [], ensure_ascii=True, sort_keys=True)}"
        )
        lines.append(
            "  - Actual scope: "
            f"graph_nodes={scope.get('graph_nodes')}, "
            f"occurrences={scope.get('occurrence_count')}, "
            f"selected={len(scope.get('selected_files') or [])}, "
            f"excluded={len(scope.get('excluded_files') or [])}, "
            f"unresolved={len(scope.get('unresolved_occurrences') or [])}"
        )
        lines.append(
            "  - Qualification state: source_integrity_proven=false, rebuilt=false, "
            "mechanical_qualification_complete=false, hardware_qualified=false. "
            "This warning is not a source or hardware qualification."
        )
    lines.append("See `reports/quality.json` and the verification certificate for evidence bindings.")
    return "\n".join(lines)


def _publication_text(
    request: PublishDomainRequest,
    *,
    certificate: Mapping[str, Any],
    certificate_sha256: str,
    subject_sha256: str,
) -> tuple[str, str]:
    body = (
        request.commit.body.rstrip()
        + _publication_warning_text(certificate)
        + "\n\n"
        + f"Verification-Certificate-SHA256: {certificate_sha256}\n"
        + f"Publication-Subject-SHA256: {subject_sha256}\n"
        + f"Repository: {request.repository_slug}\n"
    )
    return request.commit.subject, body


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
    title, body = _publication_text(
        request,
        certificate=_require_object(read_data(verify_root / VERIFY_CERTIFICATE), "verification certificate"),
        certificate_sha256=file_digest(verify_root / VERIFY_CERTIFICATE),
        subject_sha256=subject,
    )
    return destination, subject, title, body


def _write_publication_plan(
    *,
    root: Path,
    inputs: _Inputs,
    candidate: PreparedCandidate,
) -> tuple[Path, dict]:
    plan = {
        "schema_version": PUBLICATION_PLAN_SCHEMA,
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
    candidate: PreparedCandidate | _PublishedCandidate,
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


@dataclass(frozen=True, slots=True)
class _SealedGit:
    outbound_root: Path
    candidate: _PublishedCandidate
    plan: dict[str, Any]
    plan_sha256: str
    title: str
    body: str


def _read_sealed_git_outbound_root(
    *,
    repository: PublicationRepository,
    request: PublishDomainRequest,
    outbound_root: Path,
) -> _SealedGit:
    """Read the sole candidate authority from one materialized Git-effect outbound."""

    if set(inventory(outbound_root, exclude=("manifest.json",))) != _GIT_OUTBOUND_FILES:
        raise PublicationError("publish.git_recovery_inventory", {"root": str(outbound_root)})

    plan_path = outbound_root / PUBLICATION_PLAN
    plan = _require_object(read_data(plan_path), "sealed publication plan")
    if plan_path.read_bytes() != canonical(plan) or set(plan) != _PUBLICATION_PLAN_FIELDS:
        raise PublicationError("publish.git_recovery_plan")
    if plan["schema_version"] != PUBLICATION_PLAN_SCHEMA:
        raise PublicationError("publish.git_recovery_plan", {"reason": "schema"})

    artifact_values = (
        ("capture_artifact", request.capture_artifact.to_record()),
        ("generate_artifact", request.generate_artifact.to_record()),
        ("verify_artifact", request.verify_artifact.to_record()),
        ("repository_snapshot_artifact", request.repository_snapshot.to_record()),
    )
    request_values = {
        "base_ref": request.base_ref,
        "branch_ref": request.branch_ref,
        "expected_base_oid": request.expected_base_oid,
        "expected_head_oid": request.expected_head_oid,
        "origin_url": request.origin_url,
        "repository": request.repository_slug,
    }
    if any(plan.get(name) != value for name, value in artifact_values) or any(
        plan.get(name) != value for name, value in request_values.items()
    ):
        raise PublicationError("publish.git_recovery_binding")

    commit = plan["candidate_commit"]
    tree = plan["candidate_tree"]
    parents = plan["candidate_parents"]
    subject = plan["publication_subject_sha256"]
    if (
        not isinstance(commit, str)
        or _HEX40.fullmatch(commit) is None
        or not isinstance(tree, str)
        or _HEX40.fullmatch(tree) is None
        or not isinstance(parents, list)
        or not parents
        or any(not isinstance(parent, str) or _HEX40.fullmatch(parent) is None for parent in parents)
        or not isinstance(subject, str)
        or _HEX64.fullmatch(subject) is None
        or not isinstance(plan["verification_certificate_sha256"], str)
        or _HEX64.fullmatch(plan["verification_certificate_sha256"]) is None
    ):
        raise PublicationError("publish.git_recovery_plan", {"reason": "value"})
    expected_parent = request.expected_head_oid or request.expected_base_oid
    if parents != [expected_parent]:
        raise PublicationError("publish.git_recovery_parent", {"expected": expected_parent, "actual": parents})

    outbound_request_path = outbound_root / "request.json"
    outbound_request = _require_object(read_data(outbound_request_path), "sealed Git effect request")
    if outbound_request_path.read_bytes() != canonical(outbound_request):
        raise PublicationError("publish.git_recovery_request")
    expected_outbound = {
        "schema_version": "solidworks-to-urdf.git-ref-update/v1",
        "repository": request.repository_slug,
        "base_ref": request.base_ref,
        "expected_base_oid": request.expected_base_oid,
        "branch_ref": request.branch_ref,
        "expected_head_oid": request.expected_head_oid,
        "candidate_commit": commit,
        "candidate_tree": tree,
        "subject_sha256": subject,
        "parents": parents,
    }
    if outbound_request != expected_outbound:
        raise PublicationError("publish.git_recovery_request")

    repository.verify_candidate_bundle(
        outbound_root / "candidate.bundle",
        commit=commit,
        tree=tree,
        parents=tuple(parents),
    )
    certificate_raw = repository.read_blob(commit=commit, path=f"reports/{VERIFY_CERTIFICATE}")
    try:
        certificate = json.loads(certificate_raw)
    except ValueError as error:
        raise PublicationError("publish.git_recovery_certificate") from error
    if (
        not isinstance(certificate, dict)
        or certificate_raw != canonical(certificate)
        or digest(certificate) != plan["verification_certificate_sha256"]
    ):
        raise PublicationError("publish.git_recovery_certificate")
    title, body = _publication_text(
        request,
        certificate=certificate,
        certificate_sha256=plan["verification_certificate_sha256"],
        subject_sha256=subject,
    )
    if plan["pr_title_sha256"] != digest({"title": title}) or plan["pr_body_sha256"] != digest({"body": body}):
        raise PublicationError("publish.git_recovery_pr_text")
    return _SealedGit(
        outbound_root=Path(outbound_root),
        candidate=_PublishedCandidate(
            commit=commit,
            tree=tree,
            parents=tuple(parents),
            subject_sha256=subject,
        ),
        plan=plan,
        plan_sha256=file_digest(plan_path),
        title=title,
        body=body,
    )


def _read_sealed_git_outbound(
    *,
    artifacts: ArtifactStore,
    repository: PublicationRepository,
    request: PublishDomainRequest,
    workspace: StageWorkspace,
    settlement: EffectSettlement,
) -> _SealedGit:
    """Materialize and verify one settlement's immutable Git-effect outbound."""

    if settlement.state not in {EffectState.PREPARED, EffectState.UNKNOWN, EffectState.SATISFIED}:
        raise PublicationError("publish.git_recovery_state", {"state": settlement.state.value})
    if settlement.outbound_artifact is None:
        raise PublicationError("publish.git_recovery_outbound", {"effect_id": settlement.effect_id})
    artifacts.verify(settlement.outbound_artifact)
    outbound_root = artifacts.materialize(
        settlement.outbound_artifact,
        workspace.root / "effects/git-recovery/outbound",
    )
    return _read_sealed_git_outbound_root(
        repository=repository,
        request=request,
        outbound_root=Path(outbound_root),
    )


class PublishGitReconciliationAdapter:
    """Read-only observer for one unresolved Publish review-branch effect."""

    def observe(
        self,
        *,
        command: StageCommand,
        intent: EffectIntent,
        outbound_root: Path,
        workspace_root: Path,
    ) -> EffectReconciliationEvidence:
        if intent.effect_key != GIT_EFFECT_KEY or intent.effect_kind != "git_ref_update":
            raise PublicationError(
                "publish.git_recovery_effect",
                {"effect_id": intent.effect_id, "effect_key": intent.effect_key},
            )
        request = _request_contract(command)
        repository = clone_publication_repository(
            request.origin_url,
            Path(workspace_root) / "repository",
        )
        sealed = _read_sealed_git_outbound_root(
            repository=repository,
            request=request,
            outbound_root=Path(outbound_root),
        )
        observed_head = repository.observe(request.branch_ref)
        observed_base = (
            repository.fetch_expected(request.base_ref, request.expected_base_oid)
            or request.expected_base_oid
        )
        exact = observed_head == sealed.candidate.commit and observed_base == request.expected_base_oid
        state = "known" if exact else ("absent" if not observed_head else "unobservable")
        observation = {
            "state": "known" if exact else ("absent" if not observed_head else "conflict"),
            "base_oid": observed_base,
            "head_oid": observed_head,
            "candidate_commit": sealed.candidate.commit,
            "candidate_tree": sealed.candidate.tree,
            "candidate_parents": list(sealed.candidate.parents),
            "subject_sha256": sealed.candidate.subject_sha256,
            "publication_plan_sha256": sealed.plan_sha256,
            "recovered": exact,
        }
        payload_root = Path(workspace_root) / "publish-evidence"
        payload_root.mkdir()
        write_json(payload_root / "observation.json", observation)
        shutil.copyfile(sealed.outbound_root / PUBLICATION_PLAN, payload_root / PUBLICATION_PLAN)
        shutil.copyfile(sealed.outbound_root / "request.json", payload_root / "request.json")
        return EffectReconciliationEvidence(
            fact_type="reconciled" if exact else "observation",
            state_class=state,
            payload_root=payload_root,
            metadata={
                "effect_id": intent.effect_id,
                "head_oid": observed_head,
                "base_oid": observed_base,
            },
        )


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
) -> tuple[str, dict, bool] | StageDeferral:
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
        return StageDeferral(settlement.effect_id)
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
            base_ref=request.base_ref,
            branch_ref=request.branch_ref,
            expected_head_oid=request.expected_head_oid,
        )
    except Exception as error:
        try:
            observed = repository.observe(request.branch_ref)
        except Exception as observation_error:
            detail = workspace.root / "effects/git-timeout"
            detail.mkdir(parents=True, exist_ok=True)
            write_json(
                detail / "timeout.json",
                {
                    "error": type(error).__name__,
                    "message": str(error),
                    "observation_error": type(observation_error).__name__,
                    "observation_message": str(observation_error),
                },
            )
            effects.record_timeout(effect_id, source="publish.git", detail_root=detail)
            return StageDeferral(effect_id)
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
        detail.mkdir(parents=True, exist_ok=True)
        write_json(detail / "timeout.json", {"error": type(error).__name__, "message": str(error)})
        effects.record_timeout(effect_id, source="publish.git", detail_root=detail)
        return StageDeferral(effect_id)

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
    settlement = effects.effect_settlement_for_key(GIT_EFFECT_KEY)
    if (
        settlement is not None
        and settlement.effect_id == effect_id
        and settlement.state is EffectState.UNKNOWN
    ):
        return StageDeferral(effect_id)
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
    candidate_commit: str,
    title: str,
    body: str,
) -> None:
    if not _pr_matches(
        observation,
        request=request,
        candidate_commit=candidate_commit,
        title=title,
        body=body,
    ):
        raise StageFailure("publish_pr_conflict", "Pull request does not exactly match the fixed intent")


def _pr_matches(
    observation: PullRequestObservation,
    *,
    request: PublishDomainRequest,
    candidate_commit: str,
    title: str,
    body: str,
) -> bool:
    return (
        type(observation.number) is int
        and observation.number > 0
        and isinstance(observation.url, str)
        and bool(observation.url)
        and observation.state == "OPEN"
        and observation.base_ref == request.base_ref
        and observation.head_ref == request.branch_ref
        and isinstance(observation.head_oid, str)
        and _HEX40.fullmatch(observation.head_oid) is not None
        and observation.head_oid == candidate_commit
        and isinstance(observation.title, str)
        and isinstance(observation.body, str)
        and digest({"title": observation.title}) == digest({"title": title})
        and digest({"body": observation.body}) == digest({"body": body})
    )


def _read_sealed_pull_request_outbound_root(
    *,
    request: PublishDomainRequest,
    intent: EffectIntent | None,
    outbound_root: Path,
) -> dict[str, Any]:
    """Validate one PR-effect outbound and optionally bind its immutable intent."""

    outbound_root = Path(outbound_root)
    if set(inventory(outbound_root, exclude=("manifest.json",))) != {"request.json"}:
        raise PublicationError("publish.pr_recovery_inventory", {"root": str(outbound_root)})

    request_path = outbound_root / "request.json"
    sealed_request = _require_object(read_data(request_path), "sealed pull-request effect request")
    if request_path.read_bytes() != canonical(sealed_request) or set(sealed_request) != _PR_OUTBOUND_FIELDS:
        raise PublicationError("publish.pr_recovery_request")

    expected_state = {name: sealed_request[name] for name in _PR_EXPECTED_STATE_FIELDS}
    target = {
        "repository": sealed_request["repository"],
        "base_ref": sealed_request["base_ref"],
        "head_ref": sealed_request["head_ref"],
    }
    if intent is not None:
        if expected_state != dict(intent.expected_state):
            raise PublicationError(
                "publish.pr_recovery_expected_state",
                {"effect_id": intent.effect_id},
            )
        if set(intent.target) != _PR_TARGET_FIELDS or target != dict(intent.target):
            raise PublicationError(
                "publish.pr_recovery_target",
                {"effect_id": intent.effect_id},
            )
    command_binding = {
        "repository": request.repository_slug,
        "base_ref": request.base_ref,
        "head_ref": request.branch_ref,
    }
    if any(sealed_request[name] != value for name, value in command_binding.items()):
        raise PublicationError("publish.pr_recovery_binding")

    title = sealed_request["title"]
    body = sealed_request["body"]
    if (
        not isinstance(title, str)
        or not isinstance(body, str)
        or sealed_request["title_digest"] != digest({"title": title})
        or sealed_request["body_digest"] != digest({"body": body})
    ):
        raise PublicationError("publish.pr_recovery_request", {"reason": "value"})
    return sealed_request


def _expected_pr_matches(
    observation: PullRequestObservation,
    expected_pr: dict[str, Any] | None,
) -> bool:
    return expected_pr is None or (
        observation.number == expected_pr["number"]
        and observation.url == expected_pr["url"]
        and observation.head_oid == expected_pr["head_oid"]
    )


def _defer_pull_request_observation(
    *,
    effects: StageEffectPort,
    effect_id: str,
    workspace: StageWorkspace,
    observations: tuple[PullRequestObservation, ...],
    reason: str,
) -> StageDeferral:
    """Retain inconclusive PR readback and yield without claiming a conflict."""

    root = workspace.root / "effects/pr-unobservable"
    root.mkdir(parents=True, exist_ok=True)
    write_json(
        root / "observation.json",
        {
            "action": "absent" if not observations else "unobservable",
            "reason": reason,
            "observed_pull_requests": [observation.to_record() for observation in observations],
        },
    )
    effects.record_observation(
        effect_id,
        source="publish.pr",
        state_class="absent" if not observations else "unobservable",
        observation_root=root,
    )
    return StageDeferral(effect_id)


class PublishPullRequestReconciliationAdapter:
    """Read-only observer for one unresolved Publish pull-request effect."""

    def __init__(self, pull_requests: PullRequestClient) -> None:
        self.pull_requests = pull_requests

    def observe(
        self,
        *,
        command: StageCommand,
        intent: EffectIntent,
        outbound_root: Path,
        workspace_root: Path,
    ) -> EffectReconciliationEvidence:
        if (
            intent.command_id != command.command_id
            or intent.effect_key != PR_EFFECT_KEY
            or intent.effect_kind != "pr_upsert"
        ):
            raise PublicationError(
                "publish.pr_recovery_effect",
                {
                    "effect_id": intent.effect_id,
                    "effect_key": intent.effect_key,
                    "effect_kind": intent.effect_kind,
                },
            )
        request = _request_contract(command)
        sealed = _read_sealed_pull_request_outbound_root(
            request=request,
            intent=intent,
            outbound_root=outbound_root,
        )
        observations = tuple(
            self.pull_requests.observe(
                repository=sealed["repository"],
                base_ref=sealed["base_ref"],
                head_ref=sealed["head_ref"],
                candidate_commit=sealed["candidate_commit"],
            )
        )
        exact = (
            len(observations) == 1
            and _pr_matches(
                observations[0],
                request=request,
                candidate_commit=sealed["candidate_commit"],
                title=sealed["title"],
                body=sealed["body"],
            )
            and _expected_pr_matches(observations[0], sealed["expected_pr"])
        )
        if exact:
            pull_request = observations[0]
            payload = {
                "action": "reconciled",
                "pull_request": pull_request.to_record(),
            }
            fact_type = "reconciled"
            state_class = "known"
            state = "known"
            number = pull_request.number
        else:
            payload = {
                "action": "absent" if not observations else "unobservable",
                "expected_pr": sealed["expected_pr"],
                "observed_pull_requests": [item.to_record() for item in observations],
            }
            fact_type = "observation"
            state_class = "absent" if not observations else "unobservable"
            state = state_class
            number = None

        payload_root = Path(workspace_root) / "publish-pr-evidence"
        payload_root.mkdir()
        write_json(payload_root / "observation.json", payload)
        shutil.copyfile(Path(outbound_root) / "request.json", payload_root / "request.json")
        return EffectReconciliationEvidence(
            fact_type=fact_type,
            state_class=state_class,
            payload_root=payload_root,
            metadata={
                "effect_id": intent.effect_id,
                "pull_request_number": number,
                "repository": sealed["repository"],
                "base_ref": sealed["base_ref"],
                "head_ref": sealed["head_ref"],
                "candidate_commit": sealed["candidate_commit"],
                "subject_sha256": sealed["subject_sha256"],
                "state": state,
            },
        )


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
) -> tuple[str, PullRequestObservation, bool] | StageDeferral:
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
            candidate_commit=candidate.commit,
            title=title,
            body=body,
        )
        return settlement.effect_id, pull_request, True
    if settlement is not None and settlement.state is EffectState.UNKNOWN:
        return StageDeferral(settlement.effect_id)
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
        try:
            observed = client.observe(
                repository=request.repository_slug,
                base_ref=request.base_ref,
                head_ref=request.branch_ref,
                candidate_commit=candidate.commit,
            )
        except Exception as observation_error:
            detail = workspace.root / "effects/pr-timeout"
            detail.mkdir(parents=True, exist_ok=True)
            write_json(
                detail / "timeout.json",
                {
                    "error": type(error).__name__,
                    "message": str(error),
                    "observation_error": type(observation_error).__name__,
                    "observation_message": str(observation_error),
                },
            )
            effects.record_timeout(effect_id, source="publish.pr", detail_root=detail)
            return StageDeferral(effect_id)
        if len(observed) == 1:
            if not _pr_matches(
                observed[0],
                request=request,
                candidate_commit=candidate.commit,
                title=title,
                body=body,
            ):
                return _defer_pull_request_observation(
                    effects=effects,
                    effect_id=effect_id,
                    workspace=workspace,
                    observations=observed,
                    reason="response-mismatch",
                )
            observation = observed[0]
            action = "reconciled"
        elif len(observed) > 1:
            return _defer_pull_request_observation(
                effects=effects,
                effect_id=effect_id,
                workspace=workspace,
                observations=observed,
                reason="multiple-responses",
            )
        else:
            detail = workspace.root / "effects/pr-timeout"
            detail.mkdir(parents=True, exist_ok=True)
            write_json(detail / "timeout.json", {"error": type(error).__name__, "message": str(error)})
            effects.record_timeout(effect_id, source="publish.pr", detail_root=detail)
            return StageDeferral(effect_id)
    if not _pr_matches(
        observation,
        request=request,
        candidate_commit=candidate.commit,
        title=title,
        body=body,
    ):
        return _defer_pull_request_observation(
            effects=effects,
            effect_id=effect_id,
            workspace=workspace,
            observations=(observation,),
            reason="response-mismatch",
        )
    observed_again = client.observe(
        repository=request.repository_slug,
        base_ref=request.base_ref,
        head_ref=request.branch_ref,
        candidate_commit=candidate.commit,
    )
    if len(observed_again) != 1 or observed_again[0] != observation:
        return _defer_pull_request_observation(
            effects=effects,
            effect_id=effect_id,
            workspace=workspace,
            observations=observed_again,
            reason="readback-changed",
        )
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
    settlement = effects.effect_settlement_for_key(PR_EFFECT_KEY)
    if (
        settlement is not None
        and settlement.effect_id == effect_id
        and settlement.state is EffectState.UNKNOWN
    ):
        return StageDeferral(effect_id)
    _require_satisfied(effects, key=PR_EFFECT_KEY, effect_id=effect_id)
    return effect_id, observation, False


def _verify_publish_wait_identity(
    *,
    wait: StageEffectWait,
    outbound: ArtifactRef,
    expected_state: Mapping[str, Any],
    target: Mapping[str, Any],
) -> None:
    """Verify one wait against the sealed effect identity it names."""

    if (
        outbound.artifact_id != wait.outbound_artifact_id
        or outbound.set_sha256 != wait.outbound_digest
    ):
        raise ProtocolError(
            "publish.resume_wait",
            {"where": "outbound", "effect_key": wait.effect_key},
        )
    expected_state_digest = digest(expected_state)
    intent_identity_digest = digest(
        {
            "command_id": wait.command_id,
            "effect_id": wait.effect_id,
            "effect_key": wait.effect_key,
            "effect_kind": wait.effect_kind,
            "expected_state": dict(expected_state),
            "expected_state_digest": expected_state_digest,
            "outbound_artifact_id": outbound.artifact_id,
            "outbound_digest": outbound.set_sha256,
            "target": dict(target),
        }
    )
    if (
        wait.expected_state_digest != expected_state_digest
        or wait.intent_identity_digest != intent_identity_digest
    ):
        raise ProtocolError(
            "publish.resume_wait",
            {"where": "intent_identity", "effect_key": wait.effect_key},
        )


def _verify_resume_wait(
    *,
    command: StageCommand,
    wait: StageEffectWait,
    request: PublishDomainRequest,
    effects: StageEffectPort,
    artifacts: ArtifactStore,
    repository: PublicationRepository,
    workspace: StageWorkspace,
) -> None:
    """Reconstruct the non-secret wait identity from canonical Publish effects."""

    if (
        wait.run_id != command.run_id
        or wait.stage != command.stage
        or wait.command_id != command.command_id
        or wait.request_digest != command.request_digest
    ):
        raise ProtocolError(
            "publish.resume_wait",
            {"where": "command", "command_id": command.command_id},
        )
    settlement = effects.effect_settlement_for_key(wait.effect_key)
    if (
        settlement is None
        or settlement.effect_id != wait.effect_id
        or settlement.state not in {EffectState.SATISFIED, EffectState.DEFINITELY_FAILED}
    ):
        raise ProtocolError(
            "publish.resume_wait",
            {"where": "settlement", "effect_key": wait.effect_key},
        )
    outbound = settlement.outbound_artifact
    if (
        outbound is None
        or outbound.artifact_id != wait.outbound_artifact_id
        or outbound.set_sha256 != wait.outbound_digest
    ):
        raise ProtocolError(
            "publish.resume_wait",
            {"where": "outbound", "effect_key": wait.effect_key},
        )

    if wait.effect_key == GIT_EFFECT_KEY:
        if wait.effect_kind != "git_ref_update":
            raise ProtocolError(
                "publish.resume_wait",
                {"where": "effect_kind", "effect_key": wait.effect_key},
            )
        outbound_root = artifacts.materialize(
            outbound,
            workspace.root / "resume/git-outbound",
        )
        sealed_git = _read_sealed_git_outbound_root(
            repository=repository,
            request=request,
            outbound_root=Path(outbound_root),
        )
        sealed_request = _require_object(
            read_data(sealed_git.outbound_root / "request.json"),
            "sealed Git effect request",
        )
        expected_state = {
            name: value
            for name, value in sealed_request.items()
            if name != "parents"
        }
        target = {
            "repository": request.repository_slug,
            "branch_ref": request.branch_ref,
        }
    elif wait.effect_key == PR_EFFECT_KEY:
        if wait.effect_kind != "pr_upsert":
            raise ProtocolError(
                "publish.resume_wait",
                {"where": "effect_kind", "effect_key": wait.effect_key},
            )
        outbound_root = artifacts.materialize(
            outbound,
            workspace.root / "resume/pr-outbound",
        )
        sealed_request = _read_sealed_pull_request_outbound_root(
            request=request,
            intent=None,
            outbound_root=Path(outbound_root),
        )
        expected_state = {
            name: sealed_request[name]
            for name in _PR_EXPECTED_STATE_FIELDS
        }
        target = {
            "repository": sealed_request["repository"],
            "base_ref": sealed_request["base_ref"],
            "head_ref": sealed_request["head_ref"],
        }
    else:
        raise ProtocolError(
            "publish.resume_wait",
            {"where": "effect_key", "effect_key": wait.effect_key},
        )

    _verify_publish_wait_identity(
        wait=wait,
        outbound=outbound,
        expected_state=expected_state,
        target=target,
    )


@dataclass(frozen=True, slots=True)
class _ReadOnlyCommandResult:
    returncode: int
    stdout: bytes
    stderr: bytes


AsyncCommandRunner = Callable[
    [tuple[str, ...], float],
    Awaitable[_ReadOnlyCommandResult],
]


async def _run_read_only_command(
    argv: tuple[str, ...],
    timeout: float,
) -> _ReadOnlyCommandResult:
    """Run one read-only CLI command with no stdin and a bounded process group."""

    environment = {
        **os.environ,
        "GH_PROMPT_DISABLED": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "LC_ALL": "C",
        "NO_COLOR": "1",
    }
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=environment,
        start_new_session=True,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        await process.wait()
        return _ReadOnlyCommandResult(returncode=-1, stdout=b"", stderr=b"")
    if len(stdout) > 256 * 1024 or len(stderr) > 256 * 1024:
        return _ReadOnlyCommandResult(returncode=-1, stdout=b"", stderr=b"")
    return _ReadOnlyCommandResult(
        returncode=process.returncode if process.returncode is not None else -1,
        stdout=stdout,
        stderr=stderr,
    )


@dataclass(frozen=True, slots=True)
class PublishEffectWakeupHint:
    """One trigger-only hint; it is never a stage result or qualification."""

    wait_digest: str
    observed_at: datetime
    changed: bool


def _probe_outbound_names(root: Path, expected: frozenset[str]) -> None:
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise ProtocolError("publish.probe_outbound", {"reason": "root"})
    names = set()
    for item in root.iterdir():
        if item.name == "manifest.json":
            continue
        if item.is_symlink() or not item.is_file():
            raise ProtocolError("publish.probe_outbound", {"reason": "member"})
        names.add(item.name)
    if names != expected:
        raise ProtocolError(
            "publish.probe_outbound",
            {
                "missing": sorted(expected - names),
                "unknown": sorted(names - expected),
            },
        )


def _read_canonical_probe_object(path: Path, label: str) -> dict[str, Any]:
    value = read_data(path)
    if not isinstance(value, dict) or path.read_bytes() != canonical(value):
        raise ProtocolError("publish.probe_request", {"member": label})
    return value


def _probe_timeout(value: float) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not 0 < float(value) <= 60
    ):
        raise ProtocolError("publish.probe_timeout")
    return float(value)


@dataclass(frozen=True, slots=True)
class PublishGitStatusProbe:
    """Read-only exact branch probe initialized from one sealed Git effect."""

    wait: StageEffectWait
    origin_url: str
    base_ref: str
    expected_base_oid: str
    branch_ref: str
    candidate_commit: str
    runner: AsyncCommandRunner = field(default=_run_read_only_command, repr=False)
    timeout: float = 15.0

    def __post_init__(self) -> None:
        if (
            self.wait.effect_key != GIT_EFFECT_KEY
            or self.wait.effect_kind != "git_ref_update"
        ):
            raise ProtocolError("publish.probe_effect", {"effect_key": self.wait.effect_key})
        if (
            not isinstance(self.origin_url, str)
            or not self.origin_url
            or "\x00" in self.origin_url
            or _HEX40.fullmatch(self.expected_base_oid) is None
            or _HEX40.fullmatch(self.candidate_commit) is None
        ):
            raise ProtocolError("publish.probe_expectation")
        object.__setattr__(self, "timeout", _probe_timeout(self.timeout))

    @classmethod
    def from_sealed_outbound(
        cls,
        *,
        wait: StageEffectWait,
        outbound: ArtifactRef,
        outbound_root: Path,
        runner: AsyncCommandRunner = _run_read_only_command,
        timeout: float = 15.0,
    ) -> PublishGitStatusProbe:
        if (
            wait.effect_key != GIT_EFFECT_KEY
            or wait.effect_kind != "git_ref_update"
        ):
            raise ProtocolError("publish.probe_effect", {"effect_key": wait.effect_key})
        _probe_outbound_names(outbound_root, _GIT_OUTBOUND_FILES)
        request = _read_canonical_probe_object(
            Path(outbound_root) / "request.json",
            "request.json",
        )
        plan = _read_canonical_probe_object(
            Path(outbound_root) / PUBLICATION_PLAN,
            PUBLICATION_PLAN,
        )
        if (
            set(request) != {*_GIT_EXPECTED_STATE_FIELDS, "parents"}
            or request["schema_version"] != "solidworks-to-urdf.git-ref-update/v1"
            or set(plan) != _PUBLICATION_PLAN_FIELDS
            or plan["schema_version"] != PUBLICATION_PLAN_SCHEMA
        ):
            raise ProtocolError("publish.probe_request")
        expected_state = {
            name: request[name]
            for name in _GIT_EXPECTED_STATE_FIELDS
        }
        target = {
            "repository": request["repository"],
            "branch_ref": request["branch_ref"],
        }
        _verify_publish_wait_identity(
            wait=wait,
            outbound=outbound,
            expected_state=expected_state,
            target=target,
        )
        candidate = request["candidate_commit"]
        tree = request["candidate_tree"]
        parents = request["parents"]
        if (
            _HEX40.fullmatch(candidate) is None
            or _HEX40.fullmatch(tree) is None
            or not isinstance(parents, list)
            or any(not isinstance(item, str) or _HEX40.fullmatch(item) is None for item in parents)
            or plan.get("candidate_commit") != candidate
            or plan.get("candidate_tree") != tree
            or plan.get("candidate_parents") != parents
            or plan.get("repository") != request["repository"]
            or plan.get("base_ref") != request["base_ref"]
            or plan.get("branch_ref") != request["branch_ref"]
            or plan.get("expected_base_oid") != request["expected_base_oid"]
            or plan.get("expected_head_oid") != request["expected_head_oid"]
        ):
            raise ProtocolError("publish.probe_request")
        return cls(
            wait=wait,
            origin_url=plan["origin_url"],
            base_ref=request["base_ref"],
            expected_base_oid=request["expected_base_oid"],
            branch_ref=request["branch_ref"],
            candidate_commit=candidate,
            runner=runner,
            timeout=timeout,
        )

    async def observe(self) -> PublishEffectWakeupHint:
        observed_at = datetime.now(UTC)
        command = (
            "git",
            "ls-remote",
            "--",
            self.origin_url,
            f"refs/heads/{self.base_ref}",
            f"refs/heads/{self.branch_ref}",
        )
        try:
            result = await self.runner(command, self.timeout)
        except Exception:
            return PublishEffectWakeupHint(
                wait_digest=self.wait.digest,
                observed_at=observed_at,
                changed=False,
            )
        if result.returncode != 0:
            return PublishEffectWakeupHint(
                wait_digest=self.wait.digest,
                observed_at=observed_at,
                changed=False,
            )
        try:
            lines = result.stdout.decode("utf-8").splitlines()
            refs: dict[str, str] = {}
            for line in lines:
                if not line:
                    continue
                fields = line.split()
                if (
                    len(fields) != 2
                    or _HEX40.fullmatch(fields[0]) is None
                    or fields[1] in refs
                ):
                    return PublishEffectWakeupHint(
                        wait_digest=self.wait.digest,
                        observed_at=observed_at,
                        changed=False,
                    )
                refs[fields[1]] = fields[0]
        except UnicodeDecodeError:
            return PublishEffectWakeupHint(
                wait_digest=self.wait.digest,
                observed_at=observed_at,
                changed=False,
            )
        base = refs.get(f"refs/heads/{self.base_ref}")
        branch = refs.get(f"refs/heads/{self.branch_ref}")
        changed = (
            branch == self.candidate_commit
            or (base is not None and base != self.expected_base_oid)
        )
        return PublishEffectWakeupHint(
            wait_digest=self.wait.digest,
            observed_at=observed_at,
            changed=changed,
        )


@dataclass(frozen=True, slots=True)
class PublishPullRequestStatusProbe:
    """Read-only exact PR probe initialized from one sealed PR effect."""

    wait: StageEffectWait
    repository: str
    base_ref: str
    head_ref: str
    candidate_commit: str
    title_sha256: str
    body_sha256: str
    expected_pr: Mapping[str, Any] | None
    runner: AsyncCommandRunner = field(default=_run_read_only_command, repr=False)
    timeout: float = 15.0

    def __post_init__(self) -> None:
        if (
            self.wait.effect_key != PR_EFFECT_KEY
            or self.wait.effect_kind != "pr_upsert"
            or _HEX40.fullmatch(self.candidate_commit) is None
            or _HEX64.fullmatch(self.title_sha256) is None
            or _HEX64.fullmatch(self.body_sha256) is None
        ):
            raise ProtocolError("publish.probe_effect", {"effect_key": self.wait.effect_key})
        object.__setattr__(self, "timeout", _probe_timeout(self.timeout))

    @classmethod
    def from_sealed_outbound(
        cls,
        *,
        wait: StageEffectWait,
        outbound: ArtifactRef,
        outbound_root: Path,
        runner: AsyncCommandRunner = _run_read_only_command,
        timeout: float = 15.0,
    ) -> PublishPullRequestStatusProbe:
        if (
            wait.effect_key != PR_EFFECT_KEY
            or wait.effect_kind != "pr_upsert"
        ):
            raise ProtocolError("publish.probe_effect", {"effect_key": wait.effect_key})
        _probe_outbound_names(outbound_root, frozenset({"request.json"}))
        request = _read_canonical_probe_object(
            Path(outbound_root) / "request.json",
            "request.json",
        )
        if (
            set(request) != _PR_OUTBOUND_FIELDS
            or request["schema_version"] != "solidworks-to-urdf.pr-upsert/v1"
        ):
            raise ProtocolError("publish.probe_request")
        title = request["title"]
        body = request["body"]
        if (
            not isinstance(title, str)
            or not isinstance(body, str)
            or request["title_digest"] != digest({"title": title})
            or request["body_digest"] != digest({"body": body})
        ):
            raise ProtocolError("publish.probe_request")
        expected_state = {
            name: request[name]
            for name in _PR_EXPECTED_STATE_FIELDS
        }
        target = {
            "repository": request["repository"],
            "base_ref": request["base_ref"],
            "head_ref": request["head_ref"],
        }
        _verify_publish_wait_identity(
            wait=wait,
            outbound=outbound,
            expected_state=expected_state,
            target=target,
        )
        expected_pr = request["expected_pr"]
        if expected_pr is not None and (
            not isinstance(expected_pr, dict)
            or set(expected_pr) != {"number", "url", "head_oid"}
            or type(expected_pr["number"]) is not int
            or expected_pr["number"] <= 0
            or not isinstance(expected_pr["url"], str)
            or _HEX40.fullmatch(expected_pr["head_oid"]) is None
        ):
            raise ProtocolError("publish.probe_request")
        return cls(
            wait=wait,
            repository=request["repository"],
            base_ref=request["base_ref"],
            head_ref=request["head_ref"],
            candidate_commit=request["candidate_commit"],
            title_sha256=request["title_digest"],
            body_sha256=request["body_digest"],
            expected_pr=expected_pr,
            runner=runner,
            timeout=timeout,
        )

    async def observe(self) -> PublishEffectWakeupHint:
        observed_at = datetime.now(UTC)
        command = (
            "gh",
            "pr",
            "list",
            "--repo",
            self.repository,
            "--base",
            self.base_ref,
            "--head",
            self.head_ref,
            "--state",
            "open",
            "--limit",
            "2",
            "--json",
            "number,url,state,baseRefName,headRefName,headRefOid,title,body",
        )
        try:
            result = await self.runner(command, self.timeout)
        except Exception:
            return PublishEffectWakeupHint(
                wait_digest=self.wait.digest,
                observed_at=observed_at,
                changed=False,
            )
        if result.returncode != 0:
            return PublishEffectWakeupHint(
                wait_digest=self.wait.digest,
                observed_at=observed_at,
                changed=False,
            )
        try:
            rows = json.loads(result.stdout.decode("utf-8") or "[]")
        except (UnicodeDecodeError, json.JSONDecodeError):
            rows = None
        if not isinstance(rows, list) or len(rows) > 2:
            return PublishEffectWakeupHint(
                wait_digest=self.wait.digest,
                observed_at=observed_at,
                changed=False,
            )
        matches = []
        for row in rows:
            if not isinstance(row, dict) or set(row) != {
                "number",
                "url",
                "state",
                "baseRefName",
                "headRefName",
                "headRefOid",
                "title",
                "body",
            }:
                continue
            title = row["title"]
            body = row["body"]
            if (
                row["state"] == "OPEN"
                and row["baseRefName"] == self.base_ref
                and row["headRefName"] == self.head_ref
                and row["headRefOid"] == self.candidate_commit
                and isinstance(title, str)
                and digest({"title": title}) == self.title_sha256
                and isinstance(body, str)
                and digest({"body": body}) == self.body_sha256
                and (
                    self.expected_pr is None
                    or (
                        row["number"] == self.expected_pr["number"]
                        and row["url"] == self.expected_pr["url"]
                        and row["headRefOid"] == self.expected_pr["head_oid"]
                    )
                )
            ):
                matches.append(row)
        return PublishEffectWakeupHint(
            wait_digest=self.wait.digest,
            observed_at=observed_at,
            changed=len(matches) == 1,
        )


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

    def _verify_inputs(self, request: PublishDomainRequest) -> None:
        for artifact in (
            request.generate_artifact,
            request.capture_artifact,
            request.verify_artifact,
            request.repository_snapshot,
        ):
            self.artifacts.verify(artifact)

    def _prepare(
        self,
        command: StageCommand,
        claim: ExecutionClaim,
        workspace: StageWorkspace,
        *,
        request: PublishDomainRequest | None = None,
        verify_inputs: bool = True,
    ) -> _Inputs:
        request = _request(command, workspace) if request is None else request
        if verify_inputs:
            self._verify_inputs(request)
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
        publisher_tool, actual_executor = _executor_identity(command, claim)
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
    ) -> StageExecution | StageDeferral:
        return self._execute(
            command,
            claim,
            workspace,
            effects=effects,
            resume=None,
        )

    def resume_stage(
        self,
        command: StageCommand,
        claim: ExecutionClaim,
        workspace: StageWorkspace,
        *,
        effects: StageEffectPort,
        resume: StageEffectWait,
    ) -> StageExecution | StageDeferral:
        return self._execute(
            command,
            claim,
            workspace,
            effects=effects,
            resume=resume,
        )

    def _execute(
        self,
        command: StageCommand,
        claim: ExecutionClaim,
        workspace: StageWorkspace,
        *,
        effects: StageEffectPort,
        resume: StageEffectWait | None,
    ) -> StageExecution | StageDeferral:
        if command.stage != "publish" or (
            command.predecessor_decision_id is None and command.diagnostic_scope is None
        ):
            raise ProtocolError("publish.command", {"command_id": command.command_id})
        request = _request(command, workspace)
        self._verify_inputs(request)
        repository_root = workspace.root / "repository"
        workspace.root.mkdir(parents=True, exist_ok=True)
        materialize_repository_snapshot(
            workspace.inputs[request.repository_snapshot.artifact_id],
            repository_root,
            expected_repository=request.repository_slug,
            expected_origin_url=request.origin_url,
            expected_base_ref=request.base_ref,
            expected_base_oid=request.expected_base_oid,
            expected_branch_ref=request.branch_ref,
            expected_head_oid=request.expected_head_oid,
        )
        pull_requests = (
            self.pull_requests
            if self.pull_requests is not None
            else GitHubCliPullRequestClient(repository_root)
        )
        repository = PublicationRepository(
            repository_root,
            expected_origin=request.origin_url,
        )
        if resume is not None:
            _verify_resume_wait(
                command=command,
                wait=resume,
                request=request,
                effects=effects,
                artifacts=self.artifacts,
                repository=repository,
                workspace=workspace,
            )
        git_settlement = effects.effect_settlement_for_key(GIT_EFFECT_KEY)
        candidate: PreparedCandidate | None = None
        if git_settlement is not None and git_settlement.state is EffectState.UNKNOWN:
            return StageDeferral(git_settlement.effect_id)
        elif git_settlement is not None and git_settlement.state is EffectState.SATISFIED:
            sealed = _read_sealed_git_outbound(
                artifacts=self.artifacts,
                repository=repository,
                request=request,
                workspace=workspace,
                settlement=git_settlement,
            )
            git_observation, observation_root = _effect_observation(
                artifacts=self.artifacts,
                settlement=git_settlement,
                root=workspace.root / "effect-readback-git",
            )
            plan_path = Path(observation_root) / PUBLICATION_PLAN
            if (
                not plan_path.is_file()
                or file_digest(plan_path) != sealed.plan_sha256
                or git_observation.get("state") != "known"
                or git_observation.get("base_oid") != request.expected_base_oid
                or git_observation.get("head_oid") != sealed.candidate.commit
                or git_observation.get("candidate_commit") != sealed.candidate.commit
                or git_observation.get("candidate_tree") != sealed.candidate.tree
                or git_observation.get("candidate_parents") != list(sealed.candidate.parents)
                or git_observation.get("subject_sha256") != sealed.candidate.subject_sha256
                or git_observation.get("publication_plan_sha256") != sealed.plan_sha256
            ):
                raise PublicationError("publish.git_effect_mismatch")
            candidate_facts = sealed.candidate
            plan_sha256 = sealed.plan_sha256
            publication_subject_sha256 = sealed.candidate.subject_sha256
            verification_certificate_sha256 = sealed.plan["verification_certificate_sha256"]
            pr_title = sealed.title
            pr_body = sealed.body
            publisher_tool, actual_executor = _executor_identity(command, claim)
            git_effect_id = git_settlement.effect_id
        elif git_settlement is None or git_settlement.state is EffectState.PREPARED:
            inputs = self._prepare(
                command,
                claim,
                workspace,
                request=request,
                verify_inputs=False,
            )
            staging = workspace.root / "candidate"
            staging.mkdir()
            candidate = repository.candidate(
                publication_root=inputs.publication_root,
                base_ref=request.base_ref,
                expected_base_oid=request.expected_base_oid,
                branch_ref=request.branch_ref,
                expected_head_oid=request.expected_head_oid,
                commit_spec=replace(request.commit, body=inputs.pr_body),
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
            publication_subject_sha256 = inputs.publication_subject_sha256
            verification_certificate_sha256 = digest(inputs.certificate)
            pr_title = inputs.pr_title
            pr_body = inputs.pr_body
            publisher_tool = inputs.publisher_tool
            actual_executor = inputs.actual_executor_sha256
            git_result = _git_effect(
                effects=effects,
                artifacts=self.artifacts,
                repository=repository,
                request=request,
                candidate=candidate,
                workspace=workspace,
                settlement=git_settlement,
                plan_path=plan_path,
                plan_sha256=plan_sha256,
            )
            if isinstance(git_result, StageDeferral):
                if candidate is not None:
                    candidate.cleanup()
                return git_result
            git_effect_id, git_observation, _ = git_result
        elif git_settlement.state is EffectState.CONFLICT:
            raise StageFailure("publish_git_effect_conflict", "The Git effect is conflicted")
        elif git_settlement.state is EffectState.DEFINITELY_FAILED:
            raise StageFailure("publish_git_effect_failed", "The Git effect is definitively failed")
        try:
            pr_result = _pr_effect(
                effects=effects,
                artifacts=self.artifacts,
                client=pull_requests,
                request=request,
                candidate=candidate_facts,
                workspace=workspace,
                title=pr_title,
                body=pr_body,
            )
            if isinstance(pr_result, StageDeferral):
                return pr_result
            pr_effect_id, pr_observation, _ = pr_result
            if repository.observe(request.branch_ref) != candidate_facts.commit:
                raise StageFailure("publish_remote_drift", "Published branch changed before stage completion")
            repository.fetch_expected(request.base_ref, request.expected_base_oid)
            receipt = {
                "schema_version": PUBLISH_RECEIPT_SCHEMA,
                "command_id": command.command_id,
                "run_id": command.run_id,
                "request_digest": command.request_digest,
                "repository": request.repository_slug,
                "origin_url": request.origin_url,
                "base_ref": request.base_ref,
                "expected_base_oid": request.expected_base_oid,
                "branch_ref": request.branch_ref,
                "expected_head_oid": request.expected_head_oid,
                "candidate_commit": candidate_facts.commit,
                "candidate_tree": candidate_facts.tree,
                "candidate_parents": list(candidate_facts.parents),
                "publication_subject_sha256": publication_subject_sha256,
                "verification_certificate_sha256": verification_certificate_sha256,
                "publication_plan_sha256": plan_sha256,
                "generate_artifact": request.generate_artifact.to_record(),
                "capture_artifact": request.capture_artifact.to_record(),
                "verify_artifact": request.verify_artifact.to_record(),
                "repository_snapshot_artifact": request.repository_snapshot.to_record(),
                "git_effect": {"effect_id": git_effect_id, "observation": git_observation},
                "pr_effect": {
                    "effect_id": pr_effect_id,
                    "observation": pr_observation.to_record(),
                },
                "pr_title_sha256": digest({"title": pr_title}),
                "pr_body_sha256": digest({"body": pr_body}),
                "publisher_tool": publisher_tool,
                "actual_executor_sha256": actual_executor,
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
    try:
        validate_publication_target(base_ref=receipt["base_ref"], branch_ref=receipt["branch_ref"])
    except PublicationError as error:
        raise ProtocolError(error.code, error.detail) from error
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
