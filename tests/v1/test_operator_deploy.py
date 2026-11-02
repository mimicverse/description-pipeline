"""First-install Linux operator integration tests with external commands isolated."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
import zipfile
from pathlib import Path

from description_pipeline import interface_records, schemas
from description_pipeline.delivery import PIPELINE_ID
from description_pipeline.pipeline.contracts import NativeTargetIdentity
from description_pipeline.stages import CONTRACT, CONTRACT_SHA256

ROOT = Path(__file__).resolve().parents[2]
OPERATOR = ROOT / "deploy" / "operator"
CONTROL = OPERATOR / "operatorctl.sh"
ALL_UNITS = [
    "description-postgres",
    "description-airflow-dag-processor",
    "description-airflow-scheduler",
    "description-airflow-triggerer",
    "description-airflow-api-server",
    "description-solidworks-tunnel",
    "description-portal",
    "description-operator-proxy",
]
AIRFLOW_UNITS = [
    "description-airflow-dag-processor",
    "description-airflow-scheduler",
    "description-airflow-triggerer",
    "description-airflow-api-server",
    "description-solidworks-tunnel",
]


def running_windows_health(identity: dict) -> dict:
    return {
        "schema_version": schemas.HEALTH_SCHEMA,
        "pipeline_id": PIPELINE_ID,
        "native_platform": True,
        "ready": True,
        "release": identity["release"],
        "contract": {
            "schema_version": CONTRACT["schema_version"],
            "digest": CONTRACT_SHA256,
        },
        "resource_policy": {
            "schema_version": schemas.RESOURCE_POLICY_SCHEMA,
            "digest": interface_records.resource_policy_v1_digest(),
        },
        "install_id": identity["install_id"],
        "config_digest": identity["config_digest"],
        "target_identity": identity["target_identity"],
        "endpoint": {"port": identity["port"]},
        "cad": {
            "external_session_detected": False,
            "owned_sessions": 0,
            "lease_held": False,
        },
        "jobs": {"active": 0, "queued": 0, "unresolved": 0},
        "capacity": {"free_bytes": 1, "reserved_bytes": 0},
        "readiness": {
            "reason_code": None,
            "native": {"ok": True, "problems": []},
        },
    }


def run(
    arguments: list[str],
    environment: dict[str, str],
    *,
    cwd: Path = ROOT,
) -> subprocess.CompletedProcess[str]:
    merged = {
        "HOME": str(Path.home()),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "CONTROL_PYTHON": sys.executable,
    }
    merged.update(environment)
    return subprocess.run(
        arguments,
        cwd=cwd,
        env=merged,
        capture_output=True,
        text=True,
        check=False,
    )


def write_stub(path: Path, source: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(source).lstrip(), encoding="utf-8")
    path.chmod(0o755)


def file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_contract_wheel(path: Path) -> None:
    source = Path(
        os.environ.get(
            "FIXTURE_CONTRACT_SOURCE",
            ROOT / "src" / "description_pipeline",
        )
    )
    with zipfile.ZipFile(path, "w") as archive:
        for file in sorted(source.rglob("*")):
            if file.is_file() and "__pycache__" not in file.parts:
                archive.write(
                    file,
                    f"description_pipeline/{file.relative_to(source).as_posix()}",
                )


def create_package_source(root: Path) -> Path:
    package = root / "fixture-package" / "description_pipeline"
    (package / "dynamics_assessment").mkdir(parents=True)
    (package / "dynamics_assessment/__init__.py").write_text("")
    (package / "dynamics_assessment/contracts.py").write_text('DAG_ID = "upper_body_dynamics"\n')
    (package / "orchestration").mkdir(parents=True)
    (package / "platform").mkdir(parents=True)
    (package / "pipeline").mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "delivery.py").write_text(
        f"PIPELINE_ID = {PIPELINE_ID!r}\n",
        encoding="utf-8",
    )
    (package / "runtime.py").write_text(
        """\
import json
import os
from pathlib import Path

from .delivery import PIPELINE_ID


def tool_record():
    release = json.loads(Path(os.environ["FIXTURE_RELEASE_FILE"]).read_text(encoding="utf-8"))
    return {
        "release": {
            "pipeline_id": PIPELINE_ID,
            "version": release["version"],
            "source_commit": release["source_commit"],
            "source_sha256": release["source_sha256"],
        }
    }
""",
        encoding="utf-8",
    )
    (package / "interface_records.py").write_text(
        """\
def validate_install_record(record):
    return record


def validate_config_snapshot(snapshot):
    return snapshot


def validate_health(health):
    required = {
        "schema_version",
        "pipeline_id",
        "native_platform",
        "ready",
        "release",
        "contract",
        "resource_policy",
        "install_id",
        "config_digest",
        "target_identity",
        "endpoint",
        "cad",
        "jobs",
        "capacity",
        "readiness",
    }
    if (
        not isinstance(health, dict)
        or set(health) != required
        or health.get("schema_version") != __HEALTH_SCHEMA__
        or health.get("pipeline_id") != "solidworks-to-urdf"
        or health.get("native_platform") is not True
        or type(health.get("ready")) is not bool
    ):
        raise ValueError("invalid health")
    return health


def require_endpoint_matches_install_record(health, record, *, require_ready=True):
    validate_health(health)
    expected = record["ends"]["windows"]
    if (
        health.get("install_id") != expected.get("install_id")
        or health.get("config_digest") != expected.get("config_digest")
        or health.get("release") != expected.get("release")
        or health.get("target_identity") != expected.get("target_identity")
        or (require_ready and health.get("ready") is not True)
    ):
        raise ValueError("health identity differs")
""".replace("__HEALTH_SCHEMA__", repr(schemas.HEALTH_SCHEMA)),
        encoding="utf-8",
    )
    (package / "pipeline" / "__init__.py").write_text("", encoding="utf-8")
    (package / "pipeline" / "contracts.py").write_text(
        """\
import hashlib
import json


class NativeTargetIdentity:
    def __init__(self, record):
        self.record = dict(record)
        self.install_id = record["install_id"]
        self.pipeline_id = record["pipeline_id"]

    @classmethod
    def from_record(cls, record):
        required = {
            "schema_version",
            "install_id",
            "pipeline_id",
            "target_id",
        }
        if not isinstance(record, dict) or set(record) != required:
            raise ValueError("invalid target identity")
        return cls(record)

    @classmethod
    def create(cls, *, install_id, pipeline_id):
        record = {
            "schema_version": "__NATIVE_TARGET_SCHEMA__",
            "install_id": install_id,
            "pipeline_id": pipeline_id,
        }
        record["target_id"] = hashlib.sha256(
            json.dumps(record, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return cls(record)

    def to_record(self):
        return dict(self.record)
""".replace("__NATIVE_TARGET_SCHEMA__", schemas.NATIVE_TARGET_IDENTITY_SCHEMA),
        encoding="utf-8",
    )
    (package / "orchestration" / "__init__.py").write_text("", encoding="utf-8")
    (package / "orchestration" / "portal.py").write_text("", encoding="utf-8")
    (package / "orchestration" / "feishu_auth.py").write_text("", encoding="utf-8")
    (package / "orchestration" / "airflow_client.py").write_text(
        """\
import os
import sys

order = os.environ.get("ORDER_LOG")
if order:
    with open(order, "a", encoding="utf-8") as handle:
        handle.write("airflow connection\\n")
raise SystemExit(int(os.environ.get("FIXTURE_AIRFLOW_CONNECTION_RC", "0")))
""",
        encoding="utf-8",
    )
    (package / "orchestration" / "lifecycle_state.py").write_text(
        """\
import argparse
import os

parser = argparse.ArgumentParser()
parser.add_argument("action")
parser.add_argument("--require-no-unresolved", action="store_true")
parser.add_argument("--require-settled", action="store_true")
parser.add_argument("--require-admission-closed", action="store_true")
parser.parse_args()
print(os.environ["FIXTURE_LIFECYCLE_JSON"])
""",
        encoding="utf-8",
    )
    (package / "platform" / "__init__.py").write_text("", encoding="utf-8")
    (package / "platform" / "facts.py").write_text(
        """\
import json
import os
from pathlib import Path


class View:
    def __init__(self, state):
        self.state = state
        self.revision = 1


class AdmissionControlStore:
    def __init__(self, connect):
        self.path = Path(os.environ["FIXTURE_ADMISSION"])

    def close_admission(self, reason, actor):
        self.path.write_text("true", encoding="utf-8")
        return View("closed")

    def open_admission(self, reason, actor):
        self.path.write_text("false", encoding="utf-8")
        return View("open")
""",
        encoding="utf-8",
    )
    (root / "fixture-package" / "psycopg2.py").write_text(
        "def connect(*args, **kwargs):\n    return object()\n",
        encoding="utf-8",
    )
    return root / "fixture-package"


def deployment_fixture(root: Path) -> tuple[Path, Path, dict[str, str], dict[str, Path]]:
    release = root / "release"
    operator = release / "deploy" / "operator"
    airflow = release / "deploy" / "airflow"
    windows = release / "deploy" / "windows"
    platform = release / "deploy" / "platform"
    shutil.copytree(OPERATOR, operator)
    airflow.mkdir(parents=True)
    (airflow / "dags").mkdir()
    windows.mkdir(parents=True)
    shutil.copyfile(ROOT / "deploy/windows/instance-lifecycle.ps1", windows / "instance-lifecycle.ps1")
    platform.mkdir()
    shutil.copyfile(ROOT / "deploy/systemd-units.tsv", release / "deploy/systemd-units.tsv")
    package_source = create_package_source(root)

    secrets = root / "secrets"
    secrets.mkdir()
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-sha256",
            "-nodes",
            "-days",
            "1",
            "-subj",
            "/CN=operator.test",
            "-addext",
            "subjectAltName=DNS:operator.test",
            "-keyout",
            str(secrets / "tls.key"),
            "-out",
            str(secrets / "tls.crt"),
        ],
        check=True,
        capture_output=True,
    )
    (secrets / "tls.crt").chmod(0o600)
    (secrets / "tls.key").chmod(0o600)
    (secrets / "feishu.json").write_text('{"app_id":"cli","app_secret":"secret"}\n', encoding="utf-8")
    (secrets / "feishu.json").chmod(0o600)
    (secrets / "postgres.dsn").write_text("fixture\n", encoding="utf-8")
    (secrets / "postgres.dsn").chmod(0o600)
    token = root / "token"
    token.write_text("t" * 40 + "\n", encoding="utf-8")
    token.chmod(0o600)

    state = root / "state"
    runtime_releases = root / "runtime-releases"
    handoffs = root / "handoffs"
    artifacts = root / "artifacts"
    workspaces = root / "workspaces"
    specifications = root / "specifications"
    config = {
        "schema_version": schemas.PLATFORM_CONFIG_SCHEMA,
        "operator": {
            "origin": "https://operator.test:8443",
            "certificate_file": str(secrets / "tls.crt"),
            "private_key_file": str(secrets / "tls.key"),
        },
        "storage": {
            "state_root": str(state),
            "handoff_root": str(handoffs),
            "artifact_root": str(artifacts),
            "workspace_root": str(workspaces),
            "release_root": str(runtime_releases),
        },
        "database": {"dsn_file": str(secrets / "postgres.dsn")},
        "windows": {
            "ssh_host": "windows.test",
            "endpoint_port": 9765,
            "token_file": str(token),
            "instance": {
                "task_name": "description-e2e-endpoint",
                "package_root": "C:/description/packages",
                "output_root": "C:/description/results",
                "state_root": "C:/description/state",
            },
        },
        "auth": {
            "feishu_secret_file": str(secrets / "feishu.json"),
            "tenant_keys": ["tenant"],
            "admin_open_ids": [],
        },
        "targets": {"m3": {"repository_slug": "owner/repository", "base": "main"}},
        "specifications": {"record_roots": [str(specifications)]},
    }
    config_path = root / "platform.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")

    wheel = release / "mimicverse_description-1.0.0-py3-none-any.whl"
    write_contract_wheel(wheel)
    linux = release / "description-1.0.0-linux-cp312-x86_64.zip"
    windows_zip = release / "description-1.0.0-windows-cp312-x86_64.zip"
    deployment_zip = release / "description-1.0.0-deployment.zip"
    with zipfile.ZipFile(linux, "w") as archive:
        archive.writestr(f"wheels/{wheel.name}", wheel.read_bytes())
    windows_zip.write_bytes(b"windows")
    deployment_zip.write_bytes(b"deployment")
    dag = airflow / "dags" / "solidworks_to_urdf.py"
    dag.write_text("# fixture dag\n", encoding="utf-8")

    install_toolchain = operator / "scripts" / "install_toolchain.sh"
    write_stub(
        install_toolchain,
        """\
#!/usr/bin/env bash
set -euo pipefail
"$CONTROL_PYTHON" -m venv --without-pip "$AIRFLOW_VENV"
site="$AIRFLOW_VENV/lib/python3.12/site-packages"
cp -R "$FIXTURE_PACKAGE_SOURCE/description_pipeline" "$site/"
cp "$FIXTURE_PACKAGE_SOURCE/psycopg2.py" "$site/"
write_stub() {
  local path="$1"
  shift
  printf '%s\\n' "$@" > "$path"
  chmod 755 "$path"
}
write_stub "$AIRFLOW_VENV/bin/airflow" '#!/usr/bin/env bash' 'echo "airflow $*" >> "$ORDER_LOG"'
printf 'AIRFLOW_PYTHON=%s\\n' "$AIRFLOW_VENV/bin/python"
""",
    )
    write_stub(
        operator / "scripts" / "ensure_proxy_binary.sh",
        """\
#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$FIXTURE_NGINX"
""",
    )
    write_stub(
        operator / "scripts" / "check_proxy_binary.sh",
        """\
#!/usr/bin/env bash
set -euo pipefail
[ -x "${NGINX_BIN:-}" ] || { echo "nginx missing"; exit 1; }
printf '%s\\n' "$NGINX_BIN"
""",
    )
    write_stub(
        operator / "health.sh",
        """\
#!/usr/bin/env bash
set -euo pipefail
echo "HEALTH OK"
""",
    )
    write_stub(
        operator / "health_gate.sh",
        """\
#!/usr/bin/env bash
set -euo pipefail
echo "health_gate" >> "$ORDER_LOG"
exit "${HEALTH_GATE_RC:-0}"
""",
    )
    write_stub(
        operator / "runtime_dag.py",
        """\
#!/usr/bin/env python3
import sys
import json
import runpy
from pathlib import Path
if "--dag-ids" in sys.argv:
    state = Path(sys.argv[sys.argv.index("--state-root") + 1])
    record = json.loads((state / "install-record.json").read_bytes())
    snapshot = json.loads(Path(record["snapshot"]["path"]).read_bytes())
    reader = runpy.run_path(__RUNTIME_DAG_SOURCE__)
    print("\\n".join(reader["enabled_dag_ids"](snapshot)))
else:
    print("runtime-dag-ok")
""".replace("__RUNTIME_DAG_SOURCE__", repr(str(ROOT / "deploy/operator/runtime_dag.py"))),
    )
    (operator / "reconcile-effect.py").write_text(
        """\
import argparse
import json

parser = argparse.ArgumentParser()
parser.add_argument("--run-id", required=True)
parser.add_argument("--stage", required=True)
parser.add_argument("--effect-key", required=True)
args = parser.parse_args()
print(json.dumps({"run_id": args.run_id, "stage": args.stage, "effect_key": args.effect_key}))
""",
        encoding="utf-8",
    )

    write_stub(
        airflow / "services.sh",
        """\
#!/usr/bin/env bash
set -euo pipefail
case "${1:-}" in
  units)
    case "${2:-}" in
      all) printf '%s\\n' \\
        description-postgres \\
        description-airflow-dag-processor \\
        description-airflow-scheduler \\
        description-airflow-triggerer \\
        description-airflow-api-server \\
        description-solidworks-tunnel \\
        description-portal \\
        description-operator-proxy ;;
      airflow) printf '%s\\n' \\
        description-airflow-dag-processor \\
        description-airflow-scheduler \\
        description-airflow-triggerer \\
        description-airflow-api-server \\
        description-solidworks-tunnel ;;
      executor) printf '%s\\n' \\
        description-airflow-dag-processor \\
        description-airflow-scheduler \\
        description-airflow-triggerer ;;
      database) echo description-postgres ;;
      operator) printf '%s\\n' description-portal description-operator-proxy ;;
      *) exit 2 ;;
    esac ;;
  render)
    target="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
    mkdir -p "$target"
    for unit in \\
      description-postgres \\
      description-airflow-dag-processor \\
      description-airflow-scheduler \\
      description-airflow-triggerer \\
      description-airflow-api-server \\
      description-solidworks-tunnel; do
      printf 'fixture %s\\n' "$unit" > "$target/$unit.service"
    done ;;
  start|stop|stop-executors)
    echo "airflow_services $1" >> "$ORDER_LOG" ;;
  *) exit 2 ;;
esac
""",
    )
    write_stub(
        airflow / "install.sh",
        """\
#!/usr/bin/env bash
set -euo pipefail
echo "airflow_install" >> "$ORDER_LOG"
""",
    )
    (airflow / "scripts").mkdir()
    write_stub(
        airflow / "scripts" / "install_postgres.sh",
        """\
#!/usr/bin/env bash
set -euo pipefail
mkdir -p "$POSTGRES_ROOT/data" "$POSTGRES_ROOT/socket"
printf '14\\n' > "$POSTGRES_ROOT/data/PG_VERSION"
bin="$POSTGRES_ROOT/root/usr/lib/postgresql/14/bin"
mkdir -p "$bin"
printf '%s\\n' \\
  '#!/usr/bin/env bash' \\
  'set -euo pipefail' \\
  'echo postgres_psql >> "$ORDER_LOG"' \\
  'case "$*" in *pg_database*) exit 0 ;; esac' > "$bin/psql"
printf '%s\\n' \\
  '#!/usr/bin/env bash' \\
  'set -euo pipefail' \\
  'echo postgres_createdb >> "$ORDER_LOG"' \\
  ': > "$FIXTURE_DB_CREATED"' > "$bin/createdb"
printf '%s\\n' \\
  '#!/usr/bin/env bash' \\
  'set -euo pipefail' \\
  'echo postgres_pg_isready >> "$ORDER_LOG"' \\
  'exit 0' > "$bin/pg_isready"
chmod 755 "$bin/psql" "$bin/createdb" "$bin/pg_isready"
""",
    )
    write_stub(
        airflow / "scripts" / "add_connection.py",
        """\
#!/usr/bin/env python3
import os
import sys
from pathlib import Path

args = sys.argv[1:]
record = Path(args[args.index("--record-file") + 1])
if not record.is_file():
    raise SystemExit("prepared install record missing")
if os.environ.get("FIXTURE_CONNECTION_FAILURE") == "1":
    raise SystemExit("injected Airflow connection binding failure")
print("connection bound")
""",
    )
    write_stub(
        platform / "apply.sh",
        """\
#!/usr/bin/env bash
set -euo pipefail
echo "platform_apply $*" >> "$ORDER_LOG"
""",
    )
    (operator / "scripts" / "install_proxy.sh").unlink(missing_ok=True)

    release_document = {
        "schema_version": "solidworks-to-urdf.release/v1",
        "pipeline_id": PIPELINE_ID,
        "version": "1.0.0",
        "source_commit": "a" * 40,
        "source_tree": "b" * 40,
        "source_sha256": "c" * 64,
        "artifacts": {
            wheel.name: file_digest(wheel),
            linux.name: file_digest(linux),
            windows_zip.name: file_digest(windows_zip),
            deployment_zip.name: file_digest(deployment_zip),
        },
        "deployment_files": {
            path.relative_to(release).as_posix(): file_digest(path)
            for path in sorted((release / "deploy").rglob("*"))
            if path.is_file()
        },
    }
    release_file = release / "release.json"
    release_file.write_text(json.dumps(release_document), encoding="utf-8")

    bins = root / "bin"
    bins.mkdir()
    nginx = bins / "nginx"
    write_stub(nginx, "#!/usr/bin/env bash\nexit 0\n")
    order = root / "order.log"
    unit_state = root / "unit-state"
    unit_state.mkdir()
    admission = root / "admission"
    admission.write_text("true", encoding="utf-8")
    db_created = root / "database-created"
    pgbin = state / "airflow-pg" / "root" / "usr" / "lib" / "postgresql" / "14" / "bin"
    # The install script creates these later; write their final behavior once the directory exists.
    simple_systemctl = bins / "systemctl"
    write_stub(
        simple_systemctl,
        """\
#!/usr/bin/env bash
set -euo pipefail
echo "systemctl $*" >> "$ORDER_LOG"
action="${2:-}"
if [ "${1:-}" = "--user" ]; then action="${2:-}"; else action="${1:-}"; fi
case "$action" in
  is-active)
    unit="${!#}"
    if [ -n "${SYSTEMCTL_UNKNOWN_UNIT:-}" ] && [ "$unit" = "$SYSTEMCTL_UNKNOWN_UNIT" ]; then
      echo unknown
      exit 4
    fi
    if [ -f "$SYSTEMCTL_STATE_DIR/$unit" ]; then
      echo active
    else
      echo inactive
      exit 3
    fi ;;
  start|enable)
    for unit in "$@"; do
      case "$unit" in description-*.service) : > "$SYSTEMCTL_STATE_DIR/$unit" ;; esac
    done ;;
  stop|disable)
    for unit in "$@"; do
      if [ -n "${SYSTEMCTL_DISABLE_FAIL_UNIT:-}" ] && [ "$unit" = "$SYSTEMCTL_DISABLE_FAIL_UNIT" ]; then
        exit 9
      fi
    done
    for unit in "$@"; do
      case "$unit" in description-*.service) rm -f "$SYSTEMCTL_STATE_DIR/$unit" ;; esac
    done ;;
  daemon-reload) ;;
esac
""",
    )
    windows_target = NativeTargetIdentity.create(
        install_id="win-" + "1" * 32,
        pipeline_id=PIPELINE_ID,
    ).to_record()
    windows_identity_file = root / "windows-identity.json"
    windows_identity_file.write_text(
        json.dumps(
            {
                "install_id": windows_target["install_id"],
                "config_digest": "9" * 64,
                "port": 9765,
                "release": {
                    "version": "1.0.0",
                    "source_commit": "a" * 40,
                    "source_sha256": "c" * 64,
                    "role": "native",
                },
                "target_identity": windows_target,
            }
        ),
        encoding="utf-8",
    )
    write_stub(
        bins / "ssh",
        """\
#!/usr/bin/env python3
import base64
import gzip
import json
import os
import re
import sys
from pathlib import Path

payload = sys.stdin.buffer.read().strip()
script = gzip.decompress(base64.b64decode(payload)).decode("utf-8")
if "New-Item -ItemType Directory" in script and "instance-lifecycle.ps1" not in script:
    raise SystemExit(0)
match = re.search(r"-Verb (\\w+) -Binding '([^']+)'", script)
if match is None:
    raise SystemExit("missing lifecycle call")
verb, raw_binding = match.groups()
identity = json.loads(Path(os.environ["FIXTURE_WINDOWS_IDENTITY"]).read_text(encoding="utf-8"))
listener = {"listener_pid": 42}
document = {
    "schema_version": "solidworks-to-urdf.windows-lifecycle-result/v1",
    "passed": True,
    "verb": verb,
    "identity": identity,
    "task_state": "Running" if verb in {"install", "start", "status", "health"} else "Disabled",
}
if verb == "status":
    document.update({
        "task": {
            "task_name": "description-e2e-endpoint",
            "task_path": "\\\\URDF\\\\",
            "state": "Disabled",
        },
        "listener": None,
        "health": None,
        "status": {
            "consistent": True,
            "task_exists": True,
            "task_state": "Disabled",
            "error": None,
            "solidworks_processes": 0,
        },
    })
elif verb in {"start", "drain", "health"}:
    document["task"] = {
        "task_name": "description-e2e-endpoint",
        "task_path": "\\\\URDF\\\\",
        "state": "Running",
    }
    document["listener"] = listener
    document["health"] = {
        "schema_version": __HEALTH_SCHEMA__,
        "pipeline_id": "solidworks-to-urdf",
        "native_platform": True,
        "ready": True,
        "release": identity["release"],
        "contract": {
            "schema_version": __CONTRACT_SCHEMA__,
            "digest": __CONTRACT_SHA256__,
        },
        "resource_policy": {
            "schema_version": __RESOURCE_POLICY_SCHEMA__,
            "digest": __POLICY_DIGEST__,
        },
        "install_id": identity["install_id"],
        "config_digest": identity["config_digest"],
        "target_identity": identity["target_identity"],
        "endpoint": {"port": 9765},
        "cad": {
            "external_session_detected": False,
            "owned_sessions": 0,
            "lease_held": False,
        },
        "jobs": {"active": 0, "queued": 0, "unresolved": 0},
        "capacity": {"free_bytes": 1, "reserved_bytes": 0},
        "readiness": {
            "reason_code": None,
            "native": {"ok": True, "problems": []},
        },
    }
    if verb == "drain":
        document["safety"] = {
            "jobs": {"active": 0, "queued": 0, "unresolved": 0},
            "cad": {
                "external_session_detected": False,
                "owned_sessions": 0,
                "lease_held": False,
                "solidworks_processes": 0,
            },
            "listener_pid": 42,
            "task_state": "Running",
        }
elif verb == "stop":
    document["task"] = {
        "task_name": "description-e2e-endpoint",
        "task_path": "\\\\URDF\\\\",
        "state": "Disabled",
    }
    document["listener"] = None
    document["health"] = None
    document["safety"] = {
        "jobs": None,
        "cad": {
            "external_session_detected": False,
            "owned_sessions": 0,
            "lease_held": False,
            "solidworks_processes": 0,
        },
        "listener_pid": None,
        "task_state": "Disabled",
        "source": "already_stopped",
    }
    document["stopped"] = {
        "listener_count": 0,
        "writer_count": 0,
        "solidworks_process_count": 0,
    }
if verb == "health" and os.environ.get("FIXTURE_WINDOWS_HEALTH_FAILURE") == "1":
    document = {
        "schema_version": "solidworks-to-urdf.windows-lifecycle-result/v1",
        "passed": False,
        "verb": "health",
        "code": "health_failed",
    }
order = os.environ.get("ORDER_LOG")
if order:
    with open(order, "a", encoding="utf-8") as handle:
        handle.write("windows " + verb + "\\n")
override_path = os.environ.get("FIXTURE_WINDOWS_RESULT")
if override_path:
    override = json.loads(Path(override_path).read_text(encoding="utf-8"))
    document = (
        override
        if isinstance(override, dict) and "schema_version" in override
        else override.get(verb, override)
        if isinstance(override, dict)
        else override
    )
print(json.dumps(document))
"""
        .replace("__HEALTH_SCHEMA__", repr(schemas.HEALTH_SCHEMA))
        .replace("__CONTRACT_SCHEMA__", repr(CONTRACT["schema_version"]))
        .replace("__CONTRACT_SHA256__", repr(CONTRACT_SHA256))
        .replace("__RESOURCE_POLICY_SCHEMA__", repr(schemas.RESOURCE_POLICY_SCHEMA))
        .replace("__POLICY_DIGEST__", repr(interface_records.resource_policy_v1_digest())),
    )
    environment = {
        "PATH": f"{bins}:{Path(sys.executable).parent}:{os.environ.get('PATH', '/usr/bin:/bin')}",
        "XDG_CONFIG_HOME": str(root / "config"),
        "ORDER_LOG": str(order),
        "SYSTEMCTL_STATE_DIR": str(unit_state),
        "FIXTURE_PACKAGE_SOURCE": str(package_source),
        "FIXTURE_RELEASE_FILE": str(release_file),
        "FIXTURE_ADMISSION": str(admission),
        "FIXTURE_LIFECYCLE_JSON": json.dumps(
            {"admission": {"state": "closed"}, "settled": True}
        ),
        "FIXTURE_NGINX": str(nginx),
        "FIXTURE_WINDOWS_IDENTITY": str(windows_identity_file),
        "FIXTURE_DB_CREATED": str(db_created),
        "FIXTURE_PGBIN": str(pgbin),
    }
    paths = {
        "config": config_path,
        "release": release,
        "release_file": release_file,
        "state": state,
        "runtime_releases": runtime_releases,
        "handoffs": handoffs,
        "artifacts": artifacts,
        "workspaces": workspaces,
        "specifications": specifications,
        "source_wheel": wheel,
        "order": order,
        "admission": admission,
        "unit_state": unit_state,
    }
    return config_path, release, environment, paths


class FirstInstallTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        config, release, environment, paths = deployment_fixture(self.root)
        self.config = config
        self.release = release
        self.environment = environment
        self.paths = paths
        self.control = release / "deploy" / "operator" / "operatorctl.sh"

    def _command(self, verb: str, *arguments: str) -> list[str]:
        return ["bash", str(self.control), verb, "--config", str(self.config), *arguments]

    def _install(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return run(
            self._command(
                "install",
                "--install-id",
                "gen-1",
                "--release-file",
                str(self.paths["release_file"]),
                *arguments,
            ),
            self.environment,
        )

    def _enable_assessment(self):
        config = json.loads(self.config.read_bytes())
        config["operator"]["assessment"] = {
            "enabled": True, "python": "/opt/assessment/bin/python", "capacity": 3,
            "limits": {"wall_seconds": 60, "cpu_seconds": 60,
                       "memory_bytes": 2147483648, "output_bytes": 16777216},
        }
        self.config.write_text(json.dumps(config))

    def test_enabled_assessment_uses_the_same_start_keep_closed_and_drain_boundary(self):
        self._enable_assessment()
        installed = self._install()
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
        for keep_closed in (True, False):
            self.paths["order"].write_text("")
            options = ("--keep-admission-closed",) if keep_closed else ()
            started = run(self._command("start", *options), self.environment)
            self.assertEqual(started.returncode, 0, started.stdout + started.stderr)
            order = self.paths["order"].read_text()
            for dag_id in ("solidworks_to_urdf", "upper_body_dynamics"):
                self.assertIn("airflow dags pause " + dag_id, order)
                self.assertEqual("airflow dags unpause " + dag_id in order, not keep_closed)
        self.paths["order"].write_text("")
        drained = run(self._command("drain"), self.environment)
        self.assertEqual(drained.returncode, 0, drained.stdout + drained.stderr)
        for dag_id in ("solidworks_to_urdf", "upper_body_dynamics"):
            self.assertIn("airflow dags pause " + dag_id, self.paths["order"].read_text())

    def test_auxiliary_unpause_failure_repauses_both_dags_and_keeps_admission_closed(self):
        self._enable_assessment()
        installed = self._install()
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
        python = self.paths["state"] / "derived/gen-1/operator.env"
        from tests.v1.test_platform_env import render_operator

        airflow = Path(render_operator.read_env(python)["AIRFLOW_VENV"]) / "bin/airflow"
        write_stub(airflow, '#!/bin/bash\necho "airflow $*" >> "$ORDER_LOG"\n'
                   'if [ "$*" = "dags unpause upper_body_dynamics" ]; then exit 8; fi\n')
        self.paths["order"].write_text("")
        started = run(self._command("start"), self.environment)
        self.assertNotEqual(started.returncode, 0, started.stdout + started.stderr)
        order = self.paths["order"].read_text()
        self.assertIn("airflow dags unpause solidworks_to_urdf", order)
        failure = order.index("airflow dags unpause upper_body_dynamics")
        self.assertIn("airflow dags pause solidworks_to_urdf", order[failure:])
        self.assertIn("airflow dags pause upper_body_dynamics", order[failure:])
        self.assertEqual(self.paths["admission"].read_text(), "true")

    def test_clean_install_binds_one_current_record_and_windows_identity(self) -> None:
        installed = self._install()
        detail = installed.stdout + installed.stderr
        for path in sorted((self.paths["state"] / "lifecycle").glob("*/windows-health.out*")):
            if path.is_file():
                detail += f"\n== {path.name} ==\n{path.read_text(encoding='utf-8', errors='replace')}"
        if self.paths["order"].is_file():
            detail += "\n" + self.paths["order"].read_text(encoding="utf-8")
        for name in ("postgres_psql", "airflow_install", "platform_apply", "connection bound"):
            if name == "postgres_psql":
                continue
            self.assertIn(name, detail)
        self.assertEqual(installed.returncode, 0, detail)
        record = json.loads((self.paths["state"] / "install-record.json").read_text(encoding="utf-8"))
        self.assertNotIn("previous", record)
        self.assertNotIn("journal", record)
        self.assertEqual(record["install_id"], "gen-1")
        self.assertEqual(record["ends"]["linux"]["config_digest"], record["snapshot"]["digest"])
        self.assertEqual(
            record["ends"]["windows"]["install_id"],
        "win-" + "1" * 32,
        )
        identities = list((self.paths["state"] / "lifecycle").glob("*/windows-endpoint-identity.json"))
        self.assertEqual(len(identities), 1)

    def test_portal_service_delivers_the_complete_production_environment(self) -> None:
        installed = self._install()
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
        unit = self.environment["XDG_CONFIG_HOME"] + "/systemd/user/description-portal.service"
        probe = self.root / "portal_environment_probe.py"
        probe.write_text(
            textwrap.dedent(
                """\
                import json
                import os
                import sys
                from pathlib import Path
                from types import SimpleNamespace
                from unittest.mock import patch

                from description_pipeline.orchestration import portal
                from description_pipeline.pipeline.contracts import NativeTargetIdentity

                target = NativeTargetIdentity.create(
                    install_id="win-" + "1" * 32,
                    pipeline_id="solidworks-to-urdf",
                )
                application = SimpleNamespace(artifacts=object())
                with (
                    patch(
                        "description_pipeline.pipeline.native_target.load_registered_native_target",
                        return_value=target,
                    ),
                    patch(
                        "description_pipeline.pipeline.application.application_from_environment",
                        return_value=application,
                    ),
                ):
                    config = portal.load_portal_config(Path(sys.argv[1]))
                print(json.dumps({
                    "artifact_root": os.environ["PLATFORM_ARTIFACT_ROOT"],
                    "config_snapshot": str(config.production_config_snapshot),
                    "pipeline_work_root": os.environ["PIPELINE_WORK_ROOT"],
                    "platform_snapshot": os.environ["PLATFORM_CONFIG_SNAPSHOT"],
                    "application_loaded": config.production_application is application,
                }, sort_keys=True))
                """
            ),
            encoding="utf-8",
        )
        runner = self.root / "portal-service-entry.sh"
        write_stub(
            runner,
            """\
                #!/usr/bin/env bash
                set -euo pipefail
                unit="$1"
                while IFS= read -r assignment; do
                  export "${assignment#Environment=}"
                done < <(sed -n 's/^Environment=//p' "$unit")
                while IFS= read -r env_file; do
                  [ -f "$env_file" ] || exit 11
                  set -a
                  source "$env_file"
                  set +a
                done < <(sed -n 's/^EnvironmentFile=//p' "$unit")
                config="$(sed -n 's/^ExecStart=.*--config //p' "$unit")"
                exec "$PROBE_PYTHON" -B "$PROBE_SCRIPT" "$config"
            """,
        )
        result = subprocess.run(
            ["bash", str(runner), unit],
            env={
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "PROBE_PYTHON": sys.executable,
                "PROBE_SCRIPT": str(probe),
                "PYTHONPATH": str(ROOT / "src"),
                "PYTHONDONTWRITEBYTECODE": "1",
            },
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        delivered = json.loads(result.stdout)
        record = json.loads(
            (self.paths["state"] / "install-record.json").read_text(encoding="utf-8")
        )
        self.assertEqual(delivered["platform_snapshot"], record["snapshot"]["path"])
        self.assertEqual(delivered["config_snapshot"], record["snapshot"]["path"])
        self.assertEqual(delivered["artifact_root"], str(self.paths["artifacts"]))
        self.assertEqual(delivered["pipeline_work_root"], str(self.paths["workspaces"]))
        self.assertIs(delivered["application_loaded"], True)
        self.assertTrue(self.paths["artifacts"].is_dir())
        self.assertTrue(self.paths["workspaces"].is_dir())

    def test_runtime_wheel_is_stable_after_the_source_wheel_is_removed(self) -> None:
        installed = self._install()
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
        operator_env = self.paths["state"] / "derived" / "gen-1" / "operator.env"
        values = dict(
            line.split("=", 1)
            for line in operator_env.read_text(encoding="utf-8").splitlines()
            if "=" in line
        )
        stable = Path(values["PIPELINE_WHEEL"])
        expected = self.paths["runtime_releases"] / "artifacts" / self.paths["source_wheel"].name
        self.assertEqual(stable, expected)
        self.assertEqual(stable.read_bytes(), self.paths["source_wheel"].read_bytes())
        self.paths["source_wheel"].unlink()
        for unit in ALL_UNITS:
            (self.paths["unit_state"] / f"{unit}.service").write_text("active\n", encoding="utf-8")
        status = run(self._command("status"), self.environment)
        self.assertEqual(status.returncode, 0, status.stdout + status.stderr)

    def test_install_refuses_unknown_state_before_writing(self) -> None:
        self.paths["state"].mkdir()
        (self.paths["state"] / "unknown").write_text("residue", encoding="utf-8")
        installed = self._install()
        self.assertNotEqual(installed.returncode, 0)
        self.assertIn("unknown residual state", installed.stderr)
        self.assertFalse((self.paths["state"] / "snapshots").exists())
        self.assertFalse((self.paths["state"] / "lifecycle").exists())

    def test_install_refuses_unknown_business_residue(self) -> None:
        for key, label in (
            ("handoffs", "handoff root"),
            ("artifacts", "artifact root"),
            ("workspaces", "workspace root"),
        ):
            with self.subTest(root=label):
                self.paths[key].mkdir()
                sentinel = self.paths[key] / "unknown"
                sentinel.write_text("residue", encoding="utf-8")
                installed = self._install()
                self.assertNotEqual(installed.returncode, 0, installed.stdout + installed.stderr)
                self.assertIn(f"{label} contains unknown residual state", installed.stderr)
                self.assertFalse((self.paths["state"] / "install-record.json").exists())
                sentinel.unlink()
                self.paths[key].rmdir()

    def test_install_refuses_an_unreadable_business_root(self) -> None:
        self.paths["artifacts"].mkdir()
        self.paths["artifacts"].chmod(0)
        try:
            installed = self._install()
        finally:
            self.paths["artifacts"].chmod(0o700)
        self.assertNotEqual(installed.returncode, 0, installed.stdout + installed.stderr)
        self.assertIn("artifact root could not be inspected", installed.stderr)
        self.assertFalse((self.paths["state"] / "install-record.json").exists())

    def test_install_treats_nonempty_specification_roots_as_declared_inputs(self) -> None:
        self.paths["specifications"].mkdir()
        record = self.paths["specifications"] / "records.json"
        record.write_text('{"declared":true}\n', encoding="utf-8")
        installed = self._install()
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
        self.assertEqual(record.read_text(encoding="utf-8"), '{"declared":true}\n')

    def test_install_uses_the_shared_lifecycle_lock(self) -> None:
        lock_dir = self.paths["state"] / "locks"
        lock_dir.mkdir(parents=True)
        lock_file = lock_dir / "maintenance.lock"
        holder = subprocess.Popen(
            [
                sys.executable,
                "-c",
                (
                    "import fcntl, sys, time\n"
                    "handle = open(sys.argv[1], 'a+', encoding='utf-8')\n"
                    "fcntl.flock(handle, fcntl.LOCK_EX)\n"
                    "print('ready', flush=True)\n"
                    "time.sleep(30)\n"
                ),
                str(lock_file),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            self.assertIsNotNone(holder.stdout)
            self.assertEqual(holder.stdout.readline().strip(), "ready")
            installed = self._install()
            self.assertNotEqual(installed.returncode, 0, installed.stdout + installed.stderr)
            self.assertIn("another supported lifecycle operation owns the lock", installed.stderr)
            self.assertFalse((self.paths["state"] / "snapshots").exists())
        finally:
            holder.terminate()
            holder.wait(timeout=5)
            if holder.stdout is not None:
                holder.stdout.close()
            if holder.stderr is not None:
                holder.stderr.close()

    def test_failed_windows_identity_stops_the_partial_database(self) -> None:
        Path(self.environment["FIXTURE_WINDOWS_IDENTITY"]).unlink()
        installed = self._install()
        self.assertNotEqual(installed.returncode, 0)
        self.assertFalse(
            (self.paths["unit_state"] / "description-postgres.service").exists()
        )
        self.assertFalse((self.paths["state"] / "install-record.json").exists())
        retried = self._install()
        self.assertNotEqual(retried.returncode, 0)
        self.assertIn("unknown residual state", retried.stderr)

    def test_connection_failure_does_not_publish_the_active_install_record(self) -> None:
        self.environment["FIXTURE_CONNECTION_FAILURE"] = "1"
        installed = self._install()
        self.assertNotEqual(installed.returncode, 0, installed.stdout + installed.stderr)
        self.assertIn("before the active install record was published", installed.stderr)
        self.assertFalse((self.paths["state"] / "install-record.json").exists())
        self.assertFalse((self.paths["state"] / "install-records").exists())

    def test_install_refuses_a_tampered_deployment_member_before_writes(self) -> None:
        target = self.paths["release"] / "deploy" / "operator" / "health.sh"
        target.write_bytes(target.read_bytes() + b"\n# unbound deployment member\n")
        installed = self._install()
        self.assertNotEqual(installed.returncode, 0, installed.stdout + installed.stderr)
        self.assertIn("deployment members differ", installed.stderr)
        self.assertFalse(self.paths["state"].exists())
        self.assertFalse(any(self.paths["unit_state"].iterdir()))

    def test_start_drain_stop_use_current_generation_only(self) -> None:
        installed = self._install()
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
        started = run(self._command("start"), self.environment)
        self.assertEqual(started.returncode, 0, started.stdout + started.stderr)
        self.assertEqual(self.paths["admission"].read_text(encoding="utf-8"), "false")
        start_order = self.paths["order"].read_text(encoding="utf-8")
        self.assertLess(start_order.index("health_gate"), start_order.index("windows health"))
        self.assertLess(start_order.index("windows health"), start_order.index("airflow dags unpause"))
        identity = json.loads(Path(self.environment["FIXTURE_WINDOWS_IDENTITY"]).read_text(encoding="utf-8"))
        windows_results = self.root / "bound-shutdown-results.json"
        windows_results.write_text(
            json.dumps(
                {
                    verb: {
                        "schema_version": "solidworks-to-urdf.windows-lifecycle-result/v1",
                        "passed": True,
                        "verb": verb,
                        "task": {
                            "task_name": "description-e2e-endpoint",
                            "task_path": "\\\\URDF\\\\",
                            "state": "Running" if verb == "drain" else "Disabled",
                        },
                        "identity": identity,
                        "listener": {"listener_pid": 42} if verb == "drain" else None,
                        "health": running_windows_health(identity) if verb == "drain" else None,
                        "safety": {
                            "jobs": {"active": 0, "queued": 0, "unresolved": 0},
                            "cad": {
                                "external_session_detected": False,
                                "owned_sessions": 0,
                                "lease_held": False,
                                "solidworks_processes": 0,
                            },
                            "listener_pid": 42,
                            "task_state": "Running",
                        },
                        "stopped": (
                            {
                                "listener_count": 0,
                                "writer_count": 0,
                                "solidworks_process_count": 0,
                            }
                            if verb == "stop"
                            else None
                        ),
                    }
                    for verb in ("drain", "stop")
                }
            ),
            encoding="utf-8",
        )
        self.environment["FIXTURE_WINDOWS_RESULT"] = str(windows_results)
        drained = run(self._command("drain"), self.environment)
        self.assertEqual(drained.returncode, 0, drained.stdout + drained.stderr)
        self.assertEqual(self.paths["admission"].read_text(encoding="utf-8"), "true")
        stopped = run(
            self._command("stop", "--keep-database"),
            self.environment,
        )
        self.assertEqual(stopped.returncode, 0, stopped.stdout + stopped.stderr)
        order = self.paths["order"].read_text(encoding="utf-8")
        self.assertIn("airflow_services start", order)
        self.assertIn("airflow_services stop-executors", order)
        self.assertNotIn("promote", order)
        self.assertNotIn("rollback", order)

    def test_stop_accepts_an_already_stopped_windows_endpoint(self) -> None:
        installed = self._install()
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
        identity = json.loads(Path(self.environment["FIXTURE_WINDOWS_IDENTITY"]).read_text(encoding="utf-8"))
        identity.pop("target_identity")
        result_path = self.root / "already-stopped.json"
        result_path.write_text(
            json.dumps(
                {
                    "schema_version": "solidworks-to-urdf.windows-lifecycle-result/v1",
                    "passed": True,
                    "verb": "stop",
                    "task": {
                        "task_name": "description-e2e-endpoint",
                        "task_path": "\\\\URDF\\\\",
                        "state": "Disabled",
                    },
                    "identity": identity,
                    "listener": None,
                    "health": None,
                    "safety": {
                        "jobs": None,
                        "cad": {
                            "external_session_detected": False,
                            "owned_sessions": 0,
                            "lease_held": False,
                            "solidworks_processes": 0,
                        },
                        "listener_pid": None,
                        "task_state": "Disabled",
                        "source": "already_stopped",
                    },
                    "stopped": {
                        "listener_count": 0,
                        "writer_count": 0,
                        "solidworks_process_count": 0,
                    },
                }
            ),
            encoding="utf-8",
        )
        self.environment["FIXTURE_WINDOWS_RESULT"] = str(result_path)
        stopped = run(
            self._command("stop", "--keep-database"),
            self.environment,
        )
        self.assertEqual(stopped.returncode, 0, stopped.stdout + stopped.stderr)

    def test_stop_fails_when_an_operator_unit_remains_active(self) -> None:
        installed = self._install()
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
        started = run(self._command("start"), self.environment)
        self.assertEqual(started.returncode, 0, started.stdout + started.stderr)
        drained = run(self._command("drain"), self.environment)
        self.assertEqual(drained.returncode, 0, drained.stdout + drained.stderr)
        self.environment["SYSTEMCTL_DISABLE_FAIL_UNIT"] = "description-portal.service"
        stopped = run(self._command("stop", "--keep-database"), self.environment)
        self.assertNotEqual(stopped.returncode, 0, stopped.stdout + stopped.stderr)
        self.assertIn("description-portal.service is still active after stop", stopped.stderr)
        self.assertTrue(
            (self.paths["unit_state"] / "description-portal.service").exists()
        )
        self.assertFalse(
            (self.paths["unit_state"] / "description-operator-proxy.service").exists()
        )

    def test_stop_fails_closed_for_an_unknown_unit_state(self) -> None:
        installed = self._install()
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
        started = run(self._command("start"), self.environment)
        self.assertEqual(started.returncode, 0, started.stdout + started.stderr)
        drained = run(self._command("drain"), self.environment)
        self.assertEqual(drained.returncode, 0, drained.stdout + drained.stderr)
        self.environment["SYSTEMCTL_UNKNOWN_UNIT"] = "description-operator-proxy.service"
        stopped = run(self._command("stop", "--keep-database"), self.environment)
        self.assertNotEqual(stopped.returncode, 0, stopped.stdout + stopped.stderr)
        self.assertIn("description-operator-proxy.service is still unknown after stop", stopped.stderr)

    def test_start_health_failure_keeps_dag_paused_and_admission_closed(self) -> None:
        installed = self._install()
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
        self.paths["order"].write_text("", encoding="utf-8")
        self.environment["HEALTH_GATE_RC"] = "9"
        started = run(self._command("start"), self.environment)
        self.assertNotEqual(started.returncode, 0, started.stdout + started.stderr)
        self.assertEqual(self.paths["admission"].read_text(encoding="utf-8"), "true")
        order = self.paths["order"].read_text(encoding="utf-8")
        self.assertIn("airflow dags pause", order)
        self.assertIn("health_gate", order)
        self.assertNotIn("airflow dags unpause", order)

    def test_start_keep_closed_runs_full_health_and_never_unpauses(self) -> None:
        installed = self._install()
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
        self.paths["order"].write_text("", encoding="utf-8")
        started = run(
            self._command("start", "--keep-admission-closed"),
            self.environment,
        )
        self.assertEqual(started.returncode, 0, started.stdout + started.stderr)
        self.assertEqual(self.paths["admission"].read_text(encoding="utf-8"), "true")
        order = self.paths["order"].read_text(encoding="utf-8")
        self.assertIn("airflow connection", order)
        self.assertIn("health_gate", order)
        self.assertIn("windows health", order)
        self.assertNotIn("airflow dags unpause", order)

    def test_start_connection_failure_keeps_admission_closed(self) -> None:
        installed = self._install()
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
        self.paths["order"].write_text("", encoding="utf-8")
        self.environment["FIXTURE_AIRFLOW_CONNECTION_RC"] = "7"
        for options in ((), ("--keep-admission-closed",)):
            with self.subTest(options=options):
                self.paths["order"].write_text("", encoding="utf-8")
                started = run(self._command("start", *options), self.environment)
                self.assertNotEqual(started.returncode, 0, started.stdout + started.stderr)
                self.assertEqual(self.paths["admission"].read_text(encoding="utf-8"), "true")
                self.assertNotIn("airflow dags unpause", self.paths["order"].read_text(encoding="utf-8"))

    def test_start_rechecks_windows_health_after_the_linux_gate(self) -> None:
        installed = self._install()
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
        self.paths["order"].write_text("", encoding="utf-8")
        self.environment["FIXTURE_WINDOWS_HEALTH_FAILURE"] = "1"
        started = run(self._command("start"), self.environment)
        self.assertNotEqual(started.returncode, 0, started.stdout + started.stderr)
        self.assertEqual(self.paths["admission"].read_text(encoding="utf-8"), "true")
        order = self.paths["order"].read_text(encoding="utf-8")
        self.assertLess(order.index("health_gate"), order.index("windows health"))
        self.assertNotIn("airflow dags unpause", order)

    def test_status_and_health_are_read_only(self) -> None:
        installed = self._install()
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
        for unit in ALL_UNITS:
            (self.paths["unit_state"] / f"{unit}.service").write_text("active\n", encoding="utf-8")
        record_before = (self.paths["state"] / "install-record.json").read_bytes()
        status = run(self._command("status"), self.environment)
        self.assertEqual(status.returncode, 0, status.stdout + status.stderr)
        health = run(self._command("health"), self.environment)
        self.assertEqual(health.returncode, 0, health.stdout + health.stderr)
        self.assertTrue(list((self.paths["state"] / "lifecycle").glob("windows-health-*")))
        self.assertEqual((self.paths["state"] / "install-record.json").read_bytes(), record_before)

    def test_health_refuses_windows_lifecycle_evidence_without_health(self) -> None:
        installed = self._install()
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
        for unit in ALL_UNITS:
            (self.paths["unit_state"] / f"{unit}.service").write_text("active\n", encoding="utf-8")
        identity = json.loads(Path(self.environment["FIXTURE_WINDOWS_IDENTITY"]).read_text(encoding="utf-8"))
        result_path = self.root / "windows-health-missing.json"
        result_path.write_text(
            json.dumps(
                {
                    "schema_version": "solidworks-to-urdf.windows-lifecycle-result/v1",
                    "passed": True,
                    "verb": "health",
                    "task": {"state": "Running"},
                    "identity": identity,
                    "listener": {"listener_pid": 42},
                    "health": None,
                }
            ),
            encoding="utf-8",
        )
        self.environment["FIXTURE_WINDOWS_RESULT"] = str(result_path)
        health = run(self._command("health"), self.environment)
        self.assertNotEqual(health.returncode, 0, health.stdout + health.stderr)
        self.assertIn("Windows health was refused", health.stderr)

    def test_status_rejects_untrusted_windows_endpoint_evidence(self) -> None:
        installed = self._install()
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
        for unit in ALL_UNITS:
            (self.paths["unit_state"] / f"{unit}.service").write_text("active\n", encoding="utf-8")
        identity = json.loads(Path(self.environment["FIXTURE_WINDOWS_IDENTITY"]).read_text(encoding="utf-8"))
        unrelated_target = NativeTargetIdentity.create(
            install_id="win-" + "2" * 32,
            pipeline_id=PIPELINE_ID,
        ).to_record()
        unrelated_identity = {
            **identity,
            "install_id": unrelated_target["install_id"],
            "target_identity": unrelated_target,
        }
        wrong_health_schema = running_windows_health(identity)
        wrong_health_schema["schema_version"] = "unexpected-health/v1"
        running_health = running_windows_health(identity)
        running_health["cad"]["owned_sessions"] = 1
        running_status = {
            "schema_version": "solidworks-to-urdf.windows-lifecycle-result/v1",
            "passed": True,
            "verb": "status",
            "task": {"state": "Running"},
            "identity": identity,
            "listener": {"listener_pid": 42},
            "health": running_health,
            "status": {
                "consistent": True,
                "task_exists": True,
                "task_state": "Running",
                "error": None,
                "solidworks_processes": 1,
            },
        }
        cases = {
            "run2-mismatch": {
                "schema_version": "solidworks-to-urdf.windows-lifecycle-result/v1",
                "passed": True,
                "verb": "status",
                "task": {"state": "Running"},
                "identity": None,
                "listener": None,
                "health": None,
                "status": {
                    "consistent": False,
                    "task_exists": True,
                    "task_state": "Running",
                    "error": {"code": "install_receipt_mismatch"},
                    "solidworks_processes": None,
                },
            },
            "missing-identity": {
                "schema_version": "solidworks-to-urdf.windows-lifecycle-result/v1",
                "passed": True,
                "verb": "status",
                "task": {"state": "Running"},
                "identity": None,
                "listener": {"listener_pid": 42},
                "health": {"ready": True},
                "status": {
                    "consistent": True,
                    "task_exists": True,
                    "task_state": "Running",
                    "error": None,
                    "solidworks_processes": 0,
                },
            },
            "projection-mismatch": {
                "schema_version": "solidworks-to-urdf.windows-lifecycle-result/v1",
                "passed": True,
                "verb": "status",
                "task": {"state": "Running"},
                "identity": identity,
                "listener": None,
                "health": None,
                "status": {
                    "consistent": True,
                    "task_exists": True,
                    "task_state": "Disabled",
                    "error": None,
                    "solidworks_processes": 0,
                },
            },
            "running-listener": {
                **running_status,
                "listener": None,
            },
            "running-health": {
                **running_status,
                "health": None,
            },
            "stopped-listener": {
                "schema_version": "solidworks-to-urdf.windows-lifecycle-result/v1",
                "passed": True,
                "verb": "status",
                "task": {"state": "Disabled"},
                "identity": identity,
                "listener": {"listener_pid": 42},
                "health": None,
                "status": {
                    "consistent": True,
                    "task_exists": True,
                    "task_state": "Disabled",
                    "error": None,
                    "solidworks_processes": 0,
                },
            },
            "stopped-health": {
                "schema_version": "solidworks-to-urdf.windows-lifecycle-result/v1",
                "passed": True,
                "verb": "status",
                "task": {"state": "Ready"},
                "identity": identity,
                "listener": None,
                "health": running_windows_health(identity),
                "status": {
                    "consistent": True,
                    "task_exists": True,
                    "task_state": "Ready",
                    "error": None,
                    "solidworks_processes": 0,
                },
            },
            "status-error": {
                "schema_version": "solidworks-to-urdf.windows-lifecycle-result/v1",
                "passed": True,
                "verb": "status",
                "task": {"state": "Disabled"},
                "identity": identity,
                "listener": None,
                "health": None,
                "status": {
                    "consistent": True,
                    "task_exists": True,
                    "task_state": "Disabled",
                    "error": {"code": "install_receipt_mismatch"},
                    "solidworks_processes": 0,
                },
            },
            "record-mismatch": {
                "schema_version": "solidworks-to-urdf.windows-lifecycle-result/v1",
                "passed": True,
                "verb": "status",
                "task": {"state": "Disabled"},
                "identity": unrelated_identity,
                "listener": None,
                "health": None,
                "status": {
                    "consistent": True,
                    "task_exists": True,
                    "task_state": "Disabled",
                    "error": None,
                    "solidworks_processes": 0,
                },
            },
            "lifecycle-schema": {
                "schema_version": "unexpected-lifecycle/v1",
                "passed": True,
                "verb": "status",
                "task": {"state": "Disabled"},
                "identity": identity,
                "listener": None,
                "health": None,
                "status": {
                    "consistent": True,
                    "task_exists": True,
                    "task_state": "Disabled",
                    "error": None,
                    "solidworks_processes": 0,
                },
            },
            "health-schema": {
                **running_status,
                "health": wrong_health_schema,
            },
            "process-invalid": {
                "schema_version": "solidworks-to-urdf.windows-lifecycle-result/v1",
                "passed": True,
                "verb": "status",
                "task": {"state": "Disabled"},
                "identity": identity,
                "listener": None,
                "health": None,
                "status": {
                    "consistent": True,
                    "task_exists": True,
                    "task_state": "Disabled",
                    "error": None,
                    "solidworks_processes": -1,
                },
            },
        }
        result_path = self.root / "windows-status-result.json"
        self.environment["FIXTURE_WINDOWS_RESULT"] = str(result_path)
        for label, document in cases.items():
            with self.subTest(case=label):
                result_path.write_text(json.dumps(document), encoding="utf-8")
                status = run(self._command("status"), self.environment)
                self.assertNotEqual(status.returncode, 0, status.stdout + status.stderr)
                self.assertIn("Windows status was refused", status.stderr)

    def test_status_accepts_running_windows_evidence_bound_to_the_record(self) -> None:
        installed = self._install()
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
        for unit in ALL_UNITS:
            (self.paths["unit_state"] / f"{unit}.service").write_text("active\n", encoding="utf-8")
        identity = json.loads(Path(self.environment["FIXTURE_WINDOWS_IDENTITY"]).read_text(encoding="utf-8"))
        health = running_windows_health(identity)
        health["cad"]["owned_sessions"] = 1
        result_path = self.root / "windows-running-status.json"
        result_path.write_text(
            json.dumps(
                {
                    "schema_version": "solidworks-to-urdf.windows-lifecycle-result/v1",
                    "passed": True,
                    "verb": "status",
                    "task": {"state": "Running"},
                    "identity": identity,
                    "listener": {"listener_pid": 42},
                    "health": health,
                    "status": {
                        "consistent": True,
                        "task_exists": True,
                        "task_state": "Running",
                        "error": None,
                        "solidworks_processes": 1,
                    },
                }
            ),
            encoding="utf-8",
        )
        self.environment["FIXTURE_WINDOWS_RESULT"] = str(result_path)
        status = run(self._command("status"), self.environment)
        self.assertEqual(status.returncode, 0, status.stdout + status.stderr)

        ready_identity = {
            key: value for key, value in identity.items() if key != "target_identity"
        }
        result_path.write_text(
            json.dumps(
                {
                    "schema_version": "solidworks-to-urdf.windows-lifecycle-result/v1",
                    "passed": True,
                    "verb": "status",
                    "task": {"state": "Ready"},
                    "identity": ready_identity,
                    "listener": None,
                    "health": None,
                    "status": {
                        "consistent": True,
                        "task_exists": True,
                        "task_state": "Ready",
                        "error": None,
                        "solidworks_processes": 0,
                    },
                }
            ),
            encoding="utf-8",
        )
        ready = run(self._command("status"), self.environment)
        self.assertEqual(ready.returncode, 0, ready.stdout + ready.stderr)

    def test_start_rejects_a_successful_windows_result_without_identity(self) -> None:
        installed = self._install()
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
        identity = json.loads(Path(self.environment["FIXTURE_WINDOWS_IDENTITY"]).read_text(encoding="utf-8"))
        result_path = self.root / "windows-start-no-identity.json"
        result_path.write_text(
            json.dumps(
                {
                    "schema_version": "solidworks-to-urdf.windows-lifecycle-result/v1",
                    "passed": True,
                    "verb": "start",
                    "task": {"state": "Running"},
                    "identity": None,
                    "listener": {"listener_pid": 42},
                    "health": running_windows_health(identity),
                }
            ),
            encoding="utf-8",
        )
        self.environment["FIXTURE_WINDOWS_RESULT"] = str(result_path)
        started = run(self._command("start"), self.environment)
        self.assertNotEqual(started.returncode, 0, started.stdout + started.stderr)
        self.assertIn("Windows start was refused", started.stderr)

    def test_reconcile_runs_only_current_business_effect(self) -> None:
        installed = self._install()
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
        result = run(
            self._command(
                "reconcile",
                "--run-id",
                "run-1",
                "--stage",
                "capture",
                "--effect-key",
                "native-capture",
            ),
            self.environment,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('"run_id": "run-1"', result.stdout)

    def test_known_resolved_values_cannot_execute(self) -> None:
        installed = self._install()
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
        resolved = self.paths["state"] / "resolved.env"
        original = resolved.read_bytes()
        marker = self.root / "hook-ran"
        hook = self.root / "hook"
        write_stub(hook, f"#!/usr/bin/env bash\\ntouch {marker}\\n")
        for key, value in (("NGINX_BIN", hook), ("AIRFLOW_VENV", self.root / "rogue-venv")):
            if key == "AIRFLOW_VENV":
                write_stub(value / "bin" / "python", f"#!/usr/bin/env bash\\ntouch {marker}\\n")
            resolved.write_text(
                "\n".join(
                    f"{key}={value}"
                    if line.startswith(f"{key}=")
                    else line
                    for line in original.decode("utf-8").splitlines()
                )
                + "\n",
                encoding="utf-8",
            )
            result = run(self._command("status"), self.environment)
            resolved.write_bytes(original)
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("resolved environment differs", result.stderr)
            self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
