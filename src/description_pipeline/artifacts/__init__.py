"""Immutable artifact storage shared by all engineering stages."""

from .store import ArtifactStore, ArtifactStoreError

__all__ = ["ArtifactStore", "ArtifactStoreError"]
