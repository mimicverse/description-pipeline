"""Raw CAD shapes and the contact-layer protocol the freeze path depends on.

The SolidWorks adapter is the only place that talks COM.  Everything above it
works on these plain containers so the rest of the pipeline can run, and be
tested, without Windows or SolidWorks.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

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
    """One projected component instance used by portable scene consumers."""

    name: str
    path: str = ""
    transform: Matrix = field(default_factory=identity)
    is_fixed: bool = False
    document_type: str = ""


@dataclass
class RawScene:
    """A projected scene consumed after the owned CAD session has closed."""

    document: str
    components: list[RawComponent] = field(default_factory=list)
    coordinate_systems: dict[str, Matrix] = field(default_factory=dict)
    mass_properties: dict[str, dict] = field(default_factory=dict)
    notes: dict[str, Any] = field(default_factory=dict)


class CadBackend:
    """What the prepared-capture path needs from a CAD host.

    Implementations raise the errors in :mod:`.errors`; a missing environment is
    reported as ``EnvironmentError_`` and never as an empty successful read.
    """

    name = "abstract"

    def session(self, *, resource_settlement=None):
        """Own the capture session and its COM lifetime."""

        raise NotImplementedError

    def release(self) -> None:
        """Release all native objects before offline projection."""

        raise NotImplementedError

    def health(self) -> dict:
        raise NotImplementedError

    def list_documents(self) -> list[str]:
        raise NotImplementedError

    def prepare_prepared_capture(
        self,
        source_root: str,
        assembly: str,
        configuration: str,
    ) -> dict:
        """Bind the controlled prepared root, configuration and session role."""

        raise NotImplementedError

    def open_document(self, path: str) -> dict:
        raise NotImplementedError


    def collect_prepared_scene(
        self,
        doc_path: str,
        coordinate_systems: Sequence[str],
        groups: Sequence[dict],
        on_activity=None,
        require_material: bool = True,
        expected_session_sources: Mapping[str, str] | None = None,
        native_files: Mapping[str, str] | None = None,
        expected_occurrences=None,
    ) -> dict[str, Any]:
        """Read raw scene, mass, pose, configuration and source facts in COM."""

        raise NotImplementedError

    def capture_prepared_geometry(
        self,
        source_scopes: Mapping[str, dict],
        on_activity=None,
    ) -> dict[str, dict]:
        """Read direct occurrence triangles into memory without file output."""

        raise NotImplementedError


    def discover_native(self, frozen_source: Path, settings: dict, *, on_activity=None) -> dict:
        """Read the raw native primitives used for CAD-only semantic discovery.

        Returns the ``solidworks-to-urdf.native-discovery/v2`` raw record:
        identity records, the component graph, mate features with their
        entities, named datums, user-defined properties, cylinder axes and the
        per-component mass/material readings, each with the file hashes it was
        read from.  Implementations fail closed; they never invent facts.

        ``on_activity`` optionally receives partial live-activity records
        (``phase``/``action`` plus optional ``params``, ``current_object``,
        ``completed``/``total``, ``unit``) while the read is blocking; it is
        observation only and can never change the record or the failure
        behaviour.  Callbacks observed by the pipeline are invoked defensively.
        """

        raise NotImplementedError
