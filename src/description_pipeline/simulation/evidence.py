"""Parent container identities and independent whole-session finalization checks."""

from __future__ import annotations

from pathlib import Path

from .resources import PHASES, SessionResourceError, check_session_capacity, validate_policy


class SessionEvidenceError(SessionResourceError):
    """Retain an immutable diagnostic snapshot when complete evidence is unproven."""


class RecordingDiagnostic(SessionEvidenceError):
    def __init__(self, message: str, *, recording=None):
        super().__init__(message)
        self.recording = recording


def validate_finalization_proof(root: Path, manifest: dict, native: dict) -> dict:
    """Validate a sealed public proof without reopening its historical session path."""
    policy = validate_policy(manifest["resource_policy"], root)
    finalization = manifest["finalization"]
    keys = {
        "schema_version",
        "pass",
        "session_dir",
        "observed_bytes",
        "manifest_observed_bytes",
        "reserve_bytes",
        "quota_bytes",
        "self_bytes_match",
        "quota_ok",
        "finalization",
        "detail",
    }
    if (
        not isinstance(native, dict)
        or native.keys() != keys
        or native["schema_version"] != "teleop-pico-session-finalization-verification/1"
        or any(native[key] is not True for key in ("pass", "self_bytes_match", "quota_ok"))
        or native["detail"] is not None
        or native["session_dir"] != str(root)
        or native["finalization"] != finalization
        or not isinstance(finalization, dict)
        or finalization.get("whole_session") is not True
        or finalization.get("quota_ok") is not True
        or any(
            type(native[key]) is not int or native[key] < 0
            for key in ("observed_bytes", "manifest_observed_bytes", "reserve_bytes", "quota_bytes")
        )
        or type(finalization.get("observed_bytes")) is not int
        or native["manifest_observed_bytes"] != finalization["observed_bytes"]
        or native["observed_bytes"] != native["manifest_observed_bytes"]
        or native["quota_bytes"] != policy["quota_bytes"]
        or native["reserve_bytes"] != policy["finalization_reserve_bytes"] + policy["failure_reserve_bytes"]
        or any(
            type(finalization.get(key)) is not int or finalization[key] != policy[key]
            for key in ("finalization_reserve_bytes", "failure_reserve_bytes", "quota_bytes")
        )
    ):
        raise SessionEvidenceError("Public native finalization proof differs from the frozen session")
    return native


def verify_finalization_observation(root: Path, manifest: dict, native: dict) -> dict:
    """Bind the public finalization proof to the parent's exact immutable workspace."""
    validate_finalization_proof(root, manifest, native)
    policy = manifest["resource_policy"]
    actual = check_session_capacity(root, policy)
    if actual != native["observed_bytes"] or actual != native["manifest_observed_bytes"]:
        raise SessionEvidenceError("Native finalization bytes differ from the complete immutable session")
    return native


def verify_manifest_counts(manifest: dict, observed: dict) -> None:
    """Compare native phase totals with the public business-stream observations."""
    keys = ("raw_input", "session_events", "records")
    counts, timeline, policy = manifest["counts"], manifest["phase_timeline"], manifest["resource_policy"]
    if (
        not isinstance(counts, dict)
        or any(type(counts[key]) is not int or counts[key] < 0 or counts[key] != observed[key] for key in keys)
        or not isinstance(timeline, dict)
        or set(timeline) != set(PHASES)
        or any(
            counts[key] > policy[cap]
            for key, cap in (
                ("raw_input", "raw_row_cap"),
                ("session_events", "event_row_cap"),
                ("records", "record_row_cap"),
            )
        )
    ):
        raise SessionEvidenceError("Native manifest counts differ from the frozen stream observations or caps")
    totals = dict.fromkeys(keys, 0)
    for name in PHASES:
        phase = timeline[name]
        if (
            not isinstance(phase, dict)
            or not {"start_monotonic_ns", "end_monotonic_ns", "start_tick", "end_tick", "counts"} <= phase.keys()
            or not isinstance(phase["counts"], dict)
            or set(phase["counts"]) != set(keys)
            or any(type(value) is not int or value < 0 for value in phase["counts"].values())
            or any(
                phase[key] is not None and type(phase[key]) is not int
                for key in ("start_monotonic_ns", "end_monotonic_ns", "start_tick", "end_tick")
            )
        ):
            raise SessionEvidenceError("Native phase timeline is invalid")
        if phase["start_monotonic_ns"] is None:
            if any(phase[key] is not None for key in ("end_monotonic_ns", "start_tick", "end_tick")) or any(
                phase["counts"].values()
            ):
                raise SessionEvidenceError("An unentered native phase contains activity")
        elif (
            phase["start_tick"] is None
            or (name == "finalization" and (phase["end_monotonic_ns"] is not None or phase["end_tick"] is not None))
            or (
                name != "finalization"
                and (
                    phase["end_monotonic_ns"] is None
                    or phase["end_tick"] is None
                    or phase["end_monotonic_ns"] < phase["start_monotonic_ns"]
                    or phase["end_tick"] < phase["start_tick"]
                )
            )
        ):
            raise SessionEvidenceError("Native phase start or terminal boundary is invalid")
        for key in keys:
            totals[key] += phase["counts"][key]
    if any(totals[key] != counts[key] for key in keys):
        raise SessionEvidenceError("Native phase counts do not cover the complete evidence streams")
