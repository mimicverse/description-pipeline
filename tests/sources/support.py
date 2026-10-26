"""Fixtures for source-adapter tests; the backend never touches live CAD."""

from __future__ import annotations

import copy
import hashlib
import struct
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

from description_pipeline.geometry.native_scope import (
    NATIVE_SOURCE_SCOPE_RULE,
)
from description_pipeline.io import file_digest
from description_pipeline.sources.solidworks.protocol import CadBackend, Matrix, RawComponent, RawScene

def binary_stl_bytes(
    triangles: Sequence[Sequence[Sequence[float]]],
    *,
    attributes: Sequence[int] | None = None,
) -> bytes:
    """Return the strict binary STL projection used by offline fixtures."""

    attributes = list(attributes or [0] * len(triangles))
    if len(attributes) != len(triangles):
        raise ValueError("binary STL attributes must match the triangle count")
    payload = bytearray(b"fixture stl".ljust(80, b" "))
    payload += struct.pack("<I", len(triangles))
    for triangle, attribute in zip(triangles, attributes, strict=True):
        payload += struct.pack("<3f", 0.0, 0.0, 1.0)
        for vertex in triangle:
            payload += struct.pack("<3f", float(vertex[0]), float(vertex[1]), float(vertex[2]))
        payload += struct.pack("<H", attribute)
    return bytes(payload)


def cube_triangles(
    half: float = 0.02,
) -> tuple[list[tuple[tuple[float, float, float], ...]], tuple[int, ...]]:
    corners = [(x, y, z) for x in (-half, half) for y in (-half, half) for z in (-half, half)]
    faces = [
        (0, 2, 3),
        (0, 3, 1),
        (4, 5, 7),
        (4, 7, 6),
        (0, 1, 5),
        (0, 5, 4),
        (2, 6, 7),
        (2, 7, 3),
        (0, 4, 6),
        (0, 6, 2),
        (1, 3, 7),
        (1, 7, 5),
    ]
    return (
        [(corners[a], corners[c], corners[b]) for a, b, c in faces],
        (1, 1, 2, 2, 3, 3, 4, 4, 5, 5, 6, 6),
    )


def fixture_source_scope(
    component: str,
    *,
    source_document_path: str | None = None,
    source_copy_path: str | None = None,
    source_document_sha256: str | None = None,
    referenced_configuration: str = "Default",
) -> dict[str, Any]:
    """Build one independent file-backed source scope for a test occurrence."""

    if source_document_sha256 is None:
        source_document_sha256 = hashlib.sha256(
            b"native-tessellation-fixture-source"
        ).hexdigest()
    return {
        "rule_version": NATIVE_SOURCE_SCOPE_RULE,
        "component": component,
        "source_document_path": (
            source_document_path
            or f"C:/fixture/source/{component}.SLDPRT"
        ),
        "source_copy_path": (
            source_copy_path or f"source/{component}.SLDPRT"
        ),
        "source_document_sha256": source_document_sha256,
        "source_copy_sha256": source_document_sha256,
        "source_document_identity_sha256": source_document_sha256,
        "source_document_identity_kind": "file_sha256",
        "referenced_configuration": referenced_configuration,
        "occurrence_scope": component,
        "virtual_owner": None,
        "virtual_owner_sha256": None,
        "virtual_session_document_path": None,
        "virtual_session_temp_root": None,
        "virtual_native_api_pid": None,
    }


def fixture_mass_fact(reading: dict[str, Any]) -> dict[str, Any]:
    """Convert one projected fixture mass reading back to the native raw shape."""

    result = copy.deepcopy(reading)
    reference = result.get("reference") or {}
    overrides = reference.get("overrides")
    if overrides is None:
        overrides = {
            "OverrideMass": False,
            "OverrideCenterOfMass": False,
            "OverrideMomentsOfInertia": False,
        }
    inertia = [
        float(value)
        for row in result.get("inertia") or []
        for value in row
    ]
    native_raw = {
        "mass_kg": float(result.get("mass")),
        "center_of_mass_m": [
            float(value)
            for value in result.get("com") or []
        ],
        "inertia_values": inertia,
        "overrides": copy.deepcopy(overrides),
    }
    for key in ("volume_m3", "density_kg_m3"):
        if type(reference.get(key)) in (int, float):
            native_raw[key] = float(reference[key])
    part_measurement = result.pop("part_measurement", None)
    if isinstance(part_measurement, dict):
        result["part_measurement_raw"] = fixture_mass_fact(
            {
                "mass": part_measurement["mass_kg"],
                "com": part_measurement["center_of_mass_m"],
                "inertia": part_measurement["inertia_kg_m2"],
                "reference": {
                    key: value
                    for key, value in part_measurement.items()
                    if key
                    not in {
                        "mass_kg",
                        "center_of_mass_m",
                        "inertia_kg_m2",
                    }
                },
            }
        )
    result["native_raw"] = native_raw
    result["reference"] = copy.deepcopy(reference)
    result.setdefault("read_errors", {})
    return result


def fixture_component_mass_fact(reading: dict[str, Any]) -> dict[str, Any]:
    """Convert projected component-context readings to the native raw shape."""

    result = copy.deepcopy(reading)
    for row in result.get("instances") or []:
        if isinstance(row, dict) and isinstance(row.get("effective"), dict):
            row["effective"] = fixture_mass_fact(row["effective"])
        if isinstance(row, dict):
            row.setdefault("read_errors", {})
    return result


def fixture_link_group_mass_fact(reading: dict[str, Any]) -> dict[str, Any]:
    """Convert projected link reads back to native getter evidence."""

    result = copy.deepcopy(reading)
    for body in (result.get("links") or {}).values():
        for row in body.get("reads") or []:
            row["com_m"] = [float(value) for value in row.pop("com_m", row.get("com") or [])]
            row["inertia_values_raw"] = [
                float(value)
                for values in row.pop("inertia_tensor_about_com", [])
                for value in values
            ]
            row.setdefault("read_errors", {})
            row.setdefault("overrides", {
                "OverrideMass": False,
                "OverrideCenterOfMass": False,
                "OverrideMomentsOfInertia": False,
            })
    return result


def fixture_native_geometry(
    component: str,
    source_scope: dict[str, Any],
    transform_raw: Sequence[float],
    *,
    half: float = 0.199999,
) -> dict[str, Any]:
    """Build one direct `GetTessTriangles(True)`-shaped occurrence fact."""

    triangles, _attributes = cube_triangles(half=half)
    values = [coordinate for triangle in triangles for point in triangle for coordinate in point]
    body_inventory = [
        {
            "body_type": 0,
            "body_index": 0,
            "body_name": "fixture-solid",
            "face_count": 6,
        }
    ]
    face_inventory = [
        {
            "body_type": 0,
            "body_index": 0,
            "face_index": face_index,
            "triangle_start": face_index * 2,
            "triangle_count": 2,
        }
        for face_index in range(6)
    ]
    return {
        "component": component,
        "used_api": "IComponent2.GetBodies2/IBody2.GetFaces/IFace2.GetTessTriangles(True)",
        "triangles": len(triangles),
        "triangle_values": values,
        "body_inventory": body_inventory,
        "face_inventory": face_inventory,
        "empty_faces": [],
        "errors": [],
        "faces": 6,
        "units": "m",
        "state": "observed",
        "representation": "cad_face_display_triangles_raw",
        "source_scope": copy.deepcopy(source_scope),
        "transform_raw": [float(value) for value in transform_raw],
        "bodies": {"solid": 1, "sheet": 0},
    }


def write_file_source_scope_authority(
    root: Path,
    *,
    source_scopes: dict[str, dict[str, Any]],
    source_files: dict[str, dict[str, bytes]],
) -> None:
    """Write one real file-backed source-scope authority tree for tests."""

    from description_pipeline.io import read_data, write_json  # noqa: PLC0415

    root = Path(root)
    (root / "evidence/raw").mkdir(parents=True, exist_ok=True)
    discovery_components = []
    discovery_files = {}
    scene_components = []
    scene_mass_properties = {}
    dependency_mapping = []
    original_files = {}
    copy_files = {}
    for component in sorted(source_scopes):
        scope = source_scopes[component]
        files = source_files[component]
        source_bytes = files["source"]
        copy_bytes = files["copy"]
        if (
            hashlib.sha256(source_bytes).hexdigest()
            != scope["source_document_sha256"]
            or hashlib.sha256(copy_bytes).hexdigest()
            != scope["source_copy_sha256"]
        ):
            raise ValueError(
                f"source files do not match the scope for {component}"
            )
        frozen = root / "evidence" / scope["source_copy_path"]
        frozen.parent.mkdir(parents=True, exist_ok=True)
        frozen.write_bytes(copy_bytes)
        source_path = scope["source_document_path"]
        document = Path(source_path.replace("\\", "/")).name
        source_sha256 = scope["source_document_sha256"]
        copy_sha256 = scope["source_copy_sha256"]
        discovery_components.append(
            {
                "instance_id": component,
                "name2": component,
                "document": document,
                "configuration": scope["referenced_configuration"],
                "suppressed": False,
                "source_provenance": None,
            }
        )
        discovery_files[document] = source_sha256
        scene_components.append(
            {
                "name": component,
                "path": source_path,
                "configuration": scope["referenced_configuration"],
            }
        )
        scene_mass_properties[component] = {
            "reference": {
                "configuration": scope["referenced_configuration"],
            }
        }
        dependency_mapping.append(
            {
                "instance": component,
                "copy": scope["source_copy_path"],
                "copy_sha256": copy_sha256,
                "source": source_path,
                "source_sha256": source_sha256,
            }
        )
        original_files[source_path] = source_sha256
        copy_files[scope["source_copy_path"]] = copy_sha256

    write_json(
        root / "evidence/discovery.json",
        {
            "raw": {
                "components": discovery_components,
                "files": discovery_files,
            },
            "files": [
                {"path": path, "sha256": sha256}
                for path, sha256 in discovery_files.items()
            ],
        },
    )
    write_json(
        root / "evidence/raw/scene_raw.json",
        {
            "components": scene_components,
            "mass_properties": scene_mass_properties,
        },
    )
    capture_path = root / "evidence/capture.json"
    capture = read_data(capture_path) if capture_path.is_file() else {}
    capture["components"] = len(source_scopes)
    capture["scene"] = {"components": scene_components}
    write_json(capture_path, capture)
    write_json(
        root / "evidence/raw/geometry.json",
        [{"component": component} for component in sorted(source_scopes)],
    )
    write_json(
        root / "evidence/raw/dependency_closure.json",
        {
            "mapping": dependency_mapping,
            "original_files": original_files,
            "copy_files": copy_files,
        },
    )
    write_json(
        root / "evidence/raw/source_scopes.json",
        source_scopes,
    )


def _matrix16(values: Sequence[float]) -> Matrix:
    """Coerce a sequence to the 4x4 row-major matrix shape the protocol declares."""

    numbers = [float(value) for value in values]
    if len(numbers) != 16:
        raise ValueError(f"expected 16 matrix values, got {len(numbers)}")
    return cast(Matrix, tuple(numbers))


def _identity() -> list[float]:
    return [
        1.0,
        0.0,
        0.0,
        0.0,
        0.0,
        1.0,
        0.0,
        0.0,
        0.0,
        0.0,
        1.0,
        0.0,
        0.0,
        0.0,
        0.0,
        1.0,
    ]


def solidworks_transform(matrix: Sequence[float]) -> list[float]:
    """Convert one row-major scene matrix to SolidWorks' raw array order."""

    values = _matrix16(matrix)
    return [
        values[0],
        values[4],
        values[8],
        values[1],
        values[5],
        values[9],
        values[2],
        values[6],
        values[10],
        values[3],
        values[7],
        values[11],
        1.0,
        0.0,
        0.0,
        0.0,
    ]


def placement(xyz: Sequence[float] = (0.0, 0.0, 0.0), rpy: Sequence[float] = (0.0, 0.0, 0.0)) -> list[float]:
    """Row-major 4x4 for a URDF-style xyz/rpy placement."""

    from description_pipeline.sources.solidworks.scene import _rotation_from_rpy

    rotation = _rotation_from_rpy((float(rpy[0]), float(rpy[1]), float(rpy[2])))
    return [
        rotation[0][0],
        rotation[0][1],
        rotation[0][2],
        float(xyz[0]),
        rotation[1][0],
        rotation[1][1],
        rotation[1][2],
        float(xyz[1]),
        rotation[2][0],
        rotation[2][1],
        rotation[2][2],
        float(xyz[2]),
        0.0,
        0.0,
        0.0,
        1.0,
    ]


def mass_payload(
    mass: float,
    com: Sequence[float] = (0.0, 0.0, 0.0),
    inertia: Sequence[Sequence[float]] | None = None,
) -> dict[str, Any]:
    tensor = [list(row) for row in (inertia or [[0.001, 0.0, 0.0], [0.0, 0.001, 0.0], [0.0, 0.0, 0.001]])]
    return {
        "mass": float(mass),
        "com": [float(value) for value in com],
        "inertia": tensor,
        "reference": {"used_api": "fixture", "product_convention": "solidworks_standard"},
    }


def compose_fixture_link_reads(
    components: Sequence[dict[str, Any]],
    effective: dict[str, dict[str, Any]],
    selections: dict[str, Sequence[str]],
    masses: dict[str, dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Fixture 'CAD' direct reads: the assembly-frame recombination of a selection.

    Used by the fixture backend (so capture and scene agree exactly) and by
    scene tests that need a valid link group record; divergence cases inject
    their own read values instead.  The math is convention-free on purpose:
    the native reader returns raw assembly numbers, and product-convention
    refusals belong to the part rows, not to this read.
    """

    from description_pipeline.sources.solidworks.scene import combine_mass_properties  # noqa: PLC0415

    def placed(transform: Sequence[float], com: Sequence[float]) -> list[float]:
        rows = [list(transform[index : index + 3]) for index in (0, 4, 8)]
        translation = [transform[3], transform[7], transform[11]]
        return [
            sum(rows[row][column] * float(com[column]) for column in range(3)) + translation[row] for row in range(3)
        ]

    def rotated(transform: Sequence[float], inertia: Sequence[Sequence[float]]) -> list[list[float]]:
        rows = [list(transform[index : index + 3]) for index in (0, 4, 8)]
        rotated_rows = [
            [sum(rows[row][k] * float(inertia[k][column]) for k in range(3)) for column in range(3)] for row in range(3)
        ]
        return [
            [sum(rotated_rows[row][k] * rows[column][k] for k in range(3)) for column in range(3)] for row in range(3)
        ]

    def entry_for(name: str, payload: dict[str, Any], transform: Sequence[float]) -> dict[str, Any]:
        scope = (payload.get("reference") or {}).get("scope") or "part_document"
        com = [float(value) for value in payload["com"]]
        inertia = [[float(value) for value in row] for row in payload["inertia"]]
        if scope == "assembly_component_group":
            return {"mass": float(payload["mass"]), "com": com, "inertia": inertia}
        return {
            "mass": float(payload["mass"]),
            "com": placed(transform, com),
            "inertia": rotated(transform, inertia),
        }

    by_name: dict[str, tuple[Any, dict[str, Any]]] = {}
    for entry in components:
        if isinstance(entry, dict):
            by_name[str(entry["name"])] = (entry.get("transform") or _identity(), entry["mass"])
        else:
            mass = getattr(entry, "mass", None)
            if mass is None and masses is not None:
                mass = masses[str(entry.name)]
            by_name[str(entry.name)] = (getattr(entry, "transform", None) or _identity(), mass)
    links: dict[str, dict[str, Any]] = {}
    for body, names in selections.items():
        entries = []
        claimed: set[str] = set()
        for name in names:
            parts = str(name).split("/")
            ancestors = ["/".join(parts[:index]) for index in range(1, len(parts) + 1)]
            provider = next((ancestor for ancestor in ancestors if ancestor in effective), None)
            if provider is not None:
                if provider in claimed:
                    continue
                claimed.add(provider)
                payload = dict(effective[provider], mass_source="cad")
                transform = by_name.get(provider, (_identity(), None))[0]
                entries.append(entry_for(provider, payload, transform))
            else:
                transform, mass = by_name[str(name)]
                entries.append(entry_for(str(name), mass, _matrix16(transform)))
        combined = combine_mass_properties(entries)
        mass = float(combined["mass"])
        com = [float(value) for value in combined["com"]]
        tensor = [[float(value) for value in row] for row in combined["inertia"]]
        # The second recording mirrors GetMomentOfInertia(1): the same tensor
        # about the assembly origin, related to the about-COM tensor by the
        # parallel-axis identity in the standard product convention.  The
        # capture-side convention proof reads exactly this pair.
        norm = sum(value * value for value in com)
        origin = [
            [
                tensor[row][column]
                + mass * ((norm - com[row] * com[column]) if row == column else -com[row] * com[column])
                for column in range(3)
            ]
            for row in range(3)
        ]
        read = {
            "mass_kg": mass,
            "volume_m3": None,
            "density_kg_m3": None,
            "com_m": com,
            "inertia_tensor_about_com": tensor,
            "inertia_values_about_origin_api": [origin[row][column] for row in range(3) for column in range(3)],
            "overrides": {
                "OverrideMass": False,
                "OverrideCenterOfMass": False,
                "OverrideMomentsOfInertia": False,
            },
        }
        links[str(body)] = {
            "selection": [str(name) for name in names],
            "reads": [read, copy.deepcopy(read)],
            "reproducibility": {"mass_delta_kg": 0.0, "com_delta_norm_m": 0.0, "tensor_delta_max_abs": 0.0},
        }
    return links


class FixtureCadBackend(CadBackend):
    """A CAD host with deterministic content and no SolidWorks."""

    name = "fixture"

    def __init__(
        self,
        document: Path,
        components: Sequence[dict[str, Any]],
        *,
        saved: bool = True,
        configurations: Sequence[str] = ("Default",),
        dependencies: Sequence[Path] | None = None,
        coordinate_systems: dict[str, Sequence[float]] | None = None,
    ) -> None:
        self.document = Path(document)
        self.components = [dict(entry) for entry in components]
        self.saved = saved
        self.configurations = list(configurations)
        self.open_documents: list[str] = []
        self.collected: list[str] = []
        self.exported: list[str] = []
        # Explicit neutral native datums for the shared two-body analytic fixture.
        datums = (
            {
                "base_datum": placement(),
                "arm_datum": placement((0.0, 0.0, 0.2)),
                "imu_datum": placement((0.01, 0.0, 0.03)),
            }
            if coordinate_systems is None
            else coordinate_systems
        )
        self.coordinate_system_matrices: dict[str, Sequence[float]] = {
            name: list(matrix) for name, matrix in datums.items()
        }
        self._dependencies = [Path(path) for path in (dependencies or [])]
        self.collected_scene_calls: list[str] = []
        self.scene_document_override: str | None = None
        self.originals_change_during_capture = False
        # The fixture models the one prepared capture process.
        self.session_pid = 100

    @contextmanager
    def session(self):
        """Provide the native lifetime boundary for the in-memory fixture."""

        yield self

    def health(self) -> dict[str, Any]:
        return {
            "ok": True,
            "backend": self.name,
            "sw_version": "fixture-2026",
            "active_document": str(self.document),
        }

    def list_documents(self) -> list[str]:
        # a real session has the assembly *and* its references open
        return [str(self.document), *(str(path) for path in self._dependencies)]

    def open_document(self, path: str) -> dict[str, Any]:
        order = getattr(self, "order", None)
        if order is None:
            order = self.order = []
        order.append("open_document")
        self.open_documents.append(str(path))
        return {
            "opened": Path(path).name,
            "path": str(path),
            "read_only": True,
            "errors": 0,
            "warnings": 0,
        }

    def document_state(self, path: str) -> dict[str, Any]:
        active = self.configurations[0]
        saved = bool(self.saved)
        return {
            "path": str(path),
            "title": Path(path).name,
            "save_flag": not saved,
            "saved": saved,
            "active_configuration": active,
            "configurations": list(self.configurations),
            "read_only": True,
            "lightweight": None,
            "unsupported": ["lightweight"],
        }

    def prepare_prepared_capture(
        self,
        source_root: str,
        assembly: str,
        configuration: str,
    ) -> dict[str, Any]:
        """Bind the fixture's prepared static inventory without touching CAD."""

        root = Path(source_root).resolve()
        document = Path(assembly).resolve()
        document.relative_to(root)
        if not root.is_dir() or not document.is_file():
            raise ValueError("prepared capture root or assembly is missing")
        self._prepared_capture_root = str(root)
        self._prepared_capture_document = str(document)
        self._prepared_capture_configuration = configuration
        self._capture_component_transforms = {
            str(entry["name"]): tuple(
                float(value)
                for value in (entry.get("transform") or _identity())
            )
            for entry in self.components
        }
        return {
            "mode": "prepared_read_only_copy",
            "source_root": str(root),
            "assembly": str(document),
            "requested_configuration": configuration,
            "session_role": "copy",
            "search_root": str(root),
            "sealed_documents": sorted(
                str(path)
                for path in root.rglob("*")
                if path.is_file() and path.suffix.lower() in {".sldasm", ".sldprt"}
            ),
            "com_started": False,
        }

    def source_scopes(self, components: Sequence[str]) -> dict[str, dict[str, Any]]:
        """Bind fixture occurrences to exact files in the prepared source copy."""

        root = Path(self._prepared_capture_root)
        original_root = Path(self.document).resolve().parent
        scopes = {}
        for component in components:
            entry = next(
                item
                for item in self.components
                if str(item["name"]) == str(component)
            )
            original = Path(entry.get("path") or self.document).resolve()
            relative = original.relative_to(original_root)
            copied = root / relative
            checksum = file_digest(copied)
            scope = fixture_source_scope(
                str(component),
                source_document_path=str(copied),
                source_copy_path=f"source/{relative.as_posix()}",
                source_document_sha256=checksum,
                referenced_configuration=str(
                    entry.get("configuration") or self.configurations[0]
                ),
            )
            scopes[str(component)] = scope
        return scopes

    def capture_axis_reference(self, reference):
        """Return deterministic raw axis evidence for the production projector."""

        selector = copy.deepcopy(reference)
        component = str(reference.get("component") or "")
        common = {
            "component": component,
            "selector": selector,
            "coordinate_frame": "component_local" if component else "assembly",
            "read_errors": {},
            "used_api": "fixture-raw-axis",
        }
        if reference.get("kind") == "reference_axis":
            return {
                **common,
                "kind": "reference_axis",
                "feature_name": str(reference.get("feature_name") or ""),
                "suppressed_raw": False,
                "feature_error_code_raw": 0,
                "endpoints_raw": [0.0, 0.0, 0.1, 0.0, 0.0, 1.1],
            }
        return {
            **common,
            "kind": "cylinder",
            "body_type": copy.deepcopy(reference.get("body_type")),
            "body_type_native": str(reference.get("body_type") or "solid"),
            "feature_name": copy.deepcopy(reference.get("feature_name")),
            "face_index": copy.deepcopy(reference.get("face_index")),
            "face_candidates": [
                {
                    "body_index": 0,
                    "face_index": copy.deepcopy(reference.get("face_index")),
                    "feature_name": copy.deepcopy(reference.get("feature_name")),
                    "is_cylinder": True,
                    "cylinder_params_raw": [0.0, 0.0, 0.1, 0.0, 0.0, 1.0, 0.006],
                    "persist_reference_b64": None,
                    "read_errors": {},
                }
            ],
        }

    def collect_prepared_scene(
        self,
        doc_path: str,
        coordinate_systems: Sequence[str],
        groups: Sequence[dict],
        on_activity=None,
        require_material: bool = True,
    ) -> dict[str, Any]:
        """Return the seven raw fact families consumed after CAD release."""

        order = getattr(self, "order", None)
        if order is None:
            order = self.order = []
        order.append("collect_prepared_scene")
        scene = self.collect_scene(
            doc_path,
            coordinate_systems,
            on_activity=on_activity,
            require_material=require_material,
        )
        component_entries = {
            str(entry["name"]): entry
            for entry in self.components
        }
        occurrences = []
        components = []
        for component in scene.components:
            entry = component_entries.get(component.name, {})
            occurrences.append(
                {
                    "name": component.name,
                    "path": component.path,
                    "transform_raw": solidworks_transform(component.transform),
                    "referenced_configuration": str(
                        entry.get("configuration") or self.configurations[0]
                    ),
                    "is_fixed": component.is_fixed,
                    "document_type": component.document_type,
                }
            )
            components.append(
                {
                    "name": component.name,
                    "path": component.path,
                    "is_fixed": component.is_fixed,
                    "document_type": component.document_type,
                }
            )
        raw_scene = {
            "document": scene.document,
            "configuration": self.configurations[0],
            "source_sha256": file_digest(Path(doc_path)),
            "document_state": self.document_state(doc_path),
            "occurrences": occurrences,
            "components": components,
            "coordinate_systems": {
                name: {
                    "key": name,
                    "name": name,
                    "owner": "",
                    "configuration": self.configurations[0],
                    "document": scene.document,
                    "array_data": solidworks_transform(matrix),
                    "read_errors": {},
                    "occurrence_transform_raw": None,
                }
                for name, matrix in scene.coordinate_systems.items()
            },
        }
        part_mass = {
            name: fixture_mass_fact(reading)
            for name, reading in scene.mass_properties.items()
        }
        assembly_reader = getattr(self, "assembly_mass_properties", None)
        assembly_mass = None
        if callable(assembly_reader):
            try:
                reading = assembly_reader(doc_path)
            except Exception:  # noqa: BLE001 - optional native probe
                reading = None
            if isinstance(reading, dict):
                assembly_mass = fixture_mass_fact(reading)
        context_reader = getattr(self, "assembly_component_mass_properties", None)
        component_mass = None
        if callable(context_reader):
            try:
                reading = context_reader(doc_path)
            except Exception:  # noqa: BLE001 - optional native probe
                reading = None
            if isinstance(reading, dict):
                component_mass = fixture_component_mass_fact(reading)
        selections = {
            str(group.get("name")): {
                "selection": [
                    str(name)
                    for name in group.get("components") or []
                ]
            }
            for group in groups
            if isinstance(group, dict) and group.get("name")
        }
        link_reader = getattr(self, "link_group_mass_properties", None)
        if callable(link_reader):
            link_mass = fixture_link_group_mass_fact(
                link_reader(doc_path, selections)
            )
        else:
            link_mass = {
                "assembly": scene.document,
                "configuration": self.configurations[0],
                "used_api": "IMassProperty2 (SelectedItems = component set)",
                "selection_rule": "top_most_providers+uncovered_leaves",
                "links": {},
            }
        scope_reader = getattr(self, "source_scopes", None)
        component_names = [component.name for component in scene.components]
        source_scopes = (
            scope_reader(component_names)
            if callable(scope_reader)
            else {
                name: copy.deepcopy(self._source_scope_overrides[name])
                for name in component_names
            }
        )
        return {
            "scene_raw": raw_scene,
            "part_mass_raw": part_mass,
            "assembly_mass_raw": assembly_mass,
            "component_mass_raw": component_mass,
            "link_group_mass_raw": link_mass,
            "source_scopes": source_scopes,
            "notes": copy.deepcopy(scene.notes),
        }

    def capture_prepared_geometry(
        self,
        source_scopes: Mapping[str, dict],
        on_activity=None,
    ) -> dict[str, dict]:
        """Return direct raw triangle facts for each prepared occurrence."""

        order = getattr(self, "order", None)
        if order is None:
            order = self.order = []
        order.append("capture_prepared_geometry")
        names = sorted(str(entry["name"]) for entry in self.components)
        if set(source_scopes) != set(names):
            raise ValueError("prepared geometry source scopes differ from its occurrences")
        entries = {}
        for index, name in enumerate(names, start=1):
            entry = next(item for item in self.components if str(item["name"]) == name)
            entries[name] = fixture_native_geometry(
                name,
                source_scopes[name],
                solidworks_transform(entry.get("transform") or _identity()),
            )
            if on_activity is not None:
                on_activity(
                    {
                        "phase": "capture",
                        "action": "geometry_facts",
                        "current_object": name,
                        "completed": index,
                        "total": len(names),
                    }
                )
        return entries

    def release(self) -> None:
        order = getattr(self, "order", None)
        if order is None:
            order = self.order = []
        order.append("release")
        self.open_documents.clear()

    def collect_scene(
        self,
        doc_path: str,
        coordinate_systems: Sequence[str],
        on_activity=None,
        require_material: bool = True,
    ) -> RawScene:
        order = getattr(self, "order", None)
        if order is None:
            order = self.order = []
        order.append("collect_scene")
        self.collected_scene_calls.append(str(doc_path))
        if self.originals_change_during_capture:
            Path(doc_path).write_bytes(b"fixture assembly edited during capture")
        scene = RawScene(document=str(self.scene_document_override or doc_path))
        for entry in self.components:
            scene.components.append(
                RawComponent(
                    name=str(entry["name"]),
                    path=str(entry.get("path") or self.document),
                    transform=_matrix16(entry.get("transform") or _identity()),
                    is_fixed=bool(entry.get("is_fixed", False)),
                    document_type="part",
                )
            )
        scene.coordinate_systems = {name: _matrix16(matrix) for name, matrix in self.coordinate_system_matrices.items()}
        scene.mass_properties = {str(entry["name"]): dict(entry["mass"]) for entry in self.components}
        scene.notes = {"backend": self.name}
        if on_activity is not None:
            total = len(self.components)
            for index, entry in enumerate(self.components, start=1):
                on_activity(
                    {
                        "phase": "capture",
                        "action": "instance_reads",
                        "current_object": str(entry["name"]),
                        "completed": index,
                        "total": total,
                    }
                )
        return scene

    def assembly_mass_properties(self, path):
        """Return one deterministic whole-assembly raw getter reading."""

        names = [str(entry["name"]) for entry in self.components]
        combined = compose_fixture_link_reads(
            self.components,
            {},
            {"assembly": names},
        )["assembly"]["reads"][0]
        return {
            "mass": combined["mass_kg"],
            "com": combined["com_m"],
            "inertia": combined["inertia_tensor_about_com"],
            "reference": {
                "used_api": "fixture-assembly-mass",
                "product_convention": "solidworks_standard",
                "reference_point": "center_of_mass",
                "axes": "assembly_document_axes",
                "use_system_units": True,
                "scope": "assembly_document",
                "overrides": {
                    "OverrideMass": False,
                    "OverrideCenterOfMass": False,
                    "OverrideMomentsOfInertia": False,
                },
            },
        }

    def assembly_component_mass_properties(self, path):
        """Return one assembly-context getter reading per fixture occurrence."""

        effective = getattr(self, "effective_readings", None) or {}
        instances = []
        for entry in self.components:
            name = str(entry["name"])
            reading = entry["mass"]
            reference = reading.get("reference") or {}
            overrides = copy.deepcopy(
                reference.get("overrides")
                or {
                    "OverrideMass": False,
                    "OverrideCenterOfMass": False,
                    "OverrideMomentsOfInertia": False,
                }
            )
            row = {
                "name": name,
                "parent": None,
                "depth": 0,
                "document": str(entry.get("path") or self.document),
                "document_type": "part",
                "context_mass_kg": float(reading["mass"]),
                "context_volume_m3": reference.get("volume_m3"),
                "overrides": overrides,
                "read_errors": {},
                "effective": None,
                "effective_error": None,
            }
            if name in effective:
                row["effective"] = copy.deepcopy(effective[name])
            instances.append(row)
        return {
            "assembly": str(path),
            "configuration": str(getattr(self, "configuration_name", "Default")),
            "instances": instances,
            "errors": [],
            "reference": {
                "method": "IMassProperty2 (SelectedItems = component instance)",
            },
        }

    def link_group_mass_properties(self, path, selections):
        """Direct link reads for the fixture: the assembly-frame recombination."""

        order = getattr(self, "order", None)
        if order is None:
            order = self.order = []
        order.append("link_group_read")
        normalized_selections = {
            str(body): (
                list(value.get("selection") or [])
                if isinstance(value, dict)
                else list(value)
            )
            for body, value in selections.items()
        }
        links = compose_fixture_link_reads(
            self.components,
            getattr(self, "effective_readings", None) or {},
            normalized_selections,
        )
        override = getattr(self, "link_group_selection_override", None) or {}
        for body, names in override.items():
            if str(body) in links:
                links[str(body)]["selection"] = [str(name) for name in names]
        return {
            "assembly": str(self.scene_document_override or path),
            "configuration": str(getattr(self, "configuration_name", "Default")),
            "used_api": "IMassProperty2 (SelectedItems = component set)",
            "selection_rule": "top_most_providers+uncovered_leaves",
            "links": links,
        }

    def environment(self) -> dict[str, Any]:
        order = getattr(self, "order", None)
        if order is None:
            order = self.order = []
        order.append("environment")
        return {
            "revision": "fixture-2026",
            "input_mode": "saved_documents",
            "build": "fixture-2026",
            "license": 0,
            "visible": True,
            "sessions": {
                "copy": {
                    "pid": self.session_pid,
                    "state": "active",
                    "role": "copy",
                }
            },
        }


def capture_prepared_fixture(
    config: dict[str, Any],
    destination: Path,
    *,
    backend: FixtureCadBackend,
    worker_version: str = "unknown",
    on_activity=None,
) -> dict[str, Any]:
    """Run the production prepared-Capture entry over one fixture package."""

    from description_pipeline.sources.solidworks.errors import BridgeError
    from description_pipeline.sources.solidworks.freeze import capture_prepared

    requests = capture_requests(config)
    document = Path(backend.document).resolve()
    package = document.parent
    native_files = {}
    outside = []
    for path in [document, *backend._dependencies]:
        resolved = path.resolve()
        if not resolved.is_file():
            continue
        try:
            relative = resolved.relative_to(package)
        except ValueError:
            outside.append(str(resolved))
            continue
        native_files[relative.as_posix()] = file_digest(resolved)
    if outside:
        raise BridgeError(
            "prepared_source_outside_root",
            "prepared capture inputs must remain under the prepared package",
            {"package": str(package), "outside": sorted(outside)},
            exit_code=3,
        )
    return capture_prepared(
        requests,
        Path(destination),
        package=package,
        prepared_root=package,
        native_files=native_files,
        backend=backend,
        worker_version=worker_version,
        on_activity=on_activity,
    )


def make_cad_tree(root: Path) -> Path:
    """Create a tiny on-disk 'CAD' tree: one assembly plus two parts."""

    root.mkdir(parents=True, exist_ok=True)
    assembly = root / "robot.SLDASM"
    assembly.write_bytes(b"fixture assembly")
    for index, name in enumerate(("base.SLDPRT", "arm.SLDPRT"), start=1):
        (root / name).write_bytes(f"fixture part {index}".encode())
    return assembly


def cleanup(path: Path) -> None:
    import shutil

    shutil.rmtree(path, ignore_errors=True)


def read_scene(snapshot: Path) -> dict:
    """Read the raw scene a snapshot carries (shared loader when available)."""

    from description_pipeline.sources.snapshot import load_scene

    return load_scene(Path(snapshot))


# ---------------------------------------------------------------------------
# Target-tree fixtures (IF-13): delivery roots with capture blocks and a bound
# manifest.  Shared by the migrated verification tests; block writes re-seal the
# manifest so semantic checks - not byte rebinding - are what the tests exercise.
# ---------------------------------------------------------------------------

CONTRACT_SCHEMA_VERSION = "solidworks-to-urdf.stage-contract/v1"


def saved_document_state(configuration: str = "Default") -> dict:
    """The native before/after document readings of one saved, active configuration."""

    observed = {
        "save_flag": False,
        "configuration": configuration,
        "read_only": True,
    }
    return {
        "before": copy.deepcopy(observed),
        "after": copy.deepcopy(observed),
        "used_api": (
            "IModelDoc2.GetSaveFlag/IConfiguration.Name"
        ),
    }


def default_source_record(
    *,
    hardware_id: str = "m3.0",
    configuration: str = "Default",
    main_assembly: str = "M3.0.SLDASM",
    revision: str | None = "r1",
) -> dict:
    from description_pipeline.io import digest as canonical_digest
    from description_pipeline.schemas import SOURCE_RECORD_SCHEMA

    identity = {
        "kind": "source-revision",
        "handoff_digest": canonical_digest({"fixture": main_assembly}),
        "main_assembly": main_assembly,
        "configuration": configuration,
        "hardware_id": hardware_id,
        "records": [],
    }
    return {
        "schema_version": SOURCE_RECORD_SCHEMA,
        "source_revision_id": canonical_digest(identity),
        "identity": identity,
        "selection": {
            "main_assembly": main_assembly,
            "configuration": configuration,
            "hardware_id": hardware_id,
            "hardware_source": "cad",
        },
        "metadata": {
            "revision": revision,
            "owner": "fixture",
            "control": {"system": "handoff", "reference": "fixture"},
            "change_summary": "fixture",
            "change_summary_source": "submission",
        },
    }


def seal_delivery(root: Path) -> dict:
    from description_pipeline.delivery import write_delivery_manifest
    from description_pipeline.interface_records import check_policy_digest
    from description_pipeline.schemas import CHECK_POLICY_SCHEMA
    from description_pipeline.stages import CONTRACT_SHA256

    return write_delivery_manifest(
        Path(root),
        contract={"schema_version": CONTRACT_SCHEMA_VERSION, "digest": CONTRACT_SHA256},
        policy={"schema_version": CHECK_POLICY_SCHEMA, "digest": check_policy_digest()},
    )


def write_target_delivery(
    root: Path,
    *,
    capture: dict,
    discovery: dict | None = None,
    source: dict | None = None,
    handoff: dict | None = None,
    runtime: dict | None = None,
    tool: dict | None = None,
    originals: dict[str, str] | None = None,
    records: dict[str, str] | None = None,
    request: dict | None = None,
) -> Path:
    """Create a minimal target-tree delivery holding the given capture blocks."""

    from description_pipeline.io import write_json
    from description_pipeline.schemas import TOOL_SCHEMA

    root = Path(root)
    for name in (
        "input",
        "input/cad",
        "input/records",
        "evidence",
        "evidence/raw",
        "model",
        "urdf",
        "meshes",
        "reports",
    ):
        (root / name).mkdir(parents=True, exist_ok=True)
    source = source or default_source_record()
    selection = source["selection"]
    if handoff is None:
        handoff = {
            "package_files": {},
            "input_receipt": {"inventory": {}},
            "main_assembly": selection["main_assembly"],
            "configuration": selection["configuration"],
        }
    write_json(root / "input/handoff.json", handoff)
    write_json(root / "input/source.json", source)
    if request is None:
        payload_handoff = (discovery or {}).get("handoff_sha256") if isinstance(discovery, dict) else None
        raw_selection = (
            ((discovery or {}).get("raw") or {}).get("source_selection")
            if isinstance(discovery, dict)
            else None
        )
        upload_handoff = raw_selection.get("upload_sha256") if isinstance(raw_selection, dict) else None
        request = {
            "schema_version": "solidworks-to-urdf.request/v2",
            "purpose": "engineering",
            "request_id": "00000000-0000-4000-8000-000000000000",
            "target_hardware": str(selection["hardware_id"]),
            "change_summary": str((source.get("metadata") or {}).get("change_summary") or "fixture"),
            "main_assembly": str(selection["main_assembly"]),
            "handoff_digest": (
                upload_handoff
                if isinstance(upload_handoff, str)
                else payload_handoff if isinstance(payload_handoff, str) else "0" * 64
            ),
            "platform_config_digest": "0" * 64,
        }
    write_json(root / "input/request.json", request)
    for name, text in (originals or {}).items():
        target = root / "input/cad" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    for name, text in (records or {}).items():
        relative = name if name.replace("\\", "/").startswith("records/") else f"records/{name}"
        target = root / "input" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    write_json(root / "evidence/discovery.json", discovery or {})
    capture = dict(capture)
    if "material_provenance" not in capture:
        # The sealed capture block is the material authority the verifier reads
        # (the retired evidence/raw/source_config.json is no longer a carrier).
        capture["material_provenance"] = {
            "schema_version": "solidworks-to-urdf.material-provenance/v1",
            "assembly": str((root / "input/cad" / str(selection["main_assembly"])).resolve()),
            "configuration": str(selection["configuration"]),
            "measured_source": "cad_api",
            "warnings": [],
        }
    write_json(root / "evidence/capture.json", capture)
    write_json(root / "evidence/native-runtime.json", runtime or {})
    write_json(root / "model/robot.json", {})
    (root / "urdf/robot.urdf").write_text("<robot name='m3.0'/>\n", encoding="utf-8")
    (root / "meshes/part.stl").write_text("solid\n", encoding="utf-8")
    write_json(root / "tool.json", tool or {"schema_version": TOOL_SCHEMA})
    write_json(root / "reports/quality.json", {})
    seal_delivery(root)
    return root


def capture_file_for(root: Path) -> Path:
    root = Path(root)
    return (root.parent if root.name == "evidence" else root) / "evidence/capture.json"


def load_capture_block(root: Path, name: str):
    from description_pipeline.io import read_data

    return read_data(capture_file_for(root))[name]


def store_capture_block(root: Path, name: str, value) -> None:
    """Rewrite one capture block and re-seal the manifest so bytes stay bound."""

    from description_pipeline.io import read_data, write_json

    path = capture_file_for(root)
    capture = read_data(path)
    capture[name] = value
    write_json(path, capture)
    _refresh_snapshot_manifest(path.parent.parent)
    seal_delivery(path.parent.parent)


def delete_capture_block(root: Path, name: str) -> None:
    """Remove one capture block and re-seal the manifest."""

    from description_pipeline.io import read_data, write_json

    path = capture_file_for(root)
    capture = read_data(path)
    capture.pop(name, None)
    write_json(path, capture)
    _refresh_snapshot_manifest(path.parent.parent)
    seal_delivery(path.parent.parent)


def _refresh_snapshot_manifest(delivery_root: Path) -> None:
    """Rebuild the snapshot manifest inventory when the tree carries one.

    Trees that model the frozen delivery keep ``evidence/manifest.json`` (the
    capture identity and the file inventory the verifier re-checks); rewriting
    a capture block must keep that inventory true.
    """

    from description_pipeline.io import read_data
    from description_pipeline.sources.snapshot import write_manifest

    manifest_path = Path(delivery_root) / "evidence/manifest.json"
    if not manifest_path.is_file():
        return
    current = read_data(manifest_path)
    write_manifest(
        Path(delivery_root) / "evidence",
        kind=current["kind"],
        identity=current["identity"],
        evidence_class=current["evidence_class"],
    )


# ---------------------------------------------------------------------------
# Native discovery fixtures (IF-13): fold a prepared native package into one
# target-tree delivery.  This is the test-side preview of the producer-side
# conversion (worker A's a2): the record gains the frozen file inventory and
# robot carrier, the originals move under ``input/cad`` and the embedded
# records under ``input/records``.
# ---------------------------------------------------------------------------


def frozen_discovery_payload(
    record: dict,
    *,
    files: dict[str, str],
) -> dict:
    """The IF-13 discovery record: the native payload plus the frozen additions.

    ``files`` maps frozen-source-relative native paths to their digests; the
    record carries them as ``files[]`` plus the canonical
    ``native_inventory_sha256`` and the top-level ``robot_name`` resolved from
    the recorded identity (authored name, then its technical label).
    """

    from description_pipeline.io import digest

    payload = copy.deepcopy(record)
    rows = [{"path": path, "sha256": checksum} for path, checksum in sorted(files.items())]
    payload["files"] = rows
    payload["native_inventory_sha256"] = digest(rows)
    identity = payload.get("identity") if isinstance(payload.get("identity"), dict) else {}
    payload["robot_name"] = identity.get("robot_name") or identity.get("robot_name_technical") or "robot"
    return payload


def native_discovery_delivery(root: Path, prepared: Path, *, record: dict | None = None) -> Path:
    """Materialize one target-tree delivery from a prepared native package.

    The CAD originals move to ``input/cad`` at their frozen-source-relative
    paths; the record becomes ``evidence/discovery.json`` (frozen additions
    applied); the embedded records move under ``input/records`` keeping their
    recorded ``package_file`` names; ``input/source.json`` is the prepared
    package's archived content identity verbatim (the fixture record only
    backs packages that could not name one).  Returns the delivery root.
    """

    import hashlib

    from description_pipeline.io import read_data

    prepared = Path(prepared)
    if record is None:
        record = read_data(prepared / "discovery/native-discovery.json")
    raw = record.get("raw") or {}
    raw_files = raw.get("files") if isinstance(raw.get("files"), dict) else {}
    originals: dict[str, str] = {}
    files: dict[str, str] = {}
    for relative in sorted(raw_files):
        data = (prepared / str(relative)).read_bytes()
        files[str(relative)] = hashlib.sha256(data).hexdigest()
        originals[str(relative)] = data.decode("utf-8")
    records: dict[str, str] = {}
    references = list(record.get("records") or []) + ([record["budget"]] if record.get("budget") else [])
    for entry in references:
        if not isinstance(entry, dict):
            continue
        package_file = str(entry.get("package_file") or "")
        source = prepared / package_file
        if package_file and source.is_file():
            records[package_file] = source.read_text(encoding="utf-8")
    payload = frozen_discovery_payload(record, files=files)
    prepared_source = prepared / "source.json"
    source_record = read_data(prepared_source) if prepared_source.is_file() else source_record_for_robot(record)
    return write_target_delivery(
        root,
        capture={},
        discovery=payload,
        source=source_record,
        originals=originals,
        records=records,
    )


def source_record_for_robot(record: dict) -> dict:
    """The archived input/source.json identity for one prepared native package."""

    identity = record.get("identity") if isinstance(record.get("identity"), dict) else {}
    revision = identity.get("revision")
    return default_source_record(
        hardware_id=str(identity.get("hardware_id") or "fixture"),
        configuration=str(identity.get("delivery_configuration") or "Default"),
        main_assembly=str(identity.get("main_assembly") or "robot.SLDASM"),
        revision=revision if isinstance(revision, str) and revision else None,
    )


def capture_requests(config: dict) -> dict:
    """Translate a fixture config's carriers into the minimal native request sets.

    The product consumes ``groups``/``axis_requests`` only; fixtures keep their
    bodies/joints/frames authoring structures and translate at the call
    boundary (interface frames come from the native ``TCP_``/``SCS_`` datums).
    No product code carries a compatibility fallback.
    """

    cfg = dict(config)
    for retired in (
        "allowed_roots",
        "closure",
        "document_suffixes",
        "require_saved",
    ):
        cfg.pop(retired, None)
    bodies = cfg.pop("bodies", None)
    if bodies is not None and "groups" not in cfg:
        cfg["groups"] = [
            {
                "name": str(body.get("name") or body.get("id") or ""),
                "components": sorted(str(member) for member in (body.get("components") or [])),
            }
            for body in bodies
        ]
    joints = cfg.pop("joints", None)
    if joints is not None and "axis_requests" not in cfg:
        cfg["axis_requests"] = [
            {"name": str(joint.get("name") or joint.get("id") or ""), "axis_reference": joint["axis_reference"]}
            for joint in joints
            if isinstance(joint.get("axis_reference"), dict)
        ]
    cfg.pop("frames", None)
    return cfg


def _record_keys_for(
    record: dict,
    record_keys: dict[str, str | Sequence[str]] | None,
    relative: str,
) -> list[str]:
    """Every controlled key referenced by the raw claims for one fixture record."""

    keys: list[str] = []
    properties = record.get("properties") if isinstance(record.get("properties"), dict) else {}

    def visit(value) -> None:
        if isinstance(value, dict):
            for item in value.values():
                visit(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                visit(item)
        else:
            if not isinstance(value, str) or "#" not in value:
                return
            referenced, _, key = value.partition("#")
            if referenced.replace("\\", "/") == relative and key:
                keys.append(key)

    visit(properties)
    configured = (record_keys or {}).get(relative)
    if isinstance(configured, str):
        keys.append(configured)
    elif configured:
        keys.extend(str(key) for key in configured)
    return list(dict.fromkeys(keys))


def compose_delivery_inputs(
    delivery: Path,
    *,
    main_assembly: str,
    configuration: str,
    record: dict,
    cad_files: dict[str, Path],
    records: dict[str, str] | None = None,
    record_keys: dict[str, str | Sequence[str]] | None = None,
) -> None:
    """Compose the sealed inputs Linux consumers rebuild from (fixture plumbing).

    Copies the genuine CAD bytes into ``input/cad/<relative>``, writes the
    sealed discovery payload (with its raw native record) at
    ``evidence/discovery.json`` (including optional controlled-record files at
    ``input/records/<name>`` carrying their payload entries). Synthetic
    consumer-fixture inputs only: it must never feed the unchanged
    canonical/E2E producers, which produce their own evidence.  Existing
    fixture blocks (warnings and readouts included) are preserved, and the
    provenance identity binds to the record's captured identity rather than a
    relocated fixture path.  No consumer output is injected.
    """

    import shutil

    from description_pipeline.io import file_digest, write_json
    from description_pipeline.sources.solidworks.discovery import DISCOVERY_SCHEMA

    write_fixture_request(Path(delivery), main_assembly=main_assembly)
    delivery = Path(delivery)
    evidence = delivery / "evidence"
    evidence.mkdir(parents=True, exist_ok=True)
    native_files: dict[str, str] = {}
    for relative, source in cad_files.items():
        target = delivery / "input" / "cad" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        native_files[str(relative)] = file_digest(target)
    identity = record.get("identity") if isinstance(record.get("identity"), dict) else {}
    import json
    import hashlib as _hashlib

    payload_records: list[dict] = []
    for name, text in (records or {}).items():
        archive_name = "records/" + Path(name).name
        target = delivery / "input" / archive_name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        common = {
            "file": str(name),
            "package_file": archive_name,
            "sha256": _hashlib.sha256(text.encode("utf-8")).hexdigest(),
        }
        keys = _record_keys_for(record, record_keys, str(name))
        if keys:
            payload_records.extend(
                {
                    **common,
                    "key": str(key),
                    "reference": f"{name}#{key}",
                    "anchor": json.dumps(str(key)),
                }
                for key in keys
            )
        else:
            payload_records.append(common)
    write_json(
        evidence / "discovery.json",
        {
            "schema_version": DISCOVERY_SCHEMA,
            "run_id": str(record.get("run_id") or "fixture-run"),
            "derivation": {
                "main_assembly": main_assembly,
                "configuration": configuration,
                "record_roots": [],
            },
            "identity": {
                "main_assembly": main_assembly,
                "delivery_configuration": configuration,
                "robot_name": str(identity.get("robot_name") or "robot"),
            },
            "records": payload_records,
            "raw": record,
            "native_files": dict(sorted(native_files.items())),
        },
    )
# ---------------------------------------------------------------------------
# Fixture plumbing: the small shared producer for consumer-fixture inputs.
#
# The scenario facts - which occurrences, datums, mates and masses a module
# exercises - stay with the module that builds them; these builders own the
# record boilerplate and the sealing infrastructure only.  Synthetic
# test-only inputs: the unchanged canonical/E2E producers build their own
# evidence and never use any of this.  No consumer output is injected.
# ---------------------------------------------------------------------------


def _native_float(value: float | int) -> float:
    """Canonical record float: +0.0 zeros, all other values unchanged (fixture plumbing)."""

    return float(value) + 0.0


def native_component(
    name: str, *, document: str, transform: Sequence[float] | None = None, fixed: bool = True
) -> dict:
    """One recorded native occurrence entry (fixture plumbing).

    ``document`` is the scenario's own binding of the occurrence to its
    original part document; it is never derived from the occurrence name.
    """

    return {
        "name2": name,
        "lightweight": False,
        "suppression_state": 2,
        "instance_id": name,
        "document": str(document),
        "configuration": "Default",
        "fixed": fixed,
        "suppressed": False,
        "transform": [_native_float(value) for value in (placement() if transform is None else transform)],
    }


def native_datum(name: str, owner: str, transform: Sequence[float]) -> dict:
    """One recorded native coordinate system (fixture plumbing)."""

    return {"name": name, "owner": owner, "array": [_native_float(value) for value in transform]}


def native_hinge_mates(
    left: str,
    right: str,
    *,
    left_point: Sequence[float],
    right_point: Sequence[float],
    radius: float,
    name: str = "hinge_joint",
) -> list[dict]:
    """The solved concentric+coincident mate pair one movable hinge requires.

    Both axes point along +z in their component frames; each scenario keeps
    its own solved points and shaft radius.  A hinge axis must pass through
    the child body's datum origin (the joint frame check requires it).
    """

    def cylinder(component: str, feature: str, face_index: int, point: Sequence[float]) -> dict:
        return {
            "component": component,
            "feature": feature,
            "face_index": face_index,
            "cylinder": {
                "point": [float(value) for value in point],
                "direction": [0.0, 0.0, 1.0],
                "radius": float(radius),
            },
        }

    def plane(component: str, feature: str, point: Sequence[float], normal: float) -> dict:
        return {
            "component": component,
            "feature": feature,
            "plane": {"point": [float(value) for value in point], "normal": [0.0, 0.0, normal]},
        }

    return [
        {
            "name": f"{name}__coaxial",
            "type": "concentric",
            "suppressed": False,
            "error_code": 0,
            "scope": "",
            "limits": None,
            "entities": [cylinder(left, "Cyl1", 3, left_point), cylinder(right, "Cyl2", 7, right_point)],
        },
        {
            "name": f"{name}__locate",
            "type": "coincident",
            "suppressed": False,
            "error_code": 0,
            "scope": "",
            "limits": None,
            "entities": [plane(left, "Plane1", left_point, 1.0), plane(right, "Plane2", right_point, -1.0)],
        },
    ]


def native_limit_observation(
    type_index: int,
    *,
    nominal: float,
    lower: float,
    upper: float,
) -> dict:
    """One complete raw native-limit record for a limit angle/distance mate."""

    if type_index == 6:
        unit = "rad"
        interface = "IAngleMateFeatureData"
        iid = "{EF4303D7-B4FD-46E7-AD34-D942C420E096}"
        nominative, minimum, maximum = "Angle", "MinimumAngle", "MaximumAngle"
    elif type_index == 5:
        unit = "m"
        interface = "IDistanceMateFeatureData"
        iid = "{943EED8D-E13B-4459-BD59-EBCC3F60B915}"
        nominative, minimum, maximum = "Distance", "MinimumDistance", "MaximumDistance"
    else:
        raise ValueError("unsupported native limit type")
    return {
        "type_index": type_index,
        "definition_interface": interface,
        "definition_interface_iid": iid,
        "nominal_dimension": {"value": float(nominal), "api": f"{interface}.{nominative}", "unit": unit},
        "minimum_variation": {
            "value": float(lower) - nominal,
            "api": "IMate2.MinimumVariation",
            "unit": unit,
        },
        "maximum_variation": {
            "value": float(upper) - nominal,
            "api": "IMate2.MaximumVariation",
            "unit": unit,
        },
        "minimum_limit": {"value": float(lower), "api": f"{interface}.{minimum}", "unit": unit},
        "maximum_limit": {"value": float(upper), "api": f"{interface}.{maximum}", "unit": unit},
    }


def native_limit_mate(
    left: str,
    right: str,
    *,
    lower: float = -1.0,
    upper: float = 1.0,
    name: str = "hinge_joint",
    point: Sequence[float] = (0.0, 0.0, 0.0),
) -> dict:
    """One enabled angular limit mate beside a hinge's solved constraint pair."""

    native_limit = native_limit_observation(6, nominal=0.0, lower=lower, upper=upper)
    return {
        "name": f"{name}__limit",
        "type": "limitangle",
        "suppressed": False,
        "error_code": 0,
        "scope": "",
        "limits": {"lower": float(lower), "upper": float(upper), "unit": "rad"},
        "is_advanced_mate": True,
        "is_advanced_mate_error": None,
        "is_advanced_mate_api": "IAngleMateFeatureData.IsAdvancedMate",
        "native_limit": native_limit,
        "entities": [
            {
                "entity_index": 0,
                "component": str(left),
                "reference_configuration": "Default",
                "feature": "LimitPlane1",
                "face_index": None,
                "mate_entity_reference": {"error": None, "geometry": None},
                "plane": {"point": [float(value) for value in point], "normal": [0.0, 0.0, 1.0]},
            },
            {
                "entity_index": 1,
                "component": str(right),
                "reference_configuration": "Default",
                "feature": "LimitPlane2",
                "face_index": None,
                "mate_entity_reference": {"error": None, "geometry": None},
                "plane": {"point": [float(value) for value in point], "normal": [0.0, 0.0, 1.0]},
            },
        ],
    }


def native_lock(members: Sequence[str], *, name: str = "board_rigid") -> dict:
    """One solved lock mate merging the occurrences into a rigid body (fixture plumbing)."""

    return {
        "name": name,
        "type": "lock",
        "suppressed": False,
        "error_code": 0,
        "scope": "",
        "limits": None,
        "entities": [{"component": str(member)} for member in members],
    }


def native_joint_claims(*, record_file: str = "joints/hinge.json", mate: str = "hinge_joint__coaxial") -> dict:
    """The dp.joint.* claims a hinge's name and limits/drive travel through (fixture plumbing)."""

    return {
        mate: {
            "dp.joint.axis_sign": "+1",
            "dp.joint.limits_record": f"{record_file}#limits",
            "dp.joint.drive_record": f"{record_file}#drive",
        }
    }


def native_joint_record(*, lower: float = -1.0, upper: float = 1.0, effort: float = 2.0, velocity: float = 3.0) -> str:
    """The controlled joint record text the claims resolve against (fixture plumbing)."""

    import json

    return json.dumps({"limits": {"lower": lower, "upper": upper}, "drive": {"effort": effort, "velocity": velocity}})


def native_budget_record(record: dict, *, tolerance: float = 0.1) -> str:
    """The controlled design-budget text the record's budget property binds (fixture plumbing)."""

    import json

    mass = float(record["assembly_mass_kg"])
    return json.dumps(
        {"robot": {"expected_mass_kg": [mass - tolerance, mass + tolerance], "expected_extent_m": [0.2, 0.8]}}
    )


def native_files(cad_root: Path, names: Sequence[str]) -> dict[str, str]:
    """SHA-256 of the genuine CAD fixture bytes, keyed by relative name (fixture plumbing)."""

    import hashlib

    return {name: hashlib.sha256((Path(cad_root) / name).read_bytes()).hexdigest() for name in names}


def native_record(
    *,
    hardware_id: str,
    change_summary: str,
    control_reference: str,
    components: Sequence[dict],
    datums: Sequence[dict],
    masses: Sequence[dict],
    mates: Sequence[dict] = (),
    joint_claims: dict | None = None,
    budget: bool = True,
    assembly_mass_kg: float | None = None,
    assembly_extent_m: float = 0.5,
    files: dict[str, str] | None = None,
) -> dict:
    """The sealed native discovery record the consumer fixtures rebuild from.

    Owns the record boilerplate only: the scenario facts (which occurrences,
    datums, mates and masses) stay with the module that builds them.  A
    ``joint_claims`` block names the controlled limits/drive records a hinge
    travels through; ``budget`` binds the controlled design-budget record;
    when frozen ``files`` are given, the record carries their inventory and
    the sealed selection over the ``robot.SLDASM`` fixture tree.
    """

    from description_pipeline.sources.solidworks.discovery import CONTRACT, DISCOVERY_SCHEMA

    from .selection_support import sealed_selection

    properties: dict[str, Any] = {"components": {}, "mates": dict(joint_claims or {})}
    if budget:
        properties["document"] = {"dp.design_budget_record": "budget.json#robot"}
    payload: dict[str, Any] = {
        "schema_version": DISCOVERY_SCHEMA,
        "contract": CONTRACT,
        "namespace": "dp",
        "identity": {
            "hardware_id": hardware_id,
            "revision": "r1",
            "owner": "mechanical-team",
            "change_summary": change_summary,
            "control": {"system": "handoff", "reference": control_reference},
            "delivery_configuration": "Default",
            "main_assembly": "robot.SLDASM",
            "robot_name": "robot",
        },
        "saved_configuration": "Default",
        "document_state": saved_document_state(),
        "components": [dict(item) for item in components],
        "mates": [dict(item) for item in mates],
        "datums": [dict(item) for item in datums],
        "assembly_mass_kg": float(
            assembly_mass_kg if assembly_mass_kg is not None else sum(float(row["mass_kg"]) for row in masses)
        ),
        "assembly_extent_m": float(assembly_extent_m),
        "properties": properties,
    }
    if files is not None:
        payload["files"] = dict(files)
        parts = [name for name in files if name != "robot.SLDASM"]
        payload["source_selection"] = sealed_selection(
            dict(files),
            "robot.SLDASM",
            edges={"robot.SLDASM": parts, **{name: [] for name in parts}},
        )
    return payload


def seal_scene_inputs(
    destination: Path,
    *,
    cad_root: Path,
    cad_names: Sequence[str],
    record: dict,
    controlled: dict[str, str] | None = None,
    record_keys: dict[str, str | Sequence[str]] | None = None,
) -> None:
    """Fixture plumbing: seal the consumer inputs a Linux scene rebuild reads.

    Copies the genuine CAD bytes into ``input/cad`` and writes the sealed
    discovery payload (the raw native record and any controlled records) into
    a frozen snapshot, then re-seals the manifest so the added members stay
    bound by the verified inventory.
    """

    import json

    identity = record.get("identity") if isinstance(record.get("identity"), dict) else {}
    compose_delivery_inputs(
        destination,
        main_assembly=str(identity.get("main_assembly") or "robot.SLDASM"),
        configuration=str(identity.get("delivery_configuration") or "Default"),
        record=record,
        cad_files={name: Path(cad_root) / name for name in cad_names},
        records=controlled,
        record_keys=record_keys,
    )
    runtime = Path(destination) / "native-runtime.json"
    environment = Path(destination) / "evidence/environment.json"
    if not runtime.is_file():
        runtime.write_bytes(environment.read_bytes())
    from packaging.utils import canonicalize_name

    from description_pipeline import __version__
    from description_pipeline.delivery import PIPELINE_ID
    from description_pipeline.io import read_data, write_json
    from description_pipeline.runtime import RUNTIME_VERSIONS, required_packages

    runtime_record = read_data(runtime)
    native_tool = {
        "schema_version": "solidworks-to-urdf.tool/v1",
        "pipeline_id": PIPELINE_ID,
        "version": __version__,
        "source_sha256": "f" * 64,
        "release": None,
        "runtime": {
            "role": "native",
            "system": "Windows",
            "python": "3.12.14",
            "machine": "AMD64",
            "packages": {
                canonicalize_name(name): RUNTIME_VERSIONS[name]
                for name in required_packages("native")
            },
        },
    }
    runtime_record["toolchain"] = native_tool
    runtime_record["worker_version"] = str(native_tool["version"])
    write_json(runtime, runtime_record)
    write_json(Path(destination) / "reports/native-tool.json", native_tool)
    from description_pipeline.sources.snapshot import write_manifest

    manifest = json.loads((Path(destination) / "manifest.json").read_text(encoding="utf-8"))
    write_manifest(
        Path(destination),
        kind=manifest["kind"],
        identity=manifest["identity"],
        evidence_class=manifest["evidence_class"],
    )


def stage_discovery_inputs(
    root: Path,
    *,
    record: dict,
    controlled: dict[str, str] | None = None,
    record_keys: dict[str, str | Sequence[str]] | None = None,
) -> None:
    """Fixture plumbing: stage the discovery payload for hand-built producer roots.

    The controlled records land under ``input/records``, the payload binds
    them, and the capture's base occurrence is stubbed under ``input/cad`` -
    the raw-evidence sibling of ``compose_delivery_inputs`` for the fixture
    roots that carry no snapshot manifest (closure and its accounting
    consumer).
    """

    import hashlib
    import json

    from description_pipeline.io import write_json

    root = Path(root)
    payload_records = []
    for relative, text in (controlled or {}).items():
        archive_name = "records/" + Path(relative).name
        path = root / "input" / archive_name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        common = {
            "file": relative,
            "package_file": archive_name,
            "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        }
        keys = _record_keys_for(record, record_keys, str(relative))
        if keys:
            payload_records.extend(
                {
                    **common,
                    "key": str(key),
                    "reference": f"{relative}#{key}",
                    "anchor": json.dumps(str(key)),
                }
                for key in keys
            )
        else:
            payload_records.append(common)
    write_json(
        root / "evidence" / "discovery.json",
        {
            "schema_version": "solidworks-to-urdf.native-discovery/v1",
            "run_id": "fixture-run",
            "derivation": {
                "main_assembly": "robot.SLDASM",
                "configuration": "Default",
                "record_roots": [],
            },
            "identity": {
                "main_assembly": "robot.SLDASM",
                "delivery_configuration": "Default",
                "robot_name": "robot",
            },
            "records": payload_records,
            "raw": record,
            "native_files": {},
        },
    )
    write_fixture_request(root, main_assembly="robot.SLDASM")
    assembly = root / "input" / "cad" / "robot.SLDASM"
    assembly.parent.mkdir(parents=True, exist_ok=True)
    assembly.write_bytes(b"fixture assembly")


def write_fixture_request(delivery: Path, *, main_assembly: str) -> None:
    """Fixture plumbing: the frozen v2 request record every fixture delivery carries.

    Synthetic fixture data only (``purpose=engineering``); the persisted request
    remains the single purpose authority every consumer reads.  An existing
    record is left untouched so fixtures may author their own.
    """

    from description_pipeline.request_evidence import build_request_evidence, request_evidence_bytes

    target = Path(delivery) / "input" / "request.json"
    if target.exists():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(
        request_evidence_bytes(
            build_request_evidence(
                purpose="engineering",
                request_id="fixture-run",
                target_hardware="fixture",
                change_summary="fixture frozen request",
                main_assembly=main_assembly,
                handoff_digest="0" * 64,
                platform_config_digest="0" * 64,
            )
        )
    )
