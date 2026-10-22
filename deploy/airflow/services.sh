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
# One static role inventory for rendering, lifecycle control, health, recovery and operator
# rendering. It is code data only; no runtime state or installation record is duplicated here.
UNIT_MANIFEST_FILE="$HERE/../systemd-units.tsv"
unit_manifest() {
  local requested="$1"
  [ -f "$UNIT_MANIFEST_FILE" ] && [ ! -L "$UNIT_MANIFEST_FILE" ] || {
    echo "services.sh: unit manifest is missing or a link: $UNIT_MANIFEST_FILE" >&2
    return 1
  }
  local manifest_output status=0
  manifest_output="$(
    awk -F '\t' -v requested="$requested" '
      BEGIN {
        count = split("airflow database executor maintenance-v1 operator services", known, " ")
        for (i = 1; i <= count; i++) known_role[known[i]] = 1
      }
      NF != 3 { bad = 1; exit 2 }
      {
        if ($1 !~ /^description-[a-z0-9][a-z0-9_.-]*$/ || seen[$1]++) {
          bad = 1
          exit 3
        }
        if ($2 == "") { bad = 1; exit 4 }
        delete role_seen
        role_count = split($2, roles, ",")
        has_order = 0
        for (role_index = 1; role_index <= role_count; role_index++) {
          role_name = roles[role_index]
          if (!(role_name in known_role) || role_seen[role_name]++) {
            bad = 1
            exit 5
          }
          if (role_name == "maintenance-v1") has_order = 1
        }
        if (has_order) {
          if ($3 !~ /^[1-9][0-9]*$/ || order_seen[$3]++) {
            bad = 1
            exit 6
          }
          ordered_count++
        } else if ($3 != "-") {
          bad = 1
          exit 7
        }
        if (requested == "all" || requested in role_seen) {
          selected_count++
          if (requested == "maintenance-v1") printf "%08d\t%s\n", $3, $1
          else print $1
        }
      }
      END {
        if (bad) exit
        if (selected_count == 0) exit 8
        for (i = 1; i <= ordered_count; i++) {
          if (!(i in order_seen)) exit 9
        }
      }
    ' "$UNIT_MANIFEST_FILE"
  )" || status=$?
  if [ "$status" -ne 0 ]; then
    echo "services.sh: unit role is missing or the manifest is invalid: $requested" >&2
    return 1
  fi
  if [ "$requested" = "maintenance-v1" ]; then
    printf '%s\n' "$manifest_output" | LC_ALL=C sort -t $'\t' -k1,1n | cut -f2-
  else
    printf '%s\n' "$manifest_output"
  fi
}
usage() {
  echo "usage: $0 render|start|stop|stop-executors" >&2
  echo "       $0 units all|services|airflow|executor|database|operator|maintenance-v1" >&2
  exit 2
}
case "${1:-}" in
  units)
    [ "$#" -eq 2 ] || usage
    case "$2" in
      all|services|airflow|executor|database|operator|maintenance-v1) ;;
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
load_unit_role() {
  local variable="$1" role="$2" output
  if ! output="$(unit_manifest "$role")"; then
    echo "services.sh: unit role is missing or invalid: $role" >&2
    exit 1
  fi
  mapfile -t "$variable" <<< "$output"
}
load_unit_role EXECUTOR_UNITS executor
EXECUTORS=()
for unit in "${EXECUTOR_UNITS[@]}"; do
  [ "$unit" != "description-solidworks-tunnel" ] || [ -n "${SOLIDWORKS_SSH_HOST:-}" ] || continue
  EXECUTORS+=("$unit")
done
UNITS=()
load_unit_role DATABASE_UNITS database
[ -z "${POSTGRES_ROOT:-}" ] || UNITS+=("${DATABASE_UNITS[@]}")
UNITS+=("${EXECUTORS[@]}")

render_units() {
  local -a render_units=()
  local unit output
  mkdir -p "$TARGET"
  if ! output="$(unit_manifest services)"; then
    echo 'services.sh: unit role is missing or invalid: services' >&2
    exit 1
  fi
  mapfile -t render_units <<< "$output"
  case "${AIRFLOW_API_PORT:?set AIRFLOW_API_PORT (rendered resolved.env)}" in
    *[!0-9]*|"") echo 'AIRFLOW_API_PORT must be a TCP port' >&2; exit 1 ;;
  esac
  for unit in "${render_units[@]}"; do
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
  done
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
