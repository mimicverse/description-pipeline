"""Assembly-rooted file selection, independent of transport and the CAD runtime.

The upload index is metadata, not permission to read every indexed file. Only
the main assembly and references observed from already selected documents may
be requested. Native adapters supply those observations; this module neither
searches the filesystem nor guesses a stored reference's relocated path.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping

from ...io import PipelineError, artifact_path_parts, digest

SELECTION_SCHEMA = "solidworks-to-urdf.selection/v1"
SELECTION_SCOPE = "saved_document_dependencies"
MAX_SELECTION_FILES = 4096
_SHA = re.compile(r"[0-9a-f]{64}\Z")


class SelectionError(PipelineError):
    """A source-selection refusal with stable, retained diagnostic fields."""

    def __init__(self, code: str, message: str, **detail):
        super().__init__(message)
        self.code = code
        self.detail = detail


def _path(value: object) -> str:
    if not isinstance(value, str) or unicodedata.normalize("NFC", value) != value:
        raise SelectionError("selection_path_invalid", "Selection paths must be canonical text", path=value)
    try:
        parts = artifact_path_parts(value)
    except PipelineError as error:
        raise SelectionError("selection_path_invalid", str(error), path=value) from error
    if not parts or "/".join(parts) != value or any(part.startswith("~$") for part in parts):
        raise SelectionError("selection_path_invalid", "Selection paths must be portable file names", path=value)
    return value


def validate_file_index(files: Mapping[str, str]) -> dict[str, str]:
    """Validate metadata without reading or authorizing any candidate file."""

    if not isinstance(files, Mapping) or not 0 < len(files) <= MAX_SELECTION_FILES:
        raise SelectionError("selection_inventory_invalid", "Selection inventory is empty or exceeds its limit")
    result: dict[str, str] = {}
    aliases: dict[str, str] = {}
    for name, checksum in files.items():
        name = _path(name)
        if not isinstance(checksum, str) or _SHA.fullmatch(checksum) is None:
            raise SelectionError("selection_digest_invalid", "File identity requires a SHA-256 digest", path=name)
        folded = name.casefold()
        if folded in aliases:
            raise SelectionError(
                "selection_path_collision", "Selection paths differ only by case", paths=[aliases[folded], name]
            )
        aliases[folded] = name
        result[name] = checksum
    for folded, name in aliases.items():
        parts = folded.split("/")
        if any("/".join(parts[:index]) in aliases for index in range(1, len(parts))):
            raise SelectionError("selection_path_collision", "A selection file is also a parent directory", path=name)
    return dict(sorted(result.items()))


def selection_plan(upload_files: Mapping[str, str], main_assembly: str, observations: Mapping[str, dict]) -> dict:
    """Recompute the next exact batch from recorded direct references.

    An observation has exactly ``sha256`` and ``references``. Every referenced
    file needs an observation, including an explicit empty reference list for a
    supported leaf resource. Deciding which resource types can be inspected is
    the native adapter's responsibility; missing records never mean no references.
    """

    files = validate_file_index(upload_files)
    main = _path(main_assembly)
    if not main.lower().endswith(".sldasm") or main not in files:
        raise SelectionError("selection_main_invalid", "Choose an indexed SolidWorks assembly", path=main)
    if not isinstance(observations, Mapping):
        raise SelectionError("selection_observation_invalid", "Dependency observations must be a mapping")
    normalized: dict[str, dict] = {}
    for name, record in observations.items():
        name = _path(name)
        if name not in files:
            raise SelectionError("selection_unknown_document", "Observed document is outside the upload", path=name)
        if not isinstance(record, dict) or set(record) != {"sha256", "references"}:
            raise SelectionError("selection_observation_invalid", "Invalid dependency observation", path=name)
        if record["sha256"] != files[name]:
            raise SelectionError("selection_source_changed", "Observed source differs from the upload", path=name)
        references = record["references"]
        if not isinstance(references, list) or len(references) > MAX_SELECTION_FILES:
            raise SelectionError("selection_observation_invalid", "Invalid dependency list", path=name)
        checked: list[str] = []
        for reference in references:
            reference = _path(reference)
            if reference not in files:
                raise SelectionError(
                    "selection_dependency_missing",
                    "An assembly dependency is not present at its recorded delivery path",
                    document=name,
                    reference=reference,
                )
            if reference in checked:
                raise SelectionError(
                    "selection_observation_invalid", "Repeated dependency edge", document=name, reference=reference
                )
            checked.append(reference)
        normalized[name] = {"sha256": files[name], "references": sorted(checked)}

    reachable: set[str] = set()
    pending = [main]
    while pending:
        name = pending.pop()
        if name in reachable:
            continue
        reachable.add(name)
        if name in normalized:
            pending.extend(normalized[name]["references"])
    unrelated = set(normalized) - reachable
    if unrelated:
        raise SelectionError(
            "selection_unrelated_document",
            "An observed document is not reachable from the main assembly",
            paths=sorted(unrelated),
        )
    selected = {name: files[name] for name in sorted(reachable)}
    return {
        "state": "complete" if reachable <= normalized.keys() else "requesting",
        "upload_sha256": digest(files),
        "main_assembly": main,
        "selected_files": selected,
        "requested_files": {name: files[name] for name in sorted(reachable - normalized.keys())},
        "observations": dict(sorted(normalized.items())),
    }


def seal_selection(
    upload_files: Mapping[str, str], main_assembly: str, observations: Mapping[str, dict], *, reader_sha256: str
) -> dict:
    """Seal a complete file graph; this does not qualify CAD geometry or motion."""

    if not isinstance(reader_sha256, str) or _SHA.fullmatch(reader_sha256) is None:
        raise SelectionError("selection_reader_invalid", "Selection requires a pinned native reader identity")
    plan = selection_plan(upload_files, main_assembly, observations)
    if plan["state"] != "complete":
        raise SelectionError(
            "selection_incomplete", "Dependency queries remain outstanding", paths=sorted(plan["requested_files"])
        )
    files = validate_file_index(upload_files)
    manifest = {
        "schema_version": SELECTION_SCHEMA,
        "scope": SELECTION_SCOPE,
        "reader_sha256": reader_sha256,
        "upload_sha256": plan["upload_sha256"],
        "main_assembly": plan["main_assembly"],
        "files": plan["selected_files"],
        "handoff_sha256": digest(plan["selected_files"]),
        "observations": plan["observations"],
        "excluded_files": {name: files[name] for name in files if name not in plan["selected_files"]},
    }
    return {**manifest, "selection_sha256": digest(manifest)}


def verify_selection(
    manifest: dict, upload_files: Mapping[str, str], selected_files: Mapping[str, str], *, reader_sha256: str
) -> dict:
    """Recompute graph membership and require exact transferred file identities."""

    if not isinstance(manifest, dict):
        raise SelectionError("selection_manifest_invalid", "Selection manifest must be an object")
    expected = seal_selection(
        upload_files, manifest.get("main_assembly"), manifest.get("observations"), reader_sha256=reader_sha256
    )
    if manifest != expected:
        raise SelectionError("selection_manifest_changed", "Selection manifest differs from its recorded graph")
    if validate_file_index(selected_files) != expected["files"]:
        raise SelectionError("selection_inventory_changed", "Transferred files differ from the assembly selection")
    return expected
