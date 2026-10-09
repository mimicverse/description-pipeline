#!/usr/bin/env bash
# Manage the user services for this deployment. Only the description-* units below are ever
# written; unrelated units in the same directory are left untouched.
#
#   render   write the description-* unit files (no systemctl; used by tests and for review)
#   install  render + reload the user manager
#   start    enable --now postgres (if configured), dag-processor, scheduler, api-server
#   stop     disable --now in reverse order
#   status   systemctl --user status for the units
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
: "${AIRFLOW_VENV:?set AIRFLOW_VENV}"
: "${AIRFLOW_HOME:?set AIRFLOW_HOME}"
TARGET="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
ACTION="${1:-install}"

UNITS=()
[ -n "${POSTGRES_ROOT:-}" ] && UNITS+=(description-postgres)
[ -n "${SOLIDWORKS_SSH_HOST:-}" ] && UNITS+=(description-solidworks-tunnel)
UNITS+=(description-airflow-dag-processor description-airflow-scheduler description-airflow-api-server)

render_units() {
  mkdir -p "$TARGET"
  if [ -n "${SOLIDWORKS_SSH_HOST:-}" ]; then
    [[ "$SOLIDWORKS_SSH_HOST" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]] || {
      echo 'SOLIDWORKS_SSH_HOST must be a configured SSH host alias' >&2; exit 1;
    }
    case "${SOLIDWORKS_ENDPOINT_PORT:-8765}" in
      *[!0-9]*|"") echo 'SOLIDWORKS_ENDPOINT_PORT must be a TCP port' >&2; exit 1 ;;
    esac
    sed -e "s#@SSH_HOST@#$SOLIDWORKS_SSH_HOST#g" \
        -e "s#@ENDPOINT_PORT@#${SOLIDWORKS_ENDPOINT_PORT:-8765}#g" \
      "$HERE/systemd/description-solidworks-tunnel.service" > "$TARGET/description-solidworks-tunnel.service"
  fi
  if [ -n "${POSTGRES_ROOT:-}" ]; then
    sed -e "s#@POSTGRES_ROOT@#$POSTGRES_ROOT#g" \
        -e "s#@POSTGRES_MAJOR@#${POSTGRES_MAJOR:-14}#g" \
      "$HERE/systemd/description-postgres.service" > "$TARGET/description-postgres.service"
  fi
  for unit in description-airflow-dag-processor description-airflow-scheduler description-airflow-api-server; do
    sed -e "s#@VENV@#$AIRFLOW_VENV#g" -e "s#@AIRFLOW_HOME@#$AIRFLOW_HOME#g" \
        -e "s#@FEISHU_ENV@#${FEISHU_ENV_FILE:-$AIRFLOW_HOME/feishu.env}#g" \
      "$HERE/systemd/$unit.service" > "$TARGET/$unit.service"
  done
}

case "$ACTION" in
  render) render_units ;;
  install) render_units; systemctl --user daemon-reload ;;
  start) for u in "${UNITS[@]}"; do systemctl --user enable --now "$u"; done ;;
  stop) for ((i=${#UNITS[@]}-1; i>=0; i--)); do systemctl --user disable --now "${UNITS[$i]}" || true; done ;;
  status) systemctl --user status "${UNITS[@]}" --no-pager ;;
  *) echo "usage: $0 render|install|start|stop|status" >&2; exit 2 ;;
esac
