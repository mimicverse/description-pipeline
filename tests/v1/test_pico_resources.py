"""Real filesystem regressions for the frozen W4 consumer resource formula."""

from pathlib import Path
import hashlib
import os
import tempfile
import unittest
from unittest.mock import patch

from description_pipeline.simulation.resources import (
    SESSION_QUOTA_BYTES,
    SessionResourceError,
    check_session_capacity,
    directory_bytes,
    require_storage,
    admit_storage,
)
from description_pipeline.artifacts import ArtifactStore
from description_pipeline.io import canonical, digest
from description_pipeline.orchestration.report_view import text_warning_inventory
from description_pipeline.pipeline.contracts import ArtifactRef
from description_pipeline.simulation.recording import materialize_source


@unittest.skipUnless(os.name == "posix", "The native session filesystem is POSIX")
class PicoResourceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.policy = {
            "quota_bytes": SESSION_QUOTA_BYTES,
            "finalization_reserve_bytes": 4096,
            "failure_reserve_bytes": 4096,
        }

    def sparse(self, name, size):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as stream:
            stream.truncate(size)
        return path

    def test_host_admission_includes_starting_and_unknown_verification_partials(self):
        self.sparse("active/source.bin", 4096)
        self.sparse("recording-verification-pending/source.bin", 8192)
        self.sparse("recording-verification-unknown/source.bin", 16384)

        class Facts:
            def active_session_ids(self):
                return ["active"]

            def verification_attempts(self):
                return [
                    {
                        "command_id": command,
                        "request": {"partial": "recording-verification-" + command},
                        "result": {"state": state},
                    }
                    for command, state in (("pending", "starting"), ("unknown", "unknown"), ("done", "completed"))
                ]

        with patch("description_pipeline.simulation.resources.require_storage", wraps=require_storage) as checking:
            admit_storage(Facts(), self.root, self.root, 100)
        self.assertEqual(
            100 + 3 * SESSION_QUOTA_BYTES - 4096 - 8192 - 16384, checking.call_args.kwargs["additional_bytes"]
        )

    def test_rejects_the_original_source_compiled_and_stderr_overquota_cases(self):
        record = self.sparse("recording/native/records.jsonl.gz", 34498)
        for name in ("source/padding.bin", "compiled/padding.bin", "worker-stderr.log"):
            with self.subTest(name=name):
                path = self.sparse(name, SESSION_QUOTA_BYTES + 1 - record.stat().st_size)
                try:
                    self.assertEqual(SESSION_QUOTA_BYTES + 1, directory_bytes(self.root))
                    with self.assertRaisesRegex(SessionResourceError, "complete session workspace"):
                        check_session_capacity(self.root, self.policy)
                    self.assertTrue(record.exists())
                finally:
                    path.unlink()

    def test_the_exact_shared_reserve_boundary_preserves_finalization_headroom(self):
        available = (
            SESSION_QUOTA_BYTES - self.policy["finalization_reserve_bytes"] - self.policy["failure_reserve_bytes"]
        )
        path = self.sparse("source/mesh.bin", available)
        self.assertEqual(available, check_session_capacity(self.root, self.policy))
        with path.open("ab") as stream:
            stream.write(b"x")
        with self.assertRaisesRegex(SessionResourceError, "workspace budget"):
            check_session_capacity(self.root, self.policy)

    def test_includes_platform_metadata_and_runtime_temporaries(self):
        sizes = {
            "source/model.xml": 2,
            "compiled/model.mjb": 3,
            "worker-stderr.log": 5,
            "recording/request.json": 7,
            "recording/session.json": 11,
            "recording/native/records.jsonl.gz": 13,
            "recording/native/.manifest.tmp": 17,
        }
        for name, size in sizes.items():
            self.sparse(name, size)
        self.assertEqual(sum(sizes.values()), check_session_capacity(self.root, self.policy))

    def test_does_not_follow_file_or_directory_links_outside_the_private_workspace(self):
        with tempfile.TemporaryDirectory() as external:
            target = Path(external) / "data"
            target.write_bytes(b"outside")
            for destination in (target, Path(external)):
                link = self.root / "linked"
                link.symlink_to(destination)
                try:
                    with self.assertRaisesRegex(SessionResourceError, "link or nonregular"):
                        directory_bytes(self.root)
                finally:
                    link.unlink()

    def test_rejects_nonregular_files(self):
        os.mkfifo(self.root / "pipe")
        with self.assertRaisesRegex(SessionResourceError, "nonregular"):
            directory_bytes(self.root)

    def test_rejects_an_increased_quota_and_invalid_reserves(self):
        for change in (
            {"quota_bytes": SESSION_QUOTA_BYTES + 1},
            {"quota_bytes": True},
            {"finalization_reserve_bytes": -1},
            {"failure_reserve_bytes": 0.5},
        ):
            with self.subTest(change=change), self.assertRaisesRegex(SessionResourceError, "invalid quota or reserve"):
                check_session_capacity(self.root, {**self.policy, **change})

    def test_real_filesystem_storage_admission_rejects_impossible_additional_copy(self):
        require_storage(self.root, additional_bytes=0, reserve_bytes=0)
        with self.assertRaisesRegex(SessionResourceError, "Storage is insufficient"):
            require_storage(self.root, additional_bytes=2**63 - 1, reserve_bytes=4096)

    def test_missing_workspace_is_an_observation_failure(self):
        with self.assertRaisesRegex(SessionResourceError, "real directory"):
            directory_bytes(self.root / "missing")

    def bound_source(self):
        source = self.root / "input"
        source.mkdir()
        (source / "model.xml").write_bytes(b"<mujoco/>")
        report = {
            "report": {
                "checks": [
                    {"id": "provenance.warnings", "details": {"warning_comments": [], "warning_sha256": digest([])}}
                ]
            }
        }
        certificate = source / "certificate.json"
        certificate.write_bytes(canonical(report))
        store = ArtifactStore(self.root / "cas")
        artifact = store.seal_tree(
            source, kind="pico_test_source", producer_run_id=None, producer_stage=None, producer_tool_sha256="c" * 64
        )
        members = {
            item["path"]: {**item, "artifact": artifact.to_record()} for item in store.verify(artifact)["members"]
        }
        descriptor = {
            "files": {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in source.iterdir()},
            "warning_inventory": text_warning_inventory([], members["certificate.json"]),
        }

        def read_member(name, member):
            return store.read_metadata_member(
                ArtifactRef.from_record(member["artifact"]), name, max_bytes=64 * 1024 * 1024
            )[0]

        return descriptor, members, read_member

    def test_materialization_obeys_the_resolved_source_allowance_before_writing_the_next_member(self):
        source, members, read_member = self.bound_source()
        admitted = sum(member["bytes"] for member in members.values())
        destination = self.root / "admitted"
        materialize_source(destination, source, members, read_member, max_bytes=admitted)
        self.assertEqual(admitted, directory_bytes(destination))
        rejected = self.root / "rejected"
        with self.assertRaisesRegex(ValueError, "materialization budget"):
            materialize_source(rejected, source, members, read_member, max_bytes=admitted - 1)
        self.assertLess(directory_bytes(rejected), admitted)

    def test_materialization_uses_the_canonical_projection_key_as_the_member_path(self):
        source, members, read_member = self.bound_source()
        projected = {
            name: {key: value for key, value in member.items() if key != "path"} for name, member in members.items()
        }
        destination = self.root / "projection-copy"
        materialize_source(destination, source, projected, read_member)
        self.assertEqual(sum(member["bytes"] for member in members.values()), directory_bytes(destination))
        for name, expected in source["files"].items():
            self.assertEqual(expected, hashlib.sha256((destination / name).read_bytes()).hexdigest())

    def test_materialization_rejects_an_inaccurate_declared_member_size(self):
        source, members, read_member = self.bound_source()
        members["model.xml"] = {**members["model.xml"], "bytes": members["model.xml"]["bytes"] + 1}
        with self.assertRaisesRegex(ValueError, "declared member size"):
            materialize_source(self.root / "rejected", source, members, read_member)
        self.assertFalse((self.root / "rejected/model.xml").exists())

    def test_materialization_rejects_invalid_allowances_before_creating_a_workspace(self):
        for value in (True, 0, SESSION_QUOTA_BYTES + 1):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "admitted byte budget"):
                materialize_source(self.root / "rejected", {}, {}, None, max_bytes=value)
        self.assertFalse((self.root / "rejected").exists())
