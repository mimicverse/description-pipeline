#!/usr/bin/env bash
set -euo pipefail

action="${1:?source action is required}"
shift
printf 'source_child raw action=%s argv=%s\n' "$action" "$*" >> "$FIXTURE_SOURCE_TRACE"
config=""
keep_database=""
keep_admission_closed=0
reconcile_args=()
while [ "$#" -gt 0 ]; do
  case "$1" in
    --config) config="${2:?}"; shift 2 ;;
    --keep-database) keep_database=1; shift ;;
    --keep-admission-closed) keep_admission_closed=1; shift ;;
    --install-id|--run-id|--stage|--effect-key|--outcome|--evidence)
      reconcile_args+=("$1" "${2:?}")
      shift 2
      ;;
    *) echo "source fixture: unsupported argument: $1" >&2; exit 2 ;;
  esac
done

state_root="${FIXTURE_SOURCE_STATE_ROOT:?source state root is required}"
source_venv="${FIXTURE_SOURCE_VENV:?source runtime is required}"
derived_env="$state_root/derived/gen-1/operator.env"
source_airflow="$FIXTURE_SOURCE_DEPLOYMENT_ROOT/deploy/airflow"

prepare_maintenance_lock() {
  if [ -n "${OPERATORCTL_MAINTENANCE_LOCK_OWNER:-}" ]; then
    return
  fi
  mkdir -p "$state_root/locks"
  exec {lock_fd}>>"$state_root/locks/maintenance.lock"
  flock -n "$lock_fd"
  owner_pid="$BASHPID"
  lock_owner="$("$CONTROL_PYTHON" -c '
import json
import os
import secrets
import sys
from datetime import UTC, datetime
from pathlib import Path

path = Path(sys.argv[1])
owner = secrets.token_hex(32)
document = {
    "schema_version": "solidworks-to-urdf.maintenance-lock/v1",
    "owner_id": owner,
    "acquired_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "pid": int(sys.argv[2]),
}
path.write_text(json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n")
print(owner)
' "$state_root/locks/maintenance.lock" "$owner_pid")"
  OPERATORCTL_MAINTENANCE_LOCK_OWNER="$lock_owner"
  OPERATORCTL_MAINTENANCE_LOCK_FD="$lock_fd"
  OPERATORCTL_MAINTENANCE_LOCK_PATH="$state_root/locks/maintenance.lock"
  export OPERATORCTL_MAINTENANCE_LOCK_OWNER OPERATORCTL_MAINTENANCE_LOCK_FD OPERATORCTL_MAINTENANCE_LOCK_PATH
}

case "$action" in
  drain|stop|start|reconcile) prepare_maintenance_lock ;;
esac

[ "$CONTROL_PYTHON" = "$source_venv/bin/python" ] \
  || { echo "source fixture: parent control interpreter leaked" >&2; exit 70; }
[ -z "${PYTHONPATH:-}" ] \
  || { echo "source fixture: parent PYTHONPATH leaked" >&2; exit 71; }
[ -z "${PLATFORM_SERVICE_DSN:-}" ] \
  || { echo "source fixture: parent service DSN leaked" >&2; exit 72; }
[ -z "${PLATFORM_OPERATOR_DSN:-}" ] \
  || { echo "source fixture: parent operator DSN leaked" >&2; exit 73; }
case "$action" in
  drain|stop|start|reconcile)
    [ -n "${OPERATORCTL_MAINTENANCE_LOCK_OWNER:-}" ] \
      || { echo "source fixture: maintenance lock owner is missing" >&2; exit 74; }
    [ -n "${OPERATORCTL_MAINTENANCE_LOCK_FD:-}" ] \
      || { echo "source fixture: maintenance lock descriptor is missing" >&2; exit 75; }
    [ -e "/proc/self/fd/$OPERATORCTL_MAINTENANCE_LOCK_FD" ] \
      || { echo "source fixture: maintenance lock descriptor was not inherited" >&2; exit 76; }
    ;;
esac
expected_config_sha="${FIXTURE_SOURCE_CONFIG_SHA256:?source config digest is required}"
actual_config_sha="$(sha256sum "$config" | awk '{print $1}')"
printf 'source_child config_sha=%s expected_config_sha=%s\n' \
  "$actual_config_sha" "$expected_config_sha" >> "$FIXTURE_SOURCE_TRACE"
[ "$actual_config_sha" = "$expected_config_sha" ] \
  || { echo "source fixture: config bytes differ from the source binding" >&2; exit 77; }
if [ "${FIXTURE_SOURCE_FAIL_BEFORE_CLOSE:-0}" = "1" ]; then
  echo "source fixture: injected refusal before admission mutation" >&2
  exit 9
fi

printf 'source_child control_python=%s pythonpath=%s service_dsn=%s operator_dsn=%s\n' \
  "$CONTROL_PYTHON" "${PYTHONPATH:-absent}" \
  "${PLATFORM_SERVICE_DSN:+present}" "${PLATFORM_OPERATOR_DSN:+present}" \
  >> "$FIXTURE_SOURCE_TRACE"
if [ -n "${OPERATORCTL_MAINTENANCE_LOCK_OWNER:-}" ]; then
  printf 'maintenance_lock=inherited:%s\n' "$OPERATORCTL_MAINTENANCE_LOCK_PATH"
fi

set -a
source "$state_root/resolved.env"
source "$derived_env"
set +a
[ "$AIRFLOW_VENV" = "$source_venv" ] \
  || { echo "source fixture: derived runtime differs from the bound source" >&2; exit 78; }
printf 'source_child dsn_match=%s runtime_match=true\n' \
  "$([ "$PLATFORM_SERVICE_DSN" = "$EXPECTED_PLATFORM_SERVICE_DSN" ] &&
     [ "$PLATFORM_OPERATOR_DSN" = "$EXPECTED_PLATFORM_DSN" ] && echo true || echo false)" \
  >> "$FIXTURE_SOURCE_TRACE"
[ "$PLATFORM_SERVICE_DSN" = "$EXPECTED_PLATFORM_SERVICE_DSN" ] \
  || { echo "source fixture: derived service DSN differs from the bound source" >&2; exit 77; }
[ "$PLATFORM_OPERATOR_DSN" = "$EXPECTED_PLATFORM_DSN" ] \
  || { echo "source fixture: derived operator DSN differs from the bound source" >&2; exit 77; }

case "$action" in
  status)
    printf 'source_child action=status argv=%s\n' "$*" >> "$FIXTURE_SOURCE_TRACE"
    echo "source status: current generation"
    ;;
  health)
    printf 'source_child action=health argv=%s\n' "$*" >> "$FIXTURE_SOURCE_TRACE"
    if [ "${FIXTURE_SOURCE_HEALTH_RC:-0}" != "0" ]; then
      echo "source fixture: injected health failure" >&2
      exit "$FIXTURE_SOURCE_HEALTH_RC"
    fi
    echo "source health: current generation"
    ;;
  start)
    set -a
    source "$state_root/resolved.env"
    source "$derived_env"
    set +a
    printf 'source_child action=start argv=%s\n' "$*" >> "$FIXTURE_SOURCE_TRACE"
    bash "$source_airflow/services.sh" start
    systemctl --user start description-postgres.service description-portal.service description-operator-proxy.service
    if [ "$keep_admission_closed" = "0" ]; then
      AIRFLOW_HOME="$AIRFLOW_HOME" "$source_venv/bin/airflow" dags unpause solidworks_to_urdf
      PLATFORM_SERVICE_DSN="$PLATFORM_SERVICE_DSN" "$source_venv/bin/python" - <<'PY'
# platform-open-admission
PY
      echo "source start complete: admission open"
    else
      echo "source start complete: services are running; admission stays closed"
    fi
    ;;
  drain)
    printf 'source_child action=drain argv=%s\n' "$*" >> "$FIXTURE_SOURCE_TRACE"
    printf 'source_child phase=close\n' >> "$FIXTURE_SOURCE_TRACE"
    PLATFORM_SERVICE_DSN="$PLATFORM_SERVICE_DSN" "$source_venv/bin/python" - <<'PY'
# platform-close-admission
PY
    printf 'source_child phase=dag-pause\n' >> "$FIXTURE_SOURCE_TRACE"
    AIRFLOW_HOME="$AIRFLOW_HOME" "$source_venv/bin/airflow" dags pause solidworks_to_urdf
    printf 'source_child phase=drain-projection\n' >> "$FIXTURE_SOURCE_TRACE"
    PLATFORM_OPERATOR_DSN="$PLATFORM_OPERATOR_DSN" "$source_venv/bin/python" \
      -m description_pipeline.orchestration.lifecycle_state report --require-no-unresolved
    ;;
  stop)
    [ "$keep_database" = "1" ] \
      || { echo "source fixture: stop must retain the database" >&2; exit 79; }
    [ "$(cat "$ADMISSION")" = "true" ] \
      || { echo "source fixture: stop requires a closed admission" >&2; exit 80; }
    bash "$source_airflow/services.sh" stop-executors
    systemctl --user stop description-portal.service description-operator-proxy.service
    [ "$(systemctl --user is-active description-postgres.service)" = "active" ] \
      || { echo "source fixture: database is not active" >&2; exit 81; }
    echo "stop complete: supported source writers stopped; PostgreSQL remains active"
    if [ "${FIXTURE_SOURCE_STOP_RC:-0}" != "0" ]; then
      echo "source fixture: injected stop refusal" >&2
      exit "$FIXTURE_SOURCE_STOP_RC"
    fi
    ;;
  reconcile)
    printf 'source_child action=reconcile argv=%s\n' "${reconcile_args[*]}" \
      >> "$FIXTURE_SOURCE_TRACE"
    echo "source reconcile: delegated argv accepted"
    ;;
  *)
    echo "source fixture: unsupported source action: $action" >&2
    exit 2
    ;;
esac
