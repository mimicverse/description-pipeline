"""Fixtures for source-adapter tests; the backend never touches live CAD."""

from __future__ import annotations

import copy
import struct
from pathlib import Path
from typing import Any, cast
from collections.abc import Sequence

from description_pipeline.io import file_digest
from description_pipeline.sources.solidworks.errors import CadError
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
        order = getattr(self, "order", None)
        if order is None:
            order = self.order = []
        order.append("open_document")
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

    def collect_dependencies(
        self,
        path: str,
        destination_dir: str,
        *,
        skip_missing=(),
        skip_session_temp: bool = False,
        excluded_paths=(),
        required_names=(),
        admitted_roots=(),
        closure_documents=None,
        required_paths=(),
    ) -> dict[str, Any]:
        self.collect_skips = {
            "skip_missing": sorted(str(item) for item in skip_missing),
            "skip_session_temp": bool(skip_session_temp),
            "excluded_paths": sorted(str(item) for item in excluded_paths),
            "required_names": sorted(str(item) for item in required_names),
            "admitted_roots": [str(item) for item in admitted_roots],
            "closure_documents": (
                sorted((closure_documents or {}).keys()) if isinstance(closure_documents, dict) else None
            ),
            "required_paths": sorted(str(item) for item in required_paths),
        }
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
            "skipped_missing": self.collect_skips["skip_missing"],
            "skipped_session_temp": [],
            "skipped_excluded": self.collect_skips["excluded_paths"],
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
                    "defining": str(Path(assembly_path)),
                    "defining_anchor": None,
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
        on_activity=None,
        require_material: bool = True,
    ) -> RawScene:
        order = getattr(self, "order", None)
        if order is None:
            order = self.order = []
        order.append("collect_scene")
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

    def export_component_meshes(self, destinations: dict[str, str], on_activity=None) -> dict[str, dict[str, Any]]:
        entries = {}
        total = len(destinations)
        for index, (component, dest_path) in enumerate(destinations.items(), start=1):
            if on_activity is not None:
                on_activity(
                    {
                        "phase": "capture",
                        "action": "mesh_export",
                        "current_object": component,
                        "completed": index,
                        "total": total,
                    }
                )
            triangles = cube_stl(Path(dest_path))
            self.exported.append(str(dest_path))
            entries[component] = {
                "component": component,
                "written": dest_path,
                "used_api": "fixture",
                "triangles": triangles,
            }
        return entries

    def link_group_mass_properties(self, path, selections):
        """Direct link reads for the fixture: the assembly-frame recombination."""

        order = getattr(self, "order", None)
        if order is None:
            order = self.order = []
        order.append("link_group_read")
        links = compose_fixture_link_reads(self.components, getattr(self, "effective_readings", None) or {}, selections)
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

    def close_all_documents(self):
        """Fixture close-all for the pristine readout window."""

        order = getattr(self, "order", None)
        if order is None:
            order = self.order = []
        order.append("close_all_documents")
        if getattr(self, "close_refuses", False):
            raise CadError("cad_close_failed", "fixture refuses to close documents")
        self.open_documents.clear()
        return {"closed": True}

    def verify_sources_unchanged(self) -> dict[str, Any]:
        order = getattr(self, "order", None)
        if order is None:
            order = self.order = []
        order.append("verify_sources")
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


def write_link_group_mass_record(bundle: Path, model: dict, source: dict) -> None:
    """Synthesize the direct link group record the producer will emit (fixture stand-in).

    The record is derived from the bundle's sealed raw scene plus the model the
    fixtures just built, so the verifier's direct-readout binding can run before
    the native producer method lands.
    """

    import json as _json

    import numpy as _np

    from description_pipeline.sources.solidworks import verify as oracle

    bundle = Path(bundle)
    evidence = bundle / "evidence"
    raw = _json.loads((evidence / "raw/scene_raw.json").read_text())
    masses = _json.loads((evidence / "raw/mass_properties.json").read_text())
    datums = _json.loads((evidence / "raw/coordinate_systems.json").read_text())
    declared_path = evidence / "raw/declared_masses.json"
    declared = (_json.loads(declared_path.read_text()).get("items") or {}) if declared_path.is_file() else {}
    effective = oracle.effective_math_map(masses, raw)
    links = {str(link["name"]): link for link in model.get("links", [])}
    payload = {
        "schema_version": "solidworks-to-urdf.link-group-mass-properties/v1",
        "assembly": raw.get("source_document") or raw.get("document"),
        "configuration": str(source.get("configuration") or "Default"),
        "used_api": "IMassProperty2 (SelectedItems = component set)",
        "selection_rule": "top_most_providers+uncovered_leaves",
        "links": {},
    }
    for body in source.get("bodies") or []:
        name = str(body.get("name"))
        selection = sorted(
            str(note["component"])
            for *_rest, note in oracle._resolved_contributions(body, raw, masses, declared, effective)
        )
        mass, com_link, tensor_link = oracle._canonical_in_link_frame(links[name])
        datum = _np.array(datums[body["frame"]["coordinate_system"]], dtype=float).reshape(4, 4)
        com_assembly = datum[:3, :3] @ com_link + datum[:3, 3]
        tensor_assembly = datum[:3, :3] @ tensor_link @ datum[:3, :3].T
        read = {
            "mass_kg": float(mass),
            "volume_m3": 0.001,
            "density_kg_m3": 1000.0,
            "com_m": com_assembly.tolist(),
            "inertia_tensor_about_com": tensor_assembly.tolist(),
            "inertia_values_about_origin_api": {"error": "fixture"},
            "overrides": {
                "OverrideMass": False,
                "OverrideCenterOfMass": False,
                "OverrideMomentsOfInertia": False,
            },
        }
        payload["links"][name] = {
            "selection": selection,
            "reads": [_json.loads(_json.dumps(read)), _json.loads(_json.dumps(read))],
            "reproducibility": {
                "mass_delta_kg": 0.0,
                "com_delta_norm_m": 0.0,
                "tensor_delta_max_abs": 0.0,
            },
        }
    (evidence / "raw/link_group_mass_properties.json").write_text(
        _json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


def write_link_group_mass_record_from_recombination(bundle: Path, source: dict) -> None:
    """Seed the direct link record from the recombination (fixtures without a model yet).

    Used before ``load_scene``/manifest verification so a fixture bundle is
    shaped exactly like a native12 capture; the scene that consumes it later
    regenerates the same link inertials by construction.
    """

    import json as _json

    from description_pipeline.sources.solidworks import verify as oracle

    bundle = Path(bundle)
    evidence = bundle / "evidence"
    raw = _json.loads((evidence / "raw/scene_raw.json").read_text())
    masses = _json.loads((evidence / "raw/mass_properties.json").read_text())
    declared_path = evidence / "raw/declared_masses.json"
    declared = (_json.loads(declared_path.read_text()).get("items") or {}) if declared_path.is_file() else {}
    effective = oracle.effective_math_map(masses, raw)
    payload = {
        "schema_version": "solidworks-to-urdf.link-group-mass-properties/v1",
        "assembly": raw.get("source_document") or raw.get("document"),
        "configuration": str(source.get("configuration") or "Default"),
        "used_api": "IMassProperty2 (SelectedItems = component set)",
        "selection_rule": "top_most_providers+uncovered_leaves",
        "links": {},
    }
    for body in source.get("bodies") or []:
        name = str(body.get("name"))
        selection = sorted(
            str(note["component"])
            for *_rest, note in oracle._resolved_contributions(body, raw, masses, declared, effective)
        )
        world = oracle._raw_world(body, raw, masses, declared)
        read = {
            "mass_kg": float(world["mass"]),
            "volume_m3": None,
            "density_kg_m3": None,
            "com_m": [float(value) for value in world["com"]],
            "inertia_tensor_about_com": [[float(value) for value in row] for row in world["inertia"]],
            "inertia_values_about_origin_api": {"error": "fixture"},
            "overrides": {
                "OverrideMass": False,
                "OverrideCenterOfMass": False,
                "OverrideMomentsOfInertia": False,
            },
        }
        payload["links"][name] = {
            "selection": selection,
            "reads": [_json.loads(_json.dumps(read)), _json.loads(_json.dumps(read))],
            "reproducibility": {
                "mass_delta_kg": 0.0,
                "com_delta_norm_m": 0.0,
                "tensor_delta_max_abs": 0.0,
            },
        }
    (evidence / "raw/link_group_mass_properties.json").write_text(
        _json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


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
        "needs_rebuild2_value": 0,
        "configuration_needs_rebuild": False,
        "read_only": True,
    }
    return {
        "before": copy.deepcopy(observed),
        "after": copy.deepcopy(observed),
        "used_api": "IModelDoc2.GetSaveFlag/IConfiguration.Name",
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

    return write_delivery_manifest(
        Path(root),
        contract={"schema_version": CONTRACT_SCHEMA_VERSION, "digest": "0" * 64},
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
        request = {
            "schema_version": "solidworks-to-urdf.request/v1",
            "request_id": "00000000-0000-4000-8000-000000000000",
            "target_hardware": str(selection["hardware_id"]),
            "change_summary": str((source.get("metadata") or {}).get("change_summary") or "fixture"),
            "main_assembly": str(selection["main_assembly"]),
            "handoff_digest": payload_handoff if isinstance(payload_handoff, str) else "0" * 64,
            "platform_config_digest": "0" * 64,
        }
    write_json(root / "input/request.json", request)
    for name, text in (originals or {}).items():
        target = root / "input/cad" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    for name, text in (records or {}).items():
        target = root / "input/records" / name
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
            "material_source": "cad",
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


def compose_delivery_inputs(
    delivery: Path,
    *,
    main_assembly: str,
    configuration: str,
    record: dict,
    cad_files: dict[str, Path],
    records: dict[str, str] | None = None,
    record_keys: dict[str, str] | None = None,
    material_source: str | None = None,
) -> None:
    """Compose the sealed inputs Linux consumers rebuild from (fixture plumbing).

    Copies the genuine CAD bytes into ``input/cad/<relative>``, writes the
    sealed discovery payload (with its raw native record) at
    ``evidence/discovery.json`` (including optional controlled-record files at
    ``input/records/<name>`` carrying their payload entries), and, when a
    material authority is given,
    ensures the canonical provenance block exists in ``evidence/capture.json``.
    Synthetic consumer-fixture inputs only: it must never feed the unchanged
    canonical/E2E producers, which produce their own evidence.  Existing
    fixture blocks (warnings and readouts included) are preserved, and the
    provenance identity binds to the record's captured identity rather than a
    relocated fixture path.  No consumer output is injected.
    """

    import shutil

    from description_pipeline.io import file_digest, read_data, write_json
    from description_pipeline.sources.solidworks.discovery import DISCOVERY_SCHEMA

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
    import hashlib as _hashlib

    payload_records: list[dict] = []
    for name, text in (records or {}).items():
        target = delivery / "input" / "records" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        entry = {
            "file": str(name),
            "package_file": str(name),
            "sha256": _hashlib.sha256(text.encode("utf-8")).hexdigest(),
        }
        key = (record_keys or {}).get(name)
        if key is not None:
            entry["key"] = str(key)
        payload_records.append(entry)
    write_json(
        evidence / "discovery.json",
        {
            "schema_version": DISCOVERY_SCHEMA,
            "run_id": str(record.get("run_id") or "fixture-run"),
            "derivation": {
                "main_assembly": main_assembly,
                "configuration": configuration,
                "frozen_names": {},
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
    if material_source is not None:
        capture_path = evidence / "capture.json"
        capture = read_data(capture_path) if capture_path.is_file() else {}
        if not isinstance(capture, dict):
            capture = {}
        existing = capture.get("material_provenance")
        if not isinstance(existing, dict):
            captured_assembly = str(identity.get("main_assembly") or main_assembly)
            captured_configuration = str(identity.get("delivery_configuration") or configuration)
            capture["material_provenance"] = {
                "schema_version": "solidworks-to-urdf.material-provenance/v1",
                "assembly": captured_assembly,
                "configuration": captured_configuration,
                "material_source": material_source,
                "measured_source": "cad_api",
                "warnings": [],
            }
            write_json(capture_path, capture)


# ---------------------------------------------------------------------------
# Fixture plumbing: the small shared producer for consumer-fixture inputs.
#
# The scenario facts - which occurrences, datums, mates and masses a module
# exercises - stay with the module that builds them; these builders own the
# record boilerplate and the sealing infrastructure only.  Synthetic
# test-only inputs: the unchanged canonical/E2E producers build their own
# evidence and never use any of this.  No consumer output is injected.
# ---------------------------------------------------------------------------


def native_component(
    name: str, *, transform: Sequence[float] | None = None, fixed: bool = True, document: str | None = None
) -> dict:
    """One recorded native occurrence entry (fixture plumbing)."""

    return {
        "name2": name,
        "lightweight": False,
        "suppression_state": 2,
        "instance_id": name,
        "document": document or name.split("/")[-1].split("~")[0] + ".SLDPRT",
        "configuration": "Default",
        "fixed": fixed,
        "suppressed": False,
        "transform": [float(value) for value in (placement() if transform is None else transform)],
    }


def native_datum(name: str, owner: str, transform: Sequence[float]) -> dict:
    """One recorded native coordinate system (fixture plumbing)."""

    return {"name": name, "owner": owner, "array": [float(value) for value in transform]}


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
        "masses": [dict(item) for item in masses],
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
    record_keys: dict[str, str] | None = None,
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
    record_keys: dict[str, str] | None = None,
) -> None:
    """Fixture plumbing: stage the discovery payload for hand-built producer roots.

    The controlled records land under ``input/records``, the payload binds
    them, and the capture's base occurrence is stubbed under ``input/cad`` -
    the raw-evidence sibling of ``compose_delivery_inputs`` for the fixture
    roots that carry no snapshot manifest (closure and its accounting
    consumer).
    """

    import hashlib

    from description_pipeline.io import write_json

    root = Path(root)
    payload_records = []
    for relative, text in (controlled or {}).items():
        path = root / "input" / "records" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        entry = {
            "file": relative,
            "package_file": relative,
            "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        }
        key = (record_keys or {}).get(relative)
        if key is not None:
            entry["key"] = str(key)
        payload_records.append(entry)
    write_json(
        root / "evidence" / "discovery.json",
        {
            "schema_version": "solidworks-to-urdf.native-discovery/v1",
            "run_id": "fixture-run",
            "derivation": {
                "main_assembly": "robot.SLDASM",
                "configuration": "Default",
                "frozen_names": {},
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
    assembly = root / "input" / "cad" / "robot.SLDASM"
    assembly.parent.mkdir(parents=True, exist_ok=True)
    assembly.write_bytes(b"fixture assembly")
