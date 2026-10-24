"""Synthetic authoritative Linux freeze events for Windows endpoint tests."""

from __future__ import annotations

import copy

FREEZE_RECEIPT_SHA256 = "a" * 64


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
