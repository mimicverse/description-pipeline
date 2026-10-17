#!/usr/bin/env bash
# Isolated real-PostgreSQL check for the promotion stop -> migration ordering.
# Run as non-root with a postgres bin directory (initdb/pg_ctl/psql/pg_isready) in PG_BIN_DIR;
# a throwaway cluster is created under a temp dir and stopped on exit. Set AIRFLOW_VENV to the
# real Airflow venv to also run the genuine `airflow db migrate`; SERVICES_SH overrides the
# services.sh under test.
set -euo pipefail
: "${PG_BIN_DIR:?set PG_BIN_DIR to a directory with initdb/pg_ctl/psql/pg_isready}"
BIN="$PG_BIN_DIR"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICES_SH="${SERVICES_SH:-$(cd "$HERE/../.." && pwd)/deploy/airflow/services.sh}"
PORT="${PROMO_DB_CHECK_PORT:-55432}"
T="$(mktemp -d /tmp/promo-db-dep-XXXX)"
SOCK="$T/socket"; mkdir -p "$SOCK"
cleanup() { "$BIN/pg_ctl" -D "$T/data" -m fast -w stop >/dev/null 2>&1 || true; }
trap cleanup EXIT

"$BIN/initdb" -D "$T/data" -U solidworks --auth-local=trust --auth-host=reject >/dev/null
"$BIN/pg_ctl" -D "$T/data" -o "-k $SOCK -p $PORT -c listen_addresses=''" -w start >/dev/null
probe() { "$BIN/psql" -h "$SOCK" -p "$PORT" -U solidworks -d "${2:-postgres}" -X -v ON_ERROR_STOP=1 -c "$1"; }

"$BIN/pg_isready" -h "$SOCK" -p "$PORT" >/dev/null
probe "CREATE TABLE IF NOT EXISTS promotion_migration_probe(id integer)" >/dev/null
echo "REALPG: migration probe ok while the postgres server runs"
if [ -n "${AIRFLOW_VENV:-}" ] && [ -x "$AIRFLOW_VENV/bin/airflow" ]; then
  probe "CREATE DATABASE airflow_meta" postgres >/dev/null 2>&1 || true
  migrate() {
    AIRFLOW_HOME="$T/airflow" \
    AIRFLOW__DATABASE__SQL_ALCHEMY_CONN="postgresql+psycopg2://solidworks@/airflow_meta?host=$SOCK&port=$PORT" \
      "$AIRFLOW_VENV/bin/airflow" db migrate
  }
  migrate > "$T/migrate-up.out" 2>&1
  echo "REALPG: genuine airflow db migrate ok while the server runs"
fi

"$BIN/pg_ctl" -D "$T/data" -m fast -w stop >/dev/null
if [ -n "${AIRFLOW_VENV:-}" ] && [ -x "$AIRFLOW_VENV/bin/airflow" ]; then
  if migrate >"$T/migrate-gone.out" 2>"$T/migrate-gone.err"; then
    echo "REALPG: unexpected migration success after stop"; exit 1
  fi
  grep -q "psycopg2.OperationalError" "$T/migrate-gone.err"
  grep -qE "\.s\.PGSQL\.|No such file or directory" "$T/migrate-gone.err"
  echo "REALPG: the genuine migration fails once the server is stopped (psycopg2 socket error)"
fi
if probe "SELECT 1" >"$T/gone.out" 2>"$T/gone.err"; then
  echo "REALPG: unexpected success after stop"; exit 1
fi
grep -qE "No such file or directory|could not connect" "$T/gone.err"
echo "REALPG: migration probe fails once the server is stopped (the socket dependency is real)"

"$BIN/pg_ctl" -D "$T/data" -o "-k $SOCK -p $PORT -c listen_addresses=''" -w start >/dev/null
SHIM="$T/shim"; mkdir -p "$SHIM" "$T/unit-state"
cat > "$SHIM/systemctl" <<'STUB'
#!/usr/bin/env bash
mkdir -p "$UNIT_STATE_DIR"
op=""; units=()
for a in "$@"; do
  case "$a" in
    --quiet) ;;
    start|stop|enable|disable|is-active) op="$a" ;;
    -*) ;;
    *) units+=("${a%.service}") ;;
  esac
done
case "$op" in
  start|enable) for u in "${units[@]}"; do printf active > "$UNIT_STATE_DIR/$u"; done ;;
  stop|disable) for u in "${units[@]}"; do printf inactive > "$UNIT_STATE_DIR/$u"; done ;;
  is-active) rc=0; for u in "${units[@]}"; do
      s="$(cat "$UNIT_STATE_DIR/$u" 2>/dev/null || echo inactive)"
      echo "$s"
      [ "$s" = active ] || rc=3
    done; exit "$rc" ;;
  *) exit 0 ;;
esac
STUB
chmod +x "$SHIM/systemctl"
# Pre-mark the shared database as active: any accidental postgres stop becomes visible here.
printf active > "$T/unit-state/description-postgres"
PATH="$SHIM:$PATH" UNIT_STATE_DIR="$T/unit-state" \
  AIRFLOW_VENV="$T/venv" AIRFLOW_HOME="$T/airflow" \
  POSTGRES_ROOT="${POSTGRES_ROOT:-}" POSTGRES_MAJOR="${POSTGRES_MAJOR:-18}" \
  SOLIDWORKS_SSH_HOST="${SOLIDWORKS_SSH_HOST:-windows-m3}" \
  AIRFLOW_API_PORT=8791 POSTGRES_PORT=5433 TUNNEL_LOOPBACK_PORT=18765 SOLIDWORKS_ENDPOINT_PORT=8765 \
  bash "$SERVICES_SH" stop-executors > "$T/stop.out" 2>&1
cat "$T/stop.out"
[ "$(cat "$T/unit-state/description-postgres")" = active ]
grep -q "description-airflow-scheduler stopped" "$T/stop.out"
"$BIN/pg_isready" -h "$SOCK" -p "$PORT" >/dev/null
probe "SELECT 1" >/dev/null
if [ -n "${AIRFLOW_VENV:-}" ] && [ -x "$AIRFLOW_VENV/bin/airflow" ]; then
  migrate > "$T/migrate-kept.out" 2>&1
  echo "REALPG: the genuine migration still succeeds after services.sh stop-executors"
fi
echo "REALPG: services.sh stop-executors left the real database reachable; the migration probe still succeeds"
echo "REALPG-OK"
