"""The deployment writer persists one IF-11 snapshot reference and nothing else."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from description_pipeline import interface_records

from .config_identity_support import activate, connection_document, run_writer


class AddConnectionReferenceTests(unittest.TestCase):
    def test_writer_persists_exactly_the_active_snapshot_reference(self) -> None:
        with tempfile.TemporaryDirectory(prefix="add-connection-") as tmp:
            root = Path(tmp)
            snapshot, _record = activate(root, "gen-1")
            result, row = run_writer(root, snapshot)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(row["host"], "")
            self.assertIsNone(row["port"])
            self.assertEqual(row["password"], "")
            self.assertIsNone(row["login"])
            self.assertIsNone(row["schema"])
            reference = json.loads(row["extra"])
            self.assertEqual(set(reference), {"config_identity"})
            self.assertEqual(
                reference["config_identity"],
                {
                    "schema_version": interface_records.CONFIG_SNAPSHOT_SCHEMA,
                    "path": str(snapshot),
                    "sha256": hashlib.sha256(snapshot.read_bytes()).hexdigest(),
                },
            )
            self.assertEqual(
                interface_records.validate_connection_config_identity(connection_document(row)),
                reference["config_identity"],
            )
            serialized = json.dumps(row, sort_keys=True)
            self.assertNotIn("store_root", serialized)
            self.assertNotIn("repositories", serialized)
            self.assertNotIn("handoff_roots", serialized)
            self.assertNotIn("t" * 64, serialized)

    def test_writer_is_idempotent_for_the_same_active_snapshot(self) -> None:
        with tempfile.TemporaryDirectory(prefix="add-connection-") as tmp:
            root = Path(tmp)
            snapshot, _record = activate(root, "gen-1")
            first, row_first = run_writer(root, snapshot)
            second, row_second = run_writer(root, snapshot)
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(row_first, row_second)

    def test_writer_refuses_a_snapshot_that_is_not_active(self) -> None:
        with tempfile.TemporaryDirectory(prefix="add-connection-") as tmp:
            root = Path(tmp)
            stale, _record = activate(root, "gen-1")
            active, _record = activate(root, "gen-2")
            self.assertNotEqual(stale, active)
            result, row = run_writer(root, stale)
            self.assertEqual(result.returncode, 1)
            self.assertIn("not the active install record's snapshot", result.stderr)
            self.assertEqual(row, {})

    def test_writer_can_bind_a_projected_record_before_activation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="add-connection-") as tmp:
            root = Path(tmp)
            snapshot, _record = activate(root, "gen-1")
            active = root / "state" / "install-record.json"
            prepared = root / "prepared-install-record.json"
            prepared.write_bytes(active.read_bytes())
            prepared.chmod(0o600)
            active.unlink()
            result, row = run_writer(root, snapshot, prepared)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(active.exists())
            self.assertEqual(row["host"], "")
            self.assertIsNone(row["port"])
            self.assertIn('"config_identity"', row["extra"])


if __name__ == "__main__":
    unittest.main()
