"""Identity of the captured Onshape fixture, read from the fixture itself.

``tests/fixtures/**`` is the only place allowed to carry the real document identity: an sdist ships
``tests/*.py`` without the fixtures, so a hard-coded id in a test file is published by
``pip download --no-binary``.  Anything that needs the identity asks here, and
``tests/test_release_identity.py`` fails when one shows up in a shippable file again.
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "onshape"
CACHE = FIXTURES / "cache"
SOURCE_PATH = CACHE / "source.json"
#: The captured Onshape export is 8 MB of real CAD data. It stays out of the sdist and out of a
#: fixtures-free publication, so the suite has to load without it and skip what cannot run.
AVAILABLE = SOURCE_PATH.is_file()
REASON = "the captured Onshape fixture (tests/fixtures/onshape) is not in this checkout"
requires_fixture = unittest.skipUnless(AVAILABLE, REASON)
SOURCE = json.loads(SOURCE_PATH.read_text(encoding="utf-8")) if AVAILABLE else {}
URL = SOURCE.get("url", "")
ELEMENT = SOURCE.get("element_id", "")

HEX24 = re.compile(r"[0-9a-f]{24}")


def _cached(kind: str) -> list[tuple[str, dict]]:
    found = []
    for path in sorted((CACHE / "json").glob(f"{kind}_*.json")):
        identifier = HEX24.search(path.stem)
        if identifier is not None:
            found.append((identifier.group(0), json.loads(path.read_text(encoding="utf-8"))))
    return found


def mass_properties_ids(part: str) -> tuple[str, str]:
    """``(the part studio that carries ``part``, the other studio the capture recorded)``."""

    if not AVAILABLE:
        raise RuntimeError(REASON)
    entries = _cached("mass_properties")
    populated = [identifier for identifier, payload in entries if part in (payload.get("bodies") or {})]
    other = [identifier for identifier, payload in entries if part not in (payload.get("bodies") or {})]
    if len(populated) != 1 or len(other) != 1:
        raise RuntimeError(f"the fixture should record one studio with {part} and one without: {populated} {other}")
    return populated[0], other[0]
