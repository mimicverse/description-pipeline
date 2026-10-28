"""Render the operator deployment configuration, TLS material and reverse-proxy config.

The renderer is idempotent: the TLS key pair is generated once and preserved on reruns, and every
other file is re-rendered deterministically from the environment, so restarting or re-rendering
never invalidates a live operator URL. Everything it writes lives under OPERATOR_STATE (0700);
nothing is written into Git. Authentication is one Feishu SSO (the package-local Airflow auth
manager); the proxy only terminates TLS on the single operator URL.
"""

from __future__ import annotations

import argparse
import importlib.util
import ipaddress
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = Path(__file__).resolve().parent / "nginx.conf.template"
PORTAL_TEMPLATE = Path(__file__).resolve().parent / "portal.json.template"
UNIT_TEMPLATES = Path(__file__).resolve().parent / "systemd"


def _load_sibling_module(name: str):
    path = Path(__file__).resolve().with_name(f"{name}.py")
    module_name = f"description_deploy_{name}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"render_operator: cannot load the deployment module: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


systemd_units = _load_sibling_module("systemd_units")
FORBIDDEN = {Path("/"), Path("/usr"), Path("/etc"), Path("/opt"), Path("/var"), Path.home()}
BROAD_ROOTS = {
    Path("/"),
    Path("/usr"),
    Path("/etc"),
    Path("/opt"),
    Path("/var"),
    Path("/home"),
    Path("/root"),
    Path("/tmp"),
    Path("/srv"),
    Path("/mnt"),
    Path("/media"),
    Path.home(),
}
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
#: The single source of deployment port defaults (DEP-01).  The operator env
#: file may override every value; each rendered file and every downstream
#: deploy script consumes the values from resolved.env, so no template, unit
#: or script repeats a literal.  SOLIDWORKS_ENDPOINT_PORT must equal
#: description_pipeline.ports.SOLIDWORKS_ENDPOINT_PORT
#: (tests/v1/test_port_defaults.py).
PORT_DEFAULTS = {
    "OPERATOR_HTTPS_PORT": 8443,
    "OPERATOR_UPSTREAM_PORT": 8788,
    "AIRFLOW_API_PORT": 8791,
    "TUNNEL_LOOPBACK_PORT": 18765,
    "POSTGRES_PORT": 5433,
    "SOLIDWORKS_ENDPOINT_PORT": 8765,
}
# Keys that must not appear in the operator env: removed auth modes and credential values that
# belong in the 0600 secret file. Refusing them keeps a stale/hand-edited file from silently
# reintroducing a second login or a duplicated credential.
UNSUPPORTED_ENV_KEYS = {
    "OPERATOR_BASIC_USER": "the proxy has no Basic auth; the single human login is Feishu SSO",
    "OPERATOR_BASIC_PASSWORD": "the proxy has no Basic auth; the single human login is Feishu SSO",
    "OPERATOR_HTPASSWD_FILE": "the proxy has no Basic auth; the single human login is Feishu SSO",
    "FEISHU_APP_ID": "the app_id lives in the 0600 secret file; never duplicate it in the environment",
    "FEISHU_AUTHORIZE_BASE": "the deployment uses the fixed official Feishu endpoints",
    "FEISHU_TOKEN_URL": "the deployment uses the fixed official Feishu endpoints",
    "FEISHU_USERINFO_URL": "the deployment uses the fixed official Feishu endpoints",
    "FEISHU_STATE_TTL_SECONDS": "the deployment uses the module's fixed state lifetime",
}
FEISHU_ENV_KEYS = ("FEISHU_APP_SECRET_FILE", "FEISHU_TENANT_KEYS", "FEISHU_REDIRECT_URI", "FEISHU_ADMIN_OPEN_IDS")


def die(message: str) -> None:
    raise SystemExit(f"render_operator: {message}")


def require_distinct_local_ports(ports: dict[str, int | None]) -> None:
    """Validate the actual local listener set with one shared collision rule."""

    invalid = sorted(
        name
        for name, value in ports.items()
        if value is not None and (type(value) is not int or not 1 <= value < 65536)
    )
    if invalid:
        raise ValueError(f"local ports are invalid: {', '.join(invalid)}")
    seen: dict[int, str] = {}
    collisions = []
    for name, value in ports.items():
        if value is None:
            continue
        previous = seen.get(value)
        if previous is not None:
            collisions.append(f"{previous}/{name}={value}")
        else:
            seen[value] = name
    if collisions:
        raise ValueError(f"local ports must not collide: {', '.join(collisions)}")


#: The one env-file encoding: a value stays bare only when every byte is in this shell-safe
#: set; anything else (the libpq DSNs' ``&``/``?``, lists' ``,``, quoted paths) is single-quoted
#: by shlex so that BOTH readers of this file - a shell ``source`` and the KEY=VALUE parser
#: below - observe the identical bytes.  Every writer (this module's resolved.env and
#: platform_env's operator.env) and every reader share these helpers; no consumer parses the
#: file with its own quoting assumptions.
ENV_SAFE = re.compile(r"^[A-Za-z0-9._/:=@%+-]*$")


def encode_env_value(value: str) -> str:
    """The canonical on-disk encoding of one env value.

    The format is one line per key=value, so a value carrying a line break cannot be
    represented: the encoder refuses it (the writer fails closed) instead of emitting a
    file that every reader would misparse.
    """

    if "\n" in value or "\r" in value:
        raise ValueError("an env value cannot carry a line break")
    return value if ENV_SAFE.fullmatch(value) else shlex.quote(value)


def encode_env_lines(pairs) -> list[str]:
    """Sorted ``KEY=value`` lines with the canonical encoding; empty values are omitted."""

    return [f"{key}={encode_env_value(value)}" for key, value in sorted(pairs) if value != ""]


def decode_env_value(raw: str) -> str:
    """The exact inverse of :func:`encode_env_value` for one KEY=VALUE's value part."""

    parts = shlex.split(raw, comments=False, posix=True)
    if len(parts) != 1:
        raise ValueError(f"the env value is not one shell word: {raw!r}")
    return parts[0]


def read_env(path: Path) -> dict[str, str]:
    """Read environment declarations as data without evaluating shell expressions."""

    values: dict[str, str] = {}
    if not path.is_file() or path.is_symlink():
        die(f"env file not found or is a link: {path}")
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, separator, raw = stripped.partition("=")
        if not separator or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key.strip()):
            die(f"{path}:{number}: expected KEY=VALUE")
        key = key.strip()
        try:
            value = decode_env_value(raw.strip())
        except ValueError as error:
            die(f"{path}:{number}: {error}")
        if "\x00" in value:
            die(f"{path}:{number}: environment values cannot contain NUL")
        if key in values and values[key] != value:
            die(f"{path}:{number}: conflicting duplicate declaration for {key}")
        values[key] = value
    return values


def validate_env(values: dict[str, str]) -> dict[str, str]:
    for required in (
        "OPERATOR_HOST",
        "OPERATOR_STATE",
        "AIRFLOW_VENV",
        "AIRFLOW_HOME",
        "AIRFLOW_DB_URL",
        "SOLIDWORKS_SSH_HOST",
        "SOLIDWORKS_HANDOFF_ROOT",
    ):
        if not values.get(required):
            die(f"{required} is required")
    for unsupported, reason in UNSUPPORTED_ENV_KEYS.items():
        if unsupported in values:
            die(f"{unsupported} is not supported: {reason}; delete it from the env file")
    return values


def private_dir(value: str, where: str) -> Path:
    if not value.startswith("/") or any(part in {"..", ""} for part in Path(value).parts[1:]):
        die(f"{where} must be an absolute path without '..': {value}")
    path = Path(value)
    if path in FORBIDDEN or path.is_symlink():
        die(f"{where} refuses a shared/root path or symlink: {path}")
    return path


def private_file(value: str, where: str) -> Path:
    path = private_dir(value, where)
    if not path.is_file():
        die(f"{where} not found: {path}")
    if stat.S_IMODE(path.stat().st_mode) != 0o600:
        die(f"{where} must be mode 0600: {path}")
    return path


def integer(values: dict[str, str], key: str, default: int | None = None) -> int | None:
    raw = values.get(key, "")
    if not raw:
        return default
    if not raw.isdigit() or not 1 <= int(raw) <= 65535:
        die(f"{key} must be a TCP port (1-65535), got {raw!r}")
    return int(raw)


def require_no_space(value: str, where: str) -> str:
    if not value or any(character.isspace() for character in value) or "$" in value:
        die(f"{where} must be non-empty without whitespace or '$': {value!r}")
    return value


def tls_subject_alt_name(host: str) -> str:
    try:
        address = ipaddress.ip_address(host)
        return f"IP:{address}"
    except ValueError:
        return f"DNS:{host}"


def write_private(path: Path, text: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, stat.S_IRWXU)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, mode)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    os.chmod(path, mode)


def ensure_certificate(
    state: Path, host: str, days: int, provided_cert: str, provided_key: str
) -> tuple[Path, Path, str]:
    certificate = state / "secrets" / "tls.crt"
    key = state / "secrets" / "tls.key"
    if provided_cert or provided_key:
        if not (provided_cert and provided_key):
            die("OPERATOR_TLS_CERT and OPERATOR_TLS_KEY must be provided together")
        cert_path, key_path = Path(provided_cert), Path(provided_key)
        if not cert_path.is_file() or not key_path.is_file():
            die("provided TLS certificate or key is missing")
        return cert_path, key_path, "provided"
    if certificate.is_file() and key.is_file():
        return certificate, key, "self-signed"
    if shutil.which("openssl") is None:
        die("openssl is required to generate the self-signed certificate")
    certificate.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-sha256",
            "-nodes",
            "-days",
            str(days),
            "-subj",
            f"/CN={host}",
            "-addext",
            f"subjectAltName={tls_subject_alt_name(host)}",
            "-keyout",
            str(key),
            "-out",
            str(certificate),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        die(f"openssl certificate generation failed: {result.stderr.strip()[-200:]}")
    os.chmod(key, 0o600)
    os.chmod(certificate, 0o600)
    return certificate, key, "self-signed"


def ensure_portal_config(state: Path, resolved: dict[str, str]) -> Path:
    """Render the one current portal config from the environment on every run."""
    target = state / "portal.json"
    rendered = PORTAL_TEMPLATE.read_text(encoding="utf-8")
    for token, value in (
        ("@AIRFLOW_BASE_URL@", resolved["AIRFLOW_BASE_URL"]),
        ("@TUNNEL_LOOPBACK_PORT@", resolved["TUNNEL_LOOPBACK_PORT"]),
        ("@OPERATOR_UPSTREAM_HOST@", resolved["PORTAL_HOST"]),
        ("@OPERATOR_UPSTREAM_PORT@", resolved["PORTAL_PORT"]),
        ("@ENDPOINT_TOKEN_FILE@", resolved["ENDPOINT_TOKEN_FILE"]),
        ("@OPERATOR_UPLOAD_ROOT@", resolved["SOLIDWORKS_HANDOFF_ROOT"]),
    ):
        rendered = rendered.replace(token, value)
    rendered = rendered.replace("@MODEL_REPOSITORIES_JSON@", resolved["MODEL_REPOSITORIES_JSON"])
    rendered = rendered.replace("@MODEL_TARGETS_JSON@", resolved["MODEL_TARGETS_JSON"])
    rendered = rendered.replace("@DISCOVERY_SETTINGS_JSON@", resolved["DISCOVERY_SETTINGS_JSON"])
    write_private(target, rendered)
    return target


def ensure_feishu_env(state: Path, resolved: dict[str, str]) -> Path:
    """Render the 0600 FEISHU_* environment file loaded by the Airflow api-server unit."""
    target = state / "feishu.env"
    lines = [f"{key}={resolved[key]}" for key in FEISHU_ENV_KEYS if resolved.get(key)]
    write_private(target, "\n".join(lines) + "\n")
    return target


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--state", help="override OPERATOR_STATE")
    parser.add_argument("--check-paths", action="store_true", help="require runtime paths to exist")
    parser.add_argument(
        "--nginx-bin",
        help="the managed proxy binary path decided by the installer; overrides NGINX_BIN in the env file",
    )
    parser.add_argument("--units-dir", type=Path, help="also render the two operator unit files here")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="render into a temporary directory and report drift against the installed state",
    )
    parser.add_argument("--json", action="store_true", help="print the resolved environment as JSON")
    args = parser.parse_args()

    values = read_env(args.env_file) if args.env_file is not None else {
        key: value for key, value in os.environ.items() if re.fullmatch(r"[A-Z][A-Z0-9_]*", key)
    }
    values = validate_env(values)
    if args.state:
        values["OPERATOR_STATE"] = args.state
    installed_state = private_dir(require_no_space(values["OPERATOR_STATE"], "OPERATOR_STATE"), "OPERATOR_STATE")
    workdir: tempfile.TemporaryDirectory | None = None
    if args.dry_run:
        workdir = tempfile.TemporaryDirectory(prefix="operator-drift-")
        state = Path(workdir.name) / "state"
    else:
        state = installed_state
    state.mkdir(parents=True, exist_ok=True)
    os.chmod(state, stat.S_IRWXU)

    host = values["OPERATOR_HOST"].strip()
    if not NAME.fullmatch(host):
        die(f"OPERATOR_HOST must be a DNS name or LAN address: {host!r}")
    bind = values.get("OPERATOR_BIND", "0.0.0.0").strip() or "0.0.0.0"
    if bind != "0.0.0.0":
        try:
            ipaddress.ip_address(bind)
        except ValueError:
            die(f"OPERATOR_BIND must be an IP address: {bind!r}")
    https_port = integer(values, "OPERATOR_HTTPS_PORT", PORT_DEFAULTS["OPERATOR_HTTPS_PORT"])
    upstream = require_no_space(
        values.get("OPERATOR_UPSTREAM", f"127.0.0.1:{PORT_DEFAULTS['OPERATOR_UPSTREAM_PORT']}"), "OPERATOR_UPSTREAM"
    )
    airflow_api_port = integer(values, "AIRFLOW_API_PORT", PORT_DEFAULTS["AIRFLOW_API_PORT"])
    tunnel_port = integer(values, "TUNNEL_LOOPBACK_PORT", PORT_DEFAULTS["TUNNEL_LOOPBACK_PORT"])
    postgres_port = integer(values, "POSTGRES_PORT", PORT_DEFAULTS["POSTGRES_PORT"])
    endpoint_port = integer(values, "SOLIDWORKS_ENDPOINT_PORT", PORT_DEFAULTS["SOLIDWORKS_ENDPOINT_PORT"])
    if ":" not in upstream:
        die("OPERATOR_UPSTREAM must be host:port")
    portal_host, _, portal_port_raw = upstream.rpartition(":")
    if not portal_port_raw.isdigit():
        die("OPERATOR_UPSTREAM must be host:port")
    portal_port = int(portal_port_raw)
    try:
        portal_is_loopback = ipaddress.ip_address(portal_host).is_loopback
    except ValueError:
        portal_is_loopback = portal_host == "localhost"
    try:
        require_distinct_local_ports(
            {
                "operator_https": https_port,
                "portal": portal_port if portal_is_loopback else None,
                "airflow_api": airflow_api_port,
                "tunnel_loopback": tunnel_port,
                "postgres": postgres_port,
            }
        )
    except ValueError as error:
        die(str(error))
    venv = private_dir(require_no_space(values["AIRFLOW_VENV"], "AIRFLOW_VENV"), "AIRFLOW_VENV")
    home = private_dir(require_no_space(values["AIRFLOW_HOME"], "AIRFLOW_HOME"), "AIRFLOW_HOME")
    if venv == home:
        die("AIRFLOW_VENV and AIRFLOW_HOME must be distinct")
    handoff_root = private_dir(
        require_no_space(values["SOLIDWORKS_HANDOFF_ROOT"], "SOLIDWORKS_HANDOFF_ROOT"), "SOLIDWORKS_HANDOFF_ROOT"
    )
    if handoff_root in BROAD_ROOTS:
        die(f"SOLIDWORKS_HANDOFF_ROOT must be a dedicated intake directory, not a broad root: {handoff_root}")
    runtime_paths = [("OPERATOR_STATE", state), ("AIRFLOW_VENV", venv), ("AIRFLOW_HOME", home)]
    if values.get("POSTGRES_ROOT"):
        runtime_paths.append(("POSTGRES_ROOT", Path(values["POSTGRES_ROOT"])))
    for label, other in runtime_paths:
        if handoff_root == other or other in handoff_root.parents or handoff_root in other.parents:
            die(f"SOLIDWORKS_HANDOFF_ROOT must not overlap {label}: {handoff_root}")
    repositories = {}
    repositories_file = values.get("MODEL_REPOSITORIES_FILE", "")
    if repositories_file:
        path = private_file(repositories_file, "MODEL_REPOSITORIES_FILE")
        try:
            repositories = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            die(f"MODEL_REPOSITORIES_FILE is unreadable: {error}")
        if not isinstance(repositories, dict):
            die("MODEL_REPOSITORIES_FILE must map repository slugs to Linux checkout paths")
        for slug, checkout in repositories.items():
            if (
                not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", slug)
                or any(part in {".", ".."} for part in slug.split("/"))
                or not isinstance(checkout, str)
            ):
                die("MODEL_REPOSITORIES_FILE requires owner/name keys and absolute Linux paths")
            directory = private_dir(checkout, "model checkout")
            if directory in BROAD_ROOTS or any(parent.is_symlink() for parent in directory.parents):
                die("Model checkout must be a dedicated real directory")
            for label, other in [("handoff intake", handoff_root), *runtime_paths]:
                if directory.is_relative_to(other) or other.is_relative_to(directory):
                    die(f"Model checkout must not overlap {label}")
    targets = {}
    targets_file = values.get("MODEL_TARGETS_FILE", "")
    if targets_file:
        path = private_file(targets_file, "MODEL_TARGETS_FILE")
        try:
            targets = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            die(f"MODEL_TARGETS_FILE is unreadable: {error}")
        if not isinstance(targets, dict):
            die("MODEL_TARGETS_FILE must map hardware identities to repository routing")
        for hardware, target in targets.items():
            if (
                not isinstance(hardware, str)
                or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,63}", hardware)
                or not isinstance(target, dict)
                or set(target) != {"repository_slug", "base"}
            ):
                die("MODEL_TARGETS_FILE requires hardware -> {repository_slug, base}")
            slug, base = target["repository_slug"], target["base"]
            if (
                not isinstance(slug, str)
                or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", slug)
                or slug not in repositories
            ):
                die(f"MODEL_TARGETS_FILE target {hardware!r} must name a configured repository")
            if (
                not isinstance(base, str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", base)
                or ".." in base
                or base.endswith(".lock")
                or base.startswith("-")
            ):
                die(f"MODEL_TARGETS_FILE target {hardware!r} base must be a well-formed git branch")
    discovery = {}
    discovery_file = values.get("DISCOVERY_SETTINGS_FILE", "")
    if discovery_file:
        path = private_file(discovery_file, "DISCOVERY_SETTINGS_FILE")
        try:
            discovery = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            die(f"DISCOVERY_SETTINGS_FILE is unreadable: {error}")
        if not isinstance(discovery, dict) or set(discovery) - {"record_roots"}:
            die("DISCOVERY_SETTINGS_FILE accepts only record_roots")
        records = discovery.get("record_roots", [])
        if not isinstance(records, list) or any(not isinstance(root, str) for root in records):
            die("discovery record_roots must be a list of absolute Linux directories")
        for root in records:
            directory = private_dir(root, "controlled-record root")
            if any(parent.is_symlink() for parent in directory.parents):
                die("Controlled-record roots cannot traverse a symlink")
    nginx_bin = args.nginx_bin or values.get("NGINX_BIN", "/usr/sbin/nginx")
    if args.check_paths:
        for path, where in ((Path(values["PIPELINE_WHEEL"]), "PIPELINE_WHEEL"), (Path(nginx_bin), "NGINX_BIN")):
            if not path.is_file():
                die(f"{where} not found: {path}")

    # Feishu SSO: optional at install time (the auth manager then fails explicitly), but a supplied
    # secret file and tenant allowlist must be complete, private and sane before anything is written.
    feishu_secret = values.get("FEISHU_APP_SECRET_FILE", "").strip()
    tenant_keys = values.get("FEISHU_TENANT_KEYS", "").strip()
    admin_open_ids = values.get("FEISHU_ADMIN_OPEN_IDS", "").strip()
    if bool(feishu_secret) != bool(tenant_keys):
        die("FEISHU_APP_SECRET_FILE and FEISHU_TENANT_KEYS must be provided together")
    if feishu_secret:
        private_file(require_no_space(feishu_secret, "FEISHU_APP_SECRET_FILE"), "FEISHU_APP_SECRET_FILE")
        require_no_space(tenant_keys, "FEISHU_TENANT_KEYS")
    if admin_open_ids:
        require_no_space(admin_open_ids, "FEISHU_ADMIN_OPEN_IDS")

    resolved: dict[str, str] = {
        key: value for key, value in values.items() if key not in {"PORTAL_COMMAND", "PORTAL_CONFIG"}
    }
    resolved.update(
        OPERATOR_STATE=str(state),
        OPERATOR_BIND=bind,
        OPERATOR_HTTPS_PORT=str(https_port),
        OPERATOR_UPSTREAM=upstream,
        PORTAL_HOST=portal_host,
        PORTAL_PORT=str(portal_port),
        AIRFLOW_API_PORT=str(airflow_api_port),
        AIRFLOW_BASE_URL=f"http://127.0.0.1:{airflow_api_port}",
        TUNNEL_LOOPBACK_PORT=str(tunnel_port),
        POSTGRES_PORT=str(postgres_port),
        SOLIDWORKS_ENDPOINT_PORT=str(endpoint_port),
        OPERATOR_URL=f"https://{host}:{https_port}/",
        SOLIDWORKS_HANDOFF_ROOT=str(handoff_root),
        MODEL_REPOSITORIES_JSON=json.dumps(repositories, sort_keys=True),
        MODEL_TARGETS_JSON=json.dumps(targets, sort_keys=True),
        DISCOVERY_SETTINGS_JSON=json.dumps(discovery, sort_keys=True),
        ENDPOINT_TOKEN_FILE=values.get("ENDPOINT_TOKEN_FILE", str(state / "secrets" / "endpoint.token")),
        NGINX_BIN=nginx_bin,
        FEISHU_APP_SECRET_FILE=feishu_secret,
        FEISHU_TENANT_KEYS=tenant_keys,
        FEISHU_ADMIN_OPEN_IDS=admin_open_ids,
        FEISHU_REDIRECT_URI=f"https://{host}:{https_port}/auth/feishu/callback",
    )

    drift: list[str] = []
    provided_cert = values.get("OPERATOR_TLS_CERT", "")
    provided_key = values.get("OPERATOR_TLS_KEY", "")
    if args.dry_run and not provided_cert and not provided_key:
        certificate = installed_state / "secrets" / "tls.crt"
        key = installed_state / "secrets" / "tls.key"
        tls_origin = "installed"
        for path in (certificate, key):
            if not path.is_file():
                drift.append(f"TLS file missing: {path}")
            elif stat.S_IMODE(path.stat().st_mode) != 0o600:
                drift.append(f"TLS file mode is not 0600: {path}")
        if certificate.is_file():
            check = subprocess.run(
                ["openssl", "x509", "-in", str(certificate), "-noout", "-checkhost", host],
                capture_output=True,
                text=True,
            )
            check_ip = subprocess.run(
                ["openssl", "x509", "-in", str(certificate), "-noout", "-checkip", host], capture_output=True, text=True
            )
            if check.returncode != 0 and check_ip.returncode != 0:
                drift.append(f"TLS certificate does not cover {host}")
    else:
        certificate, key, tls_origin = ensure_certificate(
            state, host, int(values.get("OPERATOR_TLS_DAYS", "825") or 825), provided_cert, provided_key
        )
    resolved["OPERATOR_TLS_CERT"] = str(certificate)
    resolved["OPERATOR_TLS_KEY"] = str(key)
    portal_config = ensure_portal_config(state, resolved)
    resolved["PORTAL_CONFIG"] = str(portal_config)
    feishu_env = ensure_feishu_env(state, resolved)
    resolved["FEISHU_ENV_FILE"] = str(feishu_env)
    resolved["PORTAL_COMMAND"] = (
        f"{venv}/bin/python -m description_pipeline.orchestration.portal --config {portal_config}"
    )

    nginx_root = state / "nginx"
    for temporary in ("body", "proxy", "fastcgi", "uwsgi", "scgi"):
        (nginx_root / "tmp" / temporary).mkdir(parents=True, exist_ok=True)
        os.chmod(nginx_root / "tmp" / temporary, stat.S_IRWXU)
    rendered = TEMPLATE.read_text(encoding="utf-8")
    for token, value in (
        ("@OPERATOR_STATE@", str(state)),
        ("@OPERATOR_BIND@", bind),
        ("@OPERATOR_HTTPS_PORT@", str(https_port)),
        ("@OPERATOR_HOST@", host),
        ("@AIRFLOW_API_PORT@", str(airflow_api_port)),
        ("@OPERATOR_UPSTREAM@", upstream),
        ("@OPERATOR_TLS_CERT@", str(certificate)),
        ("@OPERATOR_TLS_KEY@", str(key)),
    ):
        rendered = rendered.replace(token, value)
    nginx_conf = nginx_root / "nginx.conf"
    write_private(nginx_conf, rendered)
    resolved["OPERATOR_NGINX_CONF"] = str(nginx_conf)

    if args.units_dir:
        args.units_dir.mkdir(parents=True, exist_ok=True)
        try:
            operator_units = systemd_units.units_for_role("operator")
        except systemd_units.UnitManifestError as error:
            die(f"operator unit role is invalid: {error}")
        replacements = {
            "@OPERATOR_STATE@": str(state),
            "@AIRFLOW_HOME@": str(home),
            "@ENV_FILE@": str(args.env_file),
            "@PORTAL_COMMAND@": resolved["PORTAL_COMMAND"],
            "@OPERATOR_NGINX_CONF@": str(nginx_conf),
            "@NGINX_BIN@": nginx_bin,
        }
        for unit in operator_units:
            template = UNIT_TEMPLATES / f"{unit}.service"
            if not template.is_file() or template.is_symlink():
                die(f"operator unit template is missing or a link: {template}")
            rendered_unit = template.read_text(encoding="utf-8")
            for token, value in replacements.items():
                rendered_unit = rendered_unit.replace(token, value)
            unresolved = sorted(set(re.findall(r"@[A-Z][A-Z0-9_]*@", rendered_unit)))
            if unresolved:
                die(f"operator unit template has unresolved values: {template}: {', '.join(unresolved)}")
            (args.units_dir / f"{unit}.service").write_text(rendered_unit, encoding="utf-8")
        print(f"OPERATOR_UNITS={args.units_dir}")

    write_private(state / "resolved.env", "\n".join(encode_env_lines(resolved.items())) + "\n")

    if args.dry_run:
        for relative in ("portal.json", "nginx/nginx.conf", "resolved.env", "feishu.env"):
            rendered_path = state / relative
            installed_path = installed_state / relative
            if not installed_path.is_file():
                drift.append(f"installed file missing: {installed_path}")
                continue
            rendered_text = rendered_path.read_text(encoding="utf-8").replace(str(state), str(installed_state))
            if rendered_text != installed_path.read_text(encoding="utf-8"):
                drift.append(f"configuration differs: {installed_path}")
        for item in drift:
            print(f"DRIFT {item}")
        print("DRIFT none" if not drift else f"DRIFT count={len(drift)}")
        return 1 if drift else 0

    if args.json:
        print(json.dumps(dict(sorted(resolved.items())), indent=2))
    else:
        print(f"OPERATOR_URL={resolved['OPERATOR_URL']}")
        print(f"OPERATOR_STATE={state}")
        print(f"OPERATOR_TLS={tls_origin} cert={certificate}")
        print(f"PORTAL_CONFIG={portal_config}")
        print(f"PORTAL_COMMAND={resolved['PORTAL_COMMAND']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
