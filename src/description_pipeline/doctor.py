"""One command that answers "is this installation and workspace usable?".

Every check reports what was found, whether it matters, and the command that fixes it.  Failures are
conditions that would stop the next build or check; warnings are conditions a workflow may or may not
need (Git LFS for model assets, an authenticated ``gh`` for submission, the MuJoCo extra that
``build`` and ``check`` compile the consumer scene with).
"""

from __future__ import annotations

import importlib
import importlib.metadata
import os
import platform
import shutil
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path

from . import __version__
from .build import tool_identity
from .io import PipelineError, read_data
from .repository import check_layout
from .repository.tunnel import worker_health

OK = "ok"
WARN = "warn"
FAIL = "fail"

#: distribution name -> module that a full installation must import.
REQUIRED = {"numpy": "numpy", "PyYAML": "yaml", "jsonschema": "jsonschema", "packaging": "packaging"}
#: distribution name -> module a specific purpose needs; missing them is a warning, but the hint has
#: to say what still works and what does not: ``check`` compiles the consumer scene with MuJoCo on
#: every profile, kinematics included.
OPTIONAL = {"mujoco": "mujoco"}
if platform.system() == "Windows":
    OPTIONAL["pywin32"] = "win32com"

TOOLCHAIN_LOCK = "config/toolchain.lock.json"
SOURCE_LOCK = "sources/source.lock.json"

#: Windows refuses paths past this unless long paths are enabled, and a build writes paths well below
#: the model root: ``sources/snapshots/<64 hex>/…`` plus the generated consumer entries.  The margin
#: is a lower bound — the frozen source mirrors the CAD tree — and it is the number to quote when the
#: workspace is already close to the limit.
MAX_PATH = 260
PIPELINE_PATH_MARGIN = 90
#: A build writes the frozen snapshot, a staging copy of the whole delivery and the diagnostic root
#: beside the workspace; the regression suite writes a few hundred megabytes more, and RELEASING.md
#: asks for 2 GB before running it.  Below this the first build is the thing that fails.
MINIMUM_FREE_BYTES = 2 * 1024**3
WINDOWS_LONG_PATH_ADVICE = (
    "Windows cannot reach this path (MAX_PATH, 260 characters; long paths are off by default). "
    "Use a shorter model root, or enable long paths: set HKLM\\SYSTEM\\CurrentControlSet\\Control"
    "\\FileSystem\\LongPathsEnabled to 1 (and `git config --global core.longpaths true`), then open a "
    "new shell."
)

#: name -> (executable, arguments) probed for a version string.
EXTERNAL = {
    "git": ("git", ("--version",)),
    "git-lfs": ("git-lfs", ("version",)),
    "gh": ("gh", ("--version",)),
}


def _version(distribution: str) -> str | None:
    """Import the module and report the installed distribution version.

    The version comes from the installed metadata rather than ``module.__version__``: some packages
    warn (jsonschema does) when that attribute is touched, and a doctor that prints a warning on a
    healthy installation is worse than useless.
    """

    try:
        importlib.import_module(REQUIRED.get(distribution) or OPTIONAL[distribution])
    except Exception:  # noqa: BLE001 - the doctor reports every import failure as data
        return None
    try:
        return importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:  # pragma: no cover - unusual layouts only
        return "unknown"


def _external(name: str) -> str | None:
    executable, arguments = EXTERNAL[name]
    if shutil.which(executable) is None:
        return None
    try:
        result = subprocess.run([executable, *arguments], capture_output=True, text=True, encoding="utf-8", timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    first = next((line.strip() for line in (result.stdout or result.stderr).splitlines() if line.strip()), "")
    return first or "unknown"


def _check(name: str, status: str, detail: str, fix: str = "") -> dict:
    report = {"name": name, "status": status, "detail": detail}
    if fix:
        report["fix"] = fix
    return report


def _read_object(path: Path, label: str) -> tuple[dict | None, str | None]:
    """Read a JSON object; a missing or broken file becomes a check, never a traceback."""

    try:
        data = read_data(path)
    except FileNotFoundError:
        return None, f"{label} is missing"
    except (PipelineError, OSError) as error:
        return None, str(error)
    if not isinstance(data, dict):
        return None, f"{label} must contain a JSON object, found {type(data).__name__}"
    return data, None


def _long_paths_enabled() -> bool:
    """Windows' opt-in.  Anything that is not an explicit 1 counts as disabled."""

    try:
        # Imported by name so the module stays type-clean on POSIX, where `winreg` has no stubs.
        winreg = importlib.import_module("winreg")
    except ImportError:  # pragma: no cover - POSIX has no such key
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\FileSystem") as key:
            value, _kind = winreg.QueryValueEx(key, "LongPathsEnabled")
        return int(value) == 1
    except (OSError, ValueError):  # pragma: no cover - policy or a non-numeric value
        return False


def _path_length(root: Path | None) -> dict:
    """Warn before a build fails on MAX_PATH rather than after it has half-written a workspace."""

    base = Path(root) if root is not None else Path.cwd()
    length = len(str(base))
    if platform.system() != "Windows":
        return _check("path length", OK, f"{length} characters (this platform has no MAX_PATH limit)")
    if _long_paths_enabled():
        return _check("path length", OK, f"{length} characters; long paths are enabled")
    if length + PIPELINE_PATH_MARGIN <= MAX_PATH:
        return _check("path length", OK, f"{length} characters, +{PIPELINE_PATH_MARGIN} for generated paths")
    return _check(
        "path length",
        WARN,
        f"{length} characters; builds add at least {PIPELINE_PATH_MARGIN} more, past the {MAX_PATH}-character limit "
        "while long paths are disabled",
        WINDOWS_LONG_PATH_ADVICE,
    )


def _free_space(root: Path | None) -> dict:
    """Report the room a build has, so "Disk quota exceeded" is not the first sign of trouble."""

    base = Path(root) if root is not None else Path.cwd()
    probe = base if base.is_dir() else base.parent
    try:
        usage = shutil.disk_usage(probe)
    except OSError as error:  # pragma: no cover - the path was checked just above
        return _check("disk space", WARN, f"cannot read free space for {probe}: {error}")
    free = usage.free
    detail = f"{free / 1024**3:.1f} GiB free on {probe}"
    if free >= MINIMUM_FREE_BYTES:
        return _check("disk space", OK, detail)
    temporary = os.environ.get("TMPDIR") or os.environ.get("TEMP") or ""
    hint = f" (temporary files currently go to {temporary})" if temporary else ""
    return _check(
        "disk space",
        WARN,
        f"only {detail}, and a build plus the regression suite need about 2 GiB",
        f"free space on that volume, or point TMPDIR at a larger one{hint}",
    )


def _environment(*, github: bool) -> list[dict]:
    checks: list[dict] = []
    runtime = f"CPython {platform.python_version()} on {platform.system()} {platform.machine()}"
    if sys.version_info[:2] == (3, 12):
        checks.append(_check("python", OK, runtime))
    else:
        checks.append(_check("python", FAIL, runtime, "install CPython 3.12 and reinstall the package"))

    missing_required = [name for name in REQUIRED if _version(name) is None]
    if missing_required:
        checks.append(
            _check(
                "packages",
                FAIL,
                f"missing: {', '.join(sorted(missing_required))}",
                "reinstall the package with its dependencies (the offline bundle pins them)",
            )
        )
    else:
        found = ", ".join(f"{name} {_version(name)}" for name in REQUIRED)
        checks.append(_check("packages", OK, found))

    optional_missing = [name for name in OPTIONAL if _version(name) is None]
    if optional_missing:
        checks.append(
            _check(
                "optional packages",
                WARN,
                f"missing: {', '.join(sorted(optional_missing))}",
                "install the `simulation` extra (mujoco): `build` and `check` compile the consumer "
                "scene with it on every profile, kinematics included, and the tool identity records "
                "its version",
            )
        )
    else:
        found = ", ".join(f"{name} {_version(name)}" for name in OPTIONAL)
        checks.append(_check("optional packages", OK, found))

    for name in EXTERNAL:
        if name == "gh" and not github:
            continue
        version = _external(name)
        if version:
            checks.append(_check(name, OK, version))
        else:
            checks.append(
                _check(
                    name,
                    WARN,
                    "not found",
                    f"install {name} to use the workflows that need it",
                )
            )
    return checks


def _workspace(root: Path) -> list[dict]:
    checks: list[dict] = []
    root = Path(root).resolve()
    # A root that carries any part of the model contract is a model workspace with a file missing;
    # a root with none of them is simply the wrong directory, and one check is enough.
    contract_markers = ("config/robot.yaml", TOOLCHAIN_LOCK, SOURCE_LOCK, "config/profiles")
    try:
        layout = check_layout(root, "model")
    except PipelineError as error:
        broken = f"{root}: {error}"
        if any((root / path).exists() for path in contract_markers):
            # A model workspace that lost part of its contract is still worth the full report: the
            # checks below name the file and the command that writes it again.
            checks.append(
                _check(
                    "workspace",
                    FAIL,
                    broken,
                    "restore the missing file (a model branch carries the contract), or run `description model init`",
                )
            )
        else:
            return [_check("workspace", FAIL, broken, "pass --root with a model workspace")]
    else:
        checks.append(_check("workspace", OK, f"{root} ({layout.get('role', 'model')} layout)"))

    identity = tool_identity()
    locked, problem = _read_object(root / TOOLCHAIN_LOCK, TOOLCHAIN_LOCK)
    if locked is None:
        checks.append(
            _check(
                "toolchain lock",
                FAIL,
                problem or "the toolchain lock could not be read",
                "restore the file from the model branch, or run `description tool lock --root .` to pin this tool",
            )
        )
    else:
        differences = [
            key
            for key in (
                "version",
                "package_digest",
                "source_commit",
                "development",
                "python",
                "platform",
                "dependencies",
            )
            if locked.get(key) != identity[key]
        ]
        if differences:
            checks.append(
                _check(
                    "toolchain lock",
                    FAIL,
                    f"differs from this tool in: {', '.join(differences)}",
                    "run `description tool lock --root .` for a demo, or install the tool release the model pins",
                )
            )
        else:
            pinned = f"pins {locked.get('version')} / {str(locked.get('source_commit'))[:12]}"
            checks.append(_check("toolchain lock", OK, pinned))

    # ``locked`` is about to become the source lock; the worker check below needs the tool version the
    # model pins, not the snapshot it froze.
    pinned_tool = str((locked or {}).get("version") or "")
    locked, problem = _read_object(root / SOURCE_LOCK, SOURCE_LOCK)
    if locked is None:
        checks.append(
            _check(
                "source snapshot",
                FAIL,
                problem or "the source lock could not be read",
                "run `description source freeze --root .`",
            )
        )
    else:
        snapshot = locked.get("snapshot")
        if not isinstance(snapshot, str) or not snapshot:
            checks.append(
                _check(
                    "source snapshot",
                    FAIL,
                    "the lock does not name a snapshot to use",
                    "run `description source freeze --root .`",
                )
            )
        elif (root / snapshot / "manifest.json").is_file():
            checks.append(_check("source snapshot", OK, snapshot))
        else:
            checks.append(
                _check(
                    "source snapshot",
                    FAIL,
                    f"the locked snapshot is missing: {snapshot}",
                    "freeze the source again",
                )
            )

    profile_dir = root / "config/profiles"
    profiles = sorted(path.name for path in profile_dir.glob("*.json")) if profile_dir.is_dir() else []
    if profiles:
        checks.append(_check("profiles", OK, ", ".join(profiles)))
    else:
        checks.append(
            _check(
                "profiles",
                FAIL,
                "config/profiles/ is empty",
                "restore the profile files or re-run `description model init`",
            )
        )

    if (root / "manifest.json").is_file():
        checks.append(_check("delivered bundle", OK, "manifest.json present"))
    else:
        checks.append(_check("delivered bundle", WARN, "no manifest.json yet", "run `description build --root .`"))

    definition, problem = _read_object(root / "config/robot.yaml", "config/robot.yaml")
    source = (definition or {}).get("source")
    if not isinstance(source, dict) or source.get("provider") != "solidworks" or not source.get("worker_url"):
        return checks
    url = str(source["worker_url"])
    # A snapshot-only workflow does not need the worker, so this stays a warning; the probe is short
    # because `doctor` is the command a user runs while something is still wrong.
    try:
        health = worker_health(url, timeout=5)
    except PipelineError as error:
        checks.append(
            _check(
                "worker",
                WARN,
                str(error),
                "start the worker on the CAD machine (`worker.ps1 -Action Start`), or rebuild from the "
                "frozen snapshot with `description model update --reuse-source`",
            )
        )
    else:
        version = str(health.get("worker_version") or "unknown")
        if pinned_tool and version != pinned_tool:
            # The capture runs on the worker, not on this machine, so a worker that is not the version
            # the model pins captures with code the lock does not describe.  The snapshot records which
            # worker produced it, which is why this is a warning and not a failure - but it is the
            # warning a user needs before a capture from the previous release turns out to be the one
            # the model was built from.
            checks.append(
                _check(
                    "worker",
                    WARN,
                    f"{url} answers /health as {version}, but this model pins {pinned_tool}",
                    "update the worker to the pinned release "
                    "(`worker.ps1 -Action Update -Bundle description-worker-<version>-windows-x86_64.zip`, "
                    "with that release's SHA256SUMS beside it), or pin the release you actually run with "
                    "`description tool lock --root .`",
                )
            )
        else:
            checks.append(_check("worker", OK, f"{url} answers /health (worker {version})"))
    return checks


def run(root: Path | None = None, *, github: bool = False) -> dict:
    """Collect the checks; ``passed`` is false when any check failed."""

    checks = _environment(github=github)
    checks.append(_path_length(root))
    checks.append(_free_space(root))
    if root is not None:
        checks.extend(_workspace(Path(root)))
    failures = [check["name"] for check in checks if check["status"] == FAIL]
    warnings = [check["name"] for check in checks if check["status"] == WARN]
    return {
        "schema_version": "description.doctor/v1",
        "version": __version__,
        "root": str(Path(root).resolve()) if root is not None else None,
        "checks": checks,
        "failed": failures,
        "warned": warnings,
        "passed": not failures,
    }


def lines(report: dict) -> Iterable[str]:
    """Human-readable rendering: one line per check, fixes indented below."""

    for check in report["checks"]:
        yield f"{check['status']:<4} {check['name']:<16} {check['detail']}"
        if check.get("fix"):
            yield f"     {'':<16} → {check['fix']}"
