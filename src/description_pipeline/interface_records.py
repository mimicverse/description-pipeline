"""Machine schemas for records crossing the platform / endpoint boundary.

Single owner module for the W1 frozen interfaces (frozen-interfaces-v1.1):
resource policy (IF-04), endpoint health (IF-09), cache-release request,
import confirmation and immutable receipt (IF-10), connection config identity
(IF-11), config snapshot and install record (IF-16), plus the delivery-tree
records ``manifest.json`` and ``input/source.json`` (IF-13).

Every validator checks exact key sets, types and the frozen semantic rules and
raises :class:`InterfaceRecordError` with a stable machine code.  Validation is
static: it never probes services, ports or the network (probes belong to
``status`` / ``health`` / ``start``); the one explicit filesystem helper is
``check_config_identity_file`` for the consuming host.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
from pathlib import Path, PurePosixPath

from .delivery import PIPELINE_ID
from .io import PipelineError, artifact_path_parts, digest
from .schemas import (
    CACHE_IMPORT_CONFIRMATION_SCHEMA,
    CACHE_RELEASE_RECEIPT_SCHEMA,
    CACHE_RELEASE_SCHEMA,
    CONFIG_SNAPSHOT_SCHEMA,
    DELIVERY_MANIFEST_SCHEMA,
    HEALTH_SCHEMA,
    INSTALL_RECORD_SCHEMA,
    RESOURCE_POLICY_SCHEMA,
    SOURCE_RECORD_SCHEMA,
)
from .stages import CONTRACT, STAGE_IDS


class InterfaceRecordError(PipelineError):
    """A machine record failed its frozen schema; ``code`` is stable."""

    def __init__(self, code: str, detail: dict | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail or {}


_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_RFC3339_UTC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")

RELEASE_ROLES = ("native", "portable")
#: Secret-like keys are refused anywhere in a snapshot or install record.
#: File references (``*_file``) stay allowed: only identities and paths travel.
_SECRET_KEYS = frozenset(
    {"token", "password", "secret", "app_secret", "private_key", "client_secret", "access_token", "refresh_token"}
)

POLICY_LIMIT_KEYS = ("max_files", "max_file_bytes", "max_total_bytes", "max_unpacked_bytes", "max_upload_bytes")
#: Timeout key set, final for W1 (owner B): each is a positive number of seconds.
POLICY_TIMEOUT_KEYS = ("upload_seconds", "unpack_seconds", "admission_seconds", "job_seconds")
POLICY_CAPACITY_KEYS = ("min_free_bytes",)

HEALTH_KEYS = (
    "health_schema",
    "pipeline_id",
    "native_platform",
    "ready",
    "release",
    "contract",
    "resource_policy",
    "install",
    "auth",
    "endpoint",
    "cad",
    "jobs",
    "capacity",
    "readiness",
)

CONFIG_IDENTITY_ERRORS = (
    "config_identity_invalid",
    "config_identity_unreadable",
    "config_identity_mismatch",
    "config_identity_not_active",
    "config_identity_legacy",
)


def _require(condition, code: str, detail: dict | None = None) -> None:
    if not condition:
        raise InterfaceRecordError(code, detail)


def _exact_keys(record, keys, code: str, where: str) -> None:
    _require(isinstance(record, dict), code, {"where": where, "reason": "not an object"})
    missing = sorted(set(keys) - set(record))
    unknown = sorted(set(record) - set(keys))
    _require(not missing and not unknown, code, {"where": where, "missing": missing, "unknown": unknown})


def _text(value, code: str, where: str) -> str:
    _require(isinstance(value, str) and bool(value) and not _CONTROL.search(value), code, {"where": where})
    return value


def _hex64(value, code: str, where: str) -> str:
    _require(isinstance(value, str) and bool(_HEX64.match(value)), code, {"where": where})
    return value


def _positive_int(value, code: str, where: str) -> int:
    _require(type(value) is int and value > 0, code, {"where": where})
    return value


def _non_negative_int(value, code: str, where: str) -> int:
    _require(type(value) is int and value >= 0, code, {"where": where})
    return value


def _utc(value, code: str, where: str) -> str:
    _require(isinstance(value, str) and bool(_RFC3339_UTC.match(value)), code, {"where": where})
    return value


def _reject_secrets(record, *, code: str) -> None:
    stack = [("", record)]
    while stack:
        path, value = stack.pop()
        if isinstance(value, dict):
            for key, item in value.items():
                _require(
                    not (isinstance(key, str) and key.lower() in _SECRET_KEYS), code, {"secret_key": f"{path}/{key}"}
                )
                stack.append((f"{path}/{key}", item))
        elif isinstance(value, list):
            stack.extend((f"{path}/{index}", item) for index, item in enumerate(value))


def _check_release_triple(block, code: str, where: str) -> None:
    _exact_keys(block, {"version", "source_commit", "source_sha256"}, code, where)
    _text(block["version"], code, f"{where}.version")
    _require(
        isinstance(block["source_commit"], str) and bool(_COMMIT.match(block["source_commit"])),
        code,
        {"where": f"{where}.source_commit"},
    )
    _hex64(block["source_sha256"], code, f"{where}.source_sha256")


def validate_release_identity(record, *, role: str | None = None, code: str = "release_identity_invalid") -> dict:
    """The common release identity block: ``version`` + ``source_commit`` + ``source_sha256``.

    Both ends compare the full triple: equal package digests with a different
    ``source_commit`` are a mismatch, never a pass.
    """

    keys = {"version", "source_commit", "source_sha256"} | ({"role"} if role is not None else set())
    _exact_keys(record, keys, code, "release")
    _check_release_triple({key: record[key] for key in ("version", "source_commit", "source_sha256")}, code, "release")
    if role is not None:
        _require(record["role"] == role, code, {"where": "release.role", "expected": role})
    return record


def require_same_release_identity(left, right, *, code: str = "release_identity_mismatch") -> None:
    """Reject any triple divergence, including equal digests with different commits."""

    fields = ("version", "source_commit", "source_sha256")
    differing = [field for field in fields if left.get(field) != right.get(field)]
    _require(
        not differing,
        code,
        {
            "fields": differing,
            "left": {field: left.get(field) for field in fields},
            "right": {field: right.get(field) for field in fields},
        },
    )


def validate_call_binding(binding) -> dict:
    """Side-effecting calls bind operation id, parameter digest, release and contract."""

    code = "call_binding_invalid"
    _exact_keys(binding, {"operation_id", "params_sha256", "release", "contract"}, code, "binding")
    _text(binding["operation_id"], code, "binding.operation_id")
    _hex64(binding["params_sha256"], code, "binding.params_sha256")
    validate_release_identity(binding["release"], code=code)
    _exact_keys(binding["contract"], {"schema_version", "digest"}, code, "binding.contract")
    _text(binding["contract"]["schema_version"], code, "binding.contract.schema_version")
    _hex64(binding["contract"]["digest"], code, "binding.contract.digest")
    return binding


def validate_resource_policy(policy) -> dict:
    """``solidworks-to-urdf.resource-policy`` (IF-04): limits, timeouts, capacity."""

    code = "resource_policy_invalid"
    _exact_keys(policy, {"schema_version", "limits", "timeouts", "capacity"}, code, "policy")
    _require(policy["schema_version"] == RESOURCE_POLICY_SCHEMA, code, {"where": "schema_version"})
    _exact_keys(policy["limits"], POLICY_LIMIT_KEYS, code, "limits")
    for key in POLICY_LIMIT_KEYS:
        _positive_int(policy["limits"][key], code, f"limits.{key}")
    _require(
        policy["limits"]["max_upload_bytes"] >= policy["limits"]["max_total_bytes"],
        code,
        {"rule": "max_upload_bytes >= max_total_bytes", "reason": "request body includes multipart overhead"},
    )
    _exact_keys(policy["timeouts"], POLICY_TIMEOUT_KEYS, code, "timeouts")
    for key in POLICY_TIMEOUT_KEYS:
        value = policy["timeouts"][key]
        _require(
            isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0,
            code,
            {"where": f"timeouts.{key}"},
        )
    _exact_keys(policy["capacity"], POLICY_CAPACITY_KEYS, code, "capacity")
    _positive_int(policy["capacity"]["min_free_bytes"], code, "capacity.min_free_bytes")
    return policy


def resource_policy_digest(policy) -> str:
    """Canonical digest carried in requests and compared by ``/health``."""

    return digest(validate_resource_policy(policy))


def validate_health(payload) -> dict:
    """Endpoint ``/health`` v1 (IF-09): confirmed identities only, no secrets."""

    code = "health_invalid"
    _exact_keys(payload, HEALTH_KEYS, code, "health")
    _require(payload["health_schema"] == HEALTH_SCHEMA, code, {"where": "health_schema"})
    _require(payload["pipeline_id"] == PIPELINE_ID, code, {"where": "pipeline_id"})
    _require(type(payload["native_platform"]) is bool, code, {"where": "native_platform"})
    _require(type(payload["ready"]) is bool, code, {"where": "ready"})
    validate_release_identity(payload["release"], role="native", code=code)
    _exact_keys(payload["contract"], {"schema_version", "digest"}, code, "contract")
    _require(
        payload["contract"]["schema_version"] == CONTRACT["schema_version"], code, {"where": "contract.schema_version"}
    )
    _hex64(payload["contract"]["digest"], code, "contract.digest")
    _exact_keys(payload["resource_policy"], {"schema_version", "digest"}, code, "resource_policy")
    _require(
        payload["resource_policy"]["schema_version"] == RESOURCE_POLICY_SCHEMA,
        code,
        {"where": "resource_policy.schema_version"},
    )
    _hex64(payload["resource_policy"]["digest"], code, "resource_policy.digest")
    _exact_keys(payload["install"], {"config_digest", "install_id"}, code, "install")
    _hex64(payload["install"]["config_digest"], code, "install.config_digest")
    _text(payload["install"]["install_id"], code, "install.install_id")
    _exact_keys(payload["auth"], {"scheme", "token_fingerprint"}, code, "auth")
    _require(payload["auth"]["scheme"] == "bearer", code, {"where": "auth.scheme"})
    _text(payload["auth"]["token_fingerprint"], code, "auth.token_fingerprint")
    _exact_keys(payload["endpoint"], {"port"}, code, "endpoint")
    port = payload["endpoint"]["port"]
    _require(type(port) is int and 0 < port < 65536, code, {"where": "endpoint.port"})
    _exact_keys(payload["cad"], {"external_session_detected", "owned_sessions", "lease_held"}, code, "cad")
    _require(
        type(payload["cad"]["external_session_detected"]) is bool, code, {"where": "cad.external_session_detected"}
    )
    _require(type(payload["cad"]["lease_held"]) is bool, code, {"where": "cad.lease_held"})
    _non_negative_int(payload["cad"]["owned_sessions"], code, "cad.owned_sessions")
    _exact_keys(payload["jobs"], {"active", "queued", "unresolved"}, code, "jobs")
    for key in ("active", "queued", "unresolved"):
        _non_negative_int(payload["jobs"][key], code, f"jobs.{key}")
    _exact_keys(payload["capacity"], {"free_bytes", "reserved_bytes"}, code, "capacity")
    _non_negative_int(payload["capacity"]["free_bytes"], code, "capacity.free_bytes")
    _non_negative_int(payload["capacity"]["reserved_bytes"], code, "capacity.reserved_bytes")
    _exact_keys(payload["readiness"], {"reason_code", "native"}, code, "readiness")
    _exact_keys(payload["readiness"]["native"], {"ok", "problems"}, code, "readiness.native")
    _require(type(payload["readiness"]["native"]["ok"]) is bool, code, {"where": "readiness.native.ok"})
    problems = payload["readiness"]["native"]["problems"]
    _require(
        isinstance(problems, list) and all(isinstance(item, str) and item for item in problems),
        code,
        {"where": "readiness.native.problems"},
    )
    reason = payload["readiness"]["reason_code"]
    if payload["ready"]:
        _require(reason in (None, ""), code, {"rule": "ready payload carries no reason_code"})
    else:
        _text(reason, code, "readiness.reason_code")
    _reject_secrets(payload, code=code)
    return payload


def cache_release_params_digest(import_block) -> str:
    """The parameter digest bound by a cache-release operation id."""

    validate_cache_import_block(import_block)
    return digest({"operation": "cache.release", "import": import_block})


def validate_cache_import_block(import_block) -> dict:
    code = "cache_release_invalid"
    _exact_keys(import_block, {"run_id", "manifest_sha256", "receipt_sha256"}, code, "import")
    _text(import_block["run_id"], code, "import.run_id")
    _hex64(import_block["manifest_sha256"], code, "import.manifest_sha256")
    _hex64(import_block["receipt_sha256"], code, "import.receipt_sha256")
    return import_block


def validate_cache_release_request(request) -> dict:
    """``POST /v1/jobs/<run_id>/cache/release`` request (IF-10)."""

    code = "cache_release_invalid"
    _exact_keys(request, {"schema_version", "binding", "import"}, code, "request")
    _require(request["schema_version"] == CACHE_RELEASE_SCHEMA, code, {"where": "schema_version"})
    validate_call_binding(request["binding"])
    validate_cache_import_block(request["import"])
    expected = cache_release_params_digest(request["import"])
    _require(
        request["binding"]["params_sha256"] == expected,
        "cache_release_params_mismatch",
        {"expected": expected, "actual": request["binding"]["params_sha256"]},
    )
    return request


def validate_cache_import_confirmation(record) -> dict:
    """The Linux-side confirmation that a full import landed before cache release."""

    code = "cache_import_confirmation_invalid"
    _exact_keys(
        record,
        {"schema_version", "run_id", "manifest_sha256", "receipt_sha256", "files", "bytes", "confirmed_at"},
        code,
        "confirmation",
    )
    _require(record["schema_version"] == CACHE_IMPORT_CONFIRMATION_SCHEMA, code, {"where": "schema_version"})
    _text(record["run_id"], code, "run_id")
    _hex64(record["manifest_sha256"], code, "manifest_sha256")
    _hex64(record["receipt_sha256"], code, "receipt_sha256")
    _non_negative_int(record["files"], code, "files")
    _non_negative_int(record["bytes"], code, "bytes")
    _utc(record["confirmed_at"], code, "confirmed_at")
    return record


def cache_release_receipt_digest(receipt) -> str:
    """Digest of the immutable receipt payload (the ``receipt_sha256`` field excluded)."""

    payload = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    return digest(payload)


def validate_cache_release_receipt(receipt) -> dict:
    """Immutable cache-release receipt (IF-10); any edit breaks ``receipt_sha256``."""

    code = "cache_release_receipt_invalid"
    _exact_keys(
        receipt,
        {"schema_version", "status", "released", "retained", "released_at", "identity", "receipt_sha256"},
        code,
        "receipt",
    )
    _require(receipt["schema_version"] == CACHE_RELEASE_RECEIPT_SCHEMA, code, {"where": "schema_version"})
    _require(receipt["status"] in {"released", "already_released"}, code, {"where": "status"})
    _exact_keys(receipt["released"], {"files", "bytes"}, code, "released")
    _non_negative_int(receipt["released"]["files"], code, "released.files")
    _non_negative_int(receipt["released"]["bytes"], code, "released.bytes")
    retained = receipt["retained"]
    _require(
        isinstance(retained, list) and all(isinstance(item, str) and item for item in retained),
        code,
        {"where": "retained"},
    )
    _utc(receipt["released_at"], code, "released_at")
    validate_release_identity(receipt["identity"], role="native", code=code)
    _hex64(receipt["receipt_sha256"], code, "receipt_sha256")
    expected = cache_release_receipt_digest(receipt)
    _require(receipt["receipt_sha256"] == expected, "cache_release_receipt_mismatch", {"expected": expected})
    return receipt


def validate_connection_config_identity(connection) -> dict:
    """C-supplied Airflow connection carrying exactly one config reference.

    ``legacy`` is raised for any surviving route copy (non-empty host/port/
    password or extra keys beside ``config_identity``); ``invalid`` for a
    malformed reference.  Returns the ``config_identity`` block.
    """

    _require(isinstance(connection, dict), "config_identity_invalid", {"where": "connection"})
    for key in ("host", "password"):
        if connection.get(key) not in ("", None):
            raise InterfaceRecordError("config_identity_legacy", {"field": key})
    if connection.get("port") is not None:
        raise InterfaceRecordError("config_identity_legacy", {"field": "port"})
    extra = connection.get("extra")
    _require(isinstance(extra, dict), "config_identity_invalid", {"where": "extra"})
    if set(extra) - {"config_identity"}:
        raise InterfaceRecordError("config_identity_legacy", {"unknown": sorted(set(extra) - {"config_identity"})})
    code = "config_identity_invalid"
    _require("config_identity" in extra, code, {"where": "extra.config_identity"})
    reference = extra["config_identity"]
    _exact_keys(reference, {"schema_version", "path", "sha256"}, code, "config_identity")
    _require(reference["schema_version"] == CONFIG_SNAPSHOT_SCHEMA, code, {"where": "config_identity.schema_version"})
    path = reference["path"]
    _require(isinstance(path, str) and path.startswith("/"), code, {"where": "config_identity.path"})
    parts = PurePosixPath(path).parts
    _require(bool(parts) and ".." not in parts, code, {"where": "config_identity.path"})
    _hex64(reference["sha256"], code, "config_identity.sha256")
    return reference


def check_config_identity_file(reference, *, allowed_root) -> bytes:
    """Consumer-side file checks: absolute, no links, 0600, inside the allowed root."""

    code = "config_identity_unreadable"
    root = Path(allowed_root)
    _require(root.is_absolute(), code, {"where": "allowed_root"})
    path = Path(reference["path"])
    _require(path.is_absolute(), code, {"where": "path"})
    _require(not path.is_symlink() and path.is_file(), code, {"where": "path"})
    _require(stat.S_IMODE(path.stat().st_mode) == 0o600, code, {"where": "mode", "expected": "0600"})
    real = Path(os.path.realpath(path))
    _require(real.is_relative_to(Path(os.path.realpath(root))), code, {"where": "allowed_root"})
    return path.read_bytes()


def verify_config_identity_digest(data: bytes, reference) -> None:
    """Fail closed on any digest divergence; no fallback to the current config."""

    actual = hashlib.sha256(data).hexdigest()
    _require(
        actual == reference["sha256"], "config_identity_mismatch", {"expected": reference["sha256"], "actual": actual}
    )


def require_active_config_identity(reference, install_record) -> None:
    """Admission check for NEW submissions only (IF-11).

    Sealed requests resolve their fixed ``{config_digest, resolved}`` record and
    their parameters stay unchanged on a connection switch; they must never be
    routed through this check or rejected with ``config_identity_not_active``.
    """

    validate_install_record(install_record)
    active = install_record["snapshot"]["digest"]
    _require(
        reference["sha256"] == active,
        "config_identity_not_active",
        {"reference": reference["sha256"], "active": active},
    )


def validate_config_snapshot(snapshot) -> dict:
    """Immutable generation snapshot (IF-16): references and digests, no secrets."""

    code = "config_snapshot_invalid"
    _exact_keys(snapshot, {"schema_version", "install_id", "generated_at", "resolved", "sources"}, code, "snapshot")
    _require(snapshot["schema_version"] == CONFIG_SNAPSHOT_SCHEMA, code, {"where": "schema_version"})
    _text(snapshot["install_id"], code, "install_id")
    _utc(snapshot["generated_at"], code, "generated_at")
    _exact_keys(
        snapshot["resolved"],
        {"operator", "storage", "windows", "execution", "retention", "targets"},
        code,
        "resolved",
    )
    _require(all(isinstance(value, dict) for value in snapshot["resolved"].values()), code, {"where": "resolved"})
    _exact_keys(snapshot["sources"], {"platform_json_sha256"}, code, "sources")
    _hex64(snapshot["sources"]["platform_json_sha256"], code, "sources.platform_json_sha256")
    _reject_secrets(snapshot, code=code)
    return snapshot


def config_snapshot_digest(snapshot) -> str:
    """Canonical digest of the snapshot bytes (the snapshot stores no self-digest)."""

    return digest(validate_config_snapshot(snapshot))


def validate_install_record(record, *, previous_required: bool = False) -> dict:
    """Linux-side active install record (IF-16).

    ``ends.linux.config_digest`` is only ever compared with this host;
    ``ends.windows.config_digest`` only with the endpoint ``/health.install``.
    The two ends are per-host generations, so this validator never requires the
    digests to be equal (or different) - cross-end equality checks are banned.
    """

    code = "install_record_invalid"
    _exact_keys(
        record,
        {
            "schema_version",
            "install_id",
            "timestamp",
            "snapshot",
            "release",
            "services",
            "paths",
            "dag",
            "ends",
            "previous",
            "journal",
        },
        code,
        "record",
    )
    _require(record["schema_version"] == INSTALL_RECORD_SCHEMA, code, {"where": "schema_version"})
    _text(record["install_id"], code, "install_id")
    _utc(record["timestamp"], code, "timestamp")
    _exact_keys(record["snapshot"], {"path", "digest"}, code, "snapshot")
    _text(record["snapshot"]["path"], code, "snapshot.path")
    _hex64(record["snapshot"]["digest"], code, "snapshot.digest")
    _exact_keys(record["release"], {"version", "source_commit", "source_sha256", "artifacts"}, code, "release")
    _check_release_triple(
        {key: record["release"][key] for key in ("version", "source_commit", "source_sha256")}, code, "release"
    )
    artifacts = record["release"]["artifacts"]
    _require(isinstance(artifacts, list) and artifacts, code, {"where": "release.artifacts"})
    for index, artifact in enumerate(artifacts):
        _exact_keys(artifact, {"name", "sha256"}, code, f"release.artifacts[{index}]")
        _text(artifact["name"], code, f"release.artifacts[{index}].name")
        _hex64(artifact["sha256"], code, f"release.artifacts[{index}].sha256")
    services = record["services"]
    _require(
        isinstance(services, list) and services and all(isinstance(item, str) and item for item in services),
        code,
        {"where": "services"},
    )
    paths = record["paths"]
    _require(
        isinstance(paths, dict)
        and paths
        and all(isinstance(key, str) and isinstance(value, str) and value for key, value in paths.items()),
        code,
        {"where": "paths"},
    )
    _exact_keys(record["dag"], {"id", "path", "digest", "tasks"}, code, "dag")
    _text(record["dag"]["id"], code, "dag.id")
    _text(record["dag"]["path"], code, "dag.path")
    _hex64(record["dag"]["digest"], code, "dag.digest")
    _require(record["dag"]["tasks"] == len(STAGE_IDS), code, {"where": "dag.tasks", "expected": len(STAGE_IDS)})
    _exact_keys(record["ends"], {"linux", "windows"}, code, "ends")
    for side, role in (("linux", "portable"), ("windows", "native")):
        end = record["ends"][side]
        _exact_keys(end, {"install_id", "config_digest", "release"}, code, f"ends.{side}")
        _text(end["install_id"], code, f"ends.{side}.install_id")
        _hex64(end["config_digest"], code, f"ends.{side}.config_digest")
        validate_release_identity(end["release"], role=role, code=code)
    require_same_release_identity(record["release"], record["ends"]["linux"]["release"])
    previous = record["previous"]
    if previous is None:
        _require(
            not previous_required,
            code,
            {"where": "previous", "reason": "second generation requires its rollback record"},
        )
    else:
        _exact_keys(previous, {"release", "config", "record"}, code, "previous")
        _require(isinstance(previous["release"], dict), code, {"where": "previous.release"})
        _check_release_triple(
            {key: previous["release"].get(key) for key in ("version", "source_commit", "source_sha256")},
            code,
            "previous.release",
        )
        _exact_keys(previous["config"], {"path", "digest"}, code, "previous.config")
        _text(previous["config"]["path"], code, "previous.config.path")
        _hex64(previous["config"]["digest"], code, "previous.config.digest")
        _text(previous["record"], code, "previous.record")
    journal = record["journal"]
    _require(isinstance(journal, list), code, {"where": "journal"})
    for index, step in enumerate(journal):
        _exact_keys(step, {"seq", "verb", "step", "status", "at", "evidence"}, code, f"journal[{index}]")
        _require(step["seq"] == index + 1, code, {"where": f"journal[{index}].seq"})
        _require(step["verb"] in {"promote", "rollback"}, code, {"where": f"journal[{index}].verb"})
        _text(step["step"], code, f"journal[{index}].step")
        _require(step["status"] in {"completed", "failed", "unknown"}, code, {"where": f"journal[{index}].status"})
        _utc(step["at"], code, f"journal[{index}].at")
        evidence = step["evidence"]
        _require(
            isinstance(evidence, list) and evidence and all(isinstance(item, str) and item for item in evidence),
            code,
            {"where": f"journal[{index}].evidence"},
        )
    _reject_secrets(record, code=code)
    return record


def validate_delivery_manifest(manifest) -> dict:
    """``manifest.json`` (IF-13): subject inventory, subject digest, identities."""

    code = "delivery_manifest_invalid"
    _exact_keys(manifest, {"schema_version", "subject", "contract", "policy"}, code, "manifest")
    _require(manifest["schema_version"] == DELIVERY_MANIFEST_SCHEMA, code, {"where": "schema_version"})
    _exact_keys(manifest["subject"], {"files", "digest"}, code, "subject")
    files = manifest["subject"]["files"]
    _require(isinstance(files, dict) and files, code, {"where": "subject.files"})
    for name, checksum in files.items():
        artifact_path_parts(name)
        _hex64(checksum, code, f"subject.files[{name}]")
    _require(sorted(files) == list(files), code, {"where": "subject.files", "reason": "sorted relative POSIX paths"})
    expected = digest(files)
    _require(manifest["subject"]["digest"] == expected, "delivery_manifest_subject_mismatch", {"expected": expected})
    _exact_keys(manifest["contract"], {"schema_version", "digest"}, code, "contract")
    _require(
        manifest["contract"]["schema_version"] == CONTRACT["schema_version"], code, {"where": "contract.schema_version"}
    )
    _hex64(manifest["contract"]["digest"], code, "contract.digest")
    _exact_keys(manifest["policy"], {"schema_version", "digest"}, code, "policy")
    _text(manifest["policy"]["schema_version"], code, "policy.schema_version")
    _hex64(manifest["policy"]["digest"], code, "policy.digest")
    return manifest


def validate_source_record(record) -> dict:
    """``input/source.json`` (IF-13/IF-06): content identity, selection, metadata."""

    code = "source_record_invalid"
    _exact_keys(record, {"schema_version", "source_revision_id", "identity", "selection", "metadata"}, code, "source")
    _require(record["schema_version"] == SOURCE_RECORD_SCHEMA, code, {"where": "schema_version"})
    identity = record["identity"]
    _exact_keys(
        identity,
        {"kind", "handoff_digest", "main_assembly", "configuration", "hardware_id", "records"},
        code,
        "identity",
    )
    _require(identity["kind"] == "source-revision", code, {"where": "identity.kind"})
    _hex64(identity["handoff_digest"], code, "identity.handoff_digest")
    _text(identity["main_assembly"], code, "identity.main_assembly")
    _text(identity["configuration"], code, "identity.configuration")
    _text(identity["hardware_id"], code, "identity.hardware_id")
    records = identity["records"]
    _require(isinstance(records, list), code, {"where": "identity.records"})
    references = []
    for index, entry in enumerate(records):
        _exact_keys(entry, {"reference", "file_sha256"}, code, f"identity.records[{index}]")
        references.append(_text(entry["reference"], code, f"identity.records[{index}].reference"))
        _hex64(entry["file_sha256"], code, f"identity.records[{index}].file_sha256")
    _require(
        references == sorted(set(references)),
        code,
        {"where": "identity.records", "reason": "unique, sorted by full reference"},
    )
    _require(record["source_revision_id"] == digest(identity), "source_record_identity_mismatch", {})
    selection = record["selection"]
    _exact_keys(selection, {"main_assembly", "configuration", "hardware_id", "hardware_source"}, code, "selection")
    for key in ("main_assembly", "configuration", "hardware_id"):
        _text(selection[key], code, f"selection.{key}")
        _require(
            selection[key] == identity[key],
            code,
            {"where": f"selection.{key}", "reason": "selection must match the content identity"},
        )
    _require(selection["hardware_source"] in {"cad", "platform-config"}, code, {"where": "selection.hardware_source"})
    metadata = record["metadata"]
    _exact_keys(metadata, {"revision", "owner", "control", "change_summary", "change_summary_source"}, code, "metadata")
    for key in ("revision", "owner", "change_summary"):
        _require(
            metadata[key] is None or (isinstance(metadata[key], str) and metadata[key]),
            code,
            {"where": f"metadata.{key}"},
        )
    _require(metadata["control"] is None or isinstance(metadata["control"], dict), code, {"where": "metadata.control"})
    _require(
        metadata["change_summary_source"] in {"cad", "submission", "generated"},
        code,
        {"where": "metadata.change_summary_source"},
    )
    return record
