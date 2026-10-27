# Read controlled environment declarations as data through the deployment's one parser.
# Resolved files and tool output use explicit consumer projections; generation files are
# loaded only after their complete source-owned derivation has been verified.
load_env_file() {
  local file="$1" output key value
  shift
  output="$(mktemp)" || return 1
  if ! "$CONTROL_PYTHON" - "$HERE" "$file" "$@" > "$output" <<'PY'
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])
from render_operator import read_env

values = read_env(Path(sys.argv[2]))
keys = sys.argv[3:] or values
for key in keys:
    if key in values:
        sys.stdout.buffer.write(key.encode() + b"\0" + values[key].encode() + b"\0")
PY
  then
    rm -f "$output"
    return 1
  fi
  for key in "$@"; do unset "$key"; done
  while IFS= read -r -d '' key && IFS= read -r -d '' value; do
    printf -v "$key" '%s' "$value"
    export "$key"
  done < "$output"
  rm -f "$output"
}

load_resolved_env() {
  load_env_file "$1" AIRFLOW_API_PORT NGINX_BIN FEISHU_ENV_FILE OPERATOR_URL
}
