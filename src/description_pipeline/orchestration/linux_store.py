"""The authoritative Linux attempt store for the portable half of the split.

One immutable directory per attempt holds the admitted native capture, the staged
portable checkpoints (generate, verify), the published output and the merged event
stream the portal serves.  Attempts are never rewritten; a linked rerun is a new
attempt that reads its parent only as a seed.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path

from ..delivery import PIPELINE_ID, subject_digest, subject_inventory
from ..io import PipelineError, confined, read_data, write_json
from ..stages import STAGE_IDS, stage_view
from ..verification.solidworks_urdf import check_bundle, require_qualified_report
from .airflow_client import JOB_SCHEMA, validate_run_id
from .stage_transfer import CAPTURE_MANIFEST, admit_capture

STORE_SCHEMA = "solidworks-to-urdf.linux-run/v1"
PORTABLE_STAGES = ("generate", "verify", "publish")


class LinuxStore:
    """Attempt-scoped, immutable Linux store for the portable engineering stages."""

    def __init__(self, root: Path):
        root = Path(root)
        if not root.is_absolute():
            raise PipelineError("Linux store root must be an absolute path")
        self.root = root
        #: Verified previews per immutable checkpoint, keyed by path identity and mtime.
        self._previews: dict[tuple[str, int, int], dict] = {}
        #: The last verified preview per delivery that artifact serving may trust.
        self._bound_previews: dict[str, dict] = {}

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

    def checkpoint_owner(self, run_id: str, stage: str) -> str:
        """Resolve an upstream checkpoint without substituting an older attempt's output.

        A linked attempt inherits only stages before its selected restart boundary.
        Its newly generated or verified files always take precedence thereafter.
        """
        if stage not in {"capture", "generate", "verify"}:
            raise PipelineError(f"Not a reusable checkpoint: {stage!r}")
        current = validate_run_id(run_id)
        seen = set()
        while current not in seen:
            seen.add(current)
            path = self.capture_dir(current) if stage == "capture" else self.stage_dir(current, stage)
            if path.is_dir():
                return current
            meta = self.meta(current) or {}
            parent, boundary = meta.get("source_run_id"), meta.get("resume_from")
            if (
                not parent or parent == current or boundary not in PORTABLE_STAGES
                or STAGE_IDS.index(stage) >= STAGE_IDS.index(boundary)
            ):
                raise PipelineError(f"Attempt {current} has no retained {stage} checkpoint")
            current = validate_run_id(parent)
        raise PipelineError("Linked checkpoint lineage contains a cycle")

    def checkpoint_dir(self, run_id: str, stage: str) -> Path:
        owner = self.checkpoint_owner(run_id, stage)
        return self.capture_dir(owner) if stage == "capture" else self.stage_dir(owner, stage)

    def checkpoint_receipt(self, run_id: str, stage: str) -> Path:
        return self.receipt_path(self.checkpoint_owner(run_id, stage), stage)

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
        if not isinstance(payload, list):
            raise PipelineError("events.json must be a JSON list")
        return [item for item in payload if isinstance(item, dict)]

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

    def update_meta(self, run_id: str, **fields) -> dict:
        """Record attempt-level facts (routing, source lineage) without touching checkpoints."""
        meta = self.meta(run_id) or {"schema_version": STORE_SCHEMA, "run_id": run_id}
        meta.update(fields)
        self._write(self.meta_path(run_id), meta)
        return meta

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
            manifest_path = capture / CAPTURE_MANIFEST
            manifest_payload = meta.get("transfer")
            if not isinstance(manifest_payload, dict):
                if not manifest_path.is_file():
                    raise PipelineError(
                        "The admitted capture has no transfer manifest; review this attempt manually"
                    )
                manifest_payload = read_data(manifest_path)
                if not isinstance(manifest_payload, dict):
                    raise PipelineError("The admitted capture transfer manifest is not an object")
            elif not manifest_path.is_file():
                write_json(manifest_path, manifest_payload)
            native_receipt = capture / "reports/native-stages.json"
            if native_receipt.is_file():
                payload = read_data(native_receipt)
                if isinstance(payload, dict):
                    self.append_events(run_id, payload.get("events"))
            admitted = {**meta, "transfer": manifest_payload, "state": "capture_admitted"}
            self._write(self.meta_path(run_id), admitted)
            return admitted

        existing = self.meta(run_id)
        if isinstance(existing, dict):
            same = (
                existing.get("handoff_sha256") == expected_handoff_sha256
                and existing.get("main_assembly") == expected_main_assembly
                and existing.get("native_tool") == expected_native_tool
            )
            if not same:
                raise PipelineError("This attempt already admitted a different native capture")
            state = existing.get("state")
            if state == "importing":
                if self.capture_dir(run_id).is_dir():
                    return finalize(existing)
                # A crash before the capture install: re-import the identical request below.
            elif state == "capture_admitted":
                if not self.capture_dir(run_id).is_dir():
                    raise PipelineError("The admitted capture is missing; review this attempt manually")
                return finalize(existing)
            else:
                # An advanced attempt (generated/verified/published/failed) is never rolled back.
                return existing
        elif self.capture_dir(run_id).exists():
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
        return finalize({**provisional, "transfer": dict(manifest)})

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
        source = meta.get("source_run_id") or run_id
        capture = self.capture_dir(str(source))
        hardware = _capture_value(capture, "input/robot.yaml", "hardware_id")
        revision = _capture_value(capture, "input/cad-revision.json", "revision")
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
            "hardware_id": hardware,
            "revision": revision,
            "repository_slug": (native_job or {}).get("repository_slug") or meta.get("repository_slug"),
            "repository_base": (native_job or {}).get("repository_base") or meta.get("repository_base"),
        }

    def view(self, run_id: str, native_job: dict | None = None) -> dict:
        return stage_view(self.merged_job(run_id, native_job))

    def preview(self, run_id: str) -> dict:
        """Digest-bound preview of the newest verified delivery.

        The saved report is revalidated against the delivery bytes before anything is
        served, and the served subject is the verified report's own binding.  The
        verification runs once per immutable checkpoint; serving a member never
        re-runs it.
        """
        delivery = self.delivery_dir(run_id)
        if delivery is None:
            raise PipelineError("This attempt has no delivery to preview yet")
        stat = delivery.stat()
        cache_key = (str(delivery), stat.st_ino, _delivery_fingerprint(delivery))
        cached = self._previews.get(cache_key)
        if cached is not None:
            return cached
        report = require_qualified_report(check_bundle(delivery))
        subject = report.get("subject_sha256")
        if not isinstance(subject, str) or subject != subject_digest(delivery):
            raise PipelineError("The verified report is not bound to the current delivery bytes")
        files = subject_inventory(delivery)
        urdf = next((name for name in ("urdf/robot.urdf",) if name in files), None)
        if urdf is None:
            raise PipelineError("The delivery carries no urdf/robot.urdf artifact")
        preview = {
            "schema_version": "solidworks-to-urdf.preview/v1",
            "pipeline_id": PIPELINE_ID,
            "run_id": run_id,
            "subject_sha256": subject,
            "urdf": urdf,
            "files": files,
        }
        self._bound_previews[str(delivery)] = preview
        if len(self._previews) >= 8:
            self._previews.pop(next(iter(self._previews)))
        self._previews[cache_key] = preview
        return preview

    def _bound_preview(self, run_id: str) -> dict:
        """The last verified preview of this delivery; serving never re-runs verification."""
        delivery = self.delivery_dir(run_id)
        if delivery is None:
            raise PipelineError("This attempt has no delivery to serve yet")
        bound = self._bound_previews.get(str(delivery))
        return bound if bound is not None else self.preview(run_id)

    def open_artifact(self, run_id: str, name: str, *, sha256: str):
        """Open one previewed artifact; the returned stream proves the previewed digest.

        The caller-supplied digest is checked against the bound preview inventory, never
        trusted, and the opened bytes are hashed on the same stream that is served.
        """
        delivery = self.delivery_dir(run_id)
        if delivery is None:
            raise PipelineError("This attempt has no delivery to serve yet")
        preview = self._bound_preview(run_id)
        expected = preview["files"].get(name)
        if expected is None or expected != sha256:
            raise PipelineError("The requested artifact is not part of the verified preview")
        path = confined(delivery, name)
        size = path.stat().st_size
        handle = path.open("rb")
        return _BoundArtifact(handle, name=name, expected=expected, size=size), size


class _BoundArtifact:
    """Read-only stream that proves the served bytes are exactly the previewed ones."""

    def __init__(self, handle, *, name: str, expected: str, size: int):
        self._handle = handle
        self._name = name
        self._expected = expected
        self._size = size
        self._digest = hashlib.sha256()
        self._total = 0
        self._done = False

    def read(self, size: int = -1) -> bytes:
        chunk = self._handle.read(size)
        if chunk:
            self._total += len(chunk)
            self._digest.update(chunk)
            return chunk
        if not self._done:
            self._done = True
            if self._total != self._size or self._digest.hexdigest() != self._expected:
                raise PipelineError(f"Served artifact bytes do not match the verified preview: {self._name}")
        return chunk

    def close(self) -> None:
        self._handle.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def __getattr__(self, item):
        return getattr(self._handle, item)


def _capture_value(capture: Path, relative: str, key: str) -> str | None:
    """One scalar from the admitted native input, or ``None`` when it is absent."""
    path = Path(capture) / relative
    if not path.is_file():
        return None
    try:
        payload = read_data(path)
    except (PipelineError, OSError, ValueError, TypeError):
        return None
    value = payload.get(key) if isinstance(payload, dict) else None
    return value if isinstance(value, str) and value else None


def _delivery_fingerprint(delivery: Path) -> tuple:
    """Content identity of a delivery checkpoint: every member's name, size and mtime."""
    rows = []
    for path in sorted(Path(delivery).rglob("*")):
        if path.is_file():
            stat = path.stat()
            rows.append((path.relative_to(delivery).as_posix(), stat.st_size, stat.st_mtime_ns))
    return tuple(rows)
