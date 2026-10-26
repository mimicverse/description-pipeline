#!/usr/bin/env bash
# The one supported lifecycle for the Linux operator deployment:
#   validate  static platform.json checks (schema/paths/targets/ports; no probes)
#   install   render the immutable config snapshot, then install the pinned runtime
#   start     enable/start PostgreSQL, Airflow, tunnel, portal and the HTTPS proxy
#             (start --keep-admission-closed: services up, the platform admission stays closed)
#   drain     close submissions and DAG admission, keep in-flight, return active/unresolved
#   stop      disable/stop them in reverse order (only when drained and settled);
#             --keep-database stops the supported writers while retaining PostgreSQL
#   status    systemctl --user is-active for every managed unit
#   health    deploy/operator/health.sh (static checks plus live service/HTTP checks)
#   reconcile confirm and register external outcomes; never repeats unknown side effects
#             (--install-id runs it from a staged candidate in a closed admission window)
#   stage     build the two-end candidate installation (isolated targets)
#   promote   switch both ends to the staged candidate (admission closed until verified);
#             the execution units stop mid-window while the shared database stays up for
#             the guarded migrations
#   rollback  restore both ends from the recorded previous installation
#
#   operatorctl.sh <verb> --config FILE
#   reconcile selects one exact effect through the production StageApplication registry,
#   observes its external target and appends evidence-backed reconciliation.
#
# Only description-* units are written; unrelated user units are untouched. Credentials and state
# live under the configured state root (outside Git).
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
AIRFLOW_HERE="$HERE/../airflow"
RELEASE_ROOT="$(cd "$HERE/../.." && pwd -P)"
die() { echo "operatorctl: $*" >&2; exit 1; }
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
[ "${#DATABASE_UNITS[@]}" -eq 1 ] || die "the managed systemd database role must name exactly one unit"
OPERATOR_UNIT_FILES=()
for unit in "${OPERATOR_UNITS[@]}"; do OPERATOR_UNIT_FILES+=("$unit.service"); done
operator_units_enable() {
  systemctl --user enable --now "${OPERATOR_UNIT_FILES[@]}"
}
operator_units_active() {
  systemctl --user is-active --quiet "${OPERATOR_UNIT_FILES[@]}"
}
operator_units_disable() {
  local index
  local -a reversed=()
  for ((index=${#OPERATOR_UNIT_FILES[@]}-1; index>=0; index--)); do
    reversed+=("${OPERATOR_UNIT_FILES[$index]}")
  done
  systemctl --user disable --now "${reversed[@]}"
}
GENERATION_ENV_REQUIRED=(
  AIRFLOW_DB_URL AIRFLOW_HOME AIRFLOW_RUNTIME_DB_URL AIRFLOW_VENV
  OPERATOR_STATE PIPELINE_STORE_ROOT PIPELINE_WHEEL PLATFORM_CONFIG_DIGEST
  PLATFORM_OPERATOR_DSN PLATFORM_SERVICE_DSN POSTGRES_PORT POSTGRES_ROOT
  SOLIDWORKS_HANDOFF_ROOT SOLIDWORKS_SSH_HOST
)
WINDOWS_LIFECYCLE_TIMEOUT=480

CONFIG_SNAPSHOT_DIR=""
CONFIG_FILE=""
RENDER_LOG=""
TOOLCHAIN_LOG=""
cleanup_operatorctl() {
  [ -z "$RENDER_LOG" ] || rm -f -- "$RENDER_LOG" 2>/dev/null || true
  [ -z "$TOOLCHAIN_LOG" ] || rm -f -- "$TOOLCHAIN_LOG" 2>/dev/null || true
  [ -z "$CONFIG_FILE" ] || rm -f -- "$CONFIG_FILE" 2>/dev/null || true
  [ -z "$CONFIG_SNAPSHOT_DIR" ] || rmdir -- "$CONFIG_SNAPSHOT_DIR" 2>/dev/null || true
}
trap cleanup_operatorctl EXIT

windows_transport() {
  local timeout="${1:-60}" metadata="${2:-}"
  local -a transport_args=(--host "$SOLIDWORKS_SSH_HOST" --timeout "$timeout")
  [ -z "$metadata" ] || transport_args+=(--metadata-file "$metadata")
  "$CONTROL_PYTHON" "$HERE/windows_transport.py" "${transport_args[@]}"
}
windows_lifecycle() {
  local output="$1" script_path="$2" binding_path="$3" verb="$4"
  local stage_dir="${5:-}" previous="${6:-}"
  "$CONTROL_PYTHON" - "$script_path" "$binding_path" "$verb" "$stage_dir" "$previous" <<'PY' \
    | windows_transport "$WINDOWS_LIFECYCLE_TIMEOUT" "$output.transport.json" \
      > "$output" 2> "$output.stderr"
import sys


def powershell_path(value: str) -> str:
    return value.replace("/", "\\")


def powershell_quote(value: str) -> str:
    return "'" + powershell_path(value).replace("'", "''") + "'"


script_path, binding_path, verb, stage_dir, previous = sys.argv[1:]
if verb not in {"health", "resume", "stage", "promote", "rollback", "status"}:
    raise SystemExit("operatorctl: invalid Windows lifecycle verb")
arguments = ["-Verb", verb, "-Binding", powershell_quote(binding_path)]
if stage_dir:
    arguments.extend(["-StageDir", powershell_quote(stage_dir)])
if previous:
    arguments.extend(["-Previous", powershell_quote(previous)])
print("& " + powershell_quote(script_path) + " " + " ".join(arguments))
PY
}

read_output_path() {
  # Bash 5.1 cannot invoke a function containing here-documents through $()
  # or an indirect "$@" call. Lifecycle functions must be called by name with
  # stdout redirected to a file; this helper only validates and assigns it.
  local source="$1" variable="$2"
  local -a lines=()
  mapfile -t lines < "$source"
  if [ "${#lines[@]}" -ne 1 ] || [ -z "${lines[0]}" ]; then
    return 2
  fi
  printf -v "$variable" '%s' "${lines[0]}"
}

usage() {
  cat >&2 <<'USAGE'
usage: operatorctl.sh <validate|install|start|drain|stop|status|health|reconcile|stage|promote|rollback> --config FILE
install inputs: --install-id ID --wheel FILE --endpoint-identity FILE [--release-file FILE]
start inputs: [--keep-admission-closed] (services start; the platform admission stays closed)
stop inputs: [--keep-database] (supported writers stop; the managed database stays active)
reconcile inputs: --run-id R --stage STAGE --effect-key KEY
  terminal effects return their recorded state without observation; unknown effects use
  the StageApplication registry. A stage with no remote effect is replayed by the engine
exit codes: 0 ok; 1 configuration/environment error; 2 usage
USAGE
  exit 2
}

ACTION="${1:-}"
shift || true
CONFIG_SOURCE_FILE=""
ENV_FILE=""
INSTALL_ID=""
WHEEL_FILE=""
ENDPOINT_IDENTITY=""
RELEASE_FILE=""
RUN_ID=""
STAGE=""
EFFECT_KEY=""
KEEP_ADMISSION_CLOSED=""
KEEP_DATABASE=""
MAINTENANCE_LOCK_FD=""
MAINTENANCE_LOCK_PATH=""
LOCKED_STATE_ROOT=""
while [ $# -gt 0 ]; do
  case "$1" in
    --config) CONFIG_SOURCE_FILE="${2:-}"; shift 2 ;;
    --install-id) INSTALL_ID="${2:-}"; shift 2 ;;
    --wheel) WHEEL_FILE="${2:-}"; shift 2 ;;
    --endpoint-identity) ENDPOINT_IDENTITY="${2:-}"; shift 2 ;;
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
  validate|install|start|drain|stop|status|health|reconcile|stage|promote|rollback) ;;
  *) usage ;;
esac
# Only start owns this switch; every other verb refuses it here, before any side effect runs.
if [ -n "$KEEP_ADMISSION_CLOSED" ] && [ "$ACTION" != "start" ]; then
  echo "operatorctl: --keep-admission-closed is only accepted by start" >&2
  exit 2
fi
if [ -n "$KEEP_DATABASE" ] && [ "$ACTION" != "stop" ]; then
  echo "operatorctl: --keep-database is only accepted by stop" >&2
  exit 2
fi
if [ -n "$INSTALL_ID" ]; then
  "$CONTROL_PYTHON" "$HERE/platform_config.py" validate-install-id \
    --install-id "$INSTALL_ID" >/dev/null \
    || die "install-id is malformed"
fi

snapshot_config() {
  local attempt
  [ -f "$CONFIG_SOURCE_FILE" ] || die "config file not found: $CONFIG_SOURCE_FILE"
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

assert_config_unchanged() {
  cmp -s -- "$CONFIG_SOURCE_FILE" "$CONFIG_FILE" \
    || die "the configuration changed while acquiring the maintenance lock"
}

install_from_config() {
  [ -f "$ENDPOINT_IDENTITY" ] || die "--endpoint-identity FILE is required for install"
  resolve_release_file
  # Preflight the bound identities and the installed asset before any step runs: release
  # artifact digests, the endpoint generation, their common release triple, and the wheel
  # that will actually be consumed.
  if [ -n "$WHEEL_FILE" ]; then
    "$CONTROL_PYTHON" "$HERE/install_record.py" check --release-file "$RELEASE_FILE" \
      --endpoint-identity "$ENDPOINT_IDENTITY" --tree "$HERE/../.." --wheel "$WHEEL_FILE" \
      >/dev/null || exit $?
  else
    "$CONTROL_PYTHON" "$HERE/install_record.py" check --release-file "$RELEASE_FILE" \
      --endpoint-identity "$ENDPOINT_IDENTITY" --tree "$HERE/../.." >/dev/null || exit $?
  fi
  if [ -z "$INSTALL_ID" ]; then
    INSTALL_ID="$("$CONTROL_PYTHON" -c 'import uuid; print(uuid.uuid4().hex)')"
  fi
  if [ -n "$WHEEL_FILE" ]; then
    SNAPSHOT_PATH="$("$CONTROL_PYTHON" "$HERE/platform_config.py" render --config "$CONFIG_FILE" \
      --install-id "$INSTALL_ID" --wheel "$WHEEL_FILE" | sed -n 's/^snapshot=//p')" || exit $?
  else
    SNAPSHOT_PATH="$("$CONTROL_PYTHON" "$HERE/platform_config.py" render --config "$CONFIG_FILE" \
      --install-id "$INSTALL_ID" --tree "$HERE/../.." | sed -n 's/^snapshot=//p')" || exit $?
  fi
  [ -f "$SNAPSHOT_PATH" ] || die "config snapshot was not written"
  if [ -n "$WHEEL_FILE" ]; then
    DERIVED_ENV="$("$CONTROL_PYTHON" "$HERE/platform_env.py" emit --snapshot "$SNAPSHOT_PATH" \
      --install-id "$INSTALL_ID" --tree "$HERE/../.." --release-file "$RELEASE_FILE" \
      --wheel "$WHEEL_FILE" | sed -n 's/^env=//p')" || exit $?
  else
    DERIVED_ENV="$("$CONTROL_PYTHON" "$HERE/platform_env.py" emit --snapshot "$SNAPSHOT_PATH" \
      --install-id "$INSTALL_ID" --tree "$HERE/../.." --release-file "$RELEASE_FILE" | sed -n 's/^env=//p')" \
      || exit $?
  fi
  [ -f "$DERIVED_ENV" ] || die "derived environment was not written"
  ENV_FILE="$DERIVED_ENV"
}

load_env_file() {
  local file="$1" rc=0 allexport=0
  [ -f "$file" ] && [ ! -L "$file" ] \
    || die "environment file is missing or a link: $file"
  case "$-" in
    *a*) allexport=1 ;;
  esac
  set -a
  # shellcheck disable=SC1090
  source "$file" || rc=$?
  [ "$allexport" = "1" ] || set +a
  return "$rc"
}

resolve_state_root() {
  STATE_ROOT="$("$CONTROL_PYTHON" "$HERE/platform_config.py" resolve --config "$CONFIG_FILE" | \
    "$CONTROL_PYTHON" -c 'import json,sys; print(json.load(sys.stdin)["storage"]["state_root"])')" || exit $?
  [ -f "$STATE_ROOT/resolved.env" ] || die "not installed: $STATE_ROOT/resolved.env is missing (run install first)"
  if [ -n "$LOCKED_STATE_ROOT" ] && [ "$STATE_ROOT" != "$LOCKED_STATE_ROOT" ]; then
    die "the configured state root changed while waiting for the maintenance lock"
  fi
}

load_active_env() {
  resolve_state_root
  load_env_file "$STATE_ROOT/resolved.env" || return $?
  # The operational entry verifies the complete install record, the snapshot digest and the
  # current-config binding before anything else; the check runs with the deployment interpreter
  # so it consumes the canonical schema implementation (single owner, no local mirror).
  ACTIVE_ID="$(AIRFLOW_HOME="$AIRFLOW_HOME" "$AIRFLOW_VENV/bin/python" "$HERE/install_record.py" \
    verify-active --config "$CONFIG_FILE" --state-root "$STATE_ROOT" | sed -n 's/^install_id=//p')" || exit $?
  [ -n "$ACTIVE_ID" ] || die "active install record did not declare a generation"
  ENV_FILE="$STATE_ROOT/derived/$ACTIVE_ID/operator.env"
  [ -f "$ENV_FILE" ] || die "derived environment missing for generation $ACTIVE_ID; run install"
  load_generation_env || return $?
}

# The maintenance window (stage/promote/rollback) verifies the RUNNING record and its immutable
# snapshot only: the candidate configuration is the subject of the switch, not drift of the
# running generation.  Current-config binding stays with verify-active on the steady-state verbs.
load_running_env() {
  resolve_state_root
  load_env_file "$STATE_ROOT/resolved.env" || return $?
  ACTIVE_ID="$(AIRFLOW_HOME="$AIRFLOW_HOME" "$AIRFLOW_VENV/bin/python" "$HERE/install_record.py" \
    verify-running --state-root "$STATE_ROOT" | sed -n 's/^install_id=//p')" || exit $?
  [ -n "$ACTIVE_ID" ] || die "active install record did not declare a generation"
  ENV_FILE="$STATE_ROOT/derived/$ACTIVE_ID/operator.env"
  [ -f "$ENV_FILE" ] || die "derived environment missing for generation $ACTIVE_ID; run install"
}

# A lifecycle transition must execute the environment of the generation named by its verified
# record, not values inherited from the caller's previous generation. The derived file is the
# generation-scoped authority and every key it defines replaces the ambient shell value.
load_generation_env() {
  [ -n "$ENV_FILE" ] || die "generation environment path is not selected"
  unset "${GENERATION_ENV_REQUIRED[@]}"
  load_env_file "$ENV_FILE" || return $?
  for key in "${GENERATION_ENV_REQUIRED[@]}"; do
    [ -n "${!key:-}" ] \
      || die "generation environment $ENV_FILE does not define $key"
  done
}

acquire_maintenance_lock() {
  local state_root="$1" lock_dir lock_path inherited_owner inherited_fd inherited_path
  if [ -n "$MAINTENANCE_LOCK_PATH" ]; then
    [ -n "$LOCKED_STATE_ROOT" ] && [ "$state_root" = "$LOCKED_STATE_ROOT" ] \
      || die "the requested state root differs from the held maintenance lock"
    [ "$MAINTENANCE_LOCK_PATH" = "$LOCKED_STATE_ROOT/locks/maintenance.lock" ] \
      || die "the held maintenance lock path differs from its state root"
    return 0
  fi
  [ -d "$state_root" ] && [ ! -L "$state_root" ] \
    || die "the state root for the maintenance lock is missing or a link: $state_root"
  lock_dir="$state_root/locks"
  mkdir -p "$lock_dir"
  [ -d "$lock_dir" ] && [ ! -L "$lock_dir" ] \
    || die "the maintenance lock directory is not a real directory: $lock_dir"
  chmod 700 "$lock_dir"
  lock_path="$lock_dir/maintenance.lock"
  [ ! -L "$lock_path" ] || die "the maintenance lock is a link: $lock_path"
  command -v flock >/dev/null 2>&1 || die "flock is required for the maintenance lifecycle"
  inherited_owner="${OPERATORCTL_MAINTENANCE_LOCK_OWNER:-}"
  if [ -n "$inherited_owner" ]; then
    inherited_fd="${OPERATORCTL_MAINTENANCE_LOCK_FD:-}"
    inherited_path="${OPERATORCTL_MAINTENANCE_LOCK_PATH:-}"
    [[ "$inherited_fd" =~ ^[0-9]+$ ]] \
      || die "an inherited maintenance lock must carry the caller's file descriptor"
    [ "$inherited_path" = "$lock_path" ] \
      || die "the inherited maintenance lock path does not match the state-root lock"
    [ -f "$lock_path" ] || die "the inherited maintenance lock is missing: $lock_path"
    "$CONTROL_PYTHON" - "$inherited_fd" "$lock_path" "$inherited_owner" <<'PY' \
      || die "the inherited maintenance lock descriptor is not the held state-root lock"
import json
import os
import re
import sys
from pathlib import Path

descriptor = int(sys.argv[1])
lock_path = Path(sys.argv[2])
expected_owner = sys.argv[3]
try:
    document = json.loads(lock_path.read_text(encoding="utf-8"))
except Exception as error:
    raise SystemExit(f"cannot read the maintenance lock owner: {error}")
if (
    not isinstance(document, dict)
    or set(document)
    != {"schema_version", "owner_id", "acquired_at", "pid"}
    or document.get("schema_version")
    != "solidworks-to-urdf.maintenance-lock/v1"
    or document.get("owner_id") != expected_owner
):
    raise SystemExit("the maintenance lock owner is malformed")
inherited_metadata = os.fstat(descriptor)
path_metadata = os.stat(lock_path, follow_symlinks=False)
if (
    not os.path.isfile(f"/proc/self/fd/{descriptor}")
    or (inherited_metadata.st_dev, inherited_metadata.st_ino)
    != (path_metadata.st_dev, path_metadata.st_ino)
):
    raise SystemExit("the inherited descriptor names another file")
fdinfo = Path(f"/proc/self/fdinfo/{descriptor}").read_text(encoding="utf-8")
if not re.search(
    r"^lock:\s+\d+:\s+FLOCK\s+ADVISORY\s+WRITE\b",
    fdinfo,
    flags=re.MULTILINE,
):
    raise SystemExit("the inherited descriptor does not itself hold the write lock")
PY
    MAINTENANCE_LOCK_FD="$inherited_fd"
    MAINTENANCE_LOCK_PATH="$lock_path"
    echo "maintenance_lock=inherited:$lock_path"
    return 0
  fi
  exec {MAINTENANCE_LOCK_FD}>>"$lock_path"
  flock -n "$MAINTENANCE_LOCK_FD" \
    || die "another supported lifecycle operation owns the maintenance lock: $lock_path"
  chmod 600 "$lock_path"
  MAINTENANCE_LOCK_PATH="$lock_path"
  echo "maintenance_lock=$lock_path"
}

# One env contract: platform_env and render_operator write every derived value through the
# canonical encoding (bare when shell-safe, single-quoted otherwise - the libpq DSNs' "&"
# above all), so any consumer can simply `source` the file.  This helper reads one generation
# value back by sourcing it in a subshell and never parses quoting itself - and it clears the
# key first, so a candidate file that does not define it (missing line, a defeated assignment
# such as an unquoted "&", a syntax error, an empty file) can never fall back to a value the
# route shell inherited from another generation.  A missing or empty value is an explicit,
# human-readable refusal at the read, before any DDL or recovery step.
generation_env_value() {
  local file="$1" key="$2" value
  value="$(
    set -a
    unset "$key"
    source "$file" >/dev/null 2>&1 || exit 3
    printf '%s' "${!key-}"
  )" || die "the derived environment $file could not be read for $key"
  [ -n "$value" ] || die "the derived environment $file does not define $key"
  printf '%s' "$value"
}

# The release manifest is a member of the controller release tree, never an external override.
# Canonicalize it before any stage/promote/rollback side effect and refuse a cross-tree path.
resolve_release_file() {
  local candidate="${RELEASE_FILE:-$RELEASE_ROOT/release.json}" resolved output
  output="$(mktemp)"
  if ! "$CONTROL_PYTHON" - "$RELEASE_ROOT" "$candidate" > "$output" <<'PY'
import os
import sys

root = os.path.realpath(sys.argv[1])
path = os.path.realpath(sys.argv[2])
expected = os.path.join(root, "release.json")
if path != expected:
    raise SystemExit(f"release manifest must be the controller release tree manifest: {expected}")
if not os.path.isfile(path):
    raise SystemExit(f"release manifest not found: {path}")
print(path)
PY
  then
    rm -f "$output"
    die "the release manifest is outside the controller release tree"
  fi
  read_output_path "$output" resolved || {
    rm -f "$output"
    die "the release manifest did not resolve to one path"
  }
  rm -f "$output"
  RELEASE_FILE="$resolved"
}

# The declared-value reader: one generation environment decoded as DATA through the renderer's
# canonical decoder - never sourced, so no shell state, ambient or inherited, can leak into
# the value.  The bootstrap window reads the RUNNING side with this reader: a pre-contract
# generation holds its values in that writer's own format (bare single words), and the running
# deployment's declared authority must be read, not rewritten or inherited.  A value that is
# missing, empty or not exactly one shell word refuses; nothing is ever defaulted.  A key
# declared twice with conflicting values is ambiguous across readers and refuses; identical
# duplicates decode to the same bytes the canonical readers see and are accepted.
generation_env_declared_value() {
  local file="$1" key="$2" value
  value="$("$CONTROL_PYTHON" -c '
import importlib.util
import sys
from pathlib import Path

spec = importlib.util.spec_from_file_location("operator_env_decoder", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
seen = False
value = ""
for number, line in enumerate(Path(sys.argv[2]).read_text(encoding="utf-8").splitlines(), 1):
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        continue
    key, separator, raw = stripped.partition("=")
    if not separator or key.strip() != sys.argv[3]:
        continue
    seen = True
    try:
        declared = module.decode_env_value(raw.strip())
    except ValueError as error:
        raise SystemExit(f"{sys.argv[2]}:{number}: {error}")
    if value != "" and declared != value:
        raise SystemExit(f"{sys.argv[2]}:{number}: conflicting duplicate declaration for {sys.argv[3]}")
    value = declared
if not seen or value == "":
    raise SystemExit(3)
sys.stdout.write(value)
' "$HERE/render_operator.py" "$file" "$key")" || {
    code=$?
    if [ "$code" = 3 ]; then
      die "the generation environment $file does not declare $key"
    fi
    die "the generation environment $file has an unreadable $key declaration"
  }
  printf '%s' "$value"
}

# The ONE managed proxy binary has two duties, split by side effect.
#   ensure_proxy_binary  PREPARE: decides the actual result (an executable configured/system
#     binary, else the toolchain nginx ``install_proxy.sh`` lands and prints) and may install
#     or download.  It runs only inside the controlled change entries that re-render and
#     re-bind a generation: install, promote and rollback.
#   check_proxy_binary   VALIDATE, read-only: the bound value from resolved.env must exist, be
#     executable and be the binary the installed proxy unit executes.  A read verb reports that
#     as drift and never repairs it, so a missing binary cannot be masked by an implicit
#     install.
ensure_proxy_binary() {
  NGINX_BIN="${NGINX_BIN:-}" OPERATOR_STATE="${OPERATOR_STATE:-}" \
    bash "$HERE/scripts/ensure_proxy_binary.sh"
}

check_proxy_binary() {
  NGINX_BIN="${NGINX_BIN:-}" bash "$HERE/scripts/check_proxy_binary.sh" "$@"
}

# The gates read the platform authority from the managed database, so the start window must
# begin by bringing that database up and confirming its socket.  The cluster identity never
# runs here; this is the same managed unit the installation created.
ensure_postgres_ready() {
  local database_unit="${DATABASE_UNITS[0]}"
  PG_BIN="$POSTGRES_ROOT/root/usr/lib/postgresql/${POSTGRES_MAJOR:-14}/bin"
  export LD_LIBRARY_PATH="$POSTGRES_ROOT/root/usr/lib/x86_64-linux-gnu:${LD_LIBRARY_PATH:-}"
  systemctl --user daemon-reload
  systemctl --user start "$database_unit.service" \
    || die "$database_unit.service failed to start"
  local ready=0
  for _ in $(seq 1 20); do
    if "$PG_BIN/pg_isready" -q -h "$POSTGRES_ROOT/socket" -p "$POSTGRES_PORT"; then ready=1; break; fi
    sleep 0.5
  done
  [ "$ready" = "1" ] || die "PostgreSQL did not become ready on $POSTGRES_ROOT/socket"
}

airflow_render() {
  # The runtime units always load the current controlled derived environment: the binding is
  # this call's ENV_FILE, never an ambient OPERATOR_DERIVED_ENV that could point elsewhere.
  [ -n "$ENV_FILE" ] || die "ENV_FILE is not set: the runtime units must load the controlled derived environment"
  OPERATOR_DERIVED_ENV="$ENV_FILE" \
  AIRFLOW_VENV="$AIRFLOW_VENV" AIRFLOW_HOME="$AIRFLOW_HOME" \
  POSTGRES_ROOT="${POSTGRES_ROOT:-}" POSTGRES_MAJOR="${POSTGRES_MAJOR:-14}" \
  SOLIDWORKS_SSH_HOST="${SOLIDWORKS_SSH_HOST:-}" \
  AIRFLOW_API_PORT="$AIRFLOW_API_PORT" POSTGRES_PORT="$POSTGRES_PORT" \
  TUNNEL_LOOPBACK_PORT="$TUNNEL_LOOPBACK_PORT" SOLIDWORKS_ENDPOINT_PORT="$SOLIDWORKS_ENDPOINT_PORT" \
  FEISHU_ENV_FILE="${FEISHU_ENV_FILE:-$AIRFLOW_HOME/feishu.env}" \
    bash "$AIRFLOW_HERE/services.sh" render
}

airflow_units_match_generation() {
  local unit unit_file
  for unit in "${AIRFLOW_UNITS[@]}"; do
    unit_file="$TARGET/$unit.service"
    [ -f "$unit_file" ] && [ ! -L "$unit_file" ] \
      || die "the restored unit is missing or a link: $unit_file"
    grep -Fq "ExecStart=$AIRFLOW_VENV/" "$unit_file" \
      || die "the restored unit does not execute the active generation runtime: $unit_file"
    grep -Fqx "EnvironmentFile=$ENV_FILE" "$unit_file" \
      || die "the restored unit does not load the active generation environment: $unit_file"
    printf '%s sha256=%s ExecStart=%s\n' "$unit" \
      "$(sha256sum "$unit_file" | awk '{print $1}')" \
      "$(sed -n 's/^ExecStart=//p' "$unit_file" | head -1)"
  done
}

airflow_require_no_readers() {
  local unit state
  for unit in "${EXECUTOR_UNITS[@]}"; do
    state="$(systemctl --user is-active "$unit" 2>/dev/null || true)"
    case "$state" in
      inactive|failed) ;;
      *) die "Airflow DAG reader $unit is ${state:-unknown}; refusing a non-barriered generation switch" ;;
    esac
  done
}

airflow_record_dag() {
  "$CONTROL_PYTHON" -c '
import json
import re
import sys
from pathlib import Path

record = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
dag = record.get("dag")
if not isinstance(dag, dict):
    raise SystemExit("install record has no DAG binding")
path = Path(str(dag.get("path") or ""))
if not path.is_absolute():
    raise SystemExit("install record DAG path is not absolute")
print(path)
' "$1"
}

airflow_here_for_record() {
  local dag_path
  dag_path="$(airflow_record_dag "$1")" || die "the active generation has no usable DAG path"
  dirname "$(dirname "$dag_path")"
}

airflow_render_generation_config() {
  local record_path="$1" venv="$2" env_file="$3"
  local dag_path airflow_here renderer runtime_dsn api_port api_port_output
  dag_path="$(airflow_record_dag "$record_path")" \
    || die "the active generation has no usable DAG path"
  airflow_here="$(dirname "$(dirname "$dag_path")")"
  [ -f "$airflow_here/render_config.py" ] && [ ! -L "$airflow_here/render_config.py" ] \
    || die "the active generation has no config renderer: $airflow_here/render_config.py"
  [ -f "$airflow_here/airflow.cfg.template" ] && [ ! -L "$airflow_here/airflow.cfg.template" ] \
    || die "the active generation has no Airflow config template"
  renderer="$airflow_here/../operator/render_operator.py"
  [ -f "$renderer" ] && [ ! -L "$renderer" ] \
    || die "the target generation has no operator env renderer: $renderer"
  runtime_dsn="$(generation_env_value "$env_file" AIRFLOW_RUNTIME_DB_URL)" || return 1
  # Resolve the port through the target controller's own env parser and default, so the
  # generated execution URL cannot fall back to the resident generation's ambient value.
  api_port_output="$(mktemp)"
  if ! "$CONTROL_PYTHON" - "$renderer" "$env_file" > "$api_port_output" <<'PY'
import importlib.util
import sys
from pathlib import Path

spec = importlib.util.spec_from_file_location("generation_render_operator", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
values = module.load_env(Path(sys.argv[2]))
print(module.integer(values, "AIRFLOW_API_PORT", module.PORT_DEFAULTS["AIRFLOW_API_PORT"]))
PY
  then
    rm -f "$api_port_output"
    return 1
  fi
  read_output_path "$api_port_output" api_port || {
    rm -f "$api_port_output"
    return 1
  }
  rm -f "$api_port_output"
  [ -n "$api_port" ] || die "the target generation has no Airflow API port"
  AIRFLOW_DB_URL="$runtime_dsn" AIRFLOW_API_PORT="$api_port" \
    "$venv/bin/python" "$airflow_here/render_config.py" \
      --home "$AIRFLOW_HOME" --venv "$venv" \
      --template "$airflow_here/airflow.cfg.template" \
      --dags-folder "$(dirname "$dag_path")" \
    || return 1
  AIRFLOW_HOME="$AIRFLOW_HOME" "$venv/bin/python" "$HERE/runtime_dag.py" \
    --state-root "$STATE_ROOT" --airflow-home "$AIRFLOW_HOME" --files-only >/dev/null \
    || return 1
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
  printf '%s\n' "${result:-Airflow did not serialize the active DAG}" >&2
  return 1
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

airflow_wait_dag_state() {
  local desired="$1" verb attempt status
  case "$desired" in
    true) verb=pause ;;
    false) verb=unpause ;;
    *) die "DAG state must be true or false, got: $desired" ;;
  esac
  # Airflow can report success before the DAG is registered. Confirm the persisted
  # state before changing ingress or declaring a rollback complete.
  for ((attempt = 1; attempt <= 30; attempt++)); do
    if AIRFLOW_HOME="$AIRFLOW_HOME" "$AIRFLOW_VENV/bin/airflow" dags "$verb" solidworks_to_urdf >/dev/null; then
      :
    else
      status=$?
      return "$status"
    fi
    if AIRFLOW_HOME="$AIRFLOW_HOME" "$AIRFLOW_VENV/bin/python" - "$desired" <<'PY'
# airflow-dag-state-check
import sys
from airflow.models.dag import DagModel
from airflow.utils.session import create_session

expected = sys.argv[1] == "true"
with create_session() as session:
    dag = session.get(DagModel, "solidworks_to_urdf")
    sys.exit(0 if dag is not None and dag.is_paused is expected else 1)
PY
    then
      return 0
    fi
    sleep 2
  done
  return 1
}

airflow_unpause_dag() {
  local status
  # Unpause alone silently succeeds before the DAG is registered. Confirm its
  # persisted state before exposing ingress, including on a fresh installation.
  if airflow_wait_dag_state false; then
    return 0
  else
    status=$?
  fi
  [ "$status" = "1" ] && die "DAG admission did not open; ingress was not started"
  return "$status"
}

airflow_pause_dag() {
  local status
  if airflow_wait_dag_state true; then
    return 0
  else
    status=$?
  fi
  [ "$status" = "1" ] && die "DAG scheduling did not pause; the rollback admission remains closed"
  return "$status"
}

airflow_capture_dag_state() {
  local out="$1"
  AIRFLOW_HOME="$AIRFLOW_HOME" "$AIRFLOW_VENV/bin/python" - "$out" <<'PY'
# airflow-dag-capture
import json
import sys
from pathlib import Path
from airflow.models.dag import DagModel
from airflow.utils.session import create_session

with create_session() as session:
    dag = session.get(DagModel, "solidworks_to_urdf")
    if dag is None:
        raise SystemExit("the maintenance DAG is not registered")
    state = {"dag_id": "solidworks_to_urdf", "paused": dag.is_paused}
Path(sys.argv[1]).write_text(json.dumps(state, sort_keys=True) + "\n", encoding="utf-8")
print(f"dag_paused_before={str(dag.is_paused).lower()}")
PY
}

airflow_restore_dag_state() {
  local state_file="$1" paused output
  output="$(mktemp)"
  if ! "$CONTROL_PYTHON" - "$state_file" > "$output" <<'PY'
import json
import sys

document = json.loads(open(sys.argv[1], encoding="utf-8").read())
if document.get("dag_id") != "solidworks_to_urdf" or type(document.get("paused")) is not bool:
    raise SystemExit("the captured DAG state is invalid")
print("true" if document["paused"] else "false")
PY
  then
    rm -f "$output"
    die "the captured DAG state is unreadable: $state_file"
  fi
  read_output_path "$output" paused || {
    rm -f "$output"
    die "the captured DAG state is not one boolean"
  }
  rm -f "$output"
  if [ "$paused" = "true" ]; then
    airflow_pause_dag
    echo "dag_paused_restored=true"
    return
  fi
  if airflow_wait_dag_state false; then
    echo "dag_paused_restored=false"
    return
  fi
  AIRFLOW_HOME="$AIRFLOW_HOME" "$AIRFLOW_VENV/bin/airflow" dags pause solidworks_to_urdf \
    >/dev/null 2>&1 || true
  die "the pre-maintenance DAG state could not be restored; the DAG was left paused and the admission remains closed"
}

airflow_runtime_matches_generation() {
  local candidate_venv="$1" attempt
  local unit pid state restored_maps candidate_maps cmdline ready
  for ((attempt = 1; attempt <= 60; attempt++)); do
    ready=1
    for unit in "${AIRFLOW_UNITS[@]}"; do
      state="$(systemctl --user is-active "$unit.service" 2>/dev/null || true)"
      pid="$(systemctl --user show -p MainPID --value "$unit.service" 2>/dev/null || true)"
      restored_maps=0
      candidate_maps=0
      cmdline=""
      if [ "$state" = "active" ] && [ -n "$pid" ] && [ "$pid" != "0" ] \
          && [ -r "/proc/$pid/maps" ] && [ -r "/proc/$pid/cmdline" ]; then
        restored_maps="$(grep -Fc "$AIRFLOW_VENV" "/proc/$pid/maps" 2>/dev/null || true)"
        candidate_maps="$(grep -Fc "$candidate_venv" "/proc/$pid/maps" 2>/dev/null || true)"
        cmdline="$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null || true)"
      fi
      printf '%s state=%s mainpid=%s restored_maps=%s candidate_maps=%s cmdline=%s\n' \
        "$unit.service" "$state" "$pid" "$restored_maps" "$candidate_maps" "$cmdline"
      if [ "$state" != "active" ] || [ -z "$pid" ] || [ "$pid" = "0" ] \
          || [ "$restored_maps" -lt 1 ] || [ "$candidate_maps" != "0" ]; then
        ready=0
      fi
    done
    [ "$ready" = "1" ] && return 0
    sleep 1
  done
  return 1
}

# The platform admission is the authority for new work; the DAG unpause above and the pause in
# drain are scheduling measures only.  Both transitions go through the single controlled store
# entry (transaction-serialized with new admissions); the gates never write it directly.
platform_close_admission() {
  local reason="${1:-drain}" actor="${2:-operatorctl drain}"
  [ -n "${PLATFORM_SERVICE_DSN:-}" ] || die "PLATFORM_SERVICE_DSN is missing; run install to derive it"
  PLATFORM_CLOSE_REASON="$reason" PLATFORM_CLOSE_ACTOR="$actor" "$AIRFLOW_VENV/bin/python" - <<'PY' \
    || die "could not close the platform admission"
# platform-close-admission
import os

import psycopg2

from description_pipeline.platform.facts import AdmissionControlStore


def connect():
    return psycopg2.connect(os.environ["PLATFORM_SERVICE_DSN"])


view = AdmissionControlStore(connect).close_admission(
    reason=os.environ["PLATFORM_CLOSE_REASON"], actor=os.environ["PLATFORM_CLOSE_ACTOR"]
)
print(f"admission={view.state} revision={view.revision}")
PY
}

platform_open_admission() {
  [ -n "${PLATFORM_SERVICE_DSN:-}" ] || die "PLATFORM_SERVICE_DSN is missing; run install to derive it"
  "$AIRFLOW_VENV/bin/python" - <<'PY' || die "could not open the platform admission"
# platform-open-admission
import os

import psycopg2

from description_pipeline.platform.facts import AdmissionControlStore


def connect():
    return psycopg2.connect(os.environ["PLATFORM_SERVICE_DSN"])


view = AdmissionControlStore(connect).open_admission(reason="start", actor="operatorctl start")
print(f"admission={view.state} revision={view.revision}")
PY
}

# Health is a read-only relay of the endpoint that is running now.  Its binding therefore comes
# from the identity that owns that endpoint: the candidate release manifest during promotion, or
# the active install record after rollback has restored the previous resident.  It must never be
# rendered from the candidate release while checking the previous generation: that would compare
# the resident's archive with the candidate archive and turn a successful restore into a failure.
instance_lifecycle_health() {
  local generation="$1" identity_file="$2" label="${3:-windows-health}"
  local evidence_dir="${4:-}" dir
  dir="${evidence_dir:-$OPERATOR_STATE/promotions/$generation}"
  local binding="$dir/$label-binding.json" output="$dir/$label.out"
  mkdir -p "$dir"
  local evidence_path
  for evidence_path in "$output" "$output.ssh-rc" "$output.stderr" "$output.transport.json"; do
    if ! : > "$evidence_path"; then
      echo "instance_lifecycle: could not initialise health evidence in $dir" >&2
      return 2
    fi
  done
  "$CONTROL_PYTHON" - "$CONFIG_FILE" "$identity_file" "$binding" "$generation" \
    "$HERE/../windows/instance-lifecycle.ps1" <<'PY' || return 1
import json
import sys
from pathlib import Path

config_path = Path(sys.argv[1])
identity_path = Path(sys.argv[2])
out = Path(sys.argv[3])
generation = sys.argv[4]
windows = json.loads(config_path.read_text(encoding="utf-8"))["windows"]
instance = windows["instance"]
identity = json.loads(identity_path.read_text(encoding="utf-8"))

if identity.get("schema_version") == "solidworks-to-urdf.install-record/v1":
    release = identity.get("release")
    rows = release.get("artifacts") if isinstance(release, dict) else None
    if not isinstance(rows, list):
        raise SystemExit("operatorctl: the active install record carries no release artifacts")
    artifacts = {}
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"name", "sha256"}:
            raise SystemExit("operatorctl: the active install record has a malformed release artifact")
        artifacts[row["name"]] = row["sha256"]
elif identity.get("schema_version") == "solidworks-to-urdf.release/v1":
    release = identity
    artifacts = release.get("artifacts")
    if not isinstance(artifacts, dict):
        raise SystemExit("operatorctl: the release manifest carries no artifacts")
else:
    raise SystemExit(f"operatorctl: unsupported health identity: {identity_path}")

native = sorted(name for name in artifacts if name.endswith(".zip") and "-windows-" in name)
if len(native) != 1:
    raise SystemExit(
        f"operatorctl: the health identity must name exactly one native Windows archive, got {native}"
    )
remote = instance["state_root"].rstrip("/") + "/lifecycle/" + generation
out.write_text(
    json.dumps(
        {
            "task_name": instance["task_name"],
            "state_root": instance["state_root"],
            "package_root": instance["package_root"],
            "output_root": instance["output_root"],
            "port": windows["endpoint_port"],
            "generation": generation,
            "dir": remote,
            "archive": native[0],
            "artifact_sha256": artifacts[native[0]],
            "version": release["version"],
            "source_commit": release["source_commit"],
            "source_sha256": release["source_sha256"],
        },
        indent=2,
    )
    + "\n",
    encoding="utf-8",
)
PY
  local remote archive_name expected_hash remote_archive_state
  local archive_probe="$dir/$label-archive-state.ps1"
  remote="$("$CONTROL_PYTHON" -c 'import json,sys; print(json.load(open(sys.argv[1]))["dir"])' "$binding")"
  archive_name="$("$CONTROL_PYTHON" -c 'import json,sys; print(json.load(open(sys.argv[1]))["archive"])' "$binding")"
  expected_hash="$("$CONTROL_PYTHON" -c 'import json,sys; print(json.load(open(sys.argv[1]))["artifact_sha256"])' "$binding")"
  "$CONTROL_PYTHON" - "$remote/$archive_name" > "$archive_probe" <<'PY' || return 2
import sys

path = sys.argv[1].replace("/", "\\").replace("'", "''")
print(
    "if (Test-Path -LiteralPath '" + path + "') {"
    "$item=Get-Item -LiteralPath '" + path + "';"
    "Write-Output ('{0}|{1}' -f $item.Length,"
    "(Get-FileHash -LiteralPath '" + path + "' -Algorithm SHA256).Hash.ToLower())"
    "} else { Write-Output 'absent' }"
)
PY
  remote_archive_state="$(
    windows_transport 60 "$output.transport.json" < "$archive_probe" \
      2> "$output.stderr" | tr -d '\r' | tail -1
  )" || { echo "instance_lifecycle: could not inspect the health archive on $SOLIDWORKS_SSH_HOST" >&2; return 2; }
  case "$remote_archive_state" in
    *"|$expected_hash") ;;
    absent)
      echo "instance_lifecycle: the health identity's staged archive is missing on $SOLIDWORKS_SSH_HOST" >&2
      return 1
      ;;
    *)
      echo "instance_lifecycle: the staged archive does not match the health identity" >&2
      return 1
      ;;
  esac
  scp -q -o BatchMode=yes "$binding" "$SOLIDWORKS_SSH_HOST:$remote/" \
    || { echo "instance_lifecycle: could not transfer the health binding to $SOLIDWORKS_SSH_HOST" >&2; return 2; }
  local remote_rc=0
  windows_lifecycle \
    "$output" "$remote/instance-lifecycle.ps1" "$remote/$label-binding.json" health \
    || remote_rc=$?
  if ! printf '%s\n' "$remote_rc" > "$output.ssh-rc"; then
    echo "instance_lifecycle: could not persist the health SSH exit status in $dir" >&2
    return 2
  fi
  local parse_rc=0
  "$CONTROL_PYTHON" - "$output" "$remote_rc" <<'PY' || parse_rc=$?
import json
import sys

remote_rc = int(sys.argv[2])
try:
    document = json.loads(open(sys.argv[1], encoding="utf-8").read().strip().splitlines()[-1])
except (OSError, ValueError, IndexError):
    raise SystemExit(3)
if not isinstance(document, dict) or document.get("verb") != "health":
    raise SystemExit(3)
if remote_rc == 0 and document.get("passed") is True:
    raise SystemExit(0)
code = document.get("code")
if remote_rc in (0, 1) and document.get("passed") is False and isinstance(code, str) and code.strip():
    raise SystemExit(1)
raise SystemExit(3)
PY
  if [ "$parse_rc" = "1" ]; then
    echo "instance_lifecycle: the health result is a definite refusal; see $output" >&2
    return 1
  fi
  if [ "$parse_rc" != "0" ]; then
    echo "instance_lifecycle: the health outcome is unreadable; see $output" >&2
    return 2
  fi
  echo "$output"
}

instance_lifecycle_resume() {
  local generation="$1" identity_file="$2"
  local stamp dir binding output remote remote_script remote_binding
  stamp="$(date -u +%Y%m%dT%H%M%S%NZ)-$$-$RANDOM"
  dir="$OPERATOR_STATE/health-gate/windows-resume-$generation-$stamp"
  binding="$dir/windows-resume-binding.json"
  output="$dir/windows-resume.out"
  mkdir -p "$dir"
  "$CONTROL_PYTHON" - "$CONFIG_FILE" "$identity_file" "$binding" "$generation" \
    "$HERE/../windows/instance-lifecycle.ps1" <<'PY' || return 1
import hashlib
import json
import re
import sys
from pathlib import Path

config_path, identity_path, out_path = map(Path, sys.argv[1:4])
generation, script_path = sys.argv[4], Path(sys.argv[5])
config = json.loads(config_path.read_text(encoding="utf-8"))
identity = json.loads(identity_path.read_text(encoding="utf-8"))
windows = config["windows"]
instance = windows["instance"]
end = identity.get("ends", {}).get("windows")
if not isinstance(end, dict) or not isinstance(end.get("release"), dict):
    raise SystemExit("operatorctl: the active install record carries no Windows identity")
release = end["release"]
if release.get("role") != "native":
    raise SystemExit("operatorctl: the active Windows identity is not the native role")
if not all(isinstance(release.get(key), str) and release[key] for key in ("version", "source_commit", "source_sha256")):
    raise SystemExit("operatorctl: the active Windows release identity is incomplete")
if not isinstance(end.get("install_id"), str) or not re.fullmatch(r"win-[0-9a-f]{32}", end["install_id"]):
    raise SystemExit("operatorctl: the active Windows install identity is malformed")
if not isinstance(end.get("config_digest"), str) or not re.fullmatch(r"[0-9a-f]{64}", end["config_digest"]):
    raise SystemExit("operatorctl: the active Windows config digest is malformed")
triple = "\n".join(
    str(release.get(key) or "") for key in ("version", "source_commit", "source_sha256", "role")
)
document = {
    "schema_version": "solidworks-to-urdf.windows-recovery/v2",
    "ssh_host": windows["ssh_host"],
    "task_name": instance["task_name"],
    "state_root": instance["state_root"],
    "package_root": instance["package_root"],
    "output_root": instance["output_root"],
    "port": windows["endpoint_port"],
    "generation": str(generation),
    "version": release["version"],
    "source_commit": release["source_commit"],
    "source_sha256": release["source_sha256"],
    "install_id": end["install_id"],
    "config_digest": end["config_digest"],
    "release_identity_sha256": hashlib.sha256(triple.encode("utf-8")).hexdigest(),
    "script_sha256": hashlib.sha256(script_path.read_bytes()).hexdigest(),
}
out_path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
  remote="$("$CONTROL_PYTHON" -c 'import json,sys; print(json.load(open(sys.argv[1]))["state_root"].rstrip("/") + "/instance-promotions/windows-resume-" + sys.argv[2])' \
    "$binding" "$generation-$stamp")"
  remote_script="$remote/instance-lifecycle.ps1"
  remote_binding="$remote/windows-resume-binding.json"
  "$CONTROL_PYTHON" - "$remote" <<'PY' | windows_transport 60 \
    || { echo "instance_lifecycle_resume: could not create $remote" >&2; return 1; }
import sys

path = sys.argv[1].replace("/", "\\").replace("'", "''")
print(
    "$ErrorActionPreference='Stop';"
    "New-Item -ItemType Directory -Path '" + path + "' -ErrorAction Stop | Out-Null"
)
PY
  scp -q -p -o BatchMode=yes -o StrictHostKeyChecking=yes \
    "$HERE/../windows/instance-lifecycle.ps1" "$binding" \
    "$SOLIDWORKS_SSH_HOST:\"$remote/\"" \
    || { echo "instance_lifecycle_resume: could not transfer the resume binding" >&2; return 1; }
  local remote_rc=0
  : > "$output"
  windows_lifecycle "$output" "$remote_script" "$remote_binding" resume || remote_rc=$?
  local parse_rc=0
  "$CONTROL_PYTHON" - "$output" "$remote_rc" <<'PY' || parse_rc=$?
import json
import sys

try:
    document = json.loads(open(sys.argv[1], encoding="utf-8").read().strip().splitlines()[-1])
except (OSError, ValueError, IndexError):
    raise SystemExit(3)
remote_rc = int(sys.argv[2])
if remote_rc == 0 and document.get("passed") is True and document.get("verb") == "resume":
    raise SystemExit(0)
code = document.get("code")
if remote_rc == 1 and document.get("passed") is False and isinstance(code, str) and code:
    print(code, file=sys.stderr)
    raise SystemExit(1)
raise SystemExit(3)
PY
  if [ "$parse_rc" != "0" ]; then
    echo "instance_lifecycle_resume: the Windows resume outcome is not proven; see $output" >&2
    return 2
  fi
  echo "$output"
}

# The Windows end of the unified stage/promote/rollback protocol.  One binding file rendered
# here from platform.json + release.json travels with the single lifecycle script over the
# authenticated SSH channel; the script's stdout is kept as the step evidence under the
# promotion receipt directory.
instance_lifecycle_status() {
  local generation="$1"
  local dir="$OPERATOR_STATE/promotions/$generation"
  local output="$dir/windows-status.out"
  mkdir -p "$dir"
  local evidence_path
  for evidence_path in "$output" "$output.ssh-rc" "$output.stderr" "$output.transport.json"; do
    if ! : > "$evidence_path"; then
      echo "instance_lifecycle_status: could not initialise status evidence in $dir" >&2
      return 2
    fi
  done
  local remote
  local remote_output="$dir/windows-status.remote.txt"
  if ! "$CONTROL_PYTHON" - "$CONFIG_FILE" "$generation" > "$remote_output" <<'PY'
import json
import sys

document = json.load(open(sys.argv[1], encoding="utf-8"))
instance = document["windows"]["instance"]
print(instance["state_root"].rstrip("/") + "/lifecycle/" + sys.argv[2])
PY
  then
    return 1
  fi
  read_output_path "$remote_output" remote || return 1
  local remote_script="$remote/instance-lifecycle.ps1"
  local remote_binding="$remote/windows-binding.json"
  local remote_rc=0
  windows_lifecycle "$output" "$remote_script" "$remote_binding" status || remote_rc=$?
  if ! printf '%s\n' "$remote_rc" > "$output.ssh-rc"; then
    echo "instance_lifecycle_status: could not persist the status SSH exit code in $dir" >&2
    return 2
  fi
  local parse_rc=0
  "$CONTROL_PYTHON" - "$output" "$remote_rc" <<'PY' || parse_rc=$?
import json
import sys

remote_rc = int(sys.argv[2])
try:
    document = json.loads(open(sys.argv[1], encoding="utf-8").read().strip().splitlines()[-1])
except (OSError, ValueError, IndexError):
    raise SystemExit(3)
if (
    remote_rc == 0
    and isinstance(document, dict)
    and document.get("passed") is True
    and document.get("verb") == "status"
):
    raise SystemExit(0)
if (
    remote_rc == 1
    and isinstance(document, dict)
    and document.get("passed") is False
    and isinstance(document.get("code"), str)
    and document["code"].strip()
):
    raise SystemExit(1)
raise SystemExit(3)
PY
  if [ "$parse_rc" = "1" ]; then
    echo "instance_lifecycle_status: the status result is a definite refusal; see $output" >&2
    return 1
  fi
  if [ "$parse_rc" != "0" ]; then
    echo "instance_lifecycle_status: the status outcome is unreadable; see $output" >&2
    return 2
  fi
  echo "$output"
}

instance_lifecycle() {
  local verb="$1" generation="$2" previous="${3:-}"
  case "$verb" in
    stage|promote|rollback|status) ;;
    *) die "operatorctl: unsupported lifecycle verb: $verb" ;;
  esac
  local dir="$OPERATOR_STATE/promotions/$generation"
  local binding="$dir/windows-binding.json" output="$dir/windows-$verb.out"
  mkdir -p "$dir"
  local evidence_path
  for evidence_path in "$output" "$output.ssh-rc" "$output.stderr" "$output.transport.json"; do
    if ! : > "$evidence_path"; then
      echo "instance_lifecycle: could not initialise $verb evidence in $dir" >&2
      return 2
    fi
  done
  "$CONTROL_PYTHON" - "$CONFIG_FILE" "$RELEASE_FILE" "$binding" "$generation" <<'PY' || return 1
import hashlib, json, os, sys
from pathlib import Path

config_path, release_path, out, generation = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4]
windows = json.loads(config_path.read_text(encoding="utf-8"))["windows"]
instance = windows["instance"]
release = json.loads(release_path.read_text(encoding="utf-8"))
# The native role is named explicitly in the release manifest; a full release also carries the
# portable linux archive and the deployment archive, so a bare *.zip filter is not a selection.
native = sorted(name for name in release["artifacts"] if name.endswith(".zip") and "-windows-" in name)
if len(native) != 1:
    raise SystemExit(f"operatorctl: the release manifest must name exactly one native Windows archive, got {native}")
archive = release_path.parent / native[0]
if not archive.is_file():
    raise SystemExit(f"operatorctl: the native Windows archive is missing next to the release manifest: {archive}")
checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
if checksum != release["artifacts"][native[0]]:
    raise SystemExit("operatorctl: the native Windows archive does not match the release manifest")
version, commit, source_sha = release["version"], release["source_commit"], release["source_sha256"]
override = os.environ.get("WINDOWS_RELEASE_OVERRIDE", "").split()
if len(override) == 3:
    version, commit, source_sha = override
remote = instance["state_root"].rstrip("/") + "/lifecycle/" + generation
out.write_text(
    json.dumps(
        {
            "task_name": instance["task_name"],
            "state_root": instance["state_root"],
            "package_root": instance["package_root"],
            "output_root": instance["output_root"],
            "port": windows["endpoint_port"],
            "generation": generation,
            "dir": remote,
            "archive": native[0],
            "archive_local": str(archive),
            "artifact_sha256": checksum,
            "version": version,
            "source_commit": commit,
            "source_sha256": source_sha,
        },
        indent=2,
    )
    + "\n",
    encoding="utf-8",
)
PY
  local remote archive_local archive_name expected_archive_size expected_archive_hash remote_archive_state
  local archive_probe="$dir/windows-$verb-archive-state.ps1"
  remote="$("$CONTROL_PYTHON" -c 'import json,sys; print(json.load(open(sys.argv[1]))["dir"])' "$binding")"
  archive_local="$("$CONTROL_PYTHON" -c 'import json,sys; print(json.load(open(sys.argv[1]))["archive_local"])' "$binding")"
  archive_name="$("$CONTROL_PYTHON" -c 'import json,sys; print(json.load(open(sys.argv[1]))["archive"])' "$binding")"
  expected_archive_hash="$("$CONTROL_PYTHON" -c 'import json,sys; print(json.load(open(sys.argv[1]))["artifact_sha256"])' "$binding")"
  expected_archive_size="$(stat -c %s "$archive_local")" \
    || { echo "instance_lifecycle: could not size the native archive: $archive_local" >&2; return 1; }
  # The generation directory exists first (private, under the instance state root); the archive
  # travels with the binding and the script, and the script consumes exactly the transferred file.
  "$CONTROL_PYTHON" - "$remote" <<'PY' \
    | windows_transport 60 "$output.transport.json" 2> "$output.stderr" \
    || { echo "instance_lifecycle: could not create $remote on $SOLIDWORKS_SSH_HOST" >&2; return 1; }
import sys

path = sys.argv[1].replace("/", "\\").replace("'", "''")
print("New-Item -ItemType Directory -Force -Path '" + path + "' | Out-Null")
PY
  # Stage creates the archive once. Promote, rollback and reconcile reuse that
  # immutable staged copy when its remote size and digest still match the
  # binding. A present-but-different archive is a refusal, never an overwrite.
  "$CONTROL_PYTHON" - "$remote/$archive_name" > "$archive_probe" <<'PY' || return 1
import sys

path = sys.argv[1].replace("/", "\\").replace("'", "''")
print(
    "if (Test-Path -LiteralPath '" + path + "') {"
    "$item=Get-Item -LiteralPath '" + path + "';"
    "Write-Output ('{0}|{1}' -f $item.Length,"
    "(Get-FileHash -LiteralPath '" + path + "' -Algorithm SHA256).Hash.ToLower())"
    "} else { Write-Output 'absent' }"
)
PY
  remote_archive_state="$(
    windows_transport 60 "$output.transport.json" < "$archive_probe" \
      2> "$output.stderr" | tr -d '\r' | tail -1
  )" || { echo "instance_lifecycle: could not inspect the staged archive on $SOLIDWORKS_SSH_HOST" >&2; return 1; }
  local -a transfer_inputs=("$binding" "$HERE/../windows/instance-lifecycle.ps1")
  case "$remote_archive_state" in
    absent)
      transfer_inputs+=("$archive_local")
      ;;
    "$expected_archive_size|$expected_archive_hash")
      ;;
    *)
      echo "instance_lifecycle: staged archive on $SOLIDWORKS_SSH_HOST does not match the release binding" >&2
      return 1
      ;;
  esac
  scp -q -o BatchMode=yes "${transfer_inputs[@]}" "$SOLIDWORKS_SSH_HOST:$remote/" \
    || { echo "instance_lifecycle: could not transfer the lifecycle inputs to $SOLIDWORKS_SSH_HOST" >&2; return 1; }
  local stage_dir=""
  case "$verb" in
    stage|promote) stage_dir="$remote/generation" ;;
  esac
  local remote_rc=0
  windows_lifecycle \
    "$output" "$remote/instance-lifecycle.ps1" "$remote/windows-binding.json" \
    "$verb" "$stage_dir" "$previous" || remote_rc=$?
  if ! printf '%s\n' "$remote_rc" > "$output.ssh-rc"; then
    echo "instance_lifecycle: could not persist the $verb SSH exit status in $dir" >&2
    return 2
  fi
  # The remote lifecycle returns a structured passed:false object and exit 1 for a definite
  # refusal, while rc 255 (and other transport/process failures) can lose the acknowledgement.
  # Classify both signals together: parsing after an unconditional exit test would turn every
  # intentional refusal into unknown before its evidence was read.
  local parse_rc=0
  "$CONTROL_PYTHON" - "$output" "$verb" "$remote_rc" <<'PY' || parse_rc=$?
import json
import sys

remote_rc = int(sys.argv[3])
try:
    document = json.loads(open(sys.argv[1], encoding="utf-8").read().strip().splitlines()[-1])
except (OSError, ValueError, IndexError) as error:
    raise SystemExit(3)
if not isinstance(document, dict) or document.get("verb") != sys.argv[2]:
    raise SystemExit(3)
if remote_rc == 0 and document.get("passed") is True:
    raise SystemExit(0)
code = document.get("code")
if remote_rc in (0, 1) and document.get("passed") is False and isinstance(code, str) and code.strip():
    raise SystemExit(1)
raise SystemExit(3)
PY
  if [ "$parse_rc" = "1" ]; then
    echo "instance_lifecycle: the $verb result is a definite refusal; see $output" >&2
    return 1
  fi
  if [ "$parse_rc" != "0" ]; then
    echo "instance_lifecycle: the $verb outcome is unreadable; see $output" >&2
    return 2
  fi
  echo "$output"
}

promotion_step() {
  local generation="$1" verb="$2" step="$3" status="$4"
  shift 4
  local arguments=(--state-root "$OPERATOR_STATE" --install-id "$generation" --verb "$verb" --step "$step" --status "$status")
  local item sibling
  for item in "$@"; do
    if [ ! -e "$item" ]; then
      [ "$status" != "completed" ] \
        || die "completed $verb/$step evidence is missing: $item"
      mkdir -p "$(dirname "$item")"
      printf 'evidence path was not produced by the lifecycle call\n' > "$item"
    fi
    arguments+=(--evidence "$item")
    for sibling in "$item.transport.json" "$item.stderr" "$item.ssh-rc"; do
      [ -f "$sibling" ] && arguments+=(--evidence "$sibling")
    done
  done
  "$CONTROL_PYTHON" "$HERE/promotion.py" step "${arguments[@]}" \
    --freeze-root "$OPERATOR_STATE/promotions/$generation/evidence-immutable" >/dev/null
}

require_rollback_archive() {
  local receipt_dir="$1" reference_status=0
  config_reference_recorded "$receipt_dir/receipt.json" || reference_status=$?
  [ "$reference_status" != 2 ] || die "recovery_history_unavailable: rollback receipt is unreadable"
  if [ "$reference_status" = 0 ] || [ -e "$receipt_dir/evidence/private/connection-before-promotion.json" ]; then
    [ -f "$receipt_dir/backup.tar.enc" ] && [ ! -L "$receipt_dir/backup.tar.enc" ] \
      || die "recovery_history_unavailable: rollback requires an excluded historical archive; no action was taken"
  fi
}

config_reference_recorded() {
  "$CONTROL_PYTHON" - "$1" <<'PY'
import json
import sys

def reject_duplicates(pairs):
    seen = set()
    for key, _ in pairs:
        if key in seen:
            raise ValueError(f"duplicate receipt key: {key!r}")
        seen.add(key)
    return dict(pairs)

try:
    document = json.loads(
        open(sys.argv[1], encoding="utf-8").read(),
        object_pairs_hook=reject_duplicates,
    )
except (OSError, ValueError):
    raise SystemExit(2)
steps = document.get("steps") if isinstance(document, dict) else None
if not isinstance(steps, list):
    raise SystemExit(2)
raise SystemExit(
    0
    if any(
        row.get("verb") == "promote"
        and row.get("step") == "config-reference"
        for row in steps
        if isinstance(row, dict)
    )
    else 1
)
PY
}

promotion_extract_health() {
  "$CONTROL_PYTHON" - "$1" "$2" <<'PY'
import json
import sys

document = json.load(open(sys.argv[1], encoding="utf-8"))
health = document.get("health")
if document.get("passed") is not True or document.get("verb") != "health" or not isinstance(health, dict):
    raise SystemExit("the health relay is not a passed health object")
open(sys.argv[2], "w", encoding="utf-8").write(json.dumps(health) + "\n")
PY
}

windows_status_check() {
  "$CONTROL_PYTHON" - "$CONFIG_FILE" "${OPERATOR_STATE_DIR:-$OPERATOR_STATE}/install-record.json" "$HERE" <<'PY'
import base64
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, sys.argv[3])
from windows_transport import (
    TransportDecodeError,
    completed_metadata,
    decode_output,
    error_streams,
    raw_stream_metadata,
    request_metadata,
    run_powershell,
    write_metadata,
)
from windows_identity import (
    endpoint_health_matches_source,
    sealed_source_listener_matches,
)

config = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
record = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
windows = config.get("windows")
if not isinstance(windows, dict) or not isinstance(windows.get("instance"), dict):
    raise SystemExit("operatorctl status: Windows instance configuration is missing")
ends = record.get("ends")
expected = ends.get("windows") if isinstance(ends, dict) else None
if not isinstance(expected, dict):
    raise SystemExit("operatorctl status: Windows install identity is missing")
host = windows.get("ssh_host")
instance = windows["instance"]
task_name = instance.get("task_name")
port = windows.get("endpoint_port")
if (
    not isinstance(host, str)
    or not host
    or not isinstance(task_name, str)
    or not task_name
    or type(port) is not int
    or not 0 < port < 65536
):
    raise SystemExit("operatorctl status: Windows endpoint identity is malformed")
identity = {
    "task_name": task_name,
    "port": port,
    "install_id": expected.get("install_id"),
    "config_digest": expected.get("config_digest"),
    "release": expected.get("release"),
}
encoded = base64.b64encode(
    json.dumps(identity, separators=(",", ":"), sort_keys=True).encode()
).decode("ascii")
script = rf"""
$ErrorActionPreference = 'Stop'
$expected = ConvertFrom-Json ([Text.Encoding]::UTF8.GetString(
  [Convert]::FromBase64String('{encoded}')))
function Get-Sample {{
  $task = Get-ScheduledTask -TaskName $expected.task_name -ErrorAction Stop
  if ($task.State -ne 'Running') {{ throw "task state is $($task.State)" }}
  if (@($task.Actions).Count -ne 1) {{ throw 'task action count differs from exactly one' }}
  $action = $task.Actions[0]
  $execute = [string]$action.Execute
  $scripts = [IO.Path]::GetDirectoryName($execute)
  $runtime = [IO.Path]::GetDirectoryName($scripts)
  $generation = [IO.Path]::GetDirectoryName($runtime)
  if (-not $generation -or
      $execute -ne (Join-Path $generation 'runtime\Scripts\python.exe')) {{
    throw 'task executable is not the managed generation runtime'
  }}
  $generationRoot = Split-Path -Parent $generation
  $lifecycle = Join-Path $generationRoot 'instance-lifecycle.ps1'
  $binding = Join-Path $generationRoot 'windows-binding.json'
  if (-not (Test-Path -LiteralPath $lifecycle -PathType Leaf) -or
      -not (Test-Path -LiteralPath $binding -PathType Leaf)) {{
    throw 'task generation has no lifecycle identity'
  }}
  $hostExecutable = if (Test-Path -LiteralPath (Join-Path $PSHOME 'powershell.exe')) {{
    Join-Path $PSHOME 'powershell.exe'
  }} elseif (Test-Path -LiteralPath (Join-Path $PSHOME 'pwsh')) {{
    Join-Path $PSHOME 'pwsh'
  }} else {{
    throw 'the PowerShell host executable is unavailable'
  }}
  $output = @(& $hostExecutable -NoProfile -NonInteractive -ExecutionPolicy Bypass `
    -File $lifecycle -Verb status -Binding $binding 2>&1)
  if ($LASTEXITCODE -ne 0) {{
    throw 'the managed lifecycle status command failed'
  }}
  try {{
    $document = (($output | Select-Object -Last 1) | ConvertFrom-Json)
  }} catch {{
    throw 'the managed lifecycle status returned no JSON'
  }}
  if ($document.passed -ne $true -or $document.verb -ne 'status' -or
      $null -eq $document.live_task) {{
    throw 'the managed lifecycle status did not return a live task'
  }}
  $live = $document.live_task
  if ($live.state -ne 'Running' -or $live.action_count -ne 1 -or
      $live.maintenance_relation -notin @('source','target','unknown') -or
      -not $live.endpoint_root -or -not $live.config_path -or
      -not $live.config_digest -or -not $live.install_id -or
      -not $live.task_xml -or -not $live.task_xml_sha256 -or
      -not $live.task_semantic_sha256 -or
      -not $live.task_acl_sha256 -or $null -eq $live.listener -or
      $null -eq $live.health -or $null -eq $live.release) {{
    throw 'the managed lifecycle status did not prove the running task identity'
  }}
  $health = $live.health
  $listener = $live.listener
  $pendingRollback = $document.pending_rollback
  if ($null -ne $pendingRollback) {{
    if ($pendingRollback.state -eq 'present') {{
      if (-not $pendingRollback.previous -or
          -not $pendingRollback.target_generation -or
          -not $pendingRollback.restore_to -or
          -not $pendingRollback.current_snapshot -or
          [string]$pendingRollback.source_state_sha256 -cnotmatch '^[0-9a-f]{{64}}$') {{
        throw 'the managed lifecycle returned an incomplete rollback intent'
      }}
    }} elseif ($pendingRollback.state -ne 'unknown') {{
      throw 'the managed lifecycle returned an invalid rollback intent state'
    }}
  }}
  [ordered]@{{
    task_name = [string]$task.TaskName
    task_path = [string]$task.TaskPath
    task_state = [string]$task.State
    action_count = @($task.Actions).Count
    task_xml = [string]$live.task_xml
    task_xml_sha256 = [string]$live.task_xml_sha256
    task_semantic_sha256 = [string]$live.task_semantic_sha256
    task_acl_sha256 = [string]$live.task_acl_sha256
    endpoint_root = [string]$live.endpoint_root
    config_path = [string]$live.config_path
    listener = $listener
    install_id = [string]$live.install_id
    release_identity_sha256 = [string]$live.release_identity_sha256
    config_digest = [string]$live.config_digest
    release = [ordered]@{{
      version = [string]$live.release.version
      source_commit = [string]$live.release.source_commit
      source_sha256 = [string]$live.release.source_sha256
      role = [string]$live.release.role
    }}
    health = [ordered]@{{
      schema_version = [string]$health.schema_version
      pipeline_id = [string]$health.pipeline_id
      ready = [bool]$health.ready
      native_platform = [bool]$health.native_platform
      release = [ordered]@{{
        version = [string]$health.release.version
        source_commit = [string]$health.release.source_commit
        source_sha256 = [string]$health.release.source_sha256
        role = [string]$health.release.role
      }}
      endpoint = [ordered]@{{
        port = [int]$health.endpoint.port
      }}
    }}
    maintenance_relation = [string]$live.maintenance_relation
    source = $live.source
    pending_rollback = $pendingRollback
  }}
}}
try {{
  $first = Get-Sample
  Start-Sleep -Seconds 1
  $second = Get-Sample
  $firstJson = $first | ConvertTo-Json -Depth 8 -Compress
  $secondJson = $second | ConvertTo-Json -Depth 8 -Compress
  if ($firstJson -ne $secondJson) {{
    throw 'two status probes returned different process or install identity'
  }}
  @{{
    passed = $true
    sample = $first
  }} | ConvertTo-Json -Depth 8 -Compress
}} catch {{
  @{{
    passed = $false
    error = $_.Exception.Message
  }} | ConvertTo-Json -Depth 8 -Compress
  exit 1
}}
"""
ssh_bin = os.environ.get("SOLIDWORKS_SSH_BIN", "ssh")
evidence_root = Path(os.environ["OPERATOR_STATE"]) / "health-gate"
evidence_root.mkdir(parents=True, exist_ok=True)
evidence_stem = f"windows-status-{time.time_ns()}-{os.getpid()}"
stdout_path = evidence_root / (evidence_stem + ".stdout")
stderr_path = evidence_root / (evidence_stem + ".stderr")
metadata_path = evidence_root / (evidence_stem + ".transport.json")
base_metadata = {
    "schema_version": "solidworks-to-urdf.windows-transport/v1",
    "host": host,
    "timeout_seconds": 60,
    "returncode": None,
    "stdout": raw_stream_metadata(b""),
    "stderr": raw_stream_metadata(b""),
    "transport": request_metadata(script),
}


def persist_status_evidence(raw_stdout, raw_stderr, metadata):
    stdout_path.write_bytes(raw_stdout)
    stderr_path.write_bytes(raw_stderr)
    document = {
        **base_metadata,
        **metadata,
        "stdout": raw_stream_metadata(raw_stdout),
        "stderr": raw_stream_metadata(raw_stderr),
        "raw_evidence": {
            "stdout": str(stdout_path),
            "stderr": str(stderr_path),
        },
    }
    write_metadata(metadata_path, document)


try:
    completed = run_powershell(
        ssh_host=host,
        script=script,
        timeout=60,
        ssh_bin=ssh_bin,
    )
except (OSError, subprocess.SubprocessError, ValueError) as error:
    raw_stdout, raw_stderr = error_streams(error)
    persist_status_evidence(
        raw_stdout,
        raw_stderr,
        {
            "status": "error",
            "error_type": type(error).__name__,
            "error": str(error),
        },
    )
    raise SystemExit(f"operatorctl status: Windows probe failed: {error}") from error
try:
    stderr_text = decode_output(completed.stderr, stream="stderr")
    stdout_text = decode_output(completed.stdout, stream="stdout")
except TransportDecodeError as error:
    persist_status_evidence(
        completed.stdout,
        completed.stderr,
        {
            "status": "decode_error",
            "returncode": completed.returncode,
            "error_type": type(error).__name__,
            "error": str(error),
            "decode_error": {
                "stream": error.stream,
                "bytes": error.payload_bytes,
                "sha256": error.payload_sha256,
            },
        },
    )
    raise SystemExit(
        f"operatorctl status: Windows {error.stream} decode failed: "
        f"{error.payload_bytes} bytes sha256={error.payload_sha256}"
    ) from error
if completed.returncode != 0:
    persist_status_evidence(
        completed.stdout,
        completed.stderr,
        {
            "status": "error",
            "returncode": completed.returncode,
            "error_type": "RemoteExit",
        },
    )
    raise SystemExit(
        "operatorctl status: Windows probe failed: "
        + (stderr_text.strip() or stdout_text.strip() or "unknown failure")[:1000]
    )
try:
    result = json.loads(stdout_text.strip().splitlines()[-1])
except (IndexError, ValueError) as error:
    persist_status_evidence(
        completed.stdout,
        completed.stderr,
        {
            "status": "error",
            "returncode": completed.returncode,
            "error_type": "InvalidJson",
        },
    )
    raise SystemExit("operatorctl status: Windows probe returned no JSON") from error
if not isinstance(result, dict) or result.get("passed") is not True:
    persist_status_evidence(
        completed.stdout,
        completed.stderr,
        {
            "status": "error",
            "returncode": completed.returncode,
            "error_type": "Refused",
        },
    )
    raise SystemExit(
        "operatorctl status: "
        + str(result.get("error") if isinstance(result, dict) else "invalid result")
    )
sample = result.get("sample")
source = sample.get("source") if isinstance(sample, dict) else None
listener = sample.get("listener") if isinstance(sample, dict) else None
health = sample.get("health") if isinstance(sample, dict) else None
pending_rollback = sample.get("pending_rollback") if isinstance(sample, dict) else None
pending_source = pending_rollback.get("source") if isinstance(pending_rollback, dict) else None


if (
    not isinstance(sample, dict)
    or sample.get("task_name") != task_name
    or sample.get("task_state") != "Running"
    or type(sample.get("action_count")) is not int
    or sample.get("action_count") != 1
    or sample.get("install_id") != identity["install_id"]
    or sample.get("config_digest") != identity["config_digest"]
    or sample.get("release") != identity["release"]
    or not isinstance(sample.get("release_identity_sha256"), str)
    or not re.fullmatch(r"[0-9a-f]{64}", sample.get("release_identity_sha256", ""))
    or not isinstance(sample.get("task_path"), str)
    or not sample.get("task_path")
    or not isinstance(sample.get("endpoint_root"), str)
    or not sample.get("endpoint_root")
    or not isinstance(sample.get("config_path"), str)
    or not sample.get("config_path")
    or not isinstance(sample.get("task_xml_sha256"), str)
    or not re.fullmatch(r"[0-9a-f]{64}", sample.get("task_xml_sha256", ""))
    or not isinstance(sample.get("task_xml"), str)
    or not sample.get("task_xml")
    or not isinstance(sample.get("task_semantic_sha256"), str)
    or not re.fullmatch(r"[0-9a-f]{64}", sample.get("task_semantic_sha256", ""))
    or not isinstance(sample.get("task_acl_sha256"), str)
    or not re.fullmatch(r"[0-9a-f]{64}", sample.get("task_acl_sha256", ""))
    or sample.get("maintenance_relation") not in {"source", "target", "unknown"}
    or not isinstance(source, dict)
    or source.get("task_name") != sample.get("task_name")
    or source.get("task_path") != sample.get("task_path")
    or source.get("task_xml_sha256") != sample.get("task_xml_sha256")
    or source.get("task_semantic_sha256") != sample.get("task_semantic_sha256")
    or source.get("task_acl_sha256") != sample.get("task_acl_sha256")
    or source.get("endpoint_root") != sample.get("endpoint_root")
    or source.get("config_path") != sample.get("config_path")
    or source.get("port") != port
    or source.get("config_digest") != sample.get("config_digest")
    or source.get("install_id") != sample.get("install_id")
    or source.get("release") != sample.get("release")
    or source.get("release_identity_sha256")
    != sample.get("release_identity_sha256")
    or listener != (source.get("listener") if isinstance(source, dict) else None)
    or not sealed_source_listener_matches(source)
    or not endpoint_health_matches_source(health, source)
    or (
        pending_rollback is not None
        and (
            not isinstance(pending_rollback, dict)
            or pending_rollback.get("state") not in {"present", "unknown"}
            or (
                pending_rollback.get("state") == "present"
                and (
                    not isinstance(pending_rollback.get("previous"), str)
                    or not pending_rollback["previous"]
                    or not isinstance(pending_rollback.get("target_generation"), str)
                    or not pending_rollback["target_generation"]
                    or not isinstance(pending_rollback.get("restore_to"), str)
                    or not pending_rollback["restore_to"]
                    or not isinstance(pending_rollback.get("current_snapshot"), str)
                    or not pending_rollback["current_snapshot"]
                    or not isinstance(
                        pending_rollback.get("source_state_sha256"), str
                    )
                    or not re.fullmatch(
                        r"[0-9a-f]{64}",
                        pending_rollback.get("source_state_sha256", ""),
                    )
                    or not isinstance(pending_source, dict)
                    or not sealed_source_listener_matches(pending_source)
                )
            )
            or (
                pending_rollback.get("state") == "unknown"
                and pending_rollback.get("code") != "rollback_intent_unreadable"
            )
        )
    )
):
    persist_status_evidence(
        completed.stdout,
        completed.stderr,
        {
            "status": "error",
            "returncode": completed.returncode,
            "error_type": "IdentityMismatch",
        },
    )
    raise SystemExit("operatorctl status: Windows process/identity probe differs from the install record")
status_metadata = {
    **completed_metadata(completed),
    "status": "completed",
    "host": host,
    "timeout_seconds": 60,
}
persist_status_evidence(completed.stdout, completed.stderr, status_metadata)
print(
    json.dumps(
        {
            "task_name": task_name,
            "task_path": sample["task_path"],
            "task_state": "Running",
            "action_count": sample["action_count"],
            "listener": listener,
            "health": health,
            "task_xml_sha256": sample["task_xml_sha256"],
            "task_xml": sample["task_xml"],
            "task_semantic_sha256": sample["task_semantic_sha256"],
            "task_acl_sha256": sample["task_acl_sha256"],
            "endpoint_root": sample["endpoint_root"],
            "config_path": sample["config_path"],
            "install_id": sample["install_id"],
            "config_digest": sample["config_digest"],
            "release_identity_sha256": sample["release_identity_sha256"],
            "maintenance_relation": sample["maintenance_relation"],
            "source": source,
            "pending_rollback": pending_rollback,
            "transport": getattr(completed, "transport", {}),
            "evidence": {
                "stdout": str(stdout_path),
                "stderr": str(stderr_path),
                "metadata": str(metadata_path),
            },
        },
        sort_keys=True,
    )
)
PY
}

load_installed() {
  OPERATOR_STATE_DIR="$(generation_env_value "$ENV_FILE" OPERATOR_STATE)"
  [ -n "$OPERATOR_STATE_DIR" ] || die "OPERATOR_STATE is not set in $ENV_FILE"
  [ -f "$OPERATOR_STATE_DIR/resolved.env" ] \
    || die "not installed: $OPERATOR_STATE_DIR/resolved.env is missing (run install first)"
  load_env_file "$OPERATOR_STATE_DIR/resolved.env" || return $?
  # The verified generation is the final layer: values in its file override both the shared
  # resolved environment and any same-name ambient variable, then are exported to child
  # processes through the one loader above.
  load_generation_env || return $?
  # Every port comes from the single deploy source (render_operator.py) via resolved.env,
  # unless the generation-specific file explicitly overrides it.
  for key in AIRFLOW_API_PORT TUNNEL_LOOPBACK_PORT POSTGRES_PORT SOLIDWORKS_ENDPOINT_PORT; do
    [ -n "${!key:-}" ] || die "resolved.env is missing $key; run install to re-render the deployment"
  done
  TARGET="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
}

# Read-only: render the configuration and every managed unit into temporary directories and
# compare with the installed deployment. Missing installed files, missing renders and render
# failures count as drift, not as silent passes.
drift_check() {
  local rc=0 tmp_xdg drift_log tmp_state proxy_bin="${NGINX_BIN:-}" extra=() proxy_issue
  # Read-only: the bound binary must exist, be executable and be the one the installed proxy
  # unit executes.  A missing or mismatched binary is drift; reading never installs, downloads
  # or repairs (the side-effectful preparation belongs to install / promote / rollback only).
  if ! proxy_issue="$(check_proxy_binary --unit-file "$TARGET/description-operator-proxy.service" 2>&1)"; then
    echo "DRIFT $proxy_issue"
    rc=1
  fi
  [ -n "$proxy_bin" ] && extra=(--nginx-bin "$proxy_bin")
  drift_log="$(mktemp)"
  tmp_xdg="$(mktemp -d)"
  if ! "$CONTROL_PYTHON" "$HERE/render_operator.py" --env-file "$ENV_FILE" --dry-run \
        "${extra[@]}" --units-dir "$tmp_xdg/systemd/user" > "$drift_log"; then rc=1; fi
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
  "$CONTROL_PYTHON" -c "import shutil,sys; shutil.rmtree(sys.argv[1], ignore_errors=True)" "$tmp_xdg"
  rm -f "$drift_log"
  return "$rc"
}

promotion_replay_state() {
  local install_id="$1" verb="$2" output rc=0
  output="$("$CONTROL_PYTHON" "$HERE/promotion.py" replay --state-root "$OPERATOR_STATE" \
    --install-id "$install_id" --verb "$verb" \
    --active-record "$STATE_ROOT/install-record.json" 2>&1)" || rc=$?
  case "$rc" in
    0) printf '%s\n' "$output" ;;
    10) printf '%s\n' "$output"; return 10 ;;
    11) printf '%s\n' "${output:-replay=partial}"; return 11 ;;
    *) printf '%s\n' "${output:-promotion replay check failed}" >&2; return 2 ;;
  esac
}

verify_completed_replay() {
  load_installed
  windows_status_check >/dev/null || return 1
  OPERATOR_STATE="$OPERATOR_STATE" bash "$HERE/health.sh" >/dev/null || return 1
}

[ -n "$CONFIG_SOURCE_FILE" ] || usage
if [ "$ACTION" = "reconcile" ]; then
  # The maintenance reconcile may name a staged generation (--install-id); the install asset
  # inputs belong to the install/stage family alone.
  [ -z "$WHEEL_FILE$ENDPOINT_IDENTITY$RELEASE_FILE" ] || usage
elif [ -n "$INSTALL_ID$WHEEL_FILE$ENDPOINT_IDENTITY$RELEASE_FILE" ] && [ "$ACTION" != "install" ] \
   && [ "$ACTION" != "stage" ] && [ "$ACTION" != "promote" ] && [ "$ACTION" != "rollback" ]; then
  usage
fi
snapshot_config
# Every verb passes the same static config gate first; the install record and its derived
# per-generation environment stay the single authority for the operational verbs.
"$CONTROL_PYTHON" "$HERE/platform_config.py" validate --config "$CONFIG_FILE" >/dev/null || exit $?
case "$ACTION" in
  drain|stop|reconcile|stage|promote|rollback)
    resolve_state_root
    acquire_maintenance_lock "$STATE_ROOT"
    LOCKED_STATE_ROOT="$STATE_ROOT"
    assert_config_unchanged
    ;;
esac
case "$ACTION" in
  validate)
    echo "operatorctl: configuration valid: $CONFIG_SOURCE_FILE"
    exit 0 ;;
  install)
    install_from_config ;;
  start)
    # Cold-start order inside the single entry: bring the managed database up and confirm its
    # socket, then actually close the platform admission through the controlled entry (BEFORE
    # any identity/drift verification, so a failed start attempt cannot leave a leftover open
    # gate), then run the preconditions; the gate reopens only after everything is ready (under
    # --keep-admission-closed it stays closed once everything is ready).
    START_STATE_ROOT="$("$CONTROL_PYTHON" "$HERE/platform_config.py" resolve --config "$CONFIG_FILE" | \
      "$CONTROL_PYTHON" -c 'import json,sys; print(json.load(sys.stdin)["storage"]["state_root"])')" || exit $?
    [ -f "$START_STATE_ROOT/resolved.env" ] \
      || die "not installed: $START_STATE_ROOT/resolved.env is missing (run install first)"
    STATE_ROOT="$START_STATE_ROOT"
    acquire_maintenance_lock "$STATE_ROOT"
    LOCKED_STATE_ROOT="$STATE_ROOT"
    assert_config_unchanged
    load_env_file "$START_STATE_ROOT/resolved.env"
    ensure_postgres_ready
    platform_close_admission "start-window" "operatorctl start"
    load_active_env ;;
  drain|stop|status|health|reconcile)
    if [ "$ACTION" != "reconcile" ] || [ -z "$INSTALL_ID" ]; then
      load_active_env
    else
      # The bootstrap maintenance window: the staged candidate generation runs the canonical
      # recovery against the same real database.  The running record stays the reference, the
      # staged generation itself is re-verified below (recorded snapshot, wheel binding to the
      # release manifest, the runtime the reconcile executes), and the active pointer, units
      # and admission stay untouched.  The channel is closed-window only: it refuses while
      # the admission is open.
      load_running_env
      CANDIDATE_ID="$INSTALL_ID"
      RECEIPT_DIR="$OPERATOR_STATE/promotions/$CANDIDATE_ID"
      [ -f "$RECEIPT_DIR/receipt.json" ] \
        || die "no stage receipt for $CANDIDATE_ID; run stage first"
      RECONCILE_SCHEMA_COMPLETED=0
      RECONCILE_SCHEMA_STATE_RC=0
      RECONCILE_SCHEMA_STATE="$(
        "$CONTROL_PYTHON" "$HERE/promotion.py" step-state \
          --state-root "$OPERATOR_STATE" --install-id "$CANDIDATE_ID" \
          --verb reconcile --step schema-migration
      )" || RECONCILE_SCHEMA_STATE_RC=$?
      case "$RECONCILE_SCHEMA_STATE_RC" in
        0)
          [ "$RECONCILE_SCHEMA_STATE" = "step=completed" ] \
            || die "the reconcile schema-migration step is $RECONCILE_SCHEMA_STATE; refusing a duplicate mutation"
          RECONCILE_SCHEMA_COMPLETED=1
          ;;
        10) ;;
        *) die "the reconcile schema-migration state is unreadable; refusing before DDL" ;;
      esac
      resolve_release_file
      "$CONTROL_PYTHON" "$HERE/promotion.py" verify-release --state-root "$OPERATOR_STATE" \
        --install-id "$CANDIDATE_ID" --release-file "$RELEASE_FILE" >/dev/null \
        || die "the staged promotion receipt binds another release manifest"
      CANDIDATE_ENV="$OPERATOR_STATE/derived/$CANDIDATE_ID/operator.env"
      [ -f "$CANDIDATE_ENV" ] || die "the staged generation $CANDIDATE_ID is incomplete; re-run stage"
      # The generated environment of the generation this action runs now is the candidate's.
      ENV_FILE="$CANDIDATE_ENV"
      AIRFLOW_VENV="$(generation_env_value "$CANDIDATE_ENV" AIRFLOW_VENV)"
      CANDIDATE_WHEEL_PATH="$(generation_env_value "$CANDIDATE_ENV" PIPELINE_WHEEL)"
      CANDIDATE_DIGEST="$(generation_env_value "$CANDIDATE_ENV" PLATFORM_CONFIG_DIGEST)"
      # The running side of the authority check stays bound to the verified active record:
      # ACTIVE_ID comes from `install_record.py verify-running` (the active record checked
      # against its frozen snapshot), and its generation environment is the path read here.
      # Its values are decoded as data (never sourced, so nothing can inherit into them); the
      # pre-contract writer's bare single-word values are that generation's declared values and
      # decode exactly, while a missing, empty or ambiguous declaration refuses.
      RUNNING_ENV="$STATE_ROOT/derived/$ACTIVE_ID/operator.env"
      [ -f "$RUNNING_ENV" ] || die "the running generation environment is missing: $RUNNING_ENV"
      CAND_SERVICE_DSN="$(generation_env_value "$CANDIDATE_ENV" PLATFORM_SERVICE_DSN)"
      CAND_OPERATOR_DSN="$(generation_env_value "$CANDIDATE_ENV" PLATFORM_OPERATOR_DSN)"
      CAND_RUNTIME_DSN="$(generation_env_value "$CANDIDATE_ENV" AIRFLOW_RUNTIME_DB_URL)"
      RUN_SERVICE_DSN="$(generation_env_declared_value "$RUNNING_ENV" PLATFORM_SERVICE_DSN)"
      RUN_OPERATOR_DSN="$(generation_env_declared_value "$RUNNING_ENV" PLATFORM_OPERATOR_DSN)"
      RUN_RUNTIME_DSN="$(generation_env_declared_value "$RUNNING_ENV" AIRFLOW_RUNTIME_DB_URL)"
      [ -n "$CAND_SERVICE_DSN" ] && [ "$CAND_SERVICE_DSN" = "$RUN_SERVICE_DSN" ] \
        && [ "$CAND_OPERATOR_DSN" = "$RUN_OPERATOR_DSN" ] && [ "$CAND_RUNTIME_DSN" = "$RUN_RUNTIME_DSN" ] \
        || die "the staged generation names another database authority than the running generation; refused"
      [ -x "$AIRFLOW_VENV/bin/python" ] || die "the staged generation runtime is missing; re-run stage"
      # The one live authority check: both generations must reach the same real cluster and
      # database (a copied or foreign database never satisfies the recovery).
      PLATFORM_SERVICE_DSN="$CAND_SERVICE_DSN" RUNNING_SERVICE_DSN="$RUN_SERVICE_DSN" \
        "$AIRFLOW_VENV/bin/python" - <<'GUARD' || die "the staged generation does not reach the running database authority; refused"
import os

import psycopg2


def probe(dsn):
    with psycopg2.connect(dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_database(), pg_postmaster_start_time()::text")
            return cursor.fetchone()


if probe(os.environ["PLATFORM_SERVICE_DSN"]) != probe(os.environ["RUNNING_SERVICE_DSN"]):
    raise SystemExit("the staged generation reaches another database authority")
GUARD
      PLATFORM_OPERATOR_DSN="$CAND_OPERATOR_DSN" "$AIRFLOW_VENV/bin/python" \
        -m description_pipeline.orchestration.lifecycle_state report \
        --require-admission-closed >/dev/null \
        || die "the staged-candidate reconcile runs only while the admission is closed"
      "$CONTROL_PYTHON" - "$HERE/../../release.json" "$RECEIPT_DIR/receipt.json" "$CANDIDATE_DIGEST" \
        "$CANDIDATE_WHEEL_PATH" "$AIRFLOW_VENV" "$HERE" <<'GUARD' || die "the staged generation identity did not verify; re-run stage"
# staged-generation identity: the recorded snapshot, the wheel binding and the installed package
import hashlib
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[6])
from install_record import release_identity, wheel_binding  # noqa: E402  (canonical owners)

release_file, receipt_file, wheel_path, venv = (Path(sys.argv[index]) for index in (1, 2, 4, 5))
config_digest = sys.argv[3]
release = release_identity(json.loads(release_file.read_text(encoding="utf-8")))
receipt = json.loads(receipt_file.read_text(encoding="utf-8"))
snapshot = Path(receipt["to"])
if not snapshot.is_file() or hashlib.sha256(snapshot.read_bytes()).hexdigest() != receipt["to_sha256"]:
    raise SystemExit("the staged snapshot is missing or no longer matches its recorded digest")
if config_digest != receipt["to_sha256"]:
    raise SystemExit(f"the staged environment binds another snapshot generation ({config_digest} != {receipt['to_sha256']})")
steps = {step.get("step"): step.get("status") for step in receipt.get("steps", [])}
for name in ("linux-generation", "windows-generation"):
    if steps.get(name) != "completed":
        raise SystemExit(f"the staged generation has no completed {name} step")
wheel_binding(release, wheel=wheel_path)
runtime = subprocess.run(
    [str(venv / "bin" / "python"), "-c",
     "import json, pathlib, description_pipeline as p;"
     "from description_pipeline import interface_records;"
     "from description_pipeline.delivery import PIPELINE_ID;"
     "root = pathlib.Path(p.__file__).resolve().parent;"
     "print(json.dumps({'root': str(root), 'pipeline_id': PIPELINE_ID,"
     " 'domain': interface_records.release_domain_identity(),"
     " 'identity': json.loads((root / 'tool-release.json').read_text(encoding='utf-8'))}))"],
    capture_output=True, text=True,
)
if runtime.returncode != 0:
    raise SystemExit(f"the staged runtime cannot report its identity: {runtime.stderr.strip()}")
reported = json.loads(runtime.stdout)
if not Path(reported["root"]).is_relative_to(venv):
    raise SystemExit("the staged runtime imports the package from outside its own venv")
identity = reported["identity"]
domain = reported["domain"]
if (
    identity.get("pipeline_id") != reported["pipeline_id"]
    or identity.get("version") != release["version"]
    or identity.get("source_sha256") != release["source_sha256"]
    or identity.get("contract") != domain["contract"]
    or identity.get("support") != domain["support"]
):
    raise SystemExit("the staged runtime's installed package is not the staged release")
print(f"staged generation verified: {receipt['install_id']}")
GUARD
      # The staged release's platform objects travel to the live authority only here: after
      # the verified closed gate AND after the complete staged-identity verification above
      # (snapshot digest, wheel binding, installed package), in the installer's canonical
      # order (airflow-side boundary grants, then the platform schema/views/roles); they are
      # additive and idempotent and the staged read model is what the recovery below reads.
      # The step is recorded on the receipt; promote re-applies the same migrations as its
      # own verified step.
      if [ "$RECONCILE_SCHEMA_COMPLETED" != "1" ]; then
        ensure_postgres_ready
        if ! {
          PLATFORM_PSQL="$PG_BIN/psql" bash "$HERE/../platform/apply.sh" --airflow-grants \
            --database airflow_meta --socket "$POSTGRES_ROOT/socket" --port "$POSTGRES_PORT" \
            --user solidworks --airflow-schema "${AIRFLOW_SCHEMA:-airflow}" \
          && PLATFORM_PSQL="$PG_BIN/psql" bash "$HERE/../platform/apply.sh" \
            --database airflow_meta --socket "$POSTGRES_ROOT/socket" --port "$POSTGRES_PORT" \
            --user platform_migrate --airflow-schema "${AIRFLOW_SCHEMA:-airflow}"
        } > "$RECEIPT_DIR/reconcile-schema-migration.out" 2>&1; then
          promotion_step "$CANDIDATE_ID" reconcile schema-migration failed "$RECEIPT_DIR/reconcile-schema-migration.out"
          die "the staged generation's schema objects were not applied (the admission stays closed)"
        fi
        promotion_step "$CANDIDATE_ID" reconcile schema-migration completed "$RECEIPT_DIR/reconcile-schema-migration.out"
      fi
      load_generation_env
    fi ;;
  stage)
    load_running_env ;;
  promote|rollback)
    load_running_env
    load_generation_env ;;
  *)
    usage ;;
esac

case "$ACTION" in
  install)
    RENDER_LOG="$(mktemp)"
    TOOLCHAIN_LOG="$(mktemp)"
    # The managed proxy binary is decided before any render: whichever binary this
    # installation actually lands (or an already-executable system one) becomes the NGINX_BIN
    # written into resolved.env, the units and health - one path, one source.
    OPERATOR_STATE="$(generation_env_value "$ENV_FILE" OPERATOR_STATE)"
    PROXY_BIN="$(ensure_proxy_binary)" || die "the managed proxy binary is unavailable; nothing was installed"
    "$CONTROL_PYTHON" "$HERE/render_operator.py" --env-file "$ENV_FILE" --nginx-bin "$PROXY_BIN" > "$RENDER_LOG"
    cat "$RENDER_LOG"
    load_env_file "$(sed -n 's/^OPERATOR_STATE=//p' "$RENDER_LOG" | head -1)/resolved.env"
    load_generation_env
    TARGET="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
    bash "$HERE/scripts/install_toolchain.sh" > "$TOOLCHAIN_LOG"
    cat "$TOOLCHAIN_LOG"
    load_env_file "$TOOLCHAIN_LOG"
    "$CONTROL_PYTHON" "$HERE/render_operator.py" --env-file "$ENV_FILE" --nginx-bin "$PROXY_BIN" \
      --units-dir "$TARGET" >/dev/null
    airflow_render
    if [ -n "${POSTGRES_ROOT:-}" ]; then
      if [ ! -f "$POSTGRES_ROOT/data/PG_VERSION" ]; then
        POSTGRES_ROOT="$POSTGRES_ROOT" POSTGRES_MAJOR="${POSTGRES_MAJOR:-14}" POSTGRES_PORT="$POSTGRES_PORT" \
          bash "$AIRFLOW_HERE/scripts/install_postgres.sh"
      fi
      # One path for a fresh install and a stopped reinstall: the managed unit owns the server
      # `airflow db migrate` needs, and install waits for its socket before migration. No other
      # service is started here.
      ensure_postgres_ready
      db_state="$("$PG_BIN/psql" -h "$POSTGRES_ROOT/socket" -p "$POSTGRES_PORT" -U solidworks -d postgres \
        -tAc "select 1 from pg_database where datname='airflow_meta'")" \
        || die "could not query pg_database for airflow_meta"
      if [ "$db_state" != "1" ]; then
        "$PG_BIN/createdb" -h "$POSTGRES_ROOT/socket" -p "$POSTGRES_PORT" -U solidworks airflow_meta \
          || die "could not create the airflow_meta database"
      fi
      # One instance, separate schemas.  The cluster bootstrap identity (the only step that
      # runs with it) prepares the roles, the two schemas and the per-role search_path from
      # deploy/platform/bootstrap.sql; every migration and all platform objects then run as
      # the least-privilege identities (airflow_migrate / platform_migrate) that own only
      # their own schema - no public objects, no superuser migration, no cross-schema DDL.
      PLATFORM_PSQL="$PG_BIN/psql" bash "$HERE/../platform/apply.sh" --bootstrap \
        --database airflow_meta --socket "$POSTGRES_ROOT/socket" --port "$POSTGRES_PORT" \
        --user solidworks --airflow-schema "${AIRFLOW_SCHEMA:-airflow}" >&2 \
        || die "platform bootstrap failed"
    fi
    AIRFLOW_VENV="$AIRFLOW_VENV" AIRFLOW_HOME="$AIRFLOW_HOME" AIRFLOW_PYTHON="${AIRFLOW_PYTHON:-}" \
      PIPELINE_WHEEL="${PIPELINE_WHEEL:?PIPELINE_WHEEL is required for install}" \
      AIRFLOW_DB_URL="$AIRFLOW_DB_URL" AIRFLOW_RUNTIME_DB_URL="$AIRFLOW_RUNTIME_DB_URL" \
      AIRFLOW_API_PORT="$AIRFLOW_API_PORT" \
      bash "$AIRFLOW_HERE/install.sh"
    # The airflow-side boundary grants have one owner: apply.sh --airflow-grants (schema-variable
    # aware).  The platform view owner reads the real Airflow metadata under owner privileges;
    # the runtime identity stays DML-only inside the airflow schema.
    PLATFORM_PSQL="$PG_BIN/psql" bash "$HERE/../platform/apply.sh" --airflow-grants \
      --database airflow_meta --socket "$POSTGRES_ROOT/socket" --port "$POSTGRES_PORT" \
      --user solidworks --airflow-schema "${AIRFLOW_SCHEMA:-airflow}" >&2 \
      || die "could not grant the airflow-side boundaries"
    # The platform objects and the consumer grants run as the platform migration identity
    # (idempotent); the admission starts closed until "start" opens it.
    PLATFORM_PSQL="$PG_BIN/psql" bash "$HERE/../platform/apply.sh" \
      --database airflow_meta --socket "$POSTGRES_ROOT/socket" --port "$POSTGRES_PORT" \
      --user platform_migrate --airflow-schema "${AIRFLOW_SCHEMA:-airflow}" >&2 \
      || die "platform apply failed"
    # The runtime/DML grants and the default privileges travel with the same
    # apply.sh --airflow-grants step above (re-applied after every migration).
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
    # The binary decided above is the one everything rendered; it may not vanish mid-install.
    [ -x "$PROXY_BIN" ] || die "the managed proxy binary is missing: $PROXY_BIN"
    systemctl --user daemon-reload
    # The install record binds this generation to its snapshot, release identity and both ends;
    # it is written only after the installation legs are ready.
    STATE_ROOT="$("$CONTROL_PYTHON" -c 'import json,sys; print(json.load(open(sys.argv[1]))["resolved"]["storage"]["state_root"])' "$SNAPSHOT_PATH")"
    SERVICES_CSV="$(IFS=,; printf '%s' "${UNITS[*]}")"
    # The record's canonical bytes and its digest come from the same contract closure the
    # snapshot legs consumed: the installed wheel or the single wheel in the deployment tree.
    if [ -n "$WHEEL_FILE" ]; then
      "$CONTROL_PYTHON" "$HERE/install_record.py" build --state-root "$STATE_ROOT" --install-id "$INSTALL_ID" \
        --snapshot "$SNAPSHOT_PATH" --release-file "$RELEASE_FILE" \
        --endpoint-identity "$ENDPOINT_IDENTITY" --services "$SERVICES_CSV" \
        --dag "$AIRFLOW_HERE/dags/solidworks_to_urdf.py" \
        --deployment-root "$HERE/../.." --wheel "$WHEEL_FILE"
    else
      "$CONTROL_PYTHON" "$HERE/install_record.py" build --state-root "$STATE_ROOT" --install-id "$INSTALL_ID" \
        --snapshot "$SNAPSHOT_PATH" --release-file "$RELEASE_FILE" \
        --endpoint-identity "$ENDPOINT_IDENTITY" --services "$SERVICES_CSV" \
        --dag "$AIRFLOW_HERE/dags/solidworks_to_urdf.py" \
        --deployment-root "$HERE/../.." --tree "$HERE/../.."
    fi
    AIRFLOW_HOME="$AIRFLOW_HOME" "$AIRFLOW_VENV/bin/python" "$AIRFLOW_HERE/scripts/add_connection.py" \
      --state-root "$STATE_ROOT" --snapshot "$SNAPSHOT_PATH" \
      || die "could not bind solidworks_windows to the installed config snapshot"
    echo "install complete: ${OPERATOR_URL:-unknown}"
    ;;
  start)
    load_installed
    drift_check || die "configuration drift detected; run install before start"
    airflow_lifecycle start
    # --keep-admission-closed keeps BOTH scheduling and platform admission closed: the DAG
    # unpause and the platform open are the two state transitions of a plain start, and the
    # flagged start skips both (services up, admission closed; a later plain start re-verifies
    # and opens).  A failure above this point never opens anything.
    operator_units_enable \
      || die "ingress services failed to start; the platform admission stays closed"
    operator_units_active \
      || die "ingress services are not active; the platform admission stays closed"
    instance_lifecycle_resume "$ACTIVE_ID" "$STATE_ROOT/install-record.json" \
      || die "the Windows endpoint did not resume under the active install identity; admission stays closed"
    if [ -z "$KEEP_ADMISSION_CLOSED" ]; then
      airflow_unpause_dag
    fi
    if [ -n "$KEEP_ADMISSION_CLOSED" ]; then
      # Identical checks and services, but nothing is opened (never open-then-close): the
      # start-window close above is the only admission transition that was written.
      echo "start complete: services are running; the platform admission stays closed (--keep-admission-closed)"
    else
      # The gate opens only after every precondition, the identity check, the services, the
      # ingress readiness confirmation and the bounded Linux health gate (HTTP included)
      # succeeded; any failure above leaves it closed.
      OPERATOR_STATE="$OPERATOR_STATE" bash "$HERE/health_gate.sh" "$OPERATOR_STATE/health-gate/start-$(date -u +%Y%m%dT%H%M%SZ)" \
        || die "the Linux health gate did not pass; the platform admission stays closed"
      platform_open_admission
      echo "start complete: admission open"
    fi
    ;;
  drain)
    load_installed
    platform_close_admission
    AIRFLOW_HOME="$AIRFLOW_HOME" "$AIRFLOW_VENV/bin/airflow" dags pause solidworks_to_urdf \
      || die "could not pause the DAG scheduling (the platform admission is closed)"
    # Acknowledged: the platform admission is closed and in-flight work continues (the pause is
    # a scheduling measure).  "draining" and "drained" are distinct facts; the projection - one
    # read of the platform PostgreSQL authority - decides which one holds.
    projection="$(PLATFORM_OPERATOR_DSN="${PLATFORM_OPERATOR_DSN:-}" "$AIRFLOW_VENV/bin/python" \
      -m description_pipeline.orchestration.lifecycle_state report --require-no-unresolved)" || {
        echo "$projection"
        echo "drain: unresolved or unknown outcomes remain; reconcile must close them (the admission stays closed)" >&2
        exit 1
      }
    echo "$projection"
    case "$projection" in
      *'"settled": true'*) echo "drain: drained - no in-flight, unresolved or unknown work remains" ;;
      *) echo "drain: draining - admission closed; in-flight executions continue" ;;
    esac
    ;;
  stop)
    load_installed
    # stop only when drained and settled: active, unresolved or unknown work blocks it, and the
    # platform admission must already be closed by drain.  One consistent read of the platform
    # authority decides both; the gates never fall back to files or the scheduler.
    projection="$(PLATFORM_OPERATOR_DSN="${PLATFORM_OPERATOR_DSN:-}" "$AIRFLOW_VENV/bin/python" \
      -m description_pipeline.orchestration.lifecycle_state report --require-settled \
      --require-admission-closed)" || {
        echo "$projection"
        die "stop refused: active, unresolved or unknown work remains, or the admission is open; run drain first"
      }
    echo "$projection"
    drift_check || true
    operator_units_disable || true
    ADMISSION_RC=0
    AIRFLOW_HOME="$AIRFLOW_HOME" "$AIRFLOW_VENV/bin/airflow" dags pause solidworks_to_urdf || ADMISSION_RC=$?
    if [ -n "$KEEP_DATABASE" ]; then
      airflow_lifecycle stop-executors
      state="$(systemctl --user is-active "${DATABASE_UNITS[0]}.service" 2>/dev/null || true)"
      [ "$state" = "active" ] \
        || die "the managed PostgreSQL unit is ${state:-unknown}; --keep-database refused"
      echo "stop complete: supported writers are stopped; PostgreSQL remains active"
    else
      airflow_lifecycle stop
    fi
    exit "$ADMISSION_RC"
    ;;
  status)
    load_installed
    STATUS_RC=0
    drift_check || STATUS_RC=1
    STATUS_SYSTEMCTL="${SYSTEMCTL_BIN:-systemctl}"
    for unit in "${UNITS[@]}"; do
      state="$("$STATUS_SYSTEMCTL" --user is-active "$unit" 2>/dev/null || true)"
      printf '%s %s\n' "$unit" "${state:-unknown}"
      [ "$state" = "active" ] || STATUS_RC=1
    done
    windows_status_check || STATUS_RC=1
    exit "$STATUS_RC"
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
  stage)
    resolve_release_file
    [ -n "$INSTALL_ID" ] || die "stage requires --install-id naming the candidate generation"
    CANDIDATE_ID="$INSTALL_ID"
    RECEIPT_DIR="$OPERATOR_STATE/promotions/$CANDIDATE_ID"
    [ "$CANDIDATE_ID" != "$ACTIVE_ID" ] \
      || die "stage cannot target the active generation: $CANDIDATE_ID"
    if [ -e "$RECEIPT_DIR/receipt.json" ] || [ -L "$RECEIPT_DIR/receipt.json" ]; then
      die "stage preserves the existing promotion receipt: $RECEIPT_DIR/receipt.json"
    fi
    # Pre-receipt artifacts are deliberately quarantined, never inferred as accepted state.
    # Their generation ID is burned: the same ID is refused and the operator must choose a new one.
    for historical in \
      "$STATE_ROOT/install-records/$CANDIDATE_ID.json" \
      "$STATE_ROOT/snapshots/$CANDIDATE_ID.json" \
      "$STATE_ROOT/derived/$CANDIDATE_ID" \
      "$RELEASE_ROOT/airflow-venv-$CANDIDATE_ID"; do
      if [ -e "$historical" ] || [ -L "$historical" ]; then
        die "stage refuses a reused historical generation: $CANDIDATE_ID"
      fi
    done
    # The Linux generation is built from the candidate release's own wheel - a cross-release
    # upgrade consumes the release archive, never the running generation's asset - into a new
    # snapshot, derived environment and runtime, with no pointer, unit or admission change.
    CANDIDATE_WHEEL_OUTPUT="$(mktemp)"
    if ! "$CONTROL_PYTHON" - "$RELEASE_FILE" > "$CANDIDATE_WHEEL_OUTPUT" <<'PY'
import hashlib, json, sys
from pathlib import Path

release_path = Path(sys.argv[1])
release = json.loads(release_path.read_text(encoding="utf-8"))
wheels = sorted(
    name for name in release["artifacts"] if name.startswith("mimicverse_description-") and name.endswith(".whl")
)
if len(wheels) != 1:
    raise SystemExit(f"the release manifest must name exactly one package wheel, got {wheels}")
wheel = release_path.parent / wheels[0]
if not wheel.is_file():
    raise SystemExit(f"the package wheel is missing next to the release manifest: {wheel}")
if hashlib.sha256(wheel.read_bytes()).hexdigest() != release["artifacts"][wheels[0]]:
    raise SystemExit("the package wheel does not match the release manifest")
print(wheel)
PY
    then
      rm -f "$CANDIDATE_WHEEL_OUTPUT"
      die "the candidate release wheel was refused"
    fi
    if ! read_output_path "$CANDIDATE_WHEEL_OUTPUT" CANDIDATE_WHEEL; then
      rm -f "$CANDIDATE_WHEEL_OUTPUT"
      die "the candidate release wheel selection is not one path"
    fi
    rm -f "$CANDIDATE_WHEEL_OUTPUT"
    SNAPSHOT_PATH="$("$CONTROL_PYTHON" "$HERE/platform_config.py" render --config "$CONFIG_FILE" \
      --install-id "$CANDIDATE_ID" --wheel "$CANDIDATE_WHEEL" | sed -n 's/^snapshot=//p')" || exit $?
    [ -f "$SNAPSHOT_PATH" ] || die "candidate snapshot was not written"
    CANDIDATE_ENV="$("$CONTROL_PYTHON" "$HERE/platform_env.py" emit --snapshot "$SNAPSHOT_PATH" \
      --install-id "$CANDIDATE_ID" --tree "$HERE/../.." --release-file "$RELEASE_FILE" \
      --wheel "$CANDIDATE_WHEEL" | sed -n 's/^env=//p')" || exit $?
    [ -f "$CANDIDATE_ENV" ] || die "candidate derived environment was not written"
    "$CONTROL_PYTHON" "$HERE/promotion.py" init --state-root "$OPERATOR_STATE" --install-id "$CANDIDATE_ID" \
      --from "$STATE_ROOT/install-records/$ACTIVE_ID.json" --to "$SNAPSHOT_PATH" \
      --release-file "$RELEASE_FILE" \
      --plan "linux-generation,windows-generation,close-admission,drain,backup,stop-services,schema-migration,windows-switch,endpoint-identity,switch-pointer,config-reference,start-services,verify,open-admission,rollback-close,rollback-drain,rollback-stop,rollback-windows,rollback-linux,rollback-config-reference,rollback-verify" \
      >/dev/null
    printf '%s\n' "$CANDIDATE_WHEEL" > "$RECEIPT_DIR/candidate-wheel"
    ENV_FILE="$CANDIDATE_ENV"
    load_generation_env
    if bash "$AIRFLOW_HERE/install.sh" --stage > "$RECEIPT_DIR/linux-generation.out" 2>&1; then
      promotion_step "$CANDIDATE_ID" promote linux-generation completed \
        "$SNAPSHOT_PATH" "$CANDIDATE_ENV" "$RECEIPT_DIR/linux-generation.out"
    else
      promotion_step "$CANDIDATE_ID" promote linux-generation failed \
        "$RECEIPT_DIR/linux-generation.out"
      die "the candidate runtime failed to stage; the active generation is unchanged"
    fi
    WINDOWS_STAGE_STATUS=0
    instance_lifecycle stage "$CANDIDATE_ID" \
      > "$RECEIPT_DIR/windows-stage.path" 2> "$RECEIPT_DIR/windows-stage.path.stderr" \
      || WINDOWS_STAGE_STATUS=$?
    if [ "$WINDOWS_STAGE_STATUS" = "2" ]; then
      cat "$RECEIPT_DIR/windows-stage.path.stderr" >&2 2>/dev/null || true
      promotion_step "$CANDIDATE_ID" promote windows-generation unknown \
        "$RECEIPT_DIR/windows-stage.out" "$RECEIPT_DIR/windows-stage.path"
      die "the Windows candidate stage outcome is unacknowledged; the active generation is unchanged"
    fi
    if [ "$WINDOWS_STAGE_STATUS" -ne 0 ]; then
      cat "$RECEIPT_DIR/windows-stage.path.stderr" >&2 2>/dev/null || true
      promotion_step "$CANDIDATE_ID" promote windows-generation failed \
        "$RECEIPT_DIR/windows-stage.out" "$RECEIPT_DIR/windows-stage.path"
      die "the Windows candidate stage failed; the active generation is unchanged"
    fi
    if ! read_output_path "$RECEIPT_DIR/windows-stage.path" WINDOWS_STAGE; then
      promotion_step "$CANDIDATE_ID" promote windows-generation unknown \
        "$RECEIPT_DIR/windows-stage.out" "$RECEIPT_DIR/windows-stage.path"
      die "the Windows candidate stage returned no readable evidence path"
    fi
    promotion_step "$CANDIDATE_ID" promote windows-generation completed \
      "$WINDOWS_STAGE" "$RECEIPT_DIR/windows-stage.path"
    # stage performs no schema DDL: the live database is only written by guarded windows -
    # the closed-admission reconcile route (which applies the staged objects before it
    # recovers through them) and promote's own schema-migration step - so a stage while the
    # system serves never touches the live objects.
    echo "stage complete: $CANDIDATE_ID (active pointer and admission unchanged)"
    ;;
  promote)
    [ -n "$INSTALL_ID" ] || die "promote requires --install-id naming the staged generation"
    CANDIDATE_ID="$INSTALL_ID"
    RECEIPT_DIR="$OPERATOR_STATE/promotions/$CANDIDATE_ID"
    [ -f "$RECEIPT_DIR/receipt.json" ] || die "no stage receipt for $CANDIDATE_ID; run stage first"
    resolve_release_file
    "$CONTROL_PYTHON" "$HERE/promotion.py" verify-release --state-root "$OPERATOR_STATE" \
      --install-id "$CANDIDATE_ID" --release-file "$RELEASE_FILE" >/dev/null \
      || die "the promotion receipt binds another release manifest"
    REPLAY_STATUS=0
    REPLAY_STATE="$(promotion_replay_state "$CANDIDATE_ID" promote)" || REPLAY_STATUS=$?
    case "$REPLAY_STATUS" in
      0)
        [ "$REPLAY_STATE" = "replay=completed" ] \
          || die "promotion replay returned an invalid completed state: $REPLAY_STATE"
        verify_completed_replay \
          || die "completed promotion replay verification failed; no side effect was repeated"
        echo "promote replay complete: $CANDIDATE_ID (existing health verified)"
        exit 0
        ;;
      10)
        [ "$REPLAY_STATE" = "replay=fresh" ] \
          || die "promotion replay returned an invalid fresh state: $REPLAY_STATE"
        ;;
      11)
        die "the promotion receipt is partial or inconsistent; use reconcile or rollback instead of replaying promote"
        ;;
      *)
        die "the promotion receipt could not be classified for replay"
        ;;
    esac
    EVIDENCE_DIR="$RECEIPT_DIR/evidence"
    SNAPSHOT_PATH="$("$CONTROL_PYTHON" -c 'import json,sys; print(json.load(open(sys.argv[1]))["to"])' "$RECEIPT_DIR/receipt.json")"
    CANDIDATE_ENV="$OPERATOR_STATE/derived/$CANDIDATE_ID/operator.env"
    if [ ! -f "$SNAPSHOT_PATH" ] || [ ! -f "$CANDIDATE_ENV" ]; then
      die "the staged generation is incomplete; re-run stage"
    fi
    mkdir -p "$EVIDENCE_DIR"
    ensure_postgres_ready
    # 1. capture the scheduling state rollback must restore, then close the admission through
    #    the controlled entry before anything switches
    DAG_STATE_FILE="$EVIDENCE_DIR/dag-before-promotion.json"
    if ! airflow_capture_dag_state "$DAG_STATE_FILE" > "$EVIDENCE_DIR/dag-before-promotion.out" 2>&1; then
      promotion_step "$CANDIDATE_ID" promote close-admission failed "$EVIDENCE_DIR/dag-before-promotion.out"
      die "the pre-maintenance DAG state could not be captured; nothing was switched"
    fi
    if ! platform_close_admission "promote" "operatorctl promote" > "$EVIDENCE_DIR/close-admission.out" 2>&1; then
      promotion_step "$CANDIDATE_ID" promote close-admission failed "$EVIDENCE_DIR/close-admission.out"
      die "could not close the admission; nothing was switched"
    fi
    promotion_step "$CANDIDATE_ID" promote close-admission completed \
      "$EVIDENCE_DIR/dag-before-promotion.out" "$DAG_STATE_FILE" "$EVIDENCE_DIR/close-admission.out"
    # 2. drain: active or unresolved work refuses the switch (the admission stays closed)
    if ! PLATFORM_OPERATOR_DSN="${PLATFORM_OPERATOR_DSN:-}" "$AIRFLOW_VENV/bin/python" \
        -m description_pipeline.orchestration.lifecycle_state report --require-settled \
        > "$EVIDENCE_DIR/drain.out" 2>&1; then
      promotion_step "$CANDIDATE_ID" promote drain failed "$EVIDENCE_DIR/drain.out"
      die "promote refused: active or unresolved work remains (the admission stays closed)"
    fi
    promotion_step "$CANDIDATE_ID" promote drain completed "$EVIDENCE_DIR/drain.out"
    # 3. a recoverable backup: one consistent PostgreSQL snapshot plus the referenced
    #    configuration closure (records, snapshots, derived files, resolved.env), the frozen
    #    handoff originals and the artifact roots - bound in one manifest with real digests.
    #    The private staging is removed first and last by its one auditable owner
    #    (backup.py clean-staging, which normalizes the copied read-only modes inside the
    #    staging only): a leftover partial staging would otherwise nest under the cp -a
    #    targets or abort the copy, and the copied 0555 directories would abort a plain rm.
    BACKUP_DIR="$RECEIPT_DIR/backup"
    if ! "$CONTROL_PYTHON" "$HERE/backup.py" clean-staging --receipt-dir "$RECEIPT_DIR" \
        > "$EVIDENCE_DIR/backup-staging-clean.out" 2>&1; then
      promotion_step "$CANDIDATE_ID" promote backup failed "$EVIDENCE_DIR/backup-staging-clean.out"
      die "the private backup staging could not be cleaned (the admission stays closed)"
    fi
    # The one private Connection capture is taken before the database snapshot and travels
    # inside the same sealed backup.  Its digest is therefore bound by the existing backup
    # manifest, not by a second public format; the evidence copy is the exact same bytes.
    CONNECTION_PRIVATE_SNAPSHOT="$EVIDENCE_DIR/private/connection-before-promotion.json"
    CONNECTION_BACKUP_MEMBER="private/connection-before-promotion.json"
    if ! AIRFLOW_HOME="$AIRFLOW_HOME" "$AIRFLOW_VENV/bin/python" "$HERE/connection_snapshot.py" capture \
        --file "$CONNECTION_PRIVATE_SNAPSHOT" --receipt "$RECEIPT_DIR/receipt.json" \
        > "$EVIDENCE_DIR/connection-capture.out" 2>&1; then
      promotion_step "$CANDIDATE_ID" promote backup failed "$EVIDENCE_DIR/connection-capture.out"
      die "the current Airflow connection row could not be captured (the admission stays closed)"
    fi
    mkdir -p "$BACKUP_DIR/state"
    mkdir -p "$BACKUP_DIR/private"
    cp -p "$CONNECTION_PRIVATE_SNAPSHOT" "$BACKUP_DIR/$CONNECTION_BACKUP_MEMBER"
    cp -p "$STATE_ROOT/install-record.json" "$BACKUP_DIR/state/install-record.json"
    cp -a "$STATE_ROOT/install-records" "$BACKUP_DIR/state/install-records"
    cp -a "$STATE_ROOT/snapshots" "$BACKUP_DIR/state/snapshots"
    cp -a "$STATE_ROOT/derived" "$BACKUP_DIR/state/derived"
    cp -p "$STATE_ROOT/resolved.env" "$BACKUP_DIR/state/resolved.env"
    # The closure references the RUNNING generation's snapshot (the candidate describes where
    # the system is going, not where it is).
    RUNNING_SNAPSHOT="$("$CONTROL_PYTHON" -c 'import json,sys; print(json.load(open(sys.argv[1]))["snapshot"]["path"])' "$STATE_ROOT/install-record.json")"
    "$CONTROL_PYTHON" - "$RUNNING_SNAPSHOT" "$BACKUP_DIR" <<'PY'
import json
import os
import shutil
import sys
from pathlib import Path

snapshot = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
backup = Path(sys.argv[2])
storage = snapshot["resolved"]["storage"]
resolved = snapshot["resolved"]
copied = {}
for name, target in (("handoff_root", "handoff"), ("artifact_root", "artifacts")):
    source = Path(storage[name])
    if source.is_dir():
        destination = backup / target
        shutil.copytree(source, destination, symlinks=False)
        copied[name] = str(destination)
# The specification library the snapshot references travels with its files too; the diagnostic
# and delivery evidence live under the artifact root captured above.
for index, root in enumerate(resolved.get("specifications", {}).get("record_roots") or []):
    source = Path(root)
    if source.is_dir():
        destination = backup / "specifications" / f"root-{index}"
        shutil.copytree(source, destination, symlinks=False)
        copied[f"record_root_{index}"] = str(destination)
captured_secrets = {}
secrets_dir = backup / "secrets"
for key, value in (
    ("certificate", resolved["operator"]["certificate_file"]),
    ("private_key", resolved["operator"]["private_key_file"]),
    ("database_dsn", resolved["database"]["dsn_file"]),
    ("feishu_secret", resolved["auth"]["feishu_secret_file"]),
    ("endpoint_token", resolved["windows"]["token_file"]),
):
    source = Path(value)
    if source.is_file():
        secrets_dir.mkdir(parents=True, exist_ok=True)
        destination = secrets_dir / key
        shutil.copyfile(source, destination)
        os.chmod(destination, 0o600)
        captured_secrets[key] = str(destination)
(backup / "closure.json").write_text(
    json.dumps(
        {
            "captured_roots": copied,
            "captured_secrets": sorted(captured_secrets),
            "not_captured": {
                "workspace_root": "scratch working copies are rebuilt on demand",
                "store_root": "model store roots (derived.repositories) are reconstructible from the release archives and the repository origins",
                "release_root": "installed release runtimes are reconstructible from the fixed release archives bound in the manifest",
            },
        },
        indent=2,
        sort_keys=True,
    )
    + "\n",
    encoding="utf-8",
)
PY
    if ! "$PG_BIN/pg_dump" -h "$POSTGRES_ROOT/socket" -p "$POSTGRES_PORT" -U solidworks -d airflow_meta \
        -Fc -f "$BACKUP_DIR/platform.pgc" > "$EVIDENCE_DIR/pg-dump.out" 2>&1; then
      promotion_step "$CANDIDATE_ID" promote backup failed "$EVIDENCE_DIR/pg-dump.out"
      die "the PostgreSQL backup snapshot failed (the admission stays closed)"
    fi
    # The archive is sealed with the state-root key: controlled, and the only artifact the
    # manifest binds for restore.
    BACKUP_KEY="$STATE_ROOT/secrets/backup.key"
    if ! "$CONTROL_PYTHON" "$HERE/backup.py" key --path "$BACKUP_KEY" > "$EVIDENCE_DIR/backup-key.out" 2>&1 \
        || ! "$CONTROL_PYTHON" "$HERE/backup.py" seal --source "$BACKUP_DIR" --key "$BACKUP_KEY" \
            --out "$RECEIPT_DIR/backup.tar.enc" > "$EVIDENCE_DIR/backup-seal.out" 2>&1; then
      promotion_step "$CANDIDATE_ID" promote backup failed "$EVIDENCE_DIR/backup-seal.out"
      die "the backup archive could not be sealed (the admission stays closed)"
    fi
    # The manifest format has one owner: backup.py writes and strictly reads it.
    if ! "$CONTROL_PYTHON" "$HERE/backup.py" manifest --source "$BACKUP_DIR" --sealed "$RECEIPT_DIR/backup.tar.enc" \
        --key "$BACKUP_KEY" --out "$EVIDENCE_DIR/backup-manifest.json" > "$EVIDENCE_DIR/backup-manifest.out" 2>&1; then
      promotion_step "$CANDIDATE_ID" promote backup failed "$EVIDENCE_DIR/backup-manifest.out"
      die "the backup manifest could not be written (the admission stays closed)"
    fi
    # The sealed archive replaces the plaintext staging: the encrypted artifact plus the
    # manifest are the only restore inputs that remain.  The same single owner removes the
    # staging, so a cleanup failure is recorded under this step - before any service stop or
    # pointer switch - and the step completes only after the removal succeeded.
    if ! "$CONTROL_PYTHON" "$HERE/backup.py" clean-staging --receipt-dir "$RECEIPT_DIR" \
        > "$EVIDENCE_DIR/backup-staging-removed.out" 2>&1; then
      promotion_step "$CANDIDATE_ID" promote backup failed "$EVIDENCE_DIR/backup-staging-removed.out"
      die "the sealed backup staging could not be removed (the admission stays closed)"
    fi
    promotion_step "$CANDIDATE_ID" promote backup completed \
      "$EVIDENCE_DIR/backup-manifest.json" "$EVIDENCE_DIR/backup-staging-removed.out" \
      "$EVIDENCE_DIR/connection-capture.out"
    # 4. both ends' execution services stop before the pointers move, while the shared
    #    PostgreSQL (Airflow metadata + platform schema) stays up: the guarded migrations
    #    below run against that same live database authority.  services.sh stop-executors
    #    refuses a swallowed stop and proves its units stopped; portal and proxy are proven
    #    here; the database readiness is proven last - every failure is recorded under this
    #    step and stops the run before any migration or pointer movement.
    if ! {
      airflow_lifecycle stop-executors \
      && operator_units_disable
    } > "$EVIDENCE_DIR/stop-services.out" 2>&1; then
      promotion_step "$CANDIDATE_ID" promote stop-services failed "$EVIDENCE_DIR/stop-services.out"
      die "the execution services did not stop (the admission stays closed)"
    fi
    for unit in "${OPERATOR_UNIT_FILES[@]}"; do
      state="$(systemctl --user is-active "$unit" 2>/dev/null || true)"
      case "$state" in
        inactive|failed)
          echo "operatorctl: $unit stopped (active_state=$state)" >> "$EVIDENCE_DIR/stop-services.out" ;;
        *)
          echo "operatorctl: $unit is still ${state:-unknown} after the stop" >> "$EVIDENCE_DIR/stop-services.out"
          promotion_step "$CANDIDATE_ID" promote stop-services failed "$EVIDENCE_DIR/stop-services.out"
          die "the ingress services did not stop (the admission stays closed)" ;;
      esac
    done
    if ! ( ensure_postgres_ready ) >> "$EVIDENCE_DIR/stop-services.out" 2>&1; then
      promotion_step "$CANDIDATE_ID" promote stop-services failed "$EVIDENCE_DIR/stop-services.out"
      die "the shared database is not ready for the guarded migrations (the admission stays closed)"
    fi
    echo "operatorctl: shared database ready for the guarded migrations" >> "$EVIDENCE_DIR/stop-services.out"
    promotion_step "$CANDIDATE_ID" promote stop-services completed "$EVIDENCE_DIR/stop-services.out"
    # 5. the candidate release's verified schema migrations run in the closed window, in the
    #    canonical order the installer uses: the Airflow metadata migration (the candidate
    #    runtime's own `airflow db migrate`), then the airflow-side boundary grants, then the
    #    platform objects and consumer grants - all idempotent and re-verified per release.
    CANDIDATE_RUNTIME_VENV="$(generation_env_value "$CANDIDATE_ENV" AIRFLOW_VENV)"
    [ -x "$CANDIDATE_RUNTIME_VENV/bin/airflow" ] \
      || die "the staged runtime provides no Airflow entry; re-run stage"
    if ! {
      AIRFLOW_HOME="$AIRFLOW_HOME" "$CANDIDATE_RUNTIME_VENV/bin/airflow" db migrate \
      && PLATFORM_PSQL="$PG_BIN/psql" bash "$HERE/../platform/apply.sh" --airflow-grants \
        --database airflow_meta --socket "$POSTGRES_ROOT/socket" --port "$POSTGRES_PORT" \
        --user solidworks --airflow-schema "${AIRFLOW_SCHEMA:-airflow}" \
      && PLATFORM_PSQL="$PG_BIN/psql" bash "$HERE/../platform/apply.sh" \
        --database airflow_meta --socket "$POSTGRES_ROOT/socket" --port "$POSTGRES_PORT" \
        --user platform_migrate --airflow-schema "${AIRFLOW_SCHEMA:-airflow}"
    } > "$EVIDENCE_DIR/schema-migration.out" 2>&1; then
      promotion_step "$CANDIDATE_ID" promote schema-migration failed "$EVIDENCE_DIR/schema-migration.out"
      die "the verified schema migrations failed (the admission stays closed)"
    fi
    promotion_step "$CANDIDATE_ID" promote schema-migration completed "$EVIDENCE_DIR/schema-migration.out"
    # 6. the Windows end switches through the single lifecycle script (stop, task+config, IF-09 health)
    resolve_release_file
    WINDOWS_PROMOTE=""
    WINDOWS_PROMOTE_STATUS=0
    instance_lifecycle promote "$CANDIDATE_ID" \
      > "$RECEIPT_DIR/windows-promote.path" 2> "$RECEIPT_DIR/windows-promote.path.stderr" \
      || WINDOWS_PROMOTE_STATUS=$?
    if [ "$WINDOWS_PROMOTE_STATUS" = "0" ]; then
      read_output_path "$RECEIPT_DIR/windows-promote.path" WINDOWS_PROMOTE \
        || WINDOWS_PROMOTE_STATUS=2
    fi
    if [ "$WINDOWS_PROMOTE_STATUS" = "2" ]; then
      cat "$RECEIPT_DIR/windows-promote.path.stderr" >&2 2>/dev/null || true
      promotion_step "$CANDIDATE_ID" promote windows-switch unknown \
        "$RECEIPT_DIR/windows-promote.out" "$RECEIPT_DIR/windows-promote.path"
      die "the Windows switch outcome is unacknowledged (the admission stays closed); reconcile with: operatorctl rollback --config <platform.json> --install-id $CANDIDATE_ID"
    fi
    if [ "$WINDOWS_PROMOTE_STATUS" != "0" ]; then
      cat "$RECEIPT_DIR/windows-promote.path.stderr" >&2 2>/dev/null || true
      promotion_step "$CANDIDATE_ID" promote windows-switch failed \
        "$RECEIPT_DIR/windows-promote.out" "$RECEIPT_DIR/windows-promote.path"
      die "the Windows switch failed (the admission stays closed; the receipt records it)"
    fi
    promotion_step "$CANDIDATE_ID" promote windows-switch completed \
      "$WINDOWS_PROMOTE" "$RECEIPT_DIR/windows-promote.path"
    # 6. the endpoint identity is what the running endpoint reports through its own health
    #    relay; the canonical IF-09 validator decides it against the release manifest - never
    #    a handwritten declaration
    WINDOWS_HEALTH_STATUS=0
    instance_lifecycle_health "$CANDIDATE_ID" "$RELEASE_FILE" windows-health-endpoint-identity \
      > "$RECEIPT_DIR/windows-health-endpoint-identity.path" \
      2> "$RECEIPT_DIR/windows-health-endpoint-identity.path.stderr" \
      || WINDOWS_HEALTH_STATUS=$?
    if [ "$WINDOWS_HEALTH_STATUS" = "2" ]; then
      cat "$RECEIPT_DIR/windows-health-endpoint-identity.path.stderr" >&2 2>/dev/null || true
      promotion_step "$CANDIDATE_ID" promote endpoint-identity unknown \
        "$RECEIPT_DIR/windows-health-endpoint-identity.out" \
        "$RECEIPT_DIR/windows-health-endpoint-identity.path"
      die "the running endpoint health outcome is unacknowledged (the admission stays closed)"
    fi
    if [ "$WINDOWS_HEALTH_STATUS" -ne 0 ]; then
      cat "$RECEIPT_DIR/windows-health-endpoint-identity.path.stderr" >&2 2>/dev/null || true
      promotion_step "$CANDIDATE_ID" promote endpoint-identity failed \
        "$RECEIPT_DIR/windows-health-endpoint-identity.out" \
        "$RECEIPT_DIR/windows-health-endpoint-identity.path"
      die "the running endpoint health did not pass (the admission stays closed)"
    fi
    if ! read_output_path "$RECEIPT_DIR/windows-health-endpoint-identity.path" WINDOWS_HEALTH; then
      promotion_step "$CANDIDATE_ID" promote endpoint-identity unknown \
        "$RECEIPT_DIR/windows-health-endpoint-identity.out" \
        "$RECEIPT_DIR/windows-health-endpoint-identity.path"
      die "the running endpoint health returned no readable evidence path"
    fi
    if ! promotion_extract_health "$WINDOWS_HEALTH" "$EVIDENCE_DIR/health.json" \
        > "$EVIDENCE_DIR/health-extract.out" 2>&1; then
      promotion_step "$CANDIDATE_ID" promote endpoint-identity failed \
        "$EVIDENCE_DIR/health-extract.out" "$WINDOWS_HEALTH"
      die "the health relay did not carry a health object (the admission stays closed)"
    fi
    if ! "$AIRFLOW_VENV/bin/python" "$HERE/install_record.py" endpoint-identity \
        --health "$EVIDENCE_DIR/health.json" --release-file "$RELEASE_FILE" \
        --out "$EVIDENCE_DIR/endpoint-identity.json" > "$EVIDENCE_DIR/endpoint-identity.out" 2>&1; then
      promotion_step "$CANDIDATE_ID" promote endpoint-identity failed \
        "$EVIDENCE_DIR/endpoint-identity.out" "$WINDOWS_HEALTH"
      die "the endpoint identity was refused (the admission stays closed)"
    fi
    promotion_step "$CANDIDATE_ID" promote endpoint-identity completed \
      "$WINDOWS_HEALTH" "$RECEIPT_DIR/windows-health-endpoint-identity.path" \
      "$EVIDENCE_DIR/health.json" "$EVIDENCE_DIR/endpoint-identity.json"
    # 7. the Linux pointer switches exactly once: the rollback binding is the immutable
    #    generation file (with its digest) - never the mutable active pointer - and the journal
    #    carries one reference to this receipt, the single step authority.
    PREVIOUS_RECORD="$STATE_ROOT/install-records/$ACTIVE_ID.json"
    [ -f "$PREVIOUS_RECORD" ] || die "the active generation file is missing: $PREVIOUS_RECORD"
    "$CONTROL_PYTHON" - "$EVIDENCE_DIR/journal.json" "$RECEIPT_DIR/receipt.json" "$EVIDENCE_DIR/health.json" \
      "$EVIDENCE_DIR/endpoint-identity.json" <<'PY'
import json
import sys
from datetime import UTC, datetime

out, receipt, *evidence = sys.argv[1:]
row = {
    "verb": "promote",
    "step": "promotion-receipt",
    "status": "completed",
    "at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "evidence": [receipt, *evidence],
}
open(out, "w", encoding="utf-8").write(json.dumps([row]) + "\n")
PY
    SERVICES_CSV="$(IFS=,; printf '%s' "${UNITS[*]}")"
    CANDIDATE_WHEEL="$(cat "$RECEIPT_DIR/candidate-wheel")"
    [ -f "$CANDIDATE_WHEEL" ] || die "the staged candidate wheel is missing: $CANDIDATE_WHEEL"
    airflow_require_no_readers
    if ! "$CANDIDATE_RUNTIME_VENV/bin/python" "$HERE/install_record.py" build --state-root "$STATE_ROOT" \
        --install-id "$CANDIDATE_ID" --snapshot "$SNAPSHOT_PATH" --release-file "$RELEASE_FILE" \
        --endpoint-identity "$EVIDENCE_DIR/endpoint-identity.json" --services "$SERVICES_CSV" \
        --dag "$AIRFLOW_HERE/dags/solidworks_to_urdf.py" \
        --deployment-root "$HERE/../.." --wheel "$CANDIDATE_WHEEL" \
        --previous-record "$PREVIOUS_RECORD" --journal "$EVIDENCE_DIR/journal.json" \
        > "$EVIDENCE_DIR/switch-pointer.out" 2>&1; then
      promotion_step "$CANDIDATE_ID" promote switch-pointer failed "$EVIDENCE_DIR/switch-pointer.out"
      die "the pointer switch failed (the admission stays closed)"
    fi
    promotion_step "$CANDIDATE_ID" promote switch-pointer completed "$EVIDENCE_DIR/switch-pointer.out"
    AIRFLOW_HERE="$(airflow_here_for_record "$STATE_ROOT/install-record.json")" \
      || die "the switched record has no generated Airflow deployment"
    if ! {
      airflow_render_generation_config "$STATE_ROOT/install-record.json" \
        "$CANDIDATE_RUNTIME_VENV" "$CANDIDATE_ENV" &&
      AIRFLOW_HOME="$AIRFLOW_HOME" "$CANDIDATE_RUNTIME_VENV/bin/python" "$AIRFLOW_HERE/scripts/add_connection.py" \
        --state-root "$STATE_ROOT" --snapshot "$SNAPSHOT_PATH"
    } > "$EVIDENCE_DIR/config-reference.out" 2>&1; then
      promotion_step "$CANDIDATE_ID" promote config-reference failed "$EVIDENCE_DIR/config-reference.out"
      die "the Airflow DAG/config reference could not bind the switched generation (the admission stays closed)"
    fi
    promotion_step "$CANDIDATE_ID" promote config-reference completed "$EVIDENCE_DIR/config-reference.out"
    # 8. the switched generation starts through its own derived environment
    load_active_env
    load_installed
    PROXY_BIN="$(ensure_proxy_binary)" \
      || die "the managed proxy binary is unavailable (the admission stays closed)"
    if ! {
      "$CONTROL_PYTHON" "$HERE/render_operator.py" --env-file "$ENV_FILE" --nginx-bin "$PROXY_BIN" \
        --units-dir "$TARGET" \
      && load_env_file "$STATE_ROOT/resolved.env" \
      && load_generation_env \
      && airflow_render \
      && systemctl --user daemon-reload \
      && airflow_lifecycle start \
      && airflow_wait_dag_identity \
      && airflow_unpause_dag \
      && operator_units_enable \
      && operator_units_active
    } > "$EVIDENCE_DIR/start-services.out" 2>&1; then
      promotion_step "$CANDIDATE_ID" promote start-services failed "$EVIDENCE_DIR/start-services.out"
      die "the switched generation did not start (the admission stays closed)"
    fi
    promotion_step "$CANDIDATE_ID" promote start-services completed "$EVIDENCE_DIR/start-services.out"
    # 9. both ends verify: the record against the current config, the running endpoint against the record
    VERIFY_STATUS=0
    {
      "$AIRFLOW_VENV/bin/python" "$HERE/install_record.py" verify-active \
        --config "$CONFIG_FILE" --state-root "$STATE_ROOT" \
      && {
        WINDOWS_HEALTH_STATUS=0
        instance_lifecycle_health "$CANDIDATE_ID" "$RELEASE_FILE" windows-health-promote-verify \
          > "$EVIDENCE_DIR/windows-health-promote-verify.path" \
          2> "$EVIDENCE_DIR/windows-health-promote-verify.path.stderr" || WINDOWS_HEALTH_STATUS=$?
        if [ "$WINDOWS_HEALTH_STATUS" -eq 0 ]; then
          read_output_path "$EVIDENCE_DIR/windows-health-promote-verify.path" WINDOWS_HEALTH \
            || WINDOWS_HEALTH_STATUS=2
        fi
        [ "$WINDOWS_HEALTH_STATUS" -eq 0 ]
      } \
      && promotion_extract_health "$WINDOWS_HEALTH" "$EVIDENCE_DIR/final-health.json" \
      && "$AIRFLOW_VENV/bin/python" "$HERE/install_record.py" endpoint-identity \
        --health "$EVIDENCE_DIR/final-health.json" --release-file "$RELEASE_FILE" \
        --record "$STATE_ROOT/install-record.json" --out "$EVIDENCE_DIR/final-identity.json" \
      && OPERATOR_STATE="$OPERATOR_STATE" bash "$HERE/health_gate.sh" "$EVIDENCE_DIR/linux-health-$(date -u +%Y%m%dT%H%M%SZ)"
    } > "$EVIDENCE_DIR/verify.out" 2>&1 || VERIFY_STATUS=$?
    if [ "$VERIFY_STATUS" -ne 0 ]; then
      if [ "${WINDOWS_HEALTH_STATUS:-0}" = "2" ]; then
        promotion_step "$CANDIDATE_ID" promote verify unknown \
          "$EVIDENCE_DIR/verify.out" "$EVIDENCE_DIR/windows-health-promote-verify.path" \
          "$RECEIPT_DIR/windows-health-promote-verify.out"
        die "the switched ends verification outcome is unacknowledged (the admission stays closed)"
      fi
      promotion_step "$CANDIDATE_ID" promote verify failed \
        "$EVIDENCE_DIR/verify.out" "$EVIDENCE_DIR/windows-health-promote-verify.path" \
        "$RECEIPT_DIR/windows-health-promote-verify.out"
      die "the switched ends did not verify (the admission stays closed)"
    fi
    promotion_step "$CANDIDATE_ID" promote verify completed \
      "$EVIDENCE_DIR/verify.out" "$EVIDENCE_DIR/windows-health-promote-verify.path" \
      "$EVIDENCE_DIR/final-health.json" "$RECEIPT_DIR/windows-health-promote-verify.out"
    # 10. only now opens the admission
    if ! platform_open_admission > "$EVIDENCE_DIR/open-admission.out" 2>&1; then
      promotion_step "$CANDIDATE_ID" promote open-admission failed "$EVIDENCE_DIR/open-admission.out"
      die "the promotion is complete but the admission did not open; run start"
    fi
    promotion_step "$CANDIDATE_ID" promote open-admission completed "$EVIDENCE_DIR/open-admission.out"
    echo "promote complete: $CANDIDATE_ID (admission open)"
    ;;
  rollback)
    MAINT_ID="${INSTALL_ID:-$ACTIVE_ID}"
    [ -n "$MAINT_ID" ] || die "no active generation and no --install-id to reconcile"
    ROLLBACK_ID="$MAINT_ID"
    RECEIPT_DIR="$OPERATOR_STATE/promotions/$ROLLBACK_ID"
    [ -f "$RECEIPT_DIR/receipt.json" ] || die "no promotion receipt for maintenance $ROLLBACK_ID; nothing is guessed"
    require_rollback_archive "$RECEIPT_DIR"
    resolve_release_file
    "$CONTROL_PYTHON" "$HERE/promotion.py" verify-release --state-root "$OPERATOR_STATE" \
      --install-id "$ROLLBACK_ID" --release-file "$RELEASE_FILE" >/dev/null \
      || die "the rollback receipt binds another release manifest"
    REPLAY_STATUS=0
    REPLAY_STATE="$(promotion_replay_state "$ROLLBACK_ID" rollback)" || REPLAY_STATUS=$?
    case "$REPLAY_STATUS" in
      0)
        [ "$REPLAY_STATE" = "replay=completed" ] \
          || die "rollback replay returned an invalid completed state: $REPLAY_STATE"
        verify_completed_replay \
          || die "completed rollback replay verification failed; no side effect was repeated"
        echo "rollback replay complete: $ROLLBACK_ID (existing health verified)"
        exit 0
        ;;
      10)
        [ "$REPLAY_STATE" = "replay=fresh" ] \
          || die "rollback replay returned an invalid fresh state: $REPLAY_STATE"
        ;;
      11)
        die "the rollback receipt is partial or inconsistent; do not repeat rollback"
        ;;
      *)
        die "the rollback receipt could not be classified for replay"
        ;;
    esac
    EVIDENCE_DIR="$RECEIPT_DIR/evidence"
    mkdir -p "$EVIDENCE_DIR"
    DAG_STATE_FILE="$EVIDENCE_DIR/dag-before-promotion.json"
    [ -f "$DAG_STATE_FILE" ] || die "the pre-maintenance DAG state is missing; rollback refuses to guess"
    CANDIDATE_ENV="$STATE_ROOT/derived/$ROLLBACK_ID/operator.env"
    [ -f "$CANDIDATE_ENV" ] || die "the maintenance generation has no derived environment"
    CANDIDATE_VENV="$(generation_env_value "$CANDIDATE_ENV" AIRFLOW_VENV)"
    LINUX_SWITCHED=1
    if [ "$ROLLBACK_ID" = "$ACTIVE_ID" ]; then
      PREVIOUS_RECORD="$("$CONTROL_PYTHON" -c 'import json,sys; print(json.load(open(sys.argv[1]))["previous"]["record"])' "$STATE_ROOT/install-record.json")" \
        || die "the active record carries no rollback binding"
    else
      # The Linux pointer never moved for this maintenance attempt; its source generation comes
      # from the receipt's bound immutable generation, never from the mutable active pointer.
      LINUX_SWITCHED=0
      PREVIOUS_RECORD="$("$CONTROL_PYTHON" -c 'import json,sys; print(json.load(open(sys.argv[1]))["from"])' "$RECEIPT_DIR/receipt.json")" \
        || die "the receipt carries no source generation"
    fi
    [ -f "$PREVIOUS_RECORD" ] || die "the recorded source generation is missing: $PREVIOUS_RECORD"
    PREVIOUS_RELEASE_TRIPLE="$("$CONTROL_PYTHON" -c 'import json,sys; r=json.load(open(sys.argv[1]))["release"]; print(r["version"], r["source_commit"], r["source_sha256"])' "$PREVIOUS_RECORD")" \
      || die "the rollback binding carries no release identity"
    PREVIOUS_GENERATION_DIR=""
    if [ -f "$RECEIPT_DIR/windows-promote.out" ]; then
      PREVIOUS_GENERATION_DIR="$("$CONTROL_PYTHON" -c '
import json, sys
try:
    print(json.load(open(sys.argv[1]))["previous"])
except Exception:
    pass
' "$RECEIPT_DIR/windows-promote.out")"
    fi
    resolve_release_file
    ensure_postgres_ready
    if [ -z "$PREVIOUS_GENERATION_DIR" ]; then
      # An unacknowledged switch is reconciled from the endpoint's own recorded status; the
      # unknown switch itself is never resent.
      WINDOWS_STATUS_STATUS=0
      instance_lifecycle_status "$ROLLBACK_ID" \
        > "$RECEIPT_DIR/windows-status.path" 2> "$RECEIPT_DIR/windows-status.path.stderr" \
        || WINDOWS_STATUS_STATUS=$?
      if [ "$WINDOWS_STATUS_STATUS" = "0" ]; then
        read_output_path "$RECEIPT_DIR/windows-status.path" WINDOWS_STATUS \
          || WINDOWS_STATUS_STATUS=2
      fi
      if [ "$WINDOWS_STATUS_STATUS" != "0" ]; then
        cat "$RECEIPT_DIR/windows-status.path.stderr" >&2 2>/dev/null || true
        promotion_step "$ROLLBACK_ID" rollback rollback-windows unknown \
          "$RECEIPT_DIR/windows-status.out" "$RECEIPT_DIR/windows-status.path"
        die "the endpoint status is unreadable; the Windows end cannot be reconciled (the admission stays closed)"
      fi
      RECONCILIATION_OUTPUT="$RECEIPT_DIR/windows-status.reconciliation.txt"
      if ! : > "$RECONCILIATION_OUTPUT" \
          || ! : > "$RECONCILIATION_OUTPUT.stderr"; then
        cat "$RECEIPT_DIR/windows-status.path.stderr" >&2 2>/dev/null || true
        promotion_step "$ROLLBACK_ID" rollback rollback-windows unknown \
          "$RECEIPT_DIR/windows-status.out" "$RECEIPT_DIR/windows-status.path" \
          "$RECONCILIATION_OUTPUT" "$RECEIPT_DIR/windows-binding.json"
        die "the endpoint reconciliation evidence could not be initialized"
      fi
      if ! "$CONTROL_PYTHON" - "$RECEIPT_DIR/windows-binding.json" "$WINDOWS_STATUS" \
          > "$RECONCILIATION_OUTPUT" 2> "$RECONCILIATION_OUTPUT.stderr" <<'PY'
import json
import re
import sys


def normalize(value):
    return str(value or "").replace("\\", "/").rstrip("/")


binding = json.load(open(sys.argv[1], encoding="utf-8"))
status = json.load(open(sys.argv[2], encoding="utf-8"))
target = normalize(binding.get("dir")) + "/generation"
live = status.get("live_task") if isinstance(status, dict) else None
if not isinstance(live, dict):
    print("unknown")
    raise SystemExit(0)
records = [row for row in (status.get("pending"), status.get("last_promotion")) if isinstance(row, dict)]
record = next((row for row in records if normalize(row.get("generation")) == target), None)
if not isinstance(record, dict) or live.get("state") != "Running":
    print("unknown")
    raise SystemExit(0)

relation = live.get("maintenance_relation")
previous = normalize(record.get("previous"))
source = record.get("source") if isinstance(record.get("source"), dict) else {}
target_identity = record.get("target") if isinstance(record.get("target"), dict) else {}


def source_matches() -> bool:
    return (
        relation == "source"
        and bool(previous)
        and type(live.get("action_count")) is int
        and live.get("action_count") == 1
        and live.get("task_semantic_sha256") == source.get("task_semantic_sha256")
        and live.get("task_acl_sha256") == source.get("task_acl_sha256")
        and normalize(live.get("endpoint_root")) == normalize(source.get("endpoint_root"))
        and normalize(live.get("config_path")) == normalize(source.get("config_path"))
        and live.get("config_digest") == source.get("config_digest")
        and live.get("install_id") == source.get("install_id")
        and live.get("release_identity_sha256") == source.get("release_identity_sha256")
    )


def target_matches() -> bool:
    return (
        relation == "target"
        and bool(previous)
        and type(live.get("action_count")) is int
        and live.get("action_count") == 1
        and normalize(live.get("endpoint_root")) == target
        and normalize(live.get("config_path"))
        == normalize(target_identity.get("config_path"))
        and live.get("config_digest") == target_identity.get("config_digest")
        and live.get("task_xml_sha256") == target_identity.get("task_xml_sha256")
        and isinstance(target_identity.get("config_sha256"), str)
        and bool(re.fullmatch(r"[0-9a-f]{64}", target_identity["config_sha256"]))
        and isinstance(live.get("task_xml_sha256"), str)
        and bool(re.fullmatch(r"[0-9a-f]{64}", live["task_xml_sha256"]))
    )


if target_matches():
    print(previous)
elif source_matches():
    print("before")
else:
    print("unknown")
PY
      then
        cat "$RECEIPT_DIR/windows-status.path.stderr" >&2 2>/dev/null || true
        promotion_step "$ROLLBACK_ID" rollback rollback-windows unknown \
          "$RECEIPT_DIR/windows-status.out" "$RECEIPT_DIR/windows-status.path" \
          "$RECONCILIATION_OUTPUT" "$RECEIPT_DIR/windows-binding.json"
        die "the endpoint status could not be reconciled"
      fi
      if ! read_output_path "$RECONCILIATION_OUTPUT" RECONCILIATION; then
        cat "$RECEIPT_DIR/windows-status.path.stderr" >&2 2>/dev/null || true
        promotion_step "$ROLLBACK_ID" rollback rollback-windows unknown \
          "$RECEIPT_DIR/windows-status.out" "$RECEIPT_DIR/windows-status.path" \
          "$RECONCILIATION_OUTPUT" "$RECEIPT_DIR/windows-binding.json"
        die "the endpoint reconciliation result is not one token"
      fi
      case "$RECONCILIATION" in
        before)
          PREVIOUS_GENERATION_DIR=""
          ;;
        /*|*:*)
          PREVIOUS_GENERATION_DIR="$RECONCILIATION"
          ;;
        *)
          cat "$RECEIPT_DIR/windows-status.path.stderr" >&2 2>/dev/null || true
          promotion_step "$ROLLBACK_ID" rollback rollback-windows unknown \
            "$RECEIPT_DIR/windows-status.out" "$RECEIPT_DIR/windows-status.path" \
            "$RECONCILIATION_OUTPUT" "$RECEIPT_DIR/windows-binding.json"
          die "the endpoint state is not provably recoverable (the admission stays closed); the unacknowledged switch is never resent"
          ;;
      esac
    fi
    # Rollback is a maintenance switch too: the current ends close, drain and stop their
    # executors.  PostgreSQL stays live because the connection restore and the restored
    # generation's verification both require it.
    if ! platform_close_admission "rollback" "operatorctl rollback" > "$EVIDENCE_DIR/rollback-close.out" 2>&1; then
      promotion_step "$ROLLBACK_ID" rollback rollback-close failed "$EVIDENCE_DIR/rollback-close.out"
      die "could not close the admission; nothing was rolled back"
    fi
    promotion_step "$ROLLBACK_ID" rollback rollback-close completed "$EVIDENCE_DIR/rollback-close.out"
    if ! (
      airflow_pause_dag \
      && PLATFORM_OPERATOR_DSN="${PLATFORM_OPERATOR_DSN:-}" "$AIRFLOW_VENV/bin/python" \
        -m description_pipeline.orchestration.lifecycle_state report --require-settled
    ) > "$EVIDENCE_DIR/rollback-drain.out" 2>&1; then
      promotion_step "$ROLLBACK_ID" rollback rollback-drain failed "$EVIDENCE_DIR/rollback-drain.out"
      die "rollback could not pause scheduling or drain settled work (the admission stays closed)"
    fi
    promotion_step "$ROLLBACK_ID" rollback rollback-drain completed "$EVIDENCE_DIR/rollback-drain.out"
    if ! {
      airflow_lifecycle stop-executors \
      && operator_units_disable
    } > "$EVIDENCE_DIR/rollback-stop.out" 2>&1; then
      promotion_step "$ROLLBACK_ID" rollback rollback-stop failed "$EVIDENCE_DIR/rollback-stop.out"
      die "the execution services did not stop (the admission stays closed)"
    fi
    promotion_step "$ROLLBACK_ID" rollback rollback-stop completed "$EVIDENCE_DIR/rollback-stop.out"
    # The Windows end restores exactly the generation this maintenance bound, then verifies its
    # own health against that generation's release identity.
    if [ -n "$PREVIOUS_GENERATION_DIR" ]; then
      WINDOWS_ROLLBACK=""
      WINDOWS_ROLLBACK_STATUS=0
      WINDOWS_RELEASE_OVERRIDE="$PREVIOUS_RELEASE_TRIPLE" \
        instance_lifecycle rollback "$ROLLBACK_ID" "$PREVIOUS_GENERATION_DIR" \
        > "$RECEIPT_DIR/windows-rollback.path" 2> "$RECEIPT_DIR/windows-rollback.path.stderr" \
        || WINDOWS_ROLLBACK_STATUS=$?
      if [ "$WINDOWS_ROLLBACK_STATUS" = "0" ]; then
        read_output_path "$RECEIPT_DIR/windows-rollback.path" WINDOWS_ROLLBACK \
          || WINDOWS_ROLLBACK_STATUS=2
      fi
      if [ "$WINDOWS_ROLLBACK_STATUS" = "2" ]; then
        cat "$RECEIPT_DIR/windows-rollback.path.stderr" >&2 2>/dev/null || true
        promotion_step "$ROLLBACK_ID" rollback rollback-windows unknown \
          "$RECEIPT_DIR/windows-rollback.out" "$RECEIPT_DIR/windows-rollback.path"
        die "the Windows rollback outcome is unacknowledged (the admission stays closed)"
      fi
      if [ "$WINDOWS_ROLLBACK_STATUS" != "0" ]; then
        cat "$RECEIPT_DIR/windows-rollback.path.stderr" >&2 2>/dev/null || true
        promotion_step "$ROLLBACK_ID" rollback rollback-windows failed \
          "$RECEIPT_DIR/windows-rollback.out" "$RECEIPT_DIR/windows-rollback.path"
        die "the Windows rollback failed; nothing further was moved"
      fi
      promotion_step "$ROLLBACK_ID" rollback rollback-windows completed \
        "$WINDOWS_ROLLBACK" "$RECEIPT_DIR/windows-rollback.path"
    else
      # The endpoint's own status shows this maintenance never completed there: nothing to
      # restore on Windows, and the reconciliation is recorded with that relay as evidence.
      promotion_step "$ROLLBACK_ID" rollback rollback-windows completed \
        "$RECEIPT_DIR/windows-status.out" "$RECEIPT_DIR/windows-status.path" \
        "$RECONCILIATION_OUTPUT" "$RECEIPT_DIR/windows-binding.json"
    fi
    # The Linux end re-activates the recorded previous generation; no generation file is rewritten.
    if [ "$LINUX_SWITCHED" = "1" ]; then
      airflow_require_no_readers
      if ! "$AIRFLOW_VENV/bin/python" "$HERE/install_record.py" repoint \
          --state-root "$STATE_ROOT" --record "$PREVIOUS_RECORD" > "$EVIDENCE_DIR/rollback-linux.out" 2>&1; then
        promotion_step "$ROLLBACK_ID" rollback rollback-linux failed "$EVIDENCE_DIR/rollback-linux.out"
        die "could not restore the previous Linux generation"
      fi
      promotion_step "$ROLLBACK_ID" rollback rollback-linux completed "$EVIDENCE_DIR/rollback-linux.out"
    else
      # The pointer never moved (receipt source == active generation); the reconciliation is
      # recorded with a stable copy, never the mutable receipt that this step will replace.
      ROLLBACK_LINUX_EVIDENCE="$EVIDENCE_DIR/rollback-linux-noop.json"
      cp -p "$RECEIPT_DIR/receipt.json" "$ROLLBACK_LINUX_EVIDENCE"
      promotion_step "$ROLLBACK_ID" rollback rollback-linux completed "$ROLLBACK_LINUX_EVIDENCE"
    fi
    # The previous generation's exact Connection row is restored from the private capture
    # already sealed in this promotion's backup.  The capture must still match the original
    # backup manifest and the receipt's immutable generation bindings; a missing capture is
    # fatal once config-reference began, and an unchanged row is never rewritten.
    CONNECTION_PRIVATE_SNAPSHOT="$EVIDENCE_DIR/private/connection-before-promotion.json"
    CONNECTION_BACKUP_MEMBER="private/connection-before-promotion.json"
    if [ -e "$CONNECTION_PRIVATE_SNAPSHOT" ] || [ -L "$CONNECTION_PRIVATE_SNAPSHOT" ]; then
      if ! AIRFLOW_HOME="$AIRFLOW_HOME" "$AIRFLOW_VENV/bin/python" "$HERE/connection_snapshot.py" restore \
          --file "$CONNECTION_PRIVATE_SNAPSHOT" --receipt "$RECEIPT_DIR/receipt.json" \
          --manifest "$EVIDENCE_DIR/backup-manifest.json" --sealed "$RECEIPT_DIR/backup.tar.enc" \
          --member "$CONNECTION_BACKUP_MEMBER" \
          > "$EVIDENCE_DIR/rollback-config-reference.out" 2>&1; then
        promotion_step "$ROLLBACK_ID" rollback rollback-config-reference failed "$EVIDENCE_DIR/rollback-config-reference.out"
        die "the previous Airflow connection row could not be restored exactly (the admission stays closed)"
      fi
    else
      CONFIG_REFERENCE_STATUS=0
      config_reference_recorded "$RECEIPT_DIR/receipt.json" || CONFIG_REFERENCE_STATUS=$?
      case "$CONFIG_REFERENCE_STATUS" in
        0)
          echo "the promotion recorded config-reference but its private connection capture is missing" \
            > "$EVIDENCE_DIR/rollback-config-reference.out"
          promotion_step "$ROLLBACK_ID" rollback rollback-config-reference failed \
            "$EVIDENCE_DIR/rollback-config-reference.out"
          die "the private connection capture is missing; the previous row cannot be proven (the admission stays closed)"
          ;;
        1)
          echo "config-reference never began; the Airflow connection was not changed by this promotion" \
            > "$EVIDENCE_DIR/rollback-config-reference.out"
          ;;
        *)
          echo "the promotion receipt is unreadable; the Airflow connection state cannot be proven" \
            > "$EVIDENCE_DIR/rollback-config-reference.out"
          promotion_step "$ROLLBACK_ID" rollback rollback-config-reference failed \
            "$EVIDENCE_DIR/rollback-config-reference.out"
          die "the promotion receipt is unreadable (the admission stays closed)"
          ;;
      esac
    fi
    promotion_step "$ROLLBACK_ID" rollback rollback-config-reference completed "$EVIDENCE_DIR/rollback-config-reference.out"
    # The restored generation starts through its own environment and both ends re-verify; the
    # restored record is checked against its own immutable snapshot (the operator's current
    # platform.json may already describe a candidate).
    load_running_env
    load_installed
    load_generation_env
    AIRFLOW_HERE="$(airflow_here_for_record "$STATE_ROOT/install-record.json")" \
      || die "the restored record has no generated Airflow deployment"
    airflow_render_generation_config "$STATE_ROOT/install-record.json" "$AIRFLOW_VENV" "$ENV_FILE" \
      || die "the restored Airflow DAG/config reference could not be rendered (the admission stays closed)"
    PROXY_BIN="$(ensure_proxy_binary)" \
      || die "the managed proxy binary is unavailable (the admission stays closed)"
    ROLLBACK_VERIFY_STATUS=0
    {
      "$CONTROL_PYTHON" "$HERE/render_operator.py" --env-file "$ENV_FILE" --nginx-bin "$PROXY_BIN" \
        --units-dir "$TARGET" \
      && load_env_file "$STATE_ROOT/resolved.env" \
      && load_generation_env \
      && airflow_render \
      && airflow_units_match_generation \
      && systemctl --user daemon-reload \
      && airflow_lifecycle start \
      && airflow_wait_dag_identity \
      && airflow_runtime_matches_generation "$CANDIDATE_VENV" \
      && operator_units_enable \
      && operator_units_active \
      && "$AIRFLOW_VENV/bin/python" "$HERE/install_record.py" verify-running \
        --state-root "$STATE_ROOT" \
      && {
        WINDOWS_HEALTH_STATUS=0
        instance_lifecycle_health "$ACTIVE_ID" "$STATE_ROOT/install-record.json" \
          "windows-health-rollback-verify-$ROLLBACK_ID" \
          "$EVIDENCE_DIR" \
          > "$EVIDENCE_DIR/windows-health-rollback-verify.path" \
          2> "$EVIDENCE_DIR/windows-health-rollback-verify.path.stderr" || WINDOWS_HEALTH_STATUS=$?
        if [ "$WINDOWS_HEALTH_STATUS" -eq 0 ]; then
          read_output_path "$EVIDENCE_DIR/windows-health-rollback-verify.path" WINDOWS_HEALTH \
            || WINDOWS_HEALTH_STATUS=2
        fi
        [ "$WINDOWS_HEALTH_STATUS" -eq 0 ]
      } \
      && promotion_extract_health "$WINDOWS_HEALTH" "$EVIDENCE_DIR/rollback-health.json" \
      && "$AIRFLOW_VENV/bin/python" "$HERE/install_record.py" endpoint-identity \
        --health "$EVIDENCE_DIR/rollback-health.json" --release-file "$RELEASE_FILE" \
        --record "$STATE_ROOT/install-record.json" --out "$EVIDENCE_DIR/rollback-identity.json" \
      && bash "$HERE/health_gate.sh" \
        "$OPERATOR_STATE/health-gate/rollback-$(date -u +%Y%m%dT%H%M%SZ)" \
      && airflow_restore_dag_state "$DAG_STATE_FILE"
    } > "$EVIDENCE_DIR/rollback-verify.out" 2>&1 || ROLLBACK_VERIFY_STATUS=$?
    if [ "$ROLLBACK_VERIFY_STATUS" -ne 0 ]; then
      if [ "${WINDOWS_HEALTH_STATUS:-0}" = "2" ]; then
        promotion_step "$ROLLBACK_ID" rollback rollback-verify unknown \
          "$EVIDENCE_DIR/rollback-verify.out" "$EVIDENCE_DIR/windows-health-rollback-verify.path" \
          "$EVIDENCE_DIR/rollback-health.json" \
          "$EVIDENCE_DIR/windows-health-rollback-verify-$ROLLBACK_ID.out"
        die "the restored ends verification outcome is unacknowledged (the admission stays closed)"
      fi
      promotion_step "$ROLLBACK_ID" rollback rollback-verify failed \
        "$EVIDENCE_DIR/rollback-verify.out" "$EVIDENCE_DIR/windows-health-rollback-verify.path" \
        "$EVIDENCE_DIR/rollback-health.json" \
        "$EVIDENCE_DIR/windows-health-rollback-verify-$ROLLBACK_ID.out"
      die "the restored ends did not verify (the admission stays closed)"
    fi
    promotion_step "$ROLLBACK_ID" rollback rollback-verify completed \
      "$EVIDENCE_DIR/rollback-verify.out" "$EVIDENCE_DIR/windows-health-rollback-verify.path" \
      "$EVIDENCE_DIR/rollback-health.json" \
      "$EVIDENCE_DIR/windows-health-rollback-verify-$ROLLBACK_ID.out"
    echo "rollback complete: restored $(basename "$PREVIOUS_RECORD" .json) (the admission stays closed; run start after review)"
    ;;
  reconcile)
    [ -n "$RUN_ID" ] && [ -n "$STAGE" ] && [ -n "$EFFECT_KEY" ] \
      || die "reconcile requires --run-id, --stage and --effect-key"
    case "$STAGE" in
      freeze|discover|capture|generate|verify|publish) ;;
      *) die "reconcile requires a canonical pipeline stage" ;;
    esac
    # The application reads the current effect kind and selects its one installed adapter.
    # Operator arguments cannot manufacture a result or select a second registry.
    [ -f "$ENV_FILE" ] || die "the derived environment is missing; run install"
    (
      set -a
      # shellcheck disable=SC1090
      . "$ENV_FILE"
      set +a
      RESOLVED_CONFIG="$("$CONTROL_PYTHON" "$HERE/platform_config.py" resolve --config "$CONFIG_FILE")"
      PLATFORM_ARTIFACT_ROOT="$(printf '%s' "$RESOLVED_CONFIG" | "$CONTROL_PYTHON" -c \
        'import json,sys; print(json.load(sys.stdin)["storage"]["artifact_root"])')"
      PLATFORM_WORKSPACE_ROOT="$(printf '%s' "$RESOLVED_CONFIG" | "$CONTROL_PYTHON" -c \
        'import json,sys; print(json.load(sys.stdin)["storage"]["workspace_root"])')"
      PIPELINE_WORK_ROOT="$PLATFORM_WORKSPACE_ROOT"
      export PLATFORM_ARTIFACT_ROOT PIPELINE_WORK_ROOT
      "$AIRFLOW_VENV/bin/python" "$HERE/reconcile-effect.py" \
        --run-id "$RUN_ID" --stage "$STAGE" --effect-key "$EFFECT_KEY"
    ) || die "reconcile refused"
    ;;
  *) usage ;;
esac
