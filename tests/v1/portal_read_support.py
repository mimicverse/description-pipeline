"""Projection adapter: the locked PlatformStore read surface over a real LinuxStore fixture.

Tests own real LinuxStore fixtures; the portal reads through the locked platform
interfaces, so this adapter projects the fixture's bound preview and checkpoints
into ``run_record``/``stage_records``/``sealed_path`` without touching production.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from description_pipeline.io import PipelineError
from description_pipeline.orchestration.linux_store import LinuxStore


class StoreProjection:
    """One LinuxStore fixture served through the registered read projection."""

    def __init__(self, store: LinuxStore) -> None:
        self.store = store
        self.root = store.root

    # ---------------------------------------------------------- admission face
    def admission(self):
        return SimpleNamespace(state="open")

    def admit_run(self, **kwargs) -> None:
        return None

    def run_displays(self):
        return []

    def run_display(self, run_id: str):
        return None

    def set_run_display(self, run_id: str, title: str | None):
        return None

    # ---------------------------------------------------- registered read face
    def run_record(self, run_id: str) -> dict | None:
        meta = self.store.meta(run_id)
        if not isinstance(meta, dict):
            return None
        state = meta.get("state")
        projected = {"published": "succeeded"}.get(str(state), state) if isinstance(state, str) else None
        return {
            "run_id": run_id,
            "state": projected,
            "request_id": None,
            "parent_run_id": None,
            "from_stage": None,
            "tool_identity": None,
        }

    def _checkpoint_row(self, run_id: str, stage: str) -> dict | None:
        try:
            self.store.checkpoint_dir(run_id, stage)
        except PipelineError:
            return None
        return {
            "stage": stage,
            "state": "completed",
            "receipt": {"subject_sha256": None, "artifacts": []},
            "checkpoint_uri": f"store://sealed/{run_id}/{stage}",
            "reused_from_run": None,
            "original_executed_at": None,
        }

    def stage_records(self, run_id: str) -> dict[str, dict]:
        rows: dict[str, dict] = {}
        for stage in ("capture", "generate", "publish", "verify"):
            row = self._checkpoint_row(run_id, stage)
            if row is not None:
                rows[stage] = row
        try:
            root, preview = self.store._bound_preview(run_id)
        except PipelineError:
            return rows
        files = {**(preview.get("files") or {}), **(preview.get("report_files") or {})}
        artifacts = []
        for path, digest in sorted(files.items()):
            try:
                size = (Path(root) / path).stat().st_size
            except OSError:
                continue
            artifacts.append({"path": path, "bytes": size, "sha256": digest})
        verified = preview.get("verified") is not False
        rows["verify"] = {
            "stage": "verify",
            "state": "completed" if verified else "failed",
            "receipt": {
                "subject_sha256": preview.get("subject_sha256"),
                "artifacts": artifacts,
                "error": preview.get("error"),
            },
            "checkpoint_uri": f"store://sealed/{run_id}/verify",
            "reused_from_run": None,
            "original_executed_at": None,
        }
        return rows

    def sealed_path(self, checkpoint_uri: str):
        prefix = "store://sealed/"
        if not isinstance(checkpoint_uri, str) or not checkpoint_uri.startswith(prefix):
            raise PipelineError("invalid sealed reference")
        parts = checkpoint_uri.removeprefix(prefix).split("/")
        if len(parts) != 2 or not all(parts):
            raise PipelineError("invalid sealed reference")
        run_id, stage = parts
        if stage == "verify":
            root, _preview = self.store._bound_preview(run_id)
            return root
        return self.store.checkpoint_dir(run_id, stage)

    def run_events(self, run_id: str, *, after_seq: int = 0, limit: int = 200) -> list[dict]:
        meta = self.store.meta(run_id) or {}
        events = meta.get("events")
        return list(events) if isinstance(events, list) else []
