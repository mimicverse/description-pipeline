#!/usr/bin/env bash
# Read-only validation of the ONE bound managed proxy binary, for verbs that must not change
# the deployment: prints the bound ``NGINX_BIN`` (from resolved.env) when it exists, is
# executable and is the binary the installed proxy unit actually executes.
#
# This script never installs, downloads, edits or repairs anything: a missing or mismatched
# binary is reported as drift by the caller and the operator is sent to the controlled change
# entry (install / promote / rollback) that owns the side-effectful preparation.
set -euo pipefail

binary="${NGINX_BIN:-}"
unit_file=""
while [ $# -gt 0 ]; do
  case "$1" in
    --unit-file) unit_file="${2:-}"; shift 2 ;;
    *) echo "check_proxy_binary: unknown argument: $1" >&2; exit 2 ;;
  esac
done

[ -n "$binary" ] \
  || { echo "no NGINX_BIN is bound in resolved.env (run install to re-render)" >&2; exit 1; }
case "$binary" in
  /*) ;;
  *) echo "the bound NGINX_BIN is not an absolute path: $binary" >&2; exit 1 ;;
esac
[ -x "$binary" ] \
  || { echo "managed nginx binary missing or not executable: $binary" >&2; exit 1; }
if [ -n "$unit_file" ]; then
  [ -f "$unit_file" ] \
    || { echo "the installed proxy unit is missing: $unit_file" >&2; exit 1; }
  grep -qF "ExecStart=$binary " "$unit_file" \
    || { echo "the installed proxy unit does not execute the bound nginx binary: $unit_file" >&2; exit 1; }
fi
printf '%s\n' "$binary"
