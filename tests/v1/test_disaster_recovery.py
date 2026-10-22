"""Fake-CLI unit and end-to-end control tests for recovery."""

from __future__ import annotations

import argparse
import base64
import contextlib
import copy
import hashlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import textwrap
import unittest
import warnings
import zipfile
from pathlib import Path
from unittest import mock

from cryptography.fernet import Fernet
from description_pipeline.schemas import INSTALL_RECORD_SCHEMA

HERE = Path(__file__).resolve().parent
REPOSITORY = HERE.parents[1]
DEPLOY = Path(os.environ.get("DESCRIPTION_DEPLOY_DIR", REPOSITORY / "deploy" / "operator"))
sys.path.insert(0, str(DEPLOY))
sys.path.insert(0, str(HERE))

import recovery  # noqa: E402
import windows_transport  # noqa: E402

warnings.filterwarnings("ignore", category=ResourceWarning)


class UnitInventoryTests(unittest.TestCase):
    def test_maintenance_units_are_bound_to_the_receipt_schema_role(self) -> None:
        self.assertEqual(recovery.MAINTENANCE_RECEIPT_SCHEMA, "solidworks-to-urdf.recovery-maintenance/v1")
        self.assertEqual(recovery.MAINTENANCE_ROLE, "maintenance-v1")
        self.assertEqual(
            recovery.MAINTENANCE_UNITS,
            recovery.systemd_units.units_for_role(recovery.MAINTENANCE_ROLE),
        )
        self.assertEqual(
            recovery.MAINTENANCE_UNITS,
            (
                "description-solidworks-tunnel",
                "description-airflow-dag-processor",
                "description-airflow-scheduler",
                "description-airflow-api-server",
                "description-operator-proxy",
                "description-portal",
            ),
        )
        source = (DEPLOY / "recovery.py").read_text(encoding="utf-8")
        self.assertNotIn("MAINTENANCE_UNITS = (\n", source)


def write(path: Path, payload: str | bytes, mode: int = 0o600) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_bytes(payload.encode() if isinstance(payload, str) else payload)
    path.chmod(mode)
    return path


def install_record_fixture(state: Path, install_id: str, snapshot: Path) -> dict:
    triple = {"version": "1.0.0", "source_commit": "a" * 40, "source_sha256": "b" * 64}
    snapshot_sha = recovery.sha256_file(snapshot)
    return {
        "schema_version": INSTALL_RECORD_SCHEMA,
        "install_id": install_id,
        "timestamp": "2026-10-22T00:00:00Z",
        "snapshot": {"path": str(snapshot), "digest": snapshot_sha, "install_id": install_id},
        "release": {**triple, "artifacts": [{"name": "runtime.whl", "sha256": "c" * 64}]},
        "services": ["description.service"],
        "paths": {"state_root": str(state)},
        "dag": {"id": "solidworks_to_urdf", "path": str(state / "dag.py"), "digest": "d" * 64, "tasks": 6},
        "ends": {
            "linux": {
                "install_id": install_id,
                "config_digest": snapshot_sha,
                "release": {**triple, "role": "portable"},
            },
            "windows": {
                "install_id": "win-" + "a" * 32,
                "config_digest": "a" * 64,
                "release": {**triple, "role": "native"},
            },
        },
        "previous": None,
        "journal": [],
    }


def journal_row_fixture(evidence: list[str]) -> dict:
    return {
        "seq": 1,
        "verb": "promote",
        "step": "promotion-receipt",
        "status": "completed",
        "at": "2026-10-22T00:00:00Z",
        "evidence": evidence,
    }


class CheckpointTreeVerificationTests(unittest.TestCase):
    def checkpoint_fixture(
        self,
        root: Path,
        *,
        subject_files: dict[str, bytes] | None = None,
    ) -> tuple[Path, dict]:
        from description_pipeline.delivery import SUBJECT_DIRECTORIES, write_delivery_manifest
        from description_pipeline.interface_records import CHECK_POLICY_SCHEMA, check_policy_digest
        from description_pipeline.stages import CONTRACT, CONTRACT_SHA256

        tree = root / "sealed/run/discover"
        for name in SUBJECT_DIRECTORIES:
            (tree / name).mkdir(parents=True, exist_ok=True)
        write(tree / "tool.json", "{}")
        for name, payload in (subject_files or {}).items():
            write(tree / name, payload)
        manifest = write_delivery_manifest(
            tree,
            contract={"schema_version": CONTRACT["schema_version"], "digest": CONTRACT_SHA256},
            policy={"schema_version": CHECK_POLICY_SCHEMA, "digest": check_policy_digest()},
        )
        return tree, {"artifacts": manifest["subject"]["files"]}

    def rewrite_delivery_manifest(self, tree: Path) -> dict:
        from description_pipeline.delivery import write_delivery_manifest
        from description_pipeline.interface_records import CHECK_POLICY_SCHEMA, check_policy_digest
        from description_pipeline.stages import CONTRACT, CONTRACT_SHA256

        return write_delivery_manifest(
            tree,
            contract={"schema_version": CONTRACT["schema_version"], "digest": CONTRACT_SHA256},
            policy={"schema_version": CHECK_POLICY_SCHEMA, "digest": check_policy_digest()},
        )

    def verify(self, tree: Path, receipt: dict) -> None:
        recovery.verify_checkpoint_tree(
            tree,
            recovery.inventory_tree(tree),
            receipt=receipt,
        )

    def legacy_discovery_fixture(self, root: Path) -> tuple[Path, dict, dict]:
        from description_pipeline import interface_records as ir
        from description_pipeline.delivery import PIPELINE_ID
        from description_pipeline.io import canonical
        from description_pipeline.sources.solidworks.discovery import (
            DISCOVERY_SCHEMA,
            FINDING_SCHEMA,
            IDENTITY_SCHEMA,
        )
        from description_pipeline.stages import CONTRACT, CONTRACT_SHA256

        run_id = "run"
        handoff_sha256 = "a" * 64
        discovery = {
            "schema_version": DISCOVERY_SCHEMA,
            "run_id": run_id,
            "handoff_sha256": handoff_sha256,
            "findings": [{"code": "legacy.finding", "object": "assembly"}],
            "identity": {
                "hardware_id": "m3.0",
                "parent_revision": None,
                "revision": "A.3",
            },
        }
        tree, receipt = self.checkpoint_fixture(
            root,
            subject_files={"evidence/discovery.json": canonical(discovery)},
        )
        findings_bytes = canonical(
            {
                "schema_version": FINDING_SCHEMA,
                "findings": discovery["findings"],
            }
        )
        identity = {
            "schema_version": IDENTITY_SCHEMA,
            "run_id": run_id,
            "handoff_sha256": handoff_sha256,
            "hardware_id": "m3.0",
            "parent_revision": None,
            "revision": "A.3",
            "sources": [],
        }
        write(tree / "discovery/findings.json", findings_bytes)
        write(tree / "discovery/identity-resolution.json", canonical(identity))
        manifest_path = tree / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["policy"]["digest"] = next(iter(ir.RECOVERY_HISTORICAL_POLICY_DIGESTS))
        manifest_path.write_text(json.dumps(manifest))

        aliases = {
            "input/discovery/native-discovery.json": hashlib.sha256(
                (tree / "evidence/discovery.json").read_bytes()
            ).hexdigest(),
            "input/discovery/findings.json": hashlib.sha256(findings_bytes).hexdigest(),
            "input/discovery/identity-resolution.json": hashlib.sha256(canonical(identity)).hexdigest(),
        }
        rows = [
            {"id": stage["id"], "state": "not_run", "in_scope": False, "input_qc": [], "output_qc": []}
            for stage in CONTRACT["stages"]
        ]
        by_id = {row["id"]: row for row in rows}
        discover_definition = next(stage for stage in CONTRACT["stages"] if stage["id"] == "discover")
        by_id["discover"].update(
            state="completed",
            in_scope=True,
            input_qc=[
                {"id": item["id"], "state": "passed", "details": {"passed": True}}
                for item in discover_definition["input_qc"]
            ],
            output_qc=[
                {"id": item["id"], "state": "passed", "details": {"passed": True}}
                for item in discover_definition["output_qc"]
            ],
        )
        by_id["discover"]["output_qc"][1]["details"]["files"] = dict(aliases)
        by_id["capture"].update(state="failed", in_scope=True)
        by_id["verify"]["inputs"] = [{"files": dict(aliases)}]
        by_id["publish"]["inputs"] = [{"files": dict(aliases)}]
        view = {
            "schema_version": "solidworks-to-urdf.stages/v1",
            "pipeline_id": PIPELINE_ID,
            "contract_sha256": CONTRACT_SHA256,
            "run_id": run_id,
            "handoff_sha256": handoff_sha256,
            "execution_scope": ["discover", "capture"],
            "stages": rows,
        }
        native_stages = write(tree / "reports/native-stages.json", canonical(view))
        native_stages_sha256 = hashlib.sha256(native_stages.read_bytes()).hexdigest()
        receipt = {
            **receipt,
            "run_id": "receipt-run-which-is-not-the-producer",
            "stage": "discover",
            "state": "discovered",
            "passed": False,
            "native_stages_sha256": native_stages_sha256,
        }
        receipt["artifacts"] = [
            *receipt["artifacts"],
            {
                "path": "reports/native-stages.json",
                "bytes": native_stages.stat().st_size,
                "sha256": native_stages_sha256,
            },
        ]
        return tree, receipt, view

    def rewrite_native_stages(self, tree: Path, receipt: dict, view: dict) -> dict:
        from description_pipeline.io import canonical

        path = tree / "reports/native-stages.json"
        path.write_bytes(canonical(view))
        updated = copy.deepcopy(receipt)
        row = next(item for item in updated["artifacts"] if item["path"] == "reports/native-stages.json")
        row["bytes"] = path.stat().st_size
        row["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        updated["native_stages_sha256"] = row["sha256"]
        return updated

    def copy_checkpoint_tree(self, source: Path, destination: Path) -> Path:
        copied = destination / source.parent.name / source.name
        copied.parent.mkdir(parents=True)
        shutil.copytree(source, copied)
        return copied

    def test_legacy_discovery_sidecars_are_bound_by_native_stage_aliases(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tree, receipt, _view = self.legacy_discovery_fixture(Path(directory))
            self.verify(tree, receipt)

    def test_legacy_discovery_sidecars_reject_tamper_unbound_unknown_and_third_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            tree, receipt, view = self.legacy_discovery_fixture(base / "base")

            tampered = self.copy_checkpoint_tree(tree, base / "tampered")
            write(tampered / "discovery/identity-resolution.json", "{}")
            with self.assertRaisesRegex(recovery.RecoveryError, "alias SHA-256 differs"):
                self.verify(tampered, receipt)

            unbound = self.copy_checkpoint_tree(tree, base / "unbound")
            unbound_view = copy.deepcopy(view)

            def strip_aliases(value: object) -> None:
                if isinstance(value, dict):
                    for key in tuple(value):
                        if isinstance(key, str) and key.startswith("input/discovery/"):
                            value.pop(key)
                        else:
                            strip_aliases(value[key])
                elif isinstance(value, list):
                    for item in value:
                        strip_aliases(item)

            strip_aliases(unbound_view)
            with self.assertRaisesRegex(recovery.RecoveryError, "not declared at every exact stage position"):
                self.verify(unbound, self.rewrite_native_stages(unbound, receipt, unbound_view))

            unknown = self.copy_checkpoint_tree(tree, base / "unknown")
            unknown_view = copy.deepcopy(view)
            unknown_view["stages"][4]["inputs"][0]["files"]["input/discovery/other.json"] = "b" * 64
            with self.assertRaisesRegex(recovery.RecoveryError, "unknown discovery sidecar alias"):
                self.verify(unknown, self.rewrite_native_stages(unknown, receipt, unknown_view))

            misplaced = self.copy_checkpoint_tree(tree, base / "misplaced")
            misplaced_view = copy.deepcopy(view)
            misplaced_view["unexpected"] = {
                "input/discovery/findings.json": hashlib.sha256(
                    (misplaced / "discovery/findings.json").read_bytes()
                ).hexdigest()
            }
            with self.assertRaisesRegex(recovery.RecoveryError, "outside a stage file map"):
                self.verify(misplaced, self.rewrite_native_stages(misplaced, receipt, misplaced_view))

            third = self.copy_checkpoint_tree(tree, base / "third")
            write(third / "discovery/unexpected.json", "{}")
            with self.assertRaisesRegex(recovery.RecoveryError, "unregistered files"):
                self.verify(third, receipt)

            partial = self.copy_checkpoint_tree(tree, base / "partial")
            (partial / "discovery/findings.json").unlink()
            with self.assertRaisesRegex(recovery.RecoveryError, "exact pair"):
                self.verify(partial, receipt)

            current_policy = self.copy_checkpoint_tree(tree, base / "current-policy")
            from description_pipeline import interface_records as ir

            manifest_path = current_policy / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["policy"]["digest"] = ir.check_policy_digest()
            manifest_path.write_text(json.dumps(manifest))
            with self.assertRaisesRegex(recovery.RecoveryError, "exact historical policy"):
                self.verify(current_policy, receipt)

    def test_legacy_discovery_sidecars_reject_conflicts_and_bad_projection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            tree, receipt, view = self.legacy_discovery_fixture(base / "base")

            conflict = self.copy_checkpoint_tree(tree, base / "conflict")
            conflict_view = copy.deepcopy(view)
            conflict_view["stages"][4]["inputs"][0]["files"]["input/discovery/findings.json"] = "f" * 64
            with self.assertRaisesRegex(recovery.RecoveryError, "alias SHA-256 differs"):
                self.verify(conflict, self.rewrite_native_stages(conflict, receipt, conflict_view))

            native_conflict = self.copy_checkpoint_tree(tree, base / "native-conflict")
            native_conflict_view = copy.deepcopy(view)
            native_conflict_view["stages"][4]["inputs"][0]["files"][
                "input/discovery/native-discovery.json"
            ] = "f" * 64
            with self.assertRaisesRegex(recovery.RecoveryError, "alias SHA-256 differs"):
                self.verify(
                    native_conflict,
                    self.rewrite_native_stages(native_conflict, receipt, native_conflict_view),
                )

            projection = self.copy_checkpoint_tree(tree, base / "projection")
            from description_pipeline.io import canonical
            from description_pipeline.sources.solidworks.discovery import FINDING_SCHEMA

            changed = canonical({"schema_version": FINDING_SCHEMA, "findings": []})
            findings_path = write(projection / "discovery/findings.json", changed)
            projection_view = copy.deepcopy(view)
            changed_sha256 = hashlib.sha256(findings_path.read_bytes()).hexdigest()

            def update_findings(value: object) -> None:
                if isinstance(value, dict):
                    for key, item in tuple(value.items()):
                        if key == "input/discovery/findings.json":
                            value[key] = changed_sha256
                        else:
                            update_findings(item)
                elif isinstance(value, list):
                    for item in value:
                        update_findings(item)

            update_findings(projection_view)
            with self.assertRaisesRegex(recovery.RecoveryError, "canonical discovery projection"):
                self.verify(projection, self.rewrite_native_stages(projection, receipt, projection_view))

            malformed_identity = self.copy_checkpoint_tree(tree, base / "malformed-identity")
            malformed_bytes = b"[]"
            identity_path = write(malformed_identity / "discovery/identity-resolution.json", malformed_bytes)
            malformed_view = copy.deepcopy(view)
            malformed_sha256 = hashlib.sha256(identity_path.read_bytes()).hexdigest()

            def update_identity(value: object) -> None:
                if isinstance(value, dict):
                    for key, item in tuple(value.items()):
                        if key == "input/discovery/identity-resolution.json":
                            value[key] = malformed_sha256
                        else:
                            update_identity(item)
                elif isinstance(value, list):
                    for item in value:
                        update_identity(item)

            update_identity(malformed_view)
            with self.assertRaisesRegex(recovery.RecoveryError, "identity resolution differs"):
                self.verify(
                    malformed_identity,
                    self.rewrite_native_stages(malformed_identity, receipt, malformed_view),
                )

    def test_legacy_discovery_sidecars_reject_receipt_and_stage_identity_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            tree, receipt, view = self.legacy_discovery_fixture(base / "base")

            receipt_drift = self.copy_checkpoint_tree(tree, base / "receipt-drift")
            changed_receipt = copy.deepcopy(receipt)
            changed_receipt["native_stages_sha256"] = "f" * 64
            with self.assertRaisesRegex(recovery.RecoveryError, "receipt native-stage digest"):
                self.verify(receipt_drift, changed_receipt)

            stage_drift = self.copy_checkpoint_tree(tree, base / "stage-drift")
            changed_view = copy.deepcopy(view)
            changed_view["run_id"] = "another-run"
            with self.assertRaisesRegex(recovery.RecoveryError, "invalid native stage view"):
                self.verify(
                    stage_drift,
                    self.rewrite_native_stages(stage_drift, receipt, changed_view),
                )

    def test_discover_input_records_empty_directory_is_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tree, receipt = self.checkpoint_fixture(Path(directory))
            (tree / "input/records/nested-empty").mkdir(parents=True)
            self.verify(tree, receipt)

    def test_discover_input_records_unregistered_file_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tree, receipt = self.checkpoint_fixture(Path(directory))
            write(tree / "input/records/unregistered.json", "{}")
            self.rewrite_delivery_manifest(tree)
            with self.assertRaisesRegex(recovery.RecoveryError, "unregistered files"):
                self.verify(tree, receipt)

    def test_unregistered_nonempty_directory_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tree, receipt = self.checkpoint_fixture(Path(directory))
            write(tree / "input/private/unregistered.json", "{}")
            self.rewrite_delivery_manifest(tree)
            with self.assertRaisesRegex(recovery.RecoveryError, "unregistered files"):
                self.verify(tree, receipt)

    def test_historical_policy_archive_is_recovery_compatible(self) -> None:
        historical = "415813b06c4b379fbbc1b5c6ea4c3fd56cf96c6ad8bfa103eaae03326de2b7c4"
        with tempfile.TemporaryDirectory() as directory:
            tree, receipt = self.checkpoint_fixture(Path(directory))
            manifest_path = tree / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["policy"]["digest"] = historical
            manifest_path.write_text(json.dumps(manifest))
            self.verify(tree, receipt)

            manifest["policy"]["digest"] = "f" * 64
            manifest_path.write_text(json.dumps(manifest))
            with self.assertRaisesRegex(recovery.RecoveryError, "checkpoint delivery manifest is invalid"):
                self.verify(tree, receipt)


def windows_recovery_fixture(context, staging: Path) -> tuple[dict, dict]:
    task_xml = b"<Task><RegistrationInfo><URI>\\fixture-endpoint</URI></RegistrationInfo></Task>"
    config = b'{"schema_version":"solidworks-to-urdf.endpoint/v1"}\n'
    token = b"t" * 64 + b"\n"
    install_id = b"win-" + b"1" * 32 + b"\n"
    marker = b'{"schema_version":"solidworks-to-urdf.managed-root/v1"}'
    generation = "C:/description/state/lifecycle/win-11111111111111111111111111111111/generation"
    paths = {
        "managed_root_marker": "C:/description/.description-managed-root.json",
        "endpoint_config": generation + "/endpoint.json",
        "endpoint_token": "C:/description/secrets/endpoint.token",
        "install_id": "C:/description/state/install-id",
    }
    payloads = {
        "task.xml": task_xml,
        "managed-root.json": marker,
        "endpoint.json": config,
        "token": token,
        "install-id": install_id,
    }
    hashes = {name: hashlib.sha256(payload).hexdigest() for name, payload in payloads.items()}
    files = [
        {
            "role": "task_xml",
            "path": "C:/Windows/System32/Tasks/fixture-endpoint",
            "member": "task.xml",
            "bytes": len(task_xml),
            "sha256": hashes["task.xml"],
        },
        {
            "role": "managed_root_marker",
            "path": paths["managed_root_marker"],
            "member": "managed-root.json",
            "bytes": len(marker),
            "sha256": hashes["managed-root.json"],
        },
        {
            "role": "endpoint_config",
            "path": paths["endpoint_config"],
            "member": "endpoint.json",
            "bytes": len(config),
            "sha256": hashes["endpoint.json"],
        },
        {
            "role": "endpoint_token",
            "path": paths["endpoint_token"],
            "member": "token",
            "bytes": len(token),
            "sha256": hashes["token"],
        },
        {
            "role": "install_id",
            "path": paths["install_id"],
            "member": "install-id",
            "bytes": len(install_id),
            "sha256": hashes["install-id"],
        },
    ]

    def acl_projection(
        seed: str,
        *,
        directory: bool = False,
        non_inheriting: bool = False,
    ) -> dict:
        del seed
        inheritance = 3 if directory and not non_inheriting else 0
        rules = [
            {
                "sid": sid,
                "access_type": "Allow",
                "rights": rights,
                "inherited": False,
                "inheritance": inheritance,
                "propagation": 0,
            }
            for sid, rights in (
                ("S-1-5-18", 2032127),
                ("S-1-5-21-1", 2032127),
                ("S-1-5-32-544", 2032127),
            )
        ]
        projection = {
            "owner_sid": "S-1-5-32-544",
            "group_sid": "S-1-5-18",
            "protected": True,
            "sddl": "O:S-1-5-32-544G:S-1-5-18D:PAI",
            "rules": rules,
        }
        projection["projection_sha256"] = recovery._windows_acl_projection_sha256(projection)
        return projection

    acl = {
        "common_parent": acl_projection("0", directory=True),
        "managed_root_marker": acl_projection("3"),
        "lifecycle_root": acl_projection("4", directory=True),
        "endpoint_secrets_root": acl_projection("5", directory=True),
        "task_file": acl_projection("a"),
        "endpoint_config": acl_projection("b"),
        "endpoint_token": acl_projection("c"),
        "install_id": acl_projection("d"),
        "cad_ownership": None,
        "cad_lease": None,
        "roots": {
            "package_root": acl_projection("e", directory=True),
            "output_root": acl_projection("f", directory=True),
            "state_root": acl_projection("1", directory=True),
            "endpoint_root": acl_projection("2", directory=True),
        },
    }
    state = {
        "schema_version": recovery.WINDOWS_RECOVERY_SCHEMA,
        "ssh_host": "windows-fixture",
        "task": {
            "task_path": "\\",
            "task_name": "description-e2e-endpoint",
            "state": "Disabled",
            "xml_sha256": hashes["task.xml"],
            "semantic_sha256": "e" * 64,
            "principal": {
                "user": "fixture-endpoint",
                "sid": "S-1-5-21-1",
                "logon_type": "InteractiveToken",
                "run_level": "HighestAvailable",
                "group_id": "",
                "administrator": True,
            },
            "action": {
                "execute": generation + "/runtime/Scripts/python.exe",
                "arguments": '-I -B -m description_pipeline serve --config "' + generation + '/endpoint.json"',
                "working_directory": "C:/description/state",
            },
            "settings": {"multiple_instances": "IgnoreNew"},
            "triggers_sha256": "f" * 64,
            "security_descriptor_sha256": "1" * 64,
        },
        "identity": {
            "operator_sid": "S-1-5-21-1",
            "operator_administrator": True,
            "runtime_sid": "S-1-5-21-1",
            "runtime_administrator": True,
            "runtime_privilege_qualification": "user-approved-administrator-runtime",
            "runtime_launch_contract": recovery.WINDOWS_PYTHON_LAUNCH_CONTRACT,
            "endpoint_root": generation,
            "config_path": paths["endpoint_config"],
            "config_sha256": hashes["endpoint.json"],
            "config_digest": "2" * 64,
            "package_root": "C:/description/packages",
            "output_root": "C:/description/results",
            "state_root": "C:/description/state",
            "port": 8765,
            "token_path": paths["endpoint_token"],
            "token_sha256": hashes["token"],
            "install_id_path": paths["install_id"],
            "install_id": "win-" + "1" * 32,
            "release_identity_sha256": "3" * 64,
            "domain_environment": {
                "DESCRIPTION_ENDPOINT_ROOT": generation,
                "DESCRIPTION_CAD_OWNERSHIP_FILE": "C:/description/state/cad-ownership.json",
                "scope": "user",
            },
        },
        "files": files,
        "acl": acl,
        "cad": {
            "ownership_path": "C:/description/state/cad-ownership.json",
            "ownership_sha256": None,
            "ledger_present": False,
            "lease_path": "C:/description/state/cad-ownership.json.lease",
            "lease_held": False,
            "external_detected": False,
            "problems": [],
        },
        "members": [{"member": row["member"], "bytes": row["bytes"], "sha256": row["sha256"]} for row in files],
    }
    bundle = staging / recovery.WINDOWS_BUNDLE_MEMBER
    bundle.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with zipfile.ZipFile(bundle, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr(recovery.WINDOWS_STATE_MEMBER, recovery.canonical_json(state))
        for name, payload in payloads.items():
            archive.writestr(name, payload)
    block = {
        "schema_version": recovery.WINDOWS_RECOVERY_SCHEMA,
        "bundle": {
            "member": recovery.WINDOWS_BUNDLE_MEMBER,
            "bytes": bundle.stat().st_size,
            "sha256": recovery.sha256_file(bundle),
        },
        "state_sha256": recovery._digest_document(state),
        "state": state,
        "remote_cleanup": {
            "attempted": True,
            "passed": True,
            "remote_dir": "C:/description/state/recovery/snapshot/windows-fixture",
        },
        "windows_cleanup": None,
    }
    return block, state


def windows_snapshot_receipt(
    *,
    remote_cleanup: dict | None = None,
    inner_cleanup: dict | None = None,
) -> dict:
    receipt = {
        "passed": True,
        "verb": "recovery-snapshot",
        "task_state": "Disabled",
        "endpoint": "stopped",
        "cad_ownership": "free",
        "remote_cleanup": remote_cleanup
        or {
            "attempted": True,
            "passed": True,
            "remote_dir": "C:/description/state/recovery/snapshot/windows-fixture",
        },
    }
    if inner_cleanup is not None:
        receipt["cleanup"] = inner_cleanup
    return receipt


def fake_capture_windows_recovery(context, *, backup_id, staging, commands):
    del backup_id, commands
    block, _state = windows_recovery_fixture(context, staging)
    return block


def fake_restore_windows_recovery(
    *,
    manifest,
    plan,
    extracted,
    status_dir,
    verify_only,
    commands,
    timeout=3600,
):
    del plan, extracted, verify_only, commands, timeout
    document = {
        "passed": True,
        "verb": "recovery-restore",
        "task_state": "Disabled",
        "endpoint": "stopped",
        "cad_ownership": "free",
        "state_sha256": manifest["windows"]["state_sha256"],
    }
    write(status_dir / "windows-recovery.json", json.dumps(document))
    return document


def permission(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


PSQL = r"""#!/usr/bin/env python3
import hashlib
import json
import os
import pathlib
import re
import sys

args = sys.argv[1:]
if "--version" in args:
    print("psql (PostgreSQL) 14.24")
    raise SystemExit(0)

def option(name):
    return args[args.index(name) + 1] if name in args else None

query = option("-c")
file_arg = option("-f")
socket_dir = option("-h") or ""
data_dir = pathlib.Path(socket_dir).parent / "data" if socket_dir else pathlib.Path("/fixture/data")
target = "target" in socket_dir
identity = {
    "data_directory": str(data_dir),
    "system_identifier": "2222222222" if target else "1111111111",
}
roles = [
    {
        "name": name,
        "superuser": name == "solidworks",
        "inherit": True,
        "createrole": False,
        "createdb": False,
        "canlogin": True,
        "replication": False,
        "bypassrls": False,
        "connection_limit": -1,
        "valid_until": None,
        "password_verifier": None,
        "settings": [],
    }
    for name in (
        "solidworks",
        "platform_migrate",
        "platform_service",
        "operator_ro",
        "airflow_migrate",
        "airflow_runtime",
    )
]
catalog = {
    "database": {
        "name": "airflow_meta",
        "owner": "solidworks",
        "encoding": "UTF8",
        "collate": "C",
        "ctype": "C",
        "acl_is_null": True,
        "acl": [],
    },
    "schemas": [
        {"name": "airflow", "owner": "airflow_migrate", "acl": []},
        {"name": "platform", "owner": "platform_migrate", "acl": []},
        {"name": "public", "owner": "solidworks", "acl": []},
    ],
    "settings": [],
    "tables": [
        {
            "schema": "platform",
            "name": name,
            "kind": "r",
            "owner": "platform_migrate",
            "acl": [],
        }
        for name in ("admission", "operation", "request", "run", "run_event", "stage")
    ]
    + [
        {
            "schema": "airflow",
            "name": name,
            "kind": "r",
            "owner": "airflow_migrate",
            "acl": [],
        }
        for name in ("alembic_version", "connection")
    ],
    "public_create": False,
    "platform_service_airflow_usage": True,
    "platform_service_airflow_create": False,
}
full_catalog = {
    "schemas": [
        {"name": "airflow", "owner": "airflow_migrate", "acl": None},
        {"name": "platform", "owner": "platform_migrate", "acl": None},
        {"name": "public", "owner": "solidworks", "acl": None},
    ],
    "relations": [],
    "columns": [],
    "constraints": [],
    "triggers": [],
    "functions": [],
    "types": [],
    "extensions": [],
    "default_acls": [],
}

if query:
    if "'databases'" in query and "'roles'" in query:
        payload = {
            "server_version_num": "140024",
            **identity,
            "databases": [],
            "roles": ["solidworks"],
        }
    elif (
        "current_setting('server_version_num')" in query
        and "database_oid" not in query
    ):
        payload = {
            "server_version_num": "140024",
            **identity,
        }
    elif "left(rolname, 3) <> 'pg_'" in query:
        payload = [
            "solidworks",
            "platform_migrate",
            "platform_service",
            "operator_ro",
            "airflow_migrate",
            "airflow_runtime",
        ]
    elif "FROM pg_authid" in query:
        payload = roles
    elif "FROM pg_auth_members" in query:
        payload = []
    elif "'relations'" in query and "'columns'" in query:
        payload = full_catalog
    elif "'table', c.relname" in query and "pg_class" in query:
        payload = [
            {"schema": schema, "table": table}
            for schema, table in (
                ("platform", "admission"),
                ("platform", "request"),
                ("platform", "run"),
                ("platform", "stage"),
                ("platform", "operation"),
                ("platform", "run_event"),
                ("airflow", "alembic_version"),
                ("airflow", "connection"),
            )
        ]
    elif "'rows', count(*)" in query:
        match = re.search(r"FROM (platform|airflow)\.([a-z_]+) t", query)
        table = ".".join(match.groups()) if match else "unknown"
        payload = {
            "rows": 1,
            "fingerprint": hashlib.sha256(table.encode()).hexdigest()[:32],
        }
    elif "FROM pg_sequences" in query:
        payload = []
    elif "FROM airflow.alembic_version" in query:
        payload = ["head-1"]
    elif "FROM airflow.connection" in query:
        payload = {
            "count": 1,
            "password": "__FERNET_TOKEN__",
            "extra": None,
        }
    elif "FROM platform.stage" in query:
        fixture = next((parent / "checkpoint-fixture.json" for parent in pathlib.Path(socket_dir).parents
                        if (parent / "checkpoint-fixture.json").is_file()), None)
        payload = json.loads(fixture.read_text()) if fixture is not None else []
    elif "'catalog'" not in query and "'database', current_database()" in query:
        payload = {
            "database": "airflow_meta",
            "database_oid": "16384",
            "role": option("-U"),
            "server_version_num": "140024",
        }
    else:
        payload = catalog
    print(json.dumps(payload, sort_keys=True))
elif file_arg:
    pathlib.Path(file_arg).read_text(encoding="utf-8")
"""


PG_CONTROL = r"""#!/usr/bin/env python3
import sys

args = sys.argv[1:]
data = args[args.index("-D") + 1]
identifier = "2222222222" if "target" in data else "1111111111"
print(f"Database system identifier:           {identifier}")
print("Catalog version number:               202107181")
"""


PG_DUMPALL = r"""#!/usr/bin/env python3
import pathlib
import sys

args = sys.argv[1:]
path = pathlib.Path(args[args.index("-f") + 1])
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text("CREATE ROLE solidworks;\n", encoding="utf-8")
"""


PG_DUMP = r"""#!/usr/bin/env python3
import pathlib
import sys

args = sys.argv[1:]
path = pathlib.Path(args[args.index("-f") + 1])
path.parent.mkdir(parents=True, exist_ok=True)
path.write_bytes(b"fake custom dump")
"""


PG_RESTORE = r"""#!/usr/bin/env python3
import pathlib
import sys

args = sys.argv[1:]
if "-l" in args:
    print("; Archive created by fake pg_dump")
    raise SystemExit(0)
socket_dir = pathlib.Path(args[args.index("-h") + 1])
(socket_dir.parent / "pg-restore-complete").write_text("ok\n", encoding="utf-8")
"""

FAKE_SNAPSHOT_WORKER = r"""#!/usr/bin/env python3
import hashlib
import json
import pathlib
import sys
import time

args = sys.argv[1:]
sys.path.insert(0, str(pathlib.Path(args[0]).resolve().parent))
import recovery
request = json.loads(pathlib.Path(args[args.index("--request") + 1]).read_text())
reference_fixture = pathlib.Path(args[args.index("--request") + 1]).parents[2] / "checkpoint-fixture.json"
references = json.loads(reference_fixture.read_text()) if reference_fixture.is_file() else []
control = pathlib.Path(request["control_dir"])
staging = pathlib.Path(request["staging"])

def write_json(path, document):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, sort_keys=True) + "\n", encoding="utf-8")

def wait(name):
    path = control / name
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if path.is_file():
            return
        time.sleep(0.01)
    raise SystemExit(f"marker timeout: {name}")

roles = [
    {
        "name": name,
        "superuser": name == "solidworks",
        "inherit": True,
        "createrole": False,
        "createdb": False,
        "canlogin": True,
        "replication": False,
        "bypassrls": False,
        "connection_limit": -1,
        "valid_until": None,
        "password_verifier": None,
        "settings": [],
    }
    for name in (
        "solidworks",
        "platform_migrate",
        "platform_service",
        "operator_ro",
        "airflow_migrate",
        "airflow_runtime",
    )
]
catalog = {
    "database": {
        "name": "airflow_meta",
        "owner": "solidworks",
        "encoding": "UTF8",
        "collate": "C",
        "ctype": "C",
        "acl_is_null": True,
        "acl": [],
    },
    "schemas": [
        {"name": "airflow", "owner": "airflow_migrate", "acl": []},
        {"name": "platform", "owner": "platform_migrate", "acl": []},
        {"name": "public", "owner": "solidworks", "acl": []},
    ],
    "settings": [],
    "tables": [
        {
            "schema": "platform",
            "name": name,
            "kind": "r",
            "owner": "platform_migrate",
            "acl": [],
        }
        for name in ("admission", "operation", "request", "run", "run_event", "stage")
    ]
    + [
        {
            "schema": "airflow",
            "name": name,
            "kind": "r",
            "owner": "airflow_migrate",
            "acl": [],
        }
        for name in ("alembic_version", "connection")
    ],
    "public_create": False,
    "platform_service_airflow_usage": True,
    "platform_service_airflow_create": False,
}
full_catalog = {
    "schemas": [
        {"name": "airflow", "owner": "airflow_migrate", "acl": None},
        {"name": "platform", "owner": "platform_migrate", "acl": None},
        {"name": "public", "owner": "solidworks", "acl": None},
    ],
    "relations": [],
    "columns": [],
    "constraints": [],
    "triggers": [],
    "functions": [],
    "types": [],
    "extensions": [],
    "default_acls": [],
}
table_names = (
    "platform.admission",
    "platform.request",
    "platform.run",
    "platform.stage",
    "platform.operation",
    "platform.run_event",
    "airflow.alembic_version",
    "airflow.connection",
)
data = {
    "tables": {
        name: {
            "rows": 1,
            "fingerprint": hashlib.sha256(name.encode()).hexdigest()[:32],
        }
        for name in table_names
    },
    "sequences": [],
    "airflow_alembic_heads": ["head-1"],
}
maintenance = {
    "admission": {
        "state": "closed",
        "reason": "fixture",
        "actor": "fixture",
        "revision": 1,
    },
    "settled": True,
    "unsettled_count": 0,
    "active_application_sessions": [],
    "wal_lsn": "0/100",
}
role_hash = hashlib.sha256(
    recovery.canonical_json(
        {
            "roles": roles,
            "memberships": [],
            "database_settings": [],
            "non_system_roles": sorted(row["name"] for row in roles),
        }
    )
).hexdigest()
sequence_hash = hashlib.sha256(recovery.canonical_json([])).hexdigest()
try:
    write_json(
        control / "maintenance.ready.json",
        {
            "status": "ready",
            "lock_key": "5933003610084487217",
            "maintenance": maintenance,
            "roles_sha256": role_hash,
        },
    )
    database = staging / "database"
    database.mkdir(parents=True)
    role_plan = {
        "bootstrap_role": "solidworks",
        "application_roles": [
            "platform_migrate",
            "platform_service",
            "operator_ro",
            "airflow_migrate",
            "airflow_runtime",
        ],
        "roles": roles,
        "memberships": [],
    }
    write_json(
        database / "cluster-roles.json",
        role_plan,
    )
    globals_text = recovery.role_sql(role_plan)
    (database / "cluster-globals.sql").write_text(globals_text, encoding="utf-8")
    write_json(
        database / "catalog-baseline.json",
        {
            "database": catalog["database"],
            "settings": catalog["settings"],
            "public_create": catalog["public_create"],
            "platform_service_airflow_usage": catalog[
                "platform_service_airflow_usage"
            ],
            "platform_service_airflow_create": catalog[
                "platform_service_airflow_create"
            ],
            "catalog": full_catalog,
        },
    )
    write_json(database / "data-baseline.json", data)
    write_json(control / "role-settings-before.json", role_plan)
    write_json(control / "sequence-before.json", [])
    write_json(
        control / "snapshot.ready.json",
        {"schema_version": recovery.SNAPSHOT_READY_SCHEMA,
         "status": "ready", "snapshot_id": "00000001-00000001-1",
         "checkpoint_refs": references, "checkpoint_refs_sha256": recovery._digest_document(references),
         "database_bytes": 0},
    )
    wait("inventory.complete.json")
    wait("files.complete.json")
    write_json(database / "reference-index.json", recovery.checkpoint_index(references, staging / "state/runs"))
    selection = json.loads((staging / "inventory/selection.json").read_text())
    (database / "airflow_meta.dump").write_bytes(b"fake custom dump")
    (database / "dump.toc").write_text("; fake toc\n", encoding="utf-8")
    write_json(database / "sequence-setval.json", [])
    write_json(control / "sequence-dump-setval.json", [])
    write_json(control / "sequence-after.json", [])
    write_json(control / "role-settings-after.json", role_plan)
    write_json(control / "dump.complete.json", {"status": "complete"})
    wait("inventory.after.complete.json")
    consistency = {
        "isolation": "REPEATABLE READ READ ONLY",
        "closure": recovery.closure_binding(selection),
        "snapshot_id": "00000001-00000001-1",
        "snapshot_held_until_pg_dump_complete": True,
        "roles_snapshot_before_sha256": role_hash,
        "roles_snapshot_after_sha256": role_hash,
        "roles_live_before_sha256": role_hash,
        "roles_live_after_sha256": role_hash,
        "sequence_before_sha256": sequence_hash,
        "sequence_after_sha256": sequence_hash,
        "sequence_dump_setval_sha256": sequence_hash,
        "sequence_extraction": {
            "method": "pg_restore --data-only -L=<per-sequence-toc> -f=-",
            "plaintext_dump_file_created": False,
            "full_dump_materialized": False,
            "temporary_toc_files_removed": True,
            "per_sequence": [],
        },
        "cluster_globals_sha256": hashlib.sha256(globals_text.encode()).hexdigest(),
        "database_baseline_sha256": hashlib.sha256(
            recovery.canonical_json(data)
        ).hexdigest(),
        "database_live_after_dump_sha256": hashlib.sha256(
            recovery.canonical_json(data)
        ).hexdigest(),
        "database_live_after_inventory_sha256": hashlib.sha256(
            recovery.canonical_json(data)
        ).hexdigest(),
        "maintenance_before": maintenance,
        "maintenance_after": maintenance,
        "exclusive_lock_key": "5933003610084487217",
        "exclusive_lock_held": True,
    }
    write_json(
        control / "capture.completed.json",
        {
            "status": "completed",
            "airflow_alembic_head": "head-1",
            "consistency": consistency,
            "commands": [],
        },
    )
except Exception as error:
    write_json(
        control / "capture.failed.json",
        {
            "code": "recovery_snapshot_worker_failed",
            "message": str(error),
            "detail": {},
        },
    )
    raise
"""


def install_fake_pg(bin_dir: Path, fernet_token: str) -> Path:
    bin_dir.mkdir(parents=True, exist_ok=True)
    for name, source in (
        ("psql", PSQL.replace("__FERNET_TOKEN__", fernet_token)),
        ("pg_controldata", PG_CONTROL),
        ("pg_dumpall", PG_DUMPALL),
        ("pg_dump", PG_DUMP),
        ("pg_restore", PG_RESTORE),
    ):
        path = write(bin_dir / name, source, mode=0o755)
        path.chmod(0o755)
    return bin_dir


def fake_maintenance_state(
    context: recovery.BackupContext,
    database: dict,
    *,
    operatorctl_path: Path,
    timeout: int,
    maintenance_lock: recovery.MaintenanceLock | None = None,
) -> dict:
    del database, timeout
    assert maintenance_lock is not None
    operatorctl_path = Path(operatorctl_path).resolve()
    probe = {
        "admission": {
            "state": "closed",
            "reason": "fixture",
            "actor": "fixture",
            "revision": 1,
        },
        "settled": True,
        "unsettled_count": 0,
        "active_application_sessions": [],
        "wal_lsn": "0/100",
    }
    bash = shutil.which("bash") or "/bin/bash"
    command = {
        "argv": [
            bash,
            str(operatorctl_path),
            "drain",
            "--config",
            str(context.config_path),
        ],
        "returncode": 0,
        "stdout_sha256": hashlib.sha256(b"").hexdigest(),
        "stderr_sha256": hashlib.sha256(b"").hexdigest(),
    }
    linux = [{"unit": unit, "active_state": "inactive"} for unit in recovery.MAINTENANCE_UNITS]
    return {
        "opened_at": "2026-10-20T00:00:00Z",
        "operatorctl_path": str(operatorctl_path),
        "operatorctl_sha256": hashlib.sha256(operatorctl_path.read_bytes()).hexdigest(),
        "drain": command,
        "stop": {
            **command,
            "argv": [
                bash,
                str(operatorctl_path),
                "stop",
                "--config",
                str(context.config_path),
                "--keep-database",
            ],
        },
        "linux": linux,
        "windows": {
            "task_name": "description-e2e-endpoint",
            "state": "Disabled",
            "evidence": [command],
        },
        "database_before": probe,
    }


def fake_maintenance_receipt(
    context: recovery.BackupContext,
    database: dict,
    maintenance: dict,
    *,
    backup_id: str,
    timeout: int,
) -> dict:
    del timeout
    probe = maintenance["database_before"]
    return {
        "schema_version": recovery.MAINTENANCE_RECEIPT_SCHEMA,
        "status": "stopped",
        "backup_id": backup_id,
        "opened_at": maintenance["opened_at"],
        "closed_at": "2026-10-20T00:00:01Z",
        "identity": {
            "install_id": context.install_record["install_id"],
            "config_sha256": hashlib.sha256(context.config_path.read_bytes()).hexdigest(),
            "operatorctl_path": maintenance["operatorctl_path"],
            "operatorctl_sha256": maintenance["operatorctl_sha256"],
            "release": {
                "version": context.release_document["version"],
                "source_commit": context.release_document["source_commit"],
                "source_sha256": context.release_document["source_sha256"],
            },
        },
        "commands": {
            "drain": maintenance["drain"],
            "stop": maintenance["stop"],
        },
        "services": {
            "linux_before": maintenance["linux"],
            "linux_after": maintenance["linux"],
            "windows_before": maintenance["windows"],
            "windows_after": maintenance["windows"],
        },
        "database": {
            "authority": database["authority"],
            "before": probe,
            "after": probe,
        },
    }


def runtime_wheel_payloads(source_sha: str = "b" * 64) -> dict[str, bytes]:
    payloads = {
        "description_pipeline/__init__.py": b'VERSION = "1.0.0-test"',
        "description_pipeline/tool-release.json": json.dumps(
            {"source_sha256": source_sha, "version": "1.0.0", "source_commit": "a" * 40, "source_tree": "d" * 40},
            separators=(",", ":"),
        ).encode(),
        "mimicverse_description-1.0.0.dist-info/METADATA": (
            b"Metadata-Version: 2.1\nName: mimicverse-description\nVersion: 1.0.0\n"
        ),
        "apache_airflow-3.3.2.dist-info/METADATA": (b"Metadata-Version: 2.1\nName: apache-airflow\nVersion: 3.3.2\n"),
    }
    record_lines = []
    for name, payload in payloads.items():
        encoded = base64.urlsafe_b64encode(hashlib.sha256(payload).digest()).rstrip(b"=").decode("ascii")
        record_lines.append(f"{name},sha256={encoded},{len(payload)}")
    record_name = "mimicverse_description-1.0.0.dist-info/RECORD"
    record_lines.append(f"{record_name},,")
    payloads[record_name] = ("\n".join(record_lines) + "\n").encode()
    return payloads


def write_runtime_wheel(path: Path, source_sha: str = "b" * 64) -> dict[str, bytes]:
    payloads = runtime_wheel_payloads(source_sha)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with zipfile.ZipFile(path, "w") as archive:
        for name, payload in payloads.items():
            archive.writestr(name, payload)
    path.chmod(0o600)
    return payloads


def runtime_installer(venv: Path, source_sha: str = "b" * 64) -> str:
    site = venv / "lib" / "python3.12" / "site-packages"
    package = site / "description_pipeline"
    lines = [
        "#!/bin/sh",
        "set -eu",
        ': "${AIRFLOW_PYTHON:=python3.12}"',
        '"$AIRFLOW_PYTHON" -m venv "$AIRFLOW_VENV"',
        f'mkdir -p "{package}"',
        f'mkdir -p "{site / "mimicverse_description-1.0.0.dist-info"}"',
        f'mkdir -p "{site / "apache_airflow-3.3.2.dist-info"}"',
        f'mkdir -p "{site / "airflow"}"',
    ]
    for name, payload in runtime_wheel_payloads(source_sha).items():
        target = site / name
        encoded = base64.b64encode(payload).decode("ascii")
        lines.append(f"printf '%s' '{encoded}' | base64 -d > {str(target)!r}")
    lines.extend(
        [
            f"printf '%s' '# airflow' > {str(site / 'airflow' / '__init__.py')!r}",
            f"printf '%s\\n' '#!/bin/sh' > {str(venv / 'bin' / 'airflow')!r}",
            f"printf '%s\\n' 'echo 3.3.2' >> {str(venv / 'bin' / 'airflow')!r}",
            f"chmod +x {str(venv / 'bin' / 'airflow')!r}",
            f"printf '%s\\n' '#!/bin/sh' > {str(venv / 'bin' / 'description')!r}",
            f"printf '%s\\n' 'echo 1.0.0' >> {str(venv / 'bin' / 'description')!r}",
            f"chmod +x {str(venv / 'bin' / 'description')!r}",
            "",
        ]
    )
    return "\n".join(lines)


def make_context(root: Path, fake_bin: Path) -> recovery.BackupContext:
    source = root / "source"
    postgres_root = root / "fixture-pg"
    state_root = source / "state"
    handoff_root = source / "handoffs"
    artifact_root = source / "artifacts"
    workspace_root = source / "workspaces"
    fake_venv = root / "fake-airflow-venv"
    release_root = source / "releases"
    spec_root = source / "specifications"
    config_path = source / "platform.json"
    certificate = source / "secrets" / "tls.crt"
    private_key = source / "secrets" / "tls.key"
    dsn_file = source / "secrets" / "postgres.dsn"
    feishu_secret = source / "secrets" / "feishu.json"
    endpoint_token = source / "secrets" / "endpoint.token"
    socket_dir = source / "airflow-pg" / "socket"
    socket_dir.mkdir(parents=True)
    (postgres_root / "data").mkdir(parents=True)
    write(postgres_root / "data" / "PG_VERSION", "14\n")
    workspace_root.mkdir(parents=True)

    task_name = "description-e2e-endpoint"
    write(
        config_path,
        json.dumps(
            {
                "windows": {
                    "ssh_host": "windows-fixture",
                    "endpoint_port": 8765,
                    "instance": {
                        "task_name": task_name,
                        "package_root": "C:/description/packages",
                        "output_root": "C:/description/results",
                        "state_root": "C:/description/state",
                    },
                }
            }
        )
        + "\n",
    )
    snapshot = write(state_root / "snapshots" / "install-a.json", '{"snapshot":"a","install_id":"install-a"}\n')
    record = install_record_fixture(state_root, "install-a", snapshot)
    write(state_root / "install-record.json", json.dumps(record) + "\n")
    write(state_root / "install-records" / "install-a.json", json.dumps(record) + "\n")
    write(state_root / "promotions" / "README", "empty history marker\n")
    write(state_root / "resolved.env", "OPERATOR_STATE=/fixture/state\n")
    write(state_root / "portal.json", '{"fixture":"portal"}\n')
    write(state_root / "feishu.env", "FEISHU_REDIRECT_URI=https://fixture.invalid\n")
    write(state_root / "nginx" / "nginx.conf", "events {}\n")

    fernet_key = Fernet.generate_key().decode()
    fernet_token = Fernet(fernet_key.encode()).encrypt(b"fixture-windows-password").decode()
    write(fake_bin / "psql", PSQL.replace("__FERNET_TOKEN__", fernet_token), mode=0o755)
    (fake_bin / "psql").chmod(0o755)
    write(
        state_root / "airflow-home" / "airflow.cfg",
        textwrap.dedent(
            f"""\
            [core]
            fernet_key = {fernet_key}

            [webserver]
            jwt_secret = fixture-jwt-secret
            """
        ),
    )
    install_id = "install-a"
    store_root = state_root / "runs"
    write(store_root / "sealed" / "run-1" / "capture" / "record.json", '{"ok":true}\n')
    write(
        state_root / "derived" / install_id / "operator.env",
        "\n".join(
            (
                f"AIRFLOW_DB_URL=postgresql+psycopg2://airflow_migrate@/airflow_meta?host={socket_dir}&port=55432",
                f"AIRFLOW_RUNTIME_DB_URL=postgresql+psycopg2://airflow_runtime@/airflow_meta?host={socket_dir}&port=55432",
                f"PLATFORM_SERVICE_DSN=postgresql://platform_service@/airflow_meta?host={socket_dir}&port=55432",
                f"PLATFORM_OPERATOR_DSN=postgresql://operator_ro@/airflow_meta?host={socket_dir}&port=55432",
                f"POSTGRES_ROOT={postgres_root}",
                "POSTGRES_MAJOR=14",
                f"PIPELINE_STORE_ROOT={store_root}",
                f"AIRFLOW_HOME={state_root / 'airflow-home'}",
                f"AIRFLOW_VENV={release_root / ('airflow-venv-' + install_id)}",
                f"PIPELINE_WHEEL={release_root / 'dist.whl'}",
                "",
            )
        ),
    )
    write(fake_venv / "bin" / "python", FAKE_SNAPSHOT_WORKER, mode=0o755)
    (fake_venv / "bin" / "python").chmod(0o755)
    write(handoff_root / "request.json", '{"handoff":true}\n')
    write(artifact_root / "artifact.bin", b"artifact")
    write(spec_root / "spec.md", "specification\n")
    wheel_name = "mimicverse_description-1.0.0-test-py3-none-any.whl"
    wheel_path = release_root / wheel_name
    write_runtime_wheel(wheel_path)
    wheel_bytes = wheel_path.read_bytes()
    readme_bytes = b"runtime-role\n"
    linux_name = "description-1.0.0-test-linux-cp312-x86_64.zip"
    linux_path = release_root / linux_name
    with zipfile.ZipFile(linux_path, "w") as archive:
        lock_bytes = (
            f"mimicverse-description==1.0.0-test --hash=sha256:{hashlib.sha256(wheel_bytes).hexdigest()}\n"
        ).encode()
        archive.writestr(
            "files.json",
            json.dumps(
                {
                    "README.md": hashlib.sha256(readme_bytes).hexdigest(),
                    f"wheels/{wheel_name}": hashlib.sha256(wheel_bytes).hexdigest(),
                    "requirements.lock": hashlib.sha256(lock_bytes).hexdigest(),
                }
            ),
        )
        archive.writestr("README.md", readme_bytes)
        archive.writestr(f"wheels/{wheel_name}", wheel_bytes)
        archive.writestr("requirements.lock", lock_bytes)
    linux_path.chmod(0o600)
    deployment_name = "description-1.0.0-test-deployment.zip"
    deployment_path = release_root / deployment_name
    recovery_script = Path(recovery.__file__).read_bytes()
    installer = runtime_installer(release_root / ("airflow-venv-" + install_id))
    linux_lock = b"linux-lock\n"
    windows_lock = b"windows-lock\n"
    with zipfile.ZipFile(deployment_path, "w") as archive:
        archive.writestr("deploy/operator/recovery.py", recovery_script)
        archive.writestr("deploy/airflow/install.sh", installer)
        archive.writestr("requirements/linux-py312.lock", linux_lock)
        archive.writestr("requirements/windows-py312.lock", windows_lock)
    write(release_root / "deploy/airflow/install.sh", installer, mode=0o755)
    write(release_root / "deploy/operator/recovery.py", recovery_script, mode=0o755)
    write(release_root / "requirements/linux-py312.lock", linux_lock)
    write(release_root / "requirements/windows-py312.lock", windows_lock)
    deployment_path.chmod(0o600)
    release_document = {
        "schema_version": "solidworks-to-urdf.release/v1",
        "version": "1.0.0",
        "source_commit": "a" * 40,
        "source_tree": "d" * 40,
        "source_sha256": "b" * 64,
        "artifacts": [
            {
                "name": name,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
            for name, path in (
                (deployment_name, deployment_path),
                (linux_name, linux_path),
                (wheel_name, release_root / wheel_name),
            )
        ],
        "package_files": {
            name: hashlib.sha256(payload).hexdigest()
            for name, payload in runtime_wheel_payloads().items()
            if name.startswith("description_pipeline/") and name != "description_pipeline/tool-release.json"
        },
        "deployment_files": {
            "deploy/operator/recovery.py": hashlib.sha256(recovery_script).hexdigest(),
            "deploy/airflow/install.sh": hashlib.sha256(installer.encode()).hexdigest(),
            "requirements/linux-py312.lock": hashlib.sha256(linux_lock).hexdigest(),
            "requirements/windows-py312.lock": hashlib.sha256(windows_lock).hexdigest(),
        },
        "runtime_locks": {
            "linux": hashlib.sha256(linux_lock).hexdigest(),
            "windows": hashlib.sha256(windows_lock).hexdigest(),
        },
    }
    write(release_root / "release.json", json.dumps(release_document))
    dsn = f"postgresql://solidworks@/airflow_meta?host={socket_dir}&port=55432"
    for path, payload in (
        (certificate, "fixture-cert\n"),
        (private_key, "fixture-key\n"),
        (dsn_file, dsn + "\n"),
        (feishu_secret, '{"app_secret":"fixture"}\n'),
        (endpoint_token, "fixture-token\n"),
    ):
        write(path, payload)

    resolved = {
        "operator": {
            "certificate_file": str(certificate),
            "private_key_file": str(private_key),
        },
        "storage": {
            "state_root": str(state_root),
            "handoff_root": str(handoff_root),
            "artifact_root": str(artifact_root),
            "workspace_root": str(workspace_root),
            "release_root": str(release_root),
        },
        "database": {"dsn_file": str(dsn_file)},
        "windows": {
            "token_file": str(endpoint_token),
            "ssh_host": "windows-fixture",
            "endpoint_port": 8765,
            "instance": {
                "task_name": task_name,
                "package_root": "C:/description/packages",
                "output_root": "C:/description/results",
                "state_root": "C:/description/state",
            },
        },
        "auth": {"feishu_secret_file": str(feishu_secret)},
        "specifications": {"record_roots": [str(spec_root)]},
        "derived": {
            "store_root": str(store_root),
            "repositories": {},
            "discovery": {"record_roots": [str(spec_root)]},
        },
        "targets": {},
    }
    context = recovery.BackupContext(
        config_path=config_path,
        resolved=resolved,
        install_record={"install_id": install_id},
        snapshot={
            "install_id": install_id,
            "resolved": resolved,
        },
        release_document={
            "version": release_document["version"],
            "source_commit": release_document["source_commit"],
            "source_sha256": release_document["source_sha256"],
            "artifacts": release_document["artifacts"],
        },
        archive_root=release_root,
        archive_document=release_document,
        derived_env={
            "POSTGRES_ROOT": str(postgres_root),
            "POSTGRES_MAJOR": "14",
            "AIRFLOW_DB_URL": dsn,
            "AIRFLOW_RUNTIME_DB_URL": dsn,
            "PLATFORM_SERVICE_DSN": dsn,
            "PLATFORM_OPERATOR_DSN": dsn,
            "AIRFLOW_VENV": str(fake_venv),
        },
        workspace_plan={
            "schema_version": "solidworks-to-urdf.workspace-rehydrate/v2",
            "repositories": [],
        },
        source_code={
            "commit": "a" * 40,
            "tree": "d" * 40,
            "worktree": "release-archive",
        },
        host_id="source-host-fixture",
        postgres_root=postgres_root,
    )
    # The fake DB tools live under the configured portable PostgreSQL root.
    target_bin = postgres_root / "root" / "usr" / "lib" / "postgresql" / "14" / "bin"
    target_bin.parent.mkdir(parents=True, exist_ok=True)
    (postgres_root / "root" / "usr" / "lib" / "x86_64-linux-gnu").mkdir(
        parents=True,
        exist_ok=True,
    )
    if target_bin.exists():
        shutil.rmtree(target_bin)
    os.replace(fake_bin, target_bin)
    for name in ("psql", "pg_controldata", "pg_dumpall", "pg_dump", "pg_restore"):
        (target_bin / name).chmod(0o755)
    return context


class ArchiveMemberBindingTests(unittest.TestCase):
    def test_duplicate_members_are_rejected_even_when_identical(self) -> None:
        row = {
            "member": "config/state.json",
            "type": "file",
            "bytes": 2,
            "sha256": hashlib.sha256(b"{}").hexdigest(),
            "mode": 0o600,
        }
        recovery.verify_tar_rows_against_manifest([row], [row])
        for actual, expected in (([row, row], [row]), ([row], [row, row])):
            with self.subTest(actual_count=len(actual), expected_count=len(expected)):
                with self.assertRaises(recovery.RecoveryError) as raised:
                    recovery.verify_tar_rows_against_manifest(actual, expected)
                self.assertEqual(raised.exception.code, "recovery_archive_member_mismatch")

    def test_streaming_payload_verification_rejects_malformed_archives_without_extraction(self):
        expected = {
            "member": "payload",
            "type": "file",
            "bytes": 2,
            "sha256": hashlib.sha256(b"{}").hexdigest(),
            "mode": 0o600,
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            key = write(root / "key", "test-stream-key")
            for case in (
                "valid",
                "duplicate",
                "traversal",
                "symlink",
                "fifo",
                "wrong-mode",
                "wrong-payload",
                "truncated",
            ):
                with self.subTest(case=case):
                    stream = io.BytesIO()
                    with tarfile.open(fileobj=stream, mode="w") as tar:
                        info = tarfile.TarInfo("../payload" if case == "traversal" else "payload")
                        info.mode = 0o644 if case == "wrong-mode" else 0o600
                        info.size = 2
                        if case in {"symlink", "fifo"}:
                            info.type = tarfile.SYMTYPE if case == "symlink" else tarfile.FIFOTYPE
                            info.linkname = "outside"
                            info.size = 0
                        tar.addfile(info, io.BytesIO(b"[]" if case == "wrong-payload" else b"{}"))
                        if case == "duplicate":
                            tar.addfile(info, io.BytesIO(b"{}"))
                    encrypted = subprocess.run(
                        ["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-pass", f"file:{key}"],
                        input=stream.getvalue(),
                        capture_output=True,
                        check=True,
                    ).stdout
                    archive = write(root / "archive.enc", encrypted[:-32] if case == "truncated" else encrypted)
                    with mock.patch.object(
                        recovery.tempfile, "TemporaryDirectory", side_effect=AssertionError("no extraction")
                    ):
                        if case == "valid":
                            result = recovery.verify_archive_member_binding(
                                archive=archive,
                                key=key,
                                manifest_members=[expected],
                                staged_source_members={"payload": expected},
                                work_dir=root,
                            )
                            self.assertEqual(result["members"], 1)
                        else:
                            with self.assertRaises(recovery.RecoveryError):
                                recovery.verify_archive_member_binding(
                                    archive=archive,
                                    key=key,
                                    manifest_members=[expected],
                                    staged_source_members={"payload": expected},
                                    work_dir=root,
                                )


class RecoverySelectionTests(unittest.TestCase):
    def reference(self, run="producer", parent=None, state="completed"):
        return {
            "run_id": run,
            "stage": "capture",
            "stage_state": state,
            "request_id": "request-1",
            "run_parent_run_id": parent,
            "reused_from_run": parent,
            "original_executed_at": "2026-10-22T00:00:00Z",
            "checkpoint_uri": "store://sealed/producer/capture",
            "receipt": {
                "schema_version": "solidworks-to-urdf.bundle/v1",
                "run_id": "producer",
                "stage": "capture",
                "state": "native_complete" if state == "completed" else state,
                "passed": False,
                "artifacts": [],
            },
        }

    def test_multihop_reuse_resolves_one_physical_producer(self):
        refs = [self.reference(), self.reference("child", "producer"), self.reference("grandchild", "child")]
        self.assertEqual(set(recovery.checkpoint_origins(refs).values()), {"producer"})
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "runs"
            tree = source / "sealed/producer/capture"
            from description_pipeline.delivery import SUBJECT_DIRECTORIES, write_delivery_manifest
            from description_pipeline.interface_records import CHECK_POLICY_SCHEMA, check_policy_digest
            from description_pipeline.stages import CONTRACT, CONTRACT_SHA256

            for name in SUBJECT_DIRECTORIES:
                (tree / name).mkdir(parents=True, exist_ok=True)
            write(tree / "tool.json", "{}")
            manifest = write_delivery_manifest(
                tree,
                contract={"schema_version": CONTRACT["schema_version"], "digest": CONTRACT_SHA256},
                policy={"schema_version": CHECK_POLICY_SCHEMA, "digest": check_policy_digest()},
            )
            for ref in refs:
                ref["receipt"]["artifacts"] = manifest["subject"]["files"]
            write(source / "sealed/orphan/capture/large.bin", b"x" * 10000)
            indexed = recovery.checkpoint_index(refs, source)
            destination = Path(directory) / "copy"
            recovery.copy_selected_root(source, destination, sorted({row["tree_relative"] for row in indexed}))
            self.assertEqual(len(list(destination.rglob("tool.json"))), 1)
            self.assertFalse((destination / "sealed/orphan").exists())
            write(tree / "unregistered.bin", "unexpected")
            with self.assertRaises(recovery.RecoveryError):
                recovery.checkpoint_index(refs, source)

    def test_failed_checkpoint_is_preserved_but_cannot_be_reused(self):
        failed = self.reference(state="failed")
        self.assertEqual(recovery.checkpoint_origins([failed]), {("producer", "capture"): "producer"})
        with self.assertRaises(recovery.RecoveryError):
            recovery.checkpoint_origins([failed, self.reference("child", "producer")])
        changed = self.reference("child", "producer", "failed")
        with self.assertRaises(recovery.RecoveryError):
            recovery.checkpoint_origins([self.reference(), changed])

    def test_reuse_wrong_parent_receipt_uri_and_cycle_are_rejected(self):
        cases = []
        child = self.reference("child", "producer")
        for field, value in (
            ("run_parent_run_id", "other"),
            ("receipt", {"changed": True}),
            ("checkpoint_uri", "store://sealed/other/capture"),
            ("original_executed_at", "changed"),
        ):
            cases.append([self.reference(), {**child, field: value}])
        cases.append([self.reference("a", "b"), self.reference("b", "a")])
        cases.append([child])
        for refs in cases:
            with self.subTest(refs=refs), self.assertRaises(recovery.RecoveryError):
                recovery.checkpoint_origins(refs)

    def test_journal_selection_keeps_only_typed_edges_and_replays_offline(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "state"
            snapshot = write(root / "snapshots/current.json", '{"install_id":"current"}')
            write(root / "promotions/current/evidence.json", "{}")
            write(root / "promotions/orphan/backup.tar.enc", b"x" * 10000)
            record = install_record_fixture(root, "current", snapshot)
            write(root / "install-record.json", json.dumps(record))
            write(root / "derived/current/operator.env", "")
            edges = recovery.journal_selection(root)
            self.assertEqual({row["source_path"] for row in edges}, {str(snapshot)})
            destination = Path(directory) / "archive/config/state"
            recovery.copy_selected_root(
                root, destination, ["install-record.json", "snapshots", "derived", "promotions/current/evidence.json"]
            )
            self.assertFalse((destination / "promotions/orphan").exists())
            self.assertEqual(
                recovery.journal_selection(root, extracted=Path(directory) / "archive", expected=edges), edges
            )
            snapshot.unlink()
            with self.assertRaises(recovery.RecoveryError):
                recovery.journal_selection(root)

    def test_old_schema_refused_before_status_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = write(
                root / "recovery-manifest.json", '{"schema_version":"solidworks-to-urdf.recovery-manifest/v5"}'
            )
            write(root / "recovery-manifest.sha256", recovery.sha256_file(manifest))
            with self.assertRaises(recovery.RecoveryError) as raised:
                recovery.restore_command(
                    manifest_path=manifest, key=root / "key", target_plan_path=root / "plan", status_dir=root / "status"
                )
            self.assertEqual(raised.exception.code, "recovery_schema_unsupported")
            self.assertFalse((root / "status").exists())
        with self.assertRaises(recovery.RecoveryError) as raised:
            recovery.validate_target_plan({"schema_version": "solidworks-to-urdf.recovery-target/v2"}, {}, "a" * 64)
        self.assertEqual(raised.exception.code, "recovery_schema_unsupported")

    def test_journal_requires_canonical_record_and_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = write(root / "snapshots/current.json", '{"install_id":"current"}')
            write(root / "derived/current/operator.env", "")
            health = write(root / "promotions/current/health.json", "{}")
            record = install_record_fixture(root, "current", snapshot)
            record["journal"] = [journal_row_fixture([str(health)])]
            write(root / "install-record.json", json.dumps(record))
            with self.assertRaises(recovery.RecoveryError):
                recovery.journal_selection(root)
            receipt = write(root / "promotions/current/receipt.json", '{"schema_version":"wrong"}')
            record["journal"] = [journal_row_fixture([str(receipt)])]
            write(root / "install-record.json", json.dumps(record))
            with self.assertRaises(recovery.RecoveryError):
                recovery.journal_selection(root)
            record["journal"] = []
            del record["schema_version"]
            write(root / "install-record.json", json.dumps(record))
            with self.assertRaises(recovery.RecoveryError):
                recovery.journal_selection(root)

    def test_snapshot_filename_must_match_install_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = write(root / "snapshots/wrong.json", '{"install_id":"current"}')
            write(root / "derived/current/operator.env", "")
            record = install_record_fixture(root, "current", snapshot)
            write(root / "install-record.json", json.dumps(record))
            with self.assertRaises(recovery.RecoveryError) as raised:
                recovery.journal_selection(root)
            self.assertEqual(raised.exception.code, "recovery_journal_invalid")

    def test_capacity_refuses_before_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(recovery.shutil, "disk_usage", return_value=type("Usage", (), {"free": 0})()):
                with self.assertRaises(recovery.RecoveryError) as raised:
                    recovery.require_capacity([(Path(directory) / "new", 10)])
                self.assertEqual(raised.exception.code, "recovery_capacity_insufficient")
            self.assertFalse((Path(directory) / "new").exists())

    def test_shell_rollback_missing_archive_has_no_following_side_effect(self):
        source = (DEPLOY / "operatorctl.sh").read_text()
        functions = "\n".join(
            re.search(r"(?ms)^" + name + r"\(\) \{.*?^\}", source).group()
            for name in ("require_rollback_archive", "config_reference_recorded")
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            receipt = root / "receipt.json"
            receipt.write_text(json.dumps({"steps": [{"verb": "promote", "step": "config-reference"}]}))
            script = (
                functions
                + '\ndie() { echo "$*" >&2; exit 17; }\n'
                + 'CONTROL_PYTHON="$1"\nrequire_rollback_archive "$2"\nprintf "side-effect" > "$2/mutated"\n'
            )
            result = subprocess.run(
                ["bash", "-s", "--", sys.executable, str(root)], input=script, text=True, capture_output=True
            )
            self.assertEqual(result.returncode, 17)
            self.assertIn("recovery_history_unavailable", result.stderr)
            self.assertFalse((root / "mutated").exists())

    def test_windows_json_duplicate_keys_are_rejected(self):
        with self.assertRaises(recovery.RecoveryError):
            recovery.strict_json_bytes(b'{"state":{"a":1,"a":2}}', "Windows reply")
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory) / "recovery.zip"
            with zipfile.ZipFile(bundle, "w") as archive:
                archive.writestr(recovery.WINDOWS_STATE_MEMBER, '{"schema_version":1,"schema_version":2}')
            with self.assertRaises(recovery.RecoveryError) as raised:
                recovery._read_windows_state(bundle)
            self.assertEqual(raised.exception.code, "windows_recovery_bundle_invalid")


class WindowsEnvironmentPinTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        _, self.state = windows_recovery_fixture(None, Path(self.temporary.name))

    def test_equivalent_absolute_pins_preserve_raw_values(self) -> None:
        for label, transform in (
            ("forward", lambda value: value),
            ("backslash", lambda value: value.replace("/", "\\")),
        ):
            with self.subTest(label=label):
                state = copy.deepcopy(self.state)
                pins = state["identity"]["domain_environment"]
                for field in ("DESCRIPTION_ENDPOINT_ROOT", "DESCRIPTION_CAD_OWNERSHIP_FILE"):
                    pins[field] = transform(pins[field])
                original = recovery.canonical_json(pins)
                validated = recovery._validate_windows_recovery_state(state)
                self.assertEqual(recovery.canonical_json(validated["identity"]["domain_environment"]), original)

        state = copy.deepcopy(self.state)
        endpoint = state["identity"]["domain_environment"]["DESCRIPTION_ENDPOINT_ROOT"]
        state["identity"]["domain_environment"]["DESCRIPTION_ENDPOINT_ROOT"] = endpoint + "\\"
        original = recovery.canonical_json(state["identity"]["domain_environment"])
        validated = recovery._validate_windows_recovery_state(state)
        self.assertEqual(recovery.canonical_json(validated["identity"]["domain_environment"]), original)

    def test_file_pin_rejects_trailing_separator(self) -> None:
        for marker in ("/", "\\"):
            with self.subTest(marker=marker):
                state = copy.deepcopy(self.state)
                ownership = state["identity"]["domain_environment"]["DESCRIPTION_CAD_OWNERSHIP_FILE"]
                state["identity"]["domain_environment"]["DESCRIPTION_CAD_OWNERSHIP_FILE"] = ownership + marker
                with self.assertRaises(recovery.RecoveryError):
                    recovery._validate_windows_recovery_state(state)

    def test_invalid_or_different_pins_are_rejected(self) -> None:
        for field in ("DESCRIPTION_ENDPOINT_ROOT", "DESCRIPTION_CAD_OWNERSHIP_FILE"):
            for value in (
                None, "", True, [], "relative/path", "C:relative", "/rooted/path",
                "\\\\server\\share\\path", "//server/share/path", "\\\\?\\C:\\path", "\\\\.\\C:\\path",
                "C:/path/../other", "C:/path/./other",
                "C:/path//other", "C:/path/other ", "C:/path/other.", "C:/path/file:stream",
                "C:/path/\x00other", "C:/description/state/lifecycle/old-generation/generation",
                "C:/description/state/wrong-ownership.json",
            ):
                with self.subTest(field=field, value=value):
                    state = copy.deepcopy(self.state)
                    state["identity"]["domain_environment"][field] = value
                    with self.assertRaises(recovery.RecoveryError) as caught:
                        recovery._validate_windows_recovery_state(state)
                    self.assertEqual(caught.exception.code, "windows_recovery_state_invalid")

    def test_missing_pins_and_wrong_scope_are_rejected(self) -> None:
        for field in ("DESCRIPTION_ENDPOINT_ROOT", "DESCRIPTION_CAD_OWNERSHIP_FILE", "scope"):
            with self.subTest(field=field):
                state = copy.deepcopy(self.state)
                del state["identity"]["domain_environment"][field]
                with self.assertRaises(recovery.RecoveryError):
                    recovery._validate_windows_recovery_state(state)
        state = copy.deepcopy(self.state)
        state["identity"]["domain_environment"]["scope"] = "process"
        with self.assertRaises(recovery.RecoveryError):
            recovery._validate_windows_recovery_state(state)

    def test_domain_environment_must_be_an_object(self) -> None:
        for value in (None, [], "", 1):
            with self.subTest(value=value):
                state = copy.deepcopy(self.state)
                state["identity"]["domain_environment"] = value
                with self.assertRaises(recovery.RecoveryError) as caught:
                    recovery._validate_windows_recovery_state(state)
                self.assertEqual(caught.exception.code, "windows_recovery_state_invalid")

    def test_device_basenames_are_not_filesystem_pins(self) -> None:
        for name in (
            "CON",
            "NUL",
            "PRN",
            "AUX",
            "COM1",
            "LPT1",
            "COM9",
            "LPT9",
            "COM¹",
            "COM²",
            "COM³",
            "LPT¹",
            "LPT²",
            "LPT³",
            "CONIN$",
            "CONOUT$",
        ):
            for suffix in ("", ".txt", " .txt"):
                with self.subTest(name=name, suffix=suffix):
                    self.assertIsNone(
                        recovery._canonical_windows_pin(
                            "C:/data/" + name + suffix,
                            directory=False,
                        )
                    )

    def test_unicode_case_expansion_is_not_a_windows_path_identity(self) -> None:
        self.assertNotEqual(
            recovery._canonical_windows_pin("C:/data/STRASSE", directory=True),
            recovery._canonical_windows_pin("C:/data/strasse", directory=True),
        )
        self.assertNotEqual(
            recovery._canonical_windows_pin("C:/data/Straße", directory=True),
            recovery._canonical_windows_pin("C:/data/STRASSE", directory=True),
        )
        self.assertNotEqual(
            recovery._canonical_windows_pin("C:/data/İ", directory=True),
            recovery._canonical_windows_pin("C:/data/i\u0307", directory=True),
        )


class WindowsAclPolicyTests(unittest.TestCase):
    def test_service_principal_must_be_distinct_and_present(self) -> None:
        fixture = json.loads((REPOSITORY / "tests" / "fixtures" / "windows_acl_policy.json").read_text())
        leaf = copy.deepcopy(fixture["cases"][0]["leaf"])
        leaf["rules"] = [rule for rule in leaf["rules"] if rule["sid"] != fixture["service_sid"]]
        for service_sid in (fixture["service_sid"], "S-1-5-18", "S-1-5-32-544"):
            with self.subTest(service_sid=service_sid), self.assertRaises(recovery.RecoveryError):
                recovery._validate_windows_acl_policy(
                    leaf, label="two principals", operator_sid=service_sid, directory=False
                )

    def test_shared_windows_acl_policy_projections(self) -> None:
        fixture = json.loads((REPOSITORY / "tests" / "fixtures" / "windows_acl_policy.json").read_text())
        for case in fixture["cases"]:
            with self.subTest(case=case["name"]):
                parent = copy.deepcopy(case["parent"])
                leaf = copy.deepcopy(case["leaf"])
                for projection in (parent, leaf):
                    projection["projection_sha256"] = recovery._windows_acl_projection_sha256(projection)
                    recovery._validate_windows_acl_projection(projection, case["name"])

                def validate(case=case, parent=parent, leaf=leaf):
                    if case["mutable"]:
                        recovery._validate_windows_runtime_leaf_acl_policy(
                            leaf, parent, label=case["name"], operator_sid=fixture["service_sid"]
                        )
                    else:
                        recovery._validate_windows_acl_policy(
                            leaf,
                            label=case["name"],
                            operator_sid=fixture["service_sid"],
                            directory=case["directory"],
                        )

                if case["expected"]:
                    validate()
                else:
                    with self.assertRaises(recovery.RecoveryError) as raised:
                        validate()
                    self.assertEqual(raised.exception.code, "windows_managed_acl_policy_invalid")


class RecoveryControlFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        warnings.simplefilter("ignore", ResourceWarning)
        self.temporary = tempfile.TemporaryDirectory(
            prefix="recovery-unit-",
            dir=HERE,
        )
        self.root = Path(self.temporary.name)
        self.fake_bin = install_fake_pg(
            self.root / "fake-bin",
            Fernet(Fernet.generate_key()).encrypt(b"unused").decode(),
        )
        self.context = make_context(self.root, self.fake_bin)
        self.key = write(self.root / "external.key", os.urandom(32).hex() + "\n")
        self.key.chmod(0o600)
        self.real_quiesce_maintenance = recovery.quiesce_maintenance
        self.real_capture_windows_recovery = recovery.capture_windows_recovery
        maintenance_quiesce = mock.patch.object(
            recovery,
            "quiesce_maintenance",
            side_effect=fake_maintenance_state,
        )
        maintenance_finalize = mock.patch.object(
            recovery,
            "finalize_maintenance_receipt",
            side_effect=fake_maintenance_receipt,
        )
        maintenance_quiesce.start()
        maintenance_finalize.start()
        windows_capture = mock.patch.object(
            recovery,
            "capture_windows_recovery",
            side_effect=fake_capture_windows_recovery,
        )
        self.real_restore_windows_recovery = recovery.restore_windows_recovery
        windows_restore = mock.patch.object(
            recovery,
            "restore_windows_recovery",
            side_effect=fake_restore_windows_recovery,
        )
        windows_capture.start()
        windows_restore.start()
        self.addCleanup(windows_restore.stop)
        self.addCleanup(windows_capture.stop)
        self.addCleanup(maintenance_finalize.stop)
        self.addCleanup(maintenance_quiesce.stop)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def backup(self) -> tuple[Path, dict]:
        out = self.root / "recovery"
        with mock.patch.object(
            recovery,
            "load_backup_context",
            return_value=self.context,
        ):
            result = recovery.backup_command(
                config_path=self.context.config_path,
                backup_id="fixture-backup",
                key_file=self.key,
                out=out,
            )
        manifest = json.loads(Path(result["manifest"]).read_text(encoding="utf-8"))
        self.windows_target = recovery._windows_target_from_state(manifest["windows"]["state"])
        return out, manifest

    def test_snapshot_reads_non_git_archive_not_runtime_root(self) -> None:
        runtime = Path(self.context.resolved["storage"]["release_root"])
        archive = self.root / "release-archive"
        archive.mkdir()
        names = ["release.json", *(row["name"] for row in self.context.release_document["artifacts"])]
        names.extend(self.context.archive_document["deployment_files"])
        for name in names:
            (archive / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(runtime / name), archive / name)
        self.context.archive_root = archive
        self.assertFalse((archive / ".git").exists())
        self.assertFalse((runtime / "release.json").exists())
        _, manifest = self.backup()
        self.assertEqual(manifest["source_code"]["worktree"], "release-archive")
        self.assertEqual(manifest["reconstructibles"]["release_root"]["path"], str(runtime))
        self.assertFalse((runtime / "release.json").exists())

    def test_bounded_stdout_kills_writer_at_limit(self) -> None:
        script = write(
            self.root / "flood.py",
            "#!/usr/bin/env python3\nimport sys\nsys.stdout.write('x' * (2 * 1024 * 1024))\n",
            mode=0o755,
        )
        context = recovery.CommandContext(
            bin_dir=self.root,
            library_dir=None,
            commands=[],
        )
        with self.assertRaises(recovery.RecoveryError) as raised:
            context.run_stdout_bounded(
                [script],
                max_stdout_bytes=1024,
                timeout=10,
            )
        self.assertEqual(
            raised.exception.code,
            "recovery_sequence_extraction_too_large",
        )
        self.assertEqual(context.commands, [])

    def test_membership_order_accepts_a_self_grant(self) -> None:
        roles = [
            {
                "name": "solidworks",
                "superuser": True,
                "createrole": True,
            },
            {
                "name": "platform_service",
                "superuser": False,
                "createrole": False,
            },
            {
                "name": "operator_ro",
                "superuser": False,
                "createrole": False,
            },
        ]
        memberships = [
            {
                "role": "operator_ro",
                "member": "platform_service",
                "grantor": "operator_ro",
                "admin_option": False,
            }
        ]
        self.assertEqual(
            recovery.topological_memberships(roles, memberships),
            memberships,
        )

    def test_membership_order_accepts_createrole_grantor(self) -> None:
        roles = [
            {
                "name": "solidworks",
                "superuser": True,
                "createrole": True,
            },
            {
                "name": "platform_service",
                "superuser": False,
                "createrole": True,
            },
            {
                "name": "airflow_runtime",
                "superuser": False,
                "createrole": False,
            },
        ]
        memberships = [
            {
                "role": "pg_read_all_data",
                "member": "airflow_runtime",
                "grantor": "platform_service",
                "admin_option": False,
            }
        ]
        self.assertEqual(
            recovery.topological_memberships(roles, memberships),
            memberships,
        )

    def test_membership_order_refuses_createrole_grant_of_superuser(self) -> None:
        roles = [
            {
                "name": "solidworks",
                "superuser": True,
                "createrole": True,
            },
            {
                "name": "platform_service",
                "superuser": False,
                "createrole": True,
            },
            {
                "name": "airflow_runtime",
                "superuser": True,
                "createrole": False,
            },
        ]
        memberships = [
            {
                "role": "airflow_runtime",
                "member": "platform_service",
                "grantor": "platform_service",
                "admin_option": False,
            }
        ]
        with self.assertRaises(recovery.RecoveryError) as raised:
            recovery.topological_memberships(roles, memberships)
        self.assertEqual(raised.exception.code, "recovery_manifest_invalid")

    def test_target_pga_extra_is_not_treated_as_a_system_role(self) -> None:
        socket_dir = self.root / "target-socket"
        data_dir = self.root / "target-data"
        bin_dir = self.root / "target-bin"
        socket_dir.mkdir()
        data_dir.mkdir()
        bin_dir.mkdir()
        write(data_dir / "PG_VERSION", "14\n")

        def query(*args, **kwargs):
            self.assertIn("left(rolname, 3) <> 'pg_'", kwargs["query"])
            return {
                "server_version_num": "140024",
                "data_directory": str(data_dir.resolve()),
                "system_identifier": "987654321",
                "databases": [],
                "roles": ["solidworks", "pga_extra"],
            }

        manifest = {
            "database": {
                "authority": {
                    "system_identifier": "123456789",
                }
            }
        }
        postgres = {
            "socket_dir": str(socket_dir),
            "data_dir": str(data_dir),
            "bin_dir": str(bin_dir),
            "port": 55433,
            "bootstrap_role": "solidworks",
        }
        with (
            mock.patch.object(recovery, "postgres_major", return_value="14"),
            mock.patch.object(
                recovery,
                "pg_control",
                return_value={"system_identifier": "987654321"},
            ),
            mock.patch.object(recovery, "psql_json", side_effect=query),
            self.assertRaises(recovery.RecoveryError) as raised,
        ):
            recovery.target_empty_cluster(
                recovery.CommandContext(
                    bin_dir=bin_dir,
                    library_dir=None,
                    commands=[],
                ),
                postgres,
                manifest,
            )
        self.assertEqual(raised.exception.code, "recovery_target_not_empty")
        self.assertEqual(
            raised.exception.detail,
            {"roles": ["solidworks", "pga_extra"]},
        )

    def target_plan(self, manifest_sha: str, roots: dict[str, str]) -> Path:
        postgres_root = self.root / "target-db"
        socket_dir = postgres_root / "socket"
        data_dir = postgres_root / "data"
        library_dir = self.root / "not-used"
        socket_dir.mkdir(parents=True)
        data_dir.mkdir(parents=True)
        library_dir.mkdir(exist_ok=True)
        write(data_dir / "PG_VERSION", "14\n")
        plan = self.root / "target-plan.json"
        write(
            plan,
            json.dumps(
                {
                    "schema_version": recovery.TARGET_SCHEMA,
                    "backup_manifest_sha256": manifest_sha,
                    "closure": json.loads((self.root / "recovery/recovery-manifest.json").read_text())["closure"],
                    "mode": "exact_path",
                    "host_id": "target-host-fixture",
                    "postgres": {
                        "bin_dir": str(
                            self.context.postgres_root / "root" / "usr" / "lib" / "postgresql" / "14" / "bin"
                        ),
                        "library_dir": str(library_dir),
                        "socket_dir": str(socket_dir),
                        "data_dir": str(data_dir),
                        "port": 55433,
                        "bootstrap_role": "solidworks",
                    },
                    "roots": roots,
                    "windows": self.windows_target,
                },
                indent=2,
            ),
        )
        return plan

    def isolate_source(self) -> None:
        source = self.root / "source"
        saved = self.root / "saved-source"
        if source.exists():
            os.replace(source, saved)

    def workspace_fixture(self) -> tuple[Path, Path, str, str]:
        origin = self.root / "workspace-origin.git"
        repository = self.root / "workspace-source"
        subprocess.run(
            ["git", "init", "--bare", "--initial-branch=main", str(origin)],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "init", "--initial-branch=main", str(repository)],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "-C", str(repository), "config", "user.email", "fixture@example.invalid"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(repository), "config", "user.name", "Recovery Fixture"],
            check=True,
        )
        write(repository / "tracked.txt", "restored\n", mode=0o644)
        subprocess.run(
            ["git", "-C", str(repository), "add", "tracked.txt"],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "-C", str(repository), "commit", "-m", "fixture"],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "-C", str(repository), "remote", "add", "origin", str(origin)],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(repository), "push", "-u", "origin", "main"],
            check=True,
            capture_output=True,
            text=True,
        )
        head = subprocess.check_output(
            ["git", "-C", str(repository), "rev-parse", "HEAD"],
            text=True,
        ).strip()
        tree = subprocess.check_output(
            ["git", "-C", str(repository), "rev-parse", "HEAD^{tree}"],
            text=True,
        ).strip()
        return repository, origin, head, tree

    def workspace_bundle(self) -> tuple[Path, Path, dict]:
        repository, origin, head, tree = self.workspace_fixture()
        context = copy.deepcopy(self.context)
        context.workspace_plan = {
            "schema_version": "solidworks-to-urdf.workspace-rehydrate/v2",
            "repositories": [
                {
                    "repository_slug": "owner/model",
                    "path": str(repository),
                    "origin": str(origin),
                    "head": head,
                    "tree": tree,
                    "head_symref": "refs/heads/main",
                    "refs": {
                        "refs/heads/main": head,
                        "refs/remotes/origin/main": head,
                    },
                    "symrefs": {},
                    "bases": ["main"],
                    "status": "clean",
                }
            ],
        }
        staging = self.root / "workspace-capture"
        (staging / "reconstruct").mkdir(parents=True)
        recovery.capture_workspace_bundles(context, staging)
        plan = json.loads((staging / "reconstruct" / "workspace-plan.json").read_text())
        target = self.root / "workspace-targets" / "workspace-source"
        plan["repositories"][0]["path"] = str(target)
        (staging / "reconstruct" / "workspace-plan.json").write_text(
            recovery.canonical_json(plan).decode(),
            encoding="utf-8",
        )
        extracted = self.root / "workspace-extracted"
        shutil.copytree(staging / "reconstruct", extracted / "reconstruct")
        manifest = {
            "reconstructibles": {
                "workspace_root": {
                    "path": str(self.root / "workspace-targets"),
                    "plan_member": "reconstruct/workspace-plan.json",
                    "recipe": "git-origin-head-clean",
                }
            }
        }
        return extracted, origin, manifest

    def test_backup_and_restore_control_flow(self) -> None:
        out, manifest = self.backup()
        self.assertEqual(manifest["schema_version"], recovery.MANIFEST_SCHEMA)
        self.assertEqual(manifest["closure"], recovery.closure_binding(manifest["selection"]))
        self.assertFalse(any(row["member"].startswith("state/runs/sealed/") for row in manifest["members"]))
        self.assertFalse(any(row["member"].endswith("promotions/README") for row in manifest["members"]))
        self.assertEqual(manifest["database"]["bootstrap_role"], "solidworks")
        self.assertEqual(
            manifest["archive"]["sha256"],
            hashlib.sha256((out / "recovery.tar.enc").read_bytes()).hexdigest(),
        )
        self.assertTrue((out / "recovery-manifest.sha256").is_file())
        self.assertFalse((out / "backup").exists())
        self.assertFalse(
            any(self.key.read_bytes().strip() in path.read_bytes() for path in out.iterdir() if path.is_file())
        )

        self.isolate_source()
        manifest_path = out / "recovery-manifest.json"
        manifest_bytes = manifest_path.read_bytes()
        manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()
        roots = {row["name"]: row["path"] for row in manifest["roots"] if "reconstructible_runtime" not in row}
        plan = self.target_plan(manifest_sha, roots)
        status_dir = self.root / "status"
        result = recovery.restore_command(
            manifest_path=manifest_path,
            key=self.key,
            target_plan_path=plan,
            status_dir=status_dir,
        )
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["closure"], manifest["closure"])
        self.assertTrue((status_dir / "recovery-marker.json").is_file())
        self.assertEqual(
            (Path(roots["handoff_root"]) / "request.json").read_text(),
            '{"handoff":true}\n',
        )
        self.assertEqual(
            permission(Path(manifest["secrets"][0]["path"])),
            0o600,
        )
        self.assertTrue(
            any(candidate.name == "pg-restore-complete" for candidate in self.root.rglob("pg-restore-complete"))
        )
        verify_status = self.root / "verify-status"
        verified = recovery.restore_command(
            manifest_path=manifest_path,
            key=self.key,
            target_plan_path=plan,
            status_dir=verify_status,
            verify_only=True,
        )
        self.assertEqual(verified["status"], "succeeded")
        self.assertEqual(verified["operation"], "verify-only")
        self.assertFalse((verify_status / "recovery-marker.json").exists())
        state_root = Path(roots["state_root"])
        original_state_mode = permission(state_root)
        state_root.chmod(0o777)
        try:
            with self.assertRaises(recovery.RecoveryError) as mode_refused:
                recovery.restore_command(
                    manifest_path=manifest_path,
                    key=self.key,
                    target_plan_path=plan,
                    status_dir=self.root / "verify-mode-tampered",
                    verify_only=True,
                )
        finally:
            state_root.chmod(original_state_mode)
        self.assertEqual(
            mode_refused.exception.code,
            "recovery_member_hash_mismatch",
        )
        restored_file = Path(roots["handoff_root"]) / "request.json"
        restored_file.write_text("tampered\n", encoding="utf-8")
        with self.assertRaises(recovery.RecoveryError):
            recovery.restore_command(
                manifest_path=manifest_path,
                key=self.key,
                target_plan_path=plan,
                status_dir=self.root / "verify-tampered",
                verify_only=True,
            )
        self.assertEqual(restored_file.read_text(), "tampered\n")

    def test_snapshot_reference_digest_mismatch_stops_before_copy(self):
        interpreter = Path(self.context.derived_env["AIRFLOW_VENV"]) / "bin/python"
        interpreter.write_text(
            FAKE_SNAPSHOT_WORKER.replace(
                '"checkpoint_refs_sha256": recovery._digest_document(references)', '"checkpoint_refs_sha256": "0" * 64'
            )
        )
        with self.assertRaises(recovery.RecoveryError) as raised:
            self.backup()
        self.assertEqual(raised.exception.code, "recovery_snapshot_worker_failed")
        self.assertFalse((self.root / "recovery/backup/state/runs").exists())

    def test_target_generator_derives_v3_from_v6_without_external_closure_override(self):
        out, manifest = self.backup()
        manifest_path = out / "recovery-manifest.json"
        roots = {row["name"]: row["path"] for row in manifest["roots"] if "reconstructible_runtime" not in row}
        fixture_plan = json.loads(self.target_plan(recovery.sha256_file(manifest_path), roots).read_text())
        generated = recovery.build_target_plan(
            manifest,
            recovery.sha256_file(manifest_path),
            host_id="target-generation-fixture",
            postgres=fixture_plan["postgres"],
        )
        self.assertEqual(generated["schema_version"], recovery.TARGET_SCHEMA)
        self.assertEqual(generated["closure"], manifest["closure"])
        self.assertEqual(generated["roots"], roots)
        self.assertEqual(generated["windows"], recovery._windows_target_from_state(manifest["windows"]["state"]))
        postgres = write(self.root / "target-postgres.json", json.dumps(fixture_plan["postgres"]))
        target = self.root / "generated-target.json"
        arguments = [
            "target-plan",
            "--manifest",
            str(manifest_path),
            "--host-id",
            "generated-fixture",
            "--postgres-config",
            str(postgres),
            "--out",
            str(target),
        ]
        self.assertEqual(recovery.main(arguments), 0)
        self.assertEqual(json.loads(target.read_text())["closure"], manifest["closure"])
        original = target.read_bytes()
        self.assertEqual(recovery.main(arguments), 1)
        self.assertEqual(target.read_bytes(), original)

    def test_manifest_rejects_extra_source_member_and_moved_state_scope(self):
        _out, manifest = self.backup()
        changed = copy.deepcopy(manifest)
        changed["members"].append(
            {
                "member": "config/state/injected",
                "type": "file",
                "bytes": 0,
                "sha256": hashlib.sha256(b"").hexdigest(),
                "mode": 0o600,
                "required": True,
                "class": "config",
            }
        )
        with self.assertRaises(recovery.RecoveryError):
            recovery.validate_manifest(changed, expected_manifest_sha=None)
        changed = copy.deepcopy(manifest)
        scope = next(row for row in changed["selection"]["root_scopes"] if row["name"] == "install_records")
        scope["path"] = str(self.root / "unrelated")
        changed["closure"] = recovery.closure_binding(changed["selection"])
        changed["consistency"]["closure"] = changed["closure"]
        with self.assertRaises(recovery.RecoveryError):
            recovery.validate_manifest(changed, expected_manifest_sha=None)

    def test_real_promotion_receipt_preserves_release_and_frozen_evidence(self):
        import promotion

        state = Path(self.context.resolved["storage"]["state_root"])
        receipt_path = state / "promotions/install-a/receipt.json"
        evidence = write(receipt_path.parent / "health.json", '{"passed":true}')
        frozen = promotion.freeze_evidence([evidence], receipt_path.parent / "evidence-immutable", "promote", "verify")
        source = state / "install-records/install-a.json"
        target = state / "snapshots/install-a.json"
        release = self.context.archive_root / "release.json"
        document = {
            "schema_version": "solidworks-to-urdf.promotion/v1",
            "install_id": "install-a",
            "plan": ["verify"],
            "from": str(source),
            "from_sha256": recovery.sha256_file(source),
            "to": str(target),
            "to_sha256": recovery.sha256_file(target),
            "release_manifest": {"path": str(release), "sha256": recovery.sha256_file(release)},
            "steps": [
                {
                    "seq": 1,
                    "verb": "promote",
                    "step": "verify",
                    "status": "completed",
                    "at": "2026-10-22T00:00:00Z",
                    "evidence": [str(path) for path in frozen],
                    "evidence_digests": [{"path": str(path), "sha256": recovery.sha256_file(path)} for path in frozen],
                }
            ],
        }
        write(receipt_path, json.dumps(document))
        current = json.loads((state / "install-record.json").read_text())
        current["journal"] = [journal_row_fixture([str(receipt_path)])]
        write(state / "install-record.json", json.dumps(current))
        out, manifest = self.backup()
        sources = {row["source_path"] for row in manifest["selection"]["journal_entries"]}
        self.assertTrue({str(release), str(evidence), str(receipt_path)} <= sources)
        status = self.root / "journal-archive-check"
        status.mkdir()
        extracted, temporary = recovery.prepare_extraction(manifest, out / "recovery-manifest.json", self.key, status)
        self.addCleanup(temporary.cleanup)
        recovery.verify_selection_archive(manifest, extracted)

    def test_failed_checkpoint_roundtrip_and_unreferenced_store_exclusion(self):
        from description_pipeline.delivery import SUBJECT_DIRECTORIES, write_delivery_manifest
        from description_pipeline.interface_records import CHECK_POLICY_SCHEMA, check_policy_digest
        from description_pipeline.stages import CONTRACT, CONTRACT_SHA256

        tree = Path(self.context.resolved["derived"]["store_root"]) / "sealed/failed-run/discover"
        for name in SUBJECT_DIRECTORIES:
            (tree / name).mkdir(parents=True, exist_ok=True)
        write(tree / "tool.json", "{}")
        delivery = write_delivery_manifest(
            tree,
            contract={"schema_version": CONTRACT["schema_version"], "digest": CONTRACT_SHA256},
            policy={"schema_version": CHECK_POLICY_SCHEMA, "digest": check_policy_digest()},
        )
        reference = {
            "run_id": "failed-run",
            "stage": "discover",
            "stage_state": "failed",
            "request_id": "request-1",
            "run_parent_run_id": None,
            "reused_from_run": None,
            "original_executed_at": None,
            "checkpoint_uri": "store://sealed/failed-run/discover",
            "receipt": {
                "schema_version": "solidworks-to-urdf.bundle/v1",
                "run_id": "failed-run",
                "stage": "discover",
                "state": "failed",
                "passed": False,
                "artifacts": delivery["subject"]["files"],
            },
        }
        write(self.root / "checkpoint-fixture.json", json.dumps([reference]))
        out, manifest = self.backup()
        self.assertEqual(manifest["selection"]["checkpoint_refs"], [reference])
        self.isolate_source()
        manifest_path = out / "recovery-manifest.json"
        roots = {row["name"]: row["path"] for row in manifest["roots"] if "reconstructible_runtime" not in row}
        plan = self.target_plan(recovery.sha256_file(manifest_path), roots)
        result = recovery.restore_command(
            manifest_path=manifest_path, key=self.key, target_plan_path=plan, status_dir=self.root / "failed-roundtrip"
        )
        self.assertEqual(result["status"], "succeeded")
        self.assertTrue((tree / "tool.json").is_file())
        self.assertFalse((Path(roots["store_root"]) / "sealed/run-1").exists())

    def test_workspace_bundle_roundtrip_is_offline_and_verified(self) -> None:
        extracted, origin, manifest = self.workspace_bundle()
        origin.rename(self.root / "workspace-origin-unavailable.git")
        recovery.restore_workspaces(manifest, extracted, verify_only=False)
        target = self.root / "workspace-targets" / "workspace-source"
        self.assertEqual((target / "tracked.txt").read_text(), "restored\n")
        self.assertEqual(
            subprocess.check_output(
                ["git", "-C", str(target), "remote", "get-url", "origin"],
                text=True,
            ).strip(),
            str(origin),
        )
        before = subprocess.check_output(
            ["git", "-C", str(target), "status", "--porcelain=v2"],
            text=True,
        )
        recovery.restore_workspaces(manifest, extracted, verify_only=True)
        after = subprocess.check_output(
            ["git", "-C", str(target), "status", "--porcelain=v2"],
            text=True,
        )
        self.assertEqual(before, after)

    def test_workspace_restore_refuses_a_preexisting_target(self) -> None:
        extracted, _, manifest = self.workspace_bundle()
        target = self.root / "workspace-targets" / "workspace-source"
        target.mkdir(parents=True)
        write(target / "occupied", "do not replace\n")
        with self.assertRaises(recovery.RecoveryError) as raised:
            recovery.restore_workspaces(manifest, extracted, verify_only=False)
        self.assertEqual(raised.exception.code, "recovery_target_not_empty")

    def test_workspace_bundle_digest_tamper_is_refused(self) -> None:
        extracted, _, manifest = self.workspace_bundle()
        plan_path = extracted / "reconstruct" / "workspace-plan.json"
        plan = json.loads(plan_path.read_text())
        plan["repositories"][0]["bundle_sha256"] = "0" * 64
        plan_path.write_text(recovery.canonical_json(plan).decode(), encoding="utf-8")
        with self.assertRaises(recovery.RecoveryError) as raised:
            recovery.restore_workspaces(manifest, extracted, verify_only=False)
        self.assertEqual(raised.exception.code, "recovery_workspace_mismatch")

    def test_workspace_verify_detects_assume_unchanged_content(self) -> None:
        repository, origin, head, tree = self.workspace_fixture()
        subprocess.run(
            [
                "git",
                "-C",
                str(repository),
                "update-index",
                "--assume-unchanged",
                "tracked.txt",
            ],
            check=True,
        )
        write(repository / "tracked.txt", "hidden-change\n", mode=0o644)
        row = {
            "repository_slug": "owner/model",
            "path": str(repository),
            "origin": str(origin),
            "head": head,
            "tree": tree,
            "head_symref": "refs/heads/main",
            "refs": {
                "refs/heads/main": head,
                "refs/remotes/origin/main": head,
            },
            "symrefs": {},
            "bases": ["main"],
            "status": "clean",
            "bundle_member": "reconstruct/workspaces/fixture.bundle",
            "bundle_sha256": "0" * 64,
        }
        with self.assertRaises(recovery.RecoveryError) as raised:
            recovery._workspace_verify(repository, row)
        self.assertEqual(raised.exception.code, "recovery_workspace_mismatch")

    def attribute_row(self, repository: Path) -> dict:
        head_symref, refs, symrefs = recovery._workspace_refs(repository)
        return {
            "repository_slug": "owner/model",
            "path": str(repository),
            "origin": recovery._git_output(repository, "remote", "get-url", "origin"),
            "head": recovery._git_output(repository, "rev-parse", "HEAD"),
            "tree": recovery._git_output(repository, "rev-parse", "HEAD^{tree}"),
            "head_symref": head_symref,
            "refs": refs,
            "symrefs": symrefs,
            "bases": ["main"],
            "status": "clean",
            "bundle_member": "reconstruct/workspaces/fixture.bundle",
            "bundle_sha256": "0" * 64,
        }

    def commit_attributes(self, repository: Path, attributes: str) -> dict:
        write(repository / ".gitattributes", attributes, mode=0o644)
        subprocess.run(["git", "-C", str(repository), "add", ".gitattributes"], check=True)
        subprocess.run(["git", "-C", str(repository), "commit", "-qm", "Set test attributes"], check=True)
        return self.attribute_row(repository)

    def test_workspace_attributes_use_git_234_compatible_cached_query(self) -> None:
        repository, _, _, _ = self.workspace_fixture()
        row = self.attribute_row(repository)
        original = subprocess.check_output
        observed = []

        def git_234(arguments, *args, **kwargs):
            if "check-attr" in arguments:
                observed.append(arguments)
                self.assertIn("--cached", arguments)
                for required in ("-z", "filter", "working-tree-encoding", "--stdin"):
                    self.assertIn(required, arguments)
                if any(str(value).startswith("--source") for value in arguments):
                    raise subprocess.CalledProcessError(129, arguments, stderr=b"unknown option source")
            return original(arguments, *args, **kwargs)

        with mock.patch.object(recovery.subprocess, "check_output", side_effect=git_234):
            recovery._workspace_verify(repository, row)
        self.assertEqual(len(observed), 1)

    def test_workspace_nonexecutable_accepts_private_permissions(self) -> None:
        repository, _, _, _ = self.workspace_fixture()
        row = self.attribute_row(repository)
        for mode in (0o600, 0o640):
            with self.subTest(mode=oct(mode)):
                (repository / "tracked.txt").chmod(mode)
                recovery._workspace_verify(repository, row)

    def test_workspace_executable_accepts_private_permissions(self) -> None:
        repository, _, _, _ = self.workspace_fixture()
        (repository / "tracked.txt").chmod(0o700)
        subprocess.run(["git", "-C", str(repository), "add", "tracked.txt"], check=True)
        subprocess.run(["git", "-C", str(repository), "commit", "-qm", "Track executable mode"], check=True)
        row = self.attribute_row(repository)
        for mode in (0o700, 0o750, 0o755):
            with self.subTest(mode=oct(mode)):
                (repository / "tracked.txt").chmod(mode)
                recovery._workspace_verify(repository, row)

    def test_workspace_nonexecutable_rejects_group_or_other_write(self) -> None:
        repository, _, _, _ = self.workspace_fixture()
        row = self.attribute_row(repository)
        for mode in (0o664, 0o666, 0o620, 0o602):
            with self.subTest(mode=oct(mode)):
                (repository / "tracked.txt").chmod(mode)
                with self.assertRaisesRegex(recovery.RecoveryError, "writable by group or others"):
                    recovery._workspace_verify(repository, row)

    def test_workspace_executable_rejects_group_write(self) -> None:
        repository, _, _, _ = self.workspace_fixture()
        (repository / "tracked.txt").chmod(0o700)
        subprocess.run(["git", "-C", str(repository), "add", "tracked.txt"], check=True)
        subprocess.run(["git", "-C", str(repository), "commit", "-qm", "Track executable mode"], check=True)
        row = self.attribute_row(repository)
        (repository / "tracked.txt").chmod(0o775)
        with self.assertRaisesRegex(recovery.RecoveryError, "writable by group or others"):
            recovery._workspace_verify(repository, row)

    def test_workspace_nonexecutable_rejects_untracked_execute_bit(self) -> None:
        repository, _, _, _ = self.workspace_fixture()
        row = self.attribute_row(repository)
        subprocess.run(["git", "-C", str(repository), "config", "core.filemode", "false"], check=True)
        (repository / "tracked.txt").chmod(0o700)
        with self.assertRaisesRegex(recovery.RecoveryError, "workspace tracked mode differs"):
            recovery._workspace_verify(repository, row)

    def test_workspace_executable_rejects_missing_execute_bit(self) -> None:
        repository, _, _, _ = self.workspace_fixture()
        (repository / "tracked.txt").chmod(0o700)
        subprocess.run(["git", "-C", str(repository), "add", "tracked.txt"], check=True)
        subprocess.run(["git", "-C", str(repository), "commit", "-qm", "Track executable mode"], check=True)
        row = self.attribute_row(repository)
        subprocess.run(["git", "-C", str(repository), "config", "core.filemode", "false"], check=True)
        (repository / "tracked.txt").chmod(0o600)
        with self.assertRaisesRegex(recovery.RecoveryError, "workspace tracked mode differs"):
            recovery._workspace_verify(repository, row)

    def test_workspace_tracked_unset_and_unspecified_attributes_pass(self) -> None:
        repository, _, _, _ = self.workspace_fixture()
        for attributes in ("# Attributes are unspecified\n", "tracked.txt -filter -working-tree-encoding\n"):
            with self.subTest(attributes=attributes):
                row = self.commit_attributes(repository, attributes)
                recovery._workspace_verify(repository, row)

    def test_workspace_tracked_filter_and_encoding_are_rejected(self) -> None:
        repository, _, _, _ = self.workspace_fixture()
        for attributes in ("tracked.txt filter=custom\n", "tracked.txt working-tree-encoding=UTF-16\n"):
            with self.subTest(attributes=attributes):
                row = self.commit_attributes(repository, attributes)
                with self.assertRaisesRegex(recovery.RecoveryError, "attribute is unsupported"):
                    recovery._workspace_verify(repository, row)

    def test_workspace_local_attributes_remain_rejected(self) -> None:
        repository, _, _, _ = self.workspace_fixture()
        row = self.attribute_row(repository)
        write(repository / ".git/info/attributes", "tracked.txt -filter\n")
        with self.assertRaisesRegex(recovery.RecoveryError, "local attribute overrides"):
            recovery._workspace_verify(repository, row)

    def test_workspace_index_drift_is_rejected_before_attributes(self) -> None:
        repository, _, _, _ = self.workspace_fixture()
        row = self.attribute_row(repository)
        write(repository / "tracked.txt", "staged drift\n", mode=0o644)
        subprocess.run(["git", "-C", str(repository), "add", "tracked.txt"], check=True)
        original = subprocess.check_output

        def no_attribute_query(arguments, *args, **kwargs):
            self.assertNotIn("check-attr", arguments)
            return original(arguments, *args, **kwargs)

        with (
            mock.patch.object(recovery.subprocess, "check_output", side_effect=no_attribute_query),
            self.assertRaises(recovery.RecoveryError),
        ):
            recovery._workspace_verify(repository, row)

    def test_workspace_materialize_uses_cached_bound_tree_without_checkout(self) -> None:
        repository, _, _, _ = self.workspace_fixture()
        row = self.commit_attributes(repository, "tracked.txt -filter -working-tree-encoding\n")
        bundle = self.root / "attributes.bundle"
        subprocess.run(["git", "-C", str(repository), "bundle", "create", str(bundle), "--all"], check=True)
        target = self.root / "materialized-attributes"
        original = subprocess.check_output
        observed = []

        def git_234(arguments, *args, **kwargs):
            if "check-attr" in arguments:
                self.assertFalse(any(str(value).startswith("--source") for value in arguments))
                self.assertIn("--cached", arguments)
                observed.append(arguments)
                if len(observed) == 1:
                    self.assertFalse((target / "tracked.txt").exists())
                    self.assertEqual(original(["git", "-C", str(target), "write-tree"], text=True).strip(), row["tree"])
            return original(arguments, *args, **kwargs)

        with mock.patch.object(recovery.subprocess, "check_output", side_effect=git_234):
            recovery._materialize_workspace(bundle, target, row)
        self.assertTrue(observed)
        self.assertEqual((target / "tracked.txt").read_text(), "restored\n")

    def test_workspace_materialize_rejects_attributes_before_checkout(self) -> None:
        repository, _, _, _ = self.workspace_fixture()
        cases = ("tracked.txt filter=custom\n", "tracked.txt working-tree-encoding=UTF-16\n")
        for number, attributes in enumerate(cases):
            with self.subTest(attributes=attributes):
                row = self.commit_attributes(repository, attributes)
                bundle = self.root / f"unsafe-attributes-{number}.bundle"
                subprocess.run(["git", "-C", str(repository), "bundle", "create", str(bundle), "--all"], check=True)
                target = self.root / f"unsafe-materialized-{number}"
                with self.assertRaisesRegex(recovery.RecoveryError, "attribute is unsupported"):
                    recovery._materialize_workspace(bundle, target, row)
                self.assertFalse((target / "tracked.txt").exists())

    def test_workspace_attribute_index_read_failure_has_stable_error(self) -> None:
        repository, _, head, tree = self.workspace_fixture()
        original = subprocess.run

        def fail_read_tree(arguments, *args, **kwargs):
            if "read-tree" in arguments:
                raise subprocess.CalledProcessError(128, arguments, stderr=b"read failure")
            return original(arguments, *args, **kwargs)

        with (
            mock.patch.object(recovery.subprocess, "run", side_effect=fail_read_tree),
            self.assertRaisesRegex(recovery.RecoveryError, "cannot populate workspace attribute index") as raised,
        ):
            recovery._workspace_attributes_safe(repository, head, tree)
        self.assertEqual(raised.exception.code, "recovery_workspace_missing")

    def test_workspace_attribute_index_mismatch_prevents_checkout(self) -> None:
        repository, _, _, _ = self.workspace_fixture()
        row = self.attribute_row(repository)
        bundle = self.root / "index-mismatch.bundle"
        subprocess.run(["git", "-C", str(repository), "bundle", "create", str(bundle), "--all"], check=True)
        target = self.root / "index-mismatch-target"
        original = subprocess.check_output

        def wrong_index(arguments, *args, **kwargs):
            self.assertNotIn("check-attr", arguments)
            if "write-tree" in arguments:
                return "0" * 40 + "\n"
            return original(arguments, *args, **kwargs)

        with (
            mock.patch.object(recovery.subprocess, "check_output", side_effect=wrong_index),
            self.assertRaisesRegex(recovery.RecoveryError, "index differs from the bound tree"),
        ):
            recovery._materialize_workspace(bundle, target, row)
        self.assertFalse((target / "tracked.txt").exists())

    def test_workspace_verify_detects_a_staged_change(self) -> None:
        repository, origin, head, tree = self.workspace_fixture()
        head_bytes = (repository / "tracked.txt").read_bytes()
        write(repository / "tracked.txt", "staged-change\n", mode=0o644)
        subprocess.run(
            ["git", "-C", str(repository), "add", "tracked.txt"],
            check=True,
        )
        (repository / "tracked.txt").write_bytes(head_bytes)
        row = {
            "repository_slug": "owner/model",
            "path": str(repository),
            "origin": str(origin),
            "head": head,
            "tree": tree,
            "head_symref": "refs/heads/main",
            "refs": {
                "refs/heads/main": head,
                "refs/remotes/origin/main": head,
            },
            "symrefs": {},
            "bases": ["main"],
            "status": "clean",
            "bundle_member": "reconstruct/workspaces/fixture.bundle",
            "bundle_sha256": "0" * 64,
        }
        with self.assertRaises(recovery.RecoveryError) as raised:
            recovery._workspace_verify(repository, row)
        self.assertEqual(raised.exception.code, "recovery_workspace_mismatch")

    def test_workspace_verify_rejects_index_flags_without_content_change(self) -> None:
        repository, origin, head, tree = self.workspace_fixture()
        row = {
            "repository_slug": "owner/model",
            "path": str(repository),
            "origin": str(origin),
            "head": head,
            "tree": tree,
            "head_symref": "refs/heads/main",
            "refs": {
                "refs/heads/main": head,
                "refs/remotes/origin/main": head,
            },
            "symrefs": {},
            "bases": ["main"],
            "status": "clean",
            "bundle_member": "reconstruct/workspaces/fixture.bundle",
            "bundle_sha256": "0" * 64,
        }
        for flag in ("--assume-unchanged", "--skip-worktree"):
            subprocess.run(
                ["git", "-C", str(repository), "update-index", flag, "tracked.txt"],
                check=True,
            )
            with self.assertRaises(recovery.RecoveryError) as raised:
                recovery._workspace_verify(repository, row)
            self.assertEqual(raised.exception.code, "recovery_workspace_mismatch")
            clear = "--no-assume-unchanged" if flag == "--assume-unchanged" else "--no-skip-worktree"
            subprocess.run(
                ["git", "-C", str(repository), "update-index", clear, "tracked.txt"],
                check=True,
            )

    def test_workspace_verify_rejects_unsupported_refs(self) -> None:
        repository, origin, head, tree = self.workspace_fixture()
        write(repository / "tracked.txt", "stash-change\n", mode=0o644)
        subprocess.run(
            ["git", "-C", str(repository), "stash", "push", "-m", "fixture"],
            check=True,
            capture_output=True,
            text=True,
        )
        row = {
            "repository_slug": "owner/model",
            "path": str(repository),
            "origin": str(origin),
            "head": head,
            "tree": tree,
            "head_symref": "refs/heads/main",
            "refs": {
                "refs/heads/main": head,
                "refs/remotes/origin/main": head,
            },
            "symrefs": {},
            "bases": ["main"],
            "status": "clean",
            "bundle_member": "reconstruct/workspaces/fixture.bundle",
            "bundle_sha256": "0" * 64,
        }
        with self.assertRaises(recovery.RecoveryError) as raised:
            recovery._workspace_verify(repository, row)
        self.assertEqual(raised.exception.code, "recovery_workspace_mismatch")

    def test_release_runtime_preflight_rejects_external_venv(self) -> None:
        release_root = self.root / "release-preflight"
        extracted = self.root / "release-preflight-extracted"
        operator_env = extracted / "config" / "state" / "derived" / "install-a" / "operator.env"
        write(
            operator_env,
            f"AIRFLOW_VENV={self.root / 'outside-venv'}\nAIRFLOW_HOME={self.root / 'airflow-home'}\n",
        )
        manifest = {
            "reconstructibles": {
                "release_root": {
                    "path": str(release_root),
                    "recipe": "offline-release-artifacts",
                }
            },
            "maintenance": {"receipt": {"identity": {"install_id": "install-a"}}},
        }
        with self.assertRaises(recovery.RecoveryError) as raised:
            recovery.validate_release_runtime_target(
                manifest,
                extracted,
                verify_only=False,
            )
        self.assertEqual(raised.exception.code, "recovery_target_path_unsafe")

    def test_release_runtime_rehydrates_from_offline_role_archives(self) -> None:
        release_root = self.root / "runtime-release"
        extracted = self.root / "runtime-extracted"
        status_dir = self.root / "runtime-status"
        status_dir.mkdir()
        operator_env = extracted / "config" / "state" / "derived" / "install-a" / "operator.env"
        write(
            operator_env,
            "\n".join(
                (
                    f"AIRFLOW_VENV={release_root / 'airflow-venv'}",
                    f"AIRFLOW_HOME={self.root / 'airflow-home'}",
                    "",
                )
            ),
        )
        readme = b"offline-role\n"
        release_files = extracted / "release"
        (release_files / "artifacts").mkdir(parents=True)
        wheel_path = release_files / "artifacts" / "mimicverse_description-1.0.0-test-py3-none-any.whl"
        write_runtime_wheel(wheel_path)
        wheel_bytes = wheel_path.read_bytes()
        linux_zip = release_files / "artifacts" / "description-1.0.0-test-linux-cp312-x86_64.zip"
        with zipfile.ZipFile(linux_zip, "w") as archive:
            lock_bytes = (
                f"mimicverse-description==1.0.0-test --hash=sha256:{hashlib.sha256(wheel_bytes).hexdigest()}\n"
            ).encode()
            archive.writestr(
                "files.json",
                json.dumps(
                    {
                        "README.md": hashlib.sha256(readme).hexdigest(),
                        f"wheels/{wheel_path.name}": hashlib.sha256(wheel_bytes).hexdigest(),
                        "requirements.lock": hashlib.sha256(lock_bytes).hexdigest(),
                    }
                ),
            )
            archive.writestr("README.md", readme)
            archive.writestr(f"wheels/{wheel_path.name}", wheel_bytes)
            archive.writestr("requirements.lock", lock_bytes)
        deployment_zip = release_files / "artifacts" / "description-1.0.0-test-deployment.zip"
        recovery_script = Path(recovery.__file__).read_bytes()
        installer = runtime_installer(release_root / "airflow-venv")
        linux_lock = b"linux-lock\n"
        windows_lock = b"windows-lock\n"
        with zipfile.ZipFile(deployment_zip, "w") as archive:
            archive.writestr("deploy/operator/recovery.py", recovery_script)
            archive.writestr("deploy/airflow/install.sh", installer)
            archive.writestr("requirements/linux-py312.lock", linux_lock)
            archive.writestr("requirements/windows-py312.lock", windows_lock)
        write(
            release_files / "release.json",
            json.dumps(
                {
                    "schema_version": "solidworks-to-urdf.release/v1",
                    "version": "1.0.0",
                    "source_commit": "a" * 40,
                    "source_tree": "d" * 40,
                    "source_sha256": "b" * 64,
                    "package_files": {
                        name: hashlib.sha256(payload).hexdigest()
                        for name, payload in runtime_wheel_payloads().items()
                        if name.startswith("description_pipeline/") and name != "description_pipeline/tool-release.json"
                    },
                    "deployment_files": {
                        "deploy/operator/recovery.py": hashlib.sha256(recovery_script).hexdigest(),
                        "deploy/airflow/install.sh": hashlib.sha256(installer.encode()).hexdigest(),
                        "requirements/linux-py312.lock": hashlib.sha256(linux_lock).hexdigest(),
                        "requirements/windows-py312.lock": hashlib.sha256(windows_lock).hexdigest(),
                    },
                    "runtime_locks": {
                        "linux": hashlib.sha256(linux_lock).hexdigest(),
                        "windows": hashlib.sha256(windows_lock).hexdigest(),
                    },
                }
            ),
        )
        manifest = {
            "release": {
                "version": "1.0.0",
                "source_sha256": "b" * 64,
                "artifacts": [
                    {
                        "name": name,
                        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    }
                    for name, path in (
                        (deployment_zip.name, deployment_zip),
                        (linux_zip.name, linux_zip),
                        (wheel_path.name, wheel_path),
                    )
                ],
            },
            "source_code": {
                "commit": "a" * 40,
                "tree": "d" * 40,
                "worktree": "release-archive",
                "script_sha256": hashlib.sha256(recovery_script).hexdigest(),
            },
            "maintenance": {"receipt": {"identity": {"install_id": "install-a"}}},
            "members": [],
        }
        result = recovery.rehydrate_release_runtime(
            manifest=manifest,
            plan={"roots": {"release_root": str(release_root)}},
            extracted=extracted,
            status_dir=status_dir,
            verify_only=False,
        )
        self.assertEqual(result["identity"]["version"], "1.0.0")
        self.assertTrue((release_root / "airflow-venv" / "bin" / "python").is_file())
        verified = recovery.rehydrate_release_runtime(
            manifest=manifest,
            plan={"roots": {"release_root": str(release_root)}},
            extracted=extracted,
            status_dir=status_dir,
            verify_only=True,
        )
        self.assertEqual(verified["identity"]["version"], "1.0.0")
        for field in ("tree", "script_sha256"):
            saved = manifest["source_code"][field]
            manifest["source_code"][field] = "0" * len(saved)
            with (
                self.subTest(field=field),
                self.assertRaisesRegex(recovery.RecoveryError, "archived source tree differs"),
            ):
                recovery.rehydrate_release_runtime(
                    manifest=manifest,
                    plan={"roots": {"release_root": str(release_root)}},
                    extracted=extracted,
                    status_dir=status_dir,
                    verify_only=True,
                )
            manifest["source_code"][field] = saved
        installed_package = (
            release_root / "airflow-venv" / "lib" / "python3.12" / "site-packages" / "description_pipeline"
        )
        write(installed_package / "__init__.py", "fake\n", mode=0o644)
        with self.assertRaises(recovery.RecoveryError) as raised:
            recovery.rehydrate_release_runtime(
                manifest=manifest,
                plan={"roots": {"release_root": str(release_root)}},
                extracted=extracted,
                status_dir=status_dir,
                verify_only=True,
            )
        self.assertEqual(raised.exception.code, "recovery_release_missing")

    def test_release_archive_rejects_path_traversal(self) -> None:
        archive = self.root / "unsafe-release.zip"
        destination = self.root / "unsafe-release"
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("../escape.txt", "escape\n")
        with self.assertRaises(recovery.RecoveryError):
            recovery._extract_zip_archive(archive, destination)
        self.assertFalse((self.root / "escape.txt").exists())

    def test_manifest_digest_mismatch_fails_before_target_mutation(self) -> None:
        out, manifest = self.backup()
        self.isolate_source()
        sidecar = out / "recovery-manifest.sha256"
        write(sidecar, "0" * 64 + "  recovery-manifest.json\n")
        roots = {row["name"]: row["path"] for row in manifest["roots"] if "reconstructible_runtime" not in row}
        plan = self.target_plan("0" * 64, roots)
        status_dir = self.root / "status-failed"
        with self.assertRaises(recovery.RecoveryError) as raised:
            recovery.restore_command(
                manifest_path=out / "recovery-manifest.json",
                key=self.key,
                target_plan_path=plan,
                status_dir=status_dir,
            )
        self.assertEqual(raised.exception.code, "recovery_manifest_hash_mismatch")
        self.assertFalse(status_dir.exists())
        self.assertFalse(Path(roots["handoff_root"]).exists())

    def test_nonempty_target_is_refused_before_database_restore(self) -> None:
        out, manifest = self.backup()
        self.isolate_source()
        manifest_path = out / "recovery-manifest.json"
        manifest_sha = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        roots = {row["name"]: row["path"] for row in manifest["roots"] if "reconstructible_runtime" not in row}
        Path(roots["handoff_root"]).mkdir(parents=True)
        write(Path(roots["handoff_root"]) / "occupied", "do not replace\n")
        plan = self.target_plan(manifest_sha, roots)
        status_dir = self.root / "status-nonempty"
        with self.assertRaises(recovery.RecoveryError) as raised:
            recovery.restore_command(
                manifest_path=manifest_path,
                key=self.key,
                target_plan_path=plan,
                status_dir=status_dir,
            )
        self.assertEqual(raised.exception.code, "recovery_target_not_empty")
        self.assertFalse(
            any(candidate.name == "pg-restore-complete" for candidate in self.root.rglob("pg-restore-complete"))
        )

    def test_wrong_key_is_refused_before_extraction(self) -> None:
        out, manifest = self.backup()
        self.isolate_source()
        manifest_path = out / "recovery-manifest.json"
        manifest_sha = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        roots = {row["name"]: row["path"] for row in manifest["roots"] if "reconstructible_runtime" not in row}
        plan = self.target_plan(manifest_sha, roots)
        wrong = write(self.root / "wrong.key", os.urandom(32).hex() + "\n")
        wrong.chmod(0o600)
        status_dir = self.root / "status-key"
        with self.assertRaises(recovery.RecoveryError) as raised:
            recovery.restore_command(
                manifest_path=manifest_path,
                key=wrong,
                target_plan_path=plan,
                status_dir=status_dir,
            )
        self.assertEqual(raised.exception.code, "recovery_key_unavailable")
        self.assertFalse(Path(roots["handoff_root"]).exists())

    def test_dsn_authority_mismatch_is_detected(self) -> None:
        one = recovery.parse_pg_dsn(
            "postgresql://operator_ro@/airflow_meta?host=/run/pg&port=5432",
            "one",
        )
        two = recovery.parse_pg_dsn(
            "postgresql://operator_ro@/other?host=/run/pg&port=5432",
            "two",
        )
        self.assertNotEqual(one, two)
        with self.assertRaises(recovery.RecoveryError):
            recovery.parse_pg_dsn(
                "postgresql://operator_ro@/airflow_meta?host=127.0.0.1&port=5432",
                "tcp",
            )

    def test_manifest_duplicate_and_unknown_fields_are_refused(self) -> None:
        base = (
            '{"schema_version":"x","schema_version":"y",'
            '"backup_id":"b","created_at":"t","source_code":{},'
            '"release":{},"database":{},"roles":{},"airflow":{},'
            '"windows":{},'
            '"files":[],"roots":[],"members":[],"references":[],'
            '"reconstructibles":{},"secrets":[],"archive":{},'
            '"producer":{}}'
        )
        with self.assertRaises(recovery.RecoveryError):
            recovery.strict_json_bytes(base.encode(), "manifest")
        parsed = json.loads(base.replace('"schema_version":"x",', ""))
        parsed["unknown"] = True
        with self.assertRaises(recovery.RecoveryError):
            recovery.validate_manifest(parsed, expected_manifest_sha=None)

    def test_manifest_database_live_change_is_refused(self) -> None:
        _, manifest = self.backup()
        manifest["consistency"]["database_live_after_inventory_sha256"] = "0" * 64
        with self.assertRaises(recovery.RecoveryError) as raised:
            recovery.validate_manifest(manifest, expected_manifest_sha=None)
        self.assertEqual(raised.exception.code, "recovery_manifest_invalid")

    def test_manifest_inventory_mismatch_is_refused(self) -> None:
        _, manifest = self.backup()
        manifest["consistency"]["source_inventory_after_sha256"] = "0" * 64
        with self.assertRaises(recovery.RecoveryError) as raised:
            recovery.validate_manifest(manifest, expected_manifest_sha=None)
        self.assertEqual(raised.exception.code, "recovery_manifest_invalid")

    def test_manifest_maintenance_receipt_tamper_is_refused(self) -> None:
        _, manifest = self.backup()
        manifest["maintenance"]["receipt"]["services"]["linux_after"][0]["active_state"] = "active"
        with self.assertRaises(recovery.RecoveryError) as raised:
            recovery.validate_manifest(manifest, expected_manifest_sha=None)
        self.assertEqual(raised.exception.code, "recovery_manifest_invalid")

    def test_manifest_maintenance_member_digest_is_refused(self) -> None:
        _, manifest = self.backup()
        manifest["maintenance"]["sha256"] = "0" * 64
        with self.assertRaises(recovery.RecoveryError) as raised:
            recovery.validate_manifest(manifest, expected_manifest_sha=None)
        self.assertEqual(raised.exception.code, "recovery_manifest_invalid")

    def test_embedded_maintenance_receipt_must_match_archive(self) -> None:
        out, manifest = self.backup()
        status_dir = self.root / "embedded-receipt-status"
        status_dir.mkdir()
        extracted, temporary = recovery.prepare_extraction(
            manifest,
            out / "recovery-manifest.json",
            self.key,
            status_dir,
        )
        try:
            manifest["maintenance"]["receipt"]["closed_at"] = "2099-01-01T00:00:00Z"
            with self.assertRaises(recovery.RecoveryError) as raised:
                recovery.verify_archive_consistency(manifest, extracted)
            self.assertEqual(
                raised.exception.code,
                "recovery_manifest_invalid",
            )
        finally:
            temporary.cleanup()

    def test_maintenance_receipt_semantic_rewrites_are_refused(self) -> None:
        _, manifest = self.backup()
        rewrites = (
            lambda row: row.update(
                opened_at="2099-01-01T00:00:00Z",
                closed_at="1999-01-01T00:00:00Z",
            ),
            lambda row: row["identity"].update(operatorctl_sha256="0" * 64),
            lambda row: row["commands"]["drain"].update(
                argv=["echo", "fixture"],
            ),
            lambda row: row["commands"]["stop"].update(
                argv=["echo", "fixture"],
            ),
            lambda row: row["services"].update(
                linux_after=row["services"]["linux_after"][:1],
            ),
            lambda row: row.update(backup_id="another-backup"),
        )
        for rewrite in rewrites:
            with self.subTest(rewrite=rewrite):
                changed = copy.deepcopy(manifest)
                rewrite(changed["maintenance"]["receipt"])
                with self.assertRaises(recovery.RecoveryError) as raised:
                    recovery.validate_manifest(
                        changed,
                        expected_manifest_sha=None,
                    )
                self.assertEqual(
                    raised.exception.code,
                    "recovery_manifest_invalid",
                )

    def test_maintenance_receipt_binds_archived_task_and_install(self) -> None:
        out, manifest = self.backup()
        status_dir = self.root / "maintenance-archive-binding-status"
        status_dir.mkdir()
        extracted, temporary = recovery.prepare_extraction(
            manifest,
            out / "recovery-manifest.json",
            self.key,
            status_dir,
        )
        try:
            rewrites = (
                lambda row: row["identity"].update(
                    install_id="arbitrary-install",
                ),
                lambda row: (
                    row["services"]["windows_before"].update(
                        task_name="arbitrary-task",
                    ),
                    row["services"]["windows_after"].update(
                        task_name="arbitrary-task",
                    ),
                ),
            )
            for rewrite in rewrites:
                with self.subTest(rewrite=rewrite):
                    changed = copy.deepcopy(manifest)
                    rewrite(changed["maintenance"]["receipt"])
                    with self.assertRaises(
                        recovery.RecoveryError,
                    ) as raised:
                        recovery.verify_maintenance_archive_consistency(
                            changed,
                            extracted,
                        )
                    self.assertEqual(
                        raised.exception.code,
                        "recovery_manifest_invalid",
                    )
        finally:
            temporary.cleanup()

    def test_post_quiesce_failure_reports_maintenance_state(self) -> None:
        for marker_name, expected in (
            ("maintenance-window.pending.json", "unknown_or_stopped"),
            ("maintenance-window.stopped.json", "stopped"),
        ):
            with self.subTest(marker=marker_name):
                out = self.root / expected
                claim = recovery.claim_backup_output(out, "failed-window")
                write(
                    out / marker_name,
                    json.dumps(
                        {
                            "config": str(self.context.config_path),
                            "operatorctl_path": str(Path(recovery.__file__).with_name("operatorctl.sh")),
                        }
                    ),
                )
                args = argparse.Namespace(
                    out=out,
                    backup_id="failed-window",
                    config=self.context.config_path,
                )
                result = recovery.record_backup_failure(
                    args,
                    recovery.RecoveryError(
                        "fixture_failure",
                        "post-quiesce failure",
                    ),
                    claim,
                )
                self.assertEqual(result["maintenance_state"], expected)
                receipt = json.loads((out / "receipt.json").read_text(encoding="utf-8"))
                self.assertEqual(receipt["maintenance_state"], expected)
                self.assertIn("operatorctl.sh start --config", receipt["recovery_hint"])

    def test_backup_cli_reports_system_exit_and_oserror_after_quiescence(self) -> None:
        for index, error in enumerate(
            (
                SystemExit("backup: openssl failed"),
                OSError("disk full"),
            )
        ):
            with self.subTest(error=type(error).__name__):
                out = self.root / f"cli-failure-{index}"
                claim = recovery.claim_backup_output(out, "cli-failure")
                write(
                    out / "maintenance-window.pending.json",
                    json.dumps(
                        {
                            "config": str(self.context.config_path),
                            "operatorctl_path": str(Path(recovery.__file__).with_name("operatorctl.sh")),
                        }
                    ),
                )

                def fail(_error=error, _claim=claim, **_kwargs):
                    _error.backup_claim = _claim
                    raise _error

                stderr = io.StringIO()
                with (
                    mock.patch.object(
                        recovery,
                        "backup_command",
                        side_effect=fail,
                    ),
                    contextlib.redirect_stderr(stderr),
                ):
                    code = recovery.main(
                        [
                            "backup",
                            "--config",
                            str(self.context.config_path),
                            "--backup-id",
                            "cli-failure",
                            "--key-file",
                            str(self.key),
                            "--out",
                            str(out),
                        ]
                    )
                self.assertEqual(code, 1)
                self.assertIn(
                    "maintenance_state=unknown_or_stopped",
                    stderr.getvalue(),
                )
                self.assertIn("recovery_hint=", stderr.getvalue())
                receipt = json.loads((out / "receipt.json").read_text(encoding="utf-8"))
                self.assertEqual(receipt["status"], "failed")
                self.assertEqual(
                    receipt["maintenance_state"],
                    "unknown_or_stopped",
                )

    def test_early_backup_cli_reports_not_started(self) -> None:
        out = self.root / "early-failure"
        stderr = io.StringIO()

        def fail(**_kwargs):
            raise recovery.RecoveryError(
                "recovery_target_plan_invalid",
                "bad backup id",
            )

        with (
            mock.patch.object(
                recovery,
                "backup_command",
                side_effect=fail,
            ),
            contextlib.redirect_stderr(stderr),
        ):
            code = recovery.main(
                [
                    "backup",
                    "--config",
                    str(self.context.config_path),
                    "--backup-id",
                    "early-failure",
                    "--key-file",
                    str(self.key),
                    "--out",
                    str(out),
                ]
            )
        self.assertEqual(code, 1)
        self.assertIn("maintenance_state=not_started", stderr.getvalue())
        self.assertIn(
            "recovery_failure_receipt=unavailable",
            stderr.getvalue(),
        )
        self.assertFalse((out / "receipt.json").exists())

    def test_early_invalid_backup_id_never_changes_an_existing_success(self) -> None:
        out = self.root / "existing-success"
        out.mkdir()
        write(
            out / "receipt.json",
            json.dumps(
                {
                    "schema_version": recovery.RECEIPT_SCHEMA,
                    "operation": "backup",
                    "backup_id": "existing-success",
                    "status": "succeeded",
                    "claim_id": "a" * 64,
                }
            ),
        )
        write(out / "backup" / "source.txt", "preserve me\n")
        before = {path.relative_to(out).as_posix(): path.read_bytes() for path in out.rglob("*") if path.is_file()}
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = recovery.main(
                [
                    "backup",
                    "--config",
                    str(self.context.config_path),
                    "--backup-id",
                    "INVALID/ID",
                    "--key-file",
                    str(self.key),
                    "--out",
                    str(out),
                ]
            )
        self.assertEqual(code, 1)
        self.assertIn("maintenance_state=not_started", stderr.getvalue())
        after = {path.relative_to(out).as_posix(): path.read_bytes() for path in out.rglob("*") if path.is_file()}
        self.assertEqual(after, before)

    def test_symlink_output_is_refused_without_changing_the_target(self) -> None:
        target = self.root / "symlink-target"
        target.mkdir()
        receipt = write(
            target / "receipt.json",
            json.dumps(
                {
                    "schema_version": recovery.RECEIPT_SCHEMA,
                    "operation": "backup",
                    "backup_id": "existing",
                    "status": "succeeded",
                }
            ),
        )
        before = receipt.read_bytes()
        out = self.root / "symlink-output"
        out.symlink_to(target, target_is_directory=True)
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = recovery.main(
                [
                    "backup",
                    "--config",
                    str(self.context.config_path),
                    "--backup-id",
                    "symlink-refused",
                    "--key-file",
                    str(self.key),
                    "--out",
                    str(out),
                ]
            )
        self.assertEqual(code, 1)
        self.assertIn("maintenance_state=not_started", stderr.getvalue())
        self.assertEqual(receipt.read_bytes(), before)

    def test_concurrent_output_claim_has_one_winner(self) -> None:
        out = self.root / "concurrent-output"
        barrier = threading.Barrier(2)
        outcomes: list[tuple[str, str]] = []

        def attempt(backup_id: str) -> None:
            barrier.wait()
            try:
                claim = recovery.claim_backup_output(out, backup_id)
            except recovery.RecoveryError as error:
                outcomes.append(("error", error.code))
            else:
                outcomes.append(("claim", claim.claim_id))

        threads = [
            threading.Thread(target=attempt, args=("concurrent-a",)),
            threading.Thread(target=attempt, args=("concurrent-b",)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(
            sorted(kind for kind, _ in outcomes),
            ["claim", "error"],
        )
        self.assertEqual(
            next(value for kind, value in outcomes if kind == "error"),
            "recovery_write_conflict",
        )
        receipt = json.loads((out / "receipt.json").read_text(encoding="utf-8"))
        self.assertEqual(
            receipt["claim_id"],
            next(value for kind, value in outcomes if kind == "claim"),
        )

    def test_backup_holds_the_shared_lifecycle_lock_through_quiescence(self) -> None:
        observed: list[str] = []

        def observe_lock(context, database, **kwargs):
            state_root = Path(context.resolved["storage"]["state_root"])
            with self.assertRaises(recovery.RecoveryError) as raised:
                recovery.acquire_maintenance_lock(state_root)
            observed.append(raised.exception.code)
            return fake_maintenance_state(
                context,
                database,
                **kwargs,
            )

        with mock.patch.object(
            recovery,
            "quiesce_maintenance",
            side_effect=observe_lock,
        ):
            _, manifest = self.backup()
        self.assertEqual(
            observed,
            ["recovery_maintenance_not_exclusive"],
        )
        lock_record = manifest["consistency"]["maintenance_lock"]
        self.assertTrue(lock_record["held"])
        lock = recovery.acquire_maintenance_lock(Path(self.context.resolved["storage"]["state_root"]))
        lock.close()

    def test_failure_receipt_and_cleanup_run_while_backup_holds_the_lock(
        self,
    ) -> None:
        observed: list[str] = []
        original_record = recovery.record_backup_failure

        def observe_failure(args, error, claim):
            with self.assertRaises(recovery.RecoveryError) as raised:
                recovery.acquire_maintenance_lock(Path(self.context.resolved["storage"]["state_root"]))
            observed.append(raised.exception.code)
            return original_record(args, error, claim)

        with (
            mock.patch.object(
                recovery,
                "load_backup_context",
                return_value=self.context,
            ),
            mock.patch.object(
                recovery,
                "_backup_command_claimed",
                side_effect=recovery.RecoveryError(
                    "fixture_failure",
                    "failure after claim",
                ),
            ),
            mock.patch.object(
                recovery,
                "record_backup_failure",
                side_effect=observe_failure,
            ),
            self.assertRaises(recovery.RecoveryError) as raised,
        ):
            recovery.backup_command(
                config_path=self.context.config_path,
                backup_id="failure-lock",
                key_file=self.key,
                out=self.root / "failure-lock",
            )
        self.assertEqual(
            observed,
            ["recovery_maintenance_not_exclusive"],
        )
        self.assertEqual(
            raised.exception.backup_failure["maintenance_state"],
            "not_started",
        )
        lock = recovery.acquire_maintenance_lock(Path(self.context.resolved["storage"]["state_root"]))
        lock.close()

    def test_failure_reporting_survives_cleanup_error(self) -> None:
        out = self.root / "cleanup-failure"
        claim = recovery.claim_backup_output(out, "cleanup-failure")
        write(
            out / "maintenance-window.stopped.json",
            json.dumps(
                {
                    "config": str(self.context.config_path),
                    "operatorctl_path": str(Path(recovery.__file__).with_name("operatorctl.sh")),
                }
            ),
        )
        args = argparse.Namespace(
            out=out,
            backup_id="cleanup-failure",
            config=self.context.config_path,
        )
        with mock.patch.object(
            recovery.backup,
            "remove_private_staging",
            side_effect=OSError("disk full"),
        ):
            result = recovery.record_backup_failure(
                args,
                recovery.RecoveryError("fixture_failure", "later failure"),
                claim,
            )
        self.assertEqual(result["maintenance_state"], "stopped")
        receipt = json.loads((out / "receipt.json").read_text(encoding="utf-8"))
        self.assertEqual(receipt["maintenance_state"], "stopped")
        self.assertFalse(receipt["private_staging_removed"])

    def test_success_is_preserved_when_post_completion_cleanup_fails(self) -> None:
        out = self.root / "success-cleanup"
        claim = recovery.claim_backup_output(out, "success-cleanup")
        write(
            out / "maintenance-window.stopped.json",
            json.dumps(
                {
                    "config": str(self.context.config_path),
                    "operatorctl_path": str(Path(recovery.__file__).with_name("operatorctl.sh")),
                }
            ),
        )
        succeeded = {
            "schema_version": recovery.RECEIPT_SCHEMA,
            "operation": "backup",
            "backup_id": "success-cleanup",
            "claim_id": claim.claim_id,
            "status": "succeeded",
        }
        atomic = out / "receipt.json"
        before = recovery.canonical_json(succeeded)
        recovery.atomic_write(atomic, before)
        args = argparse.Namespace(
            out=out,
            backup_id="success-cleanup",
            config=self.context.config_path,
        )
        with mock.patch.object(
            recovery.backup,
            "remove_private_staging",
            side_effect=OSError("cleanup failed"),
        ):
            result = recovery.record_backup_failure(
                args,
                recovery.RecoveryError("fixture_failure", "cleanup failed"),
                claim,
            )
        self.assertEqual(result["maintenance_state"], "stopped")
        self.assertTrue(result["recovery_hint"])
        self.assertEqual(atomic.read_bytes(), before)

    def test_backup_success_records_a_cleanup_warning_without_rewriting_result(
        self,
    ) -> None:
        out = self.root / "cleanup-warning"
        with (
            mock.patch.object(
                recovery,
                "load_backup_context",
                return_value=self.context,
            ),
            mock.patch.object(
                recovery.backup,
                "remove_private_staging",
                side_effect=[False, OSError("cleanup failed")],
            ),
        ):
            result = recovery.backup_command(
                config_path=self.context.config_path,
                backup_id="cleanup-warning",
                key_file=self.key,
                out=out,
            )
        receipt = json.loads((out / "receipt.json").read_text(encoding="utf-8"))
        self.assertEqual(receipt["status"], "succeeded")
        self.assertFalse(receipt["private_staging_removed"])
        self.assertEqual(receipt["cleanup_error"], "cleanup failed")
        self.assertEqual(result["cleanup_warning"], "cleanup failed")

    def test_stdout_failure_after_success_does_not_report_not_started(self) -> None:
        class BrokenStdout(io.StringIO):
            def write(self, _value):
                raise BrokenPipeError("consumer closed")

        stderr = io.StringIO()
        completed = {
            "manifest": "/fixture/manifest.json",
            "manifest_sha256": "a" * 64,
            "archive": "/fixture/recovery.tar.enc",
            "archive_sha256": "b" * 64,
        }
        with (
            mock.patch.object(recovery, "backup_command", return_value=completed),
            mock.patch.object(recovery.sys, "stdout", new=BrokenStdout()),
            contextlib.redirect_stderr(stderr),
        ):
            code = recovery.main(
                [
                    "backup",
                    "--config",
                    str(self.context.config_path),
                    "--backup-id",
                    "completed-output-failure",
                    "--key-file",
                    str(self.key),
                    "--out",
                    str(self.root / "completed-output-failure"),
                ]
            )
        self.assertEqual(code, 1)
        self.assertIn("maintenance_state=stopped", stderr.getvalue())
        self.assertNotIn("maintenance_state=not_started", stderr.getvalue())

    def test_successful_backup_cli_reports_stopped(self) -> None:
        stdout = io.StringIO()
        with (
            mock.patch.object(
                recovery,
                "backup_command",
                return_value={
                    "manifest": "/fixture/manifest.json",
                    "manifest_sha256": "a" * 64,
                    "archive": "/fixture/recovery.tar.enc",
                    "archive_sha256": "b" * 64,
                },
            ),
            contextlib.redirect_stdout(stdout),
        ):
            code = recovery.main(
                [
                    "backup",
                    "--config",
                    str(self.context.config_path),
                    "--backup-id",
                    "successful",
                    "--key-file",
                    str(self.key),
                    "--out",
                    str(self.root / "successful"),
                ]
            )
        self.assertEqual(code, 0)
        self.assertIn("maintenance_state=stopped", stdout.getvalue())

    def test_active_linux_writer_is_refused(self) -> None:
        systemctl = write(
            self.root / "systemctl",
            textwrap.dedent(
                """\
                #!/usr/bin/env python3
                import sys

                unit = sys.argv[-1]
                print("active" if unit == "description-portal" else "inactive")
                """
            ),
            mode=0o755,
        )
        systemctl.chmod(0o755)
        with (
            mock.patch.dict(
                os.environ,
                {"PATH": f"{self.root}:/usr/bin:/bin"},
            ),
            self.assertRaises(recovery.RecoveryError) as raised,
        ):
            recovery._linux_unit_states(timeout=5)
        self.assertEqual(
            raised.exception.code,
            "recovery_maintenance_not_exclusive",
        )
        self.assertEqual(
            raised.exception.detail["unit"],
            "description-portal",
        )

    def test_windows_writer_must_become_disabled(self) -> None:
        evidence = {
            "argv": ["ssh", "windows-fixture"],
            "returncode": 0,
            "stdout_sha256": hashlib.sha256(b"Running").hexdigest(),
            "stderr_sha256": hashlib.sha256(b"").hexdigest(),
        }
        completed = subprocess.CompletedProcess(
            ["ssh", "windows-fixture"],
            0,
            "Disabled",
            "",
        )
        completed.transport = {
            "encoding": "base64(gzip(utf-8))",
            "script_sha256": "a" * 64,
            "script_bytes": 100,
            "payload_sha256": "b" * 64,
            "payload_bytes": 80,
            "launcher_sha256": "c" * 64,
            "launcher_bytes": 70,
        }
        evidence["stdout_sha256"] = hashlib.sha256(b"Disabled").hexdigest()
        evidence["transport"] = dict(completed.transport)

        def run_windows(*, commands, **kwargs):
            del kwargs
            commands.append(dict(evidence))
            return completed

        with (
            mock.patch.object(
                recovery,
                "_windows_task_state",
                return_value=("Running", evidence),
            ),
            mock.patch.object(
                recovery,
                "_run_windows_powershell",
                side_effect=run_windows,
            ),
            self.assertRaises(recovery.RecoveryError) as raised,
        ):
            recovery._disable_windows_task(
                ssh_host="windows-fixture",
                task_name="description-e2e-endpoint",
                timeout=5,
            )
        self.assertEqual(
            raised.exception.code,
            "recovery_maintenance_not_exclusive",
        )
        self.assertEqual(raised.exception.detail["state"], "Running")

    def test_windows_target_plan_must_equal_the_archived_identity(self) -> None:
        out, manifest = self.backup()
        del out
        manifest_path = self.root / "recovery" / "recovery-manifest.json"
        manifest_sha = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        roots = {row["name"]: row["path"] for row in manifest["roots"] if "reconstructible_runtime" not in row}
        plan_path = self.target_plan(manifest_sha, roots)
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        plan["windows"]["principal_sid"] = "S-1-5-21-9"
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        with self.assertRaises(recovery.RecoveryError) as raised:
            recovery.validate_target_plan(plan, manifest, manifest_sha)
        self.assertEqual(raised.exception.code, "recovery_target_plan_invalid")

    def test_windows_state_refuses_unknown_cad_ownership(self) -> None:
        _out, manifest = self.backup()
        state = copy.deepcopy(manifest["windows"]["state"])
        state["cad"]["lease_held"] = True
        with self.assertRaises(recovery.RecoveryError) as raised:
            recovery._validate_windows_recovery_state(state)
        self.assertEqual(raised.exception.code, "windows_cad_ownership_unknown")

    def test_windows_state_accepts_inherited_runtime_leaf_acls(self) -> None:
        _out, manifest = self.backup()
        fixture = json.loads((REPOSITORY / "tests" / "fixtures" / "windows_acl_policy.json").read_text())
        leaf = copy.deepcopy(next(case for case in fixture["cases"] if case["name"] == "runtime_inherited")["leaf"])
        leaf["projection_sha256"] = recovery._windows_acl_projection_sha256(leaf)
        for role in ("cad_ownership", "cad_lease"):
            with self.subTest(role=role):
                state = copy.deepcopy(manifest["windows"]["state"])
                state["acl"][role] = copy.deepcopy(leaf)
                if role == "cad_ownership":
                    ledger = b'{"schema_version":"solidworks-to-urdf.cad-ownership/v1","records":[]}'
                    ledger_sha = hashlib.sha256(ledger).hexdigest()
                    state["cad"].update(ledger_present=True, ownership_sha256=ledger_sha)
                    state["files"].append(
                        {
                            "role": role,
                            "path": state["cad"]["ownership_path"],
                            "member": "cad-ownership.json",
                            "bytes": len(ledger),
                            "sha256": ledger_sha,
                        }
                    )
                    state["members"].append(
                        {
                            "member": "cad-ownership.json",
                            "bytes": len(ledger),
                            "sha256": ledger_sha,
                        }
                    )
                validated = recovery._validate_windows_recovery_state(state)
                self.assertFalse(validated["acl"][role]["protected"])

    def test_windows_state_accepts_only_the_exact_legacy_transition_action(self) -> None:
        _out, manifest = self.backup()
        state = copy.deepcopy(manifest["windows"]["state"])
        endpoint_root = state["identity"]["endpoint_root"]
        state["identity"]["runtime_launch_contract"] = recovery.WINDOWS_LEGACY_LAUNCH_CONTRACT
        state["identity"]["release_identity_sha256"] = recovery.WINDOWS_LEGACY_RELEASE_IDENTITY_SHA256
        state["task"]["action"] = {
            "execute": endpoint_root + "/runtime/Scripts/description.exe",
            "arguments": 'serve --config "' + endpoint_root + '/endpoint.json"',
            "working_directory": state["identity"]["state_root"],
        }
        validated = recovery._validate_windows_recovery_state(state)
        self.assertEqual(
            validated["identity"]["runtime_launch_contract"],
            recovery.WINDOWS_LEGACY_LAUNCH_CONTRACT,
        )
        previous_contract_format = copy.deepcopy(state)
        previous_contract_format["identity"].pop("runtime_launch_contract")
        before_digest = recovery._digest_document(previous_contract_format)
        validated = recovery._validate_windows_recovery_state(previous_contract_format)
        self.assertEqual(recovery._digest_document(validated), before_digest)

    def test_windows_state_accepts_only_the_exact_bytecode_transition_action(self) -> None:
        _out, manifest = self.backup()
        state = copy.deepcopy(manifest["windows"]["state"])
        endpoint_root = state["identity"]["endpoint_root"]
        state["identity"]["release_identity_sha256"] = recovery.WINDOWS_BYTECODE_TRANSITION_RELEASE_IDENTITY_SHA256
        state["identity"]["runtime_launch_contract"] = recovery.WINDOWS_PYTHON_LAUNCH_CONTRACT
        state["task"]["action"] = {
            "execute": endpoint_root + "/runtime/Scripts/python.exe",
            "arguments": '-I -m description_pipeline serve --config "' + endpoint_root + '/endpoint.json"',
            "working_directory": state["identity"]["state_root"],
        }
        validated = recovery._validate_windows_recovery_state(state)
        self.assertEqual(
            validated["identity"]["runtime_launch_contract"],
            recovery.WINDOWS_PYTHON_LAUNCH_CONTRACT,
        )
        foreign = copy.deepcopy(state)
        foreign["identity"]["release_identity_sha256"] = "e" * 64
        with self.assertRaises(recovery.RecoveryError) as raised:
            recovery._validate_windows_recovery_state(foreign)
        self.assertEqual(raised.exception.code, "windows_recovery_state_invalid")

    def test_windows_state_negative_boundary_matrix(self) -> None:
        _out, manifest = self.backup()

        def reverse_acl_rules(state):
            projection = state["acl"]["roots"]["state_root"]
            first = projection["rules"][0]
            projection["rules"] = [
                {**first, "sid": "S-1-5-21-2"},
                first,
            ]
            projection["projection_sha256"] = recovery._windows_acl_projection_sha256(projection)

        def remove_required_acl_rule(state):
            projection = state["acl"]["endpoint_token"]
            projection["rules"] = [rule for rule in projection["rules"] if rule["sid"] != "S-1-5-32-544"]
            projection["projection_sha256"] = recovery._windows_acl_projection_sha256(projection)

        def move_marker(state):
            row = next(item for item in state["files"] if item["role"] == "managed_root_marker")
            row["path"] = "C:/unrelated/managed-root.json"

        cases = (
            (
                "started",
                lambda state: state["task"].update(state="Running"),
                "windows_task_not_disabled",
            ),
            (
                "operator_not_admin",
                lambda state: state["identity"].update(operator_administrator=False),
                "windows_operator_not_administrator",
            ),
            (
                "operator_runtime_mismatch",
                lambda state: state["identity"].update(operator_sid="S-1-5-21-2"),
                "windows_recovery_state_invalid",
            ),
            (
                "runtime_qualification",
                lambda state: state["identity"].update(runtime_privilege_qualification="least-privilege"),
                "windows_recovery_state_invalid",
            ),
            (
                "runtime_not_admin",
                lambda state: (
                    state["task"]["principal"].update(administrator=False),
                    state["identity"].update(
                        runtime_administrator=False,
                        runtime_privilege_qualification="non-administrative-runtime",
                    ),
                ),
                "windows_recovery_state_invalid",
            ),
            (
                "run_level",
                lambda state: state["task"]["principal"].update(run_level="Limited"),
                "windows_run_level_not_elevated",
            ),
            (
                "legacy_contract_with_python_action",
                lambda state: state["identity"].update(runtime_launch_contract=recovery.WINDOWS_LEGACY_LAUNCH_CONTRACT),
                "windows_recovery_state_invalid",
            ),
            (
                "legacy_contract_wrong_release",
                lambda state: (
                    state["identity"].update(runtime_launch_contract=recovery.WINDOWS_LEGACY_LAUNCH_CONTRACT),
                    state["task"]["action"].update(
                        execute=state["identity"]["endpoint_root"] + "/runtime/Scripts/description.exe",
                        arguments='serve --config "' + state["identity"]["endpoint_root"] + '/endpoint.json"',
                    ),
                ),
                "windows_recovery_state_invalid",
            ),
            (
                "legacy_release_python_contract",
                lambda state: state["identity"].update(
                    release_identity_sha256=recovery.WINDOWS_LEGACY_RELEASE_IDENTITY_SHA256,
                    runtime_launch_contract=recovery.WINDOWS_PYTHON_LAUNCH_CONTRACT,
                ),
                "windows_recovery_state_invalid",
            ),
            (
                "missing_contract_with_python_action",
                lambda state: state["identity"].pop("runtime_launch_contract"),
                "windows_recovery_state_invalid",
            ),
            (
                "explicit_null_contract",
                lambda state: state["identity"].update(runtime_launch_contract=None),
                "windows_recovery_state_invalid",
            ),
            (
                "unsupported_contract",
                lambda state: state["identity"].update(runtime_launch_contract="foreign_launcher_v1"),
                "windows_recovery_state_invalid",
            ),
            (
                "legacy_description_launcher",
                lambda state: state["task"]["action"].update(
                    execute=state["identity"]["endpoint_root"] + "/runtime/Scripts/description.exe"
                ),
                "windows_recovery_state_invalid",
            ),
            (
                "legacy_serve_arguments",
                lambda state: state["task"]["action"].update(
                    arguments='serve --config "' + state["identity"]["endpoint_root"] + '/endpoint.json"'
                ),
                "windows_recovery_state_invalid",
            ),
            (
                "python_module_argument_suffix",
                lambda state: state["task"]["action"].update(
                    arguments=state["task"]["action"]["arguments"] + " --foreign"
                ),
                "windows_recovery_state_invalid",
            ),
            (
                "logon",
                lambda state: state["task"]["principal"].update(logon_type="Password"),
                "windows_logon_type_unsupported",
            ),
            (
                "token",
                lambda state: state["identity"].update(token_sha256="short"),
                "windows_recovery_state_invalid",
            ),
            (
                "acl",
                lambda state: state["acl"]["task_file"].pop("owner_sid"),
                "recovery_manifest_invalid",
            ),
            (
                "acl_rule",
                lambda state: state["acl"]["roots"]["state_root"].update(rules=[1]),
                "recovery_manifest_invalid",
            ),
            (
                "acl_digest",
                lambda state: state["acl"]["roots"]["package_root"].update(sddl="D:FA"),
                "windows_recovery_state_invalid",
            ),
            (
                "acl_rule_order",
                reverse_acl_rules,
                "windows_recovery_state_invalid",
            ),
            (
                "acl_policy",
                remove_required_acl_rule,
                "windows_managed_acl_policy_invalid",
            ),
            (
                "path",
                lambda state: state["identity"].update(config_path="C:/../escape"),
                "windows_recovery_state_invalid",
            ),
            (
                "null_operator",
                lambda state: state["identity"].update(operator_sid=None),
                "windows_recovery_state_invalid",
            ),
            (
                "token_in_state_root",
                lambda state: state["identity"].update(token_path=state["identity"]["state_root"] + "/token"),
                "windows_recovery_state_invalid",
            ),
            (
                "moved_marker",
                move_marker,
                "windows_recovery_state_invalid",
            ),
        )
        for name, mutate, code in cases:
            with self.subTest(name=name):
                changed = copy.deepcopy(manifest["windows"]["state"])
                mutate(changed)
                with self.assertRaises(recovery.RecoveryError) as raised:
                    recovery._validate_windows_recovery_state(changed)
                self.assertEqual(raised.exception.code, code)

    def test_windows_restore_never_accepts_a_started_task(self) -> None:
        out, manifest = self.backup()
        manifest_path = out / "recovery-manifest.json"
        manifest_sha = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        roots = {row["name"]: row["path"] for row in manifest["roots"] if "reconstructible_runtime" not in row}
        plan_path = self.target_plan(manifest_sha, roots)
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        status_dir = self.root / "windows-restore-status"
        status_dir.mkdir()
        extracted, temporary = recovery.prepare_extraction(
            manifest,
            manifest_path,
            self.key,
            status_dir,
        )
        self.addCleanup(temporary.cleanup)
        with (
            mock.patch.object(
                recovery,
                "_transfer_windows_recovery",
                return_value={
                    "passed": True,
                    "verb": "recovery-restore",
                    "task_state": "Running",
                    "endpoint": "stopped",
                    "cad_ownership": "free",
                    "state": manifest["windows"]["state"],
                    "remote_cleanup": {
                        "attempted": True,
                        "passed": True,
                        "remote_dir": "C:/description/state/recovery/restore/windows-fixture",
                    },
                    "cleanup": {
                        "attempted": True,
                        "passed": True,
                        "path": "C:/Users/test/AppData/Local/Temp/windows-recovery-fixture",
                    },
                },
            ),
            self.assertRaises(recovery.RecoveryError) as raised,
        ):
            self.real_restore_windows_recovery(
                manifest=manifest,
                plan=plan,
                extracted=extracted,
                status_dir=status_dir,
                verify_only=False,
                commands=[],
            )
        self.assertEqual(raised.exception.code, "windows_recovery_verification_failed")

    def test_windows_restore_hashes_actual_state_and_rejects_missing_or_changed_state(self) -> None:
        out, manifest = self.backup()
        manifest_path = out / "recovery-manifest.json"
        roots = {row["name"]: row["path"] for row in manifest["roots"] if "reconstructible_runtime" not in row}
        plan = json.loads(self.target_plan(recovery.sha256_file(manifest_path), roots).read_text())
        status_dir = self.root / "windows-canonical"
        status_dir.mkdir()
        extracted, temporary = recovery.prepare_extraction(manifest, manifest_path, self.key, status_dir)
        self.addCleanup(temporary.cleanup)
        actual = json.loads(
            json.dumps(manifest["windows"]["state"]), object_pairs_hook=lambda pairs: dict(reversed(pairs))
        )
        response = {
            "passed": True,
            "task_state": "Disabled",
            "endpoint": "stopped",
            "cad_ownership": "free",
            "state": actual,
            "remote_cleanup": {"attempted": True, "passed": True, "remote_dir": "C:/recovery/test"},
            "cleanup": {"attempted": True, "passed": True, "path": "C:/Temp/test"},
        }
        with mock.patch.object(recovery, "_transfer_windows_recovery", return_value=response):
            result = self.real_restore_windows_recovery(
                manifest=manifest, plan=plan, extracted=extracted, status_dir=status_dir, verify_only=True, commands=[]
            )
        self.assertEqual(result["state_sha256"], recovery._digest_document(actual))
        changed = copy.deepcopy(actual)
        changed["ssh_host"] = "changed-authority"
        for state in (None, changed):
            with (
                self.subTest(state=state is None),
                mock.patch.object(
                    recovery,
                    "_transfer_windows_recovery",
                    return_value={**response, "state": state, "state_sha256": manifest["windows"]["state_sha256"]},
                ),
                self.assertRaises(recovery.RecoveryError),
            ):
                self.real_restore_windows_recovery(
                    manifest=manifest,
                    plan=plan,
                    extracted=extracted,
                    status_dir=status_dir,
                    verify_only=True,
                    commands=[],
                )

    def test_windows_bundle_refuses_an_extra_member(self) -> None:
        _out, manifest = self.backup()
        status_dir = self.root / "windows-extra-status"
        status_dir.mkdir()
        extracted, temporary = recovery.prepare_extraction(
            manifest,
            self.root / "recovery" / "recovery-manifest.json",
            self.key,
            status_dir,
        )
        self.addCleanup(temporary.cleanup)
        bundle = extracted / manifest["windows"]["bundle"]["member"]
        with zipfile.ZipFile(bundle, "a", compression=zipfile.ZIP_STORED) as archive:
            archive.writestr("extra.bin", b"unexpected")
        manifest["windows"]["bundle"]["bytes"] = bundle.stat().st_size
        manifest["windows"]["bundle"]["sha256"] = recovery.sha256_file(bundle)
        with self.assertRaises(recovery.RecoveryError) as raised:
            recovery.verify_windows_recovery_archive(manifest, extracted)
        self.assertEqual(raised.exception.code, "windows_recovery_bundle_invalid")

    def test_windows_restore_binding_carries_only_hashes_and_paths(self) -> None:
        _out, manifest = self.backup()
        target = recovery._windows_target_from_state(manifest["windows"]["state"])
        binding = recovery._windows_restore_binding(
            ssh_host=target["ssh_host"],
            target=target,
            remote_bundle="C:/description/state/recovery/recovery.zip",
            bundle_sha256=manifest["windows"]["bundle"]["sha256"],
        )
        encoded = recovery.canonical_json(binding)
        self.assertNotIn(b"t" * 64, encoded)
        self.assertNotIn(b'"token"', encoded)
        self.assertIn(b'"token_sha256"', encoded)

    def test_windows_snapshot_orchestration_binds_the_downloaded_state(self) -> None:
        staging = self.root / "windows-snapshot-staging"
        staging.mkdir()
        expected, _state = windows_recovery_fixture(self.context, staging)
        with mock.patch.object(
            recovery,
            "_transfer_windows_recovery",
            return_value=windows_snapshot_receipt(),
        ):
            captured = self.real_capture_windows_recovery(
                self.context,
                backup_id="windows-snapshot",
                staging=staging,
                commands=[],
            )
        self.assertEqual(
            captured["bundle"]["sha256"],
            expected["bundle"]["sha256"],
        )
        self.assertEqual(captured["state"], expected["state"])
        self.assertEqual(captured["remote_cleanup"], expected["remote_cleanup"])

    def test_windows_snapshot_carries_cleanup_residual_into_the_manifest_block(self) -> None:
        staging = self.root / "windows-snapshot-cleanup-staging"
        staging.mkdir()
        windows_recovery_fixture(self.context, staging)
        residual = {
            "attempted": True,
            "passed": False,
            "remote_dir": "C:/description/state/recovery/snapshot/windows-fixture",
            "ownership_marker": ("C:/description/state/recovery/snapshot/windows-fixture/.recovery-owner"),
            "code": "windows_recovery_transport_unknown",
            "message": "fixture cleanup failed",
            "detail": {"stderr_sha256": "0" * 64},
        }
        with mock.patch.object(
            recovery,
            "_transfer_windows_recovery",
            return_value=windows_snapshot_receipt(remote_cleanup=residual),
        ):
            captured = self.real_capture_windows_recovery(
                self.context,
                backup_id="windows-snapshot-cleanup",
                staging=staging,
                commands=[],
            )
        self.assertEqual(captured["remote_cleanup"], residual)

    def test_windows_snapshot_carries_powershell_temp_cleanup_residual(self) -> None:
        staging = self.root / "windows-snapshot-inner-cleanup-staging"
        staging.mkdir()
        windows_recovery_fixture(self.context, staging)
        residual = {
            "attempted": True,
            "passed": False,
            "path": "C:/Users/test/AppData/Local/Temp/windows-recovery-fixture",
            "error": "access denied",
        }
        with mock.patch.object(
            recovery,
            "_transfer_windows_recovery",
            return_value=windows_snapshot_receipt(inner_cleanup=residual),
        ):
            captured = self.real_capture_windows_recovery(
                self.context,
                backup_id="windows-snapshot-inner-cleanup",
                staging=staging,
                commands=[],
            )
        self.assertEqual(captured["windows_cleanup"], residual)

    def test_windows_snapshot_refuses_a_tampered_member_before_acceptance(self) -> None:
        staging = self.root / "windows-tampered-staging"
        staging.mkdir()
        _expected, _state = windows_recovery_fixture(self.context, staging)
        bundle = staging / recovery.WINDOWS_BUNDLE_MEMBER
        replacement = staging / "replacement.zip"
        with zipfile.ZipFile(bundle) as source, zipfile.ZipFile(replacement, "w") as target:
            for name in source.namelist():
                payload = source.read(name)
                if name == "token":
                    payload = b"tampered-token\n"
                target.writestr(name, payload)
        replacement.replace(bundle)
        with (
            mock.patch.object(
                recovery,
                "_transfer_windows_recovery",
                return_value=windows_snapshot_receipt(),
            ),
            self.assertRaises(recovery.RecoveryError) as raised,
        ):
            self.real_capture_windows_recovery(
                self.context,
                backup_id="windows-tampered",
                staging=staging,
                commands=[],
            )
        self.assertEqual(raised.exception.code, "windows_recovery_bundle_invalid")

    def test_windows_snapshot_refuses_state_file_member_digest_drift(self) -> None:
        staging = self.root / "windows-digest-drift-staging"
        staging.mkdir()
        _expected, _state = windows_recovery_fixture(self.context, staging)
        bundle = staging / recovery.WINDOWS_BUNDLE_MEMBER
        replacement = staging / "replacement.zip"
        with zipfile.ZipFile(bundle) as source:
            state = json.loads(source.read(recovery.WINDOWS_STATE_MEMBER))
            for row in state["files"]:
                if row["role"] == "endpoint_token":
                    row["sha256"] = "0" * 64
            payloads = {name: source.read(name) for name in source.namelist()}
        payloads[recovery.WINDOWS_STATE_MEMBER] = json.dumps(
            state,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        with zipfile.ZipFile(replacement, "w") as target:
            for name, payload in payloads.items():
                target.writestr(name, payload)
        replacement.replace(bundle)
        with (
            mock.patch.object(
                recovery,
                "_transfer_windows_recovery",
                return_value=windows_snapshot_receipt(),
            ),
            self.assertRaises(recovery.RecoveryError) as raised,
        ):
            self.real_capture_windows_recovery(
                self.context,
                backup_id="windows-digest-drift",
                staging=staging,
                commands=[],
            )
        self.assertEqual(raised.exception.code, "windows_recovery_state_invalid")

    def test_windows_bundle_binds_install_id_contents_to_identity(self) -> None:
        staging = self.root / "windows-install-id-staging"
        staging.mkdir()
        windows_recovery_fixture(self.context, staging)
        bundle = staging / recovery.WINDOWS_BUNDLE_MEMBER
        replacement = staging / "replacement.zip"
        changed_install_id = b"win-" + b"2" * 32 + b"\n"
        changed_sha256 = hashlib.sha256(changed_install_id).hexdigest()
        with zipfile.ZipFile(bundle) as source:
            state = json.loads(source.read(recovery.WINDOWS_STATE_MEMBER))
            install_file = next(row for row in state["files"] if row["role"] == "install_id")
            install_member = next(row for row in state["members"] if row["member"] == install_file["member"])
            install_file["bytes"] = len(changed_install_id)
            install_file["sha256"] = changed_sha256
            install_member["bytes"] = len(changed_install_id)
            install_member["sha256"] = changed_sha256
            payloads = {name: source.read(name) for name in source.namelist()}
        payloads["install-id"] = changed_install_id
        payloads[recovery.WINDOWS_STATE_MEMBER] = json.dumps(
            state,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        with zipfile.ZipFile(replacement, "w") as target:
            for name, payload in payloads.items():
                target.writestr(name, payload)
        replacement.replace(bundle)
        state = recovery._read_windows_state(bundle)
        with self.assertRaises(recovery.RecoveryError) as raised:
            recovery.verify_windows_recovery_bundle_members(bundle, state)
        self.assertEqual(raised.exception.code, "windows_recovery_state_invalid")

    def test_windows_snapshot_refuses_files_member_aliases(self) -> None:
        staging = self.root / "windows-member-alias-staging"
        staging.mkdir()
        _expected, _state = windows_recovery_fixture(self.context, staging)
        bundle = staging / recovery.WINDOWS_BUNDLE_MEMBER
        replacement = staging / "replacement.zip"
        with zipfile.ZipFile(bundle) as source:
            state = json.loads(source.read(recovery.WINDOWS_STATE_MEMBER))
            token = next(row for row in state["files"] if row["role"] == "endpoint_token")
            state["files"].append(
                {
                    **token,
                    "role": "cad_ownership",
                }
            )
            payloads = {name: source.read(name) for name in source.namelist()}
        payloads[recovery.WINDOWS_STATE_MEMBER] = json.dumps(
            state,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        with zipfile.ZipFile(replacement, "w") as target:
            for name, payload in payloads.items():
                target.writestr(name, payload)
        replacement.replace(bundle)
        with (
            mock.patch.object(
                recovery,
                "_transfer_windows_recovery",
                return_value=windows_snapshot_receipt(),
            ),
            self.assertRaises(recovery.RecoveryError) as raised,
        ):
            self.real_capture_windows_recovery(
                self.context,
                backup_id="windows-member-alias",
                staging=staging,
                commands=[],
            )
        self.assertEqual(raised.exception.code, "windows_recovery_state_invalid")

    def test_windows_result_with_contradictory_exit_is_unknown(self) -> None:
        completed = subprocess.CompletedProcess(
            ["ssh", "windows-fixture"],
            0,
            json.dumps({"passed": False, "code": "windows_task_not_disabled"}) + "\n",
            "",
        )
        with (
            mock.patch.object(recovery, "_run_windows_powershell", return_value=completed),
            self.assertRaises(recovery.RecoveryError) as raised,
        ):
            recovery._run_windows_recovery_json(
                ssh_host="windows-fixture",
                script="fixture",
                timeout=5,
                commands=[],
                expected_verb="recovery-snapshot",
            )
        self.assertEqual(raised.exception.code, "windows_recovery_transport_unknown")

    def test_windows_failure_receipt_must_name_the_expected_verb(self) -> None:
        completed = subprocess.CompletedProcess(
            ["ssh", "windows-fixture"],
            1,
            json.dumps(
                {
                    "passed": False,
                    "code": "windows_task_not_disabled",
                    "verb": "health",
                }
            )
            + "\n",
            "",
        )
        with (
            mock.patch.object(recovery, "_run_windows_powershell", return_value=completed),
            self.assertRaises(recovery.RecoveryError) as raised,
        ):
            recovery._run_windows_recovery_json(
                ssh_host="windows-fixture",
                script="fixture",
                timeout=5,
                commands=[],
                expected_verb="recovery-snapshot",
            )
        self.assertEqual(raised.exception.code, "windows_recovery_transport_unknown")

    def test_windows_localized_stderr_bytes_do_not_break_transport_decoding(self) -> None:
        process = subprocess.CompletedProcess(
            ["ssh", "windows-fixture", "-EncodedCommand", "fixed-launcher"],
            2,
            b'{"passed":false,"code":"fixture","message":"refused"}\n',
            " localized PowerShell error".encode("gb18030"),
        )
        process.transport = {
            "encoding": "base64(gzip(utf-8))",
            "script_sha256": "a" * 64,
            "script_bytes": 7,
            "payload_sha256": "b" * 64,
            "payload_bytes": 11,
            "launcher_sha256": "c" * 64,
            "launcher_bytes": 12,
        }
        commands: list[dict] = []
        with mock.patch.object(recovery, "_transport_powershell", return_value=process):
            completed = recovery._run_windows_powershell(
                ssh_host="windows-fixture",
                script="fixture",
                timeout=5,
                commands=commands,
            )
        self.assertEqual(completed.returncode, 2)
        self.assertIn("localized PowerShell error", completed.stderr)
        self.assertEqual(commands[-1]["transport"]["encoding"], "base64(gzip(utf-8))")

    def test_windows_truncated_utf16_is_a_classified_transport_error(self) -> None:
        process = subprocess.CompletedProcess(
            ["ssh", "windows-fixture"],
            0,
            b"\xff\xfeA",
            b"",
        )
        process.transport = windows_transport.request_metadata("fixture")
        with (
            mock.patch.object(recovery, "_transport_powershell", return_value=process),
            self.assertRaises(recovery.RecoveryError) as raised,
        ):
            recovery._run_windows_powershell(
                ssh_host="windows-fixture",
                script="fixture",
                timeout=5,
                commands=[],
            )
        self.assertEqual(raised.exception.code, "windows_transport_decode_failed")
        self.assertEqual(raised.exception.detail["stream"], "stdout")
        self.assertEqual(
            raised.exception.detail["sha256"],
            hashlib.sha256(b"\xff\xfeA").hexdigest(),
        )

    def test_recovery_propagates_bytecode_suppression_to_children(self) -> None:
        self.assertEqual(os.environ.get("PYTHONDONTWRITEBYTECODE"), "1")
        source = (DEPLOY / "recovery.py").read_text(encoding="utf-8")
        self.assertIn('os.environ["PYTHONDONTWRITEBYTECODE"] = "1"', source)
        self.assertIn("sys.dont_write_bytecode = True", source)

    def test_windows_failure_receipt_with_expected_verb_is_definite(self) -> None:
        cleanup = {
            "attempted": True,
            "passed": False,
            "path": "C:/Users/test/AppData/Local/Temp/windows-recovery-fixture",
            "error": "access denied",
        }
        completed = subprocess.CompletedProcess(
            ["ssh", "windows-fixture"],
            1,
            json.dumps(
                {
                    "passed": False,
                    "code": "windows_task_not_disabled",
                    "verb": "recovery-snapshot",
                    "cleanup": cleanup,
                }
            )
            + "\n",
            "",
        )
        with (
            mock.patch.object(recovery, "_run_windows_powershell", return_value=completed),
            self.assertRaises(recovery.RecoveryError) as raised,
        ):
            recovery._run_windows_recovery_json(
                ssh_host="windows-fixture",
                script="fixture",
                timeout=5,
                commands=[],
                expected_verb="recovery-snapshot",
            )
        self.assertEqual(raised.exception.code, "windows_task_not_disabled")
        self.assertEqual(raised.exception.detail["windows_inner_cleanup"], cleanup)

    def test_windows_failure_receipt_preserves_malformed_detail_and_cleanup(self) -> None:
        cleanup = {
            "attempted": True,
            "passed": False,
            "path": "C:/Users/test/AppData/Local/Temp/windows-recovery-fixture",
            "error": "access denied",
        }
        for malformed in (42, "invalid", ["invalid"]):
            with self.subTest(detail=malformed):
                completed = subprocess.CompletedProcess(
                    ["ssh", "windows-fixture"],
                    1,
                    json.dumps(
                        {
                            "passed": False,
                            "code": "windows_task_not_disabled",
                            "verb": "recovery-snapshot",
                            "detail": malformed,
                            "cleanup": cleanup,
                        }
                    )
                    + "\n",
                    "",
                )
                with (
                    mock.patch.object(recovery, "_run_windows_powershell", return_value=completed),
                    self.assertRaises(recovery.RecoveryError) as raised,
                ):
                    recovery._run_windows_recovery_json(
                        ssh_host="windows-fixture",
                        script="fixture",
                        timeout=5,
                        commands=[],
                        expected_verb="recovery-snapshot",
                    )
                self.assertEqual(raised.exception.code, "windows_task_not_disabled")
                self.assertEqual(
                    raised.exception.detail["malformed_original_detail"],
                    malformed,
                )
                self.assertEqual(raised.exception.detail["windows_inner_cleanup"], cleanup)

    def test_windows_remote_dir_is_not_removed_before_creation_is_owned(self) -> None:
        script = write(self.root / "instance-lifecycle.ps1", "fixture\n")
        with (
            mock.patch.object(
                recovery,
                "_create_remote_dir",
                side_effect=recovery.RecoveryError(
                    "windows_recovery_transport_unknown",
                    "fixture creation failed",
                ),
            ),
            mock.patch.object(recovery, "_remove_remote_dir") as remove,
            self.assertRaises(recovery.RecoveryError),
        ):
            recovery._transfer_windows_recovery(
                ssh_host="windows-fixture",
                remote_dir="C:/description/state/recovery/fixture",
                script=script,
                binding_document={},
                operation="recovery-snapshot",
                commands=[],
                timeout=5,
            )
        remove.assert_not_called()

    def test_windows_remote_dir_cleanup_reuses_the_creation_ownership_token(self) -> None:
        script = write(self.root / "instance-lifecycle.ps1", "fixture\n")
        with (
            mock.patch.object(recovery, "_create_remote_dir") as create,
            mock.patch.object(
                recovery,
                "_windows_scp",
                side_effect=recovery.RecoveryError(
                    "windows_recovery_transport_unknown",
                    "fixture upload failed",
                ),
            ),
            mock.patch.object(recovery, "_remove_remote_dir") as remove,
            self.assertRaises(recovery.RecoveryError),
        ):
            recovery._transfer_windows_recovery(
                ssh_host="windows-fixture",
                remote_dir="C:/description/state/recovery/fixture",
                script=script,
                binding_document={},
                operation="recovery-snapshot",
                commands=[],
                timeout=5,
            )
        ownership_token = create.call_args.kwargs["ownership_token"]
        self.assertRegex(ownership_token, r"\A[0-9a-f]{64}\Z")
        self.assertEqual(remove.call_args.kwargs["ownership_token"], ownership_token)

    def test_windows_success_receipt_records_cleanup_residue_without_reclassifying(self) -> None:
        script = write(self.root / "instance-lifecycle.ps1", "fixture\n")
        action_receipt = {
            "passed": True,
            "verb": "recovery-snapshot",
            "task_state": "Disabled",
            "endpoint": "stopped",
            "cad_ownership": "free",
            "bundle_sha256": "a" * 64,
        }
        with (
            mock.patch.object(recovery, "_create_remote_dir"),
            mock.patch.object(recovery, "_windows_scp"),
            mock.patch.object(recovery, "_run_windows_recovery_json", return_value=dict(action_receipt)),
            mock.patch.object(
                recovery,
                "_remove_remote_dir",
                side_effect=recovery.RecoveryError(
                    "windows_recovery_transport_unknown",
                    "fixture cleanup failed",
                ),
            ),
        ):
            result = recovery._transfer_windows_recovery(
                ssh_host="windows-fixture",
                remote_dir="C:/description/state/recovery/fixture",
                script=script,
                binding_document={},
                operation="recovery-snapshot",
                commands=[],
                timeout=5,
            )
        self.assertTrue(result["passed"])
        self.assertEqual(result["verb"], "recovery-snapshot")
        self.assertFalse(result["remote_cleanup"]["passed"])
        self.assertEqual(
            result["remote_cleanup"]["ownership_marker"],
            "C:/description/state/recovery/fixture/.recovery-owner",
        )

    def test_windows_action_error_survives_cleanup_error(self) -> None:
        script = write(self.root / "instance-lifecycle.ps1", "fixture\n")
        with (
            mock.patch.object(recovery, "_create_remote_dir"),
            mock.patch.object(recovery, "_windows_scp"),
            mock.patch.object(
                recovery,
                "_run_windows_recovery_json",
                side_effect=recovery.RecoveryError(
                    "windows_task_not_disabled",
                    "fixture action failed",
                    {"action": "preserved"},
                ),
            ),
            mock.patch.object(
                recovery,
                "_remove_remote_dir",
                side_effect=recovery.RecoveryError(
                    "windows_recovery_transport_unknown",
                    "fixture cleanup failed",
                ),
            ),
            self.assertRaises(recovery.RecoveryError) as raised,
        ):
            recovery._transfer_windows_recovery(
                ssh_host="windows-fixture",
                remote_dir="C:/description/state/recovery/fixture",
                script=script,
                binding_document={},
                operation="recovery-snapshot",
                commands=[],
                timeout=5,
            )
        self.assertEqual(raised.exception.code, "windows_task_not_disabled")
        self.assertEqual(raised.exception.detail["action"], "preserved")
        self.assertFalse(raised.exception.detail["remote_cleanup"]["passed"])

    def test_backup_requires_airflow_venv_interpreter(self) -> None:
        with self.assertRaises(recovery.RecoveryError) as raised:
            recovery._require_airflow_interpreter(self.context)
        self.assertEqual(
            raised.exception.code,
            "recovery_airflow_interpreter_required",
        )

    def test_quiesce_runs_drain_then_stop(self) -> None:
        operatorctl = write(
            self.root / "operatorctl.sh",
            textwrap.dedent(
                """\
                #!/usr/bin/env bash
                set -euo pipefail
                printf '%s|%s|%s|%s\\n' "$*" \
                  "${OPERATORCTL_MAINTENANCE_LOCK_OWNER:-}" \
                  "${OPERATORCTL_MAINTENANCE_LOCK_FD:-}" \
                  "${OPERATORCTL_MAINTENANCE_LOCK_PATH:-}" >> "$QUIESCE_LOG"
                """
            ),
            mode=0o700,
        )
        operatorctl.chmod(0o700)
        log = self.root / "quiesce.log"
        self.context.resolved["windows"].update(
            {
                "ssh_host": "windows-fixture",
                "instance": {"task_name": "description-e2e-endpoint"},
            }
        )
        probe = {
            "admission": {
                "state": "closed",
                "reason": "fixture",
                "actor": "fixture",
                "revision": 1,
            },
            "settled": True,
            "unsettled_count": 0,
            "active_application_sessions": [],
            "wal_lsn": "0/100",
        }
        evidence = {
            "argv": ["fixture"],
            "returncode": 0,
            "stdout_sha256": hashlib.sha256(b"").hexdigest(),
            "stderr_sha256": hashlib.sha256(b"").hexdigest(),
        }
        lock = recovery.acquire_maintenance_lock(Path(self.context.resolved["storage"]["state_root"]))
        try:
            with (
                mock.patch.dict(os.environ, {"QUIESCE_LOG": str(log)}),
                mock.patch.object(
                    recovery,
                    "_require_airflow_interpreter",
                    return_value=Path("/fixture/python"),
                ),
                mock.patch.object(
                    recovery,
                    "_wait_database_quiescence",
                    return_value=probe,
                ),
                mock.patch.object(
                    recovery,
                    "_linux_unit_states",
                    return_value=[{"unit": unit, "active_state": "inactive"} for unit in recovery.MAINTENANCE_UNITS],
                ),
                mock.patch.object(
                    recovery,
                    "_disable_windows_task",
                    return_value=("Disabled", [evidence]),
                ),
            ):
                state = self.real_quiesce_maintenance(
                    self.context,
                    {
                        "authority": {
                            "system_identifier": "1",
                            "catalog_version": "202107181",
                            "database": "airflow_meta",
                            "database_oid": "16384",
                            "socket_dir": "/fixture/socket",
                            "port": 55432,
                        }
                    },
                    operatorctl_path=operatorctl,
                    timeout=30,
                    maintenance_lock=lock,
                )
            commands = log.read_text().splitlines()
            self.assertEqual(len(commands), 2)
            self.assertTrue(commands[0].startswith("drain --config "))
            inherited = f"|{lock.owner_id}|{lock.descriptor}|{lock.path}"
            self.assertTrue(commands[0].endswith(inherited))
            self.assertNotIn("--keep-database", commands[0])
            self.assertTrue(commands[1].startswith("stop --config "))
            self.assertTrue(commands[1].endswith(" --keep-database" + inherited))
            self.assertEqual(state["windows"]["state"], "Disabled")
            self.assertEqual(state["database_before"], probe)
        finally:
            lock.close()

    def test_key_inside_source_failure_domain_is_refused(self) -> None:
        key = write(
            Path(self.context.resolved["storage"]["state_root"]) / "secrets" / "inside.key",
            os.urandom(32).hex() + "\n",
        )
        key.chmod(0o600)
        with (
            mock.patch.object(
                recovery,
                "load_backup_context",
                return_value=self.context,
            ),
            self.assertRaises(recovery.RecoveryError) as raised,
        ):
            recovery.backup_command(
                config_path=self.context.config_path,
                backup_id="inside-key",
                key_file=key,
                out=self.root / "recovery-inside-key",
            )
        self.assertEqual(raised.exception.code, "recovery_key_unavailable")

    def test_missing_required_state_file_fails_backup(self) -> None:
        Path(self.context.resolved["storage"]["state_root"], "portal.json").unlink()
        with (
            mock.patch.object(
                recovery,
                "load_backup_context",
                return_value=self.context,
            ),
            self.assertRaises(recovery.RecoveryError) as raised,
        ):
            recovery.backup_command(
                config_path=self.context.config_path,
                backup_id="missing-state",
                key_file=self.key,
                out=self.root / "recovery-missing-state",
            )
        self.assertEqual(raised.exception.code, "recovery_release_missing")

    def test_archive_corruption_is_refused_before_target_mutation(self) -> None:
        out, manifest = self.backup()
        self.isolate_source()
        archive = out / "recovery.tar.enc"
        damaged = bytearray(archive.read_bytes())
        damaged[len(damaged) // 2] ^= 1
        archive.write_bytes(damaged)
        manifest_path = out / "recovery-manifest.json"
        manifest_sha = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        roots = {row["name"]: row["path"] for row in manifest["roots"] if "reconstructible_runtime" not in row}
        plan = self.target_plan(manifest_sha, roots)
        with self.assertRaises(recovery.RecoveryError) as raised:
            recovery.restore_command(
                manifest_path=manifest_path,
                key=self.key,
                target_plan_path=plan,
                status_dir=self.root / "status-archive",
            )
        self.assertEqual(raised.exception.code, "recovery_archive_hash_mismatch")

    def test_member_digest_tamper_is_refused(self) -> None:
        out, manifest = self.backup()
        self.isolate_source()
        member = next(row for row in manifest["members"] if row["member"] == "artifacts/artifact.bin")
        member["sha256"] = "0" * 64
        manifest_path = out / "recovery-manifest.json"
        manifest_bytes = recovery.canonical_json(manifest)
        manifest_path.write_bytes(manifest_bytes)
        manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()
        write(
            out / "recovery-manifest.sha256",
            f"{manifest_sha}  recovery-manifest.json\n",
        )
        roots = {row["name"]: row["path"] for row in manifest["roots"] if "reconstructible_runtime" not in row}
        plan = self.target_plan(manifest_sha, roots)
        with self.assertRaises(recovery.RecoveryError) as raised:
            recovery.restore_command(
                manifest_path=manifest_path,
                key=self.key,
                target_plan_path=plan,
                status_dir=self.root / "status-member",
            )
        self.assertEqual(raised.exception.code, "recovery_staged_source_mismatch")

    def test_relocated_mode_is_refused(self) -> None:
        out, manifest = self.backup()
        self.isolate_source()
        manifest_path = out / "recovery-manifest.json"
        manifest_sha = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        roots = {row["name"]: row["path"] for row in manifest["roots"] if "reconstructible_runtime" not in row}
        plan = self.target_plan(manifest_sha, roots)
        document = json.loads(plan.read_text())
        document["mode"] = "relocated"
        plan.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaises(recovery.RecoveryError) as raised:
            recovery.restore_command(
                manifest_path=manifest_path,
                key=self.key,
                target_plan_path=plan,
                status_dir=self.root / "status-relocated",
            )
        self.assertEqual(raised.exception.code, "recovery_target_plan_invalid")

    def test_target_equal_to_source_authority_is_refused(self) -> None:
        out, manifest = self.backup()
        self.isolate_source()
        manifest_path = out / "recovery-manifest.json"
        manifest_sha = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        roots = {row["name"]: row["path"] for row in manifest["roots"] if "reconstructible_runtime" not in row}
        plan = self.target_plan(manifest_sha, roots)
        with (
            mock.patch.object(
                recovery,
                "pg_control",
                return_value={
                    "system_identifier": manifest["database"]["authority"]["system_identifier"],
                    "catalog_version": "202107181",
                },
            ),
            self.assertRaises(recovery.RecoveryError) as raised,
        ):
            recovery.restore_command(
                manifest_path=manifest_path,
                key=self.key,
                target_plan_path=plan,
                status_dir=self.root / "status-same",
            )
        self.assertEqual(raised.exception.code, "recovery_target_same_authority")

    def test_postgres_15_target_is_refused(self) -> None:
        out, manifest = self.backup()
        self.isolate_source()
        manifest_path = out / "recovery-manifest.json"
        manifest_sha = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        roots = {row["name"]: row["path"] for row in manifest["roots"] if "reconstructible_runtime" not in row}
        plan_path = self.target_plan(manifest_sha, roots)
        plan = json.loads(plan_path.read_text())
        target_bin = Path(plan["postgres"]["bin_dir"])
        write(
            target_bin / "psql",
            PSQL.replace("PostgreSQL) 14.24", "PostgreSQL) 15.8"),
            mode=0o755,
        )
        (target_bin / "psql").chmod(0o755)
        with self.assertRaises(recovery.RecoveryError) as raised:
            recovery.restore_command(
                manifest_path=manifest_path,
                key=self.key,
                target_plan_path=plan_path,
                status_dir=self.root / "status-pg15",
            )
        self.assertEqual(raised.exception.code, "recovery_postgres_version_mismatch")

    def test_multiple_database_authorities_are_refused(self) -> None:
        fake = recovery.CommandContext(
            bin_dir=self.context.postgres_root / "root",
            library_dir=None,
            commands=[],
        )

        probes = iter(
            [
                {
                    "database": "airflow_meta",
                    "database_oid": "1",
                    "role": "fixture",
                    "server_version_num": "140024",
                },
                {
                    "database": "airflow_meta",
                    "database_oid": "2",
                    "role": "fixture",
                    "server_version_num": "140024",
                },
            ]
        )

        def probe(_context, dsn):
            del dsn
            try:
                return next(probes)
            except StopIteration:
                return {
                    "database": "airflow_meta",
                    "database_oid": "2",
                    "role": "fixture",
                    "server_version_num": "140024",
                }

        with (
            mock.patch.object(recovery, "database_probe", side_effect=probe),
            self.assertRaises(recovery.RecoveryError) as raised,
        ):
            recovery.capture_database_authority(
                self.context.resolved,
                self.context.derived_env,
                postgres_root=self.context.postgres_root,
                context=fake,
            )
        self.assertEqual(raised.exception.code, "recovery_dsn_authority_mismatch")


class WindowsScpEvidenceTests(unittest.TestCase):
    def test_streaming_capture_drains_both_flooded_pipes_with_bounded_retention(self):
        script = "import os; [(os.write(1,b'a'*65536),os.write(2,b'b'*65536)) for _ in range(64)]"
        result = recovery._capture_transfer_process([sys.executable, "-c", script], timeout=20, sensitive_values=())
        self.assertEqual(result.returncode, 0)
        self.assertEqual(len(result.stdout), 65536)
        self.assertEqual(len(result.stderr), 65536)
        for name, value in (("stdout", b"a"), ("stderr", b"b")):
            observation = result.stream_observations[name]
            self.assertEqual(observation["bytes"], 64 * 65536)
            self.assertEqual(observation["sha256"], hashlib.sha256(value * (64 * 65536)).hexdigest())
            self.assertTrue(observation["truncated"])
            self.assertTrue(observation["complete"])

    def test_streaming_redaction_crosses_chunks_and_timeout_keeps_partial(self):
        secret = b"private-sentinel"
        script = (
            "import os,time; os.write(1,b'private-'); time.sleep(.1); os.write(1,b'sentinel'); "
            "os.write(2,b'\\xfftimeout'); time.sleep(10)"
        )
        result = recovery._capture_transfer_process(
            [sys.executable, "-c", script], timeout=1, sensitive_values=(secret,)
        )
        self.assertTrue(result.interrupted)
        self.assertNotIn(secret, result.stdout)
        self.assertEqual(result.stdout, b"<redacted>")
        self.assertEqual(result.stderr, b"\xfftimeout")
        self.assertEqual(result.stream_observations["stdout"]["sha256"], hashlib.sha256(secret).hexdigest())
        self.assertTrue(result.stream_observations["stdout"]["redacted"])

    def test_failure_cleanup_keeps_referenced_sidecars_and_partial_download(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for partial in (False, True):
                out = root / str(partial)
                claim = recovery.claim_backup_output(out, "fixture")
                staging = out / "backup"
                staging.mkdir()
                piece = write(staging / "partial.zip", "partial")
                sidecar = write(out / "windows-transfer-evidence" / "upload.stderr", "diagnostic")
                detail = {"phase": "download" if partial else "upload", "stderr": {"path": str(sidecar)}}
                if partial:
                    detail["download"] = {"present": True, "partial": {"path": str(piece)}}
                recovery.record_backup_failure(
                    argparse.Namespace(out=out, backup_id="fixture", config=root / "platform.json"),
                    recovery.RecoveryError("windows_recovery_transport_unknown", "transfer failed", detail),
                    claim,
                )
                receipt = json.loads((out / "receipt.json").read_text())
                self.assertTrue(Path(receipt["detail"]["stderr"]["path"]).is_file())
                self.assertEqual(staging.exists(), partial)

    def test_strict_umask_still_creates_private_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = write(root / "input", "payload")
            previous = os.umask(0o777)
            try:
                with (
                    mock.patch.object(
                        recovery,
                        "_capture_transfer_process",
                        return_value=subprocess.CompletedProcess([], 1, b"", b"error"),
                    ),
                    self.assertRaises(recovery.RecoveryError),
                ):
                    recovery._windows_scp(
                        ssh_host="windows-fixture",
                        sources=[source],
                        remote_dir="C:/private/probe",
                        upload=True,
                        timeout=5,
                        commands=[],
                        code="windows_recovery_transport_unknown",
                        evidence_dir=root / "evidence",
                    )
                self.assertEqual(stat.S_IMODE((root / "evidence").stat().st_mode), 0o700)
                self.assertEqual(stat.S_IMODE((root / "evidence/upload.stderr").stat().st_mode), 0o600)
            finally:
                os.umask(previous)

    def test_raw_bytes_secret_redaction_and_evidence_write_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = write(root / "input.txt", "payload")
            secret = b"private-sentinel"
            raw = b"\xff\xfe error " + secret
            for blocked in (False, True):
                evidence = root / str(blocked)
                evidence.mkdir(mode=0o700)
                if blocked:
                    (evidence / "upload.stderr").write_bytes(b"original")
                commands = []
                with (
                    mock.patch.object(
                        recovery, "_capture_transfer_process", return_value=subprocess.CompletedProcess([], 1, b"", raw)
                    ),
                    self.assertRaises(recovery.RecoveryError) as raised,
                ):
                    recovery._windows_scp(
                        ssh_host="windows-fixture",
                        sources=[source],
                        remote_dir="C:/private/probe",
                        upload=True,
                        timeout=5,
                        commands=commands,
                        code="windows_recovery_transport_unknown",
                        evidence_dir=evidence,
                        sensitive_values=(secret,),
                    )
                detail = raised.exception.detail
                self.assertEqual(detail["returncode"], 1)
                self.assertEqual(detail["stderr"]["sha256"], hashlib.sha256(raw).hexdigest())
                self.assertTrue(detail["stderr"]["redacted"])
                self.assertNotIn(secret.decode(), json.dumps(detail))
                self.assertNotIn(str(source), commands[0]["argv"])
                if blocked:
                    self.assertTrue(detail["evidence_write_errors"])
                    self.assertEqual((evidence / "upload.stderr").read_bytes(), b"original")
                else:
                    self.assertEqual((evidence / "upload.stderr").read_bytes(), raw.replace(secret, b"<redacted>"))

    def test_download_partial_records_expected_and_actual_separately(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "partial.zip"
            expected = {
                "name": "recovery.zip",
                "path": "C:/private/probe/recovery.zip",
                "bytes": 100,
                "sha256": "a" * 64,
            }

            def fail_transfer(*args, **kwargs):
                target.write_bytes(b"partial")
                return subprocess.CompletedProcess([], 1, b"", b"failed")

            with (
                mock.patch.object(recovery, "_capture_transfer_process", side_effect=fail_transfer),
                self.assertRaises(recovery.RecoveryError) as raised,
            ):
                recovery._windows_scp(
                    ssh_host="windows-fixture",
                    sources=[target],
                    remote_dir="C:/private/probe",
                    upload=False,
                    remote_name="recovery.zip",
                    timeout=5,
                    commands=[],
                    code="windows_recovery_transport_unknown",
                    evidence_dir=root / "evidence",
                    expected_download=expected,
                )
            self.assertEqual(raised.exception.detail["download"]["expected"], expected)
            self.assertEqual(
                raised.exception.detail["download"]["observed"]["sha256"], hashlib.sha256(b"partial").hexdigest()
            )

    def test_tar_reader_drains_after_archive_end_and_rejects_decrypt_failure(self):
        payload = io.BytesIO()
        with tarfile.open(fileobj=payload, mode="w") as archive:
            member = tarfile.TarInfo("member")
            member.size = 1
            archive.addfile(member, io.BytesIO(b"x"))

        class TailStream(io.BytesIO):
            exhausted = False

            def read(self, size=-1):
                value = super().read(size)
                if not value:
                    self.exhausted = True
                return value

        class Process:
            def __init__(self, code):
                self.stdout = TailStream(payload.getvalue() + b"\0" * 65536)
                self.stderr = io.BytesIO(b"" if code == 0 else b"bad decrypt")
                self.code = code
                self.finished = False

            def wait(self):
                self.finished = True
                return self.code if self.stdout.exhausted else 1

            def poll(self):
                return self.code if self.finished else None

            def kill(self):
                self.finished = True

        for code in (0, 1, 2):
            if code == 2:
                payload = io.BytesIO()
                with tarfile.open(fileobj=payload, mode="w") as archive:
                    member = tarfile.TarInfo("unsupported-link")
                    member.type = tarfile.SYMTYPE
                    member.linkname = "target"
                    archive.addfile(member)
            process = Process(0 if code == 2 else code)
            with self.subTest(code=code), mock.patch.object(recovery.subprocess, "Popen", return_value=process):
                if code:
                    with self.assertRaises(recovery.RecoveryError) as raised:
                        recovery.read_sealed_tar_member_rows(Path("archive"), Path("key"))
                    if code == 2:
                        self.assertIn("archive_stream", raised.exception.detail)
                else:
                    rows = recovery.read_sealed_tar_member_rows(Path("archive"), Path("key"))
                    self.assertEqual(rows[0]["member"], "member")
                self.assertTrue(process.stdout.exhausted)

    def test_credential_binding_is_rejected_before_any_transfer(self):
        with tempfile.TemporaryDirectory() as temporary:
            script = write(Path(temporary) / "helper.ps1", "fixture")
            for binding in ({"token": "secret"}, {"nested": [{"private-key": "secret"}]}):
                with self.subTest(binding=binding), mock.patch.object(recovery, "_create_remote_dir") as create:
                    with self.assertRaises(recovery.RecoveryError) as raised:
                        recovery._transfer_windows_recovery(
                            ssh_host="windows-fixture",
                            remote_dir="C:/private/probe",
                            script=script,
                            binding_document=binding,
                            operation="recovery-snapshot",
                            commands=[],
                            timeout=5,
                        )
                    self.assertEqual(raised.exception.code, "windows_recovery_binding_invalid")
                    create.assert_not_called()

    def test_timeout_retains_partial_streams(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = write(root / "input.txt", "payload")
            evidence = root / "evidence"
            with (
                mock.patch.object(
                    recovery,
                    "_capture_transfer_process",
                    side_effect=subprocess.TimeoutExpired(["scp"], 5, output=b"partial", stderr=b"timeout"),
                ),
                self.assertRaises(recovery.RecoveryError) as raised,
            ):
                recovery._windows_scp(
                    ssh_host="windows-fixture",
                    sources=[source],
                    remote_dir="C:/private/probe",
                    upload=True,
                    timeout=5,
                    commands=[],
                    code="windows_recovery_transport_unknown",
                    evidence_dir=evidence,
                )
            self.assertTrue(raised.exception.detail["interrupted"])
            self.assertEqual((evidence / "upload.stdout").read_bytes(), b"partial")
            self.assertEqual((evidence / "upload.stderr").read_bytes(), b"timeout")

    def test_failed_upload_retains_helper_and_binding_outside_temporary_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script = write(root / "instance-lifecycle.ps1", "fixture helper")
            evidence = root / "evidence"
            with (
                mock.patch.object(recovery, "_create_remote_dir"),
                mock.patch.object(recovery, "_remove_remote_dir"),
                mock.patch.object(
                    recovery, "_capture_transfer_process", return_value=subprocess.CompletedProcess([], 1, "", "failed")
                ),
                self.assertRaises(recovery.RecoveryError) as raised,
            ):
                recovery._transfer_windows_recovery(
                    ssh_host="windows-fixture",
                    remote_dir="C:/private/probe",
                    script=script,
                    binding_document={"token_sha256": "a" * 64},
                    operation="recovery-snapshot",
                    commands=[],
                    timeout=5,
                    evidence_dir=evidence,
                )
            self.assertEqual(raised.exception.detail["phase"], "upload")
            self.assertEqual((evidence / script.name).read_bytes(), script.read_bytes())
            self.assertEqual(json.loads((evidence / "windows-binding.json").read_text())["token_sha256"], "a" * 64)
            self.assertNotIn('"token"', (evidence / "windows-binding.json").read_text())
            self.assertFalse((evidence / "recovery.zip").exists())

    def test_sftp_argv_preserves_spaces_without_literal_quotes(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = write(Path(temporary) / "input file.txt", "payload")
            for upload in (True, False):
                with (
                    self.subTest(upload=upload),
                    mock.patch.object(
                        recovery, "_capture_transfer_process", return_value=subprocess.CompletedProcess([], 0, "", "")
                    ) as run,
                ):
                    recovery._windows_scp(
                        ssh_host="windows-fixture",
                        sources=[source],
                        remote_dir="C:/private space/probe",
                        upload=upload,
                        remote_name=None if upload else "output file.txt",
                        timeout=5,
                        commands=[],
                        code="windows_recovery_transport_unknown",
                    )
                    argv = run.call_args.args[0]
                    remote = next(value for value in argv if value.startswith("windows-fixture:"))
                    self.assertNotIn('"', remote)
                    self.assertNotIn("-O", argv)
                    self.assertIn("private space", remote)

    def test_upload_and_download_failures_retain_bounded_private_raw_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = write(root / "input.txt", "payload")
            for upload in (True, False):
                phase = "upload" if upload else "download"
                evidence = root / phase
                stderr = "failure " + "x" * 70000
                with (
                    self.subTest(upload=upload),
                    mock.patch.object(
                        recovery,
                        "_capture_transfer_process",
                        return_value=subprocess.CompletedProcess([], 1, "raw output", stderr),
                    ),
                    self.assertRaises(recovery.RecoveryError) as raised,
                ):
                    recovery._windows_scp(
                        ssh_host="windows-fixture",
                        sources=[source],
                        remote_dir="C:/private/probe",
                        upload=upload,
                        remote_name=None if upload else "recovery.zip",
                        timeout=5,
                        commands=[],
                        code="windows_recovery_transport_unknown",
                        evidence_dir=evidence,
                    )
                detail = raised.exception.detail
                self.assertEqual(detail["phase"], phase)
                self.assertEqual(detail["returncode"], 1)
                self.assertEqual((evidence / f"{phase}.stderr").read_bytes(), stderr.encode()[:65536])
                self.assertEqual((evidence / f"{phase}.stdout").read_bytes(), b"raw output")
                self.assertTrue(detail["stderr"]["truncated"])
                self.assertEqual(detail["stderr"]["sha256"], hashlib.sha256(stderr.encode()).hexdigest())
                self.assertEqual(stat.S_IMODE(evidence.stat().st_mode), 0o700)
                self.assertEqual(stat.S_IMODE((evidence / f"{phase}.stderr").stat().st_mode), 0o600)
                self.assertEqual(
                    json.loads((evidence / f"{phase}.json").read_text()),
                    {k: v for k, v in detail.items() if k != "evidence_record"},
                )

    def test_invalid_remote_paths_are_rejected_before_transport(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = write(Path(temporary) / "input", "payload")
            for path in ("C:/private/../escape", 'C:/private/"quoted"', "C:/private/\nline"):
                with self.subTest(path=path), mock.patch.object(recovery, "_capture_transfer_process") as run:
                    with self.assertRaises(recovery.RecoveryError):
                        recovery._windows_scp(
                            ssh_host="windows-fixture",
                            sources=[source],
                            remote_dir=path,
                            upload=True,
                            timeout=5,
                            commands=[],
                            code="windows_recovery_transport_unknown",
                        )
                    run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
