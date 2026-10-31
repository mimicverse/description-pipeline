"""Shared Publish contract identities for admission and execution."""

from __future__ import annotations

from ..repository.publish import (
    REPOSITORY_SNAPSHOT_FILE,
    REPOSITORY_SNAPSHOT_METADATA,
    REPOSITORY_SNAPSHOT_SCHEMA,
)
from ..schemas import PUBLISH_REQUEST_SCHEMA

PUBLISH_OUTPUT_CONTRACT = "solidworks-to-urdf.publication-artifact/v2"
REPOSITORY_SNAPSHOT_KIND = "repository_snapshot"

__all__ = [
    "PUBLISH_OUTPUT_CONTRACT",
    "PUBLISH_REQUEST_SCHEMA",
    "REPOSITORY_SNAPSHOT_FILE",
    "REPOSITORY_SNAPSHOT_KIND",
    "REPOSITORY_SNAPSHOT_METADATA",
    "REPOSITORY_SNAPSHOT_SCHEMA",
]
