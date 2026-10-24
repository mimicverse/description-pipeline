"""Passive asynchronous wake-up trigger for one canonical pending effect."""

from __future__ import annotations

import asyncio
import hashlib
import os
import stat
import subprocess
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from airflow.triggers.base import BaseTrigger, TriggerEvent

from ..artifacts import ArtifactStore
from ..io import canonical, digest, file_digest, parse_data
from ..pipeline.contracts import (
    ArtifactRef,
    StageCommand,
    StageEffectWait,
    StagePending,
)
from ..pipeline.effects import EffectIntent, EffectSettlement
from ..platform.facts import FactStore
from ..schemas import ARTIFACT_MANIFEST_SCHEMA

if TYPE_CHECKING:
    from ..pipeline.application import EffectWakeupHint
    from .native_operation_client import NativeOperationStatusExpectation


EffectWaitProbe = Callable[[StageEffectWait], Awaitable["EffectWakeupHint"]]


@dataclass(frozen=True, slots=True)
class _EffectEvidence:
    artifacts: ArtifactStore
    command: StageCommand
    intent: EffectIntent
    settlement: EffectSettlement


@dataclass(frozen=True, slots=True)
class _GitProbeTarget:
    origin_url: str
    branch_ref: str
    candidate_commit: str


@dataclass(frozen=True, slots=True)
class _PullRequestProbeTarget:
    repository: str
    base_ref: str
    head_ref: str
    candidate_commit: str
    title: str
    body: str
    expected_pr: dict[str, Any] | None


def _minimum_poll_interval(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("poll_interval must be a number")
    interval = float(value)
    if not 0.1 <= interval <= 300:
        raise ValueError("poll_interval must be between 0.1 and 300 seconds")
    return interval


def _manifest_record(
    artifacts: ArtifactStore,
    artifact: ArtifactRef,
) -> tuple[dict[str, Any], Path]:
    """Validate the artifact manifest without hashing its large payload members."""

    if artifact.locator != f"cas://artifact/{artifact.artifact_id}":
        raise ValueError("The pending outbound artifact locator is invalid")
    root = artifacts.objects / artifact.artifact_id
    if root.is_symlink() or not root.is_dir():
        raise ValueError("The pending outbound artifact is absent")
    manifest_path = root / "manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ValueError("The pending outbound artifact has no regular manifest")
    raw = manifest_path.read_bytes()
    record = parse_data(raw, Path("manifest.json"))
    if (
        not isinstance(record, dict)
        or raw != canonical(record) + b"\n"
        or record.get("schema_version") != ARTIFACT_MANIFEST_SCHEMA
        or record.get("artifact_id") != artifact.artifact_id
        or record.get("kind") != artifact.kind
        or record.get("set_sha256") != artifact.set_sha256
        or record.get("producer")
        != {
            "run_id": artifact.producer_run_id,
            "stage": artifact.producer_stage,
            "tool_sha256": artifact.producer_tool_sha256,
        }
        or file_digest(manifest_path) != artifact.manifest_sha256
    ):
        raise ValueError("The pending outbound artifact manifest is inconsistent")
    base = {key: value for key, value in record.items() if key != "artifact_id"}
    if digest(base) != artifact.artifact_id:
        raise ValueError("The pending outbound artifact identity is invalid")
    members = record.get("members")
    if (
        not isinstance(members, list)
        or digest({"members": members}) != artifact.set_sha256
        or any(
            not isinstance(member, dict)
            or set(member) != {"bytes", "path", "sha256"}
            or type(member["bytes"]) is not int
            or member["bytes"] <= 0
            or not isinstance(member["path"], str)
            or not isinstance(member["sha256"], str)
            for member in members
        )
    ):
        raise ValueError("The pending outbound artifact member manifest is invalid")
    return record, root


def _member_record(
    manifest: Mapping[str, Any],
    member: str,
) -> dict[str, Any]:
    members = manifest.get("members")
    if not isinstance(members, list):
        raise ValueError("The artifact member manifest is invalid")
    matches = [item for item in members if item.get("path") == member]
    if len(matches) != 1:
        raise ValueError(f"The artifact member {member!r} is not unique")
    return matches[0]


def _read_small_member(
    artifacts: ArtifactStore,
    artifact: ArtifactRef,
    member: str,
    *,
    max_bytes: int,
) -> tuple[bytes, dict[str, Any], dict[str, Any]]:
    """Read one hash-bound metadata member without materializing its artifact."""

    relative = PurePosixPath(member)
    if (
        not relative.parts
        or relative.is_absolute()
        or any(part in {"", ".", ".."} for part in relative.parts)
        or member == "manifest.json"
    ):
        raise ValueError("The requested artifact member path is invalid")
    manifest, root = _manifest_record(artifacts, artifact)
    declared = _member_record(manifest, member)
    size = declared.get("bytes")
    if type(size) is not int or size <= 0 or size > max_bytes:
        raise ValueError("The requested artifact member exceeds its metadata limit")
    path = root.joinpath(*relative.parts)
    if any(parent.is_symlink() for parent in path.parents if parent != root) or (
        path.is_symlink()
    ):
        raise ValueError("The requested artifact member traverses a link")
    try:
        mode = path.stat().st_mode
    except OSError as error:
        raise ValueError("The requested artifact member is absent") from error
    if not stat.S_ISREG(mode) or path.stat().st_size != size:
        raise ValueError("The requested artifact member differs from its manifest")
    content = path.read_bytes()
    if (
        len(content) != size
        or hashlib.sha256(content).hexdigest() != declared.get("sha256")
    ):
        raise ValueError("The requested artifact member bytes differ from its manifest")
    return content, declared, manifest


def _load_effect_evidence(wait: StageEffectWait) -> _EffectEvidence:
    """Load only canonical facts and the small outbound identity."""

    artifacts = ArtifactStore.from_environment(os.environ)
    facts = FactStore.from_environment(os.environ, result_reader=artifacts)
    command = facts.load_command(wait.command_id)
    facts.verify_pending_effect(command, StagePending(wait))
    intent = facts.effect_intent(
        wait.effect_id,
        expected_command_id=wait.command_id,
    )
    settlement = facts.effect_settlement(
        wait.effect_id,
        expected_command_id=wait.command_id,
    )
    outbound = settlement.outbound_artifact
    if (
        intent.effect_key != wait.effect_key
        or intent.effect_kind != wait.effect_kind
        or intent.identity_digest != wait.intent_identity_digest
        or intent.expected_state_digest != wait.expected_state_digest
        or outbound is None
        or outbound.artifact_id != wait.outbound_artifact_id
        or outbound.set_sha256 != wait.outbound_digest
    ):
        raise ValueError("The pending effect facts differ from their wait")
    return _EffectEvidence(
        artifacts=artifacts,
        command=command,
        intent=intent,
        settlement=settlement,
    )


def _expectation_from_operation(
    operation: Mapping[str, Any],
) -> NativeOperationStatusExpectation:
    from .native_operation_client import NativeOperationStatusExpectation

    return NativeOperationStatusExpectation(
        operation_id=operation["operation_id"],
        kind=operation["kind"],
        request_sha256=operation["request_sha256"],
        native_tool_requirement_sha256=operation["native_tool"][
            "requirement_sha256"
        ],
        input_archive_sha256=operation["input"]["archive_sha256"],
        payload_sha256=operation["payload_sha256"],
    )


def _native_expectation(
    evidence: _EffectEvidence,
) -> tuple[str, NativeOperationStatusExpectation]:
    from ..schemas import NATIVE_CAPTURE_EFFECT_SCHEMA
    from .native_operation_client import MAX_OPERATION_JSON_BYTES
    from .native_operation_runtime import (
        CAPTURE_KIND,
        DISCOVER_KIND,
        NativeOperationError,
        build_operation_record,
    )
    from ..pipeline.capture import (
        CAPTURE_INPUT_ARCHIVE,
        CaptureRequest,
        _CAPTURE_OUTBOUND_FIELDS,
        _capture_operation_envelope,
    )
    from ..orchestration.discover_protocol import SOURCE_ARCHIVE
    from ..sources.solidworks.discover_stage import (
        NATIVE_OBSERVATION_SCHEMA,
        NativeDiscoverRequest,
    )

    outbound = evidence.settlement.outbound_artifact
    if outbound is None:
        raise ValueError("The pending native effect has no outbound artifact")
    command = evidence.command
    if command.stage == "capture":
        raw, outbound_member, manifest = _read_small_member(
            evidence.artifacts,
            outbound,
            "outbound.json",
            max_bytes=MAX_OPERATION_JSON_BYTES,
        )
        record = parse_data(raw, Path("outbound.json"))
        if (
            not isinstance(record, dict)
            or set(record) != _CAPTURE_OUTBOUND_FIELDS
            or raw != canonical(record)
        ):
            raise ValueError("The Capture pending outbound metadata is invalid")
        archive_member = _member_record(manifest, CAPTURE_INPUT_ARCHIVE)
        if (
            record.get("input_archive_sha256") != archive_member.get("sha256")
            or record.get("input_archive_bytes") != archive_member.get("bytes")
        ):
            raise ValueError("The Capture pending archive identity is inconsistent")
        request = CaptureRequest.from_command(command)
        expected_state = {
            "command_id": command.command_id,
            "effect_key": request.effect_key,
            "handoff_sha256": request.handoff_sha256,
            "input_archive_bytes": archive_member["bytes"],
            "input_archive_sha256": archive_member["sha256"],
            "main_assembly": request.main_assembly,
            "native_tool_record_sha256": request.native_tool_record_sha256,
            "native_tool_source_sha256": request.native_tool_source_sha256,
            "object_set_sha256": record.get("object_set_sha256"),
            "operation_id": record.get("operation_id"),
            "outbound_sha256": digest(
                {"members": [outbound_member, archive_member]}
            ),
            "payload_sha256": record.get("payload_sha256"),
            "prepared_sha256": request.prepared_sha256,
            "request_digest": command.request_digest,
            "schema_version": NATIVE_CAPTURE_EFFECT_SCHEMA,
            "upload_sha256": request.upload_sha256,
        }
        if (
            record.get("schema_version") != NATIVE_CAPTURE_EFFECT_SCHEMA
            or record.get("command_id") != command.command_id
            or record.get("run_id") != command.run_id
            or record.get("request_digest") != command.request_digest
            or record.get("effect_key") != request.effect_key
            or expected_state != dict(evidence.intent.expected_state)
        ):
            raise ValueError("The Capture pending outbound differs from its intent")
        operation = _capture_operation_envelope(record)
        if (
            operation["operation_id"] != record.get("operation_id")
            or operation["payload_sha256"] != record.get("payload_sha256")
        ):
            raise ValueError("The Capture pending operation identity is invalid")
        return CAPTURE_KIND, _expectation_from_operation(operation)

    if command.stage == "discover":
        request_raw, _, manifest = _read_small_member(
            evidence.artifacts,
            outbound,
            "request.json",
            max_bytes=MAX_OPERATION_JSON_BYTES,
        )
        request = NativeDiscoverRequest.from_record(
            parse_data(request_raw, Path("request.json"))
        )
        if request_raw != canonical(request.to_record()):
            raise ValueError("The Discover pending request is not canonical")
        source_raw, _, _ = _read_small_member(
            evidence.artifacts,
            outbound,
            "source.json",
            max_bytes=MAX_OPERATION_JSON_BYTES,
        )
        source = parse_data(source_raw, Path("source.json"))
        archive_member = _member_record(manifest, SOURCE_ARCHIVE)
        if (
            source
            != {
                "source_files": dict(request.source_files),
                "source_inventory_sha256": request.source_inventory_sha256,
            }
            or source_raw != canonical(source)
            or request.command_id != command.command_id
            or request.run_id != command.run_id
            or request.request_digest != command.request_digest
            or request.expected_native_tool_sha256
            != command.request.get("expected_native_tool_sha256")
            or request.expected_native_tool_record_sha256
            != command.request.get("expected_native_tool_record_sha256")
            or request.native_tool_requirement_artifact_id
            != command.request.get("native_tool_requirement_artifact_id")
        ):
            raise ValueError("The Discover pending outbound differs from its command")
        expected_state = {
            "schema_version": "solidworks-to-urdf.native-discover-effect/v1",
            "operation_id": request.operation_id,
            "request_digest": command.request_digest,
            "source_inventory_sha256": request.source_inventory_sha256,
            "source_archive_sha256": archive_member["sha256"],
            "native_tool_sha256": request.expected_native_tool_sha256,
            "native_tool_record_sha256": request.expected_native_tool_record_sha256,
            "native_tool_requirement_artifact_id": (
                request.native_tool_requirement_artifact_id
            ),
        }
        if expected_state != dict(evidence.intent.expected_state):
            raise ValueError("The Discover pending outbound differs from its intent")
        try:
            operation = build_operation_record(
                operation_id=request.operation_id,
                kind=DISCOVER_KIND,
                request=request.to_record(),
                input_archive_sha256=archive_member["sha256"],
                input_archive_bytes=archive_member["bytes"],
                native_tool_requirement_sha256=(
                    request.expected_native_tool_record_sha256
                ),
                output_contract=NATIVE_OBSERVATION_SCHEMA,
            )
        except NativeOperationError as error:
            raise ValueError("The Discover pending operation is invalid") from error
        return DISCOVER_KIND, _expectation_from_operation(operation)

    raise ValueError("native_operation is valid only for Discover or Capture")


def _native_probe_from_environment(
    wait: StageEffectWait,
) -> tuple[Any, NativeOperationStatusExpectation]:
    """Load one small immutable native identity without materializing its archive."""

    from ..pipeline.application import _native_client_from_environment

    evidence = _load_effect_evidence(wait)
    if evidence.intent.effect_kind != "native_operation":
        raise ValueError("The pending effect is not a native operation")
    _kind, expected = _native_expectation(evidence)
    return _native_client_from_environment(os.environ), expected


def _native_wakeable(view: object | None) -> bool:
    if view is None:
        return False
    state = getattr(view, "state", None)
    if state == "accepted":
        return True
    return getattr(view, "resource_terminated", False) is True and state in {
        "satisfied",
        "failed",
        "cancelled",
        "unknown",
    }


def _git_probe_target(evidence: _EffectEvidence) -> _GitProbeTarget:
    from .native_operation_client import MAX_OPERATION_JSON_BYTES

    outbound = evidence.settlement.outbound_artifact
    if outbound is None:
        raise ValueError("The pending Git effect has no outbound artifact")
    raw, _, _ = _read_small_member(
        evidence.artifacts,
        outbound,
        "request.json",
        max_bytes=MAX_OPERATION_JSON_BYTES,
    )
    request = parse_data(raw, Path("request.json"))
    expected = dict(evidence.intent.expected_state)
    if not isinstance(request, dict):
        raise ValueError("The pending Git request is malformed")
    parents = request.pop("parents", None)
    if (
        raw != canonical({**request, "parents": parents})
        or request != expected
        or not isinstance(parents, list)
        or not parents
        or dict(evidence.intent.target)
        != {
            "repository": request.get("repository"),
            "branch_ref": request.get("branch_ref"),
        }
    ):
        raise ValueError("The pending Git request differs from its intent")
    origin_url = evidence.command.request.get("origin_url")
    branch_ref = request.get("branch_ref")
    candidate_commit = request.get("candidate_commit")
    if (
        not isinstance(origin_url, str)
        or not origin_url
        or "\x00" in origin_url
        or not isinstance(branch_ref, str)
        or not isinstance(candidate_commit, str)
    ):
        raise ValueError("The pending Git target is invalid")
    return _GitProbeTarget(origin_url, branch_ref, candidate_commit)


def _observe_git_branch(target: _GitProbeTarget) -> bool:
    """Read the exact remote review branch without cloning or writing a workspace."""

    if (
        not target.branch_ref
        or target.branch_ref.startswith("-")
        or ".." in target.branch_ref
        or target.branch_ref.endswith(".lock")
    ):
        return False
    env = {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",
        "LC_ALL": "C",
    }
    try:
        result = subprocess.run(
            [
                "git",
                "ls-remote",
                "--heads",
                target.origin_url,
                f"refs/heads/{target.branch_ref}",
            ],
            check=False,
            capture_output=True,
            text=True,
            env=env,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError, UnicodeError):
        return False
    if result.returncode != 0:
        return False
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    if len(lines) != 1:
        return False
    fields = lines[0].split()
    return (
        len(fields) == 2
        and fields[1] == f"refs/heads/{target.branch_ref}"
        and fields[0] == target.candidate_commit
    )


def _pull_request_probe_target(
    evidence: _EffectEvidence,
) -> _PullRequestProbeTarget:
    from .native_operation_client import MAX_OPERATION_JSON_BYTES

    outbound = evidence.settlement.outbound_artifact
    if outbound is None:
        raise ValueError("The pending pull request effect has no outbound artifact")
    raw, _, _ = _read_small_member(
        evidence.artifacts,
        outbound,
        "request.json",
        max_bytes=MAX_OPERATION_JSON_BYTES,
    )
    request = parse_data(raw, Path("request.json"))
    expected = dict(evidence.intent.expected_state)
    if (
        not isinstance(request, dict)
        or raw != canonical(request)
        or {key: request.get(key) for key in expected} != expected
        or any(key not in request for key in ("title", "body"))
    ):
        raise ValueError("The pending pull request differs from its intent")
    title = request["title"]
    body = request["body"]
    if (
        not isinstance(title, str)
        or not isinstance(body, str)
        or request.get("title_digest") != digest({"title": title})
        or request.get("body_digest") != digest({"body": body})
    ):
        raise ValueError("The pending pull request text identity is invalid")
    target = {
        "repository": request.get("repository"),
        "base_ref": request.get("base_ref"),
        "head_ref": request.get("head_ref"),
    }
    if dict(evidence.intent.target) != target:
        raise ValueError("The pending pull request target differs from its intent")
    expected_pr = request.get("expected_pr")
    if expected_pr is not None and not isinstance(expected_pr, dict):
        raise ValueError("The pending pull request expectation is invalid")
    values = (
        target["repository"],
        target["base_ref"],
        target["head_ref"],
        request.get("candidate_commit"),
    )
    if not all(isinstance(value, str) and value for value in values):
        raise ValueError("The pending pull request target value is invalid")
    return _PullRequestProbeTarget(
        repository=target["repository"],
        base_ref=target["base_ref"],
        head_ref=target["head_ref"],
        candidate_commit=request["candidate_commit"],
        title=title,
        body=body,
        expected_pr=expected_pr,
    )


def _observe_pull_request(target: _PullRequestProbeTarget) -> bool:
    from ..repository.publish import (
        GitHubCliPullRequestClient,
        PublicationError,
    )

    try:
        observations = GitHubCliPullRequestClient(Path.cwd()).observe(
            repository=target.repository,
            base_ref=target.base_ref,
            head_ref=target.head_ref,
            candidate_commit=target.candidate_commit,
        )
    except (
        OSError,
        subprocess.SubprocessError,
        UnicodeError,
        ValueError,
        PublicationError,
    ):
        return False
    if len(observations) != 1:
        return False
    observation = observations[0]
    if (
        observation.state != "OPEN"
        or observation.base_ref != target.base_ref
        or observation.head_ref != target.head_ref
        or observation.head_oid != target.candidate_commit
        or observation.title != target.title
        or observation.body != target.body
    ):
        return False
    expected = target.expected_pr
    return expected is None or (
        observation.number == expected.get("number")
        and observation.url == expected.get("url")
        and observation.head_oid == expected.get("head_oid")
    )


def _wakeup(wait: StageEffectWait, changed: bool) -> EffectWakeupHint:
    from ..pipeline.application import EffectWakeupHint

    return EffectWakeupHint(
        wait_digest=wait.digest,
        observed_at=datetime.now(UTC),
        changed=changed,
    )


async def _prepare_native_effect_probe(
    wait: StageEffectWait,
) -> EffectWaitProbe:
    from .native_operation_client import NativeOperationNotFound

    client, expected = await asyncio.to_thread(
        _native_probe_from_environment,
        wait,
    )

    async def probe(value: StageEffectWait) -> EffectWakeupHint:
        if value != wait:
            raise ValueError("effect status probe names a different wait")
        try:
            view = await asyncio.to_thread(
                client.get_operation,
                expected.operation_id,
            )
        except NativeOperationNotFound:
            view = None
        if view is not None:
            expected.require(view)
        return _wakeup(wait, _native_wakeable(view))

    return probe


async def _prepare_git_effect_probe(
    wait: StageEffectWait,
) -> EffectWaitProbe:
    evidence = await asyncio.to_thread(_load_effect_evidence, wait)
    if evidence.intent.effect_kind != "git_ref_update":
        raise ValueError("The pending effect is not a Git ref update")
    target = await asyncio.to_thread(_git_probe_target, evidence)

    async def probe(value: StageEffectWait) -> EffectWakeupHint:
        if value != wait:
            raise ValueError("effect status probe names a different wait")
        changed = await asyncio.to_thread(_observe_git_branch, target)
        return _wakeup(wait, changed)

    return probe


async def _prepare_pull_request_effect_probe(
    wait: StageEffectWait,
) -> EffectWaitProbe:
    evidence = await asyncio.to_thread(_load_effect_evidence, wait)
    if evidence.intent.effect_kind != "pr_upsert":
        raise ValueError("The pending effect is not a pull request upsert")
    target = await asyncio.to_thread(_pull_request_probe_target, evidence)

    async def probe(value: StageEffectWait) -> EffectWakeupHint:
        if value != wait:
            raise ValueError("effect status probe names a different wait")
        changed = await asyncio.to_thread(_observe_pull_request, target)
        return _wakeup(wait, changed)

    return probe


async def _prepare_effect_probe(wait: StageEffectWait) -> EffectWaitProbe:
    if wait.effect_kind == "native_operation":
        return await _prepare_native_effect_probe(wait)
    if wait.effect_kind == "git_ref_update":
        return await _prepare_git_effect_probe(wait)
    if wait.effect_kind == "pr_upsert":
        return await _prepare_pull_request_effect_probe(wait)
    raise ValueError(
        "The pending effect kind has no read-only wake-up observer"
    )


async def probe_effect_wait(wait: StageEffectWait) -> EffectWakeupHint:
    """Perform one read-only effect observation for a canonical wait."""

    probe = await _prepare_effect_probe(wait)
    return await probe(wait)


class EffectPollTrigger(BaseTrigger):
    """Observe one exact effect without acquiring or changing platform authority."""

    def __init__(
        self,
        *,
        wait: Mapping[str, object],
        poll_interval: float = 5.0,
        probe: EffectWaitProbe | None = None,
    ) -> None:
        super().__init__()
        self.wait = StageEffectWait.from_record(wait).to_record()
        self.poll_interval = _minimum_poll_interval(poll_interval)
        self._probe = probe

    def serialize(self) -> tuple[str, dict[str, Any]]:
        return (
            "description_pipeline.orchestration.airflow_effect_trigger."
            "EffectPollTrigger",
            {
                "wait": dict(self.wait),
                "poll_interval": self.poll_interval,
            },
        )

    async def run(self) -> AsyncIterator[TriggerEvent]:
        wait = StageEffectWait.from_record(self.wait)
        expected_digest = wait.digest
        probe = (
            await _prepare_effect_probe(wait)
            if self._probe is None
            else self._probe
        )
        while True:
            hint = await probe(wait)
            if hint.wait_digest != expected_digest:
                raise ValueError(
                    "effect status probe returned a different wait identity"
                )
            if hint.changed:
                yield TriggerEvent({"wait": wait.to_record()})
                return
            await asyncio.sleep(self.poll_interval)


__all__ = [
    "EffectPollTrigger",
    "EffectWaitProbe",
    "probe_effect_wait",
]
