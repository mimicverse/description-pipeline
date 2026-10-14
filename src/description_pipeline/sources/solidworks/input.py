"""Native acquisition binding for one prepared package.

The prepared package carries its frozen raw record at
``discovery/native-discovery.json``; capture binds against that record's
sealed source selection and file inventories, never against authored YAML.
Nothing here writes to the package, starts a CAD session, or consults a final
URDF.
"""

from __future__ import annotations

import copy
from pathlib import Path

from ...io import PipelineError, confined, file_digest, read_data


def _selection_matches_acquisition(record: dict, raw: dict, manifest: dict) -> None:
    """The sealed selection must equal both the acquired and the raw file inventories.

    ``record['native_files']`` is the acquired full package inventory and ``raw['files']``
    the native record's file map; with the producer recording every validated in-stage
    author source in both, the two must agree byte-for-byte with the sealed selection,
    so a missing or mutated retained source refuses here.
    """

    selected = manifest.get("files")
    excluded = manifest.get("excluded_files")
    if not isinstance(selected, dict) or not isinstance(excluded, dict) or set(selected) & set(excluded):
        raise PipelineError("Native source selection has invalid file inventories")
    if selected != record.get("native_files") or selected != raw.get("files"):
        raise PipelineError("Native source selection differs from the acquired CAD inventory")


def native_source_closure(package: Path) -> dict:
    """Bind capture policy to the sealed native acquisition.

    The prepared package may be relocated, while the closure retains the original
    staging paths as evidence.  The frozen handoff inventory is re-proved against
    the package bytes, and capture resolves current occurrences against these
    byte-bound native document identities.
    """
    from .selection import verify_selection

    record = read_data(confined(package, "discovery/native-discovery.json"))
    native_files = record.get("native_files")
    if not isinstance(native_files, dict) or not native_files:
        raise PipelineError("Native acquisition lacks its frozen handoff inventory")
    for relative, checksum in native_files.items():
        if not isinstance(relative, str) or file_digest(confined(package, relative)) != checksum:
            raise PipelineError("Native acquisition files differ from the frozen handoff")
    raw = record.get("raw") or {}
    manifest = raw.get("source_selection")
    if not isinstance(manifest, dict):
        raise PipelineError("Native acquisition lacks its sealed source selection")
    selected = manifest.get("files")
    excluded = manifest.get("excluded_files")
    _selection_matches_acquisition(record, raw, manifest)
    if manifest.get("main_assembly") != (record.get("identity") or {}).get("main_assembly"):
        raise PipelineError("Native source selection names another main assembly")
    verify_selection(manifest, {**excluded, **selected}, selected, reader_sha256=manifest.get("reader_sha256"))
    return copy.deepcopy(manifest["closure"])
