#!/usr/bin/env bash
# The one supported lifecycle for the Linux operator deployment:
#   validate  static platform.json checks (schema/paths/targets/ports; no probes)
#   install   render the immutable config snapshot, then install the pinned runtime
#   start     enable/start PostgreSQL, Airflow, tunnel, portal and the HTTPS proxy
#   drain     close submissions and DAG admission, keep in-flight, return active/unresolved
#   stop      disable/stop them in reverse order (only when drained and settled)
#   status    systemctl --user is-active for every managed unit
#   health    deploy/operator/health.sh (static checks plus live service/HTTP checks)
#   reconcile confirm and register external outcomes; never repeats unknown side effects
#   stage     build the two-end candidate installation (isolated targets)
#   promote   switch both ends to the staged candidate (admission closed until verified)
#   rollback  restore both ends from the recorded previous installation
#
#   operatorctl.sh <verb> --config FILE
#
# Only description-* units are written; unrelated user units are untouched. Credentials and state
# live under the configured state root (outside Git).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AIRFLOW_HERE="$HERE/../airflow"
UNITS=(description-postgres description-solidworks-tunnel description-airflow-dag-processor
       description-airflow-scheduler description-airflow-api-server description-portal
       description-operator-proxy)

die() { echo "operatorctl: $*" >&2; exit 1; }
usage() {
  cat >&2 <<'USAGE'
usage: operatorctl.sh <validate|install|start|drain|stop|status|health|reconcile|stage|promote|rollback> --config FILE
install inputs: --install-id ID --wheel FILE --endpoint-identity FILE [--release-file FILE]
exit codes: 0 ok; 1 configuration/environment error; 2 usage
USAGE
  exit 2
}

ACTION="${1:-}"
shift || true
CONFIG_FILE=""
ENV_FILE=""
INSTALL_ID=""
WHEEL_FILE=""
ENDPOINT_IDENTITY=""
RELEASE_FILE=""
RUN_ID=""
STAGE=""
OUTCOME=""
EVIDENCE=()
while [ $# -gt 0 ]; do
  case "$1" in
    --config) CONFIG_FILE="${2:-}"; shift 2 ;;
    --install-id) INSTALL_ID="${2:-}"; shift 2 ;;
    --wheel) WHEEL_FILE="${2:-}"; shift 2 ;;
    --endpoint-identity) ENDPOINT_IDENTITY="${2:-}"; shift 2 ;;
    --release-file) RELEASE_FILE="${2:-}"; shift 2 ;;
    --run-id) RUN_ID="${2:-}"; shift 2 ;;
    --stage) STAGE="${2:-}"; shift 2 ;;
    --outcome) OUTCOME="${2:-}"; shift 2 ;;
    --evidence) EVIDENCE+=("${2:-}"); shift 2 ;;
    *) usage ;;
  esac
done
[ -n "$ACTION" ] || usage
case "$ACTION" in
  validate|install|start|drain|stop|status|health|reconcile|stage|promote|rollback) ;;
  *) usage ;;
esac

install_from_config() {
  [ -f "$ENDPOINT_IDENTITY" ] || die "--endpoint-identity FILE is required for install"
  if [ -z "$RELEASE_FILE" ]; then
    RELEASE_FILE="$HERE/../../release.json"
  fi
  [ -f "$RELEASE_FILE" ] || die "release manifest not found: $RELEASE_FILE"
  # Preflight the bound identities and the installed asset before any step runs: release
  # artifact digests, the endpoint generation, their common release triple, and the wheel
  # that will actually be consumed.
  if [ -n "$WHEEL_FILE" ]; then
    python3 "$HERE/install_record.py" check --release-file "$RELEASE_FILE" \
      --endpoint-identity "$ENDPOINT_IDENTITY" --tree "$HERE/../.." --wheel "$WHEEL_FILE" \
      >/dev/null || exit $?
  else
    python3 "$HERE/install_record.py" check --release-file "$RELEASE_FILE" \
      --endpoint-identity "$ENDPOINT_IDENTITY" --tree "$HERE/../.." >/dev/null || exit $?
  fi
  if [ -z "$INSTALL_ID" ]; then
    INSTALL_ID="$(python3 -c 'import uuid; print(uuid.uuid4().hex)')"
  fi
  if [ -n "$WHEEL_FILE" ]; then
    SNAPSHOT_PATH="$(python3 "$HERE/platform_config.py" render --config "$CONFIG_FILE" \
      --install-id "$INSTALL_ID" --wheel "$WHEEL_FILE" | sed -n 's/^snapshot=//p')" || exit $?
  else
    SNAPSHOT_PATH="$(python3 "$HERE/platform_config.py" render --config "$CONFIG_FILE" \
      --install-id "$INSTALL_ID" --tree "$HERE/../.." | sed -n 's/^snapshot=//p')" || exit $?
  fi
  [ -f "$SNAPSHOT_PATH" ] || die "config snapshot was not written"
  if [ -n "$WHEEL_FILE" ]; then
    DERIVED_ENV="$(python3 "$HERE/platform_env.py" emit --snapshot "$SNAPSHOT_PATH" \
      --install-id "$INSTALL_ID" --tree "$HERE/../.." --release-file "$RELEASE_FILE" \
      --wheel "$WHEEL_FILE" | sed -n 's/^env=//p')" || exit $?
  else
    DERIVED_ENV="$(python3 "$HERE/platform_env.py" emit --snapshot "$SNAPSHOT_PATH" \
      --install-id "$INSTALL_ID" --tree "$HERE/../.." --release-file "$RELEASE_FILE" | sed -n 's/^env=//p')" \
      || exit $?
  fi
  [ -f "$DERIVED_ENV" ] || die "derived environment was not written"
  ENV_FILE="$DERIVED_ENV"
}

load_active_env() {
  STATE_ROOT="$(python3 "$HERE/platform_config.py" resolve --config "$CONFIG_FILE" | \
    python3 -c 'import json,sys; print(json.load(sys.stdin)["storage"]["state_root"])')" || exit $?
  [ -f "$STATE_ROOT/resolved.env" ] || die "not installed: $STATE_ROOT/resolved.env is missing (run install first)"
  # shellcheck disable=SC1090
  source "$STATE_ROOT/resolved.env"
  # The operational entry verifies the complete install record, the snapshot digest and the
  # current-config binding before anything else; the check runs with the deployment interpreter
  # so it consumes the canonical schema implementation (single owner, no local mirror).
  ACTIVE_ID="$(AIRFLOW_HOME="$AIRFLOW_HOME" "$AIRFLOW_VENV/bin/python" "$HERE/install_record.py" \
    verify-active --config "$CONFIG_FILE" --state-root "$STATE_ROOT" | sed -n 's/^install_id=//p')" || exit $?
  [ -n "$ACTIVE_ID" ] || die "active install record did not declare a generation"
  ENV_FILE="$STATE_ROOT/derived/$ACTIVE_ID/operator.env"
  [ -f "$ENV_FILE" ] || die "derived environment missing for generation $ACTIVE_ID; run install"
}

# The maintenance window (stage/promote/rollback) verifies the RUNNING record and its immutable
# snapshot only: the candidate configuration is the subject of the switch, not drift of the
# running generation.  Current-config binding stays with verify-active on the steady-state verbs.
load_running_env() {
  STATE_ROOT="$(python3 "$HERE/platform_config.py" resolve --config "$CONFIG_FILE" | \
    python3 -c 'import json,sys; print(json.load(sys.stdin)["storage"]["state_root"])')" || exit $?
  [ -f "$STATE_ROOT/resolved.env" ] || die "not installed: $STATE_ROOT/resolved.env is missing (run install first)"
  # shellcheck disable=SC1090
  source "$STATE_ROOT/resolved.env"
  ACTIVE_ID="$(AIRFLOW_HOME="$AIRFLOW_HOME" "$AIRFLOW_VENV/bin/python" "$HERE/install_record.py" \
    verify-running --state-root "$STATE_ROOT" | sed -n 's/^install_id=//p')" || exit $?
  [ -n "$ACTIVE_ID" ] || die "active install record did not declare a generation"
  ENV_FILE="$STATE_ROOT/derived/$ACTIVE_ID/operator.env"
  [ -f "$ENV_FILE" ] || die "derived environment missing for generation $ACTIVE_ID; run install"
}

# The gates read the platform authority from the managed database, so the start window must
# begin by bringing that database up and confirming its socket.  The cluster identity never
# runs here; this is the same managed unit the installation created.
ensure_postgres_ready() {
  PG_BIN="$POSTGRES_ROOT/root/usr/lib/postgresql/${POSTGRES_MAJOR:-14}/bin"
  export LD_LIBRARY_PATH="$POSTGRES_ROOT/root/usr/lib/x86_64-linux-gnu:${LD_LIBRARY_PATH:-}"
  systemctl --user daemon-reload
  systemctl --user start description-postgres.service \
    || die "description-postgres.service failed to start"
  local ready=0
  for _ in $(seq 1 20); do
    if "$PG_BIN/pg_isready" -q -h "$POSTGRES_ROOT/socket" -p "$POSTGRES_PORT"; then ready=1; break; fi
    sleep 0.5
  done
  [ "$ready" = "1" ] || die "PostgreSQL did not become ready on $POSTGRES_ROOT/socket"
}

airflow_render() {
  AIRFLOW_VENV="$AIRFLOW_VENV" AIRFLOW_HOME="$AIRFLOW_HOME" \
  POSTGRES_ROOT="${POSTGRES_ROOT:-}" POSTGRES_MAJOR="${POSTGRES_MAJOR:-14}" \
  SOLIDWORKS_SSH_HOST="${SOLIDWORKS_SSH_HOST:-}" \
  AIRFLOW_API_PORT="$AIRFLOW_API_PORT" POSTGRES_PORT="$POSTGRES_PORT" \
  TUNNEL_LOOPBACK_PORT="$TUNNEL_LOOPBACK_PORT" SOLIDWORKS_ENDPOINT_PORT="$SOLIDWORKS_ENDPOINT_PORT" \
  FEISHU_ENV_FILE="${FEISHU_ENV_FILE:-$AIRFLOW_HOME/feishu.env}" \
    bash "$AIRFLOW_HERE/services.sh" render
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

airflow_unpause_dag() {
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

from description_pipeline.platform.store import PlatformStore


def connect():
    return psycopg2.connect(os.environ["PLATFORM_SERVICE_DSN"])


view = PlatformStore(connect).close_admission(
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

from description_pipeline.platform.store import PlatformStore


def connect():
    return psycopg2.connect(os.environ["PLATFORM_SERVICE_DSN"])


view = PlatformStore(connect).open_admission(reason="start", actor="operatorctl start")
print(f"admission={view.state} revision={view.revision}")
PY
}

# The Windows end of the unified stage/promote/rollback protocol.  One binding file rendered
# here from platform.json + release.json travels with the single lifecycle script over the
# authenticated SSH channel; the script's stdout is kept as the step evidence under the
# promotion receipt directory.
instance_lifecycle() {
  local verb="$1" generation="$2" previous="${3:-}"
  local dir="$OPERATOR_STATE/promotions/$generation"
  local binding="$dir/windows-binding.json" output="$dir/windows-$verb.out"
  mkdir -p "$dir"
  python3 - "$CONFIG_FILE" "$RELEASE_FILE" "$binding" "$generation" <<'PY' || return 1
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
  local remote archive_local
  remote="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["dir"])' "$binding")"
  archive_local="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["archive_local"])' "$binding")"
  # The generation directory exists first (private, under the instance state root); the archive
  # travels with the binding and the script, and the script consumes exactly the transferred file.
  ssh -o BatchMode=yes "$SOLIDWORKS_SSH_HOST" \
    "powershell.exe -NoProfile -Command \"New-Item -ItemType Directory -Force -Path '$remote' | Out-Null\"" \
    || { echo "instance_lifecycle: could not create $remote on $SOLIDWORKS_SSH_HOST" >&2; return 1; }
  scp -q -o BatchMode=yes "$binding" "$HERE/../windows/instance-lifecycle.ps1" "$archive_local" \
    "$SOLIDWORKS_SSH_HOST:$remote/" \
    || { echo "instance_lifecycle: could not transfer the lifecycle inputs to $SOLIDWORKS_SSH_HOST" >&2; return 1; }
  local stage_argument=""
  case "$verb" in
    stage|promote) stage_argument=" -StageDir \"$remote/generation\"" ;;
  esac
  local previous_argument=""
  [ -z "$previous" ] || previous_argument=" -Previous \"$previous\""
  ssh -o BatchMode=yes "$SOLIDWORKS_SSH_HOST" \
    "powershell.exe -NoProfile -ExecutionPolicy Bypass -File \"$remote/instance-lifecycle.ps1\" -Verb $verb -Binding \"$remote/windows-binding.json\"$stage_argument$previous_argument" \
    > "$output" || { echo "instance_lifecycle: $verb transport failed; see $output" >&2; return 2; }
  # Only the script's own strict result counts: an exit-0 shell whose JSON is not a passed
  # object of the requested verb is a failure, never a recorded success.
  local parse_rc=0
  python3 - "$output" "$verb" <<'PY' || parse_rc=$?
import json
import sys

try:
    document = json.loads(open(sys.argv[1], encoding="utf-8").read().strip().splitlines()[-1])
except (OSError, ValueError, IndexError) as error:
    raise SystemExit(3)
if not isinstance(document, dict) or document.get("passed") is not True or document.get("verb") != sys.argv[2]:
    raise SystemExit(1)
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
  local item
  for item in "$@"; do arguments+=(--evidence "$item"); done
  python3 "$HERE/promotion.py" step "${arguments[@]}" >/dev/null
}

promotion_extract_health() {
  python3 - "$1" "$2" <<'PY'
import json
import sys

document = json.load(open(sys.argv[1], encoding="utf-8"))
health = document.get("health")
if document.get("passed") is not True or document.get("verb") != "health" or not isinstance(health, dict):
    raise SystemExit("the health relay is not a passed health object")
open(sys.argv[2], "w", encoding="utf-8").write(json.dumps(health) + "\n")
PY
}

load_installed() {
  OPERATOR_STATE_DIR="$(sed -n 's/^OPERATOR_STATE=//p' "$ENV_FILE" | head -1)"
  [ -n "$OPERATOR_STATE_DIR" ] || die "OPERATOR_STATE is not set in $ENV_FILE"
  [ -f "$OPERATOR_STATE_DIR/resolved.env" ] \
    || die "not installed: $OPERATOR_STATE_DIR/resolved.env is missing (run install first)"
  # shellcheck disable=SC1090
  source "$OPERATOR_STATE_DIR/resolved.env"
  # Every port comes from the single deploy source (render_operator.py) via resolved.env.
  for key in AIRFLOW_API_PORT TUNNEL_LOOPBACK_PORT POSTGRES_PORT SOLIDWORKS_ENDPOINT_PORT; do
    [ -n "${!key:-}" ] || die "resolved.env is missing $key; run install to re-render the deployment"
  done
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

[ -n "$CONFIG_FILE" ] || usage
if [ -n "$INSTALL_ID$WHEEL_FILE$ENDPOINT_IDENTITY$RELEASE_FILE" ] && [ "$ACTION" != "install" ] \
   && [ "$ACTION" != "stage" ] && [ "$ACTION" != "promote" ] && [ "$ACTION" != "rollback" ]; then
  usage
fi
[ -f "$CONFIG_FILE" ] || die "config file not found: $CONFIG_FILE"
# Every verb passes the same static config gate first; the install record and its derived
# per-generation environment stay the single authority for the operational verbs.
python3 "$HERE/platform_config.py" validate --config "$CONFIG_FILE" >/dev/null || exit $?
case "$ACTION" in
  validate)
    echo "operatorctl: configuration valid: $CONFIG_FILE"
    exit 0 ;;
  install)
    install_from_config ;;
  start)
    # Cold-start order inside the single entry: bring the managed database up and confirm its
    # socket, then actually close the platform admission through the controlled entry (BEFORE
    # any identity/drift verification, so a failed start attempt cannot leave a leftover open
    # gate), then run the preconditions; the gate reopens only after everything is ready.
    START_STATE_ROOT="$(python3 "$HERE/platform_config.py" resolve --config "$CONFIG_FILE" | \
      python3 -c 'import json,sys; print(json.load(sys.stdin)["storage"]["state_root"])')" || exit $?
    [ -f "$START_STATE_ROOT/resolved.env" ] \
      || die "not installed: $START_STATE_ROOT/resolved.env is missing (run install first)"
    # shellcheck disable=SC1090
    source "$START_STATE_ROOT/resolved.env"
    ensure_postgres_ready
    platform_close_admission "start-window" "operatorctl start"
    load_active_env ;;
  drain|stop|status|health|reconcile)
    load_active_env ;;
  stage|promote|rollback)
    load_running_env ;;
  *)
    usage ;;
esac

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
    # The platform view owner reads the real Airflow metadata (owner-privilege views): the
    # platform migration identity receives exactly that read grant, nothing else.
    "$PG_BIN/psql" -h "$POSTGRES_ROOT/socket" -p "$POSTGRES_PORT" -U solidworks -d airflow_meta \
      -v ON_ERROR_STOP=1 -q \
      -c "GRANT USAGE ON SCHEMA ${AIRFLOW_SCHEMA:-airflow} TO platform_migrate; \
          GRANT SELECT ON ${AIRFLOW_SCHEMA:-airflow}.task_instance TO platform_migrate" \
      || die "could not grant the platform read boundary"
    # The platform objects and the consumer grants run as the platform migration identity
    # (idempotent); the admission starts closed until "start" opens it.
    PLATFORM_PSQL="$PG_BIN/psql" bash "$HERE/../platform/apply.sh" \
      --database airflow_meta --socket "$POSTGRES_ROOT/socket" --port "$POSTGRES_PORT" \
      --user platform_migrate --airflow-schema "${AIRFLOW_SCHEMA:-airflow}" >&2 \
      || die "platform apply failed"
    # The runtime identity is least privilege: DML inside the airflow schema only, no DDL
    # anywhere.  Grants are re-applied after every migration (new tables included).
    "$PG_BIN/psql" -h "$POSTGRES_ROOT/socket" -p "$POSTGRES_PORT" -U solidworks -d airflow_meta \
      -v ON_ERROR_STOP=1 -q <<'SQL' || die "grants for airflow_runtime failed"
ALTER ROLE airflow_runtime LOGIN;
GRANT USAGE ON SCHEMA airflow TO airflow_runtime;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA airflow TO airflow_runtime;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA airflow TO airflow_runtime;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA airflow TO airflow_runtime;
ALTER DEFAULT PRIVILEGES FOR ROLE airflow_migrate IN SCHEMA airflow
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO airflow_runtime;
ALTER DEFAULT PRIVILEGES FOR ROLE airflow_migrate IN SCHEMA airflow
    GRANT USAGE, SELECT ON SEQUENCES TO airflow_runtime;
SQL
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
    AIRFLOW_HOME="$AIRFLOW_HOME" "$AIRFLOW_VENV/bin/python" "$AIRFLOW_HERE/scripts/add_connection.py" \
      --token-file "$ENDPOINT_TOKEN_FILE" --host 127.0.0.1 --port "$TUNNEL_LOOPBACK_PORT" \
      --handoff-root "$SOLIDWORKS_HANDOFF_ROOT" --pipeline-config "$PORTAL_CONFIG"
    if [ ! -x "${NGINX_BIN:-/usr/sbin/nginx}" ]; then
      OPERATOR_STATE="$OPERATOR_STATE" bash "$HERE/scripts/install_proxy.sh" >/dev/null
    fi
    systemctl --user daemon-reload
    # The install record binds this generation to its snapshot, release identity and both ends;
    # it is written only after the installation legs are ready.
    STATE_ROOT="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["resolved"]["storage"]["state_root"])' "$SNAPSHOT_PATH")"
    SERVICES_CSV="$(IFS=,; printf '%s' "${UNITS[*]}")"
    # The record's canonical bytes and its digest come from the same contract closure the
    # snapshot legs consumed: the installed wheel or the single wheel in the deployment tree.
    if [ -n "$WHEEL_FILE" ]; then
      python3 "$HERE/install_record.py" build --state-root "$STATE_ROOT" --install-id "$INSTALL_ID" \
        --snapshot "$SNAPSHOT_PATH" --release-file "$RELEASE_FILE" \
        --endpoint-identity "$ENDPOINT_IDENTITY" --services "$SERVICES_CSV" \
        --dag "$AIRFLOW_HERE/dags/solidworks_to_urdf.py" --wheel "$WHEEL_FILE"
    else
      python3 "$HERE/install_record.py" build --state-root "$STATE_ROOT" --install-id "$INSTALL_ID" \
        --snapshot "$SNAPSHOT_PATH" --release-file "$RELEASE_FILE" \
        --endpoint-identity "$ENDPOINT_IDENTITY" --services "$SERVICES_CSV" \
        --dag "$AIRFLOW_HERE/dags/solidworks_to_urdf.py" --tree "$HERE/../.."
    fi
    echo "install complete: ${OPERATOR_URL:-unknown}"
    ;;
  start)
    load_installed
    drift_check || die "configuration drift detected; run install before start"
    airflow_lifecycle start
    airflow_unpause_dag
    systemctl --user enable --now description-portal.service description-operator-proxy.service \
      || die "ingress services failed to start; the platform admission stays closed"
    systemctl --user is-active --quiet description-portal.service description-operator-proxy.service \
      || die "ingress services are not active; the platform admission stays closed"
    # The gate opens only after every precondition, the identity check, the services and the
    # ingress readiness confirmation succeeded; any failure above leaves it closed.
    platform_open_admission
    echo "start complete: admission open"
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
  stage)
    [ -n "$RELEASE_FILE" ] || RELEASE_FILE="$HERE/../../release.json"
    [ -f "$RELEASE_FILE" ] || die "release manifest not found: $RELEASE_FILE"
    [ -n "$INSTALL_ID" ] || die "stage requires --install-id naming the candidate generation"
    CANDIDATE_ID="$INSTALL_ID"
    RECEIPT_DIR="$OPERATOR_STATE/promotions/$CANDIDATE_ID"
    # The Linux generation is built from the candidate release's own wheel - a cross-release
    # upgrade consumes the release archive, never the running generation's asset - into a new
    # snapshot, derived environment and runtime, with no pointer, unit or admission change.
    CANDIDATE_WHEEL="$(python3 - "$RELEASE_FILE" <<'PY'
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
)" || die "the candidate release wheel was refused"
    SNAPSHOT_PATH="$(python3 "$HERE/platform_config.py" render --config "$CONFIG_FILE" \
      --install-id "$CANDIDATE_ID" --wheel "$CANDIDATE_WHEEL" | sed -n 's/^snapshot=//p')" || exit $?
    [ -f "$SNAPSHOT_PATH" ] || die "candidate snapshot was not written"
    CANDIDATE_ENV="$(python3 "$HERE/platform_env.py" emit --snapshot "$SNAPSHOT_PATH" \
      --install-id "$CANDIDATE_ID" --tree "$HERE/../.." --release-file "$RELEASE_FILE" \
      --wheel "$CANDIDATE_WHEEL" | sed -n 's/^env=//p')" || exit $?
    [ -f "$CANDIDATE_ENV" ] || die "candidate derived environment was not written"
    python3 "$HERE/promotion.py" init --state-root "$OPERATOR_STATE" --install-id "$CANDIDATE_ID" \
      --from "$STATE_ROOT/install-records/$ACTIVE_ID.json" --to "$SNAPSHOT_PATH" \
      --plan "linux-generation,windows-generation,close-admission,drain,backup,stop-services,windows-switch,endpoint-identity,switch-pointer,start-services,verify,open-admission,rollback-close,rollback-drain,rollback-stop,rollback-windows,rollback-linux,rollback-verify" \
      >/dev/null
    printf '%s\n' "$CANDIDATE_WHEEL" > "$RECEIPT_DIR/candidate-wheel"
    set -a
    # shellcheck disable=SC1090
    source "$CANDIDATE_ENV"
    set +a
    if bash "$AIRFLOW_HERE/install.sh" --stage > "$RECEIPT_DIR/linux-generation.out" 2>&1; then
      python3 "$HERE/promotion.py" step --state-root "$OPERATOR_STATE" --install-id "$CANDIDATE_ID" \
        --verb promote --step linux-generation --status completed \
        --evidence "$SNAPSHOT_PATH" --evidence "$CANDIDATE_ENV" --evidence "$RECEIPT_DIR/linux-generation.out" \
        >/dev/null
    else
      python3 "$HERE/promotion.py" step --state-root "$OPERATOR_STATE" --install-id "$CANDIDATE_ID" \
        --verb promote --step linux-generation --status failed \
        --evidence "$RECEIPT_DIR/linux-generation.out" >/dev/null
      die "the candidate runtime failed to stage; the active generation is unchanged"
    fi
    if ! WINDOWS_STAGE="$(instance_lifecycle stage "$CANDIDATE_ID")"; then
      python3 "$HERE/promotion.py" step --state-root "$OPERATOR_STATE" --install-id "$CANDIDATE_ID" \
        --verb promote --step windows-generation --status failed \
        --evidence "$RECEIPT_DIR/windows-stage.out" >/dev/null
      die "the Windows candidate stage failed; the active generation is unchanged"
    fi
    python3 "$HERE/promotion.py" step --state-root "$OPERATOR_STATE" --install-id "$CANDIDATE_ID" \
      --verb promote --step windows-generation --status completed --evidence "$WINDOWS_STAGE" >/dev/null
    echo "stage complete: $CANDIDATE_ID (active pointer and admission unchanged)"
    ;;
  promote)
    [ -n "$INSTALL_ID" ] || die "promote requires --install-id naming the staged generation"
    CANDIDATE_ID="$INSTALL_ID"
    RECEIPT_DIR="$OPERATOR_STATE/promotions/$CANDIDATE_ID"
    [ -f "$RECEIPT_DIR/receipt.json" ] || die "no stage receipt for $CANDIDATE_ID; run stage first"
    EVIDENCE_DIR="$RECEIPT_DIR/evidence"
    SNAPSHOT_PATH="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["to"])' "$RECEIPT_DIR/receipt.json")"
    CANDIDATE_ENV="$OPERATOR_STATE/derived/$CANDIDATE_ID/operator.env"
    if [ ! -f "$SNAPSHOT_PATH" ] || [ ! -f "$CANDIDATE_ENV" ]; then
      die "the staged generation is incomplete; re-run stage"
    fi
    mkdir -p "$EVIDENCE_DIR"
    ensure_postgres_ready
    # 1. the admission closes through the controlled entry before anything switches
    if ! platform_close_admission "promote" "operatorctl promote" > "$EVIDENCE_DIR/close-admission.out" 2>&1; then
      promotion_step "$CANDIDATE_ID" promote close-admission failed "$EVIDENCE_DIR/close-admission.out"
      die "could not close the admission; nothing was switched"
    fi
    promotion_step "$CANDIDATE_ID" promote close-admission completed "$EVIDENCE_DIR/close-admission.out"
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
    #    handoff originals and the artifact roots - bound in one manifest with real digests
    BACKUP_DIR="$RECEIPT_DIR/backup"
    mkdir -p "$BACKUP_DIR/state"
    cp -p "$STATE_ROOT/install-record.json" "$BACKUP_DIR/state/install-record.json"
    cp -a "$STATE_ROOT/install-records" "$BACKUP_DIR/state/install-records"
    cp -a "$STATE_ROOT/snapshots" "$BACKUP_DIR/state/snapshots"
    cp -a "$STATE_ROOT/derived" "$BACKUP_DIR/state/derived"
    cp -p "$STATE_ROOT/resolved.env" "$BACKUP_DIR/state/resolved.env"
    # The closure references the RUNNING generation's snapshot (the candidate describes where
    # the system is going, not where it is).
    RUNNING_SNAPSHOT="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["snapshot"]["path"])' "$STATE_ROOT/install-record.json")"
    python3 - "$RUNNING_SNAPSHOT" "$BACKUP_DIR" <<'PY'
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
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(source, destination, symlinks=False)
        copied[name] = str(destination)
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
                "store_root": "model store roots are reconstructible from the release archives",
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
    if ! python3 "$HERE/backup.py" key --path "$BACKUP_KEY" > "$EVIDENCE_DIR/backup-key.out" 2>&1 \
        || ! python3 "$HERE/backup.py" seal --source "$BACKUP_DIR" --key "$BACKUP_KEY" \
            --out "$RECEIPT_DIR/backup.tar.enc" > "$EVIDENCE_DIR/backup-seal.out" 2>&1; then
      promotion_step "$CANDIDATE_ID" promote backup failed "$EVIDENCE_DIR/backup-seal.out"
      die "the backup archive could not be sealed (the admission stays closed)"
    fi
    python3 - "$BACKUP_DIR" "$EVIDENCE_DIR/backup-manifest.json" "$RECEIPT_DIR/backup.tar.enc" "$BACKUP_KEY" <<'PY'
import hashlib
import json
import sys
from pathlib import Path

backup, out, sealed, key = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]), Path(sys.argv[4])
files = {
    path.relative_to(backup).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
    for path in sorted(backup.rglob("*"))
    if path.is_file()
}
out.write_text(
    json.dumps(
        {
            "schema": "solidworks-to-urdf.promote-backup/v1",
            "files": files,
            "encrypted": {
                "path": str(sealed),
                "sha256": hashlib.sha256(sealed.read_bytes()).hexdigest(),
                "cipher": "aes-256-cbc",
                "key": str(key),
            },
        },
        indent=2,
        sort_keys=True,
    )
    + "\n",
    encoding="utf-8",
)
PY
    promotion_step "$CANDIDATE_ID" promote backup completed "$EVIDENCE_DIR/backup-manifest.json"
    # The sealed archive replaces the plaintext staging: the encrypted artifact plus the
    # manifest are the only restore inputs that remain.
    rm -rf "$BACKUP_DIR"
    # 4. both ends' execution services stop before the pointers move
    if ! {
      airflow_lifecycle stop \
      && systemctl --user disable --now description-operator-proxy.service description-portal.service
    } > "$EVIDENCE_DIR/stop-services.out" 2>&1; then
      promotion_step "$CANDIDATE_ID" promote stop-services failed "$EVIDENCE_DIR/stop-services.out"
      die "the execution services did not stop (the admission stays closed)"
    fi
    promotion_step "$CANDIDATE_ID" promote stop-services completed "$EVIDENCE_DIR/stop-services.out"
    # 5. the Windows end switches through the single lifecycle script (stop, task+config, IF-09 health)
    [ -n "$RELEASE_FILE" ] || RELEASE_FILE="$HERE/../../release.json"
    [ -f "$RELEASE_FILE" ] || die "release manifest not found: $RELEASE_FILE"
    WINDOWS_PROMOTE=""
    WINDOWS_PROMOTE_STATUS=0
    WINDOWS_PROMOTE="$(instance_lifecycle promote "$CANDIDATE_ID")" || WINDOWS_PROMOTE_STATUS=$?
    if [ "$WINDOWS_PROMOTE_STATUS" = "2" ]; then
      promotion_step "$CANDIDATE_ID" promote windows-switch unknown "$RECEIPT_DIR/windows-promote.out"
      die "the Windows switch outcome is unacknowledged (the admission stays closed); reconcile with: operatorctl rollback --config <platform.json> --install-id $CANDIDATE_ID"
    fi
    if [ "$WINDOWS_PROMOTE_STATUS" != "0" ]; then
      promotion_step "$CANDIDATE_ID" promote windows-switch failed "$RECEIPT_DIR/windows-promote.out"
      die "the Windows switch failed (the admission stays closed; the receipt records it)"
    fi
    promotion_step "$CANDIDATE_ID" promote windows-switch completed "$WINDOWS_PROMOTE"
    # 6. the endpoint identity is what the running endpoint reports through its own health
    #    relay; the canonical IF-09 validator decides it against the release manifest - never
    #    a handwritten declaration
    if ! WINDOWS_HEALTH="$(instance_lifecycle health "$CANDIDATE_ID")"; then
      promotion_step "$CANDIDATE_ID" promote endpoint-identity failed "$RECEIPT_DIR/windows-health.out"
      die "the running endpoint health did not pass (the admission stays closed)"
    fi
    if ! promotion_extract_health "$WINDOWS_HEALTH" "$EVIDENCE_DIR/health.json" \
        > "$EVIDENCE_DIR/health-extract.out" 2>&1; then
      promotion_step "$CANDIDATE_ID" promote endpoint-identity failed "$EVIDENCE_DIR/health-extract.out"
      die "the health relay did not carry a health object (the admission stays closed)"
    fi
    if ! "$AIRFLOW_VENV/bin/python" "$HERE/install_record.py" endpoint-identity \
        --health "$EVIDENCE_DIR/health.json" --release-file "$RELEASE_FILE" \
        --out "$EVIDENCE_DIR/endpoint-identity.json" > "$EVIDENCE_DIR/endpoint-identity.out" 2>&1; then
      promotion_step "$CANDIDATE_ID" promote endpoint-identity failed "$EVIDENCE_DIR/endpoint-identity.out"
      die "the endpoint identity was refused (the admission stays closed)"
    fi
    promotion_step "$CANDIDATE_ID" promote endpoint-identity completed \
      "$EVIDENCE_DIR/health.json" "$EVIDENCE_DIR/endpoint-identity.json"
    # 7. the Linux pointer switches exactly once: the rollback binding is the immutable
    #    generation file (with its digest) - never the mutable active pointer - and the journal
    #    carries one reference to this receipt, the single step authority.
    PREVIOUS_RECORD="$STATE_ROOT/install-records/$ACTIVE_ID.json"
    [ -f "$PREVIOUS_RECORD" ] || die "the active generation file is missing: $PREVIOUS_RECORD"
    python3 - "$EVIDENCE_DIR/journal.json" "$RECEIPT_DIR/receipt.json" "$EVIDENCE_DIR/health.json" \
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
    if ! "$AIRFLOW_VENV/bin/python" "$HERE/install_record.py" build --state-root "$STATE_ROOT" \
        --install-id "$CANDIDATE_ID" --snapshot "$SNAPSHOT_PATH" --release-file "$RELEASE_FILE" \
        --endpoint-identity "$EVIDENCE_DIR/endpoint-identity.json" --services "$SERVICES_CSV" \
        --dag "$AIRFLOW_HERE/dags/solidworks_to_urdf.py" --wheel "$CANDIDATE_WHEEL" \
        --previous-record "$PREVIOUS_RECORD" --journal "$EVIDENCE_DIR/journal.json" \
        > "$EVIDENCE_DIR/switch-pointer.out" 2>&1; then
      promotion_step "$CANDIDATE_ID" promote switch-pointer failed "$EVIDENCE_DIR/switch-pointer.out"
      die "the pointer switch failed (the admission stays closed)"
    fi
    promotion_step "$CANDIDATE_ID" promote switch-pointer completed "$EVIDENCE_DIR/switch-pointer.out"
    # 8. the switched generation starts through its own derived environment
    load_active_env
    load_installed
    if ! {
      python3 "$HERE/render_operator.py" --env-file "$ENV_FILE" --units-dir "$TARGET" \
      && airflow_render \
      && systemctl --user daemon-reload \
      && airflow_lifecycle start \
      && airflow_unpause_dag \
      && systemctl --user enable --now description-portal.service description-operator-proxy.service \
      && systemctl --user is-active --quiet description-portal.service description-operator-proxy.service
    } > "$EVIDENCE_DIR/start-services.out" 2>&1; then
      promotion_step "$CANDIDATE_ID" promote start-services failed "$EVIDENCE_DIR/start-services.out"
      die "the switched generation did not start (the admission stays closed)"
    fi
    promotion_step "$CANDIDATE_ID" promote start-services completed "$EVIDENCE_DIR/start-services.out"
    # 9. both ends verify: the record against the current config, the running endpoint against the record
    if ! {
      "$AIRFLOW_VENV/bin/python" "$HERE/install_record.py" verify-active \
        --config "$CONFIG_FILE" --state-root "$STATE_ROOT" \
      && WINDOWS_HEALTH="$(instance_lifecycle health "$CANDIDATE_ID")" \
      && promotion_extract_health "$WINDOWS_HEALTH" "$EVIDENCE_DIR/final-health.json" \
      && "$AIRFLOW_VENV/bin/python" "$HERE/install_record.py" endpoint-identity \
        --health "$EVIDENCE_DIR/final-health.json" --release-file "$RELEASE_FILE" \
        --record "$STATE_ROOT/install-record.json" --out "$EVIDENCE_DIR/final-identity.json"
    } > "$EVIDENCE_DIR/verify.out" 2>&1; then
      promotion_step "$CANDIDATE_ID" promote verify failed "$EVIDENCE_DIR/verify.out"
      die "the switched ends did not verify (the admission stays closed)"
    fi
    promotion_step "$CANDIDATE_ID" promote verify completed "$EVIDENCE_DIR/verify.out"
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
    EVIDENCE_DIR="$RECEIPT_DIR/evidence"
    mkdir -p "$EVIDENCE_DIR"
    LINUX_SWITCHED=1
    if [ "$ROLLBACK_ID" = "$ACTIVE_ID" ]; then
      PREVIOUS_RECORD="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["previous"]["record"])' "$STATE_ROOT/install-record.json")" \
        || die "the active record carries no rollback binding"
    else
      # The Linux pointer never moved for this maintenance attempt; its source generation comes
      # from the receipt's bound immutable generation, never from the mutable active pointer.
      LINUX_SWITCHED=0
      PREVIOUS_RECORD="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["from"])' "$RECEIPT_DIR/receipt.json")" \
        || die "the receipt carries no source generation"
    fi
    [ -f "$PREVIOUS_RECORD" ] || die "the recorded source generation is missing: $PREVIOUS_RECORD"
    PREVIOUS_RELEASE_TRIPLE="$(python3 -c 'import json,sys; r=json.load(open(sys.argv[1]))["release"]; print(r["version"], r["source_commit"], r["source_sha256"])' "$PREVIOUS_RECORD")" \
      || die "the rollback binding carries no release identity"
    PREVIOUS_GENERATION_DIR=""
    if [ -f "$RECEIPT_DIR/windows-promote.out" ]; then
      PREVIOUS_GENERATION_DIR="$(python3 -c '
import json, sys
try:
    print(json.load(open(sys.argv[1]))["previous"])
except Exception:
    pass
' "$RECEIPT_DIR/windows-promote.out")"
    fi
    [ -n "$RELEASE_FILE" ] || RELEASE_FILE="$HERE/../../release.json"
    [ -f "$RELEASE_FILE" ] || die "release manifest not found: $RELEASE_FILE"
    ensure_postgres_ready
    if [ -z "$PREVIOUS_GENERATION_DIR" ]; then
      # An unacknowledged switch is reconciled from the endpoint's own recorded status; the
      # unknown switch itself is never resent.
      WINDOWS_STATUS_STATUS=0
      WINDOWS_STATUS="$(WINDOWS_RELEASE_OVERRIDE="$PREVIOUS_RELEASE_TRIPLE" instance_lifecycle status "$ROLLBACK_ID")" \
        || WINDOWS_STATUS_STATUS=$?
      if [ "$WINDOWS_STATUS_STATUS" != "0" ]; then
        promotion_step "$ROLLBACK_ID" rollback rollback-windows unknown "$RECEIPT_DIR/windows-status.out"
        die "the endpoint status is unreadable; the Windows end cannot be reconciled (the admission stays closed)"
      fi
      RECONCILIATION="$(python3 - "$RECEIPT_DIR/windows-binding.json" "$WINDOWS_STATUS" <<'PY'
import json
import re
import sys


def normalize(value):
    return str(value or "").replace("\\", "/").rstrip("/")


def parse_config(arguments):
    matches = re.findall(r"--config\s+\"?([^\"]+?)\"?(?:\s|$)", str(arguments or ""))
    return normalize(matches[-1]) if matches else ""


binding = json.load(open(sys.argv[1], encoding="utf-8"))
status = json.load(open(sys.argv[2], encoding="utf-8"))
target = normalize(binding.get("dir")) + "/generation"
live = status.get("live_task") if isinstance(status, dict) else None
if not isinstance(live, dict):
    print("unknown")
    raise SystemExit(0)
execute = normalize(live.get("execute")).lower()
config = parse_config(live.get("arguments")).lower()
if not execute or not config:
    print("unknown")
    raise SystemExit(0)
records = [row for row in (status.get("pending"), status.get("last_promotion")) if isinstance(row, dict)]
record = next((row for row in records if normalize(row.get("generation")) == target), None)
source = normalize(record.get("previous")) if record else ""


def runs(generation):
    # The single launch convention the endpoint script uses (Runtime-Launcher + Config-Path).
    return (
        execute == (generation + "/runtime/scripts/description.exe").lower()
        and config == (generation + "/endpoint.json").lower()
    )


if runs(target):
    print(source if source else "unknown")
elif source and runs(source):
    # Positively the bound source generation of THIS maintenance: nothing was switched.
    print("before")
else:
    # A foreign, unparseable or unbound launcher is never claimed as an unswitched source.
    print("unknown")
PY
)"
      case "$RECONCILIATION" in
        before)
          PREVIOUS_GENERATION_DIR=""
          ;;
        /*|*:*)
          PREVIOUS_GENERATION_DIR="$RECONCILIATION"
          ;;
        *)
          promotion_step "$ROLLBACK_ID" rollback rollback-windows unknown "$RECEIPT_DIR/windows-status.out"
          die "the endpoint state is not provably recoverable (the admission stays closed); the unacknowledged switch is never resent"
          ;;
      esac
    fi
    # Rollback is a maintenance switch too: the current ends close, drain and stop first.
    if ! platform_close_admission "rollback" "operatorctl rollback" > "$EVIDENCE_DIR/rollback-close.out" 2>&1; then
      promotion_step "$ROLLBACK_ID" rollback rollback-close failed "$EVIDENCE_DIR/rollback-close.out"
      die "could not close the admission; nothing was rolled back"
    fi
    promotion_step "$ROLLBACK_ID" rollback rollback-close completed "$EVIDENCE_DIR/rollback-close.out"
    if ! PLATFORM_OPERATOR_DSN="${PLATFORM_OPERATOR_DSN:-}" "$AIRFLOW_VENV/bin/python" \
        -m description_pipeline.orchestration.lifecycle_state report --require-settled \
        > "$EVIDENCE_DIR/rollback-drain.out" 2>&1; then
      promotion_step "$ROLLBACK_ID" rollback rollback-drain failed "$EVIDENCE_DIR/rollback-drain.out"
      die "rollback refused: active or unresolved work remains (the admission stays closed)"
    fi
    promotion_step "$ROLLBACK_ID" rollback rollback-drain completed "$EVIDENCE_DIR/rollback-drain.out"
    if ! {
      airflow_lifecycle stop \
      && systemctl --user disable --now description-operator-proxy.service description-portal.service
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
      WINDOWS_ROLLBACK="$(WINDOWS_RELEASE_OVERRIDE="$PREVIOUS_RELEASE_TRIPLE" \
          instance_lifecycle rollback "$ROLLBACK_ID" "$PREVIOUS_GENERATION_DIR")" || WINDOWS_ROLLBACK_STATUS=$?
      if [ "$WINDOWS_ROLLBACK_STATUS" = "2" ]; then
        promotion_step "$ROLLBACK_ID" rollback rollback-windows unknown "$RECEIPT_DIR/windows-rollback.out"
        die "the Windows rollback outcome is unacknowledged (the admission stays closed)"
      fi
      if [ "$WINDOWS_ROLLBACK_STATUS" != "0" ]; then
        promotion_step "$ROLLBACK_ID" rollback rollback-windows failed "$RECEIPT_DIR/windows-rollback.out"
        die "the Windows rollback failed; nothing further was moved"
      fi
      promotion_step "$ROLLBACK_ID" rollback rollback-windows completed "$WINDOWS_ROLLBACK"
    else
      # The endpoint's own status shows this maintenance never completed there: nothing to
      # restore on Windows, and the reconciliation is recorded with that relay as evidence.
      promotion_step "$ROLLBACK_ID" rollback rollback-windows completed "$RECEIPT_DIR/windows-status.out"
    fi
    # The Linux end re-activates the recorded previous generation; no generation file is rewritten.
    if [ "$LINUX_SWITCHED" = "1" ]; then
      if ! "$AIRFLOW_VENV/bin/python" "$HERE/install_record.py" repoint \
          --state-root "$STATE_ROOT" --record "$PREVIOUS_RECORD" > "$EVIDENCE_DIR/rollback-linux.out" 2>&1; then
        promotion_step "$ROLLBACK_ID" rollback rollback-linux failed "$EVIDENCE_DIR/rollback-linux.out"
        die "could not restore the previous Linux generation"
      fi
      promotion_step "$ROLLBACK_ID" rollback rollback-linux completed "$EVIDENCE_DIR/rollback-linux.out"
    else
      # The pointer never moved (receipt source == active generation); the reconciliation is
      # recorded with the receipt itself as evidence.
      promotion_step "$ROLLBACK_ID" rollback rollback-linux completed "$RECEIPT_DIR/receipt.json"
    fi
    # The restored generation starts through its own environment and both ends re-verify; the
    # restored record is checked against its own immutable snapshot (the operator's current
    # platform.json may already describe a candidate).
    load_running_env
    load_installed
    if ! {
      python3 "$HERE/render_operator.py" --env-file "$ENV_FILE" --units-dir "$TARGET" \
      && airflow_render \
      && systemctl --user daemon-reload \
      && airflow_lifecycle start \
      && airflow_unpause_dag \
      && systemctl --user enable --now description-portal.service description-operator-proxy.service \
      && systemctl --user is-active --quiet description-portal.service description-operator-proxy.service \
      && "$AIRFLOW_VENV/bin/python" "$HERE/install_record.py" verify-running \
        --state-root "$STATE_ROOT" \
      && WINDOWS_HEALTH="$(WINDOWS_RELEASE_OVERRIDE="$PREVIOUS_RELEASE_TRIPLE" instance_lifecycle health "$ACTIVE_ID")" \
      && promotion_extract_health "$WINDOWS_HEALTH" "$EVIDENCE_DIR/rollback-health.json" \
      && "$AIRFLOW_VENV/bin/python" "$HERE/install_record.py" endpoint-identity \
        --health "$EVIDENCE_DIR/rollback-health.json" --release-file "$RELEASE_FILE" \
        --record "$STATE_ROOT/install-record.json" --out "$EVIDENCE_DIR/rollback-identity.json"
    } > "$EVIDENCE_DIR/rollback-verify.out" 2>&1; then
      promotion_step "$ROLLBACK_ID" rollback rollback-verify failed "$EVIDENCE_DIR/rollback-verify.out"
      die "the restored ends did not verify (the admission stays closed)"
    fi
    promotion_step "$ROLLBACK_ID" rollback rollback-verify completed "$EVIDENCE_DIR/rollback-verify.out"
    echo "rollback complete: restored $(basename "$PREVIOUS_RECORD" .json) (the admission stays closed; run start after review)"
    ;;
  reconcile)
    [ -n "$RUN_ID" ] && [ -n "$STAGE" ] && [ -n "$OUTCOME" ] && [ "${#EVIDENCE[@]}" -gt 0 ] \
      || die "reconcile requires --run-id, --stage, --outcome and at least one --evidence"
    case "$OUTCOME" in
      completed|failed|cancelled) ;;
      *) die "unresolved is not a result; --outcome must be completed, failed or cancelled" ;;
    esac
    RECONCILE_RUN_ID="$RUN_ID" RECONCILE_STAGE="$STAGE" RECONCILE_OUTCOME="$OUTCOME" \
    RECONCILE_EVIDENCE="$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1:]))' "${EVIDENCE[@]}")" \
      "$AIRFLOW_VENV/bin/python" - <<'PY' || die "reconcile refused"
# platform-reconcile
import json
import os

from description_pipeline import io
from description_pipeline.platform.store import PlatformStore

run_id = os.environ["RECONCILE_RUN_ID"]
stage = os.environ["RECONCILE_STAGE"]
outcome = os.environ["RECONCILE_OUTCOME"]
evidence = json.loads(os.environ["RECONCILE_EVIDENCE"])

# The confirmation binds the same registered reconcile operation it carries as evidence: the
# store refuses an unbound dict, a foreign run/stage, or a stage without an unresolved original
# operation.  A retry with the same outcome reuses the same operation key - none is re-sent.
store = PlatformStore.from_environment()
operation = store.register_operation(
    run_id=run_id,
    stage=stage,
    kind="reconcile",
    operation_key=f"operatorctl-reconcile:{run_id}:{stage}:{outcome}",
    params_digest=io.digest({"outcome": outcome, "evidence": evidence}),
)
store.confirm_stage(
    run_id=run_id,
    stage=stage,
    state=outcome,
    reconcile={"operation_id": operation.operation_id, "evidence": evidence},
)
print(f"reconciled={run_id}:{stage} outcome={outcome}")
PY
    ;;
  *) usage ;;
esac
