"""The secret scan has to stay repeatable, and its allow-list has to stay small.

`gitleaks detect` over all 553 commits of this repository reported 33 findings; every one was one of
two patterns that carry no credential (a build `generation_key` digest in the generated
`docs/quality.json`, and a module attribute a threading test patches). `.gitleaks.toml` allow-lists
exactly those, keeps the default rules on, and is what makes the scan a step the release checklist can
require. These checks keep it from growing quietly into a list of excuses.
"""

import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / ".gitleaks.toml"

#: The only files whose `generic-api-key` hits were inspected and cleared, with why.
ALLOWED_PATHS = {
    "(^|/)docs/quality\\.json$": "the build writes a generation_key digest into its quality report",
    "(^|/)tests/solidworks_export/test_com_threading\\.py$": "the test patches a module attribute",
}


def config_problems(text: str) -> list[str]:
    """What would make the scan either noisy again or quietly blind."""

    try:
        parsed = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        return [f".gitleaks.toml does not parse: {error}"]
    problems: list[str] = []
    if not parsed.get("extend", {}).get("useDefault"):
        problems.append("the default gitleaks rules are no longer extended")
    allowlists = parsed.get("allowlists") or []
    if len(allowlists) != 1:
        problems.append(f"expected exactly one allow-list, found {len(allowlists)}")
        return problems
    paths = set(allowlists[0].get("paths") or [])
    problems += [f"unexpected allow-list entry: {path}" for path in sorted(paths - set(ALLOWED_PATHS))]
    problems += [f"allow-listed path is missing: {path}" for path in sorted(set(ALLOWED_PATHS) - paths)]
    allowed_keys = {"description", "paths"}
    problems += [
        f"the allow-list grew a {key!r} key, which widens it silently"
        for key in sorted(set(allowlists[0]) - allowed_keys)
    ]
    return problems


class SecretScanConfigTests(unittest.TestCase):
    def test_the_config_keeps_the_defaults_and_exactly_two_inspected_exceptions(self):
        self.assertEqual(config_problems(CONFIG.read_text(encoding="utf-8")), [])

    def test_a_widened_allow_list_is_reported(self):
        """The control: another allow-list, or a broader path, is how it turns into a blind spot."""

        text = CONFIG.read_text(encoding="utf-8")
        broader = config_problems(text.replace("(^|/)docs/quality\\.json$", "src/.*"))
        self.assertIn("unexpected allow-list entry: src/.*", broader)
        self.assertIn("allow-listed path is missing: (^|/)docs/quality\\.json$", broader)
        self.assertIn(
            "expected exactly one allow-list, found 2",
            config_problems(text + "\n[[allowlists]]\ndescription = \"anything\"\npaths = ['''src/.*''']\n"),
        )
        self.assertEqual(
            config_problems(text.replace("useDefault = true", "useDefault = false")),
            ["the default gitleaks rules are no longer extended"],
        )

    def test_a_broken_config_is_reported(self):
        self.assertEqual(len(config_problems("title = [")), 1)


if __name__ == "__main__":
    unittest.main()
