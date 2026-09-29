"""`check` is a verifier: it must not touch the workspace, and a read-only delivery must still pass.

Operators run `check` on a delivered model — often a read-only checkout of a release branch — and the
documentation promises it re-derives and verifies rather than builds.  Both halves of that promise are
pinned here: the tree is byte-identical afterwards, and the same call succeeds when nothing is
writable.
"""

import hashlib
import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path

from description_pipeline.build import assess, build, freeze, lock_toolchain

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "examples" / "demo-arm"
IGNORED = shutil.ignore_patterns("build", ".venv", "__pycache__")


def digest_tree(root: Path) -> str:
    """A digest over every file path and its bytes, in path order."""

    running = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root).as_posix()
        running.update(relative.encode("utf-8"))
        running.update(b"\0")
        running.update(hashlib.sha256(path.read_bytes()).digest())
    return running.hexdigest()


def set_writable(root: Path, *, writable: bool) -> None:
    for path in [root, *sorted(root.rglob("*"))]:
        mode = path.stat().st_mode
        path.chmod(mode | stat.S_IWUSR if writable else mode & ~stat.S_IWUSR)


def is_root() -> bool:
    """Whether this process ignores file modes because it is root.

    Windows has no ``os.geteuid`` and no such exemption: ``chmod`` there sets the read-only attribute,
    which a non-elevated process cannot write through, so the check below means the same thing.  The
    call is indirect because ``os.geteuid`` does not exist in the Windows typeshed either — that
    attribute error is what took the native Windows gate down.
    """

    geteuid = getattr(os, "geteuid", None)
    return bool(callable(geteuid) and geteuid() == 0)


class ReadOnlyCheckTests(unittest.TestCase):
    def prepare(self, temporary: str) -> Path:
        workspace = Path(temporary) / "demo-arm"
        shutil.copytree(EXAMPLE, workspace, ignore=IGNORED)
        lock_toolchain(workspace)
        freeze(workspace)
        report = build(workspace, "kinematics")
        self.assertTrue(report["passed"], report["blockers"])
        return workspace

    def test_check_leaves_the_workspace_byte_identical(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = self.prepare(temporary)
            before = digest_tree(workspace)
            report = assess(workspace, "kinematics")
            self.assertTrue(report["passed"], report["blockers"])
            self.assertEqual(digest_tree(workspace), before, "check must not rewrite the delivery")

    @unittest.skipIf(is_root(), "root writes wherever it likes, so read-only proves nothing")
    def test_check_passes_on_a_read_only_delivery(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = self.prepare(temporary)
            set_writable(workspace, writable=False)
            try:
                report = assess(workspace, "kinematics")
            finally:
                set_writable(workspace, writable=True)
            self.assertTrue(report["passed"], report["blockers"])
            self.assertEqual(report["qualified_for"], ["kinematics"])

    @unittest.skipIf(is_root(), "root writes wherever it likes, so read-only proves nothing")
    def test_a_read_only_failure_still_reports_its_blockers(self):
        """The diagnostic path must not need a writable workspace either."""

        with tempfile.TemporaryDirectory() as temporary:
            workspace = self.prepare(temporary)
            urdf = workspace / "urdf/robot.urdf"
            urdf.write_text(urdf.read_text(encoding="utf-8").replace('"2.2000000000000002"', '"2.4"'), encoding="utf-8")
            set_writable(workspace, writable=False)
            try:
                report = assess(workspace, "kinematics")
            finally:
                set_writable(workspace, writable=True)
            self.assertFalse(report["passed"])
            self.assertIn("urdf.joints", report["blockers"])


if __name__ == "__main__":
    unittest.main()
