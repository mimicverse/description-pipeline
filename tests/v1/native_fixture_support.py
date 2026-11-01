"""Native CAD and sealed-transfer fixtures shared by pipeline tests."""

from __future__ import annotations

import copy
import hashlib
import os
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
    files = dict(selection["files"])
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
    """Fixture backend used by Capture and stage tests."""

    evidence_class = "cad"

    def prepare_prepared_capture(self, source_root, assembly, configuration):
        policy = super().prepare_prepared_capture(source_root, assembly, configuration)
        self._fixture_capture_document = Path(assembly).resolve()
        return policy

    def _copied_component(self, entry):
        original = Path(entry.get("path") or self.document).resolve()
        relative = os.path.relpath(original, Path(self.document).resolve().parent)
        copied = (self._fixture_capture_document.parent / relative).resolve()
        root = Path(self._prepared_capture_root).resolve()
        if not copied.is_relative_to(root) or not copied.is_file():
            raise AssertionError("Fixture component has no exact copied source: " + str(original))
        return copied

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
        by_name = {str(entry["name"]): entry for entry in self.components}
        for component in scene.components:
            component.path = str(self._copied_component(by_name[component.name]))
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
            copied = self._copied_component(entry)
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
