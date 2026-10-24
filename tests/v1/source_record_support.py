"""Canonical source-record fixtures for platform and recovery tests."""

from __future__ import annotations

from description_pipeline.interface_records import validate_source_record
from description_pipeline.io import digest
from description_pipeline.schemas import SOURCE_RECORD_SCHEMA


def source_record(
    *,
    hardware_id: str,
    handoff_digest: str,
    main_assembly: str,
    configuration: str = "Default",
    revision: str | None = None,
    records=(),
    hardware_source: str = "platform-config",
    change_summary: str = "fixture",
    change_summary_source: str = "submission",
) -> dict:
    identity = {
        "kind": "source-revision",
        "handoff_digest": handoff_digest,
        "main_assembly": main_assembly,
        "configuration": configuration,
        "hardware_id": hardware_id,
        "records": list(records),
    }
    return validate_source_record(
        {
            "schema_version": SOURCE_RECORD_SCHEMA,
            "source_revision_id": digest(identity),
            "identity": identity,
            "selection": {
                "main_assembly": main_assembly,
                "configuration": configuration,
                "hardware_id": hardware_id,
                "hardware_source": hardware_source,
            },
            "metadata": {
                "revision": revision,
                "owner": None,
                "control": None,
                "change_summary": change_summary,
                "change_summary_source": change_summary_source,
            },
        }
    )
