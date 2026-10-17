"""Capture or restore one private Airflow Connection row for cross-generation rollback.

The capture is bound to the promotion receipt's immutable source/target identities and, at
restore time, to the original sealed backup manifest.  A capture is never overwritten, and all
JSON is read through the strict duplicate-key-rejecting reader.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
from pathlib import Path

from airflow.models.connection import Connection
from airflow.settings import Session

import backup
import promotion
from description_pipeline.io import file_digest, read_data

FIELDS = ("conn_type", "description", "host", "login", "password", "schema", "port", "extra")
SCHEMA = "solidworks-to-urdf.connection-snapshot/v1"
BINDING_FIELDS = ("install_id", "from", "from_sha256", "to", "to_sha256")


def _require_regular(path: Path, label: str) -> None:
    if path.is_symlink() or not path.is_file():
        raise SystemExit(f"{label} must be a regular non-link file: {path}")


def _strict_document(path: Path, label: str) -> dict:
    _require_regular(path, label)
    document = read_data(path)
    if not isinstance(document, dict):
        raise SystemExit(f"{label} is not a JSON object: {path}")
    return document


def _receipt_binding(receipt: Path) -> dict:
    document = _strict_document(receipt, "promotion receipt")
    if document.get("schema_version") != promotion.RECEIPT_SCHEMA:
        raise SystemExit("the promotion receipt schema is not supported")
    binding = {name: document.get(name) for name in BINDING_FIELDS}
    if not all(isinstance(value, str) and value for value in binding.values()):
        raise SystemExit("the promotion receipt carries no complete immutable binding")
    for name in ("from_sha256", "to_sha256"):
        if len(binding[name]) != 64 or any(character not in "0123456789abcdef" for character in binding[name]):
            raise SystemExit(f"the promotion receipt {name} is malformed")
    for name in ("from", "to"):
        source = Path(binding[name])
        if not source.is_file() or file_digest(source) != binding[f"{name}_sha256"]:
            raise SystemExit(f"the promotion receipt {name} binding does not match its immutable file")
    return binding


def _row(conn_id: str, session) -> Connection:
    row = session.query(Connection).filter(Connection.conn_id == conn_id).one_or_none()
    if row is None:
        raise SystemExit(f"connection is missing: {conn_id}")
    return row


def _capture(conn_id: str, output: Path, receipt: Path) -> int:
    if output.is_symlink():
        raise SystemExit("connection snapshot output may not be a symlink")
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.parent.is_symlink():
        raise SystemExit("connection snapshot directory may not be a symlink")
    os.chmod(output.parent, 0o700)
    binding = _receipt_binding(receipt)
    with Session() as session:
        row = _row(conn_id, session)
        payload = {
            "schema_version": SCHEMA,
            "conn_id": conn_id,
            "fields": {name: getattr(row, name) for name in FIELDS},
            "promotion": binding,
        }
    descriptor = os.open(output, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        os.fchmod(stream.fileno(), 0o600)
        json.dump(payload, stream, sort_keys=True, separators=(",", ":"))
        stream.write("\n")
    print("connection row captured privately")
    return 0


def _load(path: Path, conn_id: str) -> dict:
    if path.is_symlink() or not path.is_file():
        raise SystemExit("connection snapshot must be a regular non-link file")
    if stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise SystemExit("connection snapshot must have mode 0600")
    document = read_data(path)
    if not isinstance(document, dict) or set(document) != {
        "schema_version",
        "conn_id",
        "fields",
        "promotion",
    }:
        raise SystemExit("connection snapshot has an unexpected shape")
    if document["schema_version"] != SCHEMA or document["conn_id"] != conn_id:
        raise SystemExit("connection snapshot names another connection or schema")
    fields = document["fields"]
    binding = document["promotion"]
    if not isinstance(fields, dict) or set(fields) != set(FIELDS):
        raise SystemExit("connection snapshot does not carry the complete field set")
    if not isinstance(binding, dict) or set(binding) != set(BINDING_FIELDS):
        raise SystemExit("connection snapshot carries no complete promotion binding")
    if not all(isinstance(value, str) and value for value in binding.values()):
        raise SystemExit("connection snapshot promotion binding is malformed")
    return document


def _verify_binding(document: dict, receipt: Path) -> None:
    if document["promotion"] != _receipt_binding(receipt):
        raise SystemExit("connection snapshot belongs to another promotion generation")


def _verify_backup(snapshot: Path, manifest_path: Path, sealed: Path, member: str) -> None:
    manifest = backup.load_manifest(manifest_path)
    encrypted = manifest["encrypted"]
    if str(sealed) != encrypted["path"]:
        raise SystemExit("the backup manifest binds another sealed archive")
    if not sealed.is_file() or file_digest(sealed) != encrypted["sha256"]:
        raise SystemExit("the sealed backup archive does not match its manifest")
    expected = manifest["files"].get(member)
    if expected is None:
        raise SystemExit("the backup manifest does not bind the connection capture")
    if file_digest(snapshot) != expected:
        raise SystemExit("the connection capture does not match the original backup manifest")


def _verify_fields(row: Connection, expected: dict) -> None:
    actual = {name: getattr(row, name) for name in FIELDS}
    if actual != expected:
        raise SystemExit("connection row differs from the captured fields")


def _assign(row: Connection, name: str, value) -> None:
    if name == "password":
        # Airflow's encrypted password setter ignores falsy values; the physical column
        # must still be cleared when the captured row had no password.
        if value:
            row.password = value
        else:
            row._password = value
            row.is_encrypted = False
            row.password = value
        return
    setattr(row, name, value)


def _restore_or_verify(
    conn_id: str,
    path: Path,
    *,
    restore: bool,
    receipt: Path,
    manifest: Path,
    sealed: Path,
    member: str,
) -> int:
    document = _load(path, conn_id)
    _verify_binding(document, receipt)
    _verify_backup(path, manifest, sealed, member)
    expected = document["fields"]
    with Session() as session:
        row = session.query(Connection).filter(Connection.conn_id == conn_id).one_or_none()
        if restore and (row is None or {name: getattr(row, name) for name in FIELDS} != expected):
            if row is None:
                row = Connection(conn_id=conn_id)
                session.add(row)
            for name in FIELDS:
                _assign(row, name, expected[name])
            session.commit()
            _verify_fields(_row(conn_id, session), expected)
            print("connection row restored exactly")
            return 0
        if row is None:
            raise SystemExit(f"connection is missing: {conn_id}")
        _verify_fields(row, expected)
    if restore:
        print("connection row already matched the capture; nothing was written")
    else:
        print("connection row matches the capture")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="connection_snapshot.py")
    parser.add_argument("action", choices=("capture", "restore", "verify"))
    parser.add_argument("--conn-id", default="solidworks_windows")
    parser.add_argument("--file", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--sealed", type=Path)
    parser.add_argument("--member", default="private/connection-before-promotion.json")
    args = parser.parse_args()
    if args.action == "capture":
        return _capture(args.conn_id, args.file, args.receipt)
    if args.manifest is None or args.sealed is None:
        raise SystemExit("restore and verify require --manifest and --sealed")
    return _restore_or_verify(
        args.conn_id,
        args.file,
        restore=args.action == "restore",
        receipt=args.receipt,
        manifest=args.manifest,
        sealed=args.sealed,
        member=args.member,
    )


if __name__ == "__main__":
    raise SystemExit(main())
