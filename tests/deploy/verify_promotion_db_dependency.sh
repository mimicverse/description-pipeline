#!/usr/bin/env bash
# Isolated real-PostgreSQL lifecycle check for the promotion stop -> migration ordering.
#
# Boundary (labelled honestly): systemd is mocked by a stateful shim (unit state files), but
# PostgreSQL is a REAL throwaway cluster. The shim maps the description-postgres unit's
# stop/start to pg_ctl on that cluster, so the real full-stop helper (services.sh stop)
# genuinely removes database availability and the genuine `airflow db migrate` then fails with
# the production socket error. The positive control then proves the promotion's stop set
# (services.sh stop-executors) leaves the same real cluster ready for the migration.
#
# Requirements: non-root; a configured POSTGRES_ROOT with the operator layout
# (<root>/root/usr/lib/postgresql/<major>/bin), POSTGRES_MAJOR, and AIRFLOW_VENV with a real
# Airflow install. PG_BIN_DIR may override the derived bin directory.
set -euo pipefail
: "${POSTGRES_ROOT:?set POSTGRES_ROOT to the configured operator postgres root}"
: "${POSTGRES_MAJOR:?set POSTGRES_MAJOR (for example 14 or 18)}"
: "${AIRFLOW_VENV:?set AIRFLOW_VENV to the real Airflow venv}"
BIN="${PG_BIN_DIR:-$POSTGRES_ROOT/root/usr/lib/postgresql/$POSTGRES_MAJOR/bin}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICES_SH="${SERVICES_SH:-$(cd "$HERE/../.." && pwd)/deploy/airflow/services.sh}"
PORT="${PROMO_DB_CHECK_PORT:-55432}"
T="$(mktemp -d /tmp/promo-db-dep-XXXX)"
SOCK="$T/socket"; mkdir -p "$SOCK"
cleanup() { "$BIN/pg_ctl" -D "$T/data" -m fast -w stop >/dev/null 2>&1 || true; }
trap cleanup EXIT
start_cluster() { "$BIN/pg_ctl" -D "$T/data" -o "-k $SOCK -p $PORT -c listen_addresses=''" -w start >/dev/null; }
migrate() {
  AIRFLOW_HOME="$T/airflow" \
  AIRFLOW__DATABASE__SQL_ALCHEMY_CONN="postgresql+psycopg2://solidworks@/airflow_meta?host=$SOCK&port=$PORT" \
    "$AIRFLOW_VENV/bin/airflow" db migrate
}

"$BIN/initdb" -D "$T/data" -U solidworks --auth-local=trust --auth-host=reject >/dev/null
start_cluster
"$BIN/psql" -h "$SOCK" -p "$PORT" -U solidworks -d postgres -X -v ON_ERROR_STOP=1 \
  -c "CREATE DATABASE airflow_meta" >/dev/null
migrate > "$T/migrate-up.out" 2>&1
echo "REALPG: genuine airflow db migrate ok on the running real cluster"

SHIM="$T/shim"; mkdir -p "$SHIM" "$T/unit-state"
cat > "$SHIM/systemctl" <<'STUB'
#!/usr/bin/env bash
# Stateful systemd shim. Unit state is real; description-postgres additionally maps to the
# throwaway cluster so the full-stop helper actually stops the database (the production
# dependency). Executors are recorded only (nothing else is real here).
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
  start|enable|stop|disable)
    for u in "${units[@]}"; do
      if [ "$u" = "description-postgres" ]; then
        if [ "$op" = start ] || [ "$op" = enable ]; then
          "$PG_CTL" -D "$CLUSTER_DATA" -o "-k $CLUSTER_SOCK -p $CLUSTER_PORT -c listen_addresses=''" -w start >/dev/null 2>&1 || true
        else
          "$PG_CTL" -D "$CLUSTER_DATA" -m fast -w stop >/dev/null 2>&1 || true
        fi
      fi
      if [ "$op" = start ] || [ "$op" = enable ]; then
        printf active > "$UNIT_STATE_DIR/$u"
      else
        printf inactive > "$UNIT_STATE_DIR/$u"
      fi
    done ;;
  is-active)
    rc=0
    for u in "${units[@]}"; do
      s="$(cat "$UNIT_STATE_DIR/$u" 2>/dev/null || echo inactive)"
      echo "$s"
      [ "$s" = active ] || rc=3
    done
    exit "$rc" ;;
  *) exit 0 ;;
esac
STUB
chmod +x "$SHIM/systemctl"
run_services() {
  PATH="$SHIM:$PATH" UNIT_STATE_DIR="$T/unit-state" \
    PG_CTL="$BIN/pg_ctl" CLUSTER_DATA="$T/data" CLUSTER_SOCK="$SOCK" CLUSTER_PORT="$PORT" \
    AIRFLOW_VENV="$AIRFLOW_VENV" AIRFLOW_HOME="$T/airflow" \
    POSTGRES_ROOT="$POSTGRES_ROOT" POSTGRES_MAJOR="$POSTGRES_MAJOR" \
    SOLIDWORKS_SSH_HOST="${SOLIDWORKS_SSH_HOST:-windows-m3}" \
    AIRFLOW_API_PORT=8791 POSTGRES_PORT=5433 TUNNEL_LOOPBACK_PORT=18765 SOLIDWORKS_ENDPOINT_PORT=8765 \
    bash "$SERVICES_SH" "$1"
}

# Seed the unit state as the installed system: database and executors active.
for u in description-postgres description-solidworks-tunnel description-airflow-dag-processor \
  description-airflow-scheduler description-airflow-api-server; do
  printf active > "$T/unit-state/$u"
done

echo "== negative control: the real full stop removes database availability (mocked systemd boundary)"
run_services stop > "$T/full-stop.out" 2>&1 || { cat "$T/full-stop.out"; exit 1; }
[ "$(cat "$T/unit-state/description-postgres")" = inactive ]
if migrate > "$T/migrate-gone.out" 2>"$T/migrate-gone.err"; then
  echo "REALPG: unexpected migration success after the real full stop"; exit 1
fi
grep -q "psycopg2.OperationalError" "$T/migrate-gone.err"
grep -qE "\.s\.PGSQL\.|No such file or directory" "$T/migrate-gone.err"
echo "REALPG: after the real full stop the genuine migration fails with the production socket error"

echo "== restore the throwaway cluster; positive control: stop-executors keeps it ready"
start_cluster
printf active > "$T/unit-state/description-postgres"
migrate >/dev/null 2>&1
run_services stop-executors > "$T/stop-executors.out" 2>&1 || { cat "$T/stop-executors.out"; exit 1; }
cat "$T/stop-executors.out"
[ "$(cat "$T/unit-state/description-postgres")" = active ]
grep -q "description-airflow-scheduler stopped" "$T/stop-executors.out"
"$BIN/pg_isready" -h "$SOCK" -p "$PORT" >/dev/null
migrate > "$T/migrate-kept.out" 2>&1
echo "REALPG: genuine migration still succeeds after services.sh stop-executors"
echo "REALPG-OK"
