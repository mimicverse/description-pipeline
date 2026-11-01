"""Real gzip and workspace boundaries for W4 consumer implementation only."""

from copy import deepcopy
import gzip
import hashlib
from pathlib import Path
import shutil
import tempfile
import unittest

from description_pipeline.io import canonical
from description_pipeline.simulation.evidence import (
    SessionEvidenceError,
    validate_finalization_proof,
    verify_manifest_counts,
    verify_finalization_observation,
)
from description_pipeline.simulation.resources import PHASES, SessionResourceError, directory_bytes
from description_pipeline.simulation.verify_recording import STREAM_NAMES, inspect_containers
from tests.v1.test_pico_projection_contract import policy_fixture


class EvidenceFixture:
    def __init__(self, root):
        self.root = root
        self.native = self.root / "recording/native"
        self.native.mkdir(parents=True)
        self.manifest = {"files": {}}
        for key, name in STREAM_NAMES.items():
            path = self.native / name
            with (
                path.open("wb") as file,
                gzip.GzipFile(fileobj=file, mode="wb", filename="", mtime=0, compresslevel=6) as stream,
            ):
                stream.write(b"not a business JSON row\n")
            self.identity(key)
        (self.native / "session-manifest.json").write_bytes(canonical(self.manifest))

    def identity(self, key):
        name = STREAM_NAMES[key]
        data = (self.native / name).read_bytes()
        self.manifest["files"][key] = {
            "name": name,
            "encoding": "gzip-jsonl",
            "compressed_bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }

    def finalization_fixture(self):
        self.manifest.update(
            {
                "resource_policy": policy_fixture(self.root),
                "finalization": {
                    "whole_session": True,
                    "quota_ok": True,
                    "observed_bytes": 0,
                    "quota_bytes": 536870912,
                    "finalization_reserve_bytes": 16777216,
                    "failure_reserve_bytes": 16777216,
                },
                "counts": {"raw_input": 1, "session_events": 1, "records": 1},
                "phase_timeline": {
                    name: {
                        "start_monotonic_ns": None,
                        "end_monotonic_ns": None,
                        "start_tick": None,
                        "end_tick": None,
                        "counts": {"raw_input": 0, "session_events": 0, "records": 0},
                    }
                    for name in PHASES
                },
            }
        )
        for index, name in enumerate(("preparing", "ready_unpaired", "stop_finalization", "finalization")):
            self.manifest["phase_timeline"][name].update(
                {
                    "start_monotonic_ns": index,
                    "start_tick": 0,
                    "end_monotonic_ns": index + 1 if name != "finalization" else None,
                    "end_tick": 0 if name != "finalization" else None,
                }
            )
        self.manifest["phase_timeline"]["stop_finalization"]["counts"] = dict(self.manifest["counts"])
        path = self.native / "session-manifest.json"
        while True:
            path.write_bytes(canonical(self.manifest) + b"\n")
            actual = directory_bytes(self.root)
            if actual == self.manifest["finalization"]["observed_bytes"]:
                break
            self.manifest["finalization"]["observed_bytes"] = actual

    def finalization_result_double(self):
        """Model only the frozen public result shape; this is not a native proof."""
        finalization = self.manifest["finalization"]
        return {
            "schema_version": "teleop-pico-session-finalization-verification/1",
            "pass": True,
            "session_dir": str(self.root),
            "observed_bytes": finalization["observed_bytes"],
            "manifest_observed_bytes": finalization["observed_bytes"],
            "reserve_bytes": finalization["finalization_reserve_bytes"] + finalization["failure_reserve_bytes"],
            "quota_bytes": finalization["quota_bytes"],
            "self_bytes_match": True,
            "quota_ok": True,
            "finalization": deepcopy(finalization),
            "detail": None,
        }


class PicoEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.fixture = EvidenceFixture(self.root)
        self.native, self.manifest = self.fixture.native, self.fixture.manifest

    def identity(self, key):
        self.fixture.identity(key)

    def finalization_fixture(self):
        self.fixture.finalization_fixture()

    def test_container_scan_does_not_claim_business_json_validity(self):
        result = inspect_containers(self.native, self.manifest)
        self.assertTrue(result["container_complete"])
        self.assertEqual(set(STREAM_NAMES), set(result["streams"]))
        self.assertTrue(all(value["decompressed_bytes"] > 0 for value in result["streams"].values()))

    def test_coherent_hashes_cannot_hide_a_missing_footer_or_bad_crc(self):
        path = self.native / STREAM_NAMES["records"]
        original = path.read_bytes()
        corrupted = bytearray(original)
        corrupted[-8] ^= 1
        for data in (original[:-4], bytes(corrupted)):
            with self.subTest(size=len(data)):
                path.write_bytes(data)
                self.identity("records")
                with self.assertRaisesRegex(ValueError, "gzip container is incomplete"):
                    inspect_containers(self.native, self.manifest)
                self.assertEqual(data, path.read_bytes())

    def test_empty_bytes_are_not_a_complete_empty_gzip_member(self):
        (self.native / STREAM_NAMES["raw"]).write_bytes(b"")
        self.identity("raw")
        with self.assertRaisesRegex(ValueError, "complete header"):
            inspect_containers(self.native, self.manifest)

    def test_rejects_compressed_identity_changes_and_extra_uncompressed_replicas(self):
        value = deepcopy(self.manifest)
        value["files"]["raw"]["sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "differs from its manifest"):
            inspect_containers(self.native, value)
        (self.native / "raw-input.jsonl").write_bytes(b"Unapproved duplicate\n")
        with self.assertRaisesRegex(ValueError, "unexpected evidence files"):
            inspect_containers(self.native, self.manifest)

    def test_finalization_counts_its_own_stable_manifest_and_rejects_a_late_outcome(self):
        self.finalization_fixture()
        proof = self.fixture.finalization_result_double()
        verified = verify_finalization_observation(self.root, self.manifest, proof)
        self.assertEqual(directory_bytes(self.root), verified["observed_bytes"])
        (self.root / "recording/outcome.json").write_bytes(b"{}\n")
        with self.assertRaisesRegex(SessionEvidenceError, "complete immutable session"):
            verify_finalization_observation(self.root, self.manifest, proof)

    def test_finalization_rejects_self_byte_and_boolean_claim_changes(self):
        self.finalization_fixture()
        for change in (
            {"observed_bytes": self.manifest["finalization"]["observed_bytes"] - 1},
            {"observed_bytes": True},
            {"whole_session": 1},
            {"quota_ok": False},
        ):
            value = deepcopy(self.manifest)
            value["finalization"].update(change)
            with self.subTest(change=change), self.assertRaises(SessionEvidenceError):
                verify_finalization_observation(self.root, value, self.fixture.finalization_result_double())

    def test_parent_requires_the_fixed_public_proof_and_independently_checks_its_bytes(self):
        self.finalization_fixture()
        proof = self.fixture.finalization_result_double()
        for change in (
            {"schema_version": "foreign"},
            {"pass": False, "detail": "Native finalization failed"},
            {"self_bytes_match": 1},
            {"observed_bytes": proof["observed_bytes"] + 1},
            {"manifest_observed_bytes": True},
            {"reserve_bytes": 0},
            {"quota_bytes": 536870913},
            {"session_dir": str(self.root / "foreign")},
            {"finalization": {}},
            {"unexpected": "field"},
        ):
            with self.subTest(change=change), self.assertRaises(SessionEvidenceError):
                verify_finalization_observation(self.root, self.manifest, {**proof, **change})
        with self.assertRaises(SessionEvidenceError):
            verify_finalization_observation(self.root, self.manifest, None)

    def test_sealed_proof_validation_never_reopens_the_historical_workspace(self):
        self.finalization_fixture()
        proof = self.fixture.finalization_result_double()
        shutil.rmtree(self.root)
        self.assertEqual(proof, validate_finalization_proof(self.root, self.manifest, proof))
        with self.assertRaises(SessionResourceError):
            verify_finalization_observation(self.root, self.manifest, proof)

    def test_phase_counts_cover_all_three_streams_without_inventing_a_running_phase(self):
        self.finalization_fixture()
        verify_manifest_counts(self.manifest, self.manifest["counts"])
        value = deepcopy(self.manifest)
        value["phase_timeline"]["running"]["counts"]["records"] = 1
        with self.assertRaisesRegex(SessionEvidenceError, "unentered native phase"):
            verify_manifest_counts(value, self.manifest["counts"])
        value = deepcopy(self.manifest)
        value["phase_timeline"]["stop_finalization"]["counts"]["records"] = 0
        with self.assertRaisesRegex(SessionEvidenceError, "do not cover"):
            verify_manifest_counts(value, self.manifest["counts"])
