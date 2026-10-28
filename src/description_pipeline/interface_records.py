"""Machine schemas for records crossing the platform / endpoint boundary.

Single owner module for the W1 frozen interfaces (frozen-interfaces-v1.1):
resource policy (IF-04), endpoint health (IF-09), connection config identity
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
import math
import os
import re
import stat
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath

from . import schemas as _schemas
from .delivery import PIPELINE_ID
from .io import PipelineError, artifact_path_parts, confined, digest, file_digest, parse_data
from .runtime import LINUX_GLIBC_BASELINE, RUNTIME_HOSTS, RUNTIME_VERSIONS, SUPPORTED_PYTHON, required_packages
from .schemas import (
    CHECK_POLICY_SCHEMA,
    CONFIG_SNAPSHOT_SCHEMA,
    DELIVERY_MANIFEST_SCHEMA,
    HEALTH_SCHEMA,
    INSTALL_RECORD_SCHEMA,
    PLATFORM_CONFIG_SCHEMA,
    QUALIFICATION_SCHEMA,
    RESOURCE_POLICY_SCHEMA,
    SOURCE_RECORD_SCHEMA,
)
from .stages import CONTRACT, CONTRACT_SHA256, FIXED_REQUIRED_CHECKS, HOST_BY_STAGE, HOST_IDS, STAGE_IDS

#: Every single-owner boundary schema id of this release (one enumeration, the
#: schema module stays the owner of the ids themselves).
SCHEMA_VERSIONS = frozenset(
    value for name, value in vars(_schemas).items() if name.endswith("_SCHEMA") and isinstance(value, str)
)

#: Every machine schema version this release knows: the single-owner schema ids
#: plus the stage contract.  Unknown versions are refused wherever a record
#: quotes one (call bindings, manifests, ...).
KNOWN_SCHEMA_VERSIONS = SCHEMA_VERSIONS | {CONTRACT["schema_version"]}

#: The shipped schema definition sources the schema digest covers: the model
#: document schema (structure and field rules of produced documents) and the two
#: owner modules whose machine key sets and field rules define every boundary
#: record (health, policies, snapshots, receipts, ...).  The digest is over these
#: definitions, never over the id strings.
SCHEMA_DEFINITION_FILES = (
    "model/schema.json",
    "schemas.py",
    "interface_records.py",
    "pipeline/contracts.py",
)


def schema_definition_digest(definition_root=None) -> str:
    """Canonical digest of the shipped schema definitions' actual content."""

    root = Path(definition_root) if definition_root is not None else Path(__file__).parent
    return digest({name: file_digest(root / name) for name in SCHEMA_DEFINITION_FILES})


#: Canonical digest of the schema definitions of this release (IF-15 release identity).
SCHEMA_DEFINITION_DIGEST = schema_definition_digest()


class InterfaceRecordError(PipelineError):
    """A machine record failed its frozen schema; ``code`` is stable."""

    def __init__(self, code: str, detail: dict | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail or {}


_HEX64 = re.compile(r"\A[0-9a-f]{64}\Z")
_COMMIT = re.compile(r"\A[0-9a-f]{40}\Z")
_RFC3339_UTC = re.compile(r"\A\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z\Z")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")

RELEASE_ROLES = ("native", "portable")
#: The frozen host/role pairing of the two ends, derived from the role hosts.
END_ROLES = tuple((RUNTIME_HOSTS[role]["host"], role) for role in RELEASE_ROLES)
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
    "schema_version",
    "pipeline_id",
    "native_platform",
    "ready",
    "release",
    "contract",
    "resource_policy",
    "install_id",
    "config_digest",
    "target_identity",
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
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise InterfaceRecordError(code, {"where": where, "reason": str(error)}) from error
    _require(
        parsed.tzinfo is not None and parsed.utcoffset() == timedelta(0),
        code,
        {"where": where, "reason": "UTC offset required"},
    )
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


def _require_triple_fields(block, code: str, where: str) -> None:
    """Require the three identity fields to be present and well formed (extra keys allowed)."""

    _require(isinstance(block, dict), code, {"where": where})
    missing = sorted({"version", "source_commit", "source_sha256"} - set(block))
    _require(not missing, code, {"where": where, "missing": missing})
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

    _require_triple_fields(left, code, "left")
    _require_triple_fields(right, code, "right")
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
    # Family-bound: the call contract must be the stage-contract family of this
    # release; other known schema ids (health, source, ...) are wrong families.
    _require(
        binding["contract"]["schema_version"] == CONTRACT["schema_version"],
        code,
        {"where": "binding.contract.schema_version", "expected": CONTRACT["schema_version"]},
    )
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
            isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0,
            code,
            {"where": f"timeouts.{key}"},
        )
    _exact_keys(policy["capacity"], POLICY_CAPACITY_KEYS, code, "capacity")
    _positive_int(policy["capacity"]["min_free_bytes"], code, "capacity.min_free_bytes")
    return policy


def resource_policy_digest(policy) -> str:
    """Canonical digest carried in requests and compared by ``/health``."""

    return digest(validate_resource_policy(policy))


#: Canonical IF-04 resource-policy instance (W1, owner B).  Every value is
#: adopted from an existing implementation constant - do not invent new ones.
RESOURCE_POLICY_V1 = {
    "schema_version": RESOURCE_POLICY_SCHEMA,
    "limits": {
        "max_files": 4096,  # uploads.py:61 MAX_FILES
        "max_file_bytes": 512 * 1024**2,  # uploads.py:63 MAX_FILE_BYTES
        "max_total_bytes": 2 * 1024**3,  # uploads.py:62 MAX_TOTAL_BYTES
        "max_unpacked_bytes": 16
        * 1024**3,  # handoff.py:16 MAX_HANDOFF_BYTES (native accept/unpack bound, windows.py:1522)
        "max_upload_bytes": 2 * 1024**3 + 4096 * 1024 + 65536,  # uploads.py:68-69 MAX_BODY_BYTES derivation
    },
    "timeouts": {
        # W1 defaults: no absolute timeout exists today, only per-request socket
        # deadlines.  Rationale is recorded per key.
        "upload_seconds": 3600,  # 2 GiB upload on a slow link
        "unpack_seconds": 900,  # bounded for the 16 GiB accept limit
        "admission_seconds": 300,  # path, inventory, and capacity admission checks
        "job_seconds": 86400,  # long native capture on the serial endpoint queue
    },
    "capacity": {
        "min_free_bytes": 2
        * 1024**3,  # uploads.py:71 DISK_RESERVE_FLOOR; the 0.05 fraction stays host-side (uploads.py:72)
    },
}
# Fail fast if a constant above drifts out of the frozen shape.
validate_resource_policy(RESOURCE_POLICY_V1)


def resource_policy_v1_digest() -> str:
    """Stable digest of the canonical instance (requests and ``/health`` compare it)."""

    return resource_policy_digest(RESOURCE_POLICY_V1)


#: Canonical check-strategy policy (single source for generation, verification and
#: publication/manifest): the fixed gate inventory plus the declarative per-object
#: expansion rules.  Consumers expand the same object; digests bind to it.
CHECK_POLICY_EXPANSIONS = (
    {"family": "physics.expected_mass", "selector": "expectation:expected_mass_kg"},
    {"family": "geometry.expected_extent", "selector": "expectation:expected_extent_m"},
    {"family": "inertia.{body}", "selector": "source.bodies[*].name"},
    {"family": "geometry.{body}", "selector": "source.bodies[*].name"},
    {"family": "joints.{joint}", "selector": "source.joints[*].name"},
    {"family": "shafts.{joint}", "selector": "source.joints[*].name", "when": "type != fixed"},
    {"family": "motion.{joint}", "selector": "source.joints[*].name", "when": "type != fixed"},
    {"family": "frames.{frame}", "selector": "source.frames[*].name"},
)
CHECK_POLICY_V1 = {
    "schema_version": CHECK_POLICY_SCHEMA,
    "fixed_checks": sorted(FIXED_REQUIRED_CHECKS),
    "dynamic_expansions": [dict(rule) for rule in CHECK_POLICY_EXPANSIONS],
    # The engineering-qualification facts (distinct from model-consistency facts):
    # per-joint range authority and drive sizing, plus the per-subject machine
    # qualification.  Only authoritative native/controlled evidence can establish
    # them; a passing consistency check never does.
    "engineering_facts": {
        "per_joint": ["engineering_position_range", "drive_sizing"],
        "per_subject": ["machine_qualification"],
    },
}


def validate_check_policy(policy) -> dict:
    """Shape of the frozen check strategy (fixed gates + expansion rules)."""

    code = "check_policy_invalid"
    _exact_keys(
        policy,
        {"schema_version", "fixed_checks", "dynamic_expansions", "engineering_facts"},
        code,
        "policy",
    )
    _require(policy["schema_version"] == CHECK_POLICY_SCHEMA, code, {"where": "schema_version"})
    fixed = policy["fixed_checks"]
    _require(
        isinstance(fixed, list)
        and fixed
        and all(isinstance(name, str) and name for name in fixed)
        and len(set(fixed)) == len(fixed),
        code,
        {"where": "fixed_checks"},
    )
    expansions = policy["dynamic_expansions"]
    _require(isinstance(expansions, list) and expansions, code, {"where": "dynamic_expansions"})
    for index, rule in enumerate(expansions):
        where = f"dynamic_expansions[{index}]"
        _require(isinstance(rule, dict) and set(rule) <= {"family", "selector", "when"}, code, {"where": where})
        for key in ("family", "selector"):
            _require(isinstance(rule.get(key), str) and rule[key], code, {"where": f"{where}.{key}"})
        if "when" in rule:
            _require(isinstance(rule["when"], str) and rule["when"], code, {"where": f"{where}.when"})
    facts = policy["engineering_facts"]
    _exact_keys(facts, {"per_joint", "per_subject"}, code, "engineering_facts")
    for key in ("per_joint", "per_subject"):
        values = facts[key]
        _require(
            isinstance(values, list)
            and values
            and all(isinstance(name, str) and name for name in values)
            and len(set(values)) == len(values),
            code,
            {"where": f"engineering_facts.{key}"},
        )
    return policy


validate_check_policy(CHECK_POLICY_V1)


def check_policy_digest() -> str:
    """Stable digest of the canonical check policy (manifests and reports bind it)."""

    return digest(validate_check_policy(CHECK_POLICY_V1))


# --- IF-14 qualification machine keys --------------------------------------------

QUALIFICATION_STATES = ("established", "unqualified", "out_of_scope")


def qualification_key(item) -> tuple:
    """The unique machine key of a qualification: strictly (fact_id, scope, object_id)."""

    return (item["fact_id"], item["scope"], item["object_id"])


def qualification_identity(item) -> str:
    """Canonical identity of a qualification record.

    Computed over the machine key only: status, evidence references and any
    display text never enter it, so a changed conclusion or new evidence keeps
    the qualification identity while a changed fact, subject or object does not.
    """

    fact_id, scope, object_id = qualification_key(item)
    return digest({"fact_id": fact_id, "scope": scope, "object_id": object_id})


def validate_qualification(item, *, resolve=None) -> dict:
    """One qualification record: policy fact, three-state conclusion, evidence.

    Evidence references must be delivered records with their exact digest; with
    a ``resolve`` root, a missing record or digest mismatch refuses the record -
    an ungrounded qualification is never downgraded to an unqualified one.
    """

    code = "qualification_invalid"
    _exact_keys(item, {"schema_version", "fact_id", "scope", "object_id", "status", "evidence"}, code, "qualification")
    _require(item["schema_version"] == QUALIFICATION_SCHEMA, code, {"where": "schema_version"})
    _require(isinstance(item["fact_id"], str) and item["fact_id"], code, {"where": "fact_id"})
    _require(isinstance(item["scope"], str), code, {"where": "scope"})
    _require(isinstance(item["object_id"], str), code, {"where": "object_id"})
    _require(item["status"] in QUALIFICATION_STATES, code, {"where": "status"})
    evidence = item["evidence"]
    _require(isinstance(evidence, list) and evidence, code, {"where": "evidence"})
    for index, reference in enumerate(evidence):
        where = f"evidence[{index}]"
        _exact_keys(reference, {"record", "sha256"}, code, where)
        _text(reference["record"], code, f"{where}.record")
        _hex64(reference["sha256"], code, f"{where}.sha256")
    if resolve is not None:
        for reference in evidence:
            path = confined(Path(resolve), reference["record"])
            _require(path.is_file(), code, {"evidence": reference["record"], "reason": "missing"})
            _require(
                file_digest(path) == reference["sha256"],
                code,
                {"evidence": reference["record"], "reason": "digest mismatch"},
            )
    return item


def qualification_facts(definition) -> list:
    """The policy-defined qualification facts of a rebuilt definition (IF-14).

    One entry per (fact, object) pair expanded from ``CHECK_POLICY_V1``: two
    objects of one fact stay two identities and never merge, identical keys are
    dropped so a fact is never recorded twice, and the policy's object filters
    (a fixed joint has no shaft fact) apply exactly as they do to the required
    checks.  The expectation facts are always present - an absent optional
    budget is representable as ``out_of_scope``.  ``check`` is the expanded gate
    whose outcome feeds the fact's conclusion; ``scope`` stays the model scope
    until a fact applies to a narrower instance scope.
    """

    facts: list = []
    seen = set()

    def add(fact_id, check, object_id=""):
        key = (fact_id, "", object_id)
        if key in seen:
            return
        seen.add(key)
        facts.append({"fact_id": fact_id, "scope": "", "object_id": object_id, "check": check})

    for identifier in CHECK_POLICY_V1["fixed_checks"]:
        add(identifier, identifier)
    source = definition.get("source") or {}
    for rule in CHECK_POLICY_V1["dynamic_expansions"]:
        family, selector = rule["family"], rule["selector"]
        if selector == "expectation:expected_mass_kg":
            add("physics.expected_mass", family)
        elif selector == "expectation:expected_extent_m":
            add("geometry.expected_extent", family)
        elif selector == "source.bodies[*].name":
            for body in source.get("bodies", []):
                add(family.split(".", 1)[0], family.format(body=body["name"]), body["name"])
        elif selector == "source.joints[*].name":
            for joint in source.get("joints", []):
                if rule.get("when") == "type != fixed" and joint["type"] == "fixed":
                    continue
                add(family.split(".", 1)[0], family.format(joint=joint["name"]), joint["name"])
        elif selector == "source.frames[*].name":
            for frame in source.get("frames", []):
                add(family.split(".", 1)[0], family.format(frame=frame["name"]), frame["name"])
        else:
            raise PipelineError(f"Unknown check-policy selector: {selector}")
    # The engineering facts (B family) are appended beside the check-driven
    # facts: no consistency gate feeds their conclusion, so ``check`` is None
    # and only authoritative evidence may establish them downstream.
    engineering = CHECK_POLICY_V1["engineering_facts"]
    for fact_id in engineering["per_joint"]:
        for joint in source.get("joints", []):
            if joint["type"] == "fixed":
                continue
            add(fact_id, None, joint["name"])
    for fact_id in engineering["per_subject"]:
        add(fact_id, None)
    return facts


def validate_health(payload) -> dict:
    """Endpoint ``/health`` v1 (IF-09): confirmed identities only, no secrets."""

    code = "health_invalid"
    _exact_keys(payload, HEALTH_KEYS, code, "health")
    _require(payload["schema_version"] == HEALTH_SCHEMA, code, {"where": "schema_version"})
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
    _text(payload["install_id"], code, "install_id")
    _hex64(payload["config_digest"], code, "config_digest")
    try:
        from .pipeline.contracts import NativeTargetIdentity  # noqa: PLC0415

        target = NativeTargetIdentity.from_record(payload["target_identity"])
    except Exception as error:
        raise InterfaceRecordError(
            code,
            {"where": "target_identity", "reason": str(error)},
        ) from error
    _require(
        target.to_record() == payload["target_identity"],
        code,
        {"where": "target_identity"},
    )
    _require(
        target.install_id == payload["install_id"],
        code,
        {"rule": "target_identity install_id equals health install_id"},
    )
    _require(
        target.pipeline_id == payload["pipeline_id"] == PIPELINE_ID,
        code,
        {"where": "target_identity.pipeline_id"},
    )
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
        # One ready semantics: proven absence of external sessions, no unresolved
        # operations and native readiness.  Owned active/queued work stays healthy;
        # admission/drain is judged separately by the admission window.
        _require(payload["native_platform"] is True, code, {"rule": "ready requires the native platform"})
        _require(payload["readiness"]["native"]["ok"] is True, code, {"rule": "ready requires native readiness ok"})
        _require(not problems, code, {"rule": "ready requires no native problems"})
        _require(
            payload["cad"]["external_session_detected"] is False,
            code,
            {"rule": "ready requires no external CAD session"},
        )
        _require(payload["jobs"]["unresolved"] == 0, code, {"rule": "ready requires no unresolved operations"})
    else:
        _text(reason, code, "readiness.reason_code")
    _reject_secrets(payload, code=code)
    return payload


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


PLATFORM_STORAGE_ROOTS = ("state_root", "handoff_root", "artifact_root", "workspace_root", "release_root")
_TARGET_KEY = re.compile(r"\A[A-Za-z][A-Za-z0-9_.-]{0,63}\Z")
_REPOSITORY_SLUG = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_BASE_BRANCH = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._/-]*\Z")
_SSH_HOST = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.-]*\Z")
_ORIGIN = re.compile(r"\Ahttps://([A-Za-z0-9][A-Za-z0-9.-]*)(?::([0-9]{1,5}))?\Z")
#: Isolated Windows target binding (design ruling D3): the task name and instance
#: directories the Linux lifecycle may address.  No fallback to a production task
#: or production path exists anywhere in the lifecycle.
_TASK_NAME = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
_WINDOWS_DRIVE = re.compile(r"\A[A-Za-z]:/")


def _absolute_path(value, code: str, where: str) -> PurePosixPath:
    _require(isinstance(value, str) and bool(value) and not _CONTROL.search(value), code, {"where": where})
    path = PurePosixPath(value)
    _require(path.is_absolute() and ".." not in path.parts, code, {"where": where})
    return path


def _windows_path(value, code: str, where: str) -> str:
    """Drive-absolute Windows directory: forward slashes, no traversal or empty segments."""

    _require(isinstance(value, str) and bool(value) and not _CONTROL.search(value), code, {"where": where})
    _require(bool(_WINDOWS_DRIVE.match(value)) and "\\" not in value, code, {"where": where})
    parts = value[3:].split("/")
    _require(all(part not in {"", ".", ".."} for part in parts), code, {"where": where})
    return value


def _within(child: PurePosixPath, parent: PurePosixPath) -> bool:
    return child == parent or parent in child.parents


def _string_list(value, code: str, where: str, *, allow_empty: bool) -> list:
    _require(isinstance(value, list), code, {"where": where})
    if not allow_empty:
        _require(bool(value), code, {"where": where})
    _require(
        all(isinstance(item, str) and item and item == item.strip() for item in value)
        and len(set(value)) == len(value),
        code,
        {"where": where},
    )
    return value


def validate_config_snapshot(snapshot) -> dict:
    """Immutable generation snapshot (IF-16): the full rendered platform tree, no secrets.

    Shape per ``docs/deployment.md`` configuration authority and C's authoritative field table
    (``config-snapshot-resolved-fields-C.md``): every level rejects unknown keys,
    paths are absolute without ``..`` (symlink/mode/readability checks belong to
    the consuming host), and configured roots must not overlap.  ``derived``
    carries the IF-11 consumables (store root, repository checkouts, discovery
    roots).  ``resolved.windows.instance`` binds the isolated Windows target
    (task name plus instance directories) so no observe/stop/restore path can
    address anything but the configured instance.
    """

    code = "config_snapshot_invalid"
    _exact_keys(snapshot, {"schema_version", "install_id", "generated_at", "resolved", "sources"}, code, "snapshot")
    _require(snapshot["schema_version"] == CONFIG_SNAPSHOT_SCHEMA, code, {"where": "schema_version"})
    _text(snapshot["install_id"], code, "install_id")
    _utc(snapshot["generated_at"], code, "generated_at")
    resolved = snapshot["resolved"]
    _exact_keys(
        resolved,
        {
            "schema_version",
            "operator",
            "storage",
            "database",
            "windows",
            "auth",
            "targets",
            "specifications",
            "retention",
            "derived",
        },
        code,
        "resolved",
    )
    _require(resolved["schema_version"] == PLATFORM_CONFIG_SCHEMA, code, {"where": "resolved.schema_version"})
    operator = resolved["operator"]
    _exact_keys(operator, {"origin", "certificate_file", "private_key_file"}, code, "resolved.operator")
    match = _ORIGIN.match(operator["origin"] if isinstance(operator["origin"], str) else "")
    _require(match is not None, code, {"where": "resolved.operator.origin"})
    origin_port = int(match.group(2)) if match.group(2) else 443
    _require(0 < origin_port < 65536, code, {"where": "resolved.operator.origin"})
    _absolute_path(operator["certificate_file"], code, "resolved.operator.certificate_file")
    _absolute_path(operator["private_key_file"], code, "resolved.operator.private_key_file")
    storage = resolved["storage"]
    _exact_keys(storage, set(PLATFORM_STORAGE_ROOTS), code, "resolved.storage")
    storage_roots = {
        name: _absolute_path(storage[name], code, f"resolved.storage.{name}") for name in PLATFORM_STORAGE_ROOTS
    }
    _exact_keys(resolved["database"], {"dsn_file"}, code, "resolved.database")
    _absolute_path(resolved["database"]["dsn_file"], code, "resolved.database.dsn_file")
    windows = resolved["windows"]
    _exact_keys(
        windows,
        {"ssh_host", "endpoint_port", "token_file", "tunnel_loopback_port", "instance"},
        code,
        "resolved.windows",
    )
    _require(
        isinstance(windows["ssh_host"], str) and bool(_SSH_HOST.match(windows["ssh_host"])),
        code,
        {"where": "resolved.windows.ssh_host"},
    )
    endpoint_port = windows["endpoint_port"]
    _require(
        type(endpoint_port) is int and 0 < endpoint_port < 65536, code, {"where": "resolved.windows.endpoint_port"}
    )
    tunnel_port = windows["tunnel_loopback_port"]
    _require(
        type(tunnel_port) is int and 0 < tunnel_port < 65536,
        code,
        {"where": "resolved.windows.tunnel_loopback_port"},
    )
    _require(origin_port != tunnel_port, code, {"rule": "origin port differs from tunnel_loopback_port"})
    _absolute_path(windows["token_file"], code, "resolved.windows.token_file")
    instance = windows["instance"]
    _exact_keys(instance, {"task_name", "package_root", "output_root", "state_root"}, code, "resolved.windows.instance")
    _require(
        isinstance(instance["task_name"], str) and bool(_TASK_NAME.match(instance["task_name"])),
        code,
        {"where": "resolved.windows.instance.task_name"},
    )
    for field in ("package_root", "output_root", "state_root"):
        _windows_path(instance[field], code, f"resolved.windows.instance.{field}")
    auth = resolved["auth"]
    _exact_keys(auth, {"feishu_secret_file", "tenant_keys", "admin_open_ids"}, code, "resolved.auth")
    _absolute_path(auth["feishu_secret_file"], code, "resolved.auth.feishu_secret_file")
    _string_list(auth["tenant_keys"], code, "resolved.auth.tenant_keys", allow_empty=False)
    _string_list(auth["admin_open_ids"], code, "resolved.auth.admin_open_ids", allow_empty=True)
    targets = resolved["targets"]
    _require(isinstance(targets, dict) and bool(targets), code, {"where": "resolved.targets"})
    slugs = set()
    for hardware, target in targets.items():
        _require(isinstance(hardware, str) and bool(_TARGET_KEY.match(hardware)), code, {"where": "resolved.targets"})
        _exact_keys(target, {"repository_slug", "base"}, code, f"resolved.targets.{hardware}")
        _require(
            isinstance(target["repository_slug"], str) and bool(_REPOSITORY_SLUG.match(target["repository_slug"])),
            code,
            {"where": f"resolved.targets.{hardware}.repository_slug"},
        )
        base = target["base"]
        _require(
            isinstance(base, str)
            and bool(_BASE_BRANCH.match(base))
            and ".." not in base
            and not base.endswith(".lock"),
            code,
            {"where": f"resolved.targets.{hardware}.base"},
        )
        slugs.add(target["repository_slug"])
    specifications = resolved["specifications"]
    _exact_keys(specifications, {"record_roots"}, code, "resolved.specifications")
    record_roots = specifications["record_roots"]
    _string_list(record_roots, code, "resolved.specifications.record_roots", allow_empty=False)
    record_paths = [
        _absolute_path(value, code, f"resolved.specifications.record_roots[{index}]")
        for index, value in enumerate(record_roots)
    ]
    retention = resolved["retention"]
    _exact_keys(retention, {"unreferenced_evidence_days"}, code, "resolved.retention")
    days = retention["unreferenced_evidence_days"]
    _require(
        days is None or (type(days) is int and days > 0),
        code,
        {"where": "resolved.retention.unreferenced_evidence_days"},
    )
    derived = resolved["derived"]
    _exact_keys(derived, {"repositories", "discovery"}, code, "resolved.derived")
    repositories = derived["repositories"]
    _require(
        isinstance(repositories, dict) and bool(repositories) and set(repositories) >= slugs,
        code,
        {"where": "resolved.derived.repositories", "rule": "covers every target repository slug"},
    )
    for slug, path in repositories.items():
        _require(
            isinstance(slug, str) and bool(_REPOSITORY_SLUG.match(slug)),
            code,
            {"where": "resolved.derived.repositories"},
        )
        _require(
            _within(
                _absolute_path(path, code, f"resolved.derived.repositories[{slug}]"), storage_roots["workspace_root"]
            ),
            code,
            {"rule": "repository checkouts live under workspace_root"},
        )
    discovery = derived["discovery"]
    _exact_keys(discovery, {"record_roots"}, code, "resolved.derived.discovery")
    _require(
        discovery["record_roots"] == record_roots,
        code,
        {"where": "resolved.derived.discovery.record_roots", "rule": "normalized copy of specifications.record_roots"},
    )
    named_roots = [(name, storage_roots[name]) for name in PLATFORM_STORAGE_ROOTS] + [
        (f"record_roots[{index}]", path) for index, path in enumerate(record_paths)
    ]
    for index, (left_name, left) in enumerate(named_roots):
        for right_name, right in named_roots[index + 1 :]:
            _require(
                not _within(left, right) and not _within(right, left),
                code,
                {"rule": "configured roots must not overlap", "left": left_name, "right": right_name},
            )
    _exact_keys(snapshot["sources"], {"platform_json_sha256"}, code, "sources")
    _hex64(snapshot["sources"]["platform_json_sha256"], code, "sources.platform_json_sha256")
    _reject_secrets(snapshot, code=code)
    return snapshot


def config_snapshot_digest(snapshot) -> str:
    """Canonical digest of the snapshot bytes (the snapshot stores no self-digest)."""

    return digest(validate_config_snapshot(snapshot))


def validate_install_record(record) -> dict:
    """Linux-side active install record (IF-16).

    ``ends.linux.config_digest`` is only ever compared with this host;
    ``ends.windows.config_digest`` only with the endpoint ``/health``.
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
        },
        code,
        "record",
    )
    _require(record["schema_version"] == INSTALL_RECORD_SCHEMA, code, {"where": "schema_version"})
    _text(record["install_id"], code, "install_id")
    _utc(record["timestamp"], code, "timestamp")
    _exact_keys(record["snapshot"], {"path", "digest", "install_id"}, code, "snapshot")
    _text(record["snapshot"]["path"], code, "snapshot.path")
    _hex64(record["snapshot"]["digest"], code, "snapshot.digest")
    _text(record["snapshot"]["install_id"], code, "snapshot.install_id")
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
    for side, role in END_ROLES:
        end = record["ends"][side]
        expected_end_keys = {"install_id", "config_digest", "release"}
        if side == "windows":
            expected_end_keys.add("target_identity")
        _exact_keys(end, expected_end_keys, code, f"ends.{side}")
        _text(end["install_id"], code, f"ends.{side}.install_id")
        _hex64(end["config_digest"], code, f"ends.{side}.config_digest")
        if side == "windows":
            target = end["target_identity"]
            try:
                from .pipeline.contracts import NativeTargetIdentity  # noqa: PLC0415

                normalized_target = NativeTargetIdentity.from_record(target).to_record()
            except Exception as error:
                raise InterfaceRecordError(
                    code,
                    {"where": "ends.windows.target_identity", "reason": str(error)},
                ) from error
            _require(
                normalized_target == target,
                code,
                {"where": "ends.windows.target_identity"},
            )
            _require(
                target["install_id"] == end["install_id"],
                code,
                {"rule": "windows runtime target install_id equals Windows end install_id"},
            )
            _require(
                target["pipeline_id"] == PIPELINE_ID,
                code,
                {"where": "ends.windows.target_identity.pipeline_id"},
            )
        validate_release_identity(end["release"], role=role, code=code)
    require_same_release_identity(record["release"], record["ends"]["linux"]["release"])
    # The common release identity spans both ends (same source release); the
    # per-end config digests and install generations stay per-end and are never
    # required to be equal (or different) here.
    require_same_release_identity(record["release"], record["ends"]["windows"]["release"])
    _require(
        record["install_id"] == record["snapshot"]["install_id"] == record["ends"]["linux"]["install_id"],
        code,
        {"rule": "install_id chain: record == snapshot == Linux end"},
    )
    _require(
        record["ends"]["linux"]["config_digest"] == record["snapshot"]["digest"],
        code,
        {"rule": "Linux end config digest equals the active snapshot digest"},
    )
    _reject_secrets(record, code=code)
    return record


def require_endpoint_matches_install_record(health, record, *, require_ready: bool = True) -> None:
    """Runtime-only cross-check of the Windows end against ``/health`` (IF-16).

    Static validation cannot know the endpoint; this helper is the single runtime admission /
    health owner and binds the complete installed identity: the per-end release triple (``role``
    is not compared), install id / config digest, the immutable snapshot's configured endpoint
    port, the contract and resource-policy digests to their single owners, and ``ready`` with
    every precondition ``validate_health`` enforces (external CAD session, unresolved
    operations, registered native platform) unless ``require_ready`` is false for a
    caller such as drain that owns a separate readiness decision. Cross-end config
    digest equality is never required.
    """

    validate_health(health)
    validate_install_record(record)
    end = record["ends"]["windows"]
    _require(
        health["pipeline_id"] == end["target_identity"]["pipeline_id"],
        "endpoint_end_mismatch",
        {"field": "pipeline_id"},
    )
    require_same_release_identity(end["release"], health["release"])
    _require(
        end["install_id"] == health["install_id"],
        "endpoint_end_mismatch",
        {"field": "install_id"},
    )
    _require(
        end["config_digest"] == health["config_digest"],
        "endpoint_end_mismatch",
        {"field": "config_digest"},
    )
    _require(
        end["target_identity"] == health["target_identity"],
        "endpoint_end_mismatch",
        {"field": "target_identity"},
    )
    if require_ready:
        _require(
            health["ready"] is True,
            "endpoint_not_ready",
            {"reason_code": health["readiness"]["reason_code"]},
        )
    # The snapshot is consumed as a config-identity reference: the existing IF-11 file checks
    # (absolute, no links, 0600, inside the record's state root), the recorded digest, and the
    # single strict IO on exactly those checked bytes - then the schema and the generation bind.
    reference = {
        "schema_version": CONFIG_SNAPSHOT_SCHEMA,
        "path": record["snapshot"]["path"],
        "sha256": record["snapshot"]["digest"],
    }
    try:
        payload = check_config_identity_file(reference, allowed_root=record["paths"]["state_root"])
        verify_config_identity_digest(payload, reference)
        snapshot = parse_data(payload, Path(reference["path"]))
    except InterfaceRecordError:
        raise
    except (OSError, ValueError, PipelineError) as error:
        raise InterfaceRecordError("endpoint_end_mismatch", {"where": "snapshot", "reason": str(error)}) from error
    validate_config_snapshot(snapshot)
    _require(
        snapshot["install_id"] == record["install_id"],
        "endpoint_end_mismatch",
        {"field": "snapshot.install_id"},
    )
    expected_port = snapshot["resolved"]["windows"]["endpoint_port"]
    _require(
        health["endpoint"]["port"] == expected_port,
        "endpoint_end_mismatch",
        {"field": "endpoint.port", "expected": expected_port},
    )
    _require(
        health["contract"]["digest"] == CONTRACT_SHA256,
        "endpoint_end_mismatch",
        {"field": "contract.digest"},
    )
    _require(
        health["resource_policy"]["digest"] == resource_policy_v1_digest(),
        "endpoint_end_mismatch",
        {"field": "resource_policy.digest"},
    )


def subject_file_records(files, code: str, where: str = "subject.files") -> tuple[dict[str, str], list[str]]:
    """Validate one ``{path, bytes, sha256}`` record list; the single inventory owner.

    Every record carries exactly path, byte length and SHA-256; paths are
    unique relative POSIX paths and the digests are 64-hex.  Returns the
    path->digest mapping and the paths in their recorded order.  Ordering
    policy belongs to the caller: the manifest requires sorted paths, the
    platform receipt accepts any order but must cover the same record set.
    """

    _require(isinstance(files, list) and files, code, {"where": where})
    mapping: dict[str, str] = {}
    paths: list[str] = []
    for index, record in enumerate(files):
        position = f"{where}[{index}]"
        _exact_keys(record, {"path", "bytes", "sha256"}, code, position)
        name = record["path"]
        _require(isinstance(name, str), code, {"where": f"{position}.path"})
        artifact_path_parts(name)
        _require(name not in mapping, code, {"where": f"{position}.path", "reason": "duplicate subject path"})
        _non_negative_int(record["bytes"], code, f"{position}.bytes")
        mapping[name] = _hex64(record["sha256"], code, f"{position}.sha256")
        paths.append(name)
    return mapping, paths


def validate_delivery_manifest(manifest) -> dict:
    """``manifest.json`` (IF-13): subject inventory, subject digest, identities."""

    code = "delivery_manifest_invalid"
    _exact_keys(manifest, {"schema_version", "subject", "contract", "policy"}, code, "manifest")
    _require(manifest["schema_version"] == DELIVERY_MANIFEST_SCHEMA, code, {"where": "schema_version"})
    _exact_keys(manifest["subject"], {"files", "digest"}, code, "subject")
    files = manifest["subject"]["files"]
    # Each record carries path, byte length and SHA-256 (contracts delivery format and digest);
    # the inventory owner enforces the shape and unique paths, the manifest
    # additionally requires sorted relative POSIX paths and bound bytes.
    mapping, paths = subject_file_records(files, code)
    _require(paths == sorted(paths), code, {"where": "subject.files", "reason": "sorted relative POSIX paths"})
    expected = digest(mapping)
    _require(manifest["subject"]["digest"] == expected, "delivery_manifest_subject_mismatch", {"expected": expected})
    _exact_keys(manifest["contract"], {"schema_version", "digest"}, code, "contract")
    _require(
        manifest["contract"]["schema_version"] == CONTRACT["schema_version"], code, {"where": "contract.schema_version"}
    )
    _hex64(manifest["contract"]["digest"], code, "contract.digest")
    _require(
        manifest["contract"]["digest"] == CONTRACT_SHA256,
        code,
        {"where": "contract.digest", "expected": CONTRACT_SHA256},
    )
    _exact_keys(manifest["policy"], {"schema_version", "digest"}, code, "policy")
    _require(
        manifest["policy"]["schema_version"] == CHECK_POLICY_SCHEMA,
        code,
        {"where": "policy.schema_version", "expected": CHECK_POLICY_SCHEMA},
    )
    _hex64(manifest["policy"]["digest"], code, "policy.digest")
    _require(
        manifest["policy"]["digest"] == check_policy_digest(),
        "delivery_manifest_policy_mismatch",
        {
            "rule": "the manifest policy is not allowed by this reader",
            "actual": manifest["policy"]["digest"],
            "expected": check_policy_digest(),
        },
    )
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


REQUEST_RECORD_SCHEMA = "solidworks-to-urdf.request/v2"

#: The only two purposes an admitted request may declare.  The frozen request
#: record is the single purpose authority; the freeze writer and every reader
#: carry the value explicitly and never default it.
PURPOSE_ENGINEERING = "engineering"
PURPOSE_INSPECTION_PREVIEW = "inspection_preview"
REQUEST_PURPOSES = (PURPOSE_ENGINEERING, PURPOSE_INSPECTION_PREVIEW)

_REQUEST_RECORD_KEYS = {
    "schema_version",
    "purpose",
    "request_id",
    "target_hardware",
    "change_summary",
    "main_assembly",
    "handoff_digest",
    "platform_config_digest",
}


def validate_request_record(record) -> dict:
    """``input/request.json``: the v2 freeze-formed request record, before the generator ran.

    One strict shape shared by the freeze writer and the offline verifier: all
    eight fields required, no unknown keys (the foundation reader additionally
    rejects duplicate keys and non-finite numbers while parsing), no null
    values, strict types - ``purpose`` is required and must be one of
    ``engineering``/``inspection_preview`` (a v1 record refuses on its version
    field: there is no lenient or mixed parsing) - ``request_id`` is non-empty
    text carried unchanged
    across reuse and never compared with the current run UUID,
    ``main_assembly`` is an engineering-relative portable path (the one
    ``artifact_path_parts`` rule; absolute, UNC and escaping paths refuse), and
    both digests are lowercase SHA-256.
    """

    code = "request_record_invalid"
    _exact_keys(record, _REQUEST_RECORD_KEYS, code, "request")
    _require(record["schema_version"] == REQUEST_RECORD_SCHEMA, code, {"where": "schema_version"})
    _require(
        isinstance(record["purpose"], str) and record["purpose"] in REQUEST_PURPOSES,
        code,
        {"where": "purpose", "values": list(REQUEST_PURPOSES)},
    )
    for key in ("request_id", "target_hardware"):
        value = _text(record[key], code, key)
        _require(value.strip() != "", code, {"where": key})
    main = _text(record["main_assembly"], code, "main_assembly")
    _require(main.strip() != "", code, {"where": "main_assembly"})
    try:
        artifact_path_parts(main)
    except PipelineError as error:
        raise InterfaceRecordError(code, {"where": "main_assembly", "reason": str(error)}) from error
    summary = record["change_summary"]
    _require(
        isinstance(summary, str)
        and summary.strip() != ""
        and not any((ord(char) < 32 and char != "\n") or ord(char) == 127 for char in summary),
        code,
        {"where": "change_summary"},
    )
    for key in ("handoff_digest", "platform_config_digest"):
        _hex64(record[key], code, key)
    return record


# --- IF-15 release identity: contract digests and the support matrix -------------


def release_contract() -> dict:
    """The contract identity every release manifest carries.

    Each value is the canonical digest of one single-owner machine definition:
    the shipped schema definitions (structure and field rules), the stage
    contract, the check policy and the resource policy.
    """

    return {
        "schema": SCHEMA_DEFINITION_DIGEST,
        "stage": CONTRACT_SHA256,
        "check_policy": check_policy_digest(),
        "resource_policy": resource_policy_v1_digest(),
    }


def release_support() -> dict:
    """The structural support matrix of this release (IF-15).

    It states what the release is built to support - operating system,
    architecture and interpreter range, the roles with their exact dependency
    closures and native API prerequisite, and the stages each host executes -
    and never a qualification: pass/fail claims belong to acceptance records,
    not to the release manifest.
    """

    return {
        "python": SUPPORTED_PYTHON,
        "roles": {
            role: {
                **RUNTIME_HOSTS[role],
                **({"glibc": f"2.{LINUX_GLIBC_BASELINE}"} if role == "portable" else {}),
                "packages": {name: RUNTIME_VERSIONS[name] for name in required_packages(role)},
            }
            for role in RELEASE_ROLES
        },
        "hosts": {host: [stage for stage in STAGE_IDS if host in HOST_BY_STAGE[stage]] for host in HOST_IDS},
    }


def release_domain_identity() -> dict:
    """The domain block of the release manifest: contract digests + support matrix."""

    return {"contract": release_contract(), "support": release_support()}


def record_archive_path(package_file: str) -> str:
    """Validate the canonical embedded-record path used by every archive reader."""

    from .io import PipelineError, artifact_path_parts

    if not (
        isinstance(package_file, str) and package_file == package_file.replace("\\", "/") and "//" not in package_file
    ):
        raise PipelineError("Controlled record path is not canonical POSIX text")
    artifact_path_parts(package_file)
    parts = package_file.split("/")
    if not (len(parts) == 2 and parts[0] == "records" and bool(parts[1]) and parts[1] not in {".", ".."}):
        raise PipelineError("Controlled record path must be records/<file>")
    return package_file
