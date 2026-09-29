"""The dependency audit has to be able to fail, and it has to read the locks as they are written.

The audit is what makes "no known advisories in the pinned sets" a repeatable claim instead of a
memory, and it runs against files other tools generate.  Everything except the network call is pinned
here: the parse, the request it builds and the answer it reads, with a control for each.
"""

import tempfile
import unittest
from pathlib import Path

from tools import audit_dependencies

ANSWER = {
    "results": [
        {"vulns": [{"id": "OSV-2026-1", "aliases": ["CVE-2026-0001"], "summary": "heap overflow in the reader"}]},
        {},
    ]
}


class LockParsingTests(unittest.TestCase):
    def lock(self, text: str) -> list[tuple[str, str]]:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "requirements.lock"
            path.write_text(text, encoding="utf-8")
            return audit_dependencies.parse_lock(path)

    def test_a_hash_locked_file_yields_canonical_pins(self):
        self.assertEqual(
            self.lock("# a comment\nPyYAML==6.0.3 --hash=sha256:abc\n\ntyping_extensions==4.16.0 --hash=sha256:def\n"),
            [("pyyaml", "6.0.3"), ("typing-extensions", "4.16.0")],
        )

    def test_an_unpinned_line_is_refused(self):
        for text in ("numpy>=2\n", "numpy\n", "-r other.lock\n"):
            with self.subTest(line=text.strip()), self.assertRaisesRegex(audit_dependencies.AuditError, "not a pin"):
                self.lock(text)

    def test_an_empty_file_is_refused(self):
        with self.assertRaisesRegex(audit_dependencies.AuditError, "pins nothing"):
            self.lock("# only a comment\n")

    def test_a_missing_file_is_refused(self):
        with self.assertRaisesRegex(audit_dependencies.AuditError, "No lock file"):
            audit_dependencies.parse_lock(Path("/nonexistent/requirements.lock"))


class AnswerTests(unittest.TestCase):
    def test_the_query_names_each_pin(self):
        self.assertEqual(
            audit_dependencies.queries([("pywin32", "311")]),
            [{"package": {"name": "pywin32", "ecosystem": "PyPI"}, "version": "311"}],
        )

    def test_an_advisory_is_reported_against_its_pin(self):
        pins = [("numpy", "2.5.3"), ("pyyaml", "6.0.3")]
        self.assertEqual(
            audit_dependencies.findings(pins, ANSWER),
            ["numpy==2.5.3: CVE-2026-0001 — heap overflow in the reader"],
        )
        self.assertEqual(audit_dependencies.findings(pins, {"results": [{}, {}]}), [])

    def test_an_advisory_without_a_cve_alias_still_names_something(self):
        answer = {"results": [{"vulns": [{"id": "GHSA-xxxx-yyyy-zzzz"}]}]}
        self.assertEqual(
            audit_dependencies.findings([("numpy", "2.5.3")], answer),
            ["numpy==2.5.3: GHSA-xxxx-yyyy-zzzz"],
        )

    def test_an_answer_that_does_not_match_the_queries_is_refused(self):
        with self.assertRaisesRegex(audit_dependencies.AuditError, "different number of results"):
            audit_dependencies.findings([("numpy", "2.5.3"), ("pyyaml", "6.0.3")], {"results": [{}]})


CHECKLIST = Path(__file__).resolve().parents[1] / "RELEASING.md"
AUDITED_LOCKS = (
    "requirements/linux-py312.lock",
    "requirements/win-py312-dev.lock",
    "src/description_pipeline/sources/solidworks/deploy/requirements/win-py312.lock",
)


def checklist_problems(text: str) -> list[str]:
    """The sets a release ships have to be audited by the checklist, by exactly this command."""

    problems = []
    if "tools/audit_dependencies.py" not in text:
        problems.append("RELEASING.md does not run the dependency audit")
    problems += [f"RELEASING.md does not audit {lock}" for lock in AUDITED_LOCKS if lock not in text]
    return problems


class ChecklistTests(unittest.TestCase):
    def test_every_shipped_lock_is_audited_by_the_checklist(self):
        self.assertEqual(checklist_problems(CHECKLIST.read_text(encoding="utf-8")), [])

    def test_a_checklist_that_drops_the_audit_is_reported(self):
        """The control: both the command and the sets it has to cover."""

        text = CHECKLIST.read_text(encoding="utf-8")
        trimmed = text.replace("tools/audit_dependencies.py", "pip-audit").replace(
            AUDITED_LOCKS[1], "requirements/other.lock"
        )
        self.assertEqual(
            checklist_problems(trimmed),
            ["RELEASING.md does not run the dependency audit", f"RELEASING.md does not audit {AUDITED_LOCKS[1]}"],
        )


if __name__ == "__main__":
    unittest.main()
