"""Fixtures for source-adapter tests; the backend never touches live CAD."""

from __future__ import annotations

import struct
from pathlib import Path
from typing import Any, cast
from collections.abc import Sequence

from description_pipeline.io import file_digest
from description_pipeline.sources.solidworks.protocol import CadBackend, Matrix, RawComponent, RawScene


def write_binary_stl(path: Path, triangles: Sequence[Sequence[Sequence[float]]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    header = b"fixture stl".ljust(80, b" ")
    payload = bytearray()
    for triangle in triangles:
        first, second, third = triangle
        payload += struct.pack("<3f", 0.0, 0.0, 1.0)
        for vertex in (first, second, third):
            payload += struct.pack("<3f", float(vertex[0]), float(vertex[1]), float(vertex[2]))
        payload += struct.pack("<H", 0)
    with open(path, "wb") as handle:
        handle.write(header)
        handle.write(struct.pack("<I", len(triangles)))
        handle.write(bytes(payload))
    return len(triangles)


def cube_stl(path: Path, half: float = 0.02) -> int:
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
    return write_binary_stl(path, [(corners[a], corners[b], corners[c]) for a, b, c in faces])


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
        # failure-injection knobs for the closure contract
        self.pack_drop_files: set[str] = set()
        self.copy_unresolved = 0
        self.copy_extra_components = 0
        self.resolve_returns_empty = False
        self.copy_configuration: str | None = None
        self.copy_referenced_configuration_change = False
        self.copy_missing_files = False
        self.copy_outside_documents = False
        self.copy_duplicate_instances = False
        self.copy_rewrites_bytes = False
        self.resolve_only_existing = False
        self.collected_scene_calls: list[str] = []
        self.scene_document_override: str | None = None
        self.originals_change_during_capture = False
        self.state_calls: list[str] = []
        self.unsaved_paths: set[str] = set()
        self.drift_configuration_after_reading = False
        self._drifted = False
        self._original_paths = {str(self.document), *(str(path) for path in self._dependencies)}

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
        self.open_documents.append(str(path))
        return {"opened": Path(path).name, "path": str(path), "read_only": True}

    def close_document(self, name: str, confirm: bool = False) -> dict[str, Any]:
        return {"closed": name}

    def document_state(self, path: str) -> dict[str, Any]:
        self.state_calls.append(str(path))
        active = self.configurations[0]
        if self._drifted and str(path) in self._original_paths:
            # an edit made in the session during the capture: the file on disk is
            # untouched, only the in-memory state moved
            active = "Other"
        return {
            "path": str(path),
            "title": Path(path).name,
            "saved": self.saved and str(path) not in self.unsaved_paths,
            "active_configuration": active,
            "configurations": list(self.configurations),
            "read_only": True,
            "lightweight": False,
        }

    def save_flag_documents(self) -> list[str]:
        """The working-tree documents the fake reports as needing a save."""

        if not self.saved:
            return sorted(
                {
                    str(self.document),
                    *(str(path) for path in self._dependencies),
                    *(str(path) for path in self.unsaved_paths),
                }
            )
        return sorted(str(path) for path in self.unsaved_paths)

    def list_dependencies(self, path: str) -> list[dict[str, Any]]:
        return [
            {"path": str(item), "exists": Path(item).is_file(), "type": Path(item).suffix.lower()}
            for item in self._dependencies
        ]

    def collect_dependencies(self, path: str, destination_dir: str) -> dict[str, Any]:
        target = Path(destination_dir)
        target.mkdir(parents=True, exist_ok=True)
        self.collected = []
        for item in [self.document, *self._dependencies]:
            source = Path(item)
            if not source.is_file() or source.name in self.pack_drop_files:
                continue
            destination = target / source.name
            payload = source.read_bytes()
            if self.copy_rewrites_bytes and source.resolve() == self.document.resolve():
                # an assembly carries its references, so Native reference collection legitimately
                # rewrites it: the copy is not byte-identical to the source
                payload = payload + b" (rewritten references)"
            destination.write_bytes(payload)
            self.collected.append(str(destination))
        top_level = target / Path(self.document).name
        if self.copy_missing_files:
            # the collector reported the document but its bytes never landed
            for path in list(self.collected):
                if Path(path) != top_level:
                    Path(path).unlink(missing_ok=True)
        return {
            "method": "fixture_reference_copy",
            "destination": str(target),
            "source_document": str(self.document),
            "top_level": str(top_level),
            "configured": ["SetSaveToName(True, destination)"],
            "statuses": {
                "count": len(self.collected),
                "values": [0] * len(self.collected),
                "failures": [],
                "ok": bool(self.collected),
            },
            "files": list(self.collected),
            "mapping": {
                str(item): str(target / Path(item).name)
                for item in [self.document, *self._dependencies]
                if str(target / Path(item).name) in self.collected
            },
        }

    def inspect_copy(self, assembly_path: str) -> dict[str, Any]:
        """Report the component instances a re-opened document resolves (fixture).

        Every failure knob describes the *copy* only: the original is the baseline
        the copy is compared against, so injecting there would just make the
        source look broken.
        """

        target = Path(assembly_path)
        base = target.parent
        is_copy = target.resolve() != Path(self.document).resolve()
        extra = self.copy_extra_components if is_copy else 0
        unresolved_count = self.copy_unresolved if is_copy else 0
        entries = [dict(entry) for entry in self.components]
        entries.extend({"name": f"extra-{index}", "path": str(self.document)} for index in range(extra))
        instances = []
        unresolved = []
        suppressed = []
        for position, entry in enumerate(entries):
            name = str(entry["name"])
            parent = str(entry.get("parent") or "")
            # Name2 carries the assembly instance path, not the document path
            instance = f"{parent}/{name}" if parent else str(entry.get("instance") or name)
            if is_copy and self.copy_duplicate_instances and position == 1 and len(entries) > 1:
                instance = str(entries[0].get("instance") or entries[0]["name"])
            is_suppressed = bool(entry.get("suppressed"))
            document: Path | None = base / Path(entry.get("path") or self.document).name
            if position >= len(entries) - unresolved_count:
                document = None
            elif is_copy and self.copy_outside_documents:
                # the realistic capture defect: the copy still resolves the
                # same-named document from the working tree it was collected from
                document = Path(self.document).parent / Path(entry.get("path") or self.document).name
            configuration = str(entry.get("configuration") or "Default")
            if is_copy and self.copy_referenced_configuration_change:
                configuration = "Other"
            if is_suppressed:
                # A suppressed instance loads no model at all, which is not an
                # escape as long as the copy is suppressed in the same places.
                document = None
            # The reported document is what the host resolved, whether or not the
            # bytes are on disk; the closure check is what proves the file exists.
            if document is None:
                if is_suppressed:
                    suppressed.append(instance)
                else:
                    unresolved.append(instance)
            instances.append(
                {
                    "instance": instance,
                    "name": name,
                    "document": str(document) if document is not None else None,
                    "document_name": document.name.lower() if document is not None else None,
                    "configuration": configuration,
                    "suppressed": is_suppressed,
                    "depth": 0,
                }
            )
        active = self.configurations[0]
        if is_copy and self.copy_configuration:
            active = self.copy_configuration
        return {
            "document": str(target),
            "configuration": active,
            "components": len(instances),
            "unresolved": unresolved,
            "suppressed": suppressed,
            "instances": instances,
        }

    def resolve_dependencies(self, path: str) -> list[dict[str, Any]]:
        if self.resolve_returns_empty:
            return []
        target = Path(path).parent
        entries = [
            {
                "path": str(target / Path(item).name),
                "exists": (target / Path(item).name).is_file(),
                "type": Path(item).suffix.lower(),
            }
            for item in [self.document, *self._dependencies]
        ]
        if self.resolve_only_existing:
            # a build that lists only what it actually resolved, so a missing
            # instance document has to be caught by the re-opened copy
            entries = [entry for entry in entries if entry["exists"]]
        return entries

    def collect_scene(
        self,
        doc_path: str,
        coordinate_systems: Sequence[str],
        progress=None,
        require_material: bool = True,
    ) -> RawScene:
        self.collected_scene_calls.append(str(doc_path))
        if self.originals_change_during_capture:
            Path(self.document).write_bytes(b"fixture assembly edited during capture")
        if self.drift_configuration_after_reading:
            self._drifted = True
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
        return scene

    def export_component_meshes(self, destinations: dict[str, str], progress=None) -> dict[str, dict[str, Any]]:
        entries = {}
        for component, dest_path in destinations.items():
            triangles = cube_stl(Path(dest_path))
            self.exported.append(str(dest_path))
            entries[component] = {
                "component": component,
                "written": dest_path,
                "used_api": "fixture",
                "triangles": triangles,
            }
        return entries

    def verify_sources_unchanged(self) -> dict[str, Any]:
        files = [self.document, *self._dependencies]
        return {str(item): {"sha256": file_digest(Path(item))} for item in files if Path(item).is_file()}

    def environment(self) -> dict[str, Any]:
        return {"revision": "fixture-2026", "license": "fixture"}


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
