import os
import hashlib
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from description_pipeline.io import (
    PipelineError,
    _windows_extended_path,
    confined,
    file_digest,
    inventory,
    read_data,
    write_json,
)
from description_pipeline.sources.snapshot import verify_snapshot, write_manifest


class InputIntegrityTests(unittest.TestCase):
    def test_windows_extended_path_normalizes_and_only_prefixes_long_values(self):
        self.assertEqual(_windows_extended_path("C:/work/../model/file.txt"), "C:\\model\\file.txt")
        self.assertEqual(_windows_extended_path("C:/short/file.txt"), "C:\\short\\file.txt")
        self.assertTrue(_windows_extended_path("C:" + "/x" * 130).startswith("\\\\?\\C:"))
        self.assertTrue(_windows_extended_path("\\\\server\\share\\" + "x" * 240).startswith("\\\\?\\UNC\\"))
        already = "\\\\?\\C:\\already\\long"
        self.assertEqual(_windows_extended_path(already), already)

    @unittest.skipUnless(os.name == "nt", "Windows MAX_PATH without registry changes")
    def test_long_snapshot_is_complete_and_rejects_mutation_and_junction(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        physical = Path("\\\\?\\" + str(root))
        self.addCleanup(shutil.rmtree, physical, ignore_errors=True)
        snapshot = root / "candidate/sources/snapshots" / ("a" * 64)
        physical_snapshot = physical / snapshot.relative_to(root)
        write_json(snapshot / "scene.json", {"evidence": "fixture"})
        relative = "source/" + "cad-" * 35 + ".SLDPRT"
        payload = physical_snapshot / relative
        payload.parent.mkdir(parents=True)
        payload.write_bytes(b"frozen source")
        self.assertGreater(len(str(snapshot / relative)), 260)
        manifest = write_manifest(snapshot, kind="fixture", identity={"test": "MAX_PATH"}, evidence_class="fixture")
        self.assertEqual(set(manifest["files"]), {"scene.json", relative})
        self.assertEqual(verify_snapshot(snapshot), manifest)
        self.assertEqual(file_digest(confined(snapshot, relative)), hashlib.sha256(b"frozen source").hexdigest())
        payload.write_bytes(b"changed source")
        with self.assertRaisesRegex(PipelineError, "changed"):
            verify_snapshot(snapshot)
        # The junction itself is deeper than MAX_PATH and must not disappear
        # from confinement/inventory checks while extended reads follow it.
        parent = physical_snapshot / ("deep-" * 25)
        parent.mkdir()
        link = parent / "redirect"
        outside = physical / "outside"
        outside.mkdir()
        (outside / "data.json").write_text("{}")
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)], capture_output=True, check=True)
        try:
            with self.assertRaisesRegex(PipelineError, "Symlink"):
                inventory(snapshot)
            with self.assertRaisesRegex(PipelineError, "Symlink"):
                confined(snapshot, link.relative_to(physical_snapshot).as_posix() + "/data.json")
        finally:
            link.rmdir()

    def test_windows_path_aliases_cannot_name_an_artifact(self):
        with tempfile.TemporaryDirectory() as temporary:
            for relative in ("C:relative", "mesh.stl:stream", "con", "folder/NUL.txt", "file.", "file ", "line\nend"):
                with self.subTest(relative=relative), self.assertRaises(PipelineError):
                    confined(Path(temporary), relative, exists=False)

    @unittest.skipIf(os.name == "nt", "Windows normally prevents case-distinct siblings")
    def test_case_colliding_paths_cannot_be_shipped_to_windows(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "Mesh.stl").write_bytes(b"one")
            (root / "mesh.stl").write_bytes(b"two")
            with self.assertRaisesRegex(PipelineError, "Case-colliding"):
                inventory(root)

    @unittest.skipUnless(os.name == "nt", "Windows junction semantics")
    def test_windows_junction_cannot_redirect_artifact_inventory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            outside = root / "outside"
            outside.mkdir()
            (outside / "data").write_bytes(b"evidence")
            link = root / "sources"
            subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)], capture_output=True, check=True)
            try:
                with self.assertRaisesRegex(PipelineError, "Symlink"):
                    inventory(link)
            finally:
                link.rmdir()

    def test_windows_utf8_bom_does_not_change_data_or_bypass_duplicate_checks(self):
        with tempfile.TemporaryDirectory() as temporary:
            for suffix, valid, duplicate in (
                ("json", '{"name":"关节"}', '{"mass":1,"mass":2}'),
                ("yaml", "name: 关节\n", "mass: 1\nmass: 2\n"),
            ):
                path = Path(temporary) / f"config.{suffix}"
                path.write_text(valid, encoding="utf-8-sig")
                self.assertEqual(read_data(path), {"name": "关节"})
                path.write_text(duplicate, encoding="utf-8-sig")
                with self.assertRaisesRegex(PipelineError, "Duplicate input key"):
                    read_data(path)

    def test_duplicate_fields_cannot_silently_select_a_different_value(self):
        cases = {
            "config.json": '{"inertial": {"mass": 1, "mass": 2}}',
            "config.yaml": "inertial:\n  mass: 1\n  mass: 2\n",
            "merged.yaml": "default: &base {mass: 1}\ninertial: {<<: *base, mass: 2}\n",
        }
        with tempfile.TemporaryDirectory() as temporary:
            for name, text in cases.items():
                with self.subTest(name=name):
                    path = Path(temporary) / name
                    path.write_text(text)
                    with self.assertRaisesRegex(PipelineError, "Duplicate input key"):
                        read_data(path)

    def test_top_level_asset_directory_cannot_redirect_outside_workspace(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            outside = root / "outside"
            outside.mkdir()
            (outside / "evidence.json").write_text("{}")
            linked = root / "sources"
            try:
                linked.symlink_to(outside, target_is_directory=True)
            except OSError as error:
                if getattr(error, "winerror", None) == 1314:
                    self.skipTest(
                        "creating directory symlinks requires Developer Mode or SeCreateSymbolicLinkPrivilege"
                    )
                raise
            with self.assertRaisesRegex(PipelineError, "Symlink"):
                inventory(linked)

    def test_inventory_scan_errors_are_fatal(self):
        failure = OSError("scan interrupted")
        with tempfile.TemporaryDirectory() as temporary, patch("description_pipeline.io.os.walk") as walk:
            walk.side_effect = lambda *args, **kwargs: kwargs["onerror"](failure)
            with self.assertRaisesRegex(PipelineError, "Cannot scan artifact directory"):
                inventory(Path(temporary))
