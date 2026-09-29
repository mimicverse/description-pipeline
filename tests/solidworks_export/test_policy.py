"""Directory allowlist semantics (case/slash insensitive, no prefix escapes)."""

import unittest

from tools.solidworks_export.policy import path_is_allowed


class PathPolicyTests(unittest.TestCase):
    def test_empty_allowlist_allows_everything(self):
        self.assertTrue(path_is_allowed("C:\\anything\\x.SLDASM", []))
        self.assertTrue(path_is_allowed("C:\\anything\\x.SLDASM", []))

    def test_paths_inside_roots_are_allowed(self):
        roots = ["D:\\models", "D:\\export"]
        self.assertTrue(path_is_allowed("D:\\models\\box.SLDASM", roots))
        self.assertTrue(path_is_allowed("d:/EXPORT/job-1/robot.urdf", roots))
        self.assertTrue(path_is_allowed("D:\\export", roots))
        self.assertTrue(path_is_allowed("d:\\MODELS\\", roots))

    def test_paths_outside_roots_are_rejected(self):
        roots = ["D:\\models", "D:\\export"]
        self.assertFalse(path_is_allowed("C:\\other\\x.SLDASM", roots))
        self.assertFalse(path_is_allowed("D:\\export-other\\x", roots))
        self.assertFalse(path_is_allowed("", roots))

    def test_blank_roots_are_ignored(self):
        self.assertTrue(path_is_allowed("D:\\models\\x", ["", "  "]))


if __name__ == "__main__":
    unittest.main()
