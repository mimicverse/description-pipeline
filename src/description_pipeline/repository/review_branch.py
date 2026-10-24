"""Canonical review-branch naming rules shared by Publish boundaries."""

from __future__ import annotations

import re

from ..io import digest

REVIEW_BRANCH = "work/description/{hardware}/{request}"


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


def review_branch(hardware: str, run_id: str, request_id: str) -> str:
    """Return the immutable review branch for one admitted request."""

    if not isinstance(run_id, str) or not run_id:
        raise ReviewBranchError("run_id_invalid", run_id)
    if not isinstance(request_id, str) or not request_id:
        raise ReviewBranchError("request_id_invalid", request_id)
    request_digest = digest(
        {"run_id": run_id, "request_id": request_id}
    )[:24]
    return REVIEW_BRANCH.format(
        hardware=_slug_hardware(hardware),
        request=request_digest,
    )


def is_review_branch(value: object) -> bool:
    """Whether ``value`` is the canonical review branch of a declared purpose."""

    if not isinstance(value, str):
        return False
    parts = value.split("/")
    if len(parts) != 4 or parts[:2] != ["work", "description"]:
        return False
    request_digest = parts[3]
    if (
        len(request_digest) != 24
        or any(character not in "0123456789abcdef" for character in request_digest)
    ):
        return False
    try:
        return _slug_hardware(parts[2]) == parts[2]
    except ReviewBranchError:
        return False


__all__ = [
    "REVIEW_BRANCH",
    "ReviewBranchError",
    "is_review_branch",
    "review_branch",
]
