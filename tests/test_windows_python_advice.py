"""A first Windows run without CPython 3.12 has to name the fix, not the flag.

The worker bundle builds its virtual environment from an interpreter the machine already has, so "no
3.12 here" is the normal first-run case — 3.11, a 3.13 already on PATH, or nothing at all.  The
launcher used to answer `python not found: C:\\…` or "install it", which leaves the operator to
search for how; the Linux installer names the command, and the two first runs should read alike.
"""

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "src" / "description_pipeline" / "sources" / "solidworks" / "deploy" / "worker.ps1"


def advice_problems(text: str) -> list[str]:
    """The first-run refusals have to name how to get an interpreter, and the exact re-run."""

    problems = []
    if "winget install Python.Python.3.12" not in text:
        problems.append("worker.ps1 no longer names how to obtain CPython 3.12")
    # One definition and three refusals: no candidate, a candidate that is not there, a wrong version.
    if text.count("$PythonAdvice") < 4:
        problems.append("a CPython 3.12 refusal no longer carries the advice")
    for message in ("CPython 3.12 was not found", "no interpreter at", "the worker needs CPython 3.12"):
        if message not in text:
            problems.append(f"the refusal {message!r} is gone")
    return problems


class PythonAdviceTests(unittest.TestCase):
    def test_every_first_run_refusal_names_the_fix(self):
        self.assertEqual(advice_problems(LAUNCHER.read_text(encoding="utf-8")), [])

    def test_the_check_reports_a_refusal_that_lost_its_advice(self):
        """The control: the wrong-version message is the one a 3.11 machine hits."""

        text = LAUNCHER.read_text(encoding="utf-8")
        stripped = text.replace(
            'Stop-WithError "the worker needs CPython 3.12; $Python is $found. $PythonAdvice"',
            'Stop-WithError "the worker needs CPython 3.12"',
        )
        self.assertEqual(advice_problems(stripped), ["a CPython 3.12 refusal no longer carries the advice"])
        self.assertEqual(
            advice_problems(text.replace("winget install Python.Python.3.12", "winget install Python")),
            ["worker.ps1 no longer names how to obtain CPython 3.12"],
        )


if __name__ == "__main__":
    unittest.main()
