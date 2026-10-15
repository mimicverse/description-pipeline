"""Observe-only CAD session facts for the IF-09 health identity (no CAD, no termination)."""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from description_pipeline.sources.solidworks import isolation

EXE = "/fixture/SLDWORKS.exe"
_DEFAULT = object()


class CadSessionFactsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory(prefix="cad-facts-")
        self.addCleanup(self.directory.cleanup)
        self.ledger = Path(self.directory.name) / "cad-ownership.json"
        # The resolver only restates a bound domain: pin this test's ledger.
        pin = mock.patch.dict(os.environ, {"DESCRIPTION_CAD_OWNERSHIP_FILE": str(self.ledger)})
        pin.start()
        self.addCleanup(pin.stop)

    def _record(
        self,
        pid: int,
        *,
        creation: int = 100,
        boot: float = 1000.0,
        owner: str | None = None,
        executable: str | None = None,
        owner_pid: int | None = _DEFAULT,
        owner_creation: int | None = _DEFAULT,
    ) -> None:
        owner_pid = os.getpid() if owner_pid is _DEFAULT else owner_pid
        owner_creation = 100 if owner_creation is _DEFAULT else owner_creation
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
        reader = creation if creation is not None else (lambda pid: 100)
        return isolation.cad_session_facts(
            EXE,
            process_table=table,
            creation_time=reader,
            boot_epoch=lambda: boot,
            ownership_file=self.ledger,
        )

    def test_a_proven_own_launch_is_one_owned_session(self) -> None:
        self._record(7)
        facts = self._facts(table=[(7, EXE)])
        self.assertEqual(facts["owned_sessions"], 1)
        # lease_held reflects the actual domain lease, never the owned count.
        self.assertFalse(facts["lease_held"])
        self.assertFalse(facts["external_session_detected"])
        self.assertEqual(facts["problems"], [])

    def test_lease_held_reflects_the_domain_lease_not_the_owned_count(self) -> None:
        lease = isolation.domain_lease(self.ledger)
        lease.acquire()
        self.addCleanup(lease.release)
        self._record(7)
        with_lease = self._facts(table=[(7, EXE)])
        self.assertEqual(with_lease["owned_sessions"], 1)
        self.assertTrue(with_lease["lease_held"])
        without_launch = self._facts(table=[])
        self.assertEqual(without_launch["owned_sessions"], 0)
        self.assertTrue(without_launch["lease_held"])

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

    @unittest.skipUnless(
        os.name == "posix" and hasattr(os, "geteuid") and os.geteuid() != 0,
        "real POSIX permission boundary",
    )
    def test_an_unreadable_lease_file_is_unknown_never_a_proven_holder(self) -> None:
        # A chmod-000 lease file makes the kernel probe raise (open EACCES):
        # that is an observation error, not evidence of a lock holder - the
        # fact stays unknown and the health identity says why.
        lease_path = Path(str(self.ledger) + ".lease")
        lease_path.write_text("diagnostic-only", encoding="utf-8")
        lease_path.chmod(0)
        try:
            facts = isolation.domain_lease_facts(self.ledger)
            cad = self._facts(table=[])
        finally:
            lease_path.chmod(0o600)
        self.assertIsNone(facts.get("held"), facts)
        self.assertTrue(facts.get("error"), facts)
        self.assertFalse(cad["lease_held"])
        self.assertIn("cad_lease_unreadable", cad["problems"])

    @unittest.skipUnless(os.name == "posix", "fcntl.flock boundary")
    def test_only_proven_flock_contention_reads_held(self) -> None:
        # By ACTUAL errno: only EAGAIN/EWOULDBLOCK are proven contention.
        # EINPROGRESS/EALREADY are BlockingIOError instances too, and every
        # other flock failure is an observation error: the fact stays unknown
        # and the health identity says why.  (Synthetic OS results only.)
        import errno

        from description_pipeline import io

        lease_path = Path(str(self.ledger) + ".lease")
        lease_path.write_text("no-real-holder", encoding="utf-8")
        for label in ("EINPROGRESS", "EALREADY", "EIO", "EACCES", "ENOLCK"):
            with self.subTest(errno=label):
                with (
                    mock.patch("fcntl.flock", side_effect=OSError(getattr(errno, label), f"injected flock {label}")),
                    self.assertRaises(OSError),
                ):
                    io.try_acquire_process_lock(lease_path)
                with mock.patch(
                    "fcntl.flock", side_effect=OSError(getattr(errno, label), f"injected flock {label}")
                ):
                    facts = isolation.domain_lease_facts(self.ledger)
                    cad = self._facts(table=[])
                self.assertIsNone(facts.get("held"), facts)
                self.assertTrue(facts.get("error"), facts)
                self.assertFalse(cad["lease_held"])
                self.assertIn("cad_lease_unreadable", cad["problems"])
        with mock.patch("fcntl.flock", side_effect=OSError(errno.EAGAIN, "injected flock EAGAIN")):
            facts = isolation.domain_lease_facts(self.ledger)
            cad = self._facts(table=[])
        self.assertTrue(facts.get("held") is True and not facts.get("error"), facts)
        self.assertTrue(cad["lease_held"])
        self.assertNotIn("cad_lease_unreadable", cad["problems"])

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

    def test_another_live_owner_pid_with_a_matching_creation_is_never_owned(self) -> None:
        # The fake reader reports creation 100 for every pid, so the recorded
        # owner identity (pid 999, creation 100) is internally consistent and
        # its process is "live" - yet it is not THIS endpoint process.
        self._record(7, owner_pid=999, owner_creation=100)
        facts = self._facts(table=[(7, EXE)])
        self.assertEqual(facts["owned_sessions"], 0)
        self.assertFalse(facts["lease_held"])
        self.assertTrue(facts["external_session_detected"])
        self.assertIn("cad_identity_unverified", facts["problems"])


if __name__ == "__main__":
    unittest.main()
