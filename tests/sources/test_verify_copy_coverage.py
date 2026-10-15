"""Independent verification that the copied tree covers every selected retained source."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from typing import ClassVar

from description_pipeline.io import PipelineError, digest
from description_pipeline.verification.solidworks_urdf import _dependencies

from . import support

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
        """The target tree: the frozen originals under input/cad, the closure
        copies (possibly engine-rewritten) under evidence/source.

        Originals stay in the platform's frozen upload storage; their identity
        is the handoff upload index and the discovery inventory, never an
        equality with the (possibly engine-rewritten) copy bytes.
        """

        root = self.root
        prepared = "C:\\prepared"
        rewritten_main, rewritten_shank = b"MAIN-REWRITTEN", b"SHANK-REWRITTEN"
        copies = {"robot.SLDASM": rewritten_main, "shank.SLDPRT": rewritten_shank, "imported_part.step": NP}
        originals = {"robot.SLDASM": MAIN, "shank.SLDPRT": SHANK, "imported_part.step": NP}
        closure = {
            "declared_unresolved": [],
            "configuration": "Default",
            "original_files": {prepared + "\\" + name: _sha(data) for name, data in originals.items()},
            "mapping": [
                {
                    "source": prepared + "\\" + name,
                    "source_sha256": _sha(originals[name]),
                    "copy": "source/" + name,
                    "copy_sha256": _sha(copies[name]),
                    "instance": instance,
                }
                for name, instance in (
                    ("robot.SLDASM", "top_level"),
                    ("shank.SLDPRT", "shank-1"),
                    ("imported_part.step", "import_source"),
                )
            ],
            "copy_files": {"source/" + name: _sha(data) for name, data in copies.items()},
            "suppressed_instances": 0,
            "stale_stored_references": [],
            "seeded_required_leaves": [prepared + "\\imported_part.step"],
        }
        if mutate is not None:
            mutate(root, closure, originals, copies)
        native = {name: _sha(data) for name, data in originals.items()}
        files_rows = [{"path": name, "sha256": checksum} for name, checksum in sorted(native.items())]
        discovery = {
            "schema_version": "solidworks-to-urdf.native-discovery/v1",
            "run_id": "copy-coverage",
            "contract": "native-discovery/v1",
            "namespace": "dp",
            "robot_name": "robot",
            "files": files_rows,
            "native_inventory_sha256": digest(files_rows),
            "derived": {},
            "raw": {
                "files": dict(native),
                "source_selection": {
                    "files": {name: _sha(data) for name, data in originals.items()},
                    "closure": {
                        "stage_root": "C:\\stage",
                        "documents": {"shank.SLDPRT": {"stored_references": [], "resolved_edges": []}},
                    },
                },
                "selection": {"main_assembly": "robot.SLDASM"},
            },
        }
        handoff = {
            "package_files": {
                **{"cad/" + name: _sha(data) for name, data in originals.items()},
                "source.json": _sha(b"FROZEN-SOURCE-RECORD"),
            },
            "input_receipt": {"inventory": {}},
            "main_assembly": "robot.SLDASM",
            "configuration": "Default",
        }
        support.write_target_delivery(
            root,
            capture={
                "dependency_closure": closure,
                "collection": {
                    "capture": {
                        "originals_unchanged": {
                            "files_checked": len(closure["original_files"]),
                            "states_recorded": len(closure["original_files"]),
                            "state_scope": "initial_source_observation",
                        }
                    }
                },
            },
            discovery=discovery,
            handoff=handoff,
            originals={name: data.decode("utf-8") for name, data in originals.items()},
        )
        source_tree = root / "evidence/source"
        source_tree.mkdir(parents=True, exist_ok=True)
        for name, data in copies.items():
            path = source_tree / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        support.seal_delivery(root)

    def _drop_np_everywhere(self, root: Path, closure: dict, originals: dict, copies: dict) -> None:
        copies.pop("imported_part.step")
        closure["original_files"].pop(
            next(key for key in closure["original_files"] if key.endswith("imported_part.step"))
        )
        closure["mapping"] = [row for row in closure["mapping"] if not row["copy"].endswith("imported_part.step")]
        closure["copy_files"].pop("source/imported_part.step")

    def test_full_coverage_passes_with_rewritten_native_bytes(self) -> None:
        self._build()
        _dependencies(self.root, self.definition)

    def _root_with_package_files(self, package_files: dict) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="copy-coverage-main-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self._build()
        path = self.root / "input/handoff.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        document["package_files"] = package_files
        path.write_text(json.dumps(document), encoding="utf-8")

    def test_unreferenced_similar_basename_never_shadows_the_real_main(self) -> None:
        """notrobot.SLDASM in the upload index must not stand in for robot.SLDASM."""

        decoy = _sha(b"UNREFERENCED-OTHER-ASSEMBLY")
        real = {
            "cad/robot.SLDASM": _sha(MAIN),
            "cad/shank.SLDPRT": _sha(SHANK),
            "cad/imported_part.step": _sha(NP),
        }
        for label, package_files in (
            ("before", {"cad/notrobot.SLDASM": decoy, **real}),
            ("after", {**real, "cad/notrobot.SLDASM": decoy}),
        ):
            with self.subTest(position=label):
                self._root_with_package_files(package_files)
                _dependencies(self.root, self.definition)

    def test_main_key_binds_by_the_exact_frozen_relative_path(self) -> None:
        real = {
            "cad/robot.SLDASM": _sha(MAIN),
            "cad/shank.SLDPRT": _sha(SHANK),
            "cad/imported_part.step": _sha(NP),
        }
        # The exact request key wins even beside a boundary twin.
        self._root_with_package_files({**real, "cad/sub/robot.SLDASM": _sha(b"TWIN")})
        _dependencies(self.root, self.definition)
        # A nested key never substitutes the frozen path, even with the same
        # bytes (no suffix fallback may rewrite the frozen main-assembly key).
        self._root_with_package_files(
            {"cad/sub/robot.SLDASM": _sha(MAIN), "cad/shank.SLDPRT": _sha(SHANK), "cad/imported_part.step": _sha(NP)}
        )
        with self.assertRaises(PipelineError):
            _dependencies(self.root, self.definition)
        # A nested key with different bytes is refused as well.
        self._root_with_package_files(
            {
                "cad/sub/robot.SLDASM": _sha(b"DECOY"),
                "cad/shank.SLDPRT": _sha(SHANK),
                "cad/imported_part.step": _sha(NP),
            }
        )
        with self.assertRaises(PipelineError):
            _dependencies(self.root, self.definition)
        # A missing main-assembly entry is refused outright.
        self._root_with_package_files({"cad/shank.SLDPRT": _sha(SHANK), "cad/imported_part.step": _sha(NP)})
        with self.assertRaises(PipelineError):
            _dependencies(self.root, self.definition)

    def test_seeded_leaves_must_be_covered_and_selected(self) -> None:
        def uncovered_seed(root: Path, closure: dict, originals: dict, copies: dict) -> None:
            closure["seeded_required_leaves"].append(str(root / "prepared" / "ghost.SLDASM"))

        def unselected_seed(root: Path, closure: dict, originals: dict, copies: dict) -> None:
            decoy = b"DECOY"
            copies["decoy.SLDASM"] = decoy
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

        def malformed_seed(root: Path, closure: dict, originals: dict, copies: dict) -> None:
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
        def absent(root: Path, closure: dict, originals: dict, copies: dict) -> None:
            closure.pop("seeded_required_leaves")

        self._build(absent)
        _dependencies(self.root, self.definition)
        for label, value in (("empty_mapping", {}), ("empty_string", ""), ("zero", 0)):
            with self.subTest(case=label):
                temporary = tempfile.TemporaryDirectory(prefix="copy-coverage-falsy-")
                self.addCleanup(temporary.cleanup)
                self.root = Path(temporary.name)
                self._build(
                    lambda root, closure, originals, copies, value=value: closure.update(
                        seeded_required_leaves=value
                    )
                )
                with self.assertRaises(PipelineError):
                    _dependencies(self.root, self.definition)

    def test_swapped_native_original_hashes_are_refused(self) -> None:
        def mutate(root: Path, closure: dict, originals: dict, copies: dict) -> None:
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
        def mutate(root: Path, closure: dict, originals: dict, copies: dict) -> None:
            closure["mapping"] = [row for row in closure["mapping"] if not row["copy"].endswith("imported_part.step")]

        self._build(mutate)
        with self.assertRaises(PipelineError):
            _dependencies(self.root, self.definition)

    def test_changed_retained_non_native_bytes_are_refused(self) -> None:
        def mutate(root: Path, closure: dict, originals: dict, copies: dict) -> None:
            changed = b"NP-STEP-CHANGED"
            copies["imported_part.step"] = changed
            for row in closure["mapping"]:
                if row["copy"].endswith("imported_part.step"):
                    row["copy_sha256"] = _sha(changed)
            closure["copy_files"]["source/imported_part.step"] = _sha(changed)

        self._build(mutate)
        with self.assertRaises(PipelineError):
            _dependencies(self.root, self.definition)

    def test_selected_native_omitted_consistently_is_refused(self) -> None:
        def mutate(root: Path, closure: dict, originals: dict, copies: dict) -> None:
            copies.pop("shank.SLDPRT")
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
