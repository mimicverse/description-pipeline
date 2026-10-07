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

load_installed() {
  OPERATOR_STATE_DIR="$(sed -n 's/^OPERATOR_STATE=//p' "$ENV_FILE" | head -1)"
  [ -n "$OPERATOR_STATE_DIR" ] || die "OPERATOR_STATE is not set in $ENV_FILE"
  [ -f "$OPERATOR_STATE_DIR/resolved.env" ] \
    || die "not installed: $OPERATOR_STATE_DIR/resolved.env is missing (run install first)"
  # shellcheck disable=SC1090
  source "$OPERATOR_STATE_DIR/resolved.env"
  TARGET="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
}

# Read-only: render into temporary directories and compare with the installed deployment.
drift_check() {
  local rc=0 tmp_xdg drift_log
  drift_log="$(mktemp)"
  if ! python3 "$HERE/render_operator.py" --env-file "$ENV_FILE" --dry-run > "$drift_log"; then rc=1; fi
  grep -v '^DRIFT none$' "$drift_log" || true
  tmp_xdg="$(mktemp -d)"
  XDG_CONFIG_HOME="$tmp_xdg" airflow_render >/dev/null 2>&1 || true
  for unit in "${UNITS[@]}"; do
    if [ -f "$TARGET/$unit.service" ] && [ -f "$tmp_xdg/systemd/user/$unit.service" ]; then
      if ! diff -q "$tmp_xdg/systemd/user/$unit.service" "$TARGET/$unit.service" >/dev/null; then
        echo "DRIFT unit differs: $unit.service"; rc=1
      fi
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
    if [ -n "${POSTGRES_ROOT:-}" ] && [ ! -f "$POSTGRES_ROOT/data/PG_VERSION" ]; then
      POSTGRES_ROOT="$POSTGRES_ROOT" POSTGRES_MAJOR="${POSTGRES_MAJOR:-14}" \
        bash "$AIRFLOW_HERE/scripts/install_postgres.sh"
    fi
    AIRFLOW_VENV="$AIRFLOW_VENV" AIRFLOW_HOME="$AIRFLOW_HOME" AIRFLOW_PYTHON="$AIRFLOW_PYTHON" \
      PIPELINE_WHEEL="$PIPELINE_WHEEL" AIRFLOW_DB_URL="$AIRFLOW_DB_URL" \
      bash "$AIRFLOW_HERE/install.sh"
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
    systemctl --user enable --now description-portal.service description-operator-proxy.service
    ;;
  stop)
    load_installed
    drift_check || true
    systemctl --user disable --now description-operator-proxy.service description-portal.service || true
    airflow_lifecycle stop
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
