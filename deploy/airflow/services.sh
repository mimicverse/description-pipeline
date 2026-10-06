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
UNITS+=(description-airflow-dag-processor description-airflow-scheduler description-airflow-api-server)

render_units() {
  mkdir -p "$TARGET"
  if [ -n "${POSTGRES_ROOT:-}" ]; then
    sed -e "s#@POSTGRES_ROOT@#$POSTGRES_ROOT#g" \
      "$HERE/systemd/description-postgres.service" > "$TARGET/description-postgres.service"
  fi
  for unit in description-airflow-dag-processor description-airflow-scheduler description-airflow-api-server; do
    sed -e "s#@VENV@#$AIRFLOW_VENV#g" -e "s#@AIRFLOW_HOME@#$AIRFLOW_HOME#g" \
      "$HERE/systemd/$unit.service" > "$TARGET/$unit.service"
  done
}

warn_legacy() {
  for legacy in solidworks-postgres airflow-scheduler airflow-api-server airflow-dag-processor; do
    if [ -e "$TARGET/$legacy.service" ]; then
      echo "warning: legacy unit $TARGET/$legacy.service is not managed here;" >&2
      echo "         systemctl --user disable --now $legacy && rm $TARGET/$legacy.service" >&2
    fi
  done
}

case "$ACTION" in
  render) render_units ;;
  install) render_units; warn_legacy; systemctl --user daemon-reload ;;
  start) for u in "${UNITS[@]}"; do systemctl --user enable --now "$u"; done ;;
  stop) for ((i=${#UNITS[@]}-1; i>=0; i--)); do systemctl --user disable --now "${UNITS[$i]}" || true; done ;;
  status) systemctl --user status "${UNITS[@]}" --no-pager ;;
  *) echo "usage: $0 render|install|start|stop|status" >&2; exit 2 ;;
esac
