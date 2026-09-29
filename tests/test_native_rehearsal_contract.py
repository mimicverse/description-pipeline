"""The native rehearsal and the release checklist have to stay in step.

The Windows step is the only one of the release checklist that cannot run on the build host, so it is
the step done by hand, from memory, and the one whose evidence goes missing. `tools/native/win-rehearsal.ps1`
turns it into one command with a machine-readable summary. These checks keep that summary's fields and
the documented command from drifting apart, and they keep the two guards that protect the production
worker in the script.
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "native" / "win-rehearsal.ps1"
CHECKLIST = ROOT / "RELEASING.md"
SUMMARY = "native-rehearsal.json"

#: Fields the rehearsal summary carries; the checklist may quote them, and a release record reads them.
SUMMARY_FIELDS = (
    "schema_version",
    "bundle",
    "bundle_sha256",
    "update_from",
    "update",
    "security",
    "version",
    "versions_installed",
    "install_root",
    "port",
    "task_name",
    "assembly",
    "configuration",
    "setup",
    "start",
    "status",
    "doctor",
    "local_pipeline",
    "mujoco",
    "worker_version",
    "solidworks_revision",
    "solidworks_license",
    "components",
    "mass_properties",
    "collection",
    "quickstart",
    "started_at",
    "finished_at",
    "passed",
    "cleaned_up",
)

#: Keys the summary's ``update`` object carries after a rehearsal that used ``-UpdateFrom``.  The
#: release record quotes the words the updater printed, so they have to be in the summary: a child
#: process's console output never reaches the rehearsal's own transcript.
UPDATE_FIELDS = (
    "exit",
    "from",
    "to",
    "log",
    "rollback_exit",
    "rolled_back_to",
    "rollback_log",
)


def missing_fields(text: str) -> list[str]:
    """Summary fields the script no longer writes."""

    return [field for field in SUMMARY_FIELDS if not re.search(rf"^\s*{field}\s*=", text, re.M)]


def missing_update_fields(text: str) -> list[str]:
    """Keys the summary's ``update`` object no longer records."""

    return [field for field in UPDATE_FIELDS if not re.search(rf"\$update\.{field}\s*=", text)]


def checklist_problems(text: str) -> list[str]:
    """What the release checklist has to say about the native step."""

    problems = []
    if "tools\\native\\win-rehearsal.ps1" not in text:
        problems.append("RELEASING.md does not run the native rehearsal script")
    if SUMMARY not in text:
        problems.append(f"RELEASING.md does not name the {SUMMARY} the script writes")
    return problems


def guard_problems(text: str) -> list[str]:
    """The script's four safety properties, as text anyone can still read in a diff."""

    problems = []
    if "'description-pipeline-worker'" not in text:
        problems.append("the script no longer names the production task name it refuses")
    if "Remove-Item -LiteralPath $InstallRoot" not in text:
        problems.append("the script no longer removes exactly its own install root")
    if "SHA256SUMS" not in text:
        problems.append("the script no longer proves the bundle against the SHA256SUMS beside it")
    if "worker', 'doctor', '--target'" not in text:
        problems.append("the script no longer asks the worker for the machine-readable report")
    if "'-Action', 'Update'" not in text or "'-Action', 'Rollback'" not in text:
        problems.append("the script no longer rehearses the documented upgrade and rollback")
    if "bundle-update\\worker.ps1" not in text:
        problems.append("the upgrade is no longer run with the new archive's own launcher")
    for captured in ("$update.log", "$update.rollback_log"):
        if captured not in text:
            problems.append(f"the rehearsal no longer keeps what the updater said ({captured})")
    if "(Invoke-CapturedProcess $description $arguments).log" not in text:
        problems.append("the doctor's report is captured without the helper, so its advisory can end the run")
    for probe in ("foreign_host", "foreign_origin", "unknown_route"):
        if probe not in text:
            problems.append(f"the rehearsal no longer probes the worker's binding ({probe})")
    return problems


class NativeRehearsalContractTests(unittest.TestCase):
    def test_the_script_writes_every_promised_field(self):
        self.assertEqual(missing_fields(SCRIPT.read_text(encoding="utf-8")), [])

    def test_a_dropped_field_is_reported(self):
        """The control: a summary the record reads is a field away from being unreadable."""

        text = SCRIPT.read_text(encoding="utf-8").replace("        passed              = ", "        outcome = ")
        self.assertIn("passed", missing_fields(text))

    def test_the_update_object_records_what_the_updater_said(self):
        self.assertEqual(missing_update_fields(SCRIPT.read_text(encoding="utf-8")), [])

    def test_a_dropped_update_field_is_reported(self):
        """The control: the quoted upgrade words are one assignment away from going missing."""

        text = SCRIPT.read_text(encoding="utf-8").replace("$update.rollback_log = ", "outcome = ")
        self.assertIn("rollback_log", missing_update_fields(text))

    def test_a_rehearsal_that_stops_keeping_the_updater_output_is_reported(self):
        text = SCRIPT.read_text(encoding="utf-8").replace("$update.log", "$update.words")
        self.assertEqual(
            guard_problems(text),
            ["the rehearsal no longer keeps what the updater said ($update.log)"],
        )

    def test_a_doctor_capture_that_bypasses_the_helper_is_reported(self):
        """The control: the 0.3.20 rehearsal died here, before it could write a summary."""

        text = SCRIPT.read_text(encoding="utf-8").replace(
            "(Invoke-CapturedProcess $description $arguments).log",
            '(& $description @arguments 2>&1) -join "`n"',
        )
        self.assertEqual(
            guard_problems(text),
            ["the doctor's report is captured without the helper, so its advisory can end the run"],
        )

    def test_the_checklist_runs_the_script_and_names_its_summary(self):
        self.assertEqual(checklist_problems(CHECKLIST.read_text(encoding="utf-8")), [])

    def test_a_checklist_that_stops_naming_the_script_is_reported(self):
        text = CHECKLIST.read_text(encoding="utf-8").replace("tools\\native\\win-rehearsal.ps1", "worker.ps1")
        self.assertEqual(
            checklist_problems(text),
            ["RELEASING.md does not run the native rehearsal script"],
        )

    def test_the_guards_that_protect_the_production_worker_are_in_the_script(self):
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertEqual(guard_problems(text), [])
        self.assertEqual(
            guard_problems(text.replace("'description-pipeline-worker'", "'some-other-task'")),
            ["the script no longer names the production task name it refuses"],
        )


if __name__ == "__main__":
    unittest.main()
