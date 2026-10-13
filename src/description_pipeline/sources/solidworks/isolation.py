"""Own CAD processes by Windows job handles and bind COM by the exact process ID."""

from __future__ import annotations

import contextlib
import os
import threading
import time
import uuid
from pathlib import Path

from .errors import EnvironmentError_

# Published ISldWorks dual-interface IID in the SolidWorks type library.
ISLDWORKS_IID = "{83A33D22-27C5-11CE-BFD4-00400513BB57}"

#: How long an owned SolidWorks instance may take to register its COM server
#: and complete startup.  The old 120 s bound proved too tight after a crash
#: (native18): the CAD host was still settling when the deadline fired.  The
#: wait stays bounded, and the timeout path closes the owned process before
#: failing so it cannot linger as a remnant.
STARTUP_TIMEOUT_SECONDS = 300

#: Killed remnants are waited on; this extra moment lets the COM LocalServer32
#: registration clear before the owned launch.
REMNANT_SETTLE_SECONDS = 0.5
REMNANT_TERMINATE_WAIT_MS = 5000

#: PIDs of processes owned by a live ``WindowsCadProcess``.  The startup remnant
#: sweep never terminates a tracked instance, only same-executable leftovers of
#: a crashed capture.
_TRACKED_PIDS: set[int] = set()
_TRACKED_PIDS_LOCK = threading.Lock()


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


def _tracked_pids() -> set[int]:
    with _TRACKED_PIDS_LOCK:
        return set(_TRACKED_PIDS)


def _windows_process_table() -> list[tuple[int, str]]:
    """(pid, image path) for every process we may inspect; unreadable ones are skipped."""

    import win32api
    import win32con
    import win32process

    table: list[tuple[int, str]] = []
    for pid in win32process.EnumProcesses():
        if not pid:
            continue
        handle = None
        try:
            handle = win32api.OpenProcess(
                win32con.PROCESS_QUERY_INFORMATION | win32con.PROCESS_VM_READ, False, pid
            )
            table.append((int(pid), str(win32process.GetModuleFileNameEx(handle, 0))))
        except Exception:  # noqa: BLE001 - processes we cannot inspect are not ours to judge
            continue
        finally:
            if handle is not None:
                with contextlib.suppress(Exception):
                    handle.Close()
    return table


def _matching_process_ids(executable: str, table) -> list[int]:
    """PIDs in ``table`` whose image path equals ``executable``."""

    wanted = os.path.normcase(os.path.abspath(str(executable)))
    matched = []
    for pid, image in table:
        if type(pid) is not int or not pid or not isinstance(image, str) or not image:
            continue
        if os.path.normcase(os.path.abspath(image)) == wanted:
            matched.append(pid)
    return matched


def _windows_terminate_pid(pid: int) -> None:
    """Terminate one process and wait, bounded, for its exit."""

    import win32api
    import win32con
    import win32event

    handle = win32api.OpenProcess(win32con.PROCESS_TERMINATE | win32con.SYNCHRONIZE, False, pid)
    try:
        win32api.TerminateProcess(handle, 1)
        win32event.WaitForSingleObject(handle, REMNANT_TERMINATE_WAIT_MS)
    finally:
        handle.Close()


def terminate_remnant_processes(
    executable: str,
    *,
    exclude_pids=(),
    process_table=None,
    terminate_pid=None,
    sleep=time.sleep,
) -> list[int]:
    """Terminate same-executable leftovers that no live session tracks.

    SolidWorks is a single-instance COM server: a process left behind by a
    crashed capture keeps the LocalServer32 registration, so the next owned
    launch would attach to the corpse and never complete startup (native18).
    The sweep runs before every owned start and only addresses processes whose
    image path equals the registered executable; PIDs tracked by a live
    ``WindowsCadProcess`` (plus explicitly excluded ones) are never touched.
    After a kill the caller settles briefly so the COM registration clears
    before the fresh launch.
    """

    excluded = _tracked_pids() | set(exclude_pids)
    killed = []
    for pid in _matching_process_ids(executable, (process_table or _windows_process_table)()):
        if pid in excluded:
            continue
        (terminate_pid or _windows_terminate_pid)(pid)
        killed.append(pid)
    if killed:
        sleep(REMNANT_SETTLE_SECONDS)
    return killed


class WindowsCadProcess:
    """A suspended child is assigned to our job before it can create descendants.

    Closing the job kills only this process tree, and the PID is published in
    ``_TRACKED_PIDS`` so the startup remnant sweep never touches it.  That
    sweep is the single deliberate exception to "no PID snapshot is termination
    authority": it kills only same-executable leftovers of a crashed capture.
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
            # SolidWorks is a single-instance COM server: a process left behind
            # by a crashed capture keeps the LocalServer32 registration, and a
            # fresh owned launch would attach to it and never complete startup
            # (native18).  Only same-executable leftovers are swept, never a
            # PID tracked by a live instance.
            terminate_remnant_processes(executable)
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
            with _TRACKED_PIDS_LOCK:
                _TRACKED_PIDS.add(self.pid)
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
            try:
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
            finally:
                with _TRACKED_PIDS_LOCK:
                    _TRACKED_PIDS.discard(self.pid)
            if errors:
                raise EnvironmentError_("cad_process_cleanup_failed", "; ".join(errors), {"pid": self.pid})


def application_for_pid(pid: int):
    """Find the server launched by us, without activating another COM server."""
    import pythoncom
    import pywintypes
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
        # ISldWorks is the vendor's dual application interface. Its generic
        # IDispatch view can stop serving RPC while this interface remains live.
        # useIID selects only the Python wrapper; calls use the queried interface.
        dispatch = rot.GetObject(moniker).QueryInterface(pywintypes.IID(ISLDWORKS_IID), pythoncom.IID_IDispatch)
        # Vendor DISPID166: GetProcessID() -> VT_I4, a method (not a property).
        actual = dispatch.InvokeTypes(166, 0, pythoncom.DISPATCH_METHOD, (pythoncom.VT_I4, 0), ())
        if type(actual) is not int or actual != pid:
            raise EnvironmentError_("cad_process_identity_mismatch", "COM server does not match the owned process")
        return win32com.client.dynamic.DumbDispatch(dispatch)
    return None


class CadSession:
    """One owned application; COM references remain on the calling STA thread."""

    def __init__(self, *, process=None, binder=None, startup_timeout: float = STARTUP_TIMEOUT_SECONDS):
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
        timed_out = False
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
                timed_out = True
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
        if timed_out:
            # The owned instance did not complete startup within the wait.
            # Close its process before failing: a half-started SolidWorks
            # keeps the single-instance COM registration, so the next launch
            # would attach to it and hang again (native18).  A cleanup failure
            # must not mask the timeout itself; the caller still closes the
            # session and holds the Windows job.
            with contextlib.suppress(Exception):
                self.terminate()
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
