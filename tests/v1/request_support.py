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
    from description_pipeline.request_evidence import build_request_evidence, request_evidence_bytes

    # The frozen original is a strict seven-field request record, formed against
    # this attempt's admitted upload so the discovery gate's binding checks hold.
    meta = store.meta(run_id) or {}
    payload = request_evidence_bytes(
        build_request_evidence(
            request_id="fixture-frozen-request",
            target_hardware=str(meta.get("hardware_id") or "nd_fixture"),
            change_summary="fixture frozen original",
            main_assembly=str(meta.get("main_assembly") or "cad/robot.SLDASM"),
            handoff_digest=str(meta.get("handoff_sha256") or "0" * 64),
            platform_config_digest="c" * 64,
        )
    )
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
