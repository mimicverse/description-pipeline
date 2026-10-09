"""Durable shared run-history metadata: display rename and reversible soft deletion.

The portal owns this ledger. A rename, tombstone or restore never modifies Airflow DagRuns,
native jobs, receipts, evidence or repository state: deletion only moves a completed record
into the shared Deleted view, and every action is reversible.
"""

from __future__ import annotations

import threading
import unicodedata
from datetime import UTC, datetime
from pathlib import Path

from ..io import PipelineError, read_data, write_json

SCHEMA = "solidworks-to-urdf.run-metadata/v1"
MAX_TITLE = 120
_ENTRY_KEYS = {"title", "deleted", "deleted_at", "deleted_by", "updated_at", "updated_by"}


class RunMetadataError(RuntimeError):
    """The shared run-metadata ledger cannot be read or written safely."""


class TitleError(ValueError):
    """One rejected display title."""


def normalise_title(value: object) -> str | None:
    """One display title: printable text up to ``MAX_TITLE``; ``None`` clears it."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise TitleError("运行名称必须是文本")
    text = value.strip()
    if not text:
        raise TitleError("运行名称不能为空；如需恢复默认名称请传入 null")
    if len(text) > MAX_TITLE:
        raise TitleError(f"运行名称最多 {MAX_TITLE} 个字符")
    if any(unicodedata.category(character).startswith("C") for character in text):
        raise TitleError("运行名称包含不可见的控制字符")
    return text


def _now() -> str:
    return datetime.now(UTC).isoformat()


class RunMetadataStore:
    """One JSON ledger outside the per-run CAD directories; an absent file is an empty ledger.

    Deployments run exactly one portal process; its threads serialize on this lock and every
    operation re-reads the file, so a restart or an external edit can never be silently
    overwritten from a stale cache. Cross-process writers are not supported.
    """

    def __init__(self, path: Path | None):
        self.path = Path(path) if path is not None else None
        self._lock = threading.Lock()

    def _load(self) -> dict[str, dict]:
        if self.path is None:
            raise RunMetadataError("run metadata storage is not configured")
        try:
            data = read_data(self.path)
        except FileNotFoundError:
            data = {"schema_version": SCHEMA, "runs": {}}
        except (PipelineError, OSError, ValueError, TypeError) as error:
            raise RunMetadataError(f"run-metadata ledger is unreadable: {error}") from error
        runs = data.get("runs") if isinstance(data, dict) else None
        if not isinstance(data, dict) or data.get("schema_version") != SCHEMA or not isinstance(runs, dict):
            raise RunMetadataError("unsupported run-metadata ledger")
        clean: dict[str, dict] = {}
        for key, entry in runs.items():
            if not isinstance(key, str) or not isinstance(entry, dict):
                raise RunMetadataError("run-metadata ledger holds an invalid entry")
            clean[key] = self._validated_entry(entry)
        return clean

    @staticmethod
    def _validated_entry(entry: dict) -> dict:
        """One stored overlay; anything unexpected fails the whole ledger closed."""
        if set(entry) - _ENTRY_KEYS:
            raise RunMetadataError("run-metadata entry carries unknown fields")
        title = entry.get("title")
        if title is not None:
            try:
                normalised = normalise_title(title)
            except TitleError as error:
                raise RunMetadataError(f"run-metadata title is invalid: {error}") from error
            if normalised != title:
                raise RunMetadataError("run-metadata title is not stored in normalised form")
        deleted = entry.get("deleted", False)
        if not isinstance(deleted, bool):
            raise RunMetadataError("run-metadata deleted flag is not boolean")
        for field in ("deleted_at", "deleted_by", "updated_at", "updated_by"):
            value = entry.get(field)
            if value is not None and not isinstance(value, str):
                raise RunMetadataError(f"run-metadata {field} is not text")
        if deleted and not entry.get("deleted_at"):
            raise RunMetadataError("run-metadata tombstone lacks its timestamp")
        return dict(entry)

    def _save(self, runs: dict[str, dict]) -> None:
        if self.path is None:
            raise RunMetadataError("run metadata storage is not configured")
        try:
            write_json(self.path, {"schema_version": SCHEMA, "runs": runs})
        except (PipelineError, OSError, ValueError, TypeError) as error:
            raise RunMetadataError(f"run-metadata ledger is not writable: {error}") from error

    @staticmethod
    def _overlay(entry: dict | None) -> dict:
        entry = entry or {}
        return {
            "title": entry.get("title"),
            "deleted": bool(entry.get("deleted")),
            "deleted_at": entry.get("deleted_at"),
            "deleted_by": entry.get("deleted_by"),
        }

    def snapshot(self) -> dict[str, dict]:
        """Every recorded overlay; an unreadable ledger raises so callers fail closed."""
        with self._lock:
            return {key: self._overlay(entry) for key, entry in self._load().items()}

    def overlay(self, dag_run_id: str) -> dict:
        with self._lock:
            return self._overlay(self._load().get(dag_run_id))

    def rename(self, dag_run_id: str, value: object, *, actor: str) -> dict:
        title = normalise_title(value)
        with self._lock:
            runs = self._load()
            entry = dict(runs.get(dag_run_id) or {})
            entry["title"] = title
            entry["updated_at"] = _now()
            entry["updated_by"] = actor
            if title is None and not entry.get("deleted"):
                runs.pop(dag_run_id, None)
            else:
                runs[dag_run_id] = entry
            self._save(runs)
            return {**self._overlay(runs.get(dag_run_id)), "updated_at": entry["updated_at"]}

    def delete(self, dag_run_id: str, *, actor: str) -> dict:
        with self._lock:
            runs = self._load()
            entry = dict(runs.get(dag_run_id) or {})
            if not entry.get("deleted"):
                entry.update(deleted=True, deleted_at=_now(), deleted_by=actor)
            runs[dag_run_id] = entry
            self._save(runs)
            return self._overlay(entry)

    def restore(self, dag_run_id: str) -> dict:
        with self._lock:
            runs = self._load()
            entry = dict(runs.get(dag_run_id) or {})
            for key in ("deleted", "deleted_at", "deleted_by"):
                entry.pop(key, None)
            if entry.get("title") is None:
                runs.pop(dag_run_id, None)
            else:
                entry["updated_at"] = _now()
                runs[dag_run_id] = entry
            self._save(runs)
            return self._overlay(runs.get(dag_run_id))
