"""Persistent job store and the single-threaded CAD job runner.

Every job owns a directory with its request, state, events and heartbeat.  State
transitions are written before they are reported, so a worker restart can tell
what was running and never mixes two attempts in one result directory.

One runner thread executes jobs; the SolidWorks COM objects live on that thread
only (see :mod:`.executor`).  A watchdog observes the runner from outside the
blocking call, so a stuck modal dialog is reported instead of hanging forever.
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
import uuid
import contextlib
from concurrent.futures import CancelledError
from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import Callable, Sequence

from .errors import BridgeError
from .jsonio import digest_json, read_json, sha256_file, write_json

JOB_SCHEMA = "description-pipeline.solidworks-job/v1"
STATES = ("queued", "running", "succeeded", "failed", "cancelled")


class JobError(BridgeError):
    def __init__(self, code: str, message: str, detail: object | None = None, *, http_status: int = 500) -> None:
        super().__init__(code, message, detail, 4, http_status)


@dataclass
class Job:
    """One freeze request and everything known about its execution."""

    job_id: str
    request: dict[str, object]
    request_digest: str
    state: str = "queued"
    attempt: int = 1
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    stage: str = "queued"
    progress: dict[str, object] = field(default_factory=dict)
    error: dict[str, object] | None = None
    result: dict[str, object] | None = None
    worker_version: str = ""
    cancelled: bool = False

    def directory(self, root: Path) -> Path:
        return Path(root) / self.job_id

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": JOB_SCHEMA,
            "job_id": self.job_id,
            "request_digest": self.request_digest,
            "state": self.state,
            "attempt": self.attempt,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "stage": self.stage,
            "progress": self.progress,
            "error": self.error,
            "result": self.result,
            "worker_version": self.worker_version,
            "cancelled": self.cancelled,
            "request": self.request,
        }


class JobStore:
    """Directory-backed job records with restart recovery."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def job_dir(self, job_id: str) -> Path:
        if not job_id or "/" in job_id or "\\" in job_id or ".." in job_id:
            raise JobError("invalid_job_id", "job id must be a plain identifier", {"job_id": job_id}, http_status=400)
        return self.root / job_id

    def save(self, job: Job) -> None:
        with self._lock:
            directory = self.job_dir(job.job_id)
            directory.mkdir(parents=True, exist_ok=True)
            write_json(directory / "job.json", job.to_dict())

    def update(self, job_id: str, mutate: Callable[[Job], object]) -> Job:
        """Read-modify-write one record while holding the store lock.

        The runner, the watchdog and cancel requests all mutate the same record;
        re-loading inside the lock is what keeps those writes from overwriting
        each other with stale copies.
        """

        with self._lock:
            job = self.load(job_id)
            mutate(job)
            directory = self.job_dir(job_id)
            directory.mkdir(parents=True, exist_ok=True)
            write_json(directory / "job.json", job.to_dict())
            return job

    def load(self, job_id: str) -> Job:
        record = self.job_dir(job_id) / "job.json"
        try:
            payload = read_json(record)
        except FileNotFoundError as error:
            # A client asking about a job that was never accepted gets "not found",
            # not a server error: the HTTP layer maps ``http_status`` straight through.
            raise JobError("job_not_found", "no such job record", {"job_id": job_id}, http_status=404) from error
        except (OSError, ValueError) as error:
            raise JobError(
                "invalid_job_record",
                "job record is unreadable",
                {"job_id": job_id, "error": str(error)},
            ) from error
        if not isinstance(payload, dict) or payload.get("schema_version") != JOB_SCHEMA:
            raise JobError("invalid_job_record", "job record has an unexpected schema", {"job_id": job_id})
        return Job(
            job_id=str(payload["job_id"]),
            request=dict(payload.get("request") or {}),
            request_digest=str(payload.get("request_digest") or ""),
            state=str(payload.get("state") or "queued"),
            attempt=int(payload.get("attempt") or 1),
            created_at=float(payload.get("created_at") or time.time()),
            started_at=payload.get("started_at"),
            finished_at=payload.get("finished_at"),
            stage=str(payload.get("stage") or "queued"),
            progress=dict(payload.get("progress") or {}),
            error=payload.get("error"),
            result=payload.get("result"),
            worker_version=str(payload.get("worker_version") or ""),
            cancelled=bool(payload.get("cancelled")),
        )

    def list_jobs(self, limit: int = 50) -> list[dict[str, object]]:
        records: list[Job] = []
        for directory in sorted(self.root.iterdir() if self.root.is_dir() else []):
            record = directory / "job.json"
            if not record.is_file():
                continue
            try:
                records.append(self.load(directory.name))
            except (JobError, OSError, ValueError, KeyError):
                continue
        records.sort(key=lambda job: job.created_at, reverse=True)
        return [job.to_dict() for job in records[:limit]]

    def find_by_digest(self, request_digest: str) -> Job | None:
        for directory in sorted(self.root.iterdir() if self.root.is_dir() else []):
            if not (directory / "job.json").is_file():
                continue
            try:
                job = self.load(directory.name)
            except (JobError, OSError, ValueError, KeyError):
                continue
            if job.request_digest == request_digest:
                return job
        return None

    def append_event(self, job_id: str, event: dict[str, object]) -> None:
        directory = self.job_dir(job_id)
        directory.mkdir(parents=True, exist_ok=True)
        line = json.dumps({"at": time.time(), **event}, ensure_ascii=False, sort_keys=True)
        with open(directory / "events.log", "a", encoding="utf-8", newline="\n") as handle:
            handle.write(line + "\n")

    def log_text(self, job_id: str, limit: int = 200_000) -> str:
        path = self.job_dir(job_id) / "events.log"
        if not path.is_file():
            return ""
        text = path.read_text(encoding="utf-8", errors="replace")
        return text[-limit:]

    def heartbeat(self, job_id: str, stage: str) -> None:
        directory = self.job_dir(job_id)
        directory.mkdir(parents=True, exist_ok=True)
        write_json(directory / "heartbeat.json", {"at": time.time(), "stage": stage, "pid": os.getpid()})

    def heartbeat_of(self, job_id: str) -> dict[str, object] | None:
        path = self.job_dir(job_id) / "heartbeat.json"
        if not path.is_file():
            return None
        try:
            payload = read_json(path)
        except (OSError, ValueError):
            return None
        return payload if isinstance(payload, dict) else None

    def recover(self, *, worker_version: str = "") -> list[Job]:
        """Decide what may be replayed after a restart, and what may not.

        Requeueing re-reads the stored request, so the record has to prove it is
        still the request that was accepted: the digest taken at submission time
        must match, and the request must still have the shape its kind requires.

        Only work that never started is put back in the queue.  A *freeze* that
        was interrupted mid-run is failed rather than resumed, because nothing in
        the record proves the CAD inputs were unchanged while it ran; replaying it
        would attach new readings to an already accepted request identity.  A
        freeze submitted by a different worker version is not replayed either: the
        toolchain is part of what a capture claims.  ``doctor`` only reads, so it
        may be requeued.  Everything that is failed here keeps its staging tree -
        the half-product and any adapter diagnostics - for inspection.
        """

        recovered: list[Job] = []
        for directory in sorted(self.root.iterdir() if self.root.is_dir() else []):
            if not (directory / "job.json").is_file():
                continue
            try:
                job = self.load(directory.name)
            except (JobError, OSError, ValueError, KeyError):
                continue
            if job.state not in ("queued", "running"):
                continue
            kind = str(job.request.get("kind") or "freeze")
            problem = self.request_problem(job)
            if problem is not None:
                self.fail_recovered(job, "recovered_request_unusable", problem)
                continue
            if kind == "freeze" and job.state == "running":
                self.fail_recovered(
                    job,
                    "recovered_freeze_unverifiable",
                    "the worker stopped while this freeze was running; the CAD inputs cannot be "
                    "re-verified against that attempt, so submit a new freeze",
                )
                continue
            if kind == "freeze" and worker_version and job.worker_version and job.worker_version != worker_version:
                self.fail_recovered(
                    job,
                    "recovered_worker_version_changed",
                    f"this freeze was submitted by worker {job.worker_version} and would be replayed by "
                    f"{worker_version}; submit a new freeze",
                )
                continue
            if job.state == "running":
                job.attempt += 1
                job.error = {
                    "code": "worker_restart",
                    "message": "worker stopped while this job was running; the attempt was discarded",
                }
                self.append_event(job.job_id, {"event": "interrupted", "attempt": job.attempt})
            if worker_version and job.worker_version and job.worker_version != worker_version:
                # the replay runs under a different collector build than the
                # submission did; the record has to say so
                self.append_event(
                    job.job_id,
                    {
                        "event": "recovered_under_new_version",
                        "submitted_with": job.worker_version,
                        "worker_version": worker_version,
                    },
                )
                job.worker_version = worker_version
            job.state = "queued"
            job.stage = "queued"
            job.started_at = None
            job.finished_at = None
            shutil.rmtree(self.job_dir(job.job_id) / "staging", ignore_errors=True)
            self.save(job)
            recovered.append(job)
        recovered.sort(key=lambda job: job.created_at)
        return recovered

    def fail_recovered(self, job: Job, code: str, message: str) -> Job:
        """Fail a job that must not be replayed, keeping its staging tree."""

        job.state = "failed"
        job.error = {"code": code, "message": message}
        job.finished_at = time.time()
        self.append_event(job.job_id, {"event": code, "message": message})
        self.save(job)
        return job

    @staticmethod
    def request_problem(job: Job) -> str | None:
        """Why a stored request may not be replayed, or ``None`` when it may."""

        if digest_json(job.request) != job.request_digest:
            return "the stored request no longer matches the digest recorded when it was accepted"
        kind = job.request.get("kind") or "freeze"
        if kind not in ("freeze", "doctor"):
            return f"unsupported job kind: {kind}"
        if kind == "freeze" and not isinstance(job.request.get("config"), dict):
            return "freeze requests need a 'config' object"
        return None

    def active_jobs(self) -> dict[str, object]:
        """Jobs that are still queued or running, straight from the records.

        The runner's in-memory queue only knows what this process submitted, so
        admission control ("is the worker idle enough to switch versions?") has to
        ask the store: a job recovered from a previous process is queued without
        ever passing through ``submit()``.
        """

        queued: list[str] = []
        running: list[str] = []
        for directory in sorted(self.root.iterdir() if self.root.is_dir() else []):
            if not (directory / "job.json").is_file():
                continue
            try:
                job = self.load(directory.name)
            except (JobError, OSError, ValueError, KeyError):
                continue
            if job.state == "queued":
                queued.append(job.job_id)
            elif job.state == "running":
                running.append(job.job_id)
        return {"queued": len(queued), "running": len(running), "queued_ids": queued, "running_ids": running}


class JobRunner:
    """Execute jobs one at a time on a single thread, with an external watchdog."""

    def __init__(
        self,
        store: JobStore,
        run_job: Callable[[Job, Path], dict[str, object]],
        *,
        worker_version: str = "",
        watchdog_seconds: float = 900.0,
        poll_seconds: float = 1.0,
        initial_job_ids: Sequence[str] = (),
    ) -> None:
        self.store = store
        self.run_job = run_job
        self.worker_version = worker_version
        self.watchdog_seconds = watchdog_seconds
        self.poll_seconds = poll_seconds
        # Jobs recovered from a previous process enter the queue before the runner
        # thread exists, so nothing the loop does can race with adoption and no
        # adopted job can be lost between "record says queued" and "runner knows".
        self._queue: list[str] = list(dict.fromkeys(initial_job_ids))
        self._current: str | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._loop, name="solidworks-jobs", daemon=True)
        self._thread.start()

    # -- public API ------------------------------------------------------

    def submit(self, request: dict[str, object], *, job_id: str | None = None) -> Job:
        """Accept a request, reusing a job only when the request repeats exactly.

        An explicit ``job_id`` names an existing record: it is returned when it
        holds the same request, and refused when it holds different evidence -
        overwriting a terminal result would destroy the only copy of it.
        """

        digest = digest_json(request)
        with self._lock:
            if job_id is not None and (self.store.job_dir(job_id) / "job.json").is_file():
                held = self.store.load(job_id)
                if held.request_digest == digest:
                    self.store.append_event(job_id, {"event": "duplicate_request_ignored"})
                    return held
                raise JobError(
                    "job_id_in_use",
                    "this job id already holds different evidence; use a new id or resume it",
                    {"job_id": job_id, "state": held.state},
                )
            existing = self.store.find_by_digest(digest)
            if existing is not None and existing.state in ("queued", "running", "succeeded"):
                # Repeating a request must not create a second, mixed result.
                self.store.append_event(existing.job_id, {"event": "duplicate_request_ignored"})
                return existing
            # A millisecond timestamp plus the queue length collides when two jobs
            # are accepted in the same millisecond after the queue drained, which
            # would make the second record overwrite the first one.
            identifier = job_id or f"job-{uuid.uuid4().hex}"
            job = Job(
                job_id=identifier,
                request=dict(request),
                request_digest=digest,
                worker_version=self.worker_version,
            )
            self.store.save(job)
            self.store.append_event(job.job_id, {"event": "queued", "attempt": job.attempt})
            self._queue.append(job.job_id)
        return job

    def cancel(self, job_id: str) -> Job:
        """Cancel a job without ever holding the store lock while taking the runner lock.

        ``submit`` takes the runner lock and then the store lock, so this path has
        to use the same order: the queue is emptied under the runner lock first,
        and only then is the record updated.  Taking them the other way around is
        an ABBA deadlock between a submit and a cancel.
        """

        with self._lock:
            if job_id in self._queue:
                self._queue.remove(job_id)

        def mutate(job: Job) -> None:
            if job.state in ("succeeded", "failed", "cancelled"):
                return
            job.cancelled = True
            if job.state == "queued":
                job.state = "cancelled"
                job.finished_at = time.time()

        job = self.store.update(job_id, mutate)
        self.store.append_event(job_id, {"event": "cancel_requested"})
        return job

    def close(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._thread.join(timeout=timeout)

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return {"current": self._current, "queued": list(self._queue), "alive": self._thread.is_alive()}

    # -- execution -------------------------------------------------------

    def _loop(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                job_id = self._queue.pop(0) if self._queue else None
                self._current = job_id
            if job_id is None:
                time.sleep(self.poll_seconds)
                continue
            try:
                self._execute(job_id)
            except BaseException as exc:  # a runner bug must not kill the loop
                self._fail_stuck(job_id, exc)
            finally:
                with self._lock:
                    self._current = None

    def _fail_stuck(self, job_id: str, error: BaseException) -> None:
        """A job that left ``_execute`` without a terminal record must be finalized.

        The runner pops the id off its queue before doing anything, so a failure in
        the startup phase (reading the record, clearing/creating staging, writing
        the "running" transition, or the event log) would otherwise leave the job
        queued or running forever: never executed, never failed, and counted as
        busy by admission control, which blocks version switches for good.
        """

        message = f"the runner failed before the job reached a terminal state: {type(error).__name__}: {error}"
        with contextlib.suppress(Exception):
            self.store.append_event(job_id, {"event": "runner_error", "error": message})

        def fail(job: Job) -> None:
            if job.state in ("succeeded", "failed", "cancelled"):
                return
            job.state = "cancelled" if job.cancelled else "failed"
            job.error = {"code": "runner_error", "message": message}
            job.finished_at = time.time()

        with contextlib.suppress(Exception):
            self.store.update(job_id, fail)

    def _execute(self, job_id: str) -> None:
        current = self.store.load(job_id)
        if current.cancelled:

            def cancel_now(job: Job) -> None:
                job.state = "cancelled"
                job.finished_at = time.time()

            self.store.update(job_id, cancel_now)
            return
        staging = self.store.job_dir(job_id) / "staging"
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir(parents=True)

        def begin(job: Job) -> None:
            job.state = "running"
            job.started_at = time.time()
            job.stage = "starting"
            # A fresh attempt supersedes the note an interrupted one left behind:
            # the record must not report an error for work that is running now.
            # The history stays in events.log.
            job.error = None

        self.store.update(job_id, begin)
        self.store.append_event(job_id, {"event": "started", "attempt": current.attempt})

        outcome: dict[str, object] = {}

        def progress(stage: str, **details: object) -> None:
            def mutate(job: Job) -> None:
                job.stage = stage
                job.progress = {"stage": stage, **details}

            self.store.update(job_id, mutate)
            self.store.heartbeat(job_id, stage)
            self.store.append_event(job_id, {"event": "progress", "stage": stage, **details})

        watchdog = threading.Timer(self.watchdog_seconds, self._on_watchdog, args=(job_id,))
        watchdog.daemon = True
        watchdog.start()
        try:
            result = self.run_job(current, staging)
            outcome = result if isinstance(result, dict) else {"value": result}

            def finish(job: Job) -> None:
                if job.cancelled:
                    job.state = "cancelled"
                    job.error = {"code": "cancelled", "message": "cancelled before the job completed"}
                else:
                    job.state = "succeeded"
                    job.result = outcome
                    job.stage = "done"
                    if (job.error or {}).get("code") == "cad_call_timeout":
                        # The call did finish after all: the watchdog's transient note
                        # must not stay attached to a successful record.
                        job.error = None
                job.finished_at = time.time()

            finished = self.store.update(job_id, finish)
            self.store.append_event(job_id, {"event": finished.state})
        except BridgeError as exc:
            # Capture before defining the closure: Python clears the exception
            # variable when the except block ends.
            failure = exc.to_dict()

            def fail(job: Job) -> None:
                if job.cancelled:
                    job.state = "cancelled"
                    job.error = {"code": "cancelled", "message": "the job was cancelled", "reason": failure}
                else:
                    job.state = "failed"
                    job.error = failure
                job.finished_at = time.time()

            self.store.update(job_id, fail)
            self.store.append_event(job_id, {"event": "failed", "error": failure})
        except BaseException as exc:  # noqa: BLE001 - report, never lose the job
            # A future that was still queued in the operation executor can be
            # cancelled after the runner already owns the job; that is a
            # cancellation, not an internal error.
            cancelled = isinstance(exc, CancelledError)
            internal: dict[str, object] = {
                "code": "cancelled" if cancelled else "internal_error",
                "message": str(exc),
                "type": type(exc).__name__,
            }

            def fail_internal(job: Job) -> None:
                if job.cancelled or cancelled:
                    job.state = "cancelled"
                    job.error = {"code": "cancelled", "message": "the job was cancelled", "reason": internal}
                else:
                    job.state = "failed"
                    job.error = internal
                job.finished_at = time.time()

            self.store.update(job_id, fail_internal)
            self.store.append_event(job_id, {"event": "failed", "error": internal})
        finally:
            watchdog.cancel()

    def _on_watchdog(self, job_id: str) -> None:
        """Report a stuck job from outside the blocking call.

        Nothing here ends a CAD process: the worker only ever reports.  A human
        session is never a recovery target.
        """

        noted: list[dict[str, object]] = []

        def note_timeout(record: Job) -> None:
            # The state check belongs inside the store lock: between an outer
            # check and this update the job may have finished, and a timeout note
            # must never be attached to a terminal record.
            if record.state != "running":
                return
            record.error = {
                "code": "cad_call_timeout",
                "message": (
                    "the job exceeded the observation window; CAD timeout recovery is managed "
                    "by the operation executor and never targets the user's SolidWorks session"
                ),
                "stage": record.stage,
            }
            noted.append({"event": "watchdog", "stage": record.stage})

        try:
            self.store.update(job_id, note_timeout)
        except (JobError, OSError, ValueError):
            return
        if noted:
            with contextlib.suppress(Exception):
                self.store.append_event(job_id, noted[0])


def file_digest(path: Path) -> str:
    return sha256_file(path)
