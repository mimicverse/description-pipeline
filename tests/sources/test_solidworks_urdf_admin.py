"""Focused admin-deferral tests for the bundle verifier (no CAD, no pipeline)."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from description_pipeline.io import PipelineError
from description_pipeline.verification import solidworks_urdf

from . import support


def full_identity():
    return {
        "hardware_id": "fixture",
        "revision": "r1",
        "owner": "test-owner",
        "change_summary": "summary",
        "control": {"system": "handoff", "reference": "fixture"},
        "robot_name": "fixture",
    }


def canonical_digest(files):
    return hashlib.sha256(
        (json.dumps(files, sort_keys=True, ensure_ascii=False, allow_nan=False, indent=2) + "\n").encode()
    ).hexdigest()


class InputSealReadinessTests(unittest.TestCase):
    def _root(self, directory, *, raw_identity, revision):
        root = Path(directory) / "delivery"
        content = b"native bytes for the admin-deferral tests"
        record = support.frozen_discovery_payload(
            {"namespace": "dp", "raw": {"identity": raw_identity}, "identity": raw_identity},
            files={"robot.SLDASM": hashlib.sha256(content).hexdigest()},
            robot={
                "hardware_id": "fixture",
                "source": {"robot_name": "fixture", "bodies": [], "joints": [], "frames": []},
                "checks": {},
            },
        )
        source = support.default_source_record(
            main_assembly="robot.SLDASM",
            configuration="Default",
            hardware_id="fixture",
            revision=revision,
        )
        return support.write_target_delivery(
            root,
            capture={"declared_masses": {"items": {}, "evidence": None}},
            discovery=record,
            source=source,
            originals={"robot.SLDASM": content.decode("utf-8")},
        )

    def test_absent_seal_with_incomplete_admin_metadata_is_a_readiness_warning(self):
        # Valid hardware but a missing owner/revision keeps the seal optional.
        with tempfile.TemporaryDirectory() as directory:
            identity = full_identity()
            identity["revision"] = None
            identity["owner"] = None
            root = self._root(directory, raw_identity=identity, revision=None)
            diagnostics: list[dict] = []
            returned = solidworks_urdf._input(root, diagnostics)
            self.assertEqual(returned["source"]["assembly"], "robot.SLDASM")
            self.assertEqual(returned["hardware_id"], "fixture")
            self.assertEqual([item["code"] for item in diagnostics], ["input.cad_revision_absent"])
            self.assertEqual(diagnostics[0]["detail"]["missing"], ["revision", "owner"])
            self.assertTrue(all(item["blocking"] is False for item in diagnostics))

    def test_absent_seal_with_null_identity_is_a_readiness_warning(self):
        with tempfile.TemporaryDirectory() as directory:
            identity = dict.fromkeys(("hardware_id", "revision", "owner", "change_summary", "control", "robot_name"))
            root = self._root(directory, raw_identity=identity, revision=None)
            diagnostics: list[dict] = []
            solidworks_urdf._input(root, diagnostics)
            self.assertEqual(
                diagnostics[0]["detail"]["missing"],
                ["hardware_id", "revision", "owner", "change_summary", "control"],
            )

    def test_absent_revision_stays_missing_with_complete_admin_metadata(self):
        """The revision label is optional raw metadata; a complete authored
        identity does not make its absence blocking."""

        with tempfile.TemporaryDirectory() as directory:
            root = self._root(directory, raw_identity=full_identity(), revision=None)
            diagnostics: list[dict] = []
            returned = solidworks_urdf._input(root, diagnostics)
            self.assertEqual(returned["source"]["assembly"], "robot.SLDASM")
            self.assertEqual([item["code"] for item in diagnostics], ["input.cad_revision_absent"])
            self.assertEqual(diagnostics[0]["detail"]["missing"], [])
            self.assertTrue(all(item["blocking"] is False for item in diagnostics))

    def test_malformed_revision_metadata_keeps_failing_with_incomplete_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            identity = full_identity()
            identity["revision"] = None
            identity["owner"] = None
            root = self._root(directory, raw_identity=identity, revision="")
            with self.assertRaises(PipelineError):
                solidworks_urdf._input(root, [])


class UrdfRobotLabelTests(unittest.TestCase):
    @staticmethod
    def _root(directory, name):
        root = Path(directory)
        (root / "urdf").mkdir()
        (root / "urdf/robot.urdf").write_text(f'<robot name="{name}"><link name="base"/></robot>')
        return root

    def test_robot_tag_must_match_the_definition_robot_name(self):
        model = {"links": [{"name": "base"}], "frames": [], "joints": []}
        with tempfile.TemporaryDirectory() as directory:
            root = self._root(directory, "robot")
            definition = {"hardware_id": None, "source": {"robot_name": "robot"}}
            document = solidworks_urdf._xml(root, definition, model)
            self.assertEqual(document.get("name"), "robot")
        with tempfile.TemporaryDirectory() as directory:
            # A hardware label is not the URDF identity anymore.
            root = self._root(directory, "fixture")
            definition = {"hardware_id": "fixture", "source": {"robot_name": "robot"}}
            with self.assertRaises(PipelineError):
                solidworks_urdf._xml(root, definition, model)


class HandoffHashBindingTests(unittest.TestCase):
    def _package(self, directory, contents, *, raw_files=None, inventory_digest=None):
        root = Path(directory) / "delivery"
        native = {name: hashlib.sha256(data).hexdigest() for name, data in contents.items()}
        payload = support.frozen_discovery_payload(
            {"namespace": "dp", "raw": {"files": dict(raw_files or {})}, "identity": {}},
            files=native,
            robot={"hardware_id": None, "source": {"robot_name": "robot"}, "checks": {}},
        )
        if inventory_digest is not None:
            payload["native_inventory_sha256"] = inventory_digest
        return support.write_target_delivery(
            root,
            capture={},
            discovery=payload,
            source=support.default_source_record(),
            originals={name: data.decode("utf-8") for name, data in contents.items()},
        ), native

    def test_frozen_native_inventory_binds_without_a_seal(self):
        contents = {"a.SLDPRT": b"aaa", "b.SLDPRT": b"bbb"}
        with tempfile.TemporaryDirectory() as directory:
            hashes = {"a": hashlib.sha256(b"aaa").hexdigest(), "b": hashlib.sha256(b"bbb").hexdigest()}
            input_root, native = self._package(directory, contents, raw_files={"a.SLDPRT": hashes["a"]})
            self.assertEqual(solidworks_urdf._handoff_native_hashes(input_root), set(native.values()))

    def test_foreign_raw_hash_cannot_widen_allowed_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            input_root, _ = self._package(
                directory,
                {"a.SLDPRT": b"aaa"},
                raw_files={"foreign.SLDPRT": "f" * 64},
            )
            with self.assertRaises(PipelineError):
                solidworks_urdf._handoff_native_hashes(input_root)

    def test_tampered_or_unproven_evidence_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            input_root, _ = self._package(directory, {"a.SLDPRT": b"aaa"})
            (input_root / "input/cad/a.SLDPRT").write_bytes(b"tampered")
            with self.assertRaises(PipelineError):
                solidworks_urdf._handoff_native_hashes(input_root)
        with tempfile.TemporaryDirectory() as directory:
            input_root, _ = self._package(directory, {"a.SLDPRT": b"aaa"}, inventory_digest="0" * 64)
            with self.assertRaises(PipelineError):
                solidworks_urdf._handoff_native_hashes(input_root)

    def test_missing_or_empty_archived_evidence_fails(self):
        with tempfile.TemporaryDirectory() as directory, self.assertRaises((PipelineError, OSError)):
            solidworks_urdf._handoff_native_hashes(Path(directory))
        with tempfile.TemporaryDirectory() as directory:
            input_root, _native = self._package(directory, {})
            with self.assertRaises(PipelineError):
                solidworks_urdf._handoff_native_hashes(input_root)


if __name__ == "__main__":
    unittest.main()
