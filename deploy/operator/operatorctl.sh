#!/usr/bin/env bash
# The first-install operator lifecycle:
#   validate  validate platform.json without side effects
#   install   install one clean Linux/Windows generation and its database
#   start     start the installed generation and open admission after verification
#   drain     close admission, stop new Windows work, and report remaining work
#   stop      stop both ends after a settled drain; --keep-database retains PostgreSQL
#   status    report drift, managed units and the Windows endpoint identity
#   health    run the full Linux and endpoint health contract
#   reconcile reconcile one current business effect
#
# There is no upgrade, rollback, source-generation or previous-package operation.
set -euo pipefail
export PYTHONDONTWRITEBYTECODE=1
CONTROL_PYTHON="${CONTROL_PYTHON:-python3}"
if ! "$CONTROL_PYTHON" -I -B -c \
    'import sys; raise SystemExit(0 if sys.implementation.name == "cpython" and sys.version_info[:2] == (3, 12) else 1)' \
    2>/dev/null; then
  echo "operatorctl: CONTROL_PYTHON must be CPython 3.12" >&2
  exit 1
fi
export CONTROL_PYTHON
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OPERATOR_PROGRAM=operatorctl
source "$HERE/environment.sh"
AIRFLOW_HERE="$HERE/../airflow"
RELEASE_ROOT="$(cd "$HERE/../.." && pwd -P)"
WINDOWS_LIFECYCLE_TIMEOUT=480

unit_manifest() {
  bash "$AIRFLOW_HERE/services.sh" units "$1"
}
load_unit_role() {
  local variable="$1" role="$2" output
  if ! output="$(unit_manifest "$role")" || [ -z "$output" ]; then
    die "could not read the managed systemd unit role: $role"
  fi
  mapfile -t "$variable" <<< "$output"
}
load_unit_role UNITS all
load_unit_role AIRFLOW_UNITS airflow
load_unit_role EXECUTOR_UNITS executor
load_unit_role DATABASE_UNITS database
load_unit_role OPERATOR_UNITS operator
[ "${#DATABASE_UNITS[@]}" -eq 1 ] || die "the managed database role must name exactly one unit"
OPERATOR_UNIT_FILES=()
for unit in "${OPERATOR_UNITS[@]}"; do OPERATOR_UNIT_FILES+=("$unit.service"); done
EXECUTOR_UNIT_FILES=()
for unit in "${EXECUTOR_UNITS[@]}"; do EXECUTOR_UNIT_FILES+=("$unit.service"); done
DATABASE_UNIT_FILE="${DATABASE_UNITS[0]}.service"
operator_units_enable() {
  systemctl --user enable --now "${OPERATOR_UNIT_FILES[@]}"
}
operator_units_active() {
  systemctl --user is-active --quiet "${OPERATOR_UNIT_FILES[@]}"
}
operator_units_disable() {
  local index rc=0
  local -a reversed=()
  for ((index=${#OPERATOR_UNIT_FILES[@]}-1; index>=0; index--)); do
    reversed+=("${OPERATOR_UNIT_FILES[$index]}")
  done
  for unit in "${reversed[@]}"; do
    systemctl --user disable --now "$unit" || rc=$?
  done
  return "$rc"
}

GENERATION_ENV_REQUIRED=(
  AIRFLOW_DB_URL AIRFLOW_HOME AIRFLOW_RUNTIME_DB_URL AIRFLOW_VENV
  PIPELINE_WORK_ROOT PLATFORM_ARTIFACT_ROOT PLATFORM_CONFIG_SNAPSHOT
  OPERATOR_STATE PIPELINE_WHEEL PLATFORM_CONFIG_DIGEST
  PLATFORM_OPERATOR_DSN PLATFORM_SERVICE_DSN POSTGRES_PORT POSTGRES_ROOT
  SOLIDWORKS_HANDOFF_ROOT SOLIDWORKS_SSH_HOST
)

CONFIG_SNAPSHOT_DIR=""
CONFIG_FILE=""
RESOLVED_CONFIG=""
RENDER_LOG=""
TOOLCHAIN_LOG=""
INSTALL_WHEELHOUSE=""
PREPARED_INSTALL_DIR=""
INSTALL_DATABASE_STARTED=""
INSTALL_COMMITTED=""
cleanup_operatorctl() {
  [ -z "$INSTALL_WHEELHOUSE" ] || rm -rf -- "$INSTALL_WHEELHOUSE" 2>/dev/null || true
  [ -z "$PREPARED_INSTALL_DIR" ] || rm -rf -- "$PREPARED_INSTALL_DIR" 2>/dev/null || true
  [ -z "$RENDER_LOG" ] || rm -f -- "$RENDER_LOG" 2>/dev/null || true
  [ -z "$TOOLCHAIN_LOG" ] || rm -f -- "$TOOLCHAIN_LOG" 2>/dev/null || true
  [ -z "$RESOLVED_CONFIG" ] || rm -f -- "$RESOLVED_CONFIG" 2>/dev/null || true
  [ -z "$CONFIG_FILE" ] || rm -f -- "$CONFIG_FILE" 2>/dev/null || true
  [ -z "$CONFIG_SNAPSHOT_DIR" ] || rmdir -- "$CONFIG_SNAPSHOT_DIR" 2>/dev/null || true
  if [ -n "$INSTALL_DATABASE_STARTED" ] && [ -z "$INSTALL_COMMITTED" ] \
      && command -v systemctl >/dev/null 2>&1; then
    systemctl --user stop "${DATABASE_UNITS[0]}.service" >/dev/null 2>&1 || true
  fi
}
trap cleanup_operatorctl EXIT

usage() {
  cat >&2 <<'USAGE'
usage: operatorctl.sh <validate|install|start|drain|stop|status|health|reconcile> --config FILE
install inputs: [--install-id ID] [--wheel FILE] [--release-file FILE]
start inputs: [--keep-admission-closed]
stop inputs: [--keep-database]
reconcile inputs: --run-id R --stage STAGE --effect-key KEY
exit codes: 0 ok; 1 configuration/environment error; 2 usage
USAGE
  exit 2
}

ACTION="${1:-}"
shift || true
CONFIG_SOURCE_FILE=""
INSTALL_ID=""
WHEEL_FILE=""
RELEASE_FILE=""
RUN_ID=""
STAGE=""
EFFECT_KEY=""
KEEP_ADMISSION_CLOSED=""
KEEP_DATABASE=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --config) CONFIG_SOURCE_FILE="${2:-}"; shift 2 ;;
    --install-id) INSTALL_ID="${2:-}"; shift 2 ;;
    --wheel) WHEEL_FILE="${2:-}"; shift 2 ;;
    --release-file) RELEASE_FILE="${2:-}"; shift 2 ;;
    --run-id) RUN_ID="${2:-}"; shift 2 ;;
    --stage) STAGE="${2:-}"; shift 2 ;;
    --effect-key) EFFECT_KEY="${2:-}"; shift 2 ;;
    --keep-admission-closed) KEEP_ADMISSION_CLOSED=1; shift ;;
    --keep-database) KEEP_DATABASE=1; shift ;;
    *) usage ;;
  esac
done
[ -n "$ACTION" ] || usage
case "$ACTION" in
  validate|install|start|drain|stop|status|health|reconcile) ;;
  *) usage ;;
esac
[ -n "$CONFIG_SOURCE_FILE" ] || usage
if [ -n "$KEEP_ADMISSION_CLOSED" ] && [ "$ACTION" != "start" ]; then
  echo "operatorctl: --keep-admission-closed is only accepted by start" >&2
  exit 2
fi
if [ -n "$KEEP_DATABASE" ] && [ "$ACTION" != "stop" ]; then
  echo "operatorctl: --keep-database is only accepted by stop" >&2
  exit 2
fi
if [ -n "$INSTALL_ID$WHEEL_FILE$RELEASE_FILE" ] && [ "$ACTION" != "install" ]; then
  usage
fi
if [ -n "$RUN_ID$STAGE$EFFECT_KEY" ] && [ "$ACTION" != "reconcile" ]; then
  usage
fi
if [ -n "$INSTALL_ID" ]; then
  "$CONTROL_PYTHON" "$HERE/platform_config.py" validate-install-id \
    --install-id "$INSTALL_ID" >/dev/null \
    || die "install-id is malformed"
fi

snapshot_config() {
  local attempt
  [ -f "$CONFIG_SOURCE_FILE" ] && [ ! -L "$CONFIG_SOURCE_FILE" ] \
    || die "config file not found or is a link: $CONFIG_SOURCE_FILE"
  CONFIG_SNAPSHOT_DIR="$(mktemp -d)" || die "cannot create the private configuration snapshot"
  chmod 700 "$CONFIG_SNAPSHOT_DIR"
  CONFIG_FILE="$CONFIG_SNAPSHOT_DIR/platform.json"
  for attempt in 1 2 3; do
    cp -- "$CONFIG_SOURCE_FILE" "$CONFIG_FILE" \
      || die "cannot snapshot the configuration"
    chmod 600 "$CONFIG_FILE"
    if cmp -s -- "$CONFIG_SOURCE_FILE" "$CONFIG_FILE"; then
      return 0
    fi
  done
  die "the configuration changed while it was being snapshotted"
}

resolve_config() {
  local output
  RESOLVED_CONFIG="$(mktemp)" || die "cannot create the resolved config probe"
  "$CONTROL_PYTHON" "$HERE/platform_config.py" resolve --config "$CONFIG_FILE" \
    > "$RESOLVED_CONFIG" || exit $?
  output="$(mktemp)"
  if ! "$CONTROL_PYTHON" - "$RESOLVED_CONFIG" > "$output" <<'PY'
import json
import sys
from pathlib import Path

document = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
storage = document["storage"]
windows = document["windows"]
instance = windows["instance"]
for value in (
    storage["state_root"],
    storage["release_root"],
    windows["ssh_host"],
    instance["state_root"],
):
    if not isinstance(value, str) or "\n" in value:
        raise SystemExit("the resolved configuration carries an invalid path or identity")
    print(value)
PY
  then
    rm -f "$output"
    die "the resolved configuration is unreadable"
  fi
  local -a values=()
  mapfile -t values < "$output"
  rm -f "$output"
  [ "${#values[@]}" -eq 4 ] || die "the resolved configuration is incomplete"
  STATE_ROOT="${values[0]}"
  RELEASE_TARGET_ROOT="${values[1]}"
  SOLIDWORKS_SSH_HOST="${values[2]}"
  WINDOWS_STATE_ROOT="${values[3]}"
}

validate_lifecycle_lock() {
  local descriptor="$1" lock_path="$2" expected_owner="$3" mode="$4" owner_pid="$5"
  "$CONTROL_PYTHON" - "$descriptor" "$lock_path" "$expected_owner" "$mode" "$owner_pid" <<'PY'
import json
import os
import re
import secrets
import stat
import sys
from datetime import UTC, datetime
from pathlib import Path

descriptor = int(sys.argv[1])
path = Path(sys.argv[2])
expected_owner = sys.argv[3]
mode = sys.argv[4]
owner_pid = int(sys.argv[5])
if descriptor < 3:
    raise SystemExit("the maintenance lock descriptor is reserved")
if mode not in {"inherited", "new"}:
    raise SystemExit("the maintenance lock mode is invalid")
try:
    inherited = os.fstat(descriptor)
    metadata = os.open(path, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        current = os.fstat(metadata)
        if (
            not stat.S_ISREG(inherited.st_mode)
            or not stat.S_ISREG(current.st_mode)
            or stat.S_IMODE(current.st_mode) != 0o600
            or (inherited.st_dev, inherited.st_ino) != (current.st_dev, current.st_ino)
        ):
            raise SystemExit("the descriptor does not name the maintenance lock")
        fdinfo = Path(f"/proc/self/fdinfo/{descriptor}").read_text(encoding="utf-8")
        if not re.search(
            r"^lock:\s+\d+:\s+FLOCK\s+ADVISORY\s+WRITE\b",
            fdinfo,
            flags=re.MULTILINE,
        ):
            raise SystemExit("the descriptor does not itself hold the write lock")
        if mode == "inherited":
            document = json.loads(os.read(metadata, 65536).decode("utf-8"))
            if (
                not isinstance(document, dict)
                or set(document)
                != {"schema_version", "owner_id", "acquired_at", "pid"}
                or document.get("schema_version")
                != "solidworks-to-urdf.maintenance-lock/v1"
                or document.get("owner_id") != expected_owner
                or type(document.get("pid")) is not int
                or document["pid"] <= 0
            ):
                raise SystemExit("the inherited maintenance lock metadata is invalid")
        else:
            if owner_pid <= 0:
                raise SystemExit("the maintenance lock owner pid is invalid")
            owner = secrets.token_hex(32)
            payload = {
                "schema_version": "solidworks-to-urdf.maintenance-lock/v1",
                "owner_id": owner,
                "acquired_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "pid": owner_pid,
            }
            encoded = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
            os.ftruncate(metadata, 0)
            os.lseek(metadata, 0, os.SEEK_SET)
            os.write(metadata, encoded)
            os.fsync(metadata)
    finally:
        os.close(metadata)
except (OSError, ValueError, TypeError) as error:
    raise SystemExit(f"maintenance lock validation failed: {error}")
PY
}

acquire_lifecycle_lock() {
  local lock_dir="$STATE_ROOT/locks" lock_path="$STATE_ROOT/locks/maintenance.lock"
  local inherited_owner inherited_fd inherited_path lock_fd
  [ -d "$STATE_ROOT" ] && [ ! -L "$STATE_ROOT" ] \
    || die "the install state root is missing or a link: $STATE_ROOT"
  mkdir -p "$lock_dir"
  [ -d "$lock_dir" ] && [ ! -L "$lock_dir" ] \
    || die "the lifecycle lock directory is missing or a link"
  chmod 700 "$lock_dir"
  [ ! -L "$lock_path" ] || die "the lifecycle lock is a link: $lock_path"

  inherited_owner="${OPERATORCTL_MAINTENANCE_LOCK_OWNER:-}"
  inherited_fd="${OPERATORCTL_MAINTENANCE_LOCK_FD:-}"
  inherited_path="${OPERATORCTL_MAINTENANCE_LOCK_PATH:-}"
  if [ -n "$inherited_owner$inherited_fd$inherited_path" ]; then
    [ -n "$inherited_owner" ] && [ -n "$inherited_fd" ] && [ -n "$inherited_path" ] \
      || die "the inherited maintenance lock environment is incomplete"
    [[ "$inherited_owner" =~ ^[0-9a-f]{64}$ ]] \
      || die "the inherited maintenance lock owner is malformed"
    [[ "$inherited_fd" =~ ^[0-9]+$ ]] \
      || die "the inherited maintenance lock descriptor is malformed"
    [ "$inherited_path" = "$lock_path" ] \
      || die "the inherited maintenance lock path differs from the state root"
    if ! validate_lifecycle_lock "$inherited_fd" "$lock_path" "$inherited_owner" inherited 0; then
      die "the inherited maintenance lock is invalid"
    fi
    printf 'maintenance_lock=inherited:%s\n' "$lock_path"
    return 0
  fi

  command -v flock >/dev/null 2>&1 || die "flock is required for lifecycle operations"
  exec {lock_fd}>>"$lock_path"
  flock -n "$lock_fd" \
    || die "another supported lifecycle operation owns the lock: $lock_path"
  chmod 600 "$lock_path"
  if ! validate_lifecycle_lock "$lock_fd" "$lock_path" "" new "$BASHPID"; then
    die "the maintenance lock metadata could not be written"
  fi
  printf 'maintenance_lock=%s\n' "$lock_path"
}

resolve_release_file() {
  local candidate="${RELEASE_FILE:-$RELEASE_ROOT/release.json}"
  "$CONTROL_PYTHON" - "$RELEASE_ROOT" "$candidate" <<'PY'
import os
import sys

root = os.path.realpath(sys.argv[1])
path = os.path.realpath(sys.argv[2])
expected = os.path.join(root, "release.json")
if path != expected or not os.path.isfile(path):
    raise SystemExit(f"the release manifest must be {expected}")
print(path)
PY
  RELEASE_FILE="$("$CONTROL_PYTHON" -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$candidate")"
}

clean_or_absent() {
  local path="$1" label="$2" first
  if [ -L "$path" ]; then
    die "$label is a link: $path"
  fi
  if [ ! -e "$path" ]; then
    return 0
  fi
  [ -d "$path" ] || die "$label is not a directory: $path"
  first="$(find "$path" -mindepth 1 -maxdepth 1 -print -quit)" \
    || die "$label could not be inspected: $path"
  [ -z "$first" ] || die "$label contains unknown residual state: $path"
}

install_state_inventory() {
  local mode="$1"
  "$CONTROL_PYTHON" - "$STATE_ROOT" "$mode" <<'PY'
import os
import stat
import sys
from pathlib import Path

root = Path(sys.argv[1])
mode = sys.argv[2]
if mode not in {"prelock", "postlock"}:
    raise SystemExit("operatorctl: invalid install-state inventory mode")
if root.is_symlink() or not root.is_dir():
    raise SystemExit(f"operatorctl: install state root is missing, linked, or not a directory: {root}")
entries = {entry.name: entry for entry in os.scandir(root)}
unexpected = sorted(set(entries) - {"locks"})
if unexpected:
    raise SystemExit(f"operatorctl: state root contains unknown residual state: {unexpected}")
if "locks" not in entries:
    if mode == "postlock":
        raise SystemExit("operatorctl: the maintenance lock directory is missing")
    raise SystemExit(0)
locks = Path(entries["locks"].path)
if locks.is_symlink() or not locks.is_dir():
    raise SystemExit(f"operatorctl: lifecycle lock directory is linked or invalid: {locks}")
if mode == "prelock":
    raise SystemExit(0)
lock_entries = {entry.name: entry for entry in os.scandir(locks)}
if set(lock_entries) != {"maintenance.lock"}:
    raise SystemExit(
        f"operatorctl: lifecycle lock directory contains unknown residual state: {sorted(lock_entries)}"
    )
lock = Path(lock_entries["maintenance.lock"].path)
try:
    metadata = lock.lstat()
except OSError as error:
    raise SystemExit(f"operatorctl: lifecycle lock cannot be inspected: {error}")
if (
    not stat.S_ISREG(metadata.st_mode)
    or stat.S_IMODE(metadata.st_mode) != 0o600
    or stat.S_IMODE(locks.stat().st_mode) != 0o700
):
    raise SystemExit(f"operatorctl: lifecycle lock permissions are invalid: {lock}")
PY
}

prepare_install_state_root() {
  [ ! -L "$STATE_ROOT" ] || die "install state root is a link: $STATE_ROOT"
  if [ ! -e "$STATE_ROOT" ]; then
    mkdir -m 700 -- "$STATE_ROOT" \
      || [ -d "$STATE_ROOT" ] \
      || die "could not create the install state root: $STATE_ROOT"
  fi
  [ -d "$STATE_ROOT" ] || die "install state root is not a directory: $STATE_ROOT"
  install_state_inventory prelock
}

check_first_install_business_roots() {
  local -a roots=()
  local output
  output="$(
    "$CONTROL_PYTHON" - "$RESOLVED_CONFIG" <<'PY'
import json
import sys
from pathlib import Path

document = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
for name in ("handoff_root", "artifact_root", "workspace_root"):
    print(document["storage"][name])
PY
  )" || die "the first-install business roots are unreadable"
  mapfile -t roots <<< "$output"
  [ "${#roots[@]}" -eq 3 ] || die "the first-install business roots are incomplete"
  clean_or_absent "${roots[0]}" "handoff root"
  clean_or_absent "${roots[1]}" "artifact root"
  clean_or_absent "${roots[2]}" "workspace root"
}

first_install_preflight() {
  local target="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user" unit
  install_state_inventory postlock
  clean_or_absent "$RELEASE_TARGET_ROOT" "runtime release root"
  check_first_install_business_roots
  for unit in "${UNITS[@]}"; do
    if [ -e "$target/$unit.service" ] || [ -L "$target/$unit.service" ]; then
      die "managed unit already exists before first install: $target/$unit.service"
    fi
  done
}

require_units_stopped() {
  local label="$1" unit state rc=0
  shift
  for unit in "$@"; do
    state="$(systemctl --user is-active "$unit" 2>/dev/null || true)"
    case "$state" in
      inactive|failed) ;;
      *)
        echo "operatorctl: $label unit $unit is still ${state:-unknown} after stop" >&2
        rc=1
        ;;
    esac
  done
  return "$rc"
}

ensure_proxy_binary() {
  NGINX_BIN="${NGINX_BIN:-}" OPERATOR_STATE="${OPERATOR_STATE:-}" \
    bash "$HERE/scripts/ensure_proxy_binary.sh"
}

check_proxy_binary() {
  NGINX_BIN="${NGINX_BIN:-}" bash "$HERE/scripts/check_proxy_binary.sh" "$@"
}

ensure_postgres_ready() {
  local database_unit="${1:-${DATABASE_UNITS[0]}}"
  PG_BIN="$POSTGRES_ROOT/root/usr/lib/postgresql/${POSTGRES_MAJOR:-14}/bin"
  export LD_LIBRARY_PATH="$POSTGRES_ROOT/root/usr/lib/x86_64-linux-gnu:${LD_LIBRARY_PATH:-}"
  systemctl --user daemon-reload
  systemctl --user start "$database_unit.service" \
    || die "$database_unit.service failed to start"
  local ready=0
  for _ in $(seq 1 20); do
    if "$PG_BIN/pg_isready" -q -h "$POSTGRES_ROOT/socket" -p "$POSTGRES_PORT"; then
      ready=1
      break
    fi
    sleep 0.5
  done
  [ "$ready" = "1" ] || die "PostgreSQL did not become ready on $POSTGRES_ROOT/socket"
}

airflow_render() {
  [ -n "$ENV_FILE" ] || die "ENV_FILE is not selected"
  OPERATOR_DERIVED_ENV="$ENV_FILE" \
  AIRFLOW_VENV="$AIRFLOW_VENV" AIRFLOW_HOME="$AIRFLOW_HOME" \
  POSTGRES_ROOT="${POSTGRES_ROOT:-}" POSTGRES_MAJOR="${POSTGRES_MAJOR:-14}" \
  SOLIDWORKS_SSH_HOST="${SOLIDWORKS_SSH_HOST:-}" \
  AIRFLOW_API_PORT="$AIRFLOW_API_PORT" POSTGRES_PORT="$POSTGRES_PORT" \
  TUNNEL_LOOPBACK_PORT="$TUNNEL_LOOPBACK_PORT" \
  SOLIDWORKS_ENDPOINT_PORT="$SOLIDWORKS_ENDPOINT_PORT" \
  FEISHU_ENV_FILE="${FEISHU_ENV_FILE:-$AIRFLOW_HOME/feishu.env}" \
    bash "$AIRFLOW_HERE/services.sh" render
}

airflow_lifecycle() {
  AIRFLOW_VENV="$AIRFLOW_VENV" AIRFLOW_HOME="$AIRFLOW_HOME" \
  POSTGRES_ROOT="${POSTGRES_ROOT:-}" POSTGRES_MAJOR="${POSTGRES_MAJOR:-14}" \
  SOLIDWORKS_SSH_HOST="${SOLIDWORKS_SSH_HOST:-}" \
  AIRFLOW_API_PORT="$AIRFLOW_API_PORT" POSTGRES_PORT="$POSTGRES_PORT" \
  TUNNEL_LOOPBACK_PORT="$TUNNEL_LOOPBACK_PORT" \
  SOLIDWORKS_ENDPOINT_PORT="$SOLIDWORKS_ENDPOINT_PORT" \
  FEISHU_ENV_FILE="${FEISHU_ENV_FILE:-$AIRFLOW_HOME/feishu.env}" \
    bash "$AIRFLOW_HERE/services.sh" "$1"
}

airflow_wait_dag_identity() {
  local attempt result=""
  for attempt in $(seq 1 60); do
    if result="$(
      AIRFLOW_HOME="$AIRFLOW_HOME" "$AIRFLOW_VENV/bin/python" "$HERE/runtime_dag.py" \
        --state-root "$STATE_ROOT" --airflow-home "$AIRFLOW_HOME" 2>&1
    )"; then
      printf '%s\n' "$result"
      return 0
    fi
    sleep 2
  done
  printf '%s\n' "${result:-Airflow did not serialize the installed DAG}" >&2
  return 1
}

airflow_set_dag_paused() {
  local paused="$1" verb
  if [ "$paused" = "true" ]; then verb=pause; else verb=unpause; fi
  AIRFLOW_HOME="$AIRFLOW_HOME" "$AIRFLOW_VENV/bin/airflow" dags "$verb" solidworks_to_urdf
}

verify_airflow_connection() {
  AIRFLOW_HOME="$AIRFLOW_HOME" "$AIRFLOW_VENV/bin/python" \
    -m description_pipeline.orchestration.airflow_client \
    --conn-id solidworks_windows --state-root "$STATE_ROOT"
}

platform_close_admission() {
  local reason="${1:-drain}" actor="${2:-operatorctl drain}"
  [ -n "${PLATFORM_SERVICE_DSN:-}" ] || die "PLATFORM_SERVICE_DSN is missing"
  PLATFORM_CLOSE_REASON="$reason" PLATFORM_CLOSE_ACTOR="$actor" \
    "$AIRFLOW_VENV/bin/python" - <<'PY'
import os

import psycopg2

from description_pipeline.platform.facts import AdmissionControlStore


def connect():
    return psycopg2.connect(os.environ["PLATFORM_SERVICE_DSN"])


view = AdmissionControlStore(connect).close_admission(
    reason=os.environ["PLATFORM_CLOSE_REASON"],
    actor=os.environ["PLATFORM_CLOSE_ACTOR"],
)
print(f"admission={view.state} revision={view.revision}")
PY
}

platform_open_admission() {
  [ -n "${PLATFORM_SERVICE_DSN:-}" ] || die "PLATFORM_SERVICE_DSN is missing"
  "$AIRFLOW_VENV/bin/python" - <<'PY'
import os

import psycopg2

from description_pipeline.platform.facts import AdmissionControlStore


def connect():
    return psycopg2.connect(os.environ["PLATFORM_SERVICE_DSN"])


view = AdmissionControlStore(connect).open_admission(
    reason="start",
    actor="operatorctl start",
)
print(f"admission={view.state} revision={view.revision}")
PY
}

load_generation_env() {
  [ -n "$ENV_FILE" ] || die "generation environment path is not selected"
  unset "${GENERATION_ENV_REQUIRED[@]}"
  load_env_file "$ENV_FILE" || return $?
  for key in "${GENERATION_ENV_REQUIRED[@]}"; do
    [ -n "${!key:-}" ] || die "generation environment $ENV_FILE does not define $key"
  done
}

load_installed() {
  local verified
  resolve_config
  "$CONTROL_PYTHON" "$HERE/install_record.py" verify-deployment \
    --release-file "$HERE/../../release.json" --tree "$RELEASE_ROOT" >/dev/null \
    || exit $?
  load_current_environment "$STATE_ROOT"
  load_generation_env
  TARGET="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
  verified="$(
    AIRFLOW_HOME="$AIRFLOW_HOME" "$AIRFLOW_VENV/bin/python" "$HERE/install_record.py" \
      verify-active --config "$CONFIG_FILE" --state-root "$STATE_ROOT" \
        --release-file "$HERE/../../release.json" \
      | sed -n 's/^install_id=//p'
  )" || exit $?
  [ "$verified" = "$ACTIVE_ID" ] \
    || die "the install record changed while its environment was verified"
}


windows_lifecycle() {
  local output="$1" script_path="$2" binding_path="$3" verb="$4"
  "$CONTROL_PYTHON" - "$script_path" "$binding_path" "$verb" <<'PY' \
    | windows_transport "$WINDOWS_LIFECYCLE_TIMEOUT" "$output.transport.json" \
      > "$output" 2> "$output.stderr"
import sys


def path(value):
    return value.replace("/", "\\")


def quote(value):
    return "'" + path(value).replace("'", "''") + "'"


script, binding, verb = sys.argv[1:]
if verb not in {"start", "drain", "stop", "status", "health"}:
    raise SystemExit("operatorctl: invalid Windows lifecycle verb")
print(f"& {quote(script)} -Verb {verb} -Binding {quote(binding)}")
PY
}

windows_transport() {
  local timeout="${1:-60}" metadata="${2:-}"
  local -a arguments=(--host "$SOLIDWORKS_SSH_HOST" --timeout "$timeout")
  [ -z "$metadata" ] || arguments+=(--metadata-file "$metadata")
  "$CONTROL_PYTHON" "$HERE/windows_transport.py" "${arguments[@]}"
}

windows_action() {
  local verb="$1" record_file="${2:-}"
  local stamp dir output remote_binding remote_script remote_rc parse_rc
  case "$verb" in
    start|drain|stop|status|health) ;;
    *) die "unsupported Windows lifecycle verb: $verb" ;;
  esac
  stamp="$(date -u +%Y%m%dT%H%M%SZ)-$$-$RANDOM"
  dir="$STATE_ROOT/lifecycle/windows-$verb-$stamp"
  mkdir -p "$dir"
  chmod 700 "$dir"
  output="$dir/windows-$verb.out"
  : > "$output"
  : > "$output.ssh-rc"
  : > "$output.stderr"
  remote_binding="$WINDOWS_STATE_ROOT/windows-binding.json"
  remote_script="$WINDOWS_STATE_ROOT/instance-lifecycle.ps1"
  remote_rc=0
  windows_lifecycle "$output" "$remote_script" "$remote_binding" "$verb" || remote_rc=$?
  printf '%s\n' "$remote_rc" > "$output.ssh-rc"
  parse_rc=0
  verify_windows_result "$output" "$remote_rc" "$verb" "$record_file" || parse_rc=$?
  if [ "$parse_rc" = "5" ]; then
    local status_document="$dir/windows-status.json"
    local identity_document="$dir/windows-endpoint-identity.json"
    [ -f "$status_document" ] \
      || { echo "operatorctl: Windows status document is missing" >&2; return 2; }
    "$AIRFLOW_VENV/bin/python" "$HERE/install_record.py" endpoint-identity \
      --status "$status_document" --release-file "$HERE/../../release.json" \
      --out "$identity_document" >/dev/null \
      || { echo "operatorctl: Windows endpoint status identity is invalid" >&2; return 1; }
    if [ -n "$record_file" ] && [ "$record_file" != "-" ]; then
      "$CONTROL_PYTHON" - "$identity_document" "$record_file" <<'PY' \
        || { echo "operatorctl: Windows health identity differs from the install record" >&2; return 1; }
import json
import sys

identity = json.load(open(sys.argv[1], encoding="utf-8"))
record = json.load(open(sys.argv[2], encoding="utf-8"))
raise SystemExit(0 if identity == record["ends"]["windows"] else 1)
PY
    fi
    parse_rc=0
  fi
  case "$parse_rc" in
    0) printf '%s\n' "$output" ;;
    1) echo "operatorctl: Windows $verb was refused; see $output" >&2; return 1 ;;
    *) echo "operatorctl: Windows $verb outcome is unknown; see $output" >&2; return 2 ;;
  esac
}

verify_windows_result() {
  local output="$1" remote_rc="$2" verb="$3" record_file="$4"
  local -a arguments=(
    verify-windows-result --result "$output" --remote-rc "$remote_rc" --verb "$verb"
  )
  if [ -n "$record_file" ] && [ "$record_file" != "-" ]; then
    arguments+=(--install-record "$record_file")
  fi
  "$AIRFLOW_VENV/bin/python" "$HERE/install_record.py" "${arguments[@]}"
}

drift_check() {
  local rc=0 tmp_xdg drift_log tmp_state proxy_bin="${NGINX_BIN:-}" extra=() proxy_issue
  if ! proxy_issue="$(check_proxy_binary --unit-file "$TARGET/description-operator-proxy.service" 2>&1)"; then
    echo "DRIFT $proxy_issue"
    rc=1
  fi
  [ -n "$proxy_bin" ] && extra=(--nginx-bin "$proxy_bin")
  drift_log="$(mktemp)"
  tmp_xdg="$(mktemp -d)"
  if ! "$CONTROL_PYTHON" "$HERE/render_operator.py" --env-file "$ENV_FILE" --dry-run \
      "${extra[@]}" --units-dir "$tmp_xdg/systemd/user" > "$drift_log"; then
    rc=1
  fi
  grep -v -e '^DRIFT none$' -e '^OPERATOR_UNITS=' "$drift_log" || true
  tmp_state="$(sed -n 's/^Environment=OPERATOR_STATE=//p' \
    "$tmp_xdg/systemd/user/description-portal.service" 2>/dev/null | head -1)"
  if [ -n "$tmp_state" ] && [ "$tmp_state" != "$OPERATOR_STATE" ]; then
    for unit in "${UNITS[@]}"; do
      [ -f "$tmp_xdg/systemd/user/$unit.service" ] \
        && sed -i "s#$tmp_state#$OPERATOR_STATE#g" "$tmp_xdg/systemd/user/$unit.service"
    done
  fi
  if ! XDG_CONFIG_HOME="$tmp_xdg" airflow_render >/dev/null 2>&1; then
    echo "DRIFT airflow unit render failed"
    rc=1
  fi
  for unit in "${UNITS[@]}"; do
    if [ ! -f "$TARGET/$unit.service" ]; then
      echo "DRIFT unit missing: $unit.service"
      rc=1
    elif [ ! -f "$tmp_xdg/systemd/user/$unit.service" ]; then
      echo "DRIFT unit missing from render: $unit.service"
      rc=1
    elif ! diff -q "$tmp_xdg/systemd/user/$unit.service" "$TARGET/$unit.service" >/dev/null; then
      echo "DRIFT unit differs: $unit.service"
      rc=1
    fi
  done
  "$CONTROL_PYTHON" -c 'import shutil,sys; shutil.rmtree(sys.argv[1], ignore_errors=True)' "$tmp_xdg"
  rm -f "$drift_log"
  return "$rc"
}

install_from_config() {
  local install_output snapshot_digest snapshot_path derived_env proxy_bin stable_wheel
  local windows_output windows_identity services_csv prepared_record
  first_install_preflight
  INSTALL_WHEELHOUSE="$(mktemp -d)"
  "$CONTROL_PYTHON" "$HERE/install_record.py" extract-linux-wheelhouse \
    --release-file "$RELEASE_FILE" --destination "$INSTALL_WHEELHOUSE" || exit $?
  if [ -z "$INSTALL_ID" ]; then
    INSTALL_ID="$("$CONTROL_PYTHON" -c 'import uuid; print(uuid.uuid4().hex)')"
  fi
  if [ -n "$WHEEL_FILE" ]; then
    stable_wheel="$("$CONTROL_PYTHON" "$HERE/install_record.py" stage-wheel \
      --release-file "$RELEASE_FILE" --release-root "$RELEASE_TARGET_ROOT" \
      --wheel "$WHEEL_FILE")" || exit $?
  else
    stable_wheel="$("$CONTROL_PYTHON" "$HERE/install_record.py" stage-wheel \
      --release-file "$RELEASE_FILE" --release-root "$RELEASE_TARGET_ROOT" \
      --tree "$RELEASE_ROOT")" || exit $?
  fi
  [ -f "$stable_wheel" ] || die "the stable runtime wheel was not installed"
  install_output="$(mktemp)"
  "$CONTROL_PYTHON" "$HERE/platform_config.py" render --config "$CONFIG_FILE" \
    --install-id "$INSTALL_ID" --wheel "$stable_wheel" > "$install_output" || exit $?
  snapshot_digest="$(sed -n 's/^config_digest=//p' "$install_output")"
  snapshot_path="$(sed -n 's/^snapshot=//p' "$install_output")"
  rm -f "$install_output"
  [ -n "$snapshot_digest" ] && [ -f "$snapshot_path" ] \
    || die "the configuration snapshot was not written"
  derived_env="$("$CONTROL_PYTHON" "$HERE/platform_env.py" emit --snapshot "$snapshot_path" \
    --install-id "$INSTALL_ID" --tree "$RELEASE_ROOT" --release-file "$RELEASE_FILE" \
    --wheel "$stable_wheel" | sed -n 's/^env=//p')" || exit $?
  [ -f "$derived_env" ] || die "the derived environment was not written"
  ENV_FILE="$derived_env"
  OPERATOR_STATE="$(generation_env_declared_value "$ENV_FILE" OPERATOR_STATE)"
  proxy_bin="$(ensure_proxy_binary)" \
    || die "the managed proxy binary is unavailable"
  RENDER_LOG="$(mktemp)"
  "$CONTROL_PYTHON" "$HERE/render_operator.py" --env-file "$ENV_FILE" \
    --nginx-bin "$proxy_bin" > "$RENDER_LOG"
  cat "$RENDER_LOG"
  load_resolved_env "$OPERATOR_STATE/resolved.env"
  load_generation_env
  for root in "$PLATFORM_ARTIFACT_ROOT" "$PIPELINE_WORK_ROOT" "$SOLIDWORKS_HANDOFF_ROOT"; do
    [ ! -L "$root" ] || die "generation root is a link: $root"
    mkdir -p "$root"
    chmod 700 "$root"
  done
  TARGET="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
  TOOLCHAIN_LOG="$(mktemp)"
  bash "$HERE/scripts/install_toolchain.sh" > "$TOOLCHAIN_LOG"
  cat "$TOOLCHAIN_LOG"
  load_env_file "$TOOLCHAIN_LOG" AIRFLOW_PYTHON
  "$CONTROL_PYTHON" "$HERE/render_operator.py" --env-file "$ENV_FILE" \
    --nginx-bin "$proxy_bin" --units-dir "$TARGET" >/dev/null
  airflow_render
  if [ ! -f "$POSTGRES_ROOT/data/PG_VERSION" ]; then
    POSTGRES_ROOT="$POSTGRES_ROOT" POSTGRES_MAJOR="${POSTGRES_MAJOR:-14}" \
      POSTGRES_PORT="$POSTGRES_PORT" bash "$AIRFLOW_HERE/scripts/install_postgres.sh"
  fi
  ensure_postgres_ready
  INSTALL_DATABASE_STARTED=1
  local db_state
  db_state="$("$PG_BIN/psql" -h "$POSTGRES_ROOT/socket" -p "$POSTGRES_PORT" \
    -U solidworks -d postgres -tAc "select 1 from pg_database where datname='airflow_meta'")" \
    || die "could not inspect airflow_meta"
  [ -z "$db_state" ] \
    || die "airflow_meta already exists without a first-install record; refusing unknown residue"
  "$PG_BIN/createdb" -h "$POSTGRES_ROOT/socket" -p "$POSTGRES_PORT" \
    -U solidworks airflow_meta \
    || die "could not create airflow_meta"
  PLATFORM_PSQL="$PG_BIN/psql" bash "$HERE/../platform/apply.sh" --bootstrap \
    --database airflow_meta --socket "$POSTGRES_ROOT/socket" --port "$POSTGRES_PORT" \
    --user solidworks --airflow-schema "${AIRFLOW_SCHEMA:-airflow}" >&2 \
    || die "platform bootstrap failed"
  AIRFLOW_VENV="$AIRFLOW_VENV" AIRFLOW_HOME="$AIRFLOW_HOME" \
    AIRFLOW_PYTHON="${AIRFLOW_PYTHON:-}" \
    PIP_NO_INDEX=1 PIP_FIND_LINKS="$INSTALL_WHEELHOUSE" \
    PIPELINE_WHEEL="${PIPELINE_WHEEL:?PIPELINE_WHEEL is required}" \
    AIRFLOW_DB_URL="$AIRFLOW_DB_URL" AIRFLOW_RUNTIME_DB_URL="$AIRFLOW_RUNTIME_DB_URL" \
    AIRFLOW_API_PORT="$AIRFLOW_API_PORT" bash "$AIRFLOW_HERE/install.sh"
  PLATFORM_PSQL="$PG_BIN/psql" bash "$HERE/../platform/apply.sh" --airflow-grants \
    --database airflow_meta --socket "$POSTGRES_ROOT/socket" --port "$POSTGRES_PORT" \
    --user solidworks --airflow-schema "${AIRFLOW_SCHEMA:-airflow}" >&2 \
    || die "could not grant the airflow-side boundaries"
  PLATFORM_PSQL="$PG_BIN/psql" bash "$HERE/../platform/apply.sh" \
    --database airflow_meta --socket "$POSTGRES_ROOT/socket" --port "$POSTGRES_PORT" \
    --user platform_migrate --airflow-schema "${AIRFLOW_SCHEMA:-airflow}" >&2 \
    || die "platform apply failed"
  "$AIRFLOW_VENV/bin/python" -c 'import importlib.util, sys; sys.exit(0 if all(
      importlib.util.find_spec(name) for name in
      ("description_pipeline.orchestration.portal", "description_pipeline.orchestration.feishu_auth")) else 1)' \
    || die "the installed wheel does not provide portal and Feishu auth"
  [ -f "$ENDPOINT_TOKEN_FILE" ] || die "endpoint token file missing: $ENDPOINT_TOKEN_FILE"
  verify_runtime_identity "$AIRFLOW_VENV" "$RELEASE_FILE"
  windows_output="$(windows_action status)"
  windows_identity="$(dirname "$windows_output")/windows-endpoint-identity.json"
  [ -f "$windows_identity" ] \
    || die "the installed Windows endpoint returned no usable identity"
  services_csv="$(IFS=,; printf '%s' "${UNITS[*]}")"
  PREPARED_INSTALL_DIR="$(mktemp -d)"
  prepared_record="$PREPARED_INSTALL_DIR/install-record.json"
  "$AIRFLOW_VENV/bin/python" "$HERE/install_record.py" build \
    --state-root "$STATE_ROOT" --install-id "$INSTALL_ID" \
    --snapshot "$snapshot_path" --release-file "$RELEASE_FILE" \
    --endpoint-identity "$windows_identity" --services "$services_csv" \
    --dag "$AIRFLOW_HERE/dags/solidworks_to_urdf.py" \
    --deployment-root "$RELEASE_ROOT" --wheel "$stable_wheel" \
    --prepared-record "$prepared_record"
  AIRFLOW_HOME="$AIRFLOW_HOME" "$AIRFLOW_VENV/bin/python" \
    "$AIRFLOW_HERE/scripts/add_connection.py" \
    --state-root "$STATE_ROOT" --snapshot "$snapshot_path" \
    --record-file "$prepared_record" \
    || die "the Airflow connection binding failed before the active install record was published"
  "$AIRFLOW_VENV/bin/python" "$HERE/install_record.py" activate \
    --record-file "$prepared_record" --state-root "$STATE_ROOT" \
    --wheel "$stable_wheel"
  INSTALL_COMMITTED=1
  echo "install complete: ${OPERATOR_URL:-unknown}"
}

case "$ACTION" in
  validate)
    snapshot_config
    "$CONTROL_PYTHON" "$HERE/platform_config.py" validate --config "$CONFIG_FILE"
    ;;
  install)
    snapshot_config
    resolve_config
    resolve_release_file
    if [ -n "$WHEEL_FILE" ]; then
      "$CONTROL_PYTHON" "$HERE/install_record.py" check --release-file "$RELEASE_FILE" \
        --tree "$RELEASE_ROOT" --wheel "$WHEEL_FILE" >/dev/null || exit $?
    else
      "$CONTROL_PYTHON" "$HERE/install_record.py" check --release-file "$RELEASE_FILE" \
        --tree "$RELEASE_ROOT" >/dev/null || exit $?
    fi
    prepare_install_state_root
    acquire_lifecycle_lock
    install_from_config
    ;;
  start)
    snapshot_config
    resolve_config
    acquire_lifecycle_lock
    load_installed
    drift_check || die "configuration drift detected; run install on a clean target"
    ensure_postgres_ready
    platform_close_admission "start-window" "operatorctl start"
    airflow_lifecycle start
    airflow_wait_dag_identity >/dev/null
    airflow_set_dag_paused true \
      || die "the DAG could not be paused; admission stays closed"
    systemctl --user enable --now "${OPERATOR_UNIT_FILES[@]}" \
      || die "ingress services failed to start; admission stays closed"
    systemctl --user is-active --quiet "${OPERATOR_UNIT_FILES[@]}" \
      || die "ingress services are not active; admission stays closed"
    windows_action start "$STATE_ROOT/install-record.json" >/dev/null \
      || die "the Windows endpoint did not start; admission stays closed"
    verify_airflow_connection >/dev/null \
      || die "the Airflow connection identity did not pass; admission stays closed"
    OPERATOR_STATE="$STATE_ROOT" bash "$HERE/health_gate.sh" \
      "$STATE_ROOT/health-gate/start-$(date -u +%Y%m%dT%H%M%SZ)" \
      || die "the Linux health gate did not pass; admission stays closed"
    windows_action health "$STATE_ROOT/install-record.json" >/dev/null \
      || die "the Windows endpoint health did not pass; admission stays closed"
    if [ -n "$KEEP_ADMISSION_CLOSED" ]; then
      echo "start complete: services are healthy; DAG and admission stay closed"
    else
      airflow_set_dag_paused false \
        || die "the DAG could not be unpaused; admission stays closed"
      if ! platform_open_admission; then
        airflow_set_dag_paused true \
          || echo "operatorctl: admission open failed and the DAG could not be repaused" >&2
        die "admission could not be opened; inspect the DAG pause result"
      fi
      echo "start complete: admission open"
    fi
    ;;
  drain)
    snapshot_config
    resolve_config
    acquire_lifecycle_lock
    load_installed
    platform_close_admission "drain" "operatorctl drain"
    airflow_set_dag_paused true \
      || die "the DAG could not be paused; admission remains closed"
    windows_action drain "$STATE_ROOT/install-record.json" >/dev/null \
      || die "the Windows endpoint did not acknowledge drain; admission remains closed"
    projection="$(
      PLATFORM_OPERATOR_DSN="${PLATFORM_OPERATOR_DSN:-}" "$AIRFLOW_VENV/bin/python" \
        -m description_pipeline.orchestration.lifecycle_state report --require-no-unresolved
    )" || {
      echo "drain: unresolved or unknown work remains; admission stays closed" >&2
      exit 1
    }
    echo "$projection"
    case "$projection" in
      *'"settled": true'*) echo "drain: drained" ;;
      *) echo "drain: draining" ;;
    esac
    ;;
  stop)
    STOP_RC=0
    snapshot_config
    resolve_config
    acquire_lifecycle_lock
    load_installed
    projection="$(
      PLATFORM_OPERATOR_DSN="${PLATFORM_OPERATOR_DSN:-}" "$AIRFLOW_VENV/bin/python" \
        -m description_pipeline.orchestration.lifecycle_state report --require-settled \
          --require-admission-closed
    )" || die "stop refused: active, unresolved or unknown work remains, or admission is open"
    echo "$projection"
    windows_action stop "$STATE_ROOT/install-record.json" >/dev/null \
      || die "the Windows endpoint did not prove stop; Linux remains active"
    operator_units_disable || STOP_RC=1
    airflow_set_dag_paused true || STOP_RC=1
    if [ -n "$KEEP_DATABASE" ]; then
      airflow_lifecycle stop-executors || STOP_RC=1
      database_state="$(systemctl --user is-active "$DATABASE_UNIT_FILE" 2>/dev/null || true)"
      [ "$database_state" = "active" ] || {
        echo "operatorctl: PostgreSQL is not active for --keep-database: ${database_state:-unknown}" >&2
        STOP_RC=1
      }
    else
      airflow_lifecycle stop || STOP_RC=1
    fi
    require_units_stopped operator "${OPERATOR_UNIT_FILES[@]}" || STOP_RC=1
    require_units_stopped executor "${EXECUTOR_UNIT_FILES[@]}" || STOP_RC=1
    if [ -z "$KEEP_DATABASE" ]; then
      require_units_stopped database "$DATABASE_UNIT_FILE" || STOP_RC=1
    fi
    [ "$STOP_RC" -ne 0 ] || {
      if [ -n "$KEEP_DATABASE" ]; then
        echo "stop complete: writers stopped; PostgreSQL remains active"
      else
        echo "stop complete: all managed units stopped"
      fi
    }
    exit "$STOP_RC"
    ;;
  status)
    snapshot_config
    resolve_config
    load_installed
    STATUS_RC=0
    drift_check || STATUS_RC=1
    for unit in "${UNITS[@]}"; do
      state="$(systemctl --user is-active "$unit.service" 2>/dev/null || true)"
      printf '%s %s\n' "$unit" "${state:-unknown}"
      [ "$state" = "active" ] || STATUS_RC=1
    done
    windows_action status "$STATE_ROOT/install-record.json" || STATUS_RC=1
    exit "$STATUS_RC"
    ;;
  health)
    snapshot_config
    resolve_config
    load_installed
    drift_check || exit 1
    OPERATOR_STATE="$STATE_ROOT" bash "$HERE/health.sh"
    windows_action health "$STATE_ROOT/install-record.json" >/dev/null \
      || die "the Windows endpoint health did not pass"
    echo "health complete: Linux and Windows endpoints passed"
    ;;
  reconcile)
    [ -n "$RUN_ID" ] && [ -n "$STAGE" ] && [ -n "$EFFECT_KEY" ] \
      || die "reconcile requires --run-id, --stage and --effect-key"
    snapshot_config
    resolve_config
    acquire_lifecycle_lock
    load_installed
    (
      PLATFORM_ARTIFACT_ROOT="$("$CONTROL_PYTHON" -c \
        'import json,sys; print(json.load(open(sys.argv[1]))["storage"]["artifact_root"])' "$RESOLVED_CONFIG")"
      PIPELINE_WORK_ROOT="$("$CONTROL_PYTHON" -c \
        'import json,sys; print(json.load(open(sys.argv[1]))["storage"]["workspace_root"])' "$RESOLVED_CONFIG")"
      export PLATFORM_ARTIFACT_ROOT PIPELINE_WORK_ROOT
      "$AIRFLOW_VENV/bin/python" "$HERE/reconcile-effect.py" \
        --run-id "$RUN_ID" --stage "$STAGE" --effect-key "$EFFECT_KEY"
    ) || die "reconcile refused"
    ;;
  *) usage ;;
esac
