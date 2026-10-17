"""Cross-generation Airflow Connection capture/restore proof."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from .config_identity_support import (
    activate,
    platform_document,
    run_writer,
    runtime_env,
    stub_directory,
)

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "deploy" / "operator" / "connection_snapshot.py"
BACKUP = ROOT / "deploy" / "operator" / "backup.py"
MEMBER = "private/connection-before-promotion.json"
TOKEN = "t" * 64


def _write_record(path: Path, rows: dict) -> None:
    path.write_text(json.dumps(rows, sort_keys=True), encoding="utf-8")


def _stub_env(root: Path, record: Path) -> dict[str, str]:
    env = runtime_env(root)
    env.update(
        STUB_AIRFLOW_RECORD=str(record),
        PYTHONPATH=os.pathsep.join([str(stub_directory(root)), str(ROOT), str(ROOT / "src")]),
    )
    return env


def _tool(root: Path, record: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(TOOL), *arguments],
        capture_output=True,
        text=True,
        env=_stub_env(root, record),
        cwd=str(ROOT),
    )


def _python(root: Path, record: Path, script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        env=_stub_env(root, record),
        cwd=str(ROOT),
    )


def _legacy_reader(root: Path, record: Path) -> subprocess.CompletedProcess[str]:
    return _python(
        root,
        record,
        "\n".join(
            [
                "import json",
                "from tests.v1.legacy_c663_reader import config_from_airflow_connection",
                "try:",
                "    config = config_from_airflow_connection('solidworks_windows')",
                "except Exception as error:",
                "    print('ERROR:' + type(error).__name__ + ':' + str(error))",
                "    raise SystemExit(3)",
                "print(json.dumps({",
                "    'base_url': config.base_url,",
                "    'token': config.token,",
                "    'store_root': str(config.store_root),",
                "    'targets': config.targets,",
                "    'handoff_roots': [str(item) for item in config.handoff_roots],",
                "}, sort_keys=True))",
            ]
        ),
    )


def _new_reader(root: Path, record: Path) -> subprocess.CompletedProcess[str]:
    return _python(
        root,
        record,
        "\n".join(
            [
                "import json",
                "from pathlib import Path",
                "from description_pipeline.orchestration.airflow_client import config_from_airflow_connection",
                "try:",
                "    config = config_from_airflow_connection('solidworks_windows', state_root=Path("
                + repr(str(root / "state"))
                + "))",
                "except Exception as error:",
                "    print('ERROR:' + type(error).__name__ + ':' + str(error))",
                "    raise SystemExit(3)",
                "print(json.dumps({'config_digest': config.config_digest, 'targets': config.targets}, sort_keys=True))",
            ]
        ),
    )


def _receipt(
    path: Path,
    *,
    install_id: str,
    source_record: Path,
    target_snapshot: Path,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": "solidworks-to-urdf.promotion/v1",
                "install_id": install_id,
                "from": str(source_record),
                "from_sha256": hashlib.sha256(source_record.read_bytes()).hexdigest(),
                "to": str(target_snapshot),
                "to_sha256": hashlib.sha256(target_snapshot.read_bytes()).hexdigest(),
                "plan": ["config-reference"],
                "created_at": "2026-10-17T00:00:00Z",
                "steps": [],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def _manifest(root: Path, snapshot: Path, receipt_dir: Path) -> tuple[Path, Path]:
    staged = receipt_dir / "backup"
    (staged / "private").mkdir(parents=True, exist_ok=True)
    (staged / MEMBER).write_bytes(snapshot.read_bytes())
    (staged / MEMBER).chmod(0o600)
    sealed = receipt_dir / "backup.tar.enc"
    sealed.write_bytes(b"sealed backup bytes")
    key = root / "state" / "secrets" / "backup.key"
    key.parent.mkdir(parents=True, exist_ok=True)
    manifest = receipt_dir / "evidence" / "backup-manifest.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    for arguments in (
        ["key", "--path", str(key)],
        [
            "manifest",
            "--source",
            str(staged),
            "--sealed",
            str(sealed),
            "--key",
            str(key),
            "--out",
            str(manifest),
        ],
    ):
        result = subprocess.run(
            [sys.executable, str(BACKUP), *arguments],
            capture_output=True,
            text=True,
            cwd=str(ROOT),
        )
        if result.returncode != 0:
            raise AssertionError(result.stdout + result.stderr)
    return manifest, sealed


def _legacy_row(root: Path) -> dict:
    return {
        "conn_id": "solidworks_windows",
        "conn_type": "http",
        "description": "legacy endpoint row",
        "host": "127.0.0.1",
        "login": None,
        "password": TOKEN,
        "schema": None,
        "port": 18765,
        "extra": json.dumps(
            {
                "handoff_roots": [str(root / "handoffs")],
                "store_root": str(root / "runs"),
                "repositories": {"owner/model": str(root / "workspaces" / "model-repository")},
                "discovery": {},
                "targets": {"m3.0": {"repository_slug": "owner/model", "base": "release-a"}},
            },
            sort_keys=True,
        ),
    }


class ConnectionSnapshotTests(unittest.TestCase):
    def _fixture(self, root: Path) -> tuple[dict, Path, Path, Path, Path, Path]:
        document_a = platform_document(root)
        document_a["targets"]["m3.0"]["base"] = "release-a"
        _snapshot_a, record_a = activate(root, "gen-1", document_a)
        records = root / "state" / "install-records"
        records.mkdir(parents=True, exist_ok=True)
        source_record = records / "gen-1.json"
        source_record.write_text(json.dumps(record_a, indent=2, sort_keys=True) + "\n", encoding="utf-8")

        document_b = platform_document(root)
        document_b["targets"]["m3.0"]["base"] = "release-b"
        snapshot_b, _record_b = activate(root, "gen-2", document_b)
        receipt_dir = root / "state" / "promotions" / "gen-2"
        receipt = _receipt(
            receipt_dir / "receipt.json",
            install_id="gen-2",
            source_record=source_record,
            target_snapshot=snapshot_b,
        )
        record = root / "airflow-connection.json"
        row = _legacy_row(root)
        _write_record(record, {"solidworks_windows": row})
        return row, source_record, snapshot_b, receipt, receipt_dir, record

    def test_old_reader_consumes_the_restored_row_and_new_reader_refuses_it(self) -> None:
        with tempfile.TemporaryDirectory(prefix="connection-snapshot-") as tmp:
            root = Path(tmp)
            legacy, _source, snapshot_b, receipt, receipt_dir, record = self._fixture(root)
            private = receipt_dir / "evidence" / "private" / "connection-before-promotion.json"
            captured = _tool(root, record, "capture", "--file", str(private), "--receipt", str(receipt))
            self.assertEqual(captured.returncode, 0, captured.stdout + captured.stderr)
            self.assertEqual(stat.S_IMODE(private.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(private.parent.stat().st_mode), 0o700)

            writer, new_row = run_writer(root, snapshot_b)
            self.assertEqual(writer.returncode, 0, writer.stdout + writer.stderr)
            self.assertEqual(new_row["host"], "")
            self.assertIsNone(new_row["port"])
            self.assertEqual(new_row["password"], "")
            self.assertIn("config_identity", json.loads(new_row["extra"]))

            old_on_new = _legacy_reader(root, record)
            self.assertNotEqual(old_on_new.returncode, 0, old_on_new.stdout + old_on_new.stderr)
            self.assertIn("has no host", old_on_new.stdout)
            new_on_new = _new_reader(root, record)
            self.assertEqual(new_on_new.returncode, 0, new_on_new.stdout + new_on_new.stderr)
            self.assertEqual(
                json.loads(new_on_new.stdout)["config_digest"],
                hashlib.sha256(snapshot_b.read_bytes()).hexdigest(),
            )

            manifest, sealed = _manifest(root, private, receipt_dir)
            self.assertNotIn(TOKEN, manifest.read_text(encoding="utf-8"))
            restored = _tool(
                root,
                record,
                "restore",
                "--file",
                str(private),
                "--receipt",
                str(receipt),
                "--manifest",
                str(manifest),
                "--sealed",
                str(sealed),
                "--member",
                MEMBER,
            )
            self.assertEqual(restored.returncode, 0, restored.stdout + restored.stderr)
            self.assertIn("restored exactly", restored.stdout)

            restored_row = json.loads(record.read_text(encoding="utf-8"))["solidworks_windows"]
            self.assertEqual(restored_row, legacy)
            old_on_restored = _legacy_reader(root, record)
            self.assertEqual(old_on_restored.returncode, 0, old_on_restored.stdout + old_on_restored.stderr)
            legacy_config = json.loads(old_on_restored.stdout)
            self.assertEqual(legacy_config["base_url"], "http://127.0.0.1:18765")
            self.assertEqual(legacy_config["token"], TOKEN)
            self.assertEqual(legacy_config["store_root"], str(root / "runs"))
            self.assertEqual(legacy_config["targets"]["m3.0"]["base"], "release-a")

            new_on_restored = _new_reader(root, record)
            self.assertNotEqual(new_on_restored.returncode, 0, new_on_restored.stdout + new_on_restored.stderr)
            self.assertIn("config_identity_legacy", new_on_restored.stdout)

    def test_capture_is_never_overwritten_and_restore_does_not_rewrite_an_unchanged_row(self) -> None:
        with tempfile.TemporaryDirectory(prefix="connection-snapshot-noop-") as tmp:
            root = Path(tmp)
            _legacy, _source, _snapshot, receipt, receipt_dir, record = self._fixture(root)
            private = receipt_dir / "evidence" / "private" / "connection-before-promotion.json"
            first = _tool(root, record, "capture", "--file", str(private), "--receipt", str(receipt))
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
            original = private.read_bytes()
            second = _tool(root, record, "capture", "--file", str(private), "--receipt", str(receipt))
            self.assertNotEqual(second.returncode, 0)
            self.assertEqual(private.read_bytes(), original)

            manifest, sealed = _manifest(root, private, receipt_dir)
            before = record.read_bytes()
            restored = _tool(
                root,
                record,
                "restore",
                "--file",
                str(private),
                "--receipt",
                str(receipt),
                "--manifest",
                str(manifest),
                "--sealed",
                str(sealed),
                "--member",
                MEMBER,
            )
            self.assertEqual(restored.returncode, 0, restored.stdout + restored.stderr)
            self.assertIn("nothing was written", restored.stdout)
            self.assertEqual(record.read_bytes(), before)

    def test_corrupt_or_wrong_generation_capture_is_refused(self) -> None:
        with tempfile.TemporaryDirectory(prefix="connection-snapshot-refuse-") as tmp:
            root = Path(tmp)
            _legacy, _source, snapshot_b, receipt, receipt_dir, record = self._fixture(root)
            private = receipt_dir / "evidence" / "private" / "connection-before-promotion.json"
            captured = _tool(root, record, "capture", "--file", str(private), "--receipt", str(receipt))
            self.assertEqual(captured.returncode, 0, captured.stdout + captured.stderr)
            manifest, sealed = _manifest(root, private, receipt_dir)
            original = private.read_bytes()

            private.write_bytes(original + b"\n")
            corrupted = _tool(
                root,
                record,
                "restore",
                "--file",
                str(private),
                "--receipt",
                str(receipt),
                "--manifest",
                str(manifest),
                "--sealed",
                str(sealed),
                "--member",
                MEMBER,
            )
            self.assertNotEqual(corrupted.returncode, 0)
            self.assertIn("does not match the original backup manifest", corrupted.stderr)

            private.write_bytes(original)
            document = json.loads(private.read_text(encoding="utf-8"))
            body = json.dumps(document, sort_keys=True, separators=(",", ":"))
            duplicate = body.replace(
                '"conn_id":"solidworks_windows"',
                '"conn_id":"other","conn_id":"solidworks_windows"',
                1,
            )
            private.write_text(duplicate + "\n", encoding="utf-8")
            private.chmod(0o600)
            manifest, sealed = _manifest(root, private, receipt_dir)
            strict = _tool(
                root,
                record,
                "restore",
                "--file",
                str(private),
                "--receipt",
                str(receipt),
                "--manifest",
                str(manifest),
                "--sealed",
                str(sealed),
                "--member",
                MEMBER,
            )
            self.assertNotEqual(strict.returncode, 0)
            self.assertIn("Duplicate input key", strict.stderr)

            private.write_bytes(original)
            manifest, sealed = _manifest(root, private, receipt_dir)
            source = Path(json.loads(receipt.read_text(encoding="utf-8"))["from"])
            other_target = snapshot_b.with_name("other.json")
            other_target.write_bytes(snapshot_b.read_bytes() + b"\n")
            wrong_receipt = _receipt(
                receipt_dir / "wrong-receipt.json",
                install_id="gen-2",
                source_record=source,
                target_snapshot=other_target,
            )
            before = record.read_bytes()
            wrong = _tool(
                root,
                record,
                "restore",
                "--file",
                str(private),
                "--receipt",
                str(wrong_receipt),
                "--manifest",
                str(manifest),
                "--sealed",
                str(sealed),
                "--member",
                MEMBER,
            )
            self.assertNotEqual(wrong.returncode, 0)
            self.assertIn("another promotion generation", wrong.stderr)
            self.assertEqual(record.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
