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
import shutil
import tempfile
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


def seal_delivery(root: str | Path, run_id: str, stage: str, source: str | Path) -> str:
    """Atomically seal one validated delivery tree and return its locator.

    The source must be a validated delivery tree: ``read_delivery_manifest``
    re-binds every subject record before anything is written.  The tree lands
    at ``<root>/sealed/<run_id>/<stage>`` through a same-filesystem temporary
    directory, per-file fsync and one atomic ``os.replace`` - readers resolve
    either the previous state or the complete new one, never a partial tree.
    Sealing is idempotent per subject digest (the same subject returns the same
    locator); a different subject for the same run and stage is refused.

    The sealed tree carries the delivery byte-verbatim - including the frozen
    ``input/request.json`` once it exists.  Which members verify against the
    native manifest and which original verifies against its database
    registration is a verification concern of the combination chain; this
    writer only commits bytes.
    """

    from ..delivery import read_delivery_manifest

    uri = f"{SEALED_URI_PREFIX}{run_id}/{stage}"
    parse_sealed_uri(uri)
    origin = Path(source).resolve()
    if not origin.is_dir():
        raise PipelineError("A sealed delivery source must be an existing directory")
    if any(path.is_symlink() for path in origin.rglob("*")):
        raise PipelineError("A sealed delivery source may not contain symbolic links")
    manifest = read_delivery_manifest(origin)
    subject = manifest["subject"]["digest"]

    base = Path(root).resolve()
    parent = base / SEALED_ROOT_NAME / run_id
    parent.mkdir(parents=True, exist_ok=True)
    target = parent / stage
    if target.exists():
        existing = read_delivery_manifest(target)
        if existing["subject"]["digest"] != subject:
            raise PipelineError("The sealed delivery already holds a different subject")
        return uri

    temporary = Path(tempfile.mkdtemp(prefix=f".{stage}.", dir=parent))
    try:
        shutil.copytree(origin, temporary, dirs_exist_ok=True)
        for path in sorted(temporary.rglob("*")):
            if path.is_file():
                _fsync_path(path)
        _fsync_dir(temporary)
        os.replace(temporary, target)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    sealed = read_delivery_manifest(target)
    if sealed["subject"]["digest"] != subject:  # pragma: no cover - replace is atomic
        raise PipelineError("The sealed delivery does not carry the sealed subject")
    _fsync_dir(parent)
    return uri


def _fsync_path(path: Path) -> None:  # pragma: no cover - durability syscall
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_dir(path: Path) -> None:  # pragma: no cover - durability syscall
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
