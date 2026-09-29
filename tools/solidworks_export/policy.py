"""Path policy helpers (pure, so they can be tested without Windows)."""

from __future__ import annotations

from typing import Iterable


def normalize_path(path: str) -> str:
    return (path or "").replace("/", "\\").strip().lower()


def path_is_allowed(path: str, roots: Iterable[str]) -> bool:
    """True when ``path`` lies under one of the configured roots.

    Empty ``roots`` means "no allowlist configured" and allows everything;
    an allowlist is enforced only when an operator opts in.
    """

    roots = [root for root in (roots or []) if root and root.strip()]
    if not roots:
        return True
    candidate = normalize_path(path)
    if not candidate:
        return False
    for root in roots:
        normalized_root = normalize_path(root).rstrip("\\")
        if not normalized_root:
            continue
        if candidate == normalized_root or candidate.startswith(normalized_root + "\\"):
            return True
    return False
