#!/usr/bin/env bash
# Apply the platform schema, the read models and the consumer grants (idempotent).
#   default mode: run as the platform migration identity (platform_migrate) after the Airflow
#                 migration; creates/updates the platform objects and the consumer grants.
#   --bootstrap:  the ONLY cluster-identity step; creates the roles, the two schemas and the
#                 per-role search_path (run by installer before the Airflow migration).
#
# Usage:
#   apply.sh [--bootstrap] --psql PATH --database DB [--airflow-schema NAME]
#            [--socket DIR | --host HOST] [--port PORT] [--user USER]
#
# Default mode runs as platform_migrate, which owns only the platform schema; --bootstrap is
# the only step that runs with the installation identity and creates roles and schemas.  Every
# file is applied with ON_ERROR_STOP so a failed statement aborts the run.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PSQL="${PLATFORM_PSQL:-psql}"
DATABASE=""
AIRFLOW_SCHEMA="airflow"
BOOTSTRAP=0
CONNECTION=()

usage() {
  echo "usage: apply.sh [--bootstrap] --psql PATH --database DB [--airflow-schema NAME]" >&2
  echo "                [--socket DIR | --host HOST] [--port PORT] [--user USER]" >&2
  exit 2
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --bootstrap) BOOTSTRAP=1; shift ;;
    --psql) PSQL="${2:?}"; shift 2 ;;
    --database) DATABASE="${2:?}"; shift 2 ;;
    --airflow-schema) AIRFLOW_SCHEMA="${2:?}"; shift 2 ;;
    --socket) CONNECTION+=(-h "${2:?}"); shift 2 ;;
    --host) CONNECTION+=(-h "${2:?}"); shift 2 ;;
    --port) CONNECTION+=(-p "${2:?}"); shift 2 ;;
    --user) CONNECTION+=(-U "${2:?}"); shift 2 ;;
    *) usage ;;
  esac
done
[ -n "$DATABASE" ] || usage

run() {
  local file="$1"
  echo "platform apply: $(basename "$file")" >&2
  "$PSQL" "${CONNECTION[@]}" -d "$DATABASE" -v ON_ERROR_STOP=1 \
    -v airflow_schema="$AIRFLOW_SCHEMA" -v platform_database="$DATABASE" -f "$file" >&2
}

if [ "$BOOTSTRAP" = "1" ]; then
  run "$HERE/bootstrap.sql"
else
  run "$HERE/schema.sql"
  run "$HERE/views.sql"
  run "$HERE/roles.sql"
fi
echo "platform apply: complete" >&2
