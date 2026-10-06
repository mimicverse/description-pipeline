"""Render the private 0600 Airflow config atomically, preserving existing secrets."""

from __future__ import annotations

import argparse
import base64
import os
import re
import secrets
import stat
import sys
from pathlib import Path

FORBIDDEN = {Path("/"), Path("/usr"), Path("/etc"), Path("/opt"), Path("/var"), Path.home()}
MARKER = "# managed-by: description-airflow\n"


def _new_fernet_key() -> str:
    """Generate a Fernet key with the standard library only (32 urlsafe-base64 bytes)."""
    return base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")


def _abs_dir(value: str, where: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise SystemExit(f"{where} must be an absolute path")
    if path in FORBIDDEN or any(part in {"..", ""} for part in path.parts):
        raise SystemExit(f"{where} refuses shared/root paths: {path}")
    if path.is_symlink():
        raise SystemExit(f"{where} must not be a symlink: {path}")
    return path


def _existing_secret(text: str, key: str) -> str | None:
    match = re.search(rf"(?m)^\s*{key}\s*=\s*(\S+)\s*$", text)
    return match.group(1) if match and match.group(1) else None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", required=True)
    parser.add_argument("--venv", required=True)
    parser.add_argument("--template", required=True)
    parser.add_argument("--dags-folder", required=True)
    args = parser.parse_args()
    home = _abs_dir(args.home, "AIRFLOW_HOME")
    venv = _abs_dir(args.venv, "AIRFLOW_VENV")
    if home == venv:
        raise SystemExit("AIRFLOW_HOME and AIRFLOW_VENV must be distinct")
    db_url = os.environ.get("AIRFLOW_DB_URL", "")
    if not db_url.startswith(("postgresql+psycopg2://",)):
        raise SystemExit("AIRFLOW_DB_URL must be the dedicated PostgreSQL DSN")
    home.mkdir(parents=True, exist_ok=True)
    os.chmod(home, stat.S_IRWXU)
    cfg = home / "airflow.cfg"
    existing = cfg.read_text(encoding="utf-8") if cfg.is_file() else ""
    if existing and MARKER.strip() not in existing:
        raise SystemExit(f"{cfg} is not managed by this installer; refusing to overwrite")
    fernet = _existing_secret(existing, "fernet_key") or _new_fernet_key()
    jwt = _existing_secret(existing, "jwt_secret") or secrets.token_hex(32)
    rendered = Path(args.template).read_text(encoding="utf-8")
    rendered = (
        rendered.replace("@AIRFLOW_HOME@", str(home))
        .replace("@DAGS_FOLDER@", str(Path(args.dags_folder).resolve()))
        .replace("@FERNET_KEY@", fernet)
        .replace("@JWT_SECRET@", jwt)
        .replace("@AIRFLOW_DB_URL@", db_url)
    )
    if not rendered.startswith("#"):
        rendered = MARKER + rendered
    fd, temp_name = None, home / f".airflow.cfg.{os.getpid()}.tmp"
    fd = os.open(temp_name, os.O_CREAT | os.O_EXCL | os.O_WRONLY, stat.S_IRUSR | stat.S_IWUSR)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(rendered)
        os.replace(temp_name, cfg)
    finally:
        if temp_name.exists():
            temp_name.unlink(missing_ok=True)
    os.chmod(cfg, stat.S_IRUSR | stat.S_IWUSR)
    return 0


if __name__ == "__main__":
    sys.exit(main())
