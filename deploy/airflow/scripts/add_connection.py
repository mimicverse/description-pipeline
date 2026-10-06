"""Create/update the endpoint connection from a token file (no token in argv)."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from airflow.models.connection import Connection
from airflow.settings import Session


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--conn-id", default="solidworks_windows")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18765)
    parser.add_argument("--token-file", required=True)
    args = parser.parse_args()
    token = Path(args.token_file).read_text(encoding="utf-8").strip()
    if not token:
        raise SystemExit("token file is empty")
    with Session() as session:
        existing = session.query(Connection).filter(Connection.conn_id == args.conn_id).one_or_none()
        if existing is None:
            existing = Connection(conn_id=args.conn_id)
            session.add(existing)
        existing.conn_type = "http"
        existing.host = args.host
        existing.port = args.port
        existing.password = token
        session.commit()
    os.chmod(Path(args.token_file), 0o600)
    print(f"connection {args.conn_id} updated from token file")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
