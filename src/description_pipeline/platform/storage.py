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
        return _guarded_seal_target(base, run_id, stage)

    return resolve


def _guarded_seal_target(base: Path, run_id: str, stage: str) -> Path:
    """The one path guard shared by the resolver and the writer.

    Every component below the store root must exist without being a symbolic
    link, and the joined path must stay confined to the root - the writer never
    creates a directory outside the root and never follows a planted link.
    """

    current = base
    for part in (SEALED_ROOT_NAME, run_id, stage):
        current = current / part
        if current.is_symlink():
            raise PipelineError("A sealed delivery path may not traverse a symbolic link")
    target = base.joinpath(SEALED_ROOT_NAME, run_id, stage)
    if not target.resolve(strict=False).is_relative_to(base):
        raise PipelineError("The sealed delivery path escapes the configured store root")
    return target


def sealed_resolver_from_environment(environ: Mapping[str, str] | None = None) -> Callable[[str], Path]:
    """The deploy-side resolver: PIPELINE_STORE_ROOT is required; no implicit fallback."""

    source = os.environ if environ is None else environ
    root = source.get("PIPELINE_STORE_ROOT")
    if not root:
        raise PipelineError("PIPELINE_STORE_ROOT is not configured; sealed deliveries cannot be resolved")
    return sealed_resolver(Path(root))


def seal_delivery(root: str | Path, run_id: str, stage: str, source: str | Path) -> str:
    """Atomically seal one validated delivery tree and return its locator.

    Normative reference form (settled with step 2): the locator stays
    ``store://sealed/<run_id>/<stage>`` - **tree-level, no member segment**.
    A member is addressed as ``(tree locator, relative member path)`` and the
    unified member table records exactly that pair with its class, byte size
    and sha256; consumers resolve the tree through :func:`sealed_resolver`
    and read the member through the existing confined-path rules.  Keeping the
    tree locator free of member paths avoids opaque escaping rules for member
    names (Unicode, spaces) and keeps the frozen scope checks untouched.

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

    Durability is explicit: the writer creates the root chain when it is
    absent (root, ``sealed``, the run directory) and fsyncs, for every
    directory it created, that directory's **parent entry** as well as the
    directory itself - a freshly created ``sealed/`` is persisted in its
    parent ``store/`` before the seal is reported complete.
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
    target = _guarded_seal_target(base, run_id, stage)
    parent = target.parent
    sealed_root = base / SEALED_ROOT_NAME
    # Account for every directory this call actually creates, including the
    # implicit ancestors above the store root: walk up from the deepest needed
    # path until the first existing directory, then persist each new level's
    # parent entry on the way back down.
    created: list[Path] = []
    probe = parent
    while not probe.exists():
        created.append(probe)
        if probe == probe.parent:  # pragma: no cover - the filesystem root always exists
            break
        probe = probe.parent
    parent.mkdir(parents=True, exist_ok=True)
    for directory in created:
        _fsync_dir(directory.parent)
    for directory in (sealed_root, parent):
        _fsync_dir(directory)
    if target.exists():
        if target.is_symlink():
            raise PipelineError("A sealed delivery path may not traverse a symbolic link")
        existing = read_delivery_manifest(target)
        if existing["subject"]["digest"] != subject:
            raise PipelineError("The sealed delivery already holds a different subject")
        return uri

    temporary = Path(tempfile.mkdtemp(prefix=f".{stage}.", dir=parent))
    try:
        shutil.copytree(origin, temporary, dirs_exist_ok=True)
        copied = read_delivery_manifest(temporary)
        if copied["subject"]["digest"] != subject:
            raise PipelineError("The source changed while it was being sealed")
        _fsync_tree(temporary)
        os.replace(temporary, target)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    sealed = read_delivery_manifest(target)
    if sealed["subject"]["digest"] != subject:  # pragma: no cover - replace is atomic
        raise PipelineError("The sealed delivery does not carry the sealed subject")
    _fsync_dir(parent)
    return uri


def _fsync_tree(root: Path) -> None:  # pragma: no cover - durability syscalls
    """fsync every nested directory and file bottom-up, then the tree root."""

    for path in sorted(root.rglob("*"), reverse=True):
        if path.is_dir():
            _fsync_dir(path)
        elif path.is_file():
            _fsync_path(path)
    _fsync_dir(root)


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
