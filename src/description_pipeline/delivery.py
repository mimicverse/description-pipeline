"""The delivery file boundary shared by verification and PR submission.

Target tree (contracts §交付格式与摘要): the subject is ``input/``, ``evidence/``,
``model/``, ``urdf/``, ``meshes/`` and ``tool.json``.  ``manifest.json`` and
``reports/quality.json`` stay outside the subject; every other file is refused.
"""

from __future__ import annotations

from pathlib import Path

from .io import PipelineError, confined, digest, file_digest, inventory, read_data, write_json
from .schemas import DELIVERY_MANIFEST_SCHEMA

PIPELINE_ID = "solidworks-to-urdf"

#: Publication bundle identity, retained until the producer side switches to the
#: delivery manifest in the same integration batch.
BUNDLE_SCHEMA = "solidworks-to-urdf.bundle/v1"

SUBJECT_DIRECTORIES = ("input", "evidence", "model", "urdf", "meshes")
SUBJECT_FILES = ("tool.json",)
#: The only files a complete delivery may carry outside the subject.
OUTSIDE_SUBJECT = ("manifest.json", "reports/quality.json")
MANIFEST = "manifest.json"
QUALITY_REPORT = "reports/quality.json"
SOURCE_RECORD = "input/source.json"
TOOL_RECORD = "tool.json"

#: Legacy receipts that now live in platform run storage.  Constants stay
#: importable until the producer side switches in the same integration batch.
TRANSFER_FILES = ("reports/native-tool.json", "reports/native-stages.json", "transfer-manifest.json")
MASS_CLOSURE_REPORT = "reports/mass-closure.json"


def _reject_root_links(root: Path) -> None:
    """No delivery entry (subject directory, receipt or manifest) may be a link."""

    for entry in root.iterdir():
        if entry.is_symlink() or entry.is_junction():
            raise PipelineError(f"Delivery entries must not be links or junctions: {entry.name}")


def subject_inventory(root: Path) -> dict[str, str]:
    """Bind the delivered inputs, captured evidence, model bytes and tool identity.

    Quality and PR receipts refer to this subject and cannot be part of their
    own digest. Git metadata and unrelated repository files are not model
    inputs. Every file inside a subject directory is bound, including unused
    assets; nothing inside these directories can be silently ignored.
    """

    root = Path(root)
    if root.is_symlink() or root.is_junction():
        raise PipelineError("A delivery cannot be a symlink or junction")
    root = root.resolve()
    _reject_root_links(root)
    files: dict[str, str] = {}
    for name in SUBJECT_DIRECTORIES:
        directory = root / name
        if not directory.exists():
            raise PipelineError(f"Missing delivery directory: {name}")
        if not directory.is_dir():
            raise PipelineError(f"Delivery directory is not a directory: {name}")
        files.update({f"{name}/{path}": checksum for path, checksum in inventory(directory).items()})
    for name in SUBJECT_FILES:
        files[name] = file_digest(confined(root, name))
    return dict(sorted(files.items()))


def subject_digest(root: Path) -> str:
    """SHA-256 of canonical JSON mapping relative filenames to file SHA-256."""

    return digest(subject_inventory(root))


def check_layout(root: Path) -> None:
    """Refuse unregistered files: subject, ``manifest.json``, ``reports/quality.json``."""

    root = Path(root)
    _reject_root_links(root)
    allowed = set(SUBJECT_DIRECTORIES) | set(SUBJECT_FILES) | {"manifest.json", "reports"}
    unknown = sorted(entry.name for entry in root.iterdir() if entry.name not in allowed)
    if unknown:
        raise PipelineError(f"Unregistered delivery entries: {unknown}")
    reports = root / "reports"
    if reports.exists():
        extra = sorted(
            path.relative_to(root).as_posix()
            for path in reports.rglob("*")
            if path.is_file() and path.relative_to(root).as_posix() != QUALITY_REPORT
        )
        if extra:
            raise PipelineError(f"Unregistered delivery files: {extra}")


def build_delivery_manifest(root: Path, *, contract: dict, policy: dict) -> dict:
    """``manifest.json`` payload: subject inventory/digest plus contract/policy identity."""

    from .interface_records import validate_delivery_manifest

    files = subject_inventory(root)
    manifest = {
        "schema_version": DELIVERY_MANIFEST_SCHEMA,
        "subject": {"files": files, "digest": digest(files)},
        "contract": contract,
        "policy": policy,
    }
    return validate_delivery_manifest(manifest)


def write_delivery_manifest(root: Path, *, contract: dict, policy: dict) -> dict:
    manifest = build_delivery_manifest(root, contract=contract, policy=policy)
    write_json(Path(root) / MANIFEST, manifest)
    return manifest


def read_delivery_manifest(root: Path) -> dict:
    """Read and validate ``manifest.json``, re-binding it to the current subject."""

    from .interface_records import validate_delivery_manifest

    root = Path(root)
    manifest = validate_delivery_manifest(read_data(confined(root, MANIFEST)))
    expected = subject_digest(root)
    if manifest["subject"]["digest"] != expected:
        raise PipelineError("Delivery subject digest differs from manifest.json")
    return manifest


def read_source_record(root: Path) -> dict:
    """Read and validate ``input/source.json`` (content identity, selection, metadata)."""

    from .interface_records import validate_source_record

    return validate_source_record(read_data(confined(Path(root), SOURCE_RECORD)))
