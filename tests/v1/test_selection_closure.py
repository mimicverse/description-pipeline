"""Selection closure contract: sealing and one-batch provisional transport."""

from __future__ import annotations

import hashlib
import tempfile
import unittest
import zipfile
from pathlib import Path

from description_pipeline.io import digest, inventory
from description_pipeline.orchestration.selection_transport import StagedSelection
from description_pipeline.sources.solidworks.selection import (
    SelectionError,
    seal_closure_selection,
    verify_selection,
)

from tests.sources.selection_support import closure_evidence, sealed_selection

UPLOAD_NAMES = {
    "cad/robot.SLDASM": b"main bytes",
    "cad/base.SLDPRT": b"base bytes",
    "cad/arm.SLDPRT": b"arm bytes",
    "notes.txt": b"notes bytes",
}
MAIN = "cad/robot.SLDASM"


class ClosureSealContractTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="selection-closure-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.upload = {name: hashlib.sha256(data).hexdigest() for name, data in UPLOAD_NAMES.items()}
        self.stage_root = str(self.root / "stage")

    def _evidence(self, **kwargs) -> dict:
        kwargs.setdefault("stage_root", self.stage_root)
        kwargs.setdefault(
            "edges",
            {MAIN: ["cad/base.SLDPRT", "cad/arm.SLDPRT"], "cad/base.SLDPRT": [], "cad/arm.SLDPRT": []},
        )
        return closure_evidence(self.upload, MAIN, **kwargs)

    def test_seal_preserves_all_input_bytes_independently_of_object_reachability(self) -> None:
        manifest = sealed_selection(
            self.upload,
            MAIN,
            edges={MAIN: ["cad/base.SLDPRT", "cad/arm.SLDPRT"], "cad/base.SLDPRT": [], "cad/arm.SLDPRT": []},
            stage_root=self.stage_root,
        )
        self.assertEqual(manifest["files"], self.upload)
        self.assertEqual(manifest["upload_sha256"], digest(self.upload))
        self.assertNotEqual(manifest["upload_sha256"], manifest["selection_sha256"])
        self.assertEqual(manifest["closure"]["stage_root"], self.stage_root)
        verified = verify_selection(manifest, self.upload, manifest["files"], reader_sha256=manifest["reader_sha256"])
        self.assertEqual(verified, manifest)
        with self.assertRaises(SelectionError):
            verify_selection(
                {**manifest, "selection_sha256": "00" * 32},
                self.upload,
                manifest["files"],
                reader_sha256=manifest["reader_sha256"],
            )

    def test_closure_refusals_keep_the_evidence_structural(self) -> None:
        def drop_edge(closure) -> None:
            closure["documents"][MAIN]["resolved_edges"].pop()

        cases = {
            "outside_index": lambda closure: closure["graph"][MAIN]["references"].append("other.SLDAPRT"),
            "repeated_edge": lambda closure: closure["graph"][MAIN]["references"].append("cad/base.SLDPRT"),
            "sha_mismatch": lambda closure: closure["graph"]["cad/base.SLDPRT"].update(sha256="00" * 32),
            "missing_observation": lambda closure: closure["graph"].pop("cad/arm.SLDPRT"),
            "upload_mismatch": lambda closure: closure.update(upload_sha256="00" * 32),
            "reader_mismatch": lambda closure: closure.update(reader_sha256="00" * 32),
            "missing_stage_root": lambda closure: closure.pop("stage_root"),
            "staged_sha_mismatch": lambda closure: closure["documents"]["cad/base.SLDPRT"].update(
                staged_sha256="00" * 32
            ),
            "search_not_restored": lambda closure: closure["search_configuration"].update(restored=False),
            "search_not_pinned": lambda closure: closure["search_configuration"].update(pinned=["/elsewhere"]),
            "search_invented_folder_type": lambda closure: closure["search_configuration"].update(
                folder_type={"name": "swDocumentType", "value": 0}
            ),
            "session_open": lambda closure: closure["session"].update(closed=False),
            "open_status_bad": lambda closure: closure.update(open_status={"errors": "many", "warnings": 0}),
            "open_status_unsupported": lambda closure: closure.update(
                open_status={"errors": 1, "warnings": 0}
            ),
            "open_status_forged": lambda closure: closure.update(
                open_status={
                    "errors": 2,
                    "warnings": 0,
                    "classification": "clean_open",
                }
            ),
            "coherence_edge_missing": drop_edge,
            "edge_unknown_kind": lambda closure: closure["documents"][MAIN]["resolved_edges"][0].update(kind="guessed"),
            "edge_without_instance": lambda closure: closure["documents"][MAIN]["resolved_edges"][0].update(
                instance="  "
            ),
            "edge_state_suppressed": lambda closure: closure["documents"][MAIN]["resolved_edges"][0].update(
                suppression_state=0
            ),
            "edge_state_not_integer": lambda closure: closure["documents"][MAIN]["resolved_edges"][0].update(
                suppression_state="2"
            ),
            "edge_lightweight_incoherent": lambda closure: closure["documents"][MAIN]["resolved_edges"][0].update(
                suppression_state=1, lightweight=False
            ),
            "edge_path_outside_stage": lambda closure: closure["documents"][MAIN]["resolved_edges"][0].update(
                resolved_path="C:\\elsewhere\\cad\\base.SLDPRT"
            ),
            "edge_path_other_target": lambda closure: closure["documents"][MAIN]["resolved_edges"][0].update(
                resolved_path=closure["stage_root"].rstrip("\\/") + "\\cad\\arm.SLDPRT"
            ),
            "suppressed_unjustified": lambda closure: closure["suppressed"].append(
                {
                    "document": MAIN,
                    "instance": "cover-1",
                    "suppression_state": 0,
                    "resolved_path": closure["stage_root"].rstrip("\\/") + "\\cad\\base.SLDPRT",
                }
            ),
            "import_state_unknown": lambda closure: closure["imports"].append(
                {"document": MAIN, "instance": "hd-1", "state": "unbound", "resolved": "cad/base.SLDPRT"}
            ),
            "import_outside_graph": lambda closure: closure["imports"].append(
                {
                    "document": "cad/other.SLDASM",
                    "instance": "hd-1",
                    "state": "cached_materialization",
                    "binding": "author_metadata_only",
                    "materialization": "C:\\Temp\\swx1\\IC~~\\hd.step.SLDPRT",
                    "session_temp_root": "C:\\Temp\\swx1",
                    "native_api_pid": 1,
                }
            ),
            "import_cached_not_metadata": lambda closure: closure["imports"].append(
                {
                    "document": MAIN,
                    "instance": "hd-1",
                    "state": "cached_materialization",
                    "binding": "proven",
                    "materialization": "C:\\Temp\\IC~~\\hd.step.SLDPRT",
                }
            ),
            "import_bound_sha_mismatch": lambda closure: closure["imports"].append(
                {
                    "document": MAIN,
                    "instance": "hd-1",
                    "state": "bound",
                    "resolved": "cad/base.SLDPRT",
                    "sha256": "00" * 32,
                }
            ),
            "import_owner_sha_mismatch": lambda closure: closure["imports"].append(
                {
                    "document": MAIN,
                    "instance": "hd-1",
                    "state": "cached_materialization",
                    "binding": "author_metadata_only",
                    "materialization": "C:\\Temp\\IC~~\\hd.step.SLDPRT",
                    "owner_sha256": "00" * 32,
                }
            ),
        }
        for label, mutate in cases.items():
            with self.subTest(case=label):
                evidence = self._evidence()
                mutate(evidence)
                with self.assertRaises(SelectionError):
                    seal_closure_selection(self.upload, MAIN, evidence, reader_sha256="ab" * 32)

    def test_suppressed_placements_and_cached_imports_stay_evidence_only(self) -> None:
        evidence = self._evidence(
            suppressed=[
                {
                    "document": MAIN,
                    "instance": "cover-1",
                    "name": "cover-1",
                    "suppression_state": 0,
                    "resolved_path": self.stage_root + "\\cad\\base.SLDPRT",
                    "justification": "authored_suppression_clean_open",
                }
            ],
            imports=[
                {
                    "document": MAIN,
                    "instance": "hd-1",
                    "occurrence": "hd-1",
                    "state": "cached_materialization",
                    "kind": "native_import_materialization",
                    "binding": "author_metadata_only",
                    "owner": MAIN,
                    "owner_anchor": None,
                    "defining_anchor": None,
                    "configuration": "Default",
                    "is_virtual": False,
                    "used_api": "IComponent2.GetPathName/IComponent2.GetImportedPath/IComponent2.IsVirtual",
                    "materialization": "C:\\Temp\\swx1\\IC~~\\hd.step.SLDPRT",
                    "session_temp_root": "C:\\Temp\\swx1",
                    "native_api_pid": 1,
                    "author_imported_path": "D:\\old\\hd.step",
                    "owner_sha256": self.upload[MAIN],
                    "session_source_sha256": digest(
                        {
                            "kind": "native_import_materialization",
                            "state": "cached_materialization",
                            "owner": MAIN,
                            "owner_sha256": self.upload[MAIN],
                            "occurrence": "hd-1",
                            "defining_anchor": None,
                            "configuration": "Default",
                            "is_virtual": False,
                            "used_api": "IComponent2.GetPathName/IComponent2.GetImportedPath/IComponent2.IsVirtual",
                        }
                    ),
                },
                {
                    "document": MAIN,
                    "instance": "hd-2",
                    "state": "bound",
                    "resolved": "cad/base.SLDPRT",
                    "sha256": self.upload["cad/base.SLDPRT"],
                },
            ],
        )
        evidence["warnings"] = [
            {
                "code": "import_cached_materialization",
                "document": MAIN,
                "instance": "hd-1",
                "needs_absence_test": True,
            }
        ]
        manifest = seal_closure_selection(self.upload, MAIN, evidence, reader_sha256="ab" * 32)
        self.assertEqual(manifest["files"], self.upload)
        self.assertEqual(manifest["closure"]["suppressed"][0]["justification"], "authored_suppression_clean_open")
        self.assertEqual(manifest["closure"]["imports"][0]["binding"], "author_metadata_only")
        self.assertEqual(manifest["closure"]["imports"][1]["state"], "bound")
        self.assertEqual(manifest["closure"]["warnings"][0]["code"], "import_cached_materialization")

    def test_embedded_virtual_component_survives_the_sealed_transport(self) -> None:
        session_root = str(self.root / "swx21040")
        session_document = session_root + "/VC~~/waist/Copy of 复件 (2) 零件10^waist.SLDPRT"
        slot = {
            "document": MAIN,
            "owner": MAIN,
            "owner_anchor": None,
            "owner_sha256": self.upload[MAIN],
            "instance": "waist-1/Copy of 复件 (2) 零件10^waist-1",
            "state": "embedded_virtual_component",
            "binding": "owner_embedded_geometry",
            "kind": "native_embedded_virtual_component",
            "session_document_path": session_document,
            "session_temp_root": session_root,
            "native_api_pid": 21040,
            "configuration": "默认",
            "suppression_state": 2,
            "is_virtual": True,
            "used_api": "IComponent2.GetPathName/IComponent2.GetImportedPath/IComponent2.IsVirtual",
            "author_imported_path": None,
            "author_imported_error": None,
        }
        evidence = self._evidence(virtual_components=[slot])
        evidence["warnings"] = [
            {
                "code": "embedded_virtual_component_geometry",
                "document": MAIN,
                "instance": slot["instance"],
                "requires_owner_binding": True,
            }
        ]
        manifest = seal_closure_selection(self.upload, MAIN, evidence, reader_sha256="ab" * 32)
        self.assertEqual(manifest["closure"]["virtual_components"], [slot])
        self.assertEqual(
            verify_selection(
                manifest,
                self.upload,
                manifest["files"],
                reader_sha256=manifest["reader_sha256"],
            ),
            manifest,
        )
        session, archive = self._staged_session()
        self.assertTrue(session.accept_batch(archive))
        sealed = session.seal_closure(evidence)
        self.assertEqual(sealed["closure"]["virtual_components"], [slot])

    def _staged_session(self):
        session = StagedSelection(self.root / "stage", self.upload, MAIN, reader_sha256="ab" * 32)
        archive = self.root / "batch.zip"
        with zipfile.ZipFile(archive, "x", zipfile.ZIP_DEFLATED) as sink:
            for name, data in UPLOAD_NAMES.items():
                sink.writestr(name, data)
        return session, archive

    def test_single_provisional_batch_seals_complete_input_with_snapshot_proof(self) -> None:
        session, archive = self._staged_session()
        self.assertEqual(set(session.snapshot()), {"state", "reader_sha256", "upload_sha256", "staged_files"})
        self.assertTrue(session.accept_batch(archive))
        self.assertFalse(session.accept_batch(archive))
        self.assertEqual(session.snapshot()["state"], "reading")
        self.assertEqual(session.snapshot()["staged_files"], self.upload)
        manifest = session.seal_closure(self._evidence())
        self.assertEqual(session.snapshot()["state"], "sealed")
        self.assertEqual(session.snapshot()["staged_files"], self.upload)
        self.assertEqual(session.snapshot()["manifest"], manifest)
        self.assertEqual(inventory(session.stage), manifest["files"])

    def test_transport_refuses_a_tampered_stage_and_a_wrong_index(self) -> None:
        session, archive = self._staged_session()
        self.assertTrue(session.accept_batch(archive))
        (session.stage / "cad/base.SLDPRT").write_bytes(b"tampered")
        with self.assertRaises(SelectionError):
            session.seal_closure(self._evidence())
        broken = self.root / "broken.zip"
        with zipfile.ZipFile(broken, "x", zipfile.ZIP_DEFLATED) as sink:
            sink.writestr(MAIN, UPLOAD_NAMES[MAIN])
        fresh = StagedSelection(self.root / "stage2", self.upload, MAIN, reader_sha256="ab" * 32)
        with self.assertRaises(SelectionError):
            fresh.accept_batch(broken)
        self.assertEqual(inventory(fresh.stage), {})


if __name__ == "__main__":
    unittest.main()
