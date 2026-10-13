#!/usr/bin/env python3
"""Seal/open the controlled backup archive: one tar stream through aes-256-cbc.

The key file lives under the state root (0600) and never leaves it; the sealed archive is the
single artifact the promotion manifest binds, and ``open`` reproduces the sealed bytes exactly.

Exit codes: 0 success; 1 error (message on stderr); 2 usage error.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path


def die(message: str) -> None:
    raise SystemExit(f"backup: {message}")


def require(condition: bool, message: str) -> None:
    if not condition:
        die(message)


def ensure_key(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(handle, "wb") as stream:
            stream.write(os.urandom(32).hex().encode("ascii"))
    os.chmod(path, 0o600)


def _openssl(arguments: list[str]) -> None:
    completed = subprocess.run(["openssl", *arguments], capture_output=True)
    require(
        completed.returncode == 0,
        f"openssl failed: {completed.stderr.decode(errors='replace').strip()}",
    )


def seal(source: Path, key: Path, out: Path) -> str:
    require(source.is_dir(), f"backup source is missing: {source}")
    ensure_key(key)
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=out.parent, delete=False) as handle:
        archive = Path(handle.name)
    try:
        with tarfile.open(archive, "w") as tar:
            for path in sorted(source.rglob("*")):
                tar.add(path, arcname=path.relative_to(source).as_posix(), recursive=False)
        _openssl(
            ["enc", "-aes-256-cbc", "-pbkdf2", "-salt", "-pass", f"file:{key}", "-in", str(archive), "-out", str(out)]
        )
    finally:
        archive.unlink(missing_ok=True)
    return hashlib.sha256(out.read_bytes()).hexdigest()


def open_archive(archive: Path, key: Path, dest: Path) -> None:
    require(archive.is_file(), f"backup archive is missing: {archive}")
    require(key.is_file(), f"backup key is missing: {key}")
    require(not dest.exists(), f"refuse to overwrite the restore destination: {dest}")
    dest.mkdir(parents=True)
    with tempfile.NamedTemporaryFile(dir=dest.parent, delete=False) as handle:
        plain = Path(handle.name)
    try:
        _openssl(
            ["enc", "-d", "-aes-256-cbc", "-pbkdf2", "-pass", f"file:{key}", "-in", str(archive), "-out", str(plain)]
        )
        with tarfile.open(plain, "r") as tar:
            tar.extractall(dest, filter="data")
    finally:
        plain.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="backup.py", description=__doc__.splitlines()[0])
    subparsers = parser.add_subparsers(dest="action", required=True)
    key = subparsers.add_parser("key")
    key.add_argument("--path", type=Path, required=True)
    seal_parser = subparsers.add_parser("seal")
    seal_parser.add_argument("--source", type=Path, required=True)
    seal_parser.add_argument("--key", type=Path, required=True)
    seal_parser.add_argument("--out", type=Path, required=True)
    open_parser = subparsers.add_parser("open")
    open_parser.add_argument("--archive", type=Path, required=True)
    open_parser.add_argument("--key", type=Path, required=True)
    open_parser.add_argument("--dest", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.action == "key":
        ensure_key(args.path)
        print(f"key={args.path}")
        return 0
    if args.action == "seal":
        digest = seal(args.source, args.key, args.out)
        print(f"sealed={args.out}")
        print(f"sha256={digest}")
        return 0
    if args.action == "open":
        open_archive(args.archive, args.key, args.dest)
        print(f"opened={args.dest}")
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
