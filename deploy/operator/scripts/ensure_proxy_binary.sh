#!/usr/bin/env bash
# The ONE managed proxy binary for this deployment: prints its absolute path.
#
# The resolved configuration, the systemd units and health all read the same ``NGINX_BIN``
# value, so this script is the single place that decides it: an already-executable configured
# or system binary is used as-is; otherwise the user-level toolchain nginx is installed by
# ``install_proxy.sh`` and the path IT actually landed is the value - the installer's own
# output is consumed here, never discarded.  A missing or non-executable result refuses; no
# path that was never installed is ever rendered.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

configured="${NGINX_BIN:-/usr/sbin/nginx}"
if [ -x "$configured" ]; then
  printf '%s\n' "$configured"
  exit 0
fi

: "${OPERATOR_STATE:?set OPERATOR_STATE to the private operator state directory}"
binary="$(OPERATOR_STATE="$OPERATOR_STATE" bash "$HERE/install_proxy.sh")" \
  || { echo "ensure_proxy_binary: the user-level nginx install failed" >&2; exit 1; }
if [ -z "$binary" ] || [ ! -x "$binary" ]; then
  echo "ensure_proxy_binary: install_proxy.sh produced no executable binary (got '${binary:-}')" >&2
  exit 1
fi
printf '%s\n' "$binary"
