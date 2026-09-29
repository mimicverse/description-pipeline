"""A hostile bundle has to be refused before anything is extracted.

``tools/verify_distribution.py`` is what turns a built archive into "verified".  It checks member
names and duplicate entries, and those checks were never exercised: a zip-slip member or a name that
is absolute only on Windows ("C:/x") would have been extracted into the reviewer's temporary
directory, or worse, unpacked by hand on another machine.
"""

import tempfile
import unittest
import warnings
import zipfile
from pathlib import Path

from tools import verify_distribution

HOSTILE = {
    "parent traversal": "../escape.txt",
    "absolute POSIX path": "/tmp/escape.txt",
    "Windows separator": "..\\escape.txt",
    "drive letter": "C:/escape.txt",
}


class IdentityMessageTests(unittest.TestCase):
    """A refusal has to name the field that differs, and the interpreter when that is the reason."""

    def test_a_wrong_interpreter_names_the_pinned_version(self):
        expected = {"version": "0.3.21", "python": "3.12.10", "platform": {"system": "Linux", "machine": "x86_64"}}
        installed = {"version": "0.3.21", "python": "3.12.14", "platform": {"system": "Linux", "machine": "x86_64"}}
        message = verify_distribution.identity_mismatch(expected, installed)
        self.assertIn("python '3.12.10' packaged vs '3.12.14' installed", message)
        self.assertIn("pins CPython 3.12.10 on Linux", message)
        self.assertIn("verify it with that exact interpreter", message)
        self.assertNotIn("version", message)
        self.assertNotIn("platform ", message)

    def test_dependency_drift_is_summarised_not_dumped(self):
        expected = {"dependencies": {"numpy": "2.5.3", "pyyaml": "6.0.3"}}
        installed = {"dependencies": {"numpy": "2.5.4", "jsonschema": "4.26.0"}}
        message = verify_distribution.identity_mismatch(expected, installed)
        self.assertIn("missing ['pyyaml']", message)
        self.assertIn("extra ['jsonschema']", message)
        self.assertIn("changed ['numpy']", message)
        self.assertNotIn("interpreter", message)


class AuthorFlowTests(unittest.TestCase):
    """The smoke builds a workspace the way the first-use guide tells an author to build one."""

    def test_the_author_flow_registers_every_movable_joint(self):
        from description_pipeline.verification.urdf_quality import ledger as joint_ledger

        scene = verify_distribution.fixture()
        expected = [item["name"] for item in scene["joints"] if item["type"] != "fixed"]
        self.assertTrue(expected, "the fixture has to carry a movable joint for this test to mean anything")
        with tempfile.TemporaryDirectory() as temporary:
            model = Path(temporary) / "model"
            registered = verify_distribution.write_joint_ledger(model, scene)
            path = model / "config" / "joint_names.yaml"
            self.assertEqual(registered, expected)
            # The pipeline's own reader has to accept the file, or the smoke would still fail URDF208.
            self.assertEqual(joint_ledger.load(path), expected)


def archive_of(root: Path, members: list[tuple[str, str]]) -> Path:
    path = root / "bundle.zip"
    with warnings.catch_warnings():  # a deliberately duplicated member warns while it is written
        warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(path, "w") as bundle:
            for name, text in members:
                bundle.writestr(name, text)
    return path


class HostileArchiveTests(unittest.TestCase):
    def test_linux_verifier_refuses_invalid_members(self):
        for label, member in HOSTILE.items():
            with self.subTest(case=label), tempfile.TemporaryDirectory() as temporary:
                bundle = archive_of(Path(temporary), [(member, "x")])
                with self.assertRaisesRegex(RuntimeError, "Invalid archive member"):
                    verify_distribution.verify(bundle)
                self.assertFalse((Path(temporary) / "escape.txt").exists())

    def test_windows_verifier_refuses_invalid_members(self):
        for label, member in HOSTILE.items():
            with self.subTest(case=label), tempfile.TemporaryDirectory() as temporary:
                bundle = archive_of(Path(temporary), [(member, "x")])
                with self.assertRaisesRegex(RuntimeError, "Invalid archive member"):
                    verify_distribution.verify_windows(bundle)
                self.assertFalse((Path(temporary) / "escape.txt").exists())

    def test_duplicate_members_are_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            bundle = archive_of(Path(temporary), [("files.json", "{}"), ("files.json", "{}")])
            for verifier in (verify_distribution.verify, verify_distribution.verify_windows):
                with (
                    self.subTest(verifier=verifier.__name__),
                    self.assertRaisesRegex(RuntimeError, r"Duplicate( Windows)? archive entries"),
                ):
                    verifier(bundle)

    def test_a_valid_archive_is_not_refused_by_the_name_check(self):
        """The negative controls must not turn into a check that rejects everything."""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bundle = archive_of(root, [("wheels/mimicverse_description-0.3.14-py3-none-any.whl", "x")])
            with self.assertRaisesRegex(RuntimeError, "files.json") as raised:
                verify_distribution.verify(bundle)
            self.assertNotIn("Invalid archive member", str(raised.exception))
            self.assertNotIn("Duplicate", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
