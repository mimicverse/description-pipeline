#!/usr/bin/env bash
set -euo pipefail
# Stop ingress before the drain check, retaining PostgreSQL and Airflow for inspection.
OPERATOR_STATE="${OPERATOR_STATE:-/home/andy/operator/state}"
source "$OPERATOR_STATE/resolved.env"
systemctl --user stop description-operator-proxy.service description-portal.service
AIRFLOW_HOME="$AIRFLOW_HOME" "$AIRFLOW_VENV/bin/airflow" dags pause solidworks_to_urdf >/dev/null
python3 "${INSPECT_RELEASE_PY:-/home/andy/operator/inspect-release.py}"
