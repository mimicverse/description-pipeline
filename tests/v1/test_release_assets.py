"""The six-asset release gate: existence, digests and no seventh asset."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from description_pipeline.io import PipelineError
from tests.v1.test_distribution import complete_release
from tools.check_release_assets import check


class ReleaseAssetGateTests(unittest.TestCase):
    def fixture(self, root: Path) -> None:
        identity = complete_release(root)
        self.assertIsInstance(identity, dict)

    def test_the_six_assets_pass_with_bound_digests(self) -> None:
        with tempfile.TemporaryDirectory(prefix="release-assets-") as temporary:
            root = Path(temporary)
            self.fixture(root)
            result = check(root)
            self.assertTrue(result["passed"])
            self.assertEqual(result["assets"], 6)
            self.assertEqual(len(result["artifacts"]), 5)
            self.assertEqual(len(result["release_json_sha256"]), 64)

    def test_a_missing_asset_is_refused(self) -> None:
        with tempfile.TemporaryDirectory(prefix="release-assets-") as temporary:
            root = Path(temporary)
            self.fixture(root)
            (archive,) = root.glob("description-*-windows-cp312-x86_64.zip")
            archive.unlink()
            with self.assertRaises(PipelineError):
                check(root)

    def test_a_byte_tampered_asset_is_refused(self) -> None:
        with tempfile.TemporaryDirectory(prefix="release-assets-") as temporary:
            root = Path(temporary)
            self.fixture(root)
            (archive,) = root.glob("description-*-linux-cp312-x86_64.zip")
            archive.write_bytes(archive.read_bytes() + b"tampered")
            with self.assertRaises(PipelineError):
                check(root)

    def test_a_seventh_asset_is_refused(self) -> None:
        with tempfile.TemporaryDirectory(prefix="release-assets-") as temporary:
            root = Path(temporary)
            self.fixture(root)
            shutil.copyfile(root / "release.json", root / "SHA256SUMS.json")
            with self.assertRaises(PipelineError):
                check(root)

    def test_a_missing_manifest_is_refused(self) -> None:
        with tempfile.TemporaryDirectory(prefix="release-assets-") as temporary:
            root = Path(temporary)
            self.fixture(root)
            (root / "release.json").unlink()
            with self.assertRaises(SystemExit):
                check(root)


if __name__ == "__main__":
    unittest.main()
