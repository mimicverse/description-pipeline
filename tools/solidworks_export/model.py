"""Data containers shared between the COM layer, the exporter and the CLI."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .transform import Matrix, identity


@dataclass
class RawComponent:
    """One SolidWorks component instance found while walking the assembly."""

    name: str
    path: str = ""
    transform: Matrix = field(default_factory=identity)
    is_fixed: bool = False
    document_type: str = ""


@dataclass
class RawScene:
    """Everything the COM layer extracted from the assembly, unmodified."""

    document: str
    components: List[RawComponent] = field(default_factory=list)
    coordinate_systems: Dict[str, Matrix] = field(default_factory=dict)
    mass_properties: Dict[str, Dict] = field(default_factory=dict)
    notes: Dict[str, str] = field(default_factory=dict)


@dataclass
class RobotLink:
    name: str
    mass: float
    com: Tuple[float, float, float]
    inertia6: Tuple[float, float, float, float, float, float]
    inertial_rpy: Tuple[float, float, float]
    mesh: Optional[str] = None
    is_frame: bool = False


@dataclass
class RobotJoint:
    name: str
    type: str
    parent: str
    child: str
    xyz: Tuple[float, float, float]
    rpy: Tuple[float, float, float]
    axis: Optional[Tuple[float, float, float]] = None
    limit: Optional[Dict[str, float]] = None
    dynamics: Optional[Dict[str, float]] = None


@dataclass
class RobotModel:
    name: str
    links: List[RobotLink] = field(default_factory=list)
    joints: List[RobotJoint] = field(default_factory=list)
