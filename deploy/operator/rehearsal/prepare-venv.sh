#!/usr/bin/env bash
# Prepare the INACTIVE <tag> Linux venv on rl-4090 (staging only).
#
#   prepare-venv.sh <release-dir> <tag>                    # install from the release lock (warm pip cache)
#   prepare-venv.sh <release-dir> <tag> --offline <dir>    # install from a local wheelhouse
#   prepare-venv.sh <release-dir> <tag> --seed  <dir>      # download the lock into a wheelhouse (network once)
#
# Base interpreter: the uv-managed CPython 3.12.14 (UV_PYTHON overrides).
# The candidate venv (airflow-venv-<tag>) is created fresh; the live venv
# (airflow-venv-$LIVE_TAG, the rollback target) is never touched.  The hash-pinned
# lock is installed binary-only with --require-hashes.  Idempotent: re-run to re-check a
# rebuilt lock.  Never starts/stops services or writes operator.env.
set -euo pipefail
REL="${1:?usage: prepare-venv.sh <release-dir> <tag> [--offline <dir>|--seed <dir>]}"
TAG="${2:?release tag required, for example split-v134final}"
MODE=install; WHEELHOUSE=""
case "${3:-}" in
  --offline|--seed) MODE="${3#--}"; WHEELHOUSE="${4:?wheelhouse directory required}";;
  "") ;;
  *) echo "unknown option: $3"; exit 1;;
esac
BASE="${UV_PYTHON:-/home/andy/.local/share/uv/python/cpython-3.12.14-linux-x86_64-gnu/bin/python3.12}"
OWNER="${OPERATOR_ROOT:-/home/andy/operator}"
VENV="$OWNER/airflow-venv-$TAG"
LIVE_TAG="${LIVE_TAG:?set LIVE_TAG to the live rollback venv tag}"
LIVE_VENV="$OWNER/airflow-venv-$LIVE_TAG"
LOCK="$REL/deploy/airflow/requirements.lock"

[ -x "$BASE" ] || { echo "base interpreter missing: $BASE"; exit 1; }
[ -f "$LOCK" ] || { echo "release lock missing: $LOCK (stage the release tree first)"; exit 1; }
[ "$VENV" != "$LIVE_VENV" ] || { echo "refusing to prepare the live rollback venv"; exit 1; }
echo "release: $REL"; echo "tag: $TAG"; "$BASE" --version; sha256sum "$LOCK"

PIP_ARGS=(--disable-pip-version-check --require-hashes --only-binary=:all:)
if [ ! -x "$VENV/bin/python" ]; then "$BASE" -m venv "$VENV"; fi

case "$MODE" in
  seed)
    mkdir -p "$WHEELHOUSE"
    "$VENV/bin/python" -m pip download "${PIP_ARGS[@]}" -d "$WHEELHOUSE" -r "$LOCK"
    echo "wheelhouse seeded: $WHEELHOUSE ($(ls "$WHEELHOUSE" | wc -l) files)"
    ;;
  offline)
    [ -d "$WHEELHOUSE" ] || { echo "wheelhouse missing: $WHEELHOUSE"; exit 1; }
    "$VENV/bin/python" -m pip --version
    "$VENV/bin/python" -m pip install "${PIP_ARGS[@]}" --no-index --find-links "$WHEELHOUSE" -r "$LOCK"
    "$VENV/bin/python" -m pip check
    echo "venv ready (offline): $VENV"
    ;;
  install)
    "$VENV/bin/python" -m pip --version
    "$VENV/bin/python" -m pip install "${PIP_ARGS[@]}" -r "$LOCK"
    "$VENV/bin/python" -m pip check
    echo "venv ready: $VENV"
    ;;
esac
