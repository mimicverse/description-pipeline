"""Owned Linux processes and a fixed Teleop subprocess entry point."""

from __future__ import annotations

import hashlib
import os
import signal
import socket
import subprocess
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from ..io import canonical
from .contracts import AssessmentConflict, RuntimePin, require

# This code is part of the assessment executor bytes. It receives only data,
# validates the installed runtime, and delegates execution to Teleop.
BOOTSTRAP = r"""
import hashlib, importlib.metadata, importlib.util, json, os, resource, runpy, sys
from pathlib import Path
try:
    gate = json.loads(sys.stdin.readline())
    if gate["nonce"] != sys.argv[1]:
        raise ValueError("Assessment process gate identity differs")
    os.umask(0o077)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_AS, (gate["memory_bytes"], gate["memory_bytes"]))
    resource.setrlimit(resource.RLIMIT_CPU, (gate["cpu_seconds"], gate["cpu_seconds"]))
    resource.setrlimit(resource.RLIMIT_FSIZE, (gate["output_bytes"], gate["output_bytes"]))
    pin = gate["runtime"]
    if sys.version_info[:2] != (3, 12):
        raise ValueError("Assessment runtime requires CPython 3.12")
    spec = importlib.util.find_spec("teleop")
    if spec is None or spec.origin is None:
        raise ValueError("Fixed Teleop distribution is not installed")
    root = Path(spec.origin).resolve().parent.parent
    actual_names = {p.relative_to(root).as_posix() for p in (root / "teleop").rglob("*")
                    if p.is_file() and p.suffix not in {".pyc", ".pyo"} and "__pycache__" not in p.parts}
    if actual_names != set(pin["files"]):
        raise ValueError("Installed Teleop file inventory differs from the fixed runtime")
    for name, expected in pin["files"].items():
        path = root / name
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError("Installed Teleop file escapes the fixed runtime")
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError("Installed Teleop file digest differs: " + name)
    versions = {name: importlib.metadata.version(name) for name in pin["dependency_versions"]}
    if versions != pin["dependency_versions"]:
        raise ValueError("Installed dependency versions differ from the fixed runtime")
    Path(gate["runtime_receipt"]).write_text(json.dumps({
        "runtime": pin, "python": sys.version, "teleop_root": str(root), "versions": versions
    }, sort_keys=True) + "\n")
except Exception as error:
    print(json.dumps({"assessment_error": "runtime_identity", "error": str(error)}), flush=True)
    sys.exit(78)
operation = gate["operation"]
if operation == "run":
    try:
        from teleop.evaluation.continuous_dynamics.plan import validate_plan
        plan_path = Path(gate["plan"])
        validate_plan(json.loads(plan_path.read_bytes()), plan_path)
    except ValueError as error:
        print(json.dumps({"assessment_error": "unsupported_input", "error": str(error)}), flush=True)
        sys.exit(65)
    sys.argv = ["teleop.evaluation", "run", "--plan", gate["plan"], "--output", gate["run"]]
elif operation == "verify":
    sys.argv = ["teleop.evaluation", "verify", "--run", gate["run"]]
else:
    raise ValueError("Unknown fixed assessment operation")
runpy.run_module("teleop.evaluation", run_name="__main__")
"""


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


@dataclass(frozen=True)
class ProcessLimits:
    wall_seconds: float
    memory_bytes: int
    cpu_seconds: int
    output_bytes: int

    def __post_init__(self):
        require(
            type(self.wall_seconds) in (int, float) and 0 < self.wall_seconds <= 86400,
            "Assessment wall-time budget is invalid",
        )
        require(type(self.memory_bytes) is int and self.memory_bytes > 0, "Assessment memory budget is invalid")
        require(type(self.cpu_seconds) is int and self.cpu_seconds > 0, "Assessment CPU budget is invalid")
        require(type(self.output_bytes) is int and self.output_bytes > 0, "Assessment output budget is invalid")


class TeleopProcess:
    """Gate native execution until its identity is durably associated with the attempt."""

    def __init__(self, python: Path, runtime: RuntimePin, limits: ProcessLimits):
        require(python.is_absolute() and python.is_file(), "Assessment interpreter must be an existing absolute file")
        self.python = python
        self.runtime = runtime
        self.limits = limits

    @property
    def adapter_sha256(self) -> str:
        return hashlib.sha256(BOOTSTRAP.encode()).hexdigest()

    def run(
        self, *, operation: str, root: Path, plan: Path, output: Path, nonce: str, checkpoint, should_cancel
    ) -> dict:
        require(operation in {"run", "verify"}, "Assessment subprocess operation is invalid")
        environment = {
            "PATH": "/usr/bin:/bin",
            "LANG": "C.UTF-8",
            "HOME": str(root / "home"),
            "OPENBLAS_NUM_THREADS": "1",
            "OMP_NUM_THREADS": "1",
        }
        (root / "home").mkdir(exist_ok=True)
        stdout_path = root / (operation + ".stdout.json")
        stderr_path = root / (operation + ".stderr.log")
        gate = {
            "nonce": nonce,
            "runtime": self.runtime.to_record(),
            "operation": operation,
            "plan": str(plan),
            "run": str(output),
            "runtime_receipt": str(root / (operation + ".runtime.json")),
            "memory_bytes": self.limits.memory_bytes,
            "cpu_seconds": self.limits.cpu_seconds,
            "output_bytes": self.limits.output_bytes,
        }
        identity = None
        process = None
        reason = None
        started = time.monotonic()
        last_size_check = started
        next_heartbeat = started + 5
        with stdout_path.open("xb") as stdout, stderr_path.open("xb") as stderr:
            try:
                process = subprocess.Popen(
                    [
                        str(self.python),
                        "-I",
                        "-B",
                        "-X",
                        "pycache_prefix=" + str(root / "home/pycache"),
                        "-c",
                        BOOTSTRAP,
                        nonce,
                    ],
                    cwd=root,
                    stdin=subprocess.PIPE,
                    stdout=stdout,
                    stderr=stderr,
                    env=environment,
                    start_new_session=True,
                )
                identity = process_identity(process.pid, nonce)
                checkpoint(identity)
                if should_cancel():
                    reason = "cancelled"
                else:
                    process.stdin.write(canonical(gate) + b"\n")
                    process.stdin.flush()
                    process.stdin.close()
                while process.poll() is None:
                    if time.monotonic() >= next_heartbeat:
                        checkpoint(identity)
                        next_heartbeat = time.monotonic() + 5
                    if reason is None and should_cancel():
                        reason = "cancelled"
                    if reason is None and time.monotonic() - started > self.limits.wall_seconds:
                        reason = "timeout"
                    if (
                        reason is None
                        and stdout_path.stat().st_size + stderr_path.stat().st_size > self.limits.output_bytes
                    ):
                        reason = "output_limit"
                    if reason is None and time.monotonic() - last_size_check >= 1:
                        generated_bytes = sum(path.stat().st_size for path in root.rglob("*") if path.is_file())
                        if generated_bytes > self.limits.output_bytes:
                            reason = "output_limit"
                        last_size_check = time.monotonic()
                    if reason and not terminate_owned(identity, reap=process.poll):
                        raise AssessmentConflict("Assessment subprocess termination could not be settled")
                    time.sleep(0.05)
                if resource_state(identity) != "dead":
                    reason = reason or "residual_processes"
                    if not terminate_owned(identity, reap=process.poll):
                        raise AssessmentConflict("Assessment process group exit could not be settled")
                checkpoint(None)
            except BaseException:
                if process is not None and process.stdin is not None and not process.stdin.closed:
                    # Closing an unreleased gate lets that child exit without a signal.
                    process.stdin.close()
                if identity is not None:
                    terminate_owned(identity, reap=process.poll)
                if process is not None:
                    with suppress(subprocess.TimeoutExpired):
                        process.wait(timeout=3)
                raise
            finally:
                if process is not None and process.stdin is not None and not process.stdin.closed:
                    process.stdin.close()
        if identity is None or resource_state(identity) != "dead":
            raise AssessmentConflict("Assessment subprocess exit could not be settled")
        process.wait()
        if reason is None and process.returncode == -signal.SIGXFSZ:
            reason = "output_limit"
        return {
            "exit_code": process.returncode,
            "reason": reason,
            "identity": identity,
            "elapsed_seconds": time.monotonic() - started,
            "stdout": stdout_path.name,
            "stderr": stderr_path.name,
        }
