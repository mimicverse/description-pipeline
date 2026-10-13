"""Read the target evidence tree, exposing the legacy leaf shapes.

W1 only changes *where* verification reads from (IF-13 tree + A's block
enumeration); W2/IF-17 rebuilds expectations from these same blocks.  Accessors
accept either the delivery root or its ``evidence/`` directory so call sites keep
their existing root convention.
"""

from __future__ import annotations

from pathlib import Path

from ..delivery import read_source_record
from ..io import PipelineError, confined, read_data

CAPTURE = "evidence/capture.json"
DISCOVERY = "evidence/discovery.json"
RUNTIME = "evidence/native-runtime.json"
HANDOFF = "input/handoff.json"


def _delivery_root(root: Path) -> Path:
    root = Path(root).resolve()
    return root.parent if root.name == "evidence" else root


def capture(root) -> dict:
    record = read_data(confined(_delivery_root(root), CAPTURE))
    if not isinstance(record, dict):
        raise PipelineError("Capture evidence is not an object")
    return record


def discovery(root) -> dict:
    record = read_data(confined(_delivery_root(root), DISCOVERY))
    if not isinstance(record, dict):
        raise PipelineError("Discovery evidence is not an object")
    return record


def runtime(root) -> dict:
    record = read_data(confined(_delivery_root(root), RUNTIME))
    if not isinstance(record, dict):
        raise PipelineError("Native runtime evidence is not an object")
    return record


def handoff(root) -> dict:
    record = read_data(confined(_delivery_root(root), HANDOFF))
    if not isinstance(record, dict):
        raise PipelineError("Frozen handoff record is not an object")
    return record


def source(root) -> dict:
    return read_source_record(_delivery_root(root))


def block(root, name: str):
    """A same-name verbatim capture block (W1 carries the old payloads)."""

    record = capture(root)
    if name not in record:
        raise PipelineError(f"Capture evidence block is missing: {name}")
    return record[name]


def scene(root) -> dict:
    return block(root, "scene")


def mass_closure(root) -> dict:
    return block(root, "mass_closure")


def material_provenance(root) -> dict:
    return block(root, "material_provenance")


def declared_masses(root) -> dict:
    return block(root, "declared_masses")


def dependency_closure(root) -> dict:
    return block(root, "dependency_closure")


def link_group_mass_properties(root) -> dict:
    return block(root, "link_group_mass_properties")


def collection(root) -> dict:
    """The verbatim collection block (kind/evidence_class/identity/limits/environment)."""

    return block(root, "collection")


def geometry(root):
    return block(root, "geometry")


def meshes(root) -> list:
    record = capture(root)
    rows = record.get("meshes")
    if not isinstance(rows, list):
        raise PipelineError("Capture evidence carries no mesh coverage rows")
    return rows


def instances(root) -> list:
    record = capture(root)
    rows = record.get("instances")
    if not isinstance(rows, list):
        raise PipelineError("Capture evidence carries no instance readings")
    return rows


def components(root) -> list:
    record = capture(root)
    rows = record.get("components")
    if not isinstance(rows, list):
        raise PipelineError("Capture evidence carries no component readings")
    return rows


def mass_properties(root) -> dict:
    """Legacy view: component-name to mass-property reading (old raw file shape)."""

    rows = components(root)
    readings = {str(row.get("name")): row.get("mass_properties") for row in rows}
    if not readings or any(value is None for value in readings.values()):
        raise PipelineError("Component mass-property readings are missing")
    return readings


def datums(root) -> dict:
    """Legacy view: datum name to 4x4 row-major matrix (old raw file shape)."""

    record = capture(root)
    rows = record.get("datums")
    if not isinstance(rows, list):
        raise PipelineError("Capture evidence carries no datum records")
    return {str(row["name"]): row["matrix"] for row in rows}


def axes(root) -> list:
    """Axis re-read records (verbatim entries, plus ``kinds`` for the rebuild)."""

    record = capture(root)
    rows = record.get("axes")
    if not isinstance(rows, list):
        raise PipelineError("Capture evidence carries no axis references")
    return rows
