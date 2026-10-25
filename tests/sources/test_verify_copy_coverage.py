"""Independent verification that the copied tree covers every selected retained source."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from typing import ClassVar

from description_pipeline.io import PipelineError, digest
from description_pipeline.schemas import NATIVE_DISCOVERY_SCHEMA
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
        """Bind every captured copy to the immutable prepared bytes."""

        root = self.root
        prepared = "C:\\prepared"
        originals = {"robot.SLDASM": MAIN, "shank.SLDPRT": SHANK, "imported_part.step": NP}
        copies = dict(originals)
        closure = {
            "schema_version": "solidworks-to-urdf.native-closure/v1",
            "method": "sealed_prepared_inventory",
            "source_dir": r"C:\capture\source",
            "top_level": r"C:\capture\source\robot.SLDASM",
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
        }
        if mutate is not None:
            mutate(root, closure, originals, copies)
        native = {name: _sha(data) for name, data in originals.items()}
        files_rows = [{"path": name, "sha256": checksum} for name, checksum in sorted(native.items())]
        discovery = {
            "schema_version": NATIVE_DISCOVERY_SCHEMA,
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

    def test_full_coverage_passes_with_unchanged_prepared_bytes(self) -> None:
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

    def test_changed_retained_copy_bytes_are_refused(self) -> None:
        for name in ("robot.SLDASM", "shank.SLDPRT", "imported_part.step"):
            with self.subTest(path=name):
                def mutate(root: Path, closure: dict, originals: dict, copies: dict, name=name) -> None:
                    changed = b"CHANGED-COPY"
                    copies[name] = changed
                    for row in closure["mapping"]:
                        if row["copy"] == "source/" + name:
                            row["copy_sha256"] = _sha(changed)
                    closure["copy_files"]["source/" + name] = _sha(changed)

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
