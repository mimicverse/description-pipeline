"""Own one Linux SSH connection while a Windows worker freezes the source."""

from __future__ import annotations

import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from urllib.parse import urlsplit

from ..io import PipelineError, write_json
from ..sources.solidworks.remote import WorkerClient
from ..sources.solidworks.errors import BridgeError


def _forward(source: dict, host: str, worker_port: int) -> tuple[str, str]:
    if sys.platform != "linux":
        raise PipelineError("Managed worker tunnels require Linux; use the packaged Windows submit.ps1 on Windows")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", host):
        raise PipelineError("--worker-host must be a plain SSH alias; configure its user and key in ~/.ssh/config")
    if source.get("provider") != "solidworks":
        raise PipelineError("--worker-host is only valid for a SolidWorks source")
    if type(worker_port) is not int or not 1 <= worker_port <= 65535:
        raise PipelineError("--worker-port must be an integer between 1 and 65535")
    url = str(source.get("worker_url") or "")
    parsed = urlsplit(url)
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
        or parsed.port is None
        or not 1 <= parsed.port <= 65535
    ):
        raise PipelineError("Managed tunnels require source.worker_url = http://127.0.0.1:<local-port>")
    return url, f"127.0.0.1:{parsed.port}:127.0.0.1:{worker_port}"


def _idle(health: dict) -> None:
    runner = health.get("runner") or {}
    jobs = health.get("jobs") or {}
    if not isinstance(runner, dict) or not isinstance(jobs, dict):
        raise PipelineError("Windows worker returned an invalid health record")
    if (
        health.get("status") != "ok"
        or runner.get("alive") is not True
        or health.get("maintenance")
        or health.get("cad_recovery_required")
        or health.get("cad_operation_active")
        or runner.get("current")
        or runner.get("queued")
        or jobs.get("queued")
        or jobs.get("running")
    ):
        raise PipelineError("Windows worker is unavailable or busy; finish its active work and retry")


def worker_health(url: str, timeout: int = 10) -> dict:
    try:
        health = WorkerClient(url, http_timeout=timeout).health()
    except BridgeError as error:
        raise PipelineError(
            f"Windows worker is not reachable at {url}; start the local worker, or for remote capture "
            "on Linux pass --worker-host SSH_ALIAS, "
            "or use --reuse-source to rebuild a frozen source without CAD"
        ) from error
    _idle(health)
    return health


@contextmanager
def worker_tunnel(root: Path, source: dict, host: str, worker_port: int = 8765) -> Iterator[dict]:
    """Prove ownership of the forward before probing HTTP; never adopt a busy port.

    The foreground SSH child has a private control socket. A synchronous mux forward
    acknowledges the bind, unlike a TCP probe which might find another listener.
    No remote command is executed, and only this child is terminated during cleanup.
    The final liveness check is best effort; capture still reports later disconnects.
    SIGTERM handling is installed only for callers on Python's main thread.
    """

    url, forward = _forward(source, host, worker_port)
    # A short, private path avoids Unix socket limits and an author-controlled TMPDIR.
    with tempfile.TemporaryDirectory(prefix="desc-ssh-", dir="/tmp") as temporary:
        socket = str(Path(temporary) / "ctl")
        log = Path(temporary) / "ssh.log"
        process = None
        previous_signal = None

        def interrupted(signum, _frame):
            raise KeyboardInterrupt(f"Worker connection interrupted by signal {signum}")

        try:
            if threading.current_thread() is threading.main_thread():
                previous_signal = signal.signal(signal.SIGTERM, interrupted)
            with log.open("w+b") as stderr:
                process = subprocess.Popen(
                    [
                        "ssh",
                        "-M",
                        "-N",
                        "-S",
                        socket,
                        "-o",
                        "ControlMaster=yes",
                        "-o",
                        "ControlPersist=no",
                        "-o",
                        "ForkAfterAuthentication=no",
                        "-o",
                        "PermitLocalCommand=no",
                        "-o",
                        "BatchMode=yes",
                        "-o",
                        "StrictHostKeyChecking=yes",
                        "-o",
                        "ConnectTimeout=10",
                        "-o",
                        "ConnectionAttempts=1",
                        "-o",
                        "ServerAliveInterval=15",
                        "-o",
                        "ServerAliveCountMax=4",
                        "-o",
                        "ClearAllForwardings=yes",
                        host,
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=stderr,
                )
                deadline = time.monotonic() + 15
                while True:
                    if process.poll() is not None:
                        raise PipelineError("SSH connection failed; check the SSH alias, key and known_hosts")
                    if time.monotonic() >= deadline:
                        raise PipelineError("SSH connection timed out; check that the Windows computer is awake")
                    if Path(socket).exists():
                        checked = subprocess.run(
                            ["ssh", "-F", "/dev/null", "-S", socket, "-O", "check", host],
                            capture_output=True,
                            timeout=3,
                        )
                        if checked.returncode == 0:
                            break
                        stderr.write(checked.stderr)
                        stderr.flush()
                    time.sleep(0.1)
                forwarded = subprocess.run(
                    ["ssh", "-F", "/dev/null", "-S", socket, "-O", "forward", "-L", forward, host],
                    capture_output=True,
                    timeout=10,
                )
                if forwarded.returncode:
                    stderr.write(forwarded.stderr)
                    stderr.flush()
                    raise PipelineError("SSH forwarding failed; the local port may be in use or require privileges")
                health = worker_health(url)
                if process.poll() is not None:
                    raise PipelineError("SSH connection closed before source capture")
                yield {
                    "host": host,
                    "worker_port": worker_port,
                    "ssh_pid": process.pid if process is not None else None,
                    "control_socket": socket,
                    "worker_url": url,
                    "worker_version": health.get("worker_version"),
                }
        except BaseException as error:
            # Preserve stderr before deleting the socket directory, including on Ctrl+C.
            failed = getattr(error, "diagnostic_path", None)
            if not failed:
                parent = root / "build/failed-connection"
                parent.mkdir(parents=True, exist_ok=True)
                failed = tempfile.mkdtemp(prefix="ssh-", dir=parent)
                vars(error)["diagnostic_path"] = failed
            detail = log.read_bytes() if log.exists() else b""
            if detail and isinstance(error, PipelineError):
                vars(error)["stderr"] = detail
            (Path(failed) / "ssh.log").write_bytes(detail)
            write_json(
                Path(failed) / "connection.json",
                {
                    "host": host,
                    "worker_url": url,
                    "worker_port": worker_port,
                    "error": type(error).__name__,
                    "ssh_pid": process.pid if process is not None else None,
                    "control_socket": socket,
                    "message": str(error),
                },
            )
            raise
        finally:
            # A second cancellation must not interrupt reaping the owned child.
            saved = {}
            if threading.current_thread() is threading.main_thread():
                for signum in (signal.SIGTERM, signal.SIGINT):
                    saved[signum] = signal.signal(signum, signal.SIG_IGN)
            try:
                if process is not None and process.poll() is None:
                    with suppress(ProcessLookupError):
                        process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        with suppress(ProcessLookupError):
                            process.kill()
                        process.wait(timeout=5)
            finally:
                if previous_signal is not None:
                    saved[signal.SIGTERM] = previous_signal
                for signum, handler in saved.items():
                    signal.signal(signum, handler)
