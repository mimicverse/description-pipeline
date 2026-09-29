"""Serialize COM work on one STA and reclaim only explicitly owned CAD jobs."""

from __future__ import annotations

import os
import queue
import threading
from concurrent.futures import Future, TimeoutError
from contextlib import suppress
from typing import Any
from collections.abc import Callable

from .errors import BridgeError


class ComExecutor:
    """Keep timeout recovery, job admission and COM ownership consistent."""

    def __init__(self, backend: Any) -> None:
        self.backend = backend
        self._queue: queue.Queue = queue.Queue()
        self._state_lock = threading.Lock()
        self._active: Future | None = None
        self._timed_out: Future | None = None
        self._closed = False
        self._operations: dict[str, Future] = {}
        self._thread = threading.Thread(target=self._loop, name="solidworks-com-sta", daemon=True)
        self._thread.start()

    @property
    def alive(self) -> bool:
        return self._thread.is_alive()

    @property
    def busy(self) -> bool:
        with self._state_lock:
            return self._active is not None

    @property
    def recovery_required(self) -> bool:
        with self._state_lock:
            return self._timed_out is not None

    def _loop(self) -> None:
        com = None
        initial_error: BaseException | None = None
        try:
            if os.name == "nt":
                import pythoncom

                com = pythoncom
                com.CoInitializeEx(com.COINIT_APARTMENTTHREADED)
        except BaseException as exc:
            initial_error = exc
        try:
            while True:
                try:
                    item = self._queue.get(timeout=0.05)
                except queue.Empty:
                    if com and initial_error is None:
                        com.PumpWaitingMessages()
                    continue
                if item is None:
                    break
                future, fn = item
                with self._state_lock:
                    if self._closed:
                        future.cancel()
                    if not future.set_running_or_notify_cancel():
                        continue
                    self._active = future
                result, error = None, None
                try:
                    if initial_error:
                        raise initial_error
                    result = fn()
                except BaseException as exc:
                    error = exc
                finally:
                    with self._state_lock:
                        self._active = None
                        if self._timed_out is future:
                            self._timed_out = None
                        if error is None:
                            future.set_result(result)
                        else:
                            future.set_exception(error)
        finally:
            try:
                release = getattr(self.backend, "release", None)
                if callable(release):
                    release()
            finally:
                if com and initial_error is None:
                    com.CoUninitialize()

    def run(self, fn: Callable[[], Any], timeout: float = 120.0, *, operation_id: str | None = None) -> Any:
        if threading.current_thread() is self._thread:
            return fn()
        future: Future = Future()
        with self._state_lock:
            if self._closed:
                raise BridgeError("cad_executor_closed", "CAD executor has closed", http_status=503)
            if self._timed_out is not None:
                raise BridgeError(
                    "cad_recovery_required", "The previous CAD operation has not stopped", http_status=503
                )
            if operation_id is not None:
                self._operations[operation_id] = future
            self._queue.put((future, fn))
        try:
            return future.result(timeout=timeout)
        except TimeoutError as exc:
            reclaimed = None
            with self._state_lock:
                # A callable's own TimeoutError must propagate unchanged. The
                # lock also prevents a late timeout from aborting the next job.
                if future.done():
                    return future.result()
                cancelled = future.cancel()
                if not cancelled and self._active is future:
                    self._timed_out = future
                    abort = getattr(self.backend, "abort_owned_processes", None)
                    if callable(abort):
                        try:
                            reclaimed = abort()
                        except Exception as error:
                            reclaimed = {"error": str(error)}
            if not cancelled and reclaimed is not None:
                # The job retains its own failure diagnostics.
                with suppress(BaseException):
                    future.result(timeout=5)
            raise BridgeError(
                "modal_dialog_blocked",
                "CAD operation timed out; owned capture processes were reclaimed when available",
                {"operation_stopped": future.done(), "owned_processes": reclaimed},
                exit_code=4,
                http_status=504,
            ) from exc
        finally:
            if operation_id is not None:
                with self._state_lock:
                    if self._operations.get(operation_id) is future:
                        del self._operations[operation_id]

    def cancel(self, operation_id: str) -> None:
        """Cancel only the named job, never a different active diagnostic."""
        with self._state_lock:
            future = self._operations.get(operation_id)
            if future is None or future.done() or future.cancel():
                return
            if self._active is future:
                self._timed_out = future
                abort = getattr(self.backend, "abort_owned_processes", None)
                if callable(abort):
                    abort()

    def close(self, timeout: float = 5.0) -> None:
        with self._state_lock:
            if not self._closed:
                self._closed = True
                abort = getattr(self.backend, "abort_owned_processes", None)
                if self._active is not None and callable(abort):
                    abort()
                self._queue.put(None)
        if threading.current_thread() is not self._thread:
            self._thread.join(timeout=timeout)
