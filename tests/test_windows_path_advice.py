"""A Windows MAX_PATH failure has to name the fix, not the staging path it tripped over.

Long paths are off by default on Windows, so this is a first-run experience rather than an edge
case: the OS reports it as a bare ``WinError 3`` (or 206) that names an internal directory and says
nothing about the 260-character limit the operator has to change.  The advice is attached to the
same envelope every command already prints, and it is exercised here with the codes Windows uses.
"""

import io
import json
import unittest
from contextlib import redirect_stderr
from unittest import mock

from description_pipeline import cli, doctor


class _WindowsPathError(OSError):
    """``OSError`` carrying the ``winerror`` slot Windows fills in (POSIX typeshed has no such slot)."""

    winerror: int = 0


def windows_error(code: int, message: str = "The system cannot find the path specified") -> OSError:
    error = _WindowsPathError(2, message)
    error.winerror = code
    return error


class PathAdviceTests(unittest.TestCase):
    def test_the_two_windows_path_codes_get_the_fix(self):
        for code in sorted(cli.WINDOWS_PATH_ERRORS):
            with self.subTest(code=code):
                advice = cli._path_advice(windows_error(code))
                self.assertIn("MAX_PATH", advice)
                self.assertIn("LongPathsEnabled", advice)
                self.assertIn("not modified", advice)
                # `description doctor --root .` warns before the failure: same text, one source.
                self.assertIn("MAX_PATH", doctor.WINDOWS_LONG_PATH_ADVICE)

    def test_other_failures_are_left_alone(self):
        for error in (windows_error(5), OSError(2, "No such file or directory"), ValueError("x")):
            with self.subTest(error=type(error).__name__):
                self.assertEqual(cli._path_advice(error), "")

    def test_the_envelope_carries_the_advice(self):
        """The CLI reports the same JSON shape for this failure as for any other."""

        with mock.patch.object(cli, "recover", side_effect=windows_error(206)):
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                code = cli.main(["recover", "--root", "."])
        self.assertEqual(code, 2)
        payload = json.loads(stderr.getvalue().strip().splitlines()[-1])
        self.assertFalse(payload["passed"])
        self.assertTrue(payload["error"].endswith("Error"), payload["error"])
        self.assertIn("MAX_PATH", payload["message"])


if __name__ == "__main__":
    unittest.main()
