"""CLI adapter over the single StageApplication service."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from pathlib import Path

from . import __version__
from .io import PipelineError, pin_utf8_streams, read_data
from .pipeline.admission import AdmissionSpec
from .pipeline.application import ApplicationOutcome, STAGES, StageApplication
from .pipeline.contracts import FailureEnvelope, StageResult
from .runtime import tool_record

ApplicationFactory = Callable[[], StageApplication]


def parser() -> argparse.ArgumentParser:
    commands = argparse.ArgumentParser(
        prog="description",
        description="Run the canonical six-stage description pipeline.",
    )
    commands.add_argument("--version", action="version", version=f"description-pipeline {__version__}")
    operations = commands.add_subparsers(dest="operation", required=True)

    admit = operations.add_parser("admit", help="admit one immutable AdmissionSpec")
    admit.add_argument("spec", type=Path)

    stage = operations.add_parser("stage", help="execute one already-registered stage command")
    stage.add_argument("command_id")

    run = operations.add_parser("run", help="execute admitted stages through one boundary")
    run.add_argument("run_id")
    run.add_argument("--through", choices=STAGES, default="publish")

    operations.add_parser("doctor", help="show the actual portable runtime identity")
    return commands


def _record(value) -> dict:
    if isinstance(value, (StageResult, FailureEnvelope, ApplicationOutcome)):
        return value.to_record()
    raise PipelineError(f"Unsupported CLI result type: {type(value).__name__}")


def _default_application() -> StageApplication:
    from .pipeline.application import application_from_environment

    return application_from_environment()


def main(
    argv: list[str] | None = None,
    *,
    application_factory: ApplicationFactory | None = None,
) -> int:
    pin_utf8_streams()
    arguments = parser().parse_args(argv)
    try:
        if arguments.operation == "doctor":
            result = {"passed": True, "role": "portable", "identity": tool_record(role="portable")}
        else:
            application = (application_factory or _default_application)()
            if arguments.operation == "admit":
                spec = AdmissionSpec.from_record(read_data(arguments.spec))
                command = application.admit(spec)
                result = {"passed": True, "outcome": "admitted", "command": command.to_record()}
            elif arguments.operation == "stage":
                result = _record(application.execute_registered_command(arguments.command_id))
            else:
                result = application.execute_run(
                    arguments.run_id,
                    through=arguments.through,
                ).to_record()
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
        passed = result.get("passed")
        if passed is None:
            passed = result.get("outcome") == "completed"
        return 0 if passed is True else 1
    except (PipelineError, ValueError, OSError, TypeError, OverflowError) as error:
        print(json.dumps({"passed": False, "error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
