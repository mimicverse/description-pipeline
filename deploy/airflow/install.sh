#!/usr/bin/env bash
# Reproducible Linux install for the Airflow side of the SolidWorks-to-URDF pipeline.
# Requires explicit, private paths; never touches system or shared interpreters.
#
#   AIRFLOW_VENV=/home/<user>/solidworks-urdf/airflow-venv
#   AIRFLOW_HOME=/home/<user>/solidworks-urdf/airflow-home
#   PIPELINE_WHEEL=/home/<user>/solidworks-urdf/wheels/mimicverse_description-<version>-py3-none-any.whl
#   AIRFLOW_DB_URL=postgresql+psycopg2://solidworks@/airflow_meta?host=/abs/socket&port=5433
#   AIRFLOW_API_PORT=<loopback port rendered from the operator deployment>
#
# Rerunning is safe: the existing Fernet key and [api_auth] jwt_secret in
# $AIRFLOW_HOME/airflow.cfg are preserved (see render_config.py).
#
#   install.sh --stage:  build and verify the generation's runtime only (venv, hash-locked
#                        dependencies, doctor); no configuration switch and no migration -
#                        the stage half of the promote protocol.  Other arguments are ignored.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODE="${1:-}"

die() { echo "install.sh: $*" >&2; exit 1; }

: "${AIRFLOW_VENV:?set AIRFLOW_VENV to the private venv path}"
: "${AIRFLOW_HOME:?set AIRFLOW_HOME to the Airflow home directory}"
: "${PIPELINE_WHEEL:?set PIPELINE_WHEEL to the built mimicverse_description wheel}"
if [ "$MODE" != "--stage" ]; then
  # Normal installation renders the managed config and migrates the live database; refuse
  # missing connection/port inputs here, before creating the venv or running pip.
  : "${AIRFLOW_DB_URL:?set AIRFLOW_DB_URL to the dedicated PostgreSQL DSN}"
  : "${AIRFLOW_RUNTIME_DB_URL:?set AIRFLOW_RUNTIME_DB_URL to the least-privilege runtime DSN}"
  : "${AIRFLOW_API_PORT:?set AIRFLOW_API_PORT to the Airflow api-server loopback port}"
fi

for pair in "AIRFLOW_VENV:$AIRFLOW_VENV" "AIRFLOW_HOME:$AIRFLOW_HOME" "PIPELINE_WHEEL:$PIPELINE_WHEEL"; do
  name="${pair%%:*}" value="${pair#*:}"
  case "$value" in
    /*) ;;
    *) die "$name must be an absolute path (got: $value)" ;;
  esac
  case "$value" in
    /|/usr|/etc|/opt|/var|"${HOME:-/nonexistent}")
      die "$name refuses a shared/root path: $value" ;;
  esac
done
[ "$AIRFLOW_VENV" != "$AIRFLOW_HOME" ] || die "AIRFLOW_VENV and AIRFLOW_HOME must be distinct"
[ -f "$PIPELINE_WHEEL" ] || die "PIPELINE_WHEEL does not exist: $PIPELINE_WHEEL"
case "$(basename "$PIPELINE_WHEEL")" in
  mimicverse_description-*.whl) ;;
  *) die "PIPELINE_WHEEL must be the built mimicverse_description-*.whl wheel" ;;
esac

PYTHON="${AIRFLOW_PYTHON:-python3.12}"
command -v "$PYTHON" >/dev/null 2>&1 || die "AIRFLOW_PYTHON=$PYTHON not found"
[ "$("$PYTHON" -c 'import sys; print("%d.%d" % sys.version_info[:2])')" = "3.12" ] \
  || die "AIRFLOW_PYTHON must be Python 3.12 (the pinned lock targets CPython 3.12)"
if [ ! -x "$AIRFLOW_VENV/bin/python" ]; then
  "$PYTHON" -m venv "$AIRFLOW_VENV" \
    || die "'$PYTHON -m venv' failed; pass AIRFLOW_PYTHON=<a Python 3.12 with working venv/ensurepip>"
fi

# The lock is a fully resolved, hash-pinned, wheel-only stack: no floating versions,
# no build dependencies, no pip upgrade in the private venv.
"$AIRFLOW_VENV/bin/python" -m pip install --require-hashes --only-binary=:all: \
  -r "$HERE/requirements.lock"
"$AIRFLOW_VENV/bin/python" -m pip install --no-deps --force-reinstall "$PIPELINE_WHEEL"
# Fail closed if the lock does not cover the wheel's declared runtime dependencies.
"$AIRFLOW_VENV/bin/python" -m pip check
# pip check excludes optional dependencies. Exercise the Linux verifier before
# installing configuration or migrating the service database.
"$AIRFLOW_VENV/bin/description" doctor

if [ "$MODE" = "--stage" ]; then
  echo "Airflow runtime staged (no configuration switch, no migration): $("$AIRFLOW_VENV/bin/airflow" version)"
  exit 0
fi

# Atomic 0600 config; preserves previously generated Fernet/JWT secrets on rerun.
AIRFLOW_DB_URL="$AIRFLOW_DB_URL" AIRFLOW_API_PORT="$AIRFLOW_API_PORT" "$AIRFLOW_VENV/bin/python" "$HERE/render_config.py" \
  --home "$AIRFLOW_HOME" --venv "$AIRFLOW_VENV" \
  --template "$HERE/airflow.cfg.template" --dags-folder "$HERE/dags"

AIRFLOW_HOME="$AIRFLOW_HOME" "$AIRFLOW_VENV/bin/airflow" db migrate
# The native stage waits on this pool (six-stage contract / IF-02): create it
# idempotently so the first run cannot fail on a missing pool.
AIRFLOW_HOME="$AIRFLOW_HOME" "$AIRFLOW_VENV/bin/python" "$HERE/scripts/ensure_pool.py"
# The sign-in state table is created inside this migration window by the one canonical owner
# (the same module entry the CI migration step runs); the runtime identity that takes over
# below owns no CREATE privilege and never attempts DDL.
AIRFLOW_HOME="$AIRFLOW_HOME" "$AIRFLOW_VENV/bin/python" -m description_pipeline.orchestration.feishu_auth
# The migration above ran under the install identity (DDL inside the airflow schema only);
# the persisted service configuration carries the least-privilege runtime identity.
AIRFLOW_DB_URL="$AIRFLOW_RUNTIME_DB_URL" AIRFLOW_API_PORT="$AIRFLOW_API_PORT" "$AIRFLOW_VENV/bin/python" "$HERE/render_config.py" \
  --home "$AIRFLOW_HOME" --venv "$AIRFLOW_VENV" \
  --template "$HERE/airflow.cfg.template" --dags-folder "$HERE/dags"
# Installation preserves admission state. Only operatorctl start opens the DAG.
echo "Airflow installed: $("$AIRFLOW_VENV/bin/airflow" version), home=$AIRFLOW_HOME"
