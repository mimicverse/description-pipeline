#!/usr/bin/env bash
# Health and restart diagnostics for the operator deployment.
#   health.sh --static   validate rendered config, permissions, units and nginx syntax
#   health.sh            static checks plus systemd/HTTP/TLS checks (does not restart anything)
set -uo pipefail
CONTROL_PYTHON="${CONTROL_PYTHON:-python3}"
if ! "$CONTROL_PYTHON" -I -B -c \
    'import sys; raise SystemExit(0 if sys.implementation.name == "cpython" and sys.version_info[:2] == (3, 12) else 1)' \
    2>/dev/null; then
  echo "health: CONTROL_PYTHON must be CPython 3.12" >&2
  exit 1
fi
export CONTROL_PYTHON
MODE="${1:-full}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OPERATOR_PROGRAM=health
source "$HERE/environment.sh"
AIRFLOW_HERE="$HERE/../airflow"
if ! UNIT_MANIFEST="$(bash "$AIRFLOW_HERE/services.sh" units all)"; then
  echo "health: could not read the managed systemd unit manifest" >&2
  exit 1
fi
mapfile -t UNITS <<< "$UNIT_MANIFEST"
[ "${#UNITS[@]}" -gt 0 ] || { echo "health: the managed systemd unit manifest is empty" >&2; exit 1; }
if ! OPERATOR_UNIT_MANIFEST="$(bash "$AIRFLOW_HERE/services.sh" units operator)"; then
  echo "health: could not read the operator systemd unit role" >&2
  exit 1
fi
mapfile -t OPERATOR_UNITS <<< "$OPERATOR_UNIT_MANIFEST"
: "${OPERATOR_STATE:?set OPERATOR_STATE (or run via operatorctl.sh health)}"
FAILURES=0

report() {
  printf '%-8s %s\n' "$1" "$2"
  if [ "$1" = "FAIL" ]; then FAILURES=$((FAILURES + 1)); fi
  return 0
}

if [ ! -f "$OPERATOR_STATE/resolved.env" ]; then
  report FAIL "missing $OPERATOR_STATE/resolved.env (run operatorctl.sh install --config FILE)"
  exit 1
fi
if ! load_current_environment "$OPERATOR_STATE" \
  OPERATOR_STATE OPERATOR_TLS_KEY OPERATOR_TLS_CERT OPERATOR_NGINX_CONF \
  OPERATOR_HTTPS_PORT OPERATOR_URL PORTAL_CONFIG PORTAL_PORT NGINX_BIN \
  AIRFLOW_VENV AIRFLOW_HOME AIRFLOW_API_PORT TUNNEL_LOOPBACK_PORT \
  ENDPOINT_TOKEN_FILE FEISHU_ENV_FILE FEISHU_APP_SECRET_FILE; then
  report FAIL "the active generation environment could not be verified"
  exit 1
fi
# Every port comes from the single deploy source (render_operator.py) via resolved.env.
for key in AIRFLOW_API_PORT TUNNEL_LOOPBACK_PORT PORTAL_PORT OPERATOR_HTTPS_PORT; do
  [ -n "${!key:-}" ] || { report FAIL "resolved.env is missing $key (run install to re-render)"; exit 1; }
done

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

TARGET="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
proxy_issue=""
if ! proxy_issue="$(
  NGINX_BIN="${NGINX_BIN:-}" bash "$HERE/scripts/check_proxy_binary.sh" \
    --unit-file "$TARGET/description-operator-proxy.service" 2>&1
)"; then
  report FAIL "$proxy_issue"
elif "$proxy_issue" -t -c "$OPERATOR_NGINX_CONF" -p "$OPERATOR_STATE/nginx" >/dev/null 2>&1; then
  report PASS "nginx -t"
else
  report FAIL "nginx -t rejected $OPERATOR_NGINX_CONF"
fi

for unit in "${OPERATOR_UNITS[@]}"; do
  [ -f "$TARGET/$unit.service" ] && report PASS "unit rendered: $unit.service" \
    || report FAIL "unit missing: $TARGET/$unit.service"
done

if [ "$MODE" = "--static" ]; then
  [ "$FAILURES" = "0" ] && echo "STATIC OK" || echo "STATIC FAILURES: $FAILURES"
  exit "$([ "$FAILURES" = "0" ] && echo 0 || echo 1)"
fi

# Authenticated endpoint probe through the tunnel: the canonical IF-09 validators decide the
# identity against the active install record; the health check keeps no identity lexemes.
if [ -x "${AIRFLOW_VENV:-}/bin/python" ] && [ -f "${ENDPOINT_TOKEN_FILE:-}" ]; then
  if AIRFLOW_HOME="${AIRFLOW_HOME:-}" "$AIRFLOW_VENV/bin/python" - \
      "$ENDPOINT_TOKEN_FILE" "$TUNNEL_LOOPBACK_PORT" "$OPERATOR_STATE/install-record.json" <<'PY'
import json
import sys
import urllib.request

from description_pipeline import interface_records

token = open(sys.argv[1], encoding="utf-8").read().strip()
request = urllib.request.Request(
    f"http://127.0.0.1:{sys.argv[2]}/health", headers={"Authorization": "Bearer " + token}
)
with urllib.request.urlopen(request, timeout=15) as response:
    health = interface_records.validate_health(json.load(response))
record = json.load(open(sys.argv[3], encoding="utf-8"))
interface_records.require_endpoint_matches_install_record(health, record)
PY
  then
    report PASS "endpoint /health is a validated IF-09 identity matching the active install record"
  else
    report FAIL "endpoint /health rejected the token or failed the canonical IF-09 identity check"
  fi
else
  report FAIL "endpoint identity check cannot run: the deployment interpreter or token file is missing"
fi

# The installed connection is consumed through the same canonical resolver as the DAG:
# exactly one IF-11 reference, resolved to the active immutable snapshot and fully checked
# on this host. A surviving route copy is refused before any dependent service is trusted.
if [ -x "${AIRFLOW_VENV:-}/bin/python" ]; then
  if AIRFLOW_HOME="${AIRFLOW_HOME:-}" "$AIRFLOW_VENV/bin/python" \
      -m description_pipeline.orchestration.airflow_client \
      --conn-id solidworks_windows --state-root "$OPERATOR_STATE"
  then
    report PASS "connection solidworks_windows resolves to the active IF-11 config snapshot"
  else
    report FAIL "connection solidworks_windows is missing, malformed, route-copied, or not the active config identity"
  fi
else
  report FAIL "connection config identity cannot be verified: AIRFLOW_VENV is not executable"
fi

# The actual effective Airflow DAG must come from the active generation's install record,
# not a historical release directory left in airflow.cfg.  The metadata check also requires
# the current serialized Airflow version to carry that exact file location.
if [ -x "${AIRFLOW_VENV:-}/bin/python" ]; then
  if DAG_IDENTITY="$(
      AIRFLOW_HOME="${AIRFLOW_HOME:-}" "$AIRFLOW_VENV/bin/python" "$HERE/runtime_dag.py" \
        --state-root "$OPERATOR_STATE" --airflow-home "$AIRFLOW_HOME" 2>&1
    )"; then
    report PASS "Airflow config, file and serialized DAG identities match the active generation"
  else
    report FAIL "Airflow runtime DAG identity differs from the active generation: ${DAG_IDENTITY:-probe failed}"
  fi
else
  report FAIL "Airflow runtime DAG identity cannot be verified: AIRFLOW_VENV is not executable"
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
else
  # Fail explicitly: an unverifiable guard leaves operator run writes closed.
  report FAIL "run-ownership request-context plugin cannot be verified: AIRFLOW_VENV is not executable"
fi

# The native-stage pool is part of the seven-stage contract: a missing pool or a
# capacity other than 1 is a deployment failure (contracts stage contract / IF-02).
if [ -x "${AIRFLOW_VENV:-}/bin/python" ]; then
  if AIRFLOW_HOME="${AIRFLOW_HOME:-}" "$AIRFLOW_VENV/bin/python" - <<'PY'
import sys

from airflow.models.pool import Pool
from airflow.settings import Session

with Session() as session:
    pool = session.query(Pool).filter(Pool.pool == "windows_native").one_or_none()
    sys.exit(0 if pool is not None and int(pool.slots) == 1 and pool.include_deferred else 1)
PY
  then
    report PASS "native pool windows_native capacity=1 including deferred tasks"
  else
    report FAIL "native pool windows_native must have capacity 1 including deferred tasks"
  fi
else
  report FAIL "native pool cannot be verified: AIRFLOW_VENV is not executable"
fi

# Deferred effects require the pinned triggerer to be both installed and alive. A merely
# active systemd unit is insufficient: Airflow's own TriggererJob heartbeat must be healthy.
if [ -x "${AIRFLOW_VENV:-}/bin/airflow" ]; then
  airflow_version="$(AIRFLOW_HOME="${AIRFLOW_HOME:-}" "$AIRFLOW_VENV/bin/airflow" version 2>/dev/null || true)"
  [ "$airflow_version" = "3.3.2" ] \
    && report PASS "Airflow runtime version is 3.3.2" \
    || report FAIL "Airflow runtime version is not pinned 3.3.2: ${airflow_version:-unknown}"
  if AIRFLOW_HOME="${AIRFLOW_HOME:-}" "$AIRFLOW_VENV/bin/python" - <<'PY'
from airflow.configuration import conf

assert conf.getint("triggerer", "capacity") == 64
assert conf.getfloat("triggerer", "job_heartbeat_sec") == 5.0
assert conf.getfloat("triggerer", "triggerer_health_check_threshold") == 30.0
PY
  then
    report PASS "Airflow triggerer capacity and health thresholds match the frozen contract"
  else
    report FAIL "Airflow triggerer capacity or health thresholds differ from the frozen contract"
  fi
  if AIRFLOW_HOME="${AIRFLOW_HOME:-}" "$AIRFLOW_VENV/bin/airflow" jobs check \
      --job-type TriggererJob --local --limit 1 >/dev/null 2>&1; then
    report PASS "Airflow TriggererJob heartbeat is healthy on this host"
  else
    report FAIL "Airflow TriggererJob is missing or unhealthy on this host"
  fi
else
  report FAIL "Airflow triggerer health cannot be verified: AIRFLOW_VENV/bin/airflow is missing"
fi

command -v systemctl >/dev/null 2>&1 || { report FAIL "systemctl unavailable"; exit 1; }
for unit in "${UNITS[@]}"; do
  state="$(systemctl --user is-active "$unit.service" 2>/dev/null || true)"
  [ "$state" = "active" ] && report PASS "unit active: $unit.service" \
    || report FAIL "unit is $state: $unit.service"
done

if command -v curl >/dev/null 2>&1; then
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "http://127.0.0.1:${AIRFLOW_API_PORT}/" || true)"
  case "$code" in
    2??|3??) report PASS "Airflow api-server serves its login entry on 127.0.0.1:${AIRFLOW_API_PORT} (HTTP $code)" ;;
    *) report FAIL "Airflow api-server is not serving its login entry on 127.0.0.1:${AIRFLOW_API_PORT} (HTTP $code)" ;;
  esac
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 http://127.0.0.1:${PORTAL_PORT}/ || true)"
  case "$code" in
    2??|3??) report PASS "portal serves the unauthenticated login entry on 127.0.0.1:${PORTAL_PORT} (HTTP $code)" ;;
    *) report FAIL "portal is not serving its login entry on 127.0.0.1:${PORTAL_PORT} (HTTP $code)" ;;
  esac
fi

OPERATOR_ORIGIN="${OPERATOR_URL%/}"
if TLS_DIAG="$("$CONTROL_PYTHON" -I -B -S "$HERE/tls_probe.py" \
    --url "$OPERATOR_ORIGIN/operator-healthz" --cafile "$OPERATOR_TLS_CERT" \
    --expect-status 200 2>&1)"; then
  report PASS "proxy liveness passed verified TLS and origin-host identity (${TLS_DIAG})"
else
  report FAIL "proxy liveness failed verified TLS or origin-host identity: ${TLS_DIAG:-no response}"
fi
if TLS_DIAG="$("$CONTROL_PYTHON" -I -B -S "$HERE/tls_probe.py" \
    --url "$OPERATOR_ORIGIN/" --cafile "$OPERATOR_TLS_CERT" 2>&1)"; then
  report PASS "operator URL serves its login entry over verified TLS (${TLS_DIAG})"
else
  report FAIL "operator URL failed verified TLS or origin-host identity: ${TLS_DIAG:-no response}"
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
    for name in ("app.js", "run_history.js", "viewer.js", "vendor/three.module.min.js", "vendor/three.core.min.js"):
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
if FEISHU_DIAG="$("$CONTROL_PYTHON" -I -B -S "$HERE/tls_probe.py" \
    --url "$OPERATOR_ORIGIN/auth/feishu/health" --cafile "$OPERATOR_TLS_CERT" \
    --mode feishu-health 2>&1)"; then
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
