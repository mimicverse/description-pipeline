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


def _json_safe(value):
    """The serialization boundary for the connection ``extra`` document.

    The endpoint config validates its routing into ``Path`` values (``store_root``,
    ``repositories``, ``discovery.record_roots`` and the optional frozen-name registry), so
    every value that lands in ``extra`` is written as its string form - a ``Path`` (or a
    tuple/list/dict carrying one) is never handed to ``json.dumps``.  Record roots and the
    registry are kept, only re-encoded: deleting them to serialize is not allowed.
    """

    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    return value


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--conn-id", default="solidworks_windows")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True, help="Linux loopback port of the endpoint tunnel")
    parser.add_argument("--token-file", required=True)
    parser.add_argument(
        "--pipeline-config", required=True, help="rendered portal config containing Linux execution routing"
    )
    parser.add_argument(
        "--handoff-root", action="append", default=[], help="absolute allowed Linux CAD source directory (repeatable)"
    )
    args = parser.parse_args()
    for root in args.handoff_root:
        if not root.startswith("/") or root == "/" or any(part in {"..", ""} for part in Path(root).parts[1:]):
            raise SystemExit(f"handoff root must be a dedicated absolute directory: {root}")
    token = Path(args.token_file).read_text(encoding="utf-8").strip()
    if not token:
        raise SystemExit("token file is empty")
    from description_pipeline.orchestration.airflow_client import EndpointConfig

    pipeline = json.loads(Path(args.pipeline_config).read_text(encoding="utf-8"))["pipeline"]
    config = EndpointConfig(
        base_url=f"http://{args.host}:{args.port}",
        token=token,
        store_root=pipeline["store_root"],
        repositories=pipeline["repositories"],
        discovery=pipeline.get("discovery", {}),
        targets=pipeline.get("targets", {}),
    )
    with Session() as session:
        existing = session.query(Connection).filter(Connection.conn_id == args.conn_id).one_or_none()
        if existing is None:
            existing = Connection(conn_id=args.conn_id)
            session.add(existing)
        existing.conn_type = "http"
        existing.host = args.host
        existing.port = args.port
        existing.password = token
        existing.extra = json.dumps(
            _json_safe(
                {
                "handoff_roots": [str(Path(root)) for root in args.handoff_root],
                "store_root": str(config.store_root),
                "repositories": {slug: str(path) for slug, path in config.repositories.items()},
                "discovery": config.discovery,
                "targets": config.targets,
                }
            )
        )
        session.commit()
    os.chmod(Path(args.token_file), 0o600)
    print(f"connection {args.conn_id} updated from token file")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
