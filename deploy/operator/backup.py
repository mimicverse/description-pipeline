#!/usr/bin/env python3
"""Seal/open the controlled backup archive: one tar stream through aes-256-cbc.

The key file lives under the state root (0600) and never leaves it; the sealed archive is the
single artifact the promotion manifest binds, and ``open`` reproduces the sealed bytes exactly.

``clean-staging`` is the one owner of private-staging cleanup: it removes exactly
``<receipt dir>/backup`` - the tree ``seal`` and ``manifest`` bind - and never a source root,
the sealed archive, the manifest, the key reference or the recorded evidence; a non-canonical
receipt path (a ``.`` or ``..`` component, or a symlinked component) is refused before
anything is touched.

Exit codes: 0 success; 1 error (message on stderr); 2 usage error.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

import install_record

MANIFEST_SCHEMA = "solidworks-to-urdf.promote-backup/v1"
CIPHER = "aes-256-cbc"
STAGING_DIRNAME = "backup"


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


def _reject_duplicate_keys(pairs):
    seen = set()
    for key, _ in pairs:
        if key in seen:
            die(f"duplicate manifest key: {key!r}")
        seen.add(key)
    return dict(pairs)


def load_manifest(path: Path) -> dict:
    """The one strict manifest reader: no duplicate keys, exact fields, verified cipher."""

    try:
        manifest = json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicate_keys)
    except (OSError, ValueError) as error:
        die(f"cannot read the backup manifest: {error}")
    require(
        isinstance(manifest, dict) and set(manifest) == {"schema", "files", "encrypted"},
        "the backup manifest carries unknown or missing fields",
    )
    require(manifest["schema"] == MANIFEST_SCHEMA, "the backup manifest schema is not supported")
    sealed = manifest["encrypted"]
    require(
        isinstance(sealed, dict) and set(sealed) == {"path", "sha256", "cipher", "key"},
        "the backup manifest encrypted block is malformed",
    )
    require(sealed["cipher"] == CIPHER, f"unsupported backup cipher: {sealed['cipher']!r}")
    require(
        isinstance(sealed["sha256"], str) and len(sealed["sha256"]) == 64,
        "the backup manifest sealed digest is malformed",
    )
    require(all(isinstance(sealed[key], str) and sealed[key] for key in ("path", "key")), "sealed paths are malformed")
    declared = manifest["files"]
    require(isinstance(declared, dict) and bool(declared), "the backup manifest carries no member digests")
    validated = {}
    for name, checksum in declared.items():
        validated[install_record.confined_relative(name, "manifest member")] = checksum
    for name, checksum in validated.items():
        require(
            isinstance(checksum, str) and len(checksum) == 64,
            f"manifest member digest is malformed: {name}",
        )
    manifest["files"] = validated
    return manifest


def write_manifest(source: Path, sealed: Path, key: Path, out: Path) -> dict:
    """The one manifest writer: same format the strict reader accepts, digests from the source."""

    require(source.is_dir(), f"backup source is missing: {source}")
    require(sealed.is_file(), f"sealed archive is missing: {sealed}")
    files = {
        path.relative_to(source).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(source.rglob("*"))
        if path.is_file()
    }
    require(files, "the backup source carries no files")
    manifest = {
        "schema": MANIFEST_SCHEMA,
        "files": files,
        "encrypted": {
            "path": str(sealed),
            "sha256": hashlib.sha256(sealed.read_bytes()).hexdigest(),
            "cipher": CIPHER,
            "key": str(key),
        },
    }
    out.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


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


def open_archive(archive: Path, key: Path, manifest_path: Path, dest: Path) -> None:
    require(archive.is_file(), f"backup archive is missing: {archive}")
    require(key.is_file(), f"backup key is missing: {key}")
    manifest = load_manifest(manifest_path)
    sealed = manifest["encrypted"]
    expected = sealed["sha256"]
    require(
        isinstance(expected, str) and hashlib.sha256(archive.read_bytes()).hexdigest() == expected,
        "the sealed archive digest does not match the manifest",
    )
    validated = manifest["files"]
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
    for name, checksum in validated.items():
        restored = dest / name
        require(
            restored.is_file() and hashlib.sha256(restored.read_bytes()).hexdigest() == checksum,
            f"restored member differs from the manifest: {name}",
        )
    extras = sorted(
        path.relative_to(dest).as_posix()
        for path in dest.rglob("*")
        if path.is_file() and path.relative_to(dest).as_posix() not in validated
    )
    require(not extras, f"the archive carries members outside the manifest: {extras[:3]}")


def _remove_staging_tree(root: Path) -> None:
    """Depth-first removal of one real directory strictly inside the private staging.

    Only this subtree is ever modified: every real directory has its mode normalized u+rwx
    before its entries are removed (the staging copies carry the source roots' read-only
    modes, which block unlink and rmdir), and symlinks are unlinked as leaves - never
    followed - so no chmod or unlink ever reaches a symlink target or any path outside root.
    """

    os.chmod(root, stat.S_IMODE(os.stat(root).st_mode) | 0o700)
    for entry in os.scandir(root):
        if entry.is_symlink():
            os.unlink(entry.path)
        elif entry.is_dir(follow_symlinks=False):
            _remove_staging_tree(Path(entry.path))
        else:
            os.unlink(entry.path)
    os.rmdir(root)


def _require_canonical_receipt_path(receipt: Path) -> None:
    """The receipt path must provably name the directory it spells, before anything is touched.

    A ``.`` or ``..`` component, or a symlinked component anywhere in the chain, would let the
    cleanup resolve to another real location than the argument reads as (the PR #61 boundary
    probes).  The raw input must equal its non-strict resolve, and every component of the chain
    must be a real directory entry - never a symlink.  Either refusal happens ahead of any
    chmod or unlink, so the whole target stays untouched.
    """

    require(
        str(receipt) == os.path.realpath(receipt),
        f"the receipt directory must be a canonical path (no '.' or '..' or symbolic link components); "
        f"refused: {receipt}",
    )
    chain = Path(receipt.anchor)
    for part in receipt.parts[1:]:
        chain = chain / part
        require(
            not chain.is_symlink(),
            f"the receipt path must not contain a symbolic link component; refused: {chain}",
        )


def remove_private_staging(receipt_dir: Path) -> bool:
    """The one owner of private-staging cleanup: remove ``<receipt_dir>/backup``, nothing else.

    The promotion flow copies the running deployment's roots into that staging with their
    original modes (``cp -a`` / ``copytree`` preserve 0555 directories and 0444 files), so a
    plain ``rm -rf`` cannot remove it and a retry's ``rmtree`` fails the same way.  The guards
    make the target provable: the receipt path must be canonical (no '.'/'..' and no symlinked
    component, refused ahead of any mutation), the receipt directory must carry
    ``receipt.json``, and the staging root itself must be a real directory - a symlink is
    refused, never followed.  Returns whether the staging existed.
    """

    receipt = Path(receipt_dir)
    require(receipt.is_absolute(), "clean-staging requires an absolute receipt directory")
    _require_canonical_receipt_path(receipt)
    marker = receipt / "receipt.json"
    require(marker.is_file() and not marker.is_symlink(), f"not a promotion receipt directory: {receipt}")
    root = receipt / STAGING_DIRNAME
    if root.is_symlink():
        die(f"the private staging is a symbolic link; refused: {root}")
    if not root.exists():
        return False
    require(root.is_dir(), f"the private staging is not a directory; refused: {root}")
    _remove_staging_tree(root)
    return True


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
    open_parser.add_argument("--manifest", type=Path, required=True)
    open_parser.add_argument("--dest", type=Path, required=True)
    manifest_parser = subparsers.add_parser("manifest")
    manifest_parser.add_argument("--source", type=Path, required=True)
    manifest_parser.add_argument("--sealed", type=Path, required=True)
    manifest_parser.add_argument("--key", type=Path, required=True)
    manifest_parser.add_argument("--out", type=Path, required=True)
    clean_parser = subparsers.add_parser("clean-staging")
    clean_parser.add_argument("--receipt-dir", type=Path, required=True)
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
        open_archive(args.archive, args.key, args.manifest, args.dest)
        print(f"opened={args.dest}")
        return 0
    if args.action == "manifest":
        manifest = write_manifest(args.source, args.sealed, args.key, args.out)
        print(f"manifest={args.out}")
        print(f"sealed_sha256={manifest['encrypted']['sha256']}")
        return 0
    if args.action == "clean-staging":
        removed = remove_private_staging(args.receipt_dir)
        print("staging=removed" if removed else "staging=absent")
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
