#!/usr/bin/env bash
# The one supported lifecycle for the Linux operator deployment:
#   install  pinned toolchain + PostgreSQL + Airflow runtime + config/units + proxy binary
#   start    enable/start PostgreSQL, Airflow, tunnel, portal and the HTTPS proxy
#   stop     disable/stop them in reverse order
#   status   systemctl --user is-active for every managed unit
#   health   deploy/operator/health.sh (static checks plus live service/HTTP checks)
#
#   operatorctl.sh <install|start|stop|status|health> --env-file FILE
#
# Only description-* units are written; unrelated user units are untouched. Credentials and state
# live under OPERATOR_STATE (outside Git).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AIRFLOW_HERE="$HERE/../airflow"
UNITS=(description-postgres description-solidworks-tunnel description-airflow-dag-processor
       description-airflow-scheduler description-airflow-api-server description-portal
       description-operator-proxy)

die() { echo "operatorctl: $*" >&2; exit 1; }
usage() {
  cat >&2 <<'USAGE'
usage: operatorctl.sh <install|start|stop|status|health> --env-file FILE
USAGE
  exit 2
}

ACTION="${1:-}"
shift || true
ENV_FILE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --env-file) ENV_FILE="${2:-}"; shift 2 ;;
    *) usage ;;
  esac
done
[ -n "$ACTION" ] && [ -n "$ENV_FILE" ] || usage
[ -f "$ENV_FILE" ] || die "env file not found: $ENV_FILE"
case "$ACTION" in
  install|start|stop|status|health) ;;
  *) usage ;;
esac

airflow_render() {
  AIRFLOW_VENV="$AIRFLOW_VENV" AIRFLOW_HOME="$AIRFLOW_HOME" \
  POSTGRES_ROOT="${POSTGRES_ROOT:-}" POSTGRES_MAJOR="${POSTGRES_MAJOR:-14}" \
  SOLIDWORKS_SSH_HOST="${SOLIDWORKS_SSH_HOST:-}" \
  SOLIDWORKS_ENDPOINT_PORT="${SOLIDWORKS_ENDPOINT_PORT:-8765}" \
  FEISHU_ENV_FILE="${FEISHU_ENV_FILE:-$AIRFLOW_HOME/feishu.env}" \
    bash "$AIRFLOW_HERE/services.sh" render
}

airflow_lifecycle() {
  AIRFLOW_VENV="$AIRFLOW_VENV" AIRFLOW_HOME="$AIRFLOW_HOME" \
  POSTGRES_ROOT="${POSTGRES_ROOT:-}" POSTGRES_MAJOR="${POSTGRES_MAJOR:-14}" \
  SOLIDWORKS_SSH_HOST="${SOLIDWORKS_SSH_HOST:-}" \
  SOLIDWORKS_ENDPOINT_PORT="${SOLIDWORKS_ENDPOINT_PORT:-8765}" \
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
        POSTGRES_ROOT="$POSTGRES_ROOT" POSTGRES_MAJOR="${POSTGRES_MAJOR:-14}" \
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
        if "$PG_BIN/pg_isready" -q -h "$POSTGRES_ROOT/socket" -p 5433; then ready=1; break; fi
        sleep 0.5
      done
      [ "$ready" = "1" ] || die "PostgreSQL did not become ready on $POSTGRES_ROOT/socket"
      db_state="$("$PG_BIN/psql" -h "$POSTGRES_ROOT/socket" -p 5433 -U solidworks -d postgres \
        -tAc "select 1 from pg_database where datname='airflow_meta'")" \
        || die "could not query pg_database for airflow_meta"
      if [ "$db_state" != "1" ]; then
        "$PG_BIN/createdb" -h "$POSTGRES_ROOT/socket" -p 5433 -U solidworks airflow_meta \
          || die "could not create the airflow_meta database"
      fi
    fi
    AIRFLOW_VENV="$AIRFLOW_VENV" AIRFLOW_HOME="$AIRFLOW_HOME" AIRFLOW_PYTHON="${AIRFLOW_PYTHON:-}" \
      PIPELINE_WHEEL="${PIPELINE_WHEEL:?PIPELINE_WHEEL is required for install}" \
      AIRFLOW_DB_URL="$AIRFLOW_DB_URL" \
      bash "$AIRFLOW_HERE/install.sh"
    # The wheel must provide the portal and the Feishu auth manager before the units start.
    if ! "$AIRFLOW_VENV/bin/python" -c 'import importlib.util, sys; sys.exit(0 if all(
        importlib.util.find_spec(name) for name in
        ("description_pipeline.orchestration.portal", "description_pipeline.orchestration.feishu_auth")) else 1)'; then
      die "PIPELINE_WHEEL does not provide the portal and Feishu auth manager modules: $PIPELINE_WHEEL"
    fi
    mkdir -p "$SOLIDWORKS_HANDOFF_ROOT"
    chmod 700 "$SOLIDWORKS_HANDOFF_ROOT"
    [ -f "$ENDPOINT_TOKEN_FILE" ] || die "endpoint token file missing: $ENDPOINT_TOKEN_FILE"
    AIRFLOW_HOME="$AIRFLOW_HOME" "$AIRFLOW_VENV/bin/python" "$AIRFLOW_HERE/scripts/add_connection.py" \
      --token-file "$ENDPOINT_TOKEN_FILE" --host 127.0.0.1 --port 18765 \
      --handoff-root "$SOLIDWORKS_HANDOFF_ROOT"
    if [ ! -x "${NGINX_BIN:-/usr/sbin/nginx}" ]; then
      OPERATOR_STATE="$OPERATOR_STATE" bash "$HERE/scripts/install_proxy.sh" >/dev/null
    fi
    systemctl --user daemon-reload
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
