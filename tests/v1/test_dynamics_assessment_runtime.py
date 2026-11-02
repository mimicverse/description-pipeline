"""Real Linux child ownership and bounded unresolved termination."""

from __future__ import annotations

import base64
import csv
import ctypes
import hashlib
import io
import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager, suppress
from pathlib import Path
from unittest import mock

from description_pipeline.dynamics_assessment import runtime as runtime_module
from description_pipeline.dynamics_assessment.contracts import AssessmentConflict, RuntimePin
from description_pipeline.owned_process import (
    module_command, process_identity, process_state, resource_state, terminate_owned,
)
from tests.v1.assessment_fixture import selected_runtime
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

        # This ownership-only child does not call the runtime checker.
        with mock.patch.object(runtime_module, "RUNTIME_CHECKER", Path(runtime_module.__file__)):
            process = TeleopProcess(
                Path(sys.executable), runtime_pin(),
                ProcessLimits(0.2 if mode == "timeout" else 10, 1024**3, 10, 1024**2),
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
        # Cancellation closes stdin before any checker code can execute.
        with mock.patch.object(runtime_module, "RUNTIME_CHECKER", Path(runtime_module.__file__)):
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


class NativeRuntimeGateTests(AssessmentCase):
    def setUp(self):
        super().setUp()
        self.python, self.pin = selected_runtime()
        self.checker = runtime_module.RUNTIME_CHECKER

    def execute_gate(self, pin=None, checkpoint=None):
        with mock.patch.object(runtime_module, "RUNTIME_CHECKER", self.checker):
            process = TeleopProcess(self.python, pin or self.pin, ProcessLimits(30, 2 * 1024**3, 20, 64 * 1024**2))
        plan = self.root / "unsupported-plan.json"
        plan.write_text("{}\n")
        identities = []

        def register(identity):
            identities.append(identity)
            if checkpoint is not None:
                checkpoint(identity)

        result = process.run(
            operation="run", root=self.root, plan=plan, output=self.root / "unused-run",
            nonce=secrets.token_hex(32), checkpoint=register, should_cancel=lambda: False,
        )
        self.assertIsNone(identities[-1])
        self.assertEqual(resource_state(result["identity"]), "dead")
        self.assertFalse((self.root / "unused-run").exists())
        return result, json.loads((self.root / "run.stdout.json").read_bytes())

    def test_release_checker_preserves_native_package_and_consumer_asset_separately(self):
        result, output = self.execute_gate()
        self.assertEqual(result["exit_code"], 65, output)
        self.assertEqual(output["assessment_error"], "unsupported_input")
        receipt = json.loads((self.root / "run.runtime.json").read_bytes())
        self.assertEqual(receipt["runtime"], self.pin.to_record())
        self.assertEqual(receipt["identity"]["actuator_model"], {
            "version": "1.3.0", "git_commit": None, "editable": False,
        })
        self.assertEqual(receipt["identity"]["actuator_model_asset"], self.pin.record["actuator_model"])
        self.assertEqual(receipt["identity"]["engine"], self.pin.record["dependency_versions"]["mujoco"])

    def test_release_checker_rejects_wrong_dependency_before_model_execution(self):
        record = self.pin.to_record()
        record["dependency_versions"]["mujoco"] = "0.0.0"
        result, output = self.execute_gate(RuntimePin(record))
        self.assertEqual(result["exit_code"], 78)
        self.assertEqual(output["assessment_error"], "runtime_identity")
        self.assertIn("dependencies differ", output["error"])
        self.assertFalse((self.root / "run.runtime.json").exists())

    def test_release_checker_rejects_wrong_actuator_archive_before_model_execution(self):
        record = self.pin.to_record()
        record["actuator_model"]["wheel_sha256"] = "0" * 64
        result, output = self.execute_gate(RuntimePin(record))
        self.assertEqual(result["exit_code"], 78)
        self.assertEqual(output["assessment_error"], "runtime_identity")
        self.assertFalse((self.root / "run.runtime.json").exists())

    def test_checker_mutation_after_registration_is_rejected_before_executing_its_bytes(self):
        checker = self.root / "release-checker.py"
        checker.write_bytes(self.checker.read_bytes())
        self.checker = checker
        marker = self.root / "injected-marker"

        def mutate(identity):
            if identity is not None:
                checker.write_text("from pathlib import Path\nPath(" + repr(str(marker)) + ").touch()\n")

        result, output = self.execute_gate(checkpoint=mutate)
        self.assertEqual(result["exit_code"], 78)
        self.assertIn("checker differs", output["error"])
        self.assertFalse(marker.exists())
        self.assertFalse((self.root / "run.runtime.json").exists())


    def clone_native_runtime(self):
        temporary = tempfile.TemporaryDirectory(prefix="assessment-runtime-")
        self.addCleanup(temporary.cleanup)
        clone = Path(temporary.name) / "runtime"
        shutil.copytree(self.python.parent.parent, clone, symlinks=True)
        self.python = clone / "bin/python"
        info = subprocess.run(
            [str(self.python), "-I", "-B", "-c",
             "from importlib.metadata import distribution;d=distribution('actuator-model');"
             "print(next(d.locate_file(p).parent for p in d.files "
             "if str(p).endswith('.dist-info/direct_url.json')))"],
            capture_output=True, text=True, check=True, timeout=10,
        )
        return Path(info.stdout.strip())

    def alter_native_origin(self, change):
        distribution = self.clone_native_runtime()
        origin_path = distribution / "direct_url.json"
        inventory_path = distribution / "RECORD"
        original_prefix = Path(os.environ["TELEOP_ASSESSMENT_PYTHON"]).parent.parent
        original_path = original_prefix / origin_path.relative_to(self.python.parent.parent)
        original_origin = original_path.read_bytes()
        original_inventory = original_path.with_name("RECORD").read_bytes()
        origin = json.loads(origin_path.read_bytes())
        change(origin)
        data = json.dumps(origin).encode()
        # Replace only the private metadata; the original installation stays unchanged.
        replacement = origin_path.with_suffix(".replacement")
        replacement.write_bytes(data)
        replacement.replace(origin_path)
        rows = list(csv.reader(io.StringIO(inventory_path.read_text())))
        member = origin_path.relative_to(distribution.parent).as_posix()
        found = False
        for row in rows:
            if row[0] == member:
                row[1:] = ["sha256=" + base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode().rstrip("="),
                           str(len(data))]
                found = True
        self.assertTrue(found)
        output = io.StringIO()
        csv.writer(output, lineterminator="\n").writerows(rows)
        replacement = inventory_path.with_suffix(".replacement")
        replacement.write_text(output.getvalue())
        replacement.replace(inventory_path)
        self.assertEqual(original_path.read_bytes(), original_origin)
        self.assertEqual(original_path.with_name("RECORD").read_bytes(), original_inventory)

    def test_real_native_editable_metadata_is_rejected_before_model_execution(self):
        self.alter_native_origin(lambda origin: origin.update(dir_info={"editable": True}))
        result, output = self.execute_gate()
        self.assertEqual(result["exit_code"], 78)
        self.assertEqual(output["assessment_error"], "runtime_identity")
        self.assertFalse((self.root / "run.runtime.json").exists())

    def test_real_available_native_commit_cannot_conflict_with_consumer_asset(self):
        self.alter_native_origin(lambda origin: origin.update(vcs_info={"commit_id": "0" * 40}))
        result, output = self.execute_gate()
        self.assertEqual(result["exit_code"], 78)
        self.assertEqual(output["assessment_error"], "runtime_identity")
        self.assertFalse((self.root / "run.runtime.json").exists())

    def test_real_missing_actuator_distribution_is_rejected_before_model_execution(self):
        distribution = self.clone_native_runtime()
        distribution.rename(self.python.parent.parent.parent / "removed-actuator-metadata")
        (distribution.parent / "actuator_model").rename(self.python.parent.parent.parent / "removed-actuator-package")
        result, output = self.execute_gate()
        self.assertEqual(result["exit_code"], 78)
        self.assertEqual(output["assessment_error"], "runtime_identity")
        self.assertFalse((self.root / "run.runtime.json").exists())
