"""The native-evidence kind gate reads the frozen snapshot manifest.

W1 b2b regression: ``_native`` was switched to read ``kind``/``evidence_class``
from the capture ``collection`` block, which never carried them.  The frozen
snapshot manifest (``evidence/manifest.json``) is the carrier, exactly as the
pre-switch ``source.integrity`` gate read it.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from description_pipeline.io import PipelineError, inventory, write_json
from description_pipeline.verification import solidworks_urdf as quality

from . import support


class NativeEvidenceKindTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="native-kind-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def _build(
        self,
        *,
        manifest_kind: str = "solidworks",
        manifest_class: str = "cad",
        identity_configuration: str | None = None,
        collection_kind: str = "fixture",
        collection_class: str = "fixture",
    ) -> None:
        root = self.root
        source = support.default_source_record()
        identity = source["identity"]
        if identity_configuration is not None:
            identity = {**identity, "configuration": identity_configuration}
        environment = {"solidworks": {"revision": "34.0.5045", "build": "fixture"}}
        (root / "input").mkdir(parents=True, exist_ok=True)
        (root / "evidence/raw").mkdir(parents=True, exist_ok=True)
        write_json(root / "input/source.json", source)
        write_json(
            root / "evidence/capture.json",
            {
                "schema_version": "solidworks-to-urdf.capture/v1",
                # The real capture block carries no kind/evidence_class; wrong values here
                # are ignored because the gate reads the frozen snapshot manifest.
                "collection": {
                    "kind": collection_kind,
                    "evidence_class": collection_class,
                    "identity": identity,
                    "environment": environment,
                    "limits": {"native_cad": True},
                },
            },
        )
        write_json(root / "evidence/native-runtime.json", environment)
        (root / "evidence/raw/scene_raw.json").write_text('{"scene": "raw"}', encoding="utf-8")
        write_json(
            root / "evidence/manifest.json",
            {
                "schema_version": "solidworks-to-urdf.source/v1",
                "kind": manifest_kind,
                "evidence_class": manifest_class,
                "identity": identity,
                "scene": "raw/scene_raw.json",
                "files": inventory(root / "evidence", exclude=("manifest.json",)),
            },
        )

    def _native(self) -> dict:
        return quality._native(self.root, quality.verify_snapshot(self.root / "evidence"))

    def test_snapshot_manifest_is_the_kind_authority(self) -> None:
        self._build()
        report = self._native()
        self.assertEqual(report["solidworks_revision"], "34.0.5045")
        self.assertEqual(report["configuration"], "Default")

    def test_manifest_identity_is_the_configuration_authority(self) -> None:
        self._build(identity_configuration="Manifest-Only")
        self.assertEqual(self._native()["configuration"], "Manifest-Only")

    def test_foreign_evidence_class_or_kind_is_refused(self) -> None:
        for kind, evidence_class in (("fixture", "fixture"), ("solidworks", "fixture")):
            with self.subTest(kind=kind, evidence_class=evidence_class):
                temporary = tempfile.TemporaryDirectory(prefix="native-kind-neg-")
                self.addCleanup(temporary.cleanup)
                self.root = Path(temporary.name)
                self._build(manifest_kind=kind, manifest_class=evidence_class)
                with self.assertRaisesRegex(PipelineError, "A release requires native SolidWorks evidence"):
                    self._native()


if __name__ == "__main__":
    unittest.main()
