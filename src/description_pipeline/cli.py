"""SolidWorks-to-URDF command-line entry point."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .io import PipelineError, pin_utf8_streams, write_json


def _write_report(path: Path, result: dict, bundle: Path) -> None:
    """Recomputed reports are maintenance output: only outside the delivery."""

    if path.resolve().is_relative_to(bundle.resolve()):
        raise PipelineError("Write reports outside the delivery; delivered bytes are never rewritten")
    write_json(path, result)


def parser() -> argparse.ArgumentParser:
    commands = argparse.ArgumentParser(
        prog="description",
        description="SolidWorks worker administration and independent frozen-delivery verification.",
    )
    commands.add_argument("--version", action="version", version=f"solidworks-to-urdf {__version__}")
    operations = commands.add_subparsers(dest="operation", required=True)
    check = operations.add_parser("check", help="independently verify the actual delivered URDF and source evidence")
    check.add_argument("bundle", type=Path)
    check.add_argument("--report", type=Path, help="write the recomputed quality report to this file")
    rebuild = operations.add_parser("rebuild", help="regenerate a verified frozen bundle without SolidWorks")
    rebuild.add_argument("bundle", type=Path)
    rebuild.add_argument("--output", type=Path, required=True)
    serve = operations.add_parser("serve", help="run the authenticated Windows endpoint used by Apache Airflow")
    serve.add_argument("--config", type=Path, required=True)
    operations.add_parser("doctor", help="check this host's native or portable runtime")
    mjcf = operations.add_parser("mjcf", help="freeze, project and verify a MuJoCo delivery from a delivered URDF")
    mjcf_commands = mjcf.add_subparsers(dest="mjcf_operation", required=True)
    mjcf_build = mjcf_commands.add_parser("build", help="project a new checked MJCF delivery from a URDF tree")
    mjcf_build.add_argument("root", type=Path, help="directory holding the URDF and every mesh it references")
    mjcf_build.add_argument("--urdf", required=True, help="URDF path relative to the root")
    mjcf_build.add_argument("--config", type=Path, required=True, help="simulation configuration JSON")
    mjcf_build.add_argument("--output", type=Path, required=True, help="new output directory (must not exist)")
    mjcf_check = mjcf_commands.add_parser("check", help="independently verify an MJCF delivery")
    mjcf_check.add_argument("bundle", type=Path)
    return commands


def main(argv: list[str] | None = None) -> int:
    pin_utf8_streams()
    arguments = parser().parse_args(argv)
    try:
        if arguments.operation == "serve":
            from .orchestration.windows import serve

            serve(arguments.config)
            return 0
        if arguments.operation == "check":
            from .verification.solidworks_urdf import check_bundle

            result = check_bundle(arguments.bundle)
            if arguments.report:
                _write_report(arguments.report, result, arguments.bundle)
        elif arguments.operation == "mjcf":
            if arguments.mjcf_operation == "build":
                from . import mjcf

                result = mjcf.build(arguments.root, arguments.urdf, arguments.config, arguments.output)
            else:
                from .verification.mjcf import check_bundle

                result = check_bundle(arguments.bundle)
        else:
            from . import solidworks

            if arguments.operation == "rebuild":
                result = solidworks.rebuild(arguments.bundle, arguments.output)
            else:
                result = solidworks.doctor()
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
        return 0 if result.get("passed") is True else 1
    except (PipelineError, ValueError, OSError, TypeError, OverflowError) as error:
        print(json.dumps({"passed": False, "error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
