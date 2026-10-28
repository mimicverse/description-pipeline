"""One immutable engineering model and its deterministic projection boundary."""

from __future__ import annotations

import copy
import math
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jsonschema
import numpy as np

from ..io import PipelineError, digest, file_digest, inventory, read_data, write_json
from ..schemas import ENGINEERING_MODEL_SCHEMA as ENGINEERING_MODEL_SCHEMA
from ..sources.solidworks.naming import fixed_joint_name


@dataclass(frozen=True)
class EngineeringModel:
    """The only canonical robot semantics; mutable callers receive copies."""

    _data: dict

    @classmethod
    def from_dict(cls, data: dict) -> EngineeringModel:
        validate_engineering_model(data)
        model = cls(copy.deepcopy(data))
        model._validate()
        return model

    def to_dict(self) -> dict:
        return copy.deepcopy(self._data)

    @property
    def name(self) -> str:
        return str(self._data["name"])

    @property
    def semantic_sha256(self) -> str:
        """Digest the complete location-independent model payload."""

        return digest(self._data)

    def _validate(self) -> None:
        data = self._data
        for key in ("links", "joints", "frames"):
            for field in ("id", "name"):
                values = [obj[field] for obj in data[key]]
                if len(values) != len(set(values)):
                    raise PipelineError(f"Duplicate {key}.{field}")
        links = {link["name"]: link for link in data["links"]}
        frame_names = {frame["name"] for frame in data["frames"]}
        joint_names = {joint["name"] for joint in data["joints"]}
        generated_frame_joints = {
            fixed_joint_name(str(frame["parent"]), str(frame["name"])) for frame in data["frames"]
        }
        if frame_names & links.keys() or generated_frame_joints & joint_names:
            raise PipelineError("Frame names collide with generated URDF links/joints")
        children = set()
        for link in links.values():
            faces_by_component: dict[str, set[str]] = {}
            for geometry in link["visuals"] + link["collisions"]:
                for field in ("size", "scale"):
                    if field in geometry and any(value <= 0 for value in geometry[field]):
                        raise PipelineError(f"Geometry {field} must be positive")
                if any(not 0 <= value <= 1 for value in geometry.get("rgba", [])):
                    raise PipelineError("RGBA must be in [0, 1]")
                if "face_references" in geometry or "triangle_ranges" in geometry:
                    face_references = geometry.get("face_references")
                    ranges = geometry.get("triangle_ranges")
                    if (
                        geometry.get("provenance", {}).get("material_source")
                        != "native_appearance"
                        or not isinstance(face_references, list)
                        or not face_references
                        or not isinstance(geometry.get("mesh_sha256"), str)
                        or not isinstance(ranges, list)
                        or not ranges
                        or geometry.get("rgba") is None
                        or not isinstance(geometry.get("material"), str)
                    ):
                        raise PipelineError(
                            "Native appearance visual lacks its exact face or mesh partition"
                        )
                    next_start = 0
                    for row in ranges:
                        if (
                            not isinstance(row, dict)
                            or set(row) != {"triangle_start", "triangle_count"}
                            or type(row["triangle_start"]) is not int
                            or type(row["triangle_count"]) is not int
                            or row["triangle_start"] < next_start
                            or row["triangle_count"] <= 0
                        ):
                            raise PipelineError(
                                "Native appearance triangle ranges are not positive, ordered, and non-overlapping"
                            )
                        next_start += row["triangle_count"]
                    component = str(
                        geometry.get("provenance", {}).get("native_component")
                        or ""
                    )
                    seen = faces_by_component.setdefault(component, set())
                    if seen & set(face_references):
                        raise PipelineError(
                            "Native appearance groups repeat a delivered face"
                        )
                    seen.update(face_references)
            inertial = link["inertial"]
            if inertial is None:
                if link["provenance"].get("kind") != "reference_frame" or link["visuals"] or link["collisions"]:
                    raise PipelineError(f"Missing physical inertia is not a massless reference frame: {link['name']}")
                continue
            eigen = np.linalg.eigvalsh(tensor(inertial["inertia"]))
            if eigen[0] <= 0 or eigen[2] > eigen[0] + eigen[1] + 1e-10 * eigen[2]:
                raise PipelineError(f"Physically impossible COM inertia: {link['name']}")
        graph: dict[str, list[str]] = {name: [] for name in links}
        for joint in data["joints"]:
            if joint["parent"] not in links or joint["child"] not in links:
                raise PipelineError(f"Unknown joint endpoint: {joint['name']}")
            if joint["child"] in children:
                raise PipelineError(f"Multiple parent joints: {joint['child']}")
            children.add(joint["child"])
            graph[joint["parent"]].append(joint["child"])
            if joint["type"] != "fixed":
                if "axis" not in joint or np.linalg.norm(joint["axis"]) < 1e-12:
                    raise PipelineError(f"Missing or zero joint axis: {joint['name']}")
                if not np.isclose(np.linalg.norm(joint["axis"]), 1, atol=1e-8):
                    raise PipelineError(f"Joint axis must be normalized explicitly: {joint['name']}")
                if joint["type"] in {"revolute", "prismatic"}:
                    limits = joint.get("limits", {})
                    # Preserve a declared zero-width range for inspection.
                    # Usable motion is a separate independent qualification gate.
                    if "lower" not in limits or "upper" not in limits or limits["lower"] > limits["upper"]:
                        raise PipelineError(f"Missing or invalid position limits: {joint['name']}")
        roots = set(links) - children
        if len(roots) != 1:
            raise PipelineError(f"Expected one kinematic root, got {sorted(roots)}")
        visited = set()

        def walk(name: str) -> None:
            if name in visited:
                raise PipelineError(f"Cycle in kinematic tree: {name}")
            visited.add(name)
            for child in graph[name]:
                walk(child)

        walk(next(iter(roots)))
        if visited != set(links):
            raise PipelineError("Disconnected or cyclic links")
        moving = {joint["name"] for joint in data["joints"] if joint["type"] != "fixed"}
        mimic = {joint["name"]: joint["mimic"]["joint"] for joint in data["joints"] if joint.get("mimic")}
        for name, reference in mimic.items():
            if reference not in moving or name not in moving:
                raise PipelineError("Mimic must reference movable joints")
            seen = {name}
            while reference in mimic:
                if reference in seen:
                    raise PipelineError("Cyclic mimic dependency")
                seen.add(reference)
                reference = mimic[reference]
        for frame in data["frames"]:
            if frame["parent"] not in links:
                raise PipelineError(f"Unknown frame parent: {frame['parent']}")


def validate_engineering_model(data: dict) -> None:
    """Validate the one canonical model."""
    schema = read_data(Path(__file__).with_name("schema.json"))
    try:
        jsonschema.Draft202012Validator(schema).validate(data)
    except jsonschema.ValidationError as error:
        raise PipelineError(f"Model contract {list(error.absolute_path)}: {error.message}") from error
    _finite(data)


def compile(model_payload: dict, *, native_warnings: Iterable[dict] = ()) -> EngineeringModel:
    """Pure conversion from loader output and derived facts to the canonical model."""

    payload = copy.deepcopy(model_payload)
    warnings = payload.setdefault("provenance", {}).setdefault("value_warnings", [])
    if not isinstance(warnings, list):
        raise PipelineError("Model value warnings must be a list")
    warnings.extend(copy.deepcopy(list(native_warnings)))
    return EngineeringModel.from_dict(payload)


@dataclass(frozen=True)
class EmissionReceipt:
    """Digests of the deterministic projection emitted from one model."""

    model_sha256: str
    urdf_sha256: str
    mesh_files: tuple[dict[str, str], ...]


def emit(
    model: EngineeringModel,
    *,
    mesh_source_root: Path,
    destination: Path,
) -> EmissionReceipt:
    """Write the model and target projection without introducing new semantics."""

    from ..backends.urdf import generate_urdf

    destination = Path(destination)
    write_json(destination / "model/robot.json", model.to_dict())
    urdf = generate_urdf(
        model,
        Path(mesh_source_root),
        destination,
    )
    meshes = tuple(
        {"path": "meshes/" + path, "sha256": checksum}
        for path, checksum in sorted(inventory(destination / "meshes").items())
    )
    return EmissionReceipt(
        model_sha256=file_digest(destination / "model/robot.json"),
        urdf_sha256=file_digest(urdf),
        mesh_files=meshes,
    )


def _finite(value: Any) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise PipelineError("Nonfinite model value")
    if isinstance(value, dict):
        for child in value.values():
            _finite(child)
    elif isinstance(value, list):
        for child in value:
            _finite(child)


def tensor(values: list[float]) -> np.ndarray:
    xx, xy, xz, yy, yz, zz = values
    return np.array([[xx, xy, xz], [xy, yy, yz], [xz, yz, zz]], dtype=float)


def rotation(rpy: list[float]) -> np.ndarray:
    r, p, y = rpy
    cr, sr, cp, sp, cy, sy = math.cos(r), math.sin(r), math.cos(p), math.sin(p), math.cos(y), math.sin(y)
    return np.array(
        [
            [cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr],
            [sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr],
            [-sp, cp * sr, cp * cr],
        ]
    )
