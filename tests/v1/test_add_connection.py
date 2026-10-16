"""The endpoint connection's ``extra`` closes over validated routing that carries ``Path``
values (``store_root``, ``repositories``, ``discovery.record_roots`` and the optional
``frozen_names_file``): every one of them must be written as a string, with record roots and
the registry kept (never dropped to make serialization pass).

Runs the real ``deploy/airflow/scripts/add_connection.py`` as a subprocess with a recording
stub of the Airflow connection model/session (the SQL transport double) and the repository's
real endpoint-config validation.  No Airflow runtime and no database are touched.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "deploy" / "airflow" / "scripts" / "add_connection.py"

STUB_FILES = {
    "airflow/__init__.py": "",
    "airflow/models/__init__.py": "",
    "airflow/models/connection.py": (
        "class _ConnId:\n"
        "    def __eq__(self, other):\n"
        "        return ('conn_id', other)\n"
        "\n"
        "\n"
        "class Connection:\n"
        "    conn_id = _ConnId()\n"
        "\n"
        "    def __init__(self, **fields):\n"
        "        self.__dict__.update(fields)\n"
    ),
    "airflow/settings.py": (
        "import json\n"
        "import os\n"
        "from pathlib import Path\n"
        "\n"
        "from .models.connection import Connection\n"
        "\n"
        "\n"
        "class _Query:\n"
        "    def __init__(self, session):\n"
        "        self._session = session\n"
        "        self._key = None\n"
        "\n"
        "    def filter(self, expression):\n"
        "        if isinstance(expression, tuple) and expression[0] == 'conn_id':\n"
        "            self._key = expression[1]\n"
        "        return self\n"
        "\n"
        "    def one_or_none(self):\n"
        "        row = self._session.rows.get(self._key)\n"
        "        self._session.trace('one_or_none', self._key, row is not None)\n"
        "        if row is None:\n"
        "            return None\n"
        "        connection = Connection(**row)\n"
        "        self._session.current = connection\n"
        "        return connection\n"
        "\n"
        "\n"
        "class Session:\n"
        "    def __init__(self):\n"
        "        self.path = Path(os.environ['STUB_AIRFLOW_RECORD'])\n"
        "        self.trace_path = Path(str(self.path) + '.trace')\n"
        "        self.rows = json.loads(self.path.read_text()) if self.path.exists() else {}\n"
        "        self.pending = None\n"
        "        self.current = None\n"
        "\n"
        "    def __enter__(self):\n"
        "        return self\n"
        "\n"
        "    def __exit__(self, *exc):\n"
        "        return False\n"
        "\n"
        "    def trace(self, what, key, found):\n"
        "        with self.trace_path.open('a', encoding='utf-8') as handle:\n"
        "            handle.write(json.dumps({'what': what, 'key': key, 'found': found}) + '\\n')\n"
        "\n"
        "    def query(self, model):\n"
        "        return _Query(self)\n"
        "\n"
        "    def add(self, row):\n"
        "        self.pending = row\n"
        "\n"
        "    def commit(self):\n"
        "        rows = dict(self.rows)\n"
        "        row = self.pending if self.pending is not None else self.current\n"
        "        if row is not None:\n"
        "            fields = dict(vars(row))\n"
        "            rows[fields['conn_id']] = fields\n"
        "        self.path.write_text(json.dumps(rows, sort_keys=True), encoding='utf-8')\n"
    ),
}


class AddConnectionExtraTests(unittest.TestCase):
    def _stubs(self, tmp: Path) -> Path:
        stubs = Path(tmp) / "stubs"
        for relative, content in STUB_FILES.items():
            target = stubs / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        return stubs

    def _run(self, tmp: Path, pipeline: dict) -> tuple[subprocess.CompletedProcess, Path]:
        stubs = self._stubs(tmp)
        token = Path(tmp) / "endpoint.token"
        token.write_text("token\n", encoding="utf-8")
        token.chmod(0o600)
        handoff = Path(tmp) / "handoffs"
        handoff.mkdir(exist_ok=True)
        portal = Path(tmp) / "portal.json"
        portal.write_text(json.dumps({"pipeline": pipeline}), encoding="utf-8")
        record = Path(tmp) / "connection.json"
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(Path.home()),
            "PYTHONPATH": os.pathsep.join(
                [str(stubs), str(ROOT), str(ROOT / "src"), os.environ.get("PYTHONPATH", "")]
            ).rstrip(os.pathsep),
            "STUB_AIRFLOW_RECORD": str(record),
        }
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--host",
                "127.0.0.1",
                "--port",
                "18765",
                "--token-file",
                str(token),
                "--pipeline-config",
                str(portal),
                "--handoff-root",
                str(handoff),
            ],
            capture_output=True,
            text=True,
            env=env,
            cwd=str(ROOT),
        )
        return result, record

    @staticmethod
    def _pipeline(tmp: Path, discovery: dict) -> dict:
        return {
            "store_root": str(Path(tmp) / "runs"),
            "repositories": {"owner/model": str(Path(tmp) / "workspaces" / "model")},
            "discovery": discovery,
            "targets": {"m3.0": {"repository_slug": "owner/model", "base": "main"}},
        }

    def test_non_empty_record_roots_serialize_as_strings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root_dir = Path(tmp) / "specifications"
            root_dir.mkdir()
            result, record = self._run(Path(tmp), self._pipeline(Path(tmp), {"record_roots": [str(root_dir)]}))
            self.assertEqual(result.returncode, 0, result.stderr)
            row = json.loads(record.read_text(encoding="utf-8"))["solidworks_windows"]
            extra = json.loads(row["extra"])
            self.assertEqual(extra["discovery"]["record_roots"], [str(root_dir)])
            self.assertTrue(all(isinstance(item, str) for item in extra["discovery"]["record_roots"]))
            self.assertEqual(extra["store_root"], str(Path(tmp) / "runs"))
            self.assertEqual(extra["repositories"], {"owner/model": str(Path(tmp) / "workspaces" / "model")})
            self.assertNotIn("PosixPath", row["extra"])

    def test_frozen_names_file_serializes_as_string(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root_dir = Path(tmp) / "specifications"
            root_dir.mkdir()
            registry = Path(tmp) / "frozen-names.json"
            registry.write_text("{}\n", encoding="utf-8")
            discovery = {"record_roots": [str(root_dir)], "frozen_names_file": str(registry)}
            result, record = self._run(Path(tmp), self._pipeline(Path(tmp), discovery))
            self.assertEqual(result.returncode, 0, result.stderr)
            extra = json.loads(json.loads(record.read_text(encoding="utf-8"))["solidworks_windows"]["extra"])
            self.assertEqual(extra["discovery"]["record_roots"], [str(root_dir)])
            self.assertEqual(extra["discovery"]["frozen_names_file"], str(registry))

    def test_extra_round_trips_and_reads_back(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root_dir = Path(tmp) / "specifications"
            root_dir.mkdir()
            pipeline = self._pipeline(Path(tmp), {"record_roots": [str(root_dir)]})
            first, record = self._run(Path(tmp), pipeline)
            self.assertEqual(first.returncode, 0, first.stderr)
            raw_first = json.loads(record.read_text(encoding="utf-8"))["solidworks_windows"]["extra"]
            second, _ = self._run(Path(tmp), pipeline)
            self.assertEqual(second.returncode, 0, second.stderr)
            raw_second = json.loads(record.read_text(encoding="utf-8"))["solidworks_windows"]["extra"]
            self.assertEqual(json.loads(raw_second), json.loads(raw_first))
            self.assertEqual(
                json.dumps(json.loads(raw_second), sort_keys=True),
                json.dumps(json.loads(raw_second), sort_keys=True),
            )
            trace = (record.parent / "connection.json.trace").read_text(encoding="utf-8").splitlines()
            self.assertTrue(any(json.loads(line)["found"] for line in trace))

    def test_empty_discovery_stays_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result, record = self._run(Path(tmp), self._pipeline(Path(tmp), {}))
            self.assertEqual(result.returncode, 0, result.stderr)
            extra = json.loads(json.loads(record.read_text(encoding="utf-8"))["solidworks_windows"]["extra"])
            self.assertEqual(extra["discovery"], {})


if __name__ == "__main__":
    unittest.main()
