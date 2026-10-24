"""Explicit private-store composition for isolated diagnostic replay."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..artifacts import ArtifactStore
from .facts import FactStore, FactStoreError


@dataclass(frozen=True, slots=True)
class DiagnosticRuntimeConfig:
    """Explicit private database and artifact root; no production environment fallback."""

    database_dsn: str
    artifact_root: Path
    database_options: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.database_dsn, str) or not self.database_dsn:
            raise FactStoreError("A diagnostic database DSN must be supplied explicitly")
        if not isinstance(self.artifact_root, Path) or not self.artifact_root.is_absolute():
            raise FactStoreError("A diagnostic artifact root must be an explicit absolute path")
        if self.database_options is not None and (
            not isinstance(self.database_options, str) or "\x00" in self.database_options
        ):
            raise FactStoreError("Diagnostic database options must be safe text")


@dataclass(frozen=True, slots=True)
class DiagnosticStores:
    artifacts: ArtifactStore
    facts: FactStore


def open_diagnostic_stores(config: DiagnosticRuntimeConfig) -> DiagnosticStores:
    """Open a fresh FactStore/ArtifactStore pair from explicit private configuration."""

    if not isinstance(config, DiagnosticRuntimeConfig):
        raise FactStoreError("A DiagnosticRuntimeConfig is required")
    artifacts = ArtifactStore(config.artifact_root)

    def connect():
        import psycopg2

        return psycopg2.connect(
            config.database_dsn,
            options=config.database_options,
        )

    return DiagnosticStores(artifacts=artifacts, facts=FactStore(connect, artifacts))


__all__ = [
    "DiagnosticRuntimeConfig",
    "DiagnosticStores",
    "open_diagnostic_stores",
]
