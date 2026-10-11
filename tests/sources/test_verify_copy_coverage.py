"""Independent verification that the copied tree covers every selected retained source."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from typing import ClassVar

from description_pipeline.io import PipelineError, write_json
from description_pipeline.verification.solidworks_urdf import _dependencies

MAIN, SHANK, NP = b"MAIN", b"SHANK-ORIG", b"NP-STEP-BYTES"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class CopyCoverageTests(unittest.TestCase):
    """A selected retained source omitted from the copy graph must refuse."""

    definition: ClassVar[dict] = {"source": {"assembly": "robot.SLDASM", "configuration": "Default"}}

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="copy-coverage-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def _build(self, mutate=None) -> None:
        root = self.root
        (root / "input/discovery").mkdir(parents=True)
        (root / "evidence/raw").mkdir(parents=True)
        (root / "evidence/evidence").mkdir(parents=True)
        (root / "evidence/source").mkdir(parents=True)
        (root / "input/robot.SLDASM").write_bytes(MAIN)
        (root / "input/shank.SLDPRT").write_bytes(SHANK)
        (root / "input/NP-F550.step").write_bytes(NP)
        native = {"robot.SLDASM": _sha(MAIN), "shank.SLDPRT": _sha(SHANK), "NP-F550.step": _sha(NP)}
        discovery = {
            "native_files": native,
            "raw": {
                "files": dict(native),
                "source_selection": {
                    "files": dict(native),
                    "closure": {
                        "stage_root": "C:\\stage",
                        "documents": {"shank.SLDPRT": {"stored_references": [], "resolved_edges": []}},
                    },
                },
                "selection": {"main_assembly": "robot.SLDASM"},
            },
        }
        write_json(root / "input/discovery/native-discovery.json", discovery)
        record_bytes = (root / "input/discovery/native-discovery.json").read_bytes()
        canonical = hashlib.sha256(
            (json.dumps(native, sort_keys=True, ensure_ascii=False, allow_nan=False, indent=2) + "\n").encode()
        ).hexdigest()
        (root / "input/robot.yaml").write_text(
            json.dumps({"provenance": {"native_inventory_sha256": canonical, "discovery_sha256": _sha(record_bytes)}})
        )
        prepared = str(root / "prepared")
        rewritten_main, rewritten_shank = b"MAIN-REWRITTEN", b"SHANK-REWRITTEN"
        (root / "evidence/source/robot.SLDASM").write_bytes(rewritten_main)
        (root / "evidence/source/shank.SLDPRT").write_bytes(rewritten_shank)
        closure = {
            "declared_unresolved": [],
            "configuration": "Default",
            "original_files": {
                prepared + "\\robot.SLDASM": _sha(MAIN),
                prepared + "\\shank.SLDPRT": _sha(SHANK),
                prepared + "\\NP-F550.step": _sha(NP),
            },
            "mapping": [
                {
                    "source": prepared + "\\robot.SLDASM",
                    "source_sha256": _sha(MAIN),
                    "copy": "source/robot.SLDASM",
                    "copy_sha256": _sha(rewritten_main),
                    "instance": "top_level",
                },
                {
                    "source": prepared + "\\shank.SLDPRT",
                    "source_sha256": _sha(SHANK),
                    "copy": "source/shank.SLDPRT",
                    "copy_sha256": _sha(rewritten_shank),
                    "instance": "shank-1",
                },
                {
                    "source": prepared + "\\NP-F550.step",
                    "source_sha256": _sha(NP),
                    "copy": "source/NP-F550.step",
                    "copy_sha256": _sha(NP),
                    "instance": "import_source",
                },
            ],
            "copy_files": {
                "source/robot.SLDASM": _sha(rewritten_main),
                "source/shank.SLDPRT": _sha(rewritten_shank),
                "source/NP-F550.step": _sha(NP),
            },
            "suppressed_instances": 0,
            "stale_stored_references": [],
            "seeded_required_leaves": [prepared + "\\NP-F550.step"],
        }
        (root / "evidence/source/NP-F550.step").write_bytes(NP)
        if mutate is not None:
            mutate(root, closure)
        write_json(root / "evidence/raw/dependency_closure.json", closure)
        write_json(
            root / "evidence/evidence/collection.json",
            {
                "dependency_closure": closure,
                "capture": {"originals_unchanged": {"files_checked": len(closure["original_files"])}},
            },
        )

    def _drop_np_everywhere(self, root: Path, closure: dict) -> None:
        (root / "evidence/source/NP-F550.step").unlink()
        closure["original_files"].pop(next(key for key in closure["original_files"] if key.endswith("NP-F550.step")))
        closure["mapping"] = [row for row in closure["mapping"] if not row["copy"].endswith("NP-F550.step")]
        closure["copy_files"].pop("source/NP-F550.step")

    def test_full_coverage_passes_with_rewritten_native_bytes(self) -> None:
        self._build()
        _dependencies(self.root, self.definition)

    def test_seeded_leaves_must_be_covered_and_selected(self) -> None:
        def uncovered_seed(root: Path, closure: dict) -> None:
            closure["seeded_required_leaves"].append(str(root / "prepared" / "ghost.SLDASM"))

        def unselected_seed(root: Path, closure: dict) -> None:
            decoy = b"DECOY"
            (root / "evidence/source/decoy.SLDASM").write_bytes(decoy)
            path = str(root / "prepared" / "decoy.SLDASM")
            closure["original_files"][path] = _sha(decoy)
            closure["mapping"].append(
                {
                    "source": path,
                    "source_sha256": _sha(decoy),
                    "copy": "source/decoy.SLDASM",
                    "copy_sha256": _sha(decoy),
                    "instance": "decoy-1",
                }
            )
            closure["copy_files"]["source/decoy.SLDASM"] = _sha(decoy)
            closure["seeded_required_leaves"].append(path)

        def malformed_seed(root: Path, closure: dict) -> None:
            closure["seeded_required_leaves"] = [""]

        for label, mutate in (
            ("uncovered_seed", uncovered_seed),
            ("unselected_seed", unselected_seed),
            ("malformed_seed", malformed_seed),
        ):
            with self.subTest(case=label):
                temporary = tempfile.TemporaryDirectory(prefix="copy-coverage-mut-")
                self.addCleanup(temporary.cleanup)
                self.root = Path(temporary.name)
                self._build(mutate)
                with self.assertRaises(PipelineError):
                    _dependencies(self.root, self.definition)

    def test_falsy_or_absent_seeded_field_policy(self) -> None:
        def absent(root: Path, closure: dict) -> None:
            closure.pop("seeded_required_leaves")

        self._build(absent)
        _dependencies(self.root, self.definition)
        for label, value in (("empty_mapping", {}), ("empty_string", ""), ("zero", 0)):
            with self.subTest(case=label):
                temporary = tempfile.TemporaryDirectory(prefix="copy-coverage-falsy-")
                self.addCleanup(temporary.cleanup)
                self.root = Path(temporary.name)
                self._build(lambda root, closure, value=value: closure.update(seeded_required_leaves=value))
                with self.assertRaises(PipelineError):
                    _dependencies(self.root, self.definition)

    def test_swapped_native_original_hashes_are_refused(self) -> None:
        def mutate(root: Path, closure: dict) -> None:
            main_key = next(key for key in closure["original_files"] if key.endswith("robot.SLDASM"))
            shank_key = next(key for key in closure["original_files"] if key.endswith("shank.SLDPRT"))
            closure["original_files"][main_key] = _sha(SHANK)
            closure["original_files"][shank_key] = _sha(MAIN)
            for row in closure["mapping"]:
                if row["source"] == main_key:
                    row["source_sha256"] = _sha(SHANK)
                elif row["source"] == shank_key:
                    row["source_sha256"] = _sha(MAIN)

        self._build(mutate)
        with self.assertRaises(PipelineError):
            _dependencies(self.root, self.definition)

    def test_selected_retained_source_omitted_everywhere_is_refused(self) -> None:
        self._build(self._drop_np_everywhere)
        with self.assertRaises(PipelineError):
            _dependencies(self.root, self.definition)

    def test_mapping_missing_the_retained_source_is_refused(self) -> None:
        def mutate(root: Path, closure: dict) -> None:
            closure["mapping"] = [row for row in closure["mapping"] if not row["copy"].endswith("NP-F550.step")]

        self._build(mutate)
        with self.assertRaises(PipelineError):
            _dependencies(self.root, self.definition)

    def test_changed_retained_non_native_bytes_are_refused(self) -> None:
        def mutate(root: Path, closure: dict) -> None:
            changed = b"NP-STEP-CHANGED"
            (root / "evidence/source/NP-F550.step").write_bytes(changed)
            for row in closure["mapping"]:
                if row["copy"].endswith("NP-F550.step"):
                    row["copy_sha256"] = _sha(changed)
            closure["copy_files"]["source/NP-F550.step"] = _sha(changed)

        self._build(mutate)
        with self.assertRaises(PipelineError):
            _dependencies(self.root, self.definition)

    def test_selected_native_omitted_consistently_is_refused(self) -> None:
        def mutate(root: Path, closure: dict) -> None:
            (root / "evidence/source/shank.SLDPRT").unlink()
            closure["original_files"].pop(
                next(key for key in closure["original_files"] if key.endswith("shank.SLDPRT"))
            )
            closure["mapping"] = [row for row in closure["mapping"] if not row["copy"].endswith("shank.SLDPRT")]
            closure["copy_files"].pop("source/shank.SLDPRT")

        self._build(mutate)
        with self.assertRaises(PipelineError):
            _dependencies(self.root, self.definition)


if __name__ == "__main__":
    unittest.main()
