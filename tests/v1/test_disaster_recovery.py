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
from description_pipeline.pipeline.contracts import NativeTargetIdentity
from description_pipeline.schemas import (
    INSTALL_RECORD_SCHEMA,
    STAGE_RESULT_SCHEMA,
)

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
                "description-airflow-triggerer",
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


def canonical_checkpoint_reference(
    artifact_root: Path,
    *,
    run_id: str = "producer",
    stage: str = "capture",
    parent_run_id: str | None = None,
    outcome: str = "completed",
) -> dict:
    from description_pipeline.artifacts.store import ArtifactStore
    from description_pipeline.io import digest
    from description_pipeline.pipeline.contracts import StageResult

    tool = digest({"tool": stage})
    store = ArtifactStore(artifact_root)
    if outcome == "completed":
        with tempfile.TemporaryDirectory(prefix="output-artifact-") as directory:
            source = Path(directory)
            (source / "delivery.json").write_text(
                json.dumps({"run_id": run_id, "stage": stage}) + "\n",
                encoding="utf-8",
            )
            output_artifact = store.seal_tree(
                source,
                kind=f"{stage}_delivery",
                producer_run_id=run_id,
                producer_stage=stage,
                producer_tool_sha256=tool,
            )
        result_digest = digest({"request": run_id, "stage": stage})
        result = StageResult(
            schema_version=STAGE_RESULT_SCHEMA,
            command_id=f"command-{run_id}-{stage}",
            run_id=run_id,
            stage=stage,
            request_digest=result_digest,
            outcome="completed",
            output_artifact=output_artifact,
            actual_executor_sha256=tool,
            checks_sha256=digest({"checks": run_id, "stage": stage}),
            resources_settled=True,
        )
        result_artifact = store.seal_stage_result(result)
        result_record = result_artifact.to_record()
        output = output_artifact.to_record()
        result_digest = result.digest
        failure_digest = None
    else:
        result_record = None
        output = None
        result_digest = None
        failure_digest = digest({"failure": run_id, "stage": stage})
    return {
        "run_id": run_id,
        "stage": stage,
        "outcome": outcome,
        "request_id": f"request-{run_id}",
        "parent_run_id": parent_run_id,
        "decision_id": f"decision-{run_id}-{stage}",
        "command_id": f"command-{run_id}-{stage}",
        "result_digest": result_digest,
        "actual_executor_sha256": tool,
        "original_producer_decision_id": (f"decision-{parent_run_id}-{stage}" if parent_run_id is not None else None),
        "failure_digest": failure_digest,
        "result_record_artifact": result_record,
        "output_artifact": output,
    }


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
        "dag": {"id": "solidworks_to_urdf", "path": str(state / "dag.py"), "digest": "d" * 64, "tasks": 7},
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
                "target_identity": NativeTargetIdentity.create(
                    install_id="win-" + "a" * 32,
                    pipeline_id="solidworks-to-urdf",
                ).to_record(),
            },
        },
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


def windows_recovery_fixture(context, staging: Path) -> tuple[dict, dict]:
    identity = context.install_record["ends"]["windows"]
    windows = staging / "windows"
    windows.mkdir(parents=True, exist_ok=True, mode=0o700)
    archive_name = "description-1.0.0-windows-cp312-x86_64.zip"
    archive_bytes = b"windows runtime"
    install_id = identity["install_id"]
    release = identity["release"]
    binding = {
        "endpoint_config": "C:/description/config/endpoint.json",
        "release_file": "C:/description/release/release.json",
        "task_name": "description-e2e-endpoint",
        "task_path": "\\URDF\\",
        "service_account": "DESCRIPTION\\operator",
        "python_executable": "C:/Python312/python.exe",
    }
    endpoint = {
        "token_file": "C:/description/config/endpoint.token",
        "package_root": "C:/description/packages",
        "output_root": "C:/description/results",
    }
    payloads = {
        "windows-binding.json": recovery.canonical_json(binding),
        "endpoint.json": recovery.canonical_json(endpoint),
        "endpoint.token": b"t" * 64 + b"\n",
        "install-id": install_id.encode("ascii") + b"\n",
        "install-receipt.json": recovery.canonical_json({
            "schema_version": "solidworks-to-urdf.windows-install-receipt/v2",
            "passed": True,
            "install_id": install_id,
            "config_digest": identity["config_digest"],
            **{key: release[key] for key in ("version", "source_commit", "source_sha256")},
        }),
        "release.json": recovery.canonical_json(
            {**release, "artifacts": {archive_name: hashlib.sha256(archive_bytes).hexdigest()}}
        ),
        "instance-lifecycle.ps1": b"# lifecycle\n",
        "bootstrap-managed-root.ps1": b"# bootstrap\n",
        "acl.json": recovery.canonical_json(
            {
                "passed": True,
                "entries": [
                    {
                        "path": "C:/description/state",
                        "type": "directory",
                        "owner_sid": "S-1-5-32-544",
                        "group_sid": "S-1-5-18",
                        "sddl": "O:S-1-5-32-544G:S-1-5-18",
                    }
                ],
            }
        ),
    }
    for name, payload in payloads.items():
        write(windows / name, payload)
    output = windows / "results"
    output.mkdir(mode=0o700)

    def descriptor(name: str, source_path: str) -> dict:
        path = windows / name
        return {
            "source_path": source_path,
            "member": path.relative_to(staging).as_posix(),
            "bytes": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }

    status = {
        "schema_version": "solidworks-to-urdf.windows-lifecycle-result/v1",
        "passed": True,
        "verb": "status",
        "task": {
            "task_name": binding["task_name"],
            "task_path": binding["task_path"],
            "state": "Disabled",
            "semantic_sha256": "c" * 64,
            "security_descriptor_sha256": "e" * 64,
        },
        "identity": {
            "install_id": install_id,
            "config_digest": identity["config_digest"],
            "release": release,
        },
        "listener": None,
        "health": None,
        "status": {
            "task_exists": True,
            "task_state": "Disabled",
            "error": None,
            "consistent": True,
            "solidworks_processes": 0,
        },
    }
    state = {
        "schema_version": recovery.WINDOWS_RECOVERY_SCHEMA,
        "ssh_host": "windows-fixture",
        "state_root": "C:/description/state",
        "output_root": endpoint["output_root"],
        "package_root": endpoint["package_root"],
        "binding": descriptor("windows-binding.json", "C:/description/state/windows-binding.json"),
        "endpoint_config": descriptor("endpoint.json", binding["endpoint_config"]),
        "token": descriptor("endpoint.token", endpoint["token_file"]),
        "release": descriptor("release.json", binding["release_file"]),
        "install_id": descriptor("install-id", "C:/description/state/install-id"),
        "install_receipt": descriptor("install-receipt.json", "C:/description/state/install-receipt.json"),
        "lifecycle": descriptor("instance-lifecycle.ps1", "C:/description/state/instance-lifecycle.ps1"),
        "bootstrap": descriptor("bootstrap-managed-root.ps1", "C:/description/state/bootstrap-managed-root.ps1"),
        "acl": descriptor("acl.json", "windows/acl.json"),
        "native_operations": {
            "source_path": "C:/description/state/native-operations",
            "member": "windows/native-operations",
            "exists": False,
        },
        "output": {
            "source_path": endpoint["output_root"],
            "member": output.relative_to(staging).as_posix(),
            "exists": True,
        },
        "status": status,
        "archive_name": archive_name,
        "archive_sha256": hashlib.sha256(archive_bytes).hexdigest(),
    }
    return state, state


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
    state = manifest["windows"]
    document = {
        "status": state["status"],
        "installation": {
            "passed": True,
            "verb": "install",
            "identity": state["status"]["identity"],
        },
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
        for name in (
            "admission",
            "run_admission",
            "artifact",
            "command",
            "command_input_artifact",
            "execution_claim",
            "stage_decision",
            "stage_candidate",
            "effect_intent",
            "effect_fact",
            "run_display_metadata",
        )
    ]
    + [
        {
            "schema": "platform",
            "name": name,
            "kind": "v",
            "owner": "platform_migrate",
            "acl": [],
        }
        for name in ("lifecycle_projection", "admission_projection")
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
                ("platform", "run_admission"),
                ("platform", "artifact"),
                ("platform", "command"),
                ("platform", "command_input_artifact"),
                ("platform", "execution_claim"),
                ("platform", "stage_decision"),
                ("platform", "stage_candidate"),
                ("platform", "effect_intent"),
                ("platform", "effect_fact"),
                ("platform", "run_display_metadata"),
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
    elif "WITH referenced_ids AS" in query:
        fixture = next((parent / "checkpoint-root-fixture.json" for parent in pathlib.Path(socket_dir).parents
                        if (parent / "checkpoint-root-fixture.json").is_file()), None)
        reference_fixture = next((parent / "checkpoint-fixture.json" for parent in pathlib.Path(socket_dir).parents
                                  if (parent / "checkpoint-fixture.json").is_file()), None)
        references = json.loads(reference_fixture.read_text()) if reference_fixture is not None else []
        payload = json.loads(fixture.read_text()) if fixture is not None else {
            "decisions": references,
            "artifacts": [
                artifact
                for artifact_id, artifact in sorted({
                    artifact["artifact_id"]: artifact
                    for reference in references
                    for role in ("result_record_artifact", "output_artifact")
                    for artifact in (reference[role],)
                    if artifact is not None
                }.items())
            ],
            "missing_artifact_ids": [],
            "candidates": [],
            "retained_locations": [],
            "workspace_commands": [],
        }
    elif "FROM platform.stage_decision d" in query:
        fixture = next((parent / "checkpoint-fixture.json" for parent in pathlib.Path(socket_dir).parents
                        if (parent / "checkpoint-fixture.json").is_file()), None)
        payload = json.loads(fixture.read_text()) if fixture is not None else []
    elif "'catalog'" not in query and "'database', current_database()" in query:
        payload = {
            "database": "airflow_meta",
            "database_oid": "16384",
            "role": option("-U"),
            "server_version_num": "140024",
            **identity,
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
root_fixture = pathlib.Path(args[args.index("--request") + 1]).parents[2] / "checkpoint-root-fixture.json"
recovery_root = json.loads(root_fixture.read_text()) if root_fixture.is_file() else {
    "decisions": references,
    "artifacts": [],
    "missing_artifact_ids": [],
    "candidates": [],
    "retained_locations": [],
    "workspace_commands": [],
}
if not recovery_root["artifacts"]:
    by_id = {}
    for reference in references:
        for role in ("result_record_artifact", "output_artifact"):
            artifact = reference[role]
            if artifact is not None:
                by_id[artifact["artifact_id"]] = artifact
    recovery_root["artifacts"] = [by_id[key] for key in sorted(by_id)]
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
        for name in (
            "admission",
            "run_admission",
            "artifact",
            "command",
            "command_input_artifact",
            "execution_claim",
            "stage_decision",
            "stage_candidate",
            "effect_intent",
            "effect_fact",
            "run_display_metadata",
        )
    ]
    + [
        {
            "schema": "platform",
            "name": name,
            "kind": "v",
            "owner": "platform_migrate",
            "acl": [],
        }
        for name in ("lifecycle_projection", "admission_projection")
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
    "platform.run_admission",
    "platform.artifact",
    "platform.command",
    "platform.command_input_artifact",
    "platform.execution_claim",
    "platform.stage_decision",
    "platform.stage_candidate",
    "platform.effect_intent",
    "platform.effect_fact",
    "platform.run_display_metadata",
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
    "unsettled": [],
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
         "recovery_root": recovery_root,
         "recovery_root_sha256": recovery._digest_document(recovery_root),
         "database_bytes": 0},
    )
    wait("inventory.complete.json")
    wait("files.complete.json")
    write_json(database / "reference-index.json", recovery.checkpoint_index(recovery_root, staging / "artifacts"))
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
    maintenance_lock: recovery.MaintenanceLock | None = None,
    timeout: int,
    source_config: Path | None = None,
) -> dict:
    del database, timeout, source_config
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
        "unsettled": [],
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
    windows_evidence = {
        "schema_version": "solidworks-to-urdf.windows-lifecycle-result/v1",
        "passed": True,
        "verb": "status",
        "task": {
            "task_name": "description-e2e-endpoint",
            "state": "Disabled",
        },
    }
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
            "evidence": windows_evidence,
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


def runtime_installer(
    venv: Path,
    source_sha: str = "b" * 64,
    *,
    probe_home_side_effect: bool = False,
) -> str:
    site = venv / "lib" / "python3.12" / "site-packages"
    package = site / "description_pipeline"
    airflow_lines = ["#!/bin/sh"]
    description_lines = ["#!/bin/sh"]
    if probe_home_side_effect:
        for script_lines in (airflow_lines, description_lines):
            script_lines.extend(
                [
                    'printf "%s\\n" "$AIRFLOW_HOME" >> "$PROBE_HOMES"',
                    'mkdir -p "$AIRFLOW_HOME/logs"',
                ]
            )
    airflow_lines.append("echo 3.3.2")
    description_lines.append("echo 1.0.0")
    lines = [
        "#!/bin/sh",
        "set -eu",
        ': "${AIRFLOW_HOME:?}"',
        ': "${PIPELINE_WHEEL:?}"',
        '[ -f "$PIPELINE_WHEEL" ]',
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
        [f"printf '%s' '# airflow' > {str(site / 'airflow' / '__init__.py')!r}"]
    )
    for script_lines, entry in ((airflow_lines, "airflow"), (description_lines, "description")):
        target = venv / "bin" / entry
        lines.extend(f"printf '%s\\n' {line!r} >> {str(target)!r}" for line in script_lines)
        lines.append(f"chmod +x {str(target)!r}")
    lines.append("")
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
    socket_dir = postgres_root / "socket"
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
    runtime_installer_script = runtime_installer(release_root / ("airflow-venv-" + install_id))
    installer = "#!/bin/sh\nset -eu\nexec bash \"$(dirname \"$0\")/install_runtime.sh\"\n"
    linux_lock = b"linux-lock\n"
    windows_lock = b"windows-lock\n"
    with zipfile.ZipFile(deployment_path, "w") as archive:
        archive.writestr("deploy/operator/recovery.py", recovery_script)
        archive.writestr("deploy/airflow/install.sh", installer)
        archive.writestr("deploy/airflow/install_runtime.sh", runtime_installer_script)
        archive.writestr("requirements/linux-py312.lock", linux_lock)
        archive.writestr("requirements/windows-py312.lock", windows_lock)
    write(release_root / "deploy/airflow/install.sh", installer, mode=0o755)
    write(release_root / "deploy/airflow/install_runtime.sh", runtime_installer_script, mode=0o755)
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
            "deploy/airflow/install_runtime.sh": hashlib.sha256(runtime_installer_script.encode()).hexdigest(),
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
            "repositories": {},
            "discovery": {"record_roots": [str(spec_root)]},
        },
        "targets": {},
    }
    context = recovery.BackupContext(
        config_path=config_path,
        resolved=resolved,
        install_record=record,
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
    def reference(
        self,
        artifact_root: Path,
        *,
        run: str = "producer",
        parent: str | None = None,
        outcome: str = "completed",
    ) -> dict:
        return canonical_checkpoint_reference(
            artifact_root,
            run_id=run,
            parent_run_id=parent,
            outcome=outcome,
        )

    def recovery_root(self, references: list[dict], *, extra_artifacts=()) -> dict:
        artifacts = {
            artifact["artifact_id"]: artifact
            for reference in references
            for role in ("result_record_artifact", "output_artifact")
            for artifact in (reference[role],)
            if artifact is not None
        }
        artifacts.update({artifact["artifact_id"]: artifact for artifact in extra_artifacts})
        return {
            "decisions": references,
            "artifacts": [artifacts[key] for key in sorted(artifacts)],
            "missing_artifact_ids": [],
            "candidates": [],
            "retained_locations": [],
            "workspace_commands": [],
        }

    def test_multihop_reuse_resolves_one_original_producer_and_exact_cas_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "artifacts"
            refs = [
                self.reference(source),
                self.reference(source, run="child", parent="producer"),
                self.reference(source, run="grandchild", parent="child"),
            ]
            from description_pipeline.artifacts.store import ArtifactStore
            from description_pipeline.io import digest

            with tempfile.TemporaryDirectory(prefix="admission-requirement-") as item_directory:
                item_root = Path(item_directory)
                (item_root / "native-requirement.json").write_text(
                    '{"requirement":"future-stage"}\n',
                    encoding="utf-8",
                )
                extra_artifact = ArtifactStore(source).seal_tree(
                    item_root,
                    kind="admission_input",
                    producer_run_id=None,
                    producer_stage=None,
                    producer_tool_sha256=digest({"tool": "portal-admission"}),
                )
            root = self.recovery_root(refs, extra_artifacts=(extra_artifact.to_record(),))
            self.assertEqual(set(recovery.checkpoint_origins(refs).values()), {"producer"})
            indexed = recovery.checkpoint_index(root, source)
            self.assertEqual(
                len(indexed["artifacts"]),
                7,
            )
            self.assertIn(
                f"objects/{extra_artifact.artifact_id}",
                recovery.checkpoint_artifact_selection(root),
            )
            recovery.verify_checkpoint_index(indexed, source)
            artifact = refs[0]["output_artifact"]
            artifact_path = source / "objects" / artifact["artifact_id"] / "delivery.json"
            artifact_path.write_text("changed\n", encoding="utf-8")
            with self.assertRaises(recovery.RecoveryError):
                recovery.checkpoint_index(root, source)

    def test_failed_checkpoint_is_preserved_but_cannot_be_reused(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "artifacts"
            failed = self.reference(source, outcome="failed")
            self.assertEqual(recovery.checkpoint_origins([failed]), {("producer", "capture"): "producer"})
            with self.assertRaises(recovery.RecoveryError):
                recovery.checkpoint_origins([failed, self.reference(source, run="child", parent="producer")])
            changed = self.reference(source, run="child", parent="producer", outcome="failed")
            with self.assertRaises(recovery.RecoveryError):
                recovery.checkpoint_origins([self.reference(source), changed])

    def test_reuse_wrong_parent_artifact_and_cycle_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "artifacts"
            producer = self.reference(source)
            child = self.reference(source, run="child", parent="producer")
            cases = [
                [producer, {**child, "parent_run_id": "other"}],
                [producer, {**child, "original_producer_decision_id": "missing"}],
                [
                    producer,
                    {
                        **child,
                        "output_artifact": {
                            **child["output_artifact"],
                            "producer_run_id": "foreign",
                        },
                    },
                ],
                [
                    self.reference(source, run="a", parent="b"),
                    self.reference(source, run="b", parent="a"),
                ],
                [child],
            ]
            for refs in cases:
                with self.subTest(refs=refs), self.assertRaises(recovery.RecoveryError):
                    recovery.checkpoint_origins(refs)


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



    def test_capacity_refuses_before_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(recovery.shutil, "disk_usage", return_value=type("Usage", (), {"free": 0})()):
                with self.assertRaises(recovery.RecoveryError) as raised:
                    recovery.require_capacity([(Path(directory) / "new", 10)])
                self.assertEqual(raised.exception.code, "recovery_capacity_insufficient")
            self.assertFalse((Path(directory) / "new").exists())


    def test_windows_json_duplicate_keys_are_rejected(self):
        with self.assertRaises(recovery.RecoveryError):
            recovery.strict_json_bytes(b'{"state":{"a":1,"a":2}}', "Windows reply")
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "windows-state.json"
            state_path.write_text('{"schema_version":1,"schema_version":2}', encoding="utf-8")
            with self.assertRaises(recovery.RecoveryError) as raised:
                recovery.strict_json_file(state_path, "Windows state")
            self.assertEqual(raised.exception.code, "recovery_manifest_invalid")


class RecoveryControlFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        warnings.simplefilter("ignore", ResourceWarning)
        self.temporary = tempfile.TemporaryDirectory(
            prefix="recovery-unit-",
            dir=HERE,
        )
        self.root = Path(self.temporary.name)
        external_runtime_source = self.root / "external-runtime-source"
        write(
            external_runtime_source / "description_pipeline" / "__init__.py",
            'VERSION = "1.5.0"\n',
        )
        write(
            external_runtime_source / "mimicverse_description-1.5.0.dist-info" / "METADATA",
            "Metadata-Version: 2.1\nName: mimicverse-description\nVersion: 1.5.0\nRequires-Dist: packaging==26.3\n",
        )
        python_path = mock.patch.dict(
            os.environ,
            {"PYTHONPATH": str(external_runtime_source)},
            clear=False,
        )
        python_path.start()
        self.addCleanup(python_path.stop)
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
        self.windows_target = recovery._windows_target_from_state(manifest["windows"])
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
        self.assertTrue(Path(roots["artifact_root"]).is_dir())
        self.assertFalse((Path(roots["artifact_root"]) / "artifact.bin").exists())
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

    def test_manifest_rejects_an_extra_source_member(self):
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

    def test_recovered_runtime_environment_strips_python_context(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "PYTHONPATH": "/caller/python-path",
                "PYTHONHOME": "/caller/python-home",
                "VIRTUAL_ENV": "/caller/venv",
                "AIRFLOW_CONFIG": "/caller/airflow.cfg",
                "AIRFLOW__LOGGING__BASE_LOG_FOLDER": "/caller/logs",
                "AIRFLOW__DATABASE__SQL_ALCHEMY_CONN": "sqlite:////caller/airflow.db",
                "PATH": "/runtime/bin",
                "LD_LIBRARY_PATH": "/runtime/lib",
                "SSL_CERT_FILE": "/runtime/ca.pem",
            },
            clear=False,
        ):
            environment = recovery._recovered_runtime_environment()
        for name in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"):
            self.assertNotIn(name, environment)
        self.assertNotIn("AIRFLOW_CONFIG", environment)
        self.assertFalse(any(name.startswith("AIRFLOW__") for name in environment))
        self.assertEqual(environment["PYTHONNOUSERSITE"], "1")
        self.assertEqual(environment["PATH"], "/runtime/bin")
        self.assertEqual(environment["LD_LIBRARY_PATH"], "/runtime/lib")
        self.assertEqual(environment["SSL_CERT_FILE"], "/runtime/ca.pem")

    def test_recovered_runtime_environment_does_not_relax_pip_check(self) -> None:
        venv = self.root / "missing-package-venv"
        environment = recovery._recovered_runtime_environment()
        subprocess.run(
            [sys.executable, "-m", "venv", str(venv)],
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        )
        site_packages = venv / "lib" / "python3.12" / "site-packages"
        write(
            site_packages / "missing_requirement-1.0.dist-info" / "METADATA",
            "Metadata-Version: 2.1\n"
            "Name: missing-requirement\n"
            "Version: 1.0\n"
            "Requires-Dist: intentionally-missing-runtime-package==1\n",
        )
        checked = subprocess.run(
            [
                str(venv / "bin" / "python"),
                "-I",
                "-B",
                "-m",
                "pip",
                "check",
            ],
            env=environment,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        self.assertNotEqual(checked.returncode, 0)
        self.assertIn(
            "intentionally-missing-runtime-package",
            checked.stdout + checked.stderr,
        )

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
        runtime_installer_script = runtime_installer(
            release_root / "airflow-venv",
            probe_home_side_effect=True,
        )
        full_install_marker = self.root / "full-install-called"
        installer = (
            "#!/bin/sh\n"
            "set -eu\n"
            f"touch {str(full_install_marker)!r}\n"
            "exit 99\n"
        )
        airflow_cfg = self.root / "airflow-home" / "airflow.cfg"
        write(airflow_cfg, "restored-config\n")
        probe_homes = self.root / "entry-probe-homes"
        self.enterContext(
            mock.patch.dict(
                os.environ,
                {
                    "AIRFLOW_HOME": str(airflow_cfg.parent),
                    "AIRFLOW_CONFIG": str(self.root / "foreign-airflow.cfg"),
                    "AIRFLOW__LOGGING__BASE_LOG_FOLDER": str(self.root / "foreign-logs"),
                    "AIRFLOW__DATABASE__SQL_ALCHEMY_CONN": "sqlite:////foreign/airflow.db",
                    "PYTHONPATH": "/foreign/python-path",
                    "PYTHONHOME": "/foreign/python-home",
                    "VIRTUAL_ENV": "/foreign/venv",
                    "PROBE_HOMES": str(probe_homes),
                },
            )
        )
        linux_lock = b"linux-lock\n"
        windows_lock = b"windows-lock\n"
        with zipfile.ZipFile(deployment_zip, "w") as archive:
            archive.writestr("deploy/operator/recovery.py", recovery_script)
            archive.writestr("deploy/airflow/install.sh", installer)
            archive.writestr("deploy/airflow/install_runtime.sh", runtime_installer_script)
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
                        "deploy/airflow/install_runtime.sh": hashlib.sha256(
                            runtime_installer_script.encode()
                        ).hexdigest(),
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
        self.assertFalse(full_install_marker.exists())
        self.assertEqual(airflow_cfg.read_text(encoding="utf-8"), "restored-config\n")
        self.assertFalse((airflow_cfg.parent / "logs").exists())
        verified = recovery.rehydrate_release_runtime(
            manifest=manifest,
            plan={"roots": {"release_root": str(release_root)}},
            extracted=extracted,
            status_dir=status_dir,
            verify_only=True,
        )
        self.assertEqual(verified["identity"]["version"], "1.0.0")
        self.assertFalse((airflow_cfg.parent / "logs").exists())
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
        homes = set(probe_homes.read_text().splitlines())
        self.assertGreaterEqual(len(homes), 2)
        self.assertNotIn(str(airflow_cfg.parent), homes)
        self.assertTrue(all(Path(probe_home).is_relative_to(release_root) for probe_home in homes))
        self.assertTrue(all(not Path(probe_home).exists() for probe_home in homes))

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
            lambda row: row["database"]["before"].update(
                unsettled=[{"object_kind": "run", "object_id": "fixture", "state": "running"}],
            ),
            lambda row: row["database"]["before"].pop("unsettled"),
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
                "capture_windows_recovery",
                side_effect=recovery.RecoveryError(
                    "fixture_failure",
                    "failure after the stopped marker",
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
            "stopped",
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
            recovery,
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
            recovery,
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
                recovery,
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

    def test_windows_target_plan_must_equal_the_archived_identity(self) -> None:
        out, manifest = self.backup()
        del out
        manifest_path = self.root / "recovery" / "recovery-manifest.json"
        manifest_sha = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        roots = {row["name"]: row["path"] for row in manifest["roots"] if "reconstructible_runtime" not in row}
        plan_path = self.target_plan(manifest_sha, roots)
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        plan["windows"]["install_id"] = "win-" + "9" * 32
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        with self.assertRaises(recovery.RecoveryError) as raised:
            recovery.validate_target_plan(plan, manifest, manifest_sha)
        self.assertEqual(raised.exception.code, "recovery_target_plan_invalid")

    def test_windows_state_members_bind_every_original_recovery_input(self) -> None:
        staging = self.root / "windows-member-binding"
        state, _same_state = windows_recovery_fixture(self.context, staging)
        self.assertIs(recovery._validate_windows_recovery_state(state), state)
        recovery._verify_windows_state_members(state, staging)
        for role in (
            "binding",
            "endpoint_config",
            "token",
            "install_id",
            "install_receipt",
            "release",
            "lifecycle",
            "bootstrap",
            "acl",
        ):
            with self.subTest(role=role):
                changed = copy.deepcopy(state)
                member = staging / changed[role]["member"]
                original = member.read_bytes()
                try:
                    member.write_bytes(original + b"tampered")
                    with self.assertRaises(recovery.RecoveryError) as raised:
                        recovery._verify_windows_state_members(changed, staging)
                    self.assertEqual(raised.exception.code, "windows_recovery_bundle_invalid")
                finally:
                    member.write_bytes(original)

    def test_windows_verify_only_rejects_a_running_target(self) -> None:
        staging = self.root / "windows-verify-only"
        state, _same_state = windows_recovery_fixture(self.context, staging)
        live = copy.deepcopy(state["status"])
        live["task"]["state"] = "Running"
        live["status"]["task_state"] = "Running"
        status_dir = self.root / "windows-verify-status"
        status_dir.mkdir()
        with (
            mock.patch.object(recovery, "_windows_public_verb", return_value=live),
            self.assertRaises(recovery.RecoveryError) as raised,
        ):
            self.real_restore_windows_recovery(
                manifest={"windows": state},
                plan={
                    "windows": {
                        "ssh_host": state["ssh_host"],
                        "state_root": state["state_root"],
                    }
                },
                extracted=staging,
                status_dir=status_dir,
                verify_only=True,
                commands=[],
            )
        self.assertEqual(raised.exception.code, "windows_recovery_state_invalid")

    def test_windows_restore_target_must_be_empty(self) -> None:
        document = {
            "passed": False,
            "code": "windows_target_not_empty",
            "path": "C:/description/state",
        }
        completed = subprocess.CompletedProcess(
            ["ssh", "windows-fixture"],
            1,
            json.dumps(document) + "\n",
            "",
        )
        with (
            mock.patch.object(recovery, "_run_windows_powershell", return_value=(document, completed)),
            self.assertRaises(recovery.RecoveryError) as raised,
        ):
            recovery._windows_restore_target_empty(
                ssh_host="windows-fixture",
                binding={
                    "task_name": "description-e2e-endpoint",
                    "task_path": "\\URDF\\",
                },
                paths=["C:/description/state/install-id"],
                timeout=5,
                commands=[],
            )
        self.assertEqual(raised.exception.code, "windows_target_not_empty")
        self.assertEqual(raised.exception.detail["path"], "C:/description/state")

    def test_windows_target_observation_failure_is_unknown(self) -> None:
        document = {
            "passed": False,
            "code": "windows_target_observation_unknown",
            "message": "access denied",
        }
        completed = subprocess.CompletedProcess(
            ["ssh", "windows-fixture"],
            2,
            json.dumps(document) + "\n",
            "",
        )
        with (
            mock.patch.object(recovery, "_run_windows_powershell", return_value=(document, completed)),
            self.assertRaises(recovery.RecoveryError) as raised,
        ):
            recovery._windows_restore_target_empty(
                ssh_host="windows-fixture",
                binding={
                    "task_name": "description-e2e-endpoint",
                    "task_path": "\\URDF\\",
                },
                paths=["C:/description/state/install-id"],
                roots=["C:/description/state"],
                timeout=5,
                commands=[],
            )
        self.assertEqual(raised.exception.code, "windows_recovery_transport_unknown")

    def test_windows_path_probe_failure_is_unknown(self) -> None:
        document = {
            "passed": False,
            "code": "windows_path_observation_unknown",
            "message": "access denied",
        }
        completed = subprocess.CompletedProcess(
            ["ssh", "windows-fixture"],
            2,
            json.dumps(document) + "\n",
            "",
        )
        with (
            mock.patch.object(recovery, "_run_windows_powershell", return_value=(document, completed)),
            self.assertRaises(recovery.RecoveryError) as raised,
        ):
            recovery._windows_path_exists(
                ssh_host="windows-fixture",
                path="C:/description/state/native-operations",
                timeout=5,
                commands=[],
            )
        self.assertEqual(raised.exception.code, "windows_recovery_transport_unknown")

    def test_windows_acl_projection_rejects_incomplete_or_failed_observation(self) -> None:
        for entries, returncode in (([], 0), ([{"path": "C:/description/state"}], 2)):
            with self.subTest(entries=entries, returncode=returncode):
                document = {"passed": True, "entries": entries}
                completed = subprocess.CompletedProcess(
                    ["ssh", "windows-fixture"], returncode, json.dumps(document) + "\n", ""
                )
                with (
                    mock.patch.object(
                        recovery, "_run_windows_powershell", return_value=(document, completed)
                    ),
                    self.assertRaises(recovery.RecoveryError) as raised,
                ):
                    recovery._windows_acl_document(
                        ssh_host="windows-fixture",
                        paths=["C:/description/state"],
                        timeout=5,
                        commands=[],
                    )
                self.assertEqual(raised.exception.code, "windows_recovery_transport_unknown")

    def test_windows_lifecycle_refusal_preserves_the_lifecycle_code(self) -> None:
        completed = subprocess.CompletedProcess(
            ["ssh", "windows-fixture"],
            0,
            json.dumps({"passed": False, "code": "windows_task_not_disabled"}) + "\n",
            "",
        )
        document = {"passed": False, "code": "windows_task_not_disabled"}
        with (
            mock.patch.object(recovery, "_run_windows_powershell", return_value=(document, completed)),
            self.assertRaises(recovery.RecoveryError) as raised,
        ):
            recovery._windows_public_verb(
                ssh_host="windows-fixture",
                state_root="C:/description/state",
                verb="stop",
                timeout=5,
                commands=[],
            )
        self.assertEqual(raised.exception.code, "windows_lifecycle_refused")
        self.assertEqual(raised.exception.detail["code"], "windows_task_not_disabled")

    def test_windows_lifecycle_success_must_name_the_requested_verb(self) -> None:
        completed = subprocess.CompletedProcess(
            ["ssh", "windows-fixture"],
            0,
            json.dumps({"passed": True, "verb": "health"}) + "\n",
            "",
        )
        document = {"passed": True, "verb": "health"}
        with (
            mock.patch.object(recovery, "_run_windows_powershell", return_value=(document, completed)),
            self.assertRaises(recovery.RecoveryError) as raised,
        ):
            recovery._windows_public_verb(
                ssh_host="windows-fixture",
                state_root="C:/description/state",
                verb="stop",
                timeout=5,
                commands=[],
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
            document, completed = recovery._run_windows_powershell(
                ssh_host="windows-fixture",
                script="fixture",
                timeout=5,
                commands=commands,
            )
        self.assertEqual(document["code"], "fixture")
        self.assertEqual(completed.returncode, 2)
        self.assertEqual(
            windows_transport.decode_output(completed.stderr, stream="stderr"),
            " localized PowerShell error",
        )
        self.assertEqual(completed.transport["encoding"], "base64(gzip(utf-8))")

    def test_windows_truncated_utf16_is_a_classified_transport_error(self) -> None:
        process = subprocess.CompletedProcess(
            ["ssh", "windows-fixture"],
            0,
            b"\xff\xfeA",
            b"",
        )
        process.transport = windows_transport.request_metadata("fixture")
        commands: list[dict] = []
        with (
            mock.patch.object(recovery, "_transport_powershell", return_value=process),
            self.assertRaises(recovery.RecoveryError) as raised,
        ):
            recovery._run_windows_powershell(
                ssh_host="windows-fixture",
                script="fixture",
                timeout=5,
                commands=commands,
            )
        self.assertEqual(raised.exception.code, "windows_recovery_transport_unknown")
        self.assertEqual(raised.exception.detail["stream"], "stdout")
        self.assertEqual(
            raised.exception.detail["sha256"],
            hashlib.sha256(b"\xff\xfeA").hexdigest(),
        )
        self.assertEqual(len(commands), 1)
        self.assertEqual(commands[0]["returncode"], process.returncode)
        self.assertEqual(commands[0]["stdout_sha256"], raised.exception.detail["sha256"])

    def test_windows_missing_result_preserves_raw_transport_evidence(self) -> None:
        process = subprocess.run(
            [sys.executable, "-c", "import sys; sys.stderr.buffer.write(bytes.fromhex('b2e2cad4')); sys.exit(23)"],
            capture_output=True,
            check=False,
        )
        commands: list[dict] = []
        for recorded_commands in (commands, None):
            with (
                self.subTest(commands=recorded_commands),
                mock.patch.object(recovery, "_transport_powershell", return_value=process),
                self.assertRaises(recovery.RecoveryError) as raised,
            ):
                recovery._run_windows_powershell(
                    ssh_host="windows-fixture",
                    script="fixture",
                    timeout=5,
                    commands=recorded_commands,
                )
            self.assertEqual(raised.exception.code, "windows_recovery_transport_unknown")
            self.assertEqual(raised.exception.detail["returncode"], 23)
            self.assertEqual(raised.exception.detail["stderr_sha256"], hashlib.sha256(process.stderr).hexdigest())
            self.assertEqual(raised.exception.detail["stdout_sha256"], hashlib.sha256(process.stdout).hexdigest())
        self.assertEqual(commands, [raised.exception.detail])

    def test_windows_transport_exception_preserves_partial_streams_and_request(self) -> None:
        with self.assertRaises(subprocess.TimeoutExpired) as timeout:
            subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import sys,time; sys.stderr.buffer.write(bytes.fromhex('b2e2cad4')); "
                    "sys.stderr.buffer.flush(); time.sleep(30)",
                ],
                capture_output=True,
                check=False,
                timeout=1,
            )
        with self.assertRaises(FileNotFoundError) as missing:
            subprocess.run([str(self.root / "missing-transport")], capture_output=True, check=False)
        self.assertEqual(timeout.exception.stderr, bytes.fromhex("b2e2cad4"))
        for error in (timeout.exception, missing.exception):
            for record in (True, False):
                commands: list[dict] = []
                with (
                    self.subTest(error=type(error).__name__, record=record),
                    mock.patch.object(recovery, "_transport_powershell", side_effect=error),
                    self.assertRaises(recovery.RecoveryError) as raised,
                ):
                    recovery._run_windows_powershell(
                        ssh_host="windows-fixture",
                        script="fixture",
                        timeout=5,
                        commands=commands if record else None,
                    )
                self.assertEqual(raised.exception.code, "windows_recovery_transport_unknown")
                detail = raised.exception.detail
                self.assertIsNone(detail["returncode"])
                self.assertEqual(detail["error_type"], type(error).__name__)
                stdout, stderr = windows_transport.error_streams(error)
                self.assertEqual(detail["stdout_sha256"], hashlib.sha256(stdout).hexdigest())
                self.assertEqual(detail["stderr_sha256"], hashlib.sha256(stderr).hexdigest())
                self.assertEqual(detail["transport"], windows_transport.request_metadata("fixture"))
                self.assertEqual(commands, [detail] if record else [])

    def test_windows_unencodable_request_is_classified_before_dispatch(self) -> None:
        commands: list[dict] = []
        with (
            mock.patch.object(recovery, "_transport_powershell") as dispatch,
            self.assertRaises(recovery.RecoveryError) as raised,
        ):
            recovery._run_windows_powershell(
                ssh_host="windows-fixture",
                script="\ud800",
                timeout=5,
                commands=commands,
            )
        dispatch.assert_not_called()
        self.assertEqual(raised.exception.code, "windows_recovery_transport_unknown")
        self.assertEqual(raised.exception.detail["error_type"], "UnicodeEncodeError")
        self.assertIsNone(raised.exception.detail["transport"])
        self.assertIsNone(raised.exception.detail["returncode"])
        self.assertEqual(commands, [raised.exception.detail])

    def test_recovery_propagates_bytecode_suppression_to_children(self) -> None:
        self.assertEqual(os.environ.get("PYTHONDONTWRITEBYTECODE"), "1")
        source = (DEPLOY / "recovery.py").read_text(encoding="utf-8")
        self.assertIn('os.environ["PYTHONDONTWRITEBYTECODE"] = "1"', source)
        self.assertIn("sys.dont_write_bytecode = True", source)

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
                "instance": {
                    "task_name": "description-e2e-endpoint",
                    "state_root": "C:/description/state",
                },
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
            "unsettled": [],
            "active_application_sessions": [],
            "wal_lsn": "0/100",
        }
        evidence = {
            "schema_version": "solidworks-to-urdf.windows-lifecycle-result/v1",
            "passed": True,
            "verb": "status",
            "task": {
                "task_name": "description-e2e-endpoint",
                "state": "Disabled",
            },
        }
        lock = recovery.acquire_maintenance_lock(
            Path(self.context.resolved["storage"]["state_root"])
        )
        self.addCleanup(lock.close)
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
                "_windows_task_state",
                return_value=("Disabled", evidence),
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
                maintenance_lock=lock,
                timeout=30,
            )
        commands = log.read_text().splitlines()
        self.assertEqual(len(commands), 2)
        self.assertTrue(commands[0].startswith("drain --config "))
        inherited = f"|{lock.owner_id}|{lock.descriptor}|{lock.path}"
        self.assertTrue(commands[0].endswith(inherited))
        self.assertNotIn("--keep-database", commands[0])
        self.assertTrue(commands[1].startswith("stop --config "))
        self.assertIn(" --keep-database", commands[1])
        self.assertTrue(commands[1].endswith(" --keep-database" + inherited))
        self.assertEqual(state["windows"]["state"], "Disabled")
        self.assertEqual(state["windows"]["evidence"], evidence)
        self.assertEqual(state["database_before"], probe)

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
        member = next(row for row in manifest["members"] if row["member"] == "release/release.json")
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

    def test_windows_capture_binds_every_identity_to_linux_install(self):
        original = self.root / "windows-original"
        state, _ = windows_recovery_fixture(self.context, original)
        originals = {path.name: path.read_bytes() for path in (original / "windows").iterdir() if path.is_file()}

        def transfer(*, source, destination, **_kwargs):
            source_path = original / "windows" / str(source).rsplit("/", 1)[-1]
            if source_path.is_dir():
                shutil.copytree(source_path, destination)
            else:
                shutil.copy2(source_path, destination)

        cases = [
            "valid",
            "install_id",
            "config_digest",
            "release",
            "target_identity",
            "install-id",
            "receipt-install_id",
            "receipt-config_digest",
            "receipt-source_commit",
            "release-source_commit",
        ]
        for index, field in enumerate(cases):
            with self.subTest(field=field):
                for name, payload in originals.items():
                    (original / "windows" / name).write_bytes(payload)
                status = copy.deepcopy(state["status"])
                if field in {"install_id", "config_digest"}:
                    status["identity"][field] = "f" * 64
                elif field == "release":
                    status["identity"]["release"]["source_commit"] = "f" * 40
                elif field == "target_identity":
                    status["identity"][field] = NativeTargetIdentity.create(
                        install_id="win-" + "f" * 32, pipeline_id="solidworks-to-urdf"
                    ).to_record()
                elif field == "install-id":
                    (original / "windows" / field).write_text("win-" + "f" * 32 + "\n")
                elif field.startswith(("receipt-", "release-")):
                    filename = "install-receipt.json" if field.startswith("receipt-") else "release.json"
                    path = original / "windows" / filename
                    document = json.loads(path.read_bytes())
                    document[field.split("-", 1)[1]] = "f" * 64
                    path.write_bytes(recovery.canonical_json(document))
                staging = self.root / f"capture-{index}"
                with (
                    mock.patch.object(recovery, "_windows_scp", side_effect=transfer),
                    mock.patch.object(recovery, "_windows_public_verb", return_value=status),
                    mock.patch.object(recovery, "_windows_path_exists", return_value=False),
                    mock.patch.object(recovery, "_windows_acl_document", return_value={"passed": True}),
                ):
                    if field == "valid":
                        self.real_capture_windows_recovery(
                            self.context, backup_id="capture", staging=staging, commands=[]
                        )
                        self.assertTrue((staging / "windows" / "state.json").is_file())
                    else:
                        with self.assertRaises(recovery.RecoveryError) as raised:
                            self.real_capture_windows_recovery(
                                self.context, backup_id="capture", staging=staging, commands=[]
                            )
                        self.assertEqual(raised.exception.code, "windows_recovery_identity_mismatch")
                        self.assertFalse((staging / "windows" / "state.json").exists())

    def test_database_probe_uses_bootstrap_only_for_same_endpoint_control_reads(self):
        dsn = {
            "socket_dir": "/cluster/socket",
            "port": 5432,
            "database": "airflow_meta",
            "user": "platform_service",
            "password": "application-secret",
        }
        application = {
            "database": "airflow_meta",
            "database_oid": "123",
            "role": "platform_service",
            "server_version_num": "140024",
        }
        cluster = {**application, "data_directory": "/cluster/data", "system_identifier": "123456"}
        with mock.patch.object(recovery, "psql_json", side_effect=[application, cluster]) as probe:
            actual = recovery.database_probe(mock.sentinel.context, dsn)
        self.assertEqual(actual["system_identifier"], "123456")
        original, control = [call.kwargs for call in probe.call_args_list]
        for arguments in (original, control):
            self.assertEqual(arguments["socket_dir"], Path(dsn["socket_dir"]))
            self.assertEqual(arguments["port"], dsn["port"])
            self.assertEqual(arguments["database"], dsn["database"])
        self.assertEqual(original["user"], dsn["user"])
        self.assertEqual(original["password"], dsn["password"])
        self.assertEqual(control["user"], recovery.BOOTSTRAP_ROLE)
        self.assertNotIn("password", control)
        self.assertIn("pg_control_system()", control["query"])
        self.assertIn("current_setting('data_directory')", control["query"])
        for changed in ({"role": "other"}, {"database": "other"}):
            with (
                self.subTest(changed=changed),
                mock.patch.object(recovery, "psql_json", side_effect=[{**application, **changed}, cluster]),
                self.assertRaises(recovery.RecoveryError),
            ):
                recovery.database_probe(mock.sentinel.context, dsn)

    def test_backup_and_extraction_reject_public_or_link_keys(self):
        link = self.root / "external-link.key"
        link.symlink_to(self.key)
        for mode in (0o640, 0o604, 0o660, 0o644, "link"):
            self.key.chmod(0o600 if mode == "link" else mode)
            key = link if mode == "link" else self.key
            for action in (
                lambda key=key: recovery.backup_command(
                    config_path=self.context.config_path,
                    backup_id="unsafe-key",
                    key_file=key,
                    out=self.root / "unsafe-backup",
                ),
                lambda key=key: recovery.prepare_extraction(
                    {}, self.root / "manifest.json", key, self.root / "extract"
                ),
            ):
                with self.subTest(mode=mode, action=action):
                    with self.assertRaises(recovery.RecoveryError) as raised:
                        action()
                    self.assertIn(raised.exception.code, {"recovery_key_unavailable", "recovery_target_path_unsafe"})
            self.assertFalse((self.root / "unsafe-backup").exists())
            self.assertFalse((self.root / "extract").exists())

    @unittest.skipUnless(shutil.which("pwsh") or os.environ.get("PWSH_EXECUTABLE"), "PowerShell is unavailable")
    def test_windows_restore_checks_all_ancestors_before_target_writes(self):
        powershell = os.environ.get("PWSH_EXECUTABLE") or shutil.which("pwsh")
        external = self.root / "windows-outside"
        external.mkdir()
        alias = self.root / "windows-alias"
        alias.symlink_to(external, target_is_directory=True)
        target = alias / "new-parent" / "target"

        def execute(*, script, **_kwargs):
            script = "function Get-ScheduledTask {}\n" + script
            completed = subprocess.run(
                [powershell, "-NoProfile", "-NonInteractive", "-Command", script],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertTrue(completed.stdout.strip(), completed.stderr)
            return json.loads(completed.stdout.strip().splitlines()[-1]), completed

        with mock.patch.object(recovery, "_run_windows_powershell", side_effect=execute):
            for action in (
                lambda: recovery._windows_create_parents(
                    ssh_host="fixture", paths=[str(target)], timeout=30, commands=[]
                ),
                lambda: recovery._windows_restore_target_empty(
                    ssh_host="fixture",
                    binding={"task_name": "fixture", "task_path": "fixture"},
                    paths=[str(target)],
                    roots=[str(target.parent)],
                    timeout=30,
                    commands=[],
                ),
            ):
                with self.subTest(action=action):
                    with self.assertRaises(recovery.RecoveryError) as raised:
                        action()
                    self.assertEqual(raised.exception.code, "recovery_target_path_unsafe")
                    self.assertEqual(list(external.iterdir()), [])
            safe = self.root / "windows-safe" / "target"
            recovery._windows_create_parents(ssh_host="fixture", paths=[str(safe)], timeout=30, commands=[])
            recovery._windows_restore_target_empty(
                ssh_host="fixture",
                binding={"task_name": "fixture", "task_path": "fixture"},
                paths=[str(safe)],
                roots=[str(safe.parent)],
                timeout=30,
                commands=[],
            )
            self.assertTrue(safe.parent.is_dir())

    def test_source_database_must_match_local_control_identity(self):
        context = recovery.command_context(self.context.postgres_root / "root/usr/lib/postgresql/14/bin", None, [])
        for changed in ({"data_directory": "/elsewhere/data"}, {"system_identifier": "9999999999"}):
            with self.subTest(changed=changed):
                probe = {
                    "database": "airflow_meta",
                    "database_oid": "16384",
                    "role": "fixture",
                    "server_version_num": "140024",
                    "data_directory": str(self.context.postgres_root / "data"),
                    "system_identifier": "1111111111",
                    **changed,
                }
                with (
                    mock.patch.object(recovery, "database_probe", return_value=probe),
                    self.assertRaises(recovery.RecoveryError) as raised,
                ):
                    recovery.capture_database_authority(
                        self.context.resolved,
                        self.context.derived_env,
                        postgres_root=self.context.postgres_root,
                        context=context,
                    )
                self.assertEqual(raised.exception.code, "recovery_dsn_authority_mismatch")

    def test_restore_refuses_link_ancestors_before_creating_targets(self):
        external = self.root / "outside"
        external.mkdir()
        alias = self.root / "alias"
        alias.symlink_to(external, target_is_directory=True)
        source = self.root / "payload"
        write(source / "secret", "private")
        target = alias / "new-parent" / "restored"
        manifest = {
            "roots": [{"name": "release_root", "path": str(target), "reconstructible_runtime": True}],
            "files": [], "secrets": [], "members": [], "database": {"dump": {"bytes": 1}},
        }
        plan = {"postgres": {"data_dir": str(self.root)}, "roots": {}}
        for action in (
            lambda: recovery.validate_target_filesystem(manifest, plan),
            lambda: recovery.require_empty_target_directory(target, "target"),
            lambda: recovery.copy_extracted_directory(source, target, [], "state"),
            lambda: recovery.restore_exact_files(
                {"files": [{"name": "secret", "member": "secret", "path": str(target), "mode": 0o600}], "secrets": []},
                source,
            ),
        ):
            with self.subTest(action=action):
                with self.assertRaises(recovery.RecoveryError) as raised:
                    action()
                self.assertEqual(raised.exception.code, "recovery_target_path_unsafe")
                self.assertEqual(list(external.iterdir()), [])

    def test_restore_refuses_public_or_link_keys_before_extraction(self):
        out, manifest = self.backup()
        self.isolate_source()
        manifest_path = out / "recovery-manifest.json"
        roots = {row["name"]: row["path"] for row in manifest["roots"] if "reconstructible_runtime" not in row}
        plan = self.target_plan(recovery.sha256_file(manifest_path), roots)
        link = self.root / "linked.key"
        link.symlink_to(self.key)
        for mode in (0o640, 0o604, 0o660, 0o644, "link"):
            with self.subTest(mode=mode):
                self.key.chmod(0o600 if mode == "link" else mode)
                key = link if mode == "link" else self.key
                with (
                    mock.patch.object(recovery, "prepare_extraction") as extract,
                    self.assertRaises(recovery.RecoveryError) as raised,
                ):
                    recovery.restore_command(
                        manifest_path=manifest_path,
                        key=key,
                        target_plan_path=plan,
                        status_dir=self.root / f"status-key-{mode}",
                    )
                self.assertIn(raised.exception.code, {"recovery_key_unavailable", "recovery_target_path_unsafe"})
                extract.assert_not_called()

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


class RecoveryFailureEvidenceTests(unittest.TestCase):
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

if __name__ == "__main__":
    unittest.main()
