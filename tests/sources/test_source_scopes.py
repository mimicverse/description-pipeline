"""Source scopes bind raw observation to exact frozen native bytes."""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from description_pipeline.geometry.native_scope import (
    NATIVE_SOURCE_SCOPE_RULE,
    native_source_scope_is_valid,
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
from description_pipeline.sources.solidworks.evidence import (
    CACHED_MATERIALIZATION_WARNING_CODE,
    cached_materialization_warnings,
)
from description_pipeline.schemas import (
    NATIVE_CAPTURE_OBSERVATION_SCHEMA,
    NATIVE_DISCOVERY_SCHEMA,
    NATIVE_TRANSFORM_FRAME,
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
        frozen = self.root / "input/cad/Part.SLDPRT"
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
                "transform_frame": NATIVE_TRANSFORM_FRAME,
                "occurrences": [
                    {
                        "name": "part-1",
                        "path": source_path,
                        "transform_raw": transform,
                        "suppression_state": 2,
                        "is_fixed": True,
                        "document_type": "part",
                    }
                ],
                "components": [
                    {
                        "name": "part-1",
                        "path": source_path,
                        "suppression_state": 2,
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
            "document_state": {
                "before": {
                    "path": r"C:\source\Part.SLDASM",
                    "read_only": True,
                    "active_configuration": "Default",
                },
                "after": {
                    "path": r"C:\source\Part.SLDASM",
                    "read_only": True,
                    "active_configuration": "Default",
                },
            },
            "input_stability": {},
            "notes": {},
        }
        write_json(
            self.root / "evidence/raw/native_capture_facts.json.gz",
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

    def _use_virtual_source(self) -> str:
        source_path = (
            r"C:\Users\operator\AppData\Local\Temp"
            r"\swx21040\VC~~\owner\Copy of part.SLDPRT"
        )
        source_root = r"C:\Users\operator\AppData\Local\Temp\swx21040"
        owner = "owner.SLDPRT"
        owner_sha256 = self.source_sha256
        scope = {
            "rule_version": NATIVE_SOURCE_SCOPE_RULE,
            "component": "part-1",
            "source_document_path": source_path,
            "source_copy_path": None,
            "source_document_sha256": None,
            "source_copy_sha256": None,
            "source_document_identity_sha256": hashlib.sha256(
                source_path.encode("utf-8")
            ).hexdigest(),
            "source_document_identity_kind": "virtual_path_sha256",
            "referenced_configuration": "Default",
            "occurrence_scope": "part-1",
            "virtual_owner": owner,
            "virtual_owner_sha256": owner_sha256,
            "virtual_session_document_path": source_path,
            "virtual_session_temp_root": source_root,
            "virtual_native_api_pid": 21040,
        }
        facts_path = (
            self.root / "evidence/raw/native_capture_facts.json.gz"
        )
        observation = read_data(facts_path)
        observation["source_scopes"]["part-1"] = scope
        observation["geometry_raw"]["part-1"][
            "source_scope_sha256"
        ] = native_scope_identity_sha256(scope)
        for row in (
            observation["scene_raw"]["occurrences"][0],
            observation["scene_raw"]["components"][0],
        ):
            row["path"] = source_path
        write_json(facts_path, observation)

        discovery_path = (
            self.root / "input/discovery/native-discovery.json"
        )
        discovery = read_data(discovery_path)
        discovery["raw"]["components"][0].update(
            document="Copy of part.SLDPRT",
            source_provenance={
                "state": "embedded_virtual_component",
                "kind": "native_embedded_virtual_component",
                "owner": owner,
                "owner_sha256": owner_sha256,
                "occurrence": "part-1",
                "session_document_path": source_path,
                "session_temp_root": source_root,
                "native_api_pid": 21040,
                "is_virtual": True,
            },
        )
        discovery["raw"]["files"] = {owner: owner_sha256}
        discovery["native_files"] = {owner: owner_sha256}
        discovery["handoff_sha256"] = digest({owner: owner_sha256})
        write_json(discovery_path, discovery)
        return source_path

    def _use_cached_source(self) -> str:
        discovery_document_path = (
            r"C:\Users\operator\AppData\Local\Temp"
            r"\swx777\IC~~\part.step.SLDPRT"
        )
        discovery_root = r"C:\Users\operator\AppData\Local\Temp\swx777"
        source_path = (
            r"C:\Users\operator\AppData\Local\Temp"
            r"\swx778\IC~~\part.step.SLDPRT"
        )
        source_root = r"C:\Users\operator\AppData\Local\Temp\swx778"
        owner = "Part.SLDASM"
        identity = {
            "kind": "native_import_materialization",
            "state": "cached_materialization",
            "owner": owner,
            "owner_sha256": self.source_sha256,
            "occurrence": "part-1",
            "defining_anchor": None,
            "configuration": "Default",
            "is_virtual": False,
            "used_api": (
                "IComponent2.GetPathName/"
                "IComponent2.GetImportedPath/"
                "IComponent2.IsVirtual"
            ),
        }
        session = {
            **identity,
            "session_document_path": source_path,
            "session_temp_root": source_root,
            "native_api_pid": 778,
            "author_imported_path": None,
            "author_imported_error": None,
            "author_imported_relative": None,
            "author_imported_exists": None,
        }
        scope = {
            "rule_version": NATIVE_SOURCE_SCOPE_RULE,
            "component": "part-1",
            "source_document_path": source_path,
            "source_copy_path": None,
            "source_document_sha256": None,
            "source_copy_sha256": None,
            "source_document_identity_sha256": digest(identity),
            "source_document_identity_kind": "session_source_sha256",
            "referenced_configuration": "Default",
            "occurrence_scope": "part-1",
            "virtual_owner": None,
            "virtual_owner_sha256": None,
            "virtual_session_document_path": None,
            "virtual_session_temp_root": None,
            "virtual_native_api_pid": None,
            "session_source": session,
        }
        facts_path = self.root / "evidence/raw/native_capture_facts.json.gz"
        observation = read_data(facts_path)
        observation["source_scopes"]["part-1"] = scope
        observation["geometry_raw"]["part-1"][
            "source_scope_sha256"
        ] = native_scope_identity_sha256(scope)
        for row in (
            observation["scene_raw"]["occurrences"][0],
            observation["scene_raw"]["components"][0],
        ):
            row["path"] = source_path
        write_json(facts_path, observation)
        write_json(
            self.root / "evidence/native-runtime.json",
            {
                "solidworks": {
                    "sessions": {
                        "copy": {
                            "pid": 778,
                            "state": "active",
                        }
                    }
                }
            },
        )

        discovery_file = self.root / "input/discovery/native-discovery.json"
        discovery = read_data(discovery_file)
        discovery["raw"]["components"][0].update(
            document=discovery_document_path.replace("\\", "/"),
            configuration="Default",
            source_provenance={
                **identity,
                "session_document_path": discovery_document_path,
                "session_temp_root": discovery_root,
                "native_api_pid": 777,
                "author_imported_path": None,
                "author_imported_error": None,
                "author_imported_relative": None,
                "author_imported_exists": None,
            },
        )
        discovery["raw"]["files"] = {owner: self.source_sha256}
        discovery["native_files"] = {owner: self.source_sha256}
        discovery["handoff_sha256"] = digest({owner: self.source_sha256})
        write_json(discovery_file, discovery)
        return source_path

    def test_file_scope_returns_the_exact_sealed_mapping(self) -> None:
        self.assertEqual(source_scopes(self.root), {"part-1": self.scope})

    def test_changed_frozen_copy_is_refused(self) -> None:
        (self.root / "input/cad/Part.SLDPRT").write_bytes(
            b"changed"
        )
        with self.assertRaisesRegex(PipelineError, "Archived source bytes differ"):
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
        path = self.root / "evidence/raw/native_capture_facts.json.gz"
        observation = read_data(path)
        observation["source_scopes"]["part-1"]["component"] = "other"
        write_json(path, observation)
        with self.assertRaisesRegex(PipelineError, "scope differs"):
            source_scopes(self.root)

    def test_virtual_scope_keeps_its_session_identity_without_a_file_copy(
        self,
    ) -> None:
        source_path = self._use_virtual_source()
        scope = source_scopes(self.root)["part-1"]
        self.assertEqual(scope["source_document_identity_kind"], "virtual_path_sha256")
        self.assertIsNone(scope["source_copy_path"])
        self.assertIsNone(scope["source_document_sha256"])
        self.assertEqual(scope["source_document_path"], source_path)
        self.assertEqual(scope["virtual_session_document_path"], source_path)

    def test_virtual_scope_rejects_an_owner_file_as_the_session_document(
        self,
    ) -> None:
        self._use_virtual_source()
        discovery_path = (
            self.root / "input/discovery/native-discovery.json"
        )
        discovery = read_data(discovery_path)
        discovery["raw"]["components"][0]["source_provenance"][
            "session_document_path"
        ] = r"C:\Users\operator\AppData\Local\Temp\swx21040\owner.SLDPRT"
        write_json(discovery_path, discovery)
        with self.assertRaisesRegex(
            PipelineError,
            "Virtual source owner/path/root differs",
        ):
            source_scopes(self.root)

    def test_cached_session_scope_carries_the_current_owned_occurrence(self) -> None:
        session_path = (
            r"C:\Users\operator\AppData\Local\Temp"
            r"\swx777\IC~~\part.step.SLDPRT"
        )
        session_root = r"C:\Users\operator\AppData\Local\Temp\swx777"
        identity = {
            "kind": "native_import_materialization",
            "state": "cached_materialization",
            "owner": "Part.SLDASM",
            "owner_sha256": self.source_sha256,
            "occurrence": "part-1",
            "defining_anchor": None,
            "configuration": "Default",
            "is_virtual": False,
            "used_api": (
                "IComponent2.GetPathName/"
                "IComponent2.GetImportedPath/"
                "IComponent2.IsVirtual"
            ),
        }
        scope = {
            "rule_version": NATIVE_SOURCE_SCOPE_RULE,
            "component": "part-1",
            "source_document_path": session_path,
            "source_copy_path": None,
            "source_document_sha256": None,
            "source_copy_sha256": None,
            "source_document_identity_sha256": digest(identity),
            "source_document_identity_kind": "session_source_sha256",
            "referenced_configuration": "Default",
            "occurrence_scope": "part-1",
            "virtual_owner": None,
            "virtual_owner_sha256": None,
            "virtual_session_document_path": None,
            "virtual_session_temp_root": None,
            "virtual_native_api_pid": None,
            "session_source": {
                **identity,
                "session_document_path": session_path,
                "session_temp_root": session_root,
                "native_api_pid": 777,
                "author_imported_path": None,
                "author_imported_error": None,
                "author_imported_relative": None,
                "author_imported_exists": None,
            },
        }
        self.assertTrue(native_source_scope_is_valid(scope, "part-1"))
        scope["session_source"]["session_temp_root"] = (
            r"C:\Users\operator\AppData\Local\Temp\swx778"
        )
        self.assertFalse(native_source_scope_is_valid(scope, "part-1"))

    def test_cached_scope_flows_through_capture_evidence(self) -> None:
        source_path = self._use_cached_source()
        scope = source_scopes(self.root)["part-1"]
        self.assertEqual(scope["source_document_identity_kind"], "session_source_sha256")
        self.assertEqual(scope["source_document_path"], source_path)
        self.assertIsNone(scope["source_copy_path"])
        self.assertIsNone(scope["source_document_sha256"])
        self.assertEqual(scope["session_source"]["native_api_pid"], 778)

    def test_cached_source_cannot_downgrade_to_file_identity_to_remove_its_warning(self) -> None:
        self._use_cached_source()
        facts_path = self.root / "evidence/raw/native_capture_facts.json.gz"
        observation = read_data(facts_path)
        observation["source_scopes"]["part-1"] = dict(self.scope)
        observation["geometry_raw"]["part-1"]["source_scope_sha256"] = native_scope_identity_sha256(self.scope)
        write_json(facts_path, observation)
        with self.assertRaisesRegex(PipelineError, "Source identity kind differs"):
            source_scopes(self.root)

    def test_cached_scope_projects_a_stable_source_warning(self) -> None:
        self._use_cached_source()
        warnings = cached_materialization_warnings(source_scopes(self.root))
        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0]["code"], CACHED_MATERIALIZATION_WARNING_CODE)
        self.assertEqual(warnings[0]["objects"][0]["object"], "part-1")
        self.assertEqual(
            warnings[0]["objects"][0]["session_source_sha256"],
            source_scopes(self.root)["part-1"]["source_document_identity_sha256"],
        )

    def test_cached_source_preserves_each_sessions_author_error_in_warning(self) -> None:
        self._use_cached_source()
        facts_path = self.root / "evidence/raw/native_capture_facts.json.gz"
        discovery_path = self.root / "input/discovery/native-discovery.json"
        facts, discovery = read_data(facts_path), read_data(discovery_path)
        capture_error = "RuntimeError: capture author read failed"
        scope = facts["source_scopes"]["part-1"]
        scope["session_source"]["author_imported_error"] = capture_error
        facts["geometry_raw"]["part-1"]["source_scope_sha256"] = native_scope_identity_sha256(scope)
        discovery["raw"]["components"][0]["source_provenance"]["author_imported_error"] = (
            "RuntimeError: discovery author read failed"
        )
        write_json(facts_path, facts)
        write_json(discovery_path, discovery)
        warnings = cached_materialization_warnings(source_scopes(self.root))
        self.assertEqual(warnings[0]["objects"][0]["author_imported_error"], capture_error)
        scope["session_source"]["author_imported_exists"] = False
        facts["geometry_raw"]["part-1"]["source_scope_sha256"] = native_scope_identity_sha256(scope)
        write_json(facts_path, facts)
        with self.assertRaisesRegex(PipelineError, "Source scope"):
            source_scopes(self.root)

    def test_cached_scope_rejects_a_capture_runtime_pid_mismatch(self) -> None:
        self._use_cached_source()
        runtime_path = self.root / "evidence/native-runtime.json"
        runtime = read_data(runtime_path)
        runtime["solidworks"]["sessions"]["copy"]["pid"] = 779
        write_json(runtime_path, runtime)
        with self.assertRaisesRegex(PipelineError, "Cached source"):
            source_scopes(self.root)

    def test_cached_scope_distinguishes_missing_and_existing_external_author(self) -> None:
        self._use_cached_source()
        facts_path = self.root / "evidence/raw/native_capture_facts.json.gz"
        discovery_path = self.root / "input/discovery/native-discovery.json"
        facts = read_data(facts_path)
        discovery = read_data(discovery_path)
        external = r"D:\missing\imported_part.step"
        facts["source_scopes"]["part-1"]["session_source"].update(
            author_imported_path=external,
            author_imported_error=None,
            author_imported_relative=None,
            author_imported_exists=False,
        )
        facts["geometry_raw"]["part-1"]["source_scope_sha256"] = (
            native_scope_identity_sha256(facts["source_scopes"]["part-1"])
        )
        discovery["raw"]["components"][0]["source_provenance"].update(
            author_imported_path=external,
            author_imported_error=None,
            author_imported_relative=None,
            author_imported_exists=False,
        )
        write_json(facts_path, facts)
        write_json(discovery_path, discovery)
        self.assertEqual(source_scopes(self.root)["part-1"]["component"], "part-1")

        facts["source_scopes"]["part-1"]["session_source"][
            "author_imported_exists"
        ] = True
        facts["geometry_raw"]["part-1"]["source_scope_sha256"] = (
            native_scope_identity_sha256(facts["source_scopes"]["part-1"])
        )
        discovery["raw"]["components"][0]["source_provenance"][
            "author_imported_exists"
        ] = True
        write_json(facts_path, facts)
        write_json(discovery_path, discovery)
        with self.assertRaisesRegex(PipelineError, "existing external author"):
            source_scopes(self.root)


    def test_cached_identity_uses_canonical_utf8_for_unicode_native_names(self) -> None:
        from description_pipeline.sources.solidworks.session_artifacts import session_source_identity_sha256
        from description_pipeline.sources.solidworks.session_artifacts import SESSION_SOURCE_IDENTITY_FIELDS

        self._use_cached_source()
        observation = read_data(self.root / "evidence/raw/native_capture_facts.json.gz")
        scope = observation["source_scopes"]["part-1"]
        scope["component"] = scope["occurrence_scope"] = "module-1/\u90e8\u4ef6-1"
        scope["session_source"].update(
            owner="\u88c5\u914d.SLDASM", occurrence=scope["component"], configuration="\u9ed8\u8ba4"
        )
        scope["referenced_configuration"] = "\u9ed8\u8ba4"
        identity = {key: scope["session_source"][key] for key in SESSION_SOURCE_IDENTITY_FIELDS}
        scope["source_document_identity_sha256"] = session_source_identity_sha256(identity)
        self.assertEqual(scope["source_document_identity_sha256"], digest(identity))
        self.assertTrue(native_source_scope_is_valid(scope, scope["component"]))


if __name__ == "__main__":
    unittest.main()
