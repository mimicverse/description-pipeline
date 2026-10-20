"""Positive/negative guards for the W1 machine records (frozen-interfaces-v1.1, b4-fix)."""

from __future__ import annotations

import ast
import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from description_pipeline import interface_records as ir
from description_pipeline.io import PipelineError, digest
from description_pipeline.schemas import (
    CACHE_IMPORT_CONFIRMATION_SCHEMA,
    CACHE_RELEASE_RECEIPT_SCHEMA,
    CHECK_POLICY_SCHEMA,
    CONFIG_SNAPSHOT_SCHEMA,
    DELIVERY_MANIFEST_SCHEMA,
    HEALTH_SCHEMA,
    INSTALL_RECORD_SCHEMA,
    PLATFORM_CONFIG_SCHEMA,
    RESOURCE_POLICY_SCHEMA,
    SOURCE_RECORD_SCHEMA,
)
from description_pipeline.stages import CONTRACT, CONTRACT_SHA256, STAGE_IDS

HEX = "a" * 64
OTHER_HEX = "b" * 64
COMMIT = "c" * 40
OTHER_COMMIT = "d" * 40
TS = "2026-10-13T09:00:00Z"
_SNAPSHOT_FILE = Path(tempfile.mkdtemp(prefix="ir-snapshot-")) / "gen-1.json"


def release(role: str | None = None) -> dict:
    block = {"version": "1.4.0", "source_commit": COMMIT, "source_sha256": HEX}
    if role is not None:
        block["role"] = role
    return block


def policy() -> dict:
    return {
        "schema_version": RESOURCE_POLICY_SCHEMA,
        "limits": {
            "max_files": 4096,
            "max_file_bytes": 512 * 1024**2,
            "max_total_bytes": 2 * 1024**3,
            "max_unpacked_bytes": 4 * 1024**3,
            "max_upload_bytes": 2 * 1024**3 + 65536,
        },
        "timeouts": {"upload_seconds": 3600, "unpack_seconds": 600, "admission_seconds": 60, "job_seconds": 86400},
        "capacity": {"min_free_bytes": 8 * 1024**3},
    }


def health(ready: bool = True) -> dict:
    return {
        "schema_version": HEALTH_SCHEMA,
        "pipeline_id": "solidworks-to-urdf",
        "native_platform": True,
        "ready": ready,
        "release": release("native"),
        "contract": {"schema_version": CONTRACT["schema_version"], "digest": CONTRACT_SHA256},
        "resource_policy": {"schema_version": RESOURCE_POLICY_SCHEMA, "digest": ir.resource_policy_v1_digest()},
        "install": {"config_digest": HEX, "install_id": "gen-1"},
        "endpoint": {"port": 8765},
        "cad": {"external_session_detected": False, "owned_sessions": 0, "lease_held": False},
        "jobs": {"active": 0, "queued": 0, "unresolved": 0},
        "capacity": {"free_bytes": 10**12, "reserved_bytes": 0},
        "readiness": {
            "reason_code": None if ready else "solidworks_unregistered",
            "native": {"ok": ready, "problems": [] if ready else ["solidworks_unregistered"]},
        },
    }


def production() -> dict:
    return {"run_id": "native-run-7", "operation_id": "native-op-42"}


def import_confirmation(*, production_block: dict | None = None) -> dict:
    confirmation = {
        "schema_version": CACHE_IMPORT_CONFIRMATION_SCHEMA,
        "imported": [
            {"path": "evidence/discovery.json", "bytes": 1, "sha256": HEX},
            {"path": "input/source.json", "bytes": 2, "sha256": OTHER_HEX},
        ],
        "production": production() if production_block is None else production_block,
        "sealed": {"confirmed_at": TS, "receipt_sha256": OTHER_HEX},
        "digest": "0" * 64,
    }
    confirmation["digest"] = ir.cache_import_confirmation_digest(confirmation)
    return confirmation


def import_block() -> dict:
    return {"linux_run_id": "linux-run-9", "production": production(), "confirmation": import_confirmation()}


def cache_request() -> dict:
    return {
        "operation_id": "release-op-1",
        "binding": {"release": release(), "contract": {"schema_version": CONTRACT["schema_version"], "digest": HEX}},
        "import": import_block(),
    }


def cache_receipt(*, status: str = "released", released_files: int = 12) -> dict:
    block = import_block()
    receipt = {
        "schema_version": CACHE_RELEASE_RECEIPT_SCHEMA,
        "status": status,
        "operation_id": "op-0001",
        "params_sha256": ir.cache_release_params_digest(block),
        "import": block,
        "released": {"files": released_files, "bytes": 4096},
        "retained": ["jobs/1/events.json", "artifacts/run-1"],
        "released_at": TS,
        "identity": release("native"),
        "receipt_sha256": "0" * 64,
    }
    receipt["receipt_sha256"] = ir.cache_release_receipt_digest(receipt)
    return receipt


def config_snapshot() -> dict:
    return {
        "schema_version": CONFIG_SNAPSHOT_SCHEMA,
        "install_id": "gen-1",
        "generated_at": TS,
        "resolved": {
            "schema_version": PLATFORM_CONFIG_SCHEMA,
            "operator": {
                "origin": "https://operator.example.com:8443",
                "certificate_file": "/srv/description/secrets/tls.crt",
                "private_key_file": "/srv/description/secrets/tls.key",
            },
            "storage": {
                "state_root": "/srv/description/state",
                "handoff_root": "/srv/description/handoffs",
                "artifact_root": "/srv/description/artifacts",
                "workspace_root": "/srv/description/workspaces",
                "release_root": "/srv/description/releases",
            },
            "database": {"dsn_file": "/srv/description/secrets/postgres.dsn"},
            "windows": {
                "ssh_host": "solidworks-worker",
                "endpoint_port": 8765,
                "token_file": "/srv/description/secrets/endpoint.token",
                "tunnel_loopback_port": 18765,
                "instance": {
                    "task_name": "description-e2e-endpoint",
                    "package_root": "C:/description-e2e/packages",
                    "output_root": "C:/description-e2e/results",
                    "state_root": "C:/description-e2e/state",
                },
            },
            "auth": {
                "feishu_secret_file": "/srv/description/secrets/feishu.json",
                "tenant_keys": ["tenant-key"],
                "admin_open_ids": [],
            },
            "targets": {"arm": {"repository_slug": "owner/model-repository", "base": "main"}},
            "specifications": {"record_roots": ["/srv/description/specifications"]},
            "retention": {"unreferenced_evidence_days": None},
            "derived": {
                "store_root": "/srv/description/state/runs",
                "repositories": {"owner/model-repository": "/srv/description/workspaces/model-repository"},
                "discovery": {"record_roots": ["/srv/description/specifications"]},
            },
        },
        "sources": {"platform_json_sha256": HEX},
    }


def install_record(*, previous: bool = False, journal: list | None = None) -> dict:
    generation = "gen-2" if previous else "gen-1"
    _SNAPSHOT_FILE.write_text(json.dumps(config_snapshot()), encoding="utf-8")
    _SNAPSHOT_FILE.chmod(0o600)
    snapshot_digest = hashlib.sha256(_SNAPSHOT_FILE.read_bytes()).hexdigest()
    record = {
        "schema_version": INSTALL_RECORD_SCHEMA,
        "install_id": generation,
        "timestamp": TS,
        "snapshot": {
            "path": str(_SNAPSHOT_FILE),
            "digest": snapshot_digest,
            "install_id": generation,
        },
        "release": {**release(), "artifacts": [{"name": "linux.tar.gz", "sha256": OTHER_HEX}]},
        "services": ["description-postgres", "description-operator-proxy"],
        "paths": {"state_root": str(_SNAPSHOT_FILE.parent), "release_root": "/srv/description/releases"},
        "dag": {
            "id": "solidworks_to_urdf",
            "path": "/srv/description/dags/solidworks_to_urdf.py",
            "digest": HEX,
            "tasks": len(STAGE_IDS),
        },
        "ends": {
            "linux": {"install_id": generation, "config_digest": snapshot_digest, "release": release("portable")},
            "windows": {"install_id": "ep-7", "config_digest": OTHER_HEX, "release": release("native")},
        },
        "previous": None
        if not previous
        else {
            "release": release(),
            "config": {"path": "/srv/description/state/snapshots/gen-1.json", "digest": OTHER_HEX},
            "record": "/srv/description/state/install-record-gen-1.json",
        },
        "journal": journal
        if journal is not None
        else [
            {
                "seq": 1,
                "verb": "promote",
                "step": "admission_close",
                "status": "completed",
                "at": TS,
                "evidence": ["receipts/promote-1.json"],
            }
        ],
    }
    return record


def source_record() -> dict:
    identity = {
        "kind": "source-revision",
        "handoff_digest": HEX,
        "main_assembly": "3.0 总装1008.SLDASM",
        "configuration": "Default",
        "hardware_id": "m3.0",
        "records": [{"reference": "components/waist/r2.json#drive", "file_sha256": OTHER_HEX}],
    }
    return {
        "schema_version": SOURCE_RECORD_SCHEMA,
        "source_revision_id": digest(identity),
        "identity": identity,
        "selection": {
            "main_assembly": identity["main_assembly"],
            "configuration": "Default",
            "hardware_id": "m3.0",
            "hardware_source": "cad",
        },
        "metadata": {
            "revision": "A.3",
            "owner": "结构组",
            "control": {"system": "pdm"},
            "change_summary": "首版",
            "change_summary_source": "cad",
        },
    }


def manifest() -> dict:
    mapping = {"evidence/discovery.json": HEX, "input/handoff.json": OTHER_HEX, "tool.json": HEX}
    files = [{"path": path, "bytes": 1, "sha256": checksum} for path, checksum in sorted(mapping.items())]
    return {
        "schema_version": DELIVERY_MANIFEST_SCHEMA,
        "subject": {"files": files, "digest": digest(mapping)},
        "contract": {"schema_version": CONTRACT["schema_version"], "digest": HEX},
        "policy": {"schema_version": CHECK_POLICY_SCHEMA, "digest": ir.check_policy_digest()},
    }


class B4FixSingleVariableTests(unittest.TestCase):
    """Acceptance form: a complete legal positive first, then exactly one change per negative.

    Each negative asserts the stable error code and that validation has no side
    effect (the candidate is unchanged; the untouched positive still passes).
    """

    def assert_rejected_by_one_change(self, validator, positive, mutate, code: str) -> None:
        validator(copy.deepcopy(positive))
        candidate = copy.deepcopy(positive)
        mutate(candidate)
        frozen = copy.deepcopy(candidate)
        with self.assertRaises(ir.InterfaceRecordError) as raised:
            validator(candidate)
        self.assertEqual(raised.exception.code, code)
        self.assertEqual(candidate, frozen)
        validator(copy.deepcopy(positive))

    def test_legal_positives_are_accepted(self) -> None:
        from description_pipeline.orchestration import uploads

        ir.validate_health(health(True))
        ir.validate_health(health(False))
        ir.validate_config_snapshot(config_snapshot())
        ir.validate_delivery_manifest(manifest())
        ir.validate_cache_release_request(cache_request())
        ir.validate_cache_release_receipt(cache_receipt())
        ir.validate_install_record(install_record(previous=True), previous_required=True)
        ir.validate_resource_policy(ir.RESOURCE_POLICY_V1)
        self.assertEqual(ir.RESOURCE_POLICY_V1["limits"]["max_upload_bytes"], uploads.MAX_BODY_BYTES)
        self.assertEqual(
            ir.resource_policy_v1_digest(), "d728fd27524e835c427d72f1463385814872778f5f0be30820e5d649fc8a45f8"
        )

    def test_health_single_variable_negatives(self) -> None:
        positive = health(True)
        cases = (
            (
                "ready with native.ok=false",
                lambda h: h["readiness"]["native"].__setitem__("ok", False),
                "health_invalid",
            ),
            (
                "ready with native problems",
                lambda h: h["readiness"]["native"]["problems"].append("x"),
                "health_invalid",
            ),
            (
                "ready with external CAD",
                lambda h: h["cad"].__setitem__("external_session_detected", True),
                "health_invalid",
            ),
            ("ready with unresolved jobs", lambda h: h["jobs"].__setitem__("unresolved", 1), "health_invalid"),
            ("ready without native platform", lambda h: h.__setitem__("native_platform", False), "health_invalid"),
            (
                "old health_schema field",
                lambda h: h.__setitem__("health_schema", h.pop("schema_version")),
                "health_invalid",
            ),
            (
                "re-added auth fingerprint",
                lambda h: h.__setitem__("auth", {"scheme": "bearer", "token_fingerprint": "3e1d"}),
                "health_invalid",
            ),
        )
        for label, mutate, code in cases:
            with self.subTest(label):
                self.assert_rejected_by_one_change(ir.validate_health, positive, mutate, code)
        unready = health(False)
        self.assert_rejected_by_one_change(
            ir.validate_health, unready, lambda h: h["readiness"].__setitem__("reason_code", None), "health_invalid"
        )

    def test_config_snapshot_single_variable_negatives(self) -> None:
        positive = config_snapshot()
        cases = (
            ("missing database", lambda c: c["resolved"].pop("database"), "config_snapshot_invalid"),
            ("missing auth", lambda c: c["resolved"].pop("auth"), "config_snapshot_invalid"),
            ("missing specifications", lambda c: c["resolved"].pop("specifications"), "config_snapshot_invalid"),
            ("empty operator object", lambda c: c["resolved"].__setitem__("operator", {}), "config_snapshot_invalid"),
            (
                "empty tenant_keys",
                lambda c: c["resolved"]["auth"].__setitem__("tenant_keys", []),
                "config_snapshot_invalid",
            ),
            (
                "string endpoint port",
                lambda c: c["resolved"]["windows"].__setitem__("endpoint_port", "8765"),
                "config_snapshot_invalid",
            ),
            (
                "missing windows instance",
                lambda c: c["resolved"]["windows"].pop("instance"),
                "config_snapshot_invalid",
            ),
            (
                "windows task name with space",
                lambda c: c["resolved"]["windows"]["instance"].__setitem__("task_name", "e2e endpoint"),
                "config_snapshot_invalid",
            ),
            (
                "relative windows package root",
                lambda c: c["resolved"]["windows"]["instance"].__setitem__("package_root", "packages"),
                "config_snapshot_invalid",
            ),
            (
                "windows state root traversal",
                lambda c: c["resolved"]["windows"]["instance"].__setitem__("state_root", "C:/e2e/../state"),
                "config_snapshot_invalid",
            ),
            (
                "origin port collision",
                lambda c: c["resolved"]["windows"].__setitem__("tunnel_loopback_port", 8443),
                "config_snapshot_invalid",
            ),
            (
                "relative dsn path",
                lambda c: c["resolved"]["database"].__setitem__("dsn_file", "secrets/postgres.dsn"),
                "config_snapshot_invalid",
            ),
            ("empty targets", lambda c: c["resolved"].__setitem__("targets", {}), "config_snapshot_invalid"),
            (
                "base starts with dash",
                lambda c: c["resolved"]["targets"]["arm"].__setitem__("base", "-x"),
                "config_snapshot_invalid",
            ),
            (
                "repositories missing slug",
                lambda c: c["resolved"]["derived"]["repositories"].clear(),
                "config_snapshot_invalid",
            ),
            (
                "overlapping storage roots",
                lambda c: c["resolved"]["storage"].__setitem__("release_root", "/srv/description/artifacts"),
                "config_snapshot_invalid",
            ),
            (
                "impossible calendar date",
                lambda c: c.__setitem__("generated_at", "2026-99-99T99:99:99Z"),
                "config_snapshot_invalid",
            ),
            (
                "inline secret value",
                lambda c: c["resolved"]["windows"].__setitem__("token", "inline"),
                "config_snapshot_invalid",
            ),
        )
        for label, mutate, code in cases:
            with self.subTest(label):
                self.assert_rejected_by_one_change(ir.validate_config_snapshot, positive, mutate, code)

    def test_manifest_and_cache_single_variable_negatives(self) -> None:
        manifest_positive = manifest()
        manifest_cases = (
            ("file record without bytes", lambda m: m["subject"]["files"][0].pop("bytes"), "delivery_manifest_invalid"),
            (
                "duplicate subject path",
                lambda m: m["subject"]["files"][1].__setitem__("path", m["subject"]["files"][0]["path"]),
                "delivery_manifest_invalid",
            ),
            (
                "subject digest changed",
                lambda m: m["subject"].__setitem__("digest", OTHER_HEX),
                "delivery_manifest_subject_mismatch",
            ),
            (
                "mapping instead of records",
                lambda m: m["subject"].__setitem__("files", {r["path"]: r["sha256"] for r in m["subject"]["files"]}),
                "delivery_manifest_invalid",
            ),
        )
        for label, mutate, code in manifest_cases:
            with self.subTest(label):
                self.assert_rejected_by_one_change(ir.validate_delivery_manifest, manifest_positive, mutate, code)
        request_positive = cache_request()
        request_cases = (
            (
                "confirmation reduced to counts",
                lambda r: r.__setitem__(
                    "import",
                    {
                        **r["import"],
                        "confirmation": {
                            "schema_version": CACHE_IMPORT_CONFIRMATION_SCHEMA,
                            "run_id": "linux-run-9",
                            "files": 12,
                            "bytes": 4096,
                            "confirmed_at": TS,
                        },
                    },
                ),
                "cache_import_confirmation_invalid",
            ),
            (
                "unknown contract version",
                lambda r: r["binding"]["contract"].__setitem__("schema_version", "unknown.contract/v99"),
                "cache_release_invalid",
            ),
            (
                "release operation equals production operation",
                lambda r: r.__setitem__("operation_id", r["import"]["production"]["operation_id"]),
                "cache_release_invalid",
            ),
            (
                "confirmation digest not recomputed",
                lambda r: r["import"]["confirmation"].__setitem__("digest", OTHER_HEX),
                "cache_import_confirmation_invalid",
            ),
        )
        for label, mutate, code in request_cases:
            with self.subTest(label):
                self.assert_rejected_by_one_change(ir.validate_cache_release_request, request_positive, mutate, code)
        receipt_positive = cache_receipt()
        receipt_cases = (
            (
                "claimed release deleted nothing",
                lambda r: r.__setitem__("status", "released") or r["released"].__setitem__("files", 0),
                "cache_release_receipt_invalid",
            ),
            (
                "receipt tampered after signing",
                lambda r: r["released"].__setitem__("bytes", 1),
                "cache_release_receipt_mismatch",
            ),
            ("receipt without operation import binding", lambda r: r.pop("import"), "cache_release_receipt_invalid"),
        )
        for label, mutate, code in receipt_cases:
            with self.subTest(label):
                self.assert_rejected_by_one_change(ir.validate_cache_release_receipt, receipt_positive, mutate, code)

    def test_install_and_release_single_variable_negatives(self) -> None:
        positive = install_record(previous=True)
        cases = (
            (
                "windows release commit differs",
                lambda r: r["ends"]["windows"]["release"].__setitem__("source_commit", OTHER_COMMIT),
                "release_identity_mismatch",
            ),
            (
                "linux digest differs from snapshot",
                lambda r: r["ends"]["linux"].__setitem__("config_digest", OTHER_HEX),
                "install_record_invalid",
            ),
            (
                "linux generation differs from record",
                lambda r: r["ends"]["linux"].__setitem__("install_id", "other"),
                "install_record_invalid",
            ),
            (
                "snapshot generation differs from record",
                lambda r: r["snapshot"].__setitem__("install_id", "other"),
                "install_record_invalid",
            ),
            (
                "journal step without evidence",
                lambda r: r["journal"][0].__setitem__("evidence", []),
                "install_record_invalid",
            ),
        )
        for label, mutate, code in cases:
            with self.subTest(label):
                self.assert_rejected_by_one_change(ir.validate_install_record, positive, mutate, code)
        release_positive = release()
        for label, mutate in (
            ("sha256 trailing newline", lambda r: r.__setitem__("source_sha256", r["source_sha256"] + "\n")),
            ("commit trailing newline", lambda r: r.__setitem__("source_commit", r["source_commit"] + "\n")),
        ):
            with self.subTest(label):
                self.assert_rejected_by_one_change(
                    ir.validate_release_identity, release_positive, mutate, "release_identity_invalid"
                )
        pair_positive = (release(), release())
        with self.subTest("missing vs present release identity"):
            left, right = copy.deepcopy(pair_positive)
            with self.assertRaises(ir.InterfaceRecordError) as raised:
                ir.require_same_release_identity({}, right)
            self.assertEqual(raised.exception.code, "release_identity_mismatch")
            self.assertEqual(left, release())
            ir.require_same_release_identity(*pair_positive)
        with self.subTest("equal digest different commit"):
            left, right = copy.deepcopy(pair_positive)
            right["source_commit"] = OTHER_COMMIT
            with self.assertRaises(ir.InterfaceRecordError) as raised:
                ir.require_same_release_identity(left, right)
            self.assertEqual(raised.exception.code, "release_identity_mismatch")


class InterfaceRecordTests(unittest.TestCase):
    def reject(self, code: str, mutate) -> None:
        with self.assertRaises(ir.InterfaceRecordError) as raised:
            mutate()
        self.assertEqual(raised.exception.code, code)

    def test_release_identity_triple_and_strict_digests(self) -> None:
        left, right = release(), release()
        ir.require_same_release_identity(left, right)
        self.reject("release_identity_mismatch", lambda: ir.require_same_release_identity({}, {}))
        right["source_commit"] = OTHER_COMMIT
        self.reject("release_identity_mismatch", lambda: ir.require_same_release_identity(left, right))
        for mutated in (HEX + "\n", "z" * 64):
            bad = release()
            bad["source_sha256"] = mutated
            self.reject("release_identity_invalid", lambda bad=bad: ir.validate_release_identity(bad))
        bad = release()
        bad["source_commit"] = COMMIT + "\n"
        self.reject("release_identity_invalid", lambda: ir.validate_release_identity(bad))
        ir.validate_release_identity(release("native"), role="native")

    def test_resource_policy_positive_and_negative(self) -> None:
        policy_value = policy()
        ir.validate_resource_policy(policy_value)
        self.assertEqual(len(ir.resource_policy_digest(policy_value)), 64)
        bad = copy.deepcopy(policy_value)
        bad["timeouts"]["job_seconds"] = float("inf")
        self.reject("resource_policy_invalid", lambda: ir.validate_resource_policy(bad))
        bad = copy.deepcopy(policy_value)
        bad["limits"]["max_upload_bytes"] = bad["limits"]["max_total_bytes"] - 1
        self.reject("resource_policy_invalid", lambda: ir.validate_resource_policy(bad))
        bad = copy.deepcopy(policy_value)
        bad["capacity"]["cache_root"] = "/srv/cache"
        self.reject("resource_policy_invalid", lambda: ir.validate_resource_policy(bad))

    def test_resource_policy_v1_adopts_existing_constants(self) -> None:
        from description_pipeline.orchestration import uploads

        ir.validate_resource_policy(ir.RESOURCE_POLICY_V1)
        limits = ir.RESOURCE_POLICY_V1["limits"]
        self.assertEqual(limits["max_files"], uploads.MAX_FILES)
        self.assertEqual(limits["max_total_bytes"], uploads.MAX_TOTAL_BYTES)
        self.assertEqual(limits["max_file_bytes"], uploads.MAX_FILE_BYTES)
        self.assertEqual(limits["max_upload_bytes"], uploads.MAX_BODY_BYTES)
        self.assertGreaterEqual(limits["max_upload_bytes"], limits["max_total_bytes"])
        self.assertEqual(ir.RESOURCE_POLICY_V1["capacity"]["min_free_bytes"], uploads.DISK_RESERVE_FLOOR)
        # Pinned digest: changing any adopted value intentionally breaks this guard.
        self.assertEqual(
            ir.resource_policy_v1_digest(), "d728fd27524e835c427d72f1463385814872778f5f0be30820e5d649fc8a45f8"
        )

    def test_check_policy_is_the_single_source_for_required_gates(self) -> None:
        from description_pipeline.stages import FIXED_REQUIRED_CHECKS
        from description_pipeline.verification.solidworks_urdf import required_checks

        ir.validate_check_policy(ir.CHECK_POLICY_V1)
        self.assertEqual(ir.CHECK_POLICY_V1["fixed_checks"], sorted(FIXED_REQUIRED_CHECKS))
        self.assertEqual(set(required_checks()), set(FIXED_REQUIRED_CHECKS))
        # Pinned digest: generation, verification and the manifest consume this object.
        self.assertEqual(
            ir.check_policy_digest(),
            "6e067c427ce4b3abfde0b14886411bdbcd8f392d920acd7ae1ed153f32dd0022",
            "the check policy changed; the digest is re-registered",
        )
        bad = copy.deepcopy(ir.CHECK_POLICY_V1)
        bad.pop("fixed_checks")
        self.reject("check_policy_invalid", lambda: ir.validate_check_policy(bad))
        bad = copy.deepcopy(ir.CHECK_POLICY_V1)
        bad["dynamic_expansions"][0]["unknown"] = True
        self.reject("check_policy_invalid", lambda: ir.validate_check_policy(bad))

    def test_health_ready_is_single_semantics(self) -> None:
        ir.validate_health(health(True))
        ir.validate_health(health(False))
        busy = health(True)
        busy["jobs"]["active"] = 2
        busy["jobs"]["queued"] = 1
        ir.validate_health(busy)  # owned active/queued work stays healthy; drain is judged separately
        bad = health(False)
        bad["readiness"]["reason_code"] = None
        self.reject("health_invalid", lambda: ir.validate_health(bad))
        contradictions = (
            lambda value: value["readiness"]["native"].__setitem__("ok", False),
            lambda value: value["readiness"]["native"]["problems"].append("solidworks_unregistered"),
            lambda value: value["cad"].__setitem__("external_session_detected", True),
            lambda value: value["jobs"].__setitem__("unresolved", 1),
            lambda value: value.__setitem__("native_platform", False),
        )
        for mutate in contradictions:
            bad = health(True)
            mutate(bad)
            self.reject("health_invalid", lambda bad=bad: ir.validate_health(bad))
        bad = health(True)
        bad["release"]["role"] = "portable"
        self.reject("health_invalid", lambda: ir.validate_health(bad))
        bad = health(True)
        bad["endpoint"]["port"] = 70000
        self.reject("health_invalid", lambda: ir.validate_health(bad))
        bad = health(True)
        bad["auth"] = {"scheme": "bearer"}
        self.reject("health_invalid", lambda: ir.validate_health(bad))

    def test_obsolete_positive_fixtures_are_rejected(self) -> None:
        """Design probe parity: old-shape positives must fail the final schemas."""
        obsolete_health = health(True)
        obsolete_health["health_schema"] = obsolete_health.pop("schema_version")
        obsolete_health["auth"] = {"scheme": "bearer", "token_fingerprint": "3e1d"}
        self.reject("health_invalid", lambda: ir.validate_health(obsolete_health))
        mapping_only = manifest()
        mapping_only["subject"]["files"] = {
            record["path"]: record["sha256"] for record in mapping_only["subject"]["files"]
        }
        self.reject("delivery_manifest_invalid", lambda: ir.validate_delivery_manifest(mapping_only))
        all_empty = config_snapshot()
        all_empty["resolved"] = {key: {} for key in all_empty["resolved"]}
        self.reject("config_snapshot_invalid", lambda: ir.validate_config_snapshot(all_empty))
        counts_only = cache_request()
        counts_only["import"]["confirmation"] = {
            "schema_version": CACHE_IMPORT_CONFIRMATION_SCHEMA,
            "run_id": "linux-run-9",
            "manifest_sha256": HEX,
            "receipt_sha256": OTHER_HEX,
            "files": 12,
            "bytes": 4096,
            "confirmed_at": TS,
        }
        # A counts-only import cannot stand in for the complete confirmation
        # (imported inventory + production identity + sealed confirmation).
        self.reject("cache_import_confirmation_invalid", lambda: ir.validate_cache_release_request(counts_only))

    def test_cache_release_binds_full_import_confirmation(self) -> None:
        ir.validate_cache_release_request(cache_request())
        bad = cache_request()
        bad["binding"]["contract"]["schema_version"] = "unknown.contract/v99"
        self.reject("cache_release_invalid", lambda: ir.validate_cache_release_request(bad))
        bad = cache_request()
        # A known but wrong-family schema id must be refused as well.
        bad["binding"]["contract"]["schema_version"] = HEALTH_SCHEMA
        self.reject("cache_release_invalid", lambda: ir.validate_cache_release_request(bad))
        bad = cache_request()
        bad["operation_id"] = bad["import"]["production"]["operation_id"]
        self.reject("cache_release_invalid", lambda: ir.validate_cache_release_request(bad))
        bad = cache_request()
        bad["import"]["confirmation"] = {
            "schema_version": CACHE_IMPORT_CONFIRMATION_SCHEMA,
            "run_id": "linux-run-9",
            "manifest_sha256": HEX,
            "receipt_sha256": OTHER_HEX,
            "files": 12,
            "bytes": 4096,
            "confirmed_at": TS,
        }
        self.reject("cache_import_confirmation_invalid", lambda: ir.validate_cache_release_request(bad))
        bad = cache_request()
        bad["import"]["confirmation"]["digest"] = OTHER_HEX
        self.reject("cache_import_confirmation_invalid", lambda: ir.validate_cache_release_request(bad))
        bad = cache_request()
        mismatched = import_confirmation(production_block={"run_id": "native-run-8", "operation_id": "native-op-42"})
        bad["import"]["confirmation"] = mismatched
        self.reject("cache_release_invalid", lambda: ir.validate_cache_release_request(bad))
        bad = cache_request()
        bad["import"]["linux_run_id"] = ""
        self.reject("cache_release_invalid", lambda: ir.validate_cache_release_request(bad))

    def test_cache_receipt_zero_delete_and_immutability(self) -> None:
        ir.validate_cache_release_receipt(cache_receipt())
        ir.validate_cache_release_receipt(cache_receipt(status="already_released", released_files=0))
        self.reject(
            "cache_release_receipt_invalid",
            lambda: ir.validate_cache_release_receipt(cache_receipt(status="released", released_files=0)),
        )
        tampered = cache_receipt()
        tampered["released"]["bytes"] = 1
        self.reject("cache_release_receipt_mismatch", lambda: ir.validate_cache_release_receipt(tampered))
        bad = cache_receipt()
        bad["params_sha256"] = OTHER_HEX
        self.reject("cache_release_receipt_invalid", lambda: ir.validate_cache_release_receipt(bad))
        bad = cache_receipt()
        bad.pop("import")
        self.reject("cache_release_receipt_invalid", lambda: ir.validate_cache_release_receipt(bad))

    def test_config_identity_shape_legacy_and_file_checks(self) -> None:
        reference = {
            "schema_version": CONFIG_SNAPSHOT_SCHEMA,
            "path": "/srv/description/state/snapshots/gen-1.json",
            "sha256": HEX,
        }
        connection = {
            "conn_id": "solidworks_windows",
            "conn_type": "http",
            "host": "",
            "port": None,
            "password": "",
            "extra": {"config_identity": reference},
        }
        self.assertEqual(ir.validate_connection_config_identity(connection), reference)
        legacy = copy.deepcopy(connection)
        legacy["host"] = "127.0.0.1"
        self.reject("config_identity_legacy", lambda: ir.validate_connection_config_identity(legacy))
        legacy = copy.deepcopy(connection)
        legacy["extra"]["targets"] = {}
        self.reject("config_identity_legacy", lambda: ir.validate_connection_config_identity(legacy))
        bad = copy.deepcopy(connection)
        bad["extra"]["config_identity"]["path"] = "state/snap.json"
        self.reject("config_identity_invalid", lambda: ir.validate_connection_config_identity(bad))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "gen-1.json"
            target.write_bytes(b"{}")
            target.chmod(0o600)
            file_reference = {**reference, "path": str(target), "sha256": hashlib.sha256(b"{}").hexdigest()}
            ir.verify_config_identity_digest(
                ir.check_config_identity_file(file_reference, allowed_root=root), file_reference
            )
            target.chmod(0o644)
            self.reject(
                "config_identity_unreadable", lambda: ir.check_config_identity_file(file_reference, allowed_root=root)
            )
            with self.assertRaises(ir.InterfaceRecordError) as raised:
                ir.verify_config_identity_digest(b"{}", {**file_reference, "sha256": HEX})
            self.assertEqual(raised.exception.code, "config_identity_mismatch")

    def test_config_identity_active_only_for_new_submissions(self) -> None:
        record = install_record()
        active = {
            "schema_version": CONFIG_SNAPSHOT_SCHEMA,
            "path": "/srv/description/state/snapshots/gen-1.json",
            "sha256": record["snapshot"]["digest"],
        }
        ir.require_active_config_identity(active, record)
        sealed = dict(active, sha256=OTHER_HEX)
        self.reject("config_identity_not_active", lambda: ir.require_active_config_identity(sealed, record))
        before = json.dumps(sealed, sort_keys=True)
        ir.validate_connection_config_identity(
            {
                "conn_id": "c",
                "conn_type": "http",
                "host": "",
                "port": None,
                "password": "",
                "extra": {"config_identity": sealed},
            }
        )
        self.assertEqual(json.dumps(sealed, sort_keys=True), before)

    def test_config_snapshot_full_tree_and_negative_cases(self) -> None:
        snapshot = config_snapshot()
        ir.validate_config_snapshot(snapshot)
        self.assertEqual(len(ir.config_snapshot_digest(snapshot)), 64)
        mutations = (
            ("config_snapshot_invalid", lambda value: value["resolved"].__setitem__("auth", None)),
            ("config_snapshot_invalid", lambda value: value["resolved"]["auth"].__setitem__("tenant_keys", [])),
            (
                "config_snapshot_invalid",
                lambda value: value["resolved"]["windows"].__setitem__("endpoint_port", "8765"),
            ),
            (
                "config_snapshot_invalid",
                lambda value: value["resolved"]["database"].__setitem__("dsn_file", "secrets/postgres.dsn"),
            ),
            ("config_snapshot_invalid", lambda value: value["resolved"].__setitem__("targets", {})),
            ("config_snapshot_invalid", lambda value: value["resolved"]["targets"]["arm"].__setitem__("base", "-x")),
            (
                "config_snapshot_invalid",
                lambda value: value["resolved"]["derived"]["repositories"].clear(),
            ),
            (
                "config_snapshot_invalid",
                lambda value: value["resolved"]["windows"].__setitem__("tunnel_loopback_port", 8443),
            ),
            (
                "config_snapshot_invalid",
                lambda value: value["resolved"]["storage"].__setitem__("release_root", "/srv/description/artifacts"),
            ),
            ("config_snapshot_invalid", lambda value: value["resolved"]["derived"].__setitem__("extra", {})),
        )
        for code, mutate in mutations:
            bad = copy.deepcopy(snapshot)
            mutate(bad)
            self.reject(code, lambda bad=bad: ir.validate_config_snapshot(bad))
        bad = copy.deepcopy(snapshot)
        bad["resolved"]["windows"]["token"] = "inline-secret"
        self.reject("config_snapshot_invalid", lambda: ir.validate_config_snapshot(bad))
        bad = copy.deepcopy(snapshot)
        bad["generated_at"] = "2026-13-01T09:00:00Z"
        self.reject("config_snapshot_invalid", lambda: ir.validate_config_snapshot(bad))

    def test_install_record_ends_and_journal_rules(self) -> None:
        ir.validate_install_record(install_record())
        ir.validate_install_record(install_record(previous=True), previous_required=True)
        # Per-end config digests differ and are never compared; the common release
        # identity triple is compared across ends (role excluded).
        different_ends = install_record()
        self.assertNotEqual(
            different_ends["ends"]["linux"]["config_digest"], different_ends["ends"]["windows"]["config_digest"]
        )
        bad = install_record()
        bad["ends"]["windows"]["release"]["source_commit"] = OTHER_COMMIT
        self.reject("release_identity_mismatch", lambda: ir.validate_install_record(bad))
        bad = install_record()
        bad["snapshot"]["install_id"] = "gen-9"
        self.reject("install_record_invalid", lambda: ir.validate_install_record(bad))
        bad = install_record()
        bad["ends"]["linux"]["config_digest"] = OTHER_HEX
        self.reject("install_record_invalid", lambda: ir.validate_install_record(bad))
        bad = install_record()
        bad["dag"]["tasks"] = len(STAGE_IDS) + 1
        self.reject("install_record_invalid", lambda: ir.validate_install_record(bad))
        bad = install_record()
        bad["ends"].pop("windows")
        self.reject("install_record_invalid", lambda: ir.validate_install_record(bad))
        bad = install_record()
        bad["journal"][0]["evidence"] = []
        self.reject("install_record_invalid", lambda: ir.validate_install_record(bad))
        bad = install_record()
        bad["journal"][0]["seq"] = 2
        self.reject("install_record_invalid", lambda: ir.validate_install_record(bad))
        bad = install_record()
        bad["snapshot"]["token"] = "secret"
        self.reject("install_record_invalid", lambda: ir.validate_install_record(bad))
        self.reject(
            "install_record_invalid", lambda: ir.validate_install_record(install_record(), previous_required=True)
        )

    def test_endpoint_matches_install_record_helper(self) -> None:
        record = install_record()
        payload = health(True)
        payload["install"] = {"config_digest": OTHER_HEX, "install_id": "ep-7"}
        ir.require_endpoint_matches_install_record(payload, record)
        # Owned active/queued work with the same complete identity must not be refused.
        owned = copy.deepcopy(payload)
        owned["jobs"] = {"active": 2, "queued": 1, "unresolved": 0}
        ir.require_endpoint_matches_install_record(owned, record)
        bad = copy.deepcopy(payload)
        bad["install"]["config_digest"] = HEX
        self.reject("endpoint_end_mismatch", lambda: ir.require_endpoint_matches_install_record(bad, record))
        bad = copy.deepcopy(payload)
        bad["release"]["source_commit"] = OTHER_COMMIT
        self.reject("release_identity_mismatch", lambda: ir.require_endpoint_matches_install_record(bad, record))
        # Not-ready endpoints (every precondition) and drifted port/owner digests are refused.
        not_ready = copy.deepcopy(payload)
        not_ready.update(ready=False)
        not_ready["readiness"] = {
            "reason_code": "solidworks_unregistered",
            "native": {"ok": False, "problems": ["solidworks_unregistered"]},
        }
        self.reject("endpoint_not_ready", lambda: ir.require_endpoint_matches_install_record(not_ready, record))
        external = copy.deepcopy(payload)
        external.update(ready=False)
        external["cad"]["external_session_detected"] = True
        external["readiness"] = {"reason_code": "external_cad_session", "native": {"ok": True, "problems": []}}
        self.reject("endpoint_not_ready", lambda: ir.require_endpoint_matches_install_record(external, record))
        unresolved = copy.deepcopy(payload)
        unresolved.update(ready=False)
        unresolved["jobs"]["unresolved"] = 1
        unresolved["readiness"] = {"reason_code": "unresolved_operations", "native": {"ok": True, "problems": []}}
        self.reject("endpoint_not_ready", lambda: ir.require_endpoint_matches_install_record(unresolved, record))
        drifted = copy.deepcopy(payload)
        drifted["endpoint"]["port"] = 8766
        self.reject("endpoint_end_mismatch", lambda: ir.require_endpoint_matches_install_record(drifted, record))
        contract = copy.deepcopy(payload)
        contract["contract"]["digest"] = "0" * 64
        self.reject("endpoint_end_mismatch", lambda: ir.require_endpoint_matches_install_record(contract, record))
        policy_drift = copy.deepcopy(payload)
        policy_drift["resource_policy"]["digest"] = "0" * 64
        self.reject("endpoint_end_mismatch", lambda: ir.require_endpoint_matches_install_record(policy_drift, record))
        # Snapshot binding: the recorded digest, the strict single-byte reader, the generation
        # identity and the IF-11 file checks all guard the same checked bytes.
        original = _SNAPSHOT_FILE.read_bytes()
        snapshot = json.loads(original)
        snapshot["resolved"]["windows"]["endpoint_port"] = 9999
        _SNAPSHOT_FILE.write_text(json.dumps(snapshot), encoding="utf-8")
        port_follow = copy.deepcopy(payload)
        port_follow["endpoint"]["port"] = 9999
        self.reject(
            "config_identity_mismatch", lambda: ir.require_endpoint_matches_install_record(port_follow, record)
        )

        snapshot = json.loads(original)
        snapshot["install_id"] = "gen-9"
        sealed = json.dumps(snapshot).encode("utf-8")
        _SNAPSHOT_FILE.write_bytes(sealed)
        resealed = copy.deepcopy(record)
        resealed["snapshot"]["digest"] = hashlib.sha256(sealed).hexdigest()
        resealed["ends"]["linux"]["config_digest"] = resealed["snapshot"]["digest"]
        self.reject(
            "endpoint_end_mismatch",
            lambda: ir.require_endpoint_matches_install_record(copy.deepcopy(payload), resealed),
        )

        forged = original.replace(b'"endpoint_port": 8765', b'"endpoint_port": 8765, "endpoint_port": 8765')
        self.assertNotEqual(forged, original)
        _SNAPSHOT_FILE.write_bytes(forged)
        duplicate = copy.deepcopy(record)
        duplicate["snapshot"]["digest"] = hashlib.sha256(forged).hexdigest()
        duplicate["ends"]["linux"]["config_digest"] = duplicate["snapshot"]["digest"]
        self.reject(
            "endpoint_end_mismatch",
            lambda: ir.require_endpoint_matches_install_record(copy.deepcopy(payload), duplicate),
        )

        _SNAPSHOT_FILE.write_bytes(original)
        outside = Path(tempfile.mkdtemp(prefix="ir-outside-")) / "gen-1.json"
        outside.write_bytes(original)
        outside.chmod(0o600)
        displaced = copy.deepcopy(record)
        displaced["snapshot"]["path"] = str(outside)
        self.reject(
            "config_identity_unreadable",
            lambda: ir.require_endpoint_matches_install_record(copy.deepcopy(payload), displaced),
        )
        link = _SNAPSHOT_FILE.parent / "gen-link.json"
        link.unlink(missing_ok=True)
        link.symlink_to(_SNAPSHOT_FILE)
        linked = copy.deepcopy(record)
        linked["snapshot"]["path"] = str(link)
        self.reject(
            "config_identity_unreadable",
            lambda: ir.require_endpoint_matches_install_record(copy.deepcopy(payload), linked),
        )

    def test_install_record_validation_is_static(self) -> None:
        source = Path(ir.__file__).read_text(encoding="utf-8")
        imported = {
            alias.name.split(".")[0]
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Import)
            for alias in node.names
        } | {
            node.module.split(".")[0]
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.ImportFrom) and node.module
        }
        self.assertFalse(imported & {"socket", "ssl", "http", "urllib", "subprocess", "requests"})
        with mock.patch("socket.socket", side_effect=AssertionError("no network in validation")):
            ir.validate_install_record(install_record(previous=True))

    def test_delivery_manifest_subject_records(self) -> None:
        manifest_value = manifest()
        ir.validate_delivery_manifest(manifest_value)
        bad = copy.deepcopy(manifest_value)
        bad["policy"]["schema_version"] = "unknown.policy/v99"
        self.reject("delivery_manifest_invalid", lambda: ir.validate_delivery_manifest(bad))
        bad = copy.deepcopy(manifest_value)
        bad["policy"]["schema_version"] = HEALTH_SCHEMA
        self.reject("delivery_manifest_invalid", lambda: ir.validate_delivery_manifest(bad))
        bad = copy.deepcopy(manifest_value)
        bad["policy"]["digest"] = OTHER_HEX
        self.reject("delivery_manifest_policy_mismatch", lambda: ir.validate_delivery_manifest(bad))
        bad = copy.deepcopy(manifest_value)
        bad["subject"]["digest"] = OTHER_HEX
        self.reject("delivery_manifest_subject_mismatch", lambda: ir.validate_delivery_manifest(bad))
        bad = copy.deepcopy(manifest_value)
        bad["subject"]["files"][0].pop("bytes")
        self.reject("delivery_manifest_invalid", lambda: ir.validate_delivery_manifest(bad))
        bad = copy.deepcopy(manifest_value)
        bad["subject"]["files"] = list(reversed(bad["subject"]["files"]))
        self.reject("delivery_manifest_invalid", lambda: ir.validate_delivery_manifest(bad))
        bad = copy.deepcopy(manifest_value)
        bad["subject"]["files"][1]["path"] = bad["subject"]["files"][0]["path"]
        self.reject("delivery_manifest_invalid", lambda: ir.validate_delivery_manifest(bad))
        bad = copy.deepcopy(manifest_value)
        bad["extra"] = True
        self.reject("delivery_manifest_invalid", lambda: ir.validate_delivery_manifest(bad))

    def test_source_record_identity_binding(self) -> None:
        record = source_record()
        ir.validate_source_record(record)
        bad = copy.deepcopy(record)
        bad["source_revision_id"] = HEX
        self.reject("source_record_identity_mismatch", lambda: ir.validate_source_record(bad))
        bad = copy.deepcopy(record)
        bad["selection"]["configuration"] = "Other"
        self.reject("source_record_invalid", lambda: ir.validate_source_record(bad))
        bad = copy.deepcopy(record)
        bad["identity"]["records"] = [
            {"reference": "z.json#a", "file_sha256": HEX},
            {"reference": "a.json#a", "file_sha256": HEX},
        ]
        self.reject("source_record_invalid", lambda: ir.validate_source_record(bad))
        bad = copy.deepcopy(record)
        bad["metadata"]["change_summary_source"] = "guess"
        self.reject("source_record_invalid", lambda: ir.validate_source_record(bad))


class QualificationKeyTests(unittest.TestCase):
    """IF-14: the machine key is strictly (fact_id, scope, object_id)."""

    def _item(self, **overrides) -> dict:
        item = {
            "schema_version": ir.QUALIFICATION_SCHEMA,
            "fact_id": "inertia",
            "scope": "",
            "object_id": "base",
            "status": "established",
            "evidence": [{"record": "input/handoff.json", "sha256": HEX}],
        }
        item.update(overrides)
        return item

    def test_identity_is_the_key_only(self):
        item = self._item()
        same_key = self._item(status="unqualified", evidence=[{"record": "model/robot.json", "sha256": HEX}])
        self.assertEqual(ir.qualification_identity(item), ir.qualification_identity(same_key))
        for mutation in (
            {"fact_id": "geometry"},
            {"object_id": "tool"},
            {"scope": "instance:1"},
        ):
            with self.subTest(mutation=mutation):
                self.assertNotEqual(
                    ir.qualification_identity(item),
                    ir.qualification_identity(self._item(**mutation)),
                )

    def test_validator_refuses_unknown_shape_and_ungrounded_records(self):
        self.assertEqual(self._item(), ir.validate_qualification(self._item()))
        cases = (
            {"status": "qualified"},
            {"evidence": []},
            {"evidence": [{"record": "input/handoff.json"}]},
            {"evidence": [{"record": "input/handoff.json", "sha256": "0"}]},
            {"fact_id": ""},
            {"display": "可用运动"},
        )
        for mutation in cases:
            with self.subTest(mutation=mutation), self.assertRaises(PipelineError) as raised:
                ir.validate_qualification(self._item(**mutation))
            self.assertEqual("qualification_invalid", str(raised.exception))

    def test_resolver_refuses_missing_records_and_digest_mismatches(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "input").mkdir()
            record = root / "input/handoff.json"
            record.write_bytes(b"sealed input")
            item = self._item(
                evidence=[
                    {"record": "input/handoff.json", "sha256": hashlib.sha256(b"sealed input").hexdigest()}
                ]
            )
            ir.validate_qualification(item, resolve=root)
            record.write_bytes(b"tampered input")
            with self.assertRaises(PipelineError):
                ir.validate_qualification(item, resolve=root)
            record.unlink()
            with self.assertRaises(PipelineError):
                ir.validate_qualification(item, resolve=root)

    def test_expansion_keeps_two_objects_apart_and_never_duplicates(self):
        definition = {
            "checks": {},
            "source": {
                "bodies": [{"name": "base"}, {"name": "tool"}],
                "joints": [{"name": "hinge", "type": "revolute"}, {"name": "weld", "type": "fixed"}],
                "frames": [{"name": "tool_tcp"}],
            },
        }
        facts = ir.qualification_facts(definition)
        keys = [(fact["fact_id"], fact["scope"], fact["object_id"]) for fact in facts]
        self.assertEqual(len(keys), len(set(keys)), "the key never repeats")
        self.assertIn(("inertia", "", "base"), keys)
        self.assertIn(("inertia", "", "tool"), keys)
        self.assertNotEqual(
            ir.qualification_identity({"fact_id": "inertia", "scope": "", "object_id": "base"}),
            ir.qualification_identity({"fact_id": "inertia", "scope": "", "object_id": "tool"}),
        )
        self.assertNotIn(("shafts", "", "weld"), keys, "a fixed joint has no shaft fact")
        self.assertIn(("physics.expected_mass", "", ""), keys)
        self.assertIn(("geometry.expected_extent", "", ""), keys)
        for identifier in ir.CHECK_POLICY_V1["fixed_checks"]:
            self.assertIn((identifier, "", ""), keys)
        self.assertEqual(facts, ir.qualification_facts(definition), "recomputation is stable")


if __name__ == "__main__":
    unittest.main()
