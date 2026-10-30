"""Saved-state open evidence shared by selection, capture, and verification."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ...io import PipelineError

SAVED_STATE_WARNING_CODE = "source.saved_state_incomplete"
SAVED_STATE_WARNING_MESSAGE = (
    "The assembly was exported from its saved state with native opening diagnostics. "
    "Source integrity, rebuild state, and complete mechanical qualification are not "
    "established, and this export must not be represented as source-qualified or "
    "hardware-qualified. Hidden or automatically suppressed objects remain unknown."
)

FILE_NOT_FOUND_ERROR = 0x2
MISSING_EXTERNAL_REFERENCES_WARNING = 0x100000
BASE_PART_NOT_LOADED_WARNING = 0x40

_ERROR_NAMES = {
    FILE_NOT_FOUND_ERROR: "swFileNotFoundError",
}
_WARNING_NAMES = {
    BASE_PART_NOT_LOADED_WARNING: "swFileLoadWarning_BasePartNotLoaded",
    MISSING_EXTERNAL_REFERENCES_WARNING: "swFileLoadWarning_MissingExternalReferences",
}
_STATUS_FIELDS = (
    "errors",
    "warnings",
    "classification",
    "error_bits",
    "warning_bits",
    "missing_references",
    "continuable",
)


def _integer(value: object, field: str) -> int:
    if type(value) is not int or value < 0:
        raise PipelineError(f"{field} must be a non-negative integer bitmask")
    return value


def _bit_rows(mask: int, names: Mapping[int, str]) -> list[dict[str, Any]]:
    rows = []
    remaining = mask
    for bit, name in sorted(names.items()):
        if remaining & bit:
            rows.append({"bit": bit, "name": name})
            remaining &= ~bit
    while remaining:
        bit = remaining & -remaining
        rows.append({"bit": bit, "name": f"unrecognized_{bit:#x}"})
        remaining &= ~bit
    return rows


def _missing_references(value: object) -> dict[str, Any]:
    """Keep observed paths or state that the bounded enumeration was unavailable."""

    if value is None:
        return {"state": "unavailable", "items": []}
    if not isinstance(value, Mapping):
        raise PipelineError("missing reference evidence must be an object")
    state = value.get("state")
    items = value.get("items")
    if state not in {"observed", "unavailable"} or not isinstance(items, list):
        raise PipelineError("missing reference evidence is malformed")
    if state == "unavailable" and items:
        raise PipelineError("unavailable missing-reference evidence cannot claim items")
    normalized = []
    for item in items:
        if (
            not isinstance(item, Mapping)
            or not isinstance(item.get("name"), str)
            or not item["name"]
            or not isinstance(item.get("last_known_path"), str)
        ):
            raise PipelineError("missing reference evidence contains a malformed item")
        normalized.append(
            {
                "name": item["name"],
                "last_known_path": item["last_known_path"],
            }
        )
    normalized.sort(key=lambda row: (row["name"], row["last_known_path"]))
    return {**dict(value), "state": state, "items": normalized}


def normalize_open_status(value: object) -> dict[str, Any]:
    """Return the canonical interpretation of one ``OpenDoc6`` status pair."""

    if not isinstance(value, Mapping):
        raise PipelineError("open status must be an object")
    errors = _integer(value.get("errors"), "open errors")
    warnings = _integer(value.get("warnings"), "open warnings")
    missing = _missing_references(value.get("missing_references"))
    located_missing = (
        missing["state"] == "observed" and bool(missing["items"])
        and all(item["last_known_path"].strip() for item in missing["items"])
    )
    if errors == 0 and warnings == 0:
        classification = "clean_open"
    elif errors == FILE_NOT_FOUND_ERROR:
        classification = (
            "continuable_missing_referenced_documents"
            if located_missing else "unlocated_missing_reference_error"
        )
    elif errors:
        classification = "unsupported_open_errors"
    elif warnings & (MISSING_EXTERNAL_REFERENCES_WARNING | BASE_PART_NOT_LOADED_WARNING):
        classification = "missing_reference_warning"
    else:
        classification = "saved_state_open_warnings"
    return {
        **{key: item for key, item in value.items() if key not in _STATUS_FIELDS},
        "errors": errors,
        "warnings": warnings,
        "classification": classification,
        "error_bits": _bit_rows(errors, _ERROR_NAMES),
        "warning_bits": _bit_rows(warnings, _WARNING_NAMES),
        "missing_references": missing,
        "continuable": errors == 0 or (errors == FILE_NOT_FOUND_ERROR and located_missing),
    }


def validate_open_status(value: object) -> dict[str, Any]:
    """Recompute the interpretation and reject a stale or forged derived field."""

    actual = dict(value) if isinstance(value, Mapping) else value
    expected = normalize_open_status(actual)
    if not isinstance(actual, Mapping):
        raise PipelineError("open status must be an object")
    for field in _STATUS_FIELDS:
        if field in actual and actual.get(field) != expected[field]:
            raise PipelineError(f"open status derived field {field} is inconsistent")
    return expected


def same_open_observation(selection_status: object, observed_status: object) -> bool:
    """Bind two records of one native open, not separate document openings."""

    left = validate_open_status(selection_status)
    right = validate_open_status(observed_status)
    fields = (
        "errors",
        "warnings",
        "classification",
        "error_bits",
        "warning_bits",
        "continuable",
        "missing_references",
    )
    return left["continuable"] and right["continuable"] and all(left[field] == right[field] for field in fields)


def _scope(selection: Mapping[str, Any]) -> dict[str, Any]:
    """Project the read scope from the sealed closure; never persist a second inventory."""

    closure = selection.get("closure")
    occurrences = closure.get("observed_occurrences") if isinstance(closure, Mapping) else None
    selected = selection.get("files")
    excluded = selection.get("excluded_files")
    if not isinstance(selected, Mapping) or not isinstance(excluded, Mapping):
        raise PipelineError("saved-state selection inventory is missing")
    if (
        not isinstance(occurrences, list)
        or any(
            not isinstance(item, Mapping)
            or not isinstance(item.get("instance"), str)
            or not item["instance"]
            or type(item.get("suppression_state")) is not int
            or item["suppression_state"] not in {0, 1, 2, 3, 4}
            for item in occurrences
        )
        or len({item["instance"] for item in occurrences}) != len(occurrences)
    ):
        raise PipelineError("saved-state occurrence observations are incoherent")
    unresolved = closure.get("unresolved", [])
    if not isinstance(unresolved, list) or any(
        not isinstance(item, Mapping) or item.get("suppression_state") != 0 for item in unresolved
    ):
        raise PipelineError("an unresolved occurrence lacks native suppression evidence")
    return {
        "main_assembly": selection["main_assembly"],
        "selected_files": sorted(selected),
        "excluded_files": sorted(excluded),
        "graph_nodes": len(closure["graph"]),
        "occurrence_count": len(occurrences),
        "occurrences": occurrences,
        "unresolved_occurrences": unresolved,
    }


def _selection_status(selection: object) -> dict[str, Any]:
    if not isinstance(selection, Mapping):
        raise PipelineError("saved-state selection must be an object")
    closure = selection.get("closure")
    if not isinstance(closure, Mapping):
        raise PipelineError("saved-state selection carries no closure")
    return validate_open_status(closure.get("open_status"))


def _warning(selection: Mapping[str, Any], observations: dict[str, dict]) -> dict[str, Any] | None:
    if any(not status["continuable"] for status in observations.values()):
        raise PipelineError("native open errors have no located, continuable missing-reference evidence")
    if all(status["errors"] == 0 and status["warnings"] == 0 for status in observations.values()):
        return None
    closure = selection["closure"]
    main = selection.get("main_assembly")
    configuration = closure.get("saved_configuration")
    if not isinstance(main, str) or not main:
        raise PipelineError("saved-state selection carries no main assembly")
    if not isinstance(configuration, str) or not configuration:
        raise PipelineError("saved-state selection carries no current configuration")
    scope = _scope(selection)
    message = SAVED_STATE_WARNING_MESSAGE + " " + " ".join(
        f"{stage.capitalize()} OpenDoc6 reported errors={status['errors']} and warnings={status['warnings']}."
        for stage, status in observations.items()
    )
    sources = {
        "discover": "input/discovery/native-discovery.json#/raw/source_selection/closure/open_status",
        "capture": "evidence/native-runtime.json#/prepared_capture/opened",
    }
    return {
        "code": SAVED_STATE_WARNING_CODE,
        "severity": "warning",
        "blocking": False,
        "objects": [
            {
                "object_type": "assembly",
                "object": main,
                "state": "saved_state_incomplete",
            }
        ],
        "message": message,
        "detail": {
            "open_observations": {
                stage: {"source": sources[stage], "status": status}
                for stage, status in observations.items()
            },
            "configuration": configuration,
            "missing_reference_enumeration_complete": False,
            "actual_scope": scope,
            "source_integrity_proven": False,
            "hidden_or_auto_suppressed_objects_unknown": True,
            "rebuilt": False,
            "mechanical_qualification_complete": False,
            "hardware_qualified": False,
        },
    }


def warning_from_selection(selection: object) -> dict[str, Any] | None:
    """Retain the selection's own OpenDoc6 observation."""

    status = _selection_status(selection)
    return _warning(selection, {"discover": status})


def warning_from_capture(
    selection: object,
    captured_open: object,
) -> dict[str, Any] | None:
    """Validate each independent opening and preserve both native observations."""

    selection_status = _selection_status(selection)
    captured_status = validate_open_status(captured_open)
    return _warning(selection, {"discover": selection_status, "capture": captured_status})


__all__ = [
    "BASE_PART_NOT_LOADED_WARNING",
    "FILE_NOT_FOUND_ERROR",
    "MISSING_EXTERNAL_REFERENCES_WARNING",
    "SAVED_STATE_WARNING_CODE",
    "SAVED_STATE_WARNING_MESSAGE",
    "normalize_open_status",
    "same_open_observation",
    "validate_open_status",
    "warning_from_capture",
    "warning_from_selection",
]
