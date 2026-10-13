#!/usr/bin/env python3
"""Derive the internal operator environment from the config snapshot.

The private platform.json snapshot is the single configuration authority; the env consumers
(``render_operator.py``, ``install.sh``, ``health.sh``) read the derived keys this module emits,
so the unified ``operatorctl --config`` entry never asks a human to duplicate configuration.

Derivations (all paths under the configured storage roots; converged here as the single table):
  AIRFLOW_VENV   = <release_root>/airflow-venv-<install_id>
  AIRFLOW_HOME   = <state_root>/airflow-home
  POSTGRES_ROOT  = <state_root>/airflow-pg
  AIRFLOW_DB_URL = the Airflow migration identity (airflow_migrate: DDL inside the airflow schema
  only, DML on the bootstrap-created migration ledger)
  PIPELINE_WHEEL = the installed wheel (explicit ``--wheel`` or the single ``*.whl`` under the
  deployment tree), bound to a release artifact by name+digest before any leg runs
  PLATFORM_SERVICE_DSN / PLATFORM_OPERATOR_DSN = the platform store and read roles over the
  deployment PostgreSQL socket - the operator role carries SELECT on the two platform views
  and nothing else; both roles are authenticated by the deployment pg_hba
  MODEL_* / DISCOVERY files = derived JSON under <state_root>/derived (0600)

The derived env file is written 0600 under <state_root>/derived and is an internal artifact of
the lifecycle, never an external interface.

Every emit runs the canonical IF-16 contract validation from the contract closure carried
inside the digest-bound release wheel and refuses an invalid or tampered snapshot before the
first write.  The validator executes in an isolated ``-I -S -B`` child process with a cleared
environment, so ambient site hooks, sys.path shadowing or a cached import in the caller can
never substitute the validators; there is no local mirror and no venv/service/checkout
dependency.

Exit codes: 0 success; 1 error (message on stderr); 2 usage error.
"""

from __future__ import annotations

import argparse
import atexit
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))

from platform_config import atomic_write_once, canonical  # noqa: E402  (shared helpers)
from install_record import release_identity, wheel_binding  # noqa: E402  (single release/asset binding)
from render_operator import PORT_DEFAULTS  # noqa: E402  (single deploy-layer port source, DEP-01)

#: The minimal same-source contract closure inside the release wheel: the schema owner modules
#: plus the stage contract they read.  These files are the frozen interface validators the
#: bootstrap runs itself; the rest of the package is not needed before a venv exists.
CLOSURE_MEMBERS = (
    "__init__.py",
    "schemas.py",
    "io.py",
    "delivery.py",
    "stages.py",
    "stage-contract.json",
    "interface_records.py",
)


def die(message: str) -> None:
    raise SystemExit(f"platform_env: {message}")


def require(condition: bool, message: str) -> None:
    if not condition:
        die(message)


#: The validator script executed by the isolated child interpreter.  It holds no rules of its
#: own: it parses the stdin bytes with the closure's canonical parse entry (``io.parse_data``,
#: whose duplicate-key and encoding strictness is what the binding source enforces) and runs
#: the frozen validator over the parsed document.  It reports a machine-readable verdict on
#: stdout plus an exit code - 0 accept, 2 reject (parse or validation refusal); anything else
#: is an infrastructure failure and fails the gate closed.
ISOLATED_VALIDATION = """\
import json
import pathlib
import sys

(closure,) = sys.argv[1:]
sys.path.insert(0, closure)
from description_pipeline import interface_records
from description_pipeline import io as contract_io

source = pathlib.Path(interface_records.__file__).resolve()
if not source.is_relative_to(pathlib.Path(closure).resolve()):
    print(f"the contract closure identity check failed: {source}", file=sys.stderr)
    sys.exit(1)
try:
    snapshot = contract_io.parse_data(sys.stdin.buffer.read(), pathlib.Path("<config-snapshot>"))
    interface_records.validate_config_snapshot(snapshot)
except contract_io.PipelineError as error:
    print(json.dumps({"verdict": "reject", "error": str(error)}))
    sys.exit(2)
print(json.dumps({"verdict": "accept"}))
"""


def extract_contract_closure(wheel: Path) -> Path:
    """Extract the bound wheel's minimal contract closure into a private directory.

    The directory lives under the system temporary root (never inside the deployment tree)
    and is removed at process exit.  A wheel missing any closure member is refused - the
    gate never falls back to weaker local checks.
    """
    directory = Path(tempfile.mkdtemp(prefix="description-contract-"))
    atexit.register(shutil.rmtree, directory, ignore_errors=True)
    package = directory / "description_pipeline"
    package.mkdir()
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        for member in CLOSURE_MEMBERS:
            name = f"description_pipeline/{member}"
            require(name in names, f"the release artifact {wheel.name} misses the contract closure member {name}")
            (package / member).write_bytes(archive.read(name))
    return directory


def validate_snapshot_via_closure(wheel: Path, snapshot_bytes: bytes) -> None:
    """Run the canonical snapshot validation in a true ``-I -S`` child process.

    The child starts with a cleared environment and no site initialization, and imports the
    validators only from the closure extracted out of the digest-bound wheel: the caller's
    sys.modules, sys.path and ambient site hooks cannot shadow them.  It receives the
    snapshot's exact bytes on stdin - nothing is re-serialized.  An un-runnable child, a
    non-zero exit without a machine-readable verdict or a missing verdict refuses the gate
    (fail closed).
    """
    directory = extract_contract_closure(wheel)
    try:
        completed = subprocess.run(
            [sys.executable, "-I", "-S", "-B", "-c", ISOLATED_VALIDATION, str(directory)],
            input=snapshot_bytes,
            capture_output=True,
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as error:
        die(f"the isolated contract validation could not run: {error}")
    verdict = None
    if completed.returncode in (0, 2):
        try:
            verdict = json.loads(completed.stdout.decode("utf-8").strip().splitlines()[-1])
        except (IndexError, ValueError):
            verdict = None
    if completed.returncode == 0 and isinstance(verdict, dict) and verdict.get("verdict") == "accept":
        return
    if completed.returncode == 2 and isinstance(verdict, dict) and verdict.get("verdict") == "reject":
        die(f"snapshot invalid: {verdict.get('error') or 'config_snapshot_invalid'}")
    stderr = completed.stderr.decode("utf-8", "replace").strip()
    tail = stderr.splitlines()[-1] if stderr else f"exit code {completed.returncode}"
    die(f"the isolated contract validation failed ({completed.returncode}): {tail}")


def _text(value, where: str) -> str:
    require(isinstance(value, str) and bool(value), f"{where} must be non-empty text")
    require(not any(character in value for character in "\n\r"), f"{where} must be a single line")
    return value


def _absolute(value, where: str) -> str:
    value = _text(value, where)
    require(value.startswith("/") and ".." not in value.split("/"), f"{where} must be an absolute path")
    return value


def _origin_parts(origin: str, where: str) -> tuple[str, int]:
    parts = urlsplit(origin)
    require(
        parts.scheme == "https" and bool(parts.hostname) and not parts.path.strip("/") and not parts.query,
        f"{where} must be a bare https origin",
    )
    port = parts.port or 443
    require(0 < port < 65536, f"{where} port must be 1-65535")
    return parts.hostname, port


def derive(
    snapshot_bytes: bytes,
    *,
    install_id: str,
    tree_root: Path,
    release: dict,
    wheel: Path | None = None,
) -> dict:
    """Return ``{env, env_path, derived_dir, wheel}`` for one install generation.

    ``snapshot_bytes`` are the exact bytes of the config snapshot document - the same bytes
    the isolated validator consumes, so nothing is re-serialized between the parse here and
    the canonical gate.
    """

    try:
        snapshot = json.loads(snapshot_bytes)
    except ValueError as error:
        die(f"snapshot is not a valid JSON document: {error}")
    require(bool(install_id) and all(character not in install_id for character in "\n\r/"), "install_id is malformed")
    require(
        isinstance(release, dict) and "artifacts" in release,
        "the release identity is required to bind the consumed wheel",
    )
    wheel_path = wheel_binding(release, wheel=wheel, tree_root=tree_root)
    # The canonical IF-16 validation runs before the emit's first write, enforced by a true
    # -I -S -B child process over the closure extracted from the digest-bound wheel: the
    # caller's environment, sys.path and sys.modules cannot substitute the validators, and an
    # un-runnable or unverifiable child fails the gate closed.
    validate_snapshot_via_closure(wheel_path, snapshot_bytes)
    declared = snapshot["install_id"]
    require(
        declared == install_id,
        f"snapshot generation mismatch: the snapshot declares {declared!r}, the request is {install_id!r}",
    )
    resolved = snapshot["resolved"]
    storage = resolved["storage"]
    windows = resolved["windows"]
    operator = resolved["operator"]
    auth = resolved["auth"]
    targets = resolved["targets"]
    derived = resolved["derived"]

    state_root = _absolute(storage.get("state_root"), "resolved.storage.state_root")
    release_root = _absolute(storage.get("release_root"), "resolved.storage.release_root")
    handoff_root = _absolute(storage.get("handoff_root"), "resolved.storage.handoff_root")
    store_root = _absolute(derived.get("store_root"), "resolved.derived.store_root")
    require(
        store_root.startswith(state_root.rstrip("/") + "/"),
        "resolved.derived.store_root must live under the state root",
    )
    host, https_port = _origin_parts(
        _text(operator.get("origin"), "resolved.operator.origin"),
        "resolved.operator.origin",
    )
    _absolute(operator.get("certificate_file"), "resolved.operator.certificate_file")
    _absolute(operator.get("private_key_file"), "resolved.operator.private_key_file")
    ssh_host = _text(windows.get("ssh_host"), "resolved.windows.ssh_host")
    endpoint_port = windows.get("endpoint_port")
    tunnel_port = windows.get("tunnel_loopback_port")
    require(type(endpoint_port) is int and 0 < endpoint_port < 65536, "resolved.windows.endpoint_port is not a port")
    require(type(tunnel_port) is int and 0 < tunnel_port < 65536, "resolved.windows.tunnel_loopback_port is not a port")
    token_file = _absolute(windows.get("token_file"), "resolved.windows.token_file")
    feishu_secret = _absolute(auth.get("feishu_secret_file"), "resolved.auth.feishu_secret_file")
    tenant_keys = auth.get("tenant_keys")
    admin_open_ids = auth.get("admin_open_ids")
    require(isinstance(tenant_keys, list) and tenant_keys, "resolved.auth.tenant_keys is empty")
    require(isinstance(admin_open_ids, list), "resolved.auth.admin_open_ids is missing")
    require(isinstance(targets, dict) and bool(targets), "resolved.targets is empty")
    repositories = derived.get("repositories")
    discovery = derived.get("discovery")
    require(isinstance(repositories, dict) and bool(repositories), "resolved.derived.repositories is empty")
    require(isinstance(discovery, dict), "resolved.derived.discovery is missing")

    # Derived artifacts bind to their immutable installation generation: a candidate preparation
    # writes only inside <state_root>/derived/<install_id>/ and never edits an older generation's
    # referenced bytes; active reference switching belongs to promote/rollback alone.
    derived_dir = Path(state_root) / "derived" / install_id
    repositories_file = derived_dir / "repositories.json"
    targets_file = derived_dir / "targets.json"
    discovery_file = derived_dir / "discovery.json"
    env_path = derived_dir / "operator.env"

    venv = f"{release_root.rstrip('/')}/airflow-venv-{install_id}"
    home = f"{state_root.rstrip('/')}/airflow-home"
    postgres_root = f"{state_root.rstrip('/')}/airflow-pg"
    postgres_port = PORT_DEFAULTS["POSTGRES_PORT"]
    env = {
        "OPERATOR_HOST": host,
        "OPERATOR_HTTPS_PORT": str(https_port),
        "OPERATOR_BIND": "0.0.0.0",
        "OPERATOR_STATE": state_root,
        "OPERATOR_TLS_CERT": _absolute(operator.get("certificate_file"), "resolved.operator.certificate_file"),
        "OPERATOR_TLS_KEY": _absolute(operator.get("private_key_file"), "resolved.operator.private_key_file"),
        "AIRFLOW_VENV": venv,
        "AIRFLOW_HOME": home,
        "POSTGRES_ROOT": postgres_root,
        "POSTGRES_MAJOR": "14",
        "POSTGRES_PORT": str(postgres_port),
        "AIRFLOW_DB_URL": (
            f"postgresql+psycopg2://airflow_migrate@/airflow_meta?host={postgres_root}/socket&port={postgres_port}"
        ),
        "AIRFLOW_RUNTIME_DB_URL": (
            f"postgresql+psycopg2://airflow_runtime@/airflow_meta?host={postgres_root}/socket&port={postgres_port}"
        ),
        "PLATFORM_SERVICE_DSN": (
            f"postgresql://platform_service@/airflow_meta?host={postgres_root}/socket&port={postgres_port}"
        ),
        "PLATFORM_OPERATOR_DSN": (
            f"postgresql://operator_ro@/airflow_meta?host={postgres_root}/socket&port={postgres_port}"
        ),
        "PIPELINE_WHEEL": str(wheel_path),
        "PIPELINE_STORE_ROOT": store_root,
        "SOLIDWORKS_SSH_HOST": ssh_host,
        "SOLIDWORKS_ENDPOINT_PORT": str(endpoint_port),
        "TUNNEL_LOOPBACK_PORT": str(tunnel_port),
        "ENDPOINT_TOKEN_FILE": token_file,
        "SOLIDWORKS_HANDOFF_ROOT": handoff_root,
        "FEISHU_APP_SECRET_FILE": feishu_secret,
        "FEISHU_TENANT_KEYS": ",".join(tenant_keys),
        "FEISHU_ADMIN_OPEN_IDS": ",".join(admin_open_ids),
        "MODEL_REPOSITORIES_FILE": str(repositories_file),
        "MODEL_TARGETS_FILE": str(targets_file),
        "DISCOVERY_SETTINGS_FILE": str(discovery_file),
    }
    lines = [f"{key}={value}" for key, value in sorted(env.items())]
    atomic_write_once(repositories_file, canonical(repositories))
    atomic_write_once(targets_file, canonical(targets))
    atomic_write_once(discovery_file, canonical(discovery))
    atomic_write_once(env_path, ("\n".join(lines) + "\n").encode("utf-8"))
    return {"env": env, "env_path": env_path, "derived_dir": derived_dir, "wheel": wheel_path}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="platform_env.py", description=__doc__.splitlines()[0])
    subparsers = parser.add_subparsers(dest="action", required=True)
    emit = subparsers.add_parser("emit")
    emit.add_argument("--snapshot", type=Path, required=True)
    emit.add_argument("--install-id", required=True)
    emit.add_argument("--tree", type=Path, required=True, help="deployment tree root")
    emit.add_argument("--release-file", type=Path, required=True)
    emit.add_argument("--wheel", type=Path, default=None)
    args = parser.parse_args(argv)

    require(args.snapshot.is_file(), f"snapshot not found: {args.snapshot}")
    try:
        snapshot = args.snapshot.read_bytes()
    except OSError as error:
        die(f"cannot read snapshot {args.snapshot}: {error}")
    require(args.release_file.is_file(), f"release file not found: {args.release_file}")
    try:
        release = release_identity(json.loads(args.release_file.read_text(encoding="utf-8")))
    except (OSError, ValueError) as error:
        die(f"cannot read release file {args.release_file}: {error}")
    result = derive(
        snapshot,
        install_id=args.install_id,
        tree_root=args.tree,
        release=release,
        wheel=args.wheel,
    )
    print(f"env={result['env_path']}")
    print(f"derived={result['derived_dir']}")
    print(f"wheel={result['wheel']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
