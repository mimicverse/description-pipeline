"""Independent-process execution of real handlers against an isolated fixture database."""

from __future__ import annotations

import copy
import json
import os
import sys
from contextlib import ExitStack
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import psycopg2

from description_pipeline.artifacts import ArtifactStore
from description_pipeline.io import digest
from description_pipeline.interface_records import CHECK_POLICY_V1, validate_check_policy
from description_pipeline.pipeline.dispatch import DispatchObservation
from description_pipeline.pipeline.services import CanonicalStageServices, ClaimPolicy
from description_pipeline.platform.facts import FactStore
from description_pipeline.platform.queries import read_run_projection
from tests.v1.publish_recovery_child import _FilePullRequests
from tests.v1.test_real_six_stage_chain import RealSixStageChainTests


class FixtureDispatch:
    """The local scheduling endpoint; canonical dispatch/claim/effect code remains real."""

    def trigger(self, command, conf):
        return {"dag_id": command.dag_id, "dag_run_id": command.dag_run_id}

    def observe(self, command, conf):
        return DispatchObservation(
            "known",
            {
                "dag_id": command.dag_id,
                "dag_run_id": command.dag_run_id,
                "conf": conf.to_record(),
            },
        )


def execute(context_path: Path, run_id: str, stage: str, fault: str | None = None) -> dict:
    context = json.loads(context_path.read_text())
    if "check_policy" in context:
        policy = validate_check_policy(context["check_policy"])
        CHECK_POLICY_V1.clear()
        CHECK_POLICY_V1.update(copy.deepcopy(policy))
    artifacts = ArtifactStore(context["artifacts"])

    def connect():
        return psycopg2.connect(context["database"], options=f"-c search_path={context['schema']},public")

    facts = FactStore(connect, artifacts)
    portable, native = context["portable"], context["native"]
    now = datetime.fromisoformat(context["now"])
    services = CanonicalStageServices(
        facts=facts,
        artifacts=artifacts,
        work_root=Path(context["work"]) / f"child-{os.getpid()}",
        claim_policy=ClaimPolicy(
            owner_sha256=digest({"fixture-process": os.getpid()}),
            executor_sha256=portable["source_sha256"],
            lease=timedelta(minutes=5),
        ),
        clock=lambda: now,
    )
    fixture = SimpleNamespace(store=artifacts, native=native, portable=portable, services=services, facts=facts)
    app = RealSixStageChainTests._application(
        fixture, pull_requests=_FilePullRequests(Path(context["pull_requests"])), clock=now
    )
    with ExitStack() as stack:
        for target, tool in (
            ("description_pipeline.runtime.tool_record", native),
            ("description_pipeline.pipeline.stages.generate.tool_record", portable),
            ("description_pipeline.pipeline.stages.publish.tool_record", portable),
            ("description_pipeline.mjcf_contract.tool_record", portable),
        ):
            stack.enter_context(patch(target, side_effect=lambda *args, _tool=tool, **kwargs: copy.deepcopy(_tool)))
        outcome = app.load_admission_outcome(run_id)
        app.dispatch(outcome.dispatch_command.command_id, FixtureDispatch())
        if fault == "local_publish_failure":
            stack.enter_context(
                patch(
                    "description_pipeline.pipeline.stages.publish.PublicationRepository.candidate",
                    side_effect=OSError(7, "fixture argument list too long"),
                )
            )
        elif fault == "unknown_publish":
            stack.enter_context(
                patch(
                    "description_pipeline.pipeline.stages.publish.PublicationRepository.push",
                    side_effect=RuntimeError("fixture transport outcome is unknown"),
                )
            )
        result = app.run_stage(run_id, stage)
        with connect() as connection:
            projection = read_run_projection(connection, run_id, artifacts=artifacts)
        failures = []
        if getattr(result, "outcome", None) == "failed" and hasattr(result, "output_artifact"):
            record = artifacts.verify(result.output_artifact)
            for member in record.get("members", ()):
                name = member["path"]
                if name.endswith(".json") and name.startswith(("reports/", "evidence/")):
                    try:
                        raw, _, _ = artifacts.read_metadata_member(
                            result.output_artifact, name, max_bytes=16 * 1024 * 1024
                        )
                        document = json.loads(raw)
                    except Exception:
                        continue

                    def collect(value, path=name):
                        if isinstance(value, dict):
                            if value.get("state") == "failed" or value.get("error") or value.get("error_code"):
                                failures.append({"path": path, "record": value})
                            else:
                                for child in value.values():
                                    collect(child)
                        elif isinstance(value, list):
                            for child in value:
                                collect(child)

                    collect(document)
        record = (
            {"outcome": "pending", "wait": result.wait.to_record()} if hasattr(result, "wait") else result.to_record()
        )
        return {"result": record, "projection": projection, "process": os.getpid(), "failures": failures}


if __name__ == "__main__":
    print(
        json.dumps(
            execute(Path(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4] if len(sys.argv) > 4 else None),
            sort_keys=True,
        )
    )
