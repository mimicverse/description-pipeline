"""Build the hash-pinned, wheel-only Airflow deployment lock for Linux CPython 3.12.

One tested workflow: resolve the plain-pin requirements file(s) plus the built
``mimicverse_description`` wheel in a single pip report, then emit
``name==version --hash=sha256:...  # wheel`` for every dependency that comes from a repository.
The pipeline wheel itself is never locked -- ``install.sh`` installs ``PIPELINE_WHEEL``
separately, and embedding its hash here would make the lock (and the source-commit identity
recorded inside the wheel) circular across rebuilds.

Usage:
  build_requirements_lock.py --python <venv>/bin/python \
    --requirements deploy/airflow/requirements.txt \
    --wheel /dist/mimicverse_description-<version>-py3-none-any.whl \
    --output deploy/airflow/requirements.lock

Pass ``--resolve-requirements requirements/linux-py312.lock`` (repeatable) to make the v1 runtime
lock win over transitive version ranges. Verification happens twice: every report entry must be a
wheel, and ``pip install --dry-run --require-hashes`` must accept the rendered lock.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

#: The tool wheel itself is resolved for its runtime closure but never locked.
EXCLUDED = {"mimicverse-description"}


def normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def resolve(
    python: str,
    requirements: str,
    wheel: str,
    extra_requirements: list[str],
    constraints: list[str],
    work: Path,
) -> list[tuple[str, str, str, str, str]]:
    """One pip transaction resolves the pinned inputs plus the wheel; parse its report.

    Constraint files may be hash locks: only their ``name==version`` head is used, as a plain
    constraints file, so already-tested versions win over transitive ranges without enabling
    pip's require-hashes mode during resolution.
    """
    report = work / "report.json"
    constraint_files: list[Path] = []
    for index, path in enumerate(constraints):
        plain = work / f"constraints-{index}.txt"
        plain.write_text(
            "\n".join(
                line.split(" --hash", 1)[0].strip()
                for line in Path(path).read_text(encoding="utf-8").splitlines()
                if line.strip() and not line.lstrip().startswith("#")
            )
            + "\n",
            encoding="utf-8",
        )
        constraint_files.append(plain)
    command = [
        python,
        "-m",
        "pip",
        "install",
        "--dry-run",
        "--ignore-installed",
        "--only-binary=:all:",
        "--report",
        str(report),
        "-r",
        requirements,
    ]
    for extra in extra_requirements:
        command += ["-r", extra]
    for constraint in constraint_files:
        command += ["-c", str(constraint)]
    command.append(wheel)
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit(f"pip resolve failed:\n{result.stdout}\n{result.stderr}")
    entries: list[tuple[str, str, str, str, str]] = []
    for item in json.loads(report.read_text(encoding="utf-8"))["install"]:
        metadata = item.get("metadata") or {}
        name, version = str(metadata.get("name", "")), str(metadata.get("version", ""))
        if normalize(name) in EXCLUDED:
            continue
        download = item.get("download_info") or {}
        digest = str((download.get("archive_info") or {}).get("hashes", {}).get("sha256", ""))
        wheel_name = str(download.get("url", "")).rsplit("/", 1)[-1]
        if not (name and version and digest and wheel_name.endswith(".whl")):
            raise SystemExit(f"unresolved report entry: {json.dumps(item)[:200]}")
        entries.append((name, version, digest, wheel_name, normalize(name)))
    if not entries:
        raise SystemExit("pip resolve returned no lockable distributions")
    entries.sort(key=lambda item: item[4])
    return entries


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--python", required=True, help="python of the tested environment (its pip is used)")
    parser.add_argument("--requirements", required=True, help="plain-pin requirements file (no hashes)")
    parser.add_argument("--wheel", required=True, help="built mimicverse_description wheel to resolve with it")
    parser.add_argument(
        "--resolve-requirements",
        action="append",
        default=[],
        metavar="FILE",
        help="extra -r FILE (e.g. requirements/linux-py312.lock); repeatable",
    )
    parser.add_argument(
        "--constraints",
        action="append",
        default=[],
        metavar="FILE",
        help="constrain resolution to already-tested versions (plain or hash lock); repeatable",
    )
    parser.add_argument("--output", required=True)
    parser.add_argument("--header", default="Hash-pinned resolved stack for Linux CPython 3.12 (tested Airflow 3.3.2).")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="airflow-lock-") as tmp:
        dest = Path(tmp)
        entries = resolve(
            args.python, args.requirements, args.wheel, args.resolve_requirements, args.constraints, dest
        )
        lines = [
            f"# {args.header}",
            f"# {len(entries)} pins; regenerate with scripts/build_requirements_lock.py --python <venv>/bin/python",
            "#   --requirements requirements.txt --wheel <dist>/mimicverse_description-<version>-py3-none-any.whl",
            "# Install with: pip install --require-hashes --only-binary=:all: -r requirements.lock",
            "# The pipeline wheel is installed separately by install.sh and is not locked here.",
        ]
        lines += [f"{name}=={version} --hash=sha256:{digest}  # {wheel}" for name, version, digest, wheel, _ in entries]
        rendered = "\n".join(lines) + "\n"
        candidate = dest / "requirements.lock.candidate"
        candidate.write_text(rendered, encoding="utf-8")
        verify = subprocess.run(
            [
                args.python,
                "-m",
                "pip",
                "install",
                "--dry-run",
                "--require-hashes",
                "--only-binary=:all:",
                "--ignore-installed",
                "-r",
                str(candidate),
            ],
            capture_output=True,
            text=True,
        )
        if verify.returncode != 0:
            sys.stderr.write(verify.stdout + verify.stderr)
            raise SystemExit("rendered lock failed pip --require-hashes verification")
    Path(args.output).write_text(rendered, encoding="utf-8")
    print(f"wrote {args.output}: {len(entries)} hash-pinned wheels")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
