"""Backend abstraction: SolidWorks COM (real) and a deterministic fake (tests)."""

from __future__ import annotations

import abc
import os
import struct
from typing import Dict, List, Optional, Sequence

from .errors import BridgeError, CadError
from .model import RawComponent, RawScene
from .transform import from_xyz_rpy, identity, rotation_3x3


class Backend(abc.ABC):  # noqa: B024 - 允许后端只实现一部分能力，未实现的调用报 not_supported
    """Everything the bridge needs from a CAD host."""

    name = "abstract"

    def health(self) -> dict:
        raise BridgeError("not_supported", "health is not supported")

    def list_documents(self) -> List[str]:
        raise BridgeError("not_supported", "document listing is not supported")

    def open_document(self, path: str) -> dict:
        raise BridgeError("not_supported", "opening documents is not supported")

    def close_document(self, name: str, confirm: bool = False) -> dict:
        raise BridgeError("not_supported", "closing documents is not supported")

    def collect_scene(
        self, doc_path: str, coordinate_systems: Sequence[str], progress=None, require_material: bool = True
    ) -> RawScene:
        raise BridgeError("not_supported", "scene collection is not supported")

    def export_component_mesh(self, component: str, dest_path: str, progress=None) -> dict:
        raise BridgeError("not_supported", "mesh export is not supported")

    def selftest(self, test_cs: Optional[str] = None, export_mesh: Optional[str] = None) -> dict:
        return {"backend": self.name, "points": {}}


def _write_triangle_stl(path: str, offset: float) -> None:
    """Write a tiny valid binary STL (one triangle) for fakes and tests."""

    header = b"swbridge fake stl" + b" " * 64
    with open(path, "wb") as handle:
        handle.write(header[:80])
        handle.write(struct.pack("<I", 1))
        handle.write(struct.pack("<3f", 0.0, 0.0, 1.0))
        handle.write(struct.pack("<3f", offset, 0.0, 0.0))
        handle.write(struct.pack("<3f", offset + 0.01, 0.0, 0.0))
        handle.write(struct.pack("<3f", offset, 0.01, 0.0))
        handle.write(struct.pack("<H", 0))


def _facet_normal(v0, v1, v2):
    ux, uy, uz = (v1[0] - v0[0], v1[1] - v0[1], v1[2] - v0[2])
    vx, vy, vz = (v2[0] - v0[0], v2[1] - v0[1], v2[2] - v0[2])
    nx, ny, nz = (uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx)
    length = (nx * nx + ny * ny + nz * nz) ** 0.5 or 1.0
    return (nx / length, ny / length, nz / length)


def _write_box_stl(path: str, size, center=(0.0, 0.0, 0.0), rpy=(0.0, 0.0, 0.0)) -> int:
    """Write a closed, outward-wound box (12 triangles) in binary STL.

    ``size`` is the full edge length per axis; ``rpy`` rotates the box inside
    its own frame before ``center`` is applied.
    """

    a, b, c = (float(v) for v in size)
    cx, cy, cz = center
    rotation = rotation_3x3(rpy)
    unit = [(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0), (0, 0, 1), (1, 0, 1), (1, 1, 1), (0, 1, 1)]
    vertices = []
    for x, y, z in unit:
        local = ((2 * x - 1) * a / 2.0, (2 * y - 1) * b / 2.0, (2 * z - 1) * c / 2.0)
        vertices.append(
            (
                rotation[0][0] * local[0] + rotation[0][1] * local[1] + rotation[0][2] * local[2] + cx,
                rotation[1][0] * local[0] + rotation[1][1] * local[1] + rotation[1][2] * local[2] + cy,
                rotation[2][0] * local[0] + rotation[2][1] * local[1] + rotation[2][2] * local[2] + cz,
            )
        )
    faces = [
        (0, 3, 2),
        (0, 2, 1),  # -Z
        (4, 5, 6),
        (4, 6, 7),  # +Z
        (0, 1, 5),
        (0, 5, 4),  # -Y
        (3, 7, 6),
        (3, 6, 2),  # +Y
        (0, 4, 7),
        (0, 7, 3),  # -X
        (1, 2, 6),
        (1, 6, 5),  # +X
    ]
    header = b"swbridge closed box stl".ljust(80, b" ")
    with open(path, "wb") as handle:
        handle.write(header)
        handle.write(struct.pack("<I", len(faces)))
        for i0, i1, i2 in faces:
            v0, v1, v2 = vertices[i0], vertices[i1], vertices[i2]
            handle.write(struct.pack("<3f", *_facet_normal(v0, v1, v2)))
            for vertex in (v0, v1, v2):
                handle.write(struct.pack("<3f", *vertex))
            handle.write(struct.pack("<H", 0))
    return len(faces)


def _write_cube_stl(path: str, center=(0.0, 0.0, 0.0), half: float = 0.04) -> int:
    return _write_box_stl(path, (2.0 * half, 2.0 * half, 2.0 * half), center)


class FakeBackend(Backend):
    """Deterministic scene used by tests and by ``SWBRIDGE_BACKEND=fake``.

    Mass properties are expressed in each component's own part frame and use
    the SolidWorks positive-products notation, mirroring the real backend.
    """

    name = "fake"

    def __init__(self) -> None:
        self.document = "FAKE.SLDASM"
        self.components: Dict[str, RawComponent] = {
            "Base-1": RawComponent(
                name="Base-1", path="D:/models/Base.SLDPRT", transform=identity(), is_fixed=True, document_type="part"
            ),
            "Arm-1": RawComponent(
                name="Arm-1",
                path="D:/models/Arm.SLDPRT",
                transform=from_xyz_rpy((0.0, 0.0, 0.1), (0.0, 0.0, 0.0)),
                is_fixed=False,
                document_type="part",
            ),
        }
        self.mass_properties = {
            "Base-1": {
                "mass": 2.0,
                "com": (0.0, 0.0, 0.05),
                "inertia": ((0.1, 0.0, 0.0), (0.0, 0.2, 0.0), (0.0, 0.0, 0.3)),
                "reference": {"used_api": "fake"},
            },
            "Arm-1": {
                "mass": 1.0,
                "com": (0.0, 0.0, 0.05),
                "inertia": ((0.01, 0.002, 0.0), (0.002, 0.02, 0.0), (0.0, 0.0, 0.03)),
                "reference": {"used_api": "fake"},
            },
        }
        self.coordinate_systems = {
            "CS_dof_arm": from_xyz_rpy((0.0, 0.0, 0.1), (0.0, 0.0, 0.0)),
        }

    def health(self) -> dict:
        return {
            "ok": True,
            "backend": self.name,
            "sw_version": "fake-2026",
            "active_document": self.document,
        }

    def list_documents(self) -> List[str]:
        return [self.document]

    def open_document(self, path: str) -> dict:
        self.document = os.path.basename(path).upper()
        return {"opened": self.document, "path": path, "backend": self.name}

    def close_document(self, name: str, confirm: bool = False) -> dict:
        if not confirm:
            raise BridgeError("confirm_required", "closing a document needs --confirm")
        return {"closed": name.upper()}

    def collect_scene(
        self, doc_path: str, coordinate_systems: Sequence[str], progress=None, require_material: bool = True
    ) -> RawScene:
        if progress:
            progress(f"fake backend: collecting scene for {doc_path}")
        available = {
            name: self.coordinate_systems[name] for name in coordinate_systems if name in self.coordinate_systems
        }
        return RawScene(
            document=doc_path or self.document,
            components=list(self.components.values()),
            coordinate_systems=available,
            mass_properties=dict(self.mass_properties),
            notes={"backend": self.name},
        )

    def export_component_mesh(self, component: str, dest_path: str, progress=None) -> dict:
        if component not in self.components:
            raise CadError("cad_missing_component", f"unknown component {component!r}")
        geometry: dict = {
            "Base-1": {"center": (0.0, 0.0, 0.03), "half": 0.05},
            "Arm-1": {"center": (0.0, 0.0, 0.02), "half": 0.03},
        }
        shape = geometry.get(component, {"center": (0.0, 0.0, 0.0), "half": 0.03})
        triangles = _write_cube_stl(dest_path, center=shape["center"], half=shape["half"])
        return {"component": component, "written": dest_path, "used_api": "fake", "triangles": triangles}

    def selftest(self, test_cs: Optional[str] = None, export_mesh: Optional[str] = None) -> dict:
        points = {
            "backend": {"ok": True, "detail": "fake backend", "used_api": "fake"},
            "attach": {"ok": True, "detail": "fake", "used_api": "fake"},
            "traversal": {"ok": True, "detail": f"{len(self.components)} components", "used_api": "fake"},
            "mass_property": {"ok": True, "detail": "fake", "used_api": "fake"},
        }
        if test_cs:
            points["coordinate_system"] = {
                "ok": test_cs in self.coordinate_systems,
                "detail": test_cs,
                "used_api": "fake",
            }
        return {"backend": self.name, "points": points}
