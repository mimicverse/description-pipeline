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
from .pipeline.application import AdmissionOutcome, ApplicationOutcome, StageApplication
from .pipeline.contracts import FailureEnvelope, StagePending, StageResult
from .runtime import tool_record
from .stages import STAGE_IDS

ApplicationFactory = Callable[[], StageApplication]
CLI_VERSION_PREFIX = "description-pipeline"


def parser() -> argparse.ArgumentParser:
    commands = argparse.ArgumentParser(
        prog="description",
        description="Run the canonical seven-stage description pipeline.",
    )
    commands.add_argument("--version", action="version", version=f"{CLI_VERSION_PREFIX} {__version__}")
    operations = commands.add_subparsers(dest="operation", required=True)

    admit = operations.add_parser("admit", help="admit one immutable AdmissionSpec")
    admit.add_argument("spec", type=Path)

    stage = operations.add_parser("stage", help="execute one already-registered stage command")
    stage.add_argument("command_id")

    run = operations.add_parser("run", help="execute admitted stages through one boundary")
    run.add_argument("run_id")
    run.add_argument("--through", choices=STAGE_IDS)

    operations.add_parser("doctor", help="show the actual portable runtime identity")
    return commands


def _record(value) -> dict:
    if isinstance(value, (StageResult, FailureEnvelope, ApplicationOutcome)):
        return value.to_record()
    if isinstance(value, StagePending):
        return {
            "passed": False,
            "outcome": "pending",
            "wait": value.wait.to_record(),
        }
    raise PipelineError(f"Unsupported CLI result type: {type(value).__name__}")


def _default_application(
    arguments: argparse.Namespace, *, admission_spec: AdmissionSpec | None = None,
) -> StageApplication:
    from .pipeline.application import application_from_environment

    return application_from_environment(
        command_id=arguments.command_id if arguments.operation == "stage" else None,
        run_id=arguments.run_id if arguments.operation == "run" else None,
        through=getattr(arguments, "through", None),
        admission_spec=admission_spec,
    )


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
            spec = AdmissionSpec.from_record(read_data(arguments.spec)) if arguments.operation == "admit" else None
            application = (application_factory() if application_factory is not None
                           else _default_application(arguments, admission_spec=spec))
            if arguments.operation == "admit":
                admitted = application.admit(spec)
                result = {
                    "passed": True,
                    "outcome": "admitted",
                    **(admitted.to_record() if isinstance(admitted, AdmissionOutcome) else {}),
                }
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
