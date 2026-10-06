"""SolidWorks-to-URDF command-line entry point."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .io import PipelineError, file_digest, pin_utf8_streams, write_json


def _write_report(path: Path, result: dict, source: Path, *, bundle: bool = False) -> None:
    """Report options must never overwrite author inputs or delivered subject files."""

    destination, source = path.resolve(), source.resolve()
    if destination.is_relative_to(source):
        allowed = bundle and destination == source / "reports/quality.json"
        if not allowed:
            raise PipelineError("Write reports outside the input; only reports/quality.json may be updated in a bundle")
    write_json(path, result)


def parser() -> argparse.ArgumentParser:
    commands = argparse.ArgumentParser(
        prog="description",
        description="SolidWorks-to-URDF: inspect the CAD package, generate, verify and submit a PR.",
    )
    commands.add_argument("--version", action="version", version=f"solidworks-to-urdf {__version__}")
    operations = commands.add_subparsers(dest="operation", required=True)
    revision = operations.add_parser("revision", help="seal the mechanical team's immutable CAD handoff")
    revision.add_argument("input", type=Path)
    revision.add_argument("--hardware", required=True)
    revision.add_argument("--id", required=True, dest="revision_id")
    revision.add_argument("--parent", dest="parent_revision")
    revision.add_argument("--owner", required=True)
    revision.add_argument("--control", required=True, choices=("git", "pdm", "handoff"))
    revision.add_argument("--reference", required=True)
    revision.add_argument("--summary", required=True)
    inspect = operations.add_parser("inspect", help="check the author package before opening SolidWorks")
    inspect.add_argument("input", type=Path, help="self-contained CAD directory containing robot.yaml")
    inspect.add_argument("--report", type=Path, help="write the input report to this file")
    run = operations.add_parser("run", help="capture, generate, verify and automatically submit a PR")
    run.add_argument("input", type=Path)
    run.add_argument("--output", type=Path, required=True, help="delivery directory outside the input package")
    run.add_argument("--repository", type=Path, help="dedicated local model clone; omit for local verification")
    run.add_argument("--base", help="PR target branch; default feature/<hardware_id>")
    run.add_argument("--message", help="English commit message and PR title")
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
        if arguments.operation == "revision":
            from .sources.solidworks.revision import FILENAME, seal_revision

            revision = seal_revision(
                arguments.input,
                hardware_id=arguments.hardware,
                revision=arguments.revision_id,
                owner=arguments.owner,
                system=arguments.control,
                reference=arguments.reference,
                summary=arguments.summary,
                parent_revision=arguments.parent_revision,
            )
            result = {
                "passed": True,
                "cad_revision": revision,
                "manifest_sha256": file_digest(arguments.input / FILENAME),
            }
        elif arguments.operation == "inspect":
            from .solidworks import inspect_input

            result = inspect_input(arguments.input)
            if arguments.report:
                _write_report(arguments.report, result, arguments.input)
        elif arguments.operation == "check":
            from .verification.solidworks_urdf import check_bundle

            result = check_bundle(arguments.bundle)
            if arguments.report:
                _write_report(arguments.report, result, arguments.bundle, bundle=True)
        else:
            from . import solidworks

            if arguments.operation == "run":
                if arguments.base and not arguments.repository:
                    raise PipelineError("--base requires --repository")
                result = solidworks.run(
                    arguments.input,
                    arguments.output,
                    repository=arguments.repository,
                    base=arguments.base,
                    message=arguments.message,
                )
            elif arguments.operation == "rebuild":
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
