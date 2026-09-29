#!/usr/bin/env bash
set -euo pipefail

# Install the complete offline Linux distribution next to this script.  The
# virtual environment can be relocated with DESCRIPTION_VENV when the bundle
# directory is read-only or shared by several users.
ROOT="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
VENV="${DESCRIPTION_VENV:-$ROOT/.venv}"

# A first-time user may not have 3.12 anywhere on the machine.  Naming a way to get one, and the
# exact re-run, is more useful than repeating the variable they have just failed to set.
report_missing_python() {
    echo "$1" >&2
    echo "Install CPython 3.12 (for example 'uv python install 3.12', or a distribution package)," >&2
    echo "then re-run: DESCRIPTION_PYTHON=<path/to/python3.12> bash install.sh" >&2
}

if [[ "${1:-}" == "--venv" ]]; then
    if [[ $# -ne 2 || -z "${2:-}" ]]; then
        echo "usage: bash install.sh [--venv PATH]" >&2
        exit 2
    fi
    VENV=$2
    shift 2
fi
if [[ $# -ne 0 ]]; then
    echo "usage: bash install.sh [--venv PATH]" >&2
    exit 2
fi

if [[ ! -f "$ROOT/requirements.lock" || ! -d "$ROOT/wheels" ]]; then
    echo "This script must run from an extracted Linux distribution bundle" >&2
    exit 2
fi

if [[ -x "$VENV/bin/python" ]]; then
    # An existing 3.12 runtime is all this script needs.  Demanding a matching interpreter on PATH
    # would refuse the documented shared-runtime case (DESCRIPTION_VENV), and any re-run after the
    # distribution's python3 moved past 3.12, even though the venv is fine.
    if ! "$VENV/bin/python" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) else 1)'; then
        echo "Existing virtual environment is not CPython 3.12: $VENV" >&2
        exit 2
    fi
else
    # Only creating the runtime needs an interpreter; validate that one before touching the disk.
    python_candidate="${DESCRIPTION_PYTHON:-}"
    if [[ -z "$python_candidate" ]]; then
        for candidate in python3.12 python3; do
            if command -v "$candidate" >/dev/null 2>&1; then
                python_candidate=$(command -v "$candidate")
                break
            fi
        done
    fi
    if [[ -z "$python_candidate" ]]; then
        report_missing_python "CPython 3.12 is required; set DESCRIPTION_PYTHON to its interpreter"
        exit 2
    fi
    if ! "$python_candidate" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) else 1)'; then
        report_missing_python "CPython 3.12 is required; found $($python_candidate --version 2>&1)"
        exit 2
    fi
    if ! mkdir -p "$(dirname -- "$VENV")"; then
        echo "Cannot create the directory for the runtime: $(dirname -- "$VENV")" >&2
        echo "Set DESCRIPTION_VENV to a writable path." >&2
        exit 2
    fi
    # A Python without ensurepip (Debian and Ubuntu ship it as python3.12-venv) fails here with a
    # message that says what is missing but not what to install.
    if ! "$python_candidate" -m venv "$VENV"; then
        echo "Could not create a virtual environment at: $VENV" >&2
        echo "Install the venv module (Debian/Ubuntu: sudo apt install python3.12-venv), or choose another" >&2
        echo "location with DESCRIPTION_VENV=/some/writable/path." >&2
        exit 2
    fi
fi

if ! "$VENV/bin/python" -m pip install \
    --no-index \
    --no-cache-dir \
    --require-hashes \
    --find-links "$ROOT/wheels" \
    -r "$ROOT/requirements.lock"; then
    echo "Installing the pinned runtime into $VENV failed" >&2
    echo "Check that this bundle is complete (wheels/ and requirements.lock), that its disk has space and" >&2
    echo "that the path is writable, then run this script again; it repeats the same installation." >&2
    exit 2
fi

# ZIP extraction does not consistently preserve executable bits.  Make the
# daily launcher directly runnable after this first command where permitted.
chmod +x "$ROOT/install.sh" "$ROOT/submit.sh" 2>/dev/null || true

echo "Installed description into: $VENV"
echo "Check this machine: $VENV/bin/description doctor"
echo "Run the offline demo: $VENV/bin/description quickstart --run"
echo "Run: bash $ROOT/submit.sh --root /path/to/model"
