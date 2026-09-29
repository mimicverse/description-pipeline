"""Compatibility alias; the native implementation lives in the installed package."""

import sys
from typing import TYPE_CHECKING
from description_pipeline.sources.solidworks import native as _implementation

if TYPE_CHECKING:
    from description_pipeline.sources.solidworks.native import (
        co_initialize as co_initialize,
        _win32 as _win32,
        _dynamic as _dynamic,
        _member as _member,
        _as_list as _as_list,
        normalize_document_path as normalize_document_path,
        document_paths_match as document_paths_match,
        _parallel_axis_terms as _parallel_axis_terms,
        _inertia_from_raw as _inertia_from_raw,
        transform_from_solidworks as transform_from_solidworks,
        _hash as _hash,
        _read_material as _read_material,
        _material_assignments_document as _material_assignments_document,
        SolidWorksBackend as SolidWorksBackend,
    )

sys.modules[__name__] = _implementation
