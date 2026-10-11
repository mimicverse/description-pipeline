"""Admit requested source batches and bind a native dependency graph to staged bytes."""

from __future__ import annotations

import copy
import hashlib
import os
import stat
import tempfile
import zipfile
from contextlib import suppress
from pathlib import Path

from ..io import _filesystem_path, confined, digest, inventory
from ..sources.solidworks.handoff import MAX_HANDOFF_BYTES, _directory
from ..sources.solidworks.selection import SelectionError, seal_closure_selection, validate_file_index

_CHUNK = 1024 * 1024


def _refuse(condition: bool, code: str, message: str) -> None:
    if not condition:
        raise SelectionError(code, message)


def _validated_members(incoming: zipfile.ZipFile, expected: dict, remaining: int) -> list[zipfile.ZipInfo]:
    """Validate the whole member list before any member bytes are read or written."""

    members = incoming.infolist()
    names = [member.filename for member in members]
    _refuse(
        len(names) == len(expected) and len(set(names)) == len(names) and set(names) == set(expected),
        "selection_batch_inventory",
        "Source batch must contain exactly the requested files",
    )
    total = sum(member.file_size for member in members)
    _refuse(total <= remaining, "selection_batch_size", "Selected source exceeds its byte limit")
    for member in members:
        mode = stat.S_IFMT(member.external_attr >> 16)
        _refuse(
            not member.is_dir()
            and mode in {0, stat.S_IFREG}
            and not member.flag_bits & 1
            and member.compress_type in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED},
            "selection_batch_member",
            "Source batches require unencrypted regular files",
        )
    return members


def _read_member(incoming: zipfile.ZipFile, member: zipfile.ZipInfo, sink) -> tuple[int, str]:
    """Stream one bounded member, optionally into ``sink``, and hash what was read."""

    count, checksum = 0, hashlib.sha256()
    with incoming.open(member) as source:
        while block := source.read(min(_CHUNK, member.file_size - count + 1)):
            count += len(block)
            _refuse(count <= member.file_size, "selection_batch_size", "Source member exceeds its declared size")
            checksum.update(block)
            if sink is not None:
                sink.write(block)
    return count, checksum.hexdigest()


def _extract_batch(archive: Path, destination: Path, expected: dict, *, remaining: int) -> dict[str, int]:
    """Validate the whole member list before extracting any bounded, hash-checked bytes."""

    _refuse(
        0 < archive.stat().st_size <= MAX_HANDOFF_BYTES,
        "selection_batch_size",
        "Source batch archive exceeds its byte limit",
    )
    try:
        with zipfile.ZipFile(archive) as incoming:
            sizes = {}
            for member in _validated_members(incoming, expected, remaining):
                name = member.filename
                target = Path(_filesystem_path(confined(destination, name, exists=False)))
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("xb") as output:
                    count, checksum = _read_member(incoming, member, output)
                _refuse(
                    count == member.file_size and checksum == expected[name],
                    "selection_source_changed",
                    f"Source batch differs from the upload identity: {name}",
                )
                sizes[name] = count
            return sizes
    except (zipfile.BadZipFile, EOFError, RuntimeError) as error:
        raise SelectionError("selection_batch_invalid", f"Invalid source batch: {error}") from error


def _verify_batch(archive: Path, expected: dict, *, remaining: int) -> None:
    """Validate a logical retry against the staged index without touching the stage."""

    _refuse(
        0 < archive.stat().st_size <= MAX_HANDOFF_BYTES,
        "selection_batch_size",
        "Source batch archive exceeds its byte limit",
    )
    try:
        with zipfile.ZipFile(archive) as incoming:
            for member in _validated_members(incoming, expected, remaining):
                count, checksum = _read_member(incoming, member, None)
                _refuse(
                    count == member.file_size and checksum == expected[member.filename],
                    "selection_source_changed",
                    f"Source batch differs from the upload identity: {member.filename}",
                )
    except (zipfile.BadZipFile, EOFError, RuntimeError) as error:
        raise SelectionError("selection_batch_invalid", f"Invalid source batch: {error}") from error


class StagedSelection:
    """One serialized selection session; the endpoint owns locking and durable job records."""

    def __init__(self, stage: Path, upload_files: dict, main_assembly: str, *, reader_sha256: str):
        self.upload_files = validate_file_index(upload_files)
        # Reuse the same digest validator for the pinned reader identity.
        validate_file_index({"reader": reader_sha256})
        self.reader_sha256 = reader_sha256
        self.main_assembly = main_assembly
        self.staged_files: dict[str, str] = {}
        self.sizes: dict[str, int] = {}
        self.manifest: dict | None = None
        self.stage = _directory(stage, exists=False)
        self.stage.mkdir(parents=True, exist_ok=False)
        self.state = "awaiting_files"
        self.upload_sha256 = digest(self.upload_files)
        self.staged_identity: str | None = None
        self.sealed_evidence: object | None = None

    def snapshot(self) -> dict:
        snapshot = {
            "state": self.state,
            "reader_sha256": self.reader_sha256,
            "upload_sha256": self.upload_sha256,
            "staged_files": self.staged_files,
        }
        if self.manifest is not None:
            snapshot["manifest"] = self.manifest
        return copy.deepcopy(snapshot)

    def accept_batch(self, archive: Path) -> bool:
        """Locked contract: install the whole upload index in one provisional batch.

        A logical retry performs no native rework, but only after the incoming archive
        proves the identical member list and hashes against the staged index.
        """

        expected = dict(self.upload_files)
        if self.staged_identity == self.upload_sha256:
            _verify_batch(Path(archive), expected, remaining=MAX_HANDOFF_BYTES)
            return False
        _refuse(
            self.state == "awaiting_files",
            "selection_round_stale",
            "Provisional staging is no longer accepting the upload batch",
        )
        with tempfile.TemporaryDirectory(prefix=".selection-upload-", dir=self.stage.parent) as temporary:
            incoming = Path(temporary)
            sizes = _extract_batch(Path(archive), incoming, expected, remaining=MAX_HANDOFF_BYTES)
            installed = []
            try:
                for name in sorted(expected):
                    original = Path(_filesystem_path(confined(incoming, name)))
                    target = Path(_filesystem_path(confined(self.stage, name, exists=False)))
                    target.parent.mkdir(parents=True, exist_ok=True)
                    os.link(original, target)
                    installed.append(target)
            except BaseException:
                for target in installed:
                    target.unlink(missing_ok=True)
                raise
        self.staged_files = dict(expected)
        self.sizes = sizes
        self.staged_identity = self.upload_sha256
        self.state = "reading"
        return True

    def seal_closure(self, evidence: dict) -> dict:
        """Seal one native closure: prune only excluded bytes and freeze the selection.

        The provisional staged inventory is re-hashed against the full upload index, the
        closure graph is sealed by :func:`seal_closure_selection`, the excluded staged
        files are removed, and the retained bytes must equal the selected files exactly.
        ``snapshot()`` keeps the full original inventory as the upload proof.  Sealing is
        idempotent only for the identical evidence, because the excluded bytes no longer
        exist on disk once the closure is sealed.
        """

        if self.state == "sealed":
            _refuse(
                evidence == self.sealed_evidence,
                "selection_sealed",
                "The provisional stage is already sealed from other closure evidence",
            )
            return copy.deepcopy(self.manifest)
        _refuse(
            self.state == "reading" and self.staged_identity == self.upload_sha256,
            "selection_incomplete",
            "The full upload must be provisionally staged before sealing its closure",
        )
        actual = inventory(self.stage)
        _refuse(
            actual == self.upload_files,
            "selection_inventory_changed",
            "Provisional staging does not match the full upload index",
        )
        manifest = seal_closure_selection(
            self.upload_files, self.main_assembly, evidence, reader_sha256=self.reader_sha256
        )
        for name in sorted(self.upload_files):
            if name in manifest["files"]:
                continue
            with suppress(OSError, SelectionError):
                Path(_filesystem_path(confined(self.stage, name))).unlink()
        retained = inventory(self.stage)
        _refuse(
            retained == manifest["files"],
            "selection_inventory_changed",
            "Retained staged bytes differ from the sealed selection",
        )
        self.manifest = manifest
        self.sealed_evidence = copy.deepcopy(evidence)
        self.state = "sealed"
        return copy.deepcopy(manifest)
