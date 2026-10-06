"""The file boundary shared by verification and PR submission."""

from __future__ import annotations

from pathlib import Path

from .io import PipelineError, confined, digest, file_digest, inventory

PIPELINE_ID = "solidworks-to-urdf"
INPUT_SCHEMA = "solidworks-to-urdf.input/v1"
BUNDLE_SCHEMA = "solidworks-to-urdf.bundle/v1"
SUBJECT_DIRECTORIES = ("input", "evidence", "model", "urdf", "meshes")
SUBJECT_FILES = ("README.md", "reports/input.json", "reports/tool.json")


def subject_inventory(root: Path) -> dict[str, str]:
    """Bind the delivered inputs, captured evidence and actual model bytes.

    Quality and PR receipts refer to this subject and cannot be part of their
    own digest. Git metadata and unrelated repository files are not model
    inputs. Every file inside a subject directory is bound, including unused
    assets; nothing inside these directories can be silently ignored.
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
    return dict(sorted(files.items()))


def subject_digest(root: Path) -> str:
    """SHA-256 of canonical JSON mapping relative filenames to file SHA-256."""

    return digest(subject_inventory(root))
