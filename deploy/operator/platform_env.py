#!/usr/bin/env python3
"""Derive the internal operator environment from the config snapshot (transitional bridge).

The private platform.json snapshot is the single configuration authority.  The legacy env-file
consumers (``render_operator.py``, ``install.sh``, ``health.sh``) keep reading their env keys;
this module emits those keys from the snapshot plus go-time inputs (install generation,
deployment tree, wheel) so the unified ``operatorctl --config`` entry never asks a human to
duplicate configuration.

Derivations (all paths under the configured storage roots; converged here as the single table):
  AIRFLOW_VENV   = <release_root>/airflow-venv-<install_id>
  AIRFLOW_HOME   = <state_root>/airflow-home
  POSTGRES_ROOT  = <state_root>/airflow-pg
  AIRFLOW_DB_URL = postgresql+psycopg2://solidworks@/airflow_meta?host=<POSTGRES_ROOT>/socket&port=<POSTGRES_PORT>
  PIPELINE_WHEEL = the installed wheel (explicit ``--wheel`` or the single ``*.whl`` under the
  deployment tree), bound to a release artifact by name+digest before any leg runs
  MODEL_* / DISCOVERY files = derived JSON under <state_root>/derived (0600)

The derived env file is written 0600 under <state_root>/derived and is an internal artifact of
the lifecycle, never an external interface.

Every emit runs the canonical IF-16 contract validation from the contract closure carried
inside the digest-bound release wheel and refuses an invalid or tampered snapshot before the
first write - the bootstrap enforces the same rules as the installed pipeline, with no local
mirror and no venv/service/checkout dependency.

Exit codes: 0 success; 1 error (message on stderr); 2 usage error.
"""

from __future__ import annotations

import argparse
import json
import sys
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


def load_contract_closure(wheel: Path):
    """Import the canonical contract validators from the release wheel itself.

    Bootstrap runs before any venv, service or checkout exists, so the same-source minimal
    closure is extracted from the digest-bound release artifact into a private directory
    and imported there with the plain interpreter.  A wheel missing any closure member is
    refused - the gate never falls back to weaker local checks.
    """
    import atexit  # noqa: PLC0415
    import shutil  # noqa: PLC0415
    import tempfile  # noqa: PLC0415
    import zipfile  # noqa: PLC0415

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
    sys.path.insert(0, str(directory))
    from description_pipeline import interface_records  # noqa: PLC0415  (from the extracted closure)

    return interface_records


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
    snapshot: dict,
    *,
    install_id: str,
    tree_root: Path,
    release: dict,
    wheel: Path | None = None,
) -> dict:
    """Return ``{env, env_path, derived_dir, wheel}`` for one install generation."""

    require(bool(install_id) and all(character not in install_id for character in "\n\r/"), "install_id is malformed")
    require(
        isinstance(release, dict) and "artifacts" in release,
        "the release identity is required to bind the consumed wheel",
    )
    wheel_path = wheel_binding(release, wheel=wheel, tree_root=tree_root)
    # The canonical IF-16 validation runs before the emit's first write.  The closure travels
    # inside the digest-bound release wheel, so the bootstrap enforces exactly the rules of the
    # installed pipeline - no local mirror, no venv, no checkout dependency.
    interface_records = load_contract_closure(wheel_path)
    try:
        interface_records.validate_config_snapshot(snapshot)
    except interface_records.InterfaceRecordError as error:
        die(f"snapshot invalid: {error}")
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
            f"postgresql+psycopg2://solidworks@/airflow_meta?host={postgres_root}/socket&port={postgres_port}"
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
        snapshot = json.loads(args.snapshot.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        die(f"cannot parse snapshot {args.snapshot}: {error}")
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
