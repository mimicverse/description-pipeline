"""SolidWorks-to-URDF command-line entry point."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .io import PipelineError, pin_utf8_streams, write_json


def _write_report(path: Path, result: dict, bundle: Path) -> None:
    """Recomputed reports must never overwrite delivered subject files."""

    destination, bundle = path.resolve(), bundle.resolve()
    if destination.is_relative_to(bundle) and destination != bundle / "reports/quality.json":
        raise PipelineError("Write reports outside the delivery; only reports/quality.json may be updated inside it")
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
    rebuild.add_argument("--repository", type=Path)
    rebuild.add_argument("--base")
    rebuild.add_argument("--message")
    submit = operations.add_parser("submit", help="recheck and submit a frozen delivery from Windows or Linux")
    submit.add_argument("bundle", type=Path)
    submit.add_argument("--repository", type=Path, required=True)
    submit.add_argument("--base")
    submit.add_argument("--message")
    serve = operations.add_parser("serve", help="run the authenticated Windows endpoint used by Apache Airflow")
    serve.add_argument("--config", type=Path, required=True)
    operations.add_parser("doctor", help="check runtime and native capture availability")
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
        else:
            from . import solidworks

            if arguments.operation == "rebuild":
                if arguments.base and not arguments.repository:
                    raise PipelineError("--base requires --repository")
                result = solidworks.rebuild(
                    arguments.bundle,
                    arguments.output,
                    repository=arguments.repository,
                    base=arguments.base,
                    message=arguments.message,
                )
            elif arguments.operation == "submit":
                result = solidworks.submit(
                    arguments.bundle,
                    arguments.repository,
                    base=arguments.base,
                    message=arguments.message,
                )
            else:
                result = solidworks.doctor()
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
        return 0 if result.get("passed") is True else 1
    except (PipelineError, ValueError, OSError, TypeError, OverflowError) as error:
        print(json.dumps({"passed": False, "error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
