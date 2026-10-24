"""Sealed native fixture capture for the scheduled split smoke (never native qualification).

The control fixture drives the real freeze/discover/capture producer with the fixture CAD
backend, so the transfer archive, native tool record and stage receipts follow the production
contracts.  Its evidence class is deliberately a control fixture: the Linux verification tail
must reject it on the documented fixture limits and publication must never execute.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from packaging.utils import canonicalize_name

from description_pipeline import solidworks, steps
from description_pipeline.io import digest, file_digest
from description_pipeline.orchestration import stage_transfer as transfer
from description_pipeline.runtime import RUNTIME_VERSIONS, required_packages, tool_record
from description_pipeline.sources.solidworks.discovery import prepare_native_package
from description_pipeline.sources.solidworks.revision import package_inventory
from description_pipeline.stages import validate_freeze_events

from tests.sources import support
from tests.sources.test_solidworks_discovery import FakeBackend
from tests.v1.test_split_roundtrip import (
    AxisFixtureBackend,
    capture_record,
    fixture_mass_payload,
)

#: The release-only gates a control fixture can never satisfy; any other failure is a defect.
CONTROL_LIMITS = frozenset(
    {
        "source.native",  # control fixture is not native CAD qualification
        "source.dependencies",  # fixture closure has no top_level mapping row
        "physics.mass_closure_equality",  # fixture backend has no assembly_mass_properties
        "physics.independent",  # fixture inertia payload carries no qualified API receipt
        "geometry.base_link",
        "geometry.arm_link",
        "geometry.assets",
        "geometry.expected_extent",
    }
)

#: Gates that must pass even on the control fixture: the rejection is about qualification only.
REQUIRED_CONTROL_GATES = (
    "bundle.subject",
    "input.valid",
    "source.native_discovery",
    "source.carrier_binding",
    "frames.native",
    "urdf.syntax_names",
    "joints.shoulder_pitch_joint",
    "urdf.topology",
    "consumer.urdf",
    "verification.complete",
    "geometry.native_tessellation",
)

QUALIFICATION = "control-fixture; not native CAD qualification"


def native_twin() -> dict:
    """The native-role twin of the real portable tool record, carrying the native pins."""

    portable = tool_record(role="portable")
    packages = {canonicalize_name(name): RUNTIME_VERSIONS[name] for name in required_packages("native")}
    return {
        **portable,
        "runtime": {
            "role": "native",
            "system": "Windows",
            "python": "3.12.10",
            "machine": "AMD64",
            "packages": packages,
        },
    }


def fixture_backend(prepared_package: Path, main_assembly: str) -> AxisFixtureBackend:
    """The one synthetic CAD backend for a real prepared package capture."""

    return AxisFixtureBackend(
        prepared_package / main_assembly,
        [
            {
                "name": "base-1",
                "transform": support.placement((0.0, 0.0, 0.0)),
                # Off-axis COM: the product signal proves the convention at capture time.
                "mass": fixture_mass_payload(0.192, (0.01, 0.02, 0.0)),
            },
            {
                "name": "arm-1",
                "transform": support.placement((0.0, 0.0, 0.1)),
                "mass": fixture_mass_payload(
                    0.105654866776462,
                    (0.0, 0.0, 0.05),
                ),
            },
        ],
        dependencies=[prepared_package / "cad/base.SLDPRT", prepared_package / "cad/arm.SLDPRT"],
        coordinate_systems={
            "CS_base_link": support.placement((0.0, 0.0, 0.0)),
            "CS_arm_link": support.placement((0.0, 0.0, 0.1)),
        },
    )


def build_fixture_capture(
    root: Path,
    *,
    run_id: str,
    main_assembly: str = "cad/robot.SLDASM",
    purpose: str = "engineering",
    runner=None,
    prior_freeze_events=None,
) -> dict:
    """Run the real producer over the control fixture and return the sealed transfer."""

    capture_entry = solidworks.capture if runner is None else runner
    root = Path(root)
    handoff_dir = root / "native"
    for name in ("cad/robot.SLDASM", "cad/base.SLDPRT", "cad/arm.SLDPRT"):
        path = handoff_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"placeholder {name}\n".encode())
    records = root / "records"
    limits = records / "joints" / "arm.json"
    limits.parent.mkdir(parents=True, exist_ok=True)
    limits.write_text(
        json.dumps({"limits": {"lower": -1.5, "upper": 1.5}, "drive": {"effort": 6.0, "velocity": 2.0}}),
        encoding="utf-8",
    )
    (records / "budget.json").write_text(
        json.dumps({"robot": {"expected_mass_kg": [0.28, 0.32], "expected_extent_m": [0.35, 0.5]}}),
        encoding="utf-8",
    )
    handoff = digest(package_inventory(handoff_dir))
    events: list[dict] = []

    def record(event: dict) -> None:
        events.append({"at": datetime.now(UTC).isoformat(), **event})

    if prior_freeze_events is None:
        steps.freeze_inputs(
            handoff_dir,
            handoff,
            package_inventory(handoff_dir),
            main_assembly=main_assembly,
            on_event=record,
        )
    else:
        events.extend(validate_freeze_events(prior_freeze_events))
    prepared_package, _target, prepared, prepared_files = steps.discover_structure(
        handoff_dir,
        root / "prepared",
        run_id,
        expected_digest=handoff,
        expected_files=package_inventory(handoff_dir),
        main_assembly=main_assembly,
        configuration={"record_roots": [str(records)], "purpose": purpose},
        targets={"nd_fixture": {"repository_slug": "example/m3.0", "base": "feature/m3.0"}},
        preparer=lambda *args, **kwargs: prepare_native_package(*args, backend=FakeBackend(capture_record()), **kwargs),
        on_event=record,
    )
    backend = fixture_backend(prepared_package, main_assembly)
    tool = native_twin()
    capture_output = root / "capture-delivery"
    with (
        patch.object(solidworks, "_native_tool_record", return_value=tool),
        patch("description_pipeline.runtime.tool_record", return_value=tool),
    ):
        capture = capture_entry(
            prepared_package,
            capture_output,
            backend=backend,
            run_id=run_id,
            handoff_sha256=handoff,
            upload_sha256=handoff,
            main_assembly=main_assembly,
            expected_inputs=prepared_files,
            prior_events=events,
        )
    if capture.get("state") != "native_complete" or capture.get("passed") is not False:
        raise RuntimeError(f"the control fixture capture did not complete: {capture.get('error')}")
    archive = capture_output / transfer.CAPTURE_ARCHIVE
    manifest = capture_output / transfer.CAPTURE_MANIFEST
    archive_receipt = capture.get("capture_archive")
    if not isinstance(archive_receipt, dict):
        raise RuntimeError("the control fixture capture carries no archive receipt")
    if (
        archive_receipt.get("name") != transfer.CAPTURE_ARCHIVE
        or archive_receipt.get("sha256") != file_digest(archive)
        or archive_receipt.get("size") != archive.stat().st_size
        or archive_receipt.get("manifest_sha256") != file_digest(manifest)
    ):
        raise RuntimeError("the control fixture capture archive receipt does not bind its bytes")
    return {
        "run_id": run_id,
        "handoff_sha256": handoff,
        "main_assembly": main_assembly,
        "hardware_id": prepared.hardware_id,
        "revision": prepared.revision,
        "native_tool": tool,
        "prepared_dir": str(prepared_package),
        "prepared_files": dict(prepared_files),
        "archive": archive,
        "manifest": manifest,
        # The exact capture-archive receipt the Windows endpoint serves at its
        # native_complete boundary is the producer's own schema, not a twin.
        "receipt": archive_receipt,
        "native_stages": json.loads((capture_output / "reports/native-stages.json").read_text(encoding="utf-8")),
        "qualification": QUALIFICATION,
    }
