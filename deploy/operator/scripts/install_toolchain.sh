#!/usr/bin/env bash
# Installs the pinned uv and CPython, then prints AIRFLOW_PYTHON=<path>.
set -euo pipefail
# Qualified, captured toolchain for the supported Ubuntu 22.04 deployment. Fixed on purpose:
# neither uv nor the CPython patch is configurable, so the runtime is reproducible.
UV_VERSION=0.12.23
PYTHON_VERSION=3.12.14
export PATH="$HOME/.local/bin:$PATH"

needs_uv=0
if command -v uv >/dev/null 2>&1; then
  [ "$(uv --version | awk '{print $2}')" = "$UV_VERSION" ] || needs_uv=1
else
  needs_uv=1
fi
if [ "$needs_uv" = "1" ]; then
  command -v curl >/dev/null 2>&1 || { echo "curl is required to install uv" >&2; exit 1; }
  curl -LsSf "https://astral.sh/uv/${UV_VERSION}/install.sh" | sh >/dev/null
  export PATH="$HOME/.local/bin:$PATH"
  [ "$(uv --version | awk '{print $2}')" = "$UV_VERSION" ] || { echo "uv $UV_VERSION install failed" >&2; exit 1; }
fi

uv python install "$PYTHON_VERSION" >/dev/null
PYTHON_PATH="$(uv python find "$PYTHON_VERSION")"
"$PYTHON_PATH" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) else 1)' \
  || { echo "uv did not provide CPython 3.12" >&2; exit 1; }
echo "AIRFLOW_PYTHON=$PYTHON_PATH"
