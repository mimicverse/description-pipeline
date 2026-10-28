"""Shared real-writer/real-consumer fixtures for the config-identity boundary."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

from .test_install_record import (
    build_record,
    contract_owner,
    make_dag,
    make_endpoint_identity,
    make_release,
    platform_document,
)

ROOT = Path(__file__).resolve().parents[2]
WRITER = ROOT / "deploy" / "airflow" / "scripts" / "add_connection.py"
OPERATOR = ROOT / "deploy" / "operator"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


platform_config = _load("config_identity_platform_config", OPERATOR / "platform_config.py")
install_record = _load("config_identity_install_record", OPERATOR / "install_record.py")

STUB_FILES = {
    "airflow/__init__.py": "",
    "airflow/models/__init__.py": "",
    "airflow/models/connection.py": (
        "import json\n"
        "\n"
        "class _ConnId:\n"
        "    def __eq__(self, other):\n"
        "        return ('conn_id', other)\n"
        "\n"
        "class Connection:\n"
        "    conn_id = _ConnId()\n"
        "    def __init__(self, **fields):\n"
        "        self._password = None\n"
        "        for name in ('conn_type','description','host','port','login','password','schema','extra'):\n"
        "            setattr(self, name, None)\n"
        "        self.__dict__.update(fields)\n"
        "        self._password = fields.get('password', fields.get('_password', self._password))\n"
        "    @property\n"
        "    def password(self):\n"
        "        return self._password\n"
        "    @password.setter\n"
        "    def password(self, value):\n"
        "        self._password = value\n"
        "    @property\n"
        "    def extra_dejson(self):\n"
        "        return json.loads(self.extra or '{}')\n"
    ),
    "airflow/settings.py": (
        "import json\n"
        "import os\n"
        "from pathlib import Path\n"
        "from .models.connection import Connection\n"
        "\n"
        "class _Query:\n"
        "    def __init__(self, session):\n"
        "        self._session = session; self._key = None\n"
        "    def filter(self, expression):\n"
        "        if isinstance(expression, tuple) and expression[0] == 'conn_id': self._key = expression[1]\n"
        "        return self\n"
        "    def one_or_none(self):\n"
        "        row = self._session.rows.get(self._key)\n"
        "        self._session.trace('one_or_none', self._key, row is not None)\n"
        "        if row is None: return None\n"
        "        connection = Connection(**row); self._session.current = connection; return connection\n"
        "\n"
        "class Session:\n"
        "    def __init__(self):\n"
        "        self.path = Path(os.environ['STUB_AIRFLOW_RECORD'])\n"
        "        self.trace_path = Path(str(self.path) + '.trace')\n"
        "        self.rows = json.loads(self.path.read_text()) if self.path.exists() else {}\n"
        "        self.pending = None; self.current = None\n"
        "    def __enter__(self): return self\n"
        "    def __exit__(self, *exc): return False\n"
        "    def trace(self, what, key, found):\n"
        "        with self.trace_path.open('a', encoding='utf-8') as handle:\n"
        "            handle.write(json.dumps({'what': what, 'key': key, 'found': found}) + '\\n')\n"
        "    def query(self, model): return _Query(self)\n"
        "    def add(self, row): self.pending = row\n"
        "    def commit(self):\n"
        "        rows = dict(self.rows); row = self.pending if self.pending is not None else self.current\n"
        "        if row is not None:\n"
        "            fields = {key: value for key, value in vars(row).items() "
        "if key not in {'_password', 'is_encrypted'}}\n"
        "            fields['password'] = row.password\n"
        "            rows[fields['conn_id']] = fields\n"
        "        self.rows = rows\n"
        "        self.path.write_text(json.dumps(rows, sort_keys=True), encoding='utf-8')\n"
    ),
    "airflow/hooks/__init__.py": "",
    "airflow/hooks/base.py": (
        "import json\n"
        "import os\n"
        "\n"
        "class BaseHook:\n"
        "    @staticmethod\n"
        "    def get_connection(conn_id):\n"
        "        rows = json.load(open(os.environ['STUB_AIRFLOW_RECORD'], encoding='utf-8'))\n"
        "        row = rows[conn_id]\n"
        "        obj = type('Connection', (), {})()\n"
        "        for key, value in row.items(): setattr(obj, key, value)\n"
        "        obj.extra_dejson = json.loads(row.get('extra') or '{}')\n"
        "        return obj\n"
    ),
}


def prepare_root(root: Path) -> None:
    for relative in (
        "secrets",
        "handoffs",
        "artifacts",
        "workspaces/model-repository",
        "releases",
        "specifications",
    ):
        (root / relative).mkdir(parents=True, exist_ok=True)
    token = root / "secrets" / "endpoint.token"
    token.write_text("t" * 64 + "\n", encoding="utf-8")
    token.chmod(0o600)


def write_snapshot(root: Path, install_id: str, document: dict | None = None) -> Path:
    resolved = platform_config.validate_platform(document or platform_document(root))
    snapshot = platform_config.build_snapshot(
        resolved,
        platform_json_sha256="e" * 64,
        install_id=install_id,
        generated_at="2026-10-17T00:00:00Z",
    )
    return platform_config.write_snapshot(snapshot, root / "state" / "snapshots", contract_owner(root))


def activate(root: Path, install_id: str, document: dict | None = None) -> tuple[Path, dict]:
    prepare_root(root)
    snapshot = write_snapshot(root, install_id, document)
    make_release(root)
    make_endpoint_identity(root)
    make_dag(root)
    record = build_record(root, install_id=install_id, snapshot_path=snapshot)
    record["install_id"] = install_id
    record["snapshot"]["install_id"] = install_id
    record["ends"]["linux"]["install_id"] = install_id
    record["snapshot"]["path"] = str(snapshot)
    record["snapshot"]["digest"] = hashlib.sha256(snapshot.read_bytes()).hexdigest()
    record["ends"]["linux"]["config_digest"] = record["snapshot"]["digest"]
    from description_pipeline import interface_records

    interface_records.validate_install_record(record)
    active = root / "state" / "install-record.json"
    active.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    active.chmod(0o600)
    return snapshot, record


def runtime_env(root: Path) -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(Path.home()),
        "OPERATOR_STATE": str(root / "state"),
        "PYTHONPATH": os.pathsep.join([str(ROOT), str(ROOT / "src")]),
    }


def stub_directory(root: Path) -> Path:
    target = root / "stubs"
    for relative, content in STUB_FILES.items():
        path = target / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return target


def run_writer(
    root: Path,
    snapshot: Path,
    record_file: Path | None = None,
) -> tuple[subprocess.CompletedProcess, dict]:
    stubs = stub_directory(root)
    record_path = root / "airflow-connection.json"
    env = runtime_env(root)
    env.update(
        STUB_AIRFLOW_RECORD=str(record_path),
        PYTHONPATH=os.pathsep.join([str(stubs), str(ROOT), str(ROOT / "src")]),
    )
    command = [
        sys.executable,
        str(WRITER),
        "--state-root",
        str(root / "state"),
        "--snapshot",
        str(snapshot),
    ]
    if record_file is not None:
        command += ["--record-file", str(record_file)]
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        env=env,
        cwd=str(ROOT),
    )
    rows = json.loads(record_path.read_text(encoding="utf-8")) if record_path.exists() else {}
    return result, rows.get("solidworks_windows", {})


def run_writer_real_airflow(root: Path, snapshot: Path, env: dict[str, str]) -> subprocess.CompletedProcess:
    """Invoke the writer against the caller's real Airflow metadata database."""

    runtime = dict(env)
    runtime["PYTHONPATH"] = os.pathsep.join([str(ROOT), str(ROOT / "src")])
    return subprocess.run(
        [
            sys.executable,
            str(WRITER),
            "--state-root",
            str(root / "state"),
            "--snapshot",
            str(snapshot),
        ],
        capture_output=True,
        text=True,
        env=runtime,
        cwd=str(ROOT),
    )


def run_health_resolver(root: Path, row: dict) -> subprocess.CompletedProcess:
    stubs = stub_directory(root)
    record_path = root / "airflow-connection.json"
    record_path.write_text(json.dumps({"solidworks_windows": row}), encoding="utf-8")
    env = runtime_env(root)
    env.update(
        STUB_AIRFLOW_RECORD=str(record_path),
        PYTHONPATH=os.pathsep.join([str(stubs), str(ROOT), str(ROOT / "src")]),
    )
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "description_pipeline.orchestration.airflow_client",
            "--conn-id",
            "solidworks_windows",
            "--state-root",
            str(root / "state"),
        ],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(ROOT),
    )


def connection_document(row: dict) -> dict:
    return {
        "host": row.get("host") or "",
        "port": row.get("port"),
        "password": row.get("password") or "",
        "extra": json.loads(row.get("extra") or "{}"),
    }
