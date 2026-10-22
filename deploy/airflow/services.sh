#!/usr/bin/env bash
# Manage the user services for this deployment. Only the description-* units below are ever
# written; unrelated units in the same directory are left untouched.
#
#   render          write the description-* unit files (no systemctl; used by tests and for review)
#   start           enable --now postgres (if configured), dag-processor, scheduler, api-server
#   stop            disable --now every unit in reverse order (the full deployment stop)
#   stop-executors  disable --now and prove stopped the execution units only (tunnel,
#                   dag-processor, scheduler, api-server): the promotion window keeps the
#                   shared PostgreSQL running and ready for its guarded migrations
#   units ROLE      print the static managed-unit role inventory; read-only and env-free
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# One static role inventory for rendering, lifecycle control and health. It is code data only;
# no runtime state or installation record is duplicated here.
UNIT_MANIFEST=(
  'description-postgres|database,services'
  'description-solidworks-tunnel|executor,services'
  'description-airflow-dag-processor|airflow,executor,services'
  'description-airflow-scheduler|airflow,executor,services'
  'description-airflow-api-server|airflow,executor,services'
  'description-portal|operator'
  'description-operator-proxy|operator'
)
unit_manifest() {
  local requested="$1" record unit roles
  for record in "${UNIT_MANIFEST[@]}"; do
    IFS='|' read -r unit roles <<< "$record"
    if [ "$requested" = "all" ]; then
      printf '%s\n' "$unit"
      continue
    fi
    case ",$roles," in
      *",$requested,"*) printf '%s\n' "$unit" ;;
    esac
  done
}
usage() {
  echo "usage: $0 render|start|stop|stop-executors" >&2
  echo "       $0 units all|services|airflow|executor|database|operator" >&2
  exit 2
}
case "${1:-}" in
  units)
    [ "$#" -eq 2 ] || usage
    case "$2" in
      all|services|airflow|executor|database|operator) ;;
      *) usage ;;
    esac
    unit_manifest "$2"
    exit 0
    ;;
  render|start|stop|stop-executors)
    [ "$#" -eq 1 ] || usage
    ACTION="$1"
    ;;
  *) usage ;;
esac
: "${AIRFLOW_VENV:?set AIRFLOW_VENV}"
: "${AIRFLOW_HOME:?set AIRFLOW_HOME}"
TARGET="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

# The execution units are the maintenance window's stop set: they must be down while the
# candidate switches, and they must never take the shared database down with them. The
# database unit is managed separately because the promotion's guarded migrations run
# against it live.
EXECUTORS=()
while IFS= read -r unit; do
  [ "$unit" != "description-solidworks-tunnel" ] || [ -n "${SOLIDWORKS_SSH_HOST:-}" ] || continue
  EXECUTORS+=("$unit")
done < <(unit_manifest executor)
UNITS=()
[ -z "${POSTGRES_ROOT:-}" ] || mapfile -t UNITS < <(unit_manifest database)
UNITS+=("${EXECUTORS[@]}")

render_units() {
  mkdir -p "$TARGET"
  case "${AIRFLOW_API_PORT:?set AIRFLOW_API_PORT (rendered resolved.env)}" in
    *[!0-9]*|"") echo 'AIRFLOW_API_PORT must be a TCP port' >&2; exit 1 ;;
  esac
  while IFS= read -r unit; do
    case "$unit" in
      description-solidworks-tunnel)
        [ -n "${SOLIDWORKS_SSH_HOST:-}" ] || continue
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
          "$HERE/systemd/$unit.service" > "$TARGET/$unit.service"
        ;;
      description-postgres)
        [ -n "${POSTGRES_ROOT:-}" ] || continue
        case "${POSTGRES_PORT:?set POSTGRES_PORT}" in
          *[!0-9]*|"") echo 'POSTGRES_PORT must be a TCP port' >&2; exit 1 ;;
        esac
        sed -e "s#@POSTGRES_ROOT@#$POSTGRES_ROOT#g" \
            -e "s#@POSTGRES_PORT@#$POSTGRES_PORT#g" \
            -e "s#@POSTGRES_MAJOR@#${POSTGRES_MAJOR:-14}#g" \
          "$HERE/systemd/$unit.service" > "$TARGET/$unit.service"
        ;;
      *)
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
        ;;
    esac
  done < <(unit_manifest services)
}

# Stop verification (stop-executors): the unit must be provably not running - exactly
# "inactive", or "failed" (not running either way). An unreadable or transient state is a
# refusal, so a swallowed or half-completed stop can never be recorded as a successful one.
require_stopped() {
  local unit="$1" state
  state="$(systemctl --user is-active "$unit" 2>/dev/null || true)"
  case "$state" in
    inactive|failed) echo "services.sh: $unit stopped (active_state=$state)"; return 0 ;;
  esac
  echo "services.sh: $unit is still ${state:-unknown} after the stop" >&2
  return 1
}

case "$ACTION" in
  render) render_units ;;
  start) for u in "${UNITS[@]}"; do systemctl --user enable --now "$u"; done ;;
  stop) for ((i=${#UNITS[@]}-1; i>=0; i--)); do systemctl --user disable --now "${UNITS[$i]}"; done ;;
  stop-executors)
    for ((i=${#EXECUTORS[@]}-1; i>=0; i--)); do systemctl --user disable --now "${EXECUTORS[$i]}"; done
    for u in "${EXECUTORS[@]}"; do require_stopped "$u"; done
    ;;
esac
