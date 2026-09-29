"""CLI for the packaged Windows deployment resources."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import build_worker_bundle, export_deploy_resources, lock_requirements


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m description_pipeline.sources.solidworks.deploy",
        description=__doc__,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    export = subparsers.add_parser("export", help="copy worker.ps1, the host template and the lock out of the package")
    export.add_argument("--out", type=Path, required=True)

    bundle = subparsers.add_parser("build-bundle", help="build the offline Windows worker bundle")
    bundle.add_argument("--out", type=Path, required=True)
    bundle.add_argument("--version", required=True, help="worker version recorded in the bundle")
    bundle.add_argument("--source", type=Path, required=True, help="directory containing description_pipeline/")
    bundle.add_argument("--wheels", type=Path, default=None, help="pre-downloaded wheels for offline installs")

    subparsers.add_parser("requirements", help="print the pinned Windows dependency set")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "export":
        written = export_deploy_resources(args.out)
        print(json.dumps({"exported": [str(path) for path in written]}, indent=2))
        return 0
    if args.command == "build-bundle":
        result = build_worker_bundle(args.out, version=args.version, source=args.source, wheels=args.wheels)
        print(json.dumps(result, indent=2))
        return 0
    for requirement in lock_requirements():
        print(requirement)
    return 0


if __name__ == "__main__":
    sys.exit(main())
