#!/usr/bin/env python3
"""Assemble and persist the Linux-side install record for one generation (IF-16).

Contract: ``docs/deployment.md`` Linux installation / maintenance, upgrade, and recovery (the install record names
every managed service and path; the lifecycle reads the same record), frozen-interfaces-v1.1
IF-16, and the D04 per-end binding: ``ends.linux`` carries this host's snapshot digest while
``ends.windows`` carries the isolated endpoint's own generation - the two are never compared for
equality.  The record's release identity is bound to the deployment's ``release.json``: equal
package digests with a different ``source_commit`` are a mismatch, never a pass.

The Windows end's identity is an explicit input: the endpoint preparation produces
``{install_id, config_digest, release{version, source_commit, source_sha256, role: native}}`` and
this module refuses to write a record without it (fail closed) - no fabricated placeholder
generation is ever recorded.

Like the other deploy-side modules this one is standalone (stdlib only); the terminal
``tests/v1/test_install_record.py`` holds the parity guards against
``description_pipeline.interface_records``.

Exit codes: 0 success; 1 error (message on stderr); 2 usage error.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from platform_config import (  # noqa: E402  (single owners)
    SNAPSHOT_SCHEMA,
    atomic_write_once,
    extract_contract_closure,
    load_contract_owner,
)

INSTALL_RECORD_SCHEMA = "solidworks-to-urdf.install-record/v1"
RELEASE_SCHEMA = "solidworks-to-urdf.release/v1"
DAG_ID = "solidworks_to_urdf"
DAG_MEMBER = "deploy/airflow/dags/solidworks_to_urdf.py"
#: Frozen stage contract size; tests guard parity with description_pipeline.stages.STAGE_IDS.
DAG_TASK_COUNT = 6

_COMMIT = re.compile(r"\A[0-9a-f]{40}\Z")
_HEX64 = re.compile(r"\A[0-9a-f]{64}\Z")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_INSTALL_ID = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_RFC3339_UTC = re.compile(r"\A\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z\Z")


def die(message: str) -> None:
    raise SystemExit(f"install_record: {message}")


def require(condition: bool, message: str) -> None:
    if not condition:
        die(message)


def _text(value, where: str) -> str:
    require(isinstance(value, str) and bool(value) and not _CONTROL.search(value), f"{where} must be non-empty text")
    return value


def _hex64(value, where: str) -> str:
    require(isinstance(value, str) and bool(_HEX64.match(value)), f"{where} must be lowercase sha256 hex")
    return value


def _load_json(path: Path, what: str) -> object:
    require(path.is_file(), f"{what} not found: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        die(f"cannot read {what} {path}: {error}")


def release_identity(document) -> dict:
    """Bind the deployment's ``release.json`` to the record's release block."""

    require(isinstance(document, dict), "release.json must be an object")
    require(document.get("schema_version") == RELEASE_SCHEMA, f"release.json schema_version must be {RELEASE_SCHEMA}")
    for key in ("version", "source_commit", "source_sha256", "artifacts"):
        require(key in document, f"release.json is missing {key}")
    version = _text(document["version"], "release.json version")
    commit = _text(document["source_commit"], "release.json source_commit")
    require(bool(_COMMIT.match(commit)), f"release.json source_commit is malformed: {commit!r}")
    source_sha = _hex64(document["source_sha256"], "release.json source_sha256")
    artifacts = document["artifacts"]
    require(isinstance(artifacts, dict) and bool(artifacts), "release.json artifacts must be a non-empty object")
    rows = []
    for name, checksum in sorted(artifacts.items()):
        _text(name, "release.json artifact name")
        rows.append({"name": name, "sha256": _hex64(checksum, f"release.json artifact {name!r}")})
    return {"version": version, "source_commit": commit, "source_sha256": source_sha, "artifacts": rows}


def release_dag_digest(document) -> str:
    """The release's one deployment member that may become the active Airflow DAG."""

    require(isinstance(document, dict), "release.json must be an object")
    files = document.get("deployment_files")
    require(isinstance(files, dict), "release.json deployment_files must be an object")
    return _hex64(files.get(DAG_MEMBER), f"release.json deployment member {DAG_MEMBER!r}")


def _is_link(path: Path) -> bool:
    """A symlink or a Windows junction (reparse point); both are refused on any component."""

    return path.is_symlink() or path.is_junction()


def confined_relative(name, what: str = "member") -> str:
    """The one lexical rule for a confined relative member path (no escapes, no link syntax)."""

    require(isinstance(name, str) and bool(name) and "\\" not in name, f"{what} is malformed: {name!r}")
    require(not name.startswith("/"), f"{what} escapes its root: {name!r}")
    parts = name.split("/")
    require(
        all(part not in {"", ".", ".."} for part in parts),
        f"{what} escapes its root: {name!r}",
    )
    return name


def _confined_artifact(root: Path, name) -> Path | None:
    """A release-index entry must stay a plain relative file under the distribution root.

    The terminal node's link/junction type is checked before it is followed or read: an entry
    that is itself a symlink is refused even when it targets a real file inside the root.
    """

    confined_relative(name, "artifact path")
    parts = name.split("/")
    candidate = root.joinpath(*parts)
    node = candidate.parent
    while node != root:
        require(not _is_link(node), f"artifact path crosses a link: {name!r}")
        require(root in node.parents, f"artifact path escapes the distribution root: {name!r}")
        node = node.parent
    require(not _is_link(node), f"artifact path crosses a link: {name!r}")
    require(not _is_link(candidate), f"artifact path crosses a link: {name!r}")
    if not candidate.exists():
        return None
    require(candidate.is_file(), f"artifact is not a regular file: {name!r}")
    require(
        Path(os.path.realpath(candidate)).is_relative_to(Path(os.path.realpath(root))),
        f"artifact path escapes the distribution root: {name!r}",
    )
    return candidate


def verify_artifacts(root: Path, artifacts: list[dict]) -> int:
    """Verify every cited artifact that is present under ``root``; at least one must be."""

    verified = 0
    for row in artifacts:
        candidate = _confined_artifact(root, row["name"])
        if candidate is None:
            continue
        actual = hashlib.sha256(candidate.read_bytes()).hexdigest()
        require(actual == row["sha256"], f"artifact {row['name']} does not match release.json ({actual})")
        verified += 1
    require(verified > 0, f"no release artifact is present under {root}")
    return verified


def endpoint_identity(document) -> dict:
    """The isolated endpoint's own generation identity (produced by the endpoint preparation)."""

    require(isinstance(document, dict), "endpoint identity must be an object")
    require(
        set(document) == {"install_id", "config_digest", "release"},
        "endpoint identity must carry exactly install_id, config_digest, release",
    )
    install_id = _text(document["install_id"], "endpoint identity install_id")
    config_digest = _hex64(document["config_digest"], "endpoint identity config_digest")
    release = document["release"]
    require(isinstance(release, dict), "endpoint identity release must be an object")
    require(
        set(release) == {"version", "source_commit", "source_sha256", "role"},
        "endpoint identity release must carry exactly version, source_commit, source_sha256, role",
    )
    require(release["role"] == "native", "endpoint identity release role must be native")
    version = _text(release["version"], "endpoint identity release version")
    commit = _text(release["source_commit"], "endpoint identity release source_commit")
    require(bool(_COMMIT.match(commit)), "endpoint identity release source_commit is malformed")
    source_sha = _hex64(release["source_sha256"], "endpoint identity release source_sha256")
    return {
        "install_id": install_id,
        "config_digest": config_digest,
        "release": {"version": version, "source_commit": commit, "source_sha256": source_sha, "role": "native"},
    }


def require_matching_release(release: dict, endpoint: dict) -> None:
    """The endpoint generation must carry the same release triple (commit skew never passes)."""

    triple = {key: release[key] for key in ("version", "source_commit", "source_sha256")}
    differing = [key for key in triple if endpoint["release"].get(key) != triple[key]]
    require(not differing, f"endpoint release identity mismatch on {differing}")


def _canonical_records():
    """The frozen contract implementation from the single schema owner (mandatory)."""

    try:
        from description_pipeline import interface_records  # noqa: PLC0415
    except ImportError as error:
        die(f"cannot import the canonical schema module (run with the deployment interpreter): {error}")
    return interface_records


def _verify_generation(config_path: Path | None, state_root: Path) -> dict:
    """The one running reader: complete record + immutable snapshot identity.

    ``config_path`` adds exactly one term - the current platform.json equals the snapshot's
    recorded source - which steady-state verbs require and the maintenance window must not
    (a candidate configuration is the subject of a switch, not drift of the running record).
    """

    state_root = Path(state_root)
    if config_path is not None:
        config_path = Path(config_path)
        require(config_path.is_file(), f"config file not found: {config_path}")
    record_path = state_root / "install-record.json"
    require(record_path.is_file(), f"not installed: install record missing at {record_path} (run install first)")
    interface_records = _canonical_records()
    try:
        record = interface_records.validate_install_record(json.loads(record_path.read_text(encoding="utf-8")))
    except Exception as error:
        die(f"install record invalid: {error}")
    snapshot_path = Path(record["snapshot"]["path"])
    require(snapshot_path.is_file(), f"snapshot missing: {snapshot_path}")
    snapshot_bytes = snapshot_path.read_bytes()
    actual = hashlib.sha256(snapshot_bytes).hexdigest()
    require(
        actual == record["snapshot"]["digest"],
        f"snapshot digest mismatch (tampered or replaced): {snapshot_path}",
    )
    try:
        document = json.loads(snapshot_bytes.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        die(f"cannot parse snapshot {snapshot_path}: {error}")
    try:
        interface_records.validate_config_snapshot(document)
    except Exception as error:
        die(f"snapshot invalid: {error}")
    require(
        document["install_id"] == record["install_id"],
        "snapshot generation does not match the install record",
    )
    storage = document["resolved"]["storage"]
    require(
        storage["state_root"] == str(state_root),
        f"install record belongs to another state root: {storage['state_root']}",
    )
    if config_path is not None:
        current = hashlib.sha256(config_path.read_bytes()).hexdigest()
        require(
            current == document["sources"]["platform_json_sha256"],
            "the current platform.json does not match the active snapshot; re-run install or promote",
        )
    return record


def verify_active_generation(config_path: Path, state_root: Path) -> dict:
    """Steady-state gate: the running reader plus the current platform.json binding."""

    return _verify_generation(Path(config_path), Path(state_root))


def verify_running_generation(state_root: Path) -> dict:
    """Maintenance-window gate: the same running reader, without the current-config binding."""

    return _verify_generation(None, Path(state_root))


def wheel_binding(release: dict, *, wheel: Path | None, tree_root: Path | None = None) -> Path:
    """Select the installed wheel and bind it to the release manifest - the single place.

    An explicit ``--wheel`` and tree discovery share one rule: the consumed file's name and
    byte digest must equal a release artifact; anything else fails closed before any leg runs.
    """

    if wheel is not None:
        candidate = Path(wheel)
    else:
        require(tree_root is not None, "either --wheel or --tree is required to select the installed wheel")
        candidates = sorted(path for path in Path(tree_root).glob("*.whl") if path.is_file())
        require(
            len(candidates) == 1,
            f"expected exactly one *.whl under {tree_root} (found {len(candidates)}); pass --wheel",
        )
        candidate = candidates[0]
    require(candidate.is_file(), f"wheel not found: {candidate}")
    rows = {row["name"]: row["sha256"] for row in release["artifacts"]}
    require(candidate.name in rows, f"the consumed wheel {candidate.name!r} is not a release artifact")
    actual = hashlib.sha256(candidate.read_bytes()).hexdigest()
    require(
        actual == rows[candidate.name],
        f"the consumed wheel {candidate.name} does not match release.json ({actual})",
    )
    return candidate


def build_install_record(
    *,
    install_id: str,
    timestamp: str,
    snapshot_path: Path,
    release: dict,
    endpoint: dict,
    services: list[str],
    dag_file: Path,
    deployment_root: Path,
    release_dag_sha256: str,
    previous: dict | None = None,
    journal: list | None = None,
) -> dict:
    """Assemble a record that passes the frozen IF-16 validator (parity-tested).

    ``previous`` is the rollback binding of the generation this record replaces and ``journal``
    carries the switch steps already taken; both are written once, at the pointer switch.
    """

    require(bool(_INSTALL_ID.match(install_id)), f"install_id is malformed: {install_id!r}")
    require(bool(_RFC3339_UTC.match(timestamp)), f"timestamp must be RFC3339 UTC: {timestamp!r}")
    require(
        isinstance(services, list)
        and bool(services)
        and len(set(services)) == len(services)
        and all(isinstance(unit, str) and bool(unit) for unit in services),
        "services must be unique non-empty unit names",
    )

    snapshot_path = Path(snapshot_path)
    require(snapshot_path.is_file(), f"snapshot file not found: {snapshot_path}")
    snapshot_bytes = snapshot_path.read_bytes()
    snapshot_digest = hashlib.sha256(snapshot_bytes).hexdigest()
    try:
        snapshot_document = json.loads(snapshot_bytes.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        die(f"cannot parse snapshot {snapshot_path}: {error}")
    require(
        snapshot_document.get("schema_version") == SNAPSHOT_SCHEMA,
        f"snapshot schema_version must be {SNAPSHOT_SCHEMA}",
    )
    actual_install_id = snapshot_document["install_id"]
    require(
        actual_install_id == install_id,
        f"snapshot generation mismatch: the snapshot declares {actual_install_id!r}, the request is {install_id!r}",
    )
    storage = snapshot_document["resolved"]["storage"]
    paths = {}
    for name in ("state_root", "handoff_root", "artifact_root", "workspace_root", "release_root"):
        paths[name] = _text(storage.get(name), f"snapshot resolved.storage.{name}")

    require(
        set(release) == {"version", "source_commit", "source_sha256", "artifacts"},
        "release block is malformed",
    )
    triple = {key: release[key] for key in ("version", "source_commit", "source_sha256")}
    windows_release = endpoint["release"]
    require_matching_release(release, endpoint)

    deployment_root = Path(deployment_root)
    require(
        not _is_link(deployment_root) and deployment_root.is_dir(),
        f"deployment root must be a real directory: {deployment_root}",
    )
    deployment_root = Path(os.path.realpath(deployment_root))
    dag_file = Path(dag_file)
    require(not _is_link(dag_file), f"dag file must not be a link: {dag_file}")
    require(dag_file.is_file(), f"dag file not found: {dag_file}")
    resolved_dag = Path(os.path.realpath(dag_file))
    require(resolved_dag.name == Path(DAG_MEMBER).name, f"dag file name differs from {DAG_MEMBER!r}")
    require(
        resolved_dag == deployment_root.joinpath(*DAG_MEMBER.split("/")),
        f"dag file is outside the current deployment root: {resolved_dag}",
    )
    dag_digest = hashlib.sha256(resolved_dag.read_bytes()).hexdigest()
    require(
        dag_digest == _hex64(release_dag_sha256, "release DAG digest"),
        f"dag file does not match the release deployment member ({dag_digest})",
    )
    steps = []
    for index, row in enumerate(journal or []):
        require(
            isinstance(row, dict) and set(row) == {"verb", "step", "status", "at", "evidence"},
            f"journal step {index} is malformed",
        )
        require(row["verb"] in {"promote", "rollback"}, f"journal step {index} verb is malformed")
        require(row["status"] in {"completed", "failed", "unknown"}, f"journal step {index} status is malformed")
        _text(row["step"], f"journal step {index} name")
        require(
            isinstance(row["at"], str) and bool(row["at"]),
            f"journal step {index} timestamp is malformed",
        )
        require(
            isinstance(row["evidence"], list)
            and bool(row["evidence"])
            and all(isinstance(item, str) and item for item in row["evidence"]),
            f"journal step {index} evidence is malformed",
        )
        steps.append({"seq": index + 1, **row})
    return {
        "schema_version": INSTALL_RECORD_SCHEMA,
        "install_id": install_id,
        "timestamp": timestamp,
        "snapshot": {"path": str(snapshot_path), "digest": snapshot_digest, "install_id": install_id},
        "release": {
            "version": triple["version"],
            "source_commit": triple["source_commit"],
            "source_sha256": triple["source_sha256"],
            "artifacts": list(release["artifacts"]),
        },
        "services": list(services),
        "paths": paths,
        "dag": {
            "id": DAG_ID,
            "path": str(resolved_dag),
            "digest": dag_digest,
            "tasks": DAG_TASK_COUNT,
        },
        "ends": {
            "linux": {
                "install_id": install_id,
                "config_digest": snapshot_digest,
                "release": {**triple, "role": "portable"},
            },
            "windows": {
                "install_id": endpoint["install_id"],
                "config_digest": endpoint["config_digest"],
                "release": dict(windows_release),
            },
        },
        "previous": previous,
        "journal": steps,
    }


def previous_from_record(record: dict, record_path: Path) -> dict:
    """The rollback binding a new generation records about the one it replaces."""

    return {
        "release": {key: record["release"][key] for key in ("version", "source_commit", "source_sha256")},
        "config": {"path": record["snapshot"]["path"], "digest": record["snapshot"]["digest"]},
        "record": str(Path(record_path)),
    }


def repoint_active(state_root: Path, record_path: Path) -> dict:
    """Atomically point ``install-record.json`` at an existing generation file (rollback).

    One read is the only source: those exact bytes are validated by the canonical package
    validator and then committed, so nothing can change between the check and the write.  The
    active pointer keeps mode 0600 and refuses a conflicting temporary path.
    """

    state_root, record_path = Path(state_root), Path(record_path)
    require(record_path.is_file(), f"rollback record not found: {record_path}")
    try:
        payload = record_path.read_bytes()
    except OSError as error:
        die(f"cannot read record {record_path}: {error}")
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        die(f"cannot parse rollback record {record_path}: {error}")
    try:
        _canonical_records().validate_install_record(document)
    except Exception as error:
        die(f"rollback record invalid: {error}")
    active = state_root / "install-record.json"
    active.parent.mkdir(parents=True, exist_ok=True)
    temporary = active.with_name(active.name + ".repoint.tmp")
    require(not temporary.exists(), f"refuse a conflicting temporary path: {temporary}")
    handle = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(handle, "wb") as stream:
        stream.write(payload)
    os.replace(temporary, active)
    return document


def validate_endpoint_health(health_document, release: dict) -> tuple[dict, dict]:
    """Derive the Windows end's identity from the running endpoint's own IF-09 ``/health``.

    The health payload is validated by the canonical package validator and its release triple
    must equal the release manifest; a hand-written declaration never becomes an identity.
    """

    interface_records = _canonical_records()
    try:
        health = interface_records.validate_health(health_document)
    except Exception as error:
        die(f"endpoint health invalid: {error}")
    require_matching_release(release, {"release": health["release"]})
    identity = {
        "install_id": health["install"]["install_id"],
        "config_digest": health["install"]["config_digest"],
        "release": {key: health["release"][key] for key in ("version", "source_commit", "source_sha256", "role")},
    }
    return identity, health


def write_install_record(record: dict, state_root: Path, owner) -> tuple[Path, Path]:
    """Persist one generation (write-once) and update the active record pointer atomically."""

    payload = owner.canonical(record)
    generation = atomic_write_once(state_root / "install-records" / f"{record['install_id']}.json", payload)
    active = state_root / "install-record.json"
    active.parent.mkdir(parents=True, exist_ok=True)
    temporary = active.with_name(f".{active.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, active)
    finally:
        temporary.unlink(missing_ok=True)
    os.chmod(active, 0o600)
    return generation, active


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="install_record.py", description=__doc__.splitlines()[0])
    subparsers = parser.add_subparsers(dest="action", required=True)
    build = subparsers.add_parser("build")
    build.add_argument("--state-root", type=Path, required=True)
    build.add_argument("--install-id", required=True)
    build.add_argument("--timestamp", default=None)
    build.add_argument("--snapshot", type=Path, required=True)
    build.add_argument("--release-file", type=Path, required=True)
    build.add_argument("--endpoint-identity", type=Path, required=True)
    build.add_argument("--services", required=True, help="comma-separated managed unit names")
    build.add_argument("--dag", type=Path, required=True)
    build.add_argument("--deployment-root", type=Path, required=True)
    build.add_argument("--wheel", type=Path, default=None, help="the installed wheel (contract owner source)")
    build.add_argument("--tree", type=Path, default=None, help="deployment tree for wheel discovery")
    build.add_argument("--previous-record", type=Path, default=None, help="the generation this record replaces")
    build.add_argument("--journal", type=Path, default=None, help="the switch steps taken before the pointer switch")
    check = subparsers.add_parser("check")
    check.add_argument("--release-file", type=Path, required=True)
    check.add_argument("--endpoint-identity", type=Path, required=True)
    check.add_argument("--wheel", type=Path, default=None, help="the wheel that will be installed")
    check.add_argument("--tree", type=Path, default=None, help="deployment tree for wheel discovery")
    verify = subparsers.add_parser("verify-active")
    verify.add_argument("--config", type=Path, required=True)
    verify.add_argument("--state-root", type=Path, required=True)
    running = subparsers.add_parser("verify-running")
    running.add_argument("--state-root", type=Path, required=True)
    repoint = subparsers.add_parser("repoint")
    repoint.add_argument("--state-root", type=Path, required=True)
    repoint.add_argument("--record", type=Path, required=True, help="the generation file to activate (rollback)")
    identity = subparsers.add_parser("endpoint-identity")
    identity.add_argument("--health", type=Path, required=True, help="the endpoint's own /health payload")
    identity.add_argument("--release-file", type=Path, required=True)
    identity.add_argument("--record", type=Path, default=None, help="cross-check against this install record")
    identity.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)

    if args.action == "verify-active":
        record = verify_active_generation(args.config, args.state_root)
        print(f"install_id={record['install_id']}")
        return 0
    if args.action == "verify-running":
        record = verify_running_generation(args.state_root)
        print(f"install_id={record['install_id']}")
        return 0
    if args.action == "repoint":
        record = repoint_active(args.state_root, args.record)
        print(f"install_id={record['install_id']}")
        return 0
    if args.action == "endpoint-identity":
        release = release_identity(_load_json(args.release_file, "release file"))
        health_document = _load_json(args.health, "endpoint health")
        if args.record is not None:
            # The record is the authority for its own per-end release (rollback verifies the
            # restored generation, whose release differs from the current manifest).
            interface_records = _canonical_records()
            try:
                health = interface_records.validate_health(health_document)
                record = interface_records.validate_install_record(_load_json(args.record, "install record"))
            except Exception as error:
                die(f"endpoint health or record invalid: {error}")
            try:
                interface_records.require_endpoint_matches_install_record(health, record)
            except Exception as error:
                detail = getattr(error, "detail", None)
                die(f"endpoint does not match the active record: {error} {detail if detail else ''}")
            endpoint = {
                "install_id": health["install"]["install_id"],
                "config_digest": health["install"]["config_digest"],
                "release": {
                    key: health["release"][key] for key in ("version", "source_commit", "source_sha256", "role")
                },
            }
        else:
            endpoint, health = validate_endpoint_health(health_document, release)
        args.out.write_text(json.dumps(endpoint, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"install_id={endpoint['install_id']}")
        return 0
    if args.action == "check":
        release = release_identity(_load_json(args.release_file, "release file"))
        verified = verify_artifacts(args.release_file.parent, release["artifacts"])
        endpoint = endpoint_identity(_load_json(args.endpoint_identity, "endpoint identity"))
        require_matching_release(release, endpoint)
        if args.wheel is not None or args.tree is not None:
            wheel_binding(release, wheel=args.wheel, tree_root=args.tree)
        print("check=ok")
        print(f"verified_artifacts={verified}")
        return 0

    timestamp = args.timestamp or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    release_document = _load_json(args.release_file, "release file")
    release = release_identity(release_document)
    release_dag_sha256 = release_dag_digest(release_document)
    verified = verify_artifacts(args.release_file.parent, release["artifacts"])
    endpoint = endpoint_identity(_load_json(args.endpoint_identity, "endpoint identity"))
    services = [unit for unit in args.services.split(",") if unit]
    record = build_install_record(
        install_id=args.install_id,
        timestamp=timestamp,
        snapshot_path=args.snapshot,
        release=release,
        endpoint=endpoint,
        services=services,
        dag_file=args.dag,
        deployment_root=args.deployment_root,
        release_dag_sha256=release_dag_sha256,
        previous=(
            previous_from_record(
                _load_json(args.previous_record, "previous record"),
                Path(args.previous_record).resolve(),
            )
            if args.previous_record is not None
            else None
        ),
        journal=(_load_json(args.journal, "journal file") if args.journal is not None else None),
    )
    bound_wheel = wheel_binding(release, wheel=args.wheel, tree_root=args.tree)
    contract = load_contract_owner(extract_contract_closure(bound_wheel))
    generation, active = write_install_record(record, args.state_root, contract)
    print(f"generation={generation}")
    print(f"active={active}")
    print(f"record_digest={hashlib.sha256(contract.canonical(record)).hexdigest()}")
    print(f"verified_artifacts={verified}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
