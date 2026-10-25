"""Native CAD and sealed-transfer fixtures shared by pipeline tests.

This exercises the producer/consumer boundary end to end with the fixture CAD backend and
the real generation and verification algorithms (including the MuJoCo consumer gate on the
portable role). The control fixture is explicitly NOT native CAD qualification.
"""

from __future__ import annotations

import copy
import hashlib
from pathlib import Path

from description_pipeline.sources.solidworks.selection import seal_closure_selection

from tests.sources import support
from tests.sources.test_solidworks_discovery import record as native_record


def capture_record() -> dict:
    """The shared discovery fixture sealed with this assembly's occurrence identities.

    The capture proves occurrence coverage by exact ``(owner document, instance)``
    identity between the admitted closure and the re-opened session. The shared
    fixture spells closure instances as canonical document names, so re-seal it
    with the Name2-style instances the fixture session reports.
    """

    record = copy.deepcopy(native_record())
    selection = record["source_selection"]
    instances = {"cad/base.SLDPRT": "base-1", "cad/arm.SLDPRT": "arm-1"}
    for edge in selection["closure"]["documents"]["cad/robot.SLDASM"]["resolved_edges"]:
        edge["instance"] = instances[edge["resolved"].replace("\\", "/")]
    files = {**selection["excluded_files"], **selection["files"]}
    record["source_selection"] = seal_closure_selection(
        files,
        selection["main_assembly"],
        selection["closure"],
        reader_sha256=selection["reader_sha256"],
    )
    return record


def fixture_mass_payload(
    mass: float,
    com: tuple[float, float, float],
) -> dict:
    """Mass fixture with the configuration consumed by source-scope checks."""

    payload = support.mass_payload(mass, com)
    payload["reference"] = {
        **payload["reference"],
        "configuration": "Default",
    }
    return payload


class AxisFixtureBackend(support.FixtureCadBackend):
    """Fixture backend that can resolve the authored joint axis selector."""

    evidence_class = "cad"

    def native_ready(self) -> dict:
        return {
            "role": "native",
            "scope": "provided synthetic CAD backend for the capture regression suite",
            "solidworks_executable": r"C:\Program Files\SOLIDWORKS Corp\SOLIDWORKS\SLDWORKS.exe",
        }

    def environment(self) -> dict:
        environment = super().environment()
        copy_session = environment["sessions"]["copy"]
        if not hasattr(self, "_capture_source_pid"):
            self._capture_source_pid = max(1, self.session_pid - 1)
        environment["sessions"] = {
            "source": {
                "pid": self._capture_source_pid,
                "state": "closed",
            },
            "copy": {
                "pid": copy_session["pid"],
                "state": copy_session["state"],
            },
        }
        return environment

    def retire_session(self, role: str = "copy") -> dict:
        self.assert_role(role)
        before = {
            "pid": self.session_pid,
            "state": "active",
        }
        self.retired_session_pids.append(self.session_pid)
        self.open_documents.clear()
        self.session_pid += 1
        return {
            "role": role,
            "retired": True,
            "before": before,
            "after": {
                "pid": before["pid"],
                "state": "closed",
            },
        }

    @staticmethod
    def assert_role(role: str) -> None:
        if role != "copy":
            raise AssertionError(f"unexpected retired role: {role}")

    def collect_scene(self, doc_path, coordinate_systems, on_activity=None, require_material=True):
        self._capture_document = Path(doc_path).resolve()
        scene = super().collect_scene(
            doc_path,
            coordinate_systems,
            on_activity=on_activity,
            require_material=require_material,
        )
        for row in scene.components:
            row.path = str(self._source_path(row.name))
        return scene

    def _source_path(self, component):
        capture_root = getattr(self, "_capture_document", self.document).parent
        entry = next(
            (
                row
                for row in self.components
                if str(row.get("name")) == str(component)
            ),
            None,
        )
        path_name = (
            Path(str(entry.get("path"))).name
            if entry is not None and entry.get("path")
            else f"{str(component).split('-', 1)[0]}.SLDPRT"
        )
        source_path = capture_root / path_name
        if not source_path.is_file():
            source_path = next(
                (
                    capture_root / dependency.name
                    for dependency in self._dependencies
                    if dependency.name.casefold() == path_name.casefold()
                ),
                source_path,
            )
        if not source_path.is_file():
            raise RuntimeError(
                f"control fixture source scope has no source file: {component}"
            )
        return source_path

    def source_scopes(self, components):
        """Supply the control fixture's exact source scope records."""

        scopes = {}
        for component in components:
            source_path = self._source_path(component)
            sha256 = hashlib.sha256(source_path.read_bytes()).hexdigest()
            scopes[str(component)] = {
                "rule_version": "native-open-edge-scope-binding-v1",
                "component": str(component),
                "source_document_path": str(source_path),
                "source_copy_path": None,
                "source_document_sha256": sha256,
                "source_copy_sha256": sha256,
                "source_document_identity_sha256": sha256,
                "source_document_identity_kind": "file_sha256",
                "referenced_configuration": str(
                    self.configurations[0]
                ),
                "occurrence_scope": str(component),
                "virtual_owner": None,
                "virtual_owner_sha256": None,
                "virtual_session_document_path": None,
                "virtual_session_temp_root": None,
                "virtual_native_api_pid": None,
            }
        return scopes

    def assembly_mass_properties(self, path):
        from description_pipeline.sources.solidworks.errors import CadError

        raise CadError("cad_mass_property_unavailable", "Control fixture has no native whole-assembly reading")

    def assembly_component_mass_properties(self, path):
        from description_pipeline.sources.solidworks.errors import CadError

        raise CadError("cad_component_context_unavailable", "Control fixture has no native instance context")

    def capture_axis_reference(self, reference):
        record = {
            "component": str(reference.get("component") or ""),
            "body_type": str(reference.get("body_type") or "solid"),
            "selector": {key: value for key, value in reference.items() if key != "note"},
            "surface": "cylinder",
            "coordinate_frame": "component_local",
            "direction_semantics": "undirected_axis_line",
            "axis_point_m": [0.0, 0.0, 0.1],
            "axis_direction": [0.0, 0.0, 1.0],
            "radius_m": 0.006,
            "used_api": "fixture",
        }
        if isinstance(reference.get("face_index"), int) and not isinstance(reference.get("face_index"), bool):
            record["face_index"] = reference["face_index"]
        return record

    def export_component_meshes(self, destinations, on_activity=None):
        """Mirror the native exporter response: body counts plus tessellation accounting.

        The real native reader reports, per component, the native body counts and
        the display-tessellation accounting (total faces and any facetless sheet
        faces) beside the exported mesh identity. The control fixture reports
        the canonical cube stand-in: one solid body whose six faces all
        tessellate, with no omitted faces.
        """

        entries = super().export_component_meshes(destinations, on_activity=on_activity)
        for info in entries.values():
            info["bodies"] = {"solid": 1, "sheet": 0}
        return entries
