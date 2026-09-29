#!/usr/bin/env bash
set -euo pipefail

# Keep this launcher deliberately thin: model update owns preflight, source
# capture, build, verification, Git push and pull-request handling.
ROOT="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
VENV="${DESCRIPTION_VENV:-$ROOT/.venv}"
PYTHON="$VENV/bin/python"

if [[ ! -x "$PYTHON" ]]; then
    echo "Linux runtime is not installed: run bash $ROOT/install.sh first" >&2
    exit 2
fi
if ! "$PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) else 1)'; then
    echo "Linux runtime must use CPython 3.12: $VENV" >&2
    exit 2
fi

exec "$PYTHON" -m description_pipeline model update "$@"
