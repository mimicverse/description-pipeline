#!/usr/bin/env python3
"""Validate the private ``platform.json`` and render the immutable config snapshot (IF-16).

Contract: ``docs/deployment.md`` §配置权威 - one private ``platform.json`` (schema
``solidworks-to-urdf.platform/v1``) is the single configuration authority; the schema refuses
unknown keys, unsafe paths, incomplete targets and conflicting ports, and secrets travel only as
file references.  ``install`` renders the immutable generation snapshot
(``solidworks-to-urdf.config-snapshot/v1``) whose canonical digest the console, Airflow and
maintenance commands all read (frozen-interfaces-v1.1 IF-11/IF-16).

Like ``render_operator.py`` this module is standalone (stdlib only): a deployment tree runs it
with the host interpreter before any virtualenv exists.  ``tests/v1/test_platform_config.py``
holds the parity guards against the package-side validators
(``description_pipeline.interface_records``) and the canonical serialization
(``description_pipeline.io``).

``resolved.windows.instance`` additionally binds the isolated Windows target (task name,
package/output/state roots, endpoint port) so observation/stop/restore paths can only ever
address the configured instance; nothing falls back to a production task name or host path.

Exit codes: 0 success; 1 configuration error (message on stderr); 2 usage error.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).resolve().parent))

from render_operator import PORT_DEFAULTS  # noqa: E402  (single deploy-layer port source, DEP-01)

PLATFORM_SCHEMA = "solidworks-to-urdf.platform/v1"
SNAPSHOT_SCHEMA = "solidworks-to-urdf.config-snapshot/v1"
#: Loopback default for the Windows tunnel, owned by render_operator.PORT_DEFAULTS (DEP-01).
DEFAULT_TUNNEL_LOOPBACK_PORT = PORT_DEFAULTS["TUNNEL_LOOPBACK_PORT"]

_PLATFORM_REQUIRED_KEYS = {
    "schema_version",
    "operator",
    "storage",
    "database",
    "windows",
    "auth",
    "targets",
    "specifications",
}
_PLATFORM_ALLOWED_KEYS = _PLATFORM_REQUIRED_KEYS | {"execution", "retention"}
_OPERATOR_KEYS = {"origin", "certificate_file", "private_key_file"}
_STORAGE_KEYS = ("state_root", "handoff_root", "artifact_root", "workspace_root", "release_root")
_DATABASE_KEYS = {"dsn_file"}
_WINDOWS_KEYS = {"ssh_host", "endpoint_port", "token_file", "tunnel_loopback_port", "instance"}
_INSTANCE_KEYS = {"task_name", "package_root", "output_root", "state_root"}
_AUTH_KEYS = {"feishu_secret_file", "tenant_keys", "admin_open_ids"}
_TARGET_KEYS = {"repository_slug", "base"}
_SPECIFICATION_KEYS = {"record_roots"}
_EXECUTION_KEYS = {"linux_concurrency"}
_RETENTION_KEYS = {"unreferenced_evidence_days"}

_ORIGIN = re.compile(r"\Ahttps://([A-Za-z0-9][A-Za-z0-9.-]*)(?::([0-9]{1,5}))?\Z")
_SSH_HOST = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.-]*\Z")
_TASK_NAME = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
_WINDOWS_DRIVE = re.compile(r"\A[A-Za-z]:/")
_TARGET_KEY = re.compile(r"\A[A-Za-z][A-Za-z0-9_.-]{0,63}\Z")
_REPOSITORY_SLUG = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_BASE_BRANCH = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._/-]*\Z")
_INSTALL_ID = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_RFC3339_UTC = re.compile(r"\A\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z\Z")
_HEX64 = re.compile(r"\A[0-9a-f]{64}\Z")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def die(message: str) -> None:
    raise SystemExit(f"platform_config: {message}")


def require(condition: bool, message: str) -> None:
    if not condition:
        die(message)


def require_keys(mapping, *, allowed: set[str], required: set[str], where: str) -> None:
    require(isinstance(mapping, dict), f"{where} must be an object")
    unknown = sorted(set(mapping) - allowed)
    require(not unknown, f"{where} has unknown keys: {unknown}")
    missing = sorted(required - set(mapping))
    require(not missing, f"{where} is missing keys: {missing}")


def text(value, where: str) -> str:
    require(isinstance(value, str) and bool(value) and not _CONTROL.search(value), f"{where} must be non-empty text")
    return value


def absolute_path(value, where: str) -> PurePosixPath:
    text(value, where)
    path = PurePosixPath(value)
    require(path.is_absolute() and ".." not in path.parts, f"{where} must be an absolute path without '..'")
    return path


def windows_path(value, where: str) -> str:
    text(value, where)
    require(
        bool(_WINDOWS_DRIVE.match(value)) and "\\" not in value,
        f"{where} must be a drive-absolute Windows path with forward slashes",
    )
    parts = value[3:].split("/")
    require(all(part not in {"", ".", ".."} for part in parts), f"{where} must not contain traversal or empty segments")
    return value


def string_list(value, where: str, *, allow_empty: bool) -> list[str]:
    require(isinstance(value, list), f"{where} must be a list")
    require(allow_empty or bool(value), f"{where} must not be empty")
    require(
        all(isinstance(item, str) and bool(item) and not _CONTROL.search(item) for item in value)
        and len(set(value)) == len(value),
        f"{where} must be unique non-empty strings",
    )
    return value


def tcp_port(value, where: str) -> int:
    require(type(value) is int and 0 < value < 65536, f"{where} must be a TCP port (1-65535)")
    return value


def _within(child: PurePosixPath, parent: PurePosixPath) -> bool:
    return child == parent or parent in child.parents


def validate_platform(document) -> dict:
    """Validate the raw platform.json and return the resolved snapshot ``resolved`` tree."""

    require_keys(
        document,
        allowed=_PLATFORM_ALLOWED_KEYS,
        required=_PLATFORM_REQUIRED_KEYS,
        where="platform.json",
    )
    require(
        document["schema_version"] == PLATFORM_SCHEMA,
        f"platform.json schema_version must be {PLATFORM_SCHEMA}",
    )

    operator = document["operator"]
    require_keys(operator, allowed=_OPERATOR_KEYS, required=_OPERATOR_KEYS, where="operator")
    origin = text(operator["origin"], "operator.origin")
    match = _ORIGIN.match(origin)
    require(match is not None, f"operator.origin must be an https origin, got {origin!r}")
    origin_port = int(match.group(2)) if match.group(2) else 443
    absolute_path(operator["certificate_file"], "operator.certificate_file")
    absolute_path(operator["private_key_file"], "operator.private_key_file")

    storage = document["storage"]
    require_keys(storage, allowed=set(_STORAGE_KEYS), required=set(_STORAGE_KEYS), where="storage")
    storage_paths = {name: absolute_path(storage[name], f"storage.{name}") for name in _STORAGE_KEYS}

    database = document["database"]
    require_keys(database, allowed=_DATABASE_KEYS, required=_DATABASE_KEYS, where="database")
    absolute_path(database["dsn_file"], "database.dsn_file")

    windows = document["windows"]
    require_keys(
        windows,
        allowed=_WINDOWS_KEYS,
        required=_WINDOWS_KEYS - {"tunnel_loopback_port"},
        where="windows",
    )
    ssh_host = text(windows["ssh_host"], "windows.ssh_host")
    require(bool(_SSH_HOST.match(ssh_host)), f"windows.ssh_host is malformed: {ssh_host!r}")
    endpoint_port = tcp_port(windows["endpoint_port"], "windows.endpoint_port")
    tunnel_port = tcp_port(
        windows.get("tunnel_loopback_port", DEFAULT_TUNNEL_LOOPBACK_PORT), "windows.tunnel_loopback_port"
    )
    absolute_path(windows["token_file"], "windows.token_file")
    instance = windows["instance"]
    require_keys(instance, allowed=_INSTANCE_KEYS, required=_INSTANCE_KEYS, where="windows.instance")
    task_name = text(instance["task_name"], "windows.instance.task_name")
    require(bool(_TASK_NAME.match(task_name)), f"windows.instance.task_name is malformed: {task_name!r}")
    instance_paths = {
        field: windows_path(instance[field], f"windows.instance.{field}")
        for field in ("package_root", "output_root", "state_root")
    }
    require(
        len({origin_port, endpoint_port, tunnel_port}) == 3,
        "configured ports must not collide (origin / endpoint / tunnel_loopback)",
    )

    auth = document["auth"]
    require_keys(auth, allowed=_AUTH_KEYS, required=_AUTH_KEYS, where="auth")
    absolute_path(auth["feishu_secret_file"], "auth.feishu_secret_file")
    string_list(auth["tenant_keys"], "auth.tenant_keys", allow_empty=False)
    string_list(auth["admin_open_ids"], "auth.admin_open_ids", allow_empty=True)

    targets = document["targets"]
    require(isinstance(targets, dict) and bool(targets), "targets must be a non-empty object")
    slugs: set[str] = set()
    for hardware, target in targets.items():
        require(
            isinstance(hardware, str) and bool(_TARGET_KEY.match(hardware)),
            f"targets key is malformed: {hardware!r}",
        )
        require_keys(target, allowed=_TARGET_KEYS, required=_TARGET_KEYS, where=f"targets.{hardware}")
        slug = text(target["repository_slug"], f"targets.{hardware}.repository_slug")
        require(bool(_REPOSITORY_SLUG.match(slug)), f"targets.{hardware}.repository_slug is malformed: {slug!r}")
        base = text(target["base"], f"targets.{hardware}.base")
        require(
            bool(_BASE_BRANCH.match(base)) and ".." not in base and not base.endswith(".lock"),
            f"targets.{hardware}.base is malformed: {base!r}",
        )
        slugs.add(slug)

    specifications = document["specifications"]
    require_keys(specifications, allowed=_SPECIFICATION_KEYS, required=_SPECIFICATION_KEYS, where="specifications")
    record_roots = string_list(specifications["record_roots"], "specifications.record_roots", allow_empty=False)
    record_paths = [
        absolute_path(root, f"specifications.record_roots[{index}]") for index, root in enumerate(record_roots)
    ]

    execution = document.get("execution", {"linux_concurrency": 1})
    require_keys(execution, allowed=_EXECUTION_KEYS, required=_EXECUTION_KEYS, where="execution")
    concurrency = execution["linux_concurrency"]
    require(type(concurrency) is int and concurrency >= 1, "execution.linux_concurrency must be a positive integer")

    retention = document.get("retention", {"unreferenced_evidence_days": None})
    require_keys(retention, allowed=_RETENTION_KEYS, required=_RETENTION_KEYS, where="retention")
    days = retention["unreferenced_evidence_days"]
    require(
        days is None or (type(days) is int and days > 0),
        "retention.unreferenced_evidence_days must be null or a positive integer",
    )

    # Derived single-source values: the store root lives under state_root, every target slug has
    # exactly one checkout under workspace_root, and discovery consumes the specification roots.
    store_root = storage_paths["state_root"] / "runs"
    repositories: dict[str, str] = {}
    for slug in sorted(slugs):
        checkout = storage_paths["workspace_root"] / slug.split("/", 1)[1]
        other = repositories.get(str(checkout))
        require(other is None, f"target repositories {other!r} and {slug!r} share one checkout: {checkout}")
        repositories[slug] = str(checkout)

    named_roots = [(name, storage_paths[name]) for name in _STORAGE_KEYS]
    named_roots += [(f"specifications.record_roots[{index}]", path) for index, path in enumerate(record_paths)]
    for index, (left_name, left) in enumerate(named_roots):
        for right_name, right in named_roots[index + 1 :]:
            require(
                not _within(left, right) and not _within(right, left),
                f"configured roots must not overlap: {left_name} vs {right_name}",
            )
        if left_name in _STORAGE_KEYS and left_name != "state_root":
            require(
                not _within(store_root, left) and not _within(left, store_root),
                f"the store root must not overlap {left_name}",
            )

    return {
        "schema_version": PLATFORM_SCHEMA,
        "operator": {
            "origin": origin,
            "certificate_file": str(operator["certificate_file"]),
            "private_key_file": str(operator["private_key_file"]),
        },
        "storage": {name: str(storage_paths[name]) for name in _STORAGE_KEYS},
        "database": {"dsn_file": str(database["dsn_file"])},
        "windows": {
            "ssh_host": ssh_host,
            "endpoint_port": endpoint_port,
            "token_file": str(windows["token_file"]),
            "tunnel_loopback_port": tunnel_port,
            "instance": {"task_name": task_name, **instance_paths},
        },
        "auth": {
            "feishu_secret_file": str(auth["feishu_secret_file"]),
            "tenant_keys": list(auth["tenant_keys"]),
            "admin_open_ids": list(auth["admin_open_ids"]),
        },
        "targets": {
            hardware: {"repository_slug": target["repository_slug"], "base": target["base"]}
            for hardware, target in targets.items()
        },
        "specifications": {"record_roots": list(record_roots)},
        "execution": {"linux_concurrency": concurrency},
        "retention": {"unreferenced_evidence_days": days},
        "derived": {
            "store_root": str(store_root),
            "repositories": repositories,
            "discovery": {"record_roots": list(record_roots)},
        },
    }


def canonical(data) -> bytes:
    """The IF-05 canonical serialization; byte-identical to ``description_pipeline.io.canonical``."""

    return (json.dumps(data, sort_keys=True, ensure_ascii=False, allow_nan=False, indent=2) + "\n").encode()


def snapshot_digest(snapshot: dict) -> str:
    """Canonical digest of the snapshot bytes (the snapshot stores no self-digest)."""

    return hashlib.sha256(canonical(snapshot)).hexdigest()


def build_snapshot(resolved: dict, *, platform_json_sha256: str, install_id: str, generated_at: str) -> dict:
    require(bool(_INSTALL_ID.match(install_id)), f"install_id is malformed: {install_id!r}")
    require(bool(_RFC3339_UTC.match(generated_at)), f"generated_at must be RFC3339 UTC: {generated_at!r}")
    require(bool(_HEX64.match(platform_json_sha256)), "platform_json_sha256 must be lowercase sha256 hex")
    return {
        "schema_version": SNAPSHOT_SCHEMA,
        "install_id": install_id,
        "generated_at": generated_at,
        "resolved": resolved,
        "sources": {"platform_json_sha256": platform_json_sha256},
    }


def write_snapshot(snapshot: dict, snapshots_dir: Path) -> Path:
    """Write the generation snapshot atomically (0600); a generation is never rewritten."""

    snapshots_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(snapshots_dir, stat.S_IRWXU)
    path = snapshots_dir / f"{snapshot['install_id']}.json"
    payload = canonical(snapshot)
    if path.exists():
        require(
            path.read_bytes() == payload,
            f"the generation snapshot already exists with different bytes: {path}",
        )
        return path
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    os.chmod(path, 0o600)
    return path


def load_document(path: Path) -> tuple[bytes, object]:
    require(path.is_file(), f"config file not found: {path}")
    try:
        raw = path.read_bytes()
    except OSError as error:
        die(f"cannot read {path}: {error}")
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        die(f"cannot parse {path}: {error}")
    return raw, document


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="platform_config.py", description=__doc__.splitlines()[0])
    subparsers = parser.add_subparsers(dest="action", required=True)
    for action in ("validate", "render", "resolve"):
        subparser = subparsers.add_parser(action)
        subparser.add_argument("--config", type=Path, required=True, help="path to the private platform.json")
        if action == "render":
            subparser.add_argument("--install-id", default=None, help="installation generation id (default: generated)")
            subparser.add_argument("--generated-at", default=None, help="RFC3339 UTC timestamp (default: now)")
    args = parser.parse_args(argv)

    raw, document = load_document(args.config)
    resolved = validate_platform(document)
    if args.action == "validate":
        print(f"{args.config}: valid ({len(resolved['targets'])} targets)")
        return 0
    if args.action == "resolve":
        print(json.dumps(resolved, indent=2))
        return 0

    install_id = args.install_id or uuid.uuid4().hex
    generated_at = args.generated_at or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    snapshot = build_snapshot(
        resolved,
        platform_json_sha256=hashlib.sha256(raw).hexdigest(),
        install_id=install_id,
        generated_at=generated_at,
    )
    snapshot_path = write_snapshot(snapshot, Path(resolved["storage"]["state_root"]) / "snapshots")
    print(f"config_digest={snapshot_digest(snapshot)}")
    print(f"snapshot={snapshot_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
