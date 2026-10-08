"""Own CAD processes by Windows job handles and bind COM by the exact process ID."""

from __future__ import annotations

import os
import threading
import time
import uuid
from pathlib import Path

from .errors import EnvironmentError_


def registered_executable() -> str:
    if os.name != "nt":
        raise EnvironmentError_("windows_required", "Native SolidWorks collection requires Windows")
    import importlib

    winreg = importlib.import_module("winreg")

    try:
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, r"SldWorks.Application\CLSID") as key:
            clsid = winreg.QueryValue(key, None)
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, rf"CLSID\{clsid}\LocalServer32") as key:
            command = os.path.expandvars(winreg.QueryValue(key, None)).strip()
    except OSError as error:
        raise EnvironmentError_("solidworks_not_registered", str(error)) from error
    if command.startswith('"'):
        executable = command.split('"', 2)[1]
    else:
        end = command.lower().find(".exe")
        executable = command[: end + 4] if end >= 0 else ""
    if not Path(executable).is_absolute() or not Path(executable).is_file():
        raise EnvironmentError_("solidworks_executable_missing", "Registered SolidWorks executable is unavailable")
    return executable


class WindowsCadProcess:
    """A suspended child is assigned to our job before it can create descendants.

    Closing the job kills only this process tree. No process name, snapshot of
    PIDs, or user's running SolidWorks instance is used as termination authority.
    """

    def __init__(self, executable: str):
        import win32api
        import win32job
        import win32process

        self.executable = executable
        self.pid = 0
        self._lock = threading.Lock()
        self._job = self._process = self._thread = None
        try:
            self._job = win32job.CreateJobObject(None, "description-cad-" + uuid.uuid4().hex)
            limits = win32job.QueryInformationJobObject(self._job, win32job.JobObjectExtendedLimitInformation)
            limits["BasicLimitInformation"]["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            win32job.SetInformationJobObject(self._job, win32job.JobObjectExtendedLimitInformation, limits)
            self._process, self._thread, self.pid, _ = win32process.CreateProcess(
                executable,
                '"' + executable + '"',
                None,
                None,
                False,
                win32process.CREATE_SUSPENDED,
                None,
                None,
                win32process.STARTUPINFO(),
            )
            try:
                win32job.AssignProcessToJobObject(self._job, self._process)
            except BaseException:
                # This still-suspended process is ours even if job assignment fails.
                win32api.TerminateProcess(self._process, 1)
                raise
            win32process.ResumeThread(self._thread)
        except Exception as error:
            self.close()
            raise EnvironmentError_("cad_process_start_failed", str(error)) from error

    def alive(self) -> bool:
        import win32process

        with self._lock:
            return self._process is not None and win32process.GetExitCodeProcess(self._process) == 259

    def close(self) -> None:
        import win32event

        with self._lock:
            errors = []
            for name in ("_job", "_thread", "_process"):
                handle = getattr(self, name)
                if handle is not None:
                    try:
                        if name == "_process" and win32event.WaitForSingleObject(handle, 5000) != 0:
                            raise OSError("Owned SolidWorks process has not exited")
                    except Exception as error:
                        errors.append(str(error))
                    try:
                        handle.Close()
                    except Exception as error:
                        errors.append(str(error))
                    finally:
                        setattr(self, name, None)
            if errors:
                raise EnvironmentError_("cad_process_cleanup_failed", "; ".join(errors), {"pid": self.pid})


def application_for_pid(pid: int):
    """Find the server launched by us, without activating another COM server."""
    import pythoncom
    import win32com.client.dynamic

    rot = pythoncom.GetRunningObjectTable()
    context = pythoncom.CreateBindCtx(0)
    expected = "SolidWorks_PID_" + str(pid)
    for moniker in rot:
        try:
            display_name = moniker.GetDisplayName(context, None)
        except pythoncom.com_error:
            continue  # unrelated ROT entries need not support display names
        if display_name != expected:
            continue
        dispatch = rot.GetObject(moniker).QueryInterface(pythoncom.IID_IDispatch)
        app = win32com.client.dynamic.DumbDispatch(dispatch)
        app._FlagAsMethod("GetProcessID")
        if app.GetProcessID() != pid:
            raise EnvironmentError_("cad_process_identity_mismatch", "COM server does not match the owned process")
        return app
    return None


class CadSession:
    """One owned application; COM references remain on the calling STA thread."""

    def __init__(self, *, process=None, binder=None, startup_timeout: float = 120):
        self.process = process if process is not None else WindowsCadProcess(registered_executable())
        self._binder = binder or application_for_pid
        self.startup_timeout = startup_timeout
        self.app = None
        self._owner_thread = None
        self.closed = False

    def connect(self, cancelled: threading.Event):
        if self.closed:
            raise EnvironmentError_("cad_session_retired", "A closed CAD session cannot reconnect", self.identity())
        self._owner_thread = threading.current_thread()
        deadline = time.monotonic() + self.startup_timeout
        app = None
        while not cancelled.is_set() and self.process.alive():
            try:
                if app is None:
                    app = self._binder(self.process.pid)
                # ROT registration precedes completion of startup and add-ins.
                # SolidWorks requires this gate before external document/API work.
                ready = app is not None and app.StartupProcessCompleted
                if type(ready) is not bool:
                    raise ValueError("StartupProcessCompleted did not return a Boolean")
            except EnvironmentError_:
                raise
            except Exception as error:
                raise EnvironmentError_(
                    "cad_startup_unreadable", "Owned SolidWorks startup state could not be read", self.identity()
                ) from error
            if time.monotonic() >= deadline:
                break
            if ready and not cancelled.is_set() and self.process.alive():
                self.app = app
                # The dedicated process serves an external automation command
                # throughout this session, including gaps between COM calls.
                app.CommandInProgress = True
                app.Visible = False
                return app
            if os.name == "nt":
                import pythoncom

                pythoncom.PumpWaitingMessages()
            cancelled.wait(0.2)
        reason = "cad_session_cancelled" if cancelled.is_set() else "cad_startup_failed"
        raise EnvironmentError_(reason, "Owned SolidWorks instance did not complete startup", self.identity())

    def current_application(self):
        """Acquire this process's current dispatch once at a native boundary.

        The Windows job and process stay unchanged. A revoked application
        interface is never reused, and losing the owned binding cannot start
        another server or retry a native call.
        """
        if self._owner_thread is not threading.current_thread():
            raise EnvironmentError_("cad_thread_mismatch", "Acquire CAD interfaces on their owning STA thread")
        try:
            if self.app is None or not self.process.alive():
                raise ValueError("the original owned SolidWorks process is unavailable")
            app = self._binder(self.process.pid)
            if app is None:
                raise ValueError("the owned SolidWorks process has no registered application binding")
            if not self.process.alive():
                raise ValueError("the owned SolidWorks process exited during application acquisition")
        except EnvironmentError_:
            raise
        except Exception as error:
            raise EnvironmentError_(
                "cad_application_unreadable",
                "the owned SolidWorks application could not be acquired",
                self.identity(),
            ) from error
        self.app = app
        return app

    def identity(self) -> dict:
        return {
            "pid": self.process.pid,
            "executable": self.process.executable,
            "ownership": "windows_job",
            "state": "closed" if self.closed else "active",
        }

    def terminate(self) -> None:
        # Safe from a watchdog thread: no COM reference is touched here.
        self.process.close()

    def close(self) -> None:
        if self.closed:
            return
        self.app = None
        self._owner_thread = None
        self.terminate()
        self.closed = True
