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
  PIPELINE_WHEEL = exactly one ``*.whl`` under the deployment tree unless ``--wheel`` is given
  MODEL_* / DISCOVERY files = derived JSON under <state_root>/derived (0600)

The derived env file is written 0600 under <state_root>/derived and is an internal artifact of
the lifecycle, never an external interface.

Exit codes: 0 success; 1 error (message on stderr); 2 usage error.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))

from platform_config import canonical  # noqa: E402  (shared canonical serialization)
from render_operator import PORT_DEFAULTS  # noqa: E402  (single deploy-layer port source, DEP-01)


def die(message: str) -> None:
    raise SystemExit(f"platform_env: {message}")


def require(condition: bool, message: str) -> None:
    if not condition:
        die(message)


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


def resolve_wheel(tree_root: Path, wheel: Path | None) -> Path:
    """The exact wheel for this generation: explicit or the single one in the tree."""

    if wheel is not None:
        require(wheel.is_file(), f"wheel not found: {wheel}")
        return wheel
    candidates = sorted(path for path in tree_root.glob("*.whl") if path.is_file())
    require(
        len(candidates) == 1,
        f"expected exactly one *.whl under {tree_root} (found {len(candidates)}); pass --wheel",
    )
    return candidates[0]


def _write_replaced(path: Path, payload: bytes) -> Path:
    """Atomic replace for re-derivable artifacts (0600, private directory)."""

    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    os.chmod(path, 0o600)
    return path


def derive(snapshot: dict, *, install_id: str, tree_root: Path, wheel: Path | None = None) -> dict:
    """Return ``{env, env_path, derived_dir, wheel}`` for one install generation."""

    require(bool(install_id) and all(character not in install_id for character in "\n\r/"), "install_id is malformed")
    resolved = snapshot.get("resolved") if isinstance(snapshot, dict) else None
    require(isinstance(resolved, dict), "snapshot resolved tree is missing")
    storage = resolved.get("storage")
    windows = resolved.get("windows")
    operator = resolved.get("operator")
    auth = resolved.get("auth")
    targets = resolved.get("targets")
    specifications = resolved.get("specifications")
    derived = resolved.get("derived")
    require(
        all(isinstance(block, dict) for block in (storage, windows, operator, auth, targets, specifications, derived)),
        "snapshot resolved tree is incomplete",
    )

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

    derived_dir = Path(state_root) / "derived"
    repositories_file = _write_replaced(derived_dir / "repositories.json", canonical(repositories))
    targets_file = _write_replaced(derived_dir / "targets.json", canonical(targets))
    discovery_file = _write_replaced(derived_dir / "discovery.json", canonical(discovery))

    wheel_path = resolve_wheel(tree_root, wheel)
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
    env_path = _write_replaced(derived_dir / "operator.env", ("\n".join(lines) + "\n").encode("utf-8"))
    return {"env": env, "env_path": env_path, "derived_dir": derived_dir, "wheel": wheel_path}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="platform_env.py", description=__doc__.splitlines()[0])
    subparsers = parser.add_subparsers(dest="action", required=True)
    emit = subparsers.add_parser("emit")
    emit.add_argument("--snapshot", type=Path, required=True)
    emit.add_argument("--install-id", required=True)
    emit.add_argument("--tree", type=Path, required=True, help="deployment tree root")
    emit.add_argument("--wheel", type=Path, default=None)
    args = parser.parse_args(argv)

    require(args.snapshot.is_file(), f"snapshot not found: {args.snapshot}")
    try:
        snapshot = json.loads(args.snapshot.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        die(f"cannot parse snapshot {args.snapshot}: {error}")
    result = derive(snapshot, install_id=args.install_id, tree_root=args.tree, wheel=args.wheel)
    print(f"env={result['env_path']}")
    print(f"derived={result['derived_dir']}")
    print(f"wheel={result['wheel']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
