"""Owned Linux process groups shared by the optional execution consumers.

A fresh private session and a non-secret nonce in the kernel argv establish
membership. The nonce is supervisor metadata, separate from business inputs.
Only pidfd-bound members may receive signals; settlement needs kernel absence.
"""

from __future__ import annotations

import os
import signal
import socket
import time
from pathlib import Path


def module_command(python: Path, module: str, nonce: str) -> list[str]:
    """Launch a public module while retaining its ownership marker in kernel argv."""
    if not python.is_absolute() or not module or not all(part.isidentifier() for part in module.split(".")):
        raise ValueError("An absolute interpreter and Python module are required")
    if not isinstance(nonce, str) or not nonce or len(nonce) > 128 or "\x00" in nonce:
        raise ValueError("A bounded non-secret process nonce is required")
    program = (
        "import runpy,sys; module=sys.argv[1]; sys.argv=[module]; "
        "runpy.run_module(module,run_name='__main__')"
    )
    return [str(python), "-I", "-B", "-u", "-c", program, module, nonce]


def process_identity(pid: int, nonce: str) -> dict:
    """Bind a Linux PID to a boot and start time; a PID alone is not ownership."""
    stat = Path(f"/proc/{pid}/stat").read_text()
    fields = stat[stat.rfind(")") + 2 :].split()
    return {
        "host": socket.gethostname(),
        "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        "pid": pid,
        "start_ticks": fields[19],
        "nonce": nonce,
        "pgid": int(fields[2]),
        "session_id": int(fields[3]),
        "uid": Path(f"/proc/{pid}").stat().st_uid,
    }


def process_state(identity: dict) -> str:
    """Return alive/dead/foreign/unknown without treating an observation error as death."""
    if identity.get("host") != socket.gethostname():
        return "foreign"
    try:
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        if not isinstance(identity.get("boot_id"), str) or not identity["boot_id"]:
            return "unknown"
        if identity.get("boot_id") != boot:
            return "dead"
        pid = identity["pid"]
        stat = Path(f"/proc/{pid}/stat").read_text()
        fields = stat[stat.rfind(")") + 2 :].split()
        if fields[19] != identity.get("start_ticks") or fields[0] == "Z":
            return "dead"
        return "alive"
    except FileNotFoundError:
        return "dead"
    except (OSError, KeyError, TypeError, IndexError):
        return "unknown"


def _group_snapshot(identity: dict) -> tuple[str, list[dict]]:
    """Observe every live member of the recorded private session and group.

    An exited leader does not prove that its resources have exited. A member
    needs the original session, UID, time ordering and gated argv nonce before
    this host may signal it. Unknown membership remains unresolved.
    """
    if identity.get("host") != socket.gethostname():
        return "foreign", []
    try:
        pid = identity["pid"]
        if (
            type(pid) is not int
            or pid <= 0
            or identity.get("pgid") != pid
            or identity.get("session_id") != pid
            or identity.get("uid") != os.geteuid()
            or not isinstance(identity.get("start_ticks"), str)
            or not identity["start_ticks"].isdecimal()
            or not isinstance(identity.get("nonce"), str)
            or not identity["nonce"]
            or not isinstance(identity.get("boot_id"), str)
            or not identity["boot_id"]
        ):
            return "unknown", []
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        if identity["boot_id"] != boot:
            return "dead", []
        members = []
        leader_reused = False
        for entry in Path("/proc").iterdir():
            if not entry.name.isdecimal():
                continue
            try:
                stat = (entry / "stat").read_text()
                fields = stat[stat.rfind(")") + 2 :].split()
                member_pid = int(entry.name)
                if member_pid == pid and fields[19] != identity["start_ticks"]:
                    leader_reused = True
                if int(fields[2]) != pid and int(fields[3]) != pid:
                    continue
                if fields[0] in {"Z", "X"}:
                    continue
                if (
                    int(fields[2]) != pid
                    or int(fields[3]) != pid
                    or int(fields[19]) < int(identity["start_ticks"])
                    or entry.stat().st_uid != identity["uid"]
                    or identity["nonce"].encode() not in (entry / "cmdline").read_bytes().split(b"\0")
                ):
                    return "unknown", []
                members.append({**identity, "pid": member_pid, "start_ticks": fields[19]})
            except (FileNotFoundError, ProcessLookupError):
                continue
        if leader_reused and members:
            return "unknown", []
        if members:
            return "alive", members
        # /proc enumeration is not atomic: a leader may fork after the listing.
        # Only kernel-confirmed group absence closes the empty-snapshot case.
        try:
            os.killpg(pid, 0)
        except ProcessLookupError:
            return "dead", []
        return "unknown", []
    except (OSError, KeyError, TypeError, ValueError, IndexError):
        return "unknown", []


def resource_state(identity: dict) -> str:
    """The whole owned group, rather than its leader, is the resource boundary."""
    return _group_snapshot(identity)[0]


def _signal_members(members: list[dict], number: int) -> bool:
    if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
        return False
    for member in members:
        descriptor = None
        try:
            descriptor = os.pidfd_open(member["pid"])
            if process_identity(member["pid"], member["nonce"]) != member:
                return False
            argv = Path(f"/proc/{member['pid']}/cmdline").read_bytes().split(b"\0")
            if member["nonce"].encode() not in argv:
                return False
            signal.pidfd_send_signal(descriptor, number)
        except (FileNotFoundError, ProcessLookupError):
            continue
        except (OSError, KeyError):
            return False
        finally:
            if descriptor is not None:
                os.close(descriptor)
    return True


def terminate_owned(identity: dict, *, grace_seconds: float = 2, reap=None) -> bool:
    """Signal pidfd-bound members; success requires kernel group absence.

    A caller owning a Popen object supplies its nonblocking poll method so that
    its exit status remains with that object. Other children require their real
    parent or OS reaper; an existing zombie-only group remains unresolved.
    """

    def observe():
        if reap is not None:
            try:
                reap()
            except Exception:
                return "unknown", []
        return _group_snapshot(identity)

    for number in (signal.SIGTERM, signal.SIGKILL):
        deadline = time.monotonic() + grace_seconds
        while True:
            state, members = observe()
            if state == "dead":
                return True
            if state == "foreign":
                return False
            if state == "alive" and not _signal_members(members, number):
                return False
            if time.monotonic() >= deadline:
                break
            time.sleep(0.02)
    return observe()[0] == "dead"


