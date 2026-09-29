"""The license and the code of conduct are release contract, not decoration.

GitHub surfaces the repository as Apache-2.0 and Contributor Covenant, but the gate itself reads
neither: the LICENSE could be swapped, the conduct report address cleared, the ``license-files``
metadata dropped, or the CONTRIBUTING links removed, and every distribution would keep shipping
with a clean 6/6.  The public-release choices (2026-09-29) are Apache-2.0 as the license and the
Contributor Covenant 2.1 as the code of conduct, stated in both language versions of CONTRIBUTING;
these checks pin them, with planted defects proving each check can reject.
"""

from __future__ import annotations

import re
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: The canonical Apache-2.0 text as OSI and the license detectors define it.
APACHE_MARKERS = (
    "Apache License",
    "Version 2.0, January 2004",
    "TERMS AND CONDITIONS FOR USE, REPRODUCTION, AND DISTRIBUTION",
    "APPENDIX: How to apply the Apache License to your work.",
)
#: The canonical Contributor Covenant 2.1 markers, including the enforcement guidelines.
CONDUCT_MARKERS = (
    "Contributor Covenant Code of Conduct",
    "version 2.1",
    "https://www.contributor-covenant.org/version/2/1/code_of_conduct.html",
    "## Enforcement",
)
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")


def license_problems(text: str) -> list[str]:
    """What the Apache-2.0 file is missing, including an unfilled copyright placeholder."""

    found = [f"missing {marker!r}" for marker in APACHE_MARKERS if marker not in text]
    if "[yyyy]" in text:
        found.append("the appendix copyright placeholder is still present")
    return found


def conduct_problems(text: str) -> list[str]:
    """What the Contributor Covenant file is missing, report address included."""

    found = [f"missing {marker!r}" for marker in CONDUCT_MARKERS if marker not in text]
    heading = re.search(r"^## Enforcement\s*$", text, re.MULTILINE)
    enforcement = text[heading.end() :].split("\n## ", 1)[0] if heading else ""
    if "[INSERT CONTACT METHOD]" in text or not EMAIL.search(enforcement):
        found.append("the enforcement section names no report address")
    return found


def contributing_problems(text: str) -> list[str]:
    """What a CONTRIBUTING version fails to state about the license and the conduct."""

    found = []
    for name, needle in (
        ("the code of conduct", "CODE_OF_CONDUCT.md"),
        ("the license", "LICENSE"),
        ("the license name", "Apache-2.0"),
        ("the contribution license", "inbound = outbound"),
    ):
        if needle not in text:
            found.append(f"does not name {name}")
    return found


class CommunityFileTests(unittest.TestCase):
    def test_the_package_metadata_declares_apache_2_0_and_ships_the_file(self):
        with (ROOT / "pyproject.toml").open("rb") as handle:
            project = tomllib.load(handle)["project"]
        self.assertEqual(project["license"], "Apache-2.0")
        self.assertEqual(list(project["license-files"]), ["LICENSE"])
        self.assertTrue((ROOT / "LICENSE").is_file())

    def test_the_license_file_is_the_canonical_apache_text(self):
        text = (ROOT / "LICENSE").read_text(encoding="utf-8")
        self.assertEqual(license_problems(text), [])
        self.assertRegex(text, r"Copyright \d{4} MimicVerse", "the appendix must name the holder")

    def test_the_code_of_conduct_is_the_contributor_covenant(self):
        text = (ROOT / "CODE_OF_CONDUCT.md").read_text(encoding="utf-8")
        self.assertEqual(conduct_problems(text), [])

    def test_both_contributing_versions_state_the_license_and_the_conduct(self):
        for name in ("CONTRIBUTING.md", "CONTRIBUTING.en.md"):
            with self.subTest(document=name):
                text = (ROOT / name).read_text(encoding="utf-8")
                self.assertEqual(contributing_problems(text), [])
                self.assertIsNotNone(EMAIL.search(text), f"{name} names no report address")

    def test_the_checks_reject_planted_defects(self):
        """Each predicate has to fail on a file the standard would not accept."""

        mit = "MIT License\n\nPermission is hereby granted, free of charge, to any person\n"
        self.assertTrue(license_problems(mit))
        canonical = (ROOT / "LICENSE").read_text(encoding="utf-8")
        placeholder = "Copyright [yyyy] [name of copyright owner]"
        self.assertTrue(license_problems(canonical.replace("Copyright 2026 MimicVerse", placeholder)))
        covenant = (ROOT / "CODE_OF_CONDUCT.md").read_text(encoding="utf-8")
        self.assertTrue(conduct_problems(covenant.replace("andy.cui@mimicverse.ai", "[INSERT CONTACT METHOD]")))
        self.assertTrue(conduct_problems("# Code of conduct\n\nBe nice.\n"))
        contributing = (ROOT / "CONTRIBUTING.en.md").read_text(encoding="utf-8")
        self.assertTrue(contributing_problems(contributing.replace("inbound = outbound", "as-is")))
        self.assertTrue(contributing_problems("Just send a pull request.\n"))


if __name__ == "__main__":
    unittest.main()
