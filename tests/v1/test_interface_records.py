"""Positive/negative guards for the W1 machine records (frozen-interfaces-v1.1)."""

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
from description_pipeline.io import digest
from description_pipeline.schemas import (
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
from description_pipeline.stages import CONTRACT, STAGE_IDS

HEX = "a" * 64
OTHER_HEX = "b" * 64
COMMIT = "c" * 40
OTHER_COMMIT = "d" * 40
TS = "2026-10-13T09:00:00Z"


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
        "health_schema": HEALTH_SCHEMA,
        "pipeline_id": "solidworks-to-urdf",
        "native_platform": True,
        "ready": ready,
        "release": release("native"),
        "contract": {"schema_version": CONTRACT["schema_version"], "digest": HEX},
        "resource_policy": {"schema_version": RESOURCE_POLICY_SCHEMA, "digest": ir.resource_policy_digest(policy())},
        "install": {"config_digest": HEX, "install_id": "gen-1"},
        "auth": {"scheme": "bearer", "token_fingerprint": "3e1d"},
        "endpoint": {"port": 8765},
        "cad": {"external_session_detected": False, "owned_sessions": 0, "lease_held": False},
        "jobs": {"active": 0, "queued": 0, "unresolved": 0},
        "capacity": {"free_bytes": 10**12, "reserved_bytes": 0},
        "readiness": {
            "reason_code": None if ready else "solidworks_unregistered",
            "native": {"ok": ready, "problems": [] if ready else ["solidworks_unregistered"]},
        },
    }


def import_block() -> dict:
    return {"run_id": "7c9b44b3-0b9c-5ffe-9f4f-4bbdafb4a865", "manifest_sha256": HEX, "receipt_sha256": OTHER_HEX}


def cache_request() -> dict:
    block = import_block()
    return {
        "schema_version": CACHE_RELEASE_SCHEMA,
        "binding": {
            "operation_id": "op-0001",
            "params_sha256": ir.cache_release_params_digest(block),
            "release": release(),
            "contract": {"schema_version": CONTRACT["schema_version"], "digest": HEX},
        },
        "import": block,
    }


def cache_receipt() -> dict:
    receipt = {
        "schema_version": CACHE_RELEASE_RECEIPT_SCHEMA,
        "status": "released",
        "released": {"files": 12, "bytes": 4096},
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
            "operator": {"origin": "https://operator.example.com:8443"},
            "storage": {"state_root": "/srv/description/state", "handoff_root": "/srv/description/handoffs"},
            "windows": {
                "ssh_host": "solidworks-worker",
                "endpoint_port": 8765,
                "tunnel_loopback_port": 18765,
                "token_file": "/srv/description/secrets/endpoint.token",
            },
            "execution": {"linux_concurrency": 1},
            "retention": {"unreferenced_evidence_days": None},
            "targets": {"arm": {"repository_slug": "owner/repo", "base": "main"}},
        },
        "sources": {"platform_json_sha256": HEX},
    }


def install_record(*, previous: bool = False, journal: list | None = None) -> dict:
    record = {
        "schema_version": INSTALL_RECORD_SCHEMA,
        "install_id": "gen-2" if previous else "gen-1",
        "timestamp": TS,
        "snapshot": {"path": "/srv/description/state/snapshots/gen-2.json", "digest": HEX},
        "release": {**release(), "artifacts": [{"name": "linux.tar.gz", "sha256": OTHER_HEX}]},
        "services": ["description-postgres", "description-operator-proxy"],
        "paths": {"state_root": "/srv/description/state", "release_root": "/srv/description/releases"},
        "dag": {
            "id": "solidworks_to_urdf",
            "path": "/srv/description/dags/solidworks_to_urdf.py",
            "digest": HEX,
            "tasks": len(STAGE_IDS),
        },
        "ends": {
            "linux": {
                "install_id": "gen-2" if previous else "gen-1",
                "config_digest": HEX,
                "release": release("portable"),
            },
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
    files = {"evidence/discovery.json": HEX, "input/handoff.json": OTHER_HEX, "tool.json": HEX}
    return {
        "schema_version": DELIVERY_MANIFEST_SCHEMA,
        "subject": {"files": files, "digest": digest(files)},
        "contract": {"schema_version": CONTRACT["schema_version"], "digest": HEX},
        "policy": {"schema_version": "solidworks-to-urdf.quality-policy/v1", "digest": OTHER_HEX},
    }


class InterfaceRecordTests(unittest.TestCase):
    def reject(self, code: str, mutate) -> None:
        with self.assertRaises(ir.InterfaceRecordError) as raised:
            mutate()
        self.assertEqual(raised.exception.code, code)

    def test_release_identity_triple_rejects_equal_digest_different_commit(self) -> None:
        left, right = release(), release()
        ir.require_same_release_identity(left, right)
        right["source_commit"] = OTHER_COMMIT
        self.reject("release_identity_mismatch", lambda: ir.require_same_release_identity(left, right))
        right = release()
        right["version"] = "1.4.1"
        self.reject("release_identity_mismatch", lambda: ir.require_same_release_identity(left, right))
        ir.validate_release_identity(left)
        ir.validate_release_identity(release("native"), role="native")
        self.reject("release_identity_invalid", lambda: ir.validate_release_identity(release("platform")))

    def test_resource_policy_positive_and_negative(self) -> None:
        policy_value = policy()
        ir.validate_resource_policy(policy_value)
        self.assertEqual(len(ir.resource_policy_digest(policy_value)), 64)
        bad = copy.deepcopy(policy_value)
        bad["limits"]["max_upload_bytes"] = bad["limits"]["max_total_bytes"] - 1
        self.reject("resource_policy_invalid", lambda: ir.validate_resource_policy(bad))
        bad = copy.deepcopy(policy_value)
        bad["capacity"]["cache_root"] = "/srv/cache"
        self.reject("resource_policy_invalid", lambda: ir.validate_resource_policy(bad))
        bad = copy.deepcopy(policy_value)
        bad["timeouts"].pop("job_seconds")
        self.reject("resource_policy_invalid", lambda: ir.validate_resource_policy(bad))
        bad = copy.deepcopy(policy_value)
        bad["limits"]["max_files"] = 0
        self.reject("resource_policy_invalid", lambda: ir.validate_resource_policy(bad))

    def test_health_positive_ready_and_unready(self) -> None:
        ir.validate_health(health(True))
        ir.validate_health(health(False))
        bad = health(False)
        bad["readiness"]["reason_code"] = None
        self.reject("health_invalid", lambda: ir.validate_health(bad))
        bad = health(True)
        bad["release"]["role"] = "portable"
        self.reject("health_invalid", lambda: ir.validate_health(bad))
        bad = health(True)
        bad["endpoint"]["port"] = 70000
        self.reject("health_invalid", lambda: ir.validate_health(bad))
        bad = health(True)
        bad["jobs"]["unknown_total"] = 1
        self.reject("health_invalid", lambda: ir.validate_health(bad))

    def test_cache_release_request_binds_parameter_digest(self) -> None:
        ir.validate_cache_release_request(cache_request())
        bad = cache_request()
        bad["binding"]["params_sha256"] = OTHER_HEX
        self.reject("cache_release_params_mismatch", lambda: ir.validate_cache_release_request(bad))
        bad = cache_request()
        bad["import"]["receipt_sha256"] = "not-hex"
        self.reject("cache_release_invalid", lambda: ir.validate_cache_release_request(bad))
        bad = cache_request()
        bad["binding"]["operation_id"] = ""
        self.reject("call_binding_invalid", lambda: ir.validate_cache_release_request(bad))

    def test_cache_confirmation_and_immutable_receipt(self) -> None:
        confirmation = {
            "schema_version": CACHE_IMPORT_CONFIRMATION_SCHEMA,
            "run_id": "7c9b44b3-0b9c-5ffe-9f4f-4bbdafb4a865",
            "manifest_sha256": HEX,
            "receipt_sha256": OTHER_HEX,
            "files": 12,
            "bytes": 4096,
            "confirmed_at": TS,
        }
        ir.validate_cache_import_confirmation(confirmation)
        good = cache_receipt()
        ir.validate_cache_release_receipt(good)
        tampered = copy.deepcopy(good)
        tampered["released"]["bytes"] = 1
        self.reject("cache_release_receipt_mismatch", lambda: ir.validate_cache_release_receipt(tampered))
        bad = copy.deepcopy(good)
        bad["status"] = "maybe"
        self.reject("cache_release_receipt_invalid", lambda: ir.validate_cache_release_receipt(bad))
        bad = copy.deepcopy(confirmation)
        bad["confirmed_at"] = "2026-10-13 09:00"
        self.reject("cache_import_confirmation_invalid", lambda: ir.validate_cache_import_confirmation(bad))

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
        bad = copy.deepcopy(connection)
        bad["extra"]["config_identity"]["schema_version"] = "solidworks-to-urdf.config-snapshot/v2"
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
        # A sealed request keeps its fixed record: only the shape is validated and
        # the reference and its parameters stay byte-identical across a switch.
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

    def test_config_snapshot_positive_and_secret_rejection(self) -> None:
        snapshot = config_snapshot()
        ir.validate_config_snapshot(snapshot)
        self.assertEqual(len(ir.config_snapshot_digest(snapshot)), 64)
        bad = copy.deepcopy(snapshot)
        bad["resolved"]["windows"]["token"] = "abc"
        self.reject("config_snapshot_invalid", lambda: ir.validate_config_snapshot(bad))
        bad = copy.deepcopy(snapshot)
        bad["resolved"]["mystery"] = {}
        self.reject("config_snapshot_invalid", lambda: ir.validate_config_snapshot(bad))

    def test_install_record_positive_previous_and_journal_rules(self) -> None:
        ir.validate_install_record(install_record())
        second = install_record(previous=True)
        ir.validate_install_record(second, previous_required=True)
        # Per-host generations: the two ends are never compared for equality (or inequality).
        self.assertNotEqual(second["ends"]["linux"]["config_digest"], second["ends"]["windows"]["config_digest"])
        ir.validate_install_record(install_record(previous=True))
        self.reject(
            "install_record_invalid", lambda: ir.validate_install_record(install_record(), previous_required=True)
        )
        bad = install_record()
        bad["dag"]["tasks"] = len(STAGE_IDS) + 1
        self.reject("install_record_invalid", lambda: ir.validate_install_record(bad))
        bad = install_record()
        bad["ends"].pop("windows")
        self.reject("install_record_invalid", lambda: ir.validate_install_record(bad))
        bad = install_record()
        bad["journal"][0]["evidence"] = []
        self.reject("install_record_invalid", lambda: ir.validate_install_record(bad))
        bad = install_record(previous=True)
        bad["ends"]["windows"]["release"]["source_commit"] = OTHER_COMMIT
        ir.validate_install_record(bad)  # per-end identity is checked against that end's health, not the record
        bad = install_record()
        bad["journal"][0]["seq"] = 2
        self.reject("install_record_invalid", lambda: ir.validate_install_record(bad))
        bad = install_record()
        bad["snapshot"]["token"] = "secret"
        self.reject("install_record_invalid", lambda: ir.validate_install_record(bad))

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

    def test_delivery_manifest_subject_binding(self) -> None:
        manifest_value = manifest()
        ir.validate_delivery_manifest(manifest_value)
        bad = copy.deepcopy(manifest_value)
        bad["subject"]["digest"] = OTHER_HEX
        self.reject("delivery_manifest_subject_mismatch", lambda: ir.validate_delivery_manifest(bad))
        bad = copy.deepcopy(manifest_value)
        bad["subject"]["files"] = dict(reversed(list(bad["subject"]["files"].items())))
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


if __name__ == "__main__":
    unittest.main()
