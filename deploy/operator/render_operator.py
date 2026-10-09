"""Render the operator deployment configuration, TLS material and reverse-proxy config.

The renderer is idempotent: the TLS key pair is generated once and preserved on reruns, and every
other file is re-rendered deterministically from the environment, so restarting or re-rendering
never invalidates a live operator URL. Everything it writes lives under OPERATOR_STATE (0700);
nothing is written into Git. Authentication is one Feishu SSO (the package-local Airflow auth
manager); the proxy only terminates TLS on the single operator URL.
"""

from __future__ import annotations

import argparse
import ipaddress
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


def load_env(path: Path | None) -> dict[str, str]:
    values: dict[str, str] = {}
    if path is not None:
        if not path.is_file():
            die(f"env file not found: {path}")
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            key, separator, value = stripped.partition("=")
            if not separator or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key.strip()):
                die(f"{path}:{number}: expected KEY=VALUE")
            values[key.strip()] = value.strip()
    else:
        values = {key: value for key, value in os.environ.items() if re.fullmatch(r"[A-Z][A-Z0-9_]*", key)}
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
        ("@OPERATOR_UPSTREAM_HOST@", resolved["PORTAL_HOST"]),
        ("@OPERATOR_UPSTREAM_PORT@", resolved["PORTAL_PORT"]),
        ("@ENDPOINT_TOKEN_FILE@", resolved["ENDPOINT_TOKEN_FILE"]),
    ):
        rendered = rendered.replace(token, value)
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
    parser.add_argument("--units-dir", type=Path, help="also render the two operator unit files here")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="render into a temporary directory and report drift against the installed state",
    )
    parser.add_argument("--json", action="store_true", help="print the resolved environment as JSON")
    args = parser.parse_args()

    values = load_env(args.env_file)
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
    https_port = integer(values, "OPERATOR_HTTPS_PORT", 8443)
    upstream = require_no_space(values.get("OPERATOR_UPSTREAM", "127.0.0.1:8788"), "OPERATOR_UPSTREAM")
    if ":" not in upstream:
        die("OPERATOR_UPSTREAM must be host:port")
    portal_host, _, portal_port_raw = upstream.rpartition(":")
    if not portal_port_raw.isdigit():
        die("OPERATOR_UPSTREAM must be host:port")
    portal_port = int(portal_port_raw)
    if portal_port == https_port:
        die("OPERATOR_UPSTREAM must not collide with the proxy ports")
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
    nginx_bin = values.get("NGINX_BIN", "/usr/sbin/nginx")
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
        AIRFLOW_BASE_URL="http://127.0.0.1:8791",
        OPERATOR_URL=f"https://{host}:{https_port}/",
        SOLIDWORKS_HANDOFF_ROOT=str(handoff_root),
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
        portal_unit = (UNIT_TEMPLATES / "description-portal.service").read_text(encoding="utf-8")
        for token, value in (
            ("@OPERATOR_STATE@", str(state)),
            ("@AIRFLOW_HOME@", str(home)),
            ("@PORTAL_COMMAND@", resolved["PORTAL_COMMAND"]),
        ):
            portal_unit = portal_unit.replace(token, value)
        (args.units_dir / "description-portal.service").write_text(portal_unit, encoding="utf-8")
        proxy_unit = (UNIT_TEMPLATES / "description-operator-proxy.service").read_text(encoding="utf-8")
        for token, value in (
            ("@OPERATOR_STATE@", str(state)),
            ("@OPERATOR_NGINX_CONF@", str(nginx_conf)),
            ("@NGINX_BIN@", nginx_bin),
        ):
            proxy_unit = proxy_unit.replace(token, value)
        (args.units_dir / "description-operator-proxy.service").write_text(proxy_unit, encoding="utf-8")
        print(f"OPERATOR_UNITS={args.units_dir}")

    safe = re.compile(r"^[A-Za-z0-9._/:=@%+-]*$")
    resolved_lines = [
        f"{key}={value if safe.fullmatch(value) else shlex.quote(value)}"
        for key, value in sorted(resolved.items())
        if value != ""
    ]
    write_private(state / "resolved.env", "\n".join(resolved_lines) + "\n")

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
        import json

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
