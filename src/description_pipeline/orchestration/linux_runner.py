"""Portable continuation: drive generate, verify and publish for one admitted capture.

The three stages run sequentially inside one attempt, each producing its own
checkpoint (generate: unverified, verify: verified, publish: submission) and
seeding the next one from the previous directory, never from its own output.
The portable runtime must prove the same source/release identity as the native
tool record and carry the pinned MuJoCo before generation and before publication.
"""

from __future__ import annotations

from pathlib import Path

from .. import solidworks
from ..io import PipelineError
from ..runtime import RUNTIME_VERSIONS, tool_record
from .airflow_client import capture_archive_metadata, validate_run_id
from .linux_store import PORTABLE_STAGES, LinuxStore
from .stage_transfer import CAPTURE_ARCHIVE


def _portable_identity_gate(store: LinuxStore, run_id: str) -> dict:
    """Fail closed unless the portable runtime matches the native capture's identity."""
    meta = store.meta(run_id) or {}
    native_tool = meta.get("native_tool")
    if not isinstance(native_tool, dict) or not native_tool:
        raise PipelineError("The admitted capture carries no native tool record")
    portable = tool_record(role="portable")
    if portable.get("runtime", {}).get("role") != "portable":
        raise PipelineError("The portable runtime did not identify itself as role=portable")
    for key in ("pipeline_id", "version", "source_sha256"):
        if portable.get(key) != native_tool.get(key):
            raise PipelineError(f"The portable runtime differs from the native capture in {key}; refusing to continue")
    if portable.get("release") != native_tool.get("release"):
        raise PipelineError("The portable release identity differs from the native capture; refusing to continue")
    pin = RUNTIME_VERSIONS.get("mujoco")
    if (portable.get("runtime", {}).get("packages") or {}).get("mujoco") != pin:
        raise PipelineError("The portable runtime does not carry the pinned MuJoCo")
    return portable


def run_portable_stage(
    store: LinuxStore,
    run_id: str,
    stage: str,
    *,
    repository: Path | None = None,
    base: str | None = None,
    expected_subject: str | None = None,
    parent_run: str | None = None,
    on_event=None,
) -> dict:
    """Run one portable stage to its boundary and record the receipt in the store."""
    run_id = validate_run_id(run_id)
    if stage not in PORTABLE_STAGES:
        raise PipelineError(f"stage must be one of {PORTABLE_STAGES}: {stage!r}")
    meta = store.meta(run_id)
    if not isinstance(meta, dict) or not store.capture_dir(run_id).is_dir():
        raise PipelineError("This run has no admitted native capture; fetch the transfer first")
    if stage == "generate" and expected_subject is not None:
        raise PipelineError("generate does not take an expected subject")
    if stage in {"verify", "publish"} and not expected_subject:
        raise PipelineError(f"{stage} requires the recorded subject of the previous checkpoint")
    if stage == "publish" and repository is None:
        raise PipelineError("publish requires the configured model repository checkout")
    if stage in {"generate", "publish"}:
        _portable_identity_gate(store, run_id)
    seeds = {
        "generate": store.capture_dir(run_id),
        "verify": store.stage_dir(run_id, "generate"),
        "publish": store.stage_dir(run_id, "verify"),
    }
    outputs = {
        "generate": store.stage_dir(run_id, "generate"),
        "verify": store.stage_dir(run_id, "verify"),
        "publish": store.output_dir(run_id),
    }
    resume = {"parent_run": validate_run_id(parent_run), "from_stage": stage} if parent_run else None

    def sink(event: dict) -> None:
        # Live Linux progress is persisted immediately, not only in the final receipt.
        store.append_events(run_id, [event])
        if on_event is not None:
            on_event(event)

    with solidworks.output_lock(store.run_dir(run_id)):
        receipt = solidworks.run(
            store.capture_dir(run_id),
            outputs[stage],
            repository=Path(repository) if stage == "publish" else None,
            base=base,
            run_id=run_id,
            resume_from=stage,
            stop_after=stage,
            seed_dir=seeds[stage],
            expected_subject=expected_subject,
            handoff_sha256=meta.get("handoff_sha256"),
            prior_events=store.events(run_id),
            resume=resume,
            on_event=sink,
        )
    store.record_stage(run_id, stage, receipt)
    return receipt


def fetch_capture(store: LinuxStore, endpoint, run_id: str, job: dict) -> dict:
    """Download the sealed native capture and admit it into the Linux store."""
    run_id = validate_run_id(run_id)
    archive = capture_archive_metadata(job)
    result = job.get("result") if isinstance(job.get("result"), dict) else {}
    native_tool = result.get("native_tool")
    if not isinstance(native_tool, dict) or not native_tool:
        raise PipelineError("The native job carries no tool record; the transfer cannot be verified")
    request = job.get("request") if isinstance(job.get("request"), dict) else {}
    handoff = request.get("handoff_sha256")
    if not isinstance(handoff, str) or not handoff:
        raise PipelineError("The native job carries no frozen handoff digest")
    main_assembly = job.get("main_assembly") or request.get("main_assembly")
    store.run_dir(run_id).mkdir(parents=True, exist_ok=True)
    archive_path = store.run_dir(run_id) / CAPTURE_ARCHIVE
    endpoint.stream_capture_archive(run_id, archive, archive_path)
    meta = store.import_capture(
        run_id,
        archive_path,
        expected_handoff_sha256=handoff,
        expected_main_assembly=main_assembly,
        expected_native_tool=native_tool,
    )
    return {
        "run_id": run_id,
        "store_root": str(store.root),
        "capture_dir": str(store.capture_dir(run_id)),
        "handoff_sha256": handoff,
        "main_assembly": main_assembly,
        "state": meta.get("state"),
    }
