"""Resource-domain session lease: exclusivity, refcounting and the non-native
concurrency counterexamples for the unified CAD domain (no CAD, no Windows)."""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from . import _paths  # noqa: F401
from description_pipeline.sources.solidworks import isolation  # noqa: E402
from description_pipeline.sources.solidworks.errors import EnvironmentError_  # noqa: E402


def _record(pid: int) -> dict:
    return {
        "pid": pid,
        "executable": "D:/SW/SLDWORKS.exe",
        "creation_time": 1000000 + pid,
        "boot_epoch": 111.0,
        "owner": "test-owner",
        "owner_pid": os.getpid(),
        "owner_creation_time": 555,
        "recorded_at": 1.0,
    }


class _Handle:
    def __init__(self):
        self.closed = False

    def Close(self):  # noqa: N802 - pywin32 handle API
        self.closed = True


def _win32_module_fakes() -> dict:
    win32api = SimpleNamespace(TerminateProcess=Mock())
    win32job = SimpleNamespace(
        CreateJobObject=Mock(return_value=_Handle()),
        QueryInformationJobObject=Mock(return_value={"BasicLimitInformation": {"LimitFlags": 0}}),
        SetInformationJobObject=Mock(),
        AssignProcessToJobObject=Mock(),
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE=0x2000,
        JobObjectExtendedLimitInformation=9,
    )
    win32process = SimpleNamespace(
        CreateProcess=Mock(return_value=(_Handle(), _Handle(), 4242, 0)),
        ResumeThread=Mock(),
        CREATE_SUSPENDED=0x4,
        STARTUPINFO=Mock(return_value=object()),
    )
    win32event = SimpleNamespace(WaitForSingleObject=Mock(return_value=0))
    return {
        "win32api": win32api,
        "win32job": win32job,
        "win32process": win32process,
        "win32event": win32event,
    }


class DomainLeaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ledger = Path(self.tmp.name) / "cad-ownership.json"

    def test_domain_lease_is_exclusive_across_handles_and_reports_the_holder(self):
        holder = isolation.domain_lease(self.ledger)
        holder.acquire()
        self.addCleanup(holder.release)
        peer = isolation.DomainLease(isolation._lease_path(self.ledger))
        with self.assertRaises(EnvironmentError_) as caught:
            peer.acquire()
        self.assertEqual(caught.exception.code, "cad_domain_leased")
        self.assertIn(str(os.getpid()), str(caught.exception.detail.get("holder_pid")))
        holder.release()
        freed = isolation.DomainLease(isolation._lease_path(self.ledger))
        freed.acquire()
        freed.release()

    def test_one_lease_object_refcounts_two_sessions(self):
        lease = isolation.domain_lease(self.ledger)
        lease.acquire()
        lease.acquire()
        lease.release()
        self.assertTrue(lease.receipt()["held"])
        peer = isolation.DomainLease(isolation._lease_path(self.ledger))
        with self.assertRaises(EnvironmentError_):
            peer.acquire()
        lease.release()
        self.assertFalse(lease.receipt()["held"])
        freed = isolation.DomainLease(isolation._lease_path(self.ledger))
        freed.acquire()
        freed.release()

    def test_counterexample_without_the_lease_two_actors_overlap(self):
        """Pre-lease mechanism: the launch-time check is check-then-act.

        Two actors can both observe "no foreign process" before either records
        ownership, and nothing holds the domain for a session's lifetime - the
        overlap is representable, i.e. the old mechanism has no mutex to breach.
        """

        store = isolation.OwnershipStore(self.ledger)
        barrier = threading.Barrier(2)
        observed: list[bool] = []

        def actor(pid: int) -> None:
            seen = store.load()
            observed.append(bool(seen))
            barrier.wait(5)
            store.add(_record(pid))

        threads = [threading.Thread(target=actor, args=(pid,)) for pid in (11, 22)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertEqual(observed, [False, False])
        self.assertEqual(sorted(isolation.OwnershipStore(self.ledger).load()), [11, 22])

    def test_counterexample_is_refused_once_the_domain_is_leased(self):
        holder = isolation.domain_lease(self.ledger)
        holder.acquire()
        self.addCleanup(holder.release)
        peer = isolation.DomainLease(isolation._lease_path(self.ledger))
        with self.assertRaises(EnvironmentError_) as caught:
            peer.acquire()
        self.assertEqual(caught.exception.code, "cad_domain_leased")
        self.assertEqual(sorted(isolation.OwnershipStore(self.ledger).load()), [])

    def test_windows_cad_process_holds_the_domain_lease_until_close(self):
        lease_path = isolation._lease_path(self.ledger)
        with (
            patch.dict(sys.modules, _win32_module_fakes()),
            patch.object(isolation, "terminate_remnant_processes", return_value=[]),
            patch.object(isolation.WindowsCadProcess, "_creation_time", return_value=4242),
            patch.object(isolation, "_windows_creation_time", return_value=555),
            patch.object(isolation, "_windows_boot_epoch", return_value=111.0),
        ):
            process = isolation.WindowsCadProcess("D:/SW/SLDWORKS.exe", ownership_file=self.ledger)
            receipt = process.lease_receipt()
            self.assertTrue(receipt["held"])
            self.assertEqual(receipt["path"], str(lease_path))
            peer = isolation.DomainLease(lease_path)
            with self.assertRaises(EnvironmentError_) as caught:
                peer.acquire()
            self.assertEqual(caught.exception.code, "cad_domain_leased")
            process.close()
        self.assertFalse(process.lease_receipt()["held"])
        freed = isolation.DomainLease(lease_path)
        freed.acquire()
        freed.release()


if __name__ == "__main__":
    unittest.main()
