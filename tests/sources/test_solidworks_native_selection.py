"""Native selection observations: staged bytes only, fail-closed refusals, sealed opens."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from . import _paths  # noqa: F401  (import side effect: sys.path)

from description_pipeline.sources.solidworks.errors import CadError  # noqa: E402
from description_pipeline.sources.solidworks.native import (  # noqa: E402
    SolidWorksBackend,
    normalize_document_path,
)
from description_pipeline.sources.solidworks.native_selection import (  # noqa: E402
    NativeSelectionReader,
)
from description_pipeline.sources.solidworks.selection import (  # noqa: E402
    SelectionError,
    selection_plan,
)

FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "native_selection_external_references.json"


def stage_fixture() -> dict:
    with open(FIXTURE_PATH, encoding="utf-8") as handle:
        return json.load(handle)


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


class FakeApplication:
    """Closed-document fake: exposes only the no-search query, records every call."""

    def __init__(self, documents: dict[str, list[tuple[str, str]]]):
        self.documents = documents
        self.calls: list[str] = []

    def GetDocumentDependencies2(self, path, traverse, search, read_only):  # noqa: N802
        assert (traverse, search, read_only) == (False, False, False)
        self.calls.append(path)
        flat: list[str] = []
        for name, location in self.documents.get(path, []):
            flat.extend((name, location))
        return tuple(flat)


def observe(application, stage, pending, upload_files):
    with NativeSelectionReader(application=application) as reader:
        return reader.observe(stage, pending, upload_files)


class NativeSelectionObservationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def path(self, name: str) -> str:
        return str(self.tmp.joinpath(*name.split("/")))

    def test_main_only_start_produces_empty_references(self) -> None:
        digests = write_stage(self.tmp, {"assembly.SLDASM": b"main-bytes"})
        application = FakeApplication({self.path("assembly.SLDASM"): []})
        observations = observe(application, self.tmp, digests, digests)
        self.assertEqual(observations, {"assembly.SLDASM": {"sha256": digests["assembly.SLDASM"], "references": []}})
        self.assertEqual(application.calls, [self.path("assembly.SLDASM")])

    def test_nested_references_are_canonical_and_only_pending_documents_are_queried(self) -> None:
        digests = write_stage(
            self.tmp,
            {"assembly.SLDASM": b"main", "parts/frame.SLDASM": b"frame", "parts/link.SLDPRT": b"link"},
        )
        application = FakeApplication(
            {
                self.path("assembly.SLDASM"): [("frame", "parts/frame.SLDASM")],
                self.path("parts/frame.SLDASM"): [("link", "link.SLDPRT")],
                self.path("parts/link.SLDPRT"): [],
            }
        )
        first = observe(application, self.tmp, {"assembly.SLDASM": digests["assembly.SLDASM"]}, digests)
        self.assertEqual(first["assembly.SLDASM"]["references"], ["parts/frame.SLDASM"])
        self.assertEqual(application.calls, [self.path("assembly.SLDASM")])

        second = observe(application, self.tmp, {"parts/frame.SLDASM": digests["parts/frame.SLDASM"]}, digests)
        self.assertEqual(second["parts/frame.SLDASM"]["references"], ["parts/link.SLDPRT"])
        self.assertEqual(application.calls[-1], self.path("parts/frame.SLDASM"))

        third = observe(application, self.tmp, {"parts/link.SLDPRT": digests["parts/link.SLDPRT"]}, digests)
        self.assertEqual(third, {"parts/link.SLDPRT": {"sha256": digests["parts/link.SLDPRT"], "references": []}})

    def test_unrequested_staged_documents_are_never_queried(self) -> None:
        digests = write_stage(
            self.tmp,
            {"assembly.SLDASM": b"main", "parts/frame.SLDASM": b"frame", "parts/link.SLDPRT": b"link"},
        )
        application = FakeApplication(
            {
                self.path("assembly.SLDASM"): [("frame", "parts/frame.SLDASM")],
                self.path("parts/frame.SLDASM"): [("link", "link.SLDPRT")],
            }
        )
        observe(application, self.tmp, {"assembly.SLDASM": digests["assembly.SLDASM"]}, digests)
        self.assertEqual(application.calls, [self.path("assembly.SLDASM")])

    def test_external_same_named_decoy_is_refused_with_verbatim_evidence(self) -> None:
        digests = write_stage(self.tmp, {"assembly.SLDASM": b"main", "parts/link.SLDPRT": b"decoy"})
        external = "/elsewhere/DesignTemp/link.SLDPRT"
        application = FakeApplication({self.path("assembly.SLDASM"): [("link", external)]})
        with self.assertRaises(SelectionError) as caught:
            observe(application, self.tmp, {"assembly.SLDASM": digests["assembly.SLDASM"]}, digests)
        error = caught.exception
        self.assertEqual(error.code, "selection_dependencies_refused")
        self.assertEqual(error.detail["count"], 1)
        problem = error.detail["problems"][0]
        self.assertEqual(problem["problem"], "outside_stage")
        self.assertEqual(problem["name"], "link")
        self.assertEqual(problem["location"], external)
        self.assertEqual(problem["document"], "assembly.SLDASM")
        self.assertNotIn("parts/link.SLDPRT", json.dumps(error.detail))
        self.assertEqual(application.calls, [self.path("assembly.SLDASM")])

    def test_external_reference_fixture_refuses_with_verbatim_evidence(self) -> None:
        fixture = stage_fixture()
        upload = dict(fixture["upload_files"])
        digests = write_stage(self.tmp, {"assembly.SLDASM": b"main"})
        upload["assembly.SLDASM"] = digests["assembly.SLDASM"]
        documents = {
            self.path("assembly.SLDASM"): [
                (entry["name"], entry["location"]) for entry in fixture["documents"]["assembly.SLDASM"]
            ]
        }
        application = FakeApplication(documents)
        with self.assertRaises(SelectionError) as caught:
            observe(application, self.tmp, {"assembly.SLDASM": digests["assembly.SLDASM"]}, upload)
        error = caught.exception
        self.assertEqual(error.code, "selection_dependencies_refused")
        self.assertEqual(error.detail["count"], 3)
        problems = {problem["problem"]: problem for problem in error.detail["problems"]}
        self.assertEqual(set(problems), {"outside_stage", "import_unsupported", "unsupported_type"})
        outside = problems["outside_stage"]
        self.assertEqual(outside["name"], "link")
        self.assertEqual(outside["location"], "C:\\Users\\example\\Desktop\\DesignTemp\\link.SLDPRT")
        imported = problems["import_unsupported"]
        self.assertEqual(imported["name"], "cover.step")
        self.assertEqual(
            imported["location"], "C:\\Users\\example\\AppData\\Local\\Temp\\swx42\\IC~~\\cover.step.SLDAPRT"
        )
        unsupported = problems["unsupported_type"]
        self.assertEqual((unsupported["name"], unsupported["location"]), ("notes", "docs\\notes.txt"))
        self.assertEqual(application.calls, [self.path("assembly.SLDASM")])

    def test_import_temp_path_is_not_reinterpreted_as_the_delivered_step(self) -> None:
        digests = write_stage(self.tmp, {"assembly.SLDASM": b"main"})
        upload = {**digests, "imports/cover.step": "4" * 64}
        application = FakeApplication(
            {
                self.path("assembly.SLDASM"): [
                    ("cover.step", "C:\\Users\\example\\AppData\\Local\\Temp\\swx42\\IC~~\\cover.step.SLDAPRT")
                ]
            }
        )
        with self.assertRaises(SelectionError) as caught:
            observe(application, self.tmp, {"assembly.SLDASM": digests["assembly.SLDASM"]}, upload)
        problem = caught.exception.detail["problems"][0]
        self.assertEqual(problem["problem"], "import_unsupported")
        self.assertNotIn("imports/cover.step", json.dumps(caught.exception.detail))

    def test_relative_and_absolute_in_stage_locations_resolve_to_one_reference(self) -> None:
        digests = write_stage(self.tmp, {"assembly.SLDASM": b"main", "parts/frame.SLDASM": b"frame"})
        application = FakeApplication(
            {
                self.path("assembly.SLDASM"): [
                    ("frame", "parts/frame.SLDASM"),
                    ("frame", self.path("parts/frame.SLDASM")),
                ]
            }
        )
        observations = observe(application, self.tmp, {"assembly.SLDASM": digests["assembly.SLDASM"]}, digests)
        self.assertEqual(observations["assembly.SLDASM"]["references"], ["parts/frame.SLDASM"])

    def test_escaping_relative_location_is_outside_the_stage(self) -> None:
        digests = write_stage(self.tmp, {"assembly.SLDASM": b"main"})
        application = FakeApplication({self.path("assembly.SLDASM"): [("evil", "../evil.SLDPRT")]})
        with self.assertRaises(SelectionError) as caught:
            observe(application, self.tmp, {"assembly.SLDASM": digests["assembly.SLDASM"]}, digests)
        problem = caught.exception.detail["problems"][0]
        self.assertEqual(problem["problem"], "outside_stage")
        self.assertEqual(problem["location"], "../evil.SLDPRT")

    def test_in_namespace_reference_missing_from_the_upload_index_is_refused(self) -> None:
        digests = write_stage(self.tmp, {"assembly.SLDASM": b"main"})
        application = FakeApplication({self.path("assembly.SLDASM"): [("extra", "extra.SLDPRT")]})
        with self.assertRaises(SelectionError) as caught:
            observe(application, self.tmp, {"assembly.SLDASM": digests["assembly.SLDASM"]}, digests)
        problem = caught.exception.detail["problems"][0]
        self.assertEqual(problem["problem"], "not_in_upload_index")
        self.assertEqual(problem["location"], "extra.SLDPRT")

    def test_non_native_in_namespace_reference_is_not_an_empty_leaf(self) -> None:
        digests = write_stage(self.tmp, {"assembly.SLDASM": b"main", "notes.txt": b"notes"})
        application = FakeApplication({self.path("assembly.SLDASM"): [("notes", "notes.txt")]})
        with self.assertRaises(SelectionError) as caught:
            observe(application, self.tmp, {"assembly.SLDASM": digests["assembly.SLDASM"]}, digests)
        problem = caught.exception.detail["problems"][0]
        self.assertEqual(problem["problem"], "unsupported_type")

    def test_index_case_collision_is_refused_before_any_query(self) -> None:
        digests = write_stage(self.tmp, {"assembly.SLDASM": b"main"})
        files = {**digests, "Parts/dup.SLDPRT": "5" * 64, "parts/DUP.SLDPRT": "6" * 64}
        application = FakeApplication({})
        with self.assertRaises(SelectionError) as caught:
            observe(application, self.tmp, {"assembly.SLDASM": digests["assembly.SLDASM"]}, files)
        self.assertEqual(caught.exception.code, "selection_path_collision")
        self.assertEqual(application.calls, [])


class NativeSelectionRefusalOrderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def path(self, name: str) -> str:
        return str(self.tmp.joinpath(*name.split("/")))

    def test_requested_document_must_be_indexed_staged_and_native(self) -> None:
        digests = write_stage(self.tmp, {"assembly.SLDASM": b"main", "imports/cover.step": b"step"})
        application = FakeApplication({})

        with self.assertRaises(SelectionError) as unknown:
            observe(application, self.tmp, {"absent.SLDASM": "9" * 64}, digests)
        self.assertEqual(unknown.exception.code, "selection_request_unknown")

        upload = {**digests, "parts/frame.SLDASM": "7" * 64}
        with self.assertRaises(SelectionError) as not_staged:
            observe(application, self.tmp, {"parts/frame.SLDASM": "7" * 64}, upload)
        self.assertEqual(not_staged.exception.code, "selection_document_not_staged")

        with self.assertRaises(SelectionError) as unsupported:
            observe(application, self.tmp, {"imports/cover.step": digests["imports/cover.step"]}, digests)
        self.assertEqual(unsupported.exception.code, "selection_request_unsupported")
        self.assertEqual(application.calls, [])

    def test_pending_digest_mismatch_refuses_before_reading(self) -> None:
        digests = write_stage(self.tmp, {"assembly.SLDASM": b"main"})
        application = FakeApplication({})
        with self.assertRaises(SelectionError) as caught:
            observe(application, self.tmp, {"assembly.SLDASM": "0" * 64}, digests)
        self.assertEqual(caught.exception.code, "selection_source_changed")
        self.assertEqual(application.calls, [])

    def test_staged_hash_drift_refuses_before_reading(self) -> None:
        digests = write_stage(self.tmp, {"assembly.SLDASM": b"staged-bytes"})
        recorded = {**digests, "assembly.SLDASM": sha256(b"other-bytes")}
        application = FakeApplication({})
        with self.assertRaises(SelectionError) as caught:
            observe(application, self.tmp, {"assembly.SLDASM": recorded["assembly.SLDASM"]}, recorded)
        self.assertEqual(caught.exception.code, "selection_source_changed")
        self.assertEqual(application.calls, [])

    def test_stage_without_real_bytes_is_refused(self) -> None:
        digests = write_stage(self.tmp, {"assembly.SLDASM": b"main"})
        application = FakeApplication({})

        # A digest map is metadata, not proof that bytes exist.
        with self.assertRaises(SelectionError) as mapped:
            observe(application, digests, {"assembly.SLDASM": digests["assembly.SLDASM"]}, digests)
        self.assertEqual(mapped.exception.code, "selection_stage_invalid")

        # A stage object with extra attributes is not a path either.
        fake = type("Stage", (), {"root": str(self.tmp), "files": {"absent.SLDASM": "8" * 64}})()
        with self.assertRaises(SelectionError) as attributed:
            observe(application, fake, {"assembly.SLDASM": digests["assembly.SLDASM"]}, digests)
        self.assertEqual(attributed.exception.code, "selection_stage_invalid")

        missing_root = self.tmp / "outside-stage"
        with self.assertRaises(SelectionError) as unreadable:
            observe(application, missing_root, {"assembly.SLDASM": digests["assembly.SLDASM"]}, digests)
        self.assertEqual(unreadable.exception.code, "selection_stage_invalid")
        self.assertEqual(application.calls, [])

    def test_stage_with_a_linked_parent_is_refused(self) -> None:
        real = self.tmp / "real"
        digests = write_stage(real, {"assembly.SLDASM": b"main"})
        link = self.tmp / "linked"
        os.symlink(real, link)
        application = FakeApplication({})
        with self.assertRaises(SelectionError) as caught:
            observe(application, link, {"assembly.SLDASM": digests["assembly.SLDASM"]}, digests)
        self.assertEqual(caught.exception.code, "selection_stage_invalid")
        self.assertEqual(application.calls, [])

    def test_pending_names_must_match_the_upload_index_exactly(self) -> None:
        digests = write_stage(self.tmp, {"assembly.SLDASM": b"main"})
        application = FakeApplication({})
        wrong_keys = ("./assembly.SLDASM", "Assembly.SLDASM", "/assembly.SLDASM", "assembly.SLDASM ")
        for key in (*wrong_keys, Path("assembly.SLDASM")):
            with self.subTest(key=str(key)), self.assertRaises(SelectionError) as caught:
                observe(application, self.tmp, {key: digests["assembly.SLDASM"]}, digests)
            self.assertEqual(caught.exception.code, "selection_request_unknown")
        self.assertEqual(application.calls, [])

    def test_bytes_mutated_during_the_query_refuse_without_observation(self) -> None:
        digests = write_stage(self.tmp, {"assembly.SLDASM": b"main"})
        staged = self.path("assembly.SLDASM")

        class MutatingApplication(FakeApplication):
            def GetDocumentDependencies2(self, path, traverse, search, read_only):  # noqa: N802
                Path(staged).write_bytes(b"mutated-during-query")
                return super().GetDocumentDependencies2(path, traverse, search, read_only)

        application = MutatingApplication({staged: [("frame", "parts/frame.SLDASM")]})
        upload = {**digests, "parts/frame.SLDASM": "a" * 64}
        with self.assertRaises(SelectionError) as caught:
            observe(application, self.tmp, {"assembly.SLDASM": digests["assembly.SLDASM"]}, upload)
        self.assertEqual(caught.exception.code, "selection_source_changed")


class NativeSelectionPlanIntegrationTests(unittest.TestCase):
    def test_observations_complete_a_pure_selection_plan(self) -> None:
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        digests = write_stage(
            tmp,
            {"assembly.SLDASM": b"main", "parts/frame.SLDASM": b"frame", "parts/link.SLDPRT": b"link"},
        )
        application = FakeApplication(
            {
                str(tmp / "assembly.SLDASM"): [("frame", "parts/frame.SLDASM")],
                str(tmp / "parts/frame.SLDASM"): [("link", "link.SLDPRT")],
                str(tmp / "parts/link.SLDPRT"): [],
            }
        )
        observations: dict[str, dict] = {}
        for _ in range(4):
            plan = selection_plan(digests, "assembly.SLDASM", observations)
            if plan["state"] == "complete":
                break
            observations.update(observe(application, tmp, plan["requested_files"], digests))
        plan = selection_plan(digests, "assembly.SLDASM", observations)
        self.assertEqual(plan["state"], "complete")
        self.assertEqual(set(plan["selected_files"]), set(digests))


class SealedOpenGuardTests(unittest.TestCase):
    def test_unsealed_source_open_is_refused_before_com(self) -> None:
        backend = SolidWorksBackend()
        self.assertIsNone(backend.sealed_documents())
        sealed = backend.seal_documents(["/stage/main.SLDASM"])
        self.assertEqual(backend.sealed_documents(), sealed)
        with self.assertRaises(CadError) as caught:
            backend.open_document("/stage/other.SLDASM")
        self.assertEqual(caught.exception.code, "document_not_sealed")
        self.assertEqual(caught.exception.detail["document"], "/stage/other.SLDASM")
        # A sealed path passes the gate and only then fails for the missing CAD runtime.
        with self.assertRaises(ImportError):
            backend.open_document("/stage/main.SLDASM")

    def test_seal_is_case_insensitive_and_reports_evidence(self) -> None:
        backend = SolidWorksBackend()
        sealed = backend.seal_documents(["/stage/Main.SLDASM"])
        self.assertEqual(sealed, [normalize_document_path(os.path.abspath("/stage/Main.SLDASM"))])
        with self.assertRaises(ImportError):
            backend.open_document("/stage/main.sldasm")

    def test_collected_copy_roots_stay_exempt(self) -> None:
        backend = SolidWorksBackend()
        backend._capture_roots = [normalize_document_path("/stage/copies")]
        backend.seal_documents(["/stage/main.SLDASM"])
        with self.assertRaises(ImportError):
            backend.open_document("/stage/copies/collected.SLDASM")

    def test_guard_is_disabled_without_a_sealed_set(self) -> None:
        backend = SolidWorksBackend()
        with self.assertRaises(ImportError):
            backend.open_document("/stage/anything.SLDASM")

    def test_seal_rejects_empty_documents(self) -> None:
        backend = SolidWorksBackend()
        with self.assertRaises(CadError) as caught:
            backend.seal_documents(["  "])
        self.assertEqual(caught.exception.code, "document_seal_invalid")

    def test_seal_rejects_relative_documents(self) -> None:
        backend = SolidWorksBackend()
        with self.assertRaises(CadError) as caught:
            backend.seal_documents(["stage/main.SLDASM"])
        self.assertEqual(caught.exception.code, "document_seal_invalid")

    def test_guard_rejection_never_reaches_the_cad_runtime(self) -> None:
        backend = SolidWorksBackend()
        backend.seal_documents(["/stage/main.SLDASM"])
        with (
            mock.patch("description_pipeline.sources.solidworks.native._win32") as win32,
            self.assertRaises(CadError),
        ):
            backend.open_document("/stage/other.SLDASM")
        win32.assert_not_called()


if __name__ == "__main__":
    unittest.main()
