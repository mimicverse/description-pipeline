"""Synthetic CAD backend for CaptureHandler regression tests."""

from __future__ import annotations

from pathlib import Path

from tests.sources import support
from tests.v1.native_fixture_support import (
    AxisFixtureBackend,
    fixture_mass_payload,
)


def fixture_backend(
    prepared_package: Path,
    main_assembly: str,
) -> AxisFixtureBackend:
    """Build the one synthetic backend used by real CaptureHandler tests."""

    return AxisFixtureBackend(
        prepared_package / main_assembly,
        [
            {
                "name": "base-1",
                "path": str(prepared_package / "cad/base.SLDPRT"),
                "transform": support.placement((0.0, 0.0, 0.0)),
                "mass": fixture_mass_payload(0.192, (0.01, 0.02, 0.0)),
            },
            {
                "name": "arm-1",
                "path": str(prepared_package / "cad/arm.SLDPRT"),
                "transform": support.placement((0.0, 0.0, 0.1)),
                "mass": fixture_mass_payload(
                    0.105654866776462,
                    (0.0, 0.0, 0.05),
                ),
            },
        ],
        dependencies=[
            prepared_package / "cad/base.SLDPRT",
            prepared_package / "cad/arm.SLDPRT",
        ],
        coordinate_systems={
            "CS_base_link": support.placement((0.0, 0.0, 0.0)),
            "CS_arm_link": support.placement((0.0, 0.0, 0.1)),
        },
    )
