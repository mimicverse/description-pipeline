"""Synthetic stage observations for transport tests; never native acceptance evidence."""

from datetime import UTC, datetime

from description_pipeline.stages import CONTRACT, STAGE_IDS
from description_pipeline.schemas import NATIVE_DISCOVERY_SCHEMA, NATIVE_DISCOVERY_FINDING_SCHEMA
from description_pipeline.io import digest


def discovery_finding(**fields):
    return {
        "schema_version": NATIVE_DISCOVERY_FINDING_SCHEMA,
        "code": "native.observation",
        "object": "assembly",
        "message": "Recorded native observation",
        "detail": None,
        "blocking": False,
        **fields,
    }


def native_discovery_record(
    run_id,
    findings=(),
    *,
    hardware_id="m3.0",
    revision="r1",
    main_assembly="robot.SLDASM",
    native_files=None,
):
    """A complete synthetic envelope for transport/display tests only."""
    native_files = (
        {"robot.SLDASM": "a" * 64}
        if native_files is None
        else dict(native_files)
    )
    return {
        "schema_version": NATIVE_DISCOVERY_SCHEMA,
        "contract": "native-discovery/v2",
        "namespace": "dp",
        "run_id": run_id,
        "generator": "native-discovery",
        "generator_version": "1",
        "handoff_sha256": digest(native_files),
        "native_files": native_files,
        "derivation": {
            "record_roots": [],
            "main_assembly": main_assembly,
            "configuration": "Default",
        },
        "identity": {
            "hardware_id": hardware_id,
            "revision": revision,
            "parent_revision": None,
            "owner": "fixture",
            "change_summary": "Transport fixture",
            "control": None,
            "delivery_configuration": "Default",
            "main_assembly": main_assembly,
            "robot_name": "fixture_robot",
            "robot_name_technical": False,
        },
        "records": [],
        "derived_interface_names": {},
        "raw": {},
        "findings": list(findings),
    }


def protocol_events(*, stages=STAGE_IDS, subject=None, failed_stage=None):
    events = []
    for definition in CONTRACT["stages"]:
        stage = definition["id"]
        if stage not in stages:
            continue
        for boundary in ("input", "output"):
            for check in definition[f"{boundary}_qc"]:
                failed = stage == failed_stage and boundary == "output"
                events.append(
                    {
                        "at": datetime.now(UTC).isoformat(),
                        "stage": stage,
                        "state": "failed" if failed else "running",
                        "check": {
                            "id": check["id"],
                            "boundary": boundary,
                            "state": "failed" if failed else "passed",
                            "details": {
                                "scope": "Synthetic transport protocol; no native qualification",
                                **({"subject_sha256": subject} if subject else {}),
                            },
                        },
                    }
                )
                if failed:
                    return events
        events.append({"at": datetime.now(UTC).isoformat(), "stage": stage, "state": "completed"})
    return events
