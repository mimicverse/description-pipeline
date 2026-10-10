"""The file boundary shared by verification and PR submission."""

from __future__ import annotations

from pathlib import Path

from .io import PipelineError, confined, digest, file_digest, inventory

PIPELINE_ID = "solidworks-to-urdf"
INPUT_SCHEMA = "solidworks-to-urdf.input/v1"
BUNDLE_SCHEMA = "solidworks-to-urdf.bundle/v1"
SUBJECT_DIRECTORIES = ("input", "evidence", "model", "urdf", "meshes")
SUBJECT_FILES = ("README.md", "reports/input.json", "reports/tool.json")

#: Transferred native capture provenance.  A production capture that was sealed on Windows and
#: admitted on Linux carries all three; neutral local-backend fixtures may carry none.  A partial
#: set is an error: provenance must never be published without its bindings.
TRANSFER_FILES = ("reports/native-tool.json", "reports/native-stages.json", "transfer-manifest.json")


def subject_inventory(root: Path) -> dict[str, str]:
    """Bind the delivered inputs, captured evidence and actual model bytes.

    Quality and PR receipts refer to this subject and cannot be part of their
    own digest. Git metadata and unrelated repository files are not model
    inputs. Every file inside a subject directory is bound, including unused
    assets; nothing inside these directories can be silently ignored.  When a
    transfer provenance file is present, the complete triplet must be present
    and is bound too, so a published delivery can never omit its provenance.
    """

    root = Path(root)
    if root.is_symlink() or root.is_junction():
        raise PipelineError("A delivery cannot be a symlink or junction")
    root = root.resolve()
    files: dict[str, str] = {}
    for name in SUBJECT_DIRECTORIES:
        directory = root / name
        if not directory.exists():
            raise PipelineError(f"Missing delivery directory: {name}")
        if not directory.is_dir():
            raise PipelineError(f"Delivery directory is not a directory: {name}")
        files.update({f"{name}/{path}": checksum for path, checksum in inventory(directory).items()})
    for name in SUBJECT_FILES:
        path = confined(root, name)
        files[name] = file_digest(path)
    present = [name for name in TRANSFER_FILES if (root / name).is_file()]
    if present and len(present) != len(TRANSFER_FILES):
        missing = sorted(set(TRANSFER_FILES) - set(present))
        raise PipelineError(f"Transferred capture provenance is incomplete; missing: {missing}")
    for name in present:
        files[name] = file_digest(confined(root, name))
    return dict(sorted(files.items()))


def subject_digest(root: Path) -> str:
    """SHA-256 of canonical JSON mapping relative filenames to file SHA-256."""

    return digest(subject_inventory(root))
