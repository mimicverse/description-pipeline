#!/usr/bin/env bash
# Apply the platform schema, the read models and the consumer grants (idempotent).
#   --airflow-grants mode: the airflow-side grants (one owner of the boundary); run after
#                 `airflow db migrate` and before the default platform apply.
#   default mode: run as the platform migration identity (platform_migrate) after the Airflow
#                 migration; creates/updates the platform objects and the consumer grants.
#   --bootstrap:  the ONLY cluster-identity step; creates the roles, the two schemas and the
#                 per-role search_path (run by installer before the Airflow migration).
#
# Usage:
#   apply.sh [--bootstrap | --airflow-grants] --psql PATH --database DB
#            [--platform-schema NAME] [--airflow-schema NAME]
#            [--socket DIR | --host HOST] [--port PORT] [--user USER]
#
# Default mode runs as platform_migrate, which owns only the platform schema; --bootstrap is
# the only step that runs with the installation identity and creates roles and schemas.  Every
# file is applied with ON_ERROR_STOP so a failed statement aborts the run.
# The schema names are variables: production keeps the defaults (`platform` / `airflow`), while
# an isolated environment (CI) can bind its own `platform_ci_<run>` / `airflow_ci_<run>` pair.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PSQL="${PLATFORM_PSQL:-psql}"
DATABASE=""
PLATFORM_SCHEMA="platform"
AIRFLOW_SCHEMA="airflow"
BOOTSTRAP=0
AIRFLOW_GRANTS=0
CONNECTION=()

usage() {
  echo "usage: apply.sh [--bootstrap | --airflow-grants] --psql PATH --database DB" >&2
  echo "                [--platform-schema NAME]" >&2
  echo "                [--airflow-schema NAME]" >&2
  echo "                [--socket DIR | --host HOST] [--port PORT] [--user USER]" >&2
  exit 2
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --bootstrap) BOOTSTRAP=1; shift ;;
    --airflow-grants) AIRFLOW_GRANTS=1; shift ;;
    --psql) PSQL="${2:?}"; shift 2 ;;
    --database) DATABASE="${2:?}"; shift 2 ;;
    --platform-schema) PLATFORM_SCHEMA="${2:?}"; shift 2 ;;
    --airflow-schema) AIRFLOW_SCHEMA="${2:?}"; shift 2 ;;
    --socket) CONNECTION+=(-h "${2:?}"); shift 2 ;;
    --host) CONNECTION+=(-h "${2:?}"); shift 2 ;;
    --port) CONNECTION+=(-p "${2:?}"); shift 2 ;;
    --user) CONNECTION+=(-U "${2:?}"); shift 2 ;;
    *) usage ;;
  esac
done
[ -n "$DATABASE" ] || usage
if [ "$BOOTSTRAP" = "1" ] && [ "$AIRFLOW_GRANTS" = "1" ]; then usage; fi

run() {
  local file="$1"
  echo "platform apply: $(basename "$file")" >&2
  "$PSQL" "${CONNECTION[@]}" -d "$DATABASE" -v ON_ERROR_STOP=1 \
    -v platform_schema="$PLATFORM_SCHEMA" -v airflow_schema="$AIRFLOW_SCHEMA" \
    -v platform_database="$DATABASE" -f "$file" >&2
}

if [ "$BOOTSTRAP" = "1" ]; then
  run "$HERE/bootstrap.sql"
elif [ "$AIRFLOW_GRANTS" = "1" ]; then
  run "$HERE/grants-airflow.sql"
else
  run "$HERE/schema.sql"
  run "$HERE/views.sql"
  run "$HERE/roles.sql"
fi
echo "platform apply: complete" >&2
