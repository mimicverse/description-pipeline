"""Fake-CLI unit and end-to-end control tests for recovery."""

from __future__ import annotations

import argparse
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
import tempfile
import threading
import textwrap
import unittest
import warnings
from pathlib import Path
from unittest import mock

from cryptography.fernet import Fernet

HERE = Path(__file__).resolve().parent
REPOSITORY = HERE.parents[1]
DEPLOY = Path(
    os.environ.get("DESCRIPTION_DEPLOY_DIR", REPOSITORY / "deploy" / "operator")
)
sys.path.insert(0, str(DEPLOY))
sys.path.insert(0, str(HERE))

import recovery  # noqa: E402

warnings.filterwarnings("ignore", category=ResourceWarning)


def write(path: Path, payload: str | bytes, mode: int = 0o600) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_bytes(payload.encode() if isinstance(payload, str) else payload)
    path.chmod(mode)
    return path


def permission(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


PSQL = r"""#!/usr/bin/env python3
import hashlib
import json
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
target = "target" in socket_dir
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
            "databases": [],
            "roles": ["solidworks"],
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
        payload = []
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
    wait("inventory.complete.json")
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
    write_json(database / "reference-index.json", [])
    write_json(control / "role-settings-before.json", role_plan)
    write_json(control / "sequence-before.json", [])
    write_json(
        control / "snapshot.ready.json",
        {"status": "ready", "snapshot_id": "00000001-00000001-1"},
    )
    wait("files.complete.json")
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
                    "instance": {"task_name": task_name},
                }
            }
        )
        + "\n",
    )
    write(state_root / "install-record.json", '{"install_id":"install-a"}\n')
    write(state_root / "install-records" / "install-a.json", '{"install":"a"}\n')
    write(state_root / "snapshots" / "install-a.json", '{"snapshot":"a"}\n')
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
    release_bytes = b"portable-wheel\n"
    write(release_root / "dist.whl", release_bytes)
    release_document = {
        "version": "1.0.0-test",
        "source_commit": "a" * 40,
        "source_sha256": "b" * 64,
        "artifacts": [
            {
                "name": "dist.whl",
                "sha256": hashlib.sha256(release_bytes).hexdigest(),
            }
        ],
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
            "instance": {"task_name": task_name},
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
        release_document=release_document,
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
            "schema_version": "solidworks-to-urdf.workspace-rehydrate/v1",
            "repositories": [],
        },
        source_code={
            "commit": "c" * 40,
            "tree": "d" * 40,
            "worktree": "clean",
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
        return out, manifest

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

    def test_backup_and_restore_control_flow(self) -> None:
        out, manifest = self.backup()
        self.assertEqual(manifest["schema_version"], recovery.MANIFEST_SCHEMA)
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
        roots = {row["name"]: row["path"] for row in manifest["roots"]}
        plan = self.target_plan(manifest_sha, roots)
        status_dir = self.root / "status"
        result = recovery.restore_command(
            manifest_path=manifest_path,
            key=self.key,
            target_plan_path=plan,
            status_dir=status_dir,
        )
        self.assertEqual(result["status"], "succeeded")
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

    def test_manifest_digest_mismatch_fails_before_target_mutation(self) -> None:
        out, manifest = self.backup()
        self.isolate_source()
        sidecar = out / "recovery-manifest.sha256"
        write(sidecar, "0" * 64 + "  recovery-manifest.json\n")
        roots = {row["name"]: row["path"] for row in manifest["roots"]}
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
        receipt = json.loads((status_dir / "receipt.json").read_text())
        self.assertEqual(receipt["status"], "failed")
        self.assertEqual(receipt["phase"], "preflight")
        self.assertFalse(Path(roots["handoff_root"]).exists())

    def test_nonempty_target_is_refused_before_database_restore(self) -> None:
        out, manifest = self.backup()
        self.isolate_source()
        manifest_path = out / "recovery-manifest.json"
        manifest_sha = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        roots = {row["name"]: row["path"] for row in manifest["roots"]}
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
        roots = {row["name"]: row["path"] for row in manifest["roots"]}
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
        before = {
            path.relative_to(out).as_posix(): path.read_bytes()
            for path in out.rglob("*")
            if path.is_file()
        }
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
        after = {
            path.relative_to(out).as_posix(): path.read_bytes()
            for path in out.rglob("*")
            if path.is_file()
        }
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
        lock = recovery.acquire_maintenance_lock(
            Path(self.context.resolved["storage"]["state_root"])
        )
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
                    "operatorctl_path": str(
                        Path(recovery.__file__).with_name("operatorctl.sh")
                    ),
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
            "Running",
            "",
        )
        with (
            mock.patch.object(
                recovery,
                "_windows_task_state",
                return_value=("Running", evidence),
            ),
            mock.patch.object(
                recovery,
                "_run_maintenance_command",
                return_value=(completed, evidence),
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
        lock = recovery.acquire_maintenance_lock(
            Path(self.context.resolved["storage"]["state_root"])
        )
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
                    return_value=[
                        {"unit": unit, "active_state": "inactive"}
                        for unit in recovery.MAINTENANCE_UNITS
                    ],
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
            self.assertTrue(
                commands[1].endswith(" --keep-database" + inherited)
            )
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
        roots = {row["name"]: row["path"] for row in manifest["roots"]}
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
        roots = {row["name"]: row["path"] for row in manifest["roots"]}
        plan = self.target_plan(manifest_sha, roots)
        with self.assertRaises(recovery.RecoveryError) as raised:
            recovery.restore_command(
                manifest_path=manifest_path,
                key=self.key,
                target_plan_path=plan,
                status_dir=self.root / "status-member",
            )
        self.assertEqual(raised.exception.code, "recovery_archive_member_mismatch")

    def test_relocated_mode_is_refused(self) -> None:
        out, manifest = self.backup()
        self.isolate_source()
        manifest_path = out / "recovery-manifest.json"
        manifest_sha = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        roots = {row["name"]: row["path"] for row in manifest["roots"]}
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
        roots = {row["name"]: row["path"] for row in manifest["roots"]}
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
        roots = {row["name"]: row["path"] for row in manifest["roots"]}
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


if __name__ == "__main__":
    unittest.main()
