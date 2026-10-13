"""Own CAD processes by Windows job handles and bind COM by the exact process ID."""

from __future__ import annotations

import contextlib
import os
import threading
import time
import uuid
from pathlib import Path

from ...io import PipelineError, read_data, write_json
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

#: Persisted launch ownership: only a process matching its record (image,
#: creation time and host boot) and an earlier owner token may be terminated.
OWNERSHIP_SCHEMA = "solidworks-to-urdf.cad-ownership/v1"
#: Boot-moment tolerance when matching a persisted record against the host boot.
BOOT_ID_TOLERANCE_SECONDS = 30
#: This pipeline process; records carrying another token are earlier owners.
_OWNER_TOKEN = uuid.uuid4().hex


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


def _windows_creation_time(pid: int) -> int | None:
    """The process creation FILETIME (100 ns ticks), or None when unreadable.

    A pid without a readable creation time is never termination authority: it
    either exited between enumeration and this query or cannot be inspected,
    and both cases stay untouched.
    """

    import win32api
    import win32con
    import win32process

    handle = None
    try:
        handle = win32api.OpenProcess(win32con.PROCESS_QUERY_INFORMATION, False, pid)
        created, _exited, _kernel, _user = win32process.GetProcessTimes(handle)
        return (created.dwHighDateTime << 32) | created.dwLowDateTime
    except Exception:  # noqa: BLE001 - unreadable processes are never terminated
        return None
    finally:
        if handle is not None:
            with contextlib.suppress(Exception):
                handle.Close()


def _windows_boot_epoch() -> float:
    """The host boot moment in Unix seconds (uptime-derived; no registry writes)."""

    import win32api

    return time.time() - win32api.GetTickCount64() / 1000.0


def _ownership_path(override=None) -> Path:
    """The endpoint-owned launch ledger; callers may pin it explicitly."""

    if override is not None:
        return Path(override)
    configured = os.environ.get("DESCRIPTION_CAD_OWNERSHIP_FILE")
    if configured:
        return Path(configured)
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_STATE_HOME")
    if not base:
        base = str(Path.home() / ".local" / "state")
    return Path(base) / "description-pipeline" / "cad-ownership.json"


class OwnershipStore:
    """The small launch ledger; records only shrink through matching removals.

    A malformed or foreign ledger reads as empty: it must never become
    termination authority, and the unrecorded-process refusal covers the rest.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def load(self) -> dict[int, dict]:
        if not self.path.is_file():
            return {}
        try:
            payload = read_data(self.path)
        except (PipelineError, OSError, ValueError):
            return {}
        if not isinstance(payload, dict) or payload.get("schema_version") != OWNERSHIP_SCHEMA:
            return {}
        records = payload.get("records")
        if not isinstance(records, list):
            return {}
        loaded = {}
        for item in records:
            if isinstance(item, dict) and type(item.get("pid")) is int and item["pid"] > 0:
                loaded[item["pid"]] = item
        return loaded

    def add(self, record: dict) -> None:
        with self._lock:
            records = self.load()
            records[record["pid"]] = record
            self._save(records)

    def remove(self, pid: int, *, creation_time=None) -> None:
        with self._lock:
            records = self.load()
            record = records.get(pid)
            if record is None:
                return
            if creation_time is not None and record.get("creation_time") != creation_time:
                return
            del records[pid]
            self._save(records)

    def _save(self, records: dict[int, dict]) -> None:
        write_json(
            self.path,
            {"schema_version": OWNERSHIP_SCHEMA, "records": [records[key] for key in sorted(records)]},
        )


def terminate_remnant_processes(
    executable: str,
    *,
    exclude_pids=(),
    process_table=None,
    terminate_pid=None,
    sleep=time.sleep,
    ownership_file=None,
    creation_time=None,
    boot_epoch=None,
) -> list[int]:
    """Terminate RECORDED same-executable leftovers; refuse unrecorded ones.

    SolidWorks is a single-instance COM server: a process left behind by a
    crashed capture keeps the LocalServer32 registration, so the next owned
    launch would attach to the corpse and never complete startup (native18).
    Ownership is persisted per owned launch ({pid, executable, creation time,
    host boot, owner token}); the sweep terminates a same-executable process
    only when a record matches its image, creation time and host boot and the
    record carries an earlier owner token.  A same-executable process with no
    matching record is never touched - the operator is running SolidWorks
    outside the pipeline, so the launch refuses (fail closed) instead.  PIDs
    tracked by a live ``WindowsCadProcess`` (plus explicitly excluded ones) are
    always skipped: they are the live owner, not a remnant.  After a kill the
    caller settles briefly so the COM registration clears before the launch.
    """

    store = OwnershipStore(_ownership_path(ownership_file))
    records = store.load()
    table = (process_table or _windows_process_table)()
    tracked = _tracked_pids() | set(exclude_pids)
    read_creation = creation_time or _windows_creation_time
    current_boot = (boot_epoch or _windows_boot_epoch)()
    killed: list[int] = []
    unrecorded: list[int] = []
    for pid in _matching_process_ids(executable, table):
        if pid in tracked:
            continue
        record = records.get(pid)
        if record is None:
            unrecorded.append(pid)
            continue
        live_creation = read_creation(pid)
        if live_creation is None:
            continue  # exited between enumeration and the query; nothing to judge
        boot = record.get("boot_epoch")
        matches = (
            record.get("creation_time") == live_creation
            and isinstance(boot, (int, float))
            and abs(float(boot) - float(current_boot)) <= BOOT_ID_TOLERANCE_SECONDS
        )
        if not matches:
            unrecorded.append(pid)  # PID reuse or a record from another boot
            continue
        if record.get("owner") == _OWNER_TOKEN:
            continue  # this process's own launch; the Windows job owns its cleanup
        (terminate_pid or _windows_terminate_pid)(pid)
        killed.append(pid)
        store.remove(pid, creation_time=record.get("creation_time"))
    present = {row[0] for row in table}
    for pid in list(records):
        if pid not in present:
            store.remove(pid, creation_time=records[pid].get("creation_time"))
    if killed:
        sleep(REMNANT_SETTLE_SECONDS)
    if unrecorded:
        raise EnvironmentError_(
            "cad_external_solidworks",
            "SolidWorks is running outside the pipeline; close it before capturing",
            {"pids": sorted(unrecorded), "executable": executable},
        )
    return killed


class WindowsCadProcess:
    """A suspended child is assigned to our job before it can create descendants.

    Closing the job kills only this process tree, and the PID is published in
    ``_TRACKED_PIDS`` so the startup remnant sweep never touches it.  Each
    owned launch is also persisted in the ownership ledger: the sweep may only
    terminate a process that matches its record, and any other same-executable
    process refuses the launch instead of being killed.
    """

    def __init__(self, executable: str, *, ownership_file: Path | None = None):
        import win32api
        import win32job
        import win32process

        self.executable = executable
        self.pid = 0
        self._lock = threading.Lock()
        self._job = self._process = self._thread = None
        self._ownership = OwnershipStore(_ownership_path(ownership_file))
        self._record_creation = None
        try:
            # SolidWorks is a single-instance COM server: a process left behind
            # by a crashed capture keeps the LocalServer32 registration, and a
            # fresh owned launch would attach to it and never complete startup
            # (native18).  Only RECORDED leftovers are swept; any other
            # same-executable process refuses the launch instead of being killed.
            terminate_remnant_processes(executable, ownership_file=ownership_file)
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
                self._record_creation = self._creation_time()
                self._ownership.add(
                    {
                        "pid": self.pid,
                        "executable": self.executable,
                        "creation_time": self._record_creation,
                        "boot_epoch": _windows_boot_epoch(),
                        "owner": _OWNER_TOKEN,
                        "owner_pid": os.getpid(),
                        "recorded_at": time.time(),
                    }
                )
                win32job.AssignProcessToJobObject(self._job, self._process)
            except BaseException:
                # This still-suspended process is ours even if job assignment fails.
                win32api.TerminateProcess(self._process, 1)
                raise
            win32process.ResumeThread(self._thread)
        except EnvironmentError_:
            self.close()
            raise
        except Exception as error:
            self.close()
            raise EnvironmentError_("cad_process_start_failed", str(error)) from error

    def _creation_time(self) -> int | None:
        import win32process

        try:
            created, _exited, _kernel, _user = win32process.GetProcessTimes(self._process)
            return (created.dwHighDateTime << 32) | created.dwLowDateTime
        except Exception:  # noqa: BLE001 - the record stays valid, it just cannot be swept
            return None

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
                if self._ownership is not None:
                    with contextlib.suppress(Exception):
                        self._ownership.remove(self.pid, creation_time=self._record_creation)
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

    def __init__(
        self,
        *,
        process=None,
        binder=None,
        startup_timeout: float = STARTUP_TIMEOUT_SECONDS,
        ownership_file: Path | None = None,
    ):
        self.process = (
            process
            if process is not None
            else WindowsCadProcess(registered_executable(), ownership_file=ownership_file)
        )
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
