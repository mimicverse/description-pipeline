from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock, patch

from description_pipeline.build.publication import ENTRIES, JOURNAL, _process_alive, publish, recover
from description_pipeline.io import PipelineError, write_json


class PublicationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "model"
        self.staging = Path(temporary.name) / "staging"
        for base, contents in ((self.root, "old"), (self.staging, "new")):
            for relative in ENTRIES:
                target = base / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(contents)

    def test_exception_rolls_back_every_installed_entry(self):
        replace = os.replace
        calls = 0

        def fail_once(source, destination):
            nonlocal calls
            calls += 1
            if calls == 7:
                raise OSError("simulated disk failure")
            replace(source, destination)

        with (
            patch("description_pipeline.build.publication.os.replace", side_effect=fail_once),
            self.assertRaises(OSError),
        ):
            publish(self.staging, self.root)
        self.assertTrue(all((self.root / name).read_text(encoding="utf-8") == "old" for name in ENTRIES))
        self.assertFalse((self.root / "build/publication.json").exists())

    def test_crash_between_backup_and_install_is_recoverable(self):
        backup = self.root / "build/previous-test"
        backup.mkdir(parents=True)
        write_json(
            self.root / "build/publication.json", {"pid": os.getpid(), "backup": backup.name, "previous": list(ENTRIES)}
        )
        shutil.move(self.root / "model", backup / "model")
        self.assertTrue(recover(self.root)["recovered"])
        self.assertTrue(all((self.root / name).read_text(encoding="utf-8") == "old" for name in ENTRIES))

    def test_active_other_publisher_is_not_recovered(self):
        write_json(self.root / "build/publication.json", {"pid": os.getpid() + 1})
        with (
            patch("description_pipeline.build.publication._process_alive", return_value=True),
            self.assertRaises(PipelineError),
        ):
            recover(self.root)

    def test_recovery_never_signals_a_live_owner_and_recovers_after_exit(self):
        owner = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE)
        try:
            self.assertTrue(_process_alive(owner.pid))
            backup = self.root / "build/previous-exited"
            backup.mkdir(parents=True)
            write_json(
                self.root / "build/publication.json",
                {
                    "pid": owner.pid,
                    "backup": backup.name,
                    "previous": list(ENTRIES),
                },
            )
            with self.assertRaisesRegex(PipelineError, "active"):
                recover(self.root)
            self.assertIsNone(owner.poll(), "Liveness check must not terminate the owner")
            owner.communicate(timeout=10)
            self.assertFalse(_process_alive(owner.pid))
            self.assertTrue(recover(self.root)["recovered"])
        finally:
            if owner.poll() is None:
                owner.kill()
            owner.communicate()

    def test_a_clean_workspace_has_nothing_to_recover(self):
        self.assertEqual(recover(self.root), {"recovered": False})

    def test_an_invalid_identity_is_refused(self):
        for value in (0, -1, "1", None):
            with self.subTest(value=value), self.assertRaisesRegex(PipelineError, "Invalid publisher process"):
                _process_alive(cast("int", value))  # the point is the wrong type

    @unittest.skipUnless(sys.platform != "win32", "Windows uses the ctypes liveness probe instead of os.kill")
    def test_the_posix_probe_reads_permission_denied_as_alive(self):
        """A process we may not signal is not evidence of a dead publisher."""

        with patch("description_pipeline.build.publication.os.kill", side_effect=PermissionError("denied")):
            self.assertTrue(_process_alive(os.getpid()))
        with (
            patch("description_pipeline.build.publication.os.kill", side_effect=OSError("broken")),
            self.assertRaisesRegex(PipelineError, "Cannot inspect publisher process"),
        ):
            _process_alive(os.getpid())

    @unittest.skipUnless(sys.platform == "win32", "the ctypes probe only exists on Windows")
    def test_the_windows_probe_reads_each_kernel_answer_the_way_the_comment_says(self):
        """The mirror of the POSIX case: error 5 is not evidence of death, 87 is, and a signalled
        handle means the process has exited.  `os.kill` cannot be used as a probe on Windows."""

        kernel = MagicMock()
        kernel.OpenProcess.return_value = 0  # a failed OpenProcess is what the error codes describe
        with patch("ctypes.WinDLL", return_value=kernel), patch("ctypes.get_last_error", return_value=87):
            self.assertFalse(_process_alive(os.getpid()), "ERROR_INVALID_PARAMETER means the process is gone")
        with patch("ctypes.WinDLL", return_value=kernel), patch("ctypes.get_last_error", return_value=5):
            self.assertTrue(_process_alive(os.getpid()), "access denied is not evidence of a dead publisher")
        with (
            patch("ctypes.WinDLL", return_value=kernel),
            patch("ctypes.get_last_error", return_value=6),
            self.assertRaisesRegex(PipelineError, "Windows error 6"),
        ):
            _process_alive(os.getpid())
        kernel.OpenProcess.return_value = 1
        kernel.WaitForSingleObject.return_value = 0
        with patch("ctypes.WinDLL", return_value=kernel):
            self.assertFalse(_process_alive(os.getpid()), "WAIT_OBJECT_0 means the process has exited")
        kernel.WaitForSingleObject.return_value = 0x102
        with patch("ctypes.WinDLL", return_value=kernel):
            self.assertTrue(_process_alive(os.getpid()), "WAIT_TIMEOUT means it is still running")

    def test_a_staging_entry_that_is_not_there_is_refused(self):
        """The journal must never be written for a publication that cannot complete."""

        (self.staging / "urdf").unlink()
        with self.assertRaisesRegex(PipelineError, "Invalid publication entry"):
            publish(self.staging, self.root)
        self.assertFalse((self.root / JOURNAL).exists(), "no journal may be left behind")
        self.assertTrue(
            all((self.root / name).read_text(encoding="utf-8") == "old" for name in ENTRIES),
            "the current delivery stays as it was",
        )
        self.assertEqual(
            sorted(path.name for path in (self.root / "build").iterdir() if path.name != Path(JOURNAL).name),
            [],
            "the refused publish removes its own backup directory",
        )

    def test_recovery_replaces_a_directory_that_is_already_there(self):
        """The real delivery holds directories, so the restore path has to remove them first."""

        backup = self.root / "build/previous-dirs"
        for base, contents in ((backup, "older"), (self.root, "current")):
            for relative in ENTRIES:
                target = base / relative
                if target.is_file():
                    target.unlink()
                target.mkdir(parents=True, exist_ok=True)
                (target / "value.txt").write_text(contents, encoding="utf-8")
        write_json(
            self.root / JOURNAL,
            {"pid": os.getpid(), "backup": backup.name, "previous": list(ENTRIES)},
        )
        self.assertTrue(recover(self.root)["recovered"])
        for name in ENTRIES:
            self.assertEqual((self.root / name / "value.txt").read_text(encoding="utf-8"), "older")

    def test_recovery_removes_an_entry_the_crash_never_replaced(self):
        """An entry the journal does not list as replaced is something the interruption left behind."""

        backup = self.root / "build/previous-partial"
        backup.mkdir(parents=True)
        (backup / "model").write_text("older", encoding="utf-8")
        write_json(self.root / JOURNAL, {"pid": os.getpid(), "backup": backup.name, "previous": ["model"]})
        self.assertTrue(recover(self.root)["recovered"])
        self.assertEqual((self.root / "model").read_text(encoding="utf-8"), "older")
        self.assertFalse((self.root / "urdf").exists(), "an entry the crash never touched is removed")

    def test_a_second_publisher_is_refused_without_touching_the_delivery(self):
        """Two builders in one workspace: the second must refuse, not interleave file swaps.

        The journal is created exclusively, so whoever loses the race also has to leave the winner's
        journal and the current delivery exactly as they were, and clean up its own backup directory.
        """

        journal = self.root / JOURNAL
        journal.parent.mkdir(parents=True, exist_ok=True)
        backup = self.root / "build/previous-live"
        backup.mkdir(parents=True)
        write_json(journal, {"pid": os.getpid(), "backup": backup.name, "previous": []})
        before = {name: (self.root / name).read_text(encoding="utf-8") for name in ENTRIES}

        with self.assertRaisesRegex(PipelineError, "Publication in progress"):
            publish(self.staging, self.root)

        after = {name: (self.root / name).read_text(encoding="utf-8") for name in ENTRIES}
        self.assertEqual(before, after, "a refused publish must leave the current delivery alone")
        self.assertTrue(journal.exists(), "the other publisher's journal stays where it is")
        self.assertEqual(
            sorted(path.name for path in (self.root / "build").iterdir() if path.name != Path(JOURNAL).name),
            [backup.name],
            "the refused publish removes the backup directory it had already created",
        )

    def test_a_damaged_journal_is_a_diagnostic_instead_of_a_crash(self):
        """``recover`` is what an operator runs after an interrupted publish, so it must not crash."""

        journal = self.root / JOURNAL
        journal.parent.mkdir(parents=True, exist_ok=True)
        cases = {
            "list": "[]",
            "string": '"x"',
            "unparseable": "{",
            "empty object": "{}",
            "no process": '{"backup": "previous-x", "previous": []}',
            "bad process": '{"pid": "x", "backup": "previous-x", "previous": []}',
            "no backup": '{"pid": 1, "previous": []}',
            "escaping backup": '{"pid": 1, "backup": "../previous-x", "previous": []}',
            "no entries": '{"pid": 1, "backup": "previous-x"}',
            "entries not a list": '{"pid": 1, "backup": "previous-x", "previous": "model"}',
        }
        for label, text in cases.items():
            with self.subTest(case=label):
                journal.write_text(text, encoding="utf-8")
                with self.assertRaises(PipelineError) as raised:
                    recover(self.root)
                message = str(raised.exception)
                self.assertIn(JOURNAL, message)
                self.assertIn("description build", message)
                unchanged = all((self.root / name).read_text(encoding="utf-8") == "old" for name in ENTRIES)
                self.assertTrue(unchanged, "a refused recovery must not touch the published entries")

    def test_a_journal_whose_backup_is_gone_is_not_reported_as_recovered(self):
        (self.root / "build").mkdir(parents=True, exist_ok=True)
        write_json(
            self.root / JOURNAL,
            {"pid": os.getpid(), "backup": "previous-missing", "previous": list(ENTRIES)},
        )
        with self.assertRaises(PipelineError) as raised:
            recover(self.root)
        self.assertIn("previous-missing", str(raised.exception))
        self.assertTrue((self.root / JOURNAL).exists(), "a refused recovery keeps the journal")
        self.assertTrue(all((self.root / name).exists() for name in ENTRIES))
