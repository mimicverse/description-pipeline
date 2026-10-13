#!/usr/bin/env bash
# The one supported lifecycle for the Linux operator deployment:
#   validate  static platform.json checks (schema/paths/targets/ports; no probes)
#   install   render the immutable config snapshot, then install the pinned runtime
#   start     enable/start PostgreSQL, Airflow, tunnel, portal and the HTTPS proxy
#   drain     close submissions and DAG admission, keep in-flight, return active/unresolved
#   stop      disable/stop them in reverse order (only when drained and settled)
#   status    systemctl --user is-active for every managed unit
#   health    deploy/operator/health.sh (static checks plus live service/HTTP checks)
#   reconcile confirm and register external outcomes; never repeats unknown side effects
#   stage     build the two-end candidate installation (isolated targets)
#   promote   switch both ends to the staged candidate (admission closed until verified)
#   rollback  restore both ends from the recorded previous installation
#
#   operatorctl.sh <verb> --config FILE
#   transitional until each verb is bridged:
#     operatorctl.sh <start|stop|status|health> --env-file FILE
#
# Only description-* units are written; unrelated user units are untouched. Credentials and state
# live under the configured state root (outside Git).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AIRFLOW_HERE="$HERE/../airflow"
UNITS=(description-postgres description-solidworks-tunnel description-airflow-dag-processor
       description-airflow-scheduler description-airflow-api-server description-portal
       description-operator-proxy)

die() { echo "operatorctl: $*" >&2; exit 1; }
usage() {
  cat >&2 <<'USAGE'
usage: operatorctl.sh <validate|install|start|drain|stop|status|health|reconcile|stage|promote|rollback> --config FILE
       operatorctl.sh <start|stop|status|health> --env-file FILE  # transitional entry
install inputs: --install-id ID --wheel FILE --endpoint-identity FILE [--release-file FILE]
exit codes: 0 ok; 1 configuration/environment error; 2 usage; 3 recognized verb without its --config bridge yet
USAGE
  exit 2
}

ACTION="${1:-}"
shift || true
CONFIG_FILE=""
ENV_FILE=""
INSTALL_ID=""
WHEEL_FILE=""
ENDPOINT_IDENTITY=""
RELEASE_FILE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --config) CONFIG_FILE="${2:-}"; shift 2 ;;
    --env-file) ENV_FILE="${2:-}"; shift 2 ;;
    --install-id) INSTALL_ID="${2:-}"; shift 2 ;;
    --wheel) WHEEL_FILE="${2:-}"; shift 2 ;;
    --endpoint-identity) ENDPOINT_IDENTITY="${2:-}"; shift 2 ;;
    --release-file) RELEASE_FILE="${2:-}"; shift 2 ;;
    *) usage ;;
  esac
done
[ -n "$ACTION" ] || usage
case "$ACTION" in
  validate|install|start|drain|stop|status|health|reconcile|stage|promote|rollback) ;;
  *) usage ;;
esac

install_from_config() {
  [ -f "$ENDPOINT_IDENTITY" ] || die "--endpoint-identity FILE is required for install"
  if [ -z "$RELEASE_FILE" ]; then
    RELEASE_FILE="$HERE/../../release.json"
  fi
  [ -f "$RELEASE_FILE" ] || die "release manifest not found: $RELEASE_FILE"
  # Preflight the bound identities (release triple + artifact digests; endpoint generation)
  # before any installation step runs.
  python3 "$HERE/install_record.py" check --release-file "$RELEASE_FILE" \
    --endpoint-identity "$ENDPOINT_IDENTITY" >/dev/null || exit $?
  if [ -z "$INSTALL_ID" ]; then
    INSTALL_ID="$(python3 -c 'import uuid; print(uuid.uuid4().hex)')"
  fi
  SNAPSHOT_PATH="$(python3 "$HERE/platform_config.py" render --config "$CONFIG_FILE" \
    --install-id "$INSTALL_ID" | sed -n 's/^snapshot=//p')" || exit $?
  [ -f "$SNAPSHOT_PATH" ] || die "config snapshot was not written"
  if [ -n "$WHEEL_FILE" ]; then
    DERIVED_ENV="$(python3 "$HERE/platform_env.py" emit --snapshot "$SNAPSHOT_PATH" \
      --install-id "$INSTALL_ID" --tree "$HERE/../.." --wheel "$WHEEL_FILE" | sed -n 's/^env=//p')" || exit $?
  else
    DERIVED_ENV="$(python3 "$HERE/platform_env.py" emit --snapshot "$SNAPSHOT_PATH" \
      --install-id "$INSTALL_ID" --tree "$HERE/../.." | sed -n 's/^env=//p')" || exit $?
  fi
  [ -f "$DERIVED_ENV" ] || die "derived environment was not written"
  ENV_FILE="$DERIVED_ENV"
}

if [ -n "$CONFIG_FILE" ] && [ -n "$ENV_FILE" ]; then usage; fi
if [ -n "$INSTALL_ID$WHEEL_FILE$ENDPOINT_IDENTITY$RELEASE_FILE" ] && [ "$ACTION" != "install" ]; then usage; fi
if [ -n "$CONFIG_FILE" ]; then
  [ -f "$CONFIG_FILE" ] || die "config file not found: $CONFIG_FILE"
  # Every verb passes the same static config gate first; the snapshot rendered at install stays
  # the single authority for the whole lifecycle.
  python3 "$HERE/platform_config.py" validate --config "$CONFIG_FILE" >/dev/null || exit $?
  case "$ACTION" in
    validate)
      echo "operatorctl: configuration valid: $CONFIG_FILE"
      exit 0 ;;
    install)
      install_from_config ;;
    *)
      echo "operatorctl: '$ACTION' is recognized but its --config bridge is not implemented in this increment;" >&2
      echo "use the transitional --env-file entry for the current deployment" >&2
      exit 3 ;;
  esac
fi
if [ -z "$CONFIG_FILE" ]; then
  # Transitional env-file entry, removed as each verb is bridged.
  [ -n "$ENV_FILE" ] || usage
  [ -f "$ENV_FILE" ] || die "env file not found: $ENV_FILE"
  case "$ACTION" in
    start|stop|status|health) ;;
    *) usage ;;
  esac
fi

airflow_render() {
  AIRFLOW_VENV="$AIRFLOW_VENV" AIRFLOW_HOME="$AIRFLOW_HOME" \
  POSTGRES_ROOT="${POSTGRES_ROOT:-}" POSTGRES_MAJOR="${POSTGRES_MAJOR:-14}" \
  SOLIDWORKS_SSH_HOST="${SOLIDWORKS_SSH_HOST:-}" \
  AIRFLOW_API_PORT="$AIRFLOW_API_PORT" POSTGRES_PORT="$POSTGRES_PORT" \
  TUNNEL_LOOPBACK_PORT="$TUNNEL_LOOPBACK_PORT" SOLIDWORKS_ENDPOINT_PORT="$SOLIDWORKS_ENDPOINT_PORT" \
  FEISHU_ENV_FILE="${FEISHU_ENV_FILE:-$AIRFLOW_HOME/feishu.env}" \
    bash "$AIRFLOW_HERE/services.sh" render
}

airflow_lifecycle() {
  AIRFLOW_VENV="$AIRFLOW_VENV" AIRFLOW_HOME="$AIRFLOW_HOME" \
  POSTGRES_ROOT="${POSTGRES_ROOT:-}" POSTGRES_MAJOR="${POSTGRES_MAJOR:-14}" \
  SOLIDWORKS_SSH_HOST="${SOLIDWORKS_SSH_HOST:-}" \
  AIRFLOW_API_PORT="$AIRFLOW_API_PORT" POSTGRES_PORT="$POSTGRES_PORT" \
  TUNNEL_LOOPBACK_PORT="$TUNNEL_LOOPBACK_PORT" SOLIDWORKS_ENDPOINT_PORT="$SOLIDWORKS_ENDPOINT_PORT" \
  FEISHU_ENV_FILE="${FEISHU_ENV_FILE:-$AIRFLOW_HOME/feishu.env}" \
    bash "$AIRFLOW_HERE/services.sh" "$1"
}

open_admission() {
  local deadline=$((SECONDS + 60))
  # Unpause alone silently succeeds before the DAG is registered. Confirm its
  # persisted state before exposing ingress, including on a fresh installation.
  while ((SECONDS < deadline)); do
    AIRFLOW_HOME="$AIRFLOW_HOME" "$AIRFLOW_VENV/bin/airflow" dags unpause solidworks_to_urdf >/dev/null
    if AIRFLOW_HOME="$AIRFLOW_HOME" "$AIRFLOW_VENV/bin/python" - <<'PY'
import sys
from airflow.models.dag import DagModel
from airflow.utils.session import create_session

with create_session() as session:
    dag = session.get(DagModel, "solidworks_to_urdf")
    sys.exit(0 if dag is not None and dag.is_paused is False else 1)
PY
    then
      return
    fi
    sleep 2
  done
  die "DAG admission did not open; ingress was not started"
}

load_installed() {
  OPERATOR_STATE_DIR="$(sed -n 's/^OPERATOR_STATE=//p' "$ENV_FILE" | head -1)"
  [ -n "$OPERATOR_STATE_DIR" ] || die "OPERATOR_STATE is not set in $ENV_FILE"
  [ -f "$OPERATOR_STATE_DIR/resolved.env" ] \
    || die "not installed: $OPERATOR_STATE_DIR/resolved.env is missing (run install first)"
  # shellcheck disable=SC1090
  source "$OPERATOR_STATE_DIR/resolved.env"
  # Every port comes from the single deploy source (render_operator.py) via resolved.env.
  for key in AIRFLOW_API_PORT TUNNEL_LOOPBACK_PORT POSTGRES_PORT SOLIDWORKS_ENDPOINT_PORT; do
    [ -n "${!key:-}" ] || die "resolved.env is missing $key; run install to re-render the deployment"
  done
  TARGET="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
}

# Read-only: render the configuration and every managed unit into temporary directories and
# compare with the installed deployment. Missing installed files, missing renders and render
# failures count as drift, not as silent passes.
drift_check() {
  local rc=0 tmp_xdg drift_log tmp_state
  drift_log="$(mktemp)"
  tmp_xdg="$(mktemp -d)"
  if ! python3 "$HERE/render_operator.py" --env-file "$ENV_FILE" --dry-run \
        --units-dir "$tmp_xdg/systemd/user" > "$drift_log"; then rc=1; fi
  grep -v -e '^DRIFT none$' -e '^OPERATOR_UNITS=' "$drift_log" || true
  # The operator units carry the temporary render state; normalize it before comparing.
  tmp_state="$(sed -n 's/^Environment=OPERATOR_STATE=//p' \
    "$tmp_xdg/systemd/user/description-portal.service" 2>/dev/null | head -1)"
  if [ -n "$tmp_state" ] && [ "$tmp_state" != "$OPERATOR_STATE" ]; then
    for unit in "${UNITS[@]}"; do
      [ -f "$tmp_xdg/systemd/user/$unit.service" ] \
        && sed -i "s#$tmp_state#$OPERATOR_STATE#g" "$tmp_xdg/systemd/user/$unit.service"
    done
  fi
  if ! XDG_CONFIG_HOME="$tmp_xdg" airflow_render >/dev/null 2>&1; then
    echo "DRIFT airflow unit render failed"; rc=1
  fi
  for unit in "${UNITS[@]}"; do
    if [ ! -f "$TARGET/$unit.service" ]; then
      echo "DRIFT unit missing: $unit.service"; rc=1
    elif [ ! -f "$tmp_xdg/systemd/user/$unit.service" ]; then
      echo "DRIFT unit missing from render: $unit.service"; rc=1
    elif ! diff -q "$tmp_xdg/systemd/user/$unit.service" "$TARGET/$unit.service" >/dev/null; then
      echo "DRIFT unit differs: $unit.service"; rc=1
    fi
  done
  python3 -c "import shutil,sys; shutil.rmtree(sys.argv[1], ignore_errors=True)" "$tmp_xdg"
  rm -f "$drift_log"
  return "$rc"
}

case "$ACTION" in
  install)
    RENDER_LOG="$(mktemp)"
    TOOLCHAIN_LOG="$(mktemp)"
    trap 'rm -f "$RENDER_LOG" "$TOOLCHAIN_LOG"' EXIT
    python3 "$HERE/render_operator.py" --env-file "$ENV_FILE" > "$RENDER_LOG"
    cat "$RENDER_LOG"
    # shellcheck disable=SC1090
    source "$(sed -n 's/^OPERATOR_STATE=//p' "$RENDER_LOG" | head -1)/resolved.env"
    TARGET="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
    bash "$HERE/scripts/install_toolchain.sh" > "$TOOLCHAIN_LOG"
    cat "$TOOLCHAIN_LOG"
    # shellcheck disable=SC1090
    source "$TOOLCHAIN_LOG"
    python3 "$HERE/render_operator.py" --env-file "$ENV_FILE" --units-dir "$TARGET" >/dev/null
    airflow_render
    if [ -n "${POSTGRES_ROOT:-}" ]; then
      if [ ! -f "$POSTGRES_ROOT/data/PG_VERSION" ]; then
        POSTGRES_ROOT="$POSTGRES_ROOT" POSTGRES_MAJOR="${POSTGRES_MAJOR:-14}" POSTGRES_PORT="$POSTGRES_PORT" \
          bash "$AIRFLOW_HERE/scripts/install_postgres.sh"
      fi
      # One path for a fresh install and a stopped reinstall: the managed unit owns the server
      # `airflow db migrate` needs, and install waits for its socket before migration. No other
      # service is started here.
      systemctl --user daemon-reload
      systemctl --user start description-postgres.service \
        || die "description-postgres.service failed to start"
      PG_BIN="$POSTGRES_ROOT/root/usr/lib/postgresql/${POSTGRES_MAJOR:-14}/bin"
      export LD_LIBRARY_PATH="$POSTGRES_ROOT/root/usr/lib/x86_64-linux-gnu:${LD_LIBRARY_PATH:-}"
      ready=0
      for _ in $(seq 1 20); do
        if "$PG_BIN/pg_isready" -q -h "$POSTGRES_ROOT/socket" -p "$POSTGRES_PORT"; then ready=1; break; fi
        sleep 0.5
      done
      [ "$ready" = "1" ] || die "PostgreSQL did not become ready on $POSTGRES_ROOT/socket"
      db_state="$("$PG_BIN/psql" -h "$POSTGRES_ROOT/socket" -p "$POSTGRES_PORT" -U solidworks -d postgres \
        -tAc "select 1 from pg_database where datname='airflow_meta'")" \
        || die "could not query pg_database for airflow_meta"
      if [ "$db_state" != "1" ]; then
        "$PG_BIN/createdb" -h "$POSTGRES_ROOT/socket" -p "$POSTGRES_PORT" -U solidworks airflow_meta \
          || die "could not create the airflow_meta database"
      fi
    fi
    AIRFLOW_VENV="$AIRFLOW_VENV" AIRFLOW_HOME="$AIRFLOW_HOME" AIRFLOW_PYTHON="${AIRFLOW_PYTHON:-}" \
      PIPELINE_WHEEL="${PIPELINE_WHEEL:?PIPELINE_WHEEL is required for install}" \
      AIRFLOW_DB_URL="$AIRFLOW_DB_URL" AIRFLOW_API_PORT="$AIRFLOW_API_PORT" \
      bash "$AIRFLOW_HERE/install.sh"
    # The wheel must provide the portal and the Feishu auth manager before the units start.
    if ! "$AIRFLOW_VENV/bin/python" -c 'import importlib.util, sys; sys.exit(0 if all(
        importlib.util.find_spec(name) for name in
        ("description_pipeline.orchestration.portal", "description_pipeline.orchestration.feishu_auth")) else 1)'; then
      die "PIPELINE_WHEEL does not provide the portal and Feishu auth manager modules: $PIPELINE_WHEEL"
    fi
    mkdir -p "$SOLIDWORKS_HANDOFF_ROOT"
    chmod 700 "$SOLIDWORKS_HANDOFF_ROOT"
    mkdir -p "$PIPELINE_STORE_ROOT"
    chmod 700 "$PIPELINE_STORE_ROOT"
    [ -f "$ENDPOINT_TOKEN_FILE" ] || die "endpoint token file missing: $ENDPOINT_TOKEN_FILE"
    AIRFLOW_HOME="$AIRFLOW_HOME" "$AIRFLOW_VENV/bin/python" "$AIRFLOW_HERE/scripts/add_connection.py" \
      --token-file "$ENDPOINT_TOKEN_FILE" --host 127.0.0.1 --port "$TUNNEL_LOOPBACK_PORT" \
      --handoff-root "$SOLIDWORKS_HANDOFF_ROOT" --pipeline-config "$PORTAL_CONFIG"
    if [ ! -x "${NGINX_BIN:-/usr/sbin/nginx}" ]; then
      OPERATOR_STATE="$OPERATOR_STATE" bash "$HERE/scripts/install_proxy.sh" >/dev/null
    fi
    systemctl --user daemon-reload
    # The install record binds this generation to its snapshot, release identity and both ends;
    # it is written only after the installation legs are ready.
    STATE_ROOT="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["resolved"]["storage"]["state_root"])' "$SNAPSHOT_PATH")"
    SERVICES_CSV="$(IFS=,; printf '%s' "${UNITS[*]}")"
    python3 "$HERE/install_record.py" build --state-root "$STATE_ROOT" --install-id "$INSTALL_ID" \
      --snapshot "$SNAPSHOT_PATH" --release-file "$RELEASE_FILE" \
      --endpoint-identity "$ENDPOINT_IDENTITY" --services "$SERVICES_CSV" \
      --dag "$AIRFLOW_HERE/dags/solidworks_to_urdf.py"
    echo "install complete: ${OPERATOR_URL:-unknown}"
    ;;
  start)
    load_installed
    drift_check || die "configuration drift detected; run install before start"
    airflow_lifecycle start
    open_admission
    systemctl --user enable --now description-portal.service description-operator-proxy.service
    ;;
  stop)
    load_installed
    drift_check || true
    systemctl --user disable --now description-operator-proxy.service description-portal.service || true
    ADMISSION_RC=0
    AIRFLOW_HOME="$AIRFLOW_HOME" "$AIRFLOW_VENV/bin/airflow" dags pause solidworks_to_urdf || ADMISSION_RC=$?
    airflow_lifecycle stop
    exit "$ADMISSION_RC"
    ;;
  status)
    load_installed
    drift_check || true
    systemctl --user is-active "${UNITS[@]}" || true
    ;;
  health)
    load_installed
    DRIFT_RC=0
    drift_check || DRIFT_RC=1
    OPERATOR_STATE="$OPERATOR_STATE" bash "$HERE/health.sh"
    HEALTH_RC=$?
    [ "$DRIFT_RC" = "0" ] || exit 1
    exit "$HEALTH_RC"
    ;;
  *) usage ;;
esac
