#!/usr/bin/env bash
# Health and restart diagnostics for the operator deployment.
#   health.sh --static   validate rendered config, permissions, units and nginx syntax
#   health.sh            static checks plus systemd/HTTP/TLS checks (does not restart anything)
set -uo pipefail
MODE="${1:-full}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
: "${OPERATOR_STATE:?set OPERATOR_STATE (or run via operatorctl.sh health)}"
FAILURES=0

report() {
  printf '%-8s %s\n' "$1" "$2"
  if [ "$1" = "FAIL" ]; then FAILURES=$((FAILURES + 1)); fi
  return 0
}

if [ ! -f "$OPERATOR_STATE/resolved.env" ]; then
  report FAIL "missing $OPERATOR_STATE/resolved.env (run operatorctl.sh install --env-file FILE)"
  exit 1
fi
# shellcheck disable=SC1090
source "$OPERATOR_STATE/resolved.env"

[ "$(stat -c '%a' "$OPERATOR_STATE")" = "700" ] && report PASS "state dir is 0700" \
  || report FAIL "state dir is not 0700: $OPERATOR_STATE"
[ -f "$OPERATOR_TLS_KEY" ] && [ "$(stat -c '%a' "$OPERATOR_TLS_KEY")" = "600" ] \
  && report PASS "TLS key is 0600: $OPERATOR_TLS_KEY" \
  || report FAIL "TLS key missing or not 0600: $OPERATOR_TLS_KEY"
if [ -n "${ENDPOINT_TOKEN_FILE:-}" ] && [ -f "$ENDPOINT_TOKEN_FILE" ] \
   && [ "$(stat -c '%a' "$ENDPOINT_TOKEN_FILE")" = "600" ]; then
  report PASS "endpoint token file is 0600: $ENDPOINT_TOKEN_FILE"
else
  report FAIL "endpoint token file missing or not 0600: ${ENDPOINT_TOKEN_FILE:-unset}"
fi
if [ -n "${FEISHU_ENV_FILE:-}" ] && [ -f "$FEISHU_ENV_FILE" ] \
   && [ "$(stat -c '%a' "$FEISHU_ENV_FILE")" = "600" ]; then
  report PASS "Feishu SSO env file is 0600: $FEISHU_ENV_FILE"
else
  report FAIL "Feishu SSO env file missing or not 0600: ${FEISHU_ENV_FILE:-unset}"
fi
if [ -n "${FEISHU_APP_SECRET_FILE:-}" ]; then
  [ -f "$FEISHU_APP_SECRET_FILE" ] && [ "$(stat -c '%a' "$FEISHU_APP_SECRET_FILE")" = "600" ] \
    && report PASS "Feishu app secret file is 0600: $FEISHU_APP_SECRET_FILE" \
    || report FAIL "Feishu app secret file missing or not 0600: $FEISHU_APP_SECRET_FILE"
else
  report FAIL "Feishu SSO credentials are not configured (preparation state only; nobody can log in)"
fi
[ -f "$OPERATOR_TLS_CERT" ] && report PASS "TLS certificate: $OPERATOR_TLS_CERT" \
  || report FAIL "TLS certificate missing: $OPERATOR_TLS_CERT"
[ -f "$OPERATOR_NGINX_CONF" ] && report PASS "nginx config: $OPERATOR_NGINX_CONF" \
  || report FAIL "nginx config missing: $OPERATOR_NGINX_CONF"
[ -f "$PORTAL_CONFIG" ] && report PASS "portal config: $PORTAL_CONFIG" \
  || report FAIL "portal config missing: $PORTAL_CONFIG"

if [ -x "${NGINX_BIN:-/usr/sbin/nginx}" ]; then
  if "$NGINX_BIN" -t -c "$OPERATOR_NGINX_CONF" -p "$OPERATOR_STATE/nginx" >/dev/null 2>&1; then
    report PASS "nginx -t"
  else
    report FAIL "nginx -t rejected $OPERATOR_NGINX_CONF"
  fi
fi

TARGET="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
for unit in description-portal.service description-operator-proxy.service; do
  [ -f "$TARGET/$unit" ] && report PASS "unit rendered: $unit" || report FAIL "unit missing: $TARGET/$unit"
done

if [ "$MODE" = "--static" ]; then
  [ "$FAILURES" = "0" ] && echo "STATIC OK" || echo "STATIC FAILURES: $FAILURES"
  exit "$([ "$FAILURES" = "0" ] && echo 0 || echo 1)"
fi

# Authenticated endpoint probe through the tunnel: a bare HTTP response is not health.
if [ -f "${ENDPOINT_TOKEN_FILE:-}" ]; then
  if python3 - "$ENDPOINT_TOKEN_FILE" <<'PY'
import json, sys, urllib.request
token = open(sys.argv[1]).read().strip()
request = urllib.request.Request("http://127.0.0.1:18765/health",
                                 headers={"Authorization": "Bearer " + token})
with urllib.request.urlopen(request, timeout=15) as response:
    body = json.load(response)
raise SystemExit(0 if body.get("ready") and body.get("pipeline_id") == "solidworks-to-urdf" else 1)
PY
  then
    report PASS "endpoint /health authenticated via tunnel + token"
  else
    report FAIL "endpoint /health rejected the token or is not ready"
  fi
fi

# The installed connection must exist with the same host/port, the endpoint token file content and
# the handoff allowlist. Neither the token nor the connection is ever printed.
if [ -x "${AIRFLOW_VENV:-}/bin/python" ] && [ -f "${ENDPOINT_TOKEN_FILE:-}" ]; then
  if AIRFLOW_HOME="${AIRFLOW_HOME:-}" "$AIRFLOW_VENV/bin/python" - \
      "$SOLIDWORKS_HANDOFF_ROOT" "$ENDPOINT_TOKEN_FILE" <<'PY'
import sys
from pathlib import Path

from airflow.models.connection import Connection
from airflow.settings import Session

expected_root = sys.argv[1]
expected_token = Path(sys.argv[2]).read_text(encoding="utf-8").strip()
with Session() as session:
    connection = session.query(Connection).filter(Connection.conn_id == "solidworks_windows").one_or_none()
host_ok = bool(connection) and connection.host == "127.0.0.1" and connection.port == 18765
token_ok = bool(connection) and bool(expected_token) and connection.password == expected_token
roots_ok = bool(connection) and (connection.extra_dejson or {}).get("handoff_roots") == [expected_root]
raise SystemExit(0 if host_ok and token_ok and roots_ok else 1)
PY
  then
    report PASS "connection solidworks_windows matches host/port, the endpoint token file and handoff_roots"
  else
    report FAIL "connection solidworks_windows is missing or differs from the deployment env"
  fi
fi

# The packaged request-context plugin must be discoverable: without it every operator run write
# fails closed, so a missing entry point is a deployment failure, not a warning. Only the plugin
# and middleware names are reported; nothing secret is read or printed.
if [ -x "${AIRFLOW_VENV:-}/bin/python" ]; then
  if AIRFLOW_HOME="${AIRFLOW_HOME:-}" "$AIRFLOW_VENV/bin/python" - <<'PY'
import importlib.metadata as metadata

from airflow import plugins_manager

entries = [
    entry
    for entry in metadata.entry_points(group="airflow.plugins")
    if entry.value.endswith("airflow_plugin:DescriptionPipelinePlugin")
]
middlewares = [item.get("name") for item in plugins_manager.get_fastapi_plugins()[1]]
raise SystemExit(
    0 if entries and "description-pipeline-request-context" in middlewares else 1
)
PY
  then
    report PASS "run-ownership request-context plugin is installed"
  else
    report FAIL "run-ownership request-context plugin is missing (operator run writes stay closed)"
  fi
fi

command -v systemctl >/dev/null 2>&1 || { report FAIL "systemctl unavailable"; exit 1; }
for unit in description-postgres description-airflow-dag-processor description-airflow-scheduler \
            description-airflow-api-server description-portal description-operator-proxy; do
  state="$(systemctl --user is-active "$unit" 2>/dev/null || true)"
  [ "$state" = "active" ] && report PASS "unit active: $unit" || report FAIL "unit is $state: $unit"
done
state="$(systemctl --user is-active description-solidworks-tunnel 2>/dev/null || true)"
[ "$state" = "active" ] && report PASS "unit active: description-solidworks-tunnel" \
  || report FAIL "unit is $state: description-solidworks-tunnel"

if command -v curl >/dev/null 2>&1; then
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 http://127.0.0.1:8791/ || true)"
  case "$code" in
    2??|3??) report PASS "Airflow api-server serves its login entry on 127.0.0.1:8791 (HTTP $code)" ;;
    *) report FAIL "Airflow api-server is not serving its login entry on 127.0.0.1:8791 (HTTP $code)" ;;
  esac
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 http://127.0.0.1:${PORTAL_PORT}/ || true)"
  case "$code" in
    2??|3??) report PASS "portal serves the unauthenticated login entry on 127.0.0.1:${PORTAL_PORT} (HTTP $code)" ;;
    *) report FAIL "portal is not serving its login entry on 127.0.0.1:${PORTAL_PORT} (HTTP $code)" ;;
  esac
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 -k "https://127.0.0.1:${OPERATOR_HTTPS_PORT}/operator-healthz" || true)"
  [ "$code" = "200" ] && report PASS "proxy liveness on https://127.0.0.1:${OPERATOR_HTTPS_PORT}/operator-healthz" \
    || report FAIL "proxy liveness HTTP $code"
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 -k "https://127.0.0.1:${OPERATOR_HTTPS_PORT}/" || true)"
  case "$code" in
    2??|3??) report PASS "operator URL serves the login entry over TLS (HTTP $code)" ;;
    *) report FAIL "operator URL is not serving its login entry over TLS (HTTP $code)" ;;
  esac
fi

# Check the complete current browser module bundle against installed bytes. A
# working login page alone does not establish that the URDF viewer can start.
if [ -x "${AIRFLOW_VENV:-}/bin/python" ]; then
  if ASSET_DIAG="$("$AIRFLOW_VENV/bin/python" - "$PORTAL_PORT" <<'ASSETS'
import hashlib
from pathlib import Path
import sys
from urllib.request import urlopen

import description_pipeline

root = Path(description_pipeline.__file__).resolve().parent / "orchestration" / "static"
try:
    for name in ("app.js", "viewer.js", "vendor/three.module.min.js", "vendor/three.core.min.js"):
        expected = (root / name).read_bytes()
        with urlopen(f"http://127.0.0.1:{sys.argv[1]}/static/{name}", timeout=10) as response:
            if response.status != 200 or response.headers.get_content_type() not in {
                "application/javascript", "text/javascript"
            }:
                raise ValueError(f"{name}: expected JavaScript with HTTP 200")
            actual = response.read(len(expected) + 1)
        if hashlib.sha256(actual).digest() != hashlib.sha256(expected).digest():
            raise ValueError(f"{name}: served bytes differ from the installed bundle")
except (OSError, ValueError) as error:
    print(error)
    raise SystemExit(1)
ASSETS
)"; then
    report PASS "operator browser modules match the installed bundle"
  else
    report FAIL "operator browser modules are unavailable or changed: ${ASSET_DIAG:-probe failed}"
  fi
else
  report FAIL "operator browser modules cannot be checked: installed Python runtime is missing"
fi

# The Feishu auth manager must answer with its JSON contract, and an explicitly unconfigured
# manager is NOT READY: preparation may run without credentials, commissioning may not.
if FEISHU_DIAG="$(python3 - "$OPERATOR_HTTPS_PORT" <<'PY'
import json, ssl, sys, urllib.error, urllib.request

context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
context.check_hostname = False
context.verify_mode = ssl.CERT_NONE
url = f"https://127.0.0.1:{sys.argv[1]}/auth/feishu/health"
try:
    with urllib.request.urlopen(url, timeout=10, context=context) as response:
        status, raw = response.status, response.read()
except urllib.error.HTTPError as error:
    status, raw = error.code, error.read()
except Exception:
    raise SystemExit(1)
try:
    body = json.loads(raw or b"{}")
except ValueError:
    print(f"HTTP {status} non-JSON response")
    raise SystemExit(1)
if status == 200 and body.get("configured") is True:
    raise SystemExit(0)
if status == 503 and body.get("configured") is False:
    print("HTTP 503 " + json.dumps(body, sort_keys=True))
    raise SystemExit(2)
print(f"HTTP {status} " + json.dumps(body, sort_keys=True))
raise SystemExit(1)
PY
)"; then
  report PASS "Feishu SSO health reports configured (200 JSON)"
else
  FEISHU_RC=$?
  if [ "$FEISHU_RC" = "2" ]; then
    report FAIL "Feishu SSO is not ready for commissioning: ${FEISHU_DIAG:-HTTP 503 configured:false}"
  else
    report FAIL "Feishu SSO /auth/feishu/health did not answer the auth-manager JSON contract: ${FEISHU_DIAG:-no response}"
  fi
fi

echo "OPERATOR_URL=${OPERATOR_URL:-unknown}"
[ "$FAILURES" = "0" ] && echo "HEALTH OK" || echo "HEALTH FAILURES: $FAILURES"
exit "$([ "$FAILURES" = "0" ] && echo 0 || echo 1)"
