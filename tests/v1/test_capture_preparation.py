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
                "source": TOP,
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
                "source": TOP,
                "copy": "source/robot.SLDASM",
                "source_sha256": "a" * 64,
                "copy_sha256": "a" * 64,
            },
            *[
                {
                    "instance": instance,
                    "source": PART,
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
        "source_files_before": copy.deepcopy(source_files),
        "source_files_after": copy.deepcopy(source_files),
        "session_dependency_paths": session_paths,
        "topology_before": copy.deepcopy(topology),
        "topology_after": copy.deepcopy(topology),
    }
    return {
        "scene": {
            "document": TOP,
            "source_root": str(Path(TOP).parent),
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
        "manifest": {"identity": {"configuration": "Default"}},
    }


def dynamic_child_payload() -> dict:
    """One clean initial child becomes pending before its fresh rebuild turn."""

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
    preparation["assembly_states_before"].append(
        {
            "depth": 1,
            "path": CHILD_ASSEMBLY,
            "instances": ["arm-1"],
            "state": copy.deepcopy(child_state),
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
    fresh_before = copy.deepcopy(child_state)
    fresh_before["needs_rebuild2_value"] = 1
    preparation["child_rebuilds"] = [
        {
            "path": CHILD_ASSEMBLY,
            "depth": 1,
            "instances": ["arm-1"],
            "elapsed_seconds": 0.1,
            "result": True,
            "state_before": fresh_before,
            "state_after": copy.deepcopy(child_state),
        }
    ]
    source_files = preparation["source_files_before"]
    child_key = _normalized(CHILD_ASSEMBLY)
    source_files.pop(_normalized(PART))
    source_files[child_key] = {"path": CHILD_ASSEMBLY, "sha256": "b" * 64}
    preparation["source_files_after"] = copy.deepcopy(source_files)
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
            "source": TOP,
            "copy": "source/robot.SLDASM",
            "source_sha256": "a" * 64,
            "copy_sha256": "a" * 64,
        },
        {
            "instance": "arm-1",
            "source": CHILD_ASSEMBLY,
            "copy": "source/arm.SLDASM",
            "source_sha256": "b" * 64,
            "copy_sha256": "b" * 64,
        },
    ]
    return value


class CapturePreparationTests(unittest.TestCase):
    def validate(self, value: dict) -> dict:
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
            }

        value = payload()
        preparation = self.preparation(value)
        preparation["topology_before"].append(suppressed_row())
        preparation["topology_after"].append(copy.deepcopy(suppressed_row()))
        preparation["topology_before"] = _sort_topology(preparation["topology_before"])
        preparation["topology_after"] = _sort_topology(preparation["topology_after"])
        preparation["component_count"] += 1
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
        with self.assertRaisesRegex(PipelineError, "sealed native occurrence graph"):
            self.validate(missing)

        forged = payload()
        forged_preparation = self.preparation(forged)
        forged_preparation["topology_before"].append(suppressed_row())
        forged_preparation["topology_after"].append(copy.deepcopy(suppressed_row()))
        forged_preparation["topology_before"] = _sort_topology(forged_preparation["topology_before"])
        forged_preparation["topology_after"] = _sort_topology(forged_preparation["topology_after"])
        forged_preparation["component_count"] += 1
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
                "total_bytes",
                lambda transfer: transfer.update(total_bytes=transfer["total_bytes"] + 1),
                "size differs",
            ),
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
        with self.assertRaisesRegex(PipelineError, "raw protocol events"):
            _native_receipts_evidence(root, receipts)


if __name__ == "__main__":
    unittest.main()
