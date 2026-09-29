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
    normalise_dependency_entries,
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

    def test_alternating_name_path_pairs_keep_only_the_full_path(self) -> None:
        # GetDependencies2(Searchflag=True) returns [display name, full path, ...]
        raw = [
            "000_truncated_display_name",
            win("C:", "cad", "000_femur.SLDPRT"),
            "001_another_display_name",
            win("C:", "cad", "001_tibia.SLDPRT"),
        ]
        entries = normalise_dependency_entries(raw, ASSEMBLY)
        self.assertEqual(
            entries,
            [
                win("C:", "cad", "000_femur.SLDPRT"),
                win("C:", "cad", "001_tibia.SLDPRT"),
            ],
        )

    def test_single_valued_arrays_are_resolved_against_the_document(self) -> None:
        self.assertEqual(
            normalise_dependency_entries(["base.SLDPRT"], ASSEMBLY),
            [win("C:", "Users", "Mi", "swbridge", "validation", "microban", "cad", "base.SLDPRT")],
        )

    def test_missing_document_directory_leaves_the_name_alone(self) -> None:
        self.assertEqual(resolve_dependency_path("bare.SLDPRT", ""), "bare.SLDPRT")


if __name__ == "__main__":
    unittest.main()


class NativeReferenceCopyTests(unittest.TestCase):
    def test_nested_graph_is_copied_before_rewriting_without_touching_sources(self):
        import tempfile
        from pathlib import Path
        from types import SimpleNamespace
        from unittest.mock import Mock
        from description_pipeline.sources.solidworks.native import SolidWorksBackend

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            paths = [root / "original" / name for name in ["robot.SLDASM", "module/sub.SLDASM", "parts/arm.SLDPRT"]]
            for path in paths:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(str(path))
            graph = {str(paths[0]): ["sub", str(paths[1])], str(paths[1]): ["arm", str(paths[2])], str(paths[2]): []}
            before = {str(path): path.read_bytes() for path in paths}
            edges: list[tuple[str, str, str]] = []

            def replace(document, referenced, new):
                self.assertEqual(Path(document).read_bytes(), before[str(paths[len(edges)])])
                self.assertEqual(Path(new).read_bytes(), before[referenced])
                self.assertTrue(Path(document).is_relative_to(root / "copy"))
                edges.append((document, referenced, new))
                Path(document).write_text("rewritten")
                return True

            source = SimpleNamespace(GetDocumentDependencies2=lambda path, *args: graph[path])
            copied = SimpleNamespace(ReplaceReferencedDocument=replace)
            backend = SolidWorksBackend()
            backend._app_for_path = Mock(side_effect=[source, copied])  # type: ignore[method-assign]
            result = backend.collect_dependencies(str(paths[0]), str(root / "copy"))
            self.assertEqual(result["method"], "native_reference_copy")
            self.assertEqual(result["reference_edges"], 2)
            self.assertEqual(len(result["files"]), 3)
            self.assertEqual(result["top_level"], str(root / "copy/robot.SLDASM"))
            self.assertEqual({str(path): path.read_bytes() for path in paths}, before)

    def test_rejected_reference_rewrite_never_reports_success(self):
        import tempfile
        from pathlib import Path
        from types import SimpleNamespace
        from unittest.mock import Mock
        from description_pipeline.sources.solidworks.native import SolidWorksBackend
        from description_pipeline.sources.solidworks.errors import BridgeError

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            assembly, part = root / "robot.SLDASM", root / "part.SLDPRT"
            assembly.write_bytes(b"assembly")
            part.write_bytes(b"part")
            source = SimpleNamespace(
                GetDocumentDependencies2=lambda path, *args: ["part", str(part)] if path == str(assembly) else []
            )
            copied = SimpleNamespace(ReplaceReferencedDocument=lambda *args: False)
            backend = SolidWorksBackend()
            backend._app_for_path = Mock(side_effect=[source, copied])  # type: ignore[method-assign]
            with self.assertRaises(BridgeError) as caught:
                backend.collect_dependencies(str(assembly), str(root / "copy"))
            self.assertEqual(caught.exception.code, "dependency_rewrite_failed")
