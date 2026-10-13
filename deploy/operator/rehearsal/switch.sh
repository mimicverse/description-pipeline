#!/usr/bin/env bash
# Final v1.3.4 switch on rl-4090: source-agnostic stage / switch / rollback.
#
#   switch.sh stage  <tag> <wheel-name> <wheel-sha256> <source-sha256> <zip-name> <zip-sha256>
#       Install the inactive runtime and import the DAG in the isolated AIRFLOW_HOME.
#       No live change: services, operator.env and the live rollback runtime stays untouched.
#
#   switch.sh switch <tag> <wheel-name> <wheel-sha256> <source-sha256> <zip-name> <zip-sha256>
#       Stage (idempotent) -> require zero active Airflow runs/tasks -> backup and promote
#       operator.env (only AIRFLOW_VENV + PIPELINE_WHEEL change) -> install + start the new
#       runtime -> read-only checks.  The previous env file and the whole live rollback tree/venv
#       remain the rollback target.
#
#   switch.sh rollback <tag>
#       Restore the newest operator.env.bak-pre-<tag>-* and reinstall/start the live rollback tree.
#
# Never prints secrets.  The pre-switch gate reads the Airflow metadata DB directly because
# operatorctl health alone does not prove that no run/task is active.
set -euo pipefail

MODE="${1:?usage: switch.sh stage|switch <tag> <wheel> <wheel-sha> <source-sha> <zip> <zip-sha> | rollback <tag>}"
TAG="${2:?release tag required, for example split-v134final}"
OWNER="${OPERATOR_ROOT:-/home/andy/operator}"
OP="$OWNER"
REL_NEW="$OP/releases/$TAG"
LIVE_RELEASE="${LIVE_RELEASE:?set LIVE_RELEASE to the live release dir name (rollback target)}"
REL_OLD="$OP/releases/$LIVE_RELEASE"
ENV_ACTIVE="$OP/operator.env"
ENV_NEW="$OP/operator-$TAG.env"
VENV="$OP/airflow-venv-$TAG"
LIVE_TAG="${LIVE_TAG:?set LIVE_TAG to the live rollback venv tag}"
LIVE_VENV="$OP/airflow-venv-$LIVE_TAG"
POSTGRES_ROOT="${POSTGRES_ROOT:-$OP/airflow-pg}"
PSQL="$POSTGRES_ROOT/root/usr/lib/postgresql/${POSTGRES_MAJOR:-14}/bin/psql"
REHEARSAL="${REHEARSAL_DIR:-$OP/split-$TAG-rehearsal}"
CA="${OPERATOR_TLS_CA:-$OP/state/tls/operator-tls-fullchain.crt}"

[ "$VENV" != "$LIVE_VENV" ] || { echo "refusing to use the live rollback venv"; exit 1; }

ctl() { ( cd "$1" && bash deploy/operator/operatorctl.sh "$2" --env-file "$ENV_ACTIVE" ); }

active_rows() {
  local port
  port="$(sed -n 's/^POSTGRES_PORT=//p' "$ENV_ACTIVE" | head -1)"
  port="${POSTGRES_PORT:-$port}"
  [ -n "$port" ] || { echo "POSTGRES_PORT is missing in $ENV_ACTIVE; add the rendered value"; exit 1; }
  "$PSQL" -h "$OP/airflow-pg/socket" -p "$port" -U solidworks -d airflow_meta -At -c \
    "SELECT 'dag_run '||count(*) FROM dag_run WHERE state IN ('running','queued') UNION ALL
     SELECT 'task_instance '||count(*) FROM task_instance WHERE state IN
       ('running','queued','scheduled','up_for_retry','up_for_reschedule','restarting','deferred');"
}

require_zero_active() {
  local rows total
  rows="$(active_rows)"
  echo "--- active Airflow rows (must all be 0) ---"; echo "$rows"
  total="$(echo "$rows" | awk '{sum += $2} END {print sum + 0}')"
  [ "$total" = "0" ] || { echo "refusing: $total active Airflow run/task rows; drain first"; exit 1; }
}

stage_release() {
  local wheel_name="${1:?wheel name}" want_wheel="${2:?wheel sha}" want_source="${3:?source sha}"
  local zip_name="${4:?zip name}" want_zip="${5:?zip sha}"
  mkdir -p "$REL_NEW"
  [ -f "$REL_NEW/$zip_name" ] || { echo "put $zip_name into $REL_NEW first"; exit 1; }
  [ -f "$REL_NEW/$wheel_name" ] || { echo "put $wheel_name into $REL_NEW first"; exit 1; }
  got_zip="$(sha256sum "$REL_NEW/$zip_name" | cut -d' ' -f1)"
  [ "$got_zip" = "$want_zip" ] || { echo "deployment zip sha mismatch: $got_zip"; exit 1; }
  ( cd "$REL_NEW" && unzip -q -o "$zip_name" )
  [ -x "$REL_NEW/deploy/operator/operatorctl.sh" ] || { echo "release tree lacks deploy/operator/operatorctl.sh"; exit 1; }
  bash "$REHEARSAL/prepare-venv.sh" "$REL_NEW" "$TAG"
  derive_env
  bash "$REHEARSAL/stage-and-validate.sh" "$REL_NEW" "$TAG" "$wheel_name" "$want_wheel" "$want_source" "$zip_name" "$want_zip"
}

derive_env() {
  # Inactive candidate env from the active one: only AIRFLOW_VENV and PIPELINE_WHEEL
  # change; every secret, state and DB setting is copied byte-for-byte.  Values are
  # never printed; only the key sets are compared.
  [ -f "$ENV_ACTIVE" ] || { echo "missing $ENV_ACTIVE"; exit 1; }
  if [ ! -f "$ENV_NEW" ]; then
    ( umask 077
      sed -E "s#^AIRFLOW_VENV=.*#AIRFLOW_VENV=$VENV#; s#^PIPELINE_WHEEL=.*#PIPELINE_WHEEL=$REL_NEW/$WHEEL_FILE#" \
        "$ENV_ACTIVE" > "$ENV_NEW.tmp"
      mv "$ENV_NEW.tmp" "$ENV_NEW" )
  fi
  local a b
  a="$(sed -E 's#^AIRFLOW_VENV=.*#<ptr>#; s#^PIPELINE_WHEEL=.*#<ptr>#' "$ENV_ACTIVE")"
  b="$(sed -E 's#^AIRFLOW_VENV=.*#<ptr>#; s#^PIPELINE_WHEEL=.*#<ptr>#' "$ENV_NEW")"
  [ "$a" = "$b" ] || { echo "candidate env differs beyond AIRFLOW_VENV/PIPELINE_WHEEL"; exit 1; }
  echo "candidate env ready: $ENV_NEW (secrets, state and DB settings preserved)"
}

if [ "$MODE" = rollback ]; then
  backup="$(ls -t "$OP"/operator.env.bak-pre-"$TAG"-* 2>/dev/null | head -1 || true)"
  [ -n "$backup" ] || { echo "no operator.env.bak-pre-$TAG-* backup found"; exit 1; }
  echo "rollback: stopping $TAG (if active) and restoring $backup"
  ctl "$REL_NEW" stop || echo "$TAG stop reported an error; continuing rollback"
  cp -p "$backup" "$ENV_ACTIVE"
  ctl "$REL_OLD" install
  ctl "$REL_OLD" start
  ctl "$REL_OLD" health
  echo "ROLLBACK OK (live rollback runtime $LIVE_RELEASE restored; env from $backup)"
  exit 0
fi

wheel_name="${3:?wheel name required}"
want_wheel="${4:?wheel sha required}"
want_source="${5:?source sha required}"
zip_name="${6:?deployment zip name required}"
want_zip="${7:?deployment zip sha required}"
WHEEL_FILE="$wheel_name"

if [ "$MODE" = stage ]; then
  echo "--- stage-only: inactive runtime + DAG import, live untouched ---"
  stage_release "$wheel_name" "$want_wheel" "$want_source" "$zip_name" "$want_zip"
  echo "STAGE OK — nothing live changed; run 'switch' when the final source is approved"
  exit 0
fi
[ "$MODE" = switch ] || { echo "unknown mode: $MODE"; exit 1; }

echo "--- phase A: stage + full rehearsal in the INACTIVE $TAG venv (no live change) ---"
stage_release "$wheel_name" "$want_wheel" "$want_source" "$zip_name" "$want_zip"

echo "--- phase B0: drain gate (no active runs/tasks) ---"
require_zero_active

echo "--- phase B: quiesce the live rollback runtime and promote the validated env ---"
if systemctl --user is-active description-portal.service description-airflow-scheduler.service description-airflow-api-server.service >/dev/null 2>&1; then
  ctl "$REL_OLD" stop
else
  echo "live rollback runtime already stopped - skipping stop"
fi
backup="$ENV_ACTIVE.bak-pre-$TAG-$(date -u +%Y%m%dT%H%M%SZ)"
cp -p "$ENV_ACTIVE" "$backup"
echo "env backup: $backup"
cp "$ENV_NEW" "$ENV_ACTIVE"

echo "--- phase B2: pre-start install, then start $TAG ---"
ctl "$REL_NEW" install
ctl "$REL_NEW" start

echo "--- phase C: read-only checks (no writes to the running runtime, TLS verified) ---"
ctl "$REL_NEW" health
"$VENV/bin/python" - <<'PY'
import json
from description_pipeline import __version__
from description_pipeline.runtime import tool_record
record = tool_record(role="portable")
print(json.dumps({"version": __version__, "source_sha256": record["source_sha256"]}, ensure_ascii=False))
PY
"$VENV/bin/python" -m pip check
"$VENV/bin/python" -m pip show mimicverse-description | sed -n '1,3p'
OPERATOR_URL="${OPERATOR_URL:-}"
if [ -z "$OPERATOR_URL" ] && [ -f "$OP/state/resolved.env" ]; then
  OPERATOR_URL="$(sed -n 's/^OPERATOR_URL=//p' "$OP/state/resolved.env" | head -1)"
fi
[ -n "$OPERATOR_URL" ] || { echo "OPERATOR_URL is missing; set it or install the deployment"; exit 1; }
curl --cacert "$CA" -s -o /dev/null -w 'portal:%{http_code}\n' "${OPERATOR_URL%/}/"
curl --cacert "$CA" -s "${OPERATOR_URL%/}/operator-healthz" | head -c 200; echo
require_zero_active

echo "SWITCH OK ($TAG live) — rollback with: $0 rollback $TAG  (env backup: $backup)"
