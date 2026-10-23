"""Simulation configuration, seal-manifest format and numeric check constants.

Formats only: this module owns the strict configuration reader, the delivered
manifest shape, the deterministic element-naming scheme and the numeric gates
the independent checker applies.  It derives no mechanical fact from a robot.
"""

from __future__ import annotations

import hashlib
import math
from pathlib import Path
from typing import Any

from . import __version__
from .io import PipelineError, digest, file_digest, parse_data
from .runtime import RUNTIME_VERSIONS

MANIFEST_SCHEMA = "urdf-mjcf.manifest/v1"
REPORT_SCHEMA = "urdf-mjcf.quality/v1"

MANIFEST_NAME = "manifest.json"
REPORT_PATH = "reports/quality.json"
CONFIG_NAME = "simulation.json"
ROBOT_NAME = "robot.xml"
SCENE_NAME = "scene.xml"
INPUT_ROOT = "input"

#: The report states what the checks do not qualify; short-sample behaviour is
#: not a stability, control or training claim.
REPORT_SCOPE = (
    "structural URDF to MJCF conversion checks only; "
    "no standing, actuation, control or training qualification"
)

BASE_MODES = ("fixed", "floating")
COLLISION_SOURCES = ("urdf", "visual_convex", "none")
#: The native XML spellings accepted by the locked engine; no aliases.
INTEGRATORS = ("Euler", "RK4", "implicit", "implicitfast")
#: The locked MuJoCo enum codes behind those spellings (mjINT_*).
INTEGRATOR_CODES = {"Euler": 0, "RK4": 1, "implicit": 2, "implicitfast": 3}

#: The schema this projection speaks.  Published defaults of schema 1 are
#: immutable; any change to the default policy ships as a new schema version.
CONFIG_SCHEMA_VERSION = 1
#: The one versioned default policy every optional user field falls back to.
DEFAULT_POLICY = {
    "schema_version": CONFIG_SCHEMA_VERSION,
    "base": "floating",
    "collision": {"source": "urdf", "self_collision": True, "friction": [0.8, 0.005, 0.0001]},
    "simulation": {"timestep": 0.001, "gravity": [0.0, 0.0, -9.81], "integrator": "implicitfast"},
    "floor": {"height": 0.0},
}
#: The exact bytes sealed when the caller provides no configuration file.
EMPTY_CONFIG = b"{}\n"

#: The independent gates, in report order; the checker must produce every one.
CHECK_IDS = (
    "identity.closure",
    "engine.load",
    "topology.entities",
    "topology.dof",
    "kinematics.fk",
    "kinematics.axes",
    "inertia.links",
    "inertia.total",
    "geometry.shapes",
    "geometry.instances",
    "joints.config",
    "model.configuration",
    "scene.scene",
    "runtime.steps",
)

#: Geom groups encode the delivery role: rendering-only visuals and contacting
#: collision shapes.  Every robot surface is named <link>__<role>_<index>.
ROLE_GROUPS = {"visual": 2, "collision": 3}
VISUAL_GEOM_TEMPLATE = "{link}__visual_{index}"
COLLISION_GEOM_TEMPLATE = "{link}__collision_{index}"
CONVEX_GEOM_TEMPLATE = "{link}__convex_{index}"
SUPPORT_SITE_TEMPLATE = "{link}__support"
CONTACT_CONDIM = 3
#: Contact masks: (contype, conaffinity).  A solo robot geom still touches the
#: floor because the floor contributes the reverse mask.
ROBOT_CONTACT = (1, 1)
ROBOT_SOLO_CONTACT = (1, 0)
FLOOR_CONTACT = (1, 1)

FLOOR_GEOM_NAME = "floor"
FLOOR_RENDER_SIZE = "5 5 0.1"
LIGHT_NAME = "light"
LIGHT_POS = "0 0 3"
LIGHT_DIR = "0 0 -1"

#: Numeric gates defined once; the report records the actual maximum residual.
POSITION_ATOL_M = 1e-10
ROTATION_ATOL = 1e-10
MASS_REL_TOL = 1e-10
MASS_ATOL = 1e-12
INERTIA_RTOL = 1e-10
INERTIA_ATOL = 1e-12
#: Binary STL is float32; world vertices are compared at float32-representation
#: precision, not at the pure-encoding tolerance above.
MESH_ATOL_M = 1e-6
MESH_RTOL = 1e-6
AXIS_UNIT_ATOL = 1e-9
INERTIA_TRIANGLE_RTOL = 1e-9
#: Smaller structural gates: float32 colour storage, contact input echoes, the
#: default configuration and the option echo.
RGBA_ATOL = 1e-6
FRICTION_ATOL = 1e-10
DYNAMICS_ATOL = 1e-12
QPOS0_ATOL = 1e-15
OPTION_ATOL = 1e-12

#: Deterministic kinematics sampling: zero, two single-joint values per movable
#: joint and two combined configurations.
FK_FRACTIONS = (3 / 8, 5 / 8)
CONTINUOUS_SAMPLE_RAD = 0.75
RUNTIME_STEPS = 100

UNIT_SCALE = (1.0, 1.0, 1.0)

#: Support-site derivation decisions; one machine definition for both sides.
FLOOR_HEIGHT_SUPPORTS = "supports"
SUPPORT_PLANE_ATOL_M = 1e-6
SUPPORT_PLANE_RTOL = 1e-6
SUPPORT_MERGE_ANGLE_ATOL = 1e-6
SUPPORT_MERGE_OFFSET_ATOL_M = 1e-6
SUPPORT_AREA_RTOL = 1e-6
SUPPORT_NORMAL_Z_ATOL = 1e-9
SUPPORT_XAXIS_PARALLEL_ATOL = 1e-6
SUPPORT_TRIANGLE_EPS = 1e-12
SUPPORT_HULL_EPS = 1e-12

#: The closed generated-XML syntax: every element and attribute the projection
#: may emit.  Anything else (defaults, equality, includes in the robot,
#: gravcomp/armature/flag and friends) is refused before the engine ever reads
#: the file, so dynamics cannot be extended inside a resealed delivery.
ROBOT_XML_SYNTAX = {
    "mujoco": ["model"],
    "compiler": ["angle", "fusestatic", "inertiafromgeom"],
    "option": ["timestep", "gravity", "integrator"],
    "asset": [],
    "mesh": ["name", "file", "scale"],
    "worldbody": [],
    "body": ["name", "pos", "quat"],
    "freejoint": [],
    "joint": ["name", "type", "axis", "range", "limited", "damping", "frictionloss"],
    "inertial": ["pos", "quat", "mass", "diaginertia"],
    "geom": [
        "name",
        "type",
        "size",
        "pos",
        "quat",
        "rgba",
        "group",
        "contype",
        "conaffinity",
        "condim",
        "friction",
        "mesh",
    ],
    "site": ["name", "pos", "quat"],
}
ROBOT_XML_STRUCTURE = {
    "mujoco": ["compiler", "option", "asset", "worldbody"],
    "asset": ["mesh"],
    "worldbody": ["body"],
    "body": ["freejoint", "joint", "inertial", "geom", "site", "body"],
    "compiler": [],
    "option": [],
    "mesh": [],
    "freejoint": [],
    "joint": [],
    "inertial": [],
    "geom": [],
    "site": [],
}
SCENE_XML_SYNTAX = {
    "mujoco": [],
    "include": ["file"],
    "worldbody": [],
    "geom": ["name", "type", "size", "pos", "quat", "contype", "conaffinity", "condim", "friction"],
    "light": ["name", "pos", "dir", "directional"],
}
SCENE_XML_STRUCTURE = {
    "mujoco": ["include", "worldbody"],
    "include": [],
    "worldbody": ["geom", "light"],
    "geom": [],
    "light": [],
}
#: Attributes that must parse as finite numbers even before the engine reads them.
ROBOT_XML_NUMERIC = (
    "pos",
    "quat",
    "size",
    "rgba",
    "timestep",
    "gravity",
    "mass",
    "diaginertia",
    "axis",
    "range",
    "damping",
    "frictionloss",
    "contype",
    "conaffinity",
    "condim",
    "friction",
    "group",
)
SCENE_XML_NUMERIC = ("size", "pos", "quat", "contype", "conaffinity", "condim", "friction")


def visual_geom_name(link: str, index: int) -> str:
    return VISUAL_GEOM_TEMPLATE.format(link=link, index=index)


def collision_geom_name(link: str, index: int) -> str:
    return COLLISION_GEOM_TEMPLATE.format(link=link, index=index)


def convex_geom_name(link: str, index: int) -> str:
    return CONVEX_GEOM_TEMPLATE.format(link=link, index=index)


def mesh_asset_name(index: int) -> str:
    return f"mesh_{index}"


# --------------------------------------------------------------------------
# Pure numeric and rotation primitives shared by projection and verification.
# --------------------------------------------------------------------------


def number(value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise PipelineError(f"{where} must be a finite number, got {value!r}")
    return float(value)


def triple(value: Any, where: str) -> tuple[float, float, float]:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise PipelineError(f"{where} must be three finite numbers, got {value!r}")
    return tuple(number(item, where) for item in value)


def friction(value: Any, where: str) -> tuple[float, float, float]:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise PipelineError(f"{where} must be [slide, torsion, roll], got {value!r}")
    result = tuple(number(item, where) for item in value)
    if any(item < 0 for item in result):
        raise PipelineError(f"{where} coefficients must be non-negative, got {value!r}")
    return result


def rpy_matrix(rpy: tuple[float, float, float]) -> tuple[tuple[float, float, float], ...]:
    """The URDF convention: fixed-axis Rz(yaw) Ry(pitch) Rx(roll)."""

    roll, pitch, yaw = rpy
    cr, sr = math.cos(roll), math.sin(roll)
    cp, sp = math.cos(pitch), math.sin(pitch)
    cy, sy = math.cos(yaw), math.sin(yaw)
    return (
        (cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr),
        (sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr),
        (-sp, cp * sr, cp * cr),
    )


def quaternion_from_matrix(matrix) -> tuple[float, float, float, float]:
    """A (w, x, y, z) quaternion with a canonical sign, accepting any rotation matrix."""

    m = [[float(value) for value in row] for row in matrix]
    trace = m[0][0] + m[1][1] + m[2][2]
    if trace > 0:
        s = math.sqrt(trace + 1.0) * 2
        w, x, y, z = 0.25 * s, (m[2][1] - m[1][2]) / s, (m[0][2] - m[2][0]) / s, (m[1][0] - m[0][1]) / s
    elif m[0][0] > m[1][1] and m[0][0] > m[2][2]:
        s = math.sqrt(1.0 + m[0][0] - m[1][1] - m[2][2]) * 2
        w, x, y, z = (m[2][1] - m[1][2]) / s, 0.25 * s, (m[0][1] + m[1][0]) / s, (m[0][2] + m[2][0]) / s
    elif m[1][1] > m[2][2]:
        s = math.sqrt(1.0 + m[1][1] - m[0][0] - m[2][2]) * 2
        w, x, y, z = (m[0][2] - m[2][0]) / s, (m[0][1] + m[1][0]) / s, 0.25 * s, (m[1][2] + m[2][1]) / s
    else:
        s = math.sqrt(1.0 + m[2][2] - m[0][0] - m[1][1]) * 2
        w, x, y, z = (m[1][0] - m[0][1]) / s, (m[0][2] + m[2][0]) / s, (m[1][2] + m[2][1]) / s, 0.25 * s
    norm = math.sqrt(w * w + x * x + y * y + z * z)
    w, x, y, z = w / norm, x / norm, y / norm, z / norm
    if w < 0 or (w == 0 and next((component for component in (x, y, z) if component != 0), 0.0) < 0):
        w, x, y, z = -w, -x, -y, -z
    return w, x, y, z


# --------------------------------------------------------------------------
# Simulation configuration
# --------------------------------------------------------------------------


def _unknown(mapping: dict, allowed: set[str], where: str) -> None:
    unknown = sorted(key for key in mapping if key not in allowed)
    if unknown:
        raise PipelineError(f"Invalid simulation configuration: unknown key {unknown[0]!r} at {where}")


def _mapping(value: Any, where: str) -> dict:
    if not isinstance(value, dict):
        raise PipelineError(f"Invalid simulation configuration: {where} must be an object")
    return value


def _optional(mapping: dict, key: str, default: Any, where: str) -> Any:
    """Omitted fields fall back to the default policy; an explicit null never does."""

    if key not in mapping:
        return default
    value = mapping[key]
    if value is None:
        raise PipelineError(
            f"Invalid simulation configuration: {where}.{key} cannot be null; "
            "omit it to use the default policy"
        )
    return value


def _optional_mapping(mapping: dict, key: str, where: str) -> dict:
    return _mapping(_optional(mapping, key, {}, where), f"{where}.{key}")


def load_simulation(content: bytes, label: str = CONFIG_NAME) -> dict:
    """Resolve one user override document against the versioned default policy.

    Every field, object and list may be omitted; an explicit ``null`` is an
    error, never a shortcut back to the default.
    """

    data = parse_data(content, Path(label))
    root = _mapping(data, "the configuration")
    _unknown(
        root, {"schema_version", "base", "collision", "simulation", "sites", "support_sites", "floor"}, "root"
    )
    version = _optional(root, "schema_version", CONFIG_SCHEMA_VERSION, "root")
    if isinstance(version, bool) or version != CONFIG_SCHEMA_VERSION:
        raise PipelineError(
            f"Invalid simulation configuration: schema_version must be {CONFIG_SCHEMA_VERSION}, got {version!r}"
        )

    base = _optional(root, "base", DEFAULT_POLICY["base"], "root")
    if base not in BASE_MODES:
        raise PipelineError(f"Invalid simulation configuration: base must be one of {BASE_MODES}, got {base!r}")

    collision = _optional_mapping(root, "collision", "root")
    _unknown(collision, {"source", "self_collision", "friction"}, "collision")
    source = _optional(collision, "source", DEFAULT_POLICY["collision"]["source"], "collision")
    if source not in COLLISION_SOURCES:
        raise PipelineError(
            f"Invalid simulation configuration: collision.source must be one of {COLLISION_SOURCES}, got {source!r}"
        )
    self_collision = _optional(
        collision, "self_collision", DEFAULT_POLICY["collision"]["self_collision"], "collision"
    )
    if not isinstance(self_collision, bool):
        raise PipelineError("Invalid simulation configuration: collision.self_collision must be true or false")
    contact_friction = friction(
        _optional(collision, "friction", DEFAULT_POLICY["collision"]["friction"], "collision"), "collision.friction"
    )

    simulation = _optional_mapping(root, "simulation", "root")
    _unknown(simulation, {"timestep", "gravity", "integrator"}, "simulation")
    timestep = number(
        _optional(simulation, "timestep", DEFAULT_POLICY["simulation"]["timestep"], "simulation"),
        "simulation.timestep",
    )
    if timestep <= 0:
        raise PipelineError(f"Invalid simulation configuration: simulation.timestep must be positive, got {timestep!r}")
    gravity = triple(
        _optional(simulation, "gravity", DEFAULT_POLICY["simulation"]["gravity"], "simulation"),
        "simulation.gravity",
    )
    integrator = _optional(simulation, "integrator", DEFAULT_POLICY["simulation"]["integrator"], "simulation")
    if integrator not in INTEGRATORS:
        raise PipelineError(
            f"Invalid simulation configuration: simulation.integrator must be one of {INTEGRATORS}, got {integrator!r}"
        )

    sites: list[dict] = []
    site_names: set[str] = set()
    if "sites" in root:
        raw_sites = root["sites"]
        if not isinstance(raw_sites, list):
            raise PipelineError("Invalid simulation configuration: sites must be a list")
        for index, raw in enumerate(raw_sites):
            where = f"sites[{index}]"
            site = _mapping(raw, where)
            _unknown(site, {"name", "link", "xyz", "rpy"}, where)
            name = site.get("name")
            link = site.get("link")
            if not isinstance(name, str) or not name:
                raise PipelineError(f"Invalid simulation configuration: {where}.name must be a non-empty string")
            if not isinstance(link, str) or not link:
                raise PipelineError(f"Invalid simulation configuration: {where}.link must be a non-empty string")
            if name in site_names:
                raise PipelineError(f"Invalid simulation configuration: duplicate site name {name!r}")
            site_names.add(name)
            sites.append(
                {
                    "name": name,
                    "link": link,
                    "xyz": triple(_optional(site, "xyz", (0.0, 0.0, 0.0), where), f"{where}.xyz"),
                    "rpy": triple(_optional(site, "rpy", (0.0, 0.0, 0.0), where), f"{where}.rpy"),
                }
            )

    support_sites: list[dict] = []
    if "support_sites" in root:
        raw_sites = root["support_sites"]
        if not isinstance(raw_sites, list):
            raise PipelineError("Invalid simulation configuration: support_sites must be a list")
        for index, raw in enumerate(raw_sites):
            where = f"support_sites[{index}]"
            site = _mapping(raw, where)
            _unknown(site, {"name", "link"}, where)
            link = site.get("link")
            if not isinstance(link, str) or not link:
                raise PipelineError(f"Invalid simulation configuration: {where}.link must be a non-empty string")
            name = _optional(site, "name", SUPPORT_SITE_TEMPLATE.format(link=link), where)
            if not isinstance(name, str) or not name:
                raise PipelineError(f"Invalid simulation configuration: {where}.name must be a non-empty string")
            if name in site_names:
                raise PipelineError(
                    f"Invalid simulation configuration: site name {name!r} is used by more than one site"
                )
            site_names.add(name)
            support_sites.append({"name": name, "link": link})
    if support_sites and source == "none":
        raise PipelineError(
            "Invalid simulation configuration: support_sites require collision.source urdf or visual_convex"
        )

    floor = None
    if "floor" in root:
        raw_floor = _optional_mapping(root, "floor", "root")
        _unknown(raw_floor, {"height", "friction"}, "floor")
        raw_height = _optional(
            raw_floor,
            "height",
            FLOOR_HEIGHT_SUPPORTS if support_sites else DEFAULT_POLICY["floor"]["height"],
            "floor",
        )
        if raw_height == FLOOR_HEIGHT_SUPPORTS:
            if not support_sites:
                raise PipelineError(
                    f"Invalid simulation configuration: floor.height {FLOOR_HEIGHT_SUPPORTS!r} requires support_sites"
                )
            height, supports = None, True
        else:
            height, supports = number(raw_height, "floor.height"), False
        floor = {
            "height": height,
            "supports": supports,
            "friction": friction(_optional(raw_floor, "friction", contact_friction, "floor"), "floor.friction"),
        }

    return {
        "schema_version": CONFIG_SCHEMA_VERSION,
        "base": base,
        "collision": {"source": source, "self_collision": self_collision, "friction": contact_friction},
        "simulation": {"timestep": timestep, "gravity": gravity, "integrator": integrator},
        "sites": sites,
        "support_sites": support_sites,
        "floor": floor,
    }


# --------------------------------------------------------------------------
# Seal manifest
# --------------------------------------------------------------------------


def file_record(content: bytes) -> dict:
    return {"bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}


def manifest_document(*, source: dict, files: dict, tool: dict, engine: dict) -> dict:
    return {
        "schema_version": MANIFEST_SCHEMA,
        "source": source,
        "files": files,
        "tool": tool,
        "engine": engine,
    }


def tool_identity() -> dict:
    """Identify the code that produced or checks a delivery; volatile host facts stay out."""

    package = Path(__file__).resolve().parent
    files = {}
    for path in sorted(package.rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts and path.name != "tool-release.json":
            files[path.relative_to(package).as_posix()] = file_digest(path)
    return {"name": "mimicverse-description", "version": __version__, "source_sha256": digest(files)}


def engine_identity() -> dict:
    return {"mujoco": RUNTIME_VERSIONS["mujoco"]}
