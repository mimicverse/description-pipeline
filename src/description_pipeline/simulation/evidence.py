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


def verify_session_finalization(root: Path, manifest: dict) -> dict:
    """Independently measure the exact frozen native workspace, including its manifest."""
    policy = validate_policy(manifest["resource_policy"], root)
    finalization = manifest["finalization"]
    keys = {
        "whole_session",
        "observed_bytes",
        "finalization_reserve_bytes",
        "failure_reserve_bytes",
        "quota_bytes",
        "quota_ok",
    }
    if (
        not isinstance(finalization, dict)
        or not keys <= finalization.keys()
        or finalization["whole_session"] is not True
        or finalization["quota_ok"] is not True
        or type(finalization["observed_bytes"]) is not int
        or finalization["observed_bytes"] < 0
        or any(
            type(finalization[key]) is not int or finalization[key] != policy[key]
            for key in ("finalization_reserve_bytes", "failure_reserve_bytes", "quota_bytes")
        )
    ):
        raise SessionEvidenceError("Native finalization has an invalid complete-session capacity claim")
    actual = check_session_capacity(root, policy)
    if actual != finalization["observed_bytes"]:
        raise SessionEvidenceError("Native finalization bytes differ from the complete immutable session")
    return {"schema_version": "teleop-pico-session-finalization/1", **finalization, "observed_bytes": actual}


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
