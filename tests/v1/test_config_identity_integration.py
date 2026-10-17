"""Writer -> canonical validator -> health/DAG resolver integration."""

from __future__ import annotations

import hashlib
import os
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from description_pipeline import interface_records
from description_pipeline.orchestration.airflow_client import (
    EndpointProtocolError,
    config_from_airflow_connection,
)

from .config_identity_support import (
    activate,
    connection_document,
    platform_document,
    run_health_resolver,
    run_writer,
)


class _Connection:
    def __init__(self, row: dict) -> None:
        self.host = row.get("host") or ""
        self.port = row.get("port")
        self.password = row.get("password") or ""
        extra = row.get("extra") or {}
        self.extra_dejson = extra if isinstance(extra, dict) else json.loads(extra)


def _patch_connection(row: dict):
    class _ConnectionId:
        def __eq__(self, other):
            return ("conn_id", other)

    class FakeConnection:
        conn_id = _ConnectionId()

    class FakeQuery:
        def filter(self, expression):
            return self

        def one_or_none(self):
            return _Connection(row)

    class FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def query(self, model):
            return FakeQuery()

    airflow = types.ModuleType("airflow")
    models = types.ModuleType("airflow.models")
    connection_module = types.ModuleType("airflow.models.connection")
    settings = types.ModuleType("airflow.settings")
    connection_module.Connection = FakeConnection
    settings.Session = FakeSession
    return mock.patch.dict(
        sys.modules,
        {
            "airflow": airflow,
            "airflow.models": models,
            "airflow.models.connection": connection_module,
            "airflow.settings": settings,
        },
    )


class ConfigIdentityIntegrationTests(unittest.TestCase):
    def test_real_writer_result_is_accepted_by_validator_health_and_dag_resolver(self) -> None:
        with tempfile.TemporaryDirectory(prefix="config-identity-integration-") as tmp:
            root = Path(tmp)
            snapshot, _record = activate(root, "gen-1")
            writer, row = run_writer(root, snapshot)
            self.assertEqual(writer.returncode, 0, writer.stderr)

            reference = interface_records.validate_connection_config_identity(connection_document(row))
            self.assertEqual(reference["path"], str(snapshot))
            self.assertEqual(reference["sha256"], hashlib.sha256(snapshot.read_bytes()).hexdigest())

            health = run_health_resolver(root, row)
            self.assertEqual(health.returncode, 0, health.stderr)
            self.assertIn(f"config_digest={reference['sha256']}", health.stdout)

            with _patch_connection(row):
                config = config_from_airflow_connection("solidworks_windows", state_root=root / "state")
            document = json.loads(snapshot.read_text(encoding="utf-8"))
            self.assertEqual(config.config_digest, reference["sha256"])
            self.assertEqual(
                config.base_url,
                f"http://127.0.0.1:{document['resolved']['windows']['tunnel_loopback_port']}",
            )
            self.assertEqual(config.store_root, Path(document["resolved"]["derived"]["store_root"]))
            self.assertEqual(config.targets, document["resolved"]["targets"])
            self.assertEqual(config.handoff_roots, (Path(document["resolved"]["storage"]["handoff_root"]),))
            self.assertEqual(
                config.repositories,
                {slug: Path(path) for slug, path in document["resolved"]["derived"]["repositories"].items()},
            )

    def test_frozen_request_keeps_its_snapshot_after_active_pointer_and_connection_change(self) -> None:
        with tempfile.TemporaryDirectory(prefix="config-identity-frozen-") as tmp:
            root = Path(tmp)
            document_a = platform_document(root)
            document_a["targets"]["m3.0"]["base"] = "release-a"
            snapshot_a, _record = activate(root, "gen-1", document_a)
            first, row_a = run_writer(root, snapshot_a)
            self.assertEqual(first.returncode, 0, first.stderr)

            document_b = platform_document(root)
            document_b["targets"]["m3.0"]["base"] = "release-b"
            snapshot_b, _record_b = activate(root, "gen-2", document_b)
            second, row_b = run_writer(root, snapshot_b)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertNotEqual(row_a, row_b)

            (root / "state" / "install-record.json").unlink()
            with mock.patch.dict(
                sys.modules,
                {
                    "airflow": None,
                    "airflow.hooks": None,
                    "airflow.hooks.base": None,
                },
            ):
                frozen = config_from_airflow_connection(
                    "solidworks_windows",
                    config_digest=hashlib.sha256(snapshot_a.read_bytes()).hexdigest(),
                    state_root=root / "state",
                )
            self.assertEqual(frozen.config_digest, hashlib.sha256(snapshot_a.read_bytes()).hexdigest())
            self.assertEqual(frozen.targets["m3.0"]["base"], "release-a")
            self.assertNotEqual(frozen.targets["m3.0"]["base"], document_b["targets"]["m3.0"]["base"])

    def test_credential_rotation_reads_the_snapshot_file_without_rewriting_connection(self) -> None:
        with tempfile.TemporaryDirectory(prefix="config-identity-rotation-") as tmp:
            root = Path(tmp)
            snapshot, _record = activate(root, "gen-1")
            writer, row = run_writer(root, snapshot)
            self.assertEqual(writer.returncode, 0, writer.stderr)
            before = connection_document(row)
            token_file = root / "secrets" / "endpoint.token"
            token_file.write_text("r" * 64 + "\n", encoding="utf-8")
            token_file.chmod(0o600)
            frozen = config_from_airflow_connection(
                "solidworks_windows",
                config_digest=hashlib.sha256(snapshot.read_bytes()).hexdigest(),
                state_root=root / "state",
            )
            self.assertEqual(frozen.token, "r" * 64)
            self.assertEqual(connection_document(row), before)

    def test_legacy_or_inactive_active_reference_is_refused_for_new_work(self) -> None:
        with tempfile.TemporaryDirectory(prefix="config-identity-negative-") as tmp:
            root = Path(tmp)
            snapshot, _record = activate(root, "gen-1")
            _writer, row = run_writer(root, snapshot)
            legacy = {
                "host": "127.0.0.1",
                "port": 18765,
                "password": "t" * 64,
                "extra": {"store_root": str(root / "runs")},
            }
            with _patch_connection(legacy), self.assertRaisesRegex(
                EndpointProtocolError, "config_identity_legacy"
            ):
                config_from_airflow_connection("solidworks_windows", state_root=root / "state")

            stale = json.loads(row["extra"])
            stale["config_identity"]["sha256"] = "0" * 64
            with _patch_connection(
                {**row, "extra": json.dumps(stale)}
            ), self.assertRaisesRegex(EndpointProtocolError, "active config snapshot"):
                config_from_airflow_connection("solidworks_windows", state_root=root / "state")

    def test_frozen_snapshot_rejects_wrong_digest_mode_and_symlink(self) -> None:
        with tempfile.TemporaryDirectory(prefix="config-identity-frozen-negative-") as tmp:
            root = Path(tmp)
            snapshot, _record = activate(root, "gen-1")
            digest = hashlib.sha256(snapshot.read_bytes()).hexdigest()
            with self.assertRaisesRegex(EndpointProtocolError, "does not identify exactly one snapshot"):
                config_from_airflow_connection(
                    "solidworks_windows", config_digest="0" * 64, state_root=root / "state"
                )

            snapshot.chmod(0o644)
            with self.assertRaisesRegex(EndpointProtocolError, "config_identity_unreadable"):
                config_from_airflow_connection(
                    "solidworks_windows", config_digest=digest, state_root=root / "state"
                )

            snapshot.chmod(0o600)
            real = root / "gen-1.real.json"
            snapshot.rename(real)
            link = snapshot.with_name("alias.json")
            link.symlink_to(real)
            with self.assertRaisesRegex(EndpointProtocolError, "does not identify exactly one snapshot"):
                config_from_airflow_connection(
                    "solidworks_windows", config_digest=digest, state_root=root / "state"
                )

    def test_state_root_cannot_be_inferred_from_the_connection_reference(self) -> None:
        with tempfile.TemporaryDirectory(prefix="config-identity-root-") as tmp:
            root = Path(tmp)
            snapshot, _record = activate(root, "gen-1")
            _writer, row = run_writer(root, snapshot)
            with _patch_connection(row), mock.patch.dict(
                os.environ, {}, clear=True
            ), self.assertRaisesRegex(EndpointProtocolError, "OPERATOR_STATE"):
                config_from_airflow_connection("solidworks_windows")


if __name__ == "__main__":
    unittest.main()
