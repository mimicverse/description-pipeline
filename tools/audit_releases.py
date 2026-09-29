"""Audit the published releases: their bytes, their digests and the commit each artifact names.

    python tools/audit_releases.py                  # every release on the release page
    python tools/audit_releases.py --assets DIR     # releases already downloaded under DIR/<tag>

``tools/accept_release.py`` installs one release and re-derives everything a user relies on; this is the
cheap check that can run over every historical release, which is what a repository move, a mirror or a
suspicious upload needs: each asset must match the ``SHA256SUMS`` published beside it, and each artifact
that carries a packaged identity must name the commit its tag points at.

A repository whose history was rewritten before publication carries ``docs/history/commit-map.txt``;
when it is present, an artifact that names a pre-publication commit is checked against that commit's
successor, and the release's line says so.

Exit codes: 0 every release checked out, 1 a release did not, 2 the release list or an artifact could
not be read.
"""

from __future__ import annotations

import argparse
import json
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.accept_release import (  # noqa: E402
    COMMIT_MAP,
    AcceptanceError,
    parse_sums,
    read_commit_map,
    run,
    sha256,
)

REPOSITORY = "mimicverse/description-pipeline"
SUMS = "SHA256SUMS"
PACKAGED_IDENTITY = "src/description_pipeline/tool-release.json"
WHEEL_IDENTITY = "description_pipeline/tool-release.json"
#: Suffixes that can carry a packaged identity; a release predating it simply has none.
ARTIFACT_SUFFIXES = (".zip", ".tar.gz", ".whl")


def published_tags() -> list[str]:
    """Every tag the release page carries, oldest first, as ``gh`` reports them."""

    listed = run(["gh", "api", f"repos/{REPOSITORY}/releases?per_page=100"]).stdout
    releases = json.loads(listed)
    return [release["tag_name"] for release in sorted(releases, key=lambda item: item["published_at"])]


def tag_commit(tag: str) -> str:
    """The commit a tag points at, peeled; the repository under test is the local checkout."""

    try:
        return run(["git", "-C", str(ROOT), "rev-parse", f"{tag}^{{commit}}"]).stdout.strip()
    except AcceptanceError as error:
        raise AcceptanceError(f"{tag}: cannot resolve the tag in this checkout ({error})") from error


def version_of(tag: str) -> str:
    return tag.rsplit("/", 1)[-1].lstrip("v")


def identity_of(path: Path) -> dict | None:
    """The tool metadata an artifact carries, or ``None`` when that version predates it."""

    # A wheel is a ZIP and an sdist is a tarball, and the older releases name their bundles by hand;
    # the container is read from the bytes rather than guessed from the suffix.
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            for name in (PACKAGED_IDENTITY, WHEEL_IDENTITY, "toolchain.json"):
                if name in archive.namelist():
                    return json.loads(archive.read(name))
        return None
    with tarfile.open(path) as archive:
        for member in archive.getmembers():
            if member.name.endswith(WHEEL_IDENTITY):
                stream = archive.extractfile(member)
                return json.loads(stream.read()) if stream else None
    return None


def artifact_problems(
    tag: str, commit: str, expected: dict[str, str], artifacts: Path, commit_map: dict[str, str] | None = None
) -> tuple[list[str], list[str]]:
    """Digest and identity findings for one release, and the assets the commit map translated.

    An empty finding list means it checked out.  A pre-publication commit is only accepted when the
    published map says it is the accepted commit's predecessor, and the caller can then say which
    assets relied on that.
    """

    problems: list[str] = []
    translated: list[str] = []
    for name, digest in sorted(expected.items()):
        path = artifacts / name
        if not path.is_file():
            problems.append(f"{tag}: missing asset {name}")
        elif sha256(path) != digest:
            problems.append(f"{tag}: {name} does not match the published SHA-256")
    # The version comes from the artifacts, not from the tag: a release may be re-published under a
    # label (`v0.3.14-assets`) and an older one names its bundle without the platform suffix, so the
    # tag would guess wrong in both directions.
    declared: set[str] = set()
    for path in sorted(artifacts.iterdir()):
        if not path.is_file() or not path.name.endswith(ARTIFACT_SUFFIXES):
            continue
        identity = identity_of(path)
        if identity is None:
            continue
        version = str(identity.get("version"))
        declared.add(version)
        named = str(identity.get("source_commit"))
        if named != commit and commit_map and commit_map.get(named) == commit:
            translated.append(path.name)
        elif named != commit:
            problems.append(f"{tag}: {path.name} names {named[:12]} as its commit, not {commit[:12]}")
        if version not in path.name:
            problems.append(f"{tag}: {path.name} declares version {version!r}, which its name does not carry")
        if identity.get("development"):
            problems.append(f"{tag}: {path.name} carries a development identity")
    if len(declared) > 1:
        problems.append(f"{tag}: the artifacts disagree about their version: {sorted(declared)}")
    return problems, translated


def download(tag: str, destination: Path) -> Path:
    """The release's assets, downloaded beside each other exactly as the release page holds them."""

    directory = destination / tag.replace("/", "-")
    directory.mkdir(parents=True, exist_ok=True)
    run(["gh", "release", "download", tag, "--repo", REPOSITORY, "--dir", str(directory), "--clobber"])
    return directory


def audit(
    tags: list[str], assets: Path | None, commit_map: dict[str, str] | None = None
) -> tuple[list[str], list[str]]:
    """Audit every tag's assets; returns the findings and the tags whose identities needed the map."""

    problems: list[str] = []
    mapped_tags: list[str] = []
    temporary: tempfile.TemporaryDirectory | None = None
    if assets is None:
        temporary = tempfile.TemporaryDirectory(prefix="release-audit-")
        assets = Path(temporary.name)
    try:
        for tag in tags:
            try:
                directory = assets / tag.replace("/", "-") if assets else Path()
                if not (directory / SUMS).is_file():
                    directory = download(tag, assets)
                commit = tag_commit(tag)
                findings, translated = artifact_problems(
                    tag, commit, parse_sums(directory / SUMS), directory, commit_map
                )
            except AcceptanceError as error:
                problems.append(str(error))
                continue
            problems += findings
            if translated:
                mapped_tags.append(tag)
            state = "ok" if not findings else f"{len(findings)} problems"
            marker = " (commit map)" if translated else ""
            print(f"{tag:18s} {commit[:12]}  {len(list(directory.glob('*')))} entries  {state}{marker}")
    finally:
        if temporary is not None:
            temporary.cleanup()
    return problems, mapped_tags


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--assets", type=Path, help="directory holding <tag>/asset sets instead of downloading")
    parser.add_argument("--tag", action="append", help="only this tag (repeatable); default: every release")
    parser.add_argument(
        "--commit-map",
        type=Path,
        help="rewritten-history map; default: docs/history/commit-map.txt when the checkout carries one",
    )
    args = parser.parse_args(argv)
    try:
        tags = args.tag or published_tags()
        map_path = args.commit_map or (COMMIT_MAP if COMMIT_MAP.is_file() else None)
        commit_map = read_commit_map(map_path) if map_path else None
    except (AcceptanceError, json.JSONDecodeError) as error:
        print(f"refused: {error}", file=sys.stderr)
        return 2
    if not tags:
        print("refused: the release page lists no releases", file=sys.stderr)
        return 2
    problems, mapped_tags = audit(tags, args.assets, commit_map)
    for line in problems:
        print(f"  {line}")
    if problems:
        print(f"FAIL: {len(problems)} findings across {len(tags)} releases")
        return 1
    resolved = (
        f"; {len(mapped_tags)} of them name pre-publication commits resolved through the commit map"
        if mapped_tags
        else ""
    )
    print(f"PASS: {len(tags)} releases match their published digests and the commits their tags name{resolved}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
