"""Hold the simulation candidate a measurement may run on, using the trusted tooling.

A model is published under the profile it already qualified for, usually
``kinematics``; a :mod:`simulation` build of the same commit is therefore only a
candidate until the external application attestation exists.  This step builds that
candidate with the locked tool and lets exactly one blocker - ``consumer.application`` -
continue, handing the complete candidate directory to the acceptance runner.  Every
other blocker stops the run: the runner then re-qualifies on the same frozen inputs and
still owns the subject binding.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from description_pipeline.build import build
from description_pipeline.io import PipelineError, write_json

PENDING = ["consumer.application"]


def candidate(root: Path, profile: str, destination: Path) -> dict:
    """Build the declared bundle and return how a workflow must consume it."""

    report = build(root, profile, destination=destination)
    if report["passed"]:
        return {"state": "published", "path": str(destination), "report": report}
    if report["blockers"] == PENDING and report.get("diagnostic_path"):
        return {"state": "pending", "path": str(report["diagnostic_path"]), "report": report}
    raise PipelineError(f"Simulation candidate is not acceptable: {report['blockers']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", type=Path, required=True, help="Model checkout")
    parser.add_argument("--profile", default="simulation")
    parser.add_argument("--destination", type=Path, required=True, help="Independent directory for a published bundle")
    parser.add_argument("--report", type=Path, required=True, help="Where to keep the build report")
    args = parser.parse_args(argv)
    try:
        resolution = candidate(args.root, args.profile, args.destination)
    except (PipelineError, OSError) as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False))
        return 1
    report = resolution["report"]
    write_json(args.report, report)
    if environment := os.environ.get("GITHUB_ENV"):
        with Path(environment).open("a", encoding="utf-8") as stream:
            stream.write(f"CANDIDATE={resolution['path']}\n")
    print(
        json.dumps(
            {
                "ok": True,
                "state": resolution["state"],
                "candidate": resolution["path"],
                "subject": report["subject"],
                "blockers": report["blockers"],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
