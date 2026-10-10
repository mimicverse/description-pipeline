"""The authoritative Linux attempt store for the portable half of the split.

One immutable directory per attempt holds the admitted native capture, the staged
portable checkpoints (generate, verify), the published output and the merged event
stream the portal serves.  Attempts are never rewritten; a linked rerun is a new
attempt that reads its parent only as a seed.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from ..delivery import PIPELINE_ID, subject_digest, subject_inventory
from ..io import PipelineError, confined, file_digest, read_data, write_json
from ..stages import STAGE_IDS, stage_view
from ..verification.solidworks_urdf import check_bundle, require_qualified_report
from .airflow_client import JOB_SCHEMA, validate_run_id
from .stage_transfer import admit_capture

STORE_SCHEMA = "solidworks-to-urdf.linux-run/v1"
PORTABLE_STAGES = ("generate", "verify", "publish")


class LinuxStore:
    """Attempt-scoped, immutable Linux store for the portable engineering stages."""

    def __init__(self, root: Path):
        root = Path(root)
        if not root.is_absolute():
            raise PipelineError("Linux store root must be an absolute path")
        self.root = root

    # ------------------------------------------------------------------ paths
    def run_dir(self, run_id: str) -> Path:
        return self.root / validate_run_id(run_id)

    def capture_dir(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "capture"

    def stage_dir(self, run_id: str, stage: str) -> Path:
        if stage not in STAGE_IDS:
            raise PipelineError(f"Unknown engineering stage: {stage!r}")
        return self.run_dir(run_id) / "stages" / stage

    def output_dir(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "output"

    def receipt_path(self, run_id: str, stage: str) -> Path:
        return self.run_dir(run_id) / "receipts" / f"{stage}.json"

    def events_path(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "events.json"

    def meta_path(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "meta.json"

    def delivery_dir(self, run_id: str) -> Path | None:
        """The newest installed delivery of this attempt (publish, verify or generate)."""
        candidates = (
            self.output_dir(run_id),
            self.stage_dir(run_id, "verify"),
            self.stage_dir(run_id, "generate"),
        )
        for candidate in candidates:
            if (candidate / "input").is_dir() and (candidate / "reports").is_dir():
                return candidate
        return None

    # ------------------------------------------------------------------ state
    def meta(self, run_id: str) -> dict | None:
        path = self.meta_path(run_id)
        return read_data(path) if path.is_file() else None

    def events(self, run_id: str) -> list[dict]:
        path = self.events_path(run_id)
        if not path.is_file():
            return []
        payload = read_data(path)
        return [item for item in payload if isinstance(item, dict)] if isinstance(payload, list) else []

    def _write(self, path: Path, payload) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(  # noqa: SIM115 - closed explicitly in the finally path
            prefix=".store-", dir=path.parent, delete=False
        )
        try:
            with handle:
                write_json(Path(handle.name), payload)
            os.replace(handle.name, path)
        except BaseException:
            Path(handle.name).unlink(missing_ok=True)
            raise

    def append_events(self, run_id: str, events) -> None:
        """Append raw events, preserving timestamps; identical records are never duplicated.

        Deduplication compares the complete event content: distinct checks may share
        one timestamp and must both survive.
        """
        existing = self.events(run_id)
        seen = {json.dumps(item, sort_keys=True, ensure_ascii=False) for item in existing}
        merged = list(existing)
        for item in events or []:
            if not isinstance(item, dict):
                continue
            canonical = json.dumps(item, sort_keys=True, ensure_ascii=False)
            if canonical in seen:
                continue
            seen.add(canonical)
            merged.append(item)
        if len(merged) != len(existing):
            self._write(self.events_path(run_id), merged)

    # ------------------------------------------------------------- lifecycle
    def import_capture(
        self,
        run_id: str,
        archive: Path,
        *,
        expected_handoff_sha256: str,
        expected_main_assembly: str | None,
        expected_native_tool: dict,
    ) -> dict:
        """Admit one sealed native capture and seed the raw native events.

        Retrying the identical fetch is idempotent: a committed admission returns its
        recorded metadata, and a crash between capture install and metadata write is
        recovered from the atomic capture install.  A differing request for the same
        attempt is refused.
        """
        run_id = validate_run_id(run_id)

        def finalize(meta: dict) -> dict:
            capture = self.capture_dir(run_id)
            native_receipt = capture / "reports/native-stages.json"
            if native_receipt.is_file():
                payload = read_data(native_receipt)
                if isinstance(payload, dict):
                    self.append_events(run_id, payload.get("events"))
            admitted = {**meta, "state": "capture_admitted"}
            self._write(self.meta_path(run_id), admitted)
            return admitted

        existing = self.meta(run_id)
        if isinstance(existing, dict):
            if (
                existing.get("handoff_sha256") == expected_handoff_sha256
                and existing.get("main_assembly") == expected_main_assembly
                and existing.get("native_tool") == expected_native_tool
                and self.capture_dir(run_id).is_dir()
            ):
                return finalize(existing)
            raise PipelineError("This attempt already admitted a different native capture")
        if self.capture_dir(run_id).exists():
            raise PipelineError("A capture exists without store metadata; review this attempt manually")
        provisional = {
            "schema_version": STORE_SCHEMA,
            "run_id": run_id,
            "state": "importing",
            "handoff_sha256": expected_handoff_sha256,
            "main_assembly": expected_main_assembly,
            "native_tool": expected_native_tool,
        }
        self._write(self.meta_path(run_id), provisional)
        manifest = admit_capture(
            archive,
            self.capture_dir(run_id),
            expected_run_id=run_id,
            expected_handoff_sha256=expected_handoff_sha256,
            expected_main_assembly=expected_main_assembly,
            expected_native_tool=expected_native_tool,
        )
        native_receipt = read_data(self.capture_dir(run_id) / "reports/native-stages.json")
        if isinstance(native_receipt, dict):
            self.append_events(run_id, native_receipt.get("events"))
        meta = {
            **provisional,
            "transfer": dict(manifest),
        }
        return finalize(meta)

    def record_stage(self, run_id: str, stage: str, receipt: dict) -> None:
        """Keep one stage receipt and its raw events; the attempt is otherwise untouched."""
        if stage not in PORTABLE_STAGES:
            raise PipelineError(f"Not a portable stage: {stage!r}")
        self._write(self.receipt_path(run_id, stage), receipt)
        self.append_events(run_id, receipt.get("events"))
        meta = self.meta(run_id) or {"schema_version": STORE_SCHEMA, "run_id": run_id}
        meta.update(
            state=receipt.get("state") or meta.get("state"),
            passed=bool(receipt.get("passed")),
            subject_sha256=receipt.get("subject_sha256") or meta.get("subject_sha256"),
            last_stage=stage,
            error=receipt.get("error"),
        )
        self._write(self.meta_path(run_id), meta)

    # ------------------------------------------------------------- serving
    def merged_job(self, run_id: str, native_job: dict | None = None) -> dict:
        """One job-shaped snapshot the existing stage view and portal can render."""
        run_id = validate_run_id(run_id)
        meta = self.meta(run_id) or {}
        delivery = self.delivery_dir(run_id)
        quality_path = delivery / "reports/quality.json" if delivery else None
        submission_path = delivery / "reports/pr.json" if delivery else None
        quality = read_data(quality_path) if quality_path and quality_path.is_file() else None
        submission = read_data(submission_path) if submission_path and submission_path.is_file() else None
        subject = meta.get("subject_sha256") or (quality or {}).get("subject_sha256")
        events = self.events(run_id)
        for item in (native_job or {}).get("events") or []:
            if item not in events:
                events.append(item)
        submission_state = (submission or {}).get("state")
        submission_bound = (
            submission_state in {"published", "updated", "noop"}
            and bool((submission or {}).get("url"))
            and (submission or {}).get("subject_sha256") == subject
        )
        result = {
            "passed": bool(submission_bound and (quality or {}).get("passed")),
            "quality": quality or {},
            "subject_sha256": subject,
            "handoff_sha256": meta.get("handoff_sha256"),
        }
        if submission is not None:
            result["submission"] = submission
        if (native_job or {}).get("status") == "failed":
            return {**native_job, "run_id": run_id, "events": events}
        status = "passed" if result["passed"] else ("failed" if meta.get("state") == "failed" else "running")
        return {
            "schema_version": JOB_SCHEMA,
            "pipeline_id": PIPELINE_ID,
            "run_id": run_id,
            "status": status,
            "request": {"handoff_sha256": meta.get("handoff_sha256"), "main_assembly": meta.get("main_assembly")},
            "events": events,
            "result": result,
            "error": meta.get("error"),
            "repository_slug": (native_job or {}).get("repository_slug"),
            "repository_base": (native_job or {}).get("repository_base"),
        }

    def view(self, run_id: str, native_job: dict | None = None) -> dict:
        return stage_view(self.merged_job(run_id, native_job))

    def preview(self, run_id: str) -> dict:
        """Digest-bound preview of the newest verified delivery.

        The saved report is revalidated against the delivery bytes before anything is
        served, and the served subject is the verified report's own binding.
        """
        delivery = self.delivery_dir(run_id)
        if delivery is None:
            raise PipelineError("This attempt has no delivery to preview yet")
        report = require_qualified_report(check_bundle(delivery))
        subject = report.get("subject_sha256")
        if not isinstance(subject, str) or subject != subject_digest(delivery):
            raise PipelineError("The verified report is not bound to the current delivery bytes")
        files = subject_inventory(delivery)
        urdf = next((name for name in ("urdf/robot.urdf",) if name in files), None)
        if urdf is None:
            raise PipelineError("The delivery carries no urdf/robot.urdf artifact")
        return {
            "schema_version": "solidworks-to-urdf.preview/v1",
            "pipeline_id": PIPELINE_ID,
            "run_id": run_id,
            "subject_sha256": subject,
            "urdf": urdf,
            "files": files,
        }

    def open_artifact(self, run_id: str, name: str, *, sha256: str):
        """Open one previewed artifact whose bytes still match the previewed digest."""
        delivery = self.delivery_dir(run_id)
        if delivery is None:
            raise PipelineError("This attempt has no delivery to serve yet")
        preview = self.preview(run_id)
        expected = preview["files"].get(name)
        if expected is None or expected != sha256:
            raise PipelineError("The requested artifact is not part of the verified preview")
        path = confined(delivery, name)
        if file_digest(path) != expected:
            raise PipelineError("The artifact bytes changed after verification; refusing to serve")
        return path.open("rb"), path.stat().st_size  # noqa: SIM115 - the caller closes the stream
