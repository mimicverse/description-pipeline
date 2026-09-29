"""The user-facing documents are an operator contract: every command and flag must exist.

A copy-pasted command that names a removed subcommand or flag fails in the worst place — on someone
else's machine, after their CAD is already open.  This test asks each entry point for its help text
and checks the READMEs, the contributing guides and the runbooks against it.
"""

import functools
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLI = [sys.executable, "-m", "description_pipeline"]

CLI_TREES = (
    (),
    ("quickstart",),
    ("doctor",),
    ("build",),
    ("check",),
    ("diff",),
    ("recover",),
    ("model",),
    ("model", "accept"),
    ("model", "init"),
    ("model", "submit"),
    ("model", "update"),
    ("model", "dispatch"),
    ("model", "validate"),
    ("model", "pending"),
    ("model", "promote"),
    ("model", "layout"),
    ("source",),
    ("source", "freeze"),
    ("tool",),
    ("tool", "lock"),
    ("worker",),
)

TOOLS = (
    "tools/audit.py",
    "tools/accept_release.py",
    "tools/audit_dependencies.py",
    "tools/audit_releases.py",
    "tools/build_release.py",
    "tools/verify_distribution.py",
    "tools/quality.py",
    "tools/check_layout.py",
    "tools/run_simulation_acceptance.py",
)

#: Flags the guides legitimately show for other programs (git, git-lfs, pip, GitHub CLI).
EXTERNAL_FLAGS = {
    "--abi",
    "--atomic",  # git push, in the promotion description
    "--branch",
    "--dest",
    "--ff-only",
    "--find-links",
    "--force-with-lease",  # git push, in the promotion description
    "--global",
    "--implementation",
    "--jq",
    "--local",
    "--no-build-isolation",
    "--no-deps",
    "--no-index",
    "--notes-file",
    "--only-binary",
    "--platform",
    "--private",  # gh repo create, for a writable model repository
    "--python",  # uv, in the contributor venv note
    "--python-version",
    "--require-hashes",
    "--seed",  # uv, in the contributor venv note
    "--source",  # coverage.py, in the CONTRIBUTING coverage recipe
    "--title",
    "--verify-tag",
}

SHELLS = ("sh", "bash", "powershell", "shell", "text")


def documents() -> list[Path]:
    """Every current user-facing document; ``docs/history`` records past releases."""

    return [
        ROOT / "README.md",
        ROOT / "README.en.md",
        ROOT / "CONTRIBUTING.md",
        ROOT / "CONTRIBUTING.en.md",
        ROOT / "RELEASING.md",
        *sorted((ROOT / "examples").rglob("README.md")),
        *sorted(path for path in (ROOT / "docs").rglob("*.md") if "history" not in path.relative_to(ROOT).parts),
    ]


@functools.cache
def help_text(arguments: tuple[str, ...]) -> tuple[int, str]:
    """The exit code and help output; argparse prints usage for an invalid choice as well."""

    result = subprocess.run([*CLI, *arguments, "--help"], cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
    return result.returncode, (result.stdout or "") + (result.stderr or "")


@functools.cache
def known_flags() -> frozenset[str]:
    known = set(EXTERNAL_FLAGS)
    for tree in CLI_TREES:
        known.update(re.findall(r"--[a-z][a-z0-9-]*", help_text(tree)[1]))
    for tool in TOOLS:
        result = subprocess.run(
            [sys.executable, tool, "--help"], cwd=ROOT, capture_output=True, text=True, encoding="utf-8"
        )
        known.update(re.findall(r"--[a-z][a-z0-9-]*", (result.stdout or "") + (result.stderr or "")))
    return frozenset(known)


def command_lines() -> list[tuple[Path, str]]:
    found: list[tuple[Path, str]] = []
    for path in documents():
        text = path.read_text(encoding="utf-8")
        for language, block in re.findall(r"```(\w*)\n(.*?)```", text, re.S):
            if language and language not in SHELLS:
                continue
            for line in block.splitlines():
                stripped = line.strip()
                if stripped.startswith(("description ", "& $Python -m description_pipeline ")):
                    found.append((path, stripped))
        for inline in re.findall(r"`([^`\n]*description [^`\n]*)`", text):
            found.append((path, inline.strip()))
    return found


class DocumentedCommandTests(unittest.TestCase):
    def test_documented_subcommands_exist(self):
        checked = 0
        for path, line in command_lines():
            remainder = line
            for prefix in ("& $Python -m description_pipeline", "description"):
                if remainder.startswith(prefix):
                    remainder = remainder[len(prefix) :]
                    break
            remainder = re.sub(r"[<>$][^\s]*", "value", remainder.replace("\\", " "))
            tokens = [token for token in remainder.split() if token]
            subcommands: list[str] = []
            for token in tokens:
                if token.startswith("-"):
                    break
                if re.fullmatch(r"[a-z][a-z-]*", token):
                    subcommands.append(token)
                else:
                    break
            if not subcommands:
                continue
            code, text = help_text(tuple(subcommands))
            self.assertIn("usage:", text, f"{path.name}: `{line}` printed no usage")
            # A wrong subcommand gets the usage banner too, so the exit code is what proves it exists.
            self.assertEqual(code, 0, f"{path.name}: `{line}` is not a real command: {text.strip().splitlines()[-1]}")
            checked += 1
        self.assertGreater(checked, 5, "the documents should contain runnable command examples")

    def test_a_command_that_does_not_exist_is_rejected(self):
        """The positive control for the check above: this is what a stale command looks like."""

        code, text = help_text(("source", "fetch"))
        self.assertNotEqual(code, 0, text)
        self.assertIn("invalid choice", text)

    def test_documented_flags_exist(self):
        known = known_flags()
        missing: list[str] = []
        for path in documents():
            text = path.read_text(encoding="utf-8")
            for flag in sorted(set(re.findall(r"(?<![\w-])--[a-z][a-z0-9-]+", text))):
                if flag not in known:
                    missing.append(f"{path.relative_to(ROOT).as_posix()}: {flag}")
        self.assertEqual(missing, [], "documented flags must exist in a real entry point")


#: A `description …` command inside a generated hint: either the whole line, or backticked inside a
#: sentence like "run `description model accept --root … --out <result directory>` and keep its record".
COMMAND_IN_HINT = re.compile(r"(?:^|`)description ([^`\n]+)")


def hint_problems(hints: list[str]) -> list[str]:
    """Findings for a generated `next:`/`then` hint that names a command or flag the CLI does not have.

    The documents are checked by ``DocumentedCommandTests``; these lines are generated while the tool
    runs and are the ones a newcomer pastes into a shell, so a stale flag there fails on *their*
    machine, after the work is done, rather than here.
    """

    problems: list[str] = []
    known = known_flags()
    for hint in hints:
        for match in COMMAND_IN_HINT.finditer(hint):
            line = "description " + match.group(1).strip()
            remainder = re.sub(r"[<>$][^\s]*", "value", line[len("description ") :].replace("\\", " "))
            subcommands: list[str] = []
            flags: list[str] = []
            for token in remainder.split():
                if token.startswith("-"):
                    flags.append(token)
                elif not flags and re.fullmatch(r"[a-z][a-z-]*", token):
                    subcommands.append(token)
            if not subcommands:
                continue
            code, text = help_text(tuple(subcommands))
            if code != 0:
                problems.append(f"{line!r} is not a real command: {text.strip().splitlines()[-1]}")
                continue
            for flag in flags:
                if flag not in known:
                    problems.append(f"{line!r} uses an unknown flag {flag}")
    return problems


class GeneratedHintTests(unittest.TestCase):
    """The lines the tool tells a user to run next have to be commands that exist."""

    def run_json(self, arguments: list[str]) -> dict:
        result = subprocess.run([*CLI, *arguments], cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
        text = result.stdout
        self.assertIn("{", text, result.stderr)
        return json.loads(text[text.index("{") :])

    def test_the_next_lines_a_run_generates_are_real_commands(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "demo-arm"
            hints: list[str] = []
            for arguments in (
                ["quickstart", str(workspace)],
                ["tool", "lock", "--root", str(workspace)],
                ["build", "--root", str(workspace), "--profile", "kinematics"],
                ["check", "--root", str(workspace), "--profile", "kinematics"],
            ):
                with self.subTest(command=arguments[0]):
                    hints += self.run_json(arguments).get("next") or []
        self.assertGreater(len(hints), 4, hints)
        self.assertEqual(hint_problems(hints), [])

    def test_a_hint_with_a_stale_flag_is_reported(self):
        """The control: a hint that names a flag nobody implements is exactly what this catches."""

        self.assertEqual(
            hint_problems(["description check --root . --profile kinematics"]),
            [],
        )
        self.assertEqual(
            hint_problems(["description model accept --root . --out <result directory> --dry-run"]),
            ["'description model accept --root . --out <result directory> --dry-run' uses an unknown flag --dry-run"],
        )
        self.assertEqual(
            len(hint_problems(["description source fetch --root ."])),
            1,
        )
        report = hint_problems(["description source fetch --root ."])[0]
        self.assertIn("is not a real command", report)
        self.assertIn("invalid choice", report)


if __name__ == "__main__":
    unittest.main()
