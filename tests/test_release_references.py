"""Onboarding documents may only point at releases that exist.

The first-use guide tells a Windows operator exactly which archive to download. When that name points
at a version nobody has published, the first run dead-ends on a 404 before SolidWorks is even
involved — which is what happened when the guide was written ahead of the release it described.
`SECURITY.md` makes the opposite claim: it states which release a fix would land in, and it said
`0.3.14` while `0.3.17` was the published one, so a reporter was told the wrong version twice over.
`CHANGELOG.md` is the in-repo release list, so it is the authority for both.
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ONBOARDING = (
    ROOT / "README.md",
    ROOT / "README.en.md",
    ROOT / "docs" / "solidworks-first-use.md",
    ROOT / "docs" / "solidworks-first-use.en.md",
)
FIRST_USE = ONBOARDING[2:]
RELEASED = re.compile(r"^## \[(\d+\.\d+\.\d+)\] - ", re.M)
TAG = re.compile(r"releases/tag/v(\d+\.\d+\.\d+)")
ASSET = re.compile(r"description-worker-(\d+\.\d+\.\d+)-windows-x86_64\.zip")
SUPPORTED = re.compile(r"Latest published release \(currently `(\d+\.\d+\.\d+)`\)")


def version(value: str) -> tuple[int, int, int]:
    major, minor, patch = (int(part) for part in value.split("."))
    return major, minor, patch


def newest_release() -> tuple[int, int, int]:
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    versions = [version(found) for found in RELEASED.findall(changelog)]
    if not versions:
        raise AssertionError("CHANGELOG.md should list released versions")
    return max(versions)


def version_problems(text: str, newest: tuple[int, int, int]) -> list[str]:
    """Every release reference in ``text`` that names something newer than ``newest``."""

    problems: list[str] = []
    for pattern, kind in ((TAG, "release link"), (ASSET, "archive name")):
        for found in pattern.findall(text):
            if version(found) > newest:
                problems.append(f"{kind} names {found}, newer than {'.'.join(str(part) for part in newest)}")
    return problems


def supported_problems(text: str, newest: tuple[int, int, int]) -> list[str]:
    """Every claim in ``SECURITY.md`` about which release is supported, if it is not ``newest``."""

    found = SUPPORTED.search(text)
    expected = ".".join(str(part) for part in newest)
    if not found:
        return ["SECURITY.md no longer states which release is supported"]
    if found.group(1) == expected:
        return []
    return [f"SECURITY.md supports {found.group(1)}, but the newest release is {expected}"]


class ReleaseReferenceTests(unittest.TestCase):
    def test_onboarding_never_points_past_the_newest_release(self):
        newest = newest_release()
        for path in ONBOARDING:
            with self.subTest(document=path.name):
                self.assertEqual(
                    version_problems(path.read_text(encoding="utf-8"), newest),
                    [],
                    f"{path.name} names an unpublished release; the newest changelog entry is {newest}",
                )

    def test_a_reference_to_a_newer_release_is_reported(self):
        """The comparison has to fail when the document points ahead, or it proves nothing."""

        newest = (0, 3, 16)
        ahead = (
            "Download description-worker-0.3.17-windows-x86_64.zip from "
            "https://github.com/mimicverse/description/releases/tag/v0.3.17"
        )
        behind = (
            "Download description-worker-0.3.15-windows-x86_64.zip from "
            "https://github.com/mimicverse/description/releases/tag/v0.3.15"
        )
        self.assertEqual(
            version_problems(ahead, newest),
            ["release link names 0.3.17, newer than 0.3.16", "archive name names 0.3.17, newer than 0.3.16"],
        )
        self.assertEqual(version_problems(behind, newest), [])

    def test_the_first_use_guide_downloads_the_newest_release(self):
        """Pointing at an older release still works for a reader; pointing ahead does not."""

        newest = ".".join(str(part) for part in newest_release())
        for path in FIRST_USE:
            text = path.read_text(encoding="utf-8")
            with self.subTest(document=path.name):
                self.assertIn(f"releases/tag/v{newest}", text)
                self.assertIn(f"description-worker-{newest}-windows-x86_64.zip", text)

    def test_the_security_policy_supports_the_newest_release(self):
        """A reporter reads this table to decide whether a fix reaches them."""

        self.assertEqual(supported_problems((ROOT / "SECURITY.md").read_text(encoding="utf-8"), newest_release()), [])

    def test_a_stale_supported_version_is_reported(self):
        """The control: this is the state the file was in, three releases after 0.3.14."""

        stale = "| Latest published release (currently `0.3.14`) | fixes are released here |"
        self.assertEqual(
            supported_problems(stale, (0, 3, 17)), ["SECURITY.md supports 0.3.14, but the newest release is 0.3.17"]
        )
        self.assertEqual(supported_problems(stale, (0, 3, 14)), [])
        self.assertEqual(
            supported_problems("# Security policy\n", (0, 3, 17)),
            ["SECURITY.md no longer states which release is supported"],
        )


if __name__ == "__main__":
    unittest.main()
