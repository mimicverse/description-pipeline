"""Simulation configuration, sealed manifest and numeric check constants for URDF → MJCF.

Shared by the converter (:mod:`description_pipeline.mjcf`) and the independent verifier
(:mod:`description_pipeline.verification.mjcf`).  It owns format and schema only: no
mechanical derivation, no MuJoCo import and no state live here.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from .io import PipelineError, digest, inventory as _inventory, read_data

# --------------------------------------------------------------------------- #
# Delivery layout
# --------------------------------------------------------------------------- #

MODULE_ID = "mjcf"
SCHEMA_VERSION = 1
MUJOCO_VERSION = "3.13.0"

INPUT_PREFIX = "input"
SIMULATION_JSON = "simulation.json"
ROBOT_XML = "robot.xml"
SCENE_XML = "scene.xml"
MANIFEST_JSON = "manifest.json"
REPORT_JSON = "reports/quality.json"

#: The manifest covers every delivered file except itself and the report; the report binds to
#: the manifest subject, so including either would create a digest cycle.
SEALED_EXCLUDE = (MANIFEST_JSON, REPORT_JSON)

#: Files every delivery carries, besides the frozen ``input/`` closure it was built from.
REQUIRED_FILES = (SIMULATION_JSON, ROBOT_XML, MANIFEST_JSON, REPORT_JSON)

# --------------------------------------------------------------------------- #
# Numeric acceptance constants (declared once; every check reports its residual)
# --------------------------------------------------------------------------- #

POSITION_ABS = 1e-10
ROTATION_ABS = 1e-10
INERTIA_REL = 1e-10
INERTIA_ABS = 1e-12
MESH_ABS = 1e-6
MESH_REL = 1e-6

# --------------------------------------------------------------------------- #
# Configuration grammar
# --------------------------------------------------------------------------- #

BASE_MODES = ("fixed", "floating")
COLLISION_SOURCES = ("urdf", "visual_convex", "none")
INTEGRATORS = ("implicitfast", "implicit", "euler", "rk4")

_TOP_LEVEL = ("schema_version", "base", "collision", "simulation", "sites", "floor")
_REQUIRED_TOP = ("schema_version", "base", "collision", "simulation")
_COLLISION_KEYS = ("source", "self_collision", "friction")
_SIMULATION_KEYS = ("timestep", "gravity", "integrator")
_SITE_KEYS = ("name", "link", "xyz", "rpy")
_FLOOR_KEYS = ("height", "friction")


def _mapping(value: Any, what: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PipelineError(f"{what} must be a JSON object")
    return value


def _keys(data: dict[str, Any], allowed: tuple[str, ...], what: str) -> None:
    unknown = sorted(set(data) - set(allowed))
    if unknown:
        raise PipelineError(f"Unknown {what} keys: {unknown}")


def _number(value: Any, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PipelineError(f"{what} must be a number")
    number = float(value)
    if not math.isfinite(number):
        raise PipelineError(f"{what} must be finite")
    return number


def _boolean(value: Any, what: str) -> bool:
    if not isinstance(value, bool):
        raise PipelineError(f"{what} must be true or false")
    return value


def _text(value: Any, what: str) -> str:
    if not isinstance(value, str) or not value:
        raise PipelineError(f"{what} must be a non-empty string")
    return value


def _vector(value: Any, length: int, what: str) -> list[float]:
    if not isinstance(value, list) or len(value) != length:
        raise PipelineError(f"{what} must be a list of {length} numbers")
    return [_number(item, f"{what}[{index}]") for index, item in enumerate(value)]


def _friction(value: Any, what: str) -> list[float]:
    coefficients = _vector(value, 3, what)
    if any(coefficient < 0 for coefficient in coefficients):
        raise PipelineError(f"{what} coefficients must not be negative")
    return coefficients


def _collision(value: Any) -> dict[str, Any]:
    data = _mapping(value, "collision")
    _keys(data, _COLLISION_KEYS, "collision")
    missing = sorted(set(_COLLISION_KEYS) - set(data))
    if missing:
        raise PipelineError(f"Missing collision keys: {missing}")
    source = _text(data["source"], "collision.source")
    if source not in COLLISION_SOURCES:
        raise PipelineError(f"Unknown collision source: {source!r}; expected one of {list(COLLISION_SOURCES)}")
    return {
        "source": source,
        "self_collision": _boolean(data["self_collision"], "collision.self_collision"),
        "friction": _friction(data["friction"], "collision.friction"),
    }


def _simulation(value: Any) -> dict[str, Any]:
    data = _mapping(value, "simulation")
    _keys(data, _SIMULATION_KEYS, "simulation")
    missing = sorted(set(_SIMULATION_KEYS) - set(data))
    if missing:
        raise PipelineError(f"Missing simulation keys: {missing}")
    timestep = _number(data["timestep"], "simulation.timestep")
    if timestep <= 0:
        raise PipelineError("simulation.timestep must be positive")
    integrator = _text(data["integrator"], "simulation.integrator")
    if integrator not in INTEGRATORS:
        raise PipelineError(f"Unknown integrator: {integrator!r}; expected one of {list(INTEGRATORS)}")
    return {
        "timestep": timestep,
        "gravity": _vector(data["gravity"], 3, "simulation.gravity"),
        "integrator": integrator,
    }


def _sites(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise PipelineError("sites must be a list")
    sites: list[dict[str, Any]] = []
    names: set[str] = set()
    for index, item in enumerate(value):
        data = _mapping(item, f"sites[{index}]")
        _keys(data, _SITE_KEYS, f"sites[{index}]")
        missing = sorted(set(_SITE_KEYS) - set(data))
        if missing:
            raise PipelineError(f"Missing sites[{index}] keys: {missing}")
        name = _text(data["name"], f"sites[{index}].name")
        if name in names:
            raise PipelineError(f"Duplicate site name: {name!r}")
        names.add(name)
        sites.append(
            {
                "name": name,
                "link": _text(data["link"], f"sites[{index}].link"),
                "xyz": _vector(data["xyz"], 3, f"sites[{index}].xyz"),
                "rpy": _vector(data["rpy"], 3, f"sites[{index}].rpy"),
            }
        )
    return sites


def _floor(value: Any) -> dict[str, Any]:
    data = _mapping(value, "floor")
    _keys(data, _FLOOR_KEYS, "floor")
    missing = sorted(set(_FLOOR_KEYS) - set(data))
    if missing:
        raise PipelineError(f"Missing floor keys: {missing}")
    return {
        "height": _number(data["height"], "floor.height"),
        "friction": _friction(data["friction"], "floor.friction"),
    }


def validate_config(value: Any) -> dict[str, Any]:
    """Validate one simulation configuration document and return its normalized form."""

    data = _mapping(value, "Simulation configuration")
    _keys(data, _TOP_LEVEL, "simulation configuration")
    missing = sorted(set(_REQUIRED_TOP) - set(data))
    if missing:
        raise PipelineError(f"Missing simulation configuration keys: {missing}")
    version = data["schema_version"]
    if isinstance(version, bool) or version != SCHEMA_VERSION:
        raise PipelineError(f"Unsupported simulation configuration schema_version: {version!r}")
    base = _text(data["base"], "base")
    if base not in BASE_MODES:
        raise PipelineError(f"Unknown base mode: {base!r}; expected one of {list(BASE_MODES)}")
    return {
        "schema_version": SCHEMA_VERSION,
        "base": base,
        "collision": _collision(data["collision"]),
        "simulation": _simulation(data["simulation"]),
        "sites": _sites(data.get("sites", [])),
        "floor": _floor(data["floor"]) if "floor" in data else None,
    }


def read_config(path: Path) -> dict[str, Any]:
    """Read one simulation configuration with the shared strict reader (duplicate keys refused)."""

    return validate_config(read_data(Path(path)))


# --------------------------------------------------------------------------- #
# Sealed manifest
# --------------------------------------------------------------------------- #


def inventory(root: Path) -> dict[str, str]:
    """The manifest file table: every delivered file except the manifest and the report."""

    return _inventory(Path(root), exclude=SEALED_EXCLUDE)


def subject_sha256(manifest: dict[str, Any]) -> str:
    """The manifest subject digest: its own content without the digest it carries."""

    return digest({key: value for key, value in manifest.items() if key != "subject_sha256"})


def verify_subject(manifest: Any) -> dict[str, Any]:
    """Return the manifest when it carries a self-consistent subject digest."""

    data = _mapping(manifest, "Delivery manifest")
    if data.get("kind") != MODULE_ID:
        raise PipelineError(f"Not a {MODULE_ID} delivery manifest: kind={data.get('kind')!r}")
    if "subject_sha256" not in data:
        raise PipelineError("The delivery manifest carries no subject digest")
    if data["subject_sha256"] != subject_sha256(data):
        raise PipelineError("The delivery manifest subject digest does not match its own content")
    return data


def tool_identity() -> dict[str, str]:
    from . import __version__

    return {"module": f"description_pipeline.{MODULE_ID}", "version": __version__}


def engine_identity() -> dict[str, str]:
    """The locked consumer identity the delivery was compiled and checked against."""

    return {"mujoco": MUJOCO_VERSION}


def build_manifest(root: Path, *, urdf: str, sources: dict[str, str]) -> dict[str, Any]:
    """Seal one committed delivery directory: file table, frozen sources and subject digest."""

    manifest: dict[str, Any] = {
        "kind": MODULE_ID,
        "schema_version": SCHEMA_VERSION,
        "engine": engine_identity(),
        "tool": tool_identity(),
        "urdf": urdf,
        "config": SIMULATION_JSON,
        "sources": {name: sources[name] for name in sorted(sources)},
        "files": inventory(root),
    }
    manifest["subject_sha256"] = subject_sha256(manifest)
    return manifest
