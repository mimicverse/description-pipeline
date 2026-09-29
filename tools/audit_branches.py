#!/usr/bin/env python3
"""Audit every model branch against the strict URDF contract.

    python tools/audit_branches.py [--mujoco] [--json report.json] [--remote origin] [--ref feature/]

"Which model branches are contract-clean?" used to be answered by hand, one temporary worktree at a
time; when it was finally asked, two branches answered with 50 and 51 errors nobody had seen.  Every
branch that ships ``urdf/robot.urdf`` is checked out detached into a temporary worktree, run through
``tools/audit.py --policy strict`` and removed again.  Branches are audited whole: two branches can
share a URDF byte for byte and still differ in ledger, MJCF or meshes.

Exit codes: 0 every audited branch passed; 1 at least one did not; 2 usage, git or report failure.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
#: Branch prefixes (under the remote-tracking ref) that carry a model, by the repository's own roles.
DEFAULT_PREFIXES = ("feature/", "release/", "work/model/")


def git(repository: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(repository), *arguments], capture_output=True, text=True, encoding="utf-8")


def branch_refs(repository: Path, remote: str, prefixes: tuple[str, ...]) -> list[tuple[str, str]]:
    """(short name, commit) for every remote branch under the prefixes that ships a URDF."""

    listed = git(repository, "for-each-ref", "--format=%(refname) %(objectname)", f"refs/remotes/{remote}")
    if listed.returncode:
        raise RuntimeError(listed.stderr.strip() or f"cannot list refs/remotes/{remote}")
    base = f"refs/remotes/{remote}/"
    entries: list[tuple[str, str]] = []
    for line in listed.stdout.splitlines():
        ref, _, commit = line.partition(" ")
        if not ref.startswith(base):
            continue
        short = ref[len(base) :]
        if not any(short.startswith(prefix) for prefix in prefixes):
            continue
        if git(repository, "cat-file", "-e", f"{commit}:urdf/robot.urdf").returncode == 0:
            entries.append((short, commit))
    return sorted(entries)


def audit_branch(repository: Path, commit: str, *, mujoco: bool, keep: bool) -> dict[str, Any]:
    """One branch, one temporary worktree, one strict audit report."""

    directory = Path(tempfile.mkdtemp(prefix="description-branch-audit-"))
    worktree = directory / "worktree"
    added = git(repository, "worktree", "add", "--detach", "--quiet", str(worktree), commit)
    if added.returncode:
        shutil.rmtree(directory, ignore_errors=True)
        raise RuntimeError(f"cannot check out {commit[:12]}: {added.stderr.strip()}")
    try:
        command = [
            sys.executable,
            str(ROOT / "tools" / "audit.py"),
            "--root",
            str(worktree),
            "--policy",
            "strict",
            "--json",
        ]
        if mujoco:
            command.append("--mujoco")
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8")
        try:
            report = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise RuntimeError(f"audit of {commit[:12]} did not report JSON: {error}") from error
        summary = report.get("summary", {})
        counts = Counter(finding["code"] for finding in report.get("findings", []))
        return {
            "commit": commit,
            "passed": bool(report.get("passed")),
            "error": int(summary.get("error", 0)),
            "warning": int(summary.get("warning", 0)),
            "info": int(summary.get("info", 0)),
            "waived": int(summary.get("waived", 0)),
            "codes": dict(sorted(counts.items())),
        }
    finally:
        if keep:
            print(f"  kept worktree: {worktree}")
        else:
            git(repository, "worktree", "remove", "--force", str(worktree))
            shutil.rmtree(directory, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repository", type=Path, default=ROOT, help="checkout whose remote holds the branches")
    parser.add_argument("--remote", default="origin", help="remote name to fetch and read")
    parser.add_argument(
        "--ref",
        action="append",
        default=None,
        help="branch prefix to audit, repeatable (default: feature/, release/, work/model/)",
    )
    parser.add_argument("--mujoco", action="store_true", help="also run the compiled layer")
    parser.add_argument("--json", type=Path, help="write the full report here")
    parser.add_argument("--keep-worktrees", action="store_true", help="leave the temporary worktrees behind")
    args = parser.parse_args(argv)

    repository = args.repository.resolve()
    prefixes = tuple(args.ref) if args.ref else DEFAULT_PREFIXES
    if git(repository, "rev-parse", "--git-dir").returncode:
        print(f"Not a git checkout: {repository}", file=sys.stderr)
        return 2
    fetched = git(repository, "fetch", "--quiet", args.remote)
    if fetched.returncode:
        print(f"Cannot fetch {args.remote}: {fetched.stderr.strip()}", file=sys.stderr)
        return 2
    try:
        branches = branch_refs(repository, args.remote, prefixes)
        results: list[dict[str, Any]] = []
        for short, commit in branches:
            outcome = audit_branch(repository, commit, mujoco=args.mujoco, keep=args.keep_worktrees)
            outcome["branch"] = short
            results.append(outcome)
            print(
                f"{short:<52} {commit[:12]}  {outcome['error']:>3} error {outcome['warning']:>3} warning"
                f"  {'ok' if outcome['passed'] else 'FAIL'}"
            )
    except RuntimeError as error:
        print(str(error), file=sys.stderr)
        return 2
    if not results:
        print("No model branches with urdf/robot.urdf under " + ", ".join(prefixes))
        return 0
    passed = all(item["passed"] for item in results)
    print(("PASS: " if passed else "FAIL: ") + f"{len(results)} branch(es) audited against the strict contract")
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps({"passed": passed, "branches": results}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"report: {args.json}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
