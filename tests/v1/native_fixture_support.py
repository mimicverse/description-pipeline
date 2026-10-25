"""Native CAD and sealed-transfer fixtures shared by pipeline tests."""

from __future__ import annotations

import copy
import hashlib
from pathlib import Path

from description_pipeline.sources.solidworks.selection import seal_closure_selection

from tests.sources import support
from tests.sources.test_solidworks_discovery import record as native_record


def capture_record() -> dict:
    """Seal the shared discovery fixture with the fixture occurrence identities."""

    record = copy.deepcopy(native_record())
    selection = record["source_selection"]
    instances = {
        "cad/base.SLDPRT": "base-1",
        "cad/arm.SLDPRT": "arm-1",
    }
    for edge in selection["closure"]["documents"]["cad/robot.SLDASM"][
        "resolved_edges"
    ]:
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
    """Return one fixture mass reading with its requested configuration."""

    payload = support.mass_payload(mass, com)
    payload["reference"] = {
        **payload["reference"],
        "configuration": "Default",
    }
    return payload


class AxisFixtureBackend(support.FixtureCadBackend):
    """Fixture backend with the native readiness metadata used by stage tests."""

    evidence_class = "cad"

    def native_ready(self) -> dict:
        return {
            "role": "native",
            "scope": "provided synthetic CAD backend for the capture regression suite",
            "solidworks_executable": (
                r"C:\Program Files\SOLIDWORKS Corp\SOLIDWORKS\SLDWORKS.exe"
            ),
        }

    def collect_scene(
        self,
        doc_path,
        coordinate_systems,
        on_activity=None,
        require_material=True,
    ):
        scene = super().collect_scene(
            doc_path,
            coordinate_systems,
            on_activity=on_activity,
            require_material=require_material,
        )
        root = Path(self._prepared_capture_root).resolve()
        for component in scene.components:
            candidates = sorted(
                path
                for path in root.rglob(Path(component.path).name)
                if path.is_file()
            )
            if len(candidates) != 1:
                raise AssertionError(
                    f"fixture scene path is ambiguous for {component.name}"
                )
            component.path = str(candidates[0])
        return scene

    def assembly_mass_properties(self, path) -> dict:
        reading = super().assembly_mass_properties(path)
        reading["reference"] = {
            **reading["reference"],
            "used_api": "IMassProperty2.GetMomentOfInertia(0)",
            "override_api": "IMassPropertyOverrideOptions",
            "selection_api": "IMassProperty2.SelectedItems",
        }
        return reading

    def source_scopes(self, components) -> dict[str, dict]:
        """Bind fixture occurrences to copied files independent of package depth."""

        root = Path(self._prepared_capture_root).resolve()
        scopes = {}
        for component in components:
            entry = next(
                item
                for item in self.components
                if str(item["name"]) == str(component)
            )
            original = Path(entry.get("path") or self.document).resolve()
            candidates = sorted(
                path
                for path in root.rglob(original.name)
                if path.is_file()
            )
            if len(candidates) != 1:
                raise AssertionError(
                    f"fixture source scope is ambiguous for {component}"
                )
            copied = candidates[0]
            checksum = hashlib.sha256(copied.read_bytes()).hexdigest()
            scopes[str(component)] = support.fixture_source_scope(
                str(component),
                source_document_path=str(copied),
                source_copy_path=(
                    "source/" + copied.relative_to(root).as_posix()
                ),
                source_document_sha256=checksum,
                referenced_configuration=str(
                    entry.get("configuration") or self.configurations[0]
                ),
            )
        return scopes
