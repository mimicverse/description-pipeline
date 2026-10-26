"""Focused all-occurrence imported-provenance tests for native discovery (no CAD)."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from description_pipeline.io import digest
from description_pipeline.sources.solidworks.selection import seal_closure_selection
from description_pipeline.verification.native_discovery import DISCOVERY_SCHEMA, verify_discovery

from .selection_support import closure_evidence, sealed_selection
from . import support

ROOT = r"C:\description-v1-review\capture-session\input-copy"
TEMP = r"C:\Users\operator\AppData\Local\Temp\swx00001\IC~~"
IMPORT_API = "IComponent2.GetPathName/IComponent2.GetImportedPath/IComponent2.IsVirtual"
VIRTUAL_OWNER = "waist.SLDASM"
VIRTUAL_OCCURRENCE = "waist-1/Copy of 复件 (2) 零件10^waist-1"
VIRTUAL_CAPTURE_ROOT = r"C:\Users\operator\AppData\Local\Temp\swx21040"
VIRTUAL_SELECTION_ROOT = r"C:\Users\operator\AppData\Local\Temp\swx21041"
VIRTUAL_CAPTURE_DOCUMENT = (
    VIRTUAL_CAPTURE_ROOT + r"\VC~~\waist\Copy of 复件 (2) 零件10^waist.SLDPRT"
)
VIRTUAL_SELECTION_DOCUMENT = (
    VIRTUAL_SELECTION_ROOT + r"\VC~~\waist\Copy of 复件 (2) 零件10^waist.SLDPRT"
)


def canonical(files):
    return hashlib.sha256(
        (json.dumps(files, sort_keys=True, ensure_ascii=False, allow_nan=False, indent=2) + "\n").encode()
    ).hexdigest()


def accepted_provenance(document, sha):
    return {
        "state": "resolved",
        "source_root": ROOT,
        "native_document_path": rf"{TEMP}\{document}.SLDPRT",
        "used_api": "IComponent2.GetPathName/IComponent2.GetImportedPath/IComponent2.IsVirtual",
        "imported_path": ROOT + "\\" + document,
        "kind": "native_imported_document",
        "document": document,
        "sha256": sha,
        "is_virtual": False,
    }


def foreign_provenance():
    return {
        "state": "unread",
        "source_root": ROOT,
        "native_document_path": rf"{TEMP}\bracket_a.stp.SLDASM",
        "used_api": "IComponent2.GetPathName/IComponent2.GetImportedPath/IComponent2.IsVirtual",
        "imported_path": r"D:\design-copy\change01\bracket_a.stp",
        "kind": None,
        "document": "bracket_a.stp",
        "sha256": None,
        "is_virtual": None,
        "error": "ValueError: native imported source does not resolve to a file inside the delivered folder",
    }


def foreign_component(name, *, containing=None):
    return {
        "name2": name,
        "document": rf"{TEMP}\bracket_a.stp.SLDASM",
        "suppressed": False,
        "lightweight": False,
        "suppression_state": 2,
        "source_provenance": foreign_provenance(),
        **({"solver_observation": {"context": {"document": containing}}} if containing is not None else {}),
    }


def virtual_provenance(owner_sha, **overrides):
    provenance = {
        "state": "embedded_virtual_component",
        "kind": "native_embedded_virtual_component",
        "source_root": ROOT,
        "owner": VIRTUAL_OWNER,
        "owner_sha256": owner_sha,
        "occurrence": VIRTUAL_OCCURRENCE,
        "defining_anchor": None,
        "native_document_path": VIRTUAL_CAPTURE_DOCUMENT,
        "session_document_path": VIRTUAL_CAPTURE_DOCUMENT,
        "materialization_path": VIRTUAL_CAPTURE_DOCUMENT,
        "session_temp_root": VIRTUAL_CAPTURE_ROOT,
        "native_api_pid": 21040,
        "author_imported_path": None,
        "author_imported_error": None,
        "is_virtual": True,
        "used_api": IMPORT_API,
        "error": None,
    }
    provenance.update(overrides)
    return provenance


def virtual_component(owner_sha, **overrides):
    component = {
        "name2": VIRTUAL_OCCURRENCE,
        "document": VIRTUAL_CAPTURE_DOCUMENT,
        "configuration": "默认",
        "suppressed": False,
        "lightweight": False,
        "suppression_state": 2,
        "source_provenance": virtual_provenance(owner_sha),
    }
    component.update(overrides)
    return component


def virtual_slot(owner_sha, occurrence=VIRTUAL_OCCURRENCE, configuration="默认"):
    return {
        "document": VIRTUAL_OWNER,
        "owner": VIRTUAL_OWNER,
        "instance": occurrence,
        "owner_anchor": None,
        "state": "embedded_virtual_component",
        "binding": "owner_embedded_geometry",
        "kind": "native_embedded_virtual_component",
        "session_document_path": VIRTUAL_SELECTION_DOCUMENT,
        "session_temp_root": VIRTUAL_SELECTION_ROOT,
        "native_api_pid": 21041,
        "owner_sha256": owner_sha,
        "configuration": configuration,
        "suppression_state": 2,
        "is_virtual": True,
        "used_api": IMPORT_API,
        "author_imported_path": None,
        "author_imported_error": None,
    }


def virtual_carrier(owner_sha, virtual_components=None):
    return sealed_selection(
        {VIRTUAL_OWNER: owner_sha},
        VIRTUAL_OWNER,
        edges={VIRTUAL_OWNER: []},
        imports=[],
        virtual_components=list(virtual_components)
        if virtual_components is not None
        else [virtual_slot(owner_sha)],
    )


class ImportedSourceTests(unittest.TestCase):
    def _package(self, directory, contents, components, *, raw_files=None, extra_raw=None):
        package = Path(directory) / "delivery"
        contents = dict(contents)
        extra_raw = dict(extra_raw or {})
        explicit_selection = extra_raw.get("source_selection")
        if raw_files is None and isinstance(explicit_selection, dict):
            raw_files = dict(explicit_selection.get("files") or {})
        elif raw_files is None:
            main = next((name for name in contents if name.lower().endswith(".sldasm")), None)
            if main is None:
                main = "robot.SLDASM"
                contents[main] = b"synthetic frozen robot assembly"
        native = {}
        originals = {}
        for name, data in contents.items():
            native[name] = hashlib.sha256(data).hexdigest()
            originals[name] = data.decode("utf-8")
        raw_inventory = dict(native if raw_files is None else raw_files)
        source_selection = explicit_selection
        if not isinstance(source_selection, dict) and raw_inventory:
            main = next((name for name in raw_inventory if name.lower().endswith(".sldasm")), None)
            if main is not None:
                references = sorted(name for name in raw_inventory if name != main)
                source_selection = sealed_selection(
                    raw_inventory,
                    main,
                    edges={main: references, **{name: [] for name in references}},
                )
        payload = {
            "schema_version": DISCOVERY_SCHEMA,
            "contract": "native-discovery/v1",
            "run_id": "imported-source-test",
            "namespace": "dp",
            "handoff_sha256": (
                source_selection.get("handoff_sha256", "0" * 64)
                if isinstance(source_selection, dict)
                else "0" * 64
            ),
            "raw": {
                "components": components,
                "files": raw_inventory,
                "identity": {},
                "saved_configuration": "Default",
                "document_state": support.saved_document_state(),
                **({"source_selection": source_selection} if isinstance(source_selection, dict) else {}),
                **extra_raw,
            },
            "identity": {},
        }
        payload = support.frozen_discovery_payload(
            payload,
            files=native,
        )
        source = support.default_source_record(
            main_assembly=(
                str(source_selection["main_assembly"])
                if isinstance(source_selection, dict)
                else "robot.SLDASM"
            )
        )
        if isinstance(source_selection, dict):
            source["identity"]["handoff_digest"] = source_selection["upload_sha256"]
            source["source_revision_id"] = digest(source["identity"])
        return support.write_target_delivery(
            package,
            capture={},
            discovery=payload,
            source=source,
            originals=originals,
        )

    @staticmethod
    def _check(report):
        return next(item for item in report["checks"] if item["id"] == "discovery.imported_sources")

    @staticmethod
    def _unbound(report):
        return next(item for item in report["errors"] if item["code"] == "discovery.imported_source_unbound")

    def test_resealed_evidence_suppression_states_are_refused_by_verification(self) -> None:
        """Independently resealed raw evidence cannot hide an invalid suppression state."""

        content = b"frozen step bytes"
        sha = hashlib.sha256(content).hexdigest()
        baseline = {
            "name2": "imported_part.step-1",
            "document": "imported_part.step",
            "suppressed": False,
            "lightweight": False,
            "suppression_state": 2,
            "source_provenance": accepted_provenance("imported_part.step", sha),
        }
        with tempfile.TemporaryDirectory() as directory:
            package = self._package(directory, {"imported_part.step": content}, [json.loads(json.dumps(baseline))])
            report = verify_discovery(package)
            sealed_hash = hashlib.sha256((package / "evidence/discovery.json").read_bytes()).hexdigest()
        # The binding check stays first so the passed report keeps its sealed provenance hash.
        self.assertEqual(report["checks"][0]["id"], "discovery.binding")
        self.assertEqual(report["discovery_sha256"], sealed_hash)
        self.assertTrue(
            next(item for item in report["checks"] if item["id"] == "discovery.suppression_state")["passed"]
        )
        cases = {
            "missing_state": lambda item: item.pop("suppression_state"),
            "state5": lambda item: item.update(suppression_state=5),
            "unknown": lambda item: item.update(suppression_state=7),
            "boolean_state": lambda item: item.update(suppression_state=True),
            "flag_missing": lambda item: item.pop("lightweight"),
            "flag_nonbool": lambda item: item.update(lightweight=0),
            "flag_mismatch": lambda item: item.update(suppressed=True),
            "lightweight": lambda item: item.update(suppression_state=1, lightweight=True),
        }
        for label, mutate in cases.items():
            with self.subTest(case=label):
                component = json.loads(json.dumps(baseline))
                mutate(component)
                with tempfile.TemporaryDirectory() as directory:
                    package = self._package(directory, {"imported_part.step": content}, [component])
                    report = verify_discovery(package)
                failed = next(item for item in report["checks"] if item["id"] == "discovery.suppression_state")
                self.assertFalse(failed["passed"], label)
                self.assertIn(
                    failed["details"]["code"],
                    {"discovery.suppression_state", "discovery.suppression_lightweight"},
                )
                self.assertTrue(any(str(item["code"]).startswith("discovery.suppression") for item in report["errors"]))

    def test_resealed_malformed_raw_fails_precisely_without_a_crash(self) -> None:
        """A None raw record fails the mandatory binding check without crashing verification."""

        content = b"frozen step bytes"
        sha = hashlib.sha256(content).hexdigest()
        component = {
            "name2": "imported_part.step-1",
            "document": "imported_part.step",
            "suppressed": False,
            "lightweight": False,
            "suppression_state": 2,
            "source_provenance": accepted_provenance("imported_part.step", sha),
        }
        with tempfile.TemporaryDirectory() as directory:
            package = self._package(directory, {"imported_part.step": content}, [component])
            record_path = package / "evidence/discovery.json"
            payload = json.loads(record_path.read_bytes())
            payload["raw"] = None
            record_path.write_bytes(json.dumps(payload).encode())
            support.seal_delivery(package)
            report = verify_discovery(package)
        self.assertFalse(report["passed"])
        failed = next(item for item in report["checks"] if item["id"] == "discovery.binding")
        self.assertFalse(failed["passed"])
        self.assertEqual(failed["details"]["code"], "discovery.request")

    def test_resolved_frozen_source_class_is_preserved(self):
        content = b"frozen step bytes"
        sha = hashlib.sha256(content).hexdigest()
        component = {
            "name2": "imported_part.step-1",
            "document": "imported_part.step",
            "suppressed": False,
            "lightweight": False,
            "suppression_state": 2,
            "source_provenance": accepted_provenance("imported_part.step", sha),
        }
        with tempfile.TemporaryDirectory() as directory:
            package = self._package(directory, {"imported_part.step": content}, [component])
            report = verify_discovery(package)
        self.assertTrue(self._check(report)["passed"], self._check(report)["details"])
        self.assertNotIn("discovery.imported_source_unbound", {item["code"] for item in report["errors"]})

    def test_raw_files_omitting_the_document_are_refused_by_mandatory_binding(self):
        content = b"frozen step bytes"
        sha = hashlib.sha256(content).hexdigest()
        component = {
            "name2": "imported_part.step-1",
            "document": "imported_part.step",
            "suppressed": False,
            "lightweight": False,
            "suppression_state": 2,
            "source_provenance": accepted_provenance("imported_part.step", sha),
        }
        with tempfile.TemporaryDirectory() as directory:
            package = self._package(directory, {"imported_part.step": content}, [component], raw_files={})
            report = verify_discovery(package)
        self.assertIn("discovery.request", {item["code"] for item in report["errors"]})

    def test_foreign_imported_ancestor_under_selected_container_fails(self):
        contents = {"module.SLDASM": b"module bytes", "tool_part.SLDPRT": b"tool bytes"}
        ancestor = foreign_component("module-1", containing="module.SLDASM")
        child = {
            "name2": "module-1/tool_part",
            "document": "tool_part.SLDPRT",
            "suppressed": False,
            "lightweight": False,
            "suppression_state": 2,
        }
        with tempfile.TemporaryDirectory() as directory:
            package = self._package(directory, contents, [ancestor, child])
            report = verify_discovery(package)
        error = self._unbound(report)
        detail = error["detail"]
        self.assertEqual(detail["unbound"], 1, detail)
        self.assertEqual(detail["occurrences"], ["module-1"])
        sample = detail["samples"][0]
        self.assertEqual(sample["occurrence"], "module-1")
        self.assertEqual(sample["document"], rf"{TEMP}\bracket_a.stp.SLDASM")
        self.assertEqual(sample["containing_document"], "module.SLDASM")
        self.assertIn("IC~~", sample["native_path"])
        self.assertIn("design-copy", sample["imported_path"])
        self.assertIn("reserved IC~~/VC~~", sample["reason"])

    def test_all_unbound_occurrences_are_retained(self):
        contents = {"module.SLDASM": b"module bytes", "part.SLDPRT": b"part bytes"}
        first = foreign_component("module-1", containing="module.SLDASM")
        second = foreign_component("module-1/part", containing="module.SLDASM")
        with tempfile.TemporaryDirectory() as directory:
            package = self._package(directory, contents, [first, second])
            report = verify_discovery(package)
        detail = self._unbound(report)["detail"]
        self.assertEqual(detail["unbound"], 2)
        self.assertEqual(detail["occurrences"], ["module-1", "module-1/part"])
        self.assertEqual(len(detail["samples"]), 2)

    def test_resolved_class_still_requires_exact_provenance(self):
        content = b"frozen step bytes"
        sha = hashlib.sha256(content).hexdigest()
        cases = (
            ({"document": "other.step"}, "differs"),
            ({"document": "./imported_part.step"}, "canonical"),
            ({"is_virtual": True}, "virtual"),
            ({"sha256": "f" * 64}, "bound"),
            ({"imported_path": r"C:\elsewhere\imported_part.step"}, "frozen root"),
            ({"imported_path": r"D:\elsewhere\imported_part.step"}, "different drives"),
        )
        for patch, token in cases:
            provenance = accepted_provenance("imported_part.step", sha)
            provenance.update(patch)
            component_document = (
                "./imported_part.step" if patch.get("document") == "./imported_part.step" else "imported_part.step"
            )
            component = {
                "name2": "imported_part.step-1",
                "document": component_document,
                "suppressed": False,
                "lightweight": False,
                "suppression_state": 2,
                "source_provenance": provenance,
            }
            with tempfile.TemporaryDirectory() as directory:
                package = self._package(directory, {"imported_part.step": content}, [component])
                report = verify_discovery(package)
            with self.subTest(patch=patch):
                detail = self._unbound(report)["detail"]
                self.assertEqual(detail["unbound"], 1)
                self.assertIn(token, detail["samples"][0]["reason"])

    def test_raw_recorded_hash_must_agree_with_the_frozen_inventory(self):
        content = b"frozen step bytes"
        sha = hashlib.sha256(content).hexdigest()
        component = {
            "name2": "imported_part.step-1",
            "document": "imported_part.step",
            "suppressed": False,
            "lightweight": False,
            "suppression_state": 2,
            "source_provenance": accepted_provenance("imported_part.step", sha),
        }
        with tempfile.TemporaryDirectory() as directory:
            package = self._package(
                directory,
                {"imported_part.step": content},
                [component],
                raw_files={"imported_part.step": "e" * 64},
            )
            report = verify_discovery(package)
        self.assertIn("discovery.request", {item["code"] for item in report["errors"]})

    def test_embedded_virtual_component_is_accepted_through_the_sealed_carrier(self):
        content = b"virtual owner bytes"
        sha = hashlib.sha256(content).hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            package = self._package(
                directory,
                {VIRTUAL_OWNER: content},
                [virtual_component(sha)],
                extra_raw={"source_selection": virtual_carrier(sha)},
            )
            report = verify_discovery(package)
        check = self._check(report)
        self.assertTrue(check["passed"], check["details"])
        self.assertEqual(check["details"].get("embedded_virtual_components"), 1)
        self.assertEqual(check["details"].get("cached_materializations"), 0)

    def test_embedded_virtual_component_requires_exact_native_and_carrier_evidence(self):
        content = b"virtual owner bytes"
        sha = hashlib.sha256(content).hexdigest()
        ic_document = (
            VIRTUAL_CAPTURE_ROOT
            + r"\IC~~\waist\Copy of 复件 (2) 零件10^waist.SLDPRT"
        )
        outside_document = r"C:\other\swx21040\VC~~\waist\part.SLDPRT"
        wrong_root_document = (
            r"C:\Users\operator\AppData\Local\Temp\not-a-session\VC~~\waist\part.SLDPRT"
        )
        mixed_document = (
            VIRTUAL_CAPTURE_ROOT
            + r"\VC~~\IC~~\waist\Copy of 复件 (2) 零件10^waist.SLDPRT"
        )
        extra_directory_document = (
            VIRTUAL_CAPTURE_ROOT
            + r"\extra\VC~~\waist\Copy of 复件 (2) 零件10^waist.SLDPRT"
        )
        cases = {
            "absent_carrier": (
                virtual_component(sha),
                None,
                "no sealed embedded virtual component matches",
            ),
            "false_virtual": (
                virtual_component(sha, source_provenance=virtual_provenance(sha, is_virtual=False)),
                virtual_carrier(sha),
                "explicit True IsVirtual",
            ),
            "unknown_virtual": (
                virtual_component(sha, source_provenance=virtual_provenance(sha, is_virtual=None)),
                virtual_carrier(sha),
                "explicit True IsVirtual",
            ),
            "wrong_kind": (
                virtual_component(sha, source_provenance=virtual_provenance(sha, kind="native_import_materialization")),
                virtual_carrier(sha),
                "not an API-proven embedded virtual component",
            ),
            "wrong_api": (
                virtual_component(sha, source_provenance=virtual_provenance(sha, used_api="basename_guess")),
                virtual_carrier(sha),
                "not an API-proven embedded virtual component",
            ),
            "session_pid_mismatch": (
                virtual_component(
                    sha,
                    source_provenance=virtual_provenance(sha, native_api_pid=21041),
                ),
                virtual_carrier(sha),
                "root does not match its native API session",
            ),
            "ic_document": (
                virtual_component(
                    sha,
                    document=ic_document,
                    source_provenance=virtual_provenance(
                        sha,
                        native_document_path=ic_document,
                        materialization_path=ic_document,
                        session_document_path=ic_document,
                    ),
                ),
                virtual_carrier(sha),
                "not an exact VC~~",
            ),
            "outside_session_root": (
                virtual_component(
                    sha,
                    source_provenance=virtual_provenance(
                        sha,
                        native_document_path=outside_document,
                        materialization_path=outside_document,
                        session_document_path=outside_document,
                    ),
                ),
                virtual_carrier(sha),
                "not an exact VC~~",
            ),
            "missing_session_root": (
                virtual_component(sha, source_provenance=virtual_provenance(sha, session_temp_root=None)),
                virtual_carrier(sha),
                "not an exact VC~~",
            ),
            "wrong_session_root_shape": (
                virtual_component(
                    sha,
                    source_provenance=virtual_provenance(
                        sha,
                        native_document_path=wrong_root_document,
                        materialization_path=wrong_root_document,
                        session_document_path=wrong_root_document,
                        session_temp_root=r"C:\Users\operator\AppData\Local\Temp\not-a-session",
                    ),
                ),
                virtual_carrier(sha),
                "not an exact VC~~",
            ),
            "mixed_session_markers": (
                virtual_component(
                    sha,
                    source_provenance=virtual_provenance(
                        sha,
                        native_document_path=mixed_document,
                        materialization_path=mixed_document,
                        session_document_path=mixed_document,
                    ),
                ),
                virtual_carrier(sha),
                "not an exact VC~~",
            ),
            "marker_below_extra_directory": (
                virtual_component(
                    sha,
                    source_provenance=virtual_provenance(
                        sha,
                        native_document_path=extra_directory_document,
                        materialization_path=extra_directory_document,
                        session_document_path=extra_directory_document,
                    ),
                ),
                virtual_carrier(sha),
                "not an exact VC~~",
            ),
            "api_path_fields_disagree": (
                virtual_component(
                    sha,
                    source_provenance=virtual_provenance(
                        sha,
                        materialization_path=r"C:\other\swx21040\VC~~\waist\other.SLDPRT",
                    ),
                ),
                virtual_carrier(sha),
                "API path fields disagree",
            ),
            "missing_owner": (
                virtual_component(sha, source_provenance=virtual_provenance(sha, owner=None)),
                virtual_carrier(sha),
                "names no ultimate owner",
            ),
            "owner_sha_mismatch": (
                virtual_component(sha, source_provenance=virtual_provenance("f" * 64)),
                virtual_carrier(sha),
                "not bound to the sealed selection",
            ),
            "occurrence_mismatch": (
                virtual_component(sha, source_provenance=virtual_provenance(sha, occurrence="other-1")),
                virtual_carrier(sha),
                "no sealed embedded virtual component matches",
            ),
            "configuration_mismatch": (
                virtual_component(sha, configuration="Other"),
                virtual_carrier(sha),
                "no sealed embedded virtual component matches",
            ),
            "anchor_mismatch": (
                virtual_component(sha, source_provenance=virtual_provenance(sha, defining_anchor="other-1")),
                virtual_carrier(sha),
                "no sealed embedded virtual component matches",
            ),
            "author_path_present": (
                virtual_component(
                    sha,
                    source_provenance=virtual_provenance(
                        sha, author_imported_path=r"D:\author\Copy of 复件 (2) 零件10^waist.step"
                    ),
                ),
                virtual_carrier(sha),
                "cannot carry an imported path",
            ),
            "author_path_missing": (
                virtual_component(
                    sha,
                    source_provenance={
                        key: value
                        for key, value in virtual_provenance(sha).items()
                        if key != "author_imported_path"
                    },
                ),
                virtual_carrier(sha),
                "cannot carry an imported path",
            ),
            "author_path_unread": (
                virtual_component(
                    sha,
                    source_provenance=virtual_provenance(
                        sha, author_imported_error="RuntimeError: GetImportedPath failed"
                    ),
                ),
                virtual_carrier(sha),
                "imported-path absence is unproven",
            ),
            "virtual_marked_as_import": (
                virtual_component(
                    sha,
                    source_provenance=virtual_provenance(
                        sha,
                        state="cached_materialization",
                        kind="native_import_materialization",
                        is_virtual=False,
                    ),
                ),
                virtual_carrier(sha),
                "not a session temp materialization",
            ),
        }
        for label, (component, carrier, token) in cases.items():
            with self.subTest(case=label):
                with tempfile.TemporaryDirectory() as directory:
                    extra = {"source_selection": carrier} if carrier is not None else {}
                    package = self._package(
                        directory,
                        {VIRTUAL_OWNER: content},
                        [component],
                        extra_raw=extra,
                    )
                    report = verify_discovery(package)
                detail = self._unbound(report)["detail"]
                reasons = [item["reason"] for item in detail["samples"]]
                self.assertTrue(
                    any(token in str(reason) for reason in reasons)
                    or detail.get("unmatched_count"),
                    (label, detail),
                )

    def test_virtual_component_without_provenance_is_rejected(self):
        content = b"virtual owner bytes"
        sha = hashlib.sha256(content).hexdigest()
        for document in (
            VIRTUAL_CAPTURE_DOCUMENT,
            VIRTUAL_CAPTURE_ROOT + r"\VC~~\IC~~\mixed.SLDPRT",
        ):
            with self.subTest(document=document):
                component = {
                    "name2": VIRTUAL_OCCURRENCE,
                    "document": document,
                    "configuration": "默认",
                    "suppressed": False,
                    "lightweight": False,
                    "suppression_state": 2,
                }
                with tempfile.TemporaryDirectory() as directory:
                    package = self._package(
                        directory,
                        {VIRTUAL_OWNER: content},
                        [component],
                        extra_raw={"source_selection": virtual_carrier(sha)},
                    )
                    report = verify_discovery(package)
                detail = self._unbound(report)["detail"]
                self.assertEqual(
                    detail["samples"][0]["reason"],
                    "a session-document occurrence carries no native API provenance",
                )

    def test_resolved_provenance_cannot_use_reserved_session_path(self):
        document = "VC~~/IC~~/imported_part.step"
        content = b"reserved path bytes"
        sha = hashlib.sha256(content).hexdigest()
        component = {
            "name2": "reserved-1",
            "document": document,
            "configuration": "Default",
            "suppressed": False,
            "lightweight": False,
            "suppression_state": 2,
            "source_provenance": accepted_provenance(document, sha),
        }
        with tempfile.TemporaryDirectory() as directory:
            package = self._package(
                directory,
                {document: content},
                [component],
            )
            report = verify_discovery(package)
        detail = self._unbound(report)["detail"]
        self.assertIn("reserved IC~~/VC~~", detail["samples"][0]["reason"])

    def test_embedded_virtual_component_coverage_is_two_sided(self):
        content = b"virtual owner bytes"
        sha = hashlib.sha256(content).hexdigest()
        carrier = virtual_carrier(sha, virtual_components=[virtual_slot(sha), virtual_slot(sha, occurrence="other-1")])
        with tempfile.TemporaryDirectory() as directory:
            package = self._package(
                directory,
                {VIRTUAL_OWNER: content},
                [virtual_component(sha)],
                extra_raw={"source_selection": carrier},
            )
            report = verify_discovery(package)
        error = self._unbound(report)
        self.assertEqual(error["detail"]["unmatched_count"], 1)

    def test_nested_staged_child_must_keep_the_sealed_anchor(self):
        owner_bytes, child_bytes = b"virtual owner bytes", b"staged child bytes"
        owner_sha = hashlib.sha256(owner_bytes).hexdigest()
        child_sha = hashlib.sha256(child_bytes).hexdigest()
        nested_instance = VIRTUAL_OCCURRENCE + "/child-1"
        child = {
            "name2": nested_instance,
            "document": "child.SLDPRT",
            "configuration": "默认",
            "suppressed": False,
            "lightweight": False,
            "suppression_state": 2,
            "defining_anchor": VIRTUAL_OCCURRENCE,
        }
        files = {VIRTUAL_OWNER: owner_sha, "child.SLDPRT": child_sha}
        evidence = closure_evidence(
            files,
            VIRTUAL_OWNER,
            edges={VIRTUAL_OWNER: ["child.SLDPRT"], "child.SLDPRT": []},
            virtual_components=[virtual_slot(owner_sha)],
        )
        edge = next(
            row
            for row in evidence["documents"][VIRTUAL_OWNER]["resolved_edges"]
            if row["resolved"] == "child.SLDPRT"
        )
        edge.update(
            instance=nested_instance,
            owner_anchor=VIRTUAL_OCCURRENCE,
            defining_session_document=VIRTUAL_SELECTION_DOCUMENT,
        )
        carrier = seal_closure_selection(files, VIRTUAL_OWNER, evidence, reader_sha256="ab" * 32)
        with tempfile.TemporaryDirectory() as directory:
            package = self._package(
                directory,
                {VIRTUAL_OWNER: owner_bytes, "child.SLDPRT": child_bytes},
                [virtual_component(owner_sha), child],
                extra_raw={"source_selection": carrier},
            )
            report = verify_discovery(package)
        self.assertTrue(self._check(report)["passed"], self._check(report)["details"])

        broken = json.loads(json.dumps(child))
        broken["defining_anchor"] = None
        with tempfile.TemporaryDirectory() as directory:
            package = self._package(
                directory,
                {VIRTUAL_OWNER: owner_bytes, "child.SLDPRT": child_bytes},
                [virtual_component(owner_sha), broken],
                extra_raw={"source_selection": carrier},
            )
            report = verify_discovery(package)
        error = self._unbound(report)
        self.assertIn("defining anchor", error["detail"]["samples"][0]["reason"])

if __name__ == "__main__":
    unittest.main()
