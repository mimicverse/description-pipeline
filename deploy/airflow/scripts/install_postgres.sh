#!/usr/bin/env bash
# Portable, sudo-free PostgreSQL for Airflow's LocalExecutor: distro packages extracted under
# POSTGRES_ROOT. The cluster listens only on a private UNIX socket (directory mode 0700) owned by
# this user; pg_hba allows local socket trust and rejects every TCP connection. Rerunning is safe
# and re-hardens a cluster created by an older version of this script.
set -euo pipefail
: "${POSTGRES_ROOT:?set POSTGRES_ROOT to the isolated deployment directory}"
WORK="$POSTGRES_ROOT"
BIN="$WORK/root/usr/lib/postgresql/18/bin"
export LD_LIBRARY_PATH="$WORK/root/usr/lib/x86_64-linux-gnu:${LD_LIBRARY_PATH:-}"
SOCKET="$WORK/socket"
START_OPTS=(-D "$WORK/data" -o "-k $SOCKET -p 5433 -c listen_addresses=''")

mkdir -p "$WORK/debs" "$WORK/root" "$WORK/data" "$SOCKET"
chmod 700 "$WORK/data" "$SOCKET"
cd "$WORK/debs"
apt-get download postgresql-18 postgresql-client-18 libpq5
for deb in ./*.deb; do dpkg-deb -x "$deb" "$WORK/root"; done
"$BIN/postgres" --version
[ "$(ldd "$BIN/postgres" | grep -c 'not found' || true)" = "0" ] || { echo "missing libraries" >&2; exit 1; }

if [ ! -f "$WORK/data/PG_VERSION" ]; then
  "$BIN/initdb" -D "$WORK/data" -U solidworks \
    --auth-local=trust --auth-host=reject --encoding=UTF8 >/dev/null
fi

# Deterministic, managed cluster settings: no TCP listener, private socket, fixed port.
CONF="$WORK/data/postgresql.conf"
awk -v s="# >>> description-postgres" -v e="# <<< description-postgres" '
  $0 == s { skip = 1 } $0 == e { skip = 0; next } !skip { print }' "$CONF" > "$CONF.tmp"
mv "$CONF.tmp" "$CONF"
cat >> "$CONF" <<EOF
# >>> description-postgres
listen_addresses = ''
unix_socket_directories = '$SOCKET'
port = 5433
# <<< description-postgres
EOF

# Only the private socket may authenticate, and only by local trust; TCP is rejected outright.
HBA="$WORK/data/pg_hba.conf"
cat > "$HBA.tmp" <<EOF
# >>> description-postgres
local   all             all                                     trust
local   replication     all                                     trust
host    all             all             0.0.0.0/0               reject
host    all             all             ::/0                    reject
# <<< description-postgres
EOF
mv "$HBA.tmp" "$HBA"
chmod 600 "$CONF" "$HBA"

if "$BIN/pg_ctl" -D "$WORK/data" status >/dev/null 2>&1; then
  "$BIN/pg_ctl" "${START_OPTS[@]}" -l "$WORK/postgres.log" -w restart
else
  "$BIN/pg_ctl" "${START_OPTS[@]}" -l "$WORK/postgres.log" -w start
fi
"$BIN/createdb" -h "$SOCKET" -p 5433 -U solidworks airflow_meta 2>/dev/null || true
"$BIN/psql" -h "$SOCKET" -p 5433 -U solidworks -d airflow_meta -tAc "select version();"
echo "AIRFLOW_DB_URL=postgresql+psycopg2://solidworks@/airflow_meta?host=$SOCKET&port=5433"
