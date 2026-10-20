#!/usr/bin/env python3
"""Prototype exact-path disaster recovery for the description pipeline.

This file is intended to be reviewed as ``deploy/operator/recovery.py``.  It owns
the recovery contract; the legacy ``backup.py`` is reused only for its encrypted
tar stream, digest helper and private-staging cleanup.  ``backup.py open`` and
the legacy promotion manifest are not recovery authorities.

Backup is a maintenance-window operation.  On success it intentionally leaves
the platform admission closed, the supported Linux/Airflow writers stopped and
the Windows scheduled task disabled.  Operators must restore that window through
the controlled deployment start path after reviewing the evidence.

Commands:

    recovery.py backup --config PLATFORM_JSON --backup-id ID \
        --key-file EXTERNAL_KEY --out BACKUP_DIR \
        [--operatorctl OPERATORCTL]

    recovery.py restore --manifest RECOVERY_MANIFEST --key EXTERNAL_KEY \
        --target-plan TARGET_PLAN --status-dir STATUS_DIR

Only ``exact_path`` restoration is implemented.  The source and target must be
different PostgreSQL clusters, while every archived absolute root retains its
original absolute path on the target host.

``--out`` must name a fresh, absent directory.  An existing directory is never
silently reused or cleaned because its contents may be a successful backup.
"""

from __future__ import annotations

import argparse
import base64
import configparser
import contextlib
import errno
import fcntl
import hashlib
import json
import os
import re
import selectors
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

try:
    import backup
    import install_record
    import platform_config
    import render_operator
except ImportError as error:  # pragma: no cover - exercised by deployment layout
    raise SystemExit(
        f"recovery: deploy/operator must be importable next to recovery.py (missing dependency: {error})"
    ) from error


MANIFEST_SCHEMA = "solidworks-to-urdf.recovery-manifest/v4"
TARGET_SCHEMA = "solidworks-to-urdf.recovery-target/v1"
MARKER_SCHEMA = "solidworks-to-urdf.recovery-marker/v1"
RECEIPT_SCHEMA = "solidworks-to-urdf.recovery-receipt/v1"
MAINTENANCE_RECEIPT_SCHEMA = "solidworks-to-urdf.recovery-maintenance/v1"
SNAPSHOT_WORKER_SCHEMA = "solidworks-to-urdf.recovery-snapshot-worker/v4"
POSTGRES_MAJOR = "14"
DATABASE_NAME = "airflow_meta"
BOOTSTRAP_ROLE = "solidworks"
APPLICATION_ROLES = (
    "platform_migrate",
    "platform_service",
    "operator_ro",
    "airflow_migrate",
    "airflow_runtime",
)
PLATFORM_CORE_TABLES = (
    "admission",
    "request",
    "run",
    "stage",
    "operation",
    "run_event",
)
ARCHIVE_TOP_LEVEL = {
    "database",
    "maintenance",
    "config",
    "state",
    "handoff",
    "artifacts",
    "specifications",
    "release",
    "secrets",
    "reconstruct",
    "inventory",
}
HEX64 = re.compile(r"\A[0-9a-f]{64}\Z")
GIT_OBJECT = re.compile(r"\A[0-9a-f]{40}\Z")
BACKUP_ID = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
ROLE_NAME = re.compile(r"\A[A-Za-z_][A-Za-z0-9_$]{0,62}\Z")
CONTROL = re.compile(r"[\x00-\x1f\x7f]")
MAINTENANCE_LOCK_KEY = 0x5256435956323031
MAINTENANCE_LOCK_DIRNAME = "locks"
MAINTENANCE_LOCK_FILENAME = "maintenance.lock"
MAINTENANCE_UNITS = (
    "description-solidworks-tunnel",
    "description-airflow-dag-processor",
    "description-airflow-scheduler",
    "description-airflow-api-server",
    "description-operator-proxy",
    "description-portal",
)


class RecoveryError(Exception):
    """Stable fail-closed recovery error."""

    def __init__(self, code: str, message: str, detail: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail or {}


@dataclass
class BackupContext:
    """Validated inputs shared by CLI backup and the PG14 integration harness."""

    config_path: Path
    resolved: dict
    install_record: dict
    snapshot: dict
    release_document: dict
    derived_env: dict[str, str]
    workspace_plan: dict
    source_code: dict
    host_id: str
    postgres_root: Path


@dataclass
class CommandContext:
    bin_dir: Path
    library_dir: Path | None
    commands: list[dict]

    def run(
        self,
        argv: list[str | Path],
        *,
        env: dict[str, str] | None = None,
        timeout: int = 300,
    ) -> subprocess.CompletedProcess:
        command = [str(value) for value in argv]
        child_env = os.environ.copy()
        if self.library_dir is not None:
            current = child_env.get("LD_LIBRARY_PATH", "")
            child_env["LD_LIBRARY_PATH"] = f"{self.library_dir}:{current}" if current else str(self.library_dir)
        child_env.update(env or {})
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                env=child_env,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as error:
            self.commands.append(
                {
                    "program": Path(command[0]).name,
                    "returncode": None,
                    "error": type(error).__name__,
                }
            )
            raise RecoveryError(
                "recovery_command_failed",
                f"cannot execute {Path(command[0]).name}",
                {"program": Path(command[0]).name, "error": str(error)},
            ) from error
        self.commands.append(
            {
                "program": Path(command[0]).name,
                "returncode": completed.returncode,
            }
        )
        if completed.returncode != 0:
            stderr_tail = "\n".join(completed.stderr.strip().splitlines()[-8:])
            raise RecoveryError(
                "recovery_command_failed",
                f"{Path(command[0]).name} exited with {completed.returncode}",
                {
                    "program": Path(command[0]).name,
                    "returncode": completed.returncode,
                    "stderr": stderr_tail,
                },
            )
        return completed

    def run_stdout_bounded(
        self,
        argv: list[str | Path],
        *,
        max_stdout_bytes: int,
        timeout: int = 300,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess:
        command = [str(value) for value in argv]
        child_env = os.environ.copy()
        if self.library_dir is not None:
            current = child_env.get("LD_LIBRARY_PATH", "")
            child_env["LD_LIBRARY_PATH"] = f"{self.library_dir}:{current}" if current else str(self.library_dir)
        child_env.update(env or {})
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=child_env,
        )
        assert process.stdout is not None and process.stderr is not None
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")
        stdout = bytearray()
        stderr = bytearray()
        deadline = time.monotonic() + timeout
        returncode = None
        try:
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    process.kill()
                    process.wait()
                    raise RecoveryError(
                        "recovery_command_timeout",
                        f"{Path(command[0]).name} timed out",
                        {"program": Path(command[0]).name, "timeout": timeout},
                    )
                for key, _ in selector.select(min(remaining, 1.0)):
                    chunk = os.read(key.fileobj.fileno(), 64 * 1024)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        key.fileobj.close()
                        continue
                    if key.data == "stdout":
                        stdout.extend(chunk)
                        if len(stdout) > max_stdout_bytes:
                            process.kill()
                            process.wait()
                            raise RecoveryError(
                                "recovery_sequence_extraction_too_large",
                                "bounded subprocess stdout exceeded its limit",
                                {
                                    "program": Path(command[0]).name,
                                    "max_stdout_bytes": max_stdout_bytes,
                                },
                            )
                    elif len(stderr) < 64 * 1024:
                        stderr.extend(chunk[: 64 * 1024 - len(stderr)])
            returncode = process.wait(timeout=5)
        finally:
            selector.close()
            if process.poll() is None:
                process.kill()
                process.wait()
            for stream in (process.stdout, process.stderr):
                if stream is not None and not stream.closed:
                    stream.close()
        self.commands.append(
            {
                "program": Path(command[0]).name,
                "returncode": returncode,
                "stdout_bytes": len(stdout),
            }
        )
        if returncode != 0:
            raise RecoveryError(
                "recovery_command_failed",
                f"{Path(command[0]).name} exited with {returncode}",
                {
                    "program": Path(command[0]).name,
                    "returncode": returncode,
                    "stderr": stderr.decode("utf-8", errors="replace"),
                },
            )
        return subprocess.CompletedProcess(
            command,
            returncode,
            stdout.decode("utf-8", errors="replace"),
            stderr.decode("utf-8", errors="replace"),
        )


@dataclass
class MaintenanceLock:
    path: Path
    owner_id: str
    descriptor: int

    def close(self) -> None:
        if self.descriptor < 0:
            return
        with contextlib.suppress(OSError):
            fcntl.flock(self.descriptor, fcntl.LOCK_UN)
        with contextlib.suppress(OSError):
            os.close(self.descriptor)
        self.descriptor = -1


@dataclass(frozen=True)
class BackupOutputClaim:
    out: Path
    receipt_path: Path
    backup_id: str
    claim_id: str
    out_device: int
    out_inode: int


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def utc_timestamp(value: object, where: str) -> datetime:
    require(
        isinstance(value, str) and bool(value),
        "recovery_manifest_invalid",
        f"{where} is missing",
    )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise RecoveryError(
            "recovery_manifest_invalid",
            f"{where} is not an ISO-8601 timestamp",
        ) from error
    require(
        parsed.tzinfo is not None,
        "recovery_manifest_invalid",
        f"{where} has no timezone",
    )
    return parsed.astimezone(UTC)


def require(condition: bool, code: str, message: str, detail: dict | None = None) -> None:
    if not condition:
        raise RecoveryError(code, message, detail)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json(document: object) -> bytes:
    return (json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _pairs_no_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key!r}")
        result[key] = value
    return result


def strict_json_bytes(payload: bytes, what: str) -> object:
    try:
        return json.loads(payload.decode("utf-8"), object_pairs_hook=_pairs_no_duplicates)
    except (UnicodeDecodeError, ValueError) as error:
        raise RecoveryError(
            "recovery_manifest_invalid",
            f"{what} is not valid JSON: {error}",
        ) from error


def strict_json_file(path: Path, what: str) -> tuple[bytes, object]:
    require(
        path.is_file() and not path.is_symlink(),
        "recovery_manifest_invalid",
        f"{what} is not a regular file: {path}",
    )
    try:
        payload = path.read_bytes()
    except OSError as error:
        raise RecoveryError("recovery_manifest_invalid", f"cannot read {what}: {error}") from error
    return payload, strict_json_bytes(payload, what)


def atomic_write(path: Path, payload: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        mode,
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    os.chmod(path, mode)


def write_json_once(path: Path, document: object) -> None:
    payload = canonical_json(document)
    if path.exists():
        require(
            path.is_file() and not path.is_symlink() and path.read_bytes() == payload,
            "recovery_write_conflict",
            f"write-once output differs: {path}",
        )
        return
    atomic_write(path, payload)


def is_link(path: Path) -> bool:
    return path.is_symlink() or path.is_junction()


def absolute_text(value, where: str) -> Path:
    require(
        isinstance(value, str) and bool(value) and not CONTROL.search(value),
        "recovery_target_plan_invalid",
        f"{where} must be non-empty text",
    )
    path = Path(value)
    require(path.is_absolute(), "recovery_target_path_unsafe", f"{where} must be absolute")
    require(
        ".." not in Path(value).parts,
        "recovery_target_path_unsafe",
        f"{where} must not carry '..'",
    )
    return path


def require_no_links(path: Path, where: str) -> None:
    path = Path(path)
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if current.exists():
            require(
                not is_link(current),
                "recovery_target_path_unsafe",
                f"{where} crosses a symbolic link or junction: {current}",
            )


def require_real_file(path: Path, where: str, code: str) -> None:
    require_no_links(path, where)
    require(
        path.is_file(),
        code,
        f"{where} is missing or not a regular file: {path}",
    )


def require_real_dir(path: Path, where: str, code: str) -> None:
    require_no_links(path, where)
    require(
        path.is_dir(),
        code,
        f"{where} is missing or not a real directory: {path}",
    )


def maintenance_lock_path(state_root: Path) -> Path:
    root = Path(state_root)
    require(
        root.is_absolute() and root.is_dir(),
        "recovery_maintenance_unavailable",
        "the state root for the maintenance lock is missing",
        {"state_root": str(root)},
    )
    require_no_links(root, "state root")
    lock_dir = root / MAINTENANCE_LOCK_DIRNAME
    if not lock_dir.exists():
        ensure_parent = root
        require_real_dir(ensure_parent, "state root", "recovery_maintenance_unavailable")
        lock_dir.mkdir(mode=0o700)
    require_real_dir(lock_dir, "maintenance lock directory", "recovery_maintenance_unavailable")
    os.chmod(lock_dir, 0o700)
    path = lock_dir / MAINTENANCE_LOCK_FILENAME
    require_no_links(path, "maintenance lock")
    return path


def acquire_maintenance_lock(state_root: Path) -> MaintenanceLock:
    path = maintenance_lock_path(state_root)
    descriptor = os.open(
        path,
        os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o600,
    )
    try:
        metadata = os.fstat(descriptor)
        require(
            stat.S_ISREG(metadata.st_mode),
            "recovery_maintenance_unavailable",
            "the maintenance lock is not a regular file",
            {"path": str(path)},
        )
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if error.errno not in {errno.EACCES, errno.EAGAIN}:
                raise
            raise RecoveryError(
                "recovery_maintenance_not_exclusive",
                "another supported lifecycle operation owns the maintenance lock",
                {"lock": str(path)},
            ) from error
        os.chmod(path, 0o600)
        owner_id = os.urandom(32).hex()
        payload = canonical_json(
            {
                "schema_version": "solidworks-to-urdf.maintenance-lock/v1",
                "owner_id": owner_id,
                "acquired_at": utc_now(),
                "pid": os.getpid(),
            }
        )
        os.ftruncate(descriptor, 0)
        os.lseek(descriptor, 0, os.SEEK_SET)
        os.write(descriptor, payload)
        os.fsync(descriptor)
        return MaintenanceLock(
            path=path,
            owner_id=owner_id,
            descriptor=descriptor,
        )
    except BaseException:
        with contextlib.suppress(OSError):
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        with contextlib.suppress(OSError):
            os.close(descriptor)
        raise


def claim_backup_output(out: Path, backup_id: str) -> BackupOutputClaim:
    out = Path(out)
    require(out.is_absolute(), "recovery_target_path_unsafe", "--out must be absolute")
    require_no_links(out, "backup output")
    try:
        out.mkdir(parents=True, mode=0o700, exist_ok=False)
    except FileExistsError as error:
        raise RecoveryError(
            "recovery_write_conflict",
            f"backup output already exists and is not owned by this attempt: {out}",
        ) from error
    except OSError as error:
        raise RecoveryError(
            "recovery_write_failed",
            f"cannot create the backup output: {error}",
            {"out": str(out)},
        ) from error
    os.chmod(out, 0o700)
    out_metadata = os.stat(out, follow_symlinks=False)
    require(
        stat.S_ISDIR(out_metadata.st_mode),
        "recovery_target_path_unsafe",
        "backup output is not a real directory",
    )
    claim_id = os.urandom(32).hex()
    receipt_path = out / "receipt.json"
    receipt = {
        "schema_version": RECEIPT_SCHEMA,
        "operation": "backup",
        "backup_id": backup_id,
        "status": "running",
        "claim_id": claim_id,
        "started_at": utc_now(),
    }
    payload = canonical_json(receipt)
    try:
        descriptor = os.open(
            receipt_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(receipt_path, 0o600)
        except BaseException:
            receipt_path.unlink(missing_ok=True)
            raise
    except FileExistsError as error:
        raise RecoveryError(
            "recovery_write_conflict",
            "another operation claimed the backup output",
            {"out": str(out)},
        ) from error
    except OSError as error:
        raise RecoveryError(
            "recovery_write_failed",
            f"cannot claim the backup receipt: {error}",
            {"out": str(out)},
        ) from error
    return BackupOutputClaim(
        out=out,
        receipt_path=receipt_path,
        backup_id=backup_id,
        claim_id=claim_id,
        out_device=out_metadata.st_dev,
        out_inode=out_metadata.st_ino,
    )


def _claim_is_current(claim: BackupOutputClaim | None) -> bool:
    if claim is None:
        return False
    try:
        out_metadata = os.stat(claim.out, follow_symlinks=False)
        receipt_metadata = os.stat(claim.receipt_path, follow_symlinks=False)
        if (
            not stat.S_ISDIR(out_metadata.st_mode)
            or not stat.S_ISREG(receipt_metadata.st_mode)
            or out_metadata.st_dev != claim.out_device
            or out_metadata.st_ino != claim.out_inode
        ):
            return False
        _, document = strict_json_file(claim.receipt_path, "backup receipt claim")
        return (
            isinstance(document, dict)
            and document.get("schema_version") == RECEIPT_SCHEMA
            and document.get("operation") == "backup"
            and document.get("backup_id") == claim.backup_id
            and document.get("claim_id") == claim.claim_id
            and document.get("status") in {"running", "failed", "succeeded"}
        )
    except (OSError, RecoveryError):
        return False


def require_current_claim(claim: BackupOutputClaim | None) -> None:
    require(
        _claim_is_current(claim),
        "recovery_write_conflict",
        "the backup output claim is no longer owned by this attempt",
        {"out": str(claim.out) if claim is not None else None},
    )


def confined_member(name: object, where: str = "member") -> str:
    require(
        isinstance(name, str) and bool(name) and "\\" not in name and not name.startswith("/"),
        "recovery_manifest_invalid",
        f"{where} is not a confined POSIX relative path: {name!r}",
    )
    parts = name.split("/")
    require(
        all(part not in {"", ".", ".."} for part in parts),
        "recovery_manifest_invalid",
        f"{where} escapes its archive root: {name!r}",
    )
    return name


def clean_mode(mode: object) -> int:
    require(
        type(mode) is int and 0 <= mode <= 0o777,
        "recovery_manifest_invalid",
        "member mode must be permission bits",
    )
    return mode


def source_tree_identity(root: Path) -> dict:
    root = Path(root)
    require(
        root.is_dir(),
        "recovery_release_missing",
        f"producer source tree is missing: {root}",
    )

    def git(*args: str) -> str:
        try:
            return subprocess.check_output(
                ["git", "-C", str(root), *args],
                text=True,
                stderr=subprocess.PIPE,
            ).strip()
        except (OSError, subprocess.CalledProcessError) as error:
            raise RecoveryError(
                "recovery_release_missing",
                f"cannot establish producer source identity: {error}",
                {"source_tree": str(root)},
            ) from error

    commit = git("rev-parse", "HEAD")
    tree = git("rev-parse", "HEAD^{tree}")
    status = git("status", "--porcelain=v1")
    require(
        bool(re.fullmatch(r"[0-9a-f]{40}", commit)) and bool(re.fullmatch(r"[0-9a-f]{40}", tree)),
        "recovery_release_missing",
        "producer source identity is malformed",
    )
    require(
        status == "",
        "recovery_release_missing",
        "producer source tree is not clean",
        {"source_tree": str(root), "status": status.splitlines()[:20]},
    )
    return {"commit": commit, "tree": tree, "worktree": "clean"}


def load_backup_context(config_path: Path) -> BackupContext:
    """Resolve one configuration/install generation; all inputs are required."""

    config_path = Path(config_path)
    raw, document = platform_config.load_document(config_path)
    resolved = platform_config.validate_platform(document)
    state_root = Path(resolved["storage"]["state_root"])
    record = install_record.verify_active_generation(config_path, state_root)
    snapshot_path = Path(record["snapshot"]["path"])
    snapshot_bytes, snapshot = platform_config.load_document(snapshot_path)
    require(
        hashlib.sha256(snapshot_bytes).hexdigest() == record["snapshot"]["digest"],
        "recovery_manifest_invalid",
        "active snapshot digest differs from the install record",
    )
    require(
        snapshot.get("install_id") == record["install_id"]
        and snapshot.get("schema_version") == platform_config.SNAPSHOT_SCHEMA,
        "recovery_manifest_invalid",
        "active snapshot generation does not match the install record",
    )
    require(
        snapshot.get("sources", {}).get("platform_json_sha256") == hashlib.sha256(raw).hexdigest(),
        "recovery_manifest_invalid",
        "platform.json differs from the active snapshot",
    )
    require(
        snapshot.get("resolved", {}).get("storage") == resolved["storage"],
        "recovery_manifest_invalid",
        "snapshot and platform.json storage roots differ",
    )
    install_id = record["install_id"]
    env_path = state_root / "derived" / install_id / "operator.env"
    require_real_file(
        env_path,
        "active derived environment",
        "recovery_release_missing",
    )
    derived_env = render_operator.load_env(env_path)
    required_env = {
        "AIRFLOW_DB_URL",
        "AIRFLOW_RUNTIME_DB_URL",
        "PLATFORM_SERVICE_DSN",
        "PLATFORM_OPERATOR_DSN",
        "POSTGRES_ROOT",
        "POSTGRES_MAJOR",
        "PIPELINE_STORE_ROOT",
        "AIRFLOW_HOME",
        "AIRFLOW_VENV",
        "PIPELINE_WHEEL",
    }
    missing = sorted(required_env - set(derived_env))
    require(
        not missing,
        "recovery_release_missing",
        "active derived environment is incomplete",
        {"missing": missing},
    )
    require(
        derived_env["POSTGRES_MAJOR"] == POSTGRES_MAJOR,
        "recovery_postgres_version_mismatch",
        "active derived environment does not require PostgreSQL 14",
    )
    require(
        Path(derived_env["PIPELINE_STORE_ROOT"]) == Path(resolved["derived"]["store_root"]),
        "recovery_manifest_invalid",
        "derived store root differs from the active snapshot",
    )
    release_root = Path(resolved["storage"]["release_root"])
    release_path = release_root / "release.json"
    require_real_file(release_path, "release identity", "recovery_release_missing")
    release_document = install_record.release_identity(json.loads(release_path.read_text(encoding="utf-8")))
    require(
        release_document["source_commit"] == record["release"]["source_commit"]
        and release_document["source_sha256"] == record["release"]["source_sha256"],
        "recovery_release_missing",
        "release.json differs from the active install record",
    )
    expected_derived_paths = {
        "AIRFLOW_HOME": state_root / "airflow-home",
        "AIRFLOW_VENV": release_root / f"airflow-venv-{install_id}",
        "POSTGRES_ROOT": state_root / "airflow-pg",
    }
    for key, expected in expected_derived_paths.items():
        require(
            Path(derived_env[key]) == expected,
            "recovery_release_missing",
            f"active derived environment has an unexpected {key}",
        )
    require_real_dir(
        Path(derived_env["AIRFLOW_VENV"]),
        "active Airflow virtual environment",
        "recovery_release_missing",
    )
    install_record.wheel_binding(
        release_document,
        wheel=Path(derived_env["PIPELINE_WHEEL"]),
    )
    workspace_plan = workspace_reconstructible_plan(resolved)
    project_root = Path(__file__).resolve().parents[2]
    postgres_root = Path(derived_env["POSTGRES_ROOT"])
    return BackupContext(
        config_path=config_path,
        resolved=resolved,
        install_record=record,
        snapshot=snapshot,
        release_document=release_document,
        derived_env=derived_env,
        workspace_plan=workspace_plan,
        source_code=source_tree_identity(project_root),
        host_id=socket.gethostname(),
        postgres_root=postgres_root,
    )


def _git_output(repo: Path, *args: str) -> str:
    try:
        return subprocess.check_output(
            ["git", "-C", str(repo), *args],
            text=True,
            stderr=subprocess.PIPE,
        ).strip()
    except (OSError, subprocess.CalledProcessError) as error:
        raise RecoveryError(
            "recovery_release_missing",
            f"cannot read workspace reconstruction identity for {repo}: {error}",
        ) from error


def workspace_reconstructible_plan(resolved: dict) -> dict:
    repositories = resolved["derived"]["repositories"]
    rows = []
    for slug, raw_path in sorted(repositories.items()):
        path = Path(raw_path)
        require_real_dir(path, f"workspace checkout {slug}", "recovery_release_missing")
        require(
            path.joinpath(".git").exists(),
            "recovery_release_missing",
            f"workspace checkout is not a Git repository: {path}",
        )
        origin = _git_output(path, "remote", "get-url", "origin")
        head = _git_output(path, "rev-parse", "HEAD")
        status = _git_output(path, "status", "--porcelain=v1")
        expected = resolved["targets"]
        bases = [target["base"] for target in expected.values() if target["repository_slug"] == slug]
        require(
            bool(bases),
            "recovery_release_missing",
            f"workspace checkout has no configured target: {slug}",
        )
        require(
            status == "",
            "recovery_release_missing",
            f"workspace checkout is dirty: {path}",
            {"status": status.splitlines()[:20]},
        )
        rows.append(
            {
                "repository_slug": slug,
                "path": str(path),
                "origin": origin,
                "head": head,
                "bases": sorted(set(bases)),
                "status": "clean",
            }
        )
    return {
        "schema_version": "solidworks-to-urdf.workspace-rehydrate/v1",
        "repositories": rows,
    }


def parse_pg_dsn(dsn: str, where: str) -> dict:
    require(
        isinstance(dsn, str) and bool(dsn.strip()),
        "recovery_dsn_authority_mismatch",
        f"{where} is empty",
    )
    parts = urlsplit(dsn)
    require(
        parts.scheme in {"postgres", "postgresql", "postgresql+psycopg2"},
        "recovery_dsn_authority_mismatch",
        f"{where} is not a PostgreSQL URL",
    )
    query = parse_qs(parts.query, keep_blank_values=True)
    hosts = query.get("host", [])
    ports = query.get("port", [])
    require(
        len(hosts) == 1 and len(ports) == 1,
        "recovery_dsn_authority_mismatch",
        f"{where} must name exactly one managed socket and port",
    )
    host = unquote(hosts[0])
    require(
        host.startswith("/") and ".." not in Path(host).parts,
        "recovery_dsn_authority_mismatch",
        f"{where} must use an absolute Unix socket directory",
    )
    try:
        port = int(ports[0])
    except ValueError as error:
        raise RecoveryError("recovery_dsn_authority_mismatch", f"{where} port is malformed") from error
    require(
        0 < port < 65536,
        "recovery_dsn_authority_mismatch",
        f"{where} port is out of range",
    )
    user = unquote(parts.username or query.get("user", [""])[0])
    database = unquote(parts.path.lstrip("/"))
    password = unquote(parts.password) if parts.password is not None else None
    require(
        bool(user) and bool(database),
        "recovery_dsn_authority_mismatch",
        f"{where} must carry a role and database",
    )
    return {
        "socket_dir": host,
        "port": port,
        "user": user,
        "database": database,
        "password": password,
    }


def command_context(bin_dir: Path, library_dir: Path | None, commands: list[dict]) -> CommandContext:
    bin_dir = Path(bin_dir)
    if library_dir is not None:
        require_real_dir(
            Path(library_dir),
            "PostgreSQL library directory",
            "recovery_postgres_version_mismatch",
        )
    for name in ("psql", "pg_dump", "pg_restore", "pg_controldata"):
        require_real_file(
            bin_dir / name,
            f"PostgreSQL tool {name}",
            "recovery_postgres_version_mismatch",
        )
    return CommandContext(bin_dir=bin_dir, library_dir=library_dir, commands=commands)


def psql(
    context: CommandContext,
    *,
    socket_dir: Path,
    port: int,
    user: str,
    database: str,
    query: str | None = None,
    file: Path | None = None,
    password: str | None = None,
) -> str:
    argv = [
        context.bin_dir / "psql",
        "-X",
        "-q",
        "-A",
        "-t",
        "-v",
        "ON_ERROR_STOP=1",
        "-h",
        socket_dir,
        "-p",
        str(port),
        "-U",
        user,
        "-d",
        database,
    ]
    if query is not None:
        argv.extend(["-c", query])
    if file is not None:
        argv.extend(["-f", file])
    password_env = {"PGPASSWORD": password} if password is not None else {}
    completed = context.run(argv, env=password_env)
    return completed.stdout.strip()


def psql_json(
    context: CommandContext,
    *,
    socket_dir: Path,
    port: int,
    user: str,
    database: str,
    query: str,
    password: str | None = None,
) -> object:
    output = psql(
        context,
        socket_dir=socket_dir,
        port=port,
        user=user,
        database=database,
        query=query,
        password=password,
    )
    try:
        return json.loads(output)
    except ValueError as error:
        raise RecoveryError(
            "recovery_database_readback_failed",
            "PostgreSQL readback did not return valid JSON",
        ) from error


def _psycopg2():
    try:
        import psycopg2
    except ImportError as error:
        raise RecoveryError(
            "recovery_airflow_interpreter_required",
            "backup must run with the AIRFLOW_VENV interpreter providing psycopg2",
        ) from error
    return psycopg2


def _db_connect(
    authority: dict,
    *,
    autocommit: bool = False,
):
    connection = _psycopg2().connect(
        dbname=authority["database"],
        user=BOOTSTRAP_ROLE,
        host=authority["socket_dir"],
        port=authority["port"],
    )
    connection.autocommit = autocommit
    return connection


def _connection_json(connection, query: str, parameters=None) -> object:
    with connection.cursor() as cursor:
        cursor.execute(query, parameters)
        row = cursor.fetchone()
    require(
        row is not None and len(row) == 1,
        "recovery_database_readback_failed",
        "database query did not return one JSON value",
    )
    value = row[0]
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError as error:
            raise RecoveryError(
                "recovery_database_readback_failed",
                "database query returned malformed JSON",
            ) from error
    return value


def _connection_scalar(connection, query: str, parameters=None):
    with connection.cursor() as cursor:
        cursor.execute(query, parameters)
        row = cursor.fetchone()
    require(
        row is not None and len(row) == 1,
        "recovery_database_readback_failed",
        "database query did not return one value",
    )
    return row[0]


DATABASE_SETTINGS_QUERY = """
SELECT COALESCE(json_agg(json_build_object(
  'role', CASE WHEN s.setrole = 0 THEN NULL ELSE r.rolname END,
  'database', d.datname,
  'config', s.setconfig
) ORDER BY r.rolname NULLS FIRST, d.datname, s.setconfig::text), '[]'::json)
FROM pg_db_role_setting s
LEFT JOIN pg_roles r ON r.oid = s.setrole
JOIN pg_database d ON d.oid = s.setdatabase
WHERE d.datname = 'airflow_meta'
"""

MAINTENANCE_QUERY = """
SELECT json_build_object(
  'admission', (
    SELECT json_build_object(
      'state', state,
      'reason', reason,
      'actor', actor,
      'revision', revision
    )
    FROM platform.admission
    WHERE id = 1
  ),
  'settled', NOT EXISTS (
    SELECT 1
    FROM platform.lifecycle_projection
    WHERE object_kind <> 'request' AND bucket <> 'terminal'
  ),
  'unsettled_count', (
    SELECT count(*)
    FROM platform.lifecycle_projection
    WHERE object_kind <> 'request' AND bucket <> 'terminal'
  )
)
"""

ACTIVE_APPLICATION_SESSIONS_QUERY = """
SELECT COALESCE(json_agg(json_build_object(
  'pid', pid,
  'role', usename,
  'state', state,
  'xact_start', xact_start,
  'application_name', application_name
) ORDER BY pid), '[]'::json)
FROM pg_stat_activity
WHERE datname = current_database()
  AND usename = ANY(%s)
  AND pid <> pg_backend_pid()
  AND (state = 'active' OR xact_start IS NOT NULL)
"""


def _roles_settings_snapshot(connection) -> dict:
    roles = _connection_json(connection, ROLE_QUERY)
    memberships = _connection_json(connection, MEMBERSHIP_QUERY)
    settings = _connection_json(connection, DATABASE_SETTINGS_QUERY)
    all_roles = _connection_json(connection, ALL_NON_SYSTEM_ROLES_QUERY)
    require(
        isinstance(roles, list)
        and isinstance(memberships, list)
        and isinstance(settings, list)
        and isinstance(all_roles, list),
        "recovery_database_readback_failed",
        "role/settings snapshot is malformed",
    )
    names = {row.get("name") for row in roles if isinstance(row, dict)}
    require(
        set(all_roles) == {BOOTSTRAP_ROLE, *APPLICATION_ROLES},
        "recovery_unsupported_role_set",
        "source cluster contains an unsupported non-system role set",
        {
            "missing": sorted({BOOTSTRAP_ROLE, *APPLICATION_ROLES} - set(all_roles)),
            "unexpected": sorted(set(all_roles) - {BOOTSTRAP_ROLE, *APPLICATION_ROLES}),
        },
    )
    require(
        names == {BOOTSTRAP_ROLE, *APPLICATION_ROLES},
        "recovery_release_missing",
        "source role/settings snapshot is incomplete",
    )
    return {
        "roles": roles,
        "memberships": memberships,
        "database_settings": settings,
        "non_system_roles": all_roles,
    }


def _maintenance_probe(connection) -> dict:
    status = _connection_json(connection, MAINTENANCE_QUERY)
    sessions = _connection_json(
        connection,
        ACTIVE_APPLICATION_SESSIONS_QUERY,
        (list(APPLICATION_ROLES),),
    )
    wal_lsn = _connection_scalar(connection, "SELECT pg_current_wal_lsn()::text")
    require(
        isinstance(status, dict)
        and isinstance(status.get("admission"), dict)
        and isinstance(status.get("settled"), bool)
        and type(status.get("unsettled_count")) is int
        and isinstance(sessions, list)
        and isinstance(wal_lsn, str),
        "recovery_database_readback_failed",
        "maintenance/read quiescence probe is malformed",
    )
    return {
        **status,
        "active_application_sessions": sessions,
        "wal_lsn": wal_lsn,
    }


def _require_quiescent(probe: dict, phase: str) -> None:
    require(
        probe["admission"].get("state") == "closed",
        "recovery_maintenance_not_quiesced",
        "backup requires platform admission closed",
        {"phase": phase, "admission": probe["admission"]},
    )
    require(
        probe["settled"] is True and probe["unsettled_count"] == 0,
        "recovery_maintenance_not_settled",
        "backup requires no in-flight, unresolved or unknown work",
        {"phase": phase, "unsettled_count": probe["unsettled_count"]},
    )
    require(
        probe["active_application_sessions"] == [],
        "recovery_maintenance_not_exclusive",
        "application-role transactions are active during the backup window",
        {
            "phase": phase,
            "sessions": probe["active_application_sessions"][:20],
        },
    )


def _digest_document(document: object) -> str:
    return hashlib.sha256(canonical_json(document)).hexdigest()


def _maintenance_state_identity(probe: dict) -> dict:
    return {
        "admission": probe["admission"],
        "settled": probe["settled"],
        "unsettled_count": probe["unsettled_count"],
        "active_application_sessions": probe["active_application_sessions"],
    }


def _begin_exported_snapshot(connection) -> str:
    connection.set_session(
        isolation_level="REPEATABLE READ",
        readonly=True,
        autocommit=False,
    )
    snapshot_id = _connection_scalar(connection, "SELECT pg_export_snapshot()")
    require(
        isinstance(snapshot_id, str) and bool(snapshot_id),
        "recovery_database_readback_failed",
        "PostgreSQL did not export a valid snapshot id",
    )
    return snapshot_id


def _table_fingerprint(
    connection,
    schema: str,
    table: str,
    *,
    allow_discovered: bool = False,
) -> dict:
    value = _connection_json(
        connection,
        _table_fingerprint_query(
            schema,
            table,
            allow_discovered=allow_discovered,
        ),
    )
    require(
        isinstance(value, dict) and set(value) == {"rows", "fingerprint"},
        "recovery_database_readback_failed",
        f"table baseline is malformed: {schema}.{table}",
    )
    return value


def _sequence_baseline(connection) -> list[dict]:
    rows = _connection_json(
        connection,
        (
            "SELECT COALESCE(json_agg(json_build_object("
            "'schema', schemaname, 'name', sequencename, "
            "'increment_by', increment_by::text, 'cycle', cycle"
            ") ORDER BY schemaname, sequencename), '[]'::json) "
            "FROM pg_sequences WHERE schemaname IN ('platform', 'airflow')"
        ),
    )
    require(
        isinstance(rows, list),
        "recovery_database_readback_failed",
        "sequence catalog readback is malformed",
    )
    result = []
    for row in rows:
        require(
            isinstance(row, dict)
            and set(row) == {"schema", "name", "increment_by", "cycle"}
            and row["schema"] in {"platform", "airflow"}
            and isinstance(row["name"], str),
            "recovery_database_readback_failed",
            "sequence catalog entry is malformed",
        )
        require(
            bool(ROLE_NAME.fullmatch(row["schema"])) and bool(ROLE_NAME.fullmatch(row["name"])),
            "recovery_sequence_identifier_unsupported",
            "recovery sequence names must be unquoted safe identifiers",
            {
                "schema": row["schema"],
                "sequence": row["name"],
            },
        )
        state = _connection_json(
            connection,
            (
                "SELECT json_build_object("
                "'last_value', last_value::text, 'is_called', is_called) "
                f"FROM {quote_ident(row['schema'])}.{quote_ident(row['name'])}"
            ),
        )
        require(
            isinstance(state, dict)
            and set(state) == {"last_value", "is_called"}
            and isinstance(state["is_called"], bool),
            "recovery_database_readback_failed",
            "sequence state readback is malformed",
        )
        result.append({**row, **state})
    return result


SETVAL_PATTERN = re.compile(
    r"SELECT pg_catalog\.setval\('((?:''|[^'])+)'(?:::regclass)?,\s*"
    r"([+-]?[0-9]+),\s*(true|false)\);"
)


def sequence_positions(rows: list[dict]) -> list[dict]:
    positions = []
    for row in rows:
        require(
            isinstance(row, dict)
            and set(row)
            >= {
                "schema",
                "name",
                "last_value",
                "is_called",
            }
            and row["schema"] in {"platform", "airflow"}
            and isinstance(row["name"], str)
            and isinstance(row["last_value"], str)
            and isinstance(row["is_called"], bool),
            "recovery_database_readback_failed",
            "sequence position record is malformed",
        )
        positions.append(
            {
                "name": f"{row['schema']}.{row['name']}",
                "last_value": row["last_value"],
                "is_called": row["is_called"],
            }
        )
    return sorted(positions, key=lambda row: row["name"])


def extract_dump_sequence_positions_streaming(
    context: CommandContext,
    dump_path: Path,
    sequence_rows: list[dict],
    toc_text: str,
    work_dir: Path,
) -> tuple[list[dict], dict]:
    positions = []
    evidence = {
        "method": "pg_restore --data-only -L=<per-sequence-toc> -f=-",
        "plaintext_dump_file_created": False,
        "full_dump_materialized": False,
        "temporary_toc_files_removed": True,
        "per_sequence": [],
    }
    toc_lines = toc_text.splitlines()
    for expected in sequence_positions(sequence_rows):
        table = expected["name"]
        schema, name = table.split(".", 1)
        require(
            bool(ROLE_NAME.fullmatch(schema)) and bool(ROLE_NAME.fullmatch(name)),
            "recovery_sequence_identifier_unsupported",
            "recovery sequence identifiers must be unquoted safe identifiers",
            {"sequence": table},
        )
        toc_pattern = re.compile(rf"\bSEQUENCE SET\s+{re.escape(schema)}\s+{re.escape(name)}(?:\s|$)")
        selected = [line for line in toc_lines if toc_pattern.search(line)]
        require(
            len(selected) == 1,
            "recovery_sequence_dump_mismatch",
            "custom dump TOC does not contain exactly one sequence-set entry",
            {"sequence": table, "matches": len(selected)},
        )
        temporary = tempfile.TemporaryDirectory(
            prefix="sequence-toc-",
            dir=work_dir,
        )
        try:
            list_path = Path(temporary.name) / "sequence.toc"
            header = [line for line in toc_lines if line.startswith(";")]
            atomic_write(
                list_path,
                ("\n".join([*header, selected[0]]) + "\n").encode(),
            )
            completed = context.run_stdout_bounded(
                [
                    context.bin_dir / "pg_restore",
                    "--data-only",
                    "-L",
                    list_path,
                    "-f",
                    "-",
                    dump_path,
                ],
                max_stdout_bytes=1024 * 1024,
                timeout=300,
            )
        finally:
            temporary.cleanup()
        stdout = completed.stdout
        require(
            "COPY " not in stdout and "INSERT INTO" not in stdout,
            "recovery_sequence_dump_mismatch",
            "sequence extraction returned non-sequence data",
            {"sequence": table},
        )
        matches = SETVAL_PATTERN.findall(stdout)
        require(
            len(matches) == 1 and matches[0][0].replace("''", "'") == table,
            "recovery_sequence_dump_mismatch",
            "pg_restore did not emit exactly one matching sequence setval",
            {"sequence": table, "matches": len(matches)},
        )
        raw_name, raw_value, raw_called = matches[0]
        positions.append(
            {
                "name": raw_name.replace("''", "'"),
                "last_value": raw_value,
                "is_called": raw_called == "true",
            }
        )
        evidence["per_sequence"].append(
            {
                "sequence": table,
                "stdout_bytes": len(stdout.encode("utf-8")),
            }
        )
    return sorted(positions, key=lambda row: row["name"]), evidence


def verify_sequence_dump_positions(
    sequence_rows: list[dict],
    dump_positions: list[dict],
) -> None:
    expected = sequence_positions(sequence_rows)
    require(
        dump_positions == expected,
        "recovery_sequence_dump_mismatch",
        "pg_dump sequence setval output differs from the post-dump sequence state",
        {
            "expected": expected,
            "actual": dump_positions,
        },
    )


def _checkpoint_references(connection) -> list[dict]:
    rows = _connection_json(
        connection,
        (
            "SELECT COALESCE(json_agg(json_build_object("
            "'run_id', run_id, 'stage', stage, "
            "'checkpoint_uri', checkpoint_uri, 'receipt', receipt "
            ") ORDER BY run_id, stage), '[]'::json) "
            "FROM platform.stage "
            "WHERE checkpoint_uri IS NOT NULL AND checkpoint_uri <> ''"
        ),
    )
    require(
        isinstance(rows, list),
        "recovery_database_readback_failed",
        "checkpoint reference readback is malformed",
    )
    return rows


def _discovered_table_fingerprints(connection) -> dict[str, dict]:
    table_rows = _connection_json(connection, FULL_TABLE_LIST_QUERY)
    require(
        isinstance(table_rows, list),
        "recovery_database_readback_failed",
        "full table discovery is malformed",
    )
    tables = {}
    for table_row in table_rows:
        require(
            isinstance(table_row, dict)
            and set(table_row) == {"schema", "table"}
            and table_row["schema"] in {"platform", "airflow"}
            and isinstance(table_row["table"], str)
            and bool(ROLE_NAME.fullmatch(table_row["table"])),
            "recovery_catalog_identifier_unsupported",
            "unsupported table identifier in full data baseline",
            {"table": table_row},
        )
        name = f"{table_row['schema']}.{table_row['table']}"
        tables[name] = _table_fingerprint(
            connection,
            table_row["schema"],
            table_row["table"],
            allow_discovered=True,
        )
    return tables


def _live_database_data(connection) -> dict:
    alembic = _connection_json(
        connection,
        ("SELECT json_agg(version_num ORDER BY version_num) FROM airflow.alembic_version"),
    )
    require(
        isinstance(alembic, list) and bool(alembic),
        "recovery_database_readback_failed",
        "Airflow alembic ledger is empty",
    )
    return {
        "tables": _discovered_table_fingerprints(connection),
        "sequences": _sequence_baseline(connection),
        "airflow_alembic_heads": alembic,
    }


def _database_baseline_snapshot(connection) -> tuple[dict, dict, list[dict]]:
    legacy_catalog = _connection_json(connection, CATALOG_QUERY)
    full_catalog = _connection_json(connection, FULL_CATALOG_QUERY)
    require(
        isinstance(legacy_catalog, dict)
        and legacy_catalog.get("database")
        and isinstance(full_catalog, dict)
        and full_catalog.get("schemas") is not None,
        "recovery_database_readback_failed",
        "database catalog baseline is incomplete",
    )
    require(
        legacy_catalog["public_create"] is False,
        "recovery_database_readback_failed",
        "PUBLIC still has CREATE on airflow_meta",
    )
    catalog = {
        "database": legacy_catalog["database"],
        "settings": legacy_catalog["settings"],
        "public_create": legacy_catalog["public_create"],
        "platform_service_airflow_usage": legacy_catalog["platform_service_airflow_usage"],
        "platform_service_airflow_create": legacy_catalog["platform_service_airflow_create"],
        "catalog": full_catalog,
    }
    data = _live_database_data(connection)
    return catalog, data, _checkpoint_references(connection)


def pg_control(path: Path, context: CommandContext) -> dict:
    completed = context.run([context.bin_dir / "pg_controldata", "-D", path])
    values = {}
    for line in completed.stdout.splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            values[key.strip()] = value.strip()
    system_identifier = values.get("Database system identifier")
    require(
        isinstance(system_identifier, str) and bool(re.fullmatch(r"[0-9]+", system_identifier)),
        "recovery_postgres_version_mismatch",
        "PostgreSQL system identifier is unreadable",
    )
    return {
        "system_identifier": system_identifier,
        "catalog_version": values.get("Catalog version number"),
    }


def postgres_major(bin_dir: Path, context: CommandContext) -> str:
    completed = context.run([bin_dir / "psql", "--version"])
    match = re.search(r"PostgreSQL\)\s+(\d+)\.", completed.stdout)
    require(
        match is not None,
        "recovery_postgres_version_mismatch",
        "cannot determine PostgreSQL client major",
    )
    return match.group(1)


def database_probe(
    context: CommandContext,
    dsn: dict,
) -> dict:
    document = psql_json(
        context,
        socket_dir=Path(dsn["socket_dir"]),
        port=dsn["port"],
        user=dsn["user"],
        database=dsn["database"],
        password=dsn["password"],
        query=(
            "SELECT json_build_object("
            "'database', current_database(),"
            "'database_oid', (SELECT oid::text FROM pg_database "
            "WHERE datname = current_database()),"
            "'role', current_user,"
            "'server_version_num', current_setting('server_version_num'))"
        ),
    )
    require(
        isinstance(document, dict),
        "recovery_dsn_authority_mismatch",
        "database authority probe is malformed",
    )
    return document


def capture_database_authority(
    config: dict,
    derived_env: dict[str, str],
    *,
    postgres_root: Path,
    context: CommandContext,
) -> dict:
    dsn_sources = {
        "AIRFLOW_DB_URL": derived_env["AIRFLOW_DB_URL"],
        "AIRFLOW_RUNTIME_DB_URL": derived_env["AIRFLOW_RUNTIME_DB_URL"],
        "PLATFORM_SERVICE_DSN": derived_env["PLATFORM_SERVICE_DSN"],
        "PLATFORM_OPERATOR_DSN": derived_env["PLATFORM_OPERATOR_DSN"],
    }
    dsn_path = Path(config["database"]["dsn_file"])
    require_real_file(dsn_path, "database DSN file", "recovery_release_missing")
    dsn_sources["database.dsn_file"] = dsn_path.read_text(encoding="utf-8").strip()
    parsed = {name: parse_pg_dsn(value, name) for name, value in sorted(dsn_sources.items())}
    probes = {name: database_probe(context, dsn) for name, dsn in parsed.items()}
    endpoints = {
        (dsn["socket_dir"], dsn["port"], dsn["database"], probe["database_oid"])
        for dsn, probe in ((parsed[name], probes[name]) for name in sorted(parsed))
    }
    require(
        len(endpoints) == 1,
        "recovery_dsn_authority_mismatch",
        "configured DSNs do not identify exactly one database authority",
        {"authorities": len(endpoints)},
    )
    socket_dir, port, database, database_oid = next(iter(endpoints))
    require(
        database == DATABASE_NAME,
        "recovery_dsn_authority_mismatch",
        f"the supported recovery database is {DATABASE_NAME}",
    )
    versions = {probe["server_version_num"] for probe in probes.values()}
    require(
        len(versions) == 1,
        "recovery_dsn_authority_mismatch",
        "configured DSNs observe different server versions",
    )
    version_num = next(iter(versions))
    require(
        isinstance(version_num, str) and version_num.startswith(POSTGRES_MAJOR),
        "recovery_postgres_version_mismatch",
        f"source database is not PostgreSQL {POSTGRES_MAJOR}",
    )
    data_dir = postgres_root / "data"
    controls = pg_control(data_dir, context)
    endpoints_record = []
    for name, dsn in sorted(parsed.items()):
        fingerprint = hashlib.sha256(
            (f"{dsn['socket_dir']}\0{dsn['port']}\0{dsn['database']}\0{dsn['user']}").encode()
        ).hexdigest()
        endpoints_record.append(
            {
                "source": name,
                "kind": "unix_socket",
                "socket_dir": dsn["socket_dir"],
                "port": dsn["port"],
                "database": dsn["database"],
                "role": dsn["user"],
                "fingerprint": fingerprint,
            }
        )
    return {
        "engine": "postgresql",
        "major": int(POSTGRES_MAJOR),
        "server_version_num": int(version_num),
        "authority": {
            "system_identifier": controls["system_identifier"],
            "catalog_version": controls["catalog_version"],
            "database": database,
            "database_oid": database_oid,
            "socket_dir": socket_dir,
            "port": port,
        },
        "bootstrap_role": BOOTSTRAP_ROLE,
        "application_roles": list(APPLICATION_ROLES),
        "schemas": ["airflow", "platform"],
        "dsn_sources": endpoints_record,
    }


ROLE_QUERY = """
SELECT COALESCE(json_agg(row_to_json(role_row) ORDER BY role_row.name), '[]'::json)
FROM (
  SELECT
    a.rolname AS name,
    a.rolsuper AS superuser,
    a.rolinherit AS inherit,
    a.rolcreaterole AS createrole,
    a.rolcreatedb AS createdb,
    a.rolcanlogin AS canlogin,
    a.rolreplication AS replication,
    a.rolbypassrls AS bypassrls,
    a.rolconnlimit AS connection_limit,
    CASE WHEN a.rolvaliduntil IS NULL THEN NULL
         ELSE to_char(
           a.rolvaliduntil AT TIME ZONE 'UTC',
           'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'
         ) END AS valid_until,
    a.rolpassword AS password_verifier,
    COALESCE((
      SELECT s.setconfig
      FROM pg_db_role_setting s
      WHERE s.setrole = a.oid AND s.setdatabase = 0
    ), ARRAY[]::text[]) AS settings
  FROM pg_authid a
  WHERE a.rolname IN (
    'solidworks',
    'platform_migrate',
    'platform_service',
    'operator_ro',
    'airflow_migrate',
    'airflow_runtime'
  )
) role_row
"""

MEMBERSHIP_QUERY = """
SELECT COALESCE(
  json_agg(
    row_to_json(membership)
    ORDER BY membership.role, membership.member, membership.grantor
  ),
  '[]'::json
)
FROM (
  SELECT
    granted.rolname AS role,
    member.rolname AS member,
    grantor.rolname AS grantor,
    m.admin_option AS admin_option
  FROM pg_auth_members m
  JOIN pg_roles granted ON granted.oid = m.roleid
  JOIN pg_roles member ON member.oid = m.member
  JOIN pg_roles grantor ON grantor.oid = m.grantor
  WHERE granted.rolname IN (
    'solidworks',
    'platform_migrate',
    'platform_service',
    'operator_ro',
    'airflow_migrate',
    'airflow_runtime'
  )
  OR member.rolname IN (
    'solidworks',
    'platform_migrate',
    'platform_service',
    'operator_ro',
    'airflow_migrate',
    'airflow_runtime'
  )
) membership
"""

ALL_NON_SYSTEM_ROLES_QUERY = """
SELECT COALESCE(json_agg(rolname ORDER BY rolname), '[]'::json)
FROM pg_roles
WHERE left(rolname, 3) <> 'pg_'
"""


CATALOG_QUERY = """
SELECT json_build_object(
  'database', (
    SELECT json_build_object(
      'name', d.datname,
      'owner', pg_get_userbyid(d.datdba),
      'encoding', pg_encoding_to_char(d.encoding),
      'collate', d.datcollate,
      'ctype', d.datctype,
      'acl_is_null', d.datacl IS NULL,
      'acl', COALESCE((
        SELECT json_agg(json_build_object(
          'grantor', pg_get_userbyid(x.grantor),
          'grantee', CASE WHEN x.grantee = 0 THEN 'PUBLIC'
                          ELSE pg_get_userbyid(x.grantee) END,
          'privilege', x.privilege_type,
          'grantable', x.is_grantable
        ) ORDER BY
          CASE WHEN x.grantee = 0 THEN 'PUBLIC'
               ELSE pg_get_userbyid(x.grantee) END,
          x.privilege_type)
        FROM aclexplode(d.datacl) x
      ), '[]'::json)
    )
    FROM pg_database d
    WHERE d.datname = 'airflow_meta'
  ),
  'schemas', (
    SELECT json_agg(json_build_object(
      'name', n.nspname,
      'owner', pg_get_userbyid(n.nspowner),
      'acl', COALESCE((
        SELECT json_agg(json_build_object(
          'grantor', pg_get_userbyid(x.grantor),
          'grantee', CASE WHEN x.grantee = 0 THEN 'PUBLIC'
                          ELSE pg_get_userbyid(x.grantee) END,
          'privilege', x.privilege_type,
          'grantable', x.is_grantable
        ) ORDER BY
          CASE WHEN x.grantee = 0 THEN 'PUBLIC'
               ELSE pg_get_userbyid(x.grantee) END,
          x.privilege_type)
        FROM aclexplode(n.nspacl) x
      ), '[]'::json)
    ) ORDER BY n.nspname)
    FROM pg_namespace n
    WHERE n.nspname IN ('public', 'platform', 'airflow')
  ),
  'settings', (
    SELECT json_agg(json_build_object(
      'role', CASE WHEN s.setrole = 0 THEN NULL ELSE r.rolname END,
      'database', d.datname,
      'config', s.setconfig
    ) ORDER BY r.rolname NULLS FIRST, d.datname, s.setconfig::text)
    FROM pg_db_role_setting s
    LEFT JOIN pg_roles r ON r.oid = s.setrole
    JOIN pg_database d ON d.oid = s.setdatabase
    WHERE d.datname = 'airflow_meta'
  ),
  'tables', (
    SELECT json_agg(json_build_object(
      'schema', n.nspname,
      'name', c.relname,
      'kind', c.relkind,
      'owner', pg_get_userbyid(c.relowner),
      'acl', COALESCE((
        SELECT json_agg(json_build_object(
          'grantor', pg_get_userbyid(x.grantor),
          'grantee', CASE WHEN x.grantee = 0 THEN 'PUBLIC'
                          ELSE pg_get_userbyid(x.grantee) END,
          'privilege', x.privilege_type,
          'grantable', x.is_grantable
        ) ORDER BY
          CASE WHEN x.grantee = 0 THEN 'PUBLIC'
               ELSE pg_get_userbyid(x.grantee) END,
          x.privilege_type)
        FROM aclexplode(c.relacl) x
      ), '[]'::json)
    ) ORDER BY n.nspname, c.relname)
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE (n.nspname = 'platform' AND c.relname IN (
      'admission', 'request', 'run', 'stage', 'operation', 'run_event'
    ))
    OR (n.nspname = 'airflow' AND c.relname IN (
      'alembic_version', 'connection', 'slot_pool', 'feishu_auth_state'
    ))
  ),
  'public_create', has_database_privilege('public', 'airflow_meta', 'CREATE'),
  'platform_service_airflow_usage',
    has_schema_privilege('platform_service', 'airflow', 'USAGE'),
  'platform_service_airflow_create',
    has_schema_privilege('platform_service', 'airflow', 'CREATE')
)
"""

FULL_CATALOG_QUERY = """
SELECT json_build_object(
  'schemas', (
    SELECT json_agg(json_build_object(
      'name', n.nspname,
      'owner', pg_get_userbyid(n.nspowner),
      'acl', n.nspacl::text
    ) ORDER BY n.nspname)
    FROM pg_namespace n
    WHERE n.nspname IN ('platform', 'airflow', 'public')
  ),
  'relations', (
    SELECT json_agg(json_build_object(
      'schema', n.nspname,
      'name', c.relname,
      'kind', c.relkind,
      'owner', pg_get_userbyid(c.relowner),
      'persistence', c.relpersistence,
      'is_partition', c.relispartition,
      'acl', c.relacl::text,
      'reloptions', c.reloptions,
      'definition', CASE
        WHEN c.relkind = 'v' THEN pg_get_viewdef(c.oid, true)
        WHEN c.relkind = 'm' THEN pg_get_viewdef(c.oid, true)
        WHEN c.relkind IN ('i', 'I') THEN pg_get_indexdef(c.oid)
        ELSE NULL
      END
    ) ORDER BY n.nspname, c.relname, c.relkind)
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname IN ('platform', 'airflow')
      AND c.relkind IN ('r', 'p', 'v', 'm', 'S', 'f', 'i', 'I')
  ),
  'columns', (
    SELECT json_agg(json_build_object(
      'schema', n.nspname,
      'relation', c.relname,
      'position', a.attnum,
      'name', a.attname,
      'type', format_type(a.atttypid, a.atttypmod),
      'not_null', a.attnotnull,
      'identity', a.attidentity,
      'generated', a.attgenerated,
      'default', pg_get_expr(d.adbin, d.adrelid),
      'collation', CASE WHEN a.attcollation = 0 THEN NULL
                        ELSE a.attcollation::regcollation::text END
    ) ORDER BY n.nspname, c.relname, a.attnum)
    FROM pg_attribute a
    JOIN pg_class c ON c.oid = a.attrelid
    JOIN pg_namespace n ON n.oid = c.relnamespace
    LEFT JOIN pg_attrdef d
      ON d.adrelid = a.attrelid AND d.adnum = a.attnum
    WHERE n.nspname IN ('platform', 'airflow')
      AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
      AND a.attnum > 0
      AND NOT a.attisdropped
  ),
  'constraints', (
    SELECT json_agg(json_build_object(
      'schema', n.nspname,
      'relation', c.relname,
      'name', con.conname,
      'type', con.contype,
      'definition', pg_get_constraintdef(con.oid, true),
      'validated', con.convalidated,
      'deferrable', con.condeferrable,
      'deferred', con.condeferred,
      'no_inherit', con.connoinherit
    ) ORDER BY n.nspname, c.relname, con.conname)
    FROM pg_constraint con
    JOIN pg_class c ON c.oid = con.conrelid
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname IN ('platform', 'airflow')
  ),
  'triggers', (
    SELECT json_agg(json_build_object(
      'schema', n.nspname,
      'relation', c.relname,
      'name', t.tgname,
      'enabled', t.tgenabled,
      'definition', pg_get_triggerdef(t.oid, true)
    ) ORDER BY n.nspname, c.relname, t.tgname)
    FROM pg_trigger t
    JOIN pg_class c ON c.oid = t.tgrelid
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname IN ('platform', 'airflow')
      AND NOT t.tgisinternal
  ),
  'functions', (
    SELECT json_agg(json_build_object(
      'schema', n.nspname,
      'name', p.proname,
      'identity_arguments', pg_get_function_identity_arguments(p.oid),
      'kind', p.prokind,
      'owner', pg_get_userbyid(p.proowner),
      'language', l.lanname,
      'volatility', p.provolatile,
      'security_definer', p.prosecdef,
      'strict', p.proisstrict,
      'parallel', p.proparallel,
      'leakproof', p.proleakproof,
      'acl', p.proacl::text,
      'definition', pg_get_functiondef(p.oid)
    ) ORDER BY n.nspname, p.proname, pg_get_function_identity_arguments(p.oid))
    FROM pg_proc p
    JOIN pg_namespace n ON n.oid = p.pronamespace
    JOIN pg_language l ON l.oid = p.prolang
    WHERE n.nspname IN ('platform', 'airflow')
  ),
  'types', (
    SELECT json_agg(json_build_object(
      'schema', n.nspname,
      'name', t.typname,
      'type', t.typtype,
      'category', t.typcategory,
      'owner', pg_get_userbyid(t.typowner),
      'acl', t.typacl::text,
      'enum_labels', CASE WHEN t.typtype = 'e' THEN (
        SELECT json_agg(e.enumlabel ORDER BY e.enumsortorder)
        FROM pg_enum e
        WHERE e.enumtypid = t.oid
      ) ELSE NULL END,
      'domain', CASE WHEN t.typtype = 'd'
        THEN format_type(t.typbasetype, t.typtypmod) ELSE NULL END,
      'domain_not_null', CASE WHEN t.typtype = 'd' THEN t.typnotnull ELSE NULL END,
      'domain_default', CASE WHEN t.typtype = 'd' THEN t.typdefault ELSE NULL END
    ) ORDER BY n.nspname, t.typname)
    FROM pg_type t
    JOIN pg_namespace n ON n.oid = t.typnamespace
    WHERE n.nspname IN ('platform', 'airflow')
      AND t.typtype IN ('e', 'd', 'c')
      AND (t.typrelid = 0 OR EXISTS (
        SELECT 1 FROM pg_class c
        WHERE c.oid = t.typrelid
          AND c.relkind NOT IN ('r', 'p', 'v', 'm', 'f')
      ))
  ),
  'extensions', (
    SELECT json_agg(json_build_object(
      'name', e.extname,
      'version', e.extversion,
      'schema', n.nspname,
      'relocatable', e.extrelocatable
    ) ORDER BY e.extname)
    FROM pg_extension e
    JOIN pg_namespace n ON n.oid = e.extnamespace
  ),
  'default_acls', (
    SELECT COALESCE(json_agg(json_build_object(
      'role', pg_get_userbyid(d.defaclrole),
      'schema', CASE WHEN d.defaclnamespace = 0 THEN NULL
                     ELSE (
                       SELECT n.nspname
                       FROM pg_namespace n
                       WHERE n.oid = d.defaclnamespace
                     ) END,
      'object_type', d.defaclobjtype,
      'acl', d.defaclacl::text
    ) ORDER BY pg_get_userbyid(d.defaclrole), d.defaclobjtype), '[]'::json)
    FROM pg_default_acl d
    WHERE d.defaclnamespace = 0
      OR d.defaclnamespace IN (
        SELECT oid FROM pg_namespace
        WHERE nspname IN ('platform', 'airflow')
      )
  )
)
"""

FULL_TABLE_LIST_QUERY = """
SELECT COALESCE(json_agg(json_build_object(
  'schema', n.nspname,
  'table', c.relname
) ORDER BY n.nspname, c.relname), '[]'::json)
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname IN ('platform', 'airflow')
  AND c.relkind IN ('r', 'p')
"""

DATA_TABLES = (
    ("platform", "admission"),
    ("platform", "request"),
    ("platform", "run"),
    ("platform", "stage"),
    ("platform", "operation"),
    ("platform", "run_event"),
    ("airflow", "alembic_version"),
    ("airflow", "connection"),
)


def _table_fingerprint_query(
    schema: str,
    table: str,
    *,
    allow_discovered: bool = False,
) -> str:
    require(
        schema in {"platform", "airflow"}
        and (allow_discovered or table in {row[1] for row in DATA_TABLES})
        and bool(ROLE_NAME.fullmatch(table)),
        "recovery_database_readback_failed",
        "unexpected recovery baseline table",
    )
    return (
        "SELECT json_build_object("
        "'rows', count(*),"
        "'fingerprint', COALESCE(md5(string_agg(row_hash, '' ORDER BY row_hash)), md5(''))) "
        f"FROM (SELECT md5(to_jsonb(t)::text) AS row_hash FROM {schema}.{table} t) rows"
    )


def capture_sequence_baseline(
    *,
    context: CommandContext,
    socket_dir: Path,
    port: int,
    database: str,
) -> list[dict]:
    rows = psql_json(
        context,
        socket_dir=socket_dir,
        port=port,
        user=BOOTSTRAP_ROLE,
        database=database,
        query=(
            "SELECT COALESCE(json_agg(json_build_object("
            "'schema', schemaname, 'name', sequencename, "
            "'increment_by', increment_by::text, 'cycle', cycle"
            ") ORDER BY schemaname, sequencename), '[]'::json) "
            "FROM pg_sequences WHERE schemaname IN ('platform', 'airflow')"
        ),
    )
    require(
        isinstance(rows, list),
        "recovery_database_readback_failed",
        "sequence catalog readback is malformed",
    )
    result = []
    for row in rows:
        require(
            isinstance(row, dict)
            and set(row) == {"schema", "name", "increment_by", "cycle"}
            and row["schema"] in {"platform", "airflow"}
            and isinstance(row["name"], str)
            and bool(ROLE_NAME.fullmatch(row["name"])),
            "recovery_database_readback_failed",
            "sequence catalog entry is malformed",
        )
        state = psql_json(
            context,
            socket_dir=socket_dir,
            port=port,
            user=BOOTSTRAP_ROLE,
            database=database,
            query=(
                "SELECT json_build_object("
                "'last_value', last_value::text, 'is_called', is_called) "
                f"FROM {quote_ident(row['schema'])}.{quote_ident(row['name'])}"
            ),
        )
        require(
            isinstance(state, dict)
            and set(state) == {"last_value", "is_called"}
            and isinstance(state["is_called"], bool),
            "recovery_database_readback_failed",
            "sequence state readback is malformed",
        )
        result.append({**row, **state})
    return result


def inventory_tree(root: Path) -> list[dict]:
    root = Path(root)
    require_real_dir(root, "checkpoint tree", "recovery_member_hash_mismatch")
    records = []
    for path in sorted(root.rglob("*")):
        require(
            not is_link(path),
            "recovery_member_hash_mismatch",
            f"checkpoint tree carries a link: {path}",
        )
        relative = path.relative_to(root).as_posix()
        if path.is_dir():
            records.append(
                {
                    "path": relative,
                    "type": "directory",
                    "bytes": 0,
                    "sha256": None,
                    "mode": stat.S_IMODE(path.stat().st_mode),
                }
            )
        else:
            require(
                path.is_file(),
                "recovery_member_hash_mismatch",
                f"checkpoint tree carries a special file: {path}",
            )
            records.append(
                {
                    "path": relative,
                    "type": "file",
                    "bytes": path.stat().st_size,
                    "sha256": sha256_file(path),
                    "mode": stat.S_IMODE(path.stat().st_mode),
                }
            )
    return records


def verify_checkpoint_tree(
    tree: Path,
    files: list[dict],
    *,
    receipt: object,
) -> None:
    current = inventory_tree(tree)
    require(
        current == files,
        "recovery_member_hash_mismatch",
        f"checkpoint tree differs from its reference index: {tree}",
    )
    if isinstance(receipt, dict):
        artifacts = receipt.get("artifacts") or []
        require(
            isinstance(artifacts, list),
            "recovery_member_hash_mismatch",
            f"checkpoint receipt artifacts are malformed: {tree}",
        )
        for artifact in artifacts:
            require(
                isinstance(artifact, dict) and set(artifact) >= {"path", "bytes", "sha256"},
                "recovery_member_hash_mismatch",
                f"checkpoint receipt artifact is malformed: {tree}",
            )
            relative = confined_member(artifact["path"], "checkpoint artifact")
            target = tree / relative
            require(
                target.is_file()
                and not is_link(target)
                and target.stat().st_size == artifact["bytes"]
                and sha256_file(target) == artifact["sha256"],
                "recovery_member_hash_mismatch",
                f"checkpoint artifact differs from its receipt: {relative}",
            )


def checkpoint_index(references: list[dict], store_root: Path) -> list[dict]:
    indexed = []
    for reference in references:
        uri = reference.get("checkpoint_uri")
        run_id = reference.get("run_id")
        stage = reference.get("stage")
        require(
            isinstance(uri, str) and uri == f"store://sealed/{run_id}/{stage}",
            "recovery_member_hash_mismatch",
            "checkpoint URI does not bind the registered run/stage",
        )
        tree = store_root / "sealed" / str(run_id) / str(stage)
        files = inventory_tree(tree)
        verify_checkpoint_tree(tree, files, receipt=reference.get("receipt"))
        indexed.append(
            {
                "run_id": run_id,
                "stage": stage,
                "checkpoint_uri": uri,
                "tree_relative": Path("sealed", str(run_id), str(stage)).as_posix(),
                "files": files,
            }
        )
    return indexed


def verify_checkpoint_index(index: list[dict], store_root: Path) -> None:
    for row in index:
        tree = store_root / row["tree_relative"]
        verify_checkpoint_tree(tree, row["files"], receipt={})


def copy_tree_strict(source: Path, destination: Path) -> None:
    source = Path(source)
    destination = Path(destination)
    require_real_dir(source, "source tree", "recovery_member_hash_mismatch")
    destination.mkdir(parents=True, exist_ok=False, mode=0o700)
    os.chmod(destination, 0o700)
    directory_modes = []
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        target = destination / relative
        if is_link(path):
            raise RecoveryError(
                "recovery_member_hash_mismatch",
                f"source tree carries a link: {path}",
            )
        if path.is_dir():
            target.mkdir(mode=0o700)
            directory_modes.append((target, stat.S_IMODE(path.stat().st_mode)))
        elif path.is_file():
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.copy2(path, target)
            os.chmod(target, stat.S_IMODE(path.stat().st_mode) & 0o777)
        else:
            raise RecoveryError(
                "recovery_member_hash_mismatch",
                f"source tree carries a special file: {path}",
            )
    for directory, mode in reversed(directory_modes):
        os.chmod(directory, mode)
    os.chmod(destination, stat.S_IMODE(source.stat().st_mode))


def copy_required_tree(source: Path, destination: Path) -> None:
    require_real_dir(source, f"required root {source}", "recovery_release_missing")
    copy_tree_strict(source, destination)


def copy_required_file(
    source: Path,
    destination: Path,
    mode: int | None = None,
) -> None:
    require_real_file(source, f"required file {source}", "recovery_release_missing")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    shutil.copy2(source, destination)
    os.chmod(
        destination,
        stat.S_IMODE(source.stat().st_mode) if mode is None else mode,
    )


def source_inventory(roots: list[dict]) -> list[dict]:
    records = []
    for root in roots:
        path = Path(root["path"])
        if path.is_dir():
            inventory = inventory_tree(path)
            kind = "directory"
        else:
            require_real_file(
                path,
                f"required source file {path}",
                "recovery_release_missing",
            )
            inventory = [
                {
                    "path": path.name,
                    "type": "file",
                    "bytes": path.stat().st_size,
                    "sha256": sha256_file(path),
                    "mode": stat.S_IMODE(path.stat().st_mode),
                }
            ]
            kind = "file"
        records.append(
            {
                "name": root["name"],
                "path": str(path),
                "type": kind,
                "mode": stat.S_IMODE(path.stat().st_mode),
                "inventory": inventory,
            }
        )
    return records


def verify_staged_source_members(
    context: BackupContext,
    inventory_before: list[dict],
    staging: Path,
) -> tuple[str, dict]:
    fixed_prefix = {
        "install_records": "config/state/install-records",
        "snapshots": "config/state/snapshots",
        "derived": "config/state/derived",
        "promotions": "config/state/promotions",
        "install_record": "config/state/install-record.json",
        "resolved_env": "config/state/resolved.env",
        "portal": "config/state/portal.json",
        "feishu_env": "config/state/feishu.env",
        "nginx": "config/state/nginx/nginx.conf",
        "airflow_cfg": "config/state/airflow-home/airflow.cfg",
        "maintenance_lock": f"config/state/{MAINTENANCE_LOCK_DIRNAME}/{MAINTENANCE_LOCK_FILENAME}",
        "store_root": "state/runs",
        "handoff_root": "handoff",
        "artifact_root": "artifacts",
        "release_json": "release/release.json",
        "platform_config": "config/platform.json",
    }
    secret_members = (
        "secrets/tls.crt",
        "secrets/tls.key",
        "secrets/postgres.dsn",
        "secrets/feishu.json",
        "secrets/endpoint.token",
    )

    def prefix_for(name: str) -> str:
        if name in fixed_prefix:
            return fixed_prefix[name]
        if name.startswith("secret_"):
            index = int(name.removeprefix("secret_"))
            return secret_members[index]
        if name.startswith("release_artifact_"):
            index = int(name.removeprefix("release_artifact_"))
            artifact = context.release_document["artifacts"][index]["name"]
            return f"release/artifacts/{artifact}"
        if name.startswith("specification_root_"):
            index = int(name.removeprefix("specification_root_"))
            return f"specifications/root-{index}"
        raise RecoveryError(
            "recovery_staged_source_mismatch",
            f"source inventory root has no staging mapping: {name}",
        )

    expected = {}
    directory_prefixes = []
    for root in inventory_before:
        prefix = prefix_for(root["name"])
        if root["type"] == "file":
            entry = root["inventory"][0]
            expected_mode = 0o600 if root["name"].startswith("secret_") else entry["mode"]
            expected[prefix] = {
                "member": prefix,
                "type": "file",
                "bytes": entry["bytes"],
                "sha256": entry["sha256"],
                "mode": expected_mode,
            }
        else:
            directory_prefixes.append(prefix)
            expected[prefix] = {
                "member": prefix,
                "type": "directory",
                "bytes": 0,
                "sha256": None,
                "mode": root["mode"],
            }
            for entry in root["inventory"]:
                member = f"{prefix}/{entry['path']}"
                expected[member] = {
                    "member": member,
                    "type": entry["type"],
                    "bytes": entry["bytes"],
                    "sha256": entry["sha256"],
                    "mode": entry["mode"],
                }
    canonical_parents = set()
    for member in list(expected):
        parent = Path(member).parent
        while parent.as_posix() not in {"", "."}:
            parent_name = parent.as_posix()
            if parent_name not in expected:
                canonical_parents.add(parent_name)
                expected[parent_name] = {
                    "member": parent_name,
                    "type": "directory",
                    "bytes": 0,
                    "sha256": None,
                    "mode": 0o700,
                }
            parent = parent.parent
    release_copy = expected.get("release/release.json")
    require(
        release_copy is not None,
        "recovery_staged_source_mismatch",
        "release.json source member is missing from inventory",
    )
    expected["config/release.json"] = {
        **release_copy,
        "member": "config/release.json",
    }
    for member in canonical_parents:
        path = Path(staging) / member
        if path.is_dir() and not path.is_symlink():
            os.chmod(path, 0o700)

    actual_rows = {
        row["member"]: {
            "member": row["member"],
            "type": row["type"],
            "bytes": row["bytes"],
            "sha256": row["sha256"],
            "mode": row["mode"],
        }
        for row in manifest_members(Path(staging))
    }
    differences = [
        {
            "member": member,
            "expected": row,
            "actual": actual_rows.get(member),
        }
        for member, row in sorted(expected.items())
        if actual_rows.get(member) != row
    ]
    directory_prefixes = sorted(
        {
            *directory_prefixes,
            *(member for member, row in expected.items() if row["type"] == "directory"),
        },
        key=lambda value: (len(Path(value).parts), value),
    )
    for prefix in directory_prefixes:
        expected_subtree = {
            member: row
            for member, row in expected.items()
            if member == prefix or member.startswith(prefix.rstrip("/") + "/")
        }
        actual_subtree = {
            member: row
            for member, row in actual_rows.items()
            if member == prefix or member.startswith(prefix.rstrip("/") + "/")
        }
        if actual_subtree != expected_subtree:
            differences.append(
                {
                    "member": prefix,
                    "expected_extra": sorted(set(expected_subtree) - set(actual_subtree)),
                    "actual_extra": sorted(set(actual_subtree) - set(expected_subtree)),
                    "content_digest_differs": True,
                }
            )
    require(
        not differences,
        "recovery_staged_source_mismatch",
        "staged source-derived members differ from the pre-snapshot inventory",
        {"differences": differences[:10]},
    )
    return hashlib.sha256(canonical_json(expected)).hexdigest(), expected


def verify_manifest_source_binding(
    staging: Path,
    manifest_members_rows: list[dict],
    expected: dict,
    *,
    normalize_directory_modes: bool = False,
) -> None:
    if normalize_directory_modes:
        for member, row in expected.items():
            if row["type"] == "directory":
                path = Path(staging) / member
                if path.is_dir() and not path.is_symlink():
                    os.chmod(path, row["mode"])

    def comparable(row: dict) -> dict:
        return {
            "member": row["member"],
            "type": row["type"],
            "bytes": row["bytes"],
            "sha256": row["sha256"],
            "mode": row["mode"],
        }

    manifest_map = {row["member"]: comparable(row) for row in manifest_members_rows}
    actual_map = {row["member"]: comparable(row) for row in manifest_members(Path(staging))}
    differences = [
        {
            "member": member,
            "expected": row,
            "manifest": manifest_map.get(member),
            "actual": actual_map.get(member),
        }
        for member, row in sorted(expected.items())
        if manifest_map.get(member) != row or actual_map.get(member) != row
    ]
    expected_directories = {member for member, row in expected.items() if row["type"] == "directory"}
    top_prefixes = sorted(
        member for member in expected_directories if Path(member).parent.as_posix() not in expected_directories
    )
    for prefix in top_prefixes:
        expected_subtree = {
            member: row
            for member, row in expected.items()
            if member == prefix or member.startswith(prefix.rstrip("/") + "/")
        }
        for source_name, source_map in (
            ("manifest", manifest_map),
            ("actual", actual_map),
        ):
            actual_subtree = {
                member: row
                for member, row in source_map.items()
                if member == prefix or member.startswith(prefix.rstrip("/") + "/")
            }
            if actual_subtree != expected_subtree:
                differences.append(
                    {
                        "member": prefix,
                        "source": source_name,
                        "expected_extra": sorted(set(expected_subtree) - set(actual_subtree)),
                        "actual_extra": sorted(set(actual_subtree) - set(expected_subtree)),
                        "content_digest_differs": True,
                    }
                )
    require(
        not differences,
        "recovery_staged_source_mismatch",
        "manifest or final staging differs from the verified source member map",
        {"differences": differences[:10]},
    )


def read_sealed_tar_member_rows(archive: Path, key: Path) -> list[dict]:
    process = subprocess.Popen(
        [
            "openssl",
            "enc",
            "-d",
            "-aes-256-cbc",
            "-pbkdf2",
            "-pass",
            f"file:{key}",
            "-in",
            str(archive),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdout is not None and process.stderr is not None
    rows = []
    stream_error = None
    try:
        try:
            with tarfile.open(fileobj=process.stdout, mode="r|") as archive_stream:
                for member in archive_stream:
                    name = confined_member(member.name, "archive member")
                    mode = member.mode & 0o777
                    if member.isdir():
                        rows.append(
                            {
                                "member": name,
                                "type": "directory",
                                "bytes": 0,
                                "sha256": None,
                                "mode": mode,
                            }
                        )
                    elif member.isfile():
                        source = archive_stream.extractfile(member)
                        require(
                            source is not None,
                            "recovery_archive_member_mismatch",
                            f"archive file has no readable payload: {name}",
                        )
                        digest = hashlib.sha256()
                        size = 0
                        while chunk := source.read(1024 * 1024):
                            size += len(chunk)
                            digest.update(chunk)
                        require(
                            size == member.size,
                            "recovery_archive_member_mismatch",
                            f"archive member size changed while reading: {name}",
                        )
                        rows.append(
                            {
                                "member": name,
                                "type": "file",
                                "bytes": size,
                                "sha256": digest.hexdigest(),
                                "mode": mode,
                            }
                        )
                    else:
                        raise RecoveryError(
                            "recovery_archive_member_mismatch",
                            f"archive contains an unsupported member type: {name}",
                        )
        except (OSError, tarfile.TarError) as error:
            stream_error = error
        process.stdout.close()
        stderr = process.stderr.read() if process.stderr is not None else b""
        process.stderr.close()
        returncode = process.wait()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
    require(
        returncode == 0 and stream_error is None,
        "recovery_archive_member_mismatch",
        "cannot read sealed archive tar stream",
        {
            "openssl": stderr.decode("utf-8", errors="replace").strip(),
            "tar_error": None if stream_error is None else str(stream_error),
        },
    )
    return sorted(rows, key=lambda row: row["member"])


def verify_tar_rows_against_manifest(
    tar_rows: list[dict],
    manifest_members: list[dict],
) -> None:
    expected_rows = [
        {
            "member": row["member"],
            "type": row["type"],
            "bytes": row["bytes"],
            "sha256": row["sha256"],
            "mode": row["mode"],
        }
        for row in manifest_members
    ]
    expected_by_name = {row["member"]: row for row in expected_rows}
    actual_by_name = {row["member"]: row for row in tar_rows}
    differences = [
        {
            "member": member,
            "expected": expected_by_name.get(member),
            "actual": actual_by_name.get(member),
        }
        for member in sorted(set(expected_by_name) | set(actual_by_name))
        if expected_by_name.get(member) != actual_by_name.get(member)
    ]
    require(
        not differences,
        "recovery_archive_member_mismatch",
        "sealed archive tar headers differ from manifest members",
        {"differences": differences[:10]},
    )


def verify_archive_member_binding(
    *,
    archive: Path,
    key: Path,
    manifest_members: list[dict],
    staged_source_members: dict,
    work_dir: Path,
) -> dict:
    temporary = tempfile.TemporaryDirectory(
        prefix="archive-members-",
        dir=work_dir,
    )
    extracted = Path(temporary.name) / "archive"
    legacy_path = Path(temporary.name) / "legacy-manifest.json"
    try:
        tar_rows = read_sealed_tar_member_rows(archive, key)
        verify_tar_rows_against_manifest(tar_rows, manifest_members)
        legacy = {
            "schema": backup.MANIFEST_SCHEMA,
            "files": {row["member"]: row["sha256"] for row in manifest_members if row["type"] == "file"},
            "encrypted": {
                "path": str(archive),
                "sha256": sha256_file(archive),
                "cipher": "aes-256-cbc",
                "key": str(key),
            },
        }
        atomic_write(legacy_path, canonical_json(legacy))
        try:
            backup.open_archive(archive, key, legacy_path, extracted)
        except SystemExit as error:
            raise RecoveryError(
                "recovery_archive_member_mismatch",
                f"cannot reopen sealed archive members: {error}",
            ) from error
        try:
            verify_member_set(extracted, manifest_members)
            verify_manifest_source_binding(
                extracted,
                manifest_members,
                staged_source_members,
                normalize_directory_modes=True,
            )
        except RecoveryError as error:
            raise RecoveryError(
                "recovery_archive_member_mismatch",
                "sealed archive members differ from the expected member map",
                {
                    "cause_code": error.code,
                    "cause_message": error.message,
                    "cause_detail": error.detail,
                },
            ) from error
        rows = [
            {
                "member": row["member"],
                "type": row["type"],
                "bytes": row["bytes"],
                "sha256": row["sha256"],
                "mode": row["mode"],
            }
            for row in manifest_members
        ]
        return {
            "members": len(rows),
            "sha256": _digest_document(rows),
        }
    finally:
        temporary.cleanup()


def manifest_members(staging: Path) -> list[dict]:
    members = []
    for path in sorted(staging.rglob("*")):
        require(
            not is_link(path),
            "recovery_member_hash_mismatch",
            f"backup staging carries a link: {path}",
        )
        relative = path.relative_to(staging).as_posix()
        confined_member(relative)
        mode = stat.S_IMODE(path.stat().st_mode)
        if path.is_dir():
            members.append(
                {
                    "member": relative,
                    "type": "directory",
                    "bytes": 0,
                    "sha256": None,
                    "mode": mode,
                    "required": True,
                    "class": "directory",
                }
            )
        elif path.is_file():
            members.append(
                {
                    "member": relative,
                    "type": "file",
                    "bytes": path.stat().st_size,
                    "sha256": sha256_file(path),
                    "mode": mode,
                    "required": True,
                    "class": "member",
                }
            )
        else:
            raise RecoveryError(
                "recovery_member_hash_mismatch",
                f"backup staging carries a special file: {path}",
            )
    return members


def normalize_generated_modes(staging: Path) -> None:
    """Keep source-derived modes; normalize only generated recovery evidence."""

    os.chmod(staging, 0o700)
    for prefix in ("database", "inventory", "reconstruct"):
        root = staging / prefix
        if not root.exists():
            continue
        for path in sorted(root.rglob("*")):
            require(
                not is_link(path),
                "recovery_member_hash_mismatch",
                f"backup staging carries a link: {path}",
            )
            os.chmod(path, 0o700 if path.is_dir() else 0o600)
        os.chmod(root, 0o700)


def verify_member_set(staging: Path, members: list[dict]) -> None:
    def comparable(row: dict) -> dict:
        keys = ("member", "type", "bytes", "sha256", "required")
        if row["type"] == "file":
            keys += ("mode",)
        return {key: row[key] for key in keys}

    actual = [comparable(row) for row in manifest_members(staging)]
    expected = []
    for row in members:
        expected.append(comparable(row))
    expected_by_name = {row["member"]: row for row in expected}
    actual_by_name = {row["member"]: row for row in actual}
    differences = [
        {
            "member": name,
            "expected": expected_by_name.get(name),
            "actual": actual_by_name.get(name),
        }
        for name in sorted(set(expected_by_name) | set(actual_by_name))
        if expected_by_name.get(name) != actual_by_name.get(name)
    ]
    require(
        actual == expected,
        "recovery_member_hash_mismatch",
        "archive members differ from the recovery manifest",
        {"differences": differences[:5]},
    )


def copy_release_tree(context: BackupContext, staging: Path) -> dict:
    release_root = Path(context.resolved["storage"]["release_root"])
    destination = staging / "release"
    destination.mkdir(parents=True, mode=0o700)
    release_json = release_root / "release.json"
    copy_required_file(release_json, destination / "release.json")
    artifacts = []
    for row in context.release_document["artifacts"]:
        name = confined_member(row["name"], "release artifact")
        source = release_root / name
        require_real_file(source, f"release artifact {name}", "recovery_release_missing")
        actual = sha256_file(source)
        require(
            actual == row["sha256"],
            "recovery_release_missing",
            f"release artifact differs from release.json: {name}",
        )
        target = destination / "artifacts" / name
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        shutil.copy2(source, target)
        artifacts.append(
            {
                "name": name,
                "member": f"release/artifacts/{name}",
                "bytes": target.stat().st_size,
                "sha256": actual,
            }
        )
    return {
        "name": "release_root",
        "path": str(release_root),
        "archive": "release",
        "class": "release",
        "required": True,
        "artifacts": artifacts,
    }


def copy_state_configuration(state_root: Path, install_id: str, staging: Path) -> None:
    destination = staging / "config" / "state"
    destination.mkdir(parents=True, mode=0o700)
    file_map = (
        ("install-record.json", Path("install-record.json")),
        ("resolved.env", Path("resolved.env")),
        ("portal.json", Path("portal.json")),
        ("feishu.env", Path("feishu.env")),
        ("nginx/nginx.conf", Path("nginx/nginx.conf")),
        ("airflow-home/airflow.cfg", Path("airflow-home/airflow.cfg")),
        (
            Path(MAINTENANCE_LOCK_DIRNAME) / MAINTENANCE_LOCK_FILENAME,
            Path(MAINTENANCE_LOCK_DIRNAME) / MAINTENANCE_LOCK_FILENAME,
        ),
    )
    for source_relative, target_relative in file_map:
        source = state_root / source_relative
        require_real_file(
            source,
            f"state configuration {source_relative}",
            "recovery_release_missing",
        )
        require(
            stat.S_IMODE(source.stat().st_mode) & 0o077 == 0,
            "recovery_release_missing",
            f"state configuration is group/world accessible: {source_relative}",
        )
        copy_required_file(
            source,
            destination / target_relative,
        )
    for source_relative, target_relative in (
        ("install-records", Path("install-records")),
        ("snapshots", Path("snapshots")),
        ("derived", Path("derived")),
        ("promotions", Path("promotions")),
    ):
        copy_required_tree(
            state_root / source_relative,
            destination / target_relative,
        )
    derived = destination / "derived" / install_id / "operator.env"
    require_real_file(
        derived,
        "captured active derived environment",
        "recovery_release_missing",
    )


def parse_airflow_secrets(airflow_cfg: Path) -> dict:
    require_real_file(
        airflow_cfg,
        "AIRFLOW_HOME/airflow.cfg",
        "recovery_release_missing",
    )
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read(airflow_cfg, encoding="utf-8")
    except (OSError, configparser.Error) as error:
        raise RecoveryError(
            "recovery_release_missing",
            f"cannot parse airflow.cfg: {error}",
        ) from error
    fernet = parser.get("core", "fernet_key", fallback="")
    jwt = parser.get("webserver", "jwt_secret", fallback="") or parser.get("api_auth", "jwt_secret", fallback="")
    text = airflow_cfg.read_text(encoding="utf-8", errors="strict")
    require(
        bool(fernet),
        "recovery_release_missing",
        "airflow.cfg has no Fernet key",
    )
    require(
        bool(jwt) or "jwt_secret" in text,
        "recovery_release_missing",
        "airflow.cfg has no JWT secret",
    )
    return {"fernet_key_present": True, "jwt_secret_present": True}


def capture_secret_files(context: BackupContext, staging: Path) -> tuple[list[dict], dict]:
    resolved = context.snapshot["resolved"]
    mapping = (
        (
            "tls_certificate",
            resolved["operator"]["certificate_file"],
            "secrets/tls.crt",
        ),
        (
            "tls_private_key",
            resolved["operator"]["private_key_file"],
            "secrets/tls.key",
        ),
        ("database_dsn", resolved["database"]["dsn_file"], "secrets/postgres.dsn"),
        (
            "feishu_secret",
            resolved["auth"]["feishu_secret_file"],
            "secrets/feishu.json",
        ),
        ("endpoint_token", resolved["windows"]["token_file"], "secrets/endpoint.token"),
    )
    records = []
    for name, raw_source, member in mapping:
        source = Path(raw_source)
        require_real_file(source, f"required secret {name}", "recovery_release_missing")
        require(
            stat.S_IMODE(source.stat().st_mode) & 0o077 == 0,
            "recovery_release_missing",
            f"required secret is group/world accessible: {name}",
        )
        target = staging / member
        copy_required_file(source, target, mode=0o600)
        records.append(
            {
                "name": name,
                "path": str(source),
                "member": member,
                "mode": 0o600,
                "required": True,
            }
        )
    return records, {
        "airflow_config_member": "config/state/airflow-home/airflow.cfg",
        "connection_id": "solidworks_windows",
    }


def root_contract(context: BackupContext, release_root_record: dict) -> list[dict]:
    resolved = context.snapshot["resolved"]
    storage = resolved["storage"]
    roots = [
        {
            "name": "state_root",
            "path": storage["state_root"],
            "archive": "config/state",
            "class": "config-state",
            "required": True,
        },
        {
            "name": "store_root",
            "path": resolved["derived"]["store_root"],
            "archive": "state/runs",
            "class": "checkpoint",
            "required": True,
        },
        {
            "name": "handoff_root",
            "path": storage["handoff_root"],
            "archive": "handoff",
            "class": "handoff",
            "required": True,
        },
        {
            "name": "artifact_root",
            "path": storage["artifact_root"],
            "archive": "artifacts",
            "class": "artifact",
            "required": True,
        },
        release_root_record,
    ]
    for index, root in enumerate(resolved["specifications"]["record_roots"]):
        roots.append(
            {
                "name": f"specification_root_{index}",
                "path": root,
                "archive": f"specifications/root-{index}",
                "class": "original",
                "required": True,
            }
        )
    return roots


def capture_checkpoint_store(source: Path, staging: Path) -> None:
    copy_required_tree(source, staging / "state" / "runs")


def derived_dump_member(staging: Path, name: str) -> dict:
    path = staging / "database" / name
    require_real_file(path, name, "recovery_member_hash_mismatch")
    return {
        "member": f"database/{name}",
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def backup_source_checks(context: BackupContext) -> list[dict]:
    state_root = Path(context.resolved["storage"]["state_root"])
    release_root = Path(context.resolved["storage"]["release_root"])
    checks = [
        {"name": "install_records", "path": str(state_root / "install-records")},
        {"name": "snapshots", "path": str(state_root / "snapshots")},
        {"name": "derived", "path": str(state_root / "derived")},
        {"name": "promotions", "path": str(state_root / "promotions")},
        {"name": "install_record", "path": str(state_root / "install-record.json")},
        {"name": "resolved_env", "path": str(state_root / "resolved.env")},
        {"name": "portal", "path": str(state_root / "portal.json")},
        {"name": "feishu_env", "path": str(state_root / "feishu.env")},
        {"name": "nginx", "path": str(state_root / "nginx" / "nginx.conf")},
        {
            "name": "airflow_cfg",
            "path": str(state_root / "airflow-home" / "airflow.cfg"),
        },
        {
            "name": "maintenance_lock",
            "path": str(
                state_root
                / MAINTENANCE_LOCK_DIRNAME
                / MAINTENANCE_LOCK_FILENAME
            ),
        },
        {
            "name": "store_root",
            "path": context.resolved["derived"]["store_root"],
        },
        {
            "name": "handoff_root",
            "path": context.resolved["storage"]["handoff_root"],
        },
        {
            "name": "artifact_root",
            "path": context.resolved["storage"]["artifact_root"],
        },
        {"name": "release_json", "path": str(release_root / "release.json")},
        {"name": "platform_config", "path": str(context.config_path)},
    ]
    checks.extend(
        {
            "name": f"release_artifact_{index}",
            "path": str(release_root / row["name"]),
        }
        for index, row in enumerate(context.release_document["artifacts"])
    )
    checks.extend(
        {
            "name": f"specification_root_{index}",
            "path": root,
        }
        for index, root in enumerate(context.resolved["specifications"]["record_roots"])
    )
    secret_paths = (
        context.snapshot["resolved"]["operator"]["certificate_file"],
        context.snapshot["resolved"]["operator"]["private_key_file"],
        context.snapshot["resolved"]["database"]["dsn_file"],
        context.snapshot["resolved"]["auth"]["feishu_secret_file"],
        context.snapshot["resolved"]["windows"]["token_file"],
    )
    checks.extend({"name": f"secret_{index}", "path": path} for index, path in enumerate(secret_paths))
    return checks


def wait_for_marker(
    path: Path,
    *,
    timeout: int,
    process: subprocess.Popen | None = None,
) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file() and not path.is_symlink():
            _, document = strict_json_file(path, path.name)
            require(
                isinstance(document, dict),
                "recovery_snapshot_worker_failed",
                f"control marker is not an object: {path}",
            )
            return document
        if process is not None and process.poll() is not None:
            failed = path.with_name("capture.failed.json")
            if failed.is_file():
                _, document = strict_json_file(failed, failed.name)
                raise RecoveryError(
                    document.get("code", "recovery_snapshot_worker_failed"),
                    document.get("message", "snapshot worker failed"),
                    document.get("detail"),
                )
            raise RecoveryError(
                "recovery_snapshot_worker_failed",
                "snapshot worker exited before publishing the expected marker",
                {"exit_code": process.returncode, "marker": path.name},
            )
        time.sleep(0.05)
    if process is not None and process.poll() is None:
        process.terminate()
    raise RecoveryError(
        "recovery_snapshot_worker_timeout",
        "snapshot worker handoff timed out",
        {"marker": path.name, "timeout_seconds": timeout},
    )


def _snapshot_worker(request_path: Path) -> int:
    _, request = strict_json_file(request_path, "snapshot worker request")
    require(
        isinstance(request, dict)
        and set(request)
        == {
            "schema_version",
            "control_dir",
            "staging",
            "database",
            "postgres",
            "timeout_seconds",
        }
        and request["schema_version"] == SNAPSHOT_WORKER_SCHEMA,
        "recovery_snapshot_worker_failed",
        "snapshot worker request is malformed",
    )
    control = Path(request["control_dir"])
    staging = Path(request["staging"])
    database = request["database"]
    postgres = request["postgres"]
    timeout = request["timeout_seconds"]
    require(
        control.is_absolute() and staging.is_absolute() and type(timeout) is int and timeout > 0,
        "recovery_snapshot_worker_failed",
        "snapshot worker paths/timeout are malformed",
    )
    require(
        isinstance(database, dict)
        and set(database) == {"socket_dir", "port", "database"}
        and database["database"] == DATABASE_NAME,
        "recovery_snapshot_worker_failed",
        "snapshot worker database authority is malformed",
    )
    require(
        isinstance(postgres, dict) and set(postgres) == {"bin_dir", "library_dir", "commands"},
        "recovery_snapshot_worker_failed",
        "snapshot worker PostgreSQL tools are malformed",
    )
    control.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(control, 0o700)
    maintenance = None
    snapshot = None
    lock_held = False
    try:
        maintenance = _db_connect(database, autocommit=True)
        lock_held = bool(
            _connection_scalar(
                maintenance,
                "SELECT pg_try_advisory_lock(%s)",
                (MAINTENANCE_LOCK_KEY,),
            )
        )
        require(
            lock_held,
            "recovery_maintenance_not_exclusive",
            "another recovery maintenance session holds the exclusive lock",
        )
        maintenance_before = _maintenance_probe(maintenance)
        _require_quiescent(maintenance_before, "before_snapshot")
        roles_live_before = _roles_settings_snapshot(maintenance)
        atomic_write(
            control / "role-settings-before.json",
            canonical_json(
                {
                    "roles_sha256": _digest_document(roles_live_before),
                    "database_settings": roles_live_before["database_settings"],
                }
            ),
        )
        atomic_write(
            control / "maintenance.ready.json",
            canonical_json(
                {
                    "status": "ready",
                    "lock_key": str(MAINTENANCE_LOCK_KEY),
                    "maintenance": maintenance_before,
                    "roles_sha256": _digest_document(roles_live_before),
                    "created_at": utc_now(),
                }
            ),
        )
        wait_for_marker(
            control / "inventory.complete.json",
            timeout=timeout,
        )

        snapshot = _db_connect(database)
        snapshot_id = _begin_exported_snapshot(snapshot)
        roles_snapshot_before = _roles_settings_snapshot(snapshot)
        catalog, data, checkpoint_rows = _database_baseline_snapshot(snapshot)
        sequence_before = data["sequences"]
        atomic_write(
            control / "sequence-before.json",
            canonical_json(sequence_before),
        )
        database_dir = staging / "database"
        database_dir.mkdir(mode=0o700, exist_ok=True)
        role_plan_document = {
            "bootstrap_role": BOOTSTRAP_ROLE,
            "application_roles": list(APPLICATION_ROLES),
            "roles": roles_snapshot_before["roles"],
            "memberships": roles_snapshot_before["memberships"],
        }
        write_json_once(
            database_dir / "cluster-roles.json",
            role_plan_document,
        )
        atomic_write(
            database_dir / "cluster-globals.sql",
            role_sql(role_plan_document).encode("utf-8"),
        )
        globals_sha256 = verify_canonical_globals(staging)
        write_json_once(database_dir / "catalog-baseline.json", catalog)
        write_json_once(database_dir / "data-baseline.json", data)
        atomic_write(
            control / "snapshot.ready.json",
            canonical_json(
                {
                    "status": "ready",
                    "snapshot_id": snapshot_id,
                    "roles_sha256": _digest_document(roles_snapshot_before),
                    "created_at": utc_now(),
                }
            ),
        )
        wait_for_marker(
            control / "files.complete.json",
            timeout=timeout,
        )
        indexed_checkpoints = checkpoint_index(
            checkpoint_rows,
            staging / "state" / "runs",
        )
        write_json_once(
            database_dir / "reference-index.json",
            indexed_checkpoints,
        )
        verify_checkpoint_index(
            indexed_checkpoints,
            staging / "state" / "runs",
        )

        dump_path = database_dir / "airflow_meta.dump"
        toc_path = database_dir / "dump.toc"
        commands = postgres["commands"]
        pg = command_context(
            Path(postgres["bin_dir"]),
            Path(postgres["library_dir"]),
            commands,
        )
        pg.run(
            [
                pg.bin_dir / "pg_dump",
                "-h",
                database["socket_dir"],
                "-p",
                str(database["port"]),
                "-U",
                BOOTSTRAP_ROLE,
                f"--snapshot={snapshot_id}",
                "-Fc",
                "--create",
                "-f",
                dump_path,
                DATABASE_NAME,
            ],
            timeout=max(timeout, 3600),
        )
        toc = pg.run([pg.bin_dir / "pg_restore", "-l", dump_path]).stdout
        atomic_write(toc_path, toc.encode("utf-8"))

        dump_positions, sequence_extraction = extract_dump_sequence_positions_streaming(
            pg,
            dump_path,
            sequence_before,
            toc,
            control,
        )
        atomic_write(
            control / "sequence-dump-setval.json",
            canonical_json(dump_positions),
        )
        atomic_write(
            control / "sequence-extraction.json",
            canonical_json(sequence_extraction),
        )
        sequence_after = _sequence_baseline(snapshot)
        atomic_write(
            control / "sequence-after.json",
            canonical_json(sequence_after),
        )
        write_json_once(
            database_dir / "sequence-setval.json",
            dump_positions,
        )
        roles_snapshot_after = _roles_settings_snapshot(snapshot)
        roles_live_after = _roles_settings_snapshot(maintenance)
        atomic_write(
            control / "role-settings-after.json",
            canonical_json(
                {
                    "roles_sha256": _digest_document(roles_live_after),
                    "database_settings": roles_live_after["database_settings"],
                }
            ),
        )
        mid_maintenance = _maintenance_probe(maintenance)
        atomic_write(
            control / "maintenance.post-dump.json",
            canonical_json(mid_maintenance),
        )
        _require_quiescent(mid_maintenance, "after_dump")
        require(
            sequence_before == sequence_after,
            "recovery_sequence_changed_during_backup",
            "sequence state changed between baseline and pg_dump completion",
            {
                "before": sequence_before,
                "after": sequence_after,
            },
        )
        verify_sequence_dump_positions(sequence_after, dump_positions)
        require(
            _digest_document(roles_snapshot_before)
            == _digest_document(roles_snapshot_after)
            == _digest_document(roles_live_before)
            == _digest_document(roles_live_after),
            "recovery_roles_changed_during_backup",
            "roles, memberships, role settings or database settings changed",
        )
        live_data_after_dump = _live_database_data(maintenance)
        atomic_write(
            control / "data-live.post-dump.json",
            canonical_json(live_data_after_dump),
        )
        require(
            live_data_after_dump["sequences"] == data["sequences"],
            "recovery_sequence_changed_during_backup",
            "live sequence state changed before the post-dump file inventory",
            {
                "before": sequence_positions(data["sequences"]),
                "after": sequence_positions(live_data_after_dump["sequences"]),
            },
        )
        require(
            live_data_after_dump == data,
            "recovery_database_changed_during_backup",
            "live database data changed before the post-dump file inventory",
            {
                "baseline_sha256": _digest_document(data),
                "live_sha256": _digest_document(live_data_after_dump),
            },
        )
        atomic_write(
            control / "dump.complete.json",
            canonical_json(
                {
                    "status": "complete",
                    "snapshot_id": snapshot_id,
                    "created_at": utc_now(),
                }
            ),
        )
        wait_for_marker(
            control / "inventory.after.complete.json",
            timeout=timeout,
        )
        sequence_final = _sequence_baseline(snapshot)
        atomic_write(
            control / "sequence-final.json",
            canonical_json(sequence_final),
        )
        require(
            sequence_before == sequence_final,
            "recovery_sequence_changed_during_backup",
            "sequence state changed before the post-inventory consistency check",
            {
                "before": sequence_before,
                "after": sequence_final,
            },
        )
        verify_sequence_dump_positions(sequence_final, dump_positions)
        roles_snapshot_final = _roles_settings_snapshot(snapshot)
        roles_live_final = _roles_settings_snapshot(maintenance)
        atomic_write(
            control / "role-settings-final.json",
            canonical_json(
                {
                    "roles_sha256": _digest_document(roles_live_final),
                    "database_settings": roles_live_final["database_settings"],
                }
            ),
        )
        maintenance_after = _maintenance_probe(maintenance)
        _require_quiescent(maintenance_after, "after_inventory")
        require(
            _digest_document(roles_snapshot_before)
            == _digest_document(roles_snapshot_final)
            == _digest_document(roles_live_before)
            == _digest_document(roles_live_final),
            "recovery_roles_changed_during_backup",
            "roles, memberships or database settings changed across the backup window",
        )
        live_data_final = _live_database_data(maintenance)
        atomic_write(
            control / "data-live.final.json",
            canonical_json(live_data_final),
        )
        require(
            live_data_final["sequences"] == data["sequences"],
            "recovery_sequence_changed_during_backup",
            "live sequence state changed before the post-inventory check",
            {
                "before": sequence_positions(data["sequences"]),
                "after": sequence_positions(live_data_final["sequences"]),
            },
        )
        require(
            live_data_final == data,
            "recovery_database_changed_during_backup",
            "live database data changed during the quiescent backup window",
            {
                "baseline_sha256": _digest_document(data),
                "live_sha256": _digest_document(live_data_final),
            },
        )
        consistency = {
            "isolation": "REPEATABLE READ READ ONLY",
            "snapshot_id": snapshot_id,
            "snapshot_held_until_pg_dump_complete": True,
            "roles_snapshot_before_sha256": _digest_document(roles_snapshot_before),
            "roles_snapshot_after_sha256": _digest_document(roles_snapshot_final),
            "roles_live_before_sha256": _digest_document(roles_live_before),
            "roles_live_after_sha256": _digest_document(roles_live_final),
            "sequence_before_sha256": _digest_document(sequence_positions(sequence_before)),
            "sequence_after_sha256": _digest_document(sequence_positions(sequence_final)),
            "sequence_dump_setval_sha256": _digest_document(dump_positions),
            "sequence_extraction": sequence_extraction,
            "cluster_globals_sha256": globals_sha256,
            "database_baseline_sha256": _digest_document(data),
            "database_live_after_dump_sha256": _digest_document(live_data_after_dump),
            "database_live_after_inventory_sha256": _digest_document(live_data_final),
            "maintenance_before": maintenance_before,
            "maintenance_after": maintenance_after,
            "exclusive_lock_key": str(MAINTENANCE_LOCK_KEY),
            "exclusive_lock_held": True,
        }
        write_json_once(
            control / "capture.completed.json",
            {
                "status": "completed",
                "airflow_alembic_head": data["airflow_alembic_heads"][0],
                "consistency": consistency,
                "commands": commands,
            },
        )
        return 0
    except Exception as error:  # noqa: BLE001 - worker records every failure boundary
        detail = {
            "code": getattr(error, "code", "recovery_snapshot_worker_failed"),
            "message": getattr(error, "message", str(error)),
            "detail": getattr(error, "detail", {}),
        }
        atomic_write(
            control / "capture.failed.json",
            canonical_json(detail),
        )
        return 1
    finally:
        if snapshot is not None:
            with contextlib.suppress(Exception):
                snapshot.rollback()
            snapshot.close()
        if maintenance is not None:
            if lock_held:
                with contextlib.suppress(Exception):
                    _connection_scalar(
                        maintenance,
                        "SELECT pg_advisory_unlock(%s)",
                        (MAINTENANCE_LOCK_KEY,),
                    )
            maintenance.close()


def _command_evidence(
    argv: list[str | Path],
    completed: subprocess.CompletedProcess,
) -> dict:
    return {
        "argv": [str(value) for value in argv],
        "returncode": completed.returncode,
        "stdout_sha256": hashlib.sha256(completed.stdout.encode("utf-8")).hexdigest(),
        "stderr_sha256": hashlib.sha256(completed.stderr.encode("utf-8")).hexdigest(),
    }


def _run_maintenance_command(
    argv: list[str | Path],
    *,
    timeout: int,
    env: dict[str, str] | None = None,
    pass_fds: tuple[int, ...] = (),
) -> tuple[subprocess.CompletedProcess, dict]:
    command = [str(value) for value in argv]
    child_env = os.environ.copy()
    child_env.update(env or {})
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=child_env,
            pass_fds=pass_fds,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise RecoveryError(
            "recovery_maintenance_unavailable",
            "the maintenance command could not be executed",
            {"program": Path(command[0]).name, "error": str(error)},
        ) from error
    evidence = _command_evidence(argv, completed)
    require(
        completed.returncode == 0,
        "recovery_maintenance_unavailable",
        "the maintenance command did not prove success",
        {
            "program": Path(command[0]).name,
            "returncode": completed.returncode,
            "stderr": "\n".join(completed.stderr.strip().splitlines()[-8:]),
        },
    )
    return completed, evidence


def _require_airflow_interpreter(context: BackupContext) -> Path:
    airflow_python = Path(context.derived_env["AIRFLOW_VENV"]) / "bin" / "python"
    require(
        airflow_python.is_file() and os.access(airflow_python, os.X_OK),
        "recovery_airflow_interpreter_required",
        "AIRFLOW_VENV/bin/python is missing or not executable",
    )
    try:
        same_interpreter = os.path.samefile(sys.executable, airflow_python)
    except OSError as error:
        raise RecoveryError(
            "recovery_airflow_interpreter_required",
            "cannot compare the running interpreter with AIRFLOW_VENV",
            {"error": str(error)},
        ) from error
    require(
        same_interpreter,
        "recovery_airflow_interpreter_required",
        "backup must run with the AIRFLOW_VENV interpreter providing psycopg2",
        {"running": sys.executable, "required": str(airflow_python)},
    )
    return airflow_python


def _wait_database_quiescence(
    database: dict,
    *,
    timeout: int,
) -> dict:
    deadline = time.monotonic() + timeout
    connection = _db_connect(database["authority"], autocommit=True)
    try:
        while True:
            probe = _maintenance_probe(connection)
            ready = (
                probe["admission"].get("state") == "closed"
                and probe["settled"] is True
                and probe["unsettled_count"] == 0
                and probe["active_application_sessions"] == []
            )
            if ready:
                return probe
            if time.monotonic() >= deadline:
                _require_quiescent(probe, "maintenance_gate")
            time.sleep(1)
    finally:
        connection.close()


def _linux_unit_states(*, timeout: int) -> list[dict]:
    states = []
    for unit in MAINTENANCE_UNITS:
        argv = ["systemctl", "--user", "is-active", unit]
        try:
            completed = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=min(timeout, 30),
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise RecoveryError(
                "recovery_maintenance_unavailable",
                "cannot inspect a Linux maintenance unit",
                {"unit": unit, "error": str(error)},
            ) from error
        active_state = completed.stdout.strip()
        require(
            active_state in {"inactive", "failed"},
            "recovery_maintenance_not_exclusive",
            "a supported Linux writer is still active",
            {
                "unit": unit,
                "active_state": active_state or "unknown",
                "returncode": completed.returncode,
                "stderr": "\n".join(completed.stderr.strip().splitlines()[-4:]),
            },
        )
        states.append({"unit": unit, "active_state": active_state})
    return states


def _powershell_script(script: str) -> str:
    return base64.b64encode(script.encode("utf-16le")).decode("ascii")


def _windows_task_state(
    *,
    ssh_host: str,
    task_name: str,
    timeout: int,
) -> tuple[str, dict]:
    escaped = task_name.replace("'", "''")
    script = (
        f"$ErrorActionPreference='Stop';$task=Get-ScheduledTask -TaskName '{escaped}';[Console]::Out.Write($task.State)"
    )
    argv = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={min(timeout, 30)}",
        ssh_host,
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-EncodedCommand",
        _powershell_script(script),
    ]
    completed, evidence = _run_maintenance_command(argv, timeout=min(timeout, 60))
    state = completed.stdout.strip()
    require(
        state in {"Disabled", "Ready", "Queued", "Running"},
        "recovery_maintenance_unavailable",
        "the Windows maintenance task state is unreadable",
        {"task": task_name, "state": state or "unknown"},
    )
    return state, evidence


def _disable_windows_task(
    *,
    ssh_host: str,
    task_name: str,
    timeout: int,
) -> tuple[str, list[dict]]:
    evidence = []
    initial_state, initial_evidence = _windows_task_state(
        ssh_host=ssh_host,
        task_name=task_name,
        timeout=timeout,
    )
    evidence.append(initial_evidence)
    if initial_state != "Disabled":
        escaped = task_name.replace("'", "''")
        script = (
            "$ErrorActionPreference='Stop';"
            f"$task=Get-ScheduledTask -TaskName '{escaped}';"
            "if ($task.State -eq 'Running') {"
            f"Stop-ScheduledTask -TaskName '{escaped}' -ErrorAction Stop;"
            "};"
            f"Disable-ScheduledTask -TaskName '{escaped}' -ErrorAction Stop | Out-Null;"
            "[Console]::Out.Write('Disabled')"
        )
        argv = [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            f"ConnectTimeout={min(timeout, 30)}",
            ssh_host,
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-EncodedCommand",
            _powershell_script(script),
        ]
        _, disable_evidence = _run_maintenance_command(
            argv,
            timeout=min(timeout, 60),
        )
        evidence.append(disable_evidence)
    final_state, final_evidence = _windows_task_state(
        ssh_host=ssh_host,
        task_name=task_name,
        timeout=timeout,
    )
    evidence.append(final_evidence)
    require(
        final_state == "Disabled",
        "recovery_maintenance_not_exclusive",
        "the Windows native writer task is not disabled",
        {"task": task_name, "state": final_state},
    )
    return final_state, evidence


def quiesce_maintenance(
    context: BackupContext,
    database: dict,
    *,
    operatorctl_path: Path,
    timeout: int,
    maintenance_lock: MaintenanceLock | None = None,
) -> dict:
    _require_airflow_interpreter(context)
    operatorctl_path = Path(operatorctl_path).resolve()
    require_real_file(
        operatorctl_path,
        "operatorctl",
        "recovery_maintenance_unavailable",
    )
    bash = shutil.which("bash")
    require(
        bash is not None,
        "recovery_maintenance_unavailable",
        "bash is unavailable for operatorctl",
    )
    opened_at = utc_now()
    drain_argv = [
        bash,
        operatorctl_path,
        "drain",
        "--config",
        context.config_path,
    ]
    inherited = {}
    inherited_fds = ()
    if maintenance_lock is not None:
        require(
            maintenance_lock.descriptor >= 0,
            "recovery_maintenance_unavailable",
            "the maintenance lock descriptor is closed",
        )
        inherited = {
            "OPERATORCTL_MAINTENANCE_LOCK_OWNER": maintenance_lock.owner_id,
            "OPERATORCTL_MAINTENANCE_LOCK_FD": str(
                maintenance_lock.descriptor
            ),
            "OPERATORCTL_MAINTENANCE_LOCK_PATH": str(maintenance_lock.path),
        }
        inherited_fds = (maintenance_lock.descriptor,)
    _, drain_evidence = _run_maintenance_command(
        drain_argv,
        timeout=timeout,
        env=inherited,
        pass_fds=inherited_fds,
    )
    _wait_database_quiescence(database, timeout=timeout)
    stop_argv = [
        bash,
        operatorctl_path,
        "stop",
        "--config",
        context.config_path,
        "--keep-database",
    ]
    _, stop_evidence = _run_maintenance_command(
        stop_argv,
        timeout=timeout,
        env=inherited,
        pass_fds=inherited_fds,
    )
    linux_states = _linux_unit_states(timeout=timeout)
    windows = context.resolved["windows"]
    instance = windows["instance"]
    ssh_host = windows["ssh_host"]
    task_name = instance["task_name"]
    windows_state, windows_evidence = _disable_windows_task(
        ssh_host=ssh_host,
        task_name=task_name,
        timeout=timeout,
    )
    database_before = _wait_database_quiescence(database, timeout=timeout)
    return {
        "opened_at": opened_at,
        "operatorctl_path": str(operatorctl_path.resolve()),
        "operatorctl_sha256": sha256_file(operatorctl_path),
        "drain": drain_evidence,
        "stop": stop_evidence,
        "linux": linux_states,
        "windows": {
            "task_name": task_name,
            "state": windows_state,
            "evidence": windows_evidence,
        },
        "database_before": database_before,
    }


def finalize_maintenance_receipt(
    context: BackupContext,
    database: dict,
    maintenance: dict,
    *,
    backup_id: str,
    timeout: int,
) -> dict:
    linux_after = _linux_unit_states(timeout=timeout)
    windows = context.resolved["windows"]
    task_name = windows["instance"]["task_name"]
    windows_state, windows_evidence = _windows_task_state(
        ssh_host=windows["ssh_host"],
        task_name=task_name,
        timeout=timeout,
    )
    require(
        windows_state == "Disabled",
        "recovery_maintenance_window_changed",
        "the Windows native writer task changed during backup",
        {"task": task_name, "state": windows_state},
    )
    database_after = _wait_database_quiescence(database, timeout=timeout)
    require(
        maintenance["linux"] == linux_after,
        "recovery_maintenance_window_changed",
        "Linux writer service state changed during backup",
    )
    require(
        _maintenance_state_identity(maintenance["database_before"]) == _maintenance_state_identity(database_after),
        "recovery_maintenance_window_changed",
        "database maintenance state changed during backup",
        {
            "before": maintenance["database_before"],
            "after": database_after,
        },
    )
    release_identity = {
        "version": context.release_document["version"],
        "source_commit": context.release_document["source_commit"],
        "source_sha256": context.release_document["source_sha256"],
    }
    return {
        "schema_version": MAINTENANCE_RECEIPT_SCHEMA,
        "status": "stopped",
        "backup_id": backup_id,
        "opened_at": maintenance["opened_at"],
        "closed_at": utc_now(),
        "identity": {
            "install_id": context.install_record["install_id"],
            "config_sha256": sha256_file(context.config_path),
            "operatorctl_path": maintenance["operatorctl_path"],
            "operatorctl_sha256": maintenance["operatorctl_sha256"],
            "release": release_identity,
        },
        "commands": {
            "drain": maintenance["drain"],
            "stop": maintenance["stop"],
        },
        "services": {
            "linux_before": maintenance["linux"],
            "linux_after": linux_after,
            "windows_before": maintenance["windows"],
            "windows_after": {
                "task_name": task_name,
                "state": windows_state,
                "evidence": windows_evidence,
            },
        },
        "database": {
            "authority": database["authority"],
            "before": maintenance["database_before"],
            "after": database_after,
        },
    }


def backup_command(
    *,
    config_path: Path,
    backup_id: str,
    key_file: Path,
    out: Path,
    operatorctl_path: Path | None = None,
) -> dict:
    require(
        bool(BACKUP_ID.fullmatch(backup_id)),
        "recovery_target_plan_invalid",
        "backup id is malformed",
    )
    out = Path(out)
    key_file = Path(key_file)
    require(out.is_absolute(), "recovery_target_path_unsafe", "--out must be absolute")
    require_no_links(out, "backup output")
    require_real_file(key_file, "external backup key", "recovery_key_unavailable")
    require(
        key_file.stat().st_mode & 0o077 == 0,
        "recovery_key_unavailable",
        "external backup key must not be group/world accessible",
    )
    context = load_backup_context(config_path)
    source_roots_for_key = [
        Path(context.resolved["storage"][name])
        for name in (
            "state_root",
            "handoff_root",
            "artifact_root",
            "release_root",
            "workspace_root",
        )
    ]
    source_roots_for_key.extend(Path(value) for value in context.resolved["specifications"]["record_roots"])
    for root in [out, *source_roots_for_key]:
        require(
            not key_file.resolve().is_relative_to(root.resolve()),
            "recovery_key_unavailable",
            "backup key must live outside the backed-up failure domain",
            {"root": str(root)},
        )

    lifecycle_lock = acquire_maintenance_lock(Path(context.resolved["storage"]["state_root"]))
    claim = None
    try:
        claim = claim_backup_output(out, backup_id)
        try:
            return _backup_command_claimed(
                context=context,
                lifecycle_lock=lifecycle_lock,
                claim=claim,
                backup_id=backup_id,
                key_file=key_file,
                out=out,
                operatorctl_path=operatorctl_path,
            )
        except BaseException as error:
            with contextlib.suppress(Exception):
                error.backup_claim = claim
            raise
    finally:
        lifecycle_lock.close()


def _backup_command_claimed(
    *,
    context: BackupContext,
    lifecycle_lock: MaintenanceLock,
    claim: BackupOutputClaim,
    backup_id: str,
    key_file: Path,
    out: Path,
    operatorctl_path: Path | None,
) -> dict:
    require_current_claim(claim)
    receipt_path = claim.receipt_path
    postgres_root = context.postgres_root
    bin_dir = postgres_root / "root" / "usr" / "lib" / "postgresql" / POSTGRES_MAJOR / "bin"
    library_dir = postgres_root / "root" / "usr" / "lib" / "x86_64-linux-gnu"
    commands: list[dict] = []
    pg = command_context(bin_dir, library_dir if library_dir.exists() else None, commands)
    require(
        postgres_major(bin_dir, pg) == POSTGRES_MAJOR,
        "recovery_postgres_version_mismatch",
        "backup tools are not PostgreSQL 14",
    )
    database = capture_database_authority(
        context.resolved,
        context.derived_env,
        postgres_root=postgres_root,
        context=pg,
    )
    socket_dir = Path(database["authority"]["socket_dir"])
    port = database["authority"]["port"]
    if operatorctl_path is None:
        operatorctl_path = Path(__file__).resolve().with_name("operatorctl.sh")
    else:
        operatorctl_path = Path(operatorctl_path)
    maintenance_pending_path = out / "maintenance-window.pending.json"
    maintenance_stopped_path = out / "maintenance-window.stopped.json"
    write_json_once(
        maintenance_pending_path,
        {
            "schema_version": "solidworks-to-urdf.recovery-maintenance-marker/v1",
            "backup_id": backup_id,
            "state": "starting",
            "config": str(context.config_path),
            "operatorctl_path": str(operatorctl_path.resolve()),
            "started_at": utc_now(),
        },
    )
    maintenance_state = quiesce_maintenance(
        context,
        database,
        operatorctl_path=operatorctl_path,
        timeout=3600,
        maintenance_lock=lifecycle_lock,
    )
    write_json_once(
        maintenance_stopped_path,
        {
            "schema_version": "solidworks-to-urdf.recovery-maintenance-marker/v1",
            "backup_id": backup_id,
            "state": "stopped",
            "config": str(context.config_path),
            "operatorctl_path": maintenance_state["operatorctl_path"],
            "operatorctl_sha256": maintenance_state["operatorctl_sha256"],
            "stopped_at": utc_now(),
        },
    )

    staging = out / "backup"
    require_current_claim(claim)
    try:
        backup.remove_private_staging(out)
    except SystemExit as error:
        raise RecoveryError(
            "recovery_target_path_unsafe",
            f"cannot clean previous private staging: {error}",
        ) from error
    staging.mkdir(mode=0o700)
    control = out / "snapshot-control"
    control.mkdir(mode=0o700)
    source_checks = backup_source_checks(context)
    airflow_venv = Path(context.derived_env["AIRFLOW_VENV"])
    airflow_python = airflow_venv / "bin" / "python"
    require(
        airflow_python.is_file() and os.access(airflow_python, os.X_OK),
        "recovery_airflow_interpreter_required",
        "AIRFLOW_VENV/bin/python is missing or not executable",
    )
    worker_timeout = 3600
    worker_request = control / "request.json"
    write_json_once(
        worker_request,
        {
            "schema_version": SNAPSHOT_WORKER_SCHEMA,
            "control_dir": str(control),
            "staging": str(staging),
            "database": {
                "socket_dir": str(socket_dir),
                "port": port,
                "database": DATABASE_NAME,
            },
            "postgres": {
                "bin_dir": str(bin_dir),
                "library_dir": str(library_dir),
                "commands": [],
            },
            "timeout_seconds": worker_timeout,
        },
    )
    worker_env = os.environ.copy()
    deploy_dir = str(Path(backup.__file__).resolve().parent)
    python_path = worker_env.get("PYTHONPATH", "")
    worker_env["PYTHONPATH"] = f"{deploy_dir}:{python_path}" if python_path else deploy_dir
    worker_log = (control / "worker.stdout.log").open("wb")
    worker = subprocess.Popen(
        [
            str(airflow_python),
            str(Path(__file__).resolve()),
            "snapshot-worker",
            "--request",
            str(worker_request),
        ],
        stdin=subprocess.DEVNULL,
        stdout=worker_log,
        stderr=subprocess.STDOUT,
        env=worker_env,
    )
    try:
        maintenance_ready = wait_for_marker(
            control / "maintenance.ready.json",
            timeout=120,
            process=worker,
        )
        inventory_before = source_inventory(source_checks)
        before_bytes = canonical_json(inventory_before)
        inventory_root = staging / "inventory"
        inventory_root.mkdir(mode=0o700)
        write_json_once(inventory_root / "source-before.json", inventory_before)
        write_json_once(control / "inventory-before.json", inventory_before)
        atomic_write(
            control / "inventory.complete.json",
            canonical_json(
                {
                    "status": "complete",
                    "sha256": hashlib.sha256(before_bytes).hexdigest(),
                    "roots": len(inventory_before),
                    "created_at": utc_now(),
                }
            ),
        )
        snapshot_ready = wait_for_marker(
            control / "snapshot.ready.json",
            timeout=120,
            process=worker,
        )

        # File copying remains strictly inside the exported DB snapshot window.
        copy_release_capture = copy_release_tree(context, staging)
        roots = root_contract(context, copy_release_capture)
        copy_state_configuration(
            Path(context.resolved["storage"]["state_root"]),
            context.install_record["install_id"],
            staging,
        )
        require(
            stat.S_IMODE(context.config_path.stat().st_mode) & 0o077 == 0,
            "recovery_release_missing",
            "platform.json must not be group/world accessible",
        )
        copy_required_file(context.config_path, staging / "config" / "platform.json")
        copy_required_file(
            Path(context.resolved["storage"]["release_root"]) / "release.json",
            staging / "config" / "release.json",
        )
        capture_checkpoint_store(
            Path(context.resolved["derived"]["store_root"]),
            staging,
        )
        copy_required_tree(
            Path(context.resolved["storage"]["handoff_root"]),
            staging / "handoff",
        )
        copy_required_tree(
            Path(context.resolved["storage"]["artifact_root"]),
            staging / "artifacts",
        )
        for index, root in enumerate(context.resolved["specifications"]["record_roots"]):
            copy_required_tree(
                Path(root),
                staging / "specifications" / f"root-{index}",
            )
        secrets, airflow = capture_secret_files(context, staging)
        airflow.update(parse_airflow_secrets(staging / airflow["airflow_config_member"]))
        staged_source_members_sha256, staged_source_members = verify_staged_source_members(
            context,
            inventory_before,
            staging,
        )
        write_json_once(
            inventory_root / "staged-source.json",
            staged_source_members,
        )
        atomic_write(
            control / "files.complete.json",
            canonical_json(
                {
                    "status": "complete",
                    "created_at": utc_now(),
                }
            ),
        )
        wait_for_marker(
            control / "dump.complete.json",
            timeout=worker_timeout,
            process=worker,
        )
        inventory_after = source_inventory(source_checks)
        after_bytes = canonical_json(inventory_after)
        write_json_once(inventory_root / "source-after.json", inventory_after)
        write_json_once(control / "inventory-after.json", inventory_after)
        require(
            inventory_after == inventory_before,
            "recovery_source_changed",
            "a required source root changed during the snapshot/copy/dump window",
            {
                "before_sha256": hashlib.sha256(before_bytes).hexdigest(),
                "after_sha256": hashlib.sha256(after_bytes).hexdigest(),
            },
        )
        atomic_write(
            control / "inventory.after.complete.json",
            canonical_json(
                {
                    "status": "complete",
                    "sha256": hashlib.sha256(after_bytes).hexdigest(),
                    "roots": len(inventory_after),
                    "created_at": utc_now(),
                }
            ),
        )
        capture_result = wait_for_marker(
            control / "capture.completed.json",
            timeout=worker_timeout,
            process=worker,
        )
        returncode = worker.wait(timeout=30)
        require(
            returncode == 0,
            "recovery_snapshot_worker_failed",
            "snapshot worker exited unsuccessfully",
            {"exit_code": returncode},
        )
        require(
            maintenance_ready.get("lock_key") == str(MAINTENANCE_LOCK_KEY)
            and snapshot_ready.get("snapshot_id") == capture_result["consistency"]["snapshot_id"],
            "recovery_snapshot_worker_failed",
            "snapshot worker handoff identity is inconsistent",
        )
    finally:
        if worker.poll() is None:
            worker.terminate()
            try:
                worker.wait(timeout=10)
            except subprocess.TimeoutExpired:
                worker.kill()
                worker.wait(timeout=10)
        worker_log.close()

    maintenance_receipt = finalize_maintenance_receipt(
        context,
        database,
        maintenance_state,
        backup_id=backup_id,
        timeout=120,
    )
    maintenance_path = staging / "maintenance" / "receipt.json"
    atomic_write(maintenance_path, canonical_json(maintenance_receipt))
    maintenance_operatorctl_path = staging / "maintenance" / "operatorctl.sh"
    copy_required_file(
        Path(maintenance_state["operatorctl_path"]),
        maintenance_operatorctl_path,
        mode=0o755,
    )
    maintenance_operatorctl = {
        "member": "maintenance/operatorctl.sh",
        "bytes": maintenance_operatorctl_path.stat().st_size,
        "sha256": sha256_file(maintenance_operatorctl_path),
        "mode": stat.S_IMODE(maintenance_operatorctl_path.stat().st_mode),
    }
    require(
        maintenance_operatorctl["sha256"] == maintenance_receipt["identity"]["operatorctl_sha256"],
        "recovery_maintenance_window_changed",
        "the archived operatorctl differs from the executed operatorctl",
    )

    indexed_checkpoints = _load_extracted_json(
        staging,
        "database/reference-index.json",
        "consistent checkpoint reference index",
    )
    alembic_head = capture_result["airflow_alembic_head"]
    commands.extend(capture_result["commands"])

    reconstruct_dir = staging / "reconstruct"
    reconstruct_dir.mkdir(mode=0o700)
    write_json_once(reconstruct_dir / "workspace-plan.json", context.workspace_plan)

    inventory_root = staging / "inventory"
    inventory_root.mkdir(mode=0o700, exist_ok=True)
    normalize_generated_modes(staging)
    initial_inventory = [row for row in manifest_members(staging) if row["member"] != "inventory/files.json"]
    write_json_once(inventory_root / "files.json", initial_inventory)

    inventory_after = source_inventory(source_checks)
    after_bytes = canonical_json(inventory_after)
    require(
        inventory_after == inventory_before,
        "recovery_source_changed",
        "a required source root changed during backup",
    )
    members = manifest_members(staging)
    inventory_member = next(row for row in members if row["member"] == "inventory/files.json")
    inventory_rows = []
    for row in members:
        copied = dict(row)
        if copied["member"] == "inventory/files.json":
            copied["class"] = "recovery-inventory"
        elif copied["member"].startswith("database/"):
            copied["class"] = "database"
        elif copied["member"].startswith("maintenance"):
            copied["class"] = "maintenance"
        elif copied["member"].startswith("config/state/"):
            copied["class"] = "config"
        elif copied["member"].startswith("state/"):
            copied["class"] = "checkpoint"
        elif copied["member"].startswith("handoff/"):
            copied["class"] = "handoff"
        elif copied["member"].startswith("artifacts/"):
            copied["class"] = "artifact"
        elif copied["member"].startswith("specifications/"):
            copied["class"] = "original"
        elif copied["member"].startswith("secrets/"):
            copied["class"] = "secret"
        elif copied["member"].startswith("release/"):
            copied["class"] = "release"
        elif copied["member"].startswith("reconstruct/"):
            copied["class"] = "reconstruct-plan"
        inventory_rows.append(copied)
    require(
        inventory_member["member"] == "inventory/files.json",
        "recovery_member_hash_mismatch",
        "recovery inventory member is missing",
    )
    verify_manifest_source_binding(
        staging,
        inventory_rows,
        staged_source_members,
    )
    canonical_root_modes = {
        "state_root": stat.S_IMODE((staging / "config" / "state").stat().st_mode),
        "release_root": stat.S_IMODE((staging / "release").stat().st_mode),
    }
    require(
        canonical_root_modes
        == {
            "state_root": 0o700,
            "release_root": 0o700,
        },
        "recovery_staged_source_mismatch",
        "canonical state_root/release_root staging modes are not 0700",
        {"modes": canonical_root_modes},
    )

    consistency = {
        **capture_result["consistency"],
        "maintenance_lock": {
            "kind": "exclusive-filesystem-lock",
            "path": str(lifecycle_lock.path),
            "owner_id": lifecycle_lock.owner_id,
            "held": True,
        },
        "source_inventory_before_sha256": hashlib.sha256(canonical_json(inventory_before)).hexdigest(),
        "source_inventory_after_sha256": hashlib.sha256(canonical_json(inventory_after)).hexdigest(),
        "source_inventory_equal": True,
        "source_inventory_roots": len(inventory_before),
        "source_inventory_files": sum(len(row["inventory"]) for row in inventory_before),
        "staged_source_members_sha256": staged_source_members_sha256,
        "staged_source_members_equal": True,
        "staged_source_members_count": len(staged_source_members),
        "canonical_root_modes": canonical_root_modes,
    }
    archive_path = out / "recovery.tar.enc"
    require_current_claim(claim)
    archive_sha = backup.seal(staging, key_file, archive_path)
    try:
        verify_manifest_source_binding(
            staging,
            inventory_rows,
            staged_source_members,
        )
        consistency["archive_member_verification"] = verify_archive_member_binding(
            archive=archive_path,
            key=key_file,
            manifest_members=inventory_rows,
            staged_source_members=staged_source_members,
            work_dir=out,
        )
    except Exception:
        archive_path.unlink(missing_ok=True)
        raise
    key_id = hashlib.sha256(key_file.read_bytes()).hexdigest()
    artifact_rows = copy_release_capture.pop("artifacts")
    maintenance_member = {
        "member": "maintenance/receipt.json",
        "bytes": maintenance_path.stat().st_size,
        "sha256": sha256_file(maintenance_path),
    }
    manifest = {
        "schema_version": MANIFEST_SCHEMA,
        "backup_id": backup_id,
        "created_at": utc_now(),
        "source_code": {
            **context.source_code,
            "script_sha256": sha256_file(Path(__file__)),
        },
        "release": {
            **context.release_document,
            "artifacts": artifact_rows,
        },
        "database": {
            **database,
            "airflow_alembic_head": alembic_head,
            "cluster_globals": derived_dump_member(staging, "cluster-globals.sql"),
            "dump": derived_dump_member(staging, "airflow_meta.dump"),
            "toc": derived_dump_member(staging, "dump.toc"),
            "catalog_baseline": derived_dump_member(staging, "catalog-baseline.json"),
            "data_baseline": derived_dump_member(staging, "data-baseline.json"),
            "reference_index": derived_dump_member(staging, "reference-index.json"),
            "sequence_setval": derived_dump_member(staging, "sequence-setval.json"),
            "snapshot": {
                "transaction": capture_result["consistency"]["isolation"],
                "id": capture_result["consistency"]["snapshot_id"],
                "pg_dump_snapshot": capture_result["consistency"]["snapshot_id"],
                "held_until_pg_dump_complete": True,
            },
        },
        "roles": {
            "bootstrap_role": BOOTSTRAP_ROLE,
            "application_roles": list(APPLICATION_ROLES),
            "member": "database/cluster-roles.json",
        },
        "airflow": airflow,
        "maintenance": {
            **maintenance_member,
            "receipt": maintenance_receipt,
            "operatorctl": maintenance_operatorctl,
        },
        "files": [
            {
                "name": "platform_config",
                "path": str(context.config_path),
                "member": "config/platform.json",
                "mode": 0o600,
                "required": True,
            }
        ],
        "roots": roots,
        "members": inventory_rows,
        "references": indexed_checkpoints,
        "reconstructibles": {
            "release_root": {
                "path": context.resolved["storage"]["release_root"],
                "recipe": "offline-release-artifacts",
            },
            "workspace_root": {
                "path": context.resolved["storage"]["workspace_root"],
                "plan_member": "reconstruct/workspace-plan.json",
                "recipe": "git-origin-head-clean",
            },
            "external": [
                "windows-task-state",
                "github-pr-and-remote-refs",
            ],
        },
        "secrets": secrets,
        "archive": {
            "path": "recovery.tar.enc",
            "sha256": archive_sha,
            "cipher": "aes-256-cbc-pbkdf2",
            "key_id": key_id,
        },
        "producer": {
            "host_id": context.host_id,
            "python": sys.version.split()[0],
            "postgres_client_major": POSTGRES_MAJOR,
            "postgres_bin_dir": str(bin_dir),
            "postgres_library_dir": str(library_dir),
        },
        "consistency": consistency,
    }
    validate_manifest(manifest, expected_manifest_sha=None)
    require_current_claim(claim)
    manifest_path = out / "recovery-manifest.json"
    manifest_bytes = canonical_json(manifest)
    atomic_write(manifest_path, manifest_bytes)
    manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()
    atomic_write(
        out / "recovery-manifest.sha256",
        f"{manifest_sha}  recovery-manifest.json\n".encode("ascii"),
    )
    write_json_once(
        out / "command-log.json",
        {"commands": commands},
    )
    staging_removed = False
    cleanup_error = None
    try:
        require_current_claim(claim)
        staging_removed = backup.remove_private_staging(out)
    except (SystemExit, OSError, RecoveryError) as error:
        cleanup_error = str(error)
    final_receipt = {
        "schema_version": RECEIPT_SCHEMA,
        "operation": "backup",
        "backup_id": backup_id,
        "claim_id": claim.claim_id,
        "status": "succeeded",
        "manifest_sha256": manifest_sha,
        "archive_sha256": archive_sha,
        "snapshot_id": capture_result["consistency"]["snapshot_id"],
        "source_inventory_before_sha256": consistency["source_inventory_before_sha256"],
        "source_inventory_after_sha256": consistency["source_inventory_after_sha256"],
        "maintenance_receipt": str(out / "maintenance-receipt.json"),
        "maintenance_receipt_sha256": maintenance_member["sha256"],
        "maintenance_state": "stopped",
        "exclusive_lock_key": consistency["exclusive_lock_key"],
        "admission_after": consistency["maintenance_after"]["admission"],
        "settled_after": consistency["maintenance_after"]["settled"],
        "private_staging_removed": staging_removed,
        "cleanup_error": cleanup_error,
        "completed_at": utc_now(),
    }
    require_current_claim(claim)
    atomic_write(receipt_path, canonical_json(final_receipt))
    write_json_once(
        out / "maintenance-receipt.json",
        maintenance_receipt,
    )
    atomic_write(
        out / "maintenance-receipt.sha256",
        f"{maintenance_member['sha256']}  maintenance-receipt.json\n".encode("ascii"),
    )
    return {
        "manifest": str(manifest_path),
        "manifest_sha256": manifest_sha,
        "archive": str(archive_path),
        "archive_sha256": archive_sha,
        "maintenance_receipt": str(out / "maintenance-receipt.json"),
        "maintenance_receipt_sha256": maintenance_member["sha256"],
        "cleanup_warning": cleanup_error,
    }


MANIFEST_BLOCKS = {
    "schema_version",
    "backup_id",
    "created_at",
    "source_code",
    "release",
    "database",
    "roles",
    "airflow",
    "maintenance",
    "files",
    "roots",
    "members",
    "references",
    "reconstructibles",
    "secrets",
    "archive",
    "producer",
    "consistency",
}


def _exact_keys(document: object, keys: set[str], where: str) -> dict:
    require(
        isinstance(document, dict) and set(document) == keys,
        "recovery_manifest_invalid",
        f"{where} must carry exactly {sorted(keys)}",
    )
    return document


def validate_manifest(document: object, *, expected_manifest_sha: str | None) -> dict:
    doc = _exact_keys(document, MANIFEST_BLOCKS, "recovery manifest")
    require(
        doc["schema_version"] == MANIFEST_SCHEMA,
        "recovery_schema_unsupported",
        "recovery manifest schema is not supported",
    )
    require(
        isinstance(doc["backup_id"], str) and bool(BACKUP_ID.fullmatch(doc["backup_id"])),
        "recovery_manifest_invalid",
        "recovery backup id is malformed",
    )
    require(
        isinstance(doc["created_at"], str) and bool(doc["created_at"]),
        "recovery_manifest_invalid",
        "recovery creation time is missing",
    )
    source_code = _exact_keys(
        doc["source_code"],
        {"commit", "tree", "worktree", "script_sha256"},
        "source_code",
    )
    require(
        isinstance(source_code["commit"], str)
        and bool(GIT_OBJECT.fullmatch(source_code["commit"]))
        and isinstance(source_code["tree"], str)
        and bool(GIT_OBJECT.fullmatch(source_code["tree"]))
        and source_code["worktree"] == "clean"
        and isinstance(source_code["script_sha256"], str)
        and bool(HEX64.fullmatch(source_code["script_sha256"])),
        "recovery_manifest_invalid",
        "source code identity is malformed",
    )
    release = _exact_keys(
        doc["release"],
        {"version", "source_commit", "source_sha256", "artifacts"},
        "release",
    )
    require(
        isinstance(release["version"], str)
        and bool(release["version"])
        and isinstance(release["source_commit"], str)
        and bool(GIT_OBJECT.fullmatch(release["source_commit"]))
        and isinstance(release["source_sha256"], str)
        and bool(HEX64.fullmatch(release["source_sha256"]))
        and isinstance(release["artifacts"], list)
        and bool(release["artifacts"]),
        "recovery_manifest_invalid",
        "release identity is malformed",
    )
    for index, row in enumerate(release["artifacts"]):
        _exact_keys(
            row,
            {"name", "member", "bytes", "sha256"},
            f"release.artifacts[{index}]",
        )
        confined_member(row["name"], f"release.artifacts[{index}].name")
        require(
            row["member"] == f"release/artifacts/{row['name']}"
            and type(row["bytes"]) is int
            and row["bytes"] >= 0
            and isinstance(row["sha256"], str)
            and bool(HEX64.fullmatch(row["sha256"])),
            "recovery_manifest_invalid",
            f"release artifact identity is malformed: {row['name']}",
        )
    archive = _exact_keys(
        doc["archive"],
        {"path", "sha256", "cipher", "key_id"},
        "archive",
    )
    require(
        archive["path"] == "recovery.tar.enc"
        and isinstance(archive["sha256"], str)
        and bool(HEX64.fullmatch(archive["sha256"]))
        and isinstance(archive["key_id"], str)
        and bool(HEX64.fullmatch(archive["key_id"]))
        and archive["cipher"] == "aes-256-cbc-pbkdf2",
        "recovery_manifest_invalid",
        "archive identity is malformed",
    )
    members = doc["members"]
    require(
        isinstance(members, list) and bool(members),
        "recovery_manifest_invalid",
        "recovery member inventory is empty",
    )
    seen = set()
    for index, row in enumerate(members):
        _exact_keys(
            row,
            {"member", "type", "bytes", "sha256", "mode", "required", "class"},
            f"member[{index}]",
        )
        name = confined_member(row["member"])
        require(
            name.split("/", 1)[0] in ARCHIVE_TOP_LEVEL,
            "recovery_manifest_invalid",
            f"member is outside the archive top-level allowlist: {name}",
        )
        require(
            name not in seen,
            "recovery_manifest_invalid",
            f"duplicate recovery member: {name}",
        )
        seen.add(name)
        require(
            row["type"] in {"file", "directory"},
            "recovery_manifest_invalid",
            f"member type is unsupported: {name}",
        )
        require(
            type(row["bytes"]) is int and row["bytes"] >= 0,
            "recovery_manifest_invalid",
            f"member size is malformed: {name}",
        )
        require(
            isinstance(row["required"], bool),
            "recovery_manifest_invalid",
            f"member required flag is malformed: {name}",
        )
        require(
            isinstance(row["class"], str) and bool(row["class"]),
            "recovery_manifest_invalid",
            f"member class is malformed: {name}",
        )
        clean_mode(row["mode"])
        if row["type"] == "directory":
            require(
                row["bytes"] == 0 and row["sha256"] is None,
                "recovery_manifest_invalid",
                f"directory member carries file digest fields: {name}",
            )
        else:
            require(
                isinstance(row["sha256"], str) and bool(HEX64.fullmatch(row["sha256"])),
                "recovery_manifest_invalid",
                f"file member digest is malformed: {name}",
            )
    members_by_name = {row["member"]: row for row in members}

    def require_bound_file(
        member: object,
        *,
        expected_bytes: object | None = None,
        expected_sha: object | None = None,
        where: str,
    ) -> None:
        name = confined_member(member, where)
        row = members_by_name.get(name)
        require(
            isinstance(row, dict) and row["type"] == "file",
            "recovery_manifest_invalid",
            f"{where} does not name an archived file: {name}",
        )
        if expected_bytes is not None:
            require(
                row["bytes"] == expected_bytes,
                "recovery_manifest_invalid",
                f"{where} byte size differs from the member inventory",
            )
        if expected_sha is not None:
            require(
                row["sha256"] == expected_sha,
                "recovery_manifest_invalid",
                f"{where} digest differs from the member inventory",
            )

    for index, row in enumerate(release["artifacts"]):
        require_bound_file(
            row["member"],
            expected_bytes=row["bytes"],
            expected_sha=row["sha256"],
            where=f"release.artifacts[{index}]",
        )
    roots = doc["roots"]
    require(
        isinstance(roots, list) and bool(roots),
        "recovery_manifest_invalid",
        "recovery root map is empty",
    )
    root_names = set()
    for index, row in enumerate(roots):
        allowed = {
            "name",
            "path",
            "archive",
            "class",
            "required",
            "reconstructible_runtime",
        }
        require(
            isinstance(row, dict) and set(row) in (allowed, allowed - {"reconstructible_runtime"}),
            "recovery_manifest_invalid",
            f"root[{index}] has unknown or missing fields",
        )
        name = row["name"]
        require(
            isinstance(name, str) and bool(name) and name not in root_names,
            "recovery_manifest_invalid",
            "recovery root names are malformed or duplicated",
        )
        root_names.add(name)
        absolute_text(row["path"], f"root[{index}].path")
        confined_member(row["archive"], f"root[{index}].archive")
        require(
            row["required"] is True,
            "recovery_manifest_invalid",
            f"root[{index}] is not required",
        )
        require(
            row["archive"] in members_by_name and members_by_name[row["archive"]]["type"] == "directory",
            "recovery_manifest_invalid",
            f"root[{index}] archive prefix is not an archived directory",
        )
    for index, row in enumerate(doc["files"]):
        _exact_keys(
            row,
            {"name", "path", "member", "mode", "required"},
            f"files[{index}]",
        )
        absolute_text(row["path"], f"files[{index}].path")
        confined_member(row["member"], f"files[{index}].member")
        require(
            row["mode"] == 0o600 and row["required"] is True,
            "recovery_manifest_invalid",
            f"files[{index}] is not required 0600",
        )
        require_bound_file(row["member"], where=f"files[{index}]")
    platform_config_rows = [row for row in doc["files"] if row["name"] == "platform_config"]
    require(
        len(platform_config_rows) == 1,
        "recovery_manifest_invalid",
        "the manifest must bind exactly one platform configuration",
    )
    platform_config_path = platform_config_rows[0]["path"]
    for index, row in enumerate(doc["secrets"]):
        _exact_keys(
            row,
            {"name", "path", "member", "mode", "required"},
            f"secret[{index}]",
        )
        absolute_text(row["path"], f"secret[{index}].path")
        confined_member(row["member"], f"secret[{index}].member")
        require(
            row["mode"] == 0o600 and row["required"] is True,
            "recovery_manifest_invalid",
            f"secret[{index}] is not required 0600",
        )
        require_bound_file(row["member"], where=f"secret[{index}]")
    database = _exact_keys(
        doc["database"],
        {
            "engine",
            "major",
            "server_version_num",
            "authority",
            "bootstrap_role",
            "application_roles",
            "schemas",
            "dsn_sources",
            "airflow_alembic_head",
            "cluster_globals",
            "dump",
            "toc",
            "catalog_baseline",
            "data_baseline",
            "reference_index",
            "sequence_setval",
            "snapshot",
        },
        "database",
    )
    require(
        database["engine"] == "postgresql"
        and database["major"] == 14
        and database["bootstrap_role"] == BOOTSTRAP_ROLE
        and database["application_roles"] == list(APPLICATION_ROLES)
        and database["schemas"] == ["airflow", "platform"],
        "recovery_schema_unsupported",
        "recovery database topology is unsupported",
    )
    authority = _exact_keys(
        database["authority"],
        {
            "system_identifier",
            "catalog_version",
            "database",
            "database_oid",
            "socket_dir",
            "port",
        },
        "database.authority",
    )
    require(
        authority["database"] == DATABASE_NAME
        and isinstance(authority["system_identifier"], str)
        and isinstance(authority["database_oid"], str),
        "recovery_schema_unsupported",
        "database authority is malformed",
    )
    for field in (
        "cluster_globals",
        "dump",
        "toc",
        "catalog_baseline",
        "data_baseline",
        "reference_index",
        "sequence_setval",
    ):
        row = _exact_keys(
            database[field],
            {"member", "bytes", "sha256"},
            f"database.{field}",
        )
        require_bound_file(
            row["member"],
            expected_bytes=row["bytes"],
            expected_sha=row["sha256"],
            where=f"database.{field}",
        )
    snapshot_contract = _exact_keys(
        database["snapshot"],
        {
            "transaction",
            "id",
            "pg_dump_snapshot",
            "held_until_pg_dump_complete",
        },
        "database.snapshot",
    )
    require(
        snapshot_contract["transaction"] == "REPEATABLE READ READ ONLY"
        and isinstance(snapshot_contract["id"], str)
        and snapshot_contract["id"]
        and snapshot_contract["pg_dump_snapshot"] == snapshot_contract["id"]
        and snapshot_contract["held_until_pg_dump_complete"] is True,
        "recovery_manifest_invalid",
        "database snapshot contract is malformed",
    )
    roles = _exact_keys(
        doc["roles"],
        {"bootstrap_role", "application_roles", "member"},
        "roles",
    )
    require(
        roles["bootstrap_role"] == BOOTSTRAP_ROLE
        and roles["application_roles"] == list(APPLICATION_ROLES)
        and roles["member"] == "database/cluster-roles.json",
        "recovery_manifest_invalid",
        "role contract is malformed",
    )
    airflow = _exact_keys(
        doc["airflow"],
        {
            "airflow_config_member",
            "connection_id",
            "fernet_key_present",
            "jwt_secret_present",
        },
        "airflow",
    )
    require(
        airflow["airflow_config_member"] == "config/state/airflow-home/airflow.cfg"
        and isinstance(airflow["connection_id"], str)
        and bool(airflow["connection_id"])
        and airflow["fernet_key_present"] is True
        and airflow["jwt_secret_present"] is True,
        "recovery_manifest_invalid",
        "Airflow recovery identity is malformed",
    )
    require_bound_file(
        airflow["airflow_config_member"],
        where="airflow.airflow_config_member",
    )
    maintenance = _exact_keys(
        doc["maintenance"],
        {"member", "bytes", "sha256", "receipt", "operatorctl"},
        "maintenance",
    )
    require(
        maintenance["member"] == "maintenance/receipt.json",
        "recovery_manifest_invalid",
        "maintenance receipt member path is not canonical",
    )
    require_bound_file(
        maintenance["member"],
        expected_bytes=maintenance["bytes"],
        expected_sha=maintenance["sha256"],
        where="maintenance.member",
    )
    maintenance_operatorctl = _exact_keys(
        maintenance["operatorctl"],
        {"member", "bytes", "sha256", "mode"},
        "maintenance.operatorctl",
    )
    require(
        maintenance_operatorctl["member"] == "maintenance/operatorctl.sh" and maintenance_operatorctl["mode"] == 0o755,
        "recovery_manifest_invalid",
        "maintenance operatorctl member path is not canonical",
    )
    require_bound_file(
        maintenance_operatorctl["member"],
        expected_bytes=maintenance_operatorctl["bytes"],
        expected_sha=maintenance_operatorctl["sha256"],
        where="maintenance.operatorctl",
    )
    maintenance_receipt = _exact_keys(
        maintenance["receipt"],
        {
            "schema_version",
            "status",
            "backup_id",
            "opened_at",
            "closed_at",
            "identity",
            "commands",
            "services",
            "database",
        },
        "maintenance.receipt",
    )
    maintenance_identity = _exact_keys(
        maintenance_receipt["identity"],
        {
            "install_id",
            "config_sha256",
            "operatorctl_path",
            "operatorctl_sha256",
            "release",
        },
        "maintenance.receipt.identity",
    )
    maintenance_release = _exact_keys(
        maintenance_identity["release"],
        {"version", "source_commit", "source_sha256"},
        "maintenance.receipt.identity.release",
    )
    require(
        maintenance_receipt["schema_version"] == MAINTENANCE_RECEIPT_SCHEMA
        and maintenance_receipt["status"] == "stopped"
        and maintenance_receipt["backup_id"] == doc["backup_id"]
        and isinstance(maintenance_receipt["opened_at"], str)
        and bool(maintenance_receipt["opened_at"])
        and isinstance(maintenance_receipt["closed_at"], str)
        and bool(maintenance_receipt["closed_at"])
        and isinstance(maintenance_identity["install_id"], str)
        and bool(maintenance_identity["install_id"])
        and isinstance(maintenance_identity["config_sha256"], str)
        and bool(HEX64.fullmatch(maintenance_identity["config_sha256"]))
        and isinstance(maintenance_identity["operatorctl_sha256"], str)
        and bool(HEX64.fullmatch(maintenance_identity["operatorctl_sha256"]))
        and maintenance_identity["operatorctl_sha256"] == maintenance_operatorctl["sha256"]
        and maintenance_release["version"] == release["version"]
        and maintenance_release["source_commit"] == release["source_commit"]
        and maintenance_release["source_sha256"] == release["source_sha256"],
        "recovery_manifest_invalid",
        "maintenance receipt identity is malformed or belongs to another release",
    )
    opened_at = utc_timestamp(
        maintenance_receipt["opened_at"],
        "maintenance.receipt.opened_at",
    )
    closed_at = utc_timestamp(
        maintenance_receipt["closed_at"],
        "maintenance.receipt.closed_at",
    )
    created_at = utc_timestamp(doc["created_at"], "created_at")
    require(
        opened_at <= closed_at <= created_at,
        "recovery_manifest_invalid",
        "maintenance receipt timestamps are out of order",
    )
    absolute_text(
        maintenance_identity["operatorctl_path"],
        "maintenance.receipt.identity.operatorctl_path",
    )
    platform_config_member = members_by_name.get("config/platform.json")
    require(
        isinstance(platform_config_member, dict)
        and platform_config_member["sha256"] == maintenance_identity["config_sha256"],
        "recovery_manifest_invalid",
        "maintenance receipt does not bind the archived platform configuration",
    )
    maintenance_commands = _exact_keys(
        maintenance_receipt["commands"],
        {"drain", "stop"},
        "maintenance.receipt.commands",
    )
    for name in ("drain", "stop"):
        command = _exact_keys(
            maintenance_commands[name],
            {"argv", "returncode", "stdout_sha256", "stderr_sha256"},
            f"maintenance.receipt.commands.{name}",
        )
        require(
            isinstance(command["argv"], list)
            and bool(command["argv"])
            and all(isinstance(value, str) and bool(value) for value in command["argv"])
            and command["returncode"] == 0
            and isinstance(command["stdout_sha256"], str)
            and bool(HEX64.fullmatch(command["stdout_sha256"]))
            and isinstance(command["stderr_sha256"], str)
            and bool(HEX64.fullmatch(command["stderr_sha256"])),
            "recovery_manifest_invalid",
            f"maintenance {name} command evidence is malformed",
        )
        require(
            Path(command["argv"][0]).name == "bash",
            "recovery_manifest_invalid",
            f"maintenance {name} command did not execute through bash",
        )
        expected_argv = [
            command["argv"][0],
            maintenance_identity["operatorctl_path"],
            name,
            "--config",
            platform_config_path,
        ]
        if name == "stop":
            expected_argv.append("--keep-database")
        require(
            command["argv"] == expected_argv,
            "recovery_manifest_invalid",
            f"maintenance {name} command does not bind the archived operatorctl and config",
        )
    services = _exact_keys(
        maintenance_receipt["services"],
        {"linux_before", "linux_after", "windows_before", "windows_after"},
        "maintenance.receipt.services",
    )
    for phase in ("linux_before", "linux_after"):
        states = services[phase]
        require(
            isinstance(states, list)
            and bool(states)
            and all(
                isinstance(row, dict)
                and set(row) == {"unit", "active_state"}
                and row["unit"] in MAINTENANCE_UNITS
                and row["active_state"] in {"inactive", "failed"}
                for row in states
            ),
            "recovery_manifest_invalid",
            f"maintenance receipt {phase} service states are malformed",
        )
        require(
            [row["unit"] for row in states] == list(MAINTENANCE_UNITS),
            "recovery_manifest_invalid",
            f"maintenance receipt {phase} does not cover the exact writer units",
        )
    require(
        services["linux_before"] == services["linux_after"],
        "recovery_manifest_invalid",
        "Linux maintenance service state changed during backup",
    )
    for phase in ("windows_before", "windows_after"):
        windows_state = _exact_keys(
            services[phase],
            {"task_name", "state", "evidence"},
            f"maintenance.receipt.services.{phase}",
        )
        require(
            isinstance(windows_state["task_name"], str)
            and bool(windows_state["task_name"])
            and windows_state["state"] == "Disabled"
            and isinstance(windows_state["evidence"], list)
            and bool(windows_state["evidence"])
            and all(
                isinstance(command, dict)
                and set(command) == {"argv", "returncode", "stdout_sha256", "stderr_sha256"}
                and isinstance(command["argv"], list)
                and bool(command["argv"])
                and all(isinstance(value, str) and bool(value) for value in command["argv"])
                and command["returncode"] == 0
                and isinstance(command["stdout_sha256"], str)
                and bool(HEX64.fullmatch(command["stdout_sha256"]))
                and isinstance(command["stderr_sha256"], str)
                and bool(HEX64.fullmatch(command["stderr_sha256"]))
                for command in windows_state["evidence"]
            ),
            "recovery_manifest_invalid",
            f"maintenance receipt {phase} Windows state is malformed",
        )
    require(
        services["windows_before"]["task_name"] == services["windows_after"]["task_name"],
        "recovery_manifest_invalid",
        "maintenance receipt names different Windows tasks",
    )
    maintenance_database = _exact_keys(
        maintenance_receipt["database"],
        {"authority", "before", "after"},
        "maintenance.receipt.database",
    )
    maintenance_authority = _exact_keys(
        maintenance_database["authority"],
        {
            "system_identifier",
            "catalog_version",
            "database",
            "database_oid",
            "socket_dir",
            "port",
        },
        "maintenance.receipt.database.authority",
    )
    require(
        maintenance_authority == authority,
        "recovery_manifest_invalid",
        "maintenance receipt belongs to another database authority",
    )

    def validate_maintenance_probe(probe: object, where: str) -> dict:
        value = _exact_keys(
            probe,
            {
                "admission",
                "settled",
                "unsettled_count",
                "active_application_sessions",
                "wal_lsn",
            },
            where,
        )
        admission = _exact_keys(
            value["admission"],
            {"state", "reason", "actor", "revision"},
            f"{where}.admission",
        )
        require(
            admission["state"] == "closed"
            and value["settled"] is True
            and value["unsettled_count"] == 0
            and value["active_application_sessions"] == []
            and isinstance(value["wal_lsn"], str)
            and bool(value["wal_lsn"]),
            "recovery_manifest_invalid",
            f"{where} does not prove a quiescent maintenance window",
        )
        return value

    for phase in ("before", "after"):
        validate_maintenance_probe(
            maintenance_database[phase],
            f"maintenance.receipt.database.{phase}",
        )
    require(
        _maintenance_state_identity(maintenance_database["before"])
        == _maintenance_state_identity(maintenance_database["after"]),
        "recovery_manifest_invalid",
        "database maintenance state changed during backup",
    )
    reconstructibles = _exact_keys(
        doc["reconstructibles"],
        {"release_root", "workspace_root", "external"},
        "reconstructibles",
    )
    _exact_keys(
        reconstructibles["release_root"],
        {"path", "recipe"},
        "reconstructibles.release_root",
    )
    _exact_keys(
        reconstructibles["workspace_root"],
        {"path", "plan_member", "recipe"},
        "reconstructibles.workspace_root",
    )
    require(
        reconstructibles["release_root"]["recipe"] == "offline-release-artifacts"
        and reconstructibles["workspace_root"]["recipe"] == "git-origin-head-clean"
        and reconstructibles["workspace_root"]["plan_member"] == "reconstruct/workspace-plan.json"
        and isinstance(reconstructibles["external"], list),
        "recovery_manifest_invalid",
        "reconstructible plan is malformed",
    )
    producer = _exact_keys(
        doc["producer"],
        {
            "host_id",
            "python",
            "postgres_client_major",
            "postgres_bin_dir",
            "postgres_library_dir",
        },
        "producer",
    )
    require(
        isinstance(producer["host_id"], str)
        and bool(producer["host_id"])
        and producer["postgres_client_major"] == "14"
        and isinstance(producer["postgres_bin_dir"], str)
        and isinstance(producer["postgres_library_dir"], str),
        "recovery_manifest_invalid",
        "producer identity is malformed",
    )
    consistency = _exact_keys(
        doc["consistency"],
        {
            "isolation",
            "snapshot_id",
            "snapshot_held_until_pg_dump_complete",
            "roles_snapshot_before_sha256",
            "roles_snapshot_after_sha256",
            "roles_live_before_sha256",
            "roles_live_after_sha256",
            "sequence_before_sha256",
            "sequence_after_sha256",
            "sequence_dump_setval_sha256",
            "sequence_extraction",
            "cluster_globals_sha256",
            "database_baseline_sha256",
            "database_live_after_dump_sha256",
            "database_live_after_inventory_sha256",
            "maintenance_before",
            "maintenance_after",
            "maintenance_lock",
            "exclusive_lock_key",
            "exclusive_lock_held",
            "source_inventory_before_sha256",
            "source_inventory_after_sha256",
            "source_inventory_equal",
            "source_inventory_roots",
            "source_inventory_files",
            "staged_source_members_sha256",
            "staged_source_members_equal",
            "staged_source_members_count",
            "canonical_root_modes",
            "archive_member_verification",
        },
        "consistency",
    )
    role_hash_values = [
        consistency["roles_snapshot_before_sha256"],
        consistency["roles_snapshot_after_sha256"],
        consistency["roles_live_before_sha256"],
        consistency["roles_live_after_sha256"],
    ]
    sequence_hash_values = [
        consistency["sequence_before_sha256"],
        consistency["sequence_after_sha256"],
        consistency["sequence_dump_setval_sha256"],
    ]
    database_hash_values = [
        consistency["database_baseline_sha256"],
        consistency["database_live_after_dump_sha256"],
        consistency["database_live_after_inventory_sha256"],
    ]
    require(
        consistency["isolation"] == "REPEATABLE READ READ ONLY"
        and consistency["snapshot_id"] == snapshot_contract["id"]
        and consistency["snapshot_held_until_pg_dump_complete"] is True
        and all(isinstance(value, str) and bool(HEX64.fullmatch(value)) for value in role_hash_values)
        and len(set(role_hash_values)) == 1
        and all(isinstance(value, str) and bool(HEX64.fullmatch(value)) for value in sequence_hash_values)
        and len(set(sequence_hash_values)) == 1
        and isinstance(consistency["sequence_extraction"], dict)
        and consistency["sequence_extraction"].get("method") == "pg_restore --data-only -L=<per-sequence-toc> -f=-"
        and consistency["sequence_extraction"].get("plaintext_dump_file_created") is False
        and consistency["sequence_extraction"].get("full_dump_materialized") is False
        and consistency["sequence_extraction"].get("temporary_toc_files_removed") is True
        and isinstance(
            consistency["sequence_extraction"].get("per_sequence"),
            list,
        )
        and all(
            isinstance(row, dict)
            and set(row) == {"sequence", "stdout_bytes"}
            and type(row["stdout_bytes"]) is int
            and 0 < row["stdout_bytes"] <= 1024 * 1024
            for row in consistency["sequence_extraction"]["per_sequence"]
        )
        and isinstance(consistency["cluster_globals_sha256"], str)
        and bool(HEX64.fullmatch(consistency["cluster_globals_sha256"]))
        and all(isinstance(value, str) and bool(HEX64.fullmatch(value)) for value in database_hash_values)
        and len(set(database_hash_values)) == 1
        and consistency["exclusive_lock_key"] == str(MAINTENANCE_LOCK_KEY)
        and consistency["exclusive_lock_held"] is True
        and consistency["source_inventory_before_sha256"] == consistency["source_inventory_after_sha256"]
        and consistency["source_inventory_equal"] is True
        and type(consistency["source_inventory_roots"]) is int
        and consistency["source_inventory_roots"] > 0
        and type(consistency["source_inventory_files"]) is int
        and consistency["source_inventory_files"] > 0
        and isinstance(consistency["staged_source_members_sha256"], str)
        and bool(HEX64.fullmatch(consistency["staged_source_members_sha256"]))
        and consistency["staged_source_members_equal"] is True
        and type(consistency["staged_source_members_count"]) is int
        and consistency["staged_source_members_count"] > 0
        and consistency["canonical_root_modes"] == {"state_root": 0o700, "release_root": 0o700}
        and isinstance(consistency["archive_member_verification"], dict)
        and set(consistency["archive_member_verification"]) == {"members", "sha256"}
        and type(consistency["archive_member_verification"]["members"]) is int
        and consistency["archive_member_verification"]["members"] > 0
        and isinstance(
            consistency["archive_member_verification"]["sha256"],
            str,
        )
        and bool(HEX64.fullmatch(consistency["archive_member_verification"]["sha256"])),
        "recovery_manifest_invalid",
        "snapshot/file/role consistency contract is malformed",
    )
    maintenance_lock = _exact_keys(
        consistency["maintenance_lock"],
        {"kind", "path", "owner_id", "held"},
        "consistency.maintenance_lock",
    )
    require(
        maintenance_lock["kind"] == "exclusive-filesystem-lock"
        and isinstance(maintenance_lock["path"], str)
        and Path(maintenance_lock["path"]).is_absolute()
        and isinstance(maintenance_lock["owner_id"], str)
        and bool(HEX64.fullmatch(maintenance_lock["owner_id"]))
        and maintenance_lock["held"] is True,
        "recovery_manifest_invalid",
        "maintenance lock evidence is malformed",
    )
    for phase in ("maintenance_before", "maintenance_after"):
        validate_maintenance_probe(
            consistency[phase],
            f"consistency.{phase}",
        )
    require(
        _maintenance_state_identity(maintenance_database["before"])
        == _maintenance_state_identity(consistency["maintenance_before"])
        and _maintenance_state_identity(maintenance_database["after"])
        == _maintenance_state_identity(consistency["maintenance_after"]),
        "recovery_manifest_invalid",
        "maintenance receipt and snapshot worker observed different database states",
    )
    references = doc["references"]
    require(
        isinstance(references, list),
        "recovery_manifest_invalid",
        "recovery reference index is malformed",
    )
    for index, row in enumerate(references):
        require(
            isinstance(row, dict)
            and set(row)
            == {
                "run_id",
                "stage",
                "checkpoint_uri",
                "tree_relative",
                "files",
            }
            and isinstance(row["run_id"], str)
            and isinstance(row["stage"], str)
            and row["checkpoint_uri"] == f"store://sealed/{row['run_id']}/{row['stage']}"
            and isinstance(row["tree_relative"], str)
            and isinstance(row["files"], list),
            "recovery_manifest_invalid",
            f"reference[{index}] is malformed",
        )
    if expected_manifest_sha is not None:
        require(
            isinstance(expected_manifest_sha, str) and bool(HEX64.fullmatch(expected_manifest_sha)),
            "recovery_manifest_hash_mismatch",
            "external manifest digest is malformed",
        )
    return doc


def read_external_manifest_digest(path: Path) -> str:
    sidecar = path.with_suffix(".sha256")
    require_real_file(
        sidecar,
        "external recovery manifest digest",
        "recovery_manifest_hash_mismatch",
    )
    try:
        fields = sidecar.read_text(encoding="ascii").strip().split()
    except (OSError, UnicodeError) as error:
        raise RecoveryError(
            "recovery_manifest_hash_mismatch",
            f"cannot read external manifest digest: {error}",
        ) from error
    require(
        bool(fields) and bool(HEX64.fullmatch(fields[0])),
        "recovery_manifest_hash_mismatch",
        "external manifest digest is malformed",
    )
    if len(fields) > 1:
        require(
            Path(fields[1]).name == path.name,
            "recovery_manifest_hash_mismatch",
            "external manifest digest names another file",
        )
    actual = sha256_file(path)
    require(
        actual == fields[0],
        "recovery_manifest_hash_mismatch",
        "recovery manifest differs from its external digest",
    )
    return actual


def load_recovery_manifest(path: Path) -> tuple[dict, bytes, str]:
    external = read_external_manifest_digest(path)
    payload, document = strict_json_file(path, "recovery manifest")
    manifest = validate_manifest(document, expected_manifest_sha=external)
    return manifest, payload, external


def prepare_extraction(
    manifest: dict,
    manifest_path: Path,
    key: Path,
    status_dir: Path,
) -> tuple[Path, tempfile.TemporaryDirectory]:
    require_real_file(key, "external recovery key", "recovery_key_unavailable")
    key_id = hashlib.sha256(key.read_bytes()).hexdigest()
    require(
        key_id == manifest["archive"]["key_id"],
        "recovery_key_unavailable",
        "recovery key does not match the manifest key_id",
    )
    archive = manifest_path.parent / manifest["archive"]["path"]
    require_real_file(
        archive,
        "encrypted recovery archive",
        "recovery_archive_hash_mismatch",
    )
    require(
        sha256_file(archive) == manifest["archive"]["sha256"],
        "recovery_archive_hash_mismatch",
        "encrypted recovery archive digest differs from the manifest",
    )
    temporary = tempfile.TemporaryDirectory(
        prefix="extract-",
        dir=status_dir,
    )
    destination = Path(temporary.name) / "archive"
    legacy = {
        "schema": backup.MANIFEST_SCHEMA,
        "files": {row["member"]: row["sha256"] for row in manifest["members"] if row["type"] == "file"},
        "encrypted": {
            "path": str(archive),
            "sha256": manifest["archive"]["sha256"],
            "cipher": "aes-256-cbc",
            "key": str(key),
        },
    }
    legacy_path = Path(temporary.name) / "legacy-manifest.json"
    atomic_write(legacy_path, canonical_json(legacy))
    try:
        backup.open_archive(archive, key, legacy_path, destination)
    except SystemExit as error:
        temporary.cleanup()
        raise RecoveryError(
            "recovery_member_hash_mismatch",
            f"encrypted archive validation failed: {error}",
        ) from error
    verify_member_set(destination, manifest["members"])
    return destination, temporary


def validate_target_plan(
    document: object,
    manifest: dict,
    manifest_sha: str,
) -> tuple[dict, bytes]:
    plan = _exact_keys(
        document,
        {
            "schema_version",
            "backup_manifest_sha256",
            "mode",
            "host_id",
            "postgres",
            "roots",
        },
        "target plan",
    )
    require(
        plan["schema_version"] == TARGET_SCHEMA,
        "recovery_schema_unsupported",
        "target plan schema is not supported",
    )
    require(
        plan["backup_manifest_sha256"] == manifest_sha,
        "recovery_target_plan_invalid",
        "target plan binds another recovery manifest",
    )
    require(
        plan["mode"] == "exact_path",
        "recovery_target_plan_invalid",
        "only exact_path recovery is supported",
    )
    require(
        isinstance(plan["host_id"], str)
        and bool(plan["host_id"])
        and plan["host_id"] != manifest["producer"]["host_id"],
        "recovery_target_same_authority",
        "target host identity equals the backup producer",
    )
    postgres = _exact_keys(
        plan["postgres"],
        {"bin_dir", "library_dir", "socket_dir", "data_dir", "port", "bootstrap_role"},
        "target.postgres",
    )
    for key in ("bin_dir", "library_dir", "socket_dir", "data_dir"):
        absolute_text(postgres[key], f"target.postgres.{key}")
    require(
        type(postgres["port"]) is int and 0 < postgres["port"] < 65536,
        "recovery_target_plan_invalid",
        "target PostgreSQL port is malformed",
    )
    require(
        postgres["bootstrap_role"] == BOOTSTRAP_ROLE,
        "recovery_target_plan_invalid",
        "target bootstrap role differs from the archive",
    )
    roots = plan["roots"]
    require(
        isinstance(roots, dict),
        "recovery_target_plan_invalid",
        "target roots must be an object",
    )
    expected_roots = {root["name"]: root["path"] for root in manifest["roots"] if "reconstructible_runtime" not in root}
    require(
        roots == expected_roots,
        "recovery_target_plan_invalid",
        "target roots do not exactly equal archived absolute roots",
    )
    payload = canonical_json(plan)
    return plan, payload


def path_exists(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def require_empty_target_directory(path: Path, where: str) -> None:
    if not path_exists(path):
        return
    require(
        path.is_dir() and not is_link(path),
        "recovery_target_not_empty",
        f"{where} is not a real directory: {path}",
    )
    require(
        not any(path.iterdir()),
        "recovery_target_not_empty",
        f"{where} is not empty: {path}",
    )


def validate_target_filesystem(manifest: dict, plan: dict) -> None:
    file_targets = [Path(row["path"]) for row in [*manifest["files"], *manifest["secrets"]]]
    for target in file_targets:
        require_no_links(target.parent, "secret target parent")
        require(
            not path_exists(target),
            "recovery_target_not_empty",
            f"secret target already exists: {target}",
        )
    for root in manifest["roots"]:
        if "reconstructible_runtime" in root:
            continue
        target = Path(plan["roots"][root["name"]])
        require(
            target.as_posix() == root["path"],
            "recovery_target_path_unsafe",
            "exact_path root changed after validation",
        )
        require_empty_target_directory(target, f"target root {root['name']}")


def target_empty_cluster(
    context: CommandContext,
    postgres: dict,
    manifest: dict,
) -> dict:
    socket_dir = Path(postgres["socket_dir"])
    data_dir = Path(postgres["data_dir"])
    require_real_dir(socket_dir, "target PostgreSQL socket directory", "recovery_target_plan_invalid")
    require_real_dir(data_dir, "target PostgreSQL data directory", "recovery_target_plan_invalid")
    version_file = data_dir / "PG_VERSION"
    require_real_file(
        version_file,
        "target PG_VERSION",
        "recovery_postgres_version_mismatch",
    )
    require(
        version_file.read_text(encoding="ascii").strip() == POSTGRES_MAJOR,
        "recovery_postgres_version_mismatch",
        "target data directory is not PostgreSQL 14",
    )
    require(
        postgres_major(Path(postgres["bin_dir"]), context) == POSTGRES_MAJOR,
        "recovery_postgres_version_mismatch",
        "target PostgreSQL tools are not major 14",
    )
    controls = pg_control(data_dir, context)
    require(
        controls["system_identifier"] != manifest["database"]["authority"]["system_identifier"],
        "recovery_target_same_authority",
        "target cluster is the backup source cluster",
    )
    status = psql_json(
        context,
        socket_dir=socket_dir,
        port=postgres["port"],
        user=postgres["bootstrap_role"],
        database="postgres",
        query=(
            "SELECT json_build_object("
            "'server_version_num', current_setting('server_version_num'),"
            "'databases', (SELECT COALESCE(json_agg(datname ORDER BY datname), "
            "'[]'::json) FROM pg_database WHERE datname NOT IN "
            "('postgres','template0','template1')),"
            "'roles', (SELECT COALESCE(json_agg(rolname ORDER BY rolname), "
            "'[]'::json) FROM pg_roles WHERE left(rolname, 3) <> 'pg_'))"
        ),
    )
    require(
        isinstance(status, dict)
        and isinstance(status.get("server_version_num"), str)
        and status["server_version_num"].startswith(POSTGRES_MAJOR),
        "recovery_postgres_version_mismatch",
        "target server is not PostgreSQL 14",
    )
    require(
        status.get("databases") == [],
        "recovery_target_not_empty",
        "target cluster already contains application databases",
    )
    require(
        status.get("roles") == [BOOTSTRAP_ROLE],
        "recovery_target_not_empty",
        "target cluster roles are not empty beyond the bootstrap role",
        {"roles": status.get("roles")},
    )
    return controls


def quote_ident(value: str) -> str:
    require(
        isinstance(value, str) and bool(ROLE_NAME.fullmatch(value)),
        "recovery_manifest_invalid",
        f"unsafe role identifier: {value!r}",
    )
    return '"' + value.replace('"', '""') + '"'


def quote_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def topological_memberships(
    roles: list[dict],
    memberships: list[dict],
) -> list[dict]:
    superusers = {row["name"] for row in roles if isinstance(row, dict) and row.get("superuser") is True}
    createroles = {row["name"] for row in roles if isinstance(row, dict) and row.get("createrole") is True}
    role_rows = {row["name"]: row for row in roles if isinstance(row, dict) and isinstance(row.get("name"), str)}
    pending = list(memberships)
    ordered = []
    while pending:
        ready = [
            row
            for row in pending
            if row["grantor"] == row["role"]
            or row["grantor"] in superusers
            or (row["grantor"] in createroles and role_rows.get(row["role"], {}).get("superuser") is not True)
            or any(
                prior["role"] == row["role"] and prior["member"] == row["grantor"] and prior["admin_option"] is True
                for prior in ordered
            )
        ]
        require(
            bool(ready),
            "recovery_manifest_invalid",
            "membership grantor order has no authorized root",
            {
                "pending": [
                    {
                        "role": row["role"],
                        "member": row["member"],
                        "grantor": row["grantor"],
                    }
                    for row in pending
                ]
            },
        )
        ready.sort(
            key=lambda row: (
                row["role"],
                row["member"],
                row["grantor"],
            )
        )
        ordered.extend(ready)
        ready_ids = {id(row) for row in ready}
        pending = [row for row in pending if id(row) not in ready_ids]
    return ordered


def role_sql(plan_document: dict) -> str:
    roles = plan_document.get("roles")
    memberships = plan_document.get("memberships")
    require(
        isinstance(roles, list) and isinstance(memberships, list),
        "recovery_manifest_invalid",
        "normalized role plan is malformed",
    )
    by_name = {row["name"]: row for row in roles}
    require(
        set(by_name) == {BOOTSTRAP_ROLE, *APPLICATION_ROLES},
        "recovery_manifest_invalid",
        "normalized role plan has an incomplete role set",
    )
    membership_roles = {
        value
        for row in memberships
        if isinstance(row, dict)
        for value in (row.get("role"), row.get("member"), row.get("grantor"))
        if isinstance(value, str) and value not in by_name
    }
    require(
        all(bool(re.fullmatch(r"pg_[A-Za-z0-9_]+", value)) for value in membership_roles),
        "recovery_manifest_invalid",
        "membership plan references an unsupported non-system role",
        {"roles": sorted(membership_roles)},
    )
    known_roles = set(by_name) | membership_roles
    membership_rows = []
    for row in memberships:
        require(
            isinstance(row, dict)
            and set(row) == {"role", "member", "grantor", "admin_option"}
            and row["role"] in known_roles
            and row["member"] in known_roles
            and row["grantor"] in known_roles
            and isinstance(row["admin_option"], bool),
            "recovery_manifest_invalid",
            "role membership is malformed",
        )
        membership_rows.append(row)
    ordered_memberships = topological_memberships(roles, membership_rows)
    lines = ["SET client_min_messages = warning;", "BEGIN;"]
    for name in sorted(by_name):
        if name != BOOTSTRAP_ROLE:
            lines.append(f"CREATE ROLE {quote_ident(name)};")
    for name in sorted(by_name):
        row = by_name[name]
        attributes = []
        for field, keyword in (
            ("superuser", "SUPERUSER"),
            ("inherit", "INHERIT"),
            ("createrole", "CREATEROLE"),
            ("createdb", "CREATEDB"),
            ("canlogin", "LOGIN"),
            ("replication", "REPLICATION"),
            ("bypassrls", "BYPASSRLS"),
        ):
            require(
                isinstance(row.get(field), bool),
                "recovery_manifest_invalid",
                f"role attribute is malformed: {name}.{field}",
            )
            attributes.append(keyword if row[field] else f"NO{keyword}")
        connection_limit = row.get("connection_limit")
        require(
            type(connection_limit) is int and connection_limit >= -1,
            "recovery_manifest_invalid",
            f"role connection limit is malformed: {name}",
        )
        attributes.append(f"CONNECTION LIMIT {connection_limit}")
        valid_until = row.get("valid_until")
        require(
            valid_until is None or isinstance(valid_until, str),
            "recovery_manifest_invalid",
            f"role valid-until value is malformed: {name}",
        )
        attributes.append(f"VALID UNTIL {quote_literal(valid_until or 'infinity')}")
        verifier = row.get("password_verifier")
        password = "PASSWORD NULL" if verifier is None else f"PASSWORD {quote_literal(verifier)}"
        lines.append(f"ALTER ROLE {quote_ident(name)} WITH {' '.join(attributes)} {password};")
        settings = row.get("settings")
        require(
            isinstance(settings, list) and all(isinstance(item, str) and "=" in item for item in settings),
            "recovery_manifest_invalid",
            f"role settings are malformed: {name}",
        )
        lines.append(f"ALTER ROLE {quote_ident(name)} RESET ALL;")
        for setting in settings:
            key, value = setting.split("=", 1)
            require(
                bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", key)),
                "recovery_manifest_invalid",
                f"role setting key is unsafe: {key!r}",
            )
            lines.append(f"ALTER ROLE {quote_ident(name)} SET {quote_ident(key)} TO {quote_literal(value)};")
    for row in ordered_memberships:
        admin = " WITH ADMIN OPTION" if row["admin_option"] else ""
        lines.append(f"SET ROLE {quote_ident(row['grantor'])};")
        lines.append(f"GRANT {quote_ident(row['role'])} TO {quote_ident(row['member'])}{admin};")
        lines.append("RESET ROLE;")
    lines.extend(["COMMIT;", ""])
    return "\n".join(lines)


def verify_canonical_globals(extracted: Path) -> str:
    role_plan = _load_extracted_json(
        Path(extracted),
        "database/cluster-roles.json",
        "normalized role plan",
    )
    expected = role_sql(role_plan).encode("utf-8")
    globals_path = Path(extracted) / "database" / "cluster-globals.sql"
    require_real_file(
        globals_path,
        "canonical cluster globals",
        "recovery_globals_mismatch",
    )
    require(
        globals_path.read_bytes() == expected,
        "recovery_globals_mismatch",
        "cluster globals differ from the normalized role plan authority",
    )
    return hashlib.sha256(expected).hexdigest()


def verify_maintenance_archive_consistency(manifest: dict, extracted: Path) -> None:
    maintenance = manifest["maintenance"]
    receipt_payload, receipt = strict_json_file(
        extracted / maintenance["member"],
        "archived maintenance receipt",
    )
    embedded = maintenance["receipt"]
    require(
        receipt_payload == canonical_json(embedded) and receipt == embedded,
        "recovery_manifest_invalid",
        "embedded maintenance receipt differs from the archived receipt",
    )
    operatorctl = maintenance["operatorctl"]
    operatorctl_path = extracted / operatorctl["member"]
    require_real_file(
        operatorctl_path,
        "archived operatorctl",
        "recovery_manifest_invalid",
    )
    require(
        operatorctl_path.stat().st_size == operatorctl["bytes"]
        and sha256_file(operatorctl_path) == operatorctl["sha256"] == receipt["identity"]["operatorctl_sha256"]
        and stat.S_IMODE(operatorctl_path.stat().st_mode) == operatorctl["mode"] == 0o755,
        "recovery_manifest_invalid",
        "archived operatorctl differs from the maintenance receipt",
    )
    platform = _load_extracted_json(
        extracted,
        "config/platform.json",
        "archived platform configuration",
    )
    require(
        isinstance(platform, dict)
        and isinstance(platform.get("windows"), dict)
        and isinstance(platform["windows"].get("instance"), dict),
        "recovery_manifest_invalid",
        "archived platform Windows instance is malformed",
    )
    state_roots = [
        root["path"]
        for root in manifest["roots"]
        if isinstance(root, dict) and root.get("name") == "state_root"
    ]
    expected_lock = (
        Path(state_roots[0]) / MAINTENANCE_LOCK_DIRNAME / MAINTENANCE_LOCK_FILENAME
        if len(state_roots) == 1 and isinstance(state_roots[0], str)
        else None
    )
    archived_lock = _load_extracted_json(
        extracted,
        f"config/state/{MAINTENANCE_LOCK_DIRNAME}/{MAINTENANCE_LOCK_FILENAME}",
        "archived maintenance lock owner",
    )
    require(
        expected_lock is not None
        and manifest["consistency"]["maintenance_lock"]["path"] == str(expected_lock)
        and archived_lock.get("owner_id")
        == manifest["consistency"]["maintenance_lock"]["owner_id"],
        "recovery_manifest_invalid",
        "maintenance lock evidence does not bind the archived state-root lock",
    )
    task_name = platform["windows"]["instance"].get("task_name")
    require(
        isinstance(task_name, str)
        and bool(task_name)
        and receipt["services"]["windows_before"]["task_name"] == task_name
        and receipt["services"]["windows_after"]["task_name"] == task_name,
        "recovery_manifest_invalid",
        "maintenance receipt does not bind the archived Windows task",
    )
    install_record = _load_extracted_json(
        extracted,
        "config/state/install-record.json",
        "archived install record",
    )
    require(
        isinstance(install_record, dict) and install_record.get("install_id") == receipt["identity"]["install_id"],
        "recovery_manifest_invalid",
        "maintenance receipt does not bind the archived install identity",
    )


def verify_archive_consistency(manifest: dict, extracted: Path) -> None:
    consistency = manifest["consistency"]
    verify_maintenance_archive_consistency(manifest, extracted)
    inventory_before = _load_extracted_json(
        extracted,
        "inventory/source-before.json",
        "source inventory before snapshot",
    )
    inventory_after = _load_extracted_json(
        extracted,
        "inventory/source-after.json",
        "source inventory after dump",
    )
    staged_source = _load_extracted_json(
        extracted,
        "inventory/staged-source.json",
        "staged source inventory",
    )
    verify_manifest_source_binding(
        extracted,
        manifest["members"],
        staged_source,
        normalize_directory_modes=True,
    )
    archive_member_rows = [
        {
            "member": row["member"],
            "type": row["type"],
            "bytes": row["bytes"],
            "sha256": row["sha256"],
            "mode": row["mode"],
        }
        for row in manifest["members"]
    ]
    require(
        len(archive_member_rows) == consistency["archive_member_verification"]["members"]
        and _digest_document(archive_member_rows) == consistency["archive_member_verification"]["sha256"],
        "recovery_archive_member_mismatch",
        "archive member verification evidence differs from the manifest",
    )
    require(
        inventory_before == inventory_after,
        "recovery_source_changed",
        "archived source inventories differ",
    )
    require(
        hashlib.sha256(canonical_json(inventory_before)).hexdigest()
        == consistency["source_inventory_before_sha256"]
        == consistency["source_inventory_after_sha256"]
        and hashlib.sha256(canonical_json(staged_source)).hexdigest() == consistency["staged_source_members_sha256"]
        and len(staged_source) == consistency["staged_source_members_count"],
        "recovery_staged_source_mismatch",
        "archived file consistency evidence differs from the manifest",
    )
    require(
        {
            "state_root": stat.S_IMODE((extracted / "config" / "state").stat().st_mode),
            "release_root": stat.S_IMODE((extracted / "release").stat().st_mode),
        }
        == consistency["canonical_root_modes"],
        "recovery_staged_source_mismatch",
        "archived canonical root modes differ from the manifest",
    )
    globals_sha256 = verify_canonical_globals(extracted)
    require(
        globals_sha256 == consistency["cluster_globals_sha256"],
        "recovery_globals_mismatch",
        "canonical cluster globals digest differs from the consistency evidence",
    )

    role_plan = _load_extracted_json(
        extracted,
        "database/cluster-roles.json",
        "normalized role plan",
    )
    catalog = _load_extracted_json(
        extracted,
        "database/catalog-baseline.json",
        "catalog baseline",
    )
    role_facts = {
        "roles": role_plan["roles"],
        "memberships": role_plan["memberships"],
        "database_settings": catalog["settings"],
        "non_system_roles": sorted(row["name"] for row in role_plan["roles"]),
    }
    role_sha256 = _digest_document(role_facts)
    require(
        role_sha256
        == consistency["roles_snapshot_before_sha256"]
        == consistency["roles_snapshot_after_sha256"]
        == consistency["roles_live_before_sha256"]
        == consistency["roles_live_after_sha256"],
        "recovery_roles_changed_during_backup",
        "archived role facts differ from the recorded consistency digests",
    )

    data = _load_extracted_json(
        extracted,
        "database/data-baseline.json",
        "data baseline",
    )
    dump_positions = _load_extracted_json(
        extracted,
        "database/sequence-setval.json",
        "dump sequence setval evidence",
    )
    verify_sequence_dump_positions(data["sequences"], dump_positions)
    sequence_positions_value = sequence_positions(data["sequences"])
    require(
        _digest_document(sequence_positions_value)
        == consistency["sequence_before_sha256"]
        == consistency["sequence_after_sha256"]
        == consistency["sequence_dump_setval_sha256"],
        "recovery_sequence_dump_mismatch",
        "archived sequence facts differ from the consistency digests",
    )


def restore_roles(
    *,
    context: CommandContext,
    postgres: dict,
    extracted: Path,
    status_dir: Path,
) -> None:
    roles_path = extracted / "database" / "cluster-roles.json"
    payload, document = strict_json_file(roles_path, "normalized role plan")
    del payload
    sql = role_sql(document)
    sql_path = status_dir / "restore-roles.sql"
    atomic_write(sql_path, sql.encode("utf-8"))
    psql(
        context,
        socket_dir=Path(postgres["socket_dir"]),
        port=postgres["port"],
        user=postgres["bootstrap_role"],
        database="postgres",
        file=sql_path,
    )
    sql_path.unlink(missing_ok=True)


def restore_database(
    *,
    context: CommandContext,
    postgres: dict,
    extracted: Path,
) -> None:
    dump = extracted / "database" / "airflow_meta.dump"
    require_real_file(dump, "database dump", "recovery_member_hash_mismatch")
    context.run(
        [
            context.bin_dir / "pg_restore",
            "-h",
            postgres["socket_dir"],
            "-p",
            str(postgres["port"]),
            "-U",
            postgres["bootstrap_role"],
            "--create",
            "--exit-on-error",
            "-d",
            "postgres",
            dump,
        ],
        timeout=3600,
    )


def apply_database_acl(
    *,
    context: CommandContext,
    postgres: dict,
    extracted: Path,
) -> None:
    """Reapply the database-level ACL that pg_dump does not consistently emit on PG14."""

    baseline = _load_extracted_json(
        extracted,
        "database/catalog-baseline.json",
        "catalog baseline",
    )
    database = baseline.get("database")
    require(
        isinstance(database, dict) and isinstance(database.get("acl"), list),
        "recovery_database_readback_failed",
        "database ACL baseline is malformed",
    )
    rows = database["acl"]
    if database.get("acl_is_null") is True:
        return
    require(
        database.get("acl_is_null") is False,
        "recovery_database_readback_failed",
        "database ACL null state is malformed",
    )
    grantees = {row.get("grantee") for row in rows if isinstance(row, dict) and row.get("grantee") != "PUBLIC"}
    require(
        grantees <= {BOOTSTRAP_ROLE, *APPLICATION_ROLES},
        "recovery_database_readback_failed",
        "database ACL grants a role outside the recovery role plan",
    )
    require(
        all(
            isinstance(row, dict)
            and set(row) == {"grantor", "grantee", "privilege", "grantable"}
            and row["grantor"] == BOOTSTRAP_ROLE
            and isinstance(row["grantable"], bool)
            and row["privilege"] in {"CONNECT", "CREATE", "TEMPORARY"}
            and (
                row["grantee"] == "PUBLIC"
                or (isinstance(row["grantee"], str) and bool(ROLE_NAME.fullmatch(row["grantee"])))
            )
            for row in rows
        ),
        "recovery_database_readback_failed",
        "database ACL baseline contains unsupported values",
    )
    statements = [f"REVOKE ALL ON DATABASE {quote_ident(DATABASE_NAME)} FROM PUBLIC;"]
    for grantee in sorted(grantees):
        statements.append(f"REVOKE ALL ON DATABASE {quote_ident(DATABASE_NAME)} FROM {quote_ident(grantee)};")
    for row in rows:
        grantee = "PUBLIC" if row["grantee"] == "PUBLIC" else quote_ident(row["grantee"])
        grantable = " WITH GRANT OPTION" if row["grantable"] else ""
        statements.append(f"GRANT {row['privilege']} ON DATABASE {quote_ident(DATABASE_NAME)} TO {grantee}{grantable};")
    psql(
        context,
        socket_dir=Path(postgres["socket_dir"]),
        port=postgres["port"],
        user=postgres["bootstrap_role"],
        database=DATABASE_NAME,
        query="\n".join(statements),
    )


def _load_extracted_json(extracted: Path, member: str, what: str) -> object:
    _, document = strict_json_file(extracted / member, what)
    return document


def readback_roles(
    *,
    context: CommandContext,
    postgres: dict,
    extracted: Path,
) -> None:
    expected = _load_extracted_json(
        extracted,
        "database/cluster-roles.json",
        "normalized role plan",
    )
    socket_dir = Path(postgres["socket_dir"])
    all_roles = psql_json(
        context,
        socket_dir=socket_dir,
        port=postgres["port"],
        user=postgres["bootstrap_role"],
        database=DATABASE_NAME,
        query=ALL_NON_SYSTEM_ROLES_QUERY,
    )
    expected_role_names = {row["name"] for row in expected["roles"]}
    all_role_names = set(all_roles) if isinstance(all_roles, list) else set()
    require(
        isinstance(all_roles, list) and all_role_names == expected_role_names,
        "recovery_unsupported_role_set",
        "target cluster contains an unsupported non-system role set",
        {
            "missing": sorted(expected_role_names - all_role_names),
            "unexpected": sorted(all_role_names - expected_role_names),
        },
    )
    actual = {
        "bootstrap_role": BOOTSTRAP_ROLE,
        "application_roles": list(APPLICATION_ROLES),
        "roles": psql_json(
            context,
            socket_dir=socket_dir,
            port=postgres["port"],
            user=postgres["bootstrap_role"],
            database=DATABASE_NAME,
            query=ROLE_QUERY,
        ),
        "memberships": psql_json(
            context,
            socket_dir=socket_dir,
            port=postgres["port"],
            user=postgres["bootstrap_role"],
            database=DATABASE_NAME,
            query=MEMBERSHIP_QUERY,
        ),
    }
    require(
        actual == expected,
        "recovery_database_readback_failed",
        "roles, attributes, verifier presence, settings or memberships differ",
    )


def readback_database(
    *,
    context: CommandContext,
    postgres: dict,
    extracted: Path,
) -> dict:
    socket_dir = Path(postgres["socket_dir"])
    expected_catalog = _load_extracted_json(
        extracted,
        "database/catalog-baseline.json",
        "catalog baseline",
    )
    expected_data = _load_extracted_json(
        extracted,
        "database/data-baseline.json",
        "data baseline",
    )
    legacy_catalog = psql_json(
        context,
        socket_dir=socket_dir,
        port=postgres["port"],
        user=postgres["bootstrap_role"],
        database=DATABASE_NAME,
        query=CATALOG_QUERY,
    )
    full_catalog = psql_json(
        context,
        socket_dir=socket_dir,
        port=postgres["port"],
        user=postgres["bootstrap_role"],
        database=DATABASE_NAME,
        query=FULL_CATALOG_QUERY,
    )
    actual_catalog = {
        "database": legacy_catalog["database"],
        "settings": legacy_catalog["settings"],
        "public_create": legacy_catalog["public_create"],
        "platform_service_airflow_usage": legacy_catalog["platform_service_airflow_usage"],
        "platform_service_airflow_create": legacy_catalog["platform_service_airflow_create"],
        "catalog": full_catalog,
    }
    require(
        actual_catalog == expected_catalog,
        "recovery_database_readback_failed",
        "database owner, ACL, schema owner/ACL, table ACL or role settings differ",
        {
            "differences": [
                {
                    "field": key,
                    "expected": expected_catalog.get(key),
                    "actual": actual_catalog.get(key),
                }
                for key in sorted(set(expected_catalog) | set(actual_catalog))
                if expected_catalog.get(key) != actual_catalog.get(key)
            ]
        },
    )
    table_rows = psql_json(
        context,
        socket_dir=socket_dir,
        port=postgres["port"],
        user=postgres["bootstrap_role"],
        database=DATABASE_NAME,
        query=FULL_TABLE_LIST_QUERY,
    )
    actual_table_names = {f"{row['schema']}.{row['table']}" for row in table_rows if isinstance(row, dict)}
    require(
        actual_table_names == set(expected_data["tables"]),
        "recovery_database_readback_failed",
        "full table set differs from the archive baseline",
        {
            "missing": sorted(set(expected_data["tables"]) - actual_table_names),
            "unexpected": sorted(actual_table_names - set(expected_data["tables"])),
        },
    )
    actual_tables = {}
    for name in sorted(expected_data["tables"]):
        schema, table = name.split(".", 1)
        actual_tables[name] = psql_json(
            context,
            socket_dir=socket_dir,
            port=postgres["port"],
            user=postgres["bootstrap_role"],
            database=DATABASE_NAME,
            query=_table_fingerprint_query(
                schema,
                table,
                allow_discovered=True,
            ),
        )
    actual_sequences = capture_sequence_baseline(
        context=context,
        socket_dir=socket_dir,
        port=postgres["port"],
        database=DATABASE_NAME,
    )
    actual_alembic = psql_json(
        context,
        socket_dir=socket_dir,
        port=postgres["port"],
        user=postgres["bootstrap_role"],
        database=DATABASE_NAME,
        query=("SELECT json_agg(version_num ORDER BY version_num) FROM airflow.alembic_version"),
    )
    actual_data = {
        "tables": actual_tables,
        "sequences": actual_sequences,
        "airflow_alembic_heads": actual_alembic,
    }
    require(
        actual_data == expected_data,
        "recovery_database_readback_failed",
        "core table data, sequences or Airflow alembic head differ",
        {
            "tables_differ": actual_tables != expected_data.get("tables"),
            "sequences_differ": actual_sequences != expected_data.get("sequences"),
            "alembic_differ": actual_alembic != expected_data.get("airflow_alembic_heads"),
        },
    )
    return {"catalog": actual_catalog, "data": actual_data}


def decrypt_airflow_connection(
    *,
    context: CommandContext,
    postgres: dict,
    airflow_cfg: Path,
    connection_id: str,
) -> None:
    require(
        isinstance(connection_id, str) and bool(re.fullmatch(r"[A-Za-z0-9_.-]+", connection_id)),
        "recovery_manifest_invalid",
        "Airflow connection id is malformed",
    )
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(airflow_cfg, encoding="utf-8")
    raw_keys = parser.get("core", "fernet_key", fallback="")
    keys = [item.strip() for item in raw_keys.split(",") if item.strip()]
    require(
        bool(keys),
        "recovery_database_readback_failed",
        "restored airflow.cfg has no Fernet key",
    )
    row = psql_json(
        context,
        socket_dir=Path(postgres["socket_dir"]),
        port=postgres["port"],
        user=postgres["bootstrap_role"],
        database=DATABASE_NAME,
        query=(
            "SELECT json_build_object("
            "'count', count(*),"
            "'password', min(password),"
            "'extra', min(extra)) "
            "FROM airflow.connection "
            f"WHERE conn_id = {quote_literal(connection_id)}"
        ),
    )
    require(
        isinstance(row, dict) and row.get("count") == 1,
        "recovery_database_readback_failed",
        "the required Airflow Connection row is absent or duplicated",
    )
    try:
        from cryptography.fernet import Fernet, InvalidToken
    except ImportError as error:
        raise RecoveryError(
            "recovery_release_missing",
            "cryptography is required for Airflow Connection decryption verification",
        ) from error
    decrypted = False
    for token_value in (row.get("password"), row.get("extra")):
        if not isinstance(token_value, str) or not token_value:
            continue
        if token_value.startswith(("{", "[", "postgresql://", "postgres://")):
            continue
        for key in keys:
            try:
                Fernet(key.encode("ascii")).decrypt(token_value.encode("utf-8"))
                decrypted = True
                break
            except (InvalidToken, ValueError, UnicodeError):
                continue
        if decrypted:
            break
    require(
        decrypted,
        "recovery_database_readback_failed",
        "restored airflow.cfg cannot decrypt the Airflow Connection row",
    )


def copy_extracted_directory(
    source: Path,
    target: Path,
    members: list[dict],
    archive_prefix: str,
) -> None:
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path_exists(target):
        require(
            target.is_dir() and not is_link(target) and not any(target.iterdir()),
            "recovery_target_not_empty",
            f"target root became nonempty: {target}",
        )
        target.rmdir()
    partial = target.with_name(f".{target.name}.{os.getpid()}.partial")
    require(
        not path_exists(partial),
        "recovery_target_not_empty",
        f"partial target path already exists: {partial}",
    )
    try:
        copy_tree_strict(source, partial)
        modes = {
            row["member"]: row["mode"]
            for row in members
            if row["member"] == archive_prefix or row["member"].startswith(archive_prefix.rstrip("/") + "/")
        }
        for source_path in sorted(partial.rglob("*")):
            relative = source_path.relative_to(partial).as_posix()
            member = f"{archive_prefix.rstrip('/')}/{relative}"
            mode = modes.get(member)
            if mode is not None:
                os.chmod(source_path, mode)
        os.chmod(
            partial,
            next(
                (row["mode"] for row in members if row["member"] == archive_prefix and row["type"] == "directory"),
                0o700,
            ),
        )
        os.replace(partial, target)
    finally:
        if path_exists(partial):
            with contextlib.suppress(OSError, SystemExit):
                backup._remove_staging_tree(partial)  # same private-tree cleanup primitive


def restore_directories(
    manifest: dict,
    plan: dict,
    extracted: Path,
) -> None:
    roots = sorted(
        [root for root in manifest["roots"] if "reconstructible_runtime" not in root],
        key=lambda row: (len(Path(row["path"]).parts), row["name"]),
    )
    for root in roots:
        source = extracted / root["archive"]
        require_real_dir(
            source,
            f"archived root {root['name']}",
            "recovery_member_hash_mismatch",
        )
        target = Path(plan["roots"][root["name"]])
        copy_extracted_directory(
            source,
            target,
            manifest["members"],
            root["archive"],
        )


def restore_exact_files(
    manifest: dict,
    extracted: Path,
) -> None:
    for entry in [*manifest["files"], *manifest["secrets"]]:
        source = extracted / entry["member"]
        require_real_file(
            source,
            f"archived file {entry['name']}",
            "recovery_member_hash_mismatch",
        )
        target = Path(entry["path"])
        require(
            not path_exists(target),
            "recovery_target_not_empty",
            f"exact file target already exists: {target}",
        )
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        partial = target.with_name(f".{target.name}.{os.getpid()}.partial")
        require(
            not path_exists(partial),
            "recovery_target_not_empty",
            f"secret partial path already exists: {partial}",
        )
        try:
            shutil.copy2(source, partial)
            os.chmod(partial, entry["mode"])
            os.replace(partial, target)
        finally:
            partial.unlink(missing_ok=True)


def verify_restored_files(manifest: dict, plan: dict) -> None:
    members_by_name = {row["member"]: row for row in manifest["members"]}
    exact_file_targets = {Path(row["path"]) for row in [*manifest["files"], *manifest["secrets"]]}
    directory_targets = {Path(plan["roots"][root["name"]]) for root in manifest["roots"]}
    for root in manifest["roots"]:
        target = Path(plan["roots"][root["name"]])
        require_real_dir(target, f"restored root {root['name']}", "recovery_member_hash_mismatch")
        expected = [
            row
            for row in manifest["members"]
            if row["member"] == root["archive"] or row["member"].startswith(root["archive"].rstrip("/") + "/")
        ]
        actual = []
        for path in sorted(target.rglob("*")):
            if any(
                len(other.parts) > len(target.parts) and (path == other or path.is_relative_to(other))
                for other in directory_targets
            ):
                continue
            if any(path == file_target or file_target.is_relative_to(path) for file_target in exact_file_targets):
                continue
            require(
                not is_link(path),
                "recovery_member_hash_mismatch",
                f"restored root carries a link: {path}",
            )
            relative = path.relative_to(target).as_posix()
            member = f"{root['archive'].rstrip('/')}/{relative}"
            manifest_row = next(
                (row for row in expected if row["member"] == member),
                None,
            )
            require(
                manifest_row is not None,
                "recovery_member_hash_mismatch",
                f"restored root carries an unregistered member: {member}",
            )
            require(
                stat.S_IMODE(path.stat().st_mode) == manifest_row["mode"],
                "recovery_member_hash_mismatch",
                f"restored member mode differs: {member}",
            )
            if path.is_file():
                require(
                    path.stat().st_size == manifest_row["bytes"] and sha256_file(path) == manifest_row["sha256"],
                    "recovery_member_hash_mismatch",
                    f"restored member bytes differ: {member}",
                )
            else:
                require(
                    path.is_dir() and manifest_row["type"] == "directory",
                    "recovery_member_hash_mismatch",
                    f"restored member type differs: {member}",
                )
            actual.append(member)
        require(
            sorted(actual) == sorted(row["member"] for row in expected if row["member"] != root["archive"]),
            "recovery_member_hash_mismatch",
            f"restored root inventory differs: {root['name']}",
        )
    for entry in [*manifest["files"], *manifest["secrets"]]:
        target = Path(entry["path"])
        require_real_file(
            target,
            f"restored exact file {entry['name']}",
            "recovery_member_hash_mismatch",
        )
        require(
            stat.S_IMODE(target.stat().st_mode) == entry["mode"],
            "recovery_member_hash_mismatch",
            f"restored exact file mode differs: {entry['name']}",
        )
        member = members_by_name.get(entry["member"])
        require(
            isinstance(member, dict)
            and member["type"] == "file"
            and target.stat().st_size == member["bytes"]
            and sha256_file(target) == member["sha256"],
            "recovery_member_hash_mismatch",
            f"restored exact file bytes differ from its manifest member: {entry['name']}",
        )


def write_status(
    status_dir: Path,
    document: dict,
) -> None:
    atomic_write(status_dir / "receipt.json", canonical_json(document))


def restore_command(
    *,
    manifest_path: Path,
    key: Path,
    target_plan_path: Path,
    status_dir: Path,
) -> dict:
    manifest_path = Path(manifest_path)
    key = Path(key)
    target_plan_path = Path(target_plan_path)
    status_dir = Path(status_dir)
    require(
        status_dir.is_absolute(),
        "recovery_target_path_unsafe",
        "--status-dir must be absolute",
    )
    require_no_links(status_dir, "status directory")
    if status_dir.exists():
        require(
            status_dir.is_dir() and not is_link(status_dir) and not any(status_dir.iterdir()),
            "recovery_target_not_empty",
            f"status directory is not empty: {status_dir}",
        )
    else:
        status_dir.mkdir(parents=True, mode=0o700)
    os.chmod(status_dir, 0o700)

    receipt = {
        "schema_version": RECEIPT_SCHEMA,
        "operation": "restore",
        "status": "running",
        "started_at": utc_now(),
        "manifest": str(manifest_path),
        "target_plan": str(target_plan_path),
    }
    write_status(status_dir, receipt)
    mutated = False
    commands: list[dict] = []
    temporary: tempfile.TemporaryDirectory | None = None
    try:
        manifest, manifest_bytes, manifest_sha = load_recovery_manifest(manifest_path)
        del manifest_bytes
        archive_path = manifest_path.parent / manifest["archive"]["path"]
        require_real_file(
            archive_path,
            "encrypted recovery archive",
            "recovery_archive_hash_mismatch",
        )
        require(
            sha256_file(archive_path) == manifest["archive"]["sha256"],
            "recovery_archive_hash_mismatch",
            "encrypted recovery archive digest differs from the manifest",
        )
        require_real_file(
            key,
            "external recovery key",
            "recovery_key_unavailable",
        )
        require(
            hashlib.sha256(key.read_bytes()).hexdigest() == manifest["archive"]["key_id"],
            "recovery_key_unavailable",
            "recovery key does not match the manifest key_id",
        )
        verify_tar_rows_against_manifest(
            read_sealed_tar_member_rows(archive_path, key),
            manifest["members"],
        )
        _, target_document = strict_json_file(target_plan_path, "target plan")
        plan, canonical_plan = validate_target_plan(
            target_document,
            manifest,
            manifest_sha,
        )
        plan_sha = hashlib.sha256(canonical_plan).hexdigest()
        extracted, temporary = prepare_extraction(
            manifest,
            manifest_path,
            key,
            status_dir,
        )
        verify_archive_consistency(manifest, extracted)
        postgres = plan["postgres"]
        pg = command_context(
            Path(postgres["bin_dir"]),
            Path(postgres["library_dir"]),
            commands,
        )
        target_empty_cluster(pg, postgres, manifest)
        validate_target_filesystem(manifest, plan)
        verify_checkpoint_index(
            _load_extracted_json(
                extracted,
                manifest["database"]["reference_index"]["member"],
                "checkpoint reference index",
            ),
            extracted / "state" / "runs",
        )
        receipt.update(
            {
                "phase": "preflight-complete",
                "manifest_sha256": manifest_sha,
                "target_plan_sha256": plan_sha,
            }
        )
        write_status(status_dir, receipt)

        mutated = True
        restore_roles(
            context=pg,
            postgres=postgres,
            extracted=extracted,
            status_dir=status_dir,
        )
        restore_database(
            context=pg,
            postgres=postgres,
            extracted=extracted,
        )
        apply_database_acl(
            context=pg,
            postgres=postgres,
            extracted=extracted,
        )
        readback_roles(
            context=pg,
            postgres=postgres,
            extracted=extracted,
        )
        readback_database(
            context=pg,
            postgres=postgres,
            extracted=extracted,
        )
        restore_directories(manifest, plan, extracted)
        restore_exact_files(manifest, extracted)
        verify_restored_files(manifest, plan)
        verify_checkpoint_index(
            _load_extracted_json(
                extracted,
                manifest["database"]["reference_index"]["member"],
                "checkpoint reference index",
            ),
            Path(plan["roots"]["store_root"]),
        )
        state_root = next(Path(root["path"]) for root in manifest["roots"] if root["name"] == "state_root")
        airflow_cfg = state_root / "airflow-home" / "airflow.cfg"
        decrypt_airflow_connection(
            context=pg,
            postgres=postgres,
            airflow_cfg=airflow_cfg,
            connection_id=manifest["airflow"]["connection_id"],
        )
        readback_database(
            context=pg,
            postgres=postgres,
            extracted=extracted,
        )
        marker = {
            "schema_version": MARKER_SCHEMA,
            "manifest_sha256": manifest_sha,
            "target_plan_sha256": plan_sha,
            "release": {
                key_name: manifest["release"][key_name] for key_name in ("version", "source_commit", "source_sha256")
            },
            "mode": "exact_path",
            "verdict": "recovery-verified",
            "created_at": utc_now(),
        }
        write_json_once(status_dir / "recovery-marker.json", marker)
        write_json_once(
            status_dir / "command-log.json",
            {"commands": commands},
        )
        success = {
            **receipt,
            "status": "succeeded",
            "phase": "verified",
            "completed_at": utc_now(),
            "marker": str(status_dir / "recovery-marker.json"),
        }
        write_status(status_dir, success)
        return success
    except BaseException as error:
        code = "recovery_incomplete" if mutated else getattr(error, "code", "recovery_target_plan_invalid")
        failed = {
            **receipt,
            "status": "failed",
            "phase": "target-mutated" if mutated else "preflight",
            "completed_at": utc_now(),
            "code": code,
            "cause": getattr(error, "message", str(error)),
            "detail": getattr(error, "detail", {}),
            "admission": "closed",
            "retry": "discard and recreate an empty target; partial targets are not reusable",
            "command_log": commands,
        }
        write_status(status_dir, failed)
        if isinstance(error, RecoveryError):
            raise
        if isinstance(error, SystemExit):
            raise
        raise RecoveryError(code, str(error)) from error
    finally:
        if temporary is not None:
            temporary.cleanup()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="recovery.py",
        description="Exact-path disaster-recovery backup and empty-target restore.",
    )
    subparsers = parser.add_subparsers(dest="action", required=True)
    backup_parser = subparsers.add_parser("backup")
    backup_parser.add_argument("--config", type=Path, required=True)
    backup_parser.add_argument("--backup-id", required=True)
    backup_parser.add_argument("--key-file", type=Path, required=True)
    backup_parser.add_argument("--out", type=Path, required=True)
    backup_parser.add_argument("--operatorctl", type=Path)
    restore_parser = subparsers.add_parser("restore")
    restore_parser.add_argument("--manifest", type=Path, required=True)
    restore_parser.add_argument("--key", type=Path, required=True)
    restore_parser.add_argument("--target-plan", type=Path, required=True)
    restore_parser.add_argument("--status-dir", type=Path, required=True)
    worker_parser = subparsers.add_parser("snapshot-worker")
    worker_parser.add_argument("--request", type=Path, required=True)
    return parser


def record_backup_failure(
    args: argparse.Namespace,
    error: BaseException,
    claim: BackupOutputClaim | None = None,
) -> dict:
    if claim is None:
        return {
            "maintenance_state": "not_started",
            "recovery_hint": None,
            "receipt_written": False,
            "ownership_verified": False,
        }
    if not _claim_is_current(claim) or claim.backup_id != args.backup_id:
        return {
            "maintenance_state": "unknown_or_stopped",
            "recovery_hint": None,
            "receipt_written": False,
            "ownership_verified": False,
        }
    out = claim.out
    receipt_path = claim.receipt_path
    pending_path = out / "maintenance-window.pending.json"
    stopped_path = out / "maintenance-window.stopped.json"
    marker_path = stopped_path if stopped_path.is_file() else pending_path
    marker = {}
    if marker_path.is_file():
        try:
            value = json.loads(marker_path.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                marker = value
        except (OSError, ValueError):
            marker = {}
    maintenance_state = (
        "stopped" if stopped_path.is_file() else "unknown_or_stopped" if pending_path.is_file() else "not_started"
    )
    recovery_hint = None
    if maintenance_state != "not_started":
        config = marker.get("config", str(args.config))
        operatorctl = marker.get(
            "operatorctl_path",
            str(Path(__file__).resolve().with_name("operatorctl.sh")),
        )
        recovery_hint = "review the failed evidence, then run " + shlex.join(
            [
                shutil.which("bash") or "bash",
                operatorctl,
                "start",
                "--config",
                config,
            ]
        )
    if not receipt_path.is_file():
        return {
            "maintenance_state": maintenance_state,
            "recovery_hint": recovery_hint,
            "receipt_written": False,
            "ownership_verified": True,
        }
    try:
        _, current_receipt = strict_json_file(receipt_path, "backup receipt claim")
    except RecoveryError:
        return {
            "maintenance_state": "unknown_or_stopped",
            "recovery_hint": None,
            "receipt_written": False,
            "ownership_verified": False,
        }
    if current_receipt.get("status") == "succeeded":
        return {
            "maintenance_state": maintenance_state,
            "recovery_hint": recovery_hint,
            "receipt_written": False,
            "ownership_verified": True,
            "completion_preserved": True,
        }
    payload = {
        **current_receipt,
        "status": "failed",
        "completed_at": utc_now(),
        "code": getattr(error, "code", "recovery_backup_failed"),
        "cause": getattr(error, "message", str(error)),
        "detail": getattr(error, "detail", {}),
        "private_staging_removed": None,
        "maintenance_state": maintenance_state,
        "recovery_hint": recovery_hint,
    }
    receipt_written = True
    try:
        require_current_claim(claim)
        atomic_write(receipt_path, canonical_json(payload))
    except (OSError, RecoveryError):
        receipt_written = False
    staging_removed = False
    try:
        if _claim_is_current(claim):
            staging_removed = backup.remove_private_staging(out)
    except (SystemExit, OSError, RecoveryError):
        staging_removed = False
    if receipt_written:
        payload["private_staging_removed"] = staging_removed
        try:
            require_current_claim(claim)
            atomic_write(receipt_path, canonical_json(payload))
        except (OSError, RecoveryError):
            receipt_written = False
    return {
        "maintenance_state": maintenance_state,
        "recovery_hint": recovery_hint,
        "receipt_written": receipt_written,
        "ownership_verified": True,
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    completed_backup = None
    try:
        if args.action == "snapshot-worker":
            return _snapshot_worker(args.request)
        if args.action == "backup":
            result = backup_command(
                config_path=args.config,
                backup_id=args.backup_id,
                key_file=args.key_file,
                out=args.out,
                operatorctl_path=args.operatorctl,
            )
            completed_backup = result
            print(f"manifest={result['manifest']}")
            print(f"manifest_sha256={result['manifest_sha256']}")
            print(f"archive={result['archive']}")
            print(f"archive_sha256={result['archive_sha256']}")
            print("maintenance_state=stopped")
            if result.get("cleanup_warning"):
                print(
                    f"recovery_cleanup_warning={result['cleanup_warning']}",
                    file=sys.stderr,
                )
        else:
            result = restore_command(
                manifest_path=args.manifest,
                key=args.key,
                target_plan_path=args.target_plan,
                status_dir=args.status_dir,
            )
            print(f"status={result['status']}")
            print(f"receipt={args.status_dir / 'receipt.json'}")
        return 0
    except (RecoveryError, SystemExit, Exception) as error:
        failure = None
        if args.action == "backup":
            if completed_backup is not None:
                failure = {
                    "maintenance_state": "stopped",
                    "recovery_hint": (
                        "backup completed; review the preserved output and start services "
                        "only through operatorctl"
                    ),
                    "receipt_written": False,
                    "completion_preserved": True,
                }
            else:
                try:
                    failure = record_backup_failure(
                        args,
                        error,
                        getattr(error, "backup_claim", None),
                    )
                except Exception as recording_error:
                    failure = {
                        "maintenance_state": (
                            "unknown_or_stopped"
                            if getattr(error, "backup_claim", None) is not None
                            else "not_started"
                        ),
                        "recovery_hint": (
                            "inspect backup markers and run the controlled operatorctl start command"
                        ),
                        "receipt_written": False,
                        "recording_error": str(recording_error),
                    }
        print(
            f"recovery: {getattr(error, 'code', 'recovery_command_failed')}: {getattr(error, 'message', str(error))}",
            file=sys.stderr,
        )
        if failure:
            print(
                f"maintenance_state={failure['maintenance_state']}",
                file=sys.stderr,
            )
            if failure.get("recovery_hint"):
                print(
                    f"recovery_hint={failure['recovery_hint']}",
                    file=sys.stderr,
                )
            if not failure.get("receipt_written", True):
                print("recovery_failure_receipt=unavailable", file=sys.stderr)
        if getattr(error, "detail", None):
            print(
                "recovery_detail="
                + json.dumps(
                    error.detail,
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                file=sys.stderr,
            )
        return 1


if __name__ == "__main__":
    sys.exit(main())
