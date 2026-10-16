"""Shared split-capture fixture data for the v1 suites (no product imports)."""
from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path

from description_pipeline.io import digest, file_digest

from .test_stage_transfer import MAIN_ASSEMBLY


SELECTED = {MAIN_ASSEMBLY: hashlib.sha256(b"CAD-BYTES").hexdigest()}
UPLOAD = {**SELECTED, "unrelated.txt": hashlib.sha256(b"not selected").hexdigest()}
HANDOFF = digest(SELECTED)


def capture_job(run_id: str, archive: Path, tool: dict) -> dict:
    """A current server receipt whose upload differs from its selected closure."""
    from tests.sources.selection_support import sealed_selection

    selection = sealed_selection(
        UPLOAD,
        MAIN_ASSEMBLY,
        reader_sha256=tool["source_sha256"],
        stage_root=r"C:\stage",
        edges={MAIN_ASSEMBLY: []},
    )
    with zipfile.ZipFile(archive) as packed:
        manifest_sha256 = hashlib.sha256(packed.read("transfer-manifest.json")).hexdigest()
    return {
        "status": "native_complete",
        "run_id": run_id,
        "main_assembly": MAIN_ASSEMBLY,
        "native_tool": tool,
        "handoff_sha256": HANDOFF,
        "package_files": dict(SELECTED),
        "request": {
            "run_id": run_id,
            "upload_files": dict(UPLOAD),
            "upload_sha256": digest(UPLOAD),
            "main_assembly": MAIN_ASSEMBLY,
        },
        "selection": {
            "state": "sealed",
            "reader_sha256": tool["source_sha256"],
            "upload_sha256": digest(UPLOAD),
            "staged_files": dict(UPLOAD),
            "manifest": selection,
        },
        "result": {
            "native_complete": True,
            "native_tool": tool,
            "capture_archive": {
                "name": "native-evidence.zip",
                "sha256": file_digest(archive),
                "size": archive.stat().st_size,
                "manifest_sha256": manifest_sha256,
            },
        },
    }


