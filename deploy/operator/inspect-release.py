"""Read-only operator inspection: portal/connection routing and active Airflow rows.

Environment (defaults encode the supported deployment):
  OPERATOR_ROOT (default /home/andy/operator), OPERATOR_STATE (default $OPERATOR_ROOT/state),
  POSTGRES_ROOT (default $OPERATOR_ROOT/airflow-pg), POSTGRES_MAJOR (default 14),
  POSTGRES_PORT (default: the shared deployment default).
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from render_operator import PORT_DEFAULTS

BASE = Path(os.environ.get("OPERATOR_ROOT", "/home/andy/operator"))
STATE = Path(os.environ.get("OPERATOR_STATE", str(BASE / "state")))
POSTGRES_ROOT = Path(os.environ.get("POSTGRES_ROOT", str(BASE / "airflow-pg")))
POSTGRES_MAJOR = os.environ.get("POSTGRES_MAJOR", "14")
POSTGRES_PORT = os.environ.get("POSTGRES_PORT") or str(PORT_DEFAULTS["POSTGRES_PORT"])
PSQL = POSTGRES_ROOT / "root/usr/lib/postgresql" / POSTGRES_MAJOR / "bin/psql"

config = json.loads((STATE / "portal.json").read_text(encoding="utf-8"))
print(
    json.dumps(
        {
            "airflow_url": config["airflow"]["url"],
            "portal_upload_root": config["portal"]["upload_root"],
            "store_root": config["pipeline"]["store_root"],
            "repositories": config["pipeline"]["repositories"],
            "targets": sorted(config["pipeline"].get("targets", {})),
            "discovery_keys": sorted(config["pipeline"].get("discovery", {})),
            "discovery_record_roots_count": len(config["pipeline"].get("discovery", {}).get("record_roots", [])),
            "frozen_names_configured": bool(config["pipeline"].get("discovery", {}).get("frozen_names_file")),
        },
        ensure_ascii=False,
    )
)
query = (
    "SELECT 'dag_run '||count(*) FROM dag_run WHERE state IN ('running','queued') UNION ALL "
    "SELECT 'task_instance '||count(*) FROM task_instance WHERE state IN "
    "('running','queued','scheduled','up_for_retry','up_for_reschedule','restarting','deferred');"
)
subprocess.run(
    [
        str(PSQL),
        "-h",
        str(POSTGRES_ROOT / "socket"),
        "-p",
        POSTGRES_PORT,
        "-U",
        "solidworks",
        "-d",
        "airflow_meta",
        "-At",
        "-c",
        query,
    ],
    check=True,
)
