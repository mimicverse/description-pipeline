"""Auditing every published release has to be able to fail, on real artifact shapes.

`tools/audit_releases.py` is the cheap check that can run over the whole release history: each asset
against the `SHA256SUMS` published beside it, and each artifact's packaged identity against the commit
its tag points at.  The shapes it has to survive are the ones the release page actually holds — an old
bundle named without the platform suffix, a re-published tag whose artifacts carry the plain version,
a wheel (a ZIP) and an sdist (a tarball) — so those are what the tests build.
"""

import io
import json
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from tools import accept_release, audit_releases

COMMIT = "a" * 40
IDENTITY = "description_pipeline/tool-release.json"


def zipped(path: Path, identity: dict | None = None, *, entry: str = IDENTITY) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        if identity is not None:
            archive.writestr(entry, json.dumps(identity))
        else:
            archive.writestr("description_pipeline/__init__.py", "")
    return path


def tarred(path: Path, identity: dict) -> Path:
    payload = json.dumps(identity).encode()
    with tarfile.open(path, "w:gz") as archive:
        info = tarfile.TarInfo(f"mimicverse_description-1.2.3/{IDENTITY}")
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))
    return path


def sums_of(root: Path) -> dict[str, str]:
    return {path.name: accept_release.sha256(path) for path in sorted(root.iterdir()) if path.is_file()}


class IdentityTests(unittest.TestCase):
    def test_a_zip_and_a_tarball_both_yield_their_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            wheel = zipped(root / "description_pipeline-1.2.3-py3-none-any.whl", {"version": "1.2.3"})
            sdist = tarred(root / "mimicverse_description-1.2.3.tar.gz", {"version": "1.2.3"})
            with zipfile.ZipFile(root / "bundle.zip", "w") as archive:
                archive.writestr("src/description_pipeline/tool-release.json", json.dumps({"version": "1.2.3"}))
            self.assertEqual(audit_releases.identity_of(wheel), {"version": "1.2.3"})
            self.assertEqual(audit_releases.identity_of(sdist), {"version": "1.2.3"})
            self.assertEqual(audit_releases.identity_of(root / "bundle.zip"), {"version": "1.2.3"})

    def test_an_artifact_without_an_identity_is_not_a_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            self.assertIsNone(audit_releases.identity_of(zipped(Path(temporary) / "old-0.2.0.zip")))


class ArtifactAuditTests(unittest.TestCase):
    def release(self, root: Path, **identity: object) -> tuple[Path, dict[str, str]]:
        payload = {"version": "1.2.3", "source_commit": COMMIT, "development": False, **identity}
        zipped(root / "description-worker-1.2.3-windows-x86_64.zip", payload)
        tarred(root / "mimicverse_description-1.2.3.tar.gz", payload)
        return root, sums_of(root)

    def problems(self, commit_map: dict[str, str] | None = None, **identity: object) -> tuple[list[str], list[str]]:
        with tempfile.TemporaryDirectory() as temporary:
            root, sums = self.release(Path(temporary), **identity)
            return audit_releases.artifact_problems("v1.2.3", COMMIT, sums, root, commit_map)

    def test_a_release_that_matches_its_tag_has_no_findings(self):
        self.assertEqual(self.problems(), ([], []))

    def test_a_mismatched_digest_is_reported(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, sums = self.release(Path(temporary))
            sums["mimicverse_description-1.2.3.tar.gz"] = "0" * 64
            findings, _ = audit_releases.artifact_problems("v1.2.3", COMMIT, sums, root)
        self.assertEqual(findings, ["v1.2.3: mimicverse_description-1.2.3.tar.gz does not match the published SHA-256"])

    def test_an_identity_that_names_another_commit_is_reported(self):
        findings, translated = self.problems(source_commit="b" * 40)
        self.assertEqual(len(findings), 2)
        self.assertTrue(all("not " + COMMIT[:12] in finding for finding in findings))
        self.assertEqual(translated, [])

    def test_a_pre_publication_identity_is_resolved_by_the_published_map(self):
        """Artifacts built before a history rewrite name the old commit; the map is the bridge."""

        old = "c" * 40
        findings, translated = self.problems(commit_map={old: COMMIT}, source_commit=old)
        self.assertEqual(findings, [])
        self.assertEqual(
            translated, ["description-worker-1.2.3-windows-x86_64.zip", "mimicverse_description-1.2.3.tar.gz"]
        )
        # A map that names a different successor proves nothing, so the finding stays.
        findings, translated = self.problems(commit_map={old: "d" * 40}, source_commit=old)
        self.assertEqual(len(findings), 2)
        self.assertEqual(translated, [])

    def test_a_development_identity_is_reported(self):
        self.assertEqual(len(self.problems(development=True)[0]), 2)

    def test_a_version_its_name_does_not_carry_is_reported(self):
        """The control for re-published tags: the artifact, not the tag, has to say which version it is."""

        findings = self.problems(version="9.9.9")[0]
        self.assertEqual(len(findings), 2)
        self.assertTrue(all("its name does not carry" in finding for finding in findings))

    def test_artifacts_that_disagree_about_their_version_are_reported(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, _ = self.release(Path(temporary))
            zipped(
                root / "description-worker-1.2.3-windows-x86_64.zip",
                {"version": "1.2.4", "source_commit": COMMIT, "development": False},
            )
            findings, _ = audit_releases.artifact_problems("v1.2.3", COMMIT, sums_of(root), root)
        self.assertIn("v1.2.3: the artifacts disagree about their version: ['1.2.3', '1.2.4']", findings)


class TagTests(unittest.TestCase):
    def test_tags_are_ordered_by_publication(self):
        payload = json.dumps(
            [
                {"tag_name": "v1.2.0", "published_at": "2026-01-02T00:00:00Z"},
                {"tag_name": "tool/v0.9.0", "published_at": "2025-01-02T00:00:00Z"},
            ]
        )
        with mock.patch.object(audit_releases, "run", return_value=mock.Mock(stdout=payload)):
            self.assertEqual(audit_releases.published_tags(), ["tool/v0.9.0", "v1.2.0"])

    def test_a_tag_without_a_local_commit_is_refused(self):
        with self.assertRaisesRegex(accept_release.AcceptanceError, "cannot resolve the tag"):
            audit_releases.tag_commit("no-such-tag-4c1f")


if __name__ == "__main__":
    unittest.main()
