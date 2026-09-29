"""Create the offline demo workspace, so the first run needs no CAD, account or network.

The demo exists twice on purpose.  ``examples/demo-arm`` is what a checkout shows a reader, and
``templates/quickstart/demo-arm`` is what an installed wheel and a release bundle carry; without the
packaged copy the documented "try it without CAD" path would start with a git clone.  The two copies
must stay byte-identical, and ``tests/test_quickstart.py`` fails when they drift apart.

Two names lose their leading dot inside the package: setuptools data patterns skip hidden files, so
the copies travel as ``gitignore`` and ``gitattributes`` and get their dot back while the workspace
is written.  That is the same rename ``init_model`` already uses for the model template.
"""

from __future__ import annotations

import shutil
import tempfile
from importlib.resources import as_file, files
from pathlib import Path

from .build import assess, build, freeze, lock_toolchain
from .io import PipelineError

DEMO = "demo-arm"
PACKAGE_DEMO = f"templates/quickstart/{DEMO}"
PROFILE = "kinematics"

#: Names the package cannot carry with their leading dot.
RESTORED_NAMES = {"gitignore": ".gitignore", "gitattributes": ".gitattributes"}


def _write_template(source: Path, target: Path) -> int:
    """Copy the packaged workspace, restoring the names that travel without a dot."""

    written = 0
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        destination = target.joinpath(*(RESTORED_NAMES.get(part, part) for part in relative.parts))
        if path.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, destination)
        written += 1
    if not written:
        # Only reachable from a broken installation, but a silent empty workspace would send the
        # user to `description doctor --root .` with nothing to diagnose.
        raise PipelineError("The packaged demo workspace is empty; reinstall the released package")
    return written


def scaffold(root: Path) -> dict:
    """Write the demo workspace to ``root``, or refuse without touching anything.

    A half-written workspace is worse than none, so the copy lands in a staging directory beside the
    destination and moves into place in one step; an interrupted run leaves no partial model.
    """

    root = Path(root)
    if root.exists() and not root.is_dir():
        raise PipelineError(f"Quickstart destination is a file, not a directory: {root}")
    if root.is_dir() and any(root.iterdir()):
        example = f"{root.name or DEMO}-2"
        raise PipelineError(
            f"Quickstart destination is not empty: {root}. Choose a new directory, for example: "
            f"description quickstart {example}"
        )
    root.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".quickstart-{root.name or DEMO}-", dir=root.parent))
    try:
        with as_file(files("description_pipeline").joinpath(PACKAGE_DEMO)) as source:
            written = _write_template(Path(source), staging / DEMO)
        if root.is_dir():
            root.rmdir()
        (staging / DEMO).rename(root)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return {"root": str(root), "profile": PROFILE, "files": written}


def run(root: Path, profile: str = PROFILE) -> dict:
    """Run the four documented commands on a scaffolded workspace and report the qualification."""

    root = Path(root).resolve()
    lock_toolchain(root)
    freeze(root)
    report = build(root, profile)
    if not report.get("passed"):
        blockers = ", ".join(str(check) for check in report.get("blockers", []))
        raise PipelineError(
            f"Build stopped before the check: {blockers or 'see the diagnostic'}; "
            f"diagnostic: {report.get('diagnostic_path', root)}"
        )
    check = assess(root, profile)
    value = {
        "profile": profile,
        "passed": bool(check["passed"]),
        "qualified_for": check["qualified_for"],
        "blockers": check["blockers"],
        "quality": str(root / "docs/quality.md"),
    }
    if not value["passed"]:
        value["failed_root"] = str(root)
    return value
