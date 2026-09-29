"""Audit a pinned dependency set against the advisory database, on any platform.

    python tools/audit_dependencies.py requirements/linux-py312.lock requirements/win-py312-dev.lock

``pip-audit`` installs the set it audits, so the Windows lock cannot be audited from Linux: ``pywin32``
has no Linux wheel and the install fails before any advisory is checked. The release checklist needs
the audit to be repeatable for every set a release ships, so this asks the same database (OSV) about
the exact pins instead: no installation, no platform, one request per lock file.

Exit codes: 0 every pin is clean, 1 an advisory names a pin, 2 a lock file cannot be read.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

OSV_BATCH = "https://api.osv.dev/v1/querybatch"
PIN = re.compile(r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)==(?P<version>[^\s;]+)")


class AuditError(RuntimeError):
    """A lock file or an answer that cannot be trusted."""


def canonical(name: str) -> str:
    """PyPI compares names case-insensitively with ``-`` and ``_`` equivalent."""

    return re.sub(r"[-_.]+", "-", name).lower()


def parse_lock(path: Path) -> list[tuple[str, str]]:
    """The ``name==version`` pairs of a hash-locked requirements file; anything else is a refusal."""

    if not path.is_file():
        raise AuditError(f"No lock file at {path}")
    pins: list[tuple[str, str]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        found = PIN.match(stripped)
        if not found:
            raise AuditError(f"{path.name} has a line that is not a pinned requirement: {stripped!r}")
        pins.append((canonical(found.group("name")), found.group("version")))
    if not pins:
        raise AuditError(f"{path.name} pins nothing")
    return pins


def queries(pins: list[tuple[str, str]]) -> list[dict]:
    return [{"package": {"name": name, "ecosystem": "PyPI"}, "version": version} for name, version in pins]


def findings(pins: list[tuple[str, str]], answer: dict) -> list[str]:
    """One line per advisory, naming the pin it applies to; the answer has one result per query."""

    results = answer.get("results")
    if not isinstance(results, list) or len(results) != len(pins):
        raise AuditError("the advisory service answered with a different number of results")
    found: list[str] = []
    for (name, version), result in zip(pins, results, strict=True):
        for advisory in result.get("vulns") or []:
            aliases = [alias for alias in advisory.get("aliases") or [] if alias.startswith("CVE-")]
            identifier = aliases[0] if aliases else advisory.get("id", "unknown")
            summary = " ".join((advisory.get("summary") or "").split())
            found.append(f"{name}=={version}: {identifier}{f' — {summary}' if summary else ''}")
    return found


def ask(batch: list[dict]) -> dict:
    request = urllib.request.Request(
        OSV_BATCH,
        data=json.dumps({"queries": batch}).encode(),
        headers={"Content-Type": "application/json", "User-Agent": "description-audit-dependencies"},
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        raise AuditError(f"the advisory service answered HTTP {error.code}") from error
    except urllib.error.URLError as error:
        raise AuditError(f"cannot reach {OSV_BATCH}: {error.reason}") from error


def audit(path: Path) -> list[str]:
    """Every advisory that names a pin of ``path``, in lock-file order."""

    pins = parse_lock(path)
    found = findings(pins, ask(queries(pins)))
    print(f"{path.name}: {len(pins)} pins, {'no known advisories' if not found else f'{len(found)} advisories'}")
    for line in found:
        print(f"  {line}")
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("locks", nargs="+", type=Path, help="hash-locked requirements files to audit")
    args = parser.parse_args(argv)
    total = 0
    try:
        for path in args.locks:
            total += len(audit(path))
    except AuditError as error:
        print(f"refused: {error}", file=sys.stderr)
        return 2
    if total:
        print(f"FAIL: {total} advisories name a pinned dependency")
        return 1
    print("PASS: every pinned dependency is clear of known advisories")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
