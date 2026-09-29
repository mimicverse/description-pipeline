"""The snapshot manifest is the integrity boundary every source provider shares.

`verify_snapshot` is what stops a half-copied or edited frozen source from being treated as evidence,
and coverage showed none of its rejections were executed.  Each one is written here, next to the
round trip that has to keep working.
"""

import json
import tempfile
import unittest
from pathlib import Path

from description_pipeline.io import PipelineError, file_digest, write_json
from description_pipeline.sources.snapshot import SCHEMA, load_scene, verify_snapshot, write_manifest

IDENTITY = {"provider": "fixture", "revision": "fixture-1"}


def build(root: Path, *, scene: object = None, extra: dict[str, str] | None = None) -> dict:
    """A complete snapshot: scene.json plus whatever the case declares."""

    (root / "scene.json").write_text(json.dumps({"links": []} if scene is None else scene), encoding="utf-8")
    for name, text in (extra or {}).items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    return write_manifest(root, kind="fixture", identity=IDENTITY, evidence_class="fixture")


class WriteManifestTests(unittest.TestCase):
    def test_a_snapshot_round_trips(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = build(root, extra={"parts/base.stl": "binary"})
            self.assertEqual(manifest["schema_version"], SCHEMA)
            self.assertEqual(verify_snapshot(root), manifest)
            self.assertEqual(load_scene(root), {"links": []})
            self.assertIn("parts/base.stl", manifest["files"])

    def test_invalid_inputs_are_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "scene.json").write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(PipelineError, "Unknown evidence class"):
                write_manifest(root, kind="fixture", identity=IDENTITY, evidence_class="guess")
            for label, kind, identity in (
                ("missing kind", "", IDENTITY),
                ("missing identity", "fixture", {}),
            ):
                with self.subTest(case=label), self.assertRaisesRegex(PipelineError, "kind and identity"):
                    write_manifest(root, kind=kind, identity=identity, evidence_class="fixture")


class VerifySnapshotTests(unittest.TestCase):
    def snapshot(self, root: Path, manifest: dict) -> Path:
        write_json(root / "manifest.json", manifest)
        return root

    def test_each_rejection_is_a_diagnostic(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "scene.json").write_text("{}", encoding="utf-8")
            scene_hash = file_digest(root / "scene.json")
            complete = {
                "schema_version": SCHEMA,
                "kind": "fixture",
                "identity": IDENTITY,
                "evidence_class": "fixture",
                "scene": "scene.json",
                "files": {"scene.json": scene_hash},
            }
            cases: dict[str, tuple[dict, str]] = {
                "unsupported schema": ({**complete, "schema_version": "other/v1"}, "Unsupported source manifest"),
                "invalid evidence class": ({**complete, "evidence_class": "guess"}, "evidence class is missing"),
                "cad with a non-native kind": (
                    {**complete, "evidence_class": "cad", "kind": "fixture"},
                    "CAD evidence requires a supported native source kind",
                ),
                "missing identity": ({**complete, "identity": {}}, "source identity is missing"),
                "no declared files": ({**complete, "files": {}}, "no declared files"),
                "changed file": ({**complete, "files": {"scene.json": "0" * 64}}, "incomplete or changed"),
            }
            for label, (manifest, fragment) in cases.items():
                with self.subTest(case=label):
                    self.snapshot(root, manifest)
                    with self.assertRaises(PipelineError) as raised:
                        verify_snapshot(root)
                    self.assertIn(fragment, str(raised.exception))

    def test_a_scene_that_is_not_declared_is_refused(self):
        """The manifest has to list the scene it points at, even when its file list matches the tree."""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "other.txt").write_text("x", encoding="utf-8")
            write_json(
                root / "manifest.json",
                {
                    "schema_version": SCHEMA,
                    "kind": "fixture",
                    "identity": IDENTITY,
                    "evidence_class": "fixture",
                    "scene": "scene.json",
                    "files": {"other.txt": file_digest(root / "other.txt")},
                },
            )
            with self.assertRaisesRegex(PipelineError, "scene is not bound by manifest"):
                verify_snapshot(root)

    def test_a_scene_that_is_not_an_object_is_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "scene.json").write_text("[1, 2]", encoding="utf-8")
            write_json(
                root / "manifest.json",
                {
                    "schema_version": SCHEMA,
                    "kind": "fixture",
                    "identity": IDENTITY,
                    "evidence_class": "fixture",
                    "scene": "scene.json",
                    "files": {"scene.json": file_digest(root / "scene.json")},
                },
            )
            self.assertEqual(verify_snapshot(root)["kind"], "fixture")
            with self.assertRaisesRegex(PipelineError, "must be an object"):
                load_scene(root)


if __name__ == "__main__":
    unittest.main()
