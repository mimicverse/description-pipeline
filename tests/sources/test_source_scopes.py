"""Source scopes bind raw observation to exact frozen native bytes."""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from description_pipeline.geometry.native_scope import (
    native_scope_identity_sha256,
)
from description_pipeline.geometry.native_tessellation import (
    native_triangle_relative_path,
    write_native_triangle_stream,
)
from description_pipeline.io import (
    PipelineError,
    digest,
    read_data,
    write_json,
)
from description_pipeline.schemas import (
    NATIVE_CAPTURE_OBSERVATION_SCHEMA,
    NATIVE_DISCOVERY_SCHEMA,
    SOURCE_RECORD_SCHEMA,
)
from description_pipeline.verification.capture_evidence import source_scopes

from . import support


class SourceScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="source-scopes-raw-"
        )
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source_bytes = b"frozen native part bytes"
        self.source_sha256 = hashlib.sha256(self.source_bytes).hexdigest()
        self.scope = support.fixture_source_scope(
            "part-1",
            source_document_path=r"C:\source\Part.SLDPRT",
            source_copy_path="source/Part.SLDPRT",
            source_document_sha256=self.source_sha256,
        )
        frozen = self.root / "evidence/source/Part.SLDPRT"
        frozen.parent.mkdir(parents=True)
        frozen.write_bytes(self.source_bytes)
        self._write_stream()
        self._write_records()

    def _write_stream(self) -> None:
        stream = self.root / "evidence/raw/fixture.triangles"
        stream.parent.mkdir(parents=True, exist_ok=True)
        count = write_native_triangle_stream(
            stream,
            [
                0.0,
                0.0,
                0.0,
                1.0,
                0.0,
                0.0,
                0.0,
                1.0,
                0.0,
            ],
        )
        checksum = hashlib.sha256(stream.read_bytes()).hexdigest()
        relative = native_triangle_relative_path(checksum)
        target = self.root / "evidence" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        stream.replace(target)
        self.stream = {
            "stream_path": relative,
            "stream_sha256": checksum,
            "triangle_count": count,
        }

    def _write_records(self) -> None:
        source_path = self.scope["source_document_path"]
        copy_path = self.scope["source_copy_path"]
        transform = [
            1.0,
            0.0,
            0.0,
            0.0,
            1.0,
            0.0,
            0.0,
            0.0,
            1.0,
            0.0,
            0.0,
            0.0,
            1.0,
            0.0,
            0.0,
            0.0,
        ]
        observation = {
            "schema_version": NATIVE_CAPTURE_OBSERVATION_SCHEMA,
            "scene_raw": {
                "document": r"C:\source\Part.SLDASM",
                "configuration": "Default",
                "occurrences": [
                    {
                        "name": "part-1",
                        "path": source_path,
                        "transform_raw": transform,
                        "is_fixed": True,
                        "document_type": "part",
                    }
                ],
                "components": [
                    {
                        "name": "part-1",
                        "path": source_path,
                        "is_fixed": True,
                        "document_type": "part",
                    }
                ],
                "coordinate_systems": {},
            },
            "part_mass_raw": {},
            "assembly_mass_raw": None,
            "component_mass_raw": None,
            "link_group_mass_raw": {
                "assembly": r"C:\source\Part.SLDASM",
                "configuration": "Default",
                "used_api": "fixture",
                "selection_rule": "fixture",
                "links": {},
            },
            "source_scopes": {"part-1": self.scope},
            "geometry_raw": {
                "part-1": {
                    **self.stream,
                    "transform_raw": transform,
                    "body_inventory": [],
                    "face_inventory": [],
                    "body_counts": {"solid": 0, "sheet": 0},
                    "used_api": "fixture",
                    "source_scope_sha256": native_scope_identity_sha256(
                        self.scope
                    ),
                    "appearance_raw": None,
                }
            },
            "axis_references_raw": [],
            "document_state": {},
            "input_stability": {},
            "notes": {},
        }
        write_json(
            self.root / "evidence/raw/native_capture_facts.json",
            observation,
        )
        write_json(
            self.root / "input/discovery/native-discovery.json",
            {
                "schema_version": NATIVE_DISCOVERY_SCHEMA,
                "run_id": "source-scope-run",
                "derivation": {
                    "main_assembly": "Part.SLDASM",
                    "configuration": "Default",
                    "record_roots": [],
                },
                "identity": {
                    "main_assembly": "Part.SLDASM",
                    "delivery_configuration": "Default",
                    "hardware_id": "fixture",
                    "robot_name": "part",
                },
                "records": [],
                "raw": {
                    "components": [
                        {
                            "instance_id": "part-1",
                            "name2": "part-1",
                            "document": "Part.SLDPRT",
                            "configuration": "Default",
                            "suppressed": False,
                            "source_provenance": None,
                        }
                    ],
                    "files": {"Part.SLDPRT": self.source_sha256},
                },
                "native_files": {"Part.SLDPRT": self.source_sha256},
                "handoff_sha256": digest(
                    {"Part.SLDPRT": self.source_sha256}
                ),
            },
        )
        write_json(
            self.root / "evidence/raw/dependency_closure.json",
            {
                "original_files": {source_path: self.source_sha256},
                "copy_files": {copy_path: self.source_sha256},
                "mapping": [
                    {
                        "source": source_path,
                        "source_sha256": self.source_sha256,
                        "copy": copy_path,
                        "copy_sha256": self.source_sha256,
                    }
                ],
            },
        )
        identity = {
            "kind": "source-revision",
            "handoff_digest": "a" * 64,
            "main_assembly": "Part.SLDASM",
            "configuration": "Default",
            "hardware_id": "fixture",
            "records": [],
        }
        write_json(
            self.root / "input/source.json",
            {
                "schema_version": SOURCE_RECORD_SCHEMA,
                "source_revision_id": digest(identity),
                "identity": identity,
                "selection": {
                    "main_assembly": "Part.SLDASM",
                    "configuration": "Default",
                    "hardware_id": "fixture",
                    "hardware_source": "platform-config",
                },
                "metadata": {
                    "revision": None,
                    "owner": None,
                    "control": None,
                    "change_summary": None,
                    "change_summary_source": "generated",
                },
            },
        )

    def test_file_scope_returns_the_exact_sealed_mapping(self) -> None:
        self.assertEqual(source_scopes(self.root), {"part-1": self.scope})

    def test_changed_frozen_copy_is_refused(self) -> None:
        (self.root / "evidence/source/Part.SLDPRT").write_bytes(
            b"changed"
        )
        with self.assertRaisesRegex(PipelineError, "Frozen source bytes differ"):
            source_scopes(self.root)

    def test_missing_discovery_component_is_refused(self) -> None:
        path = self.root / "input/discovery/native-discovery.json"
        discovery = read_data(path)
        discovery["raw"]["components"] = []
        write_json(path, discovery)
        with self.assertRaisesRegex(
            PipelineError,
            "Source scopes do not cover",
        ):
            source_scopes(self.root)

    def test_invalid_scope_is_refused(self) -> None:
        path = self.root / "evidence/raw/native_capture_facts.json"
        observation = read_data(path)
        observation["source_scopes"]["part-1"]["component"] = "other"
        write_json(path, observation)
        with self.assertRaisesRegex(PipelineError, "scope differs"):
            source_scopes(self.root)


if __name__ == "__main__":
    unittest.main()
