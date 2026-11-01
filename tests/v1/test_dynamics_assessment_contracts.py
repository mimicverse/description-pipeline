"""Assessment identities reject mutable, cross-member and escaped inputs."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

from description_pipeline.artifacts import ArtifactStore
from description_pipeline.dynamics_assessment.contracts import (
    REQUEST_SCHEMA,
    RUNTIME_SCHEMA,
    SOURCE_SCHEMA,
    AssessmentError,
    AssessmentRequest,
    MemberRef,
    RuntimePin,
    SourceBinding,
    auxiliary_dag_run_id,
    dispatch_conf,
    parse_json,
)
from description_pipeline.io import PipelineError, digest


def source_case(tmp_path: Path):
    artifacts = ArtifactStore(tmp_path / "artifacts")
    inputs = tmp_path / "source"
    inputs.mkdir()
    contents = {
        "robot.urdf": b"<robot name='test'/>",
        "robot.xml": b"<mujoco/>",
        "urdf-certificate.json": b'{"scope":"software","warnings":[{"code":"SOURCE_LIMITATION"}]}',
        "mjcf-certificate.json": b'{"scope":"structural"}',
    }
    for name, content in contents.items():
        (inputs / name).write_bytes(content)
    artifact = artifacts.seal_tree(
        inputs,
        kind="source_fixture",
        producer_run_id="source-run",
        producer_stage=None,
        producer_tool_sha256=digest({"tool": "source"}),
    )

    def member(name):
        return MemberRef(artifact, name, len(contents[name]), hashlib.sha256(contents[name]).hexdigest()).to_record()

    source = SourceBinding(
        {
            "schema": SOURCE_SCHEMA,
            "run_id": "source-run",
            "dag_run_id": "source-dag-run",
            "subject_sha256": digest(contents["robot.xml"].decode()),
            "urdf": member("robot.urdf"),
            "mjcf": member("robot.xml"),
            "certificates": {"urdf": member("urdf-certificate.json"), "mjcf": member("mjcf-certificate.json")},
        }
    )
    return (artifacts, source)


def runtime_pin():
    return RuntimePin(
        {
            "schema": RUNTIME_SCHEMA,
            "source_commit": "a" * 40,
            "wheel_sha256": "b" * 64,
            "files": {"teleop/__init__.py": "c" * 64},
            "dependency_versions": {"robot-teleop": "0.3.0", "mujoco": "3.13.0", "numpy": "2.4.1"},
        }
    )


def request_case(tmp_path):
    artifacts, source = source_case(tmp_path)
    request = AssessmentRequest(
        {
            "schema": REQUEST_SCHEMA,
            "principal": "tenant:owner",
            "source": source.to_record(),
            "bundle": None,
            "runtime": runtime_pin().to_record(),
            "executor_sha256": "d" * 64,
        }
    )
    return (artifacts, request)


class AssessmentCase(unittest.TestCase):
    def setUp(self):
        super().setUp()
        self._temporary = tempfile.TemporaryDirectory(prefix="assessment-test-")
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)


class ContractsTests(AssessmentCase):
    def test_original_engineering_certificate_capacity_and_exact_byte_preservation(self):
        location = os.environ.get("ASSESSMENT_TEST_ORIGINAL_MJCF_CERTIFICATE")
        if not location:
            self.skipTest("The original engineering certificate is required for the capacity-only regression")
        original = Path(location)
        content = original.read_bytes()
        expected = "09c46bc375cb41c7731f331ff93147d2258ba6d4e8a0e43fe65b884e02a7d34f"
        self.assertEqual(len(content), 3161953)
        self.assertEqual(hashlib.sha256(content).hexdigest(), expected)
        source = json.loads(content)
        self.assertIn("input_binding", source)
        self.assertIn("source_artifacts", source)
        store = ArtifactStore(self.root / "capacity-artifacts")
        copied = self.root / "original-certificate"
        copied.mkdir()
        (copied / "mjcf-certificate.json").write_bytes(content)
        artifact = store.seal_tree(
            copied,
            kind="original_certificate_capacity_test",
            producer_run_id=None,
            producer_stage=None,
            producer_tool_sha256=digest({"test": "capacity_and_byte_preservation_only"}),
        )
        member = MemberRef(artifact, "mjcf-certificate.json", len(content), expected)
        self.assertLess(len(json.dumps(member.to_record()).encode()), 4096)
        self.assertEqual(member.read(store), content)
        detached = store.materialize(artifact, self.root / "detached-original")
        self.assertEqual((detached / member.path).read_bytes(), content)
        self.assertEqual(json.loads((detached / member.path).read_bytes()), source)
        self.assertEqual(hashlib.sha256(original.read_bytes()).hexdigest(), expected)

    def test_source_verification_reads_exact_members_and_retains_warnings(self):
        tmp_path = self.root
        artifacts, request = request_case(tmp_path)
        request.source.verify(artifacts)
        certificate = MemberRef.from_record(request.source.record["certificates"]["urdf"])
        assert parse_json(certificate.read(artifacts))["warnings"] == [{"code": "SOURCE_LIMITATION"}]
        assert "warnings" not in request.source.record
        assert request.bundle is None

    def test_replaced_source_bytes_are_refused_before_runtime(self):
        tmp_path = self.root
        artifacts, source = source_case(tmp_path)
        member = MemberRef.from_record(source.record["urdf"])
        path = artifacts.objects / member.artifact.artifact_id / member.path
        path.write_bytes(path.read_bytes().replace(b"test", b"fake"))
        with self.assertRaises(PipelineError):
            source.verify(artifacts)

    def test_rehashed_member_cannot_override_registered_artifact_manifest(self):
        tmp_path = self.root
        artifacts, source = source_case(tmp_path)
        changed = source.to_record()
        changed["urdf"]["sha256"] = "0" * 64
        with self.assertRaisesRegex(AssessmentError, "registered source"):
            SourceBinding(changed).verify(artifacts)

    def test_request_values_do_not_alias_callers_or_returned_records(self):
        tmp_path = self.root
        _artifacts, request = request_case(tmp_path)
        original = request.sha256
        record = request.to_record()
        record["source"]["certificates"].clear()
        request.record["runtime"]["files"].clear()
        assert request.sha256 == original
        assert set(request.source.record["certificates"]) == {"urdf", "mjcf"}
        assert request.runtime.record["files"]

    def _check_runtime_file_paths_are_confined(self, path):
        record = runtime_pin().to_record()
        record["files"] = {path: "c" * 64}
        with self.assertRaises(AssessmentError):
            RuntimePin(record)

    def test_runtime_file_paths_are_confined_case_1(self):
        self._check_runtime_file_paths_are_confined("../teleop/run.py")

    def test_runtime_file_paths_are_confined_case_2(self):
        self._check_runtime_file_paths_are_confined("/teleop/run.py")

    def test_runtime_file_paths_are_confined_case_3(self):
        self._check_runtime_file_paths_are_confined("teleop/../run.py")

    def test_runtime_file_paths_are_confined_case_4(self):
        self._check_runtime_file_paths_are_confined("teleop\\run.py")

    def test_wrong_bundle_role_cannot_impersonate_an_execution_bundle(self):
        tmp_path = self.root
        _artifacts, request = request_case(tmp_path)
        record = request.to_record()
        record["bundle"] = record["source"]["urdf"]["artifact"]
        with self.assertRaisesRegex(AssessmentError, "separately sealed"):
            AssessmentRequest(record)

    def _check_ambiguous_and_nonfinite_json_is_not_accepted(self, raw):
        with self.assertRaises(AssessmentError):
            parse_json(raw)

    def test_ambiguous_and_nonfinite_json_is_not_accepted_case_1(self):
        self._check_ambiguous_and_nonfinite_json_is_not_accepted(b'{"a":1,"a":2}')

    def test_ambiguous_and_nonfinite_json_is_not_accepted_case_2(self):
        self._check_ambiguous_and_nonfinite_json_is_not_accepted(b'{"a":NaN}')

    def test_ambiguous_and_nonfinite_json_is_not_accepted_case_3(self):
        self._check_ambiguous_and_nonfinite_json_is_not_accepted(b"[]")

    def test_auxiliary_dispatch_has_its_own_identity_without_stage_fields(self):
        tmp_path = self.root
        _artifacts, request = request_case(tmp_path)
        conf = dispatch_conf("request", "a" * 64, request)
        assert conf == {"request_id": "request", "attempt_id": "a" * 64, "request_sha256": request.sha256}
        assert auxiliary_dag_run_id("a" * 64) == "assessment-" + "a" * 64
        assert "publish" not in json.dumps(conf)
