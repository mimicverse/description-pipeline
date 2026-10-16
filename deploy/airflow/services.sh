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
  case "${AIRFLOW_API_PORT:?set AIRFLOW_API_PORT (rendered resolved.env)}" in
    *[!0-9]*|"") echo 'AIRFLOW_API_PORT must be a TCP port' >&2; exit 1 ;;
  esac
  if [ -n "${SOLIDWORKS_SSH_HOST:-}" ]; then
    [[ "$SOLIDWORKS_SSH_HOST" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]] || {
      echo 'SOLIDWORKS_SSH_HOST must be a configured SSH host alias' >&2; exit 1;
    }
    case "${SOLIDWORKS_ENDPOINT_PORT:?set SOLIDWORKS_ENDPOINT_PORT}" in
      *[!0-9]*|"") echo 'SOLIDWORKS_ENDPOINT_PORT must be a TCP port' >&2; exit 1 ;;
    esac
    case "${TUNNEL_LOOPBACK_PORT:?set TUNNEL_LOOPBACK_PORT}" in
      *[!0-9]*|"") echo 'TUNNEL_LOOPBACK_PORT must be a TCP port' >&2; exit 1 ;;
    esac
    sed -e "s#@SSH_HOST@#$SOLIDWORKS_SSH_HOST#g" \
        -e "s#@TUNNEL_LOOPBACK_PORT@#$TUNNEL_LOOPBACK_PORT#g" \
        -e "s#@ENDPOINT_PORT@#$SOLIDWORKS_ENDPOINT_PORT#g" \
      "$HERE/systemd/description-solidworks-tunnel.service" > "$TARGET/description-solidworks-tunnel.service"
  fi
  if [ -n "${POSTGRES_ROOT:-}" ]; then
    case "${POSTGRES_PORT:?set POSTGRES_PORT}" in
      *[!0-9]*|"") echo 'POSTGRES_PORT must be a TCP port' >&2; exit 1 ;;
    esac
    sed -e "s#@POSTGRES_ROOT@#$POSTGRES_ROOT#g" \
        -e "s#@POSTGRES_PORT@#$POSTGRES_PORT#g" \
        -e "s#@POSTGRES_MAJOR@#${POSTGRES_MAJOR:-14}#g" \
      "$HERE/systemd/description-postgres.service" > "$TARGET/description-postgres.service"
  fi
  for unit in description-airflow-dag-processor description-airflow-scheduler description-airflow-api-server; do
    sed -e "s#@VENV@#$AIRFLOW_VENV#g" -e "s#@AIRFLOW_HOME@#$AIRFLOW_HOME#g" \
        -e "s#@AIRFLOW_API_PORT@#$AIRFLOW_API_PORT#g" \
        -e "s#@FEISHU_ENV@#${FEISHU_ENV_FILE:-$AIRFLOW_HOME/feishu.env}#g" \
      "$HERE/systemd/$unit.service" > "$TARGET/$unit.service"
    # The runtime units consume the one controlled derived environment (the same file the
    # portal unit loads) so task processes inherit the platform identity values; the renderer
    # only ever points at the file - it never copies any value into the unit.
    if [ -n "${OPERATOR_DERIVED_ENV:-}" ]; then
      sed -i "/^ExecStart=/i EnvironmentFile=$OPERATOR_DERIVED_ENV" "$TARGET/$unit.service"
    fi
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
