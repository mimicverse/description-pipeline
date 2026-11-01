"""Observe private workspace bytes using the frozen native session policy."""

from __future__ import annotations

import os
import math
import json
from pathlib import Path
import shutil
import stat
import asyncio
import fcntl

from .store import SimulationConflict

SESSION_QUOTA_BYTES = 512 * 1024 * 1024
EVIDENCE_REVISION = "w4-public-2"
PHASES = ("preparing", "ready_unpaired", "running", "stop_finalization", "finalization")


class SessionResourceError(SimulationConflict):
    """Preserve original session evidence when resource admission cannot succeed."""


class StorageGate:
    """One allocation lock for this root, shared by local tasks and managed CLI processes."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self._local = asyncio.Lock()
        self._descriptor = None

    def locked(self):
        return self._local.locked()

    async def __aenter__(self):
        await self._local.acquire()
        descriptor = None
        try:
            if self.root.is_symlink() or not self.root.is_dir():
                raise SessionResourceError("Allocation root must be a real directory")
            descriptor = os.open(self.root / "allocation.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise SessionResourceError("Allocation lock must be a regular file")
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    await asyncio.sleep(0.02)
            self._descriptor = descriptor
            return self
        except BaseException:
            if descriptor is not None:
                os.close(descriptor)
            self._local.release()
            raise

    async def __aexit__(self, *_error):
        os.close(self._descriptor)
        self._descriptor = None
        self._local.release()


def verification_partial(root: Path, command_id: str, request: dict) -> Path:
    name = "recording-verification-" + command_id
    if request.get("partial") != name or Path(name).name != name:
        raise SessionResourceError("Recording verification partial identity is invalid")
    path = root / name
    if path.is_symlink():
        raise SessionResourceError("Recording verification partial must not be a symlink")
    return path


def admit_storage(
    store, root: Path, destination: Path, additional_bytes: int, excluded_session=None, *, excluded_verification=None
):
    """Derive remaining workspace capacity from the existing durable session and attempt facts."""
    workspaces = []
    for identifier in store.active_session_ids():
        if identifier == excluded_session:
            continue
        if (
            not isinstance(identifier, str)
            or not identifier
            or Path(identifier).name != identifier
            or identifier in {".", ".."}
        ):
            raise SessionResourceError("Active session identity is not a confined workspace name")
        workspaces.append(root / identifier)
    for attempt in store.verification_attempts():
        if attempt["command_id"] == excluded_verification or attempt["result"]["state"] in {
            "completed",
            "failed",
            "cancelled",
        }:
            continue
        workspaces.append(verification_partial(root, attempt["command_id"], attempt["request"]))
    remaining = 0
    for path in workspaces:
        observed = directory_bytes(path) if path.exists() or path.is_symlink() else 0
        if observed > SESSION_QUOTA_BYTES:
            raise SessionResourceError("An active workspace already exceeds its complete budget")
        remaining += SESSION_QUOTA_BYTES - observed
    if destination.stat().st_dev == root.stat().st_dev:
        require_storage(destination, additional_bytes=additional_bytes + remaining, reserve_bytes=0)
    else:
        require_storage(destination, additional_bytes=additional_bytes, reserve_bytes=0)
        require_storage(root, additional_bytes=remaining, reserve_bytes=0)


def directory_bytes(root: Path) -> int:
    """Count all logical file bytes; links and nonregular files are observation failures."""
    if root.is_symlink() or not root.is_dir():
        raise SessionResourceError("Session workspace must be a real directory")
    pending, total = [root], 0
    while pending:
        with os.scandir(pending.pop()) as entries:
            for entry in entries:
                info = entry.stat(follow_symlinks=False)
                mode = info.st_mode
                if stat.S_ISDIR(mode):
                    pending.append(Path(entry.path))
                elif stat.S_ISREG(mode):
                    total += info.st_size
                else:
                    raise SessionResourceError("Session workspace contains a link or nonregular file")
    return total


def check_session_capacity(root: Path, policy: dict) -> int:
    """Apply the contract's complete-directory plus shared-reserve capacity rule."""
    quota = policy["quota_bytes"]
    finalization = policy["finalization_reserve_bytes"]
    failure = policy["failure_reserve_bytes"]
    if (
        type(quota) is not int
        or quota != SESSION_QUOTA_BYTES
        or any(type(value) is not int or value < 0 for value in (finalization, failure))
    ):
        raise SessionResourceError("Session resource policy has an invalid quota or reserve")
    observed = directory_bytes(root)
    if observed + finalization + failure > quota:
        raise SessionResourceError("Simulation exceeded its complete session workspace budget")
    return observed


def require_storage(root: Path, *, additional_bytes: int, reserve_bytes: int) -> None:
    """Admit this copy against its actual filesystem; retained input is already allocated."""
    if any(type(value) is not int or value < 0 for value in (additional_bytes, reserve_bytes)):
        raise SessionResourceError("Storage admission requires nonnegative byte counts")
    if shutil.disk_usage(root).free < additional_bytes + reserve_bytes:
        raise SessionResourceError("Storage is insufficient for the retained session and pending copy")


def _integer(value, *, minimum=0):
    return type(value) is int and value >= minimum


def _number(value, *, minimum=0):
    return type(value) in {int, float} and math.isfinite(value) and value >= minimum


def validate_policy(policy: dict, root: Path) -> dict:
    """Validate the public projection's types without deriving native defaults or caps."""
    integers = {
        "quota_bytes",
        "maximum_control_ticks",
        "raw_row_cap",
        "event_row_cap",
        "record_row_cap",
        "setup_tick_cap",
        "max_row_bytes",
        "finalization_reserve_bytes",
        "failure_reserve_bytes",
        "setup_reserve_bytes",
        "source_bytes",
        "compiled_bytes",
        "stderr_request_session_bytes",
        "fixed_bytes",
        "evidence_byte_cap",
        "declared_source_bytes",
        "source_materialization_limit_bytes",
    }
    numbers = {
        "minimum_duration_s",
        "control_period_s",
        "raw_rows_per_second",
        "event_rows_per_second",
        "traffic_headroom",
    }
    if (
        not isinstance(policy, dict)
        or not integers | numbers | {"session_dir", "formula"} <= policy.keys()
        or any(not _integer(policy[key]) for key in integers)
        or any(not _number(policy[key]) or policy[key] <= 0 for key in numbers)
        or any(
            policy[key] == 0
            for key in (
                "maximum_control_ticks",
                "raw_row_cap",
                "event_row_cap",
                "record_row_cap",
                "max_row_bytes",
                "evidence_byte_cap",
            )
        )
        or policy["quota_bytes"] != SESSION_QUOTA_BYTES
        or policy["traffic_headroom"] < 1
        or not isinstance(policy["session_dir"], str)
        or not Path(policy["session_dir"]).is_absolute()
        or Path(policy["session_dir"]) != root
        or policy["source_bytes"] != policy["declared_source_bytes"]
        or policy["declared_source_bytes"] > policy["source_materialization_limit_bytes"]
        or policy["formula"] != "evidence_byte_cap = quota_bytes - fixed_bytes"
    ):
        raise SessionResourceError("Native session resource projection is invalid")
    return policy


def _validate_phase(phase):
    if (
        not isinstance(phase, dict)
        or not {"name", "start_tick", "start_monotonic_ns", "counts"} <= phase.keys()
        or phase["name"] not in PHASES
        or not _integer(phase["start_tick"])
        or not _integer(phase["start_monotonic_ns"])
        or not isinstance(phase["counts"], dict)
        or set(phase["counts"]) != {"raw_input", "session_events", "records"}
        or any(not _integer(value) for value in phase["counts"].values())
    ):
        raise SessionResourceError("Native session phase projection is invalid")


def validate_ready(evidence: dict, policy: dict, root: Path) -> dict:
    validate_policy(policy, root)
    if isinstance(evidence, dict) and "resource_policy" in evidence:
        validate_policy(evidence["resource_policy"], root)
    if (
        not isinstance(evidence, dict)
        or not {"contract_revision", "phase", "resource_policy", "timing_contract", "files"} <= evidence.keys()
        or evidence["contract_revision"] != EVIDENCE_REVISION
        or evidence["resource_policy"] != policy
    ):
        raise SessionResourceError("Native ready projection differs from the frozen resource admission")
    _validate_phase(evidence["phase"])
    if evidence["phase"]["name"] != "ready_unpaired" or evidence["phase"]["start_tick"] != 0:
        raise SessionResourceError("Native ready projection has an invalid initial phase")
    timing = evidence["timing_contract"]
    required = {
        "clock": "time.monotonic_ns",
        "minimum_duration_s": policy["minimum_duration_s"],
        "minimum_duration_ns": int(policy["minimum_duration_s"] * 1_000_000_000),
        "control_period_s": policy["control_period_s"],
        "running_start": "first validated real frame applied at an actual control tick after ready",
        "running_end": (
            "first actual control tick at or after start + minimum_duration_ns at a requested normal stop boundary"
        ),
        "record_rule": (
            "one canonical record for every actual control tick from tick zero through the terminal control boundary"
        ),
        "stop_and_finalization_excluded_from_running_duration": True,
    }
    if (
        not isinstance(timing, dict)
        or any(
            key not in timing or (type(timing[key]) is bool and type(value) is not bool) or timing[key] != value
            for key, value in required.items()
        )
        or type(timing["minimum_duration_ns"]) is not int
        or timing["stop_and_finalization_excluded_from_running_duration"] is not True
    ):
        raise SessionResourceError("Native timing projection differs from the frozen running semantics")
    files = {
        key: {"name": name, "encoding": "gzip-jsonl", "compresslevel": 6, "mtime": 0}
        for key, name in (
            ("raw", "raw-input.jsonl.gz"),
            ("events", "session-events.jsonl.gz"),
            ("records", "records.jsonl.gz"),
        )
    }
    files["manifest"] = {"name": "session-manifest.json", "encoding": "json", "commit": "fsync-then-atomic-last"}
    if json.dumps(evidence["files"], sort_keys=True) != json.dumps(files, sort_keys=True):
        raise SessionResourceError("Native evidence files differ from the frozen streaming format")
    return evidence


def validate_state(evidence: dict, ready: dict, *, tick: int, previous: dict | None) -> dict:
    if (
        not isinstance(evidence, dict)
        or not {"contract_revision", "phase", "run_window"} <= evidence.keys()
        or evidence["contract_revision"] != EVIDENCE_REVISION
        or not _integer(tick)
    ):
        raise SessionResourceError("Native state evidence projection is invalid")
    _validate_phase(evidence["phase"])
    window, timing = evidence["run_window"], ready["timing_contract"]
    required = {
        "minimum_duration_s",
        "minimum_duration_ns",
        "start_tick",
        "start_monotonic_ns",
        "start_command_id",
        "end_tick",
        "end_monotonic_ns",
        "elapsed_monotonic_ns",
        "elapsed_monotonic_s",
        "minimum_duration_met",
    }
    if (
        not isinstance(window, dict)
        or not required <= window.keys()
        or not _number(window["minimum_duration_s"])
        or not _integer(window["minimum_duration_ns"])
        or any(window[key] != timing[key] for key in ("minimum_duration_s", "minimum_duration_ns"))
        or window["end_tick"] is not None
        or window["end_monotonic_ns"] is not None
        or type(window["minimum_duration_met"]) is not bool
    ):
        raise SessionResourceError("Native live running window is invalid")
    starts = ("start_tick", "start_monotonic_ns", "start_command_id")
    if window["start_tick"] is None:
        if (
            any(window[key] is not None for key in (*starts, "elapsed_monotonic_ns", "elapsed_monotonic_s"))
            or window["minimum_duration_met"]
            or evidence["phase"]["name"] == "running"
        ):
            raise SessionResourceError("Native pre-running projection created an unproven running clock")
    elif (
        not _integer(window["start_tick"])
        or window["start_tick"] > tick
        or not _integer(window["start_monotonic_ns"])
        or not isinstance(window["start_command_id"], str)
        or not window["start_command_id"]
        or not _integer(window["elapsed_monotonic_ns"])
        or not _number(window["elapsed_monotonic_s"])
        or window["elapsed_monotonic_s"] != window["elapsed_monotonic_ns"] / 1_000_000_000
        or window["minimum_duration_met"] != (window["elapsed_monotonic_ns"] >= timing["minimum_duration_ns"])
        or evidence["phase"]["name"] == "ready_unpaired"
    ):
        raise SessionResourceError("Native running progress is inconsistent")
    if previous is not None:
        old = previous["run_window"]
        if PHASES.index(evidence["phase"]["name"]) < PHASES.index(previous["phase"]["name"]) or (
            old["start_tick"] is not None
            and (
                any(window[key] != old[key] for key in starts)
                or window["elapsed_monotonic_ns"] is None
                or window["elapsed_monotonic_ns"] < old["elapsed_monotonic_ns"]
            )
        ):
            raise SessionResourceError("Native running phase or clock origin regressed")
    return evidence
