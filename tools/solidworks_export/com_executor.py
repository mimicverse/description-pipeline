"""Serialize complete CAD operations on one COM-owning STA thread."""

import os
import queue
import threading
from concurrent.futures import Future, TimeoutError

from .errors import BridgeError


class ComExecutor:
    def __init__(self, backend):
        self.backend = backend
        self._queue: queue.Queue = queue.Queue()
        self._thread = threading.Thread(target=self._loop, name="swbridge-com-sta", daemon=True)
        self._thread.start()

    def _loop(self):
        com = None
        initial_error = None
        try:
            if os.name == "nt":
                import pythoncom

                com = pythoncom
                com.CoInitializeEx(com.COINIT_APARTMENTTHREADED)
        except Exception as exc:
            initial_error = exc
        try:
            while True:
                try:
                    item = self._queue.get(timeout=0.05)
                except queue.Empty:
                    if com:
                        com.PumpWaitingMessages()
                    continue
                if item is None:
                    break
                future, fn = item
                if not future.set_running_or_notify_cancel():
                    continue
                try:
                    if initial_error:
                        raise initial_error
                    future.set_result(fn())
                except BaseException as exc:
                    future.set_exception(exc)
                finally:
                    if com:
                        com.PumpWaitingMessages()
        finally:
            release = getattr(self.backend, "release", None)
            if release:
                release()
            if com and initial_error is None:
                com.CoUninitialize()

    def run(self, fn, timeout=120):
        if threading.current_thread() is self._thread:
            return fn()
        future: Future = Future()
        self._queue.put((future, fn))
        try:
            return future.result(timeout=timeout)
        except TimeoutError as exc:
            future.cancel()  # cancels queued work; never kills SolidWorks
            raise BridgeError(
                "modal_dialog_blocked", "CAD operation timed out; inspect SolidWorks", exit_code=4, http_status=504
            ) from exc

    def close(self):
        self._queue.put(None)
        self._thread.join(timeout=5)
