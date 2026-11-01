"""Real Linux child ownership and bounded unresolved termination."""

from __future__ import annotations

import ctypes
import os
import secrets
import signal
import subprocess
import sys
import time
from contextlib import contextmanager, suppress
from pathlib import Path
from unittest import mock

from description_pipeline.dynamics_assessment import runtime as runtime_module
from description_pipeline.dynamics_assessment.contracts import AssessmentConflict
from description_pipeline.dynamics_assessment.runtime import (
    ProcessLimits,
    TeleopProcess,
    process_identity,
    process_state,
    resource_state,
    terminate_owned,
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
sys.stdin.readline()
"""


@contextmanager
def owned_group(root, *, unbound=False):
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
            "unbound" if unbound else "bound",
        ],
        start_new_session=True,
        stdin=subprocess.PIPE,
    )
    identity = process_identity(leader.pid, nonce)
    child = None
    try:
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert marker.exists()
        child = process_identity(int(marker.read_text()), nonce)
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
    def _check_group_counterexample(self, already_exited):
        with owned_group(self.root) as (leader, identity, child):
            if already_exited:
                leader.stdin.write(b"exit\n")
                leader.stdin.flush()
                leader.wait(timeout=5)
                self.assertEqual(process_state(identity), "dead")
                self.assertEqual(process_state(child), "alive")
            self.assertTrue(terminate_owned(identity, grace_seconds=0.1))
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
            if identities and identities[0] is not None:
                terminate_owned(identities[0], grace_seconds=0.1)
            if marker.exists():
                pid = int(marker.read_text())
                fd = os.pidfd_open(pid)
                try:
                    with suppress(ProcessLookupError):
                        signal.pidfd_send_signal(fd, signal.SIGKILL)
                finally:
                    os.close(fd)
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
            assert terminate_owned(identity, grace_seconds=0.1)
            assert process_state(identity) == "dead"
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
