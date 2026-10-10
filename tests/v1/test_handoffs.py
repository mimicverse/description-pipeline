"""Real handoff import and binding controls; synthetic CAD is never qualified."""

from __future__ import annotations

import os
import stat
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from description_pipeline.io import PipelineError, digest, inventory
from description_pipeline.sources.solidworks import handoff as handoff_module
from description_pipeline.sources.solidworks.handoff import (
    describe_handoff,
    freeze_handoff,
    import_archive,
    prepare_archive,
)
from description_pipeline.sources.solidworks.revision import package_inventory


def make_handoff(root: Path):
    root.mkdir(parents=True)
    (root / "总装.SLDASM").write_bytes(b"Non-native input control fixture")
    (root / "零件.SLDPRT").write_bytes(b"Non-native part control fixture")
    return root


class HandoffTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.source = make_handoff(self.root / "结构组 新版本")
        self.store = self.root / "imports"

    def test_freeze_binds_all_native_bytes_and_reuses_identical_import(self):
        identity = describe_handoff(self.source)
        self.assertEqual(digest(package_inventory(self.source)), identity["handoff_sha256"])
        package, imported = freeze_handoff(self.source, self.store)
        self.assertEqual(identity, imported)
        self.assertEqual(identity["files"], package_inventory(package))
        self.assertEqual((package, imported), freeze_handoff(self.source, self.store))
        (self.source / "零件.SLDPRT").write_bytes(b"New immutable native revision")
        self.assertEqual(identity["files"], package_inventory(package))
        next_package, next_identity = freeze_handoff(self.source, self.store)
        self.assertNotEqual(package, next_package)
        self.assertNotEqual(identity["handoff_sha256"], next_identity["handoff_sha256"])

    def test_linux_zip_transport_preserves_inventory_and_unicode_names(self):
        (self.source / "~$总装.SLDASM").write_bytes(b"Transient CAD editor lock")
        archive = self.root / "transport.zip"
        sent = prepare_archive(self.source, archive)
        with zipfile.ZipFile(archive) as contents:
            self.assertNotIn("~$总装.SLDASM", contents.namelist())
        package, received = import_archive(archive, self.store)
        self.assertEqual(sent, received)
        self.assertEqual(sent["files"], package_inventory(package))

    def test_zip_import_installs_without_a_second_package_copy(self):
        archive = self.root / "transport.zip"
        sent = prepare_archive(self.source, archive)
        with patch.object(handoff_module.shutil, "copyfile", side_effect=AssertionError("second package copy")):
            package, received = import_archive(archive, self.store)
        self.assertEqual(sent, received)
        self.assertEqual(package, self.store / sent["handoff_sha256"])
        self.assertEqual(sent["files"], package_inventory(package))
        # Only the content-addressed package remains; the extraction staging is gone.
        self.assertEqual([entry.name for entry in self.store.iterdir()], [sent["handoff_sha256"]])

    def test_repeat_import_reuses_the_installed_package_and_rejects_modification(self):
        archive = self.root / "transport.zip"
        sent = prepare_archive(self.source, archive)
        package, _received = import_archive(archive, self.store)
        with patch.object(handoff_module.shutil, "copyfile", side_effect=AssertionError("second package copy")):
            again, repeated = import_archive(archive, self.store)
        self.assertEqual((again, repeated), (package, sent))
        (package / "零件.SLDPRT").write_bytes(b"tampered managed bytes")
        with self.assertRaises(PipelineError) as caught:
            import_archive(archive, self.store)
        self.assertIn("Managed handoff inventory was modified", str(caught.exception))

    def test_concurrent_identical_install_is_tolerated(self):
        archive = self.root / "transport.zip"
        sent = prepare_archive(self.source, archive)
        real_rename = os.rename
        raced = {"done": False}

        def racing_rename(source_path, target_path):
            if not raced["done"]:
                raced["done"] = True
                freeze_handoff(self.source, self.store)  # identical bytes win the race first
            return real_rename(source_path, target_path)

        with patch.object(handoff_module.os, "rename", side_effect=racing_rename):
            package, received = import_archive(archive, self.store)
        self.assertEqual(received, sent)
        self.assertEqual(package, self.store / sent["handoff_sha256"])
        self.assertEqual(sent["files"], package_inventory(package))

    def test_mutation_between_inspection_and_install_is_refused(self):
        archive = self.root / "transport.zip"
        prepare_archive(self.source, archive)
        real_describe = handoff_module.describe_handoff
        real_inventory = handoff_module.inventory
        state = {"after_describe": False}

        def spy_describe(path):
            identity = real_describe(path)
            state["after_describe"] = True
            return identity

        def spy_inventory(path):
            result = real_inventory(path)
            if state["after_describe"] and result:
                return dict.fromkeys(result, "0" * 64)
            return result

        with (
            patch.object(handoff_module, "describe_handoff", side_effect=spy_describe),
            patch.object(handoff_module, "inventory", side_effect=spy_inventory),
            self.assertRaises(PipelineError) as caught,
        ):
            import_archive(archive, self.store)
        self.assertIn("changed during import", str(caught.exception))

    def test_incoming_solidworks_locks_are_excluded_from_the_installed_package(self):
        archive = self.root / "transport.zip"
        sent = prepare_archive(self.source, archive)
        with zipfile.ZipFile(archive, "a") as incoming:
            incoming.writestr("~$零件.SLDPRT", b"incoming editor lock")
        package, received = import_archive(archive, self.store)
        self.assertEqual(received, sent)
        self.assertFalse((package / "~$零件.SLDPRT").exists())
        # Physical inventory, not the lock-ignoring handoff inventory, must match.
        self.assertEqual(inventory(package), received["files"])

    def test_imported_package_physical_inventory_matches_identity(self):
        archive = self.root / "transport.zip"
        prepare_archive(self.source, archive)
        package, received = import_archive(archive, self.store)
        self.assertEqual(inventory(package), received["files"])
        self.assertEqual(package_inventory(package), received["files"])

    def test_native_only_input_needs_no_authored_pipeline_files(self):
        identity = describe_handoff(self.source)
        self.assertNotIn("kind", identity)
        self.assertNotIn("hardware_id", identity)
        self.assertNotIn("revision_sha256", identity)
        for name in ("robot.yaml", "cad-revision.json"):
            with self.subTest(name=name):
                path = self.source / name
                path.write_text("not a native engineering file")
                with self.assertRaises(PipelineError):
                    freeze_handoff(self.source, self.store)
                path.unlink()
        (self.source / "总装.SLDASM").write_bytes(b"")
        with self.assertRaises(PipelineError):
            freeze_handoff(self.source, self.store)

    def test_linked_source_parent_and_modified_managed_import_are_rejected(self):
        alias = self.root / "linked-parent"
        alias.symlink_to(self.source.parent, target_is_directory=True)
        with self.assertRaises(PipelineError):
            describe_handoff(alias / self.source.name)
        package, _ = freeze_handoff(self.source, self.store)
        (package / "零件.SLDPRT").write_bytes(b"Changed frozen bytes")
        with self.assertRaises(PipelineError):
            freeze_handoff(self.source, self.store)

    def test_source_changes_during_copy_do_not_install_mixed_bytes(self):
        import shutil

        copyfile = shutil.copyfile

        def copying(source, target):
            result = copyfile(source, target)
            if Path(source).name == "零件.SLDPRT":
                Path(source).write_bytes(b"Changed while collecting")
            return result

        with (
            patch("description_pipeline.sources.solidworks.handoff.shutil.copyfile", side_effect=copying),
            self.assertRaises(PipelineError),
        ):
            freeze_handoff(self.source, self.store)
        self.assertEqual([], list(self.store.iterdir()))

    def test_store_cannot_be_inside_source_or_through_a_linked_parent(self):
        with self.assertRaises(PipelineError):
            freeze_handoff(self.source, self.source / "imports")
        self.assertFalse((self.source / "imports").exists())
        parent = self.root / "linked-store"
        parent.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(PipelineError):
            freeze_handoff(self.source, parent / "unexpected")
        self.assertFalse((self.root / "unexpected").exists())

    def test_unsafe_zip_members_are_rejected_without_path_escape(self):
        archive = self.root / "unsafe.zip"
        for members in (("../outside.txt",), ("a.txt", "A.txt"), ("a/./b",), ("a\\b",), ("/absolute",)):
            with self.subTest(members=members):
                with zipfile.ZipFile(archive, "w") as output:
                    for name in members:
                        output.writestr(name, b"unsafe")
                with self.assertRaises(PipelineError):
                    import_archive(archive, self.store)
        member = zipfile.ZipInfo("symlink")
        member.external_attr = (stat.S_IFLNK | 0o777) << 16
        with zipfile.ZipFile(archive, "w") as output:
            output.writestr(member, "../outside.txt")
        with self.assertRaises(PipelineError):
            import_archive(archive, self.store)
        self.assertFalse((self.root / "outside.txt").exists())
