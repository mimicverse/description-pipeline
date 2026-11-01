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
import io
import json
import os
import re
import sys
import zipfile
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
WINDOWS_RESULT_SCHEMA = "solidworks-to-urdf.windows-lifecycle-result/v1"
DAG_ID = "solidworks_to_urdf"
DAG_MEMBER = "deploy/airflow/dags/solidworks_to_urdf.py"
#: Frozen stage contract size; tests guard parity with description_pipeline.stages.STAGE_IDS.
DAG_TASK_COUNT = 7

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


def release_deployment_files(document) -> dict[str, str]:
    """The exact path-to-digest inventory extracted beside release.json."""

    require(isinstance(document, dict), "release.json must be an object")
    files = document.get("deployment_files")
    require(
        isinstance(files, dict) and bool(files),
        "release.json deployment_files must be a non-empty object",
    )
    inventory = {}
    for name, checksum in files.items():
        confined_relative(name, "release.json deployment member")
        inventory[name] = _hex64(checksum, f"release.json deployment member {name!r}")
    return inventory


def release_dag_digest(document) -> str:
    """The release's one deployment member that may become the active Airflow DAG."""

    return release_deployment_files(document)[DAG_MEMBER]


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


def verify_deployment_tree(root: Path, document) -> int:
    """Verify the extracted deployment tree against the release's exact member inventory."""

    root = Path(root)
    require(not _is_link(root) and root.is_dir(), f"deployment root must be a real directory: {root}")
    release_manifest = root / "release.json"
    require(
        release_manifest.is_file() and not _is_link(release_manifest),
        f"deployment release.json must be a regular non-link file: {release_manifest}",
    )
    expected = release_deployment_files(document)
    release = release_identity(document)
    allowed_root_files = {"release.json", *(row["name"] for row in release["artifacts"])}
    observed = {}
    pending = [root]
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                path = Path(entry.path)
                require(not _is_link(path), f"deployment tree crosses a link: {path}")
                relative = path.relative_to(root).as_posix()
                if entry.is_dir(follow_symlinks=False):
                    pending.append(path)
                    continue
                require(entry.is_file(follow_symlinks=False), f"deployment member is not a regular file: {relative}")
                if directory == root and relative in allowed_root_files:
                    continue
                observed[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    require(
        set(observed) == set(expected),
        "deployment tree members differ from release.json "
        f"(missing={sorted(set(expected) - set(observed))}, extra={sorted(set(observed) - set(expected))})",
    )
    differing = sorted(name for name, checksum in expected.items() if observed[name] != checksum)
    require(not differing, f"deployment members differ from release.json: {differing}")
    return len(expected)


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
    task = status_document.get("task")
    status = status_document.get("status")
    require(
        isinstance(task, dict)
        and task.get("state") == "Disabled"
        and isinstance(status, dict)
        and status.get("task_exists") is True
        and status.get("task_state") == "Disabled"
        and status.get("error") is None
        and status.get("consistent") is True
        and type(status.get("solidworks_processes")) is int
        and status["solidworks_processes"] == 0
        and status_document.get("listener") is None
        and status_document.get("health") is None,
        "endpoint status does not prove a stopped, consistent endpoint",
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


def _windows_identity_matches(record: dict, identity: dict, *, target_required: bool) -> bool:
    expected = record["ends"]["windows"]
    target = identity.get("target_identity")
    return (
        identity.get("install_id") == expected["install_id"]
        and identity.get("config_digest") == expected["config_digest"]
        and identity.get("release") == expected["release"]
        and (not target_required or target == expected["target_identity"])
        and (target is None or target == expected["target_identity"])
    )


def _zero_fields(document, names: tuple[str, ...]) -> bool:
    return isinstance(document, dict) and all(
        type(document.get(name)) is int and document[name] == 0 for name in names
    )


def _windows_safety_matches(safety, *, require_jobs: bool) -> bool:
    if not isinstance(safety, dict):
        return False
    jobs = safety.get("jobs")
    if require_jobs:
        if not _zero_fields(jobs, ("active", "queued", "unresolved")):
            return False
    elif jobs is not None:
        return False
    cad = safety.get("cad")
    listener_pid = safety.get("listener_pid")
    return (
        isinstance(cad, dict)
        and cad.get("external_session_detected") is False
        and type(cad.get("owned_sessions")) is int
        and cad["owned_sessions"] == 0
        and cad.get("lease_held") is False
        and type(cad.get("solidworks_processes")) is int
        and cad["solidworks_processes"] == 0
        and (
            (type(listener_pid) is int and listener_pid > 0)
            if require_jobs
            else listener_pid is None
        )
    )


def _windows_stopped_is_empty(stopped) -> bool:
    return _zero_fields(
        stopped,
        ("listener_count", "writer_count", "solidworks_process_count"),
    )


def verify_windows_result(
    document,
    *,
    remote_rc: int,
    verb: str,
    record_path: Path | None,
) -> int:
    """Validate one Windows lifecycle result; return the operatorctl exit code."""

    if not isinstance(document, dict) or document.get("verb") != verb:
        return 3
    if (
        remote_rc in (0, 1)
        and document.get("passed") is False
        and isinstance(document.get("code"), str)
        and document["code"]
    ):
        return 1
    if remote_rc != 0 or document.get("passed") is not True:
        return 3
    if document.get("schema_version") != WINDOWS_RESULT_SCHEMA:
        return 1

    identity = document.get("identity")
    task = document.get("task")
    if not isinstance(identity, dict) or not isinstance(task, dict):
        return 1

    record = None
    if record_path is not None:
        try:
            loaded_record = json.loads(Path(record_path).read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return 3
        try:
            interface_records = _canonical_records()
            record = interface_records.validate_install_record(loaded_record)
        except (Exception, SystemExit):
            return 3

    if verb == "status":
        status = document.get("status")
        task_state = status.get("task_state") if isinstance(status, dict) else None
        if (
            not isinstance(status, dict)
            or status.get("consistent") is not True
            or status.get("error") is not None
            or status.get("task_exists") is not True
            or type(status.get("solidworks_processes")) is not int
            or status["solidworks_processes"] < 0
            or task_state not in {"Disabled", "Ready", "Running"}
            or task.get("state") != task_state
        ):
            return 1
        if task_state == "Running":
            if record is None or not isinstance(document.get("listener"), dict) or not document["listener"]:
                return 1
            try:
                interface_records.require_endpoint_matches_install_record(document.get("health"), record)
            except Exception:
                return 1
            return 0 if _windows_identity_matches(record, identity, target_required=True) else 1
        if document.get("listener") is not None or document.get("health") is not None:
            return 1
        if record is None:
            return 5
        return 0 if _windows_identity_matches(record, identity, target_required=False) else 1

    if record is None:
        return 3
    if verb in {"start", "health"}:
        if task.get("state") != "Running":
            return 1
        listener = document.get("listener")
        if not isinstance(listener, dict) or not listener:
            return 1
        try:
            interface_records.require_endpoint_matches_install_record(document.get("health"), record)
        except Exception:
            return 1
        return 0 if _windows_identity_matches(record, identity, target_required=True) else 1
    if verb == "drain":
        health = document.get("health")
        try:
            interface_records.require_endpoint_matches_install_record(
                health,
                record,
                require_ready=False,
            )
        except Exception:
            return 1
        listener = document.get("listener")
        safety = document.get("safety")
        if (
            task.get("state") != "Running"
            or not isinstance(listener, dict)
            or not listener
            or not _windows_safety_matches(safety, require_jobs=True)
            or safety.get("task_state") != "Running"
        ):
            return 1
        return 0 if _windows_identity_matches(record, identity, target_required=True) else 1
    if verb == "stop":
        safety = document.get("safety")
        safety_task_state = safety.get("task_state") if isinstance(safety, dict) else None
        if (
            task.get("state") != "Disabled"
            or document.get("listener") is not None
            or document.get("health") is not None
            or safety_task_state not in {"Disabled", "Ready", "Running"}
            or not _windows_safety_matches(
                safety,
                require_jobs=safety_task_state == "Running",
            )
            or not _windows_stopped_is_empty(document.get("stopped"))
        ):
            return 1
        return 0 if _windows_identity_matches(record, identity, target_required=False) else 1
    return 3


def load_windows_result(path: Path):
    """Read the final JSON line emitted by the Windows lifecycle transport."""

    try:
        return json.loads(Path(path).read_text(encoding="utf-8").strip().splitlines()[-1])
    except (OSError, TypeError, ValueError, IndexError):
        return None


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


def stage_release_wheel(
    release: dict,
    *,
    release_root: Path,
    wheel: Path | None,
    tree_root: Path | None = None,
) -> Path:
    """Copy the verified wheel to the runtime artifact root and return that stable path."""

    require(release_root.is_absolute(), "runtime release root must be absolute")
    source = wheel_binding(release, wheel=wheel, tree_root=tree_root)
    stable = release_root / "artifacts" / source.name
    atomic_write_once(stable, source.read_bytes())
    return wheel_binding(release, wheel=stable)


def extract_linux_wheelhouse(release_file: Path, destination: Path) -> None:
    """Materialize only wheels from the manifest-bound Linux role archive."""

    release = release_identity(_load_json(release_file, "release file"))
    name = f"description-{release['version']}-linux-cp312-x86_64.zip"
    artifacts = {row["name"]: row["sha256"] for row in release["artifacts"]}
    require(name in artifacts, "release.json has no Linux role archive")
    source = _confined_artifact(release_file.parent, name)
    require(source is not None, "the Linux role archive is missing")
    payload = source.read_bytes()
    require(hashlib.sha256(payload).hexdigest() == artifacts[name], "Linux role archive differs from release.json")
    require(destination.is_dir() and not _is_link(destination) and not any(destination.iterdir()),
            "wheelhouse destination must be an empty ordinary directory")
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        names = archive.namelist()
        require(len(names) == len(set(names)), "Linux role archive repeats a member")
        wheels = []
        for member in names:
            confined_relative(member, "Linux role member")
            parts = member.split("/")
            if len(parts) == 2 and parts[0] == "wheels" and parts[1].endswith(".whl"):
                wheels.append(member)
        require(bool(wheels), "Linux role archive contains no wheels")
        for member in wheels:
            atomic_write_once(destination / member.split("/")[1], archive.read(member))


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


def write_prepared_record(path: Path, record: dict, owner) -> Path:
    """Stage the exact record bytes without publishing an active installation."""

    path = Path(path)
    require(path.is_absolute(), "prepared record path must be absolute")
    require(not path.exists() and not path.is_symlink(), f"prepared record already exists: {path}")
    return atomic_write_once(path, owner.canonical(record))


def activate_prepared_record(record_file: Path, state_root: Path, wheel: Path) -> tuple[Path, Path]:
    """Validate and atomically publish one record after all external bindings succeed."""

    record_file = Path(record_file)
    require(
        record_file.is_file() and not _is_link(record_file),
        f"prepared record must be a regular non-link file: {record_file}",
    )
    require(
        (record_file.stat().st_mode & 0o777) == 0o600,
        f"prepared record must be mode 0600: {record_file}",
    )
    owner = load_contract_owner(extract_contract_closure(Path(wheel)))
    payload = record_file.read_bytes()
    try:
        records = _canonical_records()
        record = records.validate_install_record(json.loads(payload.decode("utf-8")))
    except Exception as error:
        raise SystemExit(f"install_record: prepared install record is invalid: {error}") from error
    require(
        payload == owner.canonical(record),
        "prepared install record is not in canonical byte form",
    )
    state_root = Path(state_root)
    require(state_root.is_absolute(), "prepared install state root must be absolute")
    require(
        record["paths"].get("state_root") == str(state_root),
        "prepared install record belongs to another state root",
    )
    snapshot = Path(record["snapshot"]["path"])
    require(
        snapshot.is_file() and not _is_link(snapshot),
        f"prepared record snapshot must be a regular non-link file: {snapshot}",
    )
    snapshot_bytes = snapshot.read_bytes()
    require(
        hashlib.sha256(snapshot_bytes).hexdigest() == record["snapshot"]["digest"],
        "prepared record snapshot digest differs",
    )
    try:
        snapshot_document = json.loads(snapshot_bytes.decode("utf-8"))
        records.validate_config_snapshot(snapshot_document)
    except Exception as error:
        raise SystemExit(f"install_record: prepared record snapshot is invalid: {error}") from error
    require(
        snapshot_document["install_id"] == record["install_id"],
        "prepared record snapshot names another install generation",
    )
    require(
        snapshot_document["resolved"]["storage"]["state_root"] == str(state_root),
        "prepared record snapshot belongs to another state root",
    )
    wheel_binding(record["release"], wheel=Path(wheel))
    return write_install_record(record, state_root, owner)


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
    build.add_argument(
        "--prepared-record",
        type=Path,
        default=None,
        help="stage the record here without publishing it",
    )
    activate = subparsers.add_parser("activate")
    activate.add_argument("--record-file", type=Path, required=True)
    activate.add_argument("--state-root", type=Path, required=True)
    activate.add_argument("--wheel", type=Path, required=True)
    check = subparsers.add_parser("check")
    check.add_argument("--release-file", type=Path, required=True)
    check.add_argument("--wheel", type=Path, default=None, help="the wheel that will be installed")
    check.add_argument("--tree", type=Path, default=None, help="deployment tree for wheel discovery")
    verify_deployment = subparsers.add_parser("verify-deployment")
    verify_deployment.add_argument("--release-file", type=Path, required=True)
    verify_deployment.add_argument("--tree", type=Path, required=True)
    stage = subparsers.add_parser("stage-wheel")
    stage.add_argument("--release-file", type=Path, required=True)
    stage.add_argument("--release-root", type=Path, required=True)
    stage.add_argument("--wheel", type=Path, default=None)
    stage.add_argument("--tree", type=Path, default=None)
    wheelhouse = subparsers.add_parser("extract-linux-wheelhouse")
    wheelhouse.add_argument("--release-file", type=Path, required=True)
    wheelhouse.add_argument("--destination", type=Path, required=True)
    verify = subparsers.add_parser("verify-active")
    verify.add_argument("--config", type=Path, required=True)
    verify.add_argument("--state-root", type=Path, required=True)
    verify.add_argument("--release-file", type=Path)
    identity = subparsers.add_parser("endpoint-identity")
    identity.add_argument("--status", type=Path, required=True)
    identity.add_argument("--release-file", type=Path, required=True)
    identity.add_argument("--out", type=Path, required=True)
    windows_result = subparsers.add_parser("verify-windows-result")
    windows_result.add_argument("--result", type=Path, required=True)
    windows_result.add_argument("--remote-rc", type=int, required=True)
    windows_result.add_argument("--verb", required=True)
    windows_result.add_argument("--install-record", type=Path)
    args = parser.parse_args(argv)

    if args.action == "extract-linux-wheelhouse":
        extract_linux_wheelhouse(args.release_file, args.destination)
        return 0
    if args.action == "verify-windows-result":
        document = load_windows_result(args.result)
        if document is None:
            return 3
        code = verify_windows_result(
            document,
            remote_rc=args.remote_rc,
            verb=args.verb,
            record_path=args.install_record,
        )
        if code == 5:
            args.result.with_name("windows-status.json").write_text(
                json.dumps(document, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        return code
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
    if args.action == "stage-wheel":
        release = release_identity(_load_json(args.release_file, "release file"))
        stable = stage_release_wheel(
            release,
            release_root=args.release_root,
            wheel=args.wheel,
            tree_root=args.tree,
        )
        print(stable)
        return 0
    if args.action == "activate":
        generation, active = activate_prepared_record(
            args.record_file,
            args.state_root,
            args.wheel,
        )
        print(f"generation={generation}")
        print(f"active={active}")
        return 0
    if args.action == "check":
        release_document = _load_json(args.release_file, "release file")
        release = release_identity(release_document)
        verified = verify_artifacts(args.release_file.parent, release["artifacts"])
        deployment_members = verify_deployment_tree(args.release_file.parent, release_document)
        if args.wheel is not None or args.tree is not None:
            wheel_binding(release, wheel=args.wheel, tree_root=args.tree)
        print("check=ok")
        print(f"verified_artifacts={verified}")
        print(f"verified_deployment_members={deployment_members}")
        return 0
    if args.action == "verify-deployment":
        release_document = _load_json(args.release_file, "release file")
        deployment_members = verify_deployment_tree(args.tree, release_document)
        print(f"verified_deployment_members={deployment_members}")
        return 0

    timestamp = args.timestamp or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    release_document = _load_json(args.release_file, "release file")
    release = release_identity(release_document)
    verified_deployment_members = verify_deployment_tree(args.deployment_root, release_document)
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
    if args.prepared_record is not None:
        prepared = write_prepared_record(args.prepared_record, record, contract)
        print(f"prepared={prepared}")
        print(f"record_digest={hashlib.sha256(contract.canonical(record)).hexdigest()}")
        print(f"verified_artifacts={verified}")
        print(f"verified_deployment_members={verified_deployment_members}")
        return 0
    generation, active = write_install_record(record, args.state_root, contract)
    print(f"generation={generation}")
    print(f"active={active}")
    print(f"record_digest={hashlib.sha256(contract.canonical(record)).hexdigest()}")
    print(f"verified_artifacts={verified}")
    print(f"verified_deployment_members={verified_deployment_members}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
