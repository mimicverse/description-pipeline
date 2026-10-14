"""Observe-only CAD session facts for the IF-09 health identity (no CAD, no termination)."""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from description_pipeline.sources.solidworks import isolation

EXE = "/fixture/SLDWORKS.exe"


class CadSessionFactsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory(prefix="cad-facts-")
        self.addCleanup(self.directory.cleanup)
        self.ledger = Path(self.directory.name) / "cad-ownership.json"

    def _record(
        self,
        pid: int,
        *,
        creation: int = 100,
        boot: float = 1000.0,
        owner: str | None = None,
        executable: str | None = None,
        owner_pid: int | None = 99,
        owner_creation: int | None = 555,
    ) -> None:
        payload = {
            "pid": pid,
            "creation_time": creation,
            "boot_epoch": boot,
            "owner": owner if owner is not None else isolation._OWNER_TOKEN,
            "executable": executable if executable is not None else EXE,
        }
        if owner_pid is not None:
            payload["owner_pid"] = owner_pid
        if owner_creation is not None:
            payload["owner_creation_time"] = owner_creation
        isolation.OwnershipStore(self.ledger).add(payload)

    def _facts(self, *, table, creation=None, boot: float = 1000.0):
        reader = creation if creation is not None else (lambda pid: 555 if pid == 99 else 100)
        return isolation.cad_session_facts(
            EXE,
            process_table=table,
            creation_time=reader,
            boot_epoch=lambda: boot,
            ownership_file=self.ledger,
        )

    def test_a_proven_own_launch_is_one_owned_lease(self) -> None:
        self._record(7)
        facts = self._facts(table=[(7, EXE)])
        self.assertEqual(facts["owned_sessions"], 1)
        self.assertTrue(facts["lease_held"])
        self.assertFalse(facts["external_session_detected"])
        self.assertEqual(facts["problems"], [])

    def test_pid_reuse_is_never_counted_as_owned(self) -> None:
        self._record(7, creation=100)
        facts = self._facts(table=[(7, EXE)], creation=lambda pid: 200)
        self.assertEqual(facts["owned_sessions"], 0)
        self.assertFalse(facts["lease_held"])
        self.assertTrue(facts["external_session_detected"])
        self.assertEqual(facts["problems"], [])

    def test_a_record_from_another_boot_is_not_ours(self) -> None:
        self._record(7, boot=1000.0)
        facts = self._facts(table=[(7, EXE)], boot=9999.0)
        self.assertEqual(facts["owned_sessions"], 0)
        self.assertTrue(facts["external_session_detected"])

    def test_an_unreadable_creation_is_a_problem_never_a_quiet_zero(self) -> None:
        self._record(7)
        facts = self._facts(table=[(7, EXE)], creation=lambda pid: None)
        self.assertEqual(facts["owned_sessions"], 0)
        self.assertTrue(facts["external_session_detected"])
        self.assertIn("cad_identity_unverified", facts["problems"])

    def test_unrecorded_matching_processes_are_external(self) -> None:
        facts = self._facts(table=[(9, EXE)])
        self.assertEqual(facts["owned_sessions"], 0)
        self.assertTrue(facts["external_session_detected"])
        self.assertEqual(facts["problems"], [])

    def test_a_verified_process_the_endpoint_does_not_own_is_external(self) -> None:
        self._record(7, owner="another-pipeline-process")
        facts = self._facts(table=[(7, EXE)])
        self.assertEqual(facts["owned_sessions"], 0)
        self.assertTrue(facts["external_session_detected"])

    def test_a_tracked_launch_counts_owned_even_without_the_token(self) -> None:
        self._record(7, owner="another")
        with mock.patch.object(isolation, "_tracked_pids", return_value={7}):
            facts = self._facts(table=[(7, EXE)])
        self.assertEqual(facts["owned_sessions"], 1)
        self.assertFalse(facts["external_session_detected"])

    def test_a_quiet_host_reads_cleanly(self) -> None:
        facts = self._facts(table=[])
        self.assertEqual(
            facts,
            {"owned_sessions": 0, "lease_held": False, "external_session_detected": False, "problems": []},
        )

    def test_our_token_without_the_owner_identity_is_never_owned(self) -> None:
        self._record(7, owner_pid=None)
        facts = self._facts(table=[(7, EXE)])
        self.assertEqual(facts["owned_sessions"], 0)
        self.assertFalse(facts["lease_held"])
        self.assertTrue(facts["external_session_detected"])
        self.assertIn("cad_identity_unverified", facts["problems"])

    def test_a_swapped_owner_pid_is_never_owned(self) -> None:
        self._record(7, owner_pid=1000)
        facts = self._facts(table=[(7, EXE)])
        self.assertEqual(facts["owned_sessions"], 0)
        self.assertTrue(facts["external_session_detected"])
        self.assertIn("cad_identity_unverified", facts["problems"])

    def test_a_swapped_owner_creation_time_is_never_owned(self) -> None:
        self._record(7, owner_creation=999)
        facts = self._facts(table=[(7, EXE)])
        self.assertEqual(facts["owned_sessions"], 0)
        self.assertIn("cad_identity_unverified", facts["problems"])

    def test_a_record_for_another_executable_is_never_owned(self) -> None:
        self._record(7, executable="/fixture/OTHER.exe")
        facts = self._facts(table=[(7, EXE)])
        self.assertEqual(facts["owned_sessions"], 0)
        self.assertTrue(facts["external_session_detected"])
        self.assertIn("cad_identity_unverified", facts["problems"])


if __name__ == "__main__":
    unittest.main()
