"""Recoverable workspace publication; manifest is the final visible commit marker."""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

from ..io import PipelineError, read_data

ENTRIES = (
    "model",
    "urdf",
    "mjcf",
    "meshes",
    "config/consumer.json",
    "docs/quality.json",
    "docs/quality.md",
    "manifest.json",
)

#: Journal of an interrupted publication, relative to the model root.
JOURNAL = "build/publication.json"


def _remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def _process_alive(pid: int) -> bool:
    if type(pid) is not int or pid <= 0:
        raise PipelineError("Invalid publisher process identity")
    if sys.platform == "win32":
        # os.kill(pid, 0) terminates the process on Windows; never use it as a probe.  The platform
        # test also tells mypy which stubs apply, so `ctypes.WinDLL` needs no ignore on either host
        # (an ignore here is unused on Windows and `warn_unused_ignores` rejects it).
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        handle = kernel.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE, never terminate access
        if not handle:
            error = ctypes.get_last_error()
            if error == 87:  # ERROR_INVALID_PARAMETER: process no longer exists
                return False
            if error == 5:  # Access denied is not evidence of a dead process.
                return True
            raise PipelineError(f"Cannot inspect publisher process: Windows error {error}")
        try:
            state = kernel.WaitForSingleObject(handle, 0)
            if state not in (0, 0x102):
                raise PipelineError("Cannot determine whether publisher process is alive")
            return state == 0x102  # WAIT_TIMEOUT
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as error:
        raise PipelineError(f"Cannot inspect publisher process: {error}") from error
    return True


def _journal(journal: Path) -> tuple[int, str, list[str]]:
    """Read the publication journal, naming what to do when it is damaged.

    ``recover`` is the command an operator runs when a publication was interrupted, so a journal
    that is unreadable, not an object or missing a key has to say what to do next instead of raising
    KeyError/TypeError out of the recovery path itself.
    """

    advice = f"if no publish is running, remove {JOURNAL} and run `description build --root .`"
    try:
        state = read_data(journal)
    except PipelineError as error:
        raise PipelineError(f"{error}; {advice}") from None
    if not isinstance(state, dict):
        raise PipelineError(f"{JOURNAL} must contain a JSON object, found {type(state).__name__}; {advice}")
    owner = state.get("pid")
    if type(owner) is not int or owner <= 0:
        raise PipelineError(f"{JOURNAL} does not name the publishing process; {advice}")
    backup = state.get("backup")
    if not isinstance(backup, str) or not backup or Path(backup).name != backup or not backup.startswith("previous-"):
        raise PipelineError(f"{JOURNAL} names no backup directory to restore from; {advice}")
    previous = state.get("previous")
    if not isinstance(previous, list) or not all(isinstance(entry, str) for entry in previous):
        raise PipelineError(f"{JOURNAL} does not list the entries it replaced; {advice}")
    return owner, backup, previous


def recover(root: Path) -> dict:
    journal = root / JOURNAL
    if not journal.exists():
        return {"recovered": False}
    owner, backup_name, replaced = _journal(journal)
    if owner != os.getpid() and _process_alive(owner):
        raise PipelineError("Another publisher is active; recovery refused")
    backup = root / "build" / backup_name
    if not backup.is_dir():
        # Reporting "recovered" without the backup would hide that nothing was restored.
        raise PipelineError(
            f"{JOURNAL} names a backup that is not there: {backup_name}; "
            f"if no publish is running, remove {JOURNAL} and run `description build --root .`"
        )
    for relative in reversed(ENTRIES):
        restored = backup / relative
        target = root / relative
        if restored.exists():
            _remove(target)
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(restored, target)
        elif relative not in replaced:
            _remove(target)
    shutil.rmtree(backup, ignore_errors=True)
    journal.unlink()
    return {"recovered": True}


def publish(staging: Path, root: Path) -> None:
    build = root / "build"
    build.mkdir(exist_ok=True)
    backup = Path(tempfile.mkdtemp(prefix="previous-", dir=build))
    journal = root / JOURNAL
    for relative in ENTRIES:
        if (root / relative).is_symlink() or not (staging / relative).exists():
            shutil.rmtree(backup)
            raise PipelineError(f"Invalid publication entry: {relative}")
    state = {
        "pid": os.getpid(),
        "backup": backup.name,
        "previous": [name for name in ENTRIES if (root / name).exists()],
    }
    try:
        with journal.open("x", encoding="utf-8") as stream:
            json.dump(state, stream)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError as error:
        shutil.rmtree(backup)
        raise PipelineError("Publication in progress or interrupted; inspect/recover it first") from error
    try:
        for relative in ENTRIES:
            target = root / relative
            previous = backup / relative
            if target.exists():
                previous.parent.mkdir(parents=True, exist_ok=True)
                os.replace(target, previous)
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staging / relative, target)
        # The journal disappears only after all assets and the final manifest have been installed.
        journal.unlink()
    except BaseException:
        recover(root)
        raise
    # Publication is committed. A cleanup failure must not report a failed build
    # after the new delivery has already become visible.
    shutil.rmtree(backup, ignore_errors=True)
