"""Compose the optional executor from the one platform-derived environment."""

from __future__ import annotations

import os
import stat
from importlib.resources import files
from pathlib import Path

from ..artifacts import ArtifactStore
from ..artifacts.store import path_has_link_ancestor
from ..runtime import scoped_executor_sha256, tool_record
from ..simulation.schemas import load_runtime_contract
from .contracts import RUNTIME_SCHEMA, RuntimePin, object_record, parse_json, require, text
from .executor import AssessmentExecutor
from .runtime import ProcessLimits, TeleopProcess
from .service import AssessmentService
from .store import AssessmentStore


def assessment_executor_from_environment(environment=None) -> AssessmentExecutor | None:
    """Disabled configuration returns before any inventory, DB or filesystem IO."""
    environment = os.environ if environment is None else environment
    encoded = environment.get("ASSESSMENT_CONFIG_JSON")
    if encoded is None:
        return None
    config = parse_json(encoded.encode())
    require(type(config.get("enabled")) is bool, "Assessment enable flag must be explicit")
    if not config["enabled"]:
        return None
    config = object_record(config, {"enabled", "root", "python", "capacity", "limits"}, "Assessment configuration")
    require(type(config["capacity"]) is int and 1 <= config["capacity"] <= 8, "Assessment capacity is invalid")
    root = Path(text(config["root"], "Assessment work root"))
    require(root.is_absolute() and not path_has_link_ancestor(root), "Assessment root is unsafe")
    root.mkdir(mode=0o700, exist_ok=True)
    identity = root.stat()
    require(
        identity.st_uid == os.geteuid() and stat.S_IMODE(identity.st_mode) == 0o700,
        "Assessment root must be private to the resource service account with mode 0700",
    )
    limits = ProcessLimits(
        **object_record(
            config["limits"], {"wall_seconds", "memory_bytes", "cpu_seconds", "output_bytes"}, "Assessment limits"
        )
    )
    installed = load_runtime_contract(
        files("description_pipeline").joinpath("simulation/teleop-runtime.json"), consumer="assessment"
    )
    pin = RuntimePin(
        {
            "schema": RUNTIME_SCHEMA,
            **{key: value for key, value in installed.items() if key != "schema_version"},
        }
    )
    python = Path(text(config["python"], "Assessment interpreter"))
    process = TeleopProcess(python, pin, limits)
    dsn = text(environment.get("PLATFORM_SERVICE_DSN"), "Platform service DSN")
    executor_sha = scoped_executor_sha256(tool_record("portable"), "optional_dynamics_assessment")

    def connect():
        import psycopg2

        return psycopg2.connect(dsn)

    artifacts = ArtifactStore.from_environment(environment)
    store = AssessmentStore(connect, artifacts)
    service = AssessmentService(
        store,
        enabled=True,
        runtime=pin,
        executor_sha256=executor_sha,
        work_root=root,
        capacity=config["capacity"],
        input_limit=min(limits.output_bytes, 64 * 1024**2),
    )
    return AssessmentExecutor(service, process)
