#!/usr/bin/env bash
# Install a user-level nginx under OPERATOR_STATE/toolchain (no sudo, no system-wide changes).
# Prints the nginx binary path on success; reuses an existing extraction.
set -euo pipefail
: "${OPERATOR_STATE:?set OPERATOR_STATE to the private operator state directory}"
WORK="$OPERATOR_STATE/toolchain/nginx"
BIN="$WORK/root/usr/sbin/nginx"
if [ -x "$BIN" ]; then echo "$BIN"; exit 0; fi

mkdir -p "$WORK/debs" "$WORK/root"
# Release 1.0 supports one proxy: the Ubuntu 22.04 nginx-core binary, extracted user-level.
ATTEMPT="$WORK/debs/nginx-core"
mkdir -p "$ATTEMPT"
(cd "$ATTEMPT" && apt-get download nginx-core >/dev/null 2>&1) \
  || { echo "install_proxy: apt-get download nginx-core failed" >&2; exit 1; }
for deb in "$ATTEMPT"/*.deb; do dpkg-deb -x "$deb" "$WORK/root"; done
[ -x "$BIN" ] || { echo "install_proxy: nginx binary missing after extraction: $BIN" >&2; exit 1; }
echo "$BIN"
