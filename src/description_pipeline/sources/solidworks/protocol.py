"""Raw CAD shapes and the contact-layer protocol the freeze path depends on.

The SolidWorks adapter is the only place that talks COM.  Everything above it
works on these plain containers so the rest of the pipeline can run, and be
tested, without Windows or SolidWorks.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from collections.abc import Sequence
from pathlib import Path

Matrix = tuple[
    float,
    float,
    float,
    float,
    float,
    float,
    float,
    float,
    float,
    float,
    float,
    float,
    float,
    float,
    float,
    float,
]


def identity() -> Matrix:
    """A row-major 4x4 identity matrix."""

    return (
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
    )


@dataclass
class RawComponent:
    """One SolidWorks component instance found while walking an assembly."""

    name: str
    path: str = ""
    transform: Matrix = field(default_factory=identity)
    is_fixed: bool = False
    document_type: str = ""


@dataclass
class RawScene:
    """Everything the contact layer read from one document, unmodified."""

    document: str
    components: list[RawComponent] = field(default_factory=list)
    coordinate_systems: dict[str, Matrix] = field(default_factory=dict)
    mass_properties: dict[str, dict] = field(default_factory=dict)
    notes: dict[str, str] = field(default_factory=dict)


class CadBackend:
    """What the freeze path needs from a CAD host.

    Implementations raise the errors in :mod:`.errors`; a missing environment is
    reported as ``EnvironmentError_`` and never as an empty successful read.
    """

    name = "abstract"

    def health(self) -> dict:
        raise NotImplementedError

    def list_documents(self) -> list[str]:
        raise NotImplementedError

    def open_document(self, path: str) -> dict:
        raise NotImplementedError

    def close_document(self, name: str, confirm: bool = False) -> dict:
        raise NotImplementedError

    def collect_scene(
        self,
        doc_path: str,
        coordinate_systems: Sequence[str],
        progress=None,
        require_material: bool = True,
    ) -> RawScene:
        raise NotImplementedError

    def export_component_meshes(self, destinations: dict[str, str], progress=None) -> dict[str, dict]:
        """Export one geometry batch while its native parent interfaces stay alive."""
        raise NotImplementedError

    def verify_sources_unchanged(self) -> dict[str, dict]:
        raise NotImplementedError

    def discover_native(self, frozen_source: Path, settings: dict) -> dict:
        """Read the raw native primitives used for CAD-only semantic discovery.

        Returns the ``solidworks-to-urdf.native-discovery/v1`` raw record:
        identity records, the component graph, mate features with their
        entities, named datums, user-defined properties, cylinder axes and the
        per-component mass/material readings, each with the file hashes it was
        read from.  Implementations fail closed; they never invent facts.
        """

        raise NotImplementedError
