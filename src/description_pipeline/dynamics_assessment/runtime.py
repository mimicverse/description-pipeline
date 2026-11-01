"""Owned Linux processes and a fixed Teleop subprocess entry point."""

from __future__ import annotations

import hashlib
import signal
import subprocess
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from ..io import canonical
from ..owned_process import process_identity, resource_state, terminate_owned
from .contracts import AssessmentConflict, RuntimePin, require

# This code is part of the assessment executor bytes. It receives only data,
# validates the installed runtime, and delegates execution to Teleop.
BOOTSTRAP = r"""
import hashlib, json, os, resource, runpy, sys
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
    helper_path = Path(gate["runtime_checker"]["path"])
    helper_bytes = helper_path.read_bytes()
    if hashlib.sha256(helper_bytes).hexdigest() != gate["runtime_checker"]["sha256"]:
        raise ValueError("Assessment runtime checker differs from the fixed executor")
    api = {"__file__": str(helper_path), "__name__": "_assessment_runtime_checker"}
    exec(compile(helper_bytes, str(helper_path), "exec"), api)
    contract = {key: value for key, value in pin.items() if key != "schema"}
    contract["schema_version"] = api["RUNTIME_CONTRACT_SCHEMA"]
    identity = api["verify_runtime_contract"](contract)
    Path(gate["runtime_receipt"]).write_text(json.dumps({
        "runtime": pin, "python": sys.version, "identity": identity,
        "runtime_checker_sha256": gate["runtime_checker"]["sha256"]
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


RUNTIME_CHECKER = Path(__file__).resolve().parents[1] / "simulation/runtime.py"


class TeleopProcess:
    """Gate native execution until its identity is durably associated with the attempt."""

    def __init__(self, python: Path, runtime: RuntimePin, limits: ProcessLimits):
        require(python.is_absolute() and python.is_file(), "Assessment interpreter must be an existing absolute file")
        self.python = python
        self.runtime = runtime
        self.limits = limits
        self.runtime_checker = RUNTIME_CHECKER
        require(
            self.runtime_checker.is_absolute() and self.runtime_checker.is_file(),
            "The fixed assessment runtime checker is unavailable",
        )
        self._runtime_checker_sha256 = hashlib.sha256(self.runtime_checker.read_bytes()).hexdigest()

    def _checker_identity(self) -> dict:
        require(
            self.runtime_checker.is_absolute() and self.runtime_checker.is_file(),
            "The fixed assessment runtime checker is unavailable",
        )
        actual = hashlib.sha256(self.runtime_checker.read_bytes()).hexdigest()
        require(actual == self._runtime_checker_sha256, "Assessment runtime checker changed within the executor")
        return {"path": str(self.runtime_checker), "sha256": actual}

    @property
    def adapter_sha256(self) -> str:
        return hashlib.sha256(canonical({
            "bootstrap_sha256": hashlib.sha256(BOOTSTRAP.encode()).hexdigest(),
            "runtime_checker_sha256": self._checker_identity()["sha256"],
        })).hexdigest()

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
            "runtime_checker": self._checker_identity(),
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
