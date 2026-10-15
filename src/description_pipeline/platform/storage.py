"""Sealed delivery storage: the one locator for registered checkpoint references.

The platform store registers ``store://sealed/<run_id>/<stage>`` locators; the
bytes live under the configured store root.  Resolution is confined to that
root, binds exactly one run and one stage, refuses symbolic links and never
scans for a "newest" delivery - only a reference the store already registered
is resolved, and the delivery manifest is still verified independently.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Mapping
from pathlib import Path

from ..io import PipelineError
from ..stages import STAGE_IDS

SEALED_URI_PREFIX = "store://sealed/"
SEALED_ROOT_NAME = "sealed"
_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,249}\Z")


def parse_sealed_uri(uri: str) -> tuple[str, str]:
    """The (run_id, stage) of one sealed locator; every other shape is refused."""

    if not isinstance(uri, str) or not uri.startswith(SEALED_URI_PREFIX):
        raise PipelineError("A sealed checkpoint reference must read store://sealed/<run_id>/<stage>")
    parts = uri[len(SEALED_URI_PREFIX) :].split("/")
    if len(parts) != 2 or not all(parts):
        raise PipelineError("A sealed checkpoint reference names exactly one run and one stage")
    run_id, stage = parts
    if _RUN_ID.fullmatch(run_id) is None:
        raise PipelineError("The sealed reference carries an invalid run id")
    if stage not in STAGE_IDS:
        raise PipelineError(f"The sealed reference must name one engineering stage: {stage!r}")
    return run_id, stage


def sealed_resolver(root: str | Path) -> Callable[[str], Path]:
    """Resolve registered sealed locators under ``root``, refusing escape and symlinks."""

    base = Path(root).resolve()

    def resolve(uri: str) -> Path:
        run_id, stage = parse_sealed_uri(uri)
        current = base
        for part in (SEALED_ROOT_NAME, run_id, stage):
            current = current / part
            if current.is_symlink():
                raise PipelineError("A sealed delivery path may not traverse a symbolic link")
        target = base.joinpath(SEALED_ROOT_NAME, run_id, stage)
        if not target.resolve(strict=False).is_relative_to(base):
            raise PipelineError("The sealed delivery path escapes the configured store root")
        return target

    return resolve


def sealed_resolver_from_environment(environ: Mapping[str, str] | None = None) -> Callable[[str], Path]:
    """The deploy-side resolver: PIPELINE_STORE_ROOT is required; no implicit fallback."""

    source = os.environ if environ is None else environ
    root = source.get("PIPELINE_STORE_ROOT")
    if not root:
        raise PipelineError("PIPELINE_STORE_ROOT is not configured; sealed deliveries cannot be resolved")
    return sealed_resolver(Path(root))
