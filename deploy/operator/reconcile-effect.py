#!/usr/bin/env python3
"""Reconcile one exact external effect through the production application."""

from __future__ import annotations

import argparse
import json
import sys

from description_pipeline.io import PipelineError
from description_pipeline.pipeline.application import STAGES, application_from_environment


class MaintenanceError(RuntimeError):
    """The production application cannot reconcile the requested effect."""


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--stage", choices=STAGES, required=True)
    parser.add_argument("--effect-key", required=True)
    return parser.parse_args(argv)


def _main(
    argv: list[str] | None = None,
    *,
    application_factory=application_from_environment,
) -> int:
    args = parse_args(argv)
    application = application_factory()
    facts = getattr(application, "facts", None)
    load_stage_command = getattr(facts, "load_stage_command", None)
    if not callable(load_stage_command):
        raise MaintenanceError("The production application has no canonical stage-command reader")
    command = load_stage_command(args.run_id, args.stage)
    reconcile_effect = getattr(application, "reconcile_effect", None)
    if not callable(reconcile_effect):
        raise MaintenanceError("The production application has no canonical effect reconciler")
    result = reconcile_effect(
        command_id=command.command_id,
        effect_key=args.effect_key,
    )
    print(
        json.dumps(
            {
                "run_id": args.run_id,
                "stage": args.stage,
                "command_id": command.command_id,
                "effect_id": result.effect_id,
                "effect_key": result.effect_key,
                "effect_state": result.settlement.state.value,
                "fact_id": None if result.fact is None else result.fact.fact_id,
                "fact_type": None if result.fact is None else result.fact.fact_type,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


def main(
    argv: list[str] | None = None,
    *,
    application_factory=application_from_environment,
) -> int:
    """CLI boundary with stable failure text and no parallel effect registry."""

    try:
        return _main(argv, application_factory=application_factory)
    except (OSError, PipelineError, RuntimeError) as error:
        code = getattr(error, "code", "application_effect_reconcile_failed")
        retained_workspace = getattr(error, "retained_workspace", None)
        detail = f":{retained_workspace}" if retained_workspace is not None else ""
        print(f"reconcile-effect[{code}]{detail}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
