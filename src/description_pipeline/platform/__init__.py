"""Application-side platform integration.

The store API here is the single in-application transaction entry for platform
state; the database schema, projection views and roles are the deploy side's
(``deploy/platform``).
"""

from .store import (
    AdmissionClosedError,
    AdmissionView,
    OperationConflictError,
    OperationRecord,
    PlatformStore,
    PlatformStoreError,
)
from .diagnostic import DiagnosticRuntimeConfig, DiagnosticStores, open_diagnostic_stores

__all__ = [
    "AdmissionClosedError",
    "AdmissionView",
    "DiagnosticRuntimeConfig",
    "DiagnosticStores",
    "OperationConflictError",
    "OperationRecord",
    "PlatformStore",
    "PlatformStoreError",
    "open_diagnostic_stores",
]
