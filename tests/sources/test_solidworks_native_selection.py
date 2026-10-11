"""Native closure admission: staged-inventory walk, fail-closed refusals, seal alignment."""

from __future__ import annotations

import hashlib
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
    NativeSelectionReader,
)
from description_pipeline.sources.solidworks.selection import (  # noqa: E402
    SelectionError,
    seal_selection,
    selection_plan,
)

READER = "a" * 64


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


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
    virtual=None,
    imported=None,
    name=None,
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
    return record


def readings(stage: Path, occurrences: list[dict], *, stored=None, **extra) -> dict:
    value = {
        "stage_root": str(stage),
        "occurrences": occurrences,
        "stored_references": stored or {},
        "search": {
            "folder_type": 0,
            "evidence": [
                {
                    "pid": 111,
                    "before": [],
                    "pinned": [str(stage)],
                    "observed_after_pin": [str(stage)],
                }
            ],
        },
        "open": {"errors": 0, "warnings": 0},
        "session": {"native_api_pid": 111, "solidworks_revision": "34.0.0"},
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
                {"name": "arm", "location": r"C:\Users\Mi\Desktop\M3.0\arm.SLDPRT"},
                {"name": "base", "location": r"C:\Users\Mi\Desktop\M3.0\base.SLDPRT"},
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
        self.assertEqual(payload["search_configuration"]["evidence"][0]["pid"], 111)
        # The produced graph is exactly B's seal contract.
        plan = selection_plan(self.files, "robot.SLDASM", payload["graph"])
        self.assertEqual(plan["state"], "complete")
        manifest = seal_selection(
            self.files,
            "robot.SLDASM",
            payload["graph"],
            selected_files={name: self.files[name] for name in payload["graph"]},
            reader_sha256=READER,
        )
        self.assertEqual(manifest["handoff_sha256"], plan["upload_sha256"])
        self.assertEqual(manifest["excluded_files"], {})

    def test_excluded_lists_unresolved_staged_files(self) -> None:
        payload = self.observe([occurrence("arm-1", self.resolved("arm.SLDPRT"))])
        self.assertEqual(payload["excluded"], ["base.SLDPRT"])

    def test_outside_resolution_refused_with_verbatim_paths(self) -> None:
        with self.assertRaises(SelectionError) as caught:
            self.observe([occurrence("arm-1", r"C:\Users\Mi\Desktop\M3.0\arm.SLDPRT")])
        error = caught.exception
        self.assertEqual(error.code, "selection_resolved_outside_stage")
        item = error.detail["problems"][0]
        self.assertEqual(
            sorted(item),
            ["code", "document", "instance", "message", "resolved", "stored_location"],
        )
        self.assertEqual(item["instance"], "arm-1")
        self.assertEqual(item["resolved"], r"C:\Users\Mi\Desktop\M3.0\arm.SLDPRT")
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

    def test_bound_import_admitted_by_documented_evidence(self) -> None:
        extra = write_stage(self.stage, {"NP-F550.step": b"np-bytes"})
        files = {**self.files, **extra}
        temp = r"C:\Users\Mi\AppData\Local\Temp\swx39388\IC~~\NP-F550.step.SLDPRT"
        payload = self.observe(
            [
                occurrence(
                    "NP-F550.step-1",
                    temp,
                    virtual=True,
                    imported=str(self.stage / "NP-F550.step"),
                )
            ],
            files=files,
        )
        self.assertEqual(payload["graph"]["robot.SLDASM"]["references"], ["NP-F550.step"])
        self.assertIn("NP-F550.step", payload["graph"])
        self.assertEqual(payload["imports"][0]["state"], "bound")
        self.assertEqual(payload["imports"][0]["sha256"], files["NP-F550.step"])
        edge = payload["documents"]["robot.SLDASM"]["resolved_edges"][0]
        self.assertEqual(edge["kind"], "native_imported_document")
        self.assertEqual(edge["temp_document"] if "temp_document" in edge else edge["resolved_path"], temp)

    def test_unbound_import_refused_with_needed_evidence(self) -> None:
        temp = r"C:\Users\Mi\AppData\Local\Temp\swx39388\IC~~\NP-F550.step.SLDPRT"
        with self.assertRaises(SelectionError) as caught:
            self.observe(
                [
                    occurrence(
                        "NP-F550.step-1",
                        temp,
                        virtual=True,
                        imported=r"C:\Users\Mi\Desktop\NP-F550.step",
                    )
                ]
            )
        error = caught.exception
        self.assertEqual(error.code, "selection_import_unsupported")
        item = error.detail["problems"][0]
        self.assertEqual(item["imported_path"], r"C:\Users\Mi\Desktop\NP-F550.step")
        self.assertEqual(item["temp_document"], temp)
        self.assertIn("GetImportedPath", item["needed_evidence"])

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
        self.assertEqual(payload["suppressed"], [
            {
                "document": "robot.SLDASM",
                "instance": "cover-3",
                "name": "cover-3",
                "suppression_state": 0,
                "resolved_path": r"C:\elsewhere\cover.SLDPRT",
            }
        ])
        self.assertNotIn("cover.SLDPRT", payload["graph"])

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


if __name__ == "__main__":
    unittest.main()
