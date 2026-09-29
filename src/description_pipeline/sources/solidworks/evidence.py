"""Environment and capture conditions recorded next to every snapshot.

A model built from a snapshot is only reproducible together with the conditions
it was captured under: which SolidWorks build, which worker, which configuration
and which capture options.  They are written as evidence, not inferred later.
"""

from __future__ import annotations

import platform
import sys
import time
from pathlib import Path
from typing import Any

EVIDENCE_SCHEMA = "description-pipeline.solidworks-evidence/v1"


def capture_environment(backend: Any = None, worker_version: str = "unknown") -> dict[str, Any]:
    """Everything that is cheap to read and needed to explain a snapshot later."""
    from ...build import tool_identity

    environment: dict[str, Any] = {
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "platform": platform.platform(),
        "python": sys.version.split()[0],
        "worker_version": worker_version,
        "toolchain": tool_identity(),
    }
    if backend is not None:
        reader = getattr(backend, "environment", None)
        if callable(reader):
            environment["solidworks"] = reader()
    return environment


def build_evidence(
    *,
    identity: dict[str, Any],
    environment: dict[str, Any],
    dependency_closure: dict[str, Any],
    capture: dict[str, Any],
    limits: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The evidence file that travels with a snapshot."""

    return {
        "schema_version": EVIDENCE_SCHEMA,
        "identity": identity,
        "environment": environment,
        "dependency_closure": dependency_closure,
        "capture": capture,
        "limits": limits or {},
    }


def write_json(path: Path, payload: object) -> None:
    from .jsonio import write_json as write

    write(path, payload)
