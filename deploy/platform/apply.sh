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
#            [--legacy-archive-receipt PATH]
#            [--socket DIR | --host HOST] [--port PORT] [--user USER]
#
# Default mode runs as platform_migrate, which owns only the platform schema; --bootstrap is
# the only step that runs with the installation identity and creates roles and schemas.  Every
# file is applied with ON_ERROR_STOP so a failed statement aborts the run.
# A legacy cutover is the one exception: it requires the one-time database administrator,
# an inherited lifecycle maintenance lock and an archive-receipt output path. The helper
# creates and independently restores the archive, freezes relation/catalog state, and applies
# schema/views/roles in one transaction before returning to the normal migration boundary.
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
LEGACY_ARCHIVE_RECEIPT=""
ARCHIVE_USER=""
LEGACY_ARCHIVE_SHA256=""
LEGACY_RECEIPT_SHA256=""
LEGACY_EVIDENCE_SHA256=""
LEGACY_ARCHIVE_CLUSTER_SYSTEM_IDENTIFIER=""
LEGACY_ARCHIVE_DATABASE=""
LEGACY_ARCHIVE_DATABASE_OID=""
LEGACY_ARCHIVE_PLATFORM_SCHEMA=""
LEGACY_ARCHIVE_PLATFORM_SCHEMA_OID=""
LEGACY_ARCHIVE_MIGRATION_ROLE=""
CONNECTION=()

usage() {
  echo "usage: apply.sh [--bootstrap | --airflow-grants] --psql PATH --database DB" >&2
  echo "                [--platform-schema NAME]" >&2
  echo "                [--airflow-schema NAME]" >&2
  echo "                [--legacy-archive-receipt PATH]" >&2
  echo "                [--archive-user USER]" >&2
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
    --legacy-archive-receipt)
      LEGACY_ARCHIVE_RECEIPT="${2:?}"
      shift 2
      ;;
    --archive-user) ARCHIVE_USER="${2:?}"; shift 2 ;;
    --socket) CONNECTION+=(-h "${2:?}"); shift 2 ;;
    --host) CONNECTION+=(-h "${2:?}"); shift 2 ;;
    --port) CONNECTION+=(-p "${2:?}"); shift 2 ;;
    --user) CONNECTION+=(-U "${2:?}"); shift 2 ;;
    *) usage ;;
  esac
done
[ -n "$DATABASE" ] || usage
if [ "$BOOTSTRAP" = "1" ] && [ "$AIRFLOW_GRANTS" = "1" ]; then usage; fi
if [ "$BOOTSTRAP" = "1" ] && [ -n "$LEGACY_ARCHIVE_RECEIPT" ]; then usage; fi
if [ "$AIRFLOW_GRANTS" = "1" ] && [ -n "$LEGACY_ARCHIVE_RECEIPT" ]; then usage; fi
if [ -n "$ARCHIVE_USER" ] && [ -z "$LEGACY_ARCHIVE_RECEIPT" ]; then usage; fi

run() {
  local file="$1"
  echo "platform apply: $(basename "$file")" >&2
  "$PSQL" "${CONNECTION[@]}" -d "$DATABASE" -v ON_ERROR_STOP=1 \
    -v platform_schema="$PLATFORM_SCHEMA" -v airflow_schema="$AIRFLOW_SCHEMA" \
    -v platform_database="$DATABASE" \
    -c "SELECT
          set_config('description.legacy_archive_sha256', '$LEGACY_ARCHIVE_SHA256', false),
          set_config('description.legacy_receipt_sha256', '$LEGACY_RECEIPT_SHA256', false),
          set_config('description.legacy_evidence_sha256', '$LEGACY_EVIDENCE_SHA256', false),
          set_config('description.legacy_archive_cluster_system_identifier', '$LEGACY_ARCHIVE_CLUSTER_SYSTEM_IDENTIFIER', false),
          set_config('description.legacy_archive_database', '$LEGACY_ARCHIVE_DATABASE', false),
          set_config('description.legacy_archive_database_oid', '$LEGACY_ARCHIVE_DATABASE_OID', false),
          set_config('description.legacy_archive_platform_schema', '$LEGACY_ARCHIVE_PLATFORM_SCHEMA', false),
          set_config('description.legacy_archive_platform_schema_oid', '$LEGACY_ARCHIVE_PLATFORM_SCHEMA_OID', false),
          set_config('description.legacy_archive_migration_role', '$LEGACY_ARCHIVE_MIGRATION_ROLE', false)" \
    -f "$file" >&2
}

if [ "$BOOTSTRAP" = "1" ]; then
  run "$HERE/bootstrap.sql"
elif [ "$AIRFLOW_GRANTS" = "1" ]; then
  run "$HERE/grants-airflow.sql"
else
  if [ -n "$LEGACY_ARCHIVE_RECEIPT" ]; then
    ARCHIVE_PYTHON="$(command -v "${PLATFORM_ARCHIVE_PYTHON:-python3}" || true)"
    PSQL="$(command -v "$PSQL" || true)"
    [ -x "$PSQL" ] || { echo "legacy archive psql is unavailable" >&2; exit 2; }
    PG_BIN="$(dirname "$PSQL")"
    PG_DUMP="${PLATFORM_PG_DUMP:-$PG_BIN/pg_dump}"
    PG_RESTORE="${PLATFORM_PG_RESTORE:-$PG_BIN/pg_restore}"
    PG_INITDB="${PLATFORM_PG_INITDB:-$PG_BIN/initdb}"
    PG_CTL="${PLATFORM_PG_CTL:-$PG_BIN/pg_ctl}"
    PG_CREATEDB="${PLATFORM_PG_CREATEDB:-$PG_BIN/createdb}"
    [ -x "$ARCHIVE_PYTHON" ] || { echo "legacy archive Python is unavailable" >&2; exit 2; }
    [ -x "$PG_DUMP" ] || { echo "legacy archive pg_dump is unavailable" >&2; exit 2; }
    [ -x "$PG_RESTORE" ] || { echo "legacy archive pg_restore is unavailable" >&2; exit 2; }
    [ -x "$PG_INITDB" ] || { echo "legacy archive initdb is unavailable" >&2; exit 2; }
    [ -x "$PG_CTL" ] || { echo "legacy archive pg_ctl is unavailable" >&2; exit 2; }
    [ -x "$PG_CREATEDB" ] || { echo "legacy archive createdb is unavailable" >&2; exit 2; }
    ARCHIVE_ARGS=(
      ensure
      --receipt "$LEGACY_ARCHIVE_RECEIPT"
      --psql "$PSQL"
      --pg-dump "$PG_DUMP"
      --pg-restore "$PG_RESTORE"
      --initdb "$PG_INITDB"
      --pg-ctl "$PG_CTL"
      --createdb "$PG_CREATEDB"
      --database "$DATABASE"
      --platform-schema "$PLATFORM_SCHEMA"
      --airflow-schema "$AIRFLOW_SCHEMA"
      --schema-file "$HERE/schema.sql"
      --views-file "$HERE/views.sql"
      --roles-file "$HERE/roles.sql"
    )
    for ((index = 0; index < ${#CONNECTION[@]}; index += 2)); do
      case "${CONNECTION[index]}" in
        -h) ARCHIVE_ARGS+=(--libpq "h=${CONNECTION[index + 1]}") ;;
        -p) ARCHIVE_ARGS+=(--libpq "p=${CONNECTION[index + 1]}") ;;
        -U)
          if [ -z "$ARCHIVE_USER" ]; then
            ARCHIVE_ARGS+=(--libpq "U=${CONNECTION[index + 1]}")
          fi
          ;;
        *) { echo "unsupported archive connection option" >&2; exit 2; } ;;
      esac
    done
    [ -z "$ARCHIVE_USER" ] || ARCHIVE_ARGS+=(--libpq "U=$ARCHIVE_USER")
    ARCHIVE_RESULT="$("$ARCHIVE_PYTHON" "$HERE/legacy_cutover.py" "${ARCHIVE_ARGS[@]}")"
    IFS=$'\t' read -r ARCHIVE_STATUS LEGACY_EVIDENCE_SHA256 LEGACY_ARCHIVE_SHA256 \
      LEGACY_ARCHIVE_DATABASE LEGACY_ARCHIVE_DATABASE_OID \
      LEGACY_ARCHIVE_CLUSTER_SYSTEM_IDENTIFIER \
      LEGACY_ARCHIVE_PLATFORM_SCHEMA LEGACY_ARCHIVE_PLATFORM_SCHEMA_OID \
      ARCHIVE_MANIFEST_SHA256 LEGACY_ARCHIVE_MIGRATION_ROLE LEGACY_RECEIPT_SHA256 \
      <<<"$ARCHIVE_RESULT"
    case "$ARCHIVE_STATUS" in
      no-legacy|created-cutover|verified-cutover) ;;
      *) { echo "legacy archive returned an invalid status" >&2; exit 2; } ;;
    esac
    echo "legacy archive: $ARCHIVE_STATUS" >&2
  fi
  if [ "${ARCHIVE_STATUS:-no-legacy}" = "no-legacy" ]; then
    run "$HERE/schema.sql"
    run "$HERE/views.sql"
    run "$HERE/roles.sql"
  fi
fi
echo "platform apply: complete" >&2
