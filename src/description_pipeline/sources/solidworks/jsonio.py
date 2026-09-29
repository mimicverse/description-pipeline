"""Small JSON/hash helpers shared by the SolidWorks adapter modules.

Deliberately local: the *snapshot format* is owned by
``description_pipeline.sources.snapshot``; these helpers only keep the adapter
from re-implementing the same four lines in every module.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

JsonValue = dict | list | str | int | float | bool | None

#: Windows makes an atomic replace or a concurrent read fail transiently when the other side holds
#: the same file open (a sharing violation surfaces as ``PermissionError``).  The job runner and its
#: clients read and write the same record, so those two operations retry briefly instead of reporting
#: an unreadable job.
_SHARING_ATTEMPTS = 5
_SHARING_DELAY_SECONDS = 0.05


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest()


def digest_json(payload: object) -> str:
    """Stable digest of a JSON-compatible payload (sorted keys, no spaces)."""

    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def write_json(path: Path, payload: object) -> None:
    """Write JSON atomically: a reader never sees a half-written file.

    The watchdog and the job runner both persist state, so a plain ``open(w)``
    would let a reader parse a truncated document.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text + "\n", encoding="utf-8", newline="\n")
    for attempt in range(_SHARING_ATTEMPTS):
        try:
            os.replace(temporary, path)
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
