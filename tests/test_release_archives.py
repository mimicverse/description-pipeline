"""Release artifacts must be byte-identical when they are rebuilt from the same commit.

The builders upstream are not deterministic: setuptools stamps wheels with the time of the build and
tar records ownership and mtimes.  These tests pin the normalization that removes that variance, so
a reviewer can rebuild a release and compare digests instead of trusting the upload.
"""

import io
import tarfile
import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory

from description_pipeline.build.archive import normalize_sdist, normalize_zip, write_zip


def build_zip(path: Path, entries: list[tuple[str, bytes]], date_time: tuple[int, int, int, int, int, int]) -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries:
            archive.writestr(zipfile.ZipInfo(name, date_time=date_time), data)


def build_sdist(path: Path, entries: list[tuple[str, bytes, int]], *, mtime: int, uid: int, gzip_mtime: int) -> None:
    """Write a tarball whose ownership and timestamps depend on where it was built."""

    with tarfile.open(path, "w:gz", format=tarfile.PAX_FORMAT) as archive:
        for name, data, mode in entries:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = mode
            info.mtime = mtime
            info.uid = info.gid = uid
            info.uname = info.gname = f"builder{uid}"
            archive.addfile(info, io.BytesIO(data))
    # ``tarfile`` writes the current time into the gzip header itself.
    with path.open("r+b") as stream:
        stream.seek(4)
        stream.write(gzip_mtime.to_bytes(4, "little"))


class DeterministicZipTests(unittest.TestCase):
    def test_normalized_wheels_are_byte_identical(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            first, second = root / "first.whl", root / "second.whl"
            build_zip(
                first,
                [("pkg/__init__.py", b"value = 1\n"), ("pkg/data.json", b"{}\n")],
                (2026, 1, 2, 3, 4, 6),
            )
            build_zip(
                second,
                [("pkg/data.json", b"{}\n"), ("pkg/__init__.py", b"value = 1\n")],
                (2026, 9, 22, 16, 8, 4),
            )
            normalize_zip(first)
            normalize_zip(second)
            self.assertEqual(first.read_bytes(), second.read_bytes())
            with zipfile.ZipFile(first) as archive:
                self.assertEqual(archive.namelist(), ["pkg/__init__.py", "pkg/data.json"])
                self.assertEqual({info.date_time for info in archive.infolist()}, {(1980, 1, 1, 0, 0, 0)})
                self.assertEqual(archive.read("pkg/data.json"), b"{}\n")

    def test_write_zip_sorts_names_and_keeps_contents(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "bundle.zip"
            write_zip(path, [("b.txt", b"second"), ("a.txt", b"first")])
            with zipfile.ZipFile(path) as archive:
                self.assertEqual(archive.namelist(), ["a.txt", "b.txt"])
                self.assertEqual(archive.read("a.txt"), b"first")


class DeterministicSdistTests(unittest.TestCase):
    def test_normalized_sdists_are_byte_identical(self):
        entries = [
            ("demo-1.0/PKG-INFO", b"Name: demo\n", 0o644),
            ("demo-1.0/setup.cfg", b"[metadata]\n", 0o644),
            ("demo-1.0/tool.sh", b"#!/bin/sh\n", 0o755),
        ]
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            first, second = root / "first.tar.gz", root / "second.tar.gz"
            build_sdist(first, entries, mtime=1_700_000_000, uid=1000, gzip_mtime=1_700_000_001)
            build_sdist(
                second,
                list(reversed(entries)),
                mtime=1_790_000_000,
                uid=501,
                gzip_mtime=1_790_000_002,
            )
            self.assertNotEqual(first.read_bytes(), second.read_bytes())
            normalize_sdist(first)
            normalize_sdist(second)
            self.assertEqual(first.read_bytes(), second.read_bytes())
            with tarfile.open(first, "r:gz") as archive:
                names = archive.getnames()
                self.assertEqual(names, sorted(names))
                for info in archive.getmembers():
                    self.assertEqual((info.uid, info.gid, info.uname, info.gname, info.mtime), (0, 0, "", "", 0))
                executable = archive.getmember("demo-1.0/tool.sh")
                self.assertEqual(executable.mode, 0o755)
                self.assertEqual(archive.getmember("demo-1.0/PKG-INFO").mode, 0o644)
                member_stream = archive.extractfile("demo-1.0/PKG-INFO")
                self.assertIsNotNone(member_stream)
                self.assertEqual(member_stream.read() if member_stream is not None else b"", b"Name: demo\n")


if __name__ == "__main__":
    unittest.main()
