"""Dependency listing must return usable absolute paths on Windows.

``GetDependencies2`` hands back bare file names for references SolidWorks
resolved relative to the assembly; the adapter has to join them against the
document directory, or every legitimate part looks like an escaped dependency.
Windows separators are used explicitly so the same test runs on the build host.
"""

from __future__ import annotations

import unittest

from . import _paths  # noqa: F401  (import side effect: sys.path)

from description_pipeline.sources.solidworks.native import (  # noqa: E402
    resolve_dependency_path,
)

BS = chr(92)


def win(*parts: str) -> str:
    return BS.join(parts)


ASSEMBLY = win("C:", "Users", "Mi", "swbridge", "validation", "microban", "cad", "robot.SLDASM")


class DependencyPathTests(unittest.TestCase):
    def test_bare_names_are_resolved_against_the_assembly_directory(self) -> None:
        self.assertEqual(
            resolve_dependency_path("000_femur.SLDPRT", ASSEMBLY),
            win("C:", "Users", "Mi", "swbridge", "validation", "microban", "cad", "000_femur.SLDPRT"),
        )

    def test_relative_subpaths_are_joined(self) -> None:
        self.assertEqual(
            resolve_dependency_path(win("parts", "tibia.SLDPRT"), ASSEMBLY),
            win("C:", "Users", "Mi", "swbridge", "validation", "microban", "cad", "parts", "tibia.SLDPRT"),
        )

    def test_drive_qualified_paths_pass_through(self) -> None:
        value = win("D:", "models", "base.SLDPRT")
        self.assertEqual(resolve_dependency_path(value, ASSEMBLY), value)

    def test_unc_paths_pass_through(self) -> None:
        value = BS * 2 + win("server", "share", "x.SLDPRT")
        self.assertEqual(resolve_dependency_path(value, ASSEMBLY), value)



    def test_missing_document_directory_leaves_the_name_alone(self) -> None:
        self.assertEqual(resolve_dependency_path("bare.SLDPRT", ""), "bare.SLDPRT")


if __name__ == "__main__":
    unittest.main()
