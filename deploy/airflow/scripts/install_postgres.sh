#!/usr/bin/env bash
# Portable, sudo-free PostgreSQL for Airflow's LocalExecutor: distro packages extracted under
# POSTGRES_ROOT. The cluster listens only on a private UNIX socket (directory mode 0700) owned by
# this user; pg_hba allows local socket trust and rejects every TCP connection. Rerunning
# preserves the cluster and reapplies its managed configuration.
set -euo pipefail
: "${POSTGRES_ROOT:?set POSTGRES_ROOT to the isolated deployment directory}"
WORK="$POSTGRES_ROOT"
export LD_LIBRARY_PATH="$WORK/root/usr/lib/x86_64-linux-gnu:${LD_LIBRARY_PATH:-}"
SOCKET="$WORK/socket"

mkdir -p "$WORK/debs" "$WORK/root" "$WORK/data" "$SOCKET"
chmod 700 "$WORK/data" "$SOCKET"
cd "$WORK/debs"
# Release 1.0 supports one layout: the distro PostgreSQL major named by POSTGRES_MAJOR (14 on
# Ubuntu 22.04, matching the supported deployment). No probing or alternate majors.
MAJOR="${POSTGRES_MAJOR:-14}"
DEB_DIR="$WORK/debs/$MAJOR"
mkdir -p "$DEB_DIR"
(cd "$DEB_DIR" && apt-get download "postgresql-$MAJOR" "postgresql-client-$MAJOR" libpq5)
BIN="$WORK/root/usr/lib/postgresql/$MAJOR/bin"
for deb in "$DEB_DIR"/*.deb; do dpkg-deb -x "$deb" "$WORK/root"; done
[ -x "$BIN/postgres" ] || { echo "extracted PostgreSQL binary missing: $BIN/postgres" >&2; exit 1; }
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

# Provision only: this script never starts, restarts or stops a server, and it never creates the
# database. The managed description-postgres unit owns the only long-running server; operatorctl
# starts that unit, waits for readiness, then creates airflow_meta before Airflow migration.
echo "AIRFLOW_DB_URL=postgresql+psycopg2://solidworks@/airflow_meta?host=$SOCKET&port=5433"
echo "POSTGRES_MAJOR=$MAJOR"
