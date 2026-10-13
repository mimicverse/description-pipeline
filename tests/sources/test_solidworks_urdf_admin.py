"""Focused admin-deferral tests for the bundle verifier (no CAD, no pipeline)."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from description_pipeline.io import PipelineError
from description_pipeline.sources.solidworks.revision import package_inventory
from description_pipeline.verification import solidworks_urdf


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
    def _root(self, directory, definition, identity):
        root = Path(directory)
        input_root = root / "input"
        (input_root / "discovery").mkdir(parents=True)
        (input_root / "robot.SLDASM").write_bytes(b"native bytes for the admin-deferral tests")
        (input_root / "discovery/native-discovery.json").write_text(
            json.dumps(
                {
                    "namespace": "dp",
                    "raw": {"identity": identity},
                    "identity": identity,
                    "native_files": {},
                }
            )
        )
        (root / "reports").mkdir()
        (root / "reports/input.json").write_text(
            json.dumps(
                {
                    "passed": True,
                    "input": definition,
                    "package_files": package_inventory(input_root),
                }
            )
        )
        return root, input_root

    def _run(self, root, definition, diagnostics):
        with mock.patch.object(solidworks_urdf, "inspect_package", return_value={"passed": True, "input": definition}):
            return solidworks_urdf._input(root, diagnostics)

    def test_absent_seal_with_incomplete_admin_metadata_is_a_readiness_warning(self):
        # Valid hardware but a missing owner/revision keeps the seal optional.
        with tempfile.TemporaryDirectory() as directory:
            identity = full_identity()
            identity["revision"] = None
            identity["owner"] = None
            definition = {"hardware_id": "fixture", "source": {"robot_name": "fixture"}}
            root, _ = self._root(directory, definition, identity)
            diagnostics: list[dict] = []
            returned = self._run(root, definition, diagnostics)
            self.assertEqual(returned, definition)
            self.assertEqual([item["code"] for item in diagnostics], ["input.cad_revision_not_sealed"])
            self.assertEqual(diagnostics[0]["detail"]["missing"], ["revision", "owner"])
            self.assertTrue(all(item["blocking"] is False for item in diagnostics))

    def test_absent_seal_with_null_identity_is_a_readiness_warning(self):
        with tempfile.TemporaryDirectory() as directory:
            identity = dict.fromkeys(("hardware_id", "revision", "owner", "change_summary", "control", "robot_name"))
            definition = {"hardware_id": None, "source": {"robot_name": "robot"}}
            root, _ = self._root(directory, definition, identity)
            diagnostics: list[dict] = []
            self._run(root, definition, diagnostics)
            self.assertEqual(
                diagnostics[0]["detail"]["missing"],
                ["hardware_id", "revision", "owner", "change_summary", "control"],
            )

    def test_absent_seal_with_complete_admin_metadata_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            definition = {"hardware_id": "fixture", "source": {"robot_name": "fixture"}}
            root, _ = self._root(directory, definition, full_identity())
            with mock.patch.object(
                solidworks_urdf, "inspect_package", return_value={"passed": True, "input": definition}
            ), self.assertRaises(PipelineError):
                solidworks_urdf._input(root, [])

    def test_supplied_malformed_seal_keeps_failing_with_incomplete_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            identity = full_identity()
            identity["revision"] = None
            identity["owner"] = None
            definition = {"hardware_id": "fixture", "source": {"robot_name": "fixture"}}
            root, input_root = self._root(directory, definition, identity)
            (input_root / "cad-revision.json").write_text(json.dumps({"schema_version": "not-the-contract"}))
            with mock.patch.object(
                solidworks_urdf, "inspect_package", return_value={"passed": True, "input": definition}
            ), self.assertRaises(PipelineError):
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
        input_root = Path(directory)
        (input_root / "discovery").mkdir()
        for name, data in contents.items():
            path = input_root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        native = {name: hashlib.sha256(data).hexdigest() for name, data in contents.items()}
        payload = {
            "namespace": "dp",
            "native_files": native,
            "raw": {"files": dict(raw_files or {})},
            "identity": {},
        }
        record = json.dumps(payload).encode()
        (input_root / "discovery/native-discovery.json").write_bytes(record)
        (input_root / "robot.yaml").write_text(
            json.dumps(
                {
                    "provenance": {
                        "native_inventory_sha256": inventory_digest
                        if inventory_digest is not None
                        else canonical_digest(native),
                        "discovery_sha256": hashlib.sha256(record).hexdigest(),
                    }
                }
            )
        )
        return input_root, native

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
            (input_root / "a.SLDPRT").write_bytes(b"tampered")
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
            input_root = Path(directory)
            (input_root / "discovery").mkdir()
            (input_root / "discovery/native-discovery.json").write_text(json.dumps({"native_files": {}}))
            with self.assertRaises(PipelineError):
                solidworks_urdf._handoff_native_hashes(input_root)


if __name__ == "__main__":
    unittest.main()
