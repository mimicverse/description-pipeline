"""Single-flight job queue with a small JSON journal (survives restarts)."""

from __future__ import annotations

import json
import os
import queue
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from .errors import BridgeError

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"
TERMINAL = (STATUS_SUCCEEDED, STATUS_FAILED, STATUS_CANCELLED)


@dataclass
class Job:
    id: str
    kind: str
    params: Dict
    status: str = STATUS_QUEUED
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    result: Optional[Dict] = None
    error: Optional[Dict] = None
    cancel_requested: bool = False
    log_path: str = ""
    progress: Optional[Dict] = None

    def to_dict(self) -> Dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "params": self.params,
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "result": self.result,
            "error": self.error,
            "cancel_requested": self.cancel_requested,
            "progress": self.progress,
        }


class JobManager:
    """One worker thread; jobs run strictly one at a time."""

    def __init__(self, root_dir: str) -> None:
        self.root = os.path.abspath(root_dir)
        self.jobs_dir = os.path.join(self.root, "jobs")
        os.makedirs(self.jobs_dir, exist_ok=True)
        self._jobs: Dict[str, Job] = {}
        self._lock = threading.Lock()
        self._persist_lock = threading.Lock()
        self._closed = False
        self._queue: "queue.Queue[tuple]" = queue.Queue()
        self._recover_existing_jobs()
        self._worker = threading.Thread(target=self._loop, name="swbridge-worker", daemon=True)
        self._worker.start()

    # ------------------------------------------------------------- recovery
    def _job_from_payload(self, payload: Dict) -> Job:
        job_id = str(payload["id"])
        return Job(
            id=job_id,
            kind=str(payload.get("kind", "unknown")),
            params=payload.get("params") or {},
            status=str(payload.get("status", STATUS_FAILED)),
            created_at=float(payload.get("created_at") or time.time()),
            started_at=payload.get("started_at"),
            finished_at=payload.get("finished_at"),
            result=payload.get("result"),
            error=payload.get("error"),
            cancel_requested=bool(payload.get("cancel_requested", False)),
            log_path=os.path.join(self.jobs_dir, f"{job_id}.log"),
            progress=payload.get("progress"),
        )

    def _recover_existing_jobs(self) -> None:
        """Reload the journal after a restart; interrupted jobs become failed."""

        for name in sorted(os.listdir(self.jobs_dir)):
            if not name.endswith(".json"):
                continue
            path = os.path.join(self.jobs_dir, name)
            try:
                with open(path, encoding="utf-8") as handle:
                    payload = json.load(handle)
                job = self._job_from_payload(payload)
            except (OSError, ValueError, KeyError, TypeError):
                continue
            if job.status in (STATUS_QUEUED, STATUS_RUNNING):
                job.status = STATUS_FAILED
                job.error = {
                    "code": "job_interrupted",
                    "message": "service restarted before the job finished",
                }
                job.finished_at = job.finished_at or time.time()
                self._persist(job)
            self._jobs[job.id] = job

    # ------------------------------------------------------------------ api
    def submit(self, kind: str, params: Dict, run_fn: Callable[[Job], Dict]) -> Job:
        if self._closed:
            raise BridgeError("job_manager_closed", "service is shutting down")
        job_id = params.get("job_id") or (f"job-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}")
        job = Job(id=job_id, kind=kind, params=params, log_path=os.path.join(self.jobs_dir, f"{job_id}.log"))
        with self._lock:
            if job_id in self._jobs:
                raise BridgeError(
                    "job_id_conflict", "job id already exists", {"job_id": job_id}, exit_code=1, http_status=409
                )
            self._jobs[job_id] = job
        self._persist(job)
        self._queue.put((job, run_fn))
        return job

    def get(self, job_id: str) -> Job:
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            raise BridgeError(
                "job_not_found", f"no such job: {job_id}", {"job_id": job_id}, exit_code=1, http_status=404
            )
        return job

    def list_jobs(self, limit: int = 50) -> List[Dict]:
        with self._lock:
            jobs = sorted(self._jobs.values(), key=lambda item: item.created_at, reverse=True)
        return [job.to_dict() for job in jobs[:limit]]

    def log_text(self, job_id: str, max_chars: int = 200000) -> str:
        job = self.get(job_id)
        try:
            with open(job.log_path, encoding="utf-8", errors="replace") as handle:
                text = handle.read()
        except OSError:
            return ""
        return text[-max_chars:]

    def cancel(self, job_id: str) -> Dict:
        job = self.get(job_id)
        job.cancel_requested = True
        if job.status == STATUS_QUEUED:
            job.status = STATUS_CANCELLED
            job.finished_at = time.time()
        self._persist(job)
        return job.to_dict()

    def update_progress(self, job_id: str, progress: Dict) -> None:
        """Record coarse progress for a running job (best effort)."""

        job = self.get(job_id)
        job.progress = dict(progress)
        self._persist(job)

    # -------------------------------------------------------------- worker
    def close(self, timeout=5):
        if not self._closed:
            self._closed = True
            self._queue.put(None)  # type: ignore[arg-type]  # 哨兵：通知 worker 退出
        self._worker.join(timeout=timeout)

    def _loop(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is None:
                    return
                job, run_fn = item
                try:
                    self._execute(job, run_fn)
                except Exception as exc:
                    # A journal I/O failure must not silently kill the queue.
                    # Retain failure in memory so API callers see it even when
                    # the disk itself cannot accept another error record.
                    job.status = STATUS_FAILED
                    job.finished_at = time.time()
                    job.error = {"code": "job_journal_failed", "message": str(exc)}
            finally:
                self._queue.task_done()

    def _execute(self, job: Job, run_fn: Callable[[Job], Dict]) -> None:
        if job.cancel_requested:
            job.status = STATUS_CANCELLED
            job.finished_at = time.time()
            self._persist(job)
            return
        job.status = STATUS_RUNNING
        job.started_at = time.time()
        self._persist(job)
        try:
            job.result = run_fn(job)
            job.status = STATUS_SUCCEEDED
        except BridgeError as exc:
            job.status = STATUS_CANCELLED if exc.code == "job_cancelled" else STATUS_FAILED
            job.error = exc.to_dict()
        except Exception as exc:  # keep the journal complete
            job.status = STATUS_FAILED
            job.error = {"code": "internal_error", "message": str(exc)}
        finally:
            job.finished_at = time.time()
            self._persist(job)

    def _persist(self, job: Job) -> None:
        path = os.path.join(self.jobs_dir, f"{job.id}.json")
        # Progress updates and job completion can overlap. Serialize the whole
        # snapshot/write and use a unique temporary file, not one shared .tmp.
        with self._persist_lock:
            with self._lock:
                payload = job.to_dict()
            fd, tmp = tempfile.mkstemp(prefix=job.id + ".", suffix=".tmp", dir=self.jobs_dir)
            try:
                with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                    json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
                    handle.write("\n")
                # Windows readers/virus scanners can temporarily hold the
                # destination without FILE_SHARE_DELETE. Retry that specific
                # sharing/permission error briefly, but never ignore it.
                for attempt in range(6):
                    try:
                        os.replace(tmp, path)
                        break
                    except PermissionError:
                        if attempt == 5:
                            raise
                        time.sleep(0.01 * (2**attempt))
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)
