"""Create/update the endpoint connection from a token file (no token in argv).

The connection's ``extra`` carries the platform-owned ``handoff_roots`` allowlist for the shared
Linux client; the deployment installer passes the single dedicated intake directory.
"""

from __future__ import annotations

import argparse
import json
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
    parser.add_argument("--handoff-root", action="append", default=[],
                        help="absolute allowed Linux CAD source directory (repeatable)")
    args = parser.parse_args()
    for root in args.handoff_root:
        if not root.startswith("/") or root == "/" or any(part in {"..", ""} for part in Path(root).parts[1:]):
            raise SystemExit(f"handoff root must be a dedicated absolute directory: {root}")
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
        if args.handoff_root:
            existing.extra = json.dumps({"handoff_roots": [str(Path(root)) for root in args.handoff_root]})
        session.commit()
    os.chmod(Path(args.token_file), 0o600)
    print(f"connection {args.conn_id} updated from token file")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
