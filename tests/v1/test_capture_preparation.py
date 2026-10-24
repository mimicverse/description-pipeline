"""Independent rebuild-preparation checks against sealed discovery topology."""

from __future__ import annotations

import copy
import hashlib
import json
import ntpath
import posixpath
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from description_pipeline.io import PipelineError, canonical, digest, write_json
from description_pipeline.verification.solidworks_urdf import (
    _capture_preparation,
    _native_receipts_evidence,
)
from tests.v1.test_stage_transfer import (
    HANDOFF,
    MAIN_ASSEMBLY,
    RUN_ID,
    capture_root,
    native_tool,
    seal,
)

TOP = r"C:\capture\source\robot.SLDASM"
PART = r"C:\capture\source\arm.SLDPRT"
VIRTUAL = "/tmp/swx21040/VC~~/waist/virtual.SLDPRT"
CHILD_ASSEMBLY = r"C:\capture\source\arm.SLDASM"
SOURCE_TOP = r"C:\stage\cad\robot.SLDASM"
SOURCE_PART = r"C:\stage\cad\arm.SLDPRT"


def _normalized(path: str) -> str:
    module = ntpath if re.match(r"^[A-Za-z]:", path) or "\\" in path else posixpath
    return module.abspath(path).replace("/", "\\").strip().lower()


def _state(path: str, *, needs_rebuild: int = 0) -> dict:
    return {
        "save_flag": False,
        "needs_rebuild2_value": needs_rebuild,
        "read_only": True,
        "configuration": "Default",
        "configuration_needs_rebuild": False,
        "path": path,
        "configurations": [
            {
                "name": "Default",
                "needs_rebuild": False,
                "add_rebuild_save_mark": False,
            }
        ],
        "controls": {
            "enable_assembly_rebuild": False,
            "lightweight_component_count": 0,
        },
    }


def _topology_row(
    instance: str,
    path: str,
    *,
    depth: int,
    document_type: int,
    storage_scope: str,
    provenance_state=None,
    suppression_state=2,
) -> dict:
    session = storage_scope == "session"
    return {
        "instance": instance,
        "depth": depth,
        "document_path": path,
        "normalized_path": _normalized(path),
        "document_type": document_type,
        "referenced_configuration": "Default",
        "suppression_state": suppression_state,
        "storage_scope": storage_scope,
        "provenance_state": provenance_state,
        "owner_document_path": _normalized(TOP) if session else None,
        "owner_sha256": "a" * 64 if session else None,
        "native_api_pid": int(re.search(r"swx(\d+)", path).group(1)) if session else None,
    }


def _sort_topology(rows: list[dict]) -> list[dict]:
    return sorted(
        rows,
        key=lambda row: tuple(
            "" if row.get(name) is None else str(row.get(name))
            for name in (
                "instance",
                "normalized_path",
                "document_type",
                "referenced_configuration",
                "depth",
                "suppression_state",
                "storage_scope",
                "provenance_state",
            )
        ),
    )


def _document_state_snapshots(preparation: dict) -> dict[str, dict]:
    states = {}
    for item in preparation["assembly_states_after"]:
        states[_normalized(item["path"])] = (item["path"], 2, item["state"])
    for item in preparation["part_states_after"]:
        states[_normalized(item["path"])] = (item["path"], 1, item["state"])
    documents = []
    for row in preparation["topology_after"]:
        if row["storage_scope"] == "suppressed":
            continue
        document = states.pop(_normalized(row["document_path"]), None)
        if document is not None:
            documents.append(
                {
                    "path": document[0],
                    "document_type": document[1],
                    "state": copy.deepcopy(document[2]),
                }
            )
    if states:
        raise AssertionError(f"document state fixture has unbound rows: {sorted(states)}")
    documents = sorted(documents, key=lambda item: _normalized(item["path"]))
    snapshot = {
        "document_count": len(documents),
        "parts": sum(item["document_type"] == 1 for item in documents),
        "assemblies": sum(item["document_type"] == 2 for item in documents),
        "documents": documents,
        "topology": copy.deepcopy(preparation["topology_after"]),
    }
    return {
        "document_state_after_reads.json": copy.deepcopy(snapshot),
        "document_state_after_pristine_readout.json": copy.deepcopy(snapshot),
    }


def payload(*, virtual_reuse: bool = False) -> dict:
    state = _state(TOP)
    if virtual_reuse:
        session_row = _topology_row(
            "virtual-1",
            VIRTUAL,
            depth=1,
            document_type=1,
            storage_scope="session",
            provenance_state="embedded_virtual_component",
        )
        session_row_2 = {
            **session_row,
            "instance": "virtual-2",
        }
        topology = _sort_topology(
            [
                _topology_row(
                    "top",
                    TOP,
                    depth=0,
                    document_type=2,
                    storage_scope="file",
                    suppression_state=None,
                ),
                session_row,
                session_row_2,
            ]
        )
        source_files = {
            _normalized(TOP): {"path": TOP, "sha256": "a" * 64},
        }
        raw_components = [
            {
                "instance_id": "virtual-1",
                "document": VIRTUAL,
                "configuration": "Default",
                "suppressed": False,
                "suppression_state": 2,
            },
            {
                "instance_id": "virtual-2",
                "document": VIRTUAL,
                "configuration": "Default",
                "suppressed": False,
                "suppression_state": 2,
            },
        ]
        selection_closure = {
            "documents": {
                "cad/robot.SLDASM": {"resolved_edges": []},
            },
            "virtual_components": [
                {
                    "instance": "virtual-1",
                    "session_document_path": VIRTUAL,
                    "configuration": "Default",
                },
                {
                    "instance": "virtual-2",
                    "session_document_path": VIRTUAL,
                    "configuration": "Default",
                },
            ],
            "imports": [],
            "suppressed": [],
        }
        mapping = [
            {
                "instance": "top_level",
                "source": SOURCE_TOP,
                "copy": "source/robot.SLDASM",
                "source_sha256": "a" * 64,
                "copy_sha256": "a" * 64,
            }
        ]
        copy_files = {"source/robot.SLDASM": "a" * 64}
        session_paths = [VIRTUAL]
    else:
        topology = _sort_topology(
            [
                _topology_row(
                    "top",
                    TOP,
                    depth=0,
                    document_type=2,
                    storage_scope="file",
                    suppression_state=None,
                ),
                _topology_row(
                    "arm-1",
                    PART,
                    depth=1,
                    document_type=1,
                    storage_scope="file",
                ),
                _topology_row(
                    "arm-2",
                    PART,
                    depth=1,
                    document_type=1,
                    storage_scope="file",
                ),
            ]
        )
        source_files = {
            _normalized(TOP): {"path": TOP, "sha256": "a" * 64},
            _normalized(PART): {"path": PART, "sha256": "b" * 64},
        }
        raw_components = [
            {
                "instance_id": instance,
                "document": r"cad\arm.SLDPRT",
                "configuration": "Default",
                "suppressed": False,
                "suppression_state": 2,
            }
            for instance in ("arm-1", "arm-2")
        ]
        selection_closure = {
            "documents": {
                "cad/robot.SLDASM": {
                    "resolved_edges": [
                        {
                            "instance": "arm-1",
                            "resolved": r"cad\arm.SLDPRT",
                            "configuration": "Default",
                            "suppression_state": 2,
                        },
                        {
                            "instance": "arm-2",
                            "resolved": r"cad\arm.SLDPRT",
                            "configuration": "Default",
                            "suppression_state": 2,
                        },
                    ]
                },
                "cad/arm.SLDPRT": {"resolved_edges": []},
            },
            "virtual_components": [],
            "imports": [],
            "suppressed": [],
        }
        mapping = [
            {
                "instance": "top_level",
                "source": SOURCE_TOP,
                "copy": "source/robot.SLDASM",
                "source_sha256": "a" * 64,
                "copy_sha256": "a" * 64,
            },
            *[
                {
                    "instance": instance,
                    "source": SOURCE_PART,
                    "copy": "source/arm.SLDPRT",
                    "source_sha256": "b" * 64,
                    "copy_sha256": "b" * 64,
                }
                for instance in ("arm-1", "arm-2")
            ],
        ]
        copy_files = {
            "source/robot.SLDASM": "a" * 64,
            "source/arm.SLDPRT": "b" * 64,
        }
        session_paths = []
    preparation = {
        "schema_version": "solidworks-to-urdf.capture-preparation/v1",
        "used_api": "IModelDocExtension.NeedsRebuild2/IConfiguration.NeedsRebuild",
        "scope": "collected_copy_in_memory",
        "document": TOP,
        "configuration": "Default",
        "read_only": True,
        "saved_to_disk": False,
        "rebuild_performed": False,
        "component_count": len(topology) - 1,
        "assembly_document_count": 1,
        "child_rebuilds": [],
        "top_rebuild": None,
        "rebuild_calls": [
            {
                "kind": "initial",
                "initial_pending": [],
                "rebuild_candidates": [],
            }
        ],
        "needs_rebuild2_before": 0,
        "needs_rebuild2_after": 0,
        "configuration_needs_rebuild_before": False,
        "configuration_needs_rebuild_after": False,
        "source_sha256_before": "a" * 64,
        "source_sha256_after": "a" * 64,
        "save_flag_before": False,
        "save_flag_after": False,
        "state_before": state,
        "state_after_children": copy.deepcopy(state),
        "state_after": copy.deepcopy(state),
        "assembly_states_before": [
            {
                "depth": 0,
                "path": TOP,
                "instances": ["top"],
                "state": copy.deepcopy(state),
            }
        ],
        "assembly_states_after": [
            {
                "depth": 0,
                "path": TOP,
                "instances": ["top"],
                "state": copy.deepcopy(state),
            }
        ],
        "part_states_after": [],
        "source_files_before": copy.deepcopy(source_files),
        "source_files_after": copy.deepcopy(source_files),
        "session_dependency_paths": session_paths,
        "topology_before": copy.deepcopy(topology),
        "topology_after": copy.deepcopy(topology),
    }
    if virtual_reuse:
        preparation["part_states_after"] = [
            {
                "path": VIRTUAL,
                "instances": ["virtual-1", "virtual-2"],
                "state": {
                    key: value
                    for key, value in _state(VIRTUAL).items()
                    if key != "controls"
                },
            }
        ]
    elif any(item["document_type"] == 1 for item in topology):
        preparation["part_states_after"] = [
            {
                "path": PART,
                "instances": ["arm-1", "arm-2"],
                "state": {
                    key: value
                    for key, value in _state(PART).items()
                    if key != "controls"
                },
            }
        ]
    return {
        "scene": {
            "document": TOP,
            "source_root": ntpath.dirname(TOP),
            "notes": {"capture_preparation": preparation},
        },
        "closure": {
            "top_level": TOP,
            "configuration": "Default",
            "copy_files": copy_files,
            "mapping": mapping,
        },
        "discovery": {
            "raw": {
                "identity": {"main_assembly": "cad/robot.SLDASM"},
                "components": raw_components,
                "source_selection": {
                    "main_assembly": "cad/robot.SLDASM",
                    "closure": selection_closure,
                },
            }
        },
        "document_states": _document_state_snapshots(preparation),
        "manifest": {"identity": {"configuration": "Default"}},
    }


def dynamic_child_payload() -> dict:
    """One initially pending child is rebuilt inside the fixed ancestor closure."""

    value = payload()
    preparation = value["scene"]["notes"]["capture_preparation"]
    topology = [
        row
        for row in preparation["topology_before"]
        if row["instance"] != "arm-2"
    ]
    child = next(row for row in topology if row["instance"] == "arm-1")
    child.update(
        document_path=CHILD_ASSEMBLY,
        normalized_path=_normalized(CHILD_ASSEMBLY),
        document_type=2,
    )
    preparation["topology_before"] = _sort_topology(topology)
    preparation["topology_after"] = copy.deepcopy(preparation["topology_before"])
    preparation["component_count"] = 1
    preparation["assembly_document_count"] = 2
    preparation["used_api"] = (
        "IModelDocExtension.NeedsRebuild2/"
        "IConfiguration.NeedsRebuild/"
        "IModelDocExtension.Rebuild(swForceRebuildAll=2)"
    )
    preparation["rebuild_performed"] = True
    child_state = _state(CHILD_ASSEMBLY)
    child_before = _state(CHILD_ASSEMBLY, needs_rebuild=1)
    child_key = _normalized(CHILD_ASSEMBLY)
    preparation["assembly_states_before"].append(
        {
            "depth": 1,
            "path": CHILD_ASSEMBLY,
            "instances": ["arm-1"],
            "state": copy.deepcopy(child_before),
        }
    )
    preparation["assembly_states_after"].append(
        {
            "depth": 1,
            "path": CHILD_ASSEMBLY,
            "instances": ["arm-1"],
            "state": copy.deepcopy(child_state),
        }
    )
    preparation["part_states_after"] = []
    fresh_before = copy.deepcopy(child_before)
    preparation["child_rebuilds"] = [
        {
            "path": CHILD_ASSEMBLY,
            "depth": 1,
            "instances": ["arm-1"],
            "elapsed_seconds": 0.1,
            "result": True,
            "call_sequence": 1,
            "state_before": fresh_before,
            "state_after": copy.deepcopy(child_state),
        }
    ]
    preparation["rebuild_calls"] = [
        {
            "kind": "initial",
            "initial_pending": [child_key],
            "rebuild_candidates": [child_key],
        },
        {
            "sequence": 1,
            "kind": "child",
            "path": CHILD_ASSEMBLY,
            "depth": 1,
            "state_before": copy.deepcopy(fresh_before),
            "result": True,
            "state_after": copy.deepcopy(child_state),
        },
    ]
    source_files = preparation["source_files_before"]
    source_files.pop(_normalized(PART))
    source_files[child_key] = {"path": CHILD_ASSEMBLY, "sha256": "b" * 64}
    preparation["source_files_after"] = copy.deepcopy(source_files)
    value["document_states"] = _document_state_snapshots(preparation)
    raw = value["discovery"]["raw"]
    raw["components"] = [
        {
            "instance_id": "arm-1",
            "document": r"cad\arm.SLDASM",
            "configuration": "Default",
            "suppressed": False,
            "suppression_state": 2,
        }
    ]
    closure = raw["source_selection"]["closure"]
    closure["documents"] = {
        "cad/robot.SLDASM": {
            "resolved_edges": [
                {
                    "instance": "arm-1",
                    "resolved": r"cad\arm.SLDASM",
                    "configuration": "Default",
                    "suppression_state": 2,
                }
            ]
        },
        "cad/arm.SLDASM": {"resolved_edges": []},
    }
    value["closure"]["copy_files"] = {
        "source/robot.SLDASM": "a" * 64,
        "source/arm.SLDASM": "b" * 64,
    }
    value["closure"]["mapping"] = [
        {
            "instance": "top_level",
            "source": SOURCE_TOP,
            "copy": "source/robot.SLDASM",
            "source_sha256": "a" * 64,
            "copy_sha256": "a" * 64,
        },
        {
            "instance": "arm-1",
            "source": r"C:\stage\cad\arm.SLDASM",
            "copy": "source/arm.SLDASM",
            "source_sha256": "b" * 64,
            "copy_sha256": "b" * 64,
        },
    ]
    return value


class CapturePreparationTests(unittest.TestCase):
    def validate(self, value: dict) -> dict:
        def raw_document(path):
            name = Path(path).name
            if name not in value["document_states"]:
                raise PipelineError(f"Missing raw document snapshot: {name}")
            return copy.deepcopy(value["document_states"][name])

        with (
            patch(
                "description_pipeline.verification.solidworks_urdf.evidence.scene",
                return_value=value["scene"],
            ),
            patch(
                "description_pipeline.verification.solidworks_urdf.evidence.dependency_closure",
                return_value=value["closure"],
            ),
            patch(
                "description_pipeline.verification.solidworks_urdf.evidence.discovery",
                return_value=value["discovery"],
            ),
            patch(
                "description_pipeline.verification.solidworks_urdf.confined",
                side_effect=lambda root, relative, **_kwargs: Path(root) / relative,
            ),
            patch(
                "description_pipeline.verification.solidworks_urdf.read_data",
                side_effect=raw_document,
            ),
        ):
            return _capture_preparation(Path("/delivery"), value["manifest"])

    def preparation(self, value: dict) -> dict:
        return value["scene"]["notes"]["capture_preparation"]

    def assert_rejected(self, mutate, pattern: str) -> None:
        value = payload()
        mutate(value)
        with self.assertRaisesRegex(PipelineError, pattern):
            self.validate(value)

    def test_repeated_file_document_instances_are_accepted(self) -> None:
        result = self.validate(payload())
        self.assertEqual(result["component_count"], 2)
        self.assertEqual(result["assembly_document_count"], 1)

    def test_repeated_session_document_instances_share_one_dependency(self) -> None:
        result = self.validate(payload(virtual_reuse=True))
        self.assertEqual(result["component_count"], 2)
        self.assertEqual(result["session_document_count"], 2)

    def test_final_document_snapshots_bind_every_loaded_object_and_terminal_state(self) -> None:
        value = payload()
        self.validate(value)

        pending = copy.deepcopy(value)
        next(
            item
            for item in pending["document_states"]["document_state_after_reads.json"]["documents"]
            if item["document_type"] == 1
        )["state"]["needs_rebuild2_value"] = 1
        with self.assertRaisesRegex(PipelineError, "left a loaded document pending"):
            self.validate(pending)

        missing = copy.deepcopy(value)
        missing["document_states"]["document_state_after_reads.json"]["documents"].pop()
        with self.assertRaisesRegex(PipelineError, "document count|loaded-document set"):
            self.validate(missing)

        foreign = copy.deepcopy(value)
        foreign["document_states"]["document_state_after_reads.json"]["documents"][0][
            "path"
        ] = r"C:\other\foreign.SLDPRT"
        with self.assertRaisesRegex(
            PipelineError,
            "state names another document|unbound document|loaded-document set",
        ):
            self.validate(foreign)

        changed = copy.deepcopy(value)
        changed["document_states"]["document_state_after_pristine_readout.json"]["documents"][0][
            "state"
        ]["configuration"] = "Other"
        with self.assertRaisesRegex(PipelineError, "configuration|changed across"):
            self.validate(changed)

        absent = copy.deepcopy(value)
        absent["document_states"].pop("document_state_after_pristine_readout.json")
        with self.assertRaisesRegex(PipelineError, "Missing raw document snapshot"):
            self.validate(absent)

    def test_pristine_continuity_ignores_diagnostics_and_configuration_order(self) -> None:
        value = payload()
        preparation = self.preparation(value)
        for row in [
            *preparation["assembly_states_before"],
            *preparation["assembly_states_after"],
            *preparation["part_states_after"],
        ]:
            row["state"]["configurations"].append(
                {
                    "name": "Unused",
                    "needs_rebuild": False,
                    "add_rebuild_save_mark": False,
                }
            )
        for state in (
            preparation["state_before"],
            preparation["state_after_children"],
            preparation["state_after"],
        ):
            state["configurations"].append(
                {
                    "name": "Unused",
                    "needs_rebuild": False,
                    "add_rebuild_save_mark": False,
                }
            )
        value["document_states"] = _document_state_snapshots(preparation)
        pristine = value["document_states"]["document_state_after_pristine_readout.json"]
        for document in pristine["documents"]:
            document["state"]["save_flag"] = True
            for configuration in document["state"]["configurations"]:
                configuration["add_rebuild_save_mark"] = True
            document["state"]["configurations"].reverse()
        self.validate(value)

    def test_part_occurrences_may_use_multiple_reference_configurations(self) -> None:
        value = payload()
        preparation = self.preparation(value)
        for row in preparation["topology_before"] + preparation["topology_after"]:
            if row["instance"] == "arm-1":
                row["referenced_configuration"] = "Short"
            elif row["instance"] == "arm-2":
                row["referenced_configuration"] = "Long"
        part_state = preparation["part_states_after"][0]["state"]
        part_state["configurations"].extend(
            [
                {
                    "name": "Short",
                    "needs_rebuild": False,
                    "add_rebuild_save_mark": False,
                },
                {
                    "name": "Long",
                    "needs_rebuild": False,
                    "add_rebuild_save_mark": False,
                },
            ]
        )
        for component in value["discovery"]["raw"]["components"]:
            component["configuration"] = "Short" if component["instance_id"] == "arm-1" else "Long"
        for edge in value["discovery"]["raw"]["source_selection"]["closure"]["documents"][
            "cad/robot.SLDASM"
        ]["resolved_edges"]:
            edge["configuration"] = "Short" if edge["instance"] == "arm-1" else "Long"
        value["document_states"] = _document_state_snapshots(preparation)
        self.validate(value)

    def test_session_path_may_change_only_with_current_native_owner(self) -> None:
        value = payload(virtual_reuse=True)
        pristine = value["document_states"]["document_state_after_pristine_readout.json"]
        changed = "/tmp/swx22000/VC~~/waist/virtual.SLDPRT"
        for document in pristine["documents"]:
            if document["path"] == VIRTUAL:
                document["path"] = changed
                document["state"]["path"] = changed
        for row in pristine["topology"]:
            if row["storage_scope"] == "session":
                row["document_path"] = changed
                row["normalized_path"] = _normalized(changed)
                row["native_api_pid"] = 22000
        self.validate(value)

        foreign_owner = copy.deepcopy(value)
        pristine = foreign_owner["document_states"]["document_state_after_pristine_readout.json"]
        for row in pristine["topology"]:
            if row["storage_scope"] == "session":
                row["owner_sha256"] = "f" * 64
        with self.assertRaisesRegex(PipelineError, "semantic native topology"):
            self.validate(foreign_owner)

    def test_cached_materialization_reopen_uses_ic_owner_evidence(self) -> None:
        value = payload(virtual_reuse=True)
        preparation = self.preparation(value)
        materialized = "/tmp/swx21040/IC~~/waist/imported.SLDPRT"
        for row in preparation["topology_before"] + preparation["topology_after"]:
            if row["storage_scope"] == "session":
                row["document_path"] = materialized
                row["normalized_path"] = _normalized(materialized)
                row["provenance_state"] = "cached_materialization"
        preparation["part_states_after"][0]["path"] = materialized
        part_state = preparation["part_states_after"][0]["state"]
        part_state["path"] = materialized
        for component in value["discovery"]["raw"]["components"]:
            component["document"] = materialized
        closure = value["discovery"]["raw"]["source_selection"]["closure"]
        closure["virtual_components"] = []
        closure["imports"] = [
            {
                "instance": instance,
                "materialization": materialized,
                "configuration": "Default",
            }
            for instance in ("virtual-1", "virtual-2")
        ]
        preparation["session_dependency_paths"] = [materialized]
        value["closure"]["copy_files"] = {"source/robot.SLDASM": "a" * 64}
        value["document_states"] = _document_state_snapshots(preparation)

        pristine = value["document_states"]["document_state_after_pristine_readout.json"]
        reopened = "/tmp/swx22000/IC~~/waist/imported.SLDPRT"
        for document in pristine["documents"]:
            if document["path"] == materialized:
                document["path"] = reopened
                document["state"]["path"] = reopened
        for row in pristine["topology"]:
            if row["storage_scope"] == "session":
                row["document_path"] = reopened
                row["normalized_path"] = _normalized(reopened)
                row["native_api_pid"] = 22000
        self.validate(value)

    def test_actual_rebuild_producer_feeds_the_independent_final_state_consumer(self) -> None:
        from description_pipeline.sources.solidworks import native
        from tests.sources.test_solidworks_rebuild_lifecycle import _Backend, _Component, _Doc

        top_path = r"C:\capture\prepared\robot.SLDASM"
        part_path = r"C:\capture\prepared\part.SLDPRT"
        part = _Doc(part_path, document_type=1)
        top = _Doc(
            top_path,
            configuration="默认",
            children=[_Component("part-1", part)],
        )
        backend = _Backend({top_path: top, part_path: part})
        with patch.object(native, "_hash", return_value="a" * 64):
            _, preparation = backend._rebuild_capture_copy(top, top_path)
            documents = backend.verify_capture_documents_ready()
        for name in ("topology_before", "topology_after"):
            for row in preparation[name]:
                if row.get("document_path"):
                    row["normalized_path"] = _normalized(row["document_path"])
        for row in documents["topology"]:
            if row.get("document_path"):
                row["normalized_path"] = _normalized(row["document_path"])
        for name in ("source_files_before", "source_files_after"):
            preparation[name] = {
                _normalized(row["path"]): row
                for row in preparation[name].values()
            }
        source_files = copy.deepcopy(preparation["source_files_after"])
        source_hashes = {
            _normalized(row["path"]): row["sha256"]
            for row in source_files.values()
        }
        topology = copy.deepcopy(preparation["topology_after"])
        value = {
            "scene": {
                "document": top_path,
                "source_root": ntpath.dirname(top_path),
                "notes": {"capture_preparation": preparation},
            },
            "closure": {
                "top_level": top_path,
                "configuration": "默认",
                "copy_files": {
                    "source/robot.SLDASM": source_hashes[_normalized(top_path)],
                    "source/part.SLDPRT": source_hashes[_normalized(part_path)],
                },
                "mapping": [
                    {
                        "instance": "top_level",
                        "source": r"C:\stage\cad\robot.SLDASM",
                        "copy": "source/robot.SLDASM",
                        "source_sha256": source_hashes[_normalized(top_path)],
                        "copy_sha256": source_hashes[_normalized(top_path)],
                    },
                    {
                        "instance": "part-1",
                        "source": r"C:\stage\cad\part.SLDPRT",
                        "copy": "source/part.SLDPRT",
                        "source_sha256": source_hashes[_normalized(part_path)],
                        "copy_sha256": source_hashes[_normalized(part_path)],
                    },
                ],
            },
            "discovery": {
                "raw": {
                    "identity": {"main_assembly": "cad/robot.SLDASM"},
                    "components": [
                        {
                            "instance_id": "part-1",
                            "document": r"cad\part.SLDPRT",
                            "configuration": "默认",
                            "suppressed": False,
                            "suppression_state": 2,
                        }
                    ],
                    "source_selection": {
                        "main_assembly": "cad/robot.SLDASM",
                        "closure": {
                            "documents": {
                                "cad/robot.SLDASM": {
                                    "resolved_edges": [
                                        {
                                            "instance": "part-1",
                                            "resolved": r"cad\part.SLDPRT",
                                            "configuration": "默认",
                                            "suppression_state": 2,
                                        }
                                    ]
                                },
                                "cad/part.SLDPRT": {"resolved_edges": []},
                            },
                            "virtual_components": [],
                            "imports": [],
                            "suppressed": [],
                        },
                    },
                }
            },
            "document_states": {
                "document_state_after_reads.json": documents,
                "document_state_after_pristine_readout.json": copy.deepcopy(documents),
            },
            "manifest": {"identity": {"configuration": "默认"}},
        }
        self.assertEqual({row["instance"] for row in topology}, {"top", "part-1"})
        result = self.validate(value)
        self.assertEqual(result["final_document_count"], 2)

    def test_authored_suppressed_occurrence_is_preserved_and_bounded(self) -> None:
        def suppressed_row() -> dict:
            return {
                "instance": "ghost-1",
                "depth": 1,
                "document_path": None,
                "normalized_path": None,
                "document_type": None,
                "referenced_configuration": None,
                "suppression_state": 0,
                "storage_scope": "suppressed",
                "provenance_state": None,
                "owner_document_path": None,
                "owner_sha256": None,
                "native_api_pid": None,
            }

        value = payload()
        preparation = self.preparation(value)
        preparation["topology_before"].append(suppressed_row())
        preparation["topology_after"].append(copy.deepcopy(suppressed_row()))
        preparation["topology_before"] = _sort_topology(preparation["topology_before"])
        preparation["topology_after"] = _sort_topology(preparation["topology_after"])
        preparation["component_count"] += 1
        value["document_states"] = _document_state_snapshots(preparation)
        value["discovery"]["raw"]["source_selection"]["closure"]["suppressed"] = [
            {
                "document": "cad/robot.SLDASM",
                "instance": "ghost-1",
                "resolved_path": r"C:\capture\source\ghost.SLDPRT",
                "suppression_state": 0,
            }
        ]
        self.validate(value)

        missing = copy.deepcopy(value)
        missing["scene"]["notes"]["capture_preparation"]["topology_before"] = [
            row
            for row in missing["scene"]["notes"]["capture_preparation"]["topology_before"]
            if row["instance"] != "ghost-1"
        ]
        missing["scene"]["notes"]["capture_preparation"]["topology_after"] = copy.deepcopy(
            missing["scene"]["notes"]["capture_preparation"]["topology_before"]
        )
        missing["scene"]["notes"]["capture_preparation"]["component_count"] -= 1
        missing["document_states"] = _document_state_snapshots(
            missing["scene"]["notes"]["capture_preparation"]
        )
        with self.assertRaisesRegex(PipelineError, "sealed native occurrence graph"):
            self.validate(missing)

        forged = payload()
        forged_preparation = self.preparation(forged)
        forged_preparation["topology_before"].append(suppressed_row())
        forged_preparation["topology_after"].append(copy.deepcopy(suppressed_row()))
        forged_preparation["topology_before"] = _sort_topology(forged_preparation["topology_before"])
        forged_preparation["topology_after"] = _sort_topology(forged_preparation["topology_after"])
        forged_preparation["component_count"] += 1
        forged["document_states"] = _document_state_snapshots(forged_preparation)
        with self.assertRaisesRegex(PipelineError, "sealed native occurrence graph"):
            self.validate(forged)

    def test_dynamic_child_fresh_state_is_not_forced_to_equal_initial_state(self) -> None:
        value = dynamic_child_payload()
        result = self.validate(value)
        self.assertEqual(result["rebuilt_document_count"], 1)

        wrong_path = copy.deepcopy(value)
        wrong_path["scene"]["notes"]["capture_preparation"]["child_rebuilds"][0][
            "state_before"
        ]["path"] = PART
        with self.assertRaisesRegex(PipelineError, "state_before names another document"):
            self.validate(wrong_path)

        wrong_configuration = copy.deepcopy(value)
        wrong_configuration["scene"]["notes"]["capture_preparation"]["child_rebuilds"][0][
            "state_before"
        ]["configuration"] = "Other"
        wrong_configuration["scene"]["notes"]["capture_preparation"]["child_rebuilds"][0][
            "state_before"
        ]["configurations"][0]["name"] = "Other"
        with self.assertRaisesRegex(PipelineError, "state_before changed the assembly configuration"):
            self.validate(wrong_configuration)

        duplicate = copy.deepcopy(value)
        duplicate["scene"]["notes"]["capture_preparation"]["child_rebuilds"].append(
            copy.deepcopy(duplicate["scene"]["notes"]["capture_preparation"]["child_rebuilds"][0])
        )
        with self.assertRaisesRegex(PipelineError, "rebuilt more than once"):
            self.validate(duplicate)

    def test_fresh_pending_sibling_outside_initial_closure_is_rejected(self) -> None:
        value = dynamic_child_payload()
        preparation = self.preparation(value)
        preparation["assembly_states_before"][-1]["state"]["needs_rebuild2_value"] = 0
        preparation["rebuild_calls"][0]["initial_pending"] = []
        preparation["rebuild_calls"][0]["rebuild_candidates"] = []
        with self.assertRaisesRegex(PipelineError, "outside the initial dirty ancestor closure"):
            self.validate(value)

    def test_file_and_session_occurrence_paths_are_not_rebound_by_basename(self) -> None:
        rebound_file = payload()
        rebound_path = r"C:\other\arm.SLDPRT"
        preparation = self.preparation(rebound_file)
        topology_row = next(
            row
            for row in preparation["topology_before"]
            if row["instance"] == "arm-1"
        )
        preparation["topology_before"] = [
            row
            for row in preparation["topology_before"]
            if row["instance"] != "arm-2"
        ]
        preparation["component_count"] -= 1
        rebound_file["discovery"]["raw"]["components"] = [
            row
            for row in rebound_file["discovery"]["raw"]["components"]
            if row["instance_id"] != "arm-2"
        ]
        closure = rebound_file["discovery"]["raw"]["source_selection"]["closure"]
        closure["documents"]["cad/robot.SLDASM"]["resolved_edges"] = [
            edge
            for edge in closure["documents"]["cad/robot.SLDASM"]["resolved_edges"]
            if edge["instance"] != "arm-2"
        ]
        topology_row["document_path"] = rebound_path
        topology_row["normalized_path"] = _normalized(rebound_path)
        preparation["topology_after"] = copy.deepcopy(preparation["topology_before"])
        source_files = preparation["source_files_before"]
        source_files.pop(_normalized(PART))
        source_files[_normalized(rebound_path)] = {
            "path": rebound_path,
            "sha256": "b" * 64,
        }
        preparation["source_files_after"] = copy.deepcopy(source_files)
        with self.assertRaisesRegex(PipelineError, "escapes its capture root"):
            self.validate(rebound_file)

        rebound_session = payload(virtual_reuse=True)
        changed_session = "/tmp/swx21040/VC~~/other/virtual.SLDPRT"
        preparation = self.preparation(rebound_session)
        for row in preparation["topology_before"]:
            if row["storage_scope"] == "session":
                row["document_path"] = changed_session
                row["normalized_path"] = _normalized(changed_session)
        preparation["topology_after"] = copy.deepcopy(preparation["topology_before"])
        preparation["session_dependency_paths"] = [changed_session]
        with self.assertRaisesRegex(PipelineError, "Session path identity changed"):
            self.validate(rebound_session)

    def test_session_dependency_inventory_is_a_unique_sorted_list(self) -> None:
        duplicated = payload(virtual_reuse=True)
        duplicated["scene"]["notes"]["capture_preparation"]["session_dependency_paths"] = [
            VIRTUAL,
            VIRTUAL,
        ]
        with self.assertRaisesRegex(PipelineError, "unique sorted list"):
            self.validate(duplicated)

        mapping = payload(virtual_reuse=True)
        mapping["scene"]["notes"]["capture_preparation"]["session_dependency_paths"] = {
            VIRTUAL: True
        }
        with self.assertRaisesRegex(PipelineError, "unique sorted list"):
            self.validate(mapping)

    def test_schema_state_and_source_mutations_are_rejected(self) -> None:
        cases = (
            (
                "unknown_schema",
                lambda value: self.preparation(value).update(schema_version="unknown"),
                "schema changed",
            ),
            (
                "pending_after",
                lambda value: (
                    self.preparation(value)["state_after"].update(needs_rebuild2_value=1),
                    self.preparation(value)["assembly_states_after"][0]["state"].update(needs_rebuild2_value=1),
                ),
                "not fully rebuilt",
            ),
            (
                "read_only",
                lambda value: (
                    self.preparation(value)["state_after"].update(read_only=False),
                    self.preparation(value)["assembly_states_after"][0]["state"].update(read_only=False),
                ),
                "not read-only",
            ),
            (
                "saved",
                lambda value: self.preparation(value).update(saved_to_disk=True),
                "source save",
            ),
            (
                "configuration",
                lambda value: (
                    self.preparation(value)["state_after"].update(configuration_needs_rebuild=True),
                    self.preparation(value)["assembly_states_after"][0]["state"].update(
                        configuration_needs_rebuild=True
                    ),
                ),
                "not fully rebuilt",
            ),
            (
                "source_hash",
                lambda value: self.preparation(value)["source_files_after"][_normalized(PART)].update(
                    sha256="c" * 64
                ),
                "source bytes",
            ),
            (
                "pending_without_rebuild",
                lambda value: (
                    self.preparation(value)["state_before"].update(needs_rebuild2_value=1),
                    self.preparation(value)["assembly_states_before"][0]["state"].update(
                        needs_rebuild2_value=1
                    ),
                    self.preparation(value).update(needs_rebuild2_before=1),
                ),
                "no rebuild evidence",
            ),
            (
                "topology_after",
                lambda value: self.preparation(value)["topology_after"].pop(1),
                "final topology",
            ),
            (
                "topology_path",
                lambda value: self.preparation(value)["topology_before"][0].update(
                    normalized_path="wrong"
                ),
                "normalized path",
            ),
            (
                "assembly_instances",
                lambda value: self.preparation(value)["assembly_states_before"][0].update(
                    instances=["top", "extra"]
                ),
                "instances differ",
            ),
            (
                "raw_component",
                lambda value: value["discovery"]["raw"]["components"].pop(),
                "active native discovery",
            ),
            (
                "sealed_edge",
                lambda value: value["discovery"]["raw"]["source_selection"]["closure"]["documents"][
                    "cad/robot.SLDASM"
                ]["resolved_edges"].pop(),
                "sealed native occurrence graph",
            ),
        )
        for label, mutate, pattern in cases:
            with self.subTest(case=label):
                self.assert_rejected(mutate, pattern)


class NativeReceiptTests(unittest.TestCase):
    def receipts(self) -> tuple[Path, dict[str, bytes], dict]:
        temporary = tempfile.TemporaryDirectory(prefix="native-receipts-")
        self.addCleanup(temporary.cleanup)
        workspace = Path(temporary.name)
        root = capture_root(
            workspace,
            run_id=RUN_ID,
            handoff=HANDOFF,
            main=MAIN_ASSEMBLY,
        )
        tool = native_tool()
        transfer = seal(
            root,
            workspace / "native-evidence.zip",
            run_id=RUN_ID,
            handoff_sha256=HANDOFF,
            main_assembly=MAIN_ASSEMBLY,
            native_tool=tool,
        )
        write_json(root / "tool.json", tool)
        return (
            root,
            {
                "native_tool_bytes": (root / "reports/native-tool.json").read_bytes(),
                "native_stages_bytes": (root / "reports/native-stages.json").read_bytes(),
                "transfer_manifest_bytes": canonical(transfer),
            },
            transfer,
        )

    def test_receipt_triplet_is_exactly_cross_bound(self) -> None:
        root, receipts, _transfer = self.receipts()
        evidence = _native_receipts_evidence(root, receipts)
        self.assertEqual(evidence["native_tool_sha256"], digest(evidence["native_tool"]))

    def test_tampered_native_tool_or_stage_bytes_are_rejected(self) -> None:
        for key in ("native_tool_bytes", "native_stages_bytes"):
            with self.subTest(key=key):
                root, receipts, _transfer = self.receipts()
                payload = json.loads(receipts[key])
                payload["tampered"] = True
                receipts[key] = canonical(payload)
                with self.assertRaises(PipelineError):
                    _native_receipts_evidence(root, receipts)

    def test_transfer_identity_must_match_the_tool_bytes(self) -> None:
        root, receipts, transfer = self.receipts()
        transfer["native_tool_sha256"] = "f" * 64
        receipts["transfer_manifest_bytes"] = canonical(transfer)
        with self.assertRaisesRegex(PipelineError, "native tool digest"):
            _native_receipts_evidence(root, receipts)

    def test_transfer_schema_inventory_total_and_stage_events_are_semantic(self) -> None:
        mutations = (
            ("schema", lambda transfer: transfer.update(schema_version="unknown"), "unknown schema"),
            ("file_count", lambda transfer: transfer.update(file_count=transfer["file_count"] + 1), "file count"),
            (
                "file_count_type",
                lambda transfer: transfer.update(file_count=float(transfer["file_count"])),
                "file count",
            ),
            (
                "total_bytes",
                lambda transfer: transfer.update(total_bytes=transfer["total_bytes"] + 1),
                "size differs",
            ),
            (
                "total_bytes_type",
                lambda transfer: transfer.update(total_bytes=float(transfer["total_bytes"])),
                "total bytes",
            ),
            ("unknown_field", lambda transfer: transfer.update(extra=True), "incomplete or unknown"),
            ("run_id", lambda transfer: transfer.update(run_id=RUN_ID + "-other"), "UUID"),
            (
                "stage_scope",
                lambda transfer: transfer.update(native_stage_scope=["freeze"]),
                "stage scope",
            ),
        )
        for label, mutate, pattern in mutations:
            with self.subTest(case=label):
                root, receipts, transfer = self.receipts()
                mutate(transfer)
                receipts["transfer_manifest_bytes"] = canonical(transfer)
                with self.assertRaisesRegex(PipelineError, pattern):
                    _native_receipts_evidence(root, receipts)

        root, receipts, transfer = self.receipts()
        previous_stages_bytes = len(receipts["native_stages_bytes"])
        stages = json.loads(receipts["native_stages_bytes"])
        stages.pop("events")
        stages_blob = canonical(stages)
        transfer["total_bytes"] += len(stages_blob) - previous_stages_bytes
        transfer["files"]["reports/native-stages.json"] = hashlib.sha256(stages_blob).hexdigest()
        receipts["native_stages_bytes"] = stages_blob
        receipts["transfer_manifest_bytes"] = canonical(transfer)
        (root / "reports/native-stages.json").write_bytes(stages_blob)
        with self.assertRaisesRegex(PipelineError, "raw protocol events"):
            _native_receipts_evidence(root, receipts)

    def test_required_receipt_members_cannot_be_dropped_around_changed_blobs(self) -> None:
        root, receipts, transfer = self.receipts()
        original_lengths = {
            name: len(receipts[name])
            for name in ("native_tool_bytes", "native_stages_bytes")
        }
        transfer["files"].pop("reports/native-tool.json")
        transfer["files"].pop("reports/native-stages.json")
        transfer["file_count"] -= 2
        transfer["total_bytes"] -= sum(original_lengths.values())
        receipts["native_tool_bytes"] = b" " + receipts["native_tool_bytes"]
        receipts["transfer_manifest_bytes"] = canonical(transfer)

        with self.assertRaisesRegex(PipelineError, "omits required capture reports"):
            _native_receipts_evidence(root, receipts)
        write_json(root / "transfer-manifest.json", transfer)
        with self.assertRaisesRegex(PipelineError, "omits required capture reports"):
            from description_pipeline.orchestration.stage_transfer import verify_transfer

            verify_transfer(root)


if __name__ == "__main__":
    unittest.main()
