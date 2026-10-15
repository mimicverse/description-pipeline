#!/usr/bin/env python3
"""Check one release directory against the six-asset contract (existence + digests).

The release boundary has a single manifest: ``release.json`` binds the five canonical assets
(wheel, sdist, deployment zip, linux role zip, windows role zip) by digest, and the six assets
are exactly those five plus ``release.json`` itself.  This CLI reuses the canonical verifier
(``tools.verify_distribution.verify_release``) so the gate has one implementation owner.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.verify_distribution import verify_release  # noqa: E402


def check(directory: Path) -> dict:
    directory = Path(directory)
    release_path = directory / "release.json"
    if not release_path.is_file():
        raise SystemExit(f"release.json is missing from {directory}")
    release = json.loads(release_path.read_text(encoding="utf-8"))
    if not isinstance(release, dict):
        raise SystemExit("release.json is not an object")
    identity = {key: value for key, value in release.items() if key != "artifacts"}
    verify_release(directory, identity)
    artifacts = release.get("artifacts")
    return {
        "passed": True,
        "assets": len(artifacts) + 1,
        "artifacts": sorted(artifacts),
        "release_json_sha256": hashlib.sha256(release_path.read_bytes()).hexdigest(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("directory", type=Path, help="the built release directory")
    args = parser.parse_args()
    result = check(args.directory)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
