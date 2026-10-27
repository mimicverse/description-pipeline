# The one data-only environment authority for the installed Linux generation.

die() {
  echo "${OPERATOR_PROGRAM:-operatorctl}: $*" >&2
  exit 1
}

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
  local file="$1"
  shift
  if [ "$#" -eq 0 ]; then
    set -- OPERATOR_STATE AIRFLOW_API_PORT NGINX_BIN FEISHU_ENV_FILE OPERATOR_URL
  fi
  load_env_file "$file" "$@"
}

generation_env_declared_value() {
  local file="$1" key="$2" value
  value="$("$CONTROL_PYTHON" -c '
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])
from render_operator import read_env

value = read_env(Path(sys.argv[2])).get(sys.argv[3], "")
if value == "":
    raise SystemExit(3)
sys.stdout.write(value)
' "$HERE" "$file" "$key")" || {
    code=$?
    if [ "$code" = "3" ]; then
      die "the generation environment $file does not declare $key"
    fi
    die "the generation environment $file has an unreadable $key declaration"
  }
  printf '%s' "$value"
}

verify_snapshot_derived_env() {
  local snapshot_file="$1" install_id="$2" env_file="$3"
  local deployment_root="$4" release_file="$5" wheel_file
  wheel_file="$(generation_env_declared_value "$env_file" PIPELINE_WHEEL)"
  [ -f "$wheel_file" ] || die "the installed wheel is missing: $wheel_file"
  "$CONTROL_PYTHON" - \
    "$deployment_root/deploy/operator/platform_env.py" \
    "$deployment_root/deploy/operator/render_operator.py" \
    "$deployment_root" "$snapshot_file" "$install_id" "$env_file" \
    "$release_file" "$wheel_file" <<'PY' \
    || die "the installed environment does not match its snapshot and release"
import hashlib
import importlib.util
import json
import sys
from pathlib import Path

platform_env_path, render_operator_path = map(Path, sys.argv[1:3])
deployment_root, snapshot_file = map(Path, sys.argv[3:5])
install_id = sys.argv[5]
env_file, release_file, wheel_file = map(Path, sys.argv[6:9])
release_document = json.loads(release_file.read_text(encoding="utf-8"))
files = release_document.get("deployment_files")
if not isinstance(files, dict):
    raise SystemExit("the release manifest has no deployment file identities")
for name in (
    "platform_env.py",
    "platform_config.py",
    "install_record.py",
    "render_operator.py",
    "systemd_units.py",
):
    path = platform_env_path.with_name(name)
    member = path.relative_to(deployment_root).as_posix()
    if path.is_symlink() or not path.is_file():
        raise SystemExit(f"the deployment helper is missing or a link: {member}")
    if files.get(member) != hashlib.sha256(path.read_bytes()).hexdigest():
        raise SystemExit(f"the deployment helper does not match its release: {member}")


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sys.path.insert(0, str(platform_env_path.parent))
platform_env = load_module("current_platform_env", platform_env_path)
render_operator = load_module("current_render_operator", render_operator_path)
install_record = load_module(
    "current_install_record", platform_env_path.with_name("install_record.py")
)
release = install_record.release_identity(release_document)
platform_env.atomic_write_once = lambda path, payload: None
result = platform_env.derive(
    snapshot_file.read_bytes(),
    install_id=install_id,
    snapshot_path=snapshot_file,
    tree_root=deployment_root,
    release=release,
    wheel=wheel_file,
)
actual = render_operator.read_env(env_file)
expected = {key: value for key, value in result["env"].items() if value != ""}
if actual != expected:
    differing = sorted(
        key
        for key in actual.keys() | expected.keys()
        if actual.get(key) != expected.get(key)
    )
    raise SystemExit(
        "the installed environment differs from its source-owned derivation: "
        + ",".join(differing)
    )
PY
}

verify_resolved_env() {
  local generation_env="$1" state_root="$2" verified_env="$3"
  "$CONTROL_PYTHON" - "$HERE" "$state_root" "$generation_env" "$verified_env" \
    "${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/description-operator-proxy.service" <<'PY'
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile

sys.path.insert(0, sys.argv[1])
from render_operator import encode_env_lines, read_env

renderer = Path(sys.argv[1]) / "render_operator.py"
state, generation_env, verified_env, proxy_unit = map(Path, sys.argv[2:6])
values = read_env(generation_env)
actual = read_env(state / "resolved.env")
commands = [
    shlex.split(line.partition("=")[2])
    for line in proxy_unit.read_text(encoding="utf-8").splitlines()
    if line.startswith("ExecStart=")
]
if len(commands) != 1 or not commands[0] or not Path(commands[0][0]).is_absolute():
    raise SystemExit("the installed proxy unit has no unambiguous executable")
binary = commands[0][0]
runtime_env = {
    key: value
    for key, value in os.environ.items()
    if key not in {"PYTHONPATH", "PYTHONHOME", "PYTHONNOUSERSITE"}
}
with tempfile.TemporaryDirectory(prefix="operator-resolved-") as temporary:
    rendered_state = Path(temporary) / "state"
    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            str(renderer),
            "--env-file",
            str(generation_env),
            "--state",
            str(rendered_state),
            "--nginx-bin",
            binary,
            "--json",
        ],
        capture_output=True,
        text=True,
        env=runtime_env,
    )
    if completed.returncode:
        raise SystemExit(
            "the installed renderer could not resolve its environment: "
            + completed.stderr.strip()
        )
    expected = {
        key: value.replace(str(rendered_state), str(state))
        for key, value in json.loads(completed.stdout).items()
        if value != ""
    }
if actual != expected:
    differing = sorted(
        key for key in actual.keys() | expected.keys()
        if actual.get(key) != expected.get(key)
    )
    raise SystemExit(
        "the installed resolved environment differs from its renderer: "
        + ",".join(differing)
    )
verified_env.write_text(
    "\n".join(encode_env_lines(actual.items())) + "\n",
    encoding="utf-8",
)
PY
}

verify_runtime_identity() {
  local runtime_venv="$1" release_file="$2"
  [ -x "$runtime_venv/bin/python" ] \
    || die "the installed generation runtime is missing: $runtime_venv/bin/python"
  env -u PYTHONPATH -u PYTHONHOME -u PYTHONNOUSERSITE \
    "$runtime_venv/bin/python" -I -B - "$release_file" "$runtime_venv" <<'PY' \
    || die "the installed runtime package does not match the current release"
import json
import sys
from pathlib import Path

import description_pipeline
from description_pipeline.delivery import PIPELINE_ID
from description_pipeline.runtime import tool_record

release_file, venv = map(Path, sys.argv[1:3])
release = json.loads(release_file.read_text(encoding="utf-8"))
identity = tool_record()["release"]
package_root = Path(description_pipeline.__file__).resolve().parent
if not package_root.is_relative_to(venv.resolve()):
    raise SystemExit("the installed runtime imports the package outside its own venv")
for key in ("version", "source_commit", "source_sha256"):
    if identity.get(key) != release.get(key):
        raise SystemExit(f"the installed runtime release differs on {key}")
if identity.get("pipeline_id") != PIPELINE_ID:
    raise SystemExit("the installed runtime pipeline identity differs")
PY
}

# Parse and verify the one installed generation before any runtime executable is called.
load_current_environment() {
  local state_root="${1:?the operator state root is required}"
  shift
  local record_file="$state_root/install-record.json" active_output generation_state
  local snapshot_file snapshot_sha verified_env load_status release_file
  local -a values=()
  [ -d "$state_root" ] && [ ! -L "$state_root" ] \
    || die "the operator state root is missing or a link: $state_root"
  [ -f "$record_file" ] && [ ! -L "$record_file" ] \
    || die "the install record is missing or a link: $record_file"
  active_output="$(mktemp)" || die "cannot create the install-record probe"
  if ! "$CONTROL_PYTHON" -c '
import json
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[2])
from platform_config import valid_install_id

record = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
install_id = record.get("install_id") if isinstance(record, dict) else None
snapshot = record.get("snapshot") if isinstance(record, dict) else None
if not valid_install_id(install_id) or not isinstance(snapshot, dict):
    raise SystemExit("the install record has no valid generation")
print(install_id)
print(snapshot.get("path", ""))
print(snapshot.get("digest", ""))
' "$record_file" "$HERE" > "$active_output"; then
    rm -f "$active_output"
    die "the install record is unreadable"
  fi
  mapfile -t values < "$active_output"
  rm -f "$active_output"
  [ "${#values[@]}" -eq 3 ] \
    || die "the install record does not select one generation"
  ACTIVE_ID="${values[0]}"
  snapshot_file="${values[1]}"
  snapshot_sha="${values[2]}"
  [ -f "$snapshot_file" ] && [ ! -L "$snapshot_file" ] \
    || die "the installed snapshot is missing or a link: $snapshot_file"
  [ "$(sha256sum "$snapshot_file" | awk '{print $1}')" = "$snapshot_sha" ] \
    || die "the installed snapshot differs from its record"
  ENV_FILE="$state_root/derived/$ACTIVE_ID/operator.env"
  [ -f "$ENV_FILE" ] && [ ! -L "$ENV_FILE" ] \
    || die "the installed environment is missing or a link: $ENV_FILE"
  generation_state="$(generation_env_declared_value "$ENV_FILE" OPERATOR_STATE)" \
    || die "the installed environment has no unambiguous OPERATOR_STATE"
  [ "$generation_state" = "$state_root" ] \
    || die "the installed environment belongs to another state root: $generation_state"
  AIRFLOW_VENV="$(generation_env_declared_value "$ENV_FILE" AIRFLOW_VENV)" \
    || die "the installed environment has no unambiguous AIRFLOW_VENV"
  AIRFLOW_HOME="$(generation_env_declared_value "$ENV_FILE" AIRFLOW_HOME)" \
    || die "the installed environment has no unambiguous AIRFLOW_HOME"
  release_file="$HERE/../../release.json"
  verify_snapshot_derived_env \
    "$snapshot_file" "$ACTIVE_ID" "$ENV_FILE" "$HERE/../.." "$release_file"
  verified_env="$(mktemp)" || die "cannot create the verified environment snapshot"
  if ! verify_resolved_env "$ENV_FILE" "$state_root" "$verified_env"; then
    rm -f "$verified_env"
    return 1
  fi
  verify_runtime_identity "$AIRFLOW_VENV" "$release_file"
  load_resolved_env "$verified_env" "$@"
  load_status=$?
  rm -f "$verified_env"
  [ "$load_status" -eq 0 ] || return "$load_status"
  [ "${OPERATOR_STATE:-}" = "$state_root" ] \
    || die "the verified environment names another state root: ${OPERATOR_STATE:-unset}"
}
