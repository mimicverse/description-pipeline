"""Small JSON/hash helpers shared by the SolidWorks adapter modules.

The canonical encoding, numeric format and hashes are owned by
``description_pipeline.io``; these helpers delegate to it and keep only the
Windows sharing-violation retry that the adapter state files need.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from ...io import (
    digest as _canonical_digest,
    file_digest as _file_digest,
    write_json as _canonical_write,
)

JsonValue = dict | list | str | int | float | bool | None

#: Windows makes an atomic replace or a concurrent read fail transiently when the other side holds
#: the same file open (a sharing violation surfaces as ``PermissionError``).  The job runner and its
#: clients read and write the same record, so those two operations retry briefly instead of reporting
#: an unreadable job.
_SHARING_ATTEMPTS = 5
_SHARING_DELAY_SECONDS = 0.05


def sha256_file(path: Path) -> str:
    """The streamed sha256 of one file (``io.file_digest``)."""

    return _file_digest(path)


def digest_json(payload: object) -> str:
    """The canonical digest of a JSON-compatible payload (``io.digest``)."""

    return _canonical_digest(payload)


def write_json(path: Path, payload: object) -> None:
    """Canonical, atomic JSON with the Windows sharing-violation retry.

    The bytes and the atomic replace come from ``io.write_json``; only the
    transient ``PermissionError`` retry (a reader holding the record open)
    lives here.
    """

    for attempt in range(_SHARING_ATTEMPTS):
        try:
            _canonical_write(path, payload)
            return
        except PermissionError:
            if attempt == _SHARING_ATTEMPTS - 1:
                raise
            time.sleep(_SHARING_DELAY_SECONDS)


def read_json(path: Path) -> JsonValue | None:
    for attempt in range(_SHARING_ATTEMPTS):
        try:
            with open(path, encoding="utf-8") as handle:
                return json.load(handle)
        except PermissionError:
            if attempt == _SHARING_ATTEMPTS - 1:
                raise
            time.sleep(_SHARING_DELAY_SECONDS)
    raise AssertionError("unreachable")  # pragma: no cover - the loop either returns or raises
