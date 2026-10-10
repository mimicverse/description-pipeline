"""Exercise the actual batch ZIP boundary without any CAD process."""

from __future__ import annotations

import copy
import hashlib
import stat
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from description_pipeline.io import inventory
from description_pipeline.orchestration import selection_transport
from description_pipeline.orchestration.selection_client import prepare_selection_batch
from description_pipeline.orchestration.selection_transport import StagedSelection
from description_pipeline.sources.solidworks.selection import SelectionError, seal_selection


class SelectionTransportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "intake"
        self.source.mkdir()
        self.data = {
            "root.SLDASM": b"synthetic assembly",
            "parts/link.SLDPRT": b"synthetic referenced part",
            "decoy.SLDPRT": b"unrelated native document",
            "notes.txt": b"unrelated notes",
        }
        for name, data in self.data.items():
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        self.files = inventory(self.source)
        self.reader = "a" * 64
        self.session = StagedSelection(self.root / "stage", self.files, "root.SLDASM", reader_sha256=self.reader)
        self.serial = 0

    def archive(self, files):
        self.serial += 1
        archive = self.root / f"batch-{self.serial}.zip"
        prepare_selection_batch(self.source, archive, files, expected_upload=self.files)
        return archive

    def push(self):
        snapshot = self.session.snapshot()
        archive = self.archive(snapshot["requested_files"])
        self.assertTrue(self.session.accept_batch(archive, snapshot["round_id"]))
        return archive, snapshot["round_id"]

    def observe(self, name, references):
        self.session.record_observations({name: {"sha256": self.files[name], "references": references}})

    def ready(self):
        self.push()
        self.observe("root.SLDASM", ["parts/link.SLDPRT"])
        self.push()
        self.observe("parts/link.SLDPRT", [])

    def manifest(self):
        return seal_selection(
            self.files,
            "root.SLDASM",
            self.session.observations,
            selected_files=inventory(self.session.stage),
            reader_sha256=self.reader,
        )

    def test_exact_rounds_select_dependencies_and_never_install_decoys(self):
        self.assertEqual(self.session.snapshot()["requested_files"], {"root.SLDASM": self.files["root.SLDASM"]})
        self.ready()
        self.assertEqual(self.session.state, "ready")
        self.assertEqual(set(inventory(self.session.stage)), {"root.SLDASM", "parts/link.SLDPRT"})
        manifest = self.manifest()
        self.assertEqual(self.session.seal(manifest), manifest)
        self.assertEqual(self.session.snapshot()["state"], "sealed")
        self.assertEqual(self.session.seal(manifest), manifest)
        self.assertEqual(set(manifest["excluded_files"]), {"decoy.SLDPRT", "notes.txt"})

    def test_logically_identical_retry_is_idempotent_even_after_an_observation(self):
        archive, round_id = self.push()
        self.assertFalse(self.session.accept_batch(archive, round_id))
        self.observe("root.SLDASM", ["parts/link.SLDPRT"])
        before = self.session.snapshot()
        self.assertFalse(self.session.accept_batch(archive, round_id))
        self.assertEqual(self.session.snapshot(), before)

    def test_changed_retry_cannot_overwrite_the_stage(self):
        _archive, round_id = self.push()
        archive = self.root / "changed.zip"
        with zipfile.ZipFile(archive, "w") as output:
            output.writestr("root.SLDASM", b"changed")
        with self.assertRaises(SelectionError) as caught:
            self.session.accept_batch(archive, round_id)
        self.assertEqual(caught.exception.code, "selection_source_changed")
        self.assertEqual((self.session.stage / "root.SLDASM").read_bytes(), self.data["root.SLDASM"])

    def test_extra_missing_and_duplicate_members_leave_stage_empty(self):
        for names in (["root.SLDASM", "decoy.SLDPRT"], ["decoy.SLDPRT"], []):
            with self.subTest(names=names):
                archive = self.root / "invalid.zip"
                with zipfile.ZipFile(archive, "w") as output:
                    for name in names:
                        output.writestr(name, self.data[name])
                with self.assertRaises(SelectionError) as caught:
                    self.session.accept_batch(archive, self.session.round_id)
                self.assertEqual(caught.exception.code, "selection_batch_inventory")
                self.assertEqual(inventory(self.session.stage), {})
        with zipfile.ZipFile(archive, "w") as output:
            output.writestr("root.SLDASM", self.data["root.SLDASM"])
            with self.assertWarns(UserWarning):
                output.writestr("root.SLDASM", self.data["root.SLDASM"])
        with self.assertRaises(SelectionError):
            self.session.accept_batch(archive, self.session.round_id)
        self.assertEqual(inventory(self.session.stage), {})

    def test_link_member_is_refused_before_any_installation(self):
        archive = self.root / "link.zip"
        with zipfile.ZipFile(archive, "w") as output:
            member = zipfile.ZipInfo("root.SLDASM")
            member.create_system = 3
            member.external_attr = (stat.S_IFLNK | 0o777) << 16
            output.writestr(member, self.data["root.SLDASM"])
        with self.assertRaises(SelectionError) as caught:
            self.session.accept_batch(archive, self.session.round_id)
        self.assertEqual(caught.exception.code, "selection_batch_member")
        self.assertEqual(inventory(self.session.stage), {})

    def test_stale_round_cannot_request_or_install_more_files(self):
        archive = self.archive({"decoy.SLDPRT": self.files["decoy.SLDPRT"]})
        with self.assertRaises(SelectionError) as caught:
            self.session.accept_batch(archive, "0" * 64)
        self.assertEqual(caught.exception.code, "selection_round_stale")
        self.assertEqual(inventory(self.session.stage), {})

    def test_reader_cannot_supply_observations_for_untransferred_documents(self):
        self.push()
        with self.assertRaises(SelectionError) as caught:
            self.session.record_observations(
                {
                    "root.SLDASM": {"sha256": self.files["root.SLDASM"], "references": ["parts/link.SLDPRT"]},
                    "parts/link.SLDPRT": {"sha256": self.files["parts/link.SLDPRT"], "references": []},
                }
            )
        self.assertEqual(caught.exception.code, "selection_observation_invalid")
        self.assertEqual(self.session.state, "reading")

    def test_resealed_forged_graph_cannot_replace_native_observations(self):
        self.ready()
        graph = copy.deepcopy(self.session.observations)
        graph["parts/link.SLDPRT"]["references"] = ["root.SLDASM"]
        forged = seal_selection(
            self.files,
            "root.SLDASM",
            graph,
            selected_files=self.session.staged_files,
            reader_sha256=self.reader,
        )
        with self.assertRaises(SelectionError) as caught:
            self.session.seal(forged)
        self.assertEqual(caught.exception.code, "selection_manifest_changed")

    def test_changed_stage_refuses_seal(self):
        self.ready()
        manifest = self.manifest()
        (self.session.stage / "parts/link.SLDPRT").write_bytes(b"changed after query")
        with self.assertRaises(SelectionError) as caught:
            self.session.seal(manifest)
        self.assertEqual(caught.exception.code, "selection_inventory_changed")

    def test_selected_bytes_are_bounded_across_rounds(self):
        for name in ("a.SLDASM", "b.SLDPRT", "c.SLDPRT"):
            (self.source / name).write_bytes(b"x" * 100)
        files = {name: hashlib.sha256(b"x" * 100).hexdigest() for name in ("a.SLDASM", "b.SLDPRT", "c.SLDPRT")}
        session = StagedSelection(self.root / "capped", files, "a.SLDASM", reader_sha256=self.reader)
        with patch.object(selection_transport, "MAX_HANDOFF_BYTES", 250):
            for name, next_name in (("a.SLDASM", "b.SLDPRT"), ("b.SLDPRT", "c.SLDPRT")):
                archive = self.root / f"{name}.zip"
                with zipfile.ZipFile(archive, "w") as output:
                    output.writestr(name, b"x" * 100)
                session.accept_batch(archive, session.round_id)
                session.record_observations({name: {"sha256": files[name], "references": [next_name]}})
            archive = self.root / "c.zip"
            with zipfile.ZipFile(archive, "w") as output:
                output.writestr("c.SLDPRT", b"x" * 100)
            with self.assertRaises(SelectionError) as caught:
                session.accept_batch(archive, session.round_id)
            self.assertEqual(caught.exception.code, "selection_batch_size")
        self.assertEqual(set(inventory(session.stage)), {"a.SLDASM", "b.SLDPRT"})


if __name__ == "__main__":
    unittest.main()
