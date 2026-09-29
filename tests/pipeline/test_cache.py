import errno
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from description_pipeline.build.cache import ENTRIES, restore, save


class GenerationCacheTests(unittest.TestCase):
    def test_concurrent_identical_writers_keep_a_complete_restorable_entry(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, cache, target = root / "source", root / "cache", root / "restored"
            for entry in ENTRIES:
                path = source / entry
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(entry)
            save(cache, "same-input", source)
            # A second writer already generating this key must tolerate the first.
            save(cache, "same-input", source)
            self.assertTrue(restore(cache, "same-input", target))
            self.assertTrue(all((target / entry).read_text(encoding="utf-8") == entry for entry in ENTRIES))
            self.assertEqual([path.name for path in cache.iterdir()], ["same-input"])

    def test_disk_errors_are_not_mistaken_for_a_concurrent_writer(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with (
                patch("description_pipeline.build.cache._copy"),
                patch("description_pipeline.build.cache.os.rename", side_effect=OSError(errno.ENOSPC, "full")),
                self.assertRaises(OSError),
            ):
                save(root / "cache", "key", root)
