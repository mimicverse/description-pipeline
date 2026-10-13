#!/usr/bin/env bash
# Apply the platform schema, the read models and the role boundary (idempotent).
#
# Usage:
#   apply.sh --psql PATH --database DB [--airflow-schema NAME]
#            [--socket DIR | --host HOST] [--port PORT] [--user USER]
#
# The caller is the maintenance role that may create the platform schema, the roles and the
# grants (and that can read the Airflow metadata schema).  Every file is applied with
# ON_ERROR_STOP so a failed statement aborts the run.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PSQL="${PLATFORM_PSQL:-psql}"
DATABASE=""
AIRFLOW_SCHEMA="airflow"
CONNECTION=()

usage() {
  echo "usage: apply.sh --psql PATH --database DB [--airflow-schema NAME]" >&2
  echo "                [--socket DIR | --host HOST] [--port PORT] [--user USER]" >&2
  exit 2
}

while [ "$#" -gt 0 ]; do
  case "$1" in
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
    -v airflow_schema="$AIRFLOW_SCHEMA" -f "$file" >&2
}

run "$HERE/schema.sql"
run "$HERE/views.sql"
run "$HERE/roles.sql"
echo "platform apply: complete" >&2
