"""Real Linux child ownership and bounded unresolved termination."""

from __future__ import annotations

import ctypes
import os
import secrets
import signal
import subprocess
import sys
import threading
import time
from contextlib import contextmanager, suppress
from pathlib import Path
from unittest import mock

from description_pipeline.dynamics_assessment import runtime as runtime_module
from description_pipeline.dynamics_assessment.contracts import AssessmentConflict
from description_pipeline.owned_process import (
    module_command, process_identity, process_state, resource_state, terminate_owned,
)
from description_pipeline.dynamics_assessment.runtime import (
    ProcessLimits,
    TeleopProcess,
)
from tests.v1.test_dynamics_assessment_contracts import AssessmentCase, runtime_pin

CHILD = """
import os, signal, sys, time
from pathlib import Path
signal.signal(signal.SIGTERM, signal.SIG_IGN)
Path(sys.argv[1]).write_text(str(os.getpid()))
while True:
    time.sleep(0.05)
"""

GROUP = """
import os, signal, subprocess, sys, time
from pathlib import Path
marker = Path(sys.argv[2])
if sys.argv[3] == 'late':
    sys.stdin.readline()
if sys.argv[3] == 'unbound':
    subprocess.Popen([sys.executable, '-I', '-c', CHILD, str(marker)])
else:
    child = os.fork()
    if child == 0:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        marker.write_text(str(os.getpid()))
        while True:
            time.sleep(0.05)
while not marker.exists():
    time.sleep(0.01)
if sys.argv[3] == 'late':
    sys.exit(0)
sys.stdin.readline()
"""


@contextmanager
def owned_group(root, *, unbound=False, late=False):
    """Own and reap every helper process, including orphaned grandchildren."""
    libc = ctypes.CDLL(None, use_errno=True)
    previous = ctypes.c_int()
    assert libc.prctl(37, ctypes.byref(previous), 0, 0, 0) == 0
    assert libc.prctl(36, 1, 0, 0, 0) == 0
    nonce = secrets.token_hex(32)
    marker = root / (nonce + ".pid")
    leader = subprocess.Popen(
        [
            sys.executable,
            "-I",
            "-c",
            "CHILD=" + repr(CHILD) + "\n" + GROUP,
            nonce,
            str(marker),
            "late" if late else ("unbound" if unbound else "bound"),
        ],
        start_new_session=True,
        stdin=subprocess.PIPE,
    )
    identity = process_identity(leader.pid, nonce)
    child = None
    child_record = {}

    def capture_child():
        nonlocal child
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                pid = int(marker.read_text())
                break
            except (FileNotFoundError, ValueError):
                pass
            time.sleep(0.01)
        else:
            raise AssertionError("The owned helper did not report its child")
        child = process_identity(pid, nonce)
        child_record["identity"] = child

    def trigger_fork():
        leader.stdin.write(b"fork\n")
        leader.stdin.flush()
        capture_child()
        leader.wait(timeout=5)

    try:
        if late:
            yield leader, identity, trigger_fork, child_record
        else:
            capture_child()
            yield leader, identity, child
    finally:
        for member in (identity, child):
            if member is None or process_state(member) != "alive":
                continue
            fd = os.pidfd_open(member["pid"])
            try:
                if process_identity(member["pid"], nonce) == member:
                    signal.pidfd_send_signal(fd, signal.SIGKILL)
            finally:
                os.close(fd)
        if leader.stdin is not None:
            leader.stdin.close()
        leader.wait(timeout=5)
        if child is not None:
            with suppress(ChildProcessError):
                os.waitpid(child["pid"], 0)
        assert libc.prctl(36, previous.value, 0, 0, 0) == 0


def reap_group(leader, child):
    leader.poll()
    with suppress(ChildProcessError):
        os.waitpid(child["pid"], os.WNOHANG)


@contextmanager
def fork_after_proc_listing(trigger):
    """Schedule real fork/exit after the real kernel listing has been captured."""
    original = Path.iterdir
    armed = True

    def scheduled(directory):
        nonlocal armed
        entries = list(original(directory))
        if directory == Path("/proc") and armed:
            armed = False
            trigger()
        return iter(entries)

    with mock.patch.object(Path, "iterdir", scheduled):
        yield
    assert not armed


GATED_GROUP = """
import json, os, signal, sys, time
from pathlib import Path
gate = json.loads(sys.stdin.readline())
marker = Path(gate['plan'])
child = os.fork()
if child == 0:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    marker.write_text(str(os.getpid()))
    while True:
        time.sleep(0.05)
while not marker.exists():
    time.sleep(0.01)
if MODE == 'normal':
    sys.exit(0)
while True:
    time.sleep(0.05)
"""


class RuntimeTests(AssessmentCase):
    def test_module_marker_binds_kernel_argv_without_changing_public_module_arguments(self):
        nonce = secrets.token_hex(32)
        process = subprocess.Popen(
            module_command(Path(sys.executable), "json.tool", nonce),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True,
        )
        identity = process_identity(process.pid, nonce)
        try:
            self.assertEqual(resource_state(identity), "alive")
            output, error = process.communicate(b'{"value":1}', timeout=5)
            self.assertEqual(process.returncode, 0, error.decode())
            self.assertEqual(output, b'{\n    "value": 1\n}\n')
            self.assertEqual(resource_state(identity), "dead")
        finally:
            if process.poll() is None:
                terminate_owned(identity, grace_seconds=0.1, reap=process.poll)
            process.communicate(timeout=5)

    def test_empty_proc_snapshot_is_unknown_when_kernel_group_still_exists(self):
        with owned_group(self.root, late=True) as (_leader, identity, trigger, record):
            with fork_after_proc_listing(trigger):
                self.assertEqual(resource_state(identity), "unknown")
            self.assertEqual(process_state(record["identity"]), "alive")
            self.assertEqual(resource_state(identity), "alive")

    def test_scan_fork_exit_race_cannot_return_success_before_kernel_group_absence(self):
        with owned_group(self.root, late=True) as (leader, identity, trigger, record):
            with fork_after_proc_listing(trigger):
                self.assertFalse(terminate_owned(identity, grace_seconds=0.1, reap=leader.poll))
            self.assertNotEqual(resource_state(identity), "dead")
            os.killpg(identity["pgid"], 0)
            reap_group(leader, record["identity"])
            self.assertEqual(resource_state(identity), "dead")
            self.assertTrue(terminate_owned(identity, grace_seconds=0.1))

    def _check_group_counterexample(self, already_exited):
        with owned_group(self.root) as (leader, identity, child):
            if already_exited:
                leader.stdin.write(b"exit\n")
                leader.stdin.flush()
                leader.wait(timeout=5)
                self.assertEqual(process_state(identity), "dead")
                self.assertEqual(process_state(child), "alive")
            self.assertTrue(terminate_owned(identity, grace_seconds=0.1, reap=lambda: reap_group(leader, child)))
            self.assertEqual(resource_state(identity), "dead")
            self.assertEqual(process_state(child), "dead")

    def test_sigterm_leader_exit_does_not_hide_ignoring_grandchild(self):
        self._check_group_counterexample(False)

    def test_already_exited_leader_does_not_hide_ignoring_grandchild(self):
        self._check_group_counterexample(True)

    def test_unbound_group_member_stays_unresolved_and_is_not_signalled(self):
        with owned_group(self.root, unbound=True) as (leader, identity, child):
            self.assertEqual(resource_state(identity), "unknown")
            self.assertFalse(terminate_owned(identity, grace_seconds=0.1))
            self.assertIsNone(leader.poll())
            self.assertEqual(process_state(child), "alive")

    def _check_gated_group(self, mode):
        libc = ctypes.CDLL(None, use_errno=True)
        previous = ctypes.c_int()
        self.assertEqual(libc.prctl(37, ctypes.byref(previous), 0, 0, 0), 0)
        self.assertEqual(libc.prctl(36, 1, 0, 0, 0), 0)
        marker = self.root / "grandchild.pid"
        identities = []
        stopped = threading.Event()

        def reap_adopted_child():
            while not stopped.wait(0.01):
                if marker.exists():
                    with suppress(ChildProcessError):
                        os.waitpid(int(marker.read_text()), os.WNOHANG)

        reaper = threading.Thread(target=reap_adopted_child)
        reaper.start()

        def checkpoint(identity):
            identities.append(identity)
            if identity is None:
                self.assertEqual(resource_state(identities[-2]), "dead")

        process = TeleopProcess(
            Path(sys.executable), runtime_pin(), ProcessLimits(0.2 if mode == "timeout" else 10, 1024**3, 10, 1024**2)
        )
        try:
            with mock.patch.object(runtime_module, "BOOTSTRAP", "MODE=" + repr(mode) + "\n" + GATED_GROUP):
                result = process.run(
                    operation="run",
                    root=self.root,
                    plan=marker,
                    output=self.root / "unused",
                    nonce=secrets.token_hex(32),
                    checkpoint=checkpoint,
                    should_cancel=lambda: mode == "cancelled" and marker.exists(),
                )
            self.assertEqual(result["reason"], "residual_processes" if mode == "normal" else mode)
            self.assertEqual(resource_state(result["identity"]), "dead")
            self.assertIsNone(identities[-1])
        finally:
            stopped.set()
            reaper.join(timeout=5)
            if identities and identities[0] is not None:
                terminate_owned(identities[0], grace_seconds=0.1)
            if marker.exists():
                pid = int(marker.read_text())
                with suppress(ProcessLookupError):
                    fd = os.pidfd_open(pid)
                    try:
                        with suppress(ProcessLookupError):
                            signal.pidfd_send_signal(fd, signal.SIGKILL)
                    finally:
                        os.close(fd)
                with suppress(ChildProcessError):
                    os.waitpid(pid, 0)
            libc.prctl(36, previous.value, 0, 0, 0)

    def test_normal_native_entry_exit_settles_group_before_checkpoint_release(self):
        self._check_gated_group("normal")

    def test_cancel_native_entry_settles_group_before_checkpoint_release(self):
        self._check_gated_group("cancelled")

    def test_timeout_native_entry_settles_group_before_checkpoint_release(self):
        self._check_gated_group("timeout")

    def test_foreign_or_unbound_identity_cannot_signal_a_live_child(self):
        nonce = secrets.token_hex(32)
        process = subprocess.Popen(
            [sys.executable, "-I", "-c", "import time; time.sleep(30)", nonce], start_new_session=True
        )
        identity = process_identity(process.pid, nonce)
        try:
            foreign = {**identity, "host": "not-the-owning-host"}
            assert process_state(foreign) == "foreign"
            assert not terminate_owned(foreign, grace_seconds=0.1)
            assert not terminate_owned({**identity, "nonce": "another-child"}, grace_seconds=0.1)
            assert not terminate_owned({**identity, "start_ticks": "0"}, grace_seconds=0.1)
            assert process.poll() is None
            assert terminate_owned(identity, grace_seconds=0.1, reap=process.poll)
            assert process_state(identity) == "dead"
            self.assertEqual(process.returncode, -signal.SIGTERM)
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)

    def test_unsettled_termination_returns_conflict_without_releasing_identity(self):
        tmp_path = self.root
        process = TeleopProcess(Path(sys.executable), runtime_pin(), ProcessLimits(30, 1024**3, 10, 1024**2))
        registered = []

        def checkpoint(identity):
            registered.append(dict(identity))
            identity["host"] = "unavailable-resource-host"

        started = time.monotonic()
        with self.assertRaisesRegex(AssessmentConflict, "termination could not be settled"):
            process.run(
                operation="run",
                root=tmp_path,
                plan=tmp_path / "unused-plan.json",
                output=tmp_path / "unused-output",
                nonce=secrets.token_hex(32),
                checkpoint=checkpoint,
                should_cancel=lambda: True,
            )
        assert time.monotonic() - started < 8
        assert len(registered) == 1
        deadline = time.monotonic() + 3
        while process_state(registered[0]) == "alive" and time.monotonic() < deadline:
            time.sleep(0.01)
        assert process_state(registered[0]) == "dead"
