"""Real Linux child ownership and bounded unresolved termination."""

from __future__ import annotations

import secrets
import subprocess
import sys
import time
from pathlib import Path

import pytest

from description_pipeline.dynamics_assessment.contracts import AssessmentConflict
from description_pipeline.dynamics_assessment.runtime import (
    ProcessLimits,
    TeleopProcess,
    process_identity,
    process_state,
    terminate_owned,
)
from tests.v1.test_dynamics_assessment_contracts import runtime_pin


def test_foreign_or_unbound_identity_cannot_signal_a_live_child():
    nonce = secrets.token_hex(32)
    process = subprocess.Popen(
        [sys.executable, "-I", "-c", "import time; time.sleep(30)", nonce],
        start_new_session=True,
    )
    identity = process_identity(process.pid, nonce)
    try:
        foreign = {**identity, "host": "not-the-owning-host"}
        assert process_state(foreign) == "foreign"
        assert not terminate_owned(foreign, grace_seconds=0.1)
        assert not terminate_owned({**identity, "nonce": "another-child"}, grace_seconds=0.1)
        assert terminate_owned({**identity, "start_ticks": "0"}, grace_seconds=0.1)
        assert process.poll() is None
        assert terminate_owned(identity, grace_seconds=0.1)
        assert process_state(identity) == "dead"
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)


def test_unsettled_termination_returns_conflict_without_releasing_identity(tmp_path):
    process = TeleopProcess(Path(sys.executable), runtime_pin(), ProcessLimits(30, 1024**3, 10, 1024**2))
    registered = []

    def checkpoint(identity):
        registered.append(dict(identity))
        # Simulate an ownership observation that cannot be settled on this host.
        # The real gated child must not be signalled through this foreign identity.
        identity["host"] = "unavailable-resource-host"

    started = time.monotonic()
    with pytest.raises(AssessmentConflict, match="termination could not be settled"):
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
