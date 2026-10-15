"""Unit-fixture support: the registered frozen original pair for LinuxStore fixtures.

The final consumption chain has no file fallback: the registered freeze record
is the authority and both mutable copies must equal its bytes.  Unit fixtures
pin a synthetic pair with a matching platform stub; the authority
counterexamples live in the dedicated probes and tests.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest import mock


def install_request_authority(testcase, store, run_id: str) -> None:
    """Give one LinuxStore fixture its frozen original pair and the platform stub."""
    from description_pipeline.io import file_digest
    from description_pipeline.orchestration import linux_runner

    payload = b'{"fixture": "registered frozen request original"}\n'
    frozen = store.run_dir(run_id) / "input" / "request.json"
    frozen.parent.mkdir(parents=True, exist_ok=True)
    frozen.write_bytes(payload)
    carried = store.capture_dir(run_id) / "input" / "request.json"
    carried.parent.mkdir(parents=True, exist_ok=True)
    carried.write_bytes(payload)
    record = {"path": "input/request.json", "bytes": len(payload), "sha256": file_digest(frozen)}
    platform = SimpleNamespace(
        confirmed_stage=lambda run, stage: {"artifacts": [record]} if (run, stage) == (run_id, "freeze") else None
    )
    patcher = mock.patch.object(linux_runner, "_capture_platform", return_value=platform)
    patcher.start()
    testcase.addCleanup(patcher.stop)
