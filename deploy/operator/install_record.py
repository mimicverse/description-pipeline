#!/usr/bin/env python3
"""Assemble and persist the first-install Linux-side install record (IF-16).

The record names the current snapshot, release, services, DAG and the independently installed
Windows endpoint. ``ends.linux`` carries this host's snapshot digest while ``ends.windows``
carries the endpoint's own generation; the two digests are not compared for equality. The
record's release identity is bound to the deployment's ``release.json``.

The Windows end's identity is an explicit input: the endpoint preparation produces
``{install_id, config_digest, release{version, source_commit, source_sha256, role: native},
target_identity{schema_version, install_id, pipeline_id, target_id}}`` and this module refuses
to write a record without it (fail closed) - no fabricated placeholder generation is ever
recorded.

This module intentionally has no upgrade, rollback, previous-record or journal operations.

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
    valid_install_id,
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
    tree = _text(document.get("source_tree"), "release.json source_tree")
    require(bool(_COMMIT.fullmatch(tree)), "release.json source_tree is malformed")
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
    """Verify the complete release artifact set under ``root``."""

    verified = 0
    for row in artifacts:
        candidate = _confined_artifact(root, row["name"])
        if candidate is None:
            die(f"release artifact is missing: {row['name']}")
        actual = hashlib.sha256(candidate.read_bytes()).hexdigest()
        require(actual == row["sha256"], f"artifact {row['name']} does not match release.json ({actual})")
        verified += 1
    return verified


def _native_target_contract():
    """Load the canonical target identity owner from the installed release."""

    try:
        from description_pipeline.delivery import PIPELINE_ID  # noqa: PLC0415
        from description_pipeline.pipeline.contracts import NativeTargetIdentity  # noqa: PLC0415
    except ImportError as error:
        die(f"cannot import the canonical native target contract: {error}")
    return NativeTargetIdentity, PIPELINE_ID


def endpoint_identity(document) -> dict:
    """The isolated endpoint's own generation identity (produced by the endpoint preparation)."""

    require(isinstance(document, dict), "endpoint identity must be an object")
    require(
        set(document) == {"install_id", "config_digest", "release", "target_identity"},
        "endpoint identity must carry exactly install_id, config_digest, release, target_identity",
    )
    install_id = _text(document["install_id"], "endpoint identity install_id")
    config_digest = _hex64(document["config_digest"], "endpoint identity config_digest")
    target_contract, pipeline_id = _native_target_contract()
    try:
        target = target_contract.from_record(document["target_identity"])
    except Exception as error:
        die(f"endpoint target identity is invalid: {error}")
    require(
        target.install_id == install_id,
        "endpoint target identity install_id differs from the endpoint install_id",
    )
    require(
        target.pipeline_id == pipeline_id,
        f"endpoint target pipeline_id must be {pipeline_id}",
    )
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
        "target_identity": target.to_record(),
    }


def endpoint_identity_from_status(status_document, release: dict) -> dict:
    """Read the installed endpoint identity from its read-only status result."""

    require(
        isinstance(status_document, dict)
        and status_document.get("schema_version")
        == "solidworks-to-urdf.windows-lifecycle-result/v1"
        and status_document.get("passed") is True
        and status_document.get("verb") == "status",
        "endpoint status result is invalid",
    )
    identity = status_document.get("identity")
    require(isinstance(identity, dict), "endpoint status carries no installed identity")
    install_id = _text(identity.get("install_id"), "endpoint status install_id")
    config_digest = _hex64(
        identity.get("config_digest"),
        "endpoint status config_digest",
    )
    endpoint_release = identity.get("release")
    require(isinstance(endpoint_release, dict), "endpoint status release is missing")
    require_matching_release(release, {"release": endpoint_release})
    target_contract, pipeline_id = _native_target_contract()
    target = target_contract.create(
        install_id=install_id,
        pipeline_id=pipeline_id,
    )
    return {
        "install_id": install_id,
        "config_digest": config_digest,
        "release": {
            key: endpoint_release[key]
            for key in ("version", "source_commit", "source_sha256", "role")
        },
        "target_identity": target.to_record(),
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


def read_installed_generation(
    state_root: Path,
    release_file: Path | None = None,
) -> tuple[dict, dict]:
    """Read the complete current record and its immutable snapshot identity."""

    state_root = Path(state_root)
    record_path = state_root / "install-record.json"
    require(record_path.is_file(), f"not installed: install record missing at {record_path} (run install first)")
    interface_records = _canonical_records()
    try:
        record = interface_records.validate_install_record(json.loads(record_path.read_text(encoding="utf-8")))
    except Exception as error:
        die(f"install record invalid: {error}")
    require(valid_install_id(record.get("install_id")), "install record install_id is malformed")
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
    if release_file is not None:
        release = release_identity(_load_json(Path(release_file), "release file"))
        for key in ("version", "source_commit", "source_sha256"):
            require(
                record["release"].get(key) == release.get(key),
                f"install record release differs on {key}",
            )
    return record, document


def verify_active_generation(
    config_path: Path,
    state_root: Path,
    release_file: Path | None = None,
) -> dict:
    """Verify the installed identity and the current configuration source."""

    config_path = Path(config_path)
    require(config_path.is_file(), f"config file not found: {config_path}")
    record, document = read_installed_generation(state_root, release_file)
    current = hashlib.sha256(config_path.read_bytes()).hexdigest()
    require(
        current == document["sources"]["platform_json_sha256"],
        "the current platform.json does not match the active snapshot; re-run install",
    )
    return record


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
) -> dict:
    """Assemble the one first-install record that passes the frozen IF-16 validator."""

    require(valid_install_id(install_id), "install_id is malformed")
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
    target_contract, pipeline_id = _native_target_contract()
    try:
        target = target_contract.from_record(endpoint["target_identity"])
    except Exception as error:
        die(f"endpoint target identity is invalid: {error}")
    require(target.install_id == endpoint["install_id"], "endpoint target identity names another install_id")
    require(target.pipeline_id == pipeline_id, f"endpoint target pipeline_id must be {pipeline_id}")

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
                "target_identity": target.to_record(),
            },
        },
    }


def write_install_record(record: dict, state_root: Path, owner) -> tuple[Path, Path]:
    """Persist the one first installation without replacing any existing authority."""

    payload = owner.canonical(record)
    active = state_root / "install-record.json"
    require(not active.exists() and not active.is_symlink(), f"already installed: {active}")
    generation_path = state_root / "install-records" / f"{record['install_id']}.json"
    require(
        not generation_path.exists() and not generation_path.is_symlink(),
        f"install generation already exists: {generation_path}",
    )
    generation = atomic_write_once(generation_path, payload)
    active.parent.mkdir(parents=True, exist_ok=True)
    temporary = active.with_name(f".{active.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, active)
        except FileExistsError as error:
            raise SystemExit(f"install_record: already installed: {active}") from error
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
    check = subparsers.add_parser("check")
    check.add_argument("--release-file", type=Path, required=True)
    check.add_argument("--wheel", type=Path, default=None, help="the wheel that will be installed")
    check.add_argument("--tree", type=Path, default=None, help="deployment tree for wheel discovery")
    verify = subparsers.add_parser("verify-active")
    verify.add_argument("--config", type=Path, required=True)
    verify.add_argument("--state-root", type=Path, required=True)
    verify.add_argument("--release-file", type=Path)
    identity = subparsers.add_parser("endpoint-identity")
    identity.add_argument("--status", type=Path, required=True)
    identity.add_argument("--release-file", type=Path, required=True)
    identity.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)

    if args.action == "verify-active":
        record = verify_active_generation(args.config, args.state_root, args.release_file)
        print(f"install_id={record['install_id']}")
        return 0
    if args.action == "endpoint-identity":
        release = release_identity(_load_json(args.release_file, "release file"))
        endpoint = endpoint_identity_from_status(
            _load_json(args.status, "endpoint status"),
            release,
        )
        args.out.write_text(
            json.dumps(endpoint, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(f"install_id={endpoint['install_id']}")
        return 0
    if args.action == "check":
        release = release_identity(_load_json(args.release_file, "release file"))
        verified = verify_artifacts(args.release_file.parent, release["artifacts"])
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
