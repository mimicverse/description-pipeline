"""Bind the Airflow endpoint connection to one active immutable config snapshot.

The persisted connection is only an IF-11 reference. Endpoint address, Linux
routing, target routing and token-file references are resolved from the
snapshot by the DAG and health consumers.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from airflow.models.connection import Connection
from airflow.settings import Session

from description_pipeline import interface_records
from description_pipeline.io import parse_data


def _active_snapshot_reference(state_root: Path, snapshot: Path) -> dict:
    if not state_root.is_absolute():
        raise SystemExit("state root must be absolute")
    if not snapshot.is_absolute():
        raise SystemExit("snapshot path must be absolute")
    record_path = state_root / "install-record.json"
    if not record_path.is_file():
        raise SystemExit(f"active install record is missing: {record_path}")
    record = interface_records.validate_install_record(parse_data(record_path.read_bytes(), record_path))
    if record["paths"].get("state_root") != str(state_root):
        raise SystemExit("active install record belongs to another state root")
    if record["snapshot"]["path"] != str(snapshot):
        raise SystemExit("snapshot is not the active install record's snapshot")
    reference = {
        "schema_version": interface_records.CONFIG_SNAPSHOT_SCHEMA,
        "path": str(snapshot),
        "sha256": record["snapshot"]["digest"],
    }
    payload = interface_records.check_config_identity_file(reference, allowed_root=str(state_root))
    actual = hashlib.sha256(payload).hexdigest()
    if actual != reference["sha256"]:
        raise SystemExit("snapshot digest does not match the active install record")
    document = parse_data(payload, snapshot)
    interface_records.validate_config_snapshot(document)
    if document["resolved"]["storage"]["state_root"] != str(state_root):
        raise SystemExit("snapshot belongs to another state root")
    interface_records.require_active_config_identity(reference, record)
    return reference


def _connection_document(connection: Connection) -> dict:
    return {
        "host": connection.host or "",
        "port": connection.port,
        "password": connection.password or "",
        "extra": dict(connection.extra_dejson or {}),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--conn-id", default="solidworks_windows")
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument("--snapshot", type=Path, required=True)
    args = parser.parse_args()

    reference = _active_snapshot_reference(args.state_root, args.snapshot)
    with Session() as session:
        connection = session.query(Connection).filter(Connection.conn_id == args.conn_id).one_or_none()
        if connection is None:
            connection = Connection(conn_id=args.conn_id)
            session.add(connection)
        connection.conn_type = "http"
        connection.host = ""
        connection.port = None
        connection.login = None
        connection.password = ""
        connection.schema = None
        connection.extra = json.dumps({"config_identity": reference}, sort_keys=True, separators=(",", ":"))
        session.commit()
        stored = session.query(Connection).filter(Connection.conn_id == args.conn_id).one_or_none()
    if stored is None:
        raise SystemExit(f"connection {args.conn_id} was not persisted")
    stored_reference = interface_records.validate_connection_config_identity(_connection_document(stored))
    if stored_reference != reference:
        raise SystemExit("persisted connection does not carry the requested config identity")
    print(f"connection {args.conn_id} bound to config {reference['sha256']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
