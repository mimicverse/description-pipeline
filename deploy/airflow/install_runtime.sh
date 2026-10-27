#!/usr/bin/env bash
# Install only the reproducible private Airflow/Python runtime.
#   AIRFLOW_VENV=/home/<user>/solidworks-urdf/airflow-venv
#   AIRFLOW_HOME=/home/<user>/solidworks-urdf/airflow-home
#   PIPELINE_WHEEL=/home/<user>/solidworks-urdf/wheels/mimicverse_description-<version>-py3-none-any.whl
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

die() { echo "install_runtime.sh: $*" >&2; exit 1; }

if [ "$#" -ne 0 ]; then
  echo "usage: $0" >&2
  exit 2
fi

: "${AIRFLOW_VENV:?set AIRFLOW_VENV to the private venv path}"
: "${AIRFLOW_HOME:?set AIRFLOW_HOME to the Airflow home directory}"
: "${PIPELINE_WHEEL:?set PIPELINE_WHEEL to the built mimicverse_description wheel}"

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
"$AIRFLOW_VENV/bin/python" -m pip check
"$AIRFLOW_VENV/bin/description" doctor

# Airflow CLI probes may initialize logging under AIRFLOW_HOME. Keep that
# side effect out of the restored or configured home and remove it on exit.
PROBE_HOME="$(mktemp -d "$AIRFLOW_VENV/.airflow-probe-home.XXXXXXXX")"
trap 'rm -rf -- "$PROBE_HOME"' EXIT
probe_airflow() {
  (
    unset AIRFLOW_CONFIG PYTHONHOME PYTHONPATH VIRTUAL_ENV
    while IFS= read -r name; do
      case "$name" in
        AIRFLOW__*) unset "$name" ;;
      esac
    done < <(compgen -e)
    PYTHONNOUSERSITE=1 AIRFLOW_HOME="$PROBE_HOME" "$AIRFLOW_VENV/bin/airflow" "$@"
  )
}
INSTALLED_AIRFLOW_VERSION="$(probe_airflow version 2>/dev/null)"
[ "$INSTALLED_AIRFLOW_VERSION" = "3.3.2" ] \
  || die "expected Airflow 3.3.2, found: ${INSTALLED_AIRFLOW_VERSION:-unknown}"
probe_airflow triggerer --help >/dev/null \
  || die "the pinned Airflow runtime has no triggerer command"
