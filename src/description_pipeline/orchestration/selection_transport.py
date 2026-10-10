"""Admit requested source batches and bind a native dependency graph to staged bytes."""

from __future__ import annotations

import copy
import hashlib
import os
import stat
import tempfile
import zipfile
from pathlib import Path

from ..io import _filesystem_path, confined, digest, inventory
from ..sources.solidworks.handoff import MAX_HANDOFF_BYTES, _directory
from ..sources.solidworks.selection import SelectionError, selection_plan, validate_file_index, verify_selection

_CHUNK = 1024 * 1024


def _refuse(condition: bool, code: str, message: str) -> None:
    if not condition:
        raise SelectionError(code, message)


def _extract_batch(archive: Path, destination: Path, expected: dict, *, remaining: int) -> dict[str, int]:
    """Validate the whole member list before extracting any bounded, hash-checked bytes."""

    _refuse(
        0 < archive.stat().st_size <= MAX_HANDOFF_BYTES,
        "selection_batch_size",
        "Source batch archive exceeds its byte limit",
    )
    try:
        with zipfile.ZipFile(archive) as incoming:
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
            sizes = {}
            for member in members:
                name = member.filename
                target = Path(_filesystem_path(confined(destination, name, exists=False)))
                target.parent.mkdir(parents=True, exist_ok=True)
                count, checksum = 0, hashlib.sha256()
                with incoming.open(member) as source, target.open("xb") as output:
                    while block := source.read(min(_CHUNK, member.file_size - count + 1)):
                        count += len(block)
                        _refuse(
                            count <= member.file_size,
                            "selection_batch_size",
                            "Source member exceeds its declared size",
                        )
                        checksum.update(block)
                        output.write(block)
                _refuse(
                    count == member.file_size and checksum.hexdigest() == expected[name],
                    "selection_source_changed",
                    f"Source batch differs from the upload identity: {name}",
                )
                sizes[name] = count
            return sizes
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
        self.observations: dict[str, dict] = {}
        self.staged_files: dict[str, str] = {}
        self.sizes: dict[str, int] = {}
        self.accepted: dict[str, dict[str, str]] = {}
        self.pending: dict[str, str] = {}
        self.manifest: dict | None = None
        self.plan = selection_plan(self.upload_files, main_assembly, {})
        self.stage = _directory(stage, exists=False)
        self.stage.mkdir(parents=True, exist_ok=False)
        self.state = "awaiting_files"
        self.round_id = self._round_id()

    def _round_id(self) -> str:
        return digest(
            {
                "upload_sha256": self.plan["upload_sha256"],
                "main_assembly": self.main_assembly,
                "reader_sha256": self.reader_sha256,
                "observations": self.observations,
                "staged_files": self.staged_files,
            }
        )

    def snapshot(self) -> dict:
        snapshot = {
            "state": self.state,
            "round_id": self.round_id,
            "reader_sha256": self.reader_sha256,
            "observations": self.observations,
            "staged_files": self.staged_files,
            "requested_files": self.plan["requested_files"],
        }
        if self.manifest is not None:
            snapshot["manifest"] = self.manifest
        return copy.deepcopy(snapshot)

    def accept_batch(self, archive: Path, round_id: str) -> bool:
        """Install one exact request once; an identical logical retry does no native work."""

        retry = self.accepted.get(round_id)
        if retry is None:
            _refuse(
                self.state == "awaiting_files" and round_id == self.round_id,
                "selection_round_stale",
                "Source batch does not belong to the outstanding request",
            )
        expected = retry if retry is not None else self.plan["requested_files"]
        remaining = MAX_HANDOFF_BYTES if retry is not None else MAX_HANDOFF_BYTES - sum(self.sizes.values())
        with tempfile.TemporaryDirectory(prefix=".selection-batch-", dir=self.stage.parent) as temporary:
            incoming = Path(temporary)
            sizes = _extract_batch(Path(archive), incoming, expected, remaining=remaining)
            if retry is not None:
                return False
            installed = []
            try:
                for name in sorted(expected):
                    original = Path(_filesystem_path(confined(incoming, name)))
                    target = Path(_filesystem_path(confined(self.stage, name, exists=False)))
                    target.parent.mkdir(parents=True, exist_ok=True)
                    # Both directories are on the same volume. Linking installs without
                    # a second CAD copy and fails exclusively if a target already exists.
                    os.link(original, target)
                    installed.append(target)
            except BaseException:
                for target in installed:
                    target.unlink(missing_ok=True)
                raise
        self.staged_files.update(expected)
        self.sizes.update(sizes)
        self.accepted[round_id] = dict(expected)
        self.pending = dict(expected)
        self.state = "reading"
        return True

    def record_observations(self, observations: dict) -> None:
        """Accept only results for the already staged batch currently owned by the reader."""

        _refuse(
            self.state == "reading" and isinstance(observations, dict) and set(observations) == set(self.pending),
            "selection_observation_invalid",
            "Native observations must cover exactly the pending staged documents",
        )
        combined = {**self.observations, **observations}
        plan = selection_plan(self.upload_files, self.main_assembly, combined)
        _refuse(
            set(combined) <= self.staged_files.keys(),
            "selection_observation_invalid",
            "Native observations include documents whose bytes have not been staged",
        )
        self.observations = plan["observations"]
        self.plan = plan
        self.pending = {}
        self.state = "ready" if plan["state"] == "complete" else "awaiting_files"
        self.round_id = self._round_id()

    def seal(self, manifest: dict) -> dict:
        """Re-hash the owned stage and refuse a seal that changes the native observations."""

        _refuse(
            self.state in {"ready", "sealed"},
            "selection_incomplete",
            "Dependency queries remain outstanding",
        )
        actual = inventory(self.stage)
        _refuse(
            actual == self.staged_files,
            "selection_inventory_changed",
            "Selected source changed after transfer",
        )
        checked = verify_selection(manifest, self.upload_files, actual, reader_sha256=self.reader_sha256)
        _refuse(
            checked["observations"] == self.observations and checked["main_assembly"] == self.main_assembly,
            "selection_manifest_changed",
            "The requested seal differs from the native dependency observations",
        )
        self.manifest = checked
        self.state = "sealed"
        return copy.deepcopy(checked)
