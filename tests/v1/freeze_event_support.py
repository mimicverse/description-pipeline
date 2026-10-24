"""Synthetic authoritative Linux freeze events for Windows endpoint tests."""

from __future__ import annotations

import copy

from description_pipeline.io import digest
from description_pipeline.stages import validate_freeze_events

FREEZE_RECEIPT_SHA256 = "a" * 64


def freeze_request_fields(source: dict | None = None) -> dict:
    events = validate_freeze_events(source["freeze_events"] if source is not None else freeze_events())
    return {
        "freeze_events": copy.deepcopy(events),
        "freeze_events_sha256": digest(events),
        "freeze_receipt_sha256": source["freeze_receipt_sha256"] if source is not None else FREEZE_RECEIPT_SHA256,
    }


def freeze_events() -> list[dict]:
    return copy.deepcopy(
        [
            {
                "at": "2026-10-24T00:00:00.000000Z",
                "stage": "freeze",
                "state": "running",
            },
            {
                "at": "2026-10-24T00:00:00.100000Z",
                "stage": "freeze",
                "state": "running",
                "check": {
                    "id": "handoff.admission",
                    "boundary": "input",
                    "state": "passed",
                    "details": {},
                },
            },
            {
                "at": "2026-10-24T00:00:00.200000Z",
                "stage": "freeze",
                "state": "running",
                "check": {
                    "id": "handoff.integrity",
                    "boundary": "output",
                    "state": "passed",
                    "details": {},
                },
            },
            {
                "at": "2026-10-24T00:00:00.300000Z",
                "stage": "freeze",
                "state": "completed",
            },
        ]
    )
