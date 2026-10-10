"""Portable continuation: drive generate, verify and publish for one admitted capture.

The three stages run sequentially inside one attempt, each producing its own
checkpoint (generate: unverified, verify: verified, publish: submission) and
seeding the next one from the previous directory, never from its own output.
The portable runtime must prove the same source/release identity as the native
tool record and carry the pinned MuJoCo before generation and before publication.
"""

from __future__ import annotations

from pathlib import Path

from packaging.utils import canonicalize_name

from .. import solidworks
from ..io import PipelineError, file_digest, read_data
from ..runtime import RUNTIME_VERSIONS, required_packages, tool_record
from ..stages import STAGE_IDS
from .airflow_client import capture_archive_metadata, validate_run_id
from .linux_store import PORTABLE_STAGES, LinuxStore
from .stage_transfer import CAPTURE_ARCHIVE, CAPTURE_MANIFEST, verify_transfer


def _portable_identity_gate(store: LinuxStore, run_id: str) -> dict:
    """Fail closed unless the portable runtime matches the native capture's identity."""
    meta = store.meta(run_id) or {}
    native_tool = meta.get("native_tool")
    if not isinstance(native_tool, dict) or not native_tool:
        raise PipelineError("The admitted capture carries no native tool record")
    portable = tool_record(role="portable")
    runtime = portable.get("runtime") or {}
    if runtime.get("role") != "portable":
        raise PipelineError("The portable runtime did not identify itself as role=portable")
    if runtime.get("system") != "Linux":
        raise PipelineError("The portable runtime must execute on Linux")
    if not str(runtime.get("python") or "").startswith("3.12"):
        raise PipelineError("The portable runtime must execute on the pinned Python 3.12")
    for key in ("pipeline_id", "version", "source_sha256"):
        if portable.get(key) != native_tool.get(key):
            raise PipelineError(f"The portable runtime differs from the native capture in {key}; refusing to continue")
    if portable.get("release") != native_tool.get("release"):
        raise PipelineError("The portable release identity differs from the native capture; refusing to continue")
    packages = {canonicalize_name(str(name)): value for name, value in (runtime.get("packages") or {}).items()}
    for name in required_packages("portable"):
        if packages.get(canonicalize_name(name)) != RUNTIME_VERSIONS.get(name):
            raise PipelineError(f"The portable runtime does not carry the pinned {name}")
    return portable


def _revalidate_capture(store: LinuxStore, run_id: str) -> None:
    """Revalidate the admitted payload against its sealed transfer at point of use."""
    meta = store.meta(run_id) or {}
    recorded = meta.get("transfer") or {}
    capture = store.capture_dir(run_id)
    summary = verify_transfer(capture)
    if not isinstance(summary, dict):
        raise PipelineError("The admitted capture carries no sealed transfer to revalidate")
    for key in ("run_id", "handoff_sha256", "main_assembly"):
        if summary.get(key) != recorded.get(key):
            raise PipelineError(f"Admitted capture provenance differs from the recorded admission: {key}")
    manifest = read_data(capture / CAPTURE_MANIFEST)
    if (
        not isinstance(manifest, dict)
        or manifest.get("files") != recorded.get("files")
        or manifest.get("file_count") != recorded.get("file_count")
        or manifest.get("total_bytes") != recorded.get("total_bytes")
    ):
        raise PipelineError("Admitted capture manifest differs from the recorded admission")
    if read_data(capture / "reports/native-tool.json") != recorded.get("native_tool"):
        raise PipelineError("Admitted capture native tool record differs from the recorded admission")


def _failure_receipt(store: LinuxStore, run_id: str, stage: str, error: Exception) -> dict:
    return {
        "run_id": run_id,
        "state": "failed",
        "passed": False,
        "stage": stage,
        "error": f"{type(error).__name__}: {error}",
        "error_code": getattr(error, "code", None),
        "detail": getattr(error, "detail", None) or getattr(error, "details", None),
        "events": store.events(run_id),
    }


def run_portable_stage(
    store: LinuxStore,
    run_id: str,
    stage: str,
    *,
    repository: Path | None = None,
    base: str | None = None,
    expected_subject: str | None = None,
    parent_run: str | None = None,
    source_run_id: str | None = None,
    on_event=None,
) -> dict:
    """Run one portable stage to its boundary and record the receipt in the store."""
    run_id = validate_run_id(run_id)
    if stage not in PORTABLE_STAGES:
        raise PipelineError(f"stage must be one of {PORTABLE_STAGES}: {stage!r}")
    owner = validate_run_id(source_run_id or parent_run) if source_run_id or parent_run else run_id
    outputs = {
        "generate": store.stage_dir(run_id, "generate"),
        "verify": store.stage_dir(run_id, "verify"),
        "publish": store.output_dir(run_id),
    }

    def sink(event: dict) -> None:
        # Live Linux progress is persisted immediately, not only in the final receipt.
        store.append_events(run_id, [event])
        if on_event is not None:
            on_event(event)

    with solidworks.output_lock(store.run_dir(run_id)):
        try:
            meta = store.meta(owner)
            if not isinstance(meta, dict):
                raise PipelineError("This run has no admitted native capture; fetch the transfer first")
            current = store.meta(run_id) or {}
            if owner != run_id:
                if current.get("source_run_id") not in {None, owner}:
                    raise PipelineError("A linked attempt cannot change its parent")
                boundary = current.get("resume_from") or stage
                store.update_meta(
                    run_id,
                    source_run_id=owner,
                    resume_from=boundary,
                    **{key: meta.get(key) for key in (
                        "handoff_sha256", "main_assembly", "native_tool", "repository_slug", "repository_base"
                    )},
                )
                resume = {"parent_run": owner, "from_stage": boundary}
            else:
                boundary, resume = None, None
            if stage == "generate" and expected_subject is not None:
                raise PipelineError("generate does not take an expected subject")
            if stage in {"verify", "publish"} and not expected_subject:
                raise PipelineError(f"{stage} requires the recorded subject of the previous checkpoint")
            if stage == "publish" and repository is None:
                raise PipelineError("publish requires the configured model repository checkout")
            capture_owner = store.checkpoint_owner(run_id, "capture")
            capture = store.capture_dir(capture_owner)
            _revalidate_capture(store, capture_owner)
            _portable_identity_gate(store, capture_owner)
            seed_stage = {"generate": "capture", "verify": "generate", "publish": "verify"}[stage]
            seed = store.checkpoint_dir(run_id, seed_stage)
            if owner != run_id and boundary == stage:
                _seed_reuse(store, run_id, owner, stage)
            receipt = solidworks.run(
                capture,
                outputs[stage],
                repository=Path(repository) if stage == "publish" else None,
                base=base,
                run_id=run_id,
                resume_from=stage,
                stop_after=stage,
                seed_dir=seed,
                expected_subject=expected_subject,
                handoff_sha256=meta.get("handoff_sha256"),
                prior_events=store.events(run_id),
                resume=resume,
                on_event=sink,
            )
        except Exception as error:
            store.record_stage(run_id, stage, _failure_receipt(store, run_id, stage, error))
            raise
        store.record_stage(run_id, stage, receipt)
    return receipt


def _seed_reuse(store: LinuxStore, run_id: str, owner: str, stage: str) -> None:
    """Copy the parent's raw events for the stages this attempt resumes past, marked reused.

    Timestamps and check records are preserved verbatim: a linked attempt renders the
    parent's evidence as reused instead of fabricating new passes.
    """
    prefix = set(STAGE_IDS[: STAGE_IDS.index(stage)])
    seeded = [
        {**event, "reuse": {
            "parent_run": owner,
            "source_run": (event.get("reuse") or {}).get("source_run", owner),
            "reused": True,
        }}
        for event in store.events(owner)
        if isinstance(event, dict) and event.get("stage") in prefix
    ]
    store.append_events(run_id, seeded)


def fetch_capture(store: LinuxStore, endpoint, run_id: str, job: dict) -> dict:
    """Download the sealed native capture and admit it into the Linux store."""
    run_id = validate_run_id(run_id)
    result = job.get("result") if isinstance(job.get("result"), dict) else {}
    native_tool = result.get("native_tool")
    if not isinstance(native_tool, dict) or not native_tool:
        raise PipelineError("The native job carries no tool record; the transfer cannot be verified")
    request = job.get("request") if isinstance(job.get("request"), dict) else {}
    handoff = request.get("handoff_sha256")
    if not isinstance(handoff, str) or not handoff:
        raise PipelineError("The native job carries no frozen handoff digest")
    main_assembly = job.get("main_assembly") or request.get("main_assembly")
    meta = store.meta(run_id)
    if (
        isinstance(meta, dict)
        and meta.get("state") == "capture_admitted"
        and meta.get("handoff_sha256") == handoff
        and meta.get("main_assembly") == main_assembly
        and meta.get("native_tool") == native_tool
        and store.capture_dir(run_id).is_dir()
    ):
        # A committed identical admission is returned without re-downloading the archive.
        return {
            "run_id": run_id,
            "store_root": str(store.root),
            "capture_dir": str(store.capture_dir(run_id)),
            "handoff_sha256": handoff,
            "main_assembly": main_assembly,
            "state": meta.get("state"),
        }
    archive = capture_archive_metadata(job)
    store.run_dir(run_id).mkdir(parents=True, exist_ok=True)
    archive_path = store.run_dir(run_id) / CAPTURE_ARCHIVE
    endpoint.stream_capture_archive(run_id, archive, archive_path)
    if file_digest(archive_path) != archive["sha256"] or archive_path.stat().st_size != archive["size"]:
        raise PipelineError("The downloaded capture archive does not match its declared receipt")
    meta = store.import_capture(
        run_id,
        archive_path,
        expected_handoff_sha256=handoff,
        expected_main_assembly=main_assembly,
        expected_native_tool=native_tool,
    )
    slug = job.get("repository_slug")
    base = job.get("repository_base")
    store.update_meta(run_id, repository_slug=slug, repository_base=base)
    return {
        "run_id": run_id,
        "store_root": str(store.root),
        "capture_dir": str(store.capture_dir(run_id)),
        "handoff_sha256": handoff,
        "main_assembly": main_assembly,
        "state": meta.get("state"),
        "repository_slug": slug,
        "repository_base": base,
    }
