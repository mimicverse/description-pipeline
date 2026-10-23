"""Finite-anchor tests for immutable native source scopes."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from . import _paths  # noqa: F401  (import side effect: sys.path)

from description_pipeline.io import PipelineError, digest  # noqa: E402
from description_pipeline.verification.native_discovery import (  # noqa: E402
    source_scopes,
)


class SourceScopeTests(unittest.TestCase):
    def setUp(self):
        self.workspace = tempfile.TemporaryDirectory()
        self.addCleanup(self.workspace.cleanup)
        self.root = Path(self.workspace.name)
        for relative in (
            "evidence/raw",
            "evidence/source",
            "input",
        ):
            (self.root / relative).mkdir(parents=True, exist_ok=True)
        identity = {
            "kind": "source-revision",
            "handoff_digest": "a" * 64,
            "main_assembly": "Part.SLDASM",
            "configuration": "Default",
            "hardware_id": "fixture",
            "records": [],
        }
        self.write(
            "input/source.json",
            {
                "schema_version": "solidworks-to-urdf.source-record/v1",
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
        self.write("evidence/capture.json", {"components": 1})

    def write(self, relative, value):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(value, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def file_scope(self):
        source_path = "C:/staged/source/Part.SLDPRT"
        source = self.root / "evidence/source/Part.SLDPRT"
        source.write_bytes(b"frozen-file")
        source_sha256 = hashlib.sha256(b"original-source").hexdigest()
        copy_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
        return {
            "rule_version": "native-open-edge-scope-binding-v1",
            "component": "part-1",
            "source_document_path": source_path,
            "source_copy_path": "source/Part.SLDPRT",
            "source_document_sha256": source_sha256,
            "source_copy_sha256": copy_sha256,
            "source_document_identity_sha256": source_sha256,
            "source_document_identity_kind": "file_sha256",
            "referenced_configuration": "Default",
            "occurrence_scope": "part-1",
            "virtual_owner": None,
            "virtual_owner_sha256": None,
            "virtual_session_document_path": None,
            "virtual_session_temp_root": None,
            "virtual_native_api_pid": None,
        }

    def write_file_anchors(self, scope):
        source_sha256 = scope["source_document_sha256"]
        copy_sha256 = scope["source_copy_sha256"]
        source_path = "C:/original/Part.SLDPRT"
        self.write(
            "evidence/discovery.json",
            {
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
                    "files": {
                        "Part.SLDPRT": source_sha256,
                    },
                },
                "files": [
                    {"path": "Part.SLDPRT", "sha256": source_sha256}
                ],
            },
        )
        self.write(
            "evidence/raw/scene_raw.json",
            {
                "components": [
                    {
                        "name": "part-1",
                        "path": scope["source_document_path"],
                    }
                ],
                "mass_properties": {
                    "part-1": {
                        "reference": {"configuration": "Default"}
                    }
                },
            },
        )
        self.write(
            "evidence/capture.json",
            {
                "components": 1,
                "scene": {
                    "components": [
                        {
                            "name": "part-1",
                            "configuration": "Default",
                        }
                    ]
                },
            },
        )
        self.write(
            "evidence/raw/geometry.json",
            [{"component": "part-1"}],
        )
        self.write(
            "evidence/raw/dependency_closure.json",
            {
                "session_temp_documents": [],
                "mapping": [
                    {
                        "instance": "part-1",
                        "copy": "source/Part.SLDPRT",
                        "copy_sha256": copy_sha256,
                        "source": source_path,
                        "source_sha256": copy_sha256,
                    }
                ],
                "original_files": {
                    source_path: source_sha256,
                },
                "copy_files": {
                    "source/Part.SLDPRT": copy_sha256,
                },
            },
        )
        self.write(
            "evidence/raw/source_scopes.json",
            {"part-1": scope},
        )

    def write_shared_file_anchors(self, scopes):
        first = scopes["part-1"]
        source_sha256 = first["source_document_sha256"]
        copy_sha256 = first["source_copy_sha256"]
        source_path = "C:/original/Part.SLDPRT"
        self.write(
            "evidence/discovery.json",
            {
                "raw": {
                    "components": [
                        {
                            "instance_id": name,
                            "name2": name,
                            "document": "Part.SLDPRT",
                            "configuration": "Default",
                            "suppressed": False,
                            "source_provenance": None,
                        }
                        for name in scopes
                    ],
                    "files": {
                        "Part.SLDPRT": source_sha256,
                    },
                },
                "files": [
                    {"path": "Part.SLDPRT", "sha256": source_sha256}
                ],
            },
        )
        self.write(
            "evidence/raw/scene_raw.json",
            {
                "components": [
                    {
                        "name": name,
                        "path": scope["source_document_path"],
                    }
                    for name, scope in scopes.items()
                ],
                "mass_properties": {
                    name: {
                        "reference": {"configuration": "Default"}
                    }
                    for name in scopes
                },
            },
        )
        self.write(
            "evidence/capture.json",
            {
                "components": len(scopes),
                "scene": {
                    "components": [
                        {
                            "name": name,
                            "configuration": "Default",
                        }
                        for name in scopes
                    ]
                },
            },
        )
        self.write(
            "evidence/raw/geometry.json",
            [{"component": name} for name in scopes],
        )
        self.write(
            "evidence/raw/dependency_closure.json",
            {
                "session_temp_documents": [],
                "mapping": [
                    {
                        "instance": "part-1",
                        "copy": "source/Part.SLDPRT",
                        "copy_sha256": copy_sha256,
                        "source": source_path,
                        "source_sha256": copy_sha256,
                    }
                ],
                "original_files": {
                    source_path: source_sha256,
                },
                "copy_files": {
                    "source/Part.SLDPRT": copy_sha256,
                },
            },
        )
        self.write("evidence/raw/source_scopes.json", scopes)

    def virtual_scope(self):
        root = r"c:\users\test\appdata\local\temp\swx123"
        path = root + r"\vc~~\owner\imported.sldprt"
        return {
            "rule_version": "native-open-edge-scope-binding-v1",
            "component": "part-1",
            "source_document_path": path,
            "source_copy_path": None,
            "source_document_sha256": None,
            "source_copy_sha256": None,
            "source_document_identity_sha256": hashlib.sha256(
                path.encode("utf-8")
            ).hexdigest(),
            "source_document_identity_kind": "virtual_path_sha256",
            "referenced_configuration": "Default",
            "occurrence_scope": "part-1",
            "virtual_owner": "Owner.SLDASM",
            "virtual_owner_sha256": "a" * 64,
            "virtual_session_document_path": path,
            "virtual_session_temp_root": root,
            "virtual_native_api_pid": 123,
        }

    def write_virtual_anchors(self, scope):
        provenance = {
            "is_virtual": True,
            "state": "embedded_virtual_component",
            "kind": "native_embedded_virtual_component",
            "owner": scope["virtual_owner"],
            "owner_sha256": scope["virtual_owner_sha256"],
            "occurrence": "part-1",
            "native_api_pid": 123,
            "session_document_path": scope[
                "virtual_session_document_path"
            ],
            "session_temp_root": scope["virtual_session_temp_root"],
        }
        self.write(
            "evidence/discovery.json",
            {
                "raw": {
                    "components": [
                        {
                            "instance_id": "part-1",
                            "name2": "part-1",
                            "document": scope["source_document_path"],
                            "configuration": "Default",
                            "suppressed": False,
                            "source_provenance": provenance,
                        }
                    ],
                    "files": {
                        "Owner.SLDASM": scope["virtual_owner_sha256"],
                    },
                },
                "files": [
                    {
                        "path": "Owner.SLDASM",
                        "sha256": "b" * 64,
                    }
                ],
            },
        )
        self.write(
            "evidence/raw/scene_raw.json",
            {
                "components": [
                    {
                        "name": "part-1",
                        "path": scope["source_document_path"],
                    }
                ],
                "mass_properties": {
                    "part-1": {
                        "reference": {"configuration": "Default"}
                    }
                },
            },
        )
        self.write(
            "evidence/capture.json",
            {
                "components": 1,
                "scene": {
                    "components": [
                        {
                            "name": "part-1",
                            "configuration": "Default",
                        }
                    ]
                },
            },
        )
        self.write(
            "evidence/raw/geometry.json",
            [{"component": "part-1"}],
        )
        self.write(
            "evidence/raw/dependency_closure.json",
            {
                "session_temp_documents": [
                    scope["virtual_session_document_path"]
                ]
            },
        )
        self.write(
            "evidence/raw/source_scopes.json",
            {"part-1": scope},
        )

    def test_file_scope_binds_frozen_bytes_and_component_inventory(self):
        scope = self.file_scope()
        self.write_file_anchors(scope)

        self.assertEqual(
            source_scopes(self.root),
            {"part-1": scope},
        )

    def test_file_scope_shared_occurrences_bind_one_physical_copy(self):
        first = self.file_scope()
        second = dict(
            first,
            component="part-2",
            occurrence_scope="part-2",
        )
        scopes = {"part-1": first, "part-2": second}
        self.write_shared_file_anchors(scopes)

        self.assertEqual(source_scopes(self.root), scopes)

    def test_file_scope_rejects_changed_frozen_bytes(self):
        scope = self.file_scope()
        self.write_file_anchors(scope)
        (self.root / "evidence/source/Part.SLDPRT").write_bytes(
            b"changed"
        )

        with self.assertRaises(PipelineError):
            source_scopes(self.root)

    def test_file_scope_rejects_dependency_mapping_hash_mismatch(self):
        scope = self.file_scope()
        self.write_file_anchors(scope)
        dependency = json.loads(
            (
                self.root / "evidence/raw/dependency_closure.json"
            ).read_text(encoding="utf-8")
        )
        dependency["mapping"][0]["copy_sha256"] = "c" * 64
        self.write(
            "evidence/raw/dependency_closure.json",
            dependency,
        )

        with self.assertRaises(PipelineError):
            source_scopes(self.root)

    def test_file_scope_rejects_source_and_copy_counterparts(self):
        mutations = (
            (
                "discovery-source-hash",
                "evidence/discovery.json",
                lambda value: value["files"][0].update(
                    {"sha256": "d" * 64}
                ),
            ),
            (
                "discovery-original-hash",
                "evidence/discovery.json",
                lambda value: value["raw"]["files"].update(
                    {"Part.SLDPRT": "d" * 64}
                ),
            ),
            (
                "original-files",
                "evidence/raw/dependency_closure.json",
                lambda value: value["original_files"].update(
                    {"C:/original/Part.SLDPRT": "d" * 64}
                ),
            ),
            (
                "mapping-copy",
                "evidence/raw/dependency_closure.json",
                lambda value: value["mapping"][0].update(
                    {"copy": "source/Foreign.SLDPRT"}
                ),
            ),
            (
                "mapping-source",
                "evidence/raw/dependency_closure.json",
                lambda value: value["mapping"][0].update(
                    {"source": "C:/foreign/Part.SLDPRT"}
                ),
            ),
            (
                "copy-files",
                "evidence/raw/dependency_closure.json",
                lambda value: value["copy_files"].update(
                    {"source/Part.SLDPRT": "d" * 64}
                ),
            ),
        )
        for label, relative, mutate in mutations:
            with self.subTest(label=label):
                scope = self.file_scope()
                self.write_file_anchors(scope)
                value = json.loads(
                    (self.root / relative).read_text(encoding="utf-8")
                )
                mutate(value)
                self.write(relative, value)
                with self.assertRaises(PipelineError):
                    source_scopes(self.root)

    def test_file_scope_rejects_same_basename_at_another_copy_path(self):
        scope = self.file_scope()
        self.write_file_anchors(scope)
        discovery = json.loads(
            (self.root / "evidence/discovery.json").read_text(
                encoding="utf-8"
            )
        )
        discovery["raw"]["components"][0]["document"] = (
            "nested/Part.SLDPRT"
        )
        discovery["raw"]["files"] = {
            "nested/Part.SLDPRT": scope["source_document_sha256"]
        }
        discovery["files"] = [
            {
                "path": "nested/Part.SLDPRT",
                "sha256": scope["source_copy_sha256"],
            }
        ]
        self.write("evidence/discovery.json", discovery)
        nested = dict(
            scope,
            source_copy_path="source/nested/Part.SLDPRT",
        )
        self.write(
            "evidence/raw/source_scopes.json",
            {"part-1": nested},
        )

        with self.assertRaises(PipelineError):
            source_scopes(self.root)

    def test_file_scope_rejects_duplicate_normalized_path_anchors(self):
        mutations = (
            (
                "discovery-copy",
                "evidence/discovery.json",
                lambda value: value["files"].append(
                    {
                        "path": "part.sldprt",
                        "sha256": value["files"][0]["sha256"],
                    }
                ),
            ),
            (
                "discovery-original",
                "evidence/discovery.json",
                lambda value: value["raw"]["files"].update(
                    {"part.sldprt": value["raw"]["files"]["Part.SLDPRT"]}
                ),
            ),
            (
                "dependency-original",
                "evidence/raw/dependency_closure.json",
                lambda value: value["original_files"].update(
                    {
                        "c:/ORIGINAL/part.sldprt": value[
                            "original_files"
                        ]["C:/original/Part.SLDPRT"]
                    }
                ),
            ),
            (
                "dependency-copy",
                "evidence/raw/dependency_closure.json",
                lambda value: value["copy_files"].update(
                    {
                        "source/part.sldprt": value["copy_files"][
                            "source/Part.SLDPRT"
                        ]
                    }
                ),
            ),
        )
        for label, relative, mutate in mutations:
            with self.subTest(label=label):
                scope = self.file_scope()
                self.write_file_anchors(scope)
                value = json.loads(
                    (self.root / relative).read_text(encoding="utf-8")
                )
                mutate(value)
                self.write(relative, value)
                with self.assertRaises(PipelineError):
                    source_scopes(self.root)

    def test_file_scope_rejects_configuration_mismatch(self):
        scope = self.file_scope()
        self.write_file_anchors(scope)
        discovery = json.loads(
            (self.root / "evidence/discovery.json").read_text(
                encoding="utf-8"
            )
        )
        discovery["raw"]["components"][0]["configuration"] = "Other"
        self.write("evidence/discovery.json", discovery)

        with self.assertRaises(PipelineError):
            source_scopes(self.root)

    def test_file_scope_rejects_scene_mass_configuration_mismatch(self):
        scope = self.file_scope()
        self.write_file_anchors(scope)
        scene = json.loads(
            (self.root / "evidence/raw/scene_raw.json").read_text(
                encoding="utf-8"
            )
        )
        scene["mass_properties"]["part-1"]["reference"][
            "configuration"
        ] = "WrongCaptureConfiguration"
        self.write("evidence/raw/scene_raw.json", scene)

        with self.assertRaises(PipelineError):
            source_scopes(self.root)

    def test_file_scope_rejects_foreign_capture_scene_component(self):
        scope = self.file_scope()
        self.write_file_anchors(scope)
        capture = json.loads(
            (self.root / "evidence/capture.json").read_text(
                encoding="utf-8"
            )
        )
        capture["scene"]["components"][0]["name"] = "foreign"
        self.write("evidence/capture.json", capture)

        with self.assertRaises(PipelineError):
            source_scopes(self.root)

    def test_capture_representations_are_checked_independently(self):
        scope = self.file_scope()
        self.write_file_anchors(scope)
        self.write(
            "evidence/capture.json",
            {
                "components": [
                    {
                        "name": "part-1",
                        "mass_properties": {"mass_kg": 1.0},
                    }
                ],
                "scene": {
                    "components": [
                        {
                            "name": "part-1",
                        }
                    ]
                },
            },
        )

        self.assertEqual(
            source_scopes(self.root),
            {"part-1": scope},
        )

    def test_file_scope_rejects_ambiguous_basename_fallback(self):
        scope = self.file_scope()
        self.write_file_anchors(scope)
        discovery = json.loads(
            (self.root / "evidence/discovery.json").read_text(
                encoding="utf-8"
            )
        )
        discovery["raw"]["components"][0]["document"] = (
            "Unknown/Part.SLDPRT"
        )
        discovery["files"].append(
            {
                "path": "Other/Part.SLDPRT",
                "sha256": "b" * 64,
            }
        )
        self.write("evidence/discovery.json", discovery)

        with self.assertRaises(PipelineError):
            source_scopes(self.root)

    def test_file_scope_rejects_same_basename_path_replacement(self):
        scope = self.file_scope()
        self.write_file_anchors(scope)
        scene = json.loads(
            (self.root / "evidence/raw/scene_raw.json").read_text(
                encoding="utf-8"
            )
        )
        scene["components"][0]["path"] = "C:/foreign/Part.SLDPRT"
        self.write("evidence/raw/scene_raw.json", scene)

        with self.assertRaises(PipelineError):
            source_scopes(self.root)

    def test_virtual_scope_binds_owner_path_root_and_configuration(self):
        scope = self.virtual_scope()
        self.write_virtual_anchors(scope)

        self.assertEqual(
            source_scopes(self.root),
            {"part-1": scope},
        )

    def test_virtual_scope_accepts_cross_session_relative_identity(self):
        scope = self.virtual_scope()
        self.write_virtual_anchors(scope)
        discovery = json.loads(
            (self.root / "evidence/discovery.json").read_text(
                encoding="utf-8"
            )
        )
        provenance = discovery["raw"]["components"][0][
            "source_provenance"
        ]
        other_root = r"c:\users\test\appdata\local\temp\swx456"
        other_path = other_root + r"\vc~~\owner\imported.sldprt"
        provenance["session_temp_root"] = other_root
        provenance["session_document_path"] = other_path
        provenance["native_api_pid"] = 456
        self.write("evidence/discovery.json", discovery)

        self.assertEqual(
            source_scopes(self.root),
            {"part-1": scope},
        )

    def test_virtual_scope_rejects_owner_mismatch(self):
        scope = self.virtual_scope()
        self.write_virtual_anchors(scope)
        discovery = json.loads(
            (self.root / "evidence/discovery.json").read_text(
                encoding="utf-8"
            )
        )
        discovery["raw"]["components"][0]["source_provenance"][
            "owner"
        ] = "Foreign.SLDASM"
        self.write("evidence/discovery.json", discovery)

        with self.assertRaises(PipelineError):
            source_scopes(self.root)

    def test_virtual_scope_rejects_occurrence_mismatch(self):
        scope = self.virtual_scope()
        self.write_virtual_anchors(scope)
        discovery = json.loads(
            (self.root / "evidence/discovery.json").read_text(
                encoding="utf-8"
            )
        )
        discovery["raw"]["components"][0]["source_provenance"][
            "occurrence"
        ] = "foreign"
        self.write("evidence/discovery.json", discovery)

        with self.assertRaises(PipelineError):
            source_scopes(self.root)

    def test_virtual_scope_rejects_foreign_parent_with_same_pid(self):
        scope = self.virtual_scope()
        scope["virtual_session_temp_root"] = r"c:\foreign\swx123"
        self.write_virtual_anchors(scope)

        with self.assertRaises(PipelineError):
            source_scopes(self.root)

    def test_virtual_scope_rejects_forged_owner_hash(self):
        scope = self.virtual_scope()
        self.write_virtual_anchors(scope)
        forged = dict(scope, virtual_owner_sha256="c" * 64)
        self.write(
            "evidence/raw/source_scopes.json",
            {"part-1": forged},
        )
        discovery = json.loads(
            (self.root / "evidence/discovery.json").read_text(
                encoding="utf-8"
            )
        )
        discovery["raw"]["components"][0]["source_provenance"][
            "owner_sha256"
        ] = "c" * 64
        self.write("evidence/discovery.json", discovery)

        with self.assertRaises(PipelineError):
            source_scopes(self.root)

    def test_virtual_scope_rejects_session_root_counterpart_mismatch(self):
        scope = self.virtual_scope()
        self.write_virtual_anchors(scope)
        dependency = json.loads(
            (
                self.root / "evidence/raw/dependency_closure.json"
            ).read_text(encoding="utf-8")
        )
        dependency["session_temp_documents"] = [
            r"c:\users\test\appdata\local\temp\swx999\vc~~\foreign\imported.sldprt"
        ]
        self.write(
            "evidence/raw/dependency_closure.json",
            dependency,
        )

        with self.assertRaises(PipelineError):
            source_scopes(self.root)

    def test_source_scope_inventory_rejects_extra_component(self):
        scope = self.file_scope()
        self.write_file_anchors(scope)
        scopes = {
            "part-1": scope,
            "extra": dict(scope, component="extra", occurrence_scope="extra"),
        }
        self.write("evidence/raw/source_scopes.json", scopes)

        with self.assertRaises(PipelineError):
            source_scopes(self.root)

    def test_source_scope_inventory_rejects_duplicate_geometry(self):
        scope = self.file_scope()
        self.write_file_anchors(scope)
        self.write(
            "evidence/raw/geometry.json",
            [{"component": "part-1"}, {"component": "part-1"}],
        )

        with self.assertRaises(PipelineError):
            source_scopes(self.root)

    def test_container_occurrence_is_excluded_from_exported_domain(self):
        scope = self.file_scope()
        self.write_file_anchors(scope)
        discovery = json.loads(
            (self.root / "evidence/discovery.json").read_text(
                encoding="utf-8"
            )
        )
        discovery["raw"]["components"].append(
            {
                "instance_id": "container-1",
                "name2": "container-1",
                "document": "Container.SLDASM",
                "configuration": "Default",
                "suppressed": False,
                "source_provenance": None,
            }
        )
        self.write("evidence/discovery.json", discovery)

        self.assertEqual(
            source_scopes(self.root),
            {"part-1": scope},
        )

    def test_unclassified_unsuppressed_component_is_rejected(self):
        scope = self.file_scope()
        self.write_file_anchors(scope)
        discovery = json.loads(
            (self.root / "evidence/discovery.json").read_text(
                encoding="utf-8"
            )
        )
        discovery["raw"]["components"].append(
            {
                "instance_id": "unexported-leaf",
                "name2": "unexported-leaf",
                "document": "Unexported.SLDPRT",
                "configuration": "Default",
                "suppressed": False,
                "source_provenance": None,
            }
        )
        self.write("evidence/discovery.json", discovery)

        with self.assertRaises(PipelineError):
            source_scopes(self.root)


if __name__ == "__main__":
    unittest.main()
