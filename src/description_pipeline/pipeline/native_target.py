"""Read the one deployed native target identity from existing install facts."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from ..interface_records import (
    validate_config_snapshot,
    validate_install_record,
)
from ..io import parse_data
from .contracts import NativeTargetIdentity, ProtocolError


def load_registered_native_target(
    platform_snapshot: str | Path,
    *,
    snapshot_bytes: bytes | None = None,
) -> NativeTargetIdentity:
    """Return the target bound by the active snapshot and install record."""

    snapshot_path = Path(platform_snapshot)
    if (
        not snapshot_path.is_absolute()
        or not snapshot_path.is_file()
        or snapshot_path.is_symlink()
        or any(parent.is_symlink() for parent in snapshot_path.parents)
    ):
        raise ProtocolError(
            "native_target.snapshot_invalid",
            {"path": str(snapshot_path)},
        )
    if snapshot_bytes is None:
        snapshot_bytes = snapshot_path.read_bytes()
    elif not isinstance(snapshot_bytes, bytes) or not snapshot_bytes:
        raise ProtocolError(
            "native_target.snapshot_invalid",
            {"path": str(snapshot_path)},
        )
    try:
        snapshot = validate_config_snapshot(
            parse_data(snapshot_bytes, snapshot_path)
        )
    except Exception as error:
        raise ProtocolError(
            "native_target.snapshot_invalid",
            {"path": str(snapshot_path)},
        ) from error

    state_root = Path(snapshot["resolved"]["storage"]["state_root"])
    record_path = state_root / "install-record.json"
    if (
        not state_root.is_absolute()
        or not state_root.is_dir()
        or state_root.is_symlink()
        or any(parent.is_symlink() for parent in state_root.parents)
        or not record_path.is_file()
        or record_path.is_symlink()
    ):
        raise ProtocolError(
            "native_target.install_record_invalid",
            {"path": str(record_path)},
        )
    try:
        record = validate_install_record(
            parse_data(record_path.read_bytes(), record_path)
        )
    except Exception as error:
        raise ProtocolError(
            "native_target.install_record_invalid",
            {"path": str(record_path)},
        ) from error

    declared_snapshot_value = record["snapshot"]["path"]
    declared_snapshot = Path(declared_snapshot_value)
    if (
        not declared_snapshot.is_absolute()
        or not declared_snapshot.is_file()
        or declared_snapshot.is_symlink()
        or any(parent.is_symlink() for parent in declared_snapshot.parents)
        or ".." in declared_snapshot.parts
        or Path(os.path.abspath(declared_snapshot))
        != Path(os.path.abspath(snapshot_path))
    ):
        raise ProtocolError(
            "native_target.install_record_snapshot_mismatch",
            {"snapshot": str(snapshot_path)},
        )
    snapshot_sha256 = hashlib.sha256(snapshot_bytes).hexdigest()
    if (
        record["snapshot"]["digest"] != snapshot_sha256
        or record["snapshot"]["install_id"] != snapshot["install_id"]
        or record["ends"]["linux"]["config_digest"] != snapshot_sha256
        or record["paths"].get("state_root") != str(state_root)
    ):
        raise ProtocolError(
            "native_target.install_record_snapshot_mismatch",
            {"snapshot": str(snapshot_path)},
        )
    target = NativeTargetIdentity.from_record(
        record["ends"]["windows"]["target_identity"]
    )
    if target.install_id != record["ends"]["windows"]["install_id"]:
        raise ProtocolError(
            "native_target.install_record_target_mismatch",
            {"install_id": target.install_id},
        )
    return target


__all__ = ["load_registered_native_target"]
