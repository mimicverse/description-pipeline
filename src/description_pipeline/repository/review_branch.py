"""Canonical review-branch naming rules shared by Publish boundaries."""

from __future__ import annotations

import re

from ..interface_records import REQUEST_PURPOSES

REVIEW_BRANCH = "work/solidworks/{hardware}/{purpose}"


class ReviewBranchError(ValueError):
    def __init__(self, code: str, detail: object | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


def _slug_hardware(value: str) -> str:
    slug = re.sub(r"[^a-z0-9_.-]", "-", value.strip().lower())
    if not slug or slug.startswith("-"):
        raise ReviewBranchError("hardware_id_invalid", value)
    return slug


def review_branch(hardware: str, purpose: str) -> str:
    """Return the one managed review branch for a hardware and declared purpose."""

    if purpose not in REQUEST_PURPOSES:
        raise ReviewBranchError("purpose_not_supported", {"hardware": hardware, "purpose": purpose})
    return REVIEW_BRANCH.format(hardware=_slug_hardware(hardware), purpose=purpose)


def is_review_branch(value: object) -> bool:
    """Whether ``value`` is the canonical review branch of a declared purpose."""

    if not isinstance(value, str):
        return False
    parts = value.split("/")
    if len(parts) != 4 or parts[:2] != ["work", "solidworks"]:
        return False
    try:
        return review_branch(parts[2], parts[3]) == value
    except ReviewBranchError:
        return False


__all__ = [
    "REVIEW_BRANCH",
    "ReviewBranchError",
    "is_review_branch",
    "review_branch",
]
