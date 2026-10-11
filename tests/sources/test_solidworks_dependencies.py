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

    def test_session_materialization_references_are_skipped_and_recorded(self):
        import tempfile
        from pathlib import Path
        from types import SimpleNamespace
        from unittest.mock import Mock
        from description_pipeline.sources.solidworks.native import SolidWorksBackend

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            assembly, part = root / "robot.SLDASM", root / "part.SLDPRT"
            assembly.write_bytes(b"assembly")
            part.write_bytes(b"part")
            materialization = root / "swx00000" / "IC~~" / "X.step.SLDASM"
            materialization.parent.mkdir(parents=True)
            materialization.write_bytes(b"temp")
            author = root / "author" / "X.stp"
            author.parent.mkdir(parents=True)
            author.write_bytes(b"author")
            graph = {
                str(assembly): ["X.step", str(materialization), "author", str(author), "part", str(part)],
                str(part): [],
            }
            rewritten: list = []
            copied = SimpleNamespace(ReplaceReferencedDocument=lambda *args: rewritten.append(args) or True)
            backend = SolidWorksBackend()
            backend._app_for_path = Mock(  # type: ignore[method-assign]
                side_effect=[SimpleNamespace(GetDocumentDependencies2=lambda path, *args: graph[path]), copied]
            )
            result = backend.collect_dependencies(
                str(assembly), str(root / "copy"), skip_session_temp=True, excluded_paths=[str(author)]
            )
            self.assertEqual(result["skipped_session_temp"], [str(materialization)])
            self.assertEqual(result["skipped_excluded"], [str(author)])
            self.assertEqual(len(result["files"]), 2)  # assembly + part only
            self.assertEqual(len(rewritten), 1)  # only the real reference is rewritten

    def test_required_leaf_missing_from_the_saved_walk_is_seeded_not_rewritten(self):
        import tempfile
        from pathlib import Path
        from types import SimpleNamespace
        from unittest.mock import Mock
        from description_pipeline.sources.solidworks.native import SolidWorksBackend

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            assembly, sub = root / "robot.SLDASM", root / "sub.SLDASM"
            assembly.write_bytes(b"assembly")
            sub.write_bytes(b"sub")
            leaf = root / "NP-F550.step"
            leaf.write_bytes(b"np-bytes")
            graph = {str(assembly): ["sub", str(sub)], str(sub): []}
            rewrites: list = []
            copied = SimpleNamespace(ReplaceReferencedDocument=lambda *args: rewrites.append(args) or True)
            backend = SolidWorksBackend()
            backend._app_for_path = Mock(  # type: ignore[method-assign]
                side_effect=[SimpleNamespace(GetDocumentDependencies2=lambda path, *args: graph.get(path, [])), copied]
            )
            result = backend.collect_dependencies(
                str(assembly),
                str(root / "copy"),
                required_names=["robot.SLDASM", "sub.SLDASM", "NP-F550.step"],
                admitted_roots=[str(root)],
                closure_documents={
                    "robot.SLDASM": {
                        "stored_locations": [],
                        "resolved_targets": ["sub.SLDASM"],
                        "resolved_paths": [str(sub)],
                    }
                },
                required_paths=[str(leaf)],
            )
            self.assertEqual(result["seeded_required_leaves"], [str(leaf)])
            self.assertIn(str(leaf), result["mapping"])
            mirrored = Path(result["mapping"][str(leaf)])
            self.assertTrue(mirrored.is_file())
            self.assertEqual(mirrored.read_bytes(), b"np-bytes")
            # The seeded leaf never becomes a synthetic native edge: the only
            # rewrite is the walked assembly -> sub edge.
            self.assertEqual(len(rewrites), 1)
            self.assertNotIn(str(leaf), [str(item) for args in rewrites for item in args])

    def test_required_leaf_missing_or_outside_roots_refuses(self):
        import tempfile
        from pathlib import Path
        from types import SimpleNamespace
        from unittest.mock import Mock
        from description_pipeline.sources.solidworks.native import SolidWorksBackend
        from description_pipeline.sources.solidworks.errors import CadError

        with tempfile.TemporaryDirectory() as folder, tempfile.TemporaryDirectory() as outside:
            root, out = Path(folder), Path(outside)
            assembly = root / "robot.SLDASM"
            assembly.write_bytes(b"assembly")
            graph = {str(assembly): []}
            copied = SimpleNamespace(ReplaceReferencedDocument=lambda *args: True)

            def run(required, label):
                backend = SolidWorksBackend()
                backend._app_for_path = Mock(  # type: ignore[method-assign]
                    side_effect=[
                        SimpleNamespace(GetDocumentDependencies2=lambda path, *args: graph.get(path, [])),
                        copied,
                    ]
                )
                return backend.collect_dependencies(
                    str(assembly),
                    str(root / f"copy-{label}"),
                    required_names=["robot.SLDASM"],
                    admitted_roots=[str(root)],
                    closure_documents={
                        "robot.SLDASM": {"stored_locations": [], "resolved_targets": [], "resolved_paths": []}
                    },
                    required_paths=required,
                )

            with self.assertRaises(CadError) as caught:
                run([str(root / "gone.step")], "missing")
            self.assertEqual(caught.exception.code, "dependency_missing")
            external = out / "NP-F550.step"
            external.write_bytes(b"outside")
            with self.assertRaises(CadError) as caught:
                run([str(external)], "external")
            self.assertEqual(caught.exception.code, "dependency_missing")

    def test_native5_shape_external_np_reference_is_skipped_and_seeded_copy_matches(self):
        import tempfile
        from pathlib import Path
        from types import SimpleNamespace
        from unittest.mock import Mock
        from description_pipeline.sources.solidworks.native import SolidWorksBackend

        with tempfile.TemporaryDirectory() as folder, tempfile.TemporaryDirectory() as outside:
            root, out = Path(folder), Path(outside)
            assembly = root / "robot.SLDASM"
            assembly.write_bytes(b"assembly")
            prepared_np = root / "NP-F550.step"
            prepared_np.write_bytes(b"sealed np bytes")
            desktop = out / "NP-F550.step"
            desktop.write_bytes(b"author desktop np bytes")
            graph = {str(assembly): ["NP-F550.step", str(desktop)]}
            copied = SimpleNamespace(ReplaceReferencedDocument=lambda *args: True)
            backend = SolidWorksBackend()
            backend._app_for_path = Mock(  # type: ignore[method-assign]
                side_effect=[SimpleNamespace(GetDocumentDependencies2=lambda path, *args: graph.get(path, [])), copied]
            )
            result = backend.collect_dependencies(
                str(assembly),
                str(root / "copy"),
                required_names=["robot.SLDASM", "NP-F550.step"],
                admitted_roots=[str(root)],
                closure_documents={
                    "robot.SLDASM": {
                        "stored_locations": [str(desktop)],
                        "resolved_targets": ["NP-F550.step"],
                        "resolved_paths": [str(prepared_np)],
                    }
                },
                required_paths=[str(assembly), str(prepared_np)],
            )
            # The desktop reference was evidence-covered and skipped; the sealed
            # NP was seeded, so the mapping equals the required set exactly.
            self.assertEqual(len(result["skipped_stale"]), 1)
            self.assertEqual(result["skipped_stale"][0]["resolved_path"], str(desktop))
            self.assertEqual(result["seeded_required_leaves"], [str(prepared_np)])
            self.assertEqual(set(result["mapping"]), {str(assembly), str(prepared_np)})
            mirrored = Path(result["mapping"][str(prepared_np)])
            self.assertEqual(mirrored.read_bytes(), b"sealed np bytes")

    def _stale_collect(self, root, outside, *, write_outside, closure_documents, required_names=None, location=None):
        from pathlib import Path
        from types import SimpleNamespace
        from unittest.mock import Mock
        from description_pipeline.sources.solidworks.native import SolidWorksBackend

        assembly, sub = root / "robot.SLDASM", root / "sub.SLDASM"
        assembly.write_bytes(b"assembly")
        sub.write_bytes(b"sub")
        location = location if location is not None else str(outside / "CopyDesignTemp" / "lowerbody.SLDASM")
        if write_outside:
            target = Path(location)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"stale external artifact")
        graph = {str(assembly): ["sub", str(sub)], str(sub): ["lowerbody", str(location)]}
        copied = SimpleNamespace(ReplaceReferencedDocument=lambda *args: True)
        backend = SolidWorksBackend()
        backend._app_for_path = Mock(  # type: ignore[method-assign]
            side_effect=[SimpleNamespace(GetDocumentDependencies2=lambda path, *args: graph.get(path, [])), copied]
        )
        kwargs = {
            "admitted_roots": [str(root)],
            "closure_documents": closure_documents,
        }
        if required_names is not None:
            kwargs["required_names"] = required_names
        return backend.collect_dependencies(str(assembly), str(root / "copy"), **kwargs)

    def test_evidence_covered_unused_missing_reference_is_skipped_with_record(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as folder, tempfile.TemporaryDirectory() as outside:
            root, out = Path(folder), Path(outside)
            location = str(out / "CopyDesignTemp" / "lowerbody.SLDASM")
            result = self._stale_collect(
                root,
                out,
                write_outside=False,
                required_names=["sub.SLDASM"],
                closure_documents={
                    "sub.SLDASM": {"stored_locations": [location], "resolved_targets": [], "resolved_paths": []}
                },
            )
            self.assertEqual(len(result["skipped_stale"]), 1)
            entry = result["skipped_stale"][0]
            self.assertEqual(entry["document"], "sub.SLDASM")
            self.assertEqual(entry["location"], location)
            self.assertFalse(entry["exists"])
            self.assertEqual(len(result["files"]), 2)  # assembly + sub; stale never copied

    def test_evidence_covered_existing_stale_reference_is_never_ingested(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as folder, tempfile.TemporaryDirectory() as outside:
            root, out = Path(folder), Path(outside)
            location = str(out / "CopyDesignTemp" / "lowerbody.SLDASM")
            result = self._stale_collect(
                root,
                out,
                write_outside=True,
                required_names=["sub.SLDASM"],
                closure_documents={
                    "sub.SLDASM": {"stored_locations": [location], "resolved_targets": [], "resolved_paths": []}
                },
            )
            entry = result["skipped_stale"][0]
            self.assertTrue(entry["exists"])
            self.assertEqual(len(result["files"]), 2)  # the present external file is not ingested
            self.assertNotIn(str(Path(location).resolve()), result["files"])

    def test_missing_selected_or_unrecorded_or_active_reference_stays_hard(self):
        import tempfile
        from pathlib import Path

        from description_pipeline.sources.solidworks.errors import CadError

        with tempfile.TemporaryDirectory() as folder, tempfile.TemporaryDirectory() as outside:
            root, out = Path(folder), Path(outside)
            # Selected missing file: evidence does not excuse it.  The artifact
            # lies inside the admitted root, so its canonical name is selected.
            selected_location = str(root / "missing.SLDPRT")
            with self.assertRaises(CadError) as caught:
                self._stale_collect(
                    root,
                    out,
                    write_outside=False,
                    required_names=["sub.SLDASM", "missing.SLDPRT"],
                    location=selected_location,
                    closure_documents={
                        "sub.SLDASM": {
                            "stored_locations": [selected_location],
                            "resolved_targets": [],
                            "resolved_paths": [],
                        }
                    },
                )
            self.assertEqual(caught.exception.code, "dependency_missing")
            location = str(out / "CopyDesignTemp" / "lowerbody.SLDASM")
            # Unrecorded location: hard.
            with self.assertRaises(CadError) as caught:
                self._stale_collect(
                    root,
                    out,
                    write_outside=False,
                    required_names=["sub.SLDASM"],
                    closure_documents={
                        "sub.SLDASM": {"stored_locations": [], "resolved_targets": [], "resolved_paths": []}
                    },
                )
            self.assertEqual(caught.exception.code, "dependency_missing")
            # Active resolved edge path for the same artifact: hard.
            with self.assertRaises(CadError) as caught:
                self._stale_collect(
                    root,
                    out,
                    write_outside=False,
                    required_names=["sub.SLDASM"],
                    closure_documents={
                        "sub.SLDASM": {
                            "stored_locations": [location],
                            "resolved_targets": [],
                            "resolved_paths": [location],
                        }
                    },
                )
            self.assertEqual(caught.exception.code, "dependency_missing")
            # No closure authorisation at all: legacy hard refusal.
            with self.assertRaises(CadError) as caught:
                self._stale_collect(
                    root,
                    out,
                    write_outside=False,
                    closure_documents=None,
                )
            self.assertEqual(caught.exception.code, "dependency_missing")

    def test_ungated_session_materializations_are_not_skipped(self):
        import tempfile
        from pathlib import Path
        from types import SimpleNamespace
        from unittest.mock import Mock
        from description_pipeline.sources.solidworks.native import SolidWorksBackend

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            assembly = root / "robot.SLDASM"
            assembly.write_bytes(b"assembly")
            materialization = root / "swx00000" / "IC~~" / "X.step.SLDASM"
            materialization.parent.mkdir(parents=True)
            materialization.write_bytes(b"temp")
            graph = {str(assembly): ["X.step", str(materialization)]}
            copied = SimpleNamespace(ReplaceReferencedDocument=lambda *args: True)
            backend = SolidWorksBackend()
            backend._app_for_path = Mock(  # type: ignore[method-assign]
                side_effect=[SimpleNamespace(GetDocumentDependencies2=lambda path, *args: graph.get(path, [])), copied]
            )
            result = backend.collect_dependencies(str(assembly), str(root / "copy"))
            self.assertEqual(result["skipped_session_temp"], [])
            self.assertEqual(len(result["files"]), 2)  # without the coverage proof it is copied

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
