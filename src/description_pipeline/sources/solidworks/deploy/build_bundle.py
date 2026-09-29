"""Build the fixed-version worker bundle that ``worker.ps1 -Action Install`` consumes.

    python -m description_pipeline.sources.solidworks.deploy build-bundle \
        --version 0.2.0 --source /path/to/src --out description-worker-0.2.0.zip

The bundle carries the worker source, a version marker, the pinned Windows
requirements (the lock shipped next to this module) and, for hosts without
network access, a pre-downloaded wheel set:

    python -m pip download --only-binary=:all: --platform win_amd64 \
        --python-version 3.12 --implementation cp --abi cp312 -d wheels \
        -r src/description_pipeline/sources/solidworks/deploy/requirements/win-py312.lock
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
from pathlib import Path
from description_pipeline import __version__
from description_pipeline.build.archive import write_zip

DEFAULT_VERSION = __version__
LOCK_FILE = Path(__file__).with_name("requirements") / "win-py312.lock"


def default_requirements() -> tuple[str, ...]:
    """The pinned Windows worker dependency set (core + COM boundary)."""

    lines = []
    for line in LOCK_FILE.read_text(encoding="utf-8").splitlines():
        entry = line.strip()
        if entry and not entry.startswith("#"):
            lines.append(entry)
    return tuple(lines)


def build(source: Path, out: Path, version: str, wheels: Path | None, requirements: tuple) -> dict:
    source = Path(source).resolve()
    package_root = source / "description_pipeline"
    if not package_root.is_dir():
        raise SystemExit(f"no package at {package_root}")
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        raise SystemExit("bundle version must be X.Y.Z")
    declarations = ast.parse((package_root / "__init__.py").read_text(encoding="utf-8"))
    declared = [
        ast.literal_eval(node.value)
        for node in declarations.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "__version__" for target in node.targets)
    ]
    if declared != [version]:
        raise SystemExit(f"bundle version {version} differs from package version {declared}")
    out = Path(out).resolve()
    if out.exists():
        raise SystemExit(f"refusing to overwrite {out}")
    # A worker bundle must be byte-identical when it is rebuilt from the same commit: the extraction
    # and install paths hash it, and operators compare that hash between hosts.  Fixed metadata also
    # keeps the bundle digest stable when a checkout is cloned at a different time.
    start_here = (
        f"description Windows worker bundle {version}\n"
        "\n"
        "This archive is a component of the pipeline, not the starting point.  Start with the\n"
        "first-use guide, which covers the Windows machine, SolidWorks and the first pull request:\n"
        "\n"
        "    https://github.com/mimicverse/description-pipeline/blob/main/docs/solidworks-first-use.en.md\n"
        "    https://github.com/mimicverse/description-pipeline/blob/main/docs/solidworks-first-use.md  (Chinese)\n"
        "\n"
        "The guided install writes the host configuration, verifies this archive against the\n"
        "SHA256SUMS published beside it, installs the pinned runtime and runs the worker Doctor:\n"
        "\n"
        "    powershell -ExecutionPolicy Bypass -File .\\worker.ps1 -Action Setup"
        f" -Bundle .\\description-worker-{version}-windows-x86_64.zip `\n"
        "        -Assembly '<assembly path>' -AssemblyConfiguration Default\n"
        "\n"
        "The assembly is what Doctor opens; without it Setup installs and stops before the Doctor\n"
        "step, and says how to run it later.\n"
        "\n"
        "Repeated setup is safe, and -NoInstall writes the configuration only.  What is inside:\n"
        "\n"
        "    worker.ps1 / worker-host.example.json    the worker service and an example host config\n"
        "    submit.ps1 / submit-host.example.json    the one-command model submission entry\n"
        "    requirements.txt                         the pinned worker dependency set\n"
        "    src/                                     the exact tool source this bundle installs\n"
        "    version.json                             the bundle version checked before install\n"
    )
    entries: list[tuple[str, bytes]] = [
        ("START-HERE.txt", start_here.encode("utf-8")),
        ("version.json", (json.dumps({"version": version}, indent=2) + "\n").encode("utf-8")),
        ("requirements.txt", ("\n".join(requirements) + "\n").encode("utf-8")),
    ]
    for name in ("worker.ps1", "worker-host.example.json", "submit.ps1", "submit-host.example.json"):
        entries.append((name, Path(__file__).with_name(name).read_bytes()))
    for path in sorted(package_root.rglob("*")):
        if "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        if path.is_file():
            entries.append(
                (
                    (Path("src") / "description_pipeline" / path.relative_to(package_root)).as_posix(),
                    path.read_bytes(),
                )
            )
    if wheels is not None:
        for wheel in sorted(Path(wheels).glob("*")):
            if wheel.is_file():
                entries.append((f"wheels/{wheel.name}", wheel.read_bytes()))
    write_zip(out, entries)
    digest = hashlib.sha256(out.read_bytes()).hexdigest()
    print(json.dumps({"bundle": str(out), "version": version, "sha256": digest}, indent=2))
    return {"bundle": str(out), "version": version, "sha256": digest}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="directory containing description_pipeline/")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--version", default=DEFAULT_VERSION)
    parser.add_argument("--wheels", type=Path, default=None)
    parser.add_argument("--requirements", default=",".join(default_requirements()))
    args = parser.parse_args()
    build(
        args.source, args.out, args.version, args.wheels, tuple(item for item in args.requirements.split(",") if item)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
