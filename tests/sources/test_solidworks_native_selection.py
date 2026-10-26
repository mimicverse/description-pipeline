"""Native closure admission: staged-inventory walk, fail-closed refusals, seal alignment."""

from __future__ import annotations

import copy
import hashlib
import re
import tempfile
import unittest
from pathlib import Path

from . import _paths  # noqa: F401  (import side effect: sys.path)

from description_pipeline.sources.solidworks.errors import CadError  # noqa: E402
from description_pipeline.sources.solidworks.native import (  # noqa: E402
    pin_search_folders,
    read_search_folders,
)
from description_pipeline.sources.solidworks.native_selection import (  # noqa: E402
    IDENTITY_DECLARATIONS,
    NativeSelectionReader,
)
from description_pipeline.sources.solidworks.selection import (  # noqa: E402
    SelectionError,
    seal_closure_selection,
)

READER = "a" * 64

#: A complete declared delivery identity (W6 pre-verification).  The closure
#: reader refuses fail-closed when any of these is missing from the staged main.
DECLARED = {
    "dp.hardware_id": "m3.0",
    "dp.revision": "M3.0.3_20261013",
    "dp.owner": "structures",
    "dp.change_summary": "fixture declaration set",
    "dp.control.system": "git",
    "dp.control.reference": "mimicverse/description@main",
    "dp.delivery_configuration": "3.0 总装",
}


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def session_temp(*parts: str) -> str:
    return str(Path(tempfile.gettempdir()).joinpath(*parts))


def write_stage(root: Path, files: dict[str, bytes]) -> dict[str, str]:
    digests: dict[str, str] = {}
    for name, data in files.items():
        path = root.joinpath(*name.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        digests[name] = sha256(data)
    return digests


def occurrence(
    instance: str,
    resolved: str,
    *,
    defining: str = "robot.SLDASM",
    state: int | None = 2,
    virtual=False,
    imported=None,
    name=None,
    native_owner=None,
    configuration: str = "默认",
) -> dict:
    record = {
        "instance": instance,
        "name": name or instance,
        "depth": 0,
        "defining": defining,
        "configuration": configuration,
        "suppression_state": state,
        "resolved_path": resolved,
    }
    if virtual is not None:
        record["is_virtual"] = virtual
    if imported is not None:
        record["imported_path"] = imported
    if native_owner is not None:
        record["native_owner"] = native_owner
    return record


def readings(stage: Path, occurrences: list[dict], *, stored=None, **extra) -> dict:
    native_api_pid = None
    for item in occurrences:
        match = re.search(r"(?i)(?:^|[\\/])swx([0-9]+)(?:[\\/]|$)", str(item.get("resolved_path") or ""))
        if match is not None:
            native_api_pid = int(match.group(1))
            break
    value = {
        "stage_root": str(stage),
        "occurrences": occurrences,
        "stored_references": stored or {},
        "identity_properties": dict(DECLARED),
        "search": {
            "folder_type": 0,
            "apis": ["ISldWorks.GetSearchFolders", "ISldWorks.SetSearchFolders"],
            "before": [],
            "pinned": [str(stage)],
            "observed_after_pin": [str(stage)],
            "restored": True,
            "restore_error": None,
        },
        "open": {"errors": 0, "warnings": 0},
        "session": {
            "native_api_pid": native_api_pid or 111,
            "solidworks_revision": "34.0.0",
            "sealed": True,
            "closed": True,
        },
        "sealed": ["sealed"],
        "documents_before": 0,
        "documents_after": 0,
    }
    value.update(extra)
    return value


class ClosureAdmissionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.stage = Path(self._tmp.name).resolve()
        self.files = write_stage(
            self.stage,
            {"robot.SLDASM": b"robot-bytes", "arm.SLDPRT": b"arm-bytes", "base.SLDPRT": b"base-bytes"},
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def observe(self, occurrences: list[dict], *, stored=None, files=None, **extra) -> dict:
        index = files if files is not None else self.files
        raw = readings(self.stage, occurrences, stored=stored, **extra)
        reader = NativeSelectionReader(walker=lambda stage, main, files_: raw, reader_sha256=READER)
        return reader.observe(self.stage, "robot.SLDASM", index)

    def resolved(self, name: str) -> str:
        return str(self.stage.joinpath(*name.split("/")))

    def test_resolution_closure_seals_the_graph(self) -> None:
        stored = {
            "robot.SLDASM": [
                {"name": "arm", "location": r"C:\Users\operator\Desktop\M3.0\arm.SLDPRT"},
                {"name": "base", "location": r"C:\Users\operator\Desktop\M3.0\base.SLDPRT"},
            ]
        }
        payload = self.observe(
            [
                occurrence("arm-1", self.resolved("arm.SLDPRT")),
                occurrence("base-1", self.resolved("base.SLDPRT")),
            ],
            stored=stored,
        )
        self.assertEqual(payload["schema_version"], "solidworks-to-urdf.native-closure/v1")
        self.assertEqual(payload["main_assembly"], "robot.SLDASM")
        self.assertEqual(payload["reader_sha256"], READER)
        self.assertEqual(sorted(payload["graph"]), ["arm.SLDPRT", "base.SLDPRT", "robot.SLDASM"])
        self.assertEqual(payload["graph"]["robot.SLDASM"]["references"], ["arm.SLDPRT", "base.SLDPRT"])
        self.assertEqual(payload["graph"]["arm.SLDPRT"]["references"], [])
        self.assertEqual(sorted(payload["documents"]), sorted(payload["graph"]))
        # Stored authoring-absolute locations are verbatim evidence, never admission.
        self.assertEqual(payload["documents"]["robot.SLDASM"]["stored_references"], stored["robot.SLDASM"])
        self.assertEqual(payload["excluded"], [])
        self.assertEqual(payload["suppressed"], [])
        self.assertEqual(payload["imports"], [])
        self.assertEqual(payload["search_configuration"]["pinned"], [str(self.stage)])
        self.assertTrue(payload["search_configuration"]["restored"])
        self.assertIsNone(payload["search_configuration"]["restore_error"])
        self.assertTrue(payload["session"]["sealed"])
        self.assertTrue(payload["session"]["closed"])
        # The produced payload is exactly the current seal contract.
        manifest = seal_closure_selection(self.files, "robot.SLDASM", payload, reader_sha256=READER)
        self.assertEqual(sorted(manifest["files"]), ["arm.SLDPRT", "base.SLDPRT", "robot.SLDASM"])
        self.assertEqual(manifest["excluded_files"], {})
        self.assertEqual(manifest["closure"]["main_assembly"], "robot.SLDASM")

    def test_excluded_lists_unresolved_staged_files(self) -> None:
        payload = self.observe([occurrence("arm-1", self.resolved("arm.SLDPRT"))])
        self.assertEqual(payload["excluded"], ["base.SLDPRT"])

    def test_outside_resolution_refused_with_verbatim_paths(self) -> None:
        with self.assertRaises(SelectionError) as caught:
            self.observe([occurrence("arm-1", r"C:\Users\operator\Desktop\M3.0\arm.SLDPRT")])
        error = caught.exception
        self.assertEqual(error.code, "selection_resolved_outside_stage")
        item = error.detail["problems"][0]
        self.assertEqual(
            sorted(item),
            ["code", "document", "instance", "message", "resolved", "stored_location"],
        )
        self.assertEqual(item["instance"], "arm-1")
        self.assertEqual(item["resolved"], r"C:\Users\operator\Desktop\M3.0\arm.SLDPRT")
        self.assertIsNone(item["stored_location"])
        self.assertEqual(error.detail["count"], 1)

    def test_under_stage_but_not_indexed_refused(self) -> None:
        extra = write_stage(self.stage, {"other.SLDPRT": b"other"})
        files = {**self.files, **extra}
        with self.assertRaises(SelectionError) as caught:
            self.observe(
                [occurrence("arm-1", self.resolved("other.SLDPRT"))],
                files={name: files[name] for name in ("robot.SLDASM", "arm.SLDPRT")},
            )
        self.assertEqual(caught.exception.code, "selection_resolved_not_in_inventory")

    def test_unresolved_occurrence_refused(self) -> None:
        with self.assertRaises(SelectionError) as caught:
            self.observe([occurrence("arm-1", "")])
        self.assertEqual(caught.exception.code, "selection_dependency_unresolved")
        self.assertEqual(caught.exception.detail["problems"][0]["instance"], "arm-1")

    def test_delivered_occurrence_requires_an_explicit_false_virtual_reading(self) -> None:
        with self.assertRaises(SelectionError) as caught:
            self.observe(
                [
                    occurrence(
                        "arm-1",
                        self.resolved("arm.SLDPRT"),
                        virtual=None,
                    )
                ]
            )
        self.assertEqual(caught.exception.code, "selection_virtual_state_unproven")

    def test_suppressed_virtual_component_is_refused_without_an_active_binding(self) -> None:
        document = session_temp("swx21040", "VC~~", "waist", "virtual_part.SLDPRT")
        with self.assertRaises(SelectionError) as caught:
            self.observe(
                [
                    occurrence(
                        "waist-1/virtual_part-1",
                        document,
                        state=0,
                        virtual=True,
                        native_owner="robot.SLDASM",
                    )
                ]
            )
        self.assertEqual(
            caught.exception.code,
            "selection_virtual_state_unproven",
        )

    def test_suppressed_occurrence_requires_false_virtual_and_no_mixed_session_marker(self) -> None:
        with self.assertRaises(SelectionError) as caught:
            self.observe(
                [
                    occurrence(
                        "arm-1",
                        self.resolved("arm.SLDPRT"),
                        state=0,
                        virtual=None,
                    )
                ]
            )
        self.assertEqual(caught.exception.code, "selection_virtual_state_unproven")

        mixed = session_temp("swx21040", "VC~~", "IC~~", "waist", "virtual_part.SLDPRT")
        with self.assertRaises(SelectionError) as caught:
            self.observe(
                [
                    occurrence(
                        "virtual_part-1",
                        mixed,
                        state=0,
                        virtual=False,
                    )
                ]
            )
        self.assertEqual(
            caught.exception.code,
            "selection_suppressed_session_document_unproven",
        )

    def test_embedded_virtual_component_is_owner_bound_without_an_import(self) -> None:
        document = session_temp(
            "swx21040",
            "VC~~",
            "waist",
            "Copy of 复件 (2) 零件10^waist.SLDPRT",
        )
        instance = "waist-1/Copy of 复件 (2) 零件10^waist-1"
        payload = self.observe(
            [
                occurrence(
                    instance,
                    document,
                    defining="robot.SLDASM",
                    virtual=True,
                    native_owner="robot.SLDASM",
                    configuration="默认",
                )
            ]
        )
        self.assertEqual(payload["imports"], [])
        self.assertEqual(sorted(payload["graph"]), ["robot.SLDASM"])
        self.assertEqual(len(payload["virtual_components"]), 1)
        entry = payload["virtual_components"][0]
        self.assertEqual(entry["state"], "embedded_virtual_component")
        self.assertEqual(entry["binding"], "owner_embedded_geometry")
        self.assertEqual(entry["owner"], "robot.SLDASM")
        self.assertEqual(entry["owner_sha256"], self.files["robot.SLDASM"])
        self.assertEqual(entry["instance"], instance)
        self.assertEqual(entry["configuration"], "默认")
        self.assertEqual(entry["session_document_path"], document)
        self.assertEqual(entry["session_temp_root"], session_temp("swx21040"))
        self.assertEqual(entry["native_api_pid"], 21040)
        self.assertIs(entry["is_virtual"], True)
        self.assertEqual(
            entry["used_api"],
            "IComponent2.GetPathName/IComponent2.GetImportedPath/IComponent2.IsVirtual",
        )
        warning = payload["warnings"][0]
        self.assertEqual(warning["code"], "embedded_virtual_component_geometry")
        self.assertTrue(warning["requires_owner_binding"])
        manifest = seal_closure_selection(self.files, "robot.SLDASM", payload, reader_sha256=READER)
        self.assertEqual(manifest["closure"]["virtual_components"], [entry])
        self.assertEqual(sorted(manifest["files"]), ["robot.SLDASM"])

    def test_embedded_virtual_component_requires_complete_api_and_owner_evidence(self) -> None:
        document = session_temp("swx21040", "VC~~", "waist", "virtual_part.SLDPRT")
        instance = "waist-1/virtual_part-1"
        valid = occurrence(
            instance,
            document,
            defining="robot.SLDASM",
            virtual=True,
            native_owner="robot.SLDASM",
            configuration="默认",
        )
        cases = {
            "missing_virtual": (
                {**valid, "is_virtual": None},
                "selection_virtual_component_unproven",
            ),
            "false_virtual": (
                {**valid, "is_virtual": False},
                "selection_virtual_component_unproven",
            ),
            "outside_session_temp": (
                {**valid, "resolved_path": r"C:\Users\operator\Documents\VC~~\waist\virtual_part.SLDPRT"},
                "selection_virtual_component_unproven",
            ),
            "wrong_session_root": (
                {
                    **valid,
                    "resolved_path": session_temp("not-a-session", "VC~~", "waist", "virtual_part.SLDPRT"),
                },
                "selection_virtual_component_unproven",
            ),
            "mixed_session_markers": (
                {
                    **valid,
                    "resolved_path": session_temp(
                        "swx21040", "VC~~", "IC~~", "waist", "virtual_part.SLDPRT"
                    ),
                },
                "selection_virtual_component_unproven",
            ),
            "ic_mislabeled_virtual": (
                {
                    **valid,
                    "resolved_path": session_temp("swx21040", "IC~~", "virtual_part.SLDPRT"),
                },
                "selection_virtual_component_import_conflict",
            ),
            "import_path_present": (
                {**valid, "imported_path": r"D:\author\virtual_part.step"},
                "selection_virtual_component_import_conflict",
            ),
            "import_path_unread": (
                {**valid, "import_error": "RuntimeError: GetImportedPath failed"},
                "selection_virtual_component_unproven",
            ),
            "import_path_non_text": (
                {**valid, "imported_path": 123},
                "selection_virtual_component_unproven",
            ),
            "normal_path_marked_virtual": (
                {**valid, "resolved_path": r"C:\Users\operator\Documents\virtual_part.SLDPRT"},
                "selection_virtual_component_unproven",
            ),
            "missing_owner": (
                {
                    **valid,
                    "defining": str(Path(document).parent),
                    "native_owner": str(Path(document).parent),
                },
                "selection_virtual_component_unproven",
            ),
            "different_owner": (
                {**valid, "native_owner": "arm.SLDPRT"},
                "selection_virtual_component_unproven",
            ),
            "missing_configuration": (
                {**valid, "configuration": ""},
                "selection_virtual_component_unproven",
            ),
            "anchor_mismatch": (
                {**valid, "defining_anchor": "other-1"},
                "selection_virtual_component_unproven",
            ),
        }
        for label, (record, code) in cases.items():
            with self.subTest(case=label):
                with self.assertRaises(SelectionError) as caught:
                    self.observe([record])
                self.assertEqual(caught.exception.code, code)

    def test_virtual_component_root_must_match_the_native_session_pid(self) -> None:
        document = session_temp("swx99999", "VC~~", "waist", "virtual_part.SLDPRT")
        record = occurrence(
            "waist-1/virtual_part-1",
            document,
            defining="robot.SLDASM",
            virtual=True,
            native_owner="robot.SLDASM",
            configuration="默认",
        )
        with self.assertRaises(SelectionError) as caught:
            self.observe(
                [record],
                session={
                    "native_api_pid": 111,
                    "solidworks_revision": "34.0.0",
                    "sealed": True,
                    "closed": True,
                },
            )
        self.assertEqual(caught.exception.code, "selection_virtual_component_unproven")

    def test_normal_child_of_a_virtual_assembly_binds_through_its_anchor(self) -> None:
        virtual_assembly = session_temp(
            "swx21040",
            "VC~~",
            "waist",
            "virtual_assembly.SLDASM",
        )
        parent = occurrence(
            "waist-1/virtual_assembly-1",
            virtual_assembly,
            defining="robot.SLDASM",
            virtual=True,
            native_owner="robot.SLDASM",
            configuration="默认",
        )
        child = occurrence(
            "waist-1/virtual_assembly-1/arm-1",
            self.resolved("arm.SLDPRT"),
            defining=virtual_assembly,
            virtual=False,
            native_owner="robot.SLDASM",
            configuration="默认",
        )
        child["defining_anchor"] = "waist-1/virtual_assembly-1"
        payload = self.observe([parent, child])
        self.assertEqual(sorted(payload["graph"]), ["arm.SLDPRT", "robot.SLDASM"])
        self.assertEqual(payload["graph"]["robot.SLDASM"]["references"], ["arm.SLDPRT"])
        edge = payload["documents"]["robot.SLDASM"]["resolved_edges"][0]
        self.assertEqual(edge["resolved"], "arm.SLDPRT")
        self.assertEqual(edge["owner_anchor"], "waist-1/virtual_assembly-1")
        self.assertEqual(edge["defining_session_document"], virtual_assembly)
        self.assertEqual(len(payload["virtual_components"]), 1)
        manifest = seal_closure_selection(self.files, "robot.SLDASM", payload, reader_sha256=READER)
        self.assertEqual(sorted(manifest["files"]), ["arm.SLDPRT", "robot.SLDASM"])
        forged = copy.deepcopy(payload)
        forged["documents"]["robot.SLDASM"]["resolved_edges"][0][
            "defining_session_document"
        ] = session_temp("swx21040", "VC~~", "waist", "other_virtual_assembly.SLDASM")
        with self.assertRaises(SelectionError) as caught:
            seal_closure_selection(self.files, "robot.SLDASM", forged, reader_sha256=READER)
        self.assertEqual(caught.exception.code, "selection_closure_invalid")

    def test_virtual_child_under_a_normal_assembly_uses_the_anchored_owner_chain(self) -> None:
        extra = write_stage(self.stage, {"sub.SLDASM": b"sub-bytes"})
        files = {**self.files, **extra}
        outer = session_temp("swx21040", "VC~~", "robot", "outer.SLDASM")
        inner = session_temp("swx21040", "VC~~", "sub", "inner.SLDPRT")
        parent = occurrence(
            "outer-1",
            outer,
            defining="robot.SLDASM",
            virtual=True,
            native_owner="robot.SLDASM",
            configuration="默认",
        )
        normal = occurrence(
            "outer-1/sub-1",
            self.resolved("sub.SLDASM"),
            defining=outer,
            virtual=False,
            native_owner="robot.SLDASM",
            configuration="默认",
        )
        normal["defining_anchor"] = "outer-1"
        nested = occurrence(
            "outer-1/sub-1/inner-1",
            inner,
            defining="sub.SLDASM",
            virtual=True,
            native_owner="sub.SLDASM",
            configuration="默认",
        )
        nested["defining_anchor"] = "outer-1"
        payload = self.observe([parent, normal, nested], files=files)
        self.assertEqual(payload["graph"]["robot.SLDASM"]["references"], ["sub.SLDASM"])
        self.assertEqual({row["owner"] for row in payload["virtual_components"]}, {"robot.SLDASM", "sub.SLDASM"})
        manifest = seal_closure_selection(files, "robot.SLDASM", payload, reader_sha256=READER)
        self.assertEqual(sorted(manifest["files"]), ["robot.SLDASM", "sub.SLDASM"])
        forged = copy.deepcopy(payload)
        nested_slot = next(
            row for row in forged["virtual_components"] if row["owner"] == "sub.SLDASM"
        )
        nested_slot["document"] = session_temp(
            "swx21040", "VC~~", "other", "other_owner.SLDASM"
        )
        with self.assertRaises(SelectionError) as caught:
            seal_closure_selection(files, "robot.SLDASM", forged, reader_sha256=READER)
        self.assertEqual(caught.exception.code, "selection_closure_invalid")

    def test_virtual_component_seal_rejects_forged_identity(self) -> None:
        document = session_temp("swx21040", "VC~~", "waist", "virtual_part.SLDPRT")
        instance = "waist-1/virtual_part-1"
        payload = self.observe(
            [
                occurrence(
                    instance,
                    document,
                    defining="robot.SLDASM",
                    virtual=True,
                    native_owner="robot.SLDASM",
                    configuration="默认",
                )
            ]
        )
        cases = {
            "not_virtual": lambda row: row.update(is_virtual=False),
            "wrong_kind": lambda row: row.update(kind="native_import_materialization"),
            "ic_document": lambda row: row.update(
                session_document_path=session_temp("swx21040", "IC~~", "virtual_part.SLDPRT")
            ),
            "unknown_owner": lambda row: row.update(owner="other.SLDASM"),
            "owner_sha": lambda row: row.update(owner_sha256="f" * 64),
            "missing_instance": lambda row: row.update(instance=""),
            "missing_configuration": lambda row: row.update(configuration=""),
            "author_path": lambda row: row.update(author_imported_path=r"D:\author\part.step"),
            "author_path_missing": lambda row: row.pop("author_imported_path"),
            "author_read_error": lambda row: row.update(author_imported_error="RuntimeError: failed"),
            "wrong_anchor": lambda row: row.update(owner_anchor="other-1"),
            "orphan_anchor": lambda row: row.update(
                owner_anchor="ghost-1", instance="ghost-1/virtual_part-1"
            ),
            "wrong_api": lambda row: row.update(used_api="basename_guess"),
            "wrong_native_pid": lambda row: row.update(native_api_pid=99999),
        }
        for label, mutate in cases.items():
            with self.subTest(case=label):
                forged = copy.deepcopy(payload)
                mutate(forged["virtual_components"][0])
                with self.assertRaises(SelectionError) as caught:
                    seal_closure_selection(self.files, "robot.SLDASM", forged, reader_sha256=READER)
                self.assertEqual(caught.exception.code, "selection_closure_invalid")
        forged = copy.deepcopy(payload)
        forged["virtual_components"].append(copy.deepcopy(forged["virtual_components"][0]))
        with self.assertRaises(SelectionError) as caught:
            seal_closure_selection(self.files, "robot.SLDASM", forged, reader_sha256=READER)
        self.assertEqual(caught.exception.code, "selection_closure_invalid")

    def test_materialized_import_is_a_recorded_warning_not_a_binding(self) -> None:
        temp = session_temp("swx39388", "IC~~", "imported_part.step.SLDPRT")
        payload = self.observe(
            [
                occurrence(
                    "imported_part.step-1",
                    temp,
                    imported=r"C:\Users\operator\Desktop\imported_part.step",
                )
            ]
        )
        self.assertEqual(sorted(payload["graph"]), ["robot.SLDASM"])
        entry = payload["imports"][0]
        self.assertEqual(entry["state"], "cached_materialization")
        self.assertEqual(entry["binding"], "author_metadata_only")
        self.assertEqual(entry["author_imported_path"], r"C:\Users\operator\Desktop\imported_part.step")
        self.assertEqual(entry["materialization"], temp)
        self.assertEqual(entry["session_temp_root"], str(Path(tempfile.gettempdir()) / "swx39388"))
        self.assertEqual(entry["owner"], "robot.SLDASM")
        self.assertEqual(entry["owner_sha256"], self.files["robot.SLDASM"])
        self.assertFalse(entry["author_metadata_in_stage"])
        warning = payload["warnings"][0]
        self.assertEqual(warning["code"], "import_cached_materialization")
        self.assertTrue(warning["needs_absence_test"])
        self.assertEqual(warning["owner_sha256"], self.files["robot.SLDASM"])

    def test_in_stage_author_metadata_is_flagged_but_not_a_binding(self) -> None:
        extra = write_stage(self.stage, {"imported_part.step": b"np-bytes"})
        files = {**self.files, **extra}
        temp = session_temp("swx28528", "IC~~", "imported_part.step.SLDPRT")
        payload = self.observe(
            [
                occurrence(
                    "imported_part.step-1",
                    temp,
                    imported=str(self.stage / "imported_part.step"),
                )
            ],
            files=files,
        )
        entry = payload["imports"][0]
        self.assertTrue(entry["author_metadata_in_stage"])
        self.assertEqual(entry["state"], "cached_materialization")
        # The in-stage author source is the actual delivered source: it stays
        # in the sealed graph so the frozen folder retains it for the collector.
        self.assertEqual(entry["bound_source"], "imported_part.step")
        self.assertEqual(payload["graph"]["robot.SLDASM"]["references"], ["imported_part.step"])
        edge = payload["documents"]["robot.SLDASM"]["resolved_edges"][0]
        self.assertEqual(edge["kind"], "native_imported_document")
        self.assertEqual(edge["resolved"], "imported_part.step")

    def test_np_real_shape_selection_and_raw_agree_on_the_cached_class(self) -> None:
        from description_pipeline.sources.solidworks.native import _component_source_document

        extra = write_stage(self.stage, {"imported_part.step": b"np-bytes"})
        files = {**self.files, **extra}
        temp = session_temp("swx28528", "IC~~", "imported_part.step.SLDPRT")
        payload = self.observe(
            [
                occurrence(
                    "imported_part.step-1",
                    temp,
                    imported=str(self.stage / "imported_part.step"),
                    native_owner="robot.SLDASM",
                )
            ],
            files=files,
        )
        slot = payload["imports"][0]
        self.assertEqual(slot["state"], "cached_materialization")
        self.assertEqual(slot["bound_source"], "imported_part.step")
        self.assertEqual(slot["owner"], "robot.SLDASM")
        self.assertEqual(slot["owner_sha256"], files["robot.SLDASM"])
        self.assertEqual(payload["graph"]["robot.SLDASM"]["references"], ["imported_part.step"])

        class RawComponent:
            def __init__(self, path, imported):
                self._path = path
                self.GetImportedPath = lambda: imported
                self.IsVirtual = False

            def GetPathName(self):
                return self._path

        raw = _component_source_document(
            RawComponent(temp, str(self.stage / "imported_part.step")), self.stage
        )
        self.assertEqual(raw["source_provenance"]["state"], "cached_materialization")
        # Two-sided class parity: the raw occurrence and the carrier slot agree
        # on the cached class for the same occurrence, so the independent gate
        # matches exactly one raw occurrence per cached slot.
        self.assertEqual(slot["instance"], "imported_part.step-1")
        self.assertTrue(raw["source_provenance"]["author_imported_in_source_root"])

    def test_import_marker_outside_session_temp_is_not_a_cache(self) -> None:
        fake_temp = r"C:\Users\operator\Documents\IC~~\imported_part.step.SLDPRT"
        with self.assertRaises(SelectionError) as caught:
            self.observe(
                [
                    occurrence(
                        "imported_part.step-1",
                        fake_temp,
                        imported=r"C:\Users\operator\Desktop\imported_part.step",
                    )
                ]
            )
        self.assertEqual(caught.exception.code, "selection_resolved_outside_stage")
        item = caught.exception.detail["problems"][0]
        self.assertEqual(item["resolved"], fake_temp)

    def test_cached_import_subtree_is_recorded_without_refusal(self) -> None:
        parent = session_temp("swx25524", "IC~~", "bracket_a.stp.SLDASM")
        child = session_temp("swx25524", "IC~~", "HL-3608_ASM_52_ASM.stp.SLDASM")
        payload = self.observe(
            [
                occurrence(
                    "bracket_a.stp-2",
                    parent,
                    imported=r"D:\M2.2\change11\HD.stp",
                    native_owner="robot.SLDASM",
                ),
                occurrence(
                    "bracket_a.stp-2/HL-3608_ASM_52_ASM.stp-1",
                    child,
                    defining=parent,
                    imported=r"D:\M2.2\change11\HD.stp",
                    native_owner="robot.SLDASM",
                ),
            ]
        )
        self.assertEqual(len(payload["imports"]), 2)
        self.assertEqual(sorted(payload["graph"]), ["robot.SLDASM"])
        # The ultimate native owner propagates through nested materialization
        # descendants (never a temp parent, never None) with its owner hash.
        for entry in payload["imports"]:
            self.assertEqual(entry["owner"], "robot.SLDASM")
            self.assertEqual(entry["owner_sha256"], self.files["robot.SLDASM"])
        by_instance = {entry["instance"]: entry for entry in payload["imports"]}
        # document stays verbatim raw evidence: canonical for the top-level
        # import, the temp parent materialization for the nested descendant.
        self.assertEqual(by_instance["bracket_a.stp-2"]["document"], "robot.SLDASM")
        self.assertEqual(
            by_instance["bracket_a.stp-2/HL-3608_ASM_52_ASM.stp-1"]["document"], parent
        )
        # The seal resolves graph membership from the validated ultimate owner.
        manifest = seal_closure_selection(self.files, "robot.SLDASM", payload, reader_sha256=READER)
        # Cached imports add no edges, so only the main is reachable here; the
        # staged parts remain reported as excluded, never silently dropped.
        self.assertEqual(sorted(manifest["files"]), ["robot.SLDASM"])

    def _nested_payload(self):
        parent = session_temp("swx25524", "IC~~", "bracket_a.stp.SLDASM")
        child = session_temp("swx25524", "IC~~", "HL-3608_ASM_52_ASM.stp.SLDASM")
        child_record = occurrence(
            "bracket_a.stp-2/HL-3608_ASM_52_ASM.stp-1",
            child,
            defining=parent,
            imported=r"D:\M2.2\change11\HD.stp",
            native_owner="robot.SLDASM",
        )
        child_record["defining_anchor"] = "bracket_a.stp-2"
        payload = self.observe(
            [
                occurrence(
                    "bracket_a.stp-2",
                    parent,
                    imported=r"D:\M2.2\change11\HD.stp",
                    native_owner="robot.SLDASM",
                ),
                child_record,
            ]
        )
        return parent, child, payload

    def test_seal_refuses_missing_or_forged_cached_owner(self) -> None:
        import copy

        _, _, payload = self._nested_payload()
        child_instance = "bracket_a.stp-2/HL-3608_ASM_52_ASM.stp-1"

        def drop_owner(entry):
            entry.pop("owner")

        def ghost_owner(entry):
            entry["owner"] = "ghost.SLDASM"

        def forged_sha(entry):
            entry["owner_sha256"] = "0" * 64

        def wrong_anchor(entry):
            entry["owner_anchor"] = "not-an-ancestor"

        def wrong_parent_document(entry):
            entry["document"] = session_temp("swx25524", "IC~~", "other_parent.SLDASM")

        for tamper in (drop_owner, ghost_owner, forged_sha, wrong_anchor, wrong_parent_document):
            with self.subTest(tamper=tamper.__name__):
                forged = copy.deepcopy(payload)
                entry = next(item for item in forged["imports"] if item["instance"] == child_instance)
                tamper(entry)
                with self.assertRaises(SelectionError) as caught:
                    seal_closure_selection(self.files, "robot.SLDASM", forged, reader_sha256=READER)
                self.assertEqual(caught.exception.code, "selection_closure_invalid")

    def test_nested_bound_source_is_retained_under_the_ultimate_owner(self) -> None:
        extra = write_stage(self.stage, {"imported_part.step": b"np-bytes"})
        files = {**self.files, **extra}
        parent = session_temp("swx25524", "IC~~", "bracket_a.stp.SLDASM")
        child = session_temp("swx25524", "IC~~", "HL-3608_ASM_52_ASM.stp.SLDASM")
        payload = self.observe(
            [
                occurrence(
                    "bracket_a.stp-2",
                    parent,
                    imported=r"D:\M2.2\change11\HD.stp",
                    native_owner="robot.SLDASM",
                ),
                occurrence(
                    "bracket_a.stp-2/HL-3608_ASM_52_ASM.stp-1",
                    child,
                    defining=parent,
                    imported=str(self.stage / "imported_part.step"),
                    native_owner="robot.SLDASM",
                ),
            ],
            files=files,
        )
        child_entry = next(
            entry
            for entry in payload["imports"]
            if entry["instance"].endswith("HL-3608_ASM_52_ASM.stp-1")
        )
        self.assertEqual(child_entry["bound_source"], "imported_part.step")
        self.assertEqual(child_entry["document"], parent)  # verbatim raw evidence
        self.assertEqual(child_entry["owner"], "robot.SLDASM")
        # The retained edge hangs off the ultimate owner (never the temp parent).
        self.assertEqual(payload["graph"]["robot.SLDASM"]["references"], ["imported_part.step"])
        manifest = seal_closure_selection(files, "robot.SLDASM", payload, reader_sha256=READER)
        self.assertIn("imported_part.step", manifest["files"])

    def test_bound_source_without_valid_ultimate_owner_refuses_before_mutation(self) -> None:
        extra = write_stage(self.stage, {"imported_part.step": b"np-bytes"})
        files = {**self.files, **extra}
        parent = session_temp("swx25524", "IC~~", "bracket_a.stp.SLDASM")
        child = session_temp("swx25524", "IC~~", "HL-3608_ASM_52_ASM.stp.SLDASM")
        with self.assertRaises(SelectionError) as caught:
            self.observe(
                [
                    occurrence(
                        "bracket_a.stp-2",
                        parent,
                        imported=r"D:\M2.2\change11\HD.stp",
                        native_owner="ghost.SLDASM",
                    ),
                    occurrence(
                        "bracket_a.stp-2/HL-3608_ASM_52_ASM.stp-1",
                        child,
                        defining=parent,
                        imported=str(self.stage / "imported_part.step"),
                        native_owner="ghost.SLDASM",
                    ),
                ],
                files=files,
            )
        self.assertEqual(caught.exception.code, "selection_closure_invalid")
        item = caught.exception.detail["problems"][0]
        self.assertEqual(item["instance"], "bracket_a.stp-2/HL-3608_ASM_52_ASM.stp-1")
        self.assertEqual(item["resolved"], "imported_part.step")

    def test_import_without_materialization_refused(self) -> None:
        with self.assertRaises(SelectionError) as caught:
            self.observe(
                [
                    occurrence(
                        "imported_part.step-1",
                        "",
                        imported=r"C:\Users\operator\Desktop\imported_part.step",
                    )
                ]
            )
        error = caught.exception
        self.assertEqual(error.code, "selection_import_unresolved")
        item = error.detail["problems"][0]
        self.assertEqual(item["imported_path"], r"C:\Users\operator\Desktop\imported_part.step")

    def test_in_stage_import_suffixed_document_is_a_native_delivery(self) -> None:
        extra = write_stage(self.stage, {"bracket_a.step.SLDPRT": b"cached-brep"})
        files = {**self.files, **extra}
        payload = self.observe(
            [
                occurrence(
                    "bracket_a.stp-2",
                    self.resolved("bracket_a.step.SLDPRT"),
                    imported=r"D:\author\bracket_a.stp",
                )
            ],
            files=files,
        )
        self.assertEqual(
            payload["graph"]["robot.SLDASM"]["references"], ["bracket_a.step.SLDPRT"]
        )
        self.assertEqual(payload["imports"], [])

    def test_state5_and_unreadable_states_refuse_with_instance(self) -> None:
        with self.assertRaises(SelectionError) as caught:
            self.observe([occurrence("servo-1", "", state=5)])
        self.assertEqual(caught.exception.code, "cad_component_internal_id_mismatch")
        self.assertEqual(caught.exception.detail["problems"][0]["suppression_state"], 5)
        with self.assertRaises(SelectionError) as caught:
            self.observe([occurrence("servo-1", "", state=None)])
        self.assertEqual(caught.exception.code, "cad_component_state_unreadable")
        with self.assertRaises(SelectionError) as caught:
            self.observe([occurrence("servo-1", "", state=9)])
        self.assertEqual(caught.exception.code, "cad_component_state_unsupported")

    def test_suppressed_placement_omitted_from_graph(self) -> None:
        payload = self.observe(
            [
                occurrence("cover-3", r"C:\elsewhere\cover.SLDPRT", state=0),
                occurrence("arm-1", self.resolved("arm.SLDPRT")),
            ]
        )
        entry = payload["suppressed"][0]
        self.assertEqual(entry["document"], "robot.SLDASM")
        self.assertEqual(entry["instance"], "cover-3")
        self.assertEqual(entry["suppression_state"], 0)
        self.assertEqual(entry["resolved_path"], r"C:\elsewhere\cover.SLDPRT")
        # A suppressed placement is only author intent after a clean open
        # (errors == 0), and it is recorded with that justification.
        self.assertEqual(entry["justification"], "authored_suppression_clean_open")
        self.assertNotIn("cover.SLDPRT", payload["graph"])

    def test_suppressed_without_resolvable_document_refused(self) -> None:
        with self.assertRaises(SelectionError) as caught:
            self.observe([occurrence("cover-3", "", state=0)])
        self.assertEqual(caught.exception.code, "selection_suppressed_unresolved")

    def test_open_errors_refuse_the_closure(self) -> None:
        with self.assertRaises(SelectionError) as caught:
            self.observe(
                [occurrence("arm-1", self.resolved("arm.SLDPRT"))],
                open={"errors": 2, "warnings": 0},
            )
        error = caught.exception
        self.assertEqual(error.code, "selection_open_errors")
        item = error.detail["problems"][0]
        self.assertEqual(item["open_errors"], 2)
        self.assertEqual(item["document"], "robot.SLDASM")

    def test_unreadable_open_status_refused(self) -> None:
        with self.assertRaises(SelectionError) as caught:
            self.observe(
                [occurrence("arm-1", self.resolved("arm.SLDPRT"))],
                open={"errors": None, "warnings": None},
            )
        self.assertEqual(caught.exception.code, "selection_open_status_unreadable")

    def test_lightweight_placement_admitted_and_flagged(self) -> None:
        payload = self.observe([occurrence("arm-1", self.resolved("arm.SLDPRT"), state=1)])
        self.assertEqual(payload["graph"]["robot.SLDASM"]["references"], ["arm.SLDPRT"])
        self.assertTrue(payload["documents"]["robot.SLDASM"]["resolved_edges"][0]["lightweight"])

    def test_staged_bytes_changed_from_index_refused(self) -> None:
        broken = dict(self.files)
        broken["arm.SLDPRT"] = "0" * 64
        with self.assertRaises(SelectionError) as caught:
            self.observe([occurrence("arm-1", self.resolved("arm.SLDPRT"))], files=broken)
        self.assertEqual(caught.exception.code, "selection_source_changed")

    def test_main_must_be_an_indexed_assembly(self) -> None:
        reader = NativeSelectionReader(walker=lambda *_: readings(self.stage, []), reader_sha256=READER)
        with self.assertRaises(SelectionError) as caught:
            reader.observe(self.stage, "missing.SLDASM", self.files)
        self.assertEqual(caught.exception.code, "selection_main_invalid")

    def test_reader_construction_refusals(self) -> None:
        with self.assertRaises(SelectionError):
            NativeSelectionReader(walker=lambda *_: {}, backend=object())
        with self.assertRaises(SelectionError):
            NativeSelectionReader(walker=lambda *_: {}, reader_sha256="short")
        with self.assertRaises(SelectionError):
            NativeSelectionReader(walker="not-callable")
        with self.assertRaises(SelectionError):
            NativeSelectionReader(walker=lambda *_: {}, cad_ownership_file=Path("ledger.json"))
        with self.assertRaises(SelectionError):
            NativeSelectionReader(backend=object(), cad_ownership_file=Path("ledger.json"))

    def test_default_backend_can_be_bound_to_a_cad_ownership_file(self) -> None:
        ledger = Path("/state/cad-ownership.json")
        reader = NativeSelectionReader(cad_ownership_file=ledger)
        backend = reader._walker.__self__
        self.assertEqual(backend._cad_ownership_file, ledger)
        self.assertIsNone(NativeSelectionReader()._walker.__self__._cad_ownership_file)


class SearchFolderPinTests(unittest.TestCase):
    class App:
        def __init__(self, value=""):
            self.value = value
            self.calls: list = []

        def GetSearchFolders(self, folder_type):  # noqa: N802
            self.calls.append(("get", folder_type))
            return self.value

        def SetSearchFolders(self, folder_type, folders):  # noqa: N802
            self.calls.append(("set", folder_type, folders))
            self.value = folders

    class Broken:
        def GetSearchFolders(self, folder_type):  # noqa: N802
            return ""

        def SetSearchFolders(self, folder_type, folders):  # noqa: N802
            raise RuntimeError("denied")

    class Refusing(App):
        def SetSearchFolders(self, folder_type, folders):  # noqa: N802
            self.calls.append(("set", folder_type, folders))
            return False

    class Ignoring(App):
        def SetSearchFolders(self, folder_type, folders):  # noqa: N802
            self.calls.append(("set", folder_type, folders))
            return True

    def test_pin_records_before_after_and_reads_back(self) -> None:
        app = self.App(r"C:\machine-wide")
        evidence = pin_search_folders(app, r"C:\stage")
        self.assertEqual(evidence["before"], [r"C:\machine-wide"])
        self.assertEqual(evidence["pinned"], [r"C:\stage"])
        self.assertEqual(evidence["observed_after_pin"], [r"C:\stage"])
        self.assertEqual(evidence["folder_type"], 0)
        self.assertEqual(app.calls, [("get", 0), ("set", 0, r"C:\stage"), ("get", 0)])

    def test_pin_fails_closed_when_unwritable(self) -> None:
        with self.assertRaises(CadError) as caught:
            pin_search_folders(self.Broken(), r"C:\stage")
        self.assertEqual(caught.exception.code, "cad_search_folders_unwritable")

    def test_pin_refuses_false_return_and_unapplied_readback(self) -> None:
        with self.assertRaises(CadError) as caught:
            pin_search_folders(self.Refusing(r"C:\x"), r"C:\stage")
        self.assertEqual(caught.exception.code, "cad_search_folders_not_applied")
        with self.assertRaises(CadError) as caught:
            pin_search_folders(self.Ignoring(r"C:\x"), r"C:\stage")
        self.assertEqual(caught.exception.code, "cad_search_folders_not_applied")

    def test_read_parses_delimited_and_sequence_values(self) -> None:
        app = self.App(r"C:\a;C:\b")
        self.assertEqual(read_search_folders(app)["folders"], [r"C:\a", r"C:\b"])
        app.value = ["C:\\a", "C:\\b"]
        self.assertEqual(read_search_folders(app)["folders"], [r"C:\a", r"C:\b"])
        app.value = None
        self.assertEqual(read_search_folders(app)["folders"], [])


class BackendShapeIntegrationTests(unittest.TestCase):
    """The policy consumes what the real backend walk produces, not a payload."""

    def test_backend_closure_readings_feed_policy_and_seal(self) -> None:
        from .test_solidworks_native_reader import _Component, _Doc, _com_stubs
        from description_pipeline.sources.solidworks.native import SolidWorksBackend

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        stage = Path(tmp.name).resolve()
        files = write_stage(stage, {"robot.SLDASM": b"robot", "arm.SLDPRT": b"arm"})
        arm = _Component("arm-1", str(stage / "arm.SLDPRT"), doc=_Doc(str(stage / "arm.SLDPRT")))
        robot = _Doc(
            str(stage / "robot.SLDASM"), children=(arm,), configuration="默认", properties=dict(DECLARED)
        )
        get_save_calls: list = []
        robot.GetSaveFlag = lambda: (get_save_calls.append(1), False)[1]

        class FakeApp:
            @staticmethod
            def RevisionNumber():
                return "34.0.0"

            def __init__(self):
                self.folders = ""

            def GetSearchFolders(self, folder_type):  # noqa: N802
                return self.folders

            def SetSearchFolders(self, folder_type, folders):  # noqa: N802
                self.folders = folders
                return True

            def GetProcessID(self):  # noqa: N802
                return 111

            def GetDocumentCount(self):  # noqa: N802
                return 0

            def GetDocumentDependencies2(self, path, traverse, search, read_only):  # noqa: N802
                assert (traverse, search, read_only) == (False, False, False)
                return []

        app = FakeApp()

        class FakeSession:
            def __init__(self):
                self.closed = False

            def connect(self, cancelled):
                return app

            def current_application(self):
                return app

            def identity(self):
                return {
                    "pid": 111,
                    "executable": "fake",
                    "ownership": "windows_job",
                    "state": "closed" if self.closed else "active",
                }

            def close(self):
                self.closed = True

            def terminate(self):
                self.closed = True

        backend = SolidWorksBackend(session_factory=FakeSession)
        backend._probe_document = lambda path: {
            "doc": robot,
            "errors": 0,
            "warnings": 0,
            "open_exception": None,
            "already_open": False,
        }
        with _com_stubs():
            readings = backend.closure_readings(stage, "robot.SLDASM", files)

        for key in ("before", "pinned", "observed_after_pin", "restored", "restore_error", "apis"):
            self.assertIn(key, readings["search"])
        self.assertEqual(readings["search"]["observed_after_pin"], [str(stage)])
        self.assertTrue(readings["search"]["restored"])
        self.assertIsNone(readings["search"]["restore_error"])
        self.assertTrue(readings["session"]["sealed"])
        self.assertTrue(readings["session"]["closed"])
        self.assertEqual(readings["stored_references_phase"], "preopen")
        self.assertEqual(readings["saved_configuration"], "默认")
        state = readings["document_state"]
        self.assertEqual(state["before"]["save_flag"], False)
        self.assertEqual(state["after"]["save_flag"], False)
        self.assertEqual(state["after"]["configuration"], "默认")
        self.assertEqual(state["before"]["read_only"], True)
        self.assertIn("used_api", state)
        # Two saved-state samples: one at open and one after the native walk.
        self.assertGreaterEqual(len(get_save_calls), 2)

        payload = NativeSelectionReader(
            walker=lambda stage_root, main, index: readings, reader_sha256=READER
        ).observe(stage, "robot.SLDASM", files)
        self.assertEqual(payload["graph"]["robot.SLDASM"]["references"], ["arm.SLDPRT"])
        self.assertEqual(payload["saved_configuration"], "默认")
        self.assertIn("document_state", payload)
        manifest = seal_closure_selection(files, "robot.SLDASM", payload, reader_sha256=READER)
        self.assertEqual(manifest["main_assembly"], "robot.SLDASM")
        self.assertEqual(sorted(manifest["files"]), ["arm.SLDPRT", "robot.SLDASM"])

    def test_real_walk_nested_materialization_and_virtual_component_seal_with_owner(self) -> None:
        from .test_solidworks_native_reader import _Component, _Doc, _com_stubs
        from description_pipeline.sources.solidworks.native import SolidWorksBackend

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        stage = Path(tmp.name).resolve()
        files = write_stage(stage, {"robot.SLDASM": b"robot", "arm.SLDPRT": b"arm"})
        parent_doc = session_temp("swx111", "IC~~", "bracket_a.stp.SLDASM")
        child_doc = session_temp("swx111", "IC~~", "3915_ASM_2_ASM.stp.SLDASM")
        child = _Component("3915_ASM_2_ASM.stp-1", child_doc, doc=_Doc(child_doc))
        parent = _Component(
            "bracket_a.stp-2", parent_doc, doc=_Doc(parent_doc), children=(child,)
        )
        virtual_doc = session_temp(
            "swx111",
            "VC~~",
            "waist",
            "Copy of 复件 (2) 零件10^waist.SLDPRT",
        )
        virtual = _Component(
            "Copy of 复件 (2) 零件10^waist-1",
            virtual_doc,
            doc=_Doc(virtual_doc),
            configuration="默认",
        )
        virtual.IsVirtual = True
        virtual.GetImportedPath = lambda: None
        arm = _Component("arm-1", str(stage / "arm.SLDPRT"), doc=_Doc(str(stage / "arm.SLDPRT")))
        robot = _Doc(
            str(stage / "robot.SLDASM"),
            children=(arm, parent, virtual),
            configuration="默认",
            properties=dict(DECLARED),
        )

        class FakeApp:
            @staticmethod
            def RevisionNumber():
                return "34.0.0"

            def __init__(self):
                self.folders = ""

            def GetSearchFolders(self, folder_type):  # noqa: N802
                return self.folders

            def SetSearchFolders(self, folder_type, folders):  # noqa: N802
                self.folders = folders
                return True

            def GetProcessID(self):  # noqa: N802
                return 111

            def GetDocumentCount(self):  # noqa: N802
                return 0

            def GetDocumentDependencies2(self, path, traverse, search, read_only):  # noqa: N802
                assert (traverse, search, read_only) == (False, False, False)
                return []

        app = FakeApp()

        class FakeSession:
            def __init__(self):
                self.closed = False

            def connect(self, cancelled):
                return app

            def current_application(self):
                return app

            def identity(self):
                return {
                    "pid": 111,
                    "executable": "fake",
                    "ownership": "windows_job",
                    "state": "closed" if self.closed else "active",
                }

            def close(self):
                self.closed = True

            def terminate(self):
                self.closed = True

        backend = SolidWorksBackend(session_factory=FakeSession)
        backend._probe_document = lambda path: {
            "doc": robot,
            "errors": 0,
            "warnings": 0,
            "open_exception": None,
            "already_open": False,
        }
        with _com_stubs():
            readings = backend.closure_readings(stage, "robot.SLDASM", files)
        payload = NativeSelectionReader(
            walker=lambda stage_root, main, index: readings, reader_sha256=READER
        ).observe(stage, "robot.SLDASM", files)
        self.assertEqual(sorted(payload["graph"]), ["arm.SLDPRT", "robot.SLDASM"])
        entries = payload["imports"]
        self.assertEqual(len(entries), 2)
        by_instance = {entry["instance"]: entry for entry in entries}
        # document stays verbatim evidence; owner is the validated ultimate
        # owner on every cached slot (the real failure mode: the seal must
        # resolve via owner, not via the temp parent label).
        self.assertEqual(by_instance["bracket_a.stp-2"]["document"], "robot.SLDASM")
        self.assertEqual(
            by_instance["bracket_a.stp-2/3915_ASM_2_ASM.stp-1"]["document"], parent_doc
        )
        for entry in entries:
            self.assertEqual(entry["owner"], "robot.SLDASM")
            self.assertIn(entry["owner"], payload["graph"])
        self.assertEqual(len(payload["virtual_components"]), 1)
        virtual_entry = payload["virtual_components"][0]
        self.assertEqual(virtual_entry["state"], "embedded_virtual_component")
        self.assertEqual(virtual_entry["owner"], "robot.SLDASM")
        self.assertEqual(virtual_entry["owner_sha256"], files["robot.SLDASM"])
        self.assertEqual(virtual_entry["instance"], "Copy of 复件 (2) 零件10^waist-1")
        self.assertEqual(virtual_entry["configuration"], "默认")
        self.assertEqual(virtual_entry["session_document_path"], virtual_doc)
        self.assertIs(virtual_entry["is_virtual"], True)
        self.assertIsNone(by_instance["bracket_a.stp-2"]["owner_anchor"])
        self.assertEqual(
            by_instance["bracket_a.stp-2/3915_ASM_2_ASM.stp-1"]["owner_anchor"],
            "bracket_a.stp-2",
        )
        manifest = seal_closure_selection(files, "robot.SLDASM", payload, reader_sha256=READER)
        self.assertEqual(manifest["main_assembly"], "robot.SLDASM")
        self.assertEqual(sorted(manifest["files"]), ["arm.SLDPRT", "robot.SLDASM"])


class CuPreverifyTests(unittest.TestCase):
    """W6: the CU pre-verification (probe-v4 check set) refuses before native work."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.stage = Path(self._tmp.name).resolve()
        self.files = write_stage(
            self.stage,
            {"robot.SLDASM": b"robot-bytes", "arm.SLDPRT": b"arm-bytes"},
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def observe(self, **extra) -> dict:
        occurrence_rows = [occurrence("arm-1", str(self.stage / "arm.SLDPRT"))]
        raw = readings(self.stage, occurrence_rows, **extra)
        reader = NativeSelectionReader(walker=lambda stage, main, files_: raw, reader_sha256=READER)
        return reader.observe(self.stage, "robot.SLDASM", self.files)

    def test_complete_declarations_seal_a_preverify_pass_receipt(self) -> None:
        payload = self.observe()
        preverify = payload["preverify"]
        self.assertEqual(preverify["verdict"], "pass")
        self.assertTrue(preverify["closure_ok"])
        self.assertEqual(preverify["open_errors"], 0)
        self.assertEqual(preverify["declarations_missing"], [])
        self.assertEqual(preverify["declarations"]["dp.hardware_id"], "m3.0")
        self.assertEqual(preverify["declarations"]["dp.delivery_configuration"], "3.0 总装")

    def test_missing_declarations_are_recorded_and_never_refuse(self) -> None:
        payload = self.observe(identity_properties={"dp.hardware_id": "m3.0"})
        preverify = payload["preverify"]
        self.assertEqual(preverify["verdict"], "pass")
        self.assertEqual(set(preverify["declarations_missing"]), set(IDENTITY_DECLARATIONS) - {"dp.hardware_id"})
        self.assertEqual(preverify["declarations"]["dp.hardware_id"], "m3.0")

    def test_absent_delivery_configuration_keeps_the_native_saved_configuration(self) -> None:
        payload = self.observe(identity_properties={"dp.hardware_id": "m3.0"})
        recorded = payload["preverify"]["delivery_configuration"]
        self.assertIsNone(recorded["declared"])
        self.assertEqual(recorded["source"], "native_saved_configuration")

    def test_unusable_delivery_configuration_refuses(self) -> None:
        with self.assertRaises(SelectionError) as raised:
            self.observe(identity_properties={"dp.hardware_id": "m3.0", "dp.delivery_configuration": "   "})
        error = raised.exception
        self.assertEqual(error.code, "selection_configuration_invalid")
        problem = next(item for item in error.detail["problems"] if item["code"] == "selection_configuration_invalid")
        self.assertEqual(problem["configuration"], "   ")

    def test_unreadable_declaration_set_refuses_with_the_reason(self) -> None:
        with self.assertRaises(SelectionError) as raised:
            self.observe(
                identity_properties=None,
                identity_properties_error="CadError: custom properties unreadable",
            )
        error = raised.exception
        self.assertEqual(error.code, "selection_identity_unreadable")
        problem = next(item for item in error.detail["problems"] if item["code"] == "selection_identity_unreadable")
        self.assertEqual(problem["declarations_error"], "CadError: custom properties unreadable")


if __name__ == "__main__":
    unittest.main()
